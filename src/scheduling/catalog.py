"""资源目录：维护讲解员、讲师、场地、学校团体的主数据。"""
from __future__ import annotations

from .models import Group, ROLES, SessionReq, Staff, Venue, Window


class Catalog:
    def __init__(self, default_travel_minutes: int = 30) -> None:
        self._staff: dict[str, Staff] = {}
        self._venues: dict[str, Venue] = {}
        self._groups: dict[str, Group] = {}
        self.default_travel_minutes = default_travel_minutes

    # ---- 变更 ----------------------------------------------------------
    def upsert_staff(self, staff: Staff) -> None:
        if staff.role not in ROLES:
            raise ValueError(f"未知人员角色：{staff.role}")
        self._staff[staff.id] = staff

    def upsert_venue(self, venue: Venue) -> None:
        if venue.capacity <= 0:
            raise ValueError("场地容量必须为正")
        self._venues[venue.id] = venue

    def upsert_group(self, group: Group) -> None:
        if group.headcount <= 0:
            raise ValueError("团体人数必须为正")
        self._groups[group.id] = group

    # ---- 查询 ----------------------------------------------------------
    def staff(self, staff_id: str) -> Staff:
        return self._staff[staff_id]

    def venue(self, venue_id: str) -> Venue:
        return self._venues[venue_id]

    def group(self, group_id: str) -> Group:
        return self._groups[group_id]

    def has_group(self, group_id: str) -> bool:
        return group_id in self._groups

    def eligible_staff(self, req: SessionReq) -> list[Staff]:
        """角色 + 资格双匹配。"""
        return [
            s for s in self._staff.values()
            if s.role == req.role and req.qualification in s.qualifications
        ]

    def candidate_venues(self, req: SessionReq, headcount: int) -> list[Venue]:
        if req.venue_id is not None:
            venue = self._venues.get(req.venue_id)
            return [venue] if venue is not None and venue.capacity >= headcount else []
        return [v for v in self._venues.values() if v.capacity >= headcount]

    def all_venues(self) -> list[Venue]:
        return list(self._venues.values())

    def travel_minutes(self, staff: Staff, venue_a: str, venue_b: str) -> int:
        """两点移动缓冲，键按无向处理；同厅为 0，缺省回落默认缓冲。"""
        if venue_a == venue_b:
            return 0
        for key in (f"{venue_a}>{venue_b}", f"{venue_b}>{venue_a}"):
            if key in staff.travel:
                return staff.travel[key]
        return self.default_travel_minutes

    @staticmethod
    def within_any(window: tuple[Window, ...], start: int, end: int) -> bool:
        return any(w.start <= start and end <= w.end for w in window)
