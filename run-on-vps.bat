@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt -q
)

echo Starting 3CX Telegram bot (Call Control + optional webhook on port 8080)...
echo Keep this window open. See 3CX-AI-SETUP.txt for configuration.
echo.
.venv\Scripts\python.exe bot.py
