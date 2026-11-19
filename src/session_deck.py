"""Session Deck - one place for every Claude Code conversation (desktop app + CLI).

An unofficial companion app for Claude Code on Windows; not affiliated with Anthropic.

Runs a tiny local web server (127.0.0.1 only, token-protected) and shows the UI in an
Edge app window. Clicking a session reopens it where it lives:
  * desktop-app conversation -> claude://claude.ai/epitaxy/local_<id> (that exact conversation)
  * CLI conversation         -> new cmd window in its folder running `claude --resume <id>`

Also: live status of running sessions (working / needs you / done) with sound, taskbar
flash and desktop notifications, usage analytics (active time, tokens) and offline
plain-words search.

Read-only on Claude's own data. Standard library only (Python 3.8+).

Usage:  SessionDeck.exe            open the window (reuses a running instance)
        SessionDeck.exe --demo     run with made-up sessions (for screenshots / trying it out)
        SessionDeck.exe --version
"""
import ctypes
import glob
import io
import json
import math
import os
import queue
import re
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import wave
from ctypes import wintypes
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import suggest as suggest_engine  # noqa: E402  (local, offline)

APP_NAME = "Session Deck"
APP_ID = "SessionDeck"
VERSION = "1.0.1"
REPO_URL = "https://github.com/optimus-ook-prime-gluk/session-deck"
DEMO = "--demo" in sys.argv

# Program files (read-only): next to this script, or inside the PyInstaller bundle.
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ASSETS = RES / "assets" if (RES / "assets").is_dir() else RES.parent / "assets"
UI_FILE = RES / "ui.html"

HOME = Path.home()
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
APPDATA = Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming"))

# Per-user data (cache, search index, settings): %LOCALAPPDATA%\SessionDeck
DATA = Path(os.environ.get("SESSIONDECK_DATA") or (LOCALAPPDATA / APP_ID)) / ("demo" if DEMO else "")
DATA.mkdir(parents=True, exist_ok=True)
CACHE_FILE = DATA / "cache.json"
SERVER_FILE = DATA / "server.json"
SETTINGS_FILE = DATA / "settings.json"
LOG_FILE = DATA / "error.log"
TEXT_DIR = DATA / "textindex"
EDGE_PROFILE = DATA / "edge-profile"

# Where Claude Code keeps its data (read only).
if DEMO:
    import demo_data  # noqa: E402
    _demo_root = demo_data.generate(DATA / "sample-claude")
    PROJECTS = _demo_root / "projects"
    LIVE_DIR = _demo_root / "sessions"
    APP_SESSION_GLOBS = [str(_demo_root / "app-sessions" / "**" / "local_*.json")]
else:
    PROJECTS = HOME / ".claude" / "projects"
    LIVE_DIR = HOME / ".claude" / "sessions"
    APP_SESSION_GLOBS = [
        str(LOCALAPPDATA / "Claude-Data" / "*" / "claude-code-sessions" / "**" / "local_*.json"),
        str(APPDATA / "Claude" / "claude-code-sessions" / "**" / "local_*.json"),
    ]

CACHE_VERSION = 7


def folder_ok(cwd):
    """Does the session's folder still exist? (Demo folders are made up, so they count as present.)"""
    return bool(cwd) and (DEMO or os.path.isdir(cwd))


def text_file(cli_id):
    return TEXT_DIR / (cli_id + ".txt")


IDLE_EXIT_SECONDS = 15 * 60
ACTIVE_GAP = 10 * 60          # gaps longer than this between messages count as a break
MIN_TURN_FOR_ALERT = 4.0      # seconds a session must have been working before "done" alerts
CREATE_NEW_CONSOLE = 0x00000010
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
LOCAL_RE = re.compile(r"^local_[0-9a-f-]{36}$")

RE_CUSTOM = re.compile(rb'"customTitle":"((?:[^"\\]|\\.)*)"')
RE_AI = re.compile(rb'"aiTitle":"((?:[^"\\]|\\.)*)"')
RE_LAST = re.compile(rb'"lastPrompt":"((?:[^"\\]|\\.)*)"')
RE_CWD = re.compile(rb'"cwd":"((?:[^"\\]|\\.)+)"')
RE_ENTRY = re.compile(rb'"entrypoint":"([^"]*)"')
RE_MODEL = re.compile(rb'"model":"(claude-[a-z0-9.-]+)"')
RE_TS = re.compile(rb'"timestamp":"(\d{4}-\d\d-\d\dT[0-9:.]+(?:Z|[+-]\d\d:\d\d)?)"')
RE_MSGID = re.compile(rb'"id":"(msg_[A-Za-z0-9]+)"')
RE_U_IN = re.compile(rb'"input_tokens":(\d+)')
RE_U_OUT = re.compile(rb'"output_tokens":(\d+)')
RE_U_CW = re.compile(rb'"cache_creation_input_tokens":(\d+)')
RE_U_CR = re.compile(rb'"cache_read_input_tokens":(\d+)')

DEFAULT_SETTINGS = {"sound": True, "notify": True, "flash": True, "alertApp": True, "alertCli": True}


def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


# =================================================================== transcript parsing
def _unescape(raw):
    try:
        return json.loads(b'"' + raw + b'"')
    except Exception:
        return raw.decode("utf-8", "replace")


