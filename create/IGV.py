"""create/IGV.py - IGV (iMeta Seller Centre) marketplace adapter.

Entry points dipakai orchestrator (bot_create.py):
- scrape_form_options(game_name) -> dict | {} | None
- create_listing(...) -> (ok, err, uploaded_count)
- run(sheet, baris_nomor, worker_id, **kwargs) -> (ok, k_line)
- cache_looks_bogus(cache) -> bool

Flow IGV:
  Step 1: goto ?step=1 -> pick Brand (game) dari dropdown -> pilih Product
          category 'Accounts' -> klik Next (navigate ke ?step=2).
  Step 2: Game Details (scrape + AI mapping own, IGV unik karena mix select+
          input), Title, upload 5 gambar, Jodit Product Description -> Next.
  Step 3: Delivery method (fix text semua required input), Price (USD kolom H),
          Insurance Warranty '14Days', klik Post product, verify redirect
          /product/my atau Element-Plus success toast.

UI: Element Plus components (el-input, el-radio, el-button, el-select) +
Jodit WYSIWYG editor untuk Product Description.
"""

import json
import re
from datetime import datetime

from playwright.sync_api import sync_playwright

from shared import call_with_timeout, TimeoutHangError

from create._shared import (
    _worker_local,
    _log as add_log,
    _get_chrome_cdp_url,
    _get_gemini_model,
    xpath_literal as _xpath_literal,
    smart_wait,
    get_or_create_context,
    resolve_image_future,
)


# ===================== KONSTANTA =====================
IGV_START_URL            = "https://seller.imetastore.io/product/add?step=1"
IGV_STEP2_URL_PATTERN    = "**/product/add?step=2"
IGV_STEP3_URL_PATTERN    = "**/product/add?step=3"
IGV_SUCCESS_URL_PATTERN  = "**/product/my*"
IGV_MAX_IMAGES           = 5
MARKET_CODE              = "IGV"
HARGA_COL                = 8   # kolom H (USD)
MAX_IMAGES               = IGV_MAX_IMAGES
NO_OPTIONS_SENTINEL_IGV  = "[tidak ditemukan options IGV]"
CACHE_SENTINEL           = NO_OPTIONS_SENTINEL_IGV

# Fix text untuk field delivery method (Account, Password, dll). Diisi semua
# field required di section Delivery method (jumlah field berbeda per game).
IGV_DELIVERY_FIX_TEXT    = "PleaseContactSeller@IGVChat.ForDetails"
# Warranty period untuk field Insurance
IGV_WARRANTY_OPTION      = "14Days"


# ===================== STEP 1 HELPERS =====================
def _pick_brand_game(page, game_name):
    """Klik dropdown Brand -> pilih option dgn text == game_name.
    Element-Plus el-select: click wrapper -> popper muncul di body level.
    Flow: click wrapper -> tunggu popper visible -> kalau option ndak langsung
    ketemu, coba ketik ke search input (filterable) -> click option exact match."""
    add_log(f"[IGV] Pilih brand (game): '{game_name}'")

    # Click wrapper div (lebih reliable dari click inner readonly input)
    wrapper = page.locator(
        "div.el-input__wrapper:has(input[placeholder='Please select brand'])"
    ).first
    wrapper.wait_for(state="visible", timeout=15000)
    wrapper.click()
    smart_wait(page, 600, 1000)

    # Tunggu popper muncul (di body level biasanya). Coba beberapa selector.
    popper = None
    for sel in [
        "div.el-select__popper.el-popper:visible",
        "div.el-select-dropdown:visible",
        "div.el-popper[role='tooltip']:visible",
    ]:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=3000)
            popper = loc
            break
        except Exception:
            continue
    if popper is None:
        raise Exception("Popper dropdown brand tidak muncul setelah click")

    # Type game_name via keyboard -> Element-Plus filterable select auto-filter
    # list ke option yg match. Lebih reliable dari click option di list panjang
    # (dropdown brand IGV = ratusan game, virtual scroll).
    add_log(f"[IGV] Ketik '{game_name}' untuk filter dropdown")
    page.keyboard.type(game_name, delay=30)
    smart_wait(page, 600, 1000)

    # Case-insensitive EXACT match - handle kasus:
    #   sheet: 'Arena breakout'  vs  IGV options: 'Arena Breakout', 'Arena Breakout Infinite'
    # XPath exact match case-sensitive gagal. Ambil semua option visible -> compare lowercase
    # di Python -> click yg match exact (case-insensitive).
    target_lower = game_name.strip().lower()
    option_list_loc = page.locator("li.el-select-dropdown__item:visible")
    # Wait at least 1 option visible (filter animation)
    try:
        option_list_loc.first.wait_for(state="visible", timeout=6000)
    except Exception:
        raise Exception(f"Option list ndak muncul setelah type '{game_name}'")

    items = option_list_loc.all()
    texts = [it.inner_text().strip() for it in items]
    matched_idx = None
    for i, t in enumerate(texts):
        if t.lower() == target_lower:
            matched_idx = i
            break

    if matched_idx is None:
        preview = " | ".join(texts[:10])
        add_log(f"[IGV] Options setelah filter '{game_name}' (first 10): {preview or '(kosong)'}")
        raise Exception(
            f"Option '{game_name}' tidak exact-match di dropdown (case-insensitive). "
            f"Ada {len(texts)} kandidat, cek log above."
        )

    matched_text = texts[matched_idx]
    if matched_text != game_name:
        add_log(f"[IGV] Exact-match (case-ins): '{game_name}' -> '{matched_text}'")
    items[matched_idx].click()
    smart_wait(page, 500, 1000)


