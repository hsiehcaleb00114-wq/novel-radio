@echo off
cd /d "%~dp0"
python -c "import edge_tts, webview" >nul 2>&1
if errorlevel 1 (
  title Novel Radio setup
  echo Installing components, first run only...
  python -m pip install --user edge-tts pywebview
)
start "" pythonw app.py
