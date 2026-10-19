from __future__ import annotations

import pytest

from app.core.ids import (
    AmbiguousReference,
    IdAllocator,
    UnknownReference,
    display_label,
    display_numbers,
    parse_id,
    resolve_ref,
)
from app.models import Test, TestScope, Topic
from tests.kit_factory import make_small_kit


def test_parse_id() -> None:
    assert parse_id("T2.3") == ("T", (2, 3))
    assert parse_id("LO1.2") == ("LO", (1, 2))
    assert parse_id("TS1") == ("TS", (1,))
    assert parse_id("SRC12") == ("SRC", (12,))
    assert parse_id("S4") == ("S", (4,))
    assert parse_id("FA") == ("FA", ())
    with pytest.raises(ValueError):
        parse_id("тема 2.3")


def test_allocator_next_ids() -> None:
    alloc = IdAllocator(make_small_kit())
    assert alloc.module() == "M3"
    assert alloc.topic("M1") == "T1.3"
    assert alloc.topic("M1") == "T1.4"
    assert alloc.topic("M3") == "T3.1"
    assert alloc.outcome("C1") == "LO1.3"
    assert alloc.competency() == "C2"
    assert alloc.lecture("M2") == "L2.1"
    assert alloc.seminar() == "S2"
    assert alloc.criterion("S1") == "S1.K2"
    assert alloc.criterion("FA") == "FA.K2"
    assert alloc.portfolio_item() == "FA.P1"
    assert alloc.test() == "TS2"
    assert alloc.question() == "Q3"
    assert alloc.source() == "SRC3"


def test_allocator_never_reuses_retired_ids() -> None:
    kit = make_small_kit()
    kit.topics = [t for t in kit.topics if t.id != "T1.2"]
    kit.retired_ids.append("T1.2")
    assert IdAllocator(kit).topic("M1") == "T1.3"


def test_allocator_empty() -> None:
    alloc = IdAllocator()
    assert alloc.module() == "M1"
    assert alloc.topic("M1") == "T1.1"
    assert alloc.outcome("C1") == "LO1.1"


def test_display_numbers_follow_order_not_id() -> None:
    kit = make_small_kit()
    # новая тема T1.3 вставлена первой в модуле 1 — номера в документе сдвигаются, ID нет
    kit.topics.append(Topic(id="T1.3", module_id="M1", order=0, title="Новая"))
    numbers = display_numbers(kit)
    assert numbers["T1.3"] == "1.1"
    assert numbers["T1.1"] == "1.2"
    assert numbers["T1.2"] == "1.3"
    assert numbers["M2"] == "2"


def test_intro_module_is_zero() -> None:
    kit = make_small_kit(with_intro=True)
    numbers = display_numbers(kit)
    assert numbers["M3"] == "0"
    assert numbers["T3.1"] == "0.1"
    assert numbers["M1"] == "1"
    assert display_label(kit, "T3.1") == "Тема 0.1"


def test_question_numbers_follow_test_order() -> None:
    numbers = display_numbers(make_small_kit())
    assert numbers["Q2"] == "1"
    assert numbers["Q1"] == "2"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("T2.1", "T2.1"),
        ("t2.1", "T2.1"),
        ("тема 2.1", "T2.1"),
        ("Тему 1.2", "T1.2"),
        ("модуль 2", "M2"),
        ("раздел 1", "M1"),
        ("лекция 1.1", "L1.1"),
        ("задание 1", "S1"),
        ("компетенция 1", "C1"),
        ("результат обучения 1.2", "LO1.2"),
        ("итоговый тест", "TS1"),
        ("тест", "TS1"),
        ("вопрос 1", "Q2"),
        ("итоговая аттестация", "FA"),
        ("ИА", "FA"),
        ("«тема 2.2»", "T2.2"),
    ],
)
def test_resolve_ref(text: str, expected: str) -> None:
    assert resolve_ref(make_small_kit(), text) == expected


def test_resolve_unknown() -> None:
    kit = make_small_kit()
    with pytest.raises(UnknownReference):
        resolve_ref(kit, "тема 7.1")
    with pytest.raises(UnknownReference):
        resolve_ref(kit, "T9.9")
    with pytest.raises(UnknownReference):
        resolve_ref(kit, "сделай лучше")


def test_resolve_ambiguous_question_across_tests() -> None:
    kit = make_small_kit()
    kit.tests.append(Test(id="TS2", scope=TestScope.MODULE, question_ids=["Q1"]))
    kit.tests[0].question_ids = ["Q2"]
    with pytest.raises(AmbiguousReference):
        resolve_ref(kit, "вопрос 1")
    assert resolve_ref(kit, "тест") == "TS1"  # итоговый тест один — он и выбирается
    kit.tests[1].scope = TestScope.FINAL
    with pytest.raises(AmbiguousReference):
        resolve_ref(kit, "итоговый тест")
