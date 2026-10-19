"""Построители заведомо валидных комплектов для тестов validate.py.

Каждый комплект проходит все проверки V01–V46 (кроме V31) без error и warning с реестром make_registry().
Тесты портят по одному аспекту и ждут замечание конкретной проверки.
"""

from __future__ import annotations

from app.core.calendar import build_calendar
from app.core.hours import constraints_from_request
from app.core.registry import Registry
from app.core.registry_api import (
    AIServiceRecord,
    LaborFunctionRecord,
    LiteratureRecord,
    NormRecord,
    ProfStandardRecord,
    PublisherRecord,
    ToolRecord,
)
from app.models import (
    Audience,
    Competency,
    Constraints,
    Criterion,
    FAForm,
    FinalAttestation,
    GlossaryItem,
    HoursRow,
    InterviewQuestion,
    Kit,
    LearningOutcome,
    Lecture,
    Module,
    ModuleKind,
    OutcomeKind,
    Outputs,
    PlanItem,
    Program,
    ProgramTexts,
    ProgramType,
    RequestSpec,
    ScheduleParams,
    Seminar,
    Slide,
    SlidesScope,
    Source,
    SourceKind,
    SourceSection,
    StudyForm,
    StudyFormKind,
    Test,
    TestQuestion,
    TestScope,
    TFRef,
    Topic,
    TopicRequirement,
    Verification,
    WorkKind,
)

URL = "https://example.org/source"


# --------------------------------------------------------------------------------------------------
# Реестр
# --------------------------------------------------------------------------------------------------


def make_registry() -> Registry:
    def norm(**kw: object) -> NormRecord:
        return NormRecord(status="verified", source_url=URL, checked_at="2026-10-14", **kw)  # type: ignore[arg-type]

    return Registry(
        norms=[
            norm(
                id="fz273",
                kind="federal_law",
                number="273-ФЗ",
                date="2012-12-29",
                title="Федеральный закон от 29.12.2012 № 273-ФЗ «Об образовании в Российской Федерации»",
                applies_to=["pk", "dop"],
            ),
            norm(
                id="minobrnauki266",
                kind="order",
                number="266",
                date="2025-03-24",
                title="Приказ Минобрнауки России от 24.03.2025 № 266 «Об утверждении Порядка организации и "
                "осуществления образовательной деятельности по дополнительным профессиональным программам»",
                applies_to=["pk"],
                in_force_since="2025-09-01",
                replaces="minobrnauki499",
            ),
            norm(
                id="minobrnauki499",
                kind="order",
                number="499",
                date="2013-07-01",
                title="Приказ Минобрнауки России от 01.07.2013 № 499",
                applies_to=["pk"],
                in_force=False,
                replaced_by="minobrnauki266",
            ),
            norm(
                id="minpros629",
                kind="order",
                number="629",
                date="2022-07-27",
                title="Приказ Минпросвещения России от 27.07.2022 № 629 «Об утверждении Порядка организации и "
                "осуществления образовательной деятельности по дополнительным общеобразовательным программам»",
                applies_to=["dop"],
            ),
            norm(
                id="minobrnauki816",
                kind="order",
                number="816",
                date="2017-08-23",
                title="Приказ Минобрнауки России от 23.08.2017 № 816 «Об утверждении Порядка применения "
                "организациями, осуществляющими образовательную деятельность, электронного обучения, "
                "дистанционных образовательных технологий при реализации образовательных программ»",
                applies_to=["pk", "dop"],
            ),
        ],
        profstandards=[
            ProfStandardRecord(
                code="06.015",
                title="Специалист по информационным системам",
                order="Приказ Минтруда России от 18.11.2014 № 896н",
                status="verified",
                source_url=URL,
                labor_functions=[
                    LaborFunctionRecord(otf="C", code="C/11.6", title="Выявление требований к ИС"),
                    LaborFunctionRecord(otf="C", code="C/12.6", title="Анализ требований"),
                ],
                tags=["data", "analysis"],
            )
        ],
        ai_services=[
            AIServiceRecord(
                id="gigachat", name="GigaChat", aliases=["ГигаЧат"], status="verified", source_url=URL
            ),
            AIServiceRecord(
                id="chatgpt", name="ChatGPT", aliases=["Чат ГПТ"], available_in_ru=False, status="verified"
            ),
        ],
        literature=[
            LiteratureRecord(
                id="lit_pandas",
                authors=["Маккини У."],
                title="Python и анализ данных",
                year=2023,
                publisher="ДМК Пресс",
                isbn="978-5-93700-174-0",
                status="verified",
                source_url=URL,
                tags=["data"],
            ),
            LiteratureRecord(
                id="lit_sql",
                authors=["Молинаро Э."],
                title="SQL. Сборник рецептов",
                year=2022,
                publisher="Питер",
                status="verified",
                source_url=URL,
                tags=["data"],
            ),
        ],
        publishers=[
            PublisherRecord(id="dmk", name="ДМК Пресс", status="verified", source_url=URL),
            PublisherRecord(id="piter", name="Питер", status="verified", source_url=URL),
        ],
        tools=[
            ToolRecord(
                id="python",
                name="Python",
                docs_url="https://docs.python.org/3/",
                min_version="3.10",
                status="verified",
                source_url=URL,
            ),
            ToolRecord(
                id="pandas",
                name="pandas",
                docs_url="https://pandas.pydata.org/docs/",
                min_version="2.0",
                status="verified",
                source_url=URL,
            ),
        ],
    )


