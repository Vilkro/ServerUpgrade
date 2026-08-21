# health-check.ps1
#
# Detects two different broken-but-still-running states and kills the
# process for either, so whatever restarts crashed processes (watchdog.bat's
# loop, or NSSM) picks it back up cleanly.
#
# A crashed process (gone entirely) is already handled elsewhere - this
# script exists for cases a crash-restarter can't see, because the process
# is technically still alive:
#
#  1. HUNG - stopped doing anything at all. Detected by log staleness: a
#     live server writes to its log constantly, so silence for a couple of
#     minutes means the tick loop itself has stopped.
#
#  2. STUCK IN AN ERROR LOOP - very much still running, log still being
#     written every tick, but every line is the same repeated failure
#     rather than real activity (this is what an unrecovered
#     "Socket error during UDP send... WSAEADDRNOTAVAIL" storm looks like -
#     the output queue is jammed, the server is unreachable for every
#     client, but it never goes quiet, so check #1 alone would never catch
#     it). Detected by counting repeats of the same error text in the most
#     recent lines.
#
# Run this on a schedule (Task Scheduler, every 1-2 minutes). Cheap and
# safe to run constantly: if everything's healthy it just exits.

# ---- EDIT THESE FOR YOUR SETUP ----
$ProcessName    = "Sam_DS"                                     # process name, without .exe
$LogFile        = "C:\Path\To\Server1\Content\SamTFE\Log.log"  # a file the server writes to continuously
$StaleAfterSec  = 120                                          # no log activity for this long = assume hung
$ErrorPattern   = "Socket error during UDP send"                # the generic part of the message - catches any
                                                                 # WSAE... code that triggers the same engine bug,
                                                                 # not just WSAEADDRNOTAVAIL specifically
$ErrorTailLines = 30                                            # look at this many of the most recent log lines
$ErrorThreshold = 20                                            # ...and if at least this many are the same error, it's a loop
$WatchdogLog    = Join-Path $PSScriptRoot "health-check.log"
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

function Kill-And-Report($reason, $procId) {
    Write-Log $reason
    Send-DiscordAlert $reason
    Stop-Process -Id $procId -Force
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

# --- Check 1: staleness (hung) ---
$lastWrite = (Get-Item $LogFile).LastWriteTime
$secondsSince = [math]::Round(((Get-Date) - $lastWrite).TotalSeconds)

if ($secondsSince -gt $StaleAfterSec) {
    Kill-And-Report "Server looks hung: log hasn't updated in ${secondsSince}s (limit ${StaleAfterSec}s). Killing PID $($proc.Id)." $proc.Id
    exit
}

# --- Check 2: repeated-error loop (stuck, but still writing) ---
$recentLines = Get-Content -Path $LogFile -Tail $ErrorTailLines -ErrorAction SilentlyContinue
if ($recentLines) {
    $errorCount = ($recentLines | Select-String -SimpleMatch $ErrorPattern).Count
    if ($errorCount -ge $ErrorThreshold) {
        Kill-And-Report "Server looks stuck in an error loop: $errorCount of the last $ErrorTailLines log lines are '$ErrorPattern'. Killing PID $($proc.Id)." $proc.Id
        exit
    }
}

# else: healthy, nothing logged - keeps health-check.log from filling up
# with a line every single run. Uncomment below if you want that anyway:
# Write-Log "OK - last activity ${secondsSince}s ago, no error loop detected."