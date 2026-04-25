"""create/G2G.py - G2G marketplace adapter.

Entry points dipakai orchestrator (bot_create.py):
- scrape_form_options(game_name_g2g) -> dict | {} | None
- create_listing(game_name_g2g, title, deskripsi, harga, field_mapping, image_urls)
    -> (ok: bool, err: str | None)
- cache_looks_bogus(cache_dict) -> bool

Helper internal (_g2g_*) tetap module-level supaya mudah di-monkeypatch saat
debug. Tidak ada import dari bot_create - semua runtime dep via create._shared.

NOTE: G2G pakai Quasar framework. Selector XPath & timings di sini TIDAK
diubah relatif ke implementasi asli di bot_create.py. Pindah-only refactor.
"""

import re
from datetime import datetime
from playwright.sync_api import sync_playwright

from create._shared import (
    _worker_local,
    _log as add_log,
    _get_chrome_debug_port,
    xpath_literal as _xpath_literal,
    obfuscate_image_url as _obfuscate_image_url,
    smart_wait,
    get_or_create_context,
    resolve_image_future,
)


# ===================== KONSTANTA =====================
G2G_CREATE_URL = "https://www.g2g.com/offers/sell?cat_id=5830014a-b974-45c6-9672-b51e83112fb7"

NO_OPTIONS_SENTINEL_G2G = "[tidak ditemukan options G2G]"  # marker P45: game G2G tanpa dynamic form

# Adapter protocol (dibaca orchestrator via importlib):
MARKET_CODE     = "G2G"
HARGA_COL       = 7                              # G (G2G pakai kolom harga berbeda dari GM)
MAX_IMAGES      = 10                             # G2G URL paste, cap 10
CACHE_SENTINEL  = NO_OPTIONS_SENTINEL_G2G

_G2G_LABEL_EXCLUDE = {
    "silakan pilih", "expand_more", "expand_less", "arrow_drop_down",
    "arrow_drop_up", "close", "search", "visibility", "cancel",
    "", "*",
}


# ===================== TAB TITLE =====================
def _g2g_set_worker_tab_title(page):
    """Inject prefix 'Worker N | ' ke document.title tab G2G (sama logika
    dengan _set_worker_tab_title GM tapi tanpa asumsi title awal)."""
    wid = getattr(_worker_local, "worker_id", None)
    if not wid:
        return
    js = """
    (function(wid){
      var prefix = 'Worker ' + wid + ' G2G | ';
      function apply(){
        var t = document.title || '';
        var stripped = t.replace(/^Worker \\d+ G2G \\| /, '');
        if (document.title !== prefix + stripped) {
          document.title = prefix + stripped;
        }
      }
      apply();
      if (!window.__workerTitleObsG2G) {
        var el = document.querySelector('title');
        if (el) {
          var obs = new MutationObserver(apply);
          obs.observe(el, {childList: true, subtree: true, characterData: true});
          window.__workerTitleObsG2G = obs;
        }
      }
    })(""" + str(wid) + ");"
    try:
        page.evaluate(js)
    except Exception:
        pass


# ===================== GAME SELECTION =====================
def _select_g2g_game(page, game_name_g2g):
    """Pilih produk di G2G: klik dropdown 'Pilih produk' -> search input -> ketik
    nama game -> klik option match.
    """
    add_log(f"[G2G] Pilih produk: {game_name_g2g}")

    # Step 1: klik trigger dropdown "Pilih produk" (Quasar q-btn atau q-select wrapper).
    trigger = None
    for sel in [
        "xpath=//button[.//span[contains(normalize-space(),'Pilih produk')]]",
        "xpath=//*[contains(@class,'q-field') and .//*[contains(normalize-space(),'Pilih produk')]]",
        "xpath=//*[normalize-space()='Pilih produk']/ancestor::*[self::button or contains(@class,'q-field')][1]",
        "xpath=//*[contains(normalize-space(.),'Pilih produk')][self::button or @role='button']",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=5000)
            trigger = loc
            break
        except Exception:
            continue
    if trigger is None:
        raise Exception("Trigger dropdown 'Pilih produk' tidak ditemukan")

    trigger.click()
    smart_wait(page, 300, 600)

    # Step 2: search input muncul (di menu dropdown atau dialog).
    search_box = None
    for sel in [
        "xpath=//div[contains(@class,'q-menu') or contains(@class,'q-dialog')]//input",
        "xpath=//input[contains(@placeholder,'Cari') or contains(@placeholder,'cari') or contains(@placeholder,'Search')]",
        "input.q-field__native:focus",
        "input.q-field__native:visible",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=3000)
            search_box = loc
            break
        except Exception:
            continue

    if search_box is not None:
        try:
            search_box.click()
        except Exception:
            pass
        try:
            search_box.fill("")
        except Exception:
            pass
        page.keyboard.type(game_name_g2g, delay=10)
        smart_wait(page, 400, 800)

    # Step 3: klik option yang match.
    gm_lit = _xpath_literal(game_name_g2g)
    upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    lower = "abcdefghijklmnopqrstuvwxyz"
    gm_lower_lit = _xpath_literal(game_name_g2g.lower())
    selectors = [
        f"xpath=//div[contains(@class,'q-item')][normalize-space()={gm_lit}]",
        f"xpath=//*[@role='option' and normalize-space()={gm_lit}]",
        f"xpath=//*[normalize-space()={gm_lit}]",
        f"xpath=//div[contains(@class,'q-item')][contains(translate(normalize-space(.),'{upper}','{lower}'),{gm_lower_lit})]",
    ]
    option = None
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=3000)
            option = loc
            break
        except Exception:
            continue
    if option is None:
        raise Exception(f"Option produk '{game_name_g2g}' tidak muncul")
    option.click()
    smart_wait(page, 800, 1500)


