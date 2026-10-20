"""app/core/bibliography.py: модель предлагает литературу, код проверяет (docs/ARCHITECTURE.md §2.4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import bibliography
from app.core.registry import Registry, default_registry
from app.core.registry_api import LiteratureRecord, PublisherRecord, ToolRecord
from app.core.validate import ValidationContext, validate
from app.models import BookCandidate, CatalogPick, SourceSection, Verification
from tests.validate_kits import make_pk_kit

YEAR = 2026


def small_registry() -> Registry:
    return Registry(
        literature=[
            LiteratureRecord(
                id="py_book",
                authors=["Лутц М."],
                title="Изучаем Python",
                year=2019,
                publisher="Диалектика",
                isbn="978-5-907144-52-1",
                tags=["python"],
                status="verified",
                source_url="https://example.org",
            ),
            LiteratureRecord(
                id="pd_book",
                authors=["Маккини У."],
                title="Python и анализ данных",
                year=2023,
                publisher="ДМК Пресс",
                tags=["python", "pandas", "data"],
                status="verified",
                source_url="https://example.org",
            ),
            LiteratureRecord(
                id="pd_doc",
                kind="doc",
                title="Документация pandas",
                url="https://pandas.pydata.org/docs/",
                tags=["pandas"],
                status="verified",
                source_url="https://pandas.pydata.org/docs/",
            ),
        ],
        publishers=[
            PublisherRecord(id="piter", name="Питер", aliases=["ИД Питер"]),
            PublisherRecord(id="dialektika", name="Диалектика"),
        ],
        tools=[
            ToolRecord(
                id="pandas",
                name="pandas",
                docs_url="https://pandas.pydata.org/docs/",
                min_version="2.0",
                status="verified",
                source_url="https://pandas.pydata.org/docs/",
            )
        ],
    )


# --------------------------------------------------------------------------------------------------
# Проверка кандидатов
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("candidate", "status", "reason"),
    [
        (BookCandidate(authors=["Марк Лутц"], title="Изучаем Python", publisher="Питер"), "catalog", ""),
        (
            BookCandidate(title="Книга", publisher="Питер", isbn="978-5-4461-0000-1", year=2023),
            "rejected",
            "ISBN",
        ),
        (BookCandidate(title="Книга", publisher="Питер", year=2031), "rejected", "не наступил"),
        (BookCandidate(title="Книга", publisher="Питер", year=1999), "rejected", "старое"),
        (BookCandidate(title="Книга", publisher="Рога и копыта", year=2022), "rejected", "издательство"),
        (BookCandidate(title="Книга", year=2022), "rejected", "ни издательства"),
        (BookCandidate(title="Справка", url="https://example.com/x"), "rejected", "официальной документации"),
        (
            BookCandidate(title="Руководство", url="https://pandas.pydata.org/docs/user_guide/"),
            "unverified",
            "",
        ),
        (
            BookCandidate(title="Новая книга", publisher="ИД Питер", year=2024, isbn="978-5-4461-1528-0"),
            "unverified",
            "",
        ),
        (BookCandidate(title=" "), "rejected", "нет названия"),
    ],
)
def test_check_candidate(candidate: BookCandidate, status: str, reason: str) -> None:
    verdict = bibliography.check_candidate(candidate, small_registry(), YEAR)
    assert verdict.status == status, verdict.reasons
    if reason:
        assert any(reason in r for r in verdict.reasons)
    if status == "catalog":
        assert verdict.catalog is not None and verdict.catalog.id == "py_book"


# --------------------------------------------------------------------------------------------------
# Сборка источников
# --------------------------------------------------------------------------------------------------


def _kit_about_pandas():  # noqa: ANN202
    kit = make_pk_kit()
    kit.sources = []
    kit.topics[0].tags = ["python", "pandas"]
    return kit


def test_build_sources_picks_candidates_limits_and_fallback() -> None:
    registry = small_registry()
    kit = _kit_about_pandas()
    items = bibliography.shortlist(kit, registry)
    keys = [i.key for i in items]
    assert (
        "lit:pd_book" in keys and "doc:pandas" in keys
    )  # каталог по тегам + документация упомянутого инструмента
    unverified = [
        BookCandidate(
            title=f"Неизвестная книга {n}", publisher="Питер", year=2024, section=SourceSection.ADDITIONAL
        )
        for n in range(4)
    ]
    result = bibliography.build_sources(
        kit,
        registry,
        items,
        picks=[
            CatalogPick(catalog_id="pd_book", section=SourceSection.MAIN),
            CatalogPick(catalog_id="lit:нет", section=SourceSection.MAIN),
        ],
        candidates=[
            *unverified,
            BookCandidate(title="Фальшивка", publisher="Питер", isbn="978-5-4461-0000-1"),
        ],
        current_year=YEAR,
    )
    sources = result.sources
    by_section = {s: [x for x in sources if x.section == s] for s in SourceSection}
    assert [x.registry_id for x in by_section[SourceSection.MAIN]][:1] == ["pd_book"]
    assert len(by_section[SourceSection.MAIN]) >= 2  # добрано из шорт-листа
    assert (
        sum(1 for x in by_section[SourceSection.ADDITIONAL] if x.unverified) == 2
    )  # не больше двух на подраздел
    assert by_section[SourceSection.INTERNET] and all(
        x.verification == Verification.CATALOG for x in by_section[SourceSection.INTERNET]
    )
    statuses = [row["status"] for row in result.log]
    assert (
        statuses.count("unverified") == 2
        and statuses.count("dropped") == 2
        and statuses.count("rejected") == 1
    )
    assert any("Фальшивка" in note for note in result.notes)
    assert len({s.id for s in sources}) == len(sources)


def test_sources_from_catalog_pass_v23() -> None:
    """Источники из каталога и документация из tools.json проходят проверку V23 без ошибок."""
    registry = default_registry()
    kit = _kit_about_pandas()
    items = bibliography.shortlist(kit, registry)
    result = bibliography.build_sources(kit, registry, items, [], [], YEAR)
    kit.sources = result.sources
    report = validate(kit, ValidationContext(registry=registry, current_year=YEAR), only=["V23"])
    assert result.sources and not [i for i in report.issues if i.severity.value == "error"]


def test_log_unverified(tmp_path: Path) -> None:
    kit = make_pk_kit()
    path = tmp_path / "logs" / "unverified_sources.jsonl"
    bibliography.log_unverified(
        path, kit, [{"title": "X", "status": "rejected", "reasons": ["ISBN"]}], "2026-10-20T10:00:00+03:00"
    )
    bibliography.log_unverified(path, kit, [], "ignored")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        {
            "ts": "2026-10-20T10:00:00+03:00",
            "program": kit.program.title,
            "title": "X",
            "status": "rejected",
            "reasons": ["ISBN"],
        }
    ]


def test_ru_services_only_excludes_blocked_service_titles() -> None:
    """«Только сервисы России»: книга про недоступный сервис не попадает в шорт-лист, кандидат — отбрасывается."""
    from app.core.registry_api import AIServiceRecord

    registry = small_registry()
    registry._literature.append(  # noqa: SLF001 — тестовый каталог
        LiteratureRecord(
            id="gpt_book",
            title="Как устроен ChatGPT",
            year=2024,
            publisher="Питер",
            tags=["python", "pandas"],
        )
    )
    registry._ai_services = [AIServiceRecord(id="chatgpt", name="ChatGPT", available_in_ru=False)]  # noqa: SLF001
    kit = _kit_about_pandas()
    assert "lit:gpt_book" in [i.key for i in bibliography.shortlist(kit, registry)]
    kit.request.constraints.ru_services_only = True
    items = bibliography.shortlist(kit, registry)
    assert "lit:gpt_book" not in [i.key for i in items]
    result = bibliography.build_sources(
        kit, registry, items, [], [BookCandidate(title="Уроки с ChatGPT", publisher="Питер", year=2024)], YEAR
    )
    assert result.log[0]["status"] == "rejected" and "ChatGPT" in result.log[0]["reasons"][0]


def test_extra_main_books_move_to_additional() -> None:
    registry = small_registry()
    kit = _kit_about_pandas()
    items = bibliography.shortlist(kit, registry)
    original = dict(bibliography.MAX_PER_SECTION)
    bibliography.MAX_PER_SECTION[SourceSection.MAIN] = 1
    try:
        picks = [CatalogPick(catalog_id=i.key, section=SourceSection.MAIN) for i in items if not i.is_doc]
        result = bibliography.build_sources(kit, registry, items, picks, [], YEAR)
    finally:
        bibliography.MAX_PER_SECTION.update(original)
    sections = [s.section for s in result.sources if s.kind.value == "book"]
    assert sections.count(SourceSection.MAIN) == 1 and SourceSection.ADDITIONAL in sections


def test_unverified_catalog_record_is_marked() -> None:
    """Запись каталога без проверки (status ≠ verified) выводится с меткой, а не как проверенная."""
    record = LiteratureRecord(id="raw_book", kind="book", title="Непроверенная книга", tags=["pandas"])
    source = bibliography._source_from_item(
        bibliography.ShortlistItem(key="lit:raw_book", record=record), SourceSection.MAIN, "SRC1", []
    )
    assert source.verification == Verification.UNVERIFIED
