"""Historial persistente de eventos en SQLite.

El registro de la ventana es una vista: se recorta a las ultimas filas para que
el arbol no se arrastre. Todo lo que ocurre se guarda aqui, en
``data/alarm.db``, con dos limites que se aplican a la vez:

* ``max_age_days``: lo mas viejo que un ano se borra.
* ``max_events``: por encima del tope se borra lo mas antiguo.

Se usa ``sqlite3`` de la biblioteca estandar: sin dependencias nuevas y con
suficiente rendimiento para miles de filas al dia. La tabla se llama
``log_entries`` y no ``events`` para no chocar con la tabla de dominio prevista
en F2.
"""

from __future__ import annotations

import csv
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS log_entries (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    tag    TEXT NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_entries_ts ON log_entries(ts);
-- Estado operativo que debe sobrevivir al reinicio (armada/desarmada y su
-- marca de tiempo). Es una tabla clave-valor para no cambiar el esquema cada
-- vez que haga falta recordar un dato suelto.
CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
-- Eventos del dispositivo ya procesados, con la clave que los hace unicos:
-- el arranque de la placa (`boot_id`) y su numero de secuencia. Es lo que
-- impide que un reenvio tras reconectar vuelva a alimentar la maquina de
-- estados o a lanzar un segundo aviso. `boot_id` 0 se reserva para firmware
-- antiguo, que no puede deduplicarse con garantias entre sesiones.
CREATE TABLE IF NOT EXISTS processed_events (
    boot_id INTEGER NOT NULL,
    seq     INTEGER NOT NULL,
    ts      REAL NOT NULL,
    PRIMARY KEY (boot_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_processed_events_ts ON processed_events(ts);
"""

# Cabeceras del CSV. Se mantienen en espanol porque el fichero es para leerlo,
# no para que lo consuma otro programa.
CSV_HEADER = ("fecha_local", "fecha_utc", "severidad", "evento", "detalle")

# Cuanto se guarda la prueba de "este evento ya se proceso". Muy por encima de
# cualquier reconexion real: una placa que vuelve a enviar un evento de hace un
# mes ya no esta reenviando nada, esta contando otra cosa.
_PROCESSED_RETENTION_S = 30 * 86_400


@dataclass(frozen=True, slots=True)
class EventRow:
    """Una linea del historial."""

    ts: float
    tag: str
    kind: str
    detail: str

    def local_time(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts))

    def utc_time(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(self.ts))


def _filters(
    *,
    since: float | None = None,
    until: float | None = None,
    tag: str | None = None,
    kind: str | None = None,
    text: str | None = None,
) -> tuple[str, list[object]]:
    """Clausula `WHERE` y parametros para consultar el historial.

    `kind` y `text` buscan por subcadena y sin distinguir mayusculas con
    `instr(lower(...))` en vez de `LIKE`: asi un `%` o un `_` escritos por el
    usuario se buscan tal cual y no como comodines.
    """
    where: list[str] = []
    params: list[object] = []
    if since is not None:
        where.append("ts >= ?")
        params.append(float(since))
    if until is not None:
        where.append("ts <= ?")
        params.append(float(until))
    if tag:
        where.append("tag = ?")
        params.append(tag)
    if kind:
        where.append("instr(lower(kind), lower(?)) > 0")
        params.append(kind)
    if text:
        where.append("instr(lower(detail), lower(?)) > 0")
        params.append(text)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    return clause, params


def open_readonly(path: str | Path) -> sqlite3.Connection:
    """Abre el historial sin poder escribir en el.

    La consola consulta el mismo fichero que la ventana, y a menudo con la
    ventana abierta. Abrirlo en escritura crearia una base vacia si la ruta esta
    mal, que es justo el fallo que hay que ver y no tapar; y modificaria los
    PRAGMA de un fichero que solo se quiere leer. El modo WAL permite leer
    mientras la aplicacion escribe.

    `as_uri()` escapa los espacios de la ruta («Proyectos Cursor»), que en un
    URI de SQLite sin escapar romperian la conexion.
    """
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(target)
    return sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True)


def query_rows(
    path: str | Path,
    *,
    limit: int | None = None,
    since: float | None = None,
    until: float | None = None,
    tag: str | None = None,
    kind: str | None = None,
    text: str | None = None,
) -> list[EventRow]:
    """Lee filas del historial, de la mas antigua a la mas nueva.

    `limit` se queda con las mas recientes que cumplen los filtros sin alterar
    el orden: es lo que uno espera de «las ultimas 30».
    """
    clause, params = _filters(since=since, until=until, tag=tag, kind=kind, text=text)
    if limit is not None and limit > 0:
        sql = (
            "SELECT ts, tag, kind, detail FROM ("
            f"  SELECT id, ts, tag, kind, detail FROM log_entries{clause}"
            "  ORDER BY id DESC LIMIT ?"
            ") ORDER BY id"
        )
        params = [*params, int(limit)]
    else:
        sql = f"SELECT ts, tag, kind, detail FROM log_entries{clause} ORDER BY id"

    con = open_readonly(path)
    try:
        return [
            EventRow(float(ts), tag_, kind_, detail)
            for ts, tag_, kind_, detail in con.execute(sql, params)
        ]
    finally:
        con.close()


def count_rows(
    path: str | Path,
    *,
    since: float | None = None,
    until: float | None = None,
    tag: str | None = None,
    kind: str | None = None,
    text: str | None = None,
) -> int:
    """Cuantas filas cumplen los filtros, sin traerlas."""
    clause, params = _filters(since=since, until=until, tag=tag, kind=kind, text=text)
    con = open_readonly(path)
    try:
        row = con.execute(f"SELECT COUNT(*) FROM log_entries{clause}", params).fetchone()
        return int(row[0]) if row else 0
    finally:
        con.close()


def tag_counts(path: str | Path) -> list[tuple[str, int]]:
    """Etiquetas guardadas y cuantas filas tiene cada una, de mas a menos."""
    con = open_readonly(path)
    try:
        rows = con.execute(
            "SELECT tag, COUNT(*) FROM log_entries GROUP BY tag ORDER BY COUNT(*) DESC, tag"
        ).fetchall()
        return [(str(tag), int(total)) for tag, total in rows]
    finally:
        con.close()


def write_csv(path: str | Path, rows: list[EventRow]) -> int:
    """Vuelca filas a CSV y devuelve cuantas escribio.

    `utf-8-sig` es lo que hace que Excel abra los acentos bien al hacer doble
    clic. Vive fuera de `EventStore` para que la consola exporte con el mismo
    formato que la ventana sin duplicar el escritor.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        for row in rows:
            writer.writerow([row.local_time(), row.utc_time(), row.tag, row.kind, row.detail])
    return len(rows)


class EventStore:
    """Registro de eventos con retencion por edad y por numero.

    Un unico hilo escribe (el de la interfaz), pero ``check_same_thread=False``
    y el cerrojo permiten que una prueba o un futuro consumidor lo haga desde
    otro sin corromper la conexion.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_events: int = 100_000,
        max_age_days: int = 365,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = str(path)
        self._max_events = max(1, int(max_events))
        self._max_age_s = max(1, int(max_age_days)) * 86_400
        self._clock = clock
        self._lock = threading.Lock()
        self._closed = False
        self._inserts = 0

        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        # ``isolation_level=None`` desactiva el BEGIN implicito de sqlite3: las
        # transacciones se abren y cierran aqui (`_transaction`), para que un
        # fallo a mitad de una escritura pueda deshacerse explicitamente en vez
        # de quedar pendiente hasta el siguiente commit ajeno.
        self._con = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        with self._lock:
            self._con.executescript(_SCHEMA)
            # WAL deja leer mientras se escribe y aguanta mejor un corte de luz.
            with suppress(sqlite3.Error):
                self._con.execute("PRAGMA journal_mode=WAL")
            # FULL, no NORMAL: el ACK que se manda a la placa promete que el
            # commit sobrevivio al corte de alimentacion. Con NORMAL en WAL,
            # SQLite conserva la integridad pero puede perder la ultima
            # transaccion confirmada, que es justo la que se acaba de acusar.
            # Cuesta un fsync por evento; a la frecuencia de un PIR es
            # irrelevante y es el precio de que el ACK signifique algo.
            with suppress(sqlite3.Error):
                self._con.execute("PRAGMA synchronous=FULL")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Transaccion explicita: o se confirma entera o no queda nada.

        ``BEGIN IMMEDIATE`` toma el cerrojo de escritura al empezar, de modo
        que un historial bloqueado por otro programa falla antes de escribir la
        primera fila y no a mitad. El llamador ya tiene ``self._lock``.
        """
        self._con.execute("BEGIN IMMEDIATE")
        try:
            yield self._con
        except BaseException:
            with suppress(sqlite3.Error):
                self._con.execute("ROLLBACK")
            raise
        try:
            self._con.execute("COMMIT")
        except BaseException:
            # Un COMMIT que falla (disco lleno, E/S) deja la transaccion
            # abierta: sin deshacerla, el siguiente BEGIN IMMEDIATE fallaria
            # y el historial quedaria inservible hasta reiniciar. El evento se
            # va al buzon, como si la escritura no hubiera empezado.
            with suppress(sqlite3.Error):
                self._con.execute("ROLLBACK")
            raise

    # -- Escritura ---------------------------------------------------------

    def append(self, tag: str, kind: str, detail: str, *, ts: float | None = None) -> EventRow:
        """Anade una fila. Devuelve lo escrito, ya con su marca de tiempo."""
        row = EventRow(
            ts=self._clock() if ts is None else float(ts),
            tag=tag,
            kind=kind,
            detail=detail,
        )
        with self._lock, self._transaction():
            self._con.execute(
                "INSERT INTO log_entries (ts, tag, kind, detail) VALUES (?, ?, ?, ?)",
                (row.ts, row.tag, row.kind, row.detail),
            )
            self._inserts += 1
        # Comprobar el tope en cada insercion costaria una consulta por evento;
        # cada 500 es imperceptible y acota el exceso.
        if self._inserts % 500 == 0:
            with suppress(sqlite3.Error):
                self.purge()
        return row

    def record_motion(
        self,
        boot_id: int | None,
        seq: int,
        tag: str,
        kind: str,
        detail: str,
        *,
        ts: float | None = None,
        state: Mapping[str, str] | None = None,
        remove_state: Sequence[str] = (),
    ) -> tuple[EventRow | None, bool]:
        """Registra un evento del dispositivo de forma idempotente.

        Devuelve ``(fila, nuevo)``. En una sola transaccion se apuntan tres
        cosas: la clave ``(boot_id, seq)``, la fila del historial y, si se pasa,
        el estado de la maquina de alarma resultante. O quedan las tres o no
        queda ninguna, asi que no existe el estado intermedio en el que el
        evento se ha procesado pero no se puede reconocer al repetirse, ni el
        de un disparo registrado cuyo estado guardado aun dice "armada".

        Si la clave ya estaba, ``fila`` es ``None`` y ``nuevo`` es ``False``: el
        llamador debe volver a confirmar el evento pero no repetir sus efectos
        (sirena, avisos, transicion de estado).

        Con ``boot_id`` desconocido (firmware 1.6.0 o anterior) no se apunta la
        clave: la sesion no se puede distinguir de otra y usarla como si lo
        fuera descartaria eventos legitimos.
        """
        row = EventRow(
            ts=self._clock() if ts is None else float(ts),
            tag=tag,
            kind=kind,
            detail=detail,
        )
        with self._lock, self._transaction():
            new = True
            if boot_id is not None:
                cursor = self._con.execute(
                    "INSERT OR IGNORE INTO processed_events (boot_id, seq, ts) VALUES (?, ?, ?)",
                    (int(boot_id), int(seq), row.ts),
                )
                new = cursor.rowcount > 0
            if not new:
                return None, False
            self._con.execute(
                "INSERT INTO log_entries (ts, tag, kind, detail) VALUES (?, ?, ?, ?)",
                (row.ts, row.tag, row.kind, row.detail),
            )
            self._write_state(state, remove_state)
            self._inserts += 1
        if self._inserts % 500 == 0:
            with suppress(sqlite3.Error):
                self.purge()
        return row, True

    def _write_state(
        self,
        values: Mapping[str, str] | None,
        remove: Sequence[str] = (),
    ) -> None:
        """Aplica cambios de ``system_state`` sin hacer commit.

        Vive aparte para que ``record_motion`` y ``set_state_many`` compartan
        la misma escritura y pueda formar parte de su transaccion.
        """
        if values:
            moment = self._clock()
            self._con.executemany(
                "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                [(str(key), str(value), moment) for key, value in values.items()],
            )
        if remove:
            self._con.executemany(
                "DELETE FROM system_state WHERE key = ?",
                [(str(key),) for key in remove],
            )

    def has_processed(self, boot_id: int | None, seq: int) -> bool:
        """Si ``(boot_id, seq)`` ya se proceso alguna vez."""
        if boot_id is None:
            return False
        with self._lock:
            row = self._con.execute(
                "SELECT 1 FROM processed_events WHERE boot_id = ? AND seq = ?",
                (int(boot_id), int(seq)),
            ).fetchone()
        return row is not None

    # -- Estado del sistema ------------------------------------------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        """Valor guardado para ``key``, o ``default`` si no existe."""
        with self._lock:
            row = self._con.execute(
                "SELECT value FROM system_state WHERE key = ?", (key,)
            ).fetchone()
        return default if row is None else str(row[0])

    def set_state(self, key: str, value: str) -> None:
        """Guarda un valor que debe sobrevivir al reinicio (upsert)."""
        with self._lock, self._transaction():
            self._con.execute(
                "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, value, self._clock()),
            )

    def clear_state(self, key: str) -> None:
        with self._lock, self._transaction():
            self._con.execute("DELETE FROM system_state WHERE key = ?", (key,))

    def set_state_many(
        self,
        values: Mapping[str, str],
        *,
        remove: Sequence[str] = (),
    ) -> None:
        """Guarda y borra varias claves en un solo commit.

        El estado de la alarma son varios datos que deben moverse juntos: si se
        escribiera ``armed_intent`` y fallara el ``armed_at``, la proxima
        apertura leeria una alarma armada sin fecha. Todo o nada.
        """
        if not values and not remove:
            return
        with self._lock, self._transaction():
            self._write_state(values, remove)

    @property
    def persistent(self) -> bool:
        """``False`` si el historial vive en memoria y no sobrevive al cierre."""
        return self._path != ":memory:"

    # -- Lectura -----------------------------------------------------------

    def recent(self, limit: int) -> list[EventRow]:
        """Las ``limit`` filas mas recientes, de la mas antigua a la mas nueva."""
        if limit <= 0:
            return []
        with self._lock:
            cursor = self._con.execute(
                "SELECT ts, tag, kind, detail FROM ("
                "  SELECT id, ts, tag, kind, detail FROM log_entries"
                "  ORDER BY id DESC LIMIT ?"
                ") ORDER BY id",
                (int(limit),),
            )
            return [EventRow(float(ts), tag, kind, detail) for ts, tag, kind, detail in cursor]

    def count(self) -> int:
        with self._lock:
            cursor = self._con.execute("SELECT COUNT(*) FROM log_entries")
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    # -- Mantenimiento -----------------------------------------------------

    def clear(self) -> None:
        """Vacia el historial. Lo pide «Limpiar registro» y es irreversible."""
        with self._lock, self._transaction():
            self._con.execute("DELETE FROM log_entries")

    def purge(self) -> int:
        """Aplica los dos limites y compacta si ha borrado mucho."""
        oldest = self._clock() - self._max_age_s
        # Las claves de eventos procesados se guardan mucho mas tiempo que la
        # retencion visible: son la prueba de que un reenvio ya se atendio y
        # perderlas antes de tiempo volveria a duplicar una intrusion antigua.
        processed_oldest = self._clock() - _PROCESSED_RETENTION_S
        with self._lock, self._transaction():
            removed = self._con.execute("DELETE FROM log_entries WHERE ts < ?", (oldest,)).rowcount
            # OFFSET max_events deja fuera lo que sobra: se borra todo lo anterior.
            excess = self._con.execute(
                "DELETE FROM log_entries WHERE id <= ("
                "  SELECT id FROM log_entries ORDER BY id DESC LIMIT 1 OFFSET ?"
                ")",
                (self._max_events,),
            ).rowcount
            self._con.execute("DELETE FROM processed_events WHERE ts < ?", (processed_oldest,))
            total = max(0, removed) + max(0, excess)
        if total > 1_000:
            # VACUUM no se puede ejecutar dentro de una transaccion.
            with suppress(sqlite3.Error):
                self._con.execute("VACUUM")
        if total:
            log.info("historial purgado: %d filas fuera de retencion", total)
        return total

    # -- Exportacion -------------------------------------------------------

    def export_csv(self, path: str | Path) -> int:
        """Vuelca todo el historial a CSV y devuelve el numero de filas.

        Se lee con el cerrojo tomado y se escribe fuera de el: generar el
        fichero puede tardar y no debe bloquear al hilo que apunta eventos.
        """
        with self._lock:
            cursor = self._con.execute("SELECT ts, tag, kind, detail FROM log_entries ORDER BY id")
            rows = [EventRow(float(ts), tag, kind, detail) for ts, tag, kind, detail in cursor]
        return write_csv(path, rows)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(sqlite3.Error), self._lock:
            self._con.commit()
            self._con.close()

    def __enter__(self) -> EventStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