def _last(regex, data):
    m = None
    for m in regex.finditer(data):
        pass
    return m


def _posix_to_win(p):
    m = re.match(r"^/([a-zA-Z])/(.*)$", p or "")
    return "%s:\\%s" % (m.group(1).upper(), m.group(2).replace("/", "\\")) if m else p


def _clean_prompt(content):
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content
                           if isinstance(c, dict) and c.get("type") == "text")
    if not isinstance(content, str):
        return ""
    text = content.strip()
    if not text or text.startswith("<") or text.startswith("Caveat:"):
        return ""
    return " ".join(text.split())


def _ts(raw):
    try:
        return datetime.fromisoformat(raw.decode().replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _day(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def new_state():
    return {"off": 0, "cwd": None, "entry": None, "first": "", "tc": None, "ta": None, "last": None,
            "model": None, "turns": 0, "lastTs": None, "iv": [], "pend": None, "days": {}}


def _commit(st):
    p = st.get("pend")
    if not p:
        return
    d = st["days"].setdefault(_day(p[1]), [0, 0, 0, 0, 0])
    for i in range(4):
        d[i] += p[2 + i]
    d[4] += 1
    st["pend"] = None


_TAG_BLOCK = re.compile(r"<([a-zA-Z][\w-]*)(?:\s[^>]*)?>.*?</\1>", re.S)


def _flat(text, cap):
    """Plain searchable text: drops tool/system tag blocks that get stored inside prompts."""
    text = _TAG_BLOCK.sub(" ", text)
    if text.lstrip().startswith(("This session is being continued", "<", "Caveat:")):
        return ""
    return " ".join(text.split())[:cap]


def feed(st, chunk, texts=None):
    """Incrementally parse complete JSONL lines into the running state.
    If `texts` is a list, searchable text is appended to it as (field, text):
    'u' = something you typed, 'a' = Claude's written reply (tool calls/results are skipped)."""
    for line in chunk.split(b"\n"):
        if not line:
            continue
        if b'"customTitle":"' in line:
            m = _last(RE_CUSTOM, line)
            if m:
                st["tc"] = _unescape(m.group(1))
        if b'"aiTitle":"' in line:
            m = _last(RE_AI, line)
            if m:
                st["ta"] = _unescape(m.group(1))
        if b'"lastPrompt":"' in line:
            m = _last(RE_LAST, line)
            if m:
                st["last"] = _unescape(m.group(1))
        if (not st["cwd"] or st["cwd"].startswith("/")) and b'"cwd":"' in line:
            m = RE_CWD.search(line)
            if m:
                st["cwd"] = _posix_to_win(_unescape(m.group(1)))
        if not st["entry"] and b'"entrypoint":"' in line:
            m = RE_ENTRY.search(line)
            if m:
                st["entry"] = _unescape(m.group(1))

        is_user = b'"type":"user"' in line
        is_asst = b'"type":"assistant"' in line
        if not (is_user or is_asst):
            continue
        m = _last(RE_TS, line)
        ts = _ts(m.group(1)) if m else None
        if ts:
            if st["lastTs"] is not None and 0 <= ts - st["lastTs"] <= ACTIVE_GAP and st["iv"]:
                st["iv"][-1][1] = max(st["iv"][-1][1], ts)
            elif st["lastTs"] is None or ts >= st["lastTs"] - 1:
                st["iv"].append([ts, ts])
            st["lastTs"] = max(ts, st["lastTs"] or 0)

        if is_user and b'"tool_result"' not in line and b'"isMeta":true' not in line \
                and b'"isSidechain":true' not in line:
            try:
                obj = json.loads(line)
                text = _clean_prompt((obj.get("message") or {}).get("content")) if obj.get("type") == "user" else ""
            except Exception:
                text = ""
            if text:
                st["turns"] += 1
                if not st["first"]:
                    st["first"] = text
                if texts is not None and _flat(text, 4000):
                    texts.append(("u", _flat(text, 4000)))
        if is_asst and texts is not None and b'"type":"text"' in line:
            try:
                obj = json.loads(line)
                for blk in (obj.get("message") or {}).get("content") or []:
                    if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text"):
                        t = _flat(blk["text"], 3000)
                        if t:
                            texts.append(("a", t))
            except Exception:
                pass
        if is_asst and b'"usage"' in line:
            mid = RE_MSGID.search(line)
            mid = mid.group(1).decode() if mid else None
            vals = []
            for rx in (RE_U_IN, RE_U_OUT, RE_U_CW, RE_U_CR):
                mm = _last(rx, line)
                vals.append(int(mm.group(1)) if mm else 0)
            mm = _last(RE_MODEL, line)
            if mm:
                st["model"] = mm.group(1).decode()
            if st["pend"] and st["pend"][0] == mid and mid:
                st["pend"] = [mid, st["pend"][1]] + vals
            else:
                _commit(st)
                st["pend"] = [mid, ts or time.time()] + vals


def parse_file(path, st, size, text_path=None):
    """Bring a cached state up to date with the file, reading only the appended bytes.
    With `text_path`, the conversation's searchable text is appended to that local file."""
    if text_path is not None and st is not None and st["off"] and not text_path.exists():
        st = None   # text index missing: rebuild this transcript from the start
    if st is None or size < st["off"]:
        st = new_state()
        if text_path is not None:
            text_path.parent.mkdir(exist_ok=True)
            text_path.write_text("", "utf-8")
    if size == st["off"]:
        return st
    with open(path, "rb") as f:
        f.seek(st["off"])
        data = f.read(size - st["off"])
    cut = data.rfind(b"\n")
    if cut < 0:
        return st
    texts = [] if text_path is not None else None
    feed(st, data[:cut], texts)
    if texts:
        with open(text_path, "a", encoding="utf-8") as tf:
            for field, t in texts:
                tf.write(field + "\t" + t.replace("\n", " ") + "\n")
    st["off"] += cut + 1
    if len(st["iv"]) > 4000:
        st["iv"] = st["iv"][-4000:]
    return st


def days_with_pending(st):
    days = {k: list(v) for k, v in st["days"].items()}
    p = st.get("pend")
    if p:
        d = days.setdefault(_day(p[1]), [0, 0, 0, 0, 0])
        for i in range(4):
            d[i] += p[2 + i]
        d[4] += 1
    return days


class Store:
    lock = threading.Lock()
    files = {}          # path -> {"size": int, "st": state}
    loaded = False


def load_cache():
    try:
        c = json.loads(CACHE_FILE.read_text("utf-8"))
        if c.get("v") == CACHE_VERSION:
            Store.files = c.get("files", {})
    except Exception:
        Store.files = {}
    Store.loaded = True


def save_cache():
    try:
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"v": CACHE_VERSION, "files": Store.files}), "utf-8")
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        log("cache save: %r" % e)


