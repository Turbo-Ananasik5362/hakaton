"""Тесты клиента LLM без сети: mock-фикстуры и подменённый HTTP-транспорт (httpx.MockTransport)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, Field, SecretStr

from app.config import GATEWAY_OVERHEAD_TOKENS, Settings, now_msk
from app.llm import (
    BudgetExceeded,
    GatewayError,
    LLMClient,
    LLMResponseInvalid,
    MockFixtureMissing,
    ReplayMiss,
    TruncatedResponse,
    cost_rub,
    estimate_cost,
    extract_first_json,
    spent_today_rub,
    strict_json_schema,
)
from app.models import SkeletonDraft

SECRET = "sk-very-secret-key-123"


class Tiny(BaseModel):
    ok: bool
    word: str


class Nested(BaseModel):
    title: str  # поле с именем title не должно пропасть при чистке схемы
    items: list[Tiny] = Field(default_factory=list)
    note: str | None = None


def make_settings(tmp_path: Path, mode: str = "live", **kwargs: Any) -> Settings:
    return Settings(
        llm_mode=mode,  # type: ignore[arg-type]
        base_url="https://gateway.test/v1",
        api_key=SecretStr(SECRET),
        cache_dir=tmp_path / "cache",
        cost_log=tmp_path / "logs" / "cost.jsonl",
        fixtures_dir=tmp_path / "fixtures",
        **kwargs,
    )


def chat(content: str, finish: str = "stop", prompt: int = 1230, completion: int = 17) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "GigaChat/GigaChat-3-Pro",
            "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "prompt_tokens_details": {"cached_tokens": 1200},
            },
        },
    )


class FakeGateway:
    """Отдаёт заранее заданные ответы по очереди и запоминает тела запросов."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = list(responses)
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        self.headers.append(request.headers)
        if not self.responses:
            raise AssertionError("Лишний запрос к шлюзу")
        return self.responses.pop(0)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


async def no_sleep(_: float) -> None:
    return None


def make_client(tmp_path: Path, gateway: FakeGateway, mode: str = "live", **kwargs: Any) -> LLMClient:
    return LLMClient(make_settings(tmp_path, mode, **kwargs), transport=gateway.transport(), sleep=no_sleep)


MESSAGES = [{"role": "user", "content": "Верни объект: ok = true, word = привет"}]


def call(client: LLMClient, schema: type[BaseModel] = Tiny, step: str = "unit") -> Any:
    return client.complete_json_sync(step, None, MESSAGES, schema)


def log_rows(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "logs" / "cost.jsonl"
    return (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    )


# ---- стоимость -------------------------------------------------------------------------------------


def test_cost_and_estimate_include_gateway_overhead() -> None:
    assert cost_rub("GigaChat/GigaChat-3-Pro", 1_000_000, 0) == pytest.approx(73.03)
    assert cost_rub("GigaChat/GigaChat-3-Pro", 0, 1_000_000) == pytest.approx(176.39)
    one_empty_call = estimate_cost(1, 0, 0, model="GigaChat/GigaChat-3-Pro")
    assert one_empty_call == pytest.approx(GATEWAY_OVERHEAD_TOKENS * 73.03 / 1_000_000)
    # 30 вызовов комплекта 72 ч из docs/ARCHITECTURE.md §7.3 ≈ 12 ₽
    kit = estimate_cost(30, 88_000 - 30 * 1200, 31_500, model="GigaChat/GigaChat-3-Pro")
    assert 11 < kit < 13


# ---- схема и извлечение JSON -----------------------------------------------------------------------


def test_strict_schema_requires_all_and_forbids_extra() -> None:
    schema = strict_json_schema(Nested)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"title", "items", "note"}
    assert "title" in schema["properties"]  # свойство «title» не спутано со служебным ключом
    tiny = schema["$defs"]["Tiny"]
    assert tiny["additionalProperties"] is False and set(tiny["required"]) == {"ok", "word"}
    assert "default" not in json.dumps(schema)


def test_strict_schema_builds_for_skeleton_draft() -> None:
    schema = strict_json_schema(SkeletonDraft)
    assert schema["additionalProperties"] is False
    assert "modules" in schema["required"]


@pytest.mark.parametrize(
    "text",
    [
        '{"ok": true, "word": "привет"}',
        'Вот ответ:\n```json\n{"ok": true, "word": "привет"}\n```',
        'мусор {не json} потом {"ok": true, "word": "привет"} и хвост',
    ],
)
def test_extract_first_json(text: str) -> None:
    assert extract_first_json(text) == {"ok": True, "word": "привет"}


def test_extract_handles_braces_inside_strings() -> None:
    assert extract_first_json('{"word": "a } b { c", "ok": false}') == {"word": "a } b { c", "ok": False}


