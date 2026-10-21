"""Разбор запроса методиста кодом, без LLM: числа, тип программы, форма обучения, итоговая аттестация,
ограничения, слайды, название, противоречия.

Числа в RequestSpec попадают только отсюда — модель лишь подтверждает их цитатами (QuantityMention).
Все правила общие для любого запроса: никаких веток под конкретные открытые запросы.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from app.config import DEFAULT_LIMITS
from app.models import FAForm, ProgramType, RequestSpec, SlidesScope, StudyFormKind

FLAGS = re.IGNORECASE | re.UNICODE

# --------------------------------------------------------------------------------------------------
# Числа
# --------------------------------------------------------------------------------------------------

NUM_WORDS: dict[str, float] = {
    "один": 1,
    "одна": 1,
    "одно": 1,
    "одну": 1,
    "одного": 1,
    "одной": 1,
    "два": 2,
    "две": 2,
    "двух": 2,
    "дважды": 2,
    "три": 3,
    "трех": 3,
    "трижды": 3,
    "четыре": 4,
    "четырех": 4,
    "пять": 5,
    "пяти": 5,
    "шесть": 6,
    "шести": 6,
    "семь": 7,
    "семи": 7,
    "восемь": 8,
    "восьми": 8,
    "девять": 9,
    "девяти": 9,
    "десять": 10,
    "десяти": 10,
    "двенадцать": 12,
    "двенадцати": 12,
    "шестнадцать": 16,
    "шестнадцати": 16,
    "двадцать": 20,
    "двадцати": 20,
    "тридцать": 30,
    "тридцати": 30,
    "полтора": 1.5,
    "полутора": 1.5,
}
_WORDS = "|".join(sorted((re.escape(w) for w in NUM_WORDS), key=len, reverse=True))
NUM = rf"(\d+(?:[.,]\d+)?|\b(?:{_WORDS})\b)"
HOURS = r"(?:академическ\w*\s+|астрономическ\w*\s+|ак(?:ад)?\.\s*)?(?:час(?:а|ов|ам|ами|ах)?\b|ч\b\.?)"
_END = r"[^.;!?\n]"  # «в пределах предложения»

ACADEMIC_HOUR_MIN = 45
DAYS_PER_MONTH_WEEKS = 4.345  # недель в месяце


_UNITS = {
    "один": 1,
    "одна": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
}
_TEENS = {
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
}
_TENS = {
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "семьдесят": 70,
    "восемьдесят": 80,
    "девяносто": 90,
}
_HUNDREDS = {"сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500}


def _alt(words: dict[str, int]) -> str:
    return "|".join(sorted(words, key=len, reverse=True))


# сотни + десятки + единицы, сотни + единицы, десятки + единицы, «одиннадцать»… — минимум из двух слов или
# одно составное слово десятков/сотен; одиночные «два», «три» остаются словами (их понимает NUM_WORDS)
_COMPOUND = re.compile(
    rf"\b(?:({_alt(_HUNDREDS)})(?:\s+({_alt(_TENS)}|{_alt(_TEENS)}))?(?:\s+({_alt(_UNITS)}))?"
    rf"|({_alt(_TENS)})(?:\s+({_alt(_UNITS)}))?"
    rf"|({_alt(_TEENS)}))\b",
    re.IGNORECASE,
)
_ALL_NUMERALS = {**_UNITS, **_TEENS, **_TENS, **_HUNDREDS}


def _numerals_to_digits(text: str) -> str:
    """«семьдесят два» → «72» и пробелы до той же длины: позиции в тексте не сдвигаются."""

    def repl(match: re.Match[str]) -> str:
        value = sum(_ALL_NUMERALS[w.lower()] for w in match.groups() if w)
        span = match.group(0)
        digits = str(value)
        return digits + " " * (len(span) - len(digits)) if len(span) >= len(digits) else span

    return _COMPOUND.sub(repl, text)


def normalize(text: str) -> str:
    """Та же длина, что у исходного текста (замены 1:1, числа словами — цифрами с пробелами),
    поэтому позиции совпадают с исходным текстом."""
    text = text.replace("ё", "е").replace("Ё", "Е").replace("\u00a0", " ")
    return _numerals_to_digits(text)


def parse_num(raw: str | None) -> float | None:
    if raw is None:
        return None
    raw = normalize(raw).strip().lower()
    if raw in NUM_WORDS:
        return float(NUM_WORDS[raw])
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


def first_number(text: str) -> float | None:
    match = re.search(NUM, normalize(text), FLAGS)
    return parse_num(match.group(1)) if match else None


def round_half(value: float) -> float:
    return math.floor(value * 2 + 0.5) / 2


# --------------------------------------------------------------------------------------------------
# Факты из запроса
# --------------------------------------------------------------------------------------------------


@dataclass
class Mention:
    kind: str
    value: float
    start: int
    end: int
    text: str


@dataclass
class RequestFacts:
    total_hours: float | None = None
    total_candidates: list[float] = field(default_factory=list)
    hours_per_day: float | None = None
    hours_per_week: float | None = None
    session_hours: float | None = None
    sessions_per_week: int | None = None
    sessions_count: int | None = None
    weeks: int | None = None
    months: float | None = None
    duration_text: str | None = None
    fa_hours: float | None = None
    test_questions: int | None = None
    portfolio_items: int | None = None
    practice_share: float | None = None
    practice_hours: float | None = None
    explicit_types: list[ProgramType] = field(default_factory=list)
    retraining: bool = False
    audience_type: ProgramType | None = None
    school_audience: bool = False
    audience_phrase: str | None = None
    age_or_grade: str | None = None
    entry_level_stated: bool = False
    form_kind: StudyFormKind | None = None
    dot: bool = False
    eo: bool = False
    distance_share: float | None = None
    fa_form: FAForm | None = None
    fa_text: str | None = None
    ru_services_only: bool = False
    anonymized_data: bool = False
    all_srs: bool = False
    no_programming: bool = False
    title: str | None = None
    slides: SlidesScope | None = None
    mentions: list[Mention] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def get(self, what: str) -> float | None:
        return {
            "total_hours": self.total_hours,
            "weeks": self.weeks,
            "months": self.months,
            "hours_per_day": self.hours_per_day,
            "session_hours": self.session_hours,
            "sessions_per_week": self.sessions_per_week,
            "test_questions": self.test_questions,
            "fa_hours": self.fa_hours,
            "portfolio_items": self.portfolio_items,
        }.get(what)

    def overlaps(self, start: int, end: int) -> list[Mention]:
        return [m for m in self.mentions if m.start < end and start < m.end]


class _Scanner:
    """Ищет числа по шаблонам; найденный фрагмент «занят» и не достаётся следующим шаблонам."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.mentions: list[Mention] = []

    def free(self, start: int, end: int) -> bool:
        return all(m.end <= start or end <= m.start for m in self.mentions)

    def find(
        self,
        kind: str,
        pattern: str,
        convert: Callable[[float], float] = lambda v: v,
        reject: Callable[[re.Match[str]], bool] | None = None,
    ) -> list[Mention]:
        found: list[Mention] = []
        for match in re.finditer(pattern, self.text, FLAGS):
            start, end = match.start(1), match.end()
            value = parse_num(match.group(1))
            if value is None or not self.free(start, end) or (reject is not None and reject(match)):
                continue
            mention = Mention(kind, convert(value), start, end, self.text[match.start() : end])
            self.mentions.append(mention)
            found.append(mention)
        return found


