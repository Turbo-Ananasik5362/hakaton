"""Клиент шлюза LLM (OpenAI-совместимый API) — навык gigachat-gateway, docs/ARCHITECTURE.md §2.2 и §7.

Режимы (LLM_MODE):
- mock   — ответы из tests/fixtures/llm/<step>/ (<ключ>.json → default.json → первый *.json), сеть не трогаем;
- replay — только дисковый кеш, промах = ReplayMiss;
- live   — кеш, при промахе — запрос к шлюзу.

JSON-ответы: основной путь — response_format json_schema из pydantic-класса (на шлюзе проверено);
если шлюз отверг схему (400/422) — запасной путь: инструкция «верни только JSON» + извлечение первого объекта.
Ответ, не прошедший валидацию, — до max_repairs ремонтных попыток с текстом ошибки.
finish_reason=length → TruncatedResponse: дробит вызывающий, обрезанный JSON не чиним.

Учёт: каждая живая попытка и каждое попадание в кеш — строка в logs/llm_cost.jsonl.
Предохранитель: если расход за сегодня (МСК, с этой машины) ≥ LLM_DAILY_BUDGET_RUB — живые вызовы отклоняются.
Ключ шлюза не попадает ни в логи, ни в кеш, ни в тексты исключений.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import GATEWAY_OVERHEAD_TOKENS, PRICES_RUB_PER_M, Settings, get_settings, now_msk
from app.models import StepCost

T = TypeVar("T", bound=BaseModel)
Message = dict[str, str]

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
RATE_LIMIT_EXTRA_RETRIES = (
    4  # при 429 — до max_retries + 4 попыток с паузами 5–30 с (≈ 2,5 мин в худшем случае)
)
REASONING_MIN_TOKENS = 1000
MAX_RATE_LIMIT_WAIT_S = (
    180.0  # суммарно ждать место у шлюза (429) не дольше: дальше шаг уйдёт в повтор или заглушку
)
MIN_TOKENS_PER_SECOND = 40  # нижняя граница скорости шлюза для таймаута длинных ответов (замер: 69–80 ток/с)


# --------------------------------------------------------------------------------------------------
# Исключения
# --------------------------------------------------------------------------------------------------


class LLMError(Exception):
    """Базовая ошибка клиента LLM."""


class BudgetExceeded(LLMError):
    """Суточный предохранитель: живые вызовы запрещены."""


class TruncatedResponse(LLMError):
    """Ответ обрезан по max_tokens — вызывающий должен раздробить задачу."""

    def __init__(self, step: str, usage: dict[str, Any]) -> None:
        super().__init__(
            f"Шаг {step}: ответ обрезан по max_tokens ({usage.get('completion_tokens')} токенов)"
        )
        self.step = step
        self.usage = usage


class ReplayMiss(LLMError):
    """Режим replay: запроса нет в кеше."""


class MockFixtureMissing(LLMError):
    """Режим mock: нет фикстуры для шага."""


class LLMResponseInvalid(LLMError):
    """Ответ так и не прошёл валидацию схемы после ремонтных попыток."""


class GatewayError(LLMError):
    """Шлюз вернул ошибку, которую нельзя исправить повтором."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SchemaRejected(GatewayError):
    """Шлюз не принял response_format со схемой (внутреннее: переключает на запасной путь)."""


# --------------------------------------------------------------------------------------------------
# Стоимость и бюджет
# --------------------------------------------------------------------------------------------------


