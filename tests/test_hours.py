"""Солвер часов: сценарии §3.6 docs/ARCHITECTURE.md, конфликты, rebalance, свойство на случайных данных."""

from __future__ import annotations

import random

import pytest

from app.core.hours import (
    ConstraintConflict,
    HoursConstraints,
    ModuleSpec,
    SolverResult,
    TopicSpec,
    apply_hours,
    constraints_from_request,
    hamilton,
    rebalance,
    solve_hours,
)
from app.models import (
    Constraints,
    FAForm,
    FinalAttestation,
    HoursRow,
    Kit,
    Module,
    ModuleKind,
    Program,
    ProgramType,
    RequestSpec,
    StudyForm,
    StudyFormKind,
    Topic,
    WarningType,
    WorkMix,
)

# --------------------------------------------------------------------------------------------------
# Помощники
# --------------------------------------------------------------------------------------------------

Structure = list[tuple[float, bool, list[tuple[float, WorkMix]]]]
"""[(вес модуля, нужны лекции, [(вес темы, mix), ...]), ...]"""


def specs(structure: Structure) -> tuple[list[ModuleSpec], list[TopicSpec]]:
    modules: list[ModuleSpec] = []
    topics: list[TopicSpec] = []
    for mi, (weight, needs_lectures, topic_list) in enumerate(structure, start=1):
        modules.append(ModuleSpec(key=f"M{mi}", weight=weight, needs_lectures=needs_lectures))
        for ti, (t_weight, mix) in enumerate(topic_list, start=1):
            topics.append(TopicSpec(key=f"T{mi}.{ti}", module_key=f"M{mi}", weight=t_weight, mix=mix))
    return modules, topics


def kit_from(structure: Structure, constraints: HoursConstraints) -> tuple[Kit, SolverResult]:
    """Комплект с модулями/темами по структуре и часами от солвера (ключи солвера = ID)."""
    modules, topics = specs(structure)
    result = solve_hours(modules, topics, constraints)
    kit = Kit(
        program=Program(type=constraints.program_type, title="Тест", total_hours=constraints.total_hours),
        modules=[
            Module(id=m.key, order=i, title=f"Модуль {i}", needs_lectures=m.needs_lectures)
            for i, m in enumerate(modules, start=1)
        ],
        topics=[
            Topic(id=t.key, module_id=t.module_key, order=int(t.key.split(".")[1]), title=f"Тема {t.key}")
            for t in topics
        ],
        final_attestation=FinalAttestation(form=FAForm.PROJECT_DEFENSE),
    )
    apply_hours(kit, result)
    return kit, result


def total(result: SolverResult) -> float:
    return sum(row.total for row in result.topic_hours.values()) + result.fa_hours


def column(result: SolverResult, name: str) -> float:
    return sum(getattr(row, name) for row in result.topic_hours.values())


def half_step(value: float) -> bool:
    return value >= 0 and abs(value * 2 - round(value * 2)) < 1e-9


def codes(result: SolverResult) -> list[str]:
    return [c.code for c in result.conflicts]


MIX_BALANCED = WorkMix(lk=40, pr=40, srs=20)
MIX_THEORY = WorkMix(lk=70, pr=30)
MIX_PRACTICE = WorkMix(lk=20, pr=60, srs=20)

# --------------------------------------------------------------------------------------------------
# Метод наибольших остатков
# --------------------------------------------------------------------------------------------------


def test_hamilton_proportional_and_remainders() -> None:
    assert hamilton(10, [1, 1, 1]) == [4, 3, 3]  # равные остатки — по порядку
    assert hamilton(12, [2, 6, 7, 3]) == [1, 4, 5, 2]
    assert hamilton(36, [2, 6, 7, 3]) == [4, 12, 14, 6]  # точная пропорция без округлений
    assert hamilton(7, [0, 0]) == [4, 3]  # все веса нулевые — поровну
    assert hamilton(0, [3, 1]) == [0, 0]


