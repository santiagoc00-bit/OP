# Arranca el asistente en Windows.
# Uso:  .\run.ps1

$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "Creando entorno virtual..." -ForegroundColor Cyan
    python -m venv .venv
}

Write-Host "Instalando dependencias..." -ForegroundColor Cyan
& $python -m pip install -q --upgrade pip
& $python -m pip install -q -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    Write-Host "Se creó .env: completa tus credenciales y vuelve a ejecutar." -ForegroundColor Yellow
    exit 1
}

Write-Host "Servidor en http://localhost:8000  (docs: /docs)" -ForegroundColor Green
& $python -m uvicorn app.main:app --reload --port 8000
