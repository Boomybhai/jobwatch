"""Create / remove the Windows scheduled task that runs jobwatch in the background."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .app_paths import app_dir, is_frozen

def task_name() -> str:
    """'jobwatch' for the main copy; a second copy in e.g. a 'jobwatch-belgium'
    folder gets its own task so the two don't replace each other."""
    import re
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", app_dir().name).strip("-").lower()
    if not name.startswith("jobwatch"):
        name = "jobwatch"
    return name


def _command() -> str:
    folder = app_dir()
    if is_frozen():
        # Prefer the windowless build so no console flashes up every few hours.
        bg = folder / "jobwatch-background.exe"
        exe = bg if bg.exists() else Path(sys.executable)
        return f'"{exe}" run --notify desktop,phone,email'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    py = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{py}" -m jobwatch run --notify desktop,phone,email --state "{folder / "state_local.json"}"'


def install(hours: int = 3) -> int:
    if sys.platform != "win32":
        print("Scheduled tasks can only be created on Windows.")
        return 1
    cmd = [
        "schtasks", "/Create", "/F",
        "/TN", task_name(),
        "/SC", "HOURLY", "/MO", str(hours),
        "/TR", _command(),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        # schtasks' defaults skip runs on battery and never catch up on runs missed
        # while the laptop was off or asleep - fix both.
        fix = ("$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
               "-StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew; "
               f"Set-ScheduledTask -TaskName '{task_name()}' -Settings $s | Out-Null")
        r2 = subprocess.run(["powershell", "-NoProfile", "-Command", fix], capture_output=True, text=True)
        if r2.returncode != 0:
            print("Note: couldn't allow runs on battery:\n" + (r2.stderr or r2.stdout))
        print(f"Done. jobwatch will now check for new jobs every {hours} hours while you're logged in.")
        print("To stop it later, run:  jobwatch.exe uninstall-task")
    else:
        print("Couldn't create the scheduled task:\n" + (result.stderr or result.stdout))
    return result.returncode


def uninstall() -> int:
    if sys.platform != "win32":
        print("Scheduled tasks only exist on Windows.")
        return 1
    result = subprocess.run(["schtasks", "/Delete", "/F", "/TN", task_name()], capture_output=True, text=True)
    print("Scheduled task removed." if result.returncode == 0 else (result.stderr or result.stdout))
    return result.returncode
