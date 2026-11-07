# -*- coding: utf-8 -*-
"""日志配置。

Windows 上的两个硬约束：

1. 控制台默认代码页可能是 GBK，中文日志直接抛 `UnicodeEncodeError`。
   所有入口先调 `enable_utf8_stdout()`，把 stdout/stderr 重设成 UTF-8。
2. `http.server` 默认给每个请求打一行日志，1 秒轮询下会把控制台刷到看不见别的。
   用 `QuietHandler` 把它降到 DEBUG，并且不写进文件（写进去日志文件很快就爆）。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

#: 控制台格式：时间 级别 来源 消息
CONSOLE_FMT = "%(asctime)s %(levelname)-5s %(name)-12s %(message)s"
CONSOLE_DATEFMT = "%H:%M:%S"

#: 文件格式：加上日期与毫秒，方便事后对齐时间线
FILE_FMT = "%(asctime)s.%(msecs)03d %(levelname)-5s [%(name)s] %(message)s"
FILE_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured: set[str] = set()


def enable_utf8_stdout() -> None:
    """把标准输出/错误重设成 UTF-8。某些环境下 reconfigure 不可用，忽略即可。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):  # pragma: no cover
            pass


def quiet(level: int = logging.CRITICAL) -> None:
    """把本项目的日志压到 level 以上。**给单元测试用。**

    跑测试会大量触发"回收超时上传""批次重放内容不一致""开警/解除"这类告警 ——
    那是**被测试的行为本身**，不是问题。它们会把进度点冲得看不见，
    所以测试模块 import 时调一次这个。

    放在这里而不是 `tests/__init__.py`：`discover -s tests` 是把测试模块
    按顶层模块名加载的，那个包的 `__init__` 根本不会被执行。
    """
    for name in ("", "server", "terminal", "common", "http"):
        logging.getLogger(name).setLevel(level)


def setup(name: str, log_dir: str | None = None, level: int | str = logging.INFO,
          *, console: bool = True, to_file: bool = True) -> logging.Logger:
    """配置并返回一个 logger。同名重复调用只生效一次。"""
    logger = logging.getLogger(name)
    log_level = logging.getLevelName(level) if isinstance(level, str) else level
    if not isinstance(log_level, int):
        log_level = logging.INFO
    logger.setLevel(log_level)
    logger.propagate = False

    if name in _configured:
        return logger
    _configured.add(name)

    if to_file and log_dir:
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, f"{name}.log")
        fh = logging.handlers.RotatingFileHandler(
            path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter(FILE_FMT, FILE_DATEFMT))
        fh.setLevel(log_level)
        logger.addHandler(fh)

    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter(CONSOLE_FMT, CONSOLE_DATEFMT))
        ch.setLevel(log_level)
        logger.addHandler(ch)

    return logger


class QuietHandler:
    """混入 `http.server.BaseHTTPRequestHandler`，把逐请求日志降级。

    用法：`class Handler(QuietHandler, BaseHTTPRequestHandler): ...`

    降级到 DEBUG 而不是直接吞掉 —— 想看的时候把级别调到 DEBUG 就有，
    平时控制台干净。
    """

    _access_logger: logging.Logger | None = None

    def log_message(self, fmt: str, *args) -> None:
        lg = self._access_logger or logging.getLogger("http.access")
        lg.debug("%s - %s", self.address_string(), fmt % args)

    def log_error(self, fmt: str, *args) -> None:
        lg = self._access_logger or logging.getLogger("http.access")
        lg.debug("错误 - %s", fmt % args)


def install_excepthook(logger: logging.Logger) -> None:
    """把未捕获异常写进日志，而不是只打到 stderr 后被窗口关掉。"""

    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.critical("未捕获异常", exc_info=(exc_type, exc, tb))

    sys.excepthook = _hook
