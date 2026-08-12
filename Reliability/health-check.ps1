# health-check.ps1
#
# Detects a HUNG server (process still running, but frozen/deadlocked)
# and kills it, so whatever restarts crashed processes (watchdog.bat's
# loop, or NSSM) picks it back up cleanly.
#
# A crashed process is already handled elsewhere - this script exists
# purely for the case a crash-restarter can't see: the process is
# technically alive, Windows thinks it's fine, but it's stopped doing
# anything.
#
# Signal used: how long since the server's log file was last written to.
# A live dedicated server writes to its log constantly (joins, chat,
# frags, periodic status) - if that goes quiet for a couple of minutes,
# the tick loop itself has almost certainly stopped.
#
# Run this on a schedule (Task Scheduler, every 1-2 minutes). It's meant
# to be cheap and safe to run constantly: if everything's healthy it just
# exits immediately.

# ---- EDIT THESE FOR YOUR SETUP ----
$ProcessName   = "Sam_DS"                                  # process name, without .exe
$LogFile       = "C:\Path\To\Server1\Content\SamTFE\Log.log"  # a file the server writes to continuously
$StaleAfterSec = 120                                       # no log activity for this long = assume hung
$WatchdogLog   = Join-Path $PSScriptRoot "health-check.log"
$DiscordWebhook = ""   # optional, paste a webhook URL to get pinged when this fires
# ------------------------------------

function Write-Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Add-Content -Path $WatchdogLog
}

function Send-DiscordAlert($msg) {
    if ([string]::IsNullOrWhiteSpace($DiscordWebhook)) { return }
    try {
        $body = @{ content = $msg } | ConvertTo-Json
        Invoke-RestMethod -Uri $DiscordWebhook -Method Post -ContentType 'application/json' -Body $body | Out-Null
    } catch {
        Write-Log "Discord alert failed: $_"
    }
}

$proc = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue

if (-not $proc) {
    # Not running at all - that's a crash, not a hang. The restart
    # mechanism (watchdog.bat loop / NSSM) is responsible for this case;
    # nothing for this script to do.
    exit
}

if (-not (Test-Path $LogFile)) {
    Write-Log "WARNING: log file not found at '$LogFile' - can't check freshness. Fix the path or enable server logging."
    exit
}

$lastWrite = (Get-Item $LogFile).LastWriteTime
$secondsSince = [math]::Round(((Get-Date) - $lastWrite).TotalSeconds)

if ($secondsSince -gt $StaleAfterSec) {
    $msg = "Server looks hung: log hasn't updated in ${secondsSince}s (limit ${StaleAfterSec}s). Killing PID $($proc.Id)."
    Write-Log $msg
    Send-DiscordAlert $msg
    Stop-Process -Id $proc.Id -Force
}
# else: healthy, nothing logged - keeps health-check.log from filling up
# with a line every single run. Uncomment below if you want that anyway:
# else { Write-Log "OK - last activity ${secondsSince}s ago." }
