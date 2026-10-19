from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.registry import (
    DEFAULT_REGISTRY_DIR,
    UNVERIFIED_MARK,
    Registry,
    default_registry,
    is_verified,
    load_registry,
    mark_unverified,
)
from app.core.registry_api import LaborFunctionRecord, ProfStandardRecord, RegistryView, normalize_text
from app.models import ProgramType

ALL_FILES = [
    "norms.json",
    "profstandards.json",
    "ru_ai_services.json",
    "literature.json",
    "publishers.json",
    "tools.json",
]


def write_items(directory: Path, filename: str, items: list, **extra: object) -> None:
    payload = {"items": items, **extra}
    (directory / filename).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------------------------------
# Реальный реестр data/registry
# --------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real() -> Registry:
    return load_registry()


def test_real_registry_loads_cleanly(real: Registry) -> None:
    view: RegistryView = real  # реализует протокол реестра
    assert view.norms()
    assert real.load_warnings == []
    assert DEFAULT_REGISTRY_DIR.name == "registry"


def test_real_norm_statuses(real: Registry) -> None:
    current = real.norm("minobrnauki266")
    replaced = real.norm("minobrnauki499")
    assert current is not None and is_verified(current) and current.in_force
    assert replaced is not None and replaced.in_force is False
    assert replaced.replaced_by == "minobrnauki266"


def test_real_profstandard_and_labor_function(real: Registry) -> None:
    standard = real.profstandard("06.015")
    assert standard is not None and is_verified(standard)
    assert "586н" in standard.order  # действующая редакция (896н утратил силу с 01.09.2024)
    function = real.labor_function("06.015", "C/11.6")
    assert function is not None and function.title.startswith("Выявление требований к ИС")
    assert real.labor_function("06.015", "С / 11.6") == function  # кириллическая «С» и пробелы
    assert real.labor_function("06.015", "C/99.6") is None
    assert real.labor_function("99.999", "C/11.6") is None


def test_real_candidates_are_not_records(real: Registry) -> None:
    raw = json.loads((DEFAULT_REGISTRY_DIR / "profstandards.json").read_text(encoding="utf-8"))
    candidates = {item["code"] for item in raw.get("candidates_to_verify", [])}
    codes = {standard.code for standard in real.profstandards()}
    assert candidates and not candidates & codes


def test_real_registry_all_items_verified(real: Registry) -> None:
    """В items реестра — только подтверждённое; непроверенное лежит в candidates_to_verify."""
    assert all(is_verified(n) for n in real.norms())
    assert all(is_verified(s) for s in real.profstandards())
    assert all(is_verified(item) for item in real.literature_items())


@pytest.mark.parametrize(
    ("program_type", "dot", "expected", "absent"),
    [
        (
            ProgramType.PK,
            False,
            ["fz273", "minobrnauki266"],
            ["minobrnauki499", "minobrnauki816", "gov1678", "minpros629"],
        ),
        (
            ProgramType.PK_INTERNSHIP,
            True,
            ["fz273", "minobrnauki266", "gov1678"],
            ["minobrnauki499", "minobrnauki816"],
        ),
        (ProgramType.DOP, False, ["fz273", "minpros629"], ["minobrnauki266", "gov1678"]),
        (
            "dop",
            True,
            ["fz273", "minpros629", "gov1678"],
            ["minobrnauki266", "minobrnauki816", "letter_vk1013"],
        ),
    ],
)
def test_real_norms_for(
    real: Registry, program_type: ProgramType | str, dot: bool, expected: list[str], absent: list[str]
) -> None:
    ids = [norm.id for norm in real.norms_for(program_type, dot)]
    assert ids == expected
    assert not set(ids) & set(absent)


def test_real_norms_for_letters_optional(real: Registry) -> None:
    ids = [norm.id for norm in real.norms_for(ProgramType.PK, True, include_letters=True)]
    assert "letter_vk1013" in ids
    assert "letter_vk1013" not in [
        norm.id for norm in real.norms_for(ProgramType.PK, False, include_letters=True)
    ]


def test_real_labor_functions_for_prompt(real: Registry) -> None:
    pairs = real.labor_functions_for(["06.015", "99.999", "06.015"])
    codes = [function.code for _, function in pairs]
    assert ("06.015", "C/14.6") in [(ps, function.code) for ps, function in pairs]
    assert len(codes) == len(set(codes))  # повтор кода ПС не дублирует ТФ
    assert real.labor_functions_for(["99.999"]) == []