# --------------------------------------------------------------------------------------------------
# Общие части
# --------------------------------------------------------------------------------------------------


def _norm_sources(norm_ids: list[str]) -> list[Source]:
    registry = make_registry()
    result = []
    for i, norm_id in enumerate(norm_ids, start=1):
        record = registry.norm(norm_id)
        assert record is not None
        result.append(
            Source(
                id=f"SRC{i}",
                kind=SourceKind.NORM,
                section=SourceSection.NORMATIVE,
                registry_id=norm_id,
                verification=Verification.REGISTRY,
                title=record.title,
            )
        )
    return result


def _literature_sources(start: int) -> list[Source]:
    return [
        Source(
            id=f"SRC{start}",
            kind=SourceKind.BOOK,
            section=SourceSection.MAIN,
            registry_id="lit_pandas",
            verification=Verification.CATALOG,
            authors=["Маккини У."],
            title="Python и анализ данных",
            year=2023,
            publisher="ДМК Пресс",
            isbn="978-5-93700-174-0",
        ),
        Source(
            id=f"SRC{start + 1}",
            kind=SourceKind.BOOK,
            section=SourceSection.ADDITIONAL,
            registry_id="lit_sql",
            verification=Verification.CATALOG,
            authors=["Молинаро Э."],
            title="SQL. Сборник рецептов",
            year=2022,
            publisher="Питер",
        ),
        Source(
            id=f"SRC{start + 2}",
            kind=SourceKind.DOC,
            section=SourceSection.INTERNET,
            registry_id="pandas",
            verification=Verification.CATALOG,
            title="Документация библиотеки pandas",
            url="https://pandas.pydata.org/docs/",
        ),
        Source(
            id=f"SRC{start + 3}",
            kind=SourceKind.SOFTWARE,
            section=SourceSection.SOFTWARE,
            registry_id="python",
            verification=Verification.CATALOG,
            title="Интерпретатор Python и среда Jupyter",
            url="https://docs.python.org/3/",
        ),
    ]


def _texts(dop: bool = False) -> ProgramTexts:
    return ProgramTexts(
        annotation="Программа знакомит слушателей с подготовкой и анализом данных для ежедневных задач отделения "
        "банка: от выгрузки таблиц до наглядного отчёта для руководителя.",
        relevance="Анализ данных нужен в любой профессии: умение работать с таблицами и делать выводы помогает "
        "учиться и решать практические задачи."
        if dop
        else "",
        form_description="Обучение очно-заочное: лекции проходят дистанционно, практические занятия — в учебном "
        "классе, итоговая аттестация — очно.",
        audience_requirements="Слушатели имеют среднее профессиональное или высшее образование и опыт работы "
        "с электронными таблицами.",
        mto=["Компьютерный класс с доступом в интернет", "Проектор и экран для демонстрации"],
        software=["Python 3.12 и библиотека pandas 2.2", "Среда Jupyter для выполнения заданий"],
        org_process="Занятия проводятся по расписанию, задания выполняются на обезличенных учебных данных, "
        "преподаватель проверяет каждую работу и даёт обратную связь.",
        staffing_requirements="Преподаватели имеют высшее образование и опыт практической работы с данными "
        "не менее трёх лет.",
        methodical="Для занятий подготовлены учебные наборы данных, пошаговые инструкции и примеры решений. "
        "Консультации проводятся после каждого модуля."
        if dop
        else "",
        satisfaction="В конце обучения слушатели заполняют анкету: оценивают полезность тем, понятность "
        "материалов и организацию занятий."
        if not dop
        else "",
    )


