"""Generates a made-up Claude Code data folder for `--demo` mode (screenshots, trying the app).

Nothing here is real user data. The layout mirrors what Claude Code writes:
  <root>/projects/<project>/<session-id>.jsonl     transcripts
  <root>/sessions/<pid>.json                        live session status
  <root>/app-sessions/local_<id>.json               desktop-app session list
"""
import json
import os
import random
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SESSIONS = [
    # (kind, cwd, title, days_ago, hour, minutes, prompts)
    ("app", r"C:\Users\you\Projects\weather-app", "Add hourly forecast chart", 0, 10, 55,
     ["Add an hourly forecast chart to the weather screen", "Use the same colours as the daily view",
      "Make the chart scroll horizontally on phones"]),
    ("cli", r"D:\Work\api-gateway", "Fix flaky rate-limit tests", 0, 9, 40,
     ["The rate limit tests fail randomly in CI, can you find out why?", "Mock the clock instead of sleeping",
      "Run the whole test suite again"]),
    ("cli", r"D:\Work\data-pipeline", "Speed up nightly CSV import", 1, 16, 70,
     ["The nightly import of sales CSV files takes 40 minutes", "Profile it and find the slow part",
      "Batch the database inserts", "Add a progress log every 10k rows"]),
    ("app", r"C:\Users\you\Documents\thesis", "Thesis chapter 3 outline", 1, 11, 45,
     ["Help me outline chapter 3 of my thesis on battery ageing", "Suggest figures I should include",
      "Turn the outline into a slide deck for my supervisor"]),
    ("cli", r"E:\Games\mod-tools", "Texture converter for game mods", 2, 21, 50,
     ["Write a script that converts PNG textures to DDS for my game mod", "Handle folders recursively",
      "Skip files that are already converted"]),
    ("app", r"D:\Work\mobile-app", "Dark mode for settings screen", 3, 14, 35,
     ["Add dark mode to the settings screen", "Respect the system theme by default"]),
    ("cli", r"D:\Work\api-gateway", "Add OAuth login with GitHub", 4, 10, 90,
     ["Add GitHub OAuth login to the gateway", "Store tokens encrypted", "Write a short README section"]),
    ("app", r"C:\Users\you\Projects\portfolio-site", "Portfolio website redesign", 5, 19, 60,
     ["Redesign my portfolio website homepage", "Make the project cards bigger", "Add a contact form"]),
    ("cli", r"C:\Users\you\Projects\budget-tracker", "Budget tracker spreadsheet export", 6, 20, 25,
     ["Export my monthly budget to an Excel spreadsheet", "Add a chart of spending by category"]),
    ("app", r"D:\Work\data-pipeline", "Explain the retry logic", 8, 15, 20,
     ["Explain how the retry logic in the pipeline works", "Draw a diagram of the flow"]),
    ("cli", r"C:\Users\you\Projects\weather-app", "Set up GitHub Actions CI", 10, 12, 30,
     ["Set up GitHub Actions to run tests and lint on every push"]),
    ("app", r"C:\Users\you\Documents\thesis", "Literature review summary", 12, 9, 40,
     ["Summarise these five papers on lithium battery degradation", "Make a comparison table"]),
]
LIVE = {0: "busy", 3: "waiting"}           # index in SESSIONS -> live status
MODEL = "claude-sonnet-4-5"
REPLIES = [
    "Done. I updated the code and ran the tests — everything passes.",
    "Here is what I found and the change I made.",
    "I added it and kept the existing style. Want me to also update the docs?",
    "That's fixed. The root cause was a timing issue in the test setup.",
]


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def generate(root):
    root = Path(root)
    marker = root / ".generated"
    today = datetime.now().strftime("%Y-%m-%d")
    if marker.exists() and marker.read_text() == today:
        _refresh_live(root)
        return root
    # regenerate (timestamps are relative to today) and drop the demo cache built from older files
    shutil.rmtree(root, ignore_errors=True)
    for stale in ("cache.json", "textindex"):
        p = root.parent / stale
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
    rnd = random.Random(7)
    (root / "projects").mkdir(parents=True)
    (root / "sessions").mkdir()
    (root / "app-sessions").mkdir()
    now = datetime.now()
    for i, (kind, cwd, title, days_ago, hour, minutes, prompts) in enumerate(SESSIONS):
        sid = str(uuid.UUID(int=rnd.getrandbits(128)))
        start = (now - timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0, microsecond=0)
        if start > now - timedelta(minutes=minutes + 5):
            start = now - timedelta(minutes=minutes + 5)
        proj = root / "projects" / cwd.replace(":", "-").replace("\\", "-").replace(" ", "-")
        proj.mkdir(exist_ok=True)
        entry = "claude-desktop" if kind == "app" else "cli"
        lines = [{"type": "ai-title", "aiTitle": title, "sessionId": sid}]
        t = start
        step = timedelta(minutes=max(2, minutes // (len(prompts) * 2)))
        for p in prompts:
            lines.append({"type": "user", "message": {"role": "user", "content": p}, "timestamp": _iso(t),
                          "cwd": cwd, "entrypoint": entry, "sessionId": sid, "isSidechain": False})
            t += step
            usage = {"input_tokens": rnd.randint(2, 40), "output_tokens": rnd.randint(400, 3500),
                     "cache_creation_input_tokens": rnd.randint(2000, 30000),
                     "cache_read_input_tokens": rnd.randint(20000, 400000)}
            lines.append({"type": "assistant", "timestamp": _iso(t), "sessionId": sid, "cwd": cwd,
                          "message": {"id": "msg_" + uuid.UUID(int=rnd.getrandbits(128)).hex[:24], "model": MODEL,
                                      "role": "assistant", "content": [{"type": "text", "text": rnd.choice(REPLIES)}],
                                      "usage": usage}})
            t += step
        lines.append({"type": "last-prompt", "lastPrompt": prompts[-1], "sessionId": sid})
        path = proj / (sid + ".jsonl")
        path.write_text("\n".join(json.dumps(x, separators=(",", ":")) for x in lines) + "\n", "utf-8")
        mtime = t.timestamp()
        os.utime(path, (mtime, mtime))
        if kind == "app":
            local = "local_" + str(uuid.UUID(int=rnd.getrandbits(128)))
            (root / "app-sessions" / (local + ".json")).write_text(json.dumps({
                "sessionId": local, "cliSessionId": sid, "cwd": cwd, "title": title, "model": MODEL,
                "createdAt": int(start.timestamp() * 1000), "lastActivityAt": int(mtime * 1000),
                "isArchived": False, "completedTurns": len(prompts)}), "utf-8")
        else:
            local = None
        if i in LIVE:
            (root / "sessions" / ("demo-%d.json" % i)).write_text(json.dumps({
                "pid": os.getpid(), "sessionId": sid, "hostSessionId": local, "cwd": cwd,
                "entrypoint": entry, "status": LIVE[i], "waitingFor": "approve Bash" if LIVE[i] == "waiting" else None,
                "statusUpdatedAt": int(time.time() * 1000), "name": title}), "utf-8")
    marker.write_text(today)
    return root


def _refresh_live(root):
    """Point the demo 'live' sessions at this process so they show as running."""
    for f in (root / "sessions").glob("*.json"):
        try:
            d = json.loads(f.read_text("utf-8"))
            d["pid"] = os.getpid()
            f.write_text(json.dumps(d), "utf-8")
        except Exception:
            pass
