# -*- coding: utf-8 -*-
"""CAN 数据源抽象。

采集层只认这个接口，不关心数据是从虚拟台车来的还是从真车 CAN 卡来的。
换数据源不需要改采集循环一行代码，`config.json` 里改 `can.source` 即可。

**为什么 `read()` 要接收时间戳而不是自己去取当前时间**

波形必须由**采样时刻**决定，不能由"被调用的那一刻"决定。这样同一串时刻
无论实时跑还是回放，算出来的帧一模一样 —— 单元测试才能断言具体数值，
演示出问题时也才能原样重现。真车适配器同样受用：解码出来的值本来就
属于报文里的那个时刻，不是解码函数被调用的时刻。
"""
from __future__ import annotations

import abc
import logging
from datetime import datetime

from common.params import KIND_ANALOG, KIND_COUNTER, KIND_STATE, PARAMS

log = logging.getLogger("can_bus")


class CanSourceError(RuntimeError):
    """读不到这一帧。

    采集循环的处理是**跳过这一帧并计数**，而不是编一个值顶上。
    宁可曲线上缺一个点（平台侧看得出来），也不能把猜的数写成实测数据 ——
    这套系统对客户的全部说服力就在于"屏幕上每个数都是采到的"。
    """


class CanSource(abc.ABC):
    """一路 CAN 数据源。"""

    #: 给界面看的人话名字，例如"内置模拟器""python-can / socketcan"
    name: str = "can"

    #: 更细的说明，显示在终端设置页
    detail: str = ""

    @abc.abstractmethod
    def read(self, ts: datetime) -> dict[str, float]:
        """取 `ts` 时刻的一帧，返回 16 个参数的值。

        必须返回 `params.PARAM_KEYS` 里的**全部**键。少一个就抛
        `CanSourceError` —— 半帧数据比没有数据更危险，平台会把它当成
        "另一个参数确实是 0"存进去。
        """

    def close(self) -> None:
        """释放硬件资源。默认什么都不做。"""

    def snapshot(self) -> dict:
        """给界面看的状态。子类可扩展。"""
        return {"name": self.name, "detail": self.detail,
                "kind": type(self).__name__}

    # ---- 共用校验 ----
    @staticmethod
    def check_frame(values: dict) -> dict[str, float]:
        """补齐类型、检查是否齐帧。不齐就抛 `CanSourceError`。"""
        if values is None:
            raise CanSourceError("数据源返回了空帧")
        missing = [k for k in (p.key for p in PARAMS) if k not in values]
        if missing:
            from common.params import PARAM_BY_KEY
            raise CanSourceError(
                "数据源缺少参数：" + "、".join(PARAM_BY_KEY[k].name for k in missing))
        return values


# --------------------------------------------------------------------------
# 测试与占位用的数据源
# --------------------------------------------------------------------------
def neutral_values(**over) -> dict[str, float]:
    """按注册表推出一组"在域内但不动"的值。

    模拟量取正常范围的中点，开关量/累计量/坐标取 0。**不硬编码任何数字** ——
    注册表改了范围，这里跟着变，测试不会因为阈值调整而莫名其妙地失败。
    """
    out: dict[str, float] = {}
    for p in PARAMS:
        if p.kind == KIND_ANALOG:
            out[p.key] = round((p.lo + p.hi) / 2.0, max(p.decimals, 3))
        elif p.kind in (KIND_STATE, KIND_COUNTER):
            out[p.key] = 0.0
    out.update({k: float(v) for k, v in over.items()})
    return out


class FakeCan(CanSource):
    """测试用数据源：值由用例直接设定，不产生任何波形。

    与 `WetMixTrolley` 的区别是**没有内部状态机**，所以断言可以只盯着
    采集循环本身 —— 波形不对的时候，能立刻分清是波形的问题还是循环的问题。
    """

    name = "测试数据源"

    def __init__(self, values: dict | None = None, *, name: str | None = None):
        self.values = neutral_values(**(values or {}))
        self.reads: list[datetime] = []
        self._fail: CanSourceError | None = None
        self._fail_left = 0
        if name:
            self.name = name

    def set(self, **values: float) -> "FakeCan":
        """改一个或多个参数值。返回 self 以便链式调用。"""
        for k, v in values.items():
            if k not in self.values:
                raise KeyError(f"{k} 不是注册表里的参数")
            self.values[k] = float(v)
        return self

    def fail_next(self, message: str = "总线无响应", times: int = 1) -> "FakeCan":
        """让接下来 `times` 次读取抛错，用来测采集循环的容错。"""
        self._fail = CanSourceError(message)
        self._fail_left = int(times)
        return self

    def read(self, ts: datetime) -> dict[str, float]:
        self.reads.append(ts)
        if self._fail is not None:
            self._fail_left -= 1
            if self._fail_left <= 0:
                err, self._fail = self._fail, None
            raise err
        return dict(self.values)

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["reads"] = len(self.reads)
        return snap
