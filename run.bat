@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if exist ".venv\Scripts\python.exe" (
    set PY=.venv\Scripts\python.exe
) else (
    set PY=python
)

echo [Voice Agent] Starting server on http://localhost:8000...
echo Open http://localhost:8000 in Chrome, click 'Start Listening' and talk!
"%PY%" -m uvicorn app.main:app --port 8000
