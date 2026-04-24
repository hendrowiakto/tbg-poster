"""create/U7.py - u7buy.com marketplace adapter.

Entry points dipakai orchestrator (bot_create.py):
- scrape_form_options(game_name) -> dict | {} | None
- create_listing(...) -> (ok, err, uploaded_count)
- run(sheet, baris_nomor, worker_id, **kwargs) -> (ok, k_line)
- cache_looks_bogus(cache) -> bool

Flow U7 (per user spec):
  Step 1: goto /member/offers/create-offer -> pilih Game -> pilih Type
  Step 2: (scrape) collect dropdown options (flat, no cascading)
  Step 3: fill form: dynamic dropdowns (AI mapping), Price (H), Title (J)
  Step 4: upload gambar one-by-one (max 5), per-upload klik Confirm popup
  Step 5: Description (baris 1 = raw image URL tanpa https://, mirip ZEUS)
  Step 6: centang 3x Terms checkbox
  Step 7: klik Publish -> sukses redirect ke /member/offers
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
    resolve_image_future,
)


# U7 throttle: base × 1.0 + 0-10% jitter (user request "1x umum saja").
_U7_SLOW_MULT      = 1.0
_U7_JITTER_MAX     = 0.10


def smart_wait(page, min_ms, max_ms):
    """U7-local smart_wait: base × 1.0 + random 0-10% extra jitter."""
    jitter = 1.0 + random.uniform(0, _U7_JITTER_MAX)
    lo = int(min_ms * _U7_SLOW_MULT * jitter)
    hi = int(max_ms * _U7_SLOW_MULT * jitter)
    if hi < lo:
        hi = lo
    _base_smart_wait(page, lo, hi)


# ===================== KONSTANTA =====================
U7_ORIGIN          = "https://www.u7buy.com"
U7_START_URL       = "https://www.u7buy.com/member/offers/create-offer"
U7_SUCCESS_URL     = "https://www.u7buy.com/member/offers"
U7_MAX_IMAGES      = 3

# Adapter protocol:
MARKET_CODE        = "U7"
HARGA_COL          = 8                                 # H
MAX_IMAGES         = U7_MAX_IMAGES                     # orchestrator: adaptive download
NO_OPTIONS_SENTINEL_U7 = "[tidak ditemukan options U7]"
CACHE_SENTINEL     = NO_OPTIONS_SENTINEL_U7


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


# ===================== STEP 1-4: NAVIGASI AWAL =====================
def _click_game_service_card(page):
    """Step 1: klik kartu 'Game Service'. Nuxt loading indicator kadang masih
    aktif; tunggu hilang dulu biar click ndak ke-intercept. Plus multi-strategy
    click + verifikasi URL berubah (step 2 page)."""
    add_log("[U7] Klik Game Service card")

    # Tunggu nuxt loading indicator hilang (max 5s)
    for _ in range(25):
        try:
            li = page.locator("div.nuxt-loading-indicator").first
            if li.count() == 0:
                break
            op = (li.get_attribute("style", timeout=300) or "")
            if "opacity: 0" in op:
                break
        except Exception:
            break
        page.wait_for_timeout(200)

    pre_url = page.url
    card = None
    for sel in [
        # Exact class scope - span text match biar lebih spesifik
        "xpath=//div[contains(concat(' ',normalize-space(@class),' '),' create-offer-game-service ')][.//span[normalize-space()='Game Service']]",
        "xpath=//div[@class='create-offer-game-service']",
        "div.create-offer-game-service",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=5000)
            card = loc
            break
        except Exception:
            continue
    if card is None:
        raise Exception("Game Service card tidak ketemu")

    # Multi-strategy click + verifikasi navigasi ke step 2 (choose-offer-step2)
    for attempt in range(3):
        try:
            if attempt == 0:
                card.click()
            elif attempt == 1:
                card.click(force=True)
            else:
                # JS click fallback
                card.evaluate("el => el.click()")
        except Exception:
            continue
        # Tunggu URL berubah atau page transition (Nuxt SPA, URL mungkin same tapi
        # DOM switch ke 'choose-game-product' / step2 indicator)
        for _ in range(25):  # 5s max
            try:
                cur = page.url
                if cur != pre_url:
                    smart_wait(page, 500, 900)
                    return
                # DOM check: step 2 page punya 'choose-game-product' atau
                # input typeahead Game
                n = page.locator(
                    "xpath=//*[contains(@class,'choose-game-product')]"
                    " | //input[@id='typeahead-focus' or @placeholder='Search Game here']"
                ).count()
                if n > 0:
                    smart_wait(page, 500, 900)
                    return
            except Exception:
                pass
            page.wait_for_timeout(200)
        add_log(f"[U7] Click attempt {attempt+1}/3: URL/DOM belum berubah, retry")

    raise Exception("Click Game Service card ndak trigger navigasi setelah 3 attempt")


def _select_game_u7(page, game_name):
    """Step 2: isi Game di searchable dropdown 'Search Game here'."""
    add_log(f"[U7] Pilih Game: {game_name}")

    # Trigger dropdown - el-select__wrapper yg filterable
    trigger = None
    for sel in [
        "xpath=//div[contains(@class,'el-form-item')][.//*[normalize-space()='Choose a Game']]"
        "//div[contains(@class,'el-select__wrapper')]",
        "xpath=//*[normalize-space()='Choose a Game']/following::div[contains(@class,'el-select__wrapper')][1]",
        "div.el-select__wrapper.is-filterable",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=5000)
            trigger = loc
            break
        except Exception:
            continue
    if trigger is None:
        raise Exception("Game dropdown trigger tidak ketemu")

    trigger.click()
    smart_wait(page, 500, 900)

    # Ketik di search input yg muncul
    try:
        page.keyboard.type(game_name, delay=50)
    except Exception:
        pass
    smart_wait(page, 700, 1200)

    # Klik opsi match
    g_lit = _xpath_literal(game_name)
    g_lower_lit = _xpath_literal(game_name.lower())
    upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    lower = "abcdefghijklmnopqrstuvwxyz"
    for sel in [
        f"xpath=//li[contains(@class,'el-select-dropdown__item') and normalize-space()={g_lit}]",
        f"xpath=//li[normalize-space()={g_lit}]",
        f"xpath=//*[@role='option' and normalize-space()={g_lit}]",
        f"xpath=//li[contains(@class,'el-select-dropdown__item')][contains(translate(normalize-space(.),'{upper}','{lower}'),{g_lower_lit})]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=2500)
            loc.click()
            smart_wait(page, 400, 700)
            return
        except Exception:
            continue
    raise Exception(f"Opsi game '{game_name}' tidak muncul di dropdown")


def _select_category_accounts(page):
    """Step 3: pilih Category 'Accounts'. U7 Category pakai u7-select custom
    (bukan el-select), wrapped in u7-wrapper. Placeholder 'Select'. Dropdown
    mount delay ~1s, butuh polling."""
    add_log("[U7] Pilih Category: Accounts")

    trigger = None
    for sel in [
        # u7-select primary (layout sekarang)
        "xpath=//div[contains(@class,'el-form-item')][.//*[normalize-space()='Choose Product Category']]"
        "//div[contains(@class,'u7-select')]",
        "xpath=//*[normalize-space()='Choose Product Category']/following::div[contains(@class,'u7-select')][1]",
        # el-select fallback (kalau berubah layout)
        "xpath=//div[contains(@class,'el-form-item')][.//*[normalize-space()='Choose Product Category']]"
        "//div[contains(@class,'el-select__wrapper')]",
        "xpath=//*[normalize-space()='Choose Product Category']/following::div[contains(@class,'el-select__wrapper')][1]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=5000)
            trigger = loc
            break
        except Exception:
            continue
    if trigger is None:
        raise Exception("Category dropdown trigger tidak ketemu")

    option = None
    for attempt in range(3):
        try:
            trigger.click()
        except Exception:
            pass
        # Poll max 3s sampai option muncul. Coba u7-list (u7-select) dulu, lalu
        # el-select-dropdown, lalu role=option generic.
        for _ in range(15):
            page.wait_for_timeout(200)
            for sel in [
                "xpath=//ul[contains(@class,'u7-list')]//li[.//span[normalize-space()='Accounts']]",
                "xpath=//ul[contains(@class,'u7-list')]//li[normalize-space()='Accounts']",
                "xpath=//li[contains(@class,'el-select-dropdown__item') and normalize-space()='Accounts']",
                "xpath=//li[normalize-space()='Accounts']",
                "xpath=//*[@role='option' and normalize-space()='Accounts']",
            ]:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        option = loc
                        break
                except Exception:
                    continue
            if option is not None:
                break
        if option is not None:
            break
        add_log(f"[U7] Category dropdown belum muncul, retry click ({attempt+1}/3)")

    if option is None:
        raise Exception("Opsi 'Accounts' tidak muncul setelah 3x retry")

    option.click()
    smart_wait(page, 400, 700)


def _click_next_step(page):
    """Step 4: klik tombol 'Next step' (aside.u7-button). Verifikasi transisi
    ke halaman 'Provide Product Detail' via polling heading/form element."""
    add_log("[U7] Klik Next step")
    clicked = False
    for sel in [
        "xpath=//aside[contains(@class,'u7-button') and normalize-space()='Next step']",
        "xpath=//aside[contains(@class,'u7-button')][.//text()[contains(.,'Next step')]]",
        "xpath=//*[normalize-space()='Next step']/ancestor-or-self::aside[1]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=5000)
            loc.click()
            clicked = True
            break
        except Exception:
            continue
    if not clicked:
        raise Exception("Tombol 'Next step' tidak ketemu")

    # Verifikasi transisi: tunggu 'Provide Product Detail' heading atau u7-select
    # muncul. Max 15s.
    transitioned = False
    for _ in range(30):
        page.wait_for_timeout(500)
        try:
            n = page.locator(
                "xpath=//*[normalize-space()='Provide Product Detail']"
                " | //div[contains(@class,'u7-select')]"
                " | //textarea[@placeholder='Please Enter']"
            ).count()
            if n > 0:
                transitioned = True
                break
        except Exception:
            pass
    if not transitioned:
        add_log(f"[U7] Next step click: transisi ndak terdeteksi dalam 15s (url={page.url[:60]})")
    else:
        add_log(f"[U7] Next step OK - url: {page.url[:60]}")
    smart_wait(page, 1500, 2500)


# ===================== DROPDOWN HELPERS =====================
def _open_u7_select(page, trigger_locator):
    """Buka u7-select dropdown dengan multi-strategy click + retry loop.
    Layout u7: div.u7-select (tabindex=0) > div.u7-placeholder (Vue click handler).
    Intermittent: click kadang ndak trigger karena element belum stable/visible
    atau Vue state transient. Retry 3 round, tiap round 4 strategy berbeda."""
    def _opts_visible():
        try:
            return page.locator(
                "ul.u7-list:visible, li.el-select-dropdown__item:visible"
            ).count() > 0
        except Exception:
            return False

    # Ensure visible + scroll to center sebelum click (element di bawah viewport
    # sering bikin click miss hit atau opts render di luar layar).
    try:
        trigger_locator.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        trigger_locator.wait_for(state="visible", timeout=3000)
    except Exception:
        pass
    smart_wait(page, 200, 400)

    for attempt in range(3):
        # Strategy 1: click outer div.u7-select
        try:
            trigger_locator.click(timeout=3000)
            smart_wait(page, 500, 800)
            if _opts_visible():
                return True
        except Exception:
            pass

        # Strategy 2: click inner .u7-placeholder (actual click handler target)
        try:
            inner = trigger_locator.locator(".u7-placeholder").first
            if inner.count() > 0:
                inner.click(timeout=3000)
                smart_wait(page, 500, 800)
                if _opts_visible():
                    return True
        except Exception:
            pass

        # Strategy 3: focus + Enter (tabindex=0 keyboard activation)
        try:
            trigger_locator.focus()
            page.keyboard.press("Enter")
            smart_wait(page, 500, 800)
            if _opts_visible():
                return True
        except Exception:
            pass

        # Strategy 4: JS-level click (bypass any click interception/overlay)
        try:
            trigger_locator.evaluate("(el) => el.click()")
            smart_wait(page, 500, 800)
            if _opts_visible():
                return True
            # Juga coba fire event manual untuk Vue listener
            trigger_locator.evaluate("""(el) => {
                ['mousedown','mouseup','click'].forEach(t => {
                    el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));
                });
            }""")
            smart_wait(page, 500, 800)
            if _opts_visible():
                return True
        except Exception:
            pass

        # Kalau masih gagal, close sisa dropdown yg mungkin half-open + retry
        if attempt < 2:
            try:
                _close_u7_dropdown(page)
            except Exception:
                pass
            smart_wait(page, 600, 900)

    return False


def _collect_u7_options(page):
    """Collect opsi visible dari u7-select/el-select dropdown portal."""
    texts = []
    for sel in [
        "ul.u7-list:visible li.flex",
        "ul.u7-list:visible li",
        "li.el-select-dropdown__item:visible",
        ".el-select-dropdown:not(.el-select-dropdown--hidden) li",
        "[role='option']:visible",
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


def _close_u7_dropdown(page):
    """Tutup dropdown: Escape -> click outside -> JS dispatch."""
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
            if page.locator("ul.u7-list:visible, .el-select-dropdown:not(.el-select-dropdown--hidden)").count() == 0:
                return
        except Exception:
            return


def _click_option_by_text(page, text):
    """Klik opsi dropdown yg match text (any visible)."""
    t_lit = _xpath_literal(text)
    for sel in [
        f"xpath=//ul[contains(@class,'u7-list')]//li[.//span[normalize-space()={t_lit}]]",
        f"xpath=//li[contains(@class,'el-select-dropdown__item') and normalize-space()={t_lit}]",
        f"xpath=//li[.//span[normalize-space()={t_lit}]]",
        f"xpath=//*[@role='option' and normalize-space()={t_lit}]",
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


# ===================== STEP 7+ HELPERS =====================
def _fill_product_name(page, title):
    """Step 5: fill Product Name input (kolom J)."""
    add_log(f"[U7] Isi Product Name: {title[:60]}")
    for sel in [
        "xpath=//div[contains(@class,'el-form-item')][.//*[normalize-space()='Product Name']]"
        "//input[contains(@class,'el-input__inner')]",
        "xpath=//*[normalize-space()='Product Name']/following::input[contains(@class,'el-input')][1]",
        "input.el-input__inner[placeholder='Please Enter']",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=5000)
            loc.fill(title)
            smart_wait(page, 400, 700)
            return
        except Exception:
            continue
    raise Exception("Product Name input tidak ketemu")


def _upload_one_image_u7(page, path, idx, total, timeout_ms=30000):
    """Upload 1 gambar: click upload area -> set file -> wait confirm popup ->
    click Confirm -> wait thumbnail. Return True/False."""
    baseline = 0
    try:
        baseline = page.locator("ul.el-upload-list li.is-success").count()
    except Exception:
        pass

    add_log(f"[U7] Upload gambar {idx}/{total} (baseline={baseline})...")

    # Set file: cari hidden input[type=file] - langsung set tanpa klik area
    try:
        file_input = page.locator("input.el-upload__input[type='file']").first
        if file_input.count() == 0:
            file_input = page.locator("input[type='file']").first
        file_input.set_input_files(path)
    except Exception as e:
        # Fallback: click drag-area buat trigger file chooser
        try:
            with page.expect_file_chooser(timeout=4000) as fc_info:
                page.locator("div.el-upload.el-upload--picture-card.is-drag").first.click()
            fc = fc_info.value
            fc.set_files(path)
        except Exception as e2:
            raise Exception(f"set_files gagal: {str(e)[:40]} / {str(e2)[:40]}")

    # Tunggu popup Confirm muncul (el-modal-dialog)
    confirm_clicked = False
    for _ in range(60):  # 30s max
        try:
            confirm_btn = page.locator(
                "xpath=//div[contains(@class,'el-dialog') or contains(@class,'el-modal-dialog')]"
                "//aside[contains(@class,'u7-button')][normalize-space()='Confirm']"
            ).first
            if confirm_btn.count() > 0 and confirm_btn.is_visible():
                confirm_btn.click()
                confirm_clicked = True
                break
        except Exception:
            pass
        # Fallback selector
        try:
            for s in [
                "xpath=//aside[contains(@class,'u7-button') and normalize-space()='Confirm']",
                "xpath=//button[.//span[normalize-space()='Confirm']]",
                "xpath=//*[normalize-space()='Confirm']/ancestor-or-self::aside[contains(@class,'u7-button')][1]",
            ]:
                l = page.locator(s).first
                if l.count() > 0 and l.is_visible():
                    l.click()
                    confirm_clicked = True
                    break
            if confirm_clicked:
                break
        except Exception:
            pass
        page.wait_for_timeout(500)

    if not confirm_clicked:
        add_log(f"[U7] Confirm popup tidak muncul buat gambar {idx}/{total}")
        return False

    # Poll sampai thumbnail masuk list (count > baseline) atau timeout
    target = baseline + 1
    step_ms = 500
    elapsed = 0
    while elapsed < timeout_ms:
        page.wait_for_timeout(step_ms)
        elapsed += step_ms
        try:
            cur = page.locator("ul.el-upload-list li.is-success").count()
            if cur >= target:
                add_log(f"[U7] Upload {idx}/{total} OK ({baseline}->{cur}) {elapsed/1000:.1f}s")
                smart_wait(page, 400, 700)
                return True
        except Exception:
            pass

    add_log(f"[U7] Upload {idx}/{total} timeout {timeout_ms//1000}s")
    return False


def _fill_description_u7(page, body_text):
    """Step 9: fill Offer Description textarea (rows=4, maxlength 1000)."""
    add_log("[U7] Isi Offer Description")
    for sel in [
        "xpath=//div[contains(@class,'el-form-item')][.//*[normalize-space()='Offer Description']]"
        "//textarea[contains(@class,'el-textarea__inner')]",
        "xpath=//*[normalize-space()='Offer Description']/following::textarea[1]",
        "textarea.el-textarea__inner[maxlength='1000']",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=5000)
            loc.fill(body_text)
            smart_wait(page, 400, 700)
            return
        except Exception:
            continue
    raise Exception("Description textarea tidak ketemu")


def _label_for_u7_trigger(page, trigger_locator):
    """Derive label dari trigger via el-form-item label terdekat. Element Plus
    render label sbg <div class='el-form-item__label'> (bukan <label> tag).
    Strip '*' required marker. Return str atau None.

    CATATAN: XPath contains(@class,'el-form-item') salah karena ikut match
    'el-form-item__content' (parent langsung dari u7-select, bukan label-holder).
    Pakai word-boundary match: concat(' ',class,' '),' el-form-item ' untuk
    target class exact 'el-form-item'."""
    try:
        label_loc = trigger_locator.locator(
            "xpath=ancestor::div[contains(concat(' ',normalize-space(@class),' '),' el-form-item ')][1]"
            "//*[self::label or contains(@class,'el-form-item__label')]"
        ).first
        if label_loc.count() > 0:
            txt = (label_loc.inner_text(timeout=1000) or "").strip()
            txt = txt.strip("* \t\n").strip()
            return txt if txt else None
    except Exception:
        pass
    return None


# ===================== FORM OPTIONS SCRAPE =====================
def _scrape_form_options_page(page):
    """Scrape semua u7-select dropdown dinamis (game-specific). Skip fixed
    dropdown: Delivery Method, Guaranteed Delivery Time (selalu Manual + 1h).
    Return {label: [options]}."""
    options_map = {}

    # U7 form render lazy setelah Next step. Poll sampai u7-select muncul
    # (max 15s) atau page URL stabil.
    u7_selector = "div.u7-select"  # drop tabindex filter - lebih lenient
    for _ in range(30):  # ~15s max polling
        page.wait_for_timeout(500)
        try:
            count = page.locator(u7_selector).count()
            if count > 0:
                break
        except Exception:
            pass
    smart_wait(page, 1500, 2500)

    total_seen = 0
    try:
        total_seen = page.locator(u7_selector).count()
    except Exception:
        pass
    add_log(f"[U7] Total u7-select di page: {total_seen} (url={page.url[:60]})")

    # Skip labels yg fixed
    skip_labels = {"delivery method", "guaranteed delivery time"}

    # Find dropdown triggers - u7-select pattern
    pending = []  # (label, idx)
    try:
        triggers = page.locator(u7_selector).all()
        for idx, t in enumerate(triggers):
            label = _label_for_u7_trigger(page, t)
            if not label:
                # Log yg ndak ada label biar kelihatan
                add_log(f"[U7] u7-select #{idx}: label not found, skip")
                continue
            if label.lower() in skip_labels:
                continue
            if label not in [p[0] for p in pending]:
                pending.append((label, idx))
    except Exception as e:
        add_log(f"[U7] Gagal enum u7-select: {str(e)[:80]}")

    add_log(f"[U7] Detected {len(pending)} u7-select dynamic: {[p[0] for p in pending]}")

    for label, idx in pending:
        try:
            # Re-resolve trigger by index
            trigger = page.locator(u7_selector).nth(idx)
            if not trigger.is_visible():
                continue
            trigger.click()
            smart_wait(page, 400, 700)

            opts = _collect_u7_options(page)
            if opts:
                options_map[label] = opts
                add_log(f"[U7]    - {label}: {len(opts)} opsi -> {opts[:5]}{'...' if len(opts)>5 else ''}")
            else:
                add_log(f"[U7]    {label}: opsi kosong")

            _close_u7_dropdown(page)
        except Exception as e:
            add_log(f"[U7] Gagal scrape '{label}': {str(e)[:80]}")
            _close_u7_dropdown(page)
            continue

    return options_map


# ===================== CREATE LISTING FULL FLOW =====================
def create_listing(game_name, title, deskripsi, harga, field_mapping, image_paths,
                   raw_image_url=None, image_future=None):
    """Full U7 create flow. Return (ok, err, uploaded_count).

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

            # U7 quirk (sama kayak bot_diskon): jangan pakai networkidle,
            # website polling XHR persisten bikin idle ndak pernah triggered.
            # Pakai domcontentloaded + wait manual lebih reliable.
            page.goto(U7_START_URL, wait_until="domcontentloaded", timeout=30000)
            _set_worker_tab_title(page)
            smart_wait(page, 4000, 8000)

            # Step 1-4: navigasi ke form
            _click_game_service_card(page)
            _select_game_u7(page, game_name)
            _select_category_accounts(page)
            _click_next_step(page)

            # Step 5: Product Name (title dari J)
            _fill_product_name(page, title)

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

            # Step 6-8: Upload images one-by-one dgn confirm popup, max 5.
            # Abort-after-first-fail: kalau upload pertama timeout/exception,
            # page U7 biasanya stuck loading -> lanjut upload 2-5 sia-sia dan
            # boros sampai 5 menit. Short-circuit: gagalkan listing, pindah row.
            to_upload = (image_paths or [])[:U7_MAX_IMAGES]
            if to_upload:
                total = len(to_upload)
                for i, path in enumerate(to_upload, start=1):
                    try:
                        ok = _upload_one_image_u7(page, path, i, total)
                    except Exception as e:
                        add_log(f"[U7] Upload {i}/{total} exception: {str(e)[:80]}")
                        return False, f"Upload {i}/{total} exception: {str(e)[:80]}", uploaded
                    if not ok:
                        return False, f"Upload {i}/{total} timeout, abort listing (U7 stuck)", uploaded
                    uploaded += 1

            # Step 9: Description (line 1 = raw URL no scheme, line 2+ deskripsi)
            if raw_image_url:
                raw_line = re.sub(r"^https?://", "", raw_image_url.strip())
                body_text = f"Full Screenshot Detail: {raw_line}\n{deskripsi or ''}"
            else:
                body_text = deskripsi or ""
            _fill_description_u7(page, body_text)

            # Step 10-12: Isi dynamic dropdowns (u7-select) via AI field_mapping
            try:
                triggers = page.locator("div.u7-select").all()
                for idx, t in enumerate(triggers):
                    label = _label_for_u7_trigger(page, t)
                    if not label:
                        continue
                    if label.lower() in {"delivery method", "guaranteed delivery time"}:
                        continue
                    preferred = (field_mapping or {}).get(label)
                    trigger = page.locator("div.u7-select").nth(idx)
                    # Multi-strategy open (click outer / click placeholder / focus+Enter)
                    if not _open_u7_select(page, trigger):
                        add_log(f"[U7] Dropdown '{label}': gagal buka, skip")
                        continue
                    opts = _collect_u7_options(page)
                    if not opts:
                        add_log(f"[U7] Dropdown '{label}': opsi tidak terdeteksi, skip")
                        _close_u7_dropdown(page)
                        continue
                    pick = preferred if preferred and preferred in opts else opts[0]
                    src = "AI" if preferred and preferred == pick else "first-option"
                    add_log(f"[U7] Isi {label}: {pick} ({src})")
                    if not _click_option_by_text(page, pick):
                        add_log(f"[U7] Klik opsi '{pick}' gagal untuk '{label}'")
                        _close_u7_dropdown(page)
                    smart_wait(page, 300, 500)
            except Exception as e:
                add_log(f"[U7] Gagal fill dynamic dropdown: {str(e)[:80]}")

            # Step 13: Delivery Method = Manual (el-select - fixed value)
            add_log("[U7] Pilih Delivery Method: Manual")
            try:
                trigger = page.locator(
                    "xpath=//div[contains(@class,'el-form-item')][.//*[normalize-space()='Delivery Method']]"
                    "//div[contains(@class,'el-select__wrapper')]"
                ).first
                trigger.click()
                smart_wait(page, 400, 700)
                if not _click_option_by_text(page, "Manual"):
                    return False, "Delivery Method: 'Manual' tidak ketemu", uploaded
            except Exception as e:
                return False, f"Delivery Method: {str(e)[:100]}", uploaded

            # Step 15: Guaranteed Delivery Time = 1 hour
            add_log("[U7] Pilih Delivery Time: 1 hour")
            try:
                trigger = page.locator(
                    "xpath=//div[contains(@class,'el-form-item')][.//*[normalize-space()='Guaranteed Delivery Time']]"
                    "//div[contains(@class,'el-select__wrapper') or contains(@class,'u7-select')]"
                ).first
                trigger.click()
                smart_wait(page, 400, 700)
                if not _click_option_by_text(page, "1 hour"):
                    return False, "Delivery Time: '1 hour' tidak ketemu", uploaded
            except Exception as e:
                return False, f"Delivery Time: {str(e)[:100]}", uploaded

            # Step 16: Selling Price (kolom H)
            price_clean = re.sub(r"[^0-9.,]", "", str(harga)).replace(",", ".")
            if price_clean.count(".") > 1:
                parts = price_clean.split(".")
                price_clean = "".join(parts[:-1]) + "." + parts[-1]
            add_log(f"[U7] Isi Selling Price: ${price_clean}")
            try:
                price_input = page.locator(
                    "xpath=//div[contains(@class,'el-form-item')][.//*[normalize-space()='Selling Price']]"
                    "//input[contains(@class,'el-input__inner')]"
                ).first
                price_input.wait_for(state="visible", timeout=5000)
                price_input.fill(price_clean)
                smart_wait(page, 400, 700)
            except Exception as e:
                return False, f"Selling Price: {str(e)[:100]}", uploaded

            # Step 17-19: Centang 3 policy checkbox
            add_log("[U7] Centang 3x policy checkbox")
            for policy_num in ("1", "2", "3"):
                try:
                    cb = page.locator(
                        f"xpath=//div[contains(@class,'policy') and @data-policy='{policy_num}']"
                        f"//div[contains(@class,'policy-select')]"
                    ).first
                    cb.wait_for(state="visible", timeout=3000)
                    cb.click(force=True)
                    smart_wait(page, 200, 400)
                except Exception as e:
                    return False, f"Policy {policy_num}: {str(e)[:100]}", uploaded

            # Step 20: Submit
            add_log("[U7] Klik Submit")
            try:
                submit = page.locator(
                    "xpath=//aside[contains(@class,'u7-button') and normalize-space()='Submit']"
                ).first
                submit.wait_for(state="visible", timeout=5000)
                start_url = page.url
                submit.click()
            except Exception as e:
                return False, f"Submit: {str(e)[:100]}", uploaded

            # Sukses detection: redirect ke /member/offers (exact atau prefix match)
            redirected = False
            for _ in range(120):  # 60s max
                page.wait_for_timeout(500)
                try:
                    cur = page.url
                    if cur != start_url and "/member/offers" in cur and "/create" not in cur:
                        redirected = True
                        break
                except Exception:
                    pass

            if redirected:
                add_log(f"[U7] Redirect ke: {page.url} -> sukses")
                smart_wait(page, 800, 1500)
                return True, None, uploaded

            # Fallback error msg
            try:
                err_msgs = []
                for sel in [".el-message--error", ".el-notification", "[role='alert']:visible"]:
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
                    add_log(f"[U7] Form error: {combined[:200]}")
                    return False, f"Form error: {combined[:120]}", uploaded
            except Exception:
                pass
            return False, "Submit: tidak redirect dalam 60s", uploaded

        except Exception as e:
            pesan = str(e)
            if "Timeout" in pesan:
                err_msg = "Waktu habis, elemen tidak ditemukan"
            elif "net::" in pesan:
                err_msg = "Gagal membuka halaman, cek koneksi"
            else:
                err_msg = f"Error: {pesan[:100]}"
            add_log(f"[U7] Gagal: {err_msg}")
            return False, err_msg, uploaded
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


