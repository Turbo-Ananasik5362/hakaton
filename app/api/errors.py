"""Ошибки API: всегда JSON вида {"status": "error", "error": {"code", "message", "details"?}}.

Плохой ввод — 4xx с понятным текстом по-русски; 500 — только на настоящую внутреннюю ошибку.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.pipeline.base import (
    AgentError,
    EditConflict,
    InputError,
    InvalidTransition,
    NotFound,
    PayloadTooLarge,
    SessionBusy,
    StepUnavailable,
    TargetError,
    UnsupportedAttachment,
)
from app.storage.sessions import SessionCorrupted

logger = logging.getLogger("karkas.api")

# Порядок важен: подклассы раньше базовых.
_HTTP_STATUS: list[tuple[type[AgentError], int]] = [
    (NotFound, 404),
    (SessionBusy, 409),
    (InvalidTransition, 409),
    (PayloadTooLarge, 413),
    (UnsupportedAttachment, 415),
    (InputError, 400),
    (TargetError, 422),
    (EditConflict, 422),
    (StepUnavailable, 501),
    (SessionCorrupted, 500),
]

_HTTP_MESSAGES = {
    404: "Такого адреса нет. Список адресов — /docs.",
    405: "Этот метод не поддерживается для данного адреса.",
    413: "Запрос слишком большой.",
}

_VALIDATION_TEXT = {
    "missing": "обязательное поле",
    "json_invalid": "тело запроса — некорректный JSON",
    "string_type": "ожидается строка",
    "string_too_short": "пустое значение",
    "string_too_long": "слишком длинный текст (не больше {max_length} символов)",
    "too_short": "нужно не меньше {min_length} элемент(ов)",
    "too_long": "слишком много элементов (не больше {max_length})",
    "list_type": "ожидается список",
    "dict_type": "ожидается объект JSON",
    "model_type": "ожидается объект JSON",
    "model_attributes_type": "ожидается объект JSON",
    "bool_type": "ожидается true или false",
    "bool_parsing": "ожидается true или false",
    "int_type": "ожидается целое число",
    "int_parsing": "ожидается целое число",
    "float_type": "ожидается число",
    "float_parsing": "ожидается число",
    "greater_than_equal": "значение должно быть не меньше {ge}",
    "less_than_equal": "значение должно быть не больше {le}",
    "literal_error": "допустимые значения: {expected}",
    "enum": "допустимые значения: {expected}",
    "extra_forbidden": "неизвестное поле",
    "union_tag_invalid": "неверный вид значения",
}


def error_payload(code: str, message: str, details: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"status": "error", "error": error}


def http_status_for(exc: AgentError) -> int:
    for cls, status in _HTTP_STATUS:
        if isinstance(exc, cls):
            return status
    return 400


def _field(loc: tuple[Any, ...]) -> str:
    parts = [str(p) for p in loc if p not in ("body", "query", "path")]
    return ".".join(parts) or "тело запроса"


def _problem(err: dict[str, Any]) -> str:
    kind = err.get("type", "")
    ctx = err.get("ctx") or {}
    if kind == "value_error":
        return str(err.get("msg", "")).removeprefix("Value error, ")
    template = _VALIDATION_TEXT.get(kind)
    if template is None:
        return str(err.get("msg", "неверное значение"))
    try:
        return template.format(**ctx)
    except (KeyError, IndexError):
        return template


async def _agent_error(request: Request, exc: AgentError) -> JSONResponse:
    status = http_status_for(exc)
    if status >= 500:
        logger.error("Ошибка %s на %s: %s (%s)", exc.code, request.url.path, exc.message, exc.details)
        return JSONResponse(error_payload(exc.code, exc.message), status_code=status)
    return JSONResponse(error_payload(exc.code, exc.message, exc.details), status_code=status)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = [{"field": _field(tuple(e.get("loc", ()))), "problem": _problem(e)} for e in exc.errors()]
    shown = "; ".join(f"{d['field']}: {d['problem']}" for d in details[:5])
    more = f" (и ещё {len(details) - 5})" if len(details) > 5 else ""
    return JSONResponse(
        error_payload("invalid_request", f"Запрос не прошёл проверку — {shown}{more}.", details),
        status_code=422,
    )


async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else None
    message = _HTTP_MESSAGES.get(exc.status_code) or detail or "Ошибка запроса."
    return JSONResponse(error_payload(f"http_{exc.status_code}", message), status_code=exc.status_code)


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Необработанная ошибка на %s", request.url.path)
    return JSONResponse(
        error_payload("internal_error", "Внутренняя ошибка сервера. Подробности — в журнале сервера."),
        status_code=500,
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AgentError, _agent_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled)
