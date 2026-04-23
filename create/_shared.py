"""create/_shared.py - utilitas generic untuk semua market adapter.

Phase 1: fungsi pure (no module-state dep) - xpath_literal, obfuscate_image_url,
scrape_imgur/postimg/gdrive.

Phase 2: fungsi yang butuh akses log + temp dir + gemini model. Dependency
di-inject dari bot_create.py via `inject_runtime(...)` saat `_bind_ctx`.
Fungsi di sini pakai callback (`_log`, `_get_temp_dir`, dll.) - tidak import
langsung dari bot_create supaya tidak circular.

Digunakan oleh:
- bot_create.py (orchestrator)
- create/GM.py, create/G2G.py dst. (future phase)
"""

import os
import re
import json
import random
import threading
import requests
from bs4 import BeautifulSoup

from shared import call_with_timeout, TimeoutHangError


# ===================== MULTI-WORKER THREAD LOCAL =====================
# Single global dipakai oleh semua modul (bot_create + create/*). Dibedah
# dari threading.local biar log prefix [WORKER N] konsisten cross-module.
_worker_local = threading.local()


# ===================== RUNTIME DEPENDENCY INJECTION =====================
# Di-set dari bot_create._bind_ctx(). Sebelum inject, fungsi yang butuh ini
# akan fallback ke print/None (pada fase awal startup). Getter callback
# dipakai untuk value yang bisa berubah (mis. gemini_model lazy-init).
_deps = {
    "log": None,                     # callable(msg) -> None
    "worker_temp_dir": None,         # callable() -> str
    "prepare_worker_temp_dir": None, # callable() -> str
    "gemini_model": None,            # callable() -> model | None
    "chrome_debug_port": None,       # callable() -> int | None
    "chrome_cdp_url": None,          # callable() -> str | None
}


def inject_runtime(**kwargs):
    """Bot_create panggil ini di _bind_ctx untuk expose log + temp dir + gemini
    model + chrome port ke modul shared & semua market adapter. Nilai callback
    (fn) supaya nilai terkini ke-refresh tiap akses.

    Accepted keys: log, worker_temp_dir, prepare_worker_temp_dir, gemini_model,
    chrome_debug_port, chrome_cdp_url.
    """
    for k, v in kwargs.items():
        if k in _deps:
            _deps[k] = v


def _log(msg):
    fn = _deps.get("log")
    if fn:
        fn(msg)
    else:
        try:
            print(f"[CREATE-SHARED] {msg}")
        except Exception:
            pass


def _get_temp_dir():
    fn = _deps.get("worker_temp_dir")
    return fn() if fn else None


def _prepare_temp_dir():
    fn = _deps.get("prepare_worker_temp_dir")
    return fn() if fn else None


def _get_gemini_model():
    fn = _deps.get("gemini_model")
    return fn() if fn else None


def _get_chrome_debug_port():
    fn = _deps.get("chrome_debug_port")
    return fn() if fn else None


def _get_chrome_cdp_url():
    fn = _deps.get("chrome_cdp_url")
    return fn() if fn else None


# ===================== PAUSE-AWARE WAIT =====================
def smart_wait(page, min_ms, max_ms):
    """Wait random time. Toggle OFF tidak interrupt mid-Playwright - cek toggle
    terjadi di row boundary (proses_baris loop) bukan mid-click, supaya flow
    Playwright satu row tidak terpotong di tengah klik.
    """
    page.wait_for_timeout(random.randint(min_ms, max_ms))


# ===================== PLAYWRIGHT CONTEXT =====================
def get_or_create_context(browser):
    """Re-use existing browser context kalau ada (dari attached CDP session)
    supaya login session & cookies kebawa.
    """
    if browser.contexts:
        return browser.contexts[0]
    return browser.new_context()


# ===================== XPATH HELPER =====================
def xpath_literal(s):
    """Convert Python string ke XPath string literal yang aman dari quote.
    XPath 1.0 tidak support escape; kalau ada apostrof, harus pakai concat().
    """
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s:
        return f'"{s}"'
    # Ada keduanya -> split di setiap "'" dan gabung dengan "'" (literal kutip tunggal).
    parts = s.split("'")
    pieces = []
    for i, p in enumerate(parts):
        if p:
            pieces.append(f"'{p}'")
        if i < len(parts) - 1:
            pieces.append("\"'\"")
    return "concat(" + ", ".join(pieces) + ")"


