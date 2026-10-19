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
    assert report.hours["TOTAL"].total == kit.program_hours().total
    assert {c.lo_id for c in report.coverage} == {o.id for o in kit.outcomes}
    lo23 = next(c for c in report.coverage if c.lo_id == "LO2.3")
    assert "T2.2" in lo23.topic_ids and len(lo23.assessment_ids) >= 2
    assert sum(report.sources.values()) == len(kit.sources)


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


def test_preparation_for_work_is_not_a_promise() -> None:
    kit = make_pk_kit()
    kit.program.goal = "Сформировать базовые компетенции для подготовки к работе в сфере безопасности."
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