def scan_transcripts():
    """Returns ({cli_id: state}, {parent_cli_id: [subagent states]})."""
    if not Store.loaded:
        load_cache()
    main, subs, seen, changed = {}, {}, set(), False
    paths = list(PROJECTS.glob("*/*.jsonl")) + list(PROJECTS.glob("*/*/subagents/*.jsonl"))
    for p in paths:
        key = str(p)
        try:
            size = p.stat().st_size
        except OSError:
            continue
        seen.add(key)
        ent = Store.files.get(key)
        is_sub = p.parent.name == "subagents"
        tpath = None if is_sub else text_file(p.stem)
        if not ent or ent["size"] != size or (tpath is not None and not tpath.exists()):
            try:
                st = parse_file(p, ent["st"] if ent else None, size, tpath)
            except Exception as e:
                log("parse %s: %r" % (p, e))
                continue
            ent = {"size": size, "st": st}
            Store.files[key] = ent
            changed = True
        if p.parent.name == "subagents":
            subs.setdefault(p.parent.parent.name, []).append(ent["st"])
        else:
            st = ent["st"]
            st["_dir"] = p.parent.name
            try:
                st["_mtime"] = p.stat().st_mtime
            except OSError:
                st["_mtime"] = 0
            main[p.stem] = st
    for k in [k for k in Store.files if k not in seen]:
        del Store.files[k]
        changed = True
    if changed:
        save_cache()
    return main, subs


# =================================================================== app metadata + live
def load_app_sessions():
    sessions, seen = [], set()
    for pattern in APP_SESSION_GLOBS:
        for f in glob.glob(pattern, recursive=True):
            try:
                d = json.loads(Path(f).read_text("utf-8"))
            except Exception:
                continue
            sid = d.get("sessionId") or Path(f).stem
            if sid in seen or not LOCAL_RE.match(sid):
                continue
            seen.add(sid)
            sessions.append(d)
    return sessions


_alive_cache = {}


def _pid_alive(pid):
    now = time.time()
    c = _alive_cache.get(pid)
    if c and now - c[1] < 5:
        return c[0]
    alive = False
    if sys.platform == "win32":
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))
        if h:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            alive = bool(ok) and code.value == 259
    _alive_cache[pid] = (alive, now)
    return alive


def read_live():
    """Live session processes: list of dicts with ids and status."""
    out = []
    for f in LIVE_DIR.glob("*.json") if LIVE_DIR.exists() else []:
        try:
            d = json.loads(f.read_text("utf-8"))
        except Exception:
            continue
        pid = d.get("pid")
        if not pid or not _pid_alive(pid):
            continue
        out.append({"pid": pid, "cli": d.get("sessionId"), "host": d.get("hostSessionId"),
                    "status": d.get("status") or "idle", "waitingFor": d.get("waitingFor"),
                    "since": (d.get("statusUpdatedAt") or d.get("updatedAt") or 0) / 1000.0,
                    "entry": d.get("entrypoint"), "name": d.get("name"), "cwd": d.get("cwd")})
    return out


def volume_label(drive):
    try:
        buf = ctypes.create_unicode_buffer(261)
        if ctypes.windll.kernel32.GetVolumeInformationW(drive + "\\", buf, 261, None, None, None, None, 0):
            return buf.value
    except Exception:
        pass
    return ""


