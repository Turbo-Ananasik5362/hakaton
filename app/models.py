"""Модель комплекта — единственный источник правды (docs/ARCHITECTURE.md §1).

Принципы:
- ID выдаёт код (app/core/ids.py), LLM только ссылается на переданные ID.
- Связь хранится один раз — у зависимого объекта (Topic.module_id, Seminar.topic_id, TestQuestion.lo_ids ...).
- Вычисляемое не хранится: суммы часов, часы модуля, «Итого», объём, выдаваемый документ — computed-поля
  и методы Kit. В JSON они выгружаются, при загрузке игнорируются.
- Модель строгая к типам и форматам ID, но не запрещает смысловые ошибки (суммы, ссылки, шаг 0,5):
  их ловит app/core/validate.py — иначе нельзя собрать заведомо ошибочный комплект для тестов.
- Часы — float: кратные 0,5 значения представляются в двоичном виде точно, сложение таких чисел точно.
  Солвер внутри работает в целых получасах.
- Черновики ответов LLM (*Draft) отделены от модели комплекта и не содержат полей часов.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

# --------------------------------------------------------------------------------------------------
# Базовые типы
# --------------------------------------------------------------------------------------------------

Hours = Annotated[float, Field(ge=0)]
"""Часы. Неотрицательные; кратность 0,5 проверяет validate (V06)."""

CompetencyId = Annotated[str, Field(pattern=r"^C\d+$")]
OutcomeId = Annotated[str, Field(pattern=r"^LO\d+\.\d+$")]
ModuleId = Annotated[str, Field(pattern=r"^M\d+$")]
TopicId = Annotated[str, Field(pattern=r"^T\d+\.\d+$")]
LectureId = Annotated[str, Field(pattern=r"^L\d+\.\d+$")]
SeminarId = Annotated[str, Field(pattern=r"^S\d+$")]
TestId = Annotated[str, Field(pattern=r"^TS\d+$")]
QuestionId = Annotated[str, Field(pattern=r"^Q\d+$")]
InterviewId = Annotated[str, Field(pattern=r"^IQ\d+$")]
SourceId = Annotated[str, Field(pattern=r"^SRC\d+$")]
DayId = Annotated[str, Field(pattern=r"^D\d+$")]
CriterionId = Annotated[str, Field(pattern=r"^(S\d+|FA)\.K\d+$")]
PortfolioItemId = Annotated[str, Field(pattern=r"^FA\.P\d+$")]
IssueId = Annotated[str, Field(pattern=r"^V\d{2}-\d+$")]
CheckId = Annotated[str, Field(pattern=r"^V\d{2}$")]
AnyId = str
"""Любой ID комплекта (для target_ids и changed_ids)."""


class KitModel(BaseModel):
    """База для объектов комплекта: лишние поля (в т.ч. computed при загрузке JSON) игнорируются."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True, use_enum_values=False)


class DraftModel(BaseModel):
    """База для черновиков LLM: лишние поля игнорируются; строгость схемы задаёт app/llm.py."""

    model_config = ConfigDict(extra="ignore")


# --------------------------------------------------------------------------------------------------
# Перечисления
# --------------------------------------------------------------------------------------------------


class ProgramType(str, Enum):
    PK = "pk"  # ДПП повышения квалификации
    PK_INTERNSHIP = "pk_internship"  # ДПП ПК в форме стажировки
    DOP = "dop"  # дополнительная общеразвивающая программа


class StudyFormKind(str, Enum):
    OCHNAYA = "ochnaya"
    OCHNO_ZAOCHNAYA = "ochno_zaochnaya"
    ZAOCHNAYA = "zaochnaya"


class WorkKind(str, Enum):
    LK = "lk"  # лекции / теория
    PR = "pr"  # практические занятия
    LB = "lb"  # лабораторные работы (в документе всегда «ЛБ»)
    SRS = "srs"  # самостоятельная работа


class OutcomeKind(str, Enum):
    KNOW = "know"  # знать
    CAN = "can"  # уметь
    MASTER = "master"  # владеть: «практический опыт» (ПК) / «навыки» (ДОП)


class ModuleKind(str, Enum):
    REGULAR = "regular"
    INTRO = "intro"  # вводное занятие ДОП — в документе «Тема 0.N»


class SeminarKind(str, Enum):
    PRACTICE = "practice"
    LAB = "lab"


class TestScope(str, Enum):
    __test__ = False  # не тестовый класс для pytest

    FINAL = "final"
    MODULE = "module"
    ENTRY = "entry"


class QuestionKind(str, Enum):
    SINGLE = "single"
    MULTIPLE = "multiple"


class FAForm(str, Enum):
    PROJECT_DEFENSE = "project_defense"
    PORTFOLIO = "portfolio"
    EXAM_PRACTICAL = "exam_practical"
    REPORT_DEFENSE = "report_defense"
    TEST = "test"
    ZACHET = "zachet"
    OTHER = "other"


class SourceKind(str, Enum):
    NORM = "norm"
    PROFSTANDARD = "profstandard"
    BOOK = "book"
    DOC = "doc"
    WEB = "web"
    SOFTWARE = "software"


class SourceSection(str, Enum):
    NORMATIVE = "normative"  # нормативно-правовые документы
    MAIN = "main"  # основная литература
    ADDITIONAL = "additional"  # дополнительная литература
    INTERNET = "internet"  # интернет-ресурсы
    SOFTWARE = "software"  # ПО и информационные системы


class Verification(str, Enum):
    REGISTRY = "registry"  # из data/registry (нормы, ПС)
    CATALOG = "catalog"  # найдено в каталоге проверенных изданий
    ONLINE = "online"  # подтверждено онлайн-сверкой
    UNVERIFIED = "unverified"  # в документе — метка «[не подтверждено]»


class CalendarMode(str, Enum):
    SESSIONS = "sessions"  # занятия фиксированной длины (ДОП)
    DAYS = "days"  # учебные дни с лимитом часов (ПК)


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class FixBy(str, Enum):
    CODE = "code"
    LLM = "llm"
    HUMAN = "human"


