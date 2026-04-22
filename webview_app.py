"""webview_app.py - pywebview GUI layer untuk Bot Manage Listing.

Embed `Bot Manage Listing.html` via pywebview. Tidak recreate UI dari nol -
HTML adalah final UI, Python cuma:
  1. Launch native window yg load HTML
  2. Expose `BotAPI` ke JS (js_api) -> handle button clicks
  3. Push state (logs, workers, bots, stats, connected) dari BotContext ke JS
     setiap 500ms via `window.evaluate_js`.

Bridge contract (lihat HTML yg sudah di-patch):
  - `window.pushLogBatch(entries[])`    -> dispatch 'bot-log-batch' CustomEvent
  - `window.setAppState(snapshot)`       -> dispatch 'bot-state' CustomEvent
  - `window.pywebview.api.<method>()`    -> callable dari JS
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser

import webview

from shared import SCRIPT_DIR, LOG_DIR, BOT_NAMES


def _resolve_html_path():
    """Find HTML UI in dev OR frozen (PyInstaller onefile extracts to _MEIPASS).
    Order: _MEIPASS (frozen bundle) -> SCRIPT_DIR -> alongside this module."""
    fname = "Bot Manage Listing.html"
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, fname))
    candidates.append(os.path.join(SCRIPT_DIR, fname))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, fname))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return candidates[0]


def _resolve_icon_path():
    """Cari icon.ico (dev: SCRIPT_DIR, frozen: _MEIPASS)."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "icon.ico"))
    candidates.append(os.path.join(SCRIPT_DIR, "icon.ico"))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "icon.ico"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


HTML_PATH = _resolve_html_path()
ICON_PATH = _resolve_icon_path()

# AppUserModelID: kasih taskbar group identity sendiri biar Windows pakai
# icon kita (tanpa ini taskbar kadang warisi icon Python host).
try:
    import ctypes as _ct
    _ct.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
        "GameMarket.BotManageListing"
    )
except Exception:
    pass


