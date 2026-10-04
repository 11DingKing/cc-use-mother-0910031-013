"""SQLite 持久化：目录主数据与暂占/排定记录。

一个连接 + 写事务（BEGIN IMMEDIATE）配合服务层互斥锁，保证并发确认串行化、
确认前在同一事务内完成最新占用复检，实现原子锁定。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from .models import (
    CONFIRMED, HELD, Group, PlannedAssignment, Reservation, Staff, Venue, Window,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS staff (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS venues (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    data_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS groups_table (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    headcount INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reservations (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    confirmed_at INTEGER,
    release_reason TEXT,
    request_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    reservation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    req_id TEXT NOT NULL,
    staff_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    start_min INTEGER NOT NULL,
    end_min INTEGER NOT NULL,
    PRIMARY KEY (reservation_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_assignments_staff ON assignments(staff_id);
CREATE INDEX IF NOT EXISTS idx_assignments_venue ON assignments(venue_id);
CREATE INDEX IF NOT EXISTS idx_reservations_status ON reservations(status);
"""


class Storage:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(f"PRAGMA busy_timeout = 10000")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    # ---- 目录 ----------------------------------------------------------
    def save_staff(self, s: Staff) -> None:
        self.conn.execute(
            "INSERT INTO staff(id, name, role, data_json) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, role=excluded.role, "
            "data_json=excluded.data_json",
            (s.id, s.name, s.role, json.dumps(_staff_json(s), ensure_ascii=False)))

    def save_venue(self, v: Venue) -> None:
        self.conn.execute(
            "INSERT INTO venues(id, name, capacity, data_json) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "capacity=excluded.capacity, data_json=excluded.data_json",
            (v.id, v.name, v.capacity,
             json.dumps({"availability": [[w.start, w.end] for w in v.availability]},
                        ensure_ascii=False)))

    def save_group(self, g: Group) -> None:
        self.conn.execute(
            "INSERT INTO groups_table(id, name, headcount) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "headcount=excluded.headcount",
            (g.id, g.name, g.headcount))

    def load_staff(self) -> list[Staff]:
        rows = self.conn.execute("SELECT data_json FROM staff").fetchall()
        return [_staff_from_json(json.loads(r["data_json"])) for r in rows]

    def load_venues(self) -> list[Venue]:
        rows = self.conn.execute("SELECT id, name, capacity, data_json FROM venues").fetchall()
        out = []
        for r in rows:
            data = json.loads(r["data_json"])
            out.append(Venue(r["id"], r["name"], r["capacity"],
                             tuple(Window(a, b) for a, b in data["availability"])))
        return out

    def load_groups(self) -> list[Group]:
        rows = self.conn.execute("SELECT id, name, headcount FROM groups_table").fetchall()
        return [Group(r["id"], r["name"], r["headcount"]) for r in rows]

    # ---- 排期单 --------------------------------------------------------
    def insert_reservation(self, r: Reservation, request_json: str) -> None:
        self.conn.execute(
            "INSERT INTO reservations(id, group_id, status, version, created_at, "
            "expires_at, confirmed_at, release_reason, request_json) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (r.id, r.group_id, r.status, 1, r.created_at, r.expires_at,
             r.confirmed_at, r.release_reason, request_json))
        self._replace_assignments(r)

    def update_reservation(self, r: Reservation, bump_version: bool = True) -> None:
        version = r.version + 1 if bump_version else r.version
        self.conn.execute(
            "UPDATE reservations SET status=?, version=?, expires_at=?, "
            "confirmed_at=?, release_reason=? WHERE id=?",
            (r.status, version, r.expires_at, r.confirmed_at,
             r.release_reason, r.id))
        self._replace_assignments(r)

    def _replace_assignments(self, r: Reservation) -> None:
        self.conn.execute("DELETE FROM assignments WHERE reservation_id=?", (r.id,))
        self.conn.executemany(
            "INSERT INTO assignments(reservation_id, seq, req_id, staff_id, "
            "venue_id, start_min, end_min) VALUES(?,?,?,?,?,?,?)",
            [(r.id, i, a.req_id, a.staff_id, a.venue_id, a.start, a.end)
             for i, a in enumerate(r.assignments)])

    def get_reservation_row(self, rid: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()

    def list_reservation_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reservations ORDER BY created_at, id").fetchall()

    def assignments_of(self, rid: str) -> list[PlannedAssignment]:
        rows = self.conn.execute(
            "SELECT req_id, staff_id, venue_id, start_min, end_min "
            "FROM assignments WHERE reservation_id=? ORDER BY seq", (rid,)).fetchall()
        return [PlannedAssignment(r["req_id"], r["staff_id"], r["venue_id"],
                                  r["start_min"], r["end_min"]) for r in rows]

    def row_to_reservation(self, row: sqlite3.Row) -> Reservation:
        return Reservation(
            id=row["id"], group_id=row["group_id"], status=row["status"],
            created_at=row["created_at"], expires_at=row["expires_at"],
            confirmed_at=row["confirmed_at"], release_reason=row["release_reason"],
            assignments=tuple(self.assignments_of(row["id"])),
            request_sessions=tuple(json.loads(row["request_json"])["sessions"]),
            version=row["version"])

    def active_reservations(self) -> list[Reservation]:
        rows = self.conn.execute(
            "SELECT * FROM reservations WHERE status IN (?, ?) ORDER BY id",
            (HELD, CONFIRMED)).fetchall()
        return [self.row_to_reservation(r) for r in rows]


def _staff_json(s: Staff) -> dict:
    return {
        "id": s.id, "name": s.name, "role": s.role,
        "qualifications": list(s.qualifications),
        "availability": [[w.start, w.end] for w in s.availability],
        "travel": s.travel,
    }


def _staff_from_json(data: dict) -> Staff:
    return Staff(
        data["id"], data["name"], data["role"],
        tuple(data["qualifications"]),
        tuple(Window(a, b) for a, b in data["availability"]),
        dict(data.get("travel", {})))
