"""bot_create.py - create listings at GameMarket (GM) dari Google Sheets.

Module kontrak (dipanggil oleh main.py via orchestrator):
    BOT_NAME = "create"
    def run_one_cycle(ctx) -> int:
        # 1 cycle: scan LINK!D -> collect max CREATE_MAX_WORKER candidates ->
        # dispatch parallel worker threads. Return: jumlah row diproses
        # (>0 kalau ada progress, 0 kalau idle / toggle OFF / Sheets error).

Semua infrastruktur (log, stats, Chrome, Sheets, toggle, progress) diambil dari
ctx (lihat shared.py). File ini hanya berisi:
- Konstanta layout sheet (baris/kolom data produk, header O43/O44/O45).
- Multi-worker state (_worker_local, worker_status) - cegah 2 worker nabrak.
- Per-sheet scrape coordination (_scrape_locks_per_sheet, _scrape_memcache) -
  cegah 2 worker scrape form_options sheet yang sama.
- Image scrapers (Imgur / Postimg / Gdrive) - selectors trial-and-error.
- ai_map_fields (Gemini) - mapping field GM form ke katalog game.
- Helper Playwright (select_game_dropdown, select_type_accounts, dll.) -
  XPath & timings TIDAK diubah.
- create_listing_gm - main flow post 1 listing di GM (selector + retry).
- scrape_and_cache_form_options - scrape form + cache ke O45.
- get_active_sheet_names, batch_scan_all_sheets, proses_baris - scanner.
- Thin wrappers add_log / safe_update_cell / update_stats / set_processing /
  wait_if_paused / smart_wait - delegasi ke ctx tanpa ubah callsite.
"""

import os
import re
import sys
import time
import threading
import random
import json
import shutil
from datetime import datetime

from google.oauth2.service_account import Credentials  # noqa: F401 - kompat ekspor lama
from playwright.sync_api import sync_playwright

import requests
from bs4 import BeautifulSoup
import google.generativeai as genai

from shared import call_with_timeout, TimeoutHangError


if getattr(sys, 'frozen', False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Gemini model - lazy init di _bind_ctx() supaya API key dibaca dari ctx.config.
gemini_model = None

# ===================== KONSTANTA =====================
LINK_SHEET_NAME = "LINK"
BARIS_PLATFORM       = 43   # O43 = game name GM
BARIS_DESKRIPSI      = 44   # O44 = deskripsi fixed
BARIS_FORM_OPTIONS   = 45   # O45 = form options JSON cache
BARIS_PLATFORM_CODE  = 48   # baris 48 = kode platform (GM/G2G/Z2U/...) per kolom
BARIS_MULAI          = 51   # data produk mulai baris 51

KOLOM_HARGA          = 8    # H (1-based)
KOLOM_GAMBAR         = 9    # I (1-based)
KOLOM_JUDUL          = 10   # J (1-based)
KOLOM_CATATAN        = 11   # K (1-based)
KOLOM_EXTRA          = 13   # M (1-based) - harus kosong untuk trigger
KOLOM_TRIGGER        = 15   # O (1-based)
KOLOM_CACHE          = 15   # O (1-based), untuk O43/O44/O45

NO_OPTIONS_SENTINEL = "[tidak ditemukan options]"  # marker O45: game tidak punya dynamic form (~50 game dari 1700 di GM). Skip AI mapping.

# ===================== CTX BINDING (set at each run_one_cycle) =====================
_ctx                  = None
spreadsheet_client    = None   # di-set dari ctx.sheets.spreadsheet
CHROME_CDP_URL        = None   # di-set dari ctx.chrome.cdp_url
CHROME_DEBUG_PORT     = None   # di-set dari ctx.chrome.debug_port
SPREADSHEET_ID        = ""     # di-set dari ctx.config, dipakai build URL di proses_baris
MAX_WORKER            = 3      # di-set dari ctx.config "CREATE_MAX_WORKER"
current_sheet_label   = {"val": "-"}
current_row_info      = {"val": None}  # kompat callsite lama di proses_baris

TEMP_IMG_DIR = os.path.join(SCRIPT_DIR, "temp_images")


def _bind_ctx(ctx):
    """Bind module-level refs supaya fungsi marketplace (create_listing_gm, dll.)
    yang pakai CHROME_CDP_URL / add_log / smart_wait / spreadsheet_client tidak
    perlu diubah signature-nya."""
    global _ctx, spreadsheet_client, CHROME_CDP_URL, CHROME_DEBUG_PORT, SPREADSHEET_ID, MAX_WORKER, gemini_model
    _ctx               = ctx
    spreadsheet_client = ctx.sheets.spreadsheet
    CHROME_CDP_URL     = ctx.chrome.cdp_url
    CHROME_DEBUG_PORT  = ctx.chrome.debug_port
    SPREADSHEET_ID     = ctx.config.get("SPREADSHEET_ID", "")
    try:
        raw_max = int(str(ctx.config.get("CREATE_MAX_WORKER", "3")).strip())
    except Exception:
        raw_max = 3
    MAX_WORKER = max(1, min(10, raw_max))

    if gemini_model is None:
        api_key = ctx.config.get("GEMINI_API_KEY", "")
        if api_key:
            try:
                genai.configure(api_key=api_key)
                gemini_model = genai.GenerativeModel("gemini-2.5-flash")
            except Exception as e:
                add_log(f"Gagal init Gemini model: {str(e)[:120]}")


# ===================== THIN WRAPPERS (delegate ke ctx) =====================
def add_log(msg):
    """Log message via ctx.logger dengan bot prefix 'create'. Worker id (kalau
    ada di thread local) otomatis masuk prefix supaya parallel worker log
    tetap bisa dibedakan."""
    worker_id = getattr(_worker_local, 'worker_id', None)
    full_msg = f"[WORKER {worker_id}] {msg}" if worker_id else msg
    if _ctx is not None:
        _ctx.logger.log("create", full_msg)
    else:
        try:
            print(f"[CREATE] {full_msg}")
        except Exception:
            pass


def update_stats(platform, success):
    """Delegate ke ctx.stats.update. Key = platform code (GM/G2G/Z2U/...)."""
    if _ctx is not None and platform:
        _ctx.stats.update("create", platform, success)


def set_processing(info):
    """info = {"sheet", "row", "gid"} atau None - delegate ke ctx.progress."""
    if _ctx is None:
        return
    if info is None:
        _ctx.progress.set("create", {
            "phase": "idle",
            "current_sheet": None,
            "current_row": None,
        })
    else:
        _ctx.progress.set("create", {
            "phase": "processing",
            "current_sheet": info.get("sheet"),
            "current_row": info.get("row"),
        })


def wait_if_paused(context=""):
    """Return True kalau bot masih boleh jalan (toggle ON + stop_event clear).
    Dipanggil di row boundary (sebelum spawn worker / sebelum klik Playwright
    yang mahal). Tidak sleep/block - cek sekali return hasil."""
    if _ctx is None:
        return True
    if _ctx.stop_event.is_set():
        return False
    return _ctx.toggles.should_keep_running("create")


def _prepare_worker_temp_dir():
    """Pastikan per-worker temp folder ada + kosong sebelum download.
    Strategi: wipe seluruh isi (file & subdir), recreate folder. Verify kosong.
    """
    temp_dir = _worker_temp_dir()
    # Step 1: pastikan folder ada (handle race: worker lain belum sempat buat)
    try:
        os.makedirs(temp_dir, exist_ok=True)
    except Exception as e:
        add_log(f"Gagal buat folder {temp_dir}: {e}")
        return temp_dir  # masih coba dipakai - download yg panggil akan kena error juga

    # Step 2: wipe isi folder (per-item, biar 1 file locked tidak bikin semua gagal)
    removed = 0
    failed = 0
    try:
        entries = os.listdir(temp_dir)
    except Exception as e:
        add_log(f"Gagal listdir {temp_dir}: {e}")
        entries = []
    for name in entries:
        path = os.path.join(temp_dir, name)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=False)
            else:
                os.remove(path)
            removed += 1
        except Exception:
            failed += 1

    # Step 3: verifikasi kosong; kalau masih ada sisa, log warning (tapi tetap lanjut)
    try:
        remaining = os.listdir(temp_dir)
    except Exception:
        remaining = []
    if remaining:
        add_log(f"Folder {os.path.basename(temp_dir)} masih ada {len(remaining)} item setelah wipe "
                f"(removed={removed}, failed={failed})")
    return temp_dir


