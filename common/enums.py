# -*- coding: utf-8 -*-
"""全局状态枚举。所有状态流转都必须使用这里的枚举，不允许裸字符串。"""
from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """可 JSON 序列化的字符串枚举（3.11 起 stdlib 有 StrEnum，这里保留兼容写法）。"""

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志可读性
        return self.value


class LinkState(StrEnum):
    """网络链路状态。只由 terminal/network.py 单线程推进。"""

    ONLINE = "online"      # 最近一次探测成功
    DEGRADED = "degraded"  # 链路在，但连续失败 >= 2
    OFFLINE = "offline"    # 手动置为断网，或连续失败 >= 5

    @property
    def label(self) -> str:
        return {"online": "在线", "degraded": "弱网", "offline": "离线"}[self.value]


class UploadStatus(StrEnum):
    """单条记录的上传状态（终端 local.db outbox 行级）。"""

    PENDING = "pending"    # 待上传
    INFLIGHT = "inflight"  # 已被某批次占用，等待回执
    UPLOADED = "uploaded"  # 回执确认（accepted 与 duplicate 都算）
    REJECTED = "rejected"  # 平台明确拒收，永久失败，需人工校对
    VOID = "void"          # 人工标记无效，不再上传

    @property
    def label(self) -> str:
        return {
            "pending": "未上传",
            "inflight": "上传中",
            "uploaded": "已上传",
            "rejected": "已拒收",
            "void": "已作废",
        }[self.value]


class BatchStatus(StrEnum):
    """批次状态（终端 local_batches 行级）。"""

    SENDING = "sending"
    ACKED = "acked"
    PARTIAL = "partial"
    LOST = "lost"


class RecordResult(StrEnum):
    """服务端对单条记录的判定结果。"""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


class Quality(int, Enum):
    """数据质量。SUSPECT 由系统自动标记，BAD 需人工确认。"""

    OK = 0
    SUSPECT = 1
    BAD = 2

    @property
    def label(self) -> str:
        return {0: "正常", 1: "可疑", 2: "失真"}[int(self)]


class Source(StrEnum):
    """记录来源。"""

    CAN = "can"          # 设备总线直采
    REVISION = "revision"  # U 盘校对后重新导入
    SEED = "seed"        # 演示种子数据


class ReasonCode(StrEnum):
    """服务端拒收原因码 / 修订原因码，服务端为唯一权威。"""

    # —— 拒收原因 ——
    SCHEMA = "SCHEMA"                        # 字段缺失或类型错误
    RANGE = "RANGE"                          # 数值超出物理量程
    TS_FUTURE = "TS_FUTURE"                  # 时间戳超前于服务器
    PILE_NOT_ISSUED = "PILE_NOT_ISSUED"      # 桩号不在平台下发清单内
    SITE_NOT_ISSUED = "SITE_NOT_ISSUED"      # 工点不在平台档案内（现场录入被拒）
    DUPLICATE_CONFLICT = "DUPLICATE_CONFLICT"  # 同 record_id 但内容不一致
    BATCH_TOO_LARGE = "BATCH_TOO_LARGE"      # 单批记录数超限

    # —— 修订原因 ——
    SENSOR_FAULT = "SENSOR_FAULT"            # 传感器故障
    VALUE_DISTORTED = "VALUE_DISTORTED"      # 数据失真
    PILE_CORRECTION = "PILE_CORRECTION"      # 桩号更正（仍须在平台清单内）
    OTHER = "OTHER"

    @property
    def label(self) -> str:
        return {
            "SCHEMA": "字段格式错误",
            "RANGE": "数值超量程",
            "TS_FUTURE": "时间戳超前",
            "PILE_NOT_ISSUED": "桩号未由平台下发",
            "SITE_NOT_ISSUED": "工点未由平台建档",
            "DUPLICATE_CONFLICT": "同 ID 内容冲突",
            "BATCH_TOO_LARGE": "单批记录数超限",
            "SENSOR_FAULT": "传感器故障",
            "VALUE_DISTORTED": "数据失真",
            "PILE_CORRECTION": "桩号更正",
            "OTHER": "其他",
        }[self.value]


class AlarmLevel(StrEnum):
    """报警级别。"""

    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def label(self) -> str:
        return {"warning": "预警", "critical": "报警"}[self.value]


class EventKind(StrEnum):
    """上传批次类型。"""

    FRAME = "frame"
    REVISION = "revision"
    CYCLE = "cycle"
    AUDIT = "audit"
