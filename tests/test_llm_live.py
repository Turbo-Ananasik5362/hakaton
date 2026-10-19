"""Живой смоук шлюза. Тратит бюджет (≈0,1 ₽) — запускать только явно: pytest -m llm tests/test_llm_live.py."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.config import Settings
from app.llm import LLMClient


class Ping(BaseModel):
    ok: bool
    word: str


@pytest.mark.llm
def test_live_structured_smoke() -> None:
    settings = Settings.from_env().model_copy(update={"llm_mode": "live"})
    if not settings.api_key.get_secret_value() or not settings.base_url:
        pytest.skip("нет OPENAI_BASE_URL / OPENAI_API_KEY")
    client = LLMClient(settings)
    result = client.complete_json_sync(
        "live_smoke",
        None,
        [{"role": "user", "content": "Верни объект: ok = true, word = слово «привет»."}],
        Ping,
        max_tokens=60,
    )
    assert isinstance(result, Ping) and result.ok is True
    (cost,) = client.cost_by_step()
    assert cost.calls == 1
