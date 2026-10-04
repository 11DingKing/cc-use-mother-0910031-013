"""领域对象与 API JSON 之间的转换。时间字段接受 ISO-8601 字符串或 epoch 分钟。"""
from __future__ import annotations

from . import timeutil
from .models import (
    Candidate, Diagnosis, Group, PlannedAssignment, Reservation, Staff, Venue,
    Window,
)
from .service import ConfirmFailure, Refusal


def minute(value) -> int:
    if isinstance(value, bool):
        raise ValueError("时间值非法")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        return timeutil.to_minutes(value)
    raise ValueError(f"时间值非法：{value!r}")


def opt_minute(value):
    return minute(value) if value is not None else None


def windows(items) -> tuple[Window, ...]:
    out = []
    for w in items or []:
        out.append(Window(minute(w["start"]), minute(w["end"])))
    return tuple(out)


def staff_from_json(data: dict) -> Staff:
    return Staff(
        id=data["id"], name=data.get("name", data["id"]), role=data["role"],
        qualifications=tuple(data.get("qualifications", [])),
        availability=windows(data.get("availability")),
        travel=dict(data.get("travel", {})))


def venue_from_json(data: dict) -> Venue:
    return Venue(
        id=data["id"], name=data.get("name", data["id"]),
        capacity=int(data["capacity"]),
        availability=windows(data.get("availability")))


def group_from_json(data: dict) -> Group:
    return Group(id=data["id"], name=data.get("name", data["id"]),
                 headcount=int(data["headcount"]))


def session_from_json(data: dict) -> dict:
    return {
        "req_id": data["req_id"],
        "role": data["role"],
        "qualification": data.get("qualification", ""),
        "duration": int(data["duration"]),
        "earliest_start": minute(data["earliest_start"]),
        "latest_start": opt_minute(data.get("latest_start")),
        "preferred_start": opt_minute(data.get("preferred_start")),
        "venue_id": data.get("venue_id"),
        "after": [
            {"after_req_id": d["after_req_id"],
             "gap_minutes": int(d.get("gap_minutes", 0))}
            for d in data.get("after", [])],
    }


def assignment_from_json(data: dict) -> dict:
    return {
        "req_id": data["req_id"],
        "staff_id": data["staff_id"],
        "venue_id": data["venue_id"],
        "start": minute(data["start"]),
    }


def assignment_to_json(a: PlannedAssignment) -> dict:
    return {
        "req_id": a.req_id, "staff_id": a.staff_id, "venue_id": a.venue_id,
        "start": timeutil.to_iso(a.start), "end": timeutil.to_iso(a.end),
        "start_min": a.start, "end_min": a.end,
    }


def candidate_to_json(c: Candidate) -> dict:
    return {
        "assignments": [assignment_to_json(a) for a in c.assignments],
        "score": c.score,
        "contended": c.contended,
        "explanation": list(c.explanations),
    }


def diagnosis_to_json(d: Diagnosis) -> dict:
    return {
        "candidates": [candidate_to_json(c) for c in d.candidates],
        "blockers": [
            {
                "resource_type": b.resource_type,
                "resource_id": b.resource_id,
                "reason": b.reason,
                "message": b.message,
                "reservation_id": b.reservation_id,
                "start": timeutil.to_iso(b.start) if b.start is not None else None,
                "end": timeutil.to_iso(b.end) if b.end is not None else None,
            } for b in d.blockers],
        "suggestions": [
            {
                "kind": s.kind,
                "message": s.message,
                "candidates": [candidate_to_json(c) for c in s.candidates],
            } for s in d.suggestions],
    }


def reservation_to_json(r: Reservation, headcount: int | None = None) -> dict:
    return {
        "id": r.id,
        "group_id": r.group_id,
        "headcount": headcount,
        "status": r.status,
        "version": r.version,
        "created_at": timeutil.to_iso(r.created_at),
        "expires_at": timeutil.to_iso(r.expires_at) if r.expires_at is not None else None,
        "confirmed_at": timeutil.to_iso(r.confirmed_at) if r.confirmed_at is not None else None,
        "release_reason": r.release_reason,
        "assignments": [assignment_to_json(a) for a in r.assignments],
    }


def refusal_to_json(x: Refusal) -> dict:
    return {
        "resource_type": x.resource_type,
        "resource_id": x.resource_id,
        "message": x.message,
        "reservation_id": x.reservation_id,
    }


def failure_to_json(f: ConfirmFailure) -> dict:
    return {
        "code": f.code,
        "message": f.message,
        "refusals": [refusal_to_json(x) for x in f.refusals],
        "diagnosis": diagnosis_to_json(f.diagnosis),
    }