# ===================== URL OBFUSCATION =====================
def obfuscate_image_url(url):
    """Format URL biar tidak kena auto-delete deskripsi marketplace.
    https://imgur.com/a/r99r18H        -> imgur .com/a/r99r18H
    https://drive.google.com/folders/x -> drive.google .com/folders/x
    (strip scheme + insert space SEBELUM TLD terakhir).
    """
    if not url:
        return ""
    s = re.sub(r'^https?://', '', url.strip())
    # Insert space sebelum TLD ('.com', '.net', dll) agar marketplace tidak
    # recognize sebagai domain & hapus otomatis.
    m = re.match(r'^(.+?)(\.[a-zA-Z]{2,})(/.*)?$', s)
    if m:
        path = m.group(3) or ""
        return f"{m.group(1)} {m.group(2)}{path}"
    return s


# ===================== IMAGE SCRAPERS (PURE) =====================
def scrape_imgur(album_url):
    """Scrape Imgur album -> list direct image URL (i.imgur.com/xxx.jpg)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    # /all memuat semua gambar di album via fragment page yg sama
    base = album_url.rstrip("/")
    candidate_urls = [base, base + "/all"] if not base.endswith("/all") else [base]

    html_parts = []
    for u in candidate_urls:
        try:
            r = requests.get(u, headers=headers, timeout=30)
            if r.status_code == 200 and r.text:
                html_parts.append(r.text)
        except Exception:
            continue

    if not html_parts:
        return []

    html = "\n".join(html_parts)
    urls = []

    # Pola 1: JSON "hash":"XXX" ... "ext":".jpg" (Imgur embed postDataJSON)
    for m in re.finditer(
        r'"hash"\s*:\s*"([a-zA-Z0-9_-]+)"[\s\S]{0,400}?"ext"\s*:\s*"(\.[a-zA-Z0-9]+)"',
        html,
    ):
        h, ext = m.group(1), m.group(2)
        if ext.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            urls.append(f"https://i.imgur.com/{h}{ext}")

    # Pola 2: plain URL (dengan atau tanpa scheme) di HTML
    for m in re.finditer(
        r'(?:https?:)?//i\.imgur\.com/([a-zA-Z0-9_-]{5,})\.(jpg|jpeg|png|gif|webp)',
        html,
        re.IGNORECASE,
    ):
        h, ext = m.group(1), m.group(2).lower()
        urls.append(f"https://i.imgur.com/{h}.{ext}")

    # Ekstrak album ID untuk di-skip (biar tidak ketangkap sebagai image hash)
    album_id_match = re.search(r'/a/([a-zA-Z0-9_-]+)', album_url)
    album_id = album_id_match.group(1) if album_id_match else ""

    # Dedupe by hash (base id), buang suffix thumbnail (s/b/m/l/h/t di akhir jika ada)
    seen_hash = set()
    out = []
    for u in urls:
        m = re.search(r'i\.imgur\.com/([a-zA-Z0-9_-]+)\.', u)
        if not m:
            continue
        h = m.group(1)
        # Skip: album ID (bukan image), atau hash = "removed"/"default"/dsb
        if h == album_id or h.lower() in ("removed", "default", "image", "404"):
            continue
        # Buang thumbnail suffix tunggal (Imgur thumb variants)
        base_h = re.sub(r'[sbmlht]$', '', h) if len(h) > 7 else h
        if base_h in seen_hash:
            continue
        seen_hash.add(base_h)
        out.append(u)

    # Imgur postDataJSON urutan kebalikan dari tampilan album - reverse supaya
    # gambar paling atas di album jadi yang pertama di-download.
    out.reverse()
    return out[:20]


def scrape_postimg(gallery_url):
    """Scrape postimg.cc gallery/album -> list direct image URL."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    response = requests.get(gallery_url, headers=headers, timeout=30)
    soup = BeautifulSoup(response.text, "html.parser")
    images = []
    for img in soup.select("a.thumbnail img, img[src*='postimg.cc'], img[src*='i.postimg.cc']"):
        src = img.get("src") or img.get("data-src", "")
        if src and "postimg.cc" in src and not src.endswith("_t.jpg"):
            images.append(src)
    seen = set()
    out = []
    for u in images:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:20]


