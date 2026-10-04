"""开放日多资源排班领域模型。

时间在服务内部统一使用「 epoch 分钟 」整数表示，API 边界负责与 ISO-8601 互转。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# 排期单状态
HELD = "held"            # 待确认（暂占，带过期时间）
CONFIRMED = "confirmed"  # 已排定（原子锁定）
CANCELLED = "cancelled"  # 已取消（主动取消）
EXPIRED = "expired"      # 暂占过期回收 / 人数变化联动失效 / 确认复检失败
SUPERSEDED = "superseded"  # 他人并发确认获胜，本暂占被原子作废
TERMINAL = (CANCELLED, EXPIRED, SUPERSEDED)

GUIDE = "guide"         # 讲解员
LECTURER = "lecturer"   # 讲师
ROLES = (GUIDE, LECTURER)


@dataclass(frozen=True)
class Window:
    """半开可用时段 [start, end)。"""

    start: int
    end: int


@dataclass(frozen=True)
class Dependency:
    """场次依赖：本场开始时间 >= 依赖场次结束时间 + gap_minutes。"""

    after_req_id: str
    gap_minutes: int = 0


@dataclass(frozen=True)
class Staff:
    """讲解员 / 讲师。

    travel 为跨展厅移动分钟数，键形如 "V1>V2"（查询时按无向处理），
    未配置时回落到 Catalog.default_travel_minutes。
    """

    id: str
    name: str
    role: str
    qualifications: tuple[str, ...]
    availability: tuple[Window, ...]
    travel: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Venue:
    """场地 / 展厅。"""

    id: str
    name: str
    capacity: int
    availability: tuple[Window, ...]


@dataclass(frozen=True)
class Group:
    """学校团体。"""

    id: str
    name: str
    headcount: int


@dataclass(frozen=True)
class SessionReq:
    """场次需求。

    venue_id 非空时表示必须使用该展厅；否则由排程器在容量足够的场地中选择。
    """

    req_id: str
    role: str
    qualification: str
    duration: int
    earliest_start: int
    latest_start: Optional[int] = None
    preferred_start: Optional[int] = None
    venue_id: Optional[str] = None
    after: tuple[Dependency, ...] = ()


@dataclass(frozen=True)
class PlannedAssignment:
    """排定的一场：某人员在某展厅的 [start, end)。"""

    req_id: str
    staff_id: str
    venue_id: str
    start: int
    end: int


@dataclass(frozen=True)
class Candidate:
    assignments: tuple[PlannedAssignment, ...]
    score: float
    explanations: tuple[str, ...]
    signature: tuple
    contended: bool = False  # 与他人待确认暂占存在竞争，确认可能落败


@dataclass(frozen=True)
class Blocker:
    """冲突 / 不可行原因，结构化字段 + 中文解释。"""

    resource_type: str          # staff / venue / group / qualification / capacity / availability / dependency
    resource_id: Optional[str]
    reason: str
    message: str
    reservation_id: Optional[str] = None
    start: Optional[int] = None
    end: Optional[int] = None


@dataclass(frozen=True)
class Suggestion:
    """可解释替代方案。"""

    kind: str           # EXTEND_WINDOW / WAIT_FOR_HOLDS / SHRINK_GROUP / ...
    message: str
    candidates: tuple[Candidate, ...] = ()


@dataclass(frozen=True)
class Diagnosis:
    candidates: tuple[Candidate, ...]
    blockers: tuple[Blocker, ...] = ()
    suggestions: tuple[Suggestion, ...] = ()


@dataclass(frozen=True)
class Reservation:
    id: str
    group_id: str
    status: str
    created_at: int
    expires_at: Optional[int]
    confirmed_at: Optional[int]
    release_reason: Optional[str]
    assignments: tuple[PlannedAssignment, ...]
    request_sessions: tuple[dict, ...] = ()
    version: int = 1
