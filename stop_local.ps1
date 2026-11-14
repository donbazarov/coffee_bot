# Остановка локального веб-сервера (`neft_run_local.py`), слушающего порт.
#
# Зачем: uvicorn с `Ctrl+C` в одном окне, а процесс мог остаться от прошлой
# сессии — тогда новый запуск падает с "address already in use". Этот скрипт
# находит процесс по порту и останавливает его.
#
# Запуск:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\stop_local.ps1
# Порт можно переопределить: `-Port 8001` или переменной окружения NEFT_LOCAL_PORT.
param([int]$Port = 0)

if ($Port -le 0) {
    $Port = if ($env:NEFT_LOCAL_PORT) { [int]$env:NEFT_LOCAL_PORT } else { 8000 }
}

$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $listeners) {
    Write-Host "Порт $Port свободен — локальный сервер не запущен."
    exit 0
}

foreach ($procId in ($listeners | Select-Object -ExpandProperty OwningProcess -Unique)) {
    $process = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if (-not $process) { continue }
    Write-Host "Останавливаю PID $procId ($($process.ProcessName)) на порту $Port..."
    Stop-Process -Id $procId -Force
}

Start-Sleep -Milliseconds 400
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "Порт $Port всё ещё занят. Попробуйте терминал от имени администратора."
    exit 1
}
Write-Host "Порт $Port освобождён."
