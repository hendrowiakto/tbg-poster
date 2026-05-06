"""bot_title.py - generate listing title via Gemini visual reasoning.

Module kontrak (dipanggil oleh main.py via orchestrator):
    BOT_NAME = "title"
    def run_one_cycle(ctx) -> int:
        # 1 cycle: scan LINK!F -> proses max 1 row PERLU TITLE.
        # Return 1 kalau ada row diproses (sukses ATAU gagal-tertulis-error),
        # 0 kalau idle / toggle OFF / stop_event set / Sheets error.

Flow:
1. Scan LINK!F (counter PERLU TITLE per tab) -> pick top-1 tab aktif.
2. Scan AL51:AL di tab terpilih -> cari row pertama dengan "PERLU TITLE".
3. Batch_get A{n} + I{n} + AF2:AF15 (1 call) untuk konteks prompt.
4. Lock J{n} = "! ON WORKING !".
5. Download max 20 gambar dari kolom I (timeout 60s).
6. Load prompt_title.txt (hot-reload tiap cycle) + substitusi placeholder
   [sheets-AF2/3/4/5:14/15] + [sheets-A51].
7. Kirim prompt + images ke Gemini (model gemini-2.5-flash-lite, multimodal).
8. Tulis hasil Gemini ke J{n}. Kalau gagal -> tulis "❌ <error>" ke J{n}.

Total cap per row: 120s (download 60s + Gemini 60s, sheet writes margin).

Stats key: nama tab game (bukan platform/market). 1 worker max (sequential).
"""

import os
import sys
import time
import threading

import google.generativeai as genai
from PIL import Image

from shared import call_with_timeout, TimeoutHangError

# Re-use image scraper + downloader dari create/_shared.py. inject_runtime
# di-call di _bind_ctx supaya log download muncul ke level [TITLE], bukan
# warisan binding terakhir dari bot_create.
from create import _shared as _create_shared
from create._shared import (
    download_images_with_urls,
    cleanup_temp_images,
)


if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_prompt_file(filename):
    """Resolve prompt file path. Order:
    1. Next-to-exe / dev folder (user-editable, hot-reload).
    2. _MEIPASS bundle (default fallback kalau user hapus file).
    """
    primary = os.path.join(SCRIPT_DIR, filename)
    if os.path.exists(primary):
        return primary
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = os.path.join(meipass, filename)
        if os.path.exists(cand):
            return cand
    return primary  # error path - akan fail di open() dengan FileNotFoundError yg jelas


def _resolve_prompt_path():
    return _resolve_prompt_file("prompt_title.txt")


def _resolve_trim_prompt_path():
    return _resolve_prompt_file("prompt_title_trim.txt")


# ===================== KONSTANTA =====================
TEMP_IMG_DIR = os.path.join(SCRIPT_DIR, "temp_images")
WORKER_TEMP_DIR = os.path.join(TEMP_IMG_DIR, "title-1")

LINK_SHEET_NAME  = "LINK"
LINK_COL         = 1     # A - tab name
LINK_COUNTER_COL = 6     # F - counter PERLU TITLE
LINK_START_ROW   = 2

KOLOM_KODE     = 1   # A  - kode listing (= MANDATORY_SUFFIX di prompt)
KOLOM_GAMBAR   = 9   # I  - URL album gambar
KOLOM_OUTPUT   = 10  # J  - hasil Gemini ditulis di sini
KOLOM_TRIGGER  = 38  # AL - trigger PERLU TITLE
BARIS_MULAI    = 51

LOCK_TEXT     = "ON WORKING !"
TRIGGER_TEXT  = "PERLU TITLE"

# bot_title pakai gemini-2.5-flash (full, thinking mode ON) untuk visual reasoning
# yang lebih akurat. bot_create tetap di gemini-2.5-flash-lite (form-filling
# sederhana, ndak butuh thinking).
# Konsekuensi: latency Gemini call 30-120s (vs flash-lite 1-3s) karena thinking
# mode. Timeout di-bump biar muat dalam budget.
GEMINI_MODEL_NAME = "gemini-2.5-flash"
MAX_IMAGES        = 20
DOWNLOAD_TIMEOUT  = 60
GEMINI_TIMEOUT    = 180     # was 150; flash thinking butuh lebih lama
ROW_TIMEOUT       = 270     # total: download 60 + main 180 + trim retries + writes

