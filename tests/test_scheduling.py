"""排班领域服务测试：约束、原子确认、并发、人数变化、过期回收与替代方案。"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scheduling import (  # noqa: E402
    CONFIRMED, EXPIRED, FakeClock, Group, HELD,
    SchedulingError, SchedulingService,
    Staff, Storage, Venue, Window,
)
from scheduling.service import ConfirmFailure  # noqa: E402


def W(start: int, end: int) -> Window:
    return Window(start, end)


def make_service(clock=None, path: str = ":memory:", default_travel: int = 30):
    storage = Storage(path)
    clock = clock or FakeClock(10000)
    svc = SchedulingService(storage, clock, default_ttl=10)
    svc.catalog.default_travel_minutes = default_travel
    day = (W(0, 10000),)
    svc.upsert_staff(Staff("g1", "讲解员甲", "guide", ("q1", "q2"), day,
                           {"V1>V2": 20, "V1>V3": 40, "V2>V3": 20}))
    svc.upsert_staff(Staff("g2", "讲解员乙", "guide", ("q1",), day,
                           {"V1>V2": 20, "V1>V3": 40, "V2>V3": 20}))
    svc.upsert_staff(Staff("l1", "讲师丙", "lecturer", ("q2",), day, {}))
    svc.upsert_venue(Venue("V1", "主厅", 50, day))
    svc.upsert_venue(Venue("V2", "副厅", 100, day))
    svc.upsert_venue(Venue("V3", "小厅", 30, day))
    svc.upsert_group(Group("G1", "晨光中学", 40))
    svc.upsert_group(Group("G2", "星火小学", 40))
    return svc, storage, clock


def sess(rid="s1", role="guide", qual="q1", duration=60,
         earliest=100, latest=None, venue=None, after=()):
    return {"req_id": rid, "role": role, "qualification": qual,
            "duration": duration, "earliest_start": earliest,
            "latest_start": latest, "venue_id": venue,
            "after": [{"after_req_id": a, "gap_minutes": gap}
                      for a, gap in after]}


def assignments_of(candidate):
    return [{"req_id": a.req_id, "staff_id": a.staff_id,
             "venue_id": a.venue_id, "start": a.start}
            for a in candidate.assignments]


class CandidateGenerationTest(unittest.TestCase):
    def setUp(self):
        self.svc, _, _ = make_service()

    def test_eligibility_capacity_and_window(self):
        diag, ttl = self.svc.plan(
            "G1", [sess(earliest=100, latest=200)])
        self.assertEqual(ttl, 10)
        self.assertTrue(diag.candidates)
        top = diag.candidates[0]
        # 最优解贴最早开始时间
        self.assertEqual(top.assignments[0].start, 100)
        # 小厅容量 30 < 40 人，任何候选都不得使用
        for c in diag.candidates:
            self.assertTrue(all(a.venue_id != "V3" for a in c.assignments))
        # 角色/资格不匹配：需要讲师资质 q3 不存在
        diag2, _ = self.svc.plan("G1", [sess(role="lecturer", qual="q3")])
        self.assertFalse(diag2.candidates)
        self.assertEqual(diag2.blockers[0].resource_type, "qualification")

    def test_travel_buffer_between_venues(self):
        # 先确认一场：g1 在 V1 [100,160)
        diag, _ = self.svc.plan("G1", [sess(earliest=100, latest=100, venue="V1")])
        r = self.svc.hold("G1", [sess(earliest=100, latest=100, venue="V1")],
                          assignments_of(diag.candidates[0]))
        self.assertEqual(self.svc.confirm(r.id).status, CONFIRMED)

        # g1 是唯一具备 q2 资质的讲解员；其在 V2 的场与前一场跨厅：
        # 缓冲 20 分钟，180 之前不可行
        diag2, _ = self.svc.plan(
            "G2", [sess(earliest=100, latest=300, venue="V2", qual="q2")])
        g1_starts = sorted({a.start for c in diag2.candidates
                            for a in c.assignments if a.staff_id == "g1"})
        self.assertEqual(g1_starts[0], 180)  # 160 + 20
        # 同厅连续无需缓冲：V1 160 即可
        diag3, _ = self.svc.plan(
            "G2", [sess(earliest=100, latest=300, venue="V1", qual="q2")])
        starts = sorted({a.start for c in diag3.candidates for a in c.assignments})
        self.assertIn(160, starts)

    def test_session_dependency_with_gap(self):
        sessions = [
            sess("s1", role="lecturer", qual="q2", earliest=100, latest=100),
            sess("s2", role="guide", qual="q1", earliest=100, latest=400,
                 after=[("s1", 15)]),
        ]
        diag, _ = self.svc.plan("G1", sessions)
        self.assertTrue(diag.candidates)
        for c in diag.candidates:
            s1 = next(a for a in c.assignments if a.req_id == "s1")
            s2 = next(a for a in c.assignments if a.req_id == "s2")
            self.assertGreaterEqual(s2.start, s1.end + 15)

    def test_dependency_cycle_rejected(self):
        sessions = [
            sess("s1", after=[("s2", 0)]),
            sess("s2", after=[("s1", 0)]),
        ]
        with self.assertRaises(SchedulingError):
            self.svc.plan("G1", sessions)


class HoldConfirmTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.storage, self.clock = make_service()

    def test_hold_then_atomic_confirm(self):
        sessions = [sess(earliest=100, latest=120)]
        diag, _ = self.svc.plan("G1", sessions)
        r = self.svc.hold("G1", sessions, assignments_of(diag.candidates[0]))
        self.assertEqual(r.status, HELD)
        self.assertEqual(r.expires_at, 10010)
        got = self.svc.confirm(r.id)
        self.assertEqual(got.status, CONFIRMED)
        self.assertIsNone(got.expires_at)
        self.assertEqual(got.version, 2)
        # 幂等确认
        self.assertEqual(self.svc.confirm(r.id).status, CONFIRMED)

    def test_expected_version_optimistic_lock(self):
        # 小团体先暂占小厅，随后人数变化使暂占失效（版本号前进）
        self.svc.change_headcount("G1", 25)
        sessions = [sess(earliest=100, latest=120, venue="V3")]
        diag, _ = self.svc.plan("G1", sessions)
        r = self.svc.hold("G1", sessions, assignments_of(diag.candidates[0]))
        self.svc.change_headcount("G1", 50)
        with self.assertRaises(SchedulingError) as ctx:
            self.svc.confirm(r.id, expected_version=1)
        self.assertEqual(ctx.exception.code, "version_conflict")

    def test_stale_candidate_rejected(self):
        sessions = [sess(earliest=100, latest=120)]
        with self.assertRaises(SchedulingError) as ctx:
            self.svc.hold("G1", sessions,
                          [{"req_id": "s1", "staff_id": "nope",
                            "venue_id": "V1", "start": 100}])
        self.assertEqual(ctx.exception.code, "stale_candidate")

    def test_concurrent_confirm_only_one_wins(self):
        sessions_a = [sess(earliest=100, latest=100, venue="V1")]
        # 第二单用 q2 资质（只有 g1 具备）并放宽窗口：与 A 抢同一讲解员，
        # 失败后仍可改到 160 以后
        sessions_b = [sess(earliest=100, latest=300, qual="q2")]
        diag_a, _ = self.svc.plan("G1", sessions_a)
        pick_a = assignments_of(diag_a.candidates[0])
        r1 = self.svc.hold("G1", sessions_a, pick_a)
        # B 在软竞争提示下仍选择与 A 完全相同的资源（模拟同时抢同一讲解员/展厅）
        r2 = self.svc.hold("G2", sessions_b,
                           [{"req_id": "s1", "staff_id": "g1",
                             "venue_id": "V1", "start": 100}])

        results = []

        def confirm(rid):
            results.append(self.svc.confirm(rid))

        t1 = threading.Thread(target=confirm, args=(r1.id,))
        t2 = threading.Thread(target=confirm, args=(r2.id,))
        t1.start(); t2.start(); t1.join(); t2.join()

        confirmed = [x for x in results if not isinstance(x, ConfirmFailure)]
        refused = [x for x in results if isinstance(x, ConfirmFailure)]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(len(refused), 1)

        failure = refused[0]
        self.assertEqual(failure.code, "superseded")
        # 冲突响应给出可行替代方案（换 g2 或换厅/时段）
        self.assertTrue(failure.diagnosis.candidates)
        alt = failure.diagnosis.candidates[0].assignments[0]
        self.assertNotEqual((alt.staff_id, alt.venue_id, alt.start),
                            ("g1", "V1", 100))
        # 被拒暂占已整体释放
        loser = r2.id if confirmed[0].id == r1.id else r1.id
        loser_obj = self.svc.get(loser)
        self.assertEqual(loser_obj.status, "superseded")
        self.assertEqual(loser_obj.release_reason, confirmed[0].id)
        # 未冲突的资源仍可被输家重新排：替代方案立即可暂占
        r3 = self.svc.hold("G2", sessions_b,
                           [{"req_id": a.req_id, "staff_id": a.staff_id,
                             "venue_id": a.venue_id, "start": a.start}
                            for a in failure.diagnosis.candidates[0].assignments])
        self.assertEqual(r3.status, HELD)

    def test_many_concurrent_confirms_exactly_one(self):
        for i in range(12):
            self.svc.upsert_group(Group(f"GX{i}", f"学校{i}", 40))
        sessions = [sess(earliest=100, latest=100, venue="V1")]
        ids = []
        for i in range(12):
            diag, _ = self.svc.plan(f"GX{i}", sessions)
            ids.append(self.svc.hold(
                f"GX{i}", sessions,
                assignments_of(diag.candidates[0])).id)
        outcomes = []
        barrier = threading.Barrier(12)

        def worker(rid):
            barrier.wait()
            outcomes.append(self.svc.confirm(rid))

        threads = [threading.Thread(target=worker, args=(rid,)) for rid in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(not isinstance(x, ConfirmFailure) for x in outcomes), 1)

    def test_partial_refusal_due_to_staff_change(self):
        sessions = [sess(earliest=100, latest=100, venue="V1")]
        diag, _ = self.svc.plan("G1", sessions)
        r = self.svc.hold("G1", sessions, assignments_of(diag.candidates[0]))
        staff_id = diag.candidates[0].assignments[0].staff_id
        # 统筹在暂存期间调整了该讲解员的可用时段
        self.svc.upsert_staff(Staff(staff_id, "讲解员", "guide", ("q1", "q2"),
                                    (W(0, 90),), {}))
        result = self.svc.confirm(r.id)
        self.assertIsInstance(result, ConfirmFailure)
        self.assertTrue(any(x.resource_type == "staff" for x in result.refusals))
        # 未锁定任何资源：别人可以立即占用同时段
        diag2, _ = self.svc.plan("G2", sessions)
        self.assertTrue(diag2.candidates)

    def test_cancel_releases_resources(self):
        sessions = [sess(earliest=100, latest=100, venue="V1")]
        diag, _ = self.svc.plan("G1", sessions)
        r = self.svc.hold("G1", sessions, assignments_of(diag.candidates[0]))
        self.svc.confirm(r.id)
        # 已确认占用期间无候选
        diag2, _ = self.svc.plan("G2", sessions)
        self.assertFalse(diag2.candidates)
        self.svc.cancel(r.id)
        diag3, _ = self.svc.plan("G2", sessions)
        self.assertTrue(diag3.candidates)
        # 终态不能重复取消
        with self.assertRaises(SchedulingError):
            self.svc.cancel(r.id)


class ExpiryTest(unittest.TestCase):
    def test_ttl_expiry_reclaims_and_restart_reaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "sched.db")
            svc, storage, clock = make_service(path=db)
            sessions = [sess(earliest=100, latest=100, venue="V1")]
            diag, _ = svc.plan("G1", sessions)
            r = svc.hold("G1", sessions, assignments_of(diag.candidates[0]),
                         ttl_minutes=10)
            self.assertEqual(svc.reap_expired_holds(), [])
            clock.advance(9)
            self.assertEqual(svc.reap_expired_holds(), [])
            clock.advance(1)
            self.assertEqual(svc.reap_expired_holds(), [r.id])
            self.assertEqual(svc.get(r.id).status, EXPIRED)
            with self.assertRaises(SchedulingError):
                svc.confirm(r.id)
            # 资源已回收：新团体可排同时段
            diag2, _ = svc.plan("G2", sessions)
            self.assertTrue(diag2.candidates)

            # 再造一个未过期暂占，模拟宕机后重启
            r2 = svc.hold("G2", sessions,
                          assignments_of(diag2.candidates[0]), ttl_minutes=10)
            clock.advance(11)
            clock2 = FakeClock(clock.now_minutes())
            svc2 = SchedulingService(Storage(db), clock2)  # __init__ 自动回收
            self.assertEqual(svc2.get(r2.id).status, EXPIRED)
            self.assertEqual(svc2.get(r2.id).release_reason, "hold_expired")


class HeadcountChangeTest(unittest.TestCase):
    def setUp(self):
        self.svc, _, _ = make_service()
        self.sessions = [sess(earliest=100, latest=100, venue="V3")]

    def test_confirmed_session_blocks_headcount_increase(self):
        diag, _ = self.svc.plan("G1", self.sessions)  # 40 人? V3 容量 30
        # V3 仅 30 容不下 40 人，先降到 25
        self.svc.change_headcount("G1", 25)
        diag, _ = self.svc.plan("G1", self.sessions)
        r = self.svc.hold("G1", self.sessions, assignments_of(diag.candidates[0]))
        self.svc.confirm(r.id)
        with self.assertRaises(SchedulingError) as ctx:
            self.svc.change_headcount("G1", 50)
        self.assertEqual(ctx.exception.code, "confirmed_capacity_break")
        self.assertEqual(self.svc.catalog.group("G1").headcount, 25)

    def test_headcount_increase_invalidates_held(self):
        self.svc.change_headcount("G1", 25)
        diag, _ = self.svc.plan("G1", self.sessions)
        r = self.svc.hold("G1", self.sessions, assignments_of(diag.candidates[0]))
        result = self.svc.change_headcount("G1", 50)
        self.assertEqual(result["invalidated_holds"], [r.id])
        self.assertEqual(self.svc.get(r.id).status, EXPIRED)
        self.assertEqual(self.svc.get(r.id).release_reason,
                         "group_headcount_changed")
        with self.assertRaises(SchedulingError):
            self.svc.confirm(r.id)


class SuggestionTest(unittest.TestCase):
    def test_wait_for_holds_suggestion(self):
        svc, _, _ = make_service()
        # G1 暂占 g1 偏好方案：固定 V1 时最优候选用 g1
        sessions = [sess(earliest=100, latest=100, venue="V1")]
        diag, _ = svc.plan("G1", sessions)
        chosen = assignments_of(diag.candidates[0])
        svc.hold("G1", sessions, chosen)
        # 只保留 g1 一名讲解员，G2 同窗口无法排
        svc.upsert_staff(Staff("g2", "乙", "guide", ("never",), (W(0, 1),), {}))
        diag2, _ = svc.plan("G2", sessions)
        # 软竞争：候选仍可生成（确认时先到先得），但全部带 contended 标记
        self.assertTrue(diag2.candidates)
        self.assertTrue(all(c.contended for c in diag2.candidates))
        kinds = {s.kind for s in diag2.suggestions}
        self.assertIn("WAIT_FOR_HOLDS", kinds)
        wait = next(s for s in diag2.suggestions if s.kind == "WAIT_FOR_HOLDS")
        self.assertIn("过期", wait.message)

    def test_extend_window_suggestion(self):
        svc, _, _ = make_service()
        # g1 被 100-200 已确认占满 V1，g2 移除，窗口最晚 140，延后到 200 可行
        svc.upsert_staff(Staff("g2", "乙", "guide", ("never",), (W(0, 1),), {}))
        first = [sess(earliest=100, latest=100, venue="V1")]
        diag, _ = svc.plan("G1", first)
        r = svc.hold("G1", first, assignments_of(diag.candidates[0]))
        svc.confirm(r.id)
        probe = [sess(earliest=100, latest=140, duration=60, venue="V1")]
        diag2, _ = svc.plan("G2", probe)
        self.assertFalse(diag2.candidates)
        ext = next((s for s in diag2.suggestions if s.kind == "EXTEND_WINDOW"), None)
        self.assertIsNotNone(ext)
        # 已确认场次占用 V1 [100,160)，延后方案最早 160 开始
        self.assertGreaterEqual(ext.candidates[0].assignments[0].start, 160)

    def test_shrink_group_suggestion_when_no_venue_fits(self):
        svc, _, _ = make_service()
        svc.change_headcount("G1", 200)  # 超过最大展厅容量 100
        diag, _ = svc.plan("G1", [sess(earliest=100, latest=200)])
        self.assertFalse(diag.candidates)
        kinds = {s.kind for s in diag.suggestions}
        self.assertIn("SHRINK_GROUP", kinds)
        self.assertNotIn("RELAX_VENUE", kinds)

    def test_relax_venue_suggestion_when_only_fixed_venue_too_small(self):
        svc, _, _ = make_service()
        svc.change_headcount("G1", 80)  # V3 仅 30，V2(100) 可容纳
        diag, _ = svc.plan("G1", [sess(venue="V3", earliest=100, latest=200)])
        self.assertFalse(diag.candidates)
        kinds = {s.kind for s in diag.suggestions}
        self.assertNotIn("SHRINK_GROUP", kinds)
        self.assertIn("RELAX_VENUE", kinds)
        relaxed = next(s for s in diag.suggestions if s.kind == "RELAX_VENUE")
        self.assertTrue(relaxed.candidates)
        self.assertTrue(all(a.venue_id != "V3"
                            for c in relaxed.candidates
                            for a in c.assignments))


if __name__ == "__main__":
    unittest.main()
