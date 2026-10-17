#!/usr/bin/env python3
"""Проверка шлюза LLM: список моделей, один короткий вызов, поддержка structured output.

Переменные OPENAI_BASE_URL и OPENAI_API_KEY берутся из окружения или .env (через python-dotenv).
Ключ никогда не печатается. Каждый платный вызов пишется строкой в logs/llm_cost.jsonl.

Запуск: python scripts/gateway_check.py [--force]
  --force — разрешить запуск в окно автопрогона 06:00–10:00 МСК (по умолчанию запрещено).
Код выхода: 0 — шлюз отвечает, 1 — ошибка конфигурации/бюджета, 2 — шлюз недоступен.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
COST_LOG = ROOT / "logs" / "llm_cost.jsonl"
MSK = dt.timezone(dt.timedelta(hours=3))
MODEL = "GigaChat/GigaChat-3-Pro"
TIMEOUT_S = 60.0

# ₽ за 1M токенов (вход, выход) — из навыка gigachat-gateway
PRICES_RUB_PER_M: dict[str, tuple[float, float]] = {
    "GigaChat/GigaChat-3-Pro": (73.03, 176.39),
    "ai-sage/GigaChat3.5-432B-A28B-Reasoning": (96.22, 288.60),
    "ai-sage/Giga-Embeddings-instruct-3B": (32.22, 0.0),
}

TINY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "word": {"type": "string"},
    },
    "required": ["ok", "word"],
    "additionalProperties": False,
}


class GatewayUnavailable(Exception):
    """Сеть/DNS/таймаут — до шлюза не достучались."""


def now_msk() -> dt.datetime:
    return dt.datetime.now(MSK)


def cost_rub(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price_in, price_out = PRICES_RUB_PER_M.get(model, (0.0, 0.0))
    return round((prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000, 6)


def spent_today_rub() -> float:
    if not COST_LOG.exists():
        return 0.0
    today = now_msk().date().isoformat()
    total = 0.0
    for line in COST_LOG.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("ts", "")).startswith(today) and not row.get("cached"):
            total += float(row.get("cost_rub") or 0)
    return total


def log_cost(step: str, model: str, usage: dict[str, Any]) -> float:
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    cost = cost_rub(model, pt, ct)
    row = {
        "ts": now_msk().isoformat(timespec="seconds"),
        "step": step,
        "model": model,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "cost_rub": cost,
        "cached": False,
    }
    COST_LOG.parent.mkdir(parents=True, exist_ok=True)
    with COST_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return cost


def redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def request(
    client: httpx.Client, method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[httpx.Response, float]:
    started = time.perf_counter()
    try:
        resp = client.request(method, path, json=body)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ProxyError) as exc:
        raise GatewayUnavailable(f"{type(exc).__name__}: {exc}") from exc
    return resp, time.perf_counter() - started


def check_models(client: httpx.Client, key: str) -> None:
    print("\n[1] GET /models")
    resp, elapsed = request(client, "GET", "/models")
    print(f"    HTTP {resp.status_code}, {elapsed:.2f} с")
    if resp.status_code != 200:
        print("    тело:", redact(resp.text[:500], key))
        return
    data = resp.json().get("data", [])
    ids = sorted(str(m.get("id")) for m in data if isinstance(m, dict))
    print(f"    моделей: {len(ids)}")
    for mid in ids:
        mark = "  <- в таблице цен" if mid in PRICES_RUB_PER_M else ""
        print(f"      {mid}{mark}")


def chat(client: httpx.Client, key: str, step: str, body: dict[str, Any]) -> dict[str, Any] | None:
    resp, elapsed = request(client, "POST", "/chat/completions", body)
    print(f"    HTTP {resp.status_code}, {elapsed:.2f} с")
    if resp.status_code != 200:
        print("    тело:", redact(resp.text[:800], key))
        return None
    data = resp.json()
    usage = data.get("usage") or {}
    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content")
    ct = int(usage.get("completion_tokens") or 0)
    cost = log_cost(step, body["model"], usage)
    speed = f"{ct / elapsed:.1f} ток/с (с учётом задержки)" if elapsed > 0 and ct else "—"
    print(f"    model в ответе: {data.get('model')}")
    print(f"    usage: {json.dumps(usage, ensure_ascii=False)}")
    print(f"    finish_reason: {choice.get('finish_reason')}, скорость: {speed}")
    print(f"    стоимость: {cost:.6f} ₽")
    print(f"    content: {content!r}")
    return {"content": content, "elapsed": elapsed, "usage": usage, "cost": cost}


def check_plain(client: httpx.Client, key: str) -> None:
    print(f"\n[2] chat/completions, {MODEL}, max_tokens=20")
    chat(
        client,
        key,
        "gateway_check",
        {
            "model": MODEL,
            "max_tokens": 20,
            "messages": [{"role": "user", "content": "Ответь одним словом: столица России?"}],
        },
    )


def check_structured(client: httpx.Client, key: str) -> None:
    print(f"\n[3] chat/completions + response_format=json_schema, {MODEL}")
    result = chat(
        client,
        key,
        "gateway_check_json",
        {
            "model": MODEL,
            "max_tokens": 40,
            "messages": [
                {
                    "role": "user",
                    "content": "Верни объект: ok = true, word = слово «привет».",
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "tiny", "strict": True, "schema": TINY_SCHEMA},
            },
        },
    )
    if result is None:
        print("    ВЫВОД: шлюз НЕ принимает response_format=json_schema → нужен fallback")
        return
    try:
        parsed = json.loads(result["content"] or "")
    except json.JSONDecodeError:
        print("    ВЫВОД: параметр принят, но ответ не JSON → схема игнорируется, нужен fallback")
        return
    keys_ok = isinstance(parsed, dict) and set(parsed) == {"ok", "word"}
    types_ok = keys_ok and isinstance(parsed["ok"], bool) and isinstance(parsed["word"], str)
    if types_ok:
        print("    ВЫВОД: structured output работает, ответ соответствует схеме")
    else:
        print("    ВЫВОД: ответ JSON, но не по схеме → валидировать pydantic и держать fallback")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="разрешить запуск в 06:00–10:00 МСК")
    args = ap.parse_args()

    if 6 <= now_msk().hour < 10 and not args.force:
        print("Сейчас окно автопрогона 06:00–10:00 МСК — live-вызовы с ноутбука запрещены.")
        return 1

    load_dotenv(ROOT / ".env")
    base_url = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
    key = os.environ.get("OPENAI_API_KEY", "")
    missing = [n for n, v in (("OPENAI_BASE_URL", base_url), ("OPENAI_API_KEY", key)) if not v]
    if missing:
        env_file = "есть" if (ROOT / ".env").exists() else "нет"
        print(f"Не заданы: {', '.join(missing)} (файл .env: {env_file}; пустое значение = не задано).")
        return 1

    budget = float(os.environ.get("LLM_DAILY_BUDGET_RUB", "90"))
    spent = spent_today_rub()
    if spent >= budget:
        print(f"Расход за сегодня {spent:.2f} ₽ ≥ предохранителя {budget:.0f} ₽ — вызовы отменены.")
        return 1

    print(f"Шлюз: {base_url}  (ключ задан, длина {len(key)})")
    print(f"Расход за сегодня до проверки: {spent:.4f} ₽")
    headers = {"Authorization": f"Bearer {key}"}
    try:
        with httpx.Client(base_url=base_url, headers=headers, timeout=TIMEOUT_S) as client:
            check_models(client, key)
            check_plain(client, key)
            check_structured(client, key)
    except GatewayUnavailable as exc:
        print(f"\nШлюз недоступен: {redact(str(exc), key)}")
        print("Live-вызовы гоняем на ВМ; на ноутбуке — mock/replay.")
        return 2
    print(f"\nРасход за сегодня после проверки: {spent_today_rub():.4f} ₽")
    return 0


if __name__ == "__main__":
    sys.exit(main())
