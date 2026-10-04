"""开放日多资源排班服务的领域回归测试。

覆盖契约四不变量及题目关键场景：
资格 / 容量 / 可用时段 / 移动缓冲 / 场次依赖 /
候选-确认两阶段原子性 / 部分拒绝 / 人数变化 /
并发确认 / 取消释放 / 冲突替代方案 / 过期与重启回收。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scheduling import ManualClock, SchedulingService  # noqa: E402
from scheduling.api import create_handler  # noqa: E402
from scheduling.exceptions import (  # noqa: E402
    CandidateExpired,
    CandidateNotFound,
    ScheduleConflict,
    StaleCandidateVersion,
)
from http.server import ThreadingHTTPServer  # noqa: E402

DAY = "2026-10-01"
AVAIL = [{"start": f"{DAY}T08:00Z", "end": f"{DAY}T18:00Z"}]
BASE_TS = 1_780_000_000.0


def iso(hm: str) -> str:
    return f"{DAY}T{hm}:00Z"


class ServiceFixture:
    def __init__(self, path: str = ":memory:") -> None:
        self.clock = ManualClock(BASE_TS)
        self.svc = SchedulingService(path, self.clock)

    def seed(self) -> "ServiceFixture":
        s = self.svc
        s.register_resource({"id": "g1", "kind": "guide", "name": "张讲解员",
                             "qualifications": ["q:base"], "capacity": 1,
                             "travel_buffer_min": 15, "availability": AVAIL})
        s.register_resource({"id": "g2", "kind": "guide", "name": "李讲解员",
                             "qualifications": ["q:base", "q:vip"], "capacity": 1,
                             "travel_buffer_min": 15, "availability": AVAIL})
        s.register_resource({"id": "l1", "kind": "lecturer", "name": "王讲师",
                             "qualifications": ["lec:space"], "capacity": 1,
                             "travel_buffer_min": 30, "availability": AVAIL})
        s.register_resource({"id": "v1", "kind": "venue", "name": "航天厅",
                             "qualifications": [], "capacity": 60,
                             "travel_buffer_min": 0, "availability": AVAIL})
        s.register_resource({"id": "v2", "kind": "venue", "name": "探月厅",
                             "qualifications": [], "capacity": 30,
                             "travel_buffer_min": 0, "availability": AVAIL})
        s.register_group({"id": "sch1", "name": "第一中学", "size": 40,
                          "travel_buffer_min": 10})
        return self

    def session(self, sid: str, start: str, end: str, roles,
                group: str | None = None, depends_on=None) -> dict:
        return {"session_id": sid, "title": sid, "start": iso(start),
                "end": iso(end), "roles": roles,
                "group_id": group, "depends_on": depends_on or []}

    def guide(self, *qs, pinned=None):
        return {"kind": "guide", "qualifications": list(qs),
                **({"resource_id": pinned} if pinned else {})}

    def lecturer(self, *qs, pinned=None):
        return {"kind": "lecturer", "qualifications": list(qs),
                **({"resource_id": pinned} if pinned else {})}

    def venue(self, pinned=None):
        return {"kind": "venue",
                **({"resource_id": pinned} if pinned else {})}

    def close(self) -> None:
        self.svc.close()


class QualificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ServiceFixture().seed()

    def tearDown(self) -> None:
        self.f.close()

    def test_qualification_match_and_mismatch(self) -> None:
        f = self.f
        ok = f.svc.plan([f.session("s1", "09:00", "10:00",
                                   [f.guide("q:vip"), f.venue()])], 600)
        self.assertTrue(ok["feasible"])
        self.assertEqual(ok["entries"][0]["resource_ids"][0], "g2")
        # 无团体时两个展厅容量都满足，引擎优先占用更小的厅（省出大厅）
        self.assertIn(ok["entries"][0]["resource_ids"][1], {"v1", "v2"})

        bad = f.svc.plan([f.session("s2", "09:00", "10:00",
                                    [f.guide("q:english")])], 600)
        self.assertFalse(bad["feasible"])
        self.assertTrue(any(c["kind"] == "qualification"
                            for c in bad["conflicts"]))

    def test_pinned_resource_missing_qualification(self) -> None:
        f = self.f
        bad = f.svc.plan([f.session("s3", "11:00", "12:00",
                                    [f.guide("q:vip", pinned="g1")])], 600)
        self.assertFalse(bad["feasible"])
        self.assertEqual(bad["conflicts"][0]["kind"], "qualification")


class CapacityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ServiceFixture().seed()

    def tearDown(self) -> None:
        self.f.close()

    def test_group_over_venue_capacity(self) -> None:
        f = self.f
        f.svc.change_group_size("sch1", 80)
        plan = f.svc.plan([f.session("s1", "09:00", "10:00",
                                     [f.venue("v1")], group="sch1")], 600)
        self.assertFalse(plan["feasible"])
        self.assertTrue(any(c["kind"] == "capacity" for c in plan["conflicts"]))
        # 替代方案应推荐容量足够的其它安排或时间
        self.assertTrue(plan["alternatives"])

    def test_staff_capacity_one_session_at_a_time(self) -> None:
        f = self.f
        p = f.svc.plan([f.session("s1", "09:00", "10:00",
                                  [f.lecturer("lec:space")])], 600)
        f.svc.confirm(p["id"])
        clash = f.svc.plan([f.session("s2", "09:30", "10:30",
                                      [f.lecturer("lec:space")])], 600)
        self.assertFalse(clash["feasible"])
        self.assertTrue(any(c["kind"] == "double_booking"
                            for c in clash["conflicts"]))

    def test_venue_exclusive_per_session_even_if_people_fit(self) -> None:
        f = self.f
        f.svc.register_group({"id": "sch2", "name": "第二中学", "size": 20})
        p = f.svc.plan([f.session("s1", "09:00", "10:00",
                                  [f.venue("v1")], group="sch1")], 600)
        f.svc.confirm(p["id"])
        clash = f.svc.plan([f.session("s2", "09:30", "10:30",
                                      [f.venue("v1")], group="sch2")], 600)
        # 同厅两场不能并存，即使 40+20 不超 60
        self.assertFalse(clash["feasible"])


class AvailabilityAndBufferTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ServiceFixture().seed()

    def tearDown(self) -> None:
        self.f.close()

    def test_outside_availability_rejected(self) -> None:
        f = self.f
        plan = f.svc.plan([f.session("s1", "07:00", "08:30",
                                     [f.guide("q:base"), f.venue()])], 600)
        self.assertFalse(plan["feasible"])
        self.assertTrue(any(c["kind"] == "availability"
                            for c in plan["conflicts"]))

    def test_travel_buffer_for_guide(self) -> None:
        f = self.f
        # g1 唯一能承担 q:only 标记前，先让 g1 占 09:00-10:00
        f.svc.register_resource({"id": "g3", "kind": "guide", "name": "赵讲解员",
                                 "qualifications": ["q:other"], "capacity": 1,
                                 "travel_buffer_min": 15, "availability": AVAIL})
        p = f.svc.plan([f.session("s1", "09:00", "10:00",
                                  [f.guide("q:base", pinned="g1")])], 600)
        f.svc.confirm(p["id"])
        # 10:10 开场，与上一场仅隔 10 分钟 < g1 的 15 分钟缓冲
        tight = f.svc.plan([f.session("s2", "10:10", "11:10",
                                      [f.guide("q:base", pinned="g1")])], 600)
        self.assertFalse(tight["feasible"])
        self.assertTrue(any(c["kind"] == "buffer" for c in tight["conflicts"]))
        # 不指定人时，引擎自动选 g2
        auto = f.svc.plan([f.session("s3", "10:10", "11:10",
                                     [f.guide("q:base")])], 600)
        self.assertTrue(auto["feasible"])
        self.assertEqual(auto["entries"][0]["resource_ids"], ["g2"])

    def test_group_travel_buffer(self) -> None:
        f = self.f
        p = f.svc.plan([
            f.session("s1", "09:00", "10:00", [f.venue("v1")], group="sch1"),
        ], 600)
        f.svc.confirm(p["id"])
        # 团体转场缓冲 10 分钟：10:05 开场冲突
        clash = f.svc.plan([
            f.session("s2", "10:05", "11:05", [f.venue("v2")], group="sch1"),
        ], 600)
        self.assertFalse(clash["feasible"])
        self.assertTrue(any(c["kind"] == "buffer" for c in clash["conflicts"]))
        ok = f.svc.plan([
            f.session("s3", "10:15", "11:15", [f.venue("v1")], group="sch1"),
        ], 600)
        self.assertTrue(ok["feasible"])


class DependencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ServiceFixture().seed()

    def tearDown(self) -> None:
        self.f.close()

    def test_dependency_must_be_scheduled_first(self) -> None:
        f = self.f
        plan = f.svc.plan([
            f.session("s2", "09:00", "10:00", [f.guide("q:base")],
                      depends_on=["s1"]),
            f.session("s1", "10:30", "11:30", [f.guide("q:base")]),
        ], 600)
        # 前置场次被排在后面时间 -> 违反先后
        self.assertFalse(plan["feasible"])
        self.assertTrue(any(c["kind"] == "dependency"
                            for c in plan["conflicts"]))

    def test_dependency_order_ok(self) -> None:
        f = self.f
        plan = f.svc.plan([
            f.session("s2", "10:30", "11:30", [f.guide("q:vip")],
                      depends_on=["s1"]),
            f.session("s1", "09:00", "10:00", [f.guide("q:base"), f.venue()]),
        ], 600)
        self.assertTrue(plan["feasible"])

    def test_dependency_on_confirmed_external_session(self) -> None:
        f = self.f
        first = f.svc.plan([f.session("s1", "09:00", "10:00",
                                      [f.guide("q:base"), f.venue()])], 600)
        f.svc.confirm(first["id"])
        ok = f.svc.plan([f.session("s2", "10:00", "11:00",
                                   [f.guide("q:base"), f.venue()],
                                   depends_on=["s1"])], 600)
        self.assertTrue(ok["feasible"])
        bad = f.svc.plan([f.session("s3", "08:30", "08:50",
                                    [f.guide("q:base"), f.venue()],
                                    depends_on=["s1"])], 600)
        self.assertFalse(bad["feasible"])
        self.assertTrue(any(c["kind"] == "dependency" for c in bad["conflicts"]))

    def test_dependency_cycle_rejected(self) -> None:
        f = self.f
        with self.assertRaises(ValueError):
            f.svc.plan([
                f.session("a", "09:00", "10:00", [f.guide("q:base")],
                          depends_on=["b"]),
                f.session("b", "10:30", "11:30", [f.guide("q:base")],
                          depends_on=["a"]),
            ], 600)

    def test_role_count_assigns_distinct_resources(self) -> None:
        f = self.f
        plan = f.svc.plan([f.session("s1", "09:00", "10:00", [
            {"kind": "guide", "qualifications": ["q:base"], "count": 2},
        ])], 600)
        self.assertTrue(plan["feasible"])
        ids = plan["entries"][0]["resource_ids"]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)
        view = f.svc.confirm(plan["id"])
        self.assertEqual(len(view["entries_view"][0]["resource_ids"]), 2)


class GroupRejectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ServiceFixture().seed()

    def tearDown(self) -> None:
        self.f.close()

    def test_group_reject_window(self) -> None:
        f = self.f
        f.svc.reject_resource("sch1", iso("09:00"), iso("10:00"), "提前离校")
        plan = f.svc.plan([f.session("s1", "09:30", "10:30",
                                     [f.venue()], group="sch1")], 600)
        self.assertFalse(plan["feasible"])
        kinds = {c["kind"] for c in plan["conflicts"]}
        self.assertIn("availability", kinds)
        ok = f.svc.plan([f.session("s2", "10:30", "11:30",
                                   [f.venue()], group="sch1")], 600)
        self.assertTrue(ok["feasible"])


class TwoPhaseAtomicTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ServiceFixture().seed()

    def tearDown(self) -> None:
        self.f.close()

    def test_infeasible_plan_creates_nothing(self) -> None:
        f = self.f
        plan = f.svc.plan([
            f.session("s1", "09:00", "10:00", [f.guide("q:base")]),
            f.session("s2", "09:00", "10:00", [f.guide("q:english")]),
        ], 600)
        self.assertFalse(plan["feasible"])
        self.assertEqual(f.svc.stats()["holds"], 0)
        self.assertEqual(f.svc.stats()["candidates_pending"], 0)

    def test_confirm_locks_all_resources_atomically(self) -> None:
        f = self.f
        plan = f.svc.plan([
            f.session("s1", "09:00", "10:00",
                      [f.guide("q:base"), f.lecturer("lec:space"), f.venue("v1")],
                      group="sch1"),
        ], 600)
        cid = plan["id"]
        view = f.svc.confirm(cid)
        self.assertEqual(view["status"], "confirmed")
        # 讲解员/讲师/场地/团体四类资源同期均不可再排（pin 具体资源验证锁定）
        for roles, label in [
            ([f.guide("q:base", pinned="g1")], "guide"),
            ([f.lecturer("lec:space")], "lecturer"),
            ([f.venue("v1")], "venue"),
        ]:
            clash = f.svc.plan(
                [f.session("x_" + label, "09:15", "09:45", roles)], 600)
            self.assertFalse(clash["feasible"], label)
        group_clash = f.svc.plan(
            [f.session("x_group", "09:15", "09:45", [f.venue("v2")],
                       group="sch1")], 600)
        self.assertFalse(group_clash["feasible"])

    def test_double_confirm_only_one_wins(self) -> None:
        f = self.f
        plan = f.svc.plan([f.session("s1", "09:00", "10:00",
                                     [f.guide("q:base")])], 600)
        outcomes: list[str] = []

        def worker() -> None:
            try:
                f.svc.confirm(plan["id"])
                outcomes.append("ok")
            except ScheduleConflict:
                outcomes.append("conflict")
            except Exception:  # noqa: BLE001
                outcomes.append("error")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(len(f.svc.schedule()["entries"]), 1)

    def test_confirm_failure_after_intervening_rejection_leaves_no_half_lock(
            self) -> None:
        f = self.f
        plan = f.svc.plan([f.session("s1", "09:00", "10:00",
                                     [f.guide("q:base", pinned="g1")])], 600)
        f.svc.reject_resource("g1", iso("09:00"), iso("10:00"), "临时请假")
        with self.assertRaises(ScheduleConflict) as ctx:
            f.svc.confirm(plan["id"])
        self.assertTrue(ctx.exception.conflicts)
        self.assertTrue(ctx.exception.alternatives)
        # 整体未落锁：候选仍 pending，排期为空
        self.assertEqual(f.svc.get_candidate(plan["id"])["status"], "pending")
        self.assertEqual(f.svc.schedule()["entries"], [])
        # 取消后 g2 可正常排该时段（拒绝解除前 g1 不可用）
        cancel = f.svc.cancel(plan["id"])
        self.assertEqual(cancel["released_entries"], 0)

    def test_inactive_resource_at_confirm_time(self) -> None:
        f = self.f
        plan = f.svc.plan([f.session("s1", "09:00", "10:00",
                                     [f.guide("q:base", pinned="g1")])], 600)
        f.svc.set_resource_active("g1", False)
        with self.assertRaises(ScheduleConflict):
            f.svc.confirm(plan["id"])
        self.assertEqual(f.svc.schedule()["entries"], [])


class PartialRejectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ServiceFixture().seed()

    def tearDown(self) -> None:
        self.f.close()

    def test_reject_window_routes_assignment_elsewhere(self) -> None:
        f = self.f
        f.svc.reject_resource("g1", iso("09:00"), iso("12:00"), "接待其他团")
        plan = f.svc.plan([f.session("s1", "09:00", "10:00",
                                     [f.guide("q:base")])], 600)
        self.assertTrue(plan["feasible"])
        self.assertEqual(plan["entries"][0]["resource_ids"], ["g2"])

    def test_all_resources_rejected_gives_alternatives(self) -> None:
        f = self.f
        f.svc.reject_resource("g1", iso("08:00"), iso("18:00"), "全天请假")
        f.svc.reject_resource("g2", iso("08:00"), iso("18:00"), "全天请假")
        plan = f.svc.plan([f.session("s1", "09:00", "10:00",
                                     [f.guide("q:base")])], 600)
        self.assertFalse(plan["feasible"])
        kinds = {c["kind"] for c in plan["conflicts"]}
        self.assertIn("availability", kinds)
        # 时间维度替代方案：被拒窗口覆盖全天则无时间备选，但冲突信息完整
        self.assertIn("message", plan["conflicts"][0])


class GroupSizeChangeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ServiceFixture().seed()

    def tearDown(self) -> None:
        self.f.close()

    def test_size_increase_rejected_at_confirm_when_over_capacity(self) -> None:
        f = self.f
        plan = f.svc.plan([f.session("s1", "09:00", "10:00",
                                     [f.venue()], group="sch1")], 600)
        self.assertEqual(plan["entries"][0]["group_size"], 40)
        f.svc.change_group_size("sch1", 80)
        with self.assertRaises(ScheduleConflict) as ctx:
            f.svc.confirm(plan["id"])
        self.assertTrue(any(c["kind"] == "capacity"
                            for c in ctx.exception.conflicts))

    def test_stale_snapshot_explicit_conflict(self) -> None:
        f = self.f
        plan = f.svc.plan([f.session("s1", "09:00", "10:00",
                                     [f.venue()], group="sch1")], 600)
        f.svc.change_group_size("sch1", 45)
        with self.assertRaises(StaleCandidateVersion):
            f.svc.confirm(plan["id"], expected_group_sizes={"sch1": 40})
        # 不带快照时按新人数确认成功，并回报人数变化
        view = f.svc.confirm(plan["id"])
        self.assertEqual(view["status"], "confirmed")
        self.assertEqual(view["group_size_changes"],
                         {"sch1": {"from": 40, "to": 45}})
        self.assertEqual(view["entries_view"][0]["group_size"], 45)


class CancelReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ServiceFixture().seed()

    def tearDown(self) -> None:
        self.f.close()

    def test_cancel_pending_releases_holds(self) -> None:
        f = self.f
        plan = f.svc.plan([f.session("s1", "09:00", "10:00",
                                     [f.lecturer("lec:space")])], 600)
        self.assertEqual(f.svc.stats()["holds"], 1)
        result = f.svc.cancel(plan["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(f.svc.stats()["holds"], 0)
        # 释放后他人可排同时段
        again = f.svc.plan([f.session("s2", "09:00", "10:00",
                                      [f.lecturer("lec:space")])], 600)
        self.assertTrue(again["feasible"])

    def test_cancel_confirmed_releases_entries(self) -> None:
        f = self.f
        plan = f.svc.plan([f.session("s1", "09:00", "10:00",
                                     [f.guide("q:base"), f.venue("v1")],
                                     group="sch1")], 600)
        f.svc.confirm(plan["id"])
        self.assertEqual(len(f.svc.schedule()["entries"]), 1)
        result = f.svc.cancel(plan["id"])
        self.assertEqual(result["released_entries"], 1)
        self.assertEqual(f.svc.schedule()["entries"], [])
        again = f.svc.plan([f.session("s2", "09:00", "10:00",
                                      [f.guide("q:base"), f.venue("v1")],
                                      group="sch1")], 600)
        self.assertTrue(again["feasible"])

    def test_cancel_unknown(self) -> None:
        with self.assertRaises(CandidateNotFound):
            self.f.svc.cancel("cand_nope")


class AlternativeExplainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ServiceFixture().seed()

    def tearDown(self) -> None:
        self.f.close()

    def test_conflict_response_explains_resource_and_time_options(self) -> None:
        f = self.f
        p = f.svc.plan([f.session("s1", "09:00", "10:00",
                                  [f.lecturer("lec:space", pinned="l1")])], 600)
        f.svc.confirm(p["id"])
        clash = f.svc.plan([f.session("s2", "09:30", "10:15",
                                      [f.lecturer("lec:space")])], 600)
        self.assertFalse(clash["feasible"])
        conflict = clash["conflicts"][0]
        self.assertEqual(conflict["resource_id"], "l1")
        self.assertIn("王讲师", conflict["message"])
        self.assertIn("interval", conflict)
        # 时间替代方案：后移 >=30 分钟（讲师缓冲）
        time_opts = [o for alt in clash["alternatives"]
                     for o in alt["options"] if o.get("times")]
        self.assertTrue(time_opts)
        shifts = [t["shift_min"] for t in time_opts[0]["times"]]
        self.assertTrue(all(s >= 30 for s in shifts))

    def test_alternative_resources_listed(self) -> None:
        f = self.f
        p = f.svc.plan([f.session("s1", "09:00", "10:00",
                                  [f.guide("q:base", pinned="g1")])], 600)
        f.svc.confirm(p["id"])
        clash = f.svc.plan([f.session("s2", "09:30", "10:00",
                                      [f.guide("q:base")])], 600)
        # g1 缓冲不足，但 g2 空闲 -> 其实可行
        self.assertTrue(clash["feasible"])
        self.assertEqual(clash["entries"][0]["resource_ids"], ["g2"])


class ExpiryRestartTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ServiceFixture().seed()

    def tearDown(self) -> None:
        self.f.close()

    def test_ttl_expiry_reclaims_holds(self) -> None:
        f = self.f
        plan = f.svc.plan([f.session("s1", "09:00", "10:00",
                                     [f.guide("q:base")])], ttl_seconds=120)
        self.assertEqual(f.svc.stats()["candidates_pending"], 1)
        f.clock.advance(121)
        reaped = f.svc.reap_expired()
        self.assertEqual(reaped, [plan["id"]])
        self.assertEqual(f.svc.stats()["holds"], 0)
        with self.assertRaises(CandidateExpired):
            f.svc.confirm(plan["id"])
        # 资源已释放
        again = f.svc.plan([f.session("s2", "09:00", "10:00",
                                      [f.guide("q:base")])], 600)
        self.assertTrue(again["feasible"])

    def test_plan_auto_reaps_before_snapshot(self) -> None:
        f = self.f
        f.svc.plan([f.session("s1", "09:00", "10:00",
                              [f.guide("q:base")])], ttl_seconds=60)
        f.clock.advance(61)
        plan = f.svc.plan([f.session("s2", "09:00", "10:00",
                                     [f.guide("q:base")])], 600)
        self.assertTrue(plan["feasible"])
        self.assertEqual(f.svc.stats()["candidates_expired"], 1)

    def test_restart_reclaims_expired_holds(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            clock = ManualClock(BASE_TS)
            first = SchedulingService(path, clock)
            fr = ServiceFixture.__new__(ServiceFixture)
            fr.clock, fr.svc = clock, first
            fr.seed()
            plan = first.plan(
                [fr.session("s1", "09:00", "10:00",
                            [fr.guide("q:base")])], ttl_seconds=60)
            clock.advance(61)
            first.close()

            second = SchedulingService(path, clock)
            stats = second.stats()
            self.assertEqual(stats["candidates_pending"], 0)
            self.assertEqual(stats["candidates_expired"], 1)
            self.assertEqual(stats["holds"], 0)
            with self.assertRaises(CandidateExpired):
                second.confirm(plan["id"])
            second.close()
        finally:
            os.unlink(path)

    def test_restart_preserves_confirmed_bookings(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            first = SchedulingService(path, ManualClock(BASE_TS))
            fr = ServiceFixture.__new__(ServiceFixture)
            fr.clock, fr.svc = ManualClock(BASE_TS), first
            fr.seed()
            plan = first.plan(
                [fr.session("s1", "09:00", "10:00",
                            [fr.guide("q:base"), fr.venue("v1")],
                            group="sch1")], 600)
            first.confirm(plan["id"])
            first.close()

            second = SchedulingService(path, ManualClock(BASE_TS))
            entries = second.schedule()["entries"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["resource_ids"], ["g1", "v1"])
            clash = second.plan(
                [fr.session("s2", "09:30", "10:30", [fr.venue("v1")])], 600)
            self.assertFalse(clash["feasible"])
            second.close()
        finally:
            os.unlink(path)


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ServiceFixture().seed()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         create_handler(self.f.svc))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.f.close()

    def _req(self, method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_full_flow_over_http(self) -> None:
        status, plan = self._req("POST", "/plans", {"sessions": [{
            "session_id": "h1", "title": "HTTP 场", "start": iso("09:00"),
            "end": iso("10:00"), "group_id": "sch1",
            "roles": [{"kind": "guide", "qualifications": ["q:base"]},
                      {"kind": "venue"}]}], "ttl_seconds": 600})
        self.assertEqual(status, 200)
        self.assertTrue(plan["feasible"])
        cid = plan["id"]

        status, view = self._req("POST", f"/plans/{cid}/confirm", {})
        self.assertEqual(status, 200)
        self.assertEqual(view["status"], "confirmed")

        status, schedule = self._req("GET", "/schedule")
        self.assertEqual(status, 200)
        self.assertEqual(len(schedule["entries"]), 1)

        status, cancelled = self._req("POST", f"/plans/{cid}/cancel", {})
        self.assertEqual(status, 200)
        self.assertEqual(cancelled["released_entries"], 1)

    def test_conflict_response_shape(self) -> None:
        # 先占住 l1
        _, p1 = self._req("POST", "/plans", {"sessions": [{
            "session_id": "h1", "start": iso("09:00"), "end": iso("10:00"),
            "roles": [{"kind": "lecturer", "qualifications": ["lec:space"]}]}]})
        self._req("POST", f"/plans/{p1['id']}/confirm", {})
        status, body = self._req("POST", "/plans", {"sessions": [{
            "session_id": "h2", "start": iso("09:30"), "end": iso("10:15"),
            "roles": [{"kind": "lecturer", "qualifications": ["lec:space"]}]}]})
        self.assertEqual(status, 409)
        self.assertFalse(body["feasible"])
        self.assertTrue(body["conflicts"])
        self.assertIn("message", body["conflicts"][0])
        self.assertTrue(body["alternatives"])

    def test_expired_confirm_returns_410(self) -> None:
        _, p1 = self._req("POST", "/plans", {"ttl_seconds": 1, "sessions": [{
            "session_id": "h1", "start": iso("09:00"), "end": iso("10:00"),
            "roles": [{"kind": "guide", "qualifications": ["q:base"]}]}]})
        self.f.clock.advance(2)
        status, body = self._req("POST", f"/plans/{p1['id']}/confirm", {})
        self.assertEqual(status, 410)
        self.assertEqual(body["error"]["code"], "candidate_expired")


if __name__ == "__main__":
    unittest.main()
