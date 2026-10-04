"""零依赖 HTTP 接口（标准库 ThreadingHTTPServer）。

路由：

- ``POST /resources``                 登记/更新讲解员、讲师、场地
- ``GET  /resources[/...]``           查询资源
- ``POST /resources/{id}/reject``     部分资源拒绝某时段
- ``POST /resources/{id}/active``     停用/启用资源
- ``POST /groups`` / ``GET /groups``  学校团体
- ``POST /groups/{id}/size``          学校人数变化
- ``POST /plans``                     排定前生成候选方案（阶段一）
- ``POST /plans/{id}/confirm``        原子确认锁定全部资源（阶段二）
- ``POST /plans/{id}/cancel``         取消并释放
- ``GET  /plans/{id}``                候选状态
- ``GET  /schedule?resource_id=``     已确认排期
- ``GET  /stats``                     运行统计（含过期回收计数观测）

错误响应统一为 ``{"error": {"code", "message", ...}}``，
冲突响应携带 ``conflicts`` 与 ``alternatives`` 供前端解释。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .exceptions import DomainError
from .service import SchedulingService

STATUS_CODES = {
    "not_found": 404,
    "candidate_not_found": 404,
    "resource_not_found": 404,
    "group_not_found": 404,
    "entry_not_found": 404,
    "candidate_expired": 410,
    "schedule_conflict": 409,
    "stale_candidate_version": 409,
    "qualification_mismatch": 422,
}


def create_handler(service: SchedulingService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "OpenDayScheduler/1.0"

        def log_message(self, fmt: str, *args) -> None:  # 安静日志
            return

        # ---- 基础工具 ----------------------------------------------------
        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
            if not isinstance(data, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return data

        def _send(self, status: int, payload: dict | list) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                status = STATUS_CODES.get(exc.code, 400)
                self._send(status, {"error": exc.to_dict()})
            elif isinstance(exc, ValueError):
                self._send(400, {"error": {"code": "bad_request",
                                           "message": str(exc)}})
            else:
                self._send(500, {"error": {"code": "internal_error",
                                           "message": str(exc)}})

        # ---- 路由 --------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            try:
                parts = [p for p in urlsplit(self.path).path.split("/") if p]
                qs = parse_qs(urlsplit(self.path).query)
                if parts == ["resources"]:
                    self._send(200, {"resources": service.list_resources()})
                elif len(parts) == 2 and parts[0] == "resources":
                    self._send(200, service.get_resource(parts[1]))
                elif parts == ["groups"]:
                    self._send(200, {"groups": service.list_groups()})
                elif parts == ["schedule"]:
                    rid = qs.get("resource_id", [None])[0]
                    self._send(200, service.schedule(rid))
                elif parts == ["stats"]:
                    self._send(200, service.stats())
                elif len(parts) == 2 and parts[0] == "plans":
                    self._send(200, service.get_candidate(parts[1]))
                else:
                    self._send(404, {"error": {"code": "not_found",
                                               "message": "路径不存在"}})
            except Exception as exc:  # noqa: BLE001
                self._error(exc)

        def do_POST(self) -> None:  # noqa: N802
            try:
                parts = [p for p in urlsplit(self.path).path.split("/") if p]
                data = self._read_json()

                if parts == ["resources"]:
                    self._send(200, service.register_resource(data))
                elif len(parts) == 3 and parts[0] == "resources" and parts[2] == "reject":
                    self._send(200, service.reject_resource(
                        parts[1], data["start"], data["end"], data.get("reason", "")))
                elif len(parts) == 3 and parts[0] == "resources" and parts[2] == "active":
                    service.set_resource_active(parts[1], bool(data.get("active", True)))
                    self._send(200, {"id": parts[1], "active": bool(data.get("active", True))})
                elif parts == ["groups"]:
                    self._send(200, service.register_group(data))
                elif len(parts) == 3 and parts[0] == "groups" and parts[2] == "size":
                    self._send(200, service.change_group_size(parts[1], int(data["size"])))
                elif parts == ["plans"]:
                    result = service.plan(
                        data["sessions"],
                        ttl_seconds=int(data.get("ttl_seconds", 600)),
                        note=data.get("note", ""))
                    self._send(200 if result.get("feasible") else 409, result)
                elif len(parts) == 3 and parts[0] == "plans" and parts[2] == "confirm":
                    result = service.confirm(
                        parts[1],
                        expected_group_sizes=data.get("expected_group_sizes"))
                    self._send(200, result)
                elif len(parts) == 3 and parts[0] == "plans" and parts[2] == "cancel":
                    self._send(200, service.cancel(parts[1]))
                elif parts == ["reap"]:
                    self._send(200, {"reaped": service.reap_expired()})
                else:
                    self._send(404, {"error": {"code": "not_found",
                                               "message": "路径不存在"}})
            except Exception as exc:  # noqa: BLE001
                self._error(exc)

    return Handler


def serve(db_path: str = ":memory:", host: str = "127.0.0.1",
          port: int = 8080) -> ThreadingHTTPServer:
    service = SchedulingService(db_path)
    httpd = ThreadingHTTPServer((host, port), create_handler(service))
    httpd.service = service  # type: ignore[attr-defined]
    return httpd


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="开放日多资源排班服务")
    parser.add_argument("--db", default="data/scheduler.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    httpd = serve(args.db, args.host, args.port)
    print(f"排班服务已启动：http://{args.host}:{args.port}（数据库 {args.db}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.service.close()


if __name__ == "__main__":
    main()
