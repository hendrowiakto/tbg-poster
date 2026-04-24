"""main.py - entry point untuk Bot Manage Listing (merged bot app).

Bertanggung jawab:
- Load config, validate, init BotContext (shared.py).
- Launch Chrome, connect Google Sheets.
- Start daemon: log rotation, Chrome monitor, orchestrator loop.
- Build Tkinter GUI: 3 toggles + progress row + dashboard tabs + live log + bottom bar.
- Handle graceful close (stop_event, cleanup Chrome, destroy GUI).

Orchestrator pattern: sequential per cycle (Delete -> Create -> Diskon) dengan
toggle ON/OFF per bot. Semua infrastruktur (logger, chrome, sheets, stats,
toggles, progress) datang dari `ctx`.
"""

import os
import sys
import time
import threading

from shared import (
    Config, BotContext, validate_config,
    SCRIPT_DIR, LOG_DIR, BOT_NAMES,
    read_version,
)
from webview_app import WebviewApp

# ===================== ORCHESTRATOR =====================
# Top-N picking strategy untuk prescan: bot delete/create proses 1 row/cycle,
# jadi cukup scan 1 tab teratas. Bot diskon punya N worker paralel (MAX_WORKER),
# jadi dinamis - scan tab teratas sampai cumulative count >= budget worker
# (capped supaya ndak edge case scan kebanyakan tab kalau E per-tab kecil-kecil).
TOP_N_DELETE             = 1
TOP_N_CREATE             = 1
DISKON_MAX_TABS_HARD_CAP = 5


def prescan_link(ctx):
    """1 batch_get untuk LINK!A + C + D + E -> dict per-bot tab aktif.
    Hemat vs dulu yg 3x batch_get (1 per bot). Return:
        {"delete": [tab_names] atau None, "create": [...] atau None, "diskon": [...] atau None}
    None = prescan gagal (bot fallback ke get_active_sheet_names sendiri).

    Top-N strategy: delete/create cuma kasih 1 tab teratas (1 row/cycle),
    diskon cumulative budget sesuai DISKON_MAX_WORKER (capped 5 tab).
    Total count tetap di-track akurat di ctx.task_counts (untuk badge UI).
    """
    try:
        resp = ctx.sheets.spreadsheet.values_batch_get(
            ranges=[
                "'LINK'!A2:A",
                "'LINK'!C2:C",
                "'LINK'!D2:D",
                "'LINK'!E2:E",
            ]
        )
    except Exception as e:
        ctx.logger.log("app", f"Prescan LINK gagal: {str(e)[:200]}")
        return {"delete": None, "create": None, "diskon": None}

    vranges = resp.get("valueRanges", []) or []
    col_a = vranges[0].get("values", []) if len(vranges) >= 1 else []
    col_c = vranges[1].get("values", []) if len(vranges) >= 2 else []
    col_d = vranges[2].get("values", []) if len(vranges) >= 3 else []
    col_e = vranges[3].get("values", []) if len(vranges) >= 4 else []

    def _scan(counter_col):
        """Return list of (tab_name, count) untuk tab dengan count > 0,
        urutan sesuai LINK sheet (top -> bottom)."""
        pairs = []
        for i, arow in enumerate(col_a):
            name = (arow[0] if arow else "").strip()
            if not name:
                continue
            cstr = ""
            if i < len(counter_col) and counter_col[i]:
                cstr = str(counter_col[i][0]).strip()
            try:
                count = int(float(cstr)) if cstr else 0
            except (ValueError, TypeError):
                count = 0
            if count > 0:
                pairs.append((name, count))
        return pairs

    def _pick_top_n(pairs, n):
        """Ambil n tab teratas saja."""
        return [name for name, _ in pairs[:n]]

    def _pick_cumulative(pairs, budget, max_tabs):
        """Ambil tab teratas sampai cumulative count >= budget, atau hit max_tabs."""
        picked = []
        cum = 0
        for name, count in pairs:
            if len(picked) >= max_tabs:
                break
            picked.append(name)
            cum += count
            if cum >= budget:
                break
        return picked

    del_pairs = _scan(col_c)
    cre_pairs = _scan(col_d)
    dis_pairs = _scan(col_e)

    # Total sum semua tab (untuk task_counts badge UI - harus akurat, bukan top-N)
    del_sum = sum(c for _, c in del_pairs)
    cre_sum = sum(c for _, c in cre_pairs)
    dis_sum = sum(c for _, c in dis_pairs)

    # Diskon budget = MAX_WORKER bot_diskon (config DISKON_MAX_WORKER, range 1-10).
    diskon_budget = max(1, min(10, ctx.config.get_int("DISKON_MAX_WORKER", 5)))

    # Apply top-N picking per bot
    del_tabs = _pick_top_n(del_pairs, n=TOP_N_DELETE)
    cre_tabs = _pick_top_n(cre_pairs, n=TOP_N_CREATE)
    dis_tabs = _pick_cumulative(dis_pairs, budget=diskon_budget,
                                max_tabs=DISKON_MAX_TABS_HARD_CAP)

    # Stash ke ctx supaya StateBridge bisa push ke UI sebagai badge "DELETE (N)"
    ctx.task_counts = {"delete": del_sum, "create": cre_sum, "diskon": dis_sum}

    result = {"delete": del_tabs, "create": cre_tabs, "diskon": dis_tabs}
    # Skip log kalau semua idle - orchestrator sudah log "Semua idle, retry in Xs..."
    # setelahnya, jadi prescan 0(0) 0(0) 0(0) cuma noise.
    # Format: DELETE=picked/total_active(total_count) - misal "DELETE=1/22(141)"
    if del_sum or cre_sum or dis_sum:
        parts = []
        if del_sum:
            parts.append(f"DELETE={len(del_tabs)}/{len(del_pairs)}({del_sum})")
        if cre_sum:
            parts.append(f"CREATE={len(cre_tabs)}/{len(cre_pairs)}({cre_sum})")
        if dis_sum:
            parts.append(f"DISKON={len(dis_tabs)}/{len(dis_pairs)}({dis_sum})")
        ctx.logger.log("app", f"Prescan LINK: {' '.join(parts)}")
    return result


