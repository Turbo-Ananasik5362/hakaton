"""API (контракт docs/api_contract.md) через TestClient (в том же процессе, без сети и без LLM).

Сценарии: уточнения → каркас → утверждение → комплект; синхронный и асинхронный режимы; очередь на 2 генерации;
продолжение после перезапуска; правка; ошибки ввода — понятный JSON, а не 500.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Kit
from app.pipeline.base import StepUnavailable
from tests.fake_pipeline import FakePipeline

ROOT_DIR = Path(__file__).resolve().parent.parent
TEXT = "Нужна программа повышения квалификации для аналитиков банка."
ANSWERS = {"hours": 36}


class Api:
    """Фабрика клиентов над одной папкой сессий (для проверки перезапуска)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.open: list[TestClient] = []

    def client(self, pipeline: FakePipeline | None = None, max_generating: int = 2) -> TestClient:
        settings = Settings(sessions_dir=self.root / "sessions")
        app = create_app(
            settings=settings, pipeline=pipeline or FakePipeline(), max_generating=max_generating
        )
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()
        self.open.append(client)
        return client

    def close(self, client: TestClient) -> None:
        self.open.remove(client)
        client.__exit__(None, None, None)


@pytest.fixture
def api(tmp_path: Path) -> Iterator[Api]:
    factory = Api(tmp_path)
    yield factory
    for client in list(factory.open):
        factory.close(client)


