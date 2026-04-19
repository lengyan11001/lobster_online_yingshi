@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting Mock Comfly on http://127.0.0.1:8765
echo Set COMFLY_API_BASE=http://127.0.0.1:8765/v1 and COMFLY_API_KEY=mock in .env, then use comfly.daihuo from chat.
python scripts\mock_comfly_server.py
pause
