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
    """Pilih Game: input text biasa, ketik nama, klik opsi autocomplete."""
    add_log(f"[GM] Pilih Game: {game_name_gm}")

    # Game field: <input placeholder="Please Select Game"> di form (BUKAN header search).
    game_input = None
    for sel in [
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
    smart_wait(page, 300, 600)

    # Setelah click, dropdown terbuka & focus pindah ke search input.
    search_box = None
    for sel in [
        "input[placeholder='Type to search...']",
        "input[placeholder*='ype to search' i]",
        "input[placeholder*='earch' i]:focus",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=800)
            search_box = loc
            break
        except Exception:
            continue

    if search_box is not None:
        try:
            search_box.click()
        except Exception:
            pass
    # Clear + type cepat (30ms/char, "Clash Royale" = ~0.36s)
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    page.keyboard.type(game_name_gm, delay=30)
    smart_wait(page, 600, 1000)

    # Klik option match di popup (match FULL name, bukan search_query)
    gm_lit       = _xpath_literal(game_name_gm)
    gm_lower_lit = _xpath_literal(game_name_gm.lower())
    upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    lower = "abcdefghijklmnopqrstuvwxyz"
    selectors = [
        f"xpath=//*[@role='option' and normalize-space()={gm_lit}]",
        f"xpath=//li[normalize-space()={gm_lit}]",
        f"xpath=//div[normalize-space()={gm_lit} and (contains(@class,'cursor-pointer') or contains(@class,'hover'))]",
        f"xpath=//*[normalize-space(text())={gm_lit}]",
        f"xpath=//*[@role='option'][contains(translate(normalize-space(.),'{upper}','{lower}'),{gm_lower_lit})]",
    ]
    # Fallback fuzzy: kalau nama ada ':', match option yg mengandung suffix (bagian setelah ':')
    # Contoh: sheet 'Mobile Legend: Bang Bang' vs web 'Mobile Legends: Bang Bang' -> match via 'bang bang'
    if ":" in game_name_gm:
        suffix = game_name_gm.split(":", 1)[1].strip().lower()
        if suffix:
            suffix_lit = _xpath_literal(suffix)
            selectors.append(
                f"xpath=//*[@role='option'][contains(translate(normalize-space(.),'{upper}','{lower}'),{suffix_lit})]"
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
    """Pilih Type = Accounts. Type pakai radix combobox button."""
    add_log("[GM] Pilih Type: Accounts")

    # Type: <button role='combobox'> dengan placeholder "Select Type"
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
        raise Exception("Type combobox tidak ditemukan")

    dropdown.click()
    smart_wait(page, 800, 1500)

    # Options muncul di radix portal (luar DOM combobox). Cari by role=option.
    option = None
    for sel in [
        "xpath=//*[@role='option' and normalize-space()='Accounts']",
        "[role='option']:has-text('Accounts')",
        "xpath=//div[@role='option'][normalize-space()='Accounts']",
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
def _scrape_form_options_page(page):
    """Scrape semua dropdown dinamis SETELAH Game+Type dipilih.
    Label diderive dari placeholder span 'Select X' di dalam button combobox.
    Return dict {field_label: [options]}."""
    options_map = {}

    # Tunggu field dinamis render (bergantung pada Type=Accounts)
    page.wait_for_timeout(2500)

    # Kumpulkan semua dynamic combobox button - deteksi via placeholder "Select X"
    comboboxes = page.locator("button[role='combobox']").all()
    pending = []  # list of (label, button_locator_selector)

    for idx, btn in enumerate(comboboxes):
        try:
            span = btn.locator("span").first
            txt = (span.inner_text(timeout=1500) or "").strip()
        except Exception:
            continue
        if not txt.lower().startswith("select "):
            # Artinya sudah terisi atau bukan pattern 'Select X' -> skip (termasuk Type yg sudah 'Accounts')
            continue
        label = txt[7:].strip()
        if not label or label.lower() == "type":
            continue
        pending.append(label)

    add_log(f"[GM] Detected {len(pending)} dynamic dropdown(s): {pending}")

    for label in pending:
        try:
            # Re-find karena DOM bisa berubah. Cari by placeholder exact match.
            dropdown = page.locator(
                f"button[role='combobox']:has(span:text-is('Select {label}'))"
            ).first
            if not dropdown.is_visible():
                continue
            dropdown.click()
            page.wait_for_timeout(800)

            opts = page.locator("[role='option']:visible").all()
            opt_texts = []
            for o in opts:
                try:
                    t = (o.inner_text(timeout=800) or "").strip()
                    if t and t not in opt_texts:
                        opt_texts.append(t)
                except Exception:
                    pass
            if opt_texts:
                options_map[label] = opt_texts
                add_log(f"[GM]    - {label}: {len(opt_texts)} opsi -> {opt_texts[:5]}{'...' if len(opt_texts)>5 else ''}")
            else:
                add_log(f"[GM]    {label}: opsi tidak terdeteksi")

            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
        except Exception as e:
            add_log(f"[GM] Gagal scrape '{label}': {str(e)[:80]}")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            continue

    return options_map


def _select_dropdown_by_label(page, label_text, option_value):
    """Klik combobox 'Select {label_text}' lalu pilih option."""
    add_log(f"[GM] Isi {label_text}: {option_value}")
    try:
        dropdown = page.locator(
            f"button[role='combobox']:has(span:text-is('Select {label_text}'))"
        ).first
        dropdown.wait_for(state="visible", timeout=10000)
        dropdown.click()
        smart_wait(page, 700, 1300)

        # Opsi muncul di radix portal
        opt_lit = _xpath_literal(option_value)
        # :has-text() Playwright selector butuh escape \' untuk apostrof di CSS string
        opt_css_safe = option_value.replace("\\", "\\\\").replace("'", "\\'")
        option = None
        for sel in [
            f"xpath=//*[@role='option' and normalize-space()={opt_lit}]",
            f"[role='option']:has-text('{opt_css_safe}')",
            f"xpath=//*[normalize-space(text())={opt_lit}]",
        ]:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=3000)
                option = loc
                break
            except Exception:
                continue
        if option is None:
            raise Exception(f"opsi '{option_value}' tidak ditemukan")
        option.click()
        smart_wait(page, 500, 1000)
    except Exception as e:
        add_log(f"[GM] Gagal isi '{label_text}' = '{option_value}': {str(e)[:80]}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass


# ===================== PUBLIC ENTRIES =====================
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
                    return False, err

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
                return True, None
            if cleared:
                add_log("[GM] Form di-reset (title kosong) -> sukses")
                smart_wait(page, 1000, 2000)
                return True, None

            # Cek toast sukses
            try:
                success_toast = page.locator(
                    "xpath=//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'success')"
                    " or contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'published')]"
                )
                if success_toast.count() > 0:
                    return True, None
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
                    return False, f"Form error: {combined[:150]}"
                return False, "Submit tidak redirect, status tidak jelas"
            except Exception as e:
                return False, f"Submit tidak redirect ({str(e)[:60]})"

        except Exception as e:
            pesan = str(e)
            if "Timeout" in pesan:
                indo_error = "Waktu habis, elemen tidak ditemukan"
            elif "net::" in pesan:
                indo_error = "Gagal membuka halaman, cek koneksi"
            else:
                indo_error = f"Error: {pesan[:100]}"
            add_log(f"[GM] Gagal: {indo_error}")
            return False, indo_error

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

    ok, err = create_listing(game_name, title, description, harga,
                             field_mapping or {}, image_paths)
    ts = datetime.now().strftime("%d %b, %y | %H:%M")
    if ok:
        return True, f"✅ GM | {len(image_paths)} images uploaded | {ts}"
    return False, f"❌ GM | {(err or 'unknown')[:80]}"
