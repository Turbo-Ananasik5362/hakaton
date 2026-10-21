"""Инженерная гигиена: scripts/check.sh, отчёт docs/test_report.md, CI и отсутствие маркеров незавершённой работы."""

from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.test_report import AREAS, OTHER_AREA, Step, area_of, parse_junit, parse_steps, render

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = (ROOT / ".gitea/workflows/tests.yml", ROOT / ".github/workflows/tests.yml")
# Склеено, чтобы сам этот файл не попадал под поиск
MARKERS = re.compile(r"\b(" + "TO" + "DO|FIX" + "ME|XX" + "X)\b")
CODE_DIRS = ("app", "scripts", "tests")
CODE_SUFFIXES = {".py", ".sh", ".md", ".yml", ".html", ".toml"}


def test_no_unfinished_markers_in_code() -> None:
    found = []
    files = [p for d in CODE_DIRS for p in (ROOT / d).rglob("*") if p.suffix in CODE_SUFFIXES]
    files += [*WORKFLOWS, ROOT / "README.md", ROOT / "pyproject.toml"]
    for path in files:
        if not path.is_file() or "fixtures" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if MARKERS.search(line):
                found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:80]}")
    assert not found, "маркеры незавершённой работы:\n" + "\n".join(found)


def test_every_test_file_has_area() -> None:
    names = sorted(p.name for p in (ROOT / "tests").glob("test_*.py"))
    assert [n for n in names if area_of(n) == OTHER_AREA] == []
    listed = [f for _, files in AREAS.values() for f in files]
    assert sorted(set(listed)) == sorted(listed), "файл указан в двух областях"
    assert set(listed) <= set(names), "в AREAS есть несуществующий файл"


JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="5">
  <testcase classname="tests.test_hours" name="test_a" time="0.50"/>
  <testcase classname="tests.test_hours" name="test_b[x]" time="0.25"/>
  <testcase classname="tests.test_api" name="test_c" time="1.00"><failure message="boom">assert 0</failure></testcase>
  <testcase classname="tests.test_api.TestGroup" name="test_d" time="0.10"><skipped message="нет"/></testcase>
  <testcase classname="tests.test_api" name="test_e" time="0.20"><error message="setup"/></testcase>
</testsuite></testsuites>
"""


def _now() -> dt.datetime:
    return dt.datetime(2026, 10, 21, 12, 30, tzinfo=dt.timezone(dt.timedelta(hours=3)))


def test_report_counts_by_file_and_area(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(JUNIT, encoding="utf-8")
    stats = parse_junit(junit)
    assert list(stats) == ["tests/test_api.py", "tests/test_hours.py"]
    api, hours = stats["tests/test_api.py"], stats["tests/test_hours.py"]
    assert (api.total, api.passed, api.failed, api.skipped) == (3, 0, 2, 1)
    assert (hours.total, hours.passed) == (2, 2)
    assert hours.seconds == pytest.approx(0.75)

    steps_file = tmp_path / "steps.tsv"
    steps_file.write_text("ruff check\tok\t0.4\npytest -q\tfail\t12.3\n", encoding="utf-8")
    steps = parse_steps(steps_file)
    assert steps == [Step("ruff check", True, 0.4), Step("pytest -q", False, 12.3)]

    text = render(stats, steps, _now(), "abc1234", "3.10.14")
    assert "21.10.2026 12:30 МСК" in text and "abc1234" in text and "3.10.14" in text
    assert "**есть ошибки** — тестов 5: прошло 2, упало 2, пропущено 1" in text
    assert "| `tests/test_hours.py` | Часы и календарь | 2 | 2 | 0 | 0 | 0,8 |" in text
    assert "| Сквозной API в mock | 3 |" in text
    assert "| pytest -q | ✗ ошибка | 12,3 |" in text
    assert "`tests/test_api.py::test_c`" in text and "`tests/test_api.py::test_e`" in text


def test_report_all_green(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        JUNIT.replace('<failure message="boom">assert 0</failure>', "").replace(
            '<error message="setup"/>', ""
        ),
        encoding="utf-8",
    )
    text = render(parse_junit(junit), [Step("smoke.py", True, 3.0)], _now(), "abc1234", "3.12.1")
    assert "**всё зелёное**" in text and "Упавшие тесты" not in text


def test_report_without_tests_is_not_green(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text('<testsuites><testsuite name="pytest" tests="0"/></testsuites>', encoding="utf-8")
    assert "**есть ошибки**" in render(parse_junit(junit), [], _now(), "abc1234", "3.10.14")


def test_check_sh_runs_every_step() -> None:
    text = (ROOT / "scripts/check.sh").read_text(encoding="utf-8")
    for needle in (
        "export LLM_MODE=mock",
        "ruff check .",
        "ruff check --target-version py310 .",
        "ruff format --check .",
        "pytest -q --junitxml",
        "scripts/smoke.py",
        "scripts/test_report.py",
        "docs/test_report.md",
    ):
        assert needle in text, needle
    bash = shutil.which("bash")
    if bash:
        subprocess.run([bash, "-n", str(ROOT / "scripts/check.sh")], check=True)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.parent.parent.name)
def test_ci_workflow_runs_check_in_mock_without_secrets(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert 'python-version: "3.10"' in text
    assert "pip install -r requirements.txt" in text
    assert "bash scripts/check.sh" in text
    assert "LLM_MODE: mock" in text
    assert "secrets." not in text and "OPENAI_" not in text
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load(text)
    steps = workflow["jobs"]["check"]["steps"]
    assert any("scripts/check.sh" in str(step.get("run", "")) for step in steps)


def test_readme_and_report_are_in_place() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Тесты", 1)[1]
    assert "bash scripts/check.sh" in section and "docs/test_report.md" in section
    assert (ROOT / "docs/test_report.md").read_text(encoding="utf-8").startswith("# Отчёт о прогоне тестов")


def test_report_when_pytest_crashed_is_rewritten_as_failure(tmp_path: Path) -> None:
    from scripts.test_report import main

    out = tmp_path / "report.md"
    out.write_text("# Отчёт о прогоне тестов\n- Итог: **всё зелёное**\n", encoding="utf-8")
    assert main(["--junit", str(tmp_path / "missing.xml"), "--out", str(out)]) == 1
    text = out.read_text(encoding="utf-8")
    assert "**есть ошибки**" in text and "pytest не дошёл до конца" in text and "всё зелёное" not in text


def test_collection_error_is_attributed_to_its_file(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite name="pytest"><testcase classname="" name="tests.test_hours" time="0">'
        '<error message="collection failure"/></testcase></testsuite></testsuites>',
        encoding="utf-8",
    )
    assert list(parse_junit(junit)) == ["tests/test_hours.py"]
