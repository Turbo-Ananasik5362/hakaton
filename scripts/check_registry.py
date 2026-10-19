"""Формальная проверка реестра data/registry перед коммитом: `python scripts/check_registry.py [каталог]`.

Не сверяет факты с первоисточниками (это делается вручную по source_url), а ловит то, что ломает агента:
битые записи, verified без источника, неверные ISBN и коды ТФ, неизвестные издательства, висячие ссылки
replaced_by, повторяющиеся алиасы сервисов и издательств. Код выхода 1 — есть ошибки.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.registry import DEFAULT_REGISTRY_DIR, Registry, load_registry  # noqa: E402
from app.core.registry_api import RegistryRecord, normalize_text  # noqa: E402

# Слаги направлений литературы (docs/ARCHITECTURE.md §2.4): у каждой записи каталога — хотя бы один.
DIRECTION_SLUGS = frozenset(
    {
        "python",
        "data",
        "ml",
        "ai",
        "education",
        "security",
        "networks",
        "linux",
        "devops",
        "web",
        "databases",
        "testing",
        "analysis",
        "pm",
        "digital",
    }
)
MIN_PER_DIRECTION = 5
NORM_KINDS = frozenset({"federal_law", "order", "decree", "letter"})
PS_CODE_RE = re.compile(r"^\d{2}\.\d{3}$")
TF_CODE_RE = re.compile(r"^[A-Z]/\d{2}\.\d{1,2}$")
VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,3}$")


@dataclass
class CheckResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


# --------------------------------------------------------------------------------------------------
# ISBN
# --------------------------------------------------------------------------------------------------


def isbn_valid(isbn: str) -> bool:
    """Контрольная сумма ISBN-10 или ISBN-13 (дефисы и пробелы игнорируются, X — только последним в ISBN-10)."""
    digits = re.sub(r"[\s-]", "", isbn).upper()
    if re.fullmatch(r"\d{13}", digits):
        total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits))
        return total % 10 == 0
    if re.fullmatch(r"\d{9}[\dX]", digits):
        total = sum((10 - i) * (10 if d == "X" else int(d)) for i, d in enumerate(digits))
        return total % 11 == 0
    return False


# --------------------------------------------------------------------------------------------------
# Проверки по типам записей
# --------------------------------------------------------------------------------------------------


def _check_status(where: str, record: RegistryRecord, today: date, result: CheckResult) -> None:
    if record.status == "verified":
        if not record.source_url:
            result.errors.append(f"{where}: verified без source_url")
        if not record.checked_at:
            result.errors.append(f"{where}: verified без checked_at")
    if record.checked_at:
        try:
            checked = date.fromisoformat(record.checked_at)
        except ValueError:
            result.errors.append(f"{where}: checked_at не дата ISO ({record.checked_at!r})")
        else:
            if checked > today:
                result.errors.append(f"{where}: checked_at из будущего ({record.checked_at})")
    if record.source_url and not record.source_url.startswith(("https://", "http://")):
        result.errors.append(f"{where}: source_url не ссылка ({record.source_url!r})")


def check_norms(registry: Registry, today: date, result: CheckResult) -> None:
    ids = {n.id for n in registry.norms()}
    for norm in registry.norms():
        where = f"norms.json/{norm.id}"
        _check_status(where, norm, today, result)
        if norm.kind not in NORM_KINDS:
            result.errors.append(f"{where}: kind {norm.kind!r} не из {sorted(NORM_KINDS)}")
        if not set(norm.applies_to) <= {"pk", "dop"} or not norm.applies_to:
            result.errors.append(f"{where}: applies_to должен быть из pk, dop ({norm.applies_to})")
        for link in ("replaces", "replaced_by"):
            target = getattr(norm, link)
            if target and target not in ids:
                result.errors.append(f"{where}: {link} → {target!r}, такой записи нет")
        if not norm.in_force and not (norm.replaced_by or norm.in_force_until):
            result.warnings.append(f"{where}: не действует, но не указано, чем заменён и до какой даты")
    result.stats["norms"] = len(ids)


def check_profstandards(registry: Registry, today: date, result: CheckResult) -> None:
    tag_owners: Counter[str] = Counter()
    for standard in registry.profstandards():
        where = f"profstandards.json/{standard.code}"
        _check_status(where, standard, today, result)
        if not PS_CODE_RE.match(standard.code):
            result.errors.append(f"{where}: код ПС не вида 06.015")
        if not standard.order:
            result.warnings.append(f"{where}: не указан приказ")
        if not standard.labor_functions:
            result.errors.append(f"{where}: нет трудовых функций")
        codes = [f.code for f in standard.labor_functions]
        for code, count in Counter(codes).items():
            if count > 1:
                result.errors.append(f"{where}: ТФ {code} повторяется")
        for function in standard.labor_functions:
            if not TF_CODE_RE.match(function.code):
                result.errors.append(f"{where}: код ТФ {function.code!r} не вида C/11.6 (буква латиницей)")
            elif function.otf and function.otf != function.code[0]:
                result.errors.append(
                    f"{where}: ТФ {function.code} — ОТФ {function.otf!r} не совпадает с буквой кода"
                )
        if not standard.tags:
            result.warnings.append(f"{where}: нет тегов — стандарт найдётся только по словам названия")
        for tag in standard.tags:
            if normalize_text(tag) != tag or " " in tag:
                result.errors.append(f"{where}: тег {tag!r} — нужно одно слово в нижнем регистре без ё")
            tag_owners[tag] += 1
    shared = sorted(tag for tag, count in tag_owners.items() if count > 2)
    if shared:
        result.warnings.append(
            f"profstandards.json: теги у 3+ стандартов не различают их: {', '.join(shared)}"
        )
    result.stats["profstandards"] = len(registry.profstandards())
    result.stats["labor_functions"] = sum(len(s.labor_functions) for s in registry.profstandards())


def check_ai_services(registry: Registry, today: date, result: CheckResult) -> None:
    owners: dict[str, str] = {}
    for service in registry.ai_services():
        where = f"ru_ai_services.json/{service.id}"
        _check_status(where, service, today, result)
        if not service.kinds:
            result.warnings.append(f"{where}: не указаны kinds")
        for name in service.all_names():
            key = normalize_text(name)
            if key in owners and owners[key] != service.id:
                result.errors.append(f"{where}: имя {name!r} уже есть у {owners[key]}")
            owners.setdefault(key, service.id)
    result.stats["ai_services_ru"] = sum(1 for s in registry.ai_services() if s.available_in_ru)
    result.stats["ai_services_stop"] = sum(1 for s in registry.ai_services() if not s.available_in_ru)


def check_tools(registry: Registry, today: date, result: CheckResult) -> None:
    owners: dict[str, str] = {}
    for tool in registry.tools():
        where = f"tools.json/{tool.id}"
        _check_status(where, tool, today, result)
        if tool.min_version and not VERSION_RE.match(tool.min_version):
            result.errors.append(
                f"{where}: min_version {tool.min_version!r} не вида 3.10 (проверка сравнивает числа)"
            )
        if not tool.docs_url:
            result.warnings.append(f"{where}: нет docs_url")
        for name in tool.all_names():
            key = normalize_text(name)
            if key in owners and owners[key] != tool.id:
                result.errors.append(f"{where}: имя {name!r} уже есть у {owners[key]}")
            owners.setdefault(key, tool.id)
    result.stats["tools"] = len(registry.tools())


def check_literature(registry: Registry, today: date, result: CheckResult) -> None:
    per_direction: Counter[str] = Counter()
    isbns: dict[str, str] = {}
    for item in registry.literature_items():
        where = f"literature.json/{item.id}"
        _check_status(where, item, today, result)
        slugs = DIRECTION_SLUGS & set(item.tags)
        if not slugs:
            result.errors.append(f"{where}: нет тега направления из {sorted(DIRECTION_SLUGS)}")
        if item.status == "verified":
            per_direction.update(slugs)
        if item.year is not None and item.year > today.year:
            result.errors.append(f"{where}: год {item.year} из будущего")
        if item.kind == "book":
            if not item.authors:
                result.warnings.append(f"{where}: у книги нет авторов")
            if not item.publisher:
                result.errors.append(f"{where}: у книги нет издательства")
            elif not registry.publisher_known(item.publisher):
                result.errors.append(f"{where}: издательства {item.publisher!r} нет в publishers.json")
            if not item.isbn:
                result.warnings.append(f"{where}: у книги нет ISBN")
        if item.isbn:
            if not isbn_valid(item.isbn):
                result.errors.append(f"{where}: ISBN {item.isbn} — неверная контрольная сумма")
            key = re.sub(r"[\s-]", "", item.isbn)
            if key in isbns:
                result.errors.append(f"{where}: ISBN {item.isbn} уже у {isbns[key]}")
            isbns.setdefault(key, item.id)
    for slug in sorted(DIRECTION_SLUGS):
        if per_direction[slug] < MIN_PER_DIRECTION:
            result.warnings.append(
                f"literature.json: направление {slug} — подтверждённых записей {per_direction[slug]} "
                f"(нужно ≥ {MIN_PER_DIRECTION})"
            )
    result.stats["literature"] = len(registry.literature_items())
    result.stats["literature_verified"] = sum(
        1 for i in registry.literature_items() if i.status == "verified"
    )


def check_publishers(registry: Registry, today: date, result: CheckResult) -> None:
    owners: dict[str, str] = {}
    for publisher in registry._publishers:  # noqa: SLF001 — в RegistryView нет списка издательств
        where = f"publishers.json/{publisher.id}"
        _check_status(where, publisher, today, result)
        for name in publisher.all_names():
            key = normalize_text(name)
            if key in owners and owners[key] != publisher.id:
                result.errors.append(f"{where}: имя {name!r} уже есть у {owners[key]}")
            owners.setdefault(key, publisher.id)
    result.stats["publishers"] = len(registry._publishers)  # noqa: SLF001


def check_registry(directory: Path | None = None, today: date | None = None) -> CheckResult:
    directory = directory or DEFAULT_REGISTRY_DIR
    today = today or date.today()
    result = CheckResult()
    registry = load_registry(directory)
    result.errors.extend(f"загрузка: {w}" for w in registry.load_warnings)
    result.warnings.extend(f"нет файла {name}" for name in registry.missing_files)
    check_norms(registry, today, result)
    check_profstandards(registry, today, result)
    check_ai_services(registry, today, result)
    check_tools(registry, today, result)
    check_publishers(registry, today, result)
    check_literature(registry, today, result)
    return result


def main(argv: list[str]) -> int:
    directory = Path(argv[1]) if len(argv) > 1 else None
    result = check_registry(directory)
    print("Записей:", ", ".join(f"{k} {v}" for k, v in result.stats.items()))
    for warning in result.warnings:
        print(f"  предупреждение: {warning}")
    for error in result.errors:
        print(f"  ОШИБКА: {error}")
    print("OK" if result.ok else f"Ошибок: {len(result.errors)}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
