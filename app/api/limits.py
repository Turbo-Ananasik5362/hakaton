"""Ограничение частоты новых сессий с одного адреса (скользящее окно в памяти процесса).

Новая сессия — это вызовы модели и деньги из общего суточного бюджета, поэтому поток запросов с одного адреса
ограничен: не больше Settings.rate_limit_per_minute новых сессий в минуту. Сверх лимита — 429 с понятным JSON
и заголовком Retry-After. Число сессий в работе ограничивает оркестратор (Settings.max_active_sessions).
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable

from app.pipeline.base import RateLimited

WINDOW_S = 60.0
MAX_TRACKED_ADDRESSES = 10_000  # больше — забываем адреса без недавних запросов


class SessionRateLimiter:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute = per_minute
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}

    def check(self, address: str) -> None:
        """Учесть новую сессию с адреса или отказать (RateLimited)."""
        now = self.clock()
        hits = self._hits.setdefault(address, deque())
        while hits and now - hits[0] >= WINDOW_S:
            hits.popleft()
        if len(hits) >= self.per_minute:
            retry_after = max(1, math.ceil(WINDOW_S - (now - hits[0])))
            raise RateLimited(
                f"Слишком много новых запросов с вашего адреса: не больше {self.per_minute} в минуту. "
                f"Повторите через {retry_after} с.",
                retry_after=retry_after,
                details={"limit_per_minute": self.per_minute},
            )
        hits.append(now)
        if len(self._hits) > MAX_TRACKED_ADDRESSES:
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < WINDOW_S}
