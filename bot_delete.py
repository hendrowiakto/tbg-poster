"""bot_delete.py - delete listings across 10 marketplaces.

Module kontrak (dipanggil oleh main.py via orchestrator):
    BOT_NAME = "delete"
    def run_one_cycle(ctx) -> int:
        # 1 cycle: scan LINK!C -> proses max 1 row. Return: 1 kalau ada row
        # diproses, 0 kalau idle / toggle OFF / stop_event set / Sheets error.

Semua infrastruktur (log, stats, Chrome, Sheets, toggle, progress) diambil dari
ctx (lihat shared.py). File ini hanya berisi:
- Konstanta layout sheet (baris/kolom trigger, platform, link).
- market_locks - 10 lock per-marketplace (cegah 2 tab market sama bersamaan).
- 10 fungsi delete_listing_* - selector, timing, error handling TIDAK diubah.
- scan_all_sheets, proses_baris, proses_kolom - logic + router platform.
- Thin wrappers add_log / safe_update_cell / update_stats / set_processing /
  wait_if_paused / smart_wait - delegasi ke ctx tanpa ubah callsite.
"""

import os
import time
import threading
import random
from playwright.sync_api import sync_playwright

from shared import call_with_timeout, TimeoutHangError


LINK_SHEET_NAME = "LINK"
LINK_COL = 1              # Kolom A - nama tab
LINK_COUNTER_COL = 3      # Kolom C - counter PERLU DELETE per tab (formula COUNTIF)
LINK_START_ROW = 2

KOLOM_CENTANG_MULAI = 15  # Kolom O
KOLOM_CENTANG_AKHIR = 26  # Kolom Z
KOLOM_FLAG_AI = 35        # Kolom AI - flag "PERLU DELETE" (trigger polling)
BARIS_PLATFORM = 48       # Baris 48 - identitas platform (GM / G2G / dll)
BARIS_LINK = 49           # Baris 49 - link manage per kolom
BARIS_MULAI = 51
SCAN_IDLE_WAIT = 600      # detik tunggu antar scan kalau tidak ada PERLU DELETE
# =======================================================

# ===================== MARKET LOCKS =====================
# 1 lock per market -> cegah 2 thread paralel buka tab market yg sama di Chrome
# yang sama (terutama PA / Z2U / ZEUS yang bermasalah dengan dual-tab).
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


# ===================== CTX BINDING (set at each run_one_cycle) =====================
_ctx                  = None
spreadsheet_client    = None   # di-set dari ctx.sheets.spreadsheet
CHROME_CDP_URL        = None   # di-set dari ctx.chrome.cdp_url
current_sheet_label   = {"val": "-"}


def _bind_ctx(ctx):
    """Bind module-level refs supaya 10 fungsi delete_listing_* (yang pakai
    CHROME_CDP_URL, add_log, smart_wait) tidak perlu diubah signature-nya."""
    global _ctx, spreadsheet_client, CHROME_CDP_URL
    _ctx               = ctx
    spreadsheet_client = ctx.sheets.spreadsheet
    CHROME_CDP_URL     = ctx.chrome.cdp_url


# ===================== THIN WRAPPERS (delegate ke ctx) =====================
def add_log(msg):
    """Log message via ctx.logger dengan bot prefix 'delete'."""
    if _ctx is not None:
        _ctx.logger.log("delete", msg)
    else:
        try:
            print(f"[DELETE] {msg}")
        except Exception:
            pass


def update_stats(platform, success):
    """Delegate ke ctx.stats.update."""
    if _ctx is not None and platform:
        _ctx.stats.update("delete", platform, success)


def set_processing(info):
    """info = {"sheet_name", "row", "gid"} atau None - delegate ke ctx.progress."""
    if _ctx is None:
        return
    if info is None:
        _ctx.progress.set("delete", {
            "phase": "idle",
            "current_sheet": None,
            "current_row": None,
        })
    else:
        _ctx.progress.set("delete", {
            "phase": "processing",
            "current_sheet": info.get("sheet_name"),
            "current_row": info.get("row"),
        })


def wait_if_paused(context=""):
    """Return True kalau bot masih boleh jalan (toggle ON + stop_event clear).
    Dipakai proses_baris sebelum spawn thread market berikutnya."""
    if _ctx is None:
        return True
    if _ctx.stop_event.is_set():
        return False
    return _ctx.toggles.should_keep_running("delete")


# ===================== SHEET SCANNER (polling) =====================
_prefetched_active_sheets = None  # di-set orchestrator dari shared prescan


def set_prefetched_active_sheets(names):
    """Inject hasil prescan LINK dari orchestrator. One-shot: dikonsumsi sekali."""
    global _prefetched_active_sheets
    _prefetched_active_sheets = list(names) if names is not None else None


def get_active_sheet_names():
    """Ambil nama tab dari LINK sheet, di-filter hanya yang counter kolom C > 0.

    Kolom A = nama tab. Kolom C = formula COUNTIF "PERLU DELETE" per tab
    (di-set user di LINK sheet). Pre-filter ini hemat besar di kondisi idle:
    kalau semua counter 0 -> cukup 1 API call ringan per scan cycle, tidak perlu
    fetch worksheet metadata atau fase 1 AI scan.

    Return: list nama tab aktif (count > 0), urutan sesuai LINK sheet.
    """
    global _prefetched_active_sheets
    if _prefetched_active_sheets is not None:
        cached = _prefetched_active_sheets
        _prefetched_active_sheets = None
        if cached:
            add_log(f"LINK!C (prescan): {len(cached)} tab aktif")
        return cached
    try:
        response = spreadsheet_client.values_batch_get(
            ranges=[
                f"'{LINK_SHEET_NAME}'!A{LINK_START_ROW}:A",
                f"'{LINK_SHEET_NAME}'!C{LINK_START_ROW}:C",
            ]
        )
    except Exception as e:
        add_log(f"Gagal read LINK!A+C: {str(e)[:200]}")
        return []
    vranges = response.get("valueRanges", []) or []
    col_a = vranges[0].get("values", []) if len(vranges) >= 1 else []
    col_c = vranges[1].get("values", []) if len(vranges) >= 2 else []

    active = []
    total_pending = 0
    for i, row in enumerate(col_a):
        name = (row[0] if row else "").strip()
        if not name:
            continue
        count_str = ""
        if i < len(col_c) and col_c[i]:
            count_str = str(col_c[i][0]).strip()
        try:
            count = int(count_str) if count_str else 0
        except ValueError:
            count = 0
        if count > 0:
            active.append(name)
            total_pending += count
    if active:
        add_log(f"LINK!C counter: {len(active)} tab aktif, total {total_pending} PERLU DELETE")
    return active