class ReportStatus(str, Enum):
    OK = "ok"
    WARNINGS = "warnings"
    ERRORS = "errors"


class WarningType(str, Enum):
    REFERENCE_ERRORS = "reference_errors"  # ошибки в приложенном образце
    EXPECTATION_GAP = "expectation_gap"  # ожидания заказчика vs реальные результаты
    SCHEDULE_TIGHT = "schedule_tight"  # желаемый срок не сходится с нагрузкой
    NORM_REPLACED = "norm_replaced"  # акт из ТЗ утратил силу (№ 499 → № 266)
    UNVERIFIED_SOURCES = "unverified_sources"
    CONSTRAINT_CONFLICT = "constraint_conflict"  # несовместимые требования запроса


class SessionStatus(str, Enum):
    NEW = "new"
    NEED_CLARIFICATION = "need_clarification"
    SKELETON_READY = "skeleton_ready"
    QUEUED = "queued"
    GENERATING = "generating"
    PATCHING = "patching"
    DONE = "done"
    FAILED = "failed"


class PatchOperation(str, Enum):
    REPLACE_TOPIC = "replace_topic"
    CHANGE_HOURS = "change_hours"
    HARDEN_TEST = "harden_test"
    REPLACE_LO = "replace_lo"
    ADD_TOPIC = "add_topic"
    REMOVE_TOPIC = "remove_topic"
    REGENERATE = "regenerate"


class GenerationMode(str, Enum):
    FULL = "full"
    ECONOMY = "economy"  # module_pack: один вызов на модуль


DOCUMENT_BY_TYPE: dict[ProgramType, str] = {
    ProgramType.PK: "Удостоверение о повышении квалификации установленного образца",
    ProgramType.PK_INTERNSHIP: "Удостоверение о повышении квалификации установленного образца",
    ProgramType.DOP: "Сертификат об освоении программы",
}

# --------------------------------------------------------------------------------------------------
# Часы
# --------------------------------------------------------------------------------------------------


class HoursRow(KitModel):
    """Часы строки плана. distance — часть контактной работы (ЛК+ПР+ЛБ), проводимая с ДОТ.

    Для ДОП: lk = «Лекции/теория», pr = «Практические», srs = «Самостоятельная работа», lb = 0,
    contact = «Всего аудиторных», total = «Общая трудоёмкость».
    """

    lk: Hours = 0
    pr: Hours = 0
    lb: Hours = 0
    srs: Hours = 0
    distance: Hours = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def contact(self) -> float:
        return self.lk + self.pr + self.lb

    @computed_field  # type: ignore[prop-decorator]
    @property
    def in_person(self) -> float:
        return self.contact - self.distance

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> float:
        return self.lk + self.pr + self.lb + self.srs

    def get(self, kind: WorkKind) -> float:
        return float(getattr(self, kind.value))

    def __add__(self, other: HoursRow) -> HoursRow:
        return HoursRow(
            lk=self.lk + other.lk,
            pr=self.pr + other.pr,
            lb=self.lb + other.lb,
            srs=self.srs + other.srs,
            distance=self.distance + other.distance,
        )

    @classmethod
    def sum(cls, rows: list[HoursRow]) -> HoursRow:
        result = cls()
        for row in rows:
            result = result + row
        return result


# --------------------------------------------------------------------------------------------------
# Паспорт программы
# --------------------------------------------------------------------------------------------------


class Audience(KitModel):
    category: str = ""  # кто учится
    age_or_grade: str | None = None  # для школьников: классы / возраст
    entry_level: str | None = None  # входной уровень
    requirements: str | None = None  # требования к слушателям (ПК: СПО/ВО или получающие их)
    domain: str | None = None  # профессиональная среда: банк, школа, колледж ...
    domain_patterns: list[str] = Field(default_factory=list)  # ключевые слова среды для V36
    constraints: list[str] = Field(default_factory=list)  # «не требовать программирования» и т.п.


class StudyForm(KitModel):
    kind: StudyFormKind = StudyFormKind.OCHNAYA
    eo: bool = False  # электронное обучение
    dot: bool = False  # дистанционные образовательные технологии
    distance_share: float | None = Field(default=None, ge=0, le=1)  # желаемая доля ДОТ в контактной работе


class ScheduleParams(KitModel):
    """Параметры, с которыми солвер строил часы и календарь (после подстановки умолчаний)."""

    weeks_target: int | None = Field(default=None, ge=1)  # желаемый срок в неделях (мягкое ограничение)
    duration_text: str | None = None  # как сказано в запросе: «около двух месяцев»
    hours_per_day_max: Hours | None = None
    session_hours: Hours | None = None  # длина занятия (ДОП)
    sessions_per_week: int | None = Field(default=None, ge=1)
    max_days_per_week: int = Field(default=6, ge=1, le=7)


class SlidesScope(KitModel):
    """Для чего делать слайды."""

    mode: Literal["sessions", "module", "default"] = "default"
    session_numbers: list[int] = Field(default_factory=list)  # «к занятиям 1 и 2»
    module_pattern: str | None = None  # «модуль про pandas» → регулярка
    module_ids: list[ModuleId] = Field(default_factory=list)  # разрешено кодом


class TFRef(KitModel):
    ps_code: str  # «06.015»
    tf_code: str  # «C/11.6»


class Program(KitModel):
    type: ProgramType
    title: str  # без кавычек — «ёлочки» ставит рендер
    audience: Audience = Field(default_factory=Audience)
    form: StudyForm = Field(default_factory=StudyForm)
    total_hours: Hours  # заявленный объём (из запроса), не от LLM
    schedule: ScheduleParams = Field(default_factory=ScheduleParams)
    goal: str = ""
    tasks: list[str] = Field(default_factory=list)  # задачи (ДОП)
    direction: str | None = None  # направленность (ДОП)
    level: str | None = None  # уровень: стартовый / базовый / продвинутый (ДОП)
    profstandards: list[str] = Field(default_factory=list)  # коды ПС из реестра; для dop — пусто
    norm_ids: list[str] = Field(default_factory=list)  # ID актов из data/registry/norms.json
    slides_scope: SlidesScope = Field(default_factory=SlidesScope)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def document(self) -> str:
        return DOCUMENT_BY_TYPE[self.type]


