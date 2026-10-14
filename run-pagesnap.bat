@echo off
rem ===========================================================================
rem  PageSnap - double-click launcher for Windows
rem
rem  This file only exists to save you typing. It finds Python, installs the
rem  packages PageSnap needs into a private folder (nothing is installed
rem  system-wide), starts the server, and opens your browser.
rem
rem  If anything here fails, the manual steps in TROUBLESHOOTING.md work just as
rem  well and are easier to see.
rem ===========================================================================
setlocal
cd /d "%~dp0"
title PageSnap

echo.
echo   PageSnap - turning videos of notes into PDFs
echo   ===========================================
echo.

rem ── 1. find a Python launcher ─────────────────────────────────────────────
rem  "py" is the Windows Python launcher and is on PATH whenever Python was
rem  installed with "Add python.exe to PATH", so it is the most reliable option.
set PY=
py -3 --version >nul 2>&1 && set PY=py -3
if "%PY%"=="" (
  python --version >nul 2>&1 && set PY=python
)

if "%PY%"=="" (
  echo   Python was not found on this computer.
  echo.
  echo   What to do:
  echo     1. Install Python from  https://www.python.org/downloads/
  echo     2. In the installer, TICK "Add python.exe to PATH"
  echo     3. Close this window, then double-click this file again
  echo.
  echo   If you already have Python installed, open a new terminal and check
  echo   with:  py --version
  echo.
  pause
  exit /b 1
)

for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo   Found %%v
echo.

rem ── 2. a private environment, so nothing is installed system-wide ─────────
if not exist ".venv\Scripts\python.exe" (
  echo   Setting up a private folder for PageSnap's packages ^(once^) ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo.
    echo   Could not create the private environment.
    echo   You can install the packages normally instead, then run:
    echo      %PY% prototype\server.py
    echo.
    pause
    exit /b 1
  )
)
set VPY=.venv\Scripts\python.exe

rem ── 3. install what is missing ────────────────────────────────────────────
rem  PageSnap checks its own dependencies too, but doing it here means the very
rem  first run works without a second step.
%VPY% -c "import fastapi, uvicorn, numpy, PIL, reportlab, imageio_ffmpeg" >nul 2>&1
if errorlevel 1 (
  echo   Installing what PageSnap needs ^(once, needs the internet^) ...
  echo.
  %VPY% -m pip install --quiet --upgrade pip
  %VPY% -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo.
    echo   The install did not finish.
    echo   Check your internet connection, then run this file again.
    echo.
    pause
    exit /b 1
  )
  echo.
  echo   Done.
)

rem ── 4. start it, then open the browser ────────────────────────────────────
rem  The server runs in its own window, so you can see its messages and stop it
rem  by closing that window.
echo   Starting PageSnap ...
start "PageSnap server" %VPY% prototype\server.py
timeout /t 5 /nobreak >nul
start "" http://localhost:4567

echo.
echo   PageSnap is starting - your browser should open at http://localhost:4567
echo.
echo   Keep the "PageSnap server" window open while you use PageSnap.
echo   Closing that window stops it.
echo.
pause
