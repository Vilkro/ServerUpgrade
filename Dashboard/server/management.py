"""
management_app.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Separate Flask instance (port 5001) for admin management.
Provides read-WRITE access to PlayerStats.db so the web panel can queue
kick/mute/unmute commands and manage the persistent bans table without RCON.

The C++ side (PlayerDB_ProcessCommands / PlayerDB_CheckBan) picks those up
automatically every game second.

Auth: pass the ADMIN_TOKEN as a Bearer token in the Authorization header,
or as ?token= in the query string. Keep this service behind a firewall or
reverse proxy — never expose port 5001 directly.

Dependencies:  pip install flask flask-cors
Run:           python management_app.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os, socket, time, sqlite3
from flask import Flask, jsonify, request, send_from_directory, abort
from flask_cors import CORS

# ─── Configuration ─────────────────────────────────────────────────────────────
# Mirror your app.py SERVERS list here — only the keys actually used below are
# required: id, label, host, port, db.

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
        "id": "srv2",
        "label": "",
        "host": "192.168.10.10",
        "port": 25656,  # net_iPort from init.ini
        "db": "D:/CustomTSE/Bin/PlayerStats.db",  # path to this server's PlayerStats.db
        "demos_dir": "D:/CustomTSE/Demos/server2",  # path to Demos\ folder
        "maps_dir": "D:/CustomTSE/Maps",  # path to folder containing map pack zips for download
        "rcon_pass": "hrtmcftgmkjfgjsrhu",  # net_strAdminPassword from init.ini
    },
    {
        "id": "srv3",
        "label": "",
        "host": "192.168.10.11",
        "port": 25666,  # net_iPort from init.ini
        "db": "D:/CustomTSE/Bin/PlayerStats.db",  # path to this server's PlayerStats.db
        "demos_dir": "D:/CustomTSE/Demos/rocketjump",  # path to Demos\ folder
        "maps_dir": "D:/CustomTSE/Maps",  # path to folder containing map pack zips for download
        "rcon_pass": "hrtmcftgmkjfgjsrhu",  # net_strAdminPassword from init.ini
    },
    {
        "id":       "srv4",
        "label":    "",
        "host":     "192.168.10.11",
        "port":     25720,             # net_iPort from init.ini
        "db":       "D:/CustomTFE/Bin/PlayerStats.db",     # path to this server's PlayerStats.db
        "demos_dir":"D:/CustomTFE/Demos/rocketjump",              # path to Demos\ folder
        "maps_dir": "D:/CustomTSE/Maps",               # path to folder containing map pack zips for download
        "rcon_pass":"hrtmcftgmkjfgjsrhu",          # net_strAdminPassword from init.ini
    },
    {
        "id":       "srv5",
        "label":    "",
        "host":     "192.168.10.11",
        "port":     25676,             # net_iPort from init.ini
        "db":       "D:/CustomTSE/Bin/PlayerStats.db",     # path to this server's PlayerStats.db
        "demos_dir":"D:/CustomTSE/Demos/DM",              # path to Demos\ folder
        "maps_dir": "D:/CustomTSE/Maps",               # path to folder containing map pack zips for download
        "rcon_pass":"hrtmcftgmkjfgjsrhu",          # net_strAdminPassword from init.ini
    },
    # {
    #     "id":    "srv2",
    #     "label": "Custom Maps",
    #     "host":  "192.168.1.64",
    #     "port":  25667,
    #     "db":    "D:/CustomTSE2/Bin/PlayerStats.db",
    # },
]

ADMIN_TOKEN = "hrtmcftgmkjfgjsrhu"   # same token as app.py
MGMT_HOST   = "127.0.0.1"            # bind to localhost; use a reverse proxy for HTTPS
MGMT_PORT   = 5001

# ─── Internal helpers ──────────────────────────────────────────────────────────

# Build a lookup dict at startup
_SERVERS_BY_ID = {s["id"]: s for s in SERVERS}

app = Flask(__name__, static_folder=".")
CORS(app)


def _check_auth():
    """Abort 401 if the request does not carry a valid admin token."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        header = header[7:]
    token = request.args.get("token", "")
    if header == ADMIN_TOKEN or token == ADMIN_TOKEN:
        return
    abort(401)


def _get_server(server_id):
    srv = _SERVERS_BY_ID.get(server_id)
    if not srv:
        abort(404, description=f"Unknown server: {server_id}")
    return srv


