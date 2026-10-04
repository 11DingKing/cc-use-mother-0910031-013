"""python -m scheduling [--db data.db] [--host 127.0.0.1] [--port 8080] [--seed]"""
from __future__ import annotations

import argparse

from .clock import SystemClock
from .service import SchedulingService
from .storage import Storage
from .api import build_server
from .seed import seed_demo


def main() -> None:
    parser = argparse.ArgumentParser(description="开放日多资源排班服务")
    parser.add_argument("--db", default="scheduling.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--seed", action="store_true",
                        help="数据库为空时写入演示主数据")
    args = parser.parse_args()

    storage = Storage(args.db)
    service = SchedulingService(storage, SystemClock())
    if args.seed and not storage.load_staff():
        seed_demo(service)
    server = build_server(service, args.host, args.port)
    print(f"排班服务已启动：http://{args.host}:{args.port} （数据库 {args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
