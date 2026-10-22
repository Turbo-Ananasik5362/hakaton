"""Шаг skeleton: каркасы 5 открытых и 5 своих запросов в mock (intake → ответы → каркас) проходят validate
без ошибок; код исправляет битый ответ модели; часы, календарь, слайды и предупреждения — по общим правилам."""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

import pytest

from app.config import Settings
from app.core.hours import constraints_from_request
from app.core.ids import IdAllocator
from app.core.validate import SKELETON_SKIP, ValidationContext, validate
from app.llm import LLMClient
from app.models import (
    Kit,
    ModuleKind,
    OutcomeKind,
    ProgramType,
    RequestSpec,
    Severity,
    SkeletonDraft,
    SlidesScope,
    TopicRequirement,
    WarningType,
)
from app.pipeline.base import AgentError, Attachment, RequestInput, StepContext
from app.pipeline.clarify import apply_answers, finalize_spec
from app.pipeline.intake import run_intake
from app.pipeline.skeleton import (
    PK_REQUIREMENTS,
    assemble,
    build_skeleton,
    describe_functions,
    describe_request,
    labor_function_options,
    run_skeleton,
    subject_text,
)
from tests.intake_queries import OPEN, OWN, ROOT
from tests.skeleton_registry import make_skeleton_registry

FIXTURES = ROOT / "tests" / "fixtures" / "llm"
TEMPLATES = ROOT / "data" / "templates"
REGISTRY = make_skeleton_registry()

ANSWERS = {
    "open4": dict(OPEN[4]["methodist_answers"]),
    "sales_ai": {
        "program_type": "Повышение квалификации",
        "hours": "24 часа",
        "audience": "Менеджеры по продажам, уверенные пользователи ПК, с нейросетями не работали",
        "form": "Очно-заочно, часть занятий дистанционно",
        "final_attestation": "Защита проекта",
    },
    "sql_economists": {"conflict_load": "Объём 16 часов, срок можно изменить"},
}
FILES = {
    "open1": ["dop_intensiv_ai_24h.docx"],
    "open3": ["pk_stazhirovka_bsa_248h.docx"],
    "open5": ["pk_stazhirovka_bsa_248h.docx"],
}
QUERIES = {f"open{i}": q["request"] for i, q in OPEN.items()} | OWN


def mock_llm() -> LLMClient:
    return LLMClient(Settings(llm_mode="mock", fixtures_dir=FIXTURES))


def check(kit: Kit, warnings) -> object:  # noqa: ANN001
    ctx = ValidationContext(
        registry=REGISTRY, current_year=2026, extra_warnings=list(warnings), stage="skeleton"
    )
    return validate(kit, ctx)


def build(name: str, tmp_path: Path) -> Kit:
    attachments = []
    for n, filename in enumerate(FILES.get(name, []), start=1):
        stored = f"{n:02d}_{filename}"
        shutil.copy(TEMPLATES / filename, tmp_path / stored)
        attachments.append(Attachment(filename=filename, stored_as=stored, size=1))
    request = RequestInput(text=QUERIES[name], attachments=attachments)
    ctx = StepContext("s_test", tmp_path, tmp_path, request)
    spec = asyncio.run(run_intake(request, ctx, mock_llm()))
    spec = apply_answers(spec, ANSWERS.get(name, {}))
    return asyncio.run(run_skeleton(spec, ctx, mock_llm(), REGISTRY, check))  # type: ignore[arg-type]


def errors(kit: Kit) -> list[str]:
    assert kit.report is not None
    return [f"{i.check}: {i.message}" for i in kit.report.issues if i.severity == Severity.ERROR]


def warning_types(kit: Kit) -> set[WarningType]:
    assert kit.report is not None
    return {w.type for w in kit.report.warnings_for_methodist}


