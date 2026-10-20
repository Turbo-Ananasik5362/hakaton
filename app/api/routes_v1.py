"""Временный контракт API (docs/api_contract.md, docs/ARCHITECTURE.md §6.2).

Режимы ожидания (`?wait=`):
- POST /sessions и /answers — по умолчанию ждут ближайшей остановки: вопросы, каркас или (при auto_approve)
  готовый комплект; `wait=false` — сразу вернуть состояние и опрашивать GET /sessions/{id}.
- POST /approve — по умолчанию асинхронно (`generating` + прогресс по шагам); `wait=true` — ждать комплект.
- POST /patch — по умолчанию ждёт результат правки.
- POST /run — всегда синхронно.
"""

from __future__ import annotations

import mimetypes
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from app.models import HoursRow
from app.pipeline.base import FileMissing, InvalidTransition
from app.pipeline.editing import preview_rebalance, resolve_object
from app.pipeline.orchestrator import Orchestrator
from app.storage.sessions import SessionState

from .schemas import (
    AnswersIn,
    ApproveIn,
    CreateSessionIn,
    ErrorOut,
    PatchIn,
    RebalanceIn,
    RebalanceOut,
    RunIn,
    SessionOut,
    normalize_answers,
    to_request_input,
)
from .views import rebalance_payload, session_payload

router = APIRouter(prefix="/api/v1", tags=["v1"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorOut} for code in (400, 404, 409, 413, 415, 422, 500, 501)
}

_MEDIA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".json": "application/json",
    ".gift": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml",
    ".csv": "text/csv; charset=utf-8",
}


def _orchestrator(request: Request) -> Orchestrator:
    return request.app.state.orchestrator


def _respond(orch: Orchestrator, state: SessionState) -> JSONResponse:
    kit = orch.store.load_kit(state.id)
    return JSONResponse(session_payload(state, kit, orch.queue_position(state.id)))


@router.post("/sessions", response_model=SessionOut, responses=_ERRORS, summary="Начать работу по запросу")
async def create_session(
    body: CreateSessionIn,
    request: Request,
    wait: bool = Query(True, description="Ждать вопросов, каркаса или (при auto_approve) готового комплекта"),
) -> JSONResponse:
    orch = _orchestrator(request)
    request_input, files = to_request_input(body, auto_approve=body.auto_approve)
    state = await orch.create(request_input, files, wait=wait)
    return _respond(orch, state)


@router.post(
    "/sessions/{session_id}/answers",
    response_model=SessionOut,
    responses=_ERRORS,
    summary="Ответы на уточнения",
)
async def answer(
    session_id: str, body: AnswersIn, request: Request, wait: bool = Query(True)
) -> JSONResponse:
    orch = _orchestrator(request)
    state = await orch.answer(session_id, normalize_answers(body.answers), wait=wait)
    return _respond(orch, state)


@router.post(
    "/sessions/{session_id}/approve",
    response_model=SessionOut,
    responses=_ERRORS,
    summary="Утвердить каркас (с правками) и собрать комплект",
)
async def approve(
    session_id: str,
    request: Request,
    body: Annotated[ApproveIn | None, Body()] = None,
    wait: bool = Query(False, description="Ждать готового комплекта (иначе — опрос GET /sessions/{id})"),
) -> JSONResponse:
    orch = _orchestrator(request)
    edits = body.skeleton if body is not None else None
    state = await orch.approve(session_id, edits, wait=wait)
    return _respond(orch, state)


@router.post(
    "/sessions/{session_id}/patch", response_model=SessionOut, responses=_ERRORS, summary="Точечная правка"
)
async def patch(session_id: str, body: PatchIn, request: Request, wait: bool = Query(True)) -> JSONResponse:
    orch = _orchestrator(request)
    state = await orch.patch(session_id, body.target.strip(), body.instruction.strip(), wait=wait)
    return _respond(orch, state)


@router.get(
    "/sessions/{session_id}", response_model=SessionOut, responses=_ERRORS, summary="Состояние сессии"
)
async def get_session(session_id: str, request: Request) -> JSONResponse:
    orch = _orchestrator(request)
    return _respond(orch, orch.get(session_id))


@router.get("/sessions/{session_id}/files/{name}", responses=_ERRORS, summary="Скачать файл комплекта")
async def get_file(session_id: str, name: str, request: Request) -> FileResponse:
    orch = _orchestrator(request)
    state = orch.get(session_id)
    path = orch.store.file_path(session_id, name) if name in state.files.values() else None
    if path is None:
        raise FileMissing(f"В сессии нет файла «{name}».", details={"files": sorted(state.files.values())})
    media = (
        _MEDIA_TYPES.get(path.suffix.lower())
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )
    return FileResponse(path, media_type=media, filename=path.name)


@router.post(
    "/run", response_model=SessionOut, responses=_ERRORS, summary="Однократный вызов: запрос → комплект"
)
async def run(body: RunIn, request: Request) -> JSONResponse:
    orch = _orchestrator(request)
    request_input, files = to_request_input(body, auto_approve=True)
    state = await orch.create(request_input, files, wait=True)
    return _respond(orch, state)


@router.post(
    "/hours/rebalance",
    response_model=RebalanceOut,
    responses=_ERRORS,
    summary="Предпросмотр часов при ручной правке строк каркаса",
)
async def hours_rebalance(body: RebalanceIn, request: Request) -> JSONResponse:
    orch = _orchestrator(request)
    if body.kit is not None:
        kit = body.kit
    else:
        session_id = body.session_id or ""  # валидатор RebalanceIn гарантирует, что он задан
        orch.get(session_id)
        kit = orch.store.load_kit(session_id)
        if kit is None:
            raise InvalidTransition("Каркас этой сессии ещё не построен.")
    locked: dict[str, HoursRow] = {
        resolve_object(kit, ref, "T", "тема"): hours.to_row() for ref, hours in body.locked.items()
    }
    result = preview_rebalance(kit, locked, body.scope)
    return JSONResponse(rebalance_payload(kit, result, set(locked)))
