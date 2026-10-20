"""Программа в .docx: выбор шаблона по типу, рендер и проверка V31. Сбой здесь не лишает методиста комплекта:
app/render/bundle.py ловит исключение, пишет понятную заметку и выдаёт остальные файлы и JSON."""

from __future__ import annotations

from pathlib import Path

from app.core.registry import Registry
from app.models import Issue, Kit
from app.render import docx_dop, docx_pk
from app.render.common import Organization
from app.render.report_md import ReportDoc, build_report
from app.render.tables import PK_TYPES, build_tables
from app.render.verify import verify_docx

DOCX_NAME = "program.docx"


def render_program_docx(
    kit: Kit, out_dir: Path, registry: Registry, org: Organization, report: ReportDoc | None = None
) -> tuple[str, list[Issue]]:
    """Пишет out_dir/program.docx (последнее приложение — отчёт автопроверки). Возвращает (имя файла, замечания V31)."""
    tables = build_tables(kit)
    report = report or build_report(kit, for_docx=True)
    path = out_dir / DOCX_NAME
    if kit.program.type in PK_TYPES:
        docx_pk.render_pk(kit, tables, registry, org, path, report)
        headings = docx_pk.HEADINGS
    else:
        docx_dop.render_dop(kit, tables, registry, org, path, report)
        headings = docx_dop.HEADINGS
    return DOCX_NAME, verify_docx(path, kit, headings)
