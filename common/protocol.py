# -*- coding: utf-8 -*-
"""终端 <-> 平台 的报文契约。

只有三条上行接口（上传 / 心跳 / 拉桩号清单）和一条下行接口（回执）。
所有报文都是 JSON，字段名固定 ASCII；中文只出现在屏幕和 CSV 以外的说明字段里。

设计要点：
- `Batch.records` 的形状随 `kind` 变化：frame 批装帧数据，revision 批装修订事件，
  audit 批装审计事件。这样**只有一条上传路径**，幂等逻辑写一遍就够。
- 时间戳双写：`ts_device` 采集时定死（设备时钟），`ts_server` 由平台在入库时补，
  只作审计，永不覆盖设备时间。数据可信性从这里开始。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import ids
from .enums import BatchStatus, EventKind, ReasonCode, RecordResult
from .params import PARAM_KEYS
from .timeutil import now_utc_iso

PROTOCOL_VERSION = "1.0"

#: 服务端允许的最大时间超前量。设备时钟快于服务器超过这个值即整条拒收，
#: 防止"未来数据"污染曲线，也防止有人手工改系统时间刷数据。
MAX_CLOCK_SKEW_S = 300


# --------------------------------------------------------------------------
# 上行：批次
# --------------------------------------------------------------------------
@dataclass
class Batch:
    """一次上传请求的载荷。"""

    batch_id: str
    device_code: str
    kind: str = EventKind.FRAME.value
    created_at: str = field(default_factory=now_utc_iso)
    protocol: str = PROTOCOL_VERSION
    records: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "protocol": self.protocol,
            "batch_id": self.batch_id,
            "device_code": self.device_code,
            "kind": self.kind,
            "created_at": self.created_at,
            "note": self.note,
            "records": self.records,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Batch":
        return cls(
            batch_id=str(d.get("batch_id", "")),
            device_code=str(d.get("device_code", "")),
            kind=str(d.get("kind", EventKind.FRAME.value)),
            created_at=str(d.get("created_at", now_utc_iso())),
            protocol=str(d.get("protocol", PROTOCOL_VERSION)),
            records=list(d.get("records") or []),
            note=str(d.get("note", "")),
        )

    @property
    def record_ids(self) -> list[str]:
        return [str(r.get("record_id", "")) for r in self.records]

    def build_id(self) -> str:
        """按内容算出本批应有的 batch_id，用于自检。"""
        return ids.make_batch_id(self.device_code, self.record_ids)

    def content_digest(self) -> str:
        """整批**数值内容**的摘要（不只是 record_id 集合）。

        为什么需要它：`batch_id` 只由 record_id 集合决定。
        有人截获一个已上传的批次、把某个数值改掉再原样重放，
        `batch_id` 没变，服务端会认为"这批我收过了"直接返回原回执 ——
        数据没被污染（这点是对的），但**平台也不会察觉有人在动手脚**。

        有了这个摘要，重放时数值对不上就能报 409 冲突并留痕。
        它同时也是服务端的自检：客户端若声称"这是刚才那批"，
        就得拿得出同样的内容。
        """
        parts: list[str] = []
        for rec in sorted(self.records, key=lambda r: str(r.get("record_id", ""))):
            vals = rec.get("values") or {}
            parts.append(json.dumps({
                "record_id": rec.get("record_id"),
                "pile_id": rec.get("pile_id"),
                "ts_epoch": rec.get("ts_epoch"),
                "quality": rec.get("quality"),
                "row_hash": rec.get("row_hash"),
                "values": {k: _canon_num(vals.get(k)) for k in PARAM_KEYS},
            }, ensure_ascii=False, sort_keys=True))
        return ids.digest_text("|".join(parts), 32)

    def size_ok(self) -> bool:
        return 0 < len(self.records) <= ids.MAX_BATCH_RECORDS


def _canon_num(v: Any) -> str:
    """数值规范化：1、"1.0"、1.00 得到同一个摘要。

    不这样做的话，客户端用 JSON 发 1.0、服务端回读成 "1"，
    重放时就会被误判成"内容被改过"。
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(float(v))
    s = str(v).strip()
    try:
        return repr(float(s))
    except ValueError:
        return s


