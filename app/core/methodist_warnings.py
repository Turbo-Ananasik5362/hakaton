"""Предупреждения методисту по общим правилам (docs/ARCHITECTURE.md §1.3, проверка V46).

Каждое предупреждение — без жаргона и с конкретикой: короткий заголовок, текст с цифрами, главные цифры
отдельно (figures) и варианты решения (options). Правила общие: срабатывают по RequestSpec, комплекту и реестру,
а не по номеру запроса.

| Вид | Когда | Кто строит |
|---|---|---|
| reference_errors | к запросу приложен образец, и проверка его сырых чисел (V45) нашла расхождения | validate |
| expectation_gap | в запросе ожидание вроде «пойти работать в сферу», которого программа такого объёма не даёт | validate |
| schedule_tight | желаемый срок не вмещает объём при лимите часов в день или занятий в неделю | календарь |
| norm_replaced | в программе действующий акт, который заменил акт, указанный в задании или образце | validate |
| unverified_sources | в списке литературы есть источники с пометкой «[не подтверждено]» | validate |
| constraint_conflict | требования запроса несовместимы (находит солвер часов, app/core/hours.py) | солвер |

Если в тексте есть то, чего не должно быть в самой программе (цитаты и числа приложенного образца, номер
утратившего силу акта), заполняется program_text — вариант для приложения к программе .docx.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.core.hours import fmt_hours, plural
from app.models import (
    Kit,
    MethodistWarning,
    OutcomeKind,
    ProgramType,
    SourceSection,
    WarningFigure,
    WarningType,
)

TITLES: dict[WarningType, str] = {
    WarningType.REFERENCE_ERRORS: "Ошибки в приложенном образце",
    WarningType.EXPECTATION_GAP: "Ожидания заказчика выше, чем может дать программа",
    WarningType.SCHEDULE_TIGHT: "Желаемый срок не сходится с нагрузкой",
    WarningType.NORM_REPLACED: "Акт из задания утратил силу — в программе указан действующий",
    WarningType.UNVERIFIED_SOURCES: "Есть источники без подтверждения",
    WarningType.CONSTRAINT_CONFLICT: "Требования запроса противоречат друг другу",
}
# Для предупреждений, записанных прежними версиями без вариантов решения
DEFAULT_OPTIONS: dict[WarningType, list[str]] = {
    WarningType.REFERENCE_ERRORS: [
        "Ничего менять в новой программе не нужно: ошибки образца в неё не перенесены."
    ],
    WarningType.EXPECTATION_GAP: [
        "Обсудите с заказчиком: увеличить объём программы или уточнить ожидаемый результат."
    ],
    WarningType.SCHEDULE_TIGHT: ["Увеличьте срок, увеличьте нагрузку в день или сократите объём программы."],
    WarningType.NORM_REPLACED: ["Ничего менять в программе не нужно — в ней указан действующий акт."],
    WarningType.UNVERIFIED_SOURCES: [
        "Сверьте издания с каталогом библиотеки или замените их изданиями из каталога."
    ],
    WarningType.CONSTRAINT_CONFLICT: ["Решите, какое требование важнее, и поправьте запрос или каркас."],
}
# Сначала то, что требует решения, в конце — то, что только для сведения
ORDER = (
    WarningType.CONSTRAINT_CONFLICT,
    WarningType.SCHEDULE_TIGHT,
    WarningType.EXPECTATION_GAP,
    WarningType.UNVERIFIED_SOURCES,
    WarningType.REFERENCE_ERRORS,
    WarningType.NORM_REPLACED,
)
SECTION_NAMES: dict[SourceSection, str] = {
    SourceSection.NORMATIVE: "Нормативно-правовые документы",
    SourceSection.MAIN: "Основная литература",
    SourceSection.ADDITIONAL: "Дополнительная литература",
    SourceSection.INTERNET: "Интернет-ресурсы",
    SourceSection.SOFTWARE: "Программное обеспечение",
}
UNVERIFIED_MARK = "[не подтверждено]"
FULL_REPORT = "полной версии отчёта (report.md)"
SAMPLE_SECTION = "«Ошибки приложенного образца»"
SHOWN_SAMPLE_FINDINGS = 5  # столько расхождений образца — прямо в тексте предупреждения, остальные — списком
MAX_SESSION_HOURS = 4.0  # длиннее занятия не предлагаются

QUOTED_RE = re.compile(r"«[^»]*»")
# Служебные обозначения, которых методист не должен видеть: ID объектов комплекта, номера проверок, значения
# из кода. Отображаемые номера («тема 2.3») — не жаргон.
JARGON_RE = re.compile(
    r"(?<![\w./-])(?:LO\d+\.\d+|IQ\d+|TS\d+|SRC\d+|[TL]\d+\.\d+|[CMSQD]\d+|FA\.[KP]\d+|V\d{2}(?:-\d+)?)"
    r"(?![\w/-]|\.\d)"
    r"|(?<![\w])(?:None|null|True|False|fix_by|target_ids|kit\.json|warnings_for_methodist)(?![\w])"
)


# --------------------------------------------------------------------------------------------------
# Общие помощники
# --------------------------------------------------------------------------------------------------


def figure(label: str, value: str) -> WarningFigure:
    return WarningFigure(label=label, value=value)


def hours_value(value: float) -> str:
    return f"{fmt_hours(value)} ч"


def weeks_text(n: int) -> str:
    return f"{n} {plural(n, 'неделя', 'недели', 'недель')}"


def hours_words(value: float) -> str:
    """«72 часа», «16 часов», «4,5 ч»."""
    if abs(value - round(value)) > 1e-9:
        return f"{fmt_hours(value)} ч"
    n = int(round(value))
    return f"{n} {plural(n, 'час', 'часа', 'часов')}"


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:]


def _lower_first(text: str) -> str:
    """«В учебном плане…» → «в учебном плане…» внутри перечня; аббревиатуры («ЛБ…», «РП…») не трогаются."""
    word = re.match(r"\w+", text)
    if word is None or (len(word.group(0)) > 1 and word.group(0).isupper()):
        return text
    return text[:1].lower() + text[1:]


def _percent(part: float, whole: float) -> int:
    return round(100 * part / whole) if whole else 0


_WORD = r"[^\s.,;:!?«»()\[\]]"  # символ слова в продолжении фразы: до пробела или знака препинания


def expectation_phrase(text: str, start: int, end: int, max_words: int = 6) -> str:
    """Ожидание из запроса целиком: найденные слова и продолжение до знака препинания (не больше max_words)."""
    rest = text[end:]
    tail = re.match(rf"(?:\s+{_WORD}+){{0,{max_words}}}", rest)
    taken = tail.group(0) if tail else ""
    more = re.match(rf"\s+{_WORD}", rest[len(taken) :])  # фраза длиннее — обрезана
    return (text[start:end] + taken).strip() + ("…" if more else "")


def title_of(warning: MethodistWarning) -> str:
    return warning.title.strip() or TITLES.get(warning.type, "Предупреждение")


def options_of(warning: MethodistWarning) -> list[str]:
    return [o for o in warning.options if o.strip()] or list(DEFAULT_OPTIONS.get(warning.type, []))


def ordered(warnings: Iterable[MethodistWarning]) -> list[MethodistWarning]:
    """Порядок показа: сначала то, что требует решения методиста, затем — для сведения."""
    rank = {t: n for n, t in enumerate(ORDER)}
    return sorted(warnings, key=lambda w: rank.get(w.type, len(ORDER)))


def missing_parts(warning: MethodistWarning) -> list[str]:
    """Чего не хватает предупреждению по правилу «с цифрами и вариантами решения» (V46)."""
    missing = []
    if not warning.title.strip():
        missing.append("заголовка")
    numbers = [f.value for f in warning.figures] + [warning.text]
    if not any(re.search(r"\d", value) for value in numbers):
        missing.append("цифр")
    if not any(o.strip() for o in warning.options):
        missing.append("вариантов решения")
    return missing


def jargon_in(warning: MethodistWarning) -> str | None:
    """Первое служебное обозначение в текстах предупреждения (кроме program_text — он вариант text)."""
    texts = [
        warning.title,
        warning.text,
        *warning.options,
        *(f"{f.label} {f.value}" for f in warning.figures),
    ]
    for text in texts:
        match = JARGON_RE.search(QUOTED_RE.sub("", text))  # цитаты (названия книг, слова запроса) — не жаргон
        if match:
            return match.group(0)
    return None


# --------------------------------------------------------------------------------------------------
# reference_errors: ошибки приложенного образца
# --------------------------------------------------------------------------------------------------

_SUMS = "суммы и итоги часов в учебных планах"
_WORK_PROGRAM = "часы рабочей программы не совпадают с учебно-тематическим планом"
_NORMS = "ссылки на нормативные акты (утратившие силу или с неверной датой)"
REFERENCE_GROUPS: dict[str, str] = {
    "ref_total_vs_sections": _SUMS,
    "ref_total_vs_topics": _SUMS,
    "ref_section_vs_topics": _SUMS,
    "ref_row_sum": _SUMS,
    "ref_step": _SUMS,
    "ref_plan_vs_utp": _SUMS,
    "ref_empty_row": "строки с часами, не разнесёнными по видам работы",
    "ref_label_hours": _WORK_PROGRAM,
    "ref_wp_vs_utp": _WORK_PROGRAM,
    "ref_control_hours": _WORK_PROGRAM,
    "ref_lr_lb": "разные обозначения лабораторных работ",
    "ref_internship_contact": "аудиторные часы в стажировке",
    "ref_norm_outdated": _NORMS,
    "ref_norm_date": _NORMS,
    "ref_requisites": "заполненные реквизиты приказа об утверждении",
}
_OTHER_GROUP = "прочие расхождения"


@dataclass(frozen=True)
class SampleFinding:
    """Ошибка приложенного образца: код вида (REF_* в validate.py) и текст для методиста."""

    code: str
    message: str


def reference_errors(source_name: str, findings: Sequence[SampleFinding]) -> MethodistWarning | None:
    if not findings:
        return None
    n = len(findings)
    word = plural(n, "расхождение", "расхождения", "расхождений")
    groups = Counter(REFERENCE_GROUPS.get(f.code, _OTHER_GROUP) for f in findings).most_common()
    by_group = "; ".join(f"{label} — {count}" for label, count in groups)
    shown = SHOWN_SAMPLE_FINDINGS
    main = "; ".join(_lower_first(f.message.strip().rstrip(".")) for f in findings[:shown])
    more = f"; и ещё {n - shown} — полный список в разделе {SAMPLE_SECTION}" if n > shown else ""
    name = f" «{source_name}»" if source_name else ""
    text = (
        f"В приложенном образце{name} найдено {n} {word}: {by_group}. Главное: {main}{more}. "
        "В новой программе они не повторяются."
    )
    program_text = (
        f"В приложенном образце найдено {n} {word}: {by_group}. В новой программе они не повторяются; "
        f"формулировки с цитатами образца — в {FULL_REPORT}."
    )
    return MethodistWarning(
        type=WarningType.REFERENCE_ERRORS,
        title=TITLES[WarningType.REFERENCE_ERRORS],
        text=text,
        program_text=program_text,
        figures=[
            figure("Всего расхождений", str(n)),
            *(figure(_upper_first(label), str(count)) for label, count in groups),
        ],
        options=[
            "Ничего менять в новой программе не нужно: она собрана заново по шаблону, часы посчитаны программой, "
            "и ошибки образца в неё не перенесены.",
            f"Передайте список расхождений автору образца — он в {FULL_REPORT}, раздел {SAMPLE_SECTION}.",
            "Если образец — действующая программа организации, исправьте его, прежде чем использовать снова.",
        ],
    )


# --------------------------------------------------------------------------------------------------
# expectation_gap: ожидание заказчика против результатов программы
# --------------------------------------------------------------------------------------------------


def _retraining_option(program_type: ProgramType) -> str:
    if program_type == ProgramType.DOP:
        return (
            "Если нужна подготовка к работе, общеразвивающая программа для этого не предназначена: нужна программа "
            "профессионального обучения или дополнительного профессионального образования."
        )
    return (
        "Если заказчику нужна новая профессия — предложить программу профессиональной переподготовки: по закону "
        "об образовании она готовит к новому виду профессиональной деятельности, а повышение квалификации "
        "развивает компетенции в рамках имеющейся квалификации."
    )


def expectation_gap(kit: Kit, phrase: str) -> MethodistWarning:
    total = kit.program.total_hours
    hours = kit.program_hours()
    practice = hours.pr + hours.lb
    skills = [o.text.strip().rstrip(".") for o in kit.outcomes if o.kind == OutcomeKind.CAN]
    practical = sum(1 for o in kit.outcomes if o.kind in (OutcomeKind.CAN, OutcomeKind.MASTER))
    listed = "; ".join(f"«{_short(s, 90)}»" for s in skills[:3]) if skills else "базовые умения по теме"
    text = (
        f"В запросе ожидание «{phrase}». Программа на {hours_words(total)} даёт начальный уровень: {listed}. "
        "Этого хватит, чтобы продолжить обучение и выполнять простые задачи под руководством, но не чтобы сразу "
        "работать специалистом самостоятельно. Проверьте, что цель и результаты программы не обещают "
        "трудоустройства."
    )
    figures = [figure("Ожидание из запроса", f"«{phrase}»"), figure("Объём программы", hours_value(total))]
    if practice > 0:
        figures.append(
            figure(
                "Практические и лабораторные занятия",
                f"{hours_value(practice)} ({_percent(practice, total)} % объёма)",
            )
        )
        grow = (
            f"Увеличить объём программы и долю практики на реальных задачах: сейчас практических и лабораторных "
            f"занятий {hours_value(practice)} из {hours_value(total)}."
        )
    else:
        figures.append(figure("Самостоятельная работа", hours_value(hours.srs)))
        grow = (
            "Увеличить объём программы и добавить работу на реальных задачах под руководством наставника: сейчас "
            f"самостоятельной работы {hours_value(hours.srs)} из {hours_value(total)}."
        )
    figures.append(figure("Результатов обучения «уметь» и «владеть»", str(practical)))
    return MethodistWarning(
        type=WarningType.EXPECTATION_GAP,
        title=TITLES[WarningType.EXPECTATION_GAP],
        text=text,
        figures=figures,
        options=[
            "Сформулировать с заказчиком реалистичный результат: после программы слушатель освоит базовые умения и "
            "сможет продолжить обучение.",
            _retraining_option(kit.program.type),
            grow,
        ],
    )


# --------------------------------------------------------------------------------------------------
# schedule_tight: желаемый срок против нагрузки
# --------------------------------------------------------------------------------------------------


def _ceil_half(value: float) -> float:
    return math.ceil(value * 2 - 1e-9) / 2


def schedule_days(
    *,
    target_weeks: int,
    target_text: str,
    total: float,
    cap: float,
    days: int,
    per_week: int,
    weeks: int,
    need_per_day: float,
) -> MethodistWarning:
    """Учебные дни с лимитом часов в день: дней получается больше, чем помещается в желаемый срок."""
    day_word = plural(days, "учебный день", "учебных дня", "учебных дней")
    text = (
        f"Желаемый срок — {target_text} — не сходится с нагрузкой: при объёме {hours_value(total)} и не больше "
        f"{hours_value(cap)} в день получается {days} {day_word} — это {weeks_text(weeks)} даже при {per_week} "
        f"учебных днях в неделю. Чтобы уложиться в {weeks_text(target_weeks)}, нужно около "
        f"{hours_value(need_per_day)} в день при {per_week} учебных днях в неделю; другой вариант — срок "
        f"{weeks_text(weeks)}. В программу и календарный график записан расчётный срок — {weeks_text(weeks)}."
    )
    fit_total = target_weeks * per_week * cap
    return MethodistWarning(
        type=WarningType.SCHEDULE_TIGHT,
        title=TITLES[WarningType.SCHEDULE_TIGHT],
        text=text,
        figures=[
            figure("Желаемый срок", target_text),
            figure("Объём программы", hours_value(total)),
            figure("Нагрузка", f"не больше {hours_value(cap)} в день, до {per_week} учебных дней в неделю"),
            figure("Нужно учебных дней", str(days)),
            figure("Расчётный срок", weeks_text(weeks)),
        ],
        options=[
            f"Принять расчётный срок — {weeks_text(weeks)}: он уже записан в программу и календарный график.",
            f"Увеличить нагрузку до {hours_value(need_per_day)} в день при {per_week} учебных днях в неделю — тогда "
            f"хватит {weeks_text(target_weeks)}.",
            f"Сократить объём программы до {hours_value(fit_total)} — столько помещается в "
            f"{weeks_text(target_weeks)} при {hours_value(cap)} в день и {per_week} учебных днях в неделю.",
        ],
    )


def schedule_sessions(
    *,
    target_weeks: int,
    target_text: str,
    sessions: int,
    session_hours: float,
    per_week: int,
    weeks: int,
) -> MethodistWarning:
    """Занятия фиксированной длины: при заданном числе занятий в неделю срок длиннее желаемого."""
    need_per_week = math.ceil(sessions / target_weeks)
    sessions_word = plural(sessions, "занятие", "занятия", "занятий")
    per_week_word = plural(per_week, "занятии", "занятиях", "занятиях")
    need_word = plural(need_per_week, "занятие", "занятия", "занятий")
    text = (
        f"Желаемый срок — {target_text} — не сходится с расписанием: {sessions} {sessions_word} по "
        f"{hours_value(session_hours)} при {per_week} {per_week_word} в неделю займут {weeks_text(weeks)}. "
        f"Чтобы уложиться в {weeks_text(target_weeks)}, нужно {need_per_week} {need_word} в неделю. "
        f"В программу и календарный график записан расчётный срок — {weeks_text(weeks)}."
    )
    fit_total = target_weeks * per_week * session_hours
    options = [
        f"Принять расчётный срок — {weeks_text(weeks)}: он уже записан в программу и календарный график.",
        f"Проводить {need_per_week} {need_word} в неделю — тогда хватит {weeks_text(target_weeks)}.",
        f"Сократить объём программы до {hours_value(fit_total)} — столько занятий по {hours_value(session_hours)} "
        f"помещается в {weeks_text(target_weeks)} при {per_week} {per_week_word} в неделю.",
    ]
    longer = _ceil_half(sessions * session_hours / (target_weeks * per_week))
    if session_hours < longer <= MAX_SESSION_HOURS:
        options.append(
            f"Удлинить занятия до {hours_value(longer)} при {per_week} {per_week_word} в неделю — тогда хватит "
            f"{weeks_text(target_weeks)}."
        )
    return MethodistWarning(
        type=WarningType.SCHEDULE_TIGHT,
        title=TITLES[WarningType.SCHEDULE_TIGHT],
        text=text,
        figures=[
            figure("Желаемый срок", target_text),
            figure("Занятий", f"{sessions} по {hours_value(session_hours)}"),
            figure("Занятий в неделю", str(per_week)),
            figure("Расчётный срок", weeks_text(weeks)),
        ],
        options=options,
    )


# --------------------------------------------------------------------------------------------------
# norm_replaced: акт из задания утратил силу
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NormReplacement:
    """Пара «действующий акт — акт, который он заменил» с полными названиями и датами (дд.мм.гггг)."""

    current: str  # «Приказ Минобрнауки России от 24.03.2025 № 266»
    old: str  # «Приказ Минобрнауки России от 01.07.2013 № 499»
    current_since: str = ""
    old_until: str = ""
    lost_word: str = "утратил"  # утратил / утратило / утратила — по роду акта
    source_url: str = ""
    mentioned_in: tuple[str, ...] = ()  # «запросе», «приложенном образце»


def norm_replaced(replacements: Sequence[NormReplacement]) -> MethodistWarning | None:
    if not replacements:
        return None
    texts: list[str] = []
    safe: list[str] = []
    figures: list[WarningFigure] = []
    for r in replacements:
        since = f" (действует с {r.current_since})" if r.current_since else ""
        until = f" с {r.old_until}" if r.old_until else ""
        where = f" Прежний акт упоминается в {' и в '.join(r.mentioned_in)}." if r.mentioned_in else ""
        texts.append(
            f"Действующий акт — {r.current}{since}. {r.old} {r.lost_word} силу{until}: ссылка на него из задания "
            f"или образца в программу не переносится.{where}"
        )
        safe.append(
            f"В программе указан действующий акт — {r.current}{since}. Прежний акт, на который могут ссылаться "
            "задание и старые образцы, утратил силу и в программу не включён."
        )
        figures.append(
            figure("Действующий акт", r.current + (f", действует с {r.current_since}" if since else ""))
        )
        figures.append(figure("Прежний акт", f"{r.old} — {r.lost_word} силу{until}"))
        if r.source_url:
            figures.append(figure("Источник", r.source_url))
    current = "; ".join(r.current for r in replacements)
    return MethodistWarning(
        type=WarningType.NORM_REPLACED,
        title=TITLES[WarningType.NORM_REPLACED],
        text=" ".join(texts),
        program_text=" ".join(safe),
        figures=figures,
        options=[
            "Ничего менять в программе не нужно — в ней указан действующий акт.",
            f"Обновите ссылку в задании и шаблонах организации: {current}.",
            "Проверьте другие программы организации, где ещё указан прежний акт.",
        ],
    )


# --------------------------------------------------------------------------------------------------
# unverified_sources: источники без подтверждения
# --------------------------------------------------------------------------------------------------


def unverified_sources(kit: Kit, rejected: int = 0, dropped: int = 0) -> MethodistWarning | None:
    unverified = [s for s in kit.sources if s.unverified]
    if not unverified:
        return None
    n, total = len(unverified), len(kit.sources)
    titles = ", ".join(f"«{_short(s.title, 50)}»" for s in unverified[:5])
    if n > 5:
        titles += f" и ещё {n - 5}"
    text = (
        f"Не удалось подтвердить по реестру и каталогу {n} из {total} "
        f"{plural(total, 'источника', 'источников', 'источников')}: {titles}. В списке литературы они помечены "
        f"«{UNVERIFIED_MARK}» — проверьте их перед утверждением программы."
    )
    per_section = Counter(s.section for s in unverified)
    totals = Counter(s.section for s in kit.sources)
    figures = [figure("Без подтверждения", f"{n} из {total}")]
    figures += [
        figure(SECTION_NAMES.get(section, section.value), f"{count} из {totals[section]} без подтверждения")
        for section, count in per_section.most_common()
    ]
    if rejected or dropped:
        figures.append(figure("Отброшено при проверке", str(rejected + dropped)))
    worst, worst_count = per_section.most_common(1)[0]
    left = totals[worst] - worst_count
    left_text = (
        f"останется {left} {plural(left, 'подтверждённый источник', 'подтверждённых источника', 'подтверждённых источников')}"
        if left
        else "не останется ни одного источника — раздел придётся заполнить заново"
    )
    return MethodistWarning(
        type=WarningType.UNVERIFIED_SOURCES,
        title=TITLES[WarningType.UNVERIFIED_SOURCES],
        text=text,
        figures=figures,
        options=[
            f"Сверить издания по ISBN с каталогом библиотеки и снять пометку «{UNVERIFIED_MARK}» с найденных.",
            "Заменить их изданиями из каталога проверенных источников.",
            f"Удалить их из списка: в разделе «{SECTION_NAMES.get(worst, worst.value)}» {left_text}.",
        ],
        target_ids=[s.id for s in unverified],
    )
