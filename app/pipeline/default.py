"""Боевой пайплайн: сборка реальных шагов агента.

Шаги, которых ещё нет в этой версии, честно отвечают StepUnavailable — сессия получает понятное сообщение,
а не молчаливую пустоту. По мере готовности шаги подключаются здесь:
intake/clarify — app/pipeline/intake.py, clarify.py; skeleton — skeleton.py; содержание — content.py (подключены);
правка — patch.py; выгрузки — app/render/ (program.docx + V31, kit.json, report.json).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable

from app.config import Settings, get_settings, now_msk
from app.core.reference import extract_text
from app.core.registry import Registry, default_registry
from app.core.validate import ValidationContext, validate
from app.llm import LLMClient
from app.models import ClarifyQuestion, Issue, Kit, MethodistWarning, RequestSpec, ValidationReport
from app.pipeline import clarify as clarify_step
from app.pipeline.base import PatchResult, RequestInput, Step, StepContext, StepUnavailable
from app.pipeline.content import JUDGE_TEXTS, ContentGenerator, merge_costs
from app.pipeline.intake import run_intake
from app.pipeline.skeleton import run_skeleton
from app.render.bundle import FAILED_NOTE, RenderOutcome, error_detail, failure_issue, render_all
from app.render.common import load_organization
from app.render.kit_json import render_kit_json
from app.render.report_md import summary_line, write_report_md

logger = logging.getLogger("karkas.render")


class DefaultPipeline:
    def __init__(
        self, settings: Settings | None = None, budget_fn: Callable[[], float] | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self.budget_fn = budget_fn  # остаток суточного бюджета, ₽ (для тестов экономного режима)
        self._registry: Registry | None = None
        self._content: ContentGenerator | None = None
        self._semaphores: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}

    @property
    def registry(self) -> Registry:
        if self._registry is None:
            self._registry = default_registry()
        return self._registry

    # ---- до утверждения ----------------------------------------------------------------------------

    def llm(self) -> LLMClient:
        """Клиент LLM на один шаг: режим (mock/live/replay), кеш и учёт расхода — из настроек. Семафор — общий
        на процесс: шлюз ограничивает одновременные запросы всей команды, а не одной сессии."""
        return LLMClient(self.settings, semaphore=self._shared_semaphore())

    def _shared_semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        for closed in [lp for lp in self._semaphores if lp.is_closed()]:
            del self._semaphores[closed]
        semaphore = self._semaphores.get(loop)
        if semaphore is None:
            semaphore = self._semaphores[loop] = asyncio.Semaphore(self.settings.concurrency)
        return semaphore

    async def intake(self, request: RequestInput, ctx: StepContext) -> RequestSpec:
        return await run_intake(request, ctx, self.llm())

    def apply_answers(self, spec: RequestSpec, answers: dict[str, str]) -> RequestSpec:
        return clarify_step.apply_answers(spec, answers)

    async def clarify(self, spec: RequestSpec, ctx: StepContext, round_no: int) -> list[ClarifyQuestion]:
        return await clarify_step.run_clarify(spec, ctx, round_no, self.llm())

    async def skeleton(self, spec: RequestSpec, ctx: StepContext) -> Kit:
        def check(kit: Kit, warnings: Iterable[MethodistWarning]) -> ValidationReport:
            return self.validate(kit, warnings, ctx, stage="skeleton")

        return await run_skeleton(spec, ctx, self.llm(), self.registry, check)

    # ---- генерация ---------------------------------------------------------------------------------

    @property
    def content(self) -> ContentGenerator:
        if self._content is None:
            self._content = ContentGenerator(
                self.settings, self.registry, self._session_client, self.budget_fn
            )
        return self._content

    def _session_client(self, ctx: StepContext) -> LLMClient:
        """Клиент на шаг генерации (свой учёт расхода); семафор — общий на процесс (см. llm)."""
        return self.llm()

    def generation_steps(self, kit: Kit, finished: set[str]) -> list[Step]:
        steps = self.content.steps(kit, finished)
        if steps:
            return steps
        if "validate" not in finished:
            return [Step("validate", "Самопроверка комплекта", self._validate_step, uses_llm=False)]
        return []

    async def _validate_step(self, kit: Kit, ctx: StepContext) -> None:
        report = await asyncio.to_thread(self.validate, kit, (), ctx)  # не блокирует event loop
        report.cost = merge_costs(kit.meta.cost)
        report.timing = dict(kit.meta.timing)
        report.judge = [
            r.model_copy(update={"title": r.title or JUDGE_TEXTS.get(r.criterion, "")})
            for r in kit.meta.judge
        ]
        kit.report = report

    def validate(
        self,
        kit: Kit,
        extra_warnings: Iterable[MethodistWarning] = (),
        ctx: StepContext | None = None,
        stage: str = "kit",
    ) -> ValidationReport:
        texts = _attachment_texts(ctx) if ctx is not None else []
        context = ValidationContext(
            registry=self.registry,
            reference_texts=texts,
            extra_warnings=list(extra_warnings),
            stage="skeleton" if stage == "skeleton" else "kit",
        )
        return validate(kit, context)

    # ---- после генерации ---------------------------------------------------------------------------

    async def patch(self, kit: Kit, target_id: str, instruction: str, ctx: StepContext) -> PatchResult:
        raise StepUnavailable("Точечная правка ещё не подключена в этой версии агента.")

    def render(self, kit: Kit, ctx: StepContext) -> dict[str, str]:
        """Программа .docx, слайды .pptx, тесты GIFT / Moodle XML / CSV (каждая выгрузка — с проверкой V31),
        затем kit.json и report.json — последними, чтобы отчёт уже включал V31. Сбой выгрузки не роняет выдачу:
        понятная заметка в notes и замечание V31, остальные файлы и JSON выдаются всегда."""
        references = _attachment_texts(ctx) if ctx.request is not None else []
        try:
            org = load_organization(self.settings.organization_file)
            notes = list(kit.meta.generation_notes)
            outcome = render_all(kit, ctx.files_dir, self.registry, org, notes, references)
        except Exception as exc:  # noqa: BLE001 — последний рубеж: без файлов, но с комплектом в JSON
            logger.exception("Выгрузки не удались")
            outcome = RenderOutcome(
                notes=[FAILED_NOTE.format(title="Выгрузки .docx, .pptx и тестов", error=type(exc).__name__)],
                issues=[failure_issue("Выгрузки .docx, .pptx и тестов", error_detail(exc))],
            )
        for note in outcome.notes:
            _add_note(kit, note)
        _attach_render_issues(kit, outcome.issues)
        try:
            _finish_report(kit, ctx)
        except Exception:  # noqa: BLE001 — итог одной фразой и время сборки не важнее файлов комплекта
            logger.exception("Итог отчёта не сформирован")
        files = dict(outcome.files)
        try:  # отчёт автопроверки — после всех выгрузок, чтобы в нём был и итог их сверки (V31)
            files["report_md"] = write_report_md(kit, ctx.files_dir, (), references, list(files.values()))
        except Exception:  # noqa: BLE001 — без report.md комплект и остальные файлы всё равно выдаются
            logger.exception("report.md не сформирован")
            _add_note(
                kit,
                "Отчёт автопроверки report.md не сформирован из-за внутренней ошибки; отчёт есть в report.json.",
            )
        files.update(render_kit_json(kit, ctx.files_dir))
        return files


def _finish_report(kit: Kit, ctx: StepContext) -> None:
    """Итог одной фразой и время генерации — в report.json (после сверки выгрузок V31, перед записью отчётов)."""
    if kit.report is None:
        return
    if ctx.generation_started_at is not None:
        elapsed = (now_msk() - ctx.generation_started_at).total_seconds()
        kit.report.generation_seconds = round(max(0.0, elapsed), 1)
    kit.report.summary = summary_line(kit)


def _add_note(kit: Kit, note: str) -> None:
    if note not in kit.meta.generation_notes:
        kit.meta.generation_notes.append(note)


def _attach_render_issues(kit: Kit, issues: list[Issue]) -> None:
    """Замечания V31 — в отчёт комплекта (прежние V31 после повторной выгрузки заменяются)."""
    if kit.report is None:
        kit.report = ValidationReport()
    kit.report.issues = [i for i in kit.report.issues if i.check != "V31"] + issues


def _attachment_texts(ctx: StepContext) -> list[str]:
    """Тексты вложений — для проверки на копирование (V42)."""
    texts = []
    for attachment in ctx.request.attachments:
        path = ctx.attachment_path(attachment)
        if path.is_file():
            text = extract_text(path, attachment.filename)
            if text.strip():
                texts.append(text)
    return texts
