"""scripts/check_registry.py: формальная проверка реестра ловит битые записи на заведомо ошибочных примерах."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from scripts.check_registry import check_registry, isbn_valid

TODAY = date(2026, 10, 19)


def write(directory: Path, filename: str, items: list[dict]) -> None:
    (directory / filename).write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize(
    ("isbn", "valid"),
    [
        ("978-5-4461-0923-4", True),  # ISBN-13
        ("9785446109234", True),
        ("978-5-4461-0923-5", False),
        ("0-306-40615-2", True),  # ISBN-10
        ("0-8044-2957-X", True),
        ("0-306-40615-3", False),
        ("978-5-4461", False),
        ("", False),
    ],
)
def test_isbn_valid(isbn: str, valid: bool) -> None:
    assert isbn_valid(isbn) is valid


def test_clean_registry_passes(tmp_path: Path) -> None:
    write(tmp_path, "publishers.json", [{"id": "piter", "name": "Питер", "aliases": ["ИД Питер"]}])
    write(
        tmp_path,
        "literature.json",
        [
            {
                "id": "py_book",
                "authors": ["Автор А."],
                "title": "Книга",
                "year": 2023,
                "publisher": "ИД Питер",
                "isbn": "978-5-4461-0923-4",
                "tags": ["python"],
                "status": "verified",
                "source_url": "https://example.org/book",
                "checked_at": "2026-10-19",
            }
        ],
    )
    result = check_registry(tmp_path, TODAY)
    assert result.ok, result.errors
    assert result.stats["literature_verified"] == 1


def test_broken_records_are_reported(tmp_path: Path) -> None:
    write(
        tmp_path,
        "norms.json",
        [
            {
                "id": "a",
                "kind": "order",
                "title": "Приказ",
                "applies_to": ["pk"],
                "replaced_by": "нет_такого",
            },
            {"id": "b", "kind": "резолюция", "title": "Акт", "applies_to": ["pk"], "status": "verified"},
        ],
    )
    write(
        tmp_path,
        "profstandards.json",
        [
            {
                "code": "6.15",
                "title": "ПС",
                "labor_functions": [
                    {"otf": "C", "code": "С/11.6", "title": "кириллическая С"},
                    {"otf": "B", "code": "C/12.6", "title": "ОТФ не та"},
                ],
                "tags": ["Информационная безопасность"],
            }
        ],
    )
    write(
        tmp_path,
        "ru_ai_services.json",
        [
            {"id": "one", "name": "Сервис", "aliases": ["Servis"]},
            {"id": "two", "name": "Другой", "aliases": ["servis"]},
        ],
    )
    write(tmp_path, "tools.json", [{"id": "py", "name": "Python", "min_version": "3.10+"}])
    write(
        tmp_path,
        "literature.json",
        [
            {
                "id": "bad",
                "title": "Книга",
                "year": 2031,
                "publisher": "Неизвестное",
                "isbn": "978-5-4461-0923-5",
                "tags": ["кулинария"],
                "checked_at": "2026-12-01",
            }
        ],
    )
    result = check_registry(tmp_path, TODAY)
    text = "\n".join(result.errors)
    for expected in (
        "replaced_by → 'нет_такого'",
        "kind 'резолюция'",
        "verified без source_url",
        "код ПС не вида",
        "код ТФ 'С/11.6'",
        "ОТФ 'B'",
        "тег 'Информационная безопасность'",
        "имя 'servis' уже есть у one",
        "min_version '3.10+'",
        "нет тега направления",
        "год 2031",
        "издательства 'Неизвестное' нет",
        "неверная контрольная сумма",
        "checked_at из будущего",
    ):
        assert expected in text, expected
    assert not result.ok


def test_reference_only_norm_with_empty_applies_to(tmp_path: Path) -> None:
    """Справочная запись (152-ФЗ): applies_to пуст — не ошибка, если есть пояснение; без него — предупреждение."""
    base = {
        "kind": "federal_law",
        "title": "Закон",
        "status": "verified",
        "source_url": "https://example.org",
        "checked_at": "2026-01-15",
    }
    write(
        tmp_path,
        "norms.json",
        [
            {**base, "id": "ref", "applies_to": [], "note": "справочная запись"},
            {**base, "id": "bare", "applies_to": []},
            {**base, "id": "bad", "applies_to": ["school"]},
        ],
    )
    result = check_registry(tmp_path, TODAY)
    assert not any("/ref" in e for e in result.errors + result.warnings)
    assert any("/bare" in w and "applies_to пуст" in w for w in result.warnings)
    assert any("/bad" in e for e in result.errors)


def test_real_registry_recognizes_personal_data_law() -> None:
    from app.core.registry import default_registry
    from app.core.validate import mark_unknown_codes

    registry = default_registry()
    assert registry.norm("fz152") is not None
    text = "Обезличивание данных по Федеральному закону № 152-ФЗ."
    assert mark_unknown_codes(text, registry) == text
    for ptype in ("pk", "pk_internship", "dop"):  # в нормативную базу программ не включается сам
        assert "fz152" not in [n.id for n in registry.norms_for(ptype, dot=True, include_letters=True)]