def _g2g_click_lanjutkan(page, timeout_ms=12000):
    """Setelah produk dipilih, klik tombol 'Lanjutkan' yang redirect ke
    /offers/create?service_id=... (halaman form aktual).
    Return True kalau berhasil klik dan URL pindah ke /offers/create,
    False kalau tombol tidak muncul (berarti sudah di page create langsung).
    """
    selectors = [
        "xpath=//a[@role='link' and contains(@href,'/offers/create') and .//*[normalize-space(.)='Lanjutkan']]",
        "xpath=//a[.//span[normalize-space(.)='Lanjutkan']]",
        "xpath=//*[self::a or self::button][.//*[normalize-space(.)='Lanjutkan']]",
        "xpath=//*[normalize-space(.)='Lanjutkan' and (self::a or self::button or @role='link' or @role='button')]",
    ]
    btn = None
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout_ms)
            btn = loc
            break
        except Exception:
            continue
    if btn is None:
        add_log("[G2G] Tombol 'Lanjutkan' tidak muncul (mungkin sudah di form page)")
        return False

    add_log("[G2G] Klik 'Lanjutkan' -> menuju form create offer")
    prev_url = ""
    try:
        prev_url = page.url
    except Exception:
        pass
    try:
        btn.click()
    except Exception:
        try:
            btn.evaluate("el => el.click()")
        except Exception as e:
            add_log(f"[G2G] Gagal klik Lanjutkan: {str(e)[:100]}")
            return False

    # Tunggu URL berubah ke /offers/create atau DOM form baru.
    try:
        page.wait_for_url("**/offers/create**", timeout=20000)
    except Exception:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    # Tunggu form siap: "Silakan Pilih" dropdown pertama atau Title input muncul.
    # Mulai isi begitu salah satunya visible (max 20s), tanpa sleep tambahan.
    add_log("[G2G] Tunggu form siap (dropdown / title input)...")
    ready_sel = (
        "xpath=(//*[normalize-space(.)='Silakan Pilih']"
        " | //input[contains(@class,'q-field__native')]"
        " | //textarea)[1]"
    )
    try:
        page.locator(ready_sel).first.wait_for(state="visible", timeout=20000)
    except Exception:
        add_log("[G2G] Form marker tidak terdeteksi 20s, lanjut coba isi")
    try:
        new_url = page.url
        if new_url and new_url != prev_url:
            add_log(f"[G2G] Form page loaded: {new_url[:120]}")
    except Exception:
        pass
    return True


# ===================== DROPDOWN SCRAPER =====================
def _g2g_iter_silakan_pilih(page):
    """Return list locator 'Silakan Pilih' button yang saat ini visible.
    Pattern: button/elemen yang text-nya 'Silakan Pilih' (panel expand ke bawah).
    """
    candidates = page.locator(
        "xpath=//*[normalize-space(.)='Silakan Pilih' and (self::button or self::div or @role='button')]"
    ).all()
    return candidates


def _g2g_extract_dropdown_options(page):
    """Extract opsi dari dropdown Quasar yang baru terbuka.
    Target: `.q-list.q-virtual-scroll__content .q-item .q-item__section--main`.
    Scoped ke q-menu / q-card absolute-top yang visible.
    Return list text opsi (dedupe, preserve order).
    """
    try:
        texts = page.evaluate("""() => {
            const result = [];
            const seen = new Set();
            // Cari container dropdown yang visible (q-menu, absolute q-card, atau
            // langsung q-virtual-scroll__content visible).
            const containers = [];
            document.querySelectorAll(
                '.q-menu, .q-card.absolute-top, .q-virtual-scroll__content'
            ).forEach(el => {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    const st = window.getComputedStyle(el);
                    if (st.display !== 'none' && st.visibility !== 'hidden') {
                        containers.push(el);
                    }
                }
            });
            for (const c of containers) {
                const items = c.querySelectorAll(
                    '.q-virtual-scroll__content .q-item .q-item__section--main,' +
                    ' .q-item .q-item__section--main,' +
                    ' [role="option"]'
                );
                for (const it of items) {
                    const t = (it.innerText || '').replace(/\\s+/g,' ').trim();
                    if (!t || t.length > 200) continue;
                    if (seen.has(t)) continue;
                    seen.add(t);
                    result.push(t);
                }
                if (result.length > 0) break;
            }
            return result;
        }""")
        return texts or []
    except Exception:
        return []


def _g2g_scroll_virtual_list(page):
    """Scroll q-virtual-scroll ke bawah supaya semua item ter-render.
    Virtual-scroll hanya render item visible, jadi perlu scroll untuk ambil semua.
    """
    try:
        page.evaluate("""() => {
            const scrollers = document.querySelectorAll(
                '.q-list.q-virtual-scroll, .q-virtual-scroll'
            );
            for (const s of scrollers) {
                const rect = s.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    s.scrollTop = s.scrollHeight;
                }
            }
        }""")
    except Exception:
        pass


