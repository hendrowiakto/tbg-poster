"""Generate _prompt_defaults.py from prompt_title_template.txt.

Dipanggil otomatis di build_exe.bat sebelum PyInstaller. Tujuan: prompt jadi
external file (production team boleh edit), tapi tetap punya fallback default
yg di-embed ke EXE supaya bot bisa auto-create file kalau dihapus.

Source of truth: prompt_title_template.txt
Build artifact:  _prompt_defaults.py (di-commit untuk dev mode convenience)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "prompt_title_template.txt")
DST = os.path.join(ROOT, "_prompt_defaults.py")


def main():
    if not os.path.exists(SRC):
        print(f"[embed_prompt] ERROR: source not found: {SRC}", file=sys.stderr)
        sys.exit(1)

    with open(SRC, "r", encoding="utf-8") as f:
        content = f.read()

    out = (
        "# AUTO-GENERATED from prompt_title_template.txt by tools/embed_prompt.py.\n"
        "# JANGAN edit file ini langsung - edit prompt_title_template.txt lalu rebuild.\n"
        "\n"
        f"DEFAULT_TITLE_PROMPT = {content!r}\n"
    )

    with open(DST, "w", encoding="utf-8") as f:
        f.write(out)

    print(f"[embed_prompt] wrote {DST} ({len(content)} chars)")


if __name__ == "__main__":
    main()