def _sentence(text: str, pos: int) -> str:
    start = max(text.rfind(c, 0, pos) for c in ".!?\n") + 1
    ends = [i for i in (text.find(c, pos) for c in ".!?\n") if i != -1]
    end = min(ends) + 1 if ends else len(text)
    return text[start:end].strip()


def extract_facts(text: str, max_test_questions: int | None = None) -> RequestFacts:
    norm = normalize(text)
    facts = RequestFacts()
    scan = _Scanner(norm)
    _numbers(scan, facts, norm)
    cap = MAX_TEST_QUESTIONS if max_test_questions is None else max_test_questions  # Settings.limits
    if facts.test_questions and facts.test_questions > cap:
        facts.notes.append(
            f"Итоговый тест на {facts.test_questions} вопросов — слишком большой для одной программы и дневного бюджета "
            f"модели: собран тест на {cap} вопросов. Если нужно больше, добавьте их банком вопросов "
            "в системе дистанционного обучения."
        )
        facts.test_questions = cap
    facts.mentions = scan.mentions
    _program_type(facts, norm)
    _audience(facts, norm)
    _form(facts, norm)
    _final_attestation(facts, norm, text)
    _flags(facts, norm)
    facts.title = extract_title(text)
    facts.slides = extract_slides(norm)
    return facts


