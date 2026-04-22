@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo  Bot Manage Listing - RELEASE (Build + Push + Upload)
echo ============================================================
echo.

REM === Cek prerequisite ===
where gh >nul 2>&1
if errorlevel 1 (
    echo [ERROR] GitHub CLI tidak ditemukan.
    echo Install dulu: https://cli.github.com/
    pause
    exit /b 1
)

where git >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Git tidak ditemukan.
    echo Install dulu: https://git-scm.com/download/win
    pause
    exit /b 1
)

REM === 1. Commit perubahan kode (kalau ada) ===
echo [1/4] Commit perubahan kode...
git add .
git diff --cached --quiet
if errorlevel 1 (
    set /p COMMIT_MSG="       Pesan commit (Enter = auto): "
    if "!COMMIT_MSG!"=="" set COMMIT_MSG=release update
    git commit -m "!COMMIT_MSG!"
    if errorlevel 1 (
        echo [ERROR] Git commit gagal.
        pause
        exit /b 1
    )
) else (
    echo        Tidak ada perubahan kode untuk di-commit.
)
echo.

REM === 2. Build EXE (fast mode, skip upgrade) ===
echo [2/4] Build EXE (pyinstaller, fast mode)...
call build_exe.bat --fast --no-pause
if errorlevel 1 (
    echo [ERROR] Build EXE gagal.
    pause
    exit /b 1
)
if not exist "Bot Manage Listing.exe" (
    echo [ERROR] EXE tidak ter-generate.
    pause
    exit /b 1
)
echo.

REM === 3. Push kode ke GitHub ===
echo [3/4] Push kode ke GitHub...
git push
if errorlevel 1 (
    echo [ERROR] Git push gagal. Cek koneksi atau auth gh.
    pause
    exit /b 1
)
echo.

REM === 4. Upload EXE ke GitHub Releases ===
echo [4/4] Upload EXE ke GitHub Releases...

REM Generate tag dari timestamp (YYYYMMDD-HHMM)
for /f "tokens=2 delims==" %%a in ('wmic OS Get localdatetime /value ^| find "="') do set DT=%%a
set TAG=v!DT:~0,8!-!DT:~8,4!

REM Rename EXE ke nama tanpa spasi biar URL bersih
copy /Y "Bot Manage Listing.exe" "BotManageListing.exe" >nul

gh release create !TAG! "BotManageListing.exe" --title "Release !TAG!" --notes "Auto-release" --latest
if errorlevel 1 (
    echo [ERROR] Upload Release gagal.
    if exist BotManageListing.exe del BotManageListing.exe
    pause
    exit /b 1
)

del BotManageListing.exe

echo.
echo ============================================================
echo  RELEASE SELESAI : !TAG!
echo  Office PC tinggal klik update.bat untuk update.
echo ============================================================
echo.
pause
endlocal
