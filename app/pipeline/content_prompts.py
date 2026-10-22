"""Входные данные навыков генерации содержания: каждому вызову — только свой кусок каркаса (§2.3).

Числа часов в промпты не передаются (кроме сводки для судьи): модель знает только, какие виды работы есть у темы.
ID тем и результатов обучения передаются как есть — модель ссылается на них, а код проверяет ссылки.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.core.ids import display_numbers
from app.core.registry import Registry
from app.models import (
    FAForm,
    Kit,
    Module,
    OutcomeKind,
    ProgramType,
    StudyForm,
    Topic,
    WorkKind,
)
from app.pipeline.request_rules import FA_TITLES, FORM_TITLES, TYPE_TITLES

SCHOOL_RE = re.compile(
    r"класс|школ|учени|\b(?:[5-9]|1[0-7])\s*(?:–|-|до)\s*(?:[6-9]|1[0-7])\s*лет", re.IGNORECASE
)
WORK_NAMES = {
    WorkKind.LK: "теория",
    WorkKind.PR: "практика",
    WorkKind.LB: "лабораторные работы",
    WorkKind.SRS: "самостоятельная работа",
}
KIND_NAMES = {OutcomeKind.KNOW: "знать", OutcomeKind.CAN: "уметь", OutcomeKind.MASTER: "владеть"}


def work_kinds(topic: Topic) -> list[WorkKind]:
    """Виды работы, на которые у темы есть часы (сами часы модели не передаются)."""
    return [kind for kind in WorkKind if getattr(topic.hours, kind.value) > 0]


def form_title(form: StudyForm) -> str:
    extras = [
        x for x, on in (("электронного обучения", form.eo), ("дистанционных технологий", form.dot)) if on
    ]
    tail = f" с применением {' и '.join(extras)}" if extras else ""
    return f"{FORM_TITLES[form.kind]}{tail}"


def module_label(kit: Kit, module: Module) -> str:
    number = display_numbers(kit).get(module.id, "")
    control = f"; текущий контроль: {module.control_form}" if module.control_form else ""
    return f"Модуль {number} «{module.title}» ({module.id}){control}"


def audience_word(kit: Kit) -> str:
    """Как называть обучающихся в текстах: ученики (школьники), обучающиеся (ДОП), слушатели (ДПП)."""
    if kit.program.type == ProgramType.DOP:
        return "ученики" if kit.program.audience.age_or_grade else "обучающиеся"
    return "слушатели"


def describe_program(kit: Kit, registry: Registry | None = None) -> str:
    """Паспорт и ограничения запроса — общие для всех навыков."""
    program = kit.program
    audience = program.audience
    lines = [f"Программа «{program.title}» — {TYPE_TITLES[program.type]}."]
    if program.goal:
        lines.append(f"Цель: {program.goal}")
    who = ", ".join(x for x in (audience.category, audience.age_or_grade) if x)
    word = audience_word(kit)
    lines.append(f"Аудитория: {who or 'не указана'}. В текстах называй их «{word}».")
    if audience.entry_level:
        lines.append(f"Что уже умеют: {audience.entry_level}.")
    if audience.domain:
        lines.append(f"Профессиональная среда: {audience.domain} — примеры и ситуации бери из неё.")
    lines.append(f"Форма обучения: {form_title(program.form)}.")
    if program.type == ProgramType.PK_INTERNSHIP and kit.program_hours().contact <= 0:
        lines.append(
            "Аудиторных занятий нет: все часы — самостоятельная работа стажёра на рабочем месте или стенде по заданиям "
            "руководителя стажировки (никаких лекций, практических занятий и аудиторий)."
        )
    constraints = kit.request.constraints
    if constraints.no_programming:
        lines.append("Программирование не требуется: никаких заданий на написание кода.")
    if constraints.ru_services_only:
        allowed = (
            [s.name for s in registry.ai_services() if s.available_in_ru] if registry is not None else []
        )
        names = f": {', '.join(allowed)}" if allowed else ""
        lines.append(
            f"ИИ-сервисы — только доступные в России без VPN и зарубежного номера{names}. Другие не упоминай."
        )
    if constraints.anonymized_data:
        lines.append(
            "Данные в примерах и заданиях — только обезличенные: без ФИО, телефонов, номеров документов и счетов."
        )
    for note in audience.constraints:
        lines.append(f"Ограничение: {note}.")
    if program.type == ProgramType.DOP and SCHOOL_RE.search(
        f"{audience.age_or_grade or ''} {audience.category}"
    ):
        lines.append("Язык — простой, для школьников: каждый новый термин поясняй при первом упоминании.")
    return "\n".join(lines)


def describe_outcomes(kit: Kit, lo_ids: Iterable[str] | None = None) -> str:
    wanted = set(lo_ids) if lo_ids is not None else None
    lines = [
        f"{o.id} ({KIND_NAMES[o.kind]}): {o.text}" for o in kit.outcomes if wanted is None or o.id in wanted
    ]
    return "\n".join(lines) or "—"


def describe_topics(
    kit: Kit, topics: Iterable[Topic], content_kinds: Iterable[WorkKind] = (), with_kinds: bool = True
) -> str:
    """Темы с ID, видами работы и результатами; при content_kinds — уже готовые пункты рабочей программы."""
    numbers = display_numbers(kit)
    shown_kinds = tuple(content_kinds)
    lines: list[str] = []
    for topic in topics:
        parts = [f"{topic.id} — тема {numbers.get(topic.id, '')} «{topic.title}»"]
        if with_kinds:
            parts.append("виды работы: " + (", ".join(WORK_NAMES[k] for k in work_kinds(topic)) or "—"))
        parts.append("результаты: " + (", ".join(topic.lo_ids) or "—"))
        if topic.tags:
            parts.append("ключевые слова: " + ", ".join(topic.tags))
        lines.append("; ".join(parts))
        for kind in shown_kinds:
            items = topic.content.get(kind) or []
            if items:
                lines.append(f"    {WORK_NAMES[kind]}: " + "; ".join(items))
    return "\n".join(lines) or "—"


def module_outcome_ids(kit: Kit, module: Module) -> list[str]:
    return list(dict.fromkeys(lo for t in kit.topics_of(module.id) for lo in t.lo_ids))


def describe_required_seminars(kit: Kit) -> str:
    required = kit.request.constraints.required_seminar_types
    if not required:
        return "особых требований нет"
    return "по запросу обязательно есть задания: " + "; ".join(f"«{r.label}»" for r in required)


# --------------------------------------------------------------------------------------------------
# Тексты программы и итоговая аттестация
# --------------------------------------------------------------------------------------------------

PK_TEXT_FIELDS = {
    "annotation": "аннотация программы (1.1): 4–6 предложений — для кого, что даёт, чем отличается",
    "form_description": "форма обучения (1.7): как организовано обучение, с ЭО и ДОТ — если они есть",
    "audience_requirements": "требования к слушателям (1.6): образование и входные навыки",
    "mto": "материально-техническое обеспечение (3.1): 4–6 пунктов — помещения, оборудование, доступ к сети",
    "software": "программное обеспечение и информационные системы (3.2.5): 3–8 позиций, только реальные продукты",
    "org_process": "организация образовательного процесса (3.3): 3–5 предложений",
    "staffing_requirements": "кадровое обеспечение (3.4): требования к преподавателям, без ФИО",
    "satisfaction": "анализ удовлетворённости слушателей (2.5.3): как и когда проводится анкетирование",
}
DOP_TEXT_FIELDS = {
    "relevance": "актуальность программы (1.1): 3–5 предложений",
    "form_description": "пояснительная записка: форма и формат занятий, условия обучения",
    "audience_requirements": "категория обучающихся (1.2): возраст или классы, входной уровень",
    "methodical": "методические материалы (2.4): методы и приёмы, формы занятий, дидактические материалы",
    "mto": "материально-технические условия (3.1): 4–6 пунктов",
    "software": "программное обеспечение (3.2): 3–6 позиций, только реальные продукты",
    "staffing_requirements": "кадровое обеспечение (3.4): требования к педагогу, без ФИО",
}


def text_fields(kit: Kit) -> dict[str, str]:
    return DOP_TEXT_FIELDS if kit.program.type == ProgramType.DOP else PK_TEXT_FIELDS


def describe_text_fields(kit: Kit) -> str:
    return "\n".join(f"- {name}: {what}" for name, what in text_fields(kit).items())


def describe_modules(kit: Kit) -> str:
    numbers = display_numbers(kit)
    return "\n".join(f"Модуль {numbers.get(m.id, '')}: {m.title}" for m in kit.modules_sorted()) or "—"


FA_RULES: dict[FAForm, str] = {
    FAForm.PROJECT_DEFENSE: (
        "Защита проекта: requirements — 4–6 требований к проекту; expected_result — что сдаёт обучающийся; "
        "example_task — пример темы проекта (title, description, deliverable)."
    ),
    FAForm.PORTFOLIO: (
        "Зачёт по портфолио: portfolio_items — материалы портфолио, у каждого 2–4 требования; requirements — общие "
        "требования к оформлению."
    ),
    FAForm.EXAM_PRACTICAL: (
        "Экзамен с практическим заданием: example_task — пример задания: environment (стенд, ПО, исходные данные), "
        "description (что сделать), deliverable (что сдать); requirements — условия экзамена."
    ),
    FAForm.REPORT_DEFENSE: (
        "Защита отчёта: report_requirements — структура отчёта, 5–8 разделов; requirements — требования к оформлению "
        "и к отзыву руководителя."
    ),
    FAForm.TEST: (
        "Итоговое тестирование: requirements — как проводится тест (число попыток, время, порог); вопросы теста "
        "готовит отдельный шаг, сюда их не пиши."
    ),
    FAForm.ZACHET: "Зачёт: requirements — что обучающийся выполняет на зачёте; example_task — пример задания.",
    FAForm.OTHER: "Форма — как в запросе: requirements, пример задания и критерии под эту форму.",
}


def describe_fa_line(kit: Kit) -> str:
    """Одна строка о форме итоговой аттестации для прозаических разделов программы."""
    fa = kit.final_attestation
    form = fa.form if fa is not None else (kit.request.fa_form or FAForm.ZACHET)
    line = f"Итоговая аттестация: {FA_TITLES[form]}"
    detail = (fa.form_text if fa is not None and fa.form_text else "") or (kit.request.fa_text or "")
    if detail:
        line += f" ({detail.strip().rstrip('.')})"
    return line + ". Другие формы итоговой аттестации в текстах не называй."


def describe_fa(kit: Kit) -> str:
    fa = kit.final_attestation
    request = kit.request
    form = fa.form if fa is not None else (request.fa_form or FAForm.ZACHET)
    lines = [f"Форма: {FA_TITLES[form]}."]
    if fa is not None and fa.form_text:
        lines.append(f"Как сказано в программе: {fa.form_text}.")
    if request.fa_text:
        lines.append(f"Как сказано в запросе: {request.fa_text}.")
    lines.append(FA_RULES.get(form, FA_RULES[FAForm.OTHER]))
    if form == FAForm.PORTFOLIO and request.portfolio_items_count:
        lines.append(f"Материалов портфолио — ровно {request.portfolio_items_count}.")
    if request.constraints.anonymized_data:
        lines.append(
            "data_requirements — требования к данным проекта, обязательно: данные обезличены; expected_result — "
            "ожидаемый результат."
        )
    lines.append(
        "criteria — 3–5 измеримых критериев с баллами; pass_rule — порог «зачтено» в баллах; procedure — 3–6 шагов "
        "процедуры; lo_ids — проверяемые результаты из списка."
    )
    return "\n".join(lines)


MAX_EARLIER_TOPICS = 30


def earlier_topics(kit: Kit, module: Module) -> str:
    """Темы предыдущих модулей — что обучающиеся уже изучили к началу модуля (задания и примеры опираются только
    на изученное)."""
    numbers = display_numbers(kit)
    earlier: list[str] = []
    for other in kit.modules_sorted():
        if other.id == module.id:
            break
        earlier += [f"{numbers.get(t.id, '')} {t.title}" for t in kit.topics_of(other.id)]
    if not earlier:
        return "Это первый модуль: обучающиеся начинают с его первой темы."
    return "Уже изучено в предыдущих модулях: " + "; ".join(earlier[-MAX_EARLIER_TOPICS:]) + "."