# --------------------------------------------------------------------------------------------------
# Компетенции и результаты обучения
# --------------------------------------------------------------------------------------------------


class Competency(KitModel):
    id: CompetencyId
    activity: str = ""  # вид деятельности (таблица 1.3 ПК)
    title: str
    tf_refs: list[TFRef] = Field(default_factory=list)  # только из реестра; для dop — пусто


class LearningOutcome(KitModel):
    id: OutcomeId
    competency_id: CompetencyId
    kind: OutcomeKind
    text: str
    tf_code: str | None = None  # ТФ из tf_refs своей компетенции


# --------------------------------------------------------------------------------------------------
# Структура и содержание
# --------------------------------------------------------------------------------------------------


class Module(KitModel):
    id: ModuleId
    order: int = Field(ge=0)
    title: str
    kind: ModuleKind = ModuleKind.REGULAR
    control_form: str | None = None  # форма текущего контроля
    needs_lectures: bool = False  # на модуль запрошены слайды — солвер обязан дать ЛК > 0


class Topic(KitModel):
    id: TopicId
    module_id: ModuleId
    order: int = Field(ge=0)
    title: str
    lo_ids: list[OutcomeId] = Field(default_factory=list)
    hours: HoursRow = Field(default_factory=HoursRow)  # заполняет только солвер
    content: dict[WorkKind, list[str]] = Field(
        default_factory=dict
    )  # пункты 2.4 по видам работы, без чисел часов
    control_form: str | None = None  # «Форма контроля» в УТП
    tags: list[str] = Field(default_factory=list)


class PlanItem(KitModel):
    title: str
    theses: list[str] = Field(default_factory=list)
    lo_ids: list[OutcomeId] = Field(default_factory=list)


class GlossaryItem(KitModel):
    term: str
    definition: str


class Slide(KitModel):
    title: str
    bullets: list[str] = Field(default_factory=list)
    notes: str = ""


class Lecture(KitModel):
    id: LectureId
    module_id: ModuleId
    topic_ids: list[TopicId] = Field(default_factory=list)
    session_no: int | None = Field(default=None, ge=1)
    title: str
    goal: str = ""
    plan: list[PlanItem] = Field(default_factory=list)
    glossary: list[GlossaryItem] = Field(default_factory=list)
    slides: list[Slide] | None = None  # только для лекций из slides_scope


class Criterion(KitModel):
    id: CriterionId
    text: str
    max_points: float = Field(gt=0)


class Seminar(KitModel):
    id: SeminarId
    topic_id: TopicId
    lo_ids: list[OutcomeId] = Field(default_factory=list)
    kind: SeminarKind = SeminarKind.PRACTICE
    title: str
    situation: str = ""  # ситуация из профессиональной среды аудитории
    task: str = ""
    steps: list[str] = Field(default_factory=list)
    deliverable: str = ""
    criteria: list[Criterion] = Field(default_factory=list)
    pass_threshold: float | None = Field(default=None, ge=0)  # баллы


class Test(KitModel):
    __test__ = False  # не тестовый класс для pytest

    id: TestId
    title: str = ""
    scope: TestScope = TestScope.MODULE
    module_id: ModuleId | None = None
    question_ids: list[QuestionId] = Field(default_factory=list)  # порядок вопросов
    pass_threshold: float | None = Field(default=None, ge=0, le=100)  # % верных ответов


class TestQuestion(KitModel):
    __test__ = False  # не тестовый класс для pytest

    id: QuestionId
    test_id: TestId
    topic_id: TopicId
    lo_ids: list[OutcomeId] = Field(default_factory=list)
    kind: QuestionKind = QuestionKind.SINGLE
    stem: str
    options: list[str] = Field(default_factory=list)
    correct: list[int] = Field(default_factory=list)  # индексы верных вариантов
    explanation: str = ""
    difficulty: int = Field(default=1, ge=1, le=3)


class InterviewQuestion(KitModel):
    id: InterviewId
    module_id: ModuleId
    lo_ids: list[OutcomeId] = Field(default_factory=list)
    text: str
    answer_points: list[str] = Field(default_factory=list)  # что должно прозвучать в ответе
    criteria: list[str] = Field(default_factory=list)


class PortfolioItem(KitModel):
    id: PortfolioItemId
    title: str
    requirements: list[str] = Field(default_factory=list)


class ExampleTask(KitModel):
    title: str
    environment: str = ""  # «стенд», ПО, исходные данные
    description: str = ""
    deliverable: str = ""


class FinalAttestation(KitModel):
    id: Literal["FA"] = "FA"
    form: FAForm
    form_text: str = ""
    hours: Hours = 0  # из запроса или по умолчанию солвера
    lo_ids: list[OutcomeId] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    portfolio_items: list[PortfolioItem] = Field(default_factory=list)
    example_task: ExampleTask | None = None
    data_requirements: list[str] = Field(default_factory=list)
    expected_result: str | None = None
    report_requirements: list[str] = Field(default_factory=list)
    criteria: list[Criterion] = Field(default_factory=list)
    pass_rule: str = ""
    procedure: list[str] = Field(default_factory=list)


