#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Дневник на SQLite + SQLCipher с синхронизацией Google Drive.

Интерактивный CLI:   python TUI.py
Полноэкранный TUI:   python TUI.py tui
Скриптовый режим:    python TUI.py <команда> [параметры]      (см. python TUI.py --help)

Кросс-платформенный драйвер шифрованной SQLite — пакет ``sqlcipher3`` версии 0.6.x:
он публикует готовые wheel-файлы и для Windows (win_amd64/win32/win_arm64),
и для Linux (manylinux/musllinux). Устаревший ``sqlcipher3-binary`` колёс для
Windows не имеет, поэтому на Windows не устанавливается.
"""

import sys
import os
import re
import pickle
import textwrap
import datetime
import getpass
import tempfile
import subprocess
import shutil
from pathlib import Path

try:
    import curses
    HAS_CURSES = True
except ImportError:
    curses = None
    HAS_CURSES = False

try:
    import sqlcipher3 as sqlite3
    SQLCIPHER_DRIVER = "sqlcipher3"
except ImportError:  # очень старые окружения (Linux/macOS)
    try:
        from pysqlcipher3 import dbapi2 as sqlite3
        SQLCIPHER_DRIVER = "pysqlcipher3"
    except ImportError:
        sys.stderr.write(
            "Ошибка: не найден драйвер SQLCipher.\n"
            "Установите кросс-платформенный пакет (работает и на Windows, и на Linux):\n"
            "    python -m pip install \"sqlcipher3==0.6.2\"\n"
            "Пакет sqlcipher3-binary на Windows колёс не имеет и там не установится.\n"
        )
        sys.exit(1)

# --- Google Drive API (опционально) ---
try:
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
    HAS_GDRIVE = True
except ImportError:
    HAS_GDRIVE = False

def _resolve_db_path(env_value=None):
    """Путь к базе: DIARY_DB из окружения либо историческое ~/.diary.db."""
    if env_value is None:
        env_value = os.environ.get("DIARY_DB")
    return Path(env_value).expanduser() if env_value else (Path.home() / ".diary.db")


DB_PATH = _resolve_db_path()
DATE_FMT = "%Y-%m-%d %H:%M:%S"


def _enable_windows_ansi():
    """Включает ANSI-цвета в консоли Windows. На Linux — ничего не делает."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        for handle in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            h = kernel32.GetStdHandle(handle)
            if h in (0, -1):
                continue
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING
                kernel32.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


_enable_windows_ansi()
USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

# --- Настройки Google Drive ---
GDRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
GDRIVE_CLIENT_SECRET = "client_secret.json"
GDRIVE_TOKEN_FILE = str(Path.home() / ".diary_token.pickle")
GDRIVE_REMOTE_NAME = "diary.db"


# ------------------------- Утилиты CLI -------------------------

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITAL = "\033[3m"
    UNDER = "\033[4m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"


def c(text, color):
    return text if not USE_COLOR else f"{color}{text}{C.RESET}"


def now_str():
    return datetime.datetime.now().strftime(DATE_FMT)


def parse_tags(raw):
    parts = [t.strip().lower() for t in raw.split(",") if t.strip()]
    seen = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return ",".join(seen)


def _rule(width=58, title=None):
    if title:
        left = "─" * 2
        right = "─" * max(0, width - len(title) - len(left) - 2)
        return c(left + " " + title + " " + right, C.BLUE)
    return c("─" * width, C.BLUE)


# ------------------------- БД -------------------------

def _apply_key(conn, password):
    """Задаёт ключ: PRAGMA key не принимает placeholder'ы, поэтому экранируем кавычки."""
    safe = password.replace("'", "''")
    conn.execute(f"PRAGMA key = '{safe}';")


def _can_read(conn):
    try:
        conn.execute("SELECT count(*) FROM sqlite_master;")
        return True
    except Exception:
        return False


def _silence_sqlcipher_log(conn):
    """SQLCipher по умолчанию печатает свои диагностики в stdout — это ломает
    машиночитаемый вывод и пугает пользователя. Гасим логи до работы с ключом."""
    for pragma in ("PRAGMA cipher_log_level = NONE;", "PRAGMA cipher_log = stderr;"):
        try:
            conn.execute(pragma)
        except Exception:
            pass


def get_connection(password=None, db_path=None):
    """Открывает (при необходимости создаёт) зашифрованную базу.

    Пароль берётся из аргумента либо из переменной окружения DIARY_PASSWORD.
    Если база создана старой SQLCipher 3.x, она автоматически переоткрывается
    с PRAGMA cipher_compatibility, чтобы старые записи не остались недоступны.
    """
    path = Path(db_path).expanduser() if db_path else DB_PATH
    if password is None:
        password = os.environ.get("DIARY_PASSWORD")
    if password is None:
        if not sys.stdin.isatty():
            sys.stderr.write(
                "Ошибка: пароль не задан, а интерактивный ввод недоступен.\n"
                "Задайте переменную окружения DIARY_PASSWORD или запустите из терминала.\n"
            )
            sys.exit(1)
        password = getpass.getpass("Пароль базы дневника: ")

    if path.parent and str(path.parent) not in ("", ".") and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    _silence_sqlcipher_log(conn)
    _apply_key(conn, password)
    if _can_read(conn):
        return conn

    # Фолбэк для баз, созданных SQLCipher 3.x (другие параметры KDF).
    for compat in (3, 2, 1):
        try:
            conn.execute(f"PRAGMA cipher_compatibility = {compat};")
            _apply_key(conn, password)
        except Exception:
            continue
        if _can_read(conn):
            sys.stderr.write("  (база открыта в режиме совместимости SQLCipher %d)\n" % compat)
            return conn

    sys.stderr.write("Неверный пароль или база повреждена: %s\n" % path)
    sys.exit(1)


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            content TEXT NOT NULL,
            tags TEXT DEFAULT '',
            mood INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON entries(created_at)")
    conn.commit()


# =========================================================
#                 Google Drive синхронизация
# =========================================================

