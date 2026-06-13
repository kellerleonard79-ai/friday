# Build FridaySetup.exe on a Windows machine.
#
# Prerequisites:
#   - Python 3.12 x64 on PATH
#   - Inno Setup 6 (iscc on PATH, or default install location)
#   - Optional: packaging\windows\google_client_secret.json to bundle the
#     Google OAuth client (see BUILD_WINDOWS.md)
#
# Usage (from anywhere):
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $RepoRoot

# 1. Fresh build venv
if (-not (Test-Path ".build-venv")) {
    python -m venv .build-venv
}
& ".build-venv\Scripts\Activate.ps1"
python -m pip install --upgrade pip
pip install -r friday\requirements-win.txt pyinstaller

# 2. Icon
python packaging\windows\make_icon.py

# 3. Freeze
pyinstaller packaging\windows\friday.spec --noconfirm --clean

# 4. Installer
$iscc = Get-Command iscc -ErrorAction SilentlyContinue
if (-not $iscc) {
    $candidate = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    if (Test-Path $candidate) { $iscc = $candidate }
    else { throw "Inno Setup not found. Install from https://jrsoftware.org/isdl.php" }
}
& $iscc packaging\windows\installer.iss

Write-Host ""
Write-Host "Done: packaging\windows\Output\FridaySetup.exe" -ForegroundColor Green
