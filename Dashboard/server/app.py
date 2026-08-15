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

import os, io, socket, struct, time, threading, json, sqlite3, re, zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory, abort, Response, stream_with_context
from flask_cors import CORS

# ─── Configuration ────────────────────────────────────────────────────────────

# One entry per server you manage.  Edit these to match your setup.
SERVERS = [
    {
        "id": "srv1",
        "label": "",
        "host": "192.168.10.11",
        "port": 25646,  # net_iPort from init.ini
        "db": "D:/CustomTSE/Bin/PlayerStats.db",  # path to this server's PlayerStats.db
        "demos_dir": "D:/CustomTSE/Demos/server1",  # path to Demos\ folder
        "maps_dir": "D:/CustomTSE/Maps",  # path to folder containing map pack zips for download
        "rcon_pass": "hrtmcftgmkjfgjsrhu",  # net_strAdminPassword from init.ini
    },
    {
        "id":       "srv2",
        "label":    "",
        "host":     "192.168.10.10",
        "port":     25656,             # net_iPort from init.ini
        "db":       "D:/CustomTSE/Bin/PlayerStats.db",     # path to this server's PlayerStats.db
        "demos_dir":"D:/CustomTSE/Demos/server2",              # path to Demos\ folder
        "maps_dir": "D:/CustomTSE/Maps",               # path to folder containing map pack zips for download
        "rcon_pass":"hrtmcftgmkjfgjsrhu",          # net_strAdminPassword from init.ini
    },
    {
        "id":       "srv3",
        "label":    "",
        "host":     "192.168.10.11",
        "port":     25666,             # net_iPort from init.ini
        "db":       "D:/CustomTSE/Bin/PlayerStats.db",     # path to this server's PlayerStats.db
        "demos_dir":"D:/CustomTSE/Demos/rocketjump",              # path to Demos\ folder
        "maps_dir": "D:/CustomTSE/Maps",               # path to folder containing map pack zips for download
        "rcon_pass":"hrtmcftgmkjfgjsrhu",          # net_strAdminPassword from init.ini
    },
    {
        "id":       "srv4",
        "label":    "",
        "host":     "192.168.10.11",
        "port":     25720,             # net_iPort from init.ini
        "db":       "D:/CustomTFE/Bin/PlayerStats.db",     # path to this server's PlayerStats.db
        "demos_dir":"D:/CustomTFE/Demos/rocketjump",              # path to Demos\ folder
        "maps_dir": "D:/CustomTFE/Maps",               # path to folder containing map pack zips for download
        "rcon_pass":"hrtmcftgmkjfgjsrhu",          # net_strAdminPassword from init.ini
    },
    # Add more servers here:
    # { "id": "srv2", "label": "Custom Maps", "host": "127.0.0.1", "port": 25667, ... },
]

