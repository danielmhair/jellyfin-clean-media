<#
.SYNOPSIS
    Run the Clean Media worker as a Windows background service.

.DESCRIPTION
    Registers a scheduled task that starts the worker at boot, before anyone
    logs in, and restarts it if it crashes. Jellyfin silently reports no
    segments whenever the worker is down, so it needs to be up whenever
    Jellyfin is.

    This uses Task Scheduler rather than a real service (sc.exe / New-Service)
    because uvicorn is a console program, not a service binary — a real
    service would need NSSM or WinSW wrapped around it for no practical gain.
    Task Scheduler already does start-at-boot, restart-on-failure, and
    run-without-login.

    Run from an *elevated* PowerShell: registering a boot-time task that runs
    without a logged-in user requires administrator rights.

    -Restart genuinely re-applies the config, it doesn't just kick the
    existing process: pass -MediaRoots/-VlmHosts alongside it to change them
    and restart in one step. Omit them on a -Restart and this keeps whatever
    was already configured — it reads that back out of the existing launcher
    rather than resetting to the defaults below, so "just restart" never
    silently drops a NAS path (see scripts/install-service.sh's matching
    fix on macOS for the same bug, found first over there).

    Also (re)writes a "Clean Media Worker" Desktop shortcut — a small
    status/restart/settings menu (scripts/worker-manage.ps1), so managing
    this day to day doesn't need a remembered PowerShell command.

    Also registers a second, always-on task ("<TaskName>Supervisor",
    worker/supervisor.py) with -RunLevel Highest, on Port+1, so the Jellyfin
    plugin's "Restart worker" button works even when the worker itself is
    unresponsive or fully down. Removed together with -Uninstall; a
    plugin-side "disable" only stops it from acting, never from listening,
    so it can always be turned back on later from the plugin.

.PARAMETER Port
    Port the worker listens on. Must match the Worker URL in the Jellyfin
    plugin settings.

.PARAMETER MediaRoots
    Semicolon-separated folders to search when Jellyfin asks about a file by
    its own path (/volume1/Movies/...) that does not exist on this machine.
    Defaults to <repo>\movies on a fresh install; on -Restart, keeps whatever
    is already configured unless you pass this explicitly.

.PARAMETER VlmHosts
    Comma-separated Ollama base URLs to fan the visual pass across, e.g.
    "http://localhost:11434,http://192.168.68.102:11434". Lets a second
    machine's GPU work the same film. Omit to use only the local Ollama (or,
    on -Restart, whatever is already configured).

.PARAMETER UsePassword
    Store your Windows password with the task. Only needed if MediaRoots
    points at a UNC path or mapped drive — without it the task runs with no
    network credentials and cannot reach an SMB share. Local paths do not
    need this.

.PARAMETER Uninstall
    Remove the task.

.EXAMPLE
    # From an elevated PowerShell, in the repo root:
    .\scripts\install-service.ps1

.EXAMPLE
    .\scripts\install-service.ps1 -MediaRoots "D:\Movies;D:\TV" -Port 8765

.EXAMPLE
    .\scripts\install-service.ps1 -MediaRoots "\\Nas\nas-8tb-hdd\Movies" -VlmHosts "http://localhost:11434,http://100.95.155.5:11434" -Restart

.EXAMPLE
    .\scripts\install-service.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [int]$Port = 8765,
    [string]$MediaRoots,
    [string]$VlmHosts,
    [string]$TaskName = 'CleanMediaWorker',
    [switch]$UsePassword,
    [switch]$AtLogon,
    [switch]$Uninstall,
    [switch]$Restart
)

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$mediaRootsExplicit = $PSBoundParameters.ContainsKey('MediaRoots')
$vlmHostsExplicit = $PSBoundParameters.ContainsKey('VlmHosts')

# The always-on recovery helper (worker/supervisor.py) — a second task, on
# port+1, registered with -RunLevel Highest so it can taskkill the orphaned
# S4U worker child (see Stop-WorkerProcesses) and restart the worker task on
# request from the Jellyfin plugin's "Restart worker" button, even when the
# worker itself is unresponsive or fully down. Elevation is authorized once,
# right here (this whole script already requires an elevated PowerShell) —
# Task Scheduler then runs every future `schtasks /run` of it elevated with
# no further UAC prompt, the same mechanism the worker's own -RestartCount
# self-heal already relies on.
$SupervisorTaskName = "${TaskName}Supervisor"

function Assert-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this from an elevated PowerShell (right-click -> Run as administrator)."
    }
}

