@echo off
rem Re-checks the jobs jobwatch already notified you about, with the current filters.
rem Result: eligible-old-jobs.html in this folder. Nothing is sent, state.json is not changed.
cd /d "%~dp0"
python scripts\recheck_old_alerts.py
echo.
start "" "%~dp0eligible-old-jobs.html"
pause