def _criteria(owner: str) -> list[Criterion]:
    return [
        Criterion(id=f"{owner}.K1", text="Задание выполнено полностью и в срок", max_points=4),
        Criterion(id=f"{owner}.K2", text="Расчёты верны и проверены на контрольном примере", max_points=4),
        Criterion(id=f"{owner}.K3", text="Выводы обоснованы и понятны заказчику", max_points=2),
    ]


def _finish(kit: Kit) -> Kit:
    """Достраивает календарь солвером календаря по параметрам запроса."""
    constraints = constraints_from_request(kit.request, kit.program.type)
    calendar, warnings = build_calendar(kit, constraints)
    assert not warnings, [w.text for w in warnings]
    kit.calendar = calendar
    return kit


# --------------------------------------------------------------------------------------------------
# ПК, очно-заочно с ДОТ, 36 ч
# --------------------------------------------------------------------------------------------------


def make_pk_kit() -> Kit:
    audience = Audience(
        category="специалисты региональных отделений банка",
        entry_level="уверенно работают в электронных таблицах",
        requirements="Лица, имеющие среднее профессиональное или высшее образование",
        domain="банк",
        domain_patterns=["банк", "клиент", "кредит", "отделени"],
    )
    form = StudyForm(kind=StudyFormKind.OCHNO_ZAOCHNAYA, dot=True, eo=True, distance_share=0.5)
    request = RequestSpec(
        raw_text="Программа повышения квалификации «Анализ банковских данных» на 36 часов для специалистов "
        "отделений банка, очно-заочно с применением дистанционных технологий, срок 2 недели, итоговая "
        "аттестация — защита проекта.",
        program_type=ProgramType.PK,
        title="Анализ банковских данных",
        total_hours=36,
        audience=audience,
        form=form,
        weeks=2,
        fa_form=FAForm.PROJECT_DEFENSE,
        fa_hours=2,
        required_topics=[TopicRequirement(label="SQL", patterns=[r"\bSQL\b"])],
        constraints=Constraints(min_practice_share=0.5, anonymized_data=True),
        outputs=Outputs(slides_scope=SlidesScope(mode="module", module_pattern="SQL", module_ids=["M2"])),
    )
    program = Program(
        type=ProgramType.PK,
        title="Анализ банковских данных",
        audience=audience,
        form=form,
        total_hours=36,
        schedule=ScheduleParams(weeks_target=2, hours_per_day_max=6),
        goal="Совершенствование компетенций, необходимых для выявления и анализа требований к отчётности "
        "отделения банка на основе данных.",
        profstandards=["06.015"],
        norm_ids=["fz273", "minobrnauki266", "minobrnauki816"],
        slides_scope=SlidesScope(mode="module", module_pattern="SQL", module_ids=["M2"]),
    )
    competencies = [
        Competency(
            id="C1",
            activity="Анализ данных подразделения",
            title="Выявлять требования к отчётности по данным клиентов",
            tf_refs=[TFRef(ps_code="06.015", tf_code="C/11.6")],
        ),
        Competency(
            id="C2",
            activity="Анализ данных подразделения",
            title="Анализировать данные и готовить выводы для руководителя",
            tf_refs=[TFRef(ps_code="06.015", tf_code="C/12.6")],
        ),
    ]
    outcomes = [
        LearningOutcome(
            id="LO1.1",
            competency_id="C1",
            kind=OutcomeKind.KNOW,
            text="Знать источники данных отделения банка",
            tf_code="C/11.6",
        ),
        LearningOutcome(
            id="LO1.2",
            competency_id="C1",
            kind=OutcomeKind.CAN,
            text="Уметь формулировать требования к отчёту",
        ),
        LearningOutcome(
            id="LO1.3",
            competency_id="C1",
            kind=OutcomeKind.MASTER,
            text="Владеть приёмами описания показателей отчёта",
        ),
        LearningOutcome(
            id="LO2.1", competency_id="C2", kind=OutcomeKind.KNOW, text="Знать основные операции языка SQL"
        ),
        LearningOutcome(
            id="LO2.2", competency_id="C2", kind=OutcomeKind.CAN, text="Уметь выгружать данные запросом SQL"
        ),
        LearningOutcome(
            id="LO2.3",
            competency_id="C2",
            kind=OutcomeKind.MASTER,
            text="Владеть подготовкой выводов по результатам выгрузки",
        ),
    ]
    modules = [
        Module(id="M1", order=1, title="Требования к отчётности", control_form="Тестирование"),
        Module(
            id="M2",
            order=2,
            title="Выгрузка данных на SQL",
            control_form="Практическая работа",
            needs_lectures=True,
        ),
    ]
    topics = [
        Topic(
            id="T1.1",
            module_id="M1",
            order=1,
            title="Источники данных отделения банка",
            lo_ids=["LO1.1", "LO1.2"],
            hours=HoursRow(lk=4, pr=4, srs=0, distance=4),
            content={
                WorkKind.LK: ["Какие данные о клиентах и операциях есть в отделении", "Качество данных"],
                WorkKind.PR: ["Составление перечня источников для отчёта по кредитам"],
            },
            tags=["data"],
        ),
        Topic(
            id="T1.2",
            module_id="M1",
            order=2,
            title="Описание показателей отчёта",
            lo_ids=["LO1.2", "LO1.3"],
            hours=HoursRow(lk=4, pr=4, srs=2, distance=4),
            content={
                WorkKind.LK: ["Показатели отчёта и их определения"],
                WorkKind.PR: ["Описание показателей отчёта о просроченных кредитах"],
                WorkKind.SRS: ["Доработка описания показателей"],
            },
            tags=["data"],
        ),
        Topic(
            id="T2.1",
            module_id="M2",
            order=1,
            title="Запросы SQL для выгрузки данных",
            lo_ids=["LO2.1", "LO2.2"],
            hours=HoursRow(lk=4, pr=6, srs=0, distance=4),
            content={
                WorkKind.LK: ["Выборка, фильтрация и сортировка", "Объединение таблиц"],
                WorkKind.PR: ["Выгрузка операций клиентов отделения банка"],
            },
            tags=["sql", "data"],
        ),
        Topic(
            id="T2.2",
            module_id="M2",
            order=2,
            title="Выводы по выгрузке для руководителя",
            lo_ids=["LO2.2", "LO2.3"],
            hours=HoursRow(lk=2, pr=4, srs=0, distance=2),
            content={
                WorkKind.LK: ["Как превратить таблицу в вывод"],
                WorkKind.PR: ["Подготовка вывода по кредитному портфелю отделения"],
            },
            tags=["data"],
        ),
    ]
    lectures = [
        _lecture("L1.1", "M1", "T1.1", "Источники данных отделения банка", ["LO1.1"], slides=False),
        _lecture("L1.2", "M1", "T1.2", "Описание показателей отчёта", ["LO1.2"], slides=False),
        _lecture("L2.1", "M2", "T2.1", "Запросы SQL для выгрузки данных", ["LO2.1"], slides=True),
        _lecture("L2.2", "M2", "T2.2", "Выводы по выгрузке для руководителя", ["LO2.3"], slides=True),
    ]
    seminars = [
        _seminar(
            "S1",
            "T1.1",
            ["LO1.1", "LO1.2"],
            "Перечень источников данных для отчёта по кредитам клиентов банка",
        ),
        _seminar(
            "S2", "T1.2", ["LO1.3"], "Описание показателей отчёта о просрочке кредитов в отделении банка"
        ),
        _seminar("S3", "T2.1", ["LO2.1", "LO2.2"], "Выгрузка операций клиентов отделения банка запросом SQL"),
        _seminar("S4", "T2.2", ["LO2.3"], "Вывод для руководителя отделения банка по кредитному портфелю"),
    ]
    questions = _questions(
        "TS1",
        [
            ("T1.1", ["LO1.1"]),
            ("T1.2", ["LO1.2"]),
            ("T1.2", ["LO1.3"]),
            ("T2.1", ["LO2.1"]),
            ("T2.1", ["LO2.2"]),
            ("T2.2", ["LO2.3"]),
        ],
    )
    final = FinalAttestation(
        form=FAForm.PROJECT_DEFENSE,
        form_text="Защита итогового проекта на обезличенных данных отделения банка",
        hours=2,
        lo_ids=[o.id for o in outcomes],
        requirements=["Проект выполнен на обезличенных данных", "Отчёт содержит выгрузку, расчёты и выводы"],
        data_requirements=["Данные обезличены: без фамилий, номеров счетов и карт клиентов"],
        expected_result="Отчёт с выгрузкой, таблицей показателей и выводами для руководителя",
        criteria=_criteria("FA"),
        pass_rule="Зачтено при 6 баллах из 10",
        procedure=["Доклад 7 минут", "Ответы на вопросы комиссии"],
    )
    kit = Kit(
        request=request,
        program=program,
        competencies=competencies,
        outcomes=outcomes,
        modules=modules,
        topics=topics,
        lectures=lectures,
        seminars=seminars,
        tests=[
            Test(
                id="TS1",
                title="Итоговый тест",
                scope=TestScope.FINAL,
                pass_threshold=60,
                question_ids=[q.id for q in questions],
            )
        ],
        questions=questions,
        final_attestation=final,
        sources=_norm_sources(program.norm_ids)
        + [
            Source(
                id="SRC4",
                kind=SourceKind.PROFSTANDARD,
                section=SourceSection.NORMATIVE,
                registry_id="06.015",
                verification=Verification.REGISTRY,
                title="Профессиональный стандарт 06.015 «Специалист по информационным системам»",
            )
        ]
        + _literature_sources(5),
        texts=_texts(),
    )
    return _finish(kit)