def test_hamilton_minimums_only_bind_when_needed() -> None:
    assert hamilton(36, [2, 6, 7, 3], [4, 4, 4, 4]) == [4, 12, 14, 6]
    result = hamilton(20, [1, 10, 10], [4, 0, 0])
    assert result[0] == 4 and sum(result) == 20 and result[1] == result[2] == 8
    assert hamilton(5, [0, 1], [2, 0]) == [2, 3]
    with pytest.raises(ValueError):
        hamilton(3, [1, 1], [2, 2])


# --------------------------------------------------------------------------------------------------
# Умолчания из запроса
# --------------------------------------------------------------------------------------------------


def test_constraints_defaults_by_program_type() -> None:
    pk = constraints_from_request(RequestSpec(total_hours=72), ProgramType.PK)
    assert pk.fa_hours == 2 and pk.hours_per_day_max == 6 and not pk.all_srs and not pk.dot

    internship = constraints_from_request(RequestSpec(total_hours=256), ProgramType.PK_INTERNSHIP)
    assert internship.fa_hours == 2 and internship.hours_per_day_max == 4 and internship.all_srs

    dop_sessions = constraints_from_request(RequestSpec(total_hours=18, session_hours=1.5), ProgramType.DOP)
    assert dop_sessions.fa_hours == 1.5

    dop_plain = constraints_from_request(RequestSpec(total_hours=24), ProgramType.DOP)
    assert dop_plain.fa_hours == 2

    explicit = constraints_from_request(
        RequestSpec(
            total_hours=40,
            fa_hours=4,
            hours_per_day_max=8,
            weeks=3,
            form=StudyForm(kind=StudyFormKind.OCHNO_ZAOCHNAYA, dot=True),
            constraints=Constraints(all_srs=True, min_practice_share=0.3),
        ),
        ProgramType.PK,
    )
    assert explicit.fa_hours == 4 and explicit.hours_per_day_max == 8 and explicit.weeks_target == 3
    assert explicit.all_srs and explicit.dot and explicit.distance_share == 0.5
    assert explicit.min_practice_share == 0.3

    with pytest.raises(ValueError):
        constraints_from_request(RequestSpec(), ProgramType.PK)


# --------------------------------------------------------------------------------------------------
# Сценарии §3.6
# --------------------------------------------------------------------------------------------------


def dop_16h_constraints() -> HoursConstraints:
    request = RequestSpec(
        program_type=ProgramType.DOP,
        total_hours=16,
        session_hours=2,
        sessions_per_week=2,
        constraints=Constraints(min_practice_share=0.5),
    )
    return constraints_from_request(request, ProgramType.DOP)


DOP_16H: Structure = [
    (3, True, [(1, WorkMix(lk=60, pr=40)), (1, WorkMix(lk=50, pr=30, srs=20))]),
    (4, False, [(2, WorkMix(lk=50, pr=50)), (3, WorkMix(lk=30, pr=50, lb=20))]),
]


def test_dop_16h_sessions_practice_and_no_srs() -> None:
    modules, topics = specs(DOP_16H)
    result = solve_hours(modules, topics, dop_16h_constraints())
    assert result.ok, result.conflicts
    assert total(result) == 16
    assert result.fa_hours == 2  # ИА — одно занятие
    for row in result.topic_hours.values():
        assert row.total % 2 == 0 and row.total >= 2  # темы — целыми занятиями
        assert row.srs == 0 and row.lb == 0
    assert column(result, "pr") >= 8  # практика ≥ половины объёма, ИА практикой не считается
    for key in ("T1.1", "T1.2"):  # модуль со слайдами
        assert result.topic_hours[key].lk >= 1