def scrape_gdrive(folder_url):
    """Scrape Google Drive folder publik -> list direct-download URL.
    Pakai embeddedfolderview yang return HTML daftar file (perlu folder 'Anyone with link').
    """
    m = re.search(r'/folders/([a-zA-Z0-9_-]+)', folder_url)
    if not m:
        return []
    folder_id = m.group(1)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    }
    view_url = f"https://drive.google.com/embeddedfolderview?id={folder_id}#grid"
    try:
        r = requests.get(view_url, headers=headers, timeout=30)
        html = r.text
    except Exception:
        return []

    seen = set()
    file_ids = []
    for mm in re.finditer(r'/file/d/([a-zA-Z0-9_-]+)', html):
        fid = mm.group(1)
        if fid and fid != folder_id and fid not in seen:
            seen.add(fid)
            file_ids.append(fid)

    return [f"https://drive.google.com/uc?export=download&id={fid}" for fid in file_ids[:20]]


# ===================== IMAGE DOWNLOADERS =====================
def download_images_with_urls(gambar_url):
    """Unified image prep: scrape URL list ONCE + download ke temp_images.
    Return (local_paths, image_urls, is_imgur).
    - GM pakai local_paths (file upload).
    - G2G pakai image_urls kalau is_imgur (URL langsung), else ({} = skip + URL
      prepend description di caller).
    """
    _prepare_temp_dir()

    is_imgur = False
    is_drive = False
    if "imgur.com/a/" in gambar_url:
        is_imgur = True
        try:
            image_urls = scrape_imgur(gambar_url)
        except Exception as e:
            _log(f"Gagal scrape Imgur: {e}")
            return [], [], True
    elif "postimg.cc/gallery/" in gambar_url or "postimg.cc/album/" in gambar_url:
        try:
            image_urls = scrape_postimg(gambar_url)
        except Exception as e:
            _log(f"Gagal scrape Postimg: {e}")
            return [], [], False
    elif "drive.google.com/drive/folders/" in gambar_url:
        is_drive = True
        try:
            image_urls = scrape_gdrive(gambar_url)
        except Exception as e:
            _log(f"Gagal scrape Google Drive: {e}")
            return [], [], False
    else:
        _log(f"Image source tidak dikenali: {gambar_url}")
        _log("Listing akan dibuat tanpa gambar")
        return [], [], False

    if not image_urls:
        _log("Tidak ada URL gambar ditemukan di album")
        return [], [], is_imgur

    temp_dir = _get_temp_dir()
    local_paths = []
    for i, url in enumerate(image_urls):
        try:
            r = requests.get(url, timeout=30,
                             headers={"User-Agent": "Mozilla/5.0"},
                             allow_redirects=True)
            if is_drive:
                ct = (r.headers.get("Content-Type", "") or "").lower()
                if "png" in ct: ext = "png"
                elif "gif" in ct: ext = "gif"
                elif "webp" in ct: ext = "webp"
                elif "jpeg" in ct or "jpg" in ct: ext = "jpg"
                elif "text/html" in ct:
                    _log(f"Gambar {i+1} butuh confirm (file besar di Drive), skip")
                    continue
                else: ext = "jpg"
            else:
                ext = url.split(".")[-1].split("?")[0].lower()
                if ext not in ["jpg","jpeg","png","gif","webp"]:
                    ext = "jpg"
            filename = os.path.join(temp_dir, f"img_{i+1:02d}.{ext}")
            with open(filename, "wb") as f:
                f.write(r.content)
            size_mb = os.path.getsize(filename) / (1024 * 1024)
            if size_mb > 5:
                _log(f"Gambar {i+1} ukuran {size_mb:.1f}MB > 5MB, skip")
                os.remove(filename)
                continue
            local_paths.append(filename)
            _log(f"Download gambar {i+1}/{len(image_urls)} ({size_mb:.1f}MB)")
        except Exception as e:
            _log(f"Gagal download gambar {i+1}: {e}")

    _log(f"Total gambar siap: {len(local_paths)} file / {len(image_urls)} URL")
    return local_paths, image_urls, is_imgur