# Char-limit enforce (post-Gemini): split hasil per line, retry trim line yang
# out-of-bounds lewat Gemini text-only call (hemat token, ndak re-send images).
# Bounds: CHAR_LIMIT_LOWER <= len <= AF3.
# - Lebih panjang dari AF3      -> retry trim pakai current text (incremental shrink)
# - Lebih pendek dari LOWER     -> retry trim pakai ORIGINAL pre-trim text
#                                  (current text terlalu compressed, ndak ada
#                                  material buat di-expand)
# - Setelah TRIM_MAX_RETRIES masih out-of-bounds -> prefix "❌ " ke line tsb
#   sebelum tulis ke J cell (visual indicator buat user, trigger AL tetap off
#   karena J non-empty).
CHAR_LIMIT_DEFAULT = 150
CHAR_LIMIT_MIN     = 50
CHAR_LIMIT_MAX     = 500
CHAR_LIMIT_LOWER   = 100
TRIM_MAX_RETRIES   = 3
TRIM_TIMEOUT       = 30


# ===================== CTX BINDING =====================
_ctx               = None
spreadsheet_client = None
gemini_model       = None

# Worker status untuk UI (1 worker max, mirror bot_create/bot_delete pattern).
worker_status      = {}
worker_status_lock = threading.Lock()


def _bind_ctx(ctx):
    global _ctx, spreadsheet_client, gemini_model
    _ctx               = ctx
    spreadsheet_client = ctx.sheets.spreadsheet

    if gemini_model is None:
        api_key = ctx.config.get("GEMINI_API_KEY", "")
        if api_key:
            try:
                genai.configure(api_key=api_key)
                gemini_model = genai.GenerativeModel(GEMINI_MODEL_NAME)
            except Exception as e:
                add_log(f"Gagal init Gemini model: {str(e)[:120]}")

    # Re-bind runtime deps di create/_shared.py supaya download_images_with_urls
    # log ke level [TITLE]. Bot_create akan re-inject ulang saat dia jalan, jadi
    # ndak ada konflik (orchestrator sequential, tidak paralel antar bot).
    _create_shared.inject_runtime(
        log=add_log,
        worker_temp_dir=lambda: WORKER_TEMP_DIR,
        prepare_worker_temp_dir=_prepare_worker_temp_dir,
        gemini_model=lambda: gemini_model,
    )


def _prepare_worker_temp_dir():
    """Bikin + bersihin temp dir tiap cycle. Dipanggil download_images_with_urls."""
    try:
        os.makedirs(WORKER_TEMP_DIR, exist_ok=True)
        for fname in os.listdir(WORKER_TEMP_DIR):
            fpath = os.path.join(WORKER_TEMP_DIR, fname)
            try:
                if os.path.isfile(fpath):
                    os.remove(fpath)
            except Exception:
                pass
    except Exception:
        pass
    return WORKER_TEMP_DIR


# ===================== THIN WRAPPERS =====================
def add_log(msg):
    if _ctx is not None:
        _ctx.logger.log("title", msg)
    else:
        try:
            print(f"[TITLE] {msg}")
        except Exception:
            pass


def update_stats(game_name, success):
    """Stats key = nama tab game (bukan platform/market) sesuai spec."""
    if _ctx is not None and game_name:
        _ctx.stats.update("title", game_name, success)


def set_processing(info):
    if _ctx is None:
        return
    if info is None:
        _ctx.progress.set("title", {
            "phase": "idle",
            "current_sheet": None,
            "current_row": None,
        })
    else:
        _ctx.progress.set("title", {
            "phase": "processing",
            "current_sheet": info.get("sheet_name"),
            "current_row": info.get("row"),
        })


def safe_update_cell(sheet, row, col, value, desc=""):
    return _ctx.sheets.safe_update_cell(sheet, row, col, value, desc=desc)


# ===================== SHEET SCANNER =====================
_prefetched_active_sheets = None


def set_prefetched_active_sheets(names):
    """Inject hasil prescan LINK dari orchestrator. One-shot."""
    global _prefetched_active_sheets
    _prefetched_active_sheets = list(names) if names is not None else None


