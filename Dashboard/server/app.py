"""
sscp_dashboard/server/app.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Serves:
  GET  /api/servers                    → live state of all configured SS servers
  GET  /api/players                    → aggregated player stats from PlayerStats.db
  GET  /api/players/<guid>             → single player detail + session history
  GET  /api/activity                   → 24h online-player timeline (5-min buckets)
  GET  /api/demos                      → list of recorded demo files
  GET  /api/demos/<server_id>/<file>   → download a single demo part
  GET  /api/demos/<server_id>/zip      → download multiple parts as a .zip
                                         ?parts=file1.dem,file2.dem[,...]
  GET  /api/maps/<server_id>/<file>    → download a map pack file
  POST /api/admin/kick                 → kick a player via RCON
  POST /api/admin/ban                  → ban a player via RCON
  POST /api/admin/exec                 → arbitrary RCON command (admin auth required)
  GET  /api/health                     → sanity check

Dependencies:
  pip install flask flask-cors requests

Run:
  python app.py
  (then open http://localhost:5000)
"""

import os, io, shlex, socket, struct, subprocess, tempfile, time, threading, json, sqlite3, re, zipfile
import psutil   # used by the output-queue watchdog to find/kill a stuck server process
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Flask, jsonify, request, send_from_directory, abort, Response, stream_with_context
from flask_cors import CORS

# ─── Configuration ────────────────────────────────────────────────────────────

# One entry per server you manage.  Edit these to match your setup.
SERVERS = [
    {
        "id": "fe1",
        "label": "",
        "host": "192.168.10.11",
        "port": 25626,  # net_iPort from init.ini
        "db": "D:/CustomTFE/Bin/PlayerStats.db",  # path to this server's PlayerStats.db
        "demos_dir": "D:/CustomTFE/Demos/CustomCoop",  # path to Demos\ folder
        "maps_dir": "D:/CustomTFE",  # path to folder containing map pack zips for download
        "rcon_pass": "hrtmcftgmkjfgjsrhu",  # net_strAdminPassword from init.ini
        "log_file": "D:/CustomTFE/Dedicated_CustomCoop.log",  # the log file this server's process writes to
        "process_match": "D:/CustomTFE/Bin/DedicatedServer_Custom.exe CustomCoop",  # used to find the process among possibly-several server instances
    },
    {
        "id":       "fe2",
        "label":    "",
        "host":     "192.168.10.11",
        "port":     25636,             # net_iPort from init.ini
        "db":       "D:/CustomTFE/Bin/PlayerStats.db",     # path to this server's PlayerStats.db
        "demos_dir":"D:/CustomTFE/Demos/RocketJump",              # path to Demos\ folder
        "maps_dir": "D:/CustomTFE",               # path to folder containing map pack zips for download
        "rcon_pass":"hrtmcftgmkjfgjsrhu",          # net_strAdminPassword from init.ini
        "log_file": "D:/CustomTFE/Dedicated_RocketJump.log",  # the log file this server's process writes to
        "process_match": "D:/CustomTFE/Bin/DedicatedServer_Custom.exe RocketJump",  # used to find the process among possibly-several server instances
    },
    {
        "id":       "se1",
        "label":    "",
        "host":     "192.168.10.11",
        "port":     25646,             # net_iPort from init.ini
        "db":       "D:/CustomTSE/Bin/PlayerStats.db",     # path to this server's PlayerStats.db
        "demos_dir":"D:/CustomTSE/Demos/CustomCoop",              # path to Demos\ folder
        "maps_dir": "D:/CustomTSE",               # path to folder containing map pack zips for download
        "rcon_pass":"hrtmcftgmkjfgjsrhu",          # net_strAdminPassword from init.ini
        "log_file": "D:/CustomTSE/Dedicated_CustomCoop.log",  # the log file this server's process writes to
        "process_match": "D:/CustomTSE/Bin/DedicatedServer_Custom.exe CustomCoop",  # used to find the process among possibly-several server instances
    },
    {
        "id":       "se2",
        "label":    "",
        "host":     "192.168.10.11",
        "port":     25656,             # net_iPort from init.ini
        "db":       "D:/CustomTSE/Bin/PlayerStats.db",     # path to this server's PlayerStats.db
        "demos_dir":"D:/CustomTSE/Demos/CustomFrag",              # path to Demos\ folder
        "maps_dir": "D:/CustomTSE",               # path to folder containing map pack zips for download
        "rcon_pass":"hrtmcftgmkjfgjsrhu",          # net_strAdminPassword from init.ini
        "log_file": "D:/CustomTSE/Bin/Dedicated_CustomFrag.log",  # the log file this server's process writes to
        "process_match": "D:/CustomTSE/Bin/DedicatedServer_Custom.exe CustomFrag",  # used to find the process among possibly-several server instances
    },
    {
        "id":       "se3",
        "label":    "",
        "host":     "192.168.10.11",
        "port":     25666,             # net_iPort from init.ini
        "db":       "D:/CustomTSE/Bin/PlayerStats.db",     # path to this server's PlayerStats.db
        "demos_dir":"D:/CustomTSE/Demos/RocketJumpSE",              # path to Demos\ folder
        "maps_dir": "D:/CustomTSE",               # path to folder containing map pack zips for download
        "rcon_pass":"hrtmcftgmkjfgjsrhu",          # net_strAdminPassword from init.ini
        "log_file": "D:/CustomTSE/Dedicated_RocketJumpSE.log",  # the log file this server's process writes to
        "process_match": "D:/CustomTSE/Bin/DedicatedServer_Custom.exe RocketJumpSE",  # used to find the process among possibly-several server instances
    },
    # Add more servers here:
    # { "id": "srv2", "label": "Custom Maps", "host": "127.0.0.1", "port": 25667, ... },
]

# Which game each server belongs to (tfe/tse), derived from its db path rather
# than hand-tagged, so it can't drift out of sync with the actual config.
# Used to let cross-server views (marks leaderboard, fragmatch leaders) be
# scoped to one game - guids are meaningless to compare across TFE and TSE,
# even if two players happen to share a nickname.    1111
for _srv in SERVERS:
    _db_path_lower = str(_srv.get("db", "")).lower()
    if "tse" in _db_path_lower:
        _srv["family"] = "tse"
    elif "tfe" in _db_path_lower:
        _srv["family"] = "tfe"
    else:
        _srv["family"] = ""
del _srv, _db_path_lower

POLL_INTERVAL   = 15      # seconds between GameSpy polls
ACTIVITY_WINDOW = 86400   # 24 h of activity history
ACTIVITY_BUCKET = 300     # 5-minute buckets for the graph
ADMIN_TOKEN     = "hrtmcftgmkjfgjsrhu"
MIN_DEMO_SIZE_BYTES = 1024   # demos smaller than this are hidden (incomplete recordings)

# Several server processes can share one PlayerStats.db file (e.g. two game
# modes running out of the same install - see fe1/fe2 and se1/se2/se3 above,
# which point at the same "db" path). PlayerDB now tags every row it writes
# with the server's net_iPort in a "server_id" column, so rows read from a
# shared DB can be attributed to the *actual* server that wrote them rather
# than whichever config entry happened to be used to open the connection.
# Ports are unique per SERVERS entry, so this lookup is unambiguous.    1111
_SERVER_BY_PORT = {int(s["port"]): s for s in SERVERS if s.get("port")}

def _server_for_row(row_server_id, fallback_srv: dict) -> dict:
    """Resolve the real owning server for a DB row via its server_id (port)
    column, falling back to whichever server config we used to open the
    connection (covers rows written before this migration, where
    server_id is 0/unknown)."""
    return _SERVER_BY_PORT.get(_safe_int(row_server_id)) or fallback_srv

# ─── App ─────────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder="../static", static_url_path="/static")
CORS(app)

# In-memory cache updated by the background poller
_server_cache = {}   # server_id → dict with live state
_cache_lock   = threading.Lock()

# In-memory activity log:  server_id → list of (unix_ts, player_count)
_activity_log = {s["id"]: [] for s in SERVERS}

# Persistent name→country cache so returning/active players always show a country.
# Once we see a country for a given cleaned name we keep it here so it survives
# across polls and brief reconnects.
_country_cache: dict = {}   # cleaned-casefold name → country string

# Flask-side async IP→country cache.
# Populated by background threads when the DB country column is still empty
# (covers the window between a player joining and C++ GeoIP writing to the DB).
_ip_country_cache: dict = {}   # ip → "Country Name" or ""
_ip_lookup_pending: set  = set()

# In-memory event log used by the activity graphs.
_event_log = {s["id"]: [] for s in SERVERS}

SAM_MARKUP_RE = re.compile(r"\^[cC][0-9a-fA-F]{6}|\^[aA][0-9a-fA-F]{2}|\^[fF][0-9]?|\^[rRcCbBiIjJnNaA0-9]")
DEMO_NAME_RE = re.compile(r"^(\d{8})_(\d{6})(?:_to_(\d{8})_(\d{6}))?_(.+)\.dem$", re.IGNORECASE)
DEMO_PART_SECONDS = 300      # no longer used to compute duration (see _demo_files) - kept as a reference to the configured rotation interval
DEMO_GROUP_GAP_SECONDS = 360
DEMO_MAX_PLAUSIBLE_SECONDS = 3600   # sanity cap on a single part's inferred duration, in case a file's mtime is wrong (e.g. touched by a backup job long after recording actually stopped)

def _safe_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_sam_text(value: str | None) -> str:
    """Remove Serious Sam color/control markup from names for matching/display."""
    return SAM_MARKUP_RE.sub("", value or "").strip()


def _display_map_name(value: str | None) -> str:
    if not value:
        return ""
    name = value.replace("/", "\\").split("\\")[-1]
    if name.lower().endswith(".wld"):
        name = name[:-4]
    return name.replace("_", " ").strip()


def _trim_event_log(sid: str, now: int):
    cutoff = now - ACTIVITY_WINDOW
    _event_log[sid] = [e for e in _event_log.get(sid, []) if e.get("ts", 0) >= cutoff]


def _peak_players(sid: str) -> int:
    """Highest concurrent player count seen for this specific server in the
    last ACTIVITY_WINDOW. Backed by _activity_log, which is sampled directly
    from each server's own GameSpy query every POLL_INTERVAL - unlike the
    sessions table, this was never shared across servers, so it's already
    correctly scoped per server with no server_id filtering needed."""
    log = _activity_log.get(sid, [])
    return max((c for _t, c in log), default=0)


