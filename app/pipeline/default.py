"""Боевой пайплайн: сборка реальных шагов агента.

Шаги, которых ещё нет в этой версии, честно отвечают StepUnavailable — сессия получает понятное сообщение,
а не молчаливую пустоту. По мере готовности шаги подключаются здесь:
intake/clarify — app/pipeline/intake.py, clarify.py; skeleton — skeleton.py (подключены); содержание — content.py;
правка — patch.py; выгрузки — app/render/.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from app.config import Settings, get_settings
from app.core.reference import extract_text
from app.core.registry import Registry, default_registry
from app.core.validate import ValidationContext, validate
from app.llm import LLMClient
from app.models import ClarifyQuestion, Kit, MethodistWarning, RequestSpec, ValidationReport
from app.pipeline import clarify as clarify_step
from app.pipeline.base import PatchResult, RequestInput, Step, StepContext, StepUnavailable
from app.pipeline.intake import run_intake
from app.pipeline.skeleton import run_skeleton
from app.render.kit_json import render_kit_json


class DefaultPipeline:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._registry: Registry | None = None

    @property
    def registry(self) -> Registry:
        if self._registry is None:
            self._registry = default_registry()
        return self._registry

    # ---- до утверждения ----------------------------------------------------------------------------

    def llm(self) -> LLMClient:
        """Клиент LLM на один шаг: режим (mock/live/replay), кеш и учёт расхода — из настроек."""
        return LLMClient(self.settings)

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

    def generation_steps(self, kit: Kit, finished: set[str]) -> list[Step]:
        if "content" not in finished:
            return [Step("content", "Содержание комплекта", self._content, critical=False)]
        if "validate" not in finished:
            return [Step("validate", "Самопроверка комплекта", self._validate_step)]
        return []

    async def _content(self, kit: Kit, ctx: StepContext) -> None:
        raise StepUnavailable("генерация содержания ещё не подключена в этой версии агента.")

    async def _validate_step(self, kit: Kit, ctx: StepContext) -> None:
        kit.report = await asyncio.to_thread(self.validate, kit, (), ctx)  # не блокирует event loop

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
        return render_kit_json(kit, ctx.files_dir)


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