class GoogleDriveSync:
    """Простой клиент для синхронизации файла базы данных с Google Drive."""

    def __init__(self, client_secret=GDRIVE_CLIENT_SECRET, token_file=GDRIVE_TOKEN_FILE):
        self.client_secret = client_secret
        self.token_file = token_file
        self.service = None

    def _authenticate(self):
        creds = None
        if os.path.exists(self.token_file):
            with open(self.token_file, "rb") as token:
                creds = pickle.load(token)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self.client_secret):
                    raise FileNotFoundError(
                        f"Не найден файл «{self.client_secret}». "
                        "Скачайте его из Google Cloud Console."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.client_secret, GDRIVE_SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "wb") as token:
                pickle.dump(creds, token)

        return build("drive", "v3", credentials=creds)

    def connect(self):
        if self.service is None:
            self.service = self._authenticate()
        return self.service

    def find_file(self, name):
        """Возвращает список файлов с указанным именем (не в корзине)."""
        service = self.connect()
        query = f"name='{name}' and trashed=false"
        results = service.files().list(
            q=query, spaces="drive", fields="files(id, name, modifiedTime, size)"
        ).execute()
        return results.get("files", [])

    def upload_file(self, local_path, remote_name=None, file_id=None):
        """Загружает или обновляет файл на Google Drive."""
        service = self.connect()
        remote_name = remote_name or Path(local_path).name
        media = MediaFileUpload(str(local_path), resumable=True)

        if file_id:
            file = service.files().update(
                fileId=file_id, media_body=media, fields="id, name, modifiedTime"
            ).execute()
        else:
            metadata = {"name": remote_name}
            file = service.files().create(
                body=metadata, media_body=media, fields="id, name, modifiedTime"
            ).execute()
        return file

    def download_file(self, file_id, local_path):
        """Скачивает файл с Google Drive по ID."""
        service = self.connect()
        request = service.files().get_media(fileId=file_id)
        with open(local_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return local_path


def gdrive_sync(conn, direction="both"):
    """
    Синхронизация базы с Google Drive.

    direction:
        'up'   — только загрузить локальный файл на Диск
        'down' — только скачать файл с Диска (перезапишет локальный!)
        'both' — интерактивно выбрать действие
    """
    if not HAS_GDRIVE:
        print(c("  Google Drive API не установлен.", C.RED))
        print(c("  Установите: pip install google-api-python-client "
                "google-auth-oauthlib google-auth-httplib2", C.DIM))
        return

    try:
        gd = GoogleDriveSync()
    except Exception as e:
        print(c(f"  Ошибка инициализации: {e}", C.RED))
        return

    # Ищем существующий файл на Диске
    try:
        remote_files = gd.find_file(GDRIVE_REMOTE_NAME)
    except FileNotFoundError as e:
        print(c(f"  {e}", C.RED))
        return
    except Exception as e:
        print(c(f"  Ошибка доступа к Диску: {e}", C.RED))
        return

    remote = remote_files[0] if remote_files else None

    print()
    print(_rule(title="Синхронизация с Google Drive"))
    if remote:
        print(c("  Файл на Диске: ", C.DIM)
              + f"{remote['name']}  (изменён {remote.get('modifiedTime', '?')})")
    else:
        print(c("  На Диске файла ещё нет.", C.DIM))

    local_mtime = datetime.datetime.fromtimestamp(DB_PATH.stat().st_mtime)
    print(c("  Локальный файл: ", C.DIM)
          + f"{DB_PATH.name}  (изменён {local_mtime.strftime(DATE_FMT)})")

    if direction == "both":
        print()
        print("  Выберите действие:")
        print(c("    1", C.CYAN) + " — загрузить локальный файл на Диск")
        print(c("    2", C.CYAN) + " — скачать файл с Диска (перезапишет локальный)")
        print(c("    3", C.CYAN) + " — отмена")
        choice = input(c("  Ваш выбор [1/2/3]: ", C.CYAN)).strip()
        if choice == "1":
            direction = "up"
        elif choice == "2":
            direction = "down"
        else:
            print("  Отменено.")
            return

    # Перед загрузкой — commit, чтобы все данные были записаны на диск
    try:
        conn.commit()
    except Exception:
        pass

    if direction == "up":
        try:
            result = gd.upload_file(
                DB_PATH,
                remote_name=GDRIVE_REMOTE_NAME,
                file_id=remote["id"] if remote else None,
            )
            print(c(f"  ✓ Загружено на Диск: {result['name']} "
                    f"({result.get('modifiedTime', '')})", C.GREEN))
        except Exception as e:
            print(c(f"  Ошибка загрузки: {e}", C.RED))

    elif direction == "down":
        if not remote:
            print(c("  На Диске нет файла для скачивания.", C.YELLOW))
            return
        print(c("  ⚠ Локальный файл будет перезаписан!", C.YELLOW))
        if input(c("  Подтвердить (y/n): ", C.YELLOW)).lower() != "y":
            print("  Отменено.")
            return
        # Делаем резервную копию локального файла на всякий случай
        backup = DB_PATH.with_suffix(".db.bak")
        try:
            shutil.copy2(DB_PATH, backup)
            print(c(f"  Резервная копия: {backup}", C.DIM))
        except Exception:
            pass
        try:
            gd.download_file(remote["id"], DB_PATH)
            print(c("  ✓ Файл скачан с Диска. Перезапустите дневник, "
                    "чтобы увидеть изменения.", C.GREEN))
        except Exception as e:
            print(c(f"  Ошибка скачивания: {e}", C.RED))


# =========================================================
#                       CLI
# =========================================================

def read_multiline(prompt):
    if prompt:
        print(prompt)
    print(c("  (пустая строка — конец ввода; Ctrl+D — отмена)", C.DIM))
    lines = []
    while True:
        try:
            line = input(c("│ ", C.BLUE))
        except EOFError:
            break
        if line == "":
            break
        lines.append(line)
    return "\n".join(lines)


def format_entry_header(row, preview_len=72):
    id_, created, updated, tags, mood, preview = row[:6]
    preview = preview.replace("\n", " ").strip()
    if len(preview) > preview_len:
        preview = preview[:preview_len] + "…"
    mood_str = ""
    if mood is not None:
        bar = "★" * mood + "☆" * (10 - mood)
        mood_str = "  " + c(bar, C.YELLOW)
    tags_str = "  " + c("[" + tags + "]", C.CYAN) if tags else ""
    edited = c(" ✎", C.DIM) if updated != created else ""
    return (f"  {c('#' + str(id_), C.BOLD)}  {c(created[:16], C.GREEN)}{edited}"
            f"{tags_str}{mood_str}\n    {c(preview, C.DIM)}")


def cli_add(conn):
    content = read_multiline(c("✎ Новая запись:", C.BOLD))
    if not content.strip():
        print(c("  Пустая запись не добавлена.", C.YELLOW))
        return
    tags = parse_tags(input(c("  Теги (через запятую): ", C.CYAN)))
    mood_str = input(c("  Настроение 1–10: ", C.CYAN)).strip()
    mood = int(mood_str) if mood_str.isdigit() and 1 <= int(mood_str) <= 10 else None
    ts = now_str()
    conn.execute(
        "INSERT INTO entries (created_at, updated_at, content, tags, mood) VALUES (?,?,?,?,?)",
        (ts, ts, content, tags, mood),
    )
    conn.commit()
    print(c("  ✓ Запись добавлена.", C.GREEN))


def cli_list(conn, limit=20, tag=None, date_from=None, date_to=None):
    where, params = [], []
    if tag:
        where.append("(',' || tags || ',') LIKE ?")
        params.append(f"%,{tag.lower()},%")
    if date_from:
        where.append("created_at >= ?")
        params.append(date_from + " 00:00:00")
    if date_to:
        where.append("created_at <= ?")
        params.append(date_to + " 23:59:59")
    sql = "SELECT id, created_at, updated_at, tags, mood, substr(content,1,200) FROM entries"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print(c("  Записей не найдено.", C.YELLOW))
        return
    filter_info = []
    if tag: filter_info.append(f"тег={tag}")
    if date_from: filter_info.append(f"с {date_from}")
    if date_to: filter_info.append(f"по {date_to}")
    title = f"Записи: {len(rows)}" + (f"  ({', '.join(filter_info)})" if filter_info else "")
    print()
    print(_rule(title=title))
    for r in rows:
        print(format_entry_header(r))
        print()


def cli_view(conn, entry_id):
    row = conn.execute(
        "SELECT created_at, updated_at, content, tags, mood FROM entries WHERE id=?",
        (entry_id,),
    ).fetchone()
    if not row:
        print(c("  Запись не найдена.", C.RED))
        return
    created, updated, content, tags, mood = row
    width = max(50, min(shutil.get_terminal_size((80, 20)).columns - 4, 72))
    print()
    print(c("╭─ ", C.BLUE) + c(f"Запись #{entry_id}", C.BOLD)
          + c(" " + "─" * max(0, width - len(str(entry_id)) - 12), C.BLUE))
    print(c("│ ", C.BLUE) + c("Создана: ", C.DIM) + created)
    if updated != created:
        print(c("│ ", C.BLUE) + c("Изменена: ", C.DIM) + updated)
    if tags:
        print(c("│ ", C.BLUE) + c("Теги:    ", C.DIM) + c(tags, C.CYAN))
    if mood is not None:
        bar = "★" * mood + "☆" * (10 - mood)
        print(c("│ ", C.BLUE) + c("Настроение: ", C.DIM) + c(bar, C.YELLOW) + f" ({mood}/10)")
    print(c("╰" + "─" * width, C.BLUE))
    for line in content.split("\n"):
        print("  " + line)
    print(c("─" * (width + 1), C.BLUE))


def cli_edit(conn):
    try:
        entry_id = int(input(c("ID записи: ", C.CYAN)))
    except ValueError:
        print(c("  Введите число.", C.RED))
        return
    row = conn.execute(
        "SELECT content, tags, mood FROM entries WHERE id=?", (entry_id,)
    ).fetchone()
    if not row:
        print(c("  Запись не найдена.", C.RED))
        return
    old_content, old_tags, old_mood = row
    print(_rule(title="Текущий текст"))
    for line in old_content.split("\n"):
        print(c("  │ ", C.DIM) + line)
    print(_rule(title="Новый текст"))
    new_content = read_multiline("")
    if not new_content.strip():
        new_content = old_content
    tags_input = input(c(f"  Теги [{old_tags or '—'}]: ", C.CYAN)).strip()
    new_tags = parse_tags(tags_input) if tags_input else old_tags
    mood_input = input(c(f"  Настроение [{old_mood if old_mood is not None else '—'}]: ", C.CYAN)).strip()
    if mood_input == "":
        new_mood = old_mood
    elif mood_input.isdigit() and 1 <= int(mood_input) <= 10:
        new_mood = int(mood_input)
    else:
        new_mood = old_mood
    conn.execute(
        "UPDATE entries SET content=?, tags=?, mood=?, updated_at=? WHERE id=?",
        (new_content, new_tags, new_mood, now_str(), entry_id),
    )
    conn.commit()
    print(c("  ✓ Запись обновлена.", C.GREEN))


def cli_search(conn, query):
    q = f"%{query}%"
    rows = conn.execute(
        "SELECT id, created_at, updated_at, tags, mood, substr(content,1,200) "
        "FROM entries WHERE content LIKE ? OR tags LIKE ? ORDER BY created_at DESC",
        (q, q),
    ).fetchall()
    if not rows:
        print(c("  Ничего не найдено.", C.YELLOW))
        return
    print()
    print(_rule(title=f"Найдено: {len(rows)}  (запрос: «{query}»)"))
    for r in rows:
        print(format_entry_header(r))
        print()


def cli_delete(conn, entry_id=None):
    if entry_id is None:
        try:
            entry_id = int(input(c("ID записи для удаления: ", C.CYAN)))
        except ValueError:
            print(c("  Введите число.", C.RED))
            return
    row = conn.execute(
        "SELECT substr(content,1,60) FROM entries WHERE id=?", (entry_id,)
    ).fetchone()
    if not row:
        print(c("  Запись не найдена.", C.RED))
        return
    prev = row[0].replace("\n", " ")
    print(c(f"  Удалить #{entry_id}: «{prev}…»?", C.YELLOW))
    if input(c("  Подтвердить (y/n): ", C.YELLOW)).lower() == "y":
        conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
        conn.commit()
        print(c("  ✓ Удалено.", C.GREEN))
    else:
        print("  Отменено.")


def cli_stats(conn):
    total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    if total == 0:
        print(c("  Записей пока нет.", C.YELLOW))
        return
    avg = conn.execute("SELECT AVG(mood) FROM entries WHERE mood IS NOT NULL").fetchone()[0]
    first = conn.execute("SELECT MIN(created_at) FROM entries").fetchone()[0]
    last = conn.execute("SELECT MAX(created_at) FROM entries").fetchone()[0]
    print()
    print(c("╭─ Статистика " + "─" * 36, C.BLUE))
    print(c("│ ", C.BLUE) + f"Всего записей:       {c(str(total), C.BOLD)}")
    print(c("│ ", C.BLUE) + f"Первая запись:       {first}")
    print(c("│ ", C.BLUE) + f"Последняя запись:    {last}")
    if avg is not None:
        bar = "★" * round(avg) + "☆" * (10 - round(avg))
        print(c("│ ", C.BLUE) + f"Среднее настроение:  {c(bar, C.YELLOW)} ({avg:.2f}/10)")
    print(c("╰" + "─" * 50, C.BLUE))

    rows = conn.execute("SELECT tags FROM entries WHERE tags != ''").fetchall()
    counter = {}
    for (tags,) in rows:
        for t in tags.split(","):
            t = t.strip()
            if t:
                counter[t] = counter.get(t, 0) + 1
    if counter:
        print(c("\nТоп тегов:", C.BOLD))
        top = sorted(counter.items(), key=lambda x: -x[1])[:10]
        max_cnt = top[0][1] if top else 1
        for tag, cnt in top:
            bar = "▰" * max(1, int(cnt / max_cnt * 20))
            print(f"  {c(tag.ljust(14), C.CYAN)} {c(bar, C.MAGENTA)} {cnt}")

    print(c("\nАктивность по месяцам:", C.BOLD))
    months = conn.execute(
        "SELECT substr(created_at,1,7) AS m, COUNT(*) FROM entries "
        "GROUP BY m ORDER BY m DESC LIMIT 12"
    ).fetchall()
    if months:
        max_m = max(cnt for _, cnt in months)
        for m, cnt in months:
            bar = "█" * max(1, int(cnt / max_m * 30))
            print(f"  {m}  {c(bar, C.GREEN)} {cnt}")


def cli_tags(conn):
    rows = conn.execute("SELECT tags FROM entries WHERE tags != ''").fetchall()
    counter = {}
    for (tags,) in rows:
        for t in tags.split(","):
            t = t.strip()
            if t:
                counter[t] = counter.get(t, 0) + 1
    if not counter:
        print(c("  Тегов пока нет.", C.YELLOW))
        return
    print()
    print(_rule(title=f"Все теги ({len(counter)})"))
    for tag, cnt in sorted(counter.items()):
        print(f"  {c(tag, C.CYAN)}  {c('· ' + str(cnt), C.DIM)}")


def cli_export(conn):
    default_name = f"diary_export_{datetime.date.today().isoformat()}.md"
    filename = input(c(f"  Имя файла [{default_name}]: ", C.CYAN)).strip() or default_name
    rows = conn.execute(
        "SELECT id, created_at, updated_at, content, tags, mood "
        "FROM entries ORDER BY created_at ASC"
    ).fetchall()
    if not rows:
        print(c("  Нечего экспортировать.", C.YELLOW))
        return
    with open(filename, "w", encoding="utf-8") as f:
        f.write("# Дневник\n\n")
        f.write(f"*Экспортировано: {now_str()}*\n\n---\n\n")
        for id_, created, updated, content, tags, mood in rows:
            f.write(f"## {created} (#{id_})\n\n")
            if tags:
                f.write(f"**Теги:** {tags}  \n")
            if mood is not None:
                f.write(f"**Настроение:** {mood}/10  \n")
            if updated != created:
                f.write(f"*Изменено: {updated}*\n")
            f.write("\n" + content + "\n\n---\n\n")
    print(c(f"  ✓ Экспортировано в «{filename}» ({len(rows)} записей).", C.GREEN))


def cli_import(conn):
    filename = input(c("  Имя файла для импорта: ", C.CYAN)).strip()
    if not os.path.exists(filename):
        print(c("  Файл не найден.", C.RED))
        return
    with open(filename, "r", encoding="utf-8") as f:
        text = f.read()
    chunks = re.split(r"\n---+\n", text)
    added = 0
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk or chunk.startswith("# Дневник"):
            continue
        m = re.match(r"##\s+(\d{4}-\d{2}-\d{2}[^\n]*)\n(.+)", chunk, re.DOTALL)
        if not m:
            continue
        date_str = m.group(1).strip()
        body = m.group(2).strip()
        tags_match = re.search(r"\*\*Теги:\*\*\s*(.+)", body)
        tags = parse_tags(tags_match.group(1)) if tags_match else ""
        mood_match = re.search(r"\*\*Настроение:\*\*\s*(\d+)/10", body)
        mood = int(mood_match.group(1)) if mood_match else None
        content = re.sub(r"\*\*Теги:\*\*.*\n?", "", body)
        content = re.sub(r"\*\*Настроение:\*\*.*\n?", "", content)
        content = content.strip()
        try:
            ts = datetime.datetime.strptime(date_str[:19], DATE_FMT).strftime(DATE_FMT)
        except ValueError:
            try:
                ts = datetime.datetime.strptime(date_str[:10], "%Y-%m-%d").strftime(DATE_FMT)
            except ValueError:
                ts = now_str()
        conn.execute(
            "INSERT INTO entries (created_at, updated_at, content, tags, mood) VALUES (?,?,?,?,?)",
            (ts, ts, content, tags, mood),
        )
        added += 1
    conn.commit()
    print(c(f"  ✓ Импортировано {added} записей.", C.GREEN))


def cli_backup(conn):
    default_name = f"diary_backup_{datetime.date.today().isoformat()}.db"
    filename = input(c(f"  Имя файла резервной копии [{default_name}]: ", C.CYAN)).strip() or default_name
    conn.commit()
    try:
        shutil.copy2(DB_PATH, filename)
        print(c(f"  ✓ Резервная копия сохранена в «{filename}».", C.GREEN))
    except Exception as e:
        print(c(f"  Ошибка при копировании: {e}", C.RED))


def print_help():
    print()
    print(c("╭─ Команды " + "─" * 46, C.BLUE))
    rows = [
        ("add, a", "добавить запись"),
        ("list, l", "последние 20 записей"),
        ("view, v", "открыть запись по ID"),
        ("edit", "редактировать запись"),
        ("search, s", "поиск по тексту и тегам"),
        ("tag, t", "фильтр по тегу"),
        ("date", "фильтр по диапазону дат"),
        ("tags", "список всех тегов"),
        ("delete, d, rm", "удалить запись"),
        ("stats, st", "статистика"),
        ("export, ex", "выгрузить в Markdown"),
        ("import, im", "импорт из Markdown"),
        ("backup, b", "резервная копия файла базы"),
        ("gdrive, sync", "синхронизация с Google Drive"),
        ("tui", "открыть визуальный интерфейс"),
        ("add --help", "неинтерактивные команды (для скриптов и CI)"),
        ("help, ?, h", "эта справка"),
        ("quit, q, exit", "выход"),
    ]
    for k, v in rows:
        print(c("│ ", C.BLUE) + c(k.ljust(16), C.CYAN) + v)
    print(c("╰" + "─" * 56, C.BLUE))


def cli_main(conn):
    print(c("📔  Дневник — SQLite + SQLCipher", C.BOLD))
    print(c("   Введите ", C.DIM) + c("help", C.CYAN) + c(" для списка команд.", C.DIM))
    while True:
        try:
            cmd = input(c("\n❯ ", C.GREEN)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nВыход.")
            break
        if cmd in ("add", "a"):
            cli_add(conn)
        elif cmd in ("list", "l", "ls"):
            cli_list(conn, limit=20)
        elif cmd in ("view", "v"):
            try:
                cli_view(conn, int(input(c("  ID: ", C.CYAN))))
            except ValueError:
                print(c("  Введите число.", C.RED))
        elif cmd in ("edit",):
            cli_edit(conn)
        elif cmd in ("search", "s"):
            cli_search(conn, input(c("  Поиск: ", C.CYAN)))
        elif cmd in ("tag", "t"):
            cli_tags(conn)
            tag = input(c("  Тег для фильтра: ", C.CYAN)).strip()
            if tag:
                cli_list(conn, limit=50, tag=tag)
        elif cmd in ("date",):
            df = input(c("  С даты (YYYY-MM-DD, пусто — без ограничения): ", C.CYAN)).strip()
            dt = input(c("  По дату (YYYY-MM-DD, пусто — без ограничения): ", C.CYAN)).strip()
            cli_list(conn, limit=100, date_from=df or None, date_to=dt or None)
        elif cmd in ("tags",):
            cli_tags(conn)
        elif cmd in ("delete", "d", "rm"):
            cli_delete(conn)
        elif cmd in ("stats", "st"):
            cli_stats(conn)
        elif cmd in ("export", "ex"):
            cli_export(conn)
        elif cmd in ("import", "im"):
            cli_import(conn)
        elif cmd in ("backup", "b"):
            cli_backup(conn)
        elif cmd in ("gdrive", "sync"):
            gdrive_sync(conn, direction="both")
        elif cmd in ("tui",):
            run_tui(conn)
        elif cmd in ("help", "?", "h"):
            print_help()
        elif cmd in ("quit", "exit", "q"):
            print("Выход.")
            break
        else:
            print(c("  Неизвестная команда. Введите help.", C.YELLOW))


# =========================================================
#                        TUI
# =========================================================

def run_tui(conn):
    if not sys.stdout.isatty():
        print(c("TUI требует интерактивный терминал.", C.RED))
        return
    if not HAS_CURSES:
        print(c("Не найден модуль curses. На Windows: pip install windows-curses", C.RED))
        return
    curses.wrapper(_tui_loop, conn)


def _safe_addstr(stdscr, y, x, text, attr=0):
    """addstr без исключений, если текст не влезает."""
    if not text:
        return
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    max_w = w - x - 1
    if max_w <= 0:
        return
    try:
        stdscr.addstr(y, x, text[:max_w], attr)
    except curses.error:
        pass


def _edit_in_editor(stdscr, initial=""):
    """Открыть внешний редактор, корректно усыпив и разбудив curses."""
    default_editor = "notepad" if os.name == "nt" else "nano"
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or default_editor
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(initial)
        path = f.name
    try:
        try:
            curses.def_prog_mode()
        except curses.error:
            pass
        curses.endwin()

        rc = 0
        try:
            rc = subprocess.call([editor, path])
        except FileNotFoundError:
            curses.reset_prog_mode()
            try:
                stdscr.refresh()
                curses.flushinp()
            except Exception:
                pass
            _tui_status(stdscr, f"Редактор «{editor}» не найден. Установите $EDITOR или nano.", 2)
            return initial
        except Exception as e:
            curses.reset_prog_mode()
            try:
                stdscr.refresh()
                curses.flushinp()
            except Exception:
                pass
            _tui_status(stdscr, f"Ошибка запуска редактора: {e}", 2)
            return initial
        finally:
            try:
                curses.reset_prog_mode()
                stdscr.refresh()
                stdscr.clear()
                curses.flushinp()
            except Exception:
                pass

        if rc != 0 and not os.path.exists(path):
            return initial
        with open(path, "r", encoding="utf-8") as f:
            return f.read().rstrip("\n")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _tui_prompt(stdscr, label, initial=""):
    h, w = stdscr.getmaxyx()
    curses.echo()
    curses.curs_set(1)
    _safe_addstr(stdscr, h - 1, 0, " " * (w - 1), curses.color_pair(2))
    _safe_addstr(stdscr, h - 1, 0, f" {label}: {initial}", curses.color_pair(2))
    stdscr.refresh()
    try:
        x = len(label) + 3 + len(initial)
        raw = stdscr.getstr(h - 1, x, min(200, max(1, w - x - 1)))
        value = raw.decode("utf-8").strip()
        if not value and initial:
            value = initial
    except Exception:
        value = initial
    curses.noecho()
    curses.curs_set(0)
    return value


def _tui_confirm(stdscr, message):
    h, w = stdscr.getmaxyx()
    _safe_addstr(stdscr, h - 1, 0, " " * (w - 1), curses.color_pair(3))
    _safe_addstr(stdscr, h - 1, 0, f" {message} (y/n): ", curses.color_pair(3))
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("n"), ord("N"), 27):
            return False


def _tui_status(stdscr, text, seconds=1):
    h, w = stdscr.getmaxyx()
    _safe_addstr(stdscr, h - 1, 0, " " * (w - 1), curses.color_pair(4))
    _safe_addstr(stdscr, h - 1, 0, f" {text}", curses.color_pair(4))
    stdscr.refresh()
    curses.napms(max(1, seconds) * 1000)


def _tui_init_colors():
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except Exception:
        bg = curses.COLOR_BLACK
    curses.init_pair(1, curses.COLOR_CYAN, bg)                    # заголовки / маркеры
    curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)    # верхняя панель / промпт
    curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_YELLOW)  # подтверждение
    curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_GREEN)   # успех
    curses.init_pair(5, curses.COLOR_YELLOW, bg)                  # настроение
    curses.init_pair(6, curses.COLOR_MAGENTA, bg)                 # теги
    curses.init_pair(7, curses.COLOR_WHITE, bg)                   # обычный текст


