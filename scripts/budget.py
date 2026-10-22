#!/usr/bin/env python3
"""Расход GigaChat за сегодня (МСК) из logs/llm_cost.jsonl. --short — одной строкой."""

import argparse
import collections
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import MSK, get_settings  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--short", action="store_true")
    ap.add_argument("--log", default="logs/llm_cost.jsonl")
    a = ap.parse_args()
    limit = get_settings().daily_budget_rub
    today = dt.datetime.now(MSK).date().isoformat()
    total, calls, cached = 0.0, 0, 0
    by: dict[tuple[str, str], float] = collections.defaultdict(float)
    if os.path.exists(a.log):
        with open(a.log, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if not str(r.get("ts", "")).startswith(today):
                    continue
                calls += 1
                if r.get("cached"):
                    cached += 1
                    continue
                c = float(r.get("cost_rub", 0) or 0)
                total += c
                by[(r.get("step") or "?", r.get("model") or "?")] += c
    if a.short:
        print(f"{total:.2f} ₽ из {limit:.0f} ₽ ({calls} вызовов, из кеша {cached})")
        return
    print(f"Сегодня {today}: {total:.2f} ₽ из {limit:.0f} ₽, осталось {max(limit - total, 0):.2f} ₽")
    print(f"Вызовов: {calls}, из кеша: {cached}")
    for (step, model), c in sorted(by.items(), key=lambda x: -x[1]):
        print(f"  {step:<24} {model:<45} {c:8.2f} ₽")


if __name__ == "__main__":
    main()
