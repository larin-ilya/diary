#requires -Version 5.1
<#
    Запуск дневника на Windows через виртуальное окружение .venv.

        .\run.ps1              — интерактивный CLI
        .\run.ps1 tui          — полноэкранный интерфейс
        .\run.ps1 doctor       — диагностика базы и окружения
        .\run.ps1 add --help   — справка по неинтерактивным командам

    Если запуск .ps1 запрещён политикой, используйте:
        powershell -ExecutionPolicy Bypass -File .\run.ps1 tui
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "Виртуальное окружение не найдено: $VenvPython" -ForegroundColor Red
    Write-Host "Сначала выполните установку:" -ForegroundColor Yellow
    Write-Host "    powershell -ExecutionPolicy Bypass -File .\install.ps1" -ForegroundColor Yellow
    exit 1
}

& $VenvPython (Join-Path $Root "TUI.py") @Rest
exit $LASTEXITCODE