def _tui_fetch(conn, query=None, tag=None):
    where, params = [], []
    if query:
        where.append("(content LIKE ? OR tags LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])
    if tag:
        where.append("(',' || tags || ',') LIKE ?")
        params.append(f"%,{tag.lower()},%")
    sql = "SELECT id, created_at, updated_at, tags, mood, content FROM entries"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC"
    return conn.execute(sql, params).fetchall()


# --------- отрисовка ---------

def _tui_draw(stdscr, entries, selected, scroll, search_query=None, tag_filter=None, message=""):
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    title = " 📔  Дневник "
    if tag_filter:
        title += f"  🏷  {tag_filter} "
    if search_query:
        title += f"  🔍 «{search_query}» "
    title += f"  ·  {len(entries)} записей "
    _safe_addstr(stdscr, 0, 0, title.ljust(w - 1), curses.color_pair(2) | curses.A_BOLD)

    list_top = 1
    list_height = max(6, (h - 4) * 60 // 100)
    list_bottom = list_top + list_height
    visible_entries = max(1, list_height // 2)

    line = list_top
    for i in range(scroll, min(len(entries), scroll + visible_entries)):
        if line + 1 >= list_bottom:
            break
        eid, created, updated, tags, mood, content = entries[i]
        is_sel = (i == selected)

        marker = "▶" if is_sel else " "
        edited = "✎" if updated != created else " "
        header = f"{marker} #{eid:<4}  {created[:16]}  {edited}"

        base_attr = (curses.A_REVERSE | curses.A_BOLD) if is_sel else 0
        _safe_addstr(stdscr, line, 0, " " * (w - 1), base_attr)
        _safe_addstr(stdscr, line, 1, header, base_attr | curses.color_pair(1))

        x = 1 + len(header)
        if tags and x < w - 16:
            tag_str = f" [{tags}]"
            _safe_addstr(stdscr, line, x, tag_str, base_attr | curses.color_pair(6))

        if mood is not None:
            bar = "★" * mood + "☆" * (10 - mood)
            bar_x = max(x + (len(tags) + 3 if tags else 0), w - len(bar) - 3)
            _safe_addstr(stdscr, line, bar_x, bar, base_attr | curses.color_pair(5))

        line += 1

        preview = content.replace("\n", " ").strip()
        max_prev = w - 8
        if len(preview) > max_prev:
            preview = preview[:max_prev - 1] + "…"
        prev_attr = (curses.A_REVERSE if is_sel else curses.A_DIM)
        _safe_addstr(stdscr, line, 0, " " * (w - 1), curses.A_REVERSE if is_sel else 0)
        _safe_addstr(stdscr, line, 3, preview, prev_attr | curses.color_pair(7))
        line += 1

    if not entries:
        msg = " Здесь пока пусто. Нажмите «n», чтобы создать первую запись. "
        _safe_addstr(stdscr, list_top + 2, max(2, (w - len(msg)) // 2), msg,
                     curses.color_pair(5) | curses.A_BOLD)

    _safe_addstr(stdscr, list_bottom, 0, "─" * (w - 1), curses.color_pair(7) | curses.A_DIM)

    preview_top = list_bottom + 1
    preview_bottom = h - 2
    if entries and 0 <= selected < len(entries):
        eid, created, updated, tags, mood, content = entries[selected]
        hdr = f" Запись #{eid}  ·  {created} "
        if updated != created:
            hdr += f" (изм. {updated[:16]}) "
        _safe_addstr(stdscr, preview_top, 0, hdr.ljust(w - 1)[:w - 1],
                     curses.A_BOLD | curses.color_pair(1))

        meta_y = preview_top + 1
        if tags:
            _safe_addstr(stdscr, meta_y, 2, f"🏷  {tags}", curses.color_pair(6))
            meta_y += 1
        if mood is not None:
            bar = "★" * mood + "☆" * (10 - mood)
            _safe_addstr(stdscr, meta_y, 2, f"Настроение: {bar} ({mood}/10)",
                         curses.color_pair(5))
            meta_y += 1

        width = max(20, w - 4)
        wrapped = []
        for para in content.split("\n"):
            if not para.strip():
                wrapped.append("")
                continue
            wrapped.extend(textwrap.wrap(para, width) or [""])

        y = meta_y + 1
        for ln in wrapped:
            if y >= preview_bottom:
                break
            _safe_addstr(stdscr, y, 2, ln, curses.color_pair(7))
            y += 1

    if message:
        _safe_addstr(stdscr, h - 2, 0, f" {message}".ljust(w - 1)[:w - 1],
                     curses.color_pair(4))
    else:
        keys = (" ↑↓/jk  Enter открыть  n новая  e правка  d удалить  / поиск  "
                "t тег  g синхр.  Esc сброс  s стат  ? справка  q выход ")
        _safe_addstr(stdscr, h - 2, 0, keys.ljust(w - 1)[:w - 1], curses.A_REVERSE)

    stdscr.refresh()


def _tui_draw_view(stdscr, entry):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    eid, created, updated, tags, mood, content = entry
    header = f" 📖 Запись #{eid}  ·  {created} "
    if updated != created:
        header += f" (изм. {updated[:16]}) "
    _safe_addstr(stdscr, 0, 0, header.ljust(w - 1)[:w - 1],
                 curses.color_pair(2) | curses.A_BOLD)

    line = 2
    if tags:
        _safe_addstr(stdscr, line, 2, f"🏷  {tags}", curses.color_pair(6))
        line += 1
    if mood is not None:
        bar = "★" * mood + "☆" * (10 - mood)
        _safe_addstr(stdscr, line, 2, f"Настроение: {bar} ({mood}/10)",
                     curses.color_pair(5))
        line += 1
    line += 1

    width = max(20, w - 4)
    wrapped = []
    for para in content.split("\n"):
        if not para.strip():
            wrapped.append("")
        else:
            wrapped.extend(textwrap.wrap(para, width) or [""])

    for ln in wrapped:
        if line >= h - 2:
            break
        _safe_addstr(stdscr, line, 2, ln, curses.color_pair(7))
        line += 1

    _safe_addstr(stdscr, h - 2, 0,
                 " e редактировать  d удалить  Esc/q назад ".ljust(w - 1)[:w - 1],
                 curses.A_REVERSE)
    stdscr.refresh()


def _tui_draw_stats(stdscr, conn):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    _safe_addstr(stdscr, 0, 0, " 📊 Статистика ".ljust(w - 1)[:w - 1],
                 curses.color_pair(2) | curses.A_BOLD)
    line = 2
    total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    _safe_addstr(stdscr, line, 2, f"Всего записей: {total}")
    line += 1
    if total:
        first = conn.execute("SELECT MIN(created_at) FROM entries").fetchone()[0]
        last = conn.execute("SELECT MAX(created_at) FROM entries").fetchone()[0]
        avg = conn.execute("SELECT AVG(mood) FROM entries WHERE mood IS NOT NULL").fetchone()[0]
        _safe_addstr(stdscr, line, 2, f"Первая запись:  {first}"); line += 1
        _safe_addstr(stdscr, line, 2, f"Последняя:      {last}"); line += 1
        if avg is not None:
            bar = "★" * round(avg) + "☆" * (10 - round(avg))
            _safe_addstr(stdscr, line, 2, "Среднее настроение: ")
            _safe_addstr(stdscr, line, 22, f"{bar} ({avg:.2f}/10)", curses.color_pair(5))
            line += 1
        line += 1
        _safe_addstr(stdscr, line, 2, "Топ тегов:", curses.A_BOLD); line += 1
        rows = conn.execute("SELECT tags FROM entries WHERE tags != ''").fetchall()
        counter = {}
        for (t,) in rows:
            for x in t.split(","):
                x = x.strip()
                if x:
                    counter[x] = counter.get(x, 0) + 1
        top = sorted(counter.items(), key=lambda x: -x[1])[:10]
        max_cnt = top[0][1] if top else 1
        for tag, cnt in top:
            if line >= h - 3:
                break
            bar = "▰" * max(1, int(cnt / max_cnt * 20))
            _safe_addstr(stdscr, line, 4, tag.ljust(14), curses.color_pair(6))
            _safe_addstr(stdscr, line, 18, f"{bar} {cnt}", curses.color_pair(5))
            line += 1
        line += 1
        if line < h - 3:
            _safe_addstr(stdscr, line, 2, "Активность по месяцам:", curses.A_BOLD)
            line += 1
        months = conn.execute(
            "SELECT substr(created_at,1,7) AS m, COUNT(*) FROM entries "
            "GROUP BY m ORDER BY m DESC LIMIT 8"
        ).fetchall()
        if months:
            max_m = max(cnt for _, cnt in months)
            for m, cnt in months:
                if line >= h - 3:
                    break
                bar = "█" * max(1, int(cnt / max_m * max(10, w - 25)))
                _safe_addstr(stdscr, line, 4, f"{m} ")
                _safe_addstr(stdscr, line, 12, bar, curses.color_pair(4))
                _safe_addstr(stdscr, line, 13 + len(bar), f" {cnt}")
                line += 1
    _safe_addstr(stdscr, h - 2, 0, " Esc/q назад ".ljust(w - 1)[:w - 1], curses.A_REVERSE)
    stdscr.refresh()


def _tui_help_overlay(stdscr):
    h, w = stdscr.getmaxyx()
    lines = [
        ("НАВИГАЦИЯ", ""),
        ("  ↑ / k", "вверх"),
        ("  ↓ / j", "вниз"),
        ("  PgUp / PgDn", "на страницу"),
        ("  Home / End", "в начало / конец"),
        ("", ""),
        ("ДЕЙСТВИЯ", ""),
        ("  Enter", "открыть запись"),
        ("  n", "новая запись"),
        ("  e", "редактировать"),
        ("  d", "удалить"),
        ("  /", "поиск"),
        ("  t", "фильтр по тегу"),
        ("  g", "синхронизация с Google Drive"),
        ("  Esc", "сбросить фильтр"),
        ("  r", "обновить список"),
        ("  s", "статистика"),
        ("  ?", "эта справка"),
        ("  q", "выход"),
    ]
    box_h = len(lines) + 4
    box_w = 52
    box_y = max(0, (h - box_h) // 2)
    box_x = max(0, (w - box_w) // 2)

    for y in range(box_y, min(h, box_y + box_h)):
        _safe_addstr(stdscr, y, box_x, " " * min(box_w, w - box_x - 1),
                     curses.color_pair(2))

    _safe_addstr(stdscr, box_y, box_x, "┌" + "─" * (box_w - 2) + "┐", curses.color_pair(1))
    _safe_addstr(stdscr, box_y + box_h - 1, box_x, "└" + "─" * (box_w - 2) + "┘",
                 curses.color_pair(1))
    title = " Справка "
    _safe_addstr(stdscr, box_y, box_x + (box_w - len(title)) // 2, title,
                 curses.color_pair(1) | curses.A_BOLD)
    for i in range(box_h - 2):
        _safe_addstr(stdscr, box_y + 1 + i, box_x, "│", curses.color_pair(1))
        _safe_addstr(stdscr, box_y + 1 + i, box_x + box_w - 1, "│", curses.color_pair(1))

    for i, (k, d) in enumerate(lines):
        y = box_y + 1 + i
        if not k.startswith(" "):
            _safe_addstr(stdscr, y, box_x + 2, k, curses.color_pair(5) | curses.A_BOLD)
        else:
            _safe_addstr(stdscr, y, box_x + 2, k, curses.color_pair(6) | curses.A_BOLD)
            _safe_addstr(stdscr, y, box_x + 22, d, curses.color_pair(7))

    _safe_addstr(stdscr, box_y + box_h - 2, box_x + 2,
                 "Любая клавиша — вернуться", curses.color_pair(2))
    stdscr.refresh()
    stdscr.getch()


# --------- действия TUI ---------

def _tui_action_add(conn, stdscr):
    content = _edit_in_editor(stdscr, "")
    if not content.strip():
        _tui_status(stdscr, "Пустая запись — отменено")
        return
    tags = parse_tags(_tui_prompt(stdscr, "Теги (через запятую)"))
    mood_str = _tui_prompt(stdscr, "Настроение 1–10")
    mood = int(mood_str) if mood_str.isdigit() and 1 <= int(mood_str) <= 10 else None
    ts = now_str()
    conn.execute(
        "INSERT INTO entries (created_at, updated_at, content, tags, mood) VALUES (?,?,?,?,?)",
        (ts, ts, content, tags, mood),
    )
    conn.commit()
    _tui_status(stdscr, "✓ Запись добавлена")


def _tui_action_edit(conn, stdscr, entry):
    eid, created, updated, tags, mood, content = entry
    new_content = _edit_in_editor(stdscr, content)
    if not new_content.strip():
        _tui_status(stdscr, "Пустой текст — отменено")
        return
    new_tags = parse_tags(_tui_prompt(stdscr, "Теги", tags))
    mood_str = _tui_prompt(stdscr, "Настроение", str(mood) if mood else "")
    new_mood = int(mood_str) if mood_str.isdigit() and 1 <= int(mood_str) <= 10 else None
    conn.execute(
        "UPDATE entries SET content=?, tags=?, mood=?, updated_at=? WHERE id=?",
        (new_content, new_tags, new_mood, now_str(), eid),
    )
    conn.commit()
    _tui_status(stdscr, "✓ Запись обновлена")


def _tui_action_delete(conn, stdscr, entry):
    if _tui_confirm(stdscr, f"Удалить запись #{entry[0]}?"):
        conn.execute("DELETE FROM entries WHERE id=?", (entry[0],))
        conn.commit()
        _tui_status(stdscr, "✓ Удалено")


def _tui_action_sync(conn, stdscr):
    """Синхронизация с Google Drive внутри TUI."""
    if not HAS_GDRIVE:
        _tui_status(stdscr, "Google Drive API не установлен", 3)
        return

    # Временно выходим из curses, чтобы показать обычные приглашения
    curses.endwin()
    try:
        gdrive_sync(conn, direction="both")
    finally:
        try:
            curses.reset_prog_mode()
            stdscr.clear()
            stdscr.refresh()
            curses.flushinp()
        except Exception:
            pass
    _tui_status(stdscr, "Синхронизация завершена")


def _tui_sub_view(stdscr, conn, entry):
    while True:
        _tui_draw_view(stdscr, entry)
        ch = stdscr.getch()
        if ch in (ord("q"), 27):
            return
        elif ch == ord("e"):
            _tui_action_edit(conn, stdscr, entry)
            entry = conn.execute(
                "SELECT id, created_at, updated_at, tags, mood, content FROM entries WHERE id=?",
                (entry[0],),
            ).fetchone()
        elif ch == ord("d"):
            _tui_action_delete(conn, stdscr, entry)
            return


def _tui_sub_stats(stdscr, conn):
    while True:
        _tui_draw_stats(stdscr, conn)
        ch = stdscr.getch()
        if ch in (ord("q"), 27):
            return


def _tui_loop(stdscr, conn):
    curses.curs_set(0)
    _tui_init_colors()
    selected = 0
    scroll = 0
    search_query = None
    tag_filter = None
    entries = _tui_fetch(conn, search_query, tag_filter)

    def refetch(keep_pos=False):
        nonlocal entries
        old_id = entries[selected][0] if (keep_pos and entries and selected < len(entries)) else None
        entries = _tui_fetch(conn, search_query, tag_filter)
        if old_id is not None:
            for i, e in enumerate(entries):
                if e[0] == old_id:
                    return i
        return 0

    while True:
        if entries:
            selected = max(0, min(selected, len(entries) - 1))
        else:
            selected = 0
        h, _ = stdscr.getmaxyx()
        visible_entries = max(1, ((h - 4) * 60 // 100) // 2)
        if selected < scroll:
            scroll = selected
        if selected >= scroll + visible_entries:
            scroll = selected - visible_entries + 1

        _tui_draw(stdscr, entries, selected, scroll, search_query, tag_filter)
        ch = stdscr.getch()

        try:
            if ch in (ord("q"), 27):
                if search_query or tag_filter:
                    search_query = None
                    tag_filter = None
                    selected = 0
                    scroll = 0
                    entries = _tui_fetch(conn)
                    continue
                break
            elif ch in (curses.KEY_UP, ord("k")):
                selected -= 1
            elif ch in (curses.KEY_DOWN, ord("j")):
                selected += 1
            elif ch == curses.KEY_PPAGE:
                selected -= visible_entries
            elif ch == curses.KEY_NPAGE:
                selected += visible_entries
            elif ch == curses.KEY_HOME:
                selected = 0
            elif ch == curses.KEY_END:
                selected = len(entries) - 1 if entries else 0
            elif ch in (10, 13, curses.KEY_ENTER):
                if entries:
                    entry = conn.execute(
                        "SELECT id, created_at, updated_at, tags, mood, content FROM entries WHERE id=?",
                        (entries[selected][0],),
                    ).fetchone()
                    if entry:
                        _tui_sub_view(stdscr, conn, entry)
                        selected = refetch(keep_pos=True)
            elif ch == ord("n"):
                _tui_action_add(conn, stdscr)
                entries = _tui_fetch(conn, search_query, tag_filter)
            elif ch == ord("e"):
                if entries:
                    entry = conn.execute(
                        "SELECT id, created_at, updated_at, tags, mood, content FROM entries WHERE id=?",
                        (entries[selected][0],),
                    ).fetchone()
                    _tui_action_edit(conn, stdscr, entry)
                    selected = refetch(keep_pos=True)
            elif ch == ord("d"):
                if entries:
                    entry = conn.execute(
                        "SELECT id, created_at, updated_at, tags, mood, content FROM entries WHERE id=?",
                        (entries[selected][0],),
                    ).fetchone()
                    _tui_action_delete(conn, stdscr, entry)
                    entries = _tui_fetch(conn, search_query, tag_filter)
            elif ch == ord("/"):
                q = _tui_prompt(stdscr, "Поиск", search_query or "")
                search_query = q or None
                entries = _tui_fetch(conn, search_query, tag_filter)
                selected = 0
                scroll = 0
            elif ch == ord("t"):
                t = _tui_prompt(stdscr, "Тег", tag_filter or "")
                tag_filter = t or None
                entries = _tui_fetch(conn, search_query, tag_filter)
                selected = 0
                scroll = 0
            elif ch == ord("g"):
                _tui_action_sync(conn, stdscr)
                entries = _tui_fetch(conn, search_query, tag_filter)
            elif ch == ord("r"):
                selected = refetch(keep_pos=True)
            elif ch == ord("s"):
                _tui_sub_stats(stdscr, conn)
            elif ch == ord("?"):
                _tui_help_overlay(stdscr)
            elif ch == curses.KEY_RESIZE:
                stdscr.clear()
        except Exception as e:
            try:
                _tui_status(stdscr, f"Ошибка: {e}", 2)
            except Exception:
                pass
            entries = _tui_fetch(conn, search_query, tag_filter)
            continue


# =========================================================
#          Скриптовый (неинтерактивный) режим
# =========================================================
# Нужен для автоматических тестов и CI: можно создать/прочитать/удалить
# запись без интерактивного ввода. Интерактивный CLI не меняется.

def _script_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="TUI.py",
        description="Дневник: неинтерактивные команды (удобно для скриптов и CI).",
        epilog=("Без аргументов запускается интерактивный CLI, "
                "с аргументом «tui» — полноэкранный интерфейс."),
    )
    p.add_argument("--db", help="путь к файлу базы (по умолчанию DIARY_DB или ~/.diary.db)")
    p.add_argument("--password", help="пароль базы (по умолчанию из DIARY_PASSWORD)")
    p.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="добавить запись")
    a.add_argument("--content", help="текст записи")
    a.add_argument("--content-file", help="взять текст из файла (UTF-8)")
    a.add_argument("--tags", default="", help="теги через запятую")
    a.add_argument("--mood", type=int, choices=range(1, 11), help="настроение 1-10")

    l = sub.add_parser("list", help="список записей")
    l.add_argument("--limit", type=int, default=20)
    l.add_argument("--tag")
    l.add_argument("--since", dest="date_from", help="с даты YYYY-MM-DD")
    l.add_argument("--until", dest="date_to", help="по дату YYYY-MM-DD")

    v = sub.add_parser("view", help="показать запись")
    v.add_argument("id", type=int)

    s = sub.add_parser("search", help="поиск по тексту и тегам")
    s.add_argument("query")

    d = sub.add_parser("delete", help="удалить запись")
    d.add_argument("id", type=int)

    sub.add_parser("stats", help="статистика")
    sub.add_parser("tags", help="список тегов")

    e = sub.add_parser("export", help="экспорт в Markdown")
    e.add_argument("path")

    i = sub.add_parser("import", help="импорт из Markdown")
    i.add_argument("path")

    b = sub.add_parser("backup", help="резервная копия файла базы")
    b.add_argument("path")

    sub.add_parser("doctor", help="диагностика окружения и базы")
    sub.add_parser("tui", help="полноэкранный интерфейс (нужен настоящий терминал)")
    return p


def _json_out(enabled, payload, human):
    if enabled:
        import json
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    elif human is not None:
        print(human)


def cmd_doctor(args):
    import platform

    path = Path(args.db).expanduser() if args.db else DB_PATH
    info = {
        "os": platform.platform(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "driver": SQLCIPHER_DRIVER,
        "db_path": str(path),
        "db_exists": path.exists(),
        "db_size": path.stat().st_size if path.exists() else 0,
    }
    try:
        conn = get_connection(args.password, args.db)
    except SystemExit:
        info["ok"] = False
        info["error"] = "не удалось открыть базу (неверный пароль или повреждение)"
        _json_out(args.json, info, None)
        if not args.json:
            for k, v in info.items():
                print(f"{k}: {v}")
        return 1
    try:
        info["cipher_version"] = conn.execute("PRAGMA cipher_version").fetchone()[0]
        info["sqlite_version"] = conn.execute("SELECT sqlite_version()").fetchone()[0]
        info["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        try:
            info["entries"] = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        except Exception:
            info["entries"] = None
            info["tables"] = "база открыта, но таблицы entries ещё нет"
        info["ok"] = True
    finally:
        conn.close()
    _json_out(args.json, info, None)
    if not args.json:
        for k, v in info.items():
            print(f"{k}: {v}")
    return 0


def _script_file_op(conn, args, db_file):
    if args.command == "export":
        rows = conn.execute(
            "SELECT id, created_at, updated_at, content, tags, mood FROM entries "
            "ORDER BY created_at ASC"
        ).fetchall()
        out = Path(args.path)
        with open(out, "w", encoding="utf-8") as f:
            f.write("# Дневник\n\n")
            f.write(f"*Экспортировано: {now_str()}*\n\n---\n\n")
            for id_, created, updated, content, tags, mood in rows:
                f.write(f"## {created} (#{id_})\n\n")
                if tags:
                    f.write(f"**Теги:** {tags}  \n")
                if mood is not None:
                    f.write(f"**Настроение:** {mood}/10  \n")
                if updated != created:
                    f.write(f"*Изменено: {updated}*\n")
                f.write("\n" + content + "\n\n---\n\n")
        _json_out(args.json, {"path": str(out), "count": len(rows)},
                  f"✓ Экспортировано записей: {len(rows)} -> {out}")
        return 0

    if args.command == "import":
        src = Path(args.path)
        if not src.exists():
            print(f"Файл не найден: {src}", file=sys.stderr)
            return 4
        text = src.read_text(encoding="utf-8")
        added = 0
        for chunk in re.split(r"\n---+\n", text):
            chunk = chunk.strip()
            if not chunk or chunk.startswith("# Дневник"):
                continue
            m = re.match(r"##\s+(\d{4}-\d{2}-\d{2}[^\n]*)\n(.+)", chunk, re.DOTALL)
            if not m:
                continue
            date_str, body = m.group(1).strip(), m.group(2).strip()
            tm = re.search(r"\*\*Теги:\*\*\s*(.+)", body)
            tags = parse_tags(tm.group(1)) if tm else ""
            mm = re.search(r"\*\*Настроение:\*\*\s*(\d+)/10", body)
            mood = int(mm.group(1)) if mm else None
            body = re.sub(r"\*\*Теги:\*\*.*\n?", "", body)
            body = re.sub(r"\*\*Настроение:\*\*.*\n?", "", body)
            content = re.sub(r"\*Изменено:.*?\*\s*\n?", "", body).strip()
            try:
                ts = datetime.datetime.strptime(date_str[:19], DATE_FMT).strftime(DATE_FMT)
            except ValueError:
                try:
                    ts = datetime.datetime.strptime(date_str[:10], "%Y-%m-%d").strftime(DATE_FMT)
                except ValueError:
                    ts = now_str()
            conn.execute(
                "INSERT INTO entries (created_at, updated_at, content, tags, mood) "
                "VALUES (?,?,?,?,?)", (ts, ts, content, tags, mood))
            added += 1
        conn.commit()
        _json_out(args.json, {"path": str(src), "imported": added},
                  f"✓ Импортировано записей: {added}")
        return 0

    # backup — снимок базы. Сначала пробуем VACUUM INTO (атомарный зашифрованный
    # снимок без риска поймать незакрытую транзакцию), иначе — обычное копирование.
    dest = Path(args.path)
    conn.commit()
    note = "VACUUM INTO"
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
    except Exception:
        note = "копирование файла"
        conn.close()
        shutil.copy2(db_file, dest)
    _json_out(args.json, {"path": str(dest), "method": note},
              f"✓ Резервная копия ({note}): {dest}")
    return 0


def script_main(argv):
    args = _script_parser().parse_args(argv)
    if args.command == "doctor":
        return cmd_doctor(args)

    db_file = Path(args.db).expanduser() if args.db else DB_PATH
    conn = get_connection(args.password, args.db)
    init_db(conn)
    try:
        if args.command == "tui":
            run_tui(conn)
            return 0

        if args.command == "add":
            content = args.content
            if args.content_file:
                content = Path(args.content_file).read_text(encoding="utf-8")
            if content is None or not content.strip():
                print("Ошибка: пустой текст записи.", file=sys.stderr)
                return 2
            ts = now_str()
            cur = conn.execute(
                "INSERT INTO entries (created_at, updated_at, content, tags, mood) "
                "VALUES (?,?,?,?,?)", (ts, ts, content, parse_tags(args.tags), args.mood))
            conn.commit()
            _json_out(args.json, {"id": cur.lastrowid, "status": "added", "content": content},
                      f"✓ Добавлено #{cur.lastrowid}")
            return 0

        if args.command == "list":
            sql = ("SELECT id, created_at, updated_at, tags, mood, content "
                   "FROM entries")
            where, params = [], []
            if args.tag:
                where.append("(',' || tags || ',') LIKE ?")
                params.append(f"%,{args.tag.lower()},%")
            if args.date_from:
                where.append("created_at >= ?")
                params.append(args.date_from + " 00:00:00")
            if args.date_to:
                where.append("created_at <= ?")
                params.append(args.date_to + " 23:59:59")
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(args.limit)
            rows = conn.execute(sql, params).fetchall()
            payload = [dict(zip(("id", "created_at", "updated_at", "tags", "mood", "content"), r))
                       for r in rows]
            human = "\n".join(
                f"#{r[0]}  {r[1]}  [{r[3] or ''}]  {(r[5] or '').splitlines()[0][:60] if r[5] else ''}"
                for r in rows) or "Записей нет."
            _json_out(args.json, {"count": len(rows), "entries": payload}, human)
            return 0

        if args.command == "view":
            row = conn.execute(
                "SELECT id, created_at, updated_at, content, tags, mood FROM entries WHERE id=?",
                (args.id,)).fetchone()
            if not row:
                print(f"Запись #{args.id} не найдена.", file=sys.stderr)
                return 3
            payload = dict(zip(("id", "created_at", "updated_at", "content", "tags", "mood"), row))
            _json_out(args.json, payload, f"#{row[0]}  {row[1]}\n{row[3]}")
            return 0

        if args.command == "search":
            q = f"%{args.query}%"
            rows = conn.execute(
                "SELECT id, created_at, updated_at, tags, mood, substr(content,1,200) "
                "FROM entries WHERE content LIKE ? OR tags LIKE ? ORDER BY created_at DESC",
                (q, q)).fetchall()
            payload = [dict(zip(("id", "created_at", "updated_at", "tags", "mood", "preview"), r))
                       for r in rows]
            _json_out(args.json, {"count": len(rows), "entries": payload}, f"Найдено: {len(rows)}")
            return 0

        if args.command == "delete":
            if not conn.execute("SELECT id FROM entries WHERE id=?", (args.id,)).fetchone():
                print(f"Запись #{args.id} не найдена.", file=sys.stderr)
                return 3
            conn.execute("DELETE FROM entries WHERE id=?", (args.id,))
            conn.commit()
            _json_out(args.json, {"id": args.id, "status": "deleted"}, f"✓ Удалено #{args.id}")
            return 0

        if args.command == "stats":
            total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            payload = {"total": total}
            if total:
                payload["first"] = conn.execute("SELECT MIN(created_at) FROM entries").fetchone()[0]
                payload["last"] = conn.execute("SELECT MAX(created_at) FROM entries").fetchone()[0]
                payload["avg_mood"] = conn.execute(
                    "SELECT AVG(mood) FROM entries WHERE mood IS NOT NULL").fetchone()[0]
            _json_out(args.json, payload, f"Всего записей: {total}")
            return 0

        if args.command == "tags":
            counter = {}
            for (t,) in conn.execute("SELECT tags FROM entries WHERE tags != ''").fetchall():
                for x in t.split(","):
                    x = x.strip()
                    if x:
                        counter[x] = counter.get(x, 0) + 1
            _json_out(args.json, counter,
                      "\n".join(f"{k}: {v}" for k, v in sorted(counter.items())) or "Тегов нет.")
            return 0

        return _script_file_op(conn, args, db_file)
    finally:
        conn.close()


# =========================================================
#                     ТОЧКА ВХОДА
# =========================================================

_SCRIPT_COMMANDS = {
    "add", "list", "view", "search", "delete", "stats", "tags",
    "export", "import", "backup", "doctor", "tui", "-h", "--help",
}


def _interactive_options(argv):
    """Достаёт --db/--password из аргументов интерактивного запуска.

    Нужно, чтобы «TUI.py --db путь» открывал обычный CLI или TUI с другой базой,
    а не уходил в argparse, который требует подкоманду и падает с кодом 2.
    """
    db = password = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--db" and i + 1 < len(argv):
            db = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--db="):
            db = arg.split("=", 1)[1]
        elif arg == "--password" and i + 1 < len(argv):
            password = argv[i + 1]
            i += 2
            continue
        elif arg.startswith("--password="):
            password = arg.split("=", 1)[1]
        i += 1
    return db, password


def main():
    argv = sys.argv[1:]

    # Явный запрос полноэкранного режима.
    if argv and argv[0].lower() == "tui":
        db, password = _interactive_options(argv[1:])
        conn = get_connection(password, db)
        init_db(conn)
        try:
            run_tui(conn)
        finally:
            conn.close()
        return 0

    # Скриптовый режим — только если в аргументах есть настоящая команда.
    if any(arg.lower() in _SCRIPT_COMMANDS for arg in argv):
        return script_main(argv)

    # Иначе — интерактивный CLI (можно указать --db/--password).
    db, password = _interactive_options(argv)
    conn = get_connection(password, db)
    init_db(conn)
    try:
        cli_main(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main() or 0)