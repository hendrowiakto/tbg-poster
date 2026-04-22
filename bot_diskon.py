"""bot_diskon.py - update harga diskon di 10 marketplace dari Google Sheets.

Module kontrak (dipanggil oleh main.py via orchestrator):
    BOT_NAME = "diskon"
    def run_one_cycle(ctx) -> int:
        # 1 cycle: scan LINK!E -> collect max DISKON_MAX_WORKER candidates ->
        # dispatch parallel worker threads (1 worker = 1 produk across N market).
        # Return: jumlah produk diproses (>0 kalau ada progress, 0 kalau idle).

Semua infrastruktur (log, stats, Chrome, Sheets, toggle, progress) diambil dari
ctx (lihat shared.py). File ini hanya berisi:
- Konstanta layout sheet (baris/kolom data produk, kolom harga AE/AF/AG).
- market_locks - 10 lock per-marketplace (cegah 2 tab market sama bersamaan).
- dead_markets - set market "Session expired" per batch (skip fast-fail).
- scan_all_sheets + get_active_sheet_names - scanner.
- 10 update_harga_* - selector, timing, error handling TIDAK diubah.
- router_update_harga + proses_produk + _proses_produk_body - router + threading.
- cek_logout - deteksi pattern marketplace kick user.
- Thin wrappers add_log / safe_update_cell / update_stats / set_processing /
  wait_if_paused / smart_wait / is_chrome_alive / open_chrome - delegasi ke
  ctx tanpa ubah callsite.
"""

import os
import sys
import time
import threading
import random
import concurrent.futures  # noqa: F401 - dipakai oleh helper paralel di preserved code

from google.oauth2.service_account import Credentials  # noqa: F401
from playwright.sync_api import sync_playwright

from shared import call_with_timeout, TimeoutHangError


