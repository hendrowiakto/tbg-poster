#!/bin/bash
# update_mac.command - One-click update + launch bot di macOS.
#
# Cara pakai (daily / kapan ada update):
#   1. Pastikan bot ndak lagi jalan (atau script ini akan auto-stop)
#   2. Double-click file ini
#   3. Auto: stop bot -> git pull -> upgrade deps -> launch bot
#
# Update biasa selesai 30-60 detik (vs first-time setup 10-15 menit).

set -e

cd "$(dirname "$0")"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'
BOLD='\033[1m'

echo ""
echo -e "${BOLD}============================================================${NC}"
echo -e "${BOLD}  Bot Manage Listing - Mac Update${NC}"
echo -e "${BOLD}============================================================${NC}"
echo ""

# Detect Python binary
if command -v python3.13 &> /dev/null; then
    PYTHON_BIN="python3.13"
else
    PYTHON_BIN="python3"
fi

# Pastikan brew di PATH (Apple Silicon)
if [ -d "/opt/homebrew" ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true
fi

# ============================================================
# 1. Stop bot yang lagi jalan (kalau ada)
# ============================================================
echo -e "${GREEN}[1/4]${NC} Stop bot yang lagi jalan (kalau ada)..."

# Match Python process running main.py di folder Bot_Poster
pkill -f "Bot_Poster/main.py" 2>/dev/null || true
sleep 1

echo -e "${GREEN}     ✅ Done${NC}"
echo ""

# ============================================================
# 2. Pull latest dari github
# ============================================================
echo -e "${GREEN}[2/4]${NC} Pull update dari github..."

if [ ! -d "$HOME/Bot_Poster/.git" ]; then
    echo -e "${RED}❌ Folder Bot_Poster ndak ditemukan di $HOME/Bot_Poster${NC}"
    echo "   Run setup_mac.command dulu untuk first-time install."
    read -p "Tekan Enter untuk close..."
    exit 1
fi

cd "$HOME/Bot_Poster"

# Cek dulu kalau ada local changes (config.txt etc) — jangan overwrite
git stash push -m "auto-stash before update" 2>/dev/null || true
git pull origin main
git stash pop 2>/dev/null || true

# Tampil versi terbaru
if [ -f "VERSION.txt" ]; then
    VERSION=$(head -1 VERSION.txt)
    PUBDATE=$(sed -n '2p' VERSION.txt)
    echo "     Versi terbaru: $VERSION"
    echo "     Last update  : $PUBDATE"
fi

echo ""

# ============================================================
# 3. Upgrade Python dependencies
# ============================================================
echo -e "${GREEN}[3/4]${NC} Upgrade dependencies (kalau ada update)..."

$PYTHON_BIN -m pip install --user --upgrade --quiet -r requirements.txt 2>&1 | grep -v "already satisfied" || true

echo -e "${GREEN}     ✅ Done${NC}"
echo ""

# ============================================================
# 4. Launch bot
# ============================================================
echo -e "${GREEN}[4/4]${NC} Launch bot..."

# Launch di background, detach dari Terminal supaya window Terminal bisa tutup
nohup $PYTHON_BIN main.py > /dev/null 2>&1 &
BOT_PID=$!

sleep 2

if kill -0 $BOT_PID 2>/dev/null; then
    echo -e "${GREEN}     ✅ Bot launched (PID $BOT_PID)${NC}"
else
    echo -e "${RED}     ❌ Bot gagal launch. Cek log atau jalanin manual:${NC}"
    echo -e "${YELLOW}        cd ~/Bot_Poster && $PYTHON_BIN main.py${NC}"
fi

echo ""
echo -e "${BOLD}============================================================${NC}"
echo -e "${GREEN}${BOLD}  UPDATE SELESAI. Bot udah launch dengan versi terbaru.${NC}"
echo -e "${BOLD}============================================================${NC}"
echo ""

# Auto-close Terminal setelah 3 detik (kalau bot launch sukses)
sleep 3