def _worker_temp_dir():
    """Per-worker temp dir: temp_images/worker_N. Fallback ke TEMP_IMG_DIR kalau bukan di thread worker."""
    wid = getattr(_worker_local, 'worker_id', None)
    if wid:
        return os.path.join(TEMP_IMG_DIR, f"worker_{wid}")
    return TEMP_IMG_DIR

# ===================== THREAD SAFETY =====================
sheet_write_lock = threading.Lock()

# ===================== MULTI-WORKER =====================
_worker_local       = threading.local()        # thread-local worker_id untuk log prefix
worker_status       = {}                       # {worker_id: {"sheet": str, "row": int, "gid": int}}
worker_status_lock  = threading.Lock()

# Per-sheet scrape coordination: cegah 2+ worker scrape form options sheet sama.
# Worker pertama acquire lock -> scrape -> simpan ke memcache + sheet.
# Worker lain blok sampai lock lepas -> pakai memcache, tidak scrape ulang.
_scrape_coord_lock       = threading.Lock()    # lindungi dict di bawah ini
_scrape_locks_per_sheet  = {}                  # sheet_name -> threading.Lock()
_scrape_memcache          = {}                 # sheet_name -> dict (form_options hasil scrape)


def _get_sheet_scrape_lock(sheet_name):
    with _scrape_coord_lock:
        lk = _scrape_locks_per_sheet.get(sheet_name)
        if lk is None:
            lk = threading.Lock()
            _scrape_locks_per_sheet[sheet_name] = lk
        return lk


# ===================== CHROME & SHEETS (slim) =====================
def get_or_create_context(browser):
    if browser.contexts:
        return browser.contexts[0]
    return browser.new_context()


def safe_update_cell(sheet, row, col, value, timeout=45, desc=""):
    """Thin wrapper - delegate ke ctx.sheets.safe_update_cell (quota-safe +
    call_with_timeout sudah di-handle di shared.SheetsClient). Kompat callsite
    lama: return hasil update_cell (atau raise)."""
    if _ctx is None:
        raise RuntimeError("safe_update_cell: ctx belum di-bind")
    return _ctx.sheets.safe_update_cell(sheet, row, col, value,
                                        timeout=timeout,
                                        desc=desc or f"r{row}c{col}")


def _quote_sheet_name(name):
    """Quote sheet name untuk A1 notation jika mengandung char spesial."""
    if any(c in name for c in " !:'()[]-") or not name.replace("_", "").isalnum():
        escaped = name.replace("'", "''")
        return f"'{escaped}'"
    return name


def sheet_read_with_backoff(fn, *args, max_retries=5, base_delay=2.0, desc="", **kwargs):
    """Call a gspread read, retry with exponential backoff on 429 quota errors."""
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            if "429" in msg or "Quota exceeded" in msg or "RATE_LIMIT" in msg.upper():
                delay = base_delay * (2 ** attempt)
                add_log(f"Rate limit kena ({desc}), tunggu {delay:.0f}s (attempt {attempt+1}/{max_retries})")
                time.sleep(delay)
                continue
            raise
    raise Exception(f"sheet_read_with_backoff: {desc} gagal setelah {max_retries} retry")


def batch_get_header_cells(spreadsheet, sheet_names):
    """Batch-fetch O43 (game), O44 (deskripsi), O45 (cache) untuk semua sheet dalam 1 API call.
    Return dict: {sheet_name: {'game': str, 'deskripsi': str, 'cache': str}}
    """
    ranges = []
    for name in sheet_names:
        q = _quote_sheet_name(name)
        ranges.append(f"{q}!O43:O45")
    try:
        resp = sheet_read_with_backoff(
            spreadsheet.values_batch_get, ranges=ranges, desc="batch_get_header"
        )
    except Exception as e:
        add_log(f"batch_get_header_cells fail: {e}")
        return {}
    value_ranges = resp.get("valueRanges", []) if isinstance(resp, dict) else []
    result = {}
    for idx, name in enumerate(sheet_names):
        vals = []
        if idx < len(value_ranges):
            vals = value_ranges[idx].get("values", [])
        def _row(n):
            if n >= len(vals) or not vals[n]:
                return ""
            return str(vals[n][0]).strip()
        result[name] = {
            "game": _row(0),       # O43
            "deskripsi": _row(1),  # O44
            "cache": _row(2),      # O45
        }
    return result


def batch_scan_all_sheets(spreadsheet, sheet_names):
    """SINGLE API call: fetch header (O43:O45) + kode (A51:A) + harga/gambar/title
    (H51:J) + trigger flag (AJ51:AJ) untuk SEMUA sheet aktif sekaligus via
    values_batch_get.

    Trigger sumber: kolom AJ (formula di sheet menghitung semua kondisi -> isi
    "PERLU POST" / blank). Bot tidak perlu cek K/M/O lagi - AJ sudah
    mencerminkan semua aturan.

    Return dict:
        {sheet_name: {
            "game": str, "deskripsi": str, "cache": str,  # O43/O44/O45
            "rows": [
                {"kode":str, "harga":str, "gambar":str, "title":str,
                 "trigger_aj":str},  # index 0 = baris 51
                ...
            ]
        }}
    """
    if not sheet_names:
        return {}

    # 4 range per sheet: header, kode, harga/gambar/title, trigger(AJ)
    ranges = []
    offsets = []  # list of (sheet_name, start_idx)
    for name in sheet_names:
        q = _quote_sheet_name(name)
        start = len(ranges)
        ranges += [
            f"{q}!O43:O45",           # 0: header (game, deskripsi, cache)
            f"{q}!A{BARIS_MULAI}:A",  # 1: kode_listing
            f"{q}!H{BARIS_MULAI}:J",  # 2: harga, gambar, title (3 kol kontigu)
            f"{q}!AJ{BARIS_MULAI}:AJ",# 3: trigger flag ("PERLU POST" / blank)
        ]
        offsets.append((name, start))

    try:
        resp = sheet_read_with_backoff(
            spreadsheet.values_batch_get, ranges=ranges, desc="batch_scan_all"
        )
    except Exception as e:
        add_log(f"batch_scan_all_sheets fail: {e}")
        return {}

    vranges = resp.get("valueRanges", []) if isinstance(resp, dict) else []

    def _get_range(idx):
        return vranges[idx].get("values", []) if idx < len(vranges) else []

    def _cell(vals, row_i, col_i=0):
        if row_i >= len(vals):
            return ""
        r = vals[row_i]
        if col_i >= len(r):
            return ""
        return str(r[col_i]).strip()

    result = {}
    for name, start in offsets:
        hdr = _get_range(start + 0)
        game = _cell(hdr, 0)
        desk = _cell(hdr, 1)
        cache = _cell(hdr, 2)

        kode_col = _get_range(start + 1)
        hij_col  = _get_range(start + 2)
        aj_col   = _get_range(start + 3)

        max_len = max(
            len(kode_col), len(hij_col), len(aj_col), 0
        )

        rows = []
        for i in range(max_len):
            rows.append({
                "kode":       _cell(kode_col, i, 0),
                "harga":      _cell(hij_col,  i, 0),
                "gambar":     _cell(hij_col,  i, 1),
                "title":      _cell(hij_col,  i, 2),
                "trigger_aj": _cell(aj_col,   i, 0),
            })

        result[name] = {
            "game": game, "deskripsi": desk, "cache": cache,
            "rows": rows,
        }
    return result


def with_sheet_lock(lock, body, lock_timeout=60, desc=""):
    acquired = lock.acquire(timeout=lock_timeout)
    if not acquired:
        raise TimeoutHangError(f"Gagal acquire sheet_write_lock dalam {lock_timeout}s ({desc})")
    try:
        return body()
    finally:
        lock.release()


# ===================== PAUSE-AWARE WAIT =====================
def smart_wait(page, min_ms, max_ms):
    """Wait random time. Toggle OFF tidak interrupt mid-Playwright - cek toggle
    terjadi di row boundary (proses_baris loop) bukan mid-click, supaya flow
    Playwright satu row tidak terpotong di tengah klik."""
    page.wait_for_timeout(random.randint(min_ms, max_ms))


