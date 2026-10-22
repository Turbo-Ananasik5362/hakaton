"""Генерация содержания (app/pipeline/content.py): сквозные прогоны в mock (запросы 1, 5 и свой), режимы бюджета,
автодобор, квоты вопросов и сборка черновиков модели в объекты комплекта."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm import BudgetExceeded, GatewayError, LLMClient, TruncatedResponse
from app.main import create_app
from app.models import (
    GenerationMode,
    Kit,
    LectureDraft,
    PlanItemDraft,
    QuestionDraft,
    TestScope,
    TopicContentDraft,
    WorkKind,
)
from app.pipeline import content
from app.pipeline import content_apply as apply
from app.pipeline.base import StepContext
from app.pipeline.default import DefaultPipeline
from app.storage.sessions import SessionStore
from scripts.run_open_query import attachments_payload
from tests.intake_queries import OPEN, OWN
from tests.validate_kits import make_pk_kit

OWN_QUERY = {"request": OWN["accounting_1c"], "attachments": []}


def run(tmp_path: Path, query: dict, pipeline: DefaultPipeline | None = None) -> dict[str, Any]:
    settings = Settings(
        sessions_dir=tmp_path / "sessions", llm_mode="mock", cost_log=tmp_path / "logs" / "cost.jsonl"
    )
    with TestClient(create_app(settings=settings, pipeline=pipeline or DefaultPipeline(settings))) as client:
        payload = {
            "request": query["request"],
            "attachments": attachments_payload(query),
            "auto_approve": True,
        }
        body = client.post("/api/v1/sessions", json=payload).json()
        assert body["status"] == "done", body
        return body


def errors(body: dict, *checks: str) -> list[str]:
    return [
        f"{i['check']}: {i['message']}"
        for i in body["report"]["issues"]
        if i["severity"] == "error" and (not checks or i["check"] in checks)
    ]


CONTENT_CHECKS = ("V12", "V14", "V16", "V17", "V18", "V19", "V20", "V21", "V22", "V23", "V43")


# --------------------------------------------------------------------------------------------------
# Сквозные прогоны в mock
# --------------------------------------------------------------------------------------------------


def test_query_1_school_dop(tmp_path: Path) -> None:
    """ДОП 16 ч для школьников: итоговый тест ровно на 10 вопросов, слайды к занятиям 1 и 2, задания по практике."""
    body = run(tmp_path, OPEN[1])
    kit = Kit.model_validate(body["kit"])
    finals = [t for t in kit.tests if t.scope == TestScope.FINAL]
    assert len(finals) == 1 and len(finals[0].question_ids) == 10
    with_slides = {lec.session_no for lec in kit.lectures if lec.slides}
    assert with_slides == {1, 2}
    assert all(len(lec.slides or []) >= 3 for lec in kit.lectures if lec.slides)
    assert kit.seminars and all(len(s.criteria) >= 2 and s.pass_threshold for s in kit.seminars)
    assert kit.final_attestation.form.value == "project_defense" and kit.final_attestation.criteria
    assert kit.texts.relevance and kit.texts.methodical  # разделы ДОП
    assert not kit.texts.annotation  # аннотация — раздел ПК, в ДОП её нет
    assert errors(body, *CONTENT_CHECKS) == []
    assert body["report"]["status"] == "ok", errors(body)
    assert {j["criterion"] for j in body["report"]["judge"]} >= {"J1", "J2", "J5"}


def test_query_5_internship(tmp_path: Path) -> None:
    """Стажировка 256 ч: без лекций и заданий, собеседование по каждому модулю, защита отчёта со структурой."""
    body = run(tmp_path, OPEN[5])
    kit = Kit.model_validate(body["kit"])
    assert not kit.lectures and not kit.seminars and not kit.questions
    for module in kit.modules:
        assert sum(1 for iq in kit.interview_questions if iq.module_id == module.id) >= 3
    fa = kit.final_attestation
    assert fa.form.value == "report_defense" and len(fa.report_requirements) >= 5 and fa.pass_rule
    assert all(t.content.get(WorkKind.SRS) for t in kit.topics)
    assert errors(body, *CONTENT_CHECKS, "V25") == []


def test_own_query_pk_with_exam(tmp_path: Path) -> None:
    """Свой запрос (ПК 40 ч, экзамен с практическим заданием): модульные тесты по 2 вопроса на РО, среда экзамена."""
    body = run(tmp_path, OWN_QUERY)
    kit = Kit.model_validate(body["kit"])
    tests = [t for t in kit.tests if t.scope == TestScope.MODULE]
    assert tests and all(t.module_id for t in tests)
    per_lo: dict[str, int] = {}
    for question in kit.questions:
        for lo in question.lo_ids:
            per_lo[lo] = per_lo.get(lo, 0) + 1
    assert (
        per_lo and min(per_lo.values()) >= 2
    )  # РО, который развивают несколько модулей, — ещё по вопросу в каждом
    assert all(t.question_ids for t in tests)  # у каждого модуля свой непустой тест
    task = kit.final_attestation.example_task
    assert task is not None and task.environment
    assert kit.texts.annotation and kit.texts.satisfaction
    # профстандарта «Бухгалтер» в реестре нет — разделы 1.4 и 1.5 честно пусты (это каркас, а не содержание)
    no_ps = ("1.4.", "1.5.")
    assert [e for e in errors(body, *CONTENT_CHECKS) if not any(x in e for x in no_ps)] == []
    timing = body["report"]["timing"]
    assert {"plan", "program_texts", "module_content:M1", "lectures:M1", "tests:M1", "judge"} <= set(timing)


# --------------------------------------------------------------------------------------------------
# Бюджет: экономный режим и нехватка бюджета
# --------------------------------------------------------------------------------------------------


def test_choose_mode_thresholds() -> None:
    assert content.choose_mode(10, 6, 15)[0] == GenerationMode.FULL
    mode, note = content.choose_mode(10, 6, 14.9)
    assert mode == GenerationMode.ECONOMY and note and "экономном" in note
    mode, note = content.choose_mode(10, 6, 3)
    assert mode == GenerationMode.ECONOMY and "заполнить методисту" in (note or "")


def test_economy_mode_one_call_per_module(tmp_path: Path) -> None:
    settings = Settings(sessions_dir=tmp_path / "sessions", llm_mode="mock")
    body = run(tmp_path, OWN_QUERY, DefaultPipeline(settings, budget_fn=lambda: 6.0))
    kit = Kit.model_validate(body["kit"])
    assert kit.meta.generation_mode == GenerationMode.ECONOMY
    steps = {c["step"]: c["calls"] for c in body["report"]["cost"]}
    assert steps.get("module_pack") == len(kit.modules) and "module_content" not in steps
    assert kit.lectures and kit.seminars and kit.questions and all(t.content for t in kit.topics)
    assert any("экономном режиме" in note for note in kit.meta.generation_notes)
    assert errors(body, "V16", "V18", "V19", "V20") == []


class _NoBudgetPipeline(DefaultPipeline):
    """Шлюз отказывает (бюджет кончился) на всех шагах, кроме итоговой аттестации."""

    def _session_client(self, ctx: StepContext) -> LLMClient:
        client = super()._session_client(ctx)
        original = client.complete_json

        async def complete_json(step: str, *args: Any, **kwargs: Any):  # noqa: ANN202
            if step != "final_attestation":
                raise BudgetExceeded("Суточный бюджет LLM исчерпан (тест).")
            return await original(step, *args, **kwargs)

        client.complete_json = complete_json  # type: ignore[method-assign]
        return client


def test_budget_exhausted_kit_is_still_assembled(tmp_path: Path) -> None:
    """Нет бюджета: комплект собирается из того, что успели, недостающее — «[заполнить методисту]», а литература —
    из каталога без модели; пропущенные шаги перечислены."""
    settings = Settings(sessions_dir=tmp_path / "sessions", llm_mode="mock")
    body = run(tmp_path, OWN_QUERY, _NoBudgetPipeline(settings, budget_fn=lambda: 0.5))
    kit = Kit.model_validate(body["kit"])
    assert kit.final_attestation.criteria  # этот шаг успел
    assert kit.texts.annotation == content.FILL_MARK and kit.texts.mto == [content.FILL_MARK]
    assert all(t.content for t in kit.topics) and any(
        content.FILL_MARK in items for t in kit.topics for items in t.content.values()
    )
    # литература без модели — только издания каталога по теме; для 1С их нет, и методисту это сказано
    assert not kit.sources and any("Литературу по теме" in n for n in kit.meta.generation_notes)
    assert any("бюджет" in note.lower() for note in body["notes"])
    assert any("заполнить методисту" in note for note in kit.meta.generation_notes)


# --------------------------------------------------------------------------------------------------
# Квоты вопросов, автодобор, параллельность
# --------------------------------------------------------------------------------------------------


def test_question_quotas_from_request_and_default() -> None:
    kit = make_pk_kit()
    los = list(dict.fromkeys(lo for t in kit.topics for lo in t.lo_ids))
    default = content.question_quotas(kit)
    assert sum(len(q) for q in default.values()) == len(los)
    assert {n for q in default.values() for n in q.values()} == {content.QUESTIONS_PER_LO}
    kit.request.outputs.test_questions = len(los) + 1
    wanted = content.question_quotas(kit)
    assert sum(n for q in wanted.values() for n in q.values()) == len(los) + 1
    assert content.final_test_needed(kit)


def test_refill_adds_questions_for_uncovered_outcomes(tmp_path: Path) -> None:
    kit = make_pk_kit()
    lo = kit.outcomes[0].id
    kit.questions = [q for q in kit.questions if lo not in q.lo_ids]
    kit.seminars = [s for s in kit.seminars if lo not in s.lo_ids]
    kit.interview_questions = []
    kit.final_attestation.lo_ids = [x for x in kit.final_attestation.lo_ids if x != lo]
    for test in kit.tests:
        test.question_ids = [q for q in test.question_ids if q in {x.id for x in kit.questions}]
    settings = Settings(sessions_dir=tmp_path, llm_mode="mock", cost_log=tmp_path / "cost.jsonl")
    pipeline = DefaultPipeline(settings)
    ctx = StepContext(session_id="s_000000000001", inputs_dir=tmp_path, files_dir=tmp_path, request=None)  # type: ignore[arg-type]
    asyncio.run(pipeline.content._refill(kit, ctx))
    covered = [c for c in content.coverage_matrix(kit) if c.lo_id == lo][0]
    assert len(covered.assessment_ids) >= 2


def test_all_sessions_share_one_semaphore(tmp_path: Path) -> None:
    """Шлюз ограничивает одновременные запросы всей команды — семафор один на процесс, а не на сессию."""
    settings = Settings(sessions_dir=tmp_path, llm_mode="mock", concurrency=2)
    pipeline = DefaultPipeline(settings)

    async def semaphores() -> tuple:
        ctx = StepContext(session_id="s_000000000001", inputs_dir=tmp_path, files_dir=tmp_path, request=None)  # type: ignore[arg-type]
        other = StepContext(
            session_id="s_000000000002", inputs_dir=tmp_path, files_dir=tmp_path, request=None
        )  # type: ignore[arg-type]
        clients = pipeline._session_client(ctx), pipeline._session_client(other), pipeline.llm()
        return tuple(c._semaphore() for c in clients)

    first, second, third = asyncio.run(semaphores())
    assert first is second is third and first._value == 2


class _FlakyPipeline(DefaultPipeline):
    """Первый вызов навыка lectures отвечает 429 (как на живом прогоне 20.10), дальше — как обычно."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.failed = False

    def _session_client(self, ctx: StepContext) -> LLMClient:
        client = super()._session_client(ctx)
        original = client.complete_json

        async def complete_json(step: str, *args: Any, **kwargs: Any):  # noqa: ANN202
            if step == "lectures" and not self.failed:
                self.failed = True
                raise GatewayError("Шлюз вернул HTTP 429: too many concurrent requests", status=429)
            return await original(step, *args, **kwargs)

        client.complete_json = complete_json  # type: ignore[method-assign]
        return client


