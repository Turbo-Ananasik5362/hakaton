"""Шаг intake через настоящий клиент LLM в режиме mock (ответы модели — tests/fixtures/llm/intake/)."""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

import pytest

from app.config import Settings
from app.llm import LLMClient
from app.models import FAForm, IntakeDraft, ProgramType, QuantityMention, RequestSpec, StudyFormKind
from app.pipeline.base import Attachment, RequestInput, StepContext
from app.pipeline.intake import build_spec, read_attachments, request_key, run_intake
from app.pipeline.request_rules import extract_facts
from tests.intake_queries import OPEN, OWN, ROOT

FIXTURES = ROOT / "tests" / "fixtures" / "llm"
TEMPLATES = ROOT / "data" / "templates"


def mock_llm() -> LLMClient:
    return LLMClient(Settings(llm_mode="mock", fixtures_dir=FIXTURES))


def intake(
    text: str, tmp_path: Path, files: tuple[Path, ...] = (), llm: LLMClient | None = None
) -> RequestSpec:
    attachments = []
    for n, source in enumerate(files, start=1):
        stored = f"{n:02d}_{source.name}"
        shutil.copy(source, tmp_path / stored)
        attachments.append(Attachment(filename=source.name, stored_as=stored, size=source.stat().st_size))
    request = RequestInput(text=text, attachments=attachments)
    ctx = StepContext(session_id="s_test", inputs_dir=tmp_path, files_dir=tmp_path, request=request)
    return asyncio.run(run_intake(request, ctx, llm if llm is not None else mock_llm()))


def test_every_query_has_its_mock_answer() -> None:
    """Поменяли текст запроса — перезапустите tests/fixtures/llm/make_intake_fixtures.py."""
    texts = [q["request"] for q in OPEN.values()] + list(OWN.values())
    missing = [t[:50] for t in texts if not (FIXTURES / "intake" / f"{request_key(t)}.json").is_file()]
    assert not missing, missing


# ---- открытые запросы ------------------------------------------------------------------------------


def test_open_1_school_python(tmp_path: Path) -> None:
    spec = intake(OPEN[1]["request"], tmp_path, (TEMPLATES / "dop_intensiv_ai_24h.docx",))
    assert spec.program_type == ProgramType.DOP and spec.total_hours == 16
    assert (spec.session_hours, spec.sessions_per_week) == (2, 2)
    assert spec.constraints.min_practice_share == 0.5
    assert spec.outputs.test_questions == 10
    assert spec.outputs.slides_scope.mode == "sessions" and spec.outputs.slides_scope.session_numbers == [
        1,
        2,
    ]
    assert spec.fa_form == FAForm.PROJECT_DEFENSE and "защищает" in (spec.fa_text or "")
    assert spec.audience.age_or_grade == "8–9 классы" and spec.audience.entry_level
    assert spec.title == "Первые шаги в Python"
    assert spec.reference is not None  # приложен шаблон — разобран кодом
    assert spec.missing == []


def test_open_2_teachers_ai(tmp_path: Path) -> None:
    spec = intake(OPEN[2]["request"], tmp_path)
    assert spec.program_type == ProgramType.PK and spec.total_hours == 36
    assert spec.form is not None and spec.form.kind == StudyFormKind.OCHNO_ZAOCHNAYA and spec.form.dot
    assert spec.constraints.ru_services_only
    assert spec.fa_form == FAForm.PORTFOLIO and spec.portfolio_items_count == 3
    assert [t.label for t in spec.required_topics] == ["риски и безопасное использование ИИ"]
    # «задания для учеников» в запросе дословно нет — такое требование модели не принимается
    assert [t.label for t in spec.constraints.required_seminar_types] == ["план урока", "проверочная работа"]
    assert spec.audience.domain_patterns and spec.audience.entry_level is None
    assert spec.missing == ["audience"]


def test_open_3_bank_data_with_reference(tmp_path: Path) -> None:
    spec = intake(OPEN[3]["request"], tmp_path, (TEMPLATES / "pk_stazhirovka_bsa_248h.docx",))
    assert spec.program_type == ProgramType.PK and spec.total_hours == 72 and spec.weeks == 4
    assert [t.label for t in spec.required_topics] == [
        "pandas",
        "визуализация",
        "SQL для выгрузки данных",
        "основы статистики",
    ]
    assert re.search(spec.required_topics[2].patterns[0], "Выгрузка данных на SQL", re.I)
    assert spec.constraints.intro_block_topic == "основы Python"
    assert spec.constraints.anonymized_data and spec.fa_form == FAForm.PROJECT_DEFENSE
    scope = spec.outputs.slides_scope
    assert scope.mode == "module" and re.search(scope.module_pattern or "", "Обработка данных в pandas", re.I)
    assert spec.reference is not None and spec.reference.hour_tables
    assert spec.missing == []


