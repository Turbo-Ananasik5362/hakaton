"""scripts/run_open_query.py: сценарий через API в том же процессе (mock), запись прогона, защита бюджета."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from app.config import MSK
from app.models import Kit
from tests.intake_queries import OWN, ROOT

SPEC = importlib.util.spec_from_file_location("run_open_query", ROOT / "scripts" / "run_open_query.py")
assert SPEC is not None and SPEC.loader is not None
script = importlib.util.module_from_spec(SPEC)
sys.modules["run_open_query"] = script  # dataclasses скрипта ищут свой модуль в sys.modules
SPEC.loader.exec_module(script)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_query_4_goes_through_questions(tmp_path: Path) -> None:
    out = tmp_path / "q4"
    assert script.main(["--id", "4", "--mode", "mock", "--out", str(out)]) == 0
    transcript = load(out / "transcript.json")
    assert transcript["asked_before_building"] is True
    assert [q["id"] for q in transcript["questions"]] == [
        "program_type",
        "hours",
        "audience",
        "form",
        "final_attestation",
    ]
    assert all(q["answer_source"] == "methodist" for q in transcript["questions"])
    assert [e["action"] for e in transcript["events"]] == ["create", "answer", "approve", "result"]
    assert transcript["final_status"] == "done" and transcript["complete"] is False
    assert any("Содержание комплекта" in item for item in transcript["limitations"])
    assert any(".docx" in item for item in transcript["limitations"])
    kit = Kit.model_validate(load(out / "kit.json"))
    assert kit.program.total_hours == 72 and kit.total_hours() == 72
    assert load(out / "report.json")["status"] in ("ok", "warnings", "errors")
    assert load(out / "skeleton.json")["skeleton"]["declared_total_hours"] == 72
    cost = load(out / "cost.json")
    assert cost["mode"] == "mock" and cost["total_rub"] == 0 and "prepare:skeleton" in cost["step_seconds"]


def test_complete_query_has_no_questions(tmp_path: Path) -> None:
    out = tmp_path / "q1"
    assert script.main(["--id", "1", "--mode", "mock", "--out", str(out)]) == 0
    transcript = load(out / "transcript.json")
    assert transcript["questions"] == [] and transcript["asked_before_building"] is False
    assert transcript["request"]["attachments"] == ["data/templates/dop_intensiv_ai_24h.docx"]
    assert Kit.model_validate(load(out / "kit.json")).program.type.value == "dop"


def test_other_queries_file_and_default_answers(tmp_path: Path) -> None:
    queries = tmp_path / "stress.json"
    queries.write_text(
        json.dumps(
            {
                "queries": [
                    {"id": "s1", "title": "Продажи", "request": OWN["sales_ai"], "methodist_answers": {}}
                ]
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "s1"
    assert script.main(["--id", "s1", "--mode", "mock", "--queries", str(queries), "--out", str(out)]) == 0
    transcript = load(out / "transcript.json")
    assert transcript["questions"] and all(q["answer_source"] == "default" for q in transcript["questions"])
    assert all(q["answer"] == "на ваше усмотрение" for q in transcript["questions"])
    assert transcript["final_status"] == "done"


def test_unknown_query_id(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="нет запроса с id=42"):
        script.main(["--id", "42", "--mode", "mock", "--out", str(tmp_path / "x")])


# ---- бюджет ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "live", "force", "refused"),
    [
        (3, True, False, True),
        (9, True, False, True),
        (3, True, True, False),
        (3, False, False, False),
        (10, True, False, False),
    ],
)
def test_night_guard(hour: int, live: bool, force: bool, refused: bool) -> None:
    now = datetime(2026, 10, 20, hour, 30, tzinfo=MSK)
    assert (script.night_refusal(now, live, force) is not None) == refused


def test_live_at_night_is_refused_before_any_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(script, "now_msk", lambda: datetime(2026, 10, 20, 3, 0, tzinfo=MSK))
    out = tmp_path / "q1"
    assert script.main(["--id", "1", "--mode", "live", "--out", str(out)]) == 2
    assert not out.exists()
    assert script.main(["--id", "1", "--url", "http://127.0.0.1:9", "--out", str(out)]) == 2  # ВМ — тоже live


def test_estimate_is_reasonable() -> None:
    assert 0.5 < script.estimate_rub() < 3.0
    assert script.estimate_rub(with_clarify=False) < script.estimate_rub()


def test_cost_summary_reads_only_this_run(tmp_path: Path) -> None:
    log = tmp_path / "cost.jsonl"
    rows = [
        {"ts": "2026-10-19T10:00:00+03:00", "step": "intake", "cost_rub": 5.0, "prompt_tokens": 100},
        {
            "ts": "2026-10-19T12:00:01+03:00",
            "step": "intake",
            "cost_rub": 0.4,
            "prompt_tokens": 3000,
            "seconds": 2.0,
        },
        {
            "ts": "2026-10-19T12:00:05+03:00",
            "step": "skeleton",
            "cost_rub": 0.0,
            "cached": True,
            "prompt_tokens": 4000,
        },
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\nне json\n", encoding="utf-8")
    summary = script.cost_summary(log, "2026-10-19T12:00:00+03:00", "live", False, [])
    assert summary["total_rub"] == 0.4
    assert summary["by_step"]["intake"]["calls"] == 1 and summary["by_step"]["skeleton"]["cached"] == 1
    remote = script.cost_summary(log, "2026-10-19T12:00:00+03:00", "server", True, [])
    assert remote["by_step"] == {} and "не виден" in remote["note"]
