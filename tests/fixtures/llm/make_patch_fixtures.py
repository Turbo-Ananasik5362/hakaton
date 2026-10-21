"""Генератор mock-ответов модели для точечной правки: `python tests/fixtures/llm/make_patch_fixtures.py`.

Правка вызывает те же навыки, что и сборка (module_content, lectures, seminars, tests), с ключом заготовки
«<хеш запроса>-<ID модуля>-patch» (app/pipeline/patch.py, `_patch_key`). Здесь эти заготовки строятся из ответов
сборки (make_content_fixtures.py) для открытых запросов 1 и 3 с изменённым текстом: вопросы — новые формулировки
(иначе правка отбросила бы их как повтор), сложность — на ступень выше. Фикстуры проверяют код правки, а не
качество формулировок.

Перегенерировать после make_content_fixtures.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import Kit  # noqa: E402
from app.pipeline.intake import request_key  # noqa: E402

LLM = Path(__file__).resolve().parent
KITS = LLM.parent / "kits"
QUERIES = (1, 3)
STEPS = ("module_content", "lectures", "seminars", "tests")
NOTE = " — на примере из практики слушателей"


def _content(data: dict) -> dict:
    for topic in data.get("topics", []):
        for kind in ("lk", "pr", "lb", "srs"):
            topic[kind] = [item + NOTE for item in topic.get(kind, [])]
    return data


def _lectures(data: dict) -> dict:
    for lecture in data.get("lectures", []):
        lecture["goal"] = "Обновлённая лекция: " + lecture.get("goal", "")
        for item in lecture.get("plan", []):
            item["theses"] = [t + NOTE for t in item.get("theses", [])]
    return data


def _seminars(data: dict) -> dict:
    for seminar in data.get("seminars", []):
        seminar["task"] = seminar.get("task", "") + " Возьмите пример из своей работы."
    return data


def _tests(data: dict) -> dict:
    for question in data.get("questions", []):
        stem = question.get("stem", "")
        question["stem"] = "Ситуация из практики: " + stem[:1].lower() + stem[1:]
        question["difficulty"] = min(3, int(question.get("difficulty", 1)) + 1)
    for iq in data.get("interview", []):
        iq["text"] = "Приведите пример из практики: " + iq.get("text", "")
    return data


TRANSFORMS = {"module_content": _content, "lectures": _lectures, "seminars": _seminars, "tests": _tests}


def main() -> None:
    written = 0
    for n in QUERIES:
        kit = Kit.model_validate_json((KITS / f"open_q{n}.json").read_text(encoding="utf-8"))
        key = request_key(kit.request.raw_text)
        for step in STEPS:
            folder = LLM / step
            for source in sorted(folder.glob(f"{key}-M*.json")):
                if "-patch" in source.stem:
                    continue
                module, _, chunk = source.stem[len(key) + 1 :].partition("-")
                name = f"{key}-{module}-patch" + (f"-{chunk}" if chunk else "") + ".json"
                data = TRANSFORMS[step](json.loads(source.read_text(encoding="utf-8")))
                (folder / name).write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                written += 1
    print(f"Заготовок правки записано: {written}")


if __name__ == "__main__":
    main()
