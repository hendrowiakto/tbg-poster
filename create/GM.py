"""create/GM.py - GameMarket (GM) marketplace adapter.

Entry points dipakai orchestrator (bot_create.py):
- scrape_form_options(game_name_gm) -> dict | {} | None
- create_listing(game_name_gm, title, deskripsi, harga, field_mapping, image_paths)
    -> (ok: bool, err: str | None)

Helper internal (_set_worker_tab_title, _select_game_dropdown, dll.) tetap
module-level supaya mudah di-monkeypatch saat debug. Tidak ada import dari
bot_create - semua runtime dep via create._shared.

NOTE: Selector XPath/CSS & timings di sini TIDAK diubah relatif ke
implementasi asli di bot_create.py. Pindah-only refactor.
"""

import re
from datetime import datetime
from playwright.sync_api import sync_playwright

from create._shared import (
    _worker_local,
    _log as add_log,
    _get_chrome_debug_port,
    xpath_literal as _xpath_literal,
    smart_wait,
    get_or_create_context,
)


# ===================== KONSTANTA =====================
GM_CREATE_URL = "https://gamemarket.gg/dashboard/create-listing"

# Adapter protocol (dibaca orchestrator via importlib):
MARKET_CODE     = "GM"
HARGA_COL       = 8                              # H
CACHE_SENTINEL  = "[tidak ditemukan options]"


# ===================== TAB TITLE =====================
def _set_worker_tab_title(page):
    """Inject prefix 'Worker N | ' ke document.title tab GameMarket. Tidak replace,
    hanya prepend. MutationObserver re-apply kalau React overwrite. Silent fail."""
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


# ===================== GAME & TYPE SELECTION =====================
def _select_game_dropdown(page, game_name_gm):
    """Pilih Game: click input Market, ketik nama, klik opsi autocomplete.
    Layout baru GM (2026-04): Tailwind combobox custom, input placeholder='Select game'
    di bawah label <p>Market*</p>. Input tsb merangkap search + trigger dropdown."""
    add_log(f"[GM] Pilih Game: {game_name_gm}")

    # Input Market. Layout baru: placeholder='Select game' (case-sensitive).
    # Layout lama: 'Please Select Game'. Pertahanin fallback buat backward-compat.
    game_input = None
    for sel in [
        "input[placeholder='Select game']",
        "xpath=//p[starts-with(normalize-space(),'Market')]/following::input[@placeholder='Select game'][1]",
        "xpath=//main//input[@placeholder='Select game']",
        "input[placeholder='Please Select Game']",
        "xpath=//main//input[@placeholder='Please Select Game']",
        "xpath=//*[normalize-space()='Game' or normalize-space()='Game *']/following::input[@placeholder='Please Select Game'][1]",
        "xpath=//label[starts-with(normalize-space(),'Game')]/following::input[@type='text'][1]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=3000)
            game_input = loc
            break
        except Exception:
            continue
    if game_input is None:
        raise Exception("Game input tidak ditemukan")

    game_input.click()
    smart_wait(page, 150, 300)

    # Layout lama kadang buka modal dgn search box terpisah. Layout baru ketik
    # langsung di input-nya. Cek search box separate sebentar aja (150ms each),
    # kalau ndak ada fokus balik ke game_input.
    search_box = None
    for sel in [
        "input[placeholder='Type to search...']",
        "input[placeholder*='ype to search' i]",
        "input[placeholder*='earch' i]:focus",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=150)
            search_box = loc
            break
        except Exception:
            continue

    target = search_box if search_box is not None else game_input
    try:
        target.click()
    except Exception:
        pass
    # Clear + type cepat (30ms/char, "Clash Royale" = ~0.36s)
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    page.keyboard.type(game_name_gm, delay=30)
    smart_wait(page, 300, 500)

    # Klik option match di popup.
    gm_lit       = _xpath_literal(game_name_gm)
    gm_lower_lit = _xpath_literal(game_name_gm.lower())
    upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    lower = "abcdefghijklmnopqrstuvwxyz"
    # Layout baru: option biasanya <div class="cursor-pointer ...">  tanpa role=option.
    # Layout lama: role=option di portal. Coba urut dari spesifik -> fuzzy.
    selectors = [
        f"xpath=//button[@data-value and normalize-space()={gm_lit}]",
        f"xpath=//button[normalize-space()={gm_lit} and contains(@class,'hover:bg')]",
        f"xpath=//button[normalize-space()={gm_lit}]",
        f"xpath=//*[@role='option' and normalize-space()={gm_lit}]",
        f"xpath=//li[normalize-space()={gm_lit}]",
        f"xpath=//div[contains(@class,'cursor-pointer')][normalize-space()={gm_lit}]",
        f"xpath=//div[contains(@class,'hover:bg')][normalize-space()={gm_lit}]",
        f"xpath=//button[@data-value][contains(translate(normalize-space(.),'{upper}','{lower}'),{gm_lower_lit})]",
        f"xpath=//div[contains(@class,'cursor-pointer')][contains(translate(normalize-space(.),'{upper}','{lower}'),{gm_lower_lit})]",
        f"xpath=//*[normalize-space(text())={gm_lit}]",
        f"xpath=//*[@role='option'][contains(translate(normalize-space(.),'{upper}','{lower}'),{gm_lower_lit})]",
    ]
    if ":" in game_name_gm:
        suffix = game_name_gm.split(":", 1)[1].strip().lower()
        if suffix:
            suffix_lit = _xpath_literal(suffix)
            selectors.append(
                f"xpath=//button[@data-value][contains(translate(normalize-space(.),'{upper}','{lower}'),{suffix_lit})]"
            )
            selectors.append(
                f"xpath=//*[@role='option'][contains(translate(normalize-space(.),'{upper}','{lower}'),{suffix_lit})]"
            )
            selectors.append(
                f"xpath=//div[contains(@class,'cursor-pointer')][contains(translate(normalize-space(.),'{upper}','{lower}'),{suffix_lit})]"
            )
    option = None
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=1500)
            option = loc
            break
        except Exception:
            continue
    if option is None:
        raise Exception(f"Opsi game '{game_name_gm}' tidak muncul di dropdown")
    option.click()
    smart_wait(page, 400, 800)


