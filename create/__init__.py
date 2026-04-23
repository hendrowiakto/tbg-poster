"""create/ - per-market adapter modules untuk bot_create.

Struktur:
- _shared.py : utilitas generic (pure, no module-state dependency)
- GM.py      : GameMarket flow (Phase 3+)
- G2G.py     : G2G flow         (Phase 3+)
- Z2U.py     : future markets

bot_create.py (orchestrator) panggil market module via importlib berdasarkan
kode market di baris 48 sheet.
"""
