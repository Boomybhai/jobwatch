# Registers a Windows scheduled task that runs jobwatch on your laptop every 3 hours
# and shows desktop pop-ups (plus email, if SMTP_* environment variables are set).
#
# Run from the jobwatch folder in PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
#
# Remove it again with:
#   Unregister-ScheduledTask -TaskName jobwatch -Confirm:$false   (or jobwatch-belgium)

$ErrorActionPreference = "Stop"
$here = (Resolve-Path "$PSScriptRoot\..").Path
$python = (Get-Command python).Source
# Task named after the folder: "jobwatch" for the main copy, "jobwatch-belgium" for a second copy.
$taskName = ((Split-Path $here -Leaf).ToLower() -replace '[^a-z0-9_-]+','-').Trim('-')
if (-not $taskName.StartsWith("jobwatch")) { $taskName = "jobwatch" }

# A separate state file so this doesn't clash with the GitHub Actions copy.
$arguments = "-m jobwatch run --notify desktop,phone --state state_local.json"

$action  = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
           -RepetitionInterval (New-TimeSpan -Hours 3)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
            -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 3)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Alerts for new jobs on careers pages" -Force | Out-Null

Write-Host "Scheduled task '$taskName' created: runs every 3 hours while you're logged in."