# ===================== IMAGE SCRAPERS =====================
def scrape_imgur(album_url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    # /all memuat semua gambar di album via fragment page yg sama
    base = album_url.rstrip("/")
    candidate_urls = [base, base + "/all"] if not base.endswith("/all") else [base]

    html_parts = []
    for u in candidate_urls:
        try:
            r = requests.get(u, headers=headers, timeout=30)
            if r.status_code == 200 and r.text:
                html_parts.append(r.text)
        except Exception:
            continue

    if not html_parts:
        return []

    html = "\n".join(html_parts)
    urls = []

    # Pola 1: JSON "hash":"XXX" ... "ext":".jpg" (Imgur embed postDataJSON)
    for m in re.finditer(
        r'"hash"\s*:\s*"([a-zA-Z0-9_-]+)"[\s\S]{0,400}?"ext"\s*:\s*"(\.[a-zA-Z0-9]+)"',
        html,
    ):
        h, ext = m.group(1), m.group(2)
        if ext.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            urls.append(f"https://i.imgur.com/{h}{ext}")

    # Pola 2: plain URL (dengan atau tanpa scheme) di HTML
    for m in re.finditer(
        r'(?:https?:)?//i\.imgur\.com/([a-zA-Z0-9_-]{5,})\.(jpg|jpeg|png|gif|webp)',
        html,
        re.IGNORECASE,
    ):
        h, ext = m.group(1), m.group(2).lower()
        urls.append(f"https://i.imgur.com/{h}.{ext}")

    # Ekstrak album ID untuk di-skip (biar tidak ketangkap sebagai image hash)
    album_id_match = re.search(r'/a/([a-zA-Z0-9_-]+)', album_url)
    album_id = album_id_match.group(1) if album_id_match else ""

    # Dedupe by hash (base id), buang suffix thumbnail (s/b/m/l/h/t di akhir jika ada)
    seen_hash = set()
    out = []
    for u in urls:
        m = re.search(r'i\.imgur\.com/([a-zA-Z0-9_-]+)\.', u)
        if not m:
            continue
        h = m.group(1)
        # Skip: album ID (bukan image), atau hash = "removed"/"default"/dsb
        if h == album_id or h.lower() in ("removed", "default", "image", "404"):
            continue
        # Buang thumbnail suffix tunggal (Imgur thumb variants)
        base_h = re.sub(r'[sbmlht]$', '', h) if len(h) > 7 else h
        if base_h in seen_hash:
            continue
        seen_hash.add(base_h)
        out.append(u)

    # Imgur postDataJSON urutan kebalikan dari tampilan album - reverse supaya
    # gambar paling atas di album jadi yang pertama di-download.
    out.reverse()
    return out[:20]


def scrape_postimg(gallery_url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    response = requests.get(gallery_url, headers=headers, timeout=30)
    soup = BeautifulSoup(response.text, "html.parser")
    images = []
    for img in soup.select("a.thumbnail img, img[src*='postimg.cc'], img[src*='i.postimg.cc']"):
        src = img.get("src") or img.get("data-src", "")
        if src and "postimg.cc" in src and not src.endswith("_t.jpg"):
            images.append(src)
    seen = set()
    out = []
    for u in images:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:20]


def scrape_gdrive(folder_url):
    """Scrape Google Drive folder publik -> list direct-download URL.
    Pakai embeddedfolderview yang return HTML daftar file (perlu folder 'Anyone with link')."""
    m = re.search(r'/folders/([a-zA-Z0-9_-]+)', folder_url)
    if not m:
        return []
    folder_id = m.group(1)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    }
    view_url = f"https://drive.google.com/embeddedfolderview?id={folder_id}#grid"
    try:
        r = requests.get(view_url, headers=headers, timeout=30)
        html = r.text
    except Exception:
        return []

    seen = set()
    file_ids = []
    for mm in re.finditer(r'/file/d/([a-zA-Z0-9_-]+)', html):
        fid = mm.group(1)
        if fid and fid != folder_id and fid not in seen:
            seen.add(fid)
            file_ids.append(fid)

    return [f"https://drive.google.com/uc?export=download&id={fid}" for fid in file_ids[:20]]


def download_images(gambar_url):
    # Pre-download: pastikan folder worker bersih - hindari sisa file dari run sebelumnya
    # tercampur (misal bot crash setelah download, lalu restart & dapat URL baru).
    _prepare_worker_temp_dir()

    is_drive = False
    if "imgur.com/a/" in gambar_url:
        try:
            image_urls = scrape_imgur(gambar_url)
        except Exception as e:
            add_log(f"Gagal scrape Imgur: {e}")
            return []
    elif "postimg.cc/gallery/" in gambar_url or "postimg.cc/album/" in gambar_url:
        try:
            image_urls = scrape_postimg(gambar_url)
        except Exception as e:
            add_log(f"Gagal scrape Postimg: {e}")
            return []
    elif "drive.google.com/drive/folders/" in gambar_url:
        is_drive = True
        try:
            image_urls = scrape_gdrive(gambar_url)
        except Exception as e:
            add_log(f"Gagal scrape Google Drive: {e}")
            return []
    else:
        add_log(f"Image source tidak dikenali: {gambar_url}")
        add_log("Listing akan dibuat tanpa gambar")
        return []

    if not image_urls:
        add_log("Tidak ada URL gambar ditemukan di album")
        return []

    temp_dir = _worker_temp_dir()  # sudah dipastikan ada + kosong di atas

    local_paths = []
    for i, url in enumerate(image_urls):
        try:
            r = requests.get(url, timeout=30,
                             headers={"User-Agent": "Mozilla/5.0"},
                             allow_redirects=True)
            # Tentukan ekstensi
            if is_drive:
                ct = (r.headers.get("Content-Type", "") or "").lower()
                if "png" in ct:
                    ext = "png"
                elif "gif" in ct:
                    ext = "gif"
                elif "webp" in ct:
                    ext = "webp"
                elif "jpeg" in ct or "jpg" in ct:
                    ext = "jpg"
                elif "text/html" in ct:
                    # Drive kembalikan HTML konfirmasi (file besar) -> skip
                    add_log(f"Gambar {i+1} butuh confirm (file besar di Drive), skip")
                    continue
                else:
                    ext = "jpg"
            else:
                ext = url.split(".")[-1].split("?")[0].lower()
                if ext not in ["jpg", "jpeg", "png", "gif", "webp"]:
                    ext = "jpg"
            filename = os.path.join(temp_dir, f"img_{i+1:02d}.{ext}")
            with open(filename, "wb") as f:
                f.write(r.content)
            size_mb = os.path.getsize(filename) / (1024 * 1024)
            if size_mb > 5:
                add_log(f"Gambar {i+1} ukuran {size_mb:.1f}MB > 5MB, skip")
                os.remove(filename)
                continue
            local_paths.append(filename)
            add_log(f"Download gambar {i+1}/{len(image_urls)} ({size_mb:.1f}MB)")
        except Exception as e:
            add_log(f"Gagal download gambar {i+1}: {e}")

    add_log(f"Total gambar siap upload: {len(local_paths)}")
    return local_paths


def cleanup_temp_images(paths):
    for f in paths:
        try:
            os.remove(f)
        except Exception:
            pass
    temp_dir = _worker_temp_dir()
    if os.path.isdir(temp_dir):
        for fname in os.listdir(temp_dir):
            try:
                os.remove(os.path.join(temp_dir, fname))
            except Exception:
                pass


# ===================== GEMINI HELPERS =====================
def ai_map_fields(game_name_gm, title, form_options):
    """Panggil Gemini untuk mapping title -> form fields.
    Return dict {field: value}, sudah divalidasi terhadap form_options."""
    prompt = f"""You are a form-filling assistant for a game account marketplace.

Game: {game_name_gm}
Product title: {title}

Available form fields and their options:
{json.dumps(form_options, indent=2, ensure_ascii=False)}

Based on the product title, choose the most appropriate option for each field.
Rules:
- Analyze the title carefully for clues (server region, rank/level, account type, etc.)
- If a field has no relevant info in the title, choose "Others" if available, otherwise choose the most generic/middle option
- Return ONLY a valid JSON object
- No explanation, no markdown, no extra text whatsoever

Example output:
{{"Accounts": "End Game", "Server": "Asia", "Adventure Rank Level": "55+"}}
"""

    response = call_with_timeout(
        fn=lambda: gemini_model.generate_content(prompt),
        timeout=60,
        name="gemini_map_fields"
    )
    text = response.text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    mapping = json.loads(text)

    validated = {}
    for field, value in mapping.items():
        if field not in form_options:
            continue
        opts = form_options[field]
        if value in opts:
            validated[field] = value
        else:
            if "Others" in opts:
                validated[field] = "Others"
                add_log(f"[GM] AI pilih '{value}' untuk '{field}' -> invalid, fallback ke 'Others'")
            elif opts:
                validated[field] = opts[0]
                add_log(f"[GM] AI pilih '{value}' untuk '{field}' -> invalid, fallback ke '{opts[0]}'")

    for f, v in validated.items():
        add_log(f"[GM] AI: {f} -> {v}")

    return validated