def test_extract_without_json_raises() -> None:
    with pytest.raises(ValueError):
        extract_first_json("просто текст")


# ---- mock ------------------------------------------------------------------------------------------


def test_mock_uses_default_fixture(tmp_path: Path) -> None:
    folder = tmp_path / "fixtures" / "unit"
    folder.mkdir(parents=True)
    (folder / "default.json").write_text('{"ok": true, "word": "мок"}', encoding="utf-8")
    client = make_client(tmp_path, FakeGateway([]), mode="mock")
    assert call(client) == Tiny(ok=True, word="мок")
    assert log_rows(tmp_path) == []  # mock ничего не тратит и не пишет в лог расхода


def test_mock_missing_fixture_has_clear_message(tmp_path: Path) -> None:
    client = make_client(tmp_path, FakeGateway([]), mode="mock")
    with pytest.raises(MockFixtureMissing, match="default.json"):
        call(client)


# ---- live через подменённый транспорт --------------------------------------------------------------


def test_live_structured_call_logs_and_caches(tmp_path: Path) -> None:
    gateway = FakeGateway([chat('{"ok": true, "word": "привет"}')])
    client = make_client(tmp_path, gateway)
    assert call(client) == Tiny(ok=True, word="привет")

    body = gateway.bodies[0]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["name"] == "Tiny"
    assert gateway.headers[0]["authorization"] == f"Bearer {SECRET}"

    rows = log_rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["step"] == "unit" and row["cached"] is False
    assert row["prompt_tokens"] == 1230 and row["completion_tokens"] == 17
    assert row["cached_prompt_tokens"] == 1200
    assert row["cost_rub"] == pytest.approx(cost_rub("GigaChat/GigaChat-3-Pro", 1230, 17))
    assert row["finish_reason"] == "stop"
    assert row["ts"].startswith(now_msk().date().isoformat())

    # повтор того же запроса — из кеша, без обращения к шлюзу и бесплатно
    assert call(client) == Tiny(ok=True, word="привет")
    rows = log_rows(tmp_path)
    assert len(rows) == 2 and rows[1]["cached"] is True and rows[1]["cost_rub"] == 0
    assert len(gateway.bodies) == 1
    assert len(list((tmp_path / "cache").glob("*.json"))) == 1


def test_secret_never_written(tmp_path: Path) -> None:
    gateway = FakeGateway([chat('{"ok": true, "word": "x"}')])
    call(make_client(tmp_path, gateway))
    for path in tmp_path.rglob("*.json*"):
        assert SECRET not in path.read_text(encoding="utf-8")


def test_replay_hit_and_miss(tmp_path: Path) -> None:
    call(make_client(tmp_path, FakeGateway([chat('{"ok": true, "word": "из кеша"}')])))
    replay = make_client(tmp_path, FakeGateway([]), mode="replay")
    assert call(replay) == Tiny(ok=True, word="из кеша")
    with pytest.raises(ReplayMiss):
        replay.complete_json_sync("unit", None, [{"role": "user", "content": "другой запрос"}], Tiny)


def test_fallback_when_gateway_rejects_schema(tmp_path: Path) -> None:
    gateway = FakeGateway(
        [
            httpx.Response(400, json={"error": "response_format is not supported"}),
            chat('Конечно! Вот JSON: {"ok": false, "word": "запасной"} — готово.'),
        ]
    )
    client = make_client(tmp_path, gateway)
    assert call(client) == Tiny(ok=False, word="запасной")
    assert "response_format" not in gateway.bodies[1]
    assert "JSON-схеме" in gateway.bodies[1]["messages"][-1]["content"]
    # для этой схемы клиент больше не пробует response_format
    gateway.responses.append(chat('{"ok": true, "word": "снова"}'))
    client.complete_json_sync("unit", None, [{"role": "user", "content": "ещё"}], Tiny)
    assert "response_format" not in gateway.bodies[2]


def test_repair_after_invalid_answer(tmp_path: Path) -> None:
    gateway = FakeGateway([chat('{"ok": true}'), chat('{"ok": true, "word": "исправлено"}')])
    client = make_client(tmp_path, gateway)
    assert call(client) == Tiny(ok=True, word="исправлено")
    repair_message = gateway.bodies[1]["messages"][-1]["content"]
    assert "не прошёл проверку схемы" in repair_message and "word" in repair_message
    assert gateway.bodies[1]["messages"][-2] == {"role": "assistant", "content": '{"ok": true}'}


def test_repairs_are_limited(tmp_path: Path) -> None:
    gateway = FakeGateway([chat("не json"), chat('{"ok": 1}'), chat('{"word": "x"}')])
    client = make_client(tmp_path, gateway)
    with pytest.raises(LLMResponseInvalid):
        call(client)
    assert len(gateway.bodies) == 3  # 1 вызов + 2 ремонта


