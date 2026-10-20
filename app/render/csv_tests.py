"""Тесты в CSV для таблиц (Excel, LibreOffice): разделитель «;», кодировка UTF-8 с BOM — так русский Excel
открывает файл без мастера импорта. Одна строка — один вопрос, верные ответы — буквами вариантов."""

from __future__ import annotations

import csv
from pathlib import Path

from app.models import Kit, QuestionKind
from app.render.questions import LETTERS, letters, question_bank

CSV_NAME = "tests.csv"
BASE_COLUMNS = ["Категория", "Тест", "ID вопроса", "Тема", "Результаты обучения", "Тип", "Вопрос"]
TAIL_COLUMNS = ["Верные ответы", "Пояснение", "Сложность"]


FORMULA_START = ("=", "+", "-", "@")


def safe(value: object) -> object:
    """Текст, который таблица приняла бы за формулу («=СУММ(…)», «-1»), — с апострофом в начале."""
    if isinstance(value, str) and value.startswith(FORMULA_START):
        return "'" + value
    return value


def columns(max_options: int) -> list[str]:
    return [
        *BASE_COLUMNS,
        *[f"Вариант {LETTERS[i] if i < len(LETTERS) else i + 1}" for i in range(max_options)],
        *TAIL_COLUMNS,
    ]


def render_csv(kit: Kit, path: Path) -> int | None:
    """Пишет CSV; возвращает число вопросов. None — тестовых вопросов нет (файл не создаётся)."""
    bank = question_bank(kit)
    if not bank:
        return None
    width = max(len(item.question.options) for item in bank)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(columns(width))
        for item in bank:
            q = item.question
            kind = (
                "несколько ответов" if q.kind == QuestionKind.MULTIPLE or len(q.correct) > 1 else "один ответ"
            )
            options = list(q.options) + [""] * (width - len(q.options))
            writer.writerow(
                [
                    safe(v)
                    for v in [
                        item.category,
                        item.test.title or item.test.id,
                        q.id,
                        item.topic_label,
                        ", ".join(q.lo_ids),
                        kind,
                        q.stem,
                        *options,
                        letters(q.correct),
                        q.explanation,
                        q.difficulty,
                    ]
                ]
            )
    return len(bank)
