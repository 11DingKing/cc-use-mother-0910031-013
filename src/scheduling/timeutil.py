"""ISO-8601 与 epoch 分钟互转（一律按 UTC 解释）。"""
from __future__ import annotations

from datetime import datetime, timezone


def to_minutes(value: str) -> int:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() // 60)


def to_iso(minutes: int) -> str:
    dt = datetime.fromtimestamp(minutes * 60, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def hm(minutes: int) -> str:
    """跨日期也能稳定显示的 HH:MM，用于解释文案。"""
    dt = datetime.fromtimestamp(minutes * 60, tz=timezone.utc)
    return dt.strftime("%H:%M")