def _lecture(
    lec_id: str, module_id: str, topic_id: str, title: str, lo_ids: list[str], slides: bool
) -> Lecture:
    plan = [
        PlanItem(
            title="Цель и задачи занятия",
            theses=["О чём занятие", "Что получится в конце", "Как будем работать"],
            lo_ids=lo_ids,
        ),
        PlanItem(
            title="Основные понятия",
            theses=["Таблица и строка", "Показатель и его расчёт", "Единицы измерения"],
            lo_ids=lo_ids,
        ),
        PlanItem(
            title="Разбор примера",
            theses=["Пример из практики", "Типичные ошибки", "Как проверить себя"],
            lo_ids=lo_ids,
        ),
    ]
    return Lecture(
        id=lec_id,
        module_id=module_id,
        topic_ids=[topic_id],
        title=title,
        goal=f"Разобраться в теме «{title}» на примерах из практики слушателей",
        plan=plan,
        glossary=[
            GlossaryItem(term="показатель", definition="число, по которому оценивают результат работы")
        ],
        slides=[Slide(title=title, bullets=["Цель занятия", "План", "Результат"], notes="Вступление")]
        + [Slide(title=item.title, bullets=item.theses, notes="Пояснение к разделу") for item in plan]
        if slides
        else None,
    )


