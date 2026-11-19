# Changelog

## 1.0.1 — 2026-11-20

- Uses about half the memory while the window is open: the Edge app window now starts with low-memory settings
  (no graphics acceleration, extensions, sync, background updates or spare page processes). Measured on Windows 10:
  ~250 MB → ~130 MB for the window. The ~44 MB background part quits 15 minutes after you close it.
- Publisher name is now `optimus-ook-prime-gluk`.

## 1.0.0 — 2026-11-19

First public release.

- One window for every Claude Code conversation on your PC — from the Claude desktop app and the `claude` CLI.
- Click to reopen: app conversations open in the Claude app, terminal ones resume in a new `cmd` window in their folder.
- Tiles or details view; sidebar grouped by drive and folder.
- Search by words, or describe it in plain words ("that battery thesis slides from wednesday") — ranked offline.
- Live beacon (working · needs you · finished) with window glow, chime, Windows notification and taskbar flash.
- Insights: active time and tokens per day, where your time went by folder, app vs terminal.
- Light, dark or system theme. `--demo` mode with made-up sessions.
- Per-user installer (no admin rights) and portable zip.
