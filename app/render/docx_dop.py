"""Дополнительная общеразвивающая программа в .docx — docs/templates_spec_dop.md.

Чистый Document() со своими стилями: образец (экспорт из Google Docs) как основа не годится (§7.4 спецификации).
Times New Roman задан явно во всех слотах шрифта, включая eastAsia. Учебный план и УТП — в альбомной секции,
у учебного плана двухуровневая шапка. Добавлено то, чего нет в образце: нормативные основания (273-ФЗ, № 629)
и таблица календарного учебного графика. Город, год, реквизиты приказа, ФИО — «____».
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT, WD_SECTION
from docx.shared import Mm, Pt

from app.config import now_msk
from app.core.ids import display_numbers
from app.core.registry import Registry
from app.models import Kit, OutcomeKind, SourceSection, TestScope
from app.render.common import (
    BLANK,
    DASH,
    FILL_MARK,
    Organization,
    at_most_hours,
    cell_text,
    font_run,
    format_par,
    hours_phrase,
    merge_row,
    or_fill,
    set_doc_defaults,
    shade,
    style_font,
    up_to_points,
    upper_first,
)
from app.render.report_docx import write_report_appendix
from app.render.report_md import ReportDoc
from app.render.tables import (
    CALENDAR_HEADER,
    DOP_CALENDAR_SUMMARY_HEADER,
    DOP_PLAN_HEADER_SUB,
    DOP_PLAN_HEADER_TOP,
    DOP_UTP_HEADER,
    FA_ROW_TITLE,
    ProgramTables,
    form_title,
    module_numbers,
)
from app.render.texts import norm_lines, software_lines, source_lines
from app.render.writer import DocWriter, Look, write_current_control, write_final_attestation

PORTRAIT_TW = 9355  # A4 книжная, поля 30 / 15 мм
LANDSCAPE_TW = 14570  # A4 альбомная, поля 20 / 20 мм
MODULE_FILL = "EDEDED"

LOOK = Look(
    body_size=14,
    cell_size=11,
    header_size=11,
    header_fill="D9D9D9",
    total_fill="D9D9D9",
    border_color="000000",
    border_sz=4,
    cell_margins=(60, 90, 60, 90),
    first_line_cm=1.25,
    line_spacing=1.5,
    para_after=0,
    list_left_tw=709,
    list_hanging_tw=283,
    cell_list_left_tw=284,
    cell_list_hanging_tw=170,
    text_width_tw=PORTRAIT_TW,
)

H1 = "1. Пояснительная записка"
H2 = "2. Содержание программы"
H3 = "3. Организационно-педагогические условия реализации программы"
APPENDIX_1 = "Приложение № 1"
APPENDIX_2 = "Приложение № 2"
SUB = {
    "1.1": "1.1. Актуальность программы",
    "1.2": "1.2. Категории обучающихся",
    "1.3": "1.3. Цель и задачи программы",
    "1.4": "1.4. Сроки реализации программы",
    "1.5": "1.5. Планируемые результаты обучения",
    "1.6": "1.6. Календарный учебный график",
    "2.1": "2.1. Учебный план",
    "2.2": "2.2. Учебно-тематический план",
    "2.3": "2.3. Оценочные материалы",
    "2.4": "2.4. Методические материалы",
    "3.1": "3.1. Материально-технические условия",
    "3.2": "3.2. Учебно-методическое и информационное обеспечение",
    "3.3": "3.3. Литература",
    "3.3.1": "3.3.1. Основная литература",
    "3.3.2": "3.3.2. Дополнительная литература",
    "3.3.3": "3.3.3. Интернет-ресурсы",
    "3.4": "3.4. Кадровое обеспечение программы",
}
HEADINGS: list[str] = [
    H1,
    *[SUB[k] for k in ("1.1", "1.2", "1.3", "1.4", "1.5", "1.6")],
    H2,
    *[SUB[k] for k in ("2.1", "2.2", "2.3", "2.4")],
    H3,
    *[SUB[k] for k in ("3.1", "3.2", "3.3", "3.3.1", "3.3.2", "3.3.3", "3.4")],
    APPENDIX_1,
    APPENDIX_2,
]
ORDER_LINE = "Приказом № ____ от «____» ____________ ____ г."


def render_dop(
    kit: Kit, tables: ProgramTables, registry: Registry, org: Organization, path: Path, report: ReportDoc
) -> list[str]:
    doc = Document()
    _styles(doc)
    _page(doc.sections[0], landscape=False)
    _core_properties(doc, kit)
    w = DocWriter(doc, LOOK)
    _title_page(w, kit, org)
    _explanatory_note(w, kit, tables, registry)
    landscape = doc.add_section(WD_SECTION.NEW_PAGE)
    _page(landscape, landscape=True)
    w.heading(H2, level=1)
    w.heading(SUB["2.1"])
    _plan_table(w, tables)
    w.heading(SUB["2.2"])
    _utp_table(w, kit, tables)
    portrait = doc.add_section(WD_SECTION.NEW_PAGE)
    _page(portrait, landscape=False)
    _assessment(w, kit, tables)
    _section_3(w, kit, tables)
    _appendix(w, kit, tables)
    write_report_appendix(w, APPENDIX_2, report)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return w.headings


# --------------------------------------------------------------------------------------------------
# Стили, страница, свойства
# --------------------------------------------------------------------------------------------------


def _styles(doc) -> None:  # noqa: ANN001
    set_doc_defaults(doc, LOOK.body_size)
    normal = doc.styles["Normal"]
    style_font(normal, LOOK.body_size, bold=False)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(0)
    for name, before, after in (("Heading 1", 12, 6), ("Heading 2", 6, 0)):
        style = doc.styles[name]
        style_font(style, LOOK.body_size, bold=True)
        fmt = style.paragraph_format
        fmt.space_before = Pt(before)
        fmt.space_after = Pt(after)
        fmt.line_spacing = 1.5
        fmt.first_line_indent = 0
        fmt.keep_with_next = True


def _page(section, landscape: bool) -> None:  # noqa: ANN001
    if landscape:
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width, section.page_height = Mm(297), Mm(210)
        section.top_margin, section.bottom_margin = Mm(30), Mm(15)
        section.left_margin, section.right_margin = Mm(20), Mm(20)
    else:
        section.orientation = WD_ORIENT.PORTRAIT
        section.page_width, section.page_height = Mm(210), Mm(297)
        section.top_margin, section.bottom_margin = Mm(20), Mm(20)
        section.left_margin, section.right_margin = Mm(30), Mm(15)
    section.header_distance = section.footer_distance = Mm(12.5)


def _core_properties(doc, kit: Kit) -> None:  # noqa: ANN001
    props = doc.core_properties
    props.author = ""
    props.last_modified_by = ""
    props.title = kit.program.title
    props.comments = ""  # «generated by python-docx» из шаблона библиотеки
    props.subject = ""
    props.keywords = ""
    stamp = datetime.now()
    props.created = stamp
    props.modified = stamp
    props.revision = 1


# --------------------------------------------------------------------------------------------------
# Титул и пояснительная записка
# --------------------------------------------------------------------------------------------------


def _line(
    w: DocWriter,
    text: str,
    align: str,
    bold: bool = False,
    size: float | None = None,
    after: float = 6,
    before: float = 0,
) -> None:
    par = w.doc.add_paragraph()
    format_par(par, align=align, before=before, after=after, first_line_cm=0, line_spacing=1.0)
    if text:
        font_run(par.add_run(text), size_pt=size, bold=bold)


def _title_page(w: DocWriter, kit: Kit, org: Organization) -> None:
    for name in org.name_lines or [BLANK]:
        _line(w, name, "center", bold=True)
    _line(w, org.requisites or BLANK, "center", size=12)
    _line(w, org.address or BLANK, "center", size=12)
    _line(w, "УТВЕРЖДЕНО", "right", before=60, after=0)
    _line(w, ORDER_LINE, "right", after=0)
    _line(w, "ДОПОЛНИТЕЛЬНАЯ ОБЩЕОБРАЗОВАТЕЛЬНАЯ", "center", bold=True, before=90)
    _line(w, "ОБЩЕРАЗВИВАЮЩАЯ ПРОГРАММА", "center", bold=True)
    _line(w, f"«{kit.program.title.strip() or FILL_MARK}»", "center", bold=True)
    # город и год — как в образце и в программе ПК: город из конфигурации организации, год сборки комплекта
    year = (kit.meta.created_at or now_msk()).year
    _line(w, f"{org.city or BLANK} {year}", "center", bold=True, before=200)


def _explanatory_note(w: DocWriter, kit: Kit, tables: ProgramTables, registry: Registry) -> None:
    program = kit.program
    w.heading(H1, level=1, page_break=True)
    w.para(
        kit.texts.annotation.strip()
        or f"Дополнительная общеобразовательная общеразвивающая программа «{program.title}» (далее — Программа) "
        "определяет содержание, объём, планируемые результаты и условия обучения."
    )
    w.subhead("Нормативно-правовые основания")
    w.bullets(
        norm_lines(kit, registry)
        + [
            f"локальные нормативные акты организации: устав, положение о дополнительных общеобразовательных программах ({BLANK})"
        ]
    )
    w.label_value("Направленность", upper_first(program.direction or ""))
    w.label_value("Уровень освоения", upper_first(program.level or ""))
    w.label_value("Форма обучения", form_title(kit))
    if kit.texts.form_description:
        w.para(kit.texts.form_description)
    w.label_value("Документ, выдаваемый по итогам обучения", program.document.lower())
    w.subhead("Условия обучения")
    w.bullets(kit.texts.mto)
    w.subhead("Программное обеспечение")
    w.bullets(software_lines(kit))

    w.heading(SUB["1.1"])
    w.para(or_fill(kit.texts.relevance))

    w.heading(SUB["1.2"])
    audience = program.audience
    w.label_value("Категория обучающихся", audience.category)
    if audience.age_or_grade:
        w.label_value("Возраст (классы)", audience.age_or_grade)
    if audience.entry_level:
        w.label_value("Входной уровень", audience.entry_level)
    requirements = kit.texts.audience_requirements.strip() or (audience.requirements or "").strip()
    if requirements:
        w.para(requirements)

    w.heading(SUB["1.3"])
    w.label_value("Цель", program.goal)
    w.subhead("Задачи программы")
    w.bullets(program.tasks)

    w.heading(SUB["1.4"])
    v = tables.volume
    term = f"{v.weeks} нед. ({v.days} занятий)" if v.weeks and v.days else FILL_MARK
    w.para(f"Срок освоения программы — {term}, трудоёмкость — {hours_phrase(v.total)}.")
    w.para("Даты начала и окончания обучения устанавливаются приказом о зачислении.")

    w.heading(SUB["1.5"])
    for kind, label in (
        (OutcomeKind.KNOW, "Знания:"),
        (OutcomeKind.CAN, "Умения:"),
        (OutcomeKind.MASTER, "Навыки:"),
    ):
        w.subhead(label)
        w.bullets([o.text for o in kit.outcomes if o.kind == kind])

    w.heading(SUB["1.6"])
    _calendar(w, kit, tables)


def _regime(kit: Kit) -> str:
    schedule = kit.program.schedule
    if schedule.session_hours and schedule.sessions_per_week:
        return f"{schedule.sessions_per_week} раза в неделю по {hours_phrase(schedule.session_hours)}"
    if schedule.session_hours:
        return f"занятия по {hours_phrase(schedule.session_hours)}"
    if schedule.hours_per_day_max:
        return f"{at_most_hours(schedule.hours_per_day_max)} в день"
    return FILL_MARK


def _calendar(w: DocWriter, kit: Kit, tables: ProgramTables) -> None:
    v = tables.volume
    w.para(
        f"Режим занятий: {_regime(kit)}. Распределение часов: аудиторные занятия — {hours_phrase(v.contact)} "
        f"(теория — {hours_phrase(v.lk)}, практика — {hours_phrase(v.pr + v.lb)}), самостоятельная работа — "
        f"{hours_phrase(v.srs)}, итоговая аттестация — {hours_phrase(v.fa)}."
    )
    summary = w.table(2, [1500, 1500, 1400, 1600, 1500, 1855])
    w.header_cells(summary, 0, DOP_CALENDAR_SUMMARY_HEADER)
    values = [BLANK, BLANK, str(v.weeks or FILL_MARK), str(v.days or FILL_MARK)]
    for col, text in enumerate(values):
        cell_text(summary.rows[1].cells[col], text, LOOK.cell_size, align="center")
    w.number_cell(summary, 1, 4, v.total)
    w.text_cell(summary, 1, 5, _regime(kit))
    w.after_table()
    rows = tables.calendar
    if not rows:
        w.para(FILL_MARK)
        return
    w.para("График по неделям:", keep_next=True, first_line=False)
    table = w.table(len(rows) + 2, [1100, 1300, 5355, 1600])
    w.header_cells(table, 0, [CALENDAR_HEADER[0], "Занятия", CALENDAR_HEADER[2], CALENDAR_HEADER[3]])
    for idx, row in enumerate(rows, start=1):
        cell_text(table.rows[idx].cells[0], str(row.week), LOOK.cell_size, align="center")
        cell_text(table.rows[idx].cells[1], str(row.days), LOOK.cell_size, align="center")
        w.text_cell(table, idx, 2, row.content)
        w.number_cell(table, idx, 3, row.hours)
    last = len(rows) + 1
    merged = merge_row(table, last, 0, 2)
    cell_text(merged, "Итого часов", LOOK.cell_size, bold=True, fill=LOOK.total_fill)
    w.number_cell(table, last, 3, tables.volume.calendar_total, bold=True)
    w.after_table()


# --------------------------------------------------------------------------------------------------
# Альбомная секция: учебный план и УТП
# --------------------------------------------------------------------------------------------------


def _plan_table(w: DocWriter, tables: ProgramTables) -> None:
    rows = tables.plan
    table = w.table(len(rows) + 2, [600, 4300, 1500, 1500, 1500, 1500, 1600, 2000], header_rows=2)
    for col in (0, 1, 2, 3, 6, 7):
        merged = table.cell(0, col).merge(table.cell(1, col))
        cell_text(
            merged,
            DOP_PLAN_HEADER_TOP[col],
            LOOK.cell_size,
            bold=True,
            align="center",
            fill=LOOK.header_fill,
            v_center=True,
        )
    top = table.cell(0, 4).merge(table.cell(0, 5))
    cell_text(top, "в т.ч.", LOOK.cell_size, bold=True, align="center", fill=LOOK.header_fill, v_center=True)
    for col, text in zip((4, 5), DOP_PLAN_HEADER_SUB, strict=True):
        cell_text(
            table.cell(1, col),
            text,
            LOOK.cell_size,
            bold=True,
            align="center",
            fill=LOOK.header_fill,
            v_center=True,
        )
    for idx, row in enumerate(rows, start=2):
        h = row.hours
        bold = row.kind in ("fa", "total")
        cell_text(table.rows[idx].cells[0], row.number, LOOK.cell_size, align="center")
        w.text_cell(table, idx, 1, row.title, bold=bold)
        w.number_cell(table, idx, 2, h.total, bold=bold)
        if row.kind == "fa":
            for col in (3, 4, 5, 6):
                cell_text(table.rows[idx].cells[col], DASH, LOOK.cell_size, align="center")
        else:
            for col, value in zip((3, 4, 5, 6), (h.contact, h.lk, h.practice, h.srs), strict=True):
                w.number_cell(table, idx, col, value, bold=bold)
        w.text_cell(table, idx, 7, row.form if row.kind != "total" else "")
        if row.kind == "total":
            for cell in table.rows[idx].cells:
                shade(cell, LOOK.total_fill)
    w.after_table()


def _utp_table(w: DocWriter, kit: Kit, tables: ProgramTables) -> None:
    n_rows = 1 + sum(1 + sum(max(len(t.blocks), 1) for t in s.topics) for s in tables.sections) + 1
    table = w.table(n_rows, [3100, 2700, 8700])
    w.header_cells(table, 0, DOP_UTP_HEADER)
    row = 1
    for section in tables.sections:
        merged = merge_row(table, row, 0, 2)
        title = section.title if section.number == "0" else f"Модуль {section.number}. {section.title}"
        cell_text(merged, title, LOOK.cell_size, bold=True, fill=MODULE_FILL)
        row += 1
        for topic in section.topics:
            for idx, block in enumerate(topic.blocks or [None]):
                w.text_cell(table, row, 0, f"Тема {topic.number}. {topic.title}" if idx == 0 else "")
                if block is None:
                    w.text_cell(table, row, 1, FILL_MARK)
                    w.text_cell(table, row, 2, "")
                else:
                    w.text_cell(table, row, 1, block.label)
                    w.text_cell(table, row, 2, "; ".join(block.items))
                row += 1
    fa = kit.final_attestation
    w.text_cell(table, row, 0, FA_ROW_TITLE, bold=True)
    w.text_cell(table, row, 1, f"{tables.fa_title} – {hours_phrase(tables.fa_hours)}", bold=True)
    w.text_cell(table, row, 2, (fa.expected_result or fa.form_text or "") if fa is not None else FILL_MARK)
    w.after_table()


# --------------------------------------------------------------------------------------------------
# Оценочные и методические материалы, раздел 3, приложение
# --------------------------------------------------------------------------------------------------


def seminar_criteria(kit: Kit) -> list[str]:
    """Критерии практических работ для раздела 2.3: работа — строка со всеми её критериями, без повторов работ."""
    lines: list[str] = []
    numbers = display_numbers(kit)
    for seminar in sorted(kit.seminars, key=lambda s: int(numbers.get(s.id, "0") or 0)):
        texts = list(dict.fromkeys(c.text.strip().rstrip(".") for c in seminar.criteria if c.text.strip()))
        if texts:
            lines.append(f"{seminar.title.strip().rstrip('.')} — {'; '.join(texts)}.")
    return list(dict.fromkeys(lines))


def _assessment(w: DocWriter, kit: Kit, tables: ProgramTables) -> None:
    numbers = module_numbers(kit)
    w.heading(SUB["2.3"])
    w.para(
        "Освоение программы проверяется текущим контролем по модулям и итоговой аттестацией. Этапы контроля, "
        "сдаваемые работы и порядок оценивания приведены в таблице, задания и вопросы — в Приложении № 1."
    )
    modules = [m for m in kit.modules_sorted() if m.control_form]
    table = w.table(len(modules) + 2, [2300, 3300, 3700])
    w.header_cells(table, 0, ["Этап", "Что сдаёт обучающийся", "Кто и как оценивает"])
    topic_module = {t.id: t.module_id for t in kit.topics}
    for idx, module in enumerate(modules, start=1):
        number = numbers[module.id]
        stage = f"Текущий контроль: модуль {number}" if number != "0" else f"Текущий контроль: {module.title}"
        artifacts = [
            s.deliverable for s in kit.seminars if topic_module.get(s.topic_id) == module.id and s.deliverable
        ]
        tests = [t for t in kit.tests if t.scope == TestScope.MODULE and t.module_id == module.id]
        if tests:
            artifacts.append("ответы на тест модуля")
        w.text_cell(table, idx, 0, stage)
        w.text_cell(table, idx, 1, "; ".join(artifacts) or upper_first(module.control_form or ""))
        w.text_cell(table, idx, 2, f"Преподаватель: {module.control_form}, проверка по критериям задания")
    last = len(modules) + 1
    fa = kit.final_attestation
    w.text_cell(table, last, 0, FA_ROW_TITLE)
    w.text_cell(table, last, 1, (fa.expected_result or tables.fa_title) if fa is not None else FILL_MARK)
    w.text_cell(table, last, 2, "Комиссия по критериям итоговой аттестации")
    w.after_table()
    criteria = seminar_criteria(kit)
    if criteria:
        # все критерии, по работе на строку: обрезанный список выглядел незаполненным разделом (оценка 21.10)
        w.subhead("Критерии проверки практических работ")
        w.bullets(criteria)
        w.para("Баллы по каждому критерию и порог зачёта — в Приложении № 1.", first_line=False)
    w.subhead("Итоговая аттестация")
    text = f"Форма — {tables.fa_title.lower()}, {hours_phrase(tables.fa_hours)}."
    if fa is not None and fa.pass_rule:
        text += f" {upper_first(fa.pass_rule.strip())}"
    w.para(text)
    w.subhead("Критерии оценивания")
    w.bullets([f"{c.text} — {up_to_points(c.max_points)}" for c in fa.criteria] if fa is not None else [])

    w.heading(SUB["2.4"])
    w.para(
        kit.texts.methodical.strip()
        or "Занятия строятся по схеме «короткое объяснение — выполнение задания — обсуждение результата». "
        "Теоретическая часть сопровождается показом примеров, практическая выполняется на учебных данных. "
        "Преподаватель консультирует обучающихся индивидуально и в группе, даёт обратную связь по каждой работе."
    )


def _section_3(w: DocWriter, kit: Kit, tables: ProgramTables) -> None:
    w.heading(H3, level=1, page_break=True)
    w.heading(SUB["3.1"])
    w.bullets(kit.texts.mto)
    w.heading(SUB["3.2"])
    w.para("Для реализации программы используются:", keep_next=True)
    w.bullets(software_lines(kit))
    w.heading(SUB["3.3"])
    for key, section in (
        ("3.3.1", SourceSection.MAIN),
        ("3.3.2", SourceSection.ADDITIONAL),
        ("3.3.3", SourceSection.INTERNET),
    ):
        w.heading(SUB[key])
        w.numbered(source_lines(kit, section))
    w.heading(SUB["3.4"])
    if kit.texts.staffing_requirements:
        w.para(kit.texts.staffing_requirements)
    rows = tables.staffing
    table = w.table(len(rows) + 1, [700, 3900, 1700, 1500, 1555])
    w.header_cells(
        table,
        0,
        [
            "№",
            "Модуль / дисциплина в соответствии с учебным планом",
            "ФИО преподавателя",
            "Уч. степень / звание",
            "Должность",
        ],
    )
    for idx, title in enumerate(rows, start=1):
        cell_text(table.rows[idx].cells[0], str(idx), LOOK.cell_size, align="center")
        w.text_cell(table, idx, 1, title)
        for col in (2, 3, 4):
            cell_text(table.rows[idx].cells[col], BLANK, LOOK.cell_size, align="center")
    w.after_table()


def _appendix(w: DocWriter, kit: Kit, tables: ProgramTables) -> None:
    w.heading(APPENDIX_1, level=1, page_break=True, align="right")
    par = w.doc.add_paragraph()
    format_par(par, align="right", before=0, after=12, first_line_cm=0, line_spacing=1.0)
    font_run(
        par.add_run(f"к дополнительной общеобразовательной общеразвивающей программе «{kit.program.title}»"),
        size_pt=12,
    )
    w.para("Оценочные материалы", bold=True, align="center", keep_next=True, first_line=False)
    write_current_control(w, kit, "Модуль")
    w.subhead("Итоговая аттестация")
    write_final_attestation(w, kit, tables.fa_title)