def download_images(gambar_url):
    """Legacy downloader (GM-only). Return list of local paths."""
    # Pre-download: pastikan folder worker bersih - hindari sisa file dari run sebelumnya
    # tercampur (misal bot crash setelah download, lalu restart & dapat URL baru).
    _prepare_temp_dir()

    is_drive = False
    if "imgur.com/a/" in gambar_url:
        try:
            image_urls = scrape_imgur(gambar_url)
        except Exception as e:
            _log(f"Gagal scrape Imgur: {e}")
            return []
    elif "postimg.cc/gallery/" in gambar_url or "postimg.cc/album/" in gambar_url:
        try:
            image_urls = scrape_postimg(gambar_url)
        except Exception as e:
            _log(f"Gagal scrape Postimg: {e}")
            return []
    elif "drive.google.com/drive/folders/" in gambar_url:
        is_drive = True
        try:
            image_urls = scrape_gdrive(gambar_url)
        except Exception as e:
            _log(f"Gagal scrape Google Drive: {e}")
            return []
    else:
        _log(f"Image source tidak dikenali: {gambar_url}")
        _log("Listing akan dibuat tanpa gambar")
        return []

    if not image_urls:
        _log("Tidak ada URL gambar ditemukan di album")
        return []

    temp_dir = _get_temp_dir()  # sudah dipastikan ada + kosong di atas

    local_paths = []
    for i, url in enumerate(image_urls):
        try:
            r = requests.get(url, timeout=30,
                             headers={"User-Agent": "Mozilla/5.0"},
                             allow_redirects=True)
            # Tentukan ekstensi
            if is_drive:
                ct = (r.headers.get("Content-Type", "") or "").lower()
                if "png" in ct:
                    ext = "png"
                elif "gif" in ct:
                    ext = "gif"
                elif "webp" in ct:
                    ext = "webp"
                elif "jpeg" in ct or "jpg" in ct:
                    ext = "jpg"
                elif "text/html" in ct:
                    # Drive kembalikan HTML konfirmasi (file besar) -> skip
                    _log(f"Gambar {i+1} butuh confirm (file besar di Drive), skip")
                    continue
                else:
                    ext = "jpg"
            else:
                ext = url.split(".")[-1].split("?")[0].lower()
                if ext not in ["jpg", "jpeg", "png", "gif", "webp"]:
                    ext = "jpg"
            filename = os.path.join(temp_dir, f"img_{i+1:02d}.{ext}")
            with open(filename, "wb") as f:
                f.write(r.content)
            size_mb = os.path.getsize(filename) / (1024 * 1024)
            if size_mb > 5:
                _log(f"Gambar {i+1} ukuran {size_mb:.1f}MB > 5MB, skip")
                os.remove(filename)
                continue
            local_paths.append(filename)
            _log(f"Download gambar {i+1}/{len(image_urls)} ({size_mb:.1f}MB)")
        except Exception as e:
            _log(f"Gagal download gambar {i+1}: {e}")

    _log(f"Total gambar siap upload: {len(local_paths)}")
    return local_paths


def cleanup_temp_images(paths):
    """Hapus file hasil download + isi worker temp dir."""
    for f in paths:
        try:
            os.remove(f)
        except Exception:
            pass
    temp_dir = _get_temp_dir()
    if temp_dir and os.path.isdir(temp_dir):
        for fname in os.listdir(temp_dir):
            try:
                os.remove(os.path.join(temp_dir, fname))
            except Exception:
                pass


def extract_image_urls_for_g2g(gambar_url):
    """URL-only scrape untuk G2G (no download). Return list of direct URL.
    Hanya imgur yang return URL; sumber lain (postimg/gdrive/lain) return []
    -> G2G skip step upload image (per spec user).
    """
    if "imgur.com/a/" in gambar_url:
        try:
            urls = scrape_imgur(gambar_url)
            _log(f"[G2G] Extract {len(urls)} imgur URL (no download)")
            return urls
        except Exception as e:
            _log(f"[G2G] Gagal scrape Imgur URLs: {str(e)[:80]}")
            return []
    return []


