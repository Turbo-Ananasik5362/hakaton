"""Правила разбора запроса кодом: числа, тип, форма, ИА, ограничения, слайды, название, противоречия."""

from __future__ import annotations

import re

import pytest

from app.models import FAForm, ProgramType, RequestSpec, StudyFormKind
from app.pipeline.request_rules import (
    classify_form,
    classify_type_answer,
    extract_facts,
    extract_title,
    normalize,
    spec_conflicts,
    text_conflicts,
    topic_pattern,
)

# ---- числа -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "field", "expected"),
    [
        ("Объем 16 часов, занятия по 2 часа", "total_hours", 16),
        ("Объем 16 часов, занятия по 2 часа", "session_hours", 2),
        ("программа на 72 часа", "total_hours", 72),
        ("72-часовая программа для инженеров", "total_hours", 72),
        ("трудоемкость 72 акад. часа", "total_hours", 72),
        ("объём — семьдесят два часа", "total_hours", 72),
        ("курс на сто сорок четыре часа", "total_hours", 144),
        ("на 256 часов, не больше 4 часов в день", "total_hours", 256),
        ("на 256 часов, не больше 4 часов в день", "hours_per_day", 4),
        ("до 6 ч/день", "hours_per_day", 6),
        ("Итоговая аттестация занимает 2 часа", "fa_hours", 2),
        ("на 36 часов, 4 часа на итоговую аттестацию", "fa_hours", 4),
        ("занятия по 90 минут", "session_hours", 2),
        ("двухчасовые занятия", "session_hours", 2),
        ("занятия два раза в неделю", "sessions_per_week", 2),
        ("встречи дважды в неделю", "sessions_per_week", 2),
        ("занятия раз в неделю", "sessions_per_week", 1),
        ("8 занятий по 2 часа", "total_hours", 16),
        ("итоговый тест на 10 вопросов", "test_questions", 10),
        ("тест из 20 вопросов", "test_questions", 20),
        ("портфолио из трёх материалов", "portfolio_items", 3),
        ("срок 4 недели", "weeks", 4),
        ("шестинедельный курс", "weeks", None),
        ("6-недельный курс", "weeks", 6),
        ("около двух месяцев", "weeks", 9),
        ("примерно на месяц", "weeks", 4),
        ("полгода", "weeks", 26),
    ],
)
def test_numbers(text: str, field: str, expected: float | None) -> None:
    assert getattr(extract_facts(text), field) == expected


def test_month_is_duration_not_hours() -> None:
    facts = extract_facts("Сделайте курс для студентов колледжа, примерно на месяц.")
    assert facts.total_hours is None and facts.total_candidates == []
    assert facts.weeks == 4 and facts.duration_text == "примерно на месяц"
    assert "не объём в часах" in facts.notes[0]


def test_partial_hours_are_not_total() -> None:
    facts = extract_facts("Программа на 72 часа, из них 36 часов практики и 12 часов самостоятельной работы.")
    assert facts.total_hours == 72 and facts.total_candidates == [72]
    assert facts.practice_share == 0.5


@pytest.mark.parametrize(
    ("text", "share"),
    [
        ("Практики должно быть не меньше половины всех часов.", 0.5),
        ("практики не менее 60%", 0.6),
        ("не меньше трети времени — практика", 1 / 3),
        ("практика: не меньше 8 часов. Объем 16 часов", 0.5),
        ("третий модуль — практика", None),
    ],
)
def test_practice_share(text: str, share: float | None) -> None:
    assert extract_facts(text).practice_share == share


def test_normalize_keeps_length() -> None:
    for text in ("семьдесят два часа", "двести пятьдесят шесть часов, ёлка ", "сто часов"):
        assert len(normalize(text)) == len(text)


# ---- тип, аудитория --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "explicit", "audience"),
    [
        (
            "Нужна дополнительная общеразвивающая программа для школьников 8 и 9 классов",
            [ProgramType.DOP],
            ProgramType.DOP,
        ),
        ("Программа повышения квалификации для учителей школ", [ProgramType.PK], ProgramType.PK),
        (
            "программа повышения квалификации в форме стажировки",
            [ProgramType.PK_INTERNSHIP, ProgramType.PK],
            None,
        ),
        ("курс для бухгалтеров малого бизнеса", [], ProgramType.PK),
        ("кружок для детей 10–12 лет", [], ProgramType.DOP),
        ("курс для студентов колледжа", [], None),
    ],
)
def test_program_type_cues(text: str, explicit: list[ProgramType], audience: ProgramType | None) -> None:
    facts = extract_facts(text)
    assert facts.explicit_types == explicit
    assert facts.audience_type == audience


