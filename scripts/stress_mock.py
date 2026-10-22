#!/usr/bin/env python3
"""Изолированные mock-прогоны запросов с внешним таймаутом и техническим отчётом."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
import traceback
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
HOURS_CHECKS = frozenset(f"V{index:02d}" for index in range(1, 12))
STRUCTURE_CHECKS = frozenset({"V12", "V13", "V14", "V31"})
WORK_KINDS = ("lk", "pr", "lb", "srs")  # distance — часть контактных часов, а не отдельное слагаемое
MISSING = "нет данных"


@dataclass
class Result:
    query: dict[str, Any]
    command: list[str]
    folder: Path
    seconds: float = 0
    returncode: int | None = None
    timed_out: bool = False
    transcript: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)
    kit: dict[str, Any] = field(default_factory=dict)
    traces: list[str] = field(default_factory=list)
    read_errors: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    session_errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.timed_out or self.returncode == 3:
            return "таймаут"
        return str(self.transcript.get("final_status") or ("ошибка процесса" if self.returncode else MISSING))

    @property
    def issues(self) -> list[dict[str, Any]]:
        if not isinstance(self.report.get("issues"), list):
            return []
        return [
            item for item in self.report.get("issues", []) if isinstance(item, dict) and not item.get("fixed")
        ]


def read_json(path: Path, errors: list[str]) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("ожидался объект JSON")
        return value
    except (OSError, ValueError) as exc:
        errors.append(f"{path.name}: {exc}")
        return {}


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def different(left: float, right: float) -> bool:
    return not math.isclose(left, right, rel_tol=0, abs_tol=1e-6)


def audit_kit(kit: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    """Проверка арифметики и связей независимо от проверок агента, без оценки тематики текста."""
    if not kit:
        return []
    findings: list[str] = []
    program = kit.get("program", {})
    actual_type = program.get("type")
    if expected.get("program_type") and actual_type != expected["program_type"]:
        findings.append(f"STRUCTURE_TYPE: тип {actual_type}, ожидался {expected['program_type']}")
    topics = kit.get("topics")
    if not isinstance(topics, list) or not topics:
        return [*findings, "STRUCTURE_TOPICS: отсутствуют темы"]
    modules = {item.get("id") for item in kit.get("modules", [])}
    ids = [item.get("id") for item in topics]
    if len(ids) != len(set(ids)) or None in ids:
        findings.append("STRUCTURE_IDS: отсутствующие или повторяющиеся ID тем")
    totals = dict.fromkeys(WORK_KINDS, 0.0)
    for topic in topics:
        topic_id = topic.get("id", "?")
        if topic.get("module_id") not in modules:
            findings.append(f"STRUCTURE_IDS: {topic_id} ссылается на отсутствующий модуль")
        hours = topic.get("hours", {})
        values = [number(hours.get(kind)) for kind in WORK_KINDS]
        if any(value is None for value in values):
            findings.append(f"HOURS_DATA: у {topic_id} неполная строка часов")
            return findings
        row_total = sum(value for value in values if value is not None)
        stored = number(hours.get("total"))
        if stored is not None and different(stored, row_total):
            findings.append(f"HOURS_ROW: {topic_id}: сумма {row_total:g}, записано {stored:g}")
        for kind, value in zip(WORK_KINDS, values, strict=True):
            assert value is not None
            totals[kind] += value
            if value < 0 or different(value * 2, round(value * 2)):
                findings.append(
                    f"HOURS_STEP: {topic_id}.{kind}={value:g}: отрицательное значение или не шаг 0,5"
                )
    fa_hours = number((kit.get("final_attestation") or {}).get("hours"))
    if fa_hours is None:
        return [*findings, "HOURS_DATA: не указаны часы итоговой аттестации"]
    total = sum(totals.values()) + fa_hours
    for label, value in (
        ("объём паспорта", program.get("total_hours")),
        ("согласованный объём из expected", expected.get("total_hours")),
        ("вычисленный итог экспорта", kit.get("computed_total_hours")),
    ):
        target = number(value)
        if target is not None and different(total, target):
            findings.append(f"HOURS_TOTAL: темы + ИА = {total:g}; {label} = {target:g}")
    for field_name, actual in (
        ("srs_hours", totals["srs"]),
        ("ia_hours", fa_hours),
        ("contact_hours", sum(totals[k] for k in ("lk", "pr", "lb"))),
    ):
        target = number(expected.get(field_name))
        if target is not None and different(actual, target):
            findings.append(f"HOURS_EXPECTED: {field_name}={actual:g}, ожидалось {target:g}")
    minimum = number(expected.get("min_practice_hours"))
    if minimum is not None and totals["pr"] < minimum - 1e-6:
        findings.append(f"HOURS_PRACTICE: практики {totals['pr']:g}, нужно не менее {minimum:g}")
    days = (kit.get("calendar") or {}).get("days", [])
    if days:
        calendar_total = 0.0
        for day in days:
            items = [number(item.get("hours")) for item in day.get("items", [])]
            if any(value is None for value in items):
                findings.append("HOURS_DATA: нечисловая длительность элемента календаря")
                break
            daily = sum(value for value in items if value is not None)
            calendar_total += daily
            limit = number(expected.get("max_hours_per_day"))
            if limit is not None and daily > limit + 1e-6:
                findings.append(f"HOURS_DAY: день {day.get('n')}: {daily:g} ч, предел {limit:g}")
        else:
            if different(calendar_total, total):
                findings.append(f"HOURS_CALENDAR: календарь {calendar_total:g}, темы + ИА {total:g}")
    return findings


def missed_questions(transcript: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    if not transcript or not expected.get("must_ask_first"):
        return []
    findings: list[str] = []
    if transcript.get("asked_before_building") is False:
        findings.append("не было уточнения до каркаса")
    asked = {question.get("id") for question in transcript.get("questions", [])}
    for key in expected.get("must_ask_about", []):
        if key not in asked:
            findings.append(f"не задан вопрос {key}")
    return findings


def collect_artifacts(result: Result) -> None:
    result.transcript = read_json(result.folder / "transcript.json", result.read_errors)
    result.report = read_json(result.folder / "report.json", result.read_errors)
    result.kit = read_json(result.folder / "kit.json", result.read_errors)
    output = (result.folder / "process.log").read_text(encoding="utf-8", errors="replace")
    marker = "Traceback (most recent call last):"
    if marker in output:
        result.traces.append(output[output.index(marker) :])
    for path in sorted((result.folder / ".sessions").glob("*/log.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("event") == "failed" or entry.get("trace"):
                result.session_errors.append(entry)
            trace = entry.get("trace")
            if isinstance(trace, str) and trace not in result.traces:
                result.traces.append(trace)
    expected = result.query.get("expected", {})
    result.findings = audit_kit(result.kit, expected)
    sections = result.report.get("sections", {})
    absent = [key for key, value in sections.items() if value is False]
    if absent:
        result.findings.append("STRUCTURE_SECTIONS: незаполненные разделы: " + ", ".join(absent))
    result.missed = missed_questions(result.transcript, expected)


def run_one(query: dict[str, Any], queries_file: Path, out_dir: Path, timeout: float) -> Result:
    folder = out_dir / str(query["id"])
    folder.mkdir()
    command = [
        sys.executable,
        "scripts/run_open_query.py",
        "--id",
        str(query["id"]),
        "--queries",
        queries_file.relative_to(ROOT).as_posix(),
        "--mode",
        "mock",
        "--out",
        folder.relative_to(ROOT).as_posix(),
        "--timeout",
        f"{timeout:g}",
    ]
    result = Result(query, command, folder)
    env = {
        **os.environ,
        "LLM_MODE": "mock",
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(ROOT / ".tmp"),
        "PIP_CACHE_DIR": str(ROOT / ".cache/pip"),
        "XDG_CACHE_HOME": str(ROOT / ".cache"),
    }
    started = time.monotonic()
    with (folder / "process.log").open("w", encoding="utf-8") as output:
        try:
            process = subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
            result.returncode = process.returncode
        except subprocess.TimeoutExpired:
            result.timed_out = True
        except OSError:
            result.traces.append(traceback.format_exc())
    result.seconds = time.monotonic() - started
    try:
        collect_artifacts(result)
    except Exception:
        result.read_errors.append(
            "Исключение при разборе результатов, это ошибка измерителя:\n" + traceback.format_exc()
        )
    return result


def cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ") if value is not None else MISSING


def codes(result: Result, severity: str) -> str:
    counts = Counter(
        str(issue.get("check", "?")) for issue in result.issues if issue.get("severity") == severity
    )
    return ", ".join(f"{code}×{count}" for code, count in sorted(counts.items())) or (
        "—" if result.report else MISSING
    )


def render_report(results: Sequence[Result], timeout: float) -> str:
    arithmetic = sum(any(item.startswith("HOURS_") for item in result.findings) for result in results)
    missed = sum(bool(result.missed) for result in results)
    incomplete = sum(result.transcript.get("complete") is False for result in results)
    lines = [
        "# Стресс-проверка агента в mock",
        "",
        f"Завершено до статуса done: {sum(result.status == 'done' for result in results)} из {len(results)}. "
        f"Независимые проверки арифметики и дневного лимита выявили нарушения в {arithmetic} комплектах; "
        f"пропущенные обязательные уточнения — в {missed} запросах. "
        f"Неполными по transcript.complete помечены {incomplete} комплектов. "
        "Статус done и код процесса 0 не означают, что комплект полный или прошёл самопроверку.",
        "",
        f"Запросов: {len(results)}. На каждый процесс установлен внешний лимит {timeout:g} с и "
        "одноимённый внутренний таймаут run_open_query.py. Внешний лимит охватывает также разбор запроса, "
        "уточнения и завершение TestClient, которые внутренний таймер не ограничивает.",
        "",
        "Код агента не менялся. Запуски только LLM_MODE=mock, чтение файлов настроек отключено. "
        "Измерения и журналы — в .tmp/stress/ (либо заданной --out-dir внутри .tmp/). "
        "Время в таблице — время всего процесса; время сценария отдельно берётся из transcript.seconds.",
        "",
        "Тексты mock-содержания взяты из заготовок: несовпадение темы, аудитории или примеров с новым "
        "направлением не является найденной ошибкой. Все коды самопроверки перечислены для полноты, "
        "но выводы ниже ограничены падениями, зависаниями, часами, структурой и уточнениями. "
        "Коды V33–V44 и другие предметные проверки сами по себе не объявляются дефектами. "
        "Исправленные issues (fixed=true) не считаются открытыми ошибками.",
        "",
        "Незаполненные разделы V12 ниже — наблюдаемые структурные пробелы именно mock-результатов. "
        "Они не доказывают аналогичный дефект настоящей модели: общие заготовки могут не дать полей для "
        "нового направления. В ПК вне локального реестра отсутствие подтверждённого ПС и компетенций "
        "с кодами также не следует исправлять выдуманным стандартом. Эти ограничения фиксируются, "
        "но не приравниваются к падениям или предметному несовпадению тем.",
        "",
        "| Запрос | Статус / код процесса / полный комплект | Время процесса / сценария, с | Ошибки проверок | "
        "Предупреждения проверок | Виды предупреждений методисту | Независимые проверки / уточнения |",
        "|---|---|---|---|---|---|---|",
    ]
    for result in results:
        warnings = sorted(
            {
                str(item.get("type", "?")) + (f"/{item['code']}" if item.get("code") else "")
                for item in result.report.get("warnings_for_methodist", [])
            }
        )
        findings = sorted({item.split(":", 1)[0] for item in result.findings})
        if result.missed:
            findings.append("пропущено уточнение")
        lines.append(
            f"| {result.query['id']}: {cell(result.query['title'])} | {cell(result.status)} / "
            f"{cell(result.returncode)} / {cell(result.transcript.get('complete'))} | "
            f"{result.seconds:.2f} / {cell(result.transcript.get('seconds'))} | {codes(result, 'error')} | "
            f"{codes(result, 'warning')} | {cell(', '.join(warnings) or ('—' if result.report else MISSING))} | "
            f"{cell(', '.join(findings) or ('—' if result.kit else MISSING))} |"
        )
    crashes = [
        result
        for result in results
        if result.traces
        or (result.returncode not in (0, 3, None) and not result.timed_out)
        or result.status == "failed"
    ]
    hangs = [
        result for result in results if result.timed_out or result.returncode == 3 or result.seconds > timeout
    ]
    lines += ["", "## Падения и остановки с исключениями", "", f"Обнаружено: {len(crashes)}."]
    for result in crashes:
        error = result.transcript.get("error")
        lines += [
            "",
            f"### {result.query['id']}: {result.query['title']}",
            "",
            f"Статус: {result.status}; код процесса: {result.returncode}; ошибка API: `{json.dumps(error, ensure_ascii=False)}`.",
            "",
            "Команда:",
            "",
            "```bash",
            "LLM_MODE=mock " + shlex.join(["python", *result.command[1:]]),
            "```",
        ]
        if result.session_errors:
            lines += [
                "",
                "Ошибки журнала сессии:",
                "",
                "```json",
                json.dumps(result.session_errors, ensure_ascii=False, indent=2),
                "```",
            ]
        for trace in result.traces:
            lines += ["", "```text", trace.rstrip(), "```"]
        if not result.traces:
            lines += [
                "",
                "Traceback не записан: обработанная остановка, а не доказанное необработанное исключение.",
                "",
                "Вывод процесса:",
                "",
                "```text",
                (result.folder / "process.log").read_text(encoding="utf-8", errors="replace").rstrip(),
                "```",
            ]
    lines += ["", f"## Зависания и превышения лимита {timeout:g} с", "", f"Обнаружено: {len(hangs)}."]
    for result in hangs:
        lines.append(
            f"- {result.query['id']}: {result.seconds:.2f} с; внешний таймаут={result.timed_out}, код={result.returncode}."
        )
    for title, checks, prefix in (
        ("Часы и календарь: ошибки, предупреждения и информация", HOURS_CHECKS, "HOURS_"),
        ("Нарушения структуры шаблона и связей", STRUCTURE_CHECKS, "STRUCTURE_"),
    ):
        lines += ["", f"## {title}", ""]
        count = 0
        for result in results:
            details = [
                f"{issue.get('check')} ({issue.get('severity')}): {issue.get('message')}"
                for issue in result.issues
                if issue.get("check") in checks
            ]
            details += [item for item in result.findings if item.startswith(prefix)]
            if details:
                count += 1
                lines += [
                    f"### {result.query['id']}: {result.query['title']}",
                    "",
                    *[f"- {cell(detail)}" for detail in details],
                    "",
                ]
        if not count:
            lines.append(
                "На сохранённых комплектах и отчётах не обнаружены; отсутствие комплекта не считается прохождением проверок."
            )
    lines += ["", "## Пропущенные уточнения и противоречия", ""]
    for result in results:
        expected = result.query.get("expected", {})
        if not expected.get("must_ask_first"):
            continue
        asked = [question.get("id") for question in result.transcript.get("questions", [])]
        description = expected.get(
            "conflict_description", "Неполный запрос: блокирующие данные требуется уточнить"
        )
        state = (
            "; ".join(result.missed)
            if result.missed
            else ("уточнения заданы" if result.transcript else "нет данных: сценарий не сохранился")
        )
        lines += [f"- **{result.query['id']}**: {description}. Результат: {state}; вопросы: {cell(asked)}."]
        if result.missed:
            calendar = result.kit.get("calendar") or {}
            attestation = result.kit.get("final_attestation") or {}
            lines += [
                f"  Исходный запрос: {cell(result.query['request'])}",
                f"  Наблюдение: календарь {cell(calendar.get('weeks'))} недель; "
                f"итоговая аттестация {cell(attestation.get('hours'))} ч. "
                "Подготовленные ответы методиста применяются только после соответствующего вопроса.",
            ]
        for warning in result.report.get("warnings_for_methodist", []):
            if warning.get("type") in ("constraint_conflict", "schedule_tight"):
                lines.append(f"  Предупреждение агента: {cell(warning.get('text'))}")
    lines += [
        "",
        "Ожидания заданы в expected набора, не выведены из ответов самого агента. "
        "Отсутствие именно вопроса о противоречии фиксируется даже при предупреждении в отчёте: "
        "предупреждение и согласование изменения требований — разные действия. "
        "Если упавший сценарий не оставил transcript, пропуск уточнения не утверждается.",
        "",
        "## Неполные комплекты и ограничения заготовок",
        "",
    ]
    limitations: dict[str, list[str]] = {}
    for result in results:
        for limitation in result.transcript.get("limitations", []):
            limitations.setdefault(str(limitation), []).append(str(result.query["id"]))
    for limitation, ids in limitations.items():
        lines.append(f"- Запросы {', '.join(ids)}: {cell(limitation)}")
    if not limitations:
        lines.append("Ограничения в transcript.limitations не записаны.")
    lines += [
        "",
        "Пропущенный шаг и пустой раздел не являются несовпадением темы. В mock они показывают границу "
        "пригодности сохранённых заготовок для нового каркаса; причина требует отдельного анализа "
        "вне этого прогона. Для стажировок отсутствие лекций, слайдов и тестовых выгрузок допустимо, "
        "если соответствующих занятий нет и причина записана агентом.",
        "",
        "## Полнота данных и воспроизведение",
        "",
    ]
    for result in results:
        missing = [
            name
            for name, value in (
                ("transcript.json", result.transcript),
                ("report.json", result.report),
                ("kit.json", result.kit),
            )
            if not value
        ]
        if missing or result.read_errors:
            lines.append(
                f"- {result.query['id']}: отсутствуют данные {', '.join(missing) or '—'}; ошибки чтения: {cell(result.read_errors)}."
            )
    lines += [
        "",
        "Повтор: `source .venv/bin/activate`, затем `LLM_MODE=mock python scripts/stress_mock.py`. "
        "Скрипт не перезаписывает существующую папку результатов: для повторного запуска указать, например, "
        "`--out-dir .tmp/stress-repeat --report .tmp/stress_repeat_report.md`. "
        "Каждый process.log содержит полный объединённый вывод stdout/stderr. "
        "Внутри .sessions сохранены state.json и log.jsonl, если агент успел создать сессию. "
        "Код 0 скрипта означает завершённый сбор отчёта, а не отсутствие найденных проблем.",
        "",
        "Созданы eval/stress_queries.json, scripts/stress_mock.py и этот отчёт. "
        "Временные артефакты — только внутри .tmp/. Агент, шаблоны и остальные исходные файлы не изменялись. "
        "Работа настоящего шлюза и семантическое качество нового предметного содержания не проверялись.",
    ]
    return "\n".join(lines) + "\n"


def project_path(value: Path) -> Path:
    path = (ROOT / value).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError("Путь должен находиться внутри проекта")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--queries", type=Path, default=Path("eval/stress_queries.json"), help="Файл набора запросов"
    )
    parser.add_argument(
        "--out-dir", type=Path, default=Path(".tmp/stress"), help="Новая папка результатов внутри .tmp/"
    )
    parser.add_argument(
        "--report", type=Path, default=Path(".tmp/stress_report.md"), help="Отчёт внутри .tmp/"
    )
    parser.add_argument(
        "--timeout", type=float, default=300, help="Лимит одного процесса в секундах, не больше 300"
    )
    args = parser.parse_args(argv)
    try:
        queries_file, out_dir, report_path = (
            project_path(value) for value in (args.queries, args.out_dir, args.report)
        )
        if queries_file.suffix != ".json" or queries_file.name in {".env", ".env.example"}:
            raise ValueError("Набор должен быть файлом JSON")
        if not all(path.is_relative_to(ROOT / ".tmp") for path in (out_dir, report_path)):
            raise ValueError("Результаты и отчёт разрешены только внутри .tmp/")
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 300:
            raise ValueError("Таймаут должен быть больше 0 и не больше 300 секунд")
        data = json.loads(queries_file.read_text(encoding="utf-8"))
        queries = data["queries"]
        if not isinstance(queries, list) or not queries:
            raise ValueError("В наборе нет списка запросов")
        ids = [str(query["id"]) for query in queries]
        if len(ids) != len(set(ids)) or any(not re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in ids):
            raise ValueError("ID запросов должны быть уникальными и пригодными для имён папок")
        if out_dir.exists():
            raise ValueError("Папка результатов уже существует; выберите новую через --out-dir")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    out_dir.mkdir(parents=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    results: list[Result] = []
    for query in queries:
        result = run_one(query, queries_file, out_dir, args.timeout)
        results.append(result)
        report_path.write_text(render_report(results, args.timeout), encoding="utf-8")
        print(f"{query['id']}: {result.status}; {result.seconds:.2f} с; код {result.returncode}", flush=True)
    print(f"Отчёт: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
