"""候选方案引擎：在不写锁的快照上求解多资源排班，并产出可解释替代方案。

校验维度（对应领域契约四个不变量）：

- 组合资源约束：每场需要的讲解员/讲师/场地按角色与资格组合选取，学校团体人数计入场地容量；
- 时段缓冲冲突：同一资源相邻场次之间必须满足各自的 ``travel_buffer_min``；
- 原子确认占用：引擎只产出方案，落锁由 service 在单事务内完成；
- 过期暂占回收：快照只计入未过期暂占，过期数据由 service 回收。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import ResourceKind, min_to_iso

SLOT_STEP_MIN = 15          # 替代时段搜索粒度
ALT_SEARCH_WINDOW_MIN = 240  # 在原定时间前后各 4 小时内寻找替代时段
MAX_ALT_TIMES = 3
MAX_ALT_RESOURCES = 4


@dataclass
class RoleReq:
    """单场对一类资源的需求，例如需要一名具备 ``lecture:航天`` 资格的讲师。"""

    kind: str
    qualifications: list[str] = field(default_factory=list)
    pinned: str | None = None  # 统筹员指定资源 id 时不可替换
    count: int = 1


@dataclass
class SessionReq:
    session_id: str
    title: str
    start: int
    end: int
    roles: list[RoleReq]
    group_id: str | None = None
    depends_on: list[str] = field(default_factory=list)


@dataclass
class CandidateRequest:
    sessions: list[SessionReq]
    ttl_seconds: int = 600
    note: str = ""


@dataclass
class Occupancy:
    """一次既存占用：已确认排期或其它候选的有效暂占。"""

    resource_id: str
    session_id: str
    start: int
    end: int
    weight: int
    source: str  # booking / hold / plan


@dataclass
class Snapshot:
    resources: dict[str, dict]
    groups: dict[str, dict]
    blocks: dict[str, list[dict]]            # resource_id -> [{start,end,reason}]
    occupancies: list[Occupancy]             # 已确认 + 未过期暂占
    external_sessions: dict[str, tuple[int, int]] = field(default_factory=dict)
    """批次外已确认场次 session_id -> (start,end)，供场次依赖校验。"""

    def occ_for(self, rid: str) -> list[Occupancy]:
        return [o for o in self.occupancies if o.resource_id == rid]


@dataclass
class Assignment:
    session_id: str
    resource_ids: list[str]


@dataclass
class PlanResult:
    feasible: bool
    assignments: list[Assignment]
    group_sizes: dict[str, int]              # group_id -> 确认时人数快照
    resource_versions: dict[str, int]
    group_versions: dict[str, int]
    conflicts: list[dict] = field(default_factory=list)
    alternatives: list[dict] = field(default_factory=list)

    def resource_set(self) -> set[str]:
        out: set[str] = set()
        for a in self.assignments:
            out.update(a.resource_ids)
        return out


def _buffer_overlaps(start: int, end: int, o: Occupancy, buf: int) -> bool:
    """含移动缓冲的相邻判定：新区间向两侧展开 buf 后是否与既存占用相交。"""
    return start < o.end + buf and o.start - buf < end


def _direct_overlaps(start: int, end: int, o: Occupancy) -> bool:
    return start < o.end and o.start < end


def _source_label(source: str) -> str:
    return {"booking": "已确认排期", "hold": "他人候选暂占", "plan": "本方案场次"}.get(
        source, "既有占用")


class CandidateEngine:
    def __init__(self, snapshot: Snapshot) -> None:
        self.snap = snapshot

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def plan(self, req: CandidateRequest) -> PlanResult:
        sessions = self._topo_order(req.sessions)
        self._sessions_by_id = {s.session_id: s for s in req.sessions}
        chosen: dict[str, list[str]] = {}
        conflicts: list[dict] = []
        alternatives: list[dict] = []

        occ = list(self.snap.occupancies)  # 本方案逐步累加
        for sess in sessions:
            picked, sess_conflicts, sess_alts = self._assign_session(sess, occ, chosen)
            if picked is None:
                conflicts.extend(sess_conflicts)
                if sess_alts:
                    alternatives.append({
                        "session_id": sess.session_id,
                        "title": sess.title,
                        "options": sess_alts,
                    })
                continue
            chosen[sess.session_id] = picked
            for rid in picked:
                occ.append(Occupancy(rid, sess.session_id, sess.start, sess.end,
                                     1, "plan"))
            if sess.group_id:
                occ.append(Occupancy(sess.group_id, sess.session_id,
                                     sess.start, sess.end, 1, "plan"))

        feasible = len(chosen) == len(sessions)
        versions = {rid: self.snap.resources[rid]["version"]
                    for rid in {r for ids in chosen.values() for r in ids}}
        group_sizes = {s.group_id: self._group_size(s.group_id)
                       for s in sessions if s.group_id}
        gversions = {gid: self.snap.groups[gid]["version"]
                     for gid in group_sizes if gid in self.snap.groups}
        return PlanResult(
            feasible=feasible,
            assignments=[Assignment(s.session_id, chosen[s.session_id])
                         for s in sessions if s.session_id in chosen],
            group_sizes=group_sizes,
            resource_versions=versions,
            group_versions=gversions,
            conflicts=conflicts,
            alternatives=alternatives,
        )

    # ------------------------------------------------------------------ #
    # 场次依赖
    # ------------------------------------------------------------------ #
    def _topo_order(self, sessions: list[SessionReq]) -> list[SessionReq]:
        by_id = {s.session_id: s for s in sessions}
        ordered: list[SessionReq] = []
        visited: set[str] = set()

        def visit(s: SessionReq, stack: set[str]) -> None:
            if s.session_id in visited:
                return
            if s.session_id in stack:
                raise ValueError(f"场次依赖存在环：{s.session_id}")
            stack.add(s.session_id)
            for dep in s.depends_on:
                if dep in by_id:
                    visit(by_id[dep], stack)
            stack.discard(s.session_id)
            visited.add(s.session_id)
            ordered.append(s)

        for s in sessions:
            visit(s, set())
        return ordered

    # ------------------------------------------------------------------ #
    # 单场分配
    # ------------------------------------------------------------------ #
    def _group_size(self, gid: str | None) -> int:
        if not gid:
            return 0
        g = self.snap.groups.get(gid)
        return int(g["size"]) if g else 0

    def _assign_session(self, sess: SessionReq, occ: list[Occupancy],
                        chosen: dict[str, list[str]]
                        ) -> tuple[list[str] | None, list[dict], list[dict]]:
        conflicts: list[dict] = []
        options: list[dict] = []

        # 学校团体与场次依赖先校验
        group_size = 0
        group_buf = 0
        if sess.group_id:
            g = self.snap.groups.get(sess.group_id)
            if g is None:
                conflicts.append(self._conflict(sess, sess.group_id, "学校团体缺失",
                                                "group_missing", "group"))
            elif not g["active"]:
                conflicts.append(self._conflict(sess, sess.group_id,
                                                f"学校团体 {g['name']} 已停用",
                                                "inactive", "group"))
            else:
                group_size = int(g["size"])
                group_buf = int(g.get("travel_buffer_min", 0))
                for b in self._hard_blocks(sess.group_id, sess.start, sess.end,
                                           group_buf, sess.session_id):
                    conflicts.append(b)
                for o in occ:
                    if o.resource_id != sess.group_id:
                        continue
                    if _direct_overlaps(sess.start, sess.end, o):
                        conflicts.append({
                            "resource_id": sess.group_id,
                            "resource_name": g["name"],
                            "kind": "double_booking",
                            "session_id": sess.session_id,
                            "interval": self._iv(sess),
                            "message": f"学校团体 {g['name']} 同期已安排场次 {o.session_id}"
                                       f"（{_source_label(o.source)}）",
                            "conflicting_session": o.session_id,
                        })
                    elif _buffer_overlaps(sess.start, sess.end, o, group_buf):
                        conflicts.append({
                            "resource_id": sess.group_id,
                            "resource_name": g["name"],
                            "kind": "buffer",
                            "session_id": sess.session_id,
                            "interval": self._iv(sess),
                            "message": f"学校团体 {g['name']} 转场间隔不足 {group_buf} 分钟"
                                       f"（相邻场次 {o.session_id}，{_source_label(o.source)}）",
                            "conflicting_session": o.session_id,
                        })

        dep_conflict = self._dependency_conflict(sess, chosen)
        if dep_conflict:
            conflicts.append(dep_conflict)

        picked: list[str] = []
        role_failed = False
        for role in sess.roles:
            picked_for_role: list[str] = []
            for i in range(max(1, role.count)):
                rid, role_conflicts, _ = self._pick_role(
                    sess, role, group_size, occ,
                    exclude=set(picked) | set(picked_for_role))
                if rid is None:
                    role_failed = True
                    conflicts.extend(role_conflicts)
                    break
                picked_for_role.append(rid)
            picked.extend(picked_for_role)

        if not role_failed and not conflicts:
            return picked, [], []

        # 失败：汇总可解释替代方案（同角色备选人 + 可平移的时间段）
        options: list[dict] = []
        for role in sess.roles:
            cands = self._eligible_resources(role)
            cands.sort(key=lambda r: self._score(r, sess))
            if cands:
                options.append({"role": role.kind,
                                "qualifications": role.qualifications,
                                "resources": [self._resource_option(r, sess, group_size, occ)
                                              for r in cands[:MAX_ALT_RESOURCES]]})
        alt_times = self._alternative_times(sess, group_size, chosen, occ)
        if alt_times:
            options.append({"role": "__time__", "resources": [], "times": alt_times})
        return None, conflicts, options

    def _dependency_conflict(self, sess: SessionReq,
                             chosen: dict[str, list[str]]) -> dict | None:
        for dep in sess.depends_on:
            dep_end = self._dependency_end(dep, chosen)
            if dep_end is None:
                return {
                    "resource_id": "",
                    "resource_name": "",
                    "kind": "dependency",
                    "session_id": sess.session_id,
                    "interval": self._iv(sess),
                    "message": f"场次 {sess.title} 依赖的前置场次 {dep} 无法同期排定",
                    "conflicting_session": dep,
                }
            if dep_end > sess.start:
                return {
                    "resource_id": "",
                    "resource_name": "",
                    "kind": "dependency",
                    "session_id": sess.session_id,
                    "interval": self._iv(sess),
                    "message": f"场次 {sess.title} 开始早于前置场次 {dep} 结束，违反场次依赖先后顺序",
                    "conflicting_session": dep,
                }
        return None

    def _dependency_end(self, dep: str,
                        chosen: dict[str, list[str]]) -> int | None:
        """前置场次的结束时间；批次内未排定返回 None，批次外取已确认快照。"""
        if dep in chosen:
            dep_sess = self._sessions_by_id.get(dep)
            return dep_sess.end if dep_sess else None
        external = self.snap.external_sessions.get(dep)
        return external[1] if external else None

    def _pick_role(self, sess: SessionReq, role: RoleReq, group_size: int,
                   occ: list[Occupancy], exclude: set[str] | None = None
                   ) -> tuple[str | None, list[dict], list[dict]]:
        exclude = exclude or set()
        candidates = [r for r in self._eligible_resources(role) if r["id"] not in exclude]
        if role.pinned:
            pinned = self.snap.resources.get(role.pinned)
            if pinned is None or not pinned["active"] or role.pinned in exclude:
                return None, [self._conflict(
                    sess, role.pinned or "",
                    f"指定资源 {role.pinned} 不存在、已停用或已承担本场其他角色",
                    "inactive", role.kind)], []
            if pinned["kind"] != role.kind:
                return None, [self._conflict(
                    sess, role.pinned,
                    f"{pinned['name']} 类型为 {pinned['kind']}，"
                    f"不能承担 {role.kind} 角色",
                    "qualification", role.kind)], []
            missing = sorted(set(role.qualifications) - set(pinned["qualifs"]))
            if missing:
                return None, [self._conflict(
                    sess, role.pinned,
                    f"{pinned['name']} 缺少资格：{'、'.join(missing)}",
                    "qualification", role.kind)], []
            candidates = [pinned]

        conflicts: list[dict] = []
        scored: list[tuple[tuple, dict]] = []
        for r in candidates:
            ok, info = self._resource_fits(r, sess, group_size, occ)
            if ok:
                scored.append((self._score(r, sess), r))
            else:
                conflicts.append(info)
        scored.sort(key=lambda x: x[0])

        if scored:
            alternatives = [self._resource_option(r, sess, group_size, occ)
                            for _, r in scored[:MAX_ALT_RESOURCES]]
            return scored[0][1]["id"], [], alternatives

        if not candidates:
            # 资格层面就无人可用：给出同类资源的资格缺口说明
            same_kind = [r for r in self.snap.resources.values()
                         if r["kind"] == role.kind and r["active"]]
            for r in same_kind[:MAX_ALT_RESOURCES]:
                missing = sorted(set(role.qualifications) - set(r["qualifs"]))
                conflicts.append(self._conflict(
                    sess, r["id"],
                    f"{r['name']} 缺少资格：{'、'.join(missing)}",
                    "qualification", role.kind))
            if not same_kind:
                conflicts.append(self._conflict(
                    sess, "", f"没有类型为 {role.kind} 的可用资源",
                    "qualification", role.kind))
        return None, conflicts, []

    def _eligible_resources(self, role: RoleReq) -> list[dict]:
        out = []
        for r in self.snap.resources.values():
            if r["kind"] != role.kind or not r["active"]:
                continue
            if not set(role.qualifications) <= set(r["qualifs"]):
                continue
            out.append(r)
        return out

    # ------------------------------------------------------------------ #
    # 资源级校验
    # ------------------------------------------------------------------ #
    def _resource_fits(self, r: dict, sess: SessionReq, group_size: int,
                       occ: list[Occupancy]) -> tuple[bool, dict | None]:
        rid = r["id"]
        start, end = sess.start, sess.end
        buf = int(r["travel_buffer_min"])

        # 可用时段
        if not any(w["s"] <= start and end <= w["e"] for w in r["availability"]):
            return False, self._conflict(
                sess, rid, f"{r['name']} 在该时段不可用（未落入可用时段）",
                "availability", r["kind"])

        # 容量（场地：单场独占 + 人数上限；其余按场次权重 1）
        cap = int(r["capacity"])
        is_venue = r["kind"] == ResourceKind.VENUE.value
        if is_venue and group_size > cap:
            return False, self._conflict(
                sess, rid,
                f"{r['name']} 容量 {cap} 人，学校团体 {group_size} 人超出容量",
                "capacity", r["kind"])

        need_weight = 1  # 场地也按一场计，保证同厅不会塞入两场
        direct_load = need_weight
        overlapping: list[Occupancy] = []
        for o in occ:  # occ 已包含既存占用与本方案先前场次
            if o.resource_id != rid:
                continue
            if _direct_overlaps(start, end, o):
                direct_load += 1 if is_venue else o.weight
                overlapping.append(o)
            elif _buffer_overlaps(start, end, o, buf):
                return False, self._conflict(
                    sess, rid,
                    f"{r['name']} 与相邻场次 {o.session_id}（{_source_label(o.source)}）"
                    f"间隔不足 {buf} 分钟移动缓冲",
                    "buffer", r["kind"], o.session_id)
        overloaded = (len(overlapping) > 0 if is_venue else direct_load > cap)
        if overloaded:
            other = overlapping[0].session_id if overlapping else None
            label = _source_label(overlapping[0].source) if overlapping else ""
            return False, self._conflict(
                sess, rid,
                f"{r['name']} 在该时段已被场次 {other or '?'}（{label}）占用"
                if is_venue or overlapping else
                f"{r['name']} 容量 {cap}，该时段并行场次将达 {direct_load}",
                "double_booking" if overlapping else "capacity",
                r["kind"], other)

        # 拒绝时段 / 主动不可用
        hard = self._hard_blocks(rid, start, end, buf, sess.session_id)
        if hard:
            return False, hard[0]
        return True, None

    def _hard_blocks(self, rid: str, start: int, end: int, buf: int,
                     session_id: str = "") -> list[dict]:
        out: list[dict] = []
        for b in self.snap.blocks.get(rid, []):
            if start < b["end"] + buf and b["start"] - buf < end:
                name = (self.snap.resources.get(rid) or
                        self.snap.groups.get(rid) or {}).get("name", rid)
                out.append({
                    "resource_id": rid,
                    "resource_name": name,
                    "kind": "availability",
                    "session_id": session_id,
                    "interval": {"start_min": start, "end_min": end,
                                 "start": min_to_iso(start), "end": min_to_iso(end)},
                    "message": f"资源在该时段被拒绝占用：{b.get('reason', '已拒绝')}"
                               f"（{min_to_iso(b['start'])}-{min_to_iso(b['end'])}）",
                    "conflicting_session": None,
                })
        return out

    def _score(self, r: dict, sess: SessionReq) -> tuple:
        """确定性打分：距相邻占用越空、容量越贴合、id 越小越优先。"""
        rid = r["id"]
        buf = int(r["travel_buffer_min"])
        gap = 10 ** 6
        for o in self.snap.occ_for(rid):
            if o.end <= sess.start:
                gap = min(gap, sess.start - o.end - buf)
            elif o.start >= sess.end:
                gap = min(gap, o.start - sess.end - buf)
        return (-gap, int(r["capacity"]), rid)

    def _resource_option(self, r: dict, sess: SessionReq, group_size: int,
                         occ: list[Occupancy]) -> dict:
        return {
            "resource_id": r["id"],
            "name": r["name"],
            "kind": r["kind"],
            "capacity": r["capacity"],
            "qualifications": r["qualifs"],
        }

    # ------------------------------------------------------------------ #
    # 替代时段
    # ------------------------------------------------------------------ #
    def _alternative_times(self, sess: SessionReq, group_size: int,
                           chosen: dict[str, list[str]], occ: list[Occupancy]
                           ) -> list[dict]:
        """保持各角色最优人选不变，在前后窗口内滑动寻找可行时间。"""
        # 以角色条目为单位选基准资源（同类型多角色 / count>1 取不同资源）
        expanded_roles = [role for role in sess.roles for _ in range(max(1, role.count))]
        basis: list[str] = []
        used: set[str] = set()
        for role in expanded_roles:
            cands = sorted(
                (r for r in self._eligible_resources(role) if r["id"] not in used),
                key=lambda r: self._score(r, sess))
            if not cands:
                return []
            basis.append(cands[0]["id"])
            used.add(cands[0]["id"])
        found: list[dict] = []
        for delta in range(SLOT_STEP_MIN, ALT_SEARCH_WINDOW_MIN + 1, SLOT_STEP_MIN):
            for sign in (-1, +1):
                ns, ne = sess.start + sign * delta, sess.end + sign * delta
                if self._time_feasible(sess, ns, ne, basis, expanded_roles,
                                       group_size, chosen, occ):
                    found.append({"start_min": ns, "end_min": ne,
                                 "start": min_to_iso(ns), "end": min_to_iso(ne),
                                 "shift_min": sign * delta,
                                 "resources": basis})
                    if len(found) >= MAX_ALT_TIMES:
                        return found
        return found

    def _time_feasible(self, sess: SessionReq, start: int, end: int,
                       basis: list[str], expanded_roles: list[RoleReq],
                       group_size: int,
                       chosen: dict[str, list[str]], occ: list[Occupancy]) -> bool:
        # 依赖：前置场次（批次内或已确认的外部场次）必须在新的开始之前
        for dep in sess.depends_on:
            dep_end = self._dependency_end(dep, chosen)
            if dep_end is None or dep_end > start:
                return False
        shifted = SessionReq(sess.session_id, sess.title, start, end,
                             sess.roles, sess.group_id, sess.depends_on)
        # 每个角色条目用各自基准资源，并防止同场资源相互重叠
        local_occ = list(occ)
        for idx, role in enumerate(expanded_roles):
            rid = basis[idx]
            r = self.snap.resources[rid]
            ok, _ = self._resource_fits(r, shifted, group_size, local_occ)
            if not ok:
                return False
            local_occ.append(Occupancy(rid, sess.session_id, start, end, 1, "plan"))
        if sess.group_id:
            g = self.snap.groups.get(sess.group_id)
            if not g or not g["active"]:
                return False
            gbuf = int(g.get("travel_buffer_min", 0))
            if self._hard_blocks(sess.group_id, start, end, gbuf,
                                 sess.session_id):
                return False
            for o in occ:
                if o.resource_id == sess.group_id and _buffer_overlaps(start, end, o, gbuf):
                    return False
        return True

    # ------------------------------------------------------------------ #
    def _iv(self, sess: SessionReq) -> dict:
        return {"start_min": sess.start, "end_min": sess.end,
                "start": min_to_iso(sess.start), "end": min_to_iso(sess.end)}

    def _conflict(self, sess: SessionReq, rid: str, message: str, kind: str,
                  resource_kind: str, other_sid: str | None = None) -> dict:
        r = self.snap.resources.get(rid)
        return {
            "resource_id": rid,
            "resource_name": (r or {}).get("name", rid),
            "kind": kind,
            "session_id": sess.session_id,
            "interval": self._iv(sess),
            "message": message,
            "conflicting_session": other_sid,
        }
