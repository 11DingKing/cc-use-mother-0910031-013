"""时间与时钟抽象。

业务时间全部走 ``Clock`` 接口，便于测试过期回收；缓冲比较统一用 epoch 分钟整数。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Protocol


def now_ts() -> float:
    return time.time()


def parse_dt(value: str) -> datetime:
    """解析 ISO8601 时间，支持带 Z 后缀。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz())
    return dt


def tz() -> timezone:
    return timezone.utc


class Clock(Protocol):
    def now(self) -> datetime: ...

    def ts(self) -> float: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def ts(self) -> float:
        return time.time()


class ManualClock:
    """测试用时钟，可自由推进。"""

    def __init__(self, start: float | None = None) -> None:
        self._ts = start if start is not None else time.time()

    def now(self) -> datetime:
        return datetime.fromtimestamp(self._ts, timezone.utc)

    def ts(self) -> float:
        return self._ts

    def advance(self, seconds: float) -> None:
        self._ts += seconds

    def set(self, value: float) -> None:
        self._ts = value