# ---- 10 запросов: каркас без ошибок ----------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(QUERIES))
def test_skeleton_passes_validation(name: str, tmp_path: Path) -> None:
    kit = build(name, tmp_path)
    assert errors(kit) == []
    # часы сходятся на всех уровнях и кратны 0,5
    assert kit.total_hours() == kit.program.total_hours
    assert sum(kit.module_hours(m.id).total for m in kit.modules) + kit.fa_hours() == kit.program.total_hours
    assert all((t.hours.total * 2).is_integer() and t.hours.total > 0 for t in kit.topics)
    # связи: каждый РО раскрыт в теме, у каждой компетенции знать/уметь/владеть
    covered = {lo for t in kit.topics for lo in t.lo_ids}
    assert {o.id for o in kit.outcomes} <= covered
    for comp in kit.competencies:
        assert set(kit.competency_kun(comp.id)) == set(OutcomeKind)
        assert all(kit.competency_kun(comp.id)[k] for k in OutcomeKind)
    # ПС и ТФ — только из реестра; у ДОП их нет
    if kit.program.type == ProgramType.DOP:
        assert kit.program.profstandards == [] and all(not c.tf_refs for c in kit.competencies)
    else:
        assert kit.program.profstandards and all(c.tf_refs for c in kit.competencies)
        for comp in kit.competencies:
            for ref in comp.tf_refs:
                assert REGISTRY.labor_function(ref.ps_code, ref.tf_code) is not None
        assert kit.program.audience.requirements == PK_REQUIREMENTS
    assert kit.calendar is not None and kit.calendar.days
    assert kit.final_attestation is not None and kit.final_attestation.lo_ids


def test_open_1_sessions_and_slides(tmp_path: Path) -> None:
    kit = build("open1", tmp_path)
    assert kit.program.type == ProgramType.DOP and kit.program.norm_ids == ["fz273", "minpros629"]
    assert kit.calendar is not None and len(kit.calendar.days) == 8 and kit.calendar.weeks <= 4
    assert all(day.hours == 2 for day in kit.calendar.days)
    assert kit.program_hours().pr >= 8 and kit.program_hours().srs == 0
    first_two = {item.topic_id for day in kit.calendar.days if day.n in (1, 2) for item in day.items}
    assert any(t.hours.lk > 0 for t in kit.topics if t.id in first_two)
    assert kit.program.slides_scope.session_numbers == [1, 2]
    assert kit.program.direction == "техническая" and kit.program.level == "стартовый"


def test_open_3_reference_slides_and_intro(tmp_path: Path) -> None:
    kit = build("open3", tmp_path)
    modules = kit.modules_sorted()
    assert "Python" in modules[0].title  # вводный блок первым
    scope = kit.program.slides_scope
    assert scope.mode == "module" and scope.module_ids == ["M2"] and "pandas" in kit.index()["M2"].title
    assert [m.id for m in kit.modules if m.needs_lectures] == ["M2"]
    assert all(t.hours.lk > 0 for t in kit.topics_of("M2"))
    assert kit.calendar is not None and kit.calendar.weeks == 4
    assert {WarningType.REFERENCE_ERRORS, WarningType.NORM_REPLACED} <= warning_types(kit)


def test_open_4_after_answers_with_expectation_gap(tmp_path: Path) -> None:
    kit = build("open4", tmp_path)
    assert kit.program.type == ProgramType.PK and kit.program.total_hours == 72
    gap = [w for w in kit.report.warnings_for_methodist if w.type == WarningType.EXPECTATION_GAP]  # type: ignore[union-attr]
    assert gap and "пойти работать" in gap[0].text and "72 часа" in gap[0].text
    assert "получающие" in (kit.program.audience.requirements or "")  # студенты колледжа получают СПО


def test_open_5_internship(tmp_path: Path) -> None:
    kit = build("open5", tmp_path)
    hours = kit.program_hours()
    assert (hours.lk, hours.pr, hours.lb, hours.srs, kit.fa_hours()) == (0, 0, 0, 254, 2)
    assert kit.calendar is not None and max(d.hours for d in kit.calendar.days) <= 4
    assert {WarningType.SCHEDULE_TIGHT, WarningType.REFERENCE_ERRORS} <= warning_types(kit)
    assert all(m.control_form for m in kit.modules)
    assert kit.program.slides_scope.mode == "default" and not any(m.needs_lectures for m in kit.modules)