# =================================================================== index + analytics
class State:
    token = secrets.token_urlsafe(24)
    lock = threading.Lock()
    index = []
    by_key = {}
    cli_to_key = {}
    intervals = {}      # key -> [[s,e], ...]
    days = {}           # key -> {day: [in,out,cw,cr,n]}
    built_at = 0
    last_ping = time.time()
    subscribers = set()
    live = {}           # key -> live info
    settings = dict(DEFAULT_SETTINGS)


def _sum_iv(ivs, start=None, end=None):
    total = 0.0
    for s, e in ivs:
        if start is not None:
            s = max(s, start)
        if end is not None:
            e = min(e, end)
        if e > s:
            total += e - s
    return total


def _merge(ivs):
    out = []
    for s, e in sorted(ivs):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def _add_days(a, b):
    for k, v in b.items():
        d = a.setdefault(k, [0, 0, 0, 0, 0])
        for i in range(5):
            d[i] += v[i]
    return a


def _tok_total(days, since_day=None):
    t = 0
    for k, v in days.items():
        if since_day is None or k >= since_day:
            t += v[0] + v[1] + v[2] + v[3]
    return t


def week_start(now=None):
    d = datetime.fromtimestamp(now or time.time())
    return (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def build_index():
    with State.lock:
        if time.time() - State.built_at < 1.5 and State.index:
            return State.index
        trans, subs = scan_transcripts()
        apps = load_app_sessions()
        claimed, items, cli_to_key, ivs, dys = set(), [], {}, {}, {}
        wk = week_start().timestamp()

        def add(item, sts, cli_ids):
            key = item["key"]
            iv = _merge([list(x) for st in sts for x in st["iv"]])
            dd = {}
            for st in sts:
                _add_days(dd, days_with_pending(st))
            for cid in cli_ids:
                for sst in subs.get(cid, []):
                    _add_days(dd, days_with_pending(sst))
                cli_to_key[cid] = key
            ivs[key], dys[key] = iv, dd
            item["timeAll"] = _sum_iv(iv)
            item["timeWeek"] = _sum_iv(iv, wk)
            item["tokens"] = _tok_total(dd)
            items.append(item)

        for a in apps:
            cli_ids = [i for i in (a.get("lastAssistantCliSessionId"), a.get("cliSessionId")) if i]
            cli_ids = list(dict.fromkeys(cli_ids))
            sts = [trans[i] for i in cli_ids if i in trans]
            t = sts[0] if sts else {}
            claimed.update(cli_ids)
            cwd = a.get("cwd") or t.get("cwd")
            last_ms = max(a.get("lastActivityAt") or 0, max([(s.get("lastTs") or 0) * 1000 for s in sts] or [0])) \
                or a.get("createdAt") or 0
            add({
                "key": a["sessionId"], "kind": "app", "appId": a["sessionId"],
                "cliId": next((i for i in cli_ids if i in trans), cli_ids[0] if cli_ids else None),
                "title": a.get("title") or t.get("tc") or t.get("ta") or t.get("first") or "Untitled",
                "first": t.get("first") or "", "last": t.get("last") or "",
                "cwd": cwd, "folderExists": folder_ok(cwd),
                "lastActive": last_ms, "archived": bool(a.get("isArchived")),
                "model": a.get("model") or t.get("model"),
                "turns": sum(s.get("turns") or 0 for s in sts) or a.get("completedTurns") or 0,
            }, sts, cli_ids)

        for cid, t in trans.items():
            if cid in claimed or not (t.get("first") or t.get("tc") or t.get("ta")):
                continue
            cwd = t.get("cwd")
            add({
                "key": cid, "kind": "cli", "appId": None, "cliId": cid,
                "title": t.get("tc") or t.get("ta") or t.get("first") or cid,
                "first": t.get("first") or "", "last": t.get("last") or "",
                "cwd": cwd, "folderExists": folder_ok(cwd),
                "lastActive": (t.get("lastTs") or t.get("_mtime") or 0) * 1000, "archived": False,
                "model": t.get("model"), "turns": t.get("turns") or 0,
            }, [t], [cid])

        items.sort(key=lambda s: s["lastActive"] or 0, reverse=True)
        State.index, State.cli_to_key, State.intervals, State.days = items, cli_to_key, ivs, dys
        State.by_key = {s["key"]: s for s in items}
        State.built_at = time.time()
        return items


SUGGEST_INDEX = suggest_engine.Index()
_suggest_lock = threading.Lock()


def run_suggest(q):
    """Offline natural-language suggestions (see suggest.py)."""
    items = build_index()
    with State.lock:
        ivs = dict(State.intervals)
    with _suggest_lock:
        SUGGEST_INDEX.update(items, lambda s: text_file(s["cliId"]) if s.get("cliId") else None)
        res = suggest_engine.suggest(q, items, SUGGEST_INDEX, ivs, State.live)
    return res


def drives_info(items):
    out = {}
    for s in items:
        cwd = s.get("cwd") or ""
        letter = cwd[:2].upper() if re.match(r"^[A-Za-z]:", cwd) else "Other"
        if letter not in out:
            out[letter] = {"letter": letter, "label": volume_label(letter) if letter != "Other" else "",
                           "exists": DEMO or letter == "Other" or os.path.isdir(letter + "\\")}
    return out


def analytics():
    build_index()
    with State.lock:
        items, ivs, dys = list(State.index), dict(State.intervals), dict(State.days)
    now = time.time()
    wk = week_start(now)
    periods = {
        "week": wk.timestamp(),
        "lastweek": (wk - timedelta(days=7)).timestamp(),
        "30d": now - 30 * 86400,
        "all": 0,
    }
    pday = {k: datetime.fromtimestamp(v).strftime("%Y-%m-%d") for k, v in periods.items()}

    def tok(days, start_day, end_day=None):
        r = [0, 0, 0, 0, 0]
        for k, v in days.items():
            if k >= start_day and (end_day is None or k < end_day):
                for i in range(5):
                    r[i] += v[i]
        return r

    # global union of activity
    all_iv = _merge([list(x) for k in ivs for x in ivs[k]])
    # daily for last 14 days
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    daily = []
    all_days = {}
    for k in dys:
        _add_days(all_days, dys[k])
    for i in range(13, -1, -1):
        d0 = today - timedelta(days=i)
        s, e = d0.timestamp(), (d0 + timedelta(days=1)).timestamp()
        dk = d0.strftime("%Y-%m-%d")
        t = all_days.get(dk, [0, 0, 0, 0, 0])
        daily.append({"day": dk, "time": _sum_iv(all_iv, s, e), "in": t[0], "out": t[1], "cw": t[2],
                      "cr": t[3], "tokens": t[0] + t[1] + t[2] + t[3], "thisWeek": s >= periods["week"]})

    def period_block(start, start_day, end=None, end_day=None):
        projects = {}
        kinds = {"app": {"time": 0, "tokens": 0, "iv": []}, "cli": {"time": 0, "tokens": 0, "iv": []}}
        convs = 0
        for s in items:
            k = s["key"]
            iv = [x for x in ivs.get(k, []) if x[1] > start and (end is None or x[0] < end)]
            t = tok(dys.get(k, {}), start_day, end_day)
            tt = t[0] + t[1] + t[2] + t[3]
            if not iv and not tt:
                continue
            convs += 1
            cwd = s.get("cwd") or "(unknown)"
            p = projects.setdefault(cwd, {"cwd": cwd, "iv": [], "tokens": 0, "convs": 0, "kinds": set()})
            p["iv"].extend(iv)
            p["tokens"] += tt
            p["convs"] += 1
            p["kinds"].add(s["kind"])
            kinds[s["kind"]]["iv"].extend(iv)
            kinds[s["kind"]]["tokens"] += tt
        plist = []
        for p in projects.values():
            p["time"] = _sum_iv(_merge(p.pop("iv")), start, end)
            p["kinds"] = sorted(p["kinds"])
            plist.append(p)
        plist.sort(key=lambda p: (p["time"], p["tokens"]), reverse=True)
        for kd in kinds.values():
            kd["time"] = _sum_iv(_merge(kd.pop("iv")), start, end)
        totals = tok(all_days, start_day, end_day)
        return {"projects": plist, "kinds": kinds, "convs": convs,
                "time": _sum_iv(all_iv, start, end),
                "tokens": {"in": totals[0], "out": totals[1], "cw": totals[2], "cr": totals[3],
                           "total": sum(totals[:4]), "msgs": totals[4]}}

    top_week = sorted(({"key": s["key"], "title": s["title"], "kind": s["kind"], "cwd": s.get("cwd"),
                        "time": s.get("timeWeek", 0)} for s in items if s.get("timeWeek")),
                      key=lambda x: x["time"], reverse=True)[:5]
    return {
        "daily": daily,
        "week": period_block(periods["week"], pday["week"]),
        "lastweek": period_block(periods["lastweek"], pday["lastweek"], periods["week"], pday["week"]),
        "30d": period_block(periods["30d"], pday["30d"]),
        "all": period_block(0, "0000"),
        "topWeek": top_week,
        "weekStart": pday["week"],
        "gapMinutes": ACTIVE_GAP // 60,
    }


# =================================================================== live monitor + alerts
def broadcast(msg):
    data = json.dumps(msg)
    for q in list(State.subscribers):
        try:
            q.put_nowait(data)
        except Exception:
            pass


def live_key(rec):
    with State.lock:
        by_key, c2k = State.by_key, State.cli_to_key
    if rec["host"] and rec["host"] in by_key:
        return rec["host"]
    if rec["cli"] and rec["cli"] in c2k:
        return c2k[rec["cli"]]
    return rec["host"] or rec["cli"]


def monitor():
    prev = None
    while True:
        try:
            recs = read_live()
            cur = {}
            for r in recs:
                k = live_key(r)
                if not k:
                    continue
                st = r["status"]
                state = "waiting" if st == "waiting" else ("working" if st in ("busy", "shell") else "idle")
                if k in cur and cur[k]["state"] != "idle":
                    continue
                cur[k] = {"key": k, "state": state, "waitingFor": r.get("waitingFor"), "since": r["since"],
                          "kind": "app" if r.get("entry") == "claude-desktop" else "cli",
                          "name": r.get("name"), "pid": r["pid"], "cwd": r.get("cwd")}
            if prev is not None:
                unknown = [k for k in cur if k not in State.by_key]
                if unknown:
                    State.built_at = 0
                    try:
                        build_index()
                    except Exception as e:
                        log("index in monitor: %r" % e)
                for k, c in cur.items():
                    p = prev.get(k)
                    if c["state"] == "waiting" and (not p or p["state"] != "waiting"):
                        alert("attention", c)
                    elif c["state"] == "idle" and p and p["state"] == "working" \
                            and (c["since"] or time.time()) - (p["since"] or 0) >= MIN_TURN_FOR_ALERT:
                        alert("done", c)
            if prev is None or _live_sig(cur) != _live_sig(prev):
                State.live = cur
                broadcast({"type": "live", "live": cur})
            prev = cur
        except Exception as e:
            log("monitor: %r" % e)
        time.sleep(1.0)


def _live_sig(d):
    return sorted((k, v["state"], v.get("waitingFor")) for k, v in d.items())


def alert(kind, c):
    s = State.settings
    if (c["kind"] == "app" and not s.get("alertApp", True)) or (c["kind"] == "cli" and not s.get("alertCli", True)):
        return
    item = State.by_key.get(c["key"], {})
    title = item.get("title") or c.get("name") or "Claude Code session"
    msg = {"type": "alert", "alert": kind, "key": c["key"], "title": title, "kind": c["kind"],
           "reason": c.get("waitingFor"), "cwd": item.get("cwd") or c.get("cwd"), "at": time.time() * 1000}
    broadcast(msg)
    if s.get("sound", True):
        play_sound(kind)
    if s.get("flash", True):
        threading.Thread(target=flash_window, daemon=True).start()


# ---- sound: synthesized chimes, played from memory
def _chime(notes):
    rate = 44100
    frames = bytearray()
    for freq, dur, gap in notes:
        n = int(rate * dur)
        for i in range(n):
            t = i / rate
            env = min(1.0, t / 0.008) * math.exp(-4.5 * t / dur)
            v = env * (0.6 * math.sin(2 * math.pi * freq * t) + 0.25 * math.sin(4 * math.pi * freq * t)
                       + 0.1 * math.sin(6 * math.pi * freq * t))
            frames += struct.pack("<h", int(v * 0.42 * 32767))
        frames += b"\x00\x00" * int(rate * gap)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


SOUNDS = {}
_sound_lock = threading.Lock()


def play_sound(kind):
    def run():
        try:
            import winsound
            if not SOUNDS:
                SOUNDS["done"] = _chime([(659.25, 0.16, 0.02), (987.77, 0.42, 0.0)])
                SOUNDS["attention"] = _chime([(880.0, 0.14, 0.07), (880.0, 0.14, 0.07), (1174.66, 0.38, 0.0)])
            with _sound_lock:
                winsound.PlaySound(SOUNDS.get(kind, SOUNDS["done"]), winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        except Exception as e:
            log("sound: %r" % e)
    threading.Thread(target=run, daemon=True).start()


# ---- taskbar flash on the app window
class FLASHWINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]


def find_app_windows():
    user32 = ctypes.windll.user32
    found = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if APP_NAME in buf.value:
                    cls = ctypes.create_unicode_buffer(64)
                    user32.GetClassNameW(hwnd, cls, 64)
                    if cls.value.startswith("Chrome_WidgetWin"):
                        found.append(hwnd)
        return True
    user32.EnumWindows(proto(cb), 0)
    return found


class TaskbarIcon:
    """Keeps the app window's taskbar/title-bar icon set to our full-size .ico.
    Edge would otherwise derive it from the page favicon, which leaves it small on the taskbar."""
    badge = None          # None | "wait" | "done"  (reported by the page)
    _cache = {}

    @classmethod
    def handles(cls, variant):
        if variant in cls._cache:
            return cls._cache[variant]
        u = ctypes.windll.user32
        u.LoadImageW.restype = ctypes.c_void_p
        u.LoadImageW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        path = str(ASSETS / ("logo.ico" if not variant else "logo-%s.ico" % variant))
        if not os.path.exists(path):
            path = str(ASSETS / "logo.ico")
        try:
            dpi = u.GetDpiForSystem()
        except Exception:
            dpi = 96
        tb = int(round(24 * dpi / 96.0))     # the taskbar draws the big icon at 24 px (x DPI scale)
        big = u.LoadImageW(None, path, 1, tb, tb, 0x10)   # IMAGE_ICON, LR_LOADFROMFILE: exact-size frame, no rescale
        small = u.LoadImageW(None, path, 1, u.GetSystemMetrics(49), u.GetSystemMetrics(50), 0x10)
        cls._cache[variant] = (big, small)
        return big, small

    @classmethod
    def apply(cls):
        u = ctypes.windll.user32
        u.SendMessageW.restype = ctypes.c_void_p
        u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p]
        big, small = cls.handles(cls.badge)
        if not big:
            return
        for hwnd in find_app_windows():
            if u.SendMessageW(hwnd, 0x7F, 1, 0) != big:          # WM_GETICON ICON_BIG
                u.SendMessageW(hwnd, 0x80, 1, big)                # WM_SETICON
            if small and u.SendMessageW(hwnd, 0x7F, 0, 0) != small:
                u.SendMessageW(hwnd, 0x80, 0, small)

    @classmethod
    def loop(cls):
        while True:
            try:
                cls.apply()
            except Exception as e:
                log("taskbar icon: %r" % e)
            time.sleep(1.5)