def _numbers(scan: _Scanner, facts: RequestFacts, norm: str) -> None:
    # нагрузка в день / в неделю
    day = scan.find(
        "hours_per_day", rf"{NUM}\s*{HOURS}\s*(?:(?:в|за)\s+(?:учебн\w+\s+)?день|/\s*день|ежедневно)"
    )
    facts.hours_per_day = day[0].value if day else None
    week = scan.find("hours_per_week", rf"{NUM}\s*{HOURS}\s*(?:(?:в|за)\s+неделю|/\s*неделю|еженедельно)")
    facts.hours_per_week = week[0].value if week else None

    # итоговая аттестация
    def not_volume(match: re.Match[str]) -> bool:
        return bool(re.search(r"программ|курс|объем|трудоемк", match.group(0), FLAGS))

    fa = scan.find(
        "fa_hours",
        rf"(?:итогов\w*\s+аттестаци\w*|\bИА\b){_END}{{0,40}}?{NUM}\s*{HOURS}",
        reject=not_volume,
    )
    fa += scan.find(
        "fa_hours",
        rf"{NUM}\s*{HOURS}\s+(?:отвод\w*\s+)?(?:на|для)\s+(?:итогов\w*\s+аттестаци|\bИА\b|защит|экзамен|зачет)",
    )
    facts.fa_hours = fa[0].value if fa else None

    # длина занятия
    session_ctx = r"(?:заняти\w*|урок\w*|встреч\w*|\bпар[аыу]?\b|лекци\w*|семинар\w*)"
    sess = scan.find("session_hours", rf"{session_ctx}{_END}{{0,30}}?\bпо\s+{NUM}\s*{HOURS}")
    sess += scan.find(
        "session_hours",
        rf"{session_ctx}{_END}{{0,30}}?\bпо\s+{NUM}\s*мин\w*",
        convert=lambda m: max(0.5, round_half(m / ACADEMIC_HOUR_MIN)),
    )
    sess += scan.find("session_hours", rf"{NUM}[-\s]?часов\w*\s+(?:заняти|урок|встреч)")
    word_sess = re.search(r"\b(одно|двух|трех|четырех)часов\w*\s+(?:заняти|урок|встреч)", norm, FLAGS)
    if not sess and word_sess:
        facts.session_hours = {"одно": 1.0, "двух": 2.0, "трех": 3.0, "четырех": 4.0}[
            word_sess.group(1).lower()
        ]
    else:
        facts.session_hours = sess[0].value if sess else None

    # занятий в неделю и всего
    per_week = scan.find("sessions_per_week", rf"{NUM}\s+(?:раз\w*|заняти\w*|встреч\w*|урок\w*)\s+в\s+неделю")
    per_week += scan.find("sessions_per_week", r"(\bдважды|\bтрижды)\s+в\s+неделю")
    if per_week:
        facts.sessions_per_week = int(per_week[0].value)
    elif re.search(r"(?:^|[^\w\s]|\s)раз\s+в\s+неделю", norm, FLAGS) and not re.search(
        rf"{NUM}\s+раз\w*\s+в\s+неделю", norm, FLAGS
    ):
        facts.sessions_per_week = 1
    count = scan.find("sessions_count", rf"{NUM}\s+(?:заняти\w*|урок\w*|встреч\w*)(?!\s+в\s+неделю)")
    facts.sessions_count = int(count[0].value) if count else None

    # число вопросов теста, материалов портфолио
    tq = scan.find("test_questions", rf"тест\w*{_END}{{0,40}}?\b(?:на|из|в)\s+{NUM}\s+(?:вопрос|задани)")
    tq += scan.find("test_questions", rf"{NUM}\s+вопрос\w*{_END}{{0,25}}?тест")
    facts.test_questions = int(tq[0].value) if tq else None
    pf = scan.find("portfolio_items", rf"портфолио{_END}{{0,20}}?\bиз\s+{NUM}\s+\w+")
    facts.portfolio_items = int(pf[0].value) if pf else None

    # практика: часы как минимум практики
    practice_h = scan.find(
        "practice_hours",
        rf"практи\w*{_END}{{0,40}}?(?:не\s+меньше|не\s+менее|минимум|хотя\s+бы|не\s+ниже|от)\s+{NUM}\s*{HOURS}",
    )
    facts.practice_hours = practice_h[0].value if practice_h else None

    # часть объёма (не весь объём): «из них 36 часов практики», «24 часа самостоятельной работы».
    # «72 часа очно» — это весь объём в очной форме, поэтому форма обучения сюда не входит.
    practice_part = scan.find("practice_hours", rf"{NUM}\s*{HOURS}\s+(?:практик|практическ)")
    if practice_part and facts.practice_hours is None:
        facts.practice_hours = practice_part[0].value
    scan.find(
        "partial_hours",
        rf"{NUM}\s*{HOURS}\s+(?:лекци|теори|самостоятельн|СРС|аудиторн|контактн|лаборатор)",
    )
    scan.find("partial_hours", rf"(?:из\s+них|в\s+том\s+числе|включая|из\s+которых)\s+{NUM}\s*{HOURS}")

    # недели и месяцы
    weeks = scan.find("weeks", rf"{NUM}\s*(?:-?х\s+)?недел\w*")
    weeks += scan.find("weeks", rf"{NUM}[-\s]?недельн\w*")
    approx = r"(?:около|примерно|приблизительно|порядка|где-то|почти|не\s+больше|не\s+более|до)"
    if weeks:
        facts.weeks = max(1, int(round(weeks[0].value)))
        phrase = re.search(rf"{approx}\s+(?:на\s+)?{NUM}\s*(?:-?х\s+)?недел\w*", norm, FLAGS)
        if phrase:
            facts.duration_text = norm[phrase.start() : phrase.end()]
    months = re.search(rf"(?:{approx}\s+)?(?:на\s+)?(?:{NUM}\s*(?:-?х\s+)?)?месяц\w*|полгода", norm, FLAGS)
    if months:
        if months.group(0).lower().endswith("полгода"):
            value = 6.0
        else:
            value = parse_num(months.group(1)) if months.group(1) else 1.0
        facts.months = value
        if facts.duration_text is None:
            facts.duration_text = text_span(norm, months)
        if facts.weeks is None and value:
            facts.weeks = max(1, int(round(value * DAYS_PER_MONTH_WEEKS)))
            facts.notes.append(
                f"Срок «{facts.duration_text}» понят как примерно {facts.weeks} {_weeks_word(facts.weeks)}; "
                f"это желаемый срок, а не объём в часах."
            )

    # прилагательное «72-часовая программа» и прочие «N часов» — кандидаты общего объёма
    total = scan.find("total_hours", rf"{NUM}[-\s]?часов(?:ая|ой|ую|ые|ых|ого)\b")

    def per_item(match: re.Match[str]) -> bool:
        before = norm[max(0, match.start(1) - 4) : match.start(1)].lower()
        return bool(re.search(r"\bпо\s+$", before))

    total += scan.find("total_hours", rf"{NUM}\s*{HOURS}", reject=per_item)
    candidates: list[float] = []
    for mention in total:
        if mention.value not in candidates:
            candidates.append(mention.value)
    facts.total_candidates = candidates
    facts.total_hours = candidates[0] if len(candidates) == 1 else None

    if facts.total_hours is None and not candidates and facts.sessions_count and facts.session_hours:
        facts.total_hours = facts.sessions_count * facts.session_hours
        facts.notes.append(
            f"Объём вычислен: {facts.sessions_count} занятий по {_fmt(facts.session_hours)} ч = {_fmt(facts.total_hours)} ч."
        )

    facts.practice_share = _practice_share(norm)
    if facts.practice_share is None and facts.practice_hours and facts.total_hours:
        facts.practice_share = min(1.0, facts.practice_hours / facts.total_hours)