def test_failed_step_is_retried_once(tmp_path: Path) -> None:
    settings = Settings(sessions_dir=tmp_path / "sessions", llm_mode="mock")
    body = run(tmp_path, OWN_QUERY, _FlakyPipeline(settings))
    kit = Kit.model_validate(body["kit"])
    lecture_modules = {m.id for m in kit.modules if apply.lecture_topics(kit, m)}
    assert {
        lec.module_id for lec in kit.lectures
    } == lecture_modules  # пропавший модуль восстановлен повтором
    retried = [s for s in body["progress"]["steps"] if s["name"].startswith("retry:lectures")]
    assert len(retried) == 1 and retried[0]["status"] == "done"


class _FlakyInterviewPipeline(DefaultPipeline):
    """Первый вызов навыка tests падает: у стажировки шаг называется «Вопросы собеседования модуля N»,
    а его повтор — «Оценочные средства модуля N (повтор)» (прогон 22.10)."""

    failed = False

    def _session_client(self, ctx: StepContext) -> LLMClient:
        client = super()._session_client(ctx)
        original = client.complete_json
        owner = self

        async def complete_json(step: str, *args: Any, **kwargs: Any):  # noqa: ANN202
            if step == "tests" and not owner.failed:
                owner.failed = True
                raise GatewayError("Шлюз вернул HTTP 500", status=500)
            return await original(step, *args, **kwargs)

        client.complete_json = complete_json  # type: ignore[method-assign]
        return client


