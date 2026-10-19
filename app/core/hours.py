"""Детерминированный солвер часов (docs/ARCHITECTURE.md §3).

LLM даёт только структуру и относительные веса (модули, темы, mix видов работы), часы раскладывает код:
- всё считается в целых получасах (u = 0,5 ч); наружу отдаются float, кратные 0,5;
- распределение по уровням — модули → темы → виды работы — методом наибольших остатков (Гамильтон)
  с нижними границами: минимум модуля/темы гарантирован, остаток делится пропорционально весам,
  дробные остатки раздаются по убыванию, при равенстве — по порядку следования;
- для программы из занятий фиксированной длины делятся занятия, а не получасы, и все часы аудиторные;
- глобальные поправки, не меняющие сумм тем: ЛК ≥ 1 ч в модулях со слайдами, доля практики, ДОТ;
- несовместимые требования не нарушаются молча, а возвращаются как ConstraintConflict.

Инвариант: Σ тем + ИА = total_hours точно, все значения кратны 0,5, отрицательных нет.
Единственные исключения — конфликты, при которых объём недостижим без нарушения требования
(зафиксировано больше объёма, ИА больше объёма); о них солвер сообщает.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Any, Literal

from app.models import (
    HoursRow,
    Kit,
    MethodistWarning,
    ModuleKind,
    ProgramType,
    RequestSpec,
    ScheduleParams,
    Topic,
    WarningType,
    WorkKind,
    WorkMix,
)

UNITS_PER_HOUR = 2
KINDS: tuple[WorkKind, ...] = (WorkKind.LK, WorkKind.PR, WorkKind.LB, WorkKind.SRS)
DEFAULT_FA_HOURS = 2.0
DEFAULT_HOURS_PER_DAY: dict[ProgramType, float] = {
    ProgramType.PK: 6.0,
    ProgramType.PK_INTERNSHIP: 4.0,
    ProgramType.DOP: 4.0,
}
DEFAULT_DISTANCE_SHARE = 0.5
LECTURE_MIN_HOURS = 1.0

# --------------------------------------------------------------------------------------------------
# Единицы и форматирование
# --------------------------------------------------------------------------------------------------


def to_units(hours: float) -> int:
    """Часы → целые получасы (с округлением до ближайшего)."""
    return int(round(hours * UNITS_PER_HOUR))


def to_hours(units: int) -> float:
    return units / UNITS_PER_HOUR


def is_half_multiple(hours: float) -> bool:
    scaled = hours * UNITS_PER_HOUR
    return math.isfinite(scaled) and abs(scaled - round(scaled)) < 1e-9


def fmt_hours(value: float) -> str:
    """Число для текста методисту: 4.0 → «4», 4.5 → «4,5»."""
    rounded = round(value, 1)
    if abs(rounded - round(rounded)) < 1e-9:
        return str(int(round(rounded)))
    return f"{rounded:.1f}".replace(".", ",")


def plural(n: int, one: str, few: str, many: str) -> str:
    """Согласование с числом: plural(11, "неделя", "недели", "недель") → «недель»."""
    rest = abs(n) % 100
    if 11 <= rest <= 14:
        return many
    last = rest % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _frac(value: float | int | Fraction) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if not math.isfinite(value):
        return Fraction(0)
    return Fraction(repr(float(value)))


def _percent(share: float) -> str:
    return fmt_hours(share * 100)


# --------------------------------------------------------------------------------------------------
# Метод наибольших остатков
# --------------------------------------------------------------------------------------------------


def hamilton(total: int, weights: Sequence[float], minimums: Sequence[int] | None = None) -> list[int]:
    """Делит целое `total` пропорционально `weights` с нижними границами `minimums`.

    Элемент, чья пропорциональная доля меньше минимума, получает ровно минимум, остальной объём
    снова делится пропорционально между прочими (пока такие находятся). Если минимумы не мешают,
    результат совпадает с чистым пропорциональным делением. Округление — по наибольшим дробным
    остаткам, при равенстве остатков выигрывает элемент с меньшим индексом. Арифметика точная (дроби).
    Нулевые веса получают только минимум; если все веса нулевые — деление поровну.
    """
    n = len(weights)
    mins = [int(m) for m in minimums] if minimums is not None else [0] * n
    if len(mins) != n:
        raise ValueError("Число минимумов не совпадает с числом весов.")
    if total < 0 or any(m < 0 for m in mins):
        raise ValueError("Объём и минимумы не могут быть отрицательными.")
    if n == 0:
        if total:
            raise ValueError("Нечего распределять: нет ни одного элемента.")
        return []
    if sum(mins) > total:
        raise ValueError("Сумма минимумов больше распределяемого объёма.")

    w = [max(Fraction(0), _frac(x)) for x in weights]
    if sum(w) == 0:
        w = [Fraction(1)] * n
    fixed: set[int] = set()
    free: list[int] = list(range(n))
    rest = total
    quotas: dict[int, Fraction] = {}
    while free:
        rest = total - sum(mins[i] for i in fixed)
        weight_sum = sum((w[i] for i in free), Fraction(0))
        if weight_sum == 0:
            for i in free:
                w[i] = Fraction(1)
            weight_sum = Fraction(len(free))
        quotas = {i: rest * w[i] / weight_sum for i in free}
        below = [i for i in free if quotas[i] < mins[i]]
        if not below:
            break
        fixed.update(below)
        free = [i for i in free if i not in fixed]

    result = list(mins)
    floors = {i: math.floor(quotas[i]) for i in free}
    for i in free:
        result[i] = floors[i]
    left = rest - sum(floors.values()) if free else 0
    order = sorted(free, key=lambda i: (-(quotas[i] - floors[i]), i))
    for i in order[:left]:
        result[i] += 1
    return result


# --------------------------------------------------------------------------------------------------
# Вход и выход солвера
# --------------------------------------------------------------------------------------------------


@dataclass
class ModuleSpec:
    """Модуль каркаса: относительный вес (из SkeletonDraft), флаг «нужны лекции» (запрошены слайды)."""

    key: str
    weight: float
    needs_lectures: bool = False
    kind: ModuleKind = ModuleKind.REGULAR
    title: str | None = None  # для понятных сообщений методисту; иначе используется key

    @property
    def label(self) -> str:
        return self.title or self.key


@dataclass
class TopicSpec:
    """Тема каркаса: вес внутри модуля, доли видов работы, необязательная зафиксированная строка."""

    key: str
    module_key: str
    weight: float
    mix: WorkMix
    locked: HoursRow | None = None
    title: str | None = None

    @property
    def label(self) -> str:
        return self.title or self.key


@dataclass
class HoursConstraints:
    """Жёсткие ограничения из запроса и правила шаблона (после подстановки умолчаний)."""

    total_hours: float
    program_type: ProgramType
    fa_hours: float | None = None  # None → умолчание по типу программы
    min_practice_share: float | None = None  # доля ПР от общего объёма (ИА практикой не считается)
    all_srs: bool = False
    session_hours: float | None = None  # длина занятия: программа из занятий фиксированной длины
    sessions_per_week: int | None = None
    weeks_target: int | None = None  # желаемый срок (мягкое ограничение)
    hours_per_day_max: float | None = None  # None → умолчание по типу программы
    max_days_per_week: int = 6
    dot: bool = False
    distance_share: float | None = None  # None → 0,5 при ДОТ
    min_topic_hours: float = 1.0
    min_module_hours: float = 2.0
    min_hours_per_day: float = 2.0  # ниже этой нагрузки календарь не растягивается под желаемый срок

    def __post_init__(self) -> None:
        if not 1 <= self.max_days_per_week <= 7:
            raise ValueError("Учебных дней в неделю должно быть от 1 до 7.")
        if self.sessions_per_week is not None and self.sessions_per_week < 1:
            raise ValueError("Занятий в неделю должно быть не меньше одного.")
        if self.weeks_target is not None and self.weeks_target < 1:
            raise ValueError("Желаемый срок должен быть не меньше одной недели.")
        for name in ("min_practice_share", "distance_share"):
            value = getattr(self, name)
            if value is not None and not 0 <= value <= 1:
                raise ValueError(f"{name} должно быть в диапазоне от 0 до 1.")

    @property
    def srs_only(self) -> bool:
        """Вся работа — самостоятельная: стажировка или явное требование запроса."""
        return self.all_srs or self.program_type == ProgramType.PK_INTERNSHIP

    @property
    def sessions_program(self) -> bool:
        """Программа из занятий фиксированной длины: все часы аудиторные (СРС = 0)."""
        return bool(self.session_hours and self.session_hours > 0) and not self.srs_only

    def default_fa_hours(self) -> float:
        if self.program_type == ProgramType.DOP and self.session_hours and self.session_hours > 0:
            return float(self.session_hours)
        return DEFAULT_FA_HOURS

    def resolved_fa_hours(self) -> float:
        return float(self.fa_hours) if self.fa_hours is not None else self.default_fa_hours()

    def day_limit(self) -> float:
        if self.hours_per_day_max and self.hours_per_day_max > 0:
            return float(self.hours_per_day_max)
        return DEFAULT_HOURS_PER_DAY[self.program_type]

    def resolved_distance_share(self) -> float:
        if not self.dot:
            return 0.0
        return self.distance_share if self.distance_share is not None else DEFAULT_DISTANCE_SHARE

    def to_schedule_params(self, duration_text: str | None = None) -> ScheduleParams:
        """Параметры для Program.schedule — с подставленными умолчаниями."""
        return ScheduleParams(
            weeks_target=self.weeks_target,
            duration_text=duration_text,
            hours_per_day_max=self.day_limit(),
            session_hours=self.session_hours,
            sessions_per_week=self.sessions_per_week,
            max_days_per_week=self.max_days_per_week,
        )


@dataclass
class ConstraintConflict:
    """Несовместимые требования. message — для методиста, по-русски, с цифрами."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_warning(self, key_to_id: dict[str, str] | None = None) -> MethodistWarning:
        keys = [str(k) for k in self.details.get("keys", [])]
        if key_to_id:
            keys = [key_to_id.get(k, k) for k in keys]
        return MethodistWarning(type=WarningType.CONSTRAINT_CONFLICT, text=self.message, target_ids=keys)


