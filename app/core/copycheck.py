"""Антиплагиат по шинглам (проверка V42, жёсткое правило «не копировать референсы»).

Шингл — n подряд идущих слов (по умолчанию 7) после нормализации. Доля скопированного =
(шинглы текста, найденные в референсах, кроме шинглов белого списка) / (все шинглы текста).
Белый список — шаблонные фразы, которые обязаны совпадать: титул, заголовки разделов, названия актов.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

WORD_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
DEFAULT_N = 7
DEFAULT_THRESHOLD = 0.15

Shingle = tuple[str, ...]

DEFAULT_WHITELIST: list[str] = [
    "Автономная некоммерческая организация дополнительного профессионального образования",
    "Академия Информационных Технологий",
    "ДОПОЛНИТЕЛЬНАЯ ПРОФЕССИОНАЛЬНАЯ ПРОГРАММА ПОВЫШЕНИЯ КВАЛИФИКАЦИИ В ФОРМЕ СТАЖИРОВКИ",
    "ДОПОЛНИТЕЛЬНАЯ ОБЩЕОБРАЗОВАТЕЛЬНАЯ ОБЩЕРАЗВИВАЮЩАЯ ПРОГРАММА",
    "ОБЩАЯ ХАРАКТЕРИСТИКА ДОПОЛНИТЕЛЬНОЙ ПРОФЕССИОНАЛЬНОЙ ПРОГРАММЫ",
    "ОРГАНИЗАЦИОННО-ПЕДАГОГИЧЕСКИЕ УСЛОВИЯ РЕАЛИЗАЦИИ ПРОГРАММЫ",
    "Перечень профессиональных компетенций в рамках имеющейся квалификации",
    "Учебно-методическое и информационное обеспечение",
    "Форма(ы) текущей, промежуточной и итоговой аттестации",
    "Наименование компонентов программы",
    "Содержание учебного материала и формы организации деятельности слушателей",
    "Удостоверение о повышении квалификации установленного образца",
    "Сертификат об освоении программы",
    "Федеральный закон от 29.12.2012 № 273-ФЗ «Об образовании в Российской Федерации»",
    "Об утверждении Порядка организации и осуществления образовательной деятельности "
    "по дополнительным профессиональным программам",
    "Об утверждении Порядка организации и осуществления образовательной деятельности "
    "по дополнительным общеобразовательным программам",
    "с применением электронного обучения и дистанционных образовательных технологий",
    # кто может осваивать ДПП (ч. 3 ст. 76 273-ФЗ) — формула закона, её пишут все программы ПК
    "лица, имеющие среднее профессиональное и (или) высшее образование, а также лица, получающие "
    "среднее профессиональное и (или) высшее образование",
]


def tokenize(text: str) -> list[str]:
    return [w.lower().replace("ё", "е") for w in WORD_RE.findall(text)]


def shingles(text: str, n: int = DEFAULT_N) -> set[Shingle]:
    words = tokenize(text)
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _union(texts: Iterable[str], n: int) -> set[Shingle]:
    result: set[Shingle] = set()
    for text in texts:
        result |= shingles(text, n)
    return result


def copied_shingles(
    text: str,
    references: Sequence[str],
    n: int = DEFAULT_N,
    whitelist: Sequence[str] = tuple(DEFAULT_WHITELIST),
) -> set[Shingle]:
    return (shingles(text, n) & _union(references, n)) - _union(whitelist, n)


def copy_ratio(
    text: str,
    references: Sequence[str],
    n: int = DEFAULT_N,
    whitelist: Sequence[str] = tuple(DEFAULT_WHITELIST),
) -> float:
    """Доля шинглов текста, найденных в референсах (0…1). Текст короче n слов — 0."""
    own = shingles(text, n)
    if not own:
        return 0.0
    return len(copied_shingles(text, references, n, whitelist)) / len(own)


def copied_fragments(
    text: str,
    references: Sequence[str],
    n: int = DEFAULT_N,
    whitelist: Sequence[str] = tuple(DEFAULT_WHITELIST),
    limit: int = 5,
) -> list[str]:
    """Самые длинные скопированные фрагменты (для сообщения методисту)."""
    words = tokenize(text)
    copied = copied_shingles(text, references, n, whitelist)
    marks = [False] * len(words)
    for i in range(len(words) - n + 1):
        if tuple(words[i : i + n]) in copied:
            for j in range(i, i + n):
                marks[j] = True
    runs: list[str] = []
    start: int | None = None
    for i, marked in enumerate([*marks, False]):
        if marked and start is None:
            start = i
        elif not marked and start is not None:
            runs.append(" ".join(words[start:i]))
            start = None
    return sorted(runs, key=len, reverse=True)[:limit]
