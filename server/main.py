# -*- coding: utf-8 -*-
"""平台服务入口。

    python -m server.main [--port 8700] [--reset] [--seed] [--quiet]

只绑 `127.0.0.1`：一是演示机不需要对外暴露，二是绑回环不会触发
Windows 防火墙弹窗（绑 0.0.0.0 第一次启动必然弹一个"是否允许访问"，
演示现场被这个打断很尴尬）。

Ctrl+C 优雅退出：停后台线程 → 关连接 → 退出码 0。
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):  # 允许 python server/main.py 直接跑
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import config, http_util, logging_setup
from common.timeutil import now_epoch

from . import alarms as alarms_mod
from . import api as api_mod
from . import db

log = logging.getLogger("server")

_stop = threading.Event()


# --------------------------------------------------------------------------
# 清理线程
# --------------------------------------------------------------------------
class Retention(threading.Thread):
    """定期清理过期数据。

    **只删平台上早已入库的历史帧**，且严格按 `retention.frames_days` 走。
    终端本地的 CSV 归终端自己按 30 天清理，两边互不干涉 ——
    这一点必须清楚，否则演示时会有人以为"平台删了终端也没了"。
    """

    def __init__(self, cfg: dict):
        super().__init__(name="retention", daemon=True)
        self.cfg = cfg
        self.interval = float(cfg["retention"]["collect_interval_s"])
        self.days = int(cfg["retention"]["frames_days"])

    def run(self) -> None:
        while not _stop.wait(self.interval):
            try:
                self.tick()
            except Exception:  # pragma: no cover - 清理失败不能拖垮服务
                log.exception("清理任务异常")

    def tick(self) -> None:
        cutoff_ms = int((now_epoch() - self.days * 86400) * 1000)
        conn = db.get_conn()
        with db.transaction(conn):
            n = conn.execute("DELETE FROM frames WHERE ts_epoch < ?",
                             (cutoff_ms,)).rowcount
            m = conn.execute("DELETE FROM audit_events WHERE ts_epoch < ?",
                             (cutoff_ms,)).rowcount
        if n or m:
            log.info("清理：删除 %d 条过期帧、%d 条过期审计事件（保留 %d 天）",
                     n, m, self.days)
        db.close_conn()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(logging_setup.QuietHandler, BaseHTTPRequestHandler):
    server_version = "WSCPlatform/1.0"
    protocol_version = "HTTP/1.1"
    api: api_mod.Api = None        # type: ignore[assignment]

    # ---- 路由 ----
    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_HEAD(self) -> None:  # 浏览器/工具偶尔会探一下
        self._handle("GET", head_only=True)

    def _handle(self, method: str, head_only: bool = False) -> None:
        path, q = http_util.parse_qs(self.path)

        # ---- 接口 ----
        if path.startswith("/api/"):
            body = None
            if method == "POST":
                try:
                    body = http_util.read_json_body(self)
                except ValueError as exc:
                    if path == "/api/shutdown":
                        body = {}          # 关机接口允许空体，curl 也能用
                    else:
                        http_util.send_error_json(self, 400, str(exc))
                        return
            try:
                conn = db.get_conn()
                status, payload, kind = api_mod.dispatch(self.api, conn, method, path, q, body)
            except Exception:
                log.exception("处理 %s %s 出错", method, path)
                http_util.send_error_json(self, 500, "服务端内部错误，详见日志")
                return
            if kind == "csv":
                if not head_only:
                    http_util.send_text(self, payload, status,
                                        "text/csv; charset=utf-8")
                else:
                    self._head_only(status, "text/csv; charset=utf-8")
            else:
                # 状态码**原样发出去**。这里曾经有一句
                # `status, payload = self._jsonable(payload)`，而那个函数
                # 写的是 `return 200, payload` —— 于是业务侧的 400 / 404 / 409
                # 到了线上全变成 200。终端的重传逻辑正靠 4xx 分辨
                # "平台明确拒收"（不该再重试）和"没连上"（该退避重传），
                # 全 200 的话一次整批拒收要重传 8 次、耗时几分钟才落成
                # REJECTED，原因码还在重传路上丢了。序列化本来就有
                # `send_json` 的 `default=str` 兜着，不需要中间再包一层。
                if head_only:
                    self._head_only(status, "application/json; charset=utf-8")
                else:
                    http_util.send_json(self, payload, status)
            return

        # ---- 静态文件 ----
        rel = path.lstrip("/") or "index.html"
        if head_only:
            full = http_util.safe_join(config.SERVER_WEB, rel)
            if full and os.path.isfile(full):
                self._head_only(200, http_util.guess_mime(full))
            else:
                self._head_only(404, "text/plain; charset=utf-8")
            return
        if http_util.serve_static(self, config.SERVER_WEB, rel):
            return
        # 单页应用兜底：未知路径回 index.html，方便直接刷 /history 这种地址
        if "." not in os.path.basename(rel):
            if http_util.serve_static(self, config.SERVER_WEB, "index.html"):
                return
        http_util.send_error_json(self, 404, f"未找到 {path}")

    def _head_only(self, status: int, ctype: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Connection", "close")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
def build(cfg: dict | None = None) -> tuple[Server, api_mod.Api]:
    """建好数据库、种子、报警引擎与 HTTP 服务（不启动）。"""
    cfg = cfg or config.load_config()
    config.ensure_default_files()
    db.init_db()

    conn = db.get_conn()
    engine = alarms_mod.AlarmEngine(config.load_thresholds())
    api = api_mod.Api(cfg, engine)

    # 工点档案与桩号清单**每次启动都对齐一次**（两者都是 INSERT OR IGNORE /
    # upsert，幂等）。原来只在空库时生成，是为了省一次启动开销；现在多了一步
    # "改配置加一个工点，重启就生效"的诉求，那点开销（几百行 executemany）
    # 换来的确定性更值。
    from . import seed as seed_mod
    with db.transaction(conn):
        seed_mod.ensure_sites(conn, cfg)
        if conn.execute("SELECT COUNT(*) FROM piles").fetchone()[0] == 0:
            log.info("桩号清单为空，生成默认清单")
        seed_mod.ensure_piles(conn, cfg)
        seed_mod.ensure_device(conn, cfg)

    Handler.api = api
    host = cfg["server"]["host"]
    port = int(cfg["server"]["port"])
    httpd = Server((host, port), Handler)
    log.info("平台已就绪：http://%s:%d  （数据库 %s）", host, port,
             os.path.relpath(config.SERVER_DB, config.ROOT))
    return httpd, api


def main(argv: list[str] | None = None) -> int:
    logging_setup.enable_utf8_stdout()
    cfg = config.load_config()

    ap = argparse.ArgumentParser(description="湿喷台车信息化系统 — 云平台")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--reset", action="store_true", help="清空平台数据库后启动")
    ap.add_argument("--seed", action="store_true", help="生成 30 天演示历史数据")
    ap.add_argument("--no-retention", action="store_true", help="不启动清理线程")
    args = ap.parse_args(argv)

    if args.port:
        cfg["server"]["port"] = args.port
    config.ensure_default_files()
    log_dir = config.LOG_DIR
    logging_setup.setup("server", log_dir, logging.INFO)
    logging_setup.setup("server.api", log_dir, logging.INFO)
    logging_setup.setup("server.alarms", log_dir, logging.INFO)
    logging_setup.setup("http.access", log_dir, logging.WARNING, to_file=False)
    logging_setup.install_excepthook(log)

    if args.reset:
        log.warning("--reset：清空平台数据库 %s", config.SERVER_DB)
        db.reset_db()

    httpd, _api = build(cfg)

    if args.seed:
        from . import seed as seed_mod
        n = seed_mod.seed_history(cfg)
        log.info("演示种子数据就绪：%d 条帧记录", n)

    threads: list[threading.Thread] = []
    if not args.no_retention:
        t = Retention(cfg)
        t.start()
        threads.append(t)

    def _bye(signum, frame):  # pragma: no cover - 交互式
        log.info("收到退出信号，正在停止…")
        _stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    # SIGBREAK 只有 Windows 有，但没有它 `run_all.py` 就只能 terminate()——
    # 那是直接抹掉进程，收尾流程（关库、放端口）全跳过。
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is None:            # pragma: no cover - 非 Windows
            continue
        try:
            signal.signal(sig, _bye)
        except (ValueError, OSError):  # pragma: no cover - 非主线程
            pass

    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        _stop.set()
        httpd.server_close()
        db.close_conn()
        log.info("平台已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
