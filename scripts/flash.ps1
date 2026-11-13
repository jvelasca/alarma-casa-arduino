<#
.SYNOPSIS
    Compila y sube el sketch mega-pir al Arduino Mega 2560.

.DESCRIPTION
    Localiza arduino-cli (instalado junto al Arduino IDE), detecta el puerto
    del Arduino por VID/PID (CH340 o ATmega16U2) y compila + sube el sketch.

    No descarga nada: reutiliza el core arduino:avr ya instalado por el IDE.

.PARAMETER Port
    Puerto explicito, por ejemplo COM3. Si se omite, se detecta automaticamente.

.PARAMETER Board
    FQBN. Por defecto arduino:avr:mega.

.PARAMETER CompileOnly
    Compila sin subir.

.EXAMPLE
    .\flash.ps1
    .\flash.ps1 -Port COM3
    .\flash.ps1 -CompileOnly
#>
[CmdletBinding()]
param(
    [string]$Port,
    [string]$Board = 'arduino:avr:mega',
    [switch]$CompileOnly
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$SketchDir = Join-Path $RepoRoot 'firmware\mega-pir'

# VID/PID de los adaptadores USB-serie habituales en placas Arduino.
$KnownVids = @(
    'VID_1A86',   # CH340 / CH341 (clones)
    'VID_2341',   # Arduino oficial
    'VID_2A03',   # Arduino.org
    'VID_0403'    # FTDI
)

function Find-ArduinoCli {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe'),
        (Join-Path $env:ProgramFiles 'Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe')
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    $cmd = Get-Command arduino-cli -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw 'No se encontro arduino-cli. Instala el Arduino IDE 2.x o arduino-cli.'
}

function Find-ArduinoPort {
    $devices = Get-PnpDevice -Class Ports -ErrorAction SilentlyContinue |
        Where-Object { $_.Status -eq 'OK' -and $_.InstanceId -match '^USB\\' }

    foreach ($d in $devices) {
        foreach ($vid in $KnownVids) {
            if ($d.InstanceId -match $vid) {
                if ($d.FriendlyName -match '\((COM\d+)\)') {
                    return @{ Port = $Matches[1]; Device = $d.FriendlyName }
                }
            }
        }
    }
    return $null
}

# --- Resolucion de herramientas -------------------------------------------

$cli = Find-ArduinoCli
Write-Host "arduino-cli : $cli" -ForegroundColor DarkGray
Write-Host "sketch      : $SketchDir" -ForegroundColor DarkGray
Write-Host "board       : $Board" -ForegroundColor DarkGray

if (-not (Test-Path (Join-Path $SketchDir 'mega-pir.ino'))) {
    throw "No se encuentra el sketch en $SketchDir"
}

$found = $null
if (-not $CompileOnly) {
    if ($Port) {
        Write-Host "puerto      : $Port (indicado a mano)" -ForegroundColor DarkGray
    }
    else {
        $found = Find-ArduinoPort
        if (-not $found) {
            throw 'No se detecto ninguna placa Arduino en un puerto USB. Revisa el cable y el driver, o usa -Port COMx.'
        }
        $Port = $found.Port
        Write-Host "puerto      : $Port ($($found.Device))" -ForegroundColor DarkGray
    }
}

# --- Compilacion ----------------------------------------------------------

Write-Host "`n==> Compilando..." -ForegroundColor Cyan
& $cli compile --fqbn $Board $SketchDir
if ($LASTEXITCODE -ne 0) { throw "La compilacion fallo (codigo $LASTEXITCODE)." }

if ($CompileOnly) {
    Write-Host "`nCompilacion correcta. Nada que subir (-CompileOnly)." -ForegroundColor Green
    return
}

# --- Subida ---------------------------------------------------------------

Write-Host "`n==> Subiendo a $Port..." -ForegroundColor Cyan
& $cli upload --fqbn $Board --port $Port $SketchDir
if ($LASTEXITCODE -ne 0) {
    Write-Host "`nLa subida fallo. Causas habituales:" -ForegroundColor Yellow
    Write-Host '  - El monitor serie del Arduino IDE tiene el puerto abierto: cierralo.' -ForegroundColor Yellow
    Write-Host '  - Otro programa ocupa el puerto (tools/serial_watch.ps1).' -ForegroundColor Yellow
    Write-Host '  - Cable USB de solo carga o puerto defectuoso.' -ForegroundColor Yellow
    throw "La subida fallo (codigo $LASTEXITCODE)."
}

Write-Host "`nSubida completada en $Port." -ForegroundColor Green
Write-Host "Abre el monitor serie con:  .\tools\serial_watch.ps1 -Port $Port" -ForegroundColor Green
