@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Aru Clips - setup
echo.
echo  ==== Aru Clips setup (one time, about 5 minutes) ====
echo  Everything goes inside this folder. Nothing else on your PC is changed.
echo.

set "ROOT=%~dp0"
set "RT=%ROOT%runtime"
set "PY=%RT%\python.exe"
set "PYVER=3.12.10"
set "LOG=%ROOT%setup-log.txt"
echo Aru Clips setup %DATE% %TIME% > "%LOG%"

rem ---- 0. 64-bit Windows only (Python wheels for video/AI are 64-bit)
if /i "%PROCESSOR_ARCHITECTURE%"=="x86" if not defined PROCESSOR_ARCHITEW6432 (
  echo  This PC runs 32-bit Windows. Aru Clips needs 64-bit Windows 10 or 11.
  goto :fail
)

rem ---- 1. Portable Python, private to this folder (no admin, no winget, ignores any other Python)
if exist "%PY%" goto :pyok
echo  [1/4] Downloading Python %PYVER%...
if not exist "%RT%" mkdir "%RT%"
call :get "https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-embed-amd64.zip" "%RT%\py.zip" || goto :neterr
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath (Join-Path $env:RT 'py.zip') -DestinationPath $env:RT -Force" >>"%LOG%" 2>&1
del /q "%RT%\py.zip" >nul 2>nul
if not exist "%PY%" (
  echo  Could not unpack Python.
  goto :fail
)
rem let the portable Python use installed packages and find the app code
> "%RT%\python312._pth" (
  echo python312.zip
  echo .
  echo Lib\site-packages
  echo ..\app
  echo import site
)
:pyok

rem ---- 2. pip and packages
"%PY%" -m pip --version >nul 2>nul && goto :pipok
echo  [2/4] Setting up pip...
call :get "https://bootstrap.pypa.io/get-pip.py" "%RT%\get-pip.py" || goto :neterr
"%PY%" "%RT%\get-pip.py" --no-warn-script-location -q >>"%LOG%" 2>&1 || goto :fail
del /q "%RT%\get-pip.py" >nul 2>nul
:pipok
echo  [2/4] Installing packages (this is the slow part)...
"%PY%" -m pip install --upgrade pip --no-warn-script-location -q >>"%LOG%" 2>&1
"%PY%" -m pip install -r "%ROOT%requirements.txt" --no-warn-script-location --prefer-binary -q >>"%LOG%" 2>&1
if errorlevel 1 (
  echo  Retrying the package install once...
  "%PY%" -m pip install -r "%ROOT%requirements.txt" --no-warn-script-location --prefer-binary >>"%LOG%" 2>&1 || goto :fail
)

rem ---- 3. Microsoft C++ runtime (OpenCV and Whisper need it; most PCs already have it)
if exist "%SystemRoot%\System32\msvcp140.dll" if exist "%SystemRoot%\System32\vcruntime140_1.dll" goto :vcok
echo  [3/4] Installing the Microsoft C++ runtime (Windows may ask for permission)...
call :get "https://aka.ms/vs/17/release/vc_redist.x64.exe" "%RT%\vc_redist.x64.exe" && (
  "%RT%\vc_redist.x64.exe" /install /quiet /norestart
  del /q "%RT%\vc_redist.x64.exe" >nul 2>nul
)
:vcok

rem ---- 4. ffmpeg (with libass for captions)
if exist "%ROOT%tools\ffmpeg\bin\ffmpeg.exe" goto :ffok
echo  [4/4] Downloading ffmpeg...
if not exist "%ROOT%tools" mkdir "%ROOT%tools"
call :get "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" "%ROOT%tools\ff.zip" || call :get "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" "%ROOT%tools\ff.zip"
if exist "%ROOT%tools\ff.zip" powershell -NoProfile -ExecutionPolicy Bypass -Command "$t=Join-Path $env:ROOT 'tools'; Expand-Archive -LiteralPath (Join-Path $t 'ff.zip') -DestinationPath (Join-Path $t 'ff') -Force; $d=Get-ChildItem (Join-Path $t 'ff') -Directory | Select-Object -First 1; Move-Item -LiteralPath $d.FullName -Destination (Join-Path $t 'ffmpeg') -Force; Remove-Item (Join-Path $t 'ff.zip'),(Join-Path $t 'ff') -Recurse -Force" >>"%LOG%" 2>&1
if not exist "%ROOT%tools\ffmpeg\bin\ffmpeg.exe" (
  echo  Could not download ffmpeg automatically.
  echo  Download "ffmpeg-release-essentials.zip" from https://www.gyan.dev/ffmpeg/builds/
  echo  and unzip it so this file exists:  tools\ffmpeg\bin\ffmpeg.exe
  goto :fail
)
:ffok

rem ---- quick self-test
"%PY%" -c "import fastapi, uvicorn, numpy, cv2, yt_dlp, faster_whisper; print('self-test ok')" >>"%LOG%" 2>&1
if errorlevel 1 (
  echo  The self-test failed. Details are in setup-log.txt
  goto :fail
)

echo.
echo  Done! Double-click "Start Aru Clips.bat" to open the app.
echo  Optional: install Ollama from https://ollama.com and run  ollama pull qwen2.5:7b  for fully offline AI.
echo.
pause
exit /b 0

rem ---- download helper: curl.exe (Windows 10 1803+), then PowerShell with TLS 1.2
:get
set "DL_URL=%~1"
set "DL_OUT=%~2"
del /q "%~2" >nul 2>nul
where curl.exe >nul 2>nul && curl.exe -L --fail --retry 3 --silent --show-error -o "%~2" "%~1" >>"%LOG%" 2>&1
if exist "%~2" exit /b 0
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri $env:DL_URL -OutFile $env:DL_OUT" >>"%LOG%" 2>&1
if exist "%~2" exit /b 0
exit /b 1

:neterr
echo.
echo  Could not download a file. Check the internet connection (a VPN or school/work
echo  network can block downloads), then run Install.bat again.
:fail
echo.
echo  Setup did not finish. Details are in setup-log.txt in this folder.
echo  Run Install.bat again; it continues where it stopped.
pause
exit /b 1
