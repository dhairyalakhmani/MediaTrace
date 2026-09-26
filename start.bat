@echo off
REM Lemnisca Media Knowledge Base: double-click to start.
cd /d "%~dp0"
echo Starting database, file storage and PDF parser (Docker)...
docker compose up -d
if errorlevel 1 (
  echo.
  echo Docker is not running. Start Docker Desktop, wait until it says "running", then double-click this file again.
  pause
  exit /b 1
)
set PYTHONPATH=src
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" -m lemkb ui
pause