def _pick_category_accounts(page):
    """Radio 'Accounts' di section Product category."""
    add_log("[IGV] Pilih Product category: Accounts")
    # Match label yg punya span.el-radio__label dengan text persis 'Accounts'
    label = page.locator(
        "label.el-radio:has(span.el-radio__label:text-is('Accounts'))"
    ).first
    label.wait_for(state="visible", timeout=10000)
    label.click()
    smart_wait(page, 300, 600)


def _click_next_step1(page):
    """Tombol Next di bawah Listing Method -> navigate ke ?step=2."""
    add_log("[IGV] Klik Next (Step 1 -> Step 2)")
    btn = page.locator(
        "button.el-button--primary.el-button--large:has-text('Next')"
    ).first
    btn.wait_for(state="visible", timeout=10000)
    btn.click()
    # Navigate ke step 2 - beri waktu untuk URL change + render form.
    try:
        page.wait_for_url(IGV_STEP2_URL_PATTERN, timeout=15000)
        add_log("[IGV] Navigasi ke Step 2 sukses")
    except Exception:
        add_log(f"[IGV] URL belum pindah ke step=2 (current: {page.url})")
    smart_wait(page, 1500, 2500)


# ===================== STEP 2 HELPERS =====================
def _fill_title(page, title):
    """Isi Title di section 'Graphic and text description'. Input standar
    el-input, maxlength=144 (IGV auto-truncate kalau title lebih panjang)."""
    preview = title[:60] + ("..." if len(title) > 60 else "")
    add_log(f"[IGV] Isi Title: '{preview}' ({len(title)} char)")

    section = page.locator(
        "div.module-item:has(h4.module-item-title:text-is('Graphic and text description'))"
    ).first
    section.wait_for(state="visible", timeout=10000)

    # Form-item Title: label punya text 'Title' (setelah asterisk bold).
    title_input = section.locator(
        "div.el-form-item:has(.el-form-item__label:has-text('Title')) input.el-input__inner"
    ).first
    title_input.wait_for(state="visible", timeout=10000)
    title_input.click()
    smart_wait(page, 300, 500)
    title_input.fill(title)
    smart_wait(page, 400, 700)


