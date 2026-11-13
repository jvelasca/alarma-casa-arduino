<#
.SYNOPSIS
    Arranca la aplicacion de escritorio.

.DESCRIPTION
    Equivale a pulsar F5 en Cursor, pero desde la terminal. Si no existe el
    entorno virtual lo crea llamando a setup.ps1.

.EXAMPLE
    .\run.ps1
    .\run.ps1 -Sim                (sin hardware)
    .\run.ps1 -Sim -Speed 6       (simulador acelerado)
    .\run.ps1 -Log DEBUG
#>
[CmdletBinding()]
param(
    [switch]$Sim,
    [string]$Port,
    [int]$Baud = 115200,
    [double]$Speed = 1.0,
    [string]$Log = 'INFO'
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPy = Join-Path $RepoRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $VenvPy)) {
    Write-Host '==> No hay entorno virtual; preparandolo' -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot 'setup.ps1')
}

$arguments = @('-m', 'alarm', '--log', $Log)
if ($Sim) { $arguments += @('--sim', '--sim-speed', $Speed) }
if ($Port) { $arguments += @('--port', $Port, '--baud', $Baud) }

Push-Location $RepoRoot
try {
    & $VenvPy @arguments
}
finally {
    Pop-Location
}