def _g2g_scrape_form_options(page):
    """Setelah produk dipilih + Lanjutkan, iterate semua 'Silakan Pilih' ->
    klik -> dropdown muncul -> capture label + opsi -> close (Escape).
    Return dict {field_label: [options]}.

    Struktur G2G dropdown (observed):
      <button>Silakan Pilih <icon>expand_more</icon></button>
      (click) ->
      <div class="q-menu ..."> atau <div class="q-card absolute-top ...">
        <div class="q-list q-virtual-scroll">
          <div class="q-virtual-scroll__content">
            <div class="q-item">
              <div class="q-item__section--main">TEKS_OPSI</div>
            </div>
            ...
          </div>
        </div>
      </div>
    Field label berada di kolom kiri (sibling / parent row) dari trigger.
    """
    options_map = {}

    page.wait_for_timeout(2500)  # tunggu form render setelah produk dipilih

    buttons = _g2g_iter_silakan_pilih(page)
    add_log(f"[G2G] Detected {len(buttons)} 'Silakan Pilih' trigger(s)")

    for idx in range(len(buttons)):
        # Re-find tiap iterasi (DOM bisa re-render saat panel lain toggle).
        current_buttons = _g2g_iter_silakan_pilih(page)
        if idx >= len(current_buttons):
            break
        btn = current_buttons[idx]

        # Label: cari label field via LAYOUT (elemen di kiri trigger dengan Y
        # yg sama) + fallback DOM traversal. Exclude text icon/button internal.
        label = ""
        try:
            label = btn.evaluate(
                """(el) => {
                    const EXCLUDE = new Set([
                        'silakan pilih','expand_more','expand_less',
                        'arrow_drop_down','arrow_drop_up','close','search',
                        'visibility','cancel','','*','info','info_outline',
                        'help','help_outline','error','warning'
                    ]);
                    const clean = (s) => (s||'')
                        .replace(/\\s+/g,' ').replace(/\\*$/,'')
                        .replace(/:\\s*$/,'').trim();
                    const isLabelCandidate = (s) => {
                        if (!s) return false;
                        const low = s.toLowerCase();
                        if (EXCLUDE.has(low)) return false;
                        if (s.length < 2 || s.length > 80) return false;
                        if (s.includes('\\n')) return false;
                        if (low.includes('silakan pilih')) return false;
                        if (low.includes('expand_more')) return false;
                        if (low === low.toLowerCase() && /^[a-z_]+$/.test(s)) {
                            // snake_case lowercase = kemungkinan Material icon name
                            return false;
                        }
                        return true;
                    };
                    const rect = el.getBoundingClientRect();
                    const triggerMidY = rect.top + rect.height / 2;
                    const triggerLeft = rect.left;

                    // Strategi 1 (LAYOUT): scan semua elemen dgn text, pilih yang
                    // posisinya di kiri trigger & Y-nya overlap.
                    const candidates = [];
                    const all = document.querySelectorAll(
                        'label, div, span, p, h1, h2, h3, h4, h5, h6, td, th'
                    );
                    for (const node of all) {
                        if (node === el || node.contains(el) || el.contains(node)) continue;
                        const r = node.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        // harus di kiri trigger
                        if (r.right > triggerLeft - 4) continue;
                        // Y harus overlap dengan trigger
                        const nodeMidY = r.top + r.height / 2;
                        if (Math.abs(nodeMidY - triggerMidY) > 30) continue;
                        // hanya ambil yg child-nya minimal (leaf-ish) agar tidak
                        // menangkap container besar.
                        const ownText = (node.childNodes.length <= 3)
                            ? clean(node.innerText) : '';
                        if (!ownText) continue;
                        if (!isLabelCandidate(ownText)) continue;
                        // jarak horizontal (rightmost candidate closest to trigger)
                        const dx = triggerLeft - r.right;
                        candidates.push({ t: ownText, dx, top: r.top });
                    }
                    if (candidates.length > 0) {
                        candidates.sort((a,b) => a.dx - b.dx);
                        return candidates[0].t;
                    }

                    // Strategi 2: cari row ancestor, ambil kolom kiri.
                    let row = el;
                    for (let i = 0; i < 10; i++) {
                        if (!row || !row.parentElement) break;
                        const cls = row.className || '';
                        if (typeof cls === 'string' && (
                            cls.includes('row') || cls.includes('q-field') ||
                            cls.includes('q-item')
                        )) break;
                        row = row.parentElement;
                    }
                    if (row) {
                        const children = Array.from(row.children || []);
                        for (const c of children) {
                            if (c.contains(el)) continue;
                            const t = clean(c.innerText);
                            if (isLabelCandidate(t)) return t;
                        }
                    }
                    // Strategi 3: walk up, check siblings.
                    let node = el;
                    for (let i = 0; i < 8; i++) {
                        if (!node || !node.parentElement) break;
                        node = node.parentElement;
                        for (const s of (node.children || [])) {
                            if (s.contains(el)) continue;
                            const t = clean(s.innerText);
                            if (isLabelCandidate(t)) return t;
                        }
                    }
                    // Strategi 4: previousElementSibling chain.
                    let prev = el.previousElementSibling;
                    while (prev) {
                        const t = clean(prev.innerText);
                        if (isLabelCandidate(t)) return t;
                        prev = prev.previousElementSibling;
                    }
                    return '';
                }"""
            ) or ""
        except Exception:
            label = ""
        if not label:
            label = f"Field{idx+1}"

        add_log(f"[G2G] Scrape field '{label}' (#{idx+1})...")
        try:
            btn.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        try:
            btn.click()
        except Exception as e:
            add_log(f"[G2G]    Gagal klik Silakan Pilih '{label}': {str(e)[:80]}")
            continue
        page.wait_for_timeout(900)

        # Scroll dropdown virtual list supaya semua item ter-render.
        opt_texts = _g2g_extract_dropdown_options(page)
        if opt_texts:
            _g2g_scroll_virtual_list(page)
            page.wait_for_timeout(400)
            more = _g2g_extract_dropdown_options(page)
            for t in more:
                if t not in opt_texts:
                    opt_texts.append(t)

        if opt_texts:
            options_map[label] = opt_texts
            add_log(f"[G2G]    - {label}: {len(opt_texts)} opsi -> {opt_texts[:6]}{'...' if len(opt_texts)>6 else ''}")
        else:
            add_log(f"[G2G]    {label}: opsi tidak terdeteksi")

        # Tutup panel dengan 3-stage fallback:
        #  1. Press Escape
        #  2. Click body di koordinat neutral (atas halaman, bukan di atas dropdown)
        #  3. Click trigger lagi (toggle)
        # Setelah tiap stage, cek dropdown container sudah hilang.
        _g2g_close_dropdown(page, btn)
        page.wait_for_timeout(600)

    return options_map


