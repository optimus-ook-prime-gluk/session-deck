"""Core tests for Session Deck. Run:  python -m unittest discover -s tests -v

Everything runs against the made-up demo data in a temporary folder - never real sessions.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
TMP = tempfile.mkdtemp(prefix="sessiondeck-test-")
os.environ["SESSIONDECK_DATA"] = TMP
sys.argv = [sys.argv[0], "--demo"]
sys.path.insert(0, str(SRC))

import session_deck as sd  # noqa: E402
import suggest  # noqa: E402


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class IndexTests(unittest.TestCase):
    def test_demo_sessions_are_indexed(self):
        items = sd.build_index()
        self.assertEqual(len(items), 12)
        self.assertEqual(sum(s["kind"] == "app" for s in items), 6)
        self.assertEqual(sum(s["kind"] == "cli" for s in items), 6)
        for s in items:
            self.assertTrue(s["title"])
            self.assertGreater(s["tokens"], 0)
            self.assertGreater(s["turns"], 0)
        # newest first
        self.assertEqual(items, sorted(items, key=lambda s: s["lastActive"], reverse=True))

    def test_app_sessions_map_to_transcripts(self):
        items = sd.build_index()
        for s in items:
            if s["kind"] == "app":
                self.assertTrue(s["appId"].startswith("local_"))
                self.assertTrue(s["cliId"])

    def test_live_sessions(self):
        live = sd.read_live()
        self.assertEqual(sorted(r["status"] for r in live), ["busy", "waiting"])

    def test_incremental_parse_matches_full(self):
        f = next(sd.PROJECTS.glob("*/*.jsonl"))
        size = f.stat().st_size
        full = sd.parse_file(f, None, size)
        st = None
        for frac in (0.2, 0.21, 0.5, 0.8, 1.0):
            st = sd.parse_file(f, st, int(size * frac))
        for k in ("cwd", "entry", "first", "ta", "last", "model", "turns"):
            self.assertEqual(full[k], st[k], k)
        self.assertEqual(sd.days_with_pending(full), sd.days_with_pending(st))

    def test_analytics(self):
        a = sd.analytics()
        self.assertEqual(len(a["daily"]), 14)
        self.assertGreater(a["all"]["tokens"]["total"], 0)
        self.assertGreater(a["all"]["time"], 0)


class SuggestTests(unittest.TestCase):
    def top_title(self, q):
        res = sd.run_suggest(q)
        self.assertTrue(res["items"], q)
        return sd.State.by_key[res["items"][0]["key"]]["title"]

    def test_plain_words(self):
        self.assertEqual(self.top_title("that battery thesis slides"), "Thesis chapter 3 outline")
        self.assertEqual(self.top_title("the github oauth thing"), "Add OAuth login with GitHub")
        self.assertEqual(self.top_title("csv import was slow"), "Speed up nightly CSV import")

    def test_typo(self):
        self.assertEqual(self.top_title("textur convertr"), "Texture converter for game mods")

    def test_hints(self):
        h = suggest.parse_query("open the thing from yesterday in the terminal on d drive")
        self.assertEqual(h["kind"], "cli")
        self.assertEqual(h["drive"], "D:")
        self.assertEqual(h["timeLabel"], "Yesterday")
        res = sd.run_suggest("what was I doing yesterday in the terminal")
        for it in res["items"]:
            self.assertEqual(sd.State.by_key[it["key"]]["kind"], "cli")


class UiTests(unittest.TestCase):
    def test_ui_script_syntax(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        html = (SRC / "ui.html").read_text("utf-8")
        js = html[html.index("<script>") + 8: html.rindex("</script>")]
        path = Path(TMP) / "ui.js"
        path.write_text(js, "utf-8")
        r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