def test_dop_24h_reproduces_sample_numbers() -> None:
    """Веса = часам образца интенсива (docs/templates_spec_dop.md, T0) → те же числа."""
    constraints = HoursConstraints(total_hours=24, program_type=ProgramType.DOP, fa_hours=6)
    modules = [
        ModuleSpec("M0", 2, kind=ModuleKind.INTRO),
        ModuleSpec("M1", 6),
        ModuleSpec("M2", 7),
        ModuleSpec("M3", 3),
    ]
    topics = [
        TopicSpec("T0.1", "M0", 1, WorkMix(lk=1, pr=1, srs=2)),  # 0,5 / 0,5 / 1
        TopicSpec("T1.1", "M1", 1, WorkMix(lk=15, pr=15, srs=30)),  # 1,5 / 1,5 / 3
        TopicSpec("T2.1", "M2", 1, WorkMix(lk=15, pr=15, srs=40)),  # 1,5 / 1,5 / 4
        TopicSpec("T3.1", "M3", 1, WorkMix(lk=5, pr=5, srs=20)),  # 0,5 / 0,5 / 2
    ]
    result = solve_hours(modules, topics, constraints)
    assert result.ok
    rows = result.topic_hours
    assert (rows["T0.1"].lk, rows["T0.1"].pr, rows["T0.1"].srs) == (0.5, 0.5, 1)
    assert (rows["T1.1"].lk, rows["T1.1"].pr, rows["T1.1"].srs) == (1.5, 1.5, 3)
    assert (rows["T2.1"].lk, rows["T2.1"].pr, rows["T2.1"].srs) == (1.5, 1.5, 4)
    assert (rows["T3.1"].lk, rows["T3.1"].pr, rows["T3.1"].srs) == (0.5, 0.5, 2)
    assert result.module_hours == {"M0": 2, "M1": 6, "M2": 7, "M3": 3}
    assert result.fa_hours == 6
    assert total(result) == 24
    assert column(result, "contact") == 8
    assert column(result, "lk") == 4 and column(result, "pr") == 4 and column(result, "srs") == 10


def test_pk_36h_blended_with_distance_learning() -> None:
    request = RequestSpec(
        program_type=ProgramType.PK,
        total_hours=36,
        form=StudyForm(kind=StudyFormKind.OCHNO_ZAOCHNAYA, dot=True),
    )
    constraints = constraints_from_request(request, ProgramType.PK)
    structure: Structure = [
        (2, False, [(1, MIX_THEORY), (1, MIX_BALANCED)]),
        (3, False, [(2, MIX_PRACTICE), (1, MIX_BALANCED)]),
        (3, False, [(1, MIX_PRACTICE), (1, MIX_PRACTICE)]),
        (2, False, [(1, MIX_BALANCED)]),
    ]
    modules, topics = specs(structure)
    result = solve_hours(modules, topics, constraints)
    assert result.ok
    assert total(result) == 36 and result.fa_hours == 2
    assert column(result, "distance") > 0
    assert column(result, "in_person") > 0
    for row in result.topic_hours.values():
        assert 0 <= row.distance <= row.contact
    # ДОТ в первую очередь — лекции: дистанционных часов не больше половины контактных и не меньше
    assert column(result, "distance") == pytest.approx(column(result, "contact") / 2, abs=0.5)


def test_pk_72h_four_modules() -> None:
    constraints = constraints_from_request(
        RequestSpec(total_hours=72, weeks=4, constraints=Constraints(min_practice_share=0.4)), ProgramType.PK
    )
    structure: Structure = [
        (w, False, [(1, MIX_BALANCED), (2, MIX_PRACTICE), (1, MIX_THEORY)]) for w in (3, 3, 2, 2)
    ]
    modules, topics = specs(structure)
    result = solve_hours(modules, topics, constraints)
    assert result.ok
    assert total(result) == 72
    assert column(result, "pr") >= 0.4 * 72
    assert all(result.module_hours[m.key] >= 2 for m in modules)
    assert all(row.total >= 1 for row in result.topic_hours.values())