class Source(KitModel):
    id: SourceId
    kind: SourceKind
    section: SourceSection
    registry_id: str | None = None  # ID в data/registry (нормы, ПС, каталог литературы)
    verification: Verification = Verification.UNVERIFIED
    authors: list[str] = Field(default_factory=list)
    title: str
    year: int | None = None
    publisher: str | None = None
    isbn: str | None = None
    url: str | None = None
    topic_ids: list[TopicId] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unverified(self) -> bool:
        return self.verification == Verification.UNVERIFIED

    @computed_field  # type: ignore[prop-decorator]
    @property
    def citation(self) -> str:
        """Библиографическая строка собирается кодом из полей, а не пишется моделью."""
        parts: list[str] = []
        if self.authors:
            names = ", ".join(self.authors)
            parts.append(names if names.endswith(".") else names + ".")
        parts.append(self.title.rstrip(".") + ".")
        tail = ", ".join(str(x) for x in (self.publisher, self.year) if x)
        if tail:
            parts.append("— " + tail + ".")
        if self.isbn:
            parts.append(f"— ISBN {self.isbn}.")
        if self.url:
            parts.append(f"— URL: {self.url}")
        text = " ".join(parts)
        return f"{text} [не подтверждено]" if self.unverified else text


# --------------------------------------------------------------------------------------------------
# Календарь и тексты
# --------------------------------------------------------------------------------------------------


class CalendarItem(KitModel):
    topic_id: TopicId | None = None
    is_fa: bool = False  # итоговая аттестация
    kind: WorkKind | None = None
    hours: Hours


class CalendarDay(KitModel):
    id: DayId
    n: int = Field(ge=1)  # номер учебного дня / занятия
    week: int = Field(ge=1)
    items: list[CalendarItem] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hours(self) -> float:
        return sum(item.hours for item in self.items)


class Calendar(KitModel):
    mode: CalendarMode
    weeks: int = Field(ge=0)
    days_per_week: int | None = Field(default=None, ge=1, le=7)
    sessions_per_week: int | None = Field(default=None, ge=1)
    days: list[CalendarDay] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ProgramTexts(KitModel):
    annotation: str = ""  # 1.1 ПК
    relevance: str = ""  # 1.1 ДОП «Актуальность»
    form_description: str = ""  # 1.7 ПК / пояснительная записка ДОП
    audience_requirements: str = ""  # 1.6 ПК / 1.2 ДОП
    mto: list[str] = Field(default_factory=list)  # материально-техническое обеспечение
    software: list[str] = Field(default_factory=list)  # ПО и информационные системы
    org_process: str = ""  # 3.3 ПК
    staffing_requirements: str = ""  # 3.4
    methodical: str = ""  # 2.4 ДОП «Методические материалы»
    satisfaction: str = ""  # 2.5.3 ПК


# --------------------------------------------------------------------------------------------------
# Отчёт проверки
# --------------------------------------------------------------------------------------------------


class Issue(KitModel):
    id: IssueId
    check: CheckId
    severity: Severity
    target_ids: list[AnyId] = Field(default_factory=list)
    message: str  # для методиста, без жаргона
    detail: str = ""  # для разработчика
    checklist_refs: list[str] = Field(default_factory=list)  # ["G1", "Q5.2"]
    fix_by: FixBy = FixBy.HUMAN
    fixed: bool = False


class WarningFigure(KitModel):
    """Цифра к предупреждению методисту: подпись и значение так, как их показывать («Расчётный срок» — «11 недель»)."""

    label: str
    value: str


class MethodistWarning(KitModel):
    """Предупреждение методисту по общему правилу (app/core/methodist_warnings.py): что не так — с цифрами,
    и варианты решения. Пишется без жаргона: ни ID объектов, ни названий проверок."""

    type: WarningType
    title: str = ""  # коротко, что случилось: «Желаемый срок не сходится с нагрузкой»
    text: str  # подробно, с цифрами
    figures: list[WarningFigure] = Field(
        default_factory=list
    )  # главные цифры отдельно — для таблиц и интерфейса
    options: list[str] = Field(default_factory=list)  # варианты решения
    # вариант text для приложения к самой программе (.docx), если в text есть то, что в программу не идёт:
    # цитаты приложенного образца, номера утративших силу актов. Пусто — text годится и для программы.
    program_text: str = ""
    code: str = ""  # уточнение вида для программ (код конфликта солвера и т.п.)
    target_ids: list[AnyId] = Field(default_factory=list)


CoverageStatus = Literal["ok", "single", "unassessed", "no_topic"]


class LOCoverage(KitModel):
    """Строка матрицы «результат обучения × темы × оценочные средства»."""

    lo_id: OutcomeId
    text: str = ""  # формулировка результата — чтобы отчёт читался без комплекта
    kind: OutcomeKind | None = None
    topic_ids: list[TopicId] = Field(default_factory=list)
    assessment_ids: list[AnyId] = Field(default_factory=list)  # Q*, S*, IQ*, FA
    # ok — раскрыт в теме и проверяется не меньше чем двумя средствами; single — одним; unassessed — ни одним;
    # no_topic — не раскрыт ни в одной теме
    status: CoverageStatus | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def by_kind(self) -> dict[str, int]:
        """Оценочные средства по видам: test_questions, seminars, interview, final_attestation."""
        counts: dict[str, int] = {}
        for item in self.assessment_ids:
            kind = assessment_kind(item)
            counts[kind] = counts.get(kind, 0) + 1
        return counts


def assessment_kind(assessment_id: str) -> str:
    if assessment_id.startswith("IQ"):
        return "interview"
    if assessment_id.startswith("Q"):
        return "test_questions"
    if assessment_id.startswith("S"):
        return "seminars"
    return "final_attestation"


class HoursLine(KitModel):
    """Строка сводки часов по уровням: модуль, его темы, итоговая аттестация, итог."""

    level: Literal["module", "topic", "final_attestation", "total"]
    id: str = ""  # M1 / T1.2 / FA / TOTAL
    number: str = ""  # отображаемый номер: «1», «1.2»
    title: str = ""
    total: Hours = 0  # у ИА часы не делятся по видам работы — только total
    hours: HoursRow = Field(default_factory=HoursRow)