# ─── Flask-side GeoIP (async fallback when DB country is still empty) ─────────

def _geoip_resolve_bg(ip: str) -> None:
    """Worker thread: resolve one IP via ip-api.com and write result to cache."""
    try:
        r = requests.get(
            f"http://ip-api.com/json/{ip}?fields=status,country",
            timeout=3.0,
        )
        data = r.json()
        result = data.get("country", "") if data.get("status") == "success" else ""
    except Exception:
        result = ""
    _ip_country_cache[ip] = result   # store "" too — prevents retry storms
    _ip_lookup_pending.discard(ip)


def _geoip_ensure_resolved(ip: str) -> str:
    """Return cached country for ip.  On a miss, kick off a background resolve
    so the next poll cycle can read the result (usually ready within ~3 s)."""
    if not ip:
        return ""
    val = _ip_country_cache.get(ip)   # None = never tried; "" = tried + failed
    if val is not None:
        return val
    if ip not in _ip_lookup_pending:
        _ip_lookup_pending.add(ip)
        threading.Thread(target=_geoip_resolve_bg, args=(ip,), daemon=True).start()
    return ""


# ─── GameSpy UDP query ────────────────────────────────────────────────────────

def _gamespy_packet_seq(text: str) -> int:
    """Extracts the packet sequence number from a chunk's \\queryid\\S.N\\
    field (N = this packet's 1-based position in the response), so a
    multi-packet response can be reassembled in the correct order.
    Falls back to 0 (sorts first) if the field is missing - better to risk
    putting an unlabeled chunk first than crash on a malformed response."""
    m = re.search(r"\\queryid\\[^\\]*\.(\d+)\\", text)
    return int(m.group(1)) if m else 0


def _gamespy_query(host: str, port: int, timeout: float = 2.0) -> dict | None:
    """
    Send a GameSpy \\status\\ query to a Serious Sam dedicated server.
    Returns a dict of key→value pairs, or None on failure.

    The SS/SE1 GameSpy server listens on  net_iPort + 1  (port+1 is the
    query port in the legacy GameSpy protocol used by SE1).
    """
    query_port = port + 1
    payload = b"\\status\\"

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.sendto(payload, (host, query_port))

        # A response with several players usually doesn't fit in one UDP
        # packet, so GameSpy splits it into several, each tagged with its
        # own \queryid\<session>.<n>\ sequence number. UDP does not
        # guarantee packets arrive in the order they were sent - [1111]
        # this used to just concatenate them in arrival order, which
        # corrupted the reassembled string whenever they got reordered:
        # numplayers (always in the first packet) stayed correct, but a
        # later packet's player_N block could land mid-string, truncating
        # the player list the parser sees (e.g. showing 3/8 online but
        # only ever listing the first player). Sorting by sequence number
        # before joining reassembles it correctly regardless of arrival order.
        chunks = []  # list of (seq_number, text)
        got_any_data = False
        while True:
            try:
                data, _ = s.recvfrom(4096)
                got_any_data = True
                text = data.decode("cp1251", errors="replace")
                chunks.append((_gamespy_packet_seq(text), text))
                if b"\\final\\" in data:
                    break
            except socket.timeout:
                break
        s.close()

        # [1111] fixed: previously, a fully unreachable server (nothing ever
        # replies, so this loop times out on the very first recvfrom) fell
        # through to _parse_gamespy("") - which doesn't raise, it just returns
        # a dict with hostname="", numplayers=0, maxplayers=8 defaulted in.
        # That's a non-None, "valid-looking" result, so the poller reported
        # the server as online with nobody home. Zero bytes ever received
        # means offline, full stop - don't hand that to the parser at all.
        if not got_any_data:
            return None

        chunks.sort(key=lambda c: c[0])
        raw = "".join(text for _seq, text in chunks)
        return _parse_gamespy(raw)
    except Exception:
        return None


def _parse_gamespy(raw: str) -> dict:
    """
    Parse a GameSpy key\\value\\key\\value\\ string into a dict.
    Player blocks (player_N, frags_N, ping_N, etc.) are collected
    into a  "players"  list of dicts.
    """
    parts = raw.strip("\\").split("\\")
    kvs = {}
    for i in range(0, len(parts) - 1, 2):
        kvs[parts[i]] = parts[i + 1]

    result = {
        "hostname":   kvs.get("hostname", ""),
        "mapname":    kvs.get("mapname", ""),
        "gametype":   kvs.get("gametype", ""),
        "numplayers": _safe_int(kvs.get("numplayers"), 0),
        "maxplayers": _safe_int(kvs.get("maxplayers"), 8),
        "players":    [],
    }

    i = 0
    while True:
        name_key = f"player_{i}"
        if name_key not in kvs:
            break
        result["players"].append({
            "name":    kvs.get(f"player_{i}", ""),
            "frags":   _safe_int(kvs.get(f"frags_{i}"), 0),
            "deaths":  _safe_int(kvs.get(f"deaths_{i}"), 0),
            "score":   _safe_int(kvs.get(f"score_{i}"), 0),
            "ping":    _safe_int(kvs.get(f"ping_{i}"), 0),
            "country": kvs.get(f"country_{i}", ""),
            "city":    kvs.get(f"city_{i}", ""),
            "active":  kvs.get(f"active_{i}", kvs.get(f"time_{i}", "")),
        })
        i += 1

    return result


# ─── Background poller ────────────────────────────────────────────────────────

def _poll_loop():
    while True:
        for srv in SERVERS:
            sid = srv["id"]
            state = _gamespy_query(srv["host"], srv["port"])

            now = int(time.time())
            count = state["numplayers"] if state else 0

            with _cache_lock:
                existing = _server_cache.get(sid, {})
                if state:
                    old_map = existing.get("mapname")
                    new_map = state.get("mapname")
                    if existing.get("online") and old_map and new_map and old_map != new_map:
                        _event_log[sid].append({
                            "ts": now,
                            "type": "map",
                            "label": _display_map_name(new_map),
                            "from": old_map,
                            "to": new_map,
                        })
                        # [1111] A successful query is the ground truth that the
                        # server has genuinely recovered - clear any "stuck"
                        # state the watchdog set, rather than timing it out
                        # blindly. If it's still actually broken, this branch
                        # never runs and the indicator correctly stays on.
                        if existing.get("stuck") and existing.get("online") is False:
                            _event_log[sid].append({
                                "ts": now,
                                "type": "recovered",
                                "label": "Recovered after stuck output queue",
                            })

                    _server_cache[sid] = {
                        "online":     True,
                        "queried_at": now,
                        "stuck":      False,
                        **state,
                    }
                else:
                    if existing.get("online"):
                        _event_log[sid].append({
                            "ts": now,
                            "type": "crash",
                            "label": "Offline",
                        })
                    _server_cache[sid] = {
                        **existing,
                        "online":     False,
                        "queried_at": now,
                        "numplayers": 0,
                        "players":    [],
                    }

                log = _activity_log[sid]
                log.append((now, count))
                cutoff = now - ACTIVITY_WINDOW
                _activity_log[sid] = [(t, c) for t, c in log if t >= cutoff]
                _trim_event_log(sid, now)

            # Enrich players OUTSIDE the lock so DB reads and GeoIP threads
            # don't block cache readers.  GeoIP background threads that fire
            # here will have the full POLL_INTERVAL to complete before the
            # next cycle reads _ip_country_cache — so country shows up within
            # one extra poll (≤15 s) rather than never.
            if state:
                enriched = _enrich_live_players(sid, state.get("players", []))
                with _cache_lock:
                    if _server_cache.get(sid, {}).get("online"):
                        _server_cache[sid]["players"] = enriched

        time.sleep(POLL_INTERVAL)


threading.Thread(target=_poll_loop, daemon=True).start()

# ─── Output-queue watchdog ─────────────────────────────────────────────────────
# Detects the specific "Socket error during UDP send... WSAEADDRNOTAVAIL" loop:
# a real engine bug where one undeliverable packet gets stuck at the head of
# the outgoing queue forever, and the server goes silent for every client at
# once while spamming the same error every tick. Unlike the GameSpy poller
# above, this is NOT triggered by "server didn't respond to a query" - that
# signal is ambiguous with a completely normal map-change restart, which can
# legitimately take a while to respond. This instead watches the server's own
# log for the exact repeated error text, which a normal map load never
# produces, so it doesn't fight your own @map/restart flow.
#
# Only takes action if BOTH log_file and process_match are set for a server
# (see the SERVERS config above) - servers without them are silently skipped,
# so this is opt-in per server.

WATCHDOG_INTERVAL   = 15     # seconds between checks
WATCHDOG_ERROR_TEXT = b"Socket error during UDP send"
WATCHDOG_THRESHOLD  = 20     # this many matches in the newly-written log = stuck
WATCHDOG_COOLDOWN   = 90     # don't re-trigger for the same server within this many seconds of killing it
WATCHDOG_PORT_ERROR_TEXT = b"cannot open UDP socket"
WATCHDOG_PORT_ERROR_SIGNALS = (b"cannot open UDP socket", b"WSAEADDRINUSE")

_watchdog_offsets = {}   # server_id -> byte offset already read
_watchdog_last_kill = {}  # server_id -> unix ts of last kill, for the cooldown
_watchdog_config_warned = set()  # server_ids already warned about missing log_file/process_match, so it's logged once, not every loop


def _read_new_log_bytes(sid: str, path: str) -> bytes:
    try:
        size = os.path.getsize(path)
    except OSError:
        return b""

    last_offset = _watchdog_offsets.get(sid, size)
    if last_offset > size:
        last_offset = 0

    try:
        with open(path, "rb") as f:
            f.seek(last_offset)
            data = f.read()
    except OSError:
        return b""

    _watchdog_offsets[sid] = size
    return data


def _find_server_process(match_text: str, exclude_pid: int = None):
    matches = []

    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            if exclude_pid and p.info["pid"] == exclude_pid:
                continue

            cmdline = " ".join(p.info.get("cmdline") or [])

            if match_text.lower() in cmdline.lower():
                matches.append(p)

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if len(matches) > 1:
        print(
            f"[watchdog] WARNING: {len(matches)} processes matched "
            f"'{match_text}' - not killing anything, "
            f"fix process_match to be more specific."
        )
        return None

    return matches[0] if matches else None


