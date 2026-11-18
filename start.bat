@echo off
rem Gerçek kontrol sunucusu. İlk açılışta panel yönetici parolası ister.
cd /d "%~dp0"
py -m pip install -q -r requirements.txt
py run.py
pause
