@echo off
setlocal EnableDelayedExpansion

REM Flag --fast : skip pip upgrade + playwright install (dipakai release.bat)
REM Flag --no-pause : skip pause di akhir (dipakai release.bat)
set FAST=0
set NO_PAUSE=0
:parse_args
if "%~1"=="" goto done_args
if /i "%~1"=="--fast" set FAST=1
if /i "%~1"=="--no-pause" set NO_PAUSE=1
shift
goto parse_args
:done_args

echo ============================================================
echo  Bot Manage Listing - Build EXE
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python tidak ditemukan di PATH.
    if !NO_PAUSE!==0 pause
    exit /b 1
)

where pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [ERROR] pyinstaller tidak ditemukan. Run install_dependencies.bat dulu.
    if !NO_PAUSE!==0 pause
    exit /b 1
)

if not exist "main.py" (
    echo [ERROR] main.py tidak ditemukan. Jalankan script ini dari folder project.
    if !NO_PAUSE!==0 pause
    exit /b 1
)

if !FAST!==0 (
    echo.
    echo Upgrade pip dan packages ke versi terbaru...
    python -m pip install --upgrade pip
    python -m pip install --upgrade ^
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
        echo [ERROR] Upgrade packages gagal.
        if !NO_PAUSE!==0 pause
        exit /b 1
    )
    echo Update Chromium Playwright...
    python -m playwright install chromium
    echo.
) else (
    echo [FAST MODE] Skip upgrade packages dan Playwright.
    echo.
)

echo Membersihkan build lama...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
if exist "Bot Manage Listing.spec" del /q "Bot Manage Listing.spec"
if exist "Bot Manage Listing.exe" del /q "Bot Manage Listing.exe"

echo.
echo Membangun EXE (PyInstaller onefile, windowed)...
pyinstaller ^
    --onefile ^
    --windowed ^
    --distpath "." ^
    --name "Bot Manage Listing" ^
    --icon "icon.ico" ^
    --add-data "boys_gaming.gif;." ^
    --add-data "icon.ico;." ^
    --add-data "Bot Manage Listing.html;." ^
    --collect-all playwright ^
    --collect-all webview ^
    --collect-all clr_loader ^
    --collect-submodules pythonnet ^
    --hidden-import gspread ^
    --hidden-import google.auth ^
    --hidden-import google.oauth2 ^
    --hidden-import google.generativeai ^
    --hidden-import webview.platforms.edgechromium ^
    --hidden-import webview.platforms.winforms ^
    main.py

if errorlevel 1 (
    echo [ERROR] Build gagal.
    if !NO_PAUSE!==0 pause
    exit /b 1
)

echo.
echo ============================================================
echo  Build selesai.
echo  EXE:   Bot Manage Listing.exe  (di folder ini juga)
echo.
echo  Pastikan file berikut ada di folder yang sama:
echo    - config.txt
echo    - credentials.json
echo    - boys_gaming.gif (opsional, default to about:blank)
echo ============================================================

echo.
echo Membersihkan sisa build...
if exist build rmdir /s /q build
if exist "Bot Manage Listing.spec" del /q "Bot Manage Listing.spec"
if !NO_PAUSE!==0 pause
endlocal