def flash_window():
    try:
        for hwnd in find_app_windows():
            if ctypes.windll.user32.GetForegroundWindow() == hwnd:
                continue
            fi = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x03 | 0x0C, 0, 0)  # FLASHW_ALL | FLASHW_TIMERNOFG
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(fi))
    except Exception as e:
        log("flash: %r" % e)


# =================================================================== actions
def find_claude():
    p = shutil.which("claude")
    if p:
        return p
    for c in (HOME / ".local" / "bin" / "claude.exe", APPDATA / "npm" / "claude.cmd"):
        if c.exists():
            return str(c)
    return None


def act_open_app(s):
    if not s.get("appId"):
        return {"ok": False, "message": "This conversation was never opened in the desktop app."}
    os.startfile("claude://claude.ai/epitaxy/" + s["appId"])
    return {"ok": True, "message": "Opened in the Claude app"}


def act_resume_terminal(s, force=False):
    cwd, cid = s.get("cwd"), s.get("cliId")
    if not cid or not UUID_RE.match(cid):
        return {"ok": False, "message": "No terminal transcript exists for this conversation."}
    if not cwd or not os.path.isdir(cwd):
        return {"ok": False, "message": "Folder no longer exists: %s" % cwd}
    if s["key"] in State.live and not force:
        where = "the Claude app" if s["kind"] == "app" else "another terminal"
        return {"ok": False, "confirm": "This conversation is open right now in %s. "
                "Resuming it again creates a second copy that continues separately. Open anyway?" % where}
    claude = find_claude()
    if not claude:
        return {"ok": False, "message": "Couldn't find the claude command. Is Claude Code CLI installed?"}
    title = re.sub(r'[&|<>^%"()!]', "", "Claude - " + (s.get("title") or cid))[:70]
    # cmd strips the outer quotes of /k "...", leaving: title X & "claude" --resume id
    cmdline = 'cmd.exe /k "title %s & "%s" --resume %s"' % (title, claude, cid)
    subprocess.Popen(cmdline, cwd=cwd, creationflags=CREATE_NEW_CONSOLE)
    return {"ok": True, "message": "Resuming in a new cmd window"}


