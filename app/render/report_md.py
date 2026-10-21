"""Отчёт автопроверки для методиста: report.md, приложение «Отчёт автопроверки» в программе .docx и раздел
в веб-интерфейсе (app/web/report_page.py).

Отчёт строится один раз (build_report) как набор разделов из абзацев, списков, таблиц и карточек предупреждений;
render_report_md, app/render/report_docx.py и app/web/ только раскладывают его. Числа — из ValidationReport
(report.json); если сводок там нет (комплект прежней версии), они считаются по Kit. Пишется человеческим языком:
итог одной фразой, предупреждения методисту с цифрами и вариантами решения, что исправить и как. Без LLM.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from app.core import methodist_warnings as mw
from app.core.bibliography import REJECTED_NOTE_PREFIX
from app.core.hours import fmt_hours, plural
from app.core.ids import display_numbers
from app.core.validate import (
    ValidationContext,
    copy_summary,
    coverage_matrix,
    hours_summary,
    section_status,
    source_summary,
)
from app.models import (
    CopySummary,
    FixBy,
    GenerationMode,
    HoursRow,
    HoursSummary,
    Issue,
    Kit,
    LOCoverage,
    MethodistWarning,
    ProgramType,
    ReportStatus,
    Severity,
    Verification,
)
from app.pipeline.content import JUDGE_TEXTS

REPORT_MD_NAME = "report.md"
MAX_LISTED = 25  # больше замечаний одного вида — сводной строкой
HOURS_CHECKS = {f"V{n:02d}" for n in range(1, 12)}
SAMPLE_CHECK = "V45"  # ошибки приложенного образца: в программу не переносятся, к комплекту не относятся
COPY_CHECK = "V42"
SAMPLE_QUOTE_RE = re.compile(r"\s*\(«[^»]*»\)")  # цитата образца в замечании о совпадении

FIX_HINTS = {
    FixBy.CODE: "повторите сборку; если замечание останется — это сбой программы, сообщите разработчикам",
    FixBy.LLM: "перегенерируйте этот фрагмент точечной правкой или поправьте текст вручную",
    FixBy.HUMAN: "нужно решение методиста: поправьте текст или параметры запроса и пересоберите комплект",
}
STEP_TITLES = {
    "intake": "Разбор запроса",
    "clarify": "Уточняющие вопросы",
    "skeleton": "Каркас",
    "plan": "План генерации",
    "program_texts": "Тексты программы",
    "module_content": "Рабочая программа модулей",
    "module_pack": "Модуль целиком (экономный режим)",
    "lectures": "Лекции",
    "seminars": "Практические задания",
    "tests": "Тесты",
    "refill": "Добор недостающего",
    "final_attestation": "Итоговая аттестация",
    "literature": "Литература",
    "judge": "Смысловая проверка",
    "patch": "Точечная правка",
}
COVERAGE_TEXT = {
    "ok": "в порядке",
    "single": "одно оценочное средство",
    "unassessed": "не проверяется",
    "no_topic": "нет темы",
}
ASSESSMENT_NAMES = {
    "test_questions": "вопросы теста",
    "seminars": "задания",
    "interview": "собеседование",
    "final_attestation": "итоговая аттестация",
}
VERIFICATION_NAMES = {
    Verification.REGISTRY: "реестр нормативных актов и профстандартов",
    Verification.CATALOG: "каталог проверенных изданий",
    Verification.ONLINE: "онлайн-сверка",
}
OK, WARN, ERROR = "ok", "warn", "error"  # тон строки таблицы: цвет итоговой колонки в интерфейсе
WARNINGS_INTRO = (
    "Здесь — ситуации, где нужно решение методиста или заказчика: агент нашёл их сам по общим правилам. "
    "У каждого предупреждения — цифры и варианты решения."
)
NO_WARNINGS = (
    "Предупреждений нет: срок и нагрузка сходятся, источники подтверждены, требования запроса совместимы."
)
FULL_VERSION_NOTE = (
    "Полная версия отчёта — файл report.md: в ней дословные формулировки замечаний к образцу, цитаты приложенного "
    "образца, комментарии смысловой проверки, отброшенные источники и стоимость сборки. В программу они не "
    "включаются."
)


# --------------------------------------------------------------------------------------------------
# Структура отчёта
# --------------------------------------------------------------------------------------------------


@dataclass
class Para:
    text: str
    block: ClassVar[str] = "para"


@dataclass
class Bullets:
    items: list[str]
    block: ClassVar[str] = "bullets"


@dataclass
class Table:
    header: list[str]
    rows: list[list[str]]
    weights: list[int]  # доли ширины колонок для .docx
    tones: list[str] = field(
        default_factory=list
    )  # по строке: ok / warn / error / "" — цвет последней колонки
    block: ClassVar[str] = "table"


@dataclass
class Advice:
    """Предупреждение методисту: заголовок, что не так (с цифрами), главные цифры, варианты решения."""

    kind: str  # вид предупреждения (WarningType.value) — для оформления
    title: str
    text: str
    figures: list[tuple[str, str]]
    options: list[str]
    block: ClassVar[str] = "advice"


Block = Para | Bullets | Table | Advice


@dataclass
class Section:
    title: str
    blocks: list[Block] = field(default_factory=list)


@dataclass
class ReportDoc:
    title: str
    status: ReportStatus | None
    summary: str
    sections: list[Section] = field(default_factory=list)


# --------------------------------------------------------------------------------------------------
# Итог
# --------------------------------------------------------------------------------------------------


def _open(kit: Kit) -> list[Issue]:
    """Открытые замечания к самому комплекту (без ошибок приложенного образца)."""
    if kit.report is None:
        return []
    return [i for i in kit.report.issues if not i.fixed and i.check != SAMPLE_CHECK]


def _sample_issues(kit: Kit) -> list[Issue]:
    return [i for i in kit.report.issues if i.check == SAMPLE_CHECK] if kit.report is not None else []


def _warnings(kit: Kit) -> list[MethodistWarning]:
    return mw.ordered(kit.report.warnings_for_methodist) if kit.report is not None else []


def _counts(kit: Kit) -> tuple[int, int, int]:
    """(ошибки, предупреждения методисту, замечания автопроверки уровня «предупреждение») — как в разделе «Коротко»."""
    issues = _open(kit)
    errors = sum(1 for i in issues if i.severity == Severity.ERROR)
    remarks = sum(1 for i in issues if i.severity == Severity.WARNING)
    return errors, len(_warnings(kit)), remarks


def _warnings_phrase(methodist: int, remarks: int) -> str:
    """«3 предупреждения методисту и 2 замечания автопроверки» — нулевые части опускаются."""
    parts = []
    if methodist:
        parts.append(_n(methodist, "предупреждение", "предупреждения", "предупреждений") + " методисту")
    if remarks:
        parts.append(_n(remarks, "замечание", "замечания", "замечаний") + " автопроверки")
    return " и ".join(parts)


def _n(n: int, one: str, few: str, many: str) -> str:
    return f"{n} {plural(n, one, few, many)}"


def summary_line(kit: Kit, notes: Sequence[str] = (), see: str = "см. ниже") -> str:
    """Итог одной фразой: «Комплект собран, ошибок нет; 2 предупреждения методисту и 1 замечание автопроверки — см. ниже»."""
    if kit.report is None:
        return "Комплект собран, но автопроверка не выполнялась — проверьте материалы вручную."
    errors, methodist, remarks = _counts(kit)
    warnings = _warnings_phrase(methodist, remarks)
    if errors:
        text = (
            f"Комплект собран, но автопроверка нашла {_n(errors, 'ошибку', 'ошибки', 'ошибок')} — их нужно исправить "
            "до утверждения программы"
        )
        if warnings:
            text += f"; ещё {warnings}"
        text += f" — {see}."
    elif warnings:
        text = f"Комплект собран, ошибок нет; {warnings} — {see}."
    else:
        text = "Комплект собран, ошибок и предупреждений нет."
    sample = len(_sample_issues(kit))
    if sample:
        text += f" В приложенном образце найдено ошибок: {sample}; в программу они не перенесены."
    if _skipped(kit, notes):
        text += " Пояснения к сборке — в разделе «Что пропущено и почему»."
    return text


def api_message(kit: Kit, notes: Sequence[str] = (), has_report_md: bool = True) -> str:
    """Для поля message ответа done: итог автопроверки, главное замечание, о чём предупреждения и где подробности."""
    text = summary_line(kit, notes, see="см. отчёт")
    errors = [i for i in _open(kit) if i.severity == Severity.ERROR]
    if errors:
        first = errors[0].message.strip()
        text += f" Главное: {first[:160]}{'…' if len(first) > 160 else ''}"
    titles = [mw.title_of(w) for w in _warnings(kit)]
    if titles:
        text += f" Предупреждения методисту (с цифрами и вариантами решения): {'; '.join(titles)}."
    if kit.report is not None and has_report_md:
        text += " Подробный отчёт — файл report.md (files.report_md)."
    elif kit.report is not None:
        text += " Подробный отчёт — в поле report и файле report.json."
    return text


def _skipped(kit: Kit, notes: Sequence[str]) -> list[str]:
    return list(dict.fromkeys([*notes, *kit.meta.generation_notes]))


# --------------------------------------------------------------------------------------------------
# Сборка разделов
# --------------------------------------------------------------------------------------------------


def build_report(
    kit: Kit,
    notes: Sequence[str] = (),
    references: Sequence[str] = (),
    files: Sequence[str] = (),
    for_docx: bool = False,
) -> ReportDoc:
    """notes — заметки сессии (пропущенные шаги); references — тексты приложенных образцов (совпадение, если
    в отчёте проверки его ещё нет); files — имена выданных файлов (раздел о проверке выгрузок).

    for_docx — вариант для приложения к программе: в документ не идут тексты, которые правила не пускают в
    программу — цитаты и числа часов приложенного образца, номера утративших силу актов, свободный текст
    модели-судьи, названия и ISBN отброшенных моделью изданий, стоимость сборки. Полная версия — report.md."""
    report = kit.report
    doc = ReportDoc(
        title=f"Отчёт автопроверки: «{kit.program.title}»",
        status=report.status if report is not None else None,
        summary=summary_line(kit, notes),
    )
    doc.sections.append(_overview(kit))
    history = _history(kit, for_docx)
    if history is not None:
        doc.sections.append(history)
    errors = _errors(kit)
    if errors is not None:
        doc.sections.append(errors)
    doc.sections.append(_methodist(kit, for_docx))
    remarks = _remarks(kit, for_docx)
    if remarks is not None:
        doc.sections.append(remarks)
    sample = _sample(kit) if not for_docx else None
    if sample is not None:
        doc.sections.append(sample)
    doc.sections.append(_hours(kit))
    doc.sections.append(_coverage(kit))
    doc.sections.append(_sources(kit))
    doc.sections.append(_template(kit))
    doc.sections.append(_copying(kit, references, for_docx))
    doc.sections.append(_judge(kit, for_docx))
    doc.sections.append(_skipped_section(kit, notes, for_docx))
    if not for_docx:  # стоимость вызовов модели и токены — для команды, не для программы на утверждение
        doc.sections.append(_cost(kit))
    if files:
        doc.sections.append(_exports(kit, files))
    if for_docx:
        doc.sections.append(Section("Полная версия", [Para(FULL_VERSION_NOTE)]))
    return doc


def _overview(kit: Kit) -> Section:
    issues = _open(kit)
    errors = sum(1 for i in issues if i.severity == Severity.ERROR)
    remarks = sum(1 for i in issues if i.severity == Severity.WARNING)
    mode = {
        "live": "настоящая модель GigaChat",
        "mock": "заготовленные ответы (демонстрационный режим)",
        "replay": "ответы из кеша",
    }
    generation = (
        "экономный (один вызов модели на модуль)"
        if kit.meta.generation_mode == GenerationMode.ECONOMY
        else "полный"
    )
    lines = [
        f"ошибки (нужно исправить до утверждения): {errors}",
        f"предупреждения методисту (с вариантами решения): {len(_warnings(kit))}",
        f"замечания автопроверки (стоит проверить): {remarks}",
        f"объём: заявлено {fmt_hours(kit.program.total_hours)} ч, собрано {fmt_hours(kit.total_hours())} ч",
        f"модель: {mode.get(kit.meta.llm_mode, kit.meta.llm_mode)}; режим генерации — {generation}",
    ]
    seconds = kit.report.generation_seconds if kit.report is not None else None
    if seconds:
        lines.append(f"время сборки комплекта после утверждения каркаса: {_duration(seconds)}")
    if kit.history:
        lines.append(f"точечных правок: {len(kit.history)}; текущая версия комплекта — {kit.revision}")
    return Section("Коротко", [Bullets(lines)])


def _history(kit: Kit, for_docx: bool) -> Section | None:
    """История точечных правок: что просил методист и что изменилось (без служебных ID)."""
    if not kit.history:
        return None
    header = ["Версия", "Когда", "Указание методиста", "Что сделано"]
    weights = [1, 2, 4, 7]
    if not for_docx:
        header.append("₽")
        weights.append(1)
    rows = []
    for record in kit.history:
        row = [
            str(record.revision),
            record.at.strftime("%d.%m %H:%M"),
            record.instruction.strip() or "пересобрать заново",
            record.summary or record.operation.value,
        ]
        if not for_docx:
            row.append(_rub(record.cost_rub))
        rows.append(row)
    text = (
        "Каждая правка меняет только затронутые материалы: остальное в комплекте остаётся как было. После правки "
        "комплект заново проходит автопроверку, файлы выгружаются заново."
    )
    return Section("История правок", [Para(text), Table(header, rows, weights)])


def _issue_line(issue: Issue, for_docx: bool = False) -> str:
    message = issue.message.strip()
    if for_docx and issue.check == COPY_CHECK:
        message = SAMPLE_QUOTE_RE.sub("", message)  # цитаты образца — только в report.md
    return f"{message.rstrip('.')}. Как исправить: {FIX_HINTS[issue.fix_by]}."


def _capped(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_LISTED:
        return lines
    return lines[:MAX_LISTED] + [
        f"…и ещё {len(lines) - MAX_LISTED} замечаний того же рода (полный список — report.json)."
    ]


def _errors(kit: Kit) -> Section | None:
    errors = [i for i in _open(kit) if i.severity == Severity.ERROR]
    if not errors:
        return None
    return Section("Что нужно исправить", [Bullets(_capped([_issue_line(i) for i in errors]))])


def _advice(warning: MethodistWarning, for_docx: bool) -> Advice:
    """Карточка предупреждения. В программу (.docx) — program_text, если он задан, и тогда без цифр:
    они повторяют text (с номерами утративших силу актов или числами образца)."""
    safe = for_docx and bool(warning.program_text.strip())
    return Advice(
        kind=warning.type.value,
        title=mw.title_of(warning),
        text=(warning.program_text if safe else warning.text).strip(),
        figures=[] if safe else [(f.label, f.value) for f in warning.figures],
        options=mw.options_of(warning),
    )


def _methodist(kit: Kit, for_docx: bool) -> Section:
    warnings = _warnings(kit)
    if kit.report is None:
        return Section("Предупреждения методисту", [Para("Автопроверка не выполнялась.")])
    if not warnings:
        return Section("Предупреждения методисту", [Para(NO_WARNINGS)])
    return Section(
        "Предупреждения методисту", [Para(WARNINGS_INTRO), *(_advice(w, for_docx) for w in warnings)]
    )


def _remarks(kit: Kit, for_docx: bool) -> Section | None:
    warnings = [i for i in _open(kit) if i.severity == Severity.WARNING]
    if not warnings:
        return None
    return Section("Замечания автопроверки", [Bullets(_capped([_issue_line(i, for_docx) for i in warnings]))])


def _sample(kit: Kit) -> Section | None:
    sample = _sample_issues(kit)
    if not sample:
        return None
    return Section(
        "Ошибки приложенного образца",
        [
            Para(
                f"В образце, приложенном к запросу, найдено {len(sample)} расхождений. В новой программе они "
                "исправлены — делать ничего не нужно; список пригодится автору образца."
            ),
            Bullets(_capped([i.message.strip() for i in sample])),
        ],
    )


def _hours(kit: Kit) -> Section:
    summary = (
        kit.report.hours if kit.report is not None and kit.report.hours is not None else hours_summary(kit)
    )
    dop = kit.program.type == ProgramType.DOP
    header = (
        ["Уровень", "Всего", "Теория", "Практика", "Самост."]
        if dop
        else ["Уровень", "Всего", "ЛК", "ПР", "ЛБ", "СРС"]
    )

    def cells(h: HoursRow) -> list[str]:
        values = [h.lk, h.pr + h.lb, h.srs] if dop else [h.lk, h.pr, h.lb, h.srs]
        return [fmt_hours(v) for v in values]

    rows: list[list[str]] = []
    blank = [""] * (len(header) - 2)
    for line in summary.lines:
        if line.level == "module":
            rows.append([f"Модуль {line.number}. {line.title}", fmt_hours(line.total), *cells(line.hours)])
        elif line.level == "topic":
            rows.append([f"   тема {line.number}. {line.title}", fmt_hours(line.total), *cells(line.hours)])
        elif line.level == "final_attestation":
            rows.append(["Итоговая аттестация", fmt_hours(line.total), *blank])
        else:
            rows.append(["Итого", fmt_hours(line.total), *cells(line.hours)])
    problems = [i for i in _open(kit) if i.check in HOURS_CHECKS and i.severity != Severity.INFO]
    blocks: list[Block] = [
        Para(
            f"Заявлено {fmt_hours(summary.declared)} ч, по учебному плану {fmt_hours(summary.total)} ч — "
            + ("сходится." if summary.matches else "не сходится, см. «Что нужно исправить».")
        ),
        Para(_hours_breakdown(summary, dop)),
        Table(header, rows, [5, *[1] * (len(header) - 1)]),
        Para(
            "Суммы строк, разделов и итогов, шаг 0,5 ч и календарь проверены: "
            + (
                "замечаний нет."
                if not problems
                else f"{_n(len(problems), 'замечание', 'замечания', 'замечаний')} — см. выше."
            )
        ),
    ]
    return Section("Часы по уровням", blocks)


def _hours_breakdown(summary: HoursSummary, dop: bool) -> str:
    tail = (
        f"самостоятельная работа — {fmt_hours(summary.srs)} ч, итоговая аттестация — "
        f"{fmt_hours(summary.final_attestation)} ч."
    )
    if not summary.contact:
        return f"Аудиторных занятий нет: {tail}"
    word = "аудиторные занятия" if dop else "контактная работа"
    distance = (
        f" (из них с дистанционными технологиями — {fmt_hours(summary.distance)} ч)"
        if summary.distance
        else ""
    )
    return f"Из них {word} — {fmt_hours(summary.contact)} ч{distance}, {tail}"


def _coverage_status(item: LOCoverage) -> str:
    if item.status:
        return item.status
    if not item.topic_ids:
        return "no_topic"
    if not item.assessment_ids:
        return "unassessed"
    return "single" if len(item.assessment_ids) < 2 else "ok"


def _coverage(kit: Kit) -> Section:
    numbers = display_numbers(kit)
    coverage = kit.report.coverage if kit.report is not None and kit.report.coverage else coverage_matrix(kit)
    outcomes = {o.id: o for o in kit.outcomes}
    rows: list[list[str]] = []
    tones: list[str] = []
    complete = 0
    for item in coverage:
        outcome = outcomes.get(item.lo_id)
        text = item.text or (outcome.text if outcome is not None else "")
        status = _coverage_status(item)
        complete += status == "ok"
        kinds = Counter(item.by_kind)
        listed = (
            ", ".join(
                ASSESSMENT_NAMES.get(name, name) + ("" if name == "final_attestation" else f": {count}")
                for name, count in kinds.items()
            )
            or "—"
        )
        topics = ", ".join(numbers.get(t, t) for t in item.topic_ids) or "—"
        number = numbers.get(item.lo_id, item.lo_id.removeprefix("LO"))
        rows.append([f"{number}. {_short(text, 90)}", topics, listed, COVERAGE_TEXT[status]])
        tones.append(OK if status == "ok" else WARN if status == "single" else ERROR)
    return Section(
        "Покрытие результатов обучения",
        [
            Para(
                f"{complete} из {len(coverage)} результатов обучения раскрыты в темах и проверяются двумя и более "
                "оценочными средствами (вопросы теста, практические задания, вопросы собеседования, итоговая "
                "аттестация)."
            ),
            Table(["Результат обучения", "Темы", "Оценочные средства", "Итог"], rows, [6, 2, 3, 2], tones),
        ],
    )


def _sources(kit: Kit) -> Section:
    summary = (
        kit.report.sources
        if kit.report is not None and kit.report.sources is not None
        else source_summary(kit)
    )
    by_kind = ", ".join(
        f"{name} — {summary.by_verification.get(v.value, 0)}"
        for v, name in VERIFICATION_NAMES.items()
        if summary.by_verification.get(v.value, 0)
    )
    lines = [
        f"подтверждено: {summary.confirmed}" + (f" ({by_kind})" if by_kind else ""),
        f"не подтверждено — в документе с пометкой «{mw.UNVERIFIED_MARK}»: {summary.unverified}",
        f"отброшено при проверке: {summary.rejected + summary.dropped} (не прошли проверку ISBN, издательства или "
        f"года — {summary.rejected}; сверх лимита неподтверждённых — {summary.dropped})",
    ]
    blocks: list[Block] = [Bullets(lines)]
    if summary.by_section:
        rows = [
            [name, str(counts.get("confirmed", 0)), str(counts.get("unverified", 0))]
            for name, counts in summary.by_section.items()
        ]
        tones = [WARN if counts.get("unverified", 0) else OK for counts in summary.by_section.values()]
        blocks.append(Table(["Раздел", "Подтверждено", "Без подтверждения"], rows, [5, 2, 2], tones))
    unverified = [s for s in kit.sources if s.verification == Verification.UNVERIFIED]
    if unverified:
        blocks.append(Para("Что сверить вручную (или заменить изданием из каталога):"))
        blocks.append(Bullets([s.citation for s in unverified]))
    return Section("Источники", blocks)


def _template(kit: Kit) -> Section:
    sections = kit.report.sections if kit.report is not None and kit.report.sections else section_status(kit)
    missing = [name for name, filled in sections.items() if not filled]
    text = f"Заполнено {len(sections) - len(missing)} из {len(sections)} разделов шаблона."
    text += (
        " Незаполненные помечены в документе «[заполнить методисту]»."
        if missing
        else " Все разделы шаблона заполнены."
    )
    rows = [[name, "заполнен" if filled else "не заполнен"] for name, filled in sections.items()]
    tones = [OK if filled else ERROR for filled in sections.values()]
    return Section(
        "Полнота разделов шаблона", [Para(text), Table(["Раздел шаблона", "Итог"], rows, [7, 2], tones)]
    )


def _copy_result(kit: Kit, references: Sequence[str]) -> CopySummary:
    stored = kit.report.copying if kit.report is not None else None
    if stored is not None and stored.references:
        return stored
    if any(r and r.strip() for r in references):
        return copy_summary(kit, references)
    return stored or CopySummary(threshold=ValidationContext().copy_threshold)


def _copying(kit: Kit, references: Sequence[str], for_docx: bool = False) -> Section:
    result = _copy_result(kit, references)
    flagged = [i for i in _open(kit) if i.check == COPY_CHECK]
    if not result.references:
        blocks: list[Block] = [Para("Образец к запросу не приложен — сравнивать не с чем.")]
        if flagged:
            blocks.append(Bullets([_issue_line(i, for_docx) for i in flagged]))
        return Section("Совпадение с образцом", blocks)
    verdict = (
        "в норме"
        if result.ratio <= result.threshold + 1e-9
        else "выше порога — перепишите совпадающие фрагменты своими словами"
    )
    text = (
        f"С приложенным образцом дословно совпадает {result.ratio:.0%} текста комплекта (порог — "
        f"{result.threshold:.0%}): {verdict}."
    )
    if result.fields_flagged:
        text += (
            f" Почти дословно повторяют образец {_n(result.fields_flagged, 'фрагмент', 'фрагмента', 'фрагментов')}"
            " — см. «Замечания автопроверки»."
        )
    text += " Ошибки образца в программу не переносятся."
    return Section("Совпадение с образцом", [Para(text)])


def _judge(kit: Kit, for_docx: bool = False) -> Section:
    results = kit.report.judge if kit.report is not None and kit.report.judge else kit.meta.judge
    if not results:
        return Section(
            "Смысловая проверка",
            [
                Para(
                    "Смысловая проверка не выполнялась (демонстрационный режим без модели или не хватило бюджета)."
                )
            ],
        )
    passed = sum(1 for r in results if r.passed)
    rows = [
        [r.title or JUDGE_TEXTS.get(r.criterion, r.criterion), "пройдено" if r.passed else "есть замечания"]
        + ([] if for_docx else [r.reason.strip() or "—"])  # свободный текст модели — только в report.md
        for r in results
    ]
    tones = [OK if r.passed else WARN for r in results]
    table = (
        Table(["Критерий", "Итог"], rows, [7, 2], tones)
        if for_docx
        else Table(["Критерий", "Комментарий", "Итог"], [[r[0], r[2], r[1]] for r in rows], [5, 6, 2], tones)
    )
    return Section(
        "Смысловая проверка",
        [
            Para(
                f"Эксперт-модель проверила то, что нельзя посчитать кодом: пройдено {passed} из {len(results)} критериев. "
                "Замечания — повод перечитать соответствующий раздел."
            ),
            table,
        ],
    )


def _skipped_section(kit: Kit, notes: Sequence[str], for_docx: bool = False) -> Section:
    skipped = _skipped(kit, notes)
    if for_docx:  # названия и ISBN отброшенных моделью изданий в программу не идут — только их число
        rejected = [n for n in skipped if n.startswith(REJECTED_NOTE_PREFIX)]
        skipped = [n for n in skipped if not n.startswith(REJECTED_NOTE_PREFIX)]
        if rejected:
            skipped.append(
                f"Источников, предложенных моделью и не прошедших проверку: {len(rejected)} — они не включены."
            )
    if not skipped:
        return Section("Что пропущено и почему", [Para("Ничего не пропущено: все шаги выполнены.")])
    return Section("Что пропущено и почему", [Bullets(skipped)])


def _cost(kit: Kit) -> Section:
    report = kit.report
    costs = report.cost if report is not None and report.cost else kit.meta.cost
    timing = report.timing if report is not None and report.timing else kit.meta.timing
    rows = [
        [
            STEP_TITLES.get(c.step.split(":")[0], c.step),
            str(c.calls),
            f"{c.prompt_tokens} / {c.completion_tokens}",
            _rub(c.cost_rub),
            f"{c.seconds:.0f}",
        ]
        for c in costs
    ]
    total_rub = sum(c.cost_rub for c in costs)
    total_calls = sum(c.calls for c in costs)
    rows.append(["Итого", str(total_calls), "", _rub(total_rub), f"{sum(c.seconds for c in costs):.0f}"])
    text = f"Вызовов модели: {total_calls}, стоимость {_rub(total_rub)} ₽."
    seconds = report.generation_seconds if report is not None else None
    if seconds:
        text += f" Комплект собран за {_duration(seconds)} после утверждения каркаса."
    longest = max(timing.items(), key=lambda kv: kv[1]) if timing else None
    if longest is not None and sum(timing.values()) >= 1:
        text += (
            f" Шаги генерации заняли {sum(timing.values()):.0f} с в сумме (часть шагов идёт параллельно); "
            f"самый долгий — «{STEP_TITLES.get(longest[0].split(':')[0], longest[0])}», {longest[1]:.0f} с."
        )
    if kit.meta.llm_mode == "mock":
        text += " Режим без модели: вызовы бесплатные."
    blocks: list[Block] = [Para(text)]
    if costs:
        blocks.append(Table(["Шаг", "Вызовов", "Токены (вход / выход)", "₽", "с"], rows, [5, 2, 3, 2, 2]))
    return Section("Стоимость и время", blocks)


def _exports(kit: Kit, files: Sequence[str]) -> Section:
    issues = [i for i in _open(kit) if i.check == "V31"]
    listed = ", ".join(files)
    if not issues:
        return Section(
            "Проверка выгрузок",
            [Para(f"Файлы {listed} перечитаны после записи и сверены с комплектом: расхождений нет.")],
        )
    return Section(
        "Проверка выгрузок",
        [
            Para(f"Файлы: {listed}. При сверке с комплектом найдено:"),
            Bullets([_issue_line(i) for i in issues]),
        ],
    )


def _rub(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def _duration(seconds: float) -> str:
    total = int(round(seconds))
    minutes, rest = divmod(total, 60)
    if not minutes:
        return f"{rest} с"
    return f"{minutes} мин {rest} с" if rest else f"{minutes} мин"


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------------------------------


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip() or " "


def _advice_md(n: int, block: Advice) -> list[str]:
    lines = [f"### {n}. {block.title}", "", block.text, ""]
    if block.figures:
        lines += ["| Цифры | |", "|---|---|"]
        lines += [f"| {_cell(label)} | {_cell(value)} |" for label, value in block.figures]
        lines.append("")
    lines += ["**Варианты решения:**", ""]
    lines += [f"{k}. {option}" for k, option in enumerate(block.options, start=1)]
    return [*lines, ""]


def render_report_md(doc: ReportDoc) -> str:
    lines = [f"# {doc.title}", "", f"**Итог.** {doc.summary}", ""]
    for section in doc.sections:
        lines += [f"## {section.title}", ""]
        advice_no = 0
        for block in section.blocks:
            if isinstance(block, Para):
                lines += [block.text, ""]
            elif isinstance(block, Bullets):
                lines += [f"- {item}" for item in block.items] + [""]
            elif isinstance(block, Advice):
                advice_no += 1
                lines += _advice_md(advice_no, block)
            else:
                lines.append("| " + " | ".join(_cell(h) for h in block.header) + " |")
                lines.append("|" + "|".join("---" for _ in block.header) + "|")
                lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in block.rows]
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report_md(
    kit: Kit,
    out_dir: Path,
    notes: Sequence[str] = (),
    references: Sequence[str] = (),
    files: Sequence[str] = (),
) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / REPORT_MD_NAME).write_text(
        render_report_md(build_report(kit, notes, references, files)), encoding="utf-8"
    )
    return REPORT_MD_NAME