def _upload_product_images(page, image_paths):
    """Upload gambar di section 'Graphic and text description' > Product image.
    File input: input.el-upload__input (multiple, accept png/jpg/jpeg/mp4).
    Max file dibatasi caller via MAX_IMAGES."""
    if not image_paths:
        add_log("[IGV] Product image: ndak ada gambar, skip")
        return 0

    section = page.locator(
        "div.module-item:has(h4.module-item-title:text-is('Graphic and text description'))"
    ).first
    section.wait_for(state="visible", timeout=10000)

    # File input di form-item Product image (bukan Characters Pictures di Game Details).
    # Scope: form-item yg punya label 'Product image'.
    file_input = section.locator(
        "div.el-form-item:has(.el-form-item__label:has-text('Product image')) "
        "input.el-upload__input[type='file']"
    ).first

    paths = list(image_paths)[:IGV_MAX_IMAGES]
    add_log(f"[IGV] Upload Product image: {len(paths)} file")
    try:
        file_input.set_input_files(paths)
    except Exception as e:
        add_log(f"[IGV] set_input_files gagal: {str(e)[:80]}")
        return 0

    # Tunggu upload SELESAI - bukan cuma item muncul. Element-Plus el-upload
    # punya class status: is-uploading (progress) -> is-success (done) /
    # is-error (fail). Poll sampai semua item status 'is-success' atau
    # tidak ada yg 'is-uploading' lagi.
    deadline = 30  # detik
    poll_interval = 1.0
    item_base = (
        "div.el-form-item:has(.el-form-item__label:has-text('Product image')) "
        "ul.el-upload-list li.el-upload-list__item"
    )
    expected = len(paths)
    success_count = 0
    for _ in range(int(deadline / poll_interval)):
        smart_wait(page, int(poll_interval * 1000), int(poll_interval * 1000))
        try:
            total = section.locator(item_base).count()
            success_count = section.locator(f"{item_base}.is-success").count()
            uploading_count = section.locator(f"{item_base}.is-uploading").count()
        except Exception:
            total = 0
            success_count = 0
            uploading_count = 1  # assume still uploading
        # Done jika: semua item sukses, atau total>=expected & ndak ada yg uploading
        if success_count >= expected:
            add_log(f"[IGV] Upload Product image OK: {success_count}/{expected} sukses")
            page.wait_for_timeout(1000)  # settle delay sebelum flow lanjut
            return success_count
        if total >= expected and uploading_count == 0:
            add_log(
                f"[IGV] Upload selesai (tanpa is-uploading aktif): "
                f"sukses={success_count}/{expected}"
            )
            page.wait_for_timeout(1000)  # settle delay sebelum flow lanjut
            return success_count
    add_log(
        f"[IGV] Upload Product image timeout: "
        f"sukses={success_count}/{expected}, lanjut (mungkin partial)"
    )
    return success_count


def _fill_description_jodit(page, description, raw_image_url):
    """Jodit WYSIWYG editor di section 'Graphic and text description' > Product
    Description. Format body:
        Full Screenshot Detail: {url-no-scheme}
        {description body}

    Pakai JS innerHTML injection (instant paste) bukan keyboard typing, supaya
    ndak race dengan flow selanjutnya (typing lambat bikin Next di-click sebelum
    deskripsi selesai -> tulisan terpotong). Trigger input+change event supaya
    Jodit tahu ada perubahan & update internal state.
    """
    if raw_image_url:
        raw_line = re.sub(r"^https?://", "", raw_image_url.strip())
        body_text = f"Full Screenshot Detail: {raw_line}\n{description or ''}"
    else:
        body_text = description or ""

    add_log(f"[IGV] Isi Product Description (Jodit editor, {len(body_text)} char)")

    section = page.locator(
        "div.module-item:has(h4.module-item-title:text-is('Graphic and text description'))"
    ).first

    editor_sel = (
        "div.el-form-item:has(.el-form-item__label:has-text('Product Description')) "
        "div.jodit-wysiwyg"
    )
    editor = section.locator(editor_sel).first
    editor.wait_for(state="visible", timeout=10000)
    editor.click()
    smart_wait(page, 200, 400)

    # Paste via JS: set innerHTML langsung di element handle (bukan via
    # document.querySelector karena Playwright :has-text() bukan native CSS).
    js = r"""
    (el, text) => {
      const esc = (s) => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const html = text.split('\n').map(l => l ? '<p>'+esc(l)+'</p>' : '<p><br></p>').join('');
      el.innerHTML = html;
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
      return 'ok(' + el.innerText.length + ' chars)';
    }
    """
    try:
        result = editor.evaluate(js, body_text)
        add_log(f"[IGV] Jodit paste result: {result}")
    except Exception as e:
        add_log(f"[IGV] Jodit paste gagal ({str(e)[:80]}), fallback type")
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        editor.type(body_text, delay=3)
    smart_wait(page, 300, 500)


