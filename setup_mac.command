#!/bin/bash
# setup_mac.command - One-click setup untuk Bot Manage Listing di macOS.
#
# Cara pakai (user awam):
#   1. Download file ini ke Mac (Safari -> github -> Save Link As)
#   2. Klik kanan file -> Open -> Open (bypass Gatekeeper sekali)
#   3. Terminal akan kebuka otomatis & jalanin script
#   4. Input password Mac kamu (1x, saat brew minta sudo)
#   5. Tunggu ~10-15 menit (auto-download Homebrew + Python + dependencies)
#   6. Done!
#
# Yang script ini lakukan otomatis:
#   - Install Homebrew (kalau belum ada)
#   - Install Python 3.13 + git via brew
#   - Clone/update repo dari github
#   - Install Python dependencies via pip
#   - Install Playwright Chromium browser
#
# Script ini SAFE di-rerun berulang kali. Kalau ada yg sudah ke-install,
# akan di-skip otomatis.

set -e  # Exit kalau ada command gagal

# Auto-cd ke folder tempat script ini berada
cd "$(dirname "$0")"

# Warna ANSI untuk readability
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color
BOLD='\033[1m'

print_header() {
    echo ""
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BOLD}  $1${NC}"
    echo -e "${BLUE}============================================================${NC}"
    echo ""
}

print_step() {
    echo ""
    echo -e "${GREEN}[$1]${NC} $2"
    echo ""
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}


print_header "Bot Manage Listing - Mac Setup"

echo "Script ini akan install + setup bot otomatis."
echo "Estimasi waktu: 10-15 menit (tergantung internet)."
echo ""
echo "Kamu akan diminta password Mac SEKALI (saat Homebrew install)."
echo "Note: pas ngetik password, Terminal TIDAK menampilkan apapun (no dots/stars)."
echo "       Itu normal - Unix security feature. Ketik aja, terus tekan Enter."
echo ""
read -p "Tekan Enter untuk mulai (Ctrl+C untuk batal)..."


# ============================================================
# STEP 1: Homebrew
# ============================================================
print_step "1/5" "Cek Homebrew..."

if command -v brew &> /dev/null; then
    print_success "Homebrew sudah ke-install: $(brew --version | head -1)"
else
    print_warning "Homebrew belum ada. Install sekarang..."
    echo ""
    echo "📌 Pas diminta 'Password:' - ketik password Mac kamu, tekan Enter."
    echo "   (Layar akan terlihat kosong, tapi password sebenarnya udah ke-input.)"
    echo ""

    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

    # Tambahin brew ke PATH untuk session ini (Apple Silicon vs Intel)
    if [ -d "/opt/homebrew" ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
        # Append ke .zprofile biar permanent (next session-nya jg)
        if ! grep -q "/opt/homebrew/bin/brew shellenv" "$HOME/.zprofile" 2>/dev/null; then
            echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> "$HOME/.zprofile"
        fi
    elif [ -d "/usr/local/Homebrew" ]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi

    print_success "Homebrew installed: $(brew --version | head -1)"
fi


# ============================================================
# STEP 2: Python 3.13 + git
# ============================================================
print_step "2/5" "Install Python 3.13 + git..."

brew install python@3.13 git

# Pastikan python3 mengarah ke versi 3.13
if command -v python3.13 &> /dev/null; then
    PYTHON_BIN="python3.13"
else
    PYTHON_BIN="python3"
fi

PY_VERSION=$($PYTHON_BIN --version 2>&1)
print_success "Python: $PY_VERSION"

GIT_VERSION=$(git --version)
print_success "Git: $GIT_VERSION"


# ============================================================
# STEP 3: Clone / update repo
# ============================================================
print_step "3/5" "Clone / update repo dari github..."

TARGET_DIR="$HOME/Bot_Poster"

if [ -d "$TARGET_DIR/.git" ]; then
    echo "Repo sudah ada di $TARGET_DIR. Pull update terbaru..."
    cd "$TARGET_DIR"
    git pull origin main
    print_success "Repo updated to latest version"
else
    echo "Cloning ke $TARGET_DIR..."
    cd "$HOME"
    git clone https://github.com/hendrowiakto/tbg-poster.git Bot_Poster
    cd "$TARGET_DIR"
    print_success "Repo cloned to $TARGET_DIR"
fi


# ============================================================
# STEP 4: Install Python dependencies
# ============================================================
print_step "4/5" "Install Python dependencies via pip..."

if [ ! -f "requirements.txt" ]; then
    print_error "requirements.txt missing! Repo mungkin perlu update manual."
    exit 1
fi

# Pakai --user supaya install ke ~/Library/Python (no sudo needed)
$PYTHON_BIN -m pip install --user --upgrade pip
$PYTHON_BIN -m pip install --user -r requirements.txt

print_success "Python dependencies installed"


# ============================================================
# STEP 5: Playwright Chromium
# ============================================================
print_step "5/5" "Install Playwright Chromium browser..."

$PYTHON_BIN -m playwright install chromium

print_success "Playwright Chromium installed"


# ============================================================
# DONE
# ============================================================
print_header "✅ Setup Selesai!"

echo "Folder bot: ${BOLD}$TARGET_DIR${NC}"
echo ""
echo -e "${YELLOW}Next steps (manual, dilakukan sekali):${NC}"
echo ""
echo "  1. Edit ${BOLD}config.txt${NC} di $TARGET_DIR:"
echo "     - SPREADSHEET_ID (ID Google Sheets kamu)"
echo "     - GEMINI_API_KEY"
echo "     - CHROME_PATH=/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
echo "     - CHROME_USER_DATA_DIR=$HOME/chrome-debug"
echo ""
echo "  2. Copy ${BOLD}credentials.json${NC} (Google service account) ke $TARGET_DIR"
echo ""
echo "  3. Login marketplace di Chrome dulu:"
echo "     ${BOLD}'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \\${NC}"
echo "     ${BOLD}    --remote-debugging-port=9222 --user-data-dir=$HOME/chrome-debug${NC}"
echo ""
echo "     Login ke 10 marketplace (GameMarket, G2G, PA, U7, ZEUS, GB, ELDO, IGV, Z2U, FP)"
echo "     Tutup Chrome setelah selesai."
echo ""
echo "  4. Run bot:"
echo "     ${BOLD}cd $TARGET_DIR && $PYTHON_BIN main.py${NC}"
echo ""
echo -e "${GREEN}Untuk update bot ke versi terbaru: tinggal jalanin script ini lagi.${NC}"
echo ""
read -p "Tekan Enter untuk close window..."