# ===================== AI FIELD MAPPING =====================
def ai_map_fields(game_name_gm, title, form_options):
    """Panggil Gemini untuk mapping title -> form fields (GM-style prompt).
    Return dict {field: value}, sudah divalidasi terhadap form_options."""
    prompt = f"""You are a form-filling assistant for a game account marketplace.

Game: {game_name_gm}
Product title: {title}

Available form fields and their options:
{json.dumps(form_options, indent=2, ensure_ascii=False)}

Based on the product title, choose the most appropriate option for each field.
Rules:
- Analyze the title carefully for clues (server region, rank/level, account type, etc.)
- If a field has no relevant info in the title, choose "Others" if available, otherwise choose the most generic/middle option
- Return ONLY a valid JSON object
- No explanation, no markdown, no extra text whatsoever

Example output:
{{"Accounts": "End Game", "Server": "Asia", "Adventure Rank Level": "55+"}}
"""

    model = _get_gemini_model()
    response = call_with_timeout(
        fn=lambda: model.generate_content(prompt),
        timeout=60,
        name="gemini_map_fields"
    )
    text = response.text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    mapping = json.loads(text)

    validated = {}
    for field, value in mapping.items():
        if field not in form_options:
            continue
        opts = form_options[field]
        if value in opts:
            validated[field] = value
        else:
            if "Others" in opts:
                validated[field] = "Others"
                _log(f"[GM] AI pilih '{value}' untuk '{field}' -> invalid, fallback ke 'Others'")
            elif opts:
                validated[field] = opts[0]
                _log(f"[GM] AI pilih '{value}' untuk '{field}' -> invalid, fallback ke '{opts[0]}'")

    for f, v in validated.items():
        _log(f"[GM] AI: {f} -> {v}")

    return validated


def ai_map_fields_g2g(game_name_g2g, title, form_options):
    """G2G counterpart of ai_map_fields. Prompt sama, log prefix [G2G]."""
    prompt = f"""You are a form-filling assistant for a game account marketplace (G2G).

Game: {game_name_g2g}
Product title: {title}

Available form fields and their options:
{json.dumps(form_options, indent=2, ensure_ascii=False)}

Based on the product title, choose the most appropriate option for each field.
Rules:
- Analyze the title carefully for clues (server region, rank/level, account type, etc.)
- If a field has no relevant info in the title, choose the most generic/middle option
- Return ONLY a valid JSON object
- No explanation, no markdown, no extra text whatsoever

Example output:
{{"Server": "Asia", "Account Level": "55+"}}
"""

    model = _get_gemini_model()
    response = call_with_timeout(
        fn=lambda: model.generate_content(prompt),
        timeout=60,
        name="gemini_map_fields_g2g"
    )
    text = response.text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    mapping = json.loads(text)

    validated = {}
    for field, value in mapping.items():
        if field not in form_options:
            continue
        opts = form_options[field]
        if value in opts:
            validated[field] = value
        else:
            if opts:
                validated[field] = opts[0]
                _log(f"[G2G] AI pilih '{value}' untuk '{field}' -> invalid, fallback ke '{opts[0]}'")

    for f, v in validated.items():
        _log(f"[G2G] AI: {f} -> {v}")

    return validated


