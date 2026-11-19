"""Offline "ask in plain words" suggestions for Claude Sessions.

Everything here runs locally with the Python standard library: no network, no API calls,
no downloaded models.

How a request like "open that oauth thing from yesterday in the terminal" is handled:
  1. Understand the hints: time phrases (yesterday, last week, on monday, 3 days ago...),
     where it ran (app / terminal), drive (F drive), live state (running, waiting).
  2. What's left becomes search words, minus filler ("open", "that", "thing"...).
     Each word is lightly stemmed and expanded with
       - related words from a small built-in list (ppt -> presentation, slides, deck),
       - typo-tolerant matches from your own vocabulary (Python difflib),
       - prefix matches ("jail" -> "jailbreak").
  3. Every conversation is scored with Okapi BM25 over its title, folder, prompts and
     Claude's replies (fields weighted), then nudged by the hints and by recency.
  4. The top few come back with the reasons they matched and a snippet.

Credits
  * Ranking: Okapi BM25 - S. E. Robertson, S. Walker, S. Jones, M. Hancock-Beaulieu and
    M. Gatford, "Okapi at TREC-3" (1994), building on Robertson & Sparck Jones' probabilistic
    relevance framework. Implemented here from the published formula.
  * Typo matching: Python's standard-library difflib (Ratcliff/Obershelp "gestalt pattern
    matching").
  * The idea of searching past Claude Code sessions to resume them is shared with community
    projects such as claude-smart-fork, claude-code-search, sessfind, session-search and
    session-seek. No code from them is used here.
"""
import difflib
import math
import re
import time
from datetime import datetime, timedelta

K1, B = 1.2, 0.75
FIELD_WEIGHTS = {"title": 3.0, "folder": 2.5, "first": 1.5, "last": 1.5, "u": 1.0, "a": 0.45}

STOP = set("""
a an the this that these those it its it's is are was were be been being am i me my mine we us our ours you your
he she they them their his her do does did done doing have has had having can could would should will shall may
might must of on in at to from for with without by about into onto over under up down out off as than then so
and or but if when where which what who whom whose why how there here just also too very really quite some any
all each every more most much many few lot lots bit little kind sort like such thing things stuff something
anything one ones session sessions conversation conversations chat chats convo thread threads
open opened opening find show get take bring go goes going went pull load let lets please pls plz want wanna
need resume continue back again earlier before after while during around regarding related relating
worked work workin talking talked asked ask told said discuss discussed did made make making
claude remember think thought guess maybe probably think
""".split())

# Small, editable list of related words (both directions). Words are matched after stemming.
SYNONYM_GROUPS = [
    "ppt pptx powerpoint presentation slide slides deck keynote",
    "doc docx word document report writeup write-up",
    "xlsx excel spreadsheet sheet csv table",
    "pdf pdfs",
    "picture image photo screenshot png jpg",
    "chart graph plot diagram visual visualization figure",
    "ereader e-reader ebook kindle",
    "tv television",
    "game gaming games mod mods",
    "bug error issue problem fix crash broken failing failure flaky",
    "website site webpage portfolio homepage landing",
    "cv resume",
    "test testing tests unittest pytest jest",
    "email mail inbox",
    "install installation setup set-up configure",
    "auth authentication login signin sign-in oauth",
    "db database sql postgres mysql sqlite",
    "deploy deployment release ship publish",
    "api endpoint endpoints rest",
    "ui frontend interface screen layout",
    "refactor cleanup clean-up restructure",
    "docs documentation readme",
    "ci pipeline actions workflow",
    "perf performance speed slow faster optimize",
    "proposal pitch quote quotation offer",
    "skill skills plugin plugins extension",
    "gpu nvidia driver drivers graphics",
    "note notes notepad todo todos",
]

NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "couple": 2, "few": 3, "several": 4}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*")


