"""create/GB.py - GameBoost.com marketplace adapter.

Entry points dipakai orchestrator (bot_create.py):
- scrape_form_options(game_name) -> dict | {} | None
- create_listing(...) -> (ok, err, uploaded_count)
- run(sheet, baris_nomor, worker_id, **kwargs) -> (ok, k_line)
- cache_looks_bogus(cache) -> bool

Flow GameBoost (3-step wizard popup):
  Step 1 - Listing Info: Title (J), Currency=USD, Price (H), Description
           (ZEUS-style raw URL line 1 no scheme), Upload bulk max 20 -> Continue
  Step 2 - Game Data: dynamic multiselect dropdowns (AI mapping). Empty number
           inputs di-fill '1'. -> Continue
  Step 3 - Credentials: pilih Manual Delivery radio, delivery time input '60'
           -> klik 'Add Account'
  Success: Vue-Toastification toast 'Account has been created successfully.'
           + redirect ke /dashboard/accounts/{id}
"""

import json
import random
import re
from datetime import datetime
from playwright.sync_api import sync_playwright

from create._shared import (
    _worker_local,
    _log as add_log,
    _get_chrome_debug_port,
    _get_gemini_model,
    xpath_literal as _xpath_literal,
    smart_wait as _base_smart_wait,
    get_or_create_context,
    resolve_image_future,
)
from shared import call_with_timeout, TimeoutHangError


# GB throttle: 1.0× + 0-10% jitter (baseline).
_GB_SLOW_MULT      = 1.0
_GB_JITTER_MAX     = 0.10


def smart_wait(page, min_ms, max_ms):
    """GB-local smart_wait: base × 1.0 + random 0-10% jitter."""
    jitter = 1.0 + random.uniform(0, _GB_JITTER_MAX)
    lo = int(min_ms * _GB_SLOW_MULT * jitter)
    hi = int(max_ms * _GB_SLOW_MULT * jitter)
    if hi < lo:
        hi = lo
    _base_smart_wait(page, lo, hi)


# ===================== KONSTANTA =====================
GB_ORIGIN          = "https://gameboost.com"
GB_START_URL       = "https://gameboost.com/dashboard/accounts"
GB_MAX_IMAGES      = 20
GB_DELIVERY_MINUTES = "60"

# Adapter protocol:
MARKET_CODE        = "GB"
HARGA_COL          = 8                                 # H
MAX_IMAGES         = GB_MAX_IMAGES
NO_OPTIONS_SENTINEL_GB = "[tidak ditemukan options GB]"
CACHE_SENTINEL     = NO_OPTIONS_SENTINEL_GB


# ===================== TAB TITLE =====================
def _set_worker_tab_title(page):
    """Inject prefix 'Worker N | ' ke document.title."""
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


# ===================== STEP 1 HELPERS =====================
def _click_add_new_account(page):
    """Klik tombol 'Add New Account' -> buka modal game picker."""
    add_log("[GB] Klik Add New Account")
    for sel in [
        "xpath=//button[.//i[contains(@class,'fa-plus')] and contains(normalize-space(),'Add New Account')]",
        "xpath=//button[contains(normalize-space(),'Add New Account')]",
        "button:has-text('Add New Account')",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=8000)
            loc.click()
            smart_wait(page, 1200, 2000)
            return
        except Exception:
            continue
    raise Exception("Tombol 'Add New Account' tidak ketemu")


