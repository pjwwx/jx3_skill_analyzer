param()

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VsWhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path -LiteralPath $VsWhere)) {
    throw "Visual Studio Build Tools not found (vswhere.exe is missing)."
}

$VsRoot = & $VsWhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $VsRoot) {
    throw "Visual Studio C++ x64 build tools are not installed."
}
$VcVars = Join-Path $VsRoot "VC\Auxiliary\Build\vcvars64.bat"
$Source = Join-Path $ProjectDir "native\jx3_pak_extract.cpp"
$Output = Join-Path $ProjectDir "vendor\JX3PakBridge.exe"
# Do not enable compiler optimization or switch away from the static Debug CRT.
# The current Engine_Lua5X64.dll intermittently faults inside PakV4 initialization
# with optimized/release callers; this combination matches the stable upstream
# extractor behavior while keeping the player-facing binary self-contained.
$BuildCommand = 'call "' + $VcVars + '" -vcvars_ver=14.29 && cl.exe /nologo /std:c++17 /Od /RTC1 /EHsc /MTd /D_DEBUG /W4 /DUNICODE /D_UNICODE /Fe:"vendor\JX3PakBridge.exe" "native\jx3_pak_extract.cpp" Psapi.lib'

Push-Location $ProjectDir
try {
    cmd.exe /d /c $BuildCommand
    if ($LASTEXITCODE -ne 0) {
        throw "Native extraction helper build failed."
    }
}
finally {
    Pop-Location
}

Write-Host "Done: $Output"
