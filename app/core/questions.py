"""Тестовые вопросы: повторы по смыслу и ссылки пояснения на номера вариантов.

Дубли (оценка 21.10): «Какая команда выведет на экран текст…» и «Какая команда в Python используется для вывода
текста…» — разные формулировки, те же варианты и тот же верный ответ; «Какие этапы включает создание простого
проекта?» и «… проекта на Python?» — почти одна формулировка. Точное сравнение формулировок их не ловило.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")
STEM_STOPWORDS = frozenset(
    (
        "какой какая какое какие каким какую каких почему зачем когда сколько который которая которое которые "
        "нужно можно следует используется используют является называется верно правильно"
    ).split()
)
MIN_STEMS = 2  # короткие формулировки сравниваются только при совпадении чисел и фрагментов кода
STEM_SIMILARITY = 0.8
OPTION_OVERLAP = 0.75
NUMBER_RE = re.compile(r"[-+−]?\d+(?:[.,]\d+)?")
TOKEN_RE = re.compile(
    r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|«[^»]*»|“[^”]*”|\x60[^\x60]*\x60"""
    r"|\d+(?:\.\d+)?|\w+|[^\w\s]"
)
BRACKET_END: dict[str, str] = {"(": ")", "[": "]", "{": "}"}
QUOTE_STARTS = "\"'«“\x60"


def stem_set(text: str) -> set[str]:
    """Основы значимых слов формулировки (первые 5 букв слов длиной от 4)."""
    words = [w.lower().replace("ё", "е") for w in WORD_RE.findall(text)]
    return {w[:5] for w in words if len(w) >= 4 and w not in STEM_STOPWORDS}


def _option(text: str) -> str:
    # без снятия знаков: «print(5 + 3)» и «print(5) + 3» — разные варианты
    return " ".join(text.lower().split())


def _fragment_end(tokens: Sequence[str], start: int) -> int:
    """Конец фрагмента с учётом вложенных скобок; кавычки уже собраны в отдельные токены."""
    stack: list[str] = []
    for index in range(start, len(tokens)):
        token = tokens[index]
        if token in BRACKET_END:
            stack.append(BRACKET_END[token])
        elif stack and token == stack[-1]:
            stack.pop()
            if not stack:
                return index + 1
    return len(tokens)


def _non_root_tokens(text: str) -> tuple[tuple[str, ...], ...]:
    """Числа и содержимое скобок, кавычек и части после двоеточия, без потери знаков и регистра кода."""
    tokens = TOKEN_RE.findall(text.rstrip().rstrip("?"))
    result: list[tuple[str, ...]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == ":":
            result.append(tuple(tokens[index + 1 :]))
            break
        if token in BRACKET_END:
            end = _fragment_end(tokens, index)
            result.append(tuple(tokens[index:end]))
            index = end
            continue
        if token[0] in QUOTE_STARTS:
            result.append((token,))
        elif NUMBER_RE.fullmatch(token):
            sign = tokens[index - 1] if index and tokens[index - 1] in {"+", "-", "−"} else ""
            result.append((sign + token,))
        index += 1
    return tuple(result)


def same_question(
    stem_a: str,
    options_a: Sequence[str],
    correct_a: Sequence[int],
    stem_b: str,
    options_b: Sequence[str],
    correct_b: Sequence[int],
) -> bool:
    """Похожие формулировки или содержательные варианты, если числа и фрагменты кода совпадают."""
    if _non_root_tokens(stem_a) != _non_root_tokens(stem_b):
        return False
    a, b = stem_set(stem_a), stem_set(stem_b)
    # Коэффициент Дайса сохраняет короткие формулировки с небольшим уточнением контекста.
    if len(a) >= MIN_STEMS and len(b) >= MIN_STEMS and 2 * len(a & b) / (len(a) + len(b)) >= STEM_SIMILARITY:
        return True
    opts_a, opts_b = {_option(o) for o in options_a}, {_option(o) for o in options_b}
    right_a = {_option(options_a[i]) for i in correct_a if 0 <= i < len(options_a)}
    right_b = {_option(options_b[i]) for i in correct_b if 0 <= i < len(options_b)}
    if not right_a or right_a != right_b or not opts_a or not opts_b:
        return False
    if all(NUMBER_RE.fullmatch(option) or len(WORD_RE.findall(option)) <= 1 for option in opts_a | opts_b):
        return False
    return len(opts_a & opts_b) >= OPTION_OVERLAP * min(len(opts_a), len(opts_b))


# «Вариант 1», «вариант №2», «вариант «в»», «вариант б)», «Вариант Г»: номера вариантов в пояснении модели
OPTION_REF_RE = re.compile(
    r"\b(?P<word>[Вв]ариант|[Оо]твет)\b\s*(?:№\s*)?"  # \b: «ответа)» — не «ответ а)» (ревью 22.10)
    r"(?:(?P<num>[1-9])(?![\d.,])|[«\"](?P<qletter>[а-гa-d])[»\"]|(?P<pletter>[а-гa-d])\)|(?P<uletter>[А-ГA-D])(?![\w]))"
)
LETTERS = "абвг"
LATIN = "abcd"


def _ref_index(match: re.Match[str]) -> int | None:
    if match.group("num"):
        return int(match.group("num")) - 1
    letter = (match.group("qletter") or match.group("pletter") or match.group("uletter") or "").lower()
    if letter in LETTERS:
        return LETTERS.index(letter)
    if letter in LATIN:
        return LATIN.index(letter)
    return None


def rewrite_option_refs(explanation: str, options: Sequence[str], correct: Sequence[int]) -> str:
    """Номера вариантов в пояснении → «верный ответ» или текст варианта. Код перемешивает варианты, и «Вариант 1»
    модели после перемешивания указывал на другой вариант (оценка 21.10: «Вариант 1» при ключе «г»).
    options и correct — в порядке ответа модели, до перемешивания."""

    def replace(match: re.Match[str]) -> str:
        index = _ref_index(match)
        if index is None or index >= len(options):
            return match.group(0)
        upper = match.group("word")[0].isupper()
        if index in correct:
            return "Верный ответ" if upper else "верный ответ"
        text = options[index].strip().rstrip(".")
        short = text if len(text) <= 40 else text[:37].rstrip() + "…"
        return f"{'Вариант' if upper else 'вариант'} «{short}»"

    return OPTION_REF_RE.sub(replace, explanation)
