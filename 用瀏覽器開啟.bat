@echo off
cd /d "%~dp0"
title Novel Radio
python -c "import edge_tts" >nul 2>&1
if errorlevel 1 (
  echo Installing the Edge voice component, first run only...
  python -m pip install --user edge-tts
)
python novel_radio.py
if errorlevel 1 (
  echo.
  echo Failed to start. Please make sure Python 3 is installed.
  pause
)