def scan_all_sheets(n=1):
    """
    Scan 3-lapis untuk efisiensi bandwidth (90 tab x 800 row x 35 col = gede).

    Pre-filter -> read LINK!A+C 1 batch. Kolom C = counter COUNTIF "PERLU DELETE"
                 per tab. Skip tab dengan counter 0. Kalau semua 0 -> exit di sini
                 (1 API call saja di idle cycle).

    Fase 1 -> batch_get kolom AI51:AI hanya untuk tab aktif (payload kecil, ~1 kolom).
             Verifikasi posisi baris flag (counter C mungkin stale kalau formula
             belum re-evaluate).

    Fase 2 -> batch_get full data A:AI 1-per-1 tab sampai n baris terpenuhi.

    Total API read idle: 1 (pre-filter). Ada kerjaan: pre + metadata + fase1 + fase2.

    Return: list of (worksheet, data, baris_index), max n baris.
    """
    results = []
    sheet_names = get_active_sheet_names()
    if not sheet_names:
        return results

    def _escape(nm):
        # A1 notation: apostrof literal harus di-double ("don't" -> "don''t")
        return nm.replace("'", "''")

    # -- Filter: hanya tab yang punya kolom AI (col_count >= 35) ---------
    # Tab lama/template mungkin cuman sampai AH (34 kol). Kalau ikut di-include,
    # batch_get GAGAL ATOMIK -> semua tab lain ikut crash.
    # worksheets() pakai metadata cache gspread (1 API call per scan, murah).
    try:
        all_ws = {ws.title: ws for ws in spreadsheet_client.worksheets()}
    except Exception as e:
        add_log(f"Gagal fetch worksheet metadata: {str(e)[:200]}")
        current_sheet_label["val"] = "-"
        return results

    valid_pairs = []   # list of (original_index, name, worksheet)
    skipped = []
    for idx, name in enumerate(sheet_names):
        ws = all_ws.get(name)
        if ws is None:
            skipped.append(f"{name}(tidak ada)")
        elif ws.col_count < KOLOM_FLAG_AI:
            skipped.append(f"{name}({ws.col_count}kol)")
        else:
            valid_pairs.append((idx, name, ws))

    if skipped:
        preview = ", ".join(skipped[:3])
        tail = "..." if len(skipped) > 3 else ""
        add_log(f"Skip {len(skipped)} tab tanpa kolom AI: {preview}{tail}")

    if not valid_pairs:
        current_sheet_label["val"] = "-"
        return results

    # -- Fase 1: scan kolom AI saja untuk tab yang valid -----------------
    flag_ranges = [f"'{_escape(name)}'!AI{BARIS_MULAI}:AI" for (_, name, _) in valid_pairs]
    try:
        response = spreadsheet_client.values_batch_get(ranges=flag_ranges)
    except Exception as e:
        add_log(f"Gagal fase 1 (scan flag AI): {str(e)[:200]}")
        current_sheet_label["val"] = "-"
        return results
    flag_vranges = response.get("valueRanges", []) or []

    hits_per_tab = {}  # key = posisi dalam valid_pairs (bukan original index)
    for pair_idx, (_, name, _) in enumerate(valid_pairs):
        if pair_idx >= len(flag_vranges):
            continue
        col_data = flag_vranges[pair_idx].get("values", []) or []
        # col_data row_offset 0 == baris BARIS_MULAI (1-based) == index BARIS_MULAI-1
        for row_offset, cell_row in enumerate(col_data):
            flag = (cell_row[0] if cell_row else "").strip()
            if flag.upper() == "PERLU DELETE":
                baris_index = (BARIS_MULAI - 1) + row_offset
                hits_per_tab.setdefault(pair_idx, []).append(baris_index)

    if not hits_per_tab:
        current_sheet_label["val"] = "-"
        return results

    # -- Fase 2: fetch full data A:AI HANYA untuk tab yang dibutuhkan ----
    # Karena kita cuman proses n row (default 1), kita fetch tab 1-per-1
    # sampai cukup. Kalau 90 tab match tapi n=1, kita cukup fetch 1 tab,
    # bukan 90. Hemat bandwidth besar saat banyak tab kena flag.
    pairs_sorted = sorted(hits_per_tab.keys())
    for pair_idx in pairs_sorted:
        if len(results) >= n:
            break
        _, name, sheet = valid_pairs[pair_idx]
        current_sheet_label["val"] = name
        try:
            response2 = spreadsheet_client.values_batch_get(
                ranges=[f"'{_escape(name)}'!A:AI"]
            )
        except Exception as e:
            add_log(f"Gagal fase 2 tab '{name}': {str(e)[:200]}")
            continue
        vranges = response2.get("valueRanges", []) or []
        if not vranges:
            continue
        data = vranges[0].get("values", []) or []
        if not data:
            continue
        for baris_index in hits_per_tab[pair_idx]:
            if len(results) >= n:
                break
            # Defensive: re-verify flag di data fase 2 - catch race kalau user
            # ubah sheet antara fase 1 & fase 2.
            if baris_index >= len(data):
                continue
            row = data[baris_index]
            flag = row[KOLOM_FLAG_AI - 1].strip() if len(row) >= KOLOM_FLAG_AI else ""
            if flag.upper() != "PERLU DELETE":
                continue
            add_log(f"Ketemu PERLU DELETE: sheet='{name}' baris={baris_index + 1}")
            results.append((sheet, data, baris_index))

    current_sheet_label["val"] = "-"
    return results