def test_skip_note_dropped_when_retry_title_differs(tmp_path: Path) -> None:
    settings = Settings(sessions_dir=tmp_path / "sessions", llm_mode="mock")
    body = run(tmp_path, OPEN[5], _FlakyInterviewPipeline(settings))
    retried = [s for s in body["progress"]["steps"] if s["name"].startswith("retry:tests")]
    assert retried and all(s["status"] == "done" for s in retried)
    assert not [n for n in body.get("notes") or [] if "пропущен" in n], body.get("notes")


# --------------------------------------------------------------------------------------------------
# Черновики → объекты комплекта
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1. Разбор примера (2 часа)", "Разбор примера"),
        ("- ПР (4 ч): настройка стенда", "ПР: настройка стенда"),
        ("Работа с данными 2023 года", "Работа с данными 2023 года"),
        ("  ", ""),
        ("1.1: Подготовить обзор принципов", "Подготовить обзор принципов"),
        ("T2.3 — Настроить права доступа", "Настроить права доступа"),
        ("Тема 1.2: Выполнить базовые операции", "Выполнить базовые операции"),
        ("LO1.2) Описать процесс", "Описать процесс"),
        ("Python 3.10: новые возможности", "Python 3.10: новые возможности"),
    ],
)
def test_clean_item_drops_hours_and_bullets(text: str, expected: str) -> None:
    assert apply.clean_item(text) == expected


def test_answers_by_text_and_shuffle() -> None:
    options = ["Сервер", "Клиент", "Маршрутизатор", "Коммутатор"]
    assert apply.answer_indices(options, ["Клиент"]) == [1]
    assert apply.answer_indices(options, ["б) клиент."]) == [1]  # повтор варианта с буквой и точкой
    assert apply.answer_indices(options, ["Брандмауэр"]) == []  # такого варианта нет — ключ не засчитан
    options2, correct = apply.shuffle_options("Вопрос", ["верный", "б", "в", "г"], [0])
    assert options2[correct[0]] == "верный" and sorted(options2) == sorted(["верный", "б", "в", "г"])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Программа длится 120 часов. Слушатели решают кейсы.", "Слушатели решают кейсы."),
        ("Выполните работу за 2 часа и сдайте отчёт.", ""),
        ("Занятия — не больше 4 часов в день.", ""),  # режим занятий пишет код из расписания, не модель
        ("Практика (6 ч) на стенде.", "Практика на стенде."),
        ("Программа объёмом 36 акад. часов. Это 18-часовой интенсив. Кейсы.", "Кейсы."),
        ("Объём — 72 ак. ч. Далее практика.", "Далее практика."),
        ("36 а.ч. аудиторной работы. Далее кейсы.", "Далее кейсы."),
        ("Изучите 3 части отчёта.", "Изучите 3 части отчёта."),
    ],
)
def test_prose_loses_model_hours(text: str, expected: str) -> None:
    assert apply.clean_prose(text) == expected


def test_drafts_with_wrong_ids_are_matched_by_order() -> None:
    kit = make_pk_kit()
    module = kit.modules_sorted()[0]
    topics = kit.topics_of(module.id)
    drafts = [
        TopicContentDraft(topic_id="нет такой", lk=["Теория 1 (2 ч)"], pr=["Практика"], srs=["Дома"])
        for _ in topics
    ]
    drafts[-1].topic_id = topics[0].id.lower().replace("t", "т")  # кириллица в ID
    changed = apply.apply_module_content(kit, module, drafts)
    assert set(changed) == {t.id for t in topics if apply.work_kinds(t)}
    first = topics[0]
    for kind, items in first.content.items():
        assert getattr(first.hours, kind.value) > 0 and "ч)" not in " ".join(items)


