# -*- coding: utf-8 -*-
"""ID 生成与校验。

两类 ID，都要求**可重放** —— 同样的输入必须得到同样的 ID，
否则幂等就无从谈起（重传时会生成新 ID，服务端就会当成新数据）。

    record_id  GHP9015H:1770000000000:00042       帧记录
               GHP9015H:rev:1770000000000:00003   修订记录
               GHP9015H:audit:1770000000000:00007 审计事件

    batch_id   GHP9015H.a1b2c3d4e5f6a7b8          内容寻址：由批次内 record_id 集合决定

batch_id 的要点是**内容寻址**而不是随机：同一批数据无论重传多少次，
算出来的 batch_id 都一样，服务端才能认出"这是刚才那批，我已经收过了"，
从而返回原回执而不是重复入库。
"""
from __future__ import annotations

import hashlib
import re
import uuid

#: 单批最大记录数。超过则整批拒收，逼客户端自己分批，
#: 避免一个超大 POST 把服务端内存打满。
MAX_BATCH_RECORDS = 500

_RID_RE = re.compile(r"^(?P<dev>[^:]+):(?:(?P<kind>rev|audit):)?(?P<ts>\d{10,16}):(?P<seq>\d+)$")
_BID_RE = re.compile(r"^(?P<dev>[^.]+)\.(?P<digest>[0-9a-f]{16})$")


# --------------------------------------------------------------------------
# record_id
# --------------------------------------------------------------------------
def frame_record_id(device_code: str, ts_ms: int, seq: int) -> str:
    """一条帧记录的 ID。seq 是设备侧单调递增的帧序号。"""
    return f"{device_code}:{int(ts_ms)}:{int(seq)}"


def revision_record_id(device_code: str, ts_ms: int, seq: int) -> str:
    return f"{device_code}:rev:{int(ts_ms)}:{int(seq)}"


def audit_record_id(device_code: str, ts_ms: int, seq: int) -> str:
    return f"{device_code}:audit:{int(ts_ms)}:{int(seq)}"


def parse_record_id(rid: str) -> dict:
    """拆解 record_id。格式不合法时抛 ValueError。"""
    m = _RID_RE.match(rid or "")
    if not m:
        raise ValueError(f"record_id 格式不合法：{rid!r}")
    return {
        "device_code": m.group("dev"),
        "kind": m.group("kind") or "frame",
        "ts_epoch": int(m.group("ts")),
        "seq": int(m.group("seq")),
    }


def is_valid_record_id(rid: str) -> bool:
    try:
        parse_record_id(rid)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------
# batch_id —— 内容寻址
# --------------------------------------------------------------------------
def batch_digest(record_ids) -> str:
    """批次内容的指纹：对**排序后**的 record_id 集合取 sha1 前 16 位。

    排序是必须的 —— 否则同一批数据换个顺序重传就会算出不同的 batch_id，
    服务端会当成两批，幂等失效。
    """
    joined = ",".join(sorted(str(r) for r in record_ids))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def make_batch_id(device_code: str, record_ids) -> str:
    """生成内容寻址的 batch_id。"""
    return f"{device_code}.{batch_digest(record_ids)}"


def parse_batch_id(bid: str) -> dict:
    m = _BID_RE.match(bid or "")
    if not m:
        raise ValueError(f"batch_id 格式不合法：{bid!r}")
    return {"device_code": m.group("dev"), "digest": m.group("digest")}


def is_valid_batch_id(bid: str) -> bool:
    try:
        parse_batch_id(bid)
        return True
    except ValueError:
        return False


def verify_batch_id(bid: str, record_ids) -> bool:
    """校验 batch_id 是否确实是这批 record_id 的内容寻址结果。

    服务端用它防"客户端伪造 batch_id 覆盖别人的批次"——
    只要 batch_id 与内容对不上，直接拒收。
    """
    try:
        parsed = parse_batch_id(bid)
    except ValueError:
        return False
    return parsed["digest"] == batch_digest(record_ids)


# --------------------------------------------------------------------------
# 通用摘要与短 ID
# --------------------------------------------------------------------------
def digest_text(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def digest_bytes(data: bytes, n: int = 16) -> str:
    return hashlib.sha256(data).hexdigest()[:n]


def sha256_hex(data: bytes) -> str:
    """完整 sha256，用于 U 盘 manifest 的完整性校验。"""
    return hashlib.sha256(data).hexdigest()


def short_uid(prefix: str = "") -> str:
    """随机短 ID，用于审计事件、会话等无需可重放的场合。"""
    return f"{prefix}{uuid.uuid4().hex[:12]}"
