# Build jobwatch.exe on Windows.  Run from the project folder in PowerShell:
#   powershell -ExecutionPolicy Bypass -File packaging\build_exe.ps1
# Result: dist\jobwatch-windows.zip  (unzip anywhere and double-click jobwatch.exe)

$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path "$PSScriptRoot\..")

python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller

$common = @(
  "--onefile", "--noconfirm", "--clean",
  "--paths", ".",
  "--collect-all", "playwright",
  "--hidden-import", "plyer.platforms.win.notification",
  "--hidden-import", "lxml.etree",
  "packaging\jobwatch_entry.py"
)
python -m PyInstaller --name jobwatch --console @common
python -m PyInstaller --name jobwatch-background --noconsole @common

$out = "dist\jobwatch-windows"
Remove-Item $out -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $out | Out-Null
Copy-Item dist\jobwatch.exe, dist\jobwatch-background.exe, companies.csv, config.yaml, `
          packaging\email.env.example, packaging\HOW-TO-USE.txt $out
Compress-Archive -Path "$out\*" -DestinationPath dist\jobwatch-windows.zip -Force
Write-Host "Built dist\jobwatch-windows.zip"