def test_truncated_response_raises(tmp_path: Path) -> None:
    gateway = FakeGateway([chat('{"ok": true, "wo', finish="length", completion=2000)])
    with pytest.raises(TruncatedResponse):
        call(make_client(tmp_path, gateway))


def test_retries_on_429_and_5xx(tmp_path: Path) -> None:
    gateway = FakeGateway(
        [
            httpx.Response(429, headers={"retry-after": "1"}),
            httpx.Response(503),
            chat('{"ok": true, "word": "после повторов"}'),
        ]
    )
    assert call(make_client(tmp_path, gateway)) == Tiny(ok=True, word="после повторов")
    assert len(gateway.bodies) == 3


def test_gives_up_after_max_retries(tmp_path: Path) -> None:
    gateway = FakeGateway([httpx.Response(500)] * 3)
    with pytest.raises(GatewayError) as info:
        call(make_client(tmp_path, gateway, max_retries=2))
    assert info.value.status == 500


def test_non_retryable_error_hides_secret(tmp_path: Path) -> None:
    gateway = FakeGateway([httpx.Response(401, text=f"bad key {SECRET}")])
    client = make_client(tmp_path, gateway)
    with pytest.raises(GatewayError) as info:
        client.complete_json_sync("unit", None, MESSAGES, Tiny)
    assert info.value.status == 401
    assert SECRET not in str(info.value)


