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
import re
import sys
import json
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
    """Resolve prompt file path. Selalu next-to-exe (atau dev folder).

    Prompt sekarang TIDAK di-bundle ke EXE — production team boleh edit
    prompt_title_template.txt langsung di samping EXE. Kalau file hilang,
    bot auto-create dari DEFAULT_TITLE_PROMPT yg di-embed (lihat
    _ensure_prompt_file_exists).
    """
    return os.path.join(SCRIPT_DIR, filename)


def _resolve_template_prompt_path():
    return _resolve_prompt_file("prompt_title_template.txt")


# Default prompt di-embed dari prompt_title_template.txt via tools/embed_prompt.py.
# Dipakai sebagai fallback untuk auto-create file kalau team hapus / belum punya.
try:
    from _prompt_defaults import DEFAULT_TITLE_PROMPT as _DEFAULT_TITLE_PROMPT
except ImportError:
    _DEFAULT_TITLE_PROMPT = None


def _ensure_prompt_file_exists():
    """Kalau prompt_title_template.txt belum ada di samping EXE/script,
    tulis default-nya dari embedded constant. Idempotent — kalau sudah ada,
    JANGAN overwrite (preserve edit user)."""
    path = _resolve_template_prompt_path()
    if os.path.exists(path):
        return  # user-edited file ada, jangan disentuh
    if not _DEFAULT_TITLE_PROMPT:
        return  # dev mode tanpa embed artifact + file ndak ada = ignore
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(_DEFAULT_TITLE_PROMPT)
        # Tidak pakai add_log karena _ctx mungkin belum bound.
        print(f"[title] auto-created {path} (team boleh edit file ini)")
    except Exception as e:
        print(f"[title] gagal auto-create prompt file: {str(e)[:150]}")


# Auto-create di module-import time supaya file muncul begitu bot launch,
# TANPA harus nunggu PERLU TITLE pertama. Aman karena idempotent.
_ensure_prompt_file_exists()


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