POLL_INTERVAL   = 15      # seconds between GameSpy polls
ACTIVITY_WINDOW = 86400   # 24 h of activity history
ACTIVITY_BUCKET = 300     # 5-minute buckets for the graph
ADMIN_TOKEN     = "hrtmcftgmkjfgjsrhu"
MIN_DEMO_SIZE_BYTES = 1024   # demos smaller than this are hidden (incomplete recordings)

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

        chunks = []
        got_any_data = False
        while True:
            try:
                data, _ = s.recvfrom(4096)
                got_any_data = True
                chunks.append(data.decode("cp1251", errors="replace"))
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

        raw = "".join(chunks)
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
                    _server_cache[sid] = {
                        "online":     True,
                        "queried_at": now,
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

# ─── 333networks public server list ("All Servers" browse tab) ───────────────
# Same master list the in-game @browse command reads (Core/Query/PlayersBrowse.cpp),
# fetched here instead so the dashboard can show it too. Refreshed independently
# of the GameSpy poller above since it's a much larger, slower-moving list and
# doesn't need 15s freshness.

BROWSE_GAMES = [
    {"code": "FE", "slug": "serioussam",   "label": "The First Encounter"},
    {"code": "SE", "slug": "serioussamse", "label": "The Second Encounter"},
]
BROWSE_POLL_INTERVAL = 300  # 5 min - this is a big public list, no need to hammer it

_browse_cache: list = []
_browse_updated_at: int | None = None
_browse_last_error: str | None = None
_browse_lock = threading.Lock()


def _own_server_keys() -> set:
    """(host, port) pairs for servers we manage, so browse entries can be tagged."""
    return {(s["host"], s["port"]) for s in SERVERS}


BROWSE_HEADERS = {
    # A generic default User-Agent (what requests sends if you don't set one)
    # gets silently rejected by a lot of APIs, including possibly this one -
    # identifying the app properly is also just good manners, and 333networks'
    # own terms ask that use of their data be attributable anyway.
    "User-Agent": "HyperServerDashboard/1.0 (+https://github.com/Vilkro/ServerUpgrade)",
    "Accept": "application/json",
}


def _fetch_browse_list() -> tuple[list, str | None]:
    """Returns (servers, error). error is None on a clean fetch of at least one
    game; otherwise it's the most recent failure reason, so the frontend can
    show *why* the list is empty instead of "Loading..." forever."""
    own_keys = _own_server_keys()
    servers = []
    last_error = None
    any_ok = False
    for game in BROWSE_GAMES:
        try:
            r = requests.get(
                f"https://master.333networks.com/json/{game['slug']}",
                headers=BROWSE_HEADERS, timeout=8.0,
            )
            r.raise_for_status()
            entries = r.json()
            any_ok = True
        except requests.exceptions.Timeout:
            last_error = f"Timed out contacting 333networks for {game['code']}"
            continue
        except requests.exceptions.ConnectionError as exc:
            last_error = f"Couldn't reach 333networks ({game['code']}): {exc.__class__.__name__} - check this machine's outbound internet access"
            continue
        except requests.exceptions.HTTPError as exc:
            last_error = f"333networks returned {exc.response.status_code if exc.response is not None else '?'} for {game['code']}"
            continue
        except ValueError:
            last_error = f"333networks response for {game['code']} wasn't valid JSON"
            continue
        except Exception as exc:
            last_error = f"{game['code']}: {exc.__class__.__name__}: {exc}"
            continue
        for e in entries or []:
            ip = e.get("ip", "")
            port = _safe_int(e.get("hostport"), 0)
            servers.append({
                "game":         game["code"],
                "game_label":   game["label"],
                "hostname":     e.get("hostname", ""),
                "mapname":      e.get("mapname", ""),
                "ip":           ip,
                "port":         port,
                "queryport":    _safe_int(e.get("queryport"), port + 1 if port else 0),
                "numplayers":   _safe_int(e.get("numplayers"), 0),
                "maxplayers":   _safe_int(e.get("maxplayers"), 0),
                "is_own":       (ip, port) in own_keys,
            })
    servers.sort(key=lambda s: (-s["is_own"], -s["numplayers"], s["hostname"].lower()))
    return servers, (None if any_ok else last_error)


def _browse_poll_loop():
    global _browse_updated_at, _browse_last_error
    while True:
        try:
            fresh, error = _fetch_browse_list()
            with _browse_lock:
                if fresh or error is None:
                    # only overwrite the cache on a fetch that actually got data,
                    # or on a clean "zero servers" result - a transient failure
                    # shouldn't wipe out the last known-good list
                    _browse_cache[:] = fresh
                    _browse_updated_at = int(time.time())
                _browse_last_error = error
                if error:
                    print(f"[browse] 333networks fetch failed: {error}")
        except Exception as exc:
            with _browse_lock:
                _browse_last_error = f"Unexpected error: {exc}"
            print(f"[browse] 333networks poll loop error: {exc}")
        time.sleep(BROWSE_POLL_INTERVAL)


threading.Thread(target=_browse_poll_loop, daemon=True).start()

# ─── 42amsterdam ("All Servers" 3rd tab) ──────────────────────────────────────
# 42amsterdam.net runs its OWN separate master server - it predates 333networks
# and isn't reliably a subset of it, so filtering the 333networks feed by name
# would be guessing. Their site also disallows automated scraping (robots.txt),
# so this queries their known dedicated servers directly with the same GameSpy
# \status\ protocol already used above for your own servers - the same thing
# any server browser does, just pointed at a fixed list instead of a live master.
#
# Fill in the actual host/port pairs for the 42amsterdam servers you want shown.
# Get these the same way you'd get any server's connect address (in-game server
# browser, or their site) - left empty here since guessing at IPs would be worse
# than an honest empty list.
AMSTERDAM_SERVERS = [
    # {"host": "1.2.3.4", "port": 25601, "game": "SE"},
]
AMSTERDAM_POLL_INTERVAL = 60  # small fixed list - fine to check this often

_amsterdam_cache: list = []
_amsterdam_updated_at: int | None = None
_amsterdam_lock = threading.Lock()


def _fetch_amsterdam_list() -> list:
    servers = []
    for entry in AMSTERDAM_SERVERS:
        state = _gamespy_query(entry["host"], entry["port"])
        if state is None:
            continue  # offline or unreachable - just omit it, don't fake an entry
        servers.append({
            "game":       entry.get("game", "?"),
            "game_label": {"FE": "The First Encounter", "SE": "The Second Encounter"}.get(entry.get("game"), ""),
            "hostname":   state.get("hostname", ""),
            "mapname":    state.get("mapname", ""),
            "ip":         entry["host"],
            "port":       entry["port"],
            "numplayers": state.get("numplayers", 0),
            "maxplayers": state.get("maxplayers", 0),
            "is_own":     False,
        })
    servers.sort(key=lambda s: (-s["numplayers"], s["hostname"].lower()))
    return servers


def _amsterdam_poll_loop():
    global _amsterdam_updated_at
    while True:
        try:
            fresh = _fetch_amsterdam_list()
            with _amsterdam_lock:
                _amsterdam_cache[:] = fresh
                _amsterdam_updated_at = int(time.time())
        except Exception:
            pass
        time.sleep(AMSTERDAM_POLL_INTERVAL)


if AMSTERDAM_SERVERS:
    threading.Thread(target=_amsterdam_poll_loop, daemon=True).start()

# ─── DB helpers ───────────────────────────────────────────────────────────────

def _db(srv_id: str):
    """Return a sqlite3 connection for the given server id (read-only)."""
    srv = next((s for s in SERVERS if s["id"] == srv_id), None)
    if not srv:
        return None
    path = Path(srv["db"]).resolve()
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


def _db_any():
    """Return a connection to the first available DB (used for cross-server player queries)."""
    for srv in SERVERS:
        conn = _db(srv["id"])
        if conn:
            return conn
    return None


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
    try:
        row = conn.execute("""
            SELECT
                COUNT(*) AS sessions_today,
                COUNT(DISTINCT guid) AS total_players_today,
                COUNT(DISTINCT map) AS active_maps_count,
                COALESCE(SUM(CASE WHEN ended >= started THEN ended - started ELSE 0 END), 0) AS playtime_today
            FROM sessions
            WHERE started >= ? OR ended >= ?
        """, (since, since)).fetchone()
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
            WHERE started >= ? OR ended >= ?
            GROUP BY map
            ORDER BY last_seen DESC
            LIMIT ?
        """, (since, since, limit)).fetchall()
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

# ─── API routes ───────────────────────────────────────────────────────────────

@app.route("/api/browse")
def get_browse():
    """Public FE/SE server list from master.333networks.com - the same list
    the in-game @browse command reads. Refreshed every few minutes in the
    background; this just serves the cache."""
    with _browse_lock:
        return jsonify({
            "updated_at": _browse_updated_at,
            "servers":    list(_browse_cache),
            "error":      _browse_last_error,
        })


@app.route("/api/browse-42amsterdam")
def get_browse_amsterdam():
    """42amsterdam servers, queried directly (see AMSTERDAM_SERVERS above -
    empty by default until real host/port entries are added there)."""
    with _amsterdam_lock:
        return jsonify({
            "updated_at": _amsterdam_updated_at,
            "servers":    list(_amsterdam_cache),
        })


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
                series.append([bucket_ts, round(sum(vals) / len(vals), 1)])

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