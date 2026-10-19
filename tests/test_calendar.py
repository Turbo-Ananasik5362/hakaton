"""Календарный график: режимы sessions и days, правило «срок против нагрузки» (§3.4)."""

from __future__ import annotations

from collections import defaultdict

from app.core.calendar import build_calendar
from app.core.hours import (
    HoursConstraints,
    ModuleSpec,
    TopicSpec,
    apply_hours,
    constraints_from_request,
    solve_hours,
)
from app.models import (
    Calendar,
    CalendarMode,
    Constraints,
    FAForm,
    FinalAttestation,
    Kit,
    Module,
    Program,
    ProgramType,
    RequestSpec,
    ScheduleParams,
    Topic,
    WarningType,
    WorkMix,
)

MIX = WorkMix(lk=40, pr=40, srs=20)
MIX_DOP = WorkMix(lk=40, pr=60)


def make_kit(
    constraints: HoursConstraints,
    topics_per_module: list[int],
    mix: WorkMix = MIX,
    duration_text: str | None = None,
) -> Kit:
    """Комплект: модули с равными весами, темы с равными весами; часы — от солвера."""
    modules = [ModuleSpec(key=f"M{i}", weight=1) for i in range(1, len(topics_per_module) + 1)]
    topics = [
        TopicSpec(key=f"T{i}.{j}", module_key=f"M{i}", weight=1, mix=mix)
        for i, count in enumerate(topics_per_module, start=1)
        for j in range(1, count + 1)
    ]
    result = solve_hours(modules, topics, constraints)
    assert result.ok, result.conflicts
    kit = Kit(
        program=Program(
            type=constraints.program_type,
            title="Тест",
            total_hours=constraints.total_hours,
            schedule=ScheduleParams(duration_text=duration_text),
        ),
        modules=[Module(id=m.key, order=i, title=f"Модуль {i}") for i, m in enumerate(modules, start=1)],
        topics=[
            Topic(id=t.key, module_id=t.module_key, order=int(t.key.split(".")[1]), title=t.key)
            for t in topics
        ],
        final_attestation=FinalAttestation(form=FAForm.PROJECT_DEFENSE),
    )
    apply_hours(kit, result)
    return kit


def calendar_hours(calendar: Calendar) -> float:
    return sum(day.hours for day in calendar.days)


def assert_consistent(kit: Kit, calendar: Calendar) -> None:
    """Сумма календаря = объёму; каждая тема и вид работы разложены полностью; ИА — только в конце."""
    assert calendar_hours(calendar) == kit.total_hours() == kit.program.total_hours
    placed: dict[tuple[str | None, str | None], float] = defaultdict(float)
    for day in calendar.days:
        for item in day.items:
            placed[(item.topic_id, item.kind.value if item.kind else None)] += item.hours
    for topic in kit.topics:
        for kind in ("lk", "pr", "lb", "srs"):
            assert placed[(topic.id, kind)] == getattr(topic.hours, kind)
    assert placed[(None, None)] == kit.fa_hours()
    fa_days = [day.n for day in calendar.days if any(item.is_fa for item in day.items)]
    if kit.fa_hours():
        assert fa_days and fa_days[-1] == calendar.days[-1].n
    assert [day.id for day in calendar.days] == [f"D{n}" for n in range(1, len(calendar.days) + 1)]
    assert [day.n for day in calendar.days] == list(range(1, len(calendar.days) + 1))
    assert all(1 <= day.week <= calendar.weeks for day in calendar.days)


# --------------------------------------------------------------------------------------------------
# Занятия фиксированной длины
# --------------------------------------------------------------------------------------------------


def dop_16h() -> HoursConstraints:
    request = RequestSpec(
        program_type=ProgramType.DOP,
        total_hours=16,
        session_hours=2,
        sessions_per_week=2,
        constraints=Constraints(min_practice_share=0.5),
    )
    return constraints_from_request(request, ProgramType.DOP)


def test_dop_16h_eight_sessions_fa_last() -> None:
    constraints = dop_16h()
    kit = make_kit(constraints, [2, 3], mix=MIX_DOP)
    calendar, warnings = build_calendar(kit, constraints)
    assert warnings == []
    assert calendar.mode == CalendarMode.SESSIONS
    assert len(calendar.days) == 8
    assert all(day.hours == 2 for day in calendar.days)
    assert calendar.sessions_per_week == 2
    assert calendar.weeks == 4
    last = calendar.days[-1]
    assert last.n == 8 and [item.is_fa for item in last.items] == [True]
    assert not any(item.is_fa for day in calendar.days[:-1] for item in day.items)
    assert [day.week for day in calendar.days] == [1, 1, 2, 2, 3, 3, 4, 4]
    assert_consistent(kit, calendar)


def test_sessions_fa_shares_last_session() -> None:
    constraints = HoursConstraints(total_hours=12, program_type=ProgramType.DOP, session_hours=2, fa_hours=1)
    kit = make_kit(constraints, [2, 2], mix=MIX_DOP)
    calendar, _ = build_calendar(kit, constraints)
    assert len(calendar.days) == 6
    last = calendar.days[-1]
    assert last.hours == 2
    assert last.items[-1].is_fa and last.items[-1].hours == 1
    assert any(item.topic_id for item in last.items)
    assert_consistent(kit, calendar)


def test_sessions_per_week_from_target_weeks() -> None:
    constraints = HoursConstraints(
        total_hours=24, program_type=ProgramType.DOP, session_hours=2, weeks_target=4
    )
    kit = make_kit(constraints, [2, 2, 2], mix=MIX_DOP)
    calendar, warnings = build_calendar(kit, constraints)
    assert len(calendar.days) == 12
    assert calendar.sessions_per_week == 3 and calendar.weeks == 4
    assert warnings == []