def _click_next_step2(page):
    """Tombol Next di bawah Step 2 -> navigate ke ?step=3. Multi-selector karena
    layout Step 2 ada tombol Previous di sebelah yg juga primary class -
    pakai :text-is exact + pilih last match (Next ada di paling kanan)."""
    add_log("[IGV] Klik Next (Step 2 -> Step 3)")
    btn = None
    for sel in [
        # Exact text + primary large (paling spesifik)
        "button.el-button--primary.el-button--large >> span:text-is('Next')",
        # Span child 'Next' (handle layout dengan <span>Next<img/></span>)
        "button.el-button--primary:has(span:text-is('Next'))",
        # Fallback: any primary large dengan text Next (last match = rightmost)
        "button.el-button--primary.el-button--large:has-text('Next')",
    ]:
        try:
            loc = page.locator(sel).last
            loc.wait_for(state="visible", timeout=3000)
            btn = loc
            break
        except Exception:
            continue
    if btn is None:
        raise Exception("Tombol Next Step 2 tidak ketemu")
    btn.click()
    try:
        page.wait_for_url(IGV_STEP3_URL_PATTERN, timeout=15000)
        add_log("[IGV] Navigasi ke Step 3 sukses")
    except Exception:
        add_log(f"[IGV] URL belum pindah ke step=3 (current: {page.url})")
    smart_wait(page, 1500, 2500)


# ===================== STEP 3 HELPERS =====================
def _fill_delivery_method(page, login_name=None):
    """Section 'Delivery method' di Step 3. Flow per HTML kompleks:
      - Shipping method radio (Immediate shipping) biasanya udah ke-check default -> skip
      - Delivery time: disabled/auto -> skip
      - Field dinamis (Account, Password, Alternate Mail, dll - jumlah berbeda
        per game): field PERTAMA isi `login_name` (kolom B sheet), field ke-2+
        isi `IGV_DELIVERY_FIX_TEXT`. Kalau login_name kosong, semua pakai fix text.

    Strategi: loop semua is-required form-item di section -> fill kalau ada
    input text enabled (non-readonly, non-disabled). Skip radio/select/disabled.
    """
    section = page.locator(
        "div.module-item:has(h4.module-item-title:text-is('Delivery method'))"
    ).first
    section.wait_for(state="visible", timeout=10000)

    required_items = section.locator("div.el-form-item.is-required").all()
    add_log(f"[IGV] Delivery method: {len(required_items)} field required")

    filled = 0
    for item in required_items:
        try:
            label = item.locator(".el-form-item__label").first.inner_text().strip()
        except Exception:
            label = "?"

        # Skip kalau ndak ada input text (radio/select-only form-item)
        # Skip juga kalau input-nya disabled/readonly (misal Delivery time)
        input_el = item.locator(
            "input.el-input__inner:not([disabled]):not([readonly])"
        ).first
        if input_el.count() == 0:
            add_log(f"[IGV] Skip '{label}' (bukan input text enabled)")
            continue

        # Field pertama = login_name (kalau ada). Sisanya = fix text.
        if filled == 0 and login_name:
            value = login_name
            tag = f"login_name='{login_name}'"
        else:
            value = IGV_DELIVERY_FIX_TEXT
            tag = "fix text"

        try:
            input_el.wait_for(state="visible", timeout=5000)
            input_el.click()
            smart_wait(page, 200, 400)
            input_el.fill(value)
            smart_wait(page, 200, 400)
            add_log(f"[IGV] Fill delivery '{label}' = {tag}")
            filled += 1
        except Exception as e:
            add_log(f"[IGV] Fill delivery '{label}' gagal: {str(e)[:80]}")

    add_log(f"[IGV] Delivery method: {filled} field ter-isi")