# ===================== PAUSE-AWARE WAIT =====================
def smart_wait(page, min_ms, max_ms):
    """Wait random time. Toggle OFF tidak interrupt mid-Playwright - cek toggle
    terjadi di row boundary (proses_baris loop) bukan mid-click, supaya flow
    Playwright satu row tidak terpotong di tengah klik."""
    page.wait_for_timeout(random.randint(min_ms, max_ms))


# ===================== SHEETS QUOTA-SAFE WRAPPER =====================
def safe_update_cell(sheet, row, col, value):
    """Thin wrapper - delegate ke ctx.sheets.safe_update_cell (wrapper
    quota-safe + call_with_timeout sudah di-handle di shared.SheetsClient).
    Return True/False untuk kompat callsite lama di proses_kolom / proses_baris."""
    if _ctx is None:
        return False
    try:
        _ctx.sheets.safe_update_cell(sheet, row, col, value,
                                     desc=f"r{row}c{col}")
        return True
    except Exception as e:
        add_log(f"safe_update_cell gagal: {str(e)[:100]}")
        return False


# ===================== CONFIG GM - GameMarket =====================
def delete_listing_gm(kode_listing, manage_link):
    with sync_playwright() as p:
        page = None

        try:
            # Timeout cap: connect 10s, semua action default 60s - jangan sampai hang selamanya
            browser = p.chromium.connect_over_cdp(CHROME_CDP_URL, timeout=10000)
            context = browser.contexts[0]
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()
            add_log(f"[GM] Membuka halaman: {manage_link}")
            page.goto(manage_link)
            page.wait_for_load_state("networkidle", timeout=30000)
            smart_wait(page, 4000, 6000)  # #2 - 4-6 detik

            add_log(f"[GM] Search kode listing: {kode_listing}...")
            search_input = page.locator("input[placeholder*='search' i], input[type='search'], input[name*='search' i]").first
            search_input.fill(kode_listing)
            search_input.press("Enter")
            smart_wait(page, 3000, 5000)  # #3 - 3-5 detik

            add_log("[GM] Klik icon sampah...")
            delete_btn = page.locator("svg.text-red.cursor-pointer").first
            delete_btn.wait_for(state="visible", timeout=10000)
            delete_btn.click()
            smart_wait(page, 1000, 2000)  # #4 - 1-2 detik

            add_log("[GM] Klik tombol konfirmasi Delete...")
            confirm_btn = page.locator("button:has-text('Delete'), button:has-text('Confirm'), button:has-text('Yes')").first
            confirm_btn.click(timeout=5000)
            smart_wait(page, 3000, 5000)  # #5 - 3-5 detik

            add_log(f"[GM] Listing {kode_listing} berhasil dihapus!")
            return True, None

        except Exception as e:
            pesan_error = str(e)
            if "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[GM] Gagal: {indo_error}")
            return False, indo_error

        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


# ===================== CONFIG G2G =====================
def delete_listing_g2g(kode_listing, manage_link):
    with sync_playwright() as p:
        page = None

        try:
            # Timeout cap: connect 10s, semua action default 60s - jangan sampai hang selamanya
            browser = p.chromium.connect_over_cdp(CHROME_CDP_URL, timeout=10000)
            context = browser.contexts[0]
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()
            add_log(f"[G2G] Membuka halaman: {manage_link}")
            page.goto(manage_link)
            page.wait_for_load_state("networkidle", timeout=30000)
            smart_wait(page, 4000, 6000)  # tunggu halaman stabil

            add_log(f"[G2G] Search kode listing: {kode_listing}...")
            search_input = page.locator("input.q-field__native[placeholder='Cari judul atau nomor produk']").first
            search_input.wait_for(state="visible", timeout=10000)
            search_input.fill(kode_listing)
            search_input.press("Enter")
            smart_wait(page, 3000, 5000)  # tunggu hasil search

            add_log("[G2G] Klik tombol titik tiga...")
            more_btn = page.locator("button.g-btn-round i.material-icons:has-text('more_vert')").locator("..").locator("..").first
            more_btn.wait_for(state="visible", timeout=10000)
            more_btn.click()
            smart_wait(page, 1000, 2000)  # tunggu dropdown muncul

            add_log("[G2G] Klik Hapus...")
            hapus_btn = page.locator(".q-item__section:has-text('Hapus')").first
            hapus_btn.wait_for(state="visible", timeout=10000)
            hapus_btn.click()
            smart_wait(page, 1000, 2000)  # tunggu popup konfirmasi muncul

            add_log("[G2G] Klik tombol Konfirmasi...")
            konfirmasi_btn = page.locator("button.bg-primary:has-text('Konfirmasi')").first
            konfirmasi_btn.wait_for(state="visible", timeout=10000)
            konfirmasi_btn.click()
            smart_wait(page, 3000, 5000)  # tunggu proses hapus selesai

            add_log(f"[G2G] Listing {kode_listing} berhasil dihapus!")
            return True, None

        except Exception as e:
            pesan_error = str(e)
            if "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[G2G] Gagal: {indo_error}")
            return False, indo_error

        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


