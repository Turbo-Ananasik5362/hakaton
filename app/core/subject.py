"""Предмет программы среди «обязательных тем» запроса.

Единственная обязательная тема, которая есть в названии программы, — сам предмет («Первые шаги в Python» → «Python»,
«Кибербезопасность для студентов» → «кибербезопасность»), а не тема внутри неё: её покрывает вся программа.
Модель делала из неё отдельный модуль «…: обязательная тема» (прогон 21.10), а проверка V33 требовала такой модуль
(прогон 22.10). Правило одно для каркаса (app/pipeline/skeleton.py) и проверки V33 (app/core/validate.py).
"""

from __future__ import annotations

import re

from app.models import TopicRequirement

FLAGS = re.IGNORECASE | re.UNICODE
# «Анализ данных на Python»: язык — средство, а не предмет; тема по нему может быть нужна (вводный блок)
TOOL_PREFIX_RE = re.compile(r"(?:\bна|с\s+помощью|с\s+использованием|средствами|в\s+среде)\s+$", FLAGS)


def _fold(text: str) -> str:
    return text.replace("ё", "е").replace("Ё", "Е").replace(" ", " ")


def _match(pattern: str, text: str) -> re.Match[str] | None:
    try:
        return re.search(pattern, text, FLAGS)
    except re.error:
        return None


def _is_tool_in(req: TopicRequirement, title: str) -> bool:
    for pattern in req.patterns:
        match = _match(pattern, title)
        if match and TOOL_PREFIX_RE.search(title[: match.start()]):
            return True
    return False


def subject_requirement(required: list[TopicRequirement], title: str | None) -> TopicRequirement | None:
    """Обязательная тема — предмет программы: единственная из названия и не средство («на Python»).
    Несколько тем из названия («от Linux до CI/CD») — крайние точки программы, предмета среди них нет."""
    if not title:
        return None
    text = _fold(title)
    in_title = [r for r in required if any(_match(p, text) for p in r.patterns)]
    if len(in_title) != 1 or _is_tool_in(in_title[0], text):
        return None
    return in_title[0]


def without_subject(required: list[TopicRequirement], title: str | None) -> list[TopicRequirement]:
    subject = subject_requirement(required, title)
    return [r for r in required if r is not subject]
