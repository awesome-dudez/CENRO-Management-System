@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo No venv found. Run: py -m venv .venv
    echo Then: .venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 1
)
if "%~1"=="" (
    ".venv\Scripts\python.exe" manage.py runserver
) else (
    ".venv\Scripts\python.exe" manage.py %*
)
