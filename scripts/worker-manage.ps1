<#
.SYNOPSIS
    Manage the Clean Media worker: status, restart, change the media folder
    or VLM hosts, view recent activity.

.DESCRIPTION
    Generated as a "Clean Media Worker" Desktop shortcut by
    scripts/install-service.ps1 (which also regenerates it on every
    install/restart) -- double-click that instead of running this by hand.
    Mutating actions (restart, change settings) shell out to
    install-service.ps1, which needs elevation to touch Task Scheduler; this
    script itself does not need to run elevated, only the one action it
    triggers does (a UAC prompt on click, same as the macOS Homebrew password
    prompt is for that platform).

    If no service is installed yet, this falls back to running the worker
    directly in this window instead -- closing the window then stops it.
#>
param(
    [string]$TaskName = 'CleanMediaWorker',
    [int]$Port = 8765
)

# This is an interactive menu, not something to pipe input into: with stdin
# redirected/closed (piped, run under CI, launched some unexpected way),
# Read-Host returns an empty string immediately instead of blocking, which
# would otherwise spin the loop below as fast as the CPU allows -- hammering
# the worker's endpoints with no delay between iterations. A real double-click
# always has a live console, so this should never trip in normal use.
if ([Console]::IsInputRedirected) {
    Write-Host "This is an interactive menu -- run it from a real console (double-click the Desktop icon), not piped input."
    exit 1
}

$repo = Split-Path -Parent $PSScriptRoot
$stateDir = Join-Path $env:LOCALAPPDATA 'CleanMedia'
$launcher = Join-Path $stateDir 'worker-service.cmd'
$logPath = Join-Path $stateDir 'worker.log'
$installService = Join-Path $PSScriptRoot 'install-service.ps1'

function Get-CurrentConfig {
    $result = [PSCustomObject]@{ MediaRoots = $null; VlmHosts = $null }
    if (Test-Path $launcher) {
        $content = Get-Content $launcher -Raw
        if ($content -match 'set "CLEANMEDIA_MEDIA_ROOTS=([^"]*)"') { $result.MediaRoots = $Matches[1] }
        if ($content -match 'set "CLEANMEDIA_VLM_HOSTS=([^"]*)"') { $result.VlmHosts = $Matches[1] }
    }
    return $result
}

function Show-Status {
    Write-Host "Clean Media Worker"
    Write-Host "-------------------"
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 5 -ErrorAction Stop
        Write-Host ("  status   : online (v{0})" -f $r.version)
        $gpu = if ($r.gpu -and $r.gpu.available) { $r.gpu.name } else { "none" }
        Write-Host ("  gpu      : {0}" -f $gpu)
        $paused = if ($r.paused) { " -- paused" } else { "" }
        Write-Host ("  queue    : {0} job(s){1}" -f $r.queueSize, $paused)
    } catch {
        Write-Host "  status   : offline"
    }

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) { Write-Host ("  service  : runs automatically ({0})" -f $task.State) }
    else { Write-Host "  service  : not set up (option 1 can set it up)" }

    $cfg = Get-CurrentConfig
    if ($cfg.MediaRoots) {
        foreach ($root in ($cfg.MediaRoots -split ';' | Where-Object { $_ })) {
            if (Test-Path $root) { Write-Host "  media    : ok       $root" }
            else { Write-Host "  media    : warn     not found right now (unmounted?): $root" }
        }
    }
    if ($cfg.VlmHosts) {
        foreach ($h in ($cfg.VlmHosts -split ',' | Where-Object { $_ })) {
            try { Invoke-RestMethod -Uri "$h/api/tags" -TimeoutSec 3 -ErrorAction Stop | Out-Null; Write-Host "  vlm host : ok       $h" }
            catch { Write-Host "  vlm host : warn     unreachable right now: $h" }
        }
    }
    Write-Host ""
    return $cfg
}

function Invoke-ServiceElevated {
    param([string[]]$ExtraArgs = @())
    $allArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $installService,
                 '-TaskName', $TaskName, '-Port', $Port) + $ExtraArgs
    $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $allArgs -Verb RunAs -Wait -PassThru
    if ($p.ExitCode -ne 0) {
        Write-Host "install-service.ps1 exited with code $($p.ExitCode) -- see $logPath"
    }
}

function Start-Directly {
    Write-Host "Starting the worker directly in this window (closing this window stops it)..."
    Set-Location $repo
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv) { $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
    & $uv run uvicorn worker.main:app --host 0.0.0.0 --port $Port
}

while ($true) {
    Clear-Host
    $cfg = Show-Status
    Write-Host "  1) Restart the worker"
    Write-Host "  2) Change the media folder / VLM hosts"
    Write-Host "  3) View recent activity"
    Write-Host "  4) Quit this window"
    Write-Host ""
    $choice = Read-Host ">"
    switch ($choice) {
        '1' {
            $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            if ($task) {
                Invoke-ServiceElevated -ExtraArgs @('-Restart')
            } else {
                Write-Host "No background service is set up yet."
                $yn = Read-Host "Set one up now, so it starts automatically from here on? [Y/n]"
                if ($yn -match '^[Nn]') { Start-Directly }
                else { Invoke-ServiceElevated }
            }
            Read-Host "Press Enter to continue"
        }
        '2' {
            $defaultRoots = if ($cfg.MediaRoots) { $cfg.MediaRoots } else { Join-Path $repo 'movies' }
            $newRoots = Read-Host "Media folder(s), semicolon-separated [$defaultRoots]"
            if ([string]::IsNullOrWhiteSpace($newRoots)) { $newRoots = $defaultRoots }
            $newVlm = Read-Host "VLM hosts, comma-separated, blank for local only [$($cfg.VlmHosts)]"
            if ([string]::IsNullOrWhiteSpace($newVlm)) { $newVlm = $cfg.VlmHosts }

            $extra = @('-MediaRoots', $newRoots)
            if ($newVlm) { $extra += @('-VlmHosts', $newVlm) }
            $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            if ($task) { $extra += '-Restart' }
            Invoke-ServiceElevated -ExtraArgs $extra
            Read-Host "Press Enter to continue"
        }
        '3' {
            if (Test-Path $logPath) { Get-Content $logPath -Tail 20 }
            else { Write-Host "(no activity logged yet)" }
            Read-Host "Press Enter to continue"
        }
        '4' { return }
        default { }
    }
}
