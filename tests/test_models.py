from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from app import models
from app.models import HoursRow, Kit, OutcomeKind, ProgramType, ReportStatus, Severity, ValidationReport
from tests.kit_factory import make_small_kit


def test_hours_row_computed_fields() -> None:
    row = HoursRow(lk=2, pr=3.5, lb=1, srs=4, distance=2)
    assert row.contact == 6.5
    assert row.in_person == 4.5
    assert row.total == 10.5


def test_hours_row_sum_and_add() -> None:
    total = HoursRow.sum([HoursRow(lk=1, pr=0.5), HoursRow(srs=2, distance=0.5)])
    assert (total.lk, total.pr, total.srs, total.distance, total.total) == (1, 0.5, 2, 0.5, 3.5)


def test_hours_negative_rejected() -> None:
    with pytest.raises(ValidationError):
        HoursRow(lk=-1)


def test_kit_totals_are_computed() -> None:
    kit = make_small_kit()
    assert kit.module_hours("M1").total == 10
    assert kit.module_hours("M2").total == 10
    assert kit.program_hours().total == 20
    assert kit.total_hours() == 22  # 20 + ИА 2
    assert kit.hours_by_module["M1"].pr == 5


def test_json_roundtrip_ignores_computed_fields() -> None:
    kit = make_small_kit()
    dumped = kit.model_dump(mode="json")
    assert dumped["computed_total_hours"] == 22
    assert dumped["topics"][0]["hours"]["total"] == 5
    assert dumped["program"]["document"].startswith("Удостоверение")
    # computed-поля при загрузке игнорируются, комплект восстанавливается без потерь
    restored = Kit.model_validate_json(kit.model_dump_json())
    assert restored == kit


def test_tampered_total_in_json_has_no_effect() -> None:
    """Подделанное «Итого» в JSON не может разойтись с суммой: оно просто вычисляется заново."""
    kit = make_small_kit()
    data = kit.model_dump(mode="json")
    data["computed_total_hours"] = 999
    data["topics"][0]["hours"]["total"] = 999
    restored = Kit.model_validate(data)
    assert restored.total_hours() == 22
    assert restored.topics[0].hours.total == 5


def test_document_by_program_type() -> None:
    assert make_small_kit(ProgramType.DOP).program.document == "Сертификат об освоении программы"
    assert "повышении квалификации" in make_small_kit(ProgramType.PK_INTERNSHIP).program.document


def test_id_formats_validated() -> None:
    with pytest.raises(ValidationError):
        models.Topic(id="2.3", module_id="M2", order=1, title="x")
    with pytest.raises(ValidationError):
        models.Criterion(id="K1", text="x", max_points=1)


def test_competency_kun() -> None:
    kun = make_small_kit().competency_kun("C1")
    assert kun[OutcomeKind.KNOW] == ["Знать типы данных"]
    assert kun[OutcomeKind.CAN] == ["Уметь готовить отчёт"]
    assert kun[OutcomeKind.MASTER] == []


def test_source_citation_and_flag() -> None:
    kit = make_small_kit()
    verified, unverified = kit.sources
    assert not verified.unverified
    assert verified.citation == "Иванов И. И. Анализ данных. — Питер, 2024."
    assert unverified.citation.endswith("[не подтверждено]")


def test_report_status() -> None:
    issue = models.Issue(id="V02-1", check="V02", severity=Severity.ERROR, message="Не сходится итог")
    assert ValidationReport(issues=[issue]).status == ReportStatus.ERRORS
    assert ValidationReport(issues=[issue.model_copy(update={"fixed": True})]).status == ReportStatus.OK
    warn = issue.model_copy(update={"severity": Severity.WARNING})
    assert ValidationReport(issues=[warn]).status == ReportStatus.WARNINGS


def _draft_classes() -> list[type[BaseModel]]:
    return [
        obj
        for obj in vars(models).values()
        if isinstance(obj, type) and issubclass(obj, models.DraftModel) and obj is not models.DraftModel
    ]


def test_drafts_have_no_hours_fields() -> None:
    """Ни один черновик LLM не несёт часов: ни поля «*hours*», ни вложенного HoursRow."""
    drafts = _draft_classes()
    assert len(drafts) >= 15
    for cls in drafts:
        for name, field in cls.model_fields.items():
            assert "hour" not in name.lower(), f"{cls.__name__}.{name}"
            assert "HoursRow" not in str(field.annotation), f"{cls.__name__}.{name}"


def test_draft_json_schemas_build() -> None:
    for cls in _draft_classes():
        schema = cls.model_json_schema()
        assert schema["type"] == "object"