def act_open_folder(s):
    cwd = s.get("cwd")
    if not cwd or not os.path.isdir(cwd):
        return {"ok": False, "message": "Folder no longer exists: %s" % cwd}
    os.startfile(cwd)
    return {"ok": True, "message": "Opened folder"}


def act_open_default(s, force=False):
    return act_open_app(s) if s["kind"] == "app" else act_resume_terminal(s, force)


# =================================================================== settings
def load_settings():
    try:
        s = json.loads(SETTINGS_FILE.read_text("utf-8"))
        State.settings = {k: bool(s.get(k, v)) for k, v in DEFAULT_SETTINGS.items()}
    except Exception:
        State.settings = dict(DEFAULT_SETTINGS)


def save_settings(new):
    for k in DEFAULT_SETTINGS:
        if k in new:
            State.settings[k] = bool(new[k])
    try:
        SETTINGS_FILE.write_text(json.dumps(State.settings, indent=2), "utf-8")
    except Exception as e:
        log("settings: %r" % e)


# =================================================================== server
class Handler(BaseHTTPRequestHandler):
    server_version = "SessionDeck"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _host_ok(self):
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def _auth(self, token=None):
        tok = token if token is not None else self.headers.get("X-Token", "")
        return self._host_ok() and secrets.compare_digest(tok or "", State.token)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        if u.path == "/":
            html = UI_FILE.read_bytes().replace(b"__TOKEN__", State.token.encode())
            return self._send(200, html, "text/html; charset=utf-8")
        if u.path in ("/logo.png", "/logo-512.png"):
            return self._send(200, (ASSETS / u.path.lstrip("/")).read_bytes(), "image/png")
        if u.path == "/api/events":
            return self._events(parse_qs(u.query).get("t", [""])[0])
        if not self._auth():
            return self._send(403, {"error": "forbidden"})
        State.last_ping = time.time()
        try:
            if u.path == "/api/ping":
                return self._send(200, {"ok": True})
            if u.path == "/api/sessions":
                items = build_index()
                return self._send(200, {"sessions": items, "now": time.time() * 1000, "live": State.live,
                                        "drives": drives_info(items), "settings": State.settings,
                                        "app": {"name": APP_NAME, "version": VERSION, "demo": DEMO,
                                                "projectsDir": str(PROJECTS), "found": PROJECTS.exists()}})
            if u.path == "/api/suggest":
                q = parse_qs(u.query).get("q", [""])[0][:300]
                return self._send(200, run_suggest(q))
            if u.path == "/api/analytics":
                return self._send(200, analytics())
        except Exception as e:
            log("GET %s: %r" % (u.path, e))
            return self._send(500, {"error": str(e)})
        return self._send(404, {"error": "not found"})

    def _events(self, token):
        if not self._auth(token):
            return self._send(403, {"error": "forbidden"})
        q = queue.Queue(maxsize=200)
        State.subscribers.add(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(("data: %s\n\n" % json.dumps({"type": "live", "live": State.live})).encode())
            self.wfile.flush()
            while True:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(("data: %s\n\n" % data).encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                State.last_ping = time.time()
        except Exception:
            pass
        finally:
            State.subscribers.discard(q)
            self.close_connection = True

    def do_POST(self):
        if not self._auth():
            return self._send(403, {"error": "forbidden"})
        State.last_ping = time.time()
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        if self.path == "/api/open-repo":
            os.startfile(REPO_URL)          # fixed URL, opened in the default browser
            return self._send(200, {"ok": True})
        if self.path == "/api/badge":
            b = body.get("badge")
            TaskbarIcon.badge = b if b in ("wait", "done") else None
            threading.Thread(target=TaskbarIcon.apply, daemon=True).start()
            return self._send(200, {"ok": True})
        if self.path == "/api/settings":
            save_settings(body)
            return self._send(200, {"ok": True, "settings": State.settings})
        if self.path == "/api/test-alert":
            kind = "attention" if body.get("kind") == "attention" else "done"
            s = State.settings
            item = State.by_key.get(body.get("key")) if DEMO else None     # demo tour: alert a real demo session
            broadcast({"type": "alert", "alert": kind, "key": item["key"] if item else "__test__",
                       "title": item["title"] if item else "This is a test alert", "kind": item["kind"] if item else "cli",
                       "reason": "test", "cwd": item["cwd"] if item else None, "at": time.time() * 1000})
            if s.get("sound", True):
                play_sound(kind)
            if s.get("flash", True):
                threading.Timer(1.5, flash_window).start()
            return self._send(200, {"ok": True})
        if self.path == "/api/action":
            with State.lock:
                s = State.by_key.get(body.get("key"))
            if not s:
                return self._send(404, {"ok": False, "message": "Session not found. Try refreshing."})
            action, force = body.get("action"), bool(body.get("force"))
            if DEMO:
                where = {"app": "in the Claude app", "terminal": "in a new cmd window", "folder": "its folder",
                         "open": "in the Claude app" if s["kind"] == "app" else "in a new cmd window"}.get(action, "")
                return self._send(200, {"ok": True, "message": "Demo mode: this would open %s" % where})
            try:
                res = {"open": lambda: act_open_default(s, force), "app": lambda: act_open_app(s),
                       "terminal": lambda: act_resume_terminal(s, force), "folder": lambda: act_open_folder(s),
                       }.get(action, lambda: {"ok": False, "message": "Unknown action"})()
            except Exception as e:
                log("action %s: %r" % (action, e))
                res = {"ok": False, "message": "Failed: %s" % e}
            return self._send(200, res)
        return self._send(404, {"error": "not found"})


def find_edge():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), str(LOCALAPPDATA)):
        if base:
            p = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if p.exists():
                return str(p)
    return shutil.which("msedge")