def _weeks_word(n: int) -> str:
    rest = n % 100
    if 11 <= rest <= 14 or n % 10 in (0, 5, 6, 7, 8, 9):
        return "недель"
    return "неделя" if n % 10 == 1 else "недели"


def text_span(text: str, match: re.Match[str]) -> str:
    return text[match.start() : match.end()].strip()


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}".replace(".", ",")


_SHARE_WORDS = [
    (r"(?:двух|две)\s+трет(?:и|ей)\b", 2 / 3),
    (r"половин(?:а|ы|у|ой|е)\b", 0.5),
    (r"трет(?:ь|и|ью)\b", 1 / 3),
    (r"четверт(?:ь|и|ью)\b", 0.25),
]
_AT_LEAST = r"(?:не\s+меньше|не\s+менее|минимум|хотя\s+бы|не\s+ниже|от|≥)"


def _share_value(raw: str) -> float | None:
    raw = raw.lower()
    percent = re.match(r"(\d+(?:[.,]\d+)?)\s*(?:%|процент)", raw)
    if percent:
        return min(1.0, float(percent.group(1).replace(",", ".")) / 100)
    for pattern, value in _SHARE_WORDS:
        if re.match(pattern, raw):
            return value
    return None


def _practice_share(norm: str) -> float | None:
    share_rx = (
        r"((?:двух|две)\s+трет(?:и|ей)\b|половин(?:а|ы|у|ой|е)\b|трет(?:ь|и|ью)\b|четверт(?:ь|и|ью)\b"
        r"|\d+(?:[.,]\d+)?\s*(?:%|процент\w*))"
    )
    for pattern in (
        rf"практи\w*{_END}{{0,40}}?{_AT_LEAST}\s+{share_rx}",
        rf"{_AT_LEAST}\s+{share_rx}{_END}{{0,40}}?практи",
        rf"{share_rx}{_END}{{0,15}}?(?:—|-|:)?\s*практи",
    ):
        match = re.search(pattern, norm, FLAGS)
        if match:
            value = _share_value(match.group(1))
            if value is not None:
                return value
    return None


# --------------------------------------------------------------------------------------------------
# Тип программы и аудитория
# --------------------------------------------------------------------------------------------------