def get_active_sheet_names():
    """Tab dengan LINK!F > 0. Return list (urutan sesuai LINK sheet).
    Pakai prefetch dari orchestrator kalau ada; fallback batch_get sendiri.
    """
    global _prefetched_active_sheets
    if _prefetched_active_sheets is not None:
        cached = _prefetched_active_sheets
        _prefetched_active_sheets = None
        if cached:
            add_log(f"LINK!F (prescan): {len(cached)} tab aktif")
        return cached
    try:
        response = spreadsheet_client.values_batch_get(
            ranges=[
                f"'{LINK_SHEET_NAME}'!A{LINK_START_ROW}:A",
                f"'{LINK_SHEET_NAME}'!F{LINK_START_ROW}:F",
            ]
        )
    except Exception as e:
        add_log(f"Gagal read LINK!A+F: {str(e)[:200]}")
        return []
    vranges = response.get("valueRanges", []) or []
    col_a = vranges[0].get("values", []) if len(vranges) >= 1 else []
    col_f = vranges[1].get("values", []) if len(vranges) >= 2 else []

    active = []
    total_pending = 0
    for i, row in enumerate(col_a):
        name = (row[0] if row else "").strip()
        if not name:
            continue
        count_str = ""
        if i < len(col_f) and col_f[i]:
            count_str = str(col_f[i][0]).strip()
        try:
            count = int(float(count_str)) if count_str else 0
        except (ValueError, TypeError):
            count = 0
        if count > 0:
            active.append(name)
            total_pending += count
    if active:
        add_log(f"LINK!F counter: {len(active)} tab aktif, total {total_pending} PERLU TITLE")
    return active


def _find_first_trigger_row(tab_name):
    """Scan AL51:AL untuk cari row pertama dengan TRIGGER_TEXT.
    Return baris_nomor (int >= 51) atau None.
    """
    try:
        rng = f"'{tab_name}'!AL{BARIS_MULAI}:AL"
        resp = spreadsheet_client.values_batch_get(ranges=[rng])
        vranges = resp.get("valueRanges", []) or []
        values = vranges[0].get("values", []) if vranges else []
    except Exception as e:
        add_log(f"Gagal read AL{BARIS_MULAI}:AL '{tab_name}': {str(e)[:150]}")
        return None
    for i, row in enumerate(values):
        val = (row[0] if row else "").strip()
        if val == TRIGGER_TEXT:
            return BARIS_MULAI + i
    return None


def _read_row_context(tab_name, baris):
    """1 batch_get (3 ranges): A{n} + I{n} + AF2:AF15. Return dict atau None.

    Mapping placeholder prompt:
      [sheets-A51]      -> A{n}                  (kode listing, MANDATORY_SUFFIX)
      [sheets-AF2]      -> AF2                   (GAME_NAME)
      [sheets-AF3]      -> AF3                   (CHAR_LIMIT)
      [sheets-AF4]      -> AF4                   (TARGET_VARIANTS)
      [sheets-AF5:AF14] -> AF5..AF14 join \\n    (REFERENCE_TITLES)
      [sheets-AF15]     -> AF15                  (METADATA_POOL JSON)
    """
    try:
        ranges = [
            f"'{tab_name}'!A{baris}",
            f"'{tab_name}'!I{baris}",
            f"'{tab_name}'!AF2:AF15",
        ]
        resp = spreadsheet_client.values_batch_get(ranges=ranges)
        vranges = resp.get("valueRanges", []) or []
    except Exception as e:
        add_log(f"Gagal batch_get context '{tab_name}' baris {baris}: {str(e)[:150]}")
        return None

    def _single(idx):
        try:
            return (vranges[idx].get("values", []) or [[""]])[0][0]
        except (IndexError, TypeError):
            return ""

    kode       = str(_single(0)).strip()
    gambar_url = str(_single(1)).strip()

    af_values = vranges[2].get("values", []) if len(vranges) >= 3 else []

    def _af(rownum):
        # AF2 -> idx 0, AF3 -> idx 1, ..., AF15 -> idx 13
        idx = rownum - 2
        try:
            row = af_values[idx]
            return str(row[0] if row else "").strip()
        except IndexError:
            return ""

    af2  = _af(2)
    af3  = _af(3)
    af4  = _af(4)
    af15 = _af(15)
    af5_14 = []
    for n in range(5, 15):
        v = _af(n)
        if v:
            af5_14.append(v)

    return {
        "kode": kode,
        "gambar_url": gambar_url,
        "af2": af2,
        "af3": af3,
        "af4": af4,
        "af5_14": af5_14,
        "af15": af15,
    }


