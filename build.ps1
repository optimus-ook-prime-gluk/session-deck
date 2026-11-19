# Builds Session Deck locally (optional - GitHub Actions builds the release for you).
# Needs: Python 3.9+, `pip install -r requirements-dev.txt`, and Inno Setup 6 for the installer.
param([string]$Version = "1.0.0")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { throw "tests failed" }

python -m PyInstaller --noconfirm --clean --windowed --name SessionDeck `
  --icon assets\logo.ico `
  --add-data "src\ui.html;." --add-data "assets;assets" `
  --paths src --hidden-import demo_data `
  src\session_deck.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Compress-Archive -Path dist\SessionDeck\* -DestinationPath "dist\SessionDeck-$Version-portable.zip" -Force

$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (Test-Path $iscc) {
  & $iscc "/DAppVersion=$Version" installer\SessionDeck.iss
} else {
  Write-Host "Inno Setup not found - skipped the installer. The app is in dist\SessionDeck\SessionDeck.exe"
}
