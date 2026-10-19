"""Контракт реестра проверенных фактов: формат записей data/registry/*.json и интерфейс для проверок.

validate.py зависит только от RegistryView; загрузку файлов и нечёткий поиск делает app/core/registry.py.
InMemoryRegistry — простая реализация (точное сравнение нормализованных строк) для тестов и как база.

Форматы файлов (все — {"items": [...]}), у каждой записи status: verified | to_verify, source_url, checked_at:
- norms.json         NormRecord
- profstandards.json ProfStandardRecord (+ "candidates_to_verify" — не записи, игнорируются загрузчиком)
- ru_ai_services.json AIServiceRecord (available_in_ru=false — стоп-лист)
- literature.json    LiteratureRecord (каталог проверенных изданий и документации)
- publishers.json    PublisherRecord
- tools.json         ToolRecord (официальная документация и минимальная поддерживаемая версия)
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from functools import lru_cache
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

RecordStatus = Literal["verified", "to_verify"]

_RU_LETTERS = "А-Яа-яЁё"
_WORD_CHARS = f"0-9A-Za-z{_RU_LETTERS}_"
_RU_SUFFIX = r"(?:ами|ями|ом|ем|ой|ей|ов|ев|ам|ям|ах|ях|а|я|у|ю|е|и|ы)?"


def normalize_text(text: str) -> str:
    """Для сравнения названий: нижний регистр, ё→е, без кавычек и пунктуации, одиночные пробелы."""
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[«»\"'’‘`“”„()\[\],.;:!?—–-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fold_yo(text: str) -> str:
    """ё → е с сохранением длины строки (позиции совпадений в исходном тексте не сдвигаются)."""
    return text.replace("ё", "е").replace("Ё", "Е")


@lru_cache(maxsize=4096)
def name_pattern(name: str) -> re.Pattern[str] | None:
    """Регулярка упоминания имени сервиса или инструмента: границы слова для латиницы и кириллицы, без учёта
    регистра, любые пробелы между словами. К имени, оканчивающемуся на кириллицу, допускаются падежные окончания
    («в ГигаЧате», «Дипсику»). Искать в тексте, пропущенном через fold_yo."""
    name = fold_yo(name.strip())
    if not name:
        return None
    body = r"\s+".join(re.escape(part) for part in name.split())
    suffix = _RU_SUFFIX if re.search(f"[{_RU_LETTERS}]$", name) else ""
    return re.compile(f"(?<![{_WORD_CHARS}]){body}{suffix}(?![{_WORD_CHARS}])", re.IGNORECASE)


class RegistryRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: RecordStatus = "to_verify"
    source_url: str | None = None
    checked_at: str | None = None

    @property
    def verified(self) -> bool:
        return self.status == "verified" and bool(self.source_url)


class NormRecord(RegistryRecord):
    id: str
    kind: str = ""  # federal_law | order | decree (постановление/распоряжение Правительства) | letter
    number: str = ""
    date: str = ""  # ISO
    title: str
    applies_to: list[str] = Field(default_factory=list)  # "pk", "dop"
    in_force: bool = True
    in_force_since: str | None = None
    in_force_until: str | None = None
    replaces: str | None = None
    replaced_by: str | None = None
    note: str = ""


class LaborFunctionRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    otf: str = ""
    code: str  # «C/11.6»
    title: str


class ProfStandardRecord(RegistryRecord):
    code: str  # «06.015»
    title: str
    order: str = ""  # приказ Минтруда (номер, дата) — редакция
    note: str = ""
    labor_functions: list[LaborFunctionRecord] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)  # направления: data, security, education ...


class AIServiceRecord(RegistryRecord):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)  # латиницей и кириллицей
    url: str | None = None
    available_in_ru: bool = True  # false — стоп-лист (нужен VPN / зарубежный номер)
    kinds: list[str] = Field(default_factory=list)  # text, image, presentation, ...
    note: str = ""

    def all_names(self) -> list[str]:
        return [self.name, *self.aliases]


class LiteratureRecord(RegistryRecord):
    id: str
    kind: Literal["book", "doc"] = "book"
    authors: list[str] = Field(default_factory=list)
    title: str
    year: int | None = None
    publisher: str | None = None
    isbn: str | None = None
    url: str | None = None
    tags: list[str] = Field(default_factory=list)  # направления


class PublisherRecord(RegistryRecord):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    country: str | None = None

    def all_names(self) -> list[str]:
        return [self.name, *self.aliases]


class ToolRecord(RegistryRecord):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    docs_url: str | None = None
    min_version: str | None = None  # минимальная поддерживаемая версия на дату проверки

    def all_names(self) -> list[str]:
        return [self.name, *self.aliases]


class RegistryView(Protocol):
    """Что нужно проверкам и генерации от реестра."""

    def norm(self, norm_id: str) -> NormRecord | None: ...

    def norms(self) -> list[NormRecord]: ...

    def profstandard(self, code: str) -> ProfStandardRecord | None: ...

    def profstandards(self) -> list[ProfStandardRecord]: ...

    def labor_function(self, ps_code: str, tf_code: str) -> LaborFunctionRecord | None: ...

    def ai_services(self) -> list[AIServiceRecord]: ...

    def literature(self, item_id: str) -> LiteratureRecord | None: ...

    def literature_items(self) -> list[LiteratureRecord]: ...

    def find_literature(self, title: str, authors: Sequence[str] = ()) -> LiteratureRecord | None: ...

    def publisher_known(self, name: str) -> bool: ...

    def tool(self, name: str) -> ToolRecord | None: ...

    def tools(self) -> list[ToolRecord]: ...


class InMemoryRegistry:
    """Реестр из списков записей. Сравнение — по normalize_text, без нечёткого поиска."""

    def __init__(
        self,
        norms: Iterable[NormRecord] = (),
        profstandards: Iterable[ProfStandardRecord] = (),
        ai_services: Iterable[AIServiceRecord] = (),
        literature: Iterable[LiteratureRecord] = (),
        publishers: Iterable[PublisherRecord] = (),
        tools: Iterable[ToolRecord] = (),
    ) -> None:
        self._norms = list(norms)
        self._profstandards = list(profstandards)
        self._ai_services = list(ai_services)
        self._literature = list(literature)
        self._publishers = list(publishers)
        self._tools = list(tools)

    def norm(self, norm_id: str) -> NormRecord | None:
        return next((n for n in self._norms if n.id == norm_id), None)

    def norms(self) -> list[NormRecord]:
        return list(self._norms)

    def profstandard(self, code: str) -> ProfStandardRecord | None:
        return next((p for p in self._profstandards if p.code == code.strip()), None)

    def profstandards(self) -> list[ProfStandardRecord]:
        return list(self._profstandards)

    def labor_function(self, ps_code: str, tf_code: str) -> LaborFunctionRecord | None:
        standard = self.profstandard(ps_code)
        if standard is None:
            return None
        return next((f for f in standard.labor_functions if f.code == tf_code.strip()), None)

    def ai_services(self) -> list[AIServiceRecord]:
        return list(self._ai_services)

    def literature(self, item_id: str) -> LiteratureRecord | None:
        return next((item for item in self._literature if item.id == item_id), None)

    def literature_items(self) -> list[LiteratureRecord]:
        return list(self._literature)

    def find_literature(self, title: str, authors: Sequence[str] = ()) -> LiteratureRecord | None:
        wanted = normalize_text(title)
        for item in self._literature:
            if normalize_text(item.title) == wanted:
                return item
        return None

    def publisher_known(self, name: str) -> bool:
        wanted = normalize_text(name)
        return any(normalize_text(alias) == wanted for p in self._publishers for alias in p.all_names())

    def tool(self, name: str) -> ToolRecord | None:
        wanted = normalize_text(name)
        return next((t for t in self._tools if any(normalize_text(a) == wanted for a in t.all_names())), None)

    def tools(self) -> list[ToolRecord]:
        return list(self._tools)
