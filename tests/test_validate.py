"""Проверки validate.py: эталоны чистые, каждая порча ловится своей проверкой, ошибки образца 248 ч находятся."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.registry_api import AIServiceRecord, InMemoryRegistry, ToolRecord
from app.core.validate import CHECKS, ValidationContext, validate, validate_reference
from app.models import Kit, Severity, WarningType
from tests.validate_kits import make_dop_kit, make_internship_kit, make_pk_kit, make_registry
from tests.validate_mutations import MUTATIONS, load_reference

ROOT = Path(__file__).resolve().parent.parent
BUILDERS = {"pk": make_pk_kit, "pk_internship": make_internship_kit, "dop": make_dop_kit}


def run(kit: Kit, ctx: ValidationContext | None = None, only: list[str] | None = None):  # noqa: ANN201
    return validate(kit, ctx or ValidationContext(registry=make_registry(), current_year=2026), only=only)


# ---- эталоны ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(BUILDERS))
def test_reference_kits_are_clean(kind: str) -> None:
    report = run(BUILDERS[kind]())
    assert report.issues == [], [(i.check, i.message) for i in report.issues]


def test_all_checks_registered_except_v31() -> None:
    expected = {f"V{n:02d}" for n in range(1, 47)} - {"V31"}
    assert set(CHECKS) == expected


def test_every_check_has_a_mutation() -> None:
    assert set(MUTATIONS) == set(CHECKS)


# ---- каждая порча ловится своей проверкой ----------------------------------------------------------


@pytest.mark.parametrize("check", sorted(MUTATIONS))
def test_mutation_is_caught_by_its_check(check: str) -> None:
    builder, mutate = MUTATIONS[check]
    kit = builder()
    ctx = ValidationContext(registry=make_registry(), current_year=2026)
    mutate(kit, ctx)
    report = run(kit, ctx)
    own = [i for i in report.issues if i.check == check]
    assert own, f"{check} не сработала; сработали: {sorted({i.check for i in report.issues})}"
    assert all(i.severity != Severity.INFO for i in own)
    assert all(i.message and i.checklist_refs for i in own)
    assert not [i for i in report.issues if "внутренней ошибки" in i.message], "проверка упала с исключением"


def test_issue_ids_are_numbered_per_check() -> None:
    builder, mutate = MUTATIONS["V18"]
    kit = builder()
    kit.questions[0].correct = [7]
    kit.questions[1].correct = [9]
    issues = [i for i in run(kit).issues if i.check == "V18"]
    assert [i.id for i in issues][:2] == ["V18-1", "V18-2"]


# ---- отдельные режимы проверок ---------------------------------------------------------------------


def test_blocked_service_is_error_when_request_requires_ru_services() -> None:
    kit = make_pk_kit()
    kit.request.constraints.ru_services_only = True
    kit.seminars[0].task += " Сравнить с ответом ChatGPT."
    issues = [i for i in run(kit).issues if i.check == "V28"]
    assert issues and issues[0].severity == Severity.ERROR


def test_blocked_service_is_warning_without_requirement() -> None:
    kit = make_pk_kit()
    kit.seminars[0].task += " Сравнить с ответом Чат ГПТ."
    issues = [i for i in run(kit).issues if i.check == "V28"]
    assert issues and issues[0].severity == Severity.WARNING


def test_blocked_service_found_in_inflected_form() -> None:
    """Кириллическое имя сервиса из стоп-листа ловится и в падежной форме, ё не мешает."""
    registry = InMemoryRegistry(
        ai_services=[AIServiceRecord(id="ds", name="Дипсик", available_in_ru=False, status="verified")]
    )
    ctx = ValidationContext(registry=registry, current_year=2026)
    kit = make_pk_kit()
    kit.request.constraints.ru_services_only = True
    kit.seminars[0].task += " Попросите Дипсика объяснить ещё раз."
    issues = [i for i in run(kit, ctx, only=["V28"]).issues if i.severity == Severity.ERROR]
    assert issues and "Дипсик" in issues[0].message
    clean = make_pk_kit()
    clean.request.constraints.ru_services_only = True
    clean.seminars[0].task += " Дипсиковый — не имя сервиса."
    assert not [i for i in run(clean, ctx, only=["V28"]).issues if i.severity == Severity.ERROR]


def test_without_registry_nothing_crashes() -> None:
    report = validate(make_pk_kit(), ValidationContext(registry=None, current_year=2026))
    assert not [i for i in report.issues if "внутренней ошибки" in i.message]
    assert any(i.check in ("V23", "V24", "V25") for i in report.issues)  # честно говорит, что реестра нет
    assert not [i for i in report.issues if i.severity == Severity.ERROR and i.check not in ("V24", "V25")]


def test_only_runs_selected_checks() -> None:
    kit = make_pk_kit()
    kit.questions[0].correct = [7]
    report = run(kit, only=["V18", "V19"])
    assert {i.check for i in report.issues} == {"V18"}
    with pytest.raises(ValueError):
        run(kit, only=["V99"])


def test_report_snapshot_and_coverage() -> None:
    kit = make_pk_kit()
    report = run(kit)
    hours = report.hours
    assert hours is not None and hours.matches and hours.total == kit.total_hours()
    assert hours.topics == kit.program_hours().total and hours.final_attestation == kit.fa_hours()
    assert hours.contact == kit.program_hours().contact and hours.srs == kit.program_hours().srs
    levels = [line.level for line in hours.lines]
    assert levels.count("module") == len(kit.modules) and levels.count("topic") == len(kit.topics)
    assert levels[-2:] == ["final_attestation", "total"] and hours.lines[-1].total == kit.total_hours()
    assert {c.lo_id for c in report.coverage} == {o.id for o in kit.outcomes}
    lo23 = next(c for c in report.coverage if c.lo_id == "LO2.3")
    assert "T2.2" in lo23.topic_ids and len(lo23.assessment_ids) >= 2
    assert lo23.status == "ok" and lo23.text and sum(lo23.by_kind.values()) == len(lo23.assessment_ids)
    sources = report.sources
    assert sources is not None and sources.total == len(kit.sources)
    assert sources.confirmed + sources.unverified == sources.total == sum(sources.by_verification.values())
    assert report.copying is not None and report.copying.references == 0  # образца нет — сравнивать не с чем


def test_norm_replaced_warning_only_for_pk() -> None:
    pk = run(make_pk_kit()).warnings_for_methodist
    dop = run(make_dop_kit()).warnings_for_methodist
    assert [w.type for w in pk] == [WarningType.NORM_REPLACED]
    assert "266" in pk[0].text and "499" in pk[0].text
    assert dop == []


def test_reference_errors_become_methodist_warning() -> None:
    kit = make_internship_kit()
    kit.request.reference = load_reference("bsa_248_sample.json")
    types = [w.type for w in run(kit).warnings_for_methodist]
    assert WarningType.REFERENCE_ERRORS in types


# ---- V45: образец стажировки 248 ч -----------------------------------------------------------------

SAMPLE_ERRORS = {
    "итоговая строка учебного плана ≠ разделы + ИА": ("учебном плане", "«Итого» не равна сумме разделов"),
    "строка текущего контроля с нулями": ("Текущий контроль", "везде 0"),
    "раздел ≠ сумме тем": ("строка раздела", "не равна сумме своих тем"),
    "СРС по темам 168 ≠ 174": ("174", "168"),
    "метка ПР 9 при колонке 12 (тема 1.6)": ("тема 1.6", "в колонке часов 12"),
    "метка ПР 6 при колонке 9 (тема 1.7)": ("тема 1.7", "в колонке часов 9"),
    "РП ≠ УТП, тема 1.2": ("Тема 1.2", "в рабочей программе 12, в УТП 15"),
    "РП ≠ УТП, тема 1.5": ("Тема 1.5", "в рабочей программе 15, в УТП 18"),
    "«ЛР» против «ЛБ»": ("«ЛР»", "«ЛБ»"),
    "заполненные реквизиты приказа": ("номер и дата приказа", "____"),
}


@pytest.mark.parametrize("name", sorted(SAMPLE_ERRORS))
def test_sample_248_each_error_found(name: str) -> None:
    messages = [i.message for i in validate_reference(load_reference("bsa_248_sample.json"))]
    needles = SAMPLE_ERRORS[name]
    assert any(all(n in m for n in needles) for m in messages), f"не найдено: {name}\n" + "\n".join(messages)


def test_sample_248_internship_with_contact_hours_flagged() -> None:
    messages = [i.message for i in validate_reference(load_reference("bsa_248_sample.json"))]
    assert any("стажировка" in m and "аудиторные часы" in m for m in messages)


def test_corrected_sample_has_no_errors() -> None:
    assert validate_reference(load_reference("bsa_248_corrected.json")) == []


def test_parsed_real_sample_matches_fixture() -> None:
    """Сквозная проверка: разбор настоящего .docx → V45 находит те же ошибки, что и на фикстуре."""
    template = ROOT / "data" / "templates" / "pk_stazhirovka_bsa_248h.docx"
    if not template.exists():
        pytest.skip("нет образца в data/templates")
    from app.core.reference import parse_reference

    live = {i.message for i in validate_reference(parse_reference(template))}
    fixture = {i.message for i in validate_reference(load_reference("bsa_248_sample.json"))}
    assert live == fixture


@pytest.mark.parametrize(
    ("goal", "flagged"),
    [
        ("Сформировать базовые компетенции, достаточные для начала работы в сфере защиты информации.", True),
        ("После курса слушатели готовы к работе в отделе безопасности.", True),
        ("Слушатель сможет работать специалистом по защите информации.", True),
        ("Сформировать начальные навыки и умение работать в команде.", False),
        ("Дать основу, достаточную для продолжения обучения.", False),
    ],
)
def test_overpromise_in_goal(goal: str, flagged: bool) -> None:
    kit = make_pk_kit()
    kit.program.goal = goal
    found = [i for i in run(kit, only=["V40"]).issues if i.check == "V40"]
    assert bool(found) == flagged


def test_preparation_for_work_in_a_field_is_a_promise() -> None:
    """«…для подготовки к работе в сфере …» за 72 ч — обещание подготовки специалиста (чек-лист запроса 4,
    п. 4.8; оценка 21.10). Обычная формулировка ПК «компетенции, необходимые для работы в сфере» — не обещание."""
    kit = make_pk_kit()
    kit.program.goal = "Сформировать базовые компетенции для подготовки к работе в сфере безопасности."
    assert [i for i in run(kit, only=["V40"]).issues if i.check == "V40"]
    kit.program.goal = (
        "Совершенствование компетенций, необходимых для работы в сфере информационной безопасности."
    )
    assert not [i for i in run(kit, only=["V40"]).issues if i.check == "V40"]


def test_dot_program_with_repealed_order_816() -> None:
    """ДОТ: приказ № 816 утратил силу — ошибка с указанием действующего постановления № 1678,
    а отсутствие постановления в нормативной базе — предупреждение."""
    kit = make_pk_kit()  # очно-заочная с ДОТ
    kit.program.norm_ids = ["fz273", "minobrnauki266", "minobrnauki816"]
    issues = [i for i in run(kit, only=["V24"]).issues]
    errors = [i for i in issues if i.severity == Severity.ERROR]
    assert errors and "№ 816 утратил силу" in errors[0].message
    assert "постановление Правительства РФ от 11.10.2023 № 1678" in errors[0].message
    assert any(i.severity == Severity.WARNING and "№ 1678" in i.message for i in issues)
    kit.program.norm_ids = ["fz273", "minobrnauki266", "gov1678"]
    assert run(kit, only=["V24"]).issues == []


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("Установите библиотеку: pip3 install pandas.", False),
        ("Docker 2 ч практики, затем Docker 3 часа самостоятельно.", False),
        ("Используем Python 3.12 и pip 26.2.", False),
        ("Python3 уже установлен.", False),
        ("Пример написан для Python 3.8.", True),
        ("Нужен Python версии 3.9.", True),
        ("Подойдёт pip v9.", True),
    ],
)
def test_v29_version_mentions(text: str, flagged: bool) -> None:
    """V29: версия — число после имени через пробел, «v» или «версии»; часы, «pip3» и «python3» — не версии."""
    registry = InMemoryRegistry(
        tools=[
            ToolRecord(id="python", name="Python", aliases=["Питон"], min_version="3.11", status="verified"),
            ToolRecord(id="pip", name="pip", min_version="26.2", status="verified"),
            ToolRecord(id="docker", name="Docker", min_version="25.0", status="verified"),
        ]
    )
    kit = make_pk_kit()
    kit.seminars[0].task += " " + text
    issues = run(kit, ValidationContext(registry=registry, current_year=2026), only=["V29"]).issues
    assert bool(issues) is flagged, [i.message for i in issues]


def test_v05_hours_in_text_not_from_plan() -> None:
    """Число часов в тексте, которого нет в учебном плане, — ошибка (страховка правила «часы — только от солвера»)."""
    kit = make_pk_kit()
    total = kit.program.total_hours
    kit.texts.annotation = f"Программа рассчитана на {total:g} часов. Обучение длится 123 часа."
    kit.seminars[0].task += " Нормы: не больше 8 часов в день."
    issues = [i for i in run(kit, only=["V05"]).issues if i.severity == Severity.ERROR]
    assert len(issues) == 1 and "123 часа" in issues[0].message


def test_v05_catches_hour_forms_and_daily_norms() -> None:
    """«акад. часов», «18-часовой», «а.ч.» и «N часов в день», которых нет в плане, — тоже выдуманные часы."""
    kit = make_pk_kit()
    kit.texts.annotation = (
        "Это 117-часовой интенсив. Объём 137 акад. часов. Всего 139 а.ч. Занятия по 11 часов в день."
    )
    issues = [i for i in run(kit, only=["V05"]).issues if i.severity == Severity.ERROR]
    assert len(issues) == 4


def test_codes_without_context_are_not_marked() -> None:
    """Номер задания, сумма, версия — не реквизиты акта и не код ПС: метку не получают."""
    from app.core.validate import mark_unknown_codes

    registry = make_registry()
    for text in (
        "Ответьте на вопросы, указанные в задании № 2.",
        "Письменный ответ на вопрос № 3.",
        "Бюджет — 15.000 тыс. руб., версия 10.123.",
    ):
        assert mark_unknown_codes(text, registry) == text
    assert "[не подтверждено]" in mark_unknown_codes("Указом Президента № 4321 утверждено.", registry)
    assert "[не подтверждено]" in mark_unknown_codes("Профстандарт 06.099 не найден.", registry)


def test_mark_unknown_codes_and_v27() -> None:
    """Коды ПС/ТФ и номера актов, которых нет в реестре, получают метку; известные — нет; V27 после метки молчит."""
    from app.core.validate import mark_unknown_codes

    registry = make_registry()
    text = "По профстандарту 06.099 и приказу № 1234; действует приказ № 266, стандарт 06.015, ТФ C/11.6."
    marked = mark_unknown_codes(text, registry)
    assert marked.count("[не подтверждено]") == 2
    assert "06.099 [не подтверждено]" in marked and "№ 1234 [не подтверждено]" in marked
    assert mark_unknown_codes(marked, registry) == marked  # повторная метка не ставится
    kit = make_pk_kit()
    kit.texts.annotation = text
    assert [i for i in run(kit, only=["V27"]).issues if i.target_ids == []]
    kit.texts.annotation = marked
    assert not [i for i in run(kit, only=["V27"]).issues if "06.099" in i.message or "1234" in i.message]


@pytest.mark.parametrize(
    ("option", "flagged"),
    [
        ("SELECT c.name, SUM(t.amount) FROM customers c JOIN transactions t ON c.id = t.customer_id;", False),
        ("import pandas as pd; df = pd.read_sql('SELECT * FROM transactions', conn)", False),
        ("df.groupby('region')['amount'].sum().sort_values(ascending=False)", False),
        ("The quick analysis of the data is done with the help of the best tools in the world", True),
    ],
)
def test_v32_code_in_options_is_not_foreign_text(option: str, flagged: bool) -> None:
    kit = make_pk_kit()
    kit.questions[0].options[0] = option
    issues = [i for i in run(kit, only=["V32"]).issues if "не на русском" in i.message]
    assert bool(issues) is flagged


def test_v10_form_text_must_match_form_flags() -> None:
    """Текст «форма обучения» обещает ДОТ, которых нет в форме программы, — предупреждение; «без ДОТ» — нет."""
    from tests.validate_kits import make_internship_kit

    kit = make_internship_kit()
    assert not (kit.program.form.dot or kit.program.form.eo)
    kit.texts.form_description = "Обучение с применением электронного обучения и дистанционных технологий."
    assert [i for i in run(kit, only=["V10"]).issues if "ЭО и ДОТ не предусмотрены" in i.message]
    kit.texts.form_description = "Стажировка проходит очно, без применения дистанционных технологий."
    assert not run(kit, only=["V10"]).issues
    # отрицание после упоминания — тоже не обещание (ложное замечание в живом прогоне 21.10)
    for text in (
        "Обучение очное. Электронное обучение и дистанционные технологии не используются.",
        "Дистанционные образовательные технологии при реализации программы не применяются.",
        "Электронное обучение исключено: все занятия в аудитории.",
        # «без» на всё перечисление (ложное замечание в живом прогоне 22.10)
        "Занятия проводятся с применением традиционных методов обучения, без использования электронного обучения "
        "и дистанционных технологий.",
    ):
        kit.texts.form_description = text
        assert not run(kit, only=["V10"]).issues, text
    # «без отрыва от работы», «не менее половины» — не отрицание (ревью 22.10)
    for text in (
        "Обучение проводится без отрыва от работы с применением дистанционных образовательных технологий.",
        "Не менее половины занятий проводится в дистанционном формате.",
    ):
        kit.texts.form_description = text
        assert [i for i in run(kit, only=["V10"]).issues if "ЭО и ДОТ не предусмотрены" in i.message], text
    # отрицание в другом предложении не спасает обещание
    kit.texts.form_description = (
        "Занятия проходят с применением дистанционных технологий. Очные встречи не проводятся."
    )
    assert [i for i in run(kit, only=["V10"]).issues if "ЭО и ДОТ не предусмотрены" in i.message]


def test_v39_real_data_with_anonymized_requirement() -> None:
    kit = make_pk_kit()
    kit.request.constraints.anonymized_data = True
    kit.texts.mto = ["Доступ к SQL-серверам банка с реальными клиентскими данными"]
    assert [i for i in run(kit, only=["V39"]).issues if "обезличенные" in i.message]
    kit.texts.mto = ["Учебная база на реальных, но обезличенных данных"]
    assert not [i for i in run(kit, only=["V39"]).issues if "обезличенные" in i.message]


def test_v36_required_work_type_is_satisfied_by_final_attestation() -> None:
    """«В конце каждый защищает проект — игру или чат-бота» — итоговый проект, а не отдельное задание: вид работы
    из запроса засчитывается и итоговой аттестацией (ложная ошибка V36 в прогоне 21.10)."""
    from app.models import TopicRequirement

    kit = make_dop_kit()
    kit.request.audience.domain_patterns = []
    req = TopicRequirement(
        label="проект (игра или чат-бот)", patterns=["прое.{0,40}?игр.{0,40}?или.{0,40}?чат.{0,40}?бот"]
    )
    kit.request.constraints.required_seminar_types = [req]
    for seminar in kit.seminars:
        seminar.title, seminar.task, seminar.deliverable = (
            "Практическая работа",
            "Выполнить упражнение.",
            "Код",
        )
    assert kit.final_attestation is not None
    kit.final_attestation.expected_result = "Готовый к демонстрации проект на Python (игра или чат-бот)"
    assert not run(kit, only=["V36"]).issues
    kit.final_attestation.expected_result = "Готовый к демонстрации проект на Python"
    kit.final_attestation.requirements = []
    kit.final_attestation.example_task = None
    assert [i for i in run(kit, only=["V36"]).issues if "проект (игра или чат-бот)" in i.message]


def test_v21_texts_must_name_the_programs_final_attestation_form() -> None:
    """«Итоговая аттестация проводится в форме защиты проекта» при экзамене — предупреждение (оценка 21.10, п. 4.10)."""
    from app.models import FAForm

    kit = make_pk_kit()
    assert kit.final_attestation is not None
    kit.final_attestation.form = FAForm.EXAM_PRACTICAL
    kit.texts.org_process = (
        "Обучение очное. Итоговая аттестация проводится в форме защиты проекта перед комиссией."
    )
    found = [i for i in run(kit, only=["V21"]).issues if "итоговая аттестация названа" in i.message]
    assert found and "защита проекта" in found[0].message and "экзамен" in found[0].message
    kit.texts.org_process = "Обучение очное. Итоговая аттестация — экзамен с практическим заданием на стенде."
    assert not [i for i in run(kit, only=["V21"]).issues if "итоговая аттестация названа" in i.message]
    # «проект» в другом смысле, не про итоговую аттестацию, — не форма ИА
    kit.texts.org_process = (
        "Слушатели выполняют учебный проект по теме модуля. Итоговая аттестация — экзамен."
    )
    assert not [i for i in run(kit, only=["V21"]).issues if "итоговая аттестация названа" in i.message]


def test_v18_flags_the_same_question_in_other_words() -> None:
    kit = make_pk_kit()
    test = kit.tests[0]
    first = next(q for q in kit.questions if q.id in test.question_ids)
    twin = first.model_copy(update={"id": "Q900", "stem": first.stem + " Выберите один ответ."})
    kit.questions.append(twin)
    test.question_ids.append("Q900")
    found = [i for i in run(kit, only=["V18"]).issues if "тот же вопрос другими словами" in i.message]
    assert found and set(found[0].target_ids) == {"Q900", first.id}
    assert not [i for i in run(make_pk_kit(), only=["V18"]).issues if "другими словами" in i.message]


def test_v20_python_example_in_lecture_must_compile() -> None:
    """Пример кода в тезисе лекции программы по Python, который не компилируется, — предупреждение."""
    kit = make_dop_kit()
    kit.program.title = "Первые шаги в Python"
    lecture = kit.lectures[0]
    lecture.plan[0].theses = ["Цикл выводит числа:\nfor i in range(3):\nprint(i)"]  # потерян отступ
    found = [i for i in run(kit, only=["V20"]).issues if "пример кода не запускается" in i.message]
    assert found and lecture.id in found[0].target_ids
    lecture.plan[0].theses = ["Цикл выводит числа:\nfor i in range(3):\n    print(i)"]
    assert not [i for i in run(kit, only=["V20"]).issues if "пример кода не запускается" in i.message]


def test_v33_program_subject_is_not_a_separate_topic() -> None:
    """Прогон 22.10: «Кибербезопасность для студентов колледжа» — V33 требовал отдельную тему «кибербезопасность»,
    хотя это предмет всей программы. Тема внутри программы по-прежнему обязательна."""
    from app.models import TopicRequirement

    kit = make_pk_kit()
    kit.request.title = "Статистика для аналитиков"
    kit.request.required_topics = [TopicRequirement(label="статистика", patterns=["статистик"])]
    assert not [
        i for i in validate(kit, ValidationContext(registry=make_registry())).issues if i.check == "V33"
    ]
    kit.request.required_topics.append(TopicRequirement(label="регрессия", patterns=["регресси"]))
    found = [i for i in validate(kit, ValidationContext(registry=make_registry())).issues if i.check == "V33"]
    assert [i.message for i in found] == [
        "Нет отдельной темы или модуля «регрессия» с часами, хотя это требование запроса."
    ]


def test_final_attestation_without_criteria_covers_nothing() -> None:
    """Прогон 22.10: у ИА 0 критериев, а матрица покрытия засчитывала её как средство оценки всем РО."""
    from app.core.validate import coverage_matrix

    kit = make_pk_kit()
    assert kit.final_attestation is not None and kit.final_attestation.lo_ids
    assert any("FA" in c.assessment_ids for c in coverage_matrix(kit))
    kit.final_attestation.criteria = []
    assert not any("FA" in c.assessment_ids for c in coverage_matrix(kit))
