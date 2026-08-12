# Automatic restart on crash/hang

Two failure modes, two different fixes:

- **Crash** (process exits) → something needs to notice it's gone and relaunch it. Handled by `watchdog.bat` below.
- **Hang** (process still running, but frozen) → Windows still sees it as "running fine," so this needs an active check, not just "is the process there." Handled by `health-check.ps1`.

## Setup: one watchdog window per server

- `watchdog.bat` - the actual supervisor loop. Generic - takes the server's id/exe/args/folder as arguments, so one copy of this file works for every server you run.
- `start-all.bat` - edit this one. One line per server, listing its id, exe path, launch args, and working directory. Mirrors the `SERVERS` list in `Dashboard/server/app.py` on purpose - if you're running the dashboard too, keep both lists describing the same servers.

Run `start-all.bat` and it opens one console window per server, each independently restarting its own server on exit. Closing one window only takes down that one server - the others keep running.

Each server writes its own `watchdog-<id>.log` next to the scripts, so you can tell which server restarted and when without digging through a shared log.

## Hang detection: `health-check.ps1`

Duplicate this per server (different `$ProcessName`/`$LogFile` if they're separate working directories) and schedule each via Task Scheduler, every 1-2 minutes. When it kills a hung process, whichever `watchdog.bat` window is supervising that server notices the exit and restarts it automatically - no extra wiring needed between the two scripts.

See the comments at the top of the script for what to edit; the main thing to get right is `$StaleAfterSec` - watch `health-check.log` for a few days before trusting the default, since a genuinely quiet-but-healthy server might log less often than you'd expect.

## Dashboard integration (optional)

Before adding anything here: **your dashboard already logs a "crash" event on its own**, every time its background poller (`_poll_loop` in `app.py`) gets a failed query where it previously got a good one - regardless of what actually restarts the server afterward. If all you want is "does the dashboard show that a server went down," you already have it, no new code needed.

Two real gaps, if you want to close them:

**1. That crash log is in-memory only.** `_event_log` is a plain Python dict - it resets to empty every time the dashboard process itself restarts. If you want crash history that survives a dashboard redeploy or reboot, it needs to go into a database instead. Given you're already running SQLite per-server (`PlayerStats.db`), the simplest option is a small `watchdog_events` table there, written whenever the poller logs a crash.

**2. You can't currently tell "crashed and came back on its own" apart from "crashed and watchdog.bat caught it."** If that distinction matters to you, `watchdog.bat` has a `DASHBOARD_URL`/`DASHBOARD_ADMIN_TOKEN` pair near the top (blank by default - it's a no-op until you fill them in). When set, it POSTs to the dashboard the moment it relaunches a server. To receive that, add this to `app.py`, matching your existing admin-route style:

```python
@app.route("/api/admin/watchdog-report", methods=["POST"])
@_require_admin
def watchdog_report():
    """Body JSON: { "server": "srv1", "exit_code": 1 }
    Called by watchdog.bat right after it relaunches a server. Purely
    informational - logs a distinct event type so you can tell "watchdog
    caught this" apart from the poller's own crash detection."""
    data = request.get_json(force=True)
    sid = data.get("server")
    if not any(s["id"] == sid for s in SERVERS):
        abort(400, description="Unknown server id")

    with _cache_lock:
        _event_log[sid].append({
            "ts": int(time.time()),
            "type": "watchdog_restart",
            "label": f"Restarted by watchdog (exit code {data.get('exit_code')})",
        })
    return jsonify({"ok": True})
```

This doesn't replace the poller's own crash detection - both would fire for the same incident (the poller sees "went offline", watchdog.bat separately reports "and I relaunched it"), which is fine; they're answering different questions.

**What I'd deliberately leave alone:** don't have the dashboard's poller *act* on a failed query by killing/relaunching the process itself. A server mid-map-load looks exactly like a dead one to that same query for as long as the load takes, so an auto-restart triggered from there would periodically fight your own `@map` command. `watchdog.bat` only reacts to the process actually exiting, which a map-load never does - that's the safer signal to act on.

## Moving to a VPS later

The batch-loop approach works anywhere, but a VPS is usually headless (no one logged in with a console window open), so at that point it's worth switching the restart mechanism to a real Windows Service via [NSSM](https://nssm.cc/) - same idea as `watchdog.bat`, but it survives logout/reboot and doesn't need a window open. `health-check.ps1` and the dashboard integration above work unchanged either way, since they don't care what actually launched the process.

## Bonus: capture crash dumps for real post-mortems

If a server actually crashes (as opposed to hangs), right now you lose the chance to see why - the process is just gone. Windows can save a memory dump automatically the moment a process crashes. Run as Administrator (adjust the exe name and dump folder):

```
reg add "HKLM\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\DedicatedServer_Custom.exe" /v DumpFolder /t REG_EXPAND_SZ /d "C:\CrashDumps" /f
reg add "HKLM\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\DedicatedServer_Custom.exe" /v DumpType /t REG_DWORD /d 2 /f
reg add "HKLM\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\DedicatedServer_Custom.exe" /v DumpCount /t REG_DWORD /d 10 /f
```

(`DumpType 2` = full dump; keeps the most recent 10.) Make sure `C:\CrashDumps` exists first. Worth doing now, before you need it - a real `.dmp` turns "it crashed again, no idea why" into an actual call stack.

## What this doesn't cover

This handles the server process dying or freezing. A genuinely empty-but-working server is a different, already-solved problem - that's what `ded_bRestartWhenEmpty`/`ded_tmRestartWhenEmptyDelay` in your existing config already does, and the two layers don't overlap.
