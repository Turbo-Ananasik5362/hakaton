"""Таксономия ошибок шага (docs/ARCHITECTURE.md «Надёжность»): вид, машинный код и подсказка методисту.

Виды и что с ними делает агент:
- transient — 429, 5xx, обрыв или таймаут шлюза: клиент повторяет вызов с паузами (app/llm.py), генерация делает
  одну повторную волну упавших шагов (content._retry_steps); не помогло — шаг пропущен, комплект с заметкой;
- budget — суточный предохранитель: планировщик заранее выбирает экономный режим, недостающее помечается
  «[заполнить методисту]»;
- model — ответ не по схеме или обрезан: ремонт по тексту ошибки, дробление задачи, компактный повтор;
- config — шлюз не настроен или отклонил ключ, нет кеша/фикстуры: повтор не поможет, нужна настройка;
- deadline — истекло время генерации сессии: оставшиеся вызовы модели пропускаются, комплект собирается из готового;
- input — ошибка запроса или состояния сессии (AgentError): текст уже понятен методисту;
- code — ошибка программы: шаг failed, комплект выдаётся с заметкой, трассировка — в журнале сессии.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.llm import (
    BudgetExceeded,
    GatewayError,
    LLMError,
    LLMNotConfigured,
    LLMResponseInvalid,
    MockFixtureMissing,
    ReplayMiss,
    TruncatedResponse,
)
from app.pipeline.base import AgentError, StepUnavailable


class ErrorKind(str, Enum):
    TRANSIENT = "transient"
    BUDGET = "budget"
    MODEL = "model"
    CONFIG = "config"
    DEADLINE = "deadline"
    INPUT = "input"
    CODE = "code"


class DeadlineExceeded(AgentError):
    """Истекло время генерации сессии (Settings.generation_deadline_s)."""

    code = "deadline_exceeded"


class SkippedForOutage(AgentError):
    """Шлюз недоступен: оставшиеся вызовы модели этой генерации не запускаются (не ждать повторов каждого шага)."""

    code = "llm_unavailable"


HINTS = {
    "llm_rate_limited": "Агент уже повторил вызов с паузами и сделал повторную волну. Повторите через несколько минут: "
    "если сессия остановилась (failed), POST /approve продолжит с упавших шагов.",
    "llm_unavailable": "Агент уже повторил вызов и сделал повторную волну. Повторите позже: если сессия "
    "остановилась (failed), POST /approve продолжит с упавших шагов.",
    "budget_exceeded": "То, на что не хватило бюджета, помечено «[заполнить методисту]». Продолжить можно после "
    "00:00 МСК повторным утверждением каркаса (готовые шаги не повторяются) или после увеличения "
    "LLM_DAILY_BUDGET_RUB на сервере.",
    "llm_bad_answer": "Повторите генерацию; если ошибка повторяется на том же шаге — упростите формулировки "
    "запроса или сообщите разработчикам.",
    "llm_truncated": "Повторите генерацию; если повторится — уменьшите объём модуля или разделите программу на части.",
    "llm_rejected": "Проверьте на сервере OPENAI_API_KEY и OPENAI_BASE_URL; повтор без настройки не поможет.",
    "llm_bad_request": "Шлюз не принял запрос этого шага (например, слишком длинный). Повторите генерацию; если "
    "ошибка повторяется — сообщите разработчикам номер сессии.",
    "llm_not_configured": "Задайте OPENAI_BASE_URL и OPENAI_API_KEY (или запустите сервер в режиме live) — "
    "см. README, раздел «Работа с настоящей моделью».",
    "deadline_exceeded": "Готовые материалы собраны в комплект, недостающее помечено «[заполнить методисту]». "
    "Достроить можно вручную или новой сессией с тем же запросом: готовые ответы модели возьмутся из кеша.",
    "internal_error": "Это ошибка программы, а не запроса: подробности с трассировкой — в журнале сессии. "
    "Сообщите разработчикам номер сессии.",
}


@dataclass(frozen=True)
class ErrorInfo:
    kind: ErrorKind
    code: str
    message: str  # что случилось — коротко, по-русски
    hint: str = ""  # что делать методисту

    @property
    def full(self) -> str:
        return f"{self.message} {self.hint}".strip()


def classify(exc: BaseException) -> ErrorInfo:
    if isinstance(exc, DeadlineExceeded):
        return ErrorInfo(ErrorKind.DEADLINE, exc.code, exc.message, HINTS["deadline_exceeded"])
    if isinstance(exc, SkippedForOutage):
        return ErrorInfo(ErrorKind.TRANSIENT, exc.code, exc.message, HINTS["llm_unavailable"])
    if isinstance(exc, StepUnavailable):
        return ErrorInfo(ErrorKind.INPUT, exc.code, exc.message)
    if isinstance(exc, AgentError):
        return ErrorInfo(ErrorKind.INPUT, exc.code, exc.message)
    if isinstance(exc, BudgetExceeded):
        return _info(ErrorKind.BUDGET, "budget_exceeded", "Дневной бюджет запросов к GigaChat исчерпан.")
    if isinstance(exc, TruncatedResponse):
        return _info(
            ErrorKind.MODEL,
            "llm_truncated",
            "Ответ модели не поместился в лимит длины даже после дробления задачи.",
        )
    if isinstance(exc, LLMResponseInvalid):
        return _info(ErrorKind.MODEL, "llm_bad_answer", "Модель несколько раз вернула ответ не по формату.")
    if isinstance(exc, (LLMNotConfigured, ReplayMiss, MockFixtureMissing)):
        return _info(ErrorKind.CONFIG, "llm_not_configured", f"Модель недоступна в текущем режиме: {exc}")
    if isinstance(exc, GatewayError):
        if exc.status == 429:
            return _info(
                ErrorKind.TRANSIENT, "llm_rate_limited", "Шлюз GigaChat перегружен запросами (HTTP 429)."
            )
        if exc.transient:
            where = f"HTTP {exc.status}" if exc.status else "нет ответа"
            return _info(
                ErrorKind.TRANSIENT, "llm_unavailable", f"Шлюз GigaChat временно недоступен ({where})."
            )
        if exc.status in (401, 403):
            return _info(
                ErrorKind.CONFIG, "llm_rejected", f"Шлюз GigaChat отклонил доступ (HTTP {exc.status})."
            )
        return _info(
            ErrorKind.MODEL, "llm_bad_request", f"Шлюз GigaChat не принял запрос (HTTP {exc.status})."
        )
    if isinstance(exc, LLMError):
        return _info(ErrorKind.TRANSIENT, "llm_unavailable", f"Сбой при обращении к GigaChat: {exc}")
    return _info(ErrorKind.CODE, "internal_error", f"Внутренняя ошибка агента ({type(exc).__name__}).")


def _info(kind: ErrorKind, code: str, message: str) -> ErrorInfo:
    return ErrorInfo(kind, code, message, HINTS.get(code, ""))