# Ending the task kills its launcher, but the uvicorn grandchild it spawned is
# orphaned and keeps the port — and once orphaned it runs in the S4U service
# context, which only an *elevated* taskkill (or SYSTEM) can terminate. So a
# clean stop is: end the task, then hunt down whatever still holds the port.
function Stop-WorkerProcesses {
    param([int]$OnPort)
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1

    $victims = @()
    $held = Get-NetTCPConnection -LocalPort $OnPort -State Listen -ErrorAction SilentlyContinue
    if ($held) { $victims += $held.OwningProcess }
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'uvicorn\s+worker\.main' } |
        ForEach-Object { $victims += $_.ProcessId }

    foreach ($procId in ($victims | Select-Object -Unique | Where-Object { $_ })) {
        # taskkill /F reaches the S4U-context orphan that Stop-Process cannot.
        # Run it through cmd with output swallowed: a pid that has already
        # exited makes taskkill write to stderr, and under
        # $ErrorActionPreference='Stop' PowerShell 5.1 turns a native command's
        # stderr into a terminating error — which would abort the restart
        # half-done, with the worker killed but not yet relaunched.
        cmd /c "taskkill /F /T /PID $procId >nul 2>nul"
    }
    Start-Sleep -Seconds 2
    $still = Get-NetTCPConnection -LocalPort $OnPort -State Listen -ErrorAction SilentlyContinue
    return ($null -eq $still)
}

function Wait-Healthy {
    param([int]$OnPort, [int]$Tries = 90)
    foreach ($i in 1..$Tries) {
        try {
            # /api/health pings Ollama for its model list and routinely takes
            # ~2s, so a 2s timeout races it and reports a healthy worker as
            # down. Give it real headroom.
            $r = Invoke-RestMethod -Uri "http://127.0.0.1:$OnPort/api/health" -TimeoutSec 15
            return $r
        } catch { Start-Sleep -Seconds 2 }
    }
    return $null
}

# --- read back the existing launcher's config, for anything not explicitly --
# passed this time (see the -Restart bug this fixes, in the .DESCRIPTION).
$stateDir = Join-Path $env:LOCALAPPDATA 'CleanMedia'
$launcher = Join-Path $stateDir 'worker-service.cmd'
$vbsLauncher = Join-Path $stateDir 'worker-service.vbs'
$logPath = Join-Path $stateDir 'worker.log'
$iconPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Clean Media Worker.cmd'

function Get-ExistingConfig {
    $result = [PSCustomObject]@{ MediaRoots = $null; VlmHosts = $null }
    if (Test-Path $launcher) {
        $content = Get-Content $launcher -Raw
        if ($content -match 'set "CLEANMEDIA_MEDIA_ROOTS=([^"]*)"') { $result.MediaRoots = $Matches[1] }
        if ($content -match 'set "CLEANMEDIA_VLM_HOSTS=([^"]*)"') { $result.VlmHosts = $Matches[1] }
    }
    return $result
}

# --- write (or refresh) the Desktop icon, regardless of install/restart/etc --
# Called at the end of a successful run below; factored out so Uninstall can
# leave a working icon behind too (it still opens the "set it up" flow).
function Set-DesktopIcon {
    $psScript = Join-Path $PSScriptRoot 'worker-manage.ps1'
    $cmd = @"
@echo off
rem Generated by scripts\install-service.ps1 -- re-run it to regenerate.
powershell -NoProfile -ExecutionPolicy Bypass -File "$psScript" -TaskName "$TaskName" -Port $Port
"@
    Set-Content -Path $iconPath -Value $cmd -Encoding ASCII
}

Assert-Elevated

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Host "No task named '$TaskName'. Nothing to do."
        return
    }
    Stop-WorkerProcesses -OnPort $Port | Out-Null
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Get-ScheduledTask -TaskName $SupervisorTaskName -ErrorAction SilentlyContinue |
        Unregister-ScheduledTask -Confirm:$false
    Write-Host "Removed scheduled tasks '$TaskName' and '$SupervisorTaskName'. Neither will start at boot."
    Write-Host "Logs and the launcher are left in place under $stateDir."
    return
}

if ($Restart) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        throw "No task named '$TaskName'. Install it first: .\scripts\install-service.ps1"
    }
}

# A bare restart (nothing explicit passed) keeps whatever's already
# configured, read back from the existing launcher rather than reset to the
# hardcoded defaults below.
$existingConfig = Get-ExistingConfig
if (-not $mediaRootsExplicit -and $existingConfig.MediaRoots) { $MediaRoots = $existingConfig.MediaRoots }
if (-not $vlmHostsExplicit -and $existingConfig.VlmHosts) { $VlmHosts = $existingConfig.VlmHosts }
if ([string]::IsNullOrWhiteSpace($MediaRoots)) {
    $MediaRoots = Join-Path $repo 'movies'
}