if getattr(sys, 'frozen', False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ===================== KONSTANTA SHEET LAYOUT =====================
LINK_SHEET_NAME = "LINK"
LINK_COUNTER_COL = 5   # Kolom E di LINK -> counter COUNTIF "PERLU DISCOUNT" per tab
LINK_START_ROW   = 2   # data LINK mulai baris 2 (baris 1 = header)
BARIS_PLATFORM  = 48   # baris 48 -> identitas platform per kolom
BARIS_LINK      = 49   # baris 49 -> manage link per kolom
BARIS_MULAI     = 51   # data produk mulai baris 51

KOLOM_CENTANG_MULAI = 15   # Kolom O (index 14, 1-based 15)
KOLOM_CENTANG_AKHIR = 26   # Kolom Z (index 25, 1-based 26)
KOLOM_FLAG_AK       = 37   # Kolom AK (index 36, 1-based 37) - trigger PERLU DISCOUNT
KOLOM_CATATAN_AB    = 28   # Kolom AB (index 27, 1-based 28)
KOLOM_LAST_EDIT_AC  = 29   # Kolom AC (index 28, 1-based 29)
KOLOM_HARGA_IDR_AE  = 31   # Kolom AE (index 30, 1-based 31)
KOLOM_HARGA_USD_AF  = 32   # Kolom AF (index 31, 1-based 32)
KOLOM_HARGA_EUR_AG  = 33   # Kolom AG (index 32, 1-based 33)

SCAN_IDLE_WAIT = 600  # detik tunggu jika tidak ada PERLU DISCOUNT (10 menit)
# =======================================================

# ===================== CTX BINDING =====================
# Infrastruktur (log, stats, Chrome, Sheets, toggle, progress) datang dari ctx (shared.py).
# Module-level globals di bawah di-set oleh _bind_ctx() agar preserved marketplace
# code bisa dipanggil TANPA ubah signature.
_ctx                  = None
spreadsheet_client    = None
CHROME_CDP_URL        = None
CDP_URL               = None   # alias - dipakai langsung oleh preserved 10 update_harga_*
CHROME_DEBUG_PORT     = None
SPREADSHEET_ID        = ""     # di-set dari ctx.config, dipakai build URL sheet
MAX_WORKER            = 5      # di-set dari ctx.config "DISKON_MAX_WORKER"
current_sheet_label   = {"val": "-"}  # sheet yang sedang di-scan (untuk GUI)
force_start           = False  # flag untuk skip idle wait (di-set GUI via ctx)


def _bind_ctx(ctx):
    """Bind BotContext ke module globals. Dipanggil di awal run_one_cycle()."""
    global _ctx, spreadsheet_client, CHROME_CDP_URL, CDP_URL, CHROME_DEBUG_PORT, SPREADSHEET_ID, MAX_WORKER
    _ctx               = ctx
    spreadsheet_client = ctx.sheets.spreadsheet
    CHROME_CDP_URL     = ctx.chrome.cdp_url
    CDP_URL            = ctx.chrome.cdp_url   # preserved code refers to CDP_URL directly
    CHROME_DEBUG_PORT  = ctx.chrome.debug_port
    SPREADSHEET_ID     = ctx.config.get("SPREADSHEET_ID", "")
    try:
        raw_max = int(str(ctx.config.get("DISKON_MAX_WORKER", "5")).strip())
    except Exception:
        raw_max = 5
    MAX_WORKER = max(1, min(10, raw_max))


# Preserved `_proses_produk_body` dan dynamic picker membaca `bot_paused` sebagai
# plain bool (LOAD_GLOBAL - tidak lewat PEP 562). Simpan plain global, di-update
# dari background poll thread supaya toggle OFF mid-cycle tetap menunda worker.
bot_paused = False


def _pause_poller():
    """Sinkronisasi `bot_paused` dengan state toggle saat ini.

    Jalan di background thread selama _ctx.stop_event belum di-set. Jika toggle
    diskon OFF atau stop_event aktif -> set bot_paused=True sehingga worker yg
    lagi jalan nunggu di `while bot_paused: time.sleep(1)`.
    """
    global bot_paused
    while _ctx is not None and not _ctx.stop_event.is_set():
        try:
            paused = (
                _ctx.stop_event.is_set()
                or not _ctx.toggles.should_keep_running(BOT_NAME)
            )
            bot_paused = paused
        except Exception:
            pass
        time.sleep(0.5)
    bot_paused = False


_pause_poller_thread = None


def _ensure_pause_poller():
    global _pause_poller_thread
    if _pause_poller_thread is not None and _pause_poller_thread.is_alive():
        return
    _pause_poller_thread = threading.Thread(
        target=_pause_poller, daemon=True, name="diskon-pause-poller"
    )
    _pause_poller_thread.start()


# ===================== PRESERVED LOCKS & STATE =====================
sheet_write_lock = threading.Lock()  # lock untuk tulis sheet dari N thread paralel
# Per-market lock - memastikan hanya 1 worker buka tab market yang sama di satu waktu.
# Beberapa market (PA, Z2U, ZEUS) bermasalah kalau 2 tab dibuka bersamaan di Chrome yang sama.
market_locks = {
    "GM":   threading.Lock(),
    "G2G":  threading.Lock(),
    "PA":   threading.Lock(),
    "ELDO": threading.Lock(),
    "Z2U":  threading.Lock(),
    "ZEUS": threading.Lock(),
    "U7":   threading.Lock(),
    "GB":   threading.Lock(),
    "IGV":  threading.Lock(),
    "FP":   threading.Lock(),
}
worker_status = {}  # diisi dinamis di run_one_cycle setiap batch
worker_status_lock = threading.Lock()  # lindungi worker_status dari race GUI vs worker
# Market yang terdeteksi "Session expired" di batch ini - worker lain auto-skip.
# Reset di awal tiap batch (di run_one_cycle sebelum spawn threads).
dead_markets = set()
dead_markets_lock = threading.Lock()
SESSION_EXPIRED_MSG = "Session expired, perlu login ulang"
_worker_local = threading.local()  # thread-local untuk track worker_id di log


# ===================== THIN WRAPPERS =====================
def add_log(msg):
    """Delegate ke ctx.logger. Prefix [Worker N] kalau di dalam worker thread."""
    worker_id = getattr(_worker_local, 'worker_id', None)
    prefix = f"[Worker {worker_id}] " if worker_id else ""
    if _ctx is not None:
        _ctx.logger.log(BOT_NAME, f"{prefix}{msg}")
    else:
        try:
            print(f"[diskon] {prefix}{msg}")
        except Exception:
            pass


def update_stats(platform, success):
    """Delegate ke ctx.stats.update (all-time + today)."""
    if _ctx is None:
        return
    _ctx.stats.update(BOT_NAME, platform, success)


def wait_if_paused():
    """Block selama bot di-pause via toggle OFF; return segera kalau running."""
    if _ctx is None:
        return
    while (
        _ctx is not None
        and not _ctx.stop_event.is_set()
        and not _ctx.toggles.should_keep_running(BOT_NAME)
    ):
        time.sleep(1)


def is_chrome_alive():
    """Delegate ke ctx.chrome.is_alive()."""
    if _ctx is None:
        return False
    try:
        return _ctx.chrome.is_alive()
    except Exception:
        return False


def open_chrome():
    """Delegate ke ctx.chrome.ensure_alive() - launch Chrome kalau belum hidup."""
    if _ctx is None:
        return False
    try:
        _ctx.chrome.ensure_alive()
        return True
    except Exception as e:
        add_log(f"open_chrome gagal: {str(e)[:120]}")
        return False


BOT_NAME = "diskon"


# ===================== CHROME & SHEETS HELPERS =====================
def get_or_create_context(browser):
    """
    Ambil context pertama dari Chrome, atau buat baru kalau belum ada.
    Mencegah IndexError saat Chrome baru restart tanpa tab.
    """
    if browser.contexts:
        return browser.contexts[0]
    return browser.new_context()


def safe_update_cell(sheet, row, col, value, timeout=45, desc=""):
    """Thin wrapper - delegate ke ctx.sheets.safe_update_cell (dengan timeout keras)."""
    if _ctx is not None:
        return _ctx.sheets.safe_update_cell(sheet, row, col, value, timeout=timeout, desc=desc)
    return call_with_timeout(
        sheet.update_cell, args=(row, col, value),
        timeout=timeout, name=f"update_cell[{desc}]"
    )


def with_sheet_lock(lock, body, lock_timeout=60, desc=""):
    """
    Jalankan body() di bawah sheet_write_lock dengan timeout acquire.
    Kalau lock tidak bisa didapat dalam lock_timeout detik -> raise TimeoutHangError.
    Mencegah worker macet menunggu lock yang dipegang zombie thread.
    """
    acquired = lock.acquire(timeout=lock_timeout)
    if not acquired:
        raise TimeoutHangError(f"Gagal acquire sheet_write_lock dalam {lock_timeout}s ({desc})")
    try:
        return body()
    finally:
        lock.release()


# ===================== PAUSE-AWARE WAIT =====================
def smart_wait(page, min_ms, max_ms):
    """Random wait yang hormati pause toggle. Delegate block ke ctx.toggles."""
    wait_if_paused()
    page.wait_for_timeout(random.randint(min_ms, max_ms))


# ===================== SHEET SCANNER =====================
_prefetched_active_sheets = None  # di-set orchestrator dari shared prescan


def set_prefetched_active_sheets(names):
    """Inject hasil prescan LINK dari orchestrator. One-shot: dikonsumsi sekali."""
    global _prefetched_active_sheets
    _prefetched_active_sheets = list(names) if names is not None else None


def get_active_sheet_names():
    """Ambil nama tab dari LINK sheet, di-filter hanya yang counter kolom E > 0.

    Kolom A = nama tab. Kolom E = formula COUNTIF "PERLU DISCOUNT" per tab
    (di-set user di LINK sheet). Pre-filter ini hemat besar di kondisi idle:
    kalau semua counter 0 -> cukup 1 API call ringan per scan cycle, tidak perlu
    fase 1 scan kolom AK di semua tab.

    Return: list nama tab aktif (count > 0), urutan sesuai LINK sheet.
    """
    global _prefetched_active_sheets
    if _prefetched_active_sheets is not None:
        cached = _prefetched_active_sheets
        _prefetched_active_sheets = None
        if cached:
            add_log(f"LINK!E (prescan): {len(cached)} tab aktif")
        return cached
    try:
        response = spreadsheet_client.values_batch_get(
            ranges=[
                f"'{LINK_SHEET_NAME}'!A{LINK_START_ROW}:A",
                f"'{LINK_SHEET_NAME}'!E{LINK_START_ROW}:E",
            ]
        )
    except Exception as e:
        add_log(f"Gagal read LINK!A+E: {str(e)[:200]}")
        return []
    vranges = response.get("valueRanges", []) or []
    col_a = vranges[0].get("values", []) if len(vranges) >= 1 else []
    col_e = vranges[1].get("values", []) if len(vranges) >= 2 else []

    active = []
    total_pending = 0
    for i, row in enumerate(col_a):
        name = (row[0] if row else "").strip()
        if not name:
            continue
        count_str = ""
        if i < len(col_e) and col_e[i]:
            count_str = str(col_e[i][0]).strip()
        try:
            count = int(float(count_str)) if count_str else 0
        except (ValueError, TypeError):
            count = 0
        if count > 0:
            active.append(name)
            total_pending += count
    if active:
        add_log(f"LINK!E counter: {len(active)} tab aktif, total {total_pending} PERLU DISCOUNT")
    return active


def scan_all_sheets(n=2):
    """
    Scan 3-lapis untuk efisiensi bandwidth pada spreadsheet besar:

    Pre-filter - read LINK!A+E 1 batch. Kolom E = counter COUNTIF "PERLU DISCOUNT"
                 per tab. Skip tab dengan counter 0. Kalau semua 0 -> exit di sini
                 (1 API call saja di idle cycle).

    Fase 1 - batch_get kolom AK (flag) saja untuk tab aktif.
             Output: peta tab_index -> list(baris_index) yg PERLU DISCOUNT.

    Fase 2 - batch_get full data A:AK, hanya tab yang punya match di fase 1.

    Total API read idle: 1 (pre-filter). Ada kerjaan: pre + fase1 + fase2 = 3.

    Return: list of (worksheet, data, baris_index), max n row.
    """
    results = []
    sheet_names = get_active_sheet_names()
    if not sheet_names:
        return results

    def _escape(nm):
        # A1 notation: apostrof literal harus di-double ("don't" -> "don''t")
        return nm.replace(chr(39), chr(39) * 2)

    # -- Fase 1: scan kolom AK saja (flag) untuk tab aktif ----------------
    flag_ranges = [f"'{_escape(name)}'!AK{BARIS_MULAI}:AK" for name in sheet_names]
    try:
        response = spreadsheet_client.values_batch_get(ranges=flag_ranges)
    except Exception as e:
        add_log(f"Gagal fase 1 (scan flag AK): {str(e)[:200]}")
        current_sheet_label["val"] = "-"
        return results
    flag_vranges = response.get("valueRanges", []) or []

    # Kumpulkan hit: {sheet_names_index: [baris_index_0based, ...]}
    hits_per_tab = {}
    for idx, name in enumerate(sheet_names):
        if idx >= len(flag_vranges):
            continue
        col_data = flag_vranges[idx].get("values", []) or []
        # col_data shape: [["PERLU DISCOUNT"], [""], [""], ["PERLU DISCOUNT"], ...]
        # row_offset 0 == baris BARIS_MULAI (1-based) == index BARIS_MULAI-1 (0-based)
        for row_offset, cell_row in enumerate(col_data):
            flag = (cell_row[0] if cell_row else "").strip()
            if flag == "PERLU DISCOUNT":
                baris_index = (BARIS_MULAI - 1) + row_offset
                hits_per_tab.setdefault(idx, []).append(baris_index)

    if not hits_per_tab:
        current_sheet_label["val"] = "-"
        return results

    # Tabs yg perlu fetch full data - preserve urutan sesuai sheet_names
    tabs_to_fetch = [(idx, sheet_names[idx]) for idx in sorted(hits_per_tab.keys())]

    # -- Fase 2: batch_get full data A:AK hanya untuk tab yang match ------
    # Range diperlebar sampai AK supaya re-verify flag di fase 2 tetap bisa.
    data_ranges = [f"'{_escape(name)}'!A:AK" for (_, name) in tabs_to_fetch]
    try:
        response2 = spreadsheet_client.values_batch_get(ranges=data_ranges)
    except Exception as e:
        add_log(f"Gagal fase 2 (fetch data tab match): {str(e)[:200]}")
        current_sheet_label["val"] = "-"
        return results
    data_vranges = response2.get("valueRanges", []) or []

    # -- Build results: pair tab data + baris hit dari fase 1 -------------
    for fetch_idx, (tab_idx, name) in enumerate(tabs_to_fetch):
        if len(results) >= n:
            break
        if fetch_idx >= len(data_vranges):
            continue
        data = data_vranges[fetch_idx].get("values", []) or []
        if not data:
            continue
        current_sheet_label["val"] = name
        try:
            sheet = spreadsheet_client.worksheet(name)  # cached metadata, no API call
        except Exception as e:
            add_log(f"Gagal resolve worksheet '{name}': {str(e)[:120]}. Skip tab ini.")
            continue
        for baris_index in hits_per_tab[tab_idx]:
            if len(results) >= n:
                break
            # Defensive: re-verify flag di data fase 2 - catch race kalau user
            # ubah sheet antara fase 1 & fase 2.
            if baris_index >= len(data):
                continue
            row = data[baris_index]
            flag = row[KOLOM_FLAG_AK - 1].strip() if len(row) >= KOLOM_FLAG_AK else ""
            if flag != "PERLU DISCOUNT":
                continue
            add_log(f"Ketemu PERLU DISCOUNT: sheet='{name}' baris={baris_index + 1}")
            results.append((sheet, data, baris_index))

    current_sheet_label["val"] = "-"
    return results


# ===================== UPDATE HARGA PER PLATFORM =====================

def update_harga_gm(kode_listing, harga, manage_link):
    """GameMarket - USD"""
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        context = get_or_create_context(browser)
        page = context.new_page()
        try:
            add_log(f"[GM] Membuka halaman: {manage_link}")
            page.goto(manage_link, wait_until="networkidle", timeout=30000)
            smart_wait(page, 4000, 6000)

            add_log(f"[GM] Klik kolom search...")
            search_input = page.locator("input[placeholder='Search game..']").first
            search_input.wait_for(state="visible", timeout=10000)
            search_input.click()
            smart_wait(page, 1000, 2000)

            add_log(f"[GM] Paste kode listing: {kode_listing}...")
            search_input.fill(kode_listing)
            smart_wait(page, 3000, 5000)

            add_log("[GM] Klik tombol edit harga...")
            # XPath: cari label 'Price', naik ke parent, lalu cari div cursor-pointer di dalamnya
            edit_btn = page.locator("xpath=//label[normalize-space()='Price']/parent::div//div[contains(@class,'cursor-pointer')]").first
            edit_btn.wait_for(state="visible", timeout=10000)
            edit_btn.click()
            smart_wait(page, 1000, 2000)

            add_log(f"[GM] Isi harga baru: {harga}...")
            price_input = page.locator("input[name='value'][type='number']").first
            price_input.wait_for(state="visible", timeout=10000)
            old_price = price_input.input_value()
            time.sleep(1)
            price_input.fill(str(harga))
            smart_wait(page, 1000, 2000)

            add_log("[GM] Klik tombol Save...")
            save_btn = page.locator("button.color-dark-gradient:has-text('Save')").first
            save_btn.wait_for(state="visible", timeout=10000)
            save_btn.click()
            smart_wait(page, 3000, 6000)

            add_log(f"[GM] Harga {kode_listing} berhasil diupdate ke {harga}!")
            return True, None, old_price

        except Exception as e:
            pesan_error = str(e)
            if cek_logout(page, "GM"):
                indo_error = "Session expired, perlu login ulang"
            elif "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[GM] Gagal: {indo_error}")
            return False, indo_error, ""

        finally:
            try:
                page.close()
            except Exception:
                pass


