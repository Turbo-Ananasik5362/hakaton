"""Контракт между оркестратором и шагами агента. HTTP здесь нет: API переводит свой формат в RequestInput.

Пайплайн (`Pipeline`) — набор шагов: intake, clarify, skeleton, шаги генерации комплекта, patch, render.
Оркестратор (`app/pipeline/orchestrator.py`) ведёт машину состояний сессии и вызывает эти шаги.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.models import ClarifyQuestion, Kit, MethodistWarning, RequestSpec, ValidationReport

# --------------------------------------------------------------------------------------------------
# Вход
# --------------------------------------------------------------------------------------------------

ATTACHMENT_EXTENSIONS = (".docx", ".pdf", ".txt")


class Attachment(BaseModel):
    """Вложение, сохранённое в data/sessions/<id>/inputs/<stored_as>."""

    filename: str  # как прислал методист
    stored_as: str  # имя файла в папке inputs/ сессии
    size: int = Field(ge=0)


class RequestInput(BaseModel):
    """Запрос методиста в виде, не зависящем от транспорта."""

    text: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    answers: dict[str, str] = Field(default_factory=dict)  # ответы, присланные сразу с запросом
    auto_approve: bool = False
    notes: list[str] = Field(default_factory=list)  # что транспорт не смог принять (вложения) — в допущения


@dataclass
class StepContext:
    """Что шаг знает о сессии: ID, папки, вход. Комплект и RequestSpec передаются аргументами."""

    session_id: str
    inputs_dir: Path
    files_dir: Path
    request: RequestInput

    def attachment_path(self, attachment: Attachment) -> Path:
        return self.inputs_dir / attachment.stored_as


# --------------------------------------------------------------------------------------------------
# Шаги генерации
# --------------------------------------------------------------------------------------------------

StepFn = Callable[[Kit, StepContext], Awaitable[None]]


@dataclass(frozen=True)
class Step:
    """Один шаг генерации комплекта (обычно один вызов LLM или чистый код).

    Контракт для `run`: шаг меняет комплект на месте и только **после своего последнего await** —
    оркестратор сохраняет kit.json после каждого завершённого шага, и шаг, прерванный на середине,
    не должен оставить в комплекте половину своих объектов (после перезапуска он выполнится заново).
    """

    name: str  # уникален в сессии: "module_content:M2"
    title: str  # для методиста: «Содержание модуля 2»
    run: StepFn
    critical: bool = True  # False: сбой шага записывается в отчёт, генерация продолжается


@dataclass
class PatchResult:
    kit: Kit
    changed_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Pipeline(Protocol):
    """Шаги агента. Реальная сборка — app/pipeline/default.py, в тестах — фейковая."""

    async def intake(self, request: RequestInput, ctx: StepContext) -> RequestSpec:
        """Текст и вложения → RequestSpec (без учёта ответов на уточнения)."""
        ...

    def apply_answers(self, spec: RequestSpec, answers: dict[str, str]) -> RequestSpec:
        """Ответы методиста → новый RequestSpec. Должна быть идемпотентной: вызывается со всеми ответами сразу."""
        ...

    async def clarify(self, spec: RequestSpec, ctx: StepContext, round_no: int) -> list[ClarifyQuestion]:
        """Вопросы методисту; пустой список — данных хватает для каркаса."""
        ...

    async def skeleton(self, spec: RequestSpec, ctx: StepContext) -> Kit:
        """Каркас: цель, компетенции, РО, модули и темы с часами (солвер), календарь, отчёт проверки."""
        ...

    def generation_steps(self, kit: Kit, finished: set[str]) -> list[Step]:
        """Шаги, которые можно запустить сейчас (параллельно). finished — уже выполненные или пропущенные.
        Пустой список — генерация закончена."""
        ...

    def validate(
        self,
        kit: Kit,
        extra_warnings: Iterable[MethodistWarning] = (),
        ctx: StepContext | None = None,
        stage: str = "kit",
    ) -> ValidationReport:
        """Детерминированная самопроверка. stage="skeleton" — каркас до генерации содержания."""
        ...

    async def patch(self, kit: Kit, target_id: str, instruction: str, ctx: StepContext) -> PatchResult:
        """Точечная правка готового комплекта. kit — копия: исходный комплект не меняется до успеха."""
        ...

    def render(self, kit: Kit, ctx: StepContext) -> dict[str, str]:
        """Выгрузки в ctx.files_dir. Возвращает «ключ → имя файла» (program_docx → program.docx)."""
        ...


# --------------------------------------------------------------------------------------------------
# Ошибки
# --------------------------------------------------------------------------------------------------


class AgentError(Exception):
    """Ошибка с понятным методисту текстом. code — машинный код для API."""

    code = "agent_error"

    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class StepUnavailable(AgentError):
    """Шаг ещё не реализован (или выключен). Не путать с ошибкой во время выполнения."""

    code = "step_unavailable"


class InputError(AgentError):
    """Плохой ввод: пустой запрос, неподдерживаемое или повреждённое вложение и т.п."""

    code = "invalid_input"


class PayloadTooLarge(InputError):
    code = "payload_too_large"


class UnsupportedAttachment(InputError):
    code = "unsupported_attachment"


class NotFound(AgentError):
    code = "not_found"


class SessionNotFound(NotFound):
    code = "session_not_found"


class FileMissing(NotFound):
    code = "file_not_found"


class SessionBusy(AgentError):
    """С сессией уже идёт работа (генерация, правка) — новое действие сейчас невозможно."""

    code = "session_busy"


class InvalidTransition(AgentError):
    """Действие не подходит к текущему состоянию сессии (например, утвердить каркас до уточнений)."""

    code = "invalid_state"


class TargetError(AgentError):
    """Ссылка на объект комплекта не найдена или неоднозначна."""

    code = "unknown_target"


class EditConflict(AgentError):
    """Правка каркаса нарушает ограничения (часы не сходятся с объёмом и т.п.)."""

    code = "hours_conflict"