def _fill_price(page, harga):
    """Price input di Step 3 - type=number, min=0. Scope: 1 input type=number
    di page (assumption: cuma 1 price field di step 3).
    Strip non-numeric ('$', ',') supaya valid untuk input[type=number]."""
    raw = str(harga).strip() if harga is not None else ""
    if not raw:
        add_log("[IGV] Harga kosong, skip fill price")
        return
    # Clean: keep only digit + dot + comma, convert comma to dot, dedupe dots
    price_clean = re.sub(r"[^0-9.,]", "", raw).replace(",", ".")
    if price_clean.count(".") > 1:
        parts = price_clean.split(".")
        price_clean = "".join(parts[:-1]) + "." + parts[-1]
    if not price_clean:
        add_log(f"[IGV] Harga '{raw}' ke-strip jadi kosong, skip")
        return
    # IGV minimum $5 - override kalau harga sumber lebih rendah
    try:
        if float(price_clean) < 5:
            add_log(f"[IGV] Harga sumber ${price_clean} < $5 minimum, override ke $5")
            price_clean = "5"
    except (ValueError, TypeError):
        pass
    add_log(f"[IGV] Fill Price (USD): {price_clean} (raw='{raw}')")
    price_input = page.locator("input.el-input__inner[type='number']").first
    try:
        price_input.wait_for(state="visible", timeout=10000)
        price_input.click()
        smart_wait(page, 200, 400)
        price_input.fill(price_clean)
        smart_wait(page, 400, 700)
    except Exception as e:
        add_log(f"[IGV] Fill Price gagal: {str(e)[:80]}")


def _select_warranty_period(page, option=IGV_WARRANTY_OPTION):
    """Section 'Insurance' > Warranty Period dropdown -> pilih option."""
    add_log(f"[IGV] Select Warranty Period: '{option}'")
    section = page.locator(
        "div.module-item:has(h4.module-item-title:text-is('Insurance'))"
    ).first
    section.wait_for(state="visible", timeout=10000)

    wrapper = section.locator("div.el-input__wrapper").first
    wrapper.wait_for(state="visible", timeout=10000)
    wrapper.click()
    smart_wait(page, 500, 800)

    opt_xpath = (
        "//li[contains(@class,'el-select-dropdown__item')]"
        f"[normalize-space(.)={_xpath_literal(option)}]"
    )
    opt = page.locator(f"xpath={opt_xpath}").first
    try:
        opt.wait_for(state="visible", timeout=6000)
        opt.click()
        smart_wait(page, 400, 700)
        add_log(f"[IGV] Warranty '{option}' selected")
    except Exception as e:
        add_log(f"[IGV] Warranty '{option}' gagal dipilih: {str(e)[:80]}")


def _click_post_product(page):
    """Tombol 'Post product' -> verify redirect ke /product/my atau toast success.
    Return (ok: bool, err_msg: str)."""
    add_log("[IGV] Klik Post product")
    btn = page.locator(
        "button.el-button--primary.el-button--large:has-text('Post product')"
    ).first
    try:
        btn.wait_for(state="visible", timeout=10000)
        btn.click()
    except Exception as e:
        return False, f"Klik Post product gagal: {str(e)[:80]}"

    # Verify: redirect OR Element-Plus success message
    try:
        page.wait_for_url(IGV_SUCCESS_URL_PATTERN, timeout=20000)
        add_log("[IGV] Redirect ke /product/my - post sukses")
        page.wait_for_timeout(1000)  # delay 1s sebelum tab close (settle)
        return True, None
    except Exception:
        pass

    # Fallback: cek Element-Plus success toast (.el-message--success)
    try:
        toast = page.locator(".el-message--success, .el-notification--success").first
        toast.wait_for(state="visible", timeout=8000)
        add_log("[IGV] Toast success IGV muncul - post sukses")
        page.wait_for_timeout(1000)  # delay 1s sebelum tab close (settle)
        return True, None
    except Exception:
        pass

    # Cek toast error kalau ada
    try:
        err_toast = page.locator(".el-message--error, .el-notification--error").first
        if err_toast.count() > 0 and err_toast.is_visible():
            err_text = (err_toast.inner_text() or "").strip()[:120]
            return False, f"IGV toast error: {err_text}"
    except Exception:
        pass

    return False, f"Submit timeout - URL masih {page.url[:80]}, toast ndak muncul"


