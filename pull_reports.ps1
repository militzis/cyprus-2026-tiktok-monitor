# pull_reports.ps1 — run daily after GitHub Actions daily cron (04:00 UTC = 07:00 Cyprus)
# Pulls the latest reports from GitHub and logs the result.
# Scheduled via Windows Task Scheduler (see setup instructions below).

$RepoDir  = "C:\Users\milit\dev\cyprus-2026-tiktok-monitor"
$LogFile  = "$RepoDir\pull_reports.log"
$MaxLines = 200   # keep log from growing forever

Set-Location $RepoDir

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$result    = git pull --ff-only 2>&1
$status    = if ($LASTEXITCODE -eq 0) { "OK" } else { "FAIL" }

# Log entry
$entry = "[$timestamp] $status  $result"
Add-Content -Path $LogFile -Value $entry -Encoding utf8

# Trim log to last $MaxLines lines
$lines = Get-Content $LogFile -Encoding utf8
if ($lines.Count -gt $MaxLines) {
    $lines | Select-Object -Last $MaxLines | Set-Content $LogFile -Encoding utf8
}

# Print to console so Task Scheduler can capture it if needed
Write-Output $entry
