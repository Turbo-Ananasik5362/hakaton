"""Граф зависимостей комплекта и «что пересобрать» при точечной правке (docs/ARCHITECTURE.md §5).

Рёбра (зависимый → от чего зависит), §5.1:
    LO → C;  Topic → Module, LO*;  Lecture → Topic*, Module;  Seminar → Topic, LO*;
    TestQuestion → Topic, LO*, Test;  InterviewQuestion → Module, LO*;  FA → LO*;  Source → Topic*.
Обратные индексы (`DependencyGraph.dependents`) отвечают на вопрос «кто зависит от объекта».

`affected()` переводит операцию правки в набор объектов, которые пересобирает модель, — по таблице §5.2.
Что пересчитывает код (часы, календарь, слайды, проверка, выгрузки), здесь не перечисляется: это делается всегда.
`kind_changes()` — следствия правки часов: у темы появился или исчез вид работы (лекция, практика).
Без LLM и без изменения комплекта.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.core.ids import parse_id
from app.models import Kit, PatchOperation, Topic, WorkKind


class GraphError(ValueError):
    """Операция не подходит к объекту (например, «усложнить» тему без вопросов). Текст — для методиста."""


@dataclass
class DependencyGraph:
    """Обратные рёбра: ID объекта → ID объектов, которые от него зависят."""

    dependents: dict[str, set[str]]

    @classmethod
    def build(cls, kit: Kit) -> DependencyGraph:
        edges: dict[str, set[str]] = defaultdict(set)

        def link(dependent: str, *depends_on: str | None) -> None:
            for target in depends_on:
                if target:
                    edges[target].add(dependent)

        for outcome in kit.outcomes:
            link(outcome.id, outcome.competency_id)
        for topic in kit.topics:
            link(topic.id, topic.module_id, *topic.lo_ids)
        for lecture in kit.lectures:
            link(lecture.id, lecture.module_id, *lecture.topic_ids)
        for seminar in kit.seminars:
            link(seminar.id, seminar.topic_id, *seminar.lo_ids)
        for question in kit.questions:
            link(question.id, question.topic_id, question.test_id, *question.lo_ids)
        for iq in kit.interview_questions:
            link(iq.id, iq.module_id, *iq.lo_ids)
        if kit.final_attestation is not None:
            link("FA", *kit.final_attestation.lo_ids)
        for source in kit.sources:
            link(source.id, *source.topic_ids)
        return cls(dict(edges))

    def direct(self, obj_id: str) -> set[str]:
        return set(self.dependents.get(obj_id, set()))

    def closure(self, ids: Iterable[str]) -> set[str]:
        """Всё, что зависит от ids прямо или через цепочку (без самих ids)."""
        start = set(ids)
        seen: set[str] = set()
        stack = list(start)
        while stack:
            for dep in self.dependents.get(stack.pop(), ()):
                if dep not in seen:
                    seen.add(dep)
                    stack.append(dep)
        return seen - start


@dataclass
class Affected:
    """Что пересобирает модель (ID сохраняются) и что создаётся или удаляется после правки структуры."""

    content: list[str] = field(default_factory=list)  # темы: пункты рабочей программы
    lectures: list[str] = field(default_factory=list)
    seminars: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    interview: list[str] = field(default_factory=list)
    final_attestation: bool = False
    new_lectures: list[str] = field(default_factory=list)  # темы, которым нужна новая лекция
    new_seminars: list[str] = field(default_factory=list)  # темы, которым нужно новое задание
    retire: list[str] = field(default_factory=list)  # объекты, которые удаляются
    detach: dict[str, list[str]] = field(default_factory=dict)  # лекция → темы, которые из неё убираются
    drop_kinds: dict[str, list[WorkKind]] = field(default_factory=dict)  # тема → виды работы без часов

    def merge(self, other: Affected) -> Affected:
        def union(a: list[str], b: list[str]) -> list[str]:
            return list(dict.fromkeys([*a, *b]))

        detach = {k: list(v) for k, v in self.detach.items()}
        for key, value in other.detach.items():
            detach[key] = union(detach.get(key, []), value)
        drop = {k: list(v) for k, v in self.drop_kinds.items()}
        for key, kinds in other.drop_kinds.items():
            drop[key] = list(dict.fromkeys([*drop.get(key, []), *kinds]))
        retire = union(self.retire, other.retire)
        return Affected(
            content=[x for x in union(self.content, other.content) if x not in retire],
            lectures=[x for x in union(self.lectures, other.lectures) if x not in retire],
            seminars=[x for x in union(self.seminars, other.seminars) if x not in retire],
            questions=[x for x in union(self.questions, other.questions) if x not in retire],
            interview=[x for x in union(self.interview, other.interview) if x not in retire],
            final_attestation=self.final_attestation or other.final_attestation,
            new_lectures=union(self.new_lectures, other.new_lectures),
            new_seminars=union(self.new_seminars, other.new_seminars),
            retire=retire,
            detach=detach,
            drop_kinds=drop,
        )

    def llm_targets(self) -> list[str]:
        """Объекты, которые пересобирает модель (для оценки и отчёта)."""
        return [
            *self.content,
            *self.lectures,
            *self.seminars,
            *self.questions,
            *self.interview,
            *(["FA"] if self.final_attestation else []),
            *self.new_lectures,
            *self.new_seminars,
        ]

    def is_empty(self) -> bool:
        return not (self.llm_targets() or self.retire or self.detach or self.drop_kinds)


def kind_of(obj_id: str) -> str:
    return "FA" if obj_id == "FA" else parse_id(obj_id)[0]


# --------------------------------------------------------------------------------------------------
# Операции (§5.2)
# --------------------------------------------------------------------------------------------------


def affected(kit: Kit, operation: PatchOperation, target_id: str) -> Affected:
    """Что пересобирает модель при операции над объектом target_id (комплект — уже после правки структуры:
    новое название темы, новая формулировка РО). Правка часов, добавление и удаление темы дополнительно
    дают kind_changes() и topic_removal()."""
    graph = DependencyGraph.build(kit)
    kind = kind_of(target_id)
    if operation in (PatchOperation.REPLACE_TOPIC, PatchOperation.ADD_TOPIC) or (
        operation == PatchOperation.REGENERATE and kind == "T"
    ):
        _expect(kind, "T", "тема")
        return _topic_materials(kit, graph, target_id)
    if operation == PatchOperation.HARDEN_TEST:
        questions = _questions_of(kit, graph, target_id)
        if not questions:
            raise GraphError("У этого объекта нет тестовых вопросов — усложнять нечего.")
        return Affected(questions=questions)
    if operation == PatchOperation.REPLACE_LO or (operation == PatchOperation.REGENERATE and kind == "LO"):
        _expect(kind, "LO", "результат обучения")
        return _outcome_materials(kit, graph, target_id)
    if operation == PatchOperation.REMOVE_TOPIC:
        _expect(kind, "T", "тема")
        return topic_removal(kit, target_id)
    if operation == PatchOperation.CHANGE_HOURS:
        _expect(kind, "T", "тема")
        return Affected()  # следствия — kind_changes() после пересчёта часов
    if operation == PatchOperation.REGENERATE:
        return _regenerate(kit, graph, target_id, kind)
    raise GraphError(f"Неизвестная операция правки: {operation}")


def _expect(kind: str, wanted: str, what: str) -> None:
    if kind != wanted:
        raise GraphError(f"Эта правка относится к объекту вида «{what}», а указан другой объект.")


def _sorted(kit: Kit, ids: Iterable[str]) -> list[str]:
    """ID в порядке комплекта (стабильный порядок пересборки и отчёта)."""
    wanted = set(ids)
    return [obj_id for obj_id in kit.index() if obj_id in wanted]


def _topic_materials(kit: Kit, graph: DependencyGraph, topic_id: str) -> Affected:
    """Тема: её содержание, лекции, где она есть, задания и вопросы по ней; вопросы собеседования модуля — если
    касаются РО, которые в модуле раскрывает только эта тема."""
    deps = graph.direct(topic_id)
    topic = next(t for t in kit.topics if t.id == topic_id)
    lectures = [x for x in deps if kind_of(x) == "L"]
    seminars = [x for x in deps if kind_of(x) == "S"]
    questions = [x for x in deps if kind_of(x) == "Q"]
    others = {lo for t in kit.topics_of(topic.module_id) if t.id != topic_id for lo in t.lo_ids}
    own = set(topic.lo_ids) - others
    interview = [
        iq.id for iq in kit.interview_questions if iq.module_id == topic.module_id and own & set(iq.lo_ids)
    ]
    new_lectures = [topic_id] if topic.hours.lk > 0 and not lectures else []
    new_seminars = [topic_id] if topic.hours.pr + topic.hours.lb > 0 and not seminars else []
    return Affected(
        content=[topic_id],
        lectures=_sorted(kit, lectures),
        seminars=_sorted(kit, seminars),
        questions=_sorted(kit, questions),
        interview=_sorted(kit, interview),
        new_lectures=new_lectures,
        new_seminars=new_seminars,
    )


def _outcome_materials(kit: Kit, graph: DependencyGraph, lo_id: str) -> Affected:
    """РО: все оценочные средства с ним (задания, вопросы, вопросы собеседования); темы и ИА не трогаются."""
    deps = graph.direct(lo_id)
    return Affected(
        seminars=_sorted(kit, (x for x in deps if kind_of(x) == "S")),
        questions=_sorted(kit, (x for x in deps if kind_of(x) == "Q")),
        interview=_sorted(kit, (x for x in deps if kind_of(x) == "IQ")),
    )


def _questions_of(kit: Kit, graph: DependencyGraph, target_id: str) -> list[str]:
    kind = kind_of(target_id)
    if kind == "Q":
        return [target_id]
    if kind in ("TS", "T"):
        return _sorted(kit, (x for x in graph.direct(target_id) if kind_of(x) == "Q"))
    if kind == "M":
        topics = {t.id for t in kit.topics_of(target_id)}
        return [q.id for q in kit.questions if q.topic_id in topics]
    raise GraphError("Усложнить можно тест, вопрос, тему или модуль — укажите, например, «итоговый тест».")


def _regenerate(kit: Kit, graph: DependencyGraph, target_id: str, kind: str) -> Affected:
    if kind == "L":
        return Affected(lectures=[target_id])
    if kind == "S":
        return Affected(seminars=[target_id])
    if kind == "Q":
        return Affected(questions=[target_id])
    if kind == "IQ":
        return Affected(interview=[target_id])
    if kind == "TS":
        return Affected(questions=_questions_of(kit, graph, target_id))
    if kind == "FA":
        return Affected(final_attestation=True)
    if kind == "M":
        result = Affected()
        for topic in kit.topics_of(target_id):
            result = result.merge(_topic_materials(kit, graph, topic.id))
        result.interview = [iq.id for iq in kit.interview_questions if iq.module_id == target_id]
        return result
    raise GraphError(
        "Пересобрать можно тему, модуль, лекцию, задание, тест, вопрос, результат обучения или итоговую "
        "аттестацию."
    )


# --------------------------------------------------------------------------------------------------
# Следствия правки структуры
# --------------------------------------------------------------------------------------------------


def _kinds(topic: Topic) -> set[WorkKind]:
    return {
        kind for kind in (WorkKind.LK, WorkKind.PR, WorkKind.LB, WorkKind.SRS) if topic.hours.get(kind) > 0
    }


def kind_changes(before: Kit, after: Kit) -> Affected:
    """Темы, у которых после пересчёта часов появился или исчез вид работы (§5.2, «Изменить часы»): появилась
    теория — новая лекция, практика — новое задание, новый вид — пункты рабочей программы; исчезла теория —
    тема убирается из лекции (лекция без тем удаляется), исчезла практика — задания темы удаляются."""
    old = {t.id: t for t in before.topics}
    result = Affected()
    for topic in after.topics:
        prev = old.get(topic.id)
        if prev is None or prev.hours.model_dump() == topic.hours.model_dump():
            continue
        gained, lost = _kinds(topic) - _kinds(prev), _kinds(prev) - _kinds(topic)
        lectures = [lec for lec in after.lectures if topic.id in lec.topic_ids]
        seminars = [s for s in after.seminars if s.topic_id == topic.id]
        if WorkKind.LK in gained and not lectures:
            result.new_lectures.append(topic.id)
        if gained & {WorkKind.PR, WorkKind.LB} and not seminars:
            result.new_seminars.append(topic.id)
        if gained:
            result.content.append(topic.id)  # пункты рабочей программы для нового вида работы
        if WorkKind.LK in lost:
            for lecture in lectures:
                if len(lecture.topic_ids) == 1:
                    result.retire.append(lecture.id)
                else:
                    result.detach.setdefault(lecture.id, []).append(topic.id)
        if not _kinds(topic) & {WorkKind.PR, WorkKind.LB}:
            result.retire += [s.id for s in seminars]
        if lost:
            result.drop_kinds[topic.id] = sorted(lost, key=lambda k: k.value)
    return result


def topic_removal(kit: Kit, topic_id: str) -> Affected:
    """Удаление темы: её задания и вопросы удаляются, лекции только по ней — тоже; из общих лекций тема убирается."""
    result = Affected(retire=[topic_id])
    for lecture in kit.lectures:
        if topic_id in lecture.topic_ids:
            if len(lecture.topic_ids) == 1:
                result.retire.append(lecture.id)
            else:
                result.detach.setdefault(lecture.id, []).append(topic_id)
    result.retire += [s.id for s in kit.seminars if s.topic_id == topic_id]
    result.retire += [q.id for q in kit.questions if q.topic_id == topic_id]
    return result
