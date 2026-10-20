"""ProgramTables — снимок всех таблиц программы из Kit (docs/ARCHITECTURE.md §10).

Рендеры .docx ничего не считают: каждое число таблицы, «Итого», объём в 1.8 / 1.4 и метки «ПР (N часов)» берутся
отсюда, а здесь — только из методов Kit (topic.hours, module_hours, program_hours, fa_hours, total_hours).
Метка вида работы строится из того же числа, что и колонка часов, поэтому разойтись они не могут.
V31 (app/render/verify.py) перечитывает готовый .docx и сверяет его с Kit напрямую, а не с этим снимком.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.models import (
    FinalAttestation,
    HoursRow,
    Kit,
    Module,
    ModuleKind,
    OutcomeKind,
    ProgramType,
    Topic,
    WorkKind,
)
from app.pipeline.request_rules import FA_TITLES, FORM_TITLES
from app.render.common import FILL_MARK, hours_phrase, items_or_fill, upper_first

PK_TYPES = (ProgramType.PK, ProgramType.PK_INTERNSHIP)

# Заголовки таблиц — общие для рендера и V31 (по ним V31 находит таблицы в .docx)
PK_PLAN_HEADER = [
    "№ п/п",
    "Наименование разделов программы",
    "Трудоёмкость, всего (час.)",
    "ЛК",
    "ПР",
    "ЛБ",
    "СРС (час.)",
    "Форма аттестации",
]
PK_UTP_HEADER = [
    "№ п/п",
    "Наименование разделов, тем программы",
    "Трудоёмкость, всего (час.)",
    "ЛК",
    "ПР",
    "ЛБ",
    "СРС (час.)",
    "Форма контроля",
]
PK_WP_HEADER = [
    "Наименование компонентов программы",
    "Содержание учебного материала и формы организации деятельности слушателей",
    "Всего (час.)",
]
DOP_PLAN_HEADER_TOP = [
    "№ п/п",
    "Наименование разделов (модулей)",
    "Общая трудоёмкость, час.",
    "Всего аудиторных часов",
    "в т.ч.",
    "в т.ч.",
    "Самостоятельная работа",
    "Форма промежуточной аттестации / контроля",
]
DOP_PLAN_HEADER_SUB = ["Лекции / теория", "Практические занятия"]
DOP_UTP_HEADER = ["Тема", "Виды учебных занятий", "Содержание"]
CALENDAR_HEADER = ["Неделя", "Учебные дни", "Разделы и темы", "Часов"]
DOP_CALENDAR_SUMMARY_HEADER = [
    "Дата начала обучения",
    "Дата окончания обучения",
    "Всего учебных недель",
    "Всего учебных дней (занятий)",
    "Количество учебных часов",
    "Режим занятий",
]

FA_ROW_TITLE = "Итоговая аттестация"
TOTAL_ROW_TITLE = "Итого (час.)"
DOP_TOTAL_ROW_TITLE = "Итого часов"
INTRO_TITLE = "Вводное занятие"

KIND_ABBR = {WorkKind.LK: "ЛК", WorkKind.PR: "ПР", WorkKind.LB: "ЛБ", WorkKind.SRS: "СРС"}
DOP_KIND_TITLES = {
    WorkKind.LK: "Теоретическое занятие",
    WorkKind.PR: "Практическое занятие",
    WorkKind.LB: "Практическое занятие",  # в ДОП лабораторных нет: учитываются как практика
    WorkKind.SRS: "Самостоятельная работа",
}
WORK_ORDER = (WorkKind.LK, WorkKind.PR, WorkKind.LB, WorkKind.SRS)


class Cells(BaseModel):
    """Числа строки таблицы часов."""

    total: float = 0
    lk: float = 0
    pr: float = 0
    lb: float = 0
    srs: float = 0
    contact: float = 0
    distance: float = 0

    @property
    def practice(self) -> float:
        """«Практические занятия» ДОП: ПР и ЛБ (в общеразвивающей программе отдельной колонки ЛБ нет)."""
        return self.pr + self.lb

    @classmethod
    def of(cls, row: HoursRow) -> Cells:
        return cls(
            total=row.total,
            lk=row.lk,
            pr=row.pr,
            lb=row.lb,
            srs=row.srs,
            contact=row.contact,
            distance=row.distance,
        )


class PlanRow(BaseModel):
    kind: Literal["module", "topic", "fa", "total"]
    ref_id: str  # M1 / T1.2 / FA / TOTAL
    number: str = ""
    title: str
    hours: Cells
    form: str = ""


class WorkBlock(BaseModel):
    kind: WorkKind
    hours: float
    label: str  # «ПР (4 часа):» (ПК) или «Практическое занятие – 4 часа» (ДОП) — из того же числа, что hours
    items: list[str] = Field(default_factory=list)


class TopicBlocks(BaseModel):
    topic_id: str
    number: str  # «1.2»
    title: str
    blocks: list[WorkBlock] = Field(default_factory=list)


class SectionBlocks(BaseModel):
    module_id: str
    number: str  # «1» («0» — вводное занятие ДОП)
    title: str
    hours: Cells
    topics: list[TopicBlocks] = Field(default_factory=list)


class CalendarRow(BaseModel):
    week: int
    days: int
    content: str
    hours: float


class Volume(BaseModel):
    total: float
    contact: float
    lk: float
    pr: float
    lb: float
    srs: float
    distance: float
    fa: float
    weeks: int | None = None
    calendar_total: float = 0  # сумма часов по дням календаря Kit — строка «Итого» графика
    days: int | None = None
    hours_per_day: float | None = None
    session_hours: float | None = None
    sessions_per_week: int | None = None


class CompetencyRow(BaseModel):
    competency_id: str
    activity: str
    title: str
    master: list[str]
    can: list[str]
    know: list[str]


class ProgramTables(BaseModel):
    program_type: ProgramType
    plan: list[PlanRow]  # учебный план: разделы, ИА, итого
    utp: list[PlanRow]  # учебно-тематический план: разделы, темы, ИА, итого
    sections: list[SectionBlocks]  # рабочая программа (ПК 2.4) и УТП ДОП — блоки видов работы по темам
    fa_title: str  # форма ИА для строк таблиц
    fa_hours: float
    total: float
    calendar: list[CalendarRow]
    volume: Volume
    competencies: list[CompetencyRow]
    staffing: list[str]  # строки кадрового обеспечения ДОП: модули + ИА, названия как в учебном плане


# --------------------------------------------------------------------------------------------------
# Нумерация и подписи
# --------------------------------------------------------------------------------------------------


def module_numbers(kit: Kit) -> dict[str, str]:
    """Номер раздела: обычные модули — 1, 2, …; вводное занятие ДОП — «0» (темы 0.1, 0.2)."""
    numbers: dict[str, str] = {}
    n = 0
    for module in kit.modules_sorted():
        if module.kind == ModuleKind.INTRO:
            numbers[module.id] = "0"
        else:
            n += 1
            numbers[module.id] = str(n)
    return numbers


def topic_number(kit: Kit, topic: Topic, numbers: dict[str, str]) -> str:
    topics = kit.topics_of(topic.module_id)
    return f"{numbers[topic.module_id]}.{topics.index(topic) + 1}"


def fa_title(fa: FinalAttestation | None) -> str:
    if fa is None:
        return FILL_MARK
    return upper_first(FA_TITLES.get(fa.form, "") or fa.form_text or FILL_MARK)


def form_title(kit: Kit) -> str:
    """«очно-заочная с применением электронного обучения и дистанционных образовательных технологий»."""
    form = kit.program.form
    base = FORM_TITLES[form.kind]
    if form.eo and form.dot:
        return f"{base} с применением электронного обучения и дистанционных образовательных технологий"
    if form.dot:
        return f"{base} с применением дистанционных образовательных технологий"
    if form.eo:
        return f"{base} с применением электронного обучения"
    return base


def _module_title(module: Module) -> str:
    return module.title.strip() or (INTRO_TITLE if module.kind == ModuleKind.INTRO else FILL_MARK)


def pk_label(kind: WorkKind, hours: float) -> str:
    return f"{KIND_ABBR[kind]} ({hours_phrase(hours)}):"


def dop_label(kind: WorkKind, hours: float) -> str:
    return f"{DOP_KIND_TITLES[kind]} – {hours_phrase(hours)}"


# --------------------------------------------------------------------------------------------------
# Сборка
# --------------------------------------------------------------------------------------------------


def build_tables(kit: Kit) -> ProgramTables:
    numbers = module_numbers(kit)
    is_pk = kit.program.type in PK_TYPES
    fa = kit.final_attestation
    fa_name = fa_title(fa)
    fa_cells = Cells(total=kit.fa_hours())
    program = kit.program_hours()
    total_cells = Cells.of(program).model_copy(update={"total": kit.total_hours()})

    plan: list[PlanRow] = []
    utp: list[PlanRow] = []
    sections: list[SectionBlocks] = []
    for module in kit.modules_sorted():
        number = numbers[module.id]
        shown = "" if number == "0" else f"{number}."
        row = PlanRow(
            kind="module",
            ref_id=module.id,
            number=shown,
            title=_module_title(module),
            hours=Cells.of(kit.module_hours(module.id)),
            form=(module.control_form or "").strip() or "—",
        )
        plan.append(row)
        utp.append(row)
        section = SectionBlocks(module_id=module.id, number=number, title=row.title, hours=row.hours)
        for topic in kit.topics_of(module.id):
            tnum = topic_number(kit, topic, numbers)
            utp.append(
                PlanRow(
                    kind="topic",
                    ref_id=topic.id,
                    number=f"{tnum}.",
                    title=topic.title.strip() or FILL_MARK,
                    hours=Cells.of(topic.hours),
                    form=(topic.control_form or "").strip(),
                )
            )
            section.topics.append(_topic_blocks(topic, tnum, is_pk))
        sections.append(section)
    fa_row = PlanRow(kind="fa", ref_id="FA", title=FA_ROW_TITLE, hours=fa_cells, form=fa_name)
    total_row = PlanRow(
        kind="total",
        ref_id="TOTAL",
        title=TOTAL_ROW_TITLE if is_pk else DOP_TOTAL_ROW_TITLE,
        hours=total_cells,
    )
    plan += [fa_row, total_row]
    utp += [fa_row, total_row]

    return ProgramTables(
        program_type=kit.program.type,
        plan=plan,
        utp=utp,
        sections=sections,
        fa_title=fa_name,
        fa_hours=kit.fa_hours(),
        total=kit.total_hours(),
        calendar=_calendar_rows(kit, numbers),
        volume=_volume(kit, program),
        competencies=_competency_rows(kit),
        staffing=[row.title for row in plan if row.kind == "module"] + [FA_ROW_TITLE],
    )


def _topic_blocks(topic: Topic, number: str, is_pk: bool) -> TopicBlocks:
    blocks: list[WorkBlock] = []
    for kind in WORK_ORDER:
        hours = topic.hours.get(kind)
        if hours <= 0:
            continue
        label = pk_label(kind, hours) if is_pk else dop_label(kind, hours)
        blocks.append(
            WorkBlock(kind=kind, hours=hours, label=label, items=items_or_fill(topic.content.get(kind, [])))
        )
    return TopicBlocks(
        topic_id=topic.id, number=number, title=topic.title.strip() or FILL_MARK, blocks=blocks
    )


def _calendar_rows(kit: Kit, numbers: dict[str, str]) -> list[CalendarRow]:
    """График по неделям: учебные дни (занятия), темы недели и часы — суммы дней календаря."""
    calendar = kit.calendar
    if calendar is None or not calendar.days:
        return []
    topics = {t.id: t for t in kit.topics}
    rows: list[CalendarRow] = []
    for week in sorted({d.week for d in calendar.days}):
        days = [d for d in calendar.days if d.week == week]
        labels: list[str] = []
        for day in days:
            for item in day.items:
                if item.is_fa:
                    label = FA_ROW_TITLE
                elif item.topic_id in topics:
                    topic = topics[item.topic_id]
                    label = f"Тема {topic_number(kit, topic, numbers)}"
                else:
                    continue
                if label not in labels:
                    labels.append(label)
        rows.append(
            CalendarRow(
                week=week, days=len(days), content=", ".join(labels) or "—", hours=sum(d.hours for d in days)
            )
        )
    return rows


def _volume(kit: Kit, program: HoursRow) -> Volume:
    calendar = kit.calendar
    schedule = kit.program.schedule
    return Volume(
        total=kit.total_hours(),
        contact=program.contact,
        lk=program.lk,
        pr=program.pr,
        lb=program.lb,
        srs=program.srs,
        distance=program.distance,
        fa=kit.fa_hours(),
        calendar_total=sum(d.hours for d in calendar.days) if calendar is not None else 0,
        weeks=calendar.weeks if calendar is not None and calendar.weeks else None,
        days=len(calendar.days) if calendar is not None and calendar.days else None,
        hours_per_day=schedule.hours_per_day_max,
        session_hours=schedule.session_hours,
        sessions_per_week=schedule.sessions_per_week,
    )


def _competency_rows(kit: Kit) -> list[CompetencyRow]:
    rows: list[CompetencyRow] = []
    for comp in kit.competencies:
        kun = kit.competency_kun(comp.id)
        rows.append(
            CompetencyRow(
                competency_id=comp.id,
                activity=comp.activity.strip() or FILL_MARK,
                title=comp.title.strip() or FILL_MARK,
                master=items_or_fill(kun[OutcomeKind.MASTER]),
                can=items_or_fill(kun[OutcomeKind.CAN]),
                know=items_or_fill(kun[OutcomeKind.KNOW]),
            )
        )
    return rows
