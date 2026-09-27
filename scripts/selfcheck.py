#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сквозная проверка дневника — запускается одинаково на Windows и на Linux.

Скрипт выполняет полный пользовательский сценарий НАСТОЯЩИМИ процессами
приложения (python TUI.py ...), а не только вызовами функций:

    1. сведения об окружении и драйвере SQLCipher;
    2. создание записей, чтение, поиск, статистика, теги;
    3. изменение и удаление записей;
    4. сохранность данных после закрытия и повторного запуска;
    5. проверка, что файл базы действительно зашифрован;
    6. отказ при неверном пароле;
    7. экспорт/импорт Markdown и резервная копия;
    8. работоспособность входа в полноэкранный интерфейс (без TTY);
    9. артефакты для сверки между ОС: база и манифест её содержимого.

Использование:
    python scripts/selfcheck.py                      # временная папка
    python scripts/selfcheck.py --workdir .selfcheck # своя папка
    python scripts/selfcheck.py --keep               # не удалять временную папку

Код возврата 0 — всё прошло, 1 — есть падения.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "TUI.py"
PASSWORD = "selfcheck-password"

RESULTS: list[dict] = []


def record(name: str, ok: bool, detail: str = "", extra: dict | None = None) -> None:
    entry = {"name": name, "ok": bool(ok), "detail": detail}
    if extra:
        entry["extra"] = extra
    RESULTS.append(entry)
    mark = "PASS" if ok else "FAIL"
    print("[%s] %s%s" % (mark, name, (" — " + detail) if detail else ""), flush=True)


def section(title: str) -> None:
    print("\n== %s ==" % title, flush=True)


def run_app(args, db_path, password=PASSWORD, timeout=180):
    env = dict(os.environ)
    env["DIARY_PASSWORD"] = password
    env["PYTHONIOENCODING"] = "utf-8"
    env["NO_COLOR"] = "1"
    cmd = [sys.executable, str(APP), "--db", str(db_path), "--json"] + [str(a) for a in args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=str(ROOT), timeout=timeout)


def app_json(args, db_path, password=PASSWORD):
    proc = run_app(args, db_path, password=password)
    if proc.returncode != 0:
        raise RuntimeError("команда %s завершилась с кодом %s: %s"
                           % (args, proc.returncode, (proc.stderr or "").strip()[:400]))
    return json.loads(proc.stdout)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_entries(db_path, password=PASSWORD):
    sys.path.insert(0, str(ROOT))
    import TUI  # noqa: WPS433  (импорт по месту: нужен корень проекта в sys.path)
    conn = TUI.get_connection(password, db_path)
    try:
        rows = conn.execute(
            "SELECT id, created_at, updated_at, content, tags, mood "
            "FROM entries ORDER BY created_at, id"
        ).fetchall()
        cipher = conn.execute("PRAGMA cipher_version").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    return rows, cipher, integrity