def test_budget_guard_blocks_live_calls(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "cost.jsonl"
    log.parent.mkdir(parents=True)
    row = {"ts": now_msk().isoformat(), "step": "x", "model": "m", "cost_rub": 95.0, "cached": False}
    log.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert spent_today_rub(log) == 95.0
    gateway = FakeGateway([])
    client = make_client(tmp_path, gateway, daily_budget_rub=90)
    with pytest.raises(BudgetExceeded):
        call(client)
    assert gateway.bodies == []
    assert client.remaining_budget() == 0


def test_budget_ignores_cached_and_other_days(tmp_path: Path) -> None:
    log = tmp_path / "cost.jsonl"
    rows = [
        {"ts": now_msk().isoformat(), "cost_rub": 1.5, "cached": False},
        {"ts": now_msk().isoformat(), "cost_rub": 9.0, "cached": True},
        {"ts": "2020-01-01T10:00:00+03:00", "cost_rub": 50.0, "cached": False},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\nне json\n", encoding="utf-8")
    assert spent_today_rub(log) == 1.5


def test_live_without_credentials_fails_clearly(tmp_path: Path) -> None:
    settings = make_settings(tmp_path).model_copy(update={"api_key": SecretStr("")})
    client = LLMClient(settings, transport=FakeGateway([]).transport(), sleep=no_sleep)
    with pytest.raises(Exception, match="OPENAI_API_KEY"):
        call(client)


def test_reasoning_model_gets_min_tokens(tmp_path: Path) -> None:
    gateway = FakeGateway([chat('{"ok": true, "word": "r"}')])
    client = make_client(tmp_path, gateway)
    client.complete_json_sync(
        "unit", client.settings.reasoning_model, MESSAGES, Tiny, max_tokens=200, temperature=None
    )
    assert gateway.bodies[0]["max_tokens"] >= 1000
    assert "temperature" not in gateway.bodies[0]


def test_cost_by_step_aggregates(tmp_path: Path) -> None:
    gateway = FakeGateway([chat('{"ok": true, "word": "a"}'), chat('{"ok": true, "word": "b"}')])
    client = make_client(tmp_path, gateway)
    client.complete_json_sync("skeleton", None, MESSAGES, Tiny)
    client.complete_json_sync("skeleton", None, [{"role": "user", "content": "2"}], Tiny)
    (item,) = client.cost_by_step()
    assert item.step == "skeleton" and item.calls == 2 and item.prompt_tokens == 2460


def test_semaphore_limits_parallel_calls(tmp_path: Path) -> None:
    state = {"now": 0, "max": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return chat('{"ok": true, "word": "w"}')

    client = LLMClient(make_settings(tmp_path, concurrency=3), transport=httpx.MockTransport(handler))

    async def run_all() -> list[BaseModel]:
        tasks = [
            client.complete_json("par", None, [{"role": "user", "content": f"запрос {i}"}], Tiny)
            for i in range(10)
        ]
        return await asyncio.gather(*tasks)

    results = asyncio.run(run_all())
    assert len(results) == 10
    assert state["max"] <= 3


# ---- в кеш — только полный и валидный ответ --------------------------------------------------------


def test_truncated_response_is_not_cached(tmp_path: Path) -> None:
    gateway = FakeGateway([chat('{"ok": true, "wo', finish="length"), chat('{"ok": true, "word": "да"}')])
    client = make_client(tmp_path, gateway)
    with pytest.raises(TruncatedResponse):
        call(client)
    assert not list((tmp_path / "cache").glob("*.json"))
    assert call(make_client(tmp_path, gateway)).word == "да"  # повтор идёт в шлюз, а не в кеш с обрезком
    assert len(gateway.bodies) == 2
    assert len(log_rows(tmp_path)) == 2  # оплаченный обрезанный ответ всё равно учтён в расходе


def test_invalid_response_is_not_cached(tmp_path: Path) -> None:
    gateway = FakeGateway([chat("не json")] * 3)
    with pytest.raises(LLMResponseInvalid):
        call(make_client(tmp_path, gateway, max_repairs=2))
    assert not list((tmp_path / "cache").glob("*.json"))


def test_repaired_response_is_cached_and_replayed(tmp_path: Path) -> None:
    gateway = FakeGateway([chat("не json"), chat('{"ok": true, "word": "да"}')])
    assert call(make_client(tmp_path, gateway)).word == "да"
    assert len(list((tmp_path / "cache").glob("*.json"))) == 2  # под ключом ремонта и исходного запроса
    replay = LLMClient(make_settings(tmp_path, "replay"))
    assert call(replay).word == "да"  # повтор исходного запроса бесплатен и сразу даёт исправленный ответ


def test_timeout_is_retried_only_once(tmp_path: Path) -> None:
    calls = {"n": 0}

    def slow(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("долго", request=request)

    client = LLMClient(
        make_settings(tmp_path, max_retries=4), transport=httpx.MockTransport(slow), sleep=no_sleep
    )
    with pytest.raises(GatewayError):
        call(client)
    assert calls["n"] == 2


def test_team_rate_limit_waits_longer_than_server_errors(tmp_path: Path) -> None:
    """429 «too many concurrent requests from this team»: место освобождается, когда закончится чужой вызов, —
    клиент ждёт дольше и пробует больше раз, чем при сбое сервера (живой прогон 20.10: 4 коротких повтора не хватило)."""
    busy = httpx.Response(429, json={"error": {"code": "llm_too_many_requests"}})
    gateway = FakeGateway([busy] * 7 + [chat('{"ok": true, "word": "дождались"}')])
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    client = LLMClient(make_settings(tmp_path, max_retries=4), transport=gateway.transport(), sleep=record)
    assert call(client) == Tiny(ok=True, word="дождались")
    assert len(sleeps) == 7 and sum(sleeps) > 60  # не 15 с, как у экспоненты 1–8 с
    gateway = FakeGateway([httpx.Response(429)] * 20)
    with pytest.raises(GatewayError) as info:
        call(
            LLMClient(
                make_settings(tmp_path / "other", max_retries=4),
                transport=gateway.transport(),
                sleep=no_sleep,
            )
        )
    assert info.value.status == 429 and len(gateway.bodies) <= 4 + 1 + 4  # конечное число попыток


def test_rate_limit_total_wait_is_capped(tmp_path: Path) -> None:
    """Retry-After: 60 на каждый ответ — суммарное ожидание одного вызова не больше MAX_RATE_LIMIT_WAIT_S."""
    from app.llm import MAX_RATE_LIMIT_WAIT_S

    gateway = FakeGateway([httpx.Response(429, headers={"retry-after": "60"})] * 20)
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    client = LLMClient(make_settings(tmp_path, max_retries=4), transport=gateway.transport(), sleep=record)
    with pytest.raises(GatewayError) as info:
        call(client)
    assert info.value.status == 429 and sum(sleeps) <= MAX_RATE_LIMIT_WAIT_S and len(sleeps) == 3


def test_gateway_down_gets_no_rate_limit_retries(tmp_path: Path) -> None:
    """Шлюз лежит (ошибка соединения): только max_retries повторов, доп. попытки — лишь для 429."""
    calls = {"n": 0}

    def down(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("нет соединения", request=request)

    client = LLMClient(
        make_settings(tmp_path, max_retries=2), transport=httpx.MockTransport(down), sleep=no_sleep
    )
    with pytest.raises(GatewayError):
        call(client)
    assert calls["n"] == 3


def test_server_errors_get_no_rate_limit_retries(tmp_path: Path) -> None:
    gateway = FakeGateway([httpx.Response(503)] * 20)
    with pytest.raises(GatewayError) as info:
        call(make_client(tmp_path, gateway, max_retries=2))
    assert info.value.status == 503 and len(gateway.bodies) == 3
