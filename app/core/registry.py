"""Загрузка реестра проверенных фактов (data/registry/*.json) и поиск по нему.

Формат записей и протокол RegistryView — в app/core/registry_api.py. Здесь:
- load_registry(): читает файлы {"items": [...]}; нет файла — пустой список, битая запись — пропуск
  с текстом в Registry.load_warnings (загрузка не падает никогда);
- нечёткий поиск литературы, шорт-лист каталога по тегам, поиск профстандартов и списка ТФ для промпта;
- нормативка по типу программы, упоминания ИИ-сервисов в тексте;
- статусы: is_verified(), mark_unverified().

LLM реестр не пишет: все записи — из файлов, которые наполняются сверкой с первоисточниками.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.registry_api import (
    AIServiceRecord,
    InMemoryRegistry,
    LaborFunctionRecord,
    LiteratureRecord,
    NormRecord,
    ProfStandardRecord,
    PublisherRecord,
    ToolRecord,
    fold_yo,
    name_pattern,
    normalize_text,
)
from app.models import ProgramType

DEFAULT_REGISTRY_DIR = Path(__file__).resolve().parents[2] / "data" / "registry"

UNVERIFIED_MARK = "[не подтверждено]"
LITERATURE_MATCH_THRESHOLD = 0.85
"""Порог похожести названий (SequenceMatcher.ratio) для совпадения кандидата с каталогом."""
PS_FUNCTION_WORDS_CAP = 3
"""Сколько баллов профстандарту могут дать слова названий его ТФ (иначе большие ПС выигрывают числом ТФ)."""
COMMON_FUNCTION_STEM_SHARE = 0.3
"""Слово, которое есть больше чем в этой доле ТФ стандарта, не помогает выбрать ТФ под запрос."""

_MIN_MAIN_TITLE_LEN = 12
_STEM_LEN = 5
_CYR_TO_LAT = str.maketrans("АВСЕНКМОРТХ", "ABCEHKMOPTX")


def normalize_tf_code(code: str) -> str:
    """Код ТФ как в реестре: без пробелов, буква латиницей («С / 11.6» → «C/11.6»)."""
    return re.sub(r"\s+", "", code).upper().translate(_CYR_TO_LAT)


RecordT = TypeVar("RecordT", bound=BaseModel)


# --------------------------------------------------------------------------------------------------
# Статусы
# --------------------------------------------------------------------------------------------------


def is_verified(record: object) -> bool:
    """Запись подтверждена: status = verified и есть source_url. Для объектов без статуса — False."""
    return bool(getattr(record, "verified", False))


def mark_unverified(text: str) -> str:
    """Добавляет метку «[не подтверждено]» в конец текста; повторно метку не ставит."""
    text = text.strip()
    if UNVERIFIED_MARK in text:
        return text
    return f"{text} {UNVERIFIED_MARK}" if text else UNVERIFIED_MARK


# --------------------------------------------------------------------------------------------------
# Вспомогательное: токены, основы слов, фамилии
# --------------------------------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    return normalize_text(text).split()


def _stem(word: str) -> str:
    """Грубая основа слова для русской морфологии: первые 5 букв («данные» ~ «данных»)."""
    return word[:_STEM_LEN]


def _stems(text: str) -> set[str]:
    return {_stem(token) for token in _tokens(text) if len(token) > 2}


def _surname_tokens(author: str) -> set[str]:
    """Значимые части имени автора без инициалов: «Вигерс К.» и «Карл Вигерс» дают общую «вигерс»."""
    return {token for token in _tokens(author) if len(token) > 2}


SURNAME_MATCH_RATIO = 0.8
"""Фамилии с опечаткой («Лубанович» и «Любанович») считаются одной: модель ошибается в транслитерации."""


def _surnames_match(wanted: set[str], item: set[str]) -> bool:
    if wanted & item:
        return True
    return any(SequenceMatcher(None, a, b).ratio() >= SURNAME_MATCH_RATIO for a in wanted for b in item)


def title_similarity(wanted_title: str, item_title: str) -> float:
    """Похожесть названий изданий (0…1) — для поиска в каталоге и отсева дублей."""
    return _title_similarity(wanted_title, item_title)


def _main_title(title: str) -> str:
    """Основное заглавие без подзаголовка и сведений об издании (до «:» или «. »)."""
    head = re.split(r":|\.\s|\s[—–-]\s", title, maxsplit=1)[0]
    return normalize_text(head)


def _title_similarity(wanted_title: str, item_title: str) -> float:
    wanted, candidate = normalize_text(wanted_title), normalize_text(item_title)
    score = SequenceMatcher(None, wanted, candidate).ratio()
    wanted_main, candidate_main = _main_title(wanted_title), _main_title(item_title)
    if min(len(wanted_main), len(candidate_main)) >= _MIN_MAIN_TITLE_LEN:
        score = max(score, SequenceMatcher(None, wanted_main, candidate_main).ratio())
    return score


def _tag_matcher(tag: str) -> tuple[str, bool]:
    """Тег ПС → (образец, по началу слова?). Русское слово от 6 букв сравнивается без двух последних букв
    («машинного» ~ «машинное», «тестированию» ~ «тестирование»); латиница и короткие теги — целиком («sql», «иб»)."""
    tag = normalize_text(tag)
    if re.fullmatch(r"[а-я]{6,}", tag):
        return tag[: max(_STEM_LEN, len(tag) - 2)], True
    return tag, False


def _tag_hits(tags: Iterable[str], query_tokens: set[str]) -> int:
    """Сколько разных слов запроса совпало хотя бы с одним тегом (словоформы одного слова в тегах не суммируются)."""
    matchers = [_tag_matcher(tag) for tag in tags if normalize_text(tag)]
    return sum(
        1
        for token in query_tokens
        if any(token.startswith(sample) if by_prefix else token == sample for sample, by_prefix in matchers)
    )


def _applies_key(program_type: ProgramType | str) -> str:
    value = program_type.value if isinstance(program_type, ProgramType) else str(program_type)
    return "dop" if value == ProgramType.DOP.value else "pk"


# --------------------------------------------------------------------------------------------------
# Реестр
# --------------------------------------------------------------------------------------------------


def function_level(code: str) -> int | None:
    """Уровень квалификации ТФ из кода: «C/06.7» → 7."""
    match = re.search(r"\.(\d)\s*$", code)
    return int(match.group(1)) if match else None


def within_level(functions: list[LaborFunctionRecord], max_level: int | None) -> list[LaborFunctionRecord]:
    """ТФ не выше уровня аудитории; если таких нет — ТФ самого низкого уровня стандарта."""
    if max_level is None:
        return functions
    levels = [function_level(f.code) for f in functions]
    fitting = [f for f, lvl in zip(functions, levels, strict=True) if lvl is None or lvl <= max_level]
    if fitting:
        return fitting
    lowest = min((lvl for lvl in levels if lvl is not None), default=None)
    return [f for f, lvl in zip(functions, levels, strict=True) if lvl == lowest]


class Registry(InMemoryRegistry):
    """Реестр из файлов data/registry. Реализует RegistryView и добавляет нечёткий поиск."""

    def __init__(
        self,
        norms: Iterable[NormRecord] = (),
        profstandards: Iterable[ProfStandardRecord] = (),
        ai_services: Iterable[AIServiceRecord] = (),
        literature: Iterable[LiteratureRecord] = (),
        publishers: Iterable[PublisherRecord] = (),
        tools: Iterable[ToolRecord] = (),
        load_warnings: Iterable[str] = (),
        missing_files: Iterable[str] = (),
    ) -> None:
        super().__init__(norms, profstandards, ai_services, literature, publishers, tools)
        self.load_warnings: list[str] = list(load_warnings)
        self.missing_files: list[str] = list(missing_files)
        self._service_patterns: list[tuple[AIServiceRecord, list[re.Pattern[str]]]] | None = None
        self._function_stems_cache: dict[str, set[str]] = {}

    # --- статусы (для удобства вызова через реестр) ---

    @staticmethod
    def is_verified(record: object) -> bool:
        return is_verified(record)

    @staticmethod
    def mark_unverified(text: str) -> str:
        return mark_unverified(text)

    # --- литература ---

    def find_literature(self, title: str, authors: Sequence[str] = ()) -> LiteratureRecord | None:
        """Запись каталога, похожая на кандидата: название ≥ порога; если авторы даны — совпадает фамилия
        первого автора. Возвращает самую похожую запись или None."""
        if not normalize_text(title):
            return None
        wanted_author = _surname_tokens(authors[0]) if authors else None
        best: LiteratureRecord | None = None
        best_score = 0.0
        for item in self._literature:
            if wanted_author is not None:
                item_author = _surname_tokens(item.authors[0]) if item.authors else set()
                if not _surnames_match(wanted_author, item_author):
                    continue
            score = _title_similarity(title, item.title)
            if score > best_score:
                best, best_score = item, score
        return best if best_score >= LITERATURE_MATCH_THRESHOLD else None

    def literature_for_tags(self, tags: Iterable[str], limit: int = 30) -> list[LiteratureRecord]:
        """Шорт-лист каталога для навыка литературы: записи, совпавшие с тегами (по полю tags — вес 2,
        по словам названия — вес 1). Порядок: релевантность, подтверждённые выше, новее выше."""
        wanted = [normalize_text(tag) for tag in tags if normalize_text(tag)]
        if not wanted or limit <= 0:
            return []
        wanted_stems = {_stem(token) for tag in wanted for token in tag.split() if len(token) > 2}
        scored: list[tuple[int, LiteratureRecord]] = []
        for item in self._literature:
            item_tags = {normalize_text(tag) for tag in item.tags}
            score = 2 * sum(1 for tag in wanted if tag in item_tags)
            score += len(wanted_stems & _stems(item.title))
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], not is_verified(pair[1]), -(pair[1].year or 0)))
        return [item for _, item in scored[:limit]]

    # --- профстандарты ---

    def labor_function(self, ps_code: str, tf_code: str) -> LaborFunctionRecord | None:
        """ТФ профстандарта; буква кода, набранная кириллицей, и пробелы не мешают поиску."""
        standard = self.profstandard(ps_code)
        if standard is None:
            return None
        wanted = normalize_tf_code(tf_code)
        return next((f for f in standard.labor_functions if normalize_tf_code(f.code) == wanted), None)

    def search_profstandards(
        self, text_or_tags: str | Iterable[str], min_score: int = 1
    ) -> list[ProfStandardRecord]:
        """Профстандарты по убыванию релевантности (см. scored_profstandards)."""
        return [standard for _, standard in self.scored_profstandards(text_or_tags, min_score)]

    def scored_profstandards(
        self, text_or_tags: str | Iterable[str], min_score: int = 1
    ) -> list[tuple[int, ProfStandardRecord]]:
        """Профстандарты по тексту запроса или списку тегов: код в тексте — выше всего, затем теги,
        затем слова названия и названий ТФ. Только записи с релевантностью не ниже min_score
        (код в тексте — 100, тег — 5, слово названия ПС — 2, слова названий ТФ — по 1, но не больше
        PS_FUNCTION_WORDS_CAP и только если совпал код, тег или слово названия)."""
        if isinstance(text_or_tags, str):
            text = text_or_tags
            query_tokens = set(_tokens(text))
        else:
            parts = [str(tag) for tag in text_or_tags]
            text = " ".join(parts)
            query_tokens = {token for part in parts for token in _tokens(part)}
        query_stems = _stems(text)
        scored: list[tuple[int, int, ProfStandardRecord]] = []
        for order, standard in enumerate(self._profstandards):
            score = 0
            if re.search(rf"(?<![\d.]){re.escape(standard.code)}(?![\d])", text):
                score += 100
            score += 5 * _tag_hits(standard.tags, query_tokens)
            score += 2 * len(query_stems & _stems(standard.title))
            if score == 0:
                continue  # совпали только слова названий ТФ — у больших ПС (150+ ТФ) это случайность
            score += min(PS_FUNCTION_WORDS_CAP, len(query_stems & self._function_stems(standard)))
            if score >= max(1, min_score):
                scored.append((score, order, standard))
        scored.sort(key=lambda triple: (-triple[0], triple[1]))
        return [(score, standard) for score, _, standard in scored]

    def _function_stems(self, standard: ProfStandardRecord) -> set[str]:
        cached = self._function_stems_cache.get(standard.code)
        if cached is None:
            cached = _stems(" ".join(lf.title for lf in standard.labor_functions))
            self._function_stems_cache[standard.code] = cached
        return cached

    def relevant_labor_functions(
        self, standard: ProfStandardRecord, text: str, limit: int, max_level: int | None = None
    ) -> list[LaborFunctionRecord]:
        """Не больше limit ТФ стандарта, ближайших к тексту запроса, в исходном порядке. Слова, которые есть
        в большинстве ТФ стандарта («в рамках … сопровождению ИС»), не различают ТФ и не учитываются.
        max_level — уровень квалификации аудитории: ТФ выше него не предлагаются (если таких нет — ТФ самого
        низкого уровня стандарта)."""
        functions = within_level(list(standard.labor_functions), max_level)
        if len(functions) <= limit:
            return functions
        stems = [_stems(f.title) for f in functions]
        counts = Counter(stem for item in stems for stem in item)
        common = {
            stem for stem, count in counts.items() if count > COMMON_FUNCTION_STEM_SHARE * len(functions)
        }
        query = _stems(text) - common
        best = sorted(range(len(functions)), key=lambda i: (-len(stems[i] & query), i))[:limit]
        return [functions[i] for i in sorted(best)]

    def labor_functions_for(self, codes: Iterable[str]) -> list[tuple[str, LaborFunctionRecord]]:
        """Все ТФ указанных профстандартов парами (код ПС, ТФ) — список для промпта каркаса.
        Неизвестные коды пропускаются, повторы кодов не дублируют ТФ."""
        result: list[tuple[str, LaborFunctionRecord]] = []
        seen: set[str] = set()
        for code in codes:
            standard = self.profstandard(code)
            if standard is None or standard.code in seen:
                continue
            seen.add(standard.code)
            result.extend((standard.code, function) for function in standard.labor_functions)
        return result

    # --- нормативка ---

    def norms_for(
        self, program_type: ProgramType | str, dot: bool, include_letters: bool = False
    ) -> list[NormRecord]:
        """Нормативные акты для типа программы (только действующие):
        - федеральный закон (273-ФЗ);
        - порядок организации и осуществления образовательной деятельности по типу программ
          (ПК — действующий порядок по ДПП, ДОП — порядок по дополнительным общеобразовательным);
        - при ЭО/ДОТ — правила применения ЭО и ДОТ (приказ или постановление Правительства, kind decree);
          include_letters добавляет письма по ДОТ.
        """
        key = _applies_key(program_type)
        applicable = [n for n in self._norms if n.in_force and key in n.applies_to]

        def title_has(norm: NormRecord, *needles: str) -> bool:
            title = normalize_text(norm.title)
            return any(normalize_text(needle) in title for needle in needles)

        result = [n for n in applicable if n.kind == "federal_law"]
        result += [
            n
            for n in applicable
            if n.kind == "order" and title_has(n, "порядка организации и осуществления образовательной")
        ]
        if dot:
            dot_needles = ("дистанционных образовательных технологий", "электронного обучения")
            kinds = {"order", "decree", "letter"} if include_letters else {"order", "decree"}
            result += [n for n in applicable if n.kind in kinds and title_has(n, *dot_needles)]
        unique: dict[str, NormRecord] = {}
        for norm in result:
            unique.setdefault(norm.id, norm)
        return list(unique.values())

    # --- ИИ-сервисы ---

    def mentioned_ai_services(self, text: str) -> list[tuple[AIServiceRecord, str]]:
        """Сервисы из реестра, упомянутые в тексте (по name и aliases), с найденным фрагментом.
        Один результат на сервис, в порядке первого упоминания."""
        if not text:
            return []
        haystack = fold_yo(text)
        found: list[tuple[int, AIServiceRecord, str]] = []
        for service, patterns in self._compiled_services():
            spans = [m.span() for pattern in patterns if (m := pattern.search(haystack))]
            if spans:
                start, end = min(spans)
                found.append((start, service, text[start:end]))
        found.sort(key=lambda triple: triple[0])
        return [(service, fragment) for _, service, fragment in found]

    def _compiled_services(self) -> list[tuple[AIServiceRecord, list[re.Pattern[str]]]]:
        if self._service_patterns is None:
            compiled = []
            for service in self._ai_services:
                patterns = [p for name in service.all_names() if (p := name_pattern(name)) is not None]
                compiled.append((service, patterns))
            self._service_patterns = compiled
        return self._service_patterns


# --------------------------------------------------------------------------------------------------
# Загрузка
# --------------------------------------------------------------------------------------------------

_FILES: dict[str, tuple[str, type[BaseModel], str]] = {
    "norms": ("norms.json", NormRecord, "id"),
    "profstandards": ("profstandards.json", ProfStandardRecord, "code"),
    "ai_services": ("ru_ai_services.json", AIServiceRecord, "id"),
    "literature": ("literature.json", LiteratureRecord, "id"),
    "publishers": ("publishers.json", PublisherRecord, "id"),
    "tools": ("tools.json", ToolRecord, "id"),
}


def _short_error(error: ValidationError) -> str:
    parts = []
    for item in error.errors()[:3]:
        location = ".".join(str(part) for part in item.get("loc", ())) or "запись"
        parts.append(f"{location}: {item.get('msg', '')}")
    return "; ".join(parts)


def _read_items(path: Path, warnings: list[str]) -> list[Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        warnings.append(f"{path.name}: файл не прочитан ({exc.__class__.__name__}: {exc})")
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]  # прочие ключи (например, candidates_to_verify) — не записи
    warnings.append(f"{path.name}: нет списка items — файл пропущен")
    return []


def _load_records(path: Path, model: type[RecordT], key_field: str, warnings: list[str]) -> list[RecordT]:
    records: list[RecordT] = []
    seen: set[str] = set()
    for index, raw in enumerate(_read_items(path, warnings)):
        label = raw.get(key_field) if isinstance(raw, dict) else None
        where = f"{path.name}: запись #{index}" + (f" ({label})" if label else "")
        if not isinstance(raw, dict):
            warnings.append(f"{where} пропущена: ожидался объект, а не {type(raw).__name__}")
            continue
        try:
            record = model.model_validate(raw)
        except ValidationError as exc:
            warnings.append(f"{where} пропущена: {_short_error(exc)}")
            continue
        key = str(getattr(record, key_field))
        if key in seen:
            warnings.append(f"{where} пропущена: повтор {key_field} = {key}")
            continue
        seen.add(key)
        records.append(record)
    return records


def load_registry(path: Path | str | None = None) -> Registry:
    """Читает реестр из каталога (по умолчанию data/registry). Никогда не падает на данных:
    отсутствующий файл — пустой список (имя в missing_files), битый файл или запись — в load_warnings."""
    base = Path(path) if path is not None else DEFAULT_REGISTRY_DIR
    warnings: list[str] = []
    missing: list[str] = []
    loaded: dict[str, list[Any]] = {}
    for field, (filename, model, key_field) in _FILES.items():
        file_path = base / filename
        if not file_path.is_file():
            missing.append(filename)
            loaded[field] = []
            continue
        loaded[field] = _load_records(file_path, model, key_field, warnings)
    return Registry(**loaded, load_warnings=warnings, missing_files=missing)


@lru_cache(maxsize=1)
def default_registry() -> Registry:
    """Реестр из data/registry, загруженный один раз на процесс."""
    return load_registry()