def _g2g_close_dropdown(page, trigger_loc=None):
    """Tutup dropdown Quasar. Multi-stage fallback: Escape x2 -> body.click via
    JS -> toggle trigger -> force hide via JS. Quasar q-menu kadang ndak respon
    single Escape / coordinate click (navbar/overlay cover area), jadi perlu
    beberapa strategy berurutan dgn wait pendek."""
    def _dropdown_open():
        try:
            return page.evaluate("""() => {
                const els = document.querySelectorAll(
                    '.q-menu, .q-card.absolute-top, .q-virtual-scroll__content'
                );
                for (const el of els) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        const st = window.getComputedStyle(el);
                        if (st.display !== 'none' && st.visibility !== 'hidden') {
                            return true;
                        }
                    }
                }
                return false;
            }""")
        except Exception:
            return False

    # Stage 1: Escape x2 (kadang first Escape ndak register kalau focus lepas)
    for _ in range(2):
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(200)
        if not _dropdown_open():
            return

    # Stage 2: body.click() via JS - Quasar listen ke global click event buat
    # close menu. Lebih reliable dari mouse coordinate (ndak kena navbar/tombol).
    try:
        page.evaluate("document.body.click();")
    except Exception:
        pass
    page.wait_for_timeout(250)
    if not _dropdown_open():
        return

    # Stage 3: click trigger lagi untuk toggle close
    if trigger_loc is not None:
        try:
            trigger_loc.click(timeout=2000)
        except Exception:
            pass
        page.wait_for_timeout(300)
        if not _dropdown_open():
            return

    # Stage 4: force hide DOM via JS (last resort - nuke q-menu element)
    try:
        page.evaluate("""() => {
            document.querySelectorAll('.q-menu, .q-card.absolute-top').forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) {
                    el.style.display = 'none';
                }
            });
        }""")
    except Exception:
        pass
    page.wait_for_timeout(150)


# ===================== TEXT INPUT FILLER =====================
def _g2g_fill_text_inputs_with_ai(page, title):
    """Scan empty text inputs di atas label 'Judul Penawaran' (Primogem, Pass,
    Stellar Jade, dll) -> fill '1'. Skip inputs yang sudah terisi, skip URL /
    title / deskripsi / harga. Title arg di-accept untuk kompat tapi tidak
    dipakai (dulu buat AI extract, sekarang default '1')."""
    # Collect empty text inputs + label via layout heuristic.
    try:
        fields = page.evaluate("""() => {
            const EXCLUDE_PH = new Set(['https://']);
            const results = [];
            // Cutoff: Y posisi label 'Judul Penawaran'. Input di bawah cutoff diabaikan
            // (title/desc/price/URL berada di bawah section atribut).
            let cutoffY = Infinity;
            const all0 = document.querySelectorAll('label, div, span, p, h4, h5, h6');
            for (const el of all0) {
                let ownText = '';
                for (const c of el.childNodes) {
                    if (c.nodeType === 3) ownText += c.textContent;
                }
                ownText = ownText.replace(/\\s+/g, ' ').replace(/\\*$/, '').trim();
                if (ownText === 'Judul Penawaran' || ownText === 'Judul' || ownText === 'Title') {
                    const r = el.getBoundingClientRect();
                    if (r.top < cutoffY) cutoffY = r.top;
                }
            }
            const inputs = document.querySelectorAll("input.q-field__native");
            inputs.forEach((inp, idx) => {
                if (!inp.offsetParent) return;
                const ph = (inp.placeholder || '').trim();
                if (EXCLUDE_PH.has(ph)) return;
                const type = (inp.type || 'text').toLowerCase();
                if (type !== 'text' && type !== 'number') return;
                if ((inp.value || '').trim() !== '') return;
                // exclude title/judul (large maxLength)
                const ml = parseInt(inp.maxLength, 10) || 0;
                if (ml > 100) return;
                // Skip input di bawah label 'Judul Penawaran' (batas atribut section).
                const rect = inp.getBoundingClientRect();
                if (rect.top >= cutoffY) return;
                // find label left of input
                const left = rect.left;
                const candidates = [];
                const all = document.querySelectorAll('label, div, span, p, h4, h5, h6');
                for (const node of all) {
                    if (node === inp || node.contains(inp) || inp.contains(node)) continue;
                    const r = node.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    const overlap = Math.min(r.bottom, rect.bottom) - Math.max(r.top, rect.top);
                    if (overlap < Math.min(r.height, rect.height) * 0.3) continue;
                    if (r.right > left + 5) continue;
                    let ownText = '';
                    for (const c of node.childNodes) {
                        if (c.nodeType === 3) ownText += c.textContent;
                    }
                    ownText = ownText.replace(/\\s+/g, ' ').replace(/\\*$/, '').trim();
                    if (ownText.length < 2 || ownText.length > 60) continue;
                    const low = ownText.toLowerCase();
                    if (['harga','price','judul penawaran','title','deskripsi','description'].includes(low)) continue;
                    candidates.push({t: ownText, dx: left - r.right});
                }
                candidates.sort((a, b) => a.dx - b.dx);
                const label = candidates.length ? candidates[0].t : `Field${idx}`;
                if (!inp.id) {
                    inp.id = 'g2g_txt_' + idx + '_' + Date.now();
                }
                results.push({label: label, id: inp.id});
            });
            return results;
        }""")
    except Exception as e:
        add_log(f"[G2G] Scan text inputs gagal: {str(e)[:80]}")
        return

    if not fields:
        return

    labels = [f["label"] for f in fields]
    add_log(f"[G2G] Field text kosong -> fill '1': {labels}")

    for f in fields:
        lbl = f["label"]
        try:
            loc = page.locator(f"#{f['id']}").first
            loc.click()
            loc.fill("1")
            # Blur supaya Vue model commit value sebelum submit.
            page.keyboard.press("Tab")
        except Exception as e:
            add_log(f"[G2G] Gagal isi text field '{lbl}': {str(e)[:60]}")


