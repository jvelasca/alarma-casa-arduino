<#
.SYNOPSIS
    Compila la aplicacion en un unico ejecutable (AlarmaCasa.exe).

.DESCRIPTION
    Genera `dist\AlarmaCasa.exe` junto a `dist\LEEME.txt`: un solo ejecutable
    que se copia a cualquier PC con Windows y arranca sin instalar Python. La
    configuracion y el historial se crean junto al .exe la primera vez que se
    abre. El LEEME explica la instalacion, los avisos y como configurar ntfy.

    Antes de empaquetar se ejecutan pruebas, ruff y mypy (salvo -SkipTests) y se
    genera el icono y el recurso de version a partir del codigo
    (`tools\make_assets.py`), para que no se queden atras.

    La primera vez tarda un par de minutos: PyInstaller analiza PyQt6 entero.
    El resultado pesa decenas de MB porque lleva Qt dentro.

.EXAMPLE
    .\build.ps1
    .\build.ps1 -SkipTests        (iterar rapido mientras se ajusta el .exe)
    .\build.ps1 -Clean            (borrar build\ y dist\ antes de compilar)
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$Clean,
    [string]$Python
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPy = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if ($Python) { $VenvPy = $Python }

# Los comandos nativos escriben en stderr cosas que no son errores (los INFO de
# PyInstaller, sin ir mas lejos) y con ErrorActionPreference = Stop eso aborta el
# script. Se ejecutan con Continue y se decide por el codigo de salida, que es
# lo que de verdad dice si han funcionado.
function Invoke-Step {
    param(
        [Parameter(Mandatory)] [string]$Title,
        [Parameter(Mandatory)] [scriptblock]$Action,
        [Parameter(Mandatory)] [string]$Failure
    )
    Write-Host "==> $Title" -ForegroundColor Cyan
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Action 2>&1 | ForEach-Object { Write-Host $_ }
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) { throw $Failure }
}

if (-not (Test-Path $VenvPy)) {
    Write-Host '==> No hay entorno virtual; preparandolo' -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot 'setup.ps1')
}

Push-Location $RepoRoot
try {
    if (-not $SkipTests) {
        Invoke-Step 'Pruebas (pytest)' { & $VenvPy -m pytest -q } `
            'Las pruebas fallan; no se empaqueta.'
        Invoke-Step 'Estilo (ruff)' { & $VenvPy -m ruff check src tools tests packaging } `
            'ruff ha encontrado problemas.'
        Invoke-Step 'Tipos (mypy)' { & $VenvPy -m mypy } 'mypy ha encontrado problemas.'
    }

    Invoke-Step 'Icono y version del ejecutable' { & $VenvPy tools\make_assets.py } `
        'No se pudieron generar los recursos del ejecutable.'

    if ($Clean) {
        Write-Host '==> Limpiando build\ y dist\' -ForegroundColor Yellow
        Remove-Item -Recurse -Force (Join-Path $RepoRoot 'build\pyinstaller') -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force (Join-Path $RepoRoot 'dist') -ErrorAction SilentlyContinue
    }

    Invoke-Step 'Empaquetando con PyInstaller (un solo fichero)' {
        & $VenvPy -m PyInstaller --noconfirm --clean --log-level WARN `
            --distpath (Join-Path $RepoRoot 'dist') `
            --workpath (Join-Path $RepoRoot 'build\pyinstaller') `
            (Join-Path $RepoRoot 'packaging\alarma.spec')
    } 'PyInstaller ha fallado.'

    $exe = Join-Path $RepoRoot 'dist\AlarmaCasa.exe'
    if (-not (Test-Path $exe)) { throw "No se genero $exe" }

    # Las instrucciones viajan con el ejecutable: quien recibe la carpeta no
    # tiene el repositorio y necesita saber que hacer con el .exe.
    $readme = Join-Path $RepoRoot 'packaging\LEEME.txt'
    if (-not (Test-Path $readme)) { throw "No existe $readme" }
    $distReadme = Join-Path $RepoRoot 'dist\LEEME.txt'
    Copy-Item -Path $readme -Destination $distReadme -Force

    $size = [math]::Round((Get-Item $exe).Length / 1MB, 1)

    Write-Host ''
    Write-Host '==> Listo: dist\' -ForegroundColor Green
    Write-Host "    $exe ($size MB)" -ForegroundColor Gray
    Write-Host "    $distReadme" -ForegroundColor Gray
    Write-Host '    Copia la carpeta al otro PC y abre AlarmaCasa.exe. La primera' -ForegroundColor Gray
    Write-Host '    vez crea config\ y data\ a su lado. Para recuperar los ajustes,' -ForegroundColor Gray
    Write-Host '    usa Archivo -> Restaurar copia completa.' -ForegroundColor Gray
}
finally {
    Pop-Location
}
