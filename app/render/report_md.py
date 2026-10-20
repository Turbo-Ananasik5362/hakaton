"""Отчёт автопроверки для методиста: report.md и приложение «Отчёт автопроверки» в программе .docx.

Отчёт строится один раз (build_report) как набор разделов из абзацев, списков и таблиц; render_report_md
и app/render/report_docx.py только раскладывают его. Пишется человеческим языком: итог одной фразой, что
исправить и как, предупреждения с цифрами и вариантами решения. Всё — из Kit и его отчёта проверки, без LLM.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.core.bibliography import REJECTED_NOTE_PREFIX
from app.core.hours import fmt_hours, plural
from app.core.ids import display_numbers
from app.core.validate import ValidationContext, copy_stats, coverage_matrix, section_status
from app.models import (
    FixBy,
    GenerationMode,
    Issue,
    Kit,
    ProgramType,
    ReportStatus,
    Severity,
    Verification,
    WarningType,
)
from app.pipeline.content import JUDGE_TEXTS

REPORT_MD_NAME = "report.md"
MAX_LISTED = 25  # больше замечаний одного вида — сводной строкой
HOURS_CHECKS = {f"V{n:02d}" for n in range(1, 12)}

FIX_HINTS = {
    FixBy.CODE: "повторите сборку; если замечание останется — это сбой программы, сообщите разработчикам",
    FixBy.LLM: "перегенерируйте этот фрагмент точечной правкой или поправьте текст вручную",
    FixBy.HUMAN: "нужно решение методиста: поправьте текст или параметры запроса и пересоберите комплект",
}
WARNING_HINTS = {
    WarningType.REFERENCE_ERRORS: "в программу эти ошибки не перенесены; при желании сообщите автору образца",
    WarningType.EXPECTATION_GAP: "обсудите с заказчиком: увеличить объём программы или уточнить ожидаемый результат",
    WarningType.SCHEDULE_TIGHT: "варианты: увеличить срок, увеличить нагрузку в день или сократить объём",
    WarningType.NORM_REPLACED: "в программе указан действующий акт — ничего делать не нужно, только учесть в ТЗ",
    WarningType.UNVERIFIED_SOURCES: "сверьте издания с каталогом библиотеки или замените их на проверенные",
    WarningType.CONSTRAINT_CONFLICT: "решите, какое требование важнее, и поправьте запрос или каркас",
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
}


# --------------------------------------------------------------------------------------------------
# Структура отчёта
# --------------------------------------------------------------------------------------------------


@dataclass
class Para:
    text: str


@dataclass
class Bullets:
    items: list[str]


@dataclass
class Table:
    header: list[str]
    rows: list[list[str]]
    weights: list[int]  # доли ширины колонок для .docx


Block = Para | Bullets | Table


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


SAMPLE_CHECK = "V45"  # ошибки приложенного образца: в программу не переносятся, к комплекту не относятся
QUOTES_SAMPLE = {"V42", SAMPLE_CHECK}  # замечания с цитатами образца — только в report.md
FULL_VERSION_NOTE = (
    "Полная версия отчёта — файл report.md: в ней дословные формулировки замечаний к образцу, цитаты приложенного "
    "образца, комментарии смысловой проверки и отброшенные источники. В программу они не включаются."
)


def _open(kit: Kit) -> list[Issue]:
    """Открытые замечания к самому комплекту (без ошибок приложенного образца)."""
    if kit.report is None:
        return []
    return [i for i in kit.report.issues if not i.fixed and i.check != SAMPLE_CHECK]


def _sample_issues(kit: Kit) -> list[Issue]:
    return [i for i in kit.report.issues if i.check == SAMPLE_CHECK] if kit.report is not None else []


def _methodist_warnings(kit: Kit, for_docx: bool = False) -> list:  # noqa: ANN202
    """Предупреждения методисту, кроме ошибок образца (у них свой раздел). В .docx нет и предупреждения об
    утративших силу актах из запроса: номера отменённых актов в тексте программы не нужны."""
    if kit.report is None:
        return []
    skip = {WarningType.REFERENCE_ERRORS} | ({WarningType.NORM_REPLACED} if for_docx else set())
    return [w for w in kit.report.warnings_for_methodist if w.type not in skip]


def _counts(kit: Kit) -> tuple[int, int]:
    issues = _open(kit)
    errors = sum(1 for i in issues if i.severity == Severity.ERROR)
    warnings = sum(1 for i in issues if i.severity == Severity.WARNING) + len(_methodist_warnings(kit))
    return errors, warnings


def _n(n: int, one: str, few: str, many: str) -> str:
    return f"{n} {plural(n, one, few, many)}"


def summary_line(kit: Kit, notes: Sequence[str] = (), see: str = "см. ниже") -> str:
    """Итог одной фразой: «Комплект собран, ошибок нет, 3 предупреждения — см. ниже»."""
    if kit.report is None:
        return "Комплект собран, но автопроверка не выполнялась — проверьте материалы вручную."
    errors, warnings = _counts(kit)
    if errors:
        text = (
            f"Комплект собран, но автопроверка нашла {_n(errors, 'ошибку', 'ошибки', 'ошибок')} — их нужно исправить "
            "до утверждения программы"
        )
        if warnings:
            text += f"; ещё {_n(warnings, 'предупреждение', 'предупреждения', 'предупреждений')}"
        text += f" — {see}."
    elif warnings:
        text = f"Комплект собран, ошибок нет, {_n(warnings, 'предупреждение', 'предупреждения', 'предупреждений')} — {see}."
    else:
        text = "Комплект собран, ошибок и предупреждений нет."
    sample = len(_sample_issues(kit))
    if sample:
        text += f" В приложенном образце найдено ошибок: {sample}; в программу они не перенесены."
    if _skipped(kit, notes):
        text += " Пояснения к сборке — в разделе «Что пропущено и почему»."
    return text


def api_message(kit: Kit, notes: Sequence[str] = (), has_report_md: bool = True) -> str:
    """Для поля message ответа done: итог автопроверки, главное замечание и где подробности."""
    text = summary_line(kit, notes, see="см. отчёт")
    errors = [i for i in _open(kit) if i.severity == Severity.ERROR]
    if errors:
        first = errors[0].message.strip()
        text += f" Главное: {first[:160]}{'…' if len(first) > 160 else ''}"
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
    """notes — заметки сессии (пропущенные шаги); references — тексты приложенных образцов (совпадение);
    files — имена выданных файлов (раздел о проверке выгрузок).

    for_docx — вариант для приложения к программе: в документ не идут тексты, которые правила не пускают в
    программу — цитаты и числа часов приложенного образца, свободный текст модели-судьи, названия и ISBN отброшенных
    моделью изданий. Полная версия — report.md."""
    report = kit.report
    doc = ReportDoc(
        title=f"Отчёт автопроверки: «{kit.program.title}»",
        status=report.status if report is not None else None,
        summary=summary_line(kit, notes),
    )
    doc.sections.append(_overview(kit))
    doc.sections += _problems(kit, for_docx)
    doc.sections.append(_hours(kit))
    doc.sections.append(_coverage(kit))
    doc.sections.append(_sources(kit))
    doc.sections.append(_template(kit))
    doc.sections.append(_copying(kit, references, for_docx))
    doc.sections.append(_judge(kit, for_docx))
    doc.sections.append(_skipped_section(kit, notes, for_docx))
    doc.sections.append(_cost(kit))
    if files:
        doc.sections.append(_exports(kit, files))
    if for_docx:
        doc.sections.append(Section("Полная версия", [Para(FULL_VERSION_NOTE)]))
    return doc


def _overview(kit: Kit) -> Section:
    errors, warnings = _counts(kit)
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
    return Section(
        "Коротко",
        [
            Bullets(
                [
                    f"ошибки (нужно исправить до утверждения): {errors}",
                    f"предупреждения (стоит проверить): {warnings}",
                    f"объём: заявлено {fmt_hours(kit.program.total_hours)} ч, собрано {fmt_hours(kit.total_hours())} ч",
                    f"модель: {mode.get(kit.meta.llm_mode, kit.meta.llm_mode)}; режим генерации — {generation}",
                ]
            ),
        ],
    )


def _issue_line(issue: Issue) -> str:
    message = issue.message.strip().rstrip(".")
    return f"{message}. Как исправить: {FIX_HINTS[issue.fix_by]}."


def _capped(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_LISTED:
        return lines
    return lines[:MAX_LISTED] + [
        f"…и ещё {len(lines) - MAX_LISTED} замечаний того же рода (полный список — report.json)."
    ]


def _problems(kit: Kit, for_docx: bool = False) -> list[Section]:
    if kit.report is None:
        return []
    issues = [i for i in _open(kit) if not (for_docx and i.check in QUOTES_SAMPLE)]
    errors = [i for i in issues if i.severity == Severity.ERROR]
    warnings = [i for i in issues if i.severity == Severity.WARNING]
    sections: list[Section] = []
    if errors:
        sections.append(Section("Что нужно исправить", [Bullets(_capped([_issue_line(i) for i in errors]))]))
    lines = [
        f"{w.text.strip().rstrip('.')}. Что можно сделать: {WARNING_HINTS.get(w.type, 'решение — за методистом')}."
        for w in _methodist_warnings(kit, for_docx)
    ]
    lines += [_issue_line(i) for i in warnings]
    if lines:
        sections.append(Section("Предупреждения", [Bullets(_capped(lines))]))
    sample = _sample_issues(kit)
    if sample and not for_docx:
        sections.append(
            Section(
                "Ошибки приложенного образца",
                [
                    Para(
                        f"В образце, приложенном к запросу, найдено {len(sample)} расхождений. В новой программе они "
                        "исправлены — делать ничего не нужно; список пригодится автору образца."
                    ),
                    Bullets(_capped([i.message.strip() for i in sample])),
                ],
            )
        )
    return sections


def _hours(kit: Kit) -> Section:
    numbers = display_numbers(kit)
    dop = kit.program.type == ProgramType.DOP
    header = (
        ["Уровень", "Всего", "Теория", "Практика", "Самост."]
        if dop
        else ["Уровень", "Всего", "ЛК", "ПР", "ЛБ", "СРС"]
    )

    def cells(h) -> list[str]:  # noqa: ANN001
        values = [h.total, h.lk, h.pr + h.lb, h.srs] if dop else [h.total, h.lk, h.pr, h.lb, h.srs]
        return [fmt_hours(v) for v in values]

    rows: list[list[str]] = []
    for module in kit.modules_sorted():
        rows.append(
            [f"Модуль {numbers.get(module.id, '')}. {module.title}", *cells(kit.module_hours(module.id))]
        )
        for topic in kit.topics_of(module.id):
            rows.append([f"   тема {numbers.get(topic.id, '')}. {topic.title}", *cells(topic.hours)])
    blank = [""] * (len(header) - 2)
    rows.append(["Итоговая аттестация", fmt_hours(kit.fa_hours()), *blank])
    total = kit.program_hours()
    rows.append(["Итого", fmt_hours(kit.total_hours()), *cells(total)[1:]])
    declared, actual = kit.program.total_hours, kit.total_hours()
    agree = abs(declared - actual) < 1e-6
    problems = [i for i in _open(kit) if i.check in HOURS_CHECKS]
    blocks: list[Block] = [
        Para(
            f"Заявлено {fmt_hours(declared)} ч, по учебному плану {fmt_hours(actual)} ч — "
            + ("сходится." if agree else "не сходится, см. «Что нужно исправить».")
        ),
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


def _coverage(kit: Kit) -> Section:
    numbers = display_numbers(kit)
    coverage = kit.report.coverage if kit.report is not None and kit.report.coverage else coverage_matrix(kit)
    outcomes = {o.id: o for o in kit.outcomes}
    rows: list[list[str]] = []
    complete = 0
    for item in coverage:
        outcome = outcomes.get(item.lo_id)
        text = outcome.text if outcome is not None else ""
        kinds = Counter(_assessment_kind(a) for a in item.assessment_ids)
        listed = (
            ", ".join(f"{name}: {count}" if name != "ИА" else name for name, count in kinds.items()) or "—"
        )
        if not item.topic_ids:
            status = "нет темы"
        elif not item.assessment_ids:
            status = "не проверяется"
        elif len(item.assessment_ids) < 2:
            status = "одно оценочное средство"
        else:
            status = "в порядке"
            complete += 1
        topics = ", ".join(numbers.get(t, t) for t in item.topic_ids) or "—"
        rows.append([f"{item.lo_id.removeprefix('LO')}. {_short(text, 90)}", topics, listed, status])
    total = len(coverage)
    return Section(
        "Покрытие результатов обучения",
        [
            Para(
                f"{complete} из {total} результатов обучения раскрыты в темах и проверяются двумя и более оценочными "
                "средствами (вопросы теста, практические задания, вопросы собеседования, итоговая аттестация)."
            ),
            Table(["Результат обучения", "Темы", "Оценочные средства", "Итог"], rows, [6, 2, 3, 2]),
        ],
    )


def _assessment_kind(assessment_id: str) -> str:
    if assessment_id.startswith("Q"):
        return "вопросы теста"
    if assessment_id.startswith("S"):
        return "задания"
    if assessment_id.startswith("IQ"):
        return "собеседование"
    return "ИА"


def _sources(kit: Kit) -> Section:
    counts = Counter(s.verification for s in kit.sources)
    confirmed = counts[Verification.REGISTRY] + counts[Verification.CATALOG] + counts[Verification.ONLINE]
    unverified = [s for s in kit.sources if s.verification == Verification.UNVERIFIED]
    checks = kit.meta.literature_checks
    rejected, dropped = checks.get("rejected", 0), checks.get("dropped", 0)
    lines = [
        f"подтверждено (реестр нормативки и каталог проверенных изданий): {confirmed}",
        f"не подтверждено — в документе с пометкой «[не подтверждено]»: {len(unverified)}",
        f"отброшено при проверке: {rejected + dropped} (не прошли проверку ISBN, издательства или года — {rejected}; "
        f"сверх лимита неподтверждённых — {dropped})",
    ]
    blocks: list[Block] = [Bullets(lines)]
    if unverified:
        blocks.append(Para("Что сверить вручную (или заменить изданием из каталога):"))
        blocks.append(Bullets([s.citation for s in unverified]))
    return Section("Источники", blocks)


def _template(kit: Kit) -> Section:
    sections = kit.report.sections if kit.report is not None and kit.report.sections else section_status(kit)
    missing = [name for name, filled in sections.items() if not filled]
    blocks: list[Block] = [
        Para(f"Заполнено {len(sections) - len(missing)} из {len(sections)} разделов шаблона.")
    ]
    if missing:
        blocks.append(Para("Не заполнены (в документе стоит «[заполнить методисту]»):"))
        blocks.append(Bullets(missing))
    else:
        blocks.append(Para("Все разделы шаблона заполнены."))
    return Section("Полнота разделов шаблона", blocks)


def _copying(kit: Kit, references: Sequence[str], for_docx: bool = False) -> Section:
    references = [r for r in references if r and r.strip()]
    flagged = [] if for_docx else [i for i in _open(kit) if i.check == "V42"]
    if not references:
        text = "Образец к запросу не приложен — сравнивать не с чем."
        if flagged:
            return Section("Совпадение с образцом", [Para(text), Bullets([_issue_line(i) for i in flagged])])
        return Section("Совпадение с образцом", [Para(text)])
    stats = copy_stats(kit, references)
    threshold = ValidationContext().copy_threshold
    verdict = (
        "в норме"
        if stats.ratio <= threshold
        else "выше порога — перепишите совпадающие фрагменты своими словами"
    )
    blocks: list[Block] = [
        Para(
            f"С приложенным образцом дословно совпадает {stats.ratio:.0%} текста комплекта (порог — {threshold:.0%}): "
            f"{verdict}. Ошибки образца в программу не переносятся."
        )
    ]
    if flagged:
        blocks.append(Bullets([_issue_line(i) for i in flagged]))
    return Section("Совпадение с образцом", blocks)


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
        [JUDGE_TEXTS.get(r.criterion, r.criterion), "пройдено" if r.passed else "есть замечания"]
        + ([] if for_docx else [r.reason.strip() or "—"])  # свободный текст модели — только в report.md
        for r in results
    ]
    return Section(
        "Смысловая проверка",
        [
            Para(
                f"Эксперт-модель проверила то, что нельзя посчитать кодом: пройдено {passed} из {len(results)} критериев. "
                "Замечания — повод перечитать соответствующий раздел."
            ),
            Table(["Критерий", "Итог"], rows, [7, 2])
            if for_docx
            else Table(["Критерий", "Итог", "Комментарий"], rows, [5, 2, 6]),
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
    costs = kit.report.cost if kit.report is not None and kit.report.cost else kit.meta.cost
    timing = kit.report.timing if kit.report is not None and kit.report.timing else kit.meta.timing
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
    longest = max(timing.items(), key=lambda kv: kv[1]) if timing else None
    text = f"Вызовов модели: {total_calls}, стоимость {_rub(total_rub)} ₽."
    if timing and sum(timing.values()) >= 1:
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


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------------------------------


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip() or " "


def render_report_md(doc: ReportDoc) -> str:
    lines = [f"# {doc.title}", "", f"**Итог.** {doc.summary}", ""]
    for section in doc.sections:
        lines += [f"## {section.title}", ""]
        for block in section.blocks:
            if isinstance(block, Para):
                lines += [block.text, ""]
            elif isinstance(block, Bullets):
                lines += [f"- {item}" for item in block.items] + [""]
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
