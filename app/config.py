"""Настройки приложения. Переменные берутся из окружения; на ноутбуке — ещё из .env (python-dotenv).

Ключ шлюза хранится как SecretStr и не попадает в repr, логи и сообщения об ошибках.
"""

from __future__ import annotations

import datetime as dt
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr

ROOT = Path(__file__).resolve().parent.parent
MSK = dt.timezone(dt.timedelta(hours=3))

DEFAULT_MODEL = "GigaChat/GigaChat-3-Pro"
REASONING_MODEL = "ai-sage/GigaChat3.5-432B-A28B-Reasoning"
EMBEDDINGS_MODEL = "ai-sage/Giga-Embeddings-instruct-3B"

# ₽ за 1M токенов (вход, выход) — из навыка gigachat-gateway
PRICES_RUB_PER_M: dict[str, tuple[float, float]] = {
    DEFAULT_MODEL: (73.03, 176.39),
    REASONING_MODEL: (96.22, 288.60),
    EMBEDDINGS_MODEL: (32.22, 0.0),
}

GATEWAY_OVERHEAD_TOKENS = 1200
"""Скрытый системный промпт шлюза: ≈1200 входных токенов на каждый вызов (замер 18.10.2026)."""

LLMMode = Literal["mock", "live", "replay"]


class Settings(BaseModel):
    llm_mode: LLMMode = "mock"
    base_url: str = ""
    api_key: SecretStr = SecretStr("")
    default_model: str = DEFAULT_MODEL
    reasoning_model: str = REASONING_MODEL
    daily_budget_rub: float = Field(default=90.0, ge=0)
    concurrency: int = Field(default=2, ge=1)  # шлюз ограничивает одновременные запросы всей команды (429)
    timeout_s: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=4, ge=0)
    max_repairs: int = Field(default=2, ge=0)
    schema_strict: bool = True
    cache_dir: Path = ROOT / "cache" / "llm"
    cost_log: Path = ROOT / "logs" / "llm_cost.jsonl"
    fixtures_dir: Path = ROOT / "tests" / "fixtures" / "llm"
    sessions_dir: Path = ROOT / "data" / "sessions"
    registry_dir: Path = ROOT / "data" / "registry"

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(ROOT / ".env", override=False)
        env = os.environ
        mode = env.get("LLM_MODE", "mock").strip().lower()
        if mode not in ("mock", "live", "replay"):
            raise ValueError(f"LLM_MODE должен быть mock, live или replay, а не {mode!r}")
        return cls(
            llm_mode=mode,  # type: ignore[arg-type]
            base_url=env.get("OPENAI_BASE_URL", "").rstrip("/"),
            api_key=SecretStr(env.get("OPENAI_API_KEY", "")),
            default_model=env.get("LLM_DEFAULT_MODEL", DEFAULT_MODEL),
            daily_budget_rub=float(env.get("LLM_DAILY_BUDGET_RUB", "90")),
            concurrency=int(env.get("LLM_CONCURRENCY", "2")),
            timeout_s=float(env.get("LLM_TIMEOUT_S", "120")),
            max_retries=int(env.get("LLM_MAX_RETRIES", "4")),
            schema_strict=env.get("LLM_SCHEMA_STRICT", "1") not in ("0", "false", "False"),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def now_msk() -> dt.datetime:
    return dt.datetime.now(MSK)