EXPLICIT_TYPES: list[tuple[ProgramType, str]] = [
    (ProgramType.PK_INTERNSHIP, r"стажировк\w*"),
    (ProgramType.DOP, r"общеразвива\w*"),
    (ProgramType.PK, r"повышени\w*\s+квалификаци\w*|\bДПП\s*ПК\b"),
]
RETRAINING = r"переподготовк\w*"
SCHOOL_CUES = (
    r"школьник\w*|\bученик\w*|\bучениц\w*|\b\d{1,2}\s*(?:-?(?:го|х|м|й)\s*)?(?:(?:и|-|–|—|,)\s*\d{1,2}\s*(?:-?х\s*)?)?класс\w*"
    r"|\bдет(?:ей|и|ям|ьми)\b|подростк\w*|дошкольник\w*"
)
ADULT_CUES = (
    r"сотрудник\w*|специалист\w*|\bучител\w*|педагог\w*|преподавател\w*|работник\w*|инженер\w*|менеджер\w*"
    r"|бухгалтер\w*|руководител\w*|\bврач\w*|юрист\w*|аналитик\w*|служащ\w*|методист\w*|медсестр\w*"
    r"|воспитател\w*|администратор\w*|экономист\w*|программист\w*|разработчик\w*|тестировщик\w*|кадров\w+\s+служб"
)
ENTRY_LEVEL_CUES = (
    r"без\s+опыта|без\s+подготовк|с\s+нуля|начинающ\w*|не\s+(?:знают|умеют|владеют|имеют|программир)|\bзнают\b"
    r"|\bумеют\b|владеют|работают\s+(?:в|с)\b|опыт\w*\s+(?:работы|программир|в\b)|уровн\w*\s+подготовк"
    r"|базов\w+\s+(?:знани|навык|уровн)|уже\s+(?:знают|умеют|изучал|работа)|знаком\w*\s+с\b"
)
TYPE_TITLES = {
    ProgramType.PK: "программа повышения квалификации",
    ProgramType.PK_INTERNSHIP: "программа повышения квалификации в форме стажировки",
    ProgramType.DOP: "дополнительная общеразвивающая программа",
}


def explicit_types(norm: str) -> list[ProgramType]:
    return [ptype for ptype, rx in EXPLICIT_TYPES if re.search(rx, norm, FLAGS)]


def classify_type_answer(answer: str) -> ProgramType | None:
    """Тип программы из ответа методиста (допускаются и сокращения: ПК, ДПП, ДОП)."""
    norm = normalize(answer)
    found = explicit_types(norm)
    if ProgramType.PK_INTERNSHIP in found:
        return ProgramType.PK_INTERNSHIP
    if ProgramType.DOP in found:
        return ProgramType.DOP
    if ProgramType.PK in found:
        return ProgramType.PK
    if re.search(r"\bДОП\b|общеобразоват\w*|сертификат", norm, FLAGS):
        return ProgramType.DOP
    if re.search(r"\bПК\b|\bДПП\b|удостоверени\w*|квалификаци\w*", norm, FLAGS):
        return ProgramType.PK
    return None


def _program_type(facts: RequestFacts, norm: str) -> None:
    facts.explicit_types = explicit_types(norm)
    facts.retraining = bool(re.search(RETRAINING, norm, FLAGS))


def _audience(facts: RequestFacts, norm: str) -> None:
    # «для учителей начальных классов о работе с детьми» → «учителей начальных классов»: кто учится, а не о чём курс
    phrases = [
        re.split(
            r"\s(?:о|об|обо|по|про|на|с|со|в|во|чтобы|которые|которых|так\s+как)\s", m.group(1), flags=FLAGS
        )[0].strip()
        for m in re.finditer(r"\bдля\s+([^.;:!?\n]{3,120})", norm, FLAGS)
    ]
    scope = " ".join(phrases) if phrases else norm
    school = bool(re.search(SCHOOL_CUES, scope, FLAGS))
    adult = bool(re.search(ADULT_CUES, scope, FLAGS))
    if not phrases or not (school or adult):
        school = school or bool(re.search(SCHOOL_CUES, norm, FLAGS))
        adult = adult or bool(re.search(ADULT_CUES, norm, FLAGS))
    facts.school_audience = school and not adult
    if school != adult:
        facts.audience_type = ProgramType.DOP if school else ProgramType.PK
    for phrase in phrases:
        if re.search(
            SCHOOL_CUES + "|" + ADULT_CUES + r"|студент\w*|слушател\w*|обучающ\w*|учащ\w*", phrase, FLAGS
        ):
            facts.audience_phrase = re.split(r",|\s+(?:которые|чтобы|так\s+как)\s", phrase)[0].strip()[:100]
            break
    facts.age_or_grade = extract_age_or_grade(norm)
    facts.entry_level_stated = bool(re.search(ENTRY_LEVEL_CUES, norm, FLAGS))


