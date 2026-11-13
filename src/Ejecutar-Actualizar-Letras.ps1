param(
    [Parameter(Position = 0)]
    [string]$Root = '',

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

# -Root es opcional: sin él se abre el selector de carpetas de Windows.

if (-not (Test-Path -LiteralPath $ScriptPath -PathType Leaf)) {
    throw "No se encontró el script Python: $ScriptPath"
}
# Si no se indicó -Root, permite elegir la carpeta de origen con un selector
# nativo de Windows. La selección incluye todas las subcarpetas.
if ([string]::IsNullOrWhiteSpace($Root)) {
    $FolderDialog = $null
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        $FolderDialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $FolderDialog.Description = 'Selecciona la carpeta raíz que contiene los archivos M4A y sus TTML asociados.'
        $FolderDialog.ShowNewFolderButton = $false

        # Ubicación inicial sugerida; el usuario puede navegar a cualquier otra carpeta.
        $InitialCandidates = @(
            'D:\Música\Apple Music',
            [Environment]::GetFolderPath([Environment+SpecialFolder]::MyMusic),
            [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
        )
        foreach ($Candidate in $InitialCandidates) {
            if (-not [string]::IsNullOrWhiteSpace($Candidate) -and
                (Test-Path -LiteralPath $Candidate -PathType Container)) {
                $FolderDialog.SelectedPath = (Resolve-Path -LiteralPath $Candidate).Path
                break
            }
        }

        Write-Host 'Selecciona la carpeta raíz de la biblioteca M4A...' -ForegroundColor Cyan
        $DialogResult = $FolderDialog.ShowDialog()
        if ($DialogResult -ne [System.Windows.Forms.DialogResult]::OK -or
            [string]::IsNullOrWhiteSpace($FolderDialog.SelectedPath)) {
            Write-Host 'Selección cancelada. No se procesó ningún archivo.' -ForegroundColor Yellow
            return
        }
        $Root = $FolderDialog.SelectedPath
    }
    catch {
        Write-Warning "No se pudo abrir el selector de carpetas: $($_.Exception.Message)"
        $Root = Read-Host 'Introduce la ruta completa de la carpeta raíz que contiene los M4A'
        if ([string]::IsNullOrWhiteSpace($Root)) {
            Write-Host 'No se indicó una carpeta. No se procesó ningún archivo.' -ForegroundColor Yellow
            return
        }
    }
    finally {
        if ($null -ne $FolderDialog) {
            $FolderDialog.Dispose()
        }
    }
}

if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
    throw "La carpeta de origen no existe o no es accesible: $Root"
}
$Root = (Resolve-Path -LiteralPath $Root).Path
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
