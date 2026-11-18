@echo off
rem Windows masaustu uygulamasini derler: dist\Mercan\Mercan.exe
cd /d "%~dp0"
py -m pip install -q -r requirements.txt pywebview pyinstaller || exit /b 1
py -m PyInstaller --noconfirm --clean --windowed --name Mercan --icon packaging\mercan.ico ^
  --add-data "orkestra\static;orkestra\static" --add-data "agent;agent" --add-data "relay;relay" ^
  --collect-submodules uvicorn --collect-submodules asyncssh --collect-submodules orkestra --collect-all webview ^
  --hidden-import tarfile --hidden-import zipfile --hidden-import http.server --hidden-import winreg ^
  desktop.py || exit /b 1
echo Hazir: dist\Mercan\Mercan.exe