def _select_type_accounts(page):
    """Pilih Type = Accounts. Layout baru GM (2026-04): input Tailwind custom
    placeholder='Select type' yg initially disabled sampe Game dipilih.
    Layout lama: <button role='combobox'>. Keep backward-compat."""
    add_log("[GM] Pilih Type: Accounts")

    # Tunggu Type input jadi enabled (after game dipilih form bakal enable Type).
    type_input = None
    for _ in range(30):  # max 6s
        for sel in [
            "input[placeholder='Select type']:not([disabled])",
            "xpath=//p[starts-with(normalize-space(),'Type')]/following::input[@placeholder='Select type' and not(@disabled)][1]",
        ]:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=200)
                type_input = loc
                break
            except Exception:
                continue
        if type_input is not None:
            break
        page.wait_for_timeout(200)

    if type_input is not None:
        type_input.click()
        smart_wait(page, 400, 800)
    else:
        # Fallback layout lama: button role=combobox
        dropdown = None
        for sel in [
            "button[role='combobox']:has(span:has-text('Select Type'))",
            "xpath=//button[@role='combobox' and .//span[normalize-space()='Select Type']]",
            "xpath=//label[normalize-space()='Type']/following::button[@role='combobox'][1]",
            "xpath=//*[normalize-space()='Type']/following::button[@role='combobox'][1]",
        ]:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=3000)
                dropdown = loc
                break
            except Exception:
                continue
        if dropdown is None:
            raise Exception("Type input/combobox tidak ditemukan")
        dropdown.click()
        smart_wait(page, 800, 1500)

    # Options muncul di dropdown portal. Layout baru (2026-04) pakai
    # <button data-value="..."> dgn text 'Accounts'. Layout older pakai
    # div.cursor-pointer atau role=option.
    option = None
    for sel in [
        "xpath=//button[@data-value and normalize-space()='Accounts']",
        "xpath=//button[normalize-space()='Accounts' and contains(@class,'hover:bg')]",
        "xpath=//button[normalize-space()='Accounts']",
        "xpath=//*[@role='option' and normalize-space()='Accounts']",
        "[role='option']:has-text('Accounts')",
        "xpath=//div[@role='option'][normalize-space()='Accounts']",
        "xpath=//div[contains(@class,'cursor-pointer')][normalize-space()='Accounts']",
        "xpath=//li[normalize-space()='Accounts']",
        "xpath=//div[contains(@class,'hover:bg')][normalize-space()='Accounts']",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=3000)
            option = loc
            break
        except Exception:
            continue
    if option is None:
        raise Exception("Opsi 'Accounts' tidak muncul di dropdown Type")
    option.click()
    smart_wait(page, 1000, 3000)


