"""Надёжность: таксономия ошибок шага, дедлайн генерации, ограничение частоты и числа сессий в работе."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.limits import SessionRateLimiter
from app.config import Settings
from app.llm import (
    BudgetExceeded,
    GatewayError,
    LLMNotConfigured,
    LLMResponseInvalid,
    ReplayMiss,
    TruncatedResponse,
)
from app.main import create_app
from app.models import Kit
from app.pipeline.base import InputError, RateLimited, Step, StepContext
from app.pipeline.errors import DeadlineExceeded, ErrorKind, classify
from tests.fake_pipeline import FakePipeline

ANSWERS = {"hours": "36"}


# --------------------------------------------------------------------------------------------------
# Таксономия
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "kind", "code"),
    [
        (GatewayError("busy", status=429), ErrorKind.TRANSIENT, "llm_rate_limited"),
        (GatewayError("down", status=503), ErrorKind.TRANSIENT, "llm_unavailable"),
        (GatewayError("Шлюз недоступен: ReadTimeout"), ErrorKind.TRANSIENT, "llm_unavailable"),
        (GatewayError("bad key", status=401), ErrorKind.CONFIG, "llm_rejected"),
        (GatewayError("too long", status=400), ErrorKind.MODEL, "llm_bad_request"),
        (BudgetExceeded("лимит"), ErrorKind.BUDGET, "budget_exceeded"),
        (TruncatedResponse("lectures", {"completion_tokens": 3000}), ErrorKind.MODEL, "llm_truncated"),
        (LLMResponseInvalid("схема"), ErrorKind.MODEL, "llm_bad_answer"),
        (LLMNotConfigured("нет ключа"), ErrorKind.CONFIG, "llm_not_configured"),
        (ReplayMiss("нет в кеше"), ErrorKind.CONFIG, "llm_not_configured"),
        (DeadlineExceeded("истекло время генерации (25 мин)"), ErrorKind.DEADLINE, "deadline_exceeded"),
        (InputError("Пустой запрос."), ErrorKind.INPUT, "invalid_input"),
        (KeyError("M9"), ErrorKind.CODE, "internal_error"),
    ],
)
def test_every_error_has_kind_code_and_explanation(exc: BaseException, kind: ErrorKind, code: str) -> None:
    info = classify(exc)
    assert (info.kind, info.code) == (kind, code)
    assert info.message and "Traceback" not in info.full
    if kind not in (ErrorKind.INPUT,):
        assert info.hint  # что делать методисту


# --------------------------------------------------------------------------------------------------
# Оркестратор: разбор по типу, журнал, дедлайн
# --------------------------------------------------------------------------------------------------


def client(tmp_path: Path, pipeline: FakePipeline, **settings: Any) -> TestClient:
    config = Settings(sessions_dir=tmp_path / "sessions", **settings)
    return TestClient(create_app(settings=config, pipeline=pipeline), raise_server_exceptions=False)


def run(c: TestClient) -> dict[str, Any]:
    body = c.post(
        "/api/v1/sessions",
        json={"request": "Программа на 36 часов", "answers": ANSWERS, "auto_approve": True},
    )
    assert body.status_code == 200, body.text
    return body.json()


def journal(tmp_path: Path, session_id: str) -> list[dict[str, Any]]:
    path = tmp_path / "sessions" / session_id / "log.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_code_error_in_optional_step_keeps_kit_and_logs_trace(tmp_path: Path) -> None:
    pipeline = FakePipeline(fail_once={"tests": KeyError("M9")}, noncritical={"tests"})
    with client(tmp_path, pipeline) as c:
        body = run(c)
    assert body["status"] == "done" and body["kit"]
    assert any("Внутренняя ошибка агента (KeyError)" in n for n in body["notes"])
    events = [e for e in journal(tmp_path, body["session_id"]) if e["event"] == "step_failed"]
    assert events[0]["code"] == "internal_error" and events[0]["kind"] == "code"
    assert "KeyError" in events[0]["trace"] and "Traceback" in events[0]["trace"]


@pytest.mark.parametrize(
    ("exc", "code", "hint"),
    [
        (GatewayError("busy", status=429), "llm_rate_limited", "через несколько минут"),
        (BudgetExceeded("лимит"), "budget_exceeded", "[заполнить методисту]"),
        (LLMResponseInvalid("схема"), "llm_bad_answer", "Повторите генерацию"),
    ],
)
def test_critical_step_failure_explains_what_to_do(
    tmp_path: Path, exc: Exception, code: str, hint: str
) -> None:
    with client(tmp_path, FakePipeline(fail_once={"content:M1": exc})) as c:
        body = run(c)
    assert body["status"] == "failed"
    assert body["error"]["code"] == code and hint in body["error"]["message"]
    failed = [e for e in journal(tmp_path, body["session_id"]) if e["event"] == "failed"]
    assert failed[-1]["code"] == code and "trace" not in failed[-1]  # трассировка — только у ошибок программы


class SlowPipeline(FakePipeline):
    """Шаг с «моделью», который не укладывается в дедлайн, и шаги-код (сборка, проверка)."""

    def __init__(self, delay: float = 5.0) -> None:
        super().__init__()
        self.delay = delay

    def generation_steps(self, kit: Kit, finished: set[str]) -> list[Step]:
        for name, title, fn, uses_llm in (
            ("lectures:M1", "Лекции модуля 1", self._slow, True),
            ("finalize", "Проверка полноты", self._assemble, False),
            ("validate", "Самопроверка", self._validate_step, False),
        ):
            if name not in finished:
                return [Step(name, title, fn, critical=name == "validate", uses_llm=uses_llm)]
        return []

    async def _slow(self, kit: Kit, ctx: StepContext) -> None:
        self.calls["slow"] += 1
        await asyncio.sleep(self.delay)
        kit.texts.org_process += "[slow]"

    async def _assemble(self, kit: Kit, ctx: StepContext) -> None:
        self.calls["assemble"] += 1
        kit.texts.org_process += "[assemble]"


def test_deadline_skips_model_steps_and_assembles_the_rest(tmp_path: Path) -> None:
    pipeline = SlowPipeline(delay=5.0)
    started = time.monotonic()
    with client(tmp_path, pipeline, generation_deadline_s=0.3) as c:
        body = run(c)
    assert time.monotonic() - started < 4  # шаг прерван, а не дождались
    assert body["status"] == "done"
    steps = {s["name"]: s["status"] for s in body["progress"]["steps"]}
    assert steps["lectures:M1"] == "skipped" and steps["finalize"] == "done" and steps["validate"] == "done"
    assert any("Лекции модуля 1" in n and "истекло время генерации" in n for n in body["notes"])
    assert (
        "[slow]" not in body["kit"]["texts"]["org_process"]
        and "[assemble]" in body["kit"]["texts"]["org_process"]
    )


def test_real_pipeline_after_deadline_builds_kit_from_what_is_ready(tmp_path: Path) -> None:
    """Дедлайн истёк сразу: вызовы модели пропущены, сборка, выгрузки и самопроверка выполнены, всё объяснено."""
    from app.pipeline.default import DefaultPipeline
    from tests.intake_queries import OPEN

    settings = Settings(sessions_dir=tmp_path / "sessions", llm_mode="mock", generation_deadline_s=1e-6)
    with TestClient(create_app(settings=settings, pipeline=DefaultPipeline(settings))) as c:
        body = c.post("/api/v1/sessions", json={"request": OPEN[3]["request"], "auto_approve": True}).json()
    assert body["status"] == "done" and "program_docx" in body["files"] and "kit_json" in body["files"]
    steps = {s["name"]: s["status"] for s in body["progress"]["steps"]}
    assert steps["plan"] == "done" and steps["finalize"] == "done" and steps["validate"] == "done"
    assert all(
        status == "skipped" for name, status in steps.items() if name.startswith(("lectures:", "tests:"))
    )
    assert any("истекло время генерации" in n for n in body["notes"])
    kit = Kit.model_validate(body["kit"])
    assert kit.total_hours() == kit.program.total_hours  # каркас и часы на месте
    assert "[заполнить методисту]" in json.dumps(body["kit"], ensure_ascii=False)


# --------------------------------------------------------------------------------------------------
# Ограничение частоты и числа сессий
# --------------------------------------------------------------------------------------------------


def test_too_many_new_sessions_from_one_address_get_429(tmp_path: Path) -> None:
    with client(tmp_path, FakePipeline(), rate_limit_per_minute=2) as c:
        for _ in range(2):
            assert c.post("/api/v1/sessions", json={"request": "Программа на 36 часов"}).status_code == 200
        resp = c.post("/api/v1/run", json={"request": "Программа на 36 часов"})
    assert resp.status_code == 429
    retry = int(resp.headers["Retry-After"])
    assert 1 <= retry <= 60
    error = resp.json()["error"]
    assert error["code"] == "rate_limited" and "не больше 2 в минуту" in error["message"]


def test_too_many_active_sessions_get_429_and_running_ones_continue(tmp_path: Path) -> None:
    pipeline = FakePipeline(hold={"content:M1"})
    with client(tmp_path, pipeline, max_active_sessions=1) as c:
        first = c.post(
            "/api/v1/sessions?wait=false",
            json={"request": "Программа на 36 часов", "answers": ANSWERS, "auto_approve": True},
        ).json()
        busy = c.post("/api/v1/sessions", json={"request": "Ещё одна программа"})
        assert busy.status_code == 429 and busy.json()["error"]["code"] == "rate_limited"
        assert busy.headers["Retry-After"] == "60" and busy.json()["error"]["details"]["limit"] == 1
        pipeline.release.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = c.get(f"/api/v1/sessions/{first['session_id']}").json()
            if state["status"] == "done":
                break
            time.sleep(0.02)
        assert state["status"] == "done"
        assert c.post("/api/v1/sessions", json={"request": "Теперь можно"}).status_code == 200


def test_rate_limiter_window_slides() -> None:
    now = [0.0]
    limiter = SessionRateLimiter(per_minute=2, clock=lambda: now[0])
    limiter.check("10.0.0.1")
    limiter.check("10.0.0.1")
    limiter.check("10.0.0.2")  # другой адрес — свой счётчик
    with pytest.raises(RateLimited) as info:
        limiter.check("10.0.0.1")
    assert info.value.retry_after == 60
    now[0] = 30.0
    with pytest.raises(RateLimited) as info:
        limiter.check("10.0.0.1")
    assert info.value.retry_after == 30
    now[0] = 60.5
    limiter.check("10.0.0.1")  # окно сдвинулось
