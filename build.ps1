param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$NativeHelper = Join-Path $ProjectDir "vendor\JX3PakBridge.exe"
$NativeSource = Join-Path $ProjectDir "native\jx3_pak_extract.cpp"
$Decompiler = Join-Path $ProjectDir "vendor\unluac.exe"

if (-not (Test-Path -LiteralPath $NativeHelper) -or
    (Get-Item -LiteralPath $NativeSource).LastWriteTimeUtc -gt (Get-Item -LiteralPath $NativeHelper).LastWriteTimeUtc) {
    & (Join-Path $ProjectDir "build-native.ps1")
}
$DecompilerIsCurrent = $false
if (Test-Path -LiteralPath $Decompiler) {
    try {
        $DetectedVersion = (& $Decompiler --version 2>$null | Out-String).Trim()
        $DecompilerIsCurrent = $LASTEXITCODE -eq 0 -and $DetectedVersion -match '\b1\.4\.4\b'
    }
    catch { $DecompilerIsCurrent = $false }
}
if (-not $DecompilerIsCurrent) {
    & (Join-Path $ProjectDir "build-unluac.ps1")
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    py -3.10 -m venv (Join-Path $ProjectDir ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Python 3.10 virtual environment creation failed." }
}

if (-not $SkipInstall) {
    & $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $ProjectDir "requirements-build.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Python build dependency installation failed."
    }
}

Push-Location $ProjectDir
try {
    & $VenvPython -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name "JX3SkillAnalyzer" `
        --exclude-module numpy `
        --exclude-module pandas `
        --exclude-module matplotlib `
        --exclude-module PIL `
        --exclude-module lxml `
        --add-binary "vendor\unluac.exe;vendor" `
        --add-binary "vendor\JX3PakBridge.exe;vendor" `
        "main.py"
    if ($LASTEXITCODE -ne 0) {
        throw "EXE packaging failed."
    }
}
finally {
    Pop-Location
}

Write-Host "Done: $ProjectDir\dist\JX3SkillAnalyzer.exe"