def update_harga_g2g(kode_listing, harga, manage_link):
    """G2G - IDR"""
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        context = get_or_create_context(browser)
        page = context.new_page()
        try:
            add_log(f"[G2G] Membuka halaman: {manage_link}")
            page.goto(manage_link, wait_until="networkidle", timeout=30000)
            smart_wait(page, 4000, 6000)

            add_log(f"[G2G] Search kode listing: {kode_listing}...")
            search_input = page.locator("input.q-field__native[placeholder='Cari judul atau nomor produk']").first
            search_input.wait_for(state="visible", timeout=10000)
            search_input.click()
            smart_wait(page, 1000, 2000)
            search_input.fill(kode_listing)
            search_input.press("Enter")
            smart_wait(page, 3000, 5000)

            add_log("[G2G] Klik Harga Satuan...")
            # XPath: cari td yang isinya "IDR" (Mata uang), lalu ambil td berikutnya (Harga satuan)
            # Ini menghindari salah klik Stok yang juga pakai span dengan class yang sama
            price_span = page.locator(
                "xpath=//td[normalize-space(.)='IDR']/following-sibling::td[1]"
                "//span[contains(@class,'base-hyperlink') and contains(@class,'cursor-pointer')]"
            ).first
            price_span.wait_for(state="visible", timeout=10000)
            price_span.click()
            smart_wait(page, 1000, 2000)

            # Bersihkan harga: hapus "Rp", koma, titik, spasi -> pure angka (khusus G2G)
            harga_bersih = ''.join(filter(str.isdigit, str(harga)))
            add_log(f"[G2G] Tunggu popup lalu isi harga baru: {harga_bersih}...")
            # Tunggu dialog Quasar muncul - scope ke .q-dialog agar tidak salah klik search input
            page.wait_for_selector(".q-dialog", state="visible", timeout=10000)
            price_input = page.locator(".q-dialog input.q-field__native").first
            price_input.wait_for(state="visible", timeout=10000)
            old_price_raw = price_input.input_value()
            # Format IDR: field berisi angka mentah (6076000) -> Rp 6,076,000
            try:
                old_price = f"Rp {int(old_price_raw.strip()):,}" if old_price_raw.strip().isdigit() else old_price_raw
            except Exception:
                old_price = old_price_raw
            time.sleep(1)
            price_input.click()
            page.wait_for_timeout(1000)   # delay 1 detik setelah klik sebelum isi harga
            price_input.fill(harga_bersih)
            smart_wait(page, 1000, 2000)

            add_log("[G2G] Klik tombol Perbarui...")
            perbarui_btn = page.locator(".q-dialog button.bg-primary").filter(has_text="Perbarui").first
            perbarui_btn.wait_for(state="visible", timeout=10000)
            perbarui_btn.click()
            smart_wait(page, 2000, 5000)

            add_log(f"[G2G] Harga {kode_listing} berhasil diupdate ke {harga}!")
            return True, None, old_price

        except Exception as e:
            pesan_error = str(e)
            if cek_logout(page, "G2G"):
                indo_error = "Session expired, perlu login ulang"
            elif "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[G2G] Gagal: {indo_error}")
            return False, indo_error, ""

        finally:
            try:
                page.close()
            except Exception:
                pass