# ===================== CONFIG PA - PlayerAuctions =====================
def delete_listing_pa(kode_listing, manage_link):
    with sync_playwright() as p:
        page = None

        try:
            # Timeout cap: connect 10s, semua action default 60s - jangan sampai hang selamanya
            browser = p.chromium.connect_over_cdp(CHROME_CDP_URL, timeout=10000)
            context = browser.contexts[0]
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()
            add_log(f"[PA] Membuka halaman: {manage_link}")
            page.goto(manage_link)
            page.wait_for_load_state("networkidle", timeout=30000)
            smart_wait(page, 4000, 6000)  # tunggu halaman stabil

            add_log(f"[PA] Search kode listing: {kode_listing}...")
            search_input = page.locator("input#keywords[placeholder='Search...']").first
            search_input.wait_for(state="visible", timeout=10000)
            search_input.fill(kode_listing)
            search_input.press("Enter")
            smart_wait(page, 3000, 5000)  # tunggu hasil search

            add_log("[PA] Klik checkbox...")
            checkbox = page.locator("input.ant-checkbox-input[type='checkbox']").first
            checkbox.wait_for(state="visible", timeout=10000)
            checkbox.click()
            smart_wait(page, 1000, 2000)  # tunggu checkbox tercentang

            add_log("[PA] Klik tombol Cancel...")
            cancel_btn = page.locator("button.ant-btn-dangerous:has-text('Cancel')").first
            cancel_btn.wait_for(state="visible", timeout=10000)
            cancel_btn.click()
            smart_wait(page, 3000, 5000)  # tunggu proses cancel

            add_log("[PA] Klik tombol SELECTED OFFERS...")
            selected_btn = page.locator("button.ant-btn-primary:has-text('SELECTED OFFERS')").first
            selected_btn.wait_for(state="visible", timeout=10000)
            selected_btn.click()
            smart_wait(page, 3000, 5000)  # tunggu proses selesai

            add_log(f"[PA] Listing {kode_listing} berhasil di-cancel!")
            return True, None

        except Exception as e:
            pesan_error = str(e)
            if "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[PA] Gagal: {indo_error}")
            return False, indo_error

        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


# ===================== CONFIG ELDO - Eldorado =====================
def delete_listing_eldo(kode_listing, manage_link):
    with sync_playwright() as p:
        page = None

        try:
            # Timeout cap: connect 10s, semua action default 60s - jangan sampai hang selamanya
            browser = p.chromium.connect_over_cdp(CHROME_CDP_URL, timeout=10000)
            context = browser.contexts[0]
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()
            add_log(f"[ELDO] Membuka halaman: {manage_link}")
            page.goto(manage_link)
            page.wait_for_load_state("networkidle", timeout=30000)
            smart_wait(page, 4000, 6000)  # tunggu halaman stabil

            add_log(f"[ELDO] Search kode listing: {kode_listing}...")
            search_input = page.locator("input[placeholder='Search offers']").first
            search_input.wait_for(state="visible", timeout=10000)
            search_input.fill(kode_listing)
            search_input.press("Enter")
            smart_wait(page, 3000, 5000)  # tunggu hasil search

            add_log("[ELDO] Klik icon delete...")
            delete_btn = page.locator("button.button__ghost[aria-label='Delete'] span.icomoon-icon.icon-delete").locator("..").locator("..").locator("..").first
            delete_btn.wait_for(state="visible", timeout=10000)
            delete_btn.click()
            smart_wait(page, 1000, 2000)  # tunggu popup muncul

            add_log("[ELDO] Klik tombol konfirmasi Delete...")
            confirm_btn = page.locator("button.button__primary[aria-label='Delete']").first
            confirm_btn.wait_for(state="visible", timeout=10000)
            confirm_btn.click()
            smart_wait(page, 3000, 5000)  # tunggu proses hapus selesai

            add_log(f"[ELDO] Listing {kode_listing} berhasil dihapus!")
            return True, None

        except Exception as e:
            pesan_error = str(e)
            if "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[ELDO] Gagal: {indo_error}")
            return False, indo_error

        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


# ===================== CONFIG Z2U =====================
def delete_listing_z2u(kode_listing, manage_link):
    with sync_playwright() as p:
        page = None

        try:
            # Timeout cap: connect 10s, semua action default 60s - jangan sampai hang selamanya
            browser = p.chromium.connect_over_cdp(CHROME_CDP_URL, timeout=10000)
            context = browser.contexts[0]
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()
            add_log(f"[Z2U] Membuka halaman: {manage_link}")
            page.goto(manage_link)
            page.wait_for_load_state("networkidle", timeout=30000)
            smart_wait(page, 5000, 9000)  # tunggu halaman stabil - 5-9 detik

            add_log(f"[Z2U] Klik kolom search...")
            search_input = page.locator("input.form-control.searchbox").first
            search_input.wait_for(state="visible", timeout=10000)
            search_input.click()
            smart_wait(page, 2000, 3000)  # wait 2-3 detik

            add_log(f"[Z2U] Paste kode listing: {kode_listing}...")
            search_input.fill(kode_listing)
            smart_wait(page, 2000, 3000)  # wait 2-3 detik

            add_log("[Z2U] Klik tombol search...")
            search_btn = page.locator("button.filter-search-button").first
            search_btn.wait_for(state="visible", timeout=10000)
            search_btn.click()
            smart_wait(page, 4000, 6000)  # tunggu hasil search - 4-6 detik

            add_log("[Z2U] Centang checkbox...")
            checkbox = page.locator("input.dataid[type='checkbox']").first
            checkbox.wait_for(state="visible", timeout=10000)
            checkbox.click()
            smart_wait(page, 2000, 3000)  # wait 2-3 detik

            add_log("[Z2U] Klik tombol Delete...")
            delete_btn = page.locator("button.zu-btn.zu-btn-outline-danger.deleteAll").first
            delete_btn.wait_for(state="visible", timeout=10000)
            delete_btn.click()
            smart_wait(page, 3000, 5000)  # tunggu popup muncul - 3-5 detik

            add_log("[Z2U] Klik tombol Submit...")
            submit_btn = page.locator("button#deleteOption").first
            submit_btn.wait_for(state="visible", timeout=10000)
            submit_btn.click()
            smart_wait(page, 4000, 6000)  # tunggu proses selesai - 4-6 detik

            add_log(f"[Z2U] Listing {kode_listing} berhasil dihapus!")
            return True, None

        except Exception as e:
            pesan_error = str(e)
            if "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[Z2U] Gagal: {indo_error}")
            return False, indo_error

        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


