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


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            "Какой результат выполнения кода: for i in range(3): print(i)?",
            "Какой результат выполнения кода: for i in range(1, 4): print(i * 2)?",
        ),
        (
            "Какой результат выполнения кода print((left + right) * factor)?",
            "Какой результат выполнения кода print((left - right) * factor)?",
        ),
        (
            "Какой результат выполнения кода: left + right?",
            "Какой результат выполнения кода: left - right?",
        ),
        (
            'Какой результат вернёт обработка строки "result_a"?',
            'Какой результат вернёт обработка строки "result_b"?',
        ),
        (
            "Какой результат вернёт обработка строки «result_a»?",
            "Какой результат вернёт обработка строки «result_b»?",
        ),
        (
            "Какой результат вернёт обработка списка [first, second]?",
            "Какой результат вернёт обработка списка [second, first]?",
        ),
        (
            "Какой результат вернёт обработка значения (Value)?",
            "Какой результат вернёт обработка значения (value)?",
        ),
        (
            "Какой результат вернёт обработка числа 10?",
            "Какой результат вернёт обработка числа 20?",
        ),
        (
            "Какой результат вернёт обработка числа -10?",
            "Какой результат вернёт обработка числа 10?",
        ),
        (
            'Какой результат вернёт обработка строки "a b"?',
            'Какой результат вернёт обработка строки "ab"?',
        ),
    ],
    ids=[
        "loop_bounds_and_expression",
        "nested_parentheses",
        "code_after_colon",
        "quoted_string",
        "quoted_string_guillemets",
        "list_order",
        "code_case",
        "numbers",
        "number_sign",
        "string_whitespace",
    ],
)
def test_different_payloads_are_not_duplicates(first: str, second: str) -> None:
    options = ["Первое значение", "Второе значение", "Третье значение", "Ошибка выполнения"]
    assert not same_question(first, options, [0], second, options, [0])
    assert not same_question(second, options, [0], first, options, [0])


@pytest.mark.parametrize(
    "options",
    [
        ["1", "2", "3", "4"],
        ["1.5", "2.5", "3.5", "4.5"],
        ["1,5", "2,5", "3,5", "4,5"],
        ["Байт", "Бит", "Слово", "Элемент"],
    ],
)
def test_short_options_do_not_match_unrelated_questions(options: list[str]) -> None:
    assert not same_question(
        "Сколько байт занимает тип int?",
        options,
        [0],
        "Сколько элементов в списке [1, 2]?",
        options,
        [0],
    )
    # Одинаковые короткие ответы не должны связывать и вопросы без чисел или скобок.
    assert not same_question(
        "Что определяет размер памяти?",
        options,
        [0],
        "Какая операция меняет порядок элементов?",
        options,
        [0],
    )


def test_command_paraphrases_with_same_answers_remain_duplicates() -> None:
    assert same_question(
        "Какая команда выведет на экран текст?",
        OPTIONS,
        [1],
        "Какая команда в Python используется для вывода текста?",
        OPTIONS,
        [1],
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            "Какие этапы включает создание простого проекта?",
            "Какие этапы включает создание простого проекта на Python?",
        ),
        ("Что такое переменная?", "Что такое переменная в Python?"),
    ],
)
def test_wording_with_minor_context_remains_duplicate(first: str, second: str) -> None:
    # Совпадение формулировки сохраняется даже при разных коротких вариантах.
    assert same_question(first, ["а", "б"], [0], second, ["в", "г"], [1])
    assert same_question(second, ["в", "г"], [1], first, ["а", "б"], [0])


def test_code_whitespace_does_not_change_question() -> None:
    assert same_question(
        "Какой результат выполнения кода: print(range(1,4))?",
        ["а", "б"],
        [0],
        "Какой результат выполнения кода: print( range(1, 4) )?",
        ["в", "г"],
        [1],
    )


def test_option_refs_ignore_word_endings() -> None:
    """«…из правильного ответа).» и «два варианта)» — окончания слов, а не буквы вариантов (ревью 22.10)."""
    from app.core.questions import rewrite_option_refs

    for text in ("Выберите формулировку из правильного ответа).", "Сравните два варианта) и выберите один."):
        assert rewrite_option_refs(text, ["Да", "Нет", "Иногда", "Всегда"], [0]) == text
