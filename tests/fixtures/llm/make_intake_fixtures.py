"""Генератор mock-ответов модели для шагов intake и clarify: `python tests/fixtures/llm/make_intake_fixtures.py`.

Имя файла — tests/fixtures/llm/<step>/<ключ запроса>.json, где ключ — хеш текста запроса (intake.request_key,
clarify.clarify_key). Поле `_request` — только для людей. default.json — «пустой» ответ модели: тогда работают
одни правила кода. Если поменяли текст запроса в тестах — перезапустите генератор.

Ответы написаны так, как правдоподобно ответил бы GigaChat, включая типичные ошибки (например, в запросе 4
модель додумывает тип, уровень слушателей, форму и ИА — тесты проверяют, что код этому не верит).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import ClarifyDraft, IntakeDraft, RequestSpec  # noqa: E402
from app.pipeline.clarify import clarify_key  # noqa: E402
from app.pipeline.intake import request_key  # noqa: E402
from tests.intake_queries import OPEN, OWN  # noqa: E402

HERE = Path(__file__).resolve().parent
INTAKE = HERE / "intake"
CLARIFY = HERE / "clarify"


def aud(
    category: str,
    entry: str | None = None,
    grade: str | None = None,
    domain: str | None = None,
    patterns: tuple[str, ...] | list[str] = (),
) -> dict:
    return {
        "category": category,
        "age_or_grade": grade,
        "entry_level": entry,
        "requirements": None,
        "domain": domain,
        "domain_patterns": list(patterns),
        "constraints": [],
    }


def draft(**fields: object) -> dict:
    base: dict = {
        "program_type_guess": None,
        "title": None,
        "audience": aud(""),
        "form_kind": None,
        "dot": False,
        "eo": False,
        "quantities": [],
        "duration_text": None,
        "fa_form": None,
        "fa_text": None,
        "required_topics": [],
        "required_seminar_types": [],
        "ru_services_only": False,
        "no_programming": False,
        "anonymized_data": False,
        "all_srs": False,
        "intro_block_topic": None,
        "slides_request": None,
        "assumptions": [],
    }
    base.update(fields)
    IntakeDraft.model_validate(base)
    return base


def q(what: str, text: str) -> dict:
    return {"what": what, "text": text}


DRAFTS: dict[str, dict] = {
    OPEN[1]["request"]: draft(
        program_type_guess="dop",
        title="Первые шаги в Python",
        audience=aud("школьники 8–9 классов", entry="без опыта программирования", grade="8–9 классы"),
        form_kind="ochnaya",
        quantities=[
            q("total_hours", "Объем 16 часов"),
            q("session_hours", "по 2 часа"),
            q("sessions_per_week", "два раза в неделю"),
            q("test_questions", "итоговый тест на 10 вопросов"),
        ],
        fa_form="project_defense",
        fa_text="каждый ученик защищает небольшой проект, например простую игру или чат-бота",
        slides_request="слайды для первых двух занятий",
    ),
    OPEN[2]["request"]: draft(
        program_type_guess="pk",
        title="Искусственный интеллект в работе учителя",
        audience=aud(
            "учителя общеобразовательных школ Ингушетии",
            domain="школа",
            patterns=["урок", "ученик", "школ", "класс", "учител"],
        ),
        form_kind="ochno_zaochnaya",
        dot=True,
        quantities=[q("total_hours", "36 часов"), q("portfolio_items", "портфолио из трёх материалов")],
        fa_form="portfolio",
        fa_text="зачёт по портфолио из трёх материалов, созданных с помощью ИИ",
        required_topics=["риски и безопасное использование ИИ"],
        required_seminar_types=["план урока", "задания для учеников", "проверочная работа"],
        ru_services_only=True,
    ),
    OPEN[3]["request"]: draft(
        program_type_guess="pk",
        title="Анализ данных на Python",
        audience=aud(
            "сотрудники региональных отделений банков",
            entry="работают в Excel, программированием не владеют",
            domain="банк",
            patterns=["банк", "клиент", "кредит", "вклад", "отделени"],
        ),
        form_kind="ochno_zaochnaya",
        quantities=[q("total_hours", "на 72 часа"), q("weeks", "срок 4 недели")],
        fa_form="project_defense",
        fa_text="защита проекта на обезличенных банковских данных",
        required_topics=["pandas", "визуализация", "SQL для выгрузки данных", "основы статистики"],
        anonymized_data=True,
        intro_block_topic="основы Python",
        slides_request="Слайды нужны для модуля про pandas",
        assumptions=["Структура разделов берётся из приложенной программы, содержание — новое."],
    ),
    OPEN[4]["request"]: draft(
        program_type_guess="pk",
        title="Основы кибербезопасности",
        audience=aud("студенты колледжа", entry="начальный уровень"),
        form_kind="ochnaya",
        quantities=[q("months", "примерно на месяц")],
        duration_text="примерно на месяц",
        fa_form="exam_practical",
        assumptions=["Не указан объём программы в часах.", "Не указан тип программы."],
    ),
    OPEN[5]["request"]: draft(
        program_type_guess="pk_internship",
        title="DevOps-инженер от Linux до CI/CD",
        audience=aud(
            "специалисты, проходящие стажировку в ИТ-компаниях региона",
            domain="ИТ-компания",
            patterns=["компани", "сервер", "инфраструктур", "развертыван"],
        ),
        quantities=[
            q("total_hours", "на 256 часов"),
            q("fa_hours", "Итоговая аттестация занимает 2 часа"),
            q("months", "Срок около двух месяцев"),
            q("hours_per_day", "не больше 4 часов в день"),
        ],
        duration_text="около двух месяцев",
        fa_form="report_defense",
        fa_text="защита отчёта о стажировке",
        required_topics=["Linux", "CI/CD"],
        all_srs=True,
    ),
    OWN["med_security"]: draft(
        program_type_guess="pk",
        title="Информационная безопасность в медицинской организации",
        audience=aud(
            "медицинские регистраторы и администраторы поликлиник",
            domain="поликлиника",
            patterns=["пациент", "поликлиник", "медицин", "регистратур"],
        ),
        form_kind="zaochnaya",
        dot=True,
        quantities=[q("total_hours", "24 часа")],
        fa_form="test",
        fa_text="тестирование",
    ),
    OWN["arduino_kids"]: draft(
        program_type_guess="dop",
        title="Робототехника на Arduino",
        audience=aud("дети 10–12 лет", grade="10–12 лет"),
        form_kind="ochnaya",
        quantities=[
            q("total_hours", "36 часов"),
            q("session_hours", "занятия по 90 минут"),
            q("sessions_per_week", "раз в неделю"),
        ],
        fa_form="project_defense",
        fa_text="выставка собственных проектов",
        required_topics=["Arduino"],
    ),
    OWN["accounting_1c"]: draft(
        program_type_guess="pk",
        title="1С:Бухгалтерия для малого бизнеса",
        audience=aud(
            "бухгалтеры малого бизнеса",
            domain="бухгалтерия",
            patterns=["бухгалтер", "проводк", "налог", "отчетност"],
        ),
        form_kind="ochnaya",
        quantities=[q("total_hours", "на 40 часов")],
        fa_form="exam_practical",
        fa_text="экзамен с практическим заданием",
        required_topics=["1С:Бухгалтерия"],
    ),
    OWN["sql_economists"]: draft(
        program_type_guess="pk",
        title="Основы SQL для экономистов",
        audience=aud(
            "экономисты", domain="экономика предприятия", patterns=["экономи", "бюджет", "отчет", "затрат"]
        ),
        quantities=[q("total_hours", "16 часов"), q("months", "срок 3 месяца")],
        duration_text="3 месяца",
        fa_form="zachet",
        fa_text="зачёт",
        required_topics=["SQL"],
        assumptions=["16 часов на 3 месяца — очень редкие занятия."],
    ),
    OWN["sales_ai"]: draft(
        program_type_guess="pk",
        title="Нейросети для подготовки коммерческих предложений",
        audience=aud(
            "сотрудники отдела продаж", domain="продажи", patterns=["клиент", "продаж", "сделк", "коммерческ"]
        ),
        quantities=[q("weeks", "примерно за две недели")],
        required_seminar_types=["коммерческое предложение"],
    ),
}

CLARIFY_FIXTURES: dict[tuple[str, tuple[str, ...]], dict] = {
    (OWN["sql_economists"], ("conflict_load",)): {
        "questions": [
            {
                "id": "conflict_load",
                "text": "В запросе 16 часов и срок 3 месяца — это чуть больше часа в неделю. Что поправить?",
                "options": [
                    "Сократить срок, оставить 16 часов",
                    "Увеличить объём, оставить срок 3 месяца",
                    "Оставить как есть",
                ],
            }
        ]
    }
}


def main() -> None:
    INTAKE.mkdir(parents=True, exist_ok=True)
    CLARIFY.mkdir(parents=True, exist_ok=True)
    (INTAKE / "default.json").write_text("{}\n", encoding="utf-8")
    (CLARIFY / "default.json").write_text('{"questions": []}\n', encoding="utf-8")
    for text, data in DRAFTS.items():
        path = INTAKE / f"{request_key(text)}.json"
        path.write_text(
            json.dumps({"_request": text, **data}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(path.relative_to(ROOT), "—", text[:60])
    for (text, conflicts), data in CLARIFY_FIXTURES.items():
        ClarifyDraft.model_validate(data)
        key = clarify_key(RequestSpec(raw_text=text), list(conflicts))
        path = CLARIFY / f"{key}.json"
        payload = {"_request": text, "_conflicts": list(conflicts), **data}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(path.relative_to(ROOT), "—", text[:60])


if __name__ == "__main__":
    main()
