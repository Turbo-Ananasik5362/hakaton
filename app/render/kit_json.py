"""Выгрузка комплекта и отчёта самопроверки в JSON."""

from __future__ import annotations

from pathlib import Path

from app.models import Kit

# Разобранный образец (request.reference) в выгрузку не идёт: в нём реквизиты приказа и дословный текст чужой
# программы (жёсткие правила 4 и 5). Во внутреннем kit.json сессии он остаётся — по нему работают проверки.
EXPORT_EXCLUDE: dict = {"request": {"reference"}}


def kit_export(kit: Kit) -> dict:
    """Комплект для выгрузки и ответов API."""
    return kit.model_dump(mode="json", exclude=EXPORT_EXCLUDE)


def render_kit_json(kit: Kit, out_dir: Path) -> dict[str, str]:
    """kit.json (весь комплект) и report.json (отчёт проверки, если есть). Возвращает «ключ → имя файла»."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "kit.json").write_text(kit.model_dump_json(indent=2, exclude=EXPORT_EXCLUDE), encoding="utf-8")
    files = {"kit_json": "kit.json"}
    if kit.report is not None:
        (out_dir / "report.json").write_text(kit.report.model_dump_json(indent=2), encoding="utf-8")
        files["report_json"] = "report.json"
    return files
