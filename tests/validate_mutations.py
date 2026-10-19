"""Порчи эталонных комплектов: по одной на проверку. Каждая должна давать замечание своей проверки."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from app.core.validate import ValidationContext
from app.models import (
    HoursRow,
    Kit,
    Module,
    OutcomeKind,
    ParsedReference,
    ProgramType,
    TFRef,
    TopicRequirement,
    WorkKind,
)
from tests.validate_kits import make_dop_kit, make_internship_kit, make_pk_kit

FIXTURES = Path(__file__).parent / "fixtures" / "kits"

Mutation = Callable[[Kit, ValidationContext], None]


def load_reference(name: str) -> ParsedReference:
    return ParsedReference.model_validate(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def _topic(kit: Kit, topic_id: str):  # noqa: ANN202
    return next(t for t in kit.topics if t.id == topic_id)


def m_v01(kit: Kit, ctx: ValidationContext) -> None:  # ДОП: ЛБ в строке плана
    _topic(kit, "T1.1").hours = HoursRow(lk=0.5, pr=0.5, lb=1)


def m_v02(kit: Kit, ctx: ValidationContext) -> None:  # модуль без тем
    kit.modules.append(Module(id="M3", order=3, title="Пустой модуль"))


def m_v03(kit: Kit, ctx: ValidationContext) -> None:  # итог ≠ заявленному объёму
    _topic(kit, "T1.2").hours = HoursRow(lk=4, pr=4, srs=6, distance=4)


def m_v04(kit: Kit, ctx: ValidationContext) -> None:  # часы ПР есть, содержания ПР нет
    _topic(kit, "T1.1").content.pop(WorkKind.PR)


def m_v05(kit: Kit, ctx: ValidationContext) -> None:  # число часов в тексте ≠ колонке
    _topic(kit, "T1.1").content[WorkKind.PR] = ["ПР (9 часов): составление перечня источников"]


def m_v06(kit: Kit, ctx: ValidationContext) -> None:  # шаг не 0,5
    _topic(kit, "T1.2").hours = HoursRow(lk=4.25, pr=4, srs=1.75, distance=4)


def m_v07(kit: Kit, ctx: ValidationContext) -> None:  # раздел «Объём» расходится с планом
    kit.texts.form_description += " Объём программы — 40 часов, из них контактная работа — 10 часов."


def m_v08(kit: Kit, ctx: ValidationContext) -> None:  # стажировка с практикой
    _topic(kit, "T1.1").hours = HoursRow(pr=2, srs=28)


def m_v09(kit: Kit, ctx: ValidationContext) -> None:  # доля практики ниже требуемой
    _topic(kit, "T2.1").hours = HoursRow(lk=8, pr=2, distance=4)


def m_v10(kit: Kit, ctx: ValidationContext) -> None:  # ДОТ заявлены, разбивки нет
    for topic in kit.topics:
        topic.hours = topic.hours.model_copy(update={"distance": 0})


def m_v11(kit: Kit, ctx: ValidationContext) -> None:  # календарь не сходится с планом
    assert kit.calendar is not None
    kit.calendar.days = kit.calendar.days[:-1]


def m_v12(kit: Kit, ctx: ValidationContext) -> None:  # пустой обязательный раздел
    kit.texts.annotation = ""


def m_v13(kit: Kit, ctx: ValidationContext) -> None:  # тип программы ≠ запросу
    kit.request.program_type = ProgramType.DOP


def m_v14(kit: Kit, ctx: ValidationContext) -> None:  # ссылка на несуществующую тему
    kit.seminars[0].topic_id = "T9.9"


def m_v15(kit: Kit, ctx: ValidationContext) -> None:  # РО не раскрыт ни в одной теме
    _topic(kit, "T2.2").lo_ids = ["LO2.2"]


def m_v16(kit: Kit, ctx: ValidationContext) -> None:  # у РО меньше двух оценочных средств
    kit.questions = [q for q in kit.questions if "LO2.3" not in q.lo_ids]
    kit.tests[0].question_ids = [q.id for q in kit.questions]
    kit.seminars[3].lo_ids = ["LO2.2"]
    assert kit.final_attestation is not None
    kit.final_attestation.lo_ids = [x for x in kit.final_attestation.lo_ids if x != "LO2.3"]


def m_v17(kit: Kit, ctx: ValidationContext) -> None:  # у компетенции нет «владеть»
    next(o for o in kit.outcomes if o.id == "LO2.3").kind = OutcomeKind.CAN


def m_v18(kit: Kit, ctx: ValidationContext) -> None:  # ключ вне вариантов
    kit.questions[0].correct = [7]


def m_v19(kit: Kit, ctx: ValidationContext) -> None:  # один критерий у задания
    kit.seminars[0].criteria = kit.seminars[0].criteria[:1]


def m_v20(kit: Kit, ctx: ValidationContext) -> None:  # нет слайдов у лекции запрошенного модуля
    kit.lectures[2].slides = None


def m_v21(kit: Kit, ctx: ValidationContext) -> None:  # у ИА нет критериев
    assert kit.final_attestation is not None
    kit.final_attestation.criteria = []


def m_v22(kit: Kit, ctx: ValidationContext) -> None:  # стажировка: по модулю нет вопросов собеседования
    kit.interview_questions = [q for q in kit.interview_questions if q.module_id != "M2"]


def m_v23(kit: Kit, ctx: ValidationContext) -> None:  # выдуманный ISBN
    book = next(s for s in kit.sources if s.isbn)
    book.isbn = "978-5-00000-000-0"


def m_v24(kit: Kit, ctx: ValidationContext) -> None:  # утративший силу приказ вместо действующего
    kit.program.norm_ids = ["fz273", "minobrnauki499", "minobrnauki816"]


def m_v25(kit: Kit, ctx: ValidationContext) -> None:  # ТФ, которой нет в реестре
    kit.competencies[0].tf_refs = [TFRef(ps_code="06.015", tf_code="C/99.9")]


def m_v26(kit: Kit, ctx: ValidationContext) -> None:  # ДОП с профстандартом
    kit.program.profstandards = ["06.015"]


def m_v27(kit: Kit, ctx: ValidationContext) -> None:  # номер приказа, которого нет в реестре
    kit.texts.org_process += " Порядок занятий установлен приказом Минобрнауки России № 9876."


def m_v28(kit: Kit, ctx: ValidationContext) -> None:  # сервис, недоступный в РФ
    kit.seminars[0].task += " Сравнить с ответом ChatGPT."


def m_v29(kit: Kit, ctx: ValidationContext) -> None:  # устаревшая версия инструмента
    kit.texts.software = ["Python 3.12 и библиотека pandas 1.3", "Среда Jupyter для выполнения заданий"]


def m_v30(kit: Kit, ctx: ValidationContext) -> None:  # ФИО в тексте
    kit.texts.staffing_requirements += " Руководитель программы — Иванов Иван Петрович."


def m_v32(kit: Kit, ctx: ValidationContext) -> None:  # текст не на русском
    kit.texts.annotation = (
        "This program teaches bank specialists how to prepare and analyse data for daily reporting tasks "
        "with practical exercises and a final project defended in front of a commission."
    )


def m_v33(kit: Kit, ctx: ValidationContext) -> None:  # нет обязательной темы из запроса
    kit.request.required_topics.append(TopicRequirement(label="статистика", patterns=["статистик"]))


def m_v34(kit: Kit, ctx: ValidationContext) -> None:  # нужен вводный блок, а его нет
    kit.request.constraints.intro_block_topic = "Python"


def m_v35(kit: Kit, ctx: ValidationContext) -> None:  # ДОП: сложная тема раньше базовых
    titles = {
        "T1.1": "Классы и объекты",
        "T1.2": "Переменные и типы",
        "T1.3": "Условия и ветвления",
        "T2.1": "Циклы",
    }
    for topic_id, title in titles.items():
        _topic(kit, topic_id).title = title


def m_v36(kit: Kit, ctx: ValidationContext) -> None:  # задания не из среды слушателей
    for seminar in kit.seminars:
        seminar.title = "Учебное задание по теме занятия"
        seminar.situation = "Нужно выполнить упражнение по теме занятия."
        seminar.task = "Выполнить упражнение и оформить результат."


def m_v37(kit: Kit, ctx: ValidationContext) -> None:  # школьники, а в программе «от 18 лет»
    kit.texts.audience_requirements = (
        "Программа предназначена для взрослых от 18 лет без требований к образованию."
    )


def m_v38(kit: Kit, ctx: ValidationContext) -> None:  # запрет на программирование нарушен
    kit.request.constraints.no_programming = True
    kit.texts.audience_requirements += " Слушатели должны уметь программировать на Python."


def m_v39(kit: Kit, ctx: ValidationContext) -> None:  # персональные данные в задании
    kit.seminars[0].situation += " Клиент: карта 4276 1234 5678 9012, телефон +7 (928) 123-45-67."


def m_v40(kit: Kit, ctx: ValidationContext) -> None:  # обещание сверх меры
    kit.program.goal = (
        "Полностью подготовить слушателей к самостоятельной работе аналитиком и трудоустройству."
    )


def m_v41(kit: Kit, ctx: ValidationContext) -> None:  # термин без пояснения
    kit.lectures[0].glossary[0].definition = ""


def m_v42(kit: Kit, ctx: ValidationContext) -> None:  # текст скопирован из референса
    ctx.reference_texts = [
        kit.texts.annotation + " " + kit.texts.org_process + " " + kit.texts.form_description
    ]


def m_v43(kit: Kit, ctx: ValidationContext) -> None:  # дубль темы
    _topic(kit, "T1.2").title = _topic(kit, "T1.1").title


def m_v44(kit: Kit, ctx: ValidationContext) -> None:  # «битое» слово и чужое название
    _topic(kit, "T1.1").content[WorkKind.LK].append("Сбор т85ребований к отчёту")


def m_v45(kit: Kit, ctx: ValidationContext) -> None:  # в приложенном образце ошибки
    kit.request.reference = load_reference("bsa_248_sample.json")


def m_v46(kit: Kit, ctx: ValidationContext) -> None:  # ожидание трудоустройства без предупреждения
    kit.request.raw_text += " Нужно, чтобы после курса слушатели могли пойти работать в эту сферу."


MUTATIONS: dict[str, tuple[Callable[[], Kit], Mutation]] = {
    "V01": (make_dop_kit, m_v01),
    "V02": (make_pk_kit, m_v02),
    "V03": (make_pk_kit, m_v03),
    "V04": (make_pk_kit, m_v04),
    "V05": (make_pk_kit, m_v05),
    "V06": (make_pk_kit, m_v06),
    "V07": (make_pk_kit, m_v07),
    "V08": (make_internship_kit, m_v08),
    "V09": (make_pk_kit, m_v09),
    "V10": (make_pk_kit, m_v10),
    "V11": (make_dop_kit, m_v11),
    "V12": (make_pk_kit, m_v12),
    "V13": (make_pk_kit, m_v13),
    "V14": (make_pk_kit, m_v14),
    "V15": (make_pk_kit, m_v15),
    "V16": (make_pk_kit, m_v16),
    "V17": (make_pk_kit, m_v17),
    "V18": (make_pk_kit, m_v18),
    "V19": (make_pk_kit, m_v19),
    "V20": (make_pk_kit, m_v20),
    "V21": (make_pk_kit, m_v21),
    "V22": (make_internship_kit, m_v22),
    "V23": (make_pk_kit, m_v23),
    "V24": (make_pk_kit, m_v24),
    "V25": (make_pk_kit, m_v25),
    "V26": (make_dop_kit, m_v26),
    "V27": (make_pk_kit, m_v27),
    "V28": (make_pk_kit, m_v28),
    "V29": (make_pk_kit, m_v29),
    "V30": (make_pk_kit, m_v30),
    "V32": (make_pk_kit, m_v32),
    "V33": (make_pk_kit, m_v33),
    "V34": (make_pk_kit, m_v34),
    "V35": (make_dop_kit, m_v35),
    "V36": (make_pk_kit, m_v36),
    "V37": (make_dop_kit, m_v37),
    "V38": (make_pk_kit, m_v38),
    "V39": (make_pk_kit, m_v39),
    "V40": (make_pk_kit, m_v40),
    "V41": (make_dop_kit, m_v41),
    "V42": (make_pk_kit, m_v42),
    "V43": (make_pk_kit, m_v43),
    "V44": (make_pk_kit, m_v44),
    "V45": (make_internship_kit, m_v45),
    "V46": (make_pk_kit, m_v46),
}
