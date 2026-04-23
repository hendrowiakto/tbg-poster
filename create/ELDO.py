"""create/ELDO.py - Eldorado.gg marketplace adapter.

Entry points dipakai orchestrator (bot_create.py):
- scrape_form_options(game_name) -> dict | {} | None
- create_listing(...) -> (ok, err, uploaded_count)
- run(sheet, baris_nomor, worker_id, **kwargs) -> (ok, k_line)
- cache_looks_bogus(cache) -> bool

Flow Eldorado (per user spec):
  Step 1: goto /sell/offer/Account -> pilih Game via dropdown searchable ->
          klik Next -> navigate ke /sell/offer/Account/{gameId}
  Step 2: scrape dynamic dropdown (format: <eld-dropdown> dgn trigger span
          'Select {Label}') -> return {label: [options]} ke AI mapping.
  Step 3: (belum-impl) fill form, upload max 5 gambar satu-persatu dgn
          polling upload selesai, centang 2 TOS, klik publish -> success
          redirect ke /dashboard/offers?category=Account.

Angular custom components: `_ngcontent-ng-cXXXXXXXXXX` attributes,
`ng-untouched/ng-pristine/ng-invalid` classes. CSS hash bisa churn antar
deploy; selector pakai role/aria + class utility yg stabil.
"""

import random
import re
from datetime import datetime
from playwright.sync_api import sync_playwright

from create._shared import (
    _worker_local,
    _log as add_log,
    _get_chrome_debug_port,
    xpath_literal as _xpath_literal,
    smart_wait as _base_smart_wait,
    get_or_create_context,
)


# ELDO anti-spam throttle (lebih konservatif dari ZEUS karena koneksi eldorado
# ndak stabil + user request "lebih lemot"). Base × 1.5 + 0-50% jitter.
_ELDO_SLOW_MULT    = 1.5
_ELDO_JITTER_MAX   = 0.50


def smart_wait(page, min_ms, max_ms):
    """ELDO-local smart_wait: base × 1.5 + random 0-50% extra jitter."""
    jitter = 1.0 + random.uniform(0, _ELDO_JITTER_MAX)
    lo = int(min_ms * _ELDO_SLOW_MULT * jitter)
    hi = int(max_ms * _ELDO_SLOW_MULT * jitter)
    if hi < lo:
        hi = lo
    _base_smart_wait(page, lo, hi)


# ===================== KONSTANTA =====================
ELDO_ORIGIN         = "https://www.eldorado.gg"
ELDO_START_URL      = "https://www.eldorado.gg/sell/offer/Account"
ELDO_SUCCESS_URL    = "https://www.eldorado.gg/dashboard/offers?category=Account"
ELDO_MAX_IMAGES     = 5
MAX_IMAGES          = ELDO_MAX_IMAGES            # alias standar adapter protocol

# Adapter protocol:
MARKET_CODE         = "ELDO"
HARGA_COL           = 8                                 # H
NO_OPTIONS_SENTINEL_ELDO = "[tidak ditemukan options ELDO]"
CACHE_SENTINEL      = NO_OPTIONS_SENTINEL_ELDO


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