def _load_prompt_template():
    """Load prompt_title.txt setiap call (hot-reload, user bisa edit on-the-fly).
    Resolve path: next-to-exe (user override) -> _MEIPASS bundle (default).
    Return string atau None kalau file missing/error.
    """
    path = _resolve_prompt_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        add_log(f"prompt_title.txt missing di {path}")
        return None
    except Exception as e:
        add_log(f"Gagal load prompt_title.txt: {str(e)[:150]}")
        return None


def _build_prompt(template, data):
    """Substitusi placeholder [sheets-XXX] dengan nilai dari sheet.

    [sheets-A51:A] = kolom A row yg sedang diproses (kode listing dynamic).
                     Notasi 'A51:A' = column-A range dari row 51, jadi value-nya
                     = A{baris_yang_sedang_diproses}, bukan literal cell A51.
    [sheets-A51]   = legacy alias, di-handle juga biar prompt versi lama tetap
                     jalan kalau user revert.
    """
    af5_14_joined = "\n".join(data["af5_14"]) if data["af5_14"] else ""
    return (
        template
        .replace("[sheets-AF2]", data["af2"])
        .replace("[sheets-AF3]", data["af3"])
        .replace("[sheets-AF4]", data["af4"])
        .replace("[sheets-AF5:AF14]", af5_14_joined)
        .replace("[sheets-AF15]", data["af15"])
        .replace("[sheets-A51:A]", data["kode"])
        .replace("[sheets-A51]",   data["kode"])
    )


def _call_gemini_with_images(prompt, image_paths):
    """Multimodal call: kirim prompt + N images ke Gemini.
    Return response.text (stripped). Raise RuntimeError kalau gagal.
    """
    if gemini_model is None:
        raise RuntimeError("Gemini model belum siap (cek GEMINI_API_KEY)")

    images = []
    for p in image_paths:
        try:
            img = Image.open(p)
            img.load()  # force decode supaya file bisa dihapus setelahnya
            images.append(img)
        except Exception as e:
            add_log(f"Skip image {os.path.basename(p)}: {str(e)[:80]}")
            continue

    if not images:
        raise RuntimeError("Tidak ada image valid setelah decode")

    add_log(f"Kirim ke Gemini: prompt {len(prompt)} char + {len(images)} images")
    payload = [prompt] + images

    response = gemini_model.generate_content(payload)
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini response kosong")
    return text


# ===================== CHAR-LIMIT ENFORCE =====================
def _resolve_char_limit(af3_value):
    """Parse AF3 ke int. Fallback CHAR_LIMIT_DEFAULT kalau invalid / out of bounds."""
    try:
        n = int(float(str(af3_value).strip()))
    except (ValueError, TypeError):
        return CHAR_LIMIT_DEFAULT
    if n < CHAR_LIMIT_MIN or n > CHAR_LIMIT_MAX:
        return CHAR_LIMIT_DEFAULT
    return n


def _parse_titles(text):
    """Split Gemini response jadi list non-empty title lines."""
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _split_titles_by_bounds(titles, lower, upper):
    """Return (good_idx, over_items, under_items).

    good_idx     : list of int (idx yg lower <= len <= upper).
    over_items   : list of (idx, text, length) yg len > upper.
    under_items  : list of (idx, text, length) yg len < lower.

    Safety: kalau lower >= upper (config edge case), disable lower-bound check
    (anything <= upper jadi good). Cegah loop forever di config ekstrim.
    """
    if lower >= upper:
        lower = 0
    good = []
    over = []
    under = []
    for i, t in enumerate(titles):
        ln = len(t)
        if ln > upper:
            over.append((i, t, ln))
        elif ln < lower:
            under.append((i, t, ln))
        else:
            good.append(i)
    return good, over, under


def _load_trim_prompt_template():
    """Load prompt_title_trim.txt setiap call (hot-reload, user bisa edit
    on-the-fly). Resolve path: next-to-exe -> _MEIPASS bundle.
    Return string atau None kalau file missing/error.
    """
    path = _resolve_trim_prompt_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        add_log(f"prompt_title_trim.txt missing di {path}")
        return None
    except Exception as e:
        add_log(f"Gagal load prompt_title_trim.txt: {str(e)[:150]}")
        return None