def _seminar(sem_id: str, topic_id: str, lo_ids: list[str], title: str) -> Seminar:
    return Seminar(
        id=sem_id,
        topic_id=topic_id,
        lo_ids=lo_ids,
        title=title,
        situation=f"Руководитель отделения банка просит подготовить материал: {title.lower()}.",
        task="Выполнить задание на учебных обезличенных данных и оформить результат.",
        steps=["Изучить исходные данные", "Выполнить расчёты", "Оформить результат"],
        deliverable="Файл с результатом и кратким пояснением",
        criteria=_criteria(sem_id),
        pass_threshold=6,
    )


def _questions(test_id: str, plan: list[tuple[str, list[str]]], start: int = 1) -> list[TestQuestion]:
    result = []
    for n, (topic_id, lo_ids) in enumerate(plan, start=start):
        result.append(
            TestQuestion(
                id=f"Q{n}",
                test_id=test_id,
                topic_id=topic_id,
                lo_ids=lo_ids,
                stem=f"Какое утверждение о теме занятия верно (вопрос {n})?",
                options=[
                    "Первое верное утверждение",
                    "Неверное утверждение",
                    "Ещё одно неверное",
                    "Совсем не так",
                ],
                correct=[0],
                explanation="Верен первый вариант: он соответствует материалу занятия.",
            )
        )
    return result


