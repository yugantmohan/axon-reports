<#
Installs (or re-installs) the two scheduled jobs on Windows. Safe to re-run.
Run from the project folder in PowerShell:

    powershell -ExecutionPolicy Bypass -File .\windows\install_schedule.ps1

  Axon Sync - daily 10:00: Vendor Central, WooCommerce, Buy Box, Seller   (sync.py)
  Axon Woo  - 13:00, 16:00, 19:00: WooCommerce + Buy Box                  (sync.py --only woo)

If the PC is asleep or off at a run time, the job runs as soon as it is back.
Jobs run while you are signed in (a locked screen is fine).
Logs: logs\sync.log and logs\woo.log in the project folder.
Remove: Unregister-ScheduledTask -TaskName "Axon Sync","Axon Woo" -Confirm:$false

At the end it prints the Claude Desktop entry for this PC, with the paths filled in.
#>

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Python: prefer the py launcher, and ignore the Microsoft Store placeholder.
$python = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $python = (& py -3 -c "import sys; print(sys.executable)").Trim()
}
if (-not $python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike "*WindowsApps*") { $python = $cmd.Source }
}
if (-not $python -or -not (Test-Path $python)) {
    throw "Python not found. Install Python 3.11+ from python.org and tick 'Add python.exe to PATH'."
}
Write-Host "Python:  $python"
Write-Host "Project: $root"

if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Warning ".env not found - copy .env.example to .env and fill it in before the first run."
}
New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null

# UTF-8 for every Python run. Windows defaults to an older encoding, and a
# rupee sign in a log line would otherwise crash a scheduled run.
[Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "User")

function New-AxonTask($name, $pyArgs, $log, $times) {
    $cmdLine = "/c set PYTHONUTF8=1&& `"$python`" -u sync.py $pyArgs >> logs\$log 2>&1"
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmdLine -WorkingDirectory $root
    $triggers = foreach ($t in $times) { New-ScheduledTaskTrigger -Daily -At $t }
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $triggers `
        -Settings $settings -Principal $principal `
        -Description "Axon reporting: sync.py $pyArgs" -Force | Out-Null
    Write-Host ("installed {0} at {1}" -f $name, ($times -join ", "))
}

New-AxonTask "Axon Sync" "" "sync.log" @("10:00")
New-AxonTask "Axon Woo" "--only woo" "woo.log" @("13:00", "16:00", "19:00")

Get-ScheduledTask -TaskName "Axon Sync", "Axon Woo" | Format-Table TaskName, State -AutoSize

# Claude Desktop entry for this PC. The read-only database password is read
# from .env (MCP_DATABASE_URL), so it is not in this file.
$server = Join-Path $root "mcp_server.py"
$entry = @{ mcpServers = @{ "axon-reports" = @{
    command = $python; args = @($server); env = @{ PYTHONUTF8 = "1" } } } }
Write-Host ""
Write-Host "Claude Desktop: Settings > Developer > Edit Config, and add this (merge it if the file already has mcpServers):"
Write-Host ""
$entry | ConvertTo-Json -Depth 6
