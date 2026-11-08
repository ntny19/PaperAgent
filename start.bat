@echo off
cd /d "%~dp0"
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo First run: created .env from .env.example.
  echo Edit .env now ^(set PAPERAGENT_PAPER_ROOT to your papers folder^), then run start.bat again.
  notepad ".env"
  pause
  exit /b
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
echo Starting PaperAgent at http://127.0.0.1:8765  (Ctrl+C to stop)
start "" http://127.0.0.1:8765
.venv\Scripts\python -W ignore -m app.main
pause
