"""Шаг skeleton: RequestSpec → каркас программы (docs/ARCHITECTURE.md §2.3, §3).

Кто что делает:
- LLM (навык app/skills/skeleton.md, один вызов) — цель, задачи, компетенции, результаты обучения, модули и темы
  с относительными весами и долями видов работы, формы текущего контроля. Ни одного числа часов;
  трудовые функции — только ID из переданного списка реестра;
- код — ID объектов, связи по ID и их исправление (битые ссылки, РО без темы, тема без РО), выбор профстандартов
  и нормативки из реестра, часы (app/core/hours.py), календарь (app/core/calendar.py), самопроверка на стадии
  «каркас» (app/core/validate.py). Конфликты ограничений и «срок против нагрузки» идут предупреждениями в отчёт.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from app.config import now_msk
from app.core.calendar import build_calendar
from app.core.hours import (
    HoursConstraints,
    ModuleSpec,
    SolverResult,
    TopicSpec,
    apply_hours,
    constraints_from_request,
    fmt_hours,
    solve_hours,
)
from app.core.ids import IdAllocator, display_label
from app.core.registry import Registry
from app.core.validate import EXPECTATION_RE
from app.llm import LLMClient, TruncatedResponse
from app.models import (
    Audience,
    Competency,
    FAForm,
    FinalAttestation,
    Kit,
    LearningOutcome,
    MethodistWarning,
    Module,
    ModuleKind,
    OutcomeKind,
    Program,
    ProgramType,
    RequestSpec,
    SkeletonDraft,
    SlidesScope,
    StudyForm,
    TFRef,
    Topic,
    ValidationReport,
    WarningType,
    WorkMix,
)
from app.pipeline.base import AgentError, StepContext
from app.pipeline.clarify import finalize_spec, hours_label
from app.pipeline.intake import request_key
from app.pipeline.request_rules import FA_TITLES, FORM_TITLES, TYPE_TITLES, normalize
from app.skills import load_skill

logger = logging.getLogger("karkas.skeleton")

PS_CANDIDATES = 2  # сколько профстандартов из реестра предлагать модели
PS_MIN_SCORE = 3  # меньше — совпадение случайное (одно общее слово)
PS_SECOND_SHARE = 0.6  # второй ПС — только если подходит почти так же, как первый
MAX_FUNCTIONS_PER_PS = 20  # у 06.015 и 06.016 по 150+ ТФ: в промпт — только ближайшие к запросу
REFERENCE_ROWS = 30  # строк учебного плана образца в промпт — как пример детализации
MAX_TOPICS_PER_MODULE = 6
INTERNSHIP_CONTROL = "Собеседование с руководителем стажировки"
DEFAULT_MIX = {
    ProgramType.PK: WorkMix(lk=40, pr=50, lb=0, srs=10),
    ProgramType.PK_INTERNSHIP: WorkMix(lk=0, pr=0, lb=0, srs=100),
    ProgramType.DOP: WorkMix(lk=40, pr=60, lb=0, srs=0),
}
PK_TYPES = (ProgramType.PK, ProgramType.PK_INTERNSHIP)
# правило шаблона ДПП (ч. 3 ст. 76 273-ФЗ): кто может осваивать программу повышения квалификации
PK_REQUIREMENTS = (
    "лица, имеющие среднее профессиональное и (или) высшее образование, а также лица, получающие "
    "среднее профессиональное и (или) высшее образование"
)

ValidateFn = Callable[[Kit, Iterable[MethodistWarning]], ValidationReport]


# --------------------------------------------------------------------------------------------------
# Профстандарты и трудовые функции (только из реестра)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LaborFunctionOption:
    id: str  # «06.015:C/11.6» — то, что модель возвращает в tf_ids
    ps_code: str
    ps_title: str
    tf_code: str
    title: str


# Фразы о форме аттестации и об образце ничего не говорят о предметной области: «Итоговая аттестация —
# тестирование» не должна приводить к профстандарту тестировщика, а «за образец возьмите стажировку по
# бизнес-анализу» — к профстандарту аналитика в программе по DevOps.
_ATTESTATION_RE = re.compile(
    r"аттестац|итогов\w*\s+(?:тестирован|контрол|экзамен)|(?:по|за|в\s+качестве)\s+образ(?:ец|ц\w*)"
    r"|образ(?:ец|ц\w*)\s+(?:структур|программ)|приложенн\w*\s+(?:программ|документ|образ|файл)|за\s+основу",
    re.IGNORECASE,
)
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[.!?;,\n])\s+")


def subject_text(raw_text: str) -> str:
    """Текст запроса без фраз (до точки или запятой) о форме аттестации и об образце — для поиска профстандарта."""
    return " ".join(part for part in _CLAUSE_SPLIT_RE.split(raw_text) if not _ATTESTATION_RE.search(part))


def labor_function_options(spec: RequestSpec, registry: Registry | None) -> list[LaborFunctionOption]:
    """ТФ самых подходящих профстандартов реестра (по тексту запроса). Для ДОП — пусто."""
    if registry is None or spec.program_type not in PK_TYPES:
        return []
    query = " ".join(
        part
        for part in (
            spec.title or "",
            subject_text(spec.raw_text),
            spec.audience.category,
            spec.audience.domain or "",
            " ".join(r.label for r in spec.required_topics),
        )
        if part
    )
    ranked = registry.scored_profstandards(query, min_score=PS_MIN_SCORE)[:PS_CANDIDATES]
    if not ranked:
        return []
    top_score = ranked[0][0]
    standards = [ps for score, ps in ranked if score >= top_score * PS_SECOND_SHARE]
    return [
        LaborFunctionOption(f"{ps.code}:{tf.code}", ps.code, ps.title, tf.code, tf.title)
        for ps in standards
        for tf in registry.relevant_labor_functions(ps, query, MAX_FUNCTIONS_PER_PS)
    ]


# --------------------------------------------------------------------------------------------------
# Промпт
# --------------------------------------------------------------------------------------------------


def _clamp(value: float, low: int, high: int) -> int:
    return max(low, min(high, round(value)))


def structure_hint(
    spec: RequestSpec, constraints: HoursConstraints, compact: bool = False
) -> tuple[int, int, int, int]:
    """Рекомендуемое число модулей и тем (от–до) по объёму: детализация растёт с часами, но не бесконечно.
    compact — для повтора после обрезанного ответа: тем примерно на 40 % меньше."""
    budget = constraints.total_hours - constraints.resolved_fa_hours()
    if constraints.sessions_program and constraints.session_hours:
        sessions = max(1, int(budget // constraints.session_hours))
        topics = sessions if sessions <= 12 else round(sessions / 2)
    elif constraints.srs_only:
        topics = _clamp(budget / 10, 6, 36)
    else:
        topics = _clamp(budget / 4, 4, 36)
    if compact:
        topics = max(3, round(topics * 0.6))
    modules = _clamp(topics / 3.5, 2, 8 if compact else 10)
    return (
        max(2, modules - 1),
        modules + 1,
        max(3, topics - 2),
        min(topics + 2, (modules + 1) * MAX_TOPICS_PER_MODULE),
    )


def _mix_rules(spec: RequestSpec, constraints: HoursConstraints) -> list[str]:
    if constraints.srs_only:
        return [
            "Аудиторных занятий нет: у каждой темы mix = {lk: 0, pr: 0, lb: 0, srs: 100}.",
            f"Текущий контроль по каждому модулю — «{INTERNSHIP_CONTROL.lower()}».",
        ]
    rules = [
        "mix — доли видов работы темы в процентах (lk — теория, pr — практика, srs — самостоятельная работа)."
    ]
    if constraints.sessions_program:
        rules.append("Занятия фиксированной длины без самостоятельной работы: srs = 0 и lb = 0 у всех тем.")
    else:
        rules.append("lb (лабораторные) — только если нужны работы на стенде или оборудовании, иначе 0.")
    if spec.constraints.min_practice_share:
        share = round(spec.constraints.min_practice_share * 100)
        rules.append(
            f"Практики (pr) в целом не меньше {share} % объёма — делай практику основной частью тем."
        )
    return rules


def describe_request(spec: RequestSpec, constraints: HoursConstraints) -> str:
    ptype = spec.program_type or ProgramType.PK
    lines = [
        f"Тип: {TYPE_TITLES[ptype]}.",
        f"Название: «{spec.title}»."
        if spec.title
        else "Название: не задано — предложи цель по смыслу запроса.",
        f"Объём: {hours_label(constraints.total_hours)}, из них итоговая аттестация "
        f"{fmt_hours(constraints.resolved_fa_hours())} ч (часы считает программа, не ты).",
    ]
    audience = spec.audience
    who = ", ".join(x for x in (audience.category, audience.age_or_grade) if x)
    lines.append(f"Слушатели: {who or 'не указаны'}.")
    lines.append(
        f"Что уже умеют: {audience.entry_level or 'не указано — считай, что начинают изучать тему'}."
    )
    if audience.domain:
        lines.append(f"Профессиональная среда: {audience.domain}.")
    if spec.form is not None:
        dot = " с применением дистанционных технологий" if spec.form.dot else ""
        lines.append(f"Форма обучения: {FORM_TITLES[spec.form.kind]}{dot}.")
    if constraints.sessions_program and constraints.session_hours:
        lines.append(
            f"Занятия по {fmt_hours(constraints.session_hours)} ч"
            + (f", {constraints.sessions_per_week} в неделю." if constraints.sessions_per_week else ".")
        )
    if spec.fa_form is not None:
        fa = (spec.fa_text or FA_TITLES[spec.fa_form]).strip().rstrip(".")
        lines.append(
            f"{fa}." if fa.lower().startswith("итоговая аттестация") else f"Итоговая аттестация: {fa}."
        )
    if spec.required_topics:
        labels = "; ".join(f"«{r.label}»" for r in spec.required_topics)
        lines.append(
            f"Обязательные темы (каждая — отдельная тема или модуль с этим словом в названии): {labels}."
        )
    c = spec.constraints
    if c.intro_block_topic:
        lines.append(
            f"Первым модулем — вводный блок «{c.intro_block_topic}» (слушатели этим ещё не владеют)."
        )
    if c.no_programming:
        lines.append("Программирование слушателям не требуется — темы без написания кода.")
    if c.ru_services_only:
        lines.append("Только сервисы, доступные в России, без VPN и зарубежных номеров.")
    if c.anonymized_data:
        lines.append("Практика — только на обезличенных данных.")
    if spec.assumptions:
        lines.append("Допущения: " + " ".join(spec.assumptions))
    return "\n".join(lines)


def describe_structure(spec: RequestSpec, constraints: HoursConstraints, compact: bool = False) -> str:
    m_low, m_high, t_low, t_high = structure_hint(spec, constraints, compact)
    lines = [
        f"Модулей: от {m_low} до {m_high}; тем всего: от {t_low} до {t_high}, не больше {MAX_TOPICS_PER_MODULE} "
        "в одном модуле.",
        "weight — относительная трудоёмкость модуля или темы от 1 до 10 (не часы).",
        *_mix_rules(spec, constraints),
    ]
    if compact:
        lines.append(
            "Отвечай компактно: ровно 3 результата у каждой компетенции, названия тем — не длиннее 8 слов, "
            "по 2 тега на тему."
        )
    if spec.program_type == ProgramType.DOP:
        lines.append(
            "Можно начать с вводного занятия (kind = intro). Заполни direction (направленность, например "
            "«техническая») и level (стартовый, базовый или продвинутый) и задачи программы (tasks)."
        )
    return "\n".join(f"- {line}" for line in lines)


def describe_functions(options: list[LaborFunctionOption], program_type: ProgramType) -> str:
    if program_type == ProgramType.DOP:
        return "Не нужны: в общеразвивающей программе профстандарты не указываются (tf_ids и tf_id оставь пустыми)."
    if not options:
        return "В реестре нет подходящего профстандарта — tf_ids и tf_id оставь пустыми."
    lines = []
    current = None
    for option in options:
        if option.ps_code != current:
            current = option.ps_code
            lines.append(f"Профстандарт {option.ps_code} «{option.ps_title}»:")
        lines.append(f"  {option.id} — {option.title}")
    return "\n".join(lines)


def describe_reference(spec: RequestSpec) -> str:
    ref = spec.reference
    if ref is None:
        return "нет"
    rows: list[str] = []
    for table in ref.hour_tables:
        if table.kind not in ("utp", "plan", "dop_plan"):
            continue
        for row in table.rows:
            if row.row_type in ("section", "topic") and row.title.strip():
                number = f"{row.number} " if row.number else ""
                rows.append(f"{'  ' if row.row_type == 'topic' else ''}{number}{row.title.strip()}")
        if rows:
            break
    if not rows:
        return "приложен документ без учебного плана"
    shown = "\n".join(rows[:REFERENCE_ROWS])
    return (
        "Строки учебного плана приложенного образца — только как пример детализации и порядка разделов; "
        f"названия и текст не копируй:\n{shown}"
    )


async def ask_llm(
    spec: RequestSpec,
    constraints: HoursConstraints,
    options: list[LaborFunctionOption],
    llm: LLMClient,
    compact: bool = False,
) -> SkeletonDraft:
    skill = load_skill("skeleton")
    messages = skill.messages(
        request=spec.raw_text or "(текст запроса пуст)",
        params=describe_request(spec, constraints),
        structure=describe_structure(spec, constraints, compact),
        functions=describe_functions(options, spec.program_type or ProgramType.PK),
        reference=describe_reference(spec),
    )
    return await llm.complete_json(
        "skeleton",
        skill.model,
        messages,
        SkeletonDraft,
        max_tokens=skill.max_tokens,
        temperature=skill.temperature,
        fixture_key=request_key(spec.raw_text),
    )


# --------------------------------------------------------------------------------------------------
# Сборка каркаса
# --------------------------------------------------------------------------------------------------


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-zа-я0-9]+", normalize(text).lower()) if len(w) > 3}


def _similarity(a: str, b: str) -> int:
    return len(_stems(a) & _stems(b))


@dataclass
class _Built:
    kit: Kit
    module_weights: dict[str, int]
    topic_weights: dict[str, int]
    topic_mix: dict[str, WorkMix]
    notes: list[str]


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


# «Модуль 2: …», «Раздел 3. …», «Тема 1.2 — …» — номера в документ ставит шаблон, в названии они лишние
NUMBER_PREFIX_RE = re.compile(
    r"^(?:модуль|раздел|тема|занятие|блок|урок)\s*№?\s*\d+(?:\.\d+)*\s*[:.)\-–—]?\s*", re.IGNORECASE
)


# ID трудовой функции из списка для модели («06.042:A/03.6») — только для tf_ids, в тексты программы не попадает
OPTION_ID_RE = re.compile(
    r"\s*[(\[]?\s*(?:ТФ\s*)?\b\d{2}\.\d{3}:[A-ZА-Я]/\d{2}\.\d{1,2}\b\s*[)\]]?", re.IGNORECASE
)


def _strip_option_ids(text: str | None) -> str:
    """Текст без ID трудовых функций; пункт, в котором ничего, кроме ID, не было, становится пустым."""
    value = _clean(OPTION_ID_RE.sub(" ", text or "")).strip(" ,;:—-")
    return value if re.search(r"[A-Za-zА-Яа-яЁё]{3}", value) else ""


def _title(text: str | None) -> str:
    cleaned = _clean(text)
    stripped = NUMBER_PREFIX_RE.sub("", cleaned).strip()
    return stripped[:1].upper() + stripped[1:] if stripped else cleaned


def _key(text: str | None) -> str:
    """Ключ модели без регистра и разделителей: «C1_LO1», «c1-lo1», «c1lo1» — одно и то же."""
    return re.sub(r"[^0-9a-zа-яё]", "", (text or "").lower())


# модуль, который модель иногда заводит под итоговую аттестацию («Итоговая аттестация», «Итоговая аттестация и …»,
# «Экзамен»), — её часы и описание ведёт программа; «Итоговый проект» и «Подготовка к экзамену» — обычные модули
FA_MODULE_RE = re.compile(
    r"^\s*(?:итогов\w*\s+(?:аттестаци|экзамен|зач[её]т|контрол)\w*|(?:экзамен|зач[её]т)\w*\s*$)",
    re.IGNORECASE,
)


class _LinkIndex:
    """Ссылки тем на результаты обучения при разных стилях ключей модели.

    Результат находится по своему ключу («lo1», если он уникален) или по ключу с компетенцией («c1_lo1»). Если такого
    ключа нет: «c1_lo3» — третий результат компетенции c1 по порядку; ссылка на компетенцию с единственным
    результатом — на него. Всё остальное не угадывается: связь пустая, её восстановит _link_orphans по смыслу.
    """

    def __init__(self) -> None:
        self.by_alias: dict[str, list[str]] = {}
        self.by_competency: dict[str, list[str]] = {}
        self.unresolved: list[str] = []

    def add(self, competency_key: str, outcome_key: str, outcome_id: str) -> None:
        ck, ok = _key(competency_key), _key(outcome_key)
        for alias in {ok, ck + ok}:
            if alias:
                self.by_alias.setdefault(alias, []).append(outcome_id)
        if ck:
            self.by_competency.setdefault(ck, []).append(outcome_id)

    def resolve(self, ref: str) -> list[str]:
        found = self.by_alias.get(_key(ref), [])
        if len(found) == 1:
            return found
        parts = [p for p in re.split(r"[_.\-:/\s]+", ref.strip()) if p]
        head = _key(parts[0]) if parts else ""
        own = self.by_competency.get(head, [])
        if own and len(parts) > 1:
            number = re.search(r"(\d+)$", parts[-1])
            if number and 1 <= int(number.group(1)) <= len(own):
                return [own[int(number.group(1)) - 1]]
        if len(own) == 1:
            return own
        self.unresolved.append(ref)
        return []


def build_skeleton(
    spec: RequestSpec,
    draft: SkeletonDraft,
    options: list[LaborFunctionOption],
    registry: Registry | None,
) -> _Built:
    """Черновик модели → Kit без часов. Битые ссылки и висячие РО/темы исправляются здесь (с записью в notes)."""
    ptype = spec.program_type or ProgramType.PK
    notes: list[str] = []
    alloc = IdAllocator()
    by_option = {o.id: o for o in options}

    # компетенции и их РО (компетенция без РО не нужна)
    comp_drafts = list({_key(c.key) or c.title: c for c in draft.competencies}.values())
    competencies: list[Competency] = []
    outcomes: list[LearningOutcome] = []
    links = _LinkIndex()
    for cd in comp_drafts:
        drafts = [o for o in cd.outcomes if _clean(o.text)]
        if not drafts:
            notes.append(f"Компетенция «{_clean(cd.title)}» без результатов обучения не включена в каркас.")
            continue
        cid = alloc.competency()
        refs: list[TFRef] = []
        if ptype in PK_TYPES:
            for tf_id in dict.fromkeys(cd.tf_ids):
                option = by_option.get(tf_id.strip())
                if option is not None:
                    refs.append(TFRef(ps_code=option.ps_code, tf_code=option.tf_code))
        competencies.append(
            Competency(id=cid, activity=_clean(cd.activity), title=_clean(cd.title), tf_refs=refs)
        )
        own_codes = {r.tf_code for r in refs}
        for od in drafts:
            lid = alloc.outcome(cid)
            option = by_option.get((od.tf_id or "").strip())
            tf_code = option.tf_code if option is not None and option.tf_code in own_codes else None
            outcomes.append(
                LearningOutcome(
                    id=lid, competency_id=cid, kind=od.kind, text=_clean(od.text), tf_code=tf_code
                )
            )
            links.add(cd.key, od.key, lid)

    # модули и темы
    modules: list[Module] = []
    topics: list[Topic] = []
    module_weights: dict[str, int] = {}
    topic_weights: dict[str, int] = {}
    topic_mix: dict[str, WorkMix] = {}
    default_mix = DEFAULT_MIX[ptype]
    for md in draft.modules:
        title = _title(md.title)
        topic_drafts = [t for t in md.topics if _clean(t.title)]
        if FA_MODULE_RE.search(title):
            notes.append(
                f"Модуль «{title}» не включён: итоговая аттестация проводится отдельно и считается программой."
            )
            continue
        if not topic_drafts:
            notes.append(f"Модуль «{title}» без тем не включён в каркас.")
            continue
        mid = alloc.module()
        # вводное занятие — только в ДОП и только первым модулем
        intro = ptype == ProgramType.DOP and not modules and md.kind == ModuleKind.INTRO
        kind = ModuleKind.INTRO if intro else ModuleKind.REGULAR
        control = _clean(md.control_form) or (
            INTERNSHIP_CONTROL if ptype == ProgramType.PK_INTERNSHIP else None
        )
        modules.append(Module(id=mid, order=len(modules) + 1, title=title, kind=kind, control_form=control))
        module_weights[mid] = md.weight
        for position, td in enumerate(topic_drafts, start=1):
            tid = alloc.topic(mid)
            lo_ids = list(dict.fromkeys(lid for ref in td.lo_keys for lid in links.resolve(ref)))
            tags = list(dict.fromkeys(_clean(t).lower() for t in td.tags if _clean(t)))[:6]
            topics.append(
                Topic(
                    id=tid,
                    module_id=mid,
                    order=position,
                    title=_title(td.title),
                    lo_ids=lo_ids,
                    control_form=_clean(td.control_form) or None,
                    tags=tags,
                )
            )
            topic_weights[tid] = td.weight
            mix = td.mix
            topic_mix[tid] = mix if (mix.lk + mix.pr + mix.lb + mix.srs) > 0 else default_mix
    if links.unresolved:
        shown = ", ".join(dict.fromkeys(links.unresolved))
        notes.append(
            f"Ссылки модели на результаты обучения не распознаны ({shown}) — связи восстановлены по смыслу."
        )
    if not modules or not topics:
        raise AgentError("Модель не предложила ни одного модуля с темами — каркас построить не удалось.")
    if not outcomes:
        raise AgentError("Модель не предложила результатов обучения — каркас построить не удалось.")

    _link_orphans(outcomes, topics, notes)

    form = spec.form or StudyForm()
    used_ps = sorted({r.ps_code for c in competencies for r in c.tf_refs})
    norm_ids = (
        [n.id for n in registry.norms_for(ptype, dot=form.dot or form.eo)] if registry is not None else []
    )
    title = _clean(spec.title)
    if not title:
        title = TYPE_TITLES[ptype].capitalize()
        notes.append("Название программы не задано — укажите его при утверждении каркаса.")
    program = Program(
        type=ptype,
        title=title,
        audience=_program_audience(spec, ptype),
        form=form,
        total_hours=spec.total_hours or 0,
        goal=_strip_option_ids(draft.goal),
        tasks=[t for t in (_strip_option_ids(x) for x in draft.tasks) if t],
        direction=(_clean(draft.direction) or None) if ptype == ProgramType.DOP else None,
        level=(_clean(draft.level) or None) if ptype == ProgramType.DOP else None,
        profstandards=used_ps if ptype in PK_TYPES else [],
        norm_ids=norm_ids,
    )
    kit = Kit(
        request=spec,
        program=program,
        competencies=competencies,
        outcomes=outcomes,
        modules=modules,
        topics=topics,
    )
    if ptype in PK_TYPES and not options:
        notes.append(
            "В реестре нет подходящего профстандарта: трудовые функции не указаны, реестр нужно пополнить."
        )
    return _Built(kit, module_weights, topic_weights, topic_mix, notes)


def _program_audience(spec: RequestSpec, ptype: ProgramType) -> Audience:
    """Аудитория из запроса; для ПК код добавляет требование к образованию по правилу шаблона."""
    audience = spec.audience.model_copy(deep=True)
    if ptype in PK_TYPES and not audience.requirements:
        audience.requirements = PK_REQUIREMENTS
    return audience


def _link_orphans(outcomes: list[LearningOutcome], topics: list[Topic], notes: list[str]) -> None:
    """РО без темы привязывается к самой близкой по словам теме; тема без РО — к самому близкому РО."""
    covered = {lo for t in topics for lo in t.lo_ids}
    for outcome in outcomes:
        if outcome.id in covered:
            continue
        best = max(
            topics, key=lambda t: (_similarity(outcome.text, f"{t.title} {' '.join(t.tags)}"), t.module_id)
        )
        best.lo_ids.append(outcome.id)
        notes.append(
            f"Результат обучения «{outcome.text}» не был привязан к теме — привязан к теме «{best.title}»."
        )
    for topic in topics:
        if not topic.lo_ids:
            best_lo = max(
                outcomes, key=lambda o: _similarity(o.text, f"{topic.title} {' '.join(topic.tags)}")
            )
            topic.lo_ids.append(best_lo.id)


# --------------------------------------------------------------------------------------------------
# Часы, календарь, слайды
# --------------------------------------------------------------------------------------------------


def slides_modules(kit: Kit, scope: SlidesScope) -> list[str]:
    """Модули, к которым нужны слайды: сначала по названиям модулей, и только если ни один не подошёл — по темам."""
    if scope.mode != "module" or not scope.module_pattern:
        return []
    try:
        pattern = re.compile(scope.module_pattern, re.IGNORECASE)
    except re.error:
        pattern = re.compile(re.escape(scope.module_pattern), re.IGNORECASE)
    modules = kit.modules_sorted()
    by_title = [m.id for m in modules if pattern.search(normalize(m.title))]
    if by_title:
        return by_title
    return [m.id for m in modules if any(pattern.search(normalize(t.title)) for t in kit.topics_of(m.id))]


def expectation_warnings(kit: Kit) -> list[MethodistWarning]:
    """Общее правило: ожидание заказчика («пойти работать в сферу») против того, что реально даёт программа."""
    match = EXPECTATION_RE.search(kit.request.raw_text)
    if not match:
        return []
    skills = [o.text.rstrip(".") for o in kit.outcomes if o.kind == OutcomeKind.CAN][:4]
    listed = "; ".join(f"«{s}»" for s in skills) if skills else "базовые умения по теме"
    text = (
        f"В запросе ожидание «{match.group(0)}». Программа на {hours_label(kit.program.total_hours)} даёт начальный "
        f"уровень: {listed}. Этого хватит, чтобы продолжить обучение и выполнять простые задачи под руководством, "
        "но не чтобы работать специалистом самостоятельно. Для этого обычно нужны профессиональная переподготовка "
        "и практика. Проверьте, что цель и результаты программы не обещают трудоустройства, и обсудите это "
        "с заказчиком."
    )
    return [MethodistWarning(type=WarningType.EXPECTATION_GAP, text=text)]


def _solve(built: _Built, constraints: HoursConstraints) -> SolverResult:
    kit = built.kit
    modules = [
        ModuleSpec(
            key=m.id,
            weight=built.module_weights[m.id],
            needs_lectures=m.needs_lectures,
            kind=m.kind,
            title=m.title,
        )
        for m in kit.modules_sorted()
    ]
    topics = [
        TopicSpec(
            key=t.id,
            module_key=t.module_id,
            weight=built.topic_weights[t.id],
            mix=built.topic_mix[t.id],
            title=t.title,
        )
        for m in kit.modules_sorted()
        for t in kit.topics_of(m.id)
    ]
    try:
        return solve_hours(modules, topics, constraints)
    except ValueError as exc:
        raise AgentError(f"Часы по каркасу не раскладываются: {exc}") from exc


def _hours_and_calendar(built: _Built, constraints: HoursConstraints) -> list[MethodistWarning]:
    kit = built.kit
    result = _solve(built, constraints)
    apply_hours(kit, result)
    calendar, calendar_warnings = build_calendar(kit, constraints)
    kit.calendar = calendar
    return [*result.warnings(), *calendar_warnings]


def _session_topics(kit: Kit, numbers: list[int]) -> list[str]:
    if kit.calendar is None:
        return []
    return list(
        dict.fromkeys(
            item.topic_id
            for day in kit.calendar.days
            if day.n in numbers
            for item in day.items
            if item.topic_id is not None
        )
    )


# --------------------------------------------------------------------------------------------------
# Шаг целиком
# --------------------------------------------------------------------------------------------------


def assemble(
    spec: RequestSpec,
    draft: SkeletonDraft,
    options: list[LaborFunctionOption],
    registry: Registry | None,
    validate_fn: ValidateFn,
) -> Kit:
    """Каркас из черновика модели: ID, связи, часы, календарь, слайды, самопроверка."""
    constraints = constraints_from_request(spec, spec.program_type or ProgramType.PK)
    built = build_skeleton(spec, draft, options, registry)
    kit = built.kit
    ptype = kit.program.type
    kit.program.schedule = constraints.to_schedule_params(spec.duration_text)

    fa_form = spec.fa_form or FAForm.ZACHET  # finalize_spec гарантирует форму ИА
    kit.final_attestation = FinalAttestation(
        form=fa_form,
        form_text=spec.fa_text or FA_TITLES[fa_form],
        hours=constraints.resolved_fa_hours(),
        lo_ids=[o.id for o in kit.outcomes],
    )

    # слайды: модуль по теме — лекции обязательны в его темах
    requested = spec.outputs.slides_scope
    scope = requested.model_copy(deep=True)
    if requested.mode == "module":
        scope.module_ids = slides_modules(kit, requested)
        if not scope.module_ids:
            built.notes.append(
                "Не найден модуль, к которому по запросу нужны слайды — проверьте названия модулей."
            )
        for module in kit.modules:
            module.needs_lectures = module.id in scope.module_ids
    if ptype == ProgramType.PK_INTERNSHIP and requested.mode == "default":
        scope = SlidesScope()
    kit.program.slides_scope = scope

    warnings = _hours_and_calendar(built, constraints)

    # слайды к занятиям: в этих занятиях должны быть лекционные часы — иначе лекции обязательны в их модулях
    if requested.mode == "sessions" and requested.session_numbers:
        session_topics = _session_topics(kit, requested.session_numbers)
        by_id = {t.id: t for t in kit.topics}
        if session_topics and not any(by_id[tid].hours.lk > 0 for tid in session_topics):
            needed = {by_id[tid].module_id for tid in session_topics}
            for module in kit.modules:
                module.needs_lectures = module.needs_lectures or module.id in needed
            warnings = _hours_and_calendar(built, constraints)

    for note in built.notes:
        logger.info("skeleton: %s", note)
    kit.request.assumptions = list(dict.fromkeys([*kit.request.assumptions, *built.notes]))
    kit.meta.created_at = now_msk()
    kit.report = validate_fn(kit, [*warnings, *expectation_warnings(kit)])
    return kit


async def run_skeleton(
    spec: RequestSpec,
    ctx: StepContext | None,
    llm: LLMClient,
    registry: Registry | None,
    validate_fn: ValidateFn,
) -> Kit:
    final = finalize_spec(spec)
    if final.total_hours is None or final.total_hours <= 0:
        raise AgentError("Не задан объём программы в часах — каркас построить нельзя.")
    constraints = constraints_from_request(final, final.program_type or ProgramType.PK)
    options = labor_function_options(final, registry)
    try:
        draft = await ask_llm(final, constraints, options, llm)
    except TruncatedResponse:
        # ответ упёрся в max_tokens (большая программа): один повтор с компактной структурой
        logger.warning("skeleton: ответ обрезан — повтор с компактной структурой")
        try:
            draft = await ask_llm(final, constraints, options, llm, compact=True)
        except TruncatedResponse as exc:
            raise AgentError(
                "Каркас слишком большой для одного ответа модели даже в компактном виде — "
                "уменьшите детализацию или объём программы."
            ) from exc
        final.assumptions.append("Каркас собран в компактном виде: полный ответ модели не уместился в лимит.")
    kit = assemble(final, draft, options, registry, validate_fn)
    kit.meta.llm_mode = llm.settings.llm_mode
    return kit


def outline(kit: Kit) -> str:
    """Короткое текстовое оглавление каркаса — для логов и отладки."""
    lines = [f"{kit.program.title} — {fmt_hours(kit.total_hours())} ч"]
    for module in kit.modules_sorted():
        lines.append(
            f"{display_label(kit, module.id)}. {module.title} — {fmt_hours(kit.module_hours(module.id).total)} ч"
        )
        for topic in kit.topics_of(module.id):
            lines.append(
                f"  {display_label(kit, topic.id)}. {topic.title} — {fmt_hours(topic.hours.total)} ч"
            )
    return "\n".join(lines)
