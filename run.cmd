@echo off
setlocal
cd /d "%~dp0"

rem Lanzador para Windows que no depende de la politica de ejecucion de PowerShell.

if not exist ".venv\Scripts\python.exe" (
  echo Creando entorno virtual...
  python -m venv .venv
  if errorlevel 1 goto :error
)

echo Instalando dependencias...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 goto :error

if not exist ".env" (
  copy /y ".env.example" ".env" >nul
  echo.
  echo Se creo el archivo .env
  echo Editalo y completa TELEGRAM_BOT_TOKEN y GEMINI_API_KEY.
  echo Despues ejecuta otra vez:  run.cmd
  echo.
  exit /b 1
)

echo Servidor en http://localhost:8000   (docs: /docs)
echo Se reinicia solo al editar .env o cualquier archivo .py
".venv\Scripts\python.exe" -m uvicorn app.main:app --reload --reload-include ".env" --port 8000
exit /b 0

:error
echo.
echo No se pudo preparar el entorno. Comprueba que Python este instalado
echo y disponible en el PATH:  python --version
exit /b 1