def test_questions_respect_quotas_and_reject_broken_keys() -> None:
    kit = make_pk_kit()
    module = kit.modules_sorted()[0]
    los = list(dict.fromkeys(x for t in kit.topics_of(module.id) for x in t.lo_ids))
    lo = los[0]
    test = kit.tests[0]
    before = len(kit.questions)
    good = QuestionDraft(
        topic_id="", lo_ids=[lo], stem="Что верно?", options=["а1", "б2", "в3", "г4"], correct_answers=["а1"]
    )
    broken = QuestionDraft(
        topic_id="", lo_ids=[lo], stem="Без ключа", options=["а1", "б2"], correct_answers=["нет"]
    )
    hours = QuestionDraft(
        topic_id="",
        lo_ids=[lo],
        stem="Сколько длится?",
        options=["2 часа", "3 часа"],
        correct_answers=["2 часа"],
    )
    good2 = good.model_copy(update={"stem": "Что ещё верно?", "options": ["а1", "д5", "е6", "ж7"]})
    good3 = good.model_copy(update={"stem": "А что верно здесь?", "options": ["а1", "з8", "и9", "к10"]})
    extra = [good.model_copy(update={"lo_ids": [los[1]], "stem": "Другой РО"})] if len(los) > 1 else []
    created = apply.apply_questions(
        kit, module, test, [broken, hours, good, good, good2, good3, *extra], {lo: 2}
    )
    assert (
        len(created) == 2 and len(kit.questions) == before + 2
    )  # повтор, сверх квоты и чужой РО — не берутся
    assert [kit.questions[-2].stem, kit.questions[-1].stem] == ["Что верно?", "Что ещё верно?"]
    assert all(q.lo_ids == [lo] and q.options[q.correct[0]] == "а1" for q in kit.questions[-2:])


def test_lecture_title_is_topic_and_slides_from_plan() -> None:
    kit = make_pk_kit()
    kit.lectures = []
    module = next(m for m in kit.modules_sorted() if apply.lecture_topics(kit, m))
    topic = apply.lecture_topics(kit, module)[0]
    plan = [PlanItemDraft(title=f"Раздел {i}", theses=["а", "б", "в"], lo_ids=["LO99.9"]) for i in range(3)]
    apply.apply_lectures(
        kit, module, [LectureDraft(topic_ids=[topic.id], title="Другое название", goal="Цель", plan=plan)]
    )
    lecture = kit.lectures[0]
    assert lecture.title == topic.title and lecture.plan[0].lo_ids == topic.lo_ids
    slides = content.build_slides(kit, lecture)
    assert slides[0].title == topic.title and len(slides) == 5 and all(len(s.bullets) <= 5 for s in slides)


def _skeleton_72h(tmp_path: Path) -> Kit:
    """Каркас запроса 4 (ПК 72 ч) после ответов методиста — генерация содержания не нужна для оценки."""
    answers = OPEN[4]["methodist_answers"]
    settings = Settings(sessions_dir=tmp_path / "sessions", llm_mode="mock")
    with TestClient(create_app(settings=settings)) as client:
        body = client.post("/api/v1/sessions", json={"request": OPEN[4]["request"]}).json()
        given = {q["id"]: answers.get(q["id"], answers["_default"]) for q in body["questions"]}
        body = client.post(f"/api/v1/sessions/{body['session_id']}/answers", json={"answers": given}).json()
        assert body["status"] == "skeleton_ready", body
    kit = SessionStore(settings.sessions_dir).load_kit(body["session_id"])
    assert kit is not None and kit.program.total_hours == 72
    return kit


def test_72h_kit_fits_time_limit(tmp_path: Path) -> None:
    """Комплект 72 ч по оценке укладывается в 20 минут при параллельности по умолчанию (§7.6)."""
    kit = _skeleton_72h(tmp_path)
    full = content.estimate_seconds(kit, GenerationMode.FULL, Settings().concurrency)
    assert 60 < full < 20 * 60
    assert content.estimate_seconds(kit, GenerationMode.FULL, 1) > full  # без параллельности дольше
    assert content.estimate_rub(
        kit, GenerationMode.ECONOMY, "GigaChat/GigaChat-3-Pro"
    ) < content.estimate_rub(kit, GenerationMode.FULL, "GigaChat/GigaChat-3-Pro")


def test_skip_notes_are_grouped_by_reason() -> None:
    from app.pipeline.orchestrator import _add_skip_note

    notes: list[str] = []
    for title in ("Тексты программы", "Литература", "Тексты программы", "Лекции модуля 1"):
        _add_skip_note(notes, title, "Дневной бюджет исчерпан")
    _add_skip_note(notes, "Слайды", "другая причина")
    assert notes == [
        "Шаги пропущены — «Тексты программы», «Литература», «Лекции модуля 1»: Дневной бюджет исчерпан",
        "Шаг «Слайды» пропущен: другая причина",
    ]


def _uncover(kit: Kit, lo: str) -> None:
    kit.questions = [q for q in kit.questions if lo not in q.lo_ids]
    kit.seminars = [s for s in kit.seminars if lo not in s.lo_ids]
    kit.interview_questions = [iq for iq in kit.interview_questions if lo not in iq.lo_ids]
    kit.final_attestation.lo_ids = [x for x in kit.final_attestation.lo_ids if x != lo]
    alive = {q.id for q in kit.questions}
    for test in kit.tests:
        test.question_ids = [q for q in test.question_ids if q in alive]


