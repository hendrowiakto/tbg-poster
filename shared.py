"""shared.py - foundation module for the merged bot app.

Exposes:
- `Config`, `Logger`, `ChromeManager`, `SheetsClient`,
  `StatsManager`, `ToggleManager`, `ProgressTracker`, `BotContext`
- `TimeoutHangError`, `call_with_timeout`, `validate_config`
- Constants: `SCRIPT_DIR`, `CONFIG_FILE`, `CREDENTIALS_FILE`, `LOG_DIR`, `BOT_NAMES`

Semua bot (bot_delete / bot_create / bot_diskon) ambil infrastruktur lewat
`BotContext` - tidak ada duplikat logger/chrome/sheets/stats di 3 file bot.
"""

import os
import sys
import json
import time
import socket
import subprocess
import threading

import gspread
from google.oauth2.service_account import Credentials


# ===================== PATHS =====================
if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE      = os.path.join(SCRIPT_DIR, "config.txt")
CREDENTIALS_FILE = os.path.join(SCRIPT_DIR, "credentials.json")
LOG_DIR          = os.path.join(SCRIPT_DIR, "log")

BOT_NAMES = ("delete", "create", "diskon")

STATS_FILE = os.path.join(SCRIPT_DIR, "stats.txt")

LOG_MAX_LINES            = 200
LOG_MAX_AGE_DAYS_DEFAULT = 120


# ===================== TIMEOUT HELPER =====================
class TimeoutHangError(Exception):
    """Raised ketika task tidak selesai dalam batas waktu (thread dibiarkan daemon)."""
    pass


def call_with_timeout(fn, args=(), kwargs=None, timeout=60, name="task"):
    if kwargs is None:
        kwargs = {}
    holder = {}

    def _runner():
        try:
            holder["result"] = fn(*args, **kwargs)
        except BaseException as e:
            holder["error"] = e

    t = threading.Thread(target=_runner, daemon=True, name=f"timeout-{name}")
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        raise TimeoutHangError(f"{name} melebihi timeout {timeout}s")
    if "error" in holder:
        raise holder["error"]
    return holder.get("result")


# ===================== CONFIG =====================
CONFIG_TEMPLATE = """# ============ APP ============
APP_VERSION=1.0.0

# ============ GOOGLE SHEETS ============
SPREADSHEET_ID=ISI_ID_SPREADSHEET_DISINI

# ============ CHROME (1 instance shared) ============
CHROME_PATH=C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe
CHROME_DEBUG_PORT=9222
CHROME_USER_DATA_DIR=C:\\chrome-debug

# ============ GEMINI AI (untuk bot_create) ============
GEMINI_API_KEY=ISI_API_KEY_GEMINI_DISINI

# ============ BOT_CREATE ============
CREATE_MAX_WORKER=3

# ============ BOT_DISKON ============
DISKON_MAX_WORKER=5

# ============ SHARED ============
SHARED_POLLING_INTERVAL=60
LOG_RETENTION_DAYS=120
"""