def _select_game_from_modal(page, game_name):
    """Setelah Add New Account, modal game picker muncul dgn search input.
    Ketik game name, klik card yg match -> side panel listing info kebuka.
    Strategi: scope ke role=dialog, klik card yg contains game_name. Setelah
    ketik, daftar terfilter - default click first card match."""
    add_log(f"[GB] Pilih Game: {game_name}")

    # Scope ke role=dialog biar ndak kena input 'Search...' di header page.
    # Halaman Accounts juga punya search 'Search... /' yg ndak ada hubungannya.
    search = None
    for sel in [
        "xpath=//div[@role='dialog']//input[@placeholder='Search...' and @type='text']",
        "xpath=//*[@data-state='open']//input[@placeholder='Search...' and @type='text']",
        "div[role='dialog'] input[placeholder='Search...']",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=8000)
            search = loc
            break
        except Exception:
            continue
    # Fallback: .last (asumsi search modal muncul SETELAH search page, jadi last match biasanya yg baru)
    if search is None:
        try:
            loc = page.locator("input[placeholder='Search...'][type='text']").last
            loc.wait_for(state="visible", timeout=5000)
            search = loc
        except Exception:
            pass
    if search is None:
        raise Exception("Search input game picker tidak ketemu")

    try:
        search.click(timeout=3000)
    except Exception:
        pass
    smart_wait(page, 200, 400)
    # Clear dulu via triple-click select-all + delete
    try:
        search.fill("")
    except Exception:
        try:
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
        except Exception:
            pass
    # Ketik: prefer fill() yg lebih reliable, fallback keyboard.type
    typed = False
    try:
        search.fill(game_name)
        typed = True
    except Exception:
        pass
    if not typed:
        try:
            search.focus()
            page.keyboard.type(game_name, delay=40)
            typed = True
        except Exception:
            pass
    if not typed:
        raise Exception("Gagal ketik di search input")
    smart_wait(page, 900, 1500)

    # Strategi 1: scope ke dialog + get_by_role button dgn accessible name
    clicked = False
    try:
        dialog = page.get_by_role("dialog").first
        try:
            btn = dialog.get_by_role("button", name=game_name, exact=True).first
            btn.wait_for(state="visible", timeout=2000)
            btn.click()
            clicked = True
        except Exception:
            pass
        if not clicked:
            # Fuzzy: substring match
            try:
                btn = dialog.get_by_role("button", name=game_name).first
                btn.wait_for(state="visible", timeout=2000)
                btn.click()
                clicked = True
            except Exception:
                pass
    except Exception:
        pass

    # Strategi 2: scope ke dialog + filter by text
    if not clicked:
        try:
            dialog = page.locator("div[role='dialog']").first
            card = dialog.locator("button, a, [role='button']").filter(has_text=game_name).first
            card.wait_for(state="visible", timeout=2000)
            card.click()
            clicked = True
        except Exception:
            pass

    # Strategi 3: generic text match + ancestor clickable
    if not clicked:
        g_lit = _xpath_literal(game_name)
        for sel in [
            f"xpath=(//*[normalize-space(text())={g_lit}]/ancestor-or-self::*[self::button or self::a or @role='button' or contains(@class,'cursor-pointer')])[1]",
            f"xpath=//button[normalize-space()={g_lit}]",
            f"xpath=//button[.//*[normalize-space()={g_lit}]]",
            f"xpath=//a[normalize-space()={g_lit} or .//*[normalize-space()={g_lit}]]",
        ]:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=2000)
                loc.click()
                clicked = True
                break
            except Exception:
                continue

    # Strategi 4: fuzzy fallback - klik card pertama di dialog (setelah filter
    # search, hasil pertama seharusnya yg paling relevan)
    if not clicked:
        try:
            dialog = page.locator("div[role='dialog']").first
            first_card = dialog.locator(
                "xpath=.//button[.//img or .//span] | .//a[.//img or .//span]"
            ).first
            first_card.wait_for(state="visible", timeout=2000)
            txt = (first_card.inner_text(timeout=1000) or "").strip()
            add_log(f"[GB] Fallback: klik card pertama ({txt[:40]})")
            first_card.click()
            clicked = True
        except Exception:
            pass

    if not clicked:
        raise Exception(f"Card game '{game_name}' tidak ketemu di modal")

    smart_wait(page, 1500, 2500)