def main() -> int:
    parser = argparse.ArgumentParser(description="Сквозная проверка дневника")
    parser.add_argument("--workdir", help="папка для артефактов и базы проверки")
    parser.add_argument("--keep", action="store_true", help="не удалять временную папку")
    args = parser.parse_args()

    tmp_created = False
    if args.workdir:
        work = Path(args.workdir).resolve()
        work.mkdir(parents=True, exist_ok=True)
    else:
        work = Path(tempfile.mkdtemp(prefix="diary-selfcheck-"))
        tmp_created = True

    db = work / "selftest.db"
    if db.exists():
        db.unlink()

    section("Окружение")
    print("ОС:        %s" % platform.platform())
    print("Python:    %s (%s)" % (sys.version.split()[0], sys.executable))
    print("Рабочая папка: %s" % work)
    record("python >= 3.9", sys.version_info >= (3, 9), sys.version.split()[0])

    try:
        import sqlcipher3
        c = sqlcipher3.connect(":memory:")
        c.execute("PRAGMA key = 'x'")
        cipher_in_proc = c.execute("PRAGMA cipher_version").fetchone()[0]
        c.close()
        record("драйвер sqlcipher3 импортируется", True,
               "%s / %s" % (sqlcipher3.__name__, cipher_in_proc))
    except Exception as exc:  # pragma: no cover
        record("драйвер sqlcipher3 импортируется", False, repr(exc))
        cipher_in_proc = ""

    section("Создание и чтение записей")
    created = []
    try:
        for i, (text, tags, mood) in enumerate([
            ("первая запись проверки", "проверка,важно", 9),
            ("вторая запись проверки", "проверка", 4),
            ("третья запись проверки", "", 7),
        ], start=1):
            payload = app_json(["add", "--content", text, "--tags", tags, "--mood", mood], db)
            created.append(payload["id"])
        record("добавление трёх записей", len(created) == 3, "ids=%s" % created)
    except Exception as exc:
        record("добавление трёх записей", False, repr(exc))

    listed = app_json(["list", "--limit", "50"], db)
    record("список возвращает 3 записи", listed["count"] == 3, "count=%s" % listed["count"])

    viewed = app_json(["view", str(created[0])], db) if created else {}
    record("чтение записи по id", viewed.get("content") == "первая запись проверки",
           "content=%r" % viewed.get("content"))

    found = app_json(["search", "вторая запись"], db)
    record("поиск по тексту", found["count"] == 1, "count=%s" % found["count"])

    stats = app_json(["stats"], db)
    record("статистика", stats["total"] == 3, json.dumps(stats, ensure_ascii=False))

    tags = app_json(["tags"], db)
    record("подсчёт тегов", tags.get("проверка") == 2, json.dumps(tags, ensure_ascii=False))

    section("Изменение и удаление")
    try:
        sys.path.insert(0, str(ROOT))
        import TUI
        conn = TUI.get_connection(PASSWORD, db)
        conn.execute("UPDATE entries SET content=?, updated_at=? WHERE id=?",
                     ("изменённый текст", "2026-12-31 23:59:59", created[0]))
        conn.commit()
        conn.close()
        after = app_json(["view", str(created[0])], db)
        record("изменение записи", after["content"] == "изменённый текст",
               "content=%r" % after["content"])

        app_json(["delete", str(created[2])], db)
        left = app_json(["list", "--limit", "50"], db)
        record("удаление записи", left["count"] == 2, "count=%s" % left["count"])
    except Exception as exc:
        record("изменение и удаление записи", False, repr(exc))

    section("Сохранность данных и шифрование")
    reopened = app_json(["list", "--limit", "50"], db)
    record("данные на месте после перезапуска процесса", reopened["count"] == 2,
           "count=%s" % reopened["count"])

    head = db.read_bytes()[:16]
    record("файл базы зашифрован (не «SQLite format 3»)",
           not head.startswith(b"SQLite format 3"), "header=%r" % head[:8])
    try:
        raw = sqlite3.connect(str(db))
        raw.execute("SELECT count(*) FROM sqlite_master").fetchall()
        raw.close()
        record("обычный sqlite3 не читает файл", False, "файл читается без пароля!")
    except sqlite3.DatabaseError as exc:
        record("обычный sqlite3 не читает файл", True, str(exc))

    wrong = run_app(["list"], db, password="заведомо-неверный-пароль")
    record("неверный пароль отклонён", wrong.returncode != 0,
           "exit=%s" % wrong.returncode)

    doctor = app_json(["doctor"], db)
    record("doctor: integrity_check", doctor.get("integrity_check") == "ok",
           json.dumps(doctor, ensure_ascii=False)[:220])
    record("doctor: cipher_version", bool(doctor.get("cipher_version")),
           str(doctor.get("cipher_version")))

    section("Экспорт, импорт, резервная копия")
    md = work / "export.md"
    try:
        result = app_json(["export", str(md)], db)
        record("экспорт в Markdown", md.exists() and md.stat().st_size > 0,
               "count=%s -> %s" % (result.get("count"), md))
    except Exception as exc:
        record("экспорт в Markdown", False, repr(exc))

    db_imported = work / "imported.db"
    if db_imported.exists():
        db_imported.unlink()
    try:
        result = app_json(["import", str(md)], db_imported)
        rows, _, _ = read_entries(db_imported)
        record("импорт из Markdown", len(rows) == 2, "imported=%s, в базе=%s"
               % (result.get("imported"), len(rows)))
    except Exception as exc:
        record("импорт из Markdown", False, repr(exc))

    backup = work / "backup.db"
    if backup.exists():
        backup.unlink()
    try:
        result = app_json(["backup", str(backup)], db)
        same = backup.exists() and sha256_of(backup) == sha256_of(db)
        b_rows, _, _ = read_entries(backup)
        ok = (backup.exists() and len(b_rows) == 2
              and not backup.read_bytes()[:16].startswith(b"SQLite format 3"))
        record("резервная копия читается и зашифрована", ok,
               "метод=%s, записей=%s, байт-в-байт=%s"
               % (result.get("method"), len(b_rows), same))
    except Exception as exc:
        record("резервная копия читается и зашифрована", False, repr(exc))

    section("Полноэкранный интерфейс (без TTY)")
    env = dict(os.environ)
    env["DIARY_PASSWORD"] = PASSWORD
    env["PYTHONIOENCODING"] = "utf-8"
    tui = subprocess.run([sys.executable, str(APP), "--db", str(db), "tui"],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", env=env, cwd=str(ROOT), timeout=120)
    record("вход в TUI не падает без терминала", tui.returncode == 0,
           "exit=%s, out=%r" % (tui.returncode, (tui.stdout or "").strip()[:120]))
    try:
        import curses  # noqa: F401
        record("модуль curses доступен", True, "windows-curses" if os.name == "nt" else "stdlib")
    except Exception as exc:
        record("модуль curses доступен", False, repr(exc))

    section("Артефакты для сверки между ОС")
    manifest = {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "machine": platform.machine(),
        "cipher_version": cipher_in_proc,
        "db_file": str(db),
        "db_sha256": sha256_of(db),
        "entry_count": None,
        "entries": [],
    }
    try:
        rows, cipher, integrity = read_entries(db)
        manifest["entry_count"] = len(rows)
        manifest["cipher_version"] = cipher
        manifest["integrity_check"] = integrity
        manifest["entries"] = [
            {"created_at": r[1], "updated_at": r[2], "content": r[3], "tags": r[4], "mood": r[5]}
            for r in rows
        ]
        record("манифест содержимого сформирован", True, "записей=%s" % len(rows))
    except Exception as exc:
        record("манифест содержимого сформирован", False, repr(exc))

    manifest_path = work / ("manifest-%s.json" % sys.platform)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    cross_db = work / ("crossos-%s.db" % sys.platform)
    shutil.copy2(db, cross_db)
    print("Манифест: %s" % manifest_path)
    print("База для переноса: %s" % cross_db)

    passed = sum(1 for r in RESULTS if r["ok"])
    failed = [r for r in RESULTS if not r["ok"]]
    print("\nИтого: %d/%d проверок пройдено" % (passed, len(RESULTS)))
    if failed:
        print("Не прошли:")
        for r in failed:
            print("  - %s: %s" % (r["name"], r["detail"]))

    report = {
        "platform": sys.platform,
        "platform_detail": platform.platform(),
        "python": sys.version.split()[0],
        "passed": passed,
        "total": len(RESULTS),
        "failed": [r["name"] for r in failed],
        "results": RESULTS,
        "workdir": str(work),
    }
    (work / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print("Отчёт: %s" % (work / "report.json"))

    if tmp_created and not args.keep:
        print("(временная папка сохранена: %s)" % work)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
