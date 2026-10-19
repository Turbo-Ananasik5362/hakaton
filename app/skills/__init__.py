"""Навыки агента: app/skills/<step>.md (docs/ARCHITECTURE.md §2.2).

Файл навыка — шапка между строками «---» (step, model, max_tokens, temperature, schema, inputs) и тело на русском.
Тело делится на две части:
- всё, кроме раздела «## Входные данные», — системное сообщение (роль, правила, формат ответа). Оно одинаковое
  для всех запросов, поэтому шлюз может его кешировать;
- раздел «## Входные данные» с подстановками {{name}} — сообщение пользователя.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent
INPUT_HEADING = "## Входные данные"
PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")


class SkillError(ValueError):
    """Файл навыка оформлен неверно (это ошибка разработки, а не запроса)."""


@dataclass(frozen=True)
class Skill:
    step: str
    model: str | None  # None — модель по умолчанию из настроек
    max_tokens: int
    temperature: float
    schema: str  # имя pydantic-класса черновика
    inputs: tuple[str, ...]
    system: str
    user_template: str

    def messages(self, **values: str) -> list[dict[str, str]]:
        """Сообщения для LLM. Нужны ровно те входные данные, что объявлены в шапке."""
        missing = sorted(set(self.inputs) - set(values))
        extra = sorted(set(values) - set(self.inputs))
        if missing or extra:
            raise SkillError(f"Навык {self.step}: не хватает {missing}, лишние {extra}")
        user = PLACEHOLDER.sub(lambda m: values[m.group(1)], self.user_template)
        return [{"role": "system", "content": self.system}, {"role": "user", "content": user}]


def _front_matter(text: str, name: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError(f"Навык {name}: нет шапки «---»")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        raise SkillError(f"Навык {name}: шапка не закрыта «---»") from exc
    header: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise SkillError(f"Навык {name}: строка шапки без «:» — {line!r}")
        header[key.strip()] = value.strip()
    return header, "\n".join(lines[end + 1 :]).strip()


def parse_skill(text: str, name: str = "skill") -> Skill:
    header, body = _front_matter(text, name)
    for key in ("step", "model", "max_tokens", "temperature", "schema", "inputs"):
        if key not in header:
            raise SkillError(f"Навык {name}: в шапке нет «{key}»")
    inputs = tuple(x.strip() for x in header["inputs"].strip("[]").split(",") if x.strip())

    head, sep, tail = body.partition(INPUT_HEADING)
    if not sep:
        raise SkillError(f"Навык {name}: нет раздела «{INPUT_HEADING}»")
    # раздел входных данных идёт до следующего заголовка второго уровня
    next_heading = re.search(r"^## ", tail, flags=re.M)
    user_part = tail[: next_heading.start()] if next_heading else tail
    rest = tail[next_heading.start() :] if next_heading else ""
    system = (head.rstrip() + ("\n\n" + rest.strip() if rest.strip() else "")).strip()
    user_template = user_part.strip()

    found = set(PLACEHOLDER.findall(user_template))
    if found != set(inputs):
        raise SkillError(f"Навык {name}: подстановки {sorted(found)} не совпадают с inputs {sorted(inputs)}")
    if PLACEHOLDER.search(system):
        raise SkillError(f"Навык {name}: подстановки допустимы только в разделе «{INPUT_HEADING}»")

    model = header["model"]
    return Skill(
        step=header["step"],
        model=None if model in ("", "default") else model,
        max_tokens=int(header["max_tokens"]),
        temperature=float(header["temperature"]),
        schema=header["schema"],
        inputs=inputs,
        system=system,
        user_template=user_template,
    )


@cache
def load_skill(name: str) -> Skill:
    path = SKILLS_DIR / f"{name}.md"
    if not path.is_file():
        raise SkillError(f"Нет файла навыка {path}")
    skill = parse_skill(path.read_text(encoding="utf-8"), name)
    if skill.step != name:
        raise SkillError(f"Навык {name}: в шапке step={skill.step!r}")
    return skill