def test_internship_256h_all_srs() -> None:
    request = RequestSpec(
        program_type=ProgramType.PK_INTERNSHIP,
        total_hours=256,
        fa_hours=2,
        hours_per_day_max=4,
        weeks=9,
        duration_text="около двух месяцев",
        constraints=Constraints(all_srs=True),
    )
    constraints = constraints_from_request(request, ProgramType.PK_INTERNSHIP)
    structure: Structure = [
        (2, False, [(1, MIX_BALANCED), (1, MIX_PRACTICE)]),
        (3, False, [(1, WorkMix(lk=10, pr=10, lb=50, srs=30)), (2, MIX_THEORY)]),
        (3, False, [(1, MIX_PRACTICE), (1, MIX_PRACTICE), (1, MIX_BALANCED)]),
        (2, False, [(1, WorkMix())]),
    ]
    modules, topics = specs(structure)
    result = solve_hours(modules, topics, constraints)
    assert result.ok
    assert result.fa_hours == 2
    assert column(result, "srs") == 254
    for row in result.topic_hours.values():
        assert row.lk == row.pr == row.lb == 0 and row.distance == 0
    assert total(result) == 256


def test_pk_500h_large_program() -> None:
    constraints = HoursConstraints(
        total_hours=500, program_type=ProgramType.PK, min_practice_share=0.5, dot=True
    )
    structure: Structure = [
        (
            w,
            i % 3 == 0,
            [(t + 1, mix) for t, mix in enumerate((MIX_BALANCED, MIX_PRACTICE, MIX_THEORY)[: 1 + i % 3])],
        )
        for i, w in enumerate((5, 8, 10, 6, 7, 9, 4, 10, 3, 8))
    ]
    modules, topics = specs(structure)
    result = solve_hours(modules, topics, constraints)
    assert result.ok, result.conflicts
    assert total(result) == 500
    assert column(result, "pr") >= 250
    assert all(half_step(getattr(row, f)) for row in result.topic_hours.values() for f in ("lk", "pr", "srs"))
    assert column(result, "distance") > 0 and column(result, "in_person") > 0


def test_module_with_slides_gets_lectures_in_every_topic() -> None:
    constraints = HoursConstraints(total_hours=40, program_type=ProgramType.PK, min_practice_share=0.6)
    no_lectures = WorkMix(lk=0, pr=60, srs=40)
    structure: Structure = [
        (1, False, [(1, MIX_BALANCED)]),
        (2, True, [(1, no_lectures), (2, no_lectures), (1, WorkMix(pr=100))]),
    ]
    modules, topics = specs(structure)
    result = solve_hours(modules, topics, constraints)
    assert result.ok, result.conflicts
    for key in ("T2.1", "T2.2", "T2.3"):
        assert result.topic_hours[key].lk >= 1
    assert column(result, "pr") >= 24
    assert total(result) == 40


def test_lab_hours_only_when_mix_asks_for_them() -> None:
    constraints = HoursConstraints(total_hours=30, program_type=ProgramType.PK)
    structure: Structure = [(1, False, [(1, MIX_BALANCED), (1, WorkMix(lk=20, lb=60, srs=20))])]
    modules, topics = specs(structure)
    result = solve_hours(modules, topics, constraints)
    assert result.topic_hours["T1.1"].lb == 0
    assert result.topic_hours["T1.2"].lb > 0

    dop = HoursConstraints(total_hours=30, program_type=ProgramType.DOP)
    result = solve_hours(modules, topics, dop)
    assert column(result, "lb") == 0  # в ДОП лабораторных нет: их доля уходит в практику
    assert result.topic_hours["T1.2"].pr > 0


def test_solver_is_deterministic() -> None:
    modules, topics = specs(DOP_16H)
    first = solve_hours(modules, topics, dop_16h_constraints())
    second = solve_hours(modules, topics, dop_16h_constraints())
    assert first.topic_hours == second.topic_hours and first.log == second.log


# --------------------------------------------------------------------------------------------------
# Конфликты
# --------------------------------------------------------------------------------------------------