def wait_for(client: TestClient, sid: str, statuses: set[str], timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/sessions/{sid}").json()
        if body["status"] in statuses:
            return body
        time.sleep(0.01)
    raise AssertionError(f"не дождались {statuses}, сейчас {body.get('status')}")


def step_status(body: dict[str, Any], name: str) -> str | None:
    for step in body.get("progress", {}).get("steps", []):
        if step["name"] == name:
            return step["status"]
    return None


def skeleton_ready(client: TestClient) -> dict[str, Any]:
    body = client.post("/api/v1/sessions", json={"request": TEXT, "answers": ANSWERS}).json()
    assert body["status"] == "skeleton_ready", body
    return body


def assert_error(resp: Any, status: int, code: str | None = None) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["status"] == "error" and body["error"]["message"], body
    if code is not None:
        assert body["error"]["code"] == code, body
    return body


# ---- служебное -------------------------------------------------------------------------------------


def test_health_and_index(api: Api) -> None:
    client = api.client()
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["llm_mode"] == "mock" and health["llm_ready"] is False
    assert set(health) == {"status", "llm_mode", "llm_ready", "version"} and health["version"]
    index = client.get("/").json()
    assert index["docs"] == "/docs" and index["version"] == health["version"]


def test_importable_app_uses_default_pipeline() -> None:
    from app.main import app

    assert app.state.orchestrator.pipeline.__class__.__name__ == "DefaultPipeline"


# ---- уточнения и каркас ----------------------------------------------------------------------------


def test_create_asks_clarifying_questions(api: Api) -> None:
    client = api.client()
    body = client.post("/api/v1/sessions", json={"request": TEXT}).json()
    assert body["status"] == "need_clarification"
    assert [q["id"] for q in body["questions"]] == ["hours"]
    assert body["questions"][0]["options"]
    assert "/answers" in body["message"]
    assert "skeleton" not in body and "kit" not in body


def test_answers_lead_to_consistent_skeleton(api: Api) -> None:
    client = api.client()
    sid = client.post("/api/v1/sessions", json={"request": TEXT}).json()["session_id"]
    body = client.post(f"/api/v1/sessions/{sid}/answers", json={"answers": ANSWERS}).json()
    assert body["status"] == "skeleton_ready"
    skeleton = body["skeleton"]
    modules_total = sum(m["hours"]["total"] for m in skeleton["modules"])
    assert modules_total + skeleton["final_attestation"]["hours"] == skeleton["declared_total_hours"]
    assert skeleton["total_hours"] == skeleton["declared_total_hours"]
    assert all(t["number"] for m in skeleton["modules"] for t in m["topics"])
    assert skeleton["competencies"][0]["outcomes"]
    assert skeleton["assumptions"]
    assert body["validation"]["status"] != "errors"  # у ПК всегда есть предупреждение № 499 → № 266
    assert "kit" not in body and "/approve" in body["message"]
    assert {s["name"]: s["status"] for s in body["progress"]["steps"]} == {
        "intake": "done",
        "clarify": "done",
        "skeleton": "done",
    }


def test_answers_sent_with_request_skip_questions(api: Api) -> None:
    pipeline = FakePipeline()
    client = api.client(pipeline)
    skeleton_ready(client)
    assert pipeline.calls["clarify"] == 1 and pipeline.calls["intake"] == 1


def test_auto_approve_still_asks_when_data_is_missing(api: Api) -> None:
    client = api.client()
    body = client.post("/api/v1/sessions", json={"request": TEXT, "auto_approve": True}).json()
    assert body["status"] == "need_clarification"
    body = client.post(f"/api/v1/sessions/{body['session_id']}/answers", json={"answers": ANSWERS}).json()
    assert body["status"] == "done"
    assert body["kit"] and body["files"]["kit_json"]


def test_run_returns_kit_report_and_files(api: Api) -> None:
    client = api.client()
    body = client.post("/api/v1/run", json={"request": TEXT, "answers": ANSWERS}).json()
    assert body["status"] == "done", body
    kit = Kit.model_validate(body["kit"])
    assert kit.total_hours() == kit.program.total_hours
    assert body["report"]["status"] != "errors"
    assert set(body["files"]) == {"kit_json", "report_json", "program_docx"}

    resp = client.get(body["files"]["kit_json"])
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/json")
    assert {t.id for t in Kit.model_validate_json(resp.content).topics} == {t.id for t in kit.topics}
    resp = client.get(body["files"]["program_docx"])
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument")


def test_run_without_enough_data_returns_questions(api: Api) -> None:
    client = api.client()
    body = client.post("/api/v1/run", json={"request": TEXT}).json()
    assert body["status"] == "need_clarification" and body["questions"]


# ---- утверждение и генерация -----------------------------------------------------------------------


def test_approve_async_shows_progress_then_done(api: Api) -> None:
    pipeline = FakePipeline(hold={"tests"})
    client = api.client(pipeline)
    sid = skeleton_ready(client)["session_id"]

    body = client.post(f"/api/v1/sessions/{sid}/approve").json()
    assert body["status"] == "generating"
    assert "skeleton" in body and "progress" in body

    deadline = time.monotonic() + 5
    while step_status(body, "content:M2") != "done" and time.monotonic() < deadline:
        body = client.get(f"/api/v1/sessions/{sid}").json()
    assert body["status"] == "generating"
    assert step_status(body, "tests") == "running" and "Шаг tests" in body["progress"]["current"]

    pipeline.release.set()
    body = wait_for(client, sid, {"done"})
    assert "[content:M1]" in body["kit"]["texts"]["org_process"]
    assert step_status(body, "render") == "done"


def test_approve_wait_returns_done(api: Api) -> None:
    client = api.client()
    sid = skeleton_ready(client)["session_id"]
    body = client.post(f"/api/v1/sessions/{sid}/approve?wait=true").json()
    assert body["status"] == "done" and body["report"]["status"] != "errors"


def test_approve_with_skeleton_edits(api: Api) -> None:
    client = api.client()
    body = skeleton_ready(client)
    sid = body["session_id"]
    topic = body["skeleton"]["modules"][0]["topics"][0]
    hours = {k: topic["hours"][k] for k in ("lk", "pr", "lb", "srs", "distance")}
    hours["pr"] += 1
    edits = {
        "title": "Аналитика данных в банке",
        "topics": [{"id": f"тема {topic['number']}", "title": "Знакомство с данными", "hours": hours}],
    }
    body = client.post(f"/api/v1/sessions/{sid}/approve?wait=true", json={"skeleton": edits}).json()
    assert body["status"] == "done", body
    kit = Kit.model_validate(body["kit"])
    edited = next(t for t in kit.topics if t.id == topic["id"])
    assert kit.program.title == "Аналитика данных в банке" and edited.title == "Знакомство с данными"
    assert {k: getattr(edited.hours, k) for k in hours} == hours
    assert kit.total_hours() == kit.program.total_hours


def test_approve_edit_conflict_changes_nothing(api: Api) -> None:
    client = api.client()
    body = skeleton_ready(client)
    sid = body["session_id"]
    topic_id = body["skeleton"]["modules"][0]["topics"][0]["id"]
    edits = {"title": "Другое", "topics": [{"id": topic_id, "hours": {"srs": 1000}}]}
    err = assert_error(
        client.post(f"/api/v1/sessions/{sid}/approve", json={"skeleton": edits}), 422, "hours_conflict"
    )
    assert err["error"]["details"]
    after = client.get(f"/api/v1/sessions/{sid}").json()
    assert after["status"] == "skeleton_ready"
    assert after["skeleton"]["program"]["title"] == body["skeleton"]["program"]["title"]


@pytest.mark.parametrize(
    ("edits", "code", "needle"),
    [
        ({"topics": [{"id": "T9.9", "title": "x"}]}, "unknown_target", "T9.9"),
        ({"topics": [{"id": "модуль 1", "title": "x"}]}, "unknown_target", "не тема"),
        ({"topics": [{"id": "T1.1", "hours": {"pr": 1.3}}]}, "invalid_request", "шагом 0,5"),
        ({"colour": "red"}, "invalid_request", "неизвестное поле"),
    ],
)
def test_approve_bad_edits(api: Api, edits: dict[str, Any], code: str, needle: str) -> None:
    client = api.client()
    sid = skeleton_ready(client)["session_id"]
    err = assert_error(client.post(f"/api/v1/sessions/{sid}/approve", json={"skeleton": edits}), 422, code)
    assert needle in err["error"]["message"]


def test_actions_in_wrong_state_are_409(api: Api) -> None:
    client = api.client()
    sid = client.post("/api/v1/sessions", json={"request": TEXT}).json()["session_id"]
    err = assert_error(client.post(f"/api/v1/sessions/{sid}/approve"), 409, "invalid_state")
    assert "уточняющие вопросы" in err["error"]["message"]
    assert_error(client.post(f"/api/v1/sessions/{sid}/patch", json={"target": "T1.1"}), 409, "invalid_state")

    client.post(f"/api/v1/sessions/{sid}/answers", json={"answers": ANSWERS})
    assert_error(
        client.post(f"/api/v1/sessions/{sid}/answers", json={"answers": ANSWERS}), 409, "invalid_state"
    )
    client.post(f"/api/v1/sessions/{sid}/approve?wait=true")
    err = assert_error(client.post(f"/api/v1/sessions/{sid}/approve"), 409, "invalid_state")
    assert "уже утверждён" in err["error"]["message"]


def test_generation_queue_allows_two_sessions(api: Api) -> None:
    pipeline = FakePipeline(hold={"tests"})
    client = api.client(pipeline, max_generating=2)
    sids = [skeleton_ready(client)["session_id"] for _ in range(3)]
    bodies = [client.post(f"/api/v1/sessions/{sid}/approve").json() for sid in sids]
    assert [b["status"] for b in bodies] == ["generating", "generating", "queued"]
    assert bodies[2]["queue_position"] == 1 and "очеред" in bodies[2]["message"]
    assert client.get(f"/api/v1/sessions/{sids[2]}").json()["status"] == "queued"

    pipeline.release.set()
    for sid in sids:
        assert wait_for(client, sid, {"done", "failed"})["status"] == "done"


def test_busy_session_rejects_new_actions(api: Api) -> None:
    pipeline = FakePipeline(hold={"tests"})
    client = api.client(pipeline)
    sid = skeleton_ready(client)["session_id"]
    client.post(f"/api/v1/sessions/{sid}/approve")
    assert_error(client.post(f"/api/v1/sessions/{sid}/approve"), 409, "session_busy")
    assert client.get(f"/api/v1/sessions/{sid}").status_code == 200
    pipeline.release.set()
    wait_for(client, sid, {"done"})


def test_critical_step_failure_then_retry_skips_finished_steps(api: Api) -> None:
    pipeline = FakePipeline(fail_once={"content:M2": RuntimeError("сбой")})
    client = api.client(pipeline)
    body = client.post("/api/v1/run", json={"request": TEXT, "answers": ANSWERS}).json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "internal_error" and "журнале" in body["error"]["message"]
    assert step_status(body, "content:M2") == "failed" and body["kit"]

    body = client.post(f"/api/v1/sessions/{body['session_id']}/approve?wait=true").json()
    assert body["status"] == "done", body
    assert pipeline.calls["content:M1"] == 1 and pipeline.calls["content:M2"] == 2


def test_unavailable_noncritical_step_is_skipped_with_note(api: Api) -> None:
    pipeline = FakePipeline(
        fail_once={"tests": StepUnavailable("генерация тестов ещё не подключена.")}, noncritical={"tests"}
    )
    client = api.client(pipeline)
    body = client.post("/api/v1/run", json={"request": TEXT, "answers": ANSWERS}).json()
    assert body["status"] == "done"
    assert step_status(body, "tests") == "skipped"
    assert any("пропущен" in note and "тестов" in note for note in body["notes"])
    assert "notes" in body["message"]


def test_failure_before_skeleton_is_reported(api: Api) -> None:
    client = api.client(FakePipeline(fail_once={"skeleton": StepUnavailable("Каркас пока не строится.")}))
    body = client.post("/api/v1/sessions", json={"request": TEXT, "answers": ANSWERS}).json()
    assert body["status"] == "failed" and body["error"]["code"] == "step_unavailable"
    assert "kit" not in body
    assert_error(client.post(f"/api/v1/sessions/{body['session_id']}/approve"), 409, "invalid_state")


def test_default_pipeline_open_query_4_end_to_end(tmp_path: Path) -> None:
    """Боевой пайплайн (LLM в mock, реестр из data/registry): запрос 4 → 5 вопросов → ответы методиста →
    каркас ПК на 72 ч → (auto_approve) генерация содержания → самопроверка со стоимостью и временем по шагам."""
    from tests.intake_queries import OPEN

    query = OPEN[4]
    settings = Settings(sessions_dir=tmp_path / "sessions", llm_mode="mock")
    with TestClient(create_app(settings=settings)) as client:
        body = client.post(
            "/api/v1/sessions", json={"request": query["request"], "auto_approve": True}
        ).json()
        assert body["status"] == "need_clarification"
        ids = [q["id"] for q in body["questions"]]
        assert ids == query["expected"]["must_ask_about"]
        answers = {i: query["methodist_answers"].get(i, query["methodist_answers"]["_default"]) for i in ids}
        body = client.post(f"/api/v1/sessions/{body['session_id']}/answers", json={"answers": answers}).json()
        assert body["status"] == "done", body
        kit = Kit.model_validate(body["kit"])
        assert kit.program.type.value == "pk" and kit.total_hours() == 72
        assert kit.lectures and kit.seminars and kit.questions and kit.final_attestation.criteria
        assert kit.texts.annotation and any(s.section.value == "main" for s in kit.sources)
        assert all(t.content for t in kit.topics)
        report = body["report"]
        assert {"module_content", "lectures", "seminars", "tests", "judge"} <= {
            c["step"] for c in report["cost"]
        }
        assert "plan" in report["timing"] and "module_content:M1" in report["timing"]
        assert not body.get("notes")  # ни один шаг не пропущен
        assert client.get(body["files"]["kit_json"]).status_code == 200


def test_default_pipeline_complete_request_stops_at_skeleton(tmp_path: Path) -> None:
    from tests.intake_queries import OPEN

    settings = Settings(sessions_dir=tmp_path / "sessions", llm_mode="mock")
    with TestClient(create_app(settings=settings)) as client:
        body = client.post("/api/v1/sessions", json={"request": OPEN[1]["request"]}).json()
    assert [s["name"] for s in body["progress"]["steps"]] == ["intake", "clarify", "skeleton"]
    assert body["status"] == "skeleton_ready", body
    skeleton = body["skeleton"]
    assert skeleton["program"]["type"] == "dop" and skeleton["total_hours"] == 16
    assert skeleton["calendar"]["days"] == 8 and skeleton["calendar"]["weeks"] <= 4
    assert body["validation"]["status"] != "errors"


# ---- перезапуск сервера ----------------------------------------------------------------------------


def test_restart_resumes_from_last_finished_step(api: Api) -> None:
    first = FakePipeline(hold={"tests"})
    client = api.client(first)
    sid = skeleton_ready(client)["session_id"]
    body = client.post(f"/api/v1/sessions/{sid}/approve").json()
    deadline = time.monotonic() + 5
    while step_status(body, "content:M2") != "done" and time.monotonic() < deadline:
        body = client.get(f"/api/v1/sessions/{sid}").json()
    api.close(client)  # остановка сервера посреди генерации

    second = FakePipeline()
    client = api.client(second)
    body = wait_for(client, sid, {"done", "failed"})
    assert body["status"] == "done", body
    assert second.calls["content:M1"] == 0 and second.calls["content:M2"] == 0
    assert second.calls["tests"] == 1 and second.calls["validate"] == 1
    assert body["kit"]["texts"]["org_process"].count("[content:M1]") == 1


# ---- правка ----------------------------------------------------------------------------------------


def test_patch_changes_only_target(api: Api) -> None:
    client = api.client()
    done = client.post("/api/v1/run", json={"request": TEXT, "answers": ANSWERS}).json()
    sid = done["session_id"]
    before = Kit.model_validate(done["kit"])
    body = client.post(
        f"/api/v1/sessions/{sid}/patch", json={"target": "тема 1.1", "instruction": "Источники данных банка"}
    ).json()
    assert body["status"] == "done" and body["patch"]["ok"]
    after = Kit.model_validate(body["kit"])
    [changed] = body["changed_ids"]
    assert after.revision == before.revision + 1 == body["revision"]
    assert next(t for t in after.topics if t.id == changed).title == "Источники данных банка"
    untouched = [t for t in before.topics if t.id != changed]
    assert all(
        t.model_dump() == next(a for a in after.topics if a.id == t.id).model_dump() for t in untouched
    )


def test_patch_failure_keeps_kit(api: Api) -> None:
    client = api.client()
    done = client.post("/api/v1/run", json={"request": TEXT, "answers": ANSWERS}).json()
    body = client.post(
        f"/api/v1/sessions/{done['session_id']}/patch", json={"target": "T1.1", "instruction": "сломай"}
    ).json()
    assert body["status"] == "done"
    assert body["patch"]["ok"] is False and "не смогла" in body["patch"]["error"]
    assert body["kit"] == done["kit"] and "правка не выполнена" in body["message"].lower()


def test_patch_unknown_target(api: Api) -> None:
    client = api.client()
    sid = client.post("/api/v1/run", json={"request": TEXT, "answers": ANSWERS}).json()["session_id"]
    err = assert_error(client.post(f"/api/v1/sessions/{sid}/patch", json={"target": "тема 9.9"}), 422)
    assert err["error"]["code"] == "unknown_target"


# ---- предпросмотр часов ----------------------------------------------------------------------------


def test_rebalance_preview_does_not_change_session(api: Api) -> None:
    client = api.client()
    body = skeleton_ready(client)
    sid = body["session_id"]
    topic = body["skeleton"]["modules"][0]["topics"][0]
    hours = {k: topic["hours"][k] for k in ("lk", "pr", "lb", "srs", "distance")}
    hours["pr"] += 1
    resp = client.post(
        "/api/v1/hours/rebalance", json={"session_id": sid, "locked": {f"тема {topic['number']}": hours}}
    )
    preview = resp.json()
    assert resp.status_code == 200 and preview["ok"], preview
    assert preview["total_hours"] == preview["declared_total_hours"]
    row = next(t for t in preview["topics"] if t["id"] == topic["id"])
    assert row["locked"] and row["hours"]["pr"] == hours["pr"]
    assert client.get(f"/api/v1/sessions/{sid}").json()["skeleton"] == body["skeleton"]


def test_rebalance_conflict_and_kit_body(api: Api) -> None:
    client = api.client()
    body = skeleton_ready(client)
    sid = body["session_id"]
    topic_id = body["skeleton"]["modules"][0]["topics"][0]["id"]
    preview = client.post(
        "/api/v1/hours/rebalance", json={"session_id": sid, "locked": {topic_id: {"srs": 1000}}}
    ).json()
    assert preview["ok"] is False and preview["conflicts"][0]["message"]

    kit = client.get(f"/api/v1/sessions/{sid}/files/kit.json")
    assert kit.status_code == 404  # до утверждения выгрузок нет
    done = client.post(f"/api/v1/sessions/{sid}/approve?wait=true").json()
    preview = client.post("/api/v1/hours/rebalance", json={"kit": done["kit"]}).json()
    assert preview["ok"] and preview["total_hours"] == preview["declared_total_hours"]


def test_rebalance_bad_requests(api: Api) -> None:
    client = api.client()
    assert_error(client.post("/api/v1/hours/rebalance", json={}), 422, "invalid_request")
    sid = client.post("/api/v1/sessions", json={"request": TEXT}).json()["session_id"]
    assert_error(client.post("/api/v1/hours/rebalance", json={"session_id": sid}), 409, "invalid_state")
    assert_error(
        client.post("/api/v1/hours/rebalance", json={"session_id": "s_000000000000"}),
        404,
        "session_not_found",
    )


# ---- плохой ввод -----------------------------------------------------------------------------------


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@pytest.mark.parametrize(
    ("payload", "status", "code", "needle"),
    [
        ({}, 422, "invalid_request", "пустой запрос"),
        ({"request": "   "}, 422, "invalid_request", "пустой запрос"),
        ({"request": 123}, 422, "invalid_request", "ожидается строка"),
        ({"request": "x" * 20_001}, 422, "invalid_request", "слишком длинный"),
        ({"request": TEXT, "answers": {"hours": {"a": 1}}}, 422, "invalid_request", "answers"),
        ({"request": TEXT, "auto_approve": "может быть"}, 422, "invalid_request", "true или false"),
        (
            {"attachments": [{"filename": f"{i}.txt", "content_base64": b64(b"x")} for i in range(6)]},
            422,
            "invalid_request",
            "слишком много",
        ),
    ],
)
def test_bad_input_gives_clear_json(
    api: Api, payload: dict[str, Any], status: int, code: str, needle: str
) -> None:
    client = api.client()
    err = assert_error(client.post("/api/v1/sessions", json=payload), status, code)
    assert needle in err["error"]["message"]


def test_malformed_json_and_unknown_addresses(api: Api) -> None:
    client = api.client()
    resp = client.post("/api/v1/sessions", content=b"{", headers={"content-type": "application/json"})
    assert "некорректный JSON" in assert_error(resp, 422, "invalid_request")["error"]["message"]
    assert_error(client.get("/api/v1/sessions/s_000000000000"), 404, "session_not_found")
    assert_error(client.get("/api/v1/sessions/not-a-session"), 404, "session_not_found")
    assert_error(client.get("/nope"), 404, "http_404")
    assert_error(client.post("/health"), 405, "http_405")
    assert_error(client.post("/api/v1/sessions/s_000000000000/answers", json={"answers": {}}), 422)


def test_files_are_served_only_from_list(api: Api) -> None:
    client = api.client()
    sid = client.post("/api/v1/run", json={"request": TEXT, "answers": ANSWERS}).json()["session_id"]
    assert_error(client.get(f"/api/v1/sessions/{sid}/files/state.json"), 404, "file_not_found")
    assert client.get(f"/api/v1/sessions/{sid}/files/..%2Fstate.json").status_code == 404


def test_txt_attachment_is_stored(api: Api, tmp_path: Path) -> None:
    client = api.client()
    payload = {
        "request": TEXT,
        "answers": ANSWERS,
        "attachments": [
            {"filename": "пожелания.txt", "content_base64": "data:text/plain;base64," + b64("тема".encode())}
        ],
    }
    sid = client.post("/api/v1/sessions", json=payload).json()["session_id"]
    stored = tmp_path / "sessions" / sid / "inputs" / "01_пожелания.txt"
    assert stored.read_text(encoding="utf-8") == "тема"


def test_oversized_body_is_413(api: Api) -> None:
    client = api.client()
    resp = client.post(
        "/api/v1/sessions",
        content=b"{}",
        headers={"content-type": "application/json", "content-length": str(10**9)},
    )
    assert_error(resp, 413, "payload_too_large")


def test_sample_requisites_and_text_do_not_leak_into_exports(tmp_path: Path) -> None:
    """Образец с заполненным приказом: ни ответ API, ни kit.json, ни report.json не содержат его реквизитов и текста."""
    from tests.intake_queries import OPEN

    sample = ROOT_DIR / "data" / "templates" / "pk_stazhirovka_bsa_248h.docx"
    attachment = {"filename": sample.name, "content_base64": b64(sample.read_bytes())}
    settings = Settings(sessions_dir=tmp_path / "sessions", llm_mode="mock")
    with TestClient(create_app(settings=settings)) as client:
        body = client.post(
            "/api/v1/run", json={"request": OPEN[5]["request"], "attachments": [attachment]}
        ).json()
        assert body["status"] == "done", body
        texts = [json.dumps(body, ensure_ascii=False)]
        texts += [client.get(url).text for url in body["files"].values()]
    for text in texts:
        assert "17-ОБР" not in text and "Приказом №" not in text
    # разобранный образец (оглавление, таблицы часов, рабочая программа) в выгрузку не идёт; отчёт лишь называет
    # строку образца, где ошибка, — это диагностика для методиста, а не текст программы
    assert "reference" not in body["kit"]["request"]
    assert "Анализ и формализация" not in json.dumps(body["kit"]["request"], ensure_ascii=False)
    assert any(w["type"] == "reference_errors" for w in body["report"]["warnings_for_methodist"])


@pytest.mark.parametrize(
    ("attachment", "note"),
    [
        ({"filename": "ref.docx", "content_base64": "не base64!"}, "base64"),
        ({"filename": "ref.doc", "content_base64": b64(b"\xd0\xcf\x11\xe0 old word")}, ".docx"),
        ({"filename": "таблица.xls", "content_base64": b64(b"\xd0\xcf\x11\xe0")}, "поддерживаются"),
    ],
)
def test_unreadable_attachment_is_skipped_not_rejected(attachment: dict[str, str], note: str) -> None:
    from app.api.schemas import AttachmentIn, decode_attachments

    files, notes = decode_attachments([AttachmentIn(**attachment)])
    assert files == [] and note in notes[0]


def test_bad_attachment_does_not_reject_request(api: Api) -> None:
    client = api.client()
    payload = {
        "request": TEXT,
        "answers": ANSWERS,
        "attachments": [{"filename": "ref.doc", "content_base64": b64(b"old")}],
    }
    assert client.post("/api/v1/sessions", json=payload).json()["status"] == "skeleton_ready"


def test_only_unreadable_attachments_is_bad_input(api: Api) -> None:
    client = api.client()
    payload = {"attachments": [{"filename": "ref.doc", "content_base64": b64(b"old")}]}
    err = assert_error(client.post("/api/v1/sessions", json=payload), 400, "invalid_input")
    assert "Пустой запрос" in err["error"]["message"]


def test_format_is_detected_by_content(tmp_path: Path) -> None:
    from app.api.schemas import AttachmentIn, decode_attachments

    pdf_named_docx = AttachmentIn(filename="программа.docx", content_base64=b64(b"%PDF-1.4 ..."))
    files, notes = decode_attachments([pdf_named_docx])
    assert [name for name, _ in files] == ["программа.pdf"] and notes == []


def test_skipped_attachment_is_explained_to_methodist(tmp_path: Path) -> None:
    from tests.intake_queries import OPEN

    settings = Settings(sessions_dir=tmp_path / "sessions", llm_mode="mock")
    payload = {
        "request": OPEN[1]["request"],
        "attachments": [{"filename": "старый.doc", "content_base64": b64(b"x")}],
    }
    with TestClient(create_app(settings=settings)) as client:
        body = client.post("/api/v1/sessions", json=payload).json()
    assert body["status"] == "skeleton_ready"
    assert any("старый.doc" in a and "пропущен" in a for a in body["skeleton"]["assumptions"])


def test_index_shows_llm_mode(api: Api) -> None:
    index = api.client().get("/").json()
    assert index["llm_mode"] == "mock" and "не настоящие" in index["warning"]


def test_stale_session_is_not_resumed_after_restart(api: Api) -> None:
    from datetime import timedelta

    from app.config import now_msk
    from app.storage.sessions import SessionStore

    pipeline = FakePipeline(hold={"tests"})
    client = api.client(pipeline)
    sid = skeleton_ready(client)["session_id"]
    client.post(f"/api/v1/sessions/{sid}/approve")
    api.close(client)
    store = SessionStore(api.root / "sessions")
    state = store.load(sid)
    state.updated_at = now_msk() - timedelta(hours=5)
    store.save(state, touch=False)

    fresh = FakePipeline()
    body = api.client(fresh).get(f"/api/v1/sessions/{sid}").json()
    assert body["status"] == "failed" and body["error"]["code"] == "interrupted"
    assert fresh.calls["tests"] == 0  # живых вызовов по старой сессии нет
