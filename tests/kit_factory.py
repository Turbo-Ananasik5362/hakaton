"""Маленький согласованный комплект для тестов модели и ID (не для проверок validate — у них свои фикстуры)."""

from __future__ import annotations

from app.models import (
    Competency,
    Criterion,
    FAForm,
    FinalAttestation,
    HoursRow,
    Kit,
    LearningOutcome,
    Lecture,
    Module,
    ModuleKind,
    OutcomeKind,
    Program,
    ProgramType,
    Seminar,
    Source,
    SourceKind,
    SourceSection,
    Test,
    TestQuestion,
    TestScope,
    Topic,
    Verification,
)


def make_small_kit(program_type: ProgramType = ProgramType.PK, with_intro: bool = False) -> Kit:
    """2 модуля × 2 темы, 1 компетенция, 2 РО, ИА 2 ч. Всего 24 ч."""
    modules = [
        Module(id="M1", order=1, title="Основы"),
        Module(id="M2", order=2, title="Практика"),
    ]
    topics = [
        Topic(
            id="T1.1",
            module_id="M1",
            order=1,
            title="Введение",
            lo_ids=["LO1.1"],
            hours=HoursRow(lk=2, pr=2, srs=1),
        ),
        Topic(
            id="T1.2", module_id="M1", order=2, title="Данные", lo_ids=["LO1.1"], hours=HoursRow(lk=2, pr=3)
        ),
        Topic(
            id="T2.1", module_id="M2", order=1, title="Проект", lo_ids=["LO1.2"], hours=HoursRow(pr=4, srs=2)
        ),
        Topic(
            id="T2.2",
            module_id="M2",
            order=2,
            title="Отчёт",
            lo_ids=["LO1.2"],
            hours=HoursRow(lk=1, pr=2, srs=1),
        ),
    ]
    if with_intro:
        modules.insert(0, Module(id="M3", order=0, title="Вводное занятие", kind=ModuleKind.INTRO))
        topics.append(
            Topic(id="T3.1", module_id="M3", order=1, title="Знакомство", hours=HoursRow(lk=1, pr=1))
        )
    return Kit(
        program=Program(type=program_type, title="Тестовая программа", total_hours=24),
        competencies=[Competency(id="C1", title="Анализировать данные")],
        outcomes=[
            LearningOutcome(id="LO1.1", competency_id="C1", kind=OutcomeKind.KNOW, text="Знать типы данных"),
            LearningOutcome(
                id="LO1.2", competency_id="C1", kind=OutcomeKind.CAN, text="Уметь готовить отчёт"
            ),
        ],
        modules=modules,
        topics=topics,
        lectures=[Lecture(id="L1.1", module_id="M1", topic_ids=["T1.1"], title="Введение")],
        seminars=[
            Seminar(
                id="S1",
                topic_id="T2.1",
                lo_ids=["LO1.2"],
                title="Мини-проект",
                criteria=[Criterion(id="S1.K1", text="Полнота", max_points=5)],
            )
        ],
        tests=[Test(id="TS1", scope=TestScope.FINAL, question_ids=["Q2", "Q1"])],
        questions=[
            TestQuestion(
                id="Q1", test_id="TS1", topic_id="T1.1", lo_ids=["LO1.1"], stem="?", options=["a", "b"]
            ),
            TestQuestion(
                id="Q2", test_id="TS1", topic_id="T1.2", lo_ids=["LO1.1"], stem="?", options=["a", "b"]
            ),
        ],
        final_attestation=FinalAttestation(
            form=FAForm.PROJECT_DEFENSE,
            hours=2,
            criteria=[Criterion(id="FA.K1", text="Качество", max_points=10)],
        ),
        sources=[
            Source(
                id="SRC1",
                kind=SourceKind.BOOK,
                section=SourceSection.MAIN,
                verification=Verification.CATALOG,
                authors=["Иванов И. И."],
                title="Анализ данных",
                year=2024,
                publisher="Питер",
            ),
            Source(id="SRC2", kind=SourceKind.BOOK, section=SourceSection.MAIN, title="Неизвестная книга"),
        ],
    )