class HoursSummary(KitModel):
    """Часы по уровням: темы → модули → итог; итоговая аттестация; контактная работа, СРС, ДОТ."""

    declared: Hours = 0  # объём из запроса
    total: Hours = 0  # по учебному плану: темы + итоговая аттестация
    topics: Hours = 0  # сумма часов тем
    final_attestation: Hours = 0
    lk: Hours = 0
    pr: Hours = 0
    lb: Hours = 0
    srs: Hours = 0
    contact: Hours = 0  # ЛК + ПР + ЛБ
    distance: Hours = 0  # часть контактной работы с ДОТ
    in_person: Hours = 0
    lines: list[HoursLine] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def matches(self) -> bool:
        return abs(self.declared - self.total) < 1e-6


class SourceSummary(KitModel):
    """Источники: подтверждено (реестр, каталог, онлайн-сверка) / не подтверждено (с пометкой) / отброшено."""

    total: int = 0  # в списке литературы программы
    confirmed: int = 0
    unverified: int = 0
    rejected: int = 0  # предложены моделью и не прошли формальную проверку (ISBN, год, издательство)
    dropped: int = 0  # сверх лимита неподтверждённых на раздел
    by_verification: dict[str, int] = Field(default_factory=dict)  # registry / catalog / online / unverified
    by_section: dict[str, dict[str, int]] = Field(default_factory=dict)  # раздел → {confirmed, unverified}
    unverified_ids: list[SourceId] = Field(default_factory=list)


class CopySummary(KitModel):
    """Совпадение текстов комплекта с приложенными образцами (шинглы по 7 слов, общие формулировки не в счёт)."""

    references: int = 0  # сколько образцов сравнивалось; 0 — сравнивать не с чем
    ratio: float = Field(default=0, ge=0, le=1)  # доля совпавших шинглов во всём комплекте
    threshold: float = Field(default=0, ge=0, le=1)
    fields_flagged: int = 0  # полей, почти дословно повторяющих образец

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        return self.ratio <= self.threshold + 1e-9 and self.fields_flagged == 0


class JudgeResult(KitModel):
    criterion: str = Field(pattern=r"^J\d+$")
    passed: bool
    reason: str = ""
    title: str = ""  # формулировка критерия для отчёта


class StepCost(KitModel):
    step: str
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_rub: float = 0
    seconds: float = 0


class ValidationReport(KitModel):
    """Отчёт самопроверки (report.json). Заполняют validate (проверки и сводки), шаг validate пайплайна
    (судья, стоимость, время по шагам) и выгрузка (итог одной фразой, время генерации, сверка файлов V31)."""

    generated_at: datetime | None = None
    summary: str = ""  # итог одной фразой для методиста
    issues: list[Issue] = Field(default_factory=list)
    hours: HoursSummary | None = None
    coverage: list[LOCoverage] = Field(default_factory=list)
    sources: SourceSummary | None = None
    sections: dict[str, bool] = Field(default_factory=dict)  # раздел шаблона → заполнен
    copying: CopySummary | None = None  # антиплагиат против приложенного образца
    warnings_for_methodist: list[MethodistWarning] = Field(default_factory=list)
    judge: list[JudgeResult] = Field(default_factory=list)
    cost: list[StepCost] = Field(default_factory=list)
    timing: dict[str, float] = Field(default_factory=dict)  # шаг → секунды
    generation_seconds: float | None = None  # от начала генерации комплекта до выгрузки файлов

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy(cls, data: object) -> object:
        """kit.json прежних версий: hours был снимком «модуль → часы», sources — «статус → число». Такие сводки
        отбрасываются — следующая проверка соберёт новые."""
        if isinstance(data, dict):
            hours, sources = data.get("hours"), data.get("sources")
            if isinstance(hours, dict) and "lines" not in hours and "declared" not in hours:
                data = {**data, "hours": None}
            if isinstance(sources, dict) and "total" not in sources and "confirmed" not in sources:
                data = {**data, "sources": None}
        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> ReportStatus:
        open_issues = [i for i in self.issues if not i.fixed]
        if any(i.severity == Severity.ERROR for i in open_issues):
            return ReportStatus.ERRORS
        if any(i.severity == Severity.WARNING for i in open_issues) or self.warnings_for_methodist:
            return ReportStatus.WARNINGS
        return ReportStatus.OK

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cost_rub(self) -> float:
        return round(sum(c.cost_rub for c in self.cost), 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def llm_calls(self) -> int:
        return sum(c.calls for c in self.cost)


# --------------------------------------------------------------------------------------------------
# Разбор запроса и референса
# --------------------------------------------------------------------------------------------------


class TopicRequirement(KitModel):
    """Обязательная тема из запроса: метка и регулярки, хотя бы одна должна совпасть с темой/модулем."""

    label: str
    patterns: list[str] = Field(default_factory=list)


class Constraints(KitModel):
    ru_services_only: bool = False
    min_practice_share: float | None = Field(default=None, ge=0, le=1)
    all_srs: bool = False  # все часы — самостоятельная работа (стажировка без аудиторных)
    no_programming: bool = False
    anonymized_data: bool = False
    intro_block_topic: str | None = None  # нужен вводный блок по ... (входной уровень ниже нужного)
    required_seminar_types: list[TopicRequirement] = Field(default_factory=list)


class Outputs(KitModel):
    test_questions: int | None = Field(default=None, ge=1)  # объём итогового теста, если задан
    slides_scope: SlidesScope = Field(default_factory=SlidesScope)


class RefHeading(KitModel):
    level: int = Field(ge=1)
    number: str | None = None  # «2.5.1»
    title: str


class RefHoursRow(KitModel):
    number: str | None = None  # «1.2.»
    title: str = ""
    row_type: Literal["section", "topic", "control", "fa", "total", "other"] = "topic"
    values: dict[str, float | None] = Field(default_factory=dict)  # колонка → число как в документе
    form: str | None = None  # форма аттестации / контроля


class RefHoursTable(KitModel):
    kind: Literal["plan", "utp", "dop_plan", "other"]
    columns: list[str] = Field(default_factory=list)  # нормализованные ключи: total, lk, pr, lb, srs, contact
    raw_header: list[str] = Field(default_factory=list)
    rows: list[RefHoursRow] = Field(default_factory=list)


class RefWorkItem(KitModel):
    """Блок вида работы в рабочей программе: метка «ПР (9 часов)» против числа в колонке."""

    topic_number: str | None = None
    topic_title: str = ""
    kind_label: str = ""  # как написано: «ЛР», «ПР», «СРС», «Текущий контроль»
    kind: WorkKind | None = None
    label_hours: float | None = None  # число из метки
    column_hours: float | None = None  # число из колонки «Всего (час.)»


class ParsedReference(KitModel):
    """Приложенная программа, разобранная кодом (app/core/reference.py). Числа — сырые, как в документе."""

    source_name: str = ""
    program_type_guess: ProgramType | None = None
    headings: list[RefHeading] = Field(default_factory=list)
    hour_tables: list[RefHoursTable] = Field(default_factory=list)
    workprogram: list[RefWorkItem] = Field(default_factory=list)
    work_kind_labels: list[str] = Field(default_factory=list)  # встреченные обозначения: ЛК, ПР, ЛБ, ЛР, СРС
    order_requisites: str | None = None  # строка «Приказом № … от …» как есть
    requisites_filled: bool = False  # реквизиты приказа заполнены (не «____»)
    ps_codes: list[str] = Field(default_factory=list)
    tf_codes: list[str] = Field(default_factory=list)
    order_refs: list[str] = Field(default_factory=list)  # упомянутые приказы / письма


class RequestSpec(KitModel):
    """Разобранный запрос методиста (выход intake после сверки кодом)."""

    raw_text: str = ""
    program_type: ProgramType | None = None
    title: str | None = None
    total_hours: Hours | None = None
    audience: Audience = Field(default_factory=Audience)
    form: StudyForm | None = None
    weeks: int | None = Field(default=None, ge=1)  # желаемый срок в неделях
    duration_text: str | None = None  # «около двух месяцев», «примерно месяц»
    hours_per_day_max: Hours | None = None
    session_hours: Hours | None = None
    sessions_per_week: int | None = Field(default=None, ge=1)
    fa_form: FAForm | None = None
    fa_text: str | None = None
    fa_hours: Hours | None = None
    portfolio_items_count: int | None = Field(default=None, ge=1)
    required_topics: list[TopicRequirement] = Field(default_factory=list)
    constraints: Constraints = Field(default_factory=Constraints)
    outputs: Outputs = Field(default_factory=Outputs)
    reference: ParsedReference | None = None
    missing: list[str] = Field(default_factory=list)  # параметры, которых не хватает (для clarify)
    assumptions: list[str] = Field(default_factory=list)  # что агент предположил сам


# --------------------------------------------------------------------------------------------------
# Комплект
# --------------------------------------------------------------------------------------------------


class PatchRecord(KitModel):
    """Запись истории правок готового комплекта (раздел «История правок» в отчёте)."""

    revision: int = Field(ge=1)
    operation: PatchOperation
    target_ids: list[AnyId] = Field(default_factory=list)
    instruction: str = ""
    changed_ids: list[AnyId] = Field(default_factory=list)
    removed_ids: list[AnyId] = Field(default_factory=list)  # удалённые объекты (их ID — в retired_ids)
    summary: str = ""  # что сделано — для методиста, без служебных ID
    planned_by: Literal["rules", "model"] = "rules"  # команду разобрал код или навык patch
    cost_rub: float = Field(default=0, ge=0)
    at: datetime


class KitMeta(KitModel):
    created_at: datetime | None = None
    llm_mode: Literal["mock", "live", "replay"] = "mock"
    generation_mode: GenerationMode = GenerationMode.FULL
    skills_version: str = ""
    timing: dict[str, float] = Field(default_factory=dict)
    cost: list[StepCost] = Field(default_factory=list)
    budget: dict[str, float] = Field(default_factory=dict)  # оценка полного и экономного режима, остаток, ₽
    judge: list[JudgeResult] = Field(default_factory=list)  # смысловая проверка (навык judge) — в отчёт
    generation_notes: list[str] = Field(default_factory=list)  # что пропущено и почему — для отчёта методисту
    literature_checks: dict[str, int] = Field(default_factory=dict)  # кандидаты литературы: статус → сколько


KitObject = (
    Competency
    | LearningOutcome
    | Module
    | Topic
    | Lecture
    | Seminar
    | Test
    | TestQuestion
    | InterviewQuestion
    | Source
    | FinalAttestation
)


class Kit(KitModel):
    """Весь комплект. Суммы часов — только через методы/computed-поля."""

    schema_version: str = "1.0"
    revision: int = Field(default=0, ge=0)
    request: RequestSpec = Field(default_factory=RequestSpec)
    program: Program
    competencies: list[Competency] = Field(default_factory=list)
    outcomes: list[LearningOutcome] = Field(default_factory=list)
    modules: list[Module] = Field(default_factory=list)
    topics: list[Topic] = Field(default_factory=list)
    lectures: list[Lecture] = Field(default_factory=list)
    seminars: list[Seminar] = Field(default_factory=list)
    tests: list[Test] = Field(default_factory=list)
    questions: list[TestQuestion] = Field(default_factory=list)
    interview_questions: list[InterviewQuestion] = Field(default_factory=list)
    final_attestation: FinalAttestation | None = None
    sources: list[Source] = Field(default_factory=list)
    calendar: Calendar | None = None
    texts: ProgramTexts = Field(default_factory=ProgramTexts)
    report: ValidationReport | None = None
    history: list[PatchRecord] = Field(default_factory=list)
    retired_ids: list[AnyId] = Field(default_factory=list)  # удалённые ID — не переиспользуются
    meta: KitMeta = Field(default_factory=KitMeta)

    # ---- навигация -------------------------------------------------------------------------------

    def index(self) -> dict[str, KitObject]:
        """ID → объект (строится заново при каждом вызове: комплект изменяемый)."""
        result: dict[str, KitObject] = {}
        groups: list[list] = [
            self.competencies,
            self.outcomes,
            self.modules,
            self.topics,
            self.lectures,
            self.seminars,
            self.tests,
            self.questions,
            self.interview_questions,
            self.sources,
        ]
        for group in groups:
            for obj in group:
                result[obj.id] = obj
        if self.final_attestation is not None:
            result["FA"] = self.final_attestation
        return result

    def modules_sorted(self) -> list[Module]:
        return sorted(self.modules, key=lambda m: (m.order, m.id))

    def topics_of(self, module_id: str) -> list[Topic]:
        return sorted((t for t in self.topics if t.module_id == module_id), key=lambda t: (t.order, t.id))

    def outcomes_of(self, competency_id: str) -> list[LearningOutcome]:
        return [o for o in self.outcomes if o.competency_id == competency_id]

    def competency_kun(self, competency_id: str) -> dict[OutcomeKind, list[str]]:
        """Знать / уметь / владеть компетенции — из её РО (для таблицы 1.3 ПК и 1.5 ДОП)."""
        result: dict[OutcomeKind, list[str]] = {kind: [] for kind in OutcomeKind}
        for outcome in self.outcomes_of(competency_id):
            result[outcome.kind].append(outcome.text)
        return result

    # ---- часы (только вычисление) ----------------------------------------------------------------

    def module_hours(self, module_id: str) -> HoursRow:
        return HoursRow.sum([t.hours for t in self.topics_of(module_id)])

    def program_hours(self) -> HoursRow:
        """Сумма всех тем (без итоговой аттестации)."""
        return HoursRow.sum([t.hours for t in self.topics])

    def fa_hours(self) -> float:
        return self.final_attestation.hours if self.final_attestation is not None else 0.0

    def total_hours(self) -> float:
        """Итоговая трудоёмкость: сумма тем + ИА. Должна быть равна program.total_hours (V03)."""
        return self.program_hours().total + self.fa_hours()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hours_by_module(self) -> dict[str, HoursRow]:
        return {m.id: self.module_hours(m.id) for m in self.modules_sorted()}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def computed_total_hours(self) -> float:
        return self.total_hours()


# --------------------------------------------------------------------------------------------------
# Черновики ответов LLM (схемы response_format). Полей часов здесь нет.
# --------------------------------------------------------------------------------------------------


class ClarifyQuestion(DraftModel):
    id: str  # параметр: program_type, hours, audience, form, final_attestation, ...
    text: str
    options: list[str] = Field(default_factory=list)


class QuantityMention(DraftModel):
    """Модель цитирует фрагмент запроса с количеством; число из него извлекает код."""

    what: Literal[
        "total_hours",
        "weeks",
        "months",
        "hours_per_day",
        "session_hours",
        "sessions_per_week",
        "test_questions",
        "fa_hours",
        "portfolio_items",
    ]
    text: str


class IntakeDraft(DraftModel):
    program_type_guess: ProgramType | None = None
    title: str | None = None
    audience: Audience = Field(default_factory=Audience)
    form_kind: StudyFormKind | None = None
    dot: bool = False
    eo: bool = False
    quantities: list[QuantityMention] = Field(default_factory=list)
    duration_text: str | None = None
    fa_form: FAForm | None = None
    fa_text: str | None = None
    required_topics: list[str] = Field(default_factory=list)
    required_seminar_types: list[str] = Field(default_factory=list)
    ru_services_only: bool = False
    no_programming: bool = False
    anonymized_data: bool = False
    all_srs: bool = False
    intro_block_topic: str | None = None
    slides_request: str | None = None  # как сказано в запросе: «слайды для модуля про pandas»
    assumptions: list[str] = Field(default_factory=list)


class ClarifyDraft(DraftModel):
    questions: list[ClarifyQuestion] = Field(default_factory=list, max_length=5)


class WorkMix(DraftModel):
    """Относительные доли видов работы темы в процентах (нормирует солвер)."""

    lk: int = Field(default=0, ge=0, le=100)
    pr: int = Field(default=0, ge=0, le=100)
    lb: int = Field(default=0, ge=0, le=100)
    srs: int = Field(default=0, ge=0, le=100)


class OutcomeDraft(DraftModel):
    key: str  # уникальный в программе: «c1_lo1»
    kind: OutcomeKind
    text: str
    tf_id: str | None = None


class CompetencyDraft(DraftModel):
    """Результаты обучения вложены в компетенцию: плоский список с перекрёстными ключами GigaChat путает
    (живой прогон 19.10: ключи результатов совпали с ключами компетенций, ссылки тем — ни с чем)."""

    key: str
    activity: str = ""
    title: str
    tf_ids: list[str] = Field(default_factory=list)  # только из переданного списка реестра
    outcomes: list[OutcomeDraft] = Field(default_factory=list)  # знать / уметь / владеть этой компетенции


class TopicDraft(DraftModel):
    key: str
    title: str
    weight: int = Field(ge=1, le=10)
    mix: WorkMix = Field(default_factory=WorkMix)
    lo_keys: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    control_form: str | None = None


class ModuleDraft(DraftModel):
    key: str
    title: str
    weight: int = Field(ge=1, le=10)
    kind: ModuleKind = ModuleKind.REGULAR
    control_form: str | None = None
    topics: list[TopicDraft] = Field(default_factory=list)


class SkeletonDraft(DraftModel):
    goal: str
    tasks: list[str] = Field(default_factory=list)
    direction: str | None = None
    level: str | None = None
    competencies: list[CompetencyDraft] = Field(default_factory=list)
    modules: list[ModuleDraft] = Field(default_factory=list)


class TopicContentDraft(DraftModel):
    topic_id: str
    lk: list[str] = Field(default_factory=list)
    pr: list[str] = Field(default_factory=list)
    lb: list[str] = Field(default_factory=list)
    srs: list[str] = Field(default_factory=list)
    control_form: str | None = None


class ModuleContentDraft(DraftModel):
    topics: list[TopicContentDraft] = Field(default_factory=list)


class PlanItemDraft(DraftModel):
    """Раздел плана лекции от модели: ссылки на РО — строки, их проверяет код (битая ссылка не роняет ответ)."""

    title: str
    theses: list[str] = Field(default_factory=list)
    lo_ids: list[str] = Field(default_factory=list)


class GlossaryDraft(DraftModel):
    term: str
    definition: str


class LectureDraft(DraftModel):
    topic_ids: list[str] = Field(default_factory=list)
    title: str
    goal: str = ""
    plan: list[PlanItemDraft] = Field(default_factory=list)
    glossary: list[GlossaryDraft] = Field(default_factory=list)


class LecturesDraft(DraftModel):
    lectures: list[LectureDraft] = Field(default_factory=list)


class SlidesDraft(DraftModel):
    lecture_id: str
    slides: list[Slide] = Field(default_factory=list)


class CriterionDraft(DraftModel):
    text: str
    max_points: float = 0  # баллы > 0 проверяет код (content_apply), а не схема


class SeminarDraft(DraftModel):
    topic_id: str
    lo_ids: list[str] = Field(default_factory=list)
    kind: SeminarKind = SeminarKind.PRACTICE
    title: str
    situation: str = ""
    task: str = ""
    steps: list[str] = Field(default_factory=list)
    deliverable: str = ""
    criteria: list[CriterionDraft] = Field(default_factory=list)
    pass_threshold: float | None = None


class SeminarsDraft(DraftModel):
    seminars: list[SeminarDraft] = Field(default_factory=list)


class QuestionDraft(DraftModel):
    """Правильные ответы — текстом варианта (correct_answers), а не номером: модели путают счёт с 0 и с 1."""

    topic_id: str
    lo_ids: list[str] = Field(default_factory=list)
    kind: QuestionKind = QuestionKind.SINGLE
    stem: str
    options: list[str] = Field(default_factory=list)
    correct_answers: list[str] = Field(default_factory=list)
    explanation: str = ""
    difficulty: int = 1  # 1–3 приводит код


class InterviewDraft(DraftModel):
    module_id: str
    lo_ids: list[str] = Field(default_factory=list)
    text: str
    answer_points: list[str] = Field(default_factory=list)
    criteria: list[str] = Field(default_factory=list)


class TestsDraft(DraftModel):
    __test__ = False  # не тестовый класс для pytest

    questions: list[QuestionDraft] = Field(default_factory=list)
    interview: list[InterviewDraft] = Field(default_factory=list)


class PortfolioItemDraft(DraftModel):
    title: str
    requirements: list[str] = Field(default_factory=list)


class FinalAttestationDraft(DraftModel):
    form_text: str
    lo_ids: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    portfolio_items: list[PortfolioItemDraft] = Field(default_factory=list)
    example_task: ExampleTask | None = None
    data_requirements: list[str] = Field(default_factory=list)
    expected_result: str | None = None
    report_requirements: list[str] = Field(default_factory=list)
    criteria: list[CriterionDraft] = Field(default_factory=list)
    pass_rule: str = ""
    procedure: list[str] = Field(default_factory=list)


class ProgramTextsDraft(DraftModel):
    annotation: str = ""
    relevance: str = ""
    form_description: str = ""
    audience_requirements: str = ""
    mto: list[str] = Field(default_factory=list)
    software: list[str] = Field(default_factory=list)
    org_process: str = ""
    staffing_requirements: str = ""
    methodical: str = ""
    satisfaction: str = ""


class CatalogPick(DraftModel):
    catalog_id: str
    section: SourceSection


class BookCandidate(DraftModel):
    authors: list[str] = Field(default_factory=list)
    title: str
    year: int | None = None
    publisher: str | None = None
    isbn: str | None = None
    url: str | None = None
    section: SourceSection = SourceSection.ADDITIONAL
    topic_ids: list[str] = Field(default_factory=list)


class LiteratureDraft(DraftModel):
    selected: list[CatalogPick] = Field(default_factory=list)
    candidates: list[BookCandidate] = Field(default_factory=list)  # не больше 6 берёт код


class PatchPlanDraft(DraftModel):
    """План точечной правки от навыка patch. Модель только называет операцию и объект; часы она не пересчитывает,
    а цитирует фрагмент указания с числами (quote) — числа из него извлекает и раскладывает код."""

    operation: str = (
        ""  # replace_topic | change_hours | harden_test | replace_lo | add_topic | remove_topic | regenerate
    )
    target: str = ""  # ID (T2.3) или «тема 2.3»; пусто — объект из запроса правки
    title: str = ""  # новое название темы (replace_topic, add_topic)
    text: str = ""  # новая формулировка результата обучения (replace_lo)
    module: str = ""  # модуль новой темы (add_topic)
    quote: str = ""  # фрагмент указания с часами — дословно (change_hours, add_topic)
    focus: str = ""  # что учесть при пересборке — коротко, своими словами


class ModulePackDraft(DraftModel):
    """Экономный режим (module_pack): содержание тем, лекции, задания и вопросы модуля — одним ответом."""

    topics: list[TopicContentDraft] = Field(default_factory=list)
    lectures: list[LectureDraft] = Field(default_factory=list)
    seminars: list[SeminarDraft] = Field(default_factory=list)
    questions: list[QuestionDraft] = Field(default_factory=list)
    interview: list[InterviewDraft] = Field(default_factory=list)


class JudgeItemDraft(DraftModel):
    # reason до passed: модель сначала перечисляет проверенное, потом выносит вердикт — с вердиктом в начале
    # смысловая проверка ставила «пройдено» почти всему (оценка 21.10: 7–8 из 8 во всех запросах)
    criterion: str
    reason: str = ""
    passed: bool


class JudgeDraft(DraftModel):
    results: list[JudgeItemDraft] = Field(default_factory=list)