def _fill_title(page, title):
    """Step 1: fill Title (kolom J) ke Account Title textarea."""
    add_log(f"[GB] Isi Title: {title[:60]}")
    for sel in [
        "textarea[placeholder*='Platinum' i]",
        "textarea[placeholder*='EUW']",
        "xpath=//textarea[@rows='1' and contains(@class,'block') and contains(@class,'w-full')]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=5000)
            loc.fill(title)
            smart_wait(page, 300, 500)
            return
        except Exception:
            continue
    raise Exception("Title textarea tidak ketemu")


def _select_currency_usd(page):
    """Pilih USD di dropdown Currency (native <select>)."""
    add_log("[GB] Pilih Currency: USD")
    for sel in [
        "select[aria-label='Currency']",
        "xpath=//select[@aria-label='Currency']",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=5000)
            loc.select_option("USD")
            smart_wait(page, 300, 500)
            return
        except Exception:
            continue
    raise Exception("Currency select tidak ketemu")


def _fill_price(page, harga):
    """Fill price input (kolom H). Input berada di-group sama currency select."""
    price_clean = re.sub(r"[^0-9.,]", "", str(harga)).replace(",", ".")
    if price_clean.count(".") > 1:
        parts = price_clean.split(".")
        price_clean = "".join(parts[:-1]) + "." + parts[-1]
    # GB minimum $1.99 - override kalau harga sumber lebih rendah
    try:
        if float(price_clean) < 1.99:
            add_log(f"[GB] Harga sumber ${price_clean} < $1.99 minimum, override ke $1.99")
            price_clean = "1.99"
    except (ValueError, TypeError):
        pass
    add_log(f"[GB] Isi Price: ${price_clean}")
    # Scope: input sibling dari select[aria-label='Currency']
    for sel in [
        "xpath=//select[@aria-label='Currency']/preceding::input[@type='text'][1]",
        "xpath=//select[@aria-label='Currency']/../input[@type='text']",
        "input[type='text'][placeholder=''][class*='rounded-e-none']",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=5000)
            loc.fill(price_clean)
            smart_wait(page, 300, 500)
            return
        except Exception:
            continue
    raise Exception("Price input tidak ketemu")


def _fill_description(page, body_text):
    """Fill Description textarea (3-row, placeholder 'Mention the details...')."""
    add_log("[GB] Isi Description")
    for sel in [
        "textarea[placeholder*='Mention the details']",
        "xpath=//textarea[@rows='3' and contains(@class,'block') and contains(@class,'w-full')]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=5000)
            loc.fill(body_text)
            smart_wait(page, 300, 500)
            return
        except Exception:
            continue
    raise Exception("Description textarea tidak ketemu")


def _upload_images_bulk(page, paths):
    """Fire-and-forget upload ke drop zone. GB upload jalan di background,
    ndak perlu tunggu - langsung lanjut ke field berikutnya. Return len(paths)
    optimistic."""
    if not paths:
        return 0
    add_log(f"[GB] Upload {len(paths)} gambar (fire-and-forget)")

    # Cari dropzone
    dz = None
    for sel in [
        "div[role='button'][aria-label*='Upload files']",
        "xpath=//div[@role='button' and contains(@aria-label,'Upload')]",
        "xpath=//div[@role='button'][.//span[contains(normalize-space(),'browse')]]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=5000)
            dz = loc
            break
        except Exception:
            continue
    if dz is None:
        raise Exception("Upload dropzone tidak ketemu")

    # Strategi 1: file chooser via dropzone click (paling reliable untuk custom
    # react-dropzone). Strategi 2: set_input_files langsung ke hidden input.
    try:
        with page.expect_file_chooser(timeout=4000) as fc_info:
            dz.click()
        fc = fc_info.value
        fc.set_files(paths)
    except Exception as e1:
        for sel in [
            "div[role='button'][aria-label*='Upload files'] input[type='file']",
            "xpath=//div[@role='button' and contains(@aria-label,'Upload')]//input[@type='file']",
            "xpath=//div[@role='button' and contains(@aria-label,'Upload')]/following::input[@type='file'][1]",
            "input[type='file']",
        ]:
            try:
                page.locator(sel).first.set_input_files(paths)
                break
            except Exception:
                continue
        else:
            raise Exception(f"set_files gagal: {str(e1)[:60]}")

    # Fire-and-forget: upload lanjut di background, bot lanjut ke field lain.
    # Beat singkat biar dropzone sempat proses file selection event.
    smart_wait(page, 400, 700)
    return len(paths)


def _count_gb_thumbnails(page):
    """Hitung preview thumbnail upload. Scope ke sekitar dropzone container
    biar ndak kena logo/game icon di sidebar."""
    for sel in [
        "xpath=//div[@role='button' and contains(@aria-label,'Upload')]/following::img[1]/..//img",
        "xpath=//div[@role='button' and contains(@aria-label,'Upload')]/ancestor::div[1]//img[@src]",
        "xpath=//div[contains(@aria-label,'Upload')]/following-sibling::*//img[@src]",
        "[class*='file-preview'] img",
        "[class*='uploaded'] img",
        "img[src^='blob:']",
        "img[src^='data:image']",
    ]:
        try:
            c = page.locator(sel).count()
            if c > 0:
                return c
        except Exception:
            continue
    return 0


def _click_continue(page, step_label=""):
    """Klik tombol Continue di popup wizard."""
    add_log(f"[GB] Klik Continue {step_label}".strip())
    for sel in [
        "xpath=//button[.//i[contains(@class,'fa-arrow-right')] and contains(normalize-space(),'Continue')]",
        "xpath=//button[contains(normalize-space(),'Continue')]",
        "button:has-text('Continue')",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=10000)
            loc.click()
            smart_wait(page, 1000, 1800)
            return
        except Exception:
            continue
    raise Exception("Tombol Continue tidak ketemu")


# ===================== MULTISELECT HELPERS =====================
def _multiselect_triggers(page):
    """Return list of (label, kind, locator) tuples dari semua dropdown form.
    kind: 'multiselect' (custom) atau 'native_select' (<select>).
    Label diambil dari aria-placeholder/placeholder 'Select X' (strip 'Select ')."""
    out = []
    seen_labels = set()

    # 1. Multiselect dgn search input (filterable)
    try:
        searches = page.locator(
            "input.multiselect-search[aria-placeholder^='Select '],"
            " input.multiselect-tags-search[aria-placeholder^='Select ']"
        ).all()
        for inp in searches:
            try:
                ph = (inp.get_attribute("aria-placeholder", timeout=500) or "").strip()
            except Exception:
                continue
            if not ph.lower().startswith("select "):
                continue
            label = ph[7:].strip()
            if label and label not in seen_labels:
                seen_labels.add(label)
                out.append((label, "multiselect", inp))
    except Exception as e:
        add_log(f"[GB] Gagal enum multiselect-search: {str(e)[:80]}")

    # 2. Multiselect non-filterable (wrapper[role=combobox] langsung)
    try:
        wrappers = page.locator(
            "div.multiselect-wrapper[role='combobox'][aria-placeholder^='Select ']"
        ).all()
        for w in wrappers:
            try:
                ph = (w.get_attribute("aria-placeholder", timeout=500) or "").strip()
            except Exception:
                continue
            if not ph.lower().startswith("select "):
                continue
            label = ph[7:].strip()
            if label and label not in seen_labels:
                seen_labels.add(label)
                out.append((label, "multiselect", w))
    except Exception as e:
        add_log(f"[GB] Gagal enum multiselect-wrapper: {str(e)[:80]}")

    # 3. Native <select> dgn placeholder 'Select X'
    try:
        selects = page.locator("select").all()
        for s in selects:
            try:
                ph = (s.get_attribute("placeholder", timeout=500) or "").strip()
            except Exception:
                ph = ""
            label = None
            if ph.lower().startswith("select "):
                label = ph[7:].strip()
            else:
                # Fallback: derive dari preceding <label> sibling
                try:
                    lbl = s.locator(
                        "xpath=ancestor::div[contains(@class,'w-full') or contains(@class,'flex')][1]//label"
                    ).first
                    if lbl.count() > 0:
                        label = (lbl.inner_text(timeout=500) or "").strip().strip("*").strip()
                except Exception:
                    pass
            if label and label not in seen_labels:
                seen_labels.add(label)
                out.append((label, "native_select", s))
    except Exception as e:
        add_log(f"[GB] Gagal enum native select: {str(e)[:80]}")

    return out


def _collect_multiselect_options(page):
    """Collect opsi visible dari multiselect-dropdown."""
    texts = []
    for sel in [
        "ul.multiselect-options:visible li.multiselect-option span",
        "ul.multiselect-options:visible li.multiselect-option",
        "[role='listbox']:visible [role='option']",
    ]:
        try:
            for o in page.locator(sel).all():
                try:
                    t = (o.inner_text(timeout=600) or "").strip()
                    if t and t not in texts:
                        texts.append(t)
                except Exception:
                    pass
        except Exception:
            pass
        if texts:
            break
    return texts


def _close_multiselect(page):
    """Tutup multiselect dropdown via blur + click outside."""
    for strat in ["escape", "outside_click", "js_dispatch"]:
        try:
            if strat == "escape":
                page.keyboard.press("Escape")
            elif strat == "outside_click":
                page.mouse.click(10, 100)
            elif strat == "js_dispatch":
                page.evaluate("""
                    () => {
                        const a = document.activeElement;
                        if (a && a.blur) a.blur();
                        const opts = {bubbles: true, cancelable: true, clientX: 5, clientY: 5, view: window};
                        ['pointerdown','mousedown','mouseup','click'].forEach(t => {
                            try { document.documentElement.dispatchEvent(new MouseEvent(t, opts)); } catch(e) {}
                        });
                    }
                """)
        except Exception:
            continue
        page.wait_for_timeout(200)
        try:
            if page.locator("ul.multiselect-options:visible").count() == 0:
                return
        except Exception:
            return


def _click_multiselect_option(page, value):
    """Klik opsi multiselect yg match value."""
    v_lit = _xpath_literal(value)
    for sel in [
        f"xpath=//ul[contains(@class,'multiselect-options')]//li[contains(@class,'multiselect-option')][.//span[normalize-space()={v_lit}]]",
        f"xpath=//ul[contains(@class,'multiselect-options')]//li[normalize-space()={v_lit}]",
        f"xpath=//li[@aria-label={v_lit} and contains(@class,'multiselect-option')]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=2500)
            loc.click()
            smart_wait(page, 300, 500)
            return True
        except Exception:
            continue
    return False


def _ai_pick_numbers(title, labels):
    """AI call inline: infer number value per label dari product title. Return
    dict {label: int}. Kalau AI ndak bisa jawab atau invalid, caller fallback '1'."""
    result = {}
    if not labels:
        return result
    model = _get_gemini_model()
    if model is None:
        add_log("[GB] Gemini model ndak tersedia, skip AI number pick")
        return result

    labels_str = json.dumps(labels, ensure_ascii=False)
    prompt = f"""You are a form-filling assistant for a game account marketplace.

Product title: {title}

Required numeric fields: {labels_str}

Task: For each field, infer the most appropriate POSITIVE INTEGER value from the
product title. Examples: 'King Tower Level' -> infer from 'KTL' or 'lvl' in title,
'Card Count' -> infer from 'Cards' number, etc.

Rules:
- Output MUST be a JSON object mapping each field name to an integer.
- If the title has NO relevant info for a field, omit that field from the output
  (do NOT guess random numbers).
- Return ONLY valid JSON. No markdown, no explanation.
- Example: {{"King Tower Level": 11, "Trophies": 3464}}
"""
    try:
        response = call_with_timeout(
            fn=lambda: model.generate_content(prompt),
            timeout=60,
            name="gemini_gb_numbers"
        )
    except Exception as e:
        add_log(f"[GB] AI number call error: {str(e)[:80]}")
        return result
    try:
        text = (response.text or "").strip()
        text = text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        if not isinstance(data, dict):
            return result
        for k, v in data.items():
            if k not in labels:
                continue
            try:
                iv = int(float(v))
                if iv >= 0:
                    result[k] = iv
            except Exception:
                continue
    except Exception as e:
        add_log(f"[GB] AI number parse error: {str(e)[:80]}")
    if result:
        add_log(f"[GB] AI number pick: {result}")
    return result


def _fill_empty_number_inputs(page, title=""):
    """Fill number input di step Game Data. Skip yg 'Optional'. Untuk required
    kosong: tanya AI dulu berdasar title, fallback '1' kalau AI ndak jawab."""
    # Pass 1: enumerate inputs + kumpulkan required kosong
    required_empty = []  # list of (label, input_locator)
    skipped_optional = 0
    try:
        inputs = page.locator("input[type='number']").all()
        for inp in inputs:
            try:
                if not inp.is_visible():
                    continue
                val = (inp.input_value(timeout=500) or "").strip()
                if val:
                    continue
                # Cek ancestor w-full wrapper punya span 'Optional' -> skip
                is_optional = False
                try:
                    opt = inp.locator(
                        "xpath=ancestor::div[contains(@class,'w-full')][1]"
                        "//span[normalize-space()='Optional']"
                    ).first
                    if opt.count() > 0:
                        is_optional = True
                except Exception:
                    pass
                if is_optional:
                    skipped_optional += 1
                    continue
                # Ambil label dari sibling
                label = None
                try:
                    lbl = inp.locator(
                        "xpath=ancestor::div[contains(@class,'w-full')][1]//label"
                    ).first
                    if lbl.count() > 0:
                        label = (lbl.inner_text(timeout=500) or "").strip()
                except Exception:
                    pass
                required_empty.append((label or "?", inp))
            except Exception:
                continue
    except Exception:
        pass

    # Pass 2: AI inference untuk label-label required
    ai_values = {}
    labels_to_ask = [lbl for lbl, _ in required_empty if lbl and lbl != "?"]
    if labels_to_ask and title:
        ai_values = _ai_pick_numbers(title, labels_to_ask)

    # Pass 3: fill (AI value or '1' fallback)
    count_ai = 0
    count_default = 0
    for label, inp in required_empty:
        try:
            val = ai_values.get(label)
            value_str = str(val) if isinstance(val, int) and val >= 0 else "1"
            inp.fill(value_str)
            if label in ai_values:
                count_ai += 1
            else:
                count_default += 1
            page.wait_for_timeout(80)
        except Exception:
            continue
    if required_empty or skipped_optional:
        add_log(f"[GB] Fill number: {count_ai} AI, {count_default} default '1', skip {skipped_optional} Optional")
    smart_wait(page, 300, 500)


# ===================== SCRAPE FORM =====================
def _scrape_form_options_page(page):
    """Scrape multiselect dropdown di step 2 Game Data. Return {label:[opts]}."""
    options_map = {}
    smart_wait(page, 1500, 2500)

    triggers = _multiselect_triggers(page)
    add_log(f"[GB] Detected {len(triggers)} dropdown: "
            f"{[(t[0], t[1]) for t in triggers]}")

    for label, kind, loc in triggers:
        try:
            if kind == "native_select":
                # Native <select> - ambil <option> texts langsung tanpa click
                opts = []
                try:
                    options = loc.locator("option").all()
                    for o in options:
                        try:
                            t = (o.inner_text(timeout=500) or "").strip()
                            if t and t.lower() != f"select {label.lower()}" and t not in opts:
                                opts.append(t)
                        except Exception:
                            continue
                except Exception:
                    pass
                if opts:
                    options_map[label] = opts
                    add_log(f"[GB]    - {label} (native): {len(opts)} opsi -> {opts[:5]}")
                else:
                    add_log(f"[GB]    {label} (native): opsi kosong")
                continue

            # Multiselect: click wrapper to open
            wrapper = loc.locator("xpath=ancestor::div[contains(@class,'multiselect')][1]").first
            target = wrapper if wrapper.count() > 0 else loc
            target.click()
            smart_wait(page, 400, 700)

            opts = _collect_multiselect_options(page)
            if opts:
                options_map[label] = opts
                add_log(f"[GB]    - {label}: {len(opts)} opsi -> {opts[:5]}{'...' if len(opts)>5 else ''}")
            else:
                add_log(f"[GB]    {label}: opsi kosong")

            _close_multiselect(page)
        except Exception as e:
            add_log(f"[GB] Gagal scrape '{label}': {str(e)[:80]}")
            _close_multiselect(page)
            continue

    return options_map


# ===================== ENTRY POINTS =====================
def scrape_form_options(game_name):
    """Buka popup Add New Account, isi minimum step 1, Continue ke step 2,
    scrape multiselect options. Return dict / {} / None (fail).
    CATATAN: butuh dummy data di step 1 (title+price) supaya Continue lolos
    validasi. Upload di-skip (kalau required Continue bakal gagal, dan scrape
    fallback ke {})."""
    add_log("[GB] Scrape form options dari GameBoost (pertama kali)...")
    with sync_playwright() as p:
        page = None
        try:
            browser = p.chromium.connect_over_cdp(
                f"http://localhost:{_get_chrome_debug_port()}", timeout=10000
            )
            context = get_or_create_context(browser)
            context.set_default_timeout(30000)
            context.set_default_navigation_timeout(30000)
            page = context.new_page()

            page.goto(GB_START_URL, wait_until="domcontentloaded", timeout=30000)
            _set_worker_tab_title(page)
            smart_wait(page, 3000, 5000)

            _click_add_new_account(page)
            _select_game_from_modal(page, game_name)
            # Dummy data minimum buat lolos validasi step 1
            try:
                _fill_title(page, "scrape-test")
            except Exception:
                pass
            try:
                _select_currency_usd(page)
            except Exception:
                pass
            try:
                _fill_price(page, "1")
            except Exception:
                pass
            try:
                _fill_description(page, "scrape")
            except Exception:
                pass

            try:
                _click_continue(page, "(to Game Data)")
            except Exception as e:
                add_log(f"[GB] Continue ke Game Data gagal: {str(e)[:100]}")
                # Kemungkinan besar upload required - return {} (kosong, tanpa cache)
                return {}

            return _scrape_form_options_page(page)

        except Exception as e:
            add_log(f"[GB] Gagal scrape form options: {str(e)[:100]}")
            return None
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


def create_listing(game_name, title, deskripsi, harga, field_mapping, image_paths,
                   raw_image_url=None, image_future=None):
    """Full GB create flow. Return (ok, err, uploaded_count).

    `image_future` (optional): async download future. Di-resolve tepat sebelum
    step upload."""
    uploaded = 0
    with sync_playwright() as p:
        page = None
        try:
            browser = p.chromium.connect_over_cdp(
                f"http://localhost:{_get_chrome_debug_port()}", timeout=10000
            )
            context = get_or_create_context(browser)
            context.set_default_timeout(30000)
            context.set_default_navigation_timeout(30000)
            page = context.new_page()

            page.goto(GB_START_URL, wait_until="domcontentloaded", timeout=30000)
            _set_worker_tab_title(page)
            smart_wait(page, 3000, 5000)

            _click_add_new_account(page)
            _select_game_from_modal(page, game_name)

            # Step 1: Listing Info
            _fill_title(page, title)
            _select_currency_usd(page)
            _fill_price(page, harga)

            # Description: line 1 = raw URL no scheme, line 2+ = deskripsi
            if raw_image_url:
                raw_line = re.sub(r"^https?://", "", raw_image_url.strip())
                body_text = f"Full Screenshot Detail: {raw_line}\n{deskripsi or ''}"
            else:
                body_text = deskripsi or ""
            _fill_description(page, body_text)

            # Resolve image future (async download pattern). Block sampai
            # download selesai. Fallback ke image_paths kwarg kalau None.
            if image_future is not None:
                try:
                    resolved_paths, _, _ = resolve_image_future(image_future)
                    image_paths = resolved_paths
                except RuntimeError as e:
                    return False, str(e), uploaded
            if not image_paths:
                return False, "Gambar tidak bisa di download", uploaded

            # Upload bulk max 20
            to_upload = (image_paths or [])[:GB_MAX_IMAGES]
            if to_upload:
                try:
                    uploaded = _upload_images_bulk(page, to_upload)
                except Exception as e:
                    add_log(f"[GB] Upload bulk exception: {str(e)[:80]}")

            _click_continue(page, "(to Game Data)")

            # Step 2: Game Data - isi dropdown dari AI (multiselect + native)
            try:
                triggers = _multiselect_triggers(page)
                for label, kind, loc in triggers:
                    preferred = (field_mapping or {}).get(label)
                    if not preferred:
                        continue
                    try:
                        if kind == "native_select":
                            # Native <select>: pakai select_option
                            try:
                                loc.select_option(label=preferred)
                                add_log(f"[GB] Isi {label}: {preferred} (AI, native)")
                            except Exception:
                                try:
                                    loc.select_option(preferred)
                                    add_log(f"[GB] Isi {label}: {preferred} (AI, native)")
                                except Exception as e:
                                    add_log(f"[GB] Gagal select '{preferred}' di {label}: {str(e)[:60]}")
                            smart_wait(page, 250, 450)
                            continue

                        # Multiselect
                        wrapper = loc.locator(
                            "xpath=ancestor::div[contains(@class,'multiselect')][1]"
                        ).first
                        target = wrapper if wrapper.count() > 0 else loc
                        target.click()
                        smart_wait(page, 400, 700)
                        ok_pick = _click_multiselect_option(page, preferred)
                        if ok_pick:
                            add_log(f"[GB] Isi {label}: {preferred} (AI)")
                        _close_multiselect(page)
                    except Exception as e:
                        add_log(f"[GB] Gagal isi '{label}': {str(e)[:80]}")
                        _close_multiselect(page)
            except Exception as e:
                add_log(f"[GB] Fill dropdown error: {str(e)[:80]}")

            _fill_empty_number_inputs(page, title=title)

            _click_continue(page, "(to Credentials)")

            # Step 3: Credentials - Manual Delivery + delivery time 60
            add_log("[GB] Pilih Manual Delivery")
            try:
                for sel in [
                    "xpath=//label[contains(normalize-space(),'Manual Delivery')]",
                    "label[for='toggle-radio-true']",
                    "xpath=//label[.//i[contains(@class,'fa-truck')] and contains(normalize-space(),'Manual')]",
                ]:
                    try:
                        loc = page.locator(sel).first
                        loc.wait_for(state="visible", timeout=5000)
                        loc.click()
                        break
                    except Exception:
                        continue
                smart_wait(page, 500, 900)
            except Exception as e:
                return False, f"Manual Delivery: {str(e)[:100]}", uploaded

            add_log(f"[GB] Isi Delivery Time: {GB_DELIVERY_MINUTES} menit")
            try:
                # Number input dgn placeholder '10' min='1' - setelah Manual
                # Delivery aktif, input ini muncul.
                for sel in [
                    "input[type='number'][placeholder='10'][min='1']",
                    "xpath=//input[@type='number' and @placeholder='10']",
                ]:
                    try:
                        loc = page.locator(sel).first
                        loc.wait_for(state="visible", timeout=5000)
                        loc.fill(GB_DELIVERY_MINUTES)
                        break
                    except Exception:
                        continue
                smart_wait(page, 300, 500)
            except Exception as e:
                return False, f"Delivery Time: {str(e)[:100]}", uploaded

            add_log("[GB] Klik Add Account")
            try:
                for sel in [
                    "xpath=//button[normalize-space()='Add Account']",
                    "button:has-text('Add Account')",
                ]:
                    try:
                        loc = page.locator(sel).first
                        loc.wait_for(state="visible", timeout=5000)
                        start_url = page.url
                        loc.click()
                        break
                    except Exception:
                        continue
            except Exception as e:
                return False, f"Add Account click: {str(e)[:100]}", uploaded

            # Sukses detection: toast success OR redirect /dashboard/accounts/{id}
            success = False
            for _ in range(120):  # 60s max
                page.wait_for_timeout(500)
                # Cek toast success
                try:
                    toast = page.locator(
                        "div.Vue-Toastification__toast--success"
                    ).first
                    if toast.count() > 0 and toast.is_visible():
                        success = True
                        break
                except Exception:
                    pass
                # Cek redirect ke /dashboard/accounts/{num}
                try:
                    cur = page.url
                    if cur != start_url and re.search(r"/dashboard/accounts/\d+", cur):
                        success = True
                        break
                except Exception:
                    pass

            if success:
                add_log(f"[GB] Listing sukses (url={page.url[:60]})")
                smart_wait(page, 800, 1500)
                return True, None, uploaded

            # Fallback error
            try:
                err_msgs = []
                for sel in [
                    "div.Vue-Toastification__toast--error",
                    "[role='alert']:visible",
                    "[class*='error']:visible",
                ]:
                    try:
                        for l in page.locator(sel).all():
                            try:
                                if not l.is_visible():
                                    continue
                                t = (l.inner_text(timeout=1000) or "").strip()
                                if t and 5 <= len(t) <= 200 and t not in err_msgs:
                                    err_msgs.append(t)
                            except Exception:
                                continue
                    except Exception:
                        continue
                if err_msgs:
                    combined = " | ".join(err_msgs[:3])
                    add_log(f"[GB] Error: {combined[:200]}")
                    return False, f"Form error: {combined[:120]}", uploaded
            except Exception:
                pass
            return False, "Add Account: tidak sukses dalam 60s", uploaded

        except Exception as e:
            pesan = str(e)
            if "Timeout" in pesan:
                err_msg = "Waktu habis, elemen tidak ditemukan"
            elif "net::" in pesan:
                err_msg = "Gagal membuka halaman, cek koneksi"
            else:
                err_msg = f"Error: {pesan[:100]}"
            add_log(f"[GB] Gagal: {err_msg}")
            return False, err_msg, uploaded
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


def cache_looks_bogus(cache_dict):
    """GB: default False."""
    return False


def run(sheet, baris_nomor, worker_id, *, game_name, description, title, harga,
        field_mapping, image_paths=None, image_urls=None,
        raw_image_url=None, is_imgur=False, image_future=None):
    """Adapter entry dipanggil orchestrator. Return (ok, k_line)."""
    _worker_local.worker_id = f"{worker_id}-GB"

    ok, err, uploaded = create_listing(
        game_name, title, description or "", harga,
        field_mapping or {},
        (image_paths or [])[:GB_MAX_IMAGES] if image_paths else None,
        raw_image_url=raw_image_url,
        image_future=image_future,
    )
    ts = datetime.now().strftime("%d %b, %y | %H:%M")
    if ok:
        return True, f"✅ [GB] | {uploaded} images uploaded | {ts}"
    return False, f"❌ [GB] | {(err or 'unknown')[:80]}"