@dataclass
class SolverResult:
    topic_hours: dict[str, HoursRow]
    fa_hours: float
    log: list[str] = field(default_factory=list)
    conflicts: list[ConstraintConflict] = field(default_factory=list)
    module_hours: dict[str, float] = field(default_factory=dict)  # ключ модуля → сумма его тем

    @property
    def ok(self) -> bool:
        return not self.conflicts

    @property
    def total_hours(self) -> float:
        return sum(row.total for row in self.topic_hours.values()) + self.fa_hours

    def warnings(self, key_to_id: dict[str, str] | None = None) -> list[MethodistWarning]:
        return [conflict.to_warning(key_to_id) for conflict in self.conflicts]


def constraints_from_request(request: RequestSpec, program_type: ProgramType) -> HoursConstraints:
    """Ограничения солвера из разобранного запроса с умолчаниями шаблона (§3.2–§3.4)."""
    if request.total_hours is None:
        raise ValueError("В запросе не указан объём программы в часах — его нужно уточнить у методиста.")
    form = request.form
    constraints = HoursConstraints(
        total_hours=float(request.total_hours),
        program_type=program_type,
        fa_hours=request.fa_hours,
        min_practice_share=request.constraints.min_practice_share or 0.0,
        all_srs=program_type == ProgramType.PK_INTERNSHIP or request.constraints.all_srs,
        session_hours=request.session_hours,
        sessions_per_week=request.sessions_per_week,
        weeks_target=request.weeks,
        hours_per_day_max=request.hours_per_day_max,
        dot=bool(form and form.dot),
        distance_share=form.distance_share if form else None,
    )
    constraints.fa_hours = constraints.resolved_fa_hours()
    constraints.hours_per_day_max = constraints.day_limit()
    if constraints.dot and constraints.distance_share is None:
        constraints.distance_share = DEFAULT_DISTANCE_SHARE
    return constraints