def _build_trim_prompt(template, bad_items, char_limit, kode, game_name):
    """Substitusi placeholder di template prompt trim:
      {{hasil_bot_jika_terdapat_title_lebih_dariAF3}} -> RAW_TITLES block
                                                          (1 line per judul over,
                                                          tanpa [N chars] label)
      [sheets-AF3]   -> int limit (char limit)
      [sheets-A51:A] -> kode listing (mandatory suffix dynamic)
      [sheets-AF2]   -> game name
    """
    raw_block = "\n".join(t for _, t, _ in bad_items)
    return (
        template
        .replace("{{hasil_bot_jika_terdapat_title_lebih_dariAF3}}", raw_block)
        .replace("[sheets-AF3]",   str(char_limit))
        .replace("[sheets-A51:A]", str(kode))
        .replace("[sheets-AF2]",   str(game_name))
    )


def _call_gemini_trim(bad_items, char_limit, kode, game_name):
    """Text-only Gemini call: kirim N line over-limit + minta trim ke <= limit.
    Pakai template di prompt_title_trim.txt (user-editable, hot-reload).
    Return list trimmed lines (sequence-aligned dgn input bad_items).
    """
    if gemini_model is None:
        raise RuntimeError("Gemini model belum siap")

    template = _load_trim_prompt_template()
    if template is None:
        raise RuntimeError("prompt_title_trim.txt missing")

    trim_prompt = _build_trim_prompt(template, bad_items, char_limit, kode, game_name)
    response = gemini_model.generate_content(trim_prompt)
    text = (response.text or "").strip()
    return _parse_titles(text)


def _enforce_char_limit(initial_text, char_limit, kode, game_name):
    """Loop: parse -> validate bounds [CHAR_LIMIT_LOWER, char_limit] -> retry
    via Gemini text call. Max TRIM_MAX_RETRIES putaran.

    Retry input strategy:
      - OVER  (len > char_limit): kirim CURRENT text (incremental shrink).
      - UNDER (len < CHAR_LIMIT_LOWER): kirim ORIGINAL pre-trim text
        (current udah terlalu compressed, ndak ada material buat di-expand;
        original adalah snapshot Gemini main-call output).

    Final: line yg masih out-of-bounds setelah max retries di-prefix "❌ "
    di output (visual indicator, J cell tetap terisi -> trigger AL off).

    Return (final_text_joined, stats_dict).
    """
    titles = _parse_titles(initial_text)
    original = list(titles)  # snapshot for UNDER retry input
    total = len(titles)

    _, over_init, under_init = _split_titles_by_bounds(
        titles, CHAR_LIMIT_LOWER, char_limit
    )
    over_init_count = len(over_init)
    under_init_count = len(under_init)
    retries = 0

    while retries < TRIM_MAX_RETRIES:
        _, over_items, under_items = _split_titles_by_bounds(
            titles, CHAR_LIMIT_LOWER, char_limit
        )
        if not over_items and not under_items:
            break  # all in bounds

        # Build retry payload:
        # OVER  -> kirim current text (yg sudah di-trim sebelumnya, lanjut shrink)
        # UNDER -> kirim ORIGINAL pre-trim text (re-do dari Gemini initial output)
        retry_inputs = []
        for idx, t, ln in over_items:
            retry_inputs.append((idx, t, ln))
        for idx, _, _ in under_items:
            orig = original[idx]
            retry_inputs.append((idx, orig, len(orig)))
        # Sort by idx supaya output Gemini line-aligned dgn bad index urutan.
        retry_inputs.sort(key=lambda x: x[0])

        bounds_msg = []
        if over_items:
            over_lens = ', '.join(str(ln) for _, _, ln in over_items)
            bounds_msg.append(f"{len(over_items)} over {char_limit} ({over_lens})")
        if under_items:
            under_lens = ', '.join(str(ln) for _, _, ln in under_items)
            bounds_msg.append(f"{len(under_items)} under {CHAR_LIMIT_LOWER} ({under_lens})")
        add_log(
            f"Trim retry {retries+1}/{TRIM_MAX_RETRIES}: " + "; ".join(bounds_msg)
        )

        try:
            trimmed = call_with_timeout(
                _call_gemini_trim,
                args=(retry_inputs, char_limit, kode, game_name),
                timeout=TRIM_TIMEOUT, name="gemini_trim",
            )
        except TimeoutHangError:
            add_log(f"Trim retry timeout >{TRIM_TIMEOUT}s, abort retry loop")
            break
        except Exception as e:
            add_log(f"Trim retry error: {str(e)[:120]}")
            break

        if not trimmed:
            add_log("Trim retry balikin kosong, abort retry loop")
            break

        # Replace bad lines dgn trimmed (positional, sequence-aligned dgn input).
        # Kalau Gemini balikin lebih sedikit, sisa bad lines tetap apa adanya
        # (akan di-validate ulang iterasi berikutnya).
        new_titles = list(titles)
        for j, (idx, _, _) in enumerate(retry_inputs):
            if j < len(trimmed):
                new_titles[idx] = trimmed[j]
        titles = new_titles
        retries += 1

    # Final validation
    _, over_final, under_final = _split_titles_by_bounds(
        titles, CHAR_LIMIT_LOWER, char_limit
    )
    final_bad_idx = set()
    for idx, _, _ in over_final:
        final_bad_idx.add(idx)
    for idx, _, _ in under_final:
        final_bad_idx.add(idx)

    # Build output: prefix "❌ " ke line yg masih out-of-bounds
    output_lines = []
    for i, t in enumerate(titles):
        if i in final_bad_idx:
            output_lines.append(f"❌ {t}")
        else:
            output_lines.append(t)

    stats = {
        "total":       total,
        "over_init":   over_init_count,
        "under_init":  under_init_count,
        "retries":     retries,
        "still_over":  len(over_final),
        "still_under": len(under_final),
        "final_bad":   len(final_bad_idx),
        "lengths":     [len(t) for t in titles],
    }
    return "\n".join(output_lines), stats