def update_harga_pa(kode_listing, harga, manage_link):
    """PlayerAuctions - USD"""
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        context = get_or_create_context(browser)
        page = context.new_page()
        try:
            add_log(f"[PA] Membuka halaman: {manage_link}")
            page.goto(manage_link, wait_until="networkidle", timeout=30000)
            smart_wait(page, 4000, 8000)

            add_log(f"[PA] Search kode listing: {kode_listing}...")
            search_input = page.locator("input#keywords[placeholder='Search...']").first
            search_input.wait_for(state="visible", timeout=10000)
            search_input.click()
            smart_wait(page, 1000, 2000)
            search_input.fill(kode_listing)
            search_input.press("Enter")
            smart_wait(page, 3000, 5000)

            add_log("[PA] Klik tombol Edit (membuka tab baru)...")
            with context.expect_page() as new_page_info:
                edit_btn = page.locator("a[nztype='primary']:has-text('Edit')").first
                edit_btn.wait_for(state="visible", timeout=10000)
                edit_btn.click()
            new_page = new_page_info.value
            new_page.wait_for_load_state("networkidle", timeout=30000)
            smart_wait(new_page, 3000, 5000)

            add_log(f"[PA] Isi harga baru: {harga}...")
            price_input = new_page.locator("input.ant-input-number-input").first
            price_input.wait_for(state="visible", timeout=10000)
            old_price = price_input.input_value()
            time.sleep(1)
            price_input.fill(str(harga))
            smart_wait(new_page, 1000, 2000)

            add_log("[PA] Copy & paste login name...")
            login_input = new_page.locator("input#loginName").first
            login_input.wait_for(state="visible", timeout=10000)
            login_value = login_input.input_value()
            new_page.wait_for_timeout(1000)
            retype_input = new_page.locator("input#retypeLoginName").first
            retype_input.wait_for(state="visible", timeout=10000)
            retype_input.fill(login_value)
            smart_wait(new_page, 1000, 2000)

            add_log("[PA] Centang checkbox konfirmasi...")
            checkbox = new_page.locator("input.ant-checkbox-input").first
            checkbox.wait_for(state="visible", timeout=10000)
            checkbox.check()
            smart_wait(new_page, 1000, 2000)

            add_log("[PA] Klik UPDATE MY OFFER...")
            submit_btn = new_page.locator("button[type='submit']:has-text('UPDATE MY OFFER')").first
            submit_btn.wait_for(state="visible", timeout=10000)
            submit_btn.click()
            smart_wait(new_page, 3000, 6000)
            new_page.close()

            add_log(f"[PA] Harga {kode_listing} berhasil diupdate ke {harga}!")
            return True, None, old_price

        except Exception as e:
            pesan_error = str(e)
            if cek_logout(page, "PA"):
                indo_error = "Session expired, perlu login ulang"
            elif "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[PA] Gagal: {indo_error}")
            return False, indo_error, ""

        finally:
            try:
                page.close()
            except Exception:
                pass


def update_harga_eldo(kode_listing, harga, manage_link):
    """Eldorado - USD"""
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        context = get_or_create_context(browser)
        page = context.new_page()
        try:
            add_log(f"[ELDO] Membuka halaman: {manage_link}")
            page.goto(manage_link, wait_until="networkidle", timeout=30000)
            smart_wait(page, 4000, 6000)

            add_log(f"[ELDO] Search kode listing: {kode_listing}...")
            search_input = page.locator("input[placeholder='Search offers']").first
            search_input.wait_for(state="visible", timeout=10000)
            search_input.click()
            smart_wait(page, 1000, 2000)
            search_input.fill(kode_listing)
            page.wait_for_timeout(1000)
            search_input.press("Enter")
            smart_wait(page, 3000, 5000)

            add_log(f"[ELDO] Isi harga baru: {harga}...")
            price_input = page.locator("input.input[inputmode='decimal']").first
            price_input.wait_for(state="visible", timeout=10000)
            old_price = price_input.input_value()
            time.sleep(1)
            price_input.fill(str(harga))
            smart_wait(page, 1000, 2000)

            add_log("[ELDO] Klik tombol Confirm harga...")
            confirm_btn = page.locator("div[role='button'][aria-label='Confirm price']").first
            confirm_btn.wait_for(state="visible", timeout=10000)
            confirm_btn.click()
            smart_wait(page, 3000, 6000)

            add_log(f"[ELDO] Harga {kode_listing} berhasil diupdate ke {harga}!")
            return True, None, old_price

        except Exception as e:
            pesan_error = str(e)
            if cek_logout(page, "ELDO"):
                indo_error = "Session expired, perlu login ulang"
            elif "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[ELDO] Gagal: {indo_error}")
            return False, indo_error, ""

        finally:
            try:
                page.close()
            except Exception:
                pass


def update_harga_z2u(kode_listing, harga, manage_link):
    """Z2U - USD"""
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        context = get_or_create_context(browser)
        page = context.new_page()
        try:
            add_log(f"[Z2U] Membuka halaman: {manage_link}")
            page.goto(manage_link, wait_until="domcontentloaded", timeout=30000)
            smart_wait(page, 4000, 6000)

            add_log(f"[Z2U] Search kode listing: {kode_listing}...")
            search_input = page.locator("input.form-control.searchbox").first
            search_input.wait_for(state="visible", timeout=10000)
            search_input.click()
            smart_wait(page, 1000, 2000)
            search_input.fill(kode_listing)

            add_log("[Z2U] Klik tombol Search...")
            search_btn = page.locator("button.filter-search-button").first
            search_btn.wait_for(state="visible", timeout=10000)
            search_btn.click()
            smart_wait(page, 3000, 5000)

            add_log(f"[Z2U] Isi harga baru: {harga}...")
            price_input = page.locator("input.form-control.change_price").first
            price_input.wait_for(state="visible", timeout=10000)
            old_price = price_input.input_value()
            time.sleep(1)
            price_input.fill(str(harga))
            smart_wait(page, 1000, 2000)

            add_log("[Z2U] Klik tombol Confirm harga...")
            confirm_btn = page.locator("button.priceChange").first
            confirm_btn.wait_for(state="visible", timeout=10000)
            confirm_btn.click()
            smart_wait(page, 3000, 6000)

            add_log(f"[Z2U] Harga {kode_listing} berhasil diupdate ke {harga}!")
            return True, None, old_price

        except Exception as e:
            pesan_error = str(e)
            if cek_logout(page, "Z2U"):
                indo_error = "Session expired, perlu login ulang"
            elif "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[Z2U] Gagal: {indo_error}")
            return False, indo_error, ""

        finally:
            try:
                page.close()
            except Exception:
                pass