# Low-memory settings for the app window. It only ever shows this one local page, so Edge's
# browser extras (graphics acceleration, extensions, sync, updates, spare/isolated renderers)
# are switched off. Measured on Windows 10: ~250 MB -> ~125-146 MB for the window's processes.
EDGE_LOW_MEMORY_FLAGS = [
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-default-apps",
    "--no-pings",
    "--renderer-process-limit=1",
    "--process-per-site",
    "--disable-features=Translate,OptimizationHints,MediaRouter,AutofillServerCommunication,"
    "msEdgeShoppingUI,msUndersideButton,EdgeCollections,msWebAssist,"
    "SpareRendererForSitePerProcess,BackForwardCache",
]


def open_window(url):
    edge = find_edge()
    if edge:
        subprocess.Popen([edge, "--app=" + url, "--window-size=1320,880",
                          "--user-data-dir=" + str(EDGE_PROFILE), "--no-first-run",
                          "--no-default-browser-check"] + EDGE_LOW_MEMORY_FLAGS)
    else:
        import webbrowser
        webbrowser.open(url)


def existing_server():
    try:
        info = json.loads(SERVER_FILE.read_text("utf-8"))
        req = urllib.request.Request("http://127.0.0.1:%d/api/ping" % info["port"],
                                     headers={"X-Token": info["token"]})
        with urllib.request.urlopen(req, timeout=1.5) as r:
            if r.status == 200:
                return info
    except Exception:
        return None
    return None


def watchdog(server):
    while True:
        time.sleep(30)
        if not State.subscribers and time.time() - State.last_ping > IDLE_EXIT_SECONDS:
            server.shutdown()
            return


def main():
    if "--version" in sys.argv:
        print("%s %s" % (APP_NAME, VERSION))
        return
    info = existing_server()
    if info:
        open_window("http://127.0.0.1:%d/" % info["port"])
        return
    load_settings()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    SERVER_FILE.write_text(json.dumps({"port": port, "token": State.token, "pid": os.getpid()}), "utf-8")
    try:
        build_index()
    except Exception as e:
        log("initial index: %r" % e)
    threading.Thread(target=monitor, daemon=True).start()
    threading.Thread(target=TaskbarIcon.loop, daemon=True).start()
    threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    if "--no-window" not in sys.argv:
        open_window("http://127.0.0.1:%d/" % port)
    try:
        server.serve_forever()
    finally:
        try:
            SERVER_FILE.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("fatal: %r" % e)
        raise
