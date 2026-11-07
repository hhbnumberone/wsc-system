# -*- coding: utf-8 -*-
"""真车适配器：从 python-can 读 CAN 报文并解码成 16 个参数。

**这个文件做到了什么、没做到什么，先说清楚**

做到了：接口形状与内置模拟器完全一致（`CanSource`），采集循环、上传状态机、
界面一行都不用改就能换成真车；解码逻辑（字节序、有无符号、线性变换、
信号超时）是完整的。

没做到：**没有在真车上联调过** —— 本机既没有 CAN 硬件也没有装 python-can。
所以这里不含任何"看起来像真的"报文 ID 与缩放系数：那种编出来的表会让人
以为接上就能用，联调时才发现每个数都不对，比空着更糟。真车接入的步骤是
拿设备 DBC 把 `signals` 填进 `config/config.json` 的 `can` 节，代码不用动。

**缺参数就抛错，绝不补零。** 一个信号没解出来，这一帧就不发 —— 半帧数据
入库后，平台上"另一个参数确实是 0"和"这个参数没读到"长得一模一样，
而这两件事在隧道里的含义完全不同。

配置示例（`config/config.json`）：

    "can": {
      "source": "real",
      "bustype": "socketcan",
      "channel": "can0",
      "bitrate": 500000,
      "max_age_s": 3.0,
      "signals": [
        {"key": "cylinderPumpingPressure", "can_id": "0x18F00100",
         "start": 0, "length": 2, "scale": 0.01, "offset": 0, "signed": false}
      ]
    }

`start` 是起始字节，`length` 是字节数（1/2/4），`scale`/`offset` 做线性变换，
值 = 原始整数 × scale + offset。多帧拼一整个参数组也支持：同一次 `read()`
期间到达的报文会累积，只要所有参数都在超时窗口内到齐即可。
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from datetime import datetime

from common.params import PARAM_BY_KEY, PARAM_KEYS

from .base import CanSource, CanSourceError

log = logging.getLogger("can_bus.python_can")

#: 支持的整数字节宽度 -> struct 格式字符（小端）
_INT_FMT = {1: "b", 2: "h", 4: "i"}
_UINT_FMT = {1: "B", 2: "H", 4: "I"}


@dataclass(frozen=True)
class SignalSpec:
    """一个 CAN 信号到参数 key 的映射。"""

    key: str            # 参数 key（必须是 params.PARAM_KEYS 之一）
    can_id: int         # 仲裁 ID
    start: int = 0      # 起始字节
    length: int = 2     # 字节数，1/2/4
    scale: float = 1.0
    offset: float = 0.0
    signed: bool = False
    is_float: bool = False   # True 时按 IEEE754 单精度解（length 须为 4）

    @classmethod
    def from_dict(cls, d: dict) -> "SignalSpec":
        key = str(d.get("key", ""))
        if key not in PARAM_BY_KEY:
            raise ValueError(f"信号映射里的 {key!r} 不是注册表参数")
        cid = d.get("can_id", 0)
        try:
            cid = int(cid, 0) if isinstance(cid, str) else int(cid)
        except (TypeError, ValueError):
            raise ValueError(f"{key} 的 can_id 不是整数：{cid!r}") from None
        return cls(
            key=key, can_id=cid, start=int(d.get("start", 0)),
            length=int(d.get("length", 2)), scale=float(d.get("scale", 1.0)),
            offset=float(d.get("offset", 0.0)), signed=bool(d.get("signed", False)),
            is_float=bool(d.get("is_float", False)),
        )

    def decode(self, data: bytes) -> float:
        """从报文数据里取出本信号的值。"""
        end = self.start + self.length
        if end > len(data):
            raise CanSourceError(
                f"{PARAM_BY_KEY[self.key].name} 的信号位置越界："
                f"报文只有 {len(data)} 字节，映射要 {end} 字节")
        if self.is_float:
            raw = struct.unpack_from("<f", data, self.start)[0]
            return float(raw)
        if self.length not in _INT_FMT:
            raise CanSourceError(
                f"{PARAM_BY_KEY[self.key].name} 的信号宽度 {self.length} 不支持"
                f"（只支持 1/2/4 字节）")
        fmt = _INT_FMT if self.signed else _UINT_FMT
        raw = struct.unpack_from("<" + fmt[self.length], data, self.start)[0]
        return float(raw) * self.scale + self.offset


def parse_signals(items) -> list[SignalSpec]:
    """把配置里的信号表转成 `SignalSpec`，并检查是否覆盖了全部参数。"""
    specs = [SignalSpec.from_dict(d) for d in (items or [])]
    got = {s.key for s in specs}
    missing = [PARAM_BY_KEY[k].name for k in PARAM_KEYS if k not in got]
    if missing:
        raise ValueError(
            "CAN 信号表不完整，缺：" + "、".join(missing)
            + "。请按设备 DBC 补齐 config.json 的 can.signals —— "
              "缺参数时这个适配器不会补零，会直接拒绝出数。")
    return specs


class PythonCanSource(CanSource):
    """基于 python-can 的数据源。

    `python-can` **惰性导入**：没装也不能影响模拟器运行 —— 这台演示机上
    就没有，而 99% 的使用者只需要内置模拟器。
    """

    name = "真车 CAN"

    def __init__(self, *, bustype: str = "socketcan", channel: str = "can0",
                 bitrate: int = 500000, signals=None, max_age_s: float = 3.0,
                 recv_timeout_s: float = 0.2):
        try:
            import can  # noqa: F401  惰性导入，缺了才报错
        except ImportError:
            raise CanSourceError(
                "没有安装 python-can，无法连接真车 CAN 总线。\n"
                "  安装：pip install python-can\n"
                "  或把 config.json 的 can.source 改回 \"sim\"（内置模拟器）"
            ) from None

        items = list(signals or [])
        self.signals: list[SignalSpec] = (
            items if items and isinstance(items[0], SignalSpec) else parse_signals(items))
        self.bustype = bustype
        self.channel = channel
        self.bitrate = int(bitrate)
        self.max_age_s = float(max_age_s)
        self.recv_timeout_s = float(recv_timeout_s)
        self.detail = f"python-can {bustype}/{channel} @ {bitrate // 1000}k"
        self._by_id: dict[int, list[SignalSpec]] = {}
        for s in self.signals:
            self._by_id.setdefault(s.can_id, []).append(s)

        import can
        try:
            self.bus = can.Bus(interface=bustype, channel=channel,
                               bitrate=self.bitrate)
        except Exception as exc:
            raise CanSourceError(
                f"打开 CAN 总线失败（{self.detail}）：{exc}\n"
                f"  检查接线与驱动；Windows 上常见的 bustype 是 "
                f"\"pcan\" / \"vector\" / \"slcan\"，Linux 上才是 \"socketcan\"。"
            ) from None
        self._latest: dict[str, tuple[float, float]] = {}   # key -> (时间, 值)
        self._frames = 0
        self._dropped = 0

    # ------------------------------------------------------------------
    def read(self, ts: datetime) -> dict[str, float]:
        """把超时窗口内到达的报文收干净，再检查参数是否到齐。"""
        deadline = self.recv_timeout_s
        while True:
            msg = self.bus.recv(timeout=deadline)
            if msg is None:
                break
            deadline = 0.0        # 先阻塞等一帧，随后只管把积压的收完
            self._ingest(msg)

        now = ts.timestamp()
        values: dict[str, float] = {}
        stale: list[str] = []
        for key in PARAM_KEYS:
            item = self._latest.get(key)
            if item is None:
                continue
            seen_at, v = item
            # 信号超时的不算数：传感器十分钟前就断了，不能还把最后一帧
            # 反复当成新数据传上去 —— 那种"数据一切正常"最害人。
            if now - seen_at > self.max_age_s:
                stale.append(PARAM_BY_KEY[key].name)
                continue
            values[key] = v
        if stale:
            raise CanSourceError("以下信号已超时未更新：" + "、".join(stale))
        return self.check_frame(values)

    def _ingest(self, msg) -> None:
        specs = self._by_id.get(msg.arbitration_id)
        if not specs:
            return
        self._frames += 1
        seen_at = float(getattr(msg, "timestamp", 0.0) or 0.0)
        if seen_at <= 0:      # 某些后端不填时间戳
            seen_at = datetime.now().timestamp()
        for s in specs:
            try:
                self._latest[s.key] = (seen_at, s.decode(bytes(msg.data)))
            except CanSourceError as exc:
                # 单条报文解不出来不该让整个采集循环炸掉 —— 记一次，等下一帧。
                # 只有**持续**解不出来才会体现为参数缺失，最终由 read() 抛错。
                self._dropped += 1
                if self._dropped % 50 == 1:
                    log.warning("CAN 报文解码失败（累计 %d 次）：%s",
                                self._dropped, exc)

    def close(self) -> None:
        try:
            self.bus.shutdown()
        except Exception:  # pragma: no cover - 关闭失败无所谓
            pass

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap.update({
            "bus": self.detail,
            "signals": len(self.signals),
            "frames": self._frames,
            "decode_errors": self._dropped,
            "max_age_s": self.max_age_s,
        })
        return snap


