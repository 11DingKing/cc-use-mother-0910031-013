"""候选方案生成与冲突诊断。

策略：先为每个场次枚举所有满足资格、容量、可用时段与既有占用（含移动缓冲）
的单场选项，再以 DFS 组合所有场次，组合阶段校验场地重叠、人员移动缓冲与场次
依赖，按评分保留前 N 个候选。无完整方案时输出结构化阻塞原因与可解释替代方案。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import timeutil
from .catalog import Catalog
from .models import (
    Blocker, Candidate, Diagnosis, PlannedAssignment, SessionReq, Suggestion,
)
from .occupancy import Booking, Occupancy

GRID_MINUTES = 5
RELAX_WINDOW_MINUTES = 120
TOP_N = 3


@dataclass(frozen=True)
class Option:
    req_id: str
    staff_id: str
    venue_id: str
    start: int
    end: int
    contended: bool = False


@dataclass
class _ReqContext:
    req: SessionReq
    options: list[Option]
    blockers: list[Blocker]
    contended: list[Blocker]


class Planner:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    # ------------------------------------------------------------------
    def diagnose(self, reqs: list[SessionReq], headcount: int,
                 hard_occ: Occupancy, soft_occ: Occupancy,
                 hold_expiry: dict[str, int],
                 top_n: int = TOP_N) -> Diagnosis:
        contexts = [self._context(r, headcount, hard_occ, soft_occ, 0)
                    for r in reqs]
        candidates = self._combine(contexts, top_n)

        blockers: list[Blocker] = []
        for ctx in contexts:
            blockers.extend(ctx.blockers)
        suggestions: list[Suggestion] = list(self._contention_suggestions(
            contexts, candidates, hold_expiry))

        if not candidates and contexts:
            suggestions.extend(self._suggest(contexts, reqs, headcount,
                                             hard_occ, soft_occ,
                                             hold_expiry, top_n))
        return Diagnosis(tuple(candidates), tuple(blockers), tuple(suggestions))

    def _contention_suggestions(self, contexts, candidates, hold_expiry):
        """所有可行候选都与他人待确认暂占竞争时，提示等待释放。"""
        if not candidates or not all(c.contended for c in candidates):
            return []
        held_ids = {b.reservation_id for ctx in contexts for b in ctx.contended}
        held_ids.discard(None)
        if not held_ids:
            return []
        expiries = [(rid, hold_expiry.get(rid)) for rid in held_ids]
        expiries = [(rid, e) for rid, e in expiries if e is not None]
        if not expiries:
            return []
        soonest_rid, soonest = min(expiries, key=lambda x: x[1])
        return [Suggestion(
            "WAIT_FOR_HOLDS",
            f"全部可行候选都与 {len(held_ids)} 个待确认暂占竞争（确认时先到先得）；"
            f"最早释放的是 {soonest_rid}（{timeutil.hm(soonest)} 过期），"
            "可稍后自动重试，或主动联系对方取消")]

    # ---- 单场选项 ------------------------------------------------------
    def _context(self, req: SessionReq, headcount: int,
                 hard_occ: Occupancy, soft_occ: Occupancy,
                 extend: int) -> _ReqContext:
        blockers: list[Blocker] = []
        eligible = self.catalog.eligible_staff(req)
        if not eligible:
            blockers.append(Blocker(
                "qualification", None, "no_eligible_staff",
                f"场次 {req.req_id} 需要 {req.role}/{req.qualification}，"
                "但没有同时满足角色与资格的人员"))
            return _ReqContext(req, [], blockers, [])

        venues = self.catalog.candidate_venues(req, headcount)
        if not venues:
            if req.venue_id is not None:
                venue = self.catalog.all_venues()
                target = next((v for v in venue if v.id == req.venue_id), None)
                if target is None:
                    blockers.append(Blocker(
                        "venue", req.venue_id, "unknown_venue",
                        f"场次 {req.req_id} 指定的展厅 {req.venue_id} 不存在"))
                else:
                    blockers.append(Blocker(
                        "capacity", req.venue_id, "venue_too_small",
                        f"展厅 {target.name} 容量 {target.capacity} 人，"
                        f"无法容纳团体 {headcount} 人"))
            else:
                largest = max(self.catalog.all_venues(),
                              key=lambda v: v.capacity, default=None)
                msg = ("没有容量足够的展厅"
                       if largest is None
                       else f"团体 {headcount} 人超过最大展厅容量"
                            f"（{largest.name} 仅 {largest.capacity} 人）")
                blockers.append(Blocker("capacity", None, "no_venue_fits", msg))
            return _ReqContext(req, [], blockers, [])

        latest = req.latest_start
        natural_upper = self._natural_upper(eligible, venues)
        base_end = latest if latest is not None else natural_upper
        # extend>0 时把收录窗口一并延展（仅用于替代方案诊断），
        # 超出原窗口的选项由调用方通过 shifted 筛选。
        window_end = base_end + extend
        search_end = window_end

        options: dict[tuple[str, str, int], Option] = {}
        held_blockers: list[Blocker] = []
        contended_blockers: list[Blocker] = []
        for staff in eligible:
            for venue in venues:
                start = req.earliest_start
                while start <= search_end:
                    end = start + req.duration
                    if not self.catalog.within_any(staff.availability, start, end):
                        start += GRID_MINUTES
                        continue
                    if not self.catalog.within_any(venue.availability, start, end):
                        start += GRID_MINUTES
                        continue
                    in_window = start <= window_end
                    hb_v = hard_occ.venue_clash(venue.id, start, end)
                    hb_s = hard_occ.staff_clash(self.catalog, staff.id,
                                                venue.id, start, end)
                    if hb_v is None and hb_s is None and in_window:
                        sb_v = soft_occ.venue_clash(venue.id, start, end)
                        sb_s = soft_occ.staff_clash(self.catalog, staff.id,
                                                    venue.id, start, end)
                        contended = (sb_v is not None or sb_s is not None)
                        if contended:
                            soft = sb_v if sb_v is not None else sb_s
                            cb = self._clash_blocker(req, soft, sb_v is not None)
                            held_blockers.append(cb)
                            contended_blockers.append(cb)
                        options[(staff.id, venue.id, start)] = Option(
                            req.req_id, staff.id, venue.id, start, end, contended)
                    elif in_window:
                        # 已确认占用构成硬阻塞
                        clash = hb_v if hb_v is not None else hb_s
                        blockers.append(self._clash_blocker(req, clash, hb_v is not None))
                    start += GRID_MINUTES

        if not options:
            if held_blockers:
                blockers.extend(self._dedupe_blockers(held_blockers)[:4])
            reason_end = timeutil.hm(latest + req.duration) if latest is not None else "当日可用时段结束"
            blockers.append(Blocker(
                "availability", None, "no_feasible_slot",
                f"场次 {req.req_id} 在 {timeutil.hm(req.earliest_start)} 至 "
                f"{reason_end} 之间找不到可行排期"
                + ("（已放宽时间窗）" if extend else "")))
        return _ReqContext(req, list(options.values()),
                           self._dedupe_blockers(blockers),
                           self._dedupe_blockers(contended_blockers))

    @staticmethod
    def _natural_upper(staff: list, venues: list) -> int:
        staff_end = min(max(w.end for w in s.availability) for s in staff)
        venue_end = min(max(w.end for w in v.availability) for v in venues)
        return min(staff_end, venue_end)

    def _clash_blocker(self, req: SessionReq, b: Booking, is_venue: bool) -> Blocker:
        kind = "venue" if is_venue else "staff"
        rid = b.venue_id if is_venue else b.staff_id
        label = "展厅" if is_venue else "人员"
        status = "暂占" if b.status == "held" else "已确认"
        return Blocker(
            kind, rid,
            f"{kind}_occupied_{b.status}",
            f"场次 {req.req_id} 与{label} {rid} 的{status}场次 {b.req_id}"
            f"（{timeutil.hm(b.start)}-{timeutil.hm(b.end)}，"
            f"排期 {b.reservation_id}）冲突",
            reservation_id=b.reservation_id, start=b.start, end=b.end)

    @staticmethod
    def _dedupe_blockers(items: list[Blocker]) -> list[Blocker]:
        seen: set[tuple] = set()
        out: list[Blocker] = []
        for x in items:
            key = (x.resource_type, x.resource_id, x.reason, x.reservation_id)
            if key not in seen:
                seen.add(key)
                out.append(x)
        return out

    # ---- 组合搜索 ------------------------------------------------------
    def _combine(self, contexts: list[_ReqContext], top_n: int) -> list[Candidate]:
        if any(not c.options for c in contexts):
            return []
        ctx_by_id = {c.req.req_id: c for c in contexts}
        order = self._dependency_order([c.req for c in contexts])
        results: list[Candidate] = []

        def dfs(idx: int, chosen: dict[str, Option]) -> None:
            if idx == len(order):
                results.append(self._build_candidate(ctx_by_id, chosen, order))
                return
            rid = order[idx]
            req = ctx_by_id[rid].req
            for opt in ctx_by_id[rid].options:
                if self._compatible(req, opt, chosen):
                    chosen[rid] = opt
                    dfs(idx + 1, chosen)
                    del chosen[rid]

        dfs(0, {})
        results.sort(key=lambda c: c.score)
        # 同分按人员/展厅签名去重；保证至少保留一个最优软竞争候选，
        # 让统筹员可以在知情情况下主动抢占。
        unique: list[Candidate] = []
        seen: set[tuple] = set()
        best_contended: Candidate | None = None
        for c in results:
            if c.signature in seen:
                continue
            seen.add(c.signature)
            if c.contended and best_contended is None:
                best_contended = c
            unique.append(c)
        picked = unique[:top_n]
        if best_contended is not None and best_contended not in picked:
            picked.append(best_contended)
        return picked

    def assert_dependency_acyclic(self, reqs: list[SessionReq]) -> list[str]:
        return self._dependency_order(reqs)

    @staticmethod
    def _dependency_order(reqs: list[SessionReq]) -> list[str]:
        ids = {r.req_id for r in reqs}
        order: list[str] = []
        visiting: set[str] = set()

        def visit(rid: str, trail: tuple[str, ...]) -> None:
            if rid in order:
                return
            if rid in visiting:
                cycle = " -> ".join(trail + (rid,))
                raise ValueError(f"场次依赖存在环：{cycle}")
            visiting.add(rid)
            req = next(r for r in reqs if r.req_id == rid)
            for dep in req.after:
                if dep.after_req_id in ids:
                    visit(dep.after_req_id, trail + (rid,))
            visiting.discard(rid)
            order.append(rid)

        for r in reqs:
            visit(r.req_id, ())
        return order

    def _compatible(self, req: SessionReq, opt: Option,
                    chosen: dict[str, Option]) -> bool:
        staff = self.catalog.staff(opt.staff_id)
        for dep in req.after:
            prior = chosen.get(dep.after_req_id)
            if prior is None:
                continue  # 依赖的场次不在本单内，忽略（也可上抛为校验错误）
            if opt.start < prior.end + dep.gap_minutes:
                return False
        for other in chosen.values():
            if other.req_id == opt.req_id:
                continue
            if other.venue_id == opt.venue_id and opt.start < other.end and other.start < opt.end:
                return False
            if other.staff_id == opt.staff_id:
                gap = self.catalog.travel_minutes(staff, opt.venue_id, other.venue_id)
                if opt.start < other.end + gap and other.start < opt.end + gap:
                    return False
        return True

    def _build_candidate(self, ctx_by_id: dict[str, _ReqContext],
                         chosen: dict[str, Option], order: list[str]) -> Candidate:
        assignments = tuple(
            PlannedAssignment(rid, chosen[rid].staff_id, chosen[rid].venue_id,
                              chosen[rid].start, chosen[rid].end)
            for rid in order)
        score = 0.0
        explanations: list[str] = []
        staff_names = {a.staff_id for a in assignments}
        contended = False
        for a in assignments:
            req = ctx_by_id[a.req_id].req
            target = req.preferred_start or req.earliest_start
            score += abs(a.start - target)
            contended = contended or chosen[a.req_id].contended
            explanations.append(
                f"{a.req_id}：{a.staff_id} 于 {a.venue_id}，"
                f"{timeutil.hm(a.start)}-{timeutil.hm(a.end)}")
        score += 5 * (len(staff_names) - 1)  # 优先复用同一人员，减少协调成本
        if contended:
            score += 1000  # 软竞争候选排到无竞争候选之后
        signature = tuple(sorted((a.staff_id, a.venue_id, a.start) for a in assignments))
        return Candidate(assignments, float(score), tuple(explanations),
                         signature, contended)

    # ---- 替代方案 ------------------------------------------------------
    def _suggest(self, contexts: list[_ReqContext], reqs: list[SessionReq],
                 headcount: int, hard_occ: Occupancy, soft_occ: Occupancy,
                 hold_expiry: dict[str, int], top_n: int) -> list[Suggestion]:
        out: list[Suggestion] = []

        # 1) 缩人：任何场地都装不下
        max_cap = max((v.capacity for v in self.catalog.all_venues()), default=0)
        if headcount > max_cap and any(
                any(b.reason in ("venue_too_small", "no_venue_fits")
                    for b in c.blockers) for c in contexts):
            largest = max(self.catalog.all_venues(), key=lambda v: v.capacity)
            out.append(Suggestion(
                "SHRINK_GROUP",
                f"将团体人数从 {headcount} 人降至不超过 {largest.capacity} 人"
                f"（{largest.name}），或拆分为两个团体分别预约"))

        # 2) 等待暂占释放：软竞争全部来自 held
        held_ids = {b.reservation_id
                    for c in contexts for b in c.blockers
                    if b.reservation_id and b.reason.endswith("_occupied_held")}
        confirmed_ids = {b.reservation_id
                         for c in contexts for b in c.blockers
                         if b.reservation_id and b.reason.endswith("_occupied_confirmed")}
        if held_ids and not confirmed_ids:
            expiries = [(rid, hold_expiry.get(rid)) for rid in held_ids]
            expiries = [(rid, e) for rid, e in expiries if e is not None]
            if expiries:
                soonest_rid, soonest = min(expiries, key=lambda x: x[1])
                out.append(Suggestion(
                    "WAIT_FOR_HOLDS",
                    f"冲突来自 {len(held_ids)} 个待确认暂占；最早释放的是 "
                    f"{soonest_rid}（{timeutil.hm(soonest)} 过期），"
                    "可稍后自动重试，或主动联系对方取消"))

        # 3) 放宽时间窗
        relaxed = [self._context(c.req, headcount, hard_occ, soft_occ,
                                 RELAX_WINDOW_MINUTES)
                   for c in contexts]
        rel_candidates = self._combine(relaxed, top_n)
        if rel_candidates:
            shifted = [c for c in rel_candidates
                       if any(a.start > (next(r for r in reqs if r.req_id == a.req_id).latest_start or 1 << 30)
                              for a in c.assignments)]
            if shifted:
                out.append(Suggestion(
                    "EXTEND_WINDOW",
                    f"将最晚开始时间延后至多 {RELAX_WINDOW_MINUTES} 分钟，"
                    "可得到下列可行方案（已按偏好排序）",
                    tuple(shifted[:top_n])))

        # 4) 放宽指定展厅（同时放宽时间窗，给系统最大调配空间）
        fixed = [c for c in contexts if c.req.venue_id is not None and not c.options]
        if fixed:
            relaxed_reqs = [
                SessionReq(r.req_id, r.role, r.qualification, r.duration,
                           r.earliest_start, r.latest_start, r.preferred_start,
                           None, r.after)
                for r in reqs]
            rel_ctx = [self._context(r, headcount, hard_occ, soft_occ, 0)
                       for r in relaxed_reqs]
            rel_candidates = self._combine(rel_ctx, top_n)
            if rel_candidates:
                out.append(Suggestion(
                    "RELAX_VENUE",
                    "放弃指定展厅、允许系统在同容量展厅间调配，可得到下列方案",
                    tuple(rel_candidates[:top_n])))
        return out
