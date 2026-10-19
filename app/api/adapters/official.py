"""Адаптер официального формата автопрогона (docs/ARCHITECTURE.md §6.3). ЗАГОТОВКА.

Формат, в котором проверяющая система вызывает агента, организаторы ещё не прислали (docs/api_contract.md,
раздел «Официальный контракт»). Когда пришлют:

1. Вставить спецификацию в docs/api_contract.md как есть.
2. Описать здесь входной и выходной DTO официального формата (pydantic, extra="ignore" на входе).
3. Реализовать `to_request_input` и `to_official_response` — тонкий перевод, без логики агента:
   - TODO: как приходят уточняющие ответы — вторым вызовом или сразу в запросе (поддержаны оба:
     `RequestInput.answers` и состояние `need_clarification`);
   - TODO: ждёт ли харнесс синхронного ответа (тогда `Orchestrator.create(..., wait=True)` с auto_approve)
     или опрашивает состояние;
   - TODO: в каком виде отдавать файлы — URL (`views.file_urls`) или base64 содержимого из files/ сессии;
   - TODO: куда класть отчёт самопроверки и предупреждения методисту.
4. Подключить `router` в app/main.py (`app.include_router(official.router)`) по пути из спецификации.
5. Тест на пример запроса и ответа из спецификации — tests/test_api_official.py.

Временный контракт (routes_v1.py) остаётся рабочим и после этого.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.models import Kit
from app.pipeline.base import RequestInput, StepUnavailable
from app.storage.sessions import SessionState

router = APIRouter(tags=["official"])  # TODO: префикс и эндпоинты — по официальной спецификации


def to_request_input(payload: dict[str, Any]) -> tuple[RequestInput, list[tuple[str, bytes]]]:
    """Официальный запрос → RequestInput и вложения (имя файла, байты)."""
    raise StepUnavailable("Официальный формат API ещё не получен от организаторов.")


def to_official_response(state: SessionState, kit: Kit | None) -> dict[str, Any]:
    """Состояние сессии и комплект → ответ в официальном формате."""
    raise StepUnavailable("Официальный формат API ещё не получен от организаторов.")
