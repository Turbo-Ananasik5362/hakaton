"""Тестовые вопросы: повторы по смыслу и номера вариантов в пояснениях (app/core/questions.py)."""

from __future__ import annotations

import pytest

from app.core.questions import rewrite_option_refs, same_question

OPTIONS = ["print Привет", "print('Привет')", "echo 'Привет'", "output('Привет')"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Вариант 2 верен: print выводит строку.", "Верный ответ верен: print выводит строку."),
        ("Вариант 1 не подходит: нет скобок.", "Вариант «print Привет» не подходит: нет скобок."),
        (
            "верный — вариант «б», а вариант «в» — команда оболочки.",
            "верный — верный ответ, а вариант «echo 'Привет'» — команда оболочки.",
        ),
        ("Ответ Г — несуществующая функция.", "Вариант «output('Привет')» — несуществующая функция."),
        ("Вариант 7 не существует.", "Вариант 7 не существует."),
        ("Ответ в задаче — строка.", "Ответ в задаче — строка."),
        ("Python 3 печатает через print().", "Python 3 печатает через print()."),
    ],
)
def test_option_numbers_in_explanation_survive_shuffling(text: str, expected: str) -> None:
    """Код перемешивает варианты, и «Вариант 1» модели указывал на другой вариант («Вариант 1» при ключе «г»)."""
    assert rewrite_option_refs(text, OPTIONS, [1]) == expected


def test_same_question_by_options_or_wording() -> None:
    other = ["show 'Привет'", "print('Привет')", "echo 'Привет'", "output('Привет')"]
    assert same_question(
        "Как вывести текст на экран?", OPTIONS, [1], "Какая функция печатает строку?", other, [1]
    )
    assert not same_question("Как вывести текст на экран?", OPTIONS, [1], "Что не выводит текст?", other, [2])
    assert same_question(
        "Какие этапы включает создание простого программного проекта?",
        ["а", "б"],
        [0],
        "Какие этапы включает создание простого программного проекта на Python?",
        ["в", "г"],
        [1],
    )
    assert not same_question(
        "Что выведет print(2 + 3)?", ["5", "6"], [0], "Что выведет print(2 * 3)?", ["5", "6"], [1]
    )


def test_option_refs_ignore_word_endings() -> None:
    """«…из правильного ответа).» и «два варианта)» — окончания слов, а не буквы вариантов (ревью 22.10)."""
    from app.core.questions import rewrite_option_refs

    for text in ("Выберите формулировку из правильного ответа).", "Сравните два варианта) и выберите один."):
        assert rewrite_option_refs(text, ["Да", "Нет", "Иногда", "Всегда"], [0]) == text
