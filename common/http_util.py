# -*- coding: utf-8 -*-
"""HTTP 底层工具：静态文件、JSON 响应、无代理 opener。

Windows 演示环境踩过的坑，都在这里一次性堵掉：

* **系统代理**：客户机可能配了代理，`urllib` 会默默走代理然后超时。
  这里显式挂空 `ProxyHandler({})`，让终端只走直连。
* **IPv6 优先**：本机 `localhost` 先解析成 `::1`，而服务端绑的是 `127.0.0.1`，
  于是连接被拒。约定两端一律写字面量 `127.0.0.1`，不用 `localhost`。
* **连接不释放**：`urllib` 默认复用连接，服务端是 `http.server`（单线程/短连接），
  容易在半开连接上卡满超时。统一发 `Connection: close`。
* **超时缺失**：`urlopen` 不给 timeout 会无限等。本模块所有请求强制带 timeout。
"""
from __future__ import annotations

import json
import os
import posixpath
import socket
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus

# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------
class NetworkError(Exception):
    """所有网络层失败的统一出口。

    调用方只需要捕获这一个异常。它和"平台明确拒收"是两回事：
    网络错误 -> 数据回 PENDING，稍后重试；拒收 -> 数据永久失败，交人工。

    还带两个字段，供上传层做决策：

    * `status` —— HTTP 状态码。0 表示根本没连上（超时、拒绝连接）。
    * `body`   —— 平台回的 JSON 体（能解析出来才有）。上传层靠里面的 `code`
      分辨"这批太大"（可拆）和"这批被篡改"（必须停下来人工处理）——
      光看状态码分不出这两种，都只会变成一句"HTTP 4xx"。
    """

    def __init__(self, message: str, *, retryable: bool = True,
                 status: int = 0, body: dict | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = int(status)
        self.body: dict = body or {}

    @property
    def code(self) -> str:
        """平台回的原因码，如 BATCH_TOO_LARGE / DUPLICATE_CONFLICT。没有则空串。"""
        return str(self.body.get("code") or "")


# --------------------------------------------------------------------------
# MIME
# --------------------------------------------------------------------------
MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}


def guess_mime(path: str) -> str:
    """显式 MIME 表。不用 mimetypes.guess_type —— 它会读 Windows 注册表，
    在某些机器上把 .js 猜成 text/plain，浏览器直接拒绝执行。"""
    _, ext = os.path.splitext(path.lower())
    return MIME.get(ext, "application/octet-stream")


# --------------------------------------------------------------------------
# 服务端助手
# --------------------------------------------------------------------------
def send_json(handler, obj, status: int = 200) -> None:
    """回一个 JSON 响应。始终关闭连接。"""
    body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        # 客户端走了（刷新页面、关标签），不是错误
        pass


def send_text(handler, text: str, status: int = 200, ctype: str = "text/plain; charset=utf-8") -> None:
    body = text.encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass


def send_error_json(handler, status: int, message: str, **extra) -> None:
    payload = {"error": message, "status": int(status)}
    payload.update(extra)
    send_json(handler, payload, status=status)