# ===================== FORM OPTIONS SCRAPER =====================
def _find_dropdown_trigger(page, label):
    """Resolve trigger dropdown by label. Try layout baru (input[placeholder=
    'Select {label}']) dulu, fallback layout lama (button[role='combobox']).
    Return (locator, kind) where kind in {'input','button'} atau (None, None)."""
    # Layout baru: input placeholder exact match
    try:
        loc = page.locator(f"input[placeholder='Select {label}']").first
        if loc.is_visible(timeout=500):
            return loc, "input"
    except Exception:
        pass
    # Layout lama: button role=combobox
    try:
        loc = page.locator(
            f"button[role='combobox']:has(span:text-is('Select {label}'))"
        ).first
        if loc.is_visible(timeout=500):
            return loc, "button"
    except Exception:
        pass
    return None, None


def _dropdown_options_visible(page):
    """True kalau masih ada opsi dropdown visible di DOM (button[data-value]
    atau [role='option'])."""
    try:
        n = page.locator("button[data-value]:visible").count()
        if n > 0:
            return True
    except Exception:
        pass
    try:
        n = page.locator("[role='option']:visible").count()
        if n > 0:
            return True
    except Exception:
        pass
    return False


def _close_dropdown(page, kind):
    """Tutup dropdown yg lagi kebuka. Multi-strategy + verifikasi:
    1. Escape (cukup buat layout lama button[role=combobox]).
    2. Mouse click di koordinat absolute (5,5) — fire click-away handler tanpa
       kena elemen interactive yg lain.
    3. JS blur activeElement + dispatch pointerdown/mousedown ke documentElement.
    Verifikasi lewat _dropdown_options_visible — kalau masih open, coba strategi
    berikutnya. Max 3 attempt."""
    strategies = []
    if kind == "button":
        # Layout lama: Escape dulu, fallback click-outside
        strategies = ["escape", "mouse_corner", "js_dispatch"]
    else:
        # Layout baru: Escape ndak ngerti, click-outside jurus utama
        strategies = ["mouse_corner", "js_dispatch", "escape"]

    for strat in strategies:
        try:
            if strat == "escape":
                page.keyboard.press("Escape")
            elif strat == "mouse_corner":
                # Klik di area atas-kiri viewport. Di (5,5) biasanya masuk
                # browser chrome margin, pakai (100, 100) yg lebih aman di
                # dalam halaman tapi di luar form/dropdown.
                page.mouse.click(100, 100)
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
        page.wait_for_timeout(200)
        if not _dropdown_options_visible(page):
            return


