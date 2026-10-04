"""占用查询与缓冲冲突检测。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .catalog import Catalog
from .models import CONFIRMED, Reservation


@dataclass(frozen=True)
class Booking:
    reservation_id: str
    status: str
    req_id: str
    staff_id: str
    venue_id: str
    start: int
    end: int


def bookings_from(reservations: list[Reservation]) -> list[Booking]:
    out: list[Booking] = []
    for r in reservations:
        for a in r.assignments:
            out.append(Booking(
                reservation_id=r.id, status=r.status, req_id=a.req_id,
                staff_id=a.staff_id, venue_id=a.venue_id, start=a.start, end=a.end,
            ))
    return out


class Occupancy:
    """活动占用索引：同一时刻同一场地只允许一场；同人员相邻场计入移动缓冲。"""

    def __init__(self, bookings: list[Booking],
                 statuses: tuple[str, ...] = (CONFIRMED,)) -> None:
        self._by_staff: dict[str, list[Booking]] = {}
        self._by_venue: dict[str, list[Booking]] = {}
        for b in bookings:
            if b.status in statuses:
                self._by_staff.setdefault(b.staff_id, []).append(b)
                self._by_venue.setdefault(b.venue_id, []).append(b)
        for lst in self._by_staff.values():
            lst.sort(key=lambda x: x.start)
        for lst in self._by_venue.values():
            lst.sort(key=lambda x: x.start)

    def staff_clash(self, catalog: Catalog, staff_id: str, venue_id: str,
                    start: int, end: int) -> Optional[Booking]:
        """返回与候选场次冲突（含跨厅移动缓冲）的既有占用。"""
        staff = catalog.staff(staff_id)
        for b in self._by_staff.get(staff_id, ()):
            gap = catalog.travel_minutes(staff, venue_id, b.venue_id)
            # 候选在 b 之后：start 必须 >= b.end + gap；
            # 候选在 b 之前：b.start 必须 >= end + gap。
            if start < b.end + gap and b.start < end + gap:
                return b
        return None

    def venue_clash(self, venue_id: str, start: int, end: int) -> Optional[Booking]:
        for b in self._by_venue.get(venue_id, ()):
            if start < b.end and b.start < end:
                return b
        return None
