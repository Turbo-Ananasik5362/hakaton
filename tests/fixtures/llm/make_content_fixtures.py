"""Генератор mock-ответов модели для шагов содержания: `python tests/fixtures/llm/make_content_fixtures.py`.

Для каждого запроса строится каркас (intake → clarify → skeleton в mock, как в боевом пайплайне), и по нему —
ответы каждого навыка содержания: program_texts, final_attestation, literature, judge (ключ — хеш текста запроса),
module_content, lectures, seminars, tests, module_pack (ключ — «<хеш>-<ID модуля>»). Ответы ссылаются на настоящие
ID тем и РО каркаса и выполняют квоты, которые считает код (app/pipeline/content.py), — как правдоподобно ответил
бы GigaChat. Тексты шаблонные: фикстуры проверяют код, а не качество формулировок.

Перегенерировать после изменения каркасных фикстур (make_skeleton_fixtures.py) или правил квот.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.core import bibliography  # noqa: E402
from app.core.ids import display_numbers  # noqa: E402
from app.core.registry import default_registry  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import FAForm, Kit, Module, Topic, WorkKind  # noqa: E402
from app.pipeline import content  # noqa: E402
from app.pipeline import content_apply as apply  # noqa: E402
from app.pipeline import content_prompts as prompts  # noqa: E402
from app.pipeline.intake import request_key  # noqa: E402
from app.storage.sessions import SessionStore  # noqa: E402
from tests.intake_queries import OPEN, OWN  # noqa: E402

FOLDER = Path(__file__).resolve().parent
QUERIES: dict[str, dict] = {
    "q1": OPEN[1],
    "q2": OPEN[2],
    "q3": OPEN[3],
    "q4": OPEN[4],
    "q5": OPEN[5],
    "accounting_1c": {"request": OWN["accounting_1c"], "attachments": [], "methodist_answers": {}},
}


# --------------------------------------------------------------------------------------------------
# Каркас в mock
# --------------------------------------------------------------------------------------------------


def build_kit(query: dict) -> Kit:
    from scripts.run_open_query import attachments_payload

    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(sessions_dir=Path(tmp) / "sessions", llm_mode="mock")
        with TestClient(create_app(settings=settings)) as client:
            payload = {"request": query["request"], "attachments": attachments_payload(query)}
            body = client.post("/api/v1/sessions", json=payload).json()
            answers = query.get("methodist_answers") or {}
            while body["status"] == "need_clarification":
                given = {
                    q["id"]: answers.get(q["id"], answers.get("_default", (q.get("options") or ["да"])[0]))
                    for q in body["questions"]
                }
                body = client.post(
                    f"/api/v1/sessions/{body['session_id']}/answers", json={"answers": given}
                ).json()
            assert body["status"] == "skeleton_ready", body
            kit = SessionStore(settings.sessions_dir).load_kit(body["session_id"])
            assert kit is not None
            return kit


# --------------------------------------------------------------------------------------------------
# Тексты-шаблоны
# --------------------------------------------------------------------------------------------------


def _who(kit: Kit) -> str:
    audience = kit.program.audience
    return audience.category or "слушатели"


def _domain(kit: Kit) -> str:
    audience = kit.program.audience
    patterns = audience.domain_patterns or kit.request.audience.domain_patterns
    return audience.domain or (patterns[0] if patterns else "рабочей практики")


def _domain_word(kit: Kit, n: int) -> str:
    patterns = kit.program.audience.domain_patterns or kit.request.audience.domain_patterns or [_domain(kit)]
    return patterns[n % len(patterns)]


def program_texts(kit: Kit) -> dict:
    title, who = kit.program.title, _who(kit)
    values = {
        "annotation": (
            f"Программа «{title}» адресована: {who}. Слушатели осваивают инструменты и приёмы, которые сразу "
            "применяют в своей работе, выполняют практические задания на материале своей сферы и завершают "
            "обучение итоговой работой."
        ),
        "relevance": (
            f"Навыки, которые даёт программа «{title}», нужны уже сейчас: цифровые инструменты входят в учёбу "
            "и повседневную жизнь, а ранний опыт практической работы помогает выбрать будущую профессию."
        ),
        "form_description": (
            "Обучение проходит по расписанию: теория сразу закрепляется практикой, на каждом занятии слушатели "
            "выполняют задание и получают обратную связь преподавателя."
        ),
        "audience_requirements": f"К обучению допускаются: {who}. Специальной подготовки не требуется сверх указанной в программе.",
        "mto": [
            "Учебная аудитория с проектором и маркерной доской",
            "Компьютеры для каждого слушателя с доступом к сети Интернет",
            "Учебная среда для выполнения практических заданий",
            "Электронная информационно-образовательная среда для материалов и заданий",
        ],
        "software": [
            "Офисный пакет МойОфис",
            "Браузер Яндекс Браузер",
            "Система дистанционного обучения Moodle",
        ],
        "org_process": (
            "Занятия проводятся по учебному плану и календарному графику. Практические задания выполняются "
            "на занятиях и самостоятельно, результаты проверяет преподаватель."
        ),
        "staffing_requirements": (
            "Преподаватели с высшим образованием по профилю программы и опытом практической работы в этой сфере."
        ),
        "methodical": (
            "Используются объяснение с демонстрацией, практическая работа по образцу и самостоятельные мини-проекты; "
            "к каждому занятию готовятся раздаточные материалы и примеры."
        ),
        "satisfaction": (
            "После завершения обучения слушатели заполняют анонимную анкету об удовлетворённости содержанием, "
            "организацией и работой преподавателей; результаты учитываются при обновлении программы."
        ),
    }
    return {name: values[name] for name in prompts.text_fields(kit)}


def final_attestation(kit: Kit) -> dict:
    fa = kit.final_attestation
    form = fa.form if fa is not None else FAForm.ZACHET
    domain = _domain(kit)
    draft: dict = {
        "form_text": fa.form_text if fa is not None else "",
        "lo_ids": [o.id for o in kit.outcomes],
        "requirements": [
            "Работа выполняется самостоятельно по темам программы",
            f"Материал работы относится к сфере: {domain}",
            "Результат оформляется по шаблону и сдаётся до начала аттестации",
        ],
        "criteria": [
            {"text": "Работа выполнена полностью и соответствует заданию", "max_points": 4},
            {"text": "Решение обосновано и опирается на материал программы", "max_points": 3},
            {"text": "Слушатель уверенно отвечает на вопросы по своей работе", "max_points": 3},
        ],
        "pass_rule": "Зачтено — не меньше 6 баллов из 10.",
        "procedure": [
            "Слушатель заранее сдаёт работу преподавателю",
            "Представляет результат комиссии",
            "Отвечает на вопросы комиссии",
            "Комиссия выставляет баллы по критериям и объявляет результат",
        ],
    }
    if form == FAForm.EXAM_PRACTICAL:
        draft["example_task"] = {
            "title": "Практическое задание экзамена",
            "environment": "Учебный стенд с установленной программой и заранее подготовленными исходными данными",
            "description": f"Выполнить типовую рабочую операцию из сферы «{domain}» и проверить результат",
            "deliverable": "Файл с результатом и краткое пояснение к нему",
        }
    if form == FAForm.PROJECT_DEFENSE:
        draft["expected_result"] = "Готовый проект и короткая презентация с демонстрацией работы"
        draft["example_task"] = {
            "title": "Пример темы проекта",
            "environment": "Учебная среда программы",
            "description": "Небольшой законченный проект по темам программы",
            "deliverable": "Проект и презентация",
        }
    if form == FAForm.REPORT_DEFENSE:
        draft["report_requirements"] = [
            "Титульный лист",
            "Введение: цель и задачи стажировки",
            "Описание места стажировки и выполненных работ",
            "Результаты по каждому модулю программы",
            "Выводы и предложения",
            "Отзыв руководителя стажировки",
        ]
    if form == FAForm.PORTFOLIO:
        count = kit.request.portfolio_items_count or 3
        draft["portfolio_items"] = [
            {
                "title": f"Материал портфолио {i}",
                "requirements": ["Выполнен самостоятельно", "Оформлен по шаблону"],
            }
            for i in range(1, count + 1)
        ]
    if kit.request.constraints.anonymized_data:
        draft["data_requirements"] = ["Данные обезличены", "Объём данных достаточен для анализа"]
        draft["expected_result"] = "Отчёт с выводами и визуализацией"
    return draft


def literature(kit: Kit) -> dict:
    items = bibliography.shortlist(kit, default_registry())
    books = [i for i in items if not i.is_doc]
    docs = [i for i in items if i.is_doc]
    selected = [{"catalog_id": i.key, "section": "main"} for i in books[:3]]
    selected += [{"catalog_id": i.key, "section": "additional"} for i in books[3:6]]
    selected += [{"catalog_id": i.key, "section": "internet"} for i in docs[:3]]
    candidates = [
        {  # выдуманное издание: ISBN не проходит контрольную сумму — код обязан его отбросить
            "authors": ["Иванов И. И."],
            "title": "Полное руководство по всему",
            "year": 2023,
            "publisher": "Питер",
            "isbn": "978-5-4461-0000-1",
            "section": "additional",
        }
    ]
    return {"selected": selected, "candidates": candidates}


def judge(kit: Kit) -> dict:
    return {
        "results": [
            {
                "criterion": cid,
                "passed": True,
                "reason": "Соответствует: формулировки конкретны и опираются на темы программы.",
            }
            for cid in content.JUDGE_TEXTS
        ]
    }


def _content_items(topic: Topic, kind: WorkKind, n: int) -> list[str]:
    title = topic.title
    by_kind = {
        WorkKind.LK: [
            f"Основные понятия темы «{title}»",
            f"Как устроено: {title.lower()}",
            f"Типичные ошибки и как их избежать: {title.lower()}",
        ],
        WorkKind.PR: [
            f"Разбор примера по теме «{title}»",
            f"Выполнение задания по образцу: {title.lower()}",
            f"Самостоятельное решение задачи: {title.lower()}",
        ],
        WorkKind.LB: [
            f"Подготовка стенда для работы «{title}»",
            f"Выполнение лабораторной работы: {title.lower()}",
            f"Оформление результатов: {title.lower()}",
        ],
        WorkKind.SRS: [
            f"Изучение материалов по теме «{title}»",
            f"Подготовка к обсуждению: {title.lower()}",
            f"Выполнение домашнего задания: {title.lower()}",
        ],
    }
    return by_kind[kind][:n]


def module_content(kit: Kit, module: Module) -> dict:
    topics = []
    for topic in kit.topics_of(module.id):
        entry = {"topic_id": topic.id, "lk": [], "pr": [], "lb": [], "srs": [], "control_form": "Опрос"}
        for kind in prompts.work_kinds(topic):
            entry[kind.value] = _content_items(topic, kind, 3)
        topics.append(entry)
    return {"topics": topics}


def lectures(kit: Kit, module: Module) -> dict:
    result = []
    for topic in apply.lecture_topics(kit, module):
        los = topic.lo_ids or [kit.outcomes[0].id]
        result.append(
            {
                "topic_ids": [topic.id],
                "title": topic.title,
                "goal": f"Разобраться в теме «{topic.title}» и подготовиться к практике.",
                "plan": [
                    {
                        "title": f"{part} ({topic.title})",
                        "theses": [
                            f"{part}: главное о теме «{topic.title}»",
                            f"{part}: пример из сферы «{_domain(kit)}»",
                            f"{part}: что важно запомнить",
                        ],
                        "lo_ids": los,
                    }
                    for part in ("Введение в тему", "Основные приёмы", "Применение на практике")
                ],
                "glossary": [
                    {
                        "term": f"Понятие {i} темы «{topic.title}»",
                        "definition": "Ключевое понятие, которое объясняется на лекции простыми словами.",
                    }
                    for i in (1, 2, 3)
                ],
            }
        )
    return {"lectures": result}


def seminars(kit: Kit, module: Module) -> dict:
    result = []
    for n, topic in enumerate(apply.practice_topics(kit, module)):
        word = _domain_word(kit, n)
        result.append(
            {
                "topic_id": topic.id,
                "lo_ids": topic.lo_ids,
                "kind": "lab" if topic.hours.lb > topic.hours.pr else "practice",
                "title": f"Практическая работа: {topic.title}",
                "situation": f"В сфере «{_domain(kit)}» возникла рабочая задача: {word} — нужно применить приёмы темы «{topic.title}».",
                "task": f"Выполнить задание по теме «{topic.title}» на учебных данных.",
                "steps": [
                    "Изучить исходные данные",
                    "Выполнить задание по шагам",
                    "Проверить результат",
                    "Оформить вывод",
                ],
                "deliverable": "Файл с результатом и краткий вывод",
                "criteria": [
                    {"text": "Задание выполнено полностью", "max_points": 4},
                    {"text": "Результат проверен и корректен", "max_points": 3},
                    {"text": "Вывод обоснован", "max_points": 3},
                ],
                "pass_threshold": 6,
            }
        )
    return {"seminars": result}


def _question(topic_id: str, lo: str, text: str, n: int) -> dict:
    options = [f"Утверждение {letter} к вопросу {n + 1} ({lo})" for letter in "АБВГ"]
    return {
        "topic_id": topic_id,
        "lo_ids": [lo],
        "kind": "single",
        "stem": f"Вопрос {n + 1} по результату «{text[:60]}»: какое утверждение верно?",
        "options": options,
        "correct_answers": [options[n % 4]],
        "explanation": "Верный вариант следует из материала темы.",
        "difficulty": 1 + n % 3,
    }


def tests(kit: Kit, module: Module, quotas: dict[str, int], interview: int) -> dict:
    texts = {o.id: o.text for o in kit.outcomes}
    topic_of = {lo: t.id for t in kit.topics_of(module.id) for lo in t.lo_ids}
    questions = []
    counter = 0
    for lo, count in quotas.items():
        for _ in range(count):
            questions.append(
                _question(topic_of.get(lo, kit.topics_of(module.id)[0].id), lo, texts.get(lo, ""), counter)
            )
            counter += 1
    los = prompts.module_outcome_ids(kit, module)
    iqs = [
        {
            "module_id": module.id,
            "lo_ids": [los[i % len(los)]] if los else [],
            "text": f"Расскажите, как вы применяли на практике материал модуля «{module.title}» (вопрос {i + 1}).",
            "answer_points": ["Пример из работы", "Какие приёмы применены", "Какой получен результат"],
            "criteria": ["Ответ опирается на собственный опыт стажировки"],
        }
        for i in range(interview)
    ]
    return {"questions": questions, "interview": iqs}


def module_pack(kit: Kit, module: Module, quotas: dict[str, int], interview: int) -> dict:
    pack = module_content(kit, module)
    pack |= lectures(kit, module)
    pack |= seminars(kit, module)
    pack |= tests(kit, module, quotas, interview)
    return pack


# --------------------------------------------------------------------------------------------------
# Запись
# --------------------------------------------------------------------------------------------------


def write(step: str, key: str, data: dict) -> None:
    folder = FOLDER / step
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{key}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    for name, query in QUERIES.items():
        kit = build_kit(query)
        key = request_key(kit.request.raw_text)
        write("program_texts", key, program_texts(kit))
        write("final_attestation", key, final_attestation(kit))
        write("literature", key, literature(kit))
        write("judge", key, judge(kit))
        quotas = content.question_quotas(kit)
        for module in kit.modules_sorted():
            mkey = f"{key}-{module.id}"
            mquotas = quotas.get(module.id, {})
            interview = content.interview_quota(kit, module)
            write("module_content", mkey, module_content(kit, module))
            if apply.lecture_topics(kit, module):
                write("lectures", mkey, lectures(kit, module))
            if apply.practice_topics(kit, module):
                write("seminars", mkey, seminars(kit, module))
            if mquotas or interview:
                write("tests", mkey, tests(kit, module, mquotas, interview))
            write("module_pack", mkey, module_pack(kit, module, mquotas, interview))
        numbers = display_numbers(kit)
        print(
            f"{name}: {key}, модулей {len(kit.modules)} ({', '.join(numbers[m.id] for m in kit.modules_sorted())})"
        )
    # автодобор: вопрос без своей фикстуры — код привяжет его к РО, которому не хватает оценочных средств
    write(
        "refill",
        "default",
        {
            "questions": [_question("", "", "результат обучения", n) for n in range(4)],
            "interview": [
                {
                    "module_id": "",
                    "lo_ids": [],
                    "text": f"Как вы применили на практике материал модуля (дополнительный вопрос {n + 1})?",
                    "answer_points": ["Пример из работы", "Полученный результат"],
                    "criteria": ["Ответ опирается на собственный опыт"],
                }
                for n in range(2)
            ],
        },
    )


if __name__ == "__main__":
    main()
