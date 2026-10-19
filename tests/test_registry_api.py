from __future__ import annotations

from app.core.registry_api import (
    AIServiceRecord,
    InMemoryRegistry,
    LaborFunctionRecord,
    LiteratureRecord,
    NormRecord,
    ProfStandardRecord,
    PublisherRecord,
    ToolRecord,
    normalize_text,
)


def make_registry() -> InMemoryRegistry:
    return InMemoryRegistry(
        norms=[NormRecord(id="fz273", title="273-ФЗ", status="verified", source_url="https://x")],
        profstandards=[
            ProfStandardRecord(
                code="06.015",
                title="Специалист по информационным системам",
                labor_functions=[
                    LaborFunctionRecord(otf="C", code="C/11.6", title="Выявление требований к ИС")
                ],
            )
        ],
        ai_services=[AIServiceRecord(id="gigachat", name="GigaChat", aliases=["ГигаЧат"])],
        literature=[LiteratureRecord(id="lit1", title="Python для анализа данных", authors=["Маккини У."])],
        publishers=[PublisherRecord(id="piter", name="Питер", aliases=["Издательский дом «Питер»"])],
        tools=[ToolRecord(id="pandas", name="pandas", min_version="2.2")],
    )


def test_normalize_text() -> None:
    assert normalize_text("  «Питер», Ёж!  ") == "питер еж"


def test_lookups() -> None:
    reg = make_registry()
    assert reg.norm("fz273") is not None and reg.norm("fz273").verified
    assert reg.norm("nope") is None
    assert reg.labor_function("06.015", "C/11.6").title == "Выявление требований к ИС"
    assert reg.labor_function("06.015", "C/14.6") is None
    assert reg.labor_function("99.999", "C/11.6") is None
    assert reg.find_literature("python для анализа данных").id == "lit1"
    assert reg.publisher_known("издательский дом питер")
    assert not reg.publisher_known("Выдуманное издательство")
    assert reg.tool("Pandas").min_version == "2.2"


def test_unverified_record_without_url() -> None:
    record = ProfStandardRecord(code="01.001", title="Педагог", status="verified")
    assert not record.verified  # без source_url запись не считается подтверждённой
