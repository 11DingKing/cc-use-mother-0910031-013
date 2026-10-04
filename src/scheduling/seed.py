"""演示主数据：两位讲解员、一位讲师、两个展厅、两个学校团体。

基准日 2026-10-10（UTC），全部资源 08:00-18:00 可用。
"""
from __future__ import annotations

from datetime import datetime, timezone

from .models import Group, Staff, Venue, Window
from .service import SchedulingService

DAY = datetime(2026, 10, 10, tzinfo=timezone.utc)
BASE = int(DAY.timestamp() // 60)
M = 60


def t(hour: int, minute: int = 0) -> int:
    return BASE + hour * M + minute


def seed_demo(service: SchedulingService) -> None:
    day = (Window(t(8), t(18)),)
    service.upsert_staff(Staff(
        id="G-ANNA", name="讲解员安娜", role="guide",
        qualifications=("常规讲解", "特展讲解"),
        availability=day, travel={"V1>V2": 20, "V2>V3": 20, "V1>V3": 40}))
    service.upsert_staff(Staff(
        id="G-BEN", name="讲解员本", role="guide",
        qualifications=("常规讲解",), availability=day,
        travel={"V1>V2": 20, "V2>V3": 20, "V1>V3": 40}))
    service.upsert_staff(Staff(
        id="L-CARA", name="讲师卡拉", role="lecturer",
        qualifications=("科普讲座",), availability=day,
        travel={"V1>V2": 15, "V2>V3": 15, "V1>V3": 30}))
    service.upsert_venue(Venue(
        id="V1", name="主展厅", capacity=120, availability=day))
    service.upsert_venue(Venue(
        id="V2", name="特展厅", capacity=60, availability=day))
    service.upsert_venue(Venue(
        id="V3", name="阶梯教室", capacity=200, availability=day))
    service.upsert_group(Group(id="SCH-01", name="晨光中学", headcount=90))
    service.upsert_group(Group(id="SCH-02", name="星火小学", headcount=45))
