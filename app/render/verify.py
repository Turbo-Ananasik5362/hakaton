"""V31 «Выгрузки»: готовый .docx перечитывается и сверяется с Kit напрямую (не со снимком ProgramTables).

Проверяется: файл открывается; все разделы шаблона на месте и в нужном порядке; числа учебного плана, УТП,
рабочей программы (ПК) или УТП по видам занятий (ДОП) и календарного графика равны часам Kit по каждой строке,
итогам и ИА; метки «ПР (N часов)» совпадают с колонкой; реквизиты приказа и ФИО пустые («____»).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import docx
from docx.table import Table, _Row

from app.core.hours import fmt_hours
from app.core.validate import ORDER_FILLED_RES
from app.models import FixBy, Issue, Kit, ProgramType, Severity, WorkKind
from app.render.common import BLANK, parse_num
from app.render.tables import (
    CALENDAR_HEADER,
    DOP_KIND_TITLES,
    DOP_UTP_HEADER,
    KIND_ABBR,
    PK_PLAN_HEADER,
    PK_TYPES,
    PK_UTP_HEADER,
    PK_WP_HEADER,
)

CHECK = "V31"
REFS = ["G6a", "G6b", "K7"]
EPS = 1e-6
MAX_ROW_ISSUES = 12  # дальше одна сводная строка: длинный список одинаковых расхождений методисту не нужен

PK_LABEL_RE = re.compile(r"^(ЛК|ПР|ЛБ|СРС)\s*\((\d+(?:,\d+)?)\s*час", re.I)
DOP_LABEL_RE = re.compile(r"^(.+?)\s*[–-]\s*(\d+(?:,\d+)?)\s*час", re.I)
ABBR_KIND = {v: k for k, v in KIND_ABBR.items()}


@dataclass
class _Found:
    severity: Severity
    message: str
    detail: str = ""


def verify_docx(path: Path, kit: Kit, headings: Sequence[str]) -> list[Issue]:
    found: list[_Found] = []
    try:
        document = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001 — любой сбой чтения = файл битый
        found.append(
            _Found(Severity.ERROR, "Файл программы .docx не открывается.", f"{type(exc).__name__}: {exc}")
        )
        return _issues(found)
    found += _check_headings(document, headings)
    found += _check_requisites(document, kit)
    tables = list(document.tables)
    if kit.program.type in PK_TYPES:
        found += _check_pk_tables(tables, kit)
    else:
        found += _check_dop_tables(tables, kit)
    found += _check_calendar(tables, kit)
    return _issues(found)


def _issues(found: list[_Found]) -> list[Issue]:
    if len(found) > MAX_ROW_ISSUES:
        rest = len(found) - MAX_ROW_ISSUES
        found = found[:MAX_ROW_ISSUES] + [
            _Found(Severity.ERROR, f"И ещё {rest} расхождений документа с комплектом того же рода.")
        ]
    return [
        Issue(
            id=f"{CHECK}-{n}",
            check=CHECK,
            severity=f.severity,
            message=f.message,
            detail=f.detail,
            checklist_refs=list(REFS),
            fix_by=FixBy.CODE,
        )
        for n, f in enumerate(found, start=1)
    ]


# --------------------------------------------------------------------------------------------------
# Разделы и реквизиты
# --------------------------------------------------------------------------------------------------


def _check_headings(document, expected: Sequence[str]) -> list[_Found]:  # noqa: ANN001
    actual = [
        p.text.strip()
        for p in document.paragraphs
        if p.style is not None and p.style.name.startswith("Heading")
    ]
    missing = [h for h in expected if h not in actual]
    out: list[_Found] = []
    if missing:
        out.append(
            _Found(
                Severity.ERROR,
                f"В документе нет разделов шаблона: {'; '.join(missing[:6])}{' …' if len(missing) > 6 else ''}.",
                f"missing headings: {missing}",
            )
        )
    present = [h for h in actual if h in expected]
    if not missing and present != [h for h in expected if h in present]:
        out.append(
            _Found(
                Severity.WARNING, "Разделы документа идут не в том порядке, что в шаблоне.", "heading order"
            )
        )
    return out


def _check_requisites(document, kit: Kit) -> list[_Found]:  # noqa: ANN001
    out: list[_Found] = []
    order = next((p.text for p in document.paragraphs if p.text.strip().startswith("Приказом")), None)
    if order is None:
        out.append(
            _Found(Severity.ERROR, "На титульном листе нет строки «Приказом № … от …».", "no order line")
        )
    elif BLANK not in order or any(regex.search(order) for regex in ORDER_FILLED_RES):
        out.append(
            _Found(
                Severity.ERROR,
                f"Реквизиты приказа на титуле заполнены («{order.strip()}») — они должны оставаться пустыми полями.",
                "order requisites filled",
            )
        )
    for table in document.tables:
        header = [c.text.strip() for c in table.rows[0].cells]
        if "ФИО преподавателя" not in header:
            continue
        cols = [
            i for i, h in enumerate(header) if h in ("ФИО преподавателя", "Уч. степень / звание", "Должность")
        ]
        filled = [
            r.cells[i].text.strip() for r in table.rows[1:] for i in cols if r.cells[i].text.strip() != BLANK
        ]
        if filled:
            out.append(
                _Found(
                    Severity.ERROR,
                    "В таблице кадрового обеспечения заполнены ФИО, степени или должности — они должны быть «____».",
                    f"staffing filled: {filled[:3]}",
                )
            )
    if kit.program.type == ProgramType.DOP:
        texts = [p.text.strip() for p in document.paragraphs]
        if f"{BLANK} {BLANK}" not in texts:
            out.append(
                _Found(
                    Severity.ERROR,
                    "Город и год на титуле ДОП должны быть пустыми полями «____».",
                    "city/year",
                )
            )
    return out


# --------------------------------------------------------------------------------------------------
# Таблицы часов
# --------------------------------------------------------------------------------------------------


def _header(table: Table, row: int = 0) -> list[str]:
    return [c.text.strip() for c in table.rows[row].cells]


def _find(tables: list[Table], header: Sequence[str]) -> Table | None:
    return next((t for t in tables if len(t.rows) and _header(t) == list(header)), None)


def _nums(row: _Row, cols: Sequence[int]) -> list[float | None]:
    return [parse_num(row.cells[c].text) for c in cols]


def _differs(doc: Sequence[float | None], kit: Sequence[float | None]) -> bool:
    return any(
        (a is None) != (b is None) or (a is not None and b is not None and abs(a - b) > EPS)
        for a, b in zip(doc, kit, strict=True)
    )


def _fmt(values: Sequence[float | None]) -> str:
    return " / ".join("—" if v is None else f"{v:g}".replace(".", ",") for v in values)


def _compare_rows(
    table: Table | None,
    name: str,
    first_row: int,
    expected: list[tuple[str, list[float | None]]],
    cols: Sequence[int],
    columns: str,
) -> list[_Found]:
    if table is None:
        return [_Found(Severity.ERROR, f"В документе не найдена таблица «{name}».", f"no table {name}")]
    rows = table.rows[first_row:]
    out: list[_Found] = []
    if len(rows) != len(expected):
        out.append(
            _Found(
                Severity.ERROR,
                f"«{name}»: в документе {len(rows)} строк, а в комплекте {len(expected)}.",
                f"{name}: rows {len(rows)} vs {len(expected)}",
            )
        )
    for row, (title, want) in zip(rows, expected, strict=False):
        got = _nums(row, cols)
        if _differs(got, want):
            out.append(
                _Found(
                    Severity.ERROR,
                    f"«{name}», строка «{title}»: в документе {columns} = {_fmt(got)}, в комплекте {_fmt(want)}.",
                    f"{name}/{title}: {got} vs {want}",
                )
            )
    return out


def _pk_expected(kit: Kit, with_topics: bool) -> list[tuple[str, list[float | None]]]:
    rows: list[tuple[str, list[float | None]]] = []
    for module in kit.modules_sorted():
        h = kit.module_hours(module.id)
        rows.append((module.title, [h.total, h.lk, h.pr, h.lb, h.srs]))
        if with_topics:
            for topic in kit.topics_of(module.id):
                t = topic.hours
                rows.append((topic.title, [t.total, t.lk, t.pr, t.lb, t.srs]))
    p = kit.program_hours()
    rows.append(("Итоговая аттестация", [kit.fa_hours(), None, None, None, None]))
    rows.append(("Итого", [kit.total_hours(), p.lk, p.pr, p.lb, p.srs]))
    return rows


def _check_pk_tables(tables: list[Table], kit: Kit) -> list[_Found]:
    cols = (2, 3, 4, 5, 6)
    columns = "всего / ЛК / ПР / ЛБ / СРС"
    out = _compare_rows(
        _find(tables, PK_PLAN_HEADER), "Учебный план", 1, _pk_expected(kit, False), cols, columns
    )
    out += _compare_rows(
        _find(tables, PK_UTP_HEADER), "Учебно-тематический план", 1, _pk_expected(kit, True), cols, columns
    )
    out += _check_pk_work_program(_find(tables, PK_WP_HEADER), kit)
    return out


def _check_pk_work_program(table: Table | None, kit: Kit) -> list[_Found]:
    name = "Рабочая программа"
    if table is None:
        return [_Found(Severity.ERROR, f"В документе не найдена таблица «{name}».", "no work program")]
    out: list[_Found] = []
    sections: list[float | None] = []
    topics: list[dict[WorkKind, float]] = []
    tail: dict[str, float | None] = {}
    for row in table.rows[1:]:
        first = row.cells[0].text.strip()
        hours = parse_num(row.cells[2].text)
        if first.startswith("Раздел"):
            sections.append(hours)
            continue
        if first in ("Итоговая аттестация", "Итого"):
            tail[first] = hours
            continue
        if first.startswith("Тема"):
            topics.append({})
        if not topics:
            continue
        label = row.cells[1].paragraphs[0].text.strip()
        match = PK_LABEL_RE.match(label)
        if match is None:
            continue
        kind = ABBR_KIND[match.group(1).upper()]
        label_hours = float(match.group(2).replace(",", "."))
        if hours is None or abs(label_hours - hours) > EPS:
            out.append(
                _Found(
                    Severity.ERROR,
                    f"«{name}»: метка «{label}» не совпадает с числом в колонке часов ({row.cells[2].text.strip()}).",
                    f"label {label_hours} vs column {hours}",
                )
            )
        topics[-1][kind] = topics[-1].get(kind, 0.0) + (hours or 0.0)
    kit_topics = [t for m in kit.modules_sorted() for t in kit.topics_of(m.id)]
    if len(topics) != len(kit_topics):
        out.append(
            _Found(
                Severity.ERROR,
                f"«{name}»: в документе {len(topics)} тем, в комплекте {len(kit_topics)}.",
                "topics count",
            )
        )
    for doc_topic, topic in zip(topics, kit_topics, strict=False):
        for kind in WorkKind:
            want = topic.hours.get(kind)
            got = doc_topic.get(kind, 0.0)
            if abs(want - got) > EPS:
                out.append(
                    _Found(
                        Severity.ERROR,
                        f"«{name}», тема «{topic.title}»: {KIND_ABBR[kind]} в документе {fmt_hours(got)}, "
                        f"в учебно-тематическом плане {fmt_hours(want)}.",
                        f"{topic.id} {kind.value}: {got} vs {want}",
                    )
                )
    want_sections = [kit.module_hours(m.id).total for m in kit.modules_sorted()]
    if len(sections) != len(want_sections) or _differs(sections, want_sections):
        out.append(
            _Found(
                Severity.ERROR,
                f"«{name}»: часы разделов не совпадают с учебным планом.",
                f"{sections} vs {want_sections}",
            )
        )
    for title, want in (("Итоговая аттестация", kit.fa_hours()), ("Итого", kit.total_hours())):
        if tail.get(title) is None or abs((tail.get(title) or 0) - want) > EPS:
            out.append(
                _Found(
                    Severity.ERROR,
                    f"«{name}»: строка «{title}» — {tail.get(title)} вместо {want:g}.",
                    f"{title}",
                )
            )
    return out


def _dop_expected(kit: Kit) -> list[tuple[str, list[float | None]]]:
    rows: list[tuple[str, list[float | None]]] = []
    for module in kit.modules_sorted():
        h = kit.module_hours(module.id)
        rows.append((module.title, [h.total, h.contact, h.lk, h.pr + h.lb, h.srs]))
    p = kit.program_hours()
    rows.append(("Итоговая аттестация", [kit.fa_hours(), None, None, None, None]))
    rows.append(("Итого", [kit.total_hours(), p.contact, p.lk, p.pr + p.lb, p.srs]))
    return rows


def _check_dop_tables(tables: list[Table], kit: Kit) -> list[_Found]:
    plan = next(
        (
            t
            for t in tables
            if len(t.rows) > 2
            and "Лекции / теория" in _header(t, 1)
            and "Общая трудоёмкость, час." in _header(t)
        ),
        None,
    )
    out = _compare_rows(
        plan,
        "Учебный план",
        2,
        _dop_expected(kit),
        (2, 3, 4, 5, 6),
        "всего / аудиторных / теория / практика / СР",
    )
    utp = _find(tables, DOP_UTP_HEADER)
    name = "Учебно-тематический план"
    if utp is None:
        return out + [_Found(Severity.ERROR, f"В документе не найдена таблица «{name}».", "no utp")]
    kinds = {title: kind for kind, title in DOP_KIND_TITLES.items() if kind != WorkKind.LB}
    topics: list[dict[str, float]] = []
    fa_hours: float | None = None
    for row in utp.rows[1:]:
        if len({id(c._tc) for c in row.cells}) == 1:
            continue  # строка модуля на всю ширину
        first = row.cells[0].text.strip()
        match = DOP_LABEL_RE.match(row.cells[1].text.strip())
        if first == "Итоговая аттестация":
            fa_hours = float(match.group(2).replace(",", ".")) if match else None
            continue
        if first.startswith("Тема"):
            topics.append({})
        if not topics or match is None or match.group(1) not in kinds:
            continue
        key = kinds[match.group(1)].value
        topics[-1][key] = topics[-1].get(key, 0.0) + float(match.group(2).replace(",", "."))
    kit_topics = [t for m in kit.modules_sorted() for t in kit.topics_of(m.id)]
    if len(topics) != len(kit_topics):
        out.append(
            _Found(
                Severity.ERROR,
                f"«{name}»: в документе {len(topics)} тем, в комплекте {len(kit_topics)}.",
                "topics count",
            )
        )
    for doc_topic, topic in zip(topics, kit_topics, strict=False):
        h = topic.hours
        want = {"lk": h.lk, "pr": h.pr + h.lb, "srs": h.srs}
        for key, value in want.items():
            if abs(doc_topic.get(key, 0.0) - value) > EPS:
                out.append(
                    _Found(
                        Severity.ERROR,
                        f"«{name}», тема «{topic.title}»: часы занятий в документе не совпадают с учебным планом.",
                        f"{topic.id} {key}: {doc_topic.get(key, 0.0)} vs {value}",
                    )
                )
                break
    if fa_hours is None or abs(fa_hours - kit.fa_hours()) > EPS:
        out.append(
            _Found(
                Severity.ERROR,
                f"«{name}»: часы итоговой аттестации {fa_hours} вместо {kit.fa_hours():g}.",
                "fa",
            )
        )
    return out


def _check_calendar(tables: list[Table], kit: Kit) -> list[_Found]:
    if kit.calendar is None or not kit.calendar.days:
        return []
    table = next(
        (
            t
            for t in tables
            if len(t.rows) > 1
            and _header(t)[0] == CALENDAR_HEADER[0]
            and _header(t)[-1] == CALENDAR_HEADER[-1]
        ),
        None,
    )
    if table is None:
        return [
            _Found(
                Severity.ERROR, "В документе не найдена таблица календарного учебного графика.", "no calendar"
            )
        ]
    rows = [parse_num(r.cells[-1].text) for r in table.rows[1:]]
    weeks, total = rows[:-1], rows[-1]
    want = sum(d.hours for d in kit.calendar.days)
    got = sum(x or 0 for x in weeks)
    out: list[_Found] = []
    if abs(got - want) > EPS or total is None or abs(total - want) > EPS:
        out.append(
            _Found(
                Severity.ERROR,
                f"Календарный учебный график: в документе {got:g} ч (итог {total}), в календаре комплекта {want:g} ч.",
                f"calendar {got}/{total} vs {want}",
            )
        )
    if abs(want - kit.total_hours()) > EPS:
        out.append(
            _Found(
                Severity.WARNING,
                f"Часы календарного графика ({want:g}) не равны объёму программы ({kit.total_hours():g}).",
                "calendar vs total",
            )
        )
    return out


# --------------------------------------------------------------------------------------------------
# Слайды и тесты
# --------------------------------------------------------------------------------------------------


def verify_pptx(path: Path, expected: Sequence[tuple[str, int]]) -> list[Issue]:
    """expected — (заголовок титульного слайда лекции = тема учебного плана, число слайдов лекции) по порядку."""
    from pptx import Presentation

    found: list[_Found] = []
    try:
        deck = Presentation(str(path))
    except Exception as exc:  # noqa: BLE001
        return _issues(
            [_Found(Severity.ERROR, "Файл слайдов .pptx не открывается.", f"{type(exc).__name__}: {exc}")]
        )
    slides = list(deck.slides)
    want = sum(n for _, n in expected)
    if len(slides) != want:
        found.append(
            _Found(
                Severity.ERROR,
                f"В слайдах {len(slides)} слайдов, а по планам лекций нужно {want}.",
                "slide count",
            )
        )
    pos = 0
    for title, count in expected:
        if pos < len(slides):
            shape = slides[pos].shapes.title
            actual = shape.text_frame.text.strip() if shape is not None else ""
            if actual != title.strip():
                found.append(
                    _Found(
                        Severity.ERROR,
                        f"Титульный слайд лекции «{actual}» не совпадает с темой учебного плана «{title}».",
                        "title",
                    )
                )
        pos += count
    empty_notes = sum(
        1 for s in slides if not (s.has_notes_slide and s.notes_slide.notes_text_frame.text.strip())
    )
    if empty_notes:
        found.append(_Found(Severity.WARNING, f"У {empty_notes} слайдов нет заметок докладчика.", "notes"))
    return _issues(found)


def verify_tests(files: dict[str, Path], expected: int) -> list[Issue]:
    """GIFT, Moodle XML и CSV разбираются и содержат все вопросы банка."""
    import csv
    import xml.etree.ElementTree as ET

    from app.render.gift import parse_gift

    found: list[_Found] = []
    counters = {
        "GIFT": lambda p: len(parse_gift(p.read_text(encoding="utf-8"))),
        "Moodle XML": lambda p: len(ET.parse(str(p)).getroot().findall("question[@type='multichoice']")),
        "CSV": lambda p: (
            len(list(csv.reader(p.read_text(encoding="utf-8-sig").splitlines(), delimiter=";"))) - 1
        ),
    }
    for label, path in files.items():
        try:
            count = counters[label](path)
        except Exception as exc:  # noqa: BLE001
            found.append(
                _Found(Severity.ERROR, f"Файл тестов {label} не разбирается.", f"{type(exc).__name__}: {exc}")
            )
            continue
        if count != expected:
            found.append(
                _Found(
                    Severity.ERROR,
                    f"В файле тестов {label} {count} вопросов, в комплекте {expected}.",
                    f"{label} count",
                )
            )
    return _issues(found)