# ===================== OPTION SELECTOR =====================
def _g2g_select_option(page, label_text, option_value):
    """Klik trigger 'Silakan Pilih' untuk field `label_text`, lalu pilih option
    yang match dengan `option_value` di panel yang muncul.
    """
    add_log(f"[G2G] Isi {label_text}: {option_value}")

    # Cari trigger di dekat label. Dua varian: "Silakan Pilih" div (attribute
    # dropdown) atau button.g-btn-select dengan expand_more icon (Waktu pengiriman).
    label_lit = _xpath_literal(label_text)
    trigger = None
    for sel in [
        # container yang punya label-text + Silakan Pilih
        f"xpath=//*[contains(normalize-space(.),{label_lit})]/following::*[normalize-space(.)='Silakan Pilih'][1]",
        f"xpath=//*[contains(normalize-space(.),{label_lit})]//*[normalize-space(.)='Silakan Pilih']",
        # button-based trigger (Waktu pengiriman, etc.)
        f"xpath=//*[contains(normalize-space(.),{label_lit})]/following::button[contains(@class,'g-btn-select')][1]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=3000)
            trigger = loc
            break
        except Exception:
            continue
    if trigger is None:
        add_log(f"[G2G] Trigger untuk '{label_text}' tidak ketemu, skip")
        return

    try:
        trigger.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    trigger.click()
    smart_wait(page, 500, 900)

    opt_lit = _xpath_literal(option_value)
    # Prioritaskan item di dalam q-menu / q-virtual-scroll (dropdown G2G).
    option = None
    for sel in [
        f"xpath=//div[contains(@class,'q-menu') or contains(@class,'absolute-top')]//div[contains(@class,'q-item')][.//*[contains(@class,'q-item__section--main')][normalize-space()={opt_lit}]]",
        f"xpath=//div[contains(@class,'q-virtual-scroll__content')]//div[contains(@class,'q-item__section--main')][normalize-space()={opt_lit}]",
        f"xpath=//div[contains(@class,'q-item__section--main')][normalize-space()={opt_lit}]",
        f"xpath=//*[@role='option' and normalize-space()={opt_lit}]",
        f"xpath=//div[contains(@class,'q-radio__label')][normalize-space()={opt_lit}]",
        f"xpath=//*[normalize-space()={opt_lit}]",
    ]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=3000)
            option = loc
            break
        except Exception:
            continue
    if option is None:
        # Fallback: scroll virtual list (opsi mungkin belum render) dan coba lagi.
        _g2g_scroll_virtual_list(page)
        page.wait_for_timeout(500)
        for sel in [
            f"xpath=//div[contains(@class,'q-virtual-scroll__content')]//div[contains(@class,'q-item__section--main')][normalize-space()={opt_lit}]",
            f"xpath=//div[contains(@class,'q-item__section--main')][normalize-space()={opt_lit}]",
        ]:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=2000)
                option = loc
                break
            except Exception:
                continue
    if option is None:
        add_log(f"[G2G] Opsi '{option_value}' untuk field '{label_text}' tidak ketemu, skip")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return

    try:
        option.click()
    except Exception as e:
        add_log(f"[G2G] Klik opsi '{option_value}' gagal: {str(e)[:80]}")
    smart_wait(page, 500, 900)