def orchestrator_loop(ctx):
    """Priority cycle: DELETE (C) > CREATE (D) > DISKON (E).
    1 prescan LINK!A+C+D+E di awal tiap iterasi -> routing ke bot prioritas
    tertinggi yg punya count>0. Semua idle/OFF -> sleep SHARED_POLLING_INTERVAL.
    """
    import bot_delete
    import bot_create
    import bot_diskon
    bots_map = {"delete": bot_delete, "create": bot_create, "diskon": bot_diskon}
    priority = ["delete", "create", "diskon"]

    # Dynamic backoff saat idle: mulai 30s, tambah +10s tiap iterasi tanpa kerjaan,
    # cap di 600s (10 menit). Reset ke base begitu ada kerjaan.
    IDLE_BASE = 30
    IDLE_STEP = 10
    IDLE_MAX = 600
    idle_wait = IDLE_BASE
    last_reconnect_attempt = 0.0  # epoch; cooldown supaya tidak spam connect()

    while not ctx.stop_event.is_set():
        # Auto-reconnect Sheets kalau spreadsheet None (startup gagal / koneksi
        # drop di tengah jalan). Cooldown 60s biar tidak hammering API.
        if ctx.sheets.spreadsheet is None:
            now = time.time()
            if now - last_reconnect_attempt >= 60:
                last_reconnect_attempt = now
                try:
                    ctx.sheets.connect()
                    ctx.logger.log("app", "Google Sheets tersambung ulang")
                except Exception as e:
                    detail = str(e) or type(e).__name__
                    ctx.logger.log("app", f"Sheets reconnect gagal ({type(e).__name__}): {detail[:200]}")

        snap = prescan_link(ctx)
        processed = False

        for bot_name in priority:
            if ctx.stop_event.is_set():
                break

            bot = bots_map[bot_name]

            if not ctx.toggles.get(bot_name):
                ctx.progress.set(bot_name, {"phase": "off"})
                continue

            tabs = snap.get(bot_name)
            # None = prescan gagal, biar bot fallback batch_get sendiri.
            # [] = prescan sukses & tidak ada tab aktif -> skip tanpa panggil bot.
            if tabs is not None and len(tabs) == 0:
                ctx.progress.set(bot_name, {"phase": "idle"})
                continue

            # Inject hasil prescan ke bot (one-shot, dikonsumsi di get_active_sheet_names)
            if tabs is not None:
                try:
                    bot.set_prefetched_active_sheets(tabs)
                except AttributeError:
                    pass

            ctx.progress.set(bot_name, {"phase": "running"})
            ctx.logger.log("app", f"Mulai cycle {bot_name.upper()}")
            n = 0
            try:
                n = bot.run_one_cycle(ctx)
                if n and n > 0:
                    processed = True
                    ctx.logger.log("app", f"Cycle {bot_name.upper()} selesai - {n} item diproses")
                else:
                    ctx.logger.log("app", f"Cycle {bot_name.upper()} idle - tidak ada kerjaan")
            except Exception as e:
                ctx.logger.log("app", f"Cycle {bot_name.upper()} crash: {str(e)[:200]}")
            finally:
                ctx.progress.set(bot_name, {"phase": "idle"})
                # Clear worker_status module dict biar UI tidak nampilin stale
                # worker row saat bot balik ke Standby.
                try:
                    ws = getattr(bot, "worker_status", None)
                    ws_lock = getattr(bot, "worker_status_lock", None)
                    if ws is not None:
                        if ws_lock is not None:
                            with ws_lock:
                                ws.clear()
                        else:
                            ws.clear()
                except Exception:
                    pass

            # Cleanup tab antar bot - cegah akumulasi tab di Chrome
            try:
                ctx.chrome.cleanup_tabs()
            except Exception:
                pass

            # Priority: kalau batch ini benar-benar proses sesuatu, restart dari
            # bot prioritas tertinggi (DELETE) supaya C > D > E tidak dilanggar.
            if n and n > 0:
                break

        # Reset force_scan setelah 1 full loop
        ctx.force_scan = False

        if processed:
            idle_wait = IDLE_BASE  # ada kerjaan -> reset backoff ke base
            continue  # ada kerjaan, scan ulang dari DELETE

        # Semua idle / OFF -> dynamic backoff sleep (responsif stop_event + force_scan)
        ctx.logger.log("app", f"Belum ada tugas baru, retry dalam {idle_wait}s...")
        for _ in range(idle_wait):
            if ctx.stop_event.is_set():
                return
            if ctx.force_scan:
                ctx.logger.log("app", "Force Scan terdeteksi, skip idle wait")
                break
            time.sleep(1)
        # Naikkan backoff untuk iterasi berikutnya kalau masih idle
        idle_wait = min(idle_wait + IDLE_STEP, IDLE_MAX)


