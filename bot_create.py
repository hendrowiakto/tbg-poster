"""bot_create.py - create listings multi-market (dynamic adapter) dari Google Sheets.

Module kontrak (dipanggil oleh main.py via orchestrator):
    BOT_NAME = "create"
    def run_one_cycle(ctx) -> int:
        # 1 cycle: scan LINK!D -> ambil 1 candidate row -> spawn N-market paralel
        # sub-threads (1 thread per market aktif untuk row itu). Return jumlah
        # row diproses (0 atau 1).

Layout sheet:
- O48:Z48 = kode market (GM/G2G/Z2U/...) per kolom. Kolom yang kode-nya kosong = slot belum dipakai.
- O43:Z43 = game name per market (matched by column).
- O44:Z44 = deskripsi per market.
- O45:Z45 = form options cache per market.
- Kol K per row = status multiline. Baris "✅ {CODE} | ..." = market itu sudah done.
  K column adalah SATU-SATUNYA source of truth untuk done-detection (no more O/P TRUE).

Adapter per market di create/{CODE}.py harus expose:
    MARKET_CODE, HARGA_COL, CACHE_SENTINEL,
    scrape_form_options(game_name), run(sheet, baris, wid, **kwargs),
    cache_looks_bogus(cache_dict) [optional]

Semua infrastruktur (log, stats, Chrome, Sheets, toggle, progress) diambil dari ctx.
"""

import os
import re
import sys
import time
import threading
import random
import json
import shutil
import importlib
from datetime import datetime

from google.oauth2.service_account import Credentials  # noqa: F401 - kompat ekspor lama
from playwright.sync_api import sync_playwright

import requests
from bs4 import BeautifulSoup
import google.generativeai as genai

from shared import call_with_timeout, TimeoutHangError

# Shared adapter utilities (create/_shared.py). Runtime deps (log, temp dir,
# gemini model) di-inject di _bind_ctx() via _shared.inject_runtime(...).
from create import _shared as _create_shared
from create._shared import (
    _worker_local,
    xpath_literal as _xpath_literal,
    obfuscate_image_url as _obfuscate_image_url,
    scrape_imgur,
    scrape_postimg,
    scrape_gdrive,
    download_images,
    download_images_with_urls,
    start_image_download_async,
    cleanup_temp_images,
    extract_image_urls_for_g2g,
    ai_map_fields,
    ai_map_fields_g2g,
    ai_map_fields_combined,
    ai_map_fields_multi,
    smart_wait,
    get_or_create_context,
)

# GM + G2G market adapter. Alias ke nama lama supaya callsite orchestrator
# (_ensure_form_options_cache, _run_gm_market, _run_g2g_market) tidak berubah.
from create.GM import (
    scrape_form_options as scrape_and_cache_form_options,
    create_listing as create_listing_gm,
    GM_CREATE_URL,
)
from create.G2G import (
    scrape_form_options as scrape_and_cache_form_options_g2g,
    create_listing as create_listing_g2g,
    cache_looks_bogus as _g2g_cache_looks_bogus,
    G2G_CREATE_URL,
    NO_OPTIONS_SENTINEL_G2G,
)


