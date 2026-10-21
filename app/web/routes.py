"""Страницы интерфейса (HTML). Пока — раздел «Отчёт автопроверки» сессии; остальной интерфейс — §6.5.

GET /sessions/{id}/report            — страница отчёта со ссылками на файлы комплекта;
GET /sessions/{id}/report?partial=1  — только раздел отчёта (HTML-фрагмент для встраивания в другие страницы).
Пока комплект собирается, страница обновляется сама раз в 5 секунд.
"""

from __future__ import annotations

from html import escape

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.api.views import file_urls
from app.models import SessionStatus
from app.pipeline.base import AgentError, NotFound
from app.pipeline.orchestrator import Orchestrator
from app.render.report_md import build_report
from app.web.report_page import render_report_page, render_report_section

router = APIRouter(include_in_schema=False)

REFRESH_S = 5
IN_PROGRESS = (
    SessionStatus.NEW,
    SessionStatus.QUEUED,
    SessionStatus.GENERATING,
    SessionStatus.PATCHING,
)


def _orchestrator(request: Request) -> Orchestrator:
    return request.app.state.orchestrator


@router.get("/sessions/{session_id}/report", response_class=HTMLResponse)
def report_page(  # синхронно: чтение kit.json с диска — в пуле потоков FastAPI, цикл событий не ждёт
    session_id: str,
    request: Request,
    partial: bool = Query(False, description="Только раздел отчёта, без обрамления страницы"),
) -> HTMLResponse:
    orch = _orchestrator(request)
    try:
        state = orch.store.load(session_id)
        kit = orch.store.load_kit(session_id)
    except AgentError as exc:
        status = 404 if isinstance(exc, NotFound) else 500
        page = render_report_page(None, session_id, message=exc.message)
        return HTMLResponse(page if not partial else f"<p>{escape(exc.message)}</p>", status_code=status)
    if kit is None or kit.report is None or state.status != SessionStatus.DONE:
        waiting = state.status in IN_PROGRESS or state.status == SessionStatus.SKELETON_READY
        message = _pending_message(state.status, state.error)
        page = render_report_page(None, session_id, message=message, refresh_s=REFRESH_S if waiting else None)
        return HTMLResponse(page if not partial else "", status_code=200 if waiting else 409)
    doc = build_report(kit, state.notes, files=list(state.files.values()))
    if partial:
        return HTMLResponse(render_report_section(doc))
    return HTMLResponse(render_report_page(doc, session_id, file_urls(state)))


def _pending_message(status: SessionStatus, error: str | None) -> str:
    if status == SessionStatus.FAILED:
        return f"Сборка комплекта остановлена: {error or 'ошибка'}. Отчёт не сформирован."
    if status == SessionStatus.NEED_CLARIFICATION:
        return "Агент ждёт ответов на уточняющие вопросы — отчёт появится после сборки комплекта."
    if status == SessionStatus.SKELETON_READY:
        return "Каркас готов и ждёт утверждения — отчёт появится после сборки комплекта."
    return "Комплект собирается — отчёт появится, как только сборка закончится. Страница обновится сама."
