# -*- coding: utf-8 -*-
"""CAN 数据源：抽象基类、内置虚拟台车、真车适配器。

本机没有真实 CAN 硬件（也没有 python-can），所以默认走 `simulator.py`。
真车上把 `config.json` 的 `can.source` 改成 `real`、装好 python-can、
按设备 DBC 填好信号表即可切换，上层采集与上传逻辑一行都不用改。

采集层只调 `build()` 一个函数 —— 换数据源的判断只此一处。
"""
from __future__ import annotations

from .base import CanSource, CanSourceError, FakeCan, neutral_values

__all__ = ["CanSource", "CanSourceError", "FakeCan", "neutral_values", "build"]


def build(cfg: dict) -> CanSource:
    """按配置造一个数据源。

    `config.json` 的 `can.source`：

    * `"sim"`  —— 内置模拟器（默认，演示用）
    * `"real"` —— python-can 真车适配器

    真车适配器建不起来时（没装 python-can、总线打不开）由调用方决定怎么办：
    采集循环会把它记成"数据源不可用"并**继续重试**，因为现场最常见的情况
    就是 CAN 卡还没插好 —— 这时候要的是界面上明确显示"没数据"，
    而不是进程直接退出让整个终端黑掉。
    """
    c = cfg.get("can") or {}
    which = str(c.get("source", "sim")).lower()
    if which in ("sim", "simulator", "mock", ""):
        from .simulator import WetMixTrolley
        return WetMixTrolley(seed=int(c.get("seed", 20261005)),
                             device_code=str(cfg["device"]["device_code"]))
    if which in ("real", "can", "python-can", "python_can"):
        from .python_can_source import PythonCanSource
        return PythonCanSource(
            bustype=str(c.get("bustype", "socketcan")),
            channel=str(c.get("channel", "can0")),
            bitrate=int(c.get("bitrate", 500000)),
            signals=c.get("signals") or [],
            max_age_s=float(c.get("max_age_s", 3.0)),
            recv_timeout_s=float(c.get("recv_timeout_s", 0.2)),
        )
    raise CanSourceError(
        f"config.json 的 can.source 取值无法识别：{which!r}（只认 sim / real）")
