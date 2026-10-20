"""Настройки приложения. Переменные берутся из окружения и из .env в корне проекта (python-dotenv, окружение важнее).
Полный список переменных — .env.example.

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
LLM_MODES = ("auto", "mock", "live", "replay")  # auto — live при заданных адресе шлюза и ключе, иначе mock

APP_VERSION = "0.5.0"


class Settings(BaseModel):
    llm_mode: LLMMode = "mock"  # действующий режим
    llm_mode_requested: str = ""  # как задано в LLM_MODE (auto / mock / live / replay); пусто — задан в коде
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
    generation_deadline_s: float = Field(default=1500.0, gt=0)  # 25 мин на генерацию комплекта одной сессии
    rate_limit_per_minute: int = Field(default=30, ge=1)  # новых сессий в минуту с одного адреса
    max_active_sessions: int = Field(default=50, ge=1)  # сессий в работе одновременно (идут и ждут в очереди)
    cache_dir: Path = ROOT / "cache" / "llm"
    cost_log: Path = ROOT / "logs" / "llm_cost.jsonl"
    fixtures_dir: Path = ROOT / "tests" / "fixtures" / "llm"
    sessions_dir: Path = ROOT / "data" / "sessions"
    registry_dir: Path = ROOT / "data" / "registry"
    organization_file: Path = ROOT / "data" / "organization.json"  # шапка организации для .docx

    @property
    def gateway_configured(self) -> bool:
        """Заданы адрес шлюза и ключ (сами значения наружу не отдаются)."""
        return bool(self.base_url and self.api_key.get_secret_value())

    @property
    def llm_ready(self) -> bool:
        """Ответы модели настоящие: режим live и шлюз настроен."""
        return self.llm_mode == "live" and self.gateway_configured

    @property
    def llm_warning(self) -> str | None:
        """Предупреждение для лога и GET /, если ответы модели не настоящие или живой режим не настроен."""
        if self.llm_mode == "live":
            if self.gateway_configured:
                return None
            return (
                "LLM_MODE=live, но OPENAI_BASE_URL или OPENAI_API_KEY не заданы: вызовы модели будут "
                "завершаться ошибкой. Задайте обе переменные (см. .env.example) и перезапустите сервер."
            )
        if self.llm_mode_requested == "auto":
            return (
                "LLM_MODE=auto: OPENAI_BASE_URL и OPENAI_API_KEY не заданы — сервер работает в режиме mock, "
                "ответы модели заготовленные, а не настоящие. Чтобы подключить GigaChat, задайте обе переменные "
                "(см. .env.example) и перезапустите сервер."
            )
        return (
            f"LLM_MODE={self.llm_mode}: ответы модели не настоящие (заготовки или кеш), шлюз не вызывается."
        )

    @classmethod
    def from_env(cls, env_file: Path | None = ROOT / ".env") -> Settings:
        """Настройки из окружения. env_file — .env с локальными значениями (не перекрывает окружение); None — не
        читать (тесты)."""
        if env_file is not None:
            load_dotenv(env_file, override=False)
        env = os.environ
        mode = (env.get("LLM_MODE") or "auto").strip().lower()
        if mode not in LLM_MODES:
            raise ValueError(f"LLM_MODE должен быть auto, mock, live или replay, а не {mode!r}")
        base_url = env.get("OPENAI_BASE_URL", "").strip().rstrip("/")
        api_key = env.get("OPENAI_API_KEY", "").strip()
        effective = mode if mode != "auto" else ("live" if base_url and api_key else "mock")
        return cls(
            llm_mode=effective,  # type: ignore[arg-type]
            llm_mode_requested=mode,
            base_url=base_url,
            api_key=SecretStr(api_key),
            default_model=env.get("LLM_DEFAULT_MODEL", DEFAULT_MODEL),
            daily_budget_rub=float(env.get("LLM_DAILY_BUDGET_RUB", "90")),
            concurrency=int(env.get("LLM_CONCURRENCY", "2")),
            timeout_s=float(env.get("LLM_TIMEOUT_S", "120")),
            max_retries=int(env.get("LLM_MAX_RETRIES", "4")),
            schema_strict=env.get("LLM_SCHEMA_STRICT", "1") not in ("0", "false", "False"),
            generation_deadline_s=float(env.get("GENERATION_DEADLINE_S", "1500")),
            rate_limit_per_minute=int(env.get("RATE_LIMIT_PER_MINUTE", "30")),
            max_active_sessions=int(env.get("MAX_ACTIVE_SESSIONS", "50")),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def git_commit(root: Path = ROOT) -> str:
    """Короткий хеш текущего коммита из .git без вызова git; пусто, если репозитория нет."""
    git = root / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head[:7]
        ref = head[5:].strip()
        if (git / ref).is_file():
            return (git / ref).read_text(encoding="utf-8").strip()[:7]
        packed = (git / "packed-refs").read_text(encoding="utf-8").splitlines()
        return next((line.split()[0][:7] for line in packed if line.endswith(f" {ref}")), "")
    except (OSError, IndexError):
        return ""


def app_version() -> str:
    commit = git_commit()
    return f"{APP_VERSION}+{commit}" if commit else APP_VERSION


def now_msk() -> dt.datetime:
    return dt.datetime.now(MSK)