def ai_map_fields_combined(game_gm, game_g2g, title, form_options_gm, form_options_g2g):
    """Combined Gemini call untuk GM + G2G sekaligus (menghindari 2x
    concurrent API call yang sering timeout).

    Input:
      form_options_gm / form_options_g2g: dict | None | {} (sentinel game-tanpa-form)
        None = tidak perlu (market not in scope); {} = sentinel tidak ada field.

    Return: (mapping_gm, mapping_g2g) -- masing-masing dict {field:value}, sudah
      validated terhadap opsi yang tersedia. Return {} kalau market skip.
    """
    need_gm  = isinstance(form_options_gm, dict)  and bool(form_options_gm)
    need_g2g = isinstance(form_options_g2g, dict) and bool(form_options_g2g)

    if not (need_gm or need_g2g):
        return {}, {}

    sections = []
    if need_gm:
        sections.append(
            f"== GameMarket (GM) ==\n"
            f"Game: {game_gm}\n"
            f"Available fields (GM):\n{json.dumps(form_options_gm, indent=2, ensure_ascii=False)}"
        )
    if need_g2g:
        sections.append(
            f"== G2G ==\n"
            f"Game: {game_g2g}\n"
            f"Available fields (G2G):\n{json.dumps(form_options_g2g, indent=2, ensure_ascii=False)}"
        )

    expected_schema = {}
    if need_gm:  expected_schema["gm"]  = "{field: value, ...}"
    if need_g2g: expected_schema["g2g"] = "{field: value, ...}"

    prompt = f"""You are a form-filling assistant for game account marketplaces.

Product title: {title}

{chr(10).join(sections)}

Task: For EACH marketplace section above, choose the most appropriate option for
each field based on the product title.

Rules:
- Analyze the title carefully (server region, rank/level, account type, etc.)
- If a field has no relevant info in the title, prefer "Others" (if available) or
  the most generic/middle option for that field's options
- Return ONLY a valid JSON object with this exact shape:
  {json.dumps(expected_schema, ensure_ascii=False)}
- No explanation, no markdown, no extra text whatsoever

Example output:
{{"gm": {{"Accounts": "End Game", "Server": "Asia"}}, "g2g": {{"Server": "Asia", "Level": "55+"}}}}
"""

    model = _get_gemini_model()
    response = None
    last_err = None
    for attempt in range(3):
        try:
            response = call_with_timeout(
                fn=lambda: model.generate_content(prompt),
                timeout=120,
                name="gemini_map_fields_combined"
            )
            break
        except TimeoutHangError as e:
            last_err = e
            _log(f"[AI] Gemini timeout attempt {attempt+1}/3 (>120s)")
            continue
        except Exception as e:
            last_err = e
            _log(f"[AI] Gemini error attempt {attempt+1}/3: {str(e)[:80]}")
            continue
    if response is None:
        raise last_err if last_err else RuntimeError("Gemini combined call failed")

    text = response.text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    data = json.loads(text)

    def _validate(raw_map, form_opts, market_tag):
        validated = {}
        if not isinstance(raw_map, dict):
            return validated
        for field, value in raw_map.items():
            if field not in form_opts:
                continue
            opts = form_opts[field]
            if value in opts:
                validated[field] = value
            else:
                if market_tag == "GM" and "Others" in opts:
                    validated[field] = "Others"
                    _log(f"[GM] AI pilih '{value}' untuk '{field}' -> invalid, fallback ke 'Others'")
                elif opts:
                    validated[field] = opts[0]
                    _log(f"[{market_tag}] AI pilih '{value}' untuk '{field}' -> invalid, fallback ke '{opts[0]}'")
        for f, v in validated.items():
            _log(f"[{market_tag}] AI: {f} -> {v}")
        return validated

    mapping_gm = _validate(data.get("gm", {}), form_options_gm, "GM") if need_gm else {}
    mapping_g2g = _validate(data.get("g2g", {}), form_options_g2g, "G2G") if need_g2g else {}
    return mapping_gm, mapping_g2g


