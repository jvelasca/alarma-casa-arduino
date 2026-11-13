<#
.SYNOPSIS
    Prepara el entorno Python del proyecto.

.DESCRIPTION
    Crea .venv si no existe, instala las dependencias (runtime y desarrollo)
    y deja el entorno listo para ejecutar tools/alarmctl.py y pytest.

    No toca el firmware: para eso esta scripts/flash.ps1.

.EXAMPLE
    .\setup.ps1
    .\setup.ps1 -Dev $false     (solo runtime)
#>
[CmdletBinding()]
param(
    [bool]$Dev = $true,
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $RepoRoot '.venv'
$VenvPy = Join-Path $VenvDir 'Scripts\python.exe'

if (-not (Test-Path $VenvPy)) {
    Write-Host "==> Creando entorno virtual en $VenvDir" -ForegroundColor Cyan
    & $Python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo crear el entorno virtual.' }
}
else {
    Write-Host "==> Reutilizando $VenvDir" -ForegroundColor DarkGray
}

Write-Host '==> Actualizando pip' -ForegroundColor Cyan
& $VenvPy -m pip install --quiet --upgrade pip

Write-Host '==> Instalando paquetes' -ForegroundColor Cyan
& $VenvPy -m pip install --quiet pyserial "PyQt6>=6.7,<6.8"
if ($Dev) {
    & $VenvPy -m pip install --quiet pytest ruff mypy
}

# Instalacion editable: hace que `python -m alarm` funcione sin tocar PYTHONPATH,
# que es lo que usa la configuracion de F5 de Cursor.
Write-Host '==> Instalando el paquete en modo editable' -ForegroundColor Cyan
Push-Location $RepoRoot
try {
    & $VenvPy -m pip install --quiet -e .
}
finally {
    Pop-Location
}

Write-Host '==> Comprobando' -ForegroundColor Cyan
& $VenvPy -c "import serial, sys; from PyQt6.QtCore import QT_VERSION_STR; print('python', sys.version.split()[0]); print('pyserial', serial.__version__); print('Qt', QT_VERSION_STR)"

Write-Host ''
Write-Host 'Entorno listo. Pulsa F5 en Cursor, o desde aqui:' -ForegroundColor Green
Write-Host '  .\scripts\run.ps1' -ForegroundColor Green
Write-Host '  .\scripts\run.ps1 -Sim        (sin hardware)' -ForegroundColor Green
Write-Host ''
Write-Host 'Pruebas:' -ForegroundColor Green
Write-Host '  .\.venv\Scripts\python.exe -m pytest' -ForegroundColor Green
