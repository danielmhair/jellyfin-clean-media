<#
.SYNOPSIS
    Give every movie its own folder, taking its sidecar files with it.

.DESCRIPTION
    Turns a flat library:

        Movies\Iron Man (2008).mkv
        Movies\Iron Man (2008).cleanmedia.json
        Movies\Iron Man (2008).en.srt

    into the layout Jellyfin recommends, and that Clean Media's ten-odd
    sidecars per film make necessary:

        Movies\Iron Man (2008)\Iron Man (2008).mkv
        Movies\Iron Man (2008)\Iron Man (2008).cleanmedia.json
        Movies\Iron Man (2008)\Iron Man (2008).en.srt

    A file belongs to a movie when its name starts with that movie's name
    followed by a dot, so ".cleanmedia.json", ".eng.srt" and ".shots.json"
    travel with the video. Where two videos could both claim a file, the
    longer name wins - "Iron Man 2 (2010)" over "Iron Man (2008)".

    Nothing is deleted and nothing is overwritten. Files are moved, so on
    one volume this is a rename per file rather than a copy, and a 1200
    film library takes seconds. Every move is appended to a log as it
    happens, and -Undo replays that log backwards - which matters over
    SMB, where a share can vanish half way through.

.PARAMETER Path
    The library folder to organize.

.PARAMETER Apply
    Actually move files. Without it, the script only reports what it would
    do, which is the point: read that first.

.PARAMETER IncludeCleaned
    Also move rendered copies from a shared "cleaned" folder into each
    movie's own "cleaned" subfolder.

.PARAMETER LogPath
    Where to write the move log. Defaults to a timestamped file beside the
    library.

.PARAMETER Undo
    Move everything in the log back where it came from, newest first.

.EXAMPLE
    .\scripts\organize-library.ps1 -Path "\\NAS\Media\Movies"
    .\scripts\organize-library.ps1 -Path "\\NAS\Media\Movies" -Apply
    .\scripts\organize-library.ps1 -Undo -LogPath "\\NAS\Media\organize-20260721-140233.csv" -Apply
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Path,

    [switch]$Apply,

    [switch]$IncludeCleaned,

    [string]$LogPath,

    [switch]$Undo
)

$ErrorActionPreference = 'Stop'

# Matches VIDEO_SUFFIXES in worker/batch.py - keep the two in step.
$VideoExtensions = @('.mkv', '.mp4', '.avi', '.m4v', '.webm', '.mov')

function Write-Head($text) {
    Write-Host ""
    Write-Host "==> $text" -ForegroundColor Cyan
}

function Show-Orphans($orphans) {
    if (-not $orphans.Count) { return }
    Write-Host ""
    Write-Host "Left alone (no matching movie):" -ForegroundColor Yellow
    $orphans | Select-Object -First 20 | ForEach-Object { Write-Host "  $($_.Name)" }
    if ($orphans.Count -gt 20) { Write-Host "  ...and $($orphans.Count - 20) more" }
}

# -- undo ---------------------------------------------------------------------