def _fill_game_details(page, schema, values):
    """Fill Game Details section sesuai schema + AI-mapped values.
    Per field:
      - select: click trigger -> click option exact match dari popper
      - input:  click -> fill value string
    Fallback kalau selector atau option ndak ketemu: log + skip field (biar
    field lain tetap terisi; error handling di caller via exception).
    """
    section = page.locator(
        "div.module-item:has(h4.module-item-title:text-is('Game Details'))"
    ).first
    section.wait_for(state="visible", timeout=10000)

    for label, meta in schema.items():
        value = values.get(label, "1")
        # Locate form-item by label text
        item = section.locator(
            f"div.el-form-item:has(label.el-form-item__label:text-is({_xpath_literal(label)}))"
        ).first
        if item.count() == 0:
            add_log(f"[IGV] Fill: form-item '{label}' tidak ketemu, skip")
            continue

        try:
            if meta.get("type") == "select":
                add_log(f"[IGV] Fill [select] '{label}' = '{value}'")
                wrapper = item.locator("div.el-input__wrapper").first
                wrapper.click()
                smart_wait(page, 400, 700)
                # Click option by exact text
                opt_xpath = (
                    "//li[contains(@class,'el-select-dropdown__item')]"
                    f"[normalize-space(.)={_xpath_literal(value)}]"
                )
                option = page.locator(f"xpath={opt_xpath}").first
                option.wait_for(state="visible", timeout=5000)
                option.click()
                smart_wait(page, 300, 600)
            else:
                add_log(f"[IGV] Fill [input]  '{label}' = '{value}'")
                input_el = item.locator("input.el-input__inner").first
                input_el.wait_for(state="visible", timeout=5000)
                input_el.click()
                smart_wait(page, 200, 400)
                input_el.fill(str(value))
                smart_wait(page, 200, 400)
        except Exception as e:
            add_log(f"[IGV] Fill '{label}' gagal: {str(e)[:80]}")


def _scrape_game_details_required(page):
    """Scrape section 'Game Details' di Step 2. Ambil HANYA field required
    (class is-required di div.el-form-item). Return dict:
        {label: {"type": "select", "options": [...]}}   # dropdown
        {label: {"type": "input"}}                        # input text/number
    Field optional (tanpa is-required) di-skip. Upload fields di-skip juga
    (ndak punya input.el-input__inner maupun div.el-select).
    """
    # Scope ke section Game Details via h4 heading
    try:
        section = page.locator(
            "div.module-item:has(h4.module-item-title:text-is('Game Details'))"
        ).first
        section.wait_for(state="visible", timeout=10000)
    except Exception:
        add_log("[IGV] Section 'Game Details' tidak ketemu di Step 2")
        return {}

    result = {}
    required_items = section.locator("div.el-form-item.is-required").all()
    add_log(f"[IGV] Game Details: {len(required_items)} field required")

    for item in required_items:
        try:
            label = item.locator(".el-form-item__label").first.inner_text().strip()
        except Exception:
            continue
        if not label:
            continue

        has_select = item.locator("div.el-select").count() > 0
        has_input = item.locator("input.el-input__inner").count() > 0

        if has_select:
            # Options ada di DOM meski popper display:none - direct scrape.
            texts = item.locator(
                "ul.el-select-dropdown__list li.el-select-dropdown__item span"
            ).all_text_contents()
            options = [t.strip() for t in texts if t.strip()]
            result[label] = {"type": "select", "options": options}
            preview = ", ".join(options[:5]) + ("..." if len(options) > 5 else "")
            add_log(f"[IGV]   [select] '{label}' -> {len(options)} opt: {preview}")
        elif has_input:
            result[label] = {"type": "input"}
            add_log(f"[IGV]   [input]  '{label}'")
        else:
            add_log(f"[IGV]   [skip]   '{label}' (tipe field tidak dikenali)")

    return result


