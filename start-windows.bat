@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
title Raw to Reel

rem ---- 1. Python (the Microsoft Store "python" stub doesn't count)
call :find_python
if not defined PY (
  echo Installing Python...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  call :refresh_path
  call :find_python
)
if not defined PY (
  echo.
  echo Python could not be installed automatically.
  echo Install it from https://www.python.org/downloads/ - tick "Add python.exe to PATH" - then run this file again.
  pause
  exit /b 1
)

rem ---- 2. FFmpeg
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo Installing FFmpeg...
  winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
  call :refresh_path
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo FFmpeg could not be installed automatically. Run: winget install Gyan.FFmpeg
  pause
  exit /b 1
)

rem ---- 3. Microsoft Visual C++ runtime, needed by the speech-to-text engine
if not exist "%SystemRoot%\System32\msvcp140.dll" (
  echo Installing Microsoft Visual C++ runtime...
  winget install -e --id Microsoft.VCRedist.2015+.x64 --accept-package-agreements --accept-source-agreements
)

rem ---- 4. Python packages
%PY% -c "import faster_whisper" >nul 2>nul || %PY% -m pip install faster-whisper
%PY% -c "import cv2" >nul 2>nul || %PY% -m pip install opencv-python-headless

rem ---- 5. Desktop shortcut, first run only
powershell -NoProfile -Command "$p=[Environment]::GetFolderPath('Desktop')+'\Raw to Reel.lnk'; if (-not (Test-Path $p)) { $s=(New-Object -ComObject WScript.Shell).CreateShortcut($p); $s.TargetPath='%~f0'; $s.WorkingDirectory='%~dp0'; $s.Save() }" >nul 2>nul

echo.
echo Starting Raw to Reel at http://127.0.0.1:4680 ...
echo Keep this window open while you use it.
%PY% raw-to-reel\server.py
pause
exit /b

:find_python
set "PY="
python -c "import sys" >nul 2>nul && set "PY=python" && exit /b
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
exit /b

:refresh_path
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','Machine')+';'+[Environment]::GetEnvironmentVariable('Path','User')"`) do set "PATH=%%p"
exit /b
