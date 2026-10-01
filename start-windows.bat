@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
where python >nul 2>nul || (echo Python is not installed. Run: winget install Python.Python.3.12 & pause & exit /b 1)
where ffmpeg >nul 2>nul || (echo FFmpeg is not installed. Run: winget install Gyan.FFmpeg & pause & exit /b 1)
python -c "import faster_whisper" 2>nul || python -m pip install faster-whisper
python -c "import cv2" 2>nul || python -m pip install opencv-python-headless
echo Starting Raw to Reel at http://127.0.0.1:4680 ...
python raw-to-reel\server.py
pause
