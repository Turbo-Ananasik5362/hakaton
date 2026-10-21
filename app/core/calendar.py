"""Календарный учебный график (docs/ARCHITECTURE.md §3.4).

Два режима:
- sessions — программа из занятий фиксированной длины: N = объём / длина занятия, занятий в неделю —
  из запроса, иначе из желаемого срока, иначе 2; темы раскладываются по занятиям по порядку
  (занятие может содержать части двух тем), итоговая аттестация — в последнем занятии;
- days — учебные дни с лимитом часов в день: темы по порядку упаковываются в дни.

Общее правило «срок против нагрузки»: объём, лимит часов в день, длина занятия и число занятий в неделю —
жёсткие ограничения и не нарушаются; желаемый срок — мягкое. Дней в неделю — минимальное число из
5…max_days_per_week, при котором срок укладывается в желаемый с допуском +15 %. Если не укладывается и при
максимуме — предупреждение schedule_tight с цифрами, а в календарь идёт расчётный срок. Если программа при
полной нагрузке заканчивается раньше желаемого срока, нагрузка в день снижается (но не ниже
min_hours_per_day), чтобы график занял желаемый срок.
Даты не ставятся: календарь хранит номер дня/занятия и номер недели.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from fractions import Fraction

from app.core.hours import KINDS, HoursConstraints, fmt_hours, plural, to_hours, to_units
from app.core.ids import day_id
from app.core.methodist_warnings import schedule_days, schedule_sessions
from app.models import (
    Calendar,
    CalendarDay,
    CalendarItem,
    CalendarMode,
    Kit,
    MethodistWarning,
    Topic,
    WorkKind,
)

DEFAULT_SESSIONS_PER_WEEK = 2
MIN_DAYS_PER_WEEK = 5
WEEKS_TOLERANCE = Fraction(15, 100)  # желаемый срок можно превысить не больше чем на 15 %


@dataclass(frozen=True)
class _Piece:
    """Кусок работы для раскладки: вид работы темы или итоговая аттестация, в получасах."""

    topic_id: str | None
    is_fa: bool
    kind: WorkKind | None
    units: int


def _topic_pieces(topic: Topic) -> list[_Piece]:
    pieces = []
    for kind in KINDS:
        units = to_units(topic.hours.get(kind))
        if units > 0:
            pieces.append(_Piece(topic_id=topic.id, is_fa=False, kind=kind, units=units))
    return pieces


def _pieces(kit: Kit) -> list[_Piece]:
    """Темы по порядку модулей и тем (ЛК → ПР → ЛБ → СРС внутри темы), в конце — итоговая аттестация."""
    pieces: list[_Piece] = []
    seen: set[str] = set()
    for module in kit.modules_sorted():
        for topic in kit.topics_of(module.id):
            seen.add(topic.id)
            pieces.extend(_topic_pieces(topic))
    for topic in sorted(kit.topics, key=lambda t: (t.order, t.id)):
        if topic.id not in seen:
            pieces.extend(_topic_pieces(topic))
    if kit.final_attestation is not None:
        fa_units = to_units(kit.final_attestation.hours)
        if fa_units > 0:
            pieces.append(_Piece(topic_id=None, is_fa=True, kind=None, units=fa_units))
    return pieces


def _pack(pieces: list[_Piece], capacity: int) -> list[list[_Piece]]:
    """Последовательная упаковка в дни (занятия) ёмкостью capacity получасов; куски делятся на границе дня."""
    days: list[list[_Piece]] = []
    current: list[_Piece] = []
    free = capacity
    for piece in pieces:
        left = piece.units
        while left > 0:
            if free == 0:
                days.append(current)
                current, free = [], capacity
            take = min(left, free)
            current.append(replace(piece, units=take))
            left -= take
            free -= take
    if current:
        days.append(current)
    return days


def _to_days(packed: list[list[_Piece]], per_week: int) -> list[CalendarDay]:
    return [
        CalendarDay(
            id=day_id(n),
            n=n,
            week=(n - 1) // per_week + 1,
            items=[
                CalendarItem(topic_id=p.topic_id, is_fa=p.is_fa, kind=p.kind, hours=to_hours(p.units))
                for p in day
            ],
        )
        for n, day in enumerate(packed, start=1)
    ]


def _fits(weeks: int, target: int) -> bool:
    return weeks <= target * (1 + WEEKS_TOLERANCE)


def _weeks_text(n: int) -> str:
    return f"{n} {plural(n, 'неделя', 'недели', 'недель')}"


def _target_text(target: int, duration_text: str | None) -> str:
    if duration_text:
        return f"{duration_text} (≈ {_weeks_text(target)})"
    return _weeks_text(target)


def _duration_text(kit: Kit) -> str | None:
    return kit.program.schedule.duration_text or kit.request.duration_text


def build_calendar(kit: Kit, constraints: HoursConstraints) -> tuple[Calendar, list[MethodistWarning]]:
    """Календарь по часам комплекта и ограничениям; сумма часов календаря = сумме часов комплекта."""
    pieces = _pieces(kit)
    if constraints.sessions_program and to_units(constraints.session_hours or 0) > 0:
        return _sessions_calendar(kit, constraints, pieces)
    return _days_calendar(kit, constraints, pieces)


def _sessions_calendar(
    kit: Kit, c: HoursConstraints, pieces: list[_Piece]
) -> tuple[Calendar, list[MethodistWarning]]:
    session_u = to_units(c.session_hours or 0)
    session = to_hours(session_u)
    total_u = sum(p.units for p in pieces)
    packed = _pack(pieces, session_u)
    n = len(packed)
    notes: list[str] = []
    warnings: list[MethodistWarning] = []

    per_week = c.sessions_per_week
    if per_week is None:
        if c.weeks_target:
            per_week = min(max(1, math.ceil(n / c.weeks_target)), c.max_days_per_week)
        else:
            per_week = DEFAULT_SESSIONS_PER_WEEK
    weeks = math.ceil(n / per_week) if n else 0

    if total_u % session_u:
        last = to_hours(total_u % session_u)
        notes.append(
            f"Последнее занятие короче остальных — {fmt_hours(last)} ч вместо {fmt_hours(session)} ч: "
            "объём не делится на длину занятия."
        )
    if c.weeks_target and n and not _fits(weeks, c.weeks_target):
        warnings.append(
            schedule_sessions(
                target_weeks=c.weeks_target,
                target_text=_target_text(c.weeks_target, _duration_text(kit)),
                sessions=n,
                session_hours=session,
                per_week=per_week,
                weeks=weeks,
            )
        )
        notes.append(
            f"Расчётный срок — {_weeks_text(weeks)} при {per_week} "
            f"{plural(per_week, 'занятии', 'занятиях', 'занятиях')} в неделю; желаемый срок не выдерживается."
        )

    calendar = Calendar(
        mode=CalendarMode.SESSIONS,
        weeks=weeks,
        days_per_week=None,
        sessions_per_week=per_week,
        days=_to_days(packed, per_week),
        notes=notes,
    )
    return calendar, warnings


def _days_calendar(
    kit: Kit, c: HoursConstraints, pieces: list[_Piece]
) -> tuple[Calendar, list[MethodistWarning]]:
    total_u = sum(p.units for p in pieces)
    cap_u = max(1, to_units(c.day_limit()))
    min_d = min(MIN_DAYS_PER_WEEK, c.max_days_per_week)
    max_d = c.max_days_per_week
    target = c.weeks_target
    notes: list[str] = []
    warnings: list[MethodistWarning] = []

    load_u = cap_u
    if target and total_u:
        full_days = math.ceil(total_u / cap_u)
        if math.ceil(full_days / min_d) < target:
            floor_u = min(cap_u, max(1, to_units(c.min_hours_per_day)))
            load_u = min(cap_u, max(math.ceil(total_u / (target * min_d)), floor_u))
            if load_u < cap_u:
                notes.append(
                    f"Нагрузка — до {fmt_hours(to_hours(load_u))} ч в день (лимит {fmt_hours(to_hours(cap_u))} ч), "
                    f"чтобы график занял желаемый срок ({_weeks_text(target)})."
                )

    packed = _pack(pieces, load_u)
    n = len(packed)
    per_week = min_d
    if target and n:
        fitting = [d for d in range(min_d, max_d + 1) if _fits(math.ceil(n / d), target)]
        if fitting:
            per_week = fitting[0]
        else:
            per_week = max_d
            weeks = math.ceil(n / per_week)
            need_u = math.ceil(total_u / (target * per_week))
            warnings.append(
                schedule_days(
                    target_weeks=target,
                    target_text=_target_text(target, _duration_text(kit)),
                    total=to_hours(total_u),
                    cap=to_hours(cap_u),
                    days=n,
                    per_week=per_week,
                    weeks=weeks,
                    need_per_day=to_hours(need_u),
                )
            )
            notes.append(
                f"Расчётный срок — {_weeks_text(weeks)} при {per_week} учебных днях в неделю и нагрузке "
                f"до {fmt_hours(to_hours(cap_u))} ч в день; желаемый срок при такой нагрузке не выдерживается."
            )
    weeks = math.ceil(n / per_week) if n else 0

    calendar = Calendar(
        mode=CalendarMode.DAYS,
        weeks=weeks,
        days_per_week=per_week,
        sessions_per_week=None,
        days=_to_days(packed, per_week),
        notes=notes,
    )
    return calendar, warnings