# ===================== CONFIG ZEUS - ZeusX =====================
def delete_listing_zeus(kode_listing, manage_link):
    with sync_playwright() as p:
        page = None

        try:
            # Timeout cap: connect 10s, semua action default 60s - jangan sampai hang selamanya
            browser = p.chromium.connect_over_cdp(CHROME_CDP_URL, timeout=10000)
            context = browser.contexts[0]
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()
            add_log(f"[ZEUS] Membuka halaman: {manage_link}")
            page.goto(manage_link, wait_until="domcontentloaded")
            smart_wait(page, 4000, 6000)  # tunggu SPA render

            add_log(f"[ZEUS] Klik kolom search...")
            page.wait_for_function(
                "() => document.querySelector(\"input[placeholder='Search listing...']\") !== null",
                timeout=20000
            )
            page.evaluate("document.querySelector(\"input[placeholder='Search listing...']\").click()")
            smart_wait(page, 2000, 3000)  # wait 2-3 detik

            add_log(f"[ZEUS] Paste kode listing: {kode_listing}...")
            page.evaluate(f"""
                var el = document.querySelector("input[placeholder='Search listing...']");
                var nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                nativeInputValueSetter.call(el, '{kode_listing}');
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            """)
            smart_wait(page, 2000, 3000)  # wait 2-3 detik

            add_log("[ZEUS] Enter...")
            page.evaluate("""
                var el = document.querySelector("input[placeholder='Search listing...']");
                el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
                el.dispatchEvent(new KeyboardEvent('keypress', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
                el.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
            """)
            smart_wait(page, 4000, 6000)  # tunggu hasil search - 4-6 detik

            add_log("[ZEUS] Klik tombol titik tiga...")
            more_btn = page.locator("button.more-actions-button_more-action-button__gL0LZ").first
            more_btn.wait_for(state="visible", timeout=10000)
            more_btn.click()
            smart_wait(page, 2000, 5000)  # wait 2-5 detik

            # Cari "Cancel Offer" dulu. Kalau tidak ada, cek "Chat With Customer"
            # - kalau ada artinya listing sudah terjual di ZEUS (tidak bisa di-hapus),
            # anggap sukses supaya flag "PERLU DELETE" ikut di-uncentang.
            add_log("[ZEUS] Cari opsi Cancel Offer...")
            cancel_btn = page.locator("span.more-actions-button_label__1hW7H:has-text('Cancel Offer')").first
            try:
                cancel_btn.wait_for(state="visible", timeout=5000)
                cancel_found = True
            except Exception:
                cancel_found = False

            if not cancel_found:
                chat_btn = page.locator("span.more-actions-button_label__1hW7H:has-text('Chat With Customer')").first
                if chat_btn.count() > 0 and chat_btn.is_visible():
                    add_log(f"[ZEUS] Listing {kode_listing} sudah terjual di ZEUS (ketemu 'Chat With Customer'), anggap sukses.")
                    return True, None
                # Tidak ada Cancel Offer & tidak ada Chat With Customer - fail seperti biasa
                raise Exception("Tombol 'Cancel Offer' tidak ditemukan dan bukan listing terjual")

            add_log("[ZEUS] Klik Cancel Offer...")
            cancel_btn.click()
            smart_wait(page, 2000, 5000)  # tunggu popup muncul - 2-5 detik

            add_log("[ZEUS] Klik tombol Remove Listing...")
            remove_btn = page.locator("button.success-popup_btn-primary__DpGCB div:has-text('Remove Listing')").locator("..").first
            remove_btn.wait_for(state="visible", timeout=10000)
            remove_btn.click()
            smart_wait(page, 2000, 5000)  # tunggu proses selesai - 2-5 detik

            add_log(f"[ZEUS] Listing {kode_listing} berhasil di-cancel!")
            return True, None

        except Exception as e:
            pesan_error = str(e)
            if "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[ZEUS] Gagal: {indo_error}")
            return False, indo_error

        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


# ===================== CONFIG U7 - U7Buy =====================
def delete_listing_u7(kode_listing, manage_link):
    with sync_playwright() as p:
        page = None

        try:
            # Timeout cap: connect 10s, semua action default 60s - jangan sampai hang selamanya
            browser = p.chromium.connect_over_cdp(CHROME_CDP_URL, timeout=10000)
            context = browser.contexts[0]
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()
            add_log(f"[U7] Membuka halaman: {manage_link}")
            page.goto(manage_link, wait_until="domcontentloaded")
            smart_wait(page, 4000, 8000)  # tunggu halaman stabil - 4-8 detik

            add_log(f"[U7] Klik kolom search...")
            search_input = page.locator("input.el-input__inner[placeholder='Offer Name']").first
            search_input.wait_for(state="visible", timeout=15000)
            search_input.click()
            smart_wait(page, 2000, 3000)  # wait 2-3 detik

            add_log(f"[U7] Paste kode listing: {kode_listing}...")
            search_input.fill(kode_listing)
            smart_wait(page, 3000, 5000)  # wait 3-5 detik

            add_log("[U7] Centang checkbox (Off Sale)...")
            checkbox1 = page.locator("input.el-checkbox__original[type='checkbox']").first
            checkbox1.wait_for(state="attached", timeout=15000)
            page.evaluate("document.querySelector('input.el-checkbox__original[type=\"checkbox\"]').click()")
            smart_wait(page, 2000, 3000)  # wait 2-3 detik

            add_log("[U7] Klik tombol Off Sale...")
            off_sale_btn = page.locator("aside.u7-button:has-text('Off Sale')").first
            off_sale_btn.wait_for(state="visible", timeout=15000)
            off_sale_btn.click()
            smart_wait(page, 4000, 6000)  # wait 4-6 detik

            add_log("[U7] Centang checkbox (Delete)...")
            page.evaluate("document.querySelector('input.el-checkbox__original[type=\"checkbox\"]').click()")
            smart_wait(page, 2000, 3000)  # wait 2-3 detik

            add_log("[U7] Klik tombol Delete...")
            delete_btn = page.locator("aside.u7-button.hidden-sm-and-down:has-text('Delete')").first
            delete_btn.wait_for(state="visible", timeout=15000)
            delete_btn.click()
            smart_wait(page, 4000, 6000)  # wait 4-6 detik

            add_log(f"[U7] Listing {kode_listing} berhasil dihapus!")
            return True, None

        except Exception as e:
            pesan_error = str(e)
            if "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[U7] Gagal: {indo_error}")
            return False, indo_error

        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


