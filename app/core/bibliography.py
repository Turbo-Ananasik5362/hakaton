"""Литература: модель предлагает, код проверяет (docs/ARCHITECTURE.md §2.4).

1. Шорт-лист (код): записи каталога literature.json по тегам тем и словам названий + официальная документация
   инструментов из tools.json, которые упомянуты в программе. Модели передаются ключи вида `lit:<id>` / `doc:<id>`.
2. Модель выбирает из шорт-листа по ключу и может предложить до 6 кандидатов — только полями (без готовой строки).
3. Каждый кандидат проверяется: каталог (нечёткое совпадение названия и фамилии автора) → выходные данные берутся
   из каталога; иначе формальные признаки — контрольная сумма ISBN, год (не из будущего и не слишком старый),
   издательство из publishers.json, домен ссылки из официальной документации. Провалившие отбрасываются,
   прошедшие — с меткой «[не подтверждено]», не больше двух на подраздел.
4. Отброшенные и неподтверждённые кандидаты пишутся в logs/unverified_sources.jsonl — по ним пополняют каталог.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.config import DEFAULT_LIMITS
from app.core.ids import IdAllocator
from app.core.registry import Registry, is_verified, title_similarity
from app.core.registry_api import (
    AIServiceRecord,
    LiteratureRecord,
    ToolRecord,
    fold_yo,
    name_pattern,
    normalize_text,
)
from app.core.validate import isbn_valid
from app.models import (
    BookCandidate,
    CatalogPick,
    Kit,
    ProgramType,
    Source,
    SourceKind,
    SourceSection,
    Verification,
)

MAX_SHORTLIST = 30
MAX_TOOL_DOCS = 8
# По умолчанию — из Settings.limits (app/config.py); генератор передаёт настроенные значения в build_sources
MAX_UNVERIFIED_PER_SECTION = DEFAULT_LIMITS.literature_max_unverified
MAX_CANDIDATES = (
    DEFAULT_LIMITS.literature_max_candidates
)  # больше кандидатов от модели не рассматривается (§2.4)
MAX_SOURCE_AGE_YEARS = 15  # как в проверке V23: старше — «слишком старое для учебной программы»
MIN_PER_SECTION = {SourceSection.MAIN: 2, SourceSection.ADDITIONAL: 1, SourceSection.INTERNET: 1}
MAX_PER_SECTION = {SourceSection(k): v for k, v in DEFAULT_LIMITS.literature_per_section().items()}
BOOK_SECTIONS = (SourceSection.MAIN, SourceSection.ADDITIONAL)

Status = Literal["catalog", "unverified", "rejected"]


# --------------------------------------------------------------------------------------------------
# Шорт-лист
# --------------------------------------------------------------------------------------------------


CATALOG_KEY_RE = re.compile(r"^(lit|doc):(\S+)$")
DUPLICATE_TITLE_RATIO = 0.85  # кандидат с названием как у выбранного издания из каталога — дубль
REJECTED_NOTE_PREFIX = "Модель предложила источник"  # заметки об отброшенных кандидатах (не для .docx)


@dataclass(frozen=True)
class ShortlistItem:
    key: str  # «lit:python_lutz_2019», «doc:pandas» или «svc:gigachat» — то, чем модель отвечает в catalog_id
    record: LiteratureRecord | ToolRecord | AIServiceRecord

    @property
    def is_doc(self) -> bool:
        return (
            isinstance(self.record, (ToolRecord, AIServiceRecord))
            or getattr(self.record, "kind", "book") == "doc"
        )

    def describe(self) -> str:
        record = self.record
        if isinstance(record, AIServiceRecord):
            return f"{self.key} — {record.name}: официальный сайт сервиса ({record.url}) [интернет-ресурс]"
        if isinstance(record, ToolRecord):
            return (
                f"{self.key} — {record.name}: официальная документация ({record.docs_url}) [интернет-ресурс]"
            )
        authors = ", ".join(record.authors)
        head = f"{authors}. {record.title}" if authors else record.title
        tail = ", ".join(str(x) for x in (record.publisher, record.year) if x)
        kind = "интернет-ресурс" if record.kind == "doc" else "книга"
        return f"{self.key} — {head}{'. — ' + tail if tail else ''} [{kind}]"


def program_text(kit: Kit) -> str:
    """Всё, по чему подбирается литература: название, модули, темы с тегами и содержанием."""
    parts = [kit.program.title, *(m.title for m in kit.modules)]
    for topic in kit.topics:
        parts += [topic.title, *topic.tags]
        parts += [item for items in topic.content.values() for item in items]
    return "\n".join(parts)


def _query_tags(kit: Kit) -> list[str]:
    tags = [tag for topic in kit.topics for tag in topic.tags]
    words = re.findall(
        r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё+#-]{3,}",
        " ".join([kit.program.title, *(t.title for t in kit.topics)]),
    )
    return list(dict.fromkeys(normalize_text(x) for x in [*tags, *words] if normalize_text(x)))


def mentioned_tools(
    kit: Kit, registry: Registry, limit: int = MAX_TOOL_DOCS, extra: Iterable[str] = ()
) -> list[ToolRecord]:
    """Инструменты из tools.json, которые упомянуты в программе (по названию или алиасу, по границам слов);
    extra — дополнительные тексты (например, перечень ПО от модели)."""
    text = fold_yo("\n".join([program_text(kit), *extra]))
    found: list[tuple[int, ToolRecord]] = []
    for tool in registry.tools():
        if not tool.docs_url:
            continue
        positions = [
            m.start() for name in tool.all_names() if (p := name_pattern(name)) and (m := p.search(text))
        ]
        if positions:
            found.append((min(positions), tool))
    found.sort(key=lambda pair: pair[0])
    return [tool for _, tool in found[:limit]]


def mentioned_services(kit: Kit, registry: Registry, limit: int = MAX_TOOL_DOCS) -> list[AIServiceRecord]:
    """ИИ-сервисы реестра, доступные в России, с официальным адресом, которые упомянуты в программе, — интернет-
    ресурсы (оценка 21.10: раздел «Интернет-ресурсы» программы для учителей был пуст, хотя GigaChat, Алиса
    и Kandinsky в программе есть, а их адреса — в реестре)."""
    found = registry.mentioned_ai_services(program_text(kit))
    return [s for s, _ in found if s.available_in_ru and s.url][:limit]


MATERIAL_FIELDS = frozenset(
    {
        "modules",
        "topics",
        "lectures",
        "seminars",
        "questions",
        "interview_questions",
        "final_attestation",
        "texts",
    }
)


def materials_text(kit: Kit) -> str:
    """Весь текст материалов комплекта: лекции, задания, вопросы, аттестация и тексты программы."""
    return json.dumps(kit.model_dump(mode="json", include=set(MATERIAL_FIELDS)), ensure_ascii=False)


def add_service_resources(kit: Kit, registry: Registry) -> list[str]:
    """Интернет-ресурсы — официальные сайты ИИ-сервисов реестра, доступных в России, которые упомянуты в готовых
    материалах. Литература подбирается параллельно с содержанием и видит только названия тем, а сервисы называют
    лекции, задания и тесты (прогон 22.10: в программе для учителей GigaChat, Алиса, Kandinsky, Шедеврум — а раздел
    «Интернет-ресурсы» пуст). Возвращает названия добавленных сервисов."""
    have = {s.registry_id for s in kit.sources if s.registry_id}
    urls = {s.url for s in kit.sources if s.url}
    found: list[AIServiceRecord] = []
    for service, _ in registry.mentioned_ai_services(materials_text(kit)):
        if service.available_in_ru and service.url and service.id not in have and service.url not in urls:
            found.append(service)
            urls.add(service.url)  # Kandinsky и GigaChat — один адрес giga.chat: одна строка
    room = MAX_PER_SECTION.get(SourceSection.INTERNET, 6) - sum(
        1 for s in kit.sources if s.section == SourceSection.INTERNET
    )
    alloc = IdAllocator(kit)
    added: list[str] = []
    for service in found[: max(0, min(room, MAX_TOOL_DOCS))]:
        item = ShortlistItem(f"svc:{service.id}", service)
        kit.sources.append(_source_from_item(item, SourceSection.INTERNET, alloc.source(), []))
        added.append(service.name)
    return added


def blocked_service(kit: Kit, registry: Registry, text: str) -> str | None:
    """При требовании «только сервисы России» — название недоступного сервиса, упомянутого в тексте."""
    if not kit.request.constraints.ru_services_only:
        return None
    blocked = [
        service.name for service, _ in registry.mentioned_ai_services(text) if not service.available_in_ru
    ]
    return blocked[0] if blocked else None


HIGHER_ED_RE = re.compile(r"для\s+(?:студентов\s+)?вуз|бакалавр|магистр|высш\w+\s+(?:учебн|образован)", re.I)


BEGINNER_RE = re.compile(r"без\s+опыта|с\s+нуля|начинающ|нулев\w*\s+уров", re.I)
ADVANCED_TAGS = frozenset({"продвинутый", "вузы"})
# профильные издания (веб, данные, машинное обучение, тестирование, СУБД): начинающим — только если это темы программы
SPECIALIZED_TAGS = frozenset({"web", "data", "ml", "testing", "databases", "бэкенд", "фронтенд"})


def _beginners(kit: Kit) -> bool:
    audience = kit.request.audience
    text = " ".join([audience.entry_level or "", *audience.constraints, kit.request.raw_text or ""])
    school = kit.program.type == ProgramType.DOP and bool(kit.program.audience.age_or_grade)
    return school or bool(BEGINNER_RE.search(text))


def _level_fits(kit: Kit, record: LiteratureRecord) -> bool:
    """Для школьников (ДОП с возрастом / классами) — без вузовских учебников; для школьников и начинающих — без
    продвинутых и профильных изданий не по темам программы (оценка 21.10: школьникам 8–9 классов — TDD с Django,
    анализ данных и «К вершинам мастерства»)."""
    school = kit.program.type == ProgramType.DOP and bool(kit.program.audience.age_or_grade)
    if school and HIGHER_ED_RE.search(f"{record.title} {' '.join(record.tags)}"):
        return False
    if not _beginners(kit):
        return True
    tags = {normalize_text(tag) for tag in record.tags}
    if tags & ADVANCED_TAGS:
        return False
    wanted = {normalize_text(tag) for topic in kit.topics for tag in topic.tags}
    wanted |= set(normalize_text(kit.program.title).split())
    return not (tags & SPECIALIZED_TAGS) - wanted


def shortlist(kit: Kit, registry: Registry, limit: int = MAX_SHORTLIST) -> list[ShortlistItem]:
    tools = mentioned_tools(kit, registry)
    books = registry.literature_for_tags(_query_tags(kit), limit=max(0, limit - len(tools)) + 10)
    books = [b for b in books if blocked_service(kit, registry, b.title) is None and _level_fits(kit, b)]
    if _beginners(kit):
        # начинающим — без книг, совпавших с темами только общим тегом («основы»), если по предмету их достаточно
        # (оценка 21.10: учебник по защите информации в курсе Python для 8–9 классов). Остальным — без сужения:
        # теги тем русские («искусственный интеллект»), теги каталога — «ai», «education», и учителям оставались
        # одни книги по ML на Python (прогон 22.10, пункт 2.8)
        on = [b for b in books if on_topic(kit, ShortlistItem(f"lit:{b.id}", b))]
        if len(on) >= MIN_ON_TOPIC:
            books = on
    books = books[: max(0, limit - len(tools))]
    items = [ShortlistItem(f"lit:{record.id}", record) for record in books]
    items += [ShortlistItem(f"doc:{tool.id}", tool) for tool in tools]
    items += [ShortlistItem(f"svc:{svc.id}", svc) for svc in mentioned_services(kit, registry)]
    return items


def matches_title(kit: Kit, item: ShortlistItem) -> bool:
    """Тег издания — слово из названия программы («python» в «Первые шаги в Python»): самое надёжное попадание."""
    words = set(normalize_text(kit.program.title).split())
    tags = getattr(item.record, "tags", []) or []
    return any(normalize_text(tag) in words for tag in tags)


def on_topic(kit: Kit, item: ShortlistItem) -> bool:
    """Издание по теме программы: документация упомянутого инструмента или совпадение тегов каталога с тегами тем
    (совпадение одних слов названия — «Python» в программе по 1С — не в счёт)."""
    if isinstance(item.record, (ToolRecord, AIServiceRecord)):
        return True
    wanted = {normalize_text(tag) for topic in kit.topics for tag in topic.tags} - GENERIC_TAGS
    return bool(wanted & {normalize_text(tag) for tag in item.record.tags})


# теги уровня и формы, а не предмета: «основы» связывал курс Python с учебником по защите информации (21.10)
GENERIC_TAGS = frozenset(
    {"основы", "начинающие", "документация", "теория", "методы", "классика", "проект", "проекты", "практика"}
)
MIN_ON_TOPIC = 3


def describe_shortlist(items: Iterable[ShortlistItem]) -> str:
    lines = [item.describe() for item in items]
    return "\n".join(lines) if lines else "каталог не нашёл подходящих изданий — предложи кандидатов сам"


# --------------------------------------------------------------------------------------------------
# Проверка кандидатов
# --------------------------------------------------------------------------------------------------


@dataclass
class Verdict:
    status: Status
    reasons: list[str] = field(default_factory=list)
    catalog: LiteratureRecord | None = None


def _host(url: str | None) -> str:
    match = re.match(r"^[a-z]+://([^/:?#]+)", (url or "").strip().lower())
    return match.group(1).removeprefix("www.") if match else ""


def official_hosts(registry: Registry) -> set[str]:
    """Домены официальной документации: tools.json и интернет-ресурсы каталога."""
    hosts = {_host(t.docs_url) for t in registry.tools()}
    hosts |= {_host(item.url) for item in registry.literature_items() if item.kind == "doc"}
    return {h for h in hosts if h}


def _host_allowed(host: str, allowed: set[str]) -> bool:
    return any(host == known or host.endswith("." + known) for known in allowed)


def check_candidate(
    candidate: BookCandidate, registry: Registry, current_year: int, max_age: int = MAX_SOURCE_AGE_YEARS
) -> Verdict:
    title = (candidate.title or "").strip()
    if not title:
        return Verdict("rejected", ["нет названия"])
    record = registry.find_literature(title, candidate.authors)
    if record is not None:
        return Verdict("catalog", catalog=record)
    reasons: list[str] = []
    if candidate.isbn and not isbn_valid(candidate.isbn):
        reasons.append(f"ISBN {candidate.isbn} не проходит проверку контрольной суммы")
    if candidate.year is not None:
        if candidate.year > current_year:
            reasons.append(f"год {candidate.year} ещё не наступил")
        elif candidate.year < current_year - max_age:
            reasons.append(f"издание {candidate.year} года слишком старое для ИТ-программы")
    publisher = (candidate.publisher or "").strip()
    if publisher and not registry.publisher_known(publisher):
        reasons.append(f"издательство «{publisher}» не найдено в списке реальных издательств")
    if not publisher:
        host = _host(candidate.url)
        if not host:
            reasons.append("нет ни издательства, ни ссылки на источник")
        elif not _host_allowed(host, official_hosts(registry)):
            reasons.append(f"сайт {host} не из списка официальной документации")
    return Verdict("rejected", reasons) if reasons else Verdict("unverified")


# --------------------------------------------------------------------------------------------------
# Сборка списка источников
# --------------------------------------------------------------------------------------------------


@dataclass
class LiteratureResult:
    sources: list[Source] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)  # отброшенные и неподтверждённые кандидаты
    notes: list[str] = field(default_factory=list)  # для отчёта методисту


def _source_from_item(
    item: ShortlistItem, section: SourceSection, source_id: str, topic_ids: list[str]
) -> Source:
    record = item.record
    # запись каталога со status ≠ verified (добавлена без проверки) выводится с меткой «[не подтверждено]»
    checked = Verification.CATALOG if is_verified(record) else Verification.UNVERIFIED
    if isinstance(record, AIServiceRecord):
        return Source(
            id=source_id,
            kind=SourceKind.DOC,
            section=SourceSection.INTERNET,
            registry_id=record.id,
            verification=checked,
            title=f"{record.name}: официальный сайт сервиса",
            url=record.url,
            topic_ids=topic_ids,
        )
    if isinstance(record, ToolRecord):
        return Source(
            id=source_id,
            kind=SourceKind.DOC,
            section=SourceSection.INTERNET,
            registry_id=record.id,
            verification=checked,
            title=f"{record.name}: официальная документация",
            url=record.docs_url,
            topic_ids=topic_ids,
        )
    is_doc = record.kind == "doc"
    return Source(
        id=source_id,
        kind=SourceKind.DOC if is_doc else SourceKind.BOOK,
        section=SourceSection.INTERNET if is_doc else section,
        registry_id=record.id,
        verification=checked,
        authors=list(record.authors),
        title=record.title,
        year=record.year,
        publisher=record.publisher,
        isbn=record.isbn,
        url=record.url,
        topic_ids=topic_ids,
    )


def _book_section(section: SourceSection) -> SourceSection:
    return section if section in BOOK_SECTIONS else SourceSection.ADDITIONAL


class _Collector:
    def __init__(
        self, kit: Kit, alloc: IdAllocator, max_per_section: Mapping[SourceSection, int] | None = None
    ) -> None:
        self.kit = kit
        self.alloc = alloc
        self.max_per_section = max_per_section  # None — значения модуля MAX_PER_SECTION
        self.topic_ids = {t.id for t in kit.topics}
        self.sources: list[Source] = []
        self.used: set[str] = set()  # ключи шорт-листа / ID каталога

    def count(self, section: SourceSection) -> int:
        return sum(1 for s in self.sources if s.section == section)

    def full(self, section: SourceSection) -> bool:
        limits = self.max_per_section if self.max_per_section is not None else MAX_PER_SECTION
        return self.count(section) >= limits.get(section, 6)

    def add_item(self, item: ShortlistItem, section: SourceSection, topic_ids: Iterable[str] = ()) -> bool:
        target = SourceSection.INTERNET if item.is_doc else _book_section(section)
        if target == SourceSection.MAIN and self.full(target):
            target = SourceSection.ADDITIONAL  # лишняя основная книга — в дополнительную, а не отбрасывается
        if item.key in self.used or self.full(target):
            return False
        topics = [t for t in dict.fromkeys(topic_ids) if t in self.topic_ids]
        self.sources.append(_source_from_item(item, target, self.alloc.source(), topics))
        self.used.add(item.key)
        return True


def build_sources(
    kit: Kit,
    registry: Registry,
    items: list[ShortlistItem],
    picks: Iterable[CatalogPick],
    candidates: Iterable[BookCandidate],
    current_year: int,
    max_candidates: int | None = None,
    max_unverified: int | None = None,
    max_per_section: Mapping[str, int] | None = None,
) -> LiteratureResult:
    """Выбор модели + проверенные кандидаты → источники комплекта (ID выдаёт код). Лимиты — из Settings.limits;
    не заданы — значения модуля (MAX_CANDIDATES, MAX_UNVERIFIED_PER_SECTION, MAX_PER_SECTION)."""
    result = LiteratureResult()
    per_section = {SourceSection(k): v for k, v in max_per_section.items()} if max_per_section else None
    max_candidates = MAX_CANDIDATES if max_candidates is None else max_candidates
    max_unverified = MAX_UNVERIFIED_PER_SECTION if max_unverified is None else max_unverified
    collector = _Collector(kit, IdAllocator(kit), per_section)
    by_key = {item.key: item for item in items}
    by_key |= {item.key.split(":", 1)[1]: item for item in items}  # модель может ответить ID без префикса

    for pick in picks:
        item = by_key.get(pick.catalog_id.strip())
        if item is not None:
            collector.add_item(item, pick.section)

    unverified_count: dict[SourceSection, int] = {}
    for candidate in list(candidates)[:max_candidates]:
        url = (candidate.url or "").strip()
        key = CATALOG_KEY_RE.match(url)
        if (
            key
        ):  # модель записала в адрес ключ каталога («lit:…», «doc:…») — это выбор из каталога, а не ссылка
            item = by_key.get(url) or by_key.get(key.group(2))
            if item is not None:
                collector.add_item(item, candidate.section, candidate.topic_ids)
            continue
        if url and not url.lower().startswith(("http://", "https://")):
            candidate = candidate.model_copy(update={"url": None})  # не адрес — в документ не пойдёт
        if any(
            s.registry_id and title_similarity(candidate.title, s.title) >= DUPLICATE_TITLE_RATIO
            for s in collector.sources
        ):
            continue  # то же издание уже выбрано из каталога (модель повторила его кандидатом)
        verdict = check_candidate(candidate, registry, current_year)
        service = blocked_service(kit, registry, candidate.title)
        if service is not None:
            verdict = Verdict("rejected", [f"упоминает сервис «{service}», недоступный в России"])
        entry = {
            "title": candidate.title,
            "authors": candidate.authors,
            "year": candidate.year,
            "publisher": candidate.publisher,
            "isbn": candidate.isbn,
            "url": candidate.url,
        }
        if verdict.status == "catalog" and verdict.catalog is not None:
            item = ShortlistItem(f"lit:{verdict.catalog.id}", verdict.catalog)
            collector.add_item(item, candidate.section, candidate.topic_ids)
            continue
        if verdict.status == "rejected":
            result.log.append({**entry, "status": "rejected", "reasons": verdict.reasons})
            result.notes.append(
                f"{REJECTED_NOTE_PREFIX} «{candidate.title}», который не удалось подтвердить "
                f"({'; '.join(verdict.reasons)}), — он не включён."
            )
            continue
        section = (
            SourceSection.INTERNET
            if not candidate.publisher and candidate.url
            else _book_section(candidate.section)
        )
        if unverified_count.get(section, 0) >= max_unverified or collector.full(section):
            result.log.append(
                {**entry, "status": "dropped", "reasons": ["лимит неподтверждённых в подразделе"]}
            )
            continue
        unverified_count[section] = unverified_count.get(section, 0) + 1
        result.log.append({**entry, "status": "unverified", "reasons": []})
        collector.sources.append(
            Source(
                id=collector.alloc.source(),
                kind=SourceKind.WEB if section == SourceSection.INTERNET else SourceKind.BOOK,
                section=section,
                verification=Verification.UNVERIFIED,
                authors=[a.strip() for a in candidate.authors if a.strip()],
                title=candidate.title.strip(),
                year=candidate.year,
                publisher=(candidate.publisher or "").strip() or None,
                isbn=(candidate.isbn or "").strip() or None,
                url=(candidate.url or "").strip() or None,
                topic_ids=[t for t in candidate.topic_ids if t in collector.topic_ids],
            )
        )

    # подразделы, где модель выбрала мало, добираются из шорт-листа — только изданиями по теме программы
    for section, minimum in MIN_PER_SECTION.items():
        for item in (i for i in items if on_topic(kit, i)):
            if collector.count(section) >= minimum:
                break
            if item.is_doc == (section == SourceSection.INTERNET):
                collector.add_item(item, section)
    result.sources = collector.sources
    return result


def log_unverified(path: Path, kit: Kit, entries: list[dict], at: str) -> None:
    """Отброшенные и неподтверждённые кандидаты — в журнал для пополнения каталога (по одной строке на кандидата)."""
    if not entries:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for entry in entries:
            row = {"ts": at, "program": kit.program.title, **entry}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
