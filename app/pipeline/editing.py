"""Правка каркаса методистом до утверждения: тексты, часы тем (rebalance), пересчёт календаря.

Без LLM. Правка применяется к копии комплекта и возвращается целиком — или не применяется вовсе.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.calendar import build_calendar
from app.core.hours import HoursConstraints, SolverResult, apply_hours, constraints_from_request, rebalance
from app.core.ids import AmbiguousReference, UnknownReference, parse_id, resolve_ref
from app.models import HoursRow, Kit, MethodistWarning, WarningType
from app.pipeline.base import EditConflict, TargetError

HoursScope = Literal["module", "program"]

# Предупреждения, которые пересчитываются при каждой правке часов (старые из прошлого отчёта не переносим).
RECOMPUTED_WARNINGS = (WarningType.SCHEDULE_TIGHT, WarningType.CONSTRAINT_CONFLICT)


class HoursInput(BaseModel):
    """Часы строки, заданные методистом вручную. Шаг 0,5 ч."""

    model_config = ConfigDict(extra="forbid")

    lk: float = Field(default=0, ge=0, le=10000)
    pr: float = Field(default=0, ge=0, le=10000)
    lb: float = Field(default=0, ge=0, le=10000)
    srs: float = Field(default=0, ge=0, le=10000)
    distance: float = Field(default=0, ge=0, le=10000)

    @field_validator("lk", "pr", "lb", "srs", "distance")
    @classmethod
    def _half_step(cls, value: float) -> float:
        if abs(value * 2 - round(value * 2)) > 1e-9:
            raise ValueError("часы задаются с шагом 0,5")
        return value

    def to_row(self) -> HoursRow:
        return HoursRow(lk=self.lk, pr=self.pr, lb=self.lb, srs=self.srs, distance=self.distance)


class _Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=50)


class CompetencyEdit(_Edit):
    title: str | None = Field(default=None, min_length=1, max_length=1000)
    activity: str | None = Field(default=None, max_length=1000)


class OutcomeEdit(_Edit):
    text: str | None = Field(default=None, min_length=1, max_length=1000)


class ModuleEdit(_Edit):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    control_form: str | None = Field(default=None, max_length=500)


class TopicEdit(_Edit):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    control_form: str | None = Field(default=None, max_length=500)
    hours: HoursInput | None = None


class SkeletonEdits(BaseModel):
    """Правки каркаса из редактора. ID объектов — как в каркасе (T2.3) или по-человечески («тема 2.3»)."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=500)
    goal: str | None = Field(default=None, min_length=1, max_length=4000)
    tasks: list[str] | None = Field(default=None, max_length=30)
    competencies: list[CompetencyEdit] = Field(default_factory=list, max_length=100)
    outcomes: list[OutcomeEdit] = Field(default_factory=list, max_length=300)
    modules: list[ModuleEdit] = Field(default_factory=list, max_length=100)
    topics: list[TopicEdit] = Field(default_factory=list, max_length=300)
    hours_scope: HoursScope = "module"  # где перераспределять остаток при правке часов

    def is_empty(self) -> bool:
        scalars = (self.title, self.goal, self.tasks)
        lists = (self.competencies, self.outcomes, self.modules, self.topics)
        return all(v is None for v in scalars) and not any(lists)


# --------------------------------------------------------------------------------------------------
# Часы
# --------------------------------------------------------------------------------------------------


def kit_constraints(kit: Kit) -> HoursConstraints:
    """Ограничения солвера для готового каркаса: из запроса, объём — из паспорта, если в запросе его нет."""
    request = kit.request
    if request.total_hours is None:
        request = request.model_copy(update={"total_hours": kit.program.total_hours})
    return constraints_from_request(request, kit.program.type)


