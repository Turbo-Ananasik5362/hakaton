"""Реестр для тестов каркаса: нормативка и сервисы — из validate_kits.make_registry, плюс профстандарты под
тестовые запросы.

ВНИМАНИЕ: это тестовые данные. Коды профстандартов взяты из списка кандидатов data/registry/profstandards.json
(кроме 99.001 — заведомо вымышленный код), но коды и названия трудовых функций здесь упрощены и НЕ сверены
с первоисточником. В продукт и в data/registry их переносить нельзя — реестр наполняет norm-verifier.
"""

from __future__ import annotations

from app.core.registry import Registry
from app.core.registry_api import LaborFunctionRecord, ProfStandardRecord
from tests.validate_kits import make_registry

URL = "https://example.org/test-only"


def _ps(code: str, title: str, tags: list[str], functions: list[tuple[str, str]]) -> ProfStandardRecord:
    return ProfStandardRecord(
        code=code,
        title=title,
        order="тестовая запись",
        status="verified",
        source_url=URL,
        checked_at="2026-10-18",
        tags=tags,
        labor_functions=[LaborFunctionRecord(otf=c.split("/")[0], code=c, title=t) for c, t in functions],
    )


TEST_PROFSTANDARDS = [
    _ps(
        "01.001",
        "Педагог (педагогическая деятельность в сфере общего образования) (воспитатель, учитель)",
        ["учителей", "учителя", "педагогов"],
        [("A/01.6", "Обучение (общепедагогическая функция)"), ("A/03.6", "Развивающая деятельность")],
    ),
    _ps(
        "06.042",
        "Специалист по большим данным",
        ["pandas", "sql", "аналитика"],
        [
            ("A/01.6", "Подготовка данных для анализа"),
            ("A/02.6", "Анализ данных и представление результатов"),
        ],
    ),
    _ps(
        "06.032",
        "Специалист по безопасности компьютерных систем и сетей",
        ["кибербезопасности", "кибербезопасность", "безопасность"],
        [("A/01.5", "Обслуживание средств защиты информации"), ("A/02.5", "Мониторинг событий безопасности")],
    ),
    _ps(
        "06.026",
        "Системный администратор информационно-коммуникационных систем",
        ["linux", "devops"],
        [
            (
                "B/02.5",
                "Обеспечение работы технических и программных средств информационно-коммуникационных систем",
            ),
            (
                "B/04.5",
                "Внесение изменений в технические и программные средства информационно-коммуникационных систем "
                "по утвержденному плану работ",
            ),
        ],
    ),
    _ps(
        "08.002",
        "Бухгалтер",
        ["бухгалтеров", "бухгалтерии"],
        [("A/01.5", "Ведение бухгалтерского учёта"), ("A/02.5", "Составление бухгалтерской отчётности")],
    ),
    _ps(
        "99.001",
        "Тестовый профстандарт: специалист по работе с клиентами",
        ["продаж"],
        [("A/01.5", "Подготовка коммерческих предложений"), ("A/02.5", "Ведение переговоров с клиентами")],
    ),
]


def make_skeleton_registry() -> Registry:
    base = make_registry()
    return Registry(
        norms=base.norms(),
        profstandards=[*base.profstandards(), *TEST_PROFSTANDARDS],
        ai_services=base.ai_services(),
        literature=base.literature_items(),
        publishers=list(base._publishers),  # тестовая сборка: у реестра нет публичного списка издательств
        tools=base.tools(),
    )
