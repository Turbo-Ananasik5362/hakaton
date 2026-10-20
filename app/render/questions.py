"""Банк тестовых вопросов для выгрузок (GIFT, Moodle XML, CSV): порядок, категории, баллы вариантов."""

from __future__ import annotations

from dataclasses import dataclass

from app.models import Kit, Test, TestQuestion, TestScope
from app.render.tables import module_numbers

FINAL_CATEGORY = "Итоговый тест"
ENTRY_CATEGORY = "Входной тест"
LETTERS = "абвгдежзик"

# Доли баллов, которые принимает Moodle (список допустимых значений fraction): верные варианты делят 100 %,
# неверные при нескольких верных ответах снимают баллы, чтобы «отметить всё» не давало полного балла.
MOODLE_FRACTIONS = {
    1: "100",
    2: "50",
    3: "33.33333",
    4: "25",
    5: "20",
    6: "16.66667",
    7: "14.28571",
    8: "12.5",
    9: "11.11111",
    10: "10",
}


@dataclass(frozen=True)
class BankItem:
    category: str  # «Модуль 1. Основы Python» / «Итоговый тест»
    test: Test
    question: TestQuestion
    topic_label: str  # «Тема 1.2. …»


def category_of(kit: Kit, test: Test, numbers: dict[str, str]) -> str:
    if test.scope == TestScope.FINAL:
        return FINAL_CATEGORY
    if test.scope == TestScope.ENTRY:
        return ENTRY_CATEGORY
    module = next((m for m in kit.modules if m.id == test.module_id), None)
    if module is None:
        return test.title or test.id
    number = numbers.get(module.id, "")
    return module.title if number in ("", "0") else f"Модуль {number}. {module.title}"


def question_bank(kit: Kit) -> list[BankItem]:
    """Вопросы всех тестов: сначала тесты модулей по порядку модулей, затем входной и итоговый; вопросы — в
    порядке теста. Вопрос без вариантов или без верного ответа в выгрузку не идёт (его ловит V18/V19)."""
    numbers = module_numbers(kit)
    order = {m.id: i for i, m in enumerate(kit.modules_sorted())}
    scope_rank = {TestScope.MODULE: 0, TestScope.ENTRY: 1, TestScope.FINAL: 2}
    tests = sorted(kit.tests, key=lambda t: (scope_rank[t.scope], order.get(t.module_id or "", 99), t.id))
    questions = {q.id: q for q in kit.questions}
    topics = {t.id: t for t in kit.topics}
    items: list[BankItem] = []
    for test in tests:
        category = category_of(kit, test, numbers)
        for qid in test.question_ids:
            question = questions.get(qid)
            if question is None or len(question.options) < 2 or not question.correct:
                continue
            if any(i < 0 or i >= len(question.options) for i in question.correct):
                continue
            topic = topics.get(question.topic_id)
            label = ""
            if topic is not None:
                siblings = kit.topics_of(topic.module_id)
                label = f"Тема {numbers.get(topic.module_id, '')}.{siblings.index(topic) + 1}. {topic.title}"
            items.append(BankItem(category=category, test=test, question=question, topic_label=label))
    return items


def correct_fraction(question: TestQuestion) -> str:
    return MOODLE_FRACTIONS.get(len(question.correct), f"{100 / len(question.correct):.5f}")


def wrong_fraction(question: TestQuestion) -> str:
    """Штраф за неверный вариант в вопросе с несколькими ответами; при одном верном ответе — 0."""
    if len(question.correct) <= 1:
        return "0"
    wrong = len(question.options) - len(question.correct)
    return "-" + MOODLE_FRACTIONS.get(wrong, f"{100 / max(wrong, 1):.5f}") if wrong else "0"


def letters(indices: list[int]) -> str:
    return ", ".join(LETTERS[i] if i < len(LETTERS) else str(i + 1) for i in indices)