# ===================== FORM SCRAPER & FILLER =====================
def _xpath_literal(s):
    """Convert Python string ke XPath string literal yang aman dari quote.
    XPath 1.0 tidak support escape; kalau ada apostrof, harus pakai concat().
    """
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s:
        return f'"{s}"'
    # Ada keduanya -> split di setiap "'" dan gabung dengan "'" (literal kutip tunggal).
    parts = s.split("'")
    pieces = []
    for i, p in enumerate(parts):
        if p:
            pieces.append(f"'{p}'")
        if i < len(parts) - 1:
            pieces.append("\"'\"")
    return "concat(" + ", ".join(pieces) + ")"


def _set_worker_tab_title(page):
    """Inject prefix 'Worker N | ' ke document.title tab GameMarket. Tidak replace,
    hanya prepend. MutationObserver re-apply kalau React overwrite. Silent fail."""
    wid = getattr(_worker_local, "worker_id", None)
    if not wid:
        return
    js = """
    (function(wid){
      var prefix = 'Worker ' + wid + ' | ';
      function apply(){
        var t = document.title || '';
        var stripped = t.replace(/^Worker \\d+ \\| /, '');
        if (document.title !== prefix + stripped) {
          document.title = prefix + stripped;
        }
      }
      apply();
      if (!window.__workerTitleObs) {
        var el = document.querySelector('title');
        if (el) {
          var obs = new MutationObserver(apply);
          obs.observe(el, {childList: true, subtree: true, characterData: true});
          window.__workerTitleObs = obs;
        }
      }
    })(""" + str(wid) + ");"
    try:
        page.evaluate(js)
    except Exception:
        pass


def _select_game_dropdown(page, game_name_gm):
    """Pilih Game: input text biasa, ketik nama, klik opsi autocomplete."""
    add_log(f"[GM] Pilih Game: {game_name_gm}")

    # Game field: <input placeholder="Please Select Game"> di form (BUKAN header search).
    game_input = None
    for sel in [
        "input[placeholder='Please Select Game']",
        "xpath=//main//input[@placeholder='Please Select Game']",
        "xpath=//*[normalize-space()='Game' or normalize-space()='Game *']/following::input[@placeholder='Please Select Game'][1]",
        "xpath=//label[starts-with(normalize-space(),'Game')]/following::input[@type='text'][1]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=3000)
            game_input = loc
            break
        except Exception:
            continue
    if game_input is None:
        raise Exception("Game input tidak ditemukan")

    game_input.click()
    smart_wait(page, 300, 600)

    # Setelah click, dropdown terbuka & focus pindah ke search input.
    search_box = None
    for sel in [
        "input[placeholder='Type to search...']",
        "input[placeholder*='ype to search' i]",
        "input[placeholder*='earch' i]:focus",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=800)
            search_box = loc
            break
        except Exception:
            continue

    if search_box is not None:
        try:
            search_box.click()
        except Exception:
            pass
    # Clear + type cepat (30ms/char, "Clash Royale" = ~0.36s)
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    page.keyboard.type(game_name_gm, delay=30)
    smart_wait(page, 600, 1000)

    # Klik option match di popup (match FULL name, bukan search_query)
    gm_lit       = _xpath_literal(game_name_gm)
    gm_lower_lit = _xpath_literal(game_name_gm.lower())
    upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    lower = "abcdefghijklmnopqrstuvwxyz"
    selectors = [
        f"xpath=//*[@role='option' and normalize-space()={gm_lit}]",
        f"xpath=//li[normalize-space()={gm_lit}]",
        f"xpath=//div[normalize-space()={gm_lit} and (contains(@class,'cursor-pointer') or contains(@class,'hover'))]",
        f"xpath=//*[normalize-space(text())={gm_lit}]",
        f"xpath=//*[@role='option'][contains(translate(normalize-space(.),'{upper}','{lower}'),{gm_lower_lit})]",
    ]
    # Fallback fuzzy: kalau nama ada ':', match option yg mengandung suffix (bagian setelah ':')
    # Contoh: sheet 'Mobile Legend: Bang Bang' vs web 'Mobile Legends: Bang Bang' -> match via 'bang bang'
    if ":" in game_name_gm:
        suffix = game_name_gm.split(":", 1)[1].strip().lower()
        if suffix:
            suffix_lit = _xpath_literal(suffix)
            selectors.append(
                f"xpath=//*[@role='option'][contains(translate(normalize-space(.),'{upper}','{lower}'),{suffix_lit})]"
            )
    option = None
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=1500)
            option = loc
            break
        except Exception:
            continue
    if option is None:
        raise Exception(f"Opsi game '{game_name_gm}' tidak muncul di dropdown")
    option.click()
    smart_wait(page, 400, 800)


