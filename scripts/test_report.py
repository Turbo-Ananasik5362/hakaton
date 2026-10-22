#!/usr/bin/env python3
"""Отчёт о прогоне тестов: docs/test_report.md из JUnit XML pytest и итогов шагов scripts/check.sh.

    python scripts/test_report.py --junit .tmp/check/junit.xml --steps .tmp/check/steps.tsv --out docs/test_report.md

steps.tsv — строки «шаг<TAB>ok|fail<TAB>секунды» в порядке выполнения. Тесты раскладываются по областям (AREAS):
сколько в каждом файле, сколько прошло, упало и пропущено, сколько секунд заняли.
"""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MSK = dt.timezone(dt.timedelta(hours=3))
OTHER_AREA = "Прочее"

# Область → (что проверяет, файлы тестов). Порядок — порядок строк в отчёте.
AREAS: dict[str, tuple[str, tuple[str, ...]]] = {
    "Часы и календарь": (
        "солвер часов: суммы по строкам и колонкам, шаг 0,5 ч, стажировка без аудиторных, конфликты и rebalance; "
        "календарный график и правило «срок против нагрузки»",
        ("test_hours.py", "test_calendar.py"),
    ),
    "Проверки V01–V46": (
        "эталонные комплекты проходят без замечаний; для каждой проверки validate.py — порча, которую она ловит; "
        "ошибки образца 248 ч находятся; антиплагиат (шинглы)",
        ("test_validate.py", "test_copycheck.py"),
    ),
    "Выгрузки и V31": (
        ".docx программы по обоим шаблонам (снимок таблиц, оформление, пустые реквизиты), V31 — перечитывание "
        ".docx и сверка с комплектом; .pptx, GIFT, Moodle XML, CSV, JSON; сбой одной выгрузки не мешает остальным",
        ("test_render_docx.py", "test_render_exports.py"),
    ),
    "Сквозной API в mock": (
        "сценарии через FastAPI TestClient без сети: уточнения → каркас → утверждение → комплект → файлы; "
        "ошибки на плохом вводе; прогон открытых запросов скриптом",
        ("test_api.py", "test_run_open_query.py"),
    ),
    "Шаги агента в mock": (
        "разбор запроса кодом и моделью, уточняющие вопросы, каркас по открытым и своим запросам, генерация "
        "содержания, навыки app/skills",
        (
            "test_intake.py",
            "test_request_rules.py",
            "test_clarify.py",
            "test_skeleton.py",
            "test_content.py",
            "test_questions.py",
            "test_skills.py",
        ),
    ),
    "Точечная правка": (
        "граф зависимостей, разбор команд кодом, план от модели; после правки всё вне changed_ids побайтно прежнее, "
        "часы сходятся, ID не переиспользуются; неудачная правка не меняет комплект; done → patch → done через API",
        ("test_patch.py",),
    ),
    "Реестр, образцы, литература": (
        "профстандарты, нормативка и литература только из реестра; разбор приложенного образца; "
        "проверка литературы от модели",
        (
            "test_registry.py",
            "test_registry_api.py",
            "test_check_registry.py",
            "test_reference.py",
            "test_bibliography.py",
        ),
    ),
    "Надёжность": (
        "клиент LLM: кеш, ремонт JSON, повторы при 429 и 5xx, бюджет; таксономия ошибок, дедлайн, лимиты нагрузки; "
        "хранилище сессий; настройки",
        (
            "test_llm.py",
            "test_llm_live.py",
            "test_reliability.py",
            "test_review_fixes.py",
            "test_sessions.py",
            "test_config.py",
        ),
    ),
    "Отчёт методисту": (
        "report.json, report.md, приложение в .docx и страница интерфейса; предупреждения с цифрами и вариантами "
        "решения (V46)",
        ("test_report_md.py", "test_methodist_report.py"),
    ),
    "Модель данных": (
        "pydantic-модель комплекта, стабильные ID и связи между объектами",
        ("test_models.py", "test_ids.py"),
    ),
    "Развёртывание": (
        "Dockerfile, .dockerignore и docker-compose.yml без сборки образа: пути COPY, пользователь без root, команда "
        "запуска и порт как в README и на ВМ, HEALTHCHECK на /health, тома для данных сервера; docs/deploy.md",
        ("test_deploy.py",),
    ),
    "Инженерная гигиена": (
        "этот отчёт, scripts/check.sh и CI; в коде нет маркеров незавершённой работы",
        ("test_check.py", "test_effect.py"),
    ),
}


def area_of(file_name: str) -> str:
    for area, (_, files) in AREAS.items():
        if file_name in files:
            return area
    return OTHER_AREA


@dataclass
class FileStats:
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    seconds: float = 0.0
    failures: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.skipped


@dataclass
class Step:
    name: str
    ok: bool
    seconds: float


def _file_of(classname: str, name: str = "") -> str:
    """tests.test_api или tests.test_api.TestX → tests/test_api.py; при ошибке сбора classname пуст — берём name."""
    parts = (classname or name.replace("/", ".").removesuffix(".py")).split(".")
    for end in range(len(parts), 0, -1):
        candidate = "/".join(parts[:end]) + ".py"
        if (ROOT / candidate).is_file():
            return candidate
    return "/".join(parts) + ".py"