# --------------------------------------------------------------------------------------------------
# ПК в форме стажировки, 64 ч
# --------------------------------------------------------------------------------------------------


def make_internship_kit() -> Kit:
    audience = Audience(
        category="начинающие системные администраторы ИТ-компаний",
        entry_level="знают основы операционных систем",
        requirements="Лица, имеющие среднее профессиональное или высшее образование",
        domain="ИТ-компания",
        domain_patterns=["сервер", "компани", "инфраструктур"],
    )
    form = StudyForm(kind=StudyFormKind.OCHNAYA)
    request = RequestSpec(
        raw_text="Программа повышения квалификации в форме стажировки «Сопровождение серверов» на 64 часа, "
        "вся работа самостоятельная под руководством наставника, не больше 4 часов в день, итоговая "
        "аттестация 2 часа — защита отчёта о стажировке.",
        program_type=ProgramType.PK_INTERNSHIP,
        title="Сопровождение серверов",
        total_hours=64,
        audience=audience,
        form=form,
        hours_per_day_max=4,
        fa_form=FAForm.REPORT_DEFENSE,
        fa_hours=2,
        constraints=Constraints(all_srs=True),
    )
    program = Program(
        type=ProgramType.PK_INTERNSHIP,
        title="Сопровождение серверов",
        audience=audience,
        form=form,
        total_hours=64,
        schedule=ScheduleParams(hours_per_day_max=4),
        goal="Совершенствование компетенций, необходимых для выявления и анализа требований к сопровождению "
        "серверов компании.",
        profstandards=["06.015"],
        norm_ids=["fz273", "minobrnauki266"],
    )
    competencies = [
        Competency(
            id="C1",
            activity="Сопровождение инфраструктуры",
            title="Выявлять требования к работе серверов компании",
            tf_refs=[TFRef(ps_code="06.015", tf_code="C/11.6")],
        )
    ]
    outcomes = [
        LearningOutcome(
            id="LO1.1", competency_id="C1", kind=OutcomeKind.KNOW, text="Знать устройство серверов"
        ),
        LearningOutcome(
            id="LO1.2", competency_id="C1", kind=OutcomeKind.CAN, text="Уметь проверять состояние сервера"
        ),
        LearningOutcome(
            id="LO1.3", competency_id="C1", kind=OutcomeKind.MASTER, text="Владеть ведением журнала работ"
        ),
    ]
    modules = [
        Module(id="M1", order=1, title="Устройство серверов", control_form="Собеседование"),
        Module(id="M2", order=2, title="Ежедневное сопровождение", control_form="Собеседование"),
    ]
    topics = [
        Topic(
            id="T1.1",
            module_id="M1",
            order=1,
            title="Аппаратная часть сервера",
            lo_ids=["LO1.1"],
            hours=HoursRow(srs=30),
            content={WorkKind.SRS: ["Изучение состава серверов компании", "Описание конфигурации"]},
        ),
        Topic(
            id="T2.1",
            module_id="M2",
            order=1,
            title="Проверка состояния и журнал работ",
            lo_ids=["LO1.2", "LO1.3"],
            hours=HoursRow(srs=32),
            content={WorkKind.SRS: ["Ежедневная проверка серверов", "Ведение журнала работ"]},
        ),
    ]
    interview = [
        InterviewQuestion(
            id=f"IQ{n}",
            module_id=module_id,
            lo_ids=lo_ids,
            text=text,
            answer_points=["Называет основные элементы", "Приводит пример из стажировки"],
            criteria=["Ответ полный", "Есть пример из практики"],
        )
        for n, (module_id, lo_ids, text) in enumerate(
            [
                ("M1", ["LO1.1"], "Из каких частей состоит сервер вашей компании?"),
                ("M1", ["LO1.1"], "Чем сервер отличается от рабочего компьютера?"),
                ("M1", ["LO1.1"], "Как вы описали конфигурацию сервера?"),
                ("M2", ["LO1.2"], "Как вы проверяете состояние сервера каждый день?"),
                ("M2", ["LO1.3"], "Что вы записываете в журнал работ?"),
                ("M2", ["LO1.2", "LO1.3"], "Какую неисправность вы нашли и как её описали?"),
            ],
            start=1,
        )
    ]
    final = FinalAttestation(
        form=FAForm.REPORT_DEFENSE,
        form_text="Защита отчёта о стажировке",
        hours=2,
        lo_ids=[o.id for o in outcomes],
        requirements=["Отчёт подписан руководителем стажировки"],
        report_requirements=["Описание места стажировки", "Выполненные работы", "Журнал работ", "Выводы"],
        criteria=_criteria("FA"),
        pass_rule="Зачтено при 6 баллах из 10",
        procedure=["Доклад 10 минут", "Ответы на вопросы комиссии"],
    )
    kit = Kit(
        request=request,
        program=program,
        competencies=competencies,
        outcomes=outcomes,
        modules=modules,
        topics=topics,
        interview_questions=interview,
        final_attestation=final,
        sources=_norm_sources(program.norm_ids) + _literature_sources(3),
        texts=_texts(),
    )
    return _finish(kit)