def test_unknown_request_in_mock_uses_neutral_default(tmp_path: Path) -> None:
    text = "Программа повышения квалификации по Kubernetes для инженеров, 48 часов, очно, экзамен."
    request = RequestInput(text=text)
    ctx = StepContext("s_test", tmp_path, tmp_path, request)
    spec = asyncio.run(run_intake(request, ctx, mock_llm()))
    kit = asyncio.run(run_skeleton(spec, ctx, mock_llm(), REGISTRY, check))  # type: ignore[arg-type]
    assert kit.total_hours() == 48 and kit.modules


# ---- код исправляет ответ модели -------------------------------------------------------------------


def _spec(**fields: object) -> RequestSpec:
    base = {"raw_text": "запрос", "program_type": ProgramType.PK, "total_hours": 24, "title": "Тест"}
    return finalize_spec(RequestSpec.model_validate({**base, **fields}))


REPORT_OUTCOMES = [
    {"key": "c1_lo1", "kind": "know", "text": "Знать виды отчётов"},
    {"key": "c1_lo2", "kind": "can", "text": "Уметь строить отчёты по данным"},
    {"key": "c1_lo3", "kind": "master", "text": "Владеть проверкой отчётов"},
]


def _draft(**fields: object) -> SkeletonDraft:
    base = {
        "goal": "Цель",
        "competencies": [
            {
                "key": "c1",
                "title": "Делать отчёты",
                "tf_ids": ["06.042:A/01.6", "99.999:Z/99.9"],
                "outcomes": REPORT_OUTCOMES,
            }
        ],
        "modules": [
            {
                "key": "m1",
                "title": "Отчёты",
                "weight": 3,
                "topics": [
                    {"key": "t1", "title": "Виды отчётов", "weight": 2, "lo_keys": ["c1_lo1"]},
                    {
                        "key": "t2",
                        "title": "Построение отчётов по данным",
                        "weight": 3,
                        "lo_keys": ["c1_lo2"],
                    },
                ],
            }
        ],
    }
    base.update(fields)
    return SkeletonDraft.model_validate(base)


def test_broken_references_are_repaired() -> None:
    spec = _spec()
    options = labor_function_options(spec.model_copy(update={"raw_text": "pandas"}), REGISTRY)
    outcomes = [*REPORT_OUTCOMES[:2], {**REPORT_OUTCOMES[2], "tf_id": "чужая"}]
    draft = _draft(
        competencies=[
            {
                "key": "c1",
                "title": "Делать отчёты",
                "tf_ids": ["06.042:A/01.6", "99.999:Z/99.9"],
                "outcomes": outcomes,
            },
            {"key": "c2", "title": "Пустая компетенция"},
        ],
        modules=[
            {
                "key": "m1",
                "title": "Отчёты",
                "weight": 3,
                "kind": "intro",
                "topics": [
                    {"key": "t1", "title": "Виды отчётов", "weight": 2, "lo_keys": ["C1-LO1", "ghost"]},
                    {"key": "t2", "title": "Проверка отчётов", "weight": 2, "lo_keys": [], "mix": {}},
                ],
            },
            {"key": "m2", "title": "Пустой модуль", "weight": 1, "topics": []},
            {
                "key": "m3",
                "title": "Итоговая аттестация",
                "weight": 5,
                "topics": [{"key": "t3", "title": "Комплексное задание", "weight": 2, "lo_keys": ["c1_lo3"]}],
            },
        ],
    )
    built = build_skeleton(spec, draft, options, REGISTRY)
    kit = built.kit
    assert [c.id for c in kit.competencies] == ["C1"]  # компетенция без РО выброшена
    assert [(r.ps_code, r.tf_code) for r in kit.competencies[0].tf_refs] == [
        ("06.042", "A/01.6")
    ]  # чужая ТФ — нет
    assert all(o.competency_id == "C1" for o in kit.outcomes) and len(kit.outcomes) == 3
    assert all(o.tf_code is None for o in kit.outcomes)
    assert [m.id for m in kit.modules] == ["M1"]  # пустой модуль и модуль ИА не включены
    assert kit.modules[0].kind == ModuleKind.REGULAR  # intro — только в ДОП
    assert kit.topics[0].lo_ids[0] == "LO1.1"  # «C1-LO1» понят как c1_lo1
    assert all(t.lo_ids for t in kit.topics)  # тема без РО получила ближайший РО
    assert {lo for t in kit.topics for lo in t.lo_ids} == {o.id for o in kit.outcomes}  # висячий РО привязан
    assert built.topic_mix["T1.2"].pr > 0  # пустой mix заменён умолчанием типа программы
    assert any("без результатов" in n for n in built.notes) and any("без тем" in n for n in built.notes)
    assert any("Итоговая аттестация" in n and "отдельно" in n for n in built.notes)


