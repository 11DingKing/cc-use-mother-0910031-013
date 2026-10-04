"""HTTP API（仅标准库）。线程模型：ThreadingHTTPServer + 服务层互斥锁。"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import serializers as sz
from .service import ConfirmFailure, SchedulingError, SchedulingService

Json = dict


class ApiHandler(BaseHTTPRequestHandler):
    service: SchedulingService = None  # 由 build_server 注入到子类

    # ---- 通用 ----------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # 静默默认日志
        pass

    def _read_json(self) -> Json:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise SchedulingError("bad_json", f"请求体不是合法 JSON：{exc}", 400)
        if not isinstance(data, dict):
            raise SchedulingError("bad_json", "请求体必须是 JSON 对象", 400)
        return data

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, exc: SchedulingError) -> None:
        payload: Json = {"code": exc.code, "message": str(exc)}
        if exc.diagnosis is not None:
            payload["diagnosis"] = sz.diagnosis_to_json(exc.diagnosis)
        self._send(exc.status, payload)

    # ---- 路由 ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path.strip("/").split("/")
            if path == ["reservations"]:
                self.service.reap_expired_holds()
                rows = self.service.storage.list_reservation_rows()
                payload = [
                    self._reservation_payload(self.service.storage.row_to_reservation(row))
                    for row in rows]
                self._send(200, {"reservations": payload})
            elif len(path) == 2 and path[0] == "reservations":
                r = self.service.get(path[1])
                self._send(200, self._reservation_payload(r))
            elif path == ["health"]:
                self._send(200, {"status": "ok"})
            else:
                self._send(404, {"code": "not_found", "message": self.path})
        except SchedulingError as exc:
            self._send_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            data = self._read_json()
            path = urlparse(self.path).path.strip("/").split("/")
            svc = self.service

            if path == ["admin", "staff"]:
                svc.upsert_staff(sz.staff_from_json(data))
                self._send(201, {"ok": True})
            elif path == ["admin", "venues"]:
                svc.upsert_venue(sz.venue_from_json(data))
                self._send(201, {"ok": True})
            elif path == ["admin", "groups"]:
                svc.upsert_group(sz.group_from_json(data))
                self._send(201, {"ok": True})
            elif len(path) == 3 and path[0] == "groups" and path[2] == "headcount":
                result = svc.change_headcount(path[1], int(data["headcount"]))
                self._send(200, result)
            elif path == ["plan"]:
                sessions = [sz.session_from_json(s) for s in data["sessions"]]
                ttl = data.get("ttl_minutes")
                diagnosis, ttl_used = svc.plan(data["group_id"], sessions, ttl)
                self._send(200, {
                    "ttl_minutes": ttl_used,
                    **sz.diagnosis_to_json(diagnosis)})
            elif path == ["reservations"]:
                sessions = [sz.session_from_json(s) for s in data["sessions"]]
                assignments = [sz.assignment_from_json(a)
                               for a in data["assignments"]]
                r = svc.hold(data["group_id"], sessions, assignments,
                             data.get("ttl_minutes"))
                self._send(201, self._reservation_payload(r))
            elif len(path) == 3 and path[0] == "reservations" and path[2] == "confirm":
                result = svc.confirm(path[1], data.get("expected_version"))
                if isinstance(result, ConfirmFailure):
                    self._send(409, sz.failure_to_json(result))
                else:
                    self._send(200, self._reservation_payload(result))
            elif len(path) == 3 and path[0] == "reservations" and path[2] == "cancel":
                r = svc.cancel(path[1], data.get("reason", "user_cancelled"))
                self._send(200, self._reservation_payload(r))
            elif path == ["reap-expired"]:
                ids = svc.reap_expired_holds()
                self._send(200, {"reaped": ids})
            else:
                self._send(404, {"code": "not_found", "message": self.path})
        except SchedulingError as exc:
            self._send_error(exc)
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"code": "bad_request", "message": str(exc)})

    # ---- 序列化辅助 ----------------------------------------------------
    def _reservation_payload(self, r) -> dict:
        headcount = None
        try:
            headcount = self.service.catalog.group(r.group_id).headcount
        except KeyError:
            pass
        return sz.reservation_to_json(r, headcount)


def build_server(service: SchedulingService, host: str = "127.0.0.1",
                 port: int = 8080) -> ThreadingHTTPServer:
    handler = type("BoundApiHandler", (ApiHandler,), {"service": service})
    server = ThreadingHTTPServer((host, port), handler)
    return server
