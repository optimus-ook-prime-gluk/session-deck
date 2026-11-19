"""Captures README screenshots from a running `--demo` instance (Windows only).

Usage: start the demo first (python src/session_deck.py --demo --no-window), then
       python tools/screenshots.py
Each shot opens an Edge app window on the demo, waits for it to render and saves the window
with PrintWindow, so nothing else on screen ends up in the picture.
"""
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs"
SERVER = Path(os.environ["LOCALAPPDATA"]) / "SessionDeck" / "demo" / "server.json"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
SHOTS = [
    ("tiles.png", "view=convs&layout=tiles&theme=light"),
    ("tiles-dark.png", "view=convs&layout=tiles&theme=dark"),
    ("search.png", "view=convs&layout=tiles&theme=light&q=that%20battery%20thesis%20slides"),
    ("insights.png", "view=insights&period=week&theme=light"),
]

u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
u32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # per-monitor v2: real pixels


def find_window(title):
    found = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        n = u32.GetWindowTextLengthW(h)
        if n and u32.IsWindowVisible(h):
            b = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(h, b, n + 1)
            if b.value.endswith(title):
                found.append(h)
        return True
    u32.EnumWindows(proto(cb), 0)
    return found[0] if found else None


def capture(hwnd, path):
    r = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc = u32.GetWindowDC(hwnd)
    mdc = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, h)
    g32.SelectObject(mdc, bmp)
    u32.PrintWindow(hwnd, mdc, 2)                              # PW_RENDERFULLCONTENT
    buf = ctypes.create_string_buffer(w * h * 4)

    class BMIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("a", wintypes.LONG), ("b", wintypes.LONG),
                    ("c", wintypes.DWORD), ("d", wintypes.DWORD)]
    bi = BMIH(ctypes.sizeof(BMIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    g32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    g32.DeleteObject(bmp); g32.DeleteDC(mdc); u32.ReleaseDC(hwnd, hdc)
    img.save(path, optimize=True)
    return img.size


def grab(hwnd):
    r = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc = u32.GetWindowDC(hwnd)
    mdc = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, h)
    g32.SelectObject(mdc, bmp)
    u32.PrintWindow(hwnd, mdc, 2)
    buf = ctypes.create_string_buffer(w * h * 4)
    g32.GetBitmapBits(bmp, len(buf), buf)
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    g32.DeleteObject(bmp); g32.DeleteDC(mdc); u32.ReleaseDC(hwnd, hdc)
    return img


def window_title(hwnd):
    n = u32.GetWindowTextLengthW(hwnd)
    b = ctypes.create_unicode_buffer(n + 1)
    u32.GetWindowTextW(hwnd, b, n + 1)
    return b.value


def open_app(url, profile):
    subprocess.Popen([EDGE, "--app=" + url, "--window-size=1440,900", "--window-position=40,40",
                      "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
                      "--autoplay-policy=no-user-gesture-required"])
    for _ in range(60):
        time.sleep(0.25)
        hwnd = find_window("Session Deck")
        if hwnd:
            return hwnd
    return None


def close(hwnd):
    u32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    time.sleep(1.5)


def record(url, profile, out, fps=12, width=1280, timeout=120):
    """Records the scripted tour until the page sets its title to 'Tour done'."""
    hwnd = open_app(url, profile)
    if not hwnd:
        print("window not found")
        return
    frames, stamps, last = [], [], None
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = time.time()
        img = grab(hwnd)
        img = img.resize((width, int(img.height * width / img.width)), Image.LANCZOS)
        if last is not None and img.tobytes() == last.tobytes():
            stamps[-1][1] = t                   # identical frame: just extend the previous one
        else:
            frames.append(img)
            stamps.append([t, t])
            last = img
        if "Tour done" in window_title(hwnd):
            break
        time.sleep(max(0, 1.0 / fps - (time.time() - t)))
    end = time.time()
    durations = []
    for i, (s, _) in enumerate(stamps):
        nxt = stamps[i + 1][0] if i + 1 < len(stamps) else end
        durations.append(max(40, int((nxt - s) * 1000)))
    close(hwnd)
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=durations, loop=0,
                   quality=72, method=4)
    print("video:", out, len(frames), "frames,", round(end - t0, 1), "s,", round(out.stat().st_size / 1e6, 1), "MB")


def main():
    info = json.loads(SERVER.read_text("utf-8"))
    profile = tempfile.mkdtemp(prefix="sd-shots-")
    OUT.mkdir(exist_ok=True)
    base = "http://127.0.0.1:%d/" % info["port"]
    warm = open_app(base, profile)          # first window of a fresh profile may show Edge's welcome UI
    if warm:
        time.sleep(4)
        close(warm)
    if "--video" in sys.argv:
        record(base + "?view=convs&layout=tiles&theme=light&tour=1", profile, OUT / "demo.webp")
        return
    for name, query in SHOTS:
        hwnd = open_app(base + "?" + query, profile)
        if not hwnd:
            print("window not found for", name)
            continue
        time.sleep(5)                       # let data, charts and suggestions load
        print(name, capture(hwnd, OUT / name))
        close(hwnd)


if __name__ == "__main__":
    sys.exit(main())
