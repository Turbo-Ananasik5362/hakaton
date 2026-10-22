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
MIN_STEMS = 3  # меньше значимых слов — формулировки не сравниваются («Что выведет print(2+3)?»)
STEM_SIMILARITY = 0.8
OPTION_OVERLAP = 0.75


def stem_set(text: str) -> set[str]:
    """Основы значимых слов формулировки (первые 5 букв слов длиной от 4)."""
    words = [w.lower().replace("ё", "е") for w in WORD_RE.findall(text)]
    return {w[:5] for w in words if len(w) >= 4 and w not in STEM_STOPWORDS}


def _option(text: str) -> str:
    # без снятия знаков: «print(5 + 3)» и «print(5) + 3» — разные варианты
    return " ".join(text.lower().split())


def same_question(
    stem_a: str,
    options_a: Sequence[str],
    correct_a: Sequence[int],
    stem_b: str,
    options_b: Sequence[str],
    correct_b: Sequence[int],
) -> bool:
    """Два вопроса проверяют одно и то же: почти одна формулировка или те же варианты с тем же верным ответом."""
    a, b = stem_set(stem_a), stem_set(stem_b)
    if len(a) >= MIN_STEMS and len(b) >= MIN_STEMS and len(a & b) / len(a | b) >= STEM_SIMILARITY:
        return True
    opts_a, opts_b = {_option(o) for o in options_a}, {_option(o) for o in options_b}
    right_a = {_option(options_a[i]) for i in correct_a if 0 <= i < len(options_a)}
    right_b = {_option(options_b[i]) for i in correct_b if 0 <= i < len(options_b)}
    if not right_a or right_a != right_b or not opts_a or not opts_b:
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