def update_harga_zeus(kode_listing, harga, manage_link):
    """ZeusX - USD"""
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        context = get_or_create_context(browser)
        page = context.new_page()
        try:
            add_log(f"[ZEUS] Membuka halaman: {manage_link}")
            page.goto(manage_link, wait_until="domcontentloaded", timeout=30000)
            smart_wait(page, 4000, 6000)

            add_log(f"[ZEUS] Search kode listing: {kode_listing}...")
            # ZeusX pakai React controlled input - fill() biasa tidak trigger event React
            # Pakai JS evaluate seperti di bot.py
            page.wait_for_function(
                "() => document.querySelector(\"input[placeholder='Search listing...']\") !== null",
                timeout=20000
            )
            page.evaluate("document.querySelector(\"input[placeholder='Search listing...']\").click()")
            smart_wait(page, 1000, 2000)
            kode_safe = kode_listing.replace("'", "\\'")
            page.evaluate(f"""
                var el = document.querySelector("input[placeholder='Search listing...']");
                var nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                nativeInputValueSetter.call(el, '{kode_safe}');
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            """)
            smart_wait(page, 1000, 2000)
            page.evaluate("""
                var el = document.querySelector("input[placeholder='Search listing...']");
                el.dispatchEvent(new KeyboardEvent('keydown',  { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
                el.dispatchEvent(new KeyboardEvent('keypress', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
                el.dispatchEvent(new KeyboardEvent('keyup',    { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
            """)
            smart_wait(page, 3000, 5000)

            add_log("[ZEUS] Klik tombol Edit Price...")
            price_btn = page.locator("div.my-listing-edit-price_edit-price__W7JTd").first
            price_btn.wait_for(state="visible", timeout=10000)
            price_btn.click()
            smart_wait(page, 2000, 3000)

            add_log(f"[ZEUS] Isi harga baru: {harga}...")
            price_input = page.locator("input[type='text']").last
            price_input.wait_for(state="visible", timeout=10000)
            old_price = price_input.input_value()
            time.sleep(1)
            price_input.fill(str(harga))
            smart_wait(page, 1000, 2000)

            add_log("[ZEUS] Klik tombol Update...")
            update_btn = page.locator("button.button_button-primary__kzMct").filter(has_text="Update").first
            update_btn.wait_for(state="visible", timeout=10000)
            update_btn.click()
            smart_wait(page, 3000, 6000)

            add_log(f"[ZEUS] Harga {kode_listing} berhasil diupdate ke {harga}!")
            return True, None, old_price

        except Exception as e:
            pesan_error = str(e)
            if cek_logout(page, "ZEUS"):
                indo_error = "Session expired, perlu login ulang"
            elif "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[ZEUS] Gagal: {indo_error}")
            return False, indo_error, ""

        finally:
            try:
                page.close()
            except Exception:
                pass


def update_harga_u7(kode_listing, harga, manage_link):
    """U7Buy - USD"""
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        context = get_or_create_context(browser)
        page = context.new_page()
        try:
            add_log(f"[U7] Membuka halaman: {manage_link}")
            page.goto(manage_link, wait_until="domcontentloaded", timeout=30000)
            smart_wait(page, 4000, 8000)

            add_log(f"[U7] Search kode listing: {kode_listing}...")
            search_input = page.locator("input.el-input__inner[placeholder='Offer Name']").first
            search_input.wait_for(state="visible", timeout=10000)
            search_input.click()
            smart_wait(page, 1000, 2000)
            search_input.fill(kode_listing)
            search_input.press("Enter")
            smart_wait(page, 3000, 5000)

            add_log("[U7] Klik tombol Edit...")
            edit_btn = page.locator("span.font-medium:has-text('Edit')").first
            edit_btn.wait_for(state="visible", timeout=10000)
            edit_btn.click()
            smart_wait(page, 2000, 3000)

            add_log("[U7] Kosongkan input ke-2 dari belakang (id 1209)...")
            # Input 1209 = second-to-last 'Please Enter' - harus dikosongkan agar bisa Submit
            clear_input = page.locator("input.el-input__inner[placeholder='Please Enter']").nth(-2)
            clear_input.wait_for(state="visible", timeout=10000)
            clear_input.click()
            clear_input.fill("")
            smart_wait(page, 500, 1000)

            add_log(f"[U7] Isi harga baru di input terakhir (id 1210): {harga}...")
            # Input 1210 = last 'Please Enter' - diisi harga
            price_input = page.locator("input.el-input__inner[placeholder='Please Enter']").last
            price_input.wait_for(state="visible", timeout=10000)
            old_price = price_input.input_value()
            time.sleep(1)
            price_input.click()
            price_input.fill(str(harga))
            smart_wait(page, 1000, 2000)

            add_log("[U7] Klik tombol Submit...")
            submit_btn = page.locator("aside.u7-button:has-text('Submit')").first
            submit_btn.wait_for(state="visible", timeout=10000)
            submit_btn.click()
            smart_wait(page, 3000, 6000)

            add_log(f"[U7] Harga {kode_listing} berhasil diupdate ke {harga}!")
            return True, None, old_price

        except Exception as e:
            pesan_error = str(e)
            if cek_logout(page, "U7"):
                indo_error = "Session expired, perlu login ulang"
            elif "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[U7] Gagal: {indo_error}")
            return False, indo_error, ""

        finally:
            try:
                page.close()
            except Exception:
                pass


def update_harga_gb(kode_listing, harga, manage_link):
    """GameBoost - EUR"""
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        context = get_or_create_context(browser)
        page = context.new_page()
        try:
            add_log(f"[GB] Membuka halaman: {manage_link}")
            page.goto(manage_link, wait_until="domcontentloaded", timeout=30000)
            smart_wait(page, 4000, 8000)

            add_log(f"[GB] Search kode listing: {kode_listing}...")
            search_input = page.locator("input[placeholder='Search...']").first
            search_input.wait_for(state="visible", timeout=10000)
            search_input.fill(kode_listing)
            search_input.press("Enter")
            smart_wait(page, 4000, 8000)

            add_log("[GB] Hover ke row lalu klik tombol titik tiga...")
            # Hover dulu ke row pertama agar action button muncul
            first_row = page.locator("tbody tr").first
            first_row.wait_for(state="visible", timeout=10000)
            first_row.hover()
            page.wait_for_timeout(800)
            # Cari tombol titik tiga DALAM row saja, bukan global (hindari tombol sidebar)
            more_btn = first_row.locator("button:has(i.fa-ellipsis-h), button:has(i.fa-regular.fa-ellipsis-h)").first
            more_btn.wait_for(state="visible", timeout=10000)
            more_btn.click()
            smart_wait(page, 1000, 2000)

            add_log("[GB] Klik Edit Account...")
            # Radix UI dropdown dirender di portal (body) - cari di [role='menu'] atau [role='menuitem']
            edit_item = page.locator("[role='menu'] [role='menuitem']:has-text('Edit Account'), [role='menuitem']:has-text('Edit Account')").first
            edit_item.wait_for(state="visible", timeout=10000)
            edit_item.click()
            smart_wait(page, 1000, 2000)

            add_log(f"[GB] Isi harga baru: {harga}...")
            price_input = page.locator("input.h-9[type='text']").first
            price_input.wait_for(state="visible", timeout=10000)
            old_price = price_input.input_value()
            time.sleep(1)
            price_input.fill(str(harga))
            smart_wait(page, 1000, 2000)

            add_log("[GB] Klik Continue (1)...")
            continue_btn = page.locator("button:has-text('Continue')").first
            continue_btn.wait_for(state="visible", timeout=10000)
            continue_btn.click()
            smart_wait(page, 1000, 2000)

            add_log("[GB] Klik Continue (2)...")
            continue_btn2 = page.locator("button:has-text('Continue')").first
            continue_btn2.wait_for(state="visible", timeout=10000)
            continue_btn2.click()
            smart_wait(page, 1000, 2000)

            add_log("[GB] Klik Save Changes...")
            save_btn = page.locator("button:has-text('Save Changes')").first
            save_btn.wait_for(state="visible", timeout=10000)
            save_btn.click()
            smart_wait(page, 3000, 6000)

            add_log(f"[GB] Harga {kode_listing} berhasil diupdate ke {harga}!")
            return True, None, old_price

        except Exception as e:
            pesan_error = str(e)
            if cek_logout(page, "GB"):
                indo_error = "Session expired, perlu login ulang"
            elif "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[GB] Gagal: {indo_error}")
            return False, indo_error, ""

        finally:
            try:
                page.close()
            except Exception:
                pass