# ===================== CONFIG GB - GameBoost =====================
def delete_listing_gb(kode_listing, manage_link):
    with sync_playwright() as p:
        page = None

        try:
            # Timeout cap: connect 10s, semua action default 60s - jangan sampai hang selamanya
            browser = p.chromium.connect_over_cdp(CHROME_CDP_URL, timeout=10000)
            context = browser.contexts[0]
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()
            add_log(f"[GB] Membuka halaman: {manage_link}")
            page.goto(manage_link, wait_until="domcontentloaded")
            smart_wait(page, 4000, 8000)  # tunggu halaman stabil - 4-8 detik

            add_log(f"[GB] Klik kolom search...")
            search_input = page.locator("input[placeholder='Search...']").first
            search_input.wait_for(state="visible", timeout=15000)
            search_input.click()
            smart_wait(page, 2000, 3000)  # wait 2-3 detik

            add_log(f"[GB] Paste kode listing: {kode_listing}...")
            search_input.fill(kode_listing)
            smart_wait(page, 3000, 5000)  # wait 3-5 detik

            add_log("[GB] Klik checkbox...")
            checkbox = page.locator("button[role='checkbox'][aria-label='Select Row']").first
            checkbox.wait_for(state="visible", timeout=15000)
            checkbox.click()
            smart_wait(page, 2000, 3000)  # wait 2-3 detik

            add_log("[GB] Klik Delete 1 Account...")
            delete_btn = page.locator("button:has-text('Delete 1 Account')").first
            delete_btn.wait_for(state="visible", timeout=15000)
            delete_btn.click()
            smart_wait(page, 2000, 3000)  # wait 2-3 detik

            add_log("[GB] Klik tombol Confirm...")
            confirm_btn = page.locator("button:has-text('Confirm')").first
            confirm_btn.wait_for(state="visible", timeout=15000)
            confirm_btn.click()
            smart_wait(page, 4000, 8000)  # tunggu proses selesai - 4-8 detik

            add_log(f"[GB] Listing {kode_listing} berhasil dihapus!")
            return True, None

        except Exception as e:
            pesan_error = str(e)
            if "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[GB] Gagal: {indo_error}")
            return False, indo_error

        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


# ===================== CONFIG IGV =====================
def delete_listing_igv(kode_listing, manage_link):
    with sync_playwright() as p:
        page = None

        try:
            # Timeout cap: connect 10s, semua action default 60s - jangan sampai hang selamanya
            browser = p.chromium.connect_over_cdp(CHROME_CDP_URL, timeout=10000)
            context = browser.contexts[0]
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()
            add_log(f"[IGV] Membuka halaman: {manage_link}")
            page.goto(manage_link, wait_until="domcontentloaded")
            smart_wait(page, 4000, 8000)  # tunggu halaman stabil - 4-8 detik

            add_log(f"[IGV] Klik kolom search...")
            search_input = page.locator("input.el-input__inner[placeholder='Product Name/Product Code']").first
            search_input.wait_for(state="visible", timeout=15000)
            search_input.click()
            smart_wait(page, 2000, 3000)  # wait 2-3 detik

            add_log(f"[IGV] Paste kode listing: {kode_listing}...")
            search_input.fill(kode_listing)
            smart_wait(page, 3000, 5000)  # wait 3-5 detik

            add_log("[IGV] Klik tombol Search...")
            search_btn = page.locator("button.el-button.el-button--primary:has-text('Search')").first
            search_btn.wait_for(state="visible", timeout=15000)
            search_btn.click()
            smart_wait(page, 3000, 5000)  # tunggu hasil search - 3-5 detik

            add_log("[IGV] Klik Take offline...")
            offline_btn = page.locator("button.el-button.el-button--primary.is-link:has-text('Take offline')").first
            offline_btn.wait_for(state="visible", timeout=15000)
            offline_btn.click()
            smart_wait(page, 2000, 3000)  # tunggu popup - 2-3 detik

            add_log("[IGV] Klik tombol Confirm...")
            confirm_btn = page.locator("button.el-button.el-button--primary.el-button--large:has-text('Confirm')").first
            confirm_btn.wait_for(state="visible", timeout=15000)
            confirm_btn.click()
            smart_wait(page, 4000, 6000)  # tunggu proses selesai - 4-6 detik

            add_log(f"[IGV] Listing {kode_listing} berhasil di-offline!")
            return True, None

        except Exception as e:
            pesan_error = str(e)
            if "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[IGV] Gagal: {indo_error}")
            return False, indo_error

        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