def parse_junit(path: Path) -> dict[str, FileStats]:
    stats: dict[str, FileStats] = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        file = _file_of(case.get("classname", ""), case.get("name", ""))
        item = stats.setdefault(file, FileStats())
        item.seconds += float(case.get("time") or 0)
        if case.find("failure") is not None or case.find("error") is not None:
            item.failed += 1
            item.failures.append(case.get("name", "?"))
        elif case.find("skipped") is not None:
            item.skipped += 1
        else:
            item.passed += 1
    return dict(sorted(stats.items()))


def parse_steps(path: Path | None) -> list[Step]:
    if path is None or not path.is_file():
        return []
    steps = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name, status, seconds = (line.split("\t") + ["", ""])[:3]
        if name:
            steps.append(Step(name, status == "ok", float(seconds or 0)))
    return steps


def git_revision() -> str:
    def git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10, check=True
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    commit = git("rev-parse", "--short", "HEAD") or "неизвестен"
    dirty = git("status", "--porcelain", "--untracked-files=no")
    return f"{commit} + незакоммиченные изменения" if dirty else commit


def _num(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


def render(
    stats: dict[str, FileStats], steps: list[Step], now: dt.datetime, revision: str, python: str
) -> str:
    total = FileStats()
    for item in stats.values():
        total.passed += item.passed
        total.failed += item.failed
        total.skipped += item.skipped
        total.seconds += item.seconds
    ok = all(step.ok for step in steps) and total.failed == 0 and total.total > 0
    lines = [
        "# Отчёт о прогоне тестов",
        "",
        "Создаётся командой `bash scripts/check.sh` (её же запускает CI). Не править руками — перезапустить проверку.",
        "",
        f"- Дата: {now:%d.%m.%Y %H:%M} МСК",
        f"- Коммит рабочей копии: {revision}",
        f"- Python: {python}",
        f"- Итог: **{'всё зелёное' if ok else 'есть ошибки'}** — тестов {total.total}: прошло {total.passed}, "
        f"упало {total.failed}, пропущено {total.skipped}; время тестов {_num(total.seconds)} с",
        "- Тесты идут в режиме `LLM_MODE=mock`: без сети и без расходов. Тесты с настоящей моделью (маркер `llm`) "
        "здесь не запускаются.",
        "",
    ]
    if steps:
        lines += ["## Шаги проверки", "", "| Шаг | Итог | Время, с |", "|---|---|---|"]
        lines += [f"| {s.name} | {'✓ ок' if s.ok else '✗ ошибка'} | {_num(s.seconds)} |" for s in steps]
        lines.append("")
    if not stats:
        lines += ["**pytest не дошёл до конца: результатов тестов нет — смотрите вывод проверки.**", ""]
    lines += ["## Что покрывают тесты", "", "| Область | Тестов | Что проверяют |", "|---|---|---|"]
    by_area: dict[str, int] = {}
    for file, item in stats.items():
        area = area_of(Path(file).name)
        by_area[area] = by_area.get(area, 0) + item.total
    for area, (what, _) in AREAS.items():
        if by_area.get(area):
            lines.append(f"| {area} | {by_area[area]} | {what} |")
    if by_area.get(OTHER_AREA):
        lines.append(f"| {OTHER_AREA} | {by_area[OTHER_AREA]} | файлы без области в scripts/test_report.py |")
    lines += [
        "",
        "## По файлам",
        "",
        "| Файл | Область | Тестов | Прошло | Упало | Пропущено | Время, с |",
        "|---|---|---|---|---|---|---|",
    ]
    for file, item in stats.items():
        lines.append(
            f"| `{file}` | {area_of(Path(file).name)} | {item.total} | {item.passed} | {item.failed} "
            f"| {item.skipped} | {_num(item.seconds)} |"
        )
    lines.append(
        f"| **Всего** | | **{total.total}** | {total.passed} | {total.failed} | {total.skipped} "
        f"| {_num(total.seconds)} |"
    )
    failures = [(file, name) for file, item in stats.items() for name in item.failures]
    if failures:
        lines += ["", "## Упавшие тесты", ""]
        lines += [f"- `{file}::{name}`" for file, name in failures]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--steps", type=Path)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "test_report.md")
    args = parser.parse_args(argv)
    crashed = (
        not args.junit.is_file()
    )  # pytest не дошёл до конца: отчёт всё равно пишем, чтобы не остался старый
    if crashed:
        print(f"нет файла {args.junit}: pytest не дошёл до конца", file=sys.stderr)
    text = render(
        {} if crashed else parse_junit(args.junit),
        parse_steps(args.steps),
        dt.datetime.now(MSK),
        git_revision(),
        sys.version.split()[0],
    )
    args.out.write_text(text, encoding="utf-8")
    return 1 if crashed else 0


if __name__ == "__main__":
    sys.exit(main())