def update_harga_igv(kode_listing, harga, manage_link):
    """IGV - USD"""
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        context = get_or_create_context(browser)
        page = context.new_page()
        try:
            add_log(f"[IGV] Membuka halaman: {manage_link}")
            page.goto(manage_link, wait_until="domcontentloaded", timeout=30000)
            smart_wait(page, 4000, 8000)

            add_log(f"[IGV] Search kode listing: {kode_listing}...")
            search_input = page.locator("input.el-input__inner[placeholder='Product Name/Product Code']").first
            search_input.wait_for(state="visible", timeout=10000)
            search_input.click()
            smart_wait(page, 1000, 2000)
            search_input.fill(kode_listing)

            add_log("[IGV] Klik tombol Search...")
            search_btn = page.locator("button.el-button.el-button--primary:has-text('Search')").first
            search_btn.wait_for(state="visible", timeout=10000)
            search_btn.click()
            smart_wait(page, 4000, 8000)   # website IGV lambat - wait lebih lama

            add_log("[IGV] Hover ke row lalu klik ikon pensil...")
            # Hover dulu agar icon pensil muncul, lalu JS click
            first_row = page.locator("tr.el-table__row").first
            first_row.wait_for(state="visible", timeout=20000)
            first_row.hover()
            page.wait_for_timeout(1500)  # tunggu icon muncul setelah hover
            page.evaluate("document.querySelector('img[name=\"my-products_quick-price-edit\"]').click()")
            # Tunggu input number muncul - ini spesifik ke dialog Update prices (bukan security dialog)
            page.wait_for_selector(".el-dialog input[type='number']", state="visible", timeout=15000)
            smart_wait(page, 1000, 1500)

            add_log(f"[IGV] Isi harga baru: {harga}...")
            harga_str = str(harga)
            # Scope ke dialog yg punya input[type='number'] - hindari security password dialog
            price_dialog = page.locator(".el-dialog").filter(has=page.locator("input[type='number']"))
            price_input = price_dialog.locator("input[type='number']").first
            price_input.wait_for(state="visible", timeout=10000)
            old_price = price_input.input_value()
            time.sleep(1)
            price_input.click(click_count=3)  # triple click = select all
            page.wait_for_timeout(500)
            price_input.press_sequentially(harga_str, delay=50)
            smart_wait(page, 1000, 2000)

            add_log("[IGV] Klik tombol Submit...")
            submit_btn = price_dialog.locator("button:has-text('Submit')").first
            submit_btn.wait_for(state="visible", timeout=10000)
            submit_btn.click()
            smart_wait(page, 3000, 6000)

            add_log(f"[IGV] Harga {kode_listing} berhasil diupdate ke {harga}!")
            return True, None, old_price

        except Exception as e:
            pesan_error = str(e)
            if cek_logout(page, "IGV"):
                indo_error = "Session expired, perlu login ulang"
            elif "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[IGV] Gagal: {indo_error}")
            return False, indo_error, ""

        finally:
            try:
                page.close()
            except Exception:
                pass


def update_harga_fp(kode_listing, harga, manage_link):
    """Funpay - USD"""
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        context = get_or_create_context(browser)
        page = context.new_page()
        try:
            add_log(f"[FP] Membuka halaman: {manage_link}")
            page.goto(manage_link, wait_until="networkidle", timeout=30000)
            smart_wait(page, 4000, 6000)

            add_log(f"[FP] Cari dan klik produk: {kode_listing}...")
            # Pakai .last agar klik elemen paling spesifik (innermost) yang mengandung teks
            product_link = page.locator(f"*:has-text('{kode_listing}')").last
            product_link.wait_for(state="visible", timeout=10000)
            product_link.scroll_into_view_if_needed()
            product_link.click()
            smart_wait(page, 3000, 5000)

            add_log("[FP] Klik Edit an offer...")
            edit_btn = page.locator("a.btn.btn-gray:has-text('Edit an offer')").first
            edit_btn.wait_for(state="visible", timeout=10000)
            edit_btn.click()
            smart_wait(page, 2000, 3000)

            add_log(f"[FP] Isi harga baru: {harga}...")
            price_input = page.locator("input[name='price']").first
            price_input.wait_for(state="visible", timeout=10000)
            old_price = price_input.input_value()
            time.sleep(1)
            price_input.fill(str(harga))
            smart_wait(page, 1000, 2000)

            add_log("[FP] Klik tombol Save...")
            save_btn = page.locator("button.js-btn-save").first
            save_btn.wait_for(state="visible", timeout=10000)
            save_btn.click()
            smart_wait(page, 3000, 6000)

            add_log(f"[FP] Harga {kode_listing} berhasil diupdate ke {harga}!")
            return True, None, old_price

        except Exception as e:
            pesan_error = str(e)
            if cek_logout(page, "FP"):
                indo_error = "Session expired, perlu login ulang"
            elif "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[FP] Gagal: {indo_error}")
            return False, indo_error, ""

        finally:
            try:
                page.close()
            except Exception:
                pass


# ===================== ROUTER PLATFORM =====================
def router_update_harga(platform, kode_listing, harga, manage_link):
    mapping = {
        "GM":   update_harga_gm,
        "G2G":  update_harga_g2g,
        "PA":   update_harga_pa,
        "ELDO": update_harga_eldo,
        "Z2U":  update_harga_z2u,
        "ZEUS": update_harga_zeus,
        "U7":   update_harga_u7,
        "GB":   update_harga_gb,
        "IGV":  update_harga_igv,
        "FP":   update_harga_fp,
    }
    fn = mapping.get(platform)
    if not fn:
        add_log(f"Platform '{platform}' tidak dikenali, skip.")
        return False, f"Platform '{platform}' tidak dikenali", ""
    return fn(kode_listing, harga, manage_link)


# ===================== PROSES 1 PRODUK =====================
def proses_produk(sheet, data, baris_index, worker_id=1):
    """Wrapper - jaga worker_status selalu di-reset walau body crash total."""
    try:
        _proses_produk_body(sheet, data, baris_index, worker_id=worker_id)
    except Exception as _e:
        add_log(f"Crash tak terduga di proses_produk: {str(_e)[:150]}")
    finally:
        # Selalu reset agar GUI tidak stuck nampilkan 'ON WORKING'
        try:
            with worker_status_lock:
                if worker_id in worker_status:
                    worker_status[worker_id] = {"text": "-", "url": ""}
        except Exception:
            pass


