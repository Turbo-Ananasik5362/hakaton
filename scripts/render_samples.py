#!/usr/bin/env python3
"""Образцы выгрузок по kit-фикстурам открытых запросов: программа .docx, слайды .pptx, тесты GIFT / Moodle XML / CSV,
отчёт автопроверки report.md.

    python scripts/render_samples.py             # выгрузки tests/fixtures/kits/open_q*.json → eval/samples/qN/
    python scripts/render_samples.py --refresh   # сначала пересобрать фикстуры прогоном в mock (без сети и затрат)

Фикстуры — комплекты из mock-прогона /api/v1/run с ответами методиста из eval/open_queries.json (без разобранного
образца: его реквизиты и текст в выгрузку не идут). Для каждого запроса печатается результат проверки V31.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.core.registry import default_registry  # noqa: E402
from app.models import Kit  # noqa: E402
from app.render.bundle import render_all  # noqa: E402
from app.render.common import load_organization  # noqa: E402
from app.render.report_md import write_report_md  # noqa: E402

KITS_DIR = ROOT / "tests" / "fixtures" / "kits"
SAMPLES_DIR = ROOT / "eval" / "samples"


def fixture_path(query_id: int) -> Path:
    return KITS_DIR / f"open_q{query_id}.json"


def refresh_fixtures(queries: list[dict]) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app

    for query in queries:
        attachments = [
            {"filename": Path(p).name, "content_base64": base64.b64encode((ROOT / p).read_bytes()).decode()}
            for p in query.get("attachments", [])
        ]
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp" if (ROOT / ".tmp").is_dir() else None) as tmp:
            settings = Settings(
                llm_mode="mock", sessions_dir=Path(tmp) / "sessions", cost_log=Path(tmp) / "cost.jsonl"
            )
            with TestClient(create_app(settings=settings)) as client:
                body = client.post(
                    "/api/v1/run",
                    json={
                        "request": query["request"],
                        "attachments": attachments,
                        "answers": query.get("methodist_answers", {}),
                    },
                ).json()
        if body.get("status") != "done":
            raise SystemExit(f"Запрос {query['id']}: {body.get('status')} — {body.get('message')}")
        KITS_DIR.mkdir(parents=True, exist_ok=True)
        fixture_path(query["id"]).write_text(
            json.dumps(body["kit"], ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        print(f"фикстура: {fixture_path(query['id']).relative_to(ROOT)}")


def render_samples(ids: list[int]) -> int:
    settings = Settings()
    registry = default_registry()
    org = load_organization(settings.organization_file)
    failed = 0
    for query_id in ids:
        kit = Kit.model_validate_json(fixture_path(query_id).read_text(encoding="utf-8"))
        out_dir = SAMPLES_DIR / f"q{query_id}"
        outcome = render_all(kit, out_dir, registry, org)
        outcome.files["report_md"] = write_report_md(kit, out_dir, (), (), list(outcome.files.values()))
        errors = [i for i in outcome.issues if i.severity.value == "error"]
        failed += bool(errors)
        status = (
            "V31 ok"
            if not outcome.issues
            else f"V31: {len(errors)} ошибок, {len(outcome.issues) - len(errors)} предупр."
        )
        names = ", ".join(outcome.files.values())
        print(
            f"q{query_id}: {out_dir.relative_to(ROOT)}/ — {kit.program.type.value}, {kit.total_hours():g} ч; {names}; {status}"
        )
        for issue in outcome.issues:
            print(f"   {issue.severity.value}: {issue.message}")
        for note in outcome.notes:
            print(f"   заметка: {note}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--refresh", action="store_true", help="пересобрать kit-фикстуры прогоном в mock")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)
    queries = json.loads((ROOT / "eval" / "open_queries.json").read_text(encoding="utf-8"))["queries"]
    if args.refresh:
        refresh_fixtures(queries)
    return render_samples([q["id"] for q in queries])


if __name__ == "__main__":
    sys.exit(main())
