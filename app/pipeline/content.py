"""Генерация содержания комплекта после утверждения каркаса (docs/ARCHITECTURE.md §2.3–§2.4, §7.3–§7.6).

Шаги (их отдаёт `ContentGenerator.steps`, оркестратор запускает каждую порцию параллельно):
1. `plan` (код): оценка стоимости по размеру каркаса, выбор режима по остатку суточного бюджета, квоты вопросов,
   пустые тесты с ID.
2. Параллельно: `program_texts`, `final_attestation`, `literature`, `module_content:M*`
   (в экономном режиме вместо модульных шагов — `module_pack:M*`, один вызов на модуль).
3. Для каждого модуля с готовым содержанием: `lectures:M*`, `seminars:M*`, `tests:M*`.
4. По очереди: `slides` (код, из планов лекций), `refill` (автодобор: РО с < 2 оценочными средствами — точечно
   догенерировать вопросы, один раунд), `finalize` (код: «[заполнить методисту]» там, где модель не ответила),
   `judge` (смысловая проверка J1–J9, один вызов).
Самопроверку (`validate`) запускает пайплайн после этих шагов.

Все шаги, кроме `plan`, некритичные: сбой (нет бюджета, ошибка шлюза) не роняет комплект — он собирается из того,
что успели, а пропуски видны в отчёте. Каждый вызов получает только свой кусок каркаса (content_prompts.py).
Одновременно — не больше `settings.concurrency` (2) вызовов шлюза на весь процесс: лимит шлюза — на команду.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel

from app.config import Settings, now_msk
from app.core import bibliography
from app.core.ids import IdAllocator, display_numbers
from app.core.registry import Registry
from app.core.validate import (
    INTERVIEW_RE,
    TK_INTERVIEW_REQ_RE,
    coverage_matrix,
    fix_glossary_order,
    mark_unknown_codes,
)
from app.llm import LLMClient, TruncatedResponse, estimate_cost
from app.models import (
    FAForm,
    FinalAttestationDraft,
    GenerationMode,
    JudgeDraft,
    JudgeResult,
    Kit,
    LecturesDraft,
    LiteratureDraft,
    Module,
    ModuleContentDraft,
    ModulePackDraft,
    ProgramTextsDraft,
    ProgramType,
    SeminarsDraft,
    Slide,
    StepCost,
    Test,
    TestScope,
    TestsDraft,
    WorkKind,
)
from app.pipeline import content_apply as apply
from app.pipeline import content_prompts as prompts
from app.pipeline.base import Step, StepContext
from app.pipeline.intake import request_key
from app.pipeline.skeleton import slides_modules
from app.skills import load_skill

logger = logging.getLogger("karkas.content")

T = TypeVar("T", bound=BaseModel)
ClientFactory = Callable[[StepContext], LLMClient]

FILL_MARK = "[заполнить методисту]"
BUDGET_MARGIN = 1.5
QUESTIONS_PER_LO = 2
MIN_INTERVIEW_PER_MODULE = 3
TEST_PASS_THRESHOLD = 70.0  # % верных ответов
REFILL_SHARE = 0.1  # автодобор — около 10 % вызовов (§7.3)
TOKENS_PER_QUESTION = 110
MAX_QUESTIONS_PER_CALL = (
    20  # больше в один ответ не помещается (4 варианта + пояснение ≈ 110 токенов на вопрос)
)
MAX_SPLIT_DEPTH = 2
# Добор отличается от исходного запроса и текстом: иначе ключ кеша совпадёт и вернётся тот же негодный ответ
# (живой прогон 20.10: тест модуля 1 с повторяющимися вариантами дважды пришёл из кеша)
LITERATURE_COMPACT = "Ответ должен быть коротким: candidates — пустой список; выбери из каталога не больше 10 изданий и ресурсов."
REFILL_SEMINAR_NOTE = (
    "Это добор: к этим темам заданий пока нет — нужно ровно одно задание на каждую тему из списка."
)
REFILL_NOTE = (
    "Это добор: прежние вопросы по этим результатам не приняты (повторяющиеся варианты или правильный ответ не из "
    "списка). Напиши новые вопросы: у каждого 4 разных варианта, correct_answers — один из них слово в слово."
)  # обрезанный ответ дробится не больше двух раз (до четверти исходной части)

# Видимые токены одного вызова (вход, выход); 1200 скрытых токенов шлюза добавляет estimate_cost (§7.1, §7.3).
CALL_TOKENS: dict[str, tuple[int, int]] = {  # module_pack: выход считается по составу модуля
    "program_texts": (1_800, 1_800),
    "final_attestation": (1_800, 1_300),
    "literature": (2_600, 800),
    "module_content": (1_500, 1_000),
    "lectures": (1_700, 1_200),
    "seminars": (1_600, 1_000),
    "tests": (1_600, 1_000),
    "module_pack": (2_600, 3_600),
    "judge": (3_500, 600),
}


# --------------------------------------------------------------------------------------------------
# Что генерировать: модули, квоты вопросов, собеседование
# --------------------------------------------------------------------------------------------------


def is_interview_module(kit: Kit, module: Module) -> bool:
    """Текущий контроль модуля — собеседование (по форме контроля модуля или по требованию запроса)."""
    if module.control_form and INTERVIEW_RE.search(module.control_form):
        return True
    return bool(TK_INTERVIEW_REQ_RE.search(kit.request.raw_text or ""))


def final_test_needed(kit: Kit) -> bool:
    fa = kit.final_attestation
    return bool(kit.request.outputs.test_questions) or (fa is not None and fa.form == FAForm.TEST)


def _lo_owner(kit: Kit) -> dict[str, str]:
    """РО → первый модуль, в темах которого он раскрывается (вопросы по РО задаются там)."""
    owner: dict[str, str] = {}
    for module in kit.modules_sorted():
        for topic in kit.topics_of(module.id):
            for lo in topic.lo_ids:
                owner.setdefault(lo, module.id)
    return owner


def question_quotas(kit: Kit) -> dict[str, dict[str, int]]:
    """Модуль → {РО: сколько тестовых вопросов}. Считает код. Число из запроса (итоговый тест) распределяется по РО
    поровну. Иначе у каждого модуля свой тест: QUESTIONS_PER_LO на РО, которые модуль раскрывает первым, и по одному
    на РО, которые он развивает после предыдущих модулей (иначе модуль, продолжающий тему, остаётся без теста).
    РО модулей с собеседованием проверяются его вопросами."""
    owner = _lo_owner(kit)
    quotas: dict[str, dict[str, int]] = {}
    wanted = kit.request.outputs.test_questions
    if wanted:
        los = list(owner)
        base, extra = divmod(wanted, len(los)) if los else (0, 0)
        for i, lo in enumerate(los):
            count = base + (1 if i < extra else 0)
            if count > 0:
                quotas.setdefault(owner[lo], {})[lo] = count
        return quotas
    for module in kit.modules_sorted():
        if is_interview_module(kit, module):
            continue
        for lo in prompts.module_outcome_ids(kit, module):
            quotas.setdefault(module.id, {})[lo] = QUESTIONS_PER_LO if owner.get(lo) == module.id else 1
    return quotas


def interview_quota(kit: Kit, module: Module) -> int:
    if not is_interview_module(kit, module):
        return 0
    return max(MIN_INTERVIEW_PER_MODULE, len(prompts.module_outcome_ids(kit, module)))


def module_test(kit: Kit, module: Module) -> Test | None:
    if final_test_needed(kit):
        return next((t for t in kit.tests if t.scope == TestScope.FINAL), None)
    return next((t for t in kit.tests if t.scope == TestScope.MODULE and t.module_id == module.id), None)


# --------------------------------------------------------------------------------------------------
# Бюджет: оценка и выбор режима (§7.5)
# --------------------------------------------------------------------------------------------------


def planned_calls(kit: Kit, mode: GenerationMode) -> list[tuple[str, int, int, int]]:
    """(шаг, вызовов, видимый вход, выход) — по размеру каркаса."""
    modules = kit.modules_sorted()
    quotas = question_quotas(kit)
    calls: list[tuple[str, int, int, int]] = []

    def add(step: str, count: int, out: int | None = None) -> None:
        if count > 0:
            inp, default_out = CALL_TOKENS[step]
            calls.append((step, count, count * inp, count * (out if out is not None else default_out)))

    for step in ("program_texts", "final_attestation", "literature", "judge"):
        add(step, 1)
    per_module_out: dict[str, int] = {}
    for module in modules:
        count = sum(quotas.get(module.id, {}).values()) + interview_quota(kit, module)
        per_module_out[module.id] = max(CALL_TOKENS["tests"][1], TOKENS_PER_QUESTION * count) if count else 0
    if mode == GenerationMode.ECONOMY:
        # один вызов на модуль: вход — общий, выход — сумма того, что нужно модулю
        for module in modules:
            out = CALL_TOKENS["module_content"][1] + per_module_out[module.id]
            out += CALL_TOKENS["lectures"][1] if apply.lecture_topics(kit, module) else 0
            out += CALL_TOKENS["seminars"][1] if apply.practice_topics(kit, module) else 0
            add("module_pack", 1, out)
        return calls
    add("module_content", len(modules))
    add("lectures", sum(1 for m in modules if apply.lecture_topics(kit, m)))
    add("seminars", sum(1 for m in modules if apply.practice_topics(kit, m)))
    for module in modules:
        if per_module_out[module.id]:
            add("tests", 1, per_module_out[module.id])
    return calls


def estimate_rub(kit: Kit, mode: GenerationMode, model: str) -> float:
    total = sum(estimate_cost(n, inp, out, model) for _, n, inp, out in planned_calls(kit, mode))
    return round(total * (1 + REFILL_SHARE), 2)


SECONDS_PER_CALL = 1.5  # задержка шлюза и разбор ответа на вызов
TOKENS_PER_SECOND = 60  # скорость генерации с запасом (замер 19.10.2026: 69–80 ток/с, §7.1)
WAVES = (
    ("program_texts", "final_attestation", "literature", "module_content", "module_pack"),
    ("lectures", "seminars", "tests"),
    ("judge",),
)


def estimate_seconds(kit: Kit, mode: GenerationMode, concurrency: int) -> float:
    """Время генерации содержания: волны §7.6 по очереди, внутри волны — не больше concurrency вызовов сразу;
    автодобор — около 10 % второй волны."""
    calls = planned_calls(kit, mode)
    total = 0.0
    for n, steps in enumerate(WAVES):
        durations = [
            SECONDS_PER_CALL + out / count / TOKENS_PER_SECOND
            for step, count, _, out in calls
            if step in steps
            for _ in range(count)
        ]
        if not durations:
            continue
        wave = max(max(durations), sum(durations) / max(1, concurrency))
        total += wave * (1 + REFILL_SHARE) if n == 1 else wave
    return round(total, 1)


def choose_mode(full: float, economy: float, remaining: float) -> tuple[GenerationMode, str | None]:
    """Полный режим, если остатка хватает с запасом × 1,5; иначе экономный (один вызов на модуль)."""
    if remaining >= full * BUDGET_MARGIN:
        return GenerationMode.FULL, None
    if remaining >= economy:
        return GenerationMode.ECONOMY, (
            f"Остаток суточного бюджета модели ({remaining:.2f} ₽) меньше оценки полного режима с запасом "
            f"({full:.2f} ₽ × {BUDGET_MARGIN:g}) — содержание собрано в экономном режиме: один вызов модели на модуль."
        )
    return GenerationMode.ECONOMY, (
        f"Остаток суточного бюджета модели ({remaining:.2f} ₽) меньше оценки даже экономного режима ({economy:.2f} ₽): "
        f"собрано то, на что хватило бюджета, недостающее помечено «{FILL_MARK}»."
    )


# --------------------------------------------------------------------------------------------------
# Генератор шагов
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Call:
    step: str  # имя навыка и шага учёта стоимости
    values: dict[str, str]
    schema: type[BaseModel]
    fixture_key: str


class ContentGenerator:
    def __init__(
        self,
        settings: Settings,
        registry: Registry,
        client_factory: ClientFactory,
        budget_fn: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.client_factory = client_factory
        self.budget_fn = budget_fn

    # ---- план шагов ------------------------------------------------------------------------------

    def steps(self, kit: Kit, finished: set[str]) -> list[Step]:
        if "plan" not in finished:
            return [Step("plan", "План генерации", self._plan)]
        numbers = display_numbers(kit)
        batch: list[Step] = []
        for name, title, fn in (
            ("program_texts", "Тексты программы", self._program_texts),
            ("final_attestation", "Итоговая аттестация", self._final_attestation),
            ("literature", "Литература", self._literature),
        ):
            if name not in finished:
                batch.append(Step(name, title, fn, critical=False))
        economy = kit.meta.generation_mode == GenerationMode.ECONOMY
        for module in kit.modules_sorted():
            n = numbers.get(module.id, "")
            if economy:
                name = f"module_pack:{module.id}"
                if name not in finished:
                    batch.append(
                        Step(
                            name,
                            f"Материалы модуля {n}",
                            self._bind(self._module_pack, module),
                            critical=False,
                        )
                    )
                continue
            content = f"module_content:{module.id}"
            if content not in finished:
                batch.append(
                    Step(
                        content,
                        f"Содержание модуля {n}",
                        self._bind(self._module_content, module),
                        critical=False,
                    )
                )
                continue
            batch += self._module_steps(kit, module, n, finished)
        if batch:
            return batch
        retry = [s for s in self._retry_steps(kit, finished) if s.name not in finished]
        if retry:
            return retry
        for name, title, fn in (
            ("slides", "Слайды", self._slides),
            ("refill", "Автодобор оценочных средств", self._refill),
            ("finalize", "Проверка полноты", self._finalize),
            ("judge", "Смысловая проверка", self._judge),
        ):
            if name not in finished:
                return [Step(name, title, fn, critical=False)]
        return []

    def _retry_steps(self, kit: Kit, finished: set[str]) -> list[Step]:
        """Один повтор шагов, после которых нет результата (сбой шлюза, 429, обрезка): временная ошибка не должна
        оставлять модуль без лекций или тестов. Повтор при исчерпанном бюджете отказывает сразу, без оплаты."""
        numbers = display_numbers(kit)
        missing: list[tuple[str, str, Callable[[Kit, StepContext], Awaitable[None]]]] = []
        texts = kit.texts
        if not any(getattr(texts, name) for name in prompts.text_fields(kit)):
            missing.append(("program_texts", "Тексты программы", self._program_texts))
        if kit.final_attestation is None or not kit.final_attestation.criteria:
            missing.append(("final_attestation", "Итоговая аттестация", self._final_attestation))
        if not any(s.section.value != "normative" for s in kit.sources):
            missing.append(("literature", "Литература", self._literature))
        economy = kit.meta.generation_mode == GenerationMode.ECONOMY
        quotas = question_quotas(kit)
        for module in kit.modules_sorted():
            n = numbers.get(module.id, "")
            topics = kit.topics_of(module.id)
            no_content = not any(items for t in topics for items in t.content.values())
            if economy:
                if no_content:
                    missing.append(
                        (
                            f"module_pack:{module.id}",
                            f"Материалы модуля {n}",
                            self._bind(self._module_pack, module),
                        )
                    )
                continue
            if no_content:
                missing.append(
                    (
                        f"module_content:{module.id}",
                        f"Содержание модуля {n}",
                        self._bind(self._module_content, module),
                    )
                )
            if apply.lecture_topics(kit, module) and not any(
                lec.module_id == module.id for lec in kit.lectures
            ):
                missing.append(
                    (f"lectures:{module.id}", f"Лекции модуля {n}", self._bind(self._lectures, module))
                )
            topic_ids = {t.id for t in topics}
            if apply.practice_topics(kit, module) and not any(s.topic_id in topic_ids for s in kit.seminars):
                missing.append(
                    (f"seminars:{module.id}", f"Задания модуля {n}", self._bind(self._seminars, module))
                )
            has_tests = any(q.topic_id in topic_ids for q in kit.questions) or any(
                iq.module_id == module.id for iq in kit.interview_questions
            )
            if (quotas.get(module.id) or interview_quota(kit, module)) and not has_tests:
                missing.append(
                    (f"tests:{module.id}", f"Оценочные средства модуля {n}", self._bind(self._tests, module))
                )
        return [
            Step(f"retry:{name}", f"{title} (повтор)", fn, critical=False)
            for name, title, fn in missing
            if name in finished
        ]

    def _module_steps(self, kit: Kit, module: Module, n: str, finished: set[str]) -> list[Step]:
        steps: list[Step] = []
        wanted = [
            ("lectures", f"Лекции модуля {n}", self._lectures, bool(apply.lecture_topics(kit, module))),
            ("seminars", f"Задания модуля {n}", self._seminars, bool(apply.practice_topics(kit, module))),
            (
                "tests",
                f"{'Вопросы собеседования' if is_interview_module(kit, module) else 'Тестовые вопросы'} модуля {n}",
                self._tests,
                bool(question_quotas(kit).get(module.id) or interview_quota(kit, module)),
            ),
        ]
        for kind, title, fn, needed in wanted:
            name = f"{kind}:{module.id}"
            if needed and name not in finished:
                steps.append(Step(name, title, self._bind(fn, module), critical=False))
        return steps

    @staticmethod
    def _bind(fn: Callable[[Kit, Module, StepContext], Awaitable[None]], module: Module):  # noqa: ANN205
        module_id = module.id

        async def run(kit: Kit, ctx: StepContext) -> None:
            current = next((m for m in kit.modules if m.id == module_id), None)
            if current is not None:
                await fn(kit, current, ctx)

        return run

    # ---- вызов модели ----------------------------------------------------------------------------

    async def _ask(self, client: LLMClient, call: _Call) -> BaseModel:
        skill = load_skill(call.step if call.step != "refill" else "tests")
        return await client.complete_json(
            call.step,
            skill.model,
            skill.messages(**call.values),
            call.schema,
            max_tokens=skill.max_tokens,
            temperature=skill.temperature,
            fixture_key=call.fixture_key,
        )

    async def _ask_split(
        self,
        client: LLMClient,
        parts: Sequence[str],
        make: Callable[[Sequence[str], str], _Call],
        merge: Callable[[list[BaseModel]], BaseModel],
        key: str,
        depth: int = 0,
    ) -> BaseModel:
        """Ответ обрезан по max_tokens → части (темы, РО) пополам, каждая — отдельным вызовом; не глубже
        MAX_SPLIT_DEPTH, чтобы обрезка не превращалась в лавину оплаченных вызовов."""
        try:
            return await self._ask(client, make(parts, key))
        except TruncatedResponse:
            if len(parts) < 2 or depth >= MAX_SPLIT_DEPTH:
                raise
            half = len(parts) // 2
            first = await self._ask_split(client, parts[:half], make, merge, f"{key}-a", depth + 1)
            second = await self._ask_split(client, parts[half:], make, merge, f"{key}-b", depth + 1)
            return merge([first, second])

    @staticmethod
    def _key(kit: Kit, suffix: str = "") -> str:
        base = request_key(kit.request.raw_text or kit.program.title)
        return f"{base}-{suffix}" if suffix else base

    @staticmethod
    def _record_cost(kit: Kit, client: LLMClient) -> None:
        by_step = {item.step: item for item in kit.meta.cost}
        for item in client.cost_by_step():
            known = by_step.get(item.step)
            if known is None:
                kit.meta.cost.append(item)
                by_step[item.step] = item
                continue
            known.calls += item.calls
            known.prompt_tokens += item.prompt_tokens
            known.completion_tokens += item.completion_tokens
            known.cost_rub = round(known.cost_rub + item.cost_rub, 6)
            known.seconds = round(known.seconds + item.seconds, 3)
        client.records.clear()

    @contextmanager
    def _costed(self, kit: Kit, client: LLMClient) -> Iterator[None]:
        """Расход шага — в отчёт и при сбое: оплаченные неудачные вызовы тоже стоят денег."""
        try:
            yield
        finally:
            self._record_cost(kit, client)

    # ---- plan ------------------------------------------------------------------------------------

    def remaining_budget(self, client: LLMClient) -> float:
        if self.budget_fn is not None:
            return self.budget_fn()
        if self.settings.llm_mode != "live":
            return math.inf  # mock и replay не тратят бюджет
        return client.remaining_budget()

    async def _plan(self, kit: Kit, ctx: StepContext) -> None:
        client = self.client_factory(ctx)
        model = self.settings.default_model
        full = estimate_rub(kit, GenerationMode.FULL, model)
        economy = estimate_rub(kit, GenerationMode.ECONOMY, model)
        remaining = self.remaining_budget(client)
        mode, note = choose_mode(full, economy, remaining)
        kit.meta.generation_mode = mode
        kit.meta.llm_mode = self.settings.llm_mode
        kit.meta.budget = {
            "estimate_full_rub": full,
            "estimate_economy_rub": economy,
            "remaining_rub": round(remaining, 2) if math.isfinite(remaining) else -1.0,
            "estimate_seconds": estimate_seconds(kit, mode, self.settings.concurrency),
        }
        if note and note not in kit.meta.generation_notes:
            kit.meta.generation_notes.append(note)
        alloc = IdAllocator(kit)
        if final_test_needed(kit):
            if not any(t.scope == TestScope.FINAL for t in kit.tests):
                kit.tests.append(
                    Test(
                        id=alloc.test(),
                        title="Итоговый тест",
                        scope=TestScope.FINAL,
                        pass_threshold=TEST_PASS_THRESHOLD,
                    )
                )
        else:
            numbers = display_numbers(kit)
            quotas = question_quotas(kit)
            for module in kit.modules_sorted():
                if quotas.get(module.id) and module_test(kit, module) is None:
                    kit.tests.append(
                        Test(
                            id=alloc.test(),
                            title=f"Тест по модулю {numbers.get(module.id, '')}",
                            scope=TestScope.MODULE,
                            module_id=module.id,
                            pass_threshold=TEST_PASS_THRESHOLD,
                        )
                    )
        logger.info(
            "Содержание: режим %s, оценка %.2f ₽ (эконом %.2f ₽), остаток %s",
            mode.value,
            full,
            economy,
            remaining,
        )

    # ---- тексты, ИА, литература ------------------------------------------------------------------

    async def _program_texts(self, kit: Kit, ctx: StepContext) -> None:
        client = self.client_factory(ctx)
        values = {
            "program": prompts.describe_program(kit, self.registry),
            "modules": prompts.describe_modules(kit),
            "fields": prompts.describe_text_fields(kit),
        }
        with self._costed(kit, client):
            draft = await self._ask(client, _Call("program_texts", values, ProgramTextsDraft, self._key(kit)))
            apply.apply_program_texts(kit, draft)  # type: ignore[arg-type]

    async def _final_attestation(self, kit: Kit, ctx: StepContext) -> None:
        client = self.client_factory(ctx)
        values = {
            "program": prompts.describe_program(kit, self.registry),
            "form": prompts.describe_fa(kit),
            "outcomes": prompts.describe_outcomes(kit),
            "modules": prompts.describe_modules(kit),
        }
        with self._costed(kit, client):
            draft = await self._ask(
                client, _Call("final_attestation", values, FinalAttestationDraft, self._key(kit))
            )
            apply.apply_final_attestation(kit, draft)  # type: ignore[arg-type]

    async def _literature(self, kit: Kit, ctx: StepContext) -> None:
        client = self.client_factory(ctx)
        items = bibliography.shortlist(kit, self.registry)
        values = {
            "program": prompts.describe_program(kit, self.registry),
            "topics": prompts.describe_topics(kit, kit.topics, with_kinds=False),
            "shortlist": bibliography.describe_shortlist(items),
        }
        with self._costed(kit, client):
            try:
                draft = await self._ask(client, _Call("literature", values, LiteratureDraft, self._key(kit)))
            except TruncatedResponse:
                # модель расписывает десяток своих кандидатов (живой прогон 20.10) — один короткий повтор;
                # другой текст запроса — другой ключ кеша
                values["shortlist"] += "\n\n" + LITERATURE_COMPACT
                draft = await self._ask(
                    client, _Call("literature", values, LiteratureDraft, self._key(kit, "compact"))
                )
            self._apply_literature(kit, items, draft)  # type: ignore[arg-type]

    def _apply_literature(
        self, kit: Kit, items: list[bibliography.ShortlistItem], draft: LiteratureDraft
    ) -> None:
        result = bibliography.build_sources(
            kit, self.registry, items, draft.selected, draft.candidates, now_msk().year
        )
        kit.sources = [s for s in kit.sources if s.section.value == "normative"] + result.sources
        for note in result.notes:
            if note not in kit.meta.generation_notes:
                kit.meta.generation_notes.append(note)
        if self.settings.llm_mode != "mock":  # ответы mock — не кандидаты для пополнения каталога
            log_path = self.settings.cost_log.parent / "unverified_sources.jsonl"
            bibliography.log_unverified(log_path, kit, result.log, now_msk().isoformat(timespec="seconds"))

    # ---- модули: черновики (без изменения комплекта) ---------------------------------------------

    def _module_values(
        self, kit: Kit, module: Module, topic_ids: Sequence[str], content_kinds: Sequence[WorkKind] = ()
    ) -> dict[str, str]:
        topics = [t for t in kit.topics_of(module.id) if t.id in set(topic_ids)]
        los = list(dict.fromkeys(lo for t in topics for lo in t.lo_ids))
        return {
            "program": prompts.describe_program(kit, self.registry),
            "module": prompts.module_label(kit, module),
            "topics": prompts.describe_topics(kit, topics, content_kinds),
            "outcomes": prompts.describe_outcomes(kit, los),
        }

    async def _draft_content(self, kit: Kit, module: Module, client: LLMClient) -> ModuleContentDraft:
        def make(part: Sequence[str], key: str) -> _Call:
            values = self._module_values(kit, module, part)
            values.pop("outcomes")
            return _Call("module_content", values, ModuleContentDraft, key)

        return await self._ask_split(  # type: ignore[return-value]
            client,
            [t.id for t in kit.topics_of(module.id)],
            make,
            lambda ds: ModuleContentDraft(topics=[t for d in ds for t in d.topics]),  # type: ignore[attr-defined]
            self._key(kit, module.id),
        )

    async def _draft_lectures(self, kit: Kit, module: Module, client: LLMClient) -> LecturesDraft:
        def make(part: Sequence[str], key: str) -> _Call:
            return _Call(
                "lectures", self._module_values(kit, module, part, (WorkKind.LK,)), LecturesDraft, key
            )

        return await self._ask_split(  # type: ignore[return-value]
            client,
            [t.id for t in apply.lecture_topics(kit, module)],
            make,
            lambda ds: LecturesDraft(lectures=[x for d in ds for x in d.lectures]),  # type: ignore[attr-defined]
            self._key(kit, module.id),
        )

    async def _draft_seminars(
        self,
        kit: Kit,
        module: Module,
        client: LLMClient,
        topic_ids: Sequence[str] | None = None,
        note: str = "",
    ) -> SeminarsDraft:
        def make(part: Sequence[str], key: str) -> _Call:
            values = self._module_values(kit, module, part, (WorkKind.PR, WorkKind.LB))
            count = f"Нужно заданий: {len(part)} — по одному на темы {', '.join(part)}."
            values["requirements"] = " ".join(
                x for x in (note, count, prompts.describe_required_seminars(kit)) if x
            )
            return _Call("seminars", values, SeminarsDraft, key)

        return await self._ask_split(  # type: ignore[return-value]
            client,
            list(topic_ids) if topic_ids is not None else [t.id for t in apply.practice_topics(kit, module)],
            make,
            lambda ds: SeminarsDraft(seminars=[x for d in ds for x in d.seminars]),  # type: ignore[attr-defined]
            self._key(kit, module.id) + ("-refill" if note else ""),
        )

    def _tests_call(
        self,
        kit: Kit,
        module: Module,
        quotas: dict[str, int],
        interview: int,
        step: str,
        key: str,
        interview_los: Sequence[str] = (),
        note: str = "",
    ) -> _Call:
        lines: list[str] = [note] if note else []
        if quotas:
            lines.append("Тестовые вопросы (questions) — ровно столько на каждый результат:")
            lines += [f"- {lo}: {n}" for lo, n in quotas.items()]
        else:
            lines.append("Тестовые вопросы (questions) не нужны — оставь список пустым.")
        if interview_los:
            lines.append(
                "Вопросы собеседования (interview) — по одному на каждый из результатов: "
                + ", ".join(interview_los)
                + "; у каждого — answer_points (что должно прозвучать) и criteria."
            )
        elif interview:
            lines.append(
                f"Вопросы собеседования (interview) — не меньше {interview}, каждый результат модуля проверяется хотя бы "
                "одним вопросом; у каждого — answer_points (что должно прозвучать) и criteria."
            )
        else:
            lines.append("Вопросы собеседования (interview) не нужны — оставь список пустым.")
        los = list(dict.fromkeys([*quotas, *interview_los])) or prompts.module_outcome_ids(kit, module)
        values = {
            "program": prompts.describe_program(kit, self.registry),
            "module": prompts.module_label(kit, module),
            "topics": prompts.describe_topics(kit, kit.topics_of(module.id), (WorkKind.LK, WorkKind.PR)),
            "outcomes": prompts.describe_outcomes(kit, los),
            "quotas": "\n".join(lines),
        }
        return _Call(step, values, TestsDraft, key)

    async def _draft_tests(
        self, kit: Kit, module: Module, client: LLMClient, quotas: dict[str, int], interview: int
    ) -> TestsDraft:
        """Вопросы модуля частями не больше MAX_QUESTIONS_PER_CALL (большой тест не помещается в один ответ);
        собеседование — в первой части."""
        chunks: list[dict[str, int]] = []
        for lo, count in quotas.items():
            while count > 0:
                if not chunks or sum(chunks[-1].values()) >= MAX_QUESTIONS_PER_CALL:
                    chunks.append({})
                take = min(count, MAX_QUESTIONS_PER_CALL - sum(chunks[-1].values()))
                chunks[-1][lo] = chunks[-1].get(lo, 0) + take
                count -= take
        chunks = chunks or [{}]

        def merge(drafts: list[BaseModel]) -> TestsDraft:
            return TestsDraft(
                questions=[x for d in drafts for x in d.questions],  # type: ignore[attr-defined]
                interview=[x for d in drafts for x in d.interview],  # type: ignore[attr-defined]
            )

        async def ask_chunk(n: int, chunk: dict[str, int]) -> BaseModel:
            parts = list(chunk) or ["-"]

            def make(part: Sequence[str], key: str) -> _Call:
                part_quotas = {lo: chunk[lo] for lo in part if lo in chunk}
                part_interview = interview if n == 0 and part[0] == parts[0] else 0
                return self._tests_call(kit, module, part_quotas, part_interview, "tests", key)

            key = self._key(kit, module.id) + (f"-{n + 1}" if n else "")
            return await self._ask_split(client, parts, make, merge, key)

        drafts = await asyncio.gather(*(ask_chunk(n, chunk) for n, chunk in enumerate(chunks)))
        return merge(list(drafts))

    # ---- модули: полный режим --------------------------------------------------------------------

    async def _module_content(self, kit: Kit, module: Module, ctx: StepContext) -> None:
        client = self.client_factory(ctx)
        with self._costed(kit, client):
            draft = await self._draft_content(kit, module, client)
            apply.apply_module_content(kit, module, draft.topics)

    async def _lectures(self, kit: Kit, module: Module, ctx: StepContext) -> None:
        client = self.client_factory(ctx)
        with self._costed(kit, client):
            draft = await self._draft_lectures(kit, module, client)
            apply.apply_lectures(kit, module, draft.lectures)

    async def _seminars(self, kit: Kit, module: Module, ctx: StepContext) -> None:
        client = self.client_factory(ctx)
        with self._costed(kit, client):
            draft = await self._draft_seminars(kit, module, client)
            apply.apply_seminars(kit, module, draft.seminars)

    async def _tests(self, kit: Kit, module: Module, ctx: StepContext) -> None:
        client = self.client_factory(ctx)
        quotas = question_quotas(kit).get(module.id, {})
        interview = interview_quota(kit, module)
        with self._costed(kit, client):
            draft = await self._draft_tests(kit, module, client, quotas, interview)
            self._apply_tests(kit, module, draft, quotas, interview)

    def _apply_tests(
        self,
        kit: Kit,
        module: Module,
        draft: TestsDraft,
        quotas: dict[str, int],
        interview: int,
        interview_los: Sequence[str] = (),
    ) -> None:
        test = module_test(kit, module) if quotas else None
        if quotas and test is None:  # тест не создан на шаге plan (например, после правки каркаса)
            test = Test(
                id=IdAllocator(kit).test(),
                title=f"Тест по модулю {display_numbers(kit).get(module.id, '')}",
                scope=TestScope.MODULE,
                module_id=module.id,
                pass_threshold=TEST_PASS_THRESHOLD,
            )
            kit.tests.append(test)
        if test is not None:
            apply.apply_questions(kit, module, test, draft.questions, quotas)
        if interview or interview_los:
            limit = max(interview, len(interview_los), len(draft.interview))
            apply.apply_interview(kit, module, draft.interview, limit, focus=interview_los)

    # ---- модули: экономный режим -----------------------------------------------------------------

    async def _module_pack(self, kit: Kit, module: Module, ctx: StepContext) -> None:
        client = self.client_factory(ctx)
        topics = kit.topics_of(module.id)
        quotas = question_quotas(kit).get(module.id, {})
        interview = interview_quota(kit, module)
        values = self._module_values(kit, module, [t.id for t in topics])
        values["tasks"] = self._tests_call(kit, module, quotas, interview, "module_pack", "").values["quotas"]
        values["requirements"] = prompts.describe_required_seminars(kit)
        has_lectures, has_practice = (
            bool(apply.lecture_topics(kit, module)),
            bool(apply.practice_topics(kit, module)),
        )
        with self._costed(kit, client):
            try:
                draft = await self._ask(
                    client, _Call("module_pack", values, ModulePackDraft, self._key(kit, module.id))
                )
                assert isinstance(draft, ModulePackDraft)
                content_draft, lectures, seminars = draft.topics, draft.lectures, draft.seminars
                tests = TestsDraft(questions=draft.questions, interview=draft.interview)
            except TruncatedResponse:
                # модуль не поместился в один ответ — те же материалы шагами полного режима (по очереди)
                logger.warning("module_pack %s обрезан — материалы модуля отдельными вызовами", module.id)
                content_draft = (await self._draft_content(kit, module, client)).topics
                lectures = (await self._draft_lectures(kit, module, client)).lectures if has_lectures else []
                seminars = (await self._draft_seminars(kit, module, client)).seminars if has_practice else []
                tests = await self._draft_tests(kit, module, client, quotas, interview)
            apply.apply_module_content(kit, module, content_draft)
            if has_lectures:
                apply.apply_lectures(kit, module, lectures)
            if has_practice:
                apply.apply_seminars(kit, module, seminars)
            self._apply_tests(kit, module, tests, quotas, interview)

    # ---- слайды (код) ----------------------------------------------------------------------------

    async def _slides(self, kit: Kit, ctx: StepContext) -> None:
        fix_glossary_order(kit)  # термин — в лекции, где он встречается впервые (V41), до сборки слайдов
        for lecture in slides_lectures(kit):
            if not lecture.slides:
                lecture.slides = build_slides(kit, lecture)

    # ---- автодобор -------------------------------------------------------------------------------

    def refill_needs(self, kit: Kit) -> dict[str, dict[str, int]]:
        """Модуль → {РО: сколько догенерировать}: недобор квоты тестовых вопросов (модель ответила меньше, часть
        вопросов отброшена) и РО, у которых меньше двух оценочных средств. Итоговый тест, размер которого задан
        запросом, добирается ровно до этого размера и не больше."""
        owner = _lo_owner(kit)
        modules = {m.id: m for m in kit.modules}
        need: dict[str, dict[str, int]] = {}
        topic_module = {t.id: t.module_id for t in kit.topics}
        have = Counter((topic_module.get(q.topic_id), lo) for q in kit.questions for lo in q.lo_ids)
        final_size = bool(kit.request.outputs.test_questions)
        all_quotas = question_quotas(kit)
        for module_id, quotas in all_quotas.items():
            for lo, count in quotas.items():
                got = sum(n for (_, x), n in have.items() if x == lo) if final_size else have[(module_id, lo)]
                if count > got:
                    need.setdefault(module_id, {})[lo] = count - got
        for coverage in coverage_matrix(kit):
            gap = QUESTIONS_PER_LO - len(coverage.assessment_ids)
            if gap > 0 and coverage.lo_id in owner:  # РО без тем проверяет V15 — вопросы не к чему привязать
                current = need.setdefault(owner[coverage.lo_id], {})
                current[coverage.lo_id] = max(current.get(coverage.lo_id, 0), gap)
        wanted_total = kit.request.outputs.test_questions
        if wanted_total:
            final = next((t for t in kit.tests if t.scope == TestScope.FINAL), None)
            room = wanted_total - (len(final.question_ids) if final is not None else 0)
            for module in kit.modules_sorted():
                # модуль с собеседованием без квот добирает вопросы собеседования — они не в итоговом тесте;
                # с квотами (размер теста задан) добирает тестовые вопросы и тоже упирается в размер теста
                if module.id not in need or (
                    is_interview_module(kit, module) and not all_quotas.get(module.id)
                ):
                    continue
                trimmed: dict[str, int] = {}
                for lo, count in need[module.id].items():
                    take = min(count, max(room, 0))
                    if take > 0:
                        trimmed[lo] = take
                        room -= take
                need[module.id] = trimmed
        return {mid: wanted for mid, wanted in need.items() if wanted and mid in modules}

    @staticmethod
    def seminar_gaps(kit: Kit) -> dict[str, list[str]]:
        """Модуль → темы с практикой, к которым модель не дала задания (по правилу — одно задание на тему)."""
        with_seminar = {s.topic_id for s in kit.seminars}
        gaps: dict[str, list[str]] = {}
        for module in kit.modules_sorted():
            missing = [t.id for t in apply.practice_topics(kit, module) if t.id not in with_seminar]
            if missing:
                gaps[module.id] = missing
        return gaps

    async def _refill(self, kit: Kit, ctx: StepContext) -> None:
        """Автодобор, один раунд: недостающее по refill_needs — в модулях с собеседованием вопросами собеседования,
        иначе тестовыми вопросами."""
        needs = self.refill_needs(kit)
        seminar_gaps = self.seminar_gaps(kit)
        if not needs and not seminar_gaps:
            return
        modules = {m.id: m for m in kit.modules}
        seminar_gaps = {mid: topics for mid, topics in seminar_gaps.items() if mid in modules}
        jobs: list[tuple[Module, dict[str, int], list[str], _Call]] = []
        for module_id, wanted in needs.items():
            module = modules[module_id]
            key = self._key(kit, f"{module_id}-refill")
            if is_interview_module(kit, module) and not question_quotas(kit).get(module_id):
                los = list(wanted)
                call = self._tests_call(
                    kit, module, {}, 0, "refill", key, interview_los=los, note=REFILL_NOTE
                )
                jobs.append((module, {}, los, call))
            else:
                jobs.append(
                    (
                        module,
                        wanted,
                        [],
                        self._tests_call(kit, module, wanted, 0, "refill", key, note=REFILL_NOTE),
                    )
                )
        client = self.client_factory(ctx)
        with self._costed(kit, client):
            seminar_modules = [modules[mid] for mid in seminar_gaps]
            results = await asyncio.gather(
                *(self._ask(client, call) for *_, call in jobs),
                *(
                    self._draft_seminars(kit, m, client, seminar_gaps[m.id], REFILL_SEMINAR_NOTE)
                    for m in seminar_modules
                ),
                return_exceptions=True,
            )
            answers, seminar_answers = results[: len(jobs)], results[len(jobs) :]
            for module, answer in zip(seminar_modules, seminar_answers, strict=True):
                if isinstance(answer, asyncio.CancelledError):
                    raise answer
                if isinstance(answer, BaseException):
                    logger.warning("Добор заданий для %s не выполнен: %s", module.id, answer)
                    continue
                apply.apply_seminars(kit, module, answer.seminars, only=seminar_gaps[module.id])  # type: ignore[union-attr]
            for (module, quotas, los, _), answer in zip(jobs, answers, strict=True):
                if isinstance(answer, asyncio.CancelledError):
                    raise answer
                if isinstance(answer, BaseException):  # автодобор не должен ронять уже собранный комплект
                    logger.warning("Автодобор для %s не выполнен: %s", module.id, answer)
                    continue
                self._apply_tests(kit, module, answer, quotas, 0, interview_los=los)  # type: ignore[arg-type]

    # ---- полнота (код) ---------------------------------------------------------------------------

    async def _finalize(self, kit: Kit, ctx: StepContext) -> None:
        notes: list[str] = []
        for name in prompts.text_fields(kit):
            value = getattr(kit.texts, name)
            if not value or (isinstance(value, str) and not value.strip()):
                setattr(kit.texts, name, [FILL_MARK] if isinstance(value, list) else FILL_MARK)
                notes.append(name)
        filled = fill_content_from_materials(kit)
        if filled:
            kit.meta.generation_notes.append(
                "Модель не дала пунктов рабочей программы для части видов работы — они собраны из планов лекций и "
                f"заданий этих тем: {', '.join(filled)}."
            )
        for topic in kit.topics:
            if not any(items for items in topic.content.values()):
                topic.content = {kind: [FILL_MARK] for kind in prompts.work_kinds(topic)} or {
                    WorkKind.SRS: [FILL_MARK]
                }
                notes.append(topic.id)
        if not any(s.section.value != "normative" for s in kit.sources):
            # модель не ответила — литература из каталога без модели, только совпавшая с темами по тегам
            items = [i for i in bibliography.shortlist(kit, self.registry) if bibliography.on_topic(kit, i)]
            self._apply_literature(kit, items, LiteratureDraft())
            if not kit.sources:
                kit.meta.generation_notes.append(
                    f"Литературу по теме программы подобрать не удалось — разделы списка литературы: «{FILL_MARK}»."
                )
        kit.texts.software = software_list(kit, self.registry)
        numbers = display_numbers(kit)
        mark_model_texts(
            kit, lambda text: mark_unknown_codes(strip_service_ids(text, numbers), self.registry)
        )
        _order_final_test(kit)
        empty = [t.id for t in kit.tests if t.scope == TestScope.MODULE and not t.question_ids]
        if empty:  # модель не дала годных вопросов и автодобор не помог — пустой тест в программу не идёт
            kit.tests = [t for t in kit.tests if t.id not in empty]
            kit.meta.generation_notes.append(
                f"Тестовых вопросов для {', '.join(empty)} получить не удалось — тест не включён, нужны вопросы методиста."
            )
        if notes:
            text = f"Часть разделов модель не заполнила — там стоит «{FILL_MARK}»: {', '.join(notes)}."
            if text not in kit.meta.generation_notes:
                kit.meta.generation_notes.append(text)

    # ---- судья -----------------------------------------------------------------------------------

    async def _judge(self, kit: Kit, ctx: StepContext) -> None:
        criteria = judge_criteria(kit)
        client = self.client_factory(ctx)
        values = {
            "summary": judge_summary(kit),
            "criteria": "\n".join(f"- {cid}: {text}" for cid, text in criteria),
        }
        with self._costed(kit, client):
            draft = await self._ask(client, _Call("judge", values, JudgeDraft, self._key(kit)))
            assert isinstance(draft, JudgeDraft)
            allowed = {cid for cid, _ in criteria}
            results = {}
            for item in draft.results:
                cid = item.criterion.strip().upper()
                if cid in allowed and cid not in results:
                    results[cid] = JudgeResult(
                        criterion=cid, passed=item.passed, reason=apply.clean(item.reason)[:300]
                    )
            kit.meta.judge = [results[cid] for cid, _ in criteria if cid in results]


# --------------------------------------------------------------------------------------------------
# Тексты, написанные моделью: обход для проверки кодов ПС/ТФ и номеров актов
# --------------------------------------------------------------------------------------------------


def mark_model_texts(kit: Kit, fix: Callable[[str], str]) -> None:
    """Применяет fix ко всем текстовым полям, которые пишет модель (паспорт и тексты программы, содержание тем,
    лекции, задания, вопросы, ИА). Названия тем и РО — из каркаса, их проверяет validate."""

    def strings(values: list[str]) -> list[str]:
        return [fix(v) for v in values]

    texts = kit.texts
    for name in type(texts).model_fields:
        value = getattr(texts, name)
        setattr(texts, name, strings(value) if isinstance(value, list) else fix(value))
    for topic in kit.topics:
        topic.content = {kind: strings(items) for kind, items in topic.content.items()}
    for lecture in kit.lectures:
        lecture.goal = fix(lecture.goal)
        for item in lecture.plan:
            item.title, item.theses = fix(item.title), strings(item.theses)
        for entry in lecture.glossary:
            entry.definition = fix(entry.definition)
        for slide in lecture.slides or []:
            slide.title, slide.bullets, slide.notes = (
                fix(slide.title),
                strings(slide.bullets),
                fix(slide.notes),
            )
    for seminar in kit.seminars:
        seminar.situation, seminar.task, seminar.deliverable = (
            fix(seminar.situation),
            fix(seminar.task),
            fix(seminar.deliverable),
        )
        seminar.steps = strings(seminar.steps)
        for criterion in seminar.criteria:
            criterion.text = fix(criterion.text)
    for question in kit.questions:
        question.stem, question.explanation = fix(question.stem), fix(question.explanation)
        question.options = strings(question.options)
    for iq in kit.interview_questions:
        iq.text, iq.answer_points, iq.criteria = fix(iq.text), strings(iq.answer_points), strings(iq.criteria)
    fa = kit.final_attestation
    if fa is not None:
        fa.requirements, fa.procedure = strings(fa.requirements), strings(fa.procedure)
        fa.data_requirements, fa.report_requirements = (
            strings(fa.data_requirements),
            strings(fa.report_requirements),
        )
        fa.pass_rule = fix(fa.pass_rule)
        if fa.expected_result:
            fa.expected_result = fix(fa.expected_result)
        for criterion in fa.criteria:
            criterion.text = fix(criterion.text)
        for item in fa.portfolio_items:
            item.requirements = strings(item.requirements)
        if fa.example_task is not None:
            task = fa.example_task
            task.description, task.environment, task.deliverable = (
                fix(task.description),
                fix(task.environment),
                fix(task.deliverable),
            )


# --------------------------------------------------------------------------------------------------
# Слайды: из планов лекций, без вызова модели
# --------------------------------------------------------------------------------------------------


def slides_lectures(kit: Kit) -> list:
    """Лекции, к которым нужны слайды: занятия из запроса, модуль из запроса или первый модуль с лекциями."""
    scope = kit.program.slides_scope
    requested = kit.request.outputs.slides_scope
    if scope.mode == "default" and not scope.module_ids and requested.mode != "default":
        scope = requested
    if scope.mode == "sessions" and scope.session_numbers:
        wanted = set(scope.session_numbers)
        return [lec for lec in kit.lectures if lec.session_no in wanted]
    if scope.mode == "module":
        targets = set(scope.module_ids) or set(slides_modules(kit, scope))
        return [lec for lec in kit.lectures if lec.module_id in targets]
    for module in kit.modules_sorted():
        own = [lec for lec in kit.lectures if lec.module_id == module.id]
        if own:
            return own
    return []


def fill_content_from_materials(kit: Kit) -> list[str]:
    """Вид работы с часами, но без пунктов рабочей программы (V04), — пункты из материалов этой же темы:
    теория — разделы плана лекции, практика и лабораторные — задание, самостоятельная работа — доработка задания
    и повторение разделов лекции. Модель не вызывается; возвращает ID изменённых тем."""
    lectures = {tid: lec for lec in kit.lectures for tid in lec.topic_ids}
    seminars = {s.topic_id: s for s in kit.seminars}
    changed: list[str] = []
    for topic in kit.topics:
        lecture, seminar = lectures.get(topic.id), seminars.get(topic.id)
        plan = [p.title for p in lecture.plan] if lecture is not None else []
        for kind in prompts.work_kinds(topic):
            if topic.content.get(kind):
                continue
            items: list[str] = []
            if kind == WorkKind.LK:
                items = plan[:5]
            elif kind in (WorkKind.PR, WorkKind.LB) and seminar is not None:
                items = [seminar.title, *seminar.steps][:5]
            elif kind == WorkKind.SRS:
                if seminar is not None:
                    items.append(f"Доработка и оформление результата задания «{seminar.title}»")
                items += [f"Повторение материала: {title}" for title in plan[:3]]
            items = apply.clean_items(items)
            if items:
                topic.content = {**topic.content, kind: items}
                if topic.id not in changed:
                    changed.append(topic.id)
    return changed


SERVICE_ID_PAREN_RE = re.compile(r"\s*\((?:\s*(?:LO|TS|IQ|[TMLSQC])\d+(?:\.\d+)?\s*[,;]?)+\)")
SERVICE_ID_RE = re.compile(r"\b(?:LO|[TML])\d+\.\d+\b|\b[M]\d+\b")


def strip_service_ids(text: str, numbers: dict[str, str]) -> str:
    """Служебные ID в тексте модели: «(T5.2)» убирается, «в T5.2» → «в 5.2» (номер, как в документе)."""
    if not text:
        return text
    text = SERVICE_ID_PAREN_RE.sub("", text)
    return SERVICE_ID_RE.sub(lambda m: numbers.get(m.group(0), m.group(0)), text)


def software_list(kit: Kit, registry: Registry) -> list[str]:
    """Раздел «ПО и информационные системы»: инструменты из реестра, упомянутые в программе, — с минимальной
    поддерживаемой версией (tools.json); пункты модели, где назван инструмент из реестра, заменяются этой строкой.
    При «только сервисы России» пункты с недоступными в РФ сервисами отбрасываются."""
    tools = bibliography.mentioned_tools(kit, registry, limit=12, extra=kit.texts.software)
    rows = [f"{t.name} (версия {t.min_version} и новее)" if t.min_version else t.name for t in tools]
    names = [n for t in tools for n in t.all_names()]
    for item in kit.texts.software:
        if (
            item == FILL_MARK
            or bibliography.blocked_service(kit, registry, item)
            or any(bibliography.name_pattern(n) and bibliography.name_pattern(n).search(item) for n in names)
        ):  # type: ignore[union-attr]
            continue
        rows.append(item)
    return rows or list(kit.texts.software)


def build_slides(kit: Kit, lecture) -> list[Slide]:  # noqa: ANN001
    """Титульный слайд (заголовок = тема учебного плана), слайд на каждый пункт плана, итоговый слайд."""
    plan = lecture.plan
    intro_points = [p.title for p in plan][:5]
    if lecture.goal:
        intro_points = [f"Цель: {lecture.goal}", *intro_points][:5]
    slides = [Slide(title=lecture.title, bullets=intro_points, notes=lecture.goal)]
    for item in plan:
        slides.append(Slide(title=item.title, bullets=item.theses[:5], notes="; ".join(item.theses)))
    outcomes = {o.id: o.text for o in kit.outcomes}
    summary = [outcomes[lo] for lo in dict.fromkeys(lo for p in plan for lo in p.lo_ids) if lo in outcomes][
        :4
    ]
    summary += [_term_line(g.term, g.definition) for g in lecture.glossary][: max(0, 5 - len(summary))]
    if summary:
        slides.append(Slide(title="Итоги лекции", bullets=summary[:5]))
    return slides


def _term_line(term: str, definition: str) -> str:
    """«pandas — pandas — библиотека…» → «pandas — библиотека…»: модель часто повторяет термин в определении."""
    head = definition.strip()
    if head.lower().startswith(term.strip().lower()):
        return head
    return f"{term} — {head}"


def _order_final_test(kit: Kit) -> None:
    """Вопросы итогового теста — в порядке модулей и тем (шаги модулей завершаются в произвольном порядке)."""
    rank = {}
    for i, module in enumerate(kit.modules_sorted()):
        for j, topic in enumerate(kit.topics_of(module.id)):
            rank[topic.id] = (i, j)
    questions = {q.id: q for q in kit.questions}
    for test in kit.tests:
        if test.scope == TestScope.FINAL:
            test.question_ids.sort(
                key=lambda qid: (
                    (*rank.get(questions[qid].topic_id, (99, 99)), qid) if qid in questions else (99, 99, qid)
                )
            )


# --------------------------------------------------------------------------------------------------
# Судья: критерии по параметрам запроса и выжимка комплекта (docs/checks_matrix.md §5.3)
# --------------------------------------------------------------------------------------------------

JUDGE_TEXTS = {
    "J1": "Язык и терминология соответствуют аудитории; термины поясняются; нет жаргона.",
    "J2": "Уровень, последовательность тем и входные требования соответствуют входному уровню слушателей.",
    "J3": "Результаты обучения реалистичны для объёма программы; нет обещаний сверх объёма.",
    "J4": "Ситуации заданий взяты из профессиональной среды слушателей и реалистичны.",
    "J5": "Итоговая аттестация и задания соответствуют форме из запроса; требования конкретны, критерии измеримы, "
    "пример задания выполним.",
    "J6": "Содержание тем и лекций действительно раскрывает привязанные результаты обучения.",
    "J7": "Нет устаревших инструментов, версий и практик.",
    "J9": "Аннотация, цель и планируемые результаты по сути соответствуют своим разделам.",
}


def judge_criteria(kit: Kit) -> list[tuple[str, str]]:
    ids = ["J1", "J2", "J3"]
    if kit.seminars:
        ids.append("J4")
    ids += ["J5", "J6"]
    if kit.program.type != ProgramType.DOP or kit.sources:
        ids.append("J7")
    ids.append("J9")
    return [(cid, JUDGE_TEXTS[cid]) for cid in ids]


def judge_summary(kit: Kit) -> str:
    numbers = display_numbers(kit)
    program = kit.program
    lines = [prompts.describe_program(kit), f"Объём: {program.total_hours:g} ч."]
    fa = kit.final_attestation
    if kit.texts.annotation or kit.texts.relevance:
        lines.append(f"Аннотация: {kit.texts.annotation or kit.texts.relevance}")
    lines.append("Модули и темы (часы: всего / теория / практика / самостоятельная):")
    for module in kit.modules_sorted():
        lines.append(f"Модуль {numbers.get(module.id, '')} «{module.title}»")
        for topic in kit.topics_of(module.id):
            h = topic.hours
            lines.append(
                f"  {numbers.get(topic.id, '')} {topic.title} — {h.total:g}/{h.lk:g}/{h.pr + h.lb:g}/{h.srs:g}; "
                f"РО: {', '.join(topic.lo_ids)}"
            )
    lines.append("Результаты обучения:")
    lines.append(prompts.describe_outcomes(kit))
    if fa is not None:
        lines.append(
            f"Итоговая аттестация: {fa.form_text or fa.form.value}. Требования: {'; '.join(fa.requirements[:5])}."
        )
        if fa.example_task is not None:
            lines.append(f"Пример задания: {fa.example_task.title}. {fa.example_task.description}")
        lines.append("Критерии ИА: " + "; ".join(f"{c.text} ({c.max_points:g})" for c in fa.criteria))
    texts = kit.texts
    for label, value in (
        ("Форма обучения (текст программы)", texts.form_description),
        ("Материально-техническое обеспечение", "; ".join(texts.mto)),
        ("ПО и информационные системы", "; ".join(texts.software)),
        ("Организация процесса", texts.org_process),
    ):
        if value:
            lines.append(f"{label}: {value}")
    if fa is not None:
        if fa.data_requirements:
            lines.append("Данные для ИА: " + "; ".join(fa.data_requirements))
        if fa.procedure:
            lines.append("Процедура ИА: " + "; ".join(fa.procedure))
        lines.append(f"Порог ИА: {fa.pass_rule}")
    for seminar in kit.seminars[:6]:
        lines.append(
            f"Задание «{seminar.title}»: {seminar.situation} Шаги: {'; '.join(seminar.steps)}. Сдаёт: "
            f"{seminar.deliverable}. Критерии: "
            + "; ".join(f"{c.text} ({c.max_points:g})" for c in seminar.criteria)
        )
    for question in kit.questions[:: max(1, len(kit.questions) // 5)][:5]:
        right = "; ".join(question.options[i] for i in question.correct if i < len(question.options))
        lines.append(
            f"Вопрос: {question.stem} Варианты: {' | '.join(question.options)}. Ключ: {right}. "
            f"Пояснение: {question.explanation}"
        )
    for lecture in [*kit.lectures[:1], *kit.lectures[-1:]][: 2 if len(kit.lectures) > 1 else 1]:
        plan = "; ".join(f"{p.title}: {', '.join(p.theses[:3])}" for p in lecture.plan)
        lines.append(f"План лекции «{lecture.title}»: {plan}")
    return "\n".join(lines)


def merge_costs(costs: Sequence[StepCost]) -> list[StepCost]:
    """Стоимость по шагам для отчёта (шаги с одинаковым именем складываются)."""
    by_step: dict[str, StepCost] = {}
    for item in costs:
        known = by_step.setdefault(item.step, StepCost(step=item.step))
        known.calls += item.calls
        known.prompt_tokens += item.prompt_tokens
        known.completion_tokens += item.completion_tokens
        known.cost_rub = round(known.cost_rub + item.cost_rub, 6)
        known.seconds = round(known.seconds + item.seconds, 3)
    return list(by_step.values())