def _process_single_market(platform, kode_listing, harga, manage_link, kolom_huruf):
    """
    Panggil router + retry (maks 1x) + cek Chrome hidup.
    Return (berhasil, error, old_price). Tidak pegang market_lock - caller yg atur.
    """
    add_log(f"Proses kolom {kolom_huruf} | Platform: {platform} | Harga: {harga} | Link: {manage_link}")
    MAX_RETRIES = 1
    berhasil, error, old_price = False, "Belum dicoba", ""
    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            add_log(f"[{platform}] Retry {attempt}/{MAX_RETRIES}...")
        try:
            berhasil, error, old_price = router_update_harga(platform, kode_listing, harga, manage_link)
        except Exception as e:
            berhasil = False
            error = f"Exception: {str(e)[:100]}"
            old_price = ""
            add_log(f"[{platform}] Exception: {str(e)[:100]}")
        if berhasil:
            break
        # Gagal - cek kondisi Chrome
        if not is_chrome_alive():
            add_log(f"[{platform}] Chrome crash terdeteksi! Restart Chrome...")
            try:
                open_chrome()
            except Exception as _oc_err:
                add_log(f"open_chrome error: {_oc_err}")
            for _ in range(15):
                if is_chrome_alive():
                    break
                time.sleep(1)
            if is_chrome_alive():
                add_log(f"Chrome berhasil restart. Lanjut retry [{platform}]...")
            else:
                add_log(f"Chrome gagal restart. Hentikan retry [{platform}].")
                break
        else:
            if attempt < MAX_RETRIES:
                wait_sec = random.randint(3, 5)
                add_log(f"[{platform}] Gagal, tunggu {wait_sec}s sebelum retry...")
                time.sleep(wait_sec)
    return berhasil, error, old_price


def _proses_produk_body(sheet, data, baris_index, worker_id=1):
    _worker_local.worker_id = worker_id
    row         = data[baris_index]
    baris_nomor = baris_index + 1
    kode_listing = row[0].strip() if len(row) > 0 else ""

    if not kode_listing:
        add_log(f"Kode listing di kolom A baris {baris_nomor} kosong, skip.")
        return

    # Update status worker di GUI + build URL ke sheet
    try:
        sheet_name = sheet.title
        sheet_gid  = sheet.id
    except Exception:
        sheet_name = "?"
        sheet_gid  = 0
    sheet_url = (
        f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
        f"/edit#gid={sheet_gid}&range=AB{baris_nomor}"
    )
    # base status - tanpa market. Saat menunggu market, tampilkan "menunggu..."
    # sebagai placeholder (warna grey di GUI) supaya user bisa bedakan state
    # "sedang proses market X" vs "antri cari market kosong".
    base_status    = f"{sheet_name}  |  row {baris_nomor}  |  {kode_listing}"
    waiting_status = f"{base_status}  |  menunggu..."
    with worker_status_lock:
        worker_status[worker_id] = {
            "text": waiting_status, "url": sheet_url, "waiting": True,
        }

    # Tandai di sheet bahwa baris ini sedang dikerjakan
    try:
        with_sheet_lock(
            sheet_write_lock,
            lambda: safe_update_cell(sheet, baris_nomor, KOLOM_CATATAN_AB,
                                     f"ON WORKING {worker_id} !!!",
                                     timeout=45, desc=f"ON WORKING row {baris_nomor}"),
            lock_timeout=60, desc=f"ON WORKING row {baris_nomor}"
        )
    except Exception as e:
        add_log(f"Gagal tulis ON WORKING ke sheet: {e}")

    # Ambil harga per currency (kolom AE/AF/AG -> 1-based 31/32/33 -> 0-based 30/31/32)
    harga_idr = row[30].strip() if len(row) > 30 else ""  # AE - IDR
    harga_usd = row[31].strip() if len(row) > 31 else ""  # AF - USD
    harga_eur = row[32].strip() if len(row) > 32 else ""  # AG - EUR

    add_log(f"Proses baris {baris_nomor} | Kode: {kode_listing} | IDR={harga_idr} | USD={harga_usd} | EUR={harga_eur}")

    hasil_per_market = {}
    catatan_order    = []   # urutan key untuk catatan - disusun O->Z (konsisten walau proses dinamis)
    pending          = []   # task yang siap diproses (sudah tervalidasi)

    # -- Fase validasi: build pending[] + catatan_order -------------------
    for k in range(KOLOM_CENTANG_MULAI - 1, KOLOM_CENTANG_AKHIR):
        nilai = row[k].strip() if len(row) > k else ""
        if nilai.upper() != "TRUE":
            continue

        platform    = data[BARIS_PLATFORM - 1][k].strip().upper() if len(data[BARIS_PLATFORM - 1]) > k else ""
        manage_link = data[BARIS_LINK - 1][k].strip()              if len(data[BARIS_LINK - 1]) > k else ""
        kolom_huruf = chr(64 + k + 1)

        if not platform:
            add_log(f"Platform di kolom {kolom_huruf}48 kosong, skip.")
            hasil_per_market[kolom_huruf] = ("❌", "Platform baris 48 kosong", "", "")
            catatan_order.append(kolom_huruf)
            continue

        if not manage_link:
            add_log(f"Manage link di kolom {kolom_huruf}49 kosong, skip.")
            hasil_per_market[platform] = ("❌", "Manage link baris 49 kosong", "", "")
            catatan_order.append(platform)
            continue

        # Tentukan mata uang berdasarkan platform
        if platform == "G2G":
            harga = harga_idr
        elif platform == "GB":
            harga = harga_eur
        else:
            harga = harga_usd

        if not harga:
            add_log(f"[{platform}] Harga kosong di sheet, skip.")
            hasil_per_market[platform] = ("❌", "Harga kosong di sheet", "", "")
            catatan_order.append(platform)
            continue

        pending.append({
            "platform":    platform,
            "manage_link": manage_link,
            "harga":       harga,
            "kolom_huruf": kolom_huruf,
        })
        catatan_order.append(platform)

    # -- Fase eksekusi: dynamic market picker -----------------------------
    # Ambil task pertama yg market_lock-nya free. Kalau semua locked -> sleep 0.5s.
    # Maks nunggu 10 menit biar tidak stuck selamanya (defensive).
    max_wait_all_locked = 600
    wait_all_locked_start = None
    while pending:
        if bot_paused:
            add_log("Bot di-pause, menunda proses kolom berikutnya...")
            while bot_paused:
                time.sleep(1)
            add_log("Bot di-resume, melanjutkan proses...")

        processed_this_iter = False
        for idx, task in enumerate(pending):
            platform = task["platform"]

            # -- DEAD check: market sudah detected session expired di batch ini --
            # Skip buka browser, langsung tulis fail. Ini untuk hemat waktu -
            # kalau 1 worker sudah konfirmasi market X logout, 9 worker lain tidak
            # perlu buang waktu buka tab + deteksi logout juga. State reset tiap batch.
            with dead_markets_lock:
                is_dead = platform in dead_markets
            if is_dead:
                add_log(f"[{platform}] Skip: session expired "
                        f"sudah terdeteksi oleh worker lain di batch ini.")
                hasil_per_market[platform] = ("❌", SESSION_EXPIRED_MSG, "", "")
                update_stats(platform, False)
                pending.pop(idx)
                processed_this_iter = True
                wait_all_locked_start = None
                break  # restart scan dari task pertama

            lock = market_locks.get(platform)
            if lock is not None:
                got = lock.acquire(blocking=False)
                if not got:
                    continue  # market dipakai worker lain, coba task berikutnya
            # Update GUI status: tampilkan market yang sedang dikerjakan
            with worker_status_lock:
                if worker_id in worker_status:
                    worker_status[worker_id] = {
                        "text": f"{base_status}  |  {platform}",
                        "url": sheet_url,
                        "waiting": False,
                    }
            try:
                berhasil, error, old_price = _process_single_market(
                    task["platform"], kode_listing, task["harga"],
                    task["manage_link"], task["kolom_huruf"]
                )
            finally:
                if lock is not None:
                    try:
                        lock.release()
                    except Exception:
                        pass
                # Reset status ke "menunggu..." supaya GUI tidak "stuck"
                # menampilkan market yang baru selesai. Akan di-overwrite ke
                # "| {market}" begitu worker dapat lock market selanjutnya.
                with worker_status_lock:
                    if worker_id in worker_status:
                        worker_status[worker_id] = {
                            "text": waiting_status,
                            "url": sheet_url,
                            "waiting": True,
                        }
            if berhasil:
                hasil_per_market[platform] = ("✅", None, task["harga"], old_price)
            else:
                hasil_per_market[platform] = ("❌", error, task["harga"], "")
                # Kalau error khusus "Session expired" -> tandai DEAD supaya worker
                # lain skip market ini sampai batch selesai. Error lain (timeout,
                # element not found, dll) TIDAK trigger DEAD - hanya session expired.
                if error == SESSION_EXPIRED_MSG:
                    with dead_markets_lock:
                        if platform not in dead_markets:
                            dead_markets.add(platform)
                            add_log(f"[{platform}] Session expired terdeteksi. "
                                    f"Worker lain akan skip market ini di batch ini.")
            update_stats(platform, berhasil)
            pending.pop(idx)
            processed_this_iter = True
            wait_all_locked_start = None
            break  # restart scan dari task pertama

        if not processed_this_iter:
            # Semua pending sedang locked oleh worker lain - tunggu sebentar
            if wait_all_locked_start is None:
                wait_all_locked_start = time.time()
            elif time.time() - wait_all_locked_start > max_wait_all_locked:
                add_log(f"Semua market_lock macet > {max_wait_all_locked}s. "
                        f"Drop {len(pending)} task tersisa.")
                for task in pending:
                    plat = task["platform"]
                    hasil_per_market[plat] = ("❌", "Timeout antri market_lock", task["harga"], "")
                pending.clear()
                break
            time.sleep(0.5)

    # -- Tulis hasil ke sheet ---------------------------------------------
    last_edit = time.strftime("%d/%b/%Y %H:%M")  # format: 01/Jan/2026 09:18

    # Prefix berdasarkan jumlah market yang gagal
    total_market = len(hasil_per_market)
    jumlah_gagal = sum(1 for status, _, _, _ in hasil_per_market.values() if status == "❌")
    if total_market == 0:
        prefix = "⚠️ Tidak ada market dicentang"
    elif jumlah_gagal == 0:
        prefix = "✅ All Good"
    elif jumlah_gagal == total_market:
        prefix = "❌ Error (semua)"
    else:
        prefix = "❌ Error (sebagian)"

    catatan_lines = [prefix, ""]
    for key in catatan_order:
        if key not in hasil_per_market:
            continue
        status, error, harga_used, old_price = hasil_per_market[key]
        if error:
            catatan_lines.append(f"{status} {key}: {error}")
        else:
            harga_lama = old_price if old_price else "harga lama tidak di temukan"
            catatan_lines.append(f"{status} {key}: {harga_lama} > {harga_used}")
    catatan = "\n".join(catatan_lines)

    try:
        def _write_results():
            safe_update_cell(sheet, baris_nomor, KOLOM_CATATAN_AB, catatan,
                             timeout=45, desc=f"catatan row {baris_nomor}")
            safe_update_cell(sheet, baris_nomor, KOLOM_LAST_EDIT_AC, last_edit,
                             timeout=45, desc=f"last_edit row {baris_nomor}")
        with_sheet_lock(sheet_write_lock, _write_results,
                        lock_timeout=120, desc=f"write results row {baris_nomor}")
        add_log(f"Baris {baris_nomor} selesai. AB: {prefix} | AC: {last_edit}")
    except Exception as e:
        add_log(f"Gagal tulis hasil ke sheet baris {baris_nomor}: {e}")
    finally:
        with worker_status_lock:
            if worker_id in worker_status:
                worker_status[worker_id] = {"text": "-", "url": ""}


