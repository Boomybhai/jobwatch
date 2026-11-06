@echo off
rem Rebuilds jobwatch.exe and jobwatch-background.exe from the code in this folder
rem and puts them here. config.yaml, companies.csv, state.json and email.env are kept.
cd /d "%~dp0"
echo.
echo === Step 1 of 2: building jobwatch (a few minutes) ===
echo.
powershell -ExecutionPolicy Bypass -File packaging\build_exe.ps1
if errorlevel 1 (
  echo.
  echo Build failed - copy the red error text above and send it to Claude.
  pause
  exit /b 1
)
copy /y "dist\jobwatch.exe" "%~dp0" >nul
copy /y "dist\jobwatch-background.exe" "%~dp0" >nul
if errorlevel 1 (
  echo.
  echo Couldn't replace jobwatch-background.exe - it is probably running a check right now.
  echo Wait until it finishes, then run this file again.
  pause
  exit /b 1
)
echo.
echo === Step 2 of 2: quick check on Tata Steel (nothing is saved or sent) ===
"%~dp0jobwatch.exe" run --only "tata steel ijmuiden" --dry-run --no-render
echo.
echo Done. The scheduled checks now use the new version.
pause
