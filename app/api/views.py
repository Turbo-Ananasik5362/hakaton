"""Сборка ответов API из состояния сессии и комплекта. Формат — docs/api_contract.md."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.core.hours import SolverResult
from app.core.ids import display_numbers, id_number_safe
from app.models import HoursRow, Kit, ReportStatus, SessionStatus
from app.pipeline.editing import conflict_details
from app.render.kit_json import kit_export
from app.render.report_md import api_message
from app.storage.sessions import FINISHED_STEP_STATUSES, SessionState, StepStatus

from .schemas import (
    CompetencyOut,
    ModuleOut,
    OutcomeOut,
    ProgressOut,
    RebalanceOut,
    RebalanceTopicOut,
    SkeletonOut,
    StepOut,
    TopicOut,
)

API_PREFIX = "/api/v1"

# Когда в ответе показывать каркас и когда — готовый комплект.
_SKELETON_STATUSES = (SessionStatus.SKELETON_READY, SessionStatus.QUEUED, SessionStatus.GENERATING)
_KIT_STATUSES = (SessionStatus.DONE, SessionStatus.PATCHING, SessionStatus.FAILED)


def session_payload(
    state: SessionState, kit: Kit | None, queue_position: int | None = None
) -> dict[str, Any]:
    status = state.status
    progress = progress_view(state)
    out: dict[str, Any] = {
        "session_id": state.id,
        "status": status.value,
        "message": status_message(state, kit, progress, queue_position),
        "created_at": state.created_at.isoformat(),
        "updated_at": state.updated_at.isoformat(),
    }
    if kit is not None:
        out["revision"] = kit.revision
    if status == SessionStatus.NEED_CLARIFICATION:
        out["questions"] = [q.model_dump(mode="json") for q in state.questions]
    if kit is not None and status in _SKELETON_STATUSES:
        out["skeleton"] = skeleton_view(kit)
        if kit.report is not None:
            out["validation"] = kit.report.model_dump(mode="json")
    if state.progress:
        out["progress"] = progress.model_dump(mode="json")
    if status == SessionStatus.QUEUED and queue_position is not None:
        out["queue_position"] = queue_position
    if kit is not None and status in _KIT_STATUSES and (status != SessionStatus.FAILED or state.approved):
        out["kit"] = kit_export(kit)
        if kit.report is not None:
            out["report"] = kit.report.model_dump(mode="json")
    if state.files:
        out["files"] = file_urls(state)
    if kit is not None and kit.report is not None and status == SessionStatus.DONE:
        out["report_page"] = report_page_url(state.id)  # отчёт для методиста в браузере (HTML)
    if state.last_patch is not None:
        out["patch"] = state.last_patch.model_dump(mode="json", exclude={"instruction", "at"})
        if state.last_patch.ok:
            out["changed_ids"] = list(state.last_patch.changed_ids)
    if state.notes:
        out["notes"] = list(state.notes)
    if status == SessionStatus.FAILED:
        out["error"] = {"code": state.error_code or "failed", "message": state.error or "Работа остановлена."}
    return out


def report_page_url(session_id: str) -> str:
    return f"/sessions/{session_id}/report"


def file_urls(state: SessionState) -> dict[str, str]:
    return {key: f"{API_PREFIX}/sessions/{state.id}/files/{quote(name)}" for key, name in state.files.items()}


def progress_view(state: SessionState) -> ProgressOut:
    steps = [
        StepOut(
            name=p.name,
            title=p.title,
            phase=p.phase,
            status=p.status.value,
            seconds=p.seconds,
            error=p.error,
        )
        for p in state.progress
    ]
    return ProgressOut(
        done=sum(1 for p in state.progress if p.status in FINISHED_STEP_STATUSES),
        total=len(state.progress),
        current=[p.title for p in state.progress if p.status == StepStatus.RUNNING],
        steps=steps,
    )


def status_message(
    state: SessionState, kit: Kit | None, progress: ProgressOut, queue_position: int | None
) -> str:
    sid = state.id
    status = state.status
    if status == SessionStatus.NEW:
        current = f": {', '.join(progress.current).lower()}" if progress.current else ""
        return f"Запрос разбирается{current}."
    if status == SessionStatus.NEED_CLARIFICATION:
        return (
            f"Нужны ответы на уточняющие вопросы ({len(state.questions)}): "
            f"POST {API_PREFIX}/sessions/{sid}/answers."
        )
    if status == SessionStatus.SKELETON_READY:
        text = f"Каркас готов. Проверьте его и утвердите: POST {API_PREFIX}/sessions/{sid}/approve."
        return text + _report_hint(kit, "validation")
    if status == SessionStatus.QUEUED:
        place = f" (место в очереди — {queue_position})" if queue_position else ""
        return f"Генерация в очереди: сейчас заняты все слоты{place}."
    if status == SessionStatus.GENERATING:
        current = f" Сейчас: {', '.join(progress.current).lower()}." if progress.current else ""
        return f"Идёт генерация комплекта: выполнено шагов — {progress.done}.{current}"
    if status == SessionStatus.PATCHING:
        return "Выполняется точечная правка."
    if status == SessionStatus.DONE:
        text = (
            api_message(kit, state.notes, "report_md" in state.files)
            if kit is not None
            else "Комплект готов."
        )
        if kit is not None and kit.report is not None:
            text += f" Отчёт для методиста в браузере — {report_page_url(sid)}."
        if state.notes:
            text += " Список пропущенного — также в notes."
        patch = state.last_patch
        if patch is not None and patch.ok and patch.summary:
            text = f"Правка выполнена (версия {patch.revision}): {patch.summary} {text}"
        elif patch is not None and not patch.ok:
            text = f"{_patch_failure(patch.error)} {text}"
        return text
    return state.error or "Работа остановлена из-за ошибки."


def _patch_failure(error: str | None) -> str:
    """«Правка не выполнена: <причина>. Комплект прежний.» — без повторов, если причина уже так сформулирована."""
    reason = (error or "внутренняя ошибка").strip()
    if not reason.lower().startswith("правка не"):
        reason = f"Правка не выполнена: {reason[:1].lower()}{reason[1:]}"
    if "комплект" not in reason.lower():
        reason = reason.rstrip(".") + ". Комплект прежний."
    return reason


def _report_hint(kit: Kit | None, field: str) -> str:
    if kit is None or kit.report is None:
        return ""
    if kit.report.status == ReportStatus.ERRORS:
        return f" Самопроверка нашла ошибки — см. {field}."
    if kit.report.status == ReportStatus.WARNINGS:
        return f" Есть предупреждения самопроверки — см. {field}."
    return ""


def skeleton_view(kit: Kit) -> dict[str, Any]:
    """Каркас для методиста: паспорт, компетенции с РО, модули и темы с часами, ИА, календарь."""
    numbers = display_numbers(kit)
    modules = [
        ModuleOut(
            id=m.id,
            number=numbers.get(m.id, ""),
            title=m.title,
            kind=m.kind.value,
            control_form=m.control_form,
            needs_lectures=m.needs_lectures,
            hours=kit.module_hours(m.id),
            topics=[
                TopicOut(
                    id=t.id,
                    number=numbers.get(t.id, ""),
                    title=t.title,
                    hours=t.hours,
                    lo_ids=list(t.lo_ids),
                    control_form=t.control_form,
                )
                for t in kit.topics_of(m.id)
            ],
        )
        for m in kit.modules_sorted()
    ]
    competencies = [
        CompetencyOut(
            id=c.id,
            number=numbers.get(c.id, ""),
            title=c.title,
            activity=c.activity,
            tf_refs=[r.model_dump() for r in c.tf_refs],
            outcomes=[
                OutcomeOut(
                    id=o.id, number=numbers.get(o.id, ""), kind=o.kind.value, text=o.text, tf_code=o.tf_code
                )
                for o in kit.outcomes_of(c.id)
            ],
        )
        for c in sorted(kit.competencies, key=lambda c: id_number_safe(c.id))
    ]
    fa = kit.final_attestation
    calendar = kit.calendar
    view = SkeletonOut(
        program=kit.program.model_dump(mode="json"),
        competencies=competencies,
        modules=modules,
        final_attestation=(
            {"form": fa.form.value, "form_text": fa.form_text, "hours": fa.hours} if fa is not None else None
        ),
        hours_topics=kit.program_hours(),
        total_hours=kit.total_hours(),
        declared_total_hours=kit.program.total_hours,
        calendar=(
            {
                "mode": calendar.mode.value,
                "weeks": calendar.weeks,
                "days_per_week": calendar.days_per_week,
                "sessions_per_week": calendar.sessions_per_week,
                "days": len(calendar.days),
                "notes": list(calendar.notes),
            }
            if calendar is not None
            else None
        ),
        assumptions=list(kit.request.assumptions),
    )
    return view.model_dump(mode="json")


def rebalance_payload(kit: Kit, result: SolverResult, locked: set[str]) -> dict[str, Any]:
    numbers = display_numbers(kit)
    topics = [
        RebalanceTopicOut(
            id=t.id,
            number=numbers.get(t.id, ""),
            title=t.title,
            module_id=t.module_id,
            locked=t.id in locked,
            hours=result.topic_hours.get(t.id, t.hours),
        )
        for m in kit.modules_sorted()
        for t in kit.topics_of(m.id)
    ]
    modules = {
        m.id: HoursRow.sum([row.hours for row in topics if row.module_id == m.id])
        for m in kit.modules_sorted()
    }
    out = RebalanceOut(
        ok=result.ok,
        topics=topics,
        modules=modules,
        fa_hours=result.fa_hours,
        total_hours=result.total_hours,
        declared_total_hours=kit.program.total_hours,
        conflicts=conflict_details(result),
        log=list(result.log),
    )
    return out.model_dump(mode="json")
