"""Тесты в формате GIFT (импорт в Moodle): категории по модулям, обратная связь из explanation.

Спецсимволы GIFT (~ = # { } : \\) экранируются обратной косой чертой, перевод строки — «\\n». Один верный ответ —
«=вариант», несколько — веса «~%50%вариант» для верных и штраф для неверных (как в Moodle XML).
parse_gift — разбор обратно (для проверки V31): категории, названия, тексты и веса вариантов.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.models import Kit
from app.render.questions import correct_fraction, question_bank, wrong_fraction

GIFT_NAME = "tests.gift"
SPECIAL = "\\~=#{}:"


def escape(text: str) -> str:
    out = []
    for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
        if ch in SPECIAL:
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        else:
            out.append(ch)
    return "".join(out).strip()


def _category_path(program: str, category: str) -> str:
    """Путь категории Moodle: «/» внутри названий разделяет уровни — заменяется на «-»."""
    parts = [p.replace("/", "-").strip() for p in (program, category)]
    return "$course$/top/" + "/".join(parts)


def render_gift(kit: Kit, path: Path) -> int | None:
    """Пишет GIFT; возвращает число вопросов. None — тестовых вопросов в комплекте нет (файл не создаётся)."""
    bank = question_bank(kit)
    if not bank:
        return None
    lines = [
        f"// Тесты программы «{kit.program.title}»",
        "// Формат GIFT: импорт в Moodle — Банк вопросов → Импорт → GIFT. Категории — по модулям программы.",
        "",
    ]
    current = None
    for item in bank:
        if item.category != current:
            current = item.category
            lines += [f"$CATEGORY: {_category_path(kit.program.title, item.category)}", ""]
        q = item.question
        name = escape(f"{q.id} {item.topic_label}".strip())
        lines.append(f"::{name}:: {escape(q.stem)} {{")
        if len(q.correct) == 1:
            for idx, option in enumerate(q.options):
                mark = "=" if idx in q.correct else "~"
                lines.append(f"\t{mark}{escape(option)}")
        else:
            good, bad = correct_fraction(q), wrong_fraction(q)
            for idx, option in enumerate(q.options):
                weight = good if idx in q.correct else bad
                lines.append(f"\t~%{weight}%{escape(option)}")
        if q.explanation.strip():
            lines.append(f"\t####{escape(q.explanation)}")
        lines += ["}", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return len(bank)


# --------------------------------------------------------------------------------------------------
# Разбор (проверка V31)
# --------------------------------------------------------------------------------------------------


@dataclass
class GiftAnswer:
    text: str
    weight: float  # 100 — верный (=), иначе процент из %N%, без веса у «~» — 0


@dataclass
class GiftQuestion:
    category: str
    name: str
    text: str
    answers: list[GiftAnswer] = field(default_factory=list)
    feedback: str = ""


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n" else m.group(1), text).strip()


def _split_unescaped(text: str, chars: str) -> list[tuple[str, str]]:
    """Делит по неэкранированным символам из chars: [(разделитель, кусок)]; первый разделитель — ''."""
    parts: list[tuple[str, str]] = []
    marker, buf, i = "", [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            buf.append(text[i : i + 2])
            i += 2
            continue
        if ch in chars:
            parts.append((marker, "".join(buf)))
            marker, buf = ch, []
        else:
            buf.append(ch)
        i += 1
    parts.append((marker, "".join(buf)))
    return parts


def parse_gift(source: str) -> list[GiftQuestion]:
    """Разбор GIFT в объёме, который пишет render_gift (одно- и многовариантные вопросы, категории, общая
    обратная связь «####»). Ошибка формата — ValueError."""
    questions: list[GiftQuestion] = []
    category = ""
    lines = [ln for ln in source.splitlines() if not ln.lstrip().startswith("//")]
    text = "\n".join(lines)
    pos = 0
    while pos < len(text):
        rest = text[pos:]
        stripped = rest.lstrip()
        if not stripped:
            break
        pos += len(rest) - len(stripped)
        if stripped.startswith("$CATEGORY:"):
            end = text.find("\n", pos)
            end = len(text) if end < 0 else end
            category = text[pos + len("$CATEGORY:") : end].strip()
            pos = end
            continue
        if not stripped.startswith("::"):
            raise ValueError(f"ожидалось «::название::» в позиции {pos}")
        head_end = _find_unescaped(text, "::", pos + 2)
        name = _unescape(text[pos + 2 : head_end])
        open_brace = _find_unescaped(text, "{", head_end + 2)
        close_brace = _find_unescaped(text, "}", open_brace + 1)
        stem = _unescape(text[head_end + 2 : open_brace])
        body = text[open_brace + 1 : close_brace]
        feedback = ""
        general = _find_unescaped(body, "####", 0, required=False)
        if general >= 0:
            feedback = _unescape(body[general + 4 :])
            body = body[:general]
        answers = []
        for marker, chunk in _split_unescaped(body, "=~"):
            if not marker:
                if chunk.strip():
                    raise ValueError(f"текст до первого варианта в вопросе «{name}»")
                continue
            weight = 100.0 if marker == "=" else 0.0
            match = re.match(r"\s*%(-?\d+(?:\.\d+)?)%", chunk)
            if match:
                weight = float(match.group(1))
                chunk = chunk[match.end() :]
            answers.append(GiftAnswer(text=_unescape(chunk), weight=weight))
        questions.append(
            GiftQuestion(category=category, name=name, text=stem, answers=answers, feedback=feedback)
        )
        pos = close_brace + 1
    return questions


def _find_unescaped(text: str, token: str, start: int, required: bool = True) -> int:
    i = start
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text.startswith(token, i):
            return i
        i += 1
    if required:
        raise ValueError(f"не найден «{token}» после позиции {start}")
    return -1