def test_audience_cues_look_at_who_studies() -> None:
    """«Работа с детьми» у учителей не делает программу общеразвивающей: смотрим, для кого программа."""
    facts = extract_facts("Курс для учителей начальных классов о безопасной работе с детьми в сети, 24 часа.")
    assert facts.audience_type == ProgramType.PK


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Повышение квалификации", ProgramType.PK),
        ("ПК", ProgramType.PK),
        ("Повышение квалификации в форме стажировки — практика на рабочем месте", ProgramType.PK_INTERNSHIP),
        ("Общеразвивающая программа — слушатели получат сертификат", ProgramType.DOP),
        ("ДОП", ProgramType.DOP),
        ("курс", None),
    ],
)
def test_type_answer(answer: str, expected: ProgramType | None) -> None:
    assert classify_type_answer(answer) == expected


def test_grades_and_entry_level() -> None:
    facts = extract_facts("для школьников 8 и 9 классов без опыта программирования")
    assert facts.age_or_grade == "8–9 классы" and facts.entry_level_stated
    assert extract_facts("для детей 10–12 лет").age_or_grade == "10–12 лет"
    assert not extract_facts("для студентов колледжа").entry_level_stated


# ---- форма и ИА ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind", "dot"),
    [
        ("очно-заочно с применением дистанционных технологий", StudyFormKind.OCHNO_ZAOCHNAYA, True),
        ("Формат очно-заочный", StudyFormKind.OCHNO_ZAOCHNAYA, False),
        ("заочно с применением ДОТ", StudyFormKind.ZAOCHNAYA, True),
        ("занятия очно по 2 часа", StudyFormKind.OCHNAYA, False),
        ("Полностью дистанционно", StudyFormKind.ZAOCHNAYA, True),
        ("онлайн-вебинары", None, True),
        ("без указания формы", None, False),
    ],
)
def test_form(text: str, kind: StudyFormKind | None, dot: bool) -> None:
    found_kind, found_dot, _, _ = classify_form(text)
    assert (found_kind, found_dot) == (kind, dot)


@pytest.mark.parametrize(
    ("text", "form"),
    [
        ("В конце каждый ученик защищает небольшой проект", FAForm.PROJECT_DEFENSE),
        ("Итоговая аттестация в виде защиты проекта", FAForm.PROJECT_DEFENSE),
        ("в форме зачёта по портфолио из трёх материалов", FAForm.PORTFOLIO),
        ("в форме защиты отчёта о стажировке", FAForm.REPORT_DEFENSE),
        ("Экзамен с практическим заданием на стенде", FAForm.EXAM_PRACTICAL),
        ("Итоговая аттестация — тестирование", FAForm.TEST),
        ("итоговая аттестация — зачёт", FAForm.ZACHET),
        ("Нужны итоговый тест на 10 вопросов", None),
        ("курс по защите информации", None),
    ],
)
def test_final_attestation(text: str, form: FAForm | None) -> None:
    assert extract_facts(text).fa_form == form


# ---- ограничения, название, слайды -----------------------------------------------------------------


def test_flags() -> None:
    facts = extract_facts(
        "Использовать только сервисы, которые доступны в России. Данные обезличенные. "
        "Вся работа самостоятельная, аудиторных занятий нет. Без программирования."
    )
    assert facts.ru_services_only and facts.anonymized_data and facts.all_srs and facts.no_programming
    assert not any(
        getattr(extract_facts("Программа для аналитиков на 72 часа."), f)
        for f in ("ru_services_only", "anonymized_data", "all_srs", "no_programming")
    )


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("программа «Первые шаги в Python» для школьников", "Первые шаги в Python"),
        ("программа повышения квалификации на 72 часа «Анализ данных на Python»", "Анализ данных на Python"),
        ("стажировки «DevOps-инженер от Linux до CI/CD» на 256 часов", "DevOps-инженер от Linux до CI/CD"),
        ("курс по кибербезопасности", None),
    ],
)
def test_title(text: str, title: str | None) -> None:
    assert extract_title(text) == title


