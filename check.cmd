@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo No existe .venv. Ejecuta primero:  run.cmd
  exit /b 1
)

".venv\Scripts\python.exe" check.py