# ===================== ENTRY POINTS =====================
def scrape_form_options(game_name):
    """Buka /member/offers/create-offer, navigasi sampai form, scrape u7-select."""
    add_log("[U7] Scrape form options dari U7 (pertama kali)...")
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

            # U7 quirk (sama kayak bot_diskon): jangan pakai networkidle,
            # website polling XHR persisten bikin idle ndak pernah triggered.
            # Pakai domcontentloaded + wait manual lebih reliable.
            page.goto(U7_START_URL, wait_until="domcontentloaded", timeout=30000)
            _set_worker_tab_title(page)
            smart_wait(page, 4000, 8000)

            _click_game_service_card(page)
            _select_game_u7(page, game_name)
            _select_category_accounts(page)
            _click_next_step(page)
            return _scrape_form_options_page(page)

        except Exception as e:
            add_log(f"[U7] Gagal scrape form options: {str(e)[:100]}")
            return None
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


def cache_looks_bogus(cache_dict):
    """U7: default False."""
    return False


def run(sheet, baris_nomor, worker_id, *, game_name, description, title, harga,
        field_mapping, image_paths=None, image_urls=None,
        raw_image_url=None, is_imgur=False, image_future=None):
    """Adapter entry dipanggil orchestrator. Return (ok, k_line)."""
    _worker_local.worker_id = f"{worker_id}-U7"

    ok, err, uploaded = create_listing(
        game_name, title, description or "", harga,
        field_mapping or {},
        (image_paths or [])[:U7_MAX_IMAGES] if image_paths else None,
        raw_image_url=raw_image_url,
        image_future=image_future,
    )
    ts = datetime.now().strftime("%d %b, %y | %H:%M")
    if ok:
        return True, f"✅ U7 | {uploaded} images uploaded | {ts}"
    return False, f"❌ U7 | {(err or 'unknown')[:80]}"
