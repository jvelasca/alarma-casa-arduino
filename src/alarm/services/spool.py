"""Buzon durable para cuando el historial no puede escribir.

El ACK de un evento no puede depender de que SQLite este sano: un disco lleno,
un fichero bloqueado por otro programa o una carpeta de solo lectura no deben
convertir una deteccion en un «confio en que quizas se guarde». Con el buzon,
el orden es siempre el mismo:

1. Se intenta la transaccion en SQLite (evento + clave + estado).
2. Si falla, se escribe el mismo registro en un fichero JSONL y se fuerza a
   disco con ``fsync``.
3. Solo entonces se confirma el evento a la placa.

Al recuperar la base, ``drain_into`` reimporta lo pendiente. La reimportacion es
idempotente porque pasa por ``EventStore.record_motion``: la clave
``(boot_id, seq)`` hace que un registro ya presente no se duplique.

Si tampoco se puede escribir aqui, no se confirma nada. La placa conserva el
evento en su buffer de 16 y lo reenviara, pero la aplicacion lo dice en pantalla
en vez de fingir que la intrusion quedo registrada.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .storage import EventStore

log = logging.getLogger(__name__)

#: Prefijo de los ficheros de buzon. La fecha va detras para ordenarlos.
PREFIX = "events-"
SUFFIX = ".jsonl"
#: Sufijo de un fichero que no se pudo leer: se aparta en vez de borrarlo.
QUARANTINE_SUFFIX = ".bad"


def _has_torn_tail(path: Path) -> bool:
    """Si el fichero existe y no termina en salto de linea.

    Un apagon a mitad de ``write`` deja una linea a medias. Es la senal de que
    hay que separar antes de anadir nada nuevo.
    """
    try:
        if path.stat().st_size == 0:
            return False
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b"\n"
    except OSError:  # pragma: no cover - el fichero puede desaparecer
        return False


@dataclass(frozen=True, slots=True)
class SpoolRecord:
    """Un evento pendiente de entrar en el historial."""

    boot_id: int | None
    seq: int
    tag: str
    kind: str
    detail: str
    ts: float
    state: Mapping[str, str] = field(default_factory=dict)
    remove_state: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "boot_id": self.boot_id,
            "seq": self.seq,
            "tag": self.tag,
            "kind": self.kind,
            "detail": self.detail,
            "ts": self.ts,
            "state": dict(self.state),
            "remove_state": list(self.remove_state),
        }

    @classmethod
    def from_dict(cls, raw: object) -> SpoolRecord | None:
        """Reconstruye un registro; ``None`` si no cumple el minimo."""
        if not isinstance(raw, dict):
            return None
        try:
            boot_id = raw.get("boot_id")
            seq = int(raw["seq"])  # type: ignore[arg-type]
            tag = str(raw["tag"])
            kind = str(raw["kind"])
            detail = str(raw["detail"])
            ts = float(raw["ts"])
        except (KeyError, TypeError, ValueError):
            return None
        state_raw = raw.get("state")
        state: dict[str, str] = {}
        if isinstance(state_raw, dict):
            for key, value in state_raw.items():
                if isinstance(key, str):
                    state[key] = str(value)
        remove_raw = raw.get("remove_state")
        remove: tuple[str, ...] = ()
        if isinstance(remove_raw, list):
            remove = tuple(str(item) for item in remove_raw)
        return cls(
            boot_id=None if boot_id is None else int(boot_id),
            seq=seq,
            tag=tag,
            kind=kind,
            detail=detail,
            ts=ts,
            state=state,
            remove_state=remove,
        )


class EventSpool:
    """Ficheros JSONL con los eventos que el historial no pudo aceptar."""

    def __init__(
        self,
        directory: str | Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._dir = Path(directory)
        self._clock = clock
        self._pending = 0

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def pending(self) -> int:
        """Registros pendientes conocidos (sin volver a leer los ficheros)."""
        return self._pending

    def files(self) -> list[Path]:
        """Ficheros de buzon, del mas antiguo al mas nuevo."""
        if not self._dir.is_dir():
            return []
        found = [
            path
            for path in self._dir.iterdir()
            if path.is_file() and path.name.startswith(PREFIX) and path.name.endswith(SUFFIX)
        ]
        return sorted(found, key=lambda path: path.name)

    def refresh(self) -> int:
        """Recuenta los registros pendientes leyendo los ficheros."""
        total = 0
        for path in self.files():
            total += self._count_lines(path)
        self._pending = total
        return total

    def append(self, record: SpoolRecord) -> bool:
        """Escribe y sincroniza un registro. ``False`` si no se pudo.

        No se guarda un descriptor abierto: el buzon solo se usa cuando SQLite
        falla, asi que abrir y cerrar por evento cuesta poco y evita dejar un
        fichero bloqueado en Windows si algo se cuelga.
        """
        stamp = time.strftime("%Y%m%d", time.localtime(self._clock()))
        path = self._dir / f"{PREFIX}{stamp}{SUFFIX}"
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record.as_dict(), ensure_ascii=False)
            # Si el proceso anterior murio a mitad de una linea, el fichero no
            # termina en salto: escribir pegado convertiria el registro nuevo en
            # parte de la basura. Un salto suelto aisla el fragmento roto.
            torn = _has_torn_tail(path)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                if torn:
                    handle.write("\n")
                handle.write(line + "\n")
                handle.flush()
                # Sin fsync, el sistema puede tener la linea en cache y
                # perderla en un corte de luz: seria confirmar algo que no
                # existe en ningun sitio.
                os.fsync(handle.fileno())
        except OSError as exc:
            log.error("no se pudo escribir el buzon %s: %s", path, exc)
            return False
        self._pending += 1
        return True

    def drain_into(self, store: EventStore) -> int:
        """Reimporta lo pendiente al historial. Devuelve cuantos entraron.

        Un fichero con lineas ilegibles no se tira: las lineas buenas se
        importan y solo las rotas se apartan con la extension ``.bad``, para no
        perder ni los eventos ni la evidencia. El fichero de buzon se borra
        unicamente despues de que todo lo que contenia este a salvo.
        """
        imported = 0
        for path in self.files():
            records, broken = self._read(path)
            if broken:
                log.error(
                    "buzon %s con %d linea(s) ilegible(s): se apartan como %s",
                    path.name,
                    len(broken),
                    path.name + QUARANTINE_SUFFIX,
                )
            try:
                for record in records:
                    _, new = store.record_motion(
                        record.boot_id,
                        record.seq,
                        record.tag,
                        record.kind,
                        record.detail,
                        ts=record.ts,
                        state=record.state,
                        remove_state=record.remove_state,
                    )
                    if new:
                        imported += 1
            except Exception:  # noqa: BLE001 - se reintenta en el proximo arranque
                log.exception("no se pudo reimportar %s", path.name)
                break
            if broken:
                self._quarantine(path, broken)
            else:
                try:
                    path.unlink()
                except OSError as exc:  # pragma: no cover - Windows con fichero abierto
                    log.warning("no se pudo borrar el buzon %s: %s", path.name, exc)
                    break
        self.refresh()
        if imported:
            log.info("buzon reimportado: %d evento(s)", imported)
        return imported

    # -- Interno -----------------------------------------------------------

    @staticmethod
    def _count_lines(path: Path) -> int:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError:
            return 0

    @staticmethod
    def _read(path: Path) -> tuple[list[SpoolRecord], list[str]]:
        """Lee un fichero. Devuelve ``(registros, lineas_ilegibles)``.

        Las lineas rotas se devuelven tal cual, no solo contadas: la cuarentena
        las conserva y el resto del fichero se puede importar igual.
        """
        records: list[SpoolRecord] = []
        broken: list[str] = []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        raw = json.loads(text)
                    except json.JSONDecodeError:
                        broken.append(text)
                        continue
                    record = SpoolRecord.from_dict(raw)
                    if record is None:
                        broken.append(text)
                        continue
                    records.append(record)
        except OSError as exc:
            log.error("no se pudo leer el buzon %s: %s", path, exc)
            return [], [repr(exc)]
        return records, broken

    def _quarantine(self, path: Path, broken: list[str]) -> None:
        """Aparta solo las lineas rotas y borra el fichero del buzon.

        El ``.bad`` conserva el texto exacto para poder mirarlo a mano. Si ya
        existe uno del mismo dia, se anade detras en vez de pisarlo.
        """
        target = path.with_name(path.name + QUARANTINE_SUFFIX)
        try:
            with target.open("a", encoding="utf-8", newline="\n") as handle:
                for text in broken:
                    handle.write(text + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            path.unlink()
        except OSError as exc:  # pragma: no cover - permisos
            log.error("no se pudo apartar %s: %s", path.name, exc)


__all__ = ["EventSpool", "SpoolRecord"]
