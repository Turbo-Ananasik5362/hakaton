"""Транспортные DTO временного контракта (docs/api_contract.md) и перевод входа в RequestInput.

Не путать с app/models.py: здесь только то, что ходит по HTTP. Входные DTO основного сценария
не запрещают лишние поля (харнесс автопрогона может прислать своё) — они просто игнорируются.
"""

from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import HoursRow, Kit, ValidationReport
from app.pipeline.base import (
    InputError,
    PayloadTooLarge,
    RequestInput,
)
from app.pipeline.editing import HoursInput, HoursScope, SkeletonEdits

MAX_REQUEST_CHARS = 20_000
MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
MAX_BODY_BYTES = 100 * 1024 * 1024  # общий предел тела запроса (base64 раздувает файлы на треть)
MAX_ANSWERS = 30
MAX_ANSWER_CHARS = 2_000

Scalar = str | int | float | bool
AnswerValue = Scalar | list[Scalar] | None

_DATA_URL = re.compile(r"^data:[^,]*;base64,", re.IGNORECASE)
_WS = re.compile(r"\s+")


# --------------------------------------------------------------------------------------------------
# Вход
# --------------------------------------------------------------------------------------------------


class AttachmentIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    filename: str = Field(
        min_length=1, max_length=255, description="Имя файла с расширением: .docx, .pdf или .txt"
    )
    content_base64: str = Field(min_length=1, max_length=MAX_ATTACHMENT_BYTES * 4 // 3 + 1024)


class RequestBody(BaseModel):
    """Общее для POST /sessions и POST /run."""

    model_config = ConfigDict(extra="ignore")

    request: str = Field(
        default="", max_length=MAX_REQUEST_CHARS, description="Запрос методиста свободным текстом"
    )
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
    answers: dict[str, AnswerValue] = Field(
        default_factory=dict,
        max_length=MAX_ANSWERS,
        description="Ответы на уточняющие вопросы, если известны заранее",
    )

    @model_validator(mode="after")
    def _not_empty(self) -> RequestBody:
        if not self.request.strip() and not self.attachments:
            raise ValueError(
                "пустой запрос: опишите программу текстом или приложите файл (.docx, .pdf, .txt)"
            )
        return self


class CreateSessionIn(RequestBody):
    auto_approve: bool = Field(
        default=False,
        description="Утвердить каркас автоматически и сразу собрать комплект (уточнения всё равно задаются)",
    )


class RunIn(RequestBody):
    """Однократный вызов: каркас утверждается автоматически, ответ — когда всё готово."""


class AnswersIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    answers: dict[str, AnswerValue] = Field(min_length=1, max_length=MAX_ANSWERS)


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skeleton: SkeletonEdits | None = Field(default=None, description="Правки каркаса перед утверждением")


class PatchIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    target: str = Field(min_length=1, max_length=200, description="ID объекта (T2.3) или «тема 2.3»")
    instruction: str = Field(default="", max_length=4_000)


class RebalanceIn(BaseModel):
    """Предпросмотр перераспределения часов. Комплект — из сессии или переданный целиком."""

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    kit: Kit | None = None
    locked: dict[str, HoursInput] = Field(default_factory=dict, max_length=300)
    scope: HoursScope = "module"

    @model_validator(mode="after")
    def _one_source(self) -> RebalanceIn:
        if (self.session_id is None) == (self.kit is None):
            raise ValueError("укажите ровно одно из полей: session_id или kit")
        return self


# --------------------------------------------------------------------------------------------------
# Перевод в RequestInput
# --------------------------------------------------------------------------------------------------


def normalize_answers(answers: dict[str, AnswerValue]) -> dict[str, str]:
    """Ответы любых простых типов → строки: 72 → «72», true → «да», список → «a, b»."""
    result: dict[str, str] = {}
    for raw_key, value in answers.items():
        key = raw_key.strip()
        if not key or len(key) > 64:
            raise InputError(f"Некорректный ключ ответа: «{raw_key[:64]}».")
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        text = ", ".join(_scalar_text(v) for v in values).strip()
        if len(text) > MAX_ANSWER_CHARS:
            raise InputError(f"Ответ «{key}» слишком длинный: больше {MAX_ANSWER_CHARS} символов.")
        if text:
            result[key] = text
    return result


def _scalar_text(value: Scalar) -> str:
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _kind_by_content(data: bytes) -> str | None:
    """Формат по содержимому: .docx (zip), .pdf; None — не распознан."""
    if data.startswith(b"PK"):
        return ".docx"
    if b"%PDF" in data[:1024]:
        return ".pdf"
    return None


def _is_text(data: bytes) -> bool:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            data.decode(encoding)
            return True
        except UnicodeDecodeError:
            continue
    return False


def decode_attachments(items: list[AttachmentIn]) -> tuple[list[tuple[str, bytes]], list[str]]:
    """base64 → байты. Формат определяется по содержимому, а не только по расширению. Вложение, которое нельзя
    прочитать (битый base64, пустой файл, неподдерживаемый формат), пропускается с записью для методиста —
    запрос из-за него не отклоняется. Слишком большой файл — ошибка 413 (защита сервера)."""
    decoded: list[tuple[str, bytes]] = []
    notes: list[str] = []
    for item in items:
        name = item.filename.strip() or "вложение"
        ext = Path(name).suffix.lower()
        payload = _WS.sub("", _DATA_URL.sub("", item.content_base64.strip()))
        try:
            data = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            notes.append(f"Файл «{name}» пропущен: содержимое не в формате base64.")
            continue
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise PayloadTooLarge(f"Файл «{name}» больше {MAX_ATTACHMENT_BYTES // (1024 * 1024)} МБ.")
        if not data:
            notes.append(f"Файл «{name}» пропущен: он пустой.")
            continue
        kind = _kind_by_content(data)
        if kind is None and ext == ".txt" and _is_text(data):
            kind = ".txt"
        if kind is None:
            hint = " Документ .doc сохраните как .docx." if ext == ".doc" else ""
            notes.append(f"Файл «{name}» пропущен: поддерживаются .docx, .pdf и .txt.{hint}")
            continue
        if kind != ext:
            name = f"{Path(name).stem or 'вложение'}{kind}"  # по содержимому это другой формат
        decoded.append((name, data))
    return decoded, notes


def to_request_input(body: RequestBody, auto_approve: bool) -> tuple[RequestInput, list[tuple[str, bytes]]]:
    files, notes = decode_attachments(body.attachments)
    text = body.request.strip()
    if not text and not files:
        raise InputError("Пустой запрос: текста нет, а вложения не удалось прочитать. " + " ".join(notes))
    request = RequestInput(
        text=text, answers=normalize_answers(body.answers), auto_approve=auto_approve, notes=notes
    )
    return request, files


# --------------------------------------------------------------------------------------------------
# Выход (для документации OpenAPI; ответы собирает app/api/views.py)
# --------------------------------------------------------------------------------------------------


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorOut(BaseModel):
    status: Literal["error"] = "error"
    error: ErrorBody


class QuestionOut(BaseModel):
    id: str
    text: str
    options: list[str] = Field(default_factory=list)


class StepOut(BaseModel):
    name: str
    title: str
    phase: str
    status: str
    seconds: float | None = None
    error: str | None = None


class ProgressOut(BaseModel):
    done: int
    total: int
    current: list[str] = Field(default_factory=list)  # названия шагов, которые идут сейчас
    steps: list[StepOut] = Field(default_factory=list)


class TopicOut(BaseModel):
    id: str
    number: str
    title: str
    hours: HoursRow
    lo_ids: list[str] = Field(default_factory=list)
    control_form: str | None = None


class ModuleOut(BaseModel):
    id: str
    number: str
    title: str
    kind: str
    control_form: str | None = None
    needs_lectures: bool = False
    hours: HoursRow
    topics: list[TopicOut] = Field(default_factory=list)


class OutcomeOut(BaseModel):
    id: str
    number: str
    kind: str
    text: str
    tf_code: str | None = None


class CompetencyOut(BaseModel):
    id: str
    number: str
    title: str
    activity: str = ""
    tf_refs: list[dict[str, str]] = Field(default_factory=list)
    outcomes: list[OutcomeOut] = Field(default_factory=list)


class SkeletonOut(BaseModel):
    program: dict[str, Any]
    competencies: list[CompetencyOut]
    modules: list[ModuleOut]
    final_attestation: dict[str, Any] | None = None
    hours_topics: HoursRow  # сумма тем без ИА
    total_hours: float  # темы + ИА
    declared_total_hours: float  # объём из запроса
    calendar: dict[str, Any] | None = None
    assumptions: list[str] = Field(default_factory=list)


class PatchOut(BaseModel):
    target_id: str
    ok: bool
    changed_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    revision: int = 0


class SessionOut(BaseModel):
    session_id: str
    status: str
    message: str
    created_at: datetime
    updated_at: datetime
    revision: int | None = None
    questions: list[QuestionOut] | None = None
    skeleton: SkeletonOut | None = None
    validation: ValidationReport | None = None
    progress: ProgressOut | None = None
    queue_position: int | None = None
    kit: dict[str, Any] | None = None
    files: dict[str, str] | None = None
    report: ValidationReport | None = None
    changed_ids: list[str] | None = None
    patch: PatchOut | None = None
    notes: list[str] | None = None
    error: ErrorBody | None = None


class RebalanceTopicOut(BaseModel):
    id: str
    number: str
    title: str
    module_id: str
    locked: bool
    hours: HoursRow


class ConflictOut(BaseModel):
    code: str
    message: str
    target_ids: list[str] = Field(default_factory=list)


class RebalanceOut(BaseModel):
    ok: bool
    topics: list[RebalanceTopicOut]
    modules: dict[str, HoursRow]
    fa_hours: float
    total_hours: float
    declared_total_hours: float
    conflicts: list[ConflictOut] = Field(default_factory=list)
    log: list[str] = Field(default_factory=list)