# ===================== AI MAPPING (IGV-specific) =====================
def _ai_map_igv_fields(title, form_schema):
    """Gemini call sendiri untuk IGV - handle mix select + input dalam 1 prompt.
    Shared ai_map_fields_multi cuma support select dropdown (flat format).

    Args:
        title: product title string (dari sheet)
        form_schema: dict {label: {"type": "select", "options": [...]} atau
                                  {"type": "input"}}

    Return: dict {label: value_string}. Fallback '1' untuk field yg AI ndak
    jawab atau response invalid.
    """
    if not form_schema:
        return {}

    # Build field description untuk prompt
    lines = []
    for label, meta in form_schema.items():
        if meta.get("type") == "select":
            opts = meta.get("options", [])
            lines.append(f"- {label} (select from: {opts})")
        else:
            lines.append(f"- {label} (number/text input, generate from title)")

    prompt = f"""You are a form-filling assistant for IGV game account marketplace.

Product title: {title}

Fill these fields based on the title:
{chr(10).join(lines)}

Rules:
- For select fields: return EXACT option from the provided list.
- For input fields: extract number/value from title. If not found in title, return "1".
- Analyze title carefully (server, rank/level, counts, amounts).
- Return ONLY valid JSON, no markdown, no explanation.

Example response shape (replace values with your actual choices):
{json.dumps({k: "example_value" for k in form_schema}, ensure_ascii=False)}
"""

    model = _get_gemini_model()
    if model is None:
        add_log("[IGV] AI: Gemini model ndak tersedia, fallback '1' semua input")
        return {k: (v.get("options", ["?"])[0] if v.get("type") == "select"
                    else "1") for k, v in form_schema.items()}

    add_log(f"[IGV] AI: Gemini call untuk {len(form_schema)} field")
    try:
        response = call_with_timeout(
            fn=lambda: model.generate_content(prompt),
            timeout=30, name="igv_gemini_map",
        )
        raw = (response.text or "").strip()
        # Strip markdown fence kalau Gemini bandel
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("AI response bukan JSON object")
    except TimeoutHangError:
        add_log("[IGV] AI timeout (>30s), fallback ke default")
        parsed = {}
    except Exception as e:
        add_log(f"[IGV] AI gagal ({str(e)[:80]}), fallback ke default")
        parsed = {}

    # Validasi + fallback per field
    result = {}
    for label, meta in form_schema.items():
        val = parsed.get(label)
        if meta.get("type") == "select":
            opts = meta.get("options", [])
            if val in opts:
                result[label] = val
            else:
                # AI kasih value ndak valid atau ndak jawab - pakai option pertama
                fb = opts[0] if opts else "?"
                add_log(f"[IGV] AI '{label}': value '{val}' invalid, fallback '{fb}'")
                result[label] = fb
        else:
            # Input: ambil string, fallback "1" kalau kosong/None
            sval = str(val).strip() if val is not None else ""
            if not sval:
                sval = "1"
            result[label] = sval

    preview = ", ".join(f"{k}={v}" for k, v in list(result.items())[:3])
    add_log(f"[IGV] AI hasil ({len(result)} field): {preview}...")
    return result


# ===================== PUBLIC ENTRIES =====================
def scrape_form_options(game_name):
    """IGV ndak pakai shared ai_map_fields_multi karena formnya mix select +
    input (shared cuma handle select). Scrape + AI mapping dilakukan di dalam
    create_listing saat sudah di step=2 (DOM parse = instant, ndak butuh
    navigate tambahan).

    Return {} -> bot_create anggap 'skip shared AI', tulis sentinel ke cache
    cell. Next cycle langsung call create_listing.
    """
    add_log(f"[IGV] scrape_form_options: bypass shared AI (IGV handle own mapping)")
    return {}


def cache_looks_bogus(cache):
    """Belum ada aturan invalidasi cache. Return False = cache selalu valid."""
    return False


