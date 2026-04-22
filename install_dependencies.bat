@echo off
setlocal
echo ============================================================
echo  Bot Manage Listing - Install Dependencies
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python tidak ditemukan di PATH.
    echo Install Python 3.10+ dari https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/4] Upgrade pip...
python -m pip install --upgrade pip

echo.
echo [2/4] Install Python packages...
python -m pip install ^
    gspread ^
    google-auth ^
    google-auth-oauthlib ^
    google-generativeai ^
    playwright ^
    requests ^
    beautifulsoup4 ^
    pywebview ^
    pythonnet ^
    pyinstaller

if errorlevel 1 (
    echo [ERROR] Gagal install packages.
    pause
    exit /b 1
)

echo.
echo [3/4] Install Chromium untuk Playwright...
python -m playwright install chromium

echo.
echo [4/4] Verify imports...
python -c "import gspread, google.auth, google.generativeai, playwright, requests, bs4, webview, pythonnet, clr_loader; print('OK')"
if errorlevel 1 (
    echo [ERROR] Import verification gagal.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Install selesai. Jalankan main.py atau build_exe.bat
echo ============================================================
pause
endlocal
