"""Шаг clarify: уточняющие вопросы, применение ответов, умолчания (docs/ARCHITECTURE.md §2.3).

Когда спрашивать (общее правило для любого запроса):
- есть блокирующий пробел — не указан объём в часах, тип программы не определяется никак, или в запросе
  противоречие (несколько объёмов, разные типы программ, объём не сходится со сроком или длиной занятия);
- тогда в тот же раунд задаются и остальные недостающие ключевые вопросы (тип, если он не назван прямо;
  слушатели и их уровень; форма обучения; форма итоговой аттестации) — не больше 5 вопросов;
- если блокирующего пробела нет, методиста не дёргают: недостающее закрывается умолчаниями шаблона,
  и каждое умолчание записывается в assumptions (его видно в каркасе и в отчёте).

Вопросы о параметрах — шаблоны в коде, простым языком и с вариантами. LLM (навык app/skills/clarify.md) —
только для противоречий, которые нужно объяснить словами запроса (например, «16 часов, но 3 месяца»);
если модель недоступна, используется шаблон.
"""

from __future__ import annotations

import hashlib
import logging
import re

from app.core.hours import plural
from app.llm import LLMClient, LLMError
from app.models import (
    ClarifyDraft,
    ClarifyQuestion,
    FAForm,
    ProgramType,
    RequestSpec,
    StudyForm,
    StudyFormKind,
)
from app.pipeline.base import StepContext
from app.pipeline.request_rules import (
    FA_TITLES,
    FLAGS,
    FORM_TITLES,
    MAX_WEEKLY_HOURS,
    MIN_WEEKLY_HOURS,
    TYPE_TITLES,
    RequestFacts,
    classify_fa,
    classify_form,
    classify_type_answer,
    extract_age_or_grade,
    extract_facts,
    first_number,
    normalize,
    spec_conflicts,
)
from app.skills import load_skill

logger = logging.getLogger("karkas.clarify")

MAX_QUESTIONS = 5
CORE_PARAMS = ("program_type", "hours", "audience", "form", "final_attestation")
TEMPLATE_CONFLICTS = (
    "conflict_hours",
    "conflict_type",
    "conflict_sessions",
    "conflict_fa_hours",
    "conflict_volume",
)
# вопрос о противоречии заменяет вопрос о параметре
SUPERSEDES = {
    "conflict_type": "program_type",
    "conflict_hours": "hours",
    "conflict_sessions": "hours",
    "conflict_volume": "hours",
}

DEFAULT_HOURS = {ProgramType.PK: 36.0, ProgramType.PK_INTERNSHIP: 72.0, ProgramType.DOP: 16.0}
DEFAULT_FA = {
    ProgramType.PK: FAForm.ZACHET,
    ProgramType.PK_INTERNSHIP: FAForm.REPORT_DEFENSE,
    ProgramType.DOP: FAForm.PROJECT_DEFENSE,
}
HOURS_OPTIONS = {
    ProgramType.PK: [36, 72, 144],
    ProgramType.PK_INTERNSHIP: [72, 144, 256],
    ProgramType.DOP: [16, 24, 36, 72],
    None: [16, 36, 72, 144],
}
TYPE_OPTIONS = {
    ProgramType.PK: "Повышение квалификации — слушатели получат удостоверение",
    ProgramType.PK_INTERNSHIP: "Повышение квалификации в форме стажировки — практика на рабочем месте",
    ProgramType.DOP: "Общеразвивающая программа — слушатели получат сертификат",
}
FA_OPTIONS = {
    ProgramType.PK: ["Зачёт по итоговому тесту", "Экзамен с практическим заданием", "Защита проекта"],
    ProgramType.PK_INTERNSHIP: ["Защита отчёта о стажировке", "Экзамен с практическим заданием"],
    ProgramType.DOP: ["Защита проекта", "Итоговый тест"],
    None: ["Зачёт по итоговому тесту", "Экзамен с практическим заданием", "Защита проекта"],
}
FORM_OPTIONS = ["Очно", "Очно-заочно, часть занятий дистанционно", "Полностью дистанционно"]
AUDIENCE_OPTIONS = ["Начинающие, без опыта в теме", "Знают основы темы", "Уже работают в этой области"]
DEFER_RE = re.compile(
    r"на\s+(?:ваше|твое|ваш)\s+усмотрени|не\s+важно|без\s+разницы|\bлюб(?:ой|ая|ое|ые)\b|как\s+(?:считаете|сочтете|удобно)"
    r"|решите\s+сами|на\s+выбор",
    FLAGS,
)


