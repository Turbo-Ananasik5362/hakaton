"""Раздел «Отчёт автопроверки» в веб-интерфейсе: тот же ReportDoc, что report.md и приложение в .docx.

Статусы показываются цветом и текстом (значок + слово): цвет не единственный носитель смысла.
Предупреждения методисту — отдельным блоком карточек: что не так, цифры, варианты решения.
Фрагмент (render_report_section) встраивается в страницы интерфейса; render_report_page — отдельная страница.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.models import ReportStatus, WarningType
from app.render.report_md import ReportDoc

TEMPLATES = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

STATUS_VIEW: dict[ReportStatus | None, tuple[str, str, str]] = {
    ReportStatus.ERRORS: ("errors", "✗", "Есть ошибки — исправьте до утверждения"),
    ReportStatus.WARNINGS: ("warnings", "!", "Есть предупреждения — проверьте"),
    ReportStatus.OK: ("ok", "✓", "Ошибок и предупреждений нет"),
    None: ("pending", "…", "Автопроверка не выполнялась"),
}
TONE_ICONS = {"ok": "✓", "warn": "!", "error": "✗"}
# Предупреждения «для сведения»: в программе уже всё сделано, решение нужно только вне её
INFO_WARNINGS = frozenset({WarningType.REFERENCE_ERRORS.value, WarningType.NORM_REPLACED.value})
FILE_TITLES = {
    "program_docx": "Программа (.docx)",
    "slides_pptx": "Слайды (.pptx)",
    "tests_gift": "Тест для Moodle (GIFT)",
    "tests_moodle_xml": "Тест для Moodle (XML)",
    "tests_csv": "Тест таблицей (.csv)",
    "report_md": "Отчёт автопроверки (Markdown)",
    "report_json": "Отчёт автопроверки (JSON)",
    "kit_json": "Комплект целиком (JSON)",
}


def _context(doc: ReportDoc) -> dict[str, object]:
    key, icon, label = STATUS_VIEW.get(doc.status, STATUS_VIEW[None])
    return {
        "doc": doc,
        "status_key": key,
        "status_icon": icon,
        "status_label": label,
        "tone_icons": TONE_ICONS,
        "info_warnings": INFO_WARNINGS,
    }


def render_report_section(doc: ReportDoc) -> str:
    """HTML-фрагмент раздела отчёта — для встраивания в страницы интерфейса."""
    return _env.get_template("_report.html").render(**_context(doc))


def render_report_page(
    doc: ReportDoc | None,
    session_id: str,
    files: Mapping[str, str] | None = None,
    message: str = "",
    refresh_s: int | None = None,
) -> str:
    """Отдельная страница: отчёт и ссылки на файлы; без отчёта (идёт сборка, ошибка) — message и автообновление."""
    downloads = [(FILE_TITLES.get(key, key), url) for key, url in (files or {}).items()]
    context: dict[str, object] = _context(doc) if doc is not None else {"doc": None}
    context.update(session_id=session_id, downloads=downloads, message=message, refresh_s=refresh_s)
    return _env.get_template("report.html").render(**context)
