"""领域错误类型。

错误码稳定，可直接用于 HTTP 响应与自动化断言；消息面向统筹员，中文可解释。
"""
from __future__ import annotations


class DomainError(Exception):
    """所有排班领域错误的基类，携带稳定错误码。"""

    code = "domain_error"

    def __init__(self, message: str = "", **extra: object) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.extra = extra

    def to_dict(self) -> dict:
        data = {"code": self.code, "message": self.message}
        data.update(self.extra)
        return data


class NotFound(DomainError):
    code = "not_found"


class CandidateNotFound(NotFound):
    code = "candidate_not_found"


class ResourceNotFound(NotFound):
    code = "resource_not_found"


class GroupNotFound(NotFound):
    code = "group_not_found"


class EntryNotFound(NotFound):
    code = "entry_not_found"


class QualificationError(DomainError):
    """资源不满足场次的资格/技能要求。"""

    code = "qualification_mismatch"


class ScheduleConflict(DomainError):
    """确认或直接排定时资源发生冲突（含缓冲、容量、依赖）。"""

    code = "schedule_conflict"

    def __init__(self, message: str, conflicts: list[dict] | None = None,
                 alternatives: list[dict] | None = None) -> None:
        super().__init__(message)
        self.conflicts = conflicts or []
        self.alternatives = alternatives or []

    def to_dict(self) -> dict:
        data = super().to_dict()
        if self.conflicts:
            data["conflicts"] = self.conflicts
        if self.alternatives:
            data["alternatives"] = self.alternatives
        return data


class CandidateExpired(DomainError):
    code = "candidate_expired"


class StaleCandidateVersion(DomainError):
    """确认时实际人数/资源状态与候选快照不一致。"""

    code = "stale_candidate_version"


class NotConfirmed(DomainError):
    code = "not_confirmed"
