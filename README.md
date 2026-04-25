# Bot Manage Listing

Bot otomatis untuk manage listing di 10+ marketplace gaming (GM, G2G, PA, U7, ZEUS, GB, ELDO, IGV, Z2U, FP).
Satu aplikasi, 3 fungsi:

- **DELETE** — hapus listing berdasarkan trigger `PERLU DELETE` di Google Sheets
- **CREATE** — post listing baru berdasarkan trigger `PERLU POST`, dengan AI Gemini mapping dynamic form per game
- **DISKON** — update harga listing berdasarkan trigger `PERLU DISCOUNT`

Bot dikendalikan via Google Sheets sebagai single source of truth — user input data, bot auto-proses.

---

# Daftar Isi

- [Bagian 1 — Panduan User (Karyawan Kantor)](#bagian-1--panduan-user-karyawan-kantor)
  - [Instalasi pertama kali](#instalasi-pertama-kali)
  - [Operasional harian](#operasional-harian)
  - [Update bot](#update-bot)
  - [Troubleshooting user](#troubleshooting-user)
- [Bagian 2 — Panduan Developer](#bagian-2--panduan-developer)
  - [Arsitektur overview](#arsitektur-overview)
  - [Struktur folder](#struktur-folder)
  - [Config reference](#config-reference)
  - [Google Sheets LINK sheet](#google-sheets-link-sheet)
  - [Struktur tab game per row](#struktur-tab-game-per-row)
  - [Orchestrator flow](#orchestrator-flow)
  - [Flow bot_delete](#flow-bot_delete)
  - [Flow bot_create](#flow-bot_create)
  - [Flow bot_diskon](#flow-bot_diskon)
  - [Async image download](#async-image-download)
  - [Dev workflow — build & release](#dev-workflow--build--release)
  - [Troubleshooting developer](#troubleshooting-developer)
- [Bagian 3 — Reference](#bagian-3--reference)
  - [File structure tree](#file-structure-tree)
  - [Config fields](#config-fields)
  - [Timeout values](#timeout-values)
  - [Log lokasi](#log-lokasi)

---

# Bagian 1 — Panduan User (Karyawan Kantor)

## Instalasi pertama kali

Bot siap pakai dalam bentuk `.exe` — **tidak perlu install Python / git**.

### 1. Siapkan folder

Bikin folder baru, misal `C:\Bot_AI_Poster\`.

### 2. Copy file dari dev (diserahkan oleh admin)

File yang harus ada di folder:

| File | Keterangan | Sumber |
|------|------------|--------|
| `Bot Manage Listing.exe` | Binary aplikasi | Dari admin (dari GitHub Releases) |
| `update.bat` | Script auto-update ke versi terbaru | Dari admin |
| `icon.ico` | Icon window | Dari admin |
| `config.txt` | Konfigurasi (bikin manual, isi sesuai PC) | Bikin manual |
| `credentials.json` | Service account Google Sheets | Dari admin (unik per PC) |

### 3. Isi `config.txt`

```
INSTANCE_NAME=POSTER 1
SPREADSHEET_ID=1abc...xyz   # ID spreadsheet Google Sheets kamu
CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
CHROME_DEBUG_PORT=9222
CHROME_USER_DATA_DIR=C:\chrome-debug
GEMINI_API_KEY=AIza...   # API key Gemini (dari admin)
DISKON_MAX_WORKER=5
SHARED_POLLING_INTERVAL=60
LOG_RETENTION_DAYS=120
```

> **Detail tiap field**: lihat [Config fields](#config-fields) di Bagian Reference.

### 4. Double-click `Bot Manage Listing.exe`

Pertama kali jalan, bot akan:
- Buka Chrome debug di port 9222 (tab login ke marketplace)
- Connect ke Google Sheets
- Tampilkan UI dengan 3 toggle (DELETE / CREATE / DISKON) + live log

### 5. Login marketplace (SEKALI)

Di Chrome yang dibuka bot, login manual ke:
- GameMarket.gg (GM)
- G2G.com
- PlayerAuctions (PA)
- U7Buy (U7)
- ZeusX (ZEUS)
- GameBoost (GB)
- Eldorado.gg (ELDO)
- IGV / iMetaStore
- Z2U.com
- Funpay.com

Chrome profile tersimpan di folder `CHROME_USER_DATA_DIR` (default `C:\chrome-debug`) — **sekali login, session persist**. Bot pakai session ini untuk operasi selanjutnya.

## Operasional harian

### Cara pakai

1. **Buka bot** — double-click `Bot Manage Listing.exe`
2. **Biarkan jalan** di latar belakang. Bot auto-polling sheet tiap cycle
3. **Input data** di Google Sheets:
   - Isi data listing di tab game (Genshin Impact / Honkai Star Rail / dll)
   - Formula auto-detect `PERLU POST` / `PERLU DELETE` / `PERLU DISCOUNT` di kolom trigger
4. **Bot auto-proses** row yang trigger nyala, update status di kolom K

### UI Bot — 3 toggle

| Toggle | Fungsi |
|--------|--------|
| **DELETE** | Hapus listing di marketplace saat row trigger `PERLU DELETE` |
| **CREATE** | Post listing baru saat row trigger `PERLU POST` |
| **DISKON** | Update harga listing saat row trigger `PERLU DISCOUNT` |

Toggle bisa di-OFF kalau mau skip sementara (misal lagi test create saja).

### Indikator status per bot

Tiap card bot punya visual indikator di samping label:

| State | Indikator | Timer |
|-------|-----------|-------|
| **Running** | Pulsing dot solid (warna bot, breathing 1.4s) | Format `MM:SS` / `HH:MM:SS` di badge berwarna |
| **Standby** | Ping radar dot (warna bot, animasi pulse) | Format kalem `5s`, `45s`, `1m`, `2m 10s`, `1h`, `1h 20m` di samping kanan teks "Standby" |
| **Stopped** | (kosong, ndak ada indikator) | — |

Standby timer reset saat: toggle ON dari OFF, atau transisi running → standby (cycle selesai). Berhenti saat: toggle OFF atau saat running.

### Toast notification

Toast muncul di pojok kanan atas saat ada event sukses/gagal. Auto-dismiss setelah ~5 detik (klik × untuk close manual).

| Element | Style |
|---------|-------|
| Border kiri 3px | Hijau (`--success`) untuk sukses, merah (`--danger`) untuk error |
| Pill badge atas | `SUKSES` (centang icon) atau `ERROR` (X icon), animasi bouncy scale-in |
| Message text | Color `#b5bacb` (medium dim, antara teks utama dan log color), 12px |
| Close button | × di kanan |

Toast format konten **per bot**:

| Bot | Sukses | Gagal |
|-----|--------|-------|
| **DELETE** | `✅ [GM] Listing AAA101 berhasil dihapus!` (full log line) | `❌ [GM] Gagal: <error>` (full log line) |
| **CREATE** | `✅ [GM] ABC123 berhasil dipost 5 images uploaded!` (ringkas) | `❌ [GM] | <error>` (full line dari adapter) |
| **DISKON** | `✅ [Worker 3] [GM] ABC123 berhasil diskon 6.99 > 5.99` (per market)<br>`Baris 51 selesai. AB: ✅ All Good | AC: 25/04/26 11:30` (summary) | `❌ [Worker 3] [GM] ABC123 tidak dapat diproses!` (per market)<br>`Baris 51 selesai. AB: ❌ Gagal Total | AC: ...` (summary) |

**Catatan**: 
- CREATE toast format ringkas ≠ K column format (K column tetap full dengan timestamp + listing URL)
- DISKON per-market: 1 toast per market × N market aktif. Plus 1 summary toast di akhir cycle (kalau prefix mengandung ✅/❌ — `⚠️ Error Sebagian` ndak fire toast)
- Worker prefix `[Worker N]` muncul di DISKON karena bot diskon multi-worker paralel

### Live Log

Panel kanan tampilkan log real-time:
- `[APP] Bot Manage Listing start` — startup
- `[APP] Prescan LINK: DELETE=1/22(141) CREATE=1/5(35)` — prescan per cycle
- `[APP] Mulai cycle CREATE` — cycle dimulai
- `[CREATE] [IGV] Upload Product image OK: 3/3 sukses` — progress per market
- `[APP] Cycle CREATE selesai dalam 45.2s - 1 listing diproses` — cycle selesai

Toast notif di pojok (suara `notif.wav`) pas listing sukses/gagal.

## Update bot

Admin push update ke GitHub. Untuk update bot di PC kantor:

1. **Tutup** `Bot Manage Listing.exe` kalau lagi jalan
2. **Double-click `update.bat`**
3. Script tampilkan info versi terbaru + tanggal release dari GitHub API
4. Auto-download EXE terbaru, overwrite file lama
5. Launch ulang bot

Waktu update: ~30 detik. Output `update.bat`:

```
[1/3] Stop bot yang sedang jalan...
[2/3] Download update dari GitHub...
       Versi terbaru: v1.11.9
       Last update  : 2026-04-26 03:43
       Downloading EXE...
[==================] 100%
[3/3] Replace EXE dan launch...
UPDATE SELESAI
```

**Catatan**: window cmd auto-disable QuickEdit mode — user ndak akan stuck kalau ndak sengaja klik di window (pause issue Windows CMD biasa).

## Troubleshooting user

### Bot ndak mau buka

**Gejala**: Double-click EXE, window muncul sebentar lalu hilang.

**Cek**:
- `config.txt` ada di folder yang sama? Isi field-nya sudah benar?
- `credentials.json` ada? Format JSON valid?
- `CHROME_PATH` di config nunjuk ke path Chrome yang benar?

Kalau missing config, bot tampil popup error "Item berikut kurang" + list. Isi dulu lalu restart.

### Bot muncul tapi Chrome ndak terbuka

**Gejala**: Log "Chrome (port 9222) mati, restart..." terus menerus.

**Cek**:
- Antivirus/Firewall block Chrome debug? Whitelist Chrome.exe.
- `CHROME_USER_DATA_DIR` (default `C:\chrome-debug`) ada permission write?
- Port 9222 kepake app lain? Ganti di config ke 9223 / 9224.

### Google Sheets ndak connect

**Gejala**: Log `Sheets gagal connect` terus-menerus.

**Cek**:
- `SPREADSHEET_ID` di `config.txt` bener? ID = bagian URL sheet setelah `/d/`.
- Service account email di `credentials.json` sudah di-share ke spreadsheet (permission Editor)?
- Koneksi internet?

### Bot di-proses tapi toast error

**Gejala**: Listing terus gagal, toast `❌ GM | ...`.

**Cek**:
- Session Chrome udah login ke marketplace yg relevan? Login manual di tab Chrome yg dibuka bot.
- Listing kode duplikat? Cek di marketplace.
- Saldo / rate limit marketplace? Cek dashboard marketplace manual.
- Lihat log detail di folder `log/app_log_YYYY-MM-DD.txt`

### Bot `update.bat` gagal

- "Download gagal" — cek internet + URL: https://github.com/hendrowiakto/tbg-poster/releases/latest
- "Gagal replace EXE" — bot masih jalan, tutup dulu window
- Rollback: download EXE versi lama dari GitHub Releases, rename jadi `Bot Manage Listing.exe`, overwrite

---

# Bagian 2 — Panduan Developer

## Arsitektur overview

```
┌─────────────────────────────────────────────────────────┐
│                    main.py (Entry)                      │
│  - Config validate, BotContext init                     │
│  - Launch Chrome + connect Sheets                       │
│  - Spawn daemons: log rotation, chrome monitor          │
│  - Spawn orchestrator loop                              │
│  - Spawn WebviewApp (UI)                                │
└─────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────┐
│            orchestrator_loop (main.py)                  │
│  Priority sequential cycle: DELETE > CREATE > DISKON    │
│                                                         │
│  While not stop:                                        │
│   1. prescan_link()  (1 batch_get LINK!A+C+D+E)         │
│   2. for bot in ["delete","create","diskon"]:           │
│        if toggle ON & ada kerjaan:                      │
│          bot.run_one_cycle(ctx)  -> n (row processed)   │
│          if n > 0: break (restart cycle dari delete)    │
│   3. cleanup_tabs() antar bot cycle                     │
│   4. idle sleep backoff (30s → 600s) kalau semua idle   │
└─────────────────────────────────────────────────────────┘
          │                │               │
          ▼                ▼               ▼
   bot_delete.py    bot_create.py   bot_diskon.py
   (DELETE)         (CREATE)        (DISKON)
          │                │               │
          └────────┬───────┴───────┬───────┘
                   │               │
                   ▼               ▼
          Playwright    create/*.py (adapter per market)
          + Chrome CDP  GM / G2G / PA / U7 / ZEUS / GB /
                        ELDO / IGV
```

**Design principle**:
- **Shared infrastructure via `shared.py` → `BotContext`** (logger, chrome, sheets, stats, toggle, progress). 3 bot ndak duplicate.
- **Orchestrator single-entry** di `main.py` — bot dipanggil sequential, ndak ada 2 bot barengan (cegah race di Chrome).
- **Per-market adapter pluggable** di `create/*.py` — tambah market baru = tambah 1 file.
- **Google Sheets = single source of truth**. Bot ndak punya state lokal kecuali cache sementara.

## Struktur folder

```
C:\Bot_AI_Poster\
├── main.py                    # Entry point + orchestrator loop
├── shared.py                  # BotContext + Config + Logger + Chrome + Sheets + Stats
├── bot_delete.py              # Bot DELETE (10 market inline)
├── bot_create.py              # Bot CREATE (orchestrator + dynamic adapter)
├── bot_diskon.py              # Bot DISKON (10 market inline)
├── webview_app.py             # UI webview + HTML bridge
├── Bot Manage Listing.html    # UI React (embedded)
├── create/                    # Per-market adapter (dipakai bot_create)
│   ├── __init__.py
│   ├── _shared.py             # Shared helpers adapter
│   ├── GM.py                  # GameMarket
│   ├── G2G.py                 # G2G
│   ├── PA.py                  # PlayerAuctions
│   ├── U7.py                  # U7Buy
│   ├── ZEUS.py                # ZeusX
│   ├── GB.py                  # GameBoost
│   ├── ELDO.py                # Eldorado
│   ├── IGV.py                 # IGV / iMetaStore
│   └── README.md              # Adapter reference
├── marketbackup/              # Backup adapter sebelum refactor
├── log/                       # Log harian (app_log_YYYY-MM-DD.txt)
├── temp_images/               # Cache gambar download (auto-cleanup)
├── config.txt                 # Config runtime
├── credentials.json           # Service account Google Sheets
├── VERSION.txt                # Version current (1 baris versi, 1 baris tanggal)
├── icon.ico
├── boys_gaming.gif            # Tab "keeper" Chrome
├── notif.wav                  # Suara toast UI
├── stats.txt                  # Stats counter sukses/gagal per market
├── build_exe.bat              # PyInstaller build
├── release.bat                # Commit + build + push + upload ke GitHub Releases
├── update.bat                 # Download latest EXE dari GitHub Releases
├── install_dependencies.bat   # pip install dependency (pertama kali di dev PC)
└── README.md                  # Dokumen ini
```

## Config reference

File `config.txt`. Auto-generated dari template kalau belum ada. Template source di `shared.py` constant `CONFIG_TEMPLATE`.

| Field | Tipe | Default | Keterangan |
|-------|------|---------|------------|
| `INSTANCE_NAME` | str | (kosong) | Suffix di title window per PC. Misal "POSTER 1". Kosong = tanpa suffix |
| `SPREADSHEET_ID` | str | `ISI_ID_SPREADSHEET_DISINI` | ID spreadsheet Google Sheets. Dari URL bagian setelah `/d/` |
| `CHROME_PATH` | str | `C:\Program Files\Google\Chrome\Application\chrome.exe` | Path ke `chrome.exe` |
| `CHROME_DEBUG_PORT` | int | 9222 | Port debug Chrome. Ganti kalau bentrok |
| `CHROME_USER_DATA_DIR` | str | `C:\chrome-debug` | Profile Chrome terpisah (session login marketplace) |
| `GEMINI_API_KEY` | str | `ISI_API_KEY_GEMINI_DISINI` | API key Google Gemini (untuk form mapping bot_create) |
| `DISKON_MAX_WORKER` | int | 5 | Max parallel worker bot_diskon (1-10). Batas berapa row diskon per cycle |
| `SHARED_POLLING_INTERVAL` | int | 60 | (legacy, ndak dipakai orchestrator baru pakai idle backoff) |
| `LOG_RETENTION_DAYS` | int | 120 | Umur log file di `log/` sebelum auto-delete |

### Validasi

`validate_config()` di [shared.py:237](shared.py#L237) check:
- `SPREADSHEET_ID` not empty / not default
- `GEMINI_API_KEY` not empty / not default
- `credentials.json` file exists
- `CHROME_PATH` exists (kalau di-set)

Kalau ada yg missing, bot kasih popup error saat startup.

## Google Sheets LINK sheet

Sheet bernama **`LINK`** di spreadsheet = master directory tab game.

### Struktur

| Kolom | Isi | Formula |
|-------|-----|---------|
| **A** | Nama tab game (misal "Genshin Impact", "Honkai Star Rail") | Manual |
| **B** | (reserved, kosong) | - |
| **C** | Counter `PERLU DELETE` per tab game | `=COUNTIF('tab_name'!AI:AI, "PERLU DELETE")` atau via `MAP + INDIRECT + AI49` |
| **D** | Counter `PERLU POST` per tab game | Formula serupa |
| **E** | Counter `PERLU DISCOUNT` per tab game | Formula serupa |

### Contoh

```
A (nama)              | B | C | D   | E
Seven Deadly Sins     |   | 0 | 131 | 0
Mobile Legends BB     |   | 0 | 35  | 0
Arena Breakout        |   | 0 | 554 | 0
Honkai Star Rail      |   | 0 | 715 | 5
Wuthering Waves       |   | 1 | 246 | 2
Genshin Impact        |   | 0 | 93  | 3
```

### Formula counter (MAP + INDIRECT — recommended)

Di `C2` / `D2` / `E2`:

```
=MAP(A2:A, LAMBDA(sh,
  IF(sh="", "",
    IFERROR(
      LET(data, INDIRECT("'"&sh&"'!AI49"), IF(data="", "", data)),
      ""
    )
  )
))
```

Cell `AI49` di tiap tab game = formula `COUNTIF` yang ngitung trigger di kolom AI (PERLU DELETE) / AJ (PERLU POST) / AK (PERLU DISCOUNT).

**Keuntungan INDIRECT**: volatile function, re-evaluate setiap edit di sheet manapun → counter realtime (sub-second), ndak perlu Apps Script trigger.

### Prescan orchestrator

`prescan_link()` di [main.py:37](main.py#L37) baca LINK sheet 1x per cycle (1 batch_get `A2:A + C2:C + D2:D + E2:E`), lalu:

- **DELETE**: ambil **top-1 tab** dengan `C>0` (urut dari atas)
- **CREATE**: ambil **top-1 tab** dengan `D>0`
- **DISKON**: ambil **tab teratas sampai cumulative `sum(E) >= DISKON_MAX_WORKER`**, hard cap 5 tab

Alasan top-N=1 untuk delete/create: per cycle cuma proses 1 row, ndak perlu scan semua tab aktif (hemat bandwidth).

## Struktur tab game per row

Tiap tab game (Genshin Impact / dll) punya layout spesifik:

### Baris metadata

| Baris | Isi |
|-------|-----|
| **42** | Header label (opsional) |
| **43** | Game name per market (kolom O-Z). Misal "Genshin Impact" di kolom untuk IGV |
| **44** | Deskripsi per market (kolom O-Z) |
| **45** | Form options cache (auto-filled oleh bot, JSON) |
| **48** | Kode market (O-Z). Misal O48="GM", P48="G2G", Q48="PA", dst |
| **49** | Manage link per market (kolom O-Z). Link halaman manage listing di marketplace |

### Baris data

Mulai **baris 51** ke bawah. Tiap baris = 1 listing.

| Kolom | Isi | Tipe |
|-------|-----|------|
| **A** | Kode listing (unique ID) | str |
| **B** | Login name / Account name | str |
| **G** | Harga kolom G2G (USD) | number |
| **H** | Harga default (GM/PA/U7/ZEUS/GB/ELDO/IGV) | number |
| **I** | URL gambar album (imgur/gdrive/postimg) | URL |
| **J** | Title listing | str (max 144 char biasanya) |
| **K** | **Status multiline** per market (✅/❌ per line) | str (multiline) |
| **M** | Reserved (harus kosong untuk trigger PERLU POST valid) | str |
| **N** | Reserved (untuk PERLU DELETE) | str |
| **O-Z** | Checkbox per market (O=GM, P=G2G, dst) — TRUE = sudah selesai | bool |
| **AI** | Trigger `PERLU DELETE` (formula) | str |
| **AJ** | Trigger `PERLU POST` (formula) | str |
| **AK** | Trigger `PERLU DISCOUNT` (formula) | str |

### Formula trigger (contoh)

**Kolom AI (PERLU DELETE)**:
```
=IF(AND(N51<>"", M51<>"", COUNTIF(O51:Z51,TRUE)>0), "PERLU DELETE", "")
```

**Kolom AJ (PERLU POST)**:
```
=IF(AND(G51<>"", H51<>"", I51<>"", J51<>"", LEN(J51)<=150, K51="", M51="",
       OR(O51<>TRUE, P51<>TRUE, Q51<>TRUE, R51<>TRUE, T51<>TRUE, U51<>TRUE, V51<>TRUE)),
   "PERLU POST", "")
```

**Kolom AK (PERLU DISCOUNT)**:
```
=IF(AND(A51<>"", ISNUMBER(AD51), AD51>0,
       OR(AC51="", TODAY()-AC51>=30),
       COUNTIF(O51:X51, TRUE)>0),
   "PERLU DISCOUNT", "")
```

Trigger auto-ON kalau semua kondisi terpenuhi. Bot tidak perlu validasi ulang (trust trigger formula).

### K column format

Status multiline per market. Tiap line 1 market:

```
✅ All Good

✅ GM | 5 images uploaded | 24 Apr, 26 | 20:43 | https://gamemarket.gg/listing/abc
✅ G2G | 3 images uploaded | 24 Apr, 26 | 20:45
❌ PA | Selector timeout: Title input
✅ IGV | 1 images uploaded | 24 Apr, 26 | 20:46
```

- Prefix `✅ {CODE} |` = market sukses. Done detection baca `^✅ ([A-Z0-9]+) \|` regex ([bot_create.py](bot_create.py))
- Prefix `❌ {CODE} |` = market gagal. Row tetap ditandai untuk retry cycle berikut (kecuali K udah ter-replace timeout)
- Line pertama (opsional): `✅ All Good` / `⚠️ Error Sebagian` / `❌ Gagal Total` = summary header
- **SATU-satunya source of truth untuk done detection** (bukan kolom O-Z TRUE). Sebelumnya pakai O-Z, sekarang K column.

## Orchestrator flow

`orchestrator_loop()` di [main.py:143](main.py#L143):

```python
while not ctx.stop_event.is_set():
    # Auto-reconnect Sheets kalau spreadsheet None
    if ctx.sheets.spreadsheet is None:
        try: ctx.sheets.connect()
        except: pass  # retry next iteration (60s cooldown)

    snap = prescan_link(ctx)  # {"delete": [tab], "create": [tab], "diskon": [tabs]}
    processed = False

    for bot_name in ["delete", "create", "diskon"]:
        if stop_event: break
        if toggle OFF: continue
        if prescan snap empty untuk bot ini: continue

        # Inject prefetched tab list ke bot (1-shot, dipakai get_active_sheet_names)
        bot.set_prefetched_active_sheets(snap[bot_name])

        n = bot.run_one_cycle(ctx)  # 1 row for delete/create, N worker for diskon
        if n > 0:
            processed = True
            break  # restart cycle dari delete (priority C > D > E)

        cleanup_tabs()  # antar bot, bersihkan tab Chrome non-keeper

    if processed:
        idle_wait = 30  # reset backoff
        continue  # re-prescan dari awal

    # Semua idle → dynamic backoff 30s → +10s per iteration → cap 600s
    sleep(idle_wait)
    idle_wait = min(idle_wait + 10, 600)
```

**Priority**: `DELETE > CREATE > DISKON`. Setelah 1 cycle sukses (`n > 0`), balik ke atas — selalu cek `DELETE` dulu karena irreversible jadi priority tinggi.

**Idle backoff**: 30s → 40s → 50s → ... → 600s (10 menit). Reset ke 30s begitu ada kerjaan. User bisa tekan "Force Scan" di UI untuk skip idle wait.

## Flow bot_delete

File: [bot_delete.py](bot_delete.py). Market inline (10 function `delete_listing_*`) — ndak modular seperti create.

### run_one_cycle flow

```
1. _bind_ctx(ctx)                  # bind logger/chrome/sheets
2. scan_all_sheets(n=1)
   a. get_active_sheet_names()     # dari prescan atau batch_get LINK!A+C
   b. worksheet metadata cache     # cegah crash batch_get tab tanpa kolom AI
   c. fase 1: batch_get AI51:AI    # scan flag PERLU DELETE (payload ringan)
   d. fase 2: batch_get A:AI 1 tab # fetch full data tab pertama yg hit
3. Kalau ada hit, proses_baris:
   a. Loop kolom O-Z yang TRUE (market aktif)
   b. Spawn 1 thread per market (stagger 1s) -> delete_listing_{PLATFORM}()
   c. Market lock per-platform (cegah 2 tab market sama di Chrome)
   d. Thread join timeout 15 menit
4. Kalau sukses:
   - safe_update_cell(K, "FALSE") per kolom market (uncentang)
   - Status tracked di stats + log
```

### 10 market di-support

| Platform Code | Function | Flow singkat |
|---------------|----------|--------------|
| `GM` | `delete_listing_gm` | Goto → search kode → klik sampah → confirm Delete |
| `G2G` | `delete_listing_g2g` | Goto → search → klik titik tiga → Hapus → Konfirmasi |
| `PA` | `delete_listing_pa` | Goto → search → checkbox → Cancel → Confirm Selected |
| `ELDO` | `delete_listing_eldo` | Goto → search → delete icon → confirm |
| `Z2U` | `delete_listing_z2u` | Goto → search → checkbox → Delete → Submit |
| `ZEUS` | `delete_listing_zeus` | Goto → search → titik tiga → Cancel Offer / Chat (kalau sold) → Remove |
| `U7` | `delete_listing_u7` | Goto → search → checkbox Off Sale → Delete |
| `GB` | `delete_listing_gb` | Goto → search → checkbox → Delete 1 Account → Confirm |
| `IGV` | `delete_listing_igv` | Goto → search → Take offline → Confirm |
| `FP` | `delete_listing_fp` | Goto → Ctrl+F → click result → Edit → Delete → Confirm |

### Timeout per bot_delete

- Default action: 60s (PA: 30s)
- Default navigation: 60s (PA: 30s)
- Networkidle wait: 30s
- Wait element visible: 10s

## Flow bot_create

File: [bot_create.py](bot_create.py). Modular — pakai adapter di [create/](create/). Detail adapter di [create/README.md](create/README.md).

### run_one_cycle flow

```
1. _bind_ctx(ctx)
2. Phase 0: scan LINK!A/D → get active tab list (top-1 tab)
3. Phase 1: batch_scan_all_sheets(tabs)
   → 1 batch_get per tab: kode A + title J + harga G:J + trigger AJ + centang O-Z + catatan K
4. Phase 1.5: cari FIRST candidate row
   Iterate bottom-up tiap tab (row paling bawah dulu):
     - trigger AJ == "PERLU POST" ✓
     - For each market di O48:Z48 yg ada kode:
         - game di O43:Z43 ada
         - harga di HARGA_COL (H per market) ada
         - belum done di K column
         - belum centang O-Z
       → add ke markets_todo
     - Kalau markets_todo kosong → continue cari row lain
5. Phase 2: process row
   a. safe_update_cell(K, "ON WORKING") untuk lock row
   b. Start image download async (background thread, return Future)
   c. Cache form options per market (scrape_form_options) paralel threads
   d. AI mapping (ai_map_fields_multi) 1 Gemini call untuk semua market
   e. Spawn 1 thread per market → _run_market → mod.run(...)
      - image_future pass-through → adapter resolve saat sampai upload step
   f. Aggregate hasil → write K column final + centang O-Z per market sukses
6. cleanup_temp_images (pakai future.result kalau done)
```

### Per-row timeout

`t.join(timeout=600)` = **max 10 menit per row**. Kalau lewat:
- Track thread zombie (eventually exit sendiri via Playwright timeout)
- K column di-write `❌ Lebih dari batas timeout (10 menit) - batch di-clear`
- Cleanup_tabs setelah cycle → close sisa tab Chrome → zombie thread throw exception & exit

Per-market worst case: 10 menit / 8 market paralel ≈ cap normal per market.

### Async image download

Lihat [Async image download](#async-image-download).

## Flow bot_diskon

File: [bot_diskon.py](bot_diskon.py). Market inline seperti bot_delete.

### run_one_cycle flow

```
1. _bind_ctx(ctx) → read MAX_WORKER from config (1-10, default 5)
2. scan_all_sheets(n=MAX_WORKER)
   a. get_active_sheet_names() → tabs dengan E>0 (dari prescan cumulative budget)
   b. fase 1: batch_get AK51:AK tiap tab → hit PERLU DISCOUNT
   c. fase 2: batch_get A:AK tiap tab yg hit → ambil max N candidate
3. Spawn N thread paralel (1 worker = 1 produk across N market)
   - Stagger start 2-3s antar thread
   - Pakai market_lock per-platform (cegah dual-tab)
   - Dynamic market picker: cek kolom O-X TRUE → trigger delete di market ybs
4. Per worker:
   a. router_update_harga(platform, kode, harga, link)
   b. 10 update_harga_* function (selector per marketplace)
   c. AD column auto-update ke last edit date (via sheet formula / bot explicit)
```

### DISKON_MAX_WORKER

Budget cumulative di prescan:
- `prescan_link()` di [main.py](main.py) scan LINK!E dari atas, pick tab sampai `sum(E) >= DISKON_MAX_WORKER`, hard cap 5 tab
- `MAX_WORKER` di `bot_diskon.py` clamp ke range 1-10 dari config

Skenario (contoh `DISKON_MAX_WORKER=10`):

| Tab | E | cumulative | dipilih |
|-----|---|------------|---------|
| Honkai SR | 5 | 5 | ✅ |
| Grand Summoners | 1 | 6 | ✅ |
| Wuthering | 2 | 8 | ✅ |
| Arknights | 3 | 11 | ✅ + break |
| (tab lainnya) | - | - | ❌ |

→ 4 tab di-scan, 10 row pertama di-dispatch (cap = MAX_WORKER).

## Min price override per market

Setiap market punya minimum harga yang diterima. Adapter auto-override **saat fill form** kalau harga sumber dari sheet < min. Sheet **tidak diubah**, cuma value yang di-fill ke marketplace.

| Market | Min price | Currency | Kolom sheet |
|--------|-----------|----------|-------------|
| GM | $1.99 | USD | H |
| G2G | Rp 40000 | IDR | G |
| PA | $5 | USD | H |
| ELDO | $1.99 | USD | H |
| ZEUS | $1.99 | USD | H |
| U7 | $1.99 | USD | H |
| GB | $1.99 | USD | H |
| IGV | $5 | USD | H |
| FP | (TBD) | — | — |
| Z2U | (TBD) | — | — |

**Behavior**: harga sheet $1.50 → adapter detect < min GM ($1.99) → fill `1.99` ke form GM. Sheet kolom H tetap `1.50`.

Detail per adapter di [create/README.md](create/README.md).

## Async image download

Sebelum refactor (sync):
```
Download gambar 30-40s (BLOCK)
    ↓
Dispatch market workers
    ↓
Market jalan flow, upload gambar
```

Sesudah (async):
```
Download gambar START (background Future)
    +
Dispatch market workers (langsung start)
    ↓
Market jalan navigate, fill form, dst (paralel dengan download)
    ↓
Di step upload → adapter call resolve_image_future(timeout=120s)
    - Kalau future sudah done → ambil hasil, lanjut upload (0 wait)
    - Kalau belum → block sampai download selesai atau timeout
    ↓
Upload + lanjut submit
```

**Savings**: ~20-40 detik per row (tergantung jumlah gambar).

### Implementation

- [create/_shared.py](create/_shared.py) — `start_image_download_async(gambar_url, max_images)` + `resolve_image_future(future, timeout=120)`
- [bot_create.py](bot_create.py) — orchestrator start future, pass ke `_run_market` → `mod.run(...image_future=...)`
- Tiap adapter ([create/*.py](create/)) — terima `image_future` kwarg, resolve di dalam `create_listing()` tepat sebelum upload step

### Error handling

Download gagal / timeout → `resolve_image_future` raise `RuntimeError`:
- Adapter catch → return `(False, "Gambar tidak bisa di download", uploaded)`
- Orchestrator aggregate K column → `❌ {CODE} | Gambar tidak bisa di download` per market
- `with sync_playwright()` context close tab otomatis
- `cleanup_tabs()` orchestrator sapu sisa

## Dev workflow — build & release

### Pertama kali di dev PC (sekali seumur hidup)

```
install_dependencies.bat     # pip install dependencies
gh auth login                # login GitHub CLI
git init + gh repo create    # init repo (lihat SETUP.txt)
```

### Tiap update

```
(edit kode)
release.bat                  # interactive prompt
```

`release.bat` flow (5 step):
1. **Version bump** (prompt) → update VERSION.txt + tag
2. **Commit** (prompt commit message, Enter = auto)
3. **Build EXE** (pyinstaller --fast --no-pause)
4. **Push kode** (git push)
5. **Upload Release** (gh release create + EXE)

Total waktu: ~2 menit. Office PC tinggal `update.bat` untuk ambil versi baru.

### Build EXE manual

```
build_exe.bat               # ada flag --fast --no-pause untuk skip upgrade
```

Output: `Bot Manage Listing.exe` di folder.

### Tambah adapter market baru

Saat tambah file `create/NEW.py`, **wajib** tambah juga ke `build_exe.bat`:

```bat
--hidden-import create.NEW ^
```

Kalau ndak, pyinstaller `--collect-submodules create` kadang miss lazy-loaded adapter (seperti kejadian IGV sebelum fix).

## Troubleshooting developer

### `module create.{CODE}.run tidak ada` di PC kantor

**Gejala**: Bot berhasil import tapi adapter X ndak jalan.

**Cek**:
1. File `create/{CODE}.py` ada di dev PC?
2. **`build_exe.bat` udah include `--hidden-import create.{CODE}`?** (common mistake)
3. EXE di PC kantor udah latest version?

### `KeyError: 0` di bot_create saat AI mapping

**Gejala**: Log `[AI] Gemini error: 0 - ABORT row, retry cycle`.

**Root cause**: Adapter return `scrape_form_options()` dengan format **nested dict** `{label: {type, options}}`. Shared `ai_map_fields_multi` cuma handle format **flat** `{label: [options]}`.

**Fix**: Adapter return `{}` dari `scrape_form_options` (skip shared AI), handle AI mapping sendiri di dalam `create_listing()`. Lihat pattern IGV.

### Thread zombie menumpuk (Chrome tab bengkak)

**Gejala**: Chrome makin lama makin banyak tab, memory naik.

**Cek**:
- Cycle timeout 10 menit (`t.join(timeout=600)`) sering kena?
- Ada market yg stuck di wait forever (ndak ada timeout internal)?

**Fix**:
- Ensure setiap step market adapter punya timeout (>0s, <= 60s).
- Gemini call pakai `call_with_timeout(timeout=30)`.
- `cleanup_tabs()` orchestrator setelah cycle harusnya tutup tab zombie.

### Bot UI toast error flooding

**Gejala**: Toast `❌` muncul terus-menerus.

**Cek log internal** di adapter. `✅`/`❌` emoji di add_log trigger toast — jangan pakai untuk progress internal, reserve untuk K column final line.

---

# Bagian 3 — Reference

## File structure tree

Lihat [Struktur folder](#struktur-folder).

## Config fields

Lihat [Config reference](#config-reference).

## Timeout values

| Lokasi | Nilai | Keterangan |
|--------|-------|------------|
| `t.join(timeout=600)` [bot_create.py:1246](bot_create.py#L1246) | **10 menit** | Max total per row create |
| `_ensure_form_options_cache` scrape timeout | 7 menit | Max per-market scrape form |
| Gemini call (ai_map_fields_multi) | 2 menit | Via `call_with_timeout`, 3x retry |
| Gemini call (IGV internal) | 30s | Via `call_with_timeout`, single attempt |
| Async image download (Future resolve) | 2 menit | `resolve_image_future(timeout=120)` |
| Playwright default action | 60s | `context.set_default_timeout(60000)` |
| Playwright default navigation | 60s | `context.set_default_navigation_timeout(60000)` |
| **PA delete** action + nav | **30s** | Tighter karena PA delete flow 2-tahap (Cancel + Selected Offers) |
| **PA create** submit error polling | 60s max | Loop 500ms cek `p.text-danger`/`.ant-message-error` paralel dengan URL redirect — early fail < 1s kalau inline error muncul |
| Market action wait_for visible | 5-15s | Per helper |
| **U7 image upload per file** | **30s** | `_upload_one_image_u7(timeout_ms=30000)` — abort-after-first-fail |
| IGV image upload total polling | 30s | `_upload_product_images(deadline=30)` — wait `is-success` class + 1s settle |
| Idle orchestrator backoff | 30s → 600s | Increment +10s tiap iterasi idle |

## Log lokasi

- Folder: `log/`
- Format: `app_log_YYYY-MM-DD.txt`
- Retention: `LOG_RETENTION_DAYS` (default 120 hari)
- Daemon `log_rotation_daemon` di [main.py](main.py) auto-delete log > retention

Format line:

```
[HH:MM:SS] [BOT_CATEGORY] [Wn] message
```

- `BOT_CATEGORY` = `APP` / `DELETE` / `CREATE` / `DISKON` / `AI` / `IMG` / market code
- `Wn` = worker ID (diskon multi-worker)

---

## Changelog ringkas

Detail commit lihat `git log` atau GitHub Releases.

- **v1.11.x (latest)** — IGV adapter full flow, async image download (8 adapter), U7 dropdown hardening + max 3 images, timeout 10 menit per row, GM count fix, **min price override semua market** (GM/G2G/ELDO/ZEUS/U7/GB/IGV — PA udah ada), **PA early error detection** (inline error polling paralel), **PA delete timeout 30s**, **G2G close dropdown 4-stage fallback**, IGV login_name kolom B di delivery, IGV settle delay 1s post-redirect, **update.bat enhancements** (version display + QuickEdit disable), **UI BotCard standby timer + running pulsing dot**, **Toast redesign** (pill badge SUKSES/ERROR + dimmed text color, no more dot pulse), **Toast format per-bot** (DELETE/DISKON full log, CREATE ringkas), **K line `[CODE]` brackets** semua adapter, **diskon per-market log** dengan emoji-aware [Worker N] prefix
- **v1.10.x** — PA min price $5, "Full Screenshot Detail:" description prefix 6 market, U7 upload abort-after-first-fail
- **v1.9.x** — Top-N=1 prescan + cumulative budget diskon, bottom-up row scan, duration di log cycle
- **v1.8.x** — Cycle sequential orchestrator, per-market adapter pluggable
- Sebelumnya — legacy polling mode

---

## Kontak / Issue

Admin bot: `support@gamemarket.gg` (atau sesuai config).

Report bug / feature: Github repo (kalau dibuka public).