# ===================== PUBLIC ENTRIES =====================
def scrape_form_options(game_name_g2g):
    """Buka G2G sell URL, pilih produk, scrape Silakan-Pilih panels.
    Return: dict / {} / None (sama semantik dgn GM version)."""
    add_log("[G2G] Scrape form options dari G2G (pertama kali)...")
    with sync_playwright() as p:
        page = None
        try:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{_get_chrome_debug_port()}", timeout=10000)
            context = get_or_create_context(browser)
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()

            page.goto(G2G_CREATE_URL, wait_until="networkidle", timeout=30000)
            _g2g_set_worker_tab_title(page)
            smart_wait(page, 3000, 5000)

            _select_g2g_game(page, game_name_g2g)
            _g2g_click_lanjutkan(page)

            return _g2g_scrape_form_options(page)

        except Exception as e:
            add_log(f"[G2G] Gagal scrape form options: {str(e)[:100]}")
            return None
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


def create_listing(game_name_g2g, title, deskripsi, harga, field_mapping, image_urls,
                   image_future=None):
    """G2G: full flow isi form & submit. Return (berhasil, error_message, added_count).
    image_urls = list URL imgur (kalau source bukan imgur, list kosong -> skip image step).
    added_count = jumlah URL yang benar2 ter-add ke form (max 10).

    `image_future` (optional): future yg resolve ke (paths, urls, is_imgur).
    G2G khusus pakai urls (bukan file). Future di-resolve tepat sebelum step
    paste URL di form. Skip step kalau source bukan imgur (is_imgur False)."""
    added = 0
    with sync_playwright() as p:
        page = None
        try:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{_get_chrome_debug_port()}", timeout=10000)
            context = get_or_create_context(browser)
            context.set_default_timeout(60000)
            context.set_default_navigation_timeout(60000)
            page = context.new_page()

            add_log("[G2G] Buka halaman create offer...")
            # domcontentloaded return cepat; networkidle ketahan tracking pixels (~20s).
            page.goto(G2G_CREATE_URL, wait_until="domcontentloaded", timeout=30000)
            _g2g_set_worker_tab_title(page)
            # Tunggu trigger "Pilih produk" interaktif, tanpa sleep tambahan.
            try:
                page.locator(
                    "xpath=//*[contains(normalize-space(.),'Pilih produk')][self::button or @role='button']"
                    " | //button[.//span[contains(normalize-space(),'Pilih produk')]]"
                ).first.wait_for(state="visible", timeout=20000)
            except Exception:
                add_log("[G2G] Trigger 'Pilih produk' tidak terdeteksi 20s, lanjut coba")

            # 1. Pilih produk (game)
            _select_g2g_game(page, game_name_g2g)
            _g2g_click_lanjutkan(page)

            # 2. Isi semua Silakan Pilih dari AI mapping
            for field_label, value in field_mapping.items():
                if value is None:
                    continue
                _g2g_select_option(page, field_label, value)
                smart_wait(page, 400, 900)

            # 3. Title
            add_log("[G2G] Isi Title...")
            title_filled = False
            for sel in [
                "xpath=//*[contains(normalize-space(),'Judul') or contains(normalize-space(),'Title')]/following::input[contains(@class,'q-field__native')][1]",
                "input.q-field__native[placeholder*='udul' i]",
                "input.q-field__native[placeholder*='itle' i]",
            ]:
                try:
                    loc = page.locator(sel).first
                    loc.wait_for(state="visible", timeout=3000)
                    loc.fill(title)
                    title_filled = True
                    break
                except Exception:
                    continue
            if not title_filled:
                add_log("[G2G] Title input tidak ditemukan (selector perlu di-tune)")
            smart_wait(page, 400, 900)

            # 4. Description (textarea)
            add_log("[G2G] Isi Description...")
            desc_filled = False
            for sel in [
                "xpath=//*[contains(normalize-space(),'Deskripsi') or contains(normalize-space(),'Description')]/following::textarea[1]",
                "textarea.q-field__native",
                "textarea",
            ]:
                try:
                    loc = page.locator(sel).first
                    loc.wait_for(state="visible", timeout=3000)
                    loc.fill(deskripsi)
                    desc_filled = True
                    break
                except Exception:
                    continue
            if not desc_filled:
                add_log("[G2G] Description textarea tidak ditemukan")
            smart_wait(page, 400, 900)

            # 5. Price - strip ke pure digits (Rp, titik, koma dibuang)
            price_clean = re.sub(r'[^0-9]', '', str(harga))
            # G2G minimum 40000 IDR - override kalau harga sumber lebih rendah
            try:
                if price_clean and int(price_clean) < 40000:
                    add_log(f"[G2G] Harga sumber Rp{price_clean} < Rp40000 minimum, override ke Rp40000")
                    price_clean = "40000"
            except (ValueError, TypeError):
                pass
            add_log(f"[G2G] Isi Price: {price_clean}")
            price_filled = False
            for sel in [
                "xpath=//*[contains(normalize-space(),'Harga')]/following::input[contains(@class,'q-field__native')][1]",
                "xpath=//*[contains(normalize-space(),'Price')]/following::input[contains(@class,'q-field__native')][1]",
                "input.q-field__native[type='number']",
            ]:
                try:
                    loc = page.locator(sel).first
                    loc.wait_for(state="visible", timeout=3000)
                    loc.fill(price_clean)
                    price_filled = True
                    break
                except Exception:
                    continue
            if not price_filled:
                add_log("[G2G] Price input tidak ditemukan")
            smart_wait(page, 400, 900)

            # Resolve image future (async download pattern). G2G pakai URL
            # (bukan file), jadi kita butuh image_urls + is_imgur. Skip step
            # kalau source bukan imgur (is_imgur False) - sama behavior legacy.
            if image_future is not None:
                try:
                    _, resolved_urls, resolved_is_imgur = resolve_image_future(image_future)
                    image_urls = resolved_urls if resolved_is_imgur else []
                except RuntimeError as e:
                    return False, str(e), added

            # 8. Input image URLs one-by-one + klik "Tambah media". Max 10.
            # G2G butuh direct image URL (imgur i.imgur.com/xxx.jpeg) per slot,
            # bukan upload file. Tiap URL: fill input placeholder="https://",
            # tunggu tombol "Tambah media" enable, klik, ulangi.
            if image_urls:
                urls_to_add = image_urls[:10]
                add_log(f"[G2G] Input {len(urls_to_add)} image URL (dari {len(image_urls)} total, max 10)...")

                input_sel = (
                    "xpath=//input[@placeholder='https://' and contains(@class,'q-field__native')]"
                )
                btn_sel = (
                    "xpath=//button[.//div[normalize-space(.)='Tambah media']]"
                )

                # Setelah Tambah di-klik, slot lama "locked" (berisi URL) dan slot
                # baru muncul di bawah - input kosong baru dengan UUID id beda.
                # Kita target EMPTY visible input tiap iterasi (bisa first / last,
                # asalkan value-nya kosong).
                def _pick_empty_input():
                    for inp in page.locator(input_sel).all():
                        try:
                            if not inp.is_visible():
                                continue
                            if not (inp.input_value(timeout=500) or ""):
                                return inp
                        except Exception:
                            continue
                    return None

                # Find Tambah button sibling of the given input (same form row).
                # Fallback: last visible button dengan label "Tambah media".
                def _pick_add_button():
                    btns = page.locator(btn_sel).all()
                    for b in reversed(btns):
                        try:
                            if b.is_visible():
                                return b
                        except Exception:
                            continue
                    return None

                total_urls = len(urls_to_add)
                for idx, url in enumerate(urls_to_add, start=1):
                    is_last = (idx == total_urls)
                    try:
                        inp = _pick_empty_input()
                        if inp is None:
                            add_log(f"[G2G] Input kosong tidak ditemukan untuk URL {idx}, stop")
                            break
                        # Set value via JS + dispatch input/change (Vue reactivity
                        # synchronous, ndak perlu sleep setelah event).
                        inp.evaluate(
                            "(el, val) => {"
                            "  const setter = Object.getOwnPropertyDescriptor("
                            "    window.HTMLInputElement.prototype, 'value').set;"
                            "  setter.call(el, val);"
                            "  el.dispatchEvent(new Event('input', {bubbles:true}));"
                            "  el.dispatchEvent(new Event('change', {bubbles:true}));"
                            "}",
                            url,
                        )

                        # Last URL: skip klik Tambah media (bikin slot kosong baru
                        # yg trigger validation error "Kolom ini wajib diisi").
                        if is_last:
                            added += 1
                            add_log(f"[G2G] Isi URL terakhir {idx}/{total_urls}: {url[:70]}")
                            continue

                        btn = _pick_add_button()
                        if btn is None:
                            add_log(f"[G2G] Tombol Tambah media tidak ditemukan URL {idx}, skip")
                            continue
                        # Poll tombol enable: 50ms interval, max 800ms.
                        enabled = False
                        for _ in range(16):
                            try:
                                is_disabled = btn.get_attribute("disabled")
                                aria_dis = btn.get_attribute("aria-disabled")
                                if (is_disabled is None) and (aria_dis != "true"):
                                    enabled = True
                                    break
                            except Exception:
                                pass
                            page.wait_for_timeout(50)
                        if not enabled:
                            add_log(f"[G2G] Tombol Tambah media tidak enable URL {idx}, skip")
                            continue
                        # Count input sebelum klik, buat poll slot baru muncul.
                        try:
                            prev_count = page.locator(input_sel).count()
                        except Exception:
                            prev_count = 0
                        btn.click()
                        added += 1
                        add_log(f"[G2G] Tambah media {idx}/{total_urls}: {url[:70]}")
                        # Active poll: tunggu slot baru render (count nambah),
                        # 50ms interval, max 800ms.
                        for _ in range(16):
                            try:
                                if page.locator(input_sel).count() > prev_count:
                                    break
                            except Exception:
                                pass
                            page.wait_for_timeout(50)
                    except Exception as e:
                        add_log(f"[G2G] Gagal input URL {idx}: {str(e)[:80]}")
                        continue
                add_log(f"[G2G] Total image URL berhasil ditambah: {added}/{total_urls}")

            # 8.5. Fill text input kosong (Primogem, gem count, dll) via AI+title.
            _g2g_fill_text_inputs_with_ai(page, title)

            # 9. Pilih radio berdasarkan label text (Quasar .q-radio).
            def _click_radio(label_text):
                lit = _xpath_literal(label_text)
                for sel in [
                    f"xpath=//div[contains(@class,'q-radio')][.//div[contains(@class,'q-radio__label')][normalize-space()={lit}]]",
                    f"xpath=//div[contains(@class,'q-radio__label')][normalize-space()={lit}]",
                    f"xpath=//label[.//*[contains(@class,'q-radio__label')][normalize-space()={lit}]]",
                ]:
                    try:
                        loc = page.locator(sel).first
                        loc.wait_for(state="visible", timeout=3000)
                        try:
                            loc.scroll_into_view_if_needed(timeout=1500)
                        except Exception:
                            pass
                        loc.click()
                        add_log(f"[G2G] Pilih radio: {label_text}")
                        smart_wait(page, 400, 800)
                        return True
                    except Exception:
                        continue
                add_log(f"[G2G] Radio '{label_text}' tidak ketemu")
                return False

            # Pengiriman manual
            _click_radio("Pengiriman manual")

            # Waktu pengiriman -> 1 jam
            _g2g_select_option(page, "Waktu pengiriman", "1 jam")
            smart_wait(page, 400, 800)

            # Global (cakupan)
            _click_radio("Global")

            # 10. Terbitkan
            add_log("[G2G] Klik Terbitkan...")
            publish_btn = None
            for sel in [
                "xpath=//button[.//*[normalize-space()='Terbitkan']]",
                "xpath=//button[normalize-space()='Terbitkan']",
                "xpath=//button[contains(normalize-space(),'Terbitkan')]",
            ]:
                try:
                    loc = page.locator(sel).first
                    loc.wait_for(state="visible", timeout=5000)
                    publish_btn = loc
                    break
                except Exception:
                    continue
            if publish_btn is None:
                return False, "Tombol Terbitkan tidak ditemukan", added

            try:
                publish_btn.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            publish_btn.click()
            add_log("[G2G] Terbitkan di-klik, tunggu popup sukses...")

            # Sukses = popup "Penawaran kamu sudah di..." muncul (q-dialog dengan
            # check_circle + tombol 'Tambah penawaran baru' / 'Atur Produk').
            # Atau tab keburu ditutup = juga sukses.
            popup_sel = (
                "xpath=//div[contains(@class,'q-dialog')]"
                "[.//i[contains(@class,'text-positive') and normalize-space()='check_circle']]"
                "[.//button[.//*[normalize-space()='Atur Produk']]]"
            )
            try:
                page.locator(popup_sel).first.wait_for(state="visible", timeout=30000)
                add_log("[G2G] Popup sukses muncul, tutup tab 1s lagi")
                page.wait_for_timeout(1000)
                return True, None, added
            except Exception as e:
                if "closed" in str(e).lower():
                    add_log("[G2G] Tab tertutup sebelum popup cek -> sukses")
                    return True, None, added
                # Popup tidak muncul - cek error di form
                try:
                    err_locs = page.locator(".text-negative, .q-field__messages, [role='alert']").all()
                    msgs = []
                    for l in err_locs:
                        try:
                            if not l.is_visible():
                                continue
                            t = (l.inner_text(timeout=800) or "").strip()
                            if t and 5 < len(t) < 200 and t not in msgs:
                                msgs.append(t)
                        except Exception:
                            continue
                    if msgs:
                        return False, f"Form error: {' | '.join(msgs[:3])[:150]}", added
                except Exception:
                    pass
                return False, "Popup sukses tidak muncul setelah Terbitkan", added

        except Exception as e:
            pesan = str(e)
            if "Timeout" in pesan:
                indo_error = "Waktu habis, elemen tidak ditemukan"
            elif "net::" in pesan:
                indo_error = "Gagal membuka halaman, cek koneksi"
            else:
                indo_error = f"Error: {pesan[:100]}"
            add_log(f"[G2G] Gagal: {indo_error}")
            return False, indo_error, added

        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass


