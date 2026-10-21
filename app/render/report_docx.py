"""Приложение «Отчёт автопроверки» в программе .docx — тот же ReportDoc, что и report.md.

Служебное приложение для методиста (перед утверждением программы его можно удалить). Документ собирается до
проверки V31, поэтому итог сверки самих файлов — в report.md и report.json.
"""

from __future__ import annotations

from app.render.common import cell_text
from app.render.report_md import Advice, Bullets, Para, ReportDoc, Table
from app.render.writer import DocWriter

APPENDIX_SUBTITLE = "Отчёт автопроверки комплекта"
APPENDIX_NOTE = (
    "Служебное приложение для методиста: перед утверждением программы его можно удалить. Сверка самих файлов "
    "выгрузки с комплектом выполняется после сборки документа — её итог в файлах report.md и report.json."
)


def write_report_appendix(w: DocWriter, heading: str, report: ReportDoc) -> None:
    w.heading(heading, level=1, page_break=True, align="right")
    w.para(APPENDIX_SUBTITLE, bold=True, align="center", keep_next=True, first_line=False)
    w.para(APPENDIX_NOTE, first_line=False)
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
