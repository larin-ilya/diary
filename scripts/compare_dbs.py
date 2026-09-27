#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сверка базы дневника с манифестом, снятым на другой ОС (проверка переноса).

Типовой сценарий переноса «Linux -> Windows»:
    1) на Linux:  python scripts/selfcheck.py --workdir transfer
                  (появятся transfer/manifest-linux.json и transfer/crossos-linux.db)
    2) копируем папку transfer на Windows;
    3) на Windows: python scripts/compare_dbs.py \
                       --db transfer/crossos-linux.db \
                       --manifest transfer/manifest-linux.json
       (пароль по умолчанию — selfcheck-password, см. --password)

Скрипт открывает базу ЧУЖОЙ платформы и сверяет каждую запись с манифестом.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Сверка базы с манифестом другой ОС")
    parser.add_argument("--db", required=True, help="файл базы для проверки")
    parser.add_argument("--manifest", required=True, help="манифест, снятый на исходной ОС")
    parser.add_argument("--password", default="selfcheck-password", help="пароль базы")
    args = parser.parse_args()

    db_path = Path(args.db)
    manifest_path = Path(args.manifest)
    for path in (db_path, manifest_path):
        if not path.exists():
            print("Не найден файл: %s" % path, file=sys.stderr)
            return 2

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    sys.path.insert(0, str(ROOT))
    import TUI

    conn = TUI.get_connection(args.password, db_path)
    try:
        rows = conn.execute(
            "SELECT created_at, updated_at, content, tags, mood "
            "FROM entries ORDER BY created_at, id"
        ).fetchall()
        cipher = conn.execute("PRAGMA cipher_version").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()

    actual = [
        {"created_at": r[0], "updated_at": r[1], "content": r[2], "tags": r[3], "mood": r[4]}
        for r in rows
    ]
    expected = manifest.get("entries", [])

    print("Исходная платформа: %s (Python %s, %s)"
          % (manifest.get("platform"), manifest.get("python"), manifest.get("machine")))
    print("Проверяем на:       %s (Python %s)" % (sys.platform, sys.version.split()[0]))
    print("SQLCipher:          было %s, стало %s"
          % (manifest.get("cipher_version"), cipher))
    print("integrity_check:    %s" % integrity)
    print("Записей:            ожидалось %d, прочитано %d" % (len(expected), len(actual)))

    ok = True
    if len(expected) != len(actual):
        print("ОШИБКА: количество записей не совпадает")
        ok = False
    for i, (e, a) in enumerate(zip(expected, actual)):
        if e != a:
            print("ОШИБКА: запись #%d отличается" % i)
            print("   ожидалось: %s" % json.dumps(e, ensure_ascii=False))
            print("   получено:  %s" % json.dumps(a, ensure_ascii=False))
            ok = False

    current_sha = sha256_of(db_path)
    if current_sha == manifest.get("db_sha256"):
        print("SHA-256 файла:      совпадает с исходным (%s)" % current_sha[:16])
    else:
        print("SHA-256 файла:      отличается (копия изменялась) — это допустимо, "
              "если база открывалась на запись")

    if ok:
        print("\nРЕЗУЛЬТАТ: база, созданная на другой ОС, прочитана полностью и без потерь.")
        return 0
    print("\nРЕЗУЛЬТАТ: обнаружены расхождения.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
