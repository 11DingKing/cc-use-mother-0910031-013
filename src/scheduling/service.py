"""排班应用服务：两阶段协议、原子确认、过期回收与并发串行化。

线程模型：进程内一把 :class:`threading.RLock` 串行所有写操作，配合 SQLite
``BEGIN IMMEDIATE`` 事务，保证「并发确认」时只有一个事务能落锁成功；
竞争失败方在同一事务内重新校验后得到结构化冲突与替代方案。
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime

from .candidates import (
    CandidateEngine,
    CandidateRequest,
    Occupancy,
    RoleReq,
    SessionReq,
    Snapshot,
)
from .clock import Clock, SystemClock
from .exceptions import (
    CandidateExpired,
    CandidateNotFound,
    GroupNotFound,
    ResourceNotFound,
    ScheduleConflict,
    StaleCandidateVersion,
)
from .models import ResourceKind, min_to_iso, to_min
from .store import SQLiteStore

DEFAULT_TTL_SECONDS = 600


class _AlreadyCommitted(Exception):
    """事务已以提交方式终结（如过期回收），外层不得再回滚。"""

    def __init__(self, error: Exception) -> None:
        self.error = error


class SchedulingService:
    def __init__(self, store: SQLiteStore | str = ":memory:",
                 clock: Clock | None = None) -> None:
        self.store = store if isinstance(store, SQLiteStore) else SQLiteStore(store)
        self.clock = clock or SystemClock()
        self._lock = threading.RLock()
        self.reap_expired()  # 重启即回收上次进程遗留的过期暂占

    # ================================================================== #
    # 资源 / 团体 / 拒绝时段
    # ================================================================== #
    def register_resource(self, data: dict) -> dict:
        """登记或更新讲解员/讲师/场地。

        入参示例::

            {"id": "g1", "kind": "guide", "name": "张讲解员",
             "qualifications": ["guide:展厅A", "language:en"],
             "capacity": 1, "travel_buffer_min": 15,
             "availability": [{"start": "2026-10-01T09:00Z",
                               "end": "2026-10-01T17:00Z"}]}
        """
        self._validate_resource(data)
        rid = data["id"]
        with self._lock:
            self.store.begin()
            try:
                self.store.upsert_resource({
                    "id": rid,
                    "kind": data["kind"],
                    "name": data.get("name", rid),
                    "qualifications": list(data.get("qualifications", [])),
                    "capacity": int(data.get("capacity", 1)),
                    "travel_buffer_min": int(data.get("travel_buffer_min", 0)),
                    "availability": [
                        {"start_min": to_min(w["start"]), "end_min": to_min(w["end"])}
                        for w in data.get("availability", [])
                    ],
                    "active": bool(data.get("active", True)),
                }, self.clock.ts())
                self.store.commit()
            except Exception:
                self.store.rollback()
                raise
        return self.get_resource(rid)

    def register_group(self, data: dict) -> dict:
        """登记学校团体；``size`` 可变，更新后旧候选确认时会按新人数重新校验。"""
        gid = data["id"]
        size = int(data["size"])
        if size < 0:
            raise ValueError("学校团体人数不能为负")
        with self._lock:
            self.store.begin()
            try:
                self.store.upsert_group({
                    "id": gid, "name": data.get("name", gid), "size": size,
                    "contact": data.get("contact", ""),
                    "active": bool(data.get("active", True)),
                    "travel_buffer_min": int(data.get("travel_buffer_min", 0)),
                })
                self.store.commit()
            except Exception:
                self.store.rollback()
                raise
        return self._group_to_dict(self.store.get_group(gid))

    def change_group_size(self, gid: str, size: int) -> dict:
        """学校人数变化：仅改人数并自增版本，既有候选确认时按新值一致性处理。"""
        if size < 0:
            raise ValueError("学校团体人数不能为负")
        with self._lock:
            if self.store.get_group(gid) is None:
                raise GroupNotFound(f"学校团体不存在：{gid}")
            self.store.begin()
            try:
                self.store.update_group_size(gid, size)
                self.store.commit()
            except Exception:
                self.store.rollback()
                raise
        return self._group_to_dict(self.store.get_group(gid))

    def reject_resource(self, resource_id: str, start: str, end: str,
                        reason: str = "") -> dict:
        """部分资源拒绝：登记某资源的拒绝时段，候选与确认都会避开。"""
        with self._lock:
            row = self.store.get_resource(resource_id)
            group = self.store.get_group(resource_id)
            if row is None and group is None:
                raise ResourceNotFound(f"资源不存在：{resource_id}")
            s, e = to_min(start), to_min(end)
            if e <= s:
                raise ValueError("拒绝时段结束必须晚于开始")
            bid = self.store.add_block(resource_id, s, e, reason, self.clock.ts())
        return {"id": bid, "resource_id": resource_id,
                "start_min": s, "end_min": e, "reason": reason}

    def set_resource_active(self, resource_id: str, active: bool) -> None:
        with self._lock:
            if self.store.get_resource(resource_id) is None:
                raise ResourceNotFound(f"资源不存在：{resource_id}")
            self.store.begin()
            try:
                self.store.set_resource_active(resource_id, active)
                self.store.commit()
            except Exception:
                self.store.rollback()
                raise

    # ================================================================== #
    # 阶段一：生成候选方案（暂占）
    # ================================================================== #
    def plan(self, sessions: list[dict], ttl_seconds: int = DEFAULT_TTL_SECONDS,
             note: str = "") -> dict:
        """排定前生成候选方案。

        可行时落库为 ``pending`` 候选并写入暂占（holds）；不可行时不落任何暂占，
        直接返回冲突与可解释替代方案。
        """
        req = self._parse_request(sessions, ttl_seconds, note)
        with self._lock:
            self.reap_expired()
            self.store.begin()
            try:
                snap = self._snapshot(exclude_candidate=None)
                # 已确认排期的场次编号不可再次规划
                dup = [s for s in req.sessions
                       if self.store.get_booking_by_session(s.session_id) is not None]
                if dup:
                    self.store.rollback()
                    return {
                        "feasible": False,
                        "conflicts": [{
                            "resource_id": "", "resource_name": "",
                            "kind": "double_booking", "session_id": s.session_id,
                            "interval": {"start_min": s.start, "end_min": s.end,
                                         "start": min_to_iso(s.start),
                                         "end": min_to_iso(s.end)},
                            "message": f"场次 {s.session_id} 已有确认排期",
                            "conflicting_session": s.session_id,
                        } for s in dup],
                        "alternatives": [],
                    }
                result = CandidateEngine(snap).plan(req)
                if not result.feasible:
                    self.store.rollback()
                    return {
                        "feasible": False,
                        "conflicts": result.conflicts,
                        "alternatives": result.alternatives,
                    }
                cid = "cand_" + uuid.uuid4().hex
                now = self.clock.ts()
                payload = self._build_payload(req, result)
                self.store.insert_candidate(cid, payload, now, now + ttl_seconds, note)
                for entry in payload["entries"]:
                    for rid in entry["resource_ids"]:
                        # 场地单场独占、人员单场一次，权重统一为 1；
                        # 场地人数容量在引擎中按团体人数单独校验
                        self.store.add_holds(cid, rid, [(
                            entry["session_id"], entry["start_min"],
                            entry["end_min"], 1)])
                    if entry["group_id"]:
                        self.store.add_holds(cid, entry["group_id"], [(
                            entry["session_id"], entry["start_min"],
                            entry["end_min"], 1)])
                self.store.commit()
                return self._candidate_view(cid)
            except Exception:
                self.store.rollback()
                raise

    # ================================================================== #
    # 阶段二：原子确认
    # ================================================================== #
    def confirm(self, candidate_id: str,
                expected_group_sizes: dict[str, int] | None = None) -> dict:
        """原子锁定候选方案的全部资源。

        - 候选过期 / 已处理 -> 明确错误；
        - 学校人数变化：默认按当前人数重新校验（仍可行则带着新人数确认）；
          若调用方给出 ``expected_group_sizes`` 快照值，不一致时抛
          :class:`StaleCandidateVersion`，由统筹员决定是否用新人数重试；
        - 任一资源在确认瞬间被他人抢走 -> 整体失败、不留半锁，返回冲突与替代方案。
        """
        expected_group_sizes = expected_group_sizes or {}
        with self._lock:
            self.reap_expired()
            size_changes: dict[str, dict] = {}
            self.store.begin()
            try:
                row = self.store.get_candidate(candidate_id)
                if row is None:
                    raise CandidateNotFound(f"候选方案不存在：{candidate_id}")
                if row["status"] == "expired":
                    raise CandidateExpired(f"候选方案已过期：{candidate_id}")
                if row["status"] != "pending":
                    raise ScheduleConflict(
                        f"候选方案当前状态为 {row['status']}，无法确认",
                        conflicts=[{"kind": "state", "message":
                                    f"候选 {candidate_id} 已 {row['status']}"}],
                        alternatives=[])
                if row["expires_ts"] <= self.clock.ts():
                    self.store.set_candidate_status(candidate_id, "expired")
                    self.store.release_holds(candidate_id)
                    self.store.commit()
                    raise _AlreadyCommitted(CandidateExpired(
                        f"候选方案已过期：{candidate_id}"))

                payload = json.loads(row["payload"])

                # 人数变化检测（调用方持有旧快照时的乐观并发）
                for entry in payload["entries"]:
                    gid = entry.get("group_id")
                    if not gid:
                        continue
                    grow = self.store.get_group(gid)
                    if grow is None:
                        raise ScheduleConflict(
                            f"学校团体 {gid} 已不存在",
                            conflicts=[{"kind": "inactive", "session_id": entry["session_id"],
                                        "message": f"学校团体 {gid} 已被删除"}],
                            alternatives=[])
                    current = int(grow["size"])
                    if gid in expected_group_sizes and expected_group_sizes[gid] != current:
                        raise StaleCandidateVersion(
                            f"学校团体 {gid} 人数已变化："
                            f"{expected_group_sizes[gid]} -> {current}",
                            group_id=gid,
                            expected_size=expected_group_sizes[gid],
                            current_size=current)
                    if current != int(entry["group_size"]):
                        size_changes[gid] = {"from": entry["group_size"], "to": current}
                    entry["group_size"] = current

                # 固定分配，重新做全量约束校验（排除自身暂占）
                req = self._request_from_payload(payload)
                snap = self._snapshot(exclude_candidate=candidate_id)
                result = CandidateEngine(snap).plan(req)
                if not result.feasible:
                    raise ScheduleConflict(
                        "确认时资源已被占用或约束变化，整体未锁定",
                        conflicts=result.conflicts,
                        alternatives=result.alternatives)

                # 原子落锁：暂占转正
                ts = self.clock.ts()
                assign_by_session = {a.session_id: a.resource_ids
                                     for a in result.assignments}
                for entry in payload["entries"]:
                    if self.store.get_booking_by_session(entry["session_id"]) is not None:
                        raise ScheduleConflict(
                            f"场次 {entry['session_id']} 已有确认排期",
                            conflicts=[{"kind": "double_booking",
                                        "session_id": entry["session_id"],
                                        "message": "场次重复确认"}],
                            alternatives=[])
                    rids = assign_by_session[entry["session_id"]]
                    eid = self.store.insert_entry(
                        candidate_id, entry["session_id"], entry["title"],
                        entry["start_min"], entry["end_min"],
                        entry.get("group_id"), int(entry["group_size"]),
                        entry.get("venue_id"), ts)
                    for rid in rids:
                        r = snap.resources.get(rid)
                        self.store.add_entry_resource(
                            eid, rid, r["kind"] if r else "unknown", 1)
                self.store.release_holds(candidate_id)
                self.store.set_candidate_status(candidate_id, "confirmed")
                self.store.commit()
            except _AlreadyCommitted as sentinel:
                raise sentinel.error
            except Exception:
                if self._in_transaction():
                    self.store.rollback()
                raise

            view = self._candidate_view(candidate_id)
            view["group_size_changes"] = size_changes
            view["entries_view"] = self._entries_view(candidate_id)
            return view

    # ================================================================== #
    # 取消 / 释放
    # ================================================================== #
    def cancel(self, candidate_id: str) -> dict:
        """取消候选：pending 时释放暂占；confirmed 时释放全部已确认占用。"""
        with self._lock:
            self.reap_expired()
            self.store.begin()
            try:
                row = self.store.get_candidate(candidate_id)
                if row is None:
                    raise CandidateNotFound(f"候选方案不存在：{candidate_id}")
                status = row["status"]
                if status in ("cancelled", "expired"):
                    released = 0
                else:
                    self.store.release_holds(candidate_id)
                    released = self.store.delete_booking(candidate_id)
                    self.store.set_candidate_status(candidate_id, "cancelled")
                self.store.commit()
            except Exception:
                self.store.rollback()
                raise
        return {"id": candidate_id, "status": "cancelled",
                "previous_status": status, "released_entries": released}

    # ================================================================== #
    # 过期回收
    # ================================================================== #
    def reap_expired(self) -> list[str]:
        """回收所有过期 pending 候选的暂占；启动时与每次操作前调用。"""
        with self._lock:
            ids = self.store.list_expired_pending(self.clock.ts())
            if not ids:
                return []
            self.store.begin()
            try:
                for cid in ids:
                    self.store.release_holds(cid)
                    self.store.set_candidate_status(cid, "expired")
                self.store.commit()
            except Exception:
                self.store.rollback()
                raise
        return ids

    # ================================================================== #
    # 查询
    # ================================================================== #
    def get_resource(self, rid: str) -> dict:
        row = self.store.get_resource(rid)
        if row is None:
            raise ResourceNotFound(f"资源不存在：{rid}")
        return self._resource_to_dict(row)

    def list_resources(self, kind: str | None = None) -> list[dict]:
        return [self._resource_to_dict(r) for r in self.store.list_resources(kind)]

    def list_groups(self) -> list[dict]:
        return [self._group_to_dict(g) for g in self.store.list_groups()]

    def get_candidate(self, cid: str) -> dict:
        return self._candidate_view(cid)

    def schedule(self, resource_id: str | None = None) -> dict:
        rows = self.store.list_schedule(resource_id)
        return {"entries": [self._entry_to_dict(r) for r in rows]}

    def stats(self) -> dict:
        return self.store.stats()

    def close(self) -> None:
        self.store.close()

    # ================================================================== #
    # 内部：快照 / 解析 / 视图
    # ================================================================== #
    def _in_transaction(self) -> bool:
        return self.store.conn.in_transaction

    def _snapshot(self, exclude_candidate: str | None) -> Snapshot:
        resources = {
            r["id"]: {
                "id": r["id"], "kind": r["kind"], "name": r["name"],
                "capacity": r["capacity"], "travel_buffer_min": r["travel_buffer_min"],
                "active": bool(r["active"]), "version": r["version"],
                "qualifs": json.loads(r["qualifs"]),
                "availability": json.loads(r["availability"]),
            }
            for r in self.store.list_resources()
        }
        groups = {
            g["id"]: {"id": g["id"], "name": g["name"], "size": g["size"],
                      "active": bool(g["active"]), "version": g["version"],
                      "travel_buffer_min": g["travel_buffer_min"]}
            for g in self.store.list_groups()
        }
        blocks: dict[str, list[dict]] = {}
        for b in self.store.list_blocks():
            blocks.setdefault(b["resource_id"], []).append(
                {"start": b["start_min"], "end": b["end_min"], "reason": b["reason"]})

        occupancies: list[Occupancy] = []
        external_sessions: dict[str, tuple[int, int]] = {}
        # 已确认排期（含团体与场地人数权重）
        for e in self.store.list_schedule():
            external_sessions[e["session_id"]] = (e["start_min"], e["end_min"])
            erows = self.store.entry_resources(e["id"])
            for er in erows:
                occupancies.append(Occupancy(
                    er["resource_id"], e["session_id"], e["start_min"], e["end_min"],
                    int(er["weight"]), "booking"))
            if e["group_id"]:
                occupancies.append(Occupancy(
                    e["group_id"], e["session_id"], e["start_min"], e["end_min"], 1,
                    "booking"))
        # 未过期暂占
        now_ts = self.clock.ts()
        hsql = """
            SELECT h.* FROM holds h JOIN candidates c ON c.id = h.candidate_id
            WHERE c.status='pending' AND c.expires_ts>?
        """
        hargs: list[object] = [now_ts]
        if exclude_candidate:
            hsql += " AND h.candidate_id != ?"
            hargs.append(exclude_candidate)
        for h in self.store.conn.execute(hsql, hargs):
            occupancies.append(Occupancy(
                h["resource_id"], h["session_id"], h["start_min"], h["end_min"],
                int(h["weight"]), "hold"))
        return Snapshot(resources, groups, blocks, occupancies, external_sessions)

    def _parse_request(self, sessions: list[dict], ttl_seconds: int,
                       note: str) -> CandidateRequest:
        if not sessions:
            raise ValueError("至少需要一个场次")
        if ttl_seconds <= 0:
            raise ValueError("候选有效期必须为正数")
        out: list[SessionReq] = []
        seen: set[str] = set()
        for s in sessions:
            sid = s["session_id"]
            if sid in seen:
                raise ValueError(f"场次编号重复：{sid}")
            seen.add(sid)
            start, end = to_min(s["start"]), to_min(s["end"])
            if end <= start:
                raise ValueError(f"场次 {s.get('session_id')} 结束必须晚于开始")
            roles: list[RoleReq] = []
            for role in s.get("roles", []):
                roles.append(RoleReq(
                    kind=role["kind"],
                    qualifications=list(role.get("qualifications", [])),
                    pinned=role.get("resource_id"),
                    count=int(role.get("count", 1))))
            out.append(SessionReq(
                session_id=s["session_id"], title=s.get("title", s["session_id"]),
                start=start, end=end, roles=roles,
                group_id=s.get("group_id"),
                depends_on=list(s.get("depends_on", []))))
        return CandidateRequest(sessions=out, ttl_seconds=ttl_seconds, note=note)

    def _request_from_payload(self, payload: dict) -> CandidateRequest:
        sessions: list[SessionReq] = []
        for e in payload["entries"]:
            roles: list[RoleReq] = [
                RoleReq(kind=rp["kind"],
                        qualifications=list(rp.get("qualifications", [])),
                        pinned=rp["resource_id"])
                for rp in e.get("roles", [])
                # 兼容旧视图：没有 roles 明细时按 resource_ids 还原
            ] if "roles" in e else [
                RoleReq(kind=(self.store.get_resource(rid) or
                              {"kind": "unknown"})["kind"], pinned=rid)
                for rid in e["resource_ids"]
            ]
            sessions.append(SessionReq(
                session_id=e["session_id"], title=e["title"],
                start=e["start_min"], end=e["end_min"], roles=roles,
                group_id=e.get("group_id"), depends_on=list(e.get("depends_on", []))))
        return CandidateRequest(sessions=sessions,
                                ttl_seconds=payload.get("ttl_seconds", DEFAULT_TTL_SECONDS))

    def _build_payload(self, req: CandidateRequest, result) -> dict:
        assign_by_session = {a.session_id: a.resource_ids for a in result.assignments}
        entries = []
        for sess in req.sessions:
            rids = assign_by_session[sess.session_id]
            venue_id = next(
                (rid for rid in rids
                 if (row := self.store.get_resource(rid)) is not None
                 and row["kind"] == ResourceKind.VENUE.value), None)
            # 分配结果按角色顺序一一对应（count>1 的角色按挑选顺序展开）
            roles_payload = []
            rid_iter = iter(rids)
            for role in sess.roles:
                for _ in range(max(1, role.count)):
                    roles_payload.append({
                        "kind": role.kind, "resource_id": next(rid_iter),
                        "qualifications": list(role.qualifications)})
            entries.append({
                "session_id": sess.session_id,
                "title": sess.title,
                "start_min": sess.start,
                "end_min": sess.end,
                "resource_ids": rids,
                "roles": roles_payload,
                "group_id": sess.group_id,
                "group_size": result.group_sizes.get(sess.group_id, 0),
                "venue_id": venue_id,
                "depends_on": list(sess.depends_on),
            })
        return {"ttl_seconds": req.ttl_seconds, "entries": entries,
                "resource_versions": result.resource_versions,
                "group_versions": result.group_versions}

    def _validate_resource(self, data: dict) -> None:
        if "id" not in data:
            raise ValueError("资源必须包含 id")
        if data.get("kind") not in {k.value for k in ResourceKind}:
            raise ValueError(f"资源类型非法：{data.get('kind')}")
        if int(data.get("capacity", 1)) <= 0:
            raise ValueError("容量必须为正数")
        if int(data.get("travel_buffer_min", 0)) < 0:
            raise ValueError("移动缓冲不能为负")

    # ---- 视图 -----------------------------------------------------------
    def _candidate_view(self, cid: str) -> dict:
        row = self.store.get_candidate(cid)
        if row is None:
            raise CandidateNotFound(f"候选方案不存在：{cid}")
        payload = json.loads(row["payload"])
        expired = row["status"] == "pending" and row["expires_ts"] <= self.clock.ts()
        return {
            "id": cid,
            "feasible": True,
            "status": "expired" if expired else row["status"],
            "created_ts": row["created_ts"],
            "expires_ts": row["expires_ts"],
            "ttl_seconds": int(row["expires_ts"] - row["created_ts"]),
            "note": row["note"],
            "entries": [{
                "session_id": e["session_id"],
                "title": e["title"],
                "start_min": e["start_min"],
                "end_min": e["end_min"],
                "resource_ids": e["resource_ids"],
                "group_id": e.get("group_id"),
                "group_size": e["group_size"],
                "depends_on": e.get("depends_on", []),
            } for e in payload["entries"]],
        }

    def _entries_view(self, candidate_id: str) -> list[dict]:
        return [self._entry_to_dict(r)
                for r in self.store.entries_for_candidate(candidate_id)]

    def _entry_to_dict(self, row) -> dict:
        return {
            "session_id": row["session_id"], "title": row["title"],
            "start_min": row["start_min"], "end_min": row["end_min"],
            "group_id": row["group_id"], "group_size": row["group_size"],
            "venue_id": row["venue_id"], "candidate_id": row["candidate_id"],
            "confirmed_ts": row["confirmed_ts"],
            "resource_ids": [er["resource_id"]
                             for er in self.store.entry_resources(row["id"])],
        }

    def _resource_to_dict(self, row) -> dict:
        return {
            "id": row["id"], "kind": row["kind"], "name": row["name"],
            "capacity": row["capacity"],
            "travel_buffer_min": row["travel_buffer_min"],
            "active": bool(row["active"]), "version": row["version"],
            "qualifications": json.loads(row["qualifs"]),
            "availability": [{"start_min": w["s"], "end_min": w["e"]}
                             for w in json.loads(row["availability"])],
        }

    def _group_to_dict(self, row) -> dict:
        return {"id": row["id"], "name": row["name"], "size": row["size"],
                "contact": row["contact"], "active": bool(row["active"]),
                "travel_buffer_min": row["travel_buffer_min"],
                "version": row["version"]}
