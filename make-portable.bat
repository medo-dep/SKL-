@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
title Raw to Reel - build portable copy
python tools\make_portable.py %*
pause
