@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Aru Clips
set "PY="
if exist "%~dp0runtime\python.exe" set "PY=%~dp0runtime\python.exe"
if not defined PY if exist "%~dp0venv\Scripts\python.exe" set "PY=%~dp0venv\Scripts\python.exe"
if not defined PY (
  echo  Aru Clips is not set up yet. Running Install.bat first...
  call "%~dp0Install.bat" || exit /b 1
  set "PY=%~dp0runtime\python.exe"
)
rem keep the downloader current: sites change often and old versions stop working
echo  Checking for downloader updates...
"%PY%" -m pip install -U "yt-dlp[default]" -q --no-warn-script-location >nul 2>nul
"%PY%" "%~dp0app\server.py"
if errorlevel 1 (
  echo.
  echo  Aru Clips stopped with an error. The details are in the window above
  echo  and in app.log in your AruClips folder ^(%USERPROFILE%\AruClips^).
)
pause