def _open_db(server_id):
    """Return (conn, srv) with read-write access. Caller must close conn."""
    srv = _get_server(server_id)
    db_path = srv.get("db", "")
    if not db_path or not os.path.isfile(db_path):
        abort(404, description=f"DB not found: {db_path}")
    conn = sqlite3.connect(db_path, timeout=5, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn, srv


# ─── GameSpy UDP query ─────────────────────────────────────────────────────────

_GS_TIMEOUT = 2.0


def _gs_query(host, port):
    """Return parsed GameSpy info dict or None on failure."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(_GS_TIMEOUT)
        s.sendto(b"\\info\\\\players\\", (host, port))
        raw = b""
        while True:
            try:
                chunk, _ = s.recvfrom(4096)
                raw += chunk
                if b"\\final\\" in chunk:
                    break
            except socket.timeout:
                break
        s.close()
        return _parse_gs(raw.decode("latin-1", errors="replace"))
    except Exception:
        return None


def _parse_gs(text):
    parts = text.split("\\")
    kv = {}
    i = 0
    while i + 1 < len(parts):
        if parts[i]:
            kv[parts[i]] = parts[i + 1]
        i += 2

    players = []
    idx = 0
    while True:
        name = kv.get(f"player_{idx}") or kv.get(f"playername_{idx}")
        if name is None:
            break
        players.append({
            "slot":   idx,
            "name":   name,
            "frags":  _to_int(kv.get(f"frags_{idx}") or kv.get(f"score_{idx}", "0")),
            "ping":   _to_int(kv.get(f"ping_{idx}", "0")),
            "team":   kv.get(f"team_{idx}", ""),
        })
        idx += 1

    return {
        "online":      True,
        "hostname":    kv.get("hostname", ""),
        "gametype":    kv.get("gametype", ""),
        "mapname":     kv.get("mapname", ""),
        "numplayers":  _to_int(kv.get("numplayers", "0")),
        "maxplayers":  _to_int(kv.get("maxplayers", "0")),
        "players":     players,
    }


def _to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ─── Static: serve admin.html ──────────────────────────────────────────────────

@app.route("/")
def root():
    return send_from_directory(".", "admin.html")


# ─── Public: server list (no auth — needed for the page to bootstrap) ──────────

@app.route("/api/servers")
def api_servers():
    return jsonify([
        {"id": s["id"], "label": s.get("label", s["id"]),
         "host": s["host"], "port": s["port"]}
        for s in SERVERS
    ])


# ─── Live: GameSpy state ───────────────────────────────────────────────────────

@app.route("/api/<server_id>/live")
def api_live(server_id):
    _check_auth()
    srv = _get_server(server_id)
    info = _gs_query(srv["host"], srv["port"])
    return jsonify(info or {"online": False, "players": []})


# ─── Players DB ───────────────────────────────────────────────────────────────

@app.route("/api/<server_id>/players")
def api_players(server_id):
    _check_auth()
    conn, srv = _open_db(server_id)
    q = request.args.get("q", "").strip()
    try:
        if q:
            rows = conn.execute(
                "SELECT guid, name, last_ip, country,"
                "       total_sessions, total_playtime_secs, last_seen"
                " FROM players"
                " WHERE name LIKE ? OR guid LIKE ? OR last_ip LIKE ?"
                " ORDER BY last_seen DESC LIMIT 100",
                (f"%{q}%", f"%{q}%", f"%{q}%")
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT guid, name, last_ip, country,"
                "       total_sessions, total_playtime_secs, last_seen"
                " FROM players ORDER BY last_seen DESC LIMIT 200"
            ).fetchall()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500

    # Cross-reference with live data
    live = _gs_query(srv["host"], srv["port"]) or {}
    live_names = {p["name"].lower() for p in live.get("players", [])}

    result = [
        {
            "guid":     r["guid"],
            "name":     r["name"],
            "ip":       r["last_ip"],
            "country":  r["country"],
            "sessions": r["total_sessions"],
            "playtime": r["total_playtime_secs"],
            "last_seen": r["last_seen"],
            "online":   r["name"].lower() in live_names,
        }
        for r in rows
    ]
    conn.close()
    return jsonify(result)


# ─── Bans ──────────────────────────────────────────────────────────────────────

@app.route("/api/<server_id>/bans")
def api_bans_list(server_id):
    _check_auth()
    conn, _ = _open_db(server_id)
    try:
        rows = conn.execute(
            "SELECT id, guid, ip, reason, admin, banned_at, expires_at, active"
            " FROM bans ORDER BY id DESC LIMIT 500"
        ).fetchall()
    finally:
        conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/<server_id>/bans", methods=["POST"])
def api_bans_add(server_id):
    _check_auth()
    data = request.get_json(force=True) or {}
    guid       = data.get("guid") or None
    ip         = data.get("ip")   or None
    reason     = data.get("reason", "Banned by admin").strip() or "Banned by admin"
    admin      = data.get("admin", "web").strip() or "web"
    expires_at = data.get("expires_at")  # None = permanent, int unix ts = temporary

    if not guid and not ip:
        return jsonify({"error": "At least one of 'guid' or 'ip' is required"}), 400

    now = int(time.time())
    conn, _ = _open_db(server_id)
    try:
        cur = conn.execute(
            "INSERT INTO bans (guid, ip, reason, admin, banned_at, expires_at, active)"
            " VALUES (?, ?, ?, ?, ?, ?, 1)",
            (guid, ip, reason, admin, now, expires_at)
        )
        ban_id = cur.lastrowid

        # Also queue an immediate kick so the player is removed right away
        # if they are currently online. The C++ will process this within ~1 second.
        kick_target = guid if guid else ip
        kick_by_ip  = 0 if guid else 1
        conn.execute(
            "INSERT INTO pending_cmds (cmd, target, by_ip, param, created_at)"
            " VALUES ('kick', ?, ?, ?, ?)",
            (kick_target, kick_by_ip, reason, now)
        )
    finally:
        conn.close()
    return jsonify({"id": ban_id}), 201


@app.route("/api/<server_id>/bans/<int:ban_id>", methods=["DELETE"])
def api_bans_remove(server_id, ban_id):
    _check_auth()
    conn, _ = _open_db(server_id)
    try:
        conn.execute("UPDATE bans SET active=0 WHERE id=?", (ban_id,))
    finally:
        conn.close()
    return jsonify({"ok": True})


# ─── Kick ──────────────────────────────────────────────────────────────────────

@app.route("/api/<server_id>/kick", methods=["POST"])
def api_kick(server_id):
    _check_auth()
    data   = request.get_json(force=True) or {}
    guid   = data.get("guid") or None
    ip     = data.get("ip")   or None
    reason = data.get("reason", "Kicked by admin").strip() or "Kicked by admin"

    target = guid if guid else ip
    by_ip  = 0 if guid else 1
    if not target:
        return jsonify({"error": "guid or ip required"}), 400

    conn, _ = _open_db(server_id)
    try:
        cur = conn.execute(
            "INSERT INTO pending_cmds (cmd, target, by_ip, param, created_at)"
            " VALUES ('kick', ?, ?, ?, ?)",
            (target, by_ip, reason, int(time.time()))
        )
        cmd_id = cur.lastrowid
    finally:
        conn.close()
    return jsonify({"cmd_id": cmd_id})


# ─── Mute ──────────────────────────────────────────────────────────────────────

@app.route("/api/<server_id>/mute", methods=["POST"])
def api_mute(server_id):
    _check_auth()
    data     = request.get_json(force=True) or {}
    guid     = data.get("guid") or None
    ip       = data.get("ip")   or None
    duration = max(1, _to_int(data.get("duration", 300)))

    target = guid if guid else ip
    by_ip  = 0 if guid else 1
    if not target:
        return jsonify({"error": "guid or ip required"}), 400

    conn, _ = _open_db(server_id)
    try:
        cur = conn.execute(
            "INSERT INTO pending_cmds (cmd, target, by_ip, param, created_at)"
            " VALUES ('mute', ?, ?, ?, ?)",
            (target, by_ip, str(duration), int(time.time()))
        )
        cmd_id = cur.lastrowid
    finally:
        conn.close()
    return jsonify({"cmd_id": cmd_id})


# ─── Unmute ────────────────────────────────────────────────────────────────────

@app.route("/api/<server_id>/unmute", methods=["POST"])
def api_unmute(server_id):
    _check_auth()
    data  = request.get_json(force=True) or {}
    guid  = data.get("guid") or None
    ip    = data.get("ip")   or None

    target = guid if guid else ip
    by_ip  = 0 if guid else 1
    if not target:
        return jsonify({"error": "guid or ip required"}), 400

    conn, _ = _open_db(server_id)
    try:
        cur = conn.execute(
            "INSERT INTO pending_cmds (cmd, target, by_ip, param, created_at)"
            " VALUES ('unmute', ?, ?, NULL, ?)",
            (target, by_ip, int(time.time()))
        )
        cmd_id = cur.lastrowid
    finally:
        conn.close()
    return jsonify({"cmd_id": cmd_id})


# ─── Command queue status ──────────────────────────────────────────────────────

@app.route("/api/<server_id>/cmd/<int:cmd_id>")
def api_cmd_status(server_id, cmd_id):
    _check_auth()
    conn, _ = _open_db(server_id)
    try:
        row = conn.execute(
            "SELECT id, cmd, target, by_ip, param, created_at, done, result"
            " FROM pending_cmds WHERE id=?", (cmd_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        abort(404)
    return jsonify(dict(row))


@app.route("/api/<server_id>/pending")
def api_pending(server_id):
    _check_auth()
    conn, _ = _open_db(server_id)
    try:
        rows = conn.execute(
            "SELECT id, cmd, target, by_ip, param, created_at, done, result"
            " FROM pending_cmds ORDER BY id DESC LIMIT 50"
        ).fetchall()
    finally:
        conn.close()
    return jsonify([dict(r) for r in rows])


# ─── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[mgmt] Starting admin panel on http://{MGMT_HOST}:{MGMT_PORT}")
    app.run(host=MGMT_HOST, port=MGMT_PORT, debug=False, threaded=True)