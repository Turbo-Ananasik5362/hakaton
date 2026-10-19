"""Шаг intake: текст запроса и вложения → RequestSpec (docs/ARCHITECTURE.md §1.4, §2.3).

Кто что делает:
- код (app/pipeline/request_rules.py) — числа, тип программы, форма обучения, форма ИА, ограничения, слайды;
- app/core/reference.py — разбор приложенной программы; в LLM уходит только её оглавление;
- LLM (навык app/skills/intake.md, один вызов) — смысловые поля: аудитория и её среда, обязательные темы,
  вводный блок, название, если его нет в кавычках. Числа модель только цитирует — код сверяет цитаты с текстом.

Ответы методиста сюда не попадают: их применяет clarify.apply_answers.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.core.reference import extract_text, parse_reference, reference_outline
from app.llm import LLMClient, LLMError
from app.models import (
    Audience,
    Constraints,
    IntakeDraft,
    Outputs,
    ParsedReference,
    ProgramType,
    RequestSpec,
    SlidesScope,
    StudyForm,
    TopicRequirement,
)
from app.pipeline.base import RequestInput, StepContext
from app.pipeline.request_rules import (
    TYPE_TITLES,
    RequestFacts,
    domain_patterns,
    extract_facts,
    extract_slides,
    first_number,
    found_in,
    normalize,
    spec_conflicts,
    text_conflicts,
    topic_pattern,
)
from app.skills import load_skill

logger = logging.getLogger("karkas.intake")

MAX_REQUEST_CHARS = 20_000
DOC_EXCERPT_CHARS = 1_500
OUTLINE_HEADINGS = 60
CORE_PARAMS = ("program_type", "hours", "audience", "form", "final_attestation")


# --------------------------------------------------------------------------------------------------
# Вложения
# --------------------------------------------------------------------------------------------------


@dataclass
class AttachmentDigest:
    texts: list[str] = field(default_factory=list)  # .txt — часть запроса
    reference: ParsedReference | None = None
    summaries: list[str] = field(default_factory=list)  # для промпта LLM
    documents: list[str] = field(default_factory=list)  # тексты документов без структуры программы


def _read_txt(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _looks_like_program(ref: ParsedReference) -> bool:
    return bool(ref.hour_tables) or len(ref.headings) >= 5


def read_attachments(request: RequestInput, ctx: StepContext) -> AttachmentDigest:
    digest = AttachmentDigest()
    programs: list[tuple[str, ParsedReference]] = []
    for attachment in request.attachments:
        path = ctx.attachment_path(attachment)
        if not path.is_file():
            continue
        name = attachment.filename
        if path.suffix.lower() == ".txt":
            digest.texts.append(_read_txt(path).strip())
            digest.summaries.append(f"Файл «{name}» — текст, он включён в запрос выше.")
            continue
        ref = parse_reference(path, name)
        if _looks_like_program(ref):
            programs.append((name, ref))
            continue
        text = extract_text(path, name).strip()
        if text:
            digest.documents.append(text)
            excerpt = text[:DOC_EXCERPT_CHARS]
            digest.summaries.append(
                f"Файл «{name}» — документ без структуры программы. Начало текста:\n{excerpt}"
            )
        else:
            digest.summaries.append(f"Файл «{name}» — не удалось прочитать текст.")
    if programs:
        name, best = max(programs, key=lambda p: (len(p[1].hour_tables), len(p[1].headings)))
        digest.reference = best
        for other_name, ref in programs:
            kind = (
                TYPE_TITLES.get(ref.program_type_guess, "образовательная программа")
                if ref.program_type_guess
                else "образовательная программа"
            )
            outline = reference_outline(ref, OUTLINE_HEADINGS)
            digest.summaries.insert(0, f"Файл «{other_name}» — {kind}. Оглавление:\n{outline}")
    return digest


def request_text(request: RequestInput, digest: AttachmentDigest) -> str:
    parts = [request.text.strip(), *digest.texts]
    text = "\n\n".join(p for p in parts if p)
    if not text and digest.documents:
        text = digest.documents[0]
    return text[:MAX_REQUEST_CHARS]


def request_key(text: str) -> str:
    """Стабильный ключ запроса (имя mock-фикстуры): не зависит от промпта навыка."""
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------------------------------
# LLM
# --------------------------------------------------------------------------------------------------


async def ask_llm(text: str, digest: AttachmentDigest, llm: LLMClient) -> IntakeDraft:
    skill = load_skill("intake")
    attachments = "\n\n".join(digest.summaries) or "нет"
    messages = skill.messages(request=text or "(текст запроса пуст)", attachments=attachments)
    return await llm.complete_json(
        "intake",
        skill.model,
        messages,
        IntakeDraft,
        max_tokens=skill.max_tokens,
        temperature=skill.temperature,
        fixture_key=request_key(text),
    )


# --------------------------------------------------------------------------------------------------
# Сборка RequestSpec
# --------------------------------------------------------------------------------------------------


# в цитате должна быть единица своего вида, иначе модель подписала число не тем видом
QUOTE_UNITS = {
    "total_hours": r"час|\bч\b",
    "weeks": r"недел",
    "months": r"месяц|полгода",
    "hours_per_day": r"(?:час|\bч\b).*\bд(?:ень|ня)|в\s+день",
    "session_hours": r"заняти|урок|встреч|\bпар|мин|час",
    "sessions_per_week": r"недел",
    "test_questions": r"вопрос|задани",
    "fa_hours": r"час|\bч\b",
    "portfolio_items": r"портфолио|материал|работ|артефакт",
}
INT_QUANTITIES = ("weeks", "test_questions", "portfolio_items", "sessions_per_week")
HOUR_QUANTITIES = ("hours_per_day", "session_hours", "fa_hours")


def _confirm_quantities(text: str, facts: RequestFacts, draft: IntakeDraft, notes: list[str]) -> None:
    """Цитаты модели с количествами сверяются с текстом. Число берётся кодом из цитаты, а не из ответа модели.

    Цитата принимается, только если она есть в запросе дословно, в ней есть единица своего вида и код ещё не
    отнёс это число к другому виду. Расхождение с числом, найденным кодом, по объёму — противоречие (вопрос).
    """
    norm_text = " ".join(normalize(text).lower().split())
    for mention in draft.quantities:
        quote = " ".join(normalize(mention.text).lower().split())
        if not quote or norm_text.find(quote) < 0:
            continue  # цитаты нет в тексте — модель додумала, игнорируем
        number = first_number(quote)
        if number is None or not re.search(QUOTE_UNITS[mention.what], quote, re.IGNORECASE):
            continue
        if any(m.kind != mention.what and m.value == number for m in facts.mentions):
            continue  # код уже отнёс это число к другому виду (объём, часы практики, длина занятия…)
        current = facts.get(mention.what)
        if mention.what == "total_hours":
            if current is None and not facts.total_candidates:
                facts.total_hours = number
                facts.total_candidates = [number]
                notes.append(f"Объём {fmt(number)} ч прочитан из фразы «{mention.text.strip()}».")
            elif number not in facts.total_candidates:
                facts.total_candidates.append(number)
                facts.total_hours = None  # расхождение: спросим методиста
        elif current is None and mention.what in INT_QUANTITIES:
            setattr(facts, mention.what, int(number))
            notes.append(f"Из фразы «{mention.text.strip()}» взято: {fmt(number)}.")
        elif current is None and mention.what in HOUR_QUANTITIES:
            setattr(facts, mention.what, number)
            notes.append(f"Из фразы «{mention.text.strip()}» взято: {fmt(number)} ч.")


def fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}".replace(".", ",")


def resolve_type(
    facts: RequestFacts, draft: IntakeDraft | None, reference: ParsedReference | None
) -> tuple[ProgramType | None, str]:
    """Тип программы и уверенность: explicit (назван в запросе) > audience (по слушателям) > llm > reference."""
    explicit = facts.explicit_types
    if explicit and "conflict_type" not in text_conflicts(facts):
        if ProgramType.PK_INTERNSHIP in explicit:
            return ProgramType.PK_INTERNSHIP, "explicit"
        return explicit[0], "explicit"
    if facts.audience_type is not None:
        return facts.audience_type, "audience"
    if draft is not None and draft.program_type_guess is not None:
        return draft.program_type_guess, "llm"
    if reference is not None and reference.program_type_guess is not None:
        return reference.program_type_guess, "reference"
    return None, "none"


def _requirements(labels: list[str], text: str) -> list[TopicRequirement]:
    """Обязательные темы модели → шаблоны; остаются только те, что действительно есть в тексте запроса."""
    result: list[TopicRequirement] = []
    seen: set[str] = set()
    for raw in labels:
        label = raw.strip().strip("«»\"'.")
        key = normalize(label).lower()
        if not label or key in seen:
            continue
        pattern = topic_pattern(label)
        if not found_in(pattern, text):
            continue
        seen.add(key)
        result.append(TopicRequirement(label=label, patterns=[pattern]))
    return result


def _slides(facts: RequestFacts, draft: IntakeDraft | None, text: str) -> SlidesScope:
    if facts.slides is not None:
        return facts.slides
    if draft is not None and draft.slides_request:
        quote = draft.slides_request.strip()
        if quote and normalize(quote).lower() in normalize(text).lower():
            parsed = extract_slides(
                normalize("слайды " + quote) if "слайд" not in quote.lower() else normalize(quote)
            )
            if parsed is not None:
                return parsed
    return SlidesScope()


def _audience(facts: RequestFacts, draft: IntakeDraft | None) -> Audience:
    llm = draft.audience if draft is not None else Audience()
    category = (llm.category or "").strip() or (facts.audience_phrase or "")
    entry = llm.entry_level.strip() if llm.entry_level and facts.entry_level_stated else None
    return Audience(
        category=category,
        age_or_grade=facts.age_or_grade or (llm.age_or_grade or None),
        entry_level=entry,
        requirements=llm.requirements or None,
        domain=llm.domain or None,
        domain_patterns=domain_patterns(llm.domain_patterns, llm.domain),
        constraints=[c for c in llm.constraints if c.strip()],
    )


def build_spec(
    text: str,
    facts: RequestFacts,
    draft: IntakeDraft | None,
    reference: ParsedReference | None,
    notes: list[str] | None = None,
) -> RequestSpec:
    notes = list(notes or [])
    if draft is not None:
        _confirm_quantities(text, facts, draft, notes)
    notes = [*facts.notes, *notes]

    program_type, level = resolve_type(facts, draft, reference)
    audience = _audience(facts, draft)

    form = None
    if facts.form_kind is not None or facts.dot:
        form = StudyForm(
            kind=facts.form_kind or StudyForm().kind,
            dot=facts.dot,
            eo=facts.eo or bool(draft and draft.eo),
            distance_share=facts.distance_share,
        )
    # догадку модели о форме не берём: без признака в тексте форма — умолчание шаблона (clarify.finalize_spec)

    title = facts.title
    if not title and draft is not None and draft.title:
        title = draft.title.strip().strip('«»"')
        notes.append(f"Название в запросе не указано — предложено: «{title}».")

    practice_share = facts.practice_share
    if practice_share is None and facts.practice_hours and facts.total_hours:
        practice_share = min(1.0, facts.practice_hours / facts.total_hours)

    constraints = Constraints(
        ru_services_only=facts.ru_services_only or bool(draft and draft.ru_services_only),
        min_practice_share=practice_share,
        all_srs=facts.all_srs,
        no_programming=facts.no_programming or bool(draft and draft.no_programming),
        anonymized_data=facts.anonymized_data or bool(draft and draft.anonymized_data),
        intro_block_topic=(draft.intro_block_topic or None) if draft is not None else None,
        # у стажировки и программ «вся работа самостоятельная» семинаров нет — требований к их видам тоже
        required_seminar_types=(
            _requirements(draft.required_seminar_types, text)
            if draft and program_type != ProgramType.PK_INTERNSHIP and not facts.all_srs
            else []
        ),
    )

    spec = RequestSpec(
        raw_text=text,
        program_type=program_type,
        title=title or None,
        total_hours=facts.total_hours,
        audience=audience,
        form=form,
        weeks=facts.weeks,
        duration_text=facts.duration_text,
        hours_per_day_max=facts.hours_per_day,
        session_hours=facts.session_hours,
        sessions_per_week=facts.sessions_per_week,
        fa_form=facts.fa_form or (draft.fa_form if draft is not None else None),
        fa_text=facts.fa_text or ((draft.fa_text or None) if draft is not None else None),
        fa_hours=facts.fa_hours,
        portfolio_items_count=facts.portfolio_items,
        required_topics=_requirements(draft.required_topics, text) if draft else [],
        constraints=constraints,
        outputs=Outputs(test_questions=facts.test_questions, slides_scope=_slides(facts, draft, text)),
        reference=reference,
    )

    missing: list[str] = []
    if level != "explicit":
        missing.append("program_type")
    if spec.total_hours is None:
        missing.append("hours")
    if not audience.category or not audience.entry_level:
        missing.append("audience")
    if facts.form_kind is None:
        missing.append("form")
    if facts.fa_form is None:
        missing.append("final_attestation")
    missing += text_conflicts(facts)
    missing += [c for c in spec_conflicts(spec) if c not in missing]
    spec.missing = missing

    # допущения пишет только код: замечания модели о неясностях устаревают после ответов методиста,
    # а неясности и так закрывает clarify
    spec.assumptions = list(dict.fromkeys(notes))
    return spec


async def run_intake(request: RequestInput, ctx: StepContext, llm: LLMClient | None) -> RequestSpec:
    digest = read_attachments(request, ctx)
    text = request_text(request, digest)
    facts = extract_facts(text)
    draft: IntakeDraft | None = None
    notes: list[str] = list(request.notes)
    if llm is not None:
        try:
            draft = await ask_llm(text, digest, llm)
        except LLMError as exc:
            logger.warning("intake: модель недоступна, разбор только правилами: %s", exc)
            notes.append("Смысловой разбор запроса моделью не выполнен — параметры взяты только по правилам.")
    return build_spec(text, facts, draft, digest.reference, notes)