# ===================== LOGOUT DETECTION =====================
def cek_logout(page, platform):
    """Cek apakah halaman saat ini adalah halaman login (session expired)."""
    try:
        url = page.url.lower()
        if platform == "GM":
            return url.rstrip("/") == "https://gamemarket.gg"
        if platform == "G2G":
            return "seller/join" in url
        if platform == "FP":
            return False  # FP hampir tidak pernah logout
        return "login" in url
    except Exception:
        return False


# ===================== ENTRY POINT =====================
def run_one_cycle(ctx):
    """1 cycle: scan semua sheet -> collect hingga MAX_WORKER candidates ->
    dispatch worker threads paralel (1 worker = 1 produk) -> cleanup tab.

    Return: jumlah produk yang diproses (>0 kalau ada progress, 0 kalau idle).
    """
    _bind_ctx(ctx)
    _ensure_pause_poller()

    if ctx.stop_event.is_set():
        return 0

    # Ensure Chrome hidup
    if not is_chrome_alive():
        add_log("Chrome tidak terdeteksi, mencoba launch...")
        if not open_chrome():
            add_log(f"Chrome (port {CHROME_DEBUG_PORT}) tidak bisa launch. Skip cycle.")
            return 0

    current_max = MAX_WORKER

    # Init worker_status sesuai max_worker batch ini
    with worker_status_lock:
        worker_status.clear()
        for wid in range(1, current_max + 1):
            worker_status[wid] = {"text": "-", "url": ""}

    # Reset dead_markets - tiap cycle baru, semua market dicoba ulang
    with dead_markets_lock:
        dead_markets.clear()

    # Scan dengan timeout 2 menit - cegah hang gspread half-open TCP
    try:
        results = call_with_timeout(scan_all_sheets, args=(current_max,),
                                    timeout=120, name="scan_all_sheets")
    except TimeoutHangError:
        add_log("scan_all_sheets timeout 2 menit! Skip cycle.")
        return 0
    except Exception as e:
        add_log(f"scan_all_sheets error: {str(e)[:150]}")
        return 0

    if not results:
        current_sheet_label["val"] = "-"
        return 0

    # Spawn worker threads paralel - 1 worker klaim 1 row, proses semua market
    threads = [
        threading.Thread(
            target=proses_produk,
            args=(s, d, b),
            kwargs={"worker_id": i + 1},
            daemon=True
        )
        for i, (s, d, b) in enumerate(results)
    ]
    for i, t in enumerate(threads):
        t.start()
        if i < len(threads) - 1:
            time.sleep(random.randint(2, 3))  # stagger start 2-3 detik
    for t in threads:
        t.join(timeout=600)  # max 10 menit per worker
        if t.is_alive():
            add_log("Worker thread timeout 10 menit! Thread masih jalan, lanjut.")
            if _ctx is not None:
                _ctx.zombies.track(t, "diskon")

    # Cleanup tab dihandle di orchestrator (main.py) - hindari double-call yang bikin blink.
    return len(results)