# --------------------------------------------------------------------------------------------------
# ДОП для школьников, 16 ч = 8 занятий × 2 ч
# --------------------------------------------------------------------------------------------------


def make_dop_kit() -> Kit:
    audience = Audience(
        category="школьники 8–9 классов",
        age_or_grade="8–9 классы",
        entry_level="без опыта программирования",
        domain="школа",
        domain_patterns=["игр", "школ", "класс"],
    )
    form = StudyForm(kind=StudyFormKind.OCHNAYA)
    request = RequestSpec(
        raw_text="Дополнительная общеразвивающая программа «Первые шаги в анализе таблиц» для школьников 8 и 9 "
        "классов, 16 часов, занятия очно по 2 часа два раза в неделю, практики не меньше половины, в конце — "
        "защита проекта, итоговый тест на 6 вопросов, слайды для первых двух занятий.",
        program_type=ProgramType.DOP,
        title="Первые шаги в анализе таблиц",
        total_hours=16,
        audience=audience,
        form=form,
        weeks=4,
        session_hours=2,
        sessions_per_week=2,
        fa_form=FAForm.PROJECT_DEFENSE,
        constraints=Constraints(min_practice_share=0.5),
        outputs=Outputs(test_questions=6, slides_scope=SlidesScope(mode="sessions", session_numbers=[1, 2])),
    )
    program = Program(
        type=ProgramType.DOP,
        title="Первые шаги в анализе таблиц",
        audience=audience,
        form=form,
        total_hours=16,
        schedule=ScheduleParams(weeks_target=4, session_hours=2, sessions_per_week=2),
        goal="Познакомить школьников с работой с таблицами и научить делать простые выводы по данным.",
        tasks=["Научить заполнять и сортировать таблицы", "Научить считать простые показатели"],
        direction="техническая",
        level="стартовый",
        norm_ids=["fz273", "minpros629"],
        slides_scope=SlidesScope(mode="sessions", session_numbers=[1, 2]),
    )
    competencies = [Competency(id="C1", title="Работать с таблицами и делать выводы")]
    outcomes = [
        LearningOutcome(
            id="LO1.1", competency_id="C1", kind=OutcomeKind.KNOW, text="Знать, из чего состоит таблица"
        ),
        LearningOutcome(
            id="LO1.2", competency_id="C1", kind=OutcomeKind.CAN, text="Уметь сортировать таблицу"
        ),
        LearningOutcome(
            id="LO1.3",
            competency_id="C1",
            kind=OutcomeKind.MASTER,
            text="Владеть подсчётом простых показателей",
        ),
    ]
    modules = [
        Module(id="M1", order=1, title="Таблицы вокруг нас", control_form="Мини-тест"),
        Module(id="M2", order=2, title="Считаем и делаем выводы", control_form="Мини-проект"),
    ]
    topic_specs = [
        ("T1.1", "M1", 1, "Что такое таблица", ["LO1.1"]),
        ("T1.2", "M1", 2, "Заполняем таблицу", ["LO1.1", "LO1.2"]),
        ("T1.3", "M1", 3, "Сортируем и ищем", ["LO1.2"]),
        ("T2.1", "M2", 1, "Считаем сумму и среднее", ["LO1.3"]),
        ("T2.2", "M2", 2, "Строим простой график", ["LO1.3"]),
        ("T2.3", "M2", 3, "Делаем вывод по данным игры", ["LO1.2", "LO1.3"]),
        ("T2.4", "M2", 4, "Готовим проект", ["LO1.3"]),
    ]
    topics = [
        Topic(
            id=tid,
            module_id=mid,
            order=order,
            title=title,
            lo_ids=lo_ids,
            hours=HoursRow(lk=0.5, pr=1.5),
            content={
                WorkKind.LK: [f"Объяснение: {title.lower()}"],
                WorkKind.PR: [f"Практика: {title.lower()}"],
            },
        )
        for tid, mid, order, title, lo_ids in topic_specs
    ]
    lectures = [
        Lecture(
            id=f"L{t.id[1:]}",
            module_id=t.module_id,
            topic_ids=[t.id],
            session_no=n,
            title=t.title,
            goal=f"Понять тему «{t.title.lower()}» на примере школьной игры",
            plan=[
                PlanItem(
                    title="Зачем это нужно",
                    theses=["Пример из жизни", "Что будем делать", "Что получится"],
                    lo_ids=t.lo_ids,
                ),
                PlanItem(
                    title="Разбираем пример",
                    theses=["Шаг за шагом", "Проверяем себя", "Частые ошибки"],
                    lo_ids=t.lo_ids,
                ),
                PlanItem(
                    title="Итоги", theses=["Что запомнить", "Домашний вопрос", "Что дальше"], lo_ids=t.lo_ids
                ),
            ],
            glossary=[GlossaryItem(term="таблица", definition="данные, записанные в строки и столбцы")],
            slides=[Slide(title=t.title, bullets=["Цель занятия", "План", "Результат"], notes="Вступление")]
            + [
                Slide(title=x, bullets=["Пример", "Вопрос классу", "Ответ"], notes="Пояснение")
                for x in ("Пример", "Итоги")
            ]
            if n in (1, 2)
            else None,
        )
        for n, t in enumerate(topics, start=1)
    ]
    seminars = [
        Seminar(
            id=f"S{n}",
            topic_id=t.id,
            lo_ids=t.lo_ids,
            title=f"Задание: {t.title.lower()} на данных школьной игры",
            situation="Класс собрал результаты школьной игры и хочет понять, кто лучше всех.",
            task="Выполнить задание по шагам на учебной таблице.",
            steps=["Открыть таблицу", "Сделать задание", "Показать результат"],
            deliverable="Таблица с результатом",
            criteria=_criteria(f"S{n}"),
            pass_threshold=6,
        )
        for n, t in enumerate(topics, start=1)
    ]
    questions = _questions(
        "TS1",
        [
            ("T1.1", ["LO1.1"]),
            ("T1.2", ["LO1.1"]),
            ("T1.3", ["LO1.2"]),
            ("T2.1", ["LO1.3"]),
            ("T2.2", ["LO1.3"]),
            ("T2.3", ["LO1.2"]),
        ],
    )
    final = FinalAttestation(
        form=FAForm.PROJECT_DEFENSE,
        form_text="Защита небольшого проекта по данным школьной игры",
        hours=2,
        lo_ids=[o.id for o in outcomes],
        requirements=["Таблица с данными игры", "Один график", "Вывод в двух-трёх предложениях"],
        criteria=_criteria("FA"),
        pass_rule="Зачтено при 6 баллах из 10",
        procedure=["Рассказ о проекте 3 минуты", "Ответы на вопросы"],
    )
    kit = Kit(
        request=request,
        program=program,
        competencies=competencies,
        outcomes=outcomes,
        modules=modules,
        topics=topics,
        lectures=lectures,
        seminars=seminars,
        tests=[
            Test(
                id="TS1",
                title="Итоговый тест",
                scope=TestScope.FINAL,
                pass_threshold=60,
                question_ids=[q.id for q in questions],
            )
        ],
        questions=questions,
        final_attestation=final,
        sources=_norm_sources(program.norm_ids) + _literature_sources(3),
        texts=_texts(dop=True),
    )
    return _finish(kit)


__all__ = ["make_dop_kit", "make_internship_kit", "make_pk_kit", "make_registry", "ModuleKind"]