def cost_rub(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Стоимость по таблице цен навыка (кешированные шлюзом токены считаем по полной цене — консервативно)."""
    price_in, price_out = PRICES_RUB_PER_M.get(model, (0.0, 0.0))
    return round((prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000, 6)


def estimate_cost(calls: int, in_tokens: int, out_tokens: int, model: str | None = None) -> float:
    """Оценка стоимости для планировщика бюджета.

    in_tokens / out_tokens — суммарные видимые токены всех вызовов (промпт и ответ);
    к входу добавляется ≈1200 скрытых токенов шлюза на каждый вызов.
    """
    name = model or get_settings().default_model
    return cost_rub(name, in_tokens + calls * GATEWAY_OVERHEAD_TOKENS, out_tokens)


def spent_today_rub(cost_log: Path) -> float:
    """Расход за сегодня (МСК) по логу этой машины, без попаданий в кеш."""
    if not cost_log.exists():
        return 0.0
    today = now_msk().date().isoformat()
    total = 0.0
    for line in cost_log.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("ts", "")).startswith(today) and not row.get("cached"):
            total += float(row.get("cost_rub") or 0)
    return total


# --------------------------------------------------------------------------------------------------
# Схемы и JSON
# --------------------------------------------------------------------------------------------------

_SCHEMA_CHILD_MAPS = ("properties", "$defs", "definitions", "patternProperties")
_SCHEMA_CHILD_LISTS = ("anyOf", "allOf", "oneOf", "prefixItems")
_SCHEMA_CHILD_NODES = ("items", "not", "additionalProperties")


def strict_json_schema(schema_cls: type[BaseModel]) -> dict[str, Any]:
    """JSON-схема для response_format strict: у объектов все свойства обязательны,
    лишние запрещены; служебные title/default убраны."""
    schema = schema_cls.model_json_schema()
    _fix_schema_node(schema)
    return schema


def _fix_schema_node(node: Any) -> None:
    if not isinstance(node, dict):
        return
    node.pop("title", None)
    node.pop("default", None)
    if node.get("type") == "object" and isinstance(node.get("properties"), dict):
        node["additionalProperties"] = False
        node["required"] = list(node["properties"].keys())
    for key in _SCHEMA_CHILD_MAPS:
        children = node.get(key)
        if isinstance(children, dict):
            for child in children.values():
                _fix_schema_node(child)
    for key in _SCHEMA_CHILD_LISTS:
        children = node.get(key)
        if isinstance(children, list):
            for child in children:
                _fix_schema_node(child)
    for key in _SCHEMA_CHILD_NODES:
        _fix_schema_node(node.get(key))


def extract_first_json(text: str) -> Any:
    """Первый JSON-объект в тексте (учитывает строки и экранирование, игнорирует markdown вокруг)."""
    start = text.find("{")
    while start != -1:
        end = _matching_brace(text, start)
        if end is not None:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
        start = text.find("{", start + 1)
    raise ValueError("в ответе нет корректного JSON-объекта")


def _matching_brace(text: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def cache_key(body: dict[str, Any]) -> str:
    """sha256 от модели, сообщений, параметров и схемы (всё тело запроса, без ключа)."""
    material = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def json_instruction(schema_cls: type[BaseModel]) -> str:
    schema = json.dumps(strict_json_schema(schema_cls), ensure_ascii=False, separators=(",", ":"))
    return (
        "Ответь только одним JSON-объектом, без пояснений и без markdown, строго по этой JSON-схеме "
        f"(все поля обязательны, лишних полей нет): {schema}"
    )


def repair_instruction(error: Exception) -> str:
    if isinstance(error, ValidationError):
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'корень'}: {e['msg']}" for e in error.errors()[:15]
        )
    else:
        details = str(error)
    return (
        f"Ответ не прошёл проверку схемы: {details[:1500]}. "
        "Верни исправленный JSON-объект целиком — только JSON, без пояснений."
    )


# --------------------------------------------------------------------------------------------------
# Клиент
# --------------------------------------------------------------------------------------------------


@dataclass
class RawResponse:
    content: str
    usage: dict[str, Any]
    finish_reason: str | None
    model: str | None


@dataclass
class CallRecord:
    step: str
    model: str
    mode: str
    cached: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0
    cost_rub: float = 0.0
    seconds: float = 0.0
    finish_reason: str | None = None


class LLMClient:
    """Один клиент на сессию генерации: records копит расход по шагам для отчёта."""

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._shared_semaphore = (
            semaphore  # общий на сессию генерации: не больше N вызовов шлюза одновременно
        )
        self.records: list[CallRecord] = []
        self._transport = transport
        self._sleep = sleep
        self._semaphores: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}
        self._schema_unsupported: set[str] = set()

    # ---- публичный API ---------------------------------------------------------------------------

    async def complete_json(
        self,
        step: str,
        model: str | None,
        messages: list[Message],
        schema: type[T],
        *,
        max_tokens: int = 2000,
        temperature: float | None = 0.2,
        fixture_key: str | None = None,
    ) -> T:
        """Вызов модели с ответом по pydantic-схеме. Возвращает провалидированный объект.

        fixture_key — только для mock: стабильное имя фикстуры (например, хеш текста запроса), которое
        не меняется при правке промпта навыка, в отличие от хеша всего тела запроса.
        """
        model_name = model or self.settings.default_model
        if model_name == self.settings.reasoning_model:
            max_tokens = max(max_tokens, REASONING_MIN_TOKENS)

        use_schema = schema.__name__ not in self._schema_unsupported
        current = self._prepare_messages(messages, schema, use_schema)

        if self.settings.llm_mode == "mock":
            body = self._body(model_name, current, schema if use_schema else None, max_tokens, temperature)
            return self._from_fixture(step, model_name, schema, cache_key(body), fixture_key)

        repairs = 0
        origin_key: str | None = None  # ключ исходного запроса: исправленный ответ кешируется и под ним
        while True:
            body = self._body(model_name, current, schema if use_schema else None, max_tokens, temperature)
            try:
                raw, key, from_cache = await self._call(step, model_name, body)
                origin_key = origin_key or key
            except SchemaRejected:
                self._schema_unsupported.add(schema.__name__)
                use_schema = False
                current = self._prepare_messages(messages, schema, use_schema)
                continue
            if raw.finish_reason == "length":
                raise TruncatedResponse(step, raw.usage)  # обрезанный ответ в кеш не попадает
            try:
                parsed = self._parse(raw.content, schema)
            except (ValueError, ValidationError) as error:
                if repairs >= self.settings.max_repairs:
                    raise LLMResponseInvalid(
                        f"Шаг {step}: ответ модели не прошёл проверку схемы {schema.__name__} "
                        f"после {repairs} ремонтных попыток: {str(error)[:300]}"
                    ) from None
                repairs += 1
                current = [
                    *current,
                    {"role": "assistant", "content": raw.content[:6000]},
                    {"role": "user", "content": repair_instruction(error)},
                ]
                continue
            if not from_cache:
                # в кеш — только полный ответ, прошедший схему: иначе сбой закрепится и будет повторяться бесплатно
                for target in dict.fromkeys((key, origin_key or key)):
                    self._cache_put(target, step, model_name, raw)
            return parsed

    def complete_json_sync(self, *args: Any, **kwargs: Any) -> BaseModel:
        """Синхронная обёртка для скриптов и тестов (нельзя вызывать внутри работающего цикла asyncio)."""
        return asyncio.run(self.complete_json(*args, **kwargs))

    def remaining_budget(self) -> float:
        return max(self.settings.daily_budget_rub - spent_today_rub(self.settings.cost_log), 0.0)

    def cost_by_step(self) -> list[StepCost]:
        by_step: dict[str, StepCost] = {}
        for rec in self.records:
            item = by_step.setdefault(rec.step, StepCost(step=rec.step))
            item.calls += 1
            item.prompt_tokens += rec.prompt_tokens
            item.completion_tokens += rec.completion_tokens
            item.cost_rub = round(item.cost_rub + rec.cost_rub, 6)
            item.seconds = round(item.seconds + rec.seconds, 3)
        return list(by_step.values())

    # ---- сообщения и тело запроса ----------------------------------------------------------------

    @staticmethod
    def _prepare_messages(
        messages: list[Message], schema: type[BaseModel], use_schema: bool
    ) -> list[Message]:
        if use_schema:
            return list(messages)
        return [*messages, {"role": "user", "content": json_instruction(schema)}]

    def _body(
        self,
        model: str,
        messages: list[Message],
        schema: type[BaseModel] | None,
        max_tokens: int,
        temperature: float | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if temperature is not None:
            body["temperature"] = temperature
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": self.settings.schema_strict,
                    "schema": strict_json_schema(schema),
                },
            }
        return body

    @staticmethod
    def _parse(content: str, schema: type[T]) -> T:
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = extract_first_json(content)
        return schema.model_validate(data)

    # ---- mock ------------------------------------------------------------------------------------

    def _from_fixture(
        self, step: str, model: str, schema: type[T], key: str, fixture_key: str | None = None
    ) -> T:
        folder = self.settings.fixtures_dir / step
        candidates = [folder / f"{key}.json"]
        candidates += [folder / f"{fixture_key}.json"] if fixture_key else []
        candidates += [folder / "default.json"]
        candidates += sorted(folder.glob("*.json")) if folder.is_dir() else []
        for path in candidates:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                self.records.append(CallRecord(step=step, model=model, mode="mock", cached=False))
                return schema.model_validate(data)
        raise MockFixtureMissing(
            f"Режим mock: нет фикстуры для шага {step!r}. Создайте {folder}/default.json "
            f"(объект по схеме {schema.__name__}) или {folder}/{key}.json для конкретного запроса."
        )

    # ---- кеш, бюджет, учёт -----------------------------------------------------------------------

    async def _call(self, step: str, model: str, body: dict[str, Any]) -> tuple[RawResponse, str, bool]:
        """(ответ, ключ кеша, из кеша ли). В кеш ответ кладёт complete_json — только после проверки схемы."""
        key = cache_key(body)
        cached = self._cache_get(key)
        if cached is not None:
            self._record(step, model, cached, seconds=0.0, from_cache=True)
            return cached, key, True
        if self.settings.llm_mode == "replay":
            raise ReplayMiss(f"Режим replay: ответа для шага {step!r} нет в кеше ({key[:12]}…).")
        self._check_live_ready()
        async with self._semaphore():
            self._check_live_ready()  # в очереди к семафору можно простоять долго — лимит проверяется перед вызовом
            raw, seconds = await self._post_with_retries(body)
        self._record(step, model, raw, seconds=seconds, from_cache=False)
        return raw, key, False

    def _check_live_ready(self) -> None:
        if not self.settings.base_url or not self.settings.api_key.get_secret_value():
            raise LLMError("Режим live: не заданы OPENAI_BASE_URL или OPENAI_API_KEY.")
        spent = spent_today_rub(self.settings.cost_log)
        if spent >= self.settings.daily_budget_rub:
            raise BudgetExceeded(
                f"Суточный бюджет LLM исчерпан: потрачено {spent:.2f} ₽ из {self.settings.daily_budget_rub:.0f} ₽ "
                "(предохранитель LLM_DAILY_BUDGET_RUB). Живые вызовы отклонены."
            )

    def _cache_path(self, key: str) -> Path:
        return self.settings.cache_dir / f"{key}.json"

    def _cache_get(self, key: str) -> RawResponse | None:
        path = self._cache_path(key)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))["response"]
            return RawResponse(
                content=data["content"],
                usage=data.get("usage") or {},
                finish_reason=data.get("finish_reason"),
                model=data.get("model"),
            )
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def _cache_put(self, key: str, step: str, model: str, raw: RawResponse) -> None:
        path = self._cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "step": step,
            "model": model,
            "created_at": now_msk().isoformat(timespec="seconds"),
            "response": asdict(raw),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _record(self, step: str, model: str, raw: RawResponse, seconds: float, from_cache: bool) -> None:
        usage = raw.usage or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        details = usage.get("prompt_tokens_details") or {}
        record = CallRecord(
            step=step,
            model=model,
            mode=self.settings.llm_mode,
            cached=from_cache,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_prompt_tokens=int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0,
            cost_rub=0.0 if from_cache else cost_rub(model, prompt_tokens, completion_tokens),
            seconds=round(seconds, 3),
            finish_reason=raw.finish_reason,
        )
        self.records.append(record)
        self._append_cost_log(record)

    def _append_cost_log(self, record: CallRecord) -> None:
        row = {"ts": now_msk().isoformat(timespec="seconds"), **asdict(record)}
        log = self.settings.cost_log
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ---- HTTP ------------------------------------------------------------------------------------

    def _semaphore(self) -> asyncio.Semaphore:
        if self._shared_semaphore is not None:
            return self._shared_semaphore
        loop = asyncio.get_running_loop()
        for known in [lp for lp in self._semaphores if lp.is_closed()]:
            del self._semaphores[known]
        if loop not in self._semaphores:
            self._semaphores[loop] = asyncio.Semaphore(self.settings.concurrency)
        return self._semaphores[loop]

    async def _post_with_retries(self, body: dict[str, Any]) -> tuple[RawResponse, float]:
        url = f"{self.settings.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.api_key.get_secret_value()}"}
        # длинный ответ (module_pack до 8000 токенов) при 60–70 ток/с не должен упираться в общий таймаут
        read_s = max(self.settings.timeout_s, int(body.get("max_tokens") or 0) / MIN_TOKENS_PER_SECOND + 30)
        timeout = httpx.Timeout(read_s, connect=min(10.0, self.settings.timeout_s))
        attempts = self.settings.max_retries + 1 + RATE_LIMIT_EXTRA_RETRIES
        timeouts = 0
        failures = 0  # сбои сети и сервера: не больше max_retries повторов (доп. попытки — только на 429)
        rate_limited = 0
        waited = 0.0  # суммарное ожидание из-за 429
        for attempt in range(attempts):
            last = attempt == attempts - 1
            started = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
                    resp = await client.post(url, json=body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if isinstance(exc, httpx.TimeoutException):
                    timeouts += 1  # долгая генерация, вероятно, оплачена — повторяем её не больше одного раза
                failures += 1
                if last or timeouts > 1 or failures > self.settings.max_retries:
                    raise GatewayError(f"Шлюз недоступен: {type(exc).__name__}") from None
                await self._sleep(self._backoff(failures - 1, None))
                continue
            seconds = time.perf_counter() - started
            if resp.status_code == 200:
                return self._raw_from_response(resp), seconds
            if resp.status_code in (400, 422) and "response_format" in body:
                raise SchemaRejected(self._error_text(resp), status=resp.status_code)
            if resp.status_code == 429 and not last:
                # «too many concurrent requests from this team»: место освободится, когда закончится чужой вызов
                # (10–30 с) — ждём дольше, чем при сбое сервера
                rate_limited += 1
                pause = self._rate_limit_backoff(rate_limited, resp.headers.get("retry-after"))
                if waited + pause > MAX_RATE_LIMIT_WAIT_S:
                    raise GatewayError(self._error_text(resp), status=resp.status_code)
                waited += pause
                await self._sleep(pause)
                continue
            server_error = resp.status_code in RETRYABLE_STATUS and resp.status_code != 429  # 429 — выше
            if server_error and not last and failures < self.settings.max_retries:
                failures += 1
                await self._sleep(self._backoff(failures - 1, resp.headers.get("retry-after")))
                continue
            raise GatewayError(self._error_text(resp), status=resp.status_code)
        raise GatewayError("Шлюз не ответил после повторов")  # недостижимо, для анализатора

    @staticmethod
    def _rate_limit_backoff(n: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
        return min(5.0 * n, 30.0) * (0.75 + random.random() / 2)

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
        return min(2.0**attempt, 30.0) * (0.75 + random.random() / 2)

    def _error_text(self, resp: httpx.Response) -> str:
        secret = self.settings.api_key.get_secret_value()
        text = resp.text[:500]
        if secret:
            text = text.replace(secret, "***")
        return f"Шлюз вернул HTTP {resp.status_code}: {text}"

    @staticmethod
    def _raw_from_response(resp: httpx.Response) -> RawResponse:
        try:
            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            return RawResponse(
                content=message.get("content") or "",
                usage=data.get("usage") or {},
                finish_reason=choice.get("finish_reason"),
                model=data.get("model"),
            )
        except (ValueError, AttributeError, IndexError) as exc:
            raise GatewayError(f"Шлюз вернул неожиданный ответ: {type(exc).__name__}") from None


# --------------------------------------------------------------------------------------------------
# Клиент по умолчанию
# --------------------------------------------------------------------------------------------------

_default_client: LLMClient | None = None


def get_client() -> LLMClient:
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client


async def complete_json(
    step: str,
    model: str | None,
    messages: list[Message],
    schema: type[T],
    **kwargs: Any,
) -> T:
    return await get_client().complete_json(step, model, messages, schema, **kwargs)
