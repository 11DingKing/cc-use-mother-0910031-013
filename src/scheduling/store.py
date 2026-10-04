"""SQLite 持久层。

设计要点：

- 资源（讲解员/讲师/场地）、学校团体、候选方案（暂占）、暂占明细 holds、
  已确认排期 entries、资源拒绝时段 blocks 分表存放；
- 所有冲突检测都走同一组区间重叠查询（半开区间 + 缓冲在调用侧展开）；
- 写事务由 :class:`~scheduling.service.SchedulingService` 的全局锁串行化，
  连接以 ``check_same_thread=False`` 打开，配合 ``BEGIN IMMEDIATE`` 保证并发确认安全。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,
    name              TEXT NOT NULL,
    capacity          INTEGER NOT NULL DEFAULT 1,
    travel_buffer_min INTEGER NOT NULL DEFAULT 0,
    active            INTEGER NOT NULL DEFAULT 1,
    version           INTEGER NOT NULL DEFAULT 1,
    qualifs           TEXT NOT NULL DEFAULT '[]',
    availability      TEXT NOT NULL DEFAULT '[]',
    created_ts        REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS groups (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    size              INTEGER NOT NULL,
    contact           TEXT NOT NULL DEFAULT '',
    active            INTEGER NOT NULL DEFAULT 1,
    travel_buffer_min INTEGER NOT NULL DEFAULT 0,
    version           INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS candidates (
    id         TEXT PRIMARY KEY,
    status     TEXT NOT NULL,            -- pending/confirmed/cancelled/expired
    created_ts REAL NOT NULL,
    expires_ts REAL NOT NULL,
    version    INTEGER NOT NULL DEFAULT 1,
    note       TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    resource_id  TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    start_min    INTEGER NOT NULL,
    end_min      INTEGER NOT NULL,
    weight       INTEGER NOT NULL DEFAULT 1,
    UNIQUE(candidate_id, resource_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_holds_resource ON holds(resource_id, start_min, end_min);
CREATE TABLE IF NOT EXISTS entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id  TEXT NOT NULL,
    session_id    TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL DEFAULT '',
    start_min     INTEGER NOT NULL,
    end_min       INTEGER NOT NULL,
    group_id      TEXT,
    group_size    INTEGER NOT NULL DEFAULT 0,
    venue_id      TEXT,
    confirmed_ts  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_time ON entries(start_min, end_min);
CREATE TABLE IF NOT EXISTS entry_resources (
    entry_id    INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    resource_id TEXT NOT NULL,
    role_kind   TEXT NOT NULL,
    weight      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (entry_id, resource_id)
);
CREATE INDEX IF NOT EXISTS idx_er_resource ON entry_resources(resource_id);
CREATE TABLE IF NOT EXISTS blocks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id TEXT NOT NULL,
    start_min   INTEGER NOT NULL,
    end_min     INTEGER NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    created_ts  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_blocks_resource ON blocks(resource_id, start_min, end_min);
"""


class SQLiteStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """对早期库补充新增列（重启复用同一数据库文件时）。"""
        def columns(table: str) -> set[str]:
            return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

        if "weight" not in columns("holds"):
            self.conn.execute("ALTER TABLE holds ADD COLUMN weight INTEGER NOT NULL DEFAULT 1")
        if "weight" not in columns("entry_resources"):
            self.conn.execute("ALTER TABLE entry_resources ADD COLUMN weight INTEGER NOT NULL DEFAULT 1")
        gcols = columns("groups")
        if "travel_buffer_min" not in gcols:
            self.conn.execute(
                "ALTER TABLE groups ADD COLUMN travel_buffer_min INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        self.conn.close()

    # ---- 事务原语 -------------------------------------------------------
    def begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        self.conn.execute("ROLLBACK")

    # ---- 资源 -----------------------------------------------------------
    def upsert_resource(self, r: dict, ts: float) -> None:
        self.conn.execute(
            """
            INSERT INTO resources(id, kind, name, capacity, travel_buffer_min, active,
                                  version, qualifs, availability, created_ts)
            VALUES(:id,:kind,:name,:capacity,:buf,:active,1,:qualifs,:avail,:ts)
            ON CONFLICT(id) DO UPDATE SET
                kind=excluded.kind, name=excluded.name, capacity=excluded.capacity,
                travel_buffer_min=excluded.travel_buffer_min, active=excluded.active,
                version=resources.version+1, qualifs=excluded.qualifs,
                availability=excluded.availability
            """,
            {
                "id": r["id"], "kind": r["kind"], "name": r["name"],
                "capacity": int(r["capacity"]), "buf": int(r["travel_buffer_min"]),
                "active": 1 if r.get("active", True) else 0,
                "qualifs": json.dumps(sorted(r["qualifications"]), ensure_ascii=False),
                "avail": json.dumps(
                    [{"s": w["start_min"], "e": w["end_min"]} for w in r["availability"]],
                    ensure_ascii=False),
                "ts": ts,
            },
        )

    def get_resource(self, rid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM resources WHERE id=?", (rid,)).fetchone()

    def list_resources(self, kind: str | None = None, active_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM resources WHERE 1=1"
        args: list[object] = []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if active_only:
            sql += " AND active=1"
        sql += " ORDER BY id"
        return list(self.conn.execute(sql, args))

    def set_resource_active(self, rid: str, active: bool) -> None:
        self.conn.execute(
            "UPDATE resources SET active=?, version=version+1 WHERE id=?",
            (1 if active else 0, rid))

    # ---- 学校团体 --------------------------------------------------------
    def upsert_group(self, g: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO groups(id,name,size,contact,active,travel_buffer_min,version)
            VALUES(:id,:name,:size,:contact,:active,:buf,1)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, size=excluded.size, contact=excluded.contact,
                active=excluded.active, travel_buffer_min=excluded.travel_buffer_min,
                version=groups.version+1
            """,
            {"id": g["id"], "name": g["name"], "size": int(g["size"]),
             "contact": g.get("contact", ""), "active": 1 if g.get("active", True) else 0,
             "buf": int(g.get("travel_buffer_min", 0))},
        )

    def get_group(self, gid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()

    def list_groups(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM groups ORDER BY id"))

    def update_group_size(self, gid: str, size: int) -> None:
        self.conn.execute(
            "UPDATE groups SET size=?, version=version+1 WHERE id=?", (int(size), gid))

    # ---- 拒绝时段 --------------------------------------------------------
    def add_block(self, rid: str, start_min: int, end_min: int, reason: str, ts: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO blocks(resource_id,start_min,end_min,reason,created_ts) VALUES(?,?,?,?,?)",
            (rid, start_min, end_min, reason, ts))
        return int(cur.lastrowid)

    def list_blocks(self, rid: str | None = None) -> list[sqlite3.Row]:
        if rid:
            return list(self.conn.execute(
                "SELECT * FROM blocks WHERE resource_id=? ORDER BY start_min", (rid,)))
        return list(self.conn.execute("SELECT * FROM blocks ORDER BY start_min"))

    # ---- 候选方案 / 暂占 --------------------------------------------------
    def insert_candidate(self, cid: str, payload: dict, created_ts: float,
                         expires_ts: float, note: str) -> None:
        self.conn.execute(
            "INSERT INTO candidates(id,status,created_ts,expires_ts,version,note,payload)"
            " VALUES(?, 'pending', ?, ?, 1, ?, ?)",
            (cid, created_ts, expires_ts, note, json.dumps(payload, ensure_ascii=False)))

    def get_candidate(self, cid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()

    def set_candidate_status(self, cid: str, status: str) -> None:
        self.conn.execute("UPDATE candidates SET status=? WHERE id=?", (status, cid))

    def add_holds(self, cid: str, resource_id: str,
                  sessions: list[tuple[str, int, int, int]]) -> None:
        self.conn.executemany(
            "INSERT INTO holds(candidate_id,resource_id,session_id,start_min,end_min,weight)"
            " VALUES(?,?,?,?,?,?)",
            [(cid, resource_id, sid, s, e, w) for sid, s, e, w in sessions])

    def holds_for(self, cid: str) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM holds WHERE candidate_id=?", (cid,)))

    def release_holds(self, cid: str) -> None:
        self.conn.execute("DELETE FROM holds WHERE candidate_id=?", (cid,))

    def list_expired_pending(self, now_ts: float) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT id FROM candidates WHERE status='pending' AND expires_ts<=?", (now_ts,))]

    # ---- 已确认排期 ------------------------------------------------------
    def get_booking_by_session(self, session_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM entries WHERE session_id=?", (session_id,)).fetchone()

    def insert_entry(self, candidate_id: str, session_id: str, title: str,
                     start_min: int, end_min: int, group_id: str | None, group_size: int,
                     venue_id: str | None, confirmed_ts: float) -> int:
        cur = self.conn.execute(
            """INSERT INTO entries(candidate_id,session_id,title,start_min,end_min,
                                   group_id,group_size,venue_id,confirmed_ts)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (candidate_id, session_id, title, start_min, end_min, group_id,
             group_size, venue_id, confirmed_ts))
        return int(cur.lastrowid)

    def add_entry_resource(self, entry_id: int, resource_id: str, role_kind: str,
                           weight: int = 1) -> None:
        self.conn.execute(
            "INSERT INTO entry_resources(entry_id,resource_id,role_kind,weight)"
            " VALUES(?,?,?,?)",
            (entry_id, resource_id, role_kind, weight))

    def entries_for_candidate(self, candidate_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM entries WHERE candidate_id=? ORDER BY start_min", (candidate_id,)))

    def entry_resources(self, entry_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM entry_resources WHERE entry_id=?", (entry_id,)))

    def delete_booking(self, candidate_id: str) -> int:
        rows = self.entries_for_candidate(candidate_id)
        self.conn.execute("DELETE FROM entries WHERE candidate_id=?", (candidate_id,))
        return len(rows)

    # ---- 拒绝时段重叠查询 --------------------------------------------------
    def find_block_overlaps(self, resource_id: str, start_min: int, end_min: int
                            ) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM blocks WHERE resource_id=? AND start_min < ? AND ? < end_min",
            (resource_id, end_min, start_min)))

    # ---- 查询 -------------------------------------------------------------
    def list_schedule(self, resource_id: str | None = None) -> list[sqlite3.Row]:
        if resource_id:
            return list(self.conn.execute(
                """SELECT DISTINCT e.* FROM entries e
                   JOIN entry_resources er ON er.entry_id=e.id
                   WHERE er.resource_id=? ORDER BY e.start_min""", (resource_id,)))
        return list(self.conn.execute("SELECT * FROM entries ORDER BY start_min"))

    def stats(self) -> dict:
        def count(table: str, where: str = "") -> int:
            return int(self.conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0])
        return {
            "resources": count("resources"),
            "groups": count("groups"),
            "candidates_pending": count("candidates", "WHERE status='pending'"),
            "candidates_confirmed": count("candidates", "WHERE status='confirmed'"),
            "candidates_expired": count("candidates", "WHERE status='expired'"),
            "candidates_cancelled": count("candidates", "WHERE status='cancelled'"),
            "holds": count("holds"),
            "entries": count("entries"),
            "blocks": count("blocks"),
        }
