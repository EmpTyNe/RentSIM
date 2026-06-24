# run-local.ps1 — нативный запуск под Windows без Docker.
# Перед запуском убедитесь, что:
#   1) PostgreSQL с расширением PostGIS запущен (служба) и создана БД/пользователь;
#   2) Mosquitto запущен (служба или mosquitto.exe -c mosquitto\mosquitto.conf);
#   3) файл .env создан из .env.local (DB_HOST=localhost, MQTT_HOST=localhost).
#
# Запуск:  powershell -ExecutionPolicy Bypass -File windows\run-local.ps1

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)   # перейти в корень проекта

# Виртуальное окружение + зависимости
if (-not (Test-Path ".venv")) { python -m venv .venv }
. .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Создать таблицы и тестовые данные (один раз; повторный запуск не страшен)
python -m app.seed

# Эмулятор устройств — в отдельном окне PowerShell
Start-Process powershell -ArgumentList @(
  "-NoExit","-Command",
  ". .\.venv\Scripts\Activate.ps1; `$env:MQTT_HOST='localhost'; python -m emulator.emulator"
)

# API (этот процесс остаётся в текущем окне). Панель: http://localhost:8000/admin
Write-Host "Панель оператора: http://localhost:8000/admin  (токен: admin-secret)" -ForegroundColor Green
uvicorn app.main:app --host 0.0.0.0 --port 8000
