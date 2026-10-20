"""Строки документа из Kit и реестра — без python-docx (их раскладывают docx_pk.py и docx_dop.py).

Нормативка, профстандарты и трудовые функции — только из data/registry (жёсткое правило 3): чего нет в реестре
или что не проверено, выводится с меткой «[не подтверждено]». Библиографические строки собирает Source.citation.
"""

from __future__ import annotations

from app.core.registry import Registry, is_verified, mark_unverified
from app.models import Kit, SourceSection
from app.render.common import FILL_MARK, upper_first

UNVERIFIED = "[не подтверждено]"


def norm_lines(kit: Kit, registry: Registry) -> list[str]:
    """Нормативные акты программы (kit.program.norm_ids) полными названиями из реестра, затем нормативные
    источники комплекта, которых нет среди norm_ids."""
    lines: list[str] = []
    for norm_id in kit.program.norm_ids:
        record = registry.norm(norm_id)
        if record is None:
            lines.append(f"{norm_id} {UNVERIFIED}")
            continue
        lines.append(record.title if is_verified(record) else mark_unverified(record.title))
    known = set(kit.program.norm_ids)
    for source in kit.sources:
        if source.section == SourceSection.NORMATIVE and source.registry_id not in known:
            lines.append(source.citation)
    return lines


def profstandard_lines(kit: Kit, registry: Registry) -> list[str]:
    """«Профессиональный стандарт 06.015 «Специалист по информационным системам», утверждён приказом …»."""
    lines: list[str] = []
    for code in kit.program.profstandards:
        record = registry.profstandard(code)
        if record is None:
            lines.append(f"Профессиональный стандарт {code} {UNVERIFIED}")
            continue
        order = f", {record.order[:1].lower()}{record.order[1:]}" if record.order else ""
        text = f"Профессиональный стандарт {record.code} «{record.title}»{order}"
        lines.append(text if is_verified(record) else mark_unverified(text))
    return lines


def labor_function_lines(kit: Kit, registry: Registry) -> list[str]:
    """Трудовые функции компетенций: код и название из реестра (повторы — один раз)."""
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for comp in kit.competencies:
        for ref in comp.tf_refs:
            key = (ref.ps_code, ref.tf_code)
            if key in seen:
                continue
            seen.add(key)
            function = registry.labor_function(ref.ps_code, ref.tf_code)
            standard = registry.profstandard(ref.ps_code)
            if function is None or standard is None:
                lines.append(f"{ref.tf_code} (профессиональный стандарт {ref.ps_code}) {UNVERIFIED}")
                continue
            text = (
                f"{function.code} {upper_first(function.title)} (профессиональный стандарт {standard.code})"
            )
            lines.append(text if is_verified(standard) else mark_unverified(text))
    return lines


def competency_lines(kit: Kit, registry: Registry) -> list[str]:
    """1.5 ПК: компетенции в рамках имеющейся квалификации — с кодами ТФ из реестра."""
    lines: list[str] = []
    for n, comp in enumerate(kit.competencies, start=1):
        codes = []
        for ref in comp.tf_refs:
            known = registry.labor_function(ref.ps_code, ref.tf_code) is not None
            codes.append(ref.tf_code if known else f"{ref.tf_code} {UNVERIFIED}")
        tail = f" (трудовые функции: {', '.join(codes)})" if codes else ""
        lines.append(f"ПК-{n}. {upper_first(comp.title.strip()) or FILL_MARK}{tail}")
    return lines


def source_lines(kit: Kit, section: SourceSection) -> list[str]:
    return [s.citation for s in kit.sources if s.section == section]


def software_lines(kit: Kit) -> list[str]:
    lines = [x.strip() for x in kit.texts.software if x and x.strip()]
    lines += [s.citation for s in kit.sources if s.section == SourceSection.SOFTWARE]
    return list(dict.fromkeys(lines))