def _collect_dropdown_options(page):
    """Ambil semua option visible di portal/popup dropdown. Layout baru
    pakai <button data-value>, layout lama pakai [role='option']."""
    texts = []
    for sel in [
        "button[data-value]:visible",
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


def _scrape_form_options_page(page):
    """Scrape semua dropdown dinamis SETELAH Game+Type dipilih. Support 2 layout:
    - Baru: <input placeholder='Select X'> + <button data-value> options
    - Lama: <button role='combobox'> dgn <span>Select X</span> + [role='option']
    Return dict {field_label: [options]}."""
    options_map = {}

    # Tunggu field dinamis render (bergantung pada Type=Accounts)
    page.wait_for_timeout(2500)

    pending = []

    # Layout baru: input[placeholder^='Select '] (exclude 'Select game'/'Select type'
    # yg handled by dedicated function, dan 'Select Type' case yg sudah terisi).
    try:
        inputs = page.locator("input[placeholder^='Select ']").all()
        for inp in inputs:
            try:
                ph = (inp.get_attribute("placeholder") or "").strip()
            except Exception:
                continue
            if not ph.lower().startswith("select "):
                continue
            label = ph[7:].strip()
            if not label:
                continue
            lower_label = label.lower()
            if lower_label in ("game", "type"):
                continue
            if label not in pending:
                pending.append(label)
    except Exception:
        pass

    # Layout lama: button role=combobox dgn span 'Select X'
    try:
        comboboxes = page.locator("button[role='combobox']").all()
        for btn in comboboxes:
            try:
                span = btn.locator("span").first
                txt = (span.inner_text(timeout=1500) or "").strip()
            except Exception:
                continue
            if not txt.lower().startswith("select "):
                continue
            label = txt[7:].strip()
            if not label:
                continue
            lower_label = label.lower()
            if lower_label in ("game", "type"):
                continue
            if label not in pending:
                pending.append(label)
    except Exception:
        pass

    add_log(f"[GM] Detected {len(pending)} dynamic dropdown(s): {pending}")

    for label in pending:
        try:
            trigger, kind = _find_dropdown_trigger(page, label)
            if trigger is None:
                add_log(f"[GM]    {label}: trigger tidak visible, skip")
                continue
            trigger.click()
            page.wait_for_timeout(350)

            opt_texts = _collect_dropdown_options(page)
            if opt_texts:
                options_map[label] = opt_texts
                add_log(f"[GM]    - {label}: {len(opt_texts)} opsi -> {opt_texts[:5]}{'...' if len(opt_texts)>5 else ''}")
            else:
                add_log(f"[GM]    {label}: opsi tidak terdeteksi")

            # Tutup dropdown. Layout lama Escape cukup. Layout baru (input
            # custom Tailwind): click input ndak toggle (re-focus doang),
            # Escape ndak di-handle juga. Jurus yg reliable: blur active
            # element + click label statis "Listing information".
            _close_dropdown(page, kind)
        except Exception as e:
            add_log(f"[GM] Gagal scrape '{label}': {str(e)[:80]}")
            _close_dropdown(page, "input")
            continue

    return options_map


def _select_dropdown_by_label(page, label_text, option_value):
    """Klik trigger 'Select {label_text}' lalu pilih option. Support layout baru
    (input + button[data-value]) dan lama (button[role=combobox] + role=option)."""
    add_log(f"[GM] Isi {label_text}: {option_value}")
    try:
        # Resolve trigger - poll sedikit karena field dinamis render after parent.
        trigger = None
        for _ in range(20):  # 4s max
            trigger, _kind = _find_dropdown_trigger(page, label_text)
            if trigger is not None:
                break
            page.wait_for_timeout(200)
        if trigger is None:
            raise Exception(f"trigger 'Select {label_text}' tidak ditemukan")
        trigger.click()
        smart_wait(page, 250, 450)

        # Opsi: layout baru button[data-value], layout lama role=option.
        opt_lit = _xpath_literal(option_value)
        opt_css_safe = option_value.replace("\\", "\\\\").replace("'", "\\'")
        option = None
        for sel in [
            f"xpath=//button[@data-value and normalize-space()={opt_lit}]",
            f"xpath=//button[normalize-space()={opt_lit} and contains(@class,'hover:bg')]",
            f"xpath=//*[@role='option' and normalize-space()={opt_lit}]",
            f"[role='option']:has-text('{opt_css_safe}')",
            f"xpath=//button[normalize-space()={opt_lit}]",
            f"xpath=//*[normalize-space(text())={opt_lit}]",
        ]:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=500)
                option = loc
                break
            except Exception:
                continue
        if option is None:
            raise Exception(f"opsi '{option_value}' tidak ditemukan")
        option.click()
        smart_wait(page, 250, 500)
    except Exception as e:
        add_log(f"[GM] Gagal isi '{label_text}' = '{option_value}': {str(e)[:80]}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass


# ===================== PUBLIC ENTRIES =====================
GM_ORIGIN = "https://gamemarket.gg"


def _extract_listing_url(page):
    """Extract absolute URL listingan yg baru dipublish. Source priority:
    1. page.url kalau cocok pola /market/.../p/<slug>
    2. Anchor 'View listing' di toast sukses (href relatif, prepend origin)
    Return str URL atau None."""
    # Sumber 1: page.url (kalau redirect terjadi)
    try:
        cur = page.url or ""
        if "/market/" in cur and "/p/" in cur:
            return cur
    except Exception:
        pass
    # Sumber 2: toast anchor. Poll sedikit karena toast render async.
    for _ in range(10):  # max 2s
        for sel in [
            "a[href*='/market/'][href*='/p/']",
            "xpath=//a[contains(@href,'/market/') and contains(@href,'/p/')]",
        ]:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    href = loc.get_attribute("href", timeout=500) or ""
                    if href:
                        if href.startswith("http"):
                            return href
                        if href.startswith("/"):
                            return GM_ORIGIN + href
                        return f"{GM_ORIGIN}/{href}"
            except Exception:
                continue
        try:
            page.wait_for_timeout(200)
        except Exception:
            break
    return None


def create_listing(game_name_gm, title, deskripsi, harga, field_mapping, image_paths):
    """Full flow isi form & submit. Return (berhasil, error_message)."""
    with sync_playwright() as p:
        page = None
        try:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{_get_chrome_debug_port()}", timeout=10000)
            context = get_or_create_context(browser)
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()

            add_log("[GM] Membuka halaman create listing...")
            page.goto(GM_CREATE_URL, wait_until="networkidle", timeout=30000)
            _set_worker_tab_title(page)
            smart_wait(page, 3000, 5000)

            # 1. Game
            _select_game_dropdown(page, game_name_gm)

            # 2. Type = Accounts
            _select_type_accounts(page)

            # 3. Dynamic fields dari Gemini
            for field_label, value in field_mapping.items():
                if value is None:
                    continue
                _select_dropdown_by_label(page, field_label, value)
                smart_wait(page, 500, 1000)

            # 4. Title
            add_log("[GM] Isi Title...")
            title_input = page.locator(
                "input[placeholder*='itle' i], input#title, input[name='title']"
            ).first
            title_input.wait_for(state="visible", timeout=10000)
            title_input.fill(title)
            smart_wait(page, 500, 1000)

            # 5. Description
            add_log("[GM] Isi Description...")
            desc_input = page.locator(
                "textarea[placeholder*='escribe' i], textarea[name='description']"
            ).first
            desc_input.wait_for(state="visible", timeout=10000)
            desc_input.fill(deskripsi)
            smart_wait(page, 500, 1000)

            # 6. Duration = 90 days (radix radio button)
            add_log("[GM] Pilih Duration: 90 days")
            try:
                duration_btn = None
                for sel in [
                    "button[role='radio'][value='90']",
                    "xpath=//button[@role='radio' and @value='90']",
                    "xpath=//label[normalize-space()='90 days']/preceding-sibling::button[@role='radio']",
                    "xpath=//label[normalize-space()='90 days']/following-sibling::button[@role='radio']",
                ]:
                    try:
                        loc = page.locator(sel).first
                        loc.wait_for(state="visible", timeout=2500)
                        duration_btn = loc
                        break
                    except Exception:
                        continue
                if duration_btn is None:
                    raise Exception("tombol radio 90 days tidak ditemukan")
                duration_btn.click()
                smart_wait(page, 500, 1000)
            except Exception as e:
                add_log(f"[GM] Gagal pilih Duration 90: {str(e)[:80]}")

            # 7. Delivery = In-Chat (radix checkbox button)
            add_log("[GM] Pilih Delivery: In-Chat Delivery")
            try:
                delivery_cb = None
                for sel in [
                    "button[role='checkbox'][value='In-Chat Delivery']",
                    "button[role='checkbox'][id='In-Chat Delivery']",
                    "xpath=//button[@role='checkbox' and @value='In-Chat Delivery']",
                    "xpath=//label[normalize-space()='In-Chat Delivery']/preceding-sibling::button[@role='checkbox']",
                    "xpath=//label[normalize-space()='In-Chat Delivery']/following-sibling::button[@role='checkbox']",
                ]:
                    try:
                        loc = page.locator(sel).first
                        loc.wait_for(state="visible", timeout=2500)
                        delivery_cb = loc
                        break
                    except Exception:
                        continue
                if delivery_cb is None:
                    raise Exception("tombol checkbox In-Chat Delivery tidak ditemukan")
                state = delivery_cb.get_attribute("data-state") or ""
                if state != "checked":
                    delivery_cb.click()
                smart_wait(page, 500, 1000)
            except Exception as e:
                add_log(f"[GM] Gagal centang In-Chat: {str(e)[:80]}")

            # 8. Price - strip simbol mata uang, input type=number hanya terima angka
            price_raw = str(harga)
            price_clean = re.sub(r'[^0-9.,]', '', price_raw).replace(',', '.')
            # Handle multi-dot (1.234.56 -> 1234.56) -> keep last dot as decimal
            if price_clean.count('.') > 1:
                parts = price_clean.split('.')
                price_clean = ''.join(parts[:-1]) + '.' + parts[-1]
            add_log(f"[GM] Isi Price: ${price_clean}")
            price_input = page.locator(
                "input[placeholder*='rice' i], input[name='price'], input[type='number']"
            ).first
            price_input.wait_for(state="visible", timeout=10000)
            price_input.fill(price_clean)
            smart_wait(page, 500, 1000)

            # 9. Stock = 1
            try:
                stock_input = page.locator("input[name='stock'], input[placeholder*='tock' i]").first
                if stock_input.count() > 0 and stock_input.is_visible():
                    stock_input.fill("1")
                    smart_wait(page, 300, 600)
            except Exception:
                pass

            # 10. Min Order = 1
            try:
                min_input = page.locator(
                    "input[name*='min' i], input[placeholder*='in' i][placeholder*='rder' i]"
                ).first
                if min_input.count() > 0 and min_input.is_visible():
                    min_input.fill("1")
                    smart_wait(page, 300, 600)
            except Exception:
                pass

            # 11. Upload images - HARDENED: retry + verifikasi preview muncul.
            # Kalau image_paths ada tapi upload gagal total -> abort (jangan publish
            # listing tanpa gambar). Verifikasi: blob:-URL preview count >= expected.
            if image_paths:
                expected = len(image_paths)
                upload_ok = False
                last_err = ""
                for attempt in range(1, 4):  # 3x retry
                    add_log(f"[GM] Upload {expected} gambar (attempt {attempt}/3)...")
                    try:
                        file_input = page.locator("input[type='file']").first
                        file_input.set_input_files(image_paths)
                    except Exception as e:
                        last_err = str(e)[:80]
                        add_log(f"[GM] set_input_files gagal: {last_err}")
                        smart_wait(page, 1500, 2500)
                        continue

                    # Poll max 60s: tunggu preview blob:-images muncul sejumlah expected.
                    # Upload bisa lama karena client-side resize + server ack.
                    preview_loc = page.locator("img[src^='blob:'], img[src^='data:image']")
                    got = 0
                    deadline_ms = 60000
                    step_ms = 500
                    elapsed = 0
                    while elapsed < deadline_ms:
                        try:
                            got = preview_loc.count()
                        except Exception:
                            got = 0
                        if got >= expected:
                            break
                        page.wait_for_timeout(step_ms)
                        elapsed += step_ms

                    if got >= expected:
                        upload_ok = True
                        add_log(f"[GM] Upload sukses - {got}/{expected} preview terverifikasi")
                        break

                    last_err = f"preview {got}/{expected} setelah {elapsed//1000}s"
                    add_log(f"[GM] Upload belum komplit ({last_err}), retry...")
                    # Reset file input sebelum retry (kosongkan supaya fresh upload).
                    try:
                        file_input.set_input_files([])
                    except Exception:
                        pass
                    smart_wait(page, 1500, 3000)

                if not upload_ok:
                    err = f"Upload gambar gagal setelah 3x retry ({last_err})"
                    add_log(f"[GM] {err} - abort publish")
                    return False, err, None

            # 12. Terms of Service - GameMarket auto-centang, skip.

            # 13. Publish Listing
            add_log("[GM] Klik Publish Listing...")
            publish_btn = page.locator("button:has-text('Publish Listing')").first
            publish_btn.wait_for(state="visible", timeout=10000)
            start_url = page.url
            publish_btn.click()

            # 14. Deteksi sukses: (a) redirect URL, atau (b) title input di-clear oleh backend.
            # Poll max 120s (2 menit) - khusus tahap publish karena GM proses upload
            # gambar + server-side validation bisa lama saat peak.
            title_loc = page.locator(
                "input[placeholder*='itle' i], input#title, input[name='title']"
            ).first
            redirected = False
            cleared = False
            for _ in range(240):
                page.wait_for_timeout(500)
                try:
                    cur = page.url
                    if cur != start_url and "/create-listing" not in cur:
                        redirected = True
                        break
                except Exception:
                    pass
                try:
                    cur_title = (title_loc.input_value(timeout=500) or "").strip()
                    if cur_title == "":
                        cleared = True
                        break
                except Exception:
                    pass

            if redirected:
                add_log(f"[GM] Redirect ke: {page.url} -> sukses")
                smart_wait(page, 1000, 2000)
                return True, None, _extract_listing_url(page)
            if cleared:
                add_log("[GM] Form di-reset (title kosong) -> sukses")
                smart_wait(page, 1000, 2000)
                return True, None, _extract_listing_url(page)

            # Cek toast sukses
            try:
                success_toast = page.locator(
                    "xpath=//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'success')"
                    " or contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'published')]"
                )
                if success_toast.count() > 0:
                    return True, None, _extract_listing_url(page)
            except Exception:
                pass

            try:
                # Kumpulkan SEMUA pesan error di form, FILTER asterisk/teks required biasa
                err_selectors = [
                    "[role='alert']",
                    ".text-red-500", ".text-red-600", ".text-red-400",
                    "[class*='text-red']",
                    ".error", ".toast-error",
                    "p.text-red", "span.text-red",
                ]
                all_msgs = []
                noise = {"*", "required", "please fill", ""}
                for sel in err_selectors:
                    try:
                        locs = page.locator(sel).all()
                        for l in locs:
                            try:
                                if not l.is_visible():
                                    continue
                                t = (l.inner_text(timeout=1000) or "").strip()
                                if not t or len(t) < 5 or len(t) > 200:
                                    continue
                                if t.strip() in noise or t.strip() == "*":
                                    continue
                                if t not in all_msgs:
                                    all_msgs.append(t)
                            except Exception:
                                continue
                    except Exception:
                        continue
                if all_msgs:
                    combined = " | ".join(all_msgs[:5])
                    add_log(f"[GM] Form error detail: {combined[:300]}")
                    return False, f"Form error: {combined[:150]}", None
                return False, "Submit tidak redirect, status tidak jelas", None
            except Exception as e:
                return False, f"Submit tidak redirect ({str(e)[:60]})", None

        except Exception as e:
            pesan = str(e)
            if "Timeout" in pesan:
                indo_error = "Waktu habis, elemen tidak ditemukan"
            elif "net::" in pesan:
                indo_error = "Gagal membuka halaman, cek koneksi"
            else:
                indo_error = f"Error: {pesan[:100]}"
            add_log(f"[GM] Gagal: {indo_error}")
            return False, indo_error, None

        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


def scrape_form_options(game_name_gm):
    """Buka /create-listing, pilih Game + Type=Accounts, scrape form options.
    Return:
      dict non-empty = scrape sukses dengan field
      {} = scrape sukses tapi game tanpa dynamic form (caller tulis sentinel ke O45)
      None = scrape fail (exception/timeout) - jangan cache, retry next cycle
    """
    add_log("[GM] Scrape form options dari GM (pertama kali)...")
    with sync_playwright() as p:
        page = None
        try:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{_get_chrome_debug_port()}", timeout=10000)
            context = get_or_create_context(browser)
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()

            page.goto(GM_CREATE_URL, wait_until="networkidle", timeout=30000)
            _set_worker_tab_title(page)
            smart_wait(page, 3000, 5000)

            _select_game_dropdown(page, game_name_gm)
            _select_type_accounts(page)

            return _scrape_form_options_page(page)

        except Exception as e:
            add_log(f"[GM] Gagal scrape form options: {str(e)[:100]}")
            return None
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


# ===================== ADAPTER ENTRY =====================
def cache_looks_bogus(cache_dict):
    """GM tidak punya bogus-cache invalidation. Default False."""
    return False


def run(sheet, baris_nomor, worker_id, *, game_name, description, title, harga,
        field_mapping, image_paths, image_urls=None, raw_image_url=None, is_imgur=False):
    """Adapter entry dipanggil orchestrator. Return (ok: bool, k_line: str)."""
    _worker_local.worker_id = f"{worker_id}-GM"

    if not image_paths:
        return False, "❌ GM | Gambar tidak bisa di download"

    ok, err, listing_url = create_listing(game_name, title, description, harga,
                                          field_mapping or {}, image_paths)
    ts = datetime.now().strftime("%d %b, %y | %H:%M")
    if ok:
        base = f"✅ GM | {len(image_paths)} images uploaded | {ts}"
        if listing_url:
            return True, f"{base} | {listing_url}"
        return True, base
    return False, f"❌ GM | {(err or 'unknown')[:80]}"
