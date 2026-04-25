# create/ — Per-Market Adapter untuk bot_create

Folder ini berisi adapter untuk tiap marketplace yang di-support oleh `bot_create.py`.
Bot_create orchestrator (di parent folder) **dynamic dispatch** via `importlib.import_module("create.{CODE}")`.
Tambah market baru = tambah 1 file `create/NEW.py` yang implement contract.

> Dokumentasi parent (overview project, config, setup): [../README.md](../README.md)

---

# Daftar Isi

- [Overview](#overview)
- [Adapter Contract](#adapter-contract)
- [Flow per cycle](#flow-per-cycle)
- [Shared helpers (`_shared.py`)](#shared-helpers-_sharedpy)
- [Async image download pattern](#async-image-download-pattern)
- [Per-adapter reference](#per-adapter-reference)
  - [GM — GameMarket](#gm--gamemarket)
  - [G2G — G2G](#g2g--g2g)
  - [PA — PlayerAuctions](#pa--playerauctions)
  - [U7 — U7Buy](#u7--u7buy)
  - [ZEUS — ZeusX](#zeus--zeusx)
  - [GB — GameBoost](#gb--gameboost)
  - [ELDO — Eldorado](#eldo--eldorado)
  - [IGV — iMetaStore](#igv--imetastore)
- [Cara menambah market baru](#cara-menambah-market-baru)
- [Troubleshooting per adapter](#troubleshooting-per-adapter)

---

## Overview

`create/` berisi 8 adapter market saat ini:

| File | Market | MAX_IMAGES | HARGA_COL | Min price | Pattern upload |
|------|--------|-----------|-----------|-----------|----------------|
| [GM.py](GM.py) | GameMarket | 20 | H (USD 8) | $1.99 | Bulk, retry 3x, verify preview |
| [G2G.py](G2G.py) | G2G | 10 | G (IDR 7) | Rp 40000 | URL paste (imgur only) |
| [PA.py](PA.py) | PlayerAuctions | 1 | H (USD 8) | $5 | 1 file cover image |
| [U7.py](U7.py) | U7Buy | 3 | H (USD 8) | $1.99 | One-by-one, confirm popup |
| [ZEUS.py](ZEUS.py) | ZeusX | 10 | H (USD 8) | $1.99 | One-per-slot |
| [GB.py](GB.py) | GameBoost | 20 | H (USD 8) | $1.99 | Bulk |
| [ELDO.py](ELDO.py) | Eldorado | 5 | H (USD 8) | $1.99 | Bulk max 5 |
| [IGV.py](IGV.py) | iMetaStore | 5 | H (USD 8) | $5 | Bulk + polling `is-success` |

**Min price override**: kalau harga sumber dari sheet < min, adapter otomatis fill min (sheet **tetap nilai asli**, cuma form yang di-override). Contoh: harga sheet $1.50, min GM $1.99 → form di-fill `1.99`.

Plus:

- [_shared.py](_shared.py) — shared helpers (scrape gambar, AI mapping, timing, Chrome context)
- [__init__.py](__init__.py) — empty (marker package)
- [README.md](README.md) — dokumen ini

## Adapter Contract

Tiap adapter **wajib** ekspor:

```python
# ===================== KONSTANTA =====================
MARKET_CODE     = "GM"                          # str: kode market unik (match row 48 di sheet)
HARGA_COL       = 8                             # int: kolom harga (1-based). H=8, G=7, dst
MAX_IMAGES      = 20                            # int: batas gambar per listing
CACHE_SENTINEL  = "[tidak ditemukan options GM]" # str: marker cache saat game tidak punya form

# ===================== ENTRY 1: scrape form =====================
def scrape_form_options(game_name) -> dict | {} | None:
    """Buka marketplace, pilih game, scrape dropdown options.
    Return:
      dict {label: [option1, option2, ...]}  # ada form
      {}                                      # game valid, tapi tidak punya form (sentinel)
      None                                    # error / skip
    Dipanggil 1x per unique game, hasil di-cache di row 45 kolom market.
    """

# ===================== ENTRY 2: bogus cache check =====================
def cache_looks_bogus(cache_dict) -> bool:
    """Return True kalau cache lama terdeteksi bogus (misal option kosong,
    label aneh, dll). Orchestrator invalidate cache + re-scrape kalau True.
    Return False default.
    """

# ===================== ENTRY 3: create listing =====================
def create_listing(game_name, title, deskripsi, harga, field_mapping, image_paths,
                   raw_image_url=None, image_future=None):
    """Full flow: navigate, fill form, upload gambar, submit.
    Return (ok: bool, err: str | None, uploaded_count: int).

    Args:
      game_name: nama game di marketplace (dari O43 sheet)
      title: listing title (dari J column sheet)
      deskripsi: listing description (dari O44 sheet)
      harga: harga listing (dari HARGA_COL sheet)
      field_mapping: dict {label: chosen_value} dari AI Gemini
      image_paths: list path file lokal (legacy sync mode)
      raw_image_url: URL album source (imgur/gdrive/postimg)
      image_future: concurrent.futures.Future yg resolve ke (paths, urls, is_imgur).
                    Tepat sebelum step upload, call resolve_image_future(future).
                    Fallback ke image_paths kalau None.
    """

# ===================== ENTRY 4: adapter entry point =====================
def run(sheet, baris_nomor, worker_id, *, game_name, description, title, harga,
        field_mapping, image_paths=None, image_urls=None,
        raw_image_url=None, is_imgur=False, image_future=None):
    """Dipanggil orchestrator. Thin wrapper di atas create_listing.
    Return (ok: bool, k_line: str).

    k_line format (semua market code di-bracket dgn []):
      Sukses: "✅ [{CODE}] | {N} images uploaded | DD MMM, YY | HH:MM [| listing_url]"
      Gagal:  "❌ [{CODE}] | {error_message[:80]}"

    NOTE: orchestrator (bot_create.py) saat tulis K column pakai `k_line` ini
    apa adanya. TAPI untuk toast log (UI), orchestrator ganti format ke ringkas
    untuk sukses: `"✅ [{CODE}] {kode_listing} berhasil dipost {N} images uploaded!"`
    (extract count dari k_line). Ini sengaja — biar K column punya detail
    timestamp+URL sementara toast user lebih clean.
    """
```

### Kontrak return value

| Function | Return |
|----------|--------|
| `scrape_form_options` | `dict` \| `{}` \| `None` |
| `cache_looks_bogus` | `bool` |
| `create_listing` | `(ok: bool, err: str \| None, uploaded_count: int)` |
| `run` | `(ok: bool, k_line: str)` |

**k_line** = string yang akan di-log oleh orchestrator + aggregate ke K column sheet.

## Flow per cycle

`bot_create.py` orchestrator panggil adapter via 2 titik:

### A. Scrape form (1x per unique game)

```
bot_create._ensure_form_options_cache(sheet, code, game_name, cache_col, initial_cache)
  ↓
  mod.scrape_form_options(game_name)  ← adapter entry
  ↓
  Hasil di-JSON-stringify, di-write ke cell row 45 kolom market (cache)
  Next cycle dengan game sama: skip scrape, baca cache cell
```

### B. Create listing (setiap row PERLU POST)

```
bot_create._market_thread(entry):
  ↓
  _run_market(code, sheet, baris_nomor, worker_id, image_future=..., ...)
  ↓
  mod.run(sheet, baris_nomor, worker_id, image_future=..., ...)  ← adapter entry
  ↓
  mod.create_listing(...)  ← adapter internal
  ↓
  Return (ok, k_line)
  ↓
  Orchestrator aggregate k_line ke K column sheet
```

## Shared helpers (`_shared.py`)

File [_shared.py](_shared.py) berisi utilities yg di-share antar adapter:

### Runtime injection

```python
inject_runtime(log=..., worker_temp_dir=..., prepare_worker_temp_dir=...,
               gemini_model=..., chrome_debug_port=..., chrome_cdp_url=...)
```

Dipanggil 1x di `bot_create._bind_ctx()` → inject logger / temp dir / gemini / chrome ke shared state. Adapter ambil via `_log()` / `_get_chrome_cdp_url()` / dll.

### Helper functions

| Function | Signature | Keterangan |
|----------|-----------|------------|
| `_log(msg)` | via `inject_runtime(log=...)` | Log dengan prefix worker |
| `_get_chrome_cdp_url()` | `() -> str` | Chrome CDP WebSocket URL |
| `_get_chrome_debug_port()` | `() -> int` | Chrome debug port |
| `_get_gemini_model()` | `() -> Gemini \| None` | Gemini model instance |
| `xpath_literal(s)` | `(str) -> str` | Safe XPath 1.0 string literal (escape apostrof) |
| `smart_wait(page, min_ms, max_ms)` | Pause-aware random wait | |
| `get_or_create_context(browser)` | Reuse/create Chromium context | |
| `obfuscate_image_url(url)` | `(str) -> str` | `imgur.com/x` → `imgur .com/x` (anti auto-delete) |
| `scrape_imgur(url)` | `(str) -> list[str]` | Extract direct URL dari imgur album |
| `scrape_postimg(url)` | `(str) -> list[str]` | Extract dari postimg gallery |
| `scrape_gdrive(url)` | `(str) -> list[str]` | Extract dari GDrive folder |
| `download_images_with_urls(url, max=20)` | `(str, int) -> (paths, urls, is_imgur)` | Unified scrape + download |
| `download_images(url)` | Legacy (GM-only) | Return list local path saja |
| `start_image_download_async(url, max, name)` | `-> Future` | Spawn background download, return Future |
| `resolve_image_future(future, timeout=120)` | `-> (paths, urls, is_imgur)` | Block sampai future resolve, raise `RuntimeError` on fail |
| `cleanup_temp_images(paths)` | `(list[str])` | Delete temp file setelah selesai |
| `ai_map_fields_multi(title, market_inputs)` | `-> {code: {field: value}}` | 1 Gemini call untuk N market (flat format only) |
| `extract_image_urls_for_g2g(url)` | G2G-specific URL extractor | |

## Async image download pattern

Sebelumnya download gambar blocking (30-40s per cycle). Sekarang async:

### Orchestrator ([bot_create.py](../bot_create.py))

```python
image_future = start_image_download_async(
    gambar_url, max_images=max_images_needed,
    name=f"img-dl-{worker_id}",
)

# Dispatch market threads SEGERA - ndak nunggu download selesai
for entry in markets_todo:
    threading.Thread(target=_market_thread, args=(entry,), ...).start()

# Per market thread:
ok, line = _run_market(..., image_future=image_future, ...)
```

### Adapter ([create/*.py](./))

```python
def create_listing(..., image_future=None):
    ...
    # Jalan flow navigate, fill form, title, description, dll
    # (pre-upload steps, ndak butuh gambar)
    ...

    # Tepat sebelum step upload:
    if image_future is not None:
        try:
            resolved_paths, resolved_urls, resolved_is_imgur = resolve_image_future(image_future)
            image_paths = resolved_paths  # atau image_urls untuk G2G
        except RuntimeError as e:
            return False, str(e), uploaded

    if not image_paths:
        return False, "Gambar tidak bisa di download", uploaded

    # Upload step seperti biasa
    _upload_images(page, image_paths)
    ...
```

**Backwards compat**: kalau `image_future=None` (orchestrator rollback atau test direct), adapter fallback ke `image_paths`/`image_urls` kwarg legacy.

## Per-adapter reference

### GM — GameMarket

**File**: [GM.py](GM.py)

| Field | Value |
|-------|-------|
| URL create | `https://gamemarket.gg/dashboard/create-listing` |
| MAX_IMAGES | 20 |
| HARGA_COL | H (8) |
| Upload | Bulk `set_input_files` + retry 3x + verify blob preview |
| Form fields | Dynamic dropdown (game-specific) |
| CACHE_SENTINEL | `"[tidak ditemukan options]"` |
| K line sukses | `✅ [GM] | N images uploaded | ts | listing_url` |

**Flow**:
1. Goto URL + inject worker tab title
2. Select game via searchable dropdown
3. Fill title
4. Fill form dynamic dropdowns (via AI field_mapping)
5. Fill price
6. Min order = 1
7. **Upload images bulk** (HARDENED: retry 3x, poll blob:-URL preview muncul ≥ expected count, kalau gagal abort listing)
8. Fill description
9. Submit → wait redirect / verify URL

**Quirks**:
- File ini paling besar di adapter (~900 baris) karena banyak helper & flow complex
- URL listing ter-extract dari response → di-append ke K line

### G2G — G2G

**File**: [G2G.py](G2G.py)

| Field | Value |
|-------|-------|
| URL create | `https://www.g2g.com/offers/sell?cat_id=...` |
| MAX_IMAGES | 10 |
| HARGA_COL | G (7) — beda dari GM |
| Upload | **URL paste** (BUKAN file upload) |
| Image source | HANYA imgur (kalau `is_imgur=True`) |
| Form fields | "Silakan Pilih" buttons, Quasar q-menu popper |
| CACHE_SENTINEL | `NO_OPTIONS_SENTINEL_G2G` = `"[tidak ditemukan options G2G]"` |
| K line sukses | `✅ [G2G] | N images uploaded | ts` atau `✅ [G2G] | image URL in description | ts` |

**Flow**:
1. Goto URL + inject worker tab title
2. Select produk (game) via searchable dropdown
3. Klik Lanjutkan → transisi ke form
4. **Iterate tiap "Silakan Pilih" button** → click → scrape options dari q-menu → close dropdown (3-stage fallback)
5. AI mapping → fill dropdown per label
6. Fill title, price
7. **Resolve image future** → extract imgur direct URLs
8. Fill description (URL raw obfuscated via `obfuscate_image_url`)
9. **Input image URL one-by-one** + klik "Tambah media" per URL (max 10)
10. Terbitkan → popup confirm

**Quirks**:
- **URL paste bukan file upload** — G2G reject file upload untuk account listing
- **Image URL filter: cuma imgur** — source lain (gdrive/postimg) reject sebagai "URL tidak valid" → skip image step
- **URL di description di-obfuscate** (space sebelum TLD: `imgur .com/x`) biar ndak auto-delete marketplace
- Close dropdown Quasar punya 4-stage fallback (Escape x2 → body.click JS → toggle → force hide DOM)
- `cache_looks_bogus` detect icon name (expand_more, search, dll) = bogus label

### PA — PlayerAuctions

**File**: [PA.py](PA.py)

| Field | Value |
|-------|-------|
| URL create | `https://member.playerauctions.com/offers/creation` |
| MAX_IMAGES | 1 (cover image only) |
| HARGA_COL | H (8) |
| Min price | **$5** (override kalau harga < $5) |
| Upload | 1 file via `app-image-upload` |
| Form | TinyMCE description editor, nz-select dropdowns |
| CACHE_SENTINEL | `NO_OPTIONS_SENTINEL_PA` |

**Flow**:
1. Goto URL + smart_wait slow (PA rate-limit ketat)
2. Select game + category (2 level)
3. Login name fill (dari kolom B sheet)
4. Retype login name fill
5. Fill title
6. **Price fill dengan $5 override kalau harga < 5**
7. Radio After-Sale Protection = None
8. Tab "Manual Delivery"
9. Fill description TinyMCE (iframe switch)
10. Fill dynamic nz-select dropdowns
11. **Resolve image future** → upload 1 cover image + poll preview
12. Centang 1 checkbox TOS
13. Submit → verify redirect

**Quirks**:
- **Cuma upload 1 gambar** (cover image). `MAX_IMAGES=1`
- **Min price $5** — PA tolak listing < $5 (policy 2026). Adapter auto-override: kalau `harga < 5` → fill "5"
- Anti-spam throttle: `_PA_SLOW_MULT = 1.75x` + 0-50% jitter. PA sering kick session kalau post terlalu cepat
- Login name + retype baca dari kolom B sheet (via `run(sheet.cell(baris_nomor, 2).value)`)
- Description pakai "Full Screenshot Detail: {url-stripped-scheme}" prefix line
- **Early error detection** — saat polling redirect setelah Submit, cek paralel `p.text-danger.p-t-1`, `.ant-message-error`, `.ant-notification-notice-error`. Kalau ada inline error (misal "Provided login name already exists") → fail langsung tanpa nunggu 1 menit timeout

### U7 — U7Buy

**File**: [U7.py](U7.py)

| Field | Value |
|-------|-------|
| URL create | `https://www.u7buy.com/member/offers/create-offer` |
| MAX_IMAGES | **3** (diturunkan dari 5 karena U7 upload sering error) |
| HARGA_COL | H (8) |
| Upload | One-by-one + confirm popup per file, **abort-after-first-fail** |
| Upload timeout | 30s per file |
| Form | `u7-select` custom (bukan el-select), ada `u7-placeholder` inner |
| CACHE_SENTINEL | `NO_OPTIONS_SENTINEL_U7` |

**Flow**:
1. Goto URL (slow stagger)
2. Select game di searchable dropdown
3. Select category "Accounts"
4. Next step → Provide Product Detail heading
5. Fill Product Name (title)
6. **Resolve image future** → upload images one-by-one dengan confirm popup
   - **Abort-after-first-fail**: kalau upload 1 fail, langsung abort listing (U7 stuck loading biasanya)
7. Fill description (line 1 raw URL no scheme + body)
8. **Fill dynamic dropdowns (u7-select) via AI field_mapping** — hardened multi-strategy:
   - Round 1-3, tiap round:
     - Strategy 1: click outer `div.u7-select`
     - Strategy 2: click inner `.u7-placeholder`
     - Strategy 3: focus + Enter
     - Strategy 4: JS `el.click()` + dispatchEvent
9. Delivery Method = Manual (fixed el-select)
10. Delivery Time = 1 hour (fixed)
11. Selling Price fill
12. Policy checkbox
13. Submit → wait redirect

**Quirks**:
- **Max images 3** (sebelumnya 5) — U7 website sering error saat upload > 3 (policy change / bandwidth)
- **Min price $1.99** — auto-override kalau harga < $1.99
- **Abort-after-first-fail** — kalau upload 1 gagal, langsung return, cegah waste 3 × 30s
- **u7-select custom**: multi-strategy click karena tabindex=0 outer + click handler di inner placeholder
- **Hardening intermittent**: kadang click ndak trigger karena Vue state transient → retry 3 round dengan 4 strategy:
  1. Click outer `div.u7-select`
  2. Click inner `.u7-placeholder`
  3. Focus + Enter (keyboard activation)
  4. JS-level `el.click()` + `dispatchEvent(mousedown/mouseup/click)`

### ZEUS — ZeusX

**File**: [ZEUS.py](ZEUS.py)

| Field | Value |
|-------|-------|
| URL create | `https://zeusx.com/s/account/create-offer` |
| MAX_IMAGES | 10 |
| HARGA_COL | H (8) |
| Upload | One-per-slot (CSS hash selectors) |
| Form | CKEditor description, hashed CSS classes |
| CACHE_SENTINEL | `NO_OPTIONS_SENTINEL_ZEUS` |

**Flow**:
1. Goto URL (anti-spam throttle: `_ZEUS_SLOW_MULT = 1.25x` + 0-40% jitter)
2. Select game
3. Select category/server dynamic dropdowns
4. Price fill
5. Fill title
6. Hours = 1 (fixed)
7. Fill description CKEditor (URL line 1 + body, setData API via JS)
8. **Resolve image future** → upload one-per-slot (each slot = separate `<input type=file>`)
9. Centang terms checkbox
10. Submit "List Items" → verify URL pindah dari `/create-offer`

**Quirks**:
- **Anti-spam throttle 1.25x + jitter** — ZEUS account pernah kena suspend 24 jam karena post terlalu cepat
- **CSS hash** — class name pakai module hash (`foo_bar__HASH`). Selector pakai kombinasi hashed exact + class prefix supaya survive hash churn antar deploy
- **CKEditor description** via `setData()` JS API biar paragraph order deterministic (typing keyboard kadang kacau)
- **ZEUS "Chat With Customer" detection** — kalau ada, listing udah terjual → anggap sukses (ndak bisa di-hapus tapi tetep mark centang)

### GB — GameBoost

**File**: [GB.py](GB.py)

| Field | Value |
|-------|-------|
| URL create | `https://gameboost.com/sell-accounts/new` |
| MAX_IMAGES | 20 |
| HARGA_COL | H (8) |
| Upload | Bulk `set_input_files` via hidden input |
| Form | React-dropzone style, custom components |
| CACHE_SENTINEL | `NO_OPTIONS_SENTINEL_GB` |

**Flow**:
1. Goto URL
2. Game Selection → select game card + Continue
3. Fill title, description
4. **Resolve image future** → bulk upload (hidden input, fire-and-forget)
5. Continue (ke Game Data) → fill dynamic dropdowns via AI mapping
6. Continue (ke Pricing) → price, quantity = 1
7. Post → verify redirect

**Quirks**:
- **React-dropzone**: click pada drag-area kadang ndak trigger file chooser. Fallback: direct `set_input_files` ke hidden `input[type=file]`
- Multi-step wizard (Game → Description → Game Data → Pricing → Post)
- Auto-advance step setelah Continue click

### ELDO — Eldorado

**File**: [ELDO.py](ELDO.py)

| Field | Value |
|-------|-------|
| URL create | `https://www.eldorado.gg/sell/offer/Account` |
| MAX_IMAGES | 5 |
| HARGA_COL | H (8) |
| Upload | Bulk max 5 sekaligus |
| Form | Angular `eld-dropdown`, `_ngcontent-ng-cXXXXXXXXXX` attributes |
| CACHE_SENTINEL | `NO_OPTIONS_SENTINEL_ELDO` |

**Flow**:
1. Goto `/sell/offer/Account`
2. Pilih game dari dropdown searchable → klik Next → navigate ke `/sell/offer/Account/{gameId}`
3. Fill dynamic dropdown (via AI mapping) — format `<eld-dropdown>` dengan trigger span `Select {Label}`
4. Fill title, price
5. Fill description textarea (line 1 URL no scheme + body)
6. **Resolve image future** → upload bulk (1 `set_input_files` call)
7. Centang 2 TOS checkbox
8. Publish → verify redirect ke `/dashboard/offers?category=Account`

**Quirks**:
- **Angular custom components** — CSS hash churn antar deploy, selector pakai role/aria + utility class yg stabil
- Anti-spam throttle lebih ketat dari ZEUS: `_ELDO_SLOW_MULT = 1.5x` + 0-50% jitter
- Note di comment: "belum-impl" untuk 1 step (publish auto-TRUE) — legacy, dikerjakan tapi kadang return False di akhir untuk skip mark centang. **Cek flow jalan penuh sekarang** — dokumentasi source mungkin outdated.

### IGV — iMetaStore

**File**: [IGV.py](IGV.py)

| Field | Value |
|-------|-------|
| URL create | `https://seller.imetastore.io/product/add?step=1` |
| MAX_IMAGES | 5 |
| HARGA_COL | H (8) |
| Upload | Bulk + polling `is-success` class |
| Form | **Mix select + input** (unique) — shared AI mapping ndak cukup |
| CACHE_SENTINEL | `NO_OPTIONS_SENTINEL_IGV` |

**Flow**:
- **Step 1** (`?step=1`):
  1. Goto URL
  2. Pick brand (game) via Element-Plus el-select — click wrapper, **keyboard type filter**, click option **case-insensitive exact match**
  3. Pick Product category "Accounts" (radio)
  4. Click Next → navigate ke `?step=2`
- **Step 2** (`?step=2`):
  1. Scrape Game Details section (`h4.module-item-title = 'Game Details'`)
  2. Ambil **HANYA field required** (`div.el-form-item.is-required`)
  3. Deteksi tipe: `select` (ada `div.el-select`) atau `input` (ada `input.el-input__inner`)
  4. **Call Gemini sendiri** (`_ai_map_igv_fields`) — bukan shared, karena mix select+input
  5. Fill Game Details per schema + AI value
  6. Fill Title (max 144 char, auto-truncate)
  7. **Resolve image future** → upload max 5 bulk + **poll `li.el-upload-list__item.is-success`** sampai semua sukses atau timeout 30s
  8. Fill Product Description (Jodit WYSIWYG) — **paste via JS `el.innerHTML` + dispatch input/change event** (bukan typing, cegah race)
  9. Click Next (pilih **`.last`** button primary karena ada Previous di sebelahnya)
- **Step 3** (`?step=3`):
  1. Fill Delivery method required fields:
     - Field pertama = `login_name` (kolom B sheet)
     - Field ke-2+ = `IGV_DELIVERY_FIX_TEXT` = `"PleaseContactSeller@IGVChat.ForDetails"`
  2. Fill Price input (`type=number`, strip `$`/`,` dari raw value)
  3. Select Warranty Period = `"14Days"` (Insurance section)
  4. Click "Post product"
  5. Verify: redirect ke `/product/my` OR success toast (`.el-message--success`)
  6. **Delay 1 detik** sebelum tab close (settle)

**Quirks**:
- **Min price $5** — IGV minimum policy, auto-override kalau harga < $5
- **Mix select + input di Game Details** — IGV adapter call Gemini sendiri (bypass shared ai_map_fields_multi yang cuma handle flat select-only format)
- **Case-insensitive exact match** game name — kadang sheet `"Arena breakout"`, IGV `"Arena Breakout"` / `"Arena Breakout Infinite"` → match lowercase, ambil yg exact (ndak cocok partial)
- **Keyboard type filter** untuk brand dropdown — list panjang (1700+ game, virtual scroll) → type untuk auto-filter
- **Next button `.last`** — Step 2→3 ada Previous + Next, keduanya primary class → pilih yg rightmost
- **Jodit editor paste via JS** — typing lambat bikin tulisan terpotong saat Next click race. JS set innerHTML + fire input event = instant
- **Upload polling `.is-success`** — ndak cuma count item muncul, tapi wait sampai status sukses (Element-Plus state) — settle delay 1s setelah upload selesai sebelum lanjut Next
- **Login name kolom B** — dipakai di **field pertama** Delivery method Step 3. Field ke-2+ pakai `IGV_DELIVERY_FIX_TEXT = "PleaseContactSeller@IGVChat.ForDetails"`
- **Settle delay 1s** setelah Post product redirect sukses sebelum tab close (biar IGV server confirmed receive)
- **Internal log tanpa emoji** — `[IGV] Navigasi ke Step 2 sukses` (bukan `✅ Navigasi`) supaya ndak trigger toast UI di tiap progress step (toast hanya fire pada K line final `✅ IGV | ...`)

## Cara menambah market baru

Misal mau tambah market baru bernama **NEW** dengan kode `NEW`.

### Step 1. Bikin `create/NEW.py`

Copy template dari adapter paling mirip (biasanya IGV atau GM):

```python
"""create/NEW.py - {MarketName} marketplace adapter.

Entry points dipakai orchestrator (bot_create.py):
- scrape_form_options(game_name) -> dict | {} | None
- create_listing(...) -> (ok, err, uploaded_count)
- run(sheet, baris_nomor, worker_id, **kwargs) -> (ok, k_line)
- cache_looks_bogus(cache) -> bool
"""

import re
from datetime import datetime
from playwright.sync_api import sync_playwright

from create._shared import (
    _worker_local,
    _log as add_log,
    _get_chrome_cdp_url,
    xpath_literal as _xpath_literal,
    smart_wait,
    get_or_create_context,
    resolve_image_future,  # ← WAJIB untuk async image pattern
)


# ===================== KONSTANTA =====================
NEW_CREATE_URL          = "https://new-marketplace.com/sell"
NEW_MAX_IMAGES          = 5
MARKET_CODE             = "NEW"
HARGA_COL               = 8   # H
MAX_IMAGES              = NEW_MAX_IMAGES
NO_OPTIONS_SENTINEL_NEW = "[tidak ditemukan options NEW]"
CACHE_SENTINEL          = NO_OPTIONS_SENTINEL_NEW


# ===================== HELPERS =====================
# def _pick_game(page, game_name): ...
# def _fill_title(page, title): ...
# def _fill_description(page, description, raw_image_url): ...
# def _upload_images(page, image_paths): ...
# def _click_submit(page): ...


# ===================== PUBLIC ENTRIES =====================
def scrape_form_options(game_name):
    """Navigate marketplace, pilih game, scrape dropdown. Return dict atau {}."""
    # Kalau ndak ada form dinamis, return {} dan cache sentinel
    return {}


def cache_looks_bogus(cache):
    return False


def create_listing(game_name, title, deskripsi, harga, field_mapping, image_paths,
                   raw_image_url=None, image_future=None):
    uploaded = 0
    cdp_url = _get_chrome_cdp_url()
    if not cdp_url:
        return False, "CDP URL tidak tersedia", uploaded

    with sync_playwright() as p:
        page = None
        try:
            browser = p.chromium.connect_over_cdp(cdp_url, timeout=10000)
            context = get_or_create_context(browser)
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()

            add_log(f"[NEW] Goto {NEW_CREATE_URL}")
            page.goto(NEW_CREATE_URL, wait_until="domcontentloaded", timeout=30000)
            smart_wait(page, 2000, 4000)

            # ... fill flow: select game, title, dropdowns, description ...

            # Resolve image future (async pattern) tepat sebelum upload
            if image_future is not None:
                try:
                    resolved_paths, _, _ = resolve_image_future(image_future)
                    image_paths = resolved_paths
                except RuntimeError as e:
                    return False, str(e), uploaded
            if not image_paths:
                return False, "Gambar tidak bisa di download", uploaded

            # _upload_images(page, image_paths)
            # uploaded = len(image_paths)

            # ... submit, verify ...

            return True, None, uploaded
        except Exception as e:
            return False, f"Error: {str(e)[:100]}", uploaded
        finally:
            if page:
                try: page.close()
                except: pass


def run(sheet, baris_nomor, worker_id, *, game_name, description, title, harga,
        field_mapping, image_paths=None, image_urls=None,
        raw_image_url=None, is_imgur=False, image_future=None):
    _worker_local.worker_id = f"{worker_id}-NEW"

    ok, err, uploaded = create_listing(
        game_name, title, description or "", harga,
        field_mapping or {},
        (image_paths or [])[:MAX_IMAGES] if image_paths else None,
        raw_image_url=raw_image_url,
        image_future=image_future,
    )
    ts = datetime.now().strftime("%d %b, %y | %H:%M")
    if ok:
        return True, f"✅ NEW | {uploaded} images uploaded | {ts}"
    return False, f"❌ NEW | {err or 'gagal'}"
```

### Step 2. Update `build_exe.bat`

**Wajib!** PyInstaller `--collect-submodules create` kadang miss lazy-loaded adapter.

```bat
    --hidden-import create.ZEUS ^
    --hidden-import create.ELDO ^
    --hidden-import create.PA ^
    --hidden-import create.U7 ^
    --hidden-import create.GB ^
    --hidden-import create.IGV ^
    --hidden-import create.NEW ^     <-- TAMBAH INI
```

### Step 3. Update Google Sheets

Di tiap tab game yang mau pake market NEW:
- Row 48: isi "NEW" di kolom yang tersedia (O-Z)
- Row 43: isi game name di kolom market NEW
- Row 49: isi manage link di kolom market NEW
- Kolom checkbox (O-Z sesuai kolom market) default FALSE

### Step 4. Test

1. Run `python main.py` di dev PC
2. Trigger `PERLU POST` di salah satu row
3. Cek log: `[NEW] Goto ...` harus muncul
4. Verify listing ter-post di marketplace NEW

### Step 5. Release

`release.bat` → version bump → auto-push → auto-upload Release. Office PC `update.bat` untuk ambil versi baru.

## Troubleshooting per adapter

### `module create.{CODE}.run tidak ada`

Biasanya karena EXE di office PC belum include adapter baru. Lihat [Step 2](#step-2-update-build_exebat) di atas.

### Adapter return format `{label: {type, options}}` bikin AI error `KeyError: 0`

Shared `ai_map_fields_multi` cuma handle flat `{label: [options]}`. Kalau adapter perlu format nested (mix select+input), **bypass shared AI** dengan:

1. `scrape_form_options` return `{}`
2. Call Gemini sendiri di dalam `create_listing()` pakai `_get_gemini_model()`

Contoh: IGV.py line 82-159.

### Dropdown open tapi options ndak muncul

Banyak penyebab:
- Virtual scroll / lazy render → perlu keyboard filter atau scroll
- Click target salah (outer wrapper vs inner placeholder)
- Popper di body level (teleport) vs inline → cek DOM

Pattern debug:
1. Log 10 first option text di popper
2. Compare dengan yg expected dari scrape/AI mapping
3. Trial multi-strategy (click outer, click inner, focus+Enter, JS click)

### Upload gambar stuck / timeout

Checklist:
- Polling check status class (`.is-success` / `.is-uploading`) bukan cuma count
- Network tab di DevTools: request upload return 200 atau error?
- Abort-after-first-fail pattern (seperti U7) kalau sering stuck
- Increase timeout per file (30-60s)

### Post button click tapi ndak navigate

Checklist:
- URL match pattern bener? Pakai `page.wait_for_url("**/success", timeout=20000)`
- Fallback: check success toast element (`el-message--success`)
- Check ada validation error inline (`el-form-item__error` merah) → form field missing
- Log current URL setelah timeout untuk diagnostic

---

## Changelog adapter

- **IGV** — added v1.11.0 (full flow Step 1-3, Jodit paste, case-insensitive game match), min $5 override, login_name kolom B di field pertama Delivery method (sisa fix text `PleaseContactSeller@IGVChat.ForDetails`), settle delay 1s post-redirect, internal log tanpa emoji (cegah toast spam)
- **U7** — max images 5→3 (v1.11.x), multi-strategy dropdown open (4 strategy × 3 round), abort-after-first-fail upload, min $1.99 override
- **GM** — count fix async (v1.11.x), min $1.99 override
- **G2G** — close dropdown 4-stage fallback (v1.11.x), min Rp 40000 override
- **PA** — min price $5 override (v1.10.x), delete timeout 60s→30s (v1.11.x), early error detection (`p.text-danger` polling paralel — fail < 1s tanpa nunggu redirect timeout)
- **ELDO** — min $1.99 override
- **ZEUS** — min $1.99 override
- **GB** — min $1.99 override
- **Async image download** — all 8 adapters (v1.11.x)
- **Min price override** — semua adapter (v1.11.x), sheet tetap nilai asli, override hanya saat fill form
- **K line format `[CODE]` brackets** — semua adapter pakai `✅ [GM] | ...` / `❌ [GM] | ...` di run() return (v1.11.x)
- **Toast format ringkas (CREATE)** — orchestrator emit toast sukses ringkas `✅ [GM] ABC123 berhasil dipost 5 images uploaded!` (extract count dari k_line). K column tetap full format dengan timestamp + listing URL

---

**Parent doc**: [../README.md](../README.md)
