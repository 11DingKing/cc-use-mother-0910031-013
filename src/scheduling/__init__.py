"""开放日多资源排班服务端。

领域：开放日活动中，讲解员、讲师、场地、学校团体等多类资源需要协同排表。
核心协议为两阶段：

1. ``plan``  —— 排定前生成候选方案（不持有任何锁，仅做可行性快照）；
2. ``confirm`` —— 确认时原子锁定方案涉及的全部资源；任何一类资源不可用则整体失败。

候选方案带有过期时间，过期暂占在查询与重启时自动回收。
"""
from __future__ import annotations

from .clock import Clock, ManualClock, SystemClock
from .candidates import (
    CandidateEngine,
    CandidateRequest,
    PlanResult,
    RoleReq,
    SessionReq,
)
from .exceptions import (
    CandidateExpired,
    CandidateNotFound,
    DomainError,
    NotConfirmed,
    QualificationError,
    ScheduleConflict,
    StaleCandidateVersion,
)
from .models import (
    AvailabilityWindow,
    Candidate,
    CandidateEntry,
    Conflict,
    Qualification,
    Resource,
    ResourceKind,
    ScheduleEntry,
    SchoolGroup,
    TimeInterval,
)
from .service import SchedulingService
from .store import SQLiteStore

__all__ = [
    "AvailabilityWindow",
    "Candidate",
    "CandidateEntry",
    "CandidateEngine",
    "CandidateExpired",
    "CandidateNotFound",
    "CandidateRequest",
    "Clock",
    "Conflict",
    "DomainError",
    "ManualClock",
    "NotConfirmed",
    "PlanResult",
    "Qualification",
    "QualificationError",
    "Resource",
    "ResourceKind",
    "RoleReq",
    "SQLiteStore",
    "ScheduleConflict",
    "ScheduleEntry",
    "SchedulingService",
    "SchoolGroup",
    "SessionReq",
    "StaleCandidateVersion",
    "SystemClock",
    "TimeInterval",
]
