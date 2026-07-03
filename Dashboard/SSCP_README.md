# SSCP Dashboard

Server monitoring dashboard for Serious Sam Classics Patch dedicated servers.

## Project layout

```
sscp_dashboard/
├── DemoManager.h            ← drop into Core/Query/
├── DemoManager.cpp          ← drop into Core/Query/
├── init_demo_snippet.ini    ← append to your init.ini
├── server/
│   ├── app.py               ← Flask API + GameSpy poller
│   └── requirements.txt
└── static/
    └── index.html           ← dashboard UI (single file)
```

## Step 1 — Compile DemoManager into the patch

1. Copy `DemoManager.h` and `DemoManager.cpp` into `Core/Query/` alongside `GeoIP.cpp` and `PlayerDB.cpp`.

2. Add `DemoManager.cpp` to `Core.vcxproj` (ClCompile section, same as PlayerDB.cpp).

3. In `Core/Query/QueryManager.cpp`, at the bottom of `InitQuery()`, after `PlayerDB_Init()`:

   ```cpp
   #include "Query/DemoManager.h"   // add to top of QueryManager.cpp
   // ...
   PlayerDB_Init();    // already there
   DemoManager_Init(); // add this
   ```

4. In `Core/Core.cpp`, in `ClassicsPatch_Shutdown()`, after `PlayerDB_Shutdown()`:

   ```cpp
   DemoManager_Shutdown(); // add this
   ```

5. Rebuild Core in Visual Studio.

This adds four new shell functions:
- `StartDemoRec("auto")` — starts recording with auto timestamp filename
- `StopDemoRec()` — stops recording
- `IsDemoRecording()` — returns 1/0
- `GetDemoName()` — returns current filename or ""

## Step 2 — Enable auto demo recording in init.ini

Append the contents of `init_demo_snippet.ini` to your `init.ini`.

Key line (uses `cmd_strFifthExtra` as the rotation callback string):
```ini
cmd_strFifthExtra = "StopDemoRec();StartDemoRec(\"auto\");ScheduleScript(120.0, cmd_strFifthExtra);";
cmd_cmdOnJoin = cmd_cmdOnJoin
  + "if(IsDemoRecording() == 0){"
      + "StartDemoRec(\"auto\");"
      + "ScheduleScript(120.0, cmd_strFifthExtra);"
  + "}\n";
```

Demos are written to `<game_root>\Demos\auto_YYYYMMDD_HHMMSS_<map>.dem`.

## Step 3 — Configure and run the Python backend

Edit `server/app.py` → `SERVERS` list at the top:

```python
SERVERS = [
    {
        "id":        "srv1",
        "label":     "Main Server",
        "host":      "127.0.0.1",
        "port":      25666,            # net_iPort
        "db":        "C:/SS/PlayerStats.db",  # full path to PlayerStats.db
        "demos_dir": "C:/SS/Demos",           # full path to Demos\ folder
        "rcon_pass": "secret",         # net_strAdminPassword
    },
]
```

Also set the admin token (keep it secret):
```bash
set SSCP_ADMIN_TOKEN=your_secret_token_here
```

Install and run:
```bash
pip install flask flask-cors
python server/app.py
```

Then open `http://localhost:5000` in your browser.

## API reference

| Endpoint | Description |
|---|---|
| `GET /api/servers` | Live server status (GameSpy poll) |
| `GET /api/activity` | 24h player count timeline |
| `GET /api/players` | Player stats from PlayerStats.db |
| `GET /api/players/<guid>` | Single player profile + sessions |
| `GET /api/demos` | List of .dem files |
| `GET /api/demos/<server>/<file>` | Download demo |
| `POST /api/admin/kick` | Kick by session slot (requires Bearer token) |
| `POST /api/admin/ban` | Ban by identity ID (requires Bearer token) |
| `POST /api/admin/exec` | Execute arbitrary shell command via RCON |

## Notes on RCON

SE1 dedicated server accepts RCON on the main game port (TCP).  
Format: `rcon <password> <command>\n`

For kick/ban, the admin panel uses:
- **Kick**: `KickClient(slot, "reason")` — immediate, by session slot 1–8
- **Ban**: `!ban <identity_id> <seconds>` — uses the patch's chat command system

To find identity IDs, run `!clog` via the RCON exec panel — it lists all known clients with their identity index.

## PlayerDB schema (for reference)

```sql
-- players: one row per unique GUID
guid       TEXT PRIMARY KEY   -- 32-char hex of pc_aubGUID
name       TEXT               -- last known name
last_ip    TEXT               -- last IP
country    TEXT               -- 2-letter country code (via GeoIP)
first_seen INTEGER            -- unix timestamp
last_seen  INTEGER            -- unix timestamp
sessions   INTEGER            -- total completed sessions
playtime   INTEGER            -- total seconds

-- sessions: one row per play session
guid    TEXT     -- → players.guid
name    TEXT     -- name during this session
map     TEXT     -- world file at disconnect
frags   INTEGER
deaths  INTEGER
score   INTEGER
started INTEGER  -- unix timestamp
ended   INTEGER  -- unix timestamp
```
