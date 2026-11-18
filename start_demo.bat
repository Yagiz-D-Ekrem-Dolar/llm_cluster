@echo off
rem Simülatör: sahte makinelerle paneli dener (gerçek bir şey çalıştırmaz). Parola: demo1234
cd /d "%~dp0"
py -m pip install -q -r requirements.txt
start "" http://127.0.0.1:8080
py run.py --demo --reset-demo
pause