# --------------------------------------------------------------------------------------------------
# Внутреннее представление
# --------------------------------------------------------------------------------------------------


@dataclass
class _Row:
    """Строка часов в получасах."""

    lk: int = 0
    pr: int = 0
    lb: int = 0
    srs: int = 0
    distance: int = 0

    @property
    def contact(self) -> int:
        return self.lk + self.pr + self.lb

    @property
    def total(self) -> int:
        return self.contact + self.srs

    def get(self, kind: WorkKind) -> int:
        return int(getattr(self, kind.value))

    def add(self, kind: WorkKind, delta: int) -> None:
        setattr(self, kind.value, self.get(kind) + delta)

    def copy(self) -> _Row:
        return replace(self)

    def weights(self) -> dict[WorkKind, float]:
        return {kind: float(self.get(kind)) for kind in KINDS}

    @classmethod
    def from_hours(cls, row: HoursRow) -> _Row:
        return cls(
            lk=to_units(row.lk),
            pr=to_units(row.pr),
            lb=to_units(row.lb),
            srs=to_units(row.srs),
            distance=to_units(row.distance),
        )

    def to_hours(self) -> HoursRow:
        return HoursRow(
            lk=to_hours(self.lk),
            pr=to_hours(self.pr),
            lb=to_hours(self.lb),
            srs=to_hours(self.srs),
            distance=to_hours(self.distance),
        )


@dataclass
class _Run:
    """Общее состояние одного прогона: ограничения, журнал, конфликты."""

    c: HoursConstraints
    log: list[str] = field(default_factory=list)
    conflicts: list[ConstraintConflict] = field(default_factory=list)

    def conflict(self, code: str, message: str, **details: Any) -> None:
        self.conflicts.append(ConstraintConflict(code=code, message=message, details=details))
        self.log.append(f"Конфликт [{code}]: {message}")


def _mix_weights(mix: WorkMix) -> dict[WorkKind, float]:
    return {kind: float(getattr(mix, kind.value)) for kind in KINDS}


def _effective_mix(weights: dict[WorkKind, float], c: HoursConstraints) -> dict[WorkKind, Fraction]:
    """Доли видов работы после правил типа программы (§3.3)."""
    if c.srs_only:
        only_srs = {kind: Fraction(0) for kind in KINDS}
        only_srs[WorkKind.SRS] = Fraction(1)
        return only_srs
    w = {kind: max(Fraction(0), _frac(weights.get(kind, 0.0))) for kind in KINDS}
    if c.program_type == ProgramType.DOP:  # в ДОП нет лабораторных: их доля — практические занятия
        w[WorkKind.PR] += w[WorkKind.LB]
        w[WorkKind.LB] = Fraction(0)
    if c.sessions_program:  # все часы — занятия в расписании
        w[WorkKind.SRS] = Fraction(0)
    if sum(w.values()) == 0:
        w[WorkKind.LK] = Fraction(1)
        w[WorkKind.PR] = Fraction(1)
    return w


def _split(units: int, mix: dict[WorkKind, Fraction]) -> _Row:
    parts = hamilton(units, [mix[kind] for kind in KINDS])
    return _Row(lk=parts[0], pr=parts[1], lb=parts[2], srs=parts[3])


def _pr_share(mix: dict[WorkKind, Fraction] | None) -> Fraction:
    if not mix:
        return Fraction(0)
    total = sum(mix.values())
    return mix[WorkKind.PR] / total if total else Fraction(0)


# --------------------------------------------------------------------------------------------------
# Шаги солвера
# --------------------------------------------------------------------------------------------------


def _budget(run: _Run, fallback_fa: float | None = None) -> tuple[int, int]:
    """Объём и часы ИА в получасах (§3.2 п. 1)."""
    c = run.c
    total = c.total_hours
    if total <= 0 or not is_half_multiple(total):
        run.conflict(
            "total_invalid",
            f"Объём программы должен быть положительным и кратным 0,5 ч, а указано {fmt_hours(total)} ч. "
            "Уточните объём.",
            total_hours=total,
        )
    total_u = max(0, to_units(total))

    if c.fa_hours is not None:
        fa, origin = float(c.fa_hours), "задано"
    elif fallback_fa is not None:
        fa, origin = float(fallback_fa), "как в комплекте"
    else:
        fa, origin = c.default_fa_hours(), "по умолчанию"
    if fa < 0 or not is_half_multiple(fa):
        rounded = to_hours(max(0, to_units(max(fa, 0.0))))
        run.conflict(
            "fa_invalid",
            f"Часы итоговой аттестации должны быть кратны 0,5 ч, а указано {fmt_hours(fa)} ч; "
            f"взято {fmt_hours(rounded)} ч.",
            fa_hours=fa,
            keys=["FA"],
        )
        fa = rounded
    fa_u = to_units(fa)
    if fa_u > total_u:
        run.conflict(
            "fa_exceeds_total",
            f"Итоговая аттестация ({fmt_hours(fa)} ч) не помещается в объём программы "
            f"({fmt_hours(to_hours(total_u))} ч). Уточните часы аттестации или объём.",
            fa_hours=fa,
            total_hours=to_hours(total_u),
            keys=["FA"],
        )
        fa_u = total_u
    run.log.append(
        f"Итоговая аттестация — {fmt_hours(to_hours(fa_u))} ч ({origin}); на темы — "
        f"{fmt_hours(to_hours(total_u - fa_u))} ч из {fmt_hours(to_hours(total_u))} ч."
    )
    return total_u, fa_u


