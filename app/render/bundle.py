"""Выгрузки: программа .docx, слайды .pptx, тесты GIFT / Moodle XML / CSV с V31 и отдельный report.docx.

Каждая выгрузка независима: сбой одной даёт понятную заметку и замечание V31, остальные файлы выдаются.
Нет материала (лекций со слайдами, тестовых вопросов) — файла нет, а в заметке сказано почему.
kit.json и report.json пишет DefaultPipeline.render последними, чтобы отчёт уже включал V31.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.core.registry import Registry
from app.models import FixBy, Issue, Kit, Severity, ValidationReport
from app.render.common import Organization
from app.render.csv_tests import CSV_NAME, render_csv
from app.render.gift import GIFT_NAME, render_gift
from app.render.moodle_xml import XML_NAME, render_moodle_xml
from app.render.pptx import PPTX_NAME, deck_lectures, lecture_slides, render_pptx
from app.render.program import DOCX_NAME, render_program_docx
from app.render.questions import question_bank
from app.render.report_docx import REPORT_DOCX_NAME, render_report_docx
from app.render.report_md import ReportDoc, build_report
from app.render.verify import CHECK, REFS, verify_pptx, verify_tests

logger = logging.getLogger("karkas.render")

FAILED_NOTE = (
    "{title}: файл не сформирован из-за внутренней ошибки выгрузки ({error}). Остальные файлы и комплект в JSON "
    "выданы; повторите выгрузку после правки или сообщите разработчикам."
)
V31_NOTE = "Проверка выгрузок нашла расхождения файлов с комплектом — подробности в отчёте (V31)."
NO_SLIDES_NOTE = (
    "Слайды не сформированы: в программе нет лекций, к которым нужны слайды (нет теоретических занятий "
    "или они не попали в запрошенные занятия / модуль)."
)
NO_TESTS_NOTE = (
    "Тестовых вопросов в комплекте нет (текущий контроль — {forms}), поэтому файлы тестов GIFT, Moodle XML и CSV "
    "не сформированы."
)


@dataclass
class RenderOutcome:
    files: dict[str, str] = field(default_factory=dict)  # ключ → имя файла в out_dir
    issues: list[Issue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def render_all(
    kit: Kit,
    out_dir: Path,
    registry: Registry,
    org: Organization,
    notes: Sequence[str] = (),
    references: Sequence[str] = (),
) -> RenderOutcome:
    """notes и references используются в отдельном отчёте методисту report.docx."""
    outcome = RenderOutcome()
    steps: list[tuple[str, list[str], Callable[[], None]]] = [
        (
            "Программа .docx",
            [DOCX_NAME],
            lambda: _program(kit, out_dir, registry, org, outcome),
        ),
        ("Слайды .pptx", [PPTX_NAME], lambda: _slides(kit, out_dir, outcome)),
        (
            "Тесты GIFT, Moodle XML и CSV",
            [GIFT_NAME, XML_NAME, CSV_NAME],
            lambda: _tests(kit, out_dir, outcome),
        ),
        (
            "Отчёт автопроверки .docx",
            [REPORT_DOCX_NAME],
            lambda: _report(kit, out_dir, notes, references, outcome),
        ),
    ]
    for title, names, run in steps:
        try:
            run()
        except Exception as exc:  # noqa: BLE001 — сбой одной выгрузки не лишает методиста остальных
            logger.exception("Выгрузка «%s» не удалась", title)
            for name in names:
                (out_dir / name).unlink(missing_ok=True)
            outcome.files = {k: v for k, v in outcome.files.items() if v not in names}
            # методисту — только вид ошибки; текст исключения (могут быть пути сервера) — в отчёт разработчику
            outcome.notes.append(FAILED_NOTE.format(title=title, error=type(exc).__name__))
            outcome.issues.append(failure_issue(title, error_detail(exc)))
    if any(i.severity == Severity.ERROR for i in outcome.issues) and not any(
        n.startswith(title) for title, _, _ in steps for n in outcome.notes
    ):
        outcome.notes.append(V31_NOTE)
    outcome.issues = [
        i.model_copy(update={"id": f"{CHECK}-{n}"}) for n, i in enumerate(outcome.issues, start=1)
    ]
    return outcome


def _program(
    kit: Kit,
    out_dir: Path,
    registry: Registry,
    org: Organization,
    outcome: RenderOutcome,
) -> None:
    # Общий рендер сохраняет прежний аргумент отчёта для совместимости; программа его больше не использует.
    report = ReportDoc(title="", status=None, summary="")
    name, issues = render_program_docx(kit, out_dir, registry, org, report)
    outcome.files["program_docx"] = name
    outcome.issues += issues


def _report(
    kit: Kit,
    out_dir: Path,
    notes: Sequence[str],
    references: Sequence[str],
    outcome: RenderOutcome,
) -> None:
    # Основной пайплайн присоединит V31 позже; для Word-отчёта используем копию уже с результатами выгрузок.
    work = kit.model_copy(deep=True)
    if work.report is None:
        work.report = ValidationReport()
    work.report.issues = [issue for issue in work.report.issues if issue.check != CHECK] + outcome.issues
    report = build_report(
        work,
        [*notes, *outcome.notes],
        references,
        files=[*outcome.files.values(), REPORT_DOCX_NAME],
    )
    outcome.files["report_docx"] = render_report_docx(report, out_dir / REPORT_DOCX_NAME)


def _slides(kit: Kit, out_dir: Path, outcome: RenderOutcome) -> None:
    path = out_dir / PPTX_NAME
    if render_pptx(kit, path) is None:
        path.unlink(missing_ok=True)
        outcome.notes.append(NO_SLIDES_NOTE)
        return
    outcome.files["slides_pptx"] = PPTX_NAME
    expected = [(lecture.title, len(lecture_slides(kit, lecture))) for lecture in deck_lectures(kit)]
    outcome.issues += verify_pptx(path, expected)


def material_notes(kit: Kit) -> list[str]:
    """Почему каких-то выгрузок не будет: нет лекций со слайдами, нет тестовых вопросов."""
    notes: list[str] = []
    if not deck_lectures(kit):
        notes.append(NO_SLIDES_NOTE)
    if not question_bank(kit):
        forms = sorted({(m.control_form or "").strip() for m in kit.modules if m.control_form}) or [
            "не задан"
        ]
        notes.append(NO_TESTS_NOTE.format(forms=", ".join(forms).lower()))
    return notes


def _tests(kit: Kit, out_dir: Path, outcome: RenderOutcome) -> None:
    count = len(question_bank(kit))
    if not count:
        outcome.notes += [n for n in material_notes(kit) if n != NO_SLIDES_NOTE]
        return
    written: dict[str, Path] = {}
    for key, label, name, fn in (
        ("tests_gift", "GIFT", GIFT_NAME, render_gift),
        ("tests_moodle_xml", "Moodle XML", XML_NAME, render_moodle_xml),
        ("tests_csv", "CSV", CSV_NAME, render_csv),
    ):
        fn(kit, out_dir / name)
        outcome.files[key] = name
        written[label] = out_dir / name
    outcome.issues += verify_tests(written, count)


def error_detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"


def failure_issue(title: str, error: str) -> Issue:
    return Issue(
        id=f"{CHECK}-1",
        check=CHECK,
        severity=Severity.ERROR,
        message=f"{title}: файл не сформирован из-за внутренней ошибки выгрузки; остальные файлы и JSON выданы.",
        detail=error,
        checklist_refs=list(REFS),
        fix_by=FixBy.CODE,
    )
