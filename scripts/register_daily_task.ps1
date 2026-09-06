param(
    [string]$TaskName = "Hindsight Daily Safety Agent",
    [string]$At = "09:00"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw "Virtual environment is missing. Run scripts\setup_local.ps1 first."
}

$RunAt = [datetime]::ParseExact($At, "HH:mm", $null)
$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "-m hindsight_agent.rag_agent" `
    -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -Daily -At $RunAt
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Daily Strands Agent run for OSHA collection and factory-risk RAG." `
    -Force | Out-Null

Write-Host "Registered '$TaskName' to run every day at $At."