def is_conflict(key: str) -> bool:
    return key.startswith("conflict_")


def needs_questions(spec: RequestSpec) -> bool:
    """Есть ли блокирующий пробел (иначе вопросов не задаём, а берём умолчания)."""
    return "hours" in spec.missing or spec.program_type is None or any(is_conflict(m) for m in spec.missing)


def weeks_label(weeks: int | None) -> str:
    n = int(weeks or 0)
    return f"{n} {plural(n, 'неделя', 'недели', 'недель')}"


def hours_label(value: float) -> str:
    n = int(value)
    if value != n:
        return f"{str(value).replace('.', ',')} ч"
    rest = n % 100
    if 11 <= rest <= 14:
        word = "часов"
    elif n % 10 == 1:
        word = "час"
    elif 2 <= n % 10 <= 4:
        word = "часа"
    else:
        word = "часов"
    return f"{n} {word}"


# --------------------------------------------------------------------------------------------------
# Вопросы
# --------------------------------------------------------------------------------------------------


def _type_options(spec: RequestSpec) -> list[str]:
    order = [ProgramType.PK, ProgramType.PK_INTERNSHIP, ProgramType.DOP]
    if spec.program_type in order:
        order.remove(spec.program_type)
        order.insert(0, spec.program_type)
    return [TYPE_OPTIONS[t] for t in order]


def describe_conflict(key: str, spec: RequestSpec, facts: RequestFacts) -> str:
    total = spec.total_hours
    if key == "conflict_load" and total and spec.weeks:
        weekly = total / spec.weeks
        period = spec.duration_text or weeks_label(spec.weeks)
        side = "слишком мало" if weekly < MIN_WEEKLY_HOURS else "слишком много"
        return (
            f"Объём {hours_label(total)} на срок «{period}» — это около {weekly:.1f} ч в неделю, {side} "
            f"(разумно от {MIN_WEEKLY_HOURS:g} до {MAX_WEEKLY_HOURS:g} ч в неделю)."
        )
    if key == "conflict_hours":
        return (
            "В запросе указано несколько объёмов: "
            + ", ".join(hours_label(v) for v in facts.total_candidates)
            + "."
        )
    if key == "conflict_sessions" and total and spec.session_hours:
        return f"Объём {hours_label(total)} не делится на занятия по {hours_label(spec.session_hours)}."
    if key == "conflict_fa_hours" and total and spec.fa_hours is not None:
        return f"На итоговую аттестацию отведено {hours_label(spec.fa_hours)} — не меньше всего объёма."
    if key == "conflict_type":
        return "В запросе есть признаки разных типов программ."
    if key == "conflict_volume" and total:
        return f"Объём {hours_label(total)} слишком мал: после итоговой аттестации на темы почти не остаётся времени."
    return key


