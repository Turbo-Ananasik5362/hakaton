#!/usr/bin/env python3
"""Расчёт эффекта по сохранённым прогонам, без вызовов модели и изменения исходных данных."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MISSING = "нет данных"
# Диапазон ручной подготовки из docs/case/case.md, раздел «Описание проблемы».
MANUAL_WEEKS = (2, 8)


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

    @property
    def finished(self) -> bool:
        return self.complete is True and self.status == "done"


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


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


def _pair(values: list[float]) -> str:
    if not values:
        return f"{MISSING} / {MISSING}"
    return f"{cell(mean(values))} / {cell(max(values))} (n={len(values)})"


def render_report(runs: Sequence[RunMetrics], hours_per_week: float = 40, review_hours: float = 4) -> str:
    if any(not math.isfinite(value) or value <= 0 for value in (hours_per_week, review_hours)):
        raise ValueError("Часы рабочей недели и правки должны быть положительными конечными числами")
    lines = [
        "# Эффект подготовки образовательной программы",
        "",
        "Отчёт строится по сохранённым файлам командой `LLM_MODE=mock python scripts/effect.py`. "
        "Скрипт не вызывает модель и не изменяет архив прогонов.",
        "",
        "## Измерения по прогонам",
        "",
        f"Найдено папок: {len(runs)}. Объём — заявленные часы `kit.program.total_hours`, "
        "тип — `kit.program.type` (pk — повышение квалификации, pk_internship — стажировка, "
        "dop — общеразвивающая программа). "
        "Пустые папки также показаны. Отсутствующие, некорректные и нечисловые значения — «нет данных»; "
        "явно записанный ноль сохраняется.",
        "",
        "| Прогон | Тип | Объём, ч | До каркаса, с | Сборка, с | Стоимость, ₽ | Вызовы¹ | Из кеша¹ | "
        "Режим | Статус / полный комплект / автопроверка |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for run in runs:
        complete = None if run.complete is None else ("да" if run.complete else "нет")
        status = " / ".join(cell(value) for value in (run.status, complete, run.report_status))
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
    lines += [
        "",
        "До каркаса — `t` первого события `skeleton_ready` в `transcript.events`: от старта сценария, "
        "включая уточнения и ожидание ответов, если оно было записано. Сборка — разность `t` первого "
        "`result/done` и `approve`; она включает очередь, генерацию, проверку и выгрузку до получения результата. "
        "Точечные правки после первого результата во время сборки не входят. Если таких событий нет и нет правок, "
        "используется явно записанное `report.generation_seconds`; это отмечено ниже. Времена параллельных шагов "
        "не суммируются. У неполного комплекта время означает завершение попытки, а не готовность всех материалов.",
        "",
        "Стоимость — только записанная `cost.total_rub`, за весь прогон. Она может включать правки; "
        "стоимость из `report.cost_rub` не подставляется: её охват уже (обычно без подготовки каркаса). "
        "Для удалённого сервера локальный ноль-заглушка не считается измерением.",
        "",
        "¹ Вызовы — сумма `cost.by_step.*.calls`, включая попадания в кеш; «Из кеша» — сумма поля `cached`. "
        "Это учтённые обращения клиента, а не гарантированное число сетевых попыток шлюза. "
        "Пустой `by_step` без явных счётчиков не заменяется нулём. Стоимость повторов с кешем может быть ниже "
        "стоимости первой генерации; одинаковые запросы не считаются независимыми замерами холодного старта.",
        "",
        "## Сводка по объёмам",
        "",
        "Группировка по объёму и режиму; в парах указаны среднее / максимум и число измерений n. "
        "Для времени сборки учитываются только `complete=true` и `final_status=done` — наличие файлов "
        "не гарантирует отсутствие ошибок автопроверки. Стоимость включает все записанные попытки "
        "(в том числе неполные), но только с однозначным режимом live. Mock, replay и неизвестный/смешанный "
        "режим исключены из сводки стоимости. Нулевой расход live из кеша остаётся реальным нулём.",
        "",
        "| Объём, ч | Режим | Папок / полных | До каркаса, с: ср. / макс. | "
        "Сборка полного комплекта, с: ср. / макс. | Стоимость live, ₽: ср. / макс. | Вызовы¹: ср. / макс. |",
        "|---|---|---|---|---|---|---|",
    ]
    groups: dict[tuple[float | None, str | None], list[RunMetrics]] = defaultdict(list)
    for run in runs:
        groups[run.hours, run.mode].append(run)
    for (hours, mode), group in sorted(
        groups.items(), key=lambda item: (item[0][0] is None, item[0][0] or 0, item[0][1] or "")
    ):
        skeletons = [run.skeleton_seconds for run in group if run.skeleton_seconds is not None]
        generations = [
            run.generation_seconds for run in group if run.finished and run.generation_seconds is not None
        ]
        costs = [run.cost_rub for run in group if run.mode == "live" and run.cost_rub is not None]
        calls = [float(run.calls) for run in group if run.calls is not None]
        lines.append(
            f"| {cell(hours)} | {cell(mode)} | {len(group)} / {sum(run.finished for run in group)} | "
            f"{_pair(skeletons)} | {_pair(generations)} | {_pair(costs)} | {_pair(calls)} |"
        )
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
        "Расчёт универсален и не читает список открытых запросов.",
        "",
        "## Замечания к источникам",
        "",
    ]
    notes = [f"- `{cell(run.path)}`: {cell(note)}." for run in runs for note in run.notes]
    lines += notes or ["Дополнительных замечаний к чтению источников нет."]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, default=ROOT / "eval/runs", help="Архив папок */q*/")
    parser.add_argument("--out", type=Path, default=ROOT / "docs/effect.md", help="Путь отчёта Markdown")
    parser.add_argument("--hours-per-week", type=float, default=40, help="Допущение: часов в рабочей неделе")
    parser.add_argument("--review-hours", type=float, default=4, help="Допущение: часов проверки и правки")
    args = parser.parse_args(argv)
    if not args.runs_dir.is_dir():
        parser.error("Папка прогонов не найдена")
    try:
        text = render_report(collect_runs(args.runs_dir), args.hours_per_week, args.review_hours)
    except ValueError as exc:
        parser.error(str(exc))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"Отчёт записан: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
