"""Пайплайн без LLM для тестов API и оркестратора: каркас — эталонный комплект, шаги генерации — счётчики.

Поведение настраивается: какие параметры «не хватает» (уточняющие вопросы), какие шаги ждут сигнала release
(чтобы поймать сессию посреди генерации), какие шаги падают один раз и какие из них некритичные.
"""

from __future__ import annotations

import asyncio
import threading
from collections import Counter
from collections.abc import Callable, Iterable

from app.core.validate import ValidationContext, validate
from app.models import ClarifyQuestion, Kit, MethodistWarning, RequestSpec, ValidationReport
from app.pipeline.base import AgentError, PatchResult, RequestInput, Step, StepContext
from app.render.kit_json import render_kit_json
from tests.validate_kits import make_pk_kit, make_registry


class FakePipeline:
    def __init__(
        self,
        builder: Callable[[], Kit] = make_pk_kit,
        required: Iterable[str] = ("hours",),
        stage: Iterable[str] = ("content:M1", "content:M2", "tests"),
        hold: Iterable[str] = (),
        fail_once: dict[str, Exception] | None = None,
        noncritical: Iterable[str] = (),
    ) -> None:
        self.builder = builder
        self.required = list(required)
        self.stage = list(stage)
        self.hold = set(hold)
        self.release = threading.Event()
        self.fail_once = dict(fail_once or {})
        self.noncritical = set(noncritical)
        self.calls: Counter[str] = Counter()
        self.registry = make_registry()

    # ---- до утверждения ----------------------------------------------------------------------------

    async def intake(self, request: RequestInput, ctx: StepContext) -> RequestSpec:
        self.calls["intake"] += 1
        await asyncio.sleep(0)
        self._maybe_fail("intake")
        return RequestSpec(
            raw_text=request.text,
            missing=list(self.required),
            assumptions=["Форма обучения не указана — принята очно-заочная с ДОТ."],
        )

    def apply_answers(self, spec: RequestSpec, answers: dict[str, str]) -> RequestSpec:
        return spec.model_copy(update={"missing": [k for k in spec.missing if k not in answers]})

    async def clarify(self, spec: RequestSpec, ctx: StepContext, round_no: int) -> list[ClarifyQuestion]:
        self.calls["clarify"] += 1
        return [
            ClarifyQuestion(
                id=key, text=f"Уточните, пожалуйста: {key}?", options=["16", "36", "72", "другое"]
            )
            for key in spec.missing
        ]

    async def skeleton(self, spec: RequestSpec, ctx: StepContext) -> Kit:
        self.calls["skeleton"] += 1
        await asyncio.sleep(0)
        self._maybe_fail("skeleton")
        kit = self.builder()
        kit.request.assumptions = list(spec.assumptions)
        kit.report = self.validate(kit)
        return kit

    # ---- генерация ---------------------------------------------------------------------------------

    def generation_steps(self, kit: Kit, finished: set[str]) -> list[Step]:
        pending = [name for name in self.stage if name not in finished]
        if pending:
            return [
                Step(name, f"Шаг {name}", self._runner(name), critical=name not in self.noncritical)
                for name in pending
            ]
        if "validate" not in finished:
            return [Step("validate", "Самопроверка", self._validate_step)]
        return []

    def _runner(self, name: str):  # noqa: ANN202
        async def run(kit: Kit, ctx: StepContext) -> None:
            self.calls[name] += 1
            if name in self.hold:
                while not self.release.is_set():
                    await asyncio.sleep(0.005)
            else:
                await asyncio.sleep(0)
            self._maybe_fail(name)
            kit.texts.org_process += f"[{name}]"

        return run

    async def _validate_step(self, kit: Kit, ctx: StepContext) -> None:
        self.calls["validate"] += 1
        kit.report = self.validate(kit)

    def validate(
        self,
        kit: Kit,
        extra_warnings: Iterable[MethodistWarning] = (),
        ctx: StepContext | None = None,
        stage: str = "kit",
    ) -> ValidationReport:
        context = ValidationContext(
            registry=self.registry,
            current_year=2026,
            extra_warnings=list(extra_warnings),
            stage="skeleton" if stage == "skeleton" else "kit",
        )
        return validate(kit, context)

    # ---- после генерации ---------------------------------------------------------------------------

    async def patch(self, kit: Kit, target_id: str, instruction: str, ctx: StepContext) -> PatchResult:
        self.calls["patch"] += 1
        await asyncio.sleep(0)
        if instruction == "сломай":
            raise AgentError("Модель не смогла выполнить правку.")
        obj = kit.index()[target_id]
        if instruction and hasattr(obj, "title"):
            obj.title = instruction
        return PatchResult(kit=kit, changed_ids=[target_id])

    def render(self, kit: Kit, ctx: StepContext) -> dict[str, str]:
        files = render_kit_json(kit, ctx.files_dir)
        (ctx.files_dir / "program.docx").write_bytes(b"PK\x03\x04 fake docx")
        files["program_docx"] = "program.docx"
        return files

    def _maybe_fail(self, name: str) -> None:
        exc = self.fail_once.pop(name, None)
        if exc is not None:
            raise exc
