"""Выдача ID кодом и резолвер отображаемых номеров (docs/ARCHITECTURE.md §1.1–§1.2).

- ID выдаёт только код; LLM ссылается на переданные ей ID.
- ID не переиспользуются: учитываются и живые объекты, и Kit.retired_ids.
- Отображаемый номер («Тема 2.3») вычисляется из порядка (order), а не из ID.
  Вводный модуль ДОП (ModuleKind.INTRO) получает номер 0 — темы «0.1», «0.2».
- resolve_ref() принимает ID («T2.3») или фразу методиста («тема 2.3», «модуль 2», «итоговый тест»).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.models import Kit, ModuleKind, SourceSection, TestScope

# Префиксы ID и их форма: (префикс, число частей номера)
ID_RE = re.compile(r"^(LO|TS|IQ|SRC|C|M|T|L|S|Q|D)(\d+)(?:\.(\d+))?$")
CRITERION_RE = re.compile(r"^(S\d+|FA)\.K(\d+)$")
PORTFOLIO_RE = re.compile(r"^FA\.P(\d+)$")
ISSUE_RE = re.compile(r"^(V\d{2})-(\d+)$")


class UnknownReference(ValueError):
    """Ссылка не найдена в комплекте."""


class AmbiguousReference(ValueError):
    """Ссылка подходит к нескольким объектам."""


def parse_id(obj_id: str) -> tuple[str, tuple[int, ...]]:
    """«T2.3» → ("T", (2, 3)); «FA» → ("FA", ())."""
    if obj_id == "FA":
        return "FA", ()
    match = ID_RE.match(obj_id)
    if not match:
        raise ValueError(f"Некорректный ID: {obj_id!r}")
    prefix, first, second = match.groups()
    numbers = (int(first),) if second is None else (int(first), int(second))
    return prefix, numbers


def id_number(obj_id: str) -> int:
    """Первое число ID: «M3» → 3, «C2» → 2, «T2.3» → 2."""
    return parse_id(obj_id)[1][0]


# --------------------------------------------------------------------------------------------------
# Выдача ID
# --------------------------------------------------------------------------------------------------


class IdAllocator:
    """Выдаёт следующие свободные ID с учётом уже занятых и удалённых (retired)."""

    def __init__(self, kit: Kit | None = None, extra_used: Iterable[str] = ()) -> None:
        self._used: set[str] = set(extra_used)
        if kit is not None:
            self._used.update(collect_used_ids(kit))

    def reserve(self, obj_id: str) -> str:
        self._used.add(obj_id)
        return obj_id

    def is_used(self, obj_id: str) -> bool:
        return obj_id in self._used

    def _next_flat(self, prefix: str) -> str:
        taken = [parse_id(x)[1][0] for x in self._used if _has_prefix(x, prefix, parts=1)]
        return self.reserve(f"{prefix}{max(taken, default=0) + 1}")

    def _next_nested(self, prefix: str, parent_number: int) -> str:
        taken = [
            parse_id(x)[1][1]
            for x in self._used
            if _has_prefix(x, prefix, parts=2) and parse_id(x)[1][0] == parent_number
        ]
        return self.reserve(f"{prefix}{parent_number}.{max(taken, default=0) + 1}")

    def competency(self) -> str:
        return self._next_flat("C")

    def outcome(self, competency_id: str) -> str:
        return self._next_nested("LO", id_number(competency_id))

    def module(self) -> str:
        return self._next_flat("M")

    def topic(self, module_id: str) -> str:
        return self._next_nested("T", id_number(module_id))

    def lecture(self, module_id: str) -> str:
        return self._next_nested("L", id_number(module_id))

    def seminar(self) -> str:
        return self._next_flat("S")

    def test(self) -> str:
        return self._next_flat("TS")

    def question(self) -> str:
        return self._next_flat("Q")

    def interview(self) -> str:
        return self._next_flat("IQ")

    def source(self) -> str:
        return self._next_flat("SRC")

    def criterion(self, owner_id: str) -> str:
        """Критерий владельца: «S4» → «S4.K1», «FA» → «FA.K1»."""
        taken = [
            int(m.group(2))
            for x in self._used
            if (m := CRITERION_RE.match(x)) is not None and m.group(1) == owner_id
        ]
        return self.reserve(f"{owner_id}.K{max(taken, default=0) + 1}")

    def portfolio_item(self) -> str:
        taken = [int(m.group(1)) for x in self._used if (m := PORTFOLIO_RE.match(x)) is not None]
        return self.reserve(f"FA.P{max(taken, default=0) + 1}")


def day_id(n: int) -> str:
    """Дни календаря перенумеровываются при каждой сборке календаря — это не стабильные объекты."""
    return f"D{n}"


def issue_id(check: str, n: int) -> str:
    """Замечание проверки: issue_id("V02", 1) → «V02-1» (нумерация внутри одного отчёта)."""
    return f"{check}-{n}"


def _has_prefix(obj_id: str, prefix: str, parts: int) -> bool:
    match = ID_RE.match(obj_id)
    if not match or match.group(1) != prefix:
        return False
    return (match.group(3) is not None) == (parts == 2)


def collect_used_ids(kit: Kit) -> set[str]:
    """Все занятые ID: объекты, критерии, пункты портфолио, удалённые ID."""
    used: set[str] = set(kit.index().keys()) | set(kit.retired_ids)
    for seminar in kit.seminars:
        used.update(c.id for c in seminar.criteria)
    if kit.final_attestation is not None:
        used.update(c.id for c in kit.final_attestation.criteria)
        used.update(p.id for p in kit.final_attestation.portfolio_items)
    return used


# --------------------------------------------------------------------------------------------------
# Отображаемые номера
# --------------------------------------------------------------------------------------------------

SECTION_ORDER = [
    SourceSection.NORMATIVE,
    SourceSection.MAIN,
    SourceSection.ADDITIONAL,
    SourceSection.INTERNET,
    SourceSection.SOFTWARE,
]


def module_numbers(kit: Kit) -> dict[str, int]:
    """Номер модуля в документе: вводный — 0, остальные — 1, 2, ... по order."""
    result: dict[str, int] = {}
    counter = 0
    for module in kit.modules_sorted():
        if module.kind == ModuleKind.INTRO:
            result[module.id] = 0
        else:
            counter += 1
            result[module.id] = counter
    return result


def display_numbers(kit: Kit) -> dict[str, str]:
    """ID → номер для документа («T2.7» → «2.3», если тема третья в модуле 2)."""
    numbers: dict[str, str] = {}
    mod_num = module_numbers(kit)
    topic_rank: dict[str, tuple[int, int]] = {}

    for module in kit.modules_sorted():
        m = mod_num[module.id]
        numbers[module.id] = str(m)
        for i, topic in enumerate(kit.topics_of(module.id), start=1):
            numbers[topic.id] = f"{m}.{i}"
            topic_rank[topic.id] = (module.order, topic.order)

        lectures = [lec for lec in kit.lectures if lec.module_id == module.id]
        lectures.sort(key=lambda lec: (_first_topic_rank(lec.topic_ids, topic_rank), id_number_safe(lec.id)))
        for i, lecture in enumerate(lectures, start=1):
            numbers[lecture.id] = f"{m}.{i}"

        interviews = [q for q in kit.interview_questions if q.module_id == module.id]
        for i, question in enumerate(interviews, start=1):
            numbers[question.id] = f"{m}.{i}"

    seminars = sorted(kit.seminars, key=lambda s: (topic_rank.get(s.topic_id, (10**6, 10**6)), s.id))
    for i, seminar in enumerate(seminars, start=1):
        numbers[seminar.id] = str(i)

    competencies = sorted(kit.competencies, key=lambda c: id_number(c.id))
    for ci, competency in enumerate(competencies, start=1):
        numbers[competency.id] = str(ci)
        for oi, outcome in enumerate(kit.outcomes_of(competency.id), start=1):
            numbers[outcome.id] = f"{ci}.{oi}"

    for ti, test in enumerate(kit.tests, start=1):
        numbers[test.id] = str(ti)
        ordered = list(test.question_ids)
        ordered += [q.id for q in kit.questions if q.test_id == test.id and q.id not in ordered]
        for qi, question_id in enumerate(ordered, start=1):
            numbers[question_id] = str(qi)

    for section in SECTION_ORDER:
        for si, source in enumerate((s for s in kit.sources if s.section == section), start=1):
            numbers[source.id] = str(si)

    if kit.final_attestation is not None:
        numbers["FA"] = ""
    return numbers


def display_label(kit: Kit, obj_id: str) -> str:
    """Подпись для методиста: «Тема 2.3», «Модуль 1», «Итоговая аттестация»."""
    if obj_id == "FA":
        return "Итоговая аттестация"
    number = display_numbers(kit).get(obj_id, "")
    prefix = parse_id(obj_id)[0]
    titles = {
        "C": "Компетенция",
        "LO": "Результат обучения",
        "M": "Модуль",
        "T": "Тема",
        "L": "Лекция",
        "S": "Задание",
        "TS": "Тест",
        "Q": "Вопрос",
        "IQ": "Вопрос собеседования",
        "SRC": "Источник",
        "D": "День",
    }
    return f"{titles.get(prefix, prefix)} {number}".strip()


def _first_topic_rank(topic_ids: list[str], rank: dict[str, tuple[int, int]]) -> tuple[int, int]:
    ranks = [rank[t] for t in topic_ids if t in rank]
    return min(ranks) if ranks else (10**6, 10**6)


def id_number_safe(obj_id: str) -> tuple[int, ...]:
    try:
        return parse_id(obj_id)[1]
    except ValueError:
        return (10**6,)


# --------------------------------------------------------------------------------------------------
# Резолвер ссылок методиста
# --------------------------------------------------------------------------------------------------

_PHRASES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(?:вопрос\w*\s+собеседовани\w*)\s+(\d+\.\d+)$"), "IQ"),
    (re.compile(r"^(?:тем\w*)\s+(\d+\.\d+)$"), "T"),
    (re.compile(r"^(?:модул\w*|раздел\w*)\s+(\d+)$"), "M"),
    (re.compile(r"^(?:лекци\w*)\s+(\d+\.\d+)$"), "L"),
    (re.compile(r"^(?:задани\w*|семинар\w*|практическ\w*\s+(?:заняти\w*|работ\w*))\s+(\d+)$"), "S"),
    (re.compile(r"^(?:компетенци\w*)\s+(\d+)$"), "C"),
    (re.compile(r"^(?:результат\w*(?:\s+обучения)?|ро)\s+(\d+\.\d+)$"), "LO"),
    (re.compile(r"^(?:тест\w*)\s+(\d+)$"), "TS"),
    (re.compile(r"^(?:вопрос\w*)\s+(\d+)$"), "Q"),
    (re.compile(r"^(?:источник\w*)\s+(\d+)$"), "SRC"),
]
_FA_RE = re.compile(r"^(?:итогов\w*\s+аттестаци\w*|иа)$")
_FINAL_TEST_RE = re.compile(r"^(?:итогов\w*\s+тест\w*|тест\w*)$")


def _normalize(text: str) -> str:
    text = text.strip().lower().replace("ё", "е")
    text = re.sub(r"^[«»\"'“”„\s]+", "", text)
    text = re.sub(r"[«»\"'“”„.,;:!?\s]+$", "", text)
    text = text.replace("№", " ")
    return re.sub(r"\s+", " ", text).strip()


def resolve_ref(kit: Kit, text: str) -> str:
    """ID или фраза методиста → ID объекта комплекта.

    Примеры: «T2.3» → T2.3; «тема 2.3» → ID третьей темы модуля 2; «модуль 1»; «итоговый тест»; «ИА» → FA.
    Бросает UnknownReference / AmbiguousReference с понятным сообщением.
    """
    raw = text.strip()
    index = kit.index()

    candidate = raw.upper()
    if candidate in index:
        return candidate
    if ID_RE.match(candidate) or candidate == "FA":
        raise UnknownReference(f"В комплекте нет объекта {candidate}.")

    phrase = _normalize(raw)
    if _FA_RE.match(phrase):
        if kit.final_attestation is None:
            raise UnknownReference("В комплекте ещё нет итоговой аттестации.")
        return "FA"

    if _FINAL_TEST_RE.match(phrase):
        finals = [t.id for t in kit.tests if t.scope == TestScope.FINAL]
        if len(finals) == 1:
            return finals[0]
        if len(kit.tests) == 1:
            return kit.tests[0].id
        if not kit.tests:
            raise UnknownReference("В комплекте ещё нет тестов.")
        raise AmbiguousReference("Тестов несколько — укажите номер теста, например «тест 2».")

    for pattern, prefix in _PHRASES:
        match = pattern.match(phrase)
        if match:
            return _resolve_display(kit, prefix, match.group(1), raw)

    raise UnknownReference(
        f"Не понимаю ссылку «{raw}». Укажите, например, «тема 2.3», «модуль 1», «лекция 1.2», «итоговый тест» "
        "или ID объекта (T2.3)."
    )


def _resolve_display(kit: Kit, prefix: str, number: str, raw: str) -> str:
    numbers = display_numbers(kit)
    matches = [
        obj_id for obj_id, shown in numbers.items() if shown == number and _id_prefix(obj_id) == prefix
    ]
    if prefix == "Q" and len(matches) > 1:
        raise AmbiguousReference(
            f"Вопрос {number} есть в нескольких тестах — укажите ID вопроса (например, Q17) или тест."
        )
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise UnknownReference(f"В комплекте нет объекта «{raw}».")
    raise AmbiguousReference(f"Ссылка «{raw}» подходит к нескольким объектам: {', '.join(sorted(matches))}.")


def _id_prefix(obj_id: str) -> str:
    try:
        return parse_id(obj_id)[0]
    except ValueError:
        return ""