function Invoke-Undo {
    if (-not $LogPath) {
        throw "-Undo needs -LogPath pointing at the log from the original run."
    }
    if (-not (Test-Path $LogPath)) {
        throw "No such log: $LogPath"
    }

    $moves = @(Import-Csv -Path $LogPath)
    if (-not $moves.Count) {
        Write-Host "Log is empty; nothing to undo."
        return
    }

    Write-Head "Undoing $($moves.Count) move(s) from $LogPath"
    if (-not $Apply) {
        Write-Host "DRY RUN - add -Apply to actually move anything." -ForegroundColor Yellow
    }

    $restored = 0
    $failed = 0
    # Newest first, so a folder is emptied before anything tries to remove it.
    [array]::Reverse($moves)
    foreach ($move in $moves) {
        if (-not (Test-Path -LiteralPath $move.To)) {
            Write-Host "  missing, skipping: $($move.To)" -ForegroundColor Yellow
            continue
        }
        if (Test-Path -LiteralPath $move.From) {
            Write-Host "  original is back already, skipping: $($move.From)" -ForegroundColor Yellow
            continue
        }

        if ($Apply) {
            try {
                Move-Item -LiteralPath $move.To -Destination $move.From -ErrorAction Stop
                $restored++
            } catch {
                Write-Host "  FAILED: $($move.To) -> $($move.From): $($_.Exception.Message)" -ForegroundColor Red
                $failed++
            }
        } else {
            Write-Host "  would restore: $($move.To)"
            $restored++
        }
    }

    # Clean up folders this script created, but only if they are now empty.
    # Deepest first: a movie folder is not empty until its own "cleaned"
    # subfolder has already gone.
    if ($Apply) {
        $folders = $moves |
            ForEach-Object { Split-Path -Parent $_.To } |
            Sort-Object -Unique |
            Sort-Object -Property { ($_ -split '[\\/]').Count } -Descending
        foreach ($folder in $folders) {
            if ((Test-Path -LiteralPath $folder) -and
                -not (Get-ChildItem -LiteralPath $folder -Force)) {
                Remove-Item -LiteralPath $folder -Force
            }
        }
    }

    Write-Host ""
    Write-Host "Restored $restored file(s), $failed failure(s)." -ForegroundColor Green
}

# -- organize -----------------------------------------------------------------