def test_sessions_tight_schedule_warns() -> None:
    constraints = HoursConstraints(
        total_hours=32, program_type=ProgramType.DOP, session_hours=2, sessions_per_week=2, weeks_target=4
    )
    kit = make_kit(constraints, [3, 3], mix=MIX_DOP)
    calendar, warnings = build_calendar(kit, constraints)
    assert calendar.weeks == 8  # 16 занятий по 2 в неделю — жёсткое ограничение не нарушается
    assert [w.type for w in warnings] == [WarningType.SCHEDULE_TIGHT]
    text = warnings[0].text
    assert "16 занятий" in text and "8 недель" in text and "4 занятия в неделю" in text
    assert_consistent(kit, calendar)


# --------------------------------------------------------------------------------------------------
# Учебные дни
# --------------------------------------------------------------------------------------------------


def test_pk_72h_four_weeks() -> None:
    constraints = constraints_from_request(RequestSpec(total_hours=72, weeks=4), ProgramType.PK)
    kit = make_kit(constraints, [3, 3, 2, 2])
    calendar, warnings = build_calendar(kit, constraints)
    assert warnings == []
    assert calendar.mode == CalendarMode.DAYS
    assert calendar.weeks <= 4
    assert all(day.hours <= 6 for day in calendar.days)
    assert calendar.days_per_week == 5
    assert_consistent(kit, calendar)


def test_internship_256h_two_months_is_tight() -> None:
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
    kit = make_kit(constraints, [2, 3, 3, 2], duration_text=request.duration_text)
    assert kit.program_hours().srs == 254 and kit.program_hours().contact == 0

    calendar, warnings = build_calendar(kit, constraints)
    assert all(day.hours <= 4 for day in calendar.days)
    assert len(calendar.days) == 64
    assert calendar.days_per_week == 6
    assert calendar.weeks == 11
    assert [w.type for w in warnings] == [WarningType.SCHEDULE_TIGHT]
    text = warnings[0].text
    assert "64 учебных дня" in text
    assert "11 недель" in text
    assert "около 5 ч в день" in text
    assert "около двух месяцев" in text and "9 недель" in text
    assert calendar.notes
    assert_consistent(kit, calendar)


def test_days_without_target_use_five_day_week() -> None:
    constraints = HoursConstraints(total_hours=36, program_type=ProgramType.PK)
    kit = make_kit(constraints, [2, 2])
    calendar, warnings = build_calendar(kit, constraints)
    assert warnings == []
    assert calendar.days_per_week == 5
    assert len(calendar.days) == 6 and calendar.weeks == 2
    assert all(day.hours == 6 for day in calendar.days)
    assert_consistent(kit, calendar)


def test_days_six_day_week_when_five_is_not_enough() -> None:
    # 120 ч по 4 ч = 30 дней: при 5 днях — 6 недель (больше 5 × 1,15), при 6 днях — 5 недель
    constraints = HoursConstraints(
        total_hours=120, program_type=ProgramType.PK, hours_per_day_max=4, weeks_target=5
    )
    kit = make_kit(constraints, [3, 3])
    calendar, warnings = build_calendar(kit, constraints)
    assert warnings == []
    assert calendar.days_per_week == 6 and calendar.weeks == 5
    assert_consistent(kit, calendar)


def test_days_tolerance_keeps_five_day_week() -> None:
    # 22 дня: при 5 днях — 5 недель при желаемых 5 → допуск не нужен; при желаемых 4 (+15 % = 4,6) — 6 дней
    constraints = HoursConstraints(total_hours=132, program_type=ProgramType.PK, weeks_target=5)
    calendar, _ = build_calendar(make_kit(constraints, [2, 2]), constraints)
    assert calendar.days_per_week == 5 and calendar.weeks == 5


def test_days_stretch_load_to_target_weeks() -> None:
    constraints = HoursConstraints(total_hours=40, program_type=ProgramType.PK, weeks_target=4)
    kit = make_kit(constraints, [2, 2])
    calendar, warnings = build_calendar(kit, constraints)
    assert warnings == []
    assert calendar.weeks == 4
    assert max(day.hours for day in calendar.days) == 2  # 40 ч на 20 учебных дней
    assert any("2 ч в день" in note for note in calendar.notes)
    assert_consistent(kit, calendar)


def test_days_500h() -> None:
    constraints = HoursConstraints(total_hours=500, program_type=ProgramType.PK)
    kit = make_kit(constraints, [4, 4, 4, 4, 4, 4, 4, 4, 4, 4])
    calendar, warnings = build_calendar(kit, constraints)
    assert all(day.hours <= 6 for day in calendar.days)
    assert len(calendar.days) == 84  # ceil(500 / 6)
    assert calendar.weeks == 17 and calendar.days_per_week == 5
    assert warnings == []
    assert_consistent(kit, calendar)

    # при желаемых 20 неделях нагрузка снижается до 5 ч в день: 100 дней по 5 дней в неделю
    constraints.weeks_target = 20
    calendar, warnings = build_calendar(kit, constraints)
    assert warnings == []
    assert len(calendar.days) == 100 and calendar.weeks == 20
    assert max(day.hours for day in calendar.days) == 5
    assert_consistent(kit, calendar)


def test_calendar_is_deterministic() -> None:
    constraints = constraints_from_request(RequestSpec(total_hours=72, weeks=4), ProgramType.PK)
    kit = make_kit(constraints, [3, 3, 2, 2])
    first, _ = build_calendar(kit, constraints)
    second, _ = build_calendar(kit, constraints)
    assert first == second