def template_question(key: str, spec: RequestSpec, facts: RequestFacts) -> ClarifyQuestion | None:
    # пока тип прямо не назван, варианты часов и ИА — нейтральные, а не по догадке модели
    ptype = None if "program_type" in spec.missing or "conflict_type" in spec.missing else spec.program_type
    if key == "program_type":
        return ClarifyQuestion(
            id=key,
            text="Какую программу готовим? От этого зависят шаблон документа и то, что слушатели получат в конце.",
            options=_type_options(spec),
        )
    if key == "hours":
        text = "Какой объём программы в часах?"
        if spec.duration_text or spec.weeks:
            period = spec.duration_text or weeks_label(spec.weeks)
            text += f" Срок «{period}» учтём в расписании, но объём нужен именно в часах."
        return ClarifyQuestion(
            id=key, text=text, options=[hours_label(h) for h in HOURS_OPTIONS.get(ptype, HOURS_OPTIONS[None])]
        )
    if key == "audience":
        who = spec.audience.category.strip()
        text = (
            f"Расскажите подробнее о слушателях ({who}): что они уже знают и умеют по теме курса?"
            if who
            else "Кто будет учиться и что они уже знают и умеют по теме курса?"
        )
        return ClarifyQuestion(id=key, text=text, options=list(AUDIENCE_OPTIONS))
    if key == "form":
        return ClarifyQuestion(id=key, text="Как будут проходить занятия?", options=list(FORM_OPTIONS))
    if key == "final_attestation":
        return ClarifyQuestion(
            id=key,
            text="Как слушатели подтвердят результат в конце обучения (итоговая аттестация)?",
            options=list(FA_OPTIONS.get(ptype, FA_OPTIONS[None])),
        )
    if key == "conflict_hours":
        values = facts.total_candidates
        return ClarifyQuestion(
            id=key,
            text=describe_conflict(key, spec, facts) + " Какой объём верный?",
            options=[hours_label(v) for v in values],
        )
    if key == "conflict_type":
        if facts.retraining:
            text = "Шаблона профессиональной переподготовки у нас нет. Какую программу готовим?"
        elif facts.school_audience:
            text = (
                "Повышение квалификации проходят те, у кого есть или кто получает среднее профессиональное или высшее "
                "образование, а слушатели — школьники. Какую программу готовим?"
            )
        else:
            text = "В запросе упомянуты разные типы программ. Какую готовим?"
        return ClarifyQuestion(id=key, text=text, options=_type_options(spec))
    if key == "conflict_sessions" and spec.total_hours and spec.session_hours:
        size = spec.session_hours
        low = int(spec.total_hours // size)
        high = low + 1
        return ClarifyQuestion(
            id=key,
            text=describe_conflict(key, spec, facts) + " Как поступим?",
            options=[
                f"{hours_label(low * size)} — {low} занятий",
                f"{hours_label(high * size)} — {high} занятий",
            ],
        )
    if key == "conflict_fa_hours":
        return ClarifyQuestion(
            id=key,
            text=describe_conflict(key, spec, facts) + " Сколько часов отвести на итоговую аттестацию?",
            options=["2 часа", "4 часа"],
        )
    if key == "conflict_volume":
        return ClarifyQuestion(
            id=key,
            text=describe_conflict(key, spec, facts) + " Какой объём программы в часах?",
            options=[hours_label(h) for h in HOURS_OPTIONS.get(ptype, HOURS_OPTIONS[None])],
        )
    if key == "conflict_load" and spec.total_hours:
        period = spec.duration_text or weeks_label(spec.weeks)
        return ClarifyQuestion(
            id=key,
            text=describe_conflict(key, spec, facts) + " Что важнее сохранить?",
            options=[
                f"Объём {hours_label(spec.total_hours)}, срок можно изменить",
                f"Срок «{period}», объём нужно изменить",
            ],
        )
    return None


def clarify_key(spec: RequestSpec, conflicts: list[str]) -> str:
    raw = " ".join(spec.raw_text.split()) + "|" + ",".join(conflicts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


async def _llm_questions(
    spec: RequestSpec, facts: RequestFacts, conflicts: list[str], llm: LLMClient
) -> dict[str, ClarifyQuestion]:
    skill = load_skill("clarify")
    listing = "\n".join(f"- {key}: {describe_conflict(key, spec, facts)}" for key in conflicts)
    messages = skill.messages(request=spec.raw_text, conflicts=listing, max_questions=str(len(conflicts)))
    draft = await llm.complete_json(
        "clarify",
        skill.model,
        messages,
        ClarifyDraft,
        max_tokens=skill.max_tokens,
        temperature=skill.temperature,
        fixture_key=clarify_key(spec, conflicts),
    )
    result: dict[str, ClarifyQuestion] = {}
    unmatched = [q for q in draft.questions if q.id not in conflicts]
    for question in draft.questions:
        if question.id in conflicts and question.text.strip():
            result[question.id] = question
    free = [key for key in conflicts if key not in result]
    for key, question in zip(
        free, unmatched, strict=False
    ):  # модель переименовала id — сопоставляем по порядку
        if question.text.strip():
            result[key] = question.model_copy(update={"id": key})
    return result


async def run_clarify(
    spec: RequestSpec, ctx: StepContext | None, round_no: int, llm: LLMClient | None
) -> list[ClarifyQuestion]:
    if not needs_questions(spec):
        return []
    facts = extract_facts(spec.raw_text)
    conflicts = [m for m in spec.missing if is_conflict(m)]
    superseded = {SUPERSEDES[c] for c in conflicts if c in SUPERSEDES}
    params = [p for p in CORE_PARAMS if p in spec.missing and p not in superseded]
    if spec.program_type is None and "program_type" not in params and "program_type" not in superseded:
        params.insert(0, "program_type")

    questions: dict[str, ClarifyQuestion] = {}
    for_llm = [c for c in conflicts if c not in TEMPLATE_CONFLICTS]
    if for_llm and llm is not None:
        try:
            questions.update(await _llm_questions(spec, facts, for_llm, llm))
        except LLMError as exc:
            logger.warning("clarify: модель недоступна, вопросы по шаблонам: %s", exc)
    for key in conflicts + params:
        if key not in questions:
            question = template_question(key, spec, facts)
            if question is not None:
                questions[key] = question
    ordered = [questions[k] for k in conflicts + params if k in questions]
    return [_tidy(q) for q in ordered[:MAX_QUESTIONS]]


def _tidy(question: ClarifyQuestion) -> ClarifyQuestion:
    options = [o.strip() for o in question.options if o.strip()][:5]
    return ClarifyQuestion(id=question.id, text=question.text.strip(), options=options)


# --------------------------------------------------------------------------------------------------
# Ответы методиста
# --------------------------------------------------------------------------------------------------


def _set_hours(spec: RequestSpec, value: str) -> bool:
    number = first_number(value)
    if number is None or number <= 0 or re.search(r"(?<![\w])-\s*\d", value):  # «-5» — не объём
        return False
    spec.total_hours = number
    return True


def _set_type(spec: RequestSpec, value: str) -> bool:
    ptype = classify_type_answer(value)
    if ptype is None:
        return False
    spec.program_type = ptype
    return True


def _set_audience(spec: RequestSpec, value: str) -> bool:
    spec.audience.entry_level = value
    if not spec.audience.category:
        spec.audience.category = value
    grade = extract_age_or_grade(normalize(value))
    if grade and not spec.audience.age_or_grade:
        spec.audience.age_or_grade = grade
    return True


def _set_form(spec: RequestSpec, value: str) -> bool:
    kind, dot, eo, share = classify_form(value)
    if kind is None and dot:
        kind = StudyFormKind.ZAOCHNAYA
    if kind is None:
        return False
    spec.form = StudyForm(kind=kind, dot=dot, eo=eo, distance_share=share)
    return True


def _set_fa(spec: RequestSpec, value: str) -> bool:
    form, _ = classify_fa(value)
    spec.fa_form = form or FAForm.OTHER
    spec.fa_text = value
    return True


def _set_fa_hours(spec: RequestSpec, value: str) -> bool:
    number = first_number(value)
    if number is None or number <= 0:
        return False
    spec.fa_hours = number
    return True


def _resolve_load(spec: RequestSpec, value: str) -> bool:
    norm = normalize(value)
    number = first_number(norm)
    if number is not None and re.search(r"час|\bч\b", norm, FLAGS) and number != spec.total_hours:
        spec.total_hours = number
    elif re.search(r"срок\w*[^.]{0,30}?(?:можно|не\s+важ|сократ|измен|увелич)", norm, FLAGS):
        spec.weeks = None
        spec.duration_text = None
    elif re.search(r"объ[её]м\w*[^.]{0,30}?(?:нужно|можно|увелич|измен|сократ)", norm, FLAGS):
        spec.total_hours = None  # объём назовёт методист — следующий вопрос
    spec.assumptions.append(f"Расхождение объёма и срока решено по ответу методиста: «{value}».")
    return True


ANSWER_RULES = {
    "program_type": _set_type,
    "hours": _set_hours,
    "audience": _set_audience,
    "form": _set_form,
    "final_attestation": _set_fa,
    "conflict_type": _set_type,
    "conflict_hours": _set_hours,
    "conflict_sessions": _set_hours,
    "conflict_fa_hours": _set_fa_hours,
    "conflict_volume": _set_hours,
    "conflict_load": _resolve_load,
}


def apply_answers(spec: RequestSpec, answers: dict[str, str]) -> RequestSpec:
    """Ответы методиста → новый RequestSpec. Идемпотентна: вызывается со всеми ответами на спеке после intake."""
    new = spec.model_copy(deep=True)
    answered: set[str] = set()
    for key, raw in answers.items():
        value = (raw or "").strip()
        if not value or key.startswith("_"):
            continue
        if DEFER_RE.search(value):
            answered.add(key)
            if key in CORE_PARAMS:
                _apply_default(new, key, deferred=True)
            continue
        rule = ANSWER_RULES.get(key)
        if rule is None:
            new.assumptions.append(f"Уточнение методиста ({key}): {value}")
            answered.add(key)
        elif rule(new, value):
            answered.add(key)

    missing = [m for m in spec.missing if m not in answered]
    if new.total_hours is None and "hours" not in missing:
        missing.append("hours")
    if new.total_hours is not None:
        missing = [m for m in missing if m != "hours"]
    if answered & {"conflict_type"}:
        missing = [m for m in missing if m != "program_type"]
    for conflict in spec_conflicts(new):
        if conflict not in answered and conflict not in missing:
            missing.append(conflict)
    new.missing = missing

    facts = extract_facts(new.raw_text)
    if new.constraints.min_practice_share is None and facts.practice_hours and new.total_hours:
        new.constraints.min_practice_share = min(1.0, facts.practice_hours / new.total_hours)
    return new


# --------------------------------------------------------------------------------------------------
# Умолчания
# --------------------------------------------------------------------------------------------------


# (не указано в запросе, оставлено на усмотрение) — с согласованием по роду
_DEFAULT_REASON = {
    "program_type": ("Тип программы в запросе не указан", "Тип программы оставлен на усмотрение агента"),
    "hours": ("Объём в запросе не указан", "Объём оставлен на усмотрение агента"),
    "audience": (
        "Входной уровень слушателей в запросе не указан",
        "Входной уровень слушателей оставлен на усмотрение агента",
    ),
    "form": ("Форма обучения в запросе не указана", "Форма обучения оставлена на усмотрение агента"),
    "final_attestation": (
        "Форма итоговой аттестации в запросе не указана",
        "Форма итоговой аттестации оставлена на усмотрение агента",
    ),
}


def _apply_default(spec: RequestSpec, key: str, deferred: bool = False) -> None:
    reason = _DEFAULT_REASON[key][1 if deferred else 0]
    ptype = spec.program_type or ProgramType.PK
    if key == "program_type":
        if spec.program_type is None:
            spec.program_type = ProgramType.PK
        spec.assumptions.append(f"{reason} — выбрана {TYPE_TITLES[spec.program_type]} (по смыслу запроса).")
    elif key == "hours" and spec.total_hours is None:
        spec.total_hours = DEFAULT_HOURS[ptype]
        spec.assumptions.append(
            f"{reason} — принято {hours_label(spec.total_hours)}; при необходимости измените."
        )
    elif key == "audience":
        if not spec.audience.entry_level:
            level = (
                "на начинающих: специальной подготовки по теме не требуется"
                if ptype == ProgramType.DOP
                else "на слушателей с базовой подготовкой по профилю программы"
            )
            spec.assumptions.append(f"{reason} — программа рассчитана {level}.")
    elif key == "form":
        if spec.form is None:
            spec.form = StudyForm(kind=StudyFormKind.OCHNAYA)
        form = FORM_TITLES[spec.form.kind] + (
            " с применением дистанционных технологий" if spec.form.dot else ""
        )
        spec.assumptions.append(f"{reason} — принята {form}.")
    elif key == "final_attestation":
        if spec.fa_form is None:
            spec.fa_form = DEFAULT_FA[ptype]
        spec.assumptions.append(f"{reason} — принята форма «{FA_TITLES[spec.fa_form]}».")


NO_DOT_RE = re.compile(r"без\s+(?:применени\w+\s+)?(?:дистанционн|электронн|ДОТ\b|онлайн)", re.IGNORECASE)


def _part_time_with_dot(spec: RequestSpec) -> None:
    """Общее правило: заочная часть очно-заочной или заочной программы проводится с применением ЭО и ДОТ, если
    в запросе не сказано иначе. Тогда учебный план показывает часы с ДОТ, а нормативная база — правила ЭО и ДОТ."""
    form = spec.form
    if (
        form is None
        or form.kind == StudyFormKind.OCHNAYA
        or form.dot
        or form.eo
        or NO_DOT_RE.search(spec.raw_text)
    ):
        return
    spec.form = form.model_copy(update={"dot": True, "eo": True})
    spec.assumptions.append(
        f"Форма {FORM_TITLES[form.kind]}: заочная часть проводится с применением электронного обучения и "
        "дистанционных образовательных технологий (обычная практика ДПП); если ДОТ не нужны — уточните."
    )


def finalize_spec(spec: RequestSpec) -> RequestSpec:
    """Перед каркасом: всё, что осталось неизвестным, закрывается умолчаниями с записью в assumptions.

    Вызывается шагом skeleton. Неснятые противоречия не блокируют работу — они остаются в assumptions.
    """
    new = spec.model_copy(deep=True)
    for key in CORE_PARAMS:  # program_type первым: от типа зависят остальные умолчания
        if key in new.missing or (key == "program_type" and new.program_type is None):
            _apply_default(new, key)
    _part_time_with_dot(new)
    facts = extract_facts(new.raw_text)
    for key in [m for m in new.missing if is_conflict(m)]:
        new.assumptions.append(f"Не снято противоречие: {describe_conflict(key, new, facts)}")
    new.missing = []
    new.assumptions = list(dict.fromkeys(new.assumptions))
    return new
