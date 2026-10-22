"""Точечная правка готового комплекта (docs/ARCHITECTURE.md §5).

1. План. Простые команды разбирает код (`parse_command`): «часы темы 2.3: ЛК 2, ПР 4», «пересобери тему 2.3»,
   «усложни итоговый тест», «замени тему 2.3 на «…»», «удали тему 2.3», «добавь тему «…» в модуль 2».
   Свободный текст — один вызов навыка patch (`app/skills/patch.md`): модель называет операцию и объект,
   код проверяет план. Числа часов модель не пересчитывает — она цитирует фрагмент указания, числа берёт код.
2. Структура — код: новое название темы, формулировка РО, часы темы (rebalance в пределах модуля), новая или
   удалённая тема; затем календарь.
3. Что пересобрать — `app/core/graph.py` (таблица §5.2). Модель пишет только затронутые объекты — теми же навыками,
   что при сборке, с указанием методиста; пересобранные объекты сохраняют свои ID.
4. Слайды, метки «[не подтверждено]», самопроверка — код. Выгрузки делает оркестратор (`pipeline.render`).

Всё делается на копии комплекта: сбой на любом шаге — `PatchFailed` с объяснением для методиста, комплект прежний.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from app.config import now_msk
from app.core import bibliography
from app.core.calendar import build_calendar
from app.core.graph import Affected, GraphError, affected, kind_changes, kind_of, topic_removal
from app.core.hours import apply_hours, fmt_hours, rebalance
from app.core.ids import (
    AmbiguousReference,
    IdAllocator,
    UnknownReference,
    display_label,
    display_numbers,
    resolve_ref,
)
from app.core.validate import mark_unknown_codes
from app.llm import LLMClient
from app.models import (
    FinalAttestationDraft,
    HoursRow,
    Kit,
    Lecture,
    MethodistWarning,
    Module,
    PatchOperation,
    PatchPlanDraft,
    PatchRecord,
    Seminar,
    Severity,
    StepCost,
    Test,
    TestQuestion,
    TestScope,
    Topic,
    ValidationReport,
    WorkKind,
)
from app.pipeline import content_apply as apply
from app.pipeline import content_prompts as prompts
from app.pipeline.base import AgentError, EditConflict, PatchResult, StepContext, TargetError
from app.pipeline.content import (
    ContentGenerator,
    build_slides,
    gather_or_cancel,
    mark_model_texts,
    slides_lectures,
    strip_service_ids,
)
from app.pipeline.editing import (
    RECOMPUTED_WARNINGS,
    HoursInput,
    SkeletonEdits,
    TopicEdit,
    apply_skeleton_edits,
    conflict_details,
    kit_constraints,
)
from app.pipeline.errors import classify
from app.skills import load_skill

logger = logging.getLogger("karkas.patch")

PATCH_STEP = "patch"
HOW_TO = (
    "Сформулируйте правку, например: «часы темы 2.3: ЛК 2, ПР 4», «пересобери тему 2.3», «замени тему 2.3 на "
    "«Сводные таблицы»», «усложни итоговый тест», «удали тему 2.3», «добавь тему «Очистка данных» в модуль 2»."
)
MIN_REPLACED_SHARE = 0.5  # годных вопросов меньше половины — правка вопросов не применяется
KINDS = (WorkKind.LK, WorkKind.PR, WorkKind.LB, WorkKind.SRS)
KIND_LABELS = {WorkKind.LK: "ЛК", WorkKind.PR: "ПР", WorkKind.LB: "ЛБ", WorkKind.SRS: "СРС"}


class PatchFailed(AgentError):
    """Правка не выполнена; комплект не изменился. Текст — для методиста."""

    code = "patch_failed"


# --------------------------------------------------------------------------------------------------
# План
# --------------------------------------------------------------------------------------------------


@dataclass
class HoursAsk:
    """Часы из указания методиста: вид работы (None — вся тема), число и знак: 0 — «сделать столько»,
    +1 / −1 — прибавить / убавить к прежним часам."""

    kind: WorkKind | None
    value: float
    sign: int = 0
    fixed: bool = False  # «до N часов» — ровно столько, даже рядом с «увеличь» / «уменьши»


@dataclass
class PatchPlan:
    operation: PatchOperation
    target_id: str
    title: str = ""  # новое название темы
    text: str = ""  # новая формулировка РО
    module_id: str = ""  # модуль новой темы
    after_id: str = ""  # новая тема — после этой
    hours: list[HoursAsk] = field(default_factory=list)  # часы из указания (их разбирает код)
    program_wide: bool = False  # «… по всей программе»: остаток часов — по всем модулям, а не внутри модуля
    focus: str = ""
    planned_by: Literal["rules", "model"] = "rules"
    subject_id: str = ""  # объект, который пересобирается (у новой темы — её ID)


_KIND_WORDS: list[tuple[re.Pattern[str], WorkKind]] = [
    (re.compile(r"^(?:лк|лекц\w*|теор\w*)$"), WorkKind.LK),
    (re.compile(r"^(?:пр|практ\w*|пз)$"), WorkKind.PR),
    (re.compile(r"^(?:лб|лр|лаб\w*)$"), WorkKind.LB),
    (re.compile(r"^(?:срс|ср|самост\w*)$"), WorkKind.SRS),
]
_KIND = r"лк|лекц\w*|теор\w*|пр|практ\w*|пз|лб|лр|лаб\w*|срс|ср|самост\w*"
_NUM = r"\d+(?:[.,]\d+)?"
_UNIT = r"(?:ч\b\.?|час\w*)"
_REF = r"(?:\s+эт\w+)?(?:\s+\d+(?:\.\d+)?)?"
_QUOTE = r"\s*[«\"“„]"
# пары «вид работы — часы»: с единицей измерения; без неё — только в явном списке «часы темы 2.3: ЛК 2, ПР 4»
KIND_UNIT_RE = re.compile(rf"\b({_KIND})\b\.?\s*(?:[:=—–-]\s*)?({_NUM})\s*{_UNIT}", re.IGNORECASE)
KIND_BARE_LIST_RE = re.compile(rf"\b({_KIND})\b\.?\s*(?:[:=—–-]\s*)?({_NUM})\b(?![.,]\d)", re.IGNORECASE)
NUMBER_FIRST_RE = re.compile(rf"({_NUM})\s*{_UNIT}\s+({_KIND})\b", re.IGNORECASE)
KIND_TO_RE = re.compile(rf"\b({_KIND})\b\s+(до|на)\s+({_NUM})\s*{_UNIT}", re.IGNORECASE)
TOTAL_RE = re.compile(rf"(?:\b(до|на)\s+)?({_NUM})\s*{_UNIT}", re.IGNORECASE)
KIND_WORD_RE = re.compile(rf"\b(?:{_KIND})\b", re.IGNORECASE)
PLUS_RE = re.compile(r"\b(?:добав\w*|прибав\w*|увелич\w*|ещ[её])\b", re.IGNORECASE)
MINUS_RE = re.compile(r"\b(?:убав\w*|уменьш\w*|сократ\w*|сним\w*|забер\w*|меньше)\b", re.IGNORECASE)
# формы, в которых часы разбирает код; остальное («в лекции 3 примера устарели») — навыку
HOURS_LIST_RE = re.compile(r"^\s*(?:часы|нагрузк\w*)\b", re.IGNORECASE)
TOPIC_TOTAL_RE = re.compile(rf"\bтем\w*{_REF}\s+(?:на|до)\s+{_NUM}\s*{_UNIT}", re.IGNORECASE)
HOURS_VERB_RE = re.compile(
    r"^\s*(?:добав\w*|прибав\w*|убав\w*|уменьш\w*|сократ\w*|увелич\w*|дай\w*|сним\w*|забер\w*)\b",
    re.IGNORECASE,
)
QUOTED_RE = re.compile(r"[«\"“„]([^«»\"“”„]{3,200})[»\"”“]")
HARDEN_RE = re.compile(r"\b(?:усложн\w*|сложнее|повыс\w*\s+сложност\w*)", re.IGNORECASE)
QUESTIONS_RE = re.compile(r"тест|вопрос", re.IGNORECASE)
# «удали тему 2.3» — вся команда целиком; «убери из темы 2.3 пример» — не удаление темы (это разберёт навык)
REMOVE_TOPIC_RE = re.compile(
    rf"^\s*(?:удал\w*|убер\w*|убра\w*|исключ\w*){_REF}\s+тем\w*{_REF}(?:\s+из\s+программ\w*)?\s*[.!]?\s*$",
    re.IGNORECASE,
)
# «добавь (новую / ещё одну) тему «…»»; «добавь в тему «…» пример» — не новая тема
ADD_TOPIC_RE = re.compile(
    rf"\b(?:добав\w*|встав\w*)\s+(?:ещ[её]\s+)?(?:одн\w+\s+)?(?:нов\w+\s+)?тем[уы]\b{_REF}{_QUOTE}",
    re.IGNORECASE,
)
REPLACE_TOPIC_RE = re.compile(
    rf"\b(?:замени\w*|заменить|поменя\w*|переимен\w*|назов\w*|назвать){_REF}\s+тем\w*{_REF}\s+(?:на|в){_QUOTE}",
    re.IGNORECASE,
)
REPLACE_LO_RE = re.compile(
    rf"\b(?:замени\w*|заменить|поменя\w*|переформулир\w*){_REF}\s+(?:результат\w*(?:\s+обучения)?|ро){_REF}"
    rf"\s+(?:на|так){_QUOTE}",
    re.IGNORECASE,
)
REPLACE_RE = re.compile(r"\b(?:замен\w*|поменя\w*|переимен\w*|назов\w*|назвать)\b", re.IGNORECASE)
REGENERATE_RE = re.compile(
    r"\b(?:пересобер\w*|пересобра\w*|перегенер\w*|сгенерир\w*\s+заново|переделай\w*|переделать|перепиш\w*|"
    r"переписать|обнови\w*|обновить)\b",
    re.IGNORECASE,
)
PROGRAM_WIDE_RE = re.compile(r"\bпо\s+всей\s+программ\w*|\bза\s+сч[её]т\s+други\w+\s+модул", re.IGNORECASE)
# упоминания объектов в указании: они должны совпадать с выбранным объектом правки
MENTION_RE = re.compile(r"\b(тем[аеуы]|модул[ьяюе]|тест[ауе]?)\s+(\d+(?:\.\d+)?)\b", re.IGNORECASE)


def _number(text: str) -> float:
    return float(text.replace(",", "."))


def _kind(word: str) -> WorkKind | None:
    word = word.lower().replace("ё", "е")
    return next((kind for pattern, kind in _KIND_WORDS if pattern.match(word)), None)


def _sign(segment: str) -> int:
    """Знак по последнему глаголу в отрезке: «сократи …» → −1, «добавь …» → +1, глагола нет → 0."""
    last = max(
        [(m.start(), 1) for m in PLUS_RE.finditer(segment)]
        + [(m.start(), -1) for m in MINUS_RE.finditer(segment)],
        default=(-1, 0),
    )
    return last[1]


def parse_hours(text: str, bare: bool = False) -> list[HoursAsk]:
    """Часы из указания методиста. У каждой пары свой знак — по ближайшему глаголу перед ней («сократи практику
    на 1 час и добавь 1 час лекций»); «до N часов» — ровно столько. bare — числа без «ч» (явный список
    «часы темы 2.3: ЛК 2, ПР 4»). Вид работы не назван — часы всей темы («тема 2.3 на 6 часов»). Шаг 0,5 ч проверяет
    HoursInput."""
    found: list[tuple[int, int, WorkKind, float, bool]] = []  # начало, конец, вид, число, «до»
    patterns: list[tuple[re.Pattern[str], str]] = [
        (KIND_TO_RE, "kind-to"),
        (KIND_UNIT_RE, "kind"),
        (NUMBER_FIRST_RE, "number"),
    ]
    if bare:
        patterns.append((KIND_BARE_LIST_RE, "kind"))
    for pattern, form in patterns:
        for m in pattern.finditer(text):
            if any(m.start() < end and start < m.end() for start, end, *_ in found):
                continue
            if form == "kind-to":
                kind, value, fixed = _kind(m.group(1)), m.group(3), m.group(2).lower() == "до"
            elif form == "kind":
                kind, value, fixed = _kind(m.group(1)), m.group(2), False
            else:
                kind, value, fixed = _kind(m.group(2)), m.group(1), False
            if kind is not None:
                found.append((m.start(), m.end(), kind, _number(value), fixed))
    found.sort()
    asks: list[HoursAsk] = []
    previous_end, previous_sign = 0, 0
    for start, end, kind, value, fixed in found:
        sign = 0 if fixed else (_sign(text[previous_end:start]) or previous_sign)
        asks.append(HoursAsk(kind, value, sign, fixed))
        previous_end, previous_sign = end, sign
    if asks or KIND_WORD_RE.search(text):
        return asks
    totals = list(TOTAL_RE.finditer(text))
    if not totals:
        return []
    m = totals[-1]
    fixed = (m.group(1) or "").lower() == "до"
    return [HoursAsk(None, _number(m.group(2)), 0 if fixed else _sign(text[: m.start()]), fixed)]


def quoted_title(text: str) -> str:
    match = QUOTED_RE.search(text)
    return match.group(1).strip().strip(".") if match else ""


def check_mentions(kit: Kit, target_id: str, instruction: str) -> None:
    """Тема, модуль или тест, названные в указании (вне кавычек), должны совпадать с выбранным объектом правки
    того же вида: «удали тему 2.4» при выбранной теме 2.3 — не правка темы 2.3."""
    text = QUOTED_RE.sub(" ", instruction)
    kind = kind_of(target_id)
    for word, number in MENTION_RE.findall(text):
        phrase = f"{word.lower()} {number}"
        try:
            obj_id = resolve_ref(kit, phrase)
        except AmbiguousReference:
            continue
        except UnknownReference as exc:
            raise PatchFailed(f"{exc} Укажите объект правки, который есть в комплекте.") from exc
        if kind_of(obj_id) == kind and obj_id != target_id:
            raise PatchFailed(
                f"В указании названа «{display_label(kit, obj_id).lower()}», а правка выбрана для "
                f"«{display_label(kit, target_id).lower()}». Укажите один и тот же объект. Комплект не изменён."
            )


def parse_command(kit: Kit, target_id: str, instruction: str) -> PatchPlan | None:
    """Простые команды — кодом, без модели. None — команда не распознана (её разберёт навык patch).
    Шаблоны узкие: лучше отдать фразу навыку, чем выполнить не ту операцию."""
    text = instruction.strip()
    kind = kind_of(target_id)
    if not text:
        return PatchPlan(PatchOperation.REGENERATE, target_id)
    title = quoted_title(text)
    replacing = bool(REPLACE_RE.search(text))
    if ADD_TOPIC_RE.search(text) and title and kind in ("M", "T"):
        rest = QUOTED_RE.sub(" ", text)
        return _add_plan(kit, target_id, title, parse_hours(rest))
    if REMOVE_TOPIC_RE.match(text) and kind == "T":
        return PatchPlan(PatchOperation.REMOVE_TOPIC, target_id)
    if HARDEN_RE.search(text) and QUESTIONS_RE.search(text) and kind in ("TS", "Q", "T", "M"):
        return PatchPlan(PatchOperation.HARDEN_TEST, target_id)
    if kind == "T" and not (title or replacing):
        hours = _explicit_hours(text)
        if hours:
            return PatchPlan(
                PatchOperation.CHANGE_HOURS,
                target_id,
                hours=hours,
                program_wide=bool(PROGRAM_WIDE_RE.search(text)),
            )
    if kind == "T" and title and REPLACE_TOPIC_RE.search(text):
        return PatchPlan(PatchOperation.REPLACE_TOPIC, target_id, title=title)
    if kind == "LO" and title and REPLACE_LO_RE.search(text):
        return PatchPlan(PatchOperation.REPLACE_LO, target_id, text=title)
    if REGENERATE_RE.search(text) and not (title or replacing):
        return PatchPlan(PatchOperation.REGENERATE, target_id)
    return None


def _explicit_hours(text: str) -> list[HoursAsk]:
    """Часы, которые код разбирает сам: явный список («часы темы 2.3: ЛК 2, ПР 4»), часы темы целиком («тема 2.3
    на 6 часов») или команда с глаголом про часы («добавь 2 часа практики», «уменьши лекции до 1 часа»)."""
    if HOURS_LIST_RE.match(text):
        return parse_hours(text, bare=True)
    if TOPIC_TOTAL_RE.search(text) and not KIND_WORD_RE.search(text):
        return parse_hours(text)
    if HOURS_VERB_RE.match(text):
        return [ask for ask in parse_hours(text) if ask.kind is not None]
    return []


def _add_plan(kit: Kit, target_id: str, title: str, hours: list[HoursAsk]) -> PatchPlan:
    if kind_of(target_id) == "M":
        module_id, after_id = target_id, ""
    else:
        topic = next(t for t in kit.topics if t.id == target_id)
        module_id, after_id = topic.module_id, topic.id
    return PatchPlan(
        PatchOperation.ADD_TOPIC,
        target_id,
        title=title,
        module_id=module_id,
        after_id=after_id,
        hours=[HoursAsk(a.kind, a.value) for a in hours],  # часы новой темы — как сказано, без «прибавить»
    )


OPERATION_TARGETS: dict[PatchOperation, tuple[str, ...]] = {
    PatchOperation.REPLACE_TOPIC: ("T",),
    PatchOperation.CHANGE_HOURS: ("T",),
    PatchOperation.REMOVE_TOPIC: ("T",),
    PatchOperation.REPLACE_LO: ("LO",),
    PatchOperation.ADD_TOPIC: ("M", "T"),
    PatchOperation.HARDEN_TEST: ("TS", "Q", "T", "M"),
}


def plan_from_draft(kit: Kit, target_id: str, instruction: str, draft: PatchPlanDraft) -> PatchPlan:
    """Ответ навыка patch → проверенный план. Объект — из запроса правки; объект модели берётся, только если
    объект запроса не подходит к операции (например, указан модуль, а заменить нужно тему)."""
    try:
        operation = PatchOperation(draft.operation.strip().lower())
    except ValueError as exc:
        raise PatchFailed(f"Не удалось понять, что изменить. {HOW_TO}") from exc
    if operation == PatchOperation.REMOVE_TOPIC:  # необратимо — только явной командой, не по догадке модели
        raise PatchFailed(
            "Похоже, нужно удалить тему. Удаление выполняется только явной командой: «удали тему 2.3» "
            "(объект правки — эта тема). Комплект не изменён."
        )
    needs = OPERATION_TARGETS.get(operation)
    target = target_id
    if needs and kind_of(target) not in needs:
        target = _resolve_model_ref(kit, draft.target or draft.module, needs)
    title = apply.clean_item(draft.title).strip('«»"“” .')
    plan = PatchPlan(operation, target, focus=apply.clean(draft.focus), planned_by="model")
    if operation == PatchOperation.REPLACE_TOPIC:
        plan.title = title
        return plan
    if operation == PatchOperation.REPLACE_LO:
        plan.text = apply.clean_prose(draft.text).strip('«»"“” ')
        if not plan.text:
            raise PatchFailed(
                "Не удалось понять новую формулировку результата обучения — напишите её в кавычках."
            )
        return plan
    if operation in (PatchOperation.CHANGE_HOURS, PatchOperation.ADD_TOPIC):
        plan.hours = _model_hours(draft.quote, instruction)
        plan.program_wide = bool(PROGRAM_WIDE_RE.search(instruction))
        if operation == PatchOperation.CHANGE_HOURS and not plan.hours:
            raise PatchFailed(
                "В указании не нашлось часов. Напишите, например: «часы темы 2.3: ЛК 2, ПР 4» "
                "или «тема 2.3 на 6 часов»."
            )
        if operation == PatchOperation.ADD_TOPIC:
            if not title:
                raise PatchFailed("Не удалось понять название новой темы — напишите его в кавычках.")
            added = _add_plan(kit, target, title, plan.hours)
            added.focus, added.planned_by = plan.focus, "model"
            return added
    return plan


def _model_hours(quote: str, instruction: str) -> list[HoursAsk]:
    """Часы по цитате модели: числа — только из указания методиста (цитата должна быть в нём дословно). Знак, если
    глагола в цитате нет, — из указания; если там и «добавь», и «убавь», — просим написать часы явно."""
    if not _quote_in(quote, instruction):
        return []
    asks = parse_hours(quote, bare=True)
    plus, minus = bool(PLUS_RE.search(instruction)), bool(MINUS_RE.search(instruction))
    unsigned = [a for a in asks if a.sign == 0 and not a.fixed]
    if unsigned and not (PLUS_RE.search(quote) or MINUS_RE.search(quote)) and (plus or minus):
        if plus and minus:
            raise PatchFailed(
                "Не понял, какие часы прибавить, а какие убавить. Напишите часы темы явно: «часы темы 2.3: ЛК 2, ПР 4». "
                "Комплект не изменён."
            )
        for ask in unsigned:
            ask.sign = 1 if plus else -1
    return asks


def _quote_in(quote: str, instruction: str) -> bool:
    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.lower().replace("ё", "е")).strip()

    return bool(quote.strip()) and norm(quote) in norm(instruction)


def _resolve_model_ref(kit: Kit, ref: str, kinds: Sequence[str]) -> str:
    try:
        obj_id = resolve_ref(kit, ref) if ref.strip() else ""
    except (UnknownReference, AmbiguousReference) as exc:
        raise PatchFailed(f"Не удалось понять, к какому объекту относится правка: {exc}") from exc
    if not obj_id or kind_of(obj_id) not in kinds:
        raise PatchFailed(f"Эта правка не подходит к указанному объекту. {HOW_TO}")
    return obj_id


def describe_outline(kit: Kit) -> str:
    """Оглавление комплекта для навыка patch: модули, темы с часами и РО, результаты обучения, тесты."""
    numbers = display_numbers(kit)
    lines: list[str] = []
    for module in kit.modules_sorted():
        lines.append(f"Модуль {numbers[module.id]} ({module.id}) «{module.title}»")
        for topic in kit.topics_of(module.id):
            lines.append(
                f"  Тема {numbers[topic.id]} ({topic.id}) «{topic.title}» — {_row_text(topic.hours)}; "
                f"РО: {', '.join(topic.lo_ids)}"
            )
    lines.append("Результаты обучения:")
    lines += [f"  РО {numbers.get(o.id, '')} ({o.id}): {o.text}" for o in kit.outcomes]
    if kit.tests:
        lines.append("Тесты:")
        for test in kit.tests:
            scope = (
                "итоговый"
                if test.scope == TestScope.FINAL
                else f"по модулю {numbers.get(test.module_id or '', '')}"
            )
            lines.append(
                f"  Тест {numbers[test.id]} ({test.id}) — {scope}, вопросов: {len(test.question_ids)}"
            )
    fa = kit.final_attestation
    if fa is not None:
        lines.append(f"Итоговая аттестация (FA): {fa.form_text or fa.form.value}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------------
# Исполнитель
# --------------------------------------------------------------------------------------------------


@dataclass
class _Outcome:
    """Что сказать методисту по итогам правки."""

    notes: list[str] = field(default_factory=list)
    warnings: list[MethodistWarning] = field(default_factory=list)
    head: str = ""  # первая фраза итога: что сделано


ValidateFn = Callable[[Kit, Iterable[MethodistWarning], StepContext], ValidationReport]


class PatchEngine:
    def __init__(self, content: ContentGenerator, validate: ValidateFn) -> None:
        self.content = content
        self.validate = validate

    async def run(self, kit: Kit, target_id: str, instruction: str, ctx: StepContext) -> PatchResult:
        client = self.content.client_factory(ctx)
        try:
            check_mentions(kit, target_id, instruction)
            plan = parse_command(kit, target_id, instruction)
            if plan is None:
                plan = await self._model_plan(kit, target_id, instruction, client)
            work = kit.model_copy(deep=True)
            outcome = _Outcome()
            extra = self._structure(work, kit, plan, outcome)
            try:
                subject = plan.subject_id or plan.target_id
                plan_affected = affected(work, plan.operation, subject).merge(extra)
            except GraphError as exc:
                raise PatchFailed(str(exc)) from exc
            _retire(work, plan_affected)
            note = _note(plan, instruction)
            await self._rebuild(work, plan, plan_affected, note, client, outcome)
            self._finish(work, kit, plan_affected)
        except AgentError:
            raise
        except Exception as exc:  # сбой шлюза, бюджет, ответ не по схеме — понятной фразой; комплект прежний
            info = classify(exc)
            hint = f" {info.hint}" if info.hint else ""
            raise PatchFailed(f"Правка не выполнена: {info.message}{hint} Комплект не изменился.") from exc
        finally:
            cost = _take_cost(client)
        _add_cost(work, cost)
        work.report = await asyncio.to_thread(self.validate, work, outcome.warnings, ctx)
        _reject_new_errors(kit, work)
        changed, removed = diff_ids(kit, work)
        summary = _summary(outcome.head, changed, removed)
        if outcome.notes:  # что модель не сделала — тоже в историю правок, не только в notes ответа
            summary += " " + " ".join(outcome.notes)
        work.revision = kit.revision + 1
        work.history.append(
            PatchRecord(
                revision=work.revision,
                operation=plan.operation,
                target_ids=[plan.subject_id or plan.target_id],
                instruction=instruction,
                changed_ids=changed,
                removed_ids=removed,
                summary=summary,
                planned_by=plan.planned_by,
                cost_rub=round(cost.cost_rub, 4),
                at=now_msk(),
            )
        )
        return PatchResult(
            kit=work,
            changed_ids=changed,
            notes=outcome.notes,
            summary=summary,
            operation=plan.operation.value,
        )

    # ---- план моделью ----------------------------------------------------------------------------

    async def _model_plan(self, kit: Kit, target_id: str, instruction: str, client: LLMClient) -> PatchPlan:
        skill = load_skill("patch")
        target = f"{display_label(kit, target_id)} ({target_id})"
        obj = kit.index().get(target_id)
        title = getattr(obj, "title", None) or getattr(obj, "text", None)
        if title:
            target += f" «{title}»"
        draft = await client.complete_json(
            PATCH_STEP,
            skill.model,
            skill.messages(outline=describe_outline(kit), target=target, instruction=instruction),
            PatchPlanDraft,
            max_tokens=skill.max_tokens,
            temperature=skill.temperature,
            fixture_key=f"{ContentGenerator._key(kit)}-patch",
        )
        assert isinstance(draft, PatchPlanDraft)
        return plan_from_draft(kit, target_id, instruction, draft)

    # ---- структура (код) -------------------------------------------------------------------------

    def _structure(self, work: Kit, before: Kit, plan: PatchPlan, outcome: _Outcome) -> Affected:
        """Изменения структуры кодом; возвращает следствия для графа (новые и удалённые лекции, задания)."""
        numbers = display_numbers(before)
        op = plan.operation
        if op == PatchOperation.REPLACE_TOPIC:
            topic = _topic(work, plan.target_id)
            old = topic.title
            if plan.title and plan.title != old:
                plan.title = self._marked(plan.title)
                topic.title = plan.title
                outcome.head = f"Тема {numbers[topic.id]} заменена: «{old}» → «{plan.title}»"
            else:
                outcome.head = f"Тема {numbers[topic.id]} «{old}» пересобрана"
            return Affected()
        if op == PatchOperation.REPLACE_LO:
            lo = next(o for o in work.outcomes if o.id == plan.target_id)
            plan.text = self._marked(plan.text)
            lo.text = plan.text
            outcome.head = f"Результат обучения {numbers.get(lo.id, '')} сформулирован заново: «{plan.text}»"
            return Affected()
        if op == PatchOperation.CHANGE_HOURS:
            return self._change_hours(work, before, plan, outcome)
        if op == PatchOperation.ADD_TOPIC:
            return self._add_topic(work, before, plan, outcome)
        if op == PatchOperation.REMOVE_TOPIC:
            return self._remove_topic(work, before, plan, outcome)
        if op == PatchOperation.HARDEN_TEST:
            outcome.head = f"{display_label(before, plan.target_id)}: вопросы усложнены"
            return Affected()
        outcome.head = f"{display_label(before, plan.target_id)}: материалы пересобраны заново"
        return Affected()

    def _marked(self, text: str) -> str:
        """Коды профстандартов и номера актов в новом названии или формулировке — «[не подтверждено]», если их нет
        в реестре (правило 3; название пишет методист или модель)."""
        return mark_unknown_codes(text, self.content.registry)

    def _change_hours(self, work: Kit, before: Kit, plan: PatchPlan, outcome: _Outcome) -> Affected:
        topic = _topic(work, plan.target_id)
        old_row = topic.hours.model_copy()
        row = _wanted_row(old_row, plan.hours)
        if row == _wanted_row(old_row, []):
            raise PatchFailed(f"У темы уже такие часы: {_row_text(old_row)}. Комплект не изменён.")
        try:
            edits = SkeletonEdits(
                topics=[TopicEdit(id=topic.id, hours=HoursInput(**row))],
                hours_scope="program" if plan.program_wide else "module",
            )
        except ValueError as exc:
            raise PatchFailed("Часы задаются с шагом 0,5 ч — например, «ЛК 1,5, ПР 3».") from exc
        changed = self._apply_edits(work, edits, outcome)
        new_row = _topic(work, topic.id).hours
        moved = [tid for tid in changed if tid != topic.id]
        outcome.head = (
            f"Часы темы {display_numbers(before)[topic.id]}: {_row_text(old_row)} → {_row_text(new_row)}"
        )
        if moved:
            where = "программы" if plan.program_wide else "модуля"
            outcome.head += f"; другие темы {where} пересчитаны ({len(moved)})"
        outcome.head += f"; объём программы прежний — {fmt_hours(work.program.total_hours)} ч"
        return kind_changes(before, work)

    def _apply_edits(self, work: Kit, edits: SkeletonEdits, outcome: _Outcome) -> list[str]:
        try:
            new, changed, warnings = apply_skeleton_edits(work, edits)
        except EditConflict as exc:
            raise PatchFailed(_conflict_text(exc), details=exc.details) from exc
        except TargetError as exc:
            raise PatchFailed(exc.message) from exc
        _replace_kit(work, new)
        outcome.warnings += warnings
        return changed

    def _add_topic(self, work: Kit, before: Kit, plan: PatchPlan, outcome: _Outcome) -> Affected:
        module = next(m for m in work.modules if m.id == plan.module_id)
        siblings = work.topics_of(module.id)
        anchor = next((t for t in siblings if t.id == plan.after_id), siblings[-1] if siblings else None)
        if anchor is None:
            raise PatchFailed("В модуле нет тем, рядом с которыми можно добавить новую.")
        topic_id = IdAllocator(work).topic(module.id)
        share = sum(t.hours.total for t in siblings) / (len(siblings) + 1)
        if plan.hours and all(a.kind is not None for a in plan.hours):  # «ЛК 1, ПР 3» — только названное
            row = _wanted_row(HoursRow(), plan.hours)
        else:  # «на 4 часа» или без часов — доли видов работы как у соседней темы, по умолчанию средняя доля
            asks = plan.hours or [HoursAsk(None, max(1.0, _half(share)))]
            row = _wanted_row(anchor.hours, asks)
        order = anchor.order + 1
        for t in siblings:  # темы после новой сдвигаются (их номер в модуле тоже меняется)
            if t.order >= order:
                t.order += 1
        plan.title = self._marked(plan.title)
        work.topics.append(
            Topic(
                id=topic_id,
                module_id=module.id,
                order=order,
                title=plan.title,
                lo_ids=list(anchor.lo_ids),
                control_form=anchor.control_form,
                tags=list(anchor.tags),
            )
        )
        plan.subject_id = topic_id
        try:
            locked = {topic_id: HoursInput(**row).to_row()}
        except ValueError as exc:
            raise PatchFailed("Часы задаются с шагом 0,5 ч — например, «ЛК 1, ПР 3».") from exc
        self._rebalance_in_module(work, module, locked, outcome)
        numbers = display_numbers(work)
        outcome.head = (
            f"В модуль {numbers[module.id]} добавлена тема {numbers[topic_id]} «{plan.title}» "
            f"({_row_text(_topic(work, topic_id).hours)}); часы других тем модуля пересчитаны, "
            f"объём программы прежний — {fmt_hours(work.program.total_hours)} ч"
        )
        return kind_changes(before, work)

    def _remove_topic(self, work: Kit, before: Kit, plan: PatchPlan, outcome: _Outcome) -> Affected:
        topic = _topic(work, plan.target_id)
        siblings = [t for t in work.topics_of(topic.module_id) if t.id != topic.id]
        if not siblings:
            raise PatchFailed(
                "В модуле одна тема — её нельзя удалить отдельно. Замените её («замени тему … на «…»») "
                "или измените состав модулей в каркасе новой сессии."
            )
        removal = topic_removal(work, topic.id)
        # РО, которые раскрывала только эта тема, переходят к соседней теме модуля — иначе они без тем (V15)
        neighbour = max(
            (t for t in siblings if t.order < topic.order), key=lambda t: t.order, default=siblings[0]
        )
        orphan = [
            lo for lo in topic.lo_ids if not any(lo in t.lo_ids for t in work.topics if t.id != topic.id)
        ]
        if orphan:
            neighbour.lo_ids = list(dict.fromkeys([*neighbour.lo_ids, *orphan]))
        work.topics = [t for t in work.topics if t.id != topic.id]
        module = next(m for m in work.modules if m.id == topic.module_id)
        self._rebalance_in_module(work, module, {}, outcome)
        numbers = display_numbers(before)
        outcome.head = (
            f"Тема {numbers[topic.id]} «{topic.title}» удалена; её часы переданы другим темам модуля "
            f"{numbers[module.id]}, объём программы прежний — {fmt_hours(work.program.total_hours)} ч"
        )
        if orphan:
            outcome.head += (
                f"; результаты обучения {', '.join(numbers.get(lo, lo) for lo in orphan)} теперь раскрывает "
                f"тема {numbers[neighbour.id]}"
            )
        lost = [x for x in removal.retire if kind_of(x) == "Q"]
        if lost:
            outcome.notes.append(
                f"Вместе с темой удалены тестовые вопросы по ней ({len(lost)}) — проверьте, хватает ли "
                "вопросов по результатам обучения (раздел «Покрытие» отчёта)."
            )
        return removal.merge(kind_changes(before, work))

    def _rebalance_in_module(
        self, work: Kit, module: Module, locked: dict[str, HoursRow], outcome: _Outcome
    ) -> None:
        """Часы перераспределяются только между темами модуля: темы других модулей зафиксированы как есть."""
        fixed = {t.id: t.hours.model_copy() for t in work.topics if t.module_id != module.id}
        fixed.update(locked)
        try:
            result = rebalance(work, fixed, kit_constraints(work), scope="program")
        except ValueError as exc:
            raise PatchFailed(str(exc)) from exc
        if not result.ok:
            raise PatchFailed(
                "Часы не сходятся: "
                + " ".join(c.message for c in result.conflicts)
                + " Комплект не изменён.",
                details=conflict_details(result),
            )
        apply_hours(work, result)
        calendar, warnings = build_calendar(work, kit_constraints(work))
        work.calendar = calendar
        if work.report is not None:  # срок и конфликты пересчитываются заново (как при правке каркаса)
            work.report.warnings_for_methodist = [
                w for w in work.report.warnings_for_methodist if w.type not in RECOMPUTED_WARNINGS
            ]
        outcome.warnings += warnings

    # ---- пересборка моделью ----------------------------------------------------------------------

    async def _rebuild(
        self, work: Kit, plan: PatchPlan, aff: Affected, note: str, client: LLMClient, outcome: _Outcome
    ) -> None:
        for topic_id, kinds in aff.drop_kinds.items():
            topic = _topic(work, topic_id)
            topic.content = {k: v for k, v in topic.content.items() if k not in kinds}
        for lecture_id, topic_ids in aff.detach.items():
            lecture = next(lec for lec in work.lectures if lec.id == lecture_id)
            lecture.topic_ids = [t for t in lecture.topic_ids if t not in topic_ids]
        modules = {m.id: m for m in work.modules}
        topic_module = {t.id: t.module_id for t in work.topics}
        harden = plan.operation == PatchOperation.HARDEN_TEST
        # вопросы — суть правки только у «усложни» и пересборки теста или вопроса; в правке темы вопросы, которые
        # модель не переписала (повторила прежние), остаются прежними — с заметкой, правка темы не отменяется
        strict = harden or (
            plan.operation == PatchOperation.REGENERATE and kind_of(plan.target_id) in ("TS", "Q")
        )
        touched = [topic_module[x] for x in [*aff.content, *aff.new_lectures, *aff.new_seminars]]
        touched += [lec.module_id for lec in work.lectures if lec.id in aff.lectures]
        touched += [topic_module[s.topic_id] for s in work.seminars if s.id in aff.seminars]
        jobs = [self._rebuild_topics(work, modules[mid], aff, note, client) for mid in dict.fromkeys(touched)]
        by_module: dict[str, list[str]] = {}
        for question in work.questions:
            if question.id in aff.questions:
                by_module.setdefault(topic_module[question.topic_id], []).append(question.id)
        for module_id, ids in by_module.items():
            jobs.append(
                self._rebuild_questions(work, modules[module_id], ids, note, harden, strict, client, outcome)
            )
        interview: dict[str, list[str]] = {}
        for iq in work.interview_questions:
            if iq.id in aff.interview:
                interview.setdefault(iq.module_id, []).append(iq.id)
        for module_id, ids in interview.items():
            jobs.append(self._rebuild_interview(work, modules[module_id], ids, note, client))
        if aff.final_attestation:
            jobs.append(self._rebuild_fa(work, note, client))
        # первый сбой отменяет остальные вызовы модели: правка всё равно не применится, деньги не тратятся зря
        await gather_or_cancel(jobs)

    async def _rebuild_topics(
        self, work: Kit, module: Module, aff: Affected, note: str, client: LLMClient
    ) -> None:
        """Содержание тем, лекции и задания одного модуля: черновики — к копии, затем перенос со старыми ID."""
        own = {t.id for t in work.topics_of(module.id)}
        content_ids = [t for t in aff.content if t in own]
        old_lectures = [lec for lec in work.lectures if lec.id in aff.lectures and lec.module_id == module.id]
        new_lecture_topics = [t for t in aff.new_lectures if t in own]
        # в запрос — все темы пересобираемых лекций (лекция может объединять несколько тем)
        lecture_topics = list(
            dict.fromkeys([*(tid for lec in old_lectures for tid in lec.topic_ids), *new_lecture_topics])
        )
        old_seminars = [s for s in work.seminars if s.id in aff.seminars and s.topic_id in own]
        new_seminar_topics = [t for t in aff.new_seminars if t in own]
        seminar_topics = list(dict.fromkeys([*(s.topic_id for s in old_seminars), *new_seminar_topics]))
        key = _patch_key(work, module)
        gen = self.content
        content_draft, lectures_draft, seminars_draft = await asyncio.gather(
            gen._draft_content(work, module, client, content_ids, note, key) if content_ids else _none(),
            gen._draft_lectures(work, module, client, lecture_topics, note, key)
            if lecture_topics
            else _none(),
            gen._draft_seminars(work, module, client, seminar_topics, note, key)
            if seminar_topics
            else _none(),
        )
        if content_ids:
            done = apply.apply_module_content(work, module, content_draft.topics, only=content_ids)  # type: ignore[union-attr]
            missing = [t for t in content_ids if t not in done]
            if missing:
                raise PatchFailed(_no_answer("пункты рабочей программы", work, missing))
        if lecture_topics:
            self._place_lectures(work, module, old_lectures, new_lecture_topics, lectures_draft.lectures)  # type: ignore[union-attr]
        if seminar_topics:
            self._place_seminars(work, module, old_seminars, new_seminar_topics, seminars_draft.seminars)  # type: ignore[union-attr]

    def _place_lectures(
        self, work: Kit, module: Module, old: list[Lecture], new_topics: list[str], drafts: Sequence
    ) -> None:
        old_ids = [lec.id for lec in old]
        scratch = _scratch(work, old_ids)
        scratch.lectures = [lec for lec in scratch.lectures if lec.id not in set(old_ids)]
        covered = [tid for lec in old for tid in lec.topic_ids]
        wanted = list(dict.fromkeys([*covered, *new_topics]))
        created = set(apply.apply_lectures(scratch, module, drafts, only=wanted))
        made = {tid: lec for lec in scratch.lectures if lec.id in created for tid in lec.topic_ids}
        missing = [lec.topic_ids[0] for lec in old if lec.topic_ids[0] not in made]
        missing += [t for t in new_topics if t not in made]
        if missing:
            raise PatchFailed(_no_answer("план лекции", work, missing))
        for lecture in old:
            fresh = _merged_lecture([made[tid] for tid in lecture.topic_ids if tid in made])
            fresh.id, fresh.topic_ids = lecture.id, list(lecture.topic_ids)
            fresh.title = _topic(work, lecture.topic_ids[0]).title  # заголовок лекции = тема плана (V20)
            fresh.slides = build_slides(work, fresh) if lecture.slides is not None else None
            _swap(work.lectures, lecture.id, fresh)
        for topic_id in new_topics:
            fresh = made[topic_id]
            work.lectures.append(fresh)
            if any(lec.id == fresh.id for lec in slides_lectures(work)):
                fresh.slides = build_slides(work, fresh)

    def _place_seminars(
        self, work: Kit, module: Module, old: list[Seminar], new_topics: list[str], drafts: Sequence
    ) -> None:
        old_ids = [s.id for s in old]
        scratch = _scratch(work, old_ids)
        scratch.seminars = [s for s in scratch.seminars if s.id not in set(old_ids)]
        wanted = list(dict.fromkeys([*(s.topic_id for s in old), *new_topics]))
        created = set(
            apply.apply_seminars(
                scratch, module, drafts, only=wanted, pass_share=self.content.limits.seminar_pass_share
            )
        )
        made: dict[str, list[Seminar]] = {}
        for seminar in scratch.seminars:
            if seminar.id in created:
                made.setdefault(seminar.topic_id, []).append(seminar)
        missing = [t for t in wanted if not made.get(t)]
        if missing:
            raise PatchFailed(_no_answer("задание", work, missing))
        for seminar in old:
            pool = made[seminar.topic_id]
            if not pool:  # у темы было два задания, а модель дала одно — второе остаётся прежним
                continue
            fresh = pool.pop(0)
            replacement = _renamed_seminar(fresh, seminar.id)
            replacement.lo_ids = list(seminar.lo_ids)  # то же покрытие РО, что и раньше
            _swap(work.seminars, seminar.id, replacement)
        for topic_id in new_topics:
            work.seminars.append(made[topic_id][0])

    async def _rebuild_questions(
        self,
        work: Kit,
        module: Module,
        ids: list[str],
        note: str,
        harden: bool,
        strict: bool,
        client: LLMClient,
        outcome: _Outcome,
    ) -> None:
        old = [q for q in work.questions if q.id in set(ids)]
        quotas = dict(Counter(q.lo_ids[0] for q in old if q.lo_ids))
        if not quotas:
            raise PatchFailed("У вопросов нет результатов обучения — пересобрать их по правилам нельзя.")
        stems = "\n".join(f"- {q.stem}" for q in old)
        if harden:
            level = min(3, max(q.difficulty for q in old) + 1)
            text = (
                f"{note}\nСделай вопросы сложнее прежних (сложность {level} из 3): на применение и анализ "
                "ситуации из работы слушателей, а не на узнавание определения; неверные варианты — "
                f"правдоподобные. Прежние вопросы — не повторяй их:\n{stems}"
            )
        else:
            text = f"{note}\nНапиши новые вопросы взамен прежних — не повторяй их:\n{stems}"
        draft = await self.content._draft_tests(
            work, module, client, quotas, 0, note=text, key=_patch_key(work, module)
        )
        # прежние вопросы остаются в копии: повтор их формулировки отбрасывается как дубль
        scratch = _scratch(work, ids)
        stub = Test(id="TS999", title="", scope=TestScope.MODULE, module_id=module.id)
        created = set(
            apply.apply_questions(
                scratch,
                module,
                stub,
                draft.questions,
                quotas,
                replacing=ids,
                banned=lambda text: bibliography.blocked_service(work, self.content.registry, text),
            )
        )
        fresh = [q for q in scratch.questions if q.id in created]
        placed = 0
        for question in old:
            match = _take_question(fresh, question)
            if match is None:
                continue
            difficulty = (
                max(match.difficulty, min(3, question.difficulty + 1)) if harden else match.difficulty
            )
            replacement = match.model_copy(
                update={
                    "id": question.id,
                    "test_id": question.test_id,
                    "topic_id": question.topic_id,
                    "lo_ids": list(question.lo_ids),
                    "difficulty": difficulty,
                }
            )
            _swap(work.questions, question.id, replacement)
            placed += 1
        if strict and placed < max(1, math.ceil(len(old) * MIN_REPLACED_SHARE)):
            raise PatchFailed(
                f"Модель вернула годных вопросов {placed} из {len(old)} (прочие повторяют прежние или без верного "
                "ответа) — правка вопросов не применена, комплект прежний. Повторите правку позже."
            )
        if placed < len(old):
            where = display_label(work, module.id).lower()
            outcome.notes.append(
                f"Тестовые вопросы ({where}): модель переписала {placed} из {len(old)}, остальные оставлены "
                "прежними — проверьте, подходят ли они к изменённому материалу."
            )

    async def _rebuild_interview(
        self, work: Kit, module: Module, ids: list[str], note: str, client: LLMClient
    ) -> None:
        old = [iq for iq in work.interview_questions if iq.id in set(ids)]
        los = list(dict.fromkeys(lo for iq in old for lo in iq.lo_ids[:1]))
        draft = await self.content._draft_tests(
            work, module, client, {}, len(old), note=note, key=_patch_key(work, module)
        )
        scratch = _scratch(work, ids)
        scratch.interview_questions = [iq for iq in scratch.interview_questions if iq.id not in set(ids)]
        created = set(apply.apply_interview(scratch, module, draft.interview, len(old), focus=los))
        fresh = [iq for iq in scratch.interview_questions if iq.id in created]
        if len(fresh) < len(old):
            raise PatchFailed(_no_answer("вопросы собеседования", work, [module.id]))
        for iq in old:
            match = next((f for f in fresh if set(f.lo_ids) & set(iq.lo_ids)), fresh[0])
            fresh.remove(match)
            _swap(
                work.interview_questions,
                iq.id,
                match.model_copy(update={"id": iq.id, "lo_ids": list(iq.lo_ids)}),
            )

    async def _rebuild_fa(self, work: Kit, note: str, client: LLMClient) -> None:
        values = {
            "program": prompts.describe_program(work, self.content.registry),
            "form": prompts.describe_fa(work) + (f"\n\n{note}" if note else ""),
            "outcomes": prompts.describe_outcomes(work),
            "modules": prompts.describe_modules(work),
        }
        skill = load_skill("final_attestation")
        draft = await client.complete_json(
            "final_attestation",
            skill.model,
            skill.messages(**values),
            FinalAttestationDraft,
            max_tokens=skill.max_tokens,
            temperature=skill.temperature,
            fixture_key=f"{ContentGenerator._key(work)}-patch",
        )
        apply.apply_final_attestation(work, draft, self.content.limits.fa_pass_share)  # type: ignore[arg-type]

    # ---- после пересборки (код) ------------------------------------------------------------------

    def _finish(self, work: Kit, before: Kit, aff: Affected) -> None:
        old_calendar = before.calendar.model_dump() if before.calendar else None
        if (work.calendar.model_dump() if work.calendar else None) != old_calendar:
            for lecture in work.lectures:  # номер занятия лекции — по новому календарю
                topic = next((t for t in work.topics if t.id in lecture.topic_ids[:1]), None)
                session = apply.session_of(work, topic) if topic is not None else None
                if topic is not None and session != lecture.session_no:
                    lecture.session_no = session
        retired = set(aff.retire)
        for source in work.sources:
            if set(source.topic_ids) & retired:
                source.topic_ids = [t for t in source.topic_ids if t not in retired]
        # коды и номера актов в новых текстах модели — «[не подтверждено]», если их нет в реестре
        numbers = display_numbers(work)
        fresh = set(diff_ids(before, work)[0])
        mini = Kit(
            program=work.program,
            topics=[t for t in work.topics if t.id in fresh],
            lectures=[x for x in work.lectures if x.id in fresh],
            seminars=[x for x in work.seminars if x.id in fresh],
            questions=[x for x in work.questions if x.id in fresh],
            interview_questions=[x for x in work.interview_questions if x.id in fresh],
            final_attestation=work.final_attestation if "FA" in fresh else None,
        )
        registry = self.content.registry
        mark_model_texts(mini, lambda text: mark_unknown_codes(strip_service_ids(text, numbers), registry))


# --------------------------------------------------------------------------------------------------
# Помощники
# --------------------------------------------------------------------------------------------------


async def _none() -> None:
    return None


def _topic(kit: Kit, topic_id: str) -> Topic:
    return next(t for t in kit.topics if t.id == topic_id)


def _half(value: float) -> float:
    return math.floor(value * 2 + 0.5) / 2


def _patch_key(kit: Kit, module: Module) -> str:
    """Имя заготовки ответа в режиме mock (в live ключ кеша — весь текст запроса, он и так другой)."""
    return f"{ContentGenerator._key(kit)}-{module.id}-patch"


def _wanted_row(current: HoursRow, asks: Sequence[HoursAsk]) -> dict[str, float]:
    """Строка часов, которую просит методист: названные виды работы — как сказано или ± к прежним; остальные — как
    были; часы всей темы («на 6 часов») — по прежним долям видов работы (шаг 0,5 ч, остаток — самому большому виду)."""
    row = {k.value: current.get(k) for k in KINDS}
    for ask in asks:
        if ask.kind is not None:
            before = row[ask.kind.value]
            row[ask.kind.value] = max(0.0, before + ask.sign * ask.value) if ask.sign else ask.value
            continue
        now = sum(row.values())
        total = max(0.0, now + ask.sign * ask.value) if ask.sign else ask.value
        shares = {k: v / now for k, v in row.items()} if now else {"srs": 1.0}
        row = {k.value: _half(total * shares.get(k.value, 0.0)) for k in KINDS}
        biggest = max(shares, key=lambda k: shares[k])
        row[biggest] = round(row[biggest] + total - sum(row.values()), 1)
    contact = row["lk"] + row["pr"] + row["lb"]
    row["distance"] = min(current.distance, contact)
    return row


def _row_text(row: HoursRow) -> str:
    parts = [f"{KIND_LABELS[k]} {fmt_hours(row.get(k))}" for k in KINDS if row.get(k) > 0]
    return f"{', '.join(parts) or '0'} (всего {fmt_hours(row.total)} ч)"


def _conflict_text(exc: EditConflict) -> str:
    details = exc.details if isinstance(exc.details, list) else []
    options = [str(o).strip().rstrip(".") for d in details for o in d.get("options", [])]
    text = exc.message.rstrip(".") + "."
    if options:
        text += " Варианты: " + "; ".join(o[:1].lower() + o[1:] for o in options[:3]) + "."
    return text + " Комплект не изменён."


def _replace_kit(work: Kit, new: Kit) -> None:
    for name in type(work).model_fields:
        setattr(work, name, getattr(new, name))


def _scratch(work: Kit, freed: Iterable[str]) -> Kit:
    """Копия для применения черновиков: ID пересобираемых объектов заняты (новые объекты их не получат)."""
    scratch = work.model_copy(deep=True)
    scratch.retired_ids = list(dict.fromkeys([*scratch.retired_ids, *freed]))
    return scratch


def _swap(items: list, obj_id: str, replacement: object) -> None:
    index = next(i for i, item in enumerate(items) if item.id == obj_id)
    items[index] = replacement


def _merged_lecture(parts: list[Lecture]) -> Lecture:
    """Модель могла дать по лекции на каждую тему прежней общей лекции — план и глоссарий объединяются в одну."""
    unique = list({id(p): p for p in parts}.values())
    fresh = unique[0].model_copy(deep=True)
    for extra in unique[1:]:
        fresh.plan = [*fresh.plan, *extra.plan][: apply.MAX_PLAN]
        terms = {g.term.lower() for g in fresh.glossary}
        fresh.glossary = [*fresh.glossary, *(g for g in extra.glossary if g.term.lower() not in terms)][
            : apply.MAX_GLOSSARY
        ]
    return fresh


def _renamed_seminar(seminar: Seminar, new_id: str) -> Seminar:
    criteria = [
        c.model_copy(update={"id": f"{new_id}.K{n}"}) for n, c in enumerate(seminar.criteria, start=1)
    ]
    return seminar.model_copy(update={"id": new_id, "criteria": criteria})


def _take_question(fresh: list[TestQuestion], old: TestQuestion) -> TestQuestion | None:
    """Новый вопрос на место прежнего: тот же главный РО, иначе общий РО, иначе любой оставшийся."""
    rules: list[Callable[[TestQuestion], bool]] = [
        lambda q: q.lo_ids[:1] == old.lo_ids[:1],
        lambda q: bool(set(q.lo_ids) & set(old.lo_ids)),
        lambda q: True,
    ]
    for rule in rules:
        match = next((q for q in fresh if rule(q)), None)
        if match is not None:
            fresh.remove(match)
            return match
    return None


def _no_answer(what: str, kit: Kit, ids: Sequence[str]) -> str:
    labels = ", ".join(display_label(kit, x).lower() for x in ids)
    return f"Модель не вернула {what} ({labels}) — правка не применена, комплект прежний. Повторите правку позже."


def _note(plan: PatchPlan, instruction: str) -> str:
    """Указание для навыков генерации. Не пустое никогда: иначе повторный запрос совпал бы с исходным и вернулся
    бы из кеша прежним."""
    parts = [
        "Это точечная правка готового комплекта: напиши эти материалы заново, не повторяя прежние формулировки."
    ]
    if instruction.strip():
        parts.append(f"Правка методиста — выполни её: «{instruction.strip()}».")
    if plan.focus and plan.focus not in instruction:
        parts.append(f"Что учесть: {plan.focus}.")
    if plan.operation == PatchOperation.REPLACE_TOPIC and plan.title:
        parts.append(f"Тема теперь называется «{plan.title}» — материалы пиши под новое название.")
    return " ".join(parts)


def _retire(work: Kit, aff: Affected) -> None:
    """Удаление объектов: ID — в retired_ids (не переиспользуются), вопросы — из тестов."""
    retired = set(aff.retire)
    if not retired:
        return
    work.lectures = [x for x in work.lectures if x.id not in retired]
    work.seminars = [x for x in work.seminars if x.id not in retired]
    work.questions = [x for x in work.questions if x.id not in retired]
    work.interview_questions = [x for x in work.interview_questions if x.id not in retired]
    work.topics = [x for x in work.topics if x.id not in retired]
    for test in work.tests:
        if set(test.question_ids) & retired:
            test.question_ids = [q for q in test.question_ids if q not in retired]
    work.retired_ids = list(dict.fromkeys([*work.retired_ids, *aff.retire]))


def object_hashes(kit: Kit) -> dict[str, str]:
    """ID → JSON объекта (сравнение «до» и «после» правки)."""
    return {obj_id: obj.model_dump_json() for obj_id, obj in kit.index().items()}


def diff_ids(before: Kit, after: Kit) -> tuple[list[str], list[str]]:
    """(изменённые и новые ID, удалённые ID) — в порядке комплекта."""
    old, new = object_hashes(before), object_hashes(after)
    changed = [obj_id for obj_id, value in new.items() if old.get(obj_id) != value]
    removed = [obj_id for obj_id in old if obj_id not in new]
    return changed, removed


def _take_cost(client: LLMClient) -> StepCost:
    total = StepCost(step=PATCH_STEP)
    for item in client.cost_by_step():
        total.calls += item.calls
        total.prompt_tokens += item.prompt_tokens
        total.completion_tokens += item.completion_tokens
        total.cost_rub = round(total.cost_rub + item.cost_rub, 6)
        total.seconds = round(total.seconds + item.seconds, 3)
    client.records.clear()
    return total


def _add_cost(kit: Kit, cost: StepCost) -> None:
    if not cost.calls:
        return
    known = next((c for c in kit.meta.cost if c.step == PATCH_STEP), None)
    if known is None:
        kit.meta.cost.append(cost)
        return
    known.calls += cost.calls
    known.prompt_tokens += cost.prompt_tokens
    known.completion_tokens += cost.completion_tokens
    known.cost_rub = round(known.cost_rub + cost.cost_rub, 6)
    known.seconds = round(known.seconds + cost.seconds, 3)


KIND_WORDS = {
    "T": ("тема", "темы", "тем"),
    "L": ("лекция", "лекции", "лекций"),
    "S": ("задание", "задания", "заданий"),
    "Q": ("тестовый вопрос", "тестовых вопроса", "тестовых вопросов"),
    "IQ": ("вопрос собеседования", "вопроса собеседования", "вопросов собеседования"),
    "LO": ("результат обучения", "результата обучения", "результатов обучения"),
    "TS": ("тест", "теста", "тестов"),
    "SRC": ("источник", "источника", "источников"),
    "M": ("модуль", "модуля", "модулей"),
    "C": ("компетенция", "компетенции", "компетенций"),
}
KIND_ORDER = ("T", "L", "S", "Q", "IQ", "LO", "TS", "SRC", "M", "C")


def _plural(n: int, forms: tuple[str, str, str]) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def count_text(ids: Sequence[str]) -> str:
    """«1 тема, 1 лекция, 3 тестовых вопроса» — без служебных ID."""
    counts = Counter(kind_of(x) for x in ids)
    parts = [f"{counts[k]} {_plural(counts[k], KIND_WORDS[k])}" for k in KIND_ORDER if counts.get(k)]
    if counts.get("FA"):
        parts.append("итоговая аттестация")
    return ", ".join(parts)


def _errors(kit: Kit) -> list[str]:
    report = kit.report
    return [
        f"{i.check}: {i.message}" for i in (report.issues if report else []) if i.severity == Severity.ERROR
    ]


def _reject_new_errors(before: Kit, after: Kit) -> None:
    """Правка, после которой автопроверка нашла новые ошибки, не применяется: комплект остаётся прежним."""
    known = set(_errors(before))
    new = [e.split(": ", 1)[1] for e in _errors(after) if e not in known]
    if new:
        listed = "; ".join(x.rstrip(".") for x in new[:3]) + (
            f" и ещё {len(new) - 3}" if len(new) > 3 else ""
        )
        raise PatchFailed(
            f"Правка не применена: после неё автопроверка нашла ошибки — {listed}. Комплект прежний."
        )


def _summary(head: str, changed: list[str], removed: list[str]) -> str:
    """Итог правки для методиста — без служебных ID."""
    text = head or "Правка выполнена"
    if changed:
        text += f". Изменено: {count_text(changed)}"
    if removed:
        text += f". Удалено: {count_text(removed)}"
    if not changed and not removed:
        text += ". Материалы получились такими же, как были, — изменений нет"
    return text + "."