# Char-limit enforce (Python-side, post-assembly):
# - Bounds: CHAR_LIMIT_LOWER <= assembled_len <= AF3.
# - Lebih panjang dari AF3 -> Python auto-trim ISI ke word-boundary, re-assemble.
# - Lebih pendek dari CHAR_LIMIT_LOWER -> prefix "❌ " ke title (rare karena
#   ISI budget di-prompt minimum).
# Tidak ada Gemini trim retry — Python deterministic trim cukup karena struktur
# fixed (token mechanical). Drift cuma di ISI length, fixable via word-boundary cut.
CHAR_LIMIT_DEFAULT = 150
CHAR_LIMIT_MIN     = 50
CHAR_LIMIT_MAX     = 500
CHAR_LIMIT_LOWER   = 100


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

    # First-run: auto-create prompt_title_template.txt next to EXE kalau team
    # belum punya. Team boleh edit setelahnya, bot ndak akan overwrite.
    _ensure_prompt_file_exists()

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
    """1 batch_get (4 ranges): A{n} + I{n} + AF2:AF26 + O45. Return dict atau None.

    Sheet schema v3.2 (updated 2026-05-15):
      A{n}              -> kode listing (MANDATORY_SUFFIX, dynamic per row)
      I{n}              -> URL gambar (kolom I)
      AF2               -> GAME_NAME
      AF3               -> CHAR_LIMIT
      AF4               -> TARGET_VARIANTS
      AF5               -> TITLE_TEMPLATE
      AF6..AF25         -> REFERENCE_TITLES, 20 slots
      AF26              -> FOCUS_PROMPT (opsional, user-defined ISI emphasis bias)
      O45               -> METADATA_POOL JSON

    Return keys:
      kode, gambar_url, af2, af3, af4,
      template (AF5), references (list AF6..AF25 non-empty),
      focus_prompt (AF26 raw, optional), metadata_pool (O45 raw)
    """
    try:
        ranges = [
            f"'{tab_name}'!A{baris}",
            f"'{tab_name}'!I{baris}",
            f"'{tab_name}'!AF2:AF26",
            f"'{tab_name}'!O45",
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
        # AF2 -> idx 0, AF3 -> idx 1, ..., AF26 -> idx 24
        idx = rownum - 2
        try:
            row = af_values[idx]
            return str(row[0] if row else "").strip()
        except IndexError:
            return ""

    af2 = _af(2)
    af3 = _af(3)
    af4 = _af(4)
    template = _af(5)                              # AF5 = title template
    references = []
    for n in range(6, 26):                         # AF6..AF25 = 20 reference slots
        v = _af(n)
        if v:
            references.append(v)
    focus_prompt = _af(26)                         # AF26 = optional ISI focus instruction
    metadata_pool = str(_single(3)).strip()        # O45 = metadata JSON

    return {
        "kode": kode,
        "gambar_url": gambar_url,
        "af2": af2,
        "af3": af3,
        "af4": af4,
        "template": template,
        "references": references,
        "focus_prompt": focus_prompt,
        "metadata_pool": metadata_pool,
    }


def _load_prompt_template():
    """Load prompt_title_template.txt setiap call (hot-reload).
    Resolve path: next-to-exe (user override) -> _MEIPASS bundle (default).
    Return string atau None kalau file missing/error.

    Hanya ada SATU mode sekarang: template mode. Bot menolak proses row kalau
    AF16 (template) kosong — free-form dihapus karena structural drift terlalu
    tinggi. Python yang assemble title dari tokens, AI cuma extract data.
    """
    path = _resolve_template_prompt_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        add_log(f"prompt_title_template.txt missing di {path}")
        return None
    except Exception as e:
        add_log(f"Gagal load prompt_title_template.txt: {str(e)[:150]}")
        return None


def _build_prompt(template, data, lookup_spec, isi_min, isi_max, variants_count):
    """Substitusi semua placeholder di prompt template.

    Static placeholders (dari sheet, v3.2 schema):
      [sheets-AF2]       -> GAME_NAME
      [sheets-AF3]       -> CHAR_LIMIT (full title)
      [sheets-AF4]       -> TARGET_VARIANTS (legacy, sekarang pakai [VARIANTS_REQUESTED])
      [sheets-AF6:AF25]  -> REFERENCE_TITLES joined newline (20 slots)
      [sheets-AF26]      -> FOCUS_PROMPT (optional ISI emphasis instruction)
      [sheets-O45]       -> METADATA_POOL raw JSON string

    Listing code & template are NOT exposed to AI (Python handles both at assembly).

    Dynamic placeholders (computed per row):
      [VARIANTS_REQUESTED]         -> int, harus exact
      [ISI_MIN_CHARS]              -> int, lower bound ISI
      [ISI_MAX_CHARS]              -> int, upper bound ISI
      [LOOKUP_KEYS_SPEC]           -> human-readable spec semua lookup keys
      [LOOKUP_KEYS_JSON_TEMPLATE]  -> JSON skeleton lines untuk output_format
    """
    refs_joined = "\n".join(data["references"]) if data["references"] else "(empty)"
    focus = (data.get("focus_prompt") or "").strip()
    focus_value = focus if focus else "(none — apply default standout prioritization)"
    return (
        template
        .replace("[sheets-AF2]", data["af2"])
        .replace("[sheets-AF3]", data["af3"])
        .replace("[sheets-AF4]", str(variants_count))
        .replace("[sheets-AF6:AF25]", refs_joined)
        .replace("[sheets-AF26]", focus_value)
        .replace("[sheets-O45]", data["metadata_pool"])
        .replace("[VARIANTS_REQUESTED]", str(variants_count))
        .replace("[ISI_MIN_CHARS]", str(isi_min))
        .replace("[ISI_MAX_CHARS]", str(isi_max))
        .replace("[LOOKUP_KEYS_SPEC]", _build_lookup_keys_spec_text(lookup_spec))
        .replace("[LOOKUP_KEYS_JSON_TEMPLATE]", _build_lookup_keys_json_template(lookup_spec))
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


# ===================== TEMPLATE PARSER + ASSEMBLER =====================
# Architecture: AF16 template di-parse jadi tokens. Gemini cuma diminta extract
# lookup values + compose "isi" body (JSON output). Python ASSEMBLE final title
# secara deterministic — zero structure drift.

_TOKEN_RE = re.compile(r'\{([^}]*)\}')


def _parse_template(af16):
    """Parse AF16 template string jadi list of (type, content) tokens.

    Token types:
      ('literal', text) -> raw chars di luar {} ATAU {"text"} content
      ('isi', None)     -> {ISI} slot, di-fill AI body
      ('kode', None)    -> {KODE} slot, di-fill kode listing
      ('lookup', key)   -> {SomeKey} slot, di-fill via METADATA_POOL lookup

    Contoh: '{Server}|{ISI}|TakeMail({KODE})' →
      [('lookup','Server'), ('literal','|'), ('isi',None),
       ('literal','|TakeMail('), ('kode',None), ('literal',')')]
    """
    tokens = []
    last_end = 0
    for m in _TOKEN_RE.finditer(af16):
        if m.start() > last_end:
            tokens.append(("literal", af16[last_end:m.start()]))
        inner = m.group(1)
        if len(inner) >= 2 and inner.startswith('"') and inner.endswith('"'):
            tokens.append(("literal", inner[1:-1]))
        elif inner == "ISI":
            tokens.append(("isi", None))
        elif inner == "KODE":
            tokens.append(("kode", None))
        else:
            tokens.append(("lookup", inner))
        last_end = m.end()
    if last_end < len(af16):
        tokens.append(("literal", af16[last_end:]))
    return tokens


def _extract_lookup_keys(tokens):
    """Return list of unique lookup keys di template (case preserved, urutan first-seen)."""
    seen = set()
    result = []
    for type_, content in tokens:
        if type_ == "lookup" and content.lower() not in seen:
            seen.add(content.lower())
            result.append(content)
    return result


def _parse_metadata_pool(af15):
    """Parse AF15 JSON string. Return dict (empty kalau kosong) atau None on error."""
    s = (af15 or "").strip()
    if not s:
        return {}
    try:
        data = json.loads(s)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _build_lookup_spec(tokens, metadata_pool):
    """Bangun list lookup spec dari template + METADATA_POOL.
    Return list of dicts: [{"key", "key_lower", "options"}, ...]
    """
    keys = _extract_lookup_keys(tokens)
    result = []
    for k in keys:
        options = []
        if isinstance(metadata_pool, dict):
            for pool_key, pool_val in metadata_pool.items():
                if pool_key.lower() == k.lower() and isinstance(pool_val, list):
                    options = [str(v) for v in pool_val]
                    break
        result.append({
            "key": k,
            "key_lower": k.lower(),
            "options": options,
        })
    return result


def _calc_isi_budget(tokens, lookup_spec, kode, char_limit, lower=100):
    """Hitung ISI min/max chars supaya FULL assembled title fit [lower, char_limit].

    Untuk lookup tokens, pakai MAX option length (konservatif — biar kalau Gemini
    pilih value terpanjang, title masih fit).
    """
    # Build dict: key_lower -> max option length (or len('Unknown') as fallback)
    lookup_max = {}
    for spec in lookup_spec:
        opts = spec["options"]
        if opts:
            max_len = max(len(o) for o in opts)
        else:
            max_len = len("Unknown")
        # Always at least len("Unknown") in case Gemini falls back to "Unknown"
        lookup_max[spec["key_lower"]] = max(max_len, len("Unknown"))

    fixed_len = 0
    for type_, content in tokens:
        if type_ == "literal":
            fixed_len += len(content)
        elif type_ == "kode":
            fixed_len += len(kode or "")
        elif type_ == "lookup":
            fixed_len += lookup_max.get(content.lower(), len("Unknown"))
        # 'isi' tidak dihitung — itu yang kita budget-kan

    safety = 5
    isi_max = max(20, char_limit - fixed_len - safety)
    isi_min = max(20, lower - fixed_len)
    # Sanity: kalau template literal/lookup udah makan hampir semua char_limit,
    # ISI budget bisa nyentuh atau lewat min. Pastikan max > min.
    if isi_max < isi_min:
        isi_max = isi_min + 20
    return isi_min, isi_max


def _assemble_title(tokens, lookup_values, isi, kode):
    """Build final title string from parsed tokens + provided values.

    lookup_values: dict {key_lower -> value_string}
    isi:           string (AI-generated body)
    kode:          string (listing code)

    Missing lookup key → 'Unknown' fallback.
    """
    parts = []
    for type_, content in tokens:
        if type_ == "literal":
            parts.append(content)
        elif type_ == "isi":
            parts.append(isi or "")
        elif type_ == "kode":
            parts.append(kode or "")
        elif type_ == "lookup":
            parts.append(lookup_values.get(content.lower(), "Unknown"))
    return "".join(parts)


def _build_lookup_keys_spec_text(lookup_spec):
    """Bangun text spec lookup keys untuk prompt (human-readable list)."""
    if not lookup_spec:
        return "(none — template has no dynamic lookup tokens)"
    lines = []
    for spec in lookup_spec:
        opts = spec["options"]
        if opts:
            opts_str = ", ".join(f'"{o}"' for o in opts)
            lines.append(
                f'- "lookup_{spec["key_lower"]}" (template key: "{spec["key"]}")\n'
                f'    Options: [{opts_str}]\n'
                f'    Or "Unknown" if not determinable from screenshots.'
            )
        else:
            lines.append(
                f'- "lookup_{spec["key_lower"]}" (template key: "{spec["key"]}")\n'
                f'    No options found in METADATA_POOL for this key.\n'
                f'    Output "Unknown".'
            )
    return "\n".join(lines)


def _build_lookup_keys_json_template(lookup_spec):
    """Bangun JSON schema sketch untuk lookup keys (untuk prompt example)."""
    if not lookup_spec:
        return ""
    lines = []
    for spec in lookup_spec:
        opts = spec["options"]
        sample = opts[0] if opts else "Unknown"
        lines.append(f'      "lookup_{spec["key_lower"]}": "{sample}",')
    return "\n".join(lines)


def _parse_gemini_json_output(text):
    """Parse Gemini response yang seharusnya JSON.
    Robust: handle markdown code fence, trailing text, leading text.
    Return parsed dict atau None.
    """
    if not text:
        return None
    s = text.strip()
    # Strip markdown fence kalau ada (```json ... ``` atau ``` ... ```)
    if s.startswith("```"):
        # Cari blok kode pertama
        m = re.search(r"```(?:json)?\s*(.+?)\s*```", s, re.DOTALL)
        if m:
            s = m.group(1).strip()
    # Cari object JSON pertama (cari kurung kurawal pembuka pertama)
    start = s.find("{")
    if start == -1:
        return None
    # Coba parse dari posisi itu. Kalau gagal, coba slice ulang ke matching brace.
    candidates = [s[start:]]
    # Tambahkan: slice ke last matching closing brace
    end = s.rfind("}")
    if end > start:
        candidates.append(s[start:end + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return None


def _validate_variant_data(variant, lookup_spec):
    """Validate satu variant dict dari Gemini output.
    Return (is_valid, error_message_or_None).
    Required fields: 'isi' + setiap 'lookup_{key_lower}'.
    """
    if not isinstance(variant, dict):
        return False, "variant bukan dict"
    if "isi" not in variant or not isinstance(variant["isi"], str):
        return False, "'isi' missing atau bukan string"
    for spec in lookup_spec:
        field = f"lookup_{spec['key_lower']}"
        if field not in variant:
            return False, f"'{field}' missing"
        val = variant[field]
        if not isinstance(val, str):
            return False, f"'{field}' bukan string"
        # Force value to be from options or "Unknown"
        if spec["options"] and val.lower() != "unknown":
            valid_lower = {o.lower() for o in spec["options"]}
            if val.lower() not in valid_lower:
                # Tidak valid — caller bisa decide trim ke Unknown
                pass  # accept dulu, caller force ke Unknown
    return True, None


def _normalize_lookup_value(value, options):
    """Normalize lookup value: kalau ada di options (case-insensitive), kembalikan
    versi original-case dari options. Kalau ndak, return "Unknown"."""
    if not value or not isinstance(value, str):
        return "Unknown"
    v = value.strip()
    if not v or v.lower() == "unknown":
        return "Unknown"
    if not options:
        return "Unknown"
    v_lower = v.lower()
    for opt in options:
        if opt.lower() == v_lower:
            return opt  # case preserved from options list
    return "Unknown"


# ===================== CHAR-LIMIT ENFORCE (Python-side) =====================
def _resolve_char_limit(af3_value):
    """Parse AF3 ke int. Fallback CHAR_LIMIT_DEFAULT kalau invalid / out of bounds."""
    try:
        n = int(float(str(af3_value).strip()))
    except (ValueError, TypeError):
        return CHAR_LIMIT_DEFAULT
    if n < CHAR_LIMIT_MIN or n > CHAR_LIMIT_MAX:
        return CHAR_LIMIT_DEFAULT
    return n


def _trim_isi_to_fit(isi, target_max_chars):
    """Truncate ISI ke <= target_max_chars, prefer word boundary.
    Cari last separator (spasi/koma/dll) dalam 70% terakhir. Fallback hard cut.
    """
    if len(isi) <= target_max_chars:
        return isi
    cut = isi[:target_max_chars]
    backstop = int(target_max_chars * 0.6)
    for sep in (" ", ",", ";", "/"):
        idx = cut.rfind(sep)
        if idx >= backstop:
            return cut[:idx].rstrip(" ,;|/")
    return cut.rstrip(" ,;|/")


# ===================== MAIN ENTRY =====================
def run_one_cycle(ctx):
    """1 cycle: scan LINK!F -> proses max 1 row PERLU TITLE.

    Architecture v3 (post-2026-05-15): TEMPLATE-ONLY mode.
    - AF16 (Title Template) WAJIB di-isi. Kalau kosong: row di-skip dengan
      error message di J cell. Free-form mode dihapus (structural drift terlalu
      tinggi di model Flash thinking).
    - Python parse AF16 jadi tokens, kirim Gemini STRUCTURED REQUEST (JSON):
      AI extract lookup values (Server / Rank / etc) + compose ISI body.
    - Python ASSEMBLE final title dari tokens + Gemini output. Zero structural
      drift (Python deterministic).
    - ISI length auto-trim (word-boundary) kalau full assembled title > AF3.

    Return:
      1 -> ada row diproses (sukses, error message ditulis ke J, atau af16 kosong).
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

    # ===== AF5 (TITLE_TEMPLATE) mandatory check =====
    template_raw = data.get("template") or ""
    template_preview = template_raw[:80] if template_raw else "(empty)"
    add_log(f"AF5 template: '{template_preview}' (len={len(template_raw)})")
    if not template_raw.strip():
        msg = ("❌ AF5 (Title Template) belum di-set di tab ini. Bot_title v3 "
               "wajib pakai template — isi AF5 dengan string seperti "
               "'{Server}|{ISI}|TakeMail({KODE})'.")
        add_log(msg)
        try:
            safe_update_cell(sheet, baris, KOLOM_OUTPUT, msg, desc=f"template_err_J{baris}")
        except Exception as e:
            add_log(f"Gagal write error J{baris}: {str(e)[:120]}")
        update_stats(tab_name, success=False)
        return 1

    # ===== Parse template + O45 metadata =====
    tokens = _parse_template(template_raw)
    lookup_keys_in_template = _extract_lookup_keys(tokens)
    metadata_pool = _parse_metadata_pool(data["metadata_pool"])
    if metadata_pool is None:
        msg = "❌ O45 (METADATA_POOL Options) bukan JSON valid. Cek format JSON-nya di sheet."
        add_log(msg)
        try:
            safe_update_cell(sheet, baris, KOLOM_OUTPUT, msg, desc=f"o45_err_J{baris}")
        except Exception:
            pass
        update_stats(tab_name, success=False)
        return 1

    lookup_spec = _build_lookup_spec(tokens, metadata_pool)
    char_limit = _resolve_char_limit(data["af3"])
    try:
        variants_count = max(1, min(10, int(float(str(data["af4"]).strip()))))
    except (ValueError, TypeError):
        variants_count = 1
    isi_min, isi_max = _calc_isi_budget(
        tokens, lookup_spec, data["kode"], char_limit, CHAR_LIMIT_LOWER
    )

    add_log(f"Tokens: {len(tokens)} (lookup keys: {lookup_keys_in_template or 'none'})")
    add_log(f"ISI budget: {isi_min}-{isi_max} chars, variants: {variants_count}, char_limit: {char_limit}")

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
    final_text  = None
    success     = False
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

        # ===== STEP 3: Build structured prompt =====
        template = _load_prompt_template()
        if template is None:
            raise RuntimeError("prompt_title_template.txt missing")
        prompt = _build_prompt(template, data, lookup_spec, isi_min, isi_max, variants_count)

        # ===== STEP 4: Gemini call (multimodal) =====
        elapsed = time.time() - t_start
        remaining = ROW_TIMEOUT - elapsed
        if remaining < 10:
            raise RuntimeError(f"Sisa waktu {remaining:.1f}s < 10s, abort sebelum Gemini")
        gemini_cap = min(GEMINI_TIMEOUT, max(10, int(remaining)))
        try:
            response_text = call_with_timeout(
                _call_gemini_with_images,
                args=(prompt, image_paths),
                timeout=gemini_cap, name="gemini_call",
            )
        except TimeoutHangError:
            raise RuntimeError(f"Gemini timeout (>{gemini_cap}s)")
        except Exception as e:
            raise RuntimeError(f"Gemini error: {str(e)[:120]}")
        if not response_text:
            raise RuntimeError("Gemini response kosong")

        # ===== STEP 5: Parse JSON output =====
        parsed = _parse_gemini_json_output(response_text)
        if parsed is None:
            add_log(f"Gemini raw output: {response_text[:300]}")
            raise RuntimeError("Gemini output bukan JSON valid")
        variants = parsed.get("variants")
        if not isinstance(variants, list) or len(variants) == 0:
            raise RuntimeError(
                f"JSON 'variants' missing atau kosong (got: {type(variants).__name__})"
            )

        # ===== STEP 6: Assemble each variant deterministically =====
        final_titles = []
        n_trimmed = 0
        n_still_bad = 0
        for i in range(variants_count):
            if i >= len(variants):
                final_titles.append(f"❌ Variant {i+1}: Gemini ndak return data")
                n_still_bad += 1
                continue
            vd = variants[i]
            ok, err = _validate_variant_data(vd, lookup_spec)
            if not ok:
                final_titles.append(f"❌ Variant {i+1} invalid: {err}")
                n_still_bad += 1
                continue

            lookup_values = {}
            for spec in lookup_spec:
                raw_val = vd.get(f"lookup_{spec['key_lower']}", "Unknown")
                lookup_values[spec["key_lower"]] = _normalize_lookup_value(
                    raw_val, spec["options"]
                )
            isi = str(vd.get("isi", "")).strip()

            title = _assemble_title(tokens, lookup_values, isi, data["kode"])

            # Auto-trim ISI kalau over char_limit (word-boundary truncation)
            if len(title) > char_limit:
                over_by = len(title) - char_limit
                target_isi_len = max(20, len(isi) - over_by)
                trimmed_isi = _trim_isi_to_fit(isi, target_isi_len)
                new_title = _assemble_title(tokens, lookup_values, trimmed_isi, data["kode"])
                add_log(
                    f"Variant {i+1} auto-trim ISI: {len(isi)}→{len(trimmed_isi)} "
                    f"(title {len(title)}→{len(new_title)})"
                )
                title = new_title
                n_trimmed += 1

            if len(title) > char_limit or len(title) < CHAR_LIMIT_LOWER:
                final_titles.append(f"❌ {title}")
                n_still_bad += 1
            else:
                final_titles.append(title)

        final_text = "\n".join(final_titles)
        success = True

        lengths = [len(t) for t in final_titles]
        add_log(
            f"Assembled {len(final_titles)} variants, lengths: {lengths}, "
            f"auto-trim: {n_trimmed}, still bad: {n_still_bad}"
        )

    except Exception as e:
        err_msg = str(e)[:200]

    # ===== STEP 7: Tulis hasil ke J{n} =====
    elapsed_total = time.time() - t_start
    if err_msg:
        final_text = f"❌ {err_msg}"
        success = False
        add_log(f"❌ [{tab_name}] {data['kode']} | {err_msg} ({elapsed_total:.1f}s)")

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
        n_lines = len([ln for ln in (final_text or "").splitlines() if ln.strip()])
        add_log(f"✅ [{tab_name}] {data['kode']} berhasil generate {n_lines} title variants ({elapsed_total:.1f}s)")

    set_processing(None)
    with worker_status_lock:
        worker_status.clear()

    return 1