def cache_looks_bogus(cache_dict):
    """Detect cache G2G yang ke-isi hasil scrape lama yang salah (label icon,
    dsb). Return True kalau ada key yang mencurigakan."""
    if not isinstance(cache_dict, dict) or not cache_dict:
        return False
    bad_keys = {"expand_more", "expand_less", "arrow_drop_down",
                "arrow_drop_up", "close", "search"}
    for k in cache_dict.keys():
        if str(k).strip().lower() in bad_keys:
            return True
    return False


# ===================== ADAPTER ENTRY =====================
def run(sheet, baris_nomor, worker_id, *, game_name, description, title, harga,
        field_mapping, image_paths=None, image_urls=None,
        raw_image_url=None, is_imgur=False, image_future=None):
    """Adapter entry dipanggil orchestrator. G2G quirks:
    - URL gambar (raw) di-obfuscate & prepend ke description (lolos auto-delete).
    - Image URL field di form G2G HANYA diisi kalau source imgur. Source lain
      (gdrive, postimg, dll.) G2G reject sebagai "URL tidak valid", jadi skip.
    Return (ok: bool, k_line: str)."""
    _worker_local.worker_id = f"{worker_id}-G2G"

    final_desc = description or ""
    if raw_image_url:
        obf = _obfuscate_image_url(raw_image_url)
        final_desc = f"Full Screenshot Detail: {obf}\n{final_desc}".rstrip()

    if image_future is not None:
        # Async mode: create_listing handle resolve + filter is_imgur internally.
        ok, err, added = create_listing(game_name, title, final_desc, harga,
                                        field_mapping or {}, None,
                                        image_future=image_future)
    else:
        # Legacy sync: hanya feed URL ke form kalau source imgur (whitelisted).
        urls_for_form = image_urls if (is_imgur and image_urls) else []
        ok, err, added = create_listing(game_name, title, final_desc, harga,
                                        field_mapping or {}, urls_for_form)
    ts = datetime.now().strftime("%d %b, %y | %H:%M")
    if ok:
        if added > 0:
            return True, f"✅ G2G | {added} images uploaded | {ts}"
        return True, f"✅ G2G | image URL in description | {ts}"
    return False, f"❌ G2G | {(err or 'unknown')[:80]}"
