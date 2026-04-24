@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo  Bot Manage Listing - AUTO UPDATE
echo ============================================================
echo.

REM === 1. Stop bot yang sedang jalan ===
echo [1/3] Stop bot yang sedang jalan...
taskkill /F /IM "Bot Manage Listing.exe" >nul 2>&1
timeout /t 2 /nobreak >nul

REM === 2. Download EXE terbaru dari GitHub Releases ===
echo.
echo [2/3] Download update dari GitHub...

REM Ambil info versi + tanggal release via GitHub API
if exist release_info.json del release_info.json
curl -s "https://api.github.com/repos/hendrowiakto/tbg-poster/releases/latest" -o release_info.json 2>nul

set VERSION=
set PUBDATE=
if exist release_info.json (
    for /f "usebackq delims=" %%V in (`powershell -NoProfile -Command "try { (Get-Content release_info.json -Raw | ConvertFrom-Json).tag_name } catch { '' }" 2^>nul`) do set VERSION=%%V
    for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "try { ([datetime]((Get-Content release_info.json -Raw | ConvertFrom-Json).published_at)).ToString('yyyy-MM-dd HH:mm') } catch { '' }" 2^>nul`) do set PUBDATE=%%D
    del release_info.json
)

if defined VERSION echo        Versi terbaru: !VERSION!
if defined PUBDATE echo        Last update  : !PUBDATE!
echo        Downloading EXE...

if exist "BotManageListing.new.exe" del "BotManageListing.new.exe"

curl -L --fail --progress-bar -o "BotManageListing.new.exe" ^
    "https://github.com/hendrowiakto/tbg-poster/releases/latest/download/BotManageListing.exe"

if errorlevel 1 (
    echo.
    echo [ERROR] Download gagal. Cek koneksi internet.
    if exist "BotManageListing.new.exe" del "BotManageListing.new.exe"
    pause
    exit /b 1
)

REM Pastikan file valid (bukan HTML error page, cek size minimal 1 MB)
for %%F in ("BotManageListing.new.exe") do set SIZE=%%~zF
if !SIZE! LSS 1048576 (
    echo [ERROR] File download tidak valid. Coba lagi nanti.
    del "BotManageListing.new.exe"
    pause
    exit /b 1
)

REM === 3. Replace + Launch ===
echo.
echo [3/3] Replace EXE dan launch...
move /Y "BotManageListing.new.exe" "Bot Manage Listing.exe" >nul
if errorlevel 1 (
    echo [ERROR] Gagal replace EXE. Pastikan bot sudah benar-benar stop.
    pause
    exit /b 1
)

start "" "Bot Manage Listing.exe"

echo.
echo ============================================================
echo  UPDATE SELESAI. Bot sudah launch dengan versi baru.
echo ============================================================
echo.
timeout /t 3 /nobreak >nul
endlocal
