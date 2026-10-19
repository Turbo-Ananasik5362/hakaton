"""Загрузчик навыков app/skills/*.md."""

from __future__ import annotations

import pytest

from app import models
from app.skills import SKILLS_DIR, SkillError, load_skill, parse_skill

VALID = """---
step: demo
model: default
max_tokens: 500
temperature: 0.2
schema: ClarifyDraft
inputs: [request]
---

## Роль
Ты — методист.

## Входные данные
Запрос: {{request}}

## Формат ответа
Только JSON.
"""


def test_parse_splits_system_and_user() -> None:
    skill = parse_skill(VALID, "demo")
    assert skill.model is None and skill.max_tokens == 500 and skill.inputs == ("request",)
    system, user = skill.messages(request="курс {{не подстановка}} на 16 часов")
    assert (
        system["role"] == "system"
        and "Ты — методист." in system["content"]
        and "Только JSON." in system["content"]
    )
    assert user["content"] == "Запрос: курс {{не подстановка}} на 16 часов"


@pytest.mark.parametrize(
    ("broken", "message"),
    [
        (VALID.replace("schema: ClarifyDraft\n", ""), "schema"),
        (VALID.replace("{{request}}", "{{other}}"), "не совпадают"),
        (VALID.replace("Ты — методист.", "Ты — {{request}}."), "только в разделе"),
        (VALID.replace("## Входные данные", "## Данные"), "Входные данные"),
        (VALID.replace("---\nstep", "step", 1), "шапки"),
    ],
)
def test_broken_skill_is_rejected(broken: str, message: str) -> None:
    with pytest.raises(SkillError, match=message):
        parse_skill(broken, "demo")


def test_messages_require_declared_inputs() -> None:
    skill = parse_skill(VALID, "demo")
    with pytest.raises(SkillError):
        skill.messages()
    with pytest.raises(SkillError):
        skill.messages(request="x", extra="y")


@pytest.mark.parametrize("path", sorted(SKILLS_DIR.glob("*.md")), ids=lambda p: p.stem)
def test_project_skills_load_and_name_existing_schemas(path) -> None:  # noqa: ANN001
    skill = load_skill(path.stem)
    assert hasattr(models, skill.schema), f"нет схемы {skill.schema} в app/models.py"
    assert 0.0 <= skill.temperature <= 0.5 and skill.max_tokens > 0