def resolve_object(kit: Kit, ref: str, prefix: str, what: str) -> str:
    """Ссылка методиста → ID объекта нужного вида. Ошибки — TargetError с понятным текстом."""
    try:
        obj_id = resolve_ref(kit, ref)
    except AmbiguousReference as exc:
        raise TargetError(str(exc)) from exc
    except UnknownReference as exc:
        raise TargetError(str(exc)) from exc
    kind = "FA" if obj_id == "FA" else parse_id(obj_id)[0]
    if kind != prefix:
        raise TargetError(f"«{ref}» — это не {what}.")
    return obj_id


def preview_rebalance(kit: Kit, locked: dict[str, HoursRow], scope: HoursScope = "module") -> SolverResult:
    """Что станет с часами, если зафиксировать строки locked (ключи — ID тем). Комплект не меняется."""
    try:
        return rebalance(kit, locked, kit_constraints(kit), scope=scope)
    except ValueError as exc:
        raise TargetError(str(exc)) from exc


def conflict_details(result: SolverResult) -> list[dict[str, object]]:
    return [
        {"code": c.code, "message": c.message, "target_ids": [str(k) for k in c.details.get("keys", [])]}
        for c in result.conflicts
    ]


# --------------------------------------------------------------------------------------------------
# Правка каркаса
# --------------------------------------------------------------------------------------------------


def apply_skeleton_edits(kit: Kit, edits: SkeletonEdits) -> tuple[Kit, list[str], list[MethodistWarning]]:
    """Применяет правки к копии каркаса. Возвращает (новый комплект, изменённые ID, предупреждения календаря).

    Бросает TargetError (нет такого объекта) или EditConflict (часы не сходятся) — тогда ничего не меняется.
    """
    work = kit.model_copy(deep=True)
    changed: list[str] = []

    if edits.title is not None:
        work.program.title = edits.title.strip()
    if edits.goal is not None:
        work.program.goal = edits.goal.strip()
    if edits.tasks is not None:
        work.program.tasks = [t.strip() for t in edits.tasks if t.strip()]

    by_id = work.index()
    for edit in edits.competencies:
        obj = by_id[resolve_object(work, edit.id, "C", "компетенция")]
        _set(obj, "title", edit.title)
        _set(obj, "activity", edit.activity)
        changed.append(obj.id)
    for edit in edits.outcomes:
        obj = by_id[resolve_object(work, edit.id, "LO", "результат обучения")]
        _set(obj, "text", edit.text)
        changed.append(obj.id)
    for edit in edits.modules:
        obj = by_id[resolve_object(work, edit.id, "M", "модуль")]
        _set(obj, "title", edit.title)
        _set(obj, "control_form", edit.control_form)
        changed.append(obj.id)

    locked: dict[str, HoursRow] = {}
    for edit in edits.topics:
        topic_id = resolve_object(work, edit.id, "T", "тема")
        obj = by_id[topic_id]
        _set(obj, "title", edit.title)
        _set(obj, "control_form", edit.control_form)
        if edit.hours is not None:
            locked[topic_id] = edit.hours.to_row()
        changed.append(topic_id)

    warnings: list[MethodistWarning] = []
    if locked:
        result = preview_rebalance(work, locked, edits.hours_scope)
        if not result.ok:
            text = " ".join(c.message for c in result.conflicts)
            raise EditConflict(f"Часы не сходятся: {text}", details=conflict_details(result))
        apply_hours(work, result)
        calendar, warnings = build_calendar(work, kit_constraints(work))
        work.calendar = calendar
        if work.report is not None:
            work.report.warnings_for_methodist = [
                w for w in work.report.warnings_for_methodist if w.type not in RECOMPUTED_WARNINGS
            ]
        changed += [tid for tid, row in result.topic_hours.items() if _hours_changed(kit, tid, row)]

    return work, list(dict.fromkeys(changed)), warnings


def _set(obj: object, name: str, value: str | None) -> None:
    if value is not None:
        setattr(obj, name, value.strip())


def _hours_changed(kit: Kit, topic_id: str, row: HoursRow) -> bool:
    old = next((t.hours for t in kit.topics if t.id == topic_id), None)
    return old is None or old.model_dump() != row.model_dump()