# --------------------------------------------------------------------------
# 下行：回执
# --------------------------------------------------------------------------
@dataclass
class Receipt:
    """平台对一次上传的答复。**只有解析出合法回执，终端才会改状态。**"""

    batch_id: str
    results: list[dict[str, Any]] = field(default_factory=list)
    duplicate_batch: bool = False
    server_time: str = field(default_factory=now_utc_iso)
    status: str = BatchStatus.ACKED.value
    protocol: str = PROTOCOL_VERSION
    message: str = ""

    # ---- 计数 ----
    @property
    def accepted(self) -> int:
        return sum(1 for r in self.results if r.get("result") == RecordResult.ACCEPTED.value)

    @property
    def duplicated(self) -> int:
        return sum(1 for r in self.results if r.get("result") == RecordResult.DUPLICATE.value)

    @property
    def rejected(self) -> int:
        return sum(1 for r in self.results if r.get("result") == RecordResult.REJECTED.value)

    @property
    def ok_count(self) -> int:
        """accepted + duplicate 都算"这条数据已经在平台上了"。"""
        return self.accepted + self.duplicated

    def to_dict(self) -> dict:
        return {
            "protocol": self.protocol,
            "batch_id": self.batch_id,
            "status": self.status,
            "duplicate_batch": self.duplicate_batch,
            "server_time": self.server_time,
            "message": self.message,
            "summary": {
                "accepted": self.accepted,
                "duplicate": self.duplicated,
                "rejected": self.rejected,
                "total": len(self.results),
            },
            "results": self.results,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Receipt":
        """从平台返回的 JSON 构造回执。字段缺失或类型不对时抛 ValueError，
        调用方据此把这次上传**当作网络错误**重试，而不是误判为成功。"""
        if not isinstance(d, dict):
            raise ValueError("回执不是 JSON 对象")
        bid = d.get("batch_id")
        if not bid or not isinstance(bid, str):
            raise ValueError("回执缺少 batch_id")
        results = d.get("results")
        if results is None:
            results = []
        if not isinstance(results, list):
            raise ValueError("回执 results 不是数组")
        return cls(
            batch_id=bid,
            results=results,
            duplicate_batch=bool(d.get("duplicate_batch", False)),
            server_time=str(d.get("server_time", now_utc_iso())),
            status=str(d.get("status", BatchStatus.ACKED.value)),
            protocol=str(d.get("protocol", PROTOCOL_VERSION)),
            message=str(d.get("message", "")),
        )


# --------------------------------------------------------------------------
# 记录级结果构造助手
# --------------------------------------------------------------------------
def ok_result(record_id: str, duplicate: bool = False) -> dict:
    return {
        "record_id": record_id,
        "result": (RecordResult.DUPLICATE if duplicate else RecordResult.ACCEPTED).value,
    }


def bad_result(record_id: str, code: ReasonCode | str, message: str) -> dict:
    code_v = code.value if isinstance(code, ReasonCode) else str(code)
    return {
        "record_id": record_id,
        "result": RecordResult.REJECTED.value,
        "reason_code": code_v,
        "message": message,
    }


# --------------------------------------------------------------------------
# 心跳
# --------------------------------------------------------------------------
@dataclass
class Heartbeat:
    """终端每 5 秒上报一次。平台据此判断设备在线与否。"""

    device_code: str
    ts_device: str
    buffer_backlog: int = 0      # 本地待传条数 —— 平台可用它判断"设备在线但数据滞后"
    link_state: str = "online"
    uptime_s: int = 0
    version: str = PROTOCOL_VERSION
    #: 设备当前所在工点。**由设备自报，但平台只认档案里有的工点**
    #: （见 server/api.py 的 heartbeat）：现场在终端上改了工点，平台靠这个
    #: 字段持续对齐，而不是靠一次性提交通知 —— 后者一旦那次请求失败，
    #: 平台上的设备归属就永远停在旧工点，且没人会发现。
    #:
    #: 老终端不发这个字段时为空串，平台保持原有归属不动。
    site_id: str = ""

    def to_dict(self) -> dict:
        return {
            "device_code": self.device_code,
            "ts_device": self.ts_device,
            "buffer_backlog": int(self.buffer_backlog),
            "link_state": self.link_state,
            "uptime_s": int(self.uptime_s),
            "version": self.version,
            "site_id": self.site_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Heartbeat":
        return cls(
            device_code=str(d.get("device_code", "")),
            ts_device=str(d.get("ts_device", now_utc_iso())),
            buffer_backlog=int(d.get("buffer_backlog") or 0),
            link_state=str(d.get("link_state", "online")),
            uptime_s=int(d.get("uptime_s") or 0),
            version=str(d.get("version", PROTOCOL_VERSION)),
            site_id=str(d.get("site_id", "")),
        )


#: 平台判定设备在线的阈值（秒）。心跳间隔 5 秒，留 3 倍余量。
HEARTBEAT_TIMEOUT_S = 15