def _apply_window_icon(title, ico_path):
    """Set window + taskbar icon lewat Win32. pywebview tidak expose parameter
    icon di create_window(). Kombinasi WM_SETICON (window title bar) dan
    SetClassLongPtrW dgn GCLP_HICON/GCLP_HICONSM (class-level, biar taskbar
    ikut). Retry 20x karena HWND bisa belum siap saat 'shown' event.

    NOTE: LoadImageW tidak support PNG-compressed .ico. Pakai
    LoadIconWithScaleDown (comctl32, Vista+) yang support PNG entry.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return
    user32 = ctypes.windll.user32
    WM_SETICON = 0x0080
    ICON_SMALL = 0
    ICON_BIG = 1
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010
    LR_DEFAULTSIZE = 0x00000040
    GCLP_HICON = -14
    GCLP_HICONSM = -34

    def _load_icon_scaled(w, h):
        """Coba LoadIconWithScaleDown (support PNG), fallback ke LoadImageW."""
        try:
            comctl = ctypes.windll.comctl32
            comctl.LoadIconWithScaleDown.argtypes = [
                wintypes.HINSTANCE, wintypes.LPCWSTR,
                ctypes.c_int, ctypes.c_int, ctypes.POINTER(wintypes.HICON)
            ]
            comctl.LoadIconWithScaleDown.restype = ctypes.c_long  # HRESULT
            h_out = wintypes.HICON()
            hr = comctl.LoadIconWithScaleDown(None, ico_path, w, h, ctypes.byref(h_out))
            if hr == 0 and h_out.value:
                return h_out.value
        except Exception:
            pass
        try:
            user32.LoadImageW.restype = wintypes.HANDLE
            h = user32.LoadImageW(None, ico_path, IMAGE_ICON, w, h,
                                   LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                return h
        except Exception:
            pass
        return 0

    hicon_small = _load_icon_scaled(16, 16)
    hicon_big = _load_icon_scaled(32, 32)
    if not hicon_small and not hicon_big:
        return

    # Pointer-sized argtypes biar SetClassLongPtrW menerima HICON 64-bit di x64.
    try:
        user32.SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.SetClassLongPtrW.restype = ctypes.c_void_p
    except Exception:
        pass

    for _ in range(20):
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            try:
                if hicon_small:
                    user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
                    try: user32.SetClassLongPtrW(hwnd, GCLP_HICONSM, hicon_small)
                    except Exception: pass
                if hicon_big:
                    user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
                    try: user32.SetClassLongPtrW(hwnd, GCLP_HICON, hicon_big)
                    except Exception: pass
            except Exception:
                pass
            return
        time.sleep(0.2)

# Format: [HH:MM:SS] [LEVEL] [Wn] msg  (LEVEL & Wn optional)
_LOG_RE = re.compile(
    r"^\[(\d{2}:\d{2}:\d{2})\]\s*(?:\[([A-Z]+)\]\s*)?(?:\[(W\d+)\]\s*)?(.*)$"
)

# Levels yang dikenal UI; selain itu fallback ke 'SYS'.
_KNOWN_LEVELS = {"APP", "DELETE", "CREATE", "DISKON", "ERR", "OK", "WARN", "SYS"}

_BOT_KEYS_UI = ("DELETE", "CREATE", "DISKON")   # uppercase -> cocok UI
_RUNNING_PHASES = ("running", "scanning", "processing")


def parse_log_line(raw):
    """Parse 1 log line ke dict {time, level, msg} untuk UI.
    Kalau regex gagal, fallback ke level='SYS' dengan msg mentah."""
    m = _LOG_RE.match(raw)
    if not m:
        return {"time": time.strftime("%H:%M:%S"), "level": "SYS", "msg": raw}
    ts, level, wid, msg = m.group(1), m.group(2) or "", m.group(3) or "", m.group(4) or ""
    if level not in _KNOWN_LEVELS:
        level = "SYS"
    if wid:
        msg = f"[{wid}] {msg}"
    return {"time": ts, "level": level, "msg": msg}


def _js_escape(obj):
    """JSON-serialize to a safe JS literal. ensure_ascii=False to preserve
    Indonesian chars; still safe because json-encoded strings."""
    return json.dumps(obj, ensure_ascii=False, default=str)


class BotAPI:
    """Methods exposed to JS via `window.pywebview.api.<name>()`.
    pywebview auto-converts method names (no dash/dots). All methods return
    JSON-serializable dicts or primitives."""

    def __init__(self, ctx, window_ref):
        self.ctx = ctx
        self._window_ref = window_ref  # mutable holder: [window] (set after create)

    def _log(self, msg):
        self.ctx.logger.log("app", msg)

    # ---------- Buttons ----------
    def test_connection(self):
        self._log("Test Connection dimulai...")
        chrome_ok = False
        sheets_ok = False
        try:
            chrome_ok = self.ctx.chrome.is_alive()
            self._log(f"Chrome port {self.ctx.chrome.debug_port} "
                       f"{'alive' if chrome_ok else 'mati'}")
        except Exception as e:
            self._log(f"Chrome check error: {e}")
        try:
            sp = getattr(self.ctx.sheets, "spreadsheet", None)
            if sp is not None:
                sheets_ok = True
                try:
                    title = sp.title
                    self._log(f"Sheets terhubung: {title}")
                except Exception:
                    self._log("Sheets terhubung")
            else:
                self._log("Sheets belum connect")
        except Exception as e:
            self._log(f"Sheets error: {str(e)[:120]}")
        ok = chrome_ok and sheets_ok
        return {"ok": ok, "chrome": chrome_ok, "sheets": sheets_ok}

    def force_scan(self):
        self.ctx.force_scan = True
        self._log("Force Scan - skip idle wait, prescan ulang")
        return {"ok": True}

    def open_log_folder(self):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            os.startfile(LOG_DIR)
            return {"ok": True}
        except Exception as e:
            self._log(f"Gagal buka folder log: {e}")
            return {"ok": False, "error": str(e)}

    def open_sheets(self):
        sid = self.ctx.config.get("SPREADSHEET_ID", "")
        if not sid:
            self._log("SPREADSHEET_ID kosong")
            return {"ok": False, "error": "SPREADSHEET_ID kosong"}
        url = f"https://docs.google.com/spreadsheets/d/{sid}"
        try:
            chrome = self.ctx.config.get("CHROME_PATH", "")
            if chrome and os.path.isfile(chrome):
                subprocess.Popen([chrome, url])
            else:
                webbrowser.open(url)
            return {"ok": True, "url": url}
        except Exception as e:
            self._log(f"Gagal buka sheet: {e}")
            return {"ok": False, "error": str(e)}

    def open_url(self, url):
        if not url:
            return {"ok": False}
        try:
            webbrowser.open(url)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- Toggles / workers settings ----------
    def toggle_bot(self, name, enabled):
        key = str(name or "").lower()
        if key not in BOT_NAMES:
            return {"ok": False, "error": f"unknown bot: {name}"}
        self.ctx.toggles.set(key, bool(enabled))
        self._log(f"Bot {key.upper()} {'dinyalakan' if enabled else 'dimatikan'}")
        return {"ok": True}

    def set_max_create(self, value):
        return self._set_worker_count("CREATE_MAX_WORKER", value)

    def set_max_diskon(self, value):
        return self._set_worker_count("DISKON_MAX_WORKER", value)

    def _set_worker_count(self, key, value):
        try:
            v = max(1, min(100, int(value)))
        except Exception:
            return {"ok": False, "error": "invalid int"}
        self.ctx.config.set(key, str(v))
        self._log(f"{key} = {v} (apply next cycle)")
        return {"ok": True, "value": v}

    # ---------- Window controls (custom title bar) ----------
    def window_minimize(self):
        w = self._window()
        if w:
            try: w.minimize()
            except Exception: pass
        return {"ok": True}

    def window_maximize(self):
        w = self._window()
        if w:
            try: w.toggle_fullscreen()
            except Exception: pass
        return {"ok": True}

    def window_close(self):
        w = self._window()
        if w:
            try: w.destroy()
            except Exception: pass
        try: self.ctx.stop_event.set()
        except Exception: pass
        return {"ok": True}

    def set_always_on_top(self, enabled):
        # BUG pywebview 6.2.1: Window.on_top setter tidak marshal ke UI thread -
        # set WinForms.TopMost dari js_api worker thread bikin app hang.
        # Workaround: ambil Form instance langsung, panggil lewat .Invoke() ke UI thread.
        w = self._window()
        if not w:
            return {"ok": True, "value": bool(enabled)}
        val = bool(enabled)
        try:
            from webview.platforms.winforms import BrowserView
            from System import Action
            form = BrowserView.instances.get(w.uid)
            if form is None:
                return {"ok": False, "error": "form not found"}
            def _apply():
                form.TopMost = val
            form.Invoke(Action(_apply))
            # Sinkron juga internal state pywebview biar konsisten kalau ada
            # akses lanjutan ke w.on_top.
            try: w._Window__on_top = val
            except Exception: pass
            return {"ok": True, "value": val}
        except Exception as e:
            return {"ok": False, "error": str(e)[:160]}

    def _window(self):
        if self._window_ref and self._window_ref[0]:
            return self._window_ref[0]
        return None


class StateBridge:
    """Polls BotContext setiap tick_ms dan push snapshot ke JS.
    Run di thread sendiri (daemon). Stop saat window ditutup."""

    def __init__(self, ctx, window_ref, tick_ms=500):
        self.ctx = ctx
        self._window_ref = window_ref
        self.tick = max(0.1, tick_ms / 1000.0)
        self._last_total = 0
        self._last_messages_len = 0
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        # Give window a moment to finish loading HTML before first push
        time.sleep(0.8)
        while not self._stop.is_set():
            try:
                self._push_tick()
            except Exception:
                pass
            self._stop.wait(self.tick)

    # ---------- Internal ----------
    def _push_tick(self):
        window = self._window_ref[0] if self._window_ref else None
        if window is None:
            return

        # --- 1) Log diff push ---
        try:
            messages, total = self.ctx.logger.snapshot()
        except Exception:
            messages, total = [], self._last_total

        if total != self._last_total:
            # New lines may have been pushed AND old ones may have been popped
            # (logger keeps bounded list). Compute how many new lines.
            delta = total - self._last_total
            if delta > 0 and messages:
                new_lines = messages[-delta:] if delta <= len(messages) else messages
                entries = [parse_log_line(ln) for ln in new_lines]
                if entries:
                    self._safe_eval(
                        f"window.pushLogBatch({_js_escape(entries)})"
                    )
            self._last_total = total
            self._last_messages_len = len(messages)

        # --- 2) State snapshot push ---
        snapshot = self._build_state()
        self._safe_eval(f"window.setAppState({_js_escape(snapshot)})")

    def _build_state(self):
        ctx = self.ctx

        # Toggles
        toggles = {}
        for b in BOT_NAMES:
            try:
                toggles[b] = bool(ctx.toggles.get(b))
            except Exception:
                toggles[b] = False

        # Progress + workers (focus: running bot)
        bots_ui = {}
        running_bot = None
        workers_out = []
        task_counts = getattr(ctx, "task_counts", {}) or {}
        for b in BOT_NAMES:
            try:
                data = ctx.progress.get(b)
            except Exception:
                data = {"phase": "idle", "workers": []}
            phase = data.get("phase", "idle")
            enabled = toggles.get(b, False)
            if not enabled or phase == "off":
                status = "stopped"
            elif phase in _RUNNING_PHASES:
                status = "running"
                running_bot = b
            else:
                status = "standby"
            bots_ui[b.upper()] = {
                "enabled": enabled,
                "active": status == "running",
                "status": status.capitalize(),
                "taskCount": int(task_counts.get(b, 0) or 0),
            }

        if running_bot is not None:
            workers_out = self._collect_workers(running_bot)

        # Stats (all 3 bots, ready to slot into UI table)
        stats_out = {}
        for b in BOT_NAMES:
            try:
                all_time, today, _ = ctx.stats.snapshot(b)
            except Exception:
                all_time, today = {}, {}
            # Build per-platform allTime + aggregated today totals
            all_plat = {}
            for plat, val in (all_time or {}).items():
                s = int(val.get("success", 0))
                f = int(val.get("fail", 0))
                all_plat[plat] = {"sukses": s, "gagal": f, "total": s + f}
            today_s = sum(int(v.get("success", 0)) for v in (today or {}).values())
            today_f = sum(int(v.get("fail", 0)) for v in (today or {}).values())
            stats_out[b.upper()] = {
                "allTime": all_plat,
                "today": {"sukses": today_s, "gagal": today_f,
                           "total": today_s + today_f},
            }

        # Connection status: backend alive AND at least one bot toggled ON.
        # Semua toggle OFF -> user "stop semua" -> tampil Disconnected.
        chrome_ok = False
        sheets_ok = False
        try: chrome_ok = ctx.chrome.is_alive()
        except Exception: pass
        try: sheets_ok = getattr(ctx.sheets, "spreadsheet", None) is not None
        except Exception: pass
        any_enabled = any(toggles.values())
        connected = bool(chrome_ok and sheets_ok and any_enabled)

        # Worker limits (read live from config)
        try:
            max_create = ctx.config.get_int("CREATE_MAX_WORKER", 3)
        except Exception:
            max_create = 3
        try:
            max_diskon = ctx.config.get_int("DISKON_MAX_WORKER", 5)
        except Exception:
            max_diskon = 5

        return {
            "connected": connected,
            "bots": bots_ui,
            "workers": workers_out,
            "stats": stats_out,
            "maxCreate": max_create,
            "maxDiskon": max_diskon,
        }

    def _collect_workers(self, bot):
        """Mirror AppGUI._collect_workers - ambil dari worker_status module +
        fallback ke ctx.progress untuk bot_delete. Mapping ke schema yg
        diharapkan HTML: {id, game, row, url, highlight}."""
        out = []
        mod = sys.modules.get(f"bot_{bot}")
        if mod is not None:
            try:
                lock = getattr(mod, "worker_status_lock", None)
                ws = getattr(mod, "worker_status", {}) or {}
                if lock is not None:
                    with lock:
                        snapshot = list(ws.items())
                else:
                    snapshot = list(ws.items())
                for wid, info in snapshot:
                    if not info:
                        continue
                    text = info.get("text") or ""
                    if not text or text == "-":
                        continue
                    sheet_part, row_part = text, ""
                    if "|" in text:
                        segs = [s.strip() for s in text.split("|")]
                        sheet_part = segs[0] if segs else text
                        for s in segs[1:]:
                            if s.lower().startswith("row"):
                                row_part = s[3:].strip() or s
                                break
                        if not row_part and len(segs) > 1:
                            row_part = segs[1]
                    # bot_diskon set "waiting": True saat worker antri market_lock
                    # (idle, tidak sedang proses market). bot_create tidak punya
                    # konsep waiting - semua worker listed = active.
                    waiting = bool(info.get("waiting", False))
                    out.append({
                        "id": f"W{wid}",
                        "game": sheet_part or "-",
                        "row": row_part or "-",
                        "url": info.get("url", "") or "",
                        "highlight": not waiting,
                    })
            except Exception:
                pass
        if bot == "delete" and not out:
            try:
                data = self.ctx.progress.get(bot)
                if data.get("phase") == "processing":
                    out.append({
                        "id": "W1",
                        "game": data.get("current_sheet") or "-",
                        "row": str(data.get("current_row") or "-"),
                        "url": "",
                        "highlight": True,
                    })
            except Exception:
                pass
        # Jangan force-highlight worker pertama - semua worker yg muncul di list
        # sudah pasti active (entry "-" di-filter di atas). Highlight=True cuma
        # dipakai di delete single-worker fallback (bot_delete) biar tetap beda
        # visual dari idle state.
        return out

    def _safe_eval(self, js):
        window = self._window_ref[0] if self._window_ref else None
        if window is None:
            return
        try:
            window.evaluate_js(js)
        except Exception:
            # Window may be closing / not ready yet; drop silently.
            pass


class WebviewApp:
    """Composition: pywebview window + BotAPI + StateBridge thread."""

    def __init__(self, ctx, title=None):
        self.ctx = ctx
        self.title = title or "Bot Manage Listing"
        self._window_ref = [None]
        self.api = BotAPI(ctx, self._window_ref)
        self.bridge = StateBridge(ctx, self._window_ref)

    def run(self):
        if not os.path.isfile(HTML_PATH):
            raise FileNotFoundError(f"HTML UI not found: {HTML_PATH}")

        window = webview.create_window(
            title=self.title,
            url=HTML_PATH,
            width=1440,
            height=860,
            min_size=(1100, 700),
            background_color="#080a10",
            js_api=self.api,
            on_top=True,  # start ON; user bisa toggle via button "On Top" di log header
        )
        self._window_ref[0] = window

        # Start bridge pusher thread (daemon). Start it after window object
        # exists; pywebview.start() will load HTML asynchronously.
        t = threading.Thread(target=self.bridge.run, daemon=True,
                              name="webview-bridge")
        t.start()

        def _on_closed():
            try: self.bridge.stop()
            except Exception: pass
            try: self.ctx.stop_event.set()
            except Exception: pass
            # Tutup Chrome yg di-launch bot biar tidak nyangkut di background
            try: self.ctx.chrome.terminate()
            except Exception: pass

        def _on_shown():
            # pywebview tidak expose icon param -> set via Win32 WM_SETICON.
            # Ditunda ke thread supaya FindWindowW bisa menangkap HWND yg sudah final.
            if ICON_PATH and os.path.isfile(ICON_PATH):
                threading.Thread(
                    target=_apply_window_icon, args=(self.title, ICON_PATH),
                    daemon=True, name="icon-setter",
                ).start()

        try:
            window.events.closed += _on_closed
        except Exception:
            pass
        try:
            window.events.shown += _on_shown
        except Exception:
            pass

        webview.start(debug=False)
