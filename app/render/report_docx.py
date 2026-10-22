"""Отдельный report.docx для методиста — тот же ReportDoc, что и report.md."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from docx import Document
from docx.shared import Mm

from app.render.common import cell_text, set_doc_defaults, style_font
from app.render.docx_pk import LOOK
from app.render.report_md import Advice, Bullets, Para, ReportDoc, Table
from app.render.writer import DocWriter

REPORT_DOCX_NAME = "report.docx"


def render_report_docx(report: ReportDoc, path: Path) -> str:
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    section.left_margin = section.right_margin = Mm(20)
    section.top_margin = section.bottom_margin = Mm(20)
    set_doc_defaults(document, LOOK.body_size)
    style_font(document.styles["Normal"], LOOK.body_size)
    for name in ("Heading 1", "Heading 2"):
        style_font(document.styles[name], LOOK.body_size, bold=True)
    properties = document.core_properties
    properties.title = report.title
    properties.author = properties.last_modified_by = properties.comments = ""
    width = int(section.page_width.twips - section.left_margin.twips - section.right_margin.twips)
    w = DocWriter(document, replace(LOOK, text_width_tw=width))
    w.heading(report.title, level=1)
    w.para(f"Итог. {report.summary}", bold=True, first_line=False)
    for section in report.sections:
        w.subhead(section.title)
        advice_no = 0
        for block in section.blocks:
            if isinstance(block, Para):
                w.para(block.text)
            elif isinstance(block, Bullets):
                w.bullets(block.items)
            elif isinstance(block, Advice):
                advice_no += 1
                _advice(w, advice_no, block)
            elif isinstance(block, Table):
                _table(w, block)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))
    Document(str(path))  # повреждённый файл не должен попадать в выдачу
    return path.name


def _advice(w: DocWriter, n: int, block: Advice) -> None:
    """Предупреждение методисту: заголовок, текст, цифры одной строкой, нумерованные варианты решения."""
    w.para(f"{n}. {block.title}", bold=True, align="left", keep_next=True, first_line=False)
    w.para(block.text)
    if block.figures:
        w.para("Цифры: " + "; ".join(f"{label} — {value}" for label, value in block.figures) + ".")
    w.para("Варианты решения:", align="left", keep_next=True, first_line=False)
    w.numbered(block.options)


def _table(w: DocWriter, block: Table) -> None:
    total = sum(block.weights)
    widths = [w.look.text_width_tw * weight // total for weight in block.weights]
    table = w.table(len(block.rows) + 1, widths)
    w.header_cells(table, 0, block.header)
    for idx, row in enumerate(block.rows, start=1):
        bold = row[0] in ("Итого", "Итоговая аттестация")
        for col, value in enumerate(row):
            align = "left" if col == 0 or len(value) > 12 else "center"
            cell_text(table.rows[idx].cells[col], value, w.look.cell_size, bold=bold, align=align)
    w.after_table()