def test_live_failure_shape_is_linked() -> None:
    """Живой ответ GigaChat 19.10: ключ результата совпал с ключом компетенции, а темы ссылаются на «c1_lo1»."""
    draft = _draft(
        competencies=[
            {
                "key": f"c{n}",
                "title": f"Компетенция {n}",
                "outcomes": [{"key": f"c{n}", "kind": "know", "text": f"Знать {n}"}],
            }
            for n in (1, 10)
        ],
        modules=[
            {
                "key": "m1",
                "title": "Модуль",
                "weight": 3,
                "topics": [
                    {"key": "t1", "title": "Тема один", "weight": 2, "lo_keys": ["c1_lo1"]},
                    {"key": "t2", "title": "Тема десять", "weight": 2, "lo_keys": ["c10_lo3"]},
                ],
            }
        ],
    )
    kit = build_skeleton(_spec(), draft, [], REGISTRY).kit
    assert [t.lo_ids for t in kit.topics] == [["LO1.1"], ["LO2.1"]]


def test_intro_only_first_module_of_dop() -> None:
    spec = _spec(program_type=ProgramType.DOP, total_hours=16)
    modules = [
        {
            "key": f"m{n}",
            "title": f"Модуль {n}",
            "weight": 2,
            "kind": "intro",
            "topics": [{"key": f"t{n}", "title": f"Тема {n}", "weight": 2, "lo_keys": ["c1_lo1"]}],
        }
        for n in (1, 2, 3)
    ]
    kit = build_skeleton(spec, _draft(modules=modules), [], REGISTRY).kit
    assert [m.kind for m in kit.modules_sorted()] == [
        ModuleKind.INTRO,
        ModuleKind.REGULAR,
        ModuleKind.REGULAR,
    ]


def test_draft_without_modules_fails_clearly() -> None:
    with pytest.raises(AgentError, match="ни одного модуля"):
        build_skeleton(_spec(), _draft(modules=[]), [], REGISTRY)


def test_no_matching_profstandard_is_reported_honestly() -> None:
    spec = _spec(raw_text="Программа повышения квалификации по гончарному делу")
    assert labor_function_options(spec, REGISTRY) == []
    kit = assemble(spec, _draft(), [], REGISTRY, check)  # type: ignore[arg-type]
    assert kit.program.profstandards == [] and all(not c.tf_refs for c in kit.competencies)
    assert any("нет подходящего профстандарта" in a for a in kit.request.assumptions)
    assert any(e.startswith("V25") for e in errors(kit))  # в отчёте — честная ошибка, коды не выдуманы