def ai_map_fields_multi(title, market_inputs):
    """Generic N-market form-mapping via 1 Gemini call.

    Input:
      market_inputs = [{"code":"GM", "game":"...", "form_options": dict|None|{}}, ...]
      Market dengan form_options None/{}/non-dict auto-skip (mapping {}).

    Return: {code: {field: value}} untuk SEMUA code di input (skipped = {}).
    """
    result = {code_entry["code"]: {} for code_entry in market_inputs}

    active = []
    skipped_summary = []
    for entry in market_inputs:
        code = entry.get("code")
        fo = entry.get("form_options")
        if isinstance(fo, dict) and fo:
            active.append({"code": code, "game": entry.get("game", ""), "form_options": fo})
        else:
            kind = "None" if fo is None else ("{}" if fo == {} else type(fo).__name__)
            skipped_summary.append(f"{code}={kind}")

    if skipped_summary:
        _log(f"[AI] Skip (form_options kosong/invalid): {', '.join(skipped_summary)}")
    if not active:
        _log("[AI] Tidak ada market dengan form_options valid -> AI call di-skip")
        return result
    _log(f"[AI] Gemini multi call untuk: {[m['code'] for m in active]}")

    sections = []
    for m in active:
        sections.append(
            f"== {m['code']} ==\n"
            f"Game: {m['game']}\n"
            f"Available fields ({m['code']}):\n"
            f"{json.dumps(m['form_options'], indent=2, ensure_ascii=False)}"
        )

    # Build CONCRETE example so Gemini tidak bingung dgn placeholder string.
    # Ambil field pertama dari tiap market sebagai sample.
    example_schema = {}
    for m in active:
        sample_fields = {}
        for field_name, opts_list in m["form_options"].items():
            if opts_list:
                sample_fields[field_name] = opts_list[0]
        example_schema[m["code"].lower()] = sample_fields

    market_keys = ", ".join(f'"{m["code"].lower()}"' for m in active)
    prompt = f"""You are a form-filling assistant for game account marketplaces.

Product title: {title}

{chr(10).join(sections)}

Task: For EACH marketplace section above, choose the most appropriate option for
each field based on the product title.

Rules:
- Analyze the title carefully (server region, rank/level, account type, etc.)
- If a field has no relevant info in the title, prefer "Others" (if available) or
  the most generic/middle option for that field's options
- Use the EXACT field names and EXACT option values shown in "Available fields".
- Top-level keys MUST be exactly: {market_keys} (lowercase, no other keys).
- Each top-level value MUST be a JSON OBJECT (not a string), mapping
  field_name -> chosen_option_value.
- Return ONLY valid JSON. Example of the required shape (values here are just
  placeholders — replace with your actual choices):
  {json.dumps(example_schema, ensure_ascii=False)}
- No explanation, no markdown, no extra text whatsoever.
"""

    model = _get_gemini_model()
    response = None
    last_err = None
    for attempt in range(3):
        try:
            response = call_with_timeout(
                fn=lambda: model.generate_content(prompt),
                timeout=120,
                name="gemini_map_fields_multi"
            )
            break
        except TimeoutHangError as e:
            last_err = e
            _log(f"[AI] Gemini timeout attempt {attempt+1}/3 (>120s)")
            continue
        except Exception as e:
            last_err = e
            _log(f"[AI] Gemini error attempt {attempt+1}/3: {str(e)[:80]}")
            continue
    if response is None:
        raise last_err if last_err else RuntimeError("Gemini multi call failed")

    text = response.text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    data = json.loads(text)
    _log(f"[AI] Gemini raw response: {json.dumps(data, ensure_ascii=False)[:500]}")

    def _validate(raw_map, form_opts, market_tag):
        validated = {}
        if not isinstance(raw_map, dict):
            _log(f"[{market_tag}] AI raw_map bukan dict (type={type(raw_map).__name__})")
            return validated
        if not raw_map:
            _log(f"[{market_tag}] AI raw_map kosong {{}}")
            return validated
        for field, value in raw_map.items():
            if field not in form_opts:
                _log(f"[{market_tag}] AI field '{field}' ndak ada di form_options "
                     f"(available: {list(form_opts.keys())})")
                continue
            opts = form_opts[field]
            if value in opts:
                validated[field] = value
            else:
                if market_tag == "GM" and "Others" in opts:
                    validated[field] = "Others"
                    _log(f"[GM] AI pilih '{value}' untuk '{field}' -> invalid, fallback ke 'Others'")
                elif opts:
                    validated[field] = opts[0]
                    _log(f"[{market_tag}] AI pilih '{value}' untuk '{field}' -> invalid, fallback ke '{opts[0]}'")
        for f, v in validated.items():
            _log(f"[{market_tag}] AI: {f} -> {v}")
        return validated

    # Case-insensitive lookup: Gemini kadang kembalikan "ZEUS" (uppercase)
    # padahal schema pakai lowercase. Normalize sekali.
    data_norm = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(k, str):
                data_norm[k.lower()] = v
    for m in active:
        code = m["code"]
        key = code.lower()
        raw = data_norm.get(key, {})
        if not raw:
            _log(f"[{code}] AI response tidak ada key '{key}' (actual keys: {list(data_norm.keys())})")
        result[code] = _validate(raw, m["form_options"], code)
    return result