# ===================== DROPDOWN HELPERS =====================
def _find_trigger_by_placeholder(page, placeholder_text):
    """Find dropdown trigger dgn span placeholder 'Select X' di dalamnya.
    Return locator atau None."""
    selectors = [
        f"xpath=//div[@role='combobox' and contains(@class,'dropdown-trigger')"
        f" and .//span[normalize-space()={_xpath_literal(placeholder_text)}]]",
        f"xpath=//eld-dropdown[.//span[normalize-space()={_xpath_literal(placeholder_text)}]]"
        f"//div[@role='combobox']",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def _collect_dropdown_options(page):
    """Ambil option texts dari listbox/overlay yg lagi terbuka."""
    texts = []
    # Eldorado pakai Angular CDK overlay - coba role=option dulu, fallback list items.
    selectors = [
        "[role='option']:visible",
        "eld-dropdown-option:visible",
        ".dropdown-option:visible",
        "li.option:visible",
    ]
    for sel in selectors:
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


def _dropdown_is_open(page):
    """True kalau masih ada opsi listbox visible."""
    for sel in ["[role='option']:visible", ".dropdown-option:visible",
                "eld-dropdown-option:visible"]:
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


def _close_dropdown(page):
    """Tutup dropdown. Multi-strategy: Escape -> click outside -> JS blur."""
    for strat in ["escape", "outside_click", "js_dispatch"]:
        try:
            if strat == "escape":
                page.keyboard.press("Escape")
            elif strat == "outside_click":
                page.mouse.click(20, 20)
            elif strat == "js_dispatch":
                page.evaluate("""
                    () => {
                        const a = document.activeElement;
                        if (a && a.blur) a.blur();
                        const opts = {bubbles: true, cancelable: true,
                                      clientX: 5, clientY: 5, view: window};
                        ['pointerdown','mousedown','mouseup','click'].forEach(t => {
                            try { document.documentElement.dispatchEvent(new MouseEvent(t, opts)); } catch(e) {}
                        });
                    }
                """)
        except Exception:
            continue
        page.wait_for_timeout(250)
        if not _dropdown_is_open(page):
            return


# ===================== STEP 1: GAME PICKER + NEXT =====================
def _select_game_and_next(page, game_name):
    """Step 1: klik dropdown Game, ketik nama, pilih opsi, klik Next.
    Sukses = URL berubah dari /sell/offer/Account menjadi /sell/offer/Account/{gameId}."""
    add_log(f"[ELDO] Pilih Game: {game_name}")

    trigger = _find_trigger_by_placeholder(page, "Select your game")
    if trigger is None:
        # Fallback: pertama dropdown-trigger di halaman.
        try:
            trigger = page.locator("div[role='combobox'].dropdown-trigger").first
            trigger.wait_for(state="visible", timeout=5000)
        except Exception:
            raise Exception("Game dropdown trigger tidak ditemukan")

    trigger.click()
    smart_wait(page, 400, 700)

    # Ketik di search-input yg muncul (atau trigger itu sendiri kalau focus terforward).
    typed = False
    for sel in [
        "input.search-input:visible",
        "input.search-input:focus",
        "input[aria-hidden='true'].search-input",
    ]:
        try:
            inp = page.locator(sel).first
            if inp.count() > 0:
                try:
                    inp.fill(game_name, timeout=2000)
                    typed = True
                    break
                except Exception:
                    pass
        except Exception:
            continue
    if not typed:
        try:
            page.keyboard.type(game_name, delay=40)
            typed = True
        except Exception:
            pass
    if not typed:
        raise Exception("Gagal ketik nama game")

    smart_wait(page, 700, 1200)

    # Klik opsi match
    g_lit = _xpath_literal(game_name)
    g_lower_lit = _xpath_literal(game_name.lower())
    upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    lower = "abcdefghijklmnopqrstuvwxyz"
    option = None
    for sel in [
        f"xpath=//*[@role='option' and normalize-space()={g_lit}]",
        f"xpath=//*[@role='option'][contains(translate(normalize-space(.),'{upper}','{lower}'),{g_lower_lit})]",
        f"xpath=//eld-dropdown-option[normalize-space()={g_lit}]",
        f"xpath=//*[contains(@class,'dropdown-option') and normalize-space()={g_lit}]",
        f"xpath=//li[normalize-space()={g_lit}]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=2500)
            option = loc
            break
        except Exception:
            continue
    if option is None:
        raise Exception(f"Opsi game '{game_name}' tidak muncul")
    option.click()
    smart_wait(page, 500, 900)

    # Klik Next (anchor). Tunggu sampai aria-disabled=false.
    next_btn = None
    for _ in range(20):  # 4s
        for sel in [
            "a[aria-label='Next'][aria-disabled='false']",
            "a.button__primary[aria-label='Next']:not([aria-disabled='true'])",
            "xpath=//a[@aria-label='Next' and (@aria-disabled='false' or not(@aria-disabled))]",
        ]:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    next_btn = loc
                    break
            except Exception:
                continue
        if next_btn is not None:
            break
        page.wait_for_timeout(200)
    if next_btn is None:
        raise Exception("Next button ndak enabled setelah pilih game")

    pre_url = page.url
    next_btn.click()
    # Tunggu URL berubah (in-place Angular nav) ke /sell/offer/Account/{id}.
    for _ in range(30):  # 6s max
        try:
            if page.url != pre_url and re.search(r"/sell/offer/Account/\d+", page.url):
                break
        except Exception:
            pass
        page.wait_for_timeout(200)
    add_log(f"[ELDO] Form page loaded: {page.url}")
    smart_wait(page, 1200, 2000)


# ===================== STEP 2: FORM OPTIONS SCRAPE =====================
def _scrape_form_options_page(page):
    """Scrape semua <eld-dropdown> dinamis di form create listing. Label
    diderive dari trigger span 'Select {Label}'. Return {label: [options]}."""
    options_map = {}

    smart_wait(page, 2000, 3000)

    # Kumpulkan semua dropdown-trigger yg placeholder-nya 'Select X'.
    pending = []  # list of label strings
    try:
        triggers = page.locator("div[role='combobox'].dropdown-trigger").all()
        for t in triggers:
            try:
                span = t.locator("span.text-input-text-placeholder").first
                txt = (span.inner_text(timeout=1500) or "").strip()
            except Exception:
                continue
            if not txt.lower().startswith("select "):
                continue
            label = txt[7:].strip()
            if not label:
                continue
            # Skip 'your game' (halaman step 1) - harusnya udah lewat, tapi safety.
            if label.lower() in ("your game", "game"):
                continue
            if label not in pending:
                pending.append(label)
    except Exception as e:
        add_log(f"[ELDO] Gagal enumerate triggers: {str(e)[:80]}")

    add_log(f"[ELDO] Detected {len(pending)} dynamic dropdown(s): {pending}")

    for label in pending:
        try:
            trigger = _find_trigger_by_placeholder(page, f"Select {label}")
            if trigger is None:
                add_log(f"[ELDO]    {label}: trigger hilang, skip")
                continue
            trigger.click()
            smart_wait(page, 400, 700)

            opts = _collect_dropdown_options(page)
            if opts:
                options_map[label] = opts
                add_log(f"[ELDO]    - {label}: {len(opts)} opsi -> {opts[:5]}{'...' if len(opts)>5 else ''}")
            else:
                add_log(f"[ELDO]    {label}: opsi tidak terdeteksi")

            _close_dropdown(page)
        except Exception as e:
            add_log(f"[ELDO] Gagal scrape '{label}': {str(e)[:80]}")
            _close_dropdown(page)
            continue

    return options_map


# ===================== ENTRY POINTS =====================
def scrape_form_options(game_name):
    """Buka /sell/offer/Account, pilih game, klik Next, scrape form options.
    Return dict non-empty / {} / None (fail)."""
    add_log("[ELDO] Scrape form options dari Eldorado (pertama kali)...")
    with sync_playwright() as p:
        page = None
        try:
            browser = p.chromium.connect_over_cdp(
                f"http://localhost:{_get_chrome_debug_port()}", timeout=10000
            )
            context = get_or_create_context(browser)
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()

            page.goto(ELDO_START_URL, wait_until="networkidle", timeout=30000)
            _set_worker_tab_title(page)
            smart_wait(page, 2500, 4000)

            _select_game_and_next(page, game_name)
            return _scrape_form_options_page(page)

        except Exception as e:
            add_log(f"[ELDO] Gagal scrape form options: {str(e)[:100]}")
            return None
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


# ===================== STEP 3 HELPERS =====================
def _open_trigger_in_container(page, container_xpath, placeholder=None):
    """Buka dropdown di dalam container tertentu (scope by container_xpath).
    Kalau placeholder diisi, match trigger yg placeholder-nya sesuai."""
    if placeholder:
        xp = (f"{container_xpath}//div[@role='combobox' and contains(@class,'dropdown-trigger')"
              f" and .//span[normalize-space()={_xpath_literal(placeholder)}]]")
    else:
        xp = f"{container_xpath}//div[@role='combobox' and contains(@class,'dropdown-trigger')]"
    trigger = page.locator(f"xpath={xp}").first
    trigger.wait_for(state="visible", timeout=5000)
    trigger.click()
    smart_wait(page, 400, 700)
    return trigger


def _click_option_by_text(page, option_text):
    """Klik option visible yg text-nya match."""
    lit = _xpath_literal(option_text)
    for sel in [
        f"xpath=//*[@role='option' and normalize-space()={lit}]",
        f"xpath=//eld-dropdown-option[normalize-space()={lit}]",
        f"xpath=//*[contains(@class,'dropdown-option') and normalize-space()={lit}]",
        f"xpath=//li[normalize-space()={lit}]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=2500)
            loc.click()
            smart_wait(page, 400, 700)
            return True
        except Exception:
            continue
    return False


def _count_upload_previews(page):
    """Hitung preview-image box di <eld-offer-image-upload-preview>. Scoped
    ke upload area supaya ndak kena thumbnail lain di header/chat widget.
    Return first-matching count (bukan max) supaya predictable."""
    for sel in [
        "eld-offer-image-upload-preview div.preview-image-box",
        "eld-offer-image-upload-preview [class*='preview-image-box']",
        "eld-offer-image-upload-preview img[alt='Preview image']",
        "eld-offer-image-upload-preview img[src*='fileservice']",
        "eld-offer-image-upload-preview img[src^='http']",
        "eld-offer-image-upload-preview img",
    ]:
        try:
            c = page.locator(sel).count()
            if c > 0:
                return c
        except Exception:
            continue
    return 0


def _upload_images_bulk(page, paths, timeout_ms=120000):
    """Upload multiple gambar sekaligus lewat 1 set_input_files(paths).
    Hidden <input type='file' multiple> di Eldorado support multi-file.
    Return actual uploaded count (count preview final - baseline)."""
    if not paths:
        return 0
    baseline = _count_upload_previews(page)
    target = baseline + len(paths)
    add_log(f"[ELDO] Upload {len(paths)} gambar bulk (baseline={baseline}, target={target})...")

    # Find hidden file input. Pakai set_input_files langsung - ndak usah click
    # button buat trigger file chooser karena chooser biasanya 1 file.
    try:
        file_input = page.locator("input[type='file']").first
        file_input.set_input_files(paths)
    except Exception as e1:
        # Fallback: file chooser pattern (kalau input[type=file] ndak accessible)
        try:
            btn = None
            for sel in [
                "button[aria-label='Upload images']",
                "button[data-testid*='offer-edit-image-upload-button']",
                "xpath=//button[.//span[normalize-space()='Upload images']]",
            ]:
                try:
                    loc = page.locator(sel).first
                    loc.wait_for(state="visible", timeout=3000)
                    btn = loc
                    break
                except Exception:
                    continue
            if btn is None:
                raise Exception("Upload images button ndak ketemu")
            with page.expect_file_chooser(timeout=4000) as fc_info:
                btn.click()
            fc = fc_info.value
            fc.set_files(paths)
        except Exception as e2:
            raise Exception(f"set_files gagal: input={str(e1)[:40]} chooser={str(e2)[:40]}")

    # Poll count tiap 500ms. Timeout lebih generous buat bulk (banyak file).
    step_ms = 500
    elapsed = 0
    last_count = baseline
    while elapsed < timeout_ms:
        page.wait_for_timeout(step_ms)
        elapsed += step_ms
        cur = _count_upload_previews(page)
        if cur != last_count:
            last_count = cur
        if cur >= target:
            add_log(f"[ELDO] Upload bulk OK ({baseline}->{cur}) {elapsed/1000:.1f}s")
            smart_wait(page, 600, 1000)
            return cur - baseline

    # Timeout - return apa yg udah keupload
    actual = max(0, last_count - baseline)
    add_log(f"[ELDO] Upload bulk timeout {timeout_ms//1000}s"
            f" (count akhir={last_count}, target={target}, actual={actual})")
    return actual


def create_listing(game_name, title, deskripsi, harga, field_mapping, image_paths,
                   raw_image_url=None):
    """Flow partial (step 1-6, belum publish). Return (ok, err, uploaded_count).
    Sengaja return False di akhir biar ndak kena mark centang TRUE pas testing."""
    uploaded = 0
    with sync_playwright() as p:
        page = None
        try:
            browser = p.chromium.connect_over_cdp(
                f"http://localhost:{_get_chrome_debug_port()}", timeout=10000
            )
            context = get_or_create_context(browser)
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()

            page.goto(ELDO_START_URL, wait_until="networkidle", timeout=30000)
            _set_worker_tab_title(page)
            smart_wait(page, 2500, 4000)

            # Step 1: select game + click Next
            _select_game_and_next(page, game_name)

            # Step 2a: fill dynamic dropdowns via field_mapping (AI)
            for label, value in (field_mapping or {}).items():
                if not value:
                    continue
                try:
                    add_log(f"[ELDO] Isi {label}: {value}")
                    _open_trigger_in_container(
                        page,
                        container_xpath=f"//*[.//span[normalize-space()={_xpath_literal('Select ' + label)}]]",
                        placeholder=f"Select {label}",
                    )
                    ok = _click_option_by_text(page, value)
                    if not ok:
                        add_log(f"[ELDO] Opsi '{value}' untuk '{label}' tidak ketemu")
                    _close_dropdown(page)
                except Exception as e:
                    add_log(f"[ELDO] Gagal isi '{label}'={value}: {str(e)[:80]}")

            # Step 3: Original email -> No
            add_log("[ELDO] Pilih Original email: No")
            try:
                _open_trigger_in_container(
                    page, container_xpath="//eld-original-email-dropdown"
                )
                if not _click_option_by_text(page, "No"):
                    raise Exception("opsi 'No' tidak ketemu")
                _close_dropdown(page)
            except Exception as e:
                return False, f"Original email: {str(e)[:100]}", uploaded

            # Step 4: fill Title (kolom J)
            add_log(f"[ELDO] Isi Title: {title[:60]}")
            try:
                title_ta = None
                for sel in [
                    "eld-textarea[aria-label='Offer title'] textarea",
                    "xpath=//eld-textarea[@aria-label='Offer title']//textarea",
                    "textarea[placeholder='Type here...'][maxlength='160']",
                ]:
                    try:
                        loc = page.locator(sel).first
                        loc.wait_for(state="visible", timeout=3000)
                        title_ta = loc
                        break
                    except Exception:
                        continue
                if title_ta is None:
                    raise Exception("title textarea tidak ditemukan")
                title_ta.fill(title)
                smart_wait(page, 400, 700)
            except Exception as e:
                return False, f"Title: {str(e)[:100]}", uploaded

            # Step 5: Upload gambar bulk (max 5 sekaligus, 1 set_input_files call).
            to_upload = (image_paths or [])[:ELDO_MAX_IMAGES]
            if to_upload:
                try:
                    uploaded = _upload_images_bulk(page, to_upload)
                    if uploaded < len(to_upload):
                        add_log(f"[ELDO] Upload bulk partial: {uploaded}/{len(to_upload)}")
                except Exception as e:
                    add_log(f"[ELDO] Upload bulk exception: {str(e)[:80]}")

            # Step 6: Description (baris 1 = raw image URL tanpa https, baris 2+ = deskripsi)
            add_log("[ELDO] Isi Description")
            try:
                desc_ta = None
                for sel in [
                    "textarea[data-testid*='offer-edit-page-description-textarea']",
                    "textarea[maxlength='2000']",
                    "xpath=//eld-textarea[@datatestid][contains(@datatestid,'description')]//textarea",
                ]:
                    try:
                        loc = page.locator(sel).first
                        loc.wait_for(state="visible", timeout=3000)
                        desc_ta = loc
                        break
                    except Exception:
                        continue
                if desc_ta is None:
                    raise Exception("description textarea tidak ditemukan")
                # Build body: baris 1 URL stripped scheme, baris 2+ deskripsi
                if raw_image_url:
                    raw_line = re.sub(r"^https?://", "", raw_image_url.strip())
                    body = f"{raw_line}\n{deskripsi or ''}"
                else:
                    body = deskripsi or ""
                desc_ta.fill(body)
                smart_wait(page, 400, 700)
            except Exception as e:
                return False, f"Description: {str(e)[:100]}", uploaded

            # Step 7: Delivery = Manual (radio)
            add_log("[ELDO] Pilih Delivery: Manual")
            try:
                radio = None
                for sel in [
                    "eld-radio-option input[type='radio'][value='Manual']",
                    "input[type='radio'][value='Manual']",
                    "xpath=//label[normalize-space()='Manual']",
                ]:
                    try:
                        loc = page.locator(sel).first
                        loc.wait_for(state="visible", timeout=3000)
                        radio = loc
                        break
                    except Exception:
                        continue
                if radio is None:
                    raise Exception("radio Manual tidak ditemukan")
                try:
                    radio.check(force=True)
                except Exception:
                    radio.click(force=True)
                smart_wait(page, 400, 700)
            except Exception as e:
                return False, f"Delivery: {str(e)[:100]}", uploaded

            # Step 8: Guaranteed Delivery Time = 1 day
            add_log("[ELDO] Pilih Delivery Time: 1 day")
            try:
                _open_trigger_in_container(
                    page,
                    container_xpath="//div[contains(@class,'guaranteed-time')]",
                    placeholder="Choose",
                )
                if not _click_option_by_text(page, "1 day"):
                    raise Exception("opsi '1 day' tidak ketemu")
                _close_dropdown(page)
            except Exception as e:
                return False, f"Delivery Time: {str(e)[:100]}", uploaded

            # Step 9: Price (kolom H). Strip simbol, kasih angka aja ke numeric input.
            price_raw = str(harga)
            price_clean = re.sub(r"[^0-9.,]", "", price_raw).replace(",", ".")
            if price_clean.count(".") > 1:
                parts = price_clean.split(".")
                price_clean = "".join(parts[:-1]) + "." + parts[-1]
            add_log(f"[ELDO] Isi Price: ${price_clean}")
            try:
                price_input = None
                for sel in [
                    "eld-numeric-input input.input[placeholder='Price']",
                    "eld-numeric-input input[aria-label='Numeric input field']",
                    "xpath=//eld-numeric-input//input[@placeholder='Price']",
                    "input[placeholder='Price'][inputmode='decimal']",
                ]:
                    try:
                        loc = page.locator(sel).first
                        loc.wait_for(state="visible", timeout=3000)
                        price_input = loc
                        break
                    except Exception:
                        continue
                if price_input is None:
                    raise Exception("price input tidak ditemukan")
                price_input.fill(price_clean)
                smart_wait(page, 400, 700)
            except Exception as e:
                return False, f"Price: {str(e)[:100]}", uploaded

            # Step 10: Centang 2 TOS checkbox (Terms of Service + Seller Rules).
            # Input[type=checkbox] biasanya hidden, .check(force=True) bypass visibility.
            for tos_label in ["Terms of Service", "Seller Rules"]:
                add_log(f"[ELDO] Centang: {tos_label}")
                try:
                    cb = None
                    for sel in [
                        f"input[type='checkbox'][aria-label='{tos_label}']",
                        f"xpath=//input[@type='checkbox' and @aria-label='{tos_label}']",
                        f"xpath=//eld-checkbox[@arialabel='{tos_label}']//input[@type='checkbox']",
                    ]:
                        try:
                            loc = page.locator(sel).first
                            if loc.count() > 0:
                                cb = loc
                                break
                        except Exception:
                            continue
                    if cb is None:
                        raise Exception(f"checkbox '{tos_label}' tidak ditemukan")
                    try:
                        cb.check(force=True)
                    except Exception:
                        # Fallback: click label yg associated dgn checkbox
                        try:
                            lbl = page.locator(
                                f"xpath=//label[contains(normalize-space(.),'{tos_label}')"
                                f" or contains(normalize-space(.),'agree to the {tos_label}')]"
                            ).first
                            lbl.click(force=True)
                        except Exception:
                            cb.click(force=True)
                    smart_wait(page, 300, 600)
                except Exception as e:
                    return False, f"Checkbox {tos_label}: {str(e)[:100]}", uploaded

            # Step 11: Place offer button.
            add_log("[ELDO] Klik Place offer")
            try:
                place_btn = None
                for sel in [
                    "button[aria-label='Place offer']",
                    "xpath=//button[@aria-label='Place offer']",
                    "xpath=//button[.//span[normalize-space()='Place offer']]",
                ]:
                    try:
                        loc = page.locator(sel).first
                        loc.wait_for(state="visible", timeout=5000)
                        place_btn = loc
                        break
                    except Exception:
                        continue
                if place_btn is None:
                    raise Exception("Place offer button tidak ditemukan")
                start_url = page.url
                place_btn.click()
            except Exception as e:
                return False, f"Place offer click: {str(e)[:100]}", uploaded

            # Step 12: Sukses detection - poll max 2 menit, race 2 signal:
            # (a) toast-error muncul -> gagal, pesan toast jadi K line
            # (b) URL redirect ke /dashboard/offers -> sukses
            toast_msg = None
            redirected = False
            for _ in range(240):
                page.wait_for_timeout(500)
                # Cek toast error dulu (lebih priority - kalau muncul artinya publish reject)
                try:
                    toast_loc = page.locator(
                        "div.toast.toast-error .toast-message, "
                        "div.toast-error .toast-message"
                    ).first
                    if toast_loc.count() > 0 and toast_loc.is_visible():
                        raw = (toast_loc.inner_text(timeout=1000) or "").strip()
                        if raw:
                            toast_msg = raw
                            break
                except Exception:
                    pass
                # Cek redirect
                try:
                    cur = page.url
                    if cur != start_url and "/dashboard/offers" in cur:
                        redirected = True
                        break
                except Exception:
                    pass

            if toast_msg:
                add_log(f"[ELDO] Toast error: {toast_msg[:150]}")
                return False, toast_msg[:120], uploaded
            if redirected:
                add_log(f"[ELDO] Redirect ke: {page.url} -> sukses")
                smart_wait(page, 800, 1500)
                return True, None, uploaded

            return False, "Place offer: tidak redirect dalam 2 menit", uploaded

        except Exception as e:
            pesan = str(e)
            if "Timeout" in pesan:
                err_msg = "Waktu habis, elemen tidak ditemukan"
            elif "net::" in pesan:
                err_msg = "Gagal membuka halaman, cek koneksi"
            else:
                err_msg = f"Error: {pesan[:100]}"
            add_log(f"[ELDO] Gagal: {err_msg}")
            return False, err_msg, uploaded
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


def cache_looks_bogus(cache_dict):
    """ELDO: default False. Bisa ditambah heuristik nanti kalau AI bermasalah."""
    return False


def run(sheet, baris_nomor, worker_id, *, game_name, description, title, harga,
        field_mapping, image_paths=None, image_urls=None,
        raw_image_url=None, is_imgur=False):
    """Adapter entry dipanggil orchestrator. Return (ok, k_line)."""
    _worker_local.worker_id = f"{worker_id}-ELDO"

    ok, err, uploaded = create_listing(
        game_name, title, description or "", harga,
        field_mapping or {}, (image_paths or [])[:ELDO_MAX_IMAGES],
        raw_image_url=raw_image_url,
    )
    ts = datetime.now().strftime("%d %b, %y | %H:%M")
    if ok:
        return True, f"✅ ELDO | {uploaded} images uploaded | {ts}"
    return False, f"❌ ELDO | {(err or 'unknown')[:80]}"