if getattr(sys, 'frozen', False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Gemini model - lazy init di _bind_ctx() supaya API key dibaca dari ctx.config.
gemini_model = None

# ===================== KONSTANTA =====================
LINK_SHEET_NAME = "LINK"
BARIS_PLATFORM       = 43   # O43:Z43 = game name per market (1 kolom = 1 market)
BARIS_DESKRIPSI      = 44   # O44:Z44 = deskripsi per market
BARIS_FORM_OPTIONS   = 45   # O45:Z45 = form options cache per market
BARIS_PLATFORM_CODE  = 48   # O48:Z48 = kode market (GM/G2G/Z2U/...) -> identifies kolom
BARIS_MULAI          = 51   # data produk mulai baris 51

# Kolom data per baris (1-based). Harga per-market di resolve via module.HARGA_COL.
KOLOM_GAMBAR         = 9    # I
KOLOM_JUDUL          = 10   # J
KOLOM_CATATAN        = 11   # K - status multiline per market; SATU-SATUNYA source of truth
                             # untuk "done" detection (format "✅ {CODE} | ...").
KOLOM_EXTRA          = 13   # M (harus kosong untuk trigger)

# Range kolom platform code di row 48:
MARKET_COL_START     = 15   # O
MARKET_COL_END       = 26   # Z (inklusif)

# Back-compat aliases (dipakai existing GM code - jangan ubah):
KOLOM_HARGA          = 8    # H (GM default)
KOLOM_TRIGGER        = 15   # O (historical - tidak dipakai lagi di proses_baris_dual)
KOLOM_CACHE          = 15   # O (historical)
KOLOM_HARGA_GM       = 8
KOLOM_HARGA_G2G      = 7

NO_OPTIONS_SENTINEL     = "[tidak ditemukan options]"      # fallback generic
# NO_OPTIONS_SENTINEL_G2G & G2G_CREATE_URL di-import dari create.G2G (alias di atas).

# ===================== CTX BINDING (set at each run_one_cycle) =====================
_ctx                  = None
spreadsheet_client    = None   # di-set dari ctx.sheets.spreadsheet
CHROME_CDP_URL        = None   # di-set dari ctx.chrome.cdp_url
CHROME_DEBUG_PORT     = None   # di-set dari ctx.chrome.debug_port
SPREADSHEET_ID        = ""     # di-set dari ctx.config, dipakai build URL di proses_baris
current_sheet_label   = {"val": "-"}
current_row_info      = {"val": None}  # kompat callsite lama di proses_baris

TEMP_IMG_DIR = os.path.join(SCRIPT_DIR, "temp_images")


def _bind_ctx(ctx):
    """Bind module-level refs supaya fungsi marketplace (create_listing_gm, dll.)
    yang pakai CHROME_CDP_URL / add_log / smart_wait / spreadsheet_client tidak
    perlu diubah signature-nya."""
    global _ctx, spreadsheet_client, CHROME_CDP_URL, CHROME_DEBUG_PORT, SPREADSHEET_ID, gemini_model
    _ctx               = ctx
    spreadsheet_client = ctx.sheets.spreadsheet
    CHROME_CDP_URL     = ctx.chrome.cdp_url
    CHROME_DEBUG_PORT  = ctx.chrome.debug_port
    SPREADSHEET_ID     = ctx.config.get("SPREADSHEET_ID", "")

    if gemini_model is None:
        api_key = ctx.config.get("GEMINI_API_KEY", "")
        if api_key:
            try:
                genai.configure(api_key=api_key)
                # Pakai flash-lite: varian tanpa thinking mode. Prompt kita
                # sederhana (form-filling mapping), thinking di 2.5-flash bikin
                # latency 30-120s. Lite konsisten 1-3s untuk prompt sekecil ini.
                # (SDK lama google.generativeai belum support thinking_config=0.)
                gemini_model = genai.GenerativeModel("gemini-2.5-flash-lite")
            except Exception as e:
                add_log(f"Gagal init Gemini model: {str(e)[:120]}")

    # Inject runtime deps ke create/_shared.py (log + temp dir + gemini model).
    # Pakai getter lambda untuk gemini_model supaya nilai terkini ke-refresh
    # (lazy init di blok atas, atau re-bind di cycle berikut).
    _create_shared.inject_runtime(
        log=add_log,
        worker_temp_dir=_worker_temp_dir,
        prepare_worker_temp_dir=_prepare_worker_temp_dir,
        gemini_model=lambda: gemini_model,
        chrome_debug_port=lambda: CHROME_DEBUG_PORT,
        chrome_cdp_url=lambda: CHROME_CDP_URL,
    )


# ===================== THIN WRAPPERS (delegate ke ctx) =====================
def add_log(msg):
    """Log message via ctx.logger dengan bot prefix 'create'. Bot create single
    worker, jadi ndak perlu prefix [WORKER N]."""
    if _ctx is not None:
        _ctx.logger.log("create", msg)
    else:
        try:
            print(f"[CREATE] {msg}")
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
    """Single temp dir langsung di TEMP_IMG_DIR (bot_create single-worker,
    ndak perlu subfolder per-worker)."""
    return TEMP_IMG_DIR

# ===================== THREAD SAFETY =====================
sheet_write_lock = threading.Lock()

# ===================== MULTI-WORKER =====================
# _worker_local di-import dari create._shared (single global cross-module).
worker_status       = {}                       # {worker_id: {"sheet": str, "row": int, "gid": int}}
worker_status_lock  = threading.Lock()

# Per-sheet scrape coordination: cegah 2+ worker scrape form options sheet sama.
# Worker pertama acquire lock -> scrape -> simpan ke memcache + sheet cache_col.
# Worker lain blok sampai lock lepas -> pakai memcache, tidak scrape ulang.
# Lock & memcache per (market_code, sheet_name) karena cache cell berbeda per market.
# Dict-nya di _scrape_locks_by_code + _scrape_memcache_by_code (init di bawah).
_scrape_coord_lock = threading.Lock()


# ===================== CHROME & SHEETS (slim) =====================
# get_or_create_context di-move ke create/_shared.py (alias import di atas).


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


# ===================== MARKET MODULE REGISTRY =====================
# Cache modul adapter per kode market supaya importlib.import_module ga di-call
# tiap row. Keyed by kode uppercase. Module diharapkan ekspor:
#   MARKET_CODE, HARGA_COL, CACHE_SENTINEL,
#   scrape_form_options(game_name), run(sheet, baris, wid, **kwargs),
#   cache_looks_bogus(cache_dict) (optional).
_market_module_cache      = {}
_market_module_cache_lock = threading.Lock()

# Scrape-coordination dicts per market: {code: {sheet_name: Lock/dict}}
_scrape_locks_by_code    = {}   # {code: {sheet_name: Lock}}
_scrape_memcache_by_code = {}   # {code: {sheet_name: parsed_cache}}


def _get_market_module(code):
    """Import create/{CODE}.py lazily + cache. Return module atau None kalau gagal.
    Kode di row 48 yang belum punya adapter valid (reserve slot untuk market
    mendatang) SILENT - tidak di-log supaya ga spam tiap cycle.
    """
    if not code:
        return None
    key = code.strip().upper()
    if not key:
        return None
    with _market_module_cache_lock:
        # Gunakan `key in` supaya None (gagal) juga ke-cache - tidak retry import.
        if key in _market_module_cache:
            return _market_module_cache[key]
        try:
            mod = importlib.import_module(f"create.{key}")
        except ModuleNotFoundError:
            # Silent: slot market belum dibuat adapter-nya (normal, bukan error).
            _market_module_cache[key] = None
            return None
        except Exception as e:
            add_log(f"[MARKET] Gagal import create.{key}: {str(e)[:120]}")
            _market_module_cache[key] = None
            return None
        _market_module_cache[key] = mod
        return mod


def _get_market_scrape_lock(code, sheet_name):
    """Per (market, sheet) lock untuk cegah 2 worker scrape form options sama."""
    with _scrape_coord_lock:
        per_code = _scrape_locks_by_code.setdefault(code, {})
        lk = per_code.get(sheet_name)
        if lk is None:
            lk = threading.Lock()
            per_code[sheet_name] = lk
        return lk


def _get_market_memcache(code):
    with _scrape_coord_lock:
        return _scrape_memcache_by_code.setdefault(code, {})


# Pola K-column: "✅ GM | 3 images uploaded | 23 Apr, 26 | 17:30"
_DONE_LINE_RE = re.compile(r"^\s*✅\s*([A-Za-z0-9]+)\s*\|")


def _parse_done_codes_from_k(k_text):
    """Return set kode market yang sudah done (✅ CODE | ...) berdasar K column."""
    if not k_text:
        return set()
    out = set()
    for line in str(k_text).splitlines():
        m = _DONE_LINE_RE.match(line)
        if m:
            out.add(m.group(1).strip().upper())
    return out


def _col_letter(col_1based):
    """1-based column index -> A1 letter (max 26 untuk range kita, single char cukup)."""
    return chr(ord("A") + col_1based - 1)


def _parse_active_markets(header_rows):
    """Input: header_rows = values O43:Z48 (6 rows x 12 cols). Return list:
        [{"code": str, "col": int, "game": str, "deskripsi": str, "cache_raw": str}, ...]
    di-sort by col ascending. Row 48 (index 5) kosong = skip kolom itu.
    """
    if not header_rows:
        return []

    def _cell(r, c):
        if r >= len(header_rows):
            return ""
        row = header_rows[r]
        if c >= len(row):
            return ""
        return str(row[c]).strip()

    out = []
    total_cols = MARKET_COL_END - MARKET_COL_START + 1  # 12
    for c in range(total_cols):
        code = _cell(5, c).upper()                 # row 48
        if not code:
            continue
        out.append({
            "code":      code,
            "col":       MARKET_COL_START + c,     # 1-based absolute col
            "game":      _cell(0, c),              # row 43
            "deskripsi": _cell(1, c),              # row 44
            "cache_raw": _cell(2, c),              # row 45
        })
    return out


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
    """Batch-fetch O43:Z48 (header game/desk/cache + kode market row 48) untuk
    semua sheet dalam 1 API call. Return dict:
        {sheet_name: {"markets": [{"code","col","game","deskripsi","cache_raw"}, ...]}}
    """
    col_start = _col_letter(MARKET_COL_START)
    col_end   = _col_letter(MARKET_COL_END)
    ranges = [f"{_quote_sheet_name(n)}!{col_start}43:{col_end}48" for n in sheet_names]
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
        vals = value_ranges[idx].get("values", []) if idx < len(value_ranges) else []
        result[name] = {"markets": _parse_active_markets(vals)}
    return result


def batch_scan_all_sheets(spreadsheet, sheet_names):
    """SINGLE API call: fetch header (O43:Z48 -> dynamic market list) + kode
    (A51:A) + harga A..Z (G51:Z supaya per-market HARGA_COL bisa beda) + trigger
    (AJ51:AJ) + K catatan (K51:K) untuk SEMUA sheet aktif sekaligus.

    Trigger sumber: AJ (formula sheet). Done detection sepenuhnya dari K column
    (parse baris "✅ CODE | ...") - O/P TRUE flags tidak dipakai lagi.

    Return:
        {sheet_name: {
            "markets": [{"code","col","game","deskripsi","cache_raw"}, ...],
            "rows": [
                {"kode","gambar","title","trigger_aj","catatan",
                 "harga_by_col": {col_1based: str}},
                ...
            ]
        }}
    """
    if not sheet_names:
        return {}

    col_start_letter = _col_letter(MARKET_COL_START)  # O
    col_end_letter   = _col_letter(MARKET_COL_END)    # Z

    ranges = []
    offsets = []
    for name in sheet_names:
        q = _quote_sheet_name(name)
        start = len(ranges)
        ranges += [
            f"{q}!{col_start_letter}43:{col_end_letter}48",   # 0: header O43:Z48
            f"{q}!A{BARIS_MULAI}:A",                           # 1: kode
            f"{q}!G{BARIS_MULAI}:J",                           # 2: G..J (harga_g2g, harga_gm, gambar, title)
            f"{q}!AJ{BARIS_MULAI}:AJ",                         # 3: trigger AJ
            f"{q}!K{BARIS_MULAI}:K",                           # 4: catatan K (done detection)
            f"{q}!{col_start_letter}{BARIS_MULAI}:{col_end_letter}",  # 5: centang O:Z per row
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
        hdr_vals = _get_range(start + 0)
        markets = _parse_active_markets(hdr_vals)

        kode_col = _get_range(start + 1)
        gij_col  = _get_range(start + 2)   # G,H,I,J (4 cols)
        aj_col   = _get_range(start + 3)
        k_col    = _get_range(start + 4)
        oz_col   = _get_range(start + 5)   # O..Z centang per row

        max_len = max(len(kode_col), len(gij_col), len(aj_col), len(k_col),
                      len(oz_col), 0)

        rows = []
        for i in range(max_len):
            # Snapshot harga per kolom yang dipakai market aktif. Saat ini G/H
            # cukup (G2G=7, others=8); extend gampang kalau market baru butuh
            # kolom lain.
            harga_g = _cell(gij_col, i, 0)   # G (col 7)
            harga_h = _cell(gij_col, i, 1)   # H (col 8)
            # Centang per market col (O=15, P=16, ..., Z=26). Dict col_1based->"TRUE"/"".
            centang_by_col = {}
            if i < len(oz_col):
                oz_row = oz_col[i]
                for c_off in range(MARKET_COL_END - MARKET_COL_START + 1):
                    val = str(oz_row[c_off]).strip() if c_off < len(oz_row) else ""
                    centang_by_col[MARKET_COL_START + c_off] = val
            rows.append({
                "kode":           _cell(kode_col, i, 0),
                "harga_by_col":   {7: harga_g, 8: harga_h},
                "gambar":         _cell(gij_col, i, 2),
                "title":          _cell(gij_col, i, 3),
                "trigger_aj":     _cell(aj_col,  i, 0),
                "catatan":        _cell(k_col,   i, 0),
                "centang_by_col": centang_by_col,
            })

        result[name] = {"markets": markets, "rows": rows}
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
# smart_wait di-move ke create/_shared.py (alias import di atas).


# ===================== IMAGE SCRAPERS =====================
# scrape_imgur / scrape_postimg / scrape_gdrive di-move ke create/_shared.py
# (Phase 1). Di-re-import di atas file ini supaya callsite tetap stabil.


# download_images / download_images_with_urls / cleanup_temp_images /
# ai_map_fields / ai_map_fields_g2g / ai_map_fields_combined /
# extract_image_urls_for_g2g di-move ke create/_shared.py (Phase 2).
# Di-re-import di atas file ini; runtime deps (log, temp_dir, gemini_model)
# di-inject di _bind_ctx() via _create_shared.inject_runtime(...).


# ===================== GM FORM SCRAPER + CREATE LISTING =====================
# Di-move ke create/GM.py. Entry: scrape_form_options, create_listing.
# Di-alias ke nama lama via import di head file.


# ===================== G2G FORM SCRAPER + CREATE LISTING =====================
# Di-move ke create/G2G.py. Entry: scrape_form_options, create_listing,
# cache_looks_bogus. Di-alias ke nama lama via import di head file.


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


# ===================== PROSES PER BARIS (DUAL-MARKET GM + G2G) =====================
def _parse_cache_cell(raw, sentinel):
    """Parse form options cache cell. Return dict | {} (sentinel) | None."""
    if not raw:
        return None
    stripped = raw.strip()
    if stripped.startswith(sentinel):
        return {}
    try:
        return json.loads(stripped)
    except Exception:
        return None


# _g2g_cache_looks_bogus di-move ke create/G2G.py (alias import di head file).




def _ensure_form_options_cache(sheet, code, game_name, cache_col, initial_cache):
    """Return form_options_cache untuk market `code`.
    Urutan: initial_cache -> memcache -> re-read sheet cell -> fresh scrape.
    Side effect: kalau scrape baru, tulis ke row 45 col `cache_col`.
    Return: dict (fields) / {} (sentinel) / None (scrape fail).
    """
    mod = _get_market_module(code)
    if mod is None:
        add_log(f"[{code}] Module create.{code} tidak bisa di-import, skip market")
        return None

    sentinel = getattr(mod, "CACHE_SENTINEL", NO_OPTIONS_SENTINEL)
    cache_row = BARIS_FORM_OPTIONS
    memcache = _get_market_memcache(code)
    scrape_lk = _get_market_scrape_lock(code, sheet.title)

    # Auto-invalidasi cache bogus (tiap modul boleh punya aturan sendiri).
    bogus_fn = getattr(mod, "cache_looks_bogus", None)
    if callable(bogus_fn):
        try:
            if bogus_fn(initial_cache):
                add_log(f"[{code}] Cache lama terdeteksi bogus -> invalidate & re-scrape")
                initial_cache = None
        except Exception:
            pass

    if initial_cache is not None:
        return initial_cache

    with scrape_lk:
        # Re-read cell DULU. Tujuan: kalau user clear cell row 45, kita invalidate
        # memcache supaya force re-scrape (bukan pakai {} stale dari run sebelumnya).
        fresh_stripped = None
        try:
            fresh_raw = call_with_timeout(
                lambda: sheet.cell(cache_row, cache_col).value,
                timeout=20, name=f"reread cache {code}"
            ) or ""
            fresh_stripped = fresh_raw.strip()
        except Exception:
            fresh_stripped = None  # re-read gagal -> fall through ke memcache

        if fresh_stripped == "":
            # Cell di-clear user -> invalidate memcache, force fresh scrape.
            if sheet.title in memcache:
                add_log(f"[{code}] Cell row 45 kosong -> invalidate memcache, re-scrape")
                memcache.pop(sheet.title, None)
        elif fresh_stripped is not None:
            if fresh_stripped.startswith(sentinel):
                memcache[sheet.title] = {}
                add_log(f"[{code}] Sentinel re-read: game tanpa form, skip AI")
                return {}
            try:
                parsed = json.loads(fresh_stripped)
                memcache[sheet.title] = parsed
                add_log(f"[{code}] Form options re-read: {len(parsed)} field")
                return parsed
            except Exception:
                pass

        # Fallback: cell error/parse gagal tapi memcache valid -> pakai memcache.
        if sheet.title in memcache:
            cache = memcache[sheet.title]
            if cache == {}:
                add_log(f"[{code}] Pakai memcache: sentinel (game tanpa form)")
            else:
                add_log(f"[{code}] Pakai memcache form options: {len(cache)} field")
            return cache

        # Fresh scrape
        scraper_fn = getattr(mod, "scrape_form_options", None)
        if not callable(scraper_fn):
            add_log(f"[{code}] create.{code}.scrape_form_options tidak ada, skip")
            return None
        scraped = scraper_fn(game_name)
        if scraped is None:
            add_log(f"[{code}] Scrape form gagal, retry next cycle")
            return None
        if scraped == {}:
            memcache[sheet.title] = {}
            sentinel_val = f"{sentinel} {time.strftime('%Y-%m-%d %H:%M:%S')}"
            try:
                with_sheet_lock(
                    sheet_write_lock,
                    lambda: safe_update_cell(sheet, cache_row, cache_col, sentinel_val,
                                             timeout=45, desc=f"sentinel {code}"),
                    lock_timeout=60, desc=f"sentinel {code}"
                )
                add_log(f"[{code}] Ditandai sentinel '{sentinel_val}'")
            except Exception as e:
                add_log(f"[{code}] Gagal tulis sentinel: {e}")
            return {}

        memcache[sheet.title] = scraped
        try:
            json_str = json.dumps(scraped, ensure_ascii=False)
            with_sheet_lock(
                sheet_write_lock,
                lambda: safe_update_cell(sheet, cache_row, cache_col, json_str,
                                         timeout=45, desc=f"cache {code}"),
                lock_timeout=60, desc=f"cache {code}"
            )
            add_log(f"[{code}] Form options di-cache: {len(scraped)} field")
        except Exception as e:
            add_log(f"[{code}] Gagal tulis cache: {e}")
        return scraped


def _run_market(code, sheet, baris_nomor, worker_id, *, game_name, deskripsi,
                title, harga, field_mapping, image_paths, image_urls,
                raw_image_url, is_imgur, image_future=None):
    """Generic market runner: dispatch ke create.{CODE}.run(...). Return
    (ok: bool, k_line: str).

    `image_future` (optional) = concurrent.futures.Future yg resolve ke
    (paths, urls, is_imgur) saat download gambar selesai. Adapter yg support
    async pattern akan `.result()` future ini tepat sebelum step upload.
    Adapter lama yg belum support akan fallback ke `image_paths`/`image_urls`
    kwarg lama (backwards compat)."""
    mod = _get_market_module(code)
    if mod is None or not callable(getattr(mod, "run", None)):
        return False, f"❌ {code} | module create.{code}.run tidak ada"
    try:
        return mod.run(
            sheet, baris_nomor, worker_id,
            game_name=game_name, description=deskripsi, title=title, harga=harga,
            field_mapping=field_mapping or {},
            image_paths=image_paths, image_urls=image_urls,
            raw_image_url=raw_image_url, is_imgur=is_imgur,
            image_future=image_future,
        )
    except Exception as e:
        return False, f"❌ {code} | {str(e)[:80]}"


def proses_baris_dual(sheet, row_dict, baris_nomor, sheet_config, worker_id=1):
    """Proses 1 row dengan N-market paralel sub-threads (dynamic dispatch).

    row_dict     : {kode, title, gambar, catatan, harga_by_col: {col:str}}
    sheet_config : {markets_todo: [{"code","col","game","deskripsi","cache_parsed","harga"}, ...]}
                   markets_todo hanya berisi market yang (a) ada di row 48,
                   (b) punya game name + harga, (c) belum done di K column.
    """
    _worker_local.worker_id = worker_id

    title        = row_dict.get("title", "")
    kode_listing = row_dict.get("kode", "")
    gambar_url   = row_dict.get("gambar", "")
    existing_k   = row_dict.get("catatan", "")

    markets_todo = sheet_config.get("markets_todo", []) or []

    # Register worker_status untuk GUI
    try:
        sheet_gid = sheet.id
    except Exception:
        sheet_gid = 0
    sheet_url = (f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
                 f"/edit#gid={sheet_gid}&range=A{baris_nomor}")
    current_row_info["val"] = {"sheet": sheet.title, "row": baris_nomor, "gid": sheet_gid}
    with worker_status_lock:
        worker_status[worker_id] = {
            "sheet": sheet.title, "row": baris_nomor, "gid": sheet_gid,
            "text": f"{sheet.title} | row{baris_nomor}",
            "url": sheet_url,
        }

    def _clear_worker():
        current_row_info["val"] = None
        with worker_status_lock:
            worker_status.pop(worker_id, None)

    if not markets_todo:
        add_log(f"Row {baris_nomor}: tidak ada market yang perlu di-post")
        _clear_worker()
        return

    markets_tag = ",".join(m["code"] for m in markets_todo)
    add_log(f"Row {baris_nomor} [{markets_tag}] | Title: {title[:50]}...")

    # Validasi shared (kode in title, title length)
    if not kode_listing or kode_listing not in title:
        add_log(f"Kode '{kode_listing}' tidak ada di title - skip {baris_nomor}")
        try:
            with_sheet_lock(
                sheet_write_lock,
                lambda: safe_update_cell(sheet, baris_nomor, KOLOM_CATATAN,
                                         "❌ Code listingan tidak ada di Title",
                                         timeout=45, desc=f"no-code {baris_nomor}"),
                lock_timeout=60, desc="no-code"
            )
        except Exception as e:
            add_log(f"Gagal tulis catatan no-code: {e}")
        _clear_worker()
        return

    if len(title) > 150:
        add_log(f"Title {len(title)} char (>150) - skip {baris_nomor}")
        try:
            with_sheet_lock(
                sheet_write_lock,
                lambda: safe_update_cell(sheet, baris_nomor, KOLOM_CATATAN,
                                         "❌ Title terlalu panjang lebih dari 150 Character",
                                         timeout=45, desc=f"title-too-long {baris_nomor}"),
                lock_timeout=60, desc="title-too-long"
            )
        except Exception as e:
            add_log(f"Gagal tulis catatan: {e}")
        _clear_worker()
        return

    # ON WORKING ke K (tag markets yang sedang di-post ronde ini; preserve baris
    # done existing supaya ga ke-clear).
    already_done_lines = _extract_done_lines_from_k(existing_k)
    on_working_line = f"ON WORKING {markets_tag} !!!"
    k_working = ("\n".join([*already_done_lines, on_working_line])).strip()
    try:
        with_sheet_lock(
            sheet_write_lock,
            lambda: safe_update_cell(sheet, baris_nomor, KOLOM_CATATAN, k_working,
                                     timeout=45, desc=f"ON WORKING {baris_nomor}"),
            lock_timeout=60, desc="ON WORKING"
        )
    except Exception as e:
        add_log(f"Gagal tulis ON WORKING: {e}")

    # Image prep - ASYNC: download jalan di background thread, return Future.
    # Market workers bisa start langsung (navigate + fill form) TANPA nunggu
    # gambar selesai. Tepat di step upload gambar, adapter panggil
    # `resolve_image_future()` yang block sampai download selesai (atau
    # throw kalau gagal). Savings: ~20-40s per row kalau download lama.
    # Max gambar dihitung dari market teraktif di batch (PA=1, ELDO=5,
    # G2G/ZEUS=10, GM=20) supaya ndak waste bandwidth.
    image_future = None
    if gambar_url:
        max_images_needed = 20
        try:
            per_market = []
            for e in markets_todo:
                mod = _get_market_module(e["code"])
                per_market.append(getattr(mod, "MAX_IMAGES", 20) if mod else 20)
            if per_market:
                max_images_needed = max(per_market)
        except Exception:
            pass
        tags = ",".join(e["code"] for e in markets_todo)
        add_log(f"[IMG] Async download START (max={max_images_needed}, market={tags}): {gambar_url}")
        image_future = start_image_download_async(
            gambar_url, max_images=max_images_needed,
            name=f"img-dl-{worker_id}",
        )

    # Cache loading per market - paralel (ensure_form_options_cache handle
    # internal lock + memcache). Hasil di-attach ke entry market.
    cache_threads = []

    def _load_cache(entry):
        try:
            entry["form_options"] = _ensure_form_options_cache(
                sheet, entry["code"], entry["game"],
                entry["col"], entry.get("cache_parsed")
            )
        except Exception as e:
            entry["form_options"] = None
            add_log(f"[{entry['code']}] cache load error: {str(e)[:100]}")

    for idx, entry in enumerate(markets_todo):
        if idx > 0:
            time.sleep(random.uniform(1.5, 2.5))   # stagger biar CDP tidak rebutan
        t = threading.Thread(target=_load_cache, args=(entry,),
                             name=f"cache-{entry['code']}-{worker_id}", daemon=True)
        cache_threads.append(t); t.start()
    for t in cache_threads:
        t.join(timeout=420)  # max 7 menit scrape per market

    # AI mapping - 1 combined call untuk semua market yang punya form aktif.
    ai_inputs = [{"code": e["code"], "game": e["game"],
                  "form_options": e.get("form_options")} for e in markets_todo]
    ai_failure_msg = None
    ai_results = {}
    if any(isinstance(x["form_options"], dict) and x["form_options"] for x in ai_inputs):
        try:
            ai_results = ai_map_fields_multi(title, ai_inputs)
        except TimeoutHangError:
            ai_failure_msg = "Gemini timeout (3x >120s)"
        except Exception as e:
            ai_failure_msg = f"Gemini error: {str(e)[:80]}"

    if ai_failure_msg:
        add_log(f"[AI] {ai_failure_msg} - ABORT row, retry cycle berikutnya")
        skip_k = f"❌ {ai_failure_msg} - retry cycle"
        # Preserve existing done lines biar retry ga ke-clear
        preserved = "\n".join([*already_done_lines, skip_k]).strip()
        try:
            with_sheet_lock(
                sheet_write_lock,
                lambda: safe_update_cell(sheet, baris_nomor, KOLOM_CATATAN,
                                         preserved, timeout=45,
                                         desc=f"ai-fail {baris_nomor}"),
                lock_timeout=60, desc="ai-fail"
            )
        except Exception as e:
            add_log(f"Gagal tulis catatan ai-fail: {e}")
        _clear_worker()
        return

    # Spawn 1 thread per market
    status_lines = {}         # {code: k_line}
    done_ok      = set()      # {code} yang sukses post -> checkmark TRUE ke col-nya
    status_lock  = threading.Lock()

    def _market_thread(entry):
        code = entry["code"]
        try:
            # image_paths/image_urls/is_imgur = None sekarang (akan di-resolve
            # oleh adapter via image_future tepat sebelum step upload). Pass
            # raw_image_url untuk URL prefix di description.
            ok, line = _run_market(
                code, sheet, baris_nomor, worker_id,
                game_name=entry["game"], deskripsi=entry["deskripsi"],
                title=title, harga=entry["harga"],
                field_mapping=ai_results.get(code, {}),
                image_paths=None,
                image_urls=None,
                raw_image_url=gambar_url, is_imgur=False,
                image_future=image_future,
            )
            with status_lock:
                status_lines[code] = line
                if ok:
                    done_ok.add(code)
            update_stats(code, ok)
            add_log(line)
        except Exception as e:
            err = str(e)[:100]
            add_log(f"[{code}] Crash: {err}")
            with status_lock:
                status_lines[code] = f"❌ {code} | {err}"
            update_stats(code, False)

    threads = []
    for idx, entry in enumerate(markets_todo):
        if idx > 0:
            time.sleep(random.uniform(2.0, 3.0))   # stagger CDP
        t = threading.Thread(target=_market_thread, args=(entry,),
                             name=f"{entry['code'].lower()}-{worker_id}", daemon=True)
        threads.append(t); t.start()

    for t in threads:
        t.join(timeout=900)  # max 15 menit per market
        if t.is_alive():
            add_log(f"{t.name} timeout 15 menit, thread masih jalan")
            if _ctx is not None:
                _ctx.zombies.track(t, "create")

    # Compose final K: preserve existing done lines + synthetic "sudah centang
    # sebelumnya" + hasil market ronde ini.
    final_lines = list(already_done_lines)
    skipped_centang = sheet_config.get("markets_skipped_centang", []) or []
    for m in skipped_centang:
        final_lines.append(f"✅ {m['code']} Sudah tercentang sebelumnya")
    for entry in markets_todo:
        line = status_lines.get(entry["code"])
        if line:
            final_lines.append(line)

    if final_lines:
        # Prefix berdasar jumlah market sukses vs gagal (cek emoji awal tiap baris).
        total = len(final_lines)
        fail_count = sum(1 for ln in final_lines if ln.lstrip().startswith("❌"))
        if fail_count == 0:
            prefix = "✅ All Good"
        elif fail_count == total:
            prefix = "❌ Gagal Total"
        else:
            prefix = "⚠️ Error Sebagian"
        k_val = "\n".join([prefix, ""] + final_lines).strip()
    else:
        k_val = k_working

    def _finalize_writes():
        # Centang dulu (O-Z TRUE) baru laporan K. Kalau K gagal di tengah,
        # centang tetap ada -> market ndak di-retry.
        for entry in markets_todo:
            if entry["code"] in done_ok:
                safe_update_cell(sheet, baris_nomor, entry["col"], "TRUE",
                                 timeout=45,
                                 desc=f"{entry['code']} TRUE {baris_nomor}")
        safe_update_cell(sheet, baris_nomor, KOLOM_CATATAN, k_val,
                         timeout=45, desc=f"K final {baris_nomor}")

    try:
        with_sheet_lock(sheet_write_lock, _finalize_writes,
                        lock_timeout=120, desc=f"finalize row {baris_nomor}")
    except Exception as e:
        add_log(f"Gagal tulis hasil akhir: {e}")

    # Cleanup temp gambar - ambil paths dari future kalau ready, else skip.
    if image_future is not None and image_future.done():
        try:
            paths_to_clean, _, _ = image_future.result(timeout=1)
            cleanup_temp_images(paths_to_clean or [])
        except Exception:
            pass
    _clear_worker()


def _extract_done_lines_from_k(k_text):
    """Return list baris K yang sudah "✅ CODE | ..." (done). Baris lain
    (status lama/ON WORKING/error) di-drop supaya ga menumpuk."""
    if not k_text:
        return []
    out = []
    for line in str(k_text).splitlines():
        if _DONE_LINE_RE.match(line):
            out.append(line)
    return out


# ===================== ENTRY POINT =====================
BOT_NAME = "create"


def run_one_cycle(ctx):
    """1 cycle: scan LINK -> ambil 1 row candidate -> spawn 1 worker yang
    menjalankan GM+G2G paralel di sub-threads. Return jumlah row diproses (0 atau 1).
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

    # Phase 0: scan LINK!A/D
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

    # Deep batch scan: header (O43:P45) + kode (A) + G:J (harga_g2g/harga_gm/img/title)
    # + AJ (trigger) + O:P (done flags)
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

    # Skip tab yang tidak punya market aktif di row 48
    sheets_to_scan = [n for n in sheet_names
                      if scan_data.get(n, {}).get("markets")]
    skipped = len(sheet_names) - len(sheets_to_scan)
    skip_note = f" (skip {skipped} tab tanpa market di row 48)" if skipped else ""
    add_log(f"Deep scan: {len(sheet_names)} tab aktif dalam {scan_dur:.1f}s{skip_note}")

    # Phase 1: find FIRST candidate (1 row per cycle) — cari row yang masih
    # punya market belum done (berdasar K column parse).
    candidate = None
    for sheet_name in sheets_to_scan:
        if not wait_if_paused("collect candidate"):
            break

        sdata = scan_data[sheet_name]
        rows = sdata["rows"]
        markets = sdata.get("markets", [])

        # Pre-parse cache per market (sekali per sheet) + lookup sentinel dari modul.
        for m in markets:
            mod = _get_market_module(m["code"])
            sentinel = getattr(mod, "CACHE_SENTINEL", NO_OPTIONS_SENTINEL) if mod else NO_OPTIONS_SENTINEL
            m["cache_parsed"] = _parse_cache_cell(m.get("cache_raw", ""), sentinel)

        sheet_obj = None
        # Bottom-up scan: kerjakan row paling bawah dulu. Biasanya row bawah =
        # data terbaru yg user input, jadi di-prioritaskan supaya listing baru
        # cepat masuk market (row atas yg stuck PERLU POST bisa nunggu).
        for row_idx, r in reversed(list(enumerate(rows))):
            if r.get("trigger_aj", "").strip().upper() != "PERLU POST":
                continue

            # Resolve market todo: ada game + ada harga + belum done di K + belum centang O-Z.
            done_codes = _parse_done_codes_from_k(r.get("catatan", ""))
            harga_by_col = r.get("harga_by_col", {}) or {}
            centang_by_col = r.get("centang_by_col", {}) or {}
            markets_todo = []
            markets_skipped_centang = []
            for m in markets:
                code_up = m["code"].upper()
                centang_val = str(centang_by_col.get(m["col"], "")).strip().upper()
                is_centang = (centang_val == "TRUE")
                if is_centang:
                    # Sudah di-centang user/prev run -> skip proses.
                    # Kalau K belum punya done line buat market ini, perlu synthetic line.
                    if code_up not in done_codes:
                        markets_skipped_centang.append({"code": m["code"], "col": m["col"]})
                    continue
                if code_up in done_codes:
                    continue
                if not m.get("game"):
                    continue
                mod = _get_market_module(m["code"])
                harga_col = getattr(mod, "HARGA_COL", KOLOM_HARGA) if mod else KOLOM_HARGA
                harga = harga_by_col.get(harga_col, "")
                if not harga:
                    continue
                markets_todo.append({
                    "code":         m["code"],
                    "col":          m["col"],
                    "game":         m["game"],
                    "deskripsi":    m["deskripsi"],
                    "cache_parsed": m.get("cache_parsed"),
                    "harga":        harga,
                })
            if not markets_todo:
                continue

            # Resolve worksheet object (lazy)
            if sheet_obj is None:
                try:
                    sheet_obj = call_with_timeout(
                        spreadsheet_client.worksheet, args=(sheet_name,),
                        timeout=30, name=f"worksheet({sheet_name})"
                    )
                except Exception as e:
                    add_log(f"Gagal resolve worksheet '{sheet_name}': {str(e)[:120]}. Skip.")
                    break

            baris_nomor = BARIS_MULAI + row_idx
            sheet_config = {
                "markets_todo": markets_todo,
                "markets_skipped_centang": markets_skipped_centang,
            }
            candidate = (sheet_obj, r, baris_nomor, sheet_config)
            break

        if candidate:
            break

    if not candidate:
        ctx.progress.set(BOT_NAME, {"phase": "idle", "current_sheet": None,
                                     "current_row": None})
        return 0

    # Phase 2: process 1 row dengan N-market paralel
    sheet_obj, row_dict, baris_nomor, sheet_config = candidate
    wid = 1
    market_tags = ",".join(m["code"] for m in sheet_config["markets_todo"])
    add_log(f"Dispatch worker untuk row {baris_nomor} di '{sheet_obj.title}' [{market_tags}]")

    def _row_runner():
        try:
            proses_baris_dual(sheet_obj, row_dict, baris_nomor, sheet_config, worker_id=wid)
        except Exception as e:
            _worker_local.worker_id = wid
            add_log(f"Crash tak terduga di proses_baris_dual: {str(e)[:150]}")
        finally:
            with worker_status_lock:
                worker_status.pop(wid, None)

    t = threading.Thread(target=_row_runner, daemon=True, name=f"create-row-{wid}")
    t.start()
    t.join(timeout=600)  # max 10 menit total per row (semua market paralel)
    if t.is_alive():
        add_log(f"{t.name} timeout 10 menit, thread masih jalan, lanjut scan...")
        if _ctx is not None:
            _ctx.zombies.track(t, "create")
        # Tulis K column supaya row ditandai timeout. cleanup_tabs() di
        # orchestrator setelah return akan tutup Chrome tab -> zombie thread
        # throw exception saat akses page & exit sendiri.
        try:
            safe_update_cell(
                sheet_obj, baris_nomor, KOLOM_CATATAN,
                "❌ Lebih dari batas timeout (10 menit) - batch di-clear",
                timeout=30, desc=f"timeout K {baris_nomor}",
            )
        except Exception as e:
            add_log(f"Gagal tulis K timeout: {e}")

    ctx.progress.set(BOT_NAME, {"phase": "idle", "current_sheet": None,
                                 "current_row": None})
    return 1


# ===================== (GUI + main() dipindah ke main.py) =====================
# create_main_window / main dihapus - module ini sekarang dipanggil oleh
# orchestrator lewat BOT_NAME="create" + run_one_cycle(ctx).