def test_conflict_all_srs_with_practice_share() -> None:
    constraints = HoursConstraints(
        total_hours=72, program_type=ProgramType.PK, all_srs=True, min_practice_share=0.5
    )
    modules, topics = specs([(1, False, [(1, MIX_PRACTICE), (1, MIX_BALANCED)])])
    result = solve_hours(modules, topics, constraints)
    assert not result.ok
    assert codes(result) == ["all_srs_vs_practice"]
    assert "50 %" in result.conflicts[0].message
    # требование «всё — СРС» не нарушено молча
    assert column(result, "pr") == column(result, "lk") == 0
    assert total(result) == 72


def test_conflict_to_warning() -> None:
    conflict = ConstraintConflict("x", "Текст для методиста", {"keys": ["T1.1", "M2"]})
    warning = conflict.to_warning({"T1.1": "T3.4"})
    assert warning.type == WarningType.CONSTRAINT_CONFLICT
    assert warning.text == "Текст для методиста"
    assert warning.target_ids == ["T3.4", "M2"]


def test_conflict_sessions_indivisible() -> None:
    constraints = HoursConstraints(total_hours=15, program_type=ProgramType.DOP, session_hours=2)
    modules, topics = specs(DOP_16H)
    result = solve_hours(modules, topics, constraints)
    assert "sessions_indivisible" in codes(result)
    assert "14 ч или 16 ч" in result.conflicts[0].message
    assert total(result) == 15  # сумма всё равно точная


def test_conflict_minimums_exceed_budget_keeps_exact_sum() -> None:
    constraints = HoursConstraints(total_hours=7, program_type=ProgramType.PK)
    structure: Structure = [(1, False, [(1, MIX_BALANCED), (1, MIX_BALANCED)]) for _ in range(3)]
    modules, topics = specs(structure)
    result = solve_hours(modules, topics, constraints)
    assert codes(result) == ["minimums_exceed_budget"]
    message = result.conflicts[0].message
    assert "нужно 6 ч" in message and "остаётся 5 ч" in message  # 3 модуля × 2 темы × 1 ч против 7 − 2
    assert total(result) == 7
    assert all(row.total > 0 for row in result.topic_hours.values())


def test_conflict_fa_exceeds_total() -> None:
    constraints = HoursConstraints(total_hours=4, program_type=ProgramType.PK, fa_hours=6)
    modules, topics = specs([(1, False, [(1, MIX_BALANCED)])])
    result = solve_hours(modules, topics, constraints)
    assert "fa_exceeds_total" in codes(result)


def test_locked_topic_is_kept_by_solver() -> None:
    constraints = HoursConstraints(total_hours=36, program_type=ProgramType.PK)
    modules, topics = specs(
        [(1, False, [(1, MIX_BALANCED), (1, MIX_BALANCED)]), (1, False, [(1, MIX_BALANCED)])]
    )
    locked = HoursRow(lk=3, pr=4.5, srs=2)
    topics[0].locked = locked
    result = solve_hours(modules, topics, constraints)
    assert result.ok
    assert result.topic_hours["T1.1"] == locked
    assert total(result) == 36


def test_locked_row_with_bad_distance_is_conflict_not_crash() -> None:
    constraints = HoursConstraints(total_hours=20, program_type=ProgramType.PK, dot=True)
    modules, topics = specs([(1, False, [(1, MIX_BALANCED), (1, MIX_BALANCED)])])
    topics[0].locked = HoursRow(lk=2, srs=2, distance=5)
    result = solve_hours(modules, topics, constraints)
    assert codes(result) == ["locked_invalid"]
    assert result.topic_hours["T1.1"].distance == 2
    assert total(result) == 20


def test_locked_more_than_budget_is_conflict() -> None:
    constraints = HoursConstraints(total_hours=20, program_type=ProgramType.PK)
    modules, topics = specs([(1, False, [(1, MIX_BALANCED), (1, MIX_BALANCED)])])
    topics[0].locked = HoursRow(lk=10, pr=10)
    result = solve_hours(modules, topics, constraints)
    assert codes(result) == ["locked_exceeds_budget"]
    assert result.topic_hours["T1.1"].total == 20  # зафиксированная строка не меняется
    assert result.conflicts[0].details["keys"] == ["T1.1"]