def test_open_4_incomplete_request_does_not_trust_model_guesses(tmp_path: Path) -> None:
    spec = intake(OPEN[4]["request"], tmp_path)
    assert spec.total_hours is None and spec.weeks == 4 and spec.duration_text == "примерно на месяц"
    # модель додумала уровень, форму и ИА — код этого в запросе не нашёл
    assert spec.audience.entry_level is None
    assert spec.missing == ["program_type", "hours", "audience", "form", "final_attestation"]


def test_open_5_internship_with_sample(tmp_path: Path) -> None:
    spec = intake(OPEN[5]["request"], tmp_path, (TEMPLATES / "pk_stazhirovka_bsa_248h.docx",))
    assert spec.program_type == ProgramType.PK_INTERNSHIP and spec.total_hours == 256
    assert spec.fa_hours == 2 and spec.fa_form == FAForm.REPORT_DEFENSE
    assert spec.hours_per_day_max == 4 and spec.weeks == 9 and spec.duration_text == "около двух месяцев"
    assert spec.constraints.all_srs
    assert [t.label for t in spec.required_topics] == ["Linux", "CI/CD"]
    assert spec.reference is not None and spec.reference.requisites_filled  # образец с ошибками — для V45
    assert spec.missing == ["audience", "form"]


# ---- свои запросы других направлений ---------------------------------------------------------------


def test_own_medical_security(tmp_path: Path) -> None:
    spec = intake(OWN["med_security"], tmp_path)
    assert (spec.program_type, spec.total_hours, spec.fa_form) == (ProgramType.PK, 24, FAForm.TEST)
    assert spec.form is not None and spec.form.kind == StudyFormKind.ZAOCHNAYA and spec.form.dot


def test_own_arduino_kids(tmp_path: Path) -> None:
    spec = intake(OWN["arduino_kids"], tmp_path)
    assert (spec.program_type, spec.total_hours, spec.session_hours, spec.sessions_per_week) == (
        ProgramType.DOP,
        36,
        2,
        1,
    )
    assert spec.audience.age_or_grade == "10–12 лет"
    assert (
        "final_attestation" in spec.missing
    )  # «выставка проектов» — не из списка форм, решит clarify/умолчание
    assert spec.fa_form == FAForm.PROJECT_DEFENSE  # догадка модели сохранена как вариант по умолчанию


def test_own_accounting_type_from_audience(tmp_path: Path) -> None:
    spec = intake(OWN["accounting_1c"], tmp_path)
    assert spec.program_type == ProgramType.PK and "program_type" in spec.missing
    assert spec.total_hours == 40 and spec.fa_form == FAForm.EXAM_PRACTICAL
    assert any("Название" in a for a in spec.assumptions)  # названия в кавычках не было


def test_own_sql_economists_conflict(tmp_path: Path) -> None:
    spec = intake(OWN["sql_economists"], tmp_path)
    assert spec.total_hours == 16 and spec.weeks == 13
    assert "conflict_load" in spec.missing


def test_own_sales_incomplete(tmp_path: Path) -> None:
    spec = intake(OWN["sales_ai"], tmp_path)
    assert spec.total_hours is None and spec.weeks == 2
    assert spec.outputs.slides_scope.mode == "default"  # «презентации» тут — тема курса, а не заказ слайдов
    assert spec.missing == ["program_type", "hours", "audience", "form", "final_attestation"]


# ---- вложения и сбои модели ------------------------------------------------------------------------


def test_txt_attachment_is_part_of_request(tmp_path: Path) -> None:
    letter = tmp_path / "zapros.txt"
    letter.write_text(OPEN[2]["request"], encoding="cp1251")
    spec = intake("", tmp_path, (letter,))
    assert spec.program_type == ProgramType.PK and spec.total_hours == 36
    assert spec.raw_text.startswith("Программа повышения квалификации")


