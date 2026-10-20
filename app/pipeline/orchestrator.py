"""Машина состояний сессии и выполнение шагов (docs/ARCHITECTURE.md §2.1, §6).

    new ──► need_clarification ──(ответы)──► new ──► skeleton_ready ──(утверждение)──► queued ──► generating ──► done
     │                                                                                                      │
     └──────────────────────────── из любого состояния при сбое ──► failed                patching ◄─(правка)┘

- Каждое действие (создание, ответы, утверждение, правка) запускает фоновую задачу сессии. Синхронный режим
  (`wait=True`) просто ждёт её до ближайшей остановки: вопросы, каркас, готовый комплект или сбой.
- Одновременно генерируются не больше `max_generating` сессий, остальные ждут в состоянии `queued`.
- Состояние и комплект сохраняются после каждого шага. После перезапуска сервера `resume_pending()` продолжает
  незавершённые сессии; выполненные шаги не повторяются (а повторные вызовы LLM бесплатны благодаря кешу).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import traceback
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, TypeVar

from app.config import now_msk
from app.core.ids import AmbiguousReference, UnknownReference, resolve_ref
from app.llm import LLMError
from app.models import Kit, SessionStatus
from app.pipeline.base import (
    AgentError,
    InvalidTransition,
    Pipeline,
    RateLimited,
    RequestInput,
    SessionBusy,
    Step,
    StepContext,
    StepUnavailable,
    TargetError,
)
from app.pipeline.editing import SkeletonEdits, apply_skeleton_edits
from app.pipeline.errors import DeadlineExceeded, ErrorKind, SkippedForOutage, classify
from app.storage.sessions import (
    PatchOutcome,
    PatchRequest,
    Phase,
    SessionCorrupted,
    SessionState,
    SessionStore,
    StepProgress,
    StepStatus,
)

logger = logging.getLogger("karkas.orchestrator")

T = TypeVar("T")

MAX_CLARIFY_ROUNDS = 3  # дальше каркас строится с допущениями, без новых вопросов
MAX_GENERATION_STEPS = 1000  # предохранитель от бесконечного плана шагов
MAX_TRANSITIONS = 50  # предохранитель от зацикливания машины состояний
RESUME_MAX_AGE = timedelta(hours=2)  # старше — после перезапуска не продолжаем сами (живые вызовы, бюджет)

# Сессии в этих состояниях после перезапуска сервера продолжают работу сами.
RESUMABLE = (SessionStatus.NEW, SessionStatus.QUEUED, SessionStatus.GENERATING, SessionStatus.PATCHING)

STATUS_TEXT = {
    SessionStatus.NEW: "идёт разбор запроса",
    SessionStatus.NEED_CLARIFICATION: "ждёт ответов на уточняющие вопросы",
    SessionStatus.SKELETON_READY: "каркас готов и ждёт утверждения",
    SessionStatus.QUEUED: "генерация в очереди",
    SessionStatus.GENERATING: "идёт генерация комплекта",
    SessionStatus.PATCHING: "идёт точечная правка",
    SessionStatus.DONE: "комплект готов",
    SessionStatus.FAILED: "работа остановлена из-за ошибки",
}


def describe_error(exc: BaseException) -> tuple[str, str]:
    """(короткий текст для методиста, машинный код) по исключению шага — по таксономии app/pipeline/errors.py."""
    info = classify(exc)
    return info.message, info.code


RETRY_AFTER_BUSY_S = 60  # сколько подождать, если все места для сессий заняты
OUTAGE_REASON = "шлюз GigaChat не отвечал, шаг не запускался"


SKIP_ONE = "Шаг «{title}» пропущен: {reason}"
SKIP_MANY_PREFIX = "Шаги пропущены — "


def _add_skip_note(notes: list[str], title: str, reason: str) -> None:
    """Одна заметка на причину: при исчерпанном бюджете пропущенные шаги перечисляются в одной строке."""
    tail = f": {reason}"
    for i, note in enumerate(notes):
        if not note.endswith(tail):
            continue
        if note.startswith(SKIP_MANY_PREFIX):
            titles = note[len(SKIP_MANY_PREFIX) : -len(tail)]
            if f"«{title}»" not in titles:
                notes[i] = f"{SKIP_MANY_PREFIX}{titles}, «{title}»{tail}"
            return
        match = re.match(r"^Шаг «(.+)» пропущен$", note[: -len(tail)])
        if match:
            if match.group(1) != title:
                notes[i] = f"{SKIP_MANY_PREFIX}«{match.group(1)}», «{title}»{tail}"
            return
    notes.append(SKIP_ONE.format(title=title, reason=reason))


RETRY_SUFFIX = " (повтор)"


def _drop_skip_note(notes: list[str], title: str) -> None:
    """Шаг, пропущенный из-за сбоя, удался при повторе — из заметок он убирается."""
    for note in list(notes):
        reason_at = note.find(": ")
        head, tail = (note[:reason_at], note[reason_at:]) if reason_at >= 0 else (note, "")
        if head == f"Шаг «{title}» пропущен":
            notes.remove(note)
        elif head.startswith(SKIP_MANY_PREFIX) and f"«{title}»" in head:
            titles = [t for t in re.findall(r"«([^»]+)»", head[len(SKIP_MANY_PREFIX) :]) if t != title]
            index = notes.index(note)
            if len(titles) == 1:
                notes[index] = SKIP_ONE.format(title=titles[0], reason=tail[2:])
            else:
                notes[index] = SKIP_MANY_PREFIX + ", ".join(f"«{t}»" for t in titles) + tail


class Orchestrator:
    def __init__(
        self,
        store: SessionStore,
        pipeline: Pipeline,
        max_generating: int = 2,
        max_clarify_rounds: int = MAX_CLARIFY_ROUNDS,
        generation_deadline_s: float | None = None,
        max_active: int | None = None,
    ) -> None:
        if max_generating < 1:
            raise ValueError("max_generating должно быть не меньше 1")
        self.store = store
        self.pipeline = pipeline
        self.max_generating = max_generating
        self.max_clarify_rounds = max_clarify_rounds
        self.generation_deadline_s = generation_deadline_s  # None — без дедлайна
        self.max_active = max_active  # None — без ограничения числа сессий в работе
        self._loop: asyncio.AbstractEventLoop | None = None
        self._slots: asyncio.Semaphore | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._queue: list[str] = []

    # ---- примитивы, привязанные к циклу событий -------------------------------------------------

    def _bind_loop(self) -> asyncio.Semaphore:
        """Семафор и задачи живут в одном цикле событий; новый цикл (другой процесс тестов) — новые примитивы."""
        loop = asyncio.get_running_loop()
        if loop is not self._loop or self._slots is None:
            self._loop = loop
            self._slots = asyncio.Semaphore(self.max_generating)
            self._tasks = {}
            self._queue = []
        return self._slots

    def is_running(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        return task is not None and not task.done()

    def queue_position(self, session_id: str) -> int | None:
        return self._queue.index(session_id) + 1 if session_id in self._queue else None

    def active_count(self) -> int:
        """Сессии в работе: идут шаги или ждут места в очереди генерации."""
        return sum(1 for t in self._tasks.values() if not t.done())

    def _check_capacity(self) -> None:
        if self.max_active is not None and self.active_count() >= self.max_active:
            raise RateLimited(
                f"Сейчас в работе {self.active_count()} сессий — это предел сервера. Повторите запрос примерно через "
                f"{RETRY_AFTER_BUSY_S} с; уже начатые сессии продолжаются.",
                retry_after=RETRY_AFTER_BUSY_S,
                details={"active": self.active_count(), "limit": self.max_active},
            )

    # ---- действия (их вызывает API) ---------------------------------------------------------------

    def get(self, session_id: str) -> SessionState:
        return self.store.load(session_id)

    async def create(
        self, request: RequestInput, attachments: list[tuple[str, bytes]] | None = None, wait: bool = True
    ) -> SessionState:
        self._bind_loop()
        self._check_capacity()
        state = self.store.create(request, attachments or [])
        return await self._kick(state.id, wait)

    async def answer(self, session_id: str, answers: dict[str, str], wait: bool = True) -> SessionState:
        self._bind_loop()
        state = self._load_idle(session_id)
        if state.status != SessionStatus.NEED_CLARIFICATION:
            raise InvalidTransition(
                f"Уточняющих вопросов сейчас нет: сессия {STATUS_TEXT[state.status]}.",
                details={"status": state.status.value},
            )
        state.answers.update(answers)
        state.questions = []
        state.status = SessionStatus.NEW
        self.store.save(state)
        self.store.log(session_id, "answers", keys=sorted(answers))
        return await self._kick(session_id, wait)

    async def approve(
        self, session_id: str, edits: SkeletonEdits | None = None, wait: bool = False
    ) -> SessionState:
        self._bind_loop()
        state = self._load_idle(session_id)
        kit = self.store.load_kit(session_id)
        retry = state.status == SessionStatus.FAILED and state.approved and kit is not None
        if state.status != SessionStatus.SKELETON_READY and not retry:
            raise InvalidTransition(_approve_refusal(state), details={"status": state.status.value})
        if edits is not None and not edits.is_empty():
            if retry or kit is None:
                raise InvalidTransition("Каркас уже утверждён — менять его можно только точечной правкой.")
            new_kit, changed, warnings = apply_skeleton_edits(kit, edits)
            new_kit.report = self.pipeline.validate(new_kit, warnings, self._ctx(state), stage="skeleton")
            self.store.save_kit(session_id, new_kit)
            self.store.log(session_id, "skeleton_edited", changed_ids=changed)
        if retry:  # повтор после сбоя: упавшие шаги выполняются снова, готовые — нет
            for item in state.progress:
                if item.phase == "generate" and item.status == StepStatus.FAILED:
                    item.status = StepStatus.PENDING
        state.approved = True
        state.status = SessionStatus.QUEUED
        state.queued_at = now_msk()
        state.generation_started_at = None  # повтор после сбоя — новый отсчёт времени генерации
        state.error = state.error_code = None
        self.store.save(state)
        self.store.log(session_id, "approved", retry=retry)
        return await self._kick(session_id, wait)

    async def patch(self, session_id: str, target: str, instruction: str, wait: bool = True) -> SessionState:
        self._bind_loop()
        state = self._load_idle(session_id)
        if state.status != SessionStatus.DONE:
            raise InvalidTransition(
                f"Точечная правка возможна только для готового комплекта, а сессия {STATUS_TEXT[state.status]}.",
                details={"status": state.status.value},
            )
        kit = self.store.load_kit(session_id)
        if kit is None:
            raise InvalidTransition("В сессии нет комплекта для правки.")
        try:
            target_id = resolve_ref(kit, target)
        except AmbiguousReference as exc:
            raise TargetError(str(exc)) from exc
        except UnknownReference as exc:
            raise TargetError(str(exc)) from exc
        state.pending_patch = PatchRequest(target=target, target_id=target_id, instruction=instruction)
        state.progress = [p for p in state.progress if p.phase != "patch"]
        state.status = SessionStatus.PATCHING
        self.store.save(state)
        self.store.log(session_id, "patch_requested", target_id=target_id)
        return await self._kick(session_id, wait)

    def resume_pending(self) -> list[str]:
        """Продолжить незавершённые сессии (вызывается при старте сервера внутри цикла событий)."""
        self._bind_loop()
        resumed: list[str] = []
        for session_id in self.store.ids():
            try:
                state = self.store.load(session_id)
            except (SessionCorrupted, AgentError) as exc:
                logger.warning("Сессия %s не прочитана: %s", session_id, exc)
                continue
            approved_waiting = state.status == SessionStatus.SKELETON_READY and state.approved
            if state.status not in RESUMABLE and not approved_waiting:
                continue
            if now_msk() - state.updated_at > RESUME_MAX_AGE:
                # старые сессии не продолжаем сами: это живые вызовы и бюджет; продолжить можно повторным approve
                state.status = SessionStatus.FAILED
                state.error = (
                    "Работа прервана перезапуском сервера и не продолжена автоматически: сессия давно не обновлялась. "
                    "Повторите запрос или утвердите каркас ещё раз."
                )
                state.error_code = "interrupted"
                self.store.save(state)
                self.store.log(session_id, "not_resumed", reason="stale")
                continue
            for item in state.progress:
                if item.status == StepStatus.RUNNING:
                    item.status = StepStatus.PENDING
            self.store.save(state)
            self.store.log(session_id, "resumed", status=state.status.value)
            self._start(session_id)
            resumed.append(session_id)
        return resumed

    async def shutdown(self) -> None:
        """Остановить фоновые задачи. Состояние на диске остаётся — после старта сессии продолжатся."""
        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---- запуск задачи ------------------------------------------------------------------------------

    def _load_idle(self, session_id: str) -> SessionState:
        state = self.store.load(session_id)
        if self.is_running(session_id):
            raise SessionBusy(
                f"С сессией уже идёт работа: {STATUS_TEXT[state.status]}. "
                f"Дождитесь окончания (GET /api/v1/sessions/{session_id}).",
                details={"status": state.status.value},
            )
        return state

    def _start(self, session_id: str) -> asyncio.Task[None]:
        existing = self._tasks.get(session_id)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.get_running_loop().create_task(self._run(session_id), name=f"session:{session_id}")
        self._tasks[session_id] = task

        def _done(t: asyncio.Task[None], sid: str = session_id) -> None:
            if self._tasks.get(sid) is t:
                del self._tasks[sid]
            if not t.cancelled() and t.exception() is not None:
                logger.error("Задача сессии %s упала: %r", sid, t.exception())

        task.add_done_callback(_done)
        return task

    async def _kick(self, session_id: str, wait: bool) -> SessionState:
        task = self._start(session_id)
        if wait:
            await asyncio.shield(task)
        else:
            await asyncio.sleep(0)  # даём задаче стартовать: занять слот или встать в очередь
        return self.store.load(session_id)

    async def _run(self, session_id: str) -> None:
        try:
            for _ in range(MAX_TRANSITIONS):
                state = self.store.load(session_id)
                if state.status == SessionStatus.NEW:
                    await self._prepare(state)
                elif state.status in (SessionStatus.QUEUED, SessionStatus.GENERATING) or (
                    state.status == SessionStatus.SKELETON_READY and state.approved
                ):
                    await self._generate(state)
                elif state.status == SessionStatus.PATCHING:
                    await self._patch(state)
                else:
                    return
            raise RuntimeError("машина состояний сессии не пришла к остановке")
        except asyncio.CancelledError:
            self.store.log(session_id, "interrupted")
            raise
        except (AgentError, LLMError) as exc:  # ожидаемые: запрос, состояние, дедлайн, шлюз, бюджет, модель
            self._fail(session_id, exc)
        except Exception as exc:  # noqa: BLE001 — ошибка программы: сессия failed, трассировка в журнале
            self._fail(session_id, exc)

    def _fail(self, session_id: str, exc: BaseException) -> None:
        info = classify(exc)
        logger.warning(
            "Сессия %s остановлена: %s (%s, %s)", session_id, info.message, info.kind.value, info.code
        )
        state = self.store.load(session_id)
        state.status = SessionStatus.FAILED
        state.error = info.full
        state.error_code = info.code
        state.queued_at = None
        self.store.save(state)
        extra = {"trace": _trace(exc)} if info.kind == ErrorKind.CODE else {}
        self.store.log(
            session_id, "failed", code=info.code, kind=info.kind.value, error=info.message, **extra
        )

    def _ctx(self, state: SessionState) -> StepContext:
        return StepContext(
            session_id=state.id,
            inputs_dir=self.store.inputs_dir(state.id),
            files_dir=self.store.files_dir(state.id),
            request=state.request,
        )

    # ---- учёт шагов ---------------------------------------------------------------------------------

    def _progress(self, state: SessionState, phase: Phase, name: str, title: str) -> StepProgress:
        item = state.step(name, phase)
        if item is None:
            item = StepProgress(name=name, title=title, phase=phase)
            state.progress.append(item)
        return item

    async def _timed(self, state: SessionState, phase: Phase, name: str, title: str, work: Awaitable[T]) -> T:
        item = self._progress(state, phase, name, title)
        item.status, item.error, item.finished_at, item.seconds = StepStatus.RUNNING, None, None, None
        item.started_at = now_msk()
        self.store.save(state)
        started = time.monotonic()
        try:
            result = await work
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            skipped = isinstance(exc, (StepUnavailable, DeadlineExceeded, SkippedForOutage))
            item.status = StepStatus.SKIPPED if skipped else StepStatus.FAILED
            item.error = describe_error(exc)[0]
            item.finished_at, item.seconds = now_msk(), round(time.monotonic() - started, 3)
            self.store.save(state)
            raise
        item.status = StepStatus.DONE
        item.finished_at, item.seconds = now_msk(), round(time.monotonic() - started, 3)
        self.store.save(state)
        return result

    async def _sync_step(
        self, state: SessionState, phase: Phase, name: str, title: str, fn: Callable[..., T], *args: Any
    ) -> T:
        """Синхронный шаг (рендер) с тем же учётом времени и статуса, что и у асинхронных."""

        async def _call() -> T:
            return await asyncio.to_thread(fn, *args)  # рендер не блокирует /health и опрос состояния

        return await self._timed(state, phase, name, title, _call())

    # ---- фазы ---------------------------------------------------------------------------------------

    async def _prepare(self, state: SessionState) -> None:
        """intake → ответы → clarify → skeleton. Останавливается на вопросах или на готовом каркасе."""
        ctx = self._ctx(state)
        if state.intake_spec is None:
            state.intake_spec = await self._timed(
                state, "prepare", "intake", "Разбор запроса", self.pipeline.intake(state.request, ctx)
            )
            self.store.save(state)
        spec = state.intake_spec
        if state.answers:
            spec = self.pipeline.apply_answers(spec, dict(state.answers))
        state.spec = spec
        self.store.save(state)

        if state.clarify_rounds < self.max_clarify_rounds:
            questions = await self._timed(
                state,
                "prepare",
                "clarify",
                "Уточняющие вопросы",
                self.pipeline.clarify(spec, ctx, state.clarify_rounds),
            )
            if questions:
                state.questions = list(questions)
                state.clarify_rounds += 1
                state.status = SessionStatus.NEED_CLARIFICATION
                self.store.save(state)
                self.store.log(state.id, "need_clarification", questions=[q.id for q in questions])
                return

        kit = await self._timed(
            state, "prepare", "skeleton", "Каркас программы", self.pipeline.skeleton(spec, ctx)
        )
        self.store.save_kit(state.id, kit)
        state.questions = []
        state.status = SessionStatus.SKELETON_READY
        if state.request.auto_approve:
            state.approved = True
            state.status = SessionStatus.QUEUED
            state.queued_at = now_msk()
        self.store.save(state)
        self.store.log(state.id, "skeleton_ready", auto_approve=state.request.auto_approve)

    async def _generate(self, state: SessionState) -> None:
        slots = self._bind_loop()
        session_id = state.id
        if slots.locked():
            if state.status != SessionStatus.QUEUED or state.queued_at is None:
                state.status = SessionStatus.QUEUED
                state.queued_at = now_msk()
                self.store.save(state)
            self._queue.append(session_id)
            try:
                await slots.acquire()
            finally:
                self._queue.remove(session_id)
        else:
            await slots.acquire()
        try:
            state = self.store.load(session_id)
            state.status = SessionStatus.GENERATING
            state.queued_at = None
            if state.generation_started_at is None:
                state.generation_started_at = now_msk()
            self.store.save(state)
            self.store.log(session_id, "generating")
            kit = self.store.load_kit(session_id)
            if kit is None:
                raise AgentError("Каркас сессии не найден — начните сессию заново.")
            ctx = self._ctx(state)
            await self._run_generation(state, kit, ctx)
            for note in state.notes:  # пропущенные шаги — в комплект: их показывает отчёт автопроверки
                if note not in kit.meta.generation_notes:
                    kit.meta.generation_notes.append(note)
            notes_before = list(kit.meta.generation_notes)
            files = await self._sync_step(
                state, "generate", "render", "Выгрузка файлов", self.pipeline.render, kit, ctx
            )
            _surface_render_notes(state.notes, notes_before, kit)
            self.store.save_kit(session_id, kit)
            state.files = dict(files)
            state.status = SessionStatus.DONE
            self.store.save(state)
            self.store.log(session_id, "done", files=sorted(state.files))
        finally:
            slots.release()

    def _remaining_s(self, state: SessionState) -> float | None:
        if self.generation_deadline_s is None or state.generation_started_at is None:
            return None
        return self.generation_deadline_s - (now_msk() - state.generation_started_at).total_seconds()

    async def _before_deadline(self, state: SessionState, work: Awaitable[T]) -> T:
        """Шаг с вызовом модели ограничен остатком времени генерации. Прерывание безопасно: шаг меняет комплект
        только после последнего await (контракт Step)."""
        remaining = self._remaining_s(state)
        if remaining is None:
            return await work
        minutes = round((self.generation_deadline_s or 0) / 60)
        message = f"истекло время генерации ({minutes} мин)"
        if remaining <= 0:
            if asyncio.iscoroutine(work):
                work.close()
            raise DeadlineExceeded(message)
        # asyncio.wait, а не wait_for: собственный TimeoutError шага (в 3.11+ это тот же класс) не должен
        # приниматься за дедлайн
        task = asyncio.ensure_future(work)
        try:
            done, _ = await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if task in done:
            return task.result()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise DeadlineExceeded(message)

    async def _run_generation(self, state: SessionState, kit: Kit, ctx: StepContext) -> None:
        finished = state.finished_steps("generate")
        launched = 0
        # «шлюз не отвечает»: в волне не прошёл ни один вызов модели, а упавшие — llm_unavailable. Тогда дальше
        # шаги с моделью не запускаются (кроме одной повторной волны); первый успешный вызов снимает режим
        outage: str | None = None
        while True:
            planned = self.pipeline.generation_steps(kit, set(finished))
            batch = list({s.name: s for s in planned if s.name not in finished}.values())
            if not batch:
                return
            launched += len(batch)
            if launched > MAX_GENERATION_STEPS:
                raise AgentError("План генерации не сходится: слишком много шагов.")
            results = await asyncio.gather(
                *(self._run_step(state, kit, ctx, step, outage) for step in batch), return_exceptions=True
            )
            outage = _outage_after(batch, results, outage)
            for step, result in zip(batch, results, strict=True):
                finished.add(step.name)
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if not isinstance(result, BaseException) and step.name.startswith("retry:"):
                    _drop_skip_note(state.notes, step.title.removesuffix(RETRY_SUFFIX))  # повтор удался
                if isinstance(result, BaseException):
                    info = classify(result)
                    extra = {"trace": _trace(result)} if info.kind == ErrorKind.CODE else {}
                    self.store.log(
                        state.id, "step_failed", step=step.name, code=info.code, kind=info.kind.value, **extra
                    )
                    if step.critical:
                        raise result
                    if not step.name.startswith("retry:"):  # неудачный повтор: заметка о шаге уже есть
                        _add_skip_note(state.notes, step.title, info.message)
                    self.store.save(state)

    async def _run_step(
        self, state: SessionState, kit: Kit, ctx: StepContext, step: Step, outage: str | None = None
    ) -> None:
        work = step.run(kit, ctx)
        if step.uses_llm and outage and not step.name.startswith("retry:"):  # шлюз лёг: не ждать повторов
            work = _refuse(work, SkippedForOutage(OUTAGE_REASON))
        elif step.uses_llm:  # вызовы модели — только до дедлайна; код (сборка, проверка) выполняется всегда
            work = self._before_deadline(state, work)
        await self._timed(state, "generate", step.name, step.title, work)
        item = state.step(step.name, "generate")
        if item is not None and item.seconds is not None:
            kit.meta.timing[step.name] = item.seconds
        self.store.save_kit(state.id, kit)

    async def _patch(self, state: SessionState) -> None:
        """Правка не роняет готовый комплект: при сбое он остаётся прежним, а ошибка — в last_patch."""
        request = state.pending_patch
        kit = self.store.load_kit(state.id)
        if request is None or kit is None:
            state.status = SessionStatus.DONE
            state.pending_patch = None
            self.store.save(state)
            return
        ctx = self._ctx(state)
        try:
            result = await self._timed(
                state,
                "patch",
                "patch",
                "Точечная правка",
                self.pipeline.patch(kit.model_copy(deep=True), request.target_id, request.instruction, ctx),
            )
            new_kit = result.kit
            if new_kit.revision <= kit.revision:
                new_kit.revision = kit.revision + 1
            notes_before = list(new_kit.meta.generation_notes)
            files = await self._sync_step(
                state, "patch", "render", "Выгрузка файлов", self.pipeline.render, new_kit, ctx
            )
            _surface_render_notes(state.notes, notes_before, new_kit)
            self.store.save_kit(state.id, new_kit)
            state.files = dict(files)
            state.last_patch = PatchOutcome(
                target_id=request.target_id,
                instruction=request.instruction,
                ok=True,
                changed_ids=list(result.changed_ids),
                revision=new_kit.revision,
                at=now_msk(),
            )
            state.notes += result.notes
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — комплект остаётся прежним
            state.last_patch = PatchOutcome(
                target_id=request.target_id,
                instruction=request.instruction,
                ok=False,
                error=describe_error(exc)[0],
                revision=kit.revision,
                at=now_msk(),
            )
            self.store.log(
                state.id, "patch_failed", error=state.last_patch.error, trace=traceback.format_exc(limit=20)
            )
        state.pending_patch = None
        state.status = SessionStatus.DONE
        self.store.save(state)


def _approve_refusal(state: SessionState) -> str:
    if state.status == SessionStatus.NEED_CLARIFICATION:
        return "Каркаса ещё нет: сначала ответьте на уточняющие вопросы."
    if state.status == SessionStatus.NEW:
        return "Каркаса ещё нет: запрос ещё разбирается."
    if state.status in (
        SessionStatus.QUEUED,
        SessionStatus.GENERATING,
        SessionStatus.DONE,
        SessionStatus.PATCHING,
    ):
        return f"Каркас уже утверждён: сессия {STATUS_TEXT[state.status]}."
    return f"Утвердить нечего: сессия {STATUS_TEXT[state.status]}, каркас не построен. Начните новую сессию."


def _surface_render_notes(notes: list[str], before: list[str], kit: Kit) -> None:
    """Заметки, которые появились при выгрузке (сбой .docx, расхождения V31), — в notes ответа API."""
    for note in kit.meta.generation_notes:
        if note not in before and note not in notes:
            notes.append(note)


def _trace(exc: BaseException) -> str:
    """Трассировка исключения для журнала сессии (не для методиста)."""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=20))


async def _refuse(work: Awaitable[T], error: Exception) -> T:
    """Шаг не запускается: корутина закрывается без выполнения, вместо результата — error."""
    if asyncio.iscoroutine(work):
        work.close()
    raise error


def _outage_after(batch: list[Step], results: list[Any], outage: str | None) -> str | None:
    """Режим «шлюз не отвечает» после волны: включается, только если ни один шаг с моделью не прошёл, а среди
    упавших есть llm_unavailable; любой успешный шаг с моделью его снимает (шлюз жив — один сбой не повод
    пропускать остальное)."""
    llm = [
        (s, r)
        for s, r in zip(batch, results, strict=True)
        if s.uses_llm and not isinstance(r, SkippedForOutage)
    ]
    if any(not isinstance(r, BaseException) for _, r in llm):
        return None
    if any(isinstance(r, BaseException) and classify(r).code == "llm_unavailable" for _, r in llm):
        return outage or "llm_unavailable"
    return outage
