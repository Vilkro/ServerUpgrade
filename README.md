# ServerUpgrade

Server configs and web dashboard for running **Serious Sam Classic: The First Encounter** and
**The Second Encounter** dedicated servers on top of a modded fork of
[DreamyCecil's Serious Sam Classics Patch](https://github.com/SamClassicPatch/SuperProject):
[Vilkro/SuperProject](https://github.com/Vilkro/SuperProject).

## Contents

- `TFE105/Scripts/Dedicated/RocketJump` — dedicated server config (`init.ini`, per-round
  `N_begin.ini`/`N_end.ini` scripts). `init_source.ini` is the original config prior to
  encoding Cyrillic strings for in-game readability.
- `TSE107/Mods/ClassicsPatchMod` — mod shell used for the TSE 1.07 build.
- `Dashboard/` — Flask + single-page-HTML web dashboard for monitoring and moderating the
  running servers.

## What the fork adds

The dedicated server binary these configs run on is a modified build of Classics Patch. On top
of the full upstream feature set, it adds:

- **PlayerDB** — SQLite player database keyed by GUID: session history (map/frags/deaths/score),
  ban storage with custom reasons, a remote admin command queue (kick/mute/unmute issued from
  the dashboard and picked up server-side once a second), and per-player language/announcement
  preferences.
- **GeoIP** — asynchronous IP geolocation with in-game `@locate`/`@ping` lookups, non-blocking
  and cache-backed.
- **DemoManager** — shell-driven `.dem` recording (`StartDemoRec`/`StopDemoRec`) with an
  auto-rotating recording pattern for continuous coverage.
- **PlayersBrowse** — `@browse` chat command that lists other public FE/SE servers via
  333networks, without leaving the game.
- **ScriptScheduler** — a delayed-execution queue (`ScheduleScript`) used to build timers and
  self-rescheduling behavior straight from `init.ini`.
- **Tracking (RJT, work in progress)** — server-side rocket-jump tracking and scoring: net
  height gained, apex height, peak vertical velocity, hit-angle accuracy, and in-chat
  announcements for qualifying jumps.
- **Multilingual chat & voting** — EN/RU chat routed per recipient by stored language
  preference, plus chat-driven map voting and player kick/ban voting.
- **ServerUtilities sandbox tools** — bulk entity cleanup/inspection and batch property edits
  for level setup (mover activation modes, coop-marker handling, entity property read/write).

See the [fork's README](https://github.com/Vilkro/SuperProject) for the full breakdown of each
addition, file-by-file.

## Dashboard

`Dashboard/` is a small Flask app + static single-page UI that polls the running server(s) and,
via PlayerDB's pending-command queue, can issue kick/mute/unmute actions against a live server
from a browser. See `Dashboard/SSCP_README.md` for setup.

## Acknowledgments

- **[DreamyCecil's Classics Patch](https://github.com/SamClassicPatch/SuperProject)** is the
  foundation all of this runs on — carefully engineered, actively maintained, and the standard
  for Serious Sam Classic server/client modding. This project is a much smaller, less rigorous
  layer built on top of that work.
- Several ideas here (player database, moderation tooling, multilingual chat, voting) were
  inspired by the long-running **42amsterdam** servers run by **Ostap**, which have been online
  for over a decade with excellent server code. I don't have access to that codebase — these
  features were reimplemented independently by observing how they behave from a player's
  perspective, not by copying source.
- I have only basic coding skills myself; most of the implementation was written with the help
  of Claude (Anthropic). The design decisions and feature scope are mine, but a lot of the
  actual code came out of that collaboration.

## License

Configs and dashboard code here are provided as-is for running these specific servers. The
underlying dedicated server binary is built from Classics Patch, licensed under GNU GPL v2 (see
[SuperProject](https://github.com/Vilkro/SuperProject)'s `LICENSE`).