def _find_pid_holding_udp_port(port: int):
    try:
        output = subprocess.check_output(
            ["netstat", "-ano", "-p", "UDP"],
            text=True,
            timeout=5,
        )
    except Exception as e:
        print(f"[watchdog] netstat failed: {e}")
        return None

    port_suffix = f":{port}"

    for line in output.splitlines():
        parts = line.split()

        if len(parts) < 4 or parts[0] != "UDP":
            continue

        local_addr, pid_str = parts[1], parts[-1]

        if local_addr.endswith(port_suffix) and pid_str.isdigit():
            return int(pid_str)

    return None


def _looks_like_our_game_server(pid: int) -> bool:
    try:
        p = psutil.Process(pid)
        proc_name = (p.name() or "").lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

    if "sam_ds" in proc_name or "serioussam" in proc_name or "dedicatedserver" in proc_name:
        return True

    # cmdline() can be denied even when name() isn't (varies by Windows
    # version/permissions) - don't let that sink the whole check when the
    # exe-name comparison below might still succeed on its own.
    try:
        cmdline = " ".join(p.cmdline() or []).lower().replace("\\", "/")
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        cmdline = ""

    for srv in SERVERS:
        match_text = srv.get("process_match", "")
        if not match_text:
            continue
        match_norm = match_text.lower().replace("\\", "/")

        # Primary check: does this PID's own executable filename match the
        # exe named in process_match? Far more reliable than a full
        # command-line comparison, which can differ in path separators,
        # quoting, or relative-vs-absolute launching even for the exact
        # same process - which is what caused this check to wrongly say
        # "not ours" about a process that actually was ours.    1111
        match_exe = match_norm.split(" ")[0].rsplit("/", 1)[-1]
        if match_exe and match_exe == proc_name:
            return True

        # Fallback: full command-line substring match, both sides
        # normalized to forward slashes first.
        if match_norm and match_norm in cmdline:
            return True

    return False


def _kill_and_relaunch(srv: dict, pids_in_order: list) -> bool:
    """Runs one real, ordered command-line sequence: taskkill each PID in
    pids_in_order (in that order - an already-dead PID is skipped over, not
    treated as fatal), pause a moment for Windows to actually release the
    port, then start the server fresh via the `start` command - exactly the
    steps you'd type by hand at a command prompt.

    Written out as an actual temporary .bat file rather than one chained
    "cmd /c a & b & c" string [1111]: passing a multi-command string that
    itself contains quotes (start requires an explicit "" empty-title
    argument) through subprocess's own argument-quoting layer *on top of*
    cmd.exe's own quote parsing is a well-known Windows minefield - the two
    layers of escaping can silently mangle the final command. That's what
    caused the "Windows cannot find '\\'" popup: the launch target ended up
    garbled into a stray backslash instead of the actual exe path. A .bat
    file has no such problem - each line is just plain text, taskkill/
    timeout/start each get their own clean line, and it's directly
    inspectable (it prints its own path before running) if anything about
    it still needs debugging. It deletes itself as its last line.

    This also keeps the fix from a few iterations ago: IDEs like PyCharm
    run your script inside a Windows Job Object that force-kills every
    descendant the moment you stop the run/debug session, and attaches
    child consoles to its own Run pane instead of giving them a real
    window. CREATE_BREAKAWAY_FROM_JOB is applied to the cmd.exe that runs
    the .bat, not to the game server directly - but since cmd.exe itself is
    no longer part of any job once it breaks away, whatever it goes on to
    `start` isn't part of one either. The orchestrating cmd.exe window
    itself is hidden (it's just plumbing); the actual game server still
    gets its own new, fully visible console via `start`, same as launching
    it by hand would.

    Run with cwd = the folder containing this server's PlayerStats.db (same
    folder as its .exe in every current config), since PlayerDB.cpp opens
    "PlayerStats.db" as a path relative to the process's working directory."""
    match_text = srv.get("process_match")
    if not match_text:
        print(f"[watchdog] {srv['id']}: no process_match configured, can't relaunch")
        return False

    cwd = None
    if srv.get("db"):
        try:
            cwd = str(Path(srv["db"]).resolve().parent)
        except OSError:
            cwd = None

    try:
        # posix=False: Windows-style backslash paths shouldn't be treated
        # as escape sequences the way shlex would by default.
        args = shlex.split(match_text, posix=False)
    except ValueError as e:
        print(f"[watchdog] {srv['id']}: couldn't parse process_match '{match_text}': {e}")
        return False

    if not args:
        print(f"[watchdog] {srv['id']}: process_match '{match_text}' parsed to nothing")
        return False

    def _quote(s: str) -> str:
        return f'"{s}"' if " " in s else s

    # Dedupe while preserving order (proc and holder_pid could theoretically
    # be the same PID in a rare edge case) and drop anything falsy.
    seen = set()
    ordered_pids = []
    for pid in pids_in_order:
        if pid and pid not in seen:
            seen.add(pid)
            ordered_pids.append(pid)

    lines = ["@echo off"]
    for pid in ordered_pids:
        lines.append(f"taskkill /F /PID {pid}")
    lines.append("timeout /t 1 /nobreak >nul")
    # "start" needs an explicit "" title argument first, or it can mistake
    # a quoted exe path for the window title instead of the target to run.
    lines.append('start "" ' + " ".join(_quote(a) for a in args))
    lines.append('del "%~f0"')   # self-delete once every step above has run

    try:
        fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="watchdog_relaunch_")
        with os.fdopen(fd, "w") as f:
            f.write("\r\n".join(lines) + "\r\n")
    except OSError as e:
        print(f"[watchdog] {srv['id']}: couldn't write relaunch script: {e}")
        return False

    print(f"[watchdog] {srv['id']}: running {bat_path}:")
    for line in lines[:-1]:
        print(f"    {line}")
    if cwd:
        print(f"  (cwd={cwd})")

    popen_kwargs = {"cwd": cwd, "close_fds": True}
    if os.name == "nt":
        CREATE_NO_WINDOW = 0x08000000             # the orchestrating cmd.exe itself stays invisible
        CREATE_NEW_PROCESS_GROUP = 0x00000200     # our Ctrl+C doesn't reach it
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000    # escape PyCharm's/our launcher's job object
        popen_kwargs["creationflags"] = (
            CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
        )
    else:
        popen_kwargs["start_new_session"] = True   # POSIX equivalent: detach from our session

    try:
        subprocess.Popen(["cmd", "/c", bat_path], **popen_kwargs)
        return True
    except OSError as e:
        print(f"[watchdog] {srv['id']}: failed to run kill/relaunch sequence: {e}")
        try:
            os.remove(bat_path)   # never got the chance to self-delete
        except OSError:
            pass
        return False


def _output_queue_watchdog_loop():
    while True:
        now = time.time()

        for srv in SERVERS:
            sid = srv["id"]
            log_file = srv.get("log_file")
            match_text = srv.get("process_match")

            if not log_file or not match_text:
                if sid not in _watchdog_config_warned:
                    print(
                        f"[watchdog] {sid}: not watched - missing "
                        f"log_file and/or process_match in config"
                    )
                    _watchdog_config_warned.add(sid)
                continue

            cooldown_left = WATCHDOG_COOLDOWN - (now - _watchdog_last_kill.get(sid, 0))
            if cooldown_left > 0:
                # 1111: this used to just "continue" with zero output, which
                # looks IDENTICAL from the outside to the watchdog not
                # reacting to anything at all - including during rapid
                # manual re-testing, where you're likely to land inside your
                # own previous attempt's cooldown window and see nothing
                # happen for reasons that have nothing to do with whether
                # detection itself is working.
                print(
                    f"[watchdog] {sid}: in cooldown for {cooldown_left:.0f}s more after the last kill, skipping this check")
                continue

            new_bytes = _read_new_log_bytes(sid, log_file)

            if not new_bytes:
                continue

            # Port startup failure
            if any(sig in new_bytes for sig in WATCHDOG_PORT_ERROR_SIGNALS):
                port = srv["port"]
                holder_pid = _find_pid_holding_udp_port(port)

                # Safety check: only proceed if whatever's actually squatting
                # the port looks like one of our own server processes. If
                # it's something else entirely, we don't know what it is or
                # whether it's safe to kill - better to leave it alone and
                # skip this cycle than force-kill an unrelated process.
                if holder_pid and not _looks_like_our_game_server(holder_pid):
                    print(
                        f"[watchdog] {sid}: port {port} is held by PID "
                        f"{holder_pid}, which doesn't look like one of our "
                        f"servers - not touching it, skipping this cycle."
                    )
                    continue

                # Found separately from holder_pid so _find_server_process
                # doesn't see two matches for the same process_match (the
                # zombie holding the port is often the same server's own
                # previous instance) and refuse to pick one.
                proc = _find_server_process(match_text, exclude_pid=holder_pid)

                _watchdog_last_kill[sid] = now
                # Close the currently-running (broken) server first, then
                # whatever's actually holding the port, then relaunch -
                # in that order, as one real command-line sequence.
                relaunched = _kill_and_relaunch(
                    srv,
                    [proc.pid if proc else None, holder_pid],
                )

                with _cache_lock:
                    existing = _server_cache.get(sid, {})
                    _server_cache[sid] = {
                        **existing,
                        "stuck": True,
                    }

                    _event_log[sid].append({
                        "ts": int(now),
                        "type": "stuck",
                        "label": (
                            f"Port {port} was in use at startup - cleared and "
                            + ("relaunched" if relaunched else "FAILED TO RELAUNCH - check process_match/db path in config")
                        ),
                    })

                continue

            # Output queue jam
            count = new_bytes.count(WATCHDOG_ERROR_TEXT)

            if count < WATCHDOG_THRESHOLD:
                continue

            proc = _find_server_process(match_text)

            if not proc:
                print(
                    f"[watchdog] {sid}: {count} repeated socket errors "
                    f"detected but no matching process found for "
                    f"'{match_text}'"
                )
                continue

            print(
                f"[watchdog] {sid}: {count} repeated socket errors - "
                f"killing PID {proc.pid}"
            )

            _watchdog_last_kill[sid] = now
            relaunched = _kill_and_relaunch(srv, [proc.pid])

            with _cache_lock:
                existing = _server_cache.get(sid, {})
                _server_cache[sid] = {
                    **existing,
                    "stuck": True,
                }

                _event_log[sid].append({
                    "ts": int(now),
                    "type": "stuck",
                    "label": (
                        f"Output queue stuck ({count} repeated errors) - "
                        + ("process killed and relaunched" if relaunched else "process killed but FAILED TO RELAUNCH - check process_match/db path in config")
                    ),
                })

        time.sleep(WATCHDOG_INTERVAL)