def extract_age_or_grade(norm: str) -> str | None:
    grades = re.search(
        r"\b(\d{1,2})\s*(?:-?(?:го|х|м|й))?\s*(?:и|-|–|—|,)\s*(\d{1,2})\s*(?:-?х\s*)?класс", norm, FLAGS
    )
    if grades:
        return f"{grades.group(1)}–{grades.group(2)} классы"
    grade = re.search(r"\b(\d{1,2})\s*(?:-?(?:го|х|м|й))?\s+класс", norm, FLAGS)
    if grade:
        return f"{grade.group(1)} класс"
    ages = re.search(r"\b(\d{1,2})\s*(?:-|–|—)\s*(\d{1,2})\s*лет", norm, FLAGS)
    if ages:
        return f"{ages.group(1)}–{ages.group(2)} лет"
    age_from = re.search(r"\bот\s+(\d{1,2})\s+лет", norm, FLAGS)
    if age_from:
        return f"от {age_from.group(1)} лет"
    return None


# --------------------------------------------------------------------------------------------------
# Форма обучения и итоговая аттестация
# --------------------------------------------------------------------------------------------------

FORM_TITLES = {
    StudyFormKind.OCHNAYA: "очная",
    StudyFormKind.OCHNO_ZAOCHNAYA: "очно-заочная",
    StudyFormKind.ZAOCHNAYA: "заочная",
}


def classify_form(text: str) -> tuple[StudyFormKind | None, bool, bool, float | None]:
    """(форма, ДОТ, электронное обучение, доля ДОТ) по тексту."""
    norm = normalize(text)
    dot = bool(re.search(r"дистанционн\w*|\bДОТ\b|онлайн|вебинар\w*|\bдистант\w*", norm, FLAGS))
    eo = bool(re.search(r"электронн\w+\s+обучени\w*|\bЭО\b", norm, FLAGS))
    kind: StudyFormKind | None = None
    if re.search(r"очно[-\s]*заочн\w*|смешанн\w+\s+(?:формат|обучени|режим)", norm, FLAGS):
        kind = StudyFormKind.OCHNO_ZAOCHNAYA
    elif re.search(
        r"(?<!очно-)(?<!очно )\bзаочн\w*|(?:полностью|только|целиком)\s+(?:онлайн|дистанционн\w*)",
        norm,
        FLAGS,
    ):
        kind = StudyFormKind.ZAOCHNAYA
    elif re.search(r"\bочн(?:о|ая|ой|ую|ые|ых|ый|ом)\b(?![-\s]*заочн)", norm, FLAGS):
        kind = StudyFormKind.OCHNAYA
    if kind == StudyFormKind.ZAOCHNAYA and re.search(r"полностью|только|целиком", norm, FLAGS):
        dot = True
    share = None
    match = re.search(r"(\d+)\s*%[^.;!?\n]{0,30}?(?:дистанц|онлайн|ДОТ)", norm, FLAGS) or re.search(
        r"(?:дистанц\w*|онлайн|ДОТ)[^.;!?\n]{0,30}?(\d+)\s*%", norm, FLAGS
    )
    if match:
        share = min(1.0, int(match.group(1)) / 100)
    return kind, dot, eo, share


def _form(facts: RequestFacts, norm: str) -> None:
    facts.form_kind, facts.dot, facts.eo, facts.distance_share = classify_form(norm)


FA_RULES: list[tuple[FAForm, str]] = [
    (FAForm.REPORT_DEFENSE, r"защит\w*\s+(?:\w+\s+){0,3}?отчет\w*"),
    (FAForm.PORTFOLIO, r"портфолио"),
    (
        FAForm.PROJECT_DEFENSE,
        r"защит[аыеуо]й?\s+(?:\w+\s+){0,3}?проект\w*|защища\w*\s+(?:\w+\s+){0,3}?проект\w*",
    ),
    (FAForm.EXAM_PRACTICAL, r"экзамен\w*"),
    (
        FAForm.TEST,
        r"итогов\w*\s+аттестаци\w*" + _END + r"{0,40}?тест\w*|аттестаци\w*" + _END + r"{0,30}?тестировани\w*",
    ),
    (FAForm.ZACHET, r"зачет\w*"),
]
FA_TITLES = {
    FAForm.PROJECT_DEFENSE: "защита проекта",
    FAForm.PORTFOLIO: "зачёт по портфолио",
    FAForm.EXAM_PRACTICAL: "экзамен с практическим заданием",
    FAForm.REPORT_DEFENSE: "защита отчёта",
    FAForm.TEST: "итоговое тестирование",
    FAForm.ZACHET: "зачёт",
    FAForm.OTHER: "форма по описанию методиста",
}


def classify_fa(text: str) -> tuple[FAForm | None, re.Match[str] | None]:
    norm = normalize(text)
    for form, rx in FA_RULES:
        match = re.search(rx, norm, FLAGS)
        if match:
            return form, match
    return None, None


FA_TEXT_MAX = 160


def _fa_fragment(original: str, start: int, end: int) -> str:
    """Предложение про аттестацию, если оно короткое; иначе — часть между запятыми вокруг найденной формы."""
    sentence = _sentence(original, start)
    if len(sentence) <= FA_TEXT_MAX:
        return sentence
    left = max(original.rfind(c, 0, start) for c in ",.;!?\n") + 1
    ends = [i for i in (original.find(c, end) for c in ",.;!?\n") if i != -1]
    right = min(ends) if ends else len(original)
    return original[left:right].strip()[:FA_TEXT_MAX]