def test_session_slides_force_lectures_in_those_sessions() -> None:
    spec = _spec(
        program_type=ProgramType.DOP,
        total_hours=12,
        session_hours=2,
        sessions_per_week=2,
        outputs={"slides_scope": SlidesScope(mode="sessions", session_numbers=[1, 2])},
    )
    no_theory = {"lk": 0, "pr": 100, "lb": 0, "srs": 0}
    draft = _draft(
        competencies=[{"key": "c1", "title": "Делать отчёты", "outcomes": REPORT_OUTCOMES}],
        modules=[
            {
                "key": "m1",
                "title": "Практика",
                "weight": 3,
                "topics": [
                    {
                        "key": "t1",
                        "title": "Виды отчётов",
                        "weight": 2,
                        "lo_keys": ["c1_lo1"],
                        "mix": no_theory,
                    },
                    {
                        "key": "t2",
                        "title": "Построение отчётов",
                        "weight": 2,
                        "lo_keys": ["c1_lo2"],
                        "mix": no_theory,
                    },
                ],
            },
            {
                "key": "m2",
                "title": "Проверка",
                "weight": 2,
                "topics": [
                    {
                        "key": "t3",
                        "title": "Проверка отчётов",
                        "weight": 2,
                        "lo_keys": ["c1_lo3"],
                        "mix": no_theory,
                    }
                ],
            },
        ],
    )
    kit = assemble(spec, draft, [], REGISTRY, check)  # type: ignore[arg-type]
    assert kit.calendar is not None
    first = {item.topic_id for day in kit.calendar.days if day.n in (1, 2) for item in day.items}
    assert any(t.hours.lk > 0 for t in kit.topics if t.id in first)
    assert kit.index()["M1"].needs_lectures  # type: ignore[union-attr]


def test_prompt_lists_only_registry_functions_and_no_hours_from_model() -> None:
    spec = _spec(raw_text="Анализ данных в pandas для банка", required_topics=[TopicRequirement(label="SQL")])
    constraints = constraints_from_request(spec, ProgramType.PK)
    options = labor_function_options(spec, REGISTRY)
    listing = describe_functions(options, ProgramType.PK)
    assert "06.042:A/01.6" in listing and "Педагог" not in listing
    params = describe_request(spec, constraints)
    assert "«SQL»" in params and "часы считает программа" in params
    assert "не указываются" in describe_functions([], ProgramType.DOP)


def test_ids_come_from_code() -> None:
    kit = assemble(_spec(), _draft(), [], REGISTRY, check)  # type: ignore[arg-type]
    allocator = IdAllocator(kit)
    assert [t.id for t in kit.topics] == ["T1.1", "T1.2"] and allocator.is_used("LO1.3")
    assert not re.search(r"\d+\s*час", " ".join(t.title for t in kit.topics))


# ---- стадия проверки -------------------------------------------------------------------------------


def test_skeleton_stage_skips_content_checks(tmp_path: Path) -> None:
    kit = build("open2", tmp_path)
    assert kit.report is not None
    assert not {i.check for i in kit.report.issues} & SKELETON_SKIP
    full = validate(kit, ValidationContext(registry=REGISTRY, current_year=2026))
    assert {i.check for i in full.issues if i.severity == Severity.ERROR} & {
        "V16",
        "V17",
        "V19",
        "V20",
        "V21",
    }


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Модуль 2: Администрирование Linux", "Администрирование Linux"),
        ("Тема 1.2 — основы сетей", "Основы сетей"),
        ("Вводный модуль: Основы Linux", "Вводный модуль: Основы Linux"),
        ("Модульное тестирование", "Модульное тестирование"),
    ],
)
def test_numbering_is_stripped_from_titles(raw: str, clean: str) -> None:
    from app.pipeline.skeleton import _title

    assert _title(raw) == clean


def test_pk_requirement_formula_is_not_a_copy() -> None:
    from app.core.copycheck import copy_ratio

    sample = "К освоению допускаются " + PK_REQUIREMENTS + "."
    assert copy_ratio(PK_REQUIREMENTS, [sample]) == 0


def test_unknown_outcome_refs_are_not_guessed() -> None:
    """Живой прогон запроса 5: ссылки c1_lo6…c1_lo15 на несуществующие результаты не должны раздаваться на все РО."""
    from app.pipeline.skeleton import _LinkIndex

    index = _LinkIndex()
    for n, key in enumerate(("a", "b", "c"), start=1):
        index.add("c1", key, f"LO1.{n}")
    assert index.resolve("c1_lo2") == ["LO1.2"]  # по номеру
    assert index.resolve("c1_lo9") == [] and index.resolve("c1") == []
    assert index.unresolved == ["c1_lo9", "c1"]