def test_real_search_profstandards(real: Registry) -> None:
    found = real.search_profstandards("Программа для аналитиков: требования к информационным системам")
    assert found and found[0].code == "06.015"
    assert real.search_profstandards(["06.015"])[0].code == "06.015"
    assert real.search_profstandards("кулинария и выпечка") == []


def test_default_registry_is_cached() -> None:
    assert default_registry() is default_registry()


# --------------------------------------------------------------------------------------------------
# Временные каталоги: отсутствующие файлы, битые записи
# --------------------------------------------------------------------------------------------------


def test_missing_files_give_empty_registry(tmp_path: Path) -> None:
    registry = load_registry(tmp_path)
    assert registry.norms() == [] and registry.literature_items() == [] and registry.ai_services() == []
    assert registry.missing_files == ALL_FILES
    assert registry.load_warnings == []
    assert registry.find_literature("Что угодно") is None
    assert registry.mentioned_ai_services("GigaChat") == []


def test_missing_directory_does_not_fail(tmp_path: Path) -> None:
    registry = load_registry(tmp_path / "нет такого каталога")
    assert registry.profstandards() == []


def test_broken_records_are_skipped_with_warnings(tmp_path: Path) -> None:
    write_items(
        tmp_path,
        "norms.json",
        [
            {"id": "fz273", "title": "Закон", "status": "verified", "source_url": "https://example.org"},
            {"id": "no_title"},
            "не объект",
            {"id": "bad_status", "title": "Приказ", "status": "maybe"},
            {"id": "fz273", "title": "Дубль"},
        ],
    )
    write_items(
        tmp_path,
        "profstandards.json",
        [{"code": "06.015", "title": "ПС", "labor_functions": [{"code": "C/11.6", "title": "ТФ"}]}],
        candidates_to_verify=[{"code": "06.042", "hint": "кандидат"}],
    )
    (tmp_path / "tools.json").write_text("{не json", encoding="utf-8")
    (tmp_path / "publishers.json").write_text('{"records": []}', encoding="utf-8")

    registry = load_registry(tmp_path)

    assert [norm.id for norm in registry.norms()] == ["fz273"]
    assert registry.norm("fz273").title == "Закон"
    assert [standard.code for standard in registry.profstandards()] == ["06.015"]
    assert registry.tools() == []
    warnings = registry.load_warnings
    norm_warnings = [w for w in warnings if w.startswith("norms.json")]
    assert len(norm_warnings) == 4
    assert any("no_title" in w and "title" in w for w in norm_warnings)
    assert any("повтор" in w for w in norm_warnings)
    assert any(w.startswith("tools.json") for w in warnings)
    assert any(w.startswith("publishers.json") for w in warnings)
    assert not any("06.042" in w for w in warnings)


def test_norms_for_dot_decree_and_repealed_order(tmp_path: Path) -> None:
    """ЭО/ДОТ: действующее постановление Правительства (kind decree) попадает в список, утративший силу
    приказ — нет; порядок по типу программ берётся только из приказов."""
    dot_title = (
        "Об утверждении Правил применения электронного обучения, дистанционных образовательных технологий"
    )
    write_items(
        tmp_path,
        "norms.json",
        [
            {
                "id": "law",
                "kind": "federal_law",
                "title": "Закон об образовании",
                "applies_to": ["pk", "dop"],
            },
            {
                "id": "order_pk",
                "kind": "order",
                "title": "Об утверждении Порядка организации и осуществления образовательной деятельности по ДПП",
                "applies_to": ["pk"],
            },
            {"id": "decree_dot", "kind": "decree", "title": dot_title, "applies_to": ["pk", "dop"]},
            {
                "id": "old_dot",
                "kind": "order",
                "title": "Порядок применения электронного обучения",
                "applies_to": ["pk", "dop"],
                "in_force": False,
                "replaced_by": "decree_dot",
            },
        ],
    )
    registry = load_registry(tmp_path)
    assert [n.id for n in registry.norms_for(ProgramType.PK, dot=True)] == ["law", "order_pk", "decree_dot"]
    assert [n.id for n in registry.norms_for(ProgramType.DOP, dot=True)] == ["law", "decree_dot"]
    assert [n.id for n in registry.norms_for(ProgramType.PK, dot=False)] == ["law", "order_pk"]


