#requires -Version 5.1
<#
    Установка дневника на Windows (Python 3.9+).

        powershell -ExecutionPolicy Bypass -File .\install.ps1
        powershell -ExecutionPolicy Bypass -File .\install.ps1 -Python "C:\Python312\python.exe"
        powershell -ExecutionPolicy Bypass -File .\install.ps1 -Recreate

    Скрипт создаёт виртуальное окружение .venv рядом с проектом и ставит
    зависимости из requirements.txt (включая sqlcipher3 с готовым Windows-колёсом).
    Ничего за пределами папки проекта не изменяется; файл базы не трогается.
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$Recreate
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "== Дневник: установка на Windows ==" -ForegroundColor Cyan
Write-Host "Папка проекта: $Root"

function Get-PythonCommand {
    param([string]$Hint)

    if ($Hint) {
        if (-not (Get-Command $Hint -ErrorAction SilentlyContinue)) {
            throw "Интерпретатор не найден: $Hint"
        }
        return ,@($Hint)
    }

    $candidates = @(
        @('py', '-3.12'), @('py', '-3.11'), @('py', '-3.10'), @('py', '-3'),
        @('python3'), @('python')
    )
    foreach ($c in $candidates) {
        $exe = $c[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $extra = @()
        if ($c.Count -gt 1 -and $c[1]) { $extra = @($c[1]) }
        $probe = & $exe (@($extra) + @('-c', 'import sys; print("%d.%d" % sys.version_info[:2])')) 2>$null |
                 Select-Object -Last 1
        if ($LASTEXITCODE -eq 0 -and $probe -match '^(\d+)\.(\d+)$') {
            if ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 9) {
                return ,(@($exe) + $extra)
            }
        }
    }
    return $null
}

$py = Get-PythonCommand -Hint $Python
if (-not $py) {
    Write-Host "Не найден Python 3.9+. Установите его с https://www.python.org/downloads/windows/" -ForegroundColor Red
    Write-Host "и повторите запуск (можно указать путь: -Python `"C:\Python312\python.exe`")." -ForegroundColor Red
    exit 1
}

$pyExe = $py[0]
$pyArgs = @()
if ($py.Count -gt 1) { $pyArgs = $py[1..($py.Count - 1)] }
$pyVersion = & $pyExe (@($pyArgs) + @('-c', 'import sys; print(sys.version.split()[0])'))
Write-Host "Python: $pyExe $($pyArgs -join ' ') -> $pyVersion"

$Venv = Join-Path $Root ".venv"
if ($Recreate -and (Test-Path $Venv)) {
    Write-Host "Удаляю старое окружение .venv ..."
    Remove-Item $Venv -Recurse -Force
}

$VenvPython = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "Создаю виртуальное окружение .venv ..."
    & $pyExe (@($pyArgs) + @('-m', 'venv', $Venv))
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать виртуальное окружение" }
}

Write-Host "Обновляю pip ..."
& $VenvPython -m pip install --disable-pip-version-check --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Не удалось обновить pip" }

Write-Host "Устанавливаю зависимости из requirements.txt ..."
& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости" }

Write-Host "Проверяю драйвер SQLCipher ..." -ForegroundColor Cyan
& $VenvPython -c "import sqlcipher3, sqlite3 as _s; c = sqlcipher3.connect(':memory:'); print('  sqlcipher3 ->', c.execute('PRAGMA cipher_version').fetchone()[0]); print('  sqlite     ->', c.execute('SELECT sqlite_version()').fetchone()[0])"

Write-Host ""
Write-Host "Готово. Как запускать:" -ForegroundColor Green
Write-Host "  .\run.ps1              — интерактивный дневник"
Write-Host "  .\run.ps1 tui          — полноэкранный интерфейс"
Write-Host "  .\run.ps1 doctor       — диагностика"
Write-Host ""

$DbDefault = Join-Path $env:USERPROFILE ".diary.db"
if (Test-Path $DbDefault) {
    Write-Host "Найдена существующая база: $DbDefault" -ForegroundColor Yellow
    Write-Host "Перед первым запуском стоит сделать копию." -ForegroundColor Yellow
}
