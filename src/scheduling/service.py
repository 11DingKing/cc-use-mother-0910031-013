"""排班服务层：候选暂占、原子确认、取消释放、过期回收与并发一致性。"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, replace
from typing import Optional

from .catalog import Catalog
from .clock import Clock, SystemClock
from .models import (
    CANCELLED, CONFIRMED, EXPIRED, HELD, SUPERSEDED, Candidate,
    Dependency, Diagnosis, PlannedAssignment, Reservation, SessionReq,
)
from .occupancy import Occupancy, bookings_from
from .planner import Planner
from .storage import Storage

DEFAULT_TTL_MINUTES = 10


class SchedulingError(Exception):
    def __init__(self, code: str, message: str, status: int = 400,
                 diagnosis: Optional[Diagnosis] = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.diagnosis = diagnosis


@dataclass
class Refusal:
    resource_type: str
    resource_id: str
    message: str
    reservation_id: Optional[str] = None


@dataclass
class ConfirmFailure:
    code: str
    message: str
    refusals: list[Refusal]
    diagnosis: Diagnosis


class SchedulingService:
    def __init__(self, storage: Storage, clock: Optional[Clock] = None,
                 default_ttl: int = DEFAULT_TTL_MINUTES) -> None:
        self.storage = storage
        self.clock = clock or SystemClock()
        self.default_ttl = default_ttl
        self._lock = threading.RLock()
        self.catalog = Catalog()
        for s in storage.load_staff():
            self.catalog.upsert_staff(s)
        for v in storage.load_venues():
            self.catalog.upsert_venue(v)
        for g in storage.load_groups():
            self.catalog.upsert_group(g)
        self.planner = Planner(self.catalog)
        self.reap_expired_holds()  # 重启即回收

    # ==================================================================
    # 主数据维护
    # ==================================================================
    def upsert_staff(self, staff) -> None:
        with self._lock:
            self.storage.begin()
            try:
                self.storage.save_staff(staff)
                self.storage.commit()
            except Exception:
                self.storage.rollback()
                raise
            self.catalog.upsert_staff(staff)

    def upsert_venue(self, venue) -> None:
        with self._lock:
            self.storage.begin()
            try:
                self.storage.save_venue(venue)
                self.storage.commit()
            except Exception:
                self.storage.rollback()
                raise
            self.catalog.upsert_venue(venue)

    def upsert_group(self, group) -> None:
        """新增团体（人数变更请用 change_headcount）。"""
        with self._lock:
            self.storage.begin()
            try:
                self.storage.save_group(group)
                self.storage.commit()
            except Exception:
                self.storage.rollback()
                raise
            self.catalog.upsert_group(group)

    def change_headcount(self, group_id: str, headcount: int) -> dict:
        """学校人数变化：

        - 已排定（confirmed）场次容量不足：拒绝变更并列出受影响场次；
        - 待确认（held）场次容量不足：联动失效并释放全部资源；
        - 其余：正常生效。
        """
        if headcount <= 0:
            raise SchedulingError("invalid_headcount", "团体人数必须为正")
        with self._lock:
            if not self.catalog.has_group(group_id):
                raise SchedulingError("group_not_found", f"团体 {group_id} 不存在", 404)
            self._expire_locked()
            actives = self._active_reservations_locked()
            mine = [r for r in actives if r.group_id == group_id]
            blocking = []
            for r in mine:
                for a in r.assignments:
                    cap = self.catalog.venue(a.venue_id).capacity
                    if cap < headcount:
                        blocking.append((r, a, cap))
            confirmed_blocks = [x for x in blocking if x[0].status == CONFIRMED]
            if confirmed_blocks:
                detail = "; ".join(
                    f"排期 {r.id} 场次 {a.req_id} 使用 {a.venue_id}"
                    f"（容量 {cap}）" for r, a, cap in confirmed_blocks)
                raise SchedulingError(
                    "confirmed_capacity_break",
                    f"人数增至 {headcount} 将使已排定场次超容：{detail}；"
                    "请先取消或改期这些场次后再调整人数", 409)
            self.storage.begin()
            try:
                group = self.catalog.group(group_id)
                updated = replace(group, headcount=headcount)
                self.storage.save_group(updated)
                invalidated: list[str] = []
                for r, a, cap in blocking:
                    if r.status == HELD and r.id not in invalidated:
                        now = self.clock.now_minutes()
                        gone = replace(r, status=EXPIRED, expires_at=now,
                                       release_reason="group_headcount_changed")
                        self._save_transition(gone)
                        invalidated.append(r.id)
                self.storage.commit()
            except Exception:
                self.storage.rollback()
                raise
            self.catalog.upsert_group(updated)
            return {"headcount": headcount, "invalidated_holds": invalidated}

    # ==================================================================
    # 候选方案
    # ==================================================================
    def plan(self, group_id: str, sessions: list[dict],
             ttl_minutes: Optional[int] = None) -> tuple[Diagnosis, int]:
        with self._lock:
            group = self._get_group(group_id)
            self._expire_locked()
            reqs = self._parse_sessions(sessions)
            try:
                self.planner.assert_dependency_acyclic(reqs)
            except ValueError as exc:
                raise SchedulingError("bad_dependency", str(exc)) from exc
            hard, soft, expiries = self._occupancy_locked()
            diagnosis = self.planner.diagnose(
                reqs, group.headcount, hard, soft, expiries)
            return diagnosis, (ttl_minutes or self.default_ttl)

    # ==================================================================
    # 暂占
    # ==================================================================
    def hold(self, group_id: str, sessions: list[dict],
             assignments: list[dict], ttl_minutes: Optional[int] = None) -> Reservation:
        with self._lock:
            group = self._get_group(group_id)
            self._expire_locked()
            reqs = self._parse_sessions(sessions)
            try:
                self.planner.assert_dependency_acyclic(reqs)
            except ValueError as exc:
                raise SchedulingError("bad_dependency", str(exc)) from exc
            chosen = self._parse_assignments(assignments, reqs)
            hard, soft, expiries = self._occupancy_locked()
            diagnosis = self.planner.diagnose(
                reqs, group.headcount, hard, soft, expiries)
            if not diagnosis.candidates:
                raise SchedulingError(
                    "no_feasible_candidate",
                    "所选场次当前不存在可行方案，详见 blockers 与 suggestions",
                    409, diagnosis)
            if not self._matches_candidate(chosen, diagnosis.candidates):
                raise SchedulingError(
                    "stale_candidate",
                    "提交的分配不在当前候选方案中，可能资源状态已变化，请重新获取候选",
                    409, diagnosis)
            now = self.clock.now_minutes()
            ttl = ttl_minutes or self.default_ttl
            rid = "RSV-" + uuid.uuid4().hex[:12]
            reservation = Reservation(
                id=rid, group_id=group_id, status=HELD, created_at=now,
                expires_at=now + ttl, confirmed_at=None, release_reason=None,
                assignments=tuple(chosen))
            self.storage.begin()
            try:
                self.storage.insert_reservation(
                    reservation, _request_json(reqs, group.headcount, ttl))
                self.storage.commit()
            except Exception:
                self.storage.rollback()
                raise
            return reservation

    # ==================================================================
    # 原子确认
    # ==================================================================
    def confirm(self, reservation_id: str,
                expected_version: Optional[int] = None):
        with self._lock:
            self._expire_locked()
            self.storage.begin()
            try:
                row = self.storage.get_reservation_row(reservation_id)
                if row is None:
                    raise SchedulingError("reservation_not_found",
                                          f"排期 {reservation_id} 不存在", 404)
                if expected_version is not None and row["version"] != expected_version:
                    raise SchedulingError(
                        "version_conflict",
                        f"排期已被其他操作更新（版本 {row['version']}），"
                        "请刷新后重试", 409)
                reservation = self.storage.row_to_reservation(row)
                reqs = self._reqs_from_reservation(reservation)
                if reservation.status == CONFIRMED:
                    self.storage.commit()
                    return reservation
                if reservation.status == SUPERSEDED:
                    # 已被并发获胜者作废：直接基于最新占用给出替代方案
                    self.storage.commit()
                    hard, soft, expiries = self._occupancy_locked()
                    headcount = self.catalog.group(reservation.group_id).headcount
                    diagnosis = self.planner.diagnose(
                        reqs, headcount, hard, soft, expiries)
                    return ConfirmFailure(
                        "superseded",
                        "该暂占已在并发确认中被其他排期原子作废，请改选下列方案后重新暂占",
                        [Refusal("reservation", reservation.id,
                                 f"被排期 {reservation.release_reason} 抢占")],
                        diagnosis)
                if reservation.status != HELD:
                    raise SchedulingError(
                        "not_held",
                        f"排期当前状态为 {reservation.status}，无法确认", 409)

                now = self.clock.now_minutes()
                # 硬复检只针对「已确认」占用与主数据（资格/容量/可用时段）。
                # 其他待确认暂占不构成拒绝理由，而是在本单获胜后一并原子作废。
                refusals = self._revalidate_locked(reservation)
                if refusals:
                    # 主数据变化导致原方案不成立：原子释放本暂占，未锁定任何资源，
                    # 并基于最新占用重新诊断，给出可解释替代方案。
                    gone = replace(reservation, status=EXPIRED,
                                   expires_at=now,
                                   release_reason="resources_refused")
                    self._save_transition(gone)
                    self.storage.commit()
                    hard, soft, expiries = self._occupancy_locked()
                    headcount = self.catalog.group(reservation.group_id).headcount
                    diagnosis = self.planner.diagnose(
                        reqs, headcount, hard, soft, expiries)
                    return ConfirmFailure(
                        "resources_refused",
                        "部分资源已不可用，确认被整体拒绝，原暂占已释放、未锁定任何资源",
                        refusals, diagnosis)

                # 获胜：锁定本单，并把与本单冲突的其他待确认暂占在同一事务内作废，
                # 保证「全部资源同时锁定 / 同时释放」。
                confirmed = replace(reservation, status=CONFIRMED,
                                    confirmed_at=now, expires_at=None)
                confirmed = self._save_transition(confirmed)
                self._supersede_conflicting_locks(confirmed)
                self.storage.commit()
                return confirmed
            except SchedulingError:
                self.storage.rollback()
                raise
            except Exception:
                self.storage.rollback()
                raise

    # ==================================================================
    # 取消与释放
    # ==================================================================
    def cancel(self, reservation_id: str, reason: str = "user_cancelled") -> Reservation:
        with self._lock:
            self.storage.begin()
            try:
                row = self.storage.get_reservation_row(reservation_id)
                if row is None:
                    raise SchedulingError("reservation_not_found",
                                          f"排期 {reservation_id} 不存在", 404)
                reservation = self.storage.row_to_reservation(row)
                if reservation.status not in (HELD, CONFIRMED):
                    raise SchedulingError(
                        "not_active",
                        f"排期当前状态为 {reservation.status}，无法取消", 409)
                cancelled = replace(reservation, status=CANCELLED,
                                    expires_at=self.clock.now_minutes(),
                                    release_reason=reason)
                cancelled = self._save_transition(cancelled)
                self.storage.commit()
                return cancelled
            except SchedulingError:
                self.storage.rollback()
                raise

    def get(self, reservation_id: str) -> Reservation:
        with self._lock:
            self._expire_locked()
            row = self.storage.get_reservation_row(reservation_id)
            if row is None:
                raise SchedulingError("reservation_not_found",
                                      f"排期 {reservation_id} 不存在", 404)
            return self.storage.row_to_reservation(row)

    # ==================================================================
    # 过期回收
    # ==================================================================
    def reap_expired_holds(self) -> list[str]:
        with self._lock:
            return self._expire_locked()

    def _expire_locked(self) -> list[str]:
        now = self.clock.now_minutes()
        rows = self.storage.list_reservation_rows()
        stale = [r for r in (self.storage.row_to_reservation(row) for row in rows)
                 if r.status == HELD and r.expires_at is not None
                 and r.expires_at <= now]
        if not stale:
            return []
        self.storage.begin()
        try:
            for r in stale:
                self._save_transition(
                    replace(r, status=EXPIRED, release_reason="hold_expired"))
            self.storage.commit()
        except Exception:
            self.storage.rollback()
            raise
        return [r.id for r in stale]

    # ==================================================================
    # 内部工具
    # ==================================================================
    def _supersede_conflicting_locks(self, winner: Reservation) -> list[str]:
        """获胜确认的同一事务内，作废与获胜排期资源冲突的其他待确认暂占。

        不冲突的暂占（不同人员/场地/时段）保持有效。
        """
        losers: list[str] = []
        winner_bookings = bookings_from([winner])
        winner_occ = Occupancy(winner_bookings, (CONFIRMED,))
        now = self.clock.now_minutes()
        for other in self._active_reservations_locked():
            if other.id == winner.id or other.status != HELD:
                continue
            clashes = False
            for a in other.assignments:
                staff = self.catalog.staff(a.staff_id)
                if (winner_occ.venue_clash(a.venue_id, a.start, a.end) is not None
                        or winner_occ.staff_clash(
                            self.catalog, a.staff_id, a.venue_id,
                            a.start, a.end) is not None):
                    clashes = True
                    break
            if clashes:
                gone = replace(other, status=SUPERSEDED, expires_at=now,
                               release_reason=winner.id)
                self._save_transition(gone)
                losers.append(other.id)
        return losers

    def _save_transition(self, reservation: Reservation) -> Reservation:
        """在已开启的事务内写入状态流转，版本号 +1 并返回新对象。"""
        bumped = replace(reservation, version=reservation.version + 1)
        self.storage.update_reservation(bumped, bump_version=False)
        return bumped

    def _get_group(self, group_id: str):
        if not self.catalog.has_group(group_id):
            raise SchedulingError("group_not_found", f"团体 {group_id} 不存在", 404)
        return self.catalog.group(group_id)

    def _active_reservations_locked(self) -> list[Reservation]:
        return self.storage.active_reservations()

    def _occupancy_locked(self):
        """返回 (硬占用=已确认, 软占用=待确认暂占, 暂占过期时间表)。"""
        actives = self._active_reservations_locked()
        hard = Occupancy(bookings_from(actives), (CONFIRMED,))
        soft = Occupancy(bookings_from(actives), (HELD,))
        expiries = {r.id: r.expires_at for r in actives
                    if r.status == HELD and r.expires_at is not None}
        return hard, soft, expiries

    def _revalidate_locked(self, reservation: Reservation) -> list[Refusal]:
        """确认前硬复检：容量、可用时段，以及与「已确认」占用的冲突（含移动缓冲）。

        与其他待确认暂占的竞争不在此拒绝，而由确认获胜方原子作废输家暂占。
        """
        headcount = self.catalog.group(reservation.group_id).headcount
        req_by_id = {r.req_id: r for r in
                     self._parse_sessions(list(reservation.request_sessions))}
        others = [r for r in self._active_reservations_locked()
                  if r.id != reservation.id and r.status == CONFIRMED]
        occ = Occupancy(bookings_from(others), (CONFIRMED,))
        refusals: list[Refusal] = []
        for a in reservation.assignments:
            staff = self.catalog.staff(a.staff_id)
            venue = self.catalog.venue(a.venue_id)
            req = req_by_id.get(a.req_id)
            if req is not None and (staff.role != req.role
                                    or req.qualification not in staff.qualifications):
                refusals.append(Refusal(
                    "staff", staff.id,
                    f"人员 {staff.name} 已不再具备场次 {a.req_id} 要求的"
                    f"{req.role}/{req.qualification} 资格"))
                continue
            if venue.capacity < headcount:
                refusals.append(Refusal(
                    "venue", venue.id,
                    f"展厅 {venue.name} 容量 {venue.capacity} 不足团体 "
                    f"{headcount} 人"))
                continue
            if not self.catalog.within_any(staff.availability, a.start, a.end):
                refusals.append(Refusal(
                    "staff", staff.id,
                    f"人员 {staff.name} 在该时段已不可用（排班调整）"))
            if not self.catalog.within_any(venue.availability, a.start, a.end):
                refusals.append(Refusal(
                    "venue", venue.id,
                    f"展厅 {venue.name} 在该时段已不开放"))
            vb = occ.venue_clash(venue.id, a.start, a.end)
            if vb is not None:
                refusals.append(Refusal(
                    "venue", venue.id,
                    f"展厅已被排期 {vb.reservation_id} 的场次 {vb.req_id} 占用",
                    vb.reservation_id))
            sb = occ.staff_clash(self.catalog, staff.id, venue.id, a.start, a.end)
            if sb is not None:
                gap = self.catalog.travel_minutes(staff, venue.id, sb.venue_id)
                buffer_note = f"，跨厅移动缓冲 {gap} 分钟" if sb.venue_id != venue.id else ""
                refusals.append(Refusal(
                    "staff", staff.id,
                    f"人员已被排期 {sb.reservation_id} 的场次 {sb.req_id} 占用"
                    f"{buffer_note}", sb.reservation_id))
        return refusals

    @staticmethod
    def _matches_candidate(chosen: list[PlannedAssignment],
                           candidates: tuple[Candidate, ...]) -> bool:
        want = tuple(sorted((a.staff_id, a.venue_id, a.start) for a in chosen))
        return any(c.signature == want for c in candidates)

    @staticmethod
    def _parse_sessions(sessions: list[dict]) -> list[SessionReq]:
        if not sessions:
            raise SchedulingError("empty_sessions", "至少需要一个场次")
        reqs: list[SessionReq] = []
        seen: set[str] = set()
        for raw in sessions:
            rid = raw.get("req_id")
            if not rid or rid in seen:
                raise SchedulingError("bad_req_id", f"场次编号缺失或重复：{rid!r}")
            seen.add(rid)
            role = raw.get("role")
            if role not in ("guide", "lecturer"):
                raise SchedulingError("bad_role", f"场次 {rid} 角色非法：{role!r}")
            duration = int(raw.get("duration", 0))
            if duration <= 0:
                raise SchedulingError("bad_duration", f"场次 {rid} 时长必须为正")
            after = tuple(
                Dependency(d["after_req_id"], int(d.get("gap_minutes", 0)))
                for d in raw.get("after", []))
            reqs.append(SessionReq(
                req_id=rid, role=role,
                qualification=raw.get("qualification", ""),
                duration=duration,
                earliest_start=int(raw["earliest_start"]),
                latest_start=(int(raw["latest_start"])
                              if raw.get("latest_start") is not None else None),
                preferred_start=(int(raw["preferred_start"])
                                 if raw.get("preferred_start") is not None else None),
                venue_id=raw.get("venue_id"), after=after))
        ids = {r.req_id for r in reqs}
        for r in reqs:
            for dep in r.after:
                if dep.after_req_id not in ids:
                    raise SchedulingError(
                        "bad_dependency",
                        f"场次 {r.req_id} 依赖的 {dep.after_req_id} 不在本单中")
        return reqs

    @staticmethod
    def _parse_assignments(items: list[dict],
                           reqs: list[SessionReq]) -> list[PlannedAssignment]:
        by_id = {r.req_id: r for r in reqs}
        if len(items) != len(reqs):
            raise SchedulingError(
                "assignment_mismatch",
                f"分配数 {len(items)} 与场次数 {len(reqs)} 不一致")
        out: list[PlannedAssignment] = []
        seen: set[str] = set()
        for raw in items:
            rid = raw.get("req_id")
            if rid not in by_id or rid in seen:
                raise SchedulingError("assignment_mismatch", f"分配场次非法：{rid!r}")
            seen.add(rid)
            start = int(raw["start"])
            out.append(PlannedAssignment(
                rid, raw["staff_id"], raw["venue_id"], start,
                start + by_id[rid].duration))
        return [next(a for a in out if a.req_id == r.req_id) for r in reqs]

    def _reqs_from_reservation(self, r: Reservation) -> list[SessionReq]:
        return self._parse_sessions(list(r.request_sessions))


def _request_json(reqs: list[SessionReq], headcount: int, ttl: int) -> str:
    import json
    return json.dumps({
        "headcount": headcount,
        "ttl_minutes": ttl,
        "sessions": [
            {
                "req_id": r.req_id, "role": r.role,
                "qualification": r.qualification, "duration": r.duration,
                "earliest_start": r.earliest_start,
                "latest_start": r.latest_start,
                "preferred_start": r.preferred_start,
                "venue_id": r.venue_id,
                "after": [{"after_req_id": d.after_req_id,
                           "gap_minutes": d.gap_minutes} for d in r.after],
            } for r in reqs],
    }, ensure_ascii=False)