def stem(w):
    if len(w) <= 3 or w.isdigit():
        return w
    for suf, rep in (("ies", "y"), ("ing", ""), ("ed", "")):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)] + rep
    if w.endswith("es") and w[-3:-2] in ("s", "x", "z") or w.endswith(("ches", "shes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 4:
        return w[:-1]
    return w


def tokens(text):
    out = []
    for m in WORD_RE.finditer(text.lower()):
        w = m.group(0).replace("'", "")
        parts = re.split(r"[-]", w)
        if len(parts) > 1:
            out.append("".join(parts))
        for p in parts:
            # underscores and other separators are already split by the regex
            if len(p) >= 2:
                out.append(p)
    return [stem(t) for t in out]


SYN = {}
SURFACE = {}          # stem -> readable word, for explaining related-word matches
for g in SYNONYM_GROUPS:
    for word in g.split():
        for t in tokens(word):
            SURFACE.setdefault(t, word)
    ws = set(tokens(g))
    for w in ws:
        SYN.setdefault(w, set()).update(ws - {w})


# ------------------------------------------------------------------ query understanding
def _day_bounds(d):
    s = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return s, s + timedelta(days=1)


def parse_query(q, now=None):
    """Pull out hints; returns dict with remaining text and hint fields."""
    now = datetime.fromtimestamp(now or time.time())
    text = " " + q.lower().strip().lstrip("?") + " "
    hints = {"window": None, "timeLabel": None, "kind": None, "drive": None, "state": None, "recent": False}

    def take(pattern, fn):
        nonlocal text
        m = re.search(pattern, text)
        if m:
            fn(m)
            text = text[:m.start()] + " " + text[m.end():]
            return True
        return False

    def set_win(s, e, label):
        if hints["window"] is None:
            hints["window"] = (s.timestamp(), e.timestamp())
            hints["timeLabel"] = label

    today0, today1 = _day_bounds(now)
    parts_of_day = {"morning": (5, 12), "afternoon": (12, 17), "evening": (17, 24), "night": (18, 28)}

    def part(day0, which, label):
        a, b = parts_of_day[which]
        set_win(day0 + timedelta(hours=a), day0 + timedelta(hours=b), label)

    take(r"\bday before yesterday\b", lambda m: set_win(today0 - timedelta(days=2), today0 - timedelta(days=1), "Day before yesterday"))
    take(r"\blast night\b", lambda m: part(today0 - timedelta(days=1), "night", "Last night"))
    take(r"\byesterday (morning|afternoon|evening|night)\b", lambda m: part(today0 - timedelta(days=1), m.group(1), "Yesterday " + m.group(1)))
    take(r"\b(?:this|today) (morning|afternoon|evening)\b|\btonight\b",
         lambda m: part(today0, m.group(1) or "evening", "This " + (m.group(1) or "evening")))
    take(r"\b(?:earlier )?today\b", lambda m: set_win(today0, today1, "Today"))
    take(r"\byesterday\b", lambda m: set_win(today0 - timedelta(days=1), today0, "Yesterday"))
    take(r"\b(\d+|a|an|one|two|three|four|five|six|seven|couple(?: of)?|few|several) days? ago\b",
         lambda m: _days_ago(m, today0, set_win))
    take(r"\b(\d+|a|one|two|three|couple(?: of)?|few) weeks? ago\b", lambda m: _weeks_ago(m, today0, set_win))
    take(r"\bthis week\b", lambda m: set_win(today0 - timedelta(days=now.weekday()), today1, "This week"))
    take(r"\blast week\b", lambda m: set_win(today0 - timedelta(days=now.weekday() + 7),
                                             today0 - timedelta(days=now.weekday()), "Last week"))
    take(r"\bthis month\b", lambda m: set_win(today0.replace(day=1), today1, "This month"))
    take(r"\blast month\b", lambda m: _last_month(today0, set_win))
    take(r"\b(?:on |last |this past )?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
         lambda m: _weekday(m.group(1), today0, now, set_win))
    take(r"\b(\d{1,2})(?:st|nd|rd|th)? (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|"
         r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* (\d{1,2})(?:st|nd|rd|th)?\b",
         lambda m: _date(m, now, set_win))
    take(r"\b(most recent|latest|newest|last one|last time|just now|recently|recent)\b",
         lambda m: hints.__setitem__("recent", True))

    take(r"\b(?:in|on|from|using|via|inside)?\s*(?:the |a |my )?(terminal|cli|cmd|command line|command prompt|console|powershell)\b",
         lambda m: hints.__setitem__("kind", "cli"))
    take(r"\b(?:in|on|from|using|via|inside)\s+(?:the |my )?(?:claude )?(?:desktop )?app\b|\b(?:claude|desktop) app\b|\bin (?:the )?desktop\b",
         lambda m: hints.__setitem__("kind", "app"))
    take(r"\b(?:on |in |from )?(?:the |my )?([a-z])\s*(?:drive|:\\?)(?=\s)", lambda m: hints.__setitem__("drive", m.group(1).upper() + ":"))
    take(r"\b(waiting|needs? (?:me|my|permission|approval|input|attention)|asking for permission)\b",
         lambda m: hints.__setitem__("state", "waiting"))
    take(r"\b(running|live|open right now|currently open|still open|in progress|working on now)\b",
         lambda m: hints.__setitem__("state", "live"))
    hints["rest"] = " ".join(text.split())
    return hints


def _num(s):
    s = s.replace(" of", "")
    return int(s) if s.isdigit() else NUM_WORDS.get(s, 1)


def _days_ago(m, today0, set_win):
    n = _num(m.group(1))
    if m.group(1) in ("few", "several", "couple", "couple of"):
        set_win(today0 - timedelta(days=n + 2), today0 - timedelta(days=max(1, n - 1)) + timedelta(days=1), "%s days ago" % m.group(1).replace(" of", ""))
    else:
        d0 = today0 - timedelta(days=n)
        set_win(d0 - timedelta(days=1), d0 + timedelta(days=2), "%d day%s ago" % (n, "s" if n > 1 else ""))


def _weeks_ago(m, today0, set_win):
    n = _num(m.group(1))
    set_win(today0 - timedelta(days=7 * n + 4), today0 - timedelta(days=7 * n - 4), "%d week%s ago" % (n, "s" if n > 1 else ""))


def _last_month(today0, set_win):
    first = today0.replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    set_win(prev, first, "Last month")


def _weekday(name, today0, now, set_win):
    target = WEEKDAYS.index(name)
    delta = (now.weekday() - target) % 7 or 7
    d0 = today0 - timedelta(days=delta)
    set_win(d0, d0 + timedelta(days=1), d0.strftime("%A %d %b"))


def _date(m, now, set_win):
    day = int(m.group(1) or m.group(4))
    mon = MONTHS[(m.group(2) or m.group(3))[:3]]
    try:
        d0 = datetime(now.year, mon, day)
        if d0 > now:
            d0 = datetime(now.year - 1, mon, day)
    except ValueError:
        return
    set_win(d0, d0 + timedelta(days=1), d0.strftime("%d %b"))


# ------------------------------------------------------------------ index
class Doc:
    """One conversation: small 'head' fields (title, folder, prompts) + the growing body text."""
    __slots__ = ("key", "tf", "dl", "head_sig", "head_tf", "body_tf", "lines", "path", "off")

    def __init__(self, key, path):
        self.key, self.path, self.off = key, path, 0
        self.head_sig, self.head_tf, self.body_tf, self.lines = None, {}, {}, []
        self.tf, self.dl = {}, 0.0

    def set_head(self, s):
        sig = (s.get("title"), s.get("cwd"), s.get("first"), s.get("last"))
        if sig == self.head_sig:
            return False
        tf = {}
        for fname, text in (("title", s.get("title") or ""),
                            ("folder", re.sub(r"^[A-Za-z]:", " ", s.get("cwd") or "")),
                            ("first", s.get("first") or ""), ("last", s.get("last") or "")):
            w = FIELD_WEIGHTS[fname]
            for t in tokens(text):
                tf[t] = tf.get(t, 0.0) + w
        self.head_sig, self.head_tf = sig, tf
        return True

    def read_body(self):
        """Read only text appended to this conversation's local text file since last time."""
        if not self.path:
            return False
        try:
            size = self.path.stat().st_size
        except OSError:
            return False
        if size < self.off:
            self.off, self.body_tf, self.lines = 0, {}, []
        if size == self.off:
            return False
        with open(self.path, "rb") as f:
            f.seek(self.off)
            data = f.read(size - self.off)
        cut = data.rfind(b"\n")
        if cut < 0:
            return False
        for raw in data[:cut].decode("utf-8", "replace").split("\n"):
            field, _, text = raw.partition("\t")
            if field not in ("u", "a") or not text:
                continue
            self.lines.append((field, text))
            w = FIELD_WEIGHTS[field]
            for t in tokens(text):
                self.body_tf[t] = self.body_tf.get(t, 0.0) + w
        self.off += cut + 1
        return True

    def combine(self):
        tf = dict(self.body_tf)
        for t, v in self.head_tf.items():
            tf[t] = tf.get(t, 0.0) + v
        self.tf, self.dl = tf, sum(tf.values())


class Index:
    def __init__(self):
        self.docs = {}
        self.df = {}
        self.avgdl = 1.0
        self.vocab = []

    def update(self, items, path_for):
        """items: session dicts; path_for(item) -> Path of its local text file (or None)."""
        changed = False
        keep = set()
        for s in items:
            key = s["key"]
            keep.add(key)
            p = path_for(s)
            d = self.docs.get(key)
            if d is None or d.path != p:
                d = self.docs[key] = Doc(key, p)
            a = d.set_head(s)
            b = d.read_body()
            if a or b:
                d.combine()
                changed = True
        for k in [k for k in self.docs if k not in keep]:
            del self.docs[k]
            changed = True
        if changed:
            df = {}
            for d in self.docs.values():
                for t in d.tf:
                    df[t] = df.get(t, 0) + 1
            self.df = df
            self.avgdl = (sum(d.dl for d in self.docs.values()) / len(self.docs)) if self.docs else 1.0
            self.vocab = [t for t in df if len(t) >= 3 and not t.isdigit()]

    def idf(self, t):
        n, df = len(self.docs), self.df.get(t, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def expand(self, term):
        """[(token, weight, how)] for one query term."""
        out = [(term, 1.0, None)]
        for s in SYN.get(term, ()):
            if s in self.df:
                out.append((s, 0.6, "related"))
        if term not in self.df and len(term) >= 4:
            for c in difflib.get_close_matches(term, self.vocab, n=3, cutoff=0.8):
                out.append((c, 0.7, "typo"))
        if len(term) >= 4:
            pref = [v for v in self.vocab if v != term and v.startswith(term)]
            pref.sort(key=lambda v: -self.df.get(v, 0))
            for v in pref[:4]:
                out.append((v, 0.75, "prefix"))
        seen, uniq = set(), []
        for t, w, how in out:
            if t not in seen:
                seen.add(t)
                uniq.append((t, w, how))
        return uniq


# ------------------------------------------------------------------ scoring
def suggest(query, items, index, intervals, live, limit=4, now=None):
    now = now or time.time()
    h = parse_query(query, now)
    raw_words = [w for w in WORD_RE.findall(h["rest"].lower())]
    terms, shown = [], []
    for w in raw_words:
        wl = w.replace("'", "")
        if wl in STOP or len(wl) < 2:
            continue
        for t in tokens(wl):
            if t not in STOP and t not in terms:
                terms.append(t)
                shown.append(wl)
    expansions = {t: index.expand(t) for t in terms}

    by_key = {s["key"]: s for s in items}
    results = []
    for key, d in index.docs.items():
        s = by_key.get(key)
        if not s:
            continue
        score, matched = 0.0, {}
        norm = K1 * (1 - B + B * d.dl / (index.avgdl or 1))
        for t in terms:
            best = 0.0
            for tok, w, how in expansions[t]:
                f = d.tf.get(tok)
                if not f:
                    continue
                part = w * index.idf(tok) * f * (K1 + 1) / (f + norm)
                if part > best:
                    best = part
                    matched[t] = (tok, how)
            score += best
        coverage = (len(matched) / len(terms)) if terms else 0.0
        if terms:
            if not matched:
                continue
            score *= 0.35 + 0.65 * coverage ** 1.5
        reasons = []
        # time window
        if h["window"]:
            ws, we = h["window"]
            ivs = intervals.get(key, [])
            active = sum(max(0, min(e, we) - max(st, ws)) for st, e in ivs)
            touched = active > 0 or any(st <= we and e >= ws for st, e in ivs) or ws <= (s.get("lastActive") or 0) / 1000 <= we
            if terms:
                score *= 2.2 if touched else 0.3
            else:
                if not touched:
                    continue
                score = 1.0 + math.log1p(active / 60.0)
            if touched:
                reasons.append(("time", "Active " + h["timeLabel"].lower() if not h["timeLabel"][0].isdigit() else "Active " + h["timeLabel"]))
        elif not terms:
            score = 1.0
        # where it ran
        if h["kind"]:
            if s["kind"] == h["kind"]:
                score *= 1.8
                reasons.append(("kind", "Terminal session" if h["kind"] == "cli" else "Claude app session"))
            else:
                score *= 0.25
        if h["drive"]:
            if (s.get("cwd") or "").upper().startswith(h["drive"]):
                score *= 1.8
                reasons.append(("drive", "On " + h["drive"] + " drive"))
            else:
                score *= 0.25
        if h["state"]:
            L = live.get(key)
            ok = bool(L) if h["state"] == "live" else bool(L and L.get("state") == "waiting")
            if ok:
                score *= 2.0
                reasons.append(("state", "Live now" if h["state"] == "live" else "Waiting for you"))
            else:
                score *= 0.3
        # recency prior
        age_days = max(0.0, (now - (s.get("lastActive") or 0) / 1000) / 86400)
        score *= 1 + (0.9 if h["recent"] else 0.2) * math.exp(-age_days / (2 if h["recent"] else 6))
        if s.get("archived"):
            score *= 0.6
        if score <= 0:
            continue
        results.append((score, key, matched, reasons))

    results.sort(key=lambda r: r[0], reverse=True)
    top = results[:limit]
    out = []
    best = top[0][0] if top else 1
    second = top[1][0] if len(top) > 1 else 0
    top = [r for r in top if r[0] >= 0.2 * best] if top else top
    for i, (score, key, matched, reasons) in enumerate(top):
        d = index.docs[key]
        words, done_orig = [], set()
        for t in terms:
            if t in matched:
                tok, how = matched[t]
                orig = shown[terms.index(t)]
                if orig in done_orig:
                    continue
                done_orig.add(orig)
                if how == "related":
                    w = "%s ≈ %s" % (orig, SURFACE.get(tok, tok))
                elif how == "typo":
                    w = "%s → %s" % (orig, tok)
                else:
                    w = orig
                if w not in words:
                    words.append(w)
        if words:
            reasons.insert(0, ("words", "Matched " + ", ".join(words)))
        out.append({
            "key": key,
            "score": round(score, 3),
            "confidence": round(score / best, 3) if best else 0,
            "reasons": [r[1] for r in reasons],
            "snippet": snippet(d, [m[0] for m in matched.values()]),
            "hl": sorted({m[0] for m in matched.values()}),
        })
    return {
        "items": out,
        "best": bool(out) and (second == 0 or best >= 1.5 * second),
        "parsed": {"terms": list(dict.fromkeys(shown)), "time": h["timeLabel"], "kind": h["kind"], "drive": h["drive"],
                   "state": h["state"], "recent": h["recent"]},
    }


def snippet(doc, toks, width=170):
    if not toks:
        return None
    want = set(toks)
    best, best_n, best_field = None, 0, None
    for field, line in doc.lines:
        lt = tokens(line)
        n = len(want.intersection(lt))
        if n > best_n or (n == best_n and n and field == "u" and best_field != "u"):
            best, best_n, best_field = line, n, field
            if n == len(want) and field == "u":
                break
    if not best:
        return None
    low = best.lower()
    pos = min((low.find(t[:4]) for t in want if low.find(t[:4]) >= 0), default=0)
    start = max(0, pos - 60)
    text = ("…" if start else "") + best[start:start + width] + ("…" if start + width < len(best) else "")
    return {"text": text, "who": "You" if best_field == "u" else "Claude"}
