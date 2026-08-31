param()

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Checkout = Join-Path $ProjectDir ".deps\unluac-rs"
$Output = Join-Path $ProjectDir "vendor\unluac.exe"
$Tag = "v1.4.3"

$Git = Get-Command git -ErrorAction SilentlyContinue
if (-not $Git) {
    throw "Git is required to fetch unluac-rs $Tag."
}
$Cargo = Get-Command cargo -ErrorAction SilentlyContinue
if (-not $Cargo) {
    throw "Rust/Cargo 1.94 or newer is required to build unluac-rs $Tag."
}

New-Item -ItemType Directory -Path (Split-Path -Parent $Checkout) -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $Output) -Force | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $Checkout ".git"))) {
    & $Git.Source clone --depth 1 --branch $Tag https://github.com/x3zvawq/unluac-rs.git $Checkout
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to clone unluac-rs $Tag."
    }
}
else {
    & $Git.Source -C $Checkout fetch origin "refs/tags/$Tag`:refs/tags/$Tag" --depth 1
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to fetch unluac-rs $Tag."
    }
    & $Git.Source -C $Checkout checkout --detach $Tag
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to check out unluac-rs $Tag."
    }
}

& $Cargo.Source build --manifest-path (Join-Path $Checkout "Cargo.toml") --release --package unluac-cli
if ($LASTEXITCODE -ne 0) {
    throw "unluac-rs build failed."
}

$BuiltBinary = Join-Path $Checkout "target\release\unluac-cli.exe"
if (-not (Test-Path -LiteralPath $BuiltBinary)) {
    throw "unluac-cli.exe was not produced by Cargo."
}
Copy-Item -LiteralPath $BuiltBinary -Destination $Output -Force
Write-Host "Done: $Output"