threading.Thread(
    target=_output_queue_watchdog_loop,
    daemon=True,
).start()

# # ─── 333networks public server list ("All Servers" browse tab) ───────────────
# # Same master list the in-game @browse command reads (Core/Query/PlayersBrowse.cpp),
# # fetched here instead so the dashboard can show it too. Refreshed independently
# # of the GameSpy poller above since it's a much larger, slower-moving list and
# # doesn't need 15s freshness.
#
# BROWSE_GAMES = [
#     {"code": "FE", "slug": "serioussam",   "label": "The First Encounter"},
#     {"code": "SE", "slug": "serioussamse", "label": "The Second Encounter"},
# ]
# BROWSE_POLL_INTERVAL = 300  # 5 min - this is a big public list, no need to hammer it
#
# _browse_cache: list = []
# _browse_updated_at: int | None = None
# _browse_last_error: str | None = None
# _browse_lock = threading.Lock()
#
#
# def _own_server_keys() -> set:
#     """(host, port) pairs for servers we manage, so browse entries can be tagged."""
#     return {(s["host"], s["port"]) for s in SERVERS}
#
#
# BROWSE_HEADERS = {
#     # A generic default User-Agent (what requests sends if you don't set one)
#     # gets silently rejected by a lot of APIs, including possibly this one -
#     # identifying the app properly is also just good manners, and 333networks'
#     # own terms ask that use of their data be attributable anyway.
#     "User-Agent": "HyperServerDashboard/1.0 (+https://github.com/Vilkro/ServerUpgrade)",
#     "Accept": "application/json",
# }
#
#
# def _fetch_browse_list() -> tuple[list, str | None]:
#     """Returns (servers, error). error is None on a clean fetch of at least one
#     game; otherwise it's the most recent failure reason, so the frontend can
#     show *why* the list is empty instead of "Loading..." forever."""
#     own_keys = _own_server_keys()
#     servers = []
#     last_error = None
#     any_ok = False
#     for game in BROWSE_GAMES:
#         try:
#             r = requests.get(
#                 f"https://master.333networks.com/json/{game['slug']}",
#                 headers=BROWSE_HEADERS, timeout=8.0,
#             )
#             r.raise_for_status()
#             entries = r.json()
#             any_ok = True
#         except requests.exceptions.Timeout:
#             last_error = f"Timed out contacting 333networks for {game['code']}"
#             continue
#         except requests.exceptions.ConnectionError as exc:
#             last_error = f"Couldn't reach 333networks ({game['code']}): {exc.__class__.__name__} - check this machine's outbound internet access"
#             continue
#         except requests.exceptions.HTTPError as exc:
#             last_error = f"333networks returned {exc.response.status_code if exc.response is not None else '?'} for {game['code']}"
#             continue
#         except ValueError:
#             last_error = f"333networks response for {game['code']} wasn't valid JSON"
#             continue
#         except Exception as exc:
#             last_error = f"{game['code']}: {exc.__class__.__name__}: {exc}"
#             continue
#         for e in entries or []:
#             ip = e.get("ip", "")
#             port = _safe_int(e.get("hostport"), 0)
#             servers.append({
#                 "game":         game["code"],
#                 "game_label":   game["label"],
#                 "hostname":     e.get("hostname", ""),
#                 "mapname":      e.get("mapname", ""),
#                 "ip":           ip,
#                 "port":         port,
#                 "queryport":    _safe_int(e.get("queryport"), port + 1 if port else 0),
#                 "numplayers":   _safe_int(e.get("numplayers"), 0),
#                 "maxplayers":   _safe_int(e.get("maxplayers"), 0),
#                 "is_own":       (ip, port) in own_keys,
#             })
#     servers.sort(key=lambda s: (-s["is_own"], -s["numplayers"], s["hostname"].lower()))
#     return servers, (None if any_ok else last_error)
#
#
# def _browse_poll_loop():
#     global _browse_updated_at, _browse_last_error
#     while True:
#         try:
#             fresh, error = _fetch_browse_list()
#             with _browse_lock:
#                 if fresh or error is None:
#                     # only overwrite the cache on a fetch that actually got data,
#                     # or on a clean "zero servers" result - a transient failure
#                     # shouldn't wipe out the last known-good list
#                     _browse_cache[:] = fresh
#                     _browse_updated_at = int(time.time())
#                 _browse_last_error = error
#                 if error:
#                     print(f"[browse] 333networks fetch failed: {error}")
#         except Exception as exc:
#             with _browse_lock:
#                 _browse_last_error = f"Unexpected error: {exc}"
#             print(f"[browse] 333networks poll loop error: {exc}")
#         time.sleep(BROWSE_POLL_INTERVAL)
#
#
# threading.Thread(target=_browse_poll_loop, daemon=True).start()
#
# # ─── 42amsterdam ("All Servers" 3rd tab) ──────────────────────────────────────
# # 42amsterdam.net runs its OWN separate master server - it predates 333networks
# # and isn't reliably a subset of it, so filtering the 333networks feed by name
# # would be guessing. Their site also disallows automated scraping (robots.txt),
# # so this queries their known dedicated servers directly with the same GameSpy
# # \status\ protocol already used above for your own servers - the same thing
# # any server browser does, just pointed at a fixed list instead of a live master.
# #
# # Fill in the actual host/port pairs for the 42amsterdam servers you want shown.
# # Get these the same way you'd get any server's connect address (in-game server
# # browser, or their site) - left empty here since guessing at IPs would be worse
# # than an honest empty list.
# AMSTERDAM_SERVERS = [
#     # {"host": "1.2.3.4", "port": 25601, "game": "SE"},
# ]
# AMSTERDAM_POLL_INTERVAL = 60  # small fixed list - fine to check this often
#
# _amsterdam_cache: list = []
# _amsterdam_updated_at: int | None = None
# _amsterdam_lock = threading.Lock()
#
#
# def _fetch_amsterdam_list() -> list:
#     servers = []
#     for entry in AMSTERDAM_SERVERS:
#         state = _gamespy_query(entry["host"], entry["port"])
#         if state is None:
#             continue  # offline or unreachable - just omit it, don't fake an entry
#         servers.append({
#             "game":       entry.get("game", "?"),
#             "game_label": {"FE": "The First Encounter", "SE": "The Second Encounter"}.get(entry.get("game"), ""),
#             "hostname":   state.get("hostname", ""),
#             "mapname":    state.get("mapname", ""),
#             "ip":         entry["host"],
#             "port":       entry["port"],
#             "numplayers": state.get("numplayers", 0),
#             "maxplayers": state.get("maxplayers", 0),
#             "is_own":     False,
#         })
#     servers.sort(key=lambda s: (-s["numplayers"], s["hostname"].lower()))
#     return servers
#
#
# def _amsterdam_poll_loop():
#     global _amsterdam_updated_at
#     while True:
#         try:
#             fresh = _fetch_amsterdam_list()
#             with _amsterdam_lock:
#                 _amsterdam_cache[:] = fresh
#                 _amsterdam_updated_at = int(time.time())
#         except Exception:
#             pass
#         time.sleep(AMSTERDAM_POLL_INTERVAL)
#
#
# if AMSTERDAM_SERVERS:
#     threading.Thread(target=_amsterdam_poll_loop, daemon=True).start()

# ─── DB helpers ───────────────────────────────────────────────────────────────

def _db(srv_id: str):
    """Return a sqlite3 connection for the given server id (read-only)."""
    srv = next((s for s in SERVERS if s["id"] == srv_id), None)
    if not srv:
        return None
    path = Path(srv["db"]).resolve()
    if not path.exists():
        return None
    uri = "file:" + quote(str(path).replace("\\", "/"), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


def _unique_db_servers(server_filter: str | None = None, family_filter: str | None = None) -> list:
    """Configured servers whose DB paths exist, deduped by resolved DB file.
    family_filter restricts to one game (tfe/tse) without narrowing to a
    single server - e.g. family_filter="tfe" still returns both fe1 and fe2."""
    result = []
    seen = set()
    for srv in SERVERS:
        if server_filter and srv["id"] != server_filter:
            continue
        if family_filter and srv.get("family") != family_filter:
            continue
        path = Path(srv["db"]).resolve()
        key = str(path).casefold()
        if key in seen or not path.exists():
            continue
        seen.add(key)
        result.append(srv)
    return result


def _db_any():
    """Return a connection to the first available DB (used for cross-server player queries)."""
    for srv in SERVERS:
        conn = _db(srv["id"])
        if conn:
            return conn
    return None


def _table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1;",
        (table_name,),
    ).fetchone()
    return row is not None


def _player_lookup(srv_id: str) -> dict:
    conn = _db(srv_id)
    if not conn:
        return {}
    try:
        rows = conn.execute("""
            SELECT guid, name, country, last_ip, last_seen, sessions, playtime
            FROM players
        """).fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            raw   = item.get("name") or ""
            clean = _clean_sam_text(raw)
            # Index by multiple forms so minor markup or encoding differences
            # between PlayerDB_OnJoin and GameSpy player_N still produce a match.
            # setdefault keeps the first (best) hit per key.
            for key in filter(None, [
                clean.casefold(),   # primary: stripped + lowercased
                raw.casefold(),     # fallback: raw lowercased
                clean,              # fallback: stripped, original case
            ]):
                result.setdefault(key, item)
        return result
    finally:
        conn.close()


