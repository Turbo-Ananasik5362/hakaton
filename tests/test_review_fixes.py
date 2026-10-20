"""Исправления по замечаниям ревью генерации: шлюз лёг — шаги не ждут повторов, ссылки лекций на РО своего модуля,
лимиты собеседования и размера теста, метки кодов во всех полях, отмена частей теста, честный учёт повтора."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.registry import default_registry
from app.core.validate import mark_unknown_codes
from app.llm import GatewayError
from app.main import create_app
from app.models import (
    ExampleTask,
    InterviewDraft,
    Kit,
    LectureDraft,
    PlanItemDraft,
    Source,
    SourceKind,
    SourceSection,
    TestsDraft,
    Verification,
)
from app.pipeline import content
from app.pipeline import content_apply as apply
from app.pipeline.base import AgentError, Step, StepContext
from app.pipeline.default import DefaultPipeline
from app.pipeline.request_rules import MAX_TEST_QUESTIONS, extract_facts
from app.render.bundle import NO_SLIDES_NOTE, NO_TESTS_NOTE
from app.render.report_md import api_message
from tests.fake_pipeline import FakePipeline
from tests.validate_kits import make_pk_kit

FIXTURES = Path(__file__).parent / "fixtures" / "kits"


def load(n: int) -> Kit:
    return Kit.model_validate_json((FIXTURES / f"open_q{n}.json").read_text(encoding="utf-8"))


def ctx(tmp_path: Path) -> StepContext:
    return StepContext(session_id="s_000000000001", inputs_dir=tmp_path, files_dir=tmp_path, request=None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------------------
# 1. Шлюз не отвечает: оставшиеся вызовы модели не запускаются
# --------------------------------------------------------------------------------------------------


class OutagePipeline(FakePipeline):
    def generation_steps(self, kit: Kit, finished: set[str]) -> list[Step]:
        waves = [
            [("content:M1", True), ("content:M2", True)],
            [("lectures:M1", True), ("lectures:M2", True)],
            [("finalize", False)],
            [("validate", False)],
        ]
        for wave in waves:
            pending = [(n, llm) for n, llm in wave if n not in finished]
            if pending:
                return [
                    Step(
                        n,
                        f"Шаг {n}",
                        self._validate_step if n == "validate" else self._runner(n),
                        critical=n == "validate",
                        uses_llm=llm,
                    )
                    for n, llm in pending
                ]
        return []


def _run_outage(tmp_path: Path, failures: dict) -> tuple[OutagePipeline, dict]:  # noqa: ANN001
    pipeline = OutagePipeline(fail_once=failures)
    settings = Settings(sessions_dir=tmp_path / "sessions")
    with TestClient(create_app(settings=settings, pipeline=pipeline)) as c:
        body = c.post(
            "/api/v1/sessions",
            json={"request": "Программа на 36 часов", "answers": {"hours": "36"}, "auto_approve": True},
        ).json()
    return pipeline, body


def test_gateway_outage_skips_remaining_model_steps(tmp_path: Path) -> None:
    """Вся волна упала с «шлюз недоступен» — дальше вызовы модели не запускаются, код выполняется."""
    down = GatewayError("Шлюз недоступен: ConnectError")
    pipeline, body = _run_outage(
        tmp_path, {"content:M1": down, "content:M2": GatewayError("down", status=502)}
    )
    assert body["status"] == "done"
    assert pipeline.calls["lectures:M1"] == pipeline.calls["lectures:M2"] == 0  # не запускались
    assert pipeline.calls["finalize"] == 1 and pipeline.calls["validate"] == 1  # код выполнен
    steps = {s["name"]: s["status"] for s in body["progress"]["steps"]}
    assert steps["lectures:M1"] == steps["lectures:M2"] == "skipped"
    note = next(n for n in body["notes"] if "не отвечал" in n)
    assert "«Шаг lectures:M1»" in note and "«Шаг lectures:M2»" in note  # одна заметка на причину


def test_single_failure_with_live_gateway_does_not_stop_generation(tmp_path: Path) -> None:
    """Один шаг упал, соседний прошёл — шлюз жив: следующие шаги с моделью запускаются."""
    pipeline, body = _run_outage(tmp_path, {"content:M1": GatewayError("Шлюз недоступен: ReadTimeout")})
    assert body["status"] == "done"
    assert pipeline.calls["lectures:M1"] == pipeline.calls["lectures:M2"] == 1
    assert not any("не отвечал" in n for n in body["notes"])


def test_rate_limit_429_does_not_stop_other_steps(tmp_path: Path) -> None:
    pipeline = OutagePipeline(fail_once={"content:M1": GatewayError("busy", status=429)})
    settings = Settings(sessions_dir=tmp_path / "sessions")
    with TestClient(create_app(settings=settings, pipeline=pipeline)) as c:
        body = c.post(
            "/api/v1/sessions",
            json={"request": "Программа на 36 часов", "answers": {"hours": "36"}, "auto_approve": True},
        ).json()
    assert body["status"] == "done" and pipeline.calls["lectures:M1"] == 1


# --------------------------------------------------------------------------------------------------
# 2–5. Применение черновиков и метки
# --------------------------------------------------------------------------------------------------


def test_lecture_plan_links_only_to_outcomes_of_its_module() -> None:
    kit = make_pk_kit()
    first, second = kit.modules_sorted()[:2]
    topic = next(t for t in apply.lecture_topics(kit, first))
    foreign = next(
        lo
        for t in kit.topics_of(second.id)
        for lo in t.lo_ids
        if lo not in {x for t2 in kit.topics_of(first.id) for x in t2.lo_ids}
    )
    kit.lectures = [lec for lec in kit.lectures if lec.module_id != first.id]
    draft = LectureDraft(
        topic_ids=[topic.id],
        title=topic.title,
        plan=[PlanItemDraft(title="Раздел", theses=["Тезис"], lo_ids=[foreign])],
    )
    apply.apply_lectures(kit, first, [draft])
    lecture = next(lec for lec in kit.lectures if lec.module_id == first.id)
    assert foreign not in lecture.plan[0].lo_ids and lecture.plan[0].lo_ids == list(topic.lo_ids)


def test_interview_questions_are_capped_by_quota() -> None:
    kit = load(5)  # стажировка: текущий контроль — собеседование
    module = kit.modules_sorted()[0]
    kit.interview_questions = [iq for iq in kit.interview_questions if iq.module_id != module.id]
    quota = content.interview_quota(kit, module)
    los = [lo for t in kit.topics_of(module.id) for lo in t.lo_ids]
    draft = TestsDraft(
        interview=[
            InterviewDraft(module_id=module.id, lo_ids=los[:1], text=f"Вопрос {i} о практике")
            for i in range(quota + 7)
        ]
    )
    generator = DefaultPipeline(Settings(llm_mode="mock")).content
    generator._apply_tests(kit, module, draft, {}, quota)
    assert len([iq for iq in kit.interview_questions if iq.module_id == module.id]) == quota


def test_huge_test_is_capped_with_explanation() -> None:
    facts = extract_facts("Программа на 72 часа. Нужен итоговый тест на 1000 вопросов.")
    assert facts.test_questions == MAX_TEST_QUESTIONS
    assert any("1000 вопросов" in n and str(MAX_TEST_QUESTIONS) in n for n in facts.notes)
    assert extract_facts("Нужен итоговый тест на 30 вопросов.").test_questions == 30


def test_codes_are_marked_in_all_model_fields() -> None:
    kit = load(3)
    seminar = kit.seminars[0]
    seminar.title = "Разбор по приказу № 4321"
    kit.topics[0].control_form = "опрос по профстандарту 06.099"
    fa = kit.final_attestation
    fa.form_text = "Защита по письму № 77-ЗЗ"
    fa.example_task = ExampleTask(title="Задание по указу № 9999", description="Описание")
    registry = default_registry()
    content.mark_model_texts(kit, lambda text: mark_unknown_codes(text, registry))
    for text in (seminar.title, kit.topics[0].control_form, fa.form_text, fa.example_task.title):
        assert "[не подтверждено]" in text, text


# --------------------------------------------------------------------------------------------------
# 7–8. Части теста и повтор шага
# --------------------------------------------------------------------------------------------------


def test_failed_test_part_cancels_the_others() -> None:
    cancelled = []

    async def slow() -> int:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return 1

    async def broken() -> int:
        await asyncio.sleep(0.01)
        raise GatewayError("busy", status=429)

    async def main() -> None:
        with pytest.raises(GatewayError):
            await content.gather_or_cancel([slow(), broken(), slow()])

    started = asyncio.get_event_loop_policy().new_event_loop()
    try:
        started.run_until_complete(main())
    finally:
        started.close()
    assert cancelled == [True, True]


def test_retry_without_result_is_not_counted_as_success(tmp_path: Path) -> None:
    kit = make_pk_kit()
    kit.sources = [s for s in kit.sources if s.section == SourceSection.NORMATIVE]
    generator = DefaultPipeline(Settings(llm_mode="mock")).content

    async def nothing(kit: Kit, ctx: StepContext) -> None:
        return None

    async def adds_book(kit: Kit, ctx: StepContext) -> None:
        kit.sources.append(
            Source(
                id="SRC90",
                kind=SourceKind.BOOK,
                section=SourceSection.MAIN,
                title="Книга",
                verification=Verification.CATALOG,
            )
        )

    with pytest.raises(AgentError, match="повтор тоже не дал результата"):
        asyncio.run(generator._checked_retry("literature", nothing)(kit, ctx(tmp_path)))
    asyncio.run(
        generator._checked_retry("literature", adds_book)(kit, ctx(tmp_path))
    )  # результат есть — без ошибки


# --------------------------------------------------------------------------------------------------
# 10. Мелочи
# --------------------------------------------------------------------------------------------------


def test_api_message_does_not_promise_missing_report_md() -> None:
    kit = load(3)
    assert "report.md" in api_message(kit)
    without = api_message(kit, has_report_md=False)
    assert "report.md" not in without and "report.json" in without


def test_material_absence_notes_are_not_limitations() -> None:
    from scripts.run_open_query import NO_MATERIAL_NOTES

    assert NO_SLIDES_NOTE.startswith(NO_MATERIAL_NOTES)
    assert NO_TESTS_NOTE.format(forms="собеседование").startswith(NO_MATERIAL_NOTES)
