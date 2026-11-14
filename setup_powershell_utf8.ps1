# Настройка PowerShell 7: UTF-8 по умолчанию (кириллица без «╨╛╤В╨┤...»).
#
# Зачем: pwsh 7 в Windows открывает консоль в OEM-кодировке (cp866), поэтому
# UTF-8-байты git и других утилит отображаются как мусор. Скрипт один раз
# дописывает блок с кодировками в $PROFILE.CurrentUserAllHosts (обычно
# C:\Users\<user>\Documents\PowerShell\profile.ps1) — дальше он не нужен.
#
# Запуск:
#   pwsh -NoProfile -File .\setup_powershell_utf8.ps1
#
# Повторный запуск безопасен: блок определяется по маркеру и не дублируется
# (для принудительной перезаписи — -Force).
param([switch]$Force)

$ErrorActionPreference = "Stop"

$marker = "# --- UTF-8 defaults (added for Cyrillic-safe output) ---"
$block = @'
# --- UTF-8 defaults (added for Cyrillic-safe output) ---
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding  = [System.Text.Encoding]::UTF8
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'
'@

if ($PSVersionTable.PSVersion.Major -lt 7) {
    Write-Warning "Скрипт рассчитан на PowerShell 7 (pwsh): найден $($PSVersionTable.PSVersion)."
}

$path = $PROFILE.CurrentUserAllHosts
$dir = Split-Path -Parent $path
if (-not (Test-Path $dir)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

$current = if (Test-Path $path) { Get-Content -LiteralPath $path -Raw } else { "" }
if ($current.Contains($marker) -and -not $Force) {
    Write-Host "Профиль уже настроен: $path"
    exit 0
}

$text = $current.TrimEnd() + [Environment]::NewLine + [Environment]::NewLine + $block + [Environment]::NewLine
# UTF-8 без BOM — иначе pwsh читает BOM как мусорные символы в первой строке.
[System.IO.File]::WriteAllText($path, $text, [System.Text.UTF8Encoding]::new($false))

Write-Host "Готово. Блок UTF-8 добавлен в $path"
Write-Host "Откройте НОВЫЙ терминал pwsh, чтобы настройка применилась."