def _select_type_accounts(page):
    """Pilih Type = Accounts. Type pakai radix combobox button."""
    add_log("[GM] Pilih Type: Accounts")

    # Type: <button role='combobox'> dengan placeholder "Select Type"
    dropdown = None
    for sel in [
        "button[role='combobox']:has(span:has-text('Select Type'))",
        "xpath=//button[@role='combobox' and .//span[normalize-space()='Select Type']]",
        "xpath=//label[normalize-space()='Type']/following::button[@role='combobox'][1]",
        "xpath=//*[normalize-space()='Type']/following::button[@role='combobox'][1]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=3000)
            dropdown = loc
            break
        except Exception:
            continue
    if dropdown is None:
        raise Exception("Type combobox tidak ditemukan")

    dropdown.click()
    smart_wait(page, 800, 1500)

    # Options muncul di radix portal (luar DOM combobox). Cari by role=option.
    option = None
    for sel in [
        "xpath=//*[@role='option' and normalize-space()='Accounts']",
        "[role='option']:has-text('Accounts')",
        "xpath=//div[@role='option'][normalize-space()='Accounts']",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=3000)
            option = loc
            break
        except Exception:
            continue
    if option is None:
        raise Exception("Opsi 'Accounts' tidak muncul di dropdown Type")
    option.click()
    smart_wait(page, 1000, 3000)


def scrape_form_options(page):
    """Scrape semua dropdown dinamis SETELAH Game+Type dipilih.
    Label diderive dari placeholder span 'Select X' di dalam button combobox.
    Return dict {field_label: [options]}."""
    options_map = {}

    # Tunggu field dinamis render (bergantung pada Type=Accounts)
    page.wait_for_timeout(2500)

    # Kumpulkan semua dynamic combobox button - deteksi via placeholder "Select X"
    comboboxes = page.locator("button[role='combobox']").all()
    pending = []  # list of (label, button_locator_selector)

    for idx, btn in enumerate(comboboxes):
        try:
            span = btn.locator("span").first
            txt = (span.inner_text(timeout=1500) or "").strip()
        except Exception:
            continue
        if not txt.lower().startswith("select "):
            # Artinya sudah terisi atau bukan pattern 'Select X' -> skip (termasuk Type yg sudah 'Accounts')
            continue
        label = txt[7:].strip()
        if not label or label.lower() == "type":
            continue
        pending.append(label)

    add_log(f"[GM] Detected {len(pending)} dynamic dropdown(s): {pending}")

    for label in pending:
        try:
            # Re-find karena DOM bisa berubah. Cari by placeholder exact match.
            dropdown = page.locator(
                f"button[role='combobox']:has(span:text-is('Select {label}'))"
            ).first
            if not dropdown.is_visible():
                continue
            dropdown.click()
            page.wait_for_timeout(800)

            opts = page.locator("[role='option']:visible").all()
            opt_texts = []
            for o in opts:
                try:
                    t = (o.inner_text(timeout=800) or "").strip()
                    if t and t not in opt_texts:
                        opt_texts.append(t)
                except Exception:
                    pass
            if opt_texts:
                options_map[label] = opt_texts
                add_log(f"[GM]    - {label}: {len(opt_texts)} opsi -> {opt_texts[:5]}{'...' if len(opt_texts)>5 else ''}")
            else:
                add_log(f"[GM]    {label}: opsi tidak terdeteksi")

            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
        except Exception as e:
            add_log(f"[GM] Gagal scrape '{label}': {str(e)[:80]}")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            continue

    return options_map


def select_dropdown_by_label(page, label_text, option_value):
    """Klik combobox 'Select {label_text}' lalu pilih option."""
    add_log(f"[GM] Isi {label_text}: {option_value}")
    try:
        dropdown = page.locator(
            f"button[role='combobox']:has(span:text-is('Select {label_text}'))"
        ).first
        dropdown.wait_for(state="visible", timeout=10000)
        dropdown.click()
        smart_wait(page, 700, 1300)

        # Opsi muncul di radix portal
        opt_lit = _xpath_literal(option_value)
        # :has-text() Playwright selector butuh escape \' untuk apostrof di CSS string
        opt_css_safe = option_value.replace("\\", "\\\\").replace("'", "\\'")
        option = None
        for sel in [
            f"xpath=//*[@role='option' and normalize-space()={opt_lit}]",
            f"[role='option']:has-text('{opt_css_safe}')",
            f"xpath=//*[normalize-space(text())={opt_lit}]",
        ]:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=3000)
                option = loc
                break
            except Exception:
                continue
        if option is None:
            raise Exception(f"opsi '{option_value}' tidak ditemukan")
        option.click()
        smart_wait(page, 500, 1000)
    except Exception as e:
        add_log(f"[GM] Gagal isi '{label_text}' = '{option_value}': {str(e)[:80]}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass


# ===================== GM CREATE LISTING =====================
def create_listing_gm(game_name_gm, title, deskripsi, harga, field_mapping, image_paths):
    """Full flow isi form & submit. Return (berhasil, error_message)."""
    with sync_playwright() as p:
        page = None
        try:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{CHROME_DEBUG_PORT}", timeout=10000)
            context = get_or_create_context(browser)
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()

            add_log("[GM] Membuka halaman create listing...")
            page.goto("https://gamemarket.gg/dashboard/create-listing",
                      wait_until="networkidle", timeout=30000)
            _set_worker_tab_title(page)
            smart_wait(page, 3000, 5000)

            # 1. Game
            _select_game_dropdown(page, game_name_gm)

            # 2. Type = Accounts
            _select_type_accounts(page)

            # 3. Dynamic fields dari Gemini
            for field_label, value in field_mapping.items():
                if value is None:
                    continue
                select_dropdown_by_label(page, field_label, value)
                smart_wait(page, 500, 1000)

            # 4. Title
            add_log("[GM] Isi Title...")
            title_input = page.locator(
                "input[placeholder*='itle' i], input#title, input[name='title']"
            ).first
            title_input.wait_for(state="visible", timeout=10000)
            title_input.fill(title)
            smart_wait(page, 500, 1000)

            # 5. Description
            add_log("[GM] Isi Description...")
            desc_input = page.locator(
                "textarea[placeholder*='escribe' i], textarea[name='description']"
            ).first
            desc_input.wait_for(state="visible", timeout=10000)
            desc_input.fill(deskripsi)
            smart_wait(page, 500, 1000)

            # 6. Duration = 90 days (radix radio button)
            add_log("[GM] Pilih Duration: 90 days")
            try:
                duration_btn = None
                for sel in [
                    "button[role='radio'][value='90']",
                    "xpath=//button[@role='radio' and @value='90']",
                    "xpath=//label[normalize-space()='90 days']/preceding-sibling::button[@role='radio']",
                    "xpath=//label[normalize-space()='90 days']/following-sibling::button[@role='radio']",
                ]:
                    try:
                        loc = page.locator(sel).first
                        loc.wait_for(state="visible", timeout=2500)
                        duration_btn = loc
                        break
                    except Exception:
                        continue
                if duration_btn is None:
                    raise Exception("tombol radio 90 days tidak ditemukan")
                duration_btn.click()
                smart_wait(page, 500, 1000)
            except Exception as e:
                add_log(f"[GM] Gagal pilih Duration 90: {str(e)[:80]}")

            # 7. Delivery = In-Chat (radix checkbox button)
            add_log("[GM] Pilih Delivery: In-Chat Delivery")
            try:
                delivery_cb = None
                for sel in [
                    "button[role='checkbox'][value='In-Chat Delivery']",
                    "button[role='checkbox'][id='In-Chat Delivery']",
                    "xpath=//button[@role='checkbox' and @value='In-Chat Delivery']",
                    "xpath=//label[normalize-space()='In-Chat Delivery']/preceding-sibling::button[@role='checkbox']",
                    "xpath=//label[normalize-space()='In-Chat Delivery']/following-sibling::button[@role='checkbox']",
                ]:
                    try:
                        loc = page.locator(sel).first
                        loc.wait_for(state="visible", timeout=2500)
                        delivery_cb = loc
                        break
                    except Exception:
                        continue
                if delivery_cb is None:
                    raise Exception("tombol checkbox In-Chat Delivery tidak ditemukan")
                state = delivery_cb.get_attribute("data-state") or ""
                if state != "checked":
                    delivery_cb.click()
                smart_wait(page, 500, 1000)
            except Exception as e:
                add_log(f"[GM] Gagal centang In-Chat: {str(e)[:80]}")

            # 8. Price - strip simbol mata uang, input type=number hanya terima angka
            price_raw = str(harga)
            price_clean = re.sub(r'[^0-9.,]', '', price_raw).replace(',', '.')
            # Handle multi-dot (1.234.56 -> 1234.56) -> keep last dot as decimal
            if price_clean.count('.') > 1:
                parts = price_clean.split('.')
                price_clean = ''.join(parts[:-1]) + '.' + parts[-1]
            add_log(f"[GM] Isi Price: ${price_clean}")
            price_input = page.locator(
                "input[placeholder*='rice' i], input[name='price'], input[type='number']"
            ).first
            price_input.wait_for(state="visible", timeout=10000)
            price_input.fill(price_clean)
            smart_wait(page, 500, 1000)

            # 9. Stock = 1
            try:
                stock_input = page.locator("input[name='stock'], input[placeholder*='tock' i]").first
                if stock_input.count() > 0 and stock_input.is_visible():
                    stock_input.fill("1")
                    smart_wait(page, 300, 600)
            except Exception:
                pass

            # 10. Min Order = 1
            try:
                min_input = page.locator(
                    "input[name*='min' i], input[placeholder*='in' i][placeholder*='rder' i]"
                ).first
                if min_input.count() > 0 and min_input.is_visible():
                    min_input.fill("1")
                    smart_wait(page, 300, 600)
            except Exception:
                pass

            # 11. Upload images - HARDENED: retry + verifikasi preview muncul.
            # Kalau image_paths ada tapi upload gagal total -> abort (jangan publish
            # listing tanpa gambar). Verifikasi: blob:-URL preview count >= expected.
            if image_paths:
                expected = len(image_paths)
                upload_ok = False
                last_err = ""
                for attempt in range(1, 4):  # 3x retry
                    add_log(f"[GM] Upload {expected} gambar (attempt {attempt}/3)...")
                    try:
                        file_input = page.locator("input[type='file']").first
                        file_input.set_input_files(image_paths)
                    except Exception as e:
                        last_err = str(e)[:80]
                        add_log(f"[GM] set_input_files gagal: {last_err}")
                        smart_wait(page, 1500, 2500)
                        continue

                    # Poll max 60s: tunggu preview blob:-images muncul sejumlah expected.
                    # Upload bisa lama karena client-side resize + server ack.
                    preview_loc = page.locator("img[src^='blob:'], img[src^='data:image']")
                    got = 0
                    deadline_ms = 60000
                    step_ms = 500
                    elapsed = 0
                    while elapsed < deadline_ms:
                        try:
                            got = preview_loc.count()
                        except Exception:
                            got = 0
                        if got >= expected:
                            break
                        page.wait_for_timeout(step_ms)
                        elapsed += step_ms

                    if got >= expected:
                        upload_ok = True
                        add_log(f"[GM] Upload sukses - {got}/{expected} preview terverifikasi")
                        break

                    last_err = f"preview {got}/{expected} setelah {elapsed//1000}s"
                    add_log(f"[GM] Upload belum komplit ({last_err}), retry...")
                    # Reset file input sebelum retry (kosongkan supaya fresh upload).
                    try:
                        file_input.set_input_files([])
                    except Exception:
                        pass
                    smart_wait(page, 1500, 3000)

                if not upload_ok:
                    err = f"Upload gambar gagal setelah 3x retry ({last_err})"
                    add_log(f"[GM] {err} - abort publish")
                    return False, err

            # 12. Terms of Service - GameMarket auto-centang, skip.

            # 13. Publish Listing
            add_log("[GM] Klik Publish Listing...")
            publish_btn = page.locator("button:has-text('Publish Listing')").first
            publish_btn.wait_for(state="visible", timeout=10000)
            start_url = page.url
            publish_btn.click()

            # 14. Deteksi sukses: (a) redirect URL, atau (b) title input di-clear oleh backend.
            # Poll max 120s (2 menit) - khusus tahap publish karena GM proses upload
            # gambar + server-side validation bisa lama saat peak.
            title_loc = page.locator(
                "input[placeholder*='itle' i], input#title, input[name='title']"
            ).first
            redirected = False
            cleared = False
            for _ in range(240):
                page.wait_for_timeout(500)
                try:
                    cur = page.url
                    if cur != start_url and "/create-listing" not in cur:
                        redirected = True
                        break
                except Exception:
                    pass
                try:
                    cur_title = (title_loc.input_value(timeout=500) or "").strip()
                    if cur_title == "":
                        cleared = True
                        break
                except Exception:
                    pass

            if redirected:
                add_log(f"[GM] Redirect ke: {page.url} -> sukses")
                smart_wait(page, 1000, 2000)
                return True, None
            if cleared:
                add_log("[GM] Form di-reset (title kosong) -> sukses")
                smart_wait(page, 1000, 2000)
                return True, None

            # Cek toast sukses
            try:
                success_toast = page.locator(
                    "xpath=//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'success')"
                    " or contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'published')]"
                )
                if success_toast.count() > 0:
                    return True, None
            except Exception:
                pass

            try:
                # Kumpulkan SEMUA pesan error di form, FILTER asterisk/teks required biasa
                err_selectors = [
                    "[role='alert']",
                    ".text-red-500", ".text-red-600", ".text-red-400",
                    "[class*='text-red']",
                    ".error", ".toast-error",
                    "p.text-red", "span.text-red",
                ]
                all_msgs = []
                noise = {"*", "required", "please fill", ""}
                for sel in err_selectors:
                    try:
                        locs = page.locator(sel).all()
                        for l in locs:
                            try:
                                if not l.is_visible():
                                    continue
                                t = (l.inner_text(timeout=1000) or "").strip()
                                if not t or len(t) < 5 or len(t) > 200:
                                    continue
                                if t.strip() in noise or t.strip() == "*":
                                    continue
                                if t not in all_msgs:
                                    all_msgs.append(t)
                            except Exception:
                                continue
                    except Exception:
                        continue
                if all_msgs:
                    combined = " | ".join(all_msgs[:5])
                    add_log(f"[GM] Form error detail: {combined[:300]}")
                    return False, f"Form error: {combined[:150]}"
                return False, "Submit tidak redirect, status tidak jelas"
            except Exception as e:
                return False, f"Submit tidak redirect ({str(e)[:60]})"

        except Exception as e:
            pesan = str(e)
            if "Timeout" in pesan:
                indo_error = "Waktu habis, elemen tidak ditemukan"
            elif "net::" in pesan:
                indo_error = "Gagal membuka halaman, cek koneksi"
            else:
                indo_error = f"Error: {pesan[:100]}"
            add_log(f"[GM] Gagal: {indo_error}")
            return False, indo_error

        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


def scrape_and_cache_form_options(game_name_gm):
    """Buka /create-listing, pilih Game + Type=Accounts, scrape form options.
    Return:
      dict non-empty = scrape sukses dengan field
      {} = scrape sukses tapi game tanpa dynamic form (caller tulis sentinel ke O45)
      None = scrape fail (exception/timeout) - jangan cache, retry next cycle
    """
    add_log("[GM] Scrape form options dari GM (pertama kali)...")
    with sync_playwright() as p:
        page = None
        try:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{CHROME_DEBUG_PORT}", timeout=10000)
            context = get_or_create_context(browser)
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()

            page.goto("https://gamemarket.gg/dashboard/create-listing",
                      wait_until="networkidle", timeout=30000)
            _set_worker_tab_title(page)
            smart_wait(page, 3000, 5000)

            _select_game_dropdown(page, game_name_gm)
            _select_type_accounts(page)

            return scrape_form_options(page)

        except Exception as e:
            add_log(f"[GM] Gagal scrape form options: {str(e)[:100]}")
            return None
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


# ===================== SHEET SCANNER =====================
_prefetched_active_sheets = None  # di-set orchestrator dari shared prescan


def set_prefetched_active_sheets(names):
    """Inject hasil prescan LINK dari orchestrator. One-shot: dikonsumsi sekali."""
    global _prefetched_active_sheets
    _prefetched_active_sheets = list(names) if names is not None else None


def get_active_sheet_names():
    """Baca LINK!A2:A (nama tab) + LINK!D2:D (jumlah PERLU POST per tab) dalam 1
    batch_get call. Return hanya tab yang D > 0. Baris kosong di A di-skip
    otomatis (spacer). Payload super ringan - idle case cuma 2 range."""
    global _prefetched_active_sheets
    if _prefetched_active_sheets is not None:
        candidates = _prefetched_active_sheets
        _prefetched_active_sheets = None
        if not candidates:
            return candidates
        try:
            actual = {ws.title for ws in spreadsheet_client.worksheets()}
        except Exception:
            add_log(f"LINK!D (prescan): {len(candidates)} tab aktif")
            return candidates
        out = [n for n in candidates if n in actual]
        dropped = len(candidates) - len(out)
        if dropped:
            add_log(f"Abaikan {dropped} entri LINK (sheet tidak ada / placeholder)")
        add_log(f"LINK!D (prescan): {len(out)} tab aktif")
        return out
    link_sheet = spreadsheet_client.worksheet(LINK_SHEET_NAME)
    try:
        resp = sheet_read_with_backoff(
            link_sheet.batch_get, ["A2:A", "D2:D"], desc="link_scan",
        )
    except Exception as e:
        add_log(f"Gagal baca LINK A/D: {e}")
        return []

    a_col = resp[0] if len(resp) > 0 else []
    d_col = resp[1] if len(resp) > 1 else []

    candidates = []
    for i, arow in enumerate(a_col):
        name = arow[0].strip() if arow and len(arow) > 0 else ""
        if not name:
            continue  # spacer row
        count_str = ""
        if i < len(d_col) and d_col[i] and len(d_col[i]) > 0:
            count_str = str(d_col[i][0]).strip()
        try:
            count = int(float(count_str))
        except (ValueError, TypeError):
            count = 0
        if count > 0:
            candidates.append(name)

    if not candidates:
        return []

    # Filter vs worksheet aktual (jaga-jaga typo/tab terhapus)
    try:
        actual = {ws.title for ws in spreadsheet_client.worksheets()}
    except Exception:
        return candidates
    out = [n for n in candidates if n in actual]
    dropped = len(candidates) - len(out)
    if dropped:
        add_log(f"Abaikan {dropped} entri LINK (sheet tidak ada / placeholder)")
    return out


# ===================== PROSES PER BARIS =====================
def proses_baris(sheet, data, baris_index, game_name_gm, deskripsi, form_options_cache, worker_id=1):
    """Proses 1 baris produk. Return form_options_cache (mungkin baru di-scrape)."""
    _worker_local.worker_id = worker_id
    baris_nomor = baris_index + 1
    row = data[baris_index] if baris_index < len(data) else []

    harga = row[KOLOM_HARGA - 1].strip() if len(row) >= KOLOM_HARGA else ""
    gambar_url = row[KOLOM_GAMBAR - 1].strip() if len(row) >= KOLOM_GAMBAR else ""
    title = row[KOLOM_JUDUL - 1].strip() if len(row) >= KOLOM_JUDUL else ""
    kode_listing = row[0].strip() if len(row) >= 1 else ""

    # Platform code diambil dari row 48 kolom O (KOLOM_TRIGGER-1 = index 14).
    # Saat bot_create extend ke platform lain (G2G/Z2U/dll), ini akan ikut berubah
    # per kolom yang diproses.
    platform_row_idx = BARIS_PLATFORM_CODE - 1
    try:
        platform = data[platform_row_idx][KOLOM_TRIGGER - 1].strip().upper()
    except (IndexError, AttributeError):
        platform = "GM"  # fallback - bot_create saat ini hanya dukung GM

    try:
        sheet_gid = sheet.id
    except Exception:
        sheet_gid = 0
    info = {"sheet": sheet.title, "row": baris_nomor, "gid": sheet_gid}
    current_row_info["val"] = info  # last-write for legacy GUI
    sheet_url = (f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
                 f"/edit#gid={sheet_gid}&range=A{baris_nomor}")
    with worker_status_lock:
        worker_status[worker_id] = {
            "sheet": sheet.title, "row": baris_nomor, "gid": sheet_gid,
            "text": f"{sheet.title} | row{baris_nomor}",
            "url": sheet_url,
        }

    add_log(f"Proses baris {baris_nomor} | Game: {game_name_gm} | Title: {title[:50]}...")

    # 0. Validasi kode listingan (kolom A) harus ada di title (kolom J)
    if not kode_listing or kode_listing not in title:
        add_log(f"Kode listingan '{kode_listing}' tidak ada di title - skip baris {baris_nomor}")
        try:
            with_sheet_lock(
                sheet_write_lock,
                lambda: safe_update_cell(sheet, baris_nomor, KOLOM_CATATAN, "❌ Code listingan tidak ada di Title",
                                         timeout=45, desc=f"no-code row {baris_nomor}"),
                lock_timeout=60, desc=f"no-code row {baris_nomor}"
            )
        except Exception as e:
            add_log(f"Gagal tulis catatan no-code: {e}")
        return form_options_cache

    # 0b. Validasi panjang title max 150 char
    if len(title) > 150:
        add_log(f"Title {len(title)} char (>150) - skip baris {baris_nomor}")
        try:
            with_sheet_lock(
                sheet_write_lock,
                lambda: safe_update_cell(sheet, baris_nomor, KOLOM_CATATAN, "❌ Title terlalu panjang lebih dari 150 Character",
                                         timeout=45, desc=f"title-too-long row {baris_nomor}"),
                lock_timeout=60, desc=f"title-too-long row {baris_nomor}"
            )
        except Exception as e:
            add_log(f"Gagal tulis catatan title-too-long: {e}")
        return form_options_cache

    # 1. Tulis ON WORKING... ke K
    on_working = f"ON WORKING {worker_id} !!!"
    try:
        with_sheet_lock(
            sheet_write_lock,
            lambda: safe_update_cell(sheet, baris_nomor, KOLOM_CATATAN, on_working,
                                     timeout=45, desc=f"ON WORKING row {baris_nomor}"),
            lock_timeout=60, desc=f"ON WORKING row {baris_nomor}"
        )
    except Exception as e:
        add_log(f"Gagal tulis ON WORKING: {e}")

    downloaded_paths = []
    try:
        # 2. Scrape form options jika belum cached - serialized per-sheet supaya
        #    worker lain yang kena sheet sama tidak duplicate scrape.
        #    None = belum pernah scrape. {} = sentinel (game tanpa form, skip AI).
        if form_options_cache is None:
            scrape_lk = _get_sheet_scrape_lock(sheet.title)
            with scrape_lk:
                # Re-check memcache: mungkin worker lain barusan selesai scrape
                if sheet.title in _scrape_memcache:
                    form_options_cache = _scrape_memcache[sheet.title]
                    if form_options_cache == {}:
                        add_log("Pakai memcache worker lain: sentinel (game tanpa form, skip AI)")
                    else:
                        add_log(f"Pakai form options dari worker lain (memcache): {len(form_options_cache)} field")
                else:
                    # Re-read O45 langsung dari sheet (jaga-jaga kalau worker sebelumnya
                    # sudah nulis ke sheet tapi belum ke memcache karena crash, dsb)
                    try:
                        fresh_raw = call_with_timeout(
                            lambda: sheet.cell(BARIS_FORM_OPTIONS, KOLOM_CACHE).value,
                            timeout=20, name="reread O45"
                        ) or ""
                        fresh_stripped = fresh_raw.strip()
                        if fresh_stripped.startswith(NO_OPTIONS_SENTINEL):
                            form_options_cache = {}
                            _scrape_memcache[sheet.title] = {}
                            add_log("O45 sentinel re-read: game tanpa form, skip AI")
                        elif fresh_stripped:
                            form_options_cache = json.loads(fresh_stripped)
                            _scrape_memcache[sheet.title] = form_options_cache
                            add_log(f"Pakai form options dari O45 re-read: {len(form_options_cache)} field")
                    except Exception:
                        pass

                    if form_options_cache is None:
                        scraped = scrape_and_cache_form_options(game_name_gm)
                        if scraped is None:
                            # Scrape fail (exception/timeout) - jangan tulis sentinel.
                            # Retry di cycle berikutnya.
                            add_log("Scrape form gagal, retry cycle berikutnya")
                        elif scraped == {}:
                            # Scrape sukses tapi game tanpa dynamic form.
                            # Tulis sentinel + timestamp ke O45 supaya next cycle skip.
                            form_options_cache = {}
                            _scrape_memcache[sheet.title] = {}
                            sentinel_val = f"{NO_OPTIONS_SENTINEL} {time.strftime('%Y-%m-%d %H:%M:%S')}"
                            try:
                                with_sheet_lock(
                                    sheet_write_lock,
                                    lambda: safe_update_cell(sheet, BARIS_FORM_OPTIONS, KOLOM_CACHE, sentinel_val,
                                                             timeout=45, desc="sentinel O45"),
                                    lock_timeout=60, desc="sentinel O45"
                                )
                                add_log(f"O45 ditandai sentinel '{sentinel_val}' - next cycle skip AI")
                            except Exception as e:
                                add_log(f"Gagal tulis sentinel O45: {e}")
                        else:
                            form_options_cache = scraped
                            _scrape_memcache[sheet.title] = form_options_cache
                            try:
                                json_str = json.dumps(form_options_cache, ensure_ascii=False)
                                with_sheet_lock(
                                    sheet_write_lock,
                                    lambda: safe_update_cell(sheet, BARIS_FORM_OPTIONS, KOLOM_CACHE, json_str,
                                                             timeout=45, desc="cache O45"),
                                    lock_timeout=60, desc="cache O45"
                                )
                                add_log(f"Form options di-cache ke O45: {len(form_options_cache)} field")
                            except Exception as e:
                                add_log(f"Gagal tulis cache O45: {e}")

        # 3. AI mapping - skip kalau form_options_cache {} (sentinel) atau None (scrape fail)
        field_mapping = {}
        if form_options_cache:
            try:
                field_mapping = ai_map_fields(game_name_gm, title, form_options_cache)
            except TimeoutHangError:
                raise Exception("Gemini timeout (>60s)")
            except Exception as e:
                raise Exception(f"Gemini error: {str(e)[:80]}")

        # 4. Download gambar - WAJIB ada gambar, kalau gagal stop
        add_log(f"Download gambar dari: {gambar_url}")
        downloaded_paths = download_images(gambar_url)
        if not downloaded_paths:
            err_note = "❌ Gambar tidak bisa di download"
            try:
                with_sheet_lock(
                    sheet_write_lock,
                    lambda: safe_update_cell(sheet, baris_nomor, KOLOM_CATATAN, err_note,
                                             timeout=45, desc=f"no-image row {baris_nomor}"),
                    lock_timeout=60, desc=f"no-image row {baris_nomor}"
                )
            except Exception as e:
                add_log(f"Gagal tulis error ke sheet: {e}")
            update_stats(platform, False)
            add_log(f"Baris {baris_nomor} dibatalkan: gambar gagal didownload")
            return form_options_cache

        # 5. Create listing
        berhasil, error_msg = create_listing_gm(
            game_name_gm, title, deskripsi, harga, field_mapping, downloaded_paths
        )

        # 6. Post-process
        if berhasil:
            jumlah_gambar = len(downloaded_paths)
            timestamp = datetime.now().strftime("%d %b, %y | %H:%M")
            report = f"✅ GM | {jumlah_gambar} images uploaded | {timestamp}"
            def _success_writes():
                safe_update_cell(sheet, baris_nomor, KOLOM_TRIGGER, "TRUE",
                                 timeout=45, desc=f"TRUE row {baris_nomor}")
                safe_update_cell(sheet, baris_nomor, KOLOM_CATATAN, report,
                                 timeout=45, desc=f"report K row {baris_nomor}")
            try:
                with_sheet_lock(sheet_write_lock, _success_writes,
                                lock_timeout=120, desc=f"success row {baris_nomor}")
                add_log(report)
            except Exception as e:
                add_log(f"Gagal tulis hasil sukses ke sheet: {e}")
            update_stats(platform, True)
            add_log(f"Baris {baris_nomor} SUKSES di-listing! Game: {game_name_gm} | {jumlah_gambar} gambar")
        else:
            try:
                with_sheet_lock(
                    sheet_write_lock,
                    lambda: safe_update_cell(sheet, baris_nomor, KOLOM_CATATAN, f"❌ {error_msg}",
                                             timeout=45, desc=f"fail row {baris_nomor}"),
                    lock_timeout=60, desc=f"fail row {baris_nomor}"
                )
            except Exception as e:
                add_log(f"Gagal tulis error ke sheet: {e}")
            update_stats(platform, False)
            add_log(f"Baris {baris_nomor} GAGAL: {error_msg}")

    except Exception as e:
        err = str(e)[:120]
        add_log(f"Baris {baris_nomor} exception: {err}")
        try:
            with_sheet_lock(
                sheet_write_lock,
                lambda: safe_update_cell(sheet, baris_nomor, KOLOM_CATATAN, f"❌ {err}",
                                         timeout=45, desc=f"exc row {baris_nomor}"),
                lock_timeout=60, desc=f"exc row {baris_nomor}"
            )
        except Exception:
            pass
        update_stats(platform, False)

    finally:
        cleanup_temp_images(downloaded_paths)
        current_row_info["val"] = None
        with worker_status_lock:
            worker_status.pop(worker_id, None)

    return form_options_cache


# ===================== ENTRY POINT =====================
BOT_NAME = "create"


def run_one_cycle(ctx):
    """1 cycle: scan LINK!D -> collect max MAX_WORKER candidates -> dispatch paralel.
    Return: jumlah row yang diproses (>=0). Orchestrator di main.py yang handle
    idle sleep + Chrome keep-alive antar cycle (tidak lagi bot_loop di file ini).
    """
    _bind_ctx(ctx)

    if ctx.stop_event.is_set():
        return 0
    if not ctx.toggles.should_keep_running(BOT_NAME):
        return 0
    if ctx.sheets.spreadsheet is None:
        add_log("Sheets belum ter-connect, skip cycle")
        return 0

    ctx.progress.set(BOT_NAME, {"phase": "scanning"})

    # ====== PHASE 0: scan LINK!A/D (ringan) ======
    try:
        sheet_names = call_with_timeout(
            get_active_sheet_names, timeout=60, name="get_active_sheet_names"
        )
    except Exception as e:
        add_log(f"Gagal baca sheet LINK: {str(e)[:150]}")
        ctx.progress.set(BOT_NAME, {"phase": "idle"})
        return 0

    if not sheet_names:
        ctx.progress.set(BOT_NAME, {"phase": "idle", "current_sheet": None})
        return 0

    # ====== DEEP BATCH SCAN (hanya tab aktif dari LINK) ======
    # Scan header (O43:O45) + kode (A) + harga/gambar/title (H:J) + trigger
    # (AJ) untuk tab yang LINK!D > 0 saja. Hemat payload signifikan vs scan
    # semua tab.
    t0 = time.time()
    try:
        scan_data = batch_scan_all_sheets(spreadsheet_client, sheet_names)
    except Exception as e:
        add_log(f"batch_scan_all_sheets error: {str(e)[:150]}")
        ctx.progress.set(BOT_NAME, {"phase": "idle"})
        return 0
    scan_dur = time.time() - t0

    if not scan_data:
        add_log("Batch scan gagal / kosong, skip cycle")
        ctx.progress.set(BOT_NAME, {"phase": "idle"})
        return 0

    sheets_to_scan = [n for n in sheet_names if scan_data.get(n, {}).get("game")]
    skipped_count = len(sheet_names) - len(sheets_to_scan)
    skip_note = f" (skip {skipped_count} tab O43 kosong)" if skipped_count else ""
    add_log(f"Deep scan: {len(sheet_names)} tab aktif dalam {scan_dur:.1f}s{skip_note}")

    # ====== PHASE 1: collect candidates dari scan_data ======
    candidates = []  # list of (sheet, data, i, game_name_gm, deskripsi, form_options_cache)
    for sheet_name in sheets_to_scan:
        if len(candidates) >= MAX_WORKER:
            break
        if not wait_if_paused("collect candidates"):
            break

        sdata = scan_data[sheet_name]
        game_name_gm = sdata["game"]
        deskripsi = sdata["deskripsi"]
        cache_raw = sdata["cache"]
        rows = sdata["rows"]  # index 0 = baris 51

        # Parse O45 cache sekali per-sheet.
        # None = belum scrape (worker akan scrape), {} = sentinel (skip AI), dict = form options.
        form_options_cache = None
        if cache_raw:
            cache_stripped = cache_raw.strip()
            if cache_stripped.startswith(NO_OPTIONS_SENTINEL):
                form_options_cache = {}  # sentinel: game tanpa form, skip AI
            else:
                try:
                    form_options_cache = json.loads(cache_stripped)
                except Exception:
                    add_log(f"O45 corrupt di sheet '{sheet_name}', akan scrape ulang")
                    form_options_cache = None

        # Scan rows (baris 51+) - trigger = AJ berisi "PERLU POST".
        # Formula AJ di sheet sudah menghitung semua kondisi (G/H/I/J isi,
        # K/M kosong, O!=TRUE, title <=150 char) jadi bot cukup cek AJ.
        sheet_obj = None  # lazy - cuma resolve worksheet kalau ada candidate
        for row_idx, r in enumerate(rows):
            if len(candidates) >= MAX_WORKER:
                break
            if r["trigger_aj"].strip().upper() != "PERLU POST":
                continue

            # Candidate ketemu - resolve worksheet object (sekali saja per sheet).
            # Wrap call_with_timeout 30s - cegah hang infinite kalau TCP ke
            # Google Sheets drop tanpa reset (pernah kejadian: bot stuck 6 jam).
            if sheet_obj is None:
                try:
                    sheet_obj = call_with_timeout(
                        spreadsheet_client.worksheet, args=(sheet_name,),
                        timeout=30, name=f"worksheet({sheet_name})"
                    )
                except Exception as e:
                    add_log(f"Gagal resolve worksheet '{sheet_name}': {str(e)[:120]}. Skip.")
                    break

            # Build minimal `data` list untuk proses_baris (cuma butuh A, H, I, J).
            # Index baris 0-based = (BARIS_MULAI - 1) + row_idx.
            absolute_row_idx = (BARIS_MULAI - 1) + row_idx
            fake_row = [""] * KOLOM_TRIGGER  # 15 cols (A..O)
            fake_row[0] = r["kode"]
            fake_row[KOLOM_HARGA - 1]  = r["harga"]
            fake_row[KOLOM_GAMBAR - 1] = r["gambar"]
            fake_row[KOLOM_JUDUL - 1]  = r["title"]
            fake_data = [[] for _ in range(absolute_row_idx)] + [fake_row]

            candidates.append((sheet_obj, fake_data, absolute_row_idx,
                               game_name_gm, deskripsi, form_options_cache))

    # ====== PHASE 2: dispatch N worker paralel ======
    if not candidates:
        ctx.progress.set(BOT_NAME, {"phase": "idle", "current_sheet": None,
                                     "current_row": None})
        return 0

    add_log(f"Dispatch {len(candidates)} worker paralel...")

    def _worker_runner(cand, wid):
        s, d, idx, gnm, desk, cache = cand
        try:
            proses_baris(s, d, idx, gnm, desk, cache, worker_id=wid)
        except Exception as e:
            _worker_local.worker_id = wid
            add_log(f"Crash tak terduga di proses_baris: {str(e)[:150]}")
        finally:
            with worker_status_lock:
                worker_status.pop(wid, None)

    threads = []
    for idx, cand in enumerate(candidates):
        wid = idx + 1
        t = threading.Thread(
            target=_worker_runner, args=(cand, wid), daemon=True,
            name=f"create-worker-{wid}"
        )
        threads.append(t)
        t.start()
        if idx < len(candidates) - 1:
            time.sleep(random.uniform(2.0, 3.0))  # stagger biar tidak rebut CDP

    for t in threads:
        t.join(timeout=600)  # max 10 menit per worker
        if t.is_alive():
            add_log(f"{t.name} timeout 10 menit, thread masih jalan, lanjut scan...")
            if _ctx is not None:
                _ctx.zombies.track(t, "create")

    # Cleanup tab dihandle di orchestrator (main.py) - hindari double-call yang bikin blink.
    ctx.progress.set(BOT_NAME, {"phase": "idle", "current_sheet": None,
                                 "current_row": None})
    return len(candidates)


# ===================== (GUI + main() dipindah ke main.py) =====================
# create_main_window / main dihapus - module ini sekarang dipanggil oleh
# orchestrator lewat BOT_NAME="create" + run_one_cycle(ctx).