def _final_attestation(facts: RequestFacts, norm: str, original: str) -> None:
    form, match = classify_fa(norm)
    if form is not None and match is not None:
        facts.fa_form = form
        facts.fa_text = _fa_fragment(original, match.start(), match.end())


# --------------------------------------------------------------------------------------------------
# Ограничения, название, слайды
# --------------------------------------------------------------------------------------------------

FLAG_RULES = {
    "ru_services_only": r"(?:доступн\w*|работа\w*|разрешен\w*)\s+в\s+(?:России|РФ)\b|российск\w+\s+(?:сервис|нейросет|решени|платформ|аналог)"
    r"|отечественн\w+\s+(?:сервис|нейросет|решени|платформ|ПО|программ)|без\s+VPN|импортозамещ\w*",
    "anonymized_data": r"обезличен\w*|анонимиз\w*|деперсонализ\w*",
    "all_srs": r"вся\s+работа\s+самостоятельн\w*|аудиторных\s+(?:занятий|часов)\s+нет|без\s+аудиторных"
    r"|только\s+самостоятельн\w*|полностью\s+самостоятельн\w*",
    "no_programming": r"без\s+(?:навыков\s+)?программировани\w*|не\s+требу\w*\s+(?:навыков\s+|знания\s+)?программировани\w*"
    r"|не\s+(?:умеют|знают)\s+программир\w*|не\s+программист\w*",
}


def _flags(facts: RequestFacts, norm: str) -> None:
    for name, rx in FLAG_RULES.items():
        setattr(facts, name, bool(re.search(rx, norm, FLAGS)))


_QUOTE = r"[«\"“„]([^«»\"“”„]{3,150})[»\"”“]"


def extract_title(text: str) -> str | None:
    """Название программы — в кавычках, лучше всего сразу после слов «программа», «курс», «стажировка»."""
    norm = normalize(text)
    near = re.search(
        r"(?:программ\w*|курс\w*|стажировк\w*|тренинг\w*|интенсив\w*)[^«\"“„.!?\n]{0,60}?" + _QUOTE,
        norm,
        FLAGS,
    )
    match = near or re.search(_QUOTE, norm, FLAGS)
    if not match:
        return None
    start, end = match.span(1)
    return text[start:end].strip() or None


def extract_slides(norm: str) -> SlidesScope | None:
    """Для чего нужны слайды: номера занятий или модуль по теме. None — в запросе не сказано."""
    for cue in re.finditer(r"слайд\w*|презентаци\w*", norm, FLAGS):
        window = _sentence(norm, cue.start())
        if not re.search(r"(?:к|для|по)\s+(?:\w+\s+){0,3}?(?:заняти|модул|урок|лекци)", window, FLAGS):
            continue
        first_n = re.search(
            rf"перв\w+\s+{NUM}\s+(?:заняти|урок)|{NUM}\s+перв\w+\s+(?:заняти|урок)", window, FLAGS
        )
        if first_n:
            n = parse_num(first_n.group(1) or first_n.group(2))
            if n:
                return SlidesScope(mode="sessions", session_numbers=list(range(1, int(n) + 1)))
        listed = re.search(r"(?:заняти|урок)\w*\s+(?:№\s*)?(\d+(?:\s*(?:,|и|-|–|—)\s*\d+)*)", window, FLAGS)
        if listed:
            return SlidesScope(mode="sessions", session_numbers=_number_list(listed.group(1)))
        if re.search(r"перв\w+\s+(?:заняти|урок)", window, FLAGS):
            return SlidesScope(mode="sessions", session_numbers=[1])
        module = re.search(
            r"модул\w*\s+(?:про|по|о|об|на\s+тему)\s+[«\"]?([^.,;:!?»\"\n]{2,60})|модул\w*\s+[«\"]([^»\"]{2,80})[»\"]",
            window,
            FLAGS,
        )
        if module:
            phrase = (module.group(1) or module.group(2)).strip()
            return SlidesScope(mode="module", module_pattern=topic_pattern(phrase))
    return None


def _number_list(raw: str) -> list[int]:
    numbers: list[int] = []
    for part in re.split(r"\s*(?:,|и)\s*", raw):
        bounds = re.split(r"\s*[-–—]\s*", part.strip())
        if len(bounds) == 2 and all(b.isdigit() for b in bounds):
            numbers += list(range(int(bounds[0]), int(bounds[1]) + 1))
        elif part.strip().isdigit():
            numbers.append(int(part.strip()))
    return sorted(set(numbers))


# --------------------------------------------------------------------------------------------------
# Шаблоны тем
# --------------------------------------------------------------------------------------------------

