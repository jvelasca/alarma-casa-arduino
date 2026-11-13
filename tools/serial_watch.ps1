<#
.SYNOPSIS
    Lee el puerto serie del Arduino y muestra cada linea con marca de tiempo.

.DESCRIPTION
    Usa System.IO.Ports.SerialPort nativo de .NET: no requiere pyserial.
    Lee durante -Seconds segundos (0 = hasta Ctrl+C) y opcionalmente guarda
    la captura en -LogFile, que por defecto es data\serial-<fecha>.log.

    Al abrir el puerto, el Mega se reinicia por DTR y empieza el
    calentamiento de 60 s del sketch. Eso es normal.

.PARAMETER Port
    Puerto serie. Si se omite, se detecta automaticamente.

.PARAMETER Baud
    Velocidad. Por defecto 115200.

.PARAMETER Seconds
    Duracion de la captura. 0 o negativo = hasta Ctrl+C. Por defecto 0.

.PARAMETER LogFile
    Ruta del log. Si se omite, data\serial-<yyyyMMdd-HHmmss>.log

.EXAMPLE
    .\serial_watch.ps1
    .\serial_watch.ps1 -Seconds 90
    .\serial_watch.ps1 -Port COM3 -Seconds 120 -LogFile data\prueba.log
#>
[CmdletBinding()]
param(
    [string]$Port,
    [int]$Baud = 115200,
    [int]$Seconds = 0,
    [string]$LogFile
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$KnownVids = @('VID_1A86', 'VID_2341', 'VID_2A03', 'VID_0403')

function Find-ArduinoPort {
    $devices = Get-PnpDevice -Class Ports -ErrorAction SilentlyContinue |
        Where-Object { $_.Status -eq 'OK' -and $_.InstanceId -match '^USB\\' }
    foreach ($d in $devices) {
        foreach ($vid in $KnownVids) {
            if ($d.InstanceId -match $vid -and $d.FriendlyName -match '\((COM\d+)\)') {
                return $Matches[1]
            }
        }
    }
    return $null
}

if (-not $Port) {
    $Port = Find-ArduinoPort
    if (-not $Port) { throw 'No se detecto ninguna placa Arduino. Usa -Port COMx.' }
}

if (-not $LogFile) {
    $logDir = Join-Path $RepoRoot 'data'
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
    $LogFile = Join-Path $logDir ("serial-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
}
elseif (-not [System.IO.Path]::IsPathRooted($LogFile)) {
    $LogFile = Join-Path $RepoRoot $LogFile
    $logDir = Split-Path -Parent $LogFile
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
}

Write-Host "puerto : $Port  $Baud 8N1" -ForegroundColor DarkGray
Write-Host "log    : $LogFile" -ForegroundColor DarkGray
if ($Seconds -gt 0) {
    Write-Host "duracion: $Seconds s" -ForegroundColor DarkGray
}
else {
    Write-Host 'duracion: hasta Ctrl+C' -ForegroundColor DarkGray
}
Write-Host ''

$sp = New-Object System.IO.Ports.SerialPort $Port, $Baud, 'None', 8, 'One'
$sp.DtrEnable = $true
$sp.ReadTimeout = 500

$t0 = Get-Date
$deadline = if ($Seconds -gt 0) { $t0.AddSeconds($Seconds) } else { $null }
$lines = 0

try {
    $sp.Open()
    $writer = New-Object System.IO.StreamWriter($LogFile, $false, [System.Text.UTF8Encoding]::new($false))
    $writer.AutoFlush = $true
    try {
        while ($true) {
            if ($deadline -and (Get-Date) -gt $deadline) { break }

            try {
                $line = $sp.ReadLine()
            }
            catch [System.TimeoutException] {
                continue
            }

            $line = $line.TrimEnd("`r", "`n")
            if ($line.Length -eq 0) { continue }

            $elapsed = [int]((Get-Date) - $t0).TotalMilliseconds
            $stamped = '{0,8} ms | {1}' -f $elapsed, $line

            switch -Regex ($line) {
                '^MOTION' { Write-Host $stamped -ForegroundColor Green }
                '^READY|^PIR READY' { Write-Host $stamped -ForegroundColor Cyan }
                '^WARMUP' { Write-Host $stamped -ForegroundColor DarkYellow }
                '^BOUNCE' { Write-Host $stamped -ForegroundColor Red }
                default { Write-Host $stamped -ForegroundColor Gray }
            }

            $writer.WriteLine($stamped)
            ++$lines
        }
    }
    finally {
        $writer.Dispose()
    }
}
finally {
    if ($sp.IsOpen) { $sp.Close() }
    $sp.Dispose()
}

Write-Host ''
Write-Host "Captura finalizada: $lines lineas en $LogFile" -ForegroundColor Cyan

if ($lines -gt 0) {
    $motionStart = @(Select-String -Path $LogFile -Pattern 'MOTION start' -AllMatches).Count
    $motionEnd = @(Select-String -Path $LogFile -Pattern 'MOTION end' -AllMatches).Count
    $heartbeats = @(Select-String -Path $LogFile -Pattern '\| HB ' -AllMatches).Count
    $bounces = @(Select-String -Path $LogFile -Pattern 'BOUNCE' -AllMatches).Count
    Write-Host ("  MOTION start : {0}" -f $motionStart) -ForegroundColor Green
    Write-Host ("  MOTION end   : {0}" -f $motionEnd) -ForegroundColor Green
    Write-Host ("  latidos HB   : {0}" -f $heartbeats) -ForegroundColor Gray
    Write-Host ("  avisos BOUNCE: {0}" -f $bounces) -ForegroundColor Gray
}
