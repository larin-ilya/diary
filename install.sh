#!/usr/bin/env bash
# Установка дневника на Linux (и macOS, хотя macOS вне рамок задачи).
# Создаёт .venv рядом с проектом и ставит зависимости из requirements.txt.
# Файл базы не изменяется.
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(pwd)"
PY="${PYTHON:-python3}"

echo "== Дневник: установка на Linux =="
echo "Папка проекта: $ROOT"

if ! command -v "$PY" >/dev/null 2>&1; then
    echo "Не найден интерпретатор '$PY'." >&2
    echo "Установите Python 3.9+ (например: sudo apt install python3 python3-venv), затем повторите." >&2
    exit 1
fi

# Проверяем версию и наличие модуля venv.
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "Нужен Python 3.9 или новее. Найден: $("$PY" -V)" >&2
    exit 1
fi
if ! "$PY" -c 'import venv' >/dev/null 2>&1; then
    echo "Нет модуля venv. Установите пакет python3-venv и повторите." >&2
    exit 1
fi

echo "Python: $PY -> $("$PY" -c 'import sys; print(sys.version.split()[0])')"

if [ ! -x ".venv/bin/python" ]; then
    echo "Создаю виртуальное окружение .venv ..."
    "$PY" -m venv .venv
fi

echo "Обновляю pip ..."
./.venv/bin/python -m pip install --disable-pip-version-check --quiet --upgrade pip

echo "Устанавливаю зависимости из requirements.txt ..."
./.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt

echo "Проверяю драйвер SQLCipher ..."
./.venv/bin/python - <<'PY'
import sqlcipher3
c = sqlcipher3.connect(":memory:")
print("  sqlcipher3 ->", c.execute("PRAGMA cipher_version").fetchone()[0])
print("  sqlite     ->", c.execute("SELECT sqlite_version()").fetchone()[0])
PY

echo
echo "Готово. Как запускать:"
echo "  ./run.sh              — интерактивный дневник"
echo "  ./run.sh tui          — полноэкранный интерфейс"
echo "  ./run.sh doctor       — диагностика"
echo

DB_DEFAULT="$HOME/.diary.db"
if [ -f "$DB_DEFAULT" ]; then
    echo "Найдена существующая база: $DB_DEFAULT"
    echo "Перед первым запуском стоит сделать копию."
fi