# --------------------------------------------------------------------------------------------------
# Литература: нечёткий поиск и шорт-лист
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def catalog(tmp_path: Path) -> Registry:
    write_items(
        tmp_path,
        "literature.json",
        [
            {
                "id": "wiegers",
                "authors": ["Вигерс К.", "Битти Дж."],
                "title": "Разработка требований к программному обеспечению",
                "year": 2019,
                "tags": ["requirements", "анализ"],
                "status": "verified",
                "source_url": "https://example.org/wiegers",
            },
            {
                "id": "mckinney",
                "authors": ["Маккини У."],
                "title": "Python и анализ данных",
                "year": 2023,
                "tags": ["python", "data"],
                "status": "verified",
                "source_url": "https://example.org/mckinney",
            },
            {
                "id": "old_python",
                "authors": ["Лутц М."],
                "title": "Изучаем Python",
                "year": 2011,
                "tags": ["python"],
            },
            {"id": "pandas_docs", "kind": "doc", "title": "Документация pandas", "tags": ["data"]},
        ],
    )
    return load_registry(tmp_path)


def test_find_literature_fuzzy_title(catalog: Registry) -> None:
    exact = catalog.find_literature("«Разработка требований к программному обеспечению»")
    typo = catalog.find_literature("Разработка требований к програмному обеспечению")
    edition = catalog.find_literature("Разработка требований к программному обеспечению. 3-е изд., доп.")
    assert exact is not None and exact.id == "wiegers"
    assert typo is not None and typo.id == "wiegers"
    assert edition is not None and edition.id == "wiegers"


def test_find_literature_checks_first_author(catalog: Registry) -> None:
    assert catalog.find_literature("Разработка требований к ПО", ["Вигерс К."]) is None  # название далеко
    found = catalog.find_literature("Разработка требований к программному обеспечению", ["Карл Вигерс"])
    assert found is not None and found.id == "wiegers"
    assert (
        catalog.find_literature("Разработка требований к программному обеспечению", ["Иванов И. И."]) is None
    )
    assert catalog.find_literature("Документация pandas", ["Кто-то К."]) is None  # у записи нет авторов
    assert catalog.find_literature("Документация pandas").id == "pandas_docs"


def test_find_literature_rejects_unrelated(catalog: Registry) -> None:
    assert catalog.find_literature("Искусство программирования") is None
    assert catalog.find_literature("") is None


def test_literature_for_tags_shortlist(catalog: Registry) -> None:
    shortlist = catalog.literature_for_tags(["python", "данные"])
    ids = [item.id for item in shortlist]
    assert ids[0] == "mckinney"  # тег + слова названия, подтверждена
    assert set(ids) == {"mckinney", "old_python"}
    assert catalog.literature_for_tags(["python"], limit=1) == shortlist[:1]
    assert catalog.literature_for_tags(["астрономия"]) == []
    assert catalog.literature_for_tags([]) == []


# --------------------------------------------------------------------------------------------------
# ИИ-сервисы
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def services(tmp_path: Path) -> Registry:
    write_items(
        tmp_path,
        "ru_ai_services.json",
        [
            {"id": "gigachat", "name": "GigaChat", "aliases": ["ГигаЧат"], "available_in_ru": True},
            {"id": "yandexgpt", "name": "YandexGPT", "aliases": ["Yandex GPT", "ЯндексГПТ"]},
            {"id": "chatgpt", "name": "ChatGPT", "aliases": ["Чат GPT"], "available_in_ru": False},
        ],
    )
    return load_registry(tmp_path)


def test_mentioned_ai_services_word_boundaries(services: Registry) -> None:
    found = services.mentioned_ai_services("Нейросеть GigaChat помогает методисту")
    assert [(service.id, fragment) for service, fragment in found] == [("gigachat", "GigaChat")]
    lower = services.mentioned_ai_services("работаем в гигачат каждый день")
    assert [(service.id, fragment) for service, fragment in lower] == [("gigachat", "гигачат")]
    assert services.mentioned_ai_services("gigachatter и supergigachat") == []


def test_mentioned_ai_services_forms_and_order(services: Registry) -> None:
    found = services.mentioned_ai_services("Сравним ChatGPT, Yandex  GPT и ответы в ГигаЧате.")
    assert [service.id for service, _ in found] == ["chatgpt", "yandexgpt", "gigachat"]
    assert dict((service.id, fragment) for service, fragment in found)["gigachat"] == "ГигаЧате"
    assert services.mentioned_ai_services("") == []


# --------------------------------------------------------------------------------------------------
# Статусы
# --------------------------------------------------------------------------------------------------


def test_is_verified_requires_status_and_url() -> None:
    assert is_verified(
        ProfStandardRecord(code="01.001", title="Педагог", status="verified", source_url="https://x")
    )
    assert not is_verified(ProfStandardRecord(code="01.001", title="Педагог", status="verified"))
    assert not is_verified(ProfStandardRecord(code="01.001", title="Педагог", source_url="https://x"))
    assert not is_verified(LaborFunctionRecord(code="A/01.6", title="ТФ"))
    assert Registry.is_verified(None) is False


