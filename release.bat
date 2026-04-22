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

REM === 0. Version bump (prompt interaktif) ===
echo [0/5] Version bump
set CURRENT=1.5.3
if exist VERSION.txt (
    REM Pakai for /f biar aman terhadap LF-only (set /p hanya ngerti CRLF).
    REM Ambil baris pertama aja (version), abaikan baris tanggal.
    set _GOT=0
    for /f "usebackq delims=" %%a in ("VERSION.txt") do (
        if "!_GOT!"=="0" (
            set CURRENT=%%a
            set _GOT=1
        )
    )
)

echo        Version sekarang: v!CURRENT!
set NEW_VERSION=
set /p NEW_VERSION="       Push ke versi v (Enter = skip bump, pakai timestamp): "

REM Timestamp saat ini: YYYY-MM-DD HH:MM (wmic local time)
for /f "tokens=2 delims==" %%a in ('wmic OS Get localdatetime /value ^| find "="') do set DT=%%a
set TODAY=!DT:~0,4!-!DT:~4,2!-!DT:~6,2! !DT:~8,2!:!DT:~10,2!

if not "!NEW_VERSION!"=="" (
    echo        Update VERSION.txt -^> v!NEW_VERSION! ^(!TODAY!^)
    >VERSION.txt echo !NEW_VERSION!
    >>VERSION.txt echo !TODAY!
    set TAG=v!NEW_VERSION!
) else (
    REM Fallback: tag timestamp YYYYMMDD-HHMM, VERSION.txt tidak di-update
    set TAG=v!DT:~0,8!-!DT:~8,4!
    echo        Skip version bump. Tag fallback: !TAG!
)
echo.

REM === 1. Commit perubahan kode (kalau ada) ===
echo [1/5] Commit perubahan kode...
git add .
git diff --cached --quiet
if errorlevel 1 (
    set COMMIT_MSG=
    set /p COMMIT_MSG="       Pesan commit (Enter = auto): "
    if "!COMMIT_MSG!"=="" (
        if not "!NEW_VERSION!"=="" (
            set COMMIT_MSG=release v!NEW_VERSION!
        ) else (
            set COMMIT_MSG=release update
        )
    )
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
echo [2/5] Build EXE (pyinstaller, fast mode)...
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
echo [3/5] Push kode ke GitHub...
git push
if errorlevel 1 (
    echo [ERROR] Git push gagal. Cek koneksi atau auth gh.
    pause
    exit /b 1
)
echo.

REM === 4. Upload EXE ke GitHub Releases ===
echo [4/5] Upload EXE ke GitHub Releases (tag !TAG!)...

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