def _refill(kit: Kit, tmp_path: Path) -> None:
    settings = Settings(sessions_dir=tmp_path, llm_mode="mock", cost_log=tmp_path / "cost.jsonl")
    ctx = StepContext(session_id="s_000000000001", inputs_dir=tmp_path, files_dir=tmp_path, request=None)  # type: ignore[arg-type]
    asyncio.run(DefaultPipeline(settings).content._refill(kit, ctx))


def test_refill_fills_final_test_up_to_requested_size(tmp_path: Path) -> None:
    """Размер итогового теста задан запросом (10): недобор восполняется ровно до 10, полный тест не растёт,
    даже если какому-то РО не хватает оценочных средств (это останется в отчёте)."""
    kit = Kit.model_validate(run(tmp_path, OPEN[1])["kit"])
    final = next(t for t in kit.tests if t.scope == TestScope.FINAL)
    lo = kit.questions[0].lo_ids[0]
    _uncover(kit, lo)
    assert len(final.question_ids) < 10
    _refill(kit, tmp_path)
    assert len(final.question_ids) == 10
    full = Kit.model_validate(run(tmp_path / "again", OPEN[1])["kit"])
    full_final = next(t for t in full.tests if t.scope == TestScope.FINAL)
    full.seminars, full.final_attestation.lo_ids = [], []  # у РО остаётся меньше двух оценочных средств
    _refill(full, tmp_path)
    assert len(full_final.question_ids) == 10


def test_refill_in_interview_modules_keeps_requested_test_size(tmp_path: Path) -> None:
    """Размер теста задан, а текущий контроль модулей — собеседование: добор тестовых вопросов всё равно
    упирается в размер итогового теста."""
    kit = Kit.model_validate(run(tmp_path, OPEN[1])["kit"])
    final = next(t for t in kit.tests if t.scope == TestScope.FINAL)
    for module in kit.modules:
        module.control_form = "собеседование"
    kit.seminars, kit.final_attestation.lo_ids, kit.interview_questions = [], [], []
    generator = DefaultPipeline(
        Settings(sessions_dir=tmp_path, llm_mode="mock", cost_log=tmp_path / "cost.jsonl")
    ).content
    room = 10 - len(final.question_ids)
    assert sum(n for wanted in generator.refill_needs(kit).values() for n in wanted.values()) <= room


def test_refill_in_interview_module_adds_interview_questions(tmp_path: Path) -> None:
    """Стажировка: недостающее оценочное средство — вопрос собеседования, а не тест."""
    kit = Kit.model_validate(run(tmp_path, OPEN[5])["kit"])
    lo = kit.outcomes[0].id
    _uncover(kit, lo)
    _refill(kit, tmp_path)
    assert not kit.tests and not kit.questions
    assert any(lo in iq.lo_ids for iq in kit.interview_questions)


def test_finalize_marks_unknown_codes_in_model_texts() -> None:
    from app.core.registry import default_registry
    from app.core.validate import mark_unknown_codes

    kit = make_pk_kit()
    kit.texts.annotation = "Программа по профстандарту 06.099."
    kit.seminars[0].task = "Изучите приказ № 4321."
    content.mark_model_texts(kit, lambda text: mark_unknown_codes(text, default_registry()))
    assert (
        kit.texts.annotation.endswith("[не подтверждено].")
        or "06.099 [не подтверждено]" in kit.texts.annotation
    )
    assert "№ 4321 [не подтверждено]" in kit.seminars[0].task


def test_refill_prompt_differs_from_original_tests_prompt(tmp_path: Path) -> None:
    """Добор с той же квотой не должен совпадать с исходным запросом теста — иначе кеш вернёт тот же негодный ответ."""
    kit = make_pk_kit()
    module = kit.modules_sorted()[0]
    pipeline = DefaultPipeline(Settings(sessions_dir=tmp_path, llm_mode="mock"))
    quotas = {next(lo for t in kit.topics_of(module.id) for lo in t.lo_ids): 2}
    original = pipeline.content._tests_call(kit, module, quotas, 0, "tests", "k")
    refill = pipeline.content._tests_call(kit, module, quotas, 0, "refill", "k", note=content.REFILL_NOTE)
    assert (
        original.values["quotas"] != refill.values["quotas"]
        and content.REFILL_NOTE in refill.values["quotas"]
    )


def test_seminar_gaps_are_refilled(tmp_path: Path) -> None:
    """Модель дала меньше заданий, чем тем с практикой, — автодобор просит задания только для тем без задания."""
    kit = Kit.model_validate(run(tmp_path, OWN_QUERY)["kit"])
    dropped = kit.seminars[0].topic_id
    kit.seminars = [s for s in kit.seminars if s.topic_id != dropped]
    assert dropped in [t for topics in content.ContentGenerator.seminar_gaps(kit).values() for t in topics]
    _refill(kit, tmp_path)
    assert dropped in {s.topic_id for s in kit.seminars}
    assert not content.ContentGenerator.seminar_gaps(kit)
    assert len({s.topic_id for s in kit.seminars}) == len(kit.seminars)  # по одному на тему


def test_software_list_uses_registry_versions() -> None:
    from app.core.registry import default_registry

    kit = make_pk_kit()  # программа про pandas и Python
    kit.texts.software = ["Python 3.x", "Яндекс Браузер"]
    rows = content.software_list(kit, default_registry())
    assert any(r.startswith("Python (версия") for r in rows) and "Python 3.x" not in rows
    assert "Яндекс Браузер" in rows


def test_term_line_does_not_repeat_term() -> None:
    assert content._term_line("pandas", "pandas — библиотека для таблиц") == "pandas — библиотека для таблиц"
    assert content._term_line("DataFrame", "таблица pandas") == "DataFrame — таблица pandas"