def _enrich_live_players(srv_id: str, players: list) -> list:
    lookup = _player_lookup(srv_id)
    enriched = []
    for player in players:
        item = dict(player)
        raw_name = item.get("name") or ""
        name_key = _clean_sam_text(raw_name).casefold()

        # Try all three key forms in order
        match = (lookup.get(name_key)
                 or lookup.get(raw_name.casefold())
                 or lookup.get(_clean_sam_text(raw_name)))

        if match:
            item["guid"]          = match.get("guid")
            item["last_seen"]     = match.get("last_seen")
            item["total_sessions"] = match.get("sessions", 0)
            item["total_playtime"] = match.get("playtime", 0)

            # Country priority:
            #   1. GameSpy country_N field (only if ClassicsPatch exports it)
            #   2. DB players.country (written by C++ GeoIP a few seconds after join)
            #   3. Persistent name cache (survives reconnects and brief DB gaps)
            #   4. Flask-side GeoIP using last known IP (async; ready next cycle)
            country = (item.get("country")
                       or match.get("country")
                       or _country_cache.get(name_key, ""))

            if not country:
                last_ip = match.get("last_ip") or ""
                country = _geoip_ensure_resolved(last_ip)

            item["country"] = country

        else:
            # No DB match yet (player just joined or name mismatch).
            # Still check the name cache from a previous successful hit.
            item["country"] = item.get("country") or _country_cache.get(name_key, "")

        # Write back to name cache whenever we have a definitive country so it
        # persists across polls and reconnects.
        if item.get("country") and name_key:
            _country_cache[name_key] = item["country"]

        enriched.append(item)
    return enriched


