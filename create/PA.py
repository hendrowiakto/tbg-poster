"""create/PA.py - PlayerAuctions marketplace adapter.

Entry points dipakai orchestrator (bot_create.py):
- scrape_form_options(game_name) -> dict | {} | None
- create_listing(...) -> (ok, err, uploaded_count)
- run(sheet, baris_nomor, worker_id, **kwargs) -> (ok, k_line)
- cache_looks_bogus(cache) -> bool

Flow PlayerAuctions (per user spec):
  Step 1: goto /offers/creation -> search game di typeahead -> pilih
  Step 2: klik kategori 'Accounts' dari product-box cards
  Step 3: (scrape) collect opsi nz-select top-level (parent). Child cascading
          dropdown (contoh server region -> server name) di-scrape LIVE saat
          create_listing (Opsi 1: cache shallow).
  Step 4: (belum-impl) fill form, upload 1 gambar, centang 1 TOS, klik publish
          -> success redirect ke /offers/creation/success.

PlayerAuctions pakai NG-ZORRO (nz-select, ant-design). Opsi dropdown render
di portal `.ant-select-dropdown` pakai `.ant-select-item-option`.
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


# PA anti-spam throttle: sangat ketat, sering timeout. Base × 1.75 + 0-50% jitter.
_PA_SLOW_MULT      = 1.75
_PA_JITTER_MAX     = 0.50


def smart_wait(page, min_ms, max_ms):
    """PA-local smart_wait: base × 1.75 + random 0-50% extra jitter."""
    jitter = 1.0 + random.uniform(0, _PA_JITTER_MAX)
    lo = int(min_ms * _PA_SLOW_MULT * jitter)
    hi = int(max_ms * _PA_SLOW_MULT * jitter)
    if hi < lo:
        hi = lo
    _base_smart_wait(page, lo, hi)


# ===================== KONSTANTA =====================
PA_ORIGIN          = "https://member.playerauctions.com"
PA_START_URL       = "https://member.playerauctions.com/offers/creation"
PA_SUCCESS_URL     = "https://member.playerauctions.com/offers/creation/success"
PA_MAX_IMAGES      = 1
MAX_IMAGES         = PA_MAX_IMAGES               # alias standar adapter protocol

# Adapter protocol:
MARKET_CODE        = "PA"
HARGA_COL          = 8                                 # H
NO_OPTIONS_SENTINEL_PA = "[tidak ditemukan options PA]"
CACHE_SENTINEL     = NO_OPTIONS_SENTINEL_PA


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


# ===================== DROPDOWN HELPERS (NG-ZORRO) =====================
def _collect_antd_options(page):
    """Collect option texts dari .ant-select-dropdown yg visible (portal)."""
    texts = []
    for sel in [
        ".ant-select-dropdown:not(.ant-select-dropdown-hidden) "
        ".ant-select-item-option .ant-select-item-option-content",
        ".ant-select-dropdown:not(.ant-select-dropdown-hidden) [role='option']",
        ".ant-select-item-option-content:visible",
        "[role='option']:visible",
    ]:
        try:
            for o in page.locator(sel).all():
                try:
                    t = (o.inner_text(timeout=800) or "").strip()
                    if t and t not in texts:
                        texts.append(t)
                except Exception:
                    pass
        except Exception:
            pass
        if texts:
            break
    return texts


def _close_antd_dropdown(page):
    """Tutup nz-select overlay. Escape + click outside fallback."""
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
        # Check if dropdown still visible
        try:
            n = page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)").count()
            if n == 0:
                return
        except Exception:
            return


def _label_from_formcontrolname(fcn):
    """Convert formcontrolname ke human label. 'serverId' -> 'Server'.
    'serverRegion' -> 'Server Region'. Strip 'Id' suffix."""
    if not fcn:
        return None
    s = re.sub(r"Id$", "", fcn)
    # camelCase -> space separated
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    return s.strip().capitalize() if s else None


# ===================== STEP 1: GAME PICKER =====================
def _select_game(page, game_name):
    """Klik input typeahead, ketik nama game, pilih opsi."""
    add_log(f"[PA] Pilih Game: {game_name}")

    search = None
    for sel in [
        "input#typeahead-focus",
        "input[placeholder='Select a game...']",
        "input[role='combobox'][placeholder*='game']",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=5000)
            search = loc
            break
        except Exception:
            continue
    if search is None:
        raise Exception("Game typeahead input tidak ditemukan")

    try:
        search.click()
    except Exception:
        pass
    # Fill + type sebagai alternatif (typeahead kadang perlu event input)
    try:
        search.fill("")
    except Exception:
        pass
    page.keyboard.type(game_name, delay=50)
    smart_wait(page, 800, 1400)

    # Klik option hasil typeahead. Ng-bootstrap biasanya pakai button.dropdown-item
    # atau div role=option. Coba multi selector.
    g_lit = _xpath_literal(game_name)
    g_lower_lit = _xpath_literal(game_name.lower())
    upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    lower = "abcdefghijklmnopqrstuvwxyz"
    option = None
    for sel in [
        f"xpath=//ngb-typeahead-window//button[normalize-space()={g_lit}]",
        f"xpath=//ngb-typeahead-window//*[normalize-space()={g_lit}]",
        f"xpath=//*[@role='option' and normalize-space()={g_lit}]",
        f"xpath=//button[contains(@class,'dropdown-item') and normalize-space()={g_lit}]",
        f"xpath=//li[normalize-space()={g_lit}]",
        f"xpath=//ngb-typeahead-window//button[contains(translate(normalize-space(.),'{upper}','{lower}'),{g_lower_lit})]",
        f"xpath=//button[contains(@class,'dropdown-item')][contains(translate(normalize-space(.),'{upper}','{lower}'),{g_lower_lit})]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=2500)
            option = loc
            break
        except Exception:
            continue
    if option is None:
        raise Exception(f"Opsi game '{game_name}' tidak muncul di typeahead")
    option.click()
    smart_wait(page, 600, 1100)


# ===================== STEP 2: CATEGORY =====================
def _select_category_accounts(page):
    """Klik kartu kategori 'Accounts'."""
    add_log("[PA] Pilih Category: Accounts")
    card = None
    for sel in [
        "xpath=//div[contains(@class,'product-box')][.//p[normalize-space()='Accounts']]",
        "xpath=//*[contains(@class,'product-box')][.//*[normalize-space()='Accounts']]",
        "div.product-box:has(p:has-text('Accounts'))",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=5000)
            card = loc
            break
        except Exception:
            continue
    if card is None:
        raise Exception("Category card 'Accounts' tidak ditemukan")
    card.click()
    smart_wait(page, 1500, 2500)


# ===================== STEP 3: FORM OPTIONS SCRAPE =====================
def _scrape_form_options_page(page):
    """Scrape semua nz-select di form. Label derived dari formcontrolname.
    Return {label: [options]}. Child cascading (disabled/empty) di-skip -
    akan di-scrape live saat create_listing."""
    options_map = {}

    smart_wait(page, 2500, 4000)

    # Enum semua nz-select; ambil formcontrolname -> label.
    pending = []  # list of (label, formcontrolname)
    try:
        selects = page.locator("nz-select").all()
        for sel_el in selects:
            try:
                fcn = sel_el.get_attribute("formcontrolname", timeout=500) or ""
            except Exception:
                fcn = ""
            label = _label_from_formcontrolname(fcn) or fcn
            if not label:
                continue
            if label not in [p[0] for p in pending]:
                pending.append((label, fcn))
    except Exception as e:
        add_log(f"[PA] Gagal enum nz-select: {str(e)[:80]}")

    add_log(f"[PA] Detected {len(pending)} nz-select: {[p[0] for p in pending]}")

    for label, fcn in pending:
        try:
            # Re-resolve trigger biar DOM fresh
            sel_el = page.locator(f"nz-select[formcontrolname={_css_attr(fcn)}]").first
            if sel_el.count() == 0:
                add_log(f"[PA]    {label}: nz-select[{fcn}] hilang, skip")
                continue
            # Cek apakah disabled (cascading child)
            cls = (sel_el.get_attribute("class") or "")
            if "ant-select-disabled" in cls:
                add_log(f"[PA]    {label}: disabled (cascading child), skip - scrape live saat fill")
                continue

            trigger = sel_el.locator(".ant-select-selector").first
            trigger.click()
            smart_wait(page, 500, 900)

            opts = _collect_antd_options(page)
            if opts:
                options_map[label] = opts
                add_log(f"[PA]    - {label}: {len(opts)} opsi -> {opts[:5]}{'...' if len(opts)>5 else ''}")
            else:
                add_log(f"[PA]    {label}: opsi kosong (mungkin cascading child)")

            _close_antd_dropdown(page)
        except Exception as e:
            add_log(f"[PA] Gagal scrape '{label}': {str(e)[:80]}")
            _close_antd_dropdown(page)
            continue

    return options_map


def _css_attr(s):
    """Escape attribute value buat CSS selector."""
    return '"' + (s or "").replace('\\', '\\\\').replace('"', '\\"') + '"'


# ===================== ENTRY POINTS =====================
def scrape_form_options(game_name):
    """Buka /offers/creation, pilih game, pilih Accounts, scrape nz-select.
    Return dict / {} / None."""
    add_log("[PA] Scrape form options dari PlayerAuctions (pertama kali)...")
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

            page.goto(PA_START_URL, wait_until="networkidle", timeout=30000)
            _set_worker_tab_title(page)
            smart_wait(page, 2500, 4000)

            _select_game(page, game_name)
            _select_category_accounts(page)
            return _scrape_form_options_page(page)

        except Exception as e:
            add_log(f"[PA] Gagal scrape form options: {str(e)[:100]}")
            return None
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


def _pick_option_in_nz_select(page, sel_el, preferred_value=None):
    """Buka nz-select, pilih opsi. Priority: preferred_value kalau ada & valid,
    fallback opsi pertama. Return (ok, picked_value)."""
    trigger = sel_el.locator(".ant-select-selector").first
    trigger.click()
    smart_wait(page, 500, 900)

    opts = _collect_antd_options(page)
    if not opts:
        _close_antd_dropdown(page)
        return False, None

    # Klik opsi yg match preferred_value, atau pertama kalau ndak.
    pick = None
    if preferred_value and preferred_value in opts:
        pick = preferred_value
    else:
        pick = opts[0]

    p_lit = _xpath_literal(pick)
    clicked = False
    for sel in [
        f"xpath=//.[contains(@class,'ant-select-dropdown') and not(contains(@class,'hidden'))]"
        f"//*[contains(@class,'ant-select-item-option')][.//.[normalize-space()={p_lit}]]",
        f"xpath=//.[contains(@class,'ant-select-dropdown') and not(contains(@class,'hidden'))]"
        f"//*[contains(@class,'ant-select-item-option-content') and normalize-space()={p_lit}]",
        f"xpath=//*[@role='option' and normalize-space()={p_lit}]",
        f"xpath=//*[contains(@class,'ant-select-item-option')][normalize-space()={p_lit}]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=1500)
            loc.click()
            clicked = True
            break
        except Exception:
            continue
    if not clicked:
        _close_antd_dropdown(page)
        return False, None
    smart_wait(page, 400, 700)
    return True, pick


def _click_radio_by_text(page, text, scope=None):
    """Click ant radio yg label-nya match text. scope optional XPath scope."""
    t_lit = _xpath_literal(text)
    base = scope or ""
    for sel in [
        f"xpath={base}//span[contains(@class,'ant-radio-wrapper')][.//span[normalize-space()={t_lit}]]",
        f"xpath={base}//label[.//span[normalize-space()={t_lit}] and contains(@class,'ant-radio-wrapper')]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=3000)
            loc.click()
            return True
        except Exception:
            continue
    return False


def _click_radio_after_heading(page, heading_text, radio_text):
    """Click radio yg match radio_text, tapi HARUS ber-ancestor-preceding
    heading_text. Ini buat disambiguasi radio group yg punya option sama
    (misal After-Sale Protection & Offer Duration dua-duanya punya '30 Days')."""
    h_lit = _xpath_literal(heading_text)
    r_lit = _xpath_literal(radio_text)
    # Cari elemen teks heading, lalu radio terdekat setelahnya (following axis
    # biar stop di section heading berikutnya kalau ada).
    for sel in [
        f"xpath=(//*[normalize-space()={h_lit}]/following::span"
        f"[contains(@class,'ant-radio-wrapper')][.//span[normalize-space()={r_lit}]])[1]",
        f"xpath=(//*[contains(normalize-space(),{h_lit})]/following::span"
        f"[contains(@class,'ant-radio-wrapper')][.//span[normalize-space()={r_lit}]])[1]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=3000)
            loc.click()
            return True
        except Exception:
            continue
    return False


def _fill_tinymce(page, body_text):
    """Fill TinyMCE iframe contenteditable body dengan plain text."""
    # TinyMCE iframe id biasanya '_tinymce-<hash>_ifr' atau 'mce_<n>_ifr'
    frame = None
    for sel in [
        "iframe[id*='_ifr']",
        "iframe[id*='tinymce']",
        "iframe.tox-edit-area__iframe",
    ]:
        try:
            f = page.frame_locator(sel).first
            body = f.locator("body")
            body.wait_for(state="visible", timeout=5000)
            frame = (f, body)
            break
        except Exception:
            continue
    if frame is None:
        raise Exception("TinyMCE iframe tidak ketemu")
    _, body = frame
    try:
        body.click()
        smart_wait(page, 200, 400)
    except Exception:
        pass
    # Clear + type
    try:
        body.press("Control+A")
        body.press("Delete")
    except Exception:
        pass
    # fill() works on contenteditable
    try:
        body.fill(body_text)
    except Exception:
        # Fallback keyboard.type
        try:
            body.click()
            page.keyboard.type(body_text, delay=3)
        except Exception:
            raise


def create_listing(game_name, title, deskripsi, harga, field_mapping, image_paths,
                   raw_image_url=None, login_name=None):
    """Full PA create flow. Return (ok, err, uploaded_count)."""
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

            page.goto(PA_START_URL, wait_until="networkidle", timeout=30000)
            _set_worker_tab_title(page)
            smart_wait(page, 2500, 4000)

            # Step 1+2: Select game + Accounts category
            _select_game(page, game_name)
            _select_category_accounts(page)
            smart_wait(page, 2000, 3000)

            # Step 3: Isi semua nz-select. Iterate IN DOM ORDER. Mapping dari AI
            # priority, fallback pertama (buat cascading child). Wait enabled sblm click.
            try:
                selects = page.locator("nz-select").all()
                for idx, sel_el in enumerate(selects):
                    try:
                        fcn = sel_el.get_attribute("formcontrolname", timeout=500) or ""
                    except Exception:
                        fcn = ""
                    label = _label_from_formcontrolname(fcn) or fcn or f"field{idx}"

                    # Wait enabled max 5s (buat cascading: parent mungkin baru kepilih)
                    enabled = False
                    for _ in range(25):
                        try:
                            cls = sel_el.get_attribute("class", timeout=300) or ""
                        except Exception:
                            cls = ""
                        if "ant-select-disabled" not in cls:
                            enabled = True
                            break
                        page.wait_for_timeout(200)
                    if not enabled:
                        add_log(f"[PA] {label}: tetap disabled, skip")
                        continue

                    preferred = (field_mapping or {}).get(label)
                    ok_pick, picked = _pick_option_in_nz_select(page, sel_el, preferred)
                    if ok_pick:
                        src = "AI" if preferred and preferred == picked else "first-option"
                        add_log(f"[PA] Isi {label}: {picked} ({src})")
                    else:
                        add_log(f"[PA] {label}: gagal pilih")
            except Exception as e:
                return False, f"nz-select fill: {str(e)[:100]}", uploaded

            # Step 4: Price (kolom H)
            price_clean = re.sub(r"[^0-9.,]", "", str(harga)).replace(",", ".")
            if price_clean.count(".") > 1:
                parts = price_clean.split(".")
                price_clean = "".join(parts[:-1]) + "." + parts[-1]
            # PA minimum price policy: $5. Kalau harga sumber < $5, naikkan ke $5
            # biar form ndak reject (PA tolak listing < $5 sejak 2026).
            try:
                if float(price_clean) < 5:
                    add_log(f"[PA] Harga sumber ${price_clean} < $5 minimum, override ke $5")
                    price_clean = "5"
            except (ValueError, TypeError):
                pass  # biar error handling existing di page.fill yang nangkap
            add_log(f"[PA] Isi Price: ${price_clean}")
            try:
                price_input = page.locator(
                    "input#price, input[formcontrolname='price'],"
                    " nz-input-number-group input.ant-input-number-input"
                ).first
                price_input.wait_for(state="visible", timeout=5000)
                price_input.fill(price_clean)
                smart_wait(page, 400, 700)
            except Exception as e:
                return False, f"Price: {str(e)[:100]}", uploaded

            # Step 5: After-Sale Protection = None (scope ke section heading
            # biar ndak kena 'None' di section lain)
            add_log("[PA] Pilih After-Sale Protection: None")
            if not _click_radio_after_heading(page, "After-Sale Protection", "None"):
                # Fallback: first 'None' radio di page
                if not _click_radio_by_text(page, "None"):
                    return False, "After-Sale: radio 'None' tidak ketemu", uploaded
            smart_wait(page, 300, 600)

            # Step 6: Manual Delivery tab
            add_log("[PA] Klik tab Manual Delivery")
            try:
                for sel in [
                    "button.manualBtn",
                    "xpath=//button[normalize-space()='Manual Delivery']",
                    "xpath=//button[.//span[normalize-space()='Manual Delivery']]",
                ]:
                    try:
                        loc = page.locator(sel).first
                        loc.wait_for(state="visible", timeout=3000)
                        loc.click()
                        break
                    except Exception:
                        continue
                smart_wait(page, 1000, 1800)
            except Exception as e:
                return False, f"Manual Delivery tab: {str(e)[:100]}", uploaded

            # Step 7: Isi Login ID + Retype (keduanya value sama dari login_name)
            if login_name:
                add_log(f"[PA] Isi Login ID: {login_name}")
                try:
                    for fcn in ("loginName", "retypeLoginName"):
                        inp = page.locator(f"input[formcontrolname='{fcn}']").first
                        inp.wait_for(state="visible", timeout=5000)
                        inp.fill(str(login_name))
                        smart_wait(page, 250, 500)
                except Exception as e:
                    return False, f"Login ID: {str(e)[:100]}", uploaded
            else:
                add_log("[PA] login_name kosong - skip (kolom B sheet kosong?)")

            # Step 8: Klik semua radio 'Yes' yg belum kepilih (5 biji under manual).
            # Re-query nth() tiap iterasi (stale handle kalau Angular re-render
            # setelah click). Plus ada verify-pass kedua yg retry yg belum ke-check.
            add_log("[PA] Klik 5x radio 'Yes'")
            try:
                yes_sel = (
                    "xpath=//span[contains(@class,'ant-radio-wrapper')]"
                    "[.//span[normalize-space()='Yes']]"
                )
                yes_elements = page.locator(yes_sel)
                total = yes_elements.count()

                # Pass 1: click semua
                clicked_yes = 0
                for i in range(total):
                    try:
                        w = yes_elements.nth(i)
                        cls = w.get_attribute("class", timeout=300) or ""
                        if "ant-radio-wrapper-checked" in cls:
                            continue
                        if not w.is_visible():
                            continue
                        w.click(force=True)
                        clicked_yes += 1
                        # Delay lebih panjang supaya Angular form control
                        # commit state sebelum next click (bikin state pertama
                        # ndak ke-reset saat render berikutnya).
                        smart_wait(page, 400, 700)
                    except Exception:
                        continue
                add_log(f"[PA] Pass 1: {clicked_yes} radio 'Yes' di-click")

                # Pass 2: verify + retry yg masih uncheck
                reclick = 0
                for attempt in range(3):  # max 3 retry pass
                    fixed_in_pass = 0
                    total = yes_elements.count()
                    for i in range(total):
                        try:
                            w = yes_elements.nth(i)
                            cls = w.get_attribute("class", timeout=300) or ""
                            if "ant-radio-wrapper-checked" in cls:
                                continue
                            if not w.is_visible():
                                continue
                            w.click(force=True)
                            reclick += 1
                            fixed_in_pass += 1
                            smart_wait(page, 400, 700)
                        except Exception:
                            continue
                    if fixed_in_pass == 0:
                        break
                if reclick:
                    add_log(f"[PA] Pass retry: {reclick} radio 'Yes' re-click")
            except Exception as e:
                add_log(f"[PA] Gagal klik Yes radios: {str(e)[:80]}")

            # Step 9: Radio '1 Hour'
            add_log("[PA] Pilih '1 Hour'")
            if not _click_radio_by_text(page, "1 Hour"):
                return False, "Radio '1 Hour' tidak ketemu", uploaded
            smart_wait(page, 300, 600)

            # Step 10: Title (kolom J)
            add_log(f"[PA] Isi Title: {title[:60]}")
            try:
                title_input = page.locator("input[formcontrolname='title']").first
                title_input.wait_for(state="visible", timeout=5000)
                title_input.fill(title)
                smart_wait(page, 400, 700)
            except Exception as e:
                return False, f"Title: {str(e)[:100]}", uploaded

            # Step 11: Upload Cover Image (1 gambar)
            to_upload = (image_paths or [])[:PA_MAX_IMAGES]
            if to_upload:
                add_log(f"[PA] Upload Cover Image: {to_upload[0]}")
                try:
                    file_input = page.locator("app-image-upload input[type='file']").first
                    file_input.set_input_files(to_upload[0])
                    uploaded = 1
                    # Wait sampai preview muncul (baseline 0 -> 1) atau timeout
                    preview_ok = False
                    for _ in range(120):  # 60s max
                        try:
                            if page.locator(
                                "app-image-upload img, app-image-upload [class*='preview']"
                            ).count() > 0:
                                preview_ok = True
                                break
                        except Exception:
                            pass
                        page.wait_for_timeout(500)
                    if not preview_ok:
                        add_log("[PA] Cover image preview ndak muncul dalam 60s - lanjut")
                    else:
                        smart_wait(page, 600, 1000)
                except Exception as e:
                    return False, f"Cover Image: {str(e)[:100]}", uploaded

            # Step 12: Description di TinyMCE (line 1 = raw URL stripped, line 2+ = desc)
            add_log("[PA] Isi Description (TinyMCE)")
            try:
                if raw_image_url:
                    raw_line = re.sub(r"^https?://", "", raw_image_url.strip())
                    body_text = f"Full Screenshot Detail: {raw_line}\n{deskripsi or ''}"
                else:
                    body_text = deskripsi or ""
                _fill_tinymce(page, body_text)
                smart_wait(page, 500, 900)
            except Exception as e:
                return False, f"Description: {str(e)[:100]}", uploaded

            # Step 13: Offer Duration = 30 Days (scope ke section heading
            # biar ndak kena '30 Days' di After-Sale Protection)
            add_log("[PA] Pilih Offer Duration: 30 Days")
            if not _click_radio_after_heading(page, "Offer Duration", "30 Days"):
                return False, "Radio Offer Duration '30 Days' tidak ketemu", uploaded
            smart_wait(page, 300, 600)

            # Step 14: Agree checkbox. Ant Design: formcontrolname ada di <p>,
            # bukan di <input>. Klik .ant-checkbox-inner (visible box) yg ngerti
            # click handler-nya. Multi-strategy + verifikasi state.
            add_log("[PA] Centang agreeCheck")
            try:
                wrapper_sel = "p[formcontrolname='agreeCheck']"
                # Strategy 1: click .ant-checkbox-inner inside the wrapper
                clicked = False
                for target_sel in [
                    f"{wrapper_sel} .ant-checkbox-inner",
                    f"{wrapper_sel} .ant-checkbox",
                    wrapper_sel,
                ]:
                    try:
                        loc = page.locator(target_sel).first
                        loc.wait_for(state="visible", timeout=2000)
                        loc.click(force=True)
                        clicked = True
                        break
                    except Exception:
                        continue

                # Verifikasi checked - cek class 'ant-checkbox-wrapper-checked'
                # atau input.checked via JS
                def _is_checked():
                    try:
                        cls = (page.locator(wrapper_sel).first.get_attribute(
                            "class", timeout=500) or "")
                        if "ant-checkbox-wrapper-checked" in cls:
                            return True
                    except Exception:
                        pass
                    try:
                        return bool(page.evaluate(
                            "(s) => { const p=document.querySelector(s);"
                            " if(!p) return false;"
                            " const i=p.querySelector(\"input[type='checkbox']\");"
                            " return i && i.checked; }",
                            wrapper_sel,
                        ))
                    except Exception:
                        return False

                smart_wait(page, 300, 500)
                if not _is_checked():
                    # Strategy 2: JS direct input.click()
                    add_log("[PA] agreeCheck masih uncheck, pakai JS click fallback")
                    try:
                        page.evaluate(
                            "(s) => { const p=document.querySelector(s);"
                            " if(!p) return;"
                            " const i=p.querySelector(\"input[type='checkbox']\");"
                            " if(i && !i.checked) i.click(); }",
                            wrapper_sel,
                        )
                    except Exception:
                        pass
                    smart_wait(page, 300, 500)

                if not _is_checked():
                    return False, "agreeCheck tidak ter-centang", uploaded
            except Exception as e:
                return False, f"Agree checkbox: {str(e)[:100]}", uploaded

            # Step 15: Click CREATE NEW OFFER submit
            add_log("[PA] Klik CREATE NEW OFFER")
            try:
                submit = None
                for sel in [
                    "xpath=//button[@type='submit'][.//span[normalize-space()='CREATE NEW OFFER']]",
                    "xpath=//button[.//span[contains(translate(normalize-space(.),"
                    "'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'CREATE NEW OFFER')]]",
                    "button.ant-btn-primary[type='submit']",
                ]:
                    try:
                        loc = page.locator(sel).first
                        loc.wait_for(state="visible", timeout=5000)
                        submit = loc
                        break
                    except Exception:
                        continue
                if submit is None:
                    return False, "CREATE NEW OFFER button tidak ketemu", uploaded
                start_url = page.url
                submit.click()
            except Exception as e:
                return False, f"Submit: {str(e)[:100]}", uploaded

            # Step 16: Sukses detection - redirect ke /offers/creation/success
            redirected = False
            for _ in range(120):  # 1 menit
                page.wait_for_timeout(500)
                try:
                    cur = page.url
                    if cur != start_url and "/creation/success" in cur:
                        redirected = True
                        break
                except Exception:
                    pass

            if redirected:
                add_log(f"[PA] Redirect ke: {page.url} -> sukses")
                smart_wait(page, 800, 1500)
                return True, None, uploaded

            # Fallback: cek error message
            try:
                err_msgs = []
                for sel in [
                    ".ant-message-error", ".ant-notification-notice-error",
                    ".ant-form-item-has-error .ant-form-item-explain",
                    "[role='alert']",
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
                    add_log(f"[PA] Form error: {combined[:200]}")
                    return False, f"Form error: {combined[:120]}", uploaded
            except Exception:
                pass
            return False, "Submit: tidak redirect dalam 2 menit", uploaded

        except Exception as e:
            pesan = str(e)
            if "Timeout" in pesan:
                err_msg = "Waktu habis, elemen tidak ditemukan"
            elif "net::" in pesan:
                err_msg = "Gagal membuka halaman, cek koneksi"
            else:
                err_msg = f"Error: {pesan[:100]}"
            add_log(f"[PA] Gagal: {err_msg}")
            return False, err_msg, uploaded
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


def cache_looks_bogus(cache_dict):
    """PA: default False. Heuristik nanti kalau AI salah pilih."""
    return False


def run(sheet, baris_nomor, worker_id, *, game_name, description, title, harga,
        field_mapping, image_paths=None, image_urls=None,
        raw_image_url=None, is_imgur=False):
    """Adapter entry. Baca kolom B (login_name) dari sheet utk diisi ke
    loginName+retypeLoginName input di PA. Return (ok, k_line)."""
    _worker_local.worker_id = f"{worker_id}-PA"

    login_name = None
    try:
        raw = sheet.cell(baris_nomor, 2).value  # B = col 2
        if raw is not None:
            login_name = str(raw).strip()
    except Exception as e:
        add_log(f"[PA] Gagal baca kolom B: {str(e)[:80]}")

    ok, err, uploaded = create_listing(
        game_name, title, description or "", harga,
        field_mapping or {}, (image_paths or [])[:PA_MAX_IMAGES],
        raw_image_url=raw_image_url,
        login_name=login_name,
    )
    ts = datetime.now().strftime("%d %b, %y | %H:%M")
    if ok:
        return True, f"✅ PA | {uploaded} images uploaded | {ts}"
    return False, f"❌ PA | {(err or 'unknown')[:80]}"
