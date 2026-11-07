# -*- coding: utf-8 -*-
"""车载终端的本地界面（薄壳）。

**这里不放业务逻辑。** 每个接口都是"取一个快照"或"转交给 app 的一个方法"，
判断全在 `app.py` / `store.py` / `uploader.py` 里。理由很实际：车载终端上
真正要保证的是数据不丢，界面随时可以重写；界面一旦开始自己算东西
（比如"这个值算不算越限"），规则就有了第二个来源，早晚会和平台对不上。

只绑 `127.0.0.1`：一是避免了 Windows 防火墙弹窗（绑 0.0.0.0 必弹），
二是现场拿笔记本给客户演示时，终端界面本来也只给驾驶室里那台屏看。
"""
from __future__ import annotations

import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import config, http_util, logging_setup
from common.params import PARAM_BY_KEY, PARAM_KEYS, PARAMS, as_dicts, groups
from common.timeutil import fmt_clock, now_utc_iso

log = logging.getLogger("terminal.webui")


def _uptime_label(seconds: int) -> str:
    """把运行秒数说成人话。演示时"已运行 2 小时 13 分"比"7963 秒"直观。"""
    s = max(0, int(seconds or 0))
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分钟"
    return f"{s // 3600} 小时 {s % 3600 // 60} 分"


SHUTTING = {"v": False}          # 一键关闭进行中：/api/status 会带回给页面