# --------------------------------------------------------------------------------------------------
# rebalance и apply_hours
# --------------------------------------------------------------------------------------------------

REBALANCE_STRUCTURE: Structure = [
    (3, False, [(1, MIX_BALANCED), (2, MIX_PRACTICE), (1, MIX_THEORY)]),
    (2, False, [(1, MIX_BALANCED), (1, MIX_PRACTICE)]),
    (2, False, [(1, MIX_PRACTICE)]),
]


def rebalance_kit() -> tuple[Kit, HoursConstraints]:
    constraints = HoursConstraints(total_hours=72, program_type=ProgramType.PK)
    kit, result = kit_from(REBALANCE_STRUCTURE, constraints)
    assert result.ok
    return kit, constraints


def test_apply_hours_writes_topics_and_fa() -> None:
    kit, _ = rebalance_kit()
    assert kit.total_hours() == 72
    assert kit.final_attestation is not None and kit.final_attestation.hours == 2

    modules, topics = specs([(1, False, [(1, MIX_BALANCED)])])
    result = solve_hours(modules, topics, HoursConstraints(total_hours=10, program_type=ProgramType.PK))
    with pytest.raises(KeyError):
        apply_hours(kit, result, {"T1.1": "T9.9"})


def test_rebalance_within_module() -> None:
    kit, constraints = rebalance_kit()
    before = {t.id: t.hours.model_copy() for t in kit.topics}
    module_before = kit.module_hours("M1").total
    new_row = HoursRow(lk=4, pr=6, srs=4)
    assert new_row.total != before["T1.1"].total

    result = rebalance(kit, {"T1.1": new_row}, constraints)
    assert result.ok, result.conflicts
    assert result.topic_hours["T1.1"] == new_row
    assert result.module_hours["M1"] == module_before
    assert total(result) == 72
    for tid in ("T2.1", "T2.2", "T3.1"):  # другие модули не тронуты
        assert result.topic_hours[tid] == before[tid]
    assert result.topic_hours["T1.2"] != before["T1.2"] or result.topic_hours["T1.3"] != before["T1.3"]

    apply_hours(kit, result)
    assert kit.total_hours() == 72


def test_rebalance_program_scope() -> None:
    kit, constraints = rebalance_kit()
    new_row = HoursRow(lk=6, pr=10, srs=4)
    result = rebalance(kit, {"T3.1": new_row}, constraints, scope="program")
    assert result.ok
    assert result.topic_hours["T3.1"] == new_row
    assert total(result) == 72


def test_rebalance_fully_locked_module_moves_difference_to_other_modules() -> None:
    kit, constraints = rebalance_kit()
    old = kit.module_hours("M3").total
    result = rebalance(kit, {"T3.1": HoursRow(lk=2, pr=old)}, constraints)
    assert result.ok
    assert result.module_hours["M3"] == old + 2
    assert total(result) == 72


def test_rebalance_overflow_is_conflict() -> None:
    kit, constraints = rebalance_kit()
    module_total = kit.module_hours("M1").total
    too_big = HoursRow(pr=module_total + 1)
    result = rebalance(kit, {"T1.1": too_big}, constraints)
    assert codes(result) == ["locked_exceeds_module"]
    assert result.topic_hours["T1.1"] == too_big

    result = rebalance(kit, {"T1.1": HoursRow(srs=80)}, constraints, scope="program")
    assert codes(result) == ["locked_exceeds_budget"]
    assert "70 ч" in result.conflicts[0].message


def test_rebalance_unknown_topic() -> None:
    kit, constraints = rebalance_kit()
    with pytest.raises(ValueError):
        rebalance(kit, {"T9.9": HoursRow(lk=1)}, constraints)


# --------------------------------------------------------------------------------------------------
# Свойство: случайные веса и ограничения
# --------------------------------------------------------------------------------------------------


