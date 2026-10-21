"""Черновики навыков содержания → объекты комплекта (docs/ARCHITECTURE.md §1.1).

ID выдаёт код. Ссылки модели на темы и результаты обучения проверяются: неизвестный ID темы сопоставляется по порядку,
неизвестный результат — заменяется результатами своей темы. Числа часов из пунктов содержания вычищаются (часы —
только из солвера). Функции меняют комплект синхронно, без await (контракт Step).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Iterable, Sequence
from typing import TypeVar

from app.config import DEFAULT_LIMITS
from app.core.ids import IdAllocator
from app.core.validate import FREE_HOURS_RE, HOURS_PATTERN
from app.models import (
    Criterion,
    CriterionDraft,
    FAForm,
    FinalAttestation,
    FinalAttestationDraft,
    GlossaryItem,
    InterviewDraft,
    InterviewQuestion,
    Kit,
    Lecture,
    LectureDraft,
    Module,
    PlanItem,
    PortfolioItem,
    ProgramTextsDraft,
    QuestionDraft,
    QuestionKind,
    Seminar,
    SeminarDraft,
    SeminarKind,
    Test,
    TestQuestion,
    Topic,
    TopicContentDraft,
    WorkKind,
)
from app.pipeline.content_prompts import text_fields, work_kinds

D = TypeVar("D")

MAX_ITEMS = 6
MAX_PLAN = 7
MAX_THESES = 5
MAX_GLOSSARY = 8
MAX_STEPS = 8
MAX_CRITERIA = 6
SEMINAR_PASS_SHARE = DEFAULT_LIMITS.seminar_pass_share  # по умолчанию; генератор передаёт Settings.limits
FA_PASS_SHARE = DEFAULT_LIMITS.fa_pass_share
ANON_SENTENCE = (
    "Используются только обезличенные данные: без ФИО, телефонов, адресов, номеров документов, карт и счетов."
)

# Часы — только из солвера (жёсткое правило 2): упоминания часов из ответа модели в документ не попадают.
# Шаблон общий с проверкой V05, чтобы очистка и проверка не расходились.
PAREN_HOURS_RE = re.compile(
    r"\s*[(\[]\s*" + HOURS_PATTERN + r"\s*[)\]]", re.IGNORECASE
)  # «ПР (4 ч)» — метку ставит рендер
HOURS_MENTION_RE = FREE_HOURS_RE
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZА-ЯЁ0-9«\"(])")  # «ак. ч. аудиторной» — не конец предложения
TITLE_ID_RE = re.compile(r"^(?:[TТ]\d+\.\d+|тема\s+\d+(?:\.\d+)?)\s*[—–:.-]?\s*", re.IGNORECASE)
BULLET_RE = re.compile(r"^\s*(?:[-–—•*·]|\d+[.)]|[a-zа-я][.)])\s+", re.IGNORECASE)
ID_RE = re.compile(r"[A-Z]{1,2}\d+(?:\.\d+)?")
CYR_TO_LAT = str.maketrans("ТОСМ", "TOCM")  # кириллица, похожая на латиницу ID: «Т1.2», «М2»


# --------------------------------------------------------------------------------------------------
# Очистка текста
# --------------------------------------------------------------------------------------------------


# «ltrace, ltrace, ltrace» — сбой генерации; только слова из букв от 3 символов: «[0, 0, 0]», «print(1, 1, 1)» — код
REPEAT_RE = re.compile(r"(?<![\w\[(])([^\W\d_]{3,})(?:\s*,\s*\1\b){2,}", re.I)


def clean(text: str | None) -> str:
    value = re.sub(r"\s+", " ", (text or "").strip())
    return REPEAT_RE.sub(r"\1", value)


def has_hours(text: str) -> bool:
    return bool(HOURS_MENTION_RE.search(text))


def clean_item(text: str | None) -> str:
    """Пункт списка: без маркера; метка «(4 ч)» вырезается, пункт с другим упоминанием часов выбрасывается."""
    value = PAREN_HOURS_RE.sub("", BULLET_RE.sub("", clean(text))).strip(" ,;:")
    return "" if has_hours(value) else clean(value)


def clean_prose(text: str | None) -> str:
    """Связный текст модели: предложения с числом часов выбрасываются (часы знает только солвер).
    Часы ищутся во всём тексте до деления на предложения: «72 ак. ч.» деление разорвало бы на два куска."""
    value = PAREN_HOURS_RE.sub("", clean(text))
    spans = [m.span() for m in HOURS_MENTION_RE.finditer(value)]
    if not spans:
        return value
    bounds: list[tuple[int, int]] = []
    start = 0
    for brk in SENTENCE_RE.finditer(value):
        bounds.append((start, brk.start()))
        start = brk.end()
    bounds.append((start, len(value)))
    kept = [
        value[a:b] for a, b in bounds if value[a:b].strip() and not any(s < b and e > a for s, e in spans)
    ]
    return clean(" ".join(kept))


def clean_items(items: Iterable[str], limit: int = MAX_ITEMS) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = clean_item(item)
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result[:limit]


def round_half(value: float) -> float:
    return math.floor(value * 2 + 0.5) / 2


def norm_id(value: str | None) -> str:
    """«t1.2», «Т1.2» (кириллица), « LO1.1 » → «T1.2», «LO1.1»."""
    text = clean(value).upper().translate(CYR_TO_LAT)
    found = ID_RE.findall(text)
    return found[-1] if found else text  # «M4.T4.1» — модель приписала модуль к ID темы


# --------------------------------------------------------------------------------------------------
# Сопоставление ответов модели с объектами
# --------------------------------------------------------------------------------------------------


def match_to_topics(
    drafts: Sequence[D],
    topics: Sequence[Topic],
    key: Callable[[D], Iterable[str]],
    one_per_topic: bool = True,
) -> list[tuple[Topic, D]]:
    """Черновик → тема: по ID, который назвала модель; черновики с неизвестным ID — свободным темам по порядку,
    повтор уже занятой темы отбрасывается."""
    by_id = {t.id: t for t in topics}
    taken: set[str] = set()
    pairs: list[tuple[Topic, D]] = []
    rest: list[D] = []
    for draft in drafts:
        ids = [norm_id(x) for x in key(draft)]
        known = [i for i in ids if i in by_id]
        topic = next((by_id[i] for i in known if not one_per_topic or i not in taken), None)
        if topic is None:
            if not known:
                rest.append(draft)  # ID не узнан — свободной теме по порядку; занятую тему не переназначаем
            continue
        taken.add(topic.id)
        pairs.append((topic, draft))
    free = [t for t in topics if t.id not in taken]
    for topic, draft in zip(free, rest, strict=False):
        pairs.append((topic, draft))
    order = {t.id: i for i, t in enumerate(topics)}
    return sorted(pairs, key=lambda pair: order[pair[0].id])


def valid_los(ids: Iterable[str], allowed: Sequence[str]) -> list[str]:
    allowed_set = set(allowed)
    return [x for x in dict.fromkeys(norm_id(i) for i in ids) if x in allowed_set]


# --------------------------------------------------------------------------------------------------
# Тексты программы
# --------------------------------------------------------------------------------------------------


def apply_program_texts(kit: Kit, draft: ProgramTextsDraft) -> None:
    for name in text_fields(kit):
        value = getattr(draft, name)
        if isinstance(value, list):
            items = clean_items(value, limit=10)
            if items:
                setattr(kit.texts, name, items)
        elif clean_prose(value):
            setattr(kit.texts, name, clean_prose(value))


# --------------------------------------------------------------------------------------------------
# Содержание тем (рабочая программа)
# --------------------------------------------------------------------------------------------------


def apply_module_content(
    kit: Kit, module: Module, drafts: Sequence[TopicContentDraft], only: Sequence[str] | None = None
) -> list[str]:
    """Пункты рабочей программы тем модуля (only — при точечной правке: только эти темы)."""
    changed: list[str] = []
    topics = [t for t in kit.topics_of(module.id) if only is None or t.id in set(only)]
    for topic, draft in match_to_topics(drafts, topics, lambda d: [d.topic_id]):
        content: dict[WorkKind, list[str]] = {}
        for kind in work_kinds(topic):
            items = clean_items(getattr(draft, kind.value))
            if items:
                content[kind] = items
        if content:
            topic.content = content
            changed.append(topic.id)
        if not clean(topic.control_form) and clean(draft.control_form):
            topic.control_form = clean(draft.control_form)
    return changed


# --------------------------------------------------------------------------------------------------
# Лекции
# --------------------------------------------------------------------------------------------------


def lecture_topics(kit: Kit, module: Module) -> list[Topic]:
    return [t for t in kit.topics_of(module.id) if t.hours.lk > 0]


def session_of(kit: Kit, topic: Topic) -> int | None:
    """Номер первого занятия (дня) календаря, где у темы есть лекционные часы (иначе — любые часы темы)."""
    if kit.calendar is None:
        return None
    days = sorted(kit.calendar.days, key=lambda d: d.n)
    for want_lk in (True, False):
        for day in days:
            if any(i.topic_id == topic.id and (not want_lk or i.kind == WorkKind.LK) for i in day.items):
                return day.n
    return None


def apply_lectures(
    kit: Kit, module: Module, drafts: Sequence[LectureDraft], only: Sequence[str] | None = None
) -> list[str]:
    """Лекции к темам с часами теории (only — при доборе: только темы без лекции)."""
    topics = [t for t in lecture_topics(kit, module) if only is None or t.id in set(only)]
    # пункт плана может раскрывать только РО своего модуля: ссылка на чужой РО завысила бы покрытие
    module_los = list(dict.fromkeys(lo for t in kit.topics_of(module.id) for lo in t.lo_ids))
    alloc = IdAllocator(kit)
    created: list[str] = []
    pairs = match_to_topics(drafts, topics, lambda d: d.topic_ids)
    # модель иногда объединяет несколько тем в одну лекцию (topic_ids: [T2.1, T2.2, T2.3]) — лекция покрывает все
    # свои темы, которые не заняты другой лекцией, а не только первую (иначе у остальных нет лекции, V20)
    wanted = {t.id for t in topics}
    claimed = {topic.id for topic, _ in pairs} | {tid for lec in kit.lectures for tid in lec.topic_ids}
    for topic, draft in pairs:
        extra = [
            tid
            for tid in dict.fromkeys(norm_id(x) for x in draft.topic_ids)
            if tid in wanted and tid not in claimed
        ]
        claimed |= set(extra)
        plan: list[PlanItem] = []
        for item in draft.plan[:MAX_PLAN]:
            title = clean_item(item.title)
            if not title:
                continue
            los = valid_los(item.lo_ids, module_los) or list(topic.lo_ids)
            plan.append(PlanItem(title=title, theses=clean_items(item.theses, MAX_THESES), lo_ids=los))
        glossary: list[GlossaryItem] = []
        seen: set[str] = set()
        for entry in draft.glossary:
            term, definition = clean(entry.term), clean_prose(entry.definition)
            if term and definition and term.lower() not in seen:
                seen.add(term.lower())
                glossary.append(GlossaryItem(term=term, definition=definition))
        lecture = Lecture(
            id=alloc.lecture(module.id),
            module_id=module.id,
            topic_ids=[topic.id, *extra],
            session_no=session_of(kit, topic),
            title=topic.title,  # заголовок лекции = тема учебного плана (проверка V20)
            goal=clean_prose(draft.goal),
            plan=plan,
            glossary=glossary[:MAX_GLOSSARY],
        )
        kit.lectures.append(lecture)
        created.append(lecture.id)
    return created


# --------------------------------------------------------------------------------------------------
# Задания семинаров
# --------------------------------------------------------------------------------------------------


def practice_topics(kit: Kit, module: Module) -> list[Topic]:
    return [t for t in kit.topics_of(module.id) if t.hours.pr + t.hours.lb > 0]


def _criteria(owner_id: str, drafts: Iterable[CriterionDraft], alloc: IdAllocator) -> list[Criterion]:
    result: list[Criterion] = []
    for draft in drafts:
        text = clean_item(draft.text)
        points = round_half(draft.max_points)
        if text and points > 0 and len(result) < MAX_CRITERIA:
            result.append(Criterion(id=alloc.criterion(owner_id), text=text, max_points=points))
    return result


def _threshold(wanted: float | None, total: float, share: float) -> float | None:
    if total <= 0:
        return None
    if wanted is not None and 0 < wanted <= total:
        return round_half(wanted)
    return round_half(math.ceil(total * share * 2) / 2)


def apply_seminars(
    kit: Kit,
    module: Module,
    drafts: Sequence[SeminarDraft],
    only: Sequence[str] | None = None,
    pass_share: float = SEMINAR_PASS_SHARE,
) -> list[str]:
    """Задания к темам с практикой модуля (only — при доборе: только темы без задания)."""
    alloc = IdAllocator(kit)
    created: list[str] = []
    topics = [t for t in practice_topics(kit, module) if only is None or t.id in set(only)]
    for topic, draft in match_to_topics(drafts, topics, lambda d: [d.topic_id]):
        seminar_id = alloc.seminar()
        criteria = _criteria(seminar_id, draft.criteria, alloc)
        total = sum(c.max_points for c in criteria)
        kind = SeminarKind.LAB if topic.hours.lb > topic.hours.pr else SeminarKind.PRACTICE
        seminar = Seminar(
            id=seminar_id,
            topic_id=topic.id,
            lo_ids=valid_los(draft.lo_ids, topic.lo_ids) or list(topic.lo_ids),
            kind=kind,
            title=TITLE_ID_RE.sub("", clean_item(draft.title)).strip() or topic.title,
            situation=clean_prose(draft.situation),
            task=clean_prose(draft.task),
            steps=clean_items(draft.steps, MAX_STEPS),
            deliverable=clean_prose(draft.deliverable),
            criteria=criteria,
            pass_threshold=_threshold(draft.pass_threshold, total, pass_share),
        )
        kit.seminars.append(seminar)
        created.append(seminar.id)
    return created


# --------------------------------------------------------------------------------------------------
# Тесты и собеседование
# --------------------------------------------------------------------------------------------------


def answer_indices(options: Sequence[str], answers: Iterable[str]) -> list[int]:
    """Правильные ответы модели (текстом) → номера вариантов; ответ, которого нет среди вариантов, не засчитывается."""
    keys = [_answer_key(o) for o in options]
    result: set[int] = set()
    for answer in answers:
        key = _answer_key(answer)
        if key in keys:
            result.add(keys.index(key))
            continue
        # «Б) текст» / «текст.» — модель повторила вариант с буквой или точкой
        found = [
            i
            for i, k in enumerate(keys)
            if k and key and (k in key or key in k) and min(len(k), len(key)) >= 3
        ]
        if len(found) == 1:
            result.add(found[0])
    return sorted(result)


def _answer_key(text: str) -> str:
    value = re.sub(r"^\s*[a-zа-яё1-9][).]\s+", "", clean(text), flags=re.IGNORECASE)
    return re.sub(r"[\s.;:!?«»\"']+", " ", value).strip().lower()


def shuffle_options(stem: str, options: list[str], correct: list[int]) -> tuple[list[str], list[int]]:
    """Детерминированный сдвиг вариантов: модели почти всегда ставят верный ответ первым."""
    if len(options) < 2:
        return options, correct
    shift = int(hashlib.sha256(stem.encode("utf-8")).hexdigest(), 16) % len(options)
    order = options[shift:] + options[:shift]
    mapping = {old: (old - shift) % len(options) for old in range(len(options))}
    return order, sorted(mapping[i] for i in correct)


def _question(
    draft: QuestionDraft, qid: str, test_id: str, topic_id: str, lo_ids: list[str]
) -> TestQuestion | None:
    stem = clean_prose(draft.stem)
    options: list[str] = []
    for option in draft.options:
        value = clean(option)
        if value and value.lower() not in {o.lower() for o in options}:
            options.append(value)
    if (
        not stem
        or len(options) < 2
        or len(options) != len(draft.options)
        or any(has_hours(o) for o in options)
    ):
        return None
    correct = answer_indices(options, draft.correct_answers)
    if not correct or len(correct) == len(options):
        return None
    options, correct = shuffle_options(stem, options, correct)
    kind = QuestionKind.MULTIPLE if len(correct) > 1 else QuestionKind.SINGLE
    return TestQuestion(
        id=qid,
        test_id=test_id,
        topic_id=topic_id,
        lo_ids=lo_ids,
        kind=kind,
        stem=stem,
        options=options,
        correct=correct,
        explanation=clean_prose(draft.explanation),
        difficulty=min(3, max(1, draft.difficulty)),
    )


def apply_questions(
    kit: Kit, module: Module, test: Test, drafts: Sequence[QuestionDraft], quotas: dict[str, int]
) -> list[str]:
    """Вопросы в тест: на каждый РО — не больше его квоты; вопрос без годного ключа или с часами отбрасывается."""
    topics = kit.topics_of(module.id)
    topic_ids = {t.id for t in topics}
    topic_of_lo: dict[str, str] = {}
    for topic in topics:
        for lo in topic.lo_ids:
            topic_of_lo.setdefault(lo, topic.id)
    remaining = {lo: n for lo, n in quotas.items() if n > 0}
    module_los = {lo for t in topics for lo in t.lo_ids} | set(quotas)
    stems = {_answer_key(q.stem) for q in kit.questions}  # повтор уже заданного вопроса не нужен
    alloc = IdAllocator(kit)
    created: list[str] = []
    for draft in drafts:
        if not remaining:
            break
        named = valid_los(draft.lo_ids, sorted(module_los))
        los = [lo for lo in named if lo in remaining]
        if named and not los:
            continue  # вопрос к РО, квота которого уже набрана, — лишний; на чужой РО не перевешиваем
        if not los:
            los = [next(iter(remaining))]  # ссылка не узнана — РО, которому ещё нужны вопросы
        topic_id = norm_id(draft.topic_id)
        if topic_id not in topic_ids:
            topic_id = topic_of_lo.get(los[0]) or (topics[0].id if topics else "")
        if not topic_id:
            break
        if _answer_key(draft.stem) in stems:
            continue
        question = _question(draft, alloc.question(), test.id, topic_id, los)
        if question is None:
            continue
        stems.add(_answer_key(question.stem))
        kit.questions.append(question)
        test.question_ids.append(question.id)
        created.append(question.id)
        for lo in los:
            if lo in remaining:
                remaining[lo] -= 1
                if remaining[lo] <= 0:
                    del remaining[lo]
    return created


def apply_interview(
    kit: Kit, module: Module, drafts: Sequence[InterviewDraft], limit: int, focus: Sequence[str] = ()
) -> list[str]:
    """Вопросы собеседования модуля; каждый результат модуля (или из focus — при автодоборе) закрывается хотя бы
    одним вопросом."""
    module_los = list(dict.fromkeys(lo for t in kit.topics_of(module.id) for lo in t.lo_ids))
    uncovered = [lo for lo in focus if lo in module_los] or list(module_los)
    alloc = IdAllocator(kit)
    created: list[str] = []
    for draft in drafts[:limit]:
        text = clean_prose(draft.text)
        if not text:
            continue
        los = valid_los(draft.lo_ids, module_los)
        if not los and uncovered:
            los = [uncovered[0]]
        elif not los and module_los:
            los = [module_los[len(created) % len(module_los)]]
        uncovered = [lo for lo in uncovered if lo not in los]
        question = InterviewQuestion(
            id=alloc.interview(),
            module_id=module.id,
            lo_ids=los,
            text=text,
            answer_points=clean_items(draft.answer_points),
            criteria=clean_items(draft.criteria),
        )
        kit.interview_questions.append(question)
        created.append(question.id)
    return created


# --------------------------------------------------------------------------------------------------
# Итоговая аттестация
# --------------------------------------------------------------------------------------------------


def apply_final_attestation(
    kit: Kit, draft: FinalAttestationDraft, pass_share: float = FA_PASS_SHARE
) -> None:
    """Форма и часы ИА — из каркаса (код); модель пишет требования, критерии и процедуру."""
    request = kit.request
    fa = kit.final_attestation or FinalAttestation(form=request.fa_form or FAForm.ZACHET)
    alloc = IdAllocator(kit)
    all_los = [o.id for o in kit.outcomes]
    # описание формы ИА — своими словами модели; текст из запроса (каркас) — только если модель не дала своего
    fa.form_text = clean_prose(draft.form_text) or fa.form_text
    fa.lo_ids = valid_los(draft.lo_ids, all_los) or all_los
    fa.requirements = clean_items(draft.requirements, 8)
    if fa.form == FAForm.PORTFOLIO:
        wanted = request.portfolio_items_count
        items = [p for p in draft.portfolio_items if clean(p.title)]
        if wanted:
            items = items[:wanted]
        fa.portfolio_items = [
            PortfolioItem(
                id=alloc.portfolio_item(), title=clean_item(p.title), requirements=clean_items(p.requirements)
            )
            for p in items
        ]
    task = draft.example_task
    if task is not None and (clean(task.title) or clean(task.description)):
        task.title, task.environment = clean_prose(task.title), clean_prose(task.environment)
        task.description, task.deliverable = clean_prose(task.description), clean_prose(task.deliverable)
        fa.example_task = task
    fa.data_requirements = clean_items(draft.data_requirements, 8)
    if request.constraints.anonymized_data and not any(
        re.search(r"обезличен", x, re.I) for x in fa.data_requirements
    ):
        fa.data_requirements.append(
            ANON_SENTENCE
        )  # обязательное условие запроса — код не полагается на модель
    fa.expected_result = clean_prose(draft.expected_result) or fa.expected_result
    fa.report_requirements = clean_items(draft.report_requirements, 10)
    fa.criteria = _criteria("FA", draft.criteria, alloc)
    total = sum(c.max_points for c in fa.criteria)
    fa.pass_rule = clean_prose(draft.pass_rule)
    if not fa.pass_rule and total > 0:
        fa.pass_rule = (
            f"«Зачтено» — не меньше {_fmt(_threshold(None, total, pass_share) or 0)} баллов из {_fmt(total)}."
        )
    fa.procedure = clean_items(draft.procedure, 8)
    kit.final_attestation = fa


def _fmt(value: float) -> str:
    return f"{value:g}".replace(".", ",")