@pytest.mark.parametrize(
    ("text", "mode", "sessions", "module_hit"),
    [
        ("Нужны слайды для первых двух занятий.", "sessions", [1, 2], None),
        ("Подготовьте презентации к занятиям 1, 3 и 5.", "sessions", [1, 3, 5], None),
        ("Слайды к занятиям 2–4.", "sessions", [2, 3, 4], None),
        ("Слайды для первого занятия.", "sessions", [1], None),
        ("Слайды нужны для модуля про pandas.", "module", [], "Работа с таблицами в pandas"),
        ("Слайды для модуля по визуализации данных.", "module", [], "Визуализация данных и дашборды"),
    ],
)
def test_slides(text: str, mode: str, sessions: list[int], module_hit: str | None) -> None:
    scope = extract_facts(text).slides
    assert scope is not None and scope.mode == mode and scope.session_numbers == sessions
    if module_hit:
        assert re.search(scope.module_pattern or "", module_hit, re.I)


def test_presentations_as_a_skill_are_not_a_slides_request() -> None:
    assert extract_facts("научить сотрудников делать презентации для клиентов").slides is None


@pytest.mark.parametrize(
    ("label", "hit", "miss"),
    [
        ("pandas", "Обработка данных в pandas", "Таблицы Excel"),
        ("SQL для выгрузки данных", "Запросы SQL", "Основы статистики"),
        ("основы статистики", "Описательная статистика", "Визуализация"),
        ("CI/CD", "Конвейеры CI/CD в GitLab", "Контейнеры"),
        ("машинное обучение", "Машинное обучение без кода", "Обучение персонала"),
        ("сети", "Компьютерные сети и протоколы", "Linux"),
    ],
)
def test_topic_pattern(label: str, hit: str, miss: str) -> None:
    pattern = re.compile(topic_pattern(label), re.I)
    assert pattern.search(normalize(hit)) and not pattern.search(normalize(miss))


# ---- противоречия ----------------------------------------------------------------------------------


def test_text_conflicts() -> None:
    assert text_conflicts(extract_facts("Программа на 36 часов. Всего 72 часа.")) == ["conflict_hours"]
    assert text_conflicts(extract_facts("Общеразвивающая программа повышения квалификации")) == [
        "conflict_type"
    ]
    assert text_conflicts(extract_facts("Программа профессиональной переподготовки на 256 часов")) == [
        "conflict_type"
    ]
    assert text_conflicts(extract_facts("Программа повышения квалификации для школьников 7 класса")) == [
        "conflict_type"
    ]
    assert (
        text_conflicts(extract_facts("Программа повышения квалификации в форме стажировки на 256 часов"))
        == []
    )


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (RequestSpec(total_hours=16, weeks=13), ["conflict_load"]),
        (RequestSpec(total_hours=500, weeks=4), ["conflict_load"]),
        (
            RequestSpec(total_hours=256, weeks=9, hours_per_day_max=4),
            [],
        ),  # срок против нагрузки — правило календаря
        (RequestSpec(total_hours=16, weeks=8), []),
        (RequestSpec(total_hours=15, session_hours=2), ["conflict_sessions"]),
        (RequestSpec(total_hours=16, session_hours=2), []),
        (RequestSpec(total_hours=4, fa_hours=4), ["conflict_fa_hours"]),
    ],
)
def test_spec_conflicts(spec: RequestSpec, expected: list[str]) -> None:
    assert spec_conflicts(spec) == expected


def test_domain_patterns_skip_generic_words() -> None:
    from app.pipeline.request_rules import domain_patterns

    assert domain_patterns(["банк", "работа", "клиент", "сотрудники"], None) == ["банк", "клиент"]


def test_fa_text_is_a_fragment_not_the_whole_request() -> None:
    text = (
        "Нужна программа повышения квалификации на 72 часа по Excel для бухгалтеров малого бизнеса из районных "
        "отделений и филиалов компании, очно, в конце зачёт по итогам практических работ и тестирования знаний."
    )
    fa_text = extract_facts(text).fa_text or ""
    assert fa_text.startswith("в конце зачёт") and "Excel" not in fa_text
