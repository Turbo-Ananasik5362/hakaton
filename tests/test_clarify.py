"""Шаг clarify: когда спрашивать, какие вопросы, как применяются ответы и умолчания."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from app.config import Settings
from app.llm import LLMClient
from app.models import ClarifyQuestion, FAForm, ProgramType, RequestSpec, StudyFormKind
from app.pipeline.base import RequestInput, StepContext
from app.pipeline.clarify import (
    MAX_QUESTIONS,
    apply_answers,
    finalize_spec,
    needs_questions,
    run_clarify,
)
from app.pipeline.intake import run_intake
from tests.intake_queries import OPEN, OWN, ROOT

FIXTURES = ROOT / "tests" / "fixtures" / "llm"
JARGON = re.compile(
    r"\bДОТ\b|\bИА\b|\bРО\b|\bСРС\b|\bЛК\b|\bПР\b|\bУТП\b|json|skeleton|intake", re.IGNORECASE
)


def mock_llm() -> LLMClient:
    return LLMClient(Settings(llm_mode="mock", fixtures_dir=FIXTURES))


def spec_for(text: str, tmp_path: Path) -> RequestSpec:
    request = RequestInput(text=text)
    ctx = StepContext("s_test", tmp_path, tmp_path, request)
    return asyncio.run(run_intake(request, ctx, mock_llm()))


def ask(spec: RequestSpec, llm: LLMClient | None = None, round_no: int = 0) -> list[ClarifyQuestion]:
    return asyncio.run(run_clarify(spec, None, round_no, llm if llm is not None else mock_llm()))


# ---- открытые запросы: вопросы только к запросу 4 --------------------------------------------------


@pytest.mark.parametrize("query_id", [1, 2, 3, 5])
def test_complete_open_queries_get_no_questions(query_id: int, tmp_path: Path) -> None:
    spec = spec_for(OPEN[query_id]["request"], tmp_path)
    assert not needs_questions(spec)
    assert ask(spec) == []


def test_open_4_asks_about_everything_that_matters(tmp_path: Path) -> None:
    questions = ask(spec_for(OPEN[4]["request"], tmp_path))
    expected = OPEN[4]["expected"]
    assert [q.id for q in questions] == expected["must_ask_about"]
    assert len(questions) <= expected["max_questions"] == MAX_QUESTIONS
    for question in questions:
        assert question.text.endswith("?") or "?" in question.text
        assert len(question.options) >= 2
        assert not JARGON.search(question.text + " ".join(question.options)), question
    hours = next(q for q in questions if q.id == "hours")
    assert "примерно на месяц" in hours.text and "в часах" in hours.text  # срок — не объём
    assert "72 часа" in hours.options


def test_open_4_answers_lead_to_pk_72(tmp_path: Path) -> None:
    spec = spec_for(OPEN[4]["request"], tmp_path)
    answers = {k: v for k, v in OPEN[4]["methodist_answers"].items()}
    answered = apply_answers(spec, answers)
    assert answered.program_type == ProgramType.PK and answered.total_hours == 72
    assert answered.form is not None and answered.form.kind == StudyFormKind.OCHNAYA
    assert answered.fa_form == FAForm.EXAM_PRACTICAL and "стенде" in (answered.fa_text or "")
    assert "знают основы сетей" in (answered.audience.entry_level or "")
    assert answered.audience.category == "студенты колледжа"
    assert answered.missing == [] and not needs_questions(answered)
    assert ask(answered, round_no=1) == []
    final = finalize_spec(answered)
    defaults = re.compile(
        r"^(Тип программы|Объём|Входной уровень слушателей|Форма обучения|Форма итоговой аттестации) "
    )
    assert final.missing == [] and not [a for a in final.assumptions if defaults.match(a)]


def test_option_labels_are_valid_answers(tmp_path: Path) -> None:
    """Методист может просто выбрать вариант — каждый вариант понимается кодом."""
    spec = spec_for(OPEN[4]["request"], tmp_path)
    for question in ask(spec):
        for option in question.options:
            answered = apply_answers(spec, {question.id: option})
            assert question.id not in answered.missing, (question.id, option)


# ---- свои запросы других направлений ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["med_security", "arduino_kids", "accounting_1c"])
def test_complete_own_queries_get_no_questions(name: str, tmp_path: Path) -> None:
    assert ask(spec_for(OWN[name], tmp_path)) == []


def test_own_incomplete_request_gets_same_five_questions(tmp_path: Path) -> None:
    questions = ask(spec_for(OWN["sales_ai"], tmp_path))
    assert [q.id for q in questions] == ["program_type", "hours", "audience", "form", "final_attestation"]
    assert "сотрудники отдела продаж" in questions[2].text
    assert "2 недели" in questions[1].text


def test_contradiction_is_asked_first_with_model_wording(tmp_path: Path) -> None:
    questions = ask(spec_for(OWN["sql_economists"], tmp_path))
    assert [q.id for q in questions] == ["conflict_load", "audience", "form"]
    assert "16 часов и срок 3 месяца" in questions[0].text  # формулировка модели (mock)


def test_contradiction_falls_back_to_template_without_model(tmp_path: Path) -> None:
    spec = spec_for(OWN["sql_economists"], tmp_path)
    replay = LLMClient(
        Settings(llm_mode="replay", cache_dir=tmp_path / "cache", cost_log=tmp_path / "c.jsonl")
    )
    [conflict, *_] = ask(spec, llm=replay)
    assert conflict.id == "conflict_load" and "в неделю" in conflict.text and len(conflict.options) == 2


def test_contradiction_answer_is_not_asked_again(tmp_path: Path) -> None:
    spec = spec_for(OWN["sql_economists"], tmp_path)
    answered = apply_answers(spec, {"conflict_load": "Объём 16 часов, срок можно изменить"})
    assert answered.weeks is None and "conflict_load" not in answered.missing
    assert not needs_questions(answered)


def test_contradiction_answer_with_new_volume(tmp_path: Path) -> None:
    spec = spec_for(OWN["sql_economists"], tmp_path)
    answered = apply_answers(spec, {"conflict_load": "Давайте 72 часа"})
    assert answered.total_hours == 72 and "conflict_load" not in answered.missing


# ---- шаблонные противоречия ------------------------------------------------------------------------


def test_several_volumes_question_offers_both(tmp_path: Path) -> None:
    spec = spec_for(
        "Программа повышения квалификации для инженеров на 36 часов, очно, экзамен. Всего 72 часа.", tmp_path
    )
    [question] = [q for q in ask(spec) if q.id == "conflict_hours"]
    assert question.options == ["36 часов", "72 часа"]
    assert not any(q.id == "hours" for q in ask(spec))  # вопрос о противоречии заменяет вопрос об объёме
    assert apply_answers(spec, {"conflict_hours": "72 часа"}).total_hours == 72


def test_sessions_do_not_divide_volume(tmp_path: Path) -> None:
    spec = spec_for(
        "Общеразвивающая программа для школьников 5 классов, 15 часов, занятия по 2 часа.", tmp_path
    )
    [question] = [q for q in ask(spec) if q.id == "conflict_sessions"]
    assert question.options == ["14 часов — 7 занятий", "16 часов — 8 занятий"]
    answered = apply_answers(spec, {"conflict_sessions": question.options[1]})
    assert answered.total_hours == 16 and "conflict_sessions" not in answered.missing


def test_pk_for_schoolchildren_is_questioned(tmp_path: Path) -> None:
    spec = spec_for("Программа повышения квалификации для школьников 7 класса на 24 часа.", tmp_path)
    questions = ask(spec)
    assert questions[0].id == "conflict_type" and "школьники" in questions[0].text
    assert not any(q.id == "program_type" for q in questions)
    answered = apply_answers(spec, {"conflict_type": "Общеразвивающая программа"})
    assert answered.program_type == ProgramType.DOP and "program_type" not in answered.missing


# ---- ответы ----------------------------------------------------------------------------------------


def test_deferred_answer_becomes_assumption(tmp_path: Path) -> None:
    spec = spec_for(OPEN[4]["request"], tmp_path)
    answered = apply_answers(spec, {"hours": "72", "program_type": "ПК", "form": "на ваше усмотрение"})
    assert "form" not in answered.missing
    assert answered.form is not None and answered.form.kind == StudyFormKind.OCHNAYA
    assert any("оставлена на усмотрение" in a for a in answered.assumptions)


def test_unparsable_answer_keeps_question(tmp_path: Path) -> None:
    spec = spec_for(OPEN[4]["request"], tmp_path)
    answered = apply_answers(spec, {"program_type": "что-нибудь", "hours": "много"})
    assert {"program_type", "hours"} <= set(answered.missing)


def test_apply_answers_is_idempotent(tmp_path: Path) -> None:
    spec = spec_for(OPEN[4]["request"], tmp_path)
    answers = dict(OPEN[4]["methodist_answers"])
    once = apply_answers(spec, answers)
    assert apply_answers(spec, answers) == once


def test_unknown_answer_key_is_kept_as_note(tmp_path: Path) -> None:
    spec = spec_for(OPEN[2]["request"], tmp_path)
    answered = apply_answers(spec, {"сервисы": "GigaChat и Шедеврум"})
    assert any("GigaChat и Шедеврум" in a for a in answered.assumptions)


def test_answer_creating_new_contradiction_is_asked(tmp_path: Path) -> None:
    spec = spec_for(OPEN[4]["request"], tmp_path)  # срок ≈ 4 недели
    answered = apply_answers(spec, {"hours": "500 часов"})
    assert "conflict_load" in answered.missing  # 500 ч за 4 недели — больше 48 ч в неделю
    assert needs_questions(answered)


# ---- умолчания -------------------------------------------------------------------------------------


def test_finalize_fills_defaults_with_readable_notes(tmp_path: Path) -> None:
    final = finalize_spec(spec_for(OPEN[5]["request"], tmp_path))
    assert final.missing == []
    assert final.form is not None and final.form.kind == StudyFormKind.OCHNAYA
    assert "Форма обучения в запросе не указана — принята очная." in final.assumptions
    assert any(a.startswith("Входной уровень слушателей в запросе не указан") for a in final.assumptions)


def test_finalize_after_max_rounds_without_answers(tmp_path: Path) -> None:
    final = finalize_spec(spec_for(OPEN[4]["request"], tmp_path))
    assert final.program_type == ProgramType.PK and final.total_hours == 36
    assert final.fa_form is not None and final.form is not None
    assert any("Объём в запросе не указан" in a for a in final.assumptions)


def test_finalize_type_from_audience_is_explained(tmp_path: Path) -> None:
    final = finalize_spec(spec_for(OWN["accounting_1c"], tmp_path))
    assert final.program_type == ProgramType.PK
    assert any(a.startswith("Тип программы в запросе не указан") for a in final.assumptions)


def test_options_are_neutral_until_type_is_named(tmp_path: Path) -> None:
    """Модель угадала тип (в живом прогоне — «общеразвивающая»), но пока тип не назван, варианты не сужаются."""
    spec = spec_for(OPEN[4]["request"], tmp_path).model_copy(update={"program_type": ProgramType.DOP})
    questions = {q.id: q for q in ask(spec)}
    assert "72 часа" in questions["hours"].options and "144 часа" in questions["hours"].options
    assert any("Экзамен" in o for o in questions["final_attestation"].options)


def test_negative_hours_are_not_a_volume(tmp_path: Path) -> None:
    spec = spec_for(OPEN[4]["request"], tmp_path)
    assert "hours" in apply_answers(spec, {"hours": "-5"}).missing


def test_tiny_volume_is_questioned(tmp_path: Path) -> None:
    spec = spec_for("Программа повышения квалификации для инженеров на 0,5 часа, очно, зачёт.", tmp_path)
    assert "conflict_volume" in spec.missing
    question = next(q for q in ask(spec) if q.id == "conflict_volume")
    assert "слишком мал" in question.text and question.options
    assert apply_answers(spec, {"conflict_volume": "36 часов"}).total_hours == 36