# ===================== CONFIG FP - Funpay =====================
def delete_listing_fp(kode_listing, manage_link):
    with sync_playwright() as p:
        page = None

        try:
            # Timeout cap: connect 10s, semua action default 60s - jangan sampai hang selamanya
            browser = p.chromium.connect_over_cdp(CHROME_CDP_URL, timeout=10000)
            context = browser.contexts[0]
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()
            add_log(f"[FP] Membuka halaman: {manage_link}")
            page.goto(manage_link, wait_until="domcontentloaded")
            smart_wait(page, 4000, 6000)  # tunggu halaman stabil - 4-6 detik

            add_log(f"[FP] Ctrl+F cari kode: {kode_listing}...")
            page.keyboard.press("Control+f")
            smart_wait(page, 2000, 3000)  # wait 2-3 detik

            page.keyboard.type(kode_listing)
            smart_wait(page, 2000, 3000)  # wait 2-3 detik

            page.keyboard.press("Enter")
            smart_wait(page, 2000, 3000)  # wait 2-3 detik

            add_log(f"[FP] Klik elemen yang mengandung kode: {kode_listing}...")
            page.keyboard.press("Escape")  # tutup browser find bar dulu
            result_elem = page.locator(f"*:has-text('{kode_listing}')").last
            result_elem.wait_for(state="visible", timeout=15000)
            result_elem.click()
            smart_wait(page, 3000, 5000)  # tunggu halaman offer - 3-5 detik

            add_log("[FP] Klik Edit an offer...")
            edit_btn = page.locator("a.btn.btn-gray.btn-block:has-text('Edit an offer')").first
            edit_btn.wait_for(state="visible", timeout=15000)
            edit_btn.click()
            smart_wait(page, 3000, 5000)  # tunggu halaman edit - 3-5 detik

            add_log("[FP] Klik tombol Delete...")
            delete_btn = page.locator("button.btn.btn-danger.js-btn-delete:not(.confirm)").first
            delete_btn.wait_for(state="visible", timeout=15000)
            delete_btn.click()
            smart_wait(page, 2000, 3000)  # wait 2-3 detik

            add_log("[FP] Klik tombol Confirm deletion...")
            confirm_btn = page.locator("button.btn.btn-danger.js-btn-delete.confirm").first
            confirm_btn.wait_for(state="visible", timeout=15000)
            confirm_btn.click()
            smart_wait(page, 3000, 5000)  # tunggu proses selesai - 3-5 detik

            add_log(f"[FP] Listing {kode_listing} berhasil dihapus!")
            return True, None

        except Exception as e:
            pesan_error = str(e)
            if "Timeout" in pesan_error:
                indo_error = "Waktu habis, tombol tidak ditemukan"
            elif "visible" in pesan_error:
                indo_error = "Elemen tidak terlihat di halaman"
            elif "net::" in pesan_error:
                indo_error = "Gagal membuka halaman, cek koneksi internet"
            else:
                indo_error = f"Terjadi kesalahan: {pesan_error[:80]}"
            add_log(f"[FP] Gagal: {indo_error}")
            return False, indo_error

        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


# ===================== ROUTER PLATFORM =====================
def proses_kolom(sheet, data, baris_index, kolom_index):
    baris_nomor = baris_index + 1
    kolom_huruf = chr(64 + kolom_index + 1)

    # Baca platform dari baris 48
    platform = data[BARIS_PLATFORM - 1][kolom_index] if len(data[BARIS_PLATFORM - 1]) > kolom_index else ""
    platform = platform.strip().upper()

    # Baca link dari baris 49
    link = data[BARIS_LINK - 1][kolom_index] if len(data[BARIS_LINK - 1]) > kolom_index else ""

    if not platform:
        add_log(f"Platform di kolom {kolom_huruf}48 kosong, tidak tahu harus pakai config apa!")
        safe_update_cell(sheet, baris_nomor, kolom_index + 1, "❌ Baris 48 kosong, platform tidak dikenali")
        return

    if not link:
        add_log(f"Link di kolom {kolom_huruf}49 kosong!")
        safe_update_cell(sheet, baris_nomor, kolom_index + 1, "❌ Link di baris 49 kosong")
        return

    kode_listing = data[baris_index][0]

    if not kode_listing:
        add_log(f"Kode listing di kolom A baris {baris_nomor} kosong!")
        safe_update_cell(sheet, baris_nomor, kolom_index + 1, "❌ Kode listing di kolom A kosong")
        return

    add_log(f"Proses kolom {kolom_huruf} baris {baris_nomor} | Platform: {platform} | Kode: {kode_listing} | Link: {link}")

    # Acquire market_lock -> cegah 2 thread paralel buka market yang sama di
    # Chrome yang sama (terutama PA / Z2U / ZEUS yang bermasalah dual-tab).
    # Thread dengan market BEDA tetap jalan paralel; yang SAMA antri serial.
    # Timeout 900s supaya kalau thread sebelumnya zombie (Chrome hang), cycle
    # berikutnya tidak deadlock permanen.
    lock = market_locks.get(platform)
    lock_acquired = False
    if lock is not None:
        lock_acquired = lock.acquire(timeout=900)
        if not lock_acquired:
            msg = f"Market {platform} lock timeout 900s (thread sebelumnya stuck)"
            add_log(msg)
            safe_update_cell(sheet, baris_nomor, kolom_index + 1, f"❌ {msg}")
            update_stats(platform, False)
            return
    try:
        # Routing ke config masing-masing platform
        if platform == "GM":
            berhasil, error = delete_listing_gm(kode_listing, link)
        elif platform == "G2G":
            berhasil, error = delete_listing_g2g(kode_listing, link)
        elif platform == "PA":
            berhasil, error = delete_listing_pa(kode_listing, link)
        elif platform == "ELDO":
            berhasil, error = delete_listing_eldo(kode_listing, link)
        elif platform == "Z2U":
            berhasil, error = delete_listing_z2u(kode_listing, link)
        elif platform == "ZEUS":
            berhasil, error = delete_listing_zeus(kode_listing, link)
        elif platform == "U7":
            berhasil, error = delete_listing_u7(kode_listing, link)
        elif platform == "GB":
            berhasil, error = delete_listing_gb(kode_listing, link)
        elif platform == "IGV":
            berhasil, error = delete_listing_igv(kode_listing, link)
        elif platform == "FP":
            berhasil, error = delete_listing_fp(kode_listing, link)
        else:
            add_log(f"Platform '{platform}' di kolom {kolom_huruf}48 tidak dikenali, skip.")
            safe_update_cell(sheet, baris_nomor, kolom_index + 1, f"❌ Platform '{platform}' tidak dikenali")
            return
    finally:
        if lock is not None and lock_acquired:
            try:
                lock.release()
            except Exception:
                pass

    if berhasil:
        safe_update_cell(sheet, baris_nomor, kolom_index + 1, "FALSE")
        add_log(f"Kolom {kolom_huruf}{baris_nomor} berhasil di-uncentang.")
        update_stats(platform, True)
    else:
        safe_update_cell(sheet, baris_nomor, kolom_index + 1, f"❌ {error}")
        add_log(f"Kolom {kolom_huruf}{baris_nomor} diisi error: {error}")
        update_stats(platform, False)