# --- checks: warn, don't block ----------------------------------------------
# A NAS can be unmounted, or a GPU box turned off, at the exact moment this
# runs -- the worker itself already tolerates an unreachable root/host, so
# these are informational, not fatal.
Write-Host "==> Checking configured paths and hosts"
foreach ($root in ($MediaRoots -split ';' | Where-Object { $_ })) {
    if (Test-Path $root) { Write-Host "  ok       media folder reachable: $root" }
    else { Write-Host "  warn     media folder not found right now (unmounted?): $root" }
}
if (-not [string]::IsNullOrWhiteSpace($VlmHosts)) {
    foreach ($h in ($VlmHosts -split ',' | Where-Object { $_ })) {
        try {
            Invoke-RestMethod -Uri "$h/api/tags" -TimeoutSec 3 -ErrorAction Stop | Out-Null
            Write-Host "  ok       vlm host reachable: $h"
        } catch {
            Write-Host "  warn     vlm host unreachable right now: $h"
        }
    }
}

# --- locate uv ---------------------------------------------------------------
# uv installs to ~\.local\bin, which is not on the PATH a scheduled task gets,
# so resolve it to an absolute path now and bake that into the launcher.
$uv = $null
$found = Get-Command uv -ErrorAction SilentlyContinue
if ($null -ne $found) {
    $uv = $found.Source
} else {
    $candidates = @(
        (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\uv\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe')
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $uv = $c; break }
    }
}
if ($null -eq $uv) {
    throw "uv not found. Install it with: winget install astral-sh.uv"
}

# Something already holds the port — a hand-started worker, or a previous
# task's orphaned uvicorn. Reclaim it rather than dying on a bind error that
# only surfaces in the log. On reinstall (or restart) this is the normal case.
$inUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($null -ne $inUse) {
    Write-Host "Port $Port is in use; reclaiming it..."
    if (-not (Stop-WorkerProcesses -OnPort $Port)) {
        throw "Port $Port is still held after trying to stop it. Reboot to clear it, then re-run."
    }
}

# --- (re)write the launcher --------------------------------------------------
# A scheduled task action cannot set environment variables, so the task runs a
# small .cmd that sets them and then execs uv. It lives outside the repo so it
# is not something you have to remember to gitignore. Regenerated on every
# run, restart included -- deliberate: it's what makes -MediaRoots/-VlmHosts
# passed alongside -Restart actually take effect, and re-registering with
# Task Scheduler is cheap and idempotent either way.
if (-not (Test-Path $stateDir)) { New-Item -ItemType Directory -Path $stateDir | Out-Null }

