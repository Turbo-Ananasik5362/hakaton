"""Сервер агента: `uvicorn app.main:app --port 8000` (на ВМ — `--host 0.0.0.0`).

При старте продолжает незавершённые сессии из data/sessions/, при остановке аккуратно прерывает фоновые задачи
(их состояние на диске остаётся, и после следующего старта они продолжатся).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import routes_v1
from app.api.errors import error_payload, install_error_handlers
from app.api.schemas import MAX_BODY_BYTES
from app.config import Settings, get_settings
from app.pipeline.base import Pipeline
from app.pipeline.default import DefaultPipeline
from app.pipeline.orchestrator import Orchestrator
from app.storage.sessions import SessionStore

logger = logging.getLogger("karkas")

MAX_GENERATING_SESSIONS = 2  # ВМ: 2 ядра, 4 ГБ (docs/ARCHITECTURE.md §6.4)


def create_app(
    settings: Settings | None = None,
    pipeline: Pipeline | None = None,
    store: SessionStore | None = None,
    max_generating: int = MAX_GENERATING_SESSIONS,
) -> FastAPI:
    settings = settings or get_settings()
    store = store or SessionStore(settings.sessions_dir)
    orchestrator = Orchestrator(store, pipeline or DefaultPipeline(settings), max_generating=max_generating)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.llm_mode != "live":
            logger.warning(
                "LLM_MODE=%s: ответы модели не настоящие. Для автопрогона и демо сервер запускают с LLM_MODE=live.",
                settings.llm_mode,
            )
        resumed = orchestrator.resume_pending()
        if resumed:
            logger.info("Продолжены незавершённые сессии: %s", ", ".join(resumed))
        yield
        await orchestrator.shutdown()

    app = FastAPI(
        title="Каркас — агент методиста АЦР",
        description="Собирает образовательную программу и комплект материалов по шаблонам АЦР.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.orchestrator = orchestrator
    app.state.settings = settings
    install_error_handlers(app)

    @app.middleware("http")
    async def limit_body_size(request: Request, call_next):  # noqa: ANN001, ANN202
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            message = f"Запрос больше {MAX_BODY_BYTES // (1024 * 1024)} МБ."
            return JSONResponse(error_payload("payload_too_large", message), status_code=413)
        return await call_next(request)

    @app.get("/health", tags=["service"], summary="Сервер жив")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", tags=["service"], include_in_schema=False)
    async def index() -> dict[str, str]:
        return {
            "service": "Каркас — агент методиста АЦР",
            "api": "/api/v1",
            "docs": "/docs",
            "health": "/health",
            "llm_mode": settings.llm_mode,  # на ВМ для автопрогона должно быть live
        }

    app.include_router(routes_v1.router)
    return app


def _configure_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in (
        "httpx",
        "httpx2",
        "httpcore",
    ):  # строка на каждый HTTP-запрос к шлюзу — в логе сервера не нужна
        logging.getLogger(noisy).setLevel(logging.WARNING)


_configure_logging()
app = create_app()