class Config:
    """Load config.txt; auto-create dari template kalau belum ada; auto-append
    key baru yang hilang. `set(k,v)` persist ke file dengan preserve order+comments."""

    def __init__(self, path=CONFIG_FILE):
        self.path = path
        self._data = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(CONFIG_TEMPLATE)
            except Exception:
                pass

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if "=" in s and not s.startswith("#"):
                        k, v = s.split("=", 1)
                        self._data[k.strip()] = v.strip()
        except Exception:
            pass

        # Auto-append template keys yg hilang (upgrade-friendly)
        template_defaults = {}
        for line in CONFIG_TEMPLATE.splitlines():
            s = line.strip()
            if "=" in s and not s.startswith("#"):
                k, v = s.split("=", 1)
                template_defaults[k.strip()] = v.strip()
        missing = [k for k in template_defaults if k not in self._data]
        if missing:
            try:
                needs_newline = False
                if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
                    with open(self.path, "rb") as f:
                        f.seek(-1, os.SEEK_END)
                        needs_newline = f.read(1) != b"\n"
                with open(self.path, "a", encoding="utf-8") as f:
                    if needs_newline:
                        f.write("\n")
                    for k in missing:
                        f.write(f"{k}={template_defaults[k]}\n")
                        self._data[k] = template_defaults[k]
            except Exception:
                pass

    def get(self, key, default=""):
        with self._lock:
            return self._data.get(key, default)

    def get_int(self, key, default):
        try:
            return int(str(self.get(key, default)).strip())
        except Exception:
            return default

    def set(self, key, value):
        value = str(value)
        with self._lock:
            self._data[key] = value

        lines = []
        found = False
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    for line in f:
                        s = line.strip()
                        if "=" in s and not s.startswith("#"):
                            k, _ = s.split("=", 1)
                            if k.strip() == key:
                                lines.append(f"{key}={value}\n")
                                found = True
                                continue
                        lines.append(line if line.endswith("\n") else line + "\n")
            if not found:
                lines.append(f"{key}={value}\n")
            with open(self.path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return True
        except Exception:
            return False

    def snapshot(self):
        with self._lock:
            return dict(self._data)


def validate_config(config):
    """Return list of missing/invalid config items (empty list = OK)."""
    missing = []
    sid = config.get("SPREADSHEET_ID", "")
    if not sid or sid == "ISI_ID_SPREADSHEET_DISINI":
        missing.append("SPREADSHEET_ID")
    gk = config.get("GEMINI_API_KEY", "")
    if not gk or gk == "ISI_API_KEY_GEMINI_DISINI":
        missing.append("GEMINI_API_KEY")
    if not os.path.exists(CREDENTIALS_FILE):
        missing.append(f"credentials.json (expected at {CREDENTIALS_FILE})")
    chrome_path = config.get("CHROME_PATH", "")
    if chrome_path and not os.path.exists(chrome_path):
        missing.append(f"CHROME_PATH invalid ({chrome_path})")
    return missing


# ===================== LOGGER =====================
class Logger:
    """Thread-safe shared logger. Format: `[HH:MM:SS] [BOT] [W{n}] msg`.

    - `messages`: bounded list (max 200, FIFO) untuk feed GUI.
    - `total_count`: monotonic counter buat deteksi "ada log baru" di GUI.
    - File: `log/app_log_YYYY-MM-DD.txt` (shared, 1 file/hari, append-only).
    - Worker-id bisa di-set lewat thread-local (`set_worker_id`) atau dikirim eksplisit.
    """

    def __init__(self, log_dir=LOG_DIR):
        self.log_dir     = log_dir
        self._lock       = threading.Lock()
        self._file_lock  = threading.Lock()
        self.messages    = []
        self.total_count = 0
        self.worker_local = threading.local()

    def ensure_folder(self):
        try:
            os.makedirs(self.log_dir, exist_ok=True)
        except Exception:
            pass

    @staticmethod
    def _format(bot_name, msg, worker_id):
        ts      = time.strftime("%H:%M:%S")
        bot_tag = f"[{bot_name.upper()}] " if bot_name else ""
        wid_tag = f"[W{worker_id}] "        if worker_id else ""
        return f"[{ts}] {bot_tag}{wid_tag}{msg}"

    def log(self, bot_name, msg, worker_id=None):
        if worker_id is None:
            worker_id = getattr(self.worker_local, "worker_id", None)
        full_msg = self._format(bot_name, msg, worker_id)

        with self._lock:
            self.messages.append(full_msg)
            if len(self.messages) > LOG_MAX_LINES:
                self.messages.pop(0)
            self.total_count += 1

        try:
            print(full_msg)
        except Exception:
            try:
                print(full_msg.encode("ascii", errors="replace").decode("ascii"))
            except Exception:
                pass

        try:
            log_date = time.strftime("%Y-%m-%d")
            log_file = os.path.join(self.log_dir, f"app_log_{log_date}.txt")
            with self._file_lock:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(full_msg + "\n")
        except Exception:
            pass

    def get_bot_logger(self, bot_name):
        """Return a callable `add_log(msg, worker_id=None)` pre-bound to bot_name."""
        def _add_log(msg, worker_id=None):
            self.log(bot_name, msg, worker_id=worker_id)
        return _add_log

    def snapshot(self):
        with self._lock:
            return list(self.messages), self.total_count

    def set_worker_id(self, worker_id):
        self.worker_local.worker_id = worker_id

    def clear_worker_id(self):
        try:
            del self.worker_local.worker_id
        except AttributeError:
            pass

    def cleanup_old_files(self, max_age_days=LOG_MAX_AGE_DAYS_DEFAULT):
        if not os.path.isdir(self.log_dir):
            return 0
        cutoff = time.time() - (max_age_days * 86400)
        deleted = 0
        try:
            entries = os.listdir(self.log_dir)
        except Exception:
            return 0
        for fname in entries:
            if not fname.lower().endswith(".txt"):
                continue
            fpath = os.path.join(self.log_dir, fname)
            try:
                if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    deleted += 1
            except Exception:
                pass
        return deleted


# ===================== CHROME MANAGER =====================
class ChromeManager:
    """1 Chrome instance shared oleh 3 bot. Port & user-data-dir dari config."""

    def __init__(self, logger, chrome_path, debug_port, user_data_dir):
        self.logger        = logger
        self.chrome_path   = chrome_path
        self.debug_port    = int(debug_port)
        self.user_data_dir = user_data_dir
        self.process       = None
        self._lock         = threading.Lock()

    @property
    def cdp_url(self):
        return f"http://localhost:{self.debug_port}"

    def keeper_tab_url(self):
        asset_path = os.path.join(SCRIPT_DIR, "boys_gaming.gif")
        if os.path.isfile(asset_path):
            return "file:///" + asset_path.replace(os.sep, "/")
        return "about:blank"

    def is_alive(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            result = s.connect_ex(("localhost", self.debug_port))
            s.close()
            return result == 0
        except Exception:
            return False

    def ensure_alive(self):
        """Idempotent: kalau Chrome hidup -> no-op. Kalau mati -> launch."""
        with self._lock:
            if self.is_alive():
                return True
            return self._launch_locked()

    def _launch_locked(self):
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                pass
            self.process = None

        self.logger.log("app", f"Membuka Chrome (port {self.debug_port}, {self.user_data_dir})")
        startup_url = self.keeper_tab_url()
        try:
            self.process = subprocess.Popen([
                self.chrome_path,
                f"--remote-debugging-port={self.debug_port}",
                f"--user-data-dir={self.user_data_dir}",
                "--disable-session-crashed-bubble",
                "--hide-crash-restore-bubble",
                "--no-default-browser-check",
                "--no-first-run",
                startup_url,
            ])
        except FileNotFoundError:
            self.logger.log("app", f"CHROME_PATH tidak ditemukan: {self.chrome_path}")
            return False
        except Exception as e:
            self.logger.log("app", f"Gagal jalankan Chrome: {e}")
            return False
        time.sleep(4)
        self.logger.log("app", "Chrome siap!")
        return True

    def cleanup_tabs(self):
        """Pastikan hanya ada 1 tab keeper (boys_gaming.gif / about:blank).
        - 1 tab & sudah keeper -> no-op (hindari blink)
        - 1 tab non-keeper     -> navigate tab itu ke keeper (no create/close)
        - 2+ tab               -> pilih 1 keeper, close sisanya"""
        from playwright.sync_api import sync_playwright
        if not self.is_alive():
            return
        keeper_url = self.keeper_tab_url()
        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.connect_over_cdp(self.cdp_url, timeout=10000)
                except Exception as e:
                    self.logger.log("app", f"Cleanup tab: gagal connect CDP ({str(e)[:60]})")
                    return
                try:
                    all_pages = []  # [(context, page, url)]
                    for c in browser.contexts:
                        for pg in c.pages:
                            try:
                                url = pg.url or ""
                            except Exception:
                                url = ""
                            all_pages.append((c, pg, url))
                    total = len(all_pages)

                    if total == 0:
                        ctx0 = browser.contexts[0] if browser.contexts else browser.new_context()
                        try:
                            ctx0.new_page().goto(keeper_url, timeout=5000)
                        except Exception:
                            pass
                        return

                    if total == 1:
                        _c, pg, url = all_pages[0]
                        if url == keeper_url:
                            return  # sudah 1 tab keeper, no-op
                        try:
                            pg.goto(keeper_url, timeout=5000)
                        except Exception:
                            pass
                        return

                    # 2+ tab: pilih keeper (prefer URL yg sudah keeper), navigate kalau perlu, close sisanya
                    keeper_page = None
                    for _c, pg, url in all_pages:
                        if url == keeper_url:
                            keeper_page = pg
                            break
                    if keeper_page is None:
                        keeper_page = all_pages[0][1]
                        try:
                            keeper_page.goto(keeper_url, timeout=5000)
                        except Exception:
                            pass
                    total_closed = 0
                    for _c, pg, _url in all_pages:
                        if pg is keeper_page:
                            continue
                        try:
                            pg.close()
                            total_closed += 1
                        except Exception:
                            pass
                    if total_closed > 0:
                        self.logger.log("app", f"Cleanup: tutup {total_closed} tab, sisakan 1 keeper")
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception as e:
            self.logger.log("app", f"Cleanup tab error: {str(e)[:80]}")

    def terminate(self):
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass
            self.process = None


# ===================== SHEETS CLIENT =====================
class SheetsClient:
    """1 gspread client shared antar bot. Write-lock global (serialize semua write)
    dan retry 429 via exponential backoff."""

    def __init__(self, logger, spreadsheet_id, creds_path=CREDENTIALS_FILE):
        self.logger         = logger
        self.spreadsheet_id = spreadsheet_id
        self.creds_path     = creds_path
        self.spreadsheet    = None
        self.write_lock     = threading.Lock()

    def _connect_raw(self):
        scope = ["https://spreadsheets.google.com/feeds",
                 "https://www.googleapis.com/auth/drive"]
        creds  = Credentials.from_service_account_file(self.creds_path, scopes=scope)
        client = gspread.authorize(creds)
        return client.open_by_key(self.spreadsheet_id)

    def connect(self, timeout=45, max_retries=5, base_delay=5.0):
        """Retry pada 429 dengan exponential backoff panjang; transient network
        error (timeout, DNS, socket) retry dengan delay pendek; error lain
        (credentials, not found, permission) langsung raise tanpa retry."""
        last_err = None
        for attempt in range(max_retries):
            try:
                self.spreadsheet = call_with_timeout(
                    self._connect_raw, timeout=timeout, name="connect_sheets"
                )
                return self.spreadsheet
            except Exception as e:
                last_err = e
                msg = str(e)
                etype = type(e).__name__
                upper = msg.upper()
                # 429 / quota - long exponential
                if "429" in msg or "Quota exceeded" in msg or "RATE_LIMIT" in upper:
                    delay = base_delay * (2 ** attempt)
                    self.logger.log("app", f"Sheets 429 saat connect, tunggu {delay:.0f}s ({attempt+1}/{max_retries})")
                    time.sleep(delay)
                    continue
                # transient network - shorter fixed delay
                transient = any(kw in upper for kw in (
                    "TIMEOUT", "TIMED OUT", "TEMPORARILY", "SERVICE UNAVAILABLE",
                    "503", "500", "502", "504", "CONNECTION", "NETWORK",
                    "NAME RESOLUTION", "GETADDRINFO", "SSL"
                )) or etype in ("TimeoutError", "ConnectionError", "OSError")
                if transient:
                    delay = 5.0 * (attempt + 1)  # 5s, 10s, 15s, 20s, 25s
                    detail = msg if msg else etype
                    self.logger.log("app", f"Sheets transient error ({etype}): {detail[:150]}. "
                                            f"Retry in {delay:.0f}s ({attempt+1}/{max_retries})")
                    time.sleep(delay)
                    continue
                # fatal - kredensial / permission / not found
                raise
        raise last_err if last_err else Exception("connect_sheets: unknown failure")

    def safe_update_cell(self, sheet, row, col, value, timeout=45, desc=""):
        return call_with_timeout(
            sheet.update_cell, args=(row, col, value),
            timeout=timeout, name=f"update_cell[{desc}]"
        )

    def read_with_backoff(self, fn, *args, max_retries=5, base_delay=2.0, desc="", **kwargs):
        for attempt in range(max_retries):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                msg = str(e)
                if "429" in msg or "Quota exceeded" in msg or "RATE_LIMIT" in msg.upper():
                    delay = base_delay * (2 ** attempt)
                    self.logger.log("app", f"Rate limit ({desc}), tunggu {delay:.0f}s ({attempt+1}/{max_retries})")
                    time.sleep(delay)
                    continue
                raise
        raise Exception(f"read_with_backoff: {desc} gagal setelah {max_retries} retry")

    def batch_get(self, ranges, desc="batch_get"):
        if not self.spreadsheet:
            raise Exception("Sheets not connected - panggil connect() dulu")
        return self.read_with_backoff(
            self.spreadsheet.values_batch_get, ranges=ranges, desc=desc
        )

    def with_write_lock(self, body, lock_timeout=60, desc=""):
        acquired = self.write_lock.acquire(timeout=lock_timeout)
        if not acquired:
            raise TimeoutHangError(f"Gagal acquire write_lock dalam {lock_timeout}s ({desc})")
        try:
            return body()
        finally:
            self.write_lock.release()


# ===================== STATS MANAGER =====================
class StatsManager:
    """Per-bot stats (all-time + today). File JSON dipersist per-bot.
    Auto-reset `today` saat ganti tanggal."""

    def __init__(self, logger):
        self.logger      = logger
        self._lock       = threading.Lock()
        self._file_lock  = threading.Lock()
        self._all_time   = {b: {} for b in BOT_NAMES}
        self._today      = {b: {} for b in BOT_NAMES}
        self._today_date = {b: time.strftime("%Y-%m-%d") for b in BOT_NAMES}

    def load_all(self):
        """Load single stats.txt {bot: {all_time: {...}, today: {date, stats}}}.
        Today auto-reset kalau tanggal di file != hari ini."""
        today = time.strftime("%Y-%m-%d")
        try:
            if not os.path.exists(STATS_FILE):
                return
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.loads(f.read())
            if not isinstance(data, dict):
                return
            with self._lock:
                for bot in BOT_NAMES:
                    bot_data = data.get(bot, {})
                    if not isinstance(bot_data, dict):
                        continue
                    at = bot_data.get("all_time", {})
                    if isinstance(at, dict):
                        self._all_time[bot] = at
                    td = bot_data.get("today", {})
                    if isinstance(td, dict) and td.get("date") == today:
                        self._today[bot]      = td.get("stats", {})
                        self._today_date[bot] = today
                    else:
                        self._today[bot]      = {}
                        self._today_date[bot] = today
        except Exception:
            pass

    def _write_all_unlocked(self):
        """Build full snapshot JSON. Caller harus sudah pegang self._lock."""
        snap = {}
        for bot in BOT_NAMES:
            snap[bot] = {
                "all_time": self._all_time[bot],
                "today": {
                    "date": self._today_date[bot],
                    "stats": self._today[bot],
                },
            }
        return json.dumps(snap, indent=2, ensure_ascii=False)

    def update(self, bot_name, key, success):
        if bot_name not in BOT_NAMES or not key:
            return
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            if self._today_date[bot_name] != today:
                self._today[bot_name]      = {}
                self._today_date[bot_name] = today
            for bucket in (self._all_time[bot_name], self._today[bot_name]):
                if key not in bucket:
                    bucket[key] = {"success": 0, "fail": 0}
                bucket[key]["success" if success else "fail"] += 1
            snap = self._write_all_unlocked()
        # Atomic write: tulis ke .tmp lalu rename. os.replace() atomic di Windows+POSIX.
        tmp_path = STATS_FILE + ".tmp"
        try:
            with self._file_lock:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(snap)
                os.replace(tmp_path, STATS_FILE)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    def snapshot(self, bot_name):
        """Return deep-copied (all_time, today, today_date)."""
        with self._lock:
            return (
                {k: dict(v) for k, v in self._all_time[bot_name].items()},
                {k: dict(v) for k, v in self._today[bot_name].items()},
                self._today_date[bot_name],
            )


# ===================== TOGGLE MANAGER =====================
class ToggleManager:
    """In-memory ON/OFF state per bot. Tidak di-persist — setiap launch selalu
    mulai dengan default yang di-set caller (main.py menyalakan semuanya)."""

    def __init__(self, logger):
        self.logger = logger
        self._lock  = threading.Lock()
        self._state = {b: False for b in BOT_NAMES}

    def get(self, bot_name):
        with self._lock:
            return self._state.get(bot_name, False)

    def set(self, bot_name, on):
        if bot_name not in BOT_NAMES:
            return
        with self._lock:
            self._state[bot_name] = bool(on)

    def should_keep_running(self, bot_name):
        return self.get(bot_name)

    def snapshot(self):
        with self._lock:
            return dict(self._state)


# ===================== PROGRESS TRACKER =====================
class ProgressTracker:
    """Per-bot progress dict - di-set bot, di-poll GUI."""

    _DEFAULT = {
        "phase": "idle",           # "idle" | "scanning" | "processing" | "off"
        "current_sheet": None,
        "current_row": None,
        "total_found": 0,
        "processed": 0,
        "success": 0,
        "fail": 0,
        "workers": [],             # list of {"id", "sheet", "row", "url", "text"}
    }

    def __init__(self):
        self._lock  = threading.Lock()
        self._state = {b: dict(self._DEFAULT) for b in BOT_NAMES}

    def set(self, bot_name, data):
        if bot_name not in BOT_NAMES or not isinstance(data, dict):
            return
        with self._lock:
            self._state[bot_name].update(data)

    def reset(self, bot_name):
        if bot_name not in BOT_NAMES:
            return
        with self._lock:
            self._state[bot_name] = dict(self._DEFAULT)

    def get(self, bot_name):
        with self._lock:
            return dict(self._state.get(bot_name, self._DEFAULT))

    def snapshot(self):
        with self._lock:
            return {b: dict(v) for b, v in self._state.items()}


# ===================== ZOMBIE THREAD TRACKER =====================
class ZombieTracker:
    """Melacak thread yang tidak selesai dalam t.join(timeout=...).

    Python tidak bisa kill thread, jadi yang kita bisa lakukan:
    1. Catat zombie supaya visible di log
    2. Auto-prune yang sudah mati (GC akhir)
    3. Warn kalau jumlah zombie hidup > threshold -> tanda perlu restart
    """

    WARN_THRESHOLD = 5  # mulai warning kalau zombie hidup >= N

    def __init__(self, logger):
        self._logger  = logger
        self._lock    = threading.Lock()
        self._zombies = []  # list of threading.Thread

    def track(self, thread, bot_name, context=""):
        """Panggil setelah t.join(timeout=...) kalau t.is_alive() masih True."""
        with self._lock:
            self._zombies[:] = [z for z in self._zombies if z.is_alive()]
            self._zombies.append(thread)
            alive_count = len(self._zombies)

        suffix = f" [{context}]" if context else ""
        self._logger.log(
            bot_name,
            f"⚠️ Zombie thread: {thread.name}{suffix}. Total zombie hidup: {alive_count}"
        )
        if alive_count >= self.WARN_THRESHOLD:
            self._logger.log(
                "app",
                f"⚠️⚠️ ZOMBIE THREADS = {alive_count} (>= {self.WARN_THRESHOLD}). "
                f"Pertimbangkan restart aplikasi untuk reclaim resource."
            )

    def prune(self):
        """Dipanggil periodic - bersihkan entry yang sudah mati."""
        with self._lock:
            before = len(self._zombies)
            self._zombies[:] = [z for z in self._zombies if z.is_alive()]
            return before - len(self._zombies)

    def alive_count(self):
        with self._lock:
            self._zombies[:] = [z for z in self._zombies if z.is_alive()]
            return len(self._zombies)


# ===================== BOT CONTEXT =====================
class BotContext:
    """Composition root - construct sekali di main.py, pass ke setiap bot."""

    def __init__(self, config):
        self.config     = config
        self.stop_event = threading.Event()
        self.logger     = Logger()
        self.chrome     = ChromeManager(
            self.logger,
            config.get("CHROME_PATH"),
            config.get_int("CHROME_DEBUG_PORT", 9222),
            config.get("CHROME_USER_DATA_DIR", r"C:\chrome-debug"),
        )
        self.sheets   = SheetsClient(self.logger, config.get("SPREADSHEET_ID"))
        self.stats    = StatsManager(self.logger)
        self.toggles  = ToggleManager(self.logger)
        self.progress = ProgressTracker()
        self.zombies  = ZombieTracker(self.logger)

    def init_folders_and_files(self):
        self.logger.ensure_folder()

    def cleanup_old_logs(self):
        max_age = self.config.get_int("LOG_RETENTION_DAYS", LOG_MAX_AGE_DAYS_DEFAULT)
        return self.logger.cleanup_old_files(max_age_days=max_age)

    def load_all_stats(self):
        self.stats.load_all()