class Handler(logging_setup.QuietHandler, BaseHTTPRequestHandler):
    server_version = "WSCTerminal/1.0"
    protocol_version = "HTTP/1.1"
    app = None            # type: ignore[assignment]  由 serve() 注入

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_HEAD(self) -> None:
        self._handle("GET", head_only=True)

    # ------------------------------------------------------------------
    def _handle(self, method: str, head_only: bool = False) -> None:
        path, q = http_util.parse_qs(self.path)
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
                status, payload = self._dispatch(method, path, q, body or {})
            except ValueError as exc:
                # 参数不对 —— 是调用方的问题，明确回 400 而不是 500
                http_util.send_error_json(self, 400, str(exc))
                return
            except Exception:
                log.exception("处理 %s %s 出错", method, path)
                http_util.send_error_json(self, 500, "终端内部错误，详见日志")
                return
            if head_only:
                self._head_only(status, "application/json; charset=utf-8")
            else:
                http_util.send_json(self, payload, status)
            return

        rel = path.lstrip("/") or "index.html"
        if head_only:
            full = http_util.safe_join(config.TERMINAL_WEB, rel)
            self._head_only(200 if full and os.path.isfile(full) else 404,
                            http_util.guess_mime(full or ""))
            return
        if http_util.serve_static(self, config.TERMINAL_WEB, rel):
            return
        http_util.send_error_json(self, 404, f"未找到 {path}")

    # ------------------------------------------------------------------
    def _dispatch(self, method: str, path: str, q: dict, body: dict):
        app = self.app
        if method == "GET":
            if path == "/api/status":
                return 200, self._status(app)
            if path == "/api/records":
                return 200, {"records": app.store.recent(
                    limit=http_util.qs_int(q, "limit", 60),
                    status=http_util.qs_one(q, "status"),
                    param=http_util.qs_one(q, "param"),
                    scope=http_util.qs_one(q, "scope", "all"))}
            if path == "/api/uploads":
                return 200, {"uploads": app.store.uploads(
                    limit=http_util.qs_int(q, "limit", 60),
                    kind=http_util.qs_one(q, "kind"))}
            if path == "/api/batches":
                return 200, {"batches": app.store.batches(
                    limit=http_util.qs_int(q, "limit", 40))}
            if path == "/api/piles":
                return 200, {"piles": app.piles.all(),
                             "current": app.piles.current,
                             "version": app.piles.version}
            if path == "/api/trend":
                return 200, self._trend(app, q)
            if path == "/api/params":
                return 200, self._params()
            if path == "/api/faults":
                return 200, {"presets": self._fault_presets(),
                             "active": app.source.snapshot().get("faults", [])}
            if path == "/api/usb":
                return 200, self._usb(app, q)
            if path == "/api/logs":
                return 200, self._logs(q)
        else:
            if path == "/api/link":
                return 200, {"state": app.set_manual_offline(
                    bool(body.get("offline"))), "status": self._status(app)}
            if path == "/api/upload-now":
                n = app.upload_now()
                return 200, {"queued": n, "status": self._status(app)}
            if path == "/api/pile":
                cur = app.select_pile(str(body.get("pile_id", "")))
                return 200, {"current": cur}
            if path == "/api/identity/check":
                return 200, {"check": self._identity_check(app, body),
                             "identity": self._status(app)["identity"]}
            if path == "/api/identity/apply":
                try:
                    res = app.apply_identity(str(body.get("site_id") or ""),
                                             str(body.get("device_code") or ""))
                except http_util.NetworkError as exc:
                    # 统一转成 400 的文案：界面上"没改成"的所有原因都走同一条
                    # 显示路径，但话要说清是**连不上**还是**平台不同意**。
                    raise ValueError(f"连不上平台，身份未改动：{exc}") from None
                return 200, {"result": res, "status": self._status(app)}
            if path == "/api/fault":
                return 200, {"injected": app.inject_fault(str(body.get("key", ""))),
                             "status": self._status(app)}
            if path == "/api/fault/clear":
                return 200, {"cleared": app.clear_faults()}
            if path == "/api/usb/export":
                return 200, {"result": app.usb_export(
                    scope=str(body.get("scope") or "pending"),
                    limit=int(body.get("limit") or 2000)),
                    "status": self._status(app)}
            if path == "/api/usb/inspect":
                return 200, {"report": app.usb_inspect(self._bundle(body))}
            if path == "/api/usb/import":
                operator = str(body.get("operator") or "").strip()
                if not operator:
                    raise ValueError("请先填写操作人 —— 修订要追到人")
                return 200, {"result": app.usb_import(
                    self._bundle(body, default_latest=True), operator=operator,
                    reason_code=str(body.get("reason_code") or "VALUE_DISTORTED"),
                    note=str(body.get("note") or "")),
                    "status": self._status(app)}
            if path == "/api/shutdown":
                return 200, self._shutdown(app,
                                           broadcast=bool(body.get("broadcast")))
            if path == "/api/usb/edit":
                # 演示助手：模拟"在别的电脑上改了两行"。真机上这步是人拿 Excel 干的
                from . import usb_sync
                return 200, {"result": usb_sync.simulate_edit(
                    self._bundle(body, default_latest=True),
                    key=str(body.get("key") or "cylinderPumpingPressure"),
                    delta=float(body.get("delta") or 2.0),
                    count=int(body.get("count") or 2))}
        return 404, {"error": f"未知接口 {method} {path}"}

    # ------------------------------------------------------------------
    @staticmethod
    def _identity_check(app, body: dict) -> dict:
        """转手问平台。**浏览器不直连平台** —— 终端对平台只有一个出口
        （terminal/client.py），超时、代理、错误分类都在那里写了一遍。"""
        try:
            return app.client.check_identity(
                str(body.get("site_id") or "").strip(),
                str(body.get("device_code") or "").strip())
        except http_util.NetworkError as exc:
            # 核对必须联网。"平台拒绝了"和"没问到平台"要分开回，
            # 因为现场该做的事不同：一个是改输入，一个是查链路。
            return {"ok": False, "offline": True,
                    "error": f"连不上平台，无法核对：{exc}"}

    @staticmethod
    def _bundle(body: dict, default_latest: bool = False) -> str:
        """取要操作的 U 盘包路径。留空时用最近导出的那个。"""
        from . import usb_sync
        path = str(body.get("bundle") or "").strip()
        if not path and default_latest:
            path = usb_sync.latest_bundle()
        if not path:
            raise ValueError("没有可用的 U 盘导出包，先点「导出到 U 盘」")
        return path

    @staticmethod
    def _usb(app, q: dict) -> dict:
        """U 盘页要的全部内容：可见的包、修订队列、可选原因。"""
        from . import usb_sync
        return {
            "dir": usb_sync.usb_root(),
            "bundles": usb_sync.list_bundles(limit=http_util.qs_int(q, "limit", 20)),
            "reasons": usb_sync.reasons(),
            "revisions": app.store.revisions(limit=40),
            "revision_stat": app.store.stats()["revisions_pending"],
        }

    @staticmethod
    def _logs(q: dict) -> dict:
        """日志尾部。演示时不用去翻 data/logs 目录。"""
        want = http_util.qs_one(q, "name", "terminal.log")
        if os.sep in want or "/" in want or not want.endswith(".log"):
            raise ValueError(f"日志名不合法：{want}")
        path = os.path.join(config.LOG_DIR, want)
        if not os.path.isfile(path):
            raise ValueError(f"没有这个日志：{want}")
        with open(path, "rb") as fp:                    # 只读尾部，日志可能很大
            fp.seek(0, os.SEEK_END)
            size = fp.tell()
            fp.seek(max(0, size - 64 * 1024))
            tail = fp.read().decode("utf-8", errors="replace")
        limit = max(20, min(500, http_util.qs_int(q, "lines", 120)))
        lines = tail.splitlines()[-limit:]
        names = sorted(n for n in os.listdir(config.LOG_DIR) if n.endswith(".log"))
        return {"name": want, "dir": config.LOG_DIR, "files": names,
                "lines": lines, "count": len(lines)}

    # ------------------------------------------------------------------
    @staticmethod
    def _status(app) -> dict:
        """状态栏要的那些数字，一次取全。

        心跳、链路、上传进度是三个不同的线程写的，分别去取会取到
        "链路已离线但心跳还显示成功"这种自相矛盾的组合 —— 客户一眼就看出假。
        """
        snap = app.snapshot()
        snap["ui_time"] = now_utc_iso()
        snap["stopping"] = SHUTTING["v"]
        snap["uptime_label"] = _uptime_label(snap["heartbeat"].get("uptime_s", 0))
        return snap

    @staticmethod
    def _trend(app, q: dict) -> dict:
        """单个参数的本地趋势，**按时间升序**（画图要升序）。"""
        param = http_util.qs_one(q, "param", "cylinderPumpingPressure")
        if param not in PARAM_KEYS:
            raise ValueError(f"未知参数 {param}")
        limit = max(2, min(600, http_util.qs_int(q, "limit", 60)))
        rows = app.store.recent(limit=limit, param=param)
        rows.reverse()
        p = PARAM_BY_KEY[param]
        return {
            "param": param, "name": p.name, "unit": p.unit,
            "lo": p.lo, "hi": p.hi, "decimals": p.decimals,
            "points": [{
                "ts": r["ts_epoch"],
                "clock": fmt_clock(r["ts_epoch"]),
                "value": r.get("param_value"),
                "status": r["status"],
            } for r in rows if r.get("param_value") is not None],
        }

    @staticmethod
    def _params() -> dict:
        return {
            "groups": [{"name": k, "params": as_dicts(v)} for k, v in groups().items()],
            "all": as_dicts(PARAMS),
        }

    @staticmethod
    def _fault_presets() -> list[dict]:
        from common.machine import FAULT_PRESETS
        return [dict(f) for f in FAULT_PRESETS]

    # ------------------------------------------------------------------
    def _head_only(self, status: int, ctype: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype or "text/plain")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    @staticmethod
    def _shutdown(app, broadcast: bool = False) -> dict:
        """一键关闭：先让两端页面看到 stopping 标记自行关闭，再优雅停进程。"""
        SHUTTING["v"] = True

        def _bye():
            import os as _os
            import time as _t
            from common import config as _cfg
            from common import http_util as _hu
            _t.sleep(0.4)                 # 让响应先送达
            if not broadcast:             # 通知另一端也置标记、按同样节奏退出
                try:
                    _hu.post_json(_cfg.url_base("server", app.cfg) + "/api/shutdown",
                                  {"broadcast": True}, timeout=2)
                except Exception:
                    pass                  # 另一端不在了也无妨，run_all 会兜底
            _t.sleep(3.6)                 # 页面轮询 1s 一次，足够看到标记并关闭
            app.stop()                    # 关库、flush CSV、停全部线程
            _os._exit(0)
        threading.Thread(target=_bye, name="shutdown", daemon=True).start()
        return {"stopping": True}


class UiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32
    ui_url = ""


def serve(app, cfg: dict | None = None) -> UiServer:
    """建服务、起线程、返回句柄。调用方负责 `shutdown()`。"""
    cfg = cfg or config.load_config()
    Handler.app = app
    host = cfg["terminal"]["host"]
    port = int(cfg["terminal"]["port"])
    httpd = UiServer((host, port), Handler)
    httpd.ui_url = f"http://{host}:{port}"
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.3},
                     name="webui", daemon=True).start()
    log.info("终端界面已就绪：%s", httpd.ui_url)
    return httpd