# ===================== DAEMONS =====================
def log_rotation_daemon(ctx):
    """Jalan 1x/hari: hapus file log > LOG_RETENTION_DAYS hari."""
    last_date = ""
    while not ctx.stop_event.is_set():
        today = time.strftime("%Y-%m-%d")
        if today != last_date:
            try:
                deleted = ctx.cleanup_old_logs()
                if deleted:
                    ctx.logger.log("app", f"Log rotation: hapus {deleted} file lama")
            except Exception as e:
                ctx.logger.log("app", f"log_rotation error: {e}")
            last_date = today
        for _ in range(3600):
            if ctx.stop_event.is_set():
                return
            time.sleep(1)


def chrome_monitor_daemon(ctx):
    """Monitor Chrome; kalau port mati -> auto-restart."""
    while not ctx.stop_event.is_set():
        try:
            if not ctx.chrome.is_alive():
                ctx.logger.log("app", f"Chrome (port {ctx.chrome.debug_port}) mati, restart...")
                ctx.chrome.ensure_alive()
        except Exception as e:
            ctx.logger.log("app", f"chrome_monitor error: {e}")
        for _ in range(15):
            if ctx.stop_event.is_set():
                return
            time.sleep(1)



# ===================== ENTRY =====================
def _show_config_error(missing):
    """Config belum lengkap: coba native dialog, fallback ke stdout."""
    body = ("Item berikut kurang / invalid di config.txt atau credentials.json:\n\n"
            + "\n".join(f"- {m}" for m in missing)
            + f"\n\nEdit file di:\n{SCRIPT_DIR}\n\nLalu jalankan ulang aplikasi.")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, body, "Config tidak lengkap", 0x10)
    except Exception:
        print("CONFIG ERROR:\n" + body, file=sys.stderr)


def main():
    config = Config()
    missing = validate_config(config)
    if missing:
        _show_config_error(missing)
        sys.exit(1)

    ctx = BotContext(config)
    ctx.force_scan = False  # GUI-driven flag: skip idle wait, prescan ulang
    ctx.task_counts = {"delete": 0, "create": 0, "diskon": 0}  # diisi prescan_link

    ctx.init_folders_and_files()
    ctx.cleanup_old_logs()
    ctx.load_all_stats()
    # Semua bot mulai ON setiap launch. Toggle tidak di-persist (UI-only state).
    for _b in BOT_NAMES:
        ctx.toggles.set(_b, True)

    _ver, _ = read_version()
    _instance = config.get("INSTANCE_NAME", "").strip()
    _title_suffix = f" {_instance}" if _instance else ""
    _ver_log = f" v{_ver}" if _ver else ""
    ctx.logger.log("app", f"Bot Manage Listing{_title_suffix}{_ver_log} start")

    # Launch Chrome + connect Sheets (sync at startup). Kalau gagal, orchestrator
    # akan retry otomatis tiap iterasi (lihat orchestrator_loop -> auto-reconnect).
    ctx.chrome.ensure_alive()
    try:
        ctx.sheets.connect()
        ctx.logger.log("app", "Google Sheets terhubung")
    except Exception as e:
        detail = str(e) or type(e).__name__
        ctx.logger.log("app", f"Sheets gagal connect ({type(e).__name__}): {detail[:200]}. "
                               f"Orchestrator akan auto-retry.")

    # Daemons
    threading.Thread(target=log_rotation_daemon, args=(ctx,),
                     daemon=True, name="log-rotation").start()
    threading.Thread(target=chrome_monitor_daemon, args=(ctx,),
                     daemon=True, name="chrome-monitor").start()
    threading.Thread(target=orchestrator_loop, args=(ctx,),
                     daemon=True, name="orchestrator").start()

    app = WebviewApp(ctx, title=f"Bot Manage Listing{_title_suffix}")
    app.run()


if __name__ == "__main__":
    main()
