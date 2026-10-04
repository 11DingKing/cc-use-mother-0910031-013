"""HTTP 端到端集成测试：真实 ThreadingHTTPServer + 真实 HTTP 请求。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scheduling import FakeClock, SchedulingService, Storage  # noqa: E402
from scheduling.api import build_server  # noqa: E402

DAY_START = "2026-10-10T08:00Z"


def iso_plus(minutes: int) -> str:
    from datetime import datetime, timedelta, timezone
    dt = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%MZ")


class ApiIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock(20_000_000)
        self.service = SchedulingService(
            Storage(self.tmp.name + "/t.db"), self.clock)
        self.server = build_server(self.service, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def call(self, method: str, path: str, payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def seed(self):
        day = [{"start": DAY_START, "end": iso_plus(600)}]
        self.call("POST", "/admin/staff", {
            "id": "g1", "role": "guide", "qualifications": ["q1", "q2"],
            "availability": day, "travel": {"V1>V2": 20}})
        self.call("POST", "/admin/staff", {
            "id": "g2", "role": "guide", "qualifications": ["q1"],
            "availability": day, "travel": {"V1>V2": 20}})
        self.call("POST", "/admin/venues", {
            "id": "V1", "capacity": 80, "availability": day})
        self.call("POST", "/admin/venues", {
            "id": "V2", "capacity": 40, "availability": day})
        self.call("POST", "/admin/groups", {"id": "G1", "headcount": 50})
        self.call("POST", "/admin/groups", {"id": "G2", "headcount": 50})

    def test_full_lifecycle_over_http(self):
        self.seed()
        sessions = [{
            "req_id": "s1", "role": "guide", "qualification": "q1",
            "duration": 60, "earliest_start": DAY_START,
            "latest_start": iso_plus(120), "venue_id": "V1"}]

        status, plan = self.call("POST", "/plan", {"group_id": "G1",
                                                   "sessions": sessions})
        self.assertEqual(status, 200)
        self.assertEqual(plan["ttl_minutes"], 10)
        cand = plan["candidates"][0]
        self.assertFalse(cand["contended"])

        chosen = cand["assignments"]
        status, held = self.call("POST", "/reservations", {
            "group_id": "G1", "sessions": sessions,
            "assignments": [{"req_id": a["req_id"], "staff_id": a["staff_id"],
                             "venue_id": a["venue_id"], "start": a["start"]}
                            for a in chosen]})
        self.assertEqual(status, 201)
        rid = held["id"]
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["version"], 1)

        status, confirmed = self.call("POST", f"/reservations/{rid}/confirm", {})
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["version"], 2)
        self.assertIsNone(confirmed["expires_at"])

        # 第二单同资源被已确认占用：硬阻塞 + 无该场地候选
        status, plan2 = self.call("POST", "/plan", {"group_id": "G2",
                                                    "sessions": sessions})
        self.assertEqual(status, 200)
        reasons = {b["reason"] for b in plan2["blockers"]}
        self.assertTrue(any(r.endswith("_occupied_confirmed") for r in reasons))

        # 取消后资源立即可用
        status, _ = self.call("POST", f"/reservations/{rid}/cancel",
                              {"reason": "学校取消"})
        self.assertEqual(status, 200)
        status, plan3 = self.call("POST", "/plan", {"group_id": "G2",
                                                    "sessions": sessions})
        self.assertTrue(plan3["candidates"])

    def test_concurrent_confirms_and_restart_reap(self):
        self.seed()
        sessions = [{
            "req_id": "s1", "role": "guide", "qualification": "q1",
            "duration": 60, "earliest_start": DAY_START,
            "latest_start": DAY_START, "venue_id": "V1"}]

        sessions_wide = [dict(sessions[0], latest_start=iso_plus(300))]

        def hold(group, wide):
            use_sessions = sessions_wide if wide else sessions
            _, plan = self.call("POST", "/plan",
                                {"group_id": group, "sessions": use_sessions})
            # 显式抢占 g1 在开场时段（第二次调用时该候选带 contended 标记）
            cand = next(c for c in plan["candidates"]
                        if c["assignments"][0]["staff_id"] == "g1"
                        and c["assignments"][0]["start"] == DAY_START)
            a = cand["assignments"][0]
            _, r = self.call("POST", "/reservations", {
                "group_id": group, "sessions": use_sessions,
                "assignments": [{"req_id": "s1", "staff_id": a["staff_id"],
                                 "venue_id": a["venue_id"],
                                 "start": DAY_START}]})
            return r["id"]

        r1, r2 = hold("G1", False), hold("G2", True)
        results = []

        def confirm(rid):
            results.append(self.call("POST", f"/reservations/{rid}/confirm", {}))

        t1 = threading.Thread(target=confirm, args=(r1,))
        t2 = threading.Thread(target=confirm, args=(r2,))
        t1.start(); t2.start(); t1.join(); t2.join()
        statuses = sorted(s for s, _ in results)
        self.assertEqual(statuses, [200, 409])
        failure = next(b for s, b in results if s == 409)
        self.assertEqual(failure["code"], "superseded")
        self.assertTrue(failure["diagnosis"]["candidates"])

        # 重启回收：再造一个短暂占，推时钟后重新构造服务（同一 db 文件）
        sessions_wide = [dict(sessions[0])]
        sessions_wide[0]["latest_start"] = iso_plus(300)
        _, plan = self.call("POST", "/plan",
                            {"group_id": "G2", "sessions": sessions_wide})
        a = plan["candidates"][0]["assignments"][0]
        _, r3 = self.call("POST", "/reservations", {
            "group_id": "G2", "sessions": sessions_wide,
            "assignments": [{"req_id": "s1", "staff_id": a["staff_id"],
                             "venue_id": a["venue_id"], "start": a["start"]}],
            "ttl_minutes": 5})
        self.clock.advance(6)
        SchedulingService(Storage(self.tmp.name + "/t.db"), self.clock)
        status, got = self.call("GET", f"/reservations/{r3['id']}")
        self.assertEqual(got["status"], "expired")
        self.assertEqual(got["release_reason"], "hold_expired")


if __name__ == "__main__":
    unittest.main()