def proses_baris(sheet, data, baris_index):
    """
    Proses 1 baris yang kena flag PERLU DELETE di kolom AI.
    - Iterasi O~Z, kumpulkan kolom yang nilainya TRUE
    - Dispatch tiap kolom ke thread terpisah, stagger 1 detik antar start
    - Tiap thread pegang market_lock (lihat proses_kolom) -> 2 thread dengan
      market yang sama tetap serial, beda market boleh jalan paralel
    """
    baris_nomor = baris_index + 1
    try:
        sheet_name = sheet.title
        sheet_gid = sheet.id
    except Exception:
        sheet_name = "?"
        sheet_gid = 0

    # Set processing status untuk dashboard (clickable -> buka Sheet di row ini)
    set_processing({
        "sheet_name": sheet_name,
        "row": baris_nomor,
        "gid": sheet_gid,
    })

    try:
        row = data[baris_index]

        # Kumpulkan kolom O~Z yang di-centang TRUE
        kolom_aktif = []
        for k in range(KOLOM_CENTANG_MULAI - 1, KOLOM_CENTANG_AKHIR):
            nilai = row[k].strip() if len(row) > k else ""
            if nilai.upper() == "TRUE":
                kolom_aktif.append(k)

        if not kolom_aktif:
            add_log(f"Tidak ada centang di O-Z baris {baris_nomor} ({sheet_name}), skip.")
            return

        add_log(f"Proses baris {baris_nomor} ({sheet_name}) - {len(kolom_aktif)} market, paralel (stagger 1s)...")

        # Spawn 1 thread per kolom, stagger 1 detik antar start
        threads = []
        for i, k in enumerate(kolom_aktif):
            if not wait_if_paused(f"sebelum spawn market kolom {chr(64 + k + 1)}"):
                break
            t = threading.Thread(
                target=proses_kolom,
                args=(sheet, data, baris_index, k),
                daemon=True,
                name=f"market-{chr(64 + k + 1)}-row{baris_nomor}",
            )
            t.start()
            threads.append(t)
            if i < len(kolom_aktif) - 1:
                time.sleep(1)  # stagger 1 detik antar open market (biar Chrome napas)

        # Tunggu semua thread selesai (timeout 15 menit per thread)
        for t in threads:
            t.join(timeout=900)
            if t.is_alive():
                add_log(f"Market thread '{t.name}' timeout 15 menit, lanjut...")
                if _ctx is not None:
                    _ctx.zombies.track(t, "delete", context=f"row {baris_nomor}")

        add_log(f"Selesai proses baris {baris_nomor} ({sheet_name})")
    finally:
        # Clear processing status - dashboard balik ke "Idle"
        set_processing(None)


# ===================== ENTRY POINT =====================
BOT_NAME = "delete"


def run_one_cycle(ctx):
    """1 cycle: scan LINK!C -> proses max 1 row. Return: 1 kalau ada row diproses,
    0 kalau idle / toggle OFF / stop_event set / Sheets error.
    Orchestrator di main.py yang handle idle sleep + reconnect antar cycle
    (tidak lagi bot_loop di file ini - polling mode 2.2.5 digantikan oleh loop
    sequential Delete->Create->Diskon di main.py)."""
    _bind_ctx(ctx)

    if ctx.stop_event.is_set():
        return 0
    if not ctx.toggles.should_keep_running(BOT_NAME):
        return 0
    if ctx.sheets.spreadsheet is None:
        add_log("Sheets belum ter-connect, skip cycle")
        return 0

    ctx.progress.set(BOT_NAME, {"phase": "scanning"})

    try:
        results = scan_all_sheets(n=1)
    except Exception as e:
        add_log(f"scan_all_sheets error: {str(e)[:150]}")
        ctx.progress.set(BOT_NAME, {"phase": "idle"})
        return 0

    if not results:
        ctx.progress.set(BOT_NAME, {"phase": "idle", "current_sheet": None})
        return 0

    if ctx.stop_event.is_set() or not ctx.toggles.should_keep_running(BOT_NAME):
        ctx.progress.set(BOT_NAME, {"phase": "idle"})
        return 0

    sheet, data, baris_index = results[0]
    try:
        proses_baris(sheet, data, baris_index)
    except Exception as e:
        add_log(f"proses_baris error: {str(e)[:150]}")
        ctx.progress.set(BOT_NAME, {"phase": "idle"})
        return 0

    ctx.progress.set(BOT_NAME, {"phase": "idle", "current_sheet": None, "current_row": None})
    return 1