def test_mark_unverified_without_double_mark() -> None:
    marked = mark_unverified("Иванов И. Книга. — М., 2020.")
    assert marked == f"Иванов И. Книга. — М., 2020. {UNVERIFIED_MARK}"
    assert mark_unverified(marked) == marked
    assert mark_unverified("  ") == UNVERIFIED_MARK
    assert Registry.mark_unverified("ПС 06.999") == f"ПС 06.999 {UNVERIFIED_MARK}"


# --------------------------------------------------------------------------------------------------
# Профстандарты: большие ПС не выигрывают числом ТФ, в промпт — только ближайшие ТФ
# --------------------------------------------------------------------------------------------------

TAIL = " в рамках выполнения работ по созданию и сопровождению ИС"


def _big_and_small() -> Registry:
    big = ProfStandardRecord(
        code="06.015",
        title="Специалист по информационным системам",
        tags=["ис"],
        labor_functions=[
            LaborFunctionRecord(otf="A", code=f"A/{i:02d}.4", title=f"Функция {word}{TAIL}")
            for i, word in enumerate(
                ["продаж", "клиентов", "отчётов", "обучения", "договоров", "закупок", "данных", "серверов"],
                start=1,
            )
        ],
    )
    small = ProfStandardRecord(
        code="99.001",
        title="Специалист по продажам",
        tags=["продаж"],
        labor_functions=[LaborFunctionRecord(otf="A", code="A/01.5", title="Ведение переговоров")],
    )
    return Registry(profstandards=[big, small])


def test_function_words_alone_do_not_select_standard() -> None:
    registry = _big_and_small()
    # «клиентов», «отчётов», «обучения» есть только в названиях ТФ большого ПС — это не повод его выбирать
    assert registry.search_profstandards("Обучение клиентов работе с отчётами") == []
    scored = registry.scored_profstandards("Продажи в информационной системе: клиенты, отчёты, договоры")
    by_code = {ps.code: score for score, ps in scored}
    # большой ПС: 2 слова названия (4) + 4 совпавших слова ТФ, но в зачёт не больше 3;
    assert by_code == {"06.015": 4 + 3, "99.001": 5 + 2}  # тег «продаж» ~ «продажи» + слово названия


def test_relevant_labor_functions_limit_and_order() -> None:
    registry = _big_and_small()
    big = registry.profstandard("06.015")
    assert big is not None
    picked = registry.relevant_labor_functions(big, "Работа с данными и серверами, сопровождение ИС", limit=3)
    codes = [f.code for f in picked]
    assert {"A/07.4", "A/08.4"} <= set(codes)  # «сопровождение ИС» есть во всех ТФ и не решает выбор
    assert codes == sorted(codes)  # исходный порядок ТФ
    assert len(codes) == 3
    small = registry.profstandard("99.001")
    assert registry.relevant_labor_functions(small, "что угодно", limit=3) == small.labor_functions


def test_typographic_apostrophe_is_ignored() -> None:
    assert normalize_text("O’Reilly Media") == normalize_text("O'Reilly Media") == "o reilly media"


def test_tags_match_word_forms_once() -> None:
    """Русский тег сравнивается по основе («машинного» ~ «машинное»), латиница и короткие — целиком;
    несколько словоформ одного слова в тегах не дают лишних баллов."""
    ml = ProfStandardRecord(code="06.042", title="Аналитик", tags=["машинного", "python"])
    forms = ProfStandardRecord(code="01.001", title="Педагог", tags=["учителей", "учителя", "учитель"])
    short = ProfStandardRecord(code="06.033", title="Защитник", tags=["иб"])
    registry = Registry(profstandards=[ml, forms, short])
    scores = {ps.code: score for score, ps in registry.scored_profstandards("Машинное обучение на Python")}
    assert scores == {"06.042": 10}
    scores = {ps.code: score for score, ps in registry.scored_profstandards("Курс для учителей")}
    assert scores == {"01.001": 5}
    assert registry.search_profstandards("Основы ИБ для бухгалтеров")[0].code == "06.033"
    assert registry.search_profstandards("Либерализм и ибис") == []  # «иб» — только целым словом


def test_real_registry_passes_formal_checks() -> None:
    from scripts.check_registry import check_registry

    result = check_registry()
    assert result.ok, result.errors[:10]