def _server_db_stats(srv_id: str, since: int | None = None) -> dict:
    since = since or int(time.time()) - ACTIVITY_WINDOW
    conn = _db(srv_id)
    if not conn:
        return {
            "sessions_today": 0,
            "total_players_today": 0,
            "active_maps_count": 0,
            "playtime_today": 0,
        }
    srv = next((s for s in SERVERS if s["id"] == srv_id), None)
    port = _safe_int(srv.get("port")) if srv else 0
    try:
        # server_id = 0 covers rows written before the migration (unknown
        # server) - only worth including here if this server's own DB has
        # no rows tagged with its real port yet (fresh migration window).
        row = conn.execute("""
            SELECT
                COUNT(*) AS sessions_today,
                COUNT(DISTINCT guid) AS total_players_today,
                COUNT(DISTINCT map) AS active_maps_count,
                COALESCE(SUM(CASE WHEN ended >= started THEN ended - started ELSE 0 END), 0) AS playtime_today
            FROM sessions
            WHERE (started >= ? OR ended >= ?) AND server_id = ?
        """, (since, since, port)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _demo_parse_name(path: Path, stat) -> tuple[int, int | None, str]:
    """Returns (recorded_at, ended_at, map_name).

    ended_at is None if the filename doesn't have a real embedded stop time
    (i.e. an old recording made before DemoManager.cpp started writing one,
    or a custom-named demo that was never auto-renamed on stop) - callers
    should fall back to something reasonable in that case rather than
    trusting file mtime, which isn't a reliable stand-in (buffered writes on
    some setups mean mtime lands right next to ctime, not at actual stop time)."""
    match = DEMO_NAME_RE.match(path.name)
    if not match:
        # Doesn't match the naming convention at all (custom name via
        # StartDemoRec with an explicit name) - nothing to parse from it.
        return int(stat.st_mtime), None, path.stem

    start_date, start_time, end_date, end_time, map_part = match.groups()
    try:
        recorded_at = int(time.mktime(datetime.strptime(start_date + start_time, "%Y%m%d%H%M%S").timetuple()))
    except ValueError:
        recorded_at = int(stat.st_mtime)

    ended_at = None
    if end_date and end_time:
        try:
            ended_at = int(time.mktime(datetime.strptime(end_date + end_time, "%Y%m%d%H%M%S").timetuple()))
        except ValueError:
            ended_at = None

    return recorded_at, ended_at, map_part.replace("_", " ")


def _demo_files(srv: dict, limit: int | None = None) -> list:
    demos_dir = Path(srv.get("demos_dir", "../Demos")).resolve()
    if not demos_dir.exists():
        return []

    result = []
    files = sorted(demos_dir.glob("*.dem"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        stat = f.stat()
        if stat.st_size < MIN_DEMO_SIZE_BYTES:   # skip empty/incomplete recordings
            continue
        if limit is not None and len(result) >= limit:
            break
        recorded_at, ended_at, map_name = _demo_parse_name(f, stat)
        if ended_at is None:
            # No real stop time on this file (recorded before the
            # DemoManager.cpp fix, or a custom name) - best-effort estimate
            # rather than a hard guarantee. Clamped both directions so it
            # can't read as negative or absurdly long.
            ended_at = min(max(int(stat.st_mtime), recorded_at), recorded_at + DEMO_MAX_PLAUSIBLE_SECONDS)
        result.append({
            "server_id":  srv["id"],
            "filename":   f.name,
            "map":        map_name,
            "title":      _display_map_name(map_name),
            "size_bytes": stat.st_size,
            "recorded_at": recorded_at,
            "ended_at":   ended_at,
        })
    return result


def _demo_session_rows(srv_id: str, started_at: int, ended_at: int) -> list:
    conn = _db(srv_id)
    if not conn:
        return []
    try:
        rows = conn.execute("""
            SELECT
                s.guid,
                s.name,
                s.map,
                s.started,
                s.ended,
                COALESCE(p.country, '') AS country
            FROM sessions s
            LEFT JOIN players p ON p.guid = s.guid
            WHERE s.started < ? AND s.ended > ?
            ORDER BY s.started ASC
        """, (ended_at, started_at)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _overlap_seconds(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def _infer_demo_part_maps(srv_id: str, parts: list) -> list:
    if not parts:
        return parts

    started_at = min(p["recorded_at"] for p in parts)
    ended_at = max(p["ended_at"] for p in parts)
    sessions = _demo_session_rows(srv_id, started_at, ended_at)

    for part in parts:
        part["map_raw"] = part.get("map", "")
        weights = {}
        for sess in sessions:
            overlap = _overlap_seconds(
                part["recorded_at"], part["ended_at"],
                _safe_int(sess.get("started")), _safe_int(sess.get("ended")),
            )
            if overlap <= 0:
                continue
            raw_map = sess.get("map") or part.get("map") or ""
            weights[raw_map] = weights.get(raw_map, 0) + overlap
        if weights:
            part["map_raw"] = max(weights.items(), key=lambda item: item[1])[0]
            part["map"] = _display_map_name(part["map_raw"])
            part["title"] = part["map"]
    return parts


def _demo_player_rows(srv_id: str, parts: list) -> list:
    if not parts:
        return []

    started_at = min(p["recorded_at"] for p in parts)
    ended_at = max(p["ended_at"] for p in parts)
    sessions = _demo_session_rows(srv_id, started_at, ended_at)
    players = {}

    for sess in sessions:
        key = sess.get("guid") or _clean_sam_text(sess.get("name")).casefold()
        if not key:
            continue

        item = players.setdefault(key, {
            "guid": sess.get("guid", ""),
            "name": sess.get("name", ""),
            "country": sess.get("country", ""),
            "join_time": None,
            "leave_time": None,
            "playtime": 0,
            "parts": [False for _ in parts],
        })
        if not item.get("country") and sess.get("country"):
            item["country"] = sess.get("country")
        if sess.get("name"):
            item["name"] = sess.get("name")

        sess_start = _safe_int(sess.get("started"))
        sess_end = _safe_int(sess.get("ended"))
        clipped_start = max(started_at, sess_start)
        clipped_end = min(ended_at, sess_end)

        item["join_time"] = clipped_start if item["join_time"] is None else min(item["join_time"], clipped_start)
        item["leave_time"] = clipped_end if item["leave_time"] is None else max(item["leave_time"], clipped_end)

        for index, part in enumerate(parts):
            overlap = _overlap_seconds(part["recorded_at"], part["ended_at"], sess_start, sess_end)
            if overlap > 0:
                item["parts"][index] = True
                item["playtime"] += overlap

    result = []
    for item in players.values():
        active_parts = [i + 1 for i, present in enumerate(item["parts"]) if present]
        item["first_part"] = active_parts[0] if active_parts else None
        item["last_part"] = active_parts[-1] if active_parts else None
        result.append(item)

    result.sort(key=lambda p: (p.get("join_time") or 0, -p.get("playtime", 0), _clean_sam_text(p.get("name"))))
    return result


def _demo_game_from_parts(srv: dict, parts: list, include_players: bool) -> dict:
    parts = [dict(part) for part in parts]
    for index, part in enumerate(parts, 1):
        part["part"] = index

    started_at = min(p["recorded_at"] for p in parts)
    ended_at = max(p["ended_at"] for p in parts)
    map_weights = {}
    for part in parts:
        raw_map = part.get("map_raw") or part.get("map") or ""
        map_weights[raw_map] = map_weights.get(raw_map, 0) + 1
    raw_map = max(map_weights.items(), key=lambda item: item[1])[0] if map_weights else ""
    players = _demo_player_rows(srv["id"], parts) if include_players else []

    return {
        "id": f"{srv['id']}-{started_at}-{len(parts)}",
        "server_id": srv["id"],
        "server_label": srv["label"],
        "map": raw_map,
        "title": _display_map_name(raw_map) or (parts[0].get("title") or parts[0].get("map") or "Recorded game"),
        "recorded_at": started_at,
        "ended_at": ended_at,
        "duration": max(0, ended_at - started_at),
        "parts_count": len(parts),
        "players_count": len(players),
        "size_bytes": sum(_safe_int(p.get("size_bytes")) for p in parts),
        "parts": parts,
        "players": players,
    }


def _demo_games(srv: dict, limit: int | None = None, include_players: bool = True) -> list:
    parts = _demo_files(srv)
    if include_players:
        parts = _infer_demo_part_maps(srv["id"], parts)

    parts.sort(key=lambda p: p["recorded_at"])
    games = []
    current = []

    def flush_current():
        if current:
            games.append(_demo_game_from_parts(srv, current, include_players))

    for part in parts:
        raw_map = (part.get("map_raw") or part.get("map") or "").casefold()
        if current:
            previous = current[-1]
            previous_map = (previous.get("map_raw") or previous.get("map") or "").casefold()
            gap = part["recorded_at"] - previous["recorded_at"]
            if raw_map != previous_map or gap > DEMO_GROUP_GAP_SECONDS:
                flush_current()
                current = []
        current.append(part)

    flush_current()
    games.sort(key=lambda g: g["recorded_at"], reverse=True)
    return games[:limit] if limit else games


def _map_summaries(srv: dict, since: int, limit: int) -> list:
    conn = _db(srv["id"])
    if not conn:
        return []
    try:
        rows = conn.execute("""
            SELECT
                map,
                COUNT(*) AS sessions,
                COUNT(DISTINCT guid) AS players,
                COALESCE(SUM(CASE WHEN ended >= started THEN ended - started ELSE 0 END), 0) AS playtime,
                COALESCE(SUM(frags), 0) AS frags,
                COALESCE(SUM(deaths), 0) AS deaths,
                MIN(started) AS first_seen,
                MAX(ended) AS last_seen
            FROM sessions
            WHERE (started >= ? OR ended >= ?) AND server_id = ?
            GROUP BY map
            ORDER BY last_seen DESC
            LIMIT ?
        """, (since, since, _safe_int(srv.get("port")), limit)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["server_id"] = srv["id"]
            item["server_label"] = srv["label"]
            item["title"] = _display_map_name(item.get("map"))
            result.append(item)
        return result
    finally:
        conn.close()


def _marks_snapshot(server_filter: str | None, family_filter: str | None, selected_map: str | None,
                    limit: int, recent_limit: int) -> dict:
    known_marks = set()
    known_by_map = {}
    player_marks = {}
    player_map_marks = {}
    player_names = {}
    recent = []
    map_latest = {}

    for srv in _unique_db_servers(server_filter, family_filter):
        conn = _db(srv["id"])
        if not conn:
            continue
        # When a specific server is selected, only rows actually written by
        # that server's own process count - not siblings sharing its DB file.
        target_port = _safe_int(srv.get("port")) if server_filter else None
        try:
            if not _table_exists(conn, "marks_collected"):
                continue

            rows = conn.execute("""
                SELECT
                    mc.guid,
                    COALESCE(p.name, mc.guid) AS name,
                    mc.mark,
                    COALESCE(mc.map, '') AS map,
                    mc.collected_at,
                    mc.server_id
                FROM marks_collected mc
                LEFT JOIN players p ON p.guid = mc.guid
            """).fetchall()
        finally:
            conn.close()

        for row in rows:
            if target_port is not None and _safe_int(row["server_id"]) != target_port:
                continue

            guid = row["guid"] or ""
            mark = row["mark"] or ""
            map_name = row["map"] or ""
            if not guid or not mark:
                continue

            # Keyed by (map, mark), not mark alone - two different maps can
            # both have a mark named e.g. "Secret" without being the same
            # collectible. known_by_map/player_map_marks were already scoped
            # per-map so they were unaffected; only these two needed fixing.
            mark_key = (map_name, mark)
            known_marks.add(mark_key)
            known_by_map.setdefault(map_name, set()).add(mark)
            player_marks.setdefault(guid, set()).add(mark_key)
            player_map_marks.setdefault((guid, map_name), set()).add(mark)

            if row["name"]:
                player_names[guid] = row["name"]

            row_srv = _server_for_row(row["server_id"], srv)
            ts = _safe_int(row["collected_at"])
            map_latest[map_name] = max(map_latest.get(map_name, 0), ts)
            recent.append({
                "server_id": row_srv["id"],
                "server_label": row_srv.get("label", ""),
                "guid": guid,
                "name": row["name"] or guid,
                "mark": mark,
                "map": map_name,
                "map_title": _display_map_name(map_name),
                "collected_at": ts,
            })

    maps = []
    for map_name, marks in known_by_map.items():
        maps.append({
            "map": map_name,
            "title": _display_map_name(map_name),
            "known_marks": len(marks),
            "last_seen": map_latest.get(map_name, 0),
        })
    maps.sort(key=lambda m: (-m["known_marks"], m["title"].casefold(), m["map"].casefold()))

    if not selected_map and maps:
        selected_map = maps[0]["map"]
    selected_known = len(known_by_map.get(selected_map or "", set()))

    overall = []
    for guid, marks in player_marks.items():
        overall.append({
            "guid": guid,
            "name": player_names.get(guid, guid),
            "marks_found": len(marks),
            "known_marks": len(known_marks),
            "completion": round((len(marks) / len(known_marks)) * 100, 1) if known_marks else 0,
        })
    overall.sort(key=lambda p: (-p["marks_found"], _clean_sam_text(p["name"]).casefold()))

    by_map = []
    if selected_map is not None:
        for (guid, map_name), marks in player_map_marks.items():
            if map_name != selected_map:
                continue
            by_map.append({
                "guid": guid,
                "name": player_names.get(guid, guid),
                "map": map_name,
                "map_title": _display_map_name(map_name),
                "marks_found": len(marks),
                "known_marks": selected_known,
                "completion": round((len(marks) / selected_known) * 100, 1) if selected_known else 0,
            })
    by_map.sort(key=lambda p: (-p["marks_found"], _clean_sam_text(p["name"]).casefold()))

    recent.sort(key=lambda r: r["collected_at"], reverse=True)

    return {
        "known_marks": len(known_marks),
        "known_maps": len(maps),
        "selected_map": selected_map or "",
        "selected_map_known_marks": selected_known,
        "maps": maps,
        "overall": overall[:limit],
        "by_map": by_map[:limit],
        "recent": recent[:recent_limit],
    }


FRAGMATCH_GAME_GROUP_OVERLAP = 30   # seconds of overlap required to treat two players' sessions as "played together"
FRAGMATCH_LEADERS_MAX = 50          # hard cap regardless of ?limit=

def _group_fragmatch_games(rows: list) -> list:
    """Merges per-player fragmatch session rows into 'games': rows on the same
    server + map whose [started, ended] windows overlap become one entry with
    a list of players, instead of one duplicate-looking row per player.
    Same sorted-interval-merge approach as _infer_demo_part_maps/_overlap_seconds
    use for grouping demo parts."""
    buckets: dict = {}
    for r in rows:
        buckets.setdefault((r["server_id"], r["map"]), []).append(r)

    games = []
    for _key, bucket in buckets.items():
        bucket.sort(key=lambda r: r["started"])
        current = None
        for r in bucket:
            if current is not None and _overlap_seconds(
                current["started"], current["ended"], r["started"], r["ended"]
            ) >= FRAGMATCH_GAME_GROUP_OVERLAP:
                current["started"] = min(current["started"], r["started"])
                current["ended"] = max(current["ended"], r["ended"])
                current["rows"].append(r)
            else:
                if current is not None:
                    games.append(current)
                current = {"started": r["started"], "ended": r["ended"], "rows": [r]}
        if current is not None:
            games.append(current)

    out = []
    for g in games:
        rows_in_game = sorted(g["rows"], key=lambda r: -r["frags"])
        first = rows_in_game[0]
        out.append({
            "server_id": first["server_id"],
            "server_label": first["server_label"],
            "map": first["map"],
            "map_title": first["map_title"],
            "started": g["started"],
            "ended": g["ended"],
            "duration": max(0, g["ended"] - g["started"]),
            "players": [
                {
                    "guid": r["guid"], "name": r["name"],
                    "frags": r["frags"], "deaths": r["deaths"], "score": r["score"],
                }
                for r in rows_in_game
            ],
        })
    out.sort(key=lambda g: g["ended"], reverse=True)
    return out


def _fragmatch_snapshot(server_filter: str | None, family_filter: str | None, limit: int, recent_limit: int) -> dict:
    players = {}
    session_rows = []
    totals = {"sessions": 0, "frags": 0, "deaths": 0, "playtime": 0}

    for srv in _unique_db_servers(server_filter, family_filter):
        conn = _db(srv["id"])
        if not conn:
            continue
        target_port = _safe_int(srv.get("port")) if server_filter else None
        try:
            if not _table_exists(conn, "fragmatch_sessions"):
                continue

            rows = conn.execute("""
                SELECT
                    f.guid,
                    COALESCE(p.name, f.name, f.guid) AS name,
                    COALESCE(p.country, '') AS country,
                    f.map,
                    f.frags,
                    f.deaths,
                    f.score,
                    f.started,
                    f.ended,
                    f.server_id,
                    COALESCE(f.duration, CASE WHEN f.ended >= f.started THEN f.ended - f.started ELSE 0 END) AS duration
                FROM fragmatch_sessions f
                LEFT JOIN players p ON p.guid = f.guid
            """).fetchall()
        finally:
            conn.close()

        for row in rows:
            if target_port is not None and _safe_int(row["server_id"]) != target_port:
                continue

            guid = row["guid"] or ""
            if not guid:
                continue
            frags = _safe_int(row["frags"])
            deaths = _safe_int(row["deaths"])
            score = _safe_int(row["score"])
            duration = max(0, _safe_int(row["duration"]))
            ended = _safe_int(row["ended"])
            item = players.setdefault(guid, {
                "guid": guid,
                "name": row["name"] or guid,
                "country": row["country"] or "",
                "sessions": 0,
                "maps": set(),
                "frags": 0,
                "deaths": 0,
                "score": 0,
                "playtime": 0,
                "last_seen": 0,
            })
            item["name"] = row["name"] or item["name"]
            item["country"] = row["country"] or item["country"]
            item["sessions"] += 1
            item["maps"].add(row["map"] or "")
            item["frags"] += frags
            item["deaths"] += deaths
            item["score"] += score
            item["playtime"] += duration
            item["last_seen"] = max(item["last_seen"], ended)

            totals["sessions"] += 1
            # 1111: a session's frags can go negative (self-kills cost a
            # frag in this scoring), but that shouldn't drag the site-wide
            # total down - only its own leaderboard/K-D standing.
            totals["frags"] += max(0, frags)
            totals["deaths"] += deaths
            totals["playtime"] += duration

            row_srv = _server_for_row(row["server_id"], srv)
            session_rows.append({
                "server_id": row_srv["id"],
                "server_label": row_srv.get("label", ""),
                "guid": guid,
                "name": row["name"] or guid,
                "map": row["map"] or "",
                "map_title": _display_map_name(row["map"]),
                "frags": frags,
                "deaths": deaths,
                "score": score,
                "duration": duration,
                "started": _safe_int(row["started"]),
                "ended": ended,
            })

    leaders = []
    for item in players.values():
        deaths = item["deaths"]
        frags = item["frags"]
        kd = round(frags / deaths, 2) if deaths else float(frags)
        # 1111: a net-negative record (more self-kills than actual frags)
        # doesn't belong on a "leaders" list at all - excluded here rather
        # than floor-clamped, so it can't rank ahead of a genuine 0/0 rookie.
        if frags < 1 or kd < 0:
            continue
        leaders.append({
            **{k: v for k, v in item.items() if k != "maps"},
            "maps": len([m for m in item["maps"] if m]),
            "kd": kd,
        })
    # Sort by K/D ratio (ties broken by frags, then playtime, then name).
    leaders.sort(key=lambda p: (-p["kd"], -p["frags"], -p["playtime"], _clean_sam_text(p["name"]).casefold()))

    games = _group_fragmatch_games(session_rows)

    return {
        "totals": totals,
        "leaders": leaders[:min(limit, FRAGMATCH_LEADERS_MAX)],
        "recent": games[:recent_limit],
    }

# ─── API routes ───────────────────────────────────────────────────────────────

# @app.route("/api/browse")
# def get_browse():
#     """Public FE/SE server list from master.333networks.com - the same list
#     the in-game @browse command reads. Refreshed every few minutes in the
#     background; this just serves the cache."""
#     with _browse_lock:
#         return jsonify({
#             "updated_at": _browse_updated_at,
#             "servers":    list(_browse_cache),
#             "error":      _browse_last_error,
#         })
#
#
# @app.route("/api/browse-42amsterdam")
# def get_browse_amsterdam():
#     """42amsterdam servers, queried directly (see AMSTERDAM_SERVERS above -
#     empty by default until real host/port entries are added there)."""
#     with _amsterdam_lock:
#         return jsonify({
#             "updated_at": _amsterdam_updated_at,
#             "servers":    list(_amsterdam_cache),
#         })


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "servers": len(SERVERS)})


@app.route("/api/debug/players/<srv_id>")
def debug_players(srv_id: str):
    """Diagnostic: shows GameSpy names vs DB names and whether country resolves.
    Hit /api/debug/players/srv1 while players are online to diagnose issues."""
    gs_players = _server_cache.get(srv_id, {}).get("players", [])
    lookup     = _player_lookup(srv_id)
    rows = []
    for p in gs_players:
        raw      = p.get("name") or ""
        clean    = _clean_sam_text(raw)
        name_key = clean.casefold()
        match    = (lookup.get(name_key)
                    or lookup.get(raw.casefold())
                    or lookup.get(clean))
        rows.append({
            "gamespy_raw":   raw,
            "gamespy_clean": clean,
            "gamespy_key":   name_key,
            "db_matched":    match is not None,
            "db_name":       match.get("name")    if match else None,
            "db_country":    match.get("country") if match else None,
            "db_last_ip":    match.get("last_ip") if match else None,
            "ip_cache":      _ip_country_cache.get(match.get("last_ip", "")) if match else None,
            "name_cache":    _country_cache.get(name_key),
        })
    db_names = [
        {
            "db_raw":    v.get("name"),
            "db_key":    _clean_sam_text(v.get("name") or "").casefold(),
            "country":   v.get("country"),
            "last_ip":   v.get("last_ip"),
        }
        for v in {id(v): v for v in lookup.values()}.values()
    ]
    return jsonify({"online": rows, "db_players": db_names})


def _global_unique_players_today(since: int) -> int:
    """True count of distinct players across ALL servers in the given
    window - NOT the sum of each server's own COUNT(DISTINCT guid). Summing
    per-server counts double-counts anyone who played on more than one
    server today (e.g. both fe1 and fe2), since the same guid is legitimately
    "distinct" within each server's own count but isn't a second unique
    player site-wide.    1111
    Unions actual guid sets across the (at most a couple) unique DB files
    rather than adding up counts, so it's correct even in the unlikely case
    a guid string were ever shared across two different DBs."""
    guids = set()
    for srv in _unique_db_servers():
        conn = _db(srv["id"])
        if not conn:
            continue
        try:
            rows = conn.execute(
                "SELECT DISTINCT guid FROM sessions WHERE started >= ? OR ended >= ?",
                (since, since),
            ).fetchall()
            guids.update(r["guid"] for r in rows if r["guid"])
        finally:
            conn.close()
    return len(guids)


@app.route("/api/unique-today")
def get_unique_today():
    since = int(time.time()) - ACTIVITY_WINDOW
    return jsonify({"count": _global_unique_players_today(since)})



@app.route("/api/servers")
def get_servers():
    """Live state of all servers, merged with static config labels.
    Players are enriched with country in the poll loop; this just reads the cache."""
    snapshots = []
    now = int(time.time())
    since = now - ACTIVITY_WINDOW

    with _cache_lock:
        for srv in SERVERS:
            sid = srv["id"]
            live = dict(_server_cache.get(sid, {
                "online": False,
                "queried_at": None,
                "numplayers": 0,
                "maxplayers": 8,
                "players": [],
            }))
            live["players"] = [dict(p) for p in live.get("players", [])]
            events = list(_event_log.get(sid, []))
            snapshots.append((srv, live, events))

    # DB stats, demo counts, etc. are read outside the lock
    result = []
    for srv, live, events in snapshots:
        sid = srv["id"]
        stats      = _server_db_stats(sid, since)
        demos      = _demo_files(srv)
        demo_games = _demo_games(srv, include_players=False)
        crash_count = sum(1 for e in events if e.get("type") == "crash" and e.get("ts", 0) >= since)
        result.append({
            "id":             sid,
            "label":          srv["label"],
            "host":           srv["host"],
            "port":           srv["port"],
            "demo_count":     len(demos),
            "demo_game_count": len(demo_games),
            "crash_count":    crash_count,
            "peak_players":   _peak_players(sid),
            **stats,
            **live,
        })
    return jsonify(result)


@app.route("/api/activity")
def get_activity():
    """
    24h activity timeline, bucketed into ACTIVITY_BUCKET-second intervals.
    Returns:  { server_id: [ [unix_ts, avg_player_count], ... ] }
    """
    now = int(time.time())
    cutoff = now - ACTIVITY_WINDOW
    result = {}

    with _cache_lock:
        for srv in SERVERS:
            sid = srv["id"]
            log = _activity_log.get(sid, [])

            buckets = {}
            for ts, count in log:
                if ts < cutoff:
                    continue
                bucket_ts = (ts // ACTIVITY_BUCKET) * ACTIVITY_BUCKET
                if bucket_ts not in buckets:
                    buckets[bucket_ts] = []
                buckets[bucket_ts].append(count)

            series = []
            for bucket_ts in sorted(buckets):
                vals = buckets[bucket_ts]
                # 1111: was round(..., 1) - a decimal place on a player COUNT
                # doesn't mean anything (there's no such thing as 3.5 people
                # online), and since the frontend sums these across servers
                # for the combined graph, fractional per-server values were
                # compounding into fractional combined totals too.
                series.append([bucket_ts, round(sum(vals) / len(vals))])

            result[sid] = series

    return jsonify(result)


@app.route("/api/events")
def get_events():
    """In-memory 24h map/offline events inferred by the poller."""
    now = int(time.time())
    cutoff = now - ACTIVITY_WINDOW
    result = {}
    with _cache_lock:
        for srv in SERVERS:
            sid = srv["id"]
            result[sid] = [e for e in _event_log.get(sid, []) if e.get("ts", 0) >= cutoff]
    return jsonify(result)


@app.route("/api/maps")
def get_maps():
    """
    Recent map summary from PlayerStats.db.
    Optional query params:
      ?server=srv1
      ?since=<unix_ts>      default: last 24h
      ?limit=100            default: 100, max: 500
    """
    server_filter = request.args.get("server")
    since = _safe_int(request.args.get("since"), int(time.time()) - ACTIVITY_WINDOW)
    limit = min(_safe_int(request.args.get("limit"), 100), 500)

    result = []
    for srv in SERVERS:
        if server_filter and srv["id"] != server_filter:
            continue
        result.extend(_map_summaries(srv, since, limit))
    return jsonify(result[:limit])


@app.route("/api/marks")
def get_marks():
    """
    Raiders of Marks statistics from marks_collected.
    Known marks are discovered organically from distinct marks_collected.mark
    values; there is no fixed total or separate marks catalog.
    Optional query params:
      ?server=srv1
      ?family=tfe|tse   scope to one game - guids aren't comparable across
                        games, so mixing them can rank unrelated players
                        together even though names never actually collide
                        (marks are now keyed by map+name, not name alone)
      ?map=<raw map path>
      ?limit=50
      ?recent=20
    """
    server_filter = request.args.get("server")
    family_filter = request.args.get("family") or None
    selected_map = request.args.get("map")
    limit = min(_safe_int(request.args.get("limit"), 50), 500)
    recent_limit = min(_safe_int(request.args.get("recent"), 20), 100)
    return jsonify(_marks_snapshot(server_filter, family_filter, selected_map, limit, recent_limit))


@app.route("/api/fragmatch")
def get_fragmatch():
    """
    Fragmatch statistics from fragmatch_sessions.
    The dashboard only reads this table; it is written by PlayerDB on server
    disconnect for sessions that were running gam_iStartMode == GM_FRAGMATCH.
    "leaders" is sorted by K/D ratio and hard-capped at 50 regardless of
    ?limit=. "recent" entries are one per *game* (players who overlapped in
    time on the same server+map are merged into one entry's "players" list),
    not one per player-session row.
    Optional query params:
      ?server=srv1
      ?family=tfe|tse   scope to one game, same reasoning as /api/marks
      ?limit=50    (leaders; capped at 50 either way)
      ?recent=10
    """
    server_filter = request.args.get("server")
    family_filter = request.args.get("family") or None
    limit = min(_safe_int(request.args.get("limit"), 50), 500)
    recent_limit = min(_safe_int(request.args.get("recent"), 10), 100)
    return jsonify(_fragmatch_snapshot(server_filter, family_filter, limit, recent_limit))


@app.route("/api/players")
def get_players():
    """
    Aggregated player stats across all servers, sorted by playtime desc.
    Optional query params:
      ?server=srv1          filter to one server's DB
      ?since=<unix_ts>      only sessions after this timestamp (default: last 24h)
      ?limit=100            max rows (default 100)
    """
    server_filter = request.args.get("server")
    since = int(request.args.get("since", int(time.time()) - 86400))
    limit = min(int(request.args.get("limit", 100)), 500)

    servers_to_query = [s for s in SERVERS if not server_filter or s["id"] == server_filter]
    all_players = {}

    for srv in servers_to_query:
        conn = _db(srv["id"])
        if not conn:
            continue
        try:
            rows = conn.execute("""
                SELECT
                    p.guid,
                    p.name,
                    p.country,
                    p.last_seen,
                    p.sessions       AS total_sessions,
                    p.playtime       AS total_playtime,
                    COUNT(s.id)      AS sessions_24h,
                    COALESCE(SUM(s.ended - s.started), 0) AS playtime_24h,
                    COALESCE(SUM(s.frags), 0)             AS frags_24h,
                    COALESCE(SUM(s.deaths), 0)            AS deaths_24h
                FROM players p
                LEFT JOIN sessions s ON s.guid = p.guid AND s.started >= ?
                GROUP BY p.guid
                ORDER BY playtime_24h DESC, total_playtime DESC
                LIMIT ?
            """, (since, limit)).fetchall()

            for row in rows:
                guid = row["guid"]
                if guid not in all_players:
                    all_players[guid] = dict(row)
                    all_players[guid]["servers"] = [srv["id"]]
                else:
                    all_players[guid]["sessions_24h"]  += row["sessions_24h"]
                    all_players[guid]["playtime_24h"]  += row["playtime_24h"]
                    all_players[guid]["frags_24h"]     += row["frags_24h"]
                    all_players[guid]["deaths_24h"]    += row["deaths_24h"]
                    all_players[guid]["servers"].append(srv["id"])
        finally:
            conn.close()

    players = sorted(all_players.values(),
                     key=lambda p: p["playtime_24h"], reverse=True)
    return jsonify(players[:limit])


@app.route("/api/players/<guid>")
def get_player(guid: str):
    """Full player profile + last 50 sessions."""
    since = int(request.args.get("since", int(time.time()) - 86400))

    profile = None
    sessions = []

    for srv in SERVERS:
        conn = _db(srv["id"])
        if not conn:
            continue
        try:
            row = conn.execute(
                "SELECT * FROM players WHERE guid = ?", (guid,)
            ).fetchone()
            if row and not profile:
                profile = dict(row)

            sess_rows = conn.execute("""
                SELECT *, ? AS server_id
                FROM sessions
                WHERE guid = ?
                ORDER BY started DESC
                LIMIT 50
            """, (srv["id"], guid)).fetchall()
            sessions.extend([dict(r) for r in sess_rows])
        finally:
            conn.close()

    if not profile:
        abort(404, description="Player not found")

    sessions.sort(key=lambda s: s["started"], reverse=True)
    return jsonify({"player": profile, "sessions": sessions[:50]})


@app.route("/api/demos")
def list_demos():
    """List demo files across all servers."""
    limit = request.args.get("limit")
    limit = min(_safe_int(limit), 1000) if limit else None
    server_filter = request.args.get("server")
    result = []

    for srv in SERVERS:
        if server_filter and srv["id"] != server_filter:
            continue
        result.extend(_demo_files(srv, limit))

    return jsonify(result)


@app.route("/api/demo-games")
def list_demo_games():
    """List recorded demo runs grouped from rotating demo parts."""
    limit = request.args.get("limit")
    limit = min(_safe_int(limit), 500) if limit else None
    server_filter = request.args.get("server")
    result = []

    for srv in SERVERS:
        if server_filter and srv["id"] != server_filter:
            continue
        result.extend(_demo_games(srv, limit=limit, include_players=True))

    result.sort(key=lambda g: g["recorded_at"], reverse=True)
    return jsonify(result[:limit] if limit else result)


@app.route("/api/demos/<server_id>/<filename>")
def download_demo(server_id: str, filename: str):
    """Download a single demo part file."""
    srv = next((s for s in SERVERS if s["id"] == server_id), None)
    if not srv:
        abort(404)
    demos_dir = Path(srv.get("demos_dir", "../Demos")).resolve()
    if not filename.endswith(".dem") or "/" in filename or "\\" in filename:
        abort(400)
    return send_from_directory(demos_dir, filename, as_attachment=True)


@app.route("/api/demos/<server_id>/zip")
def download_demo_zip(server_id: str):
    """
    Stream multiple demo parts as a single zip archive.
    Query param:  ?parts=file1.dem,file2.dem,...
    """
    srv = next((s for s in SERVERS if s["id"] == server_id), None)
    if not srv:
        abort(404)

    parts_param = request.args.get("parts", "")
    filenames = [f.strip() for f in parts_param.split(",") if f.strip()]
    if not filenames:
        abort(400, description="?parts= query param required")

    demos_dir = Path(srv.get("demos_dir", "../Demos")).resolve()

    paths = []
    for fname in filenames:
        if not fname.endswith(".dem") or "/" in fname or "\\" in fname:
            abort(400, description=f"Invalid filename: {fname}")
        p = demos_dir / fname
        if not p.exists() or not p.is_file():
            abort(404, description=f"Not found: {fname}")
        paths.append((fname, p))

    zip_name = Path(paths[0][0]).stem + ".zip" if paths else "demo.zip"

    def generate_zip():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
            for fname, fpath in paths:
                zf.write(fpath, arcname=fname)
        buf.seek(0)
        while True:
            chunk = buf.read(65536)
            if not chunk:
                break
            yield chunk

    headers = {
        "Content-Disposition": f'attachment; filename="{zip_name}"',
        "Content-Type": "application/zip",
    }
    return Response(stream_with_context(generate_zip()), headers=headers)


@app.route("/api/maps/<server_id>/<filename>")
def download_map_pack(server_id: str, filename: str):
    """
    Serve a map pack file for download.
    Supported extensions: .zip  .7z  .rar  .gro
    """
    allowed_ext = (".zip", ".7z", ".rar", ".gro")
    if "/" in filename or "\\" in filename or not any(filename.lower().endswith(e) for e in allowed_ext):
        abort(400)

    srv = next((s for s in SERVERS if s["id"] == server_id), None)
    if not srv:
        abort(404)

    maps_dir_raw = srv.get("maps_dir", "")
    if not maps_dir_raw:
        abort(404)
    maps_dir = Path(maps_dir_raw).resolve()
    if not maps_dir.exists():
        abort(404)

    target = maps_dir / filename
    if not target.exists() or not target.is_file():
        abort(404)

    return send_from_directory(maps_dir, filename, as_attachment=True)


# ─── RCON ─────────────────────────────────────────────────────────────────────

def _rcon_exec(host: str, port: int, password: str, command: str,
               timeout: float = 3.0) -> str:
    """
    Send an RCON command to an SE1 dedicated server.
    Wire format:  "rcon <password> <command>\\n"
    Returns the server's text response, or an error string.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        msg = f"rcon {password} {command}\n"
        s.sendall(msg.encode("latin-1"))
        buf = b""
        while True:
            chunk = s.recv(1024)
            if not chunk:
                break
            buf += chunk
            if buf.endswith(b"\n"):
                break
        s.close()
        return buf.decode("latin-1", errors="replace").strip()
    except Exception as e:
        return f"RCON error: {e}"


def _require_admin(f):
    """Simple admin token check via Authorization: Bearer <token> header."""
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != ADMIN_TOKEN:
            abort(401, description="Admin token required")
        return f(*args, **kwargs)
    return wrapper


@app.route("/api/admin/watchdog-report", methods=["POST"])
@_require_admin
def admin_watchdog_report():
    """Body JSON: { "server": "srv1", "exit_code": 1 }
    Called by watchdog.bat right after it relaunches a server (see the
    DASHBOARD_URL/DASHBOARD_ADMIN_TOKEN fields near the top of that script -
    blank/disabled by default). Purely informational: logs a distinct event
    type so you can tell "watchdog caught a crash and relaunched it" apart
    from the poller's own online/offline detection, which fires independently
    based on whether the server actually answers a query - this just adds
    the "and here's what fixed it" half of the picture."""
    data = request.get_json(force=True) or {}
    sid = data.get("server")
    if not any(s["id"] == sid for s in SERVERS):
        abort(400, description="Unknown server id")

    exit_code = data.get("exit_code")
    with _cache_lock:
        _event_log[sid].append({
            "ts": int(time.time()),
            "type": "watchdog_restart",
            "label": f"Restarted by watchdog (exit code {exit_code})",
        })
    return jsonify({"ok": True})


@app.route("/api/admin/kick", methods=["POST"])
@_require_admin
def admin_kick():
    """Body JSON: { "server": "srv1", "client_slot": 3, "reason": "..." }"""
    data = request.get_json(force=True)
    srv = next((s for s in SERVERS if s["id"] == data.get("server")), None)
    if not srv:
        abort(400, description="Unknown server id")

    slot   = int(data.get("client_slot", -1))
    reason = str(data.get("reason", "Kicked by admin"))

    if slot < 1 or slot > 8:
        abort(400, description="client_slot must be 1–8")

    cmd = f'KickClient({slot}, "{reason}")'
    result = _rcon_exec(srv["host"], srv["port"], srv["rcon_pass"], cmd)
    return jsonify({"result": result})


@app.route("/api/admin/ban", methods=["POST"])
@_require_admin
def admin_ban():
    """Body JSON: { "server": "srv1", "identity_id": 5, "duration_seconds": 3600 }"""
    data = request.get_json(force=True)
    srv = next((s for s in SERVERS if s["id"] == data.get("server")), None)
    if not srv:
        abort(400, description="Unknown server id")

    identity = int(data.get("identity_id", -1))
    duration = int(data.get("duration_seconds", 3600))

    cmd = f"!ban {identity} {duration}"
    result = _rcon_exec(srv["host"], srv["port"], srv["rcon_pass"], cmd)
    return jsonify({"result": result})


@app.route("/api/admin/exec", methods=["POST"])
@_require_admin
def admin_exec():
    """Body JSON: { "server": "srv1", "command": "Say(\\"hello\\")" }"""
    data = request.get_json(force=True)
    srv = next((s for s in SERVERS if s["id"] == data.get("server")), None)
    if not srv:
        abort(400, description="Unknown server id")

    cmd = str(data.get("command", ""))
    if not cmd:
        abort(400, description="Empty command")

    result = _rcon_exec(srv["host"], srv["port"], srv["rcon_pass"], cmd)
    return jsonify({"result": result})


# ─── Static files ─────────────────────────────────────────────────────────────

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_static(path):
    static_dir = Path(__file__).parent.parent / "static"
    target = static_dir / path
    if path and target.exists() and target.is_file():
        return send_from_directory(static_dir, path)
    return send_from_directory(static_dir, "index.html")


if __name__ == "__main__":
    print("SSCP Dashboard starting on http://0.0.0.0:5000")
    print(f"Monitoring {len(SERVERS)} server(s), polling every {POLL_INTERVAL}s")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)