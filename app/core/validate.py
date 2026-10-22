"""Детерминированные проверки комплекта V01–V46 (docs/ARCHITECTURE.md §4, docs/checks_matrix.md).

Как устроено:
- каждая проверка — функция ``check_vNN(kit, ctx) -> list[Finding]``; реестр проверок — ``CHECKS``
  (V31 «выгрузки» живёт вне этого модуля: ей нужны готовые файлы рендера);
- ``validate()`` запускает выбранные проверки, превращает находки в ``Issue`` (ID «V02-1») и собирает
  ``ValidationReport``: часы по уровням, покрытие РО, источники, полноту разделов шаблона, антиплагиат
  и предупреждения методисту по общим правилам (app/core/methodist_warnings.py);
- ``validate_reference()`` — логика V45 на сырых числах приложенного образца (``ParsedReference``).

Параметры проверок — только из ``kit.request`` (RequestSpec) и ``kit.program``, пороги — из
``ValidationContext``. Никаких констант под конкретные запросы.

Часы в модели вычисляются (суммы строк, модулей и «Итого» не хранятся), поэтому V01/V02 на Kit
проверяют то, что в модели можно нарушить (ЛБ в ДОП, темы вне модулей, пустые модули и т.п.);
полная арифметика «как в документе» — на сырых числах референса (V45) и на снимке таблиц рендера (V31).
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal

from app.core import copycheck
from app.core import methodist_warnings as mw
from app.core.ids import display_numbers, issue_id
from app.core.questions import same_question
from app.core.registry_api import NormRecord, RegistryView, fold_yo, name_pattern, normalize_text
from app.core.subject import without_subject
from app.models import (
    CalendarMode,
    CopySummary,
    FAForm,
    FixBy,
    HoursLine,
    HoursRow,
    HoursSummary,
    Issue,
    Kit,
    Lecture,
    LOCoverage,
    MethodistWarning,
    ModuleKind,
    OutcomeKind,
    ParsedReference,
    ProgramType,
    QuestionKind,
    RefHoursRow,
    RefHoursTable,
    RefWorkItem,
    Seminar,
    SeminarKind,
    Severity,
    SlidesScope,
    Source,
    SourceKind,
    SourceSection,
    SourceSummary,
    StudyFormKind,
    TestScope,
    Topic,
    TopicRequirement,
    ValidationReport,
    Verification,
    WarningType,
    WorkKind,
)

__all__ = [
    "CHECKS",
    "CheckSpec",
    "Finding",
    "ValidationContext",
    "copy_summary",
    "coverage_matrix",
    "hours_summary",
    "methodist_warnings",
    "section_status",
    "source_summary",
    "validate",
    "validate_reference",
]

# --------------------------------------------------------------------------------------------------
# Константы
# --------------------------------------------------------------------------------------------------

EPS = 1e-6
ERROR, WARNING, INFO = Severity.ERROR, Severity.WARNING, Severity.INFO
PK_TYPES = (ProgramType.PK, ProgramType.PK_INTERNSHIP)
KINDS = (WorkKind.LK, WorkKind.PR, WorkKind.LB, WorkKind.SRS)
KIND_ABBR = {WorkKind.LK: "ЛК", WorkKind.PR: "ПР", WorkKind.LB: "ЛБ", WorkKind.SRS: "СРС"}
DOP_KIND_NAMES = {
    WorkKind.LK: "теория",
    WorkKind.PR: "практика",
    WorkKind.LB: "ЛБ",
    WorkKind.SRS: "самостоятельная работа",
}
TYPE_NAMES = {
    ProgramType.PK: "повышение квалификации",
    ProgramType.PK_INTERNSHIP: "повышение квалификации в форме стажировки",
    ProgramType.DOP: "дополнительная общеразвивающая программа",
}
FA_NAMES = {
    FAForm.PROJECT_DEFENSE: "защита проекта",
    FAForm.PORTFOLIO: "зачёт по портфолио",
    FAForm.EXAM_PRACTICAL: "экзамен с практическим заданием",
    FAForm.REPORT_DEFENSE: "защита отчёта",
    FAForm.TEST: "итоговое тестирование",
    FAForm.ZACHET: "зачёт",
    FAForm.OTHER: "другая форма",
}
KUN_NAMES = {
    "pk": {OutcomeKind.KNOW: "знания", OutcomeKind.CAN: "умения", OutcomeKind.MASTER: "практический опыт"},
    "dop": {OutcomeKind.KNOW: "знания", OutcomeKind.CAN: "умения", OutcomeKind.MASTER: "навыки"},
}

# Нормативная база по типу программы (ID записей data/registry/norms.json).
REQUIRED_NORMS: dict[str, tuple[str, ...]] = {
    "pk": ("fz273", "minobrnauki266"),
    "dop": ("fz273", "minpros629"),
}
# ЭО и ДОТ: Правила, утв. постановлением Правительства РФ от 11.10.2023 № 1678 (с 01.09.2024);
# приказ Минобрнауки № 816 утратил силу с 01.09.2023 — ссылка на него из образца АЦР не переносится.
DOT_NORM = "gov1678"
# Запасные сведения на случай, когда реестр не подключён (с реестром всё берётся из записей).
KNOWN_REPLACED_NORMS = {"minobrnauki499": "minobrnauki266", "minobrnauki816": "gov1678"}
NORM_FALLBACK_NAMES = {
    "fz273": "Федеральный закон № 273-ФЗ «Об образовании в Российской Федерации»",
    "minobrnauki266": "приказ Минобрнауки России № 266 (Порядок по ДПП)",
    "minobrnauki499": "приказ Минобрнауки России № 499",
    "minpros629": "приказ Минпросвещения России № 629 (Порядок по дополнительным общеобразовательным программам)",
    "minobrnauki816": "приказ Минобрнауки России № 816 (электронное обучение и ДОТ)",
    "gov1678": "постановление Правительства РФ № 1678 (Правила применения электронного обучения и ДОТ)",
}
# Порядок по ДПП на случай, когда реестр не подключён (с реестром всё берётся из записей).
NORM_REPLACED_FALLBACK = mw.NormReplacement(
    current="Приказ Минобрнауки России от 24.03.2025 № 266",
    old="Приказ Минобрнауки России от 01.07.2013 № 499",
    current_since="01.09.2025",
    old_until="01.09.2025",
)
NORM_KIND_WORDS = {
    "federal_law": "Федеральный закон",
    "order": "приказ",
    "decree": "постановление Правительства РФ",
    "letter": "письмо",
}

# Предупреждения, которые пишет не validate (солвер часов, календарь): переносятся из прошлого отчёта.
EXTERNAL_WARNING_TYPES = (
    WarningType.SCHEDULE_TIGHT,
    WarningType.CONSTRAINT_CONFLICT,
)
UNVERIFIED_MARK = "[не подтверждено]"

# Типовые окружения практического экзамена: если запрос называет одно из них, оно должно быть в примере.
ENV_WORDS = ("стенд", "виртуальн", "лаборатор", "тренажер", "тренажёр", "полигон", "песочниц", "эмулятор")

# --------------------------------------------------------------------------------------------------
# Регулярные выражения
# --------------------------------------------------------------------------------------------------

NUM = r"(\d+(?:[.,]\d+)?)"
HOURS_LABEL_RE = re.compile(r"(?<![\w])(ЛК|ПР|ЛБ|ЛР|СРС|СР)\s*\(\s*" + NUM + r"\s*(?:академ\w*\s*)?час", re.I)
DOP_LABEL_RES: tuple[tuple[re.Pattern[str], WorkKind], ...] = (
    (re.compile(r"(?:теоретическ\w*\s+заняти\w*|лекци\w*)\s*[–—-]\s*" + NUM + r"\s*час", re.I), WorkKind.LK),
    (re.compile(r"практическ\w*\s+заняти\w*\s*[–—-]\s*" + NUM + r"\s*час", re.I), WorkKind.PR),
    (re.compile(r"самостоятельн\w*\s+работ\w*\s*[–—(-]\s*" + NUM + r"\s*час", re.I), WorkKind.SRS),
)
ABBR_KIND = {
    "ЛК": WorkKind.LK,
    "ПР": WorkKind.PR,
    "ЛБ": WorkKind.LB,
    "ЛР": WorkKind.LB,
    "СРС": WorkKind.SRS,
    "СР": WorkKind.SRS,
}
_HOURS_TAIL = r"\s*(?:академ\w*\s+)?час\w*"
VOLUME_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("contact", re.compile(r"контактн\w*\s+работ\w*\D{0,40}?" + NUM + _HOURS_TAIL, re.I)),
    (
        "srs",
        re.compile(
            r"(?:самостоятельн\w*\s+работ\w*(?:\s+слушател\w*)?(?:\s*\(СРС\))?|(?<![\w])СРС(?![\w]))"
            r"\D{0,40}?" + NUM + _HOURS_TAIL,
            re.I,
        ),
    ),
    ("fa", re.compile(r"итогов\w*\s+аттестаци\w*\D{0,40}?" + NUM + _HOURS_TAIL, re.I)),
    (
        "total",
        re.compile(
            r"(?:объ[её]м\w*|трудо[её]мкост\w*)(?:\s+программ\w*)?\D{0,40}?" + NUM + _HOURS_TAIL, re.I
        ),
    ),
)
RATE_TAIL_RE = re.compile(
    r"^\s*(?:в\s+(?:день|неделю|сутки)|ежедневно|на\s+занятие|за\s+занятие|в\s+недел)", re.I
)
INTERVIEW_RE = re.compile(r"собеседован", re.I)
TK_INTERVIEW_REQ_RE = re.compile(
    r"текущ\w*\s+контрол\w*[^.;\n]{0,80}собеседован|собеседован\w*[^.;\n]{0,80}текущ\w*\s+контрол", re.I
)
ANON_RE = re.compile(r"обезличен|анонимизир|деперсонализ", re.I)
PS_WORD_RE = re.compile(r"профстандарт|профессиональн\w*\s+стандарт|трудов\w*\s+функци", re.I)
PS_CODE_RE = re.compile(r"(?<![\d.])\d{2}\.\d{3}(?!\d|\.\d)")  # точка конца предложения — не часть кода
# «15.000 тыс. руб.», «версия 10.123» — не коды ПС: код считается кодом ПС только рядом со словами о профстандарте
PS_CONTEXT_RE = re.compile(
    r"профстандарт|профессиональн\w*\s+стандарт|трудов\w*\s+функци|(?<![\w])(?-i:ПС)(?![\w])", re.I
)
PS_CONTEXT_BEFORE = 80
PS_CONTEXT_AFTER = 40
TF_CODE_RE = re.compile(r"(?<![\w/])[A-HАВСЕН]/\d{2}\.\d(?!\d)")
# формы слов целиком: «указанные в задании № 2», «письменный ответ на вопрос № 3» — не реквизиты актов
ACT_RE = re.compile(
    r"(?<![\w])(?:приказ(?:а|у|ом|е|ы|ов|ам|ами|ах)?|письм(?:о|а|у|ом|е)|постановлени\w*|распоряжени\w*"
    r"|указ(?:а|у|ом|е|ы|ов)?|закон(?:а|у|ом|е|ы|ов)?)(?![\w])[^№\n]{0,80}?№\s*"
    r"([0-9A-Za-zА-Яа-яЁё][\w./-]*)",
    re.I,
)
FZ_RE = re.compile(r"(?<![\w-])(\d{1,4}-ФЗ)(?![\w])")
NUMBER_RE = re.compile(r"№\s*([0-9A-Za-zА-Яа-яЁё][\w./-]*)")
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
ORDER_FILLED_RES = (
    re.compile(r"\d+\s*-\s*ОБР(?![\w])"),
    re.compile(r"Приказом\s+№\s*\d", re.I),
    re.compile(r"от\s+«\s*\d{1,2}\s*»\s+[а-яё]+\s+\d{4}", re.I),
)
FIO_INITIALS_RE = re.compile(r"(?<![\w])[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.")
FIO_INITIALS_FIRST_RE = re.compile(r"(?<![\w.])[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]{2,}")
FIO_FULL_RE = re.compile(
    r"(?<![\w])[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:вич|вна|ична|чна)(?![\w])"
)
PII_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("номер карты", re.compile(r"(?<!\d)(?:\d{4}[ -]){3}\d{4}(?!\d)|(?<!\d)\d{16}(?!\d)")),
    ("номер счёта", re.compile(r"(?<!\d)\d{20}(?!\d)")),
    ("паспортные данные", re.compile(r"(?<!\d)\d{2}\s?\d{2}\s?№?\s?\d{6}(?!\d)")),
    ("телефон", re.compile(r"(?:\+7|(?<!\d)8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")),
    ("СНИЛС", re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}[ -]\d{2}(?!\d)")),
    ("e-mail", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("ФИО", FIO_FULL_RE),
)
OVERPROMISE_RE = re.compile(
    r"полност\w*\s+подготов|готов\w*\s+к\s+самостоятельн\w*\s+(?:профессиональн\w*\s+)?работ|трудоустро"
    r"|гарантир\w*\s+(?:трудоустр|работ|результат|успех)|станет\w*\s+(?:профессионал|специалист|эксперт)"
    r"|стать\s+(?:профессионал|эксперт)|с\s+нуля\s+до\s+(?:профи|профессионал|эксперт|senior|middle)"
    r"|в\s+совершенстве|достаточн\w*\s+для\s+(?:начала\s+)?(?:самостоятельн\w*\s+)?(?:профессиональн\w*\s+)?работ"
    r"|\bготов\w*\s+к\s+работе|смож\w*\s+(?:сразу\s+)?работать\s+(?:в\s+(?:эт\w+\s+)?сфер|специалист)"
    # «для подготовки к работе в сфере кибербезопасности» за 72 ч (оценка 21.10, п. 4.8)
    r"|подготовк\w*\s+к\s+(?:самостоятельн\w*\s+|профессиональн\w*\s+)*(?:работе|деятельности)\s+"
    r"(?:в\s+(?:сфер|област|отрасл|професси)|по\s+специальност)",
    re.I,
)
EXPECTATION_RE = re.compile(
    r"трудоустро|пойти\s+работать|работать\s+в\s+(?:эт\w+\s+)?сфер|найти\s+работ|получить\s+работ"
    r"|устроиться\s+на\s+работ|смен\w*\s+професси|войти\s+в\s+професси|начать\s+карьер",
    re.I,
)
PROGRAMMING_RE = re.compile(
    r"программирован|(?<![\w])Python(?![\w])|(?<![\w])скрипт|(?<![\w])API(?![\w])|программн\w*\s+код"
    r"|(?<![\w])код(?:а|ом|у|е)?(?![\w])|написа\w*\s+(?:программ|код)",
    re.I,
)
NEGATION_RE = re.compile(
    r"(?<![\w])не\s+(?:требу|нужн|обязател|предполага|понадоб)|без\s+(?:\w+\s+){0,2}программир", re.I
)
BASIC_GROUPS: dict[str, re.Pattern[str]] = {
    "переменные": re.compile(r"переменн", re.I),
    "условия": re.compile(
        r"услови\w*\s+(?:оператор|конструкц)|условн\w+\s+(?:оператор|конструкц|переход)|ветвлен"
        r"|(?<![\w])if(?![\w])|(?<![\w])услови[ея](?![\w])",
        re.I,
    ),
    "циклы": re.compile(r"(?<![\w])цикл\w*|(?<![\w])(?:for|while)(?![\w])", re.I),
}
ADVANCED_RE = re.compile(
    r"(?<![\w])класс(?:ы|ов|ам|ами|ах|а|е|ом)?(?![\w])|(?<![\w])ООП(?![\w])|объектно-ориент"
    r"|(?<![\w])API(?![\w])|(?<![\w])HTTP(?![\w])|(?<![\w])requests(?![\w])|наследовани",
    re.I,
)
LIFECYCLE_RE = re.compile(r"жизненн\w*\s+цикл\w*", re.I)
GRADE_RE = re.compile(r"(\d{1,2})(?:\s*(?:[–—-]|и|или)\s*(\d{1,2}))?\s*(?:-?(?:х|го|м|й))?\s*класс", re.I)
ADULT_RE = re.compile(r"от\s*18\s*лет|старше\s*18|совершеннолет|(?<![\w])взросл", re.I)
EDU_RE = re.compile(r"средн\w*\s+профессиональн|высш\w*\s+образован|(?<![\w])(?:СПО|ВО)(?![\w])", re.I)
LR_RE = re.compile(r"(?<![А-ЯЁA-Zа-яё])ЛР(?![А-ЯЁA-Zа-яё])")
BROKEN_WORD_RE = re.compile(r"(?<![\w])[а-яё]+\d+[а-яё]+(?![\w])", re.I)
PLACEHOLDER_RE = re.compile(r"\[\s*заполнить", re.I)
CONTROL_LABEL_RE = re.compile(r"текущ|контрол|собеседован", re.I)
WP_ABBR_RE = re.compile(r"^\s*(ЛК|ПР|ЛБ|ЛР|СРС|СР)(?![А-ЯЁа-яё])", re.I)
SERVICE_CANDIDATE_RE = re.compile(
    r"(?:нейросет\w*|сервис\w*|чат-?бот\w*|платформ\w*|(?<![\w])ИИ(?![\w])|модел\w*)\s+«?([A-Z][A-Za-z0-9.+-]{2,})»?"
)
URL_RE = re.compile(r"https?://\S+|www\.\S+")
INLINE_CODE_RE = re.compile(r"`[^`]*`")
CODE_TOKEN_RE = re.compile(r"[_/\\()=<>{}\[\]0-9#@.]")
CAMEL_RE = re.compile(r"[a-z][A-Z]")
ENGLISH_STOPWORDS = frozenset(
    "the and of to in is with for on this that are be you your from by as an it or at we can will how what".split()
)
RU_STOPWORDS = frozenset(
    "и или с со в во на по для не без от до из к как что это лет года при об о а также либо их его её".split()
)
INTRO_STOPWORDS = frozenset(
    "основы основ введение вводный вводное вводная блок курс по для базовый базовые начала начальный".split()
)
LOOKALIKES = str.maketrans({"А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H"})


# --------------------------------------------------------------------------------------------------
# Публичные структуры
# --------------------------------------------------------------------------------------------------


@dataclass
class ValidationContext:
    """Внешние данные и пороги проверок. Параметры запроса сюда не попадают — они в kit.request."""

    registry: RegistryView | None = None
    # «skeleton» — проверяется каркас до генерации содержания: проверки лекций, заданий, тестов, литературы
    # и разделов шаблона пропускаются (SKELETON_SKIP), V17 требует только связь компетенций с темами
    stage: Literal["skeleton", "kit"] = "kit"
    reference_texts: Sequence[str] = ()  # тексты образцов и вложений для V42
    copy_threshold: float = copycheck.DEFAULT_THRESHOLD  # доля скопированных шинглов во всём комплекте
    copy_field_ratio: float = 0.5  # доля скопированного в одном поле, после которой поле показывается
    current_year: int = field(default_factory=lambda: date.today().year)
    extra_warnings: list[MethodistWarning] = field(default_factory=list)  # от солвера, календаря, LLM
    carry_over_warnings: bool = True  # переносить expectation_gap/schedule_tight/... из прошлого отчёта
    cyrillic_min_share: float = 0.75  # доля русских букв во всех текстах (код и названия ПО исключены)
    cyrillic_field_min_share: float = 0.5  # доля русских слов в отдельном поле
    language_min_words: int = 6  # поля короче не оцениваются по языку
    domain_task_share: float = 0.7  # доля заданий семинаров из профессиональной среды слушателей
    min_assessments_per_lo: int = 2
    min_interview_per_module: int = 3
    min_slides_per_lecture: int = 3
    min_seminar_criteria: int = 2
    min_fa_criteria: int = 3
    min_question_options: int = 2
    max_unverified_per_section: int = 2
    max_source_age_years: int = 15
    topic_similarity_threshold: float = 0.7  # триграммное сходство названий тем
    weeks_tolerance: float = 0.15  # допуск к желаемому сроку (мягкое ограничение)
    distance_share_tolerance: float = 0.1
    foreign_titles: Sequence[str] = ("Точка сборки",)  # чужие названия из образцов
    max_findings_per_check: int = 30


@dataclass
class Finding:
    """Находка проверки до присвоения ID (validate превращает её в Issue)."""

    severity: Severity
    message: str
    target_ids: Sequence[str] = ()
    detail: str = ""
    fix_by: FixBy = FixBy.HUMAN
    refs: Sequence[str] | None = None  # None — пункты чек-листа самой проверки


CheckFn = Callable[[Kit, ValidationContext], list[Finding]]


@dataclass(frozen=True)
class CheckSpec:
    id: str
    title: str
    fn: CheckFn
    checklist_refs: tuple[str, ...] = ()


# --------------------------------------------------------------------------------------------------
# Мелкие помощники
# --------------------------------------------------------------------------------------------------


def _eq(a: float, b: float) -> bool:
    return abs(a - b) < EPS


def _fmt(value: float | None) -> str:
    """Число для методиста: 2 → «2», 0.5 → «0,5»."""
    if value is None:
        return "—"
    if not math.isfinite(value):
        return str(value)
    if _eq(value, round(value)):
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _num(text: str) -> float:
    return float(text.replace(",", "."))


def _on_step(value: float) -> bool:
    return math.isfinite(value) and abs(value * 2 - round(value * 2)) < EPS


def _filled(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_filled(v) for v in value)
    return bool(value)


def _short(text: str, limit: int = 70) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _family(kit: Kit) -> str:
    return "dop" if kit.program.type == ProgramType.DOP else "pk"


def _internship(kit: Kit) -> bool:
    return kit.program.type == ProgramType.PK_INTERNSHIP or kit.request.constraints.all_srs


def _kind_name(kit: Kit, kind: WorkKind) -> str:
    return DOP_KIND_NAMES[kind] if kit.program.type == ProgramType.DOP else KIND_ABBR[kind]


def _compile(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern, re.I)
    except re.error:
        return re.compile(re.escape(pattern), re.I)


def _requirement_patterns(req: TopicRequirement) -> list[re.Pattern[str]]:
    patterns = [_compile(p) for p in req.patterns if p.strip()]
    if not patterns and req.label.strip():
        patterns = [re.compile(re.escape(req.label.strip()), re.I)]
    return patterns


def _stems(text: str) -> list[str]:
    """Основы значимых слов для грубого сравнения формулировок (первые 5 букв)."""
    words = re.findall(r"[A-Za-zА-Яа-яЁё]+", text.lower().replace("ё", "е"))
    return [w[:5] for w in words if len(w) >= 4 and w not in RU_STOPWORDS]


def _expand_abbr(text: str) -> str:
    text = re.sub(r"(?<![\w])СПО(?![\w])", "среднее профессиональное образование", text)
    return re.sub(r"(?<![\w])ВО(?![\w])", "высшее образование", text)


def _iso_to_ru(value: str | None) -> str:
    if not value:
        return ""
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", value)
    return f"{match.group(3)}.{match.group(2)}.{match.group(1)}" if match else value


def _norm_number(value: str) -> str:
    return value.strip().rstrip(".,;:»)").lower().replace("ё", "е").replace(" ", "")


def _list(items: Iterable[str], limit: int = 5) -> str:
    items = list(items)
    shown = ", ".join(items[:limit])
    return shown + (f" и ещё {len(items) - limit}" if len(items) > limit else "")


def _cap(findings: list[Finding], ctx: ValidationContext) -> list[Finding]:
    limit = ctx.max_findings_per_check
    if len(findings) <= limit:
        return findings
    rest = len(findings) - limit
    head = findings[:limit]
    head.append(Finding(INFO, f"И ещё {rest} похожих замечаний этой проверки.", fix_by=head[-1].fix_by))
    return head


class _Names:
    """Подписи объектов для методиста: «тема 1.2 «Интервью…»», «вопрос 3 теста 1»."""

    WORDS = {
        "C": "компетенция",
        "LO": "результат обучения",
        "M": "модуль",
        "T": "тема",
        "L": "лекция",
        "S": "задание",
        "TS": "тест",
        "Q": "вопрос",
        "IQ": "вопрос собеседования",
        "SRC": "источник",
        "D": "день",
    }
    PREFIX_RE = re.compile(r"^(LO|TS|IQ|SRC|C|M|T|L|S|Q|D)\d")

    def __init__(self, kit: Kit) -> None:
        try:
            self.numbers = display_numbers(kit)
        except Exception:  # noqa: BLE001 — подписи не должны ронять проверки
            self.numbers = {}
        self.index = kit.index()
        self.test_of = {q.id: q.test_id for q in kit.questions}

    def __call__(self, obj_id: str, title: bool = True) -> str:
        if obj_id == "FA":
            return "итоговая аттестация"
        if ".K" in obj_id:
            return f"критерий {obj_id}"
        if obj_id.startswith("FA.P"):
            return f"материал портфолио {obj_id}"
        match = self.PREFIX_RE.match(obj_id)
        prefix = match.group(1) if match else ""
        word = self.WORDS.get(prefix, "объект")
        number = self.numbers.get(obj_id) or obj_id
        if prefix == "Q":
            test_number = self.numbers.get(self.test_of.get(obj_id, ""))
            return f"вопрос {number} теста {test_number}" if test_number else f"вопрос {obj_id}"
        if prefix == "LO":
            return f"результат обучения {number}"
        label = f"{word} {number}"
        obj = self.index.get(obj_id)
        name = getattr(obj, "title", "") if title else ""
        if name:
            label += f" «{_short(name, 60)}»"
        return label


@dataclass(frozen=True)
class _Text:
    target: str  # ID объекта или "" для полей программы
    where: str  # где находится — для сообщения методисту
    text: str
    scope: str  # program | content | assessment | calendar | software | term | source


TEXT_FIELDS: tuple[tuple[str, str], ...] = (
    ("annotation", "аннотация"),
    ("relevance", "актуальность"),
    ("form_description", "описание формы обучения"),
    ("audience_requirements", "требования к слушателям"),
    ("mto", "материально-техническое обеспечение"),
    ("software", "программное обеспечение"),
    ("org_process", "организация образовательного процесса"),
    ("staffing_requirements", "кадровое обеспечение"),
    ("methodical", "методические материалы"),
    ("satisfaction", "анализ удовлетворённости"),
)


def _collect_texts(kit: Kit, names: _Names | None = None) -> list[_Text]:
    """Все текстовые поля комплекта с указанием, где они находятся."""
    names = names or _Names(kit)
    out: list[_Text] = []

    def add(target: str, where: str, value: object, scope: str) -> None:
        if isinstance(value, str):
            if value.strip():
                out.append(_Text(target, where, value, scope))
        elif isinstance(value, (list, tuple)):
            for item in value:
                add(target, where, item, scope)

    program = kit.program
    add("", "название программы", program.title, "program")
    add("", "цель программы", program.goal, "program")
    add("", "задачи программы", program.tasks, "program")
    add("", "направленность", program.direction or "", "program")
    add("", "уровень программы", program.level or "", "program")
    audience = program.audience
    add("", "категория слушателей", audience.category, "program")
    add("", "возраст / классы слушателей", audience.age_or_grade or "", "program")
    add("", "входной уровень слушателей", audience.entry_level or "", "program")
    add("", "требования к слушателям (паспорт)", audience.requirements or "", "program")
    for name, where in TEXT_FIELDS:
        add("", where, getattr(kit.texts, name), "software" if name == "software" else "program")
    for comp in kit.competencies:
        add(comp.id, f"{names(comp.id)}, вид деятельности", comp.activity, "program")
        add(comp.id, names(comp.id, title=False), comp.title, "program")
    for outcome in kit.outcomes:
        add(outcome.id, names(outcome.id), outcome.text, "program")
    for module in kit.modules:
        add(module.id, names(module.id, title=False), module.title, "program")
        add(module.id, f"{names(module.id)}, форма контроля", module.control_form or "", "program")
    for topic in kit.topics:
        add(topic.id, names(topic.id, title=False), topic.title, "program")
        for kind, items in topic.content.items():
            add(topic.id, f"{names(topic.id)}, содержание ({_kind_name(kit, kind)})", items, "content")
        add(topic.id, f"{names(topic.id)}, форма контроля", topic.control_form or "", "program")
    for lecture in kit.lectures:
        where = names(lecture.id)
        add(lecture.id, where, lecture.title, "content")
        add(lecture.id, f"{where}, цель", lecture.goal, "content")
        for item in lecture.plan:
            add(lecture.id, f"{where}, план", item.title, "content")
            add(lecture.id, f"{where}, план", item.theses, "content")
        for entry in lecture.glossary:
            add(lecture.id, f"{where}, глоссарий", entry.term, "term")
            add(lecture.id, f"{where}, глоссарий", entry.definition, "content")
        for slide in lecture.slides or []:
            add(lecture.id, f"{where}, слайды", slide.title, "content")
            add(lecture.id, f"{where}, слайды", slide.bullets, "content")
            add(lecture.id, f"{where}, заметки к слайдам", slide.notes, "content")
    for seminar in kit.seminars:
        where = names(seminar.id)
        add(seminar.id, where, seminar.title, "assessment")
        add(seminar.id, f"{where}, ситуация", seminar.situation, "assessment")
        add(seminar.id, f"{where}, задание", seminar.task, "assessment")
        add(seminar.id, f"{where}, шаги", seminar.steps, "assessment")
        add(seminar.id, f"{where}, результат", seminar.deliverable, "assessment")
        for crit in seminar.criteria:
            add(seminar.id, f"{where}, критерии", crit.text, "assessment")
    for test in kit.tests:
        add(test.id, names(test.id), test.title, "assessment")
    for question in kit.questions:
        where = names(question.id)
        add(question.id, where, question.stem, "assessment")
        add(question.id, f"{where}, варианты", question.options, "assessment")
        add(question.id, f"{where}, пояснение", question.explanation, "assessment")
    for iq in kit.interview_questions:
        where = names(iq.id)
        add(iq.id, where, iq.text, "assessment")
        add(iq.id, f"{where}, ожидаемый ответ", iq.answer_points, "assessment")
        add(iq.id, f"{where}, критерии", iq.criteria, "assessment")
    fa = kit.final_attestation
    if fa is not None:
        where = "итоговая аттестация"
        add("FA", where, fa.form_text, "assessment")
        add("FA", f"{where}, требования", fa.requirements, "assessment")
        for item in fa.portfolio_items:
            add("FA", f"{where}, портфолио", item.title, "assessment")
            add("FA", f"{where}, портфолио", item.requirements, "assessment")
        if fa.example_task is not None:
            task = fa.example_task
            add(
                "FA",
                f"{where}, пример задания",
                [task.title, task.environment, task.description, task.deliverable],
                "assessment",
            )
        add("FA", f"{where}, требования к данным", fa.data_requirements, "assessment")
        add("FA", f"{where}, ожидаемый результат", fa.expected_result or "", "assessment")
        add("FA", f"{where}, требования к отчёту", fa.report_requirements, "assessment")
        for crit in fa.criteria:
            add("FA", f"{where}, критерии", crit.text, "assessment")
        add("FA", f"{where}, правило «зачтено»", fa.pass_rule, "assessment")
        add("FA", f"{where}, процедура", fa.procedure, "assessment")
    if kit.calendar is not None:
        add("", "календарный график, примечания", kit.calendar.notes, "calendar")
    for source in kit.sources:
        where = names(source.id)
        add(source.id, where, [source.title, source.publisher or "", source.url or ""], "source")
        add(source.id, where, source.authors, "source")
    return out


def _seminar_text(seminar: Seminar) -> str:
    return " ".join([seminar.title, seminar.situation, seminar.task, *seminar.steps, seminar.deliverable])


def _fa_text(kit: Kit) -> str:
    """Текст итоговой аттестации: требования, элементы портфолио, пример задания, ожидаемый результат."""
    fa = kit.final_attestation
    if fa is None:
        return ""
    example = fa.example_task
    parts = [fa.form_text, *fa.requirements, fa.expected_result or "", *fa.data_requirements]
    parts += [f"{item.title} {' '.join(item.requirements)}" for item in fa.portfolio_items]
    if example is not None:
        parts += [example.title, example.description, example.deliverable]
    return " ".join(p for p in parts if p)


def _regular_modules(kit: Kit) -> list:
    return [m for m in kit.modules_sorted() if m.kind != ModuleKind.INTRO]


def _module_numbers_of(kit: Kit) -> dict[str, str]:
    return {t.id: t.module_id for t in kit.topics}


# --------------------------------------------------------------------------------------------------
# Сводки отчёта
# --------------------------------------------------------------------------------------------------


def hours_summary(kit: Kit) -> HoursSummary:
    """Часы по уровням: модуль → его темы, итоговая аттестация, итог; контактная работа, СРС, ДОТ."""
    numbers = display_numbers(kit)
    lines: list[HoursLine] = []
    for module in kit.modules_sorted():
        row = kit.module_hours(module.id)
        lines.append(
            HoursLine(
                level="module",
                id=module.id,
                number=numbers.get(module.id, ""),
                title=module.title,
                total=row.total,
                hours=row,
            )
        )
        lines += [
            HoursLine(
                level="topic",
                id=topic.id,
                number=numbers.get(topic.id, ""),
                title=topic.title,
                total=topic.hours.total,
                hours=topic.hours,
            )
            for topic in kit.topics_of(module.id)
        ]
    fa = kit.fa_hours()
    topics = kit.program_hours()
    lines.append(HoursLine(level="final_attestation", id="FA", title="Итоговая аттестация", total=fa))
    lines.append(HoursLine(level="total", id="TOTAL", title="Итого", total=kit.total_hours(), hours=topics))
    return HoursSummary(
        declared=kit.program.total_hours,
        total=kit.total_hours(),
        topics=topics.total,
        final_attestation=fa,
        lk=topics.lk,
        pr=topics.pr,
        lb=topics.lb,
        srs=topics.srs,
        contact=topics.contact,
        distance=topics.distance,
        in_person=topics.in_person,
        lines=lines,
    )


def _assessments_by_lo(kit: Kit) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)

    def add(lo_ids: Iterable[str], obj_id: str) -> None:
        for lo in lo_ids:
            if obj_id not in result[lo]:
                result[lo].append(obj_id)

    for question in kit.questions:
        add(question.lo_ids, question.id)
    for seminar in kit.seminars:
        add(seminar.lo_ids, seminar.id)
    for iq in kit.interview_questions:
        add(iq.lo_ids, iq.id)
    # аттестация без критериев ничего не оценивает (прогон 22.10: пустая ИА закрывала все РО в матрице покрытия)
    if kit.final_attestation is not None and kit.final_attestation.criteria:
        add(kit.final_attestation.lo_ids, "FA")
    return result


def coverage_matrix(kit: Kit, min_assessments: int = 2) -> list[LOCoverage]:
    """РО × темы × оценочные средства (вопросы, задания, вопросы собеседования, ИА) со статусом строки."""
    assessments = _assessments_by_lo(kit)
    result = []
    for outcome in kit.outcomes:
        topics = [t.id for t in kit.topics if outcome.id in t.lo_ids]
        found = assessments.get(outcome.id, [])
        if not topics:
            status = "no_topic"
        elif not found:
            status = "unassessed"
        elif len(found) < min_assessments:
            status = "single"
        else:
            status = "ok"
        result.append(
            LOCoverage(
                lo_id=outcome.id,
                text=outcome.text,
                kind=outcome.kind,
                topic_ids=topics,
                assessment_ids=found,
                status=status,
            )
        )
    return result


def source_summary(kit: Kit) -> SourceSummary:
    """Подтверждено (реестр, каталог, онлайн-сверка) / не подтверждено / отброшено при проверке."""
    counts = Counter(s.verification for s in kit.sources)
    by_section: dict[str, dict[str, int]] = {}
    for source in kit.sources:
        name = mw.SECTION_NAMES.get(source.section, source.section.value)
        entry = by_section.setdefault(name, {"confirmed": 0, "unverified": 0})
        entry["unverified" if source.unverified else "confirmed"] += 1
    unverified = [s.id for s in kit.sources if s.unverified]
    checks = kit.meta.literature_checks
    return SourceSummary(
        total=len(kit.sources),
        confirmed=len(kit.sources) - len(unverified),
        unverified=len(unverified),
        rejected=int(checks.get("rejected", 0)),
        dropped=int(checks.get("dropped", 0)),
        by_verification={v.value: counts.get(v, 0) for v in Verification},
        by_section=by_section,
        unverified_ids=unverified,
    )


SectionRule = tuple[Callable[[Kit], bool], FixBy]


def _sources_in(kit: Kit, section: SourceSection) -> bool:
    return any(s.section == section for s in kit.sources)


def _has_content(kit: Kit) -> bool:
    return bool(kit.topics) and all(any(_filled(items) for items in t.content.values()) for t in kit.topics)


def _has_calendar(kit: Kit) -> bool:
    return kit.calendar is not None and bool(kit.calendar.days)


PK_SECTIONS: dict[str, SectionRule] = {
    "1.1. Аннотация программы": (lambda k: _filled(k.texts.annotation), FixBy.LLM),
    "1.2. Цель реализации программы": (lambda k: _filled(k.program.goal), FixBy.LLM),
    "1.3. Планируемые результаты обучения": (lambda k: bool(k.competencies) and bool(k.outcomes), FixBy.LLM),
    "1.4. Программа разработана на основе": (
        lambda k: bool(k.program.norm_ids) and bool(k.program.profstandards),
        FixBy.CODE,
    ),
    "1.5. Перечень профессиональных компетенций": (
        lambda k: any(c.tf_refs for c in k.competencies),
        FixBy.LLM,
    ),
    "1.6. Требования к слушателям": (
        lambda k: _filled(k.texts.audience_requirements) or _filled(k.program.audience.requirements),
        FixBy.LLM,
    ),
    "1.7. Форма обучения": (lambda k: _filled(k.texts.form_description), FixBy.LLM),
    "1.8. Объём программы": (lambda k: k.program.total_hours > 0 and bool(k.topics), FixBy.CODE),
    "1.9. Выдаваемый документ": (lambda k: True, FixBy.CODE),
    "2.1. Учебный план": (lambda k: bool(k.modules) and bool(k.topics), FixBy.LLM),
    "2.2. Учебно-тематический план": (lambda k: any(t.hours.total > 0 for t in k.topics), FixBy.CODE),
    "2.3. Календарный учебный график": (_has_calendar, FixBy.CODE),
    "2.4. Рабочая программа": (_has_content, FixBy.LLM),
    "2.5.1. Формы аттестации": (
        lambda k: (
            k.final_attestation is not None
            and (
                any(_filled(m.control_form) for m in k.modules)
                or any(_filled(t.control_form) for t in k.topics)
                or bool(k.tests)
                or bool(k.interview_questions)
            )
        ),
        FixBy.LLM,
    ),
    "2.5.2. Оценочные материалы": (
        lambda k: bool(k.questions) or bool(k.interview_questions) or bool(k.seminars),
        FixBy.LLM,
    ),
    "2.5.3. Анализ удовлетворённости слушателей": (lambda k: _filled(k.texts.satisfaction), FixBy.LLM),
    "3.1. Материально-техническое обеспечение": (lambda k: _filled(k.texts.mto), FixBy.LLM),
    "3.2.1. Нормативно-правовые документы": (
        lambda k: bool(k.program.norm_ids) or _sources_in(k, SourceSection.NORMATIVE),
        FixBy.CODE,
    ),
    "3.2.2. Основная литература": (lambda k: _sources_in(k, SourceSection.MAIN), FixBy.CODE),
    "3.2.3. Дополнительная литература": (lambda k: _sources_in(k, SourceSection.ADDITIONAL), FixBy.CODE),
    "3.2.4. Интернет-ресурсы": (lambda k: _sources_in(k, SourceSection.INTERNET), FixBy.CODE),
    "3.2.5. Программное обеспечение и информационные системы": (
        lambda k: _filled(k.texts.software) or _sources_in(k, SourceSection.SOFTWARE),
        FixBy.LLM,
    ),
    "3.3. Организация образовательного процесса": (lambda k: _filled(k.texts.org_process), FixBy.LLM),
    "3.4. Кадровое обеспечение": (lambda k: _filled(k.texts.staffing_requirements), FixBy.LLM),
    "Приложение 1. Оценочные материалы текущего контроля": (
        lambda k: bool(k.questions) or bool(k.interview_questions) or bool(k.seminars),
        FixBy.LLM,
    ),
    "Приложение 2. Оценочные материалы итоговой аттестации": (
        lambda k: k.final_attestation is not None and bool(k.final_attestation.criteria),
        FixBy.LLM,
    ),
}

DOP_SECTIONS: dict[str, SectionRule] = {
    "Пояснительная записка": (
        lambda k: (
            _filled(k.texts.form_description) and _filled(k.program.direction) and _filled(k.program.level)
        ),
        FixBy.LLM,
    ),
    "Нормативно-правовые основания": (lambda k: bool(k.program.norm_ids), FixBy.CODE),
    "1.1. Актуальность программы": (lambda k: _filled(k.texts.relevance), FixBy.LLM),
    "1.2. Категории обучающихся": (
        lambda k: _filled(k.texts.audience_requirements) or _filled(k.program.audience.category),
        FixBy.LLM,
    ),
    "1.3. Цель и задачи программы": (
        lambda k: _filled(k.program.goal) and _filled(k.program.tasks),
        FixBy.LLM,
    ),
    "1.4. Сроки реализации": (
        lambda k: k.program.total_hours > 0 and k.calendar is not None and k.calendar.weeks > 0,
        FixBy.CODE,
    ),
    "1.5. Планируемые результаты обучения": (lambda k: bool(k.outcomes), FixBy.LLM),
    "1.6. Календарный учебный график": (_has_calendar, FixBy.CODE),
    "2.1. Учебный план": (lambda k: bool(k.modules) and bool(k.topics), FixBy.LLM),
    "2.2. Учебно-тематический план": (_has_content, FixBy.LLM),
    "2.3. Оценочные материалы": (
        lambda k: (
            k.final_attestation is not None
            and bool(k.final_attestation.criteria)
            and (bool(k.questions) or bool(k.seminars))
        ),
        FixBy.LLM,
    ),
    "2.4. Методические материалы": (lambda k: _filled(k.texts.methodical), FixBy.LLM),
    "3.1. Материально-технические условия": (lambda k: _filled(k.texts.mto), FixBy.LLM),
    "3.2. Учебно-методическое и информационное обеспечение": (lambda k: _filled(k.texts.software), FixBy.LLM),
    "3.3.1. Основная литература": (lambda k: _sources_in(k, SourceSection.MAIN), FixBy.CODE),
    "3.3.2. Дополнительная литература": (lambda k: _sources_in(k, SourceSection.ADDITIONAL), FixBy.CODE),
    "3.3.3. Интернет-ресурсы": (lambda k: _sources_in(k, SourceSection.INTERNET), FixBy.CODE),
    "3.4. Кадровое обеспечение": (lambda k: bool(k.modules), FixBy.CODE),
}


def _section_rules(kit: Kit) -> dict[str, SectionRule]:
    return DOP_SECTIONS if kit.program.type == ProgramType.DOP else PK_SECTIONS


def section_status(kit: Kit) -> dict[str, bool]:
    """Раздел шаблона → заполнен (по полям Kit; заголовки в .docx проверяет V31)."""
    return {name: bool(rule(kit)) for name, (rule, _) in _section_rules(kit).items()}


# --------------------------------------------------------------------------------------------------
# V01–V11: часы, объём, календарь
# --------------------------------------------------------------------------------------------------


def check_v01(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    """Сумма строки. На Kit суммы вычисляются; нарушить можно только состав колонок (ЛБ в ДОП)."""
    if kit.program.type != ProgramType.DOP:
        return []
    names = _Names(kit)
    bad = [t for t in kit.topics if t.hours.lb > EPS]
    if not bad:
        return []
    listed = _list(f"{names(t.id)} — {_fmt(t.hours.lb)} ч" for t in bad)
    return [
        Finding(
            ERROR,
            "В учебном плане общеразвивающей программы нет колонки «ЛБ», поэтому строка «Всего аудиторных» "
            f"не сойдётся с «Лекции + Практические». Лабораторные часы указаны у тем: {listed}.",
            [t.id for t in bad],
            "dop: hours.lb > 0",
            FixBy.CODE,
        )
    ]


def check_v02(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    """Свёртка: каждая тема входит в модуль, у каждого модуля есть темы."""
    names = _Names(kit)
    out: list[Finding] = []
    module_ids = {m.id for m in kit.modules}
    orphans = [t for t in kit.topics if t.module_id not in module_ids]
    if orphans:
        lost = HoursRow.sum([t.hours for t in orphans]).total
        by_modules = sum(kit.module_hours(m).total for m in module_ids)
        out.append(
            Finding(
                ERROR,
                f"Темы {_list(names(t.id) for t in orphans)} не входят ни в один модуль: их {_fmt(lost)} ч "
                f"не попадают в строки модулей, и «Итого» по модулям ({_fmt(by_modules)} ч) меньше суммы тем "
                f"({_fmt(kit.program_hours().total)} ч).",
                [t.id for t in orphans],
                f"orphan topics: {[t.module_id for t in orphans]}",
                FixBy.CODE,
            )
        )
    for module in kit.modules_sorted():
        if not kit.topics_of(module.id):
            out.append(
                Finding(
                    ERROR,
                    f"В {names(module.id)} нет ни одной темы — строка модуля в учебном плане будет пустой.",
                    [module.id],
                    "module without topics",
                    FixBy.LLM,
                )
            )
    return out


def check_v03(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    out: list[Finding] = []
    declared = kit.program.total_hours
    actual = kit.total_hours()
    if declared <= EPS:
        out.append(Finding(ERROR, "Не задан объём программы в часах.", detail="program.total_hours == 0"))
    elif not _eq(declared, actual):
        out.append(
            Finding(
                ERROR,
                f"Итоговая трудоёмкость {_fmt(actual)} ч (темы {_fmt(kit.program_hours().total)} ч + итоговая "
                f"аттестация {_fmt(kit.fa_hours())} ч) не равна заявленному объёму {_fmt(declared)} ч.",
                detail=f"total_hours()={actual}, program.total_hours={declared}",
                fix_by=FixBy.CODE,
            )
        )
    requested = kit.request.total_hours
    if requested is not None and not _eq(requested, declared):
        out.append(
            Finding(
                ERROR,
                f"Объём программы {_fmt(declared)} ч не совпадает с объёмом из запроса — {_fmt(requested)} ч.",
                detail=f"request.total_hours={requested}",
                fix_by=FixBy.CODE,
            )
        )
    return out


def check_v04(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    """РП = УТП: виды работы в содержании темы совпадают с видами, у которых есть часы."""
    if not any(_filled(items) for t in kit.topics for items in t.content.values()):
        return []  # содержание ещё не генерировалось (до утверждения каркаса)
    names = _Names(kit)
    out: list[Finding] = []
    for topic in kit.topics:
        extra = [k for k in KINDS if _filled(topic.content.get(k, [])) and topic.hours.get(k) <= EPS]
        missing = [k for k in KINDS if topic.hours.get(k) > EPS and not _filled(topic.content.get(k, []))]
        if extra:
            kinds = ", ".join(_kind_name(kit, k) for k in extra)
            out.append(
                Finding(
                    ERROR,
                    f"{names(topic.id)}: в рабочей программе есть содержание по видам работы «{kinds}», "
                    "а в учебно-тематическом плане у них 0 ч.",
                    [topic.id],
                    f"content without hours: {[k.value for k in extra]}",
                    FixBy.LLM,
                )
            )
        if missing:
            kinds = ", ".join(f"{_kind_name(kit, k)} {_fmt(topic.hours.get(k))} ч" for k in missing)
            out.append(
                Finding(
                    WARNING,
                    f"{names(topic.id)}: в учебно-тематическом плане есть {kinds}, а в рабочей программе нет "
                    "содержания для этого вида работы.",
                    [topic.id],
                    f"hours without content: {[k.value for k in missing]}",
                    FixBy.LLM,
                )
            )
    return _cap(out, ctx)


def _hour_mentions(text: str) -> list[tuple[str, WorkKind, float]]:
    found: list[tuple[str, WorkKind, float]] = []
    for match in HOURS_LABEL_RE.finditer(text):
        found.append((match.group(0), ABBR_KIND[match.group(1).upper()], _num(match.group(2))))
    for regex, kind in DOP_LABEL_RES:
        for match in regex.finditer(text):
            found.append((match.group(0), kind, _num(match.group(1))))
    return found


def check_v05(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    """Числа часов в тексте («ПР (9 часов)») совпадают с колонкой УТП."""
    names = _Names(kit)
    seminars_by_topic: dict[str, list[Seminar]] = defaultdict(list)
    for seminar in kit.seminars:
        seminars_by_topic[seminar.topic_id].append(seminar)
    out: list[Finding] = []
    for topic in kit.topics:
        texts = [topic.title, topic.control_form or ""]
        texts += [item for items in topic.content.values() for item in items]
        texts += [_seminar_text(s) for s in seminars_by_topic.get(topic.id, [])]
        for text in texts:
            for fragment, kind, value in _hour_mentions(text):
                actual = topic.hours.get(kind)
                if not _eq(value, actual):
                    out.append(
                        Finding(
                            WARNING,
                            f"{names(topic.id)}: в тексте «{_short(fragment, 50)}», а в учебно-тематическом "
                            f"плане {_kind_name(kit, kind)} = {_fmt(actual)} ч.",
                            [topic.id],
                            f"mention {value} vs {kind.value}={actual}",
                            FixBy.CODE,
                        )
                    )
    out += _free_hour_mentions(kit)
    return _cap(out, ctx)


# Упоминание часов: «36 ч», «2 часа», «72 ак. ч.», «16 акад. часов», «8 а.ч.», «18-часовой», «72-х часов».
# Общий шаблон для V05 и для очистки ответов модели (app/pipeline/content_apply.py). Нормы вида «4 часа в день»
# не исключаются: лимиты из запроса (hours_per_day_max, session_hours) и суммы дней есть в _plan_hour_numbers.
HOURS_PATTERN = (
    r"(\d+(?:[.,]\d+)?)(?:\s*-\s*(?:х|ти|ми))?\s*-?\s*"
    r"(?:(?:ак(?:ад\w*)?|а)\.?\s*)?"
    r"(?:ч\b\.?|час(?:а|ах|ам|ами|ом|у|ов(?:ой|ая|ое|ую|ые|ых|ого|ому|ым|ыми)?)?\b)"
)
FREE_HOURS_RE = re.compile(r"(?<![\w.,])" + HOURS_PATTERN, re.I)
HOURS_FREE_SCOPES = ("source", "term", "calendar")  # библиография, термины и примечания календаря (пишет код)


def _plan_hour_numbers(kit: Kit) -> set[float]:
    """Все числа часов, которые есть в учебном плане и паспорте: строки тем и модулей по видам, итоги, ИА, лимиты."""
    rows = (
        [t.hours for t in kit.topics] + [kit.module_hours(m.id) for m in kit.modules] + [kit.program_hours()]
    )
    values = {
        round(v, 1)
        for row in rows
        for v in (row.lk, row.pr, row.lb, row.srs, row.total, row.contact, row.distance)
    }
    values |= {round(kit.fa_hours(), 1), round(kit.total_hours(), 1), round(kit.program.total_hours, 1)}
    schedule = kit.program.schedule
    values |= {round(v, 1) for v in (schedule.hours_per_day_max, schedule.session_hours) if v}
    if kit.calendar is not None:
        values |= {round(sum(i.hours for i in d.items), 1) for d in kit.calendar.days}
    return values


def _free_hour_mentions(kit: Kit) -> list[Finding]:
    """Страховка жёсткого правила 2: число часов в тексте, которого нет ни в одной строке плана, — выдумано."""
    allowed = _plan_hour_numbers(kit)
    out: list[Finding] = []
    for item in _collect_texts(kit):
        if item.scope in HOURS_FREE_SCOPES:
            continue
        for match in FREE_HOURS_RE.finditer(item.text):
            value = round(float(match.group(1).replace(",", ".")), 1)
            if value not in allowed:
                out.append(
                    Finding(
                        ERROR,
                        f"{item.where}: «{_short(match.group(0), 30)}» — такого числа часов нет в учебном плане; часы в "
                        "тексте берутся только из плана.",
                        [item.target] if item.target else [],
                        f"free hours mention {value}",
                        FixBy.CODE,
                        refs=("G1e",),
                    )
                )
    return out


def check_v06(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    """Нет тем без часов; все значения кратны 0,5."""
    names = _Names(kit)
    out: list[Finding] = []
    for topic in kit.topics:
        if topic.hours.total <= EPS:
            out.append(
                Finding(
                    ERROR,
                    f"{names(topic.id)}: у темы нет часов — строка учебного плана будет пустой.",
                    [topic.id],
                    "topic.hours.total == 0",
                    FixBy.CODE,
                    refs=("G1f",),
                )
            )
        bad = [
            (name, value)
            for name, value in (
                *((KIND_ABBR[k], topic.hours.get(k)) for k in KINDS),
                ("ДОТ", topic.hours.distance),
            )
            if not _on_step(value)
        ]
        if bad:
            listed = ", ".join(f"{n} = {_fmt(v)}" for n, v in bad)
            out.append(
                Finding(
                    WARNING,
                    f"{names(topic.id)}: {listed} ч — часы должны быть кратны 0,5.",
                    [topic.id],
                    f"step: {bad}",
                    FixBy.CODE,
                    refs=("G1h",),
                )
            )
    fa = kit.final_attestation
    if fa is not None and not _on_step(fa.hours):
        out.append(
            Finding(
                WARNING,
                f"Часы итоговой аттестации ({_fmt(fa.hours)}) должны быть кратны 0,5.",
                ["FA"],
                "fa.hours step",
                FixBy.CODE,
                refs=("G1h",),
            )
        )
    if not _on_step(kit.program.total_hours):
        out.append(
            Finding(
                WARNING,
                f"Объём программы ({_fmt(kit.program.total_hours)} ч) должен быть кратен 0,5.",
                detail="program.total_hours step",
                fix_by=FixBy.HUMAN,
                refs=("G1h",),
            )
        )
    if kit.calendar is not None:
        bad_days = [d for d in kit.calendar.days if any(not _on_step(i.hours) for i in d.items)]
        if bad_days:
            out.append(
                Finding(
                    WARNING,
                    f"В календарном графике есть часы не кратные 0,5: дни {_list(str(d.n) for d in bad_days)}.",
                    [d.id for d in bad_days],
                    "calendar step",
                    FixBy.CODE,
                    refs=("G1h",),
                )
            )
    return _cap(out, ctx)


def check_v07(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    """Раздел «Объём»: числа в текстах программы совпадают с планом; часы ИА — с запросом."""
    out: list[Finding] = []
    hours = kit.program_hours()
    expected = {
        "contact": hours.contact,
        "srs": hours.srs,
        "fa": kit.fa_hours(),
        "total": kit.program.total_hours,
    }
    labels = {
        "contact": "контактная работа",
        "srs": "самостоятельная работа",
        "fa": "итоговая аттестация",
        "total": "объём программы",
    }
    texts = [t for t in _collect_texts(kit) if t.scope in ("program", "calendar")]
    for item in texts:
        for key, regex in VOLUME_RES:
            for match in regex.finditer(item.text):
                if RATE_TAIL_RE.match(item.text[match.end() :]):
                    continue  # «до 4 часов в день» — режим, а не объём
                value = _num(match.group(1))
                if not _eq(value, expected[key]):
                    out.append(
                        Finding(
                            ERROR,
                            f"{item.where}: «{_short(match.group(0), 60)}», а по учебному плану "
                            f"{labels[key]} — {_fmt(expected[key])} ч.",
                            [item.target] if item.target else [],
                            f"volume {key}: text {value} vs plan {expected[key]}",
                            FixBy.LLM,
                        )
                    )
    requested = kit.request.fa_hours
    if requested is not None and kit.final_attestation is not None and not _eq(requested, kit.fa_hours()):
        out.append(
            Finding(
                ERROR,
                f"На итоговую аттестацию отведено {_fmt(kit.fa_hours())} ч, а в запросе — {_fmt(requested)} ч.",
                ["FA"],
                "request.fa_hours != fa.hours",
                FixBy.CODE,
            )
        )
    return _cap(out, ctx)


def check_v08(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    """Стажировка (или «вся работа самостоятельная»): ЛК = ПР = ЛБ = 0 везде, СРС = объём − ИА."""
    if not _internship(kit):
        return []
    names = _Names(kit)
    out: list[Finding] = []
    bad = [t for t in kit.topics if t.hours.contact > EPS]
    if bad:
        hours = kit.program_hours()
        listed = _list(
            f"{names(t.id)} ({', '.join(f'{KIND_ABBR[k]} {_fmt(t.hours.get(k))}' for k in KINDS[:3] if t.hours.get(k) > EPS)})"
            for t in bad
        )
        out.append(
            Finding(
                ERROR,
                "В стажировке вся работа самостоятельная: ЛК, ПР и ЛБ должны быть равны 0 во всех строках, "
                f"включая «Итого». Аудиторные часы есть у тем: {listed}; в «Итого» ЛК {_fmt(hours.lk)}, "
                f"ПР {_fmt(hours.pr)}, ЛБ {_fmt(hours.lb)}.",
                [t.id for t in bad],
                "internship: contact > 0",
                FixBy.CODE,
                refs=("Q5.2",),
            )
        )
    srs = kit.program_hours().srs
    expected = kit.program.total_hours - kit.fa_hours()
    if not _eq(srs, expected):
        out.append(
            Finding(
                ERROR,
                f"В стажировке самостоятельная работа должна занимать весь объём без итоговой аттестации — "
                f"{_fmt(expected)} ч, а сейчас {_fmt(srs)} ч.",
                detail=f"srs={srs}, expected={expected}",
                fix_by=FixBy.CODE,
                refs=("Q5.1",),
            )
        )
    return out


def check_v09(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    share = kit.request.constraints.min_practice_share
    if not share:
        return []
    hours = kit.program_hours()
    practice = hours.pr + hours.lb
    need = share * kit.program.total_hours
    if practice + EPS >= need:
        return []
    return [
        Finding(
            ERROR,
            f"Практических занятий (ПР + ЛБ) {_fmt(practice)} ч, а по запросу нужно не меньше "
            f"{share * 100:g} % объёма — {_fmt(need)} ч.",
            detail=f"practice={practice}, need={need}",
            fix_by=FixBy.CODE,
        )
    ]


FORM_CLAIM_RE = re.compile(r"дистанционн\w*|\bДОТ\b|электронн\w*\s+обучени\w*|\bЭО\b", re.IGNORECASE)
# «без использования электронного обучения и дистанционных технологий» — отрицание на всё перечисление
# (ложное V10 в прогоне 22.10: до «дистанционных» от «без» больше 40 знаков)
FORM_NEGATION_WINDOW = 80
# «без отрыва от работы», «не менее половины занятий» — не отрицание ЭО и ДОТ (ревью 22.10)
FORM_NEGATION_RE = re.compile(
    r"(?:\bбез\b(?!\s+отрыв)|\bне\b(?!\s+(?:менее|более|только|реже|чаще)))[^.;!?]{0,80}$", re.IGNORECASE
)
# Отрицание после упоминания, в том же предложении: «ЭО и ДОТ не используются» (ложное V10 в прогоне 21.10)
FORM_NEGATION_AFTER_RE = re.compile(
    r"^[^.;!?]{0,80}?(?:\bне\s+(?:использу|применя|предусм|проводя|планиру|допуска|реализу|предполага)\w*"
    r"|\bисключ\w*|\bотсутству\w*)",
    re.IGNORECASE,
)


def _form_text_claims(kit: Kit) -> list[Finding]:
    """Тексты программы обещают ЭО или ДОТ, которых нет в форме обучения (и в учебном плане)."""
    out: list[Finding] = []
    texts = kit.texts
    fields = [
        ("описание формы обучения", texts.form_description),
        ("аннотация", texts.annotation),
        ("организация образовательного процесса", texts.org_process),
        ("пояснительная записка", texts.relevance),
    ]
    for where, text in fields:
        for match in FORM_CLAIM_RE.finditer(text or ""):
            if FORM_NEGATION_RE.search(text[max(0, match.start() - FORM_NEGATION_WINDOW) : match.start()]):
                continue
            if FORM_NEGATION_AFTER_RE.search(text[match.end() :]):
                continue
            out.append(
                Finding(
                    WARNING,
                    f"{where}: «{_short(match.group(0), 30)}» — в форме обучения программы ЭО и ДОТ не предусмотрены; "
                    "текст расходится с учебным планом и нормативной базой.",
                    detail="form text claims eo/dot",
                    fix_by=FixBy.LLM,
                    refs=("F6",),
                )
            )
            break
    return out


CLASSROOM_RE = re.compile(
    r"лекци\w*|учебн\w+\s+аудитори\w*|аудиторн\w+\s+заняти\w*|мастер-класс\w*|теоретическ\w+\s+заняти\w*",
    re.I,
)


def _internship_text_claims(kit: Kit) -> list[Finding]:
    """Стажировка без аудиторных часов (ЛК = ПР = ЛБ = 0), а тексты обещают лекции, аудитории, мастер-классы."""
    if kit.program.type != ProgramType.PK_INTERNSHIP or kit.program_hours().contact > EPS:
        return []
    texts = kit.texts
    fields = [
        ("форма обучения (1.7)", texts.form_description),
        ("организация образовательного процесса (3.3)", texts.org_process),
        ("материально-техническое обеспечение (3.1)", " ".join(texts.mto)),
    ]
    out: list[Finding] = []
    for where, text in fields:
        text = text or ""
        match = next(  # «без аудиторных занятий», «лекции не проводятся» — не обещание, а отрицание
            (
                m
                for m in CLASSROOM_RE.finditer(text)
                if not FORM_NEGATION_RE.search(text[max(0, m.start() - FORM_NEGATION_WINDOW) : m.start()])
                and not re.match(r"\s*не\s", text[m.end() : m.end() + 20])
            ),
            None,
        )
        if match:
            out.append(
                Finding(
                    WARNING,
                    f"{where}: «{_short(match.group(0), 30)}» — у стажировки нет аудиторных занятий (все часы — "
                    "самостоятельная работа на рабочем месте); текст расходится с учебным планом.",
                    detail="internship text mentions classroom work",
                    fix_by=FixBy.LLM,
                    refs=("G2",),
                )
            )
    return out


def check_v10(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    """Разбивка «очно / с ДОТ»: задана, когда есть ДОТ, и сходится в каждой строке."""
    names = _Names(kit)
    form = kit.program.form
    out: list[Finding] = []
    for topic in kit.topics:
        if topic.hours.distance > topic.hours.contact + EPS:
            out.append(
                Finding(
                    ERROR,
                    f"{names(topic.id)}: с ДОТ {_fmt(topic.hours.distance)} ч — больше, чем вся контактная "
                    f"работа темы ({_fmt(topic.hours.contact)} ч).",
                    [topic.id],
                    "distance > contact",
                    FixBy.CODE,
                )
            )
    total = kit.program_hours()
    if form.dot:
        if total.contact > EPS and total.distance <= EPS:
            out.append(
                Finding(
                    ERROR,
                    "Форма обучения с ДОТ, но ни у одной темы не выделены часы с ДОТ — разбивка "
                    "«очно / дистанционно» в учебном плане не видна.",
                    detail="dot=True, sum(distance)=0",
                    fix_by=FixBy.CODE,
                )
            )
        if (
            form.kind == StudyFormKind.OCHNO_ZAOCHNAYA
            and total.contact > EPS
            and total.in_person <= EPS
            and total.distance > EPS
        ):
            out.append(
                Finding(
                    ERROR,
                    "Форма очно-заочная, но вся контактная работа проводится с ДОТ — очной части нет.",
                    detail="ochno_zaochnaya: in_person == 0",
                    fix_by=FixBy.CODE,
                )
            )
        if form.distance_share is not None and total.contact > EPS:
            actual = total.distance / total.contact
            if abs(actual - form.distance_share) > ctx.distance_share_tolerance + EPS:
                out.append(
                    Finding(
                        WARNING,
                        f"Доля занятий с ДОТ {actual:.0%} отличается от запрошенной {form.distance_share:.0%}.",
                        detail=f"distance share {actual} vs {form.distance_share}",
                        fix_by=FixBy.CODE,
                    )
                )
    if not (form.dot or form.eo):
        out += _form_text_claims(kit)
    out += _internship_text_claims(kit)
    if not form.dot and total.distance > EPS:
        out.append(
            Finding(
                ERROR,
                f"В форме обучения ДОТ не предусмотрены, но у тем выделено {_fmt(total.distance)} ч с ДОТ.",
                detail="dot=False, distance > 0",
                fix_by=FixBy.CODE,
            )
        )
    requested = kit.request.form
    if requested is not None:
        if requested.dot and not form.dot:
            out.append(
                Finding(
                    ERROR, "В запросе обучение с ДОТ, а в программе форма без ДОТ.", detail="request.form.dot"
                )
            )
        if requested.kind != form.kind:
            out.append(
                Finding(
                    ERROR,
                    "Форма обучения в программе не совпадает с запросом.",
                    detail=f"{form.kind.value} vs {requested.kind.value}",
                )
            )
    return _cap(out, ctx)


def _weeks_target(kit: Kit) -> int | None:
    return kit.program.schedule.weeks_target or kit.request.weeks


def check_v11(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    """Календарь: суммы, длина занятий, лимит часов в день, занятий в неделю, ИА в конце, срок."""
    calendar = kit.calendar
    if calendar is None:
        return []  # отсутствие графика — V12
    names = _Names(kit)
    days = sorted(calendar.days, key=lambda d: d.n)
    if not days:
        return [
            Finding(
                ERROR, "Календарный учебный график пуст.", detail="calendar.days == []", fix_by=FixBy.CODE
            )
        ]
    out: list[Finding] = []
    request, schedule = kit.request, kit.program.schedule

    total = sum(d.hours for d in days)
    if not _eq(total, kit.total_hours()):
        out.append(
            Finding(
                ERROR,
                f"В календарном графике {_fmt(total)} ч, а в учебном плане {_fmt(kit.total_hours())} ч.",
                detail="calendar total != plan total",
                fix_by=FixBy.CODE,
            )
        )
    by_topic: dict[str, float] = defaultdict(float)
    fa_in_calendar = 0.0
    for day in days:
        for item in day.items:
            if item.is_fa:
                fa_in_calendar += item.hours
            elif item.topic_id:
                by_topic[item.topic_id] += item.hours
    mismatched = [t for t in kit.topics if not _eq(by_topic.get(t.id, 0.0), t.hours.total)]
    if mismatched:
        listed = _list(
            f"{names(t.id)} — в графике {_fmt(by_topic.get(t.id, 0.0))} ч, в плане {_fmt(t.hours.total)} ч"
            for t in mismatched
        )
        out.append(
            Finding(
                ERROR,
                f"Часы тем в календарном графике не совпадают с учебным планом: {listed}.",
                [t.id for t in mismatched],
                "calendar per-topic mismatch",
                FixBy.CODE,
            )
        )
    if kit.final_attestation is not None and not _eq(fa_in_calendar, kit.fa_hours()):
        out.append(
            Finding(
                ERROR,
                f"Итоговая аттестация в графике занимает {_fmt(fa_in_calendar)} ч, а в плане — "
                f"{_fmt(kit.fa_hours())} ч.",
                ["FA"],
                "calendar FA hours",
                FixBy.CODE,
            )
        )
    fa_days = [d for d in days if any(i.is_fa for i in d.items)]
    if fa_days and (
        fa_days[-1].n != days[-1].n or len(fa_days) > 1 and fa_days[0].n != days[-len(fa_days)].n
    ):
        out.append(
            Finding(
                ERROR,
                f"Итоговая аттестация должна проходить в последний учебный день (на последнем занятии), а стоит "
                f"в дне {fa_days[0].n} из {days[-1].n}.",
                ["FA"],
                "FA not last",
                FixBy.CODE,
                refs=("Q1.F2",),
            )
        )
    if [d.n for d in days] != list(range(1, len(days) + 1)):
        out.append(
            Finding(
                WARNING,
                "Номера учебных дней в графике идут не подряд.",
                detail=str([d.n for d in days]),
                fix_by=FixBy.CODE,
            )
        )

    max_week = max(d.week for d in days)
    if calendar.weeks != max_week:
        out.append(
            Finding(
                ERROR,
                f"В графике указано недель: {calendar.weeks}, а занятия расписаны на {max_week}.",
                detail="calendar.weeks != max(week)",
                fix_by=FixBy.CODE,
            )
        )
    per_week = Counter(d.week for d in days)
    empty = [w for w in range(1, max_week + 1) if w not in per_week]
    if empty:
        out.append(
            Finding(
                WARNING,
                f"В графике есть недели без занятий: {_list(str(w) for w in empty)}.",
                detail="empty weeks",
                fix_by=FixBy.CODE,
            )
        )

    if calendar.mode == CalendarMode.SESSIONS:
        session_hours = request.session_hours or schedule.session_hours
        per_week_max = request.sessions_per_week or schedule.sessions_per_week or calendar.sessions_per_week
        if session_hours:
            bad = [d for d in days if not _eq(d.hours, session_hours)]
            if bad:
                out.append(
                    Finding(
                        ERROR,
                        f"Занятия должны длиться {_fmt(session_hours)} ч, а занятия "
                        f"{_list(f'№ {d.n} ({_fmt(d.hours)} ч)' for d in bad)} — другой длины.",
                        [d.id for d in bad],
                        "session length",
                        FixBy.CODE,
                        refs=("Q1.3",),
                    )
                )
            sessions = kit.program.total_hours / session_hours
            if _eq(sessions, round(sessions)) and len(days) != round(sessions):
                out.append(
                    Finding(
                        ERROR,
                        f"Нужно {round(sessions)} занятий по {_fmt(session_hours)} ч, а в графике {len(days)}.",
                        detail="session count",
                        fix_by=FixBy.CODE,
                        refs=("Q1.3",),
                    )
                )
        if per_week_max:
            over = [(w, c) for w, c in sorted(per_week.items()) if c > per_week_max]
            if over:
                out.append(
                    Finding(
                        ERROR,
                        f"Допускается не больше {per_week_max} занятий в неделю, а в неделях "
                        f"{_list(f'{w} ({c})' for w, c in over)} — больше.",
                        detail="sessions per week",
                        fix_by=FixBy.CODE,
                        refs=("Q1.3",),
                    )
                )
        srs_days = [d for d in days if any(i.kind == WorkKind.SRS for i in d.items)]
        if srs_days:
            out.append(
                Finding(
                    WARNING,
                    "В программе из занятий фиксированной длины все часы проходят на занятиях, а в график "
                    f"попала самостоятельная работа (занятия {_list(str(d.n) for d in srs_days)}).",
                    [d.id for d in srs_days],
                    "srs in sessions",
                    FixBy.CODE,
                    refs=("Q1.F2",),
                )
            )
    else:
        per_day_max = request.hours_per_day_max or schedule.hours_per_day_max
        if per_day_max:
            over_days = [d for d in days if d.hours > per_day_max + EPS]
            if over_days:
                out.append(
                    Finding(
                        ERROR,
                        f"Нагрузка больше {_fmt(per_day_max)} ч в день: "
                        f"{_list(f'день {d.n} — {_fmt(d.hours)} ч' for d in over_days)}.",
                        [d.id for d in over_days],
                        "hours per day",
                        FixBy.CODE,
                        refs=("Q5.4a",),
                    )
                )
        days_per_week = calendar.days_per_week or schedule.max_days_per_week
        over = [(w, c) for w, c in sorted(per_week.items()) if c > days_per_week]
        if over:
            out.append(
                Finding(
                    ERROR,
                    f"Допускается не больше {days_per_week} учебных дней в неделю, а в неделях "
                    f"{_list(f'{w} ({c})' for w, c in over)} — больше.",
                    detail="days per week",
                    fix_by=FixBy.CODE,
                )
            )

    target = _weeks_target(kit)
    if target:
        if max_week < target * (1 - ctx.weeks_tolerance) - EPS:
            out.append(
                Finding(
                    WARNING,
                    f"График рассчитан на {max_week} нед., а в запросе срок — {target} нед.",
                    detail="calendar shorter than target",
                    fix_by=FixBy.CODE,
                    refs=("Q3.1b", "Q5.4b"),
                )
            )
        elif max_week > target * (1 + ctx.weeks_tolerance) + EPS:
            out.append(
                Finding(
                    INFO,
                    f"График длиннее желаемого срока: {max_week} нед. вместо {target}. Жёсткие ограничения "
                    "(часы в день, длина и число занятий) соблюдены; срок поясняется в отчёте.",
                    detail="calendar longer than target",
                    fix_by=FixBy.CODE,
                    refs=("Q3.1b", "Q5.4b"),
                )
            )
    return _cap(out, ctx)


# --------------------------------------------------------------------------------------------------
# V12–V22: структура, связи, оценочные средства
# --------------------------------------------------------------------------------------------------


def check_v12(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    out: list[Finding] = []
    for name, (rule, fix_by) in _section_rules(kit).items():
        if not rule(kit):
            out.append(
                Finding(
                    ERROR,
                    f"Раздел «{name}» не заполнен.",
                    detail=f"section empty: {name}",
                    fix_by=fix_by,
                    refs=("G2a", "F7")
                    if name in ("Нормативно-правовые основания", "1.6. Календарный учебный график")
                    else ("G2a",),
                )
            )
    for item in _collect_texts(kit):
        if PLACEHOLDER_RE.search(item.text):
            allowed = item.scope == "source"
            out.append(
                Finding(
                    INFO if allowed else WARNING,
                    f"{item.where}: осталась заглушка «[заполнить…]».",
                    [item.target] if item.target else [],
                    "placeholder",
                    FixBy.HUMAN if allowed else FixBy.LLM,
                    refs=("G2b",),
                )
            )
    return _cap(out, ctx)


def check_v13(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    out: list[Finding] = []
    program_type = kit.program.type
    requested = kit.request.program_type
    if requested is not None and requested != program_type:
        out.append(
            Finding(
                ERROR,
                f"Выбран шаблон «{TYPE_NAMES[program_type]}», а в запросе — «{TYPE_NAMES[requested]}».",
                detail=f"{program_type.value} vs {requested.value}",
            )
        )
    if kit.request.constraints.all_srs and program_type == ProgramType.PK:
        out.append(
            Finding(
                WARNING,
                "В запросе вся работа самостоятельная (как в стажировке), а выбран обычный шаблон "
                "повышения квалификации.",
                detail="all_srs with pk",
            )
        )
    texts = [t for t in _collect_texts(kit) if t.scope == "program"]
    if program_type in PK_TYPES:
        wrong_doc = re.compile(r"сертификат\w*\s+об\s+освоени", re.I)
        wrong_type = re.compile(r"общеразвивающ", re.I)
        doc_name = "удостоверение о повышении квалификации"
    else:
        wrong_doc = re.compile(r"удостоверени\w*\s+о\s+повышени", re.I)
        wrong_type = re.compile(
            r"(?:программ\w*|курс\w*)\s+повышени\w+\s+квалификац|профессиональн\w+\s+переподготовк", re.I
        )
        doc_name = "сертификат об освоении программы"
    for item in texts:
        match = wrong_doc.search(item.text)
        if match:
            out.append(
                Finding(
                    WARNING,
                    f"{item.where}: упомянут «{match.group(0)}», а по этому шаблону выдаётся {doc_name}.",
                    [item.target] if item.target else [],
                    "wrong document",
                    FixBy.LLM,
                    refs=("K11",),
                )
            )
    for item in texts:
        if item.where not in (
            "название программы",
            "цель программы",
            "аннотация",
            "актуальность",
            "описание формы обучения",
        ):
            continue
        match = wrong_type.search(item.text)
        if match:
            out.append(
                Finding(
                    WARNING,
                    f"{item.where}: «{match.group(0)}» не соответствует типу программы «{TYPE_NAMES[program_type]}».",
                    [item.target] if item.target else [],
                    "wrong program type wording",
                    FixBy.LLM,
                    refs=("K11",),
                )
            )
    return out


def check_v14(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    """Целостность ID: уникальность, ссылки, неповторное использование удалённых ID."""
    names = _Names(kit)
    out: list[Finding] = []
    counts: Counter[str] = Counter()
    for group in (
        kit.competencies,
        kit.outcomes,
        kit.modules,
        kit.topics,
        kit.lectures,
        kit.seminars,
        kit.tests,
        kit.questions,
        kit.interview_questions,
        kit.sources,
    ):
        counts.update(obj.id for obj in group)
    for seminar in kit.seminars:
        counts.update(c.id for c in seminar.criteria)
    fa = kit.final_attestation
    if fa is not None:
        counts.update(c.id for c in fa.criteria)
        counts.update(p.id for p in fa.portfolio_items)
    if kit.calendar is not None:
        counts.update(d.id for d in kit.calendar.days)
    duplicates = sorted(i for i, c in counts.items() if c > 1)
    if duplicates:
        out.append(
            Finding(
                ERROR,
                f"Повторяются идентификаторы объектов: {_list(duplicates, 10)}.",
                duplicates,
                "duplicate ids",
                FixBy.CODE,
            )
        )
    reused = sorted(set(kit.retired_ids) & set(counts))
    if reused:
        out.append(
            Finding(
                ERROR,
                f"Повторно использованы ID удалённых объектов: {_list(reused, 10)}.",
                reused,
                "retired ids reused",
                FixBy.CODE,
            )
        )
    for seminar in kit.seminars:
        bad = [c.id for c in seminar.criteria if not c.id.startswith(f"{seminar.id}.")]
        if bad:
            out.append(
                Finding(
                    ERROR,
                    f"{names(seminar.id)}: критерии с чужими ID ({_list(bad)}).",
                    [seminar.id],
                    "criterion owner",
                    FixBy.CODE,
                )
            )
    if fa is not None:
        bad = [c.id for c in fa.criteria if not c.id.startswith("FA.")]
        if bad:
            out.append(
                Finding(
                    ERROR,
                    f"Итоговая аттестация: критерии с чужими ID ({_list(bad)}).",
                    ["FA"],
                    "criterion owner",
                    FixBy.CODE,
                )
            )

    module_ids = {m.id for m in kit.modules}
    topic_ids = {t.id for t in kit.topics}
    lo_ids = {o.id for o in kit.outcomes}
    comp_ids = {c.id for c in kit.competencies}
    test_ids = {t.id for t in kit.tests}
    question_by_id = {q.id: q for q in kit.questions}
    topic_module = {t.id: t.module_id for t in kit.topics}
    broken: list[tuple[str, str]] = []  # (владелец, описание)

    def check(owner: str, refs: Iterable[str], valid: set[str], what: str) -> None:
        missing = [r for r in refs if r not in valid]
        if missing:
            broken.append((owner, f"{what}: {_list(missing)}"))

    for outcome in kit.outcomes:
        check(outcome.id, [outcome.competency_id], comp_ids, "компетенция")
    for topic in kit.topics:
        check(topic.id, [topic.module_id], module_ids, "модуль")
        check(topic.id, topic.lo_ids, lo_ids, "результаты обучения")
    for lecture in kit.lectures:
        check(lecture.id, [lecture.module_id], module_ids, "модуль")
        check(lecture.id, lecture.topic_ids, topic_ids, "темы")
        alien = [t for t in lecture.topic_ids if t in topic_module and topic_module[t] != lecture.module_id]
        if alien:
            broken.append((lecture.id, f"темы из другого модуля: {_list(alien)}"))
        for item in lecture.plan:
            check(lecture.id, item.lo_ids, lo_ids, "результаты обучения в плане")
    for seminar in kit.seminars:
        check(seminar.id, [seminar.topic_id], topic_ids, "тема")
        check(seminar.id, seminar.lo_ids, lo_ids, "результаты обучения")
    for test in kit.tests:
        if test.scope == TestScope.MODULE and test.module_id is None:
            broken.append((test.id, "у теста по модулю не указан модуль"))
        if test.module_id is not None:
            check(test.id, [test.module_id], module_ids, "модуль")
        check(test.id, test.question_ids, set(question_by_id), "вопросы")
        alien = [q for q in test.question_ids if q in question_by_id and question_by_id[q].test_id != test.id]
        if alien:
            broken.append((test.id, f"вопросы другого теста: {_list(alien)}"))
    for question in kit.questions:
        check(question.id, [question.test_id], test_ids, "тест")
        check(question.id, [question.topic_id], topic_ids, "тема")
        check(question.id, question.lo_ids, lo_ids, "результаты обучения")
    for iq in kit.interview_questions:
        check(iq.id, [iq.module_id], module_ids, "модуль")
        check(iq.id, iq.lo_ids, lo_ids, "результаты обучения")
    if fa is not None:
        check("FA", fa.lo_ids, lo_ids, "результаты обучения")
    for source in kit.sources:
        check(source.id, source.topic_ids, topic_ids, "темы")
    check("", kit.program.slides_scope.module_ids, module_ids, "модули для слайдов")
    if kit.calendar is not None:
        for day in kit.calendar.days:
            for item in day.items:
                if item.topic_id is None and not item.is_fa:
                    broken.append((day.id, "занятие без темы и без итоговой аттестации"))
                elif item.topic_id is not None:
                    check(day.id, [item.topic_id], topic_ids, "тема")
    for owner, what in broken:
        label = names(owner) if owner else "программа"
        out.append(
            Finding(
                ERROR,
                f"{label}: ссылка на несуществующий объект — {what}.",
                [owner] if owner else [],
                f"broken reference: {owner} {what}",
                FixBy.CODE,
            )
        )
    return _cap(out, ctx)


def check_v15(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    names = _Names(kit)
    covered = {lo for t in kit.topics for lo in t.lo_ids}
    out = [
        Finding(
            ERROR,
            f"{names(o.id)} «{_short(o.text)}» не раскрыт ни в одной теме.",
            [o.id],
            "lo without topic",
            FixBy.LLM,
        )
        for o in kit.outcomes
        if o.id not in covered
    ]
    out += [
        Finding(
            WARNING,
            f"{names(t.id)} не привязана ни к одному результату обучения.",
            [t.id],
            "topic without lo",
            FixBy.LLM,
        )
        for t in kit.topics
        if not t.lo_ids
    ]
    return _cap(out, ctx)


def check_v16(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    names = _Names(kit)
    assessments = _assessments_by_lo(kit)
    out: list[Finding] = []
    for outcome in kit.outcomes:
        found = assessments.get(outcome.id, [])
        if len(found) < ctx.min_assessments_per_lo:
            shown = f" ({_list(found)})" if found else ""
            out.append(
                Finding(
                    ERROR,
                    f"{names(outcome.id)} «{_short(outcome.text)}» проверяется оценочными средствами: "
                    f"{len(found)}{shown}, а нужно не меньше {ctx.min_assessments_per_lo}.",
                    [outcome.id],
                    f"assessments={found}",
                    FixBy.LLM,
                    refs=("G3b",),
                )
            )
    topic_module = {t.id: t.module_id for t in kit.topics}
    for module in _regular_modules(kit):
        module_los = {lo for t in kit.topics_of(module.id) for lo in t.lo_ids}
        if not module_los:
            out.append(
                Finding(
                    ERROR,
                    f"{names(module.id)}: у модуля нет своих результатов обучения (темы не привязаны к РО).",
                    [module.id],
                    "module without lo",
                    FixBy.LLM,
                    refs=("Q5.9a",),
                )
            )
            continue
        local: list[tuple[str, list[str]]] = []
        local += [(q.id, q.lo_ids) for q in kit.questions if topic_module.get(q.topic_id) == module.id]
        local += [(s.id, s.lo_ids) for s in kit.seminars if topic_module.get(s.topic_id) == module.id]
        local += [(iq.id, iq.lo_ids) for iq in kit.interview_questions if iq.module_id == module.id]
        if not local:
            out.append(
                Finding(
                    WARNING,
                    f"{names(module.id)}: нет оценочных средств по темам модуля (вопросов, заданий или вопросов "
                    "собеседования).",
                    [module.id],
                    "module without assessments",
                    FixBy.LLM,
                    refs=("Q5.9a",),
                )
            )
        elif not any(set(los) & module_los for _, los in local):
            out.append(
                Finding(
                    WARNING,
                    f"{names(module.id)}: оценочные средства модуля не проверяют его результаты обучения.",
                    [module.id],
                    "module assessments not linked to module lo",
                    FixBy.LLM,
                    refs=("Q5.9a",),
                )
            )
    return _cap(out, ctx)


def check_v17(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    names = _Names(kit)
    kun = KUN_NAMES[_family(kit)]
    internship = _internship(kit)
    out: list[Finding] = []
    for comp in kit.competencies:
        outcomes = kit.outcomes_of(comp.id)
        if not outcomes:
            out.append(
                Finding(
                    ERROR,
                    f"{names(comp.id)}: нет результатов обучения.",
                    [comp.id],
                    "competency without lo",
                    FixBy.LLM,
                    refs=("K1",),
                )
            )
            continue
        kinds = {o.kind for o in outcomes}
        missing_kinds = [kun[k] for k in OutcomeKind if k not in kinds]
        if missing_kinds:
            out.append(
                Finding(
                    ERROR,
                    f"{names(comp.id)}: не заполнено — {', '.join(missing_kinds)}.",
                    [comp.id],
                    f"missing kinds: {missing_kinds}",
                    FixBy.LLM,
                    refs=("K1",),
                )
            )
        ids = {o.id for o in outcomes}
        has_topic = any(ids & set(t.lo_ids) for t in kit.topics)
        has_question = any(ids & set(q.lo_ids) for q in kit.questions) or any(
            ids & set(iq.lo_ids) for iq in kit.interview_questions
        )
        has_task = any(ids & set(s.lo_ids) for s in kit.seminars)
        if internship and not has_task:
            fa = kit.final_attestation
            has_task = any(ids & set(iq.lo_ids) for iq in kit.interview_questions) or (
                fa is not None and bool(ids & set(fa.lo_ids))
            )
        links = [
            ("темой", has_topic),
            ("тестовым вопросом", has_question),
            ("практическим заданием", has_task),
        ]
        if ctx.stage == "skeleton":  # вопросов и заданий в каркасе ещё нет
            links = links[:1]
        missing = [what for what, ok in links if not ok]
        if missing:
            out.append(
                Finding(
                    ERROR,
                    f"{names(comp.id)}: через результаты обучения не связана с {', '.join(missing)}.",
                    [comp.id],
                    f"missing links: {missing}",
                    FixBy.LLM,
                    refs=("K2",),
                )
            )
    return out


def _test_questions(kit: Kit, test_id: str) -> list[str]:
    test = next((t for t in kit.tests if t.id == test_id), None)
    ordered = list(test.question_ids) if test is not None else []
    ordered += [q.id for q in kit.questions if q.test_id == test_id and q.id not in ordered]
    return ordered


def _duplicate_questions(kit: Kit, names: _Names) -> list[Finding]:
    """Вопросы одного теста проверяют одно и то же: та же формулировка другими словами или те же варианты с тем же
    верным ответом (оценка 21.10: Q1 = Q9, Q9 = Q10). Сборка такие отсекает; здесь — для комплектов, собранных
    иначе (старые сессии, правка вручную)."""
    out: list[Finding] = []
    by_id = {q.id: q for q in kit.questions}
    for test in kit.tests:
        questions = [by_id[qid] for qid in _test_questions(kit, test.id) if qid in by_id]
        for i, first in enumerate(questions):
            twin = next(
                (
                    q
                    for q in questions[i + 1 :]
                    if same_question(first.stem, first.options, first.correct, q.stem, q.options, q.correct)
                ),
                None,
            )
            if twin is not None:
                out.append(
                    Finding(
                        WARNING,
                        f"{names(twin.id)} повторяет {names(first.id)}: тот же вопрос другими словами.",
                        [twin.id, first.id],
                        "duplicate question",
                        FixBy.LLM,
                        refs=("K3",),
                    )
                )
    return out


def check_v18(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    names = _Names(kit)
    out: list[Finding] = []
    for question in kit.questions:
        problems: list[str] = []
        options = question.options
        filled = [o for o in options if o.strip()]
        if len(filled) < ctx.min_question_options:
            problems.append(f"вариантов ответа {len(filled)}, нужно не меньше {ctx.min_question_options}")
        if len(filled) < len(options):
            problems.append("есть пустой вариант ответа")
        # без снятия знаков: «print(5 + 3)» и «print(5) + 3» — разные варианты (код)
        if len({" ".join(o.lower().split()) for o in filled}) < len(filled):
            problems.append("варианты ответа повторяются")
        if not question.correct:
            problems.append("не указан правильный ответ")
        elif any(i < 0 or i >= len(options) for i in question.correct):
            problems.append("ключ указывает на несуществующий вариант")
        elif question.kind == QuestionKind.SINGLE and len(set(question.correct)) != 1:
            problems.append(f"в вопросе с одним ответом отмечено правильных: {len(set(question.correct))}")
        if not question.lo_ids:
            problems.append("нет привязки к результату обучения")
        if not question.stem.strip():
            problems.append("пустая формулировка")
        if problems:
            out.append(
                Finding(
                    ERROR,
                    f"{names(question.id)}: {'; '.join(problems)}.",
                    [question.id],
                    f"question problems: {problems}",
                    FixBy.LLM,
                    refs=("K3",),
                )
            )
    out += _duplicate_questions(kit, names)
    for test in kit.tests:
        count = len(_test_questions(kit, test.id))
        if count == 0:
            out.append(
                Finding(
                    ERROR,
                    f"{names(test.id)}: в тесте нет вопросов.",
                    [test.id],
                    "empty test",
                    FixBy.LLM,
                    refs=("K3",),
                )
            )
        if test.pass_threshold is None:
            out.append(
                Finding(
                    WARNING,
                    f"{names(test.id)}: не задан порог прохождения.",
                    [test.id],
                    "no pass threshold",
                    FixBy.LLM,
                    refs=("K3",),
                )
            )
    wanted = kit.request.outputs.test_questions
    if wanted:
        finals = [t for t in kit.tests if t.scope == TestScope.FINAL]
        if not finals:
            out.append(
                Finding(
                    ERROR,
                    f"По запросу нужен итоговый тест из {wanted} вопросов, но итогового теста нет.",
                    detail="no final test",
                    fix_by=FixBy.LLM,
                    refs=("Q1.6",),
                )
            )
        for test in finals:
            count = len(_test_questions(kit, test.id))
            if count != wanted:
                out.append(
                    Finding(
                        ERROR,
                        f"В итоговом тесте {count} вопросов, а по запросу нужно {wanted}.",
                        [test.id],
                        "final test size",
                        FixBy.LLM,
                        refs=("Q1.6",),
                    )
                )
    return _cap(out, ctx)


def check_v19(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    names = _Names(kit)
    topics = {t.id: t for t in kit.topics}
    out: list[Finding] = []
    for seminar in kit.seminars:
        problems: list[str] = []
        if len(seminar.criteria) < ctx.min_seminar_criteria:
            problems.append(
                f"критериев оценки {len(seminar.criteria)}, нужно не меньше {ctx.min_seminar_criteria}"
            )
        maximum = sum(c.max_points for c in seminar.criteria)
        if seminar.pass_threshold is None:
            problems.append("не задан проходной балл")
        elif seminar.pass_threshold > maximum + EPS:
            problems.append(f"проходной балл {_fmt(seminar.pass_threshold)} больше максимума {_fmt(maximum)}")
        if not seminar.task.strip():
            problems.append("не сформулировано задание")
        if problems:
            out.append(
                Finding(
                    ERROR,
                    f"{names(seminar.id)}: {'; '.join(problems)}.",
                    [seminar.id],
                    f"seminar: {problems}",
                    FixBy.LLM,
                )
            )
        if not seminar.lo_ids:
            out.append(
                Finding(
                    WARNING,
                    f"{names(seminar.id)}: задание не привязано к результатам обучения.",
                    [seminar.id],
                    "seminar without lo",
                    FixBy.LLM,
                )
            )
        if not seminar.deliverable.strip():
            out.append(
                Finding(
                    WARNING,
                    f"{names(seminar.id)}: не указано, что сдаёт слушатель.",
                    [seminar.id],
                    "no deliverable",
                    FixBy.LLM,
                )
            )
        topic = topics.get(seminar.topic_id)
        if topic is not None:
            if topic.hours.pr + topic.hours.lb <= EPS:
                out.append(
                    Finding(
                        WARNING,
                        f"{names(seminar.id)} привязано к {names(topic.id)}, у которой нет практических часов.",
                        [seminar.id, topic.id],
                        "seminar on topic without practice",
                        FixBy.LLM,
                    )
                )
            elif seminar.kind == SeminarKind.LAB and topic.hours.lb <= EPS:
                out.append(
                    Finding(
                        WARNING,
                        f"{names(seminar.id)} — лабораторная работа, а у {names(topic.id)} нет часов ЛБ.",
                        [seminar.id, topic.id],
                        "lab on topic without lb",
                        FixBy.CODE,
                    )
                )
    hours = kit.program_hours()
    practice = hours.pr + hours.lb
    if practice > EPS and not kit.seminars:
        out.append(
            Finding(
                ERROR,
                f"В учебном плане {_fmt(practice)} ч практических занятий, но нет ни одного задания с критериями.",
                detail="no seminars",
                fix_by=FixBy.LLM,
            )
        )
    elif kit.seminars:
        seminar_topics = {s.topic_id for s in kit.seminars}
        for module in _regular_modules(kit):
            module_topics = kit.topics_of(module.id)
            module_practice = sum(t.hours.pr + t.hours.lb for t in module_topics)
            if module_practice > EPS and not seminar_topics & {t.id for t in module_topics}:
                out.append(
                    Finding(
                        WARNING,
                        f"{names(module.id)}: {_fmt(module_practice)} ч практики, но нет ни одного задания.",
                        [module.id],
                        "module practice without seminars",
                        FixBy.LLM,
                    )
                )
    return _cap(out, ctx)


def _slides_scope(kit: Kit) -> SlidesScope:
    scope = kit.program.slides_scope
    requested = kit.request.outputs.slides_scope
    if scope.mode == "default" and not scope.module_ids and requested.mode != "default":
        return requested
    return scope


def _title_matches(lecture: Lecture, topics: list[Topic]) -> bool:
    if not topics:
        return True
    candidates = [normalize_text(lecture.title)]
    if lecture.slides:
        candidates.append(normalize_text(lecture.slides[0].title))
    for topic in topics:
        wanted = normalize_text(topic.title)
        for cand in candidates:
            if cand and (cand == wanted or (len(cand) >= 5 and (cand in wanted or wanted in cand))):
                return True
    return False


def _python_program(kit: Kit) -> bool:
    words = [kit.program.title, *(t.title for t in kit.topics), *(tag for t in kit.topics for tag in t.tags)]
    return any("python" in w.lower() for w in words)


def _broken_python_examples(kit: Kit, lecture: Lecture) -> list[str]:
    """Примеры кода на Python в тезисах лекции (строки после пояснения), которые не компилируются (оценка 21.10:
    примеры в одну строку через «;» и с потерянными отступами не запускались)."""
    if not _python_program(kit):
        return []
    broken: list[str] = []
    for item in lecture.plan:
        for thesis in item.theses:
            head, _, code = thesis.partition("\n")
            if not code.strip():
                continue
            try:
                compile(code, "<thesis>", "exec")
            except (SyntaxError, ValueError):
                broken.append(_short(head or code, 40))
    return broken


def check_v20(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    names = _Names(kit)
    topics = {t.id: t for t in kit.topics}
    hours = kit.program_hours()
    out: list[Finding] = []
    lecture_topics = {tid for lec in kit.lectures for tid in lec.topic_ids}
    if hours.lk > EPS:
        if not kit.lectures:
            out.append(
                Finding(
                    ERROR,
                    f"В учебном плане {_fmt(hours.lk)} ч лекций, но планов лекций нет.",
                    detail="no lectures",
                    fix_by=FixBy.LLM,
                    refs=("K6",),
                )
            )
        else:
            for topic in kit.topics:
                if topic.hours.lk > EPS and topic.id not in lecture_topics:
                    out.append(
                        Finding(
                            WARNING,
                            f"{names(topic.id)}: в плане {_fmt(topic.hours.lk)} ч лекций, но плана лекции нет.",
                            [topic.id],
                            "topic with lk without lecture",
                            FixBy.LLM,
                            refs=("K6",),
                        )
                    )
    for lecture in kit.lectures:
        own_topics = [topics[t] for t in lecture.topic_ids if t in topics]
        if not lecture.plan:
            out.append(
                Finding(
                    ERROR,
                    f"{names(lecture.id)}: нет плана лекции.",
                    [lecture.id],
                    "empty plan",
                    FixBy.LLM,
                    refs=("K6",),
                )
            )
        if own_topics and all(t.hours.lk <= EPS for t in own_topics):
            out.append(
                Finding(
                    WARNING,
                    f"{names(lecture.id)} привязана к темам без лекционных часов.",
                    [lecture.id],
                    "lecture on topics without lk",
                    FixBy.LLM,
                )
            )
        broken = _broken_python_examples(kit, lecture)
        if broken:
            out.append(
                Finding(
                    WARNING,
                    f"{names(lecture.id)}: пример кода не запускается ({_list('«' + b + '»' for b in broken)}) — "
                    "исправьте синтаксис (отступы, двоеточия, кавычки).",
                    [lecture.id],
                    f"python examples do not compile: {len(broken)}",
                    FixBy.LLM,
                )
            )
        if lecture.slides is not None:
            if len(lecture.slides) < ctx.min_slides_per_lecture:
                out.append(
                    Finding(
                        ERROR,
                        f"{names(lecture.id)}: слайдов {len(lecture.slides)}, нужно не меньше {ctx.min_slides_per_lecture}.",
                        [lecture.id],
                        "few slides",
                        FixBy.LLM,
                        refs=("K6",),
                    )
                )
            odd = [i for i, s in enumerate(lecture.slides, start=1) if not 3 <= len(s.bullets) <= 5]
            if odd:
                out.append(
                    Finding(
                        INFO,
                        f"{names(lecture.id)}: на слайдах {_list(str(i) for i in odd)} не 3–5 тезисов.",
                        [lecture.id],
                        "slide bullets",
                        FixBy.LLM,
                    )
                )
            if not _title_matches(lecture, own_topics):
                out.append(
                    Finding(
                        ERROR,
                        f"Заголовок лекции со слайдами «{_short(lecture.title)}» не совпадает с темой учебного плана "
                        f"({_list('«' + _short(t.title, 50) + '»' for t in own_topics)}).",
                        [lecture.id],
                        "lecture title != topic title",
                        FixBy.CODE,
                        refs=("Q1.7", "Q3.9b"),
                    )
                )
    for module in kit.modules_sorted():
        if module.needs_lectures:
            without = [t for t in kit.topics_of(module.id) if t.hours.lk <= EPS]
            if without:
                out.append(
                    Finding(
                        WARNING,
                        f"{names(module.id)}: к модулю нужны слайды, но у тем {_list(names(t.id) for t in without)} нет лекционных часов.",
                        [t.id for t in without],
                        "needs_lectures without lk",
                        FixBy.CODE,
                        refs=("Q3.9a",),
                    )
                )

    scope = _slides_scope(kit)
    with_slides = {lec.id for lec in kit.lectures if lec.slides}
    expected: set[str] = set()
    if scope.mode == "sessions" and scope.session_numbers:
        days = {d.n: d for d in kit.calendar.days} if kit.calendar is not None else {}
        for number in scope.session_numbers:
            lectures = [lec for lec in kit.lectures if lec.session_no == number]
            if not lectures:
                out.append(
                    Finding(
                        ERROR,
                        f"Слайды нужны к занятию {number}, но лекции для этого занятия нет.",
                        detail=f"no lecture for session {number}",
                        fix_by=FixBy.LLM,
                        refs=("Q1.7",),
                    )
                )
            for lecture in lectures:
                expected.add(lecture.id)
                if not lecture.slides:
                    out.append(
                        Finding(
                            ERROR,
                            f"Нет слайдов к занятию {number} ({names(lecture.id)}).",
                            [lecture.id],
                            "missing slides",
                            FixBy.LLM,
                            refs=("Q1.7",),
                        )
                    )
                day = days.get(number)
                day_topics = {i.topic_id for i in day.items if i.topic_id} if day is not None else set()
                if day_topics and not set(lecture.topic_ids) & day_topics:
                    out.append(
                        Finding(
                            ERROR,
                            f"{names(lecture.id)} к занятию {number} не совпадает с темой этого занятия по "
                            f"календарю ({_list(names(t) for t in sorted(day_topics))}).",
                            [lecture.id],
                            "session topic mismatch",
                            FixBy.CODE,
                            refs=("Q1.7",),
                        )
                    )
    elif scope.mode == "module":
        targets = list(scope.module_ids)
        if not targets and scope.module_pattern:
            regex = _compile(scope.module_pattern)
            targets = [m.id for m in kit.modules_sorted() if regex.search(m.title)]
        if not targets:
            out.append(
                Finding(
                    ERROR,
                    f"Не найден модуль, к которому по запросу нужны слайды («{scope.module_pattern or '—'}»).",
                    detail="slides module not resolved",
                    fix_by=FixBy.HUMAN,
                    refs=("Q3.9a",),
                )
            )
        for module_id in targets:
            lectures = [lec for lec in kit.lectures if lec.module_id == module_id]
            if kit.module_hours(module_id).lk <= EPS:
                out.append(
                    Finding(
                        ERROR,
                        f"{names(module_id)}: к модулю нужны слайды, но в нём нет лекционных часов.",
                        [module_id],
                        "slides module without lk",
                        FixBy.CODE,
                        refs=("Q3.9a",),
                    )
                )
            elif not lectures:
                out.append(
                    Finding(
                        ERROR,
                        f"{names(module_id)}: нет лекций, к которым нужны слайды.",
                        [module_id],
                        "slides module without lectures",
                        FixBy.LLM,
                        refs=("Q3.9a",),
                    )
                )
            for lecture in lectures:
                expected.add(lecture.id)
                if not lecture.slides:
                    out.append(
                        Finding(
                            ERROR,
                            f"Нет слайдов для {names(lecture.id)}.",
                            [lecture.id],
                            "missing slides",
                            FixBy.LLM,
                            refs=("Q3.9b",),
                        )
                    )
    elif hours.lk > EPS and kit.lectures:
        complete = False
        for module in kit.modules_sorted():
            lectures = [lec for lec in kit.lectures if lec.module_id == module.id]
            if lectures and all(lec.slides for lec in lectures):
                complete = True
                break
        if not complete:
            out.append(
                Finding(
                    ERROR,
                    "Слайды не подготовлены ни для одного модуля целиком — нужны слайды ко всем лекциям хотя бы "
                    "одного модуля.",
                    detail="no module with complete slides",
                    fix_by=FixBy.LLM,
                    refs=("K6",),
                )
            )
    if expected:
        extra = sorted(with_slides - expected)
        if extra:
            out.append(
                Finding(
                    WARNING,
                    f"Слайды сделаны для лекций, которых нет в запросе: {_list(names(x) for x in extra)}.",
                    extra,
                    "extra slides",
                    FixBy.CODE,
                    refs=("Q1.7",),
                )
            )
    return _cap(out, ctx)


def check_v21(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    fa = kit.final_attestation
    request = kit.request
    if fa is None:
        return [
            Finding(
                ERROR, "Итоговая аттестация не описана.", detail="final_attestation is None", fix_by=FixBy.LLM
            )
        ]
    out: list[Finding] = []

    def err(message: str, detail: str, fix_by: FixBy = FixBy.LLM) -> None:
        out.append(Finding(ERROR, message, ["FA"], detail, fix_by))

    if request.fa_form is not None and fa.form != request.fa_form:
        err(
            f"Форма итоговой аттестации — «{FA_NAMES[fa.form]}», а в запросе — «{FA_NAMES[request.fa_form]}».",
            "fa form",
            FixBy.CODE,
        )
    if len(fa.criteria) < ctx.min_fa_criteria:
        err(
            f"У итоговой аттестации критериев оценки: {len(fa.criteria)}, нужно не меньше {ctx.min_fa_criteria}.",
            "fa criteria",
        )
    if not fa.pass_rule.strip():
        err("Не задано правило «зачтено» (порог) для итоговой аттестации.", "no pass rule")
    if not fa.lo_ids:
        out.append(
            Finding(
                WARNING,
                "Итоговая аттестация не привязана к результатам обучения.",
                ["FA"],
                "fa without lo",
                FixBy.LLM,
            )
        )
    if not _filled(fa.procedure):
        out.append(
            Finding(WARNING, "Не описана процедура итоговой аттестации.", ["FA"], "no procedure", FixBy.LLM)
        )
    if fa.form == FAForm.PORTFOLIO:
        wanted = request.portfolio_items_count
        count = len(fa.portfolio_items)
        if wanted and count != wanted:
            err(f"В портфолио материалов: {count}, а по запросу нужно {wanted}.", "portfolio count")
        elif count == 0:
            err("Не перечислены материалы портфолио.", "portfolio empty")
        empty = [p.id for p in fa.portfolio_items if not _filled(p.requirements)]
        if empty:
            err(f"У материалов портфолио {_list(empty)} нет требований.", "portfolio requirements")
    elif fa.form == FAForm.EXAM_PRACTICAL:
        task = fa.example_task
        if task is None or not (_filled(task.description) or _filled(task.title)):
            err("Нет примера практического задания для экзамена.", "no example task")
        else:
            if not task.environment.strip():
                err(
                    "В примере экзаменационного задания не указана среда выполнения (стенд, ПО, исходные данные).",
                    "no environment",
                )
            requested_text = request.fa_text or request.raw_text
            text = " ".join([task.title, task.environment, task.description, task.deliverable])
            missing = [
                w
                for w in ENV_WORDS
                if re.search(w, requested_text, re.I)
                and not re.search(w[:-1] if len(w) > 5 else w, text, re.I)
            ]
            if missing and request.fa_text:
                err(
                    f"В запросе итоговая аттестация «{_short(request.fa_text)}», а в примере задания нет упоминания: "
                    f"{', '.join(missing)}.",
                    "environment words",
                )
    elif fa.form == FAForm.PROJECT_DEFENSE:
        if not (_filled(fa.requirements) or _filled(fa.expected_result)):
            err("Не описаны требования к итоговому проекту.", "no project requirements")
    elif fa.form == FAForm.REPORT_DEFENSE:
        if not _filled(fa.report_requirements):
            err("Не описаны требования к отчёту (структура, разделы).", "no report requirements")
    elif fa.form == FAForm.TEST:
        finals = [t for t in kit.tests if t.scope == TestScope.FINAL and _test_questions(kit, t.id)]
        if not finals:
            err("Итоговая аттестация — тестирование, но итогового теста с вопросами нет.", "no final test")
    if request.constraints.anonymized_data:
        if not _filled(fa.data_requirements):
            err("Не описаны требования к данным итогового проекта.", "no data requirements")
        elif not any(ANON_RE.search(x) for x in fa.data_requirements):
            err("В требованиях к данным не сказано, что данные обезличены.", "data not anonymized")
        if not _filled(fa.expected_result):
            err("Не описан ожидаемый результат итогового проекта.", "no expected result")
    out += _fa_form_in_texts(kit, fa.form)
    return out


# Формы итоговой аттестации, которые тексты программы называют рядом со словами «итоговая аттестация»
# или «программа завершается»: «защита проекта» при экзамене — частый провал (оценка 21.10, запросы 2–4)
FA_FORM_WORDS: dict[FAForm, re.Pattern[str]] = {
    FAForm.PROJECT_DEFENSE: re.compile(r"защит\w*\s+(?:\w+\s+){0,2}проект", re.I),
    FAForm.PORTFOLIO: re.compile(r"портфолио", re.I),
    FAForm.EXAM_PRACTICAL: re.compile(r"экзамен", re.I),
    FAForm.REPORT_DEFENSE: re.compile(r"защит\w*\s+(?:\w+\s+){0,2}отч[её]т", re.I),
    FAForm.TEST: re.compile(r"тестировани|\bитогов\w*\s+тест", re.I),
}
FA_SENTENCE_RE = re.compile(r"[^.!?;]*(?:итогов\w*\s+аттестаци|заверша\w*)[^.!?;]*", re.I)


def _fa_form_in_texts(kit: Kit, form: FAForm) -> list[Finding]:
    """Тексты программы называют форму итоговой аттестации, которой в программе нет (V21)."""
    if form not in FA_FORM_WORDS:
        return []
    texts = kit.texts
    fields = [
        ("аннотация", texts.annotation),
        ("актуальность", texts.relevance),
        ("форма обучения", texts.form_description),
        ("организация образовательного процесса", texts.org_process),
        ("методические материалы", texts.methodical),
    ]
    out: list[Finding] = []
    for where, text in fields:
        for sentence in FA_SENTENCE_RE.findall(text or ""):
            named = [f for f, regex in FA_FORM_WORDS.items() if regex.search(sentence)]
            if named and form not in named:
                out.append(
                    Finding(
                        WARNING,
                        f"{where}: итоговая аттестация названа «{FA_NAMES[named[0]]}», а в программе — "
                        f"«{FA_NAMES[form]}».",
                        ["FA"],
                        f"fa form in texts: {named[0].value} != {form.value}",
                        FixBy.LLM,
                    )
                )
                break
    return out


def check_v22(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    names = _Names(kit)
    required_all = bool(TK_INTERVIEW_REQ_RE.search(kit.request.raw_text))
    out: list[Finding] = []
    for module in _regular_modules(kit):
        is_interview = bool(module.control_form and INTERVIEW_RE.search(module.control_form))
        if required_all and not is_interview:
            out.append(
                Finding(
                    ERROR,
                    f"{names(module.id)}: по запросу текущий контроль — собеседование, а у модуля форма контроля "
                    f"«{module.control_form or 'не указана'}».",
                    [module.id],
                    "control form",
                    FixBy.CODE,
                )
            )
        if is_interview or required_all:
            count = sum(1 for iq in kit.interview_questions if iq.module_id == module.id)
            if count < ctx.min_interview_per_module:
                out.append(
                    Finding(
                        ERROR,
                        f"{names(module.id)}: вопросов для собеседования {count}, нужно не меньше "
                        f"{ctx.min_interview_per_module}.",
                        [module.id],
                        "few interview questions",
                        FixBy.LLM,
                    )
                )
    for iq in kit.interview_questions:
        if not iq.lo_ids or not _filled(iq.answer_points):
            what = "результатам обучения" if not iq.lo_ids else "ожидаемому ответу"
            out.append(
                Finding(
                    WARNING,
                    f"{names(iq.id)}: не задана привязка к {what}.",
                    [iq.id],
                    "interview question incomplete",
                    FixBy.LLM,
                )
            )
    return _cap(out, ctx)


# --------------------------------------------------------------------------------------------------
# V23–V30: источники, нормативка, ПС, тексты по реестру, реквизиты
# --------------------------------------------------------------------------------------------------


def _no_registry(what: str) -> Finding:
    return Finding(
        WARNING,
        f"Реестр проверенных фактов не подключён: {what} не сверены.",
        detail="ctx.registry is None",
        fix_by=FixBy.CODE,
    )


def isbn_valid(isbn: str) -> bool:
    digits = re.sub(r"[^0-9Xx]", "", isbn).upper()
    if len(digits) == 10:
        if not digits[:9].isdigit() or not (digits[9].isdigit() or digits[9] == "X"):
            return False
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(digits))
        return total % 11 == 0
    if len(digits) == 13 and digits.isdigit():
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(digits))
        return total % 10 == 0
    return False


def _registry_record(registry: RegistryView, source: Source) -> object | None:
    key = source.registry_id
    if not key:
        return None
    if source.kind == SourceKind.NORM:
        return registry.norm(key)
    if source.kind == SourceKind.PROFSTANDARD:
        return registry.profstandard(key)
    return registry.literature(key) or registry.norm(key) or registry.profstandard(key)


def _url_host(url: str | None) -> str:
    match = re.match(r"^[a-z]+://([^/:?#]+)", (url or "").strip().lower())
    return match.group(1).removeprefix("www.") if match else ""


def _tool_record(registry: RegistryView, source: Source) -> object | None:
    """Официальная документация инструмента: совпадение по ID записи tools.json, названию или домену URL."""
    tools = registry.tools()
    if source.registry_id:
        found = next((t for t in tools if t.id == source.registry_id), None)
        if found is not None:
            return found
    found = registry.tool(source.title)
    if found is not None:
        return found
    host = _url_host(source.url)
    if host:
        return next((t for t in tools if t.docs_url and _url_host(t.docs_url) == host), None)
    return None


def _service_record(registry: RegistryView, source: Source) -> object | None:
    """Официальный сайт ИИ-сервиса из реестра: по ID записи или домену URL; недоступный в России — не в счёт."""
    services = [s for s in registry.ai_services() if s.available_in_ru and s.url]
    found = next((s for s in services if source.registry_id and s.id == source.registry_id), None)
    host = _url_host(source.url)
    return found or next((s for s in services if host and _url_host(s.url) == host), None)


def check_v23(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    if not kit.sources:
        return []  # пустые разделы литературы — V12
    names = _Names(kit)
    registry = ctx.registry
    out: list[Finding] = []
    if registry is None:
        out.append(_no_registry("источники литературы и интернет-ресурсы"))
    for source in kit.sources:
        label = names(source.id)
        if (
            source.kind in (SourceKind.NORM, SourceKind.PROFSTANDARD)
            and source.verification != Verification.REGISTRY
        ):
            out.append(
                Finding(
                    ERROR,
                    f"{label}: нормативные акты и профстандарты берутся только из реестра.",
                    [source.id],
                    f"norm/ps verification={source.verification.value}",
                    FixBy.CODE,
                )
            )
        if source.isbn and not isbn_valid(source.isbn):
            out.append(
                Finding(
                    ERROR,
                    f"{label}: ISBN {source.isbn} не проходит проверку контрольной суммы — издание, вероятно, выдумано.",
                    [source.id],
                    "bad isbn",
                    FixBy.CODE,
                )
            )
        if source.year is not None and source.year > ctx.current_year:
            out.append(
                Finding(
                    ERROR,
                    f"{label}: год издания {source.year} ещё не наступил.",
                    [source.id],
                    "future year",
                    FixBy.CODE,
                )
            )
        if registry is None:
            continue
        if source.verification == Verification.REGISTRY:
            record = _registry_record(registry, source)
            if record is None:
                out.append(
                    Finding(
                        ERROR,
                        f"{label} помечен как взятый из реестра, но в реестре его нет ({source.registry_id or 'ID не указан'}).",
                        [source.id],
                        "registry record missing",
                        FixBy.CODE,
                    )
                )
            elif not getattr(record, "verified", False):
                out.append(
                    Finding(
                        WARNING,
                        f"{label}: запись реестра ещё не подтверждена по первоисточнику — выводить с меткой "
                        f"«{UNVERIFIED_MARK}».",
                        [source.id],
                        "registry record to_verify",
                        FixBy.HUMAN,
                    )
                )
        elif source.verification == Verification.CATALOG:
            record = registry.literature(source.registry_id) if source.registry_id else None
            record = record or registry.find_literature(source.title, source.authors)
            if record is None and source.kind in (SourceKind.DOC, SourceKind.WEB, SourceKind.SOFTWARE):
                record = _tool_record(registry, source)  # официальная документация из tools.json (§2.4)
            if record is None and source.kind in (SourceKind.DOC, SourceKind.WEB):
                record = _service_record(
                    registry, source
                )  # официальный сайт ИИ-сервиса (ru_ai_services.json)
            if record is None:
                out.append(
                    Finding(
                        ERROR,
                        f"{label} помечен как найденный в каталоге, но в каталоге его нет — нужна метка «{UNVERIFIED_MARK}».",
                        [source.id],
                        "catalog record missing",
                        FixBy.CODE,
                    )
                )
            else:
                diffs = []
                record_title = getattr(
                    record, "title", None
                )  # у записи документации (ToolRecord) названия нет
                record_year = getattr(record, "year", None)
                if record_title and normalize_text(record_title) != normalize_text(source.title):
                    diffs.append("название")
                if record_year and source.year and record_year != source.year:
                    diffs.append("год")
                if diffs:
                    out.append(
                        Finding(
                            WARNING,
                            f"{label}: выходные данные ({', '.join(diffs)}) расходятся с каталогом.",
                            [source.id],
                            f"catalog mismatch: {diffs}",
                            FixBy.CODE,
                        )
                    )
        elif source.verification == Verification.UNVERIFIED:
            if source.year is not None and source.year < ctx.current_year - ctx.max_source_age_years:
                out.append(
                    Finding(
                        WARNING,
                        f"{label}: издание {source.year} года — для учебной программы слишком старое.",
                        [source.id],
                        "old source",
                        FixBy.LLM,
                    )
                )
            if source.publisher and not registry.publisher_known(source.publisher):
                out.append(
                    Finding(
                        WARNING,
                        f"{label}: издательство «{source.publisher}» не найдено в списке известных издательств.",
                        [source.id],
                        "unknown publisher",
                        FixBy.HUMAN,
                    )
                )
    per_section = Counter(s.section for s in kit.sources if s.unverified)
    for section, count in per_section.items():
        if count > ctx.max_unverified_per_section:
            out.append(
                Finding(
                    WARNING,
                    f"В подразделе литературы «{section.value}» неподтверждённых источников: {count} — допускается "
                    f"не больше {ctx.max_unverified_per_section}.",
                    [s.id for s in kit.sources if s.unverified and s.section == section],
                    "too many unverified",
                    FixBy.CODE,
                )
            )
    return _cap(out, ctx)


def _norm_name(registry: RegistryView | None, norm_id: str) -> str:
    record = registry.norm(norm_id) if registry is not None else None
    if record is not None:
        return _norm_short(record)
    return NORM_FALLBACK_NAMES.get(norm_id, norm_id)


# Род вида акта: «приказ утратил», «постановление / распоряжение / письмо утратило».
_LOST_FORCE = {"decree": "утратило", "letter": "утратило"}


def _lost_force(record: NormRecord) -> str:
    return _LOST_FORCE.get(record.kind, "утратил")


def _upper_first(text: str) -> str:
    """Первая буква заглавная, остальное как есть (str.capitalize испортил бы «Правительства РФ»)."""
    return text[:1].upper() + text[1:]


def _norm_short(record: NormRecord) -> str:
    word = NORM_KIND_WORDS.get(record.kind, "акт")
    if record.kind == "decree" and record.title.lower().startswith("распоряжение"):
        word = "распоряжение Правительства РФ"  # kind decree — и постановления, и распоряжения
    date_text = f" от {_iso_to_ru(record.date)}" if record.date else ""
    if record.kind == "federal_law":
        return f"{word}{date_text} № {record.number}"
    return f"{word}{date_text} № {record.number}"


def _texts_for_registry(kit: Kit) -> list[_Text]:
    return [t for t in _collect_texts(kit) if t.scope not in ("source", "term")]


def check_v24(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    family = _family(kit)
    form = kit.program.form
    norm_ids = list(kit.program.norm_ids)
    registry = ctx.registry
    out: list[Finding] = []
    required = list(REQUIRED_NORMS[family])
    if form.dot or form.eo:
        required.append(DOT_NORM)
    missing = [n for n in required if n not in norm_ids]
    if missing:
        out.append(
            Finding(
                WARNING,
                f"В нормативной базе нет обязательных для этого типа программы актов: "
                f"{'; '.join(_norm_name(registry, n) for n in missing)}.",
                detail=f"missing norms: {missing}",
                fix_by=FixBy.CODE,
                refs=("K9", "F6") if DOT_NORM in missing else ("K9",),
            )
        )
    if registry is None:
        for norm_id in norm_ids:
            if norm_id in KNOWN_REPLACED_NORMS:
                out.append(
                    Finding(
                        ERROR,
                        f"{NORM_FALLBACK_NAMES.get(norm_id, norm_id)} утратил силу — его нельзя указывать как действующий; "
                        f"действует {NORM_FALLBACK_NAMES[KNOWN_REPLACED_NORMS[norm_id]]}.",
                        detail=f"replaced norm: {norm_id}",
                        fix_by=FixBy.CODE,
                        refs=("F4", "G4b"),
                    )
                )
        if norm_ids:
            out.append(_no_registry("реквизиты и действие нормативных актов"))
        return out
    for norm_id in norm_ids:
        record = registry.norm(norm_id)
        if record is None:
            out.append(
                Finding(
                    ERROR,
                    f"Нормативный акт «{norm_id}» не найден в реестре — его нельзя приводить в программе.",
                    detail=f"unknown norm: {norm_id}",
                    fix_by=FixBy.CODE,
                    refs=("G4b",),
                )
            )
            continue
        if not record.in_force:
            replacement = registry.norm(record.replaced_by) if record.replaced_by else None
            tail = f"; действует {_norm_short(replacement)}" if replacement is not None else ""
            until = f" с {_iso_to_ru(record.in_force_until)}" if record.in_force_until else ""
            out.append(
                Finding(
                    ERROR,
                    f"{_upper_first(_norm_short(record))} {_lost_force(record)} силу{until} — нельзя указывать как действующий акт{tail}.",
                    detail=f"norm not in force: {norm_id}",
                    fix_by=FixBy.CODE,
                    refs=("F4", "G4b"),
                )
            )
            continue
        if not record.verified:
            out.append(
                Finding(
                    WARNING,
                    f"Реквизиты акта «{_norm_short(record)}» не подтверждены по первоисточнику — выводить с меткой "
                    f"«{UNVERIFIED_MARK}».",
                    detail=f"norm to_verify: {norm_id}",
                    fix_by=FixBy.HUMAN,
                    refs=("G4b",),
                )
            )
        if record.applies_to and family not in record.applies_to:
            out.append(
                Finding(
                    WARNING,
                    f"{_upper_first(_norm_short(record))} не относится к программам этого типа.",
                    detail=f"norm applies_to={record.applies_to}",
                    fix_by=FixBy.CODE,
                    refs=("K9",),
                )
            )
    names = _Names(kit)
    for source in kit.sources:
        if source.kind != SourceKind.NORM or not source.registry_id:
            continue
        record = registry.norm(source.registry_id)
        if record is None:
            continue  # V23
        if normalize_text(source.title) != normalize_text(record.title):
            out.append(
                Finding(
                    ERROR,
                    f"{names(source.id)}: название и реквизиты акта не совпадают с реестром.",
                    [source.id],
                    "norm title mismatch",
                    FixBy.CODE,
                    refs=("G4b",),
                )
            )
        if source.registry_id not in norm_ids:
            out.append(
                Finding(
                    WARNING,
                    f"{names(source.id)} есть в списке документов, но не указан в нормативной базе программы.",
                    [source.id],
                    "norm source not in norm_ids",
                    FixBy.CODE,
                    refs=("G4b",),
                )
            )
    texts = _texts_for_registry(kit)
    for record in registry.norms():
        if not record.number or not record.date:
            continue
        number_re = re.compile(r"№\s*" + re.escape(record.number) + r"(?![\w/])", re.I)
        for item in texts:
            for match in number_re.finditer(item.text):
                window = item.text[max(0, match.start() - 60) : match.end() + 60]
                dates = [f"{y}-{m}-{d}" for d, m, y in DATE_RE.findall(window)]
                if dates and record.date not in dates:
                    out.append(
                        Finding(
                            WARNING,
                            f"{item.where}: у акта № {record.number} указана дата {_iso_to_ru(dates[0])}, а по реестру — "
                            f"{_iso_to_ru(record.date)}.",
                            [item.target] if item.target else [],
                            "norm date mismatch",
                            FixBy.CODE,
                            refs=("F5",),
                        )
                    )
    return _cap(out, ctx)


def check_v25(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    if kit.program.type not in PK_TYPES:
        return []
    names = _Names(kit)
    registry = ctx.registry
    out: list[Finding] = []
    if not kit.program.profstandards:
        out.append(
            Finding(
                ERROR,
                "Для программы повышения квалификации не указан профессиональный стандарт.",
                detail="no profstandards",
                fix_by=FixBy.CODE,
                refs=("K10",),
            )
        )
    for comp in kit.competencies:
        if not comp.tf_refs:
            out.append(
                Finding(
                    ERROR,
                    f"{names(comp.id)}: не указана трудовая функция профстандарта.",
                    [comp.id],
                    "no tf_refs",
                    FixBy.LLM,
                    refs=("K10",),
                )
            )
        stray = sorted({r.ps_code for r in comp.tf_refs} - set(kit.program.profstandards))
        if stray:
            out.append(
                Finding(
                    WARNING,
                    f"{names(comp.id)}: профстандарт {_list(stray)} не указан в паспорте программы.",
                    [comp.id],
                    "ps not in program",
                    FixBy.CODE,
                )
            )
    competencies = {c.id: c for c in kit.competencies}
    for outcome in kit.outcomes:
        comp = competencies.get(outcome.competency_id)
        if outcome.tf_code and comp is not None and outcome.tf_code not in {r.tf_code for r in comp.tf_refs}:
            out.append(
                Finding(
                    ERROR,
                    f"{names(outcome.id)}: трудовая функция {outcome.tf_code} не относится к своей компетенции.",
                    [outcome.id],
                    "lo tf_code not in competency",
                    FixBy.CODE,
                )
            )
    if registry is None:
        out.append(_no_registry("коды профстандартов и трудовых функций"))
        return out
    for code in kit.program.profstandards:
        record = registry.profstandard(code)
        if record is None:
            out.append(
                Finding(
                    ERROR,
                    f"Профстандарт {code} не найден в реестре — код нельзя выводить без подтверждения.",
                    detail=f"unknown ps {code}",
                    fix_by=FixBy.CODE,
                    refs=("G4c",),
                )
            )
        elif not record.verified:
            out.append(
                Finding(
                    WARNING,
                    f"Профстандарт {code} «{record.title}» ещё не подтверждён по первоисточнику — коды выводить с меткой "
                    f"«{UNVERIFIED_MARK}».",
                    detail=f"ps to_verify {code}",
                    fix_by=FixBy.HUMAN,
                    refs=("G4c",),
                )
            )
    for comp in kit.competencies:
        for ref in comp.tf_refs:
            if registry.labor_function(ref.ps_code, ref.tf_code) is None:
                out.append(
                    Finding(
                        ERROR,
                        f"{names(comp.id)}: трудовой функции {ref.tf_code} нет в профстандарте {ref.ps_code} по реестру.",
                        [comp.id],
                        f"unknown tf {ref.ps_code} {ref.tf_code}",
                        FixBy.CODE,
                        refs=("G4c",),
                    )
                )
    return _cap(out, ctx)


def check_v26(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    if kit.program.type != ProgramType.DOP:
        return []
    names = _Names(kit)
    out: list[Finding] = []
    if kit.program.profstandards:
        out.append(
            Finding(
                ERROR,
                "В общеразвивающей программе не должно быть профстандартов.",
                detail="dop profstandards",
                fix_by=FixBy.CODE,
            )
        )
    for comp in kit.competencies:
        if comp.tf_refs:
            out.append(
                Finding(
                    ERROR,
                    f"{names(comp.id)}: в общеразвивающей программе нет трудовых функций.",
                    [comp.id],
                    "dop tf_refs",
                    FixBy.CODE,
                )
            )
    for outcome in kit.outcomes:
        if outcome.tf_code:
            out.append(
                Finding(
                    ERROR,
                    f"{names(outcome.id)}: в общеразвивающей программе нет трудовых функций.",
                    [outcome.id],
                    "dop tf_code",
                    FixBy.CODE,
                )
            )
    for item in _texts_for_registry(kit):
        match = PS_WORD_RE.search(item.text) or _ps_code_search(item.text) or TF_CODE_RE.search(item.text)
        if match:
            out.append(
                Finding(
                    ERROR,
                    f"{item.where}: «{match.group(0)}» — в общеразвивающей программе не упоминаются профстандарты и "
                    "трудовые функции.",
                    [item.target] if item.target else [],
                    "dop ps mention",
                    FixBy.LLM,
                )
            )
    return _cap(out, ctx)


def _marked(text: str, end: int) -> bool:
    return UNVERIFIED_MARK in text[end : end + 60]


@dataclass(frozen=True)
class _KnownCodes:
    acts: set[str]
    ps: set[str]
    tf: set[str]


def _known_codes(registry: RegistryView) -> _KnownCodes:
    acts = {_norm_number(n.number) for n in registry.norms() if n.number}
    for standard in registry.profstandards():
        acts |= {_norm_number(x) for x in NUMBER_RE.findall(standard.order)}
    return _KnownCodes(
        acts=acts,
        ps={p.code for p in registry.profstandards()},
        tf={f.code for p in registry.profstandards() for f in p.labor_functions},
    )


def _ps_context(text: str, match: re.Match[str]) -> bool:
    """«06.015» — код ПС, только если рядом слова о профстандарте (иначе это сумма, версия, номер пункта)."""
    window = text[max(0, match.start() - PS_CONTEXT_BEFORE) : match.end() + PS_CONTEXT_AFTER]
    return bool(PS_CONTEXT_RE.search(window))


def _ps_code_search(text: str) -> re.Match[str] | None:
    return next((m for m in PS_CODE_RE.finditer(text) if _ps_context(text, m)), None)


def _code_mentions(text: str) -> list[tuple[str, str, str, int]]:
    """(вид, код, фрагмент, конец фрагмента) — номера актов, коды ПС и ТФ без метки «[не подтверждено]»."""
    found: list[tuple[str, str, str, int]] = []
    for regex, kind in ((ACT_RE, "act"), (FZ_RE, "act"), (PS_CODE_RE, "ps"), (TF_CODE_RE, "tf")):
        for match in regex.finditer(text):
            if _marked(text, match.end()) or (kind == "ps" and not _ps_context(text, match)):
                continue
            end = match.end()
            if kind == "act":  # «№ 4321.» — точка конца предложения не входит в номер
                code = match.group(1).rstrip("./-")
                end = match.end(1) - (len(match.group(1)) - len(code))
            else:
                code = match.group(0)
            found.append((kind, code.translate(LOOKALIKES) if kind == "tf" else code, match.group(0), end))
    return found


def _code_known(kind: str, code: str, known: _KnownCodes) -> bool:
    if kind == "act":
        number = _norm_number(code)
        return number in known.acts or "_" in code or number.endswith("-обр")  # реквизиты организации — V30
    return code in (known.ps if kind == "ps" else known.tf)


def mark_unknown_codes(text: str, registry: RegistryView) -> str:
    """Номера актов, коды ПС и ТФ, которых нет в реестре, получают метку «[не подтверждено]» (жёсткое правило 3).
    Для текстов, написанных моделью: код, а не модель, решает, что подтверждено."""
    if not text:
        return text
    mentions = _code_mentions(text)
    if not mentions:
        return text
    known = _known_codes(registry)
    ends = sorted(
        {end for kind, code, _, end in mentions if not _code_known(kind, code, known)}, reverse=True
    )
    for end in ends:
        text = f"{text[:end]} {UNVERIFIED_MARK}{text[end:]}"
    return text


def check_v27(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    """Номера актов, коды ПС и ТФ в свободном тексте — только из реестра или с меткой."""
    found = [(item, *mention) for item in _texts_for_registry(kit) for mention in _code_mentions(item.text)]
    if not found:
        return []
    registry = ctx.registry
    if registry is None:
        return [_no_registry(f"номера актов и коды ПС/ТФ в тексте ({len(found)} упоминаний)")]
    known = _known_codes(registry)
    out: list[Finding] = []
    words = {"act": "акта", "ps": "профстандарта", "tf": "трудовой функции"}
    for item, kind, code, fragment, _ in found:
        if _code_known(kind, code, known):
            continue
        out.append(
            Finding(
                WARNING,
                f"{item.where}: «{_short(fragment, 60)}» — номера {words[kind]} нет в реестре; убрать или пометить "
                f"«{UNVERIFIED_MARK}».",
                [item.target] if item.target else [],
                f"{kind} not in registry: {code}",
                FixBy.LLM,
            )
        )
    return _cap(out, ctx)


def _mentions(name: str, folded_text: str) -> bool:
    """Упоминание сервиса в тексте (уже через fold_yo) — тем же поиском, что и в реестре (с падежными
    окончаниями: «в Дипсике»)."""
    pattern = name_pattern(name)
    return pattern is not None and pattern.search(folded_text) is not None


def _blocked_services_soft(kit: Kit, registry: RegistryView) -> list[Finding]:
    """Без требования «только сервисы РФ»: недоступный в России сервис — предупреждение, а не ошибка."""
    texts = [t for t in _collect_texts(kit) if t.scope != "term"]
    folded = {id(t): fold_yo(t.text) for t in texts}
    out: list[Finding] = []
    for service in registry.ai_services():
        if service.available_in_ru:
            continue
        hits = [t for name in service.all_names() for t in texts if _mentions(name, folded[id(t)])]
        if hits:
            out.append(
                Finding(
                    WARNING,
                    f"Упомянут сервис «{service.name}», недоступный в России без обхода ограничений: "
                    f"{_list(dict.fromkeys(t.where for t in hits))}. Лучше заменить на доступный аналог.",
                    sorted({t.target for t in hits if t.target}),
                    f"blocked service {service.id} (soft)",
                    FixBy.LLM,
                    refs=("Q2.5a",),
                )
            )
    return out


def check_v28(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    strict = kit.request.constraints.ru_services_only
    registry = ctx.registry
    if registry is None:
        return [_no_registry("доступность упомянутых ИИ-сервисов в России")] if strict else []
    if not strict:
        return _blocked_services_soft(kit, registry)
    texts = [t for t in _collect_texts(kit) if t.scope != "term"]
    folded = {id(t): fold_yo(t.text) for t in texts}
    out: list[Finding] = []
    services = registry.ai_services()
    for service in services:
        if service.available_in_ru:
            continue
        hits = [t for name in service.all_names() for t in texts if _mentions(name, folded[id(t)])]
        if hits:
            out.append(
                Finding(
                    ERROR,
                    f"Упомянут сервис «{service.name}», недоступный в России без обхода ограничений: "
                    f"{_list(dict.fromkeys(t.where for t in hits))}.",
                    sorted({t.target for t in hits if t.target}),
                    f"blocked service {service.id}",
                    FixBy.LLM,
                    refs=("Q2.5a",),
                )
            )
    known = {normalize_text(n) for s in services for n in s.all_names()}
    known |= {normalize_text(n) for tool in registry.tools() for n in tool.all_names()}
    candidates: dict[str, list[str]] = defaultdict(list)
    for item in texts:
        for match in SERVICE_CANDIDATE_RE.finditer(item.text):
            name = match.group(1).rstrip(".")
            if normalize_text(name) not in known:
                candidates[name].append(item.where)
    for name, wheres in candidates.items():
        out.append(
            Finding(
                INFO,
                f"Сервис «{name}» не найден в списке проверенных — проверьте, доступен ли он в России "
                f"({_list(dict.fromkeys(wheres))}).",
                detail="service candidate for judge",
                fix_by=FixBy.HUMAN,
                refs=("Q2.5b",),
            )
        )
    return _cap(out, ctx)


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text))


# После имени инструмента — номер версии: через пробел, «v», «версия»/«версии» (не «pip3», не «python3»);
# число, за которым идут часы, минуты, проценты и т. п., — не версия («Docker 2 ч практики»).
_VERSION_SEP = r"(?:\s+(?:верси[яиюей]\s+)?|\s*v\.?\s*|\s*,\s*верси[яиюей]\s+)"
_NOT_VERSION_TAIL = r"(?!\s*(?:ч\b|ч\.|час|мин|сек|%|шт|раз|дн|нед|мес|ак\.|академ|занят|урок|тем|модул))"


def _version_regex(name: str) -> re.Pattern[str]:
    return re.compile(
        r"(?<![\w])"
        + re.escape(name)
        + _VERSION_SEP
        + r"(\d+(?:\.\d+){0,3})(?![\d.,]\d)(?![\w])"
        + _NOT_VERSION_TAIL,
        re.I,
    )


def check_v29(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    texts = [t for t in _collect_texts(kit) if t.scope != "term"]
    registry = ctx.registry
    if registry is None:
        generic = re.compile(r"(?<![\w])[A-Za-z][\w+#-]*\s+v?\d+\.\d+")
        examples = [m.group(0) for t in texts for m in generic.finditer(t.text)]
        return (
            [_no_registry(f"версии инструментов ({_list(dict.fromkeys(examples), 3)})")] if examples else []
        )
    out: list[Finding] = []
    for tool in registry.tools():
        if not tool.min_version:
            continue
        minimum = _version_tuple(tool.min_version)
        for name in tool.all_names():
            regex = _version_regex(name)
            for item in texts:
                for match in regex.finditer(item.text):
                    version = _version_tuple(match.group(1))
                    if version < minimum[: len(version)]:
                        out.append(
                            Finding(
                                ERROR,
                                f"{item.where}: {tool.name} {match.group(1)} — ниже минимальной поддерживаемой версии "
                                f"{tool.min_version}.",
                                [item.target] if item.target else [],
                                f"old version {tool.id} {match.group(1)} < {tool.min_version}",
                                FixBy.LLM,
                            )
                        )
    return _cap(out, ctx)


def check_v30(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    """Реквизиты приказа, ФИО и подписи — пустые поля."""
    out: list[Finding] = []
    for item in _texts_for_registry(kit):
        for regex in ORDER_FILLED_RES:
            match = regex.search(item.text)
            if match:
                out.append(
                    Finding(
                        ERROR,
                        f"{item.where}: заполнены реквизиты приказа («{_short(match.group(0), 40)}») — номер и дата "
                        "должны оставаться пустыми (____).",
                        [item.target] if item.target else [],
                        "order requisites filled",
                        FixBy.CODE,
                        refs=("G5a",),
                    )
                )
                break
        for regex in (FIO_FULL_RE, FIO_INITIALS_RE, FIO_INITIALS_FIRST_RE):
            match = regex.search(item.text)
            if match:
                severity = WARNING if item.scope == "content" else ERROR
                out.append(
                    Finding(
                        severity,
                        f"{item.where}: похоже на ФИО («{match.group(0)}») — ФИО преподавателей и подписи в программе "
                        "не указываются.",
                        [item.target] if item.target else [],
                        "fio",
                        FixBy.LLM,
                        refs=("G5b", "G5c"),
                    )
                )
                break
    return _cap(out, ctx)


# --------------------------------------------------------------------------------------------------
# V32–V44: язык, требования запроса, гигиена текста
# --------------------------------------------------------------------------------------------------


def _language_tokens(text: str, skip: set[str]) -> tuple[list[str], list[str]]:
    text = INLINE_CODE_RE.sub(" ", URL_RE.sub(" ", text))
    cyr: list[str] = []
    lat: list[str] = []
    for raw in text.split():
        token = raw.strip(".,;:!?«»\"'()[]—–-…")
        if len(token) < 2 or CODE_TOKEN_RE.search(token):
            continue
        if re.fullmatch(r"[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*", token):
            cyr.append(token)
        elif re.fullmatch(r"[A-Za-z]+(?:[-'][A-Za-z]+)*", token):
            if CAMEL_RE.search(token) or token.lower() in skip:
                continue
            lat.append(token)
    return cyr, lat


# Код в вариантах ответа и заданиях («SELECT … FROM …», «import pandas as pd», «df.groupby(…)») — не текст на языке
_CYR = "А-Яа-яЁё"
CODE_LIKE_RE = re.compile(
    r"\b(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b[\s\S]*?\b(?:FROM|INTO|SET|TABLE|WHERE)\b"
    r"|^\s*(?:import|from)\s+[\w.]+|\b\w+(?:\.\w+)*\([^()]*\)|[\w\]]\s*(?:=|:=|==)\s*[\w'\"\[(]"
)
CODE_SYMBOLS = set("=()[]{};:.,'\"<>*_/#")


def looks_like_code(text: str) -> bool:
    """Поле — фрагмент кода: SQL, импорт, вызов функции, присваивание или много служебных символов."""
    stripped = text.strip()
    if not stripped or re.search(f"[{_CYR}]", stripped) and not CODE_LIKE_RE.search(stripped):
        return False
    symbols = sum(1 for ch in stripped if ch in CODE_SYMBOLS)
    return bool(CODE_LIKE_RE.search(stripped)) or symbols / len(stripped) > 0.15


def check_v32(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    registry = ctx.registry
    skip: set[str] = set()
    if registry is not None:
        skip |= {n.lower() for t in registry.tools() for n in t.all_names()}
        skip |= {n.lower() for s in registry.ai_services() for n in s.all_names()}
    texts = [
        t
        for t in _collect_texts(kit)
        if t.scope not in ("source", "software", "term") and not looks_like_code(t.text)
    ]
    out: list[Finding] = []
    cyr_letters = lat_letters = 0
    for item in texts:
        cyr, lat = _language_tokens(item.text, skip)
        cyr_letters += sum(len(w) for w in cyr)
        lat_letters += sum(len(w) for w in lat)
        words = len(cyr) + len(lat)
        if words < ctx.language_min_words:
            continue
        stop = {w.lower() for w in lat} & ENGLISH_STOPWORDS
        share = len(cyr) / words
        if (not cyr and len(lat) >= ctx.language_min_words) or (
            len(stop) >= 3 and share < ctx.cyrillic_field_min_share
        ):
            out.append(
                Finding(
                    WARNING,
                    f"{item.where}: текст не на русском языке («{_short(item.text, 60)}»).",
                    [item.target] if item.target else [],
                    f"field cyrillic share {share:.2f}",
                    FixBy.LLM,
                )
            )
    letters = cyr_letters + lat_letters
    if letters and cyr_letters / letters < ctx.cyrillic_min_share:
        out.insert(
            0,
            Finding(
                ERROR,
                f"Материалы комплекта написаны не по-русски: русских букв {cyr_letters / letters:.0%} "
                f"(нужно не меньше {ctx.cyrillic_min_share:.0%}; код и названия программ не учитываются).",
                detail=f"cyrillic share {cyr_letters}/{letters}",
                fix_by=FixBy.LLM,
            ),
        )
    return _cap(out, ctx)


def _max_matching(options: list[list[str]]) -> dict[int, str]:
    """Паросочетание «требование → отдельный объект» (алгоритм Куна)."""
    owner: dict[str, int] = {}

    def try_assign(i: int, seen: set[str]) -> bool:
        for obj in options[i]:
            if obj in seen:
                continue
            seen.add(obj)
            if obj not in owner or try_assign(owner[obj], seen):
                owner[obj] = i
                return True
        return False

    for i in range(len(options)):
        try_assign(i, set())
    return {i: obj for obj, i in owner.items()}


def check_v33(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    # предмет программы («кибербезопасность» в «Кибербезопасность для студентов колледжа») покрывает вся программа
    requirements = without_subject(kit.request.required_topics, kit.request.title or kit.program.title)
    if not requirements:
        return []
    names = _Names(kit)
    candidates = [(m.id, m.title, kit.module_hours(m.id).total) for m in kit.modules_sorted()]
    candidates += [(t.id, t.title, t.hours.total) for t in kit.topics]
    options: list[list[str]] = []
    for req in requirements:
        patterns = _requirement_patterns(req)
        options.append(
            [
                obj
                for obj, title, hours in candidates
                if hours > EPS and any(p.search(title) for p in patterns)
            ]
        )
    matching = _max_matching(options)
    out: list[Finding] = []
    for i, req in enumerate(requirements):
        if i in matching:
            continue
        if not options[i]:
            message = f"Нет отдельной темы или модуля «{req.label}» с часами, хотя это требование запроса."
        else:
            message = (
                f"Тема «{req.label}» не выделена отдельно: подходящие разделы "
                f"({_list(names(x) for x in options[i])}) уже закрывают другие обязательные темы."
            )
        out.append(Finding(ERROR, message, list(options[i]), f"required topic: {req.label}", FixBy.HUMAN))
    return out


def check_v34(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    wanted = kit.request.constraints.intro_block_topic
    if not wanted:
        return []
    words = [
        w
        for w in re.findall(r"[\w+#]+", wanted.lower().replace("ё", "е"))
        if len(w) >= 3 and w not in INTRO_STOPWORDS
    ]
    words = words or [wanted.lower()]
    stems = [w[:5] if re.match(r"[а-я]", w) and len(w) > 5 else w for w in words]
    modules = kit.modules_sorted()
    titles: list[str] = []
    if modules:
        titles.append(modules[0].title)
        first_topics = kit.topics_of(modules[0].id)
        if first_topics:
            titles.append(first_topics[0].title)
    if any(any(s in title.lower().replace("ё", "е") for s in stems) for title in titles):
        return []
    first = f"«{_short(titles[0])}»" if titles else "не задан"
    return [
        Finding(
            ERROR,
            f"Программа должна начинаться с вводного блока по теме «{wanted}», а первый модуль — {first}.",
            [modules[0].id] if modules else [],
            "intro block missing",
            FixBy.HUMAN,
        )
    ]


def check_v35(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    """Порядок тем программирования: классы, ООП, API — не раньше переменных, условий и циклов."""
    names = _Names(kit)
    ordered = [t for m in kit.modules_sorted() for t in kit.topics_of(m.id)]
    titles = [LIFECYCLE_RE.sub(" ", t.title) for t in ordered]
    first_basic = {
        g: next((i for i, s in enumerate(titles) if rx.search(s)), None) for g, rx in BASIC_GROUPS.items()
    }
    found = {g: i for g, i in first_basic.items() if i is not None}
    if len(found) < 2:
        return []  # не вводный курс программирования
    advanced = [i for i, s in enumerate(titles) if ADVANCED_RE.search(s) and i not in found.values()]
    if not advanced:
        return []
    out: list[Finding] = []
    last_basic = max(found.values())
    early = [i for i in advanced if i < last_basic]
    if early:
        out.append(
            Finding(
                ERROR,
                f"Сложные темы идут раньше базовых: {_list(names(ordered[i].id) for i in early)} стоит раньше, чем "
                f"{names(ordered[last_basic].id)}. Сначала переменные, условия и циклы, потом классы и работа с API.",
                [ordered[i].id for i in early],
                f"order: basic={found}, advanced={advanced}",
                FixBy.HUMAN,
            )
        )
    missing = [g for g in BASIC_GROUPS if g not in found]
    if missing:
        out.append(
            Finding(
                WARNING,
                f"Перед сложными темами нет базовых: {', '.join(missing)}.",
                [ordered[advanced[0]].id],
                f"missing basic groups: {missing}",
                FixBy.HUMAN,
            )
        )
    return out


def check_v36(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    names = _Names(kit)
    seminars = kit.seminars
    patterns_src = kit.request.audience.domain_patterns or kit.program.audience.domain_patterns
    out: list[Finding] = []
    if patterns_src and seminars:
        patterns = [_compile(p) for p in patterns_src if p.strip()]
        misses = [s for s in seminars if not any(p.search(_seminar_text(s)) for p in patterns)]
        hits = len(seminars) - len(misses)
        if hits / len(seminars) + EPS < ctx.domain_task_share:
            domain = kit.request.audience.domain or kit.program.audience.domain or "среда слушателей"
            out.append(
                Finding(
                    ERROR,
                    f"Только {hits} из {len(seminars)} заданий построены на ситуациях из профессиональной среды "
                    f"слушателей ({domain}); нужно не меньше {ctx.domain_task_share:.0%}. Не из среды: "
                    f"{_list(names(s.id) for s in misses)}.",
                    [s.id for s in misses],
                    f"domain share {hits}/{len(seminars)}",
                    FixBy.LLM,
                )
            )
    # вид работы из запроса засчитывается и итоговой аттестацией: «в конце каждый защищает проект — игру или
    # чат-бота» — это итоговый проект, а не отдельное задание (ложная ошибка в прогоне 21.10)
    works = [_seminar_text(s) for s in seminars] + [_fa_text(kit)]
    for req in kit.request.constraints.required_seminar_types:
        patterns = _requirement_patterns(req)
        if not any(any(p.search(text) for p in patterns) for text in works if text):
            out.append(
                Finding(
                    ERROR,
                    f"Нет задания типа «{req.label}», которое требуется в запросе.",
                    detail=f"required seminar type: {req.label}",
                    fix_by=FixBy.LLM,
                )
            )
    return out


def _grades(text: str) -> set[int]:
    result: set[int] = set()
    for match in GRADE_RE.finditer(text):
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) else first
        if first <= last <= first + 11:
            result |= set(range(first, last + 1))
    return result


def check_v37(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    requested = kit.request.audience
    audience = kit.program.audience
    text = " ".join(
        [
            audience.category,
            audience.age_or_grade or "",
            audience.requirements or "",
            kit.texts.audience_requirements,
        ]
    )
    out: list[Finding] = []
    if requested.age_or_grade:
        wanted = _grades(requested.age_or_grade)
        if wanted:
            have = _grades(text)
            if not wanted <= have:
                out.append(
                    Finding(
                        WARNING,
                        f"Категория обучающихся не соответствует запросу ({requested.age_or_grade}): в программе "
                        f"«{_short(text) or 'не указана'}».",
                        detail=f"grades {sorted(wanted)} vs {sorted(have)}",
                        fix_by=FixBy.LLM,
                        refs=("Q1.F1",),
                    )
                )
            if ADULT_RE.search(text):
                out.append(
                    Finding(
                        WARNING,
                        "Программа для школьников, а в категории обучающихся указаны взрослые.",
                        detail="adult wording for school audience",
                        fix_by=FixBy.LLM,
                        refs=("Q1.F1",),
                    )
                )
        else:
            numbers = set(re.findall(r"\d{1,2}", requested.age_or_grade))
            if numbers and not numbers <= set(re.findall(r"\d{1,2}", text)):
                out.append(
                    Finding(
                        WARNING,
                        f"Возраст обучающихся не соответствует запросу ({requested.age_or_grade}).",
                        detail="age mismatch",
                        fix_by=FixBy.LLM,
                        refs=("Q1.F1",),
                    )
                )
    expanded = _expand_abbr(text).lower().replace("ё", "е")
    for value, what, ref in (
        (requested.requirements, "требования к слушателям", "Q4.6"),
        (requested.category, "категория слушателей", "Q1.F1"),
    ):
        if not value:
            continue
        stems = _stems(_expand_abbr(value))
        if stems and sum(1 for s in stems if s in expanded) / len(stems) < 0.6:
            out.append(
                Finding(
                    WARNING,
                    f"В программе не учтено из запроса ({what}): «{_short(value)}».",
                    detail=f"stems {stems}",
                    fix_by=FixBy.LLM,
                    refs=(ref,),
                )
            )
    if kit.program.type in PK_TYPES and not EDU_RE.search(text):
        out.append(
            Finding(
                WARNING,
                "Для программы повышения квалификации в требованиях к слушателям нужно указать образование: "
                "среднее профессиональное или высшее (либо получающие его).",
                detail="pk audience without education",
                fix_by=FixBy.LLM,
                refs=("Q4.6",),
            )
        )
    return out


def check_v38(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    if not kit.request.constraints.no_programming:
        return []
    names = _Names(kit)
    fields: list[tuple[str, str, str]] = [
        ("", "требования к слушателям (паспорт)", kit.program.audience.requirements or ""),
        ("", "требования к слушателям", kit.texts.audience_requirements),
        ("", "входной уровень слушателей", kit.program.audience.entry_level or ""),
        ("", "цель программы", kit.program.goal),
    ]
    fields += [(o.id, names(o.id), o.text) for o in kit.outcomes]
    for topic in kit.topics:
        fields.append((topic.id, names(topic.id, title=False), topic.title))
        fields += [
            (topic.id, f"{names(topic.id)}, содержание", x) for items in topic.content.values() for x in items
        ]
    fields += [(s.id, names(s.id), _seminar_text(s)) for s in kit.seminars]
    fa = kit.final_attestation
    if fa is not None:
        fields += [("FA", "итоговая аттестация, требования", x) for x in fa.requirements]
        if fa.example_task is not None:
            fields.append(("FA", "итоговая аттестация, пример задания", fa.example_task.description))
    out: list[Finding] = []
    for target, where, text in fields:
        for sentence in re.split(r"[.;!?\n]+", text):
            match = PROGRAMMING_RE.search(sentence)
            if match and not NEGATION_RE.search(sentence):
                out.append(
                    Finding(
                        ERROR,
                        f"{where}: «{_short(sentence, 80)}» — по запросу программирование не требуется.",
                        [target] if target else [],
                        f"programming: {match.group(0)}",
                        FixBy.LLM,
                    )
                )
                break
    return _cap(out, ctx)


REAL_DATA_RE = re.compile(
    r"реальн\w*\s+(?:\w+\s+){0,2}данн\w*|доступ\w*\s+к\s+(?:\w+\s+){0,2}(?:сервер|баз)\w*", re.I
)


def check_v39(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    out: list[Finding] = []
    if kit.request.constraints.anonymized_data:
        for item in _texts_for_registry(kit):
            for sentence in re.split(r"(?<=[.!?;])\s+", item.text):
                match = REAL_DATA_RE.search(sentence)
                if (
                    match
                    and not ANON_RE.search(sentence)
                    and not re.search(r"\bбез\b|\bне\b", sentence[: match.start()])
                ):
                    out.append(
                        Finding(
                            WARNING,
                            f"{item.where}: «{_short(match.group(0), 40)}» — по запросу используются только обезличенные "
                            "учебные данные, без доступа к реальным системам и данным организации.",
                            [item.target] if item.target else [],
                            "real data with anonymized constraint",
                            FixBy.LLM,
                        )
                    )
                    break
    for item in _texts_for_registry(kit):
        for what, regex in PII_RES:
            match = regex.search(item.text)
            if match:
                out.append(
                    Finding(
                        WARNING,
                        f"{item.where}: похоже на персональные данные ({what}: «{_short(match.group(0), 40)}») — в "
                        "примерах используются только обезличенные данные.",
                        [item.target] if item.target else [],
                        f"pii: {what}",
                        FixBy.LLM,
                    )
                )
                break
    return _cap(out, ctx)


def check_v40(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    names = _Names(kit)
    fields: list[tuple[str, str, str]] = [
        ("", "цель программы", kit.program.goal),
        ("", "аннотация", kit.texts.annotation),
    ]
    fields += [("", "задачи программы", x) for x in kit.program.tasks]
    fields += [(o.id, names(o.id), o.text) for o in kit.outcomes]
    fields += [(c.id, names(c.id, title=False), c.title) for c in kit.competencies]
    if kit.final_attestation is not None:
        fields.append(
            ("FA", "итоговая аттестация, ожидаемый результат", kit.final_attestation.expected_result or "")
        )
    out: list[Finding] = []
    for target, where, text in fields:
        match = OVERPROMISE_RE.search(text)
        if match:
            out.append(
                Finding(
                    WARNING,
                    f"{where}: «{_short(match.group(0), 50)}» — обещание сверх того, что реально дать за "
                    f"{_fmt(kit.program.total_hours)} ч.",
                    [target] if target else [],
                    "overpromise",
                    FixBy.LLM,
                )
            )
    return out


def _ordered_lectures(kit: Kit) -> list[Lecture]:
    module_rank = {m.id: (m.order, m.id) for m in kit.modules}
    topic_rank = {t.id: (module_rank.get(t.module_id, (10**6, "")), t.order) for t in kit.topics}

    def key(lecture: Lecture) -> tuple:
        ranks = [topic_rank[t] for t in lecture.topic_ids if t in topic_rank]
        first = min(ranks) if ranks else (module_rank.get(lecture.module_id, (10**6, "")), 10**6)
        return (first, lecture.session_no or 0, lecture.id)

    return sorted(kit.lectures, key=key)


def _lecture_text(lecture: Lecture) -> str:
    parts = [lecture.title, lecture.goal]
    for item in lecture.plan:
        parts += [item.title, *item.theses]
    for slide in lecture.slides or []:
        parts += [slide.title, *slide.bullets, slide.notes]
    return " ".join(parts)


def _term_regex(term: str) -> re.Pattern[str]:
    term = term.strip()
    stem = term
    if re.fullmatch(r"[А-Яа-яЁё]{6,}", term):
        stem = term[:-2]
    return re.compile(r"(?<![\w])" + re.escape(stem), re.I)


def fix_glossary_order(kit: Kit) -> list[str]:
    """Термин глоссария — в лекции, где он впервые встречается (в порядке тем), без повторов в следующих лекциях.
    Лекции модулей генерируются параллельно, и модель не знает, что уже объяснено, — порядок наводит код.
    Возвращает ID лекций, у которых изменился глоссарий."""
    lectures = _ordered_lectures(kit)
    texts = {lec.id: _lecture_text(lec) for lec in lectures}
    by_id = {lec.id: lec for lec in lectures}
    seen: set[str] = set()
    changed: set[str] = set()
    for lecture in lectures:
        for entry in list(lecture.glossary):
            key = normalize_text(entry.term)
            if not key:
                continue
            if key in seen:
                lecture.glossary = [g for g in lecture.glossary if g is not entry]
                changed.add(lecture.id)
                continue
            seen.add(key)
            regex = _term_regex(entry.term)
            first = next(
                (lec for lec in lectures if lec.id == lecture.id or regex.search(texts[lec.id])), lecture
            )
            if first.id != lecture.id:
                lecture.glossary = [g for g in lecture.glossary if g is not entry]
                by_id[first.id].glossary = [*by_id[first.id].glossary, entry]
                changed |= {lecture.id, first.id}
    return sorted(changed)


def check_v41(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    names = _Names(kit)
    lectures = _ordered_lectures(kit)
    out: list[Finding] = []
    terms: dict[str, tuple[str, list[str]]] = {}
    definitions: dict[str, set[str]] = defaultdict(set)
    for lecture in lectures:
        for entry in lecture.glossary:
            key = normalize_text(entry.term)
            if not key:
                continue
            terms.setdefault(key, (entry.term, []))[1].append(lecture.id)
            if not entry.definition.strip():
                out.append(
                    Finding(
                        WARNING,
                        f"{names(lecture.id)}: у термина «{entry.term}» нет определения.",
                        [lecture.id],
                        "empty definition",
                        FixBy.LLM,
                    )
                )
            else:
                definitions[key].add(normalize_text(entry.definition))
    texts = {lec.id: _lecture_text(lec) for lec in lectures}
    for key, (term, defined_in) in terms.items():
        regex = _term_regex(term)
        first = next((lec for lec in lectures if lec.id in defined_in or regex.search(texts[lec.id])), None)
        if first is not None and first.id not in defined_in:
            out.append(
                Finding(
                    WARNING,
                    f"Термин «{term}» впервые встречается в {names(first.id)}, а поясняется только в "
                    f"{_list(names(x) for x in defined_in)}.",
                    [first.id, *defined_in],
                    "term explained late",
                    FixBy.LLM,
                )
            )
        if len(definitions[key]) > 1:
            out.append(
                Finding(
                    INFO,
                    f"Термин «{term}» определён по-разному в {_list(names(x) for x in defined_in)}.",
                    list(defined_in),
                    "term defined differently",
                    FixBy.LLM,
                )
            )
    return _cap(out, ctx)


@dataclass
class CopyStats:
    """Совпадение текстов комплекта с образцами по шинглам: всего шинглов, совпавших, по полям."""

    total: int = 0
    copied: int = 0
    fields: list[tuple[_Text, set, set]] = field(default_factory=list)  # (поле, его шинглы, совпавшие)

    @property
    def ratio(self) -> float:
        return self.copied / self.total if self.total else 0.0


def copy_stats(kit: Kit, references: Sequence[str]) -> CopyStats:
    """Доля дословных совпадений с образцами (V42 и отчёт методисту); общие формулировки из белого списка не
    считаются."""
    stats = CopyStats()
    references = [t for t in references if t and t.strip()]
    if not references:
        return stats
    ref_shingles: set[copycheck.Shingle] = set()
    for text in references:
        ref_shingles |= copycheck.shingles(text)
    white: set[copycheck.Shingle] = set()
    for text in copycheck.DEFAULT_WHITELIST:
        white |= copycheck.shingles(text)
    for item in _collect_texts(kit):
        if item.scope in ("source", "software", "term"):
            continue
        own = copycheck.shingles(item.text)
        if not own:
            continue
        hit = (own & ref_shingles) - white
        stats.total += len(own)
        stats.copied += len(hit)
        stats.fields.append((item, own, hit))
    return stats


def _copied_fields(stats: CopyStats, field_ratio: float) -> list[tuple[_Text, set, set]]:
    """Поля, почти дословно повторяющие образец: не меньше 3 совпавших шинглов и доля выше field_ratio."""
    return [
        (item, own, hit)
        for item, own, hit in stats.fields
        if len(hit) >= 3 and len(hit) / len(own) > field_ratio
    ]


def copy_summary(kit: Kit, references: Sequence[str], ctx: ValidationContext | None = None) -> CopySummary:
    """Антиплагиат для отчёта: доля текста, дословно совпадающего с приложенными образцами, против порога."""
    ctx = ctx or ValidationContext()
    references = [t for t in references if t and t.strip()]
    if not references:
        return CopySummary(references=0, ratio=0, threshold=ctx.copy_threshold)
    stats = copy_stats(kit, references)
    return CopySummary(
        references=len(references),
        ratio=round(stats.ratio, 4),
        threshold=ctx.copy_threshold,
        fields_flagged=len(_copied_fields(stats, ctx.copy_field_ratio)),
    )


def check_v42(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    out: list[Finding] = []
    reference = kit.request.reference
    if reference is not None and reference.program_type_guess is not None:
        ref_family = "dop" if reference.program_type_guess == ProgramType.DOP else "pk"
        if ref_family != _family(kit):
            out.append(
                Finding(
                    WARNING,
                    "Приложенный образец относится к другому типу программы — структура разделов взята из шаблона "
                    f"«{TYPE_NAMES[kit.program.type]}», а не из образца.",
                    detail=f"reference {reference.program_type_guess.value} vs {kit.program.type.value}",
                    fix_by=FixBy.HUMAN,
                    refs=("Q3.4a",),
                )
            )
    references = [t for t in ctx.reference_texts if t and t.strip()]
    if not references:
        return out
    stats = copy_stats(kit, references)
    copied, total = stats.copied, stats.total
    field_findings: list[Finding] = []
    for item, own, hit in _copied_fields(stats, ctx.copy_field_ratio):
        fragments = copycheck.copied_fragments(item.text, references, limit=1)
        field_findings.append(
            Finding(
                WARNING,
                f"{item.where}: текст почти дословно совпадает с образцом («{_short(fragments[0] if fragments else item.text, 80)}»).",
                [item.target] if item.target else [],
                f"field copy ratio {len(hit) / len(own):.2f}",
                FixBy.LLM,
                refs=("K12", "Q3.4b"),
            )
        )
    if total and copied / total > ctx.copy_threshold:
        out.append(
            Finding(
                ERROR,
                f"{copied / total:.0%} текста комплекта дословно совпадает с образцом (порог {ctx.copy_threshold:.0%}).",
                detail=f"copied shingles {copied}/{total}",
                fix_by=FixBy.LLM,
                refs=("K12", "Q3.4b"),
            )
        )
    return _cap(out + field_findings, ctx)


def _trigrams(text: str) -> set[str]:
    norm = f" {normalize_text(text)} "
    return {norm[i : i + 3] for i in range(len(norm) - 2)}


def _similarity(a: str, b: str) -> float:
    ta, tb = _trigrams(a), _trigrams(b)
    union = ta | tb
    return len(ta & tb) / len(union) if union else 0.0


def check_v43(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    names = _Names(kit)
    out: list[Finding] = []
    for group, what in ((kit.topics, "Темы"), (kit.modules, "Модули")):
        by_title: dict[str, list[str]] = defaultdict(list)
        for obj in group:
            by_title[normalize_text(obj.title)].append(obj.id)
        for ids in by_title.values():
            if len(ids) > 1:
                out.append(
                    Finding(
                        ERROR,
                        f"{what} повторяются: {_list(names(x) for x in ids)}.",
                        ids,
                        "duplicate titles",
                        FixBy.HUMAN,
                        refs=("Q5.9b", "K13"),
                    )
                )
    topics = kit.topics
    for i, first in enumerate(topics):
        for second in topics[i + 1 :]:
            if normalize_text(first.title) == normalize_text(second.title):
                continue
            score = _similarity(first.title, second.title)
            if score >= ctx.topic_similarity_threshold:
                out.append(
                    Finding(
                        WARNING,
                        f"Темы почти совпадают по названию: {names(first.id)} и {names(second.id)}.",
                        [first.id, second.id],
                        f"similarity {score:.2f}",
                        FixBy.HUMAN,
                        refs=("K13", "Q5.9b"),
                    )
                )
    out += _outcome_spread(kit, names)
    for topic in topics:
        seen: dict[str, str] = {}
        for items in topic.content.values():
            for item in items:
                key = normalize_text(item)
                if key and key in seen:
                    out.append(
                        Finding(
                            INFO,
                            f"{names(topic.id)}: пункт содержания «{_short(item, 60)}» повторяется.",
                            [topic.id],
                            "duplicate content item",
                            FixBy.LLM,
                            refs=("F2",),
                        )
                    )
                seen.setdefault(key, item)
    for group, what in ((kit.questions, "stem"), (kit.seminars, "title")):
        by_text: dict[str, list[str]] = defaultdict(list)
        for obj in group:
            by_text[normalize_text(getattr(obj, what))].append(obj.id)
        for ids in by_text.values():
            if len(ids) > 1:
                out.append(
                    Finding(
                        WARNING,
                        f"Повторяются формулировки: {_list(names(x) for x in ids)}.",
                        ids,
                        f"duplicate {what}",
                        FixBy.LLM,
                        refs=("K13",),
                    )
                )
    return _cap(out, ctx)


def _outcome_spread(kit: Kit, names: _Names) -> list[Finding]:
    """Один РО раскрывается в темах трёх и более модулей (и не меньше половины модулей) — содержание модулей
    повторяется, у них нет своих результатов (живой прогон 20.10: РО в 5 модулях из 6)."""
    modules_total = len(kit.modules)
    out: list[Finding] = []
    for outcome in kit.outcomes:
        modules = list(dict.fromkeys(t.module_id for t in kit.topics if outcome.id in t.lo_ids))
        if len(modules) >= 3 and len(modules) * 2 >= modules_total:
            out.append(
                Finding(
                    WARNING,
                    f"{names(outcome.id)} раскрывается в темах {len(modules)} модулей из {modules_total} "
                    f"({_list(names(m) for m in modules)}): содержание модулей повторяется. Оставьте результат в одном-двух "
                    "модулях, у остальных — свои результаты.",
                    [outcome.id, *modules],
                    f"outcome in {len(modules)} modules",
                    FixBy.HUMAN,
                    refs=("K13", "Q5.9b"),
                )
            )
    return out


def check_v44(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    out: list[Finding] = []
    own_title = normalize_text(kit.program.title)
    foreign = [
        (t, re.compile(re.escape(t), re.I))
        for t in ctx.foreign_titles
        if t and normalize_text(t) not in own_title
    ]
    for item in _texts_for_registry(kit):
        target = [item.target] if item.target else []
        if LR_RE.search(item.text):
            out.append(
                Finding(
                    WARNING,
                    f"{item.where}: обозначение «ЛР» — лабораторные работы везде обозначаются «ЛБ».",
                    target,
                    "ЛР",
                    FixBy.CODE,
                    refs=("F1",),
                )
            )
        broken = BROKEN_WORD_RE.search(item.text)
        if broken:
            out.append(
                Finding(
                    WARNING,
                    f"{item.where}: «{broken.group(0)}» — похоже на опечатку (цифры внутри слова).",
                    target,
                    "broken word",
                    FixBy.LLM,
                    refs=("F3",),
                )
            )
        for title, regex in foreign:
            if regex.search(item.text):
                out.append(
                    Finding(
                        WARNING,
                        f"{item.where}: упомянута чужая программа «{title}».",
                        target,
                        "foreign title",
                        FixBy.LLM,
                        refs=("F8",),
                    )
                )
        if item.text.count("«") != item.text.count("»"):
            out.append(
                Finding(
                    INFO, f"{item.where}: непарные кавычки «».", target, "quotes", FixBy.LLM, refs=("F3",)
                )
            )
    return _cap(out, ctx)


# --------------------------------------------------------------------------------------------------
# V45: ошибки приложенного образца (сырые числа ParsedReference)
# --------------------------------------------------------------------------------------------------

REF_TOTAL_VS_SECTIONS = "ref_total_vs_sections"
REF_TOTAL_VS_TOPICS = "ref_total_vs_topics"
REF_SECTION_VS_TOPICS = "ref_section_vs_topics"
REF_ROW_SUM = "ref_row_sum"
REF_EMPTY_ROW = "ref_empty_row"
REF_STEP = "ref_step"
REF_PLAN_VS_UTP = "ref_plan_vs_utp"
REF_LABEL_HOURS = "ref_label_hours"
REF_WP_VS_UTP = "ref_wp_vs_utp"
REF_CONTROL_HOURS = "ref_control_hours"
REF_LR_LB = "ref_lr_lb"
REF_INTERNSHIP_CONTACT = "ref_internship_contact"
REF_REQUISITES = "ref_requisites"
REF_NORM_OUTDATED = "ref_norm_outdated"
REF_NORM_DATE = "ref_norm_date"

TABLE_WHERE = {
    "plan": "В учебном плане образца",
    "utp": "В учебно-тематическом плане образца",
    "dop_plan": "В учебном плане образца",
    "other": "В таблице часов образца",
}
COLUMN_NAMES = {
    "total": "трудоёмкость",
    "contact": "аудиторные",
    "lk": "ЛК",
    "pr": "ПР",
    "lb": "ЛБ",
    "srs": "СРС",
}
NUMERIC_COLUMNS = ("total", "contact", "lk", "pr", "lb", "srs")
REF_KIND_COLUMNS = ("lk", "pr", "lb", "srs")


def _ref(code: str, message: str, refs: Sequence[str], detail: str = "") -> Finding:
    return Finding(
        WARNING,
        message,
        detail=f"[{code}] {detail}".strip(),
        fix_by=FixBy.HUMAN,
        refs=("Q5.3b", *refs),
    )


def _rv(row: RefHoursRow, column: str) -> float:
    value = row.values.get(column)
    return 0.0 if value is None else float(value)


def _has(row: RefHoursRow, column: str) -> bool:
    return row.values.get(column) is not None


def _row_label(row: RefHoursRow) -> str:
    number = (row.number or "").strip().rstrip(".")
    title = _short(row.title, 50)
    if row.row_type == "total":
        return "Итого"
    if row.row_type == "fa":
        return "Итоговая аттестация"
    return f"{number} {title}".strip() or "без названия"


def _diff_text(diffs: list[tuple[str, float, float]], left: str, right: str) -> str:
    return "; ".join(f"{COLUMN_NAMES[c]} — {left} {_fmt(a)}, {right} {_fmt(b)}" for c, a, b in diffs)


def _ref_table_findings(table: RefHoursTable) -> list[Finding]:
    columns = list(table.columns) or sorted({c for r in table.rows for c in r.values})
    kinds = [c for c in REF_KIND_COLUMNS if c in columns]
    numeric = [c for c in NUMERIC_COLUMNS if c in columns]
    where = TABLE_WHERE.get(table.kind, TABLE_WHERE["other"])
    has_contact = "contact" in columns
    out: list[Finding] = []

    def kinds_sum(row: RefHoursRow) -> float:
        if has_contact:
            return _rv(row, "contact") + _rv(row, "srs")
        return sum(_rv(row, k) for k in kinds)

    for row in table.rows:
        label = _row_label(row)
        bad_step = [c for c in numeric if _has(row, c) and not _on_step(_rv(row, c))]
        if bad_step:
            out.append(
                _ref(
                    REF_STEP,
                    f"{where} строка «{label}»: часы не кратны 0,5 ({', '.join(f'{COLUMN_NAMES[c]} {_fmt(_rv(row, c))}' for c in bad_step)}).",
                    ("G1h",),
                    label,
                )
            )
        if row.row_type not in ("section", "topic", "control") or "total" not in columns or not kinds:
            continue
        total = _rv(row, "total")
        parts = [c for c in (["contact", *kinds] if has_contact else kinds)]
        if total > EPS and all(_rv(row, c) <= EPS for c in parts):
            out.append(
                _ref(
                    REF_EMPTY_ROW,
                    f"{where} строка «{label}»: {_fmt(total)} ч, а по видам работы везде 0 — часы не распределены "
                    "(текущий контроль нужно учитывать внутри часов тем).",
                    ("G1f",),
                    label,
                )
            )
            continue
        if has_contact:
            contact = _rv(row, "contact")
            inner = sum(_rv(row, k) for k in kinds if k != "srs")
            if not _eq(contact, inner):
                out.append(
                    _ref(
                        REF_ROW_SUM,
                        f"{where} строка «{label}»: аудиторных {_fmt(contact)} ч, а лекции + практика = {_fmt(inner)} ч.",
                        ("G1a",),
                        label,
                    )
                )
        if not _eq(total, kinds_sum(row)):
            formula = "аудиторные + СР" if has_contact else "ЛК + ПР + ЛБ + СРС"
            out.append(
                _ref(
                    REF_ROW_SUM,
                    f"{where} строка «{label}»: трудоёмкость {_fmt(total)} ч, а {formula} = {_fmt(kinds_sum(row))} ч.",
                    ("G1a",),
                    label,
                )
            )

    groups: list[tuple[RefHoursRow, list[RefHoursRow]]] = []
    loose: list[RefHoursRow] = []
    for row in table.rows:
        if row.row_type == "section":
            groups.append((row, []))
        elif row.row_type in ("topic", "control"):
            (groups[-1][1] if groups else loose).append(row)
    fa_rows = [r for r in table.rows if r.row_type == "fa"]
    total_rows = [r for r in table.rows if r.row_type == "total"]

    for section, children in groups:
        if not children:
            continue
        diffs = [
            (c, _rv(section, c), sum(_rv(ch, c) for ch in children))
            for c in numeric
            if not _eq(_rv(section, c), sum(_rv(ch, c) for ch in children))
        ]
        if diffs:
            out.append(
                _ref(
                    REF_SECTION_VS_TOPICS,
                    f"{where} строка раздела «{_row_label(section)}» не равна сумме своих тем: "
                    f"{_diff_text(diffs, 'в строке раздела', 'сумма тем')}.",
                    ("G1b",),
                    _row_label(section),
                )
            )
    if not total_rows:
        return out
    total_row = total_rows[-1]
    section_rows = [g[0] for g in groups]
    base = section_rows + loose if section_rows else loose
    fa_text = " и итоговой аттестации" if fa_rows else ""
    diffs = []
    for c in numeric:
        expected = sum(_rv(r, c) for r in base) + sum(_rv(r, c) for r in fa_rows)
        if not _eq(_rv(total_row, c), expected):
            diffs.append((c, _rv(total_row, c), expected))
    if diffs:
        what = "разделов" if section_rows else "строк"
        out.append(
            _ref(
                REF_TOTAL_VS_SECTIONS,
                f"{where} строка «Итого» не равна сумме {what}{fa_text}: "
                f"{_diff_text(diffs, 'в «Итого»', f'сумма {what}{fa_text}')}.",
                ("G1c",),
                "total row",
            )
        )
    topic_rows = [ch for _, children in groups for ch in children] + loose
    if section_rows and topic_rows:
        diffs = []
        for c in numeric:
            expected = sum(_rv(r, c) for r in topic_rows) + sum(_rv(r, c) for r in fa_rows)
            if not _eq(_rv(total_row, c), expected):
                diffs.append((c, _rv(total_row, c), expected))
        if diffs:
            out.append(
                _ref(
                    REF_TOTAL_VS_TOPICS,
                    f"{where} сумма по темам{fa_text} не сходится с «Итого»: "
                    f"{_diff_text(diffs, 'в «Итого»', f'сумма тем{fa_text}')}.",
                    ("G1c",),
                    "total vs topics",
                )
            )
    if kinds and "total" in columns:
        fa_only = sum(_rv(r, "total") - kinds_sum(r) for r in fa_rows)
        expected = kinds_sum(total_row) + fa_only
        if not _eq(_rv(total_row, "total"), expected):
            out.append(
                _ref(
                    REF_ROW_SUM,
                    f"{where} в строке «Итого» трудоёмкость {_fmt(_rv(total_row, 'total'))} ч, а сумма видов работы "
                    f"с итоговой аттестацией — {_fmt(expected)} ч.",
                    ("G1a",),
                    "total row sum",
                )
            )
    return out


def _first_table(ref: ParsedReference, kind: str) -> RefHoursTable | None:
    return next((t for t in ref.hour_tables if t.kind == kind), None)


def _ref_plan_vs_utp(ref: ParsedReference) -> list[Finding]:
    plan, utp = _first_table(ref, "plan"), _first_table(ref, "utp")
    if plan is None or utp is None:
        return []
    columns = [c for c in NUMERIC_COLUMNS if c in plan.columns and c in utp.columns]
    out: list[Finding] = []
    for row_type in ("section", "total"):
        left = [r for r in plan.rows if r.row_type == row_type]
        right = [r for r in utp.rows if r.row_type == row_type]
        for a, b in zip(left, right, strict=False):
            diffs = [(c, _rv(a, c), _rv(b, c)) for c in columns if not _eq(_rv(a, c), _rv(b, c))]
            if diffs:
                out.append(
                    _ref(
                        REF_PLAN_VS_UTP,
                        f"Строка «{_row_label(a)}» в учебном плане образца не совпадает с учебно-тематическим планом: "
                        f"{_diff_text(diffs, 'в УП', 'в УТП')}.",
                        ("G1b",),
                        row_type,
                    )
                )
    return out


def _topic_key(number: str | None) -> str | None:
    if not number:
        return None
    number = re.sub(r"^\s*(?:тема|раздел)\s*", "", number, flags=re.I)
    return number.strip().rstrip(".") or None


def _is_control(item: RefWorkItem) -> bool:
    label = f"{item.kind_label} {item.topic_title}"
    return item.kind is None and bool(CONTROL_LABEL_RE.search(label)) and "итогов" not in label.lower()


def _ref_workprogram_findings(ref: ParsedReference) -> list[Finding]:
    out: list[Finding] = []
    for item in ref.workprogram:
        if (
            item.label_hours is not None
            and item.column_hours is not None
            and not _eq(item.label_hours, item.column_hours)
        ):
            topic = _topic_key(item.topic_number) or _short(item.topic_title, 40)
            out.append(
                _ref(
                    REF_LABEL_HOURS,
                    f"Рабочая программа образца, тема {topic}: в подписи «{item.kind_label} ({_fmt(item.label_hours)} ч)», "
                    f"а в колонке часов {_fmt(item.column_hours)}.",
                    ("G1e",),
                    f"{topic} {item.kind_label}",
                )
            )
    utp = _first_table(ref, "utp")
    if utp is None or not ref.workprogram:
        return out
    kinds = [c for c in REF_KIND_COLUMNS if c in utp.columns]
    utp_topics = {_topic_key(r.number): r for r in utp.rows if r.row_type == "topic" and _topic_key(r.number)}
    sums: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    control_by_topic: dict[str, float] = defaultdict(float)
    control_parts: list[float] = []
    for item in ref.workprogram:
        hours = item.column_hours if item.column_hours is not None else item.label_hours
        if hours is None:
            continue
        key = _topic_key(item.topic_number)
        if item.kind is not None:
            if key:
                sums[key][item.kind.value] += hours
        elif _is_control(item):
            control_parts.append(hours)
            if key:
                control_by_topic[key] += hours
    for key, by_kind in sums.items():
        row = utp_topics.get(key)
        if row is None:
            continue
        diffs = [
            (k, by_kind.get(k, 0.0), _rv(row, k)) for k in kinds if not _eq(by_kind.get(k, 0.0), _rv(row, k))
        ]
        if diffs:
            extra = ""
            if control_by_topic.get(key):
                extra = f" ({_fmt(control_by_topic[key])} ч вынесены в «Текущий контроль» внутри темы)"
            out.append(
                _ref(
                    REF_WP_VS_UTP,
                    f"Тема {key} образца: часы в рабочей программе не совпадают с учебно-тематическим планом — "
                    f"{_diff_text(diffs, 'в рабочей программе', 'в УТП')}{extra}.",
                    ("G1d",),
                    key,
                )
            )
    utp_control = sum(_rv(r, "total") for r in utp.rows if r.row_type == "control")
    wp_control = sum(control_parts)
    if (control_parts or utp_control > EPS) and not _eq(wp_control, utp_control):
        parts = " + ".join(_fmt(x) for x in control_parts)
        detail = f" ({parts})" if len(control_parts) > 1 else ""
        out.append(
            _ref(
                REF_CONTROL_HOURS,
                f"Текущий контроль в образце: в рабочей программе {_fmt(wp_control)} ч{detail}, а в "
                f"учебно-тематическом плане {_fmt(utp_control)} ч.",
                ("G1d",),
                "control",
            )
        )
    return out


def _ref_label_findings(ref: ParsedReference) -> list[Finding]:
    labels = {x.strip().upper() for x in ref.work_kind_labels}
    for item in ref.workprogram:
        match = WP_ABBR_RE.match(item.kind_label or "")
        if match:
            labels.add(match.group(1).upper())
    has_lb = "ЛБ" in labels or any("lb" in t.columns for t in ref.hour_tables)
    if "ЛР" in labels and has_lb:
        return [
            _ref(
                REF_LR_LB,
                "В учебном плане образца лабораторные работы обозначены «ЛБ», а в рабочей программе — «ЛР». "
                "В новой программе используется одно обозначение — «ЛБ».",
                ("F1",),
                "ЛР/ЛБ",
            )
        ]
    return []


def _ref_internship_findings(ref: ParsedReference) -> list[Finding]:
    if ref.program_type_guess != ProgramType.PK_INTERNSHIP:
        return []
    offenders: list[str] = []
    for table in ref.hour_tables:
        for row in table.rows:
            if row.row_type in ("section", "topic", "control") and any(
                _rv(row, k) > EPS for k in ("lk", "pr", "lb")
            ):
                label = _row_label(row)
                if label not in offenders:
                    offenders.append(label)
    if not offenders:
        return []
    return [
        _ref(
            REF_INTERNSHIP_CONTACT,
            f"Образец — стажировка, но в учебно-тематическом плане есть аудиторные часы (ЛК, ПР, ЛБ) в строках "
            f"{_list(offenders)}. У стажировки вся работа самостоятельная: ЛК, ПР и ЛБ равны 0 во всех строках.",
            ("Q5.2",),
            "internship contact",
        )
    ]


def _ref_norm_findings(ref: ParsedReference, registry: RegistryView) -> list[Finding]:
    out: list[Finding] = []
    norms = registry.norms()
    for text in ref.order_refs:
        for match in NUMBER_RE.finditer(text):
            number = _norm_number(match.group(1))
            record = next((n for n in norms if n.number and _norm_number(n.number) == number), None)
            if record is None:
                continue
            if not record.in_force:
                replacement = registry.norm(record.replaced_by) if record.replaced_by else None
                tail = f"; действует {_norm_short(replacement)}" if replacement is not None else ""
                out.append(
                    _ref(
                        REF_NORM_OUTDATED,
                        f"Образец ссылается на «{_short(text, 80)}» — акт утратил силу{tail}.",
                        ("F4",),
                        record.id,
                    )
                )
            dates = [f"{y}-{m}-{d}" for d, m, y in DATE_RE.findall(text)]
            if dates and record.date and record.date not in dates:
                out.append(
                    _ref(
                        REF_NORM_DATE,
                        f"Образец: «{_short(text, 80)}» — по реестру дата акта {_iso_to_ru(record.date)}.",
                        ("F5",),
                        record.id,
                    )
                )
    return out


def _reference_findings(ref: ParsedReference, registry: RegistryView | None = None) -> list[Finding]:
    out: list[Finding] = []
    for table in ref.hour_tables:
        if table.kind in ("plan", "utp", "dop_plan"):
            out += _ref_table_findings(table)
    out += _ref_plan_vs_utp(ref)
    out += _ref_workprogram_findings(ref)
    out += _ref_label_findings(ref)
    out += _ref_internship_findings(ref)
    if ref.requisites_filled:
        # сами реквизиты образца не цитируем: отчёт уходит вместе с комплектом (правило 4 — реквизиты пустые)
        out.append(
            _ref(
                REF_REQUISITES,
                "В образце заполнены номер и дата приказа об утверждении — в новой программе реквизиты приказа "
                "остаются пустыми (____).",
                ("G5a",),
                "requisites",
            )
        )
    if registry is not None:
        out += _ref_norm_findings(ref, registry)
    return out


def validate_reference(ref: ParsedReference, registry: RegistryView | None = None) -> list[Issue]:
    """V45: ошибки приложенного образца на его сырых числах. Пайплайн показывает их методисту."""
    refs = CHECKS["V45"].checklist_refs
    return [_to_issue("V45", n, f, refs) for n, f in enumerate(_reference_findings(ref, registry), start=1)]


def check_v45(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    if kit.request.reference is None:
        return []
    return _reference_findings(kit.request.reference, ctx.registry)


# --------------------------------------------------------------------------------------------------
# V46 и предупреждения методисту
# --------------------------------------------------------------------------------------------------


def _schedule_gap(kit: Kit, ctx: ValidationContext) -> tuple[int, int] | None:
    """(желаемый срок, расчётный срок) в неделях, если расчётный не укладывается в желаемый с допуском."""
    target = _weeks_target(kit)
    if not target:
        return None
    if kit.calendar is not None and kit.calendar.days:
        weeks = max(d.week for d in kit.calendar.days)
    else:
        per_day = kit.request.hours_per_day_max or kit.program.schedule.hours_per_day_max
        if not per_day:
            return None
        days = math.ceil(kit.program.total_hours / per_day - EPS)
        weeks = math.ceil(days / kit.program.schedule.max_days_per_week)
    return (target, weeks) if weeks > target * (1 + ctx.weeks_tolerance) + EPS else None


def _constraint_conflict(kit: Kit) -> bool:
    share = kit.request.constraints.min_practice_share
    return bool(share) and _internship(kit)


def _act_name(record: NormRecord) -> str:
    """Название акта без заголовка в кавычках: «Приказ Минобрнауки России от 24.03.2025 № 266»."""
    head = record.title.split("«", 1)[0].strip().rstrip(",")
    return head if head and record.number and record.number in head else _upper_first(_norm_short(record))


def _mentioned_in(kit: Kit, record: NormRecord) -> tuple[str, ...]:
    """Где упоминается прежний акт: в тексте запроса, в приложенном образце (по номеру акта)."""
    if not record.number:
        return ()
    pattern = re.compile(r"№\s*" + re.escape(record.number) + r"(?![\w/-])", re.I)
    where = []
    if pattern.search(kit.request.raw_text):
        where.append("запросе")
    reference = kit.request.reference
    if reference is not None and any(pattern.search(text) for text in reference.order_refs):
        where.append("приложенном образце")
    return tuple(where)


def _norm_replacements(kit: Kit, registry: RegistryView | None) -> list[mw.NormReplacement]:
    """Действующие акты программы, которые заменили утратившие силу (по записям реестра)."""
    out: list[mw.NormReplacement] = []
    if registry is None:
        if kit.program.type in PK_TYPES:
            out.append(NORM_REPLACED_FALLBACK)
        return out
    for norm_id in kit.program.norm_ids:
        record = registry.norm(norm_id)
        if record is None or not record.replaces:
            continue
        old = registry.norm(record.replaces)
        if old is None:
            continue
        out.append(
            mw.NormReplacement(
                current=_act_name(record),
                old=_act_name(old),
                current_since=_iso_to_ru(record.in_force_since),
                old_until=_iso_to_ru(old.in_force_until),
                lost_word=_lost_force(old),
                source_url=record.source_url or old.source_url or "",
                mentioned_in=_mentioned_in(kit, old),
            )
        )
    if not out and kit.program.type in PK_TYPES:
        out.append(NORM_REPLACED_FALLBACK)
    return out


def _ref_code(finding: Finding) -> str:
    match = re.match(r"\[(\w+)\]", finding.detail)
    return match.group(1) if match else ""


def methodist_warnings(kit: Kit, ctx: ValidationContext | None = None) -> list[MethodistWarning]:
    """Предупреждения методисту по общим правилам (app/core/methodist_warnings.py) + переданные извне
    (ctx.extra_warnings от солвера и календаря, schedule_tight и constraint_conflict из прошлого отчёта)."""
    ctx = ctx or ValidationContext()
    result: list[MethodistWarning] = []
    reference = kit.request.reference
    if reference is not None:
        findings = [
            mw.SampleFinding(_ref_code(f), f.message) for f in _reference_findings(reference, ctx.registry)
        ]
        warning = mw.reference_errors(reference.source_name, findings)
        if warning is not None:
            result.append(warning)
    match = EXPECTATION_RE.search(kit.request.raw_text)
    if match:
        phrase = mw.expectation_phrase(kit.request.raw_text, match.start(), match.end())
        result.append(mw.expectation_gap(kit, phrase))
    checks = kit.meta.literature_checks
    sources = mw.unverified_sources(kit, int(checks.get("rejected", 0)), int(checks.get("dropped", 0)))
    if sources is not None:
        result.append(sources)
    replaced = mw.norm_replaced(_norm_replacements(kit, ctx.registry))
    if replaced is not None:
        result.append(replaced)
    external = list(ctx.extra_warnings)
    if ctx.carry_over_warnings and kit.report is not None:
        # в отчётах прежних версий у них нет заголовка и вариантов — дополняем теми же умолчаниями, что и рендер
        external += [
            w.model_copy(update={"title": mw.title_of(w), "options": mw.options_of(w)})
            for w in kit.report.warnings_for_methodist
            if w.type in EXTERNAL_WARNING_TYPES
        ]
    seen = {(w.type, w.text) for w in result}
    for warning in external:
        key = (warning.type, warning.text)
        if key not in seen:
            seen.add(key)
            result.append(warning)
    return mw.ordered(result)


def check_v46(kit: Kit, ctx: ValidationContext) -> list[Finding]:
    warnings = methodist_warnings(kit, ctx)
    types = {w.type for w in warnings}
    out: list[Finding] = []
    match = EXPECTATION_RE.search(kit.request.raw_text)
    if match and WarningType.EXPECTATION_GAP not in types:
        out.append(
            Finding(
                ERROR,
                f"В запросе есть ожидание «{match.group(0)}», но в отчёте нет предупреждения о разрыве между этим "
                "ожиданием и реальными результатами программы.",
                detail="expectation_gap missing",
                fix_by=FixBy.LLM,
                refs=("Q4.9", "K8"),
            )
        )
    gap = _schedule_gap(kit, ctx)
    if gap and WarningType.SCHEDULE_TIGHT not in types:
        target, weeks = gap
        out.append(
            Finding(
                WARNING,
                f"Желаемый срок ({target} нед.) не выдерживается — по расчёту нужно {weeks} нед., но в отчёте нет "
                "предупреждения об этом.",
                detail=f"schedule_tight missing: {gap}",
                fix_by=FixBy.CODE,
                refs=("Q5.F1", "K8"),
            )
        )
    if _constraint_conflict(kit) and WarningType.CONSTRAINT_CONFLICT not in types:
        out.append(
            Finding(
                WARNING,
                "В запросе несовместимые требования: вся работа самостоятельная и при этом задана доля практики — "
                "в отчёте нет объяснения, как конфликт решён.",
                detail="constraint_conflict missing",
                fix_by=FixBy.CODE,
                refs=("K8",),
            )
        )
    if kit.request.reference is not None and WarningType.REFERENCE_ERRORS not in types:
        if _reference_findings(kit.request.reference, ctx.registry):
            out.append(
                Finding(
                    ERROR,
                    "В образце есть ошибки, но в отчёте нет предупреждения о них.",
                    detail="reference_errors missing",
                    fix_by=FixBy.CODE,
                    refs=("Q5.3b",),
                )
            )
    for warning in warnings:
        title = mw.title_of(warning)
        if not warning.text.strip():
            out.append(
                Finding(
                    WARNING,
                    f"Предупреждение методисту «{title}» без текста.",
                    detail=f"empty warning {warning.type.value}",
                    fix_by=FixBy.CODE,
                    refs=("K8",),
                )
            )
            continue
        missing = mw.missing_parts(warning)
        if missing:
            out.append(
                Finding(
                    WARNING,
                    f"Предупреждение методисту «{title}» без {_and_list(missing)}: методисту нужно видеть, что не "
                    "сходится в цифрах, и варианты решения.",
                    detail=f"incomplete warning {warning.type.value}: {', '.join(missing)}",
                    fix_by=FixBy.CODE,
                    refs=("K8",),
                )
            )
        jargon = mw.jargon_in(warning)
        if jargon:
            out.append(
                Finding(
                    WARNING,
                    f"В предупреждении методисту «{title}» служебное обозначение «{jargon}» — методисту оно "
                    "непонятно; нужны название или номер, как в программе.",
                    detail=f"jargon in warning {warning.type.value}: {jargon}",
                    fix_by=FixBy.CODE,
                    refs=("K8",),
                )
            )
    return out


def _and_list(items: Sequence[str]) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " и " + items[-1]


# --------------------------------------------------------------------------------------------------
# Реестр проверок и сборка отчёта
# --------------------------------------------------------------------------------------------------

CHECKS: dict[str, CheckSpec] = {
    spec.id: spec
    for spec in (
        CheckSpec("V01", "Сумма строки", check_v01, ("G1a",)),
        CheckSpec("V02", "Свёртка часов", check_v02, ("G1b", "G1c")),
        CheckSpec("V03", "Объём программы", check_v03, ("Q1.2", "Q2.2a", "Q3.1a", "Q4.5", "Q5.1")),
        CheckSpec("V04", "Рабочая программа = УТП", check_v04, ("G1d",)),
        CheckSpec("V05", "Числа часов в тексте", check_v05, ("G1e",)),
        CheckSpec("V06", "Пустые строки и шаг 0,5", check_v06, ("G1f", "G1h")),
        CheckSpec("V07", "Раздел «Объём программы»", check_v07, ("G1g",)),
        CheckSpec("V08", "Стажировка: только СРС", check_v08, ("Q5.1", "Q5.2")),
        CheckSpec("V09", "Доля практики", check_v09, ("Q1.2",)),
        CheckSpec("V10", "Очно / ДОТ", check_v10, ("Q2.2b",)),
        CheckSpec("V11", "Календарный график", check_v11, ("Q1.3", "Q1.F2", "Q3.1b", "Q5.4a", "Q5.4b")),
        CheckSpec("V12", "Разделы шаблона", check_v12, ("G2a", "G2b", "F7")),
        CheckSpec("V13", "Тип программы и документ", check_v13, ("Q1.1", "Q2.1", "Q4.5", "K11")),
        CheckSpec("V14", "Целостность ID", check_v14, ("G3c",)),
        CheckSpec("V15", "РО раскрыты в темах", check_v15, ("G3a",)),
        CheckSpec("V16", "РО проверяются оценочными средствами", check_v16, ("G3b", "Q5.9a")),
        CheckSpec("V17", "Карта компетенций", check_v17, ("K1", "K2")),
        CheckSpec("V18", "Тесты", check_v18, ("K3", "Q1.6")),
        CheckSpec("V19", "Задания семинаров", check_v19, ("K4",)),
        CheckSpec("V20", "Лекции и слайды", check_v20, ("K6", "Q1.7", "Q3.9a", "Q3.9b")),
        CheckSpec(
            "V21", "Итоговая аттестация по форме", check_v21, ("Q1.5a", "Q2.7a", "Q3.7a", "Q4.10a", "Q5.7a")
        ),
        CheckSpec("V22", "Собеседование", check_v22, ("Q5.6a",)),
        CheckSpec("V23", "Источники литературы", check_v23, ("G4a",)),
        CheckSpec("V24", "Нормативная база", check_v24, ("G4b", "K9", "F4", "F5", "F6")),
        CheckSpec("V25", "Профстандарты и ТФ", check_v25, ("G4c", "K10", "Q2.3", "Q3.5", "Q4.7", "Q5.8")),
        CheckSpec("V26", "ДОП без профстандартов", check_v26, ("Q1.9",)),
        CheckSpec("V27", "Номера и коды в тексте", check_v27, ("G4d",)),
        CheckSpec("V28", "Сервисы, доступные в РФ", check_v28, ("Q2.5a", "Q2.5b")),
        CheckSpec("V29", "Версии инструментов", check_v29, ("Q3.6a",)),
        CheckSpec("V30", "Реквизиты и ФИО пустые", check_v30, ("G5a", "G5b", "G5c")),
        CheckSpec("V32", "Русский язык", check_v32, ("G6c",)),
        CheckSpec("V33", "Обязательные темы", check_v33, ("Q2.4a", "Q3.2", "Q5.5")),
        CheckSpec("V34", "Вводный блок", check_v34, ("Q3.3",)),
        CheckSpec("V35", "Порядок тем", check_v35, ("Q1.4a",)),
        CheckSpec("V36", "Задания из среды аудитории", check_v36, ("Q2.6a", "Q3.8a")),
        CheckSpec("V37", "Аудитория", check_v37, ("Q1.F1", "Q4.6")),
        CheckSpec("V38", "Без программирования", check_v38, ("Q2.8a",)),
        CheckSpec("V39", "Нет персональных данных", check_v39, ("Q3.7b",)),
        CheckSpec("V40", "Без обещаний сверх меры", check_v40, ("Q4.8b",)),
        CheckSpec("V41", "Глоссарий", check_v41, ("Q1.8b",)),
        CheckSpec("V42", "Не копия референса", check_v42, ("K12", "Q3.4a", "Q3.4b", "F8")),
        CheckSpec("V43", "Нет дублей", check_v43, ("K13", "Q5.9b", "F2")),
        CheckSpec("V44", "Гигиена текста", check_v44, ("F1", "F3", "F8")),
        CheckSpec("V45", "Ошибки приложенного образца", check_v45, ("Q5.3b",)),
        CheckSpec("V46", "Отчёт и предупреждения", check_v46, ("K8", "Q4.9", "Q5.F1", "Q5.3b", "F4")),
    )
}


def _to_issue(check: str, n: int, finding: Finding, default_refs: Sequence[str]) -> Issue:
    return Issue(
        id=issue_id(check, n),
        check=check,
        severity=finding.severity,
        target_ids=list(dict.fromkeys(t for t in finding.target_ids if t)),
        message=finding.message,
        detail=finding.detail,
        checklist_refs=list(finding.refs if finding.refs is not None else default_refs),
        fix_by=finding.fix_by,
    )


# На стадии каркаса содержания ещё нет: эти проверки ему не адресованы
SKELETON_SKIP = frozenset({"V12", "V16", "V18", "V19", "V20", "V21", "V22", "V23", "V36", "V41"})


def _selected(only: Iterable[str] | None, stage: str = "kit") -> list[CheckSpec]:
    if only is None:
        skip = SKELETON_SKIP if stage == "skeleton" else frozenset()
        return [spec for spec in CHECKS.values() if spec.id not in skip]
    wanted = [c.strip().upper() for c in only]
    unknown = [c for c in wanted if c not in CHECKS]
    if unknown:
        raise ValueError(f"Неизвестные проверки: {', '.join(unknown)}")
    return [CHECKS[c] for c in CHECKS if c in wanted]


def validate(
    kit: Kit, ctx: ValidationContext | None = None, only: Iterable[str] | None = None
) -> ValidationReport:
    """Запускает проверки (все или из only) и собирает отчёт. Комплект не изменяется."""
    ctx = ctx or ValidationContext()
    issues: list[Issue] = []
    for spec in _selected(only, ctx.stage):
        try:
            findings = spec.fn(kit, ctx)
        except Exception as exc:  # noqa: BLE001 — сбой одной проверки не должен ронять пайплайн
            findings = [
                Finding(
                    WARNING,
                    f"Проверка «{spec.title}» не выполнена из-за внутренней ошибки.",
                    detail=f"{type(exc).__name__}: {exc}",
                    fix_by=FixBy.CODE,
                )
            ]
        issues += [_to_issue(spec.id, n, f, spec.checklist_refs) for n, f in enumerate(findings, start=1)]
    return ValidationReport(
        generated_at=datetime.now(timezone.utc),
        issues=issues,
        hours=hours_summary(kit),
        coverage=coverage_matrix(kit, ctx.min_assessments_per_lo),
        sources=source_summary(kit),
        sections=section_status(kit),
        copying=copy_summary(kit, ctx.reference_texts, ctx),
        warnings_for_methodist=methodist_warnings(kit, ctx),
    )