def test_glossary_term_moves_to_first_lecture_where_it_appears() -> None:
    from app.core.validate import fix_glossary_order
    from app.models import GlossaryItem, Lecture, PlanItem

    kit = make_pk_kit()
    module = kit.modules_sorted()[0]
    topics = kit.topics_of(module.id)
    first = Lecture(
        id="L9.1",
        module_id=module.id,
        topic_ids=[topics[0].id],
        title=topics[0].title,
        plan=[PlanItem(title="Раздел", theses=["Датафрейм хранит таблицу"], lo_ids=[])],
    )
    later = Lecture(
        id="L9.2",
        module_id=module.id,
        topic_ids=[topics[-1].id],
        title=topics[-1].title,
        plan=[PlanItem(title="Раздел", theses=["Датафрейм группируют"], lo_ids=[])],
        glossary=[GlossaryItem(term="Датафрейм", definition="таблица данных в памяти")],
    )
    dup = Lecture(
        id="L9.3",
        module_id=module.id,
        topic_ids=[topics[-1].id],
        title=topics[-1].title,
        glossary=[GlossaryItem(term="датафрейм", definition="другое определение")],
    )
    kit.lectures = [first, later, dup]
    fix_glossary_order(kit)
    assert [g.term for g in first.glossary] == ["Датафрейм"] and not later.glossary and not dup.glossary


def test_missing_work_kind_content_comes_from_lecture_and_seminar() -> None:
    """V04: у вида работы есть часы, но модель не дала пунктов — код берёт их из плана лекции и задания темы."""
    kit = make_pk_kit()
    topic = next(t for t in kit.topics if t.hours.lk > 0 and any(s.topic_id == t.id for s in kit.seminars))
    lecture = (
        next(lec for lec in kit.lectures if topic.id in lec.topic_ids)
        if any(topic.id in lec.topic_ids for lec in kit.lectures)
        else None
    )
    topic.content = {}
    changed = content.fill_content_from_materials(kit)
    assert topic.id in changed
    if lecture is not None:
        assert topic.content[WorkKind.LK] == [p.title for p in lecture.plan][:5]
    if topic.hours.pr > 0:
        assert topic.content[WorkKind.PR]


class _LongLiteraturePipeline(DefaultPipeline):
    """Первый ответ навыка literature обрезан по max_tokens (как на живом прогоне 20.10)."""

    def _session_client(self, ctx: StepContext) -> LLMClient:
        client = super()._session_client(ctx)
        original = client.complete_json

        async def complete_json(step: str, model: Any, messages: Any, *args: Any, **kwargs: Any):  # noqa: ANN202
            if step == "literature" and content.LITERATURE_COMPACT not in messages[-1]["content"]:
                raise TruncatedResponse("literature", {"completion_tokens": 3000})
            return await original(step, model, messages, *args, **kwargs)

        client.complete_json = complete_json  # type: ignore[method-assign]
        return client


def test_truncated_literature_gets_one_compact_retry(tmp_path: Path) -> None:
    settings = Settings(sessions_dir=tmp_path / "sessions", llm_mode="mock")
    body = run(tmp_path, OPEN[1], _LongLiteraturePipeline(settings))
    kit = Kit.model_validate(body["kit"])
    assert any(s.section.value == "main" for s in kit.sources)
    assert not [n for n in body.get("notes") or [] if "Литература" in n]


class _LongFinalAttestationPipeline(DefaultPipeline):
    """Ответ навыка final_attestation без короткой приписки обрезан по max_tokens (живой прогон 22.10)."""

    calls: list[bool]

    def _session_client(self, ctx: StepContext) -> LLMClient:
        client = super()._session_client(ctx)
        original = client.complete_json
        calls = self.calls

        async def complete_json(step: str, model: Any, messages: Any, *args: Any, **kwargs: Any):  # noqa: ANN202
            if step == "final_attestation":
                compact = content.FA_COMPACT in messages[-1]["content"]
                calls.append(compact)
                if not compact:
                    raise TruncatedResponse("final_attestation", {"completion_tokens": 3500})
            return await original(step, model, messages, *args, **kwargs)

        client.complete_json = complete_json  # type: ignore[method-assign]
        return client


def test_truncated_final_attestation_gets_one_compact_retry(tmp_path: Path) -> None:
    settings = Settings(sessions_dir=tmp_path / "sessions", llm_mode="mock")
    pipeline = _LongFinalAttestationPipeline(settings)
    pipeline.calls = []
    body = run(tmp_path, OPEN[3], pipeline)
    kit = Kit.model_validate(body["kit"])
    assert kit.final_attestation is not None and kit.final_attestation.criteria
    assert pipeline.calls[:2] == [
        False,
        True,
    ]  # обрезанный ответ → сразу короткий повтор, не такой же длинный
    assert not [n for n in body.get("notes") or [] if "Итоговая аттестация" in n]


def test_service_ids_become_document_numbers() -> None:
    numbers = {"T5.2": "5.2", "M3": "3"}
    assert (
        content.strip_service_ids("Как в теме T5.2 (T5.2, LO1.1), модуль M3.", numbers)
        == "Как в теме 5.2, модуль 3."
    )


def test_skip_note_removed_after_successful_retry() -> None:
    from app.pipeline.orchestrator import _add_skip_note, _drop_skip_note

    notes: list[str] = []
    for title in ("Литература", "Лекции модуля 1", "Тексты программы"):
        _add_skip_note(notes, title, "сбой")
    _drop_skip_note(notes, "Лекции модуля 1")
    assert notes == ["Шаги пропущены — «Литература», «Тексты программы»: сбой"]
    _drop_skip_note(notes, "Литература")
    assert notes == ["Шаг «Тексты программы» пропущен: сбой"]
    _drop_skip_note(notes, "Тексты программы")
    assert notes == []


