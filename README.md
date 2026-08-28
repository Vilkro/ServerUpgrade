# ServerUpgrade

Server configs and web dashboard for running **Serious Sam Classic: The First Encounter** and
**The Second Encounter** dedicated servers on top of a modded fork of
[DreamyCecil's Serious Sam Classics Patch](https://github.com/SamClassicPatch/SuperProject):
[Vilkro/SuperProject](https://github.com/Vilkro/SuperProject).

## Contents

- `TFE105/Scripts/Dedicated/` and `TSE107/Scripts/Dedicated/` - dedicated server configs, one
  folder per server type: `CustomCoop` (standard coop), `CustomFrag` (TSE fragmatch only), and
  `RocketJump` (Raiders of Marks). Each has an `init.ini` plus per-level `N_begin.ini`/
  `N_end.ini` scripts that stay synced to the actual current level (see below), even across
  coop map transitions. `init_source.ini` (one level up, shared by all three) is the original
  config prior to encoding Cyrillic strings with `convert.py` for in-game readability. Meant as a starting point -
  copy a folder and edit the numbered scripts to set up your own maps.
- `TFE105/Mods/ClassicsPatchMod` and `TSE107/Mods/ClassicsPatchMod` - mod shells for each build.
- `Dashboard/` - Flask + single-page-HTML web dashboard for monitoring and moderating the
  running servers.

## What the fork adds

The dedicated server binary these configs run on is a modified build of Classics Patch. On top
of the full upstream feature set, it adds:

- **PlayerDB** - SQLite player database keyed by GUID: session history (map/frags/deaths/score),
  ban storage with custom reasons, a remote admin command queue (kick/mute/unmute issued from
  the dashboard and picked up server-side once a second), and per-player language/announcement
  preferences.
- **GeoIP** - asynchronous IP geolocation with in-game `@locate`/`@ping` lookups, non-blocking
  and cache-backed.
- **DemoManager** - shell-driven `.dem` recording (`StartDemoRec`/`StopDemoRec`) with an
  auto-rotating recording pattern for continuous coverage.
- **PlayersBrowse** - `@players` chat command that lists other public FE/SE servers via
  333networks, without leaving the game.
- **ScriptScheduler** - a delayed-execution queue (`ScheduleScript`) used to build timers and
  self-rescheduling behavior straight from `init.ini`.
- **Round sync** - the per-level `N_begin.ini`/`N_end.ini` pair always matches the level actually
  being played, for both fragmatch and coop.
- **Tracking** - AFK detection, out-of-bounds detection, and in-chat announcements for collected
  Marks and updated coop respawn points.
- **Multilingual chat & voting** - EN/RU chat routed per recipient by stored language
  preference, plus chat-driven map voting and player kick/ban voting.
- **ServerUtilities** - sandbox commands for clearing out monsters, moving-brush shoot/touch
  activation, reusable respawn points, and a starry skybox builder.

## Dashboard

`Dashboard/` is a small Flask app + static single-page UI for monitoring and moderating the
servers, split into two pages:

- A public status page - live server/player counts, an activity graph, frag-match and Marks
  leaderboards (overall, per-map, rarest finds).
- A token-gated admin page - live player list with kick/mute dialogs, ban management, the
  PlayerDB browser, and the pending remote-command queue.

Requires Python 3 - `pip install flask flask-cors requests psutil`, then `python server/app.py`
(listens on `0.0.0.0:5000`). Configure your server(s) in the `SERVERS` list near the top of
`app.py`, and change the hardcoded `ADMIN_TOKEN` there before exposing the admin page.

## Acknowledgments

- **[DreamyCecil's Classics Patch](https://github.com/SamClassicPatch/SuperProject)** is the
  foundation all of this runs on - carefully engineered, actively maintained, and the standard
  for Serious Sam Classic server/client modding. This project is a much smaller, less rigorous
  layer built on top of that work.
- Several ideas here (player database, moderation tooling, multilingual chat, voting) were
  inspired by the long-running **42amsterdam** servers run by **Ostap**, which have been online
  for over a decade with excellent server code. I don't have access to that codebase - these
  features were reimplemented independently by observing how they behave from a player's
  perspective, not by copying source.

## License

Configs and dashboard code here are provided as-is for running these specific servers. The
underlying dedicated server binary is built from Classics Patch, licensed under GNU GPL v2 (see
[SuperProject](https://github.com/Vilkro/SuperProject)'s `LICENSE`).