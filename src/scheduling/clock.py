"""时钟抽象，便于测试与过期回收。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now_minutes(self) -> int: ...


class SystemClock:
    def now_minutes(self) -> int:
        return int(datetime.now(timezone.utc).timestamp() // 60)


class FakeClock:
    """测试用可控时钟。"""

    def __init__(self, minutes: int = 0) -> None:
        self._minutes = minutes

    def now_minutes(self) -> int:
        return self._minutes

    def advance(self, minutes: int) -> None:
        if minutes < 0:
            raise ValueError("时钟只能前进")
        self._minutes += minutes
