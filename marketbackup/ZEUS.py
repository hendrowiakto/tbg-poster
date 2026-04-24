"""create/ZEUS.py - ZeusX marketplace adapter (https://zeusx.com).

Entry points dipakai orchestrator (bot_create.py):
- scrape_form_options(game_name) -> dict | {} | None
- create_listing(game_name, title, description, harga, field_mapping,
                 image_paths, raw_image_url=None) -> (ok, err)
- run(sheet, baris_nomor, worker_id, **kwargs) -> (ok, k_line)
- cache_looks_bogus(cache) -> bool

Flow ZEUS (per user spec):
  1. goto /create-offer
  2. search game -> klik hasil pertama yg match
  3. isi Title, Price (US$)
  4. isi dynamic dropdown (jumlah variabel, hasil AI mapping)
  5. pastikan radio Coordinated aktif (default aktif)
  6. isi Hours = 1
  7. isi Description: baris 1 = raw image URL (tanpa scheme https://),
     baris berikut = deskripsi listing
  8. upload gambar satu-per-satu ke tiap <input type=file> (slot kadang 8-10)
  9. centang checkbox terms
  10. klik List Items -> sukses bila URL pindah dari /create-offer

CSS classes ZEUS ber-hash (CSS Module: foo_bar__HASH). Selector pakai
kombinasi hashed exact + class prefix supaya survive hash churn.
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


# ZEUS anti-spam throttle: account ZEUS ke-suspend 24 jam karena post terlalu
# cepat. Tambah 25% base delay + 0-40% random jitter on top supaya pattern
# inter-action timing lebih human-like.
_ZEUS_SLOW_MULT    = 1.25
_ZEUS_JITTER_MAX   = 0.40  # 0-40% extra random per call


def smart_wait(page, min_ms, max_ms):
    """ZEUS-local smart_wait: base × 1.25 + random 0-40% extra jitter."""
    jitter = 1.0 + random.uniform(0, _ZEUS_JITTER_MAX)
    lo = int(min_ms * _ZEUS_SLOW_MULT * jitter)
    hi = int(max_ms * _ZEUS_SLOW_MULT * jitter)
    if hi < lo:
        hi = lo
    _base_smart_wait(page, lo, hi)


# ===================== KONSTANTA =====================
ZEUS_CREATE_URL = "https://zeusx.com/create-offer"

# Adapter protocol (dibaca orchestrator via importlib):
MARKET_CODE     = "ZEUS"
HARGA_COL       = 8                                 # H
MAX_IMAGES      = 10                                # ZEUS file one-by-one, cap 10
NO_OPTIONS_SENTINEL_ZEUS = "[tidak ditemukan options ZEUS]"
CACHE_SENTINEL  = NO_OPTIONS_SENTINEL_ZEUS


# ===================== TAB TITLE =====================
def _set_worker_tab_title(page):
    """Inject prefix 'Worker N | ' ke document.title tab ZEUS."""
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


# ===================== CATEGORY =====================
def _select_category_accounts(page):
    """Step 1: klik kartu 'Accounts' di Select Category. Idempotent - kalau
    sudah ter-select (yellow border), klik ulang ndak apa-apa."""
    add_log("[ZEUS] Pilih Category: Accounts")
    for sel in [
        "xpath=//*[normalize-space()='Accounts']/ancestor::*[self::div or self::button][1]",
        "xpath=//div[.//*[normalize-space()='Accounts'] and "
        ".//*[contains(normalize-space(),'Game accounts')]]",
        "div:has(> * >> text='Accounts'):has-text('Game accounts')",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=4000)
            loc.click()
            smart_wait(page, 500, 1000)
            return
        except Exception:
            continue
    add_log("[ZEUS] Kartu Accounts tidak ditemukan (mungkin sudah aktif default)")


# ===================== GAME PICKER =====================
def _select_game(page, game_name):
    """Search nama game, klik hasil pertama yg match."""
    add_log(f"[ZEUS] Pilih Game: {game_name}")

    search = None
    for sel in [
        "input[class*='co-select-game_input-search']",
        "input[placeholder*='Search by game name' i]",
        "input[placeholder*='game name' i]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=8000)
            search = loc
            break
        except Exception:
            continue
    if search is None:
        raise Exception("Input search game tidak ditemukan")

    # React-controlled input: focus via click, clear via keyboard, type via keyboard
    # (fill() sering ndak trigger onChange React, type() lewat keyboard event aman).
    try:
        search.scroll_into_view_if_needed()
    except Exception:
        pass
    search.click()
    smart_wait(page, 200, 400)
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    page.keyboard.type(game_name, delay=40)
    smart_wait(page, 1200, 2000)

    name_lit  = _xpath_literal(game_name)
    lower_lit = _xpath_literal(game_name.lower())
    upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    lower = "abcdefghijklmnopqrstuvwxyz"

    selectors = [
        f"xpath=//div[contains(@class,'co-select-game_game-name')]//span[normalize-space()={name_lit}]",
        f"xpath=//div[contains(@class,'co-select-game_game-name')]//span"
        f"[contains(translate(normalize-space(.),'{upper}','{lower}'),{lower_lit})]",
    ]
    # Fuzzy suffix (game dengan ':')
    if ":" in game_name:
        suffix = game_name.split(":", 1)[1].strip().lower()
        if suffix:
            suf_lit = _xpath_literal(suffix)
            selectors.append(
                f"xpath=//div[contains(@class,'co-select-game_game-name')]//span"
                f"[contains(translate(normalize-space(.),'{upper}','{lower}'),{suf_lit})]"
            )
    # Last fallback: klik hasil pertama (user said "pasti ada 1")
    selectors.append("div[class*='co-select-game_game-name']")

    option = None
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=4000)
            option = loc
            break
        except Exception:
            continue
    if option is None:
        raise Exception(f"Game '{game_name}' tidak muncul di hasil search")
    option.click()
    smart_wait(page, 1500, 2500)


# ===================== DROPDOWN META + SCRAPE =====================
_LABEL_JS = """
() => {
  const wrappers = Array.from(
    document.querySelectorAll("div[class*='select-form_select-wrapper']")
  );
  const clean = (txt) => (txt || '').trim().split('\\n')[0].trim();
  const looksLikeLabel = (txt) =>
    txt && txt.length <= 60
    && !/^please\\s+select/i.test(txt)
    && !/US\\$/.test(txt);

  return wrappers.map((w, i) => {
    let label = '';
    // Prio 1: cari element dgn class 'select-form_label' di parent yang sama
    // (struktur ZEUS: .select-form_select-form > .select-form_label + .select-form_select-wrapper)
    let parent = w.parentElement;
    let hops = 0;
    while (parent && hops < 3 && !label) {
      const lbl = parent.querySelector("[class*='select-form_label']");
      if (lbl && !lbl.contains(w)) {
        const txt = clean(lbl.innerText);
        if (looksLikeLabel(txt)) { label = txt; break; }
      }
      parent = parent.parentElement;
      hops++;
    }
    // Prio 2: sibling sebelumnya yg class-nya mengandung 'label' / 'title' / 'heading'
    if (!label) {
      let prev = w.previousElementSibling;
      while (prev) {
        const cls = (typeof prev.className === 'string') ? prev.className : '';
        if (/label|title|heading/i.test(cls)) {
          const txt = clean(prev.innerText);
          if (looksLikeLabel(txt)) { label = txt; break; }
        }
        prev = prev.previousElementSibling;
      }
    }
    // Prio 3: fallback - walk ke atas sampai 5 level cari sibling text pendek
    if (!label) {
      let cur = w;
      for (let hop = 0; hop < 5; hop++) {
        let prev = cur.previousElementSibling;
        while (prev) {
          const txt = clean(prev.innerText);
          if (looksLikeLabel(txt)) { label = txt; break; }
          prev = prev.previousElementSibling;
        }
        if (label) break;
        cur = cur.parentElement;
        if (!cur) break;
      }
    }
    return { index: i, label: label || ('Field' + (i+1)) };
  });
}
"""


def _get_dropdown_meta(page):
    """Return list of {index, label} untuk semua select wrapper ZEUS."""
    try:
        return page.evaluate(_LABEL_JS) or []
    except Exception:
        return []


def _open_dropdown_by_index(page, index):
    wrappers = page.locator("div[class*='select-form_select-wrapper']")
    if wrappers.count() <= index:
        raise Exception(f"dropdown index {index} tidak ada")
    wrapper = wrappers.nth(index)
    try:
        wrapper.scroll_into_view_if_needed()
    except Exception:
        pass
    wrapper.click()
    smart_wait(page, 500, 900)
    return wrapper


def _collect_visible_options(page):
    """Scrape option text dari dropdown ZEUS yg lagi terbuka.
    ZEUS render list pakai radio-box style (bukan <li> / [role=option]):
      div.z-scrollbar_content-container
        > div.radio-box_radio-box (= 1 opsi)
            > div.radio-box_label (= teks opsi)
    """
    try:
        return page.evaluate("""
        () => {
          const selectors = [
            // ZEUS radio-box list (primary pattern)
            "div[class*='z-scrollbar_content-container'] div[class*='radio-box_label']",
            "div[class*='select-form_list-item'] div[class*='radio-box_label']",
            "div[class*='radio-box_radio-box'] div[class*='radio-box_label']",
            // Generic fallbacks
            "div[class*='select-form_option']",
            "[role='option']",
            "li[class*='option']",
          ];
          const seen = new Set();
          const out = [];
          for (const sel of selectors) {
            document.querySelectorAll(sel).forEach(el => {
              if (el.offsetParent === null) return;
              const t = (el.innerText || '').trim();
              if (!t || t.length >= 80 || seen.has(t)) return;
              if (/^please\\s+select/i.test(t)) return;
              seen.add(t);
              out.push(t);
            });
            if (out.length) break;
          }
          return out;
        }
        """) or []
    except Exception:
        return []


def _close_dropdown_by_picking_first(page):
    """ZEUS ndak merespon Escape / re-click wrapper untuk tutup dropdown.
    Workaround: klik option pertama yg visible. Saat scrape ini nilai ke-set
    di form, tapi page langsung ditutup setelah scrape (ephemeral)."""
    try:
        first = page.locator(
            "div[class*='z-scrollbar_content-container'] "
            "div[class*='radio-box_radio-box']"
        ).first
        if first.count() > 0 and first.is_visible():
            first.click()
            return True
    except Exception:
        pass
    return False


def _scrape_form_options_page(page):
    """Scrape dynamic dropdowns setelah Game dipilih. Return {label:[opts]}."""
    smart_wait(page, 2500, 3500)
    metas = _get_dropdown_meta(page)
    add_log(f"[ZEUS] Detected {len(metas)} dropdown: {[m['label'] for m in metas]}")

    options_map = {}
    used_labels = set()
    for meta in metas:
        idx = meta["index"]
        raw_label = meta["label"] or f"Field{idx+1}"
        label = raw_label
        n = 2
        while label in used_labels:
            label = f"{raw_label} {n}"
            n += 1
        used_labels.add(label)

        try:
            _open_dropdown_by_index(page, idx)
            opts = _collect_visible_options(page)
            seen = set()
            dedup = []
            for t in opts:
                if t not in seen:
                    seen.add(t)
                    dedup.append(t)
            if dedup:
                options_map[label] = dedup
                preview = dedup[:5]
                add_log(f"[ZEUS]    - {label}: {len(dedup)} opsi -> {preview}"
                        f"{'...' if len(dedup) > 5 else ''}")
            else:
                add_log(f"[ZEUS]    {label}: opsi tidak terdeteksi")
            # Tutup dropdown dengan pick option pertama (Escape ndak jalan).
            _close_dropdown_by_picking_first(page)
            smart_wait(page, 400, 700)
        except Exception as e:
            add_log(f"[ZEUS] Gagal scrape '{label}': {str(e)[:80]}")
            _close_dropdown_by_picking_first(page)
            continue
    return options_map


def _select_dropdown_by_label(page, label_text, option_value):
    """Cari dropdown dgn inferred label sama, buka, klik option."""
    add_log(f"[ZEUS] Isi {label_text}: {option_value}")
    metas = _get_dropdown_meta(page)
    # Match exact label, lalu fallback base label (tanpa suffix ' 2', ' 3'),
    # lalu fuzzy case-insensitive + strip non-alnum (handle spasi/tanda baca beda).
    base_label = re.sub(r"\s+\d+$", "", label_text)
    def _norm(s):
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())
    want = _norm(label_text)
    want_base = _norm(base_label)
    target_idx = None
    for m in metas:
        if m["label"] == label_text:
            target_idx = m["index"]; break
    if target_idx is None:
        for m in metas:
            if m["label"] == base_label:
                target_idx = m["index"]; break
    if target_idx is None:
        for m in metas:
            n = _norm(m["label"])
            if n == want or n == want_base:
                target_idx = m["index"]; break
    if target_idx is None:
        add_log(f"[ZEUS] Dropdown '{label_text}' ndak match. "
                f"Available: {[m['label'] for m in metas]}")
        return

    try:
        _open_dropdown_by_index(page, target_idx)

        opt_lit = _xpath_literal(option_value)
        opt_css = option_value.replace("\\", "\\\\").replace("'", "\\'")
        option = None
        for sel in [
            # ZEUS radio-box container matching option label
            f"xpath=//div[contains(@class,'z-scrollbar_content-container')]"
            f"//div[contains(@class,'radio-box_radio-box')]"
            f"[.//div[contains(@class,'radio-box_label') and normalize-space()={opt_lit}]]",
            # Direct label match (click propagates ke radio-box parent)
            f"xpath=//div[contains(@class,'radio-box_label') and normalize-space()={opt_lit}]",
            # Generic fallbacks
            f"xpath=//*[contains(@class,'select-form_option') and normalize-space()={opt_lit}]",
            f"xpath=//*[@role='option' and normalize-space()={opt_lit}]",
            f"[role='option']:has-text('{opt_css}')",
        ]:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=3000)
                option = loc
                break
            except Exception:
                continue
        if option is None:
            add_log(f"[ZEUS] Opsi '{option_value}' tidak ketemu untuk '{label_text}'"
                    f", fallback pick option pertama")
            _close_dropdown_by_picking_first(page)
            return
        option.click()
        smart_wait(page, 400, 800)
    except Exception as e:
        add_log(f"[ZEUS] Gagal isi '{label_text}' = '{option_value}': {str(e)[:80]}")
        _close_dropdown_by_picking_first(page)


# ===================== FIELD HELPERS =====================
def _fill_title(page, title):
    add_log("[ZEUS] Isi Title...")
    ti = page.locator(
        "input[placeholder^='Eg:'], input[placeholder*='Account' i]"
    ).first
    ti.wait_for(state="visible", timeout=10000)
    ti.click()
    ti.fill(title)
    smart_wait(page, 400, 800)


def _fill_price(page, harga):
    price_raw = str(harga)
    price_clean = re.sub(r"[^0-9.,]", "", price_raw).replace(",", ".")
    if price_clean.count(".") > 1:
        parts = price_clean.split(".")
        price_clean = "".join(parts[:-1]) + "." + parts[-1]
    add_log(f"[ZEUS] Isi Price: US${price_clean}")
    pi = page.locator(
        "div[class*='input_input-wrapper']:has(div[class*='input_prefix']) input"
    ).first
    pi.wait_for(state="visible", timeout=10000)
    pi.click()
    pi.fill(price_clean)
    smart_wait(page, 400, 800)


def _ensure_coordinated(page):
    """Coordinated radio default aktif. Klik kalau belum."""
    add_log("[ZEUS] Cek Coordinated radio")
    try:
        active_count = page.locator(
            "div[class*='radio-box_radio'][class*='radio-box_active']"
        ).count()
        if active_count == 0:
            sel = ("xpath=(//*[normalize-space()='Coordinated'])[1]"
                   "/following::div[contains(@class,'radio-box_radio')][1]")
            page.locator(sel).first.click()
            smart_wait(page, 300, 600)
    except Exception as e:
        add_log(f"[ZEUS] Coordinated warning: {str(e)[:80]}")


def _fill_hours(page, hours="1"):
    """Delivery Time -> Hours input. Target input yg SIBLING dgn
    <div class='input_label__*'>Hours</div> (bukan 'Days')."""
    add_log(f"[ZEUS] Isi Hours: {hours}")
    # Cari div.input_input__ yg mengandung label 'Hours', lalu ambil input-nya.
    hours_block_sel = (
        "xpath=//div[contains(@class,'input_input__')]"
        "[.//div[contains(@class,'input_label__') and normalize-space()='Hours']]"
        "//input"
    )
    hi = None
    try:
        loc = page.locator(hours_block_sel).first
        loc.wait_for(state="visible", timeout=6000)
        hi = loc
    except Exception:
        pass
    if hi is None:
        add_log("[ZEUS] Input Hours tidak ketemu")
        return
    try:
        hi.click()
        # React-controlled: Ctrl+A -> Delete -> type, jangan pakai .fill()
        hi.press("Control+A")
        hi.press("Delete")
        page.keyboard.type(str(hours))
        smart_wait(page, 300, 600)
    except Exception as e:
        add_log(f"[ZEUS] Gagal isi Hours: {str(e)[:80]}")


def _fill_description(page, description, raw_image_url=None):
    """CKEditor contenteditable. Paragraph 1 = URL gambar (tanpa scheme),
    paragraph 2+ = description body. Pakai CKEditor setData API via JS
    biar urutan paragraph deterministik (typing via keyboard kadang kacau)."""
    add_log("[ZEUS] Isi Description (CKEditor)...")

    url_line = ""
    if raw_image_url:
        url_line = "Full Screenshot Detail: " + re.sub(r"^https?://", "", raw_image_url.strip())

    # Build list of paragraph: URL dulu, lalu body lines.
    paragraphs = []
    if url_line:
        paragraphs.append(url_line)
    body = description or ""
    if body:
        for line in body.split("\n"):
            paragraphs.append(line)
    if not paragraphs:
        return

    editor = None
    for sel in [
        "div.ck-editor__editable[contenteditable='true'][aria-label*='main' i]",
        "div.ck-editor__editable[contenteditable='true']",
        "div[class*='ck-editor__editable'][contenteditable='true']",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=8000)
            editor = loc
            break
        except Exception:
            continue
    if editor is None:
        add_log("[ZEUS] CKEditor tidak ketemu, skip description")
        return

    # Build HTML (escape < > & supaya ndak diartikan tag).
    def _esc(s):
        return (s.replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;"))
    html_paragraphs = [f"<p>{_esc(p)}</p>" if p else "<p>&nbsp;</p>"
                       for p in paragraphs]
    html = "".join(html_paragraphs)

    # Coba pakai CKEditor 5 instance API dulu (property ckeditorInstance
    # di-attach sama CKEditor 5 ke editable root div).
    js = """
    (html) => {
      const eds = document.querySelectorAll(
        '.ck-editor__editable, div[class*="ck-editor__editable"]'
      );
      for (const el of eds) {
        const inst = el.ckeditorInstance;
        if (inst && typeof inst.setData === 'function') {
          inst.setData(html);
          return true;
        }
      }
      return false;
    }
    """
    set_ok = False
    try:
        set_ok = bool(page.evaluate(js, html))
    except Exception as e:
        add_log(f"[ZEUS] setData JS gagal: {str(e)[:80]}")

    if not set_ok:
        # Fallback: keyboard insert. Urut URL dulu, baru body.
        add_log("[ZEUS] setData fallback -> keyboard type")
        editor.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        for i, para in enumerate(paragraphs):
            if para:
                page.keyboard.insert_text(para)
            if i < len(paragraphs) - 1:
                page.keyboard.press("Enter")

    smart_wait(page, 400, 800)


def _upload_images(page, image_paths):
    """Upload satu-per-satu ke tiap <input type=file>. Re-query tiap iterasi
    karena slot baru ter-render setelah upload sebelumnya selesai."""
    if not image_paths:
        return 0
    add_log(f"[ZEUS] Upload {len(image_paths)} gambar (one-by-one)...")
    count = 0
    for i, path in enumerate(image_paths):
        inputs = page.locator("input[type='file'][accept*='image']").all()
        if i >= len(inputs):
            add_log(f"[ZEUS] Slot gambar habis di index {i}, stop upload")
            break
        try:
            inputs[i].set_input_files(path)
            count += 1
            smart_wait(page, 1800, 2600)
        except Exception as e:
            add_log(f"[ZEUS] Upload gambar {i+1} gagal: {str(e)[:80]}")
            break
    add_log(f"[ZEUS] Total terupload: {count}/{len(image_paths)}")
    return count


def _check_terms(page):
    """Ada >1 checkbox di form ZEUS (mis. 'account already linked' + terms).
    Target SPESIFIK checkbox yg ada text 'Terms of Service' di label-nya."""
    add_log("[ZEUS] Centang checkbox Terms of Service")
    target = None
    for sel in [
        "div[class*='checkbox_checkbox__']:has-text('Terms of Service') "
        "div[class*='checkbox_checkbox-box']",
        "div[class*='checkbox_checkbox__']:has-text('I agree') "
        "div[class*='checkbox_checkbox-box']",
        "xpath=//*[contains(normalize-space(),'Terms of Service')]"
        "/ancestor::div[contains(@class,'checkbox_checkbox__')][1]"
        "//div[contains(@class,'checkbox_checkbox-box')]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=4000)
            target = loc
            break
        except Exception:
            continue
    if target is None:
        add_log("[ZEUS] Checkbox Terms tidak ditemukan")
        return
    try:
        target.click()
        smart_wait(page, 300, 600)
    except Exception as e:
        add_log(f"[ZEUS] Gagal centang terms: {str(e)[:80]}")


# ===================== PUBLIC ENTRIES =====================
def create_listing(game_name, title, description, harga, field_mapping,
                   image_paths, raw_image_url=None):
    """Full flow isi form + submit. Return (ok, err, uploaded_count)."""
    uploaded_count = 0
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

            add_log("[ZEUS] Buka halaman create-offer...")
            page.goto(ZEUS_CREATE_URL, wait_until="domcontentloaded", timeout=60000)
            _set_worker_tab_title(page)
            smart_wait(page, 3500, 5500)

            # 1. Game (Category 'Accounts' auto-selected by ZEUS)
            _select_game(page, game_name)

            # 2. Title
            _fill_title(page, title)

            # 3. Price
            _fill_price(page, harga)

            # 4. Dynamic dropdowns (AI-mapped).
            # Tunggu dropdown ter-render dulu supaya _get_dropdown_meta
            # liat DOM yg lengkap (game selection trigger form render async).
            if field_mapping:
                try:
                    page.wait_for_selector(
                        "div[class*='select-form_select-wrapper']",
                        state="visible", timeout=10000,
                    )
                except Exception:
                    add_log("[ZEUS] Dropdown wrapper belum ter-render setelah 10s")
                smart_wait(page, 800, 1500)
                for label, val in field_mapping.items():
                    if val is None or val == "":
                        continue
                    _select_dropdown_by_label(page, label, val)
                    smart_wait(page, 400, 800)

            # 5. Coordinated
            _ensure_coordinated(page)

            # 6. Hours = 1
            _fill_hours(page, "1")

            # 7. Description (URL line + body)
            _fill_description(page, description, raw_image_url=raw_image_url)

            # 8. Upload gambar
            uploaded_count = _upload_images(page, image_paths or [])

            # 9. Centang terms
            _check_terms(page)

            # 10. Submit
            add_log("[ZEUS] Klik List Items...")
            submit = None
            for sel in [
                "button[class*='create-offer-route_submit-btn']",
                "button:has-text('List Items')",
                "button:has(div:text('List Items'))",
            ]:
                try:
                    loc = page.locator(sel).first
                    loc.wait_for(state="visible", timeout=5000)
                    submit = loc
                    break
                except Exception:
                    continue
            if submit is None:
                return False, "Tombol List Items tidak ditemukan", uploaded_count
            start_url = page.url
            submit.click()

            # Deteksi sukses: (A) popup 'successfully listed' muncul, atau
            # (B) URL pindah dari /create-offer. Poll max ~120s.
            success_popup_sel = (
                "xpath=//*[contains(@class,'success-popup_text-description')"
                " or contains(@class,'success-popup')]"
                "[contains(normalize-space(.),'successfully listed')]"
            )
            success = False
            for _ in range(240):
                page.wait_for_timeout(500)
                try:
                    popup = page.locator(success_popup_sel).first
                    if popup.count() > 0 and popup.is_visible():
                        success = True
                        add_log("[ZEUS] Popup sukses terdeteksi")
                        break
                except Exception:
                    pass
                try:
                    cur = page.url
                    if cur != start_url and "/create-offer" not in cur:
                        success = True
                        add_log(f"[ZEUS] Redirect ke: {cur}")
                        break
                except Exception:
                    pass

            if success:
                # Page kadang auto-close/redirect sesudah popup sukses.
                # smart_wait di-wrap supaya exception ndak batalin sukses.
                try:
                    smart_wait(page, 1000, 2000)
                except Exception:
                    pass
                return True, None, uploaded_count

            # Fallback: kumpul error toast/msg
            try:
                err_locs = page.locator(
                    "[role='alert'], [class*='error'], [class*='toast']"
                ).all()
                msgs = []
                for el in err_locs[:10]:
                    try:
                        if not el.is_visible():
                            continue
                        t = (el.inner_text(timeout=800) or "").strip()
                        if not t or not (5 <= len(t) <= 200):
                            continue
                        if t not in msgs:
                            msgs.append(t)
                    except Exception:
                        continue
                if msgs:
                    combined = " | ".join(msgs[:4])
                    add_log(f"[ZEUS] Form error: {combined[:250]}")
                    return False, f"Form error: {combined[:150]}", uploaded_count
            except Exception:
                pass

            return False, "Submit tidak redirect, status tidak jelas", uploaded_count

        except Exception as e:
            pesan = str(e)
            if "Timeout" in pesan:
                indo = "Waktu habis, elemen tidak ditemukan"
            elif "net::" in pesan:
                indo = "Gagal buka halaman, cek koneksi"
            else:
                indo = f"Error: {pesan[:100]}"
            add_log(f"[ZEUS] Gagal: {indo}")
            return False, indo, uploaded_count

        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


def scrape_form_options(game_name):
    """Buka /create-offer, pilih Game, scrape dropdown labels+opsi.
    Return dict (non-empty) / {} (sentinel) / None (fail)."""
    add_log("[ZEUS] Scrape form options (pertama kali)...")
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

            page.goto(ZEUS_CREATE_URL, wait_until="domcontentloaded", timeout=60000)
            _set_worker_tab_title(page)
            smart_wait(page, 3500, 5500)
            _select_game(page, game_name)
            return _scrape_form_options_page(page)

        except Exception as e:
            add_log(f"[ZEUS] Gagal scrape form options: {str(e)[:100]}")
            return None
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


# ===================== ADAPTER ENTRY =====================
def cache_looks_bogus(cache_dict):
    """Kalau label fallback 'FieldN' muncul di cache, heuristik label gagal ->
    invalidate supaya re-scrape bisa dapat label yg proper."""
    if not isinstance(cache_dict, dict) or not cache_dict:
        return False
    for k in cache_dict.keys():
        if re.match(r"^Field\d+$", str(k).strip()):
            return True
    return False


def run(sheet, baris_nomor, worker_id, *, game_name, description, title, harga,
        field_mapping, image_paths=None, image_urls=None,
        raw_image_url=None, is_imgur=False):
    """Adapter entry dipanggil orchestrator. ZEUS quirks:
    - URL album raw (drive/imgur/postimg) di-strip scheme -> line 1 description.
    - Gambar pakai FILE UPLOAD (image_paths), one-per-slot.
    - image_urls/is_imgur di-ignore (ZEUS tidak punya URL field).
    Return (ok, k_line)."""
    _worker_local.worker_id = f"{worker_id}-ZEUS"

    ok, err, uploaded = create_listing(
        game_name, title, description or "", harga,
        field_mapping or {}, image_paths or [],
        raw_image_url=raw_image_url,
    )
    ts = datetime.now().strftime("%d %b, %y | %H:%M")
    if ok:
        return True, f"✅ ZEUS | {uploaded} images uploaded | {ts}"
    return False, f"❌ ZEUS | {(err or 'unknown')[:80]}"
