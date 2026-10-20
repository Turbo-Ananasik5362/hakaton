"""Тесты в формате Moodle XML: категории по модулям, вопросы multichoice, обратная связь из explanation.

XML строится через ElementTree (экранирование < > & делает библиотека). Текст вопроса и вариантов — format="html":
пользовательский текст сначала экранируется как HTML и оборачивается в <p>, чтобы символы «<», «&» из кода
(например, «df[df.x < 5]») показывались как есть.
"""

from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from pathlib import Path

from app.models import Kit
from app.render.questions import correct_fraction, question_bank, wrong_fraction

XML_NAME = "tests_moodle.xml"


def _html(text: str) -> str:
    return "<p>" + html.escape(text.strip()).replace("\n", "<br>") + "</p>"


def _text(parent: ET.Element, tag: str, value: str, fmt: str | None = "html") -> ET.Element:
    element = ET.SubElement(parent, tag, {"format": fmt} if fmt else {})
    ET.SubElement(element, "text").text = value
    return element


def render_moodle_xml(kit: Kit, path: Path) -> int | None:
    """Пишет Moodle XML; возвращает число вопросов. None — тестовых вопросов нет (файл не создаётся)."""
    bank = question_bank(kit)
    if not bank:
        return None
    quiz = ET.Element("quiz")
    current = None
    program = kit.program.title.replace("/", "-").strip()
    for item in bank:
        if item.category != current:
            current = item.category
            category = ET.SubElement(quiz, "question", {"type": "category"})
            box = ET.SubElement(category, "category")
            ET.SubElement(box, "text").text = f"$course$/top/{program}/{item.category.replace('/', '-')}"
        q = item.question
        question = ET.SubElement(quiz, "question", {"type": "multichoice"})
        name = ET.SubElement(question, "name")
        ET.SubElement(name, "text").text = f"{q.id} {item.topic_label}".strip()
        _text(question, "questiontext", _html(q.stem))
        _text(question, "generalfeedback", _html(q.explanation) if q.explanation.strip() else "")
        ET.SubElement(question, "defaultgrade").text = "1"
        ET.SubElement(question, "penalty").text = "0.3333333"
        ET.SubElement(question, "hidden").text = "0"
        ET.SubElement(question, "single").text = "true" if len(q.correct) == 1 else "false"
        ET.SubElement(question, "shuffleanswers").text = "true"
        ET.SubElement(question, "answernumbering").text = "abc"
        good, bad = correct_fraction(q), wrong_fraction(q)
        for idx, option in enumerate(q.options):
            answer = ET.SubElement(
                question, "answer", {"fraction": good if idx in q.correct else bad, "format": "html"}
            )
            ET.SubElement(answer, "text").text = _html(option)
            _text(answer, "feedback", "")
        tags = ET.SubElement(question, "tags")
        for value in (q.topic_id, *q.lo_ids):
            tag = ET.SubElement(tags, "tag")
            ET.SubElement(tag, "text").text = value
    ET.indent(quiz, space="  ")
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(quiz).write(str(path), encoding="UTF-8", xml_declaration=True)
    return len(bank)