def test_program_texts_get_final_attestation_form() -> None:
    """Тексты программы видят форму ИА: без неё модель писала «защита проекта» при экзамене (оценка 21.10)."""
    from app.models import FAForm
    from app.pipeline import content_prompts as prompts
    from tests.validate_kits import make_pk_kit

    kit = make_pk_kit()
    assert kit.final_attestation is not None
    kit.final_attestation.form = FAForm.EXAM_PRACTICAL
    kit.final_attestation.form_text = "экзамен с практическим заданием на стенде"
    line = prompts.describe_fa_line(kit)
    assert line.startswith("Итоговая аттестация: экзамен") and "на стенде" in line
    assert "Другие формы" in line


def test_final_test_questions_cover_every_module_by_topics() -> None:
    """Итоговый тест на N вопросов: вопросы у каждого модуля с темами, больше — у модуля с бóльшим числом тем;
    раньше квота РО уходила первому модулю с этим РО, и темы следующих модулей оставались без вопросов (21.10)."""
    from app.pipeline.content import question_quotas

    kit = make_pk_kit()
    for topic in kit.topics:  # один и тот же РО во всех модулях — худший случай для старого правила
        topic.lo_ids = [kit.outcomes[0].id]
    modules = [m for m in kit.modules_sorted() if kit.topics_of(m.id)]
    kit.request.outputs.test_questions = 3 * len(modules)
    quotas = question_quotas(kit)
    per_module = {m.id: sum(quotas.get(m.id, {}).values()) for m in modules}
    assert sum(per_module.values()) == 3 * len(modules)
    assert all(n >= 1 for n in per_module.values()), per_module
    topics = {m.id: len(kit.topics_of(m.id)) for m in modules}
    most, least = max(topics, key=topics.get), min(topics, key=topics.get)
    assert per_module[most] >= per_module[least]
    kit.request.outputs.test_questions = 1  # вопросов меньше, чем модулей, — ровно столько, сколько заказано
    assert sum(n for q in question_quotas(kit).values() for n in q.values()) == 1


def test_reworded_question_is_rejected_as_duplicate() -> None:
    """Тот же вопрос другими словами (те же варианты и верный ответ) или почти та же формулировка — повтор
    (оценка 21.10: Q1 = Q9 в запросе 1, Q9 = Q10 в запросе 3)."""
    kit = make_pk_kit()
    module = kit.modules_sorted()[0]
    lo = next(x for t in kit.topics_of(module.id) for x in t.lo_ids)
    test = kit.tests[0]
    first = QuestionDraft(
        topic_id="",
        lo_ids=[lo],
        stem="Какая команда выведет на экран текст в Python?",
        options=["print Привет", "print('Привет')", "echo 'Привет'", "output('Привет')"],
        correct_answers=["print('Привет')"],
    )
    reworded = first.model_copy(
        update={
            "stem": "Какая команда в Python используется для вывода текста на экран?",
            "options": ["show 'Привет'", "print('Привет')", "echo 'Привет'", "output('Привет')"],
        }
    )
    near = QuestionDraft(
        topic_id="",
        lo_ids=[lo],
        stem="Какие этапы включает создание простого программного проекта на Python?",
        options=["Только код", "Планирование, код, проверка", "Только защита", "Только оформление"],
        correct_answers=["Планирование, код, проверка"],
    )
    near_again = near.model_copy(
        update={
            "stem": "Какие этапы включает создание простого программного проекта?",
            "options": ["Код и защита", "Идея, план, код, оформление", "Только тесты", "Только отладка"],
            "correct_answers": ["Идея, план, код, оформление"],
        }
    )
    other = QuestionDraft(
        topic_id="",
        lo_ids=[lo],
        stem="Что выведет print(2 + 3)?",
        options=["5", "23", "2 + 3", "ошибка"],
        correct_answers=["5"],
    )
    created = apply.apply_questions(kit, module, test, [first, reworded, near, near_again, other], {lo: 5})
    stems = [q.stem for q in kit.questions if q.id in created]
    assert stems == [first.stem, near.stem, other.stem]


def test_internship_interview_sees_independent_work_and_covers_topics() -> None:
    """Собеседование стажировки: модель видит пункты самостоятельной работы (лекций и практики нет), вопросов не
    меньше, чем тем (оценка 21.10, п. 5.6: в разделе о Git — ни одного вопроса о Git)."""
    from app.core.registry import default_registry
    from app.models import WorkKind
    from tests.validate_kits import make_internship_kit

    kit = make_internship_kit()
    module = next(m for m in kit.modules_sorted() if content.is_interview_module(kit, m))
    topics = kit.topics_of(module.id)
    assert content.interview_quota(kit, module) >= len(topics)
    topics[0].content[WorkKind.SRS] = ["Настроить ветвление в репозитории Git по заданию руководителя"]
    generator = content.ContentGenerator(Settings(llm_mode="mock"), default_registry(), lambda ctx: None)
    call = generator._tests_call(kit, module, {}, content.interview_quota(kit, module), "tests", "key")
    assert "Настроить ветвление в репозитории Git" in call.values["topics"]
    assert "по теме модуля на вопрос" in call.values["quotas"]