def _check_structure(modules: Sequence[ModuleSpec], topics: Sequence[TopicSpec]) -> None:
    module_keys = [m.key for m in modules]
    if len(set(module_keys)) != len(module_keys):
        raise ValueError(f"Повторяются ключи модулей: {module_keys}")
    topic_keys = [t.key for t in topics]
    if len(set(topic_keys)) != len(topic_keys):
        raise ValueError(f"Повторяются ключи тем: {topic_keys}")
    known = set(module_keys)
    unknown = [t.key for t in topics if t.module_key not in known]
    if unknown:
        raise ValueError(f"Темы ссылаются на неизвестные модули: {unknown}")


def _topic_min(c: HoursConstraints, unit: int, needs_lectures: bool = False) -> int:
    """Минимум темы в единицах распределения (получасах или занятиях).

    В модуле со слайдами тема не меньше 1 ч — иначе в неё не поместится обязательная лекция.
    """
    floor_u = max(0, to_units(c.min_topic_hours))
    if needs_lectures and not c.srs_only:
        floor_u = max(floor_u, to_units(LECTURE_MIN_HOURS))
    n = math.ceil(floor_u / unit)
    return max(n, 1) if unit > 1 else n


def _allocation_unit(run: _Run, topics: Sequence[TopicSpec], budget_u: int, total_u: int) -> int:
    """Единица деления тем: длина занятия (если программа из занятий и это возможно) или полчаса."""
    c = run.c
    if not c.session_hours or c.session_hours <= 0:
        return 1
    session = float(c.session_hours)
    if not is_half_multiple(session):
        run.conflict(
            "session_invalid",
            f"Длина занятия должна быть кратна 0,5 ч, а указано {fmt_hours(session)} ч. Уточните длину занятия.",
            session_hours=session,
        )
        return 1
    s_u = to_units(session)
    if c.srs_only:
        run.conflict(
            "sessions_vs_all_srs",
            f"Задана длина занятия {fmt_hours(session)} ч, но по программе нет аудиторных занятий "
            "(вся работа самостоятельная). Уточните: нужны ли занятия в расписании.",
            session_hours=session,
        )
        return 1
    if total_u % s_u:
        lower = (total_u // s_u) * s_u
        options = [to_hours(x) for x in (lower, lower + s_u) if x > 0]
        variants = " или ".join(f"{fmt_hours(x)} ч" for x in options)
        run.conflict(
            "sessions_indivisible",
            f"Объём {fmt_hours(to_hours(total_u))} ч не делится на занятия по {fmt_hours(session)} ч "
            f"(получается {fmt_hours(total_u / s_u)} занятия). Измените объём (например, {variants}) "
            "или длину занятия.",
            total_hours=to_hours(total_u),
            session_hours=session,
        )
        return 1
    n_sessions = budget_u // s_u
    if len(topics) > n_sessions:
        run.log.append(
            f"Тем ({len(topics)}) больше, чем занятий на темы ({n_sessions}): часы тем делятся по получасам, "
            "одно занятие может включать несколько тем."
        )
        return 1
    if any(t.locked is not None and _Row.from_hours(t.locked).total % s_u for t in topics):
        run.log.append("Зафиксированные строки не кратны длине занятия: часы тем делятся по получасам.")
        return 1
    rest = budget_u % s_u
    tail = f", ещё {fmt_hours(to_hours(rest))} ч темы — в одном занятии с аттестацией" if rest else ""
    run.log.append(
        f"Программа из занятий по {fmt_hours(session)} ч: всего {total_u // s_u}, на темы — {n_sessions}{tail}."
    )
    return s_u


def _allocate_structure(
    run: _Run, modules: Sequence[ModuleSpec], topics: Sequence[TopicSpec], budget_u: int, unit: int
) -> dict[str, int]:
    """Часы тем (в получасах): модули по весам с минимумами → темы по весам с минимумами."""
    c = run.c
    by_module = {m.key: [t for t in topics if t.module_key == m.key] for m in modules}
    locked_u = {t.key: _Row.from_hours(t.locked).total for t in topics if t.locked is not None}
    result: dict[str, int] = dict(locked_u)
    total_locked = sum(locked_u.values())
    if total_locked > budget_u:
        run.conflict(
            "locked_exceeds_budget",
            f"Зафиксированные вручную строки занимают {fmt_hours(to_hours(total_locked))} ч, а на темы программы "
            f"есть только {fmt_hours(to_hours(budget_u))} ч (без итоговой аттестации). "
            "Уменьшите часы зафиксированных строк или снимите фиксацию.",
            locked_hours=to_hours(total_locked),
            budget_hours=to_hours(budget_u),
            keys=sorted(locked_u),
        )
        for t in topics:
            result.setdefault(t.key, 0)
        return result

    budget_n, remainder_u = divmod(budget_u, unit)
    fixed = [m for m in modules if all(t.locked is not None for t in by_module[m.key])]
    flex = [m for m in modules if m not in fixed]
    fixed_n = sum(locked_u[t.key] for m in fixed for t in by_module[m.key]) // unit
    flex_budget = budget_n - fixed_n
    if not flex:
        if flex_budget or remainder_u:
            run.conflict(
                "locked_mismatch",
                f"Все темы зафиксированы, их сумма {fmt_hours(to_hours(total_locked))} ч, а на темы нужно ровно "
                f"{fmt_hours(to_hours(budget_u))} ч. Снимите фиксацию хотя бы с одной темы.",
                locked_hours=to_hours(total_locked),
                budget_hours=to_hours(budget_u),
                keys=sorted(locked_u),
            )
        return result

    module_min_n = math.ceil(max(0, to_units(c.min_module_hours)) / unit)

    def locked_n(m: ModuleSpec) -> int:
        return sum(locked_u[t.key] for t in by_module[m.key] if t.locked is not None) // unit

    def free_count(m: ModuleSpec) -> int:
        return sum(1 for t in by_module[m.key] if t.locked is None)

    def topic_floor(m: ModuleSpec, level: int) -> int:
        """Уровень 0 — полные минимумы; 1 — хотя бы одна единица на тему; 2 — без минимумов."""
        full = _topic_min(c, unit, m.needs_lectures)
        return (full, min(full, 1), 0)[level]

    def minimums(level: int) -> list[int]:
        out = []
        for m in flex:
            base = locked_n(m) + free_count(m) * topic_floor(m, level)
            out.append(max(module_min_n, base) if level == 0 else base)
        return out

    level = 0
    m_mins = minimums(level)
    if sum(m_mins) > flex_budget:
        need_u = (sum(m_mins) + fixed_n) * unit
        per_topic = (
            f"одно занятие ({fmt_hours(to_hours(unit))} ч)"
            if unit > 1
            else f"не меньше {fmt_hours(c.min_topic_hours)} ч"
        )
        run.conflict(
            "minimums_exceed_budget",
            f"Часов не хватает на все модули и темы: по минимуму (модуль — не меньше "
            f"{fmt_hours(c.min_module_hours)} ч, тема — {per_topic}) нужно {fmt_hours(to_hours(need_u))} ч, "
            f"а на темы остаётся {fmt_hours(to_hours(budget_u))} ч. Сократите число модулей и тем "
            "или увеличьте объём; пока часы разложены без этих минимумов.",
            need_hours=to_hours(need_u),
            budget_hours=to_hours(budget_u),
            keys=[m.key for m in flex],
        )
        level = 1
        m_mins = minimums(level)
        if sum(m_mins) > flex_budget:
            level = 2
            m_mins = minimums(level)

    module_n = hamilton(flex_budget, [m.weight for m in flex], m_mins)
    for m, n in zip(flex, module_n, strict=True):
        free_topics = [t for t in by_module[m.key] if t.locked is None]
        free_n = n - locked_n(m)
        floor_n = topic_floor(m, level)
        parts = hamilton(free_n, [t.weight for t in free_topics], [floor_n] * len(free_topics))
        for t, part in zip(free_topics, parts, strict=True):
            result[t.key] = part * unit
    module_text = ", ".join(
        f"{m.label} — {fmt_hours(to_hours(n * unit))} ч" for m, n in zip(flex, module_n, strict=True)
    )
    run.log.append(f"Модули по весам: {module_text}.")

    if remainder_u:
        last_free = [t for t in topics if t.locked is None][-1]
        result[last_free.key] += remainder_u
        run.log.append(
            f"Последняя тема «{last_free.label}» получила ещё {fmt_hours(to_hours(remainder_u))} ч "
            "(делит последнее занятие с итоговой аттестацией)."
        )
    return result


def _ensure_lectures(
    run: _Run, rows: dict[str, _Row], order: Sequence[str], lecture_topics: dict[str, str], free: set[str]
) -> None:
    """В каждой теме модуля со слайдами ЛК ≥ 1 ч (перенос из СРС, затем ПР, затем ЛБ)."""
    c = run.c
    min_lk = to_units(LECTURE_MIN_HOURS)
    reported: set[str] = set()
    moved = 0
    for key in order:
        module_label = lecture_topics.get(key)
        if module_label is None:
            continue
        row = rows[key]
        if c.srs_only:
            if module_label not in reported:
                reported.add(module_label)
                run.conflict(
                    "lectures_vs_all_srs",
                    f"Для модуля «{module_label}» запрошены слайды к лекциям, но по программе нет аудиторных "
                    "занятий (вся работа самостоятельная), поэтому лекций в нём нет. Уточните, нужны ли слайды.",
                    keys=[key],
                )
            continue
        if row.lk >= min_lk:
            continue
        if key not in free:
            run.log.append(f"Тема {key} зафиксирована вручную: лекций в ней меньше 1 ч, строка не меняется.")
            continue
        need = min_lk - row.lk
        if row.total < min_lk:
            run.conflict(
                "lectures_impossible",
                f"В модуле «{module_label}» нужны лекции (по нему готовятся слайды), но тема {key} занимает "
                f"всего {fmt_hours(to_hours(row.total))} ч — лекция не меньше 1 ч в неё не помещается.",
                keys=[key],
            )
            need = row.total - row.lk
        for kind in (WorkKind.SRS, WorkKind.PR, WorkKind.LB):
            take = min(need, row.get(kind))
            row.add(kind, -take)
            row.lk += take
            need -= take
            moved += take
    if moved:
        run.log.append(f"Модули со слайдами: в лекции перенесено {fmt_hours(to_hours(moved))} ч.")


def _ensure_practice(
    run: _Run,
    rows: dict[str, _Row],
    mixes: dict[str, dict[WorkKind, Fraction]],
    groups: Sequence[Sequence[str]],
    total_u: int,
    lk_floor: dict[str, int],
) -> None:
    """Σ ПР ≥ min_practice_share × объём: по полчаса из ЛК, затем из СРС, в теме с наибольшим дефицитом."""
    c = run.c
    share = c.min_practice_share or 0.0
    if share <= 0:
        return
    need = math.ceil(_frac(share) * total_u)
    if c.srs_only:
        run.conflict(
            "all_srs_vs_practice",
            "По программе вся работа самостоятельная (аудиторных занятий нет), поэтому требование "
            f"«практических занятий не меньше {_percent(share)} % часов» выполнить нельзя. Уточните, что важнее: "
            "формат без аудиторных занятий или доля практики.",
            min_practice_share=share,
            need_hours=to_hours(need),
        )
        return
    have = sum(row.pr for row in rows.values())
    if have >= need:
        run.log.append(
            f"Практика: {fmt_hours(to_hours(have))} ч при минимуме {fmt_hours(to_hours(need))} ч — поправка не нужна."
        )
        return
    moved = {WorkKind.LK: 0, WorkKind.SRS: 0}

    def donor(key: str) -> WorkKind | None:
        row = rows[key]
        if row.lk > lk_floor.get(key, 0):
            return WorkKind.LK
        return WorkKind.SRS if row.srs > 0 else None

    def deficit(key: str) -> Fraction:
        """Насколько доля ПР в теме ниже доли из mix (больше — нужнее практика)."""
        row = rows[key]
        return target[key] - Fraction(row.pr, row.total)

    for group in groups:
        # Куча по (−дефицит, порядок): у темы меняется только её собственный дефицит, остальные ключи верны.
        order = {key: i for i, key in enumerate(group)}
        target = {key: _pr_share(mixes.get(key)) for key in group}
        heap = [(-deficit(key), order[key], key) for key in group if rows[key].total and donor(key)]
        heapq.heapify(heap)
        while have < need and heap:
            _, _, key = heapq.heappop(heap)
            kind = donor(key)
            if kind is None:
                continue
            rows[key].add(kind, -1)
            rows[key].pr += 1
            moved[kind] += 1
            have += 1
            if donor(key) is not None:
                heapq.heappush(heap, (-deficit(key), order[key], key))
    run.log.append(
        f"Доля практики: в практические занятия перенесено {fmt_hours(to_hours(moved[WorkKind.LK]))} ч из лекций "
        f"и {fmt_hours(to_hours(moved[WorkKind.SRS]))} ч из самостоятельной работы."
    )
    if have < need:
        run.conflict(
            "practice_share_unreachable",
            f"Практических занятий нужно не меньше {fmt_hours(to_hours(need))} ч ({_percent(share)} % от "
            f"{fmt_hours(to_hours(total_u))} ч), а получилось только {fmt_hours(to_hours(have))} ч: остальные часы "
            "зафиксированы вручную, отведены под лабораторные работы или обязательные лекции. Уточните требования.",
            need_hours=to_hours(need),
            got_hours=to_hours(have),
        )


def _assign_distance(run: _Run, rows: dict[str, _Row], groups: Sequence[Sequence[str]]) -> None:
    """ДОТ: доля контактной работы раздаётся Гамильтоном — сначала по ЛК, затем по ПР, затем по ЛБ."""
    c = run.c
    adjustable_all = [key for group in groups for key in group]
    if not c.dot:
        for key in adjustable_all:
            rows[key].distance = 0
        return
    share = c.resolved_distance_share()
    contact = sum(row.contact for row in rows.values())
    if contact == 0:
        for key in adjustable_all:
            rows[key].distance = 0
        run.log.append("ДОТ: контактных часов нет — дистанционную часть распределять не из чего.")
        return
    target = math.floor(_frac(share) * contact + Fraction(1, 2))
    if 0 < share < 1 and contact >= 2:
        target = min(max(target, 1), contact - 1)

    adjustable: list[str] = []
    need = 0
    for i in range(len(groups)):
        adjustable = [key for group in groups[: i + 1] for key in group]
        chosen = set(adjustable)
        fixed = sum(min(row.distance, row.contact) for key, row in rows.items() if key not in chosen)
        need = target - fixed
        capacity = sum(rows[key].contact for key in adjustable)
        if 0 <= need <= capacity:
            break
    capacity = sum(rows[key].contact for key in adjustable)
    need = min(max(need, 0), capacity)
    for key in adjustable:
        rows[key].distance = 0
    for kind in (WorkKind.LK, WorkKind.PR, WorkKind.LB):
        pool = [rows[key].get(kind) for key in adjustable]
        take = min(need, sum(pool))
        if take <= 0:
            continue
        for key, part in zip(adjustable, hamilton(take, pool), strict=True):
            rows[key].distance += part
        need -= take
    distance = sum(row.distance for row in rows.values())
    run.log.append(
        f"ДОТ: {fmt_hours(to_hours(distance))} ч из {fmt_hours(to_hours(contact))} ч контактной работы "
        f"(цель — {_percent(share)} %), очно — {fmt_hours(to_hours(contact - distance))} ч."
    )


def _locked_row(run: _Run, key: str, row: HoursRow) -> _Row:
    """Зафиксированная методистом строка. Некорректные числа не принимаются молча: конфликт и ближайшая
    допустимая строка (шаг 0,5; ДОТ не больше контактной работы)."""
    values = [row.lk, row.pr, row.lb, row.srs, row.distance]
    result = _Row.from_hours(row)
    if not all(is_half_multiple(v) for v in values):
        run.conflict(
            "locked_invalid",
            f"В строке {key}, зафиксированной вручную, часы должны быть кратны 0,5 ч; значения округлены.",
            keys=[key],
        )
    if result.distance > result.contact:
        run.conflict(
            "locked_invalid",
            f"В строке {key}, зафиксированной вручную, часов с ДОТ ({fmt_hours(to_hours(result.distance))}) "
            f"больше, чем контактных ({fmt_hours(to_hours(result.contact))}); взято {fmt_hours(to_hours(result.contact))} ч.",
            keys=[key],
        )
        result.distance = result.contact
    return result


def _final_check(run: _Run, rows: dict[str, _Row], fa_u: int, total_u: int) -> None:
    for key, row in rows.items():
        values = (row.lk, row.pr, row.lb, row.srs, row.distance)
        if min(values) < 0 or row.distance > row.contact:
            raise AssertionError(f"Солвер получил некорректную строку {key}: {row}")
    got = sum(row.total for row in rows.values()) + fa_u
    if got != total_u and not run.conflicts:
        run.conflict(
            "sum_mismatch",
            f"Сумма часов ({fmt_hours(to_hours(got))} ч) не совпала с объёмом ({fmt_hours(to_hours(total_u))} ч).",
            got_hours=to_hours(got),
            total_hours=to_hours(total_u),
        )


# --------------------------------------------------------------------------------------------------
# Публичные функции
# --------------------------------------------------------------------------------------------------


def solve_hours(
    modules: list[ModuleSpec], topics: list[TopicSpec], constraints: HoursConstraints
) -> SolverResult:
    """Часы каждой темы и ИА по весам каркаса и ограничениям запроса (§3.2)."""
    run = _Run(constraints)
    _check_structure(modules, topics)
    total_u, fa_u = _budget(run)
    budget_u = total_u - fa_u
    if not topics:
        run.conflict("no_topics", "В каркасе нет ни одной темы — часы распределять не по чему.")
        return SolverResult(topic_hours={}, fa_hours=to_hours(fa_u), log=run.log, conflicts=run.conflicts)

    position = {m.key: i for i, m in enumerate(modules)}
    ordered = sorted(topics, key=lambda t: position[t.module_key])
    active = [m for m in modules if any(t.module_key == m.key for t in ordered)]
    for m in modules:
        if m not in active:
            run.log.append(f"Модуль «{m.label}» без тем — часов не получает.")

    unit = _allocation_unit(run, ordered, budget_u, total_u)
    topic_units = _allocate_structure(run, active, ordered, budget_u, unit)

    mixes: dict[str, dict[WorkKind, Fraction]] = {}
    rows: dict[str, _Row] = {}
    for t in ordered:
        if t.locked is not None:
            rows[t.key] = _locked_row(run, t.key, t.locked)
            continue
        mixes[t.key] = _effective_mix(_mix_weights(t.mix), constraints)
        rows[t.key] = _split(topic_units[t.key], mixes[t.key])

    keys = [t.key for t in ordered]
    free = [t.key for t in ordered if t.locked is None]
    module_by_key = {m.key: m for m in modules}
    lecture_topics = {
        t.key: module_by_key[t.module_key].label
        for t in ordered
        if module_by_key[t.module_key].needs_lectures
    }
    _ensure_lectures(run, rows, keys, lecture_topics, set(free))
    floors = {key: to_units(LECTURE_MIN_HOURS) for key in lecture_topics}
    _ensure_practice(run, rows, mixes, [free], total_u, floors)
    _assign_distance(run, rows, [free])
    _final_check(run, rows, fa_u, total_u)

    module_hours = {
        m.key: to_hours(sum(rows[t.key].total for t in ordered if t.module_key == m.key)) for m in modules
    }
    return SolverResult(
        topic_hours={key: rows[key].to_hours() for key in keys},
        fa_hours=to_hours(fa_u),
        log=run.log,
        conflicts=run.conflicts,
        module_hours=module_hours,
    )


def _kit_topic_order(kit: Kit) -> list[Topic]:
    ordered: list[Topic] = []
    for module in kit.modules_sorted():
        ordered.extend(kit.topics_of(module.id))
    seen = {t.id for t in ordered}
    ordered.extend(t for t in sorted(kit.topics, key=lambda t: (t.order, t.id)) if t.id not in seen)
    return ordered


def _distribute(
    run: _Run, keys: Sequence[str], budget_u: int, current: dict[str, _Row], lecture_keys: set[str]
) -> dict[str, int]:
    """Новые суммы тем: веса = текущие часы, минимум темы; при занятиях — целыми занятиями, если возможно."""
    c = run.c
    weights = [current[key].total for key in keys]
    unit = 1
    if c.sessions_program and c.session_hours and is_half_multiple(c.session_hours):
        s_u = to_units(c.session_hours)
        if budget_u % s_u == 0 and all(w % s_u == 0 for w in weights) and len(keys) <= budget_u // s_u:
            unit = s_u
    budget_n = budget_u // unit
    full = [_topic_min(c, unit, key in lecture_keys) for key in keys]
    mins = list(full)
    if sum(mins) > budget_n:
        run.conflict(
            "minimums_exceed_budget",
            f"На {len(keys)} {plural(len(keys), 'тему', 'темы', 'тем')} остаётся "
            f"{fmt_hours(to_hours(budget_u))} ч — меньше минимума ({fmt_hours(c.min_topic_hours)} ч на тему). "
            "Уменьшите часы зафиксированных строк или объедините темы; пока часы разложены без минимума.",
            budget_hours=to_hours(budget_u),
            keys=list(keys),
        )
        mins = [min(n, 1) for n in full]
        if sum(mins) > budget_n:
            mins = [0] * len(keys)
    parts = hamilton(budget_n, weights, mins)
    return {key: part * unit for key, part in zip(keys, parts, strict=True)}


def rebalance(
    kit: Kit,
    locked: dict[str, HoursRow],
    constraints: HoursConstraints,
    scope: Literal["module", "program"] = "module",
) -> SolverResult:
    """Ручная правка часов (§3.5): зафиксированные строки не меняются, остальное перераспределяется
    в пределах модуля (scope="module") или всей программы (scope="program") так, чтобы сумма снова была
    ровно равна объёму. Веса — текущие часы тем, доли видов работы — текущий состав строки.
    Если все темы модуля зафиксированы, его разница уходит в незатронутые модули программы.
    """
    if scope not in ("module", "program"):
        raise ValueError(f"Неизвестная область перераспределения: {scope!r}")
    run = _Run(constraints)
    fallback_fa = kit.final_attestation.hours if kit.final_attestation is not None else None
    total_u, fa_u = _budget(run, fallback_fa)
    budget_u = total_u - fa_u

    order = _kit_topic_order(kit)
    ids = [t.id for t in order]
    unknown = sorted(set(locked) - set(ids))
    if unknown:
        raise ValueError(f"В комплекте нет тем {unknown}")
    module_of = {t.id: t.module_id for t in order}
    module_title = {m.id: m.title for m in kit.modules}
    lecture_modules = {m.id: m.title for m in kit.modules if m.needs_lectures}
    lecture_topics = {
        tid: lecture_modules[module_of[tid]] for tid in ids if module_of[tid] in lecture_modules
    }
    current = {t.id: _Row.from_hours(t.hours) for t in order}
    rows = {tid: current[tid].copy() for tid in ids}
    for tid, row in locked.items():
        rows[tid] = _locked_row(run, tid, row)
    free = [tid for tid in ids if tid not in locked]
    locked_total = sum(rows[tid].total for tid in locked)

    current_sum = sum(row.total for row in current.values())
    if scope == "module" and current_sum != budget_u:
        run.log.append(
            f"Сумма тем в комплекте ({fmt_hours(to_hours(current_sum))} ч) не равна объёму без ИА "
            f"({fmt_hours(to_hours(budget_u))} ч): часы перераспределяются по всей программе."
        )
        scope = "program"

    new_totals: dict[str, int] = {}
    in_scope: list[str] = []
    if scope == "program":
        group_budget = budget_u - locked_total
        if group_budget < 0:
            run.conflict(
                "locked_exceeds_budget",
                f"Зафиксированные строки занимают {fmt_hours(to_hours(locked_total))} ч, а на темы программы есть "
                f"только {fmt_hours(to_hours(budget_u))} ч (без итоговой аттестации). Уменьшите часы этих строк.",
                locked_hours=to_hours(locked_total),
                budget_hours=to_hours(budget_u),
                keys=sorted(locked),
            )
        elif not free:
            if group_budget:
                run.conflict(
                    "locked_mismatch",
                    f"Все темы зафиксированы, их сумма {fmt_hours(to_hours(locked_total))} ч, а на темы нужно ровно "
                    f"{fmt_hours(to_hours(budget_u))} ч. Снимите фиксацию хотя бы с одной темы.",
                    keys=sorted(locked),
                )
        else:
            new_totals.update(_distribute(run, free, group_budget, current, set(lecture_topics)))
            in_scope = list(free)
        run.log.append("Перераспределение по всей программе.")
    else:
        affected = list(dict.fromkeys(module_of[tid] for tid in ids if tid in locked))
        pool_delta = 0
        for mid in affected:
            module_topics = [tid for tid in ids if module_of[tid] == mid]
            module_free = [tid for tid in module_topics if tid not in locked]
            locked_m = sum(rows[tid].total for tid in module_topics if tid in locked)
            current_m = sum(current[tid].total for tid in module_topics)
            label = module_title.get(mid, mid)
            if not module_free:
                pool_delta += locked_m - current_m
                run.log.append(f"Все темы модуля «{label}» зафиксированы: разница уходит в другие модули.")
                continue
            free_budget = current_m - locked_m
            if free_budget < 0:
                run.conflict(
                    "locked_exceeds_module",
                    f"В модуле «{label}» зафиксированные строки занимают {fmt_hours(to_hours(locked_m))} ч, "
                    f"а весь модуль — {fmt_hours(to_hours(current_m))} ч. Уменьшите часы этих строк "
                    "или перераспределите часы по всей программе.",
                    locked_hours=to_hours(locked_m),
                    module_hours=to_hours(current_m),
                    keys=[mid, *[tid for tid in module_topics if tid in locked]],
                )
                continue
            new_totals.update(_distribute(run, module_free, free_budget, current, set(lecture_topics)))
            in_scope.extend(module_free)
            run.log.append(
                f"Перераспределение в пределах модуля «{label}» ({fmt_hours(to_hours(current_m))} ч)."
            )
        if pool_delta:
            others = [tid for tid in free if module_of[tid] not in affected]
            pool_budget = sum(current[tid].total for tid in others) - pool_delta
            if not others or pool_budget < 0:
                run.conflict(
                    "locked_exceeds_budget",
                    (
                        f"Зафиксированные модули выросли на {fmt_hours(to_hours(pool_delta))} ч, а забрать эти часы "
                        "у других модулей нельзя. Уменьшите часы зафиксированных строк."
                    )
                    if pool_delta > 0
                    else (
                        f"Зафиксированные модули уменьшились на {fmt_hours(to_hours(-pool_delta))} ч, а отдать эти "
                        "часы другим модулям нельзя. Увеличьте часы зафиксированных строк."
                    ),
                    delta_hours=to_hours(pool_delta),
                    keys=sorted(locked),
                )
            else:
                new_totals.update(_distribute(run, others, pool_budget, current, set(lecture_topics)))
                in_scope.extend(others)

    mixes = {tid: _effective_mix(current[tid].weights(), constraints) for tid in free}
    for tid, units in new_totals.items():
        if units != current[tid].total:
            rows[tid] = _split(units, mixes[tid])
    scoped = set(in_scope)
    other_free = [tid for tid in free if tid not in scoped]

    _ensure_lectures(run, rows, ids, lecture_topics, set(free))
    floors = {tid: to_units(LECTURE_MIN_HOURS) for tid in lecture_topics}
    _ensure_practice(run, rows, mixes, [in_scope, other_free], total_u, floors)
    _assign_distance(run, rows, [in_scope, other_free])
    _final_check(run, rows, fa_u, total_u)

    module_hours = {
        m.id: to_hours(sum(rows[tid].total for tid in ids if module_of[tid] == m.id))
        for m in kit.modules_sorted()
    }
    return SolverResult(
        topic_hours={tid: rows[tid].to_hours() for tid in ids},
        fa_hours=to_hours(fa_u),
        log=run.log,
        conflicts=run.conflicts,
        module_hours=module_hours,
    )


def apply_hours(kit: Kit, result: SolverResult, key_to_id: dict[str, str] | None = None) -> None:
    """Записывает часы в комплект: Topic.hours по ключам (через key_to_id) и FinalAttestation.hours."""
    by_id = {t.id: t for t in kit.topics}
    for key, row in result.topic_hours.items():
        topic_id = key_to_id.get(key, key) if key_to_id else key
        topic = by_id.get(topic_id)
        if topic is None:
            raise KeyError(f"В комплекте нет темы {topic_id} (ключ солвера {key!r})")
        topic.hours = row.model_copy()
    if kit.final_attestation is not None:
        kit.final_attestation.hours = result.fa_hours
