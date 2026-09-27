# -*- coding: utf-8 -*-
"""Автотесты дневника.

Проверяют то, что реально ломалось/могло сломаться:
  * доступность драйвера SQLCipher и то, что база действительно зашифрована;
  * создание / чтение / изменение / удаление записей;
  * сохранность данных после закрытия и повторного открытия базы;
  * работу неинтерактивного режима приложения отдельным процессом
    (тот же путь, которым пользуются скрипты и CI);
  * экспорт/импорт Markdown и резервное копирование.

Тесты не зависят от платформы: одинаково проходят на Windows и на Linux.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import TUI

PASSWORD = "test-password-123"
ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "TUI.py"


# --------------------------------------------------------------------------
# Вспомогательные средства
# --------------------------------------------------------------------------

def make_entry(conn, text, tags="", mood=None, ts="2026-01-01 10:00:00"):
    """Вставляет запись и возвращает её id."""
    conn.execute(
        "INSERT INTO entries (created_at, updated_at, content, tags, mood) "
        "VALUES (?,?,?,?,?)",
        (ts, ts, text, tags, mood),
    )
    conn.commit()
    return conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]


def run_app(args, db, password=PASSWORD, cwd=None):
    """Запускает TUI.py отдельным процессом в неинтерактивном режиме."""
    env = dict(os.environ)
    env["DIARY_PASSWORD"] = password
    env["PYTHONIOENCODING"] = "utf-8"
    env["NO_COLOR"] = "1"
    proc = subprocess.run(
        [sys.executable, str(APP), "--db", str(db), "--json"] + list(args),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(cwd or ROOT), timeout=180,
    )
    return proc


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "diary.db"


@pytest.fixture()
def conn(db):
    c = TUI.get_connection(PASSWORD, db)
    TUI.init_db(c)
    yield c
    try:
        c.close()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Драйвер шифрования
# --------------------------------------------------------------------------

def test_driver_is_cross_platform_sqlcipher3():
    assert TUI.SQLCIPHER_DRIVER == "sqlcipher3"


def test_cipher_version_is_reported(conn):
    version = conn.execute("PRAGMA cipher_version").fetchone()[0]
    assert version and version.strip(), "SQLCipher не активен"


def test_database_file_is_really_encrypted(conn, db):
    make_entry(conn, "секретный текст, который не должен лежать в открытом виде")
    conn.commit()
    conn.close()

    head = db.read_bytes()[:16]
    assert not head.startswith(b"SQLite format 3"), "файл базы не зашифрован"

    raw = sqlite3.connect(str(db))
    with pytest.raises(sqlite3.DatabaseError):
        raw.execute("SELECT count(*) FROM sqlite_master").fetchall()
    raw.close()


def test_wrong_password_is_rejected(db):
    c = TUI.get_connection(PASSWORD, db)
    TUI.init_db(c)
    make_entry(c, "запись")
    c.close()

    with pytest.raises(SystemExit):
        TUI.get_connection("совершенно-другой-пароль", db)


def test_db_path_env_override(tmp_path):
    target = tmp_path / "custom" / "my.db"
    assert TUI._resolve_db_path(str(target)) == target
    assert TUI._resolve_db_path("") == Path.home() / ".diary.db"


# --------------------------------------------------------------------------
# CRUD и персистентность
# --------------------------------------------------------------------------

def test_create_read_update_delete_roundtrip(db):
    c = TUI.get_connection(PASSWORD, db)
    TUI.init_db(c)
    eid = make_entry(c, "первая запись", "тест,важное", 7)

    row = c.execute("SELECT content, tags, mood FROM entries WHERE id=?", (eid,)).fetchone()
    assert row == ("первая запись", "тест,важное", 7)

    c.execute(
        "UPDATE entries SET content=?, updated_at=? WHERE id=?",
        ("исправленный текст", "2026-02-02 11:11:11", eid),
    )
    c.commit()
    c.close()

    c2 = TUI.get_connection(PASSWORD, db)
    assert c2.execute("SELECT content FROM entries WHERE id=?", (eid,)).fetchone()[0] == "исправленный текст"
    c2.execute("DELETE FROM entries WHERE id=?", (eid,))
    c2.commit()
    c2.close()

    c3 = TUI.get_connection(PASSWORD, db)
    assert c3.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
    c3.close()


def test_data_survives_close_and_reopen(db):
    c = TUI.get_connection(PASSWORD, db)
    TUI.init_db(c)
    for i in range(5):
        make_entry(c, "запись %d" % i, "t%d" % i, (i % 10) + 1)
    before = c.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    c.close()

    c2 = TUI.get_connection(PASSWORD, db)
    after = c2.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    contents = [r[0] for r in c2.execute("SELECT content FROM entries ORDER BY id").fetchall()]
    c2.close()

    assert before == after == 5
    assert contents == ["запись %d" % i for i in range(5)]


def test_unicode_and_multiline_content(db):
    c = TUI.get_connection(PASSWORD, db)
    TUI.init_db(c)
    text = "Юникод: дневник 📔\nвторая строка\n\tтабуляция"
    eid = make_entry(c, text)
    c.close()

    c2 = TUI.get_connection(PASSWORD, db)
    assert c2.execute("SELECT content FROM entries WHERE id=?", (eid,)).fetchone()[0] == text
    c2.close()


# --------------------------------------------------------------------------
# Интерактивный CLI (через подмену ввода)
# --------------------------------------------------------------------------

def test_cli_add_interactive(monkeypatch, db):
    c = TUI.get_connection(PASSWORD, db)
    TUI.init_db(c)
    answers = iter(["текст из CLI", "", "тест", "6"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    TUI.cli_add(c)
    assert c.execute("SELECT content, tags, mood FROM entries").fetchone() == ("текст из CLI", "тест", 6)
    c.close()


# --------------------------------------------------------------------------
# Интерактивный запуск с указанием своей базы
# --------------------------------------------------------------------------

def test_interactive_cli_accepts_db_option(db):
    """python TUI.py --db <путь> должен открывать интерактивный CLI, а не падать с кодом 2."""
    env = dict(os.environ)
    env["DIARY_PASSWORD"] = PASSWORD
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(APP), "--db", str(db)],
        input="help\nquit\n", capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, cwd=str(ROOT), timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Дневник" in proc.stdout
    assert db.exists(), "база по указанному пути не создана"


def test_tui_entry_is_not_confused_with_script_mode(db):
    """Первый аргумент --db не должен уводить разбор в argparse и ломать интерактивный запуск."""
    env = dict(os.environ)
    env["DIARY_PASSWORD"] = PASSWORD
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(APP), "--db", str(db), "tui"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(ROOT), timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert "TUI требует интерактивный терминал" in proc.stdout


# --------------------------------------------------------------------------
# Неинтерактивный режим отдельным процессом (то же, что делает CI)
# --------------------------------------------------------------------------

def test_process_add_list_view(db):
    r = run_app(["add", "--content", "из процесса", "--tags", "Ci,тест", "--mood", "8"], db)
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["status"] == "added"
    eid = payload["id"]

    r2 = run_app(["list", "--limit", "5"], db)
    assert r2.returncode == 0, r2.stderr
    data = json.loads(r2.stdout)
    assert data["count"] == 1
    assert data["entries"][0]["content"] == "из процесса"
    assert data["entries"][0]["mood"] == 8
    assert data["entries"][0]["tags"] == "ci,тест"

    r3 = run_app(["view", str(eid)], db)
    assert r3.returncode == 0, r3.stderr
    assert json.loads(r3.stdout)["content"] == "из процесса"


def test_process_search_and_delete(db):
    run_app(["add", "--content", "найти иголку в стоге", "--tags", "поиск"], db)
    run_app(["add", "--content", "другая запись", "--tags", "прочее"], db)

    r = run_app(["search", "иголку"], db)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["count"] == 1

    eid = json.loads(run_app(["list", "--limit", "1"], db).stdout)["entries"][0]["id"]
    assert run_app(["delete", str(eid)], db).returncode == 0
    assert run_app(["view", str(eid)], db).returncode == 3


def test_process_doctor_reports_healthy_db(db):
    run_app(["add", "--content", "проверка"], db)
    r = run_app(["doctor"], db)
    assert r.returncode == 0, r.stderr
    info = json.loads(r.stdout)
    assert info["ok"] is True
    assert info["cipher_version"]
    assert info["integrity_check"] == "ok"
    assert info["entries"] == 1


def test_process_stats_and_tags(db):
    run_app(["add", "--content", "раз", "--tags", "a,b", "--mood", "10"], db)
    run_app(["add", "--content", "два", "--tags", "b", "--mood", "2"], db)

    stats = json.loads(run_app(["stats"], db).stdout)
    assert stats["total"] == 2
    assert stats["avg_mood"] == pytest.approx(6.0)

    tags = json.loads(run_app(["tags"], db).stdout)
    assert tags == {"a": 1, "b": 2}


def test_process_empty_content_is_rejected(db):
    r = run_app(["add", "--content", "   "], db)
    assert r.returncode == 2


# --------------------------------------------------------------------------
# Экспорт / импорт / резервная копия
# --------------------------------------------------------------------------

def test_export_import_roundtrip(db, tmp_path):
    c = TUI.get_connection(PASSWORD, db)
    TUI.init_db(c)
    make_entry(c, "строка один", "a", 3, ts="2026-03-01 09:00:00")
    make_entry(c, "строка два\nвторая линия", "b", 9, ts="2026-03-02 09:00:00")
    c.close()

    md = tmp_path / "export.md"
    r = run_app(["export", str(md)], db)
    assert r.returncode == 0, r.stderr
    assert md.exists() and md.stat().st_size > 0
    assert json.loads(r.stdout)["count"] == 2

    db2 = tmp_path / "diary2.db"
    r2 = run_app(["import", str(md)], db2)
    assert r2.returncode == 0, r2.stderr
    assert json.loads(r2.stdout)["imported"] == 2

    c2 = TUI.get_connection(PASSWORD, db2)
    rows = c2.execute("SELECT content FROM entries ORDER BY created_at").fetchall()
    c2.close()
    assert len(rows) == 2
    assert rows[0][0].startswith("строка один")
    assert "строка два" in rows[1][0]


def test_backup_is_readable_and_encrypted(db, tmp_path):
    c = TUI.get_connection(PASSWORD, db)
    TUI.init_db(c)
    make_entry(c, "для резервной копии", "b", 4)
    c.close()

    dest = tmp_path / "backup.db"
    r = run_app(["backup", str(dest)], db)
    assert r.returncode == 0, r.stderr
    assert dest.exists() and dest.stat().st_size > 0
    assert not dest.read_bytes()[:16].startswith(b"SQLite format 3"), "копия не зашифрована"

    c2 = TUI.get_connection(PASSWORD, dest)
    assert c2.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
    assert c2.execute("SELECT content FROM entries").fetchone()[0] == "для резервной копии"
    c2.close()


# --------------------------------------------------------------------------
# Комплект поставки
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "TUI.py",
    "requirements.txt",
    "requirements-dev.txt",
    "install.ps1",
    "run.ps1",
    "install.sh",
    "run.sh",
    "README.md",
    "REPORT_COMPATIBILITY.md",
    "scripts/selfcheck.py",
    ".github/workflows/ci.yml",
])
def test_delivery_files_exist(name):
    assert (ROOT / name).exists(), "отсутствует файл поставки: %s" % name


def test_readme_screenshots_are_consistent():
    """Каждый скриншот из README существует, и в папке нет неиспользуемых файлов."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    refs = set(re.findall(r"docs/screenshots/([A-Za-z0-9_.\-]+\.png)", readme))
    present = {p.name for p in (ROOT / "docs" / "screenshots").glob("*.png")}
    assert refs, "README не ссылается ни на один скриншот"
    assert refs <= present, "в README ссылки на отсутствующие файлы: %s" % sorted(refs - present)
    assert present <= refs, "в docs/screenshots есть неиспользуемые файлы: %s" % sorted(present - refs)


def test_requirements_pin_cross_platform_driver():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "sqlcipher3==0.6.2" in text
    assert "windows-curses" in text
    assert "sys_platform == \"win32\"" in text
