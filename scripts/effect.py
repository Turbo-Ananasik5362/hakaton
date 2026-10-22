#!/usr/bin/env python3
"""Расчёт эффекта по сохранённым прогонам, без вызовов модели и изменения исходных данных."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MISSING = "нет данных"
# Диапазон ручной подготовки из docs/case/case.md, раздел «Описание проблемы».
MANUAL_WEEKS = (2, 8)
MAX_CACHED_SHARE = 0.2


@dataclass(frozen=True)
class QueryInfo:
    id: str
    program_type: str | None
    hours: float | None


@dataclass(frozen=True)
class RunMetrics:
    path: str
    program_type: str | None
    hours: float | None
    skeleton_seconds: float | None
    generation_seconds: float | None
    cost_rub: float | None
    calls: int | None
    cached_calls: int | None
    mode: str | None
    complete: bool | None
    status: str | None
    report_status: str | None
    notes: tuple[str, ...]
    query_id: str | None = None
    recorded_at: dt.datetime | None = None

    @property
    def finished(self) -> bool:
        return self.complete is True and self.status == "done"

    @property
    def cached_share(self) -> float | None:
        if self.calls is None or self.calls <= 0 or self.cached_calls is None:
            return None
        if not 0 <= self.cached_calls <= self.calls:
            return None
        return self.cached_calls / self.calls


def exclusion_reasons(run: RunMetrics) -> tuple[str, ...]:
    reasons: list[str] = []
    if run.mode != "live":
        reasons.append("режим не live или не определён однозначно")
    if run.complete is not True:
        reasons.append("нет подтверждения полного комплекта (transcript.complete=true)")
    if run.cached_share is None:
        reasons.append("доля кеша неизвестна или некорректна")
    elif run.cached_share > MAX_CACHED_SHARE:
        reasons.append("доля кеша больше 20 %")
    return tuple(reasons)


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def query_id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return str(value).strip() or None


def _timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.astimezone(dt.timezone.utc) if result.tzinfo is not None else None


def read_queries(path: Path) -> list[QueryInfo]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Не удалось прочитать список открытых запросов") from exc
    items = mapping(data).get("queries")
    if not isinstance(items, list):
        raise ValueError("В списке открытых запросов нет массива queries")
    result: list[QueryInfo] = []
    for item in items:
        item = mapping(item)
        identity = query_id(item.get("id"))
        if identity is None or any(query.id == identity for query in result):
            raise ValueError("У открытых запросов должны быть непустые уникальные id")
        expected = mapping(item.get("expected"))
        result.append(
            QueryInfo(identity, string(expected.get("program_type")), number(expected.get("total_hours")))
        )
    return result


def _read(path: Path, notes: list[str]) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        notes.append(f"{path.name}: не удалось прочитать JSON ({type(exc).__name__})")
        return {}
    if not isinstance(value, dict):
        notes.append(f"{path.name}: ожидался объект JSON")
        return {}
    return value


def _event_time(events: list[dict[str, Any]], status: str, action: str | None = None) -> float | None:
    for event in events:
        if event.get("status") == status and (action is None or event.get("action") == action):
            value = number(event.get("t"))
            if value is not None:
                return value
    return None


def _count_steps(steps: Any, field: str) -> int | None:
    if not isinstance(steps, dict) or not steps:
        return None
    values = [number(mapping(step).get(field)) for step in steps.values()]
    if any(value is None or not value.is_integer() for value in values):
        return None
    return sum(int(value) for value in values if value is not None)


def read_run(path: Path, label: str) -> RunMetrics:
    notes: list[str] = []
    cost, transcript, report, kit = (
        _read(path / name, notes) for name in ("cost.json", "transcript.json", "report.json", "kit.json")
    )
    program, meta = mapping(kit.get("program")), mapping(kit.get("meta"))
    modes = {
        value.lower()
        for raw in (cost.get("mode"), transcript.get("mode"), meta.get("llm_mode"))
        if (value := string(raw)) is not None
    }
    effective = modes - {"server"}
    mode = " / ".join(sorted(effective or modes)) or None
    if len(effective) > 1:
        notes.append("режимы в исходных файлах расходятся; стоимость исключена из сводки")
    raw_events = transcript.get("events")
    events = (
        [event for event in raw_events if isinstance(event, dict)] if isinstance(raw_events, list) else []
    )
    skeleton = _event_time(events, "skeleton_ready")
    approved = next((number(event.get("t")) for event in events if event.get("action") == "approve"), None)
    done = _event_time(events, "done", "result")
    generation = None
    if approved is not None and done is not None and done >= approved:
        generation = done - approved
    elif not transcript.get("patches"):
        generation = number(report.get("generation_seconds"))
        if generation is not None:
            notes.append("время сборки взято из report.generation_seconds, без ожидания утверждения")
    if transcript.get("patches"):
        notes.append("стоимость и вызовы всего прогона могут включать точечные правки")
    remote = "server" in modes or (
        isinstance(transcript.get("transport"), str)
        and transcript["transport"].startswith(("http://", "https://"))
    )
    cost_rub = number(cost.get("total_rub"))
    calls = _count_steps(cost.get("by_step"), "calls")
    cached = _count_steps(cost.get("by_step"), "cached")
    if remote:
        cost_rub, calls, cached = None, None, None
        notes.append("удалённый сервер: локальная сводка расходов не измеряет его стоимость и вызовы")
    identity = query_id(transcript.get("query_id"))
    if identity is None and (match := re.match(r"q(\d+)(?:\D|$)", path.name)):
        identity = match.group(1)
    recorded_at = _timestamp(transcript.get("finished_at")) or _timestamp(transcript.get("started_at"))
    return RunMetrics(
        path=label,
        program_type=string(program.get("type")),
        hours=number(program.get("total_hours")),
        skeleton_seconds=skeleton,
        generation_seconds=generation,
        cost_rub=cost_rub,
        calls=calls,
        cached_calls=cached,
        mode=mode,
        complete=transcript.get("complete") if isinstance(transcript.get("complete"), bool) else None,
        status=string(transcript.get("final_status")),
        report_status=string(report.get("status")),
        notes=tuple(notes),
        query_id=identity,
        recorded_at=recorded_at,
    )


def collect_runs(runs_dir: Path) -> list[RunMetrics]:
    return [
        read_run(path, f"{runs_dir.name}/{path.relative_to(runs_dir).as_posix()}")
        for path in sorted(runs_dir.glob("*/q*"))
        if path.is_dir()
    ]


def cell(value: str | float | int | None) -> str:
    if value is None:
        return MISSING
    if isinstance(value, (int, float)):
        return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return value.replace("|", "\\|").replace("\n", " ")


def _average(values: list[float]) -> str:
    return f"{cell(mean(values))} (n={len(values)})" if values else MISSING


def _summary_rows(runs: Sequence[RunMetrics], queries: Sequence[QueryInfo]) -> list[str]:
    latest: dict[str, RunMetrics] = {}
    minimum = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    for run in runs:
        if run.query_id is None:
            continue
        previous = latest.get(run.query_id)
        key = (run.recorded_at is not None, run.recorded_at or minimum, run.path)
        if previous is None or key > (
            previous.recorded_at is not None,
            previous.recorded_at or minimum,
            previous.path,
        ):
            latest[run.query_id] = run
    lines = [
        "| Запрос | Тип | Объём, ч | Последний подходящий прогон | До каркаса, с | Сборка, с | Стоимость, ₽ |",
        "|---|---|---|---|---|---|---|",
    ]
    selected: list[RunMetrics] = []
    for query in queries:
        run = latest.get(query.id)
        if run is None:
            values = (query.id, query.program_type, query.hours, None, None, None, None)
        else:
            selected.append(run)
            values = (
                query.id,
                run.program_type,
                run.hours,
                run.path,
                run.skeleton_seconds,
                run.generation_seconds,
                run.cost_rub,
            )
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    skeletons = [run.skeleton_seconds for run in selected if run.skeleton_seconds is not None]
    generations = [run.generation_seconds for run in selected if run.generation_seconds is not None]
    costs = [run.cost_rub for run in selected if run.cost_rub is not None]
    lines.append(
        f"| Среднее | — | — | Запросов с замером: {len(selected)} | {_average(skeletons)} | "
        f"{_average(generations)} | {_average(costs)} |"
    )
    return lines


def render_report(
    runs: Sequence[RunMetrics],
    hours_per_week: float = 40,
    review_hours: float = 4,
    queries: Sequence[QueryInfo] | None = None,
) -> str:
    if any(not math.isfinite(value) or value <= 0 for value in (hours_per_week, review_hours)):
        raise ValueError("Часы рабочей недели и правки должны быть положительными конечными числами")
    accepted = [run for run in runs if not exclusion_reasons(run)]
    rejected = [run for run in runs if exclusion_reasons(run)]
    reasons = Counter(reason for run in rejected for reason in exclusion_reasons(run))
    excluded = "; ".join(f"{reason} — {count}" for reason, count in sorted(reasons.items())) or "нет"
    if queries is None:
        known = {
            run.query_id: QueryInfo(run.query_id, run.program_type, run.hours)
            for run in runs
            if run.query_id is not None
        }
        queries = [known[key] for key in sorted(known)]
    lines = [
        "# Эффект подготовки образовательной программы",
        "",
        "Отчёт строится по сохранённым файлам командой `LLM_MODE=mock python scripts/effect.py`. "
        "Скрипт не вызывает модель и не изменяет архив прогонов.",
        "",
        "## Измерения по прогонам",
        "",
        f"Найдено папок: {len(runs)}. В таблице только live, `transcript.complete=true` и доля вызовов "
        "из кеша не больше 20 %. Полнота означает наличие комплекта по transcript, а не отсутствие "
        "ошибок автопроверки: её статус показан отдельно и не влияет на отбор. "
        "Объём — `kit.program.total_hours`, тип — `kit.program.type` "
        "(pk — повышение квалификации, pk_internship — стажировка, dop — общеразвивающая программа). "
        "Отсутствующие значения — «нет данных», явно записанный ноль сохраняется.",
        "",
        f"Исключено {len(rejected)} отладочных прогонов: {excluded}. Причины могут пересекаться.",
        "",
        "| Прогон | Тип | Объём, ч | До каркаса, с | Сборка, с | Стоимость, ₽ | Вызовы¹ | Из кеша¹ | "
        "Режим | Статус / полный комплект / автопроверка |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for run in accepted:
        status = " / ".join(cell(value) for value in (run.status, "да", run.report_status))
        values = (
            run.path,
            run.program_type,
            run.hours,
            run.skeleton_seconds,
            run.generation_seconds,
            run.cost_rub,
            run.calls,
            run.cached_calls,
            run.mode,
            status,
        )
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    if not runs:
        lines += ["", "Прогоны не найдены; измерений нет."]
    elif not accepted:
        lines += ["", "Подходящих прогонов нет; измерений для таблицы и сводки нет."]
    lines += [
        "",
        "До каркаса — `t` первого события `skeleton_ready` в `transcript.events`: от старта сценария, "
        "включая записанные уточнения и ожидание ответов. Сборка — разность `t` первого `result/done` "
        "и `approve`, включая очередь, генерацию, проверку и выгрузку. Последующие точечные правки "
        "не входят во время сборки. Если событий нет и нет правок, используется явно записанное "
        "`report.generation_seconds`; это отмечено ниже. Времена параллельных шагов не суммируются.",
        "",
        "Стоимость — записанная `cost.total_rub` за весь прогон, она может включать правки. "
        "`report.cost_rub` не подставляется: его охват уже (обычно без подготовки каркаса). "
        "Для удалённого сервера локальный ноль не считается измерением.",
        "",
        "¹ Вызовы — сумма `cost.by_step.*.calls`, включая кеш; «Из кеша» — сумма `cached`. "
        "Доля кеша — cached / calls. При неизвестных счётчиках, calls=0 или cached > calls долю "
        "подтвердить нельзя, такой прогон исключается. Это учтённые обращения клиента, "
        "а не гарантированное число сетевых попыток шлюза.",
        "",
        "## Сводка по открытым запросам",
        "",
        "По одному последнему подходящему прогону на каждый запрос из списка `--queries` "
        "(по умолчанию `eval/open_queries.json`). "
        "Тип и объём выбранного замера берутся из kit; если замера нет — из expected запроса. "
        "Последний определяется по `transcript.finished_at`, при его отсутствии — по `started_at` "
        "(даты с часовым поясом). Без даты или при равных датах используется порядок имён папок; "
        "замеры с известной датой идут после замеров без даты. ID — `transcript.query_id`, "
        "если он отсутствует — номер папки qN. ID не определяется по объёму: разные запросы на один "
        "объём остаются разными строками. Среднее считается по последним замерам, а не по всем попыткам; "
        "неизвестные значения не включаются, n указано отдельно для каждой метрики. "
        "Запрос без подходящего прогона остаётся с «нет данных» и не влияет на среднее.",
        "",
        *_summary_rows(accepted, queries),
    ]
    lower, upper = (weeks * hours_per_week for weeks in MANUAL_WEEKS)
    saved_low, saved_high = lower - review_hours, upper - review_hours
    lines += [
        "",
        "## Экономия времени методиста: сценарный расчёт",
        "",
        "Основа — [описание кейса](case/case.md), разделы 2 и 5: ручная подготовка занимает "
        "от двух до восьми недель, целевой результат — комплект, который можно поправить за несколько часов. "
        "Это исходная оценка кейса и цель, а не измеренный результат работы методистов с этим агентом.",
        "",
        "Отдельные допущения:",
        "",
        f"- В рабочей неделе {cell(hours_per_week)} часов; все эти часы условно отнесены к одной программе. "
        "Календарные недели из кейса не являются измеренными трудозатратами.",
        f"- Проверка и правка методистом занимают {cell(review_hours)} часа на программу. "
        "Это выбранная интерпретация «нескольких часов», требующая проверки с методистами.",
        "- Полный комплект действительно пригоден к такой правке; неполные или ошибочные прогоны этого не доказывают.",
        "- Экспертная вычитка, согласования, подписание, обучение сотрудников и внедрение не оценены отдельно.",
        "- Время автоматической сборки — время машины, а не часы труда методиста; для календарного срока "
        "его нужно прибавить к правке и ожиданию согласований. Оплата труда и денежная экономия не рассчитаны.",
        "",
        f"Ручная работа: 2–8 × {cell(hours_per_week)} = **{cell(lower)}–{cell(upper)} ч**. "
        f"Сценарная экономия труда: {cell(lower)}–{cell(upper)} − {cell(review_hours)} = "
        f"**{cell(saved_low)}–{cell(saved_high)} ч на программу**, "
        f"или {cell(100 * saved_low / lower)}–{cell(100 * saved_high / upper)} %. "
        "Эти числа нельзя выдавать за подтверждённый эффект: длительность правки пока не измерена.",
        "",
        "Параметры можно изменить: `python scripts/effect.py --hours-per-week 40 --review-hours 4`. "
        "Другой архив/вывод: `--runs-dir <папка> --out <путь внутри проекта>`. "
        "Список запросов для сводки можно изменить через `--queries <файл JSON>`; ID и объёмы не зашиты в коде.",
        "",
        "## Замечания к источникам",
        "",
    ]
    notes = [f"- `{cell(run.path)}`: {cell(note)}." for run in accepted for note in run.notes]
    lines += notes or ["Дополнительных замечаний к чтению источников нет."]
    return "\n".join(lines) + "\n"


def project_path(value: Path) -> Path:
    path = (ROOT / value).resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise ValueError("Путь должен находиться внутри проекта")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, default=ROOT / "eval/runs", help="Архив папок */q*/")
    parser.add_argument("--out", type=Path, default=ROOT / "docs/effect.md", help="Путь отчёта Markdown")
    parser.add_argument(
        "--queries", type=Path, default=ROOT / "eval/open_queries.json", help="Список открытых запросов"
    )
    parser.add_argument("--hours-per-week", type=float, default=40, help="Допущение: часов в рабочей неделе")
    parser.add_argument("--review-hours", type=float, default=4, help="Допущение: часов проверки и правки")
    args = parser.parse_args(argv)
    try:
        out = project_path(args.out)
        if not args.runs_dir.is_dir():
            raise ValueError("Папка прогонов не найдена")
        queries = read_queries(args.queries)
        text = render_report(collect_runs(args.runs_dir), args.hours_per_week, args.review_hours, queries)
    except ValueError as exc:
        parser.error(str(exc))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"Отчёт записан: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