# Optional: a pool of Ollama hosts for the visual pass (multi-GPU). One line
# only when set, so a single-host setup keeps the exact launcher it had.
$vlmLine = if (-not [string]::IsNullOrWhiteSpace($VlmHosts)) {
    "set `"CLEANMEDIA_VLM_HOSTS=$VlmHosts`"`r`n"
} else { '' }

# Generated, not checked in: every value below is machine-specific.
$cmd = @"
@echo off
rem Generated by scripts\install-service.ps1 -- edits here are overwritten on reinstall.
set "CLEANMEDIA_MEDIA_ROOTS=$MediaRoots"
$vlmLine
cd /d "$repo"

rem Keep the log from growing without bound across reboots.
for %%A in ("$logPath") do if %%~zA GTR 10485760 del "$logPath"

echo. >> "$logPath"
echo ==== starting worker on port $Port ==== >> "$logPath"
"$uv" run uvicorn worker.main:app --host 0.0.0.0 --port $Port >> "$logPath" 2>&1
"@
Set-Content -Path $launcher -Value $cmd -Encoding ASCII

# An interactive (-AtLogon) task runs in your session, so launching the .cmd
# directly pops a blank console window (blank because output is redirected to
# the log). Run it through wscript with window style 0 (hidden) instead. The
# worker still writes everything to worker.log; watch it with
# `Get-Content <log> -Wait -Tail 50`. bWaitOnReturn=True keeps wscript alive for
# the worker's whole lifetime, so Task Scheduler's restart-on-failure still sees
# the process and can restart it if it dies.
$vbs = @"
' Generated by scripts\install-service.ps1 -- edits here are overwritten on reinstall.
' Launches the worker with no visible console window (0 = hidden, True = wait).
CreateObject("WScript.Shell").Run Chr(34) & "$launcher" & Chr(34), 0, True
"@
Set-Content -Path $vbsLauncher -Value $vbs -Encoding ASCII

# --- register the task -----------------------------------------------------
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbsLauncher`"" -WorkingDirectory $repo
# -AtLogon runs the worker inside your interactive session when you sign in,
# which gives it your full network access and saved NAS credential with no
# stored password (a PIN cannot be used for a boot task). The tradeoff is it
# starts at login rather than before it. Default is -AtStartup (before login),
# which needs -UsePassword to reach a network share.
$trigger = if ($AtLogon) {
    New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
} else {
    New-ScheduledTaskTrigger -AtStartup
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)   # never time out: this runs forever

$user = "$env:USERDOMAIN\$env:USERNAME"

$register = @{
    TaskName    = $TaskName
    Action      = $action
    Trigger     = $trigger
    Settings    = $settings
    Description = "Clean Media analysis worker. Serves approved segments to the Jellyfin plugin."
    Force       = $true
}

if ($AtLogon) {
    # Interactive: the worker runs inside your logged-in session, so it inherits
    # your network access and Credential Manager (the saved NAS login) with no
    # stored password. This is the way to reach a network share without knowing
    # the account password (a PIN cannot be stored with a task).
    $register.Principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
} elseif ($UsePassword) {
    # Stored credentials, so the task has network access to UNC media roots.
    # This is the WINDOWS sign-in for this PC, not the NAS login: the task runs
    # as this Windows user and reuses whatever NAS credential is already saved
    # in Credential Manager for that user, so the NAS password is never entered
    # here. Only the password matters — entering the NAS *username* is the usual
    # mistake (it fails with "no mapping between account names and security
    # IDs"), so force the account to this PC's user and take just the password.
    $msg = "Enter the WINDOWS sign-in password for $user`n" +
           "(this PC's login password, NOT your NAS password).`n" +
           "The username is fixed; only the password is used."
    $cred = Get-Credential -UserName $user -Message $msg
    $register.User = $user
    $register.Password = $cred.GetNetworkCredential().Password
    $register.RunLevel = 'Limited'
} else {
    # S4U: runs whether or not you are logged in, without storing a password.
    # The tradeoff is no network credentials -- fine for local media roots.
    $register.Principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
}

Register-ScheduledTask @register | Out-Null
if ($Restart) {
    Write-Host "==> Restarted '$TaskName' with the config above."
} else {
    Write-Host "==> Registered scheduled task '$TaskName'."
}
Write-Host "  launcher   $launcher"
Write-Host "  log        $logPath"
Write-Host "  media root $MediaRoots"

Set-DesktopIcon
Write-Host "  icon       $iconPath"

# --- start and verify --------------------------------------------------------
# Verify the invariant, not the exit code: a task that starts and immediately
# dies still reports success.
Start-ScheduledTask -TaskName $TaskName
Write-Host ""
Write-Host "Waiting for the worker to answer on port $Port. First start loads the"
Write-Host "speech and vision models, which takes a minute or two..."

# Generous: a cold start on this machine took ~70s, and a too-short wait
# would report failure on a worker that was merely still loading.
$r = Wait-Healthy -OnPort $Port
if ($null -eq $r) {
    Write-Warning "The worker did not answer within 3 minutes. Check the log:"
    Write-Warning "  Get-Content '$logPath' -Tail 40"
    exit 1
}
Write-Host "Worker $($r.version) is up."
if (-not $Restart) {
    Write-Host "  engines: $(($r.engines.PSObject.Properties.Name) -join ', ')"
}

# --- register the always-on recovery helper (worker/supervisor.py) ----------
# Always AtStartup + S4U, regardless of -AtLogon above: it only makes HTTP
# calls and runs schtasks/taskkill, none of which need an interactive session
# or network credentials, and it needs to be reachable before anyone logs in.
$supervisorPort = $Port + 1
$supervisorAction = New-ScheduledTaskAction -Execute $uv `
    -Argument "run python -m worker.supervisor --port $supervisorPort --worker-port $Port --task-name $TaskName" `
    -WorkingDirectory $repo
$supervisorTrigger = New-ScheduledTaskTrigger -AtStartup
$supervisorSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$supervisorPrincipal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Highest

Register-ScheduledTask -TaskName $SupervisorTaskName -Action $supervisorAction `
    -Trigger $supervisorTrigger -Settings $supervisorSettings -Principal $supervisorPrincipal `
    -Description "Clean Media recovery helper. Lets the Jellyfin plugin restart the worker." `
    -Force | Out-Null
Start-ScheduledTask -TaskName $SupervisorTaskName
Write-Host "  recovery helper running on port $supervisorPort"

if (-not $Restart) {
    $ips = Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
        Select-Object -ExpandProperty IPAddress

    Write-Host ""
    Write-Host "Set the Worker URL in the Jellyfin plugin settings to one of:"
    foreach ($ip in $ips) { Write-Host "    http://${ip}:$Port" }
    Write-Host ""
    Write-Host "Use the address on the same LAN as your Jellyfin server. A Tailscale"
    Write-Host "address will not work if Jellyfin runs in a Docker bridge network."
}