def test_reference_goes_to_model_as_outline_only(tmp_path: Path) -> None:
    source = TEMPLATES / "pk_stazhirovka_bsa_248h.docx"
    shutil.copy(source, tmp_path / "01_ref.docx")
    request = RequestInput(
        text="x", attachments=[Attachment(filename="ref.docx", stored_as="01_ref.docx", size=1)]
    )
    digest = read_attachments(request, StepContext("s_test", tmp_path, tmp_path, request))
    assert digest.reference is not None
    [summary] = digest.summaries
    assert "Оглавление" in summary and "Учебный план" in summary
    assert "17-ОБР" not in summary  # реквизиты и текст образца в промпт не уходят


def test_model_unavailable_falls_back_to_rules(tmp_path: Path) -> None:
    replay = LLMClient(
        Settings(llm_mode="replay", cache_dir=tmp_path / "cache", cost_log=tmp_path / "cost.jsonl")
    )
    spec = intake(OPEN[1]["request"], tmp_path, llm=replay)
    assert spec.program_type == ProgramType.DOP and spec.total_hours == 16 and spec.session_hours == 2
    assert any("только по правилам" in a for a in spec.assumptions)


def test_unknown_request_uses_default_mock_answer(tmp_path: Path) -> None:
    spec = intake(
        "Программа повышения квалификации по Kubernetes для инженеров, 48 часов, очно, экзамен.", tmp_path
    )
    assert (spec.program_type, spec.total_hours, spec.fa_form) == (ProgramType.PK, 48, FAForm.EXAM_PRACTICAL)
    assert spec.missing == ["audience"]


# ---- сверка цитат модели ---------------------------------------------------------------------------


def _draft(*quotes: tuple[str, str]) -> IntakeDraft:
    return IntakeDraft(quantities=[QuantityMention(what=w, text=t) for w, t in quotes])  # type: ignore[arg-type]


def test_model_quote_fills_number_code_missed() -> None:
    text = "Программа повышения квалификации. Трудоемкость: 72 часа."
    facts = extract_facts(text)
    facts.total_hours, facts.total_candidates = None, []  # как будто правило не сработало
    spec = build_spec(text, facts, _draft(("total_hours", "72 часа")), None)
    assert spec.total_hours == 72 and any("прочитан из фразы" in a for a in spec.assumptions)


def test_model_quote_not_in_text_is_ignored() -> None:
    text = "Программа повышения квалификации для инженеров."
    spec = build_spec(text, extract_facts(text), _draft(("total_hours", "на 100 часов")), None)
    assert spec.total_hours is None and "hours" in spec.missing


def test_model_quote_with_other_number_is_a_conflict() -> None:
    text = "Программа повышения квалификации на 36 часов. Слушатели просили 72 часа."
    facts = extract_facts(text)
    assert facts.total_candidates == [36, 72]
    spec = build_spec(text, facts, _draft(("total_hours", "на 36 часов")), None)
    assert spec.total_hours is None and "conflict_hours" in spec.missing


def test_model_cannot_relabel_practice_hours_as_total() -> None:
    text = "Программа повышения квалификации на 72 часа, из них 36 часов практики."
    spec = build_spec(text, extract_facts(text), _draft(("total_hours", "36 часов")), None)
    assert spec.total_hours == 72 and "conflict_hours" not in spec.missing


@pytest.mark.parametrize("what", ["weeks", "test_questions"])
def test_model_quote_for_other_quantities(what: str) -> None:
    text = "Программа повышения квалификации, 36 часов."
    quote = {"weeks": "36 часов", "test_questions": "36 часов"}[what]
    spec = build_spec(text, extract_facts(text), _draft((what, quote)), None)
    # цитата есть, но это часы, а не недели/вопросы: правило уже отнесло число к объёму — значение не меняется
    assert spec.total_hours == 36


def test_model_guess_of_form_is_not_taken(tmp_path: Path) -> None:
    spec = intake(OPEN[4]["request"], tmp_path)  # в mock-ответе модель «угадала» очную форму
    assert spec.form is None and "form" in spec.missing


def test_internship_has_no_seminar_requirements() -> None:
    text = "Программа повышения квалификации в форме стажировки на 120 часов, защита отчёта о стажировке."
    draft = IntakeDraft(required_seminar_types=["отчёт о стажировке"])
    spec = build_spec(text, extract_facts(text), draft, None)
    assert spec.program_type == ProgramType.PK_INTERNSHIP and spec.constraints.required_seminar_types == []
