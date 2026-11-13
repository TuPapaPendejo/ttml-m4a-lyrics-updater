param(
    [Parameter(Position = 0)]
    [string]$Root = 'D:\Música\Apple Music',

    [switch]$Apply,

    [switch]$NoCredits,

    [string]$LogPath = '',

    [string]$Python = '',

    [ValidateRange(1, 2147483647)]
    [int]$BatchSize = 10,

    [ValidateRange(0, 86400)]
    [double]$BatchPauseSeconds = 1
)

$ErrorActionPreference = 'Stop'
$ScriptPath = Join-Path $PSScriptRoot 'Actualizar-Letras-TTML.py'

if (-not (Test-Path -LiteralPath $ScriptPath -PathType Leaf)) {
    throw "No se encontró el script Python: $ScriptPath"
}
if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
    throw "La carpeta de música no existe: $Root"
}
if ($BatchPauseSeconds -lt 0) {
    throw '-BatchPauseSeconds no puede ser negativo.'
}

if ([string]::IsNullOrWhiteSpace($Python)) {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $PythonCommand) {
        $PythonCommand = Get-Command py -ErrorAction SilentlyContinue
        if ($PythonCommand) {
            $Python = $PythonCommand.Source
            $UsePyLauncher = $true
        }
    }
    else {
        $Python = $PythonCommand.Source
        $UsePyLauncher = $false
    }
}
else {
    $UsePyLauncher = $false
}

if ([string]::IsNullOrWhiteSpace($Python)) {
    throw 'No se encontró Python. Instala Python 3.10 o posterior o pasa -Python con la ruta del ejecutable.'
}

if ($UsePyLauncher) {
    $VersionOutput = & $Python -3 --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw 'No se pudo iniciar Python mediante el lanzador py.'
    }
    $PythonArgs = @('-3')
}
else {
    $VersionOutput = & $Python --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "No se pudo iniciar Python: $Python"
    }
    $PythonArgs = @()
}

Write-Host "Python: $Python"
Write-Host $VersionOutput

# Comprueba Mutagen e instala una versión fijada sólo si no está disponible.
if ($UsePyLauncher) {
    & $Python @PythonArgs -c 'import mutagen' 2>$null
}
else {
    & $Python -c 'import mutagen' 2>$null
}
$MutagenCheck = $LASTEXITCODE

if ($MutagenCheck -ne 0) {
    Write-Host 'Instalando Mutagen 1.48.1 para el Python seleccionado...' -ForegroundColor Cyan
    if ($UsePyLauncher) {
        & $Python @PythonArgs -m pip install 'mutagen==1.48.1'
    }
    else {
        & $Python -m pip install 'mutagen==1.48.1'
    }
    if ($LASTEXITCODE -ne 0) {
        throw 'No se pudo instalar Mutagen. Revisa la salida de pip.'
    }
}

$BatchPauseInvariant = $BatchPauseSeconds.ToString([Globalization.CultureInfo]::InvariantCulture)
$Arguments = @($ScriptPath, '--root', $Root, '--batch-size', $BatchSize, '--batch-pause-seconds', $BatchPauseInvariant)
if ($Apply) {
    $Arguments += '--apply'
}
if ($NoCredits) {
    $Arguments += '--no-credits'
}
if (-not [string]::IsNullOrWhiteSpace($LogPath)) {
    $Arguments += @('--log-file', $LogPath)
}

if ($UsePyLauncher) {
    & $Python @PythonArgs @Arguments
}
else {
    & $Python @Arguments
}
$ExitCode = $LASTEXITCODE

if ($ExitCode -ne 0) {
    Write-Error "El script terminó con código $ExitCode. Revisa los errores anteriores."
    exit $ExitCode
}