def create_listing(game_name, title, deskripsi, harga, field_mapping, image_paths,
                   raw_image_url=None, image_future=None, login_name=None):
    """Full IGV create flow. Return (ok, err, uploaded_count).

    `image_future` (optional): kalau disediain, gambar di-resolve via future
    tepat sebelum step upload (async download pattern). Fallback ke
    `image_paths` kwarg kalau future None (legacy).

    `login_name` (optional): value dari kolom B sheet. Dipakai untuk fill
    field PERTAMA di Delivery method (field ke-2+ pakai IGV_DELIVERY_FIX_TEXT)."""
    uploaded = 0
    cdp_url = _get_chrome_cdp_url()
    if not cdp_url:
        return False, "CDP URL tidak tersedia (chrome belum ready)", uploaded

    with sync_playwright() as p:
        page = None
        try:
            browser = p.chromium.connect_over_cdp(cdp_url, timeout=10000)
            context = get_or_create_context(browser)
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()

            add_log(f"[IGV] Goto {IGV_START_URL}")
            page.goto(IGV_START_URL, wait_until="domcontentloaded", timeout=30000)
            smart_wait(page, 2000, 4000)

            # Step 1: pick game -> Accounts -> Next
            _pick_brand_game(page, game_name)
            _pick_category_accounts(page)
            _click_next_step1(page)

            # Step 2a: scrape required fields -> AI map -> fill Game Details
            schema = _scrape_game_details_required(page)
            if schema:
                values = _ai_map_igv_fields(title, schema)
                _fill_game_details(page, schema, values)
            else:
                add_log("[IGV] Game Details: tidak ada required field, skip fill")

            # Step 2b: fill Title
            _fill_title(page, title)

            # Resolve image future (block sampai download selesai kalau pakai
            # async flow). Fallback ke image_paths kwarg lama kalau future None.
            if image_future is not None:
                try:
                    resolved_paths, _, _ = resolve_image_future(image_future)
                    image_paths = resolved_paths
                except RuntimeError as e:
                    return False, str(e), uploaded
            if not image_paths:
                return False, "Gambar tidak bisa di download", uploaded

            # Step 2c: upload gambar (max 5)
            uploaded = _upload_product_images(page, image_paths)

            # Step 2d: fill Product Description (Jodit editor)
            _fill_description_jodit(page, deskripsi, raw_image_url)

            # Step 2e: Next -> Step 3
            _click_next_step2(page)

            # Step 3a: Delivery method (fix text untuk semua required input)
            _fill_delivery_method(page, login_name=login_name)

            # Step 3b: Price (USD dari kolom H)
            _fill_price(page, harga)

            # Step 3c: Insurance > Warranty Period
            _select_warranty_period(page, IGV_WARRANTY_OPTION)

            # Step 3d: Post product + verify redirect/toast
            ok, err = _click_post_product(page)
            if ok:
                return True, None, uploaded
            return False, err or "Post product gagal", uploaded

        except Exception as e:
            msg = str(e)
            if "Timeout" in msg:
                indo_error = "Timeout - element tidak ketemu / page lambat"
            elif "net::" in msg:
                indo_error = "Gagal load page (koneksi?)"
            else:
                indo_error = f"Error: {msg[:100]}"
            add_log(f"[IGV] Gagal: {indo_error}")
            return False, indo_error, uploaded

        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


def run(sheet, baris_nomor, worker_id, *, game_name, description, title, harga,
        field_mapping, image_paths=None, image_urls=None,
        raw_image_url=None, is_imgur=False, image_future=None):
    """Adapter entry dipanggil orchestrator. Return (ok, k_line).

    Baca kolom B (login_name) dari sheet untuk diisi ke field pertama
    Delivery method di Step 3."""
    _worker_local.worker_id = f"{worker_id}-IGV"

    login_name = None
    try:
        raw = sheet.cell(baris_nomor, 2).value  # B = col 2
        if raw is not None:
            login_name = str(raw).strip()
    except Exception as e:
        add_log(f"[IGV] Gagal baca kolom B: {str(e)[:80]}")

    ok, err, uploaded = create_listing(
        game_name, title, description or "", harga,
        field_mapping or {}, (image_paths or [])[:MAX_IMAGES] if image_paths else None,
        raw_image_url=raw_image_url,
        image_future=image_future,
        login_name=login_name,
    )
    ts = datetime.now().strftime("%d %b, %y | %H:%M")
    if ok:
        return True, f"✅ [IGV] | {uploaded} images uploaded | {ts}"
    return False, f"❌ [IGV] | {err or 'gagal'}"