TOPIC_STOPWORDS = {
    "основы",
    "основ",
    "основа",
    "введение",
    "для",
    "при",
    "как",
    "это",
    "работа",
    "работы",
    "работе",
    "использование",
    "применение",
    "тема",
    "темы",
    "модуль",
    "раздел",
}
_LATIN_TERM = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]*[A-Za-z0-9+#]|[A-Za-z]")


def stem(word: str) -> str:
    word = normalize(word).lower()
    if len(word) >= 6:
        return word[:-2]
    if len(word) >= 4:
        return word[:-1]
    return word


def topic_pattern(label: str) -> str:
    """Регулярка для поиска темы в названиях модулей и тем (без учёта регистра).

    Есть латинские термины (pandas, SQL, CI/CD) — ищется любой из них; иначе — основы значимых слов по порядку.
    """
    latin = [t for t in _LATIN_TERM.findall(label) if len(t) >= 2]
    if latin:
        return "|".join(rf"(?<![A-Za-z]){re.escape(t.lower())}(?![A-Za-z])" for t in latin)
    words = [
        w
        for w in re.findall(r"[А-Яа-яЁё]+", label)
        if len(w) >= 3 and normalize(w).lower() not in TOPIC_STOPWORDS
    ]
    stems = [re.escape(stem(w)) for w in words]
    return r".{0,40}?".join(stems) if stems else re.escape(label.strip().lower())


def found_in(pattern: str, text: str) -> bool:
    try:
        return bool(re.search(pattern, normalize(text), FLAGS))
    except re.error:
        return False


# слишком общие слова: совпадут с любым заданием и сделают проверку «задания из среды слушателей» бессмысленной
DOMAIN_STOPWORDS = {
    "работа",
    "работы",
    "работе",
    "работу",
    "работать",
    "задача",
    "задачи",
    "сотрудник",
    "сотрудники",
    "специалист",
    "специалисты",
    "компания",
    "организация",
    "данные",
    "информация",
    "система",
    "процесс",
}


def domain_patterns(raw: list[str], domain: str | None) -> list[str]:
    """Ключевые слова профессиональной среды → безопасные регулярки (основы слов, без спецсимволов)."""
    words = [w for item in raw for w in re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", item)]
    words = [w for w in words if normalize(w).lower() not in DOMAIN_STOPWORDS]
    if not words and domain:
        words = [w for w in re.findall(r"[A-Za-zА-Яа-яЁё]{4,}", domain)]
    result: list[str] = []
    for word in words:
        piece = re.escape(stem(word) if len(word) > 6 else normalize(word).lower())
        if piece not in result:
            result.append(piece)
    return result[:8]


# --------------------------------------------------------------------------------------------------
# Противоречия, которые видит код
# --------------------------------------------------------------------------------------------------

MAX_TEST_QUESTIONS = DEFAULT_LIMITS.max_test_questions  # больше — ≈ 5 и более вызовов модели только на тест
MIN_WEEKLY_HOURS = 1.5  # меньше — срок и объём явно не согласованы («16 часов за 3 месяца»)
MAX_WEEKLY_HOURS = 48.0  # больше невозможно даже при полной загрузке шесть дней в неделю
SESSION_TOLERANCE = 1e-6


def text_conflicts(facts: RequestFacts) -> list[str]:
    """Противоречия в самом тексте запроса."""
    found: list[str] = []
    if len(facts.total_candidates) > 1:
        found.append("conflict_hours")
    types = set(facts.explicit_types)
    pk_like = types & {ProgramType.PK, ProgramType.PK_INTERNSHIP}
    if (ProgramType.DOP in types and pk_like) or facts.retraining or (pk_like and facts.school_audience):
        found.append("conflict_type")
    return found


def spec_conflicts(spec: RequestSpec) -> list[str]:
    """Противоречия между параметрами (после разбора и после ответов методиста)."""
    found: list[str] = []
    total = spec.total_hours
    if total and spec.weeks:
        weekly = total / spec.weeks
        if weekly < MIN_WEEKLY_HOURS or weekly > MAX_WEEKLY_HOURS:
            found.append("conflict_load")
    if total and spec.session_hours:
        sessions = total / spec.session_hours
        if abs(sessions - round(sessions)) > SESSION_TOLERANCE:
            found.append("conflict_sessions")
    if total and spec.fa_hours is not None and spec.fa_hours >= total:
        found.append("conflict_fa_hours")
    elif total and total - _default_fa_hours(spec) < MIN_TOPIC_BUDGET:
        found.append("conflict_volume")
    return found


MIN_TOPIC_BUDGET = 1.0  # меньше часа на все темы — программы нет


def _default_fa_hours(spec: RequestSpec) -> float:
    if spec.fa_hours is not None:
        return spec.fa_hours
    if spec.program_type == ProgramType.DOP and spec.session_hours:
        return spec.session_hours
    return 2.0
