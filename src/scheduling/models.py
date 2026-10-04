"""领域模型：资源、资格、可用时段、时间区间、候选方案、排期条目。

时间在存储与比较层统一使用 *epoch 分钟*（int），避免时区与浮点误差；
对外接口使用 ISO8601 字符串。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .clock import parse_dt

MINUTE = 60


def to_min(value: str | datetime) -> int:
    dt = value if isinstance(value, datetime) else parse_dt(value)
    return int(dt.timestamp()) // MINUTE


def min_to_iso(value: int) -> str:
    """epoch 分钟 -> UTC ISO8601（Z 结尾）。"""
    return datetime.fromtimestamp(value * MINUTE, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


class ResourceKind(str, Enum):
    GUIDE = "guide"        # 讲解员
    LECTURER = "lecturer"  # 讲师
    VENUE = "venue"        # 场地/展厅
    GROUP = "group"        # 学校团体（容量受限于人数）


@dataclass(frozen=True)
class Qualification:
    """资格/能力标签，例如 ``lecture:航天``、``venue:展厅``、``language:en``。

    讲解员/讲师持有的资格需覆盖场次 ``required_qualifications``。
    """

    code: str

    def __str__(self) -> str:
        return self.code


@dataclass
class TimeInterval:
    """半开时间区间 ``[start, end)``，单位 epoch 分钟。"""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"时间区间非法：{self.start} >= {self.end}")

    @property
    def duration(self) -> int:
        return self.end - self.start

    def overlaps(self, other: "TimeInterval") -> bool:
        return self.start < other.end and other.start < self.end

    def to_dict(self) -> dict:
        return {"start": min_to_iso(self.start), "end": min_to_iso(self.end),
                "start_min": self.start, "end_min": self.end}


@dataclass
class AvailabilityWindow:
    """资源的可用时段，同样为半开区间。"""

    start: int
    end: int
    weekday: int | None = None  # 预留：周期性可用（0=周一）

    def contains(self, iv: TimeInterval) -> bool:
        return self.start <= iv.start and iv.end <= self.end

    def to_dict(self) -> dict:
        return {"start_min": self.start, "end_min": self.end}


@dataclass
class Resource:
    """可被排期占用的资源。

    - staff（guide/lecturer）：capacity 表示并行场次上限（通常为 1），
      ``travel_buffer_min`` 为两场之间的移动缓冲（分钟）；
    - venue：capacity 表示展厅同时容纳场次/人数的能力，可按占用模式解释；
    - group：学校团体，capacity 即学生人数占用，travel_buffer 可表示转场时间。
    """

    id: str
    kind: ResourceKind
    name: str
    qualifications: frozenset[str] = frozenset()
    capacity: int = 1
    travel_buffer_min: int = 0
    availability: list[AvailabilityWindow] = field(default_factory=list)
    active: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "name": self.name,
            "qualifications": sorted(self.qualifications),
            "capacity": self.capacity,
            "travel_buffer_min": self.travel_buffer_min,
            "availability": [w.to_dict() for w in self.availability],
            "active": self.active,
        }


@dataclass
class CandidateEntry:
    """候选方案中的单场安排。"""

    session_id: str
    title: str
    start: int               # epoch 分钟
    end: int
    resource_ids: list[str]  # 该场锁定的全部资源（人/场/团体）
    required_qualifications: list[str] = field(default_factory=list)
    expected_group_size: int = 0  # 排定时登记的学校人数（用于人数变化检测）
    depends_on: list[str] = field(default_factory=list)  # 场次依赖：前置场次 id

    def interval(self) -> TimeInterval:
        return TimeInterval(self.start, self.end)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "start": min_to_iso(self.start),
            "end": min_to_iso(self.end),
            "start_min": self.start,
            "end_min": self.end,
            "resource_ids": list(self.resource_ids),
            "required_qualifications": list(self.required_qualifications),
            "expected_group_size": self.expected_group_size,
            "depends_on": list(self.depends_on),
        }


@dataclass
class Candidate:
    """排定前生成的候选方案（暂占）。"""

    id: str
    entries: list[CandidateEntry]
    status: str = "pending"   # pending / confirmed / cancelled / expired
    created_ts: float = 0.0
    expires_ts: float = 0.0
    version: int = 1          # 资源/人数快照版本，确认时校验
    note: str = ""

    def resource_set(self) -> set[str]:
        out: set[str] = set()
        for e in self.entries:
            out.update(e.resource_ids)
        return out

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "created_ts": self.created_ts,
            "expires_ts": self.expires,
            "version": self.version,
            "note": self.note,
            "entries": [e.to_dict() for e in self.entries],
        }


@dataclass
class ScheduleEntry:
    """已确认排期条目。"""

    id: int
    session_id: str
    title: str
    start: int
    end: int
    candidate_id: str
    resource_ids: list[str]
    confirmed_ts: float

    def interval(self) -> TimeInterval:
        return TimeInterval(self.start, self.end)

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "session_id": self.session_id,
            "title": self.title,
            "start": min_to_iso(self.start),
            "end": min_to_iso(self.end),
            "start_min": self.start,
            "end_min": self.end,
            "candidate_id": self.candidate_id,
            "resource_ids": list(self.resource_ids),
            "confirmed_ts": self.confirmed_ts,
        }
        return d


@dataclass
class Conflict:
    """结构化冲突描述，驱动可解释响应。"""

    resource_id: str
    resource_name: str
    kind: str            # double_booking / buffer / capacity / availability /
    #                      qualification / dependency / inactive
    session_id: str
    interval: TimeInterval
    message: str
    conflicting_session: str | None = None
    alternatives: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "resource_id": self.resource_id,
            "resource_name": self.resource_name,
            "kind": self.kind,
            "session_id": self.session_id,
            "interval": self.interval.to_dict(),
            "message": self.message,
            "conflicting_session": self.conflicting_session,
            "alternatives": self.alternatives,
        }


@dataclass
class SchoolGroup:
    """学校团体及其可变人数。"""

    id: str
    name: str
    size: int
    contact: str = ""
    active: bool = True

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "size": self.size,
                "contact": self.contact, "active": self.active}
