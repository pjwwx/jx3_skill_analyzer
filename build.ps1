param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$NativeHelper = Join-Path $ProjectDir "vendor\JX3PakBridge.exe"
$Decompiler = Join-Path $ProjectDir "vendor\unluac.exe"

if (-not (Test-Path -LiteralPath $NativeHelper)) {
    & (Join-Path $ProjectDir "build-native.ps1")
}
if (-not (Test-Path -LiteralPath $Decompiler)) {
    & (Join-Path $ProjectDir "build-unluac.ps1")
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    py -3.10 -m venv (Join-Path $ProjectDir ".venv")
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
        --runtime-tmpdir "." `
        --name "JX3SkillAnalyzer" `
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
