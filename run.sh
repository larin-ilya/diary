#!/usr/bin/env bash
# Запуск дневника на Linux через виртуальное окружение .venv.
#   ./run.sh              — интерактивный CLI
#   ./run.sh tui          — полноэкранный интерфейс
#   ./run.sh doctor       — диагностика базы и окружения
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -x ".venv/bin/python" ]; then
    echo "Виртуальное окружение не найдено. Сначала выполните:" >&2
    echo "    bash ./install.sh" >&2
    exit 1
fi

exec ./.venv/bin/python TUI.py "$@"