@pytest.mark.parametrize(
    ("title", "dropped"),
    [
        ("Итоговая аттестация", True),
        ("Итоговая аттестация и развитие в профессии", True),
        ("Экзамен", True),
        ("Итоговый проект", False),
        ("Подготовка к итоговому экзамену", False),
    ],
)
def test_final_attestation_module_detection(title: str, dropped: bool) -> None:
    from app.pipeline.skeleton import FA_MODULE_RE

    assert bool(FA_MODULE_RE.search(title)) == dropped


class StubLLM:
    """Отвечает по очереди: исключение — бросить, иначе вернуть; запоминает текст пользовательского сообщения."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.settings = Settings(llm_mode="mock")

    async def complete_json(self, step, model, messages, schema, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.prompts.append(messages[-1]["content"])
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_truncated_skeleton_is_retried_compactly() -> None:
    from app.llm import TruncatedResponse

    llm = StubLLM(TruncatedResponse("skeleton", {"completion_tokens": 10000}), _draft())
    kit = asyncio.run(run_skeleton(_spec(), None, llm, REGISTRY, check))  # type: ignore[arg-type]
    assert len(llm.prompts) == 2 and "компактно" in llm.prompts[1] and "компактно" not in llm.prompts[0]
    assert any("компактном виде" in a for a in kit.request.assumptions)


def test_truncated_twice_fails_clearly() -> None:
    from app.llm import TruncatedResponse

    cut = TruncatedResponse("skeleton", {"completion_tokens": 10000})
    with pytest.raises(AgentError, match="слишком большой"):
        asyncio.run(run_skeleton(_spec(), None, StubLLM(cut, cut), REGISTRY, check))  # type: ignore[arg-type]


def test_second_standard_only_when_almost_as_relevant() -> None:
    """Второй профстандарт добавляет в промпт свои ТФ, только если подходит почти так же, как первый."""
    clear = labor_function_options(_spec(raw_text="Аналитика в pandas и SQL для бухгалтеров"), REGISTRY)
    assert {o.ps_code for o in clear} == {"06.042"}  # 15 баллов против 5 у «Бухгалтера»
    close = labor_function_options(_spec(raw_text="Кибербезопасность для бухгалтеров"), REGISTRY)
    assert {o.ps_code for o in close} == {"06.032", "08.002"}


def test_attestation_form_does_not_pick_standard() -> None:
    """«Итоговая аттестация — тестирование» говорит о форме контроля, а не о предмете: ПС тестировщика не выбирается."""
    assert subject_text("Курс SQL для экономистов, 16 часов, итоговая аттестация — тестирование.") == (
        "Курс SQL для экономистов, 16 часов,"
    )
    assert subject_text("Основы Linux. В конце итоговое тестирование") == "Основы Linux."
    assert subject_text("Автоматизация тестирования на Python") == "Автоматизация тестирования на Python"


def test_option_ids_are_stripped_from_tasks() -> None:
    from app.pipeline.skeleton import _strip_option_ids

    assert _strip_option_ids("06.042:A/03.6") == ""
    assert _strip_option_ids("Освоить подготовку данных (06.042:A/03.6)") == "Освоить подготовку данных"
    assert _strip_option_ids("Освоить pandas 2.3") == "Освоить pandas 2.3"


def _modules(lo_keys: list[list[str]]) -> list[dict]:
    return [
        {
            "key": f"m{i}",
            "title": f"Модуль {i}",
            "weight": 2,
            "topics": [{"key": f"t{i}", "title": f"Тема {i}", "weight": 2, "lo_keys": keys}],
        }
        for i, keys in enumerate(lo_keys, start=1)
    ]


def test_skeleton_retry_when_outcome_spreads_over_modules() -> None:
    """Один РО в темах 3+ модулей и в половине модулей и больше (V43) — повтор каркаса: у модулей нет своих РО
    (оценка 21.10, п. 5.9: РО в 5 модулях из 8)."""
    from app.pipeline.skeleton import draft_problem

    spread = _draft(modules=_modules([["c1_lo1"], ["c1_lo1", "c1_lo2"], ["c1_lo1"], ["c1_lo3"]]))
    problem = draft_problem(spread, ProgramType.DOP)
    assert "c1_lo1 — в 3 модулях из 4" in problem and "свои результаты" in problem
    assert draft_problem(spread) == ""  # не «фатально»: без повтора каркас всё равно собирается
    fine = _draft(modules=_modules([["c1_lo1"], ["c1_lo1"], ["c1_lo2"], ["c1_lo2"], ["c1_lo3"], ["c1_lo3"]]))
    assert draft_problem(fine, ProgramType.DOP) == ""


@pytest.mark.parametrize(
    "goal",
    [
        "Формирование компетенций по защите сетей для подготовки к работе в сфере кибербезопасности",
        "После курса слушатели готовы к работе инженером",
        "Обеспечить трудоустройство выпускников",
    ],
)
def test_skeleton_retry_when_goal_promises_a_job(goal: str) -> None:
    """Обещание готовности к работе в цели (V40, оценка 21.10, п. 4.8) — повтор каркаса с исправлением."""
    from app.pipeline.skeleton import draft_problem

    assert "обещают готовность к работе" in draft_problem(_draft(goal=goal), ProgramType.PK)
    honest = _draft(goal="Совершенствование компетенций по защите информации в компьютерных сетях")
    assert draft_problem(honest, ProgramType.DOP) == ""


def test_labor_functions_fit_audience_level() -> None:
    """Студентам колледжа — ТФ не выше 5 уровня, рядовым специалистам — не выше 6, руководителям — любые
    (оценка 21.10: ТФ 6–7 уровня для студентов СПО, управленческая B/05.7 для работающих в Excel)."""
    from app.core.registry import function_level, within_level
    from app.core.registry_api import LaborFunctionRecord
    from app.pipeline.skeleton import audience_max_level

    def spec(category: str, entry: str | None = None) -> RequestSpec:
        return _spec(audience={"category": category, "entry_level": entry})

    assert audience_max_level(spec("студенты колледжа", "3 курс, основы сетей")) == 5
    assert audience_max_level(spec("сотрудники банков", "работают в Excel")) == 6
    assert audience_max_level(spec("руководители ИТ-отделов")) == 9
    functions = [LaborFunctionRecord(otf=c[0], code=c, title=c) for c in ("A/01.5", "B/02.6", "C/06.7")]
    assert function_level("C/06.7") == 7
    assert [f.code for f in within_level(functions, 6)] == ["A/01.5", "B/02.6"]
    assert [f.code for f in within_level(functions, 4)] == ["A/01.5"]  # ниже уровня нет — самые низкие
    assert within_level(functions, None) == functions


@pytest.mark.parametrize(
    ("text", "anonymized", "expected"),
    [
        (
            "Учителя должны понимать риски и правила безопасного использования ИИ в работе с детьми.",
            False,
            True,
        ),
        ("Программа для сотрудников банков по анализу данных.", True, True),
        ("Курс по Excel для бухгалтеров.", False, False),
    ],
)
def test_personal_data_law_for_requests_about_children_or_personal_data(
    text: str, anonymized: bool, expected: bool
) -> None:
    """152-ФЗ — в нормативной базе, если запрос о персональных данных или о безопасности в работе с детьми
    (оценка 21.10, п. 2.4)."""
    from app.core.registry import default_registry
    from app.pipeline.skeleton import personal_data_norms

    spec = _spec(raw_text=text)
    spec.constraints.anonymized_data = anonymized
    assert bool(personal_data_norms(spec, default_registry(), ["fz273"])) is expected
    assert personal_data_norms(spec, default_registry(), ["fz152"]) == []