def read_json_body(handler, max_bytes: int = 8 * 1024 * 1024) -> dict:
    """读取并解析请求体。超限/非 JSON 时抛 ValueError（调用方回 400）。"""
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        raise ValueError("Content-Length 不合法") from None
    if length <= 0:
        raise ValueError("请求体为空")
    if length > max_bytes:
        raise ValueError(f"请求体过大：{length} 字节，上限 {max_bytes}")
    raw = handler.rfile.read(length)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"请求体不是合法 JSON：{exc}") from None
    if not isinstance(obj, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return obj


def safe_join(root: str, rel: str) -> str | None:
    """把 URL 路径安全地拼到静态根目录下。

    返回 None 表示越界（`..`、绝对路径、盘符）。**这是目录穿越的唯一防线**，
    任何静态文件响应都必须从这里过。
    """
    rel = urllib.parse.unquote(rel or "").replace("\\", "/")
    rel = posixpath.normpath("/" + rel).lstrip("/")
    if not rel:
        rel = "index.html"
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    full = os.path.normpath(os.path.join(root, *parts))
    root_abs = os.path.normpath(os.path.abspath(root))
    if not os.path.abspath(full).startswith(root_abs + os.sep) and os.path.abspath(full) != root_abs:
        return None
    return full


def serve_static(handler, root: str, rel: str, *, no_cache: bool = True) -> bool:
    """把 root 下的静态文件写进响应。命中返回 True，未命中返回 False。"""
    full = safe_join(root, rel)
    if full is None or not os.path.isfile(full):
        return False
    try:
        with open(full, "rb") as fp:
            body = fp.read()
    except OSError:
        return False
    try:
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", guess_mime(full))
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store" if no_cache else "public, max-age=3600")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass
    return True


def parse_qs(path: str) -> tuple[str, dict[str, list[str]]]:
    """拆出路径与查询参数。"""
    u = urllib.parse.urlparse(path)
    return u.path, urllib.parse.parse_qs(u.query)


def qs_one(q: dict[str, list[str]], key: str, default: str = "") -> str:
    vals = q.get(key)
    return vals[0] if vals else default


def qs_int(q: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return int(qs_one(q, key, str(default)))
    except (TypeError, ValueError):
        return default


def qs_float(q: dict[str, list[str]], key: str, default: float) -> float:
    try:
        return float(qs_one(q, key, str(default)))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------
def make_opener() -> urllib.request.OpenerDirector:
    """构造一个不读系统代理的 opener。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


_OPENER = make_opener()

#: 会被归为 NetworkError 的底层异常
_NET_EXC = (
    urllib.error.URLError,
    urllib.error.HTTPError,
    socket.timeout,
    TimeoutError,
    ConnectionError,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    OSError,
)


def _request_json(url: str, *, data: bytes | None, timeout: float,
                  method: str, headers: dict | None = None) -> dict:
    hdrs = {
        "Accept": "application/json",
        "Connection": "close",          # 不复用连接，避免半开卡死
        "User-Agent": "wsc-terminal/1.0",
    }
    if data is not None:
        hdrs["Content-Type"] = "application/json; charset=utf-8"
    if headers:
        hdrs.update(headers)

    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # 平台明确回了错误码。4xx 里除了 408/429 都不值得重试。
        raw_body = b""
        try:
            raw_body = exc.read()
        except Exception:  # pragma: no cover - 读 body 失败不影响判断
            pass
        code = int(exc.code)
        retryable = code in (408, 425, 429) or code >= 500
        # 尽量把结构化 body 解析出来：上传层要读里面的 code 决定
        # "拆批重发" 还是 "停下来交人工"，光凭状态码分辨不出。
        obj: dict = {}
        try:
            parsed = json.loads(raw_body.decode("utf-8"))
            if isinstance(parsed, dict):
                obj = parsed
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        detail = obj.get("error") or raw_body.decode("utf-8", "replace")[:400]
        raise NetworkError(f"平台返回 HTTP {code}：{detail}", retryable=retryable,
                           status=code, body=obj) from None
    except _NET_EXC as exc:
        raise NetworkError(f"网络不可达：{type(exc).__name__}: {exc}") from None

    if status >= 400:  # pragma: no cover - 已由 HTTPError 分支覆盖
        raise NetworkError(f"平台返回 HTTP {status}", retryable=status >= 500)
    if not raw:
        raise NetworkError("平台返回空响应", retryable=True)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NetworkError(f"平台响应不是合法 JSON：{exc}", retryable=True) from None
    if not isinstance(obj, dict):
        raise NetworkError("平台响应不是 JSON 对象", retryable=True)
    return obj


def post_json(url: str, payload: dict, timeout: float = 3.0) -> dict:
    """POST 一个 JSON 对象并解析 JSON 响应。失败一律抛 NetworkError。"""
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    return _request_json(url, data=data, timeout=timeout, method="POST")


def get_json(url: str, timeout: float = 3.0, params: dict | None = None) -> dict:
    """GET 并解析 JSON 响应。"""
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
    return _request_json(url, data=None, timeout=timeout, method="GET")


def probe(url: str, timeout: float = 2.0) -> bool:
    """轻量连通性探测。任何异常都当成"不通"，不抛出。"""
    try:
        get_json(url, timeout=timeout)
        return True
    except NetworkError:
        return False


def local_base(host: str, port: int) -> str:
    """拼出本机服务基地址。固定用 127.0.0.1 字面量，见模块开头说明。"""
    h = "127.0.0.1" if host in ("localhost", "::1", "", "0.0.0.0") else host
    return f"http://{h}:{int(port)}"