def random_case(rng: random.Random) -> tuple[list[ModuleSpec], list[TopicSpec], HoursConstraints]:
    program_type = rng.choice(list(ProgramType))
    structure: Structure = []
    for _ in range(rng.randint(1, 8)):
        topic_list = []
        for _ in range(rng.randint(1, 6)):
            if rng.random() < 0.05:
                mix = WorkMix()
            else:
                mix = WorkMix(
                    lk=rng.randint(0, 100),
                    pr=rng.randint(0, 100),
                    lb=rng.choice([0, 0, 0, rng.randint(0, 60)]),
                    srs=rng.randint(0, 100),
                )
            topic_list.append((rng.choice([0, 1, 2, 3, 5, 10, 0.5, 2.5, rng.random() * 10]), mix))
        structure.append(
            (rng.choice([0, 1, 2, 3, 7, 10, 1.5, rng.random() * 10]), rng.random() < 0.2, topic_list)
        )
    modules, topics = specs(structure)

    session = rng.choice([1, 1.5, 2, 3, 4]) if rng.random() < 0.3 else None
    if session is not None:
        total_hours = session * rng.randint(2, 60) + (0.5 if rng.random() < 0.1 else 0)
    else:
        total_hours = rng.randint(4, 600) + rng.choice([0, 0, 0.5])
    fa = rng.choice([None, None, 0, 1, 2, 4, 6])
    if fa is not None and fa > total_hours:
        fa = None
    if fa is None and session is not None and session > total_hours:
        fa = 0
    constraints = HoursConstraints(
        total_hours=total_hours,
        program_type=program_type,
        fa_hours=fa,
        min_practice_share=rng.choice([None, 0, 0.3, 0.5, 0.7]),
        all_srs=rng.random() < 0.15,
        session_hours=session,
        dot=rng.random() < 0.3,
        distance_share=rng.choice([None, 0.2, 0.5, 0.8, 1.0]),
        min_topic_hours=rng.choice([0.5, 1.0, 2.0]),
        min_module_hours=rng.choice([0, 2, 4]),
    )
    return modules, topics, constraints


def test_property_random_inputs() -> None:
    rng = random.Random(20261018)
    for case in range(300):
        modules, topics, c = random_case(rng)
        result = solve_hours(modules, topics, c)
        context = f"случай {case}: {c}"

        assert total(result) == c.total_hours, context
        assert half_step(result.fa_hours), context
        for key, row in result.topic_hours.items():
            for name in ("lk", "pr", "lb", "srs", "distance"):
                assert half_step(getattr(row, name)), (context, key, row)
            assert row.distance <= row.contact, (context, key, row)
        for m in modules:
            module_sum = sum(result.topic_hours[t.key].total for t in topics if t.module_key == m.key)
            assert result.module_hours[m.key] == module_sum, context

        mix_of = {t.key: t.mix for t in topics}
        for key, row in result.topic_hours.items():
            if c.srs_only:
                assert row.contact == 0, (context, key, row)
            if c.program_type == ProgramType.DOP or mix_of[key].lb == 0:
                assert row.lb == 0, (context, key, row)
            if c.sessions_program:
                assert row.srs == 0, (context, key, row)

        if result.ok:
            share = c.min_practice_share or 0
            assert column(result, "pr") >= share * c.total_hours, context
            for t in topics:
                row = result.topic_hours[t.key]
                assert row.total >= c.min_topic_hours, (context, t.key, row)
                if next(m for m in modules if m.key == t.module_key).needs_lectures:
                    assert row.lk >= 1, (context, t.key, row)
            for m in modules:
                assert result.module_hours[m.key] >= c.min_module_hours, context
            if c.dot and 0 < c.resolved_distance_share() < 1 and column(result, "contact") >= 1:
                assert column(result, "distance") > 0 and column(result, "in_person") > 0, context

        again = solve_hours(modules, topics, c)
        assert again.topic_hours == result.topic_hours, context