@pytest.mark.parametrize(
    ("rule", "total", "expected"),
    [
        (
            "Для зачёта нужно набрать не менее 60% и получить не менее 0 по каждому критерию.",
            100,
            "«Зачтено» — не меньше 60 баллов из 100 и ни по одному критерию не 0 баллов.",
        ),
        (
            "Зачёт при 70 % суммы баллов.",
            50,
            "«Зачтено» — не меньше 35 баллов из 50 и ни по одному критерию не 0 баллов.",
        ),
        (
            "Зачтено, если набрано 5 % баллов.",
            50,
            "«Зачтено» — не меньше 30 баллов из 50 и ни по одному критерию не 0 баллов.",
        ),
        ("", 51, "«Зачтено» — не меньше 31 балла из 51 и ни по одному критерию не 0 баллов."),
    ],
)
def test_final_attestation_pass_rule_is_built_from_criteria(rule: str, total: float, expected: str) -> None:
    """Порог ИА считает код: «не менее 0 по каждому критерию» ничего не требовало (оценка 21.10, п. 3.7)."""
    assert apply.fa_pass_rule(rule, total, 0.6) == expected


def test_lecture_does_not_take_topic_of_another_session() -> None:
    """Модель объединила темы двух занятий в одну лекцию: лекция остаётся у своего занятия, тема другого занятия
    получит свою лекцию при доборе (оценка 21.10, п. 1.7: слайды занятия 2 про циклы из занятия 3)."""
    from tests.validate_kits import make_dop_kit

    kit = make_dop_kit()
    kit.lectures = []
    module = kit.modules_sorted()[0]
    first, second = apply.lecture_topics(kit, module)[:2]
    assert apply.session_of(kit, first) != apply.session_of(kit, second)
    plan = [PlanItemDraft(title=f"Раздел {i}", theses=["а", "б", "в"]) for i in range(3)]
    apply.apply_lectures(
        kit, module, [LectureDraft(topic_ids=[first.id, second.id], title="Обе темы", goal="Цель", plan=plan)]
    )
    assert [lec.topic_ids for lec in kit.lectures] == [[first.id]]


def test_question_with_service_unavailable_in_russia_is_dropped() -> None:
    """«Только сервисы России»: вопрос с ChatGPT даже в неверном варианте отбрасывается (оценка 21.10, п. 2.5)."""
    from app.core import bibliography
    from app.core.registry import default_registry

    kit = make_pk_kit()
    kit.request.constraints.ru_services_only = True
    module = kit.modules_sorted()[0]
    lo = next(x for t in kit.topics_of(module.id) for x in t.lo_ids)
    registry = default_registry()
    bad = QuestionDraft(
        topic_id="",
        lo_ids=[lo],
        stem="Каким сервисом учитель может составить план урока?",
        options=["GigaChat", "ChatGPT", "Блокнот", "Калькулятор"],
        correct_answers=["GigaChat"],
    )
    good = bad.model_copy(update={"options": ["GigaChat", "Шедеврум", "Блокнот", "Калькулятор"]})
    created = apply.apply_questions(
        kit,
        module,
        kit.tests[0],
        [bad, good],
        {lo: 2},
        banned=lambda t: bibliography.blocked_service(kit, registry, t),
    )
    kept = [q for q in kit.questions if q.id in created]
    assert len(kept) == 1 and "ChatGPT" not in kept[0].options and "Шедеврум" in kept[0].options


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Цикл for перебирает числа и выводит их:\nfor i in range(3):\n    print(i)",
            "Цикл for перебирает числа и выводит их:\nfor i in range(3):\n    print(i)",
        ),
        (  # перенос, записанный моделью двумя символами, — вне кавычек; «\n» в строке кода — не трогать
            "Функция приветствия:\\ndef привет():\\n    print('Привет\\nмир')",
            "Функция приветствия:\ndef привет():\n    print('Привет\\nмир')",
        ),
        ("Пример: print('Привет, мир!') — выведет текст.", "Пример: print('Привет, мир!') — выведет текст."),
        ("- Отступ \n\n   if x > 0:\n       print(x)\n", "Отступ:\nif x > 0:\n    print(x)"),
    ],
)
def test_code_in_lecture_theses_keeps_lines_and_indents(text: str, expected: str) -> None:
    """Пример кода в тезисе не склеивается в одну строку (оценка 21.10: «for i in range(5): print(i)» в одну
    строку, примеры через «;» не запускались)."""
    value = apply.clean_thesis(text)
    assert value == expected
    code = value.split("\n", 1)[1] if "\n" in value else ""
    if code.startswith(("for ", "def ", "if ")):
        compile(code, "<thesis>", "exec")


@pytest.mark.parametrize(
    "text",
    [
        "Переменные и типы данных\nНа разбор отводится 2 часа практики, затем 1 ч самостоятельной работы",
        "Тема 1.1: Циклы\n- for\n- while (4 часа)",
    ],
)
def test_thesis_tail_with_hours_is_not_kept_verbatim(text: str) -> None:
    """Часы из ответа модели не попадают в лекцию без пересчёта и через «хвост кода» тезиса (ревью 22.10)."""
    value = apply.clean_thesis(text)
    assert not apply.has_hours(value), value
    assert "\n" not in value


def test_thesis_bullet_list_is_not_code() -> None:
    assert apply.clean_thesis("Виды циклов\n- for\n- while") == "Виды циклов - for - while"


def test_judge_sees_all_question_stems_portfolio_and_interview() -> None:
    """Смысловая проверка видит все формулировки вопросов, материалы портфолио и вопросы собеседования — без них
    она пропускала дубли и одинаковые материалы портфолио (оценка 21.10)."""
    from tests.validate_kits import make_internship_kit

    kit = make_pk_kit()
    summary = content.judge_summary(kit)
    assert all(q.stem in summary for q in kit.questions[:40]) or len(kit.questions) <= 5
    internship = make_internship_kit()
    summary = content.judge_summary(internship)
    assert all(q.text in summary for q in internship.interview_questions[:8])
