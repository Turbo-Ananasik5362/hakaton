#!/usr/bin/env python3
"""Первичная настройка проекта на ноутбуке (запускаете вы, один раз, из корня проекта).

Что делает — всё строго внутри папки проекта:
  1. проверяет версию Python (нужна ≥ 3.10, лучше та же минорная, что на ВМ);
  2. создаёт виртуальное окружение .venv/ и ставит в него requirements.txt
     (кеш pip — .cache/pip, временные файлы — .tmp/);
  3. создаёт .cache/, .tmp/, logs/;
  4. подсказывает, что сделать дальше (.env, проверка тестами).

Использование:
  python3 scripts/setup_local.py [--vm-python 3.10] [--no-install]
На Windows: py -3 scripts\\setup_local.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
IS_WIN = os.name == "nt"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")


def isolated_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        PIP_CACHE_DIR=str(ROOT / ".cache" / "pip"),
        XDG_CACHE_HOME=str(ROOT / ".cache"),
        TMPDIR=str(ROOT / ".tmp"),
        TEMP=str(ROOT / ".tmp"),
        TMP=str(ROOT / ".tmp"),
        PIP_REQUIRE_VIRTUALENV="1",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
    )
    return env


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vm-python", default=None, help="минорная версия Python на ВМ, например 3.10")
    ap.add_argument("--no-install", action="store_true")
    a = ap.parse_args()

    v = sys.version_info
    print(f"Python ноутбука: {v.major}.{v.minor}.{v.micro}")
    if (v.major, v.minor) < (3, 10):
        sys.exit("Нужен Python 3.10 или новее.")
    if a.vm_python and a.vm_python != f"{v.major}.{v.minor}":
        print(
            f"⚠ На ВМ Python {a.vm_python}, здесь {v.major}.{v.minor}. Код пишем совместимым с {a.vm_python} "
            "(ruff target-version в pyproject.toml), зависимости на ВМ ставятся отдельно."
        )

    if a.vm_python:
        (ROOT / ".python-version").write_text(a.vm_python + "\n", encoding="utf-8")

    for d in (".cache/pip", ".cache/downloads", ".tmp", "logs"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)

    if not venv_python().exists():
        print("Создаю виртуальное окружение .venv …")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True, env=isolated_env())
    else:
        print(".venv уже есть")

    if not a.no_install and (ROOT / "requirements.txt").exists():
        py = str(venv_python())
        env = isolated_env()
        subprocess.run([py, "-m", "pip", "install", "--upgrade", "pip"], check=True, env=env, cwd=ROOT)
        subprocess.run([py, "-m", "pip", "install", "-r", "requirements.txt"], check=True, env=env, cwd=ROOT)

    print("\nДальше:")
    if not (ROOT / ".env").exists():
        print("  1. Скопируйте .env.example в .env и впишите OPENAI_API_KEY из личного кабинета (вручную).")
    else:
        print("  1. .env найден.")
    print("  2. Проверьте окружение: .venv/bin/python -m pytest -q")


if __name__ == "__main__":
    main()