# ===================== MAIN ENTRY =====================
def run_one_cycle(ctx):
    """1 cycle: scan LINK!F -> proses max 1 row PERLU TITLE.

    Return:
      1 -> ada row diproses (sukses ATAU gagal-with-error-tertulis ke J).
      0 -> idle / toggle OFF / stop_event / Sheets error / no trigger ditemukan.
    """
    if ctx.stop_event.is_set():
        return 0

    _bind_ctx(ctx)

    if not _ctx.toggles.should_keep_running("title"):
        return 0

    if spreadsheet_client is None:
        add_log("Sheets belum connect, skip cycle")
        return 0

    sheet_names = get_active_sheet_names()
    if not sheet_names:
        return 0

    tab_name = sheet_names[0]  # top-1
    add_log(f"Scan tab '{tab_name}'...")

    try:
        sheet = spreadsheet_client.worksheet(tab_name)
    except Exception as e:
        add_log(f"Gagal akses tab '{tab_name}': {str(e)[:150]}")
        return 0

    baris = _find_first_trigger_row(tab_name)
    if baris is None:
        add_log(f"Tab '{tab_name}': counter LINK!F > 0 tapi PERLU TITLE tidak ditemukan (formula stale)")
        return 0

    add_log(f"Ketemu PERLU TITLE: sheet='{tab_name}' baris={baris}")

    data = _read_row_context(tab_name, baris)
    if data is None:
        return 0

    if not data["gambar_url"]:
        add_log(f"Baris {baris}: kolom I (URL gambar) kosong, skip")
        return 0
    if not data["af2"]:
        add_log(f"Baris {baris}: AF2 (GAME_NAME) kosong, skip")
        return 0

    set_processing({"sheet_name": tab_name, "row": baris})
    with worker_status_lock:
        worker_status[1] = {
            "text": f"{tab_name} | row {baris}",
            "url": data["gambar_url"],
            "waiting": False,
        }

    # ===== STEP 1: Lock J{n} =====
    try:
        safe_update_cell(sheet, baris, KOLOM_OUTPUT, LOCK_TEXT, desc=f"lock_J{baris}")
        add_log(f"Lock J{baris} = '{LOCK_TEXT}'")
    except Exception as e:
        add_log(f"Gagal lock J{baris}: {str(e)[:150]} (lanjut tanpa lock)")

    image_paths = []
    err_msg     = None
    result_text = None
    t_start     = time.time()

    try:
        # ===== STEP 2: Download images =====
        def _do_download():
            return download_images_with_urls(data["gambar_url"], MAX_IMAGES)

        try:
            paths, _, _ = call_with_timeout(
                _do_download, timeout=DOWNLOAD_TIMEOUT, name="download_images"
            )
        except TimeoutHangError:
            raise RuntimeError(f"Download images timeout (>{DOWNLOAD_TIMEOUT}s)")
        except Exception as e:
            raise RuntimeError(f"Download gagal: {str(e)[:120]}")

        if not paths:
            raise RuntimeError("Gambar tidak bisa di download")

        image_paths = paths
        add_log(f"Download selesai: {len(paths)} images")

        # ===== STEP 3: Load + build prompt =====
        template = _load_prompt_template()
        if template is None:
            raise RuntimeError("prompt_title.txt missing")
        prompt = _build_prompt(template, data)

        # ===== STEP 4: Gemini call (cap by remaining budget) =====
        elapsed = time.time() - t_start
        remaining = ROW_TIMEOUT - elapsed
        if remaining < 10:
            raise RuntimeError(f"Sisa waktu {remaining:.1f}s < 10s, abort sebelum Gemini")
        gemini_cap = min(GEMINI_TIMEOUT, max(10, int(remaining)))

        try:
            result_text = call_with_timeout(
                _call_gemini_with_images,
                args=(prompt, image_paths),
                timeout=gemini_cap, name="gemini_call",
            )
        except TimeoutHangError:
            raise RuntimeError(f"Gemini timeout (>{gemini_cap}s)")
        except Exception as e:
            raise RuntimeError(f"Gemini error: {str(e)[:120]}")

        if not result_text:
            raise RuntimeError("Gemini response kosong")

        # ===== STEP 4.5: Enforce char bounds [LOWER..AF3] per judul =====
        # Split per line, retry via Gemini text-only call (hemat, ndak re-send images).
        # OVER -> retry pakai current text. UNDER -> retry pakai original pre-trim.
        char_limit = _resolve_char_limit(data["af3"])
        result_text, trim_stats = _enforce_char_limit(
            result_text, char_limit, data["kode"], data["af2"]
        )
        if trim_stats["over_init"] > 0 or trim_stats["under_init"] > 0:
            lengths = trim_stats["lengths"]
            len_summary = (
                f"{min(lengths)}-{max(lengths)}" if lengths else "0"
            )
            add_log(
                f"Char bounds [{CHAR_LIMIT_LOWER}-{char_limit}]: "
                f"{trim_stats['total']} judul, init over={trim_stats['over_init']} "
                f"under={trim_stats['under_init']} -> {trim_stats['retries']} retry, "
                f"sisa over={trim_stats['still_over']} under={trim_stats['still_under']}, "
                f"len final: {len_summary}"
            )
            if trim_stats["final_bad"] > 0:
                add_log(
                    f"⚠ {trim_stats['final_bad']} judul masih out-of-bounds "
                    f"setelah {trim_stats['retries']} retry → prefix ❌ di J cell"
                )

    except Exception as e:
        err_msg = str(e)[:200]

    # ===== STEP 5: Tulis hasil ke J{n} =====
    elapsed_total = time.time() - t_start
    if err_msg:
        final_text = f"❌ {err_msg}"
        success    = False
        add_log(f"❌ [{tab_name}] {data['kode']} | {err_msg} ({elapsed_total:.1f}s)")
    else:
        final_text = result_text
        success    = True

    try:
        safe_update_cell(sheet, baris, KOLOM_OUTPUT, final_text, desc=f"write_J{baris}")
    except Exception as e:
        add_log(f"Gagal write J{baris}: {str(e)[:150]}")

    update_stats(tab_name, success=success)

    try:
        cleanup_temp_images(image_paths)
    except Exception:
        pass

    if success:
        n_lines = len([ln for ln in (result_text or "").splitlines() if ln.strip()])
        add_log(f"✅ [{tab_name}] {data['kode']} berhasil generate {n_lines} title variants ({elapsed_total:.1f}s)")

    set_processing(None)
    with worker_status_lock:
        worker_status.clear()

    return 1