function Invoke-Organize {
    if (-not $Path) { throw "-Path is required." }
    if (-not (Test-Path -LiteralPath $Path)) { throw "No such folder: $Path" }

    $root = (Resolve-Path -LiteralPath $Path).Path
    Write-Head "Scanning $root"

    # Only the top level: files already inside a folder are left alone, so
    # re-running after adding films is safe and cheap.
    $files = @(Get-ChildItem -LiteralPath $root -File)
    $videos = @($files | Where-Object { $VideoExtensions -contains $_.Extension.ToLower() })

    if (-not $files.Count) {
        Write-Host "Nothing loose at the top level - nothing to do."
        return
    }

    # Folders already organized count as movies too, so a sidecar written
    # after the fact - a new .srt, or analysis run before organizing - joins
    # its film instead of being stranded at the root forever.
    $existing = @(Get-ChildItem -LiteralPath $root -Directory |
        Where-Object { $_.Name -ne 'cleaned' } |
        ForEach-Object { $_.Name })

    # Longest name first: "Iron Man 2 (2010)" must claim its files before
    # "Iron Man (2008)" gets a chance to.
    $stems = @(@($videos |
        ForEach-Object { [IO.Path]::GetFileNameWithoutExtension($_.Name) }) + $existing |
        Sort-Object -Unique |
        Sort-Object -Property Length -Descending)

    $groups = [ordered]@{}
    foreach ($stem in $stems) { $groups[$stem] = New-Object System.Collections.ArrayList }

    $orphans = New-Object System.Collections.ArrayList
    foreach ($file in $files) {
        $owner = $null
        foreach ($stem in $stems) {
            if ($file.Name -eq $stem -or $file.Name.StartsWith("$stem.", 'OrdinalIgnoreCase')) {
                $owner = $stem
                break
            }
        }
        if ($owner) { [void]$groups[$owner].Add($file) }
        else { [void]$orphans.Add($file) }
    }

    $cleanedRoot = Join-Path $root 'cleaned'
    $cleanedFiles = @()
    if ($IncludeCleaned -and (Test-Path -LiteralPath $cleanedRoot)) {
        $cleanedFiles = @(Get-ChildItem -LiteralPath $cleanedRoot -File)
    }

    $fileCount = 0
    $movieCount = 0
    foreach ($key in $groups.Keys) {
        $fileCount += $groups[$key].Count
        if ($groups[$key].Count) { $movieCount++ }
    }

    Write-Host "  $($videos.Count) loose video file(s) across $movieCount movie(s)"
    Write-Host "  $fileCount file(s) to move"
    if ($orphans.Count) {
        Write-Host "  $($orphans.Count) file(s) match no movie and will be left alone" -ForegroundColor Yellow
    }

    if (-not $fileCount) {
        Write-Host "Nothing to move - the library is already organized."
        Show-Orphans $orphans
        return
    }

    if (-not $Apply) {
        Write-Host ""
        Write-Host "DRY RUN - add -Apply to actually move anything." -ForegroundColor Yellow
    }

    if (-not $LogPath) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $LogPath = Join-Path (Split-Path -Parent $root) "organize-$stamp.csv"
        # Two runs in the same second must not have the second one overwrite
        # the first one's undo log.
        $suffix = 2
        while (Test-Path -LiteralPath $LogPath) {
            $LogPath = Join-Path (Split-Path -Parent $root) "organize-$stamp-$suffix.csv"
            $suffix++
        }
    }
    if ($Apply) {
        'From,To' | Out-File -FilePath $LogPath -Encoding utf8
        Write-Host "  log: $LogPath"
    }

    Write-Head "Organizing"
    $moved = 0
    $skipped = 0
    $failed = 0
    $done = 0

    foreach ($stem in $groups.Keys) {
        $done++
        if ($done % 100 -eq 0) { Write-Host "  ...$done of $($groups.Keys.Count)" }

        $folder = Join-Path $root $stem
        $group = @($groups[$stem])

        # A movie whose folder already exists needs care, not confidence.
        if ((Test-Path -LiteralPath $folder) -and -not (Test-Path -LiteralPath $folder -PathType Container)) {
            Write-Host "  SKIP $stem - a file of that name is in the way" -ForegroundColor Red
            $skipped += $group.Count
            continue
        }

        $targets = $group
        if ($IncludeCleaned) {
            $mine = @($cleanedFiles | Where-Object {
                $_.Name.StartsWith("$stem ", 'OrdinalIgnoreCase') -or
                $_.Name.StartsWith("$stem.", 'OrdinalIgnoreCase')
            })
            foreach ($file in $mine) {
                $targets = $targets + $file
            }
        }

        foreach ($file in $targets) {
            $isCleaned = $file.DirectoryName -eq $cleanedRoot
            if ($isCleaned) {
                $destFolder = Join-Path $folder 'cleaned'
            } else {
                $destFolder = $folder
            }
            $destination = Join-Path $destFolder $file.Name

            if (Test-Path -LiteralPath $destination) {
                Write-Host "  SKIP $($file.Name) - already at the destination" -ForegroundColor Yellow
                $skipped++
                continue
            }

            if (-not $Apply) {
                $moved++
                continue
            }

            try {
                if (-not (Test-Path -LiteralPath $destFolder)) {
                    New-Item -ItemType Directory -Path $destFolder -Force | Out-Null
                }
                Move-Item -LiteralPath $file.FullName -Destination $destination -ErrorAction Stop
                # Log after the move, so the log only ever claims what happened.
                '"{0}","{1}"' -f $file.FullName, $destination |
                    Out-File -FilePath $LogPath -Encoding utf8 -Append
                $moved++
            } catch {
                # One unreadable file, or an SMB blip, must not abandon the
                # other 1199 movies half organized.
                Write-Host "  FAILED $($file.Name): $($_.Exception.Message)" -ForegroundColor Red
                $failed++
            }
        }
    }

    Write-Host ""
    if ($Apply) {
        Write-Host "Moved $moved file(s), skipped $skipped, failed $failed." -ForegroundColor Green
        Write-Host "Undo with:" -ForegroundColor Cyan
        Write-Host "  .\scripts\organize-library.ps1 -Undo -LogPath `"$LogPath`" -Apply"
    } else {
        Write-Host "Would move $moved file(s), skip $skipped." -ForegroundColor Green
        Write-Host "Re-run with -Apply when that looks right." -ForegroundColor Cyan
    }

    Show-Orphans $orphans
}

if ($Undo) { Invoke-Undo } else { Invoke-Organize }
