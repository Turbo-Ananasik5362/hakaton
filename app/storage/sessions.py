"""Сессии на диске: data/sessions/<id>/ — state.json, kit.json, inputs/, files/, log.jsonl.

state.json и kit.json пишутся атомарно (временный файл + os.replace), поэтому чтение во время генерации
всегда видит целый файл. После перезапуска сервера оркестратор продолжает сессию с последнего готового шага.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Sequence
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import now_msk
from app.models import ClarifyQuestion, Kit, RequestSpec, SessionStatus
from app.pipeline.base import AgentError, Attachment, RequestInput, SessionNotFound

SESSION_ID_RE = re.compile(r"^s_[0-9a-f]{12}$")
_FILENAME_BAD = re.compile(r"[^\w.\- ]+", re.UNICODE)

Phase = Literal["prepare", "generate", "patch"]


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


FINISHED_STEP_STATUSES = (StepStatus.DONE, StepStatus.FAILED, StepStatus.SKIPPED)


class StepProgress(BaseModel):
    name: str
    title: str
    phase: Phase
    status: StepStatus = StepStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    seconds: float | None = None
    error: str | None = None


class PatchRequest(BaseModel):
    target: str  # как прислал методист: «тема 2.3» или T2.3
    target_id: str  # разрешённый ID
    instruction: str = ""


class PatchOutcome(BaseModel):
    target_id: str
    instruction: str = ""
    ok: bool
    changed_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    revision: int = 0
    at: datetime


class SessionState(BaseModel):
    """Состояние сессии. Комплект хранится отдельно (kit.json), чтобы опрос состояния был дешёвым."""

    model_config = ConfigDict(extra="ignore")

    id: str
    status: SessionStatus = SessionStatus.NEW
    created_at: datetime
    updated_at: datetime
    request: RequestInput
    answers: dict[str, str] = Field(default_factory=dict)  # все ответы методиста (с запросом и после)
    approved: bool = False
    intake_spec: RequestSpec | None = None  # результат intake без ответов
    spec: RequestSpec | None = None  # intake + ответы
    questions: list[ClarifyQuestion] = Field(default_factory=list)
    clarify_rounds: int = 0
    progress: list[StepProgress] = Field(default_factory=list)
    files: dict[str, str] = Field(default_factory=dict)  # ключ выгрузки → имя файла в files/
    queued_at: datetime | None = None
    generation_started_at: datetime | None = None  # отсчёт дедлайна генерации (переживает перезапуск сервера)
    error: str | None = None  # понятный текст для методиста
    error_code: str | None = None
    notes: list[str] = Field(default_factory=list)  # пропущенные шаги и прочее, что надо знать
    pending_patch: PatchRequest | None = None
    last_patch: PatchOutcome | None = None

    def step(self, name: str, phase: Phase) -> StepProgress | None:
        for item in self.progress:
            if item.name == name and item.phase == phase:
                return item
        return None

    def finished_steps(self, phase: Phase) -> set[str]:
        return {p.name for p in self.progress if p.phase == phase and p.status in FINISHED_STEP_STATUSES}


class SessionCorrupted(AgentError):
    code = "session_corrupted"


def safe_filename(name: str, default: str = "file") -> str:
    """Имя файла без путей и спецсимволов (кириллица сохраняется)."""
    base = Path(name.replace("\\", "/")).name.strip()
    base = _FILENAME_BAD.sub("_", base).strip(" .")
    if not base:
        return default
    stem, dot, ext = base.rpartition(".")
    if dot and len(base) > 120:
        return stem[: 120 - len(ext) - 1] + "." + ext
    return base[:120]


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class SessionStore:
    """Файловое хранилище сессий. Один процесс сервера — блокировки между процессами не нужны."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ---- пути ------------------------------------------------------------------------------------

    @staticmethod
    def check_id(session_id: str) -> str:
        if not SESSION_ID_RE.match(session_id or ""):
            raise SessionNotFound(f"Сессия «{session_id}» не найдена.")
        return session_id

    def session_dir(self, session_id: str) -> Path:
        return self.root / self.check_id(session_id)

    def inputs_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "inputs"

    def files_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "files"

    def file_path(self, session_id: str, name: str) -> Path | None:
        """Путь к выгрузке, только если это обычное имя файла из files/ (никаких «..» и подпапок)."""
        if not name or name != safe_filename(name) or name.startswith("."):
            return None
        path = self.files_dir(session_id) / name
        return path if path.is_file() else None

    # ---- создание и чтение -------------------------------------------------------------------------

    def create(self, request: RequestInput, attachments: Sequence[tuple[str, bytes]] = ()) -> SessionState:
        session_id = f"s_{uuid.uuid4().hex[:12]}"
        root = self.session_dir(session_id)
        (root / "inputs").mkdir(parents=True)
        (root / "files").mkdir()
        stored: list[Attachment] = []
        for n, (filename, data) in enumerate(attachments, start=1):
            stored_as = f"{n:02d}_{safe_filename(filename)}"
            (root / "inputs" / stored_as).write_bytes(data)
            stored.append(Attachment(filename=filename, stored_as=stored_as, size=len(data)))
        now = now_msk()
        state = SessionState(
            id=session_id,
            created_at=now,
            updated_at=now,
            request=request.model_copy(update={"attachments": stored}),
            answers=dict(request.answers),
        )
        self.save(state, touch=False)
        self.log(session_id, "created", attachments=[a.filename for a in stored])
        return state

    def exists(self, session_id: str) -> bool:
        return (
            bool(SESSION_ID_RE.match(session_id or "")) and (self.root / session_id / "state.json").is_file()
        )

    def load(self, session_id: str) -> SessionState:
        path = self.session_dir(session_id) / "state.json"
        if not path.is_file():
            raise SessionNotFound(f"Сессия «{session_id}» не найдена.")
        try:
            return SessionState.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, ValueError) as exc:
            raise SessionCorrupted(
                f"Файл состояния сессии «{session_id}» повреждён.", details=str(exc)
            ) from exc

    def save(self, state: SessionState, touch: bool = True) -> None:
        if touch:
            state.updated_at = now_msk()
        _write_atomic(self.session_dir(state.id) / "state.json", state.model_dump_json(indent=2))

    def load_kit(self, session_id: str) -> Kit | None:
        path = self.session_dir(session_id) / "kit.json"
        if not path.is_file():
            return None
        try:
            return Kit.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, ValueError) as exc:
            raise SessionCorrupted(f"Комплект сессии «{session_id}» повреждён.", details=str(exc)) from exc

    def save_kit(self, session_id: str, kit: Kit) -> None:
        _write_atomic(self.session_dir(session_id) / "kit.json", kit.model_dump_json(indent=2))

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if SESSION_ID_RE.match(p.name) and p.is_dir())

    # ---- журнал ----------------------------------------------------------------------------------

    def log(self, session_id: str, event: str, **data: Any) -> None:
        """Журнал событий сессии (для разбора полётов; методисту не показывается)."""
        record = {"ts": now_msk().isoformat(timespec="seconds"), "event": event, **data}
        path = self.session_dir(session_id) / "log.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
