# -*- coding: utf-8 -*-
"""车载终端原始 CSV 的列定义与读写。

约定：

* **表头全 ASCII**，中文名只在界面显示（由 params.py 映射）。理由有两个——
  一是 Windows 上不同区域设置的 Excel 对中文表头编码处理不一致，
  二是列名要直接当 SQLite 列名和字典键用，中文键会让后面每一处都难写。
* **编码 utf-8-sig**：带 BOM，双击用 Excel 打开不乱码。
* **CSV 是不可变原始日志**，落盘后就不再改。筛选、手动上传、30 天清理
  全部走本地 SQLite 索引，因为 Windows 上 Excel 打开 CSV 会锁文件（WinError 32）。
* 每行带 `row_hash`：对内容取摘要。U 盘校对回来的数据若被改过，
  这个值会对不上，服务端据此报 `DUPLICATE_CONFLICT`，而不是默默覆盖。
"""
from __future__ import annotations

import csv
import io
import os
from typing import Any, Iterator

from . import ids
from .params import PARAM_KEYS, PARAM_BY_KEY
from .timeutil import fmt_local, to_datetime

ENCODING = "utf-8-sig"          # 带 BOM，Excel 友好
NEWLINE = ""                    # csv 模块要求

#: 元数据列（不属于 params.py 的参数注册表）
META_COLUMNS: tuple[str, ...] = (
    "record_id",    # 全局唯一，重传时不变
    "device_code",  # 设备编号，如 GHP9015H
    "ts_device",    # 设备采集时间 ISO8601（UTC）
    "ts_epoch",     # 同一时间的 Unix 毫秒，便于排序与索引
    "seq",          # 设备侧帧序号，单调递增
    "pile_id",      # 桩号，只能取自平台下发清单
    "quality",      # 0 正常 / 1 可疑 / 2 失真
    "source",       # can / revision / seed
)

#: 校验列
HASH_COLUMN = "row_hash"

#: 完整表头：元数据 + 16 个参数 + 校验列
HEADER: tuple[str, ...] = META_COLUMNS + tuple(PARAM_KEYS) + (HASH_COLUMN,)

#: 参与 row_hash 计算的列（不含 row_hash 自身）
HASHED_COLUMNS: tuple[str, ...] = META_COLUMNS + tuple(PARAM_KEYS)


# --------------------------------------------------------------------------
# 摘要
# --------------------------------------------------------------------------
def _canon(v: Any) -> str:
    """把单元格值规范化成稳定的字符串。

    数值统一走 float -> repr，这样 "1.50"、1.5、"1.5" 得到同一个摘要，
    不会因为 Excel 顺手改了小数位数就误判成"数据被篡改"。
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
    except (TypeError, ValueError):
        return s


def compute_row_hash(row: dict) -> str:
    """对一行数据取 16 位摘要，顺序固定为 HASHED_COLUMNS。"""
    joined = "|".join(_canon(row.get(c)) for c in HASHED_COLUMNS)
    return ids.digest_text(joined, 16)


# --------------------------------------------------------------------------
# 写
# --------------------------------------------------------------------------
def make_writer(fp: io.TextIOBase) -> csv.DictWriter:
    """在已打开的文件对象上建 writer（不写表头）。"""
    return csv.DictWriter(fp, fieldnames=list(HEADER), extrasaction="ignore")


def open_new(path: str) -> tuple[io.TextIOBase, csv.DictWriter]:
    """新建 CSV 并写好表头。目录不存在会自动创建。返回 (文件对象, writer)。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fp = open(path, "w", encoding=ENCODING, newline=NEWLINE)
    w = make_writer(fp)
    w.writeheader()
    fp.flush()
    return fp, w


def open_append(path: str) -> tuple[io.TextIOBase, csv.DictWriter]:
    """以追加方式打开。文件为空（或不存在）时补写表头。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    need_header = (not os.path.exists(path)) or os.path.getsize(path) == 0
    fp = open(path, "a", encoding=ENCODING, newline=NEWLINE)
    w = make_writer(fp)
    if need_header:
        w.writeheader()
        fp.flush()
    return fp, w


def row_from_frame(rec: dict) -> dict:
    """把一条帧记录（协议里的 dict 形状）转成 CSV 行，并补上 row_hash。"""
    row = {c: rec.get(c, "") for c in META_COLUMNS}
    values = rec.get("values") or {}
    for k in PARAM_KEYS:
        row[k] = values.get(k, "")
    row[HASH_COLUMN] = compute_row_hash(row)
    return row


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------
def validate_header(header: list[str] | None) -> list[str]:
    """检查表头是否与契约一致。返回中文错误列表，空表示通过。"""
    if not header:
        return ["CSV 文件为空或缺少表头"]
    got = [h.strip() for h in header]
    errors: list[str] = []
    missing = [c for c in HEADER if c not in got]
    if missing:
        errors.append("缺少列：" + "、".join(missing))
    extra = [c for c in got if c not in HEADER]
    if extra:
        # 多出来的列不算致命错误（平台可能在后面追加列），但要说清楚
        errors.append("存在未知列（将被忽略）：" + "、".join(extra))
    return errors


def iter_rows(path: str) -> Iterator[dict]:
    """逐行读取 CSV。表头不对时抛 ValueError。"""
    with open(path, "r", encoding=ENCODING, newline=NEWLINE) as fp:
        reader = csv.DictReader(fp)
        errs = validate_header(reader.fieldnames)
        fatal = [e for e in errs if e.startswith("缺少列")]
        if fatal:
            raise ValueError(f"{os.path.basename(path)}：{fatal[0]}")
        for raw in reader:
            if not raw or not (raw.get("record_id") or "").strip():
                continue  # 跳过空行（Excel 常在末尾留一行）
            yield {k: (v if v is not None else "") for k, v in raw.items()}


def frame_from_row(row: dict) -> dict:
    """把 CSV 行转回帧记录 dict（协议形状）。字段转换失败时抛 ValueError。"""
    values: dict[str, float] = {}
    for k in PARAM_KEYS:
        raw = row.get(k, "")
        if raw is None or str(raw).strip() == "":
            raise ValueError(f"{PARAM_BY_KEY[k].name} 为空")
        try:
            values[k] = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{PARAM_BY_KEY[k].name} 不是数值：{raw!r}") from None

    try:
        quality = int(float(row.get("quality") or 0))
    except (TypeError, ValueError):
        raise ValueError(f"quality 不是整数：{row.get('quality')!r}") from None

    try:
        seq = int(float(row.get("seq") or 0))
    except (TypeError, ValueError):
        raise ValueError(f"seq 不是整数：{row.get('seq')!r}") from None

    ts_device = str(row.get("ts_device") or "").strip()
    ts_epoch = row.get("ts_epoch") or ""
    if not ts_device:
        raise ValueError("ts_device 为空")
    try:
        dt = to_datetime(ts_device)
    except ValueError:
        raise ValueError(f"ts_device 不是合法时间：{ts_device!r}") from None
    if not ts_epoch:
        ts_epoch = int(dt.timestamp() * 1000)

    return {
        "record_id": str(row.get("record_id") or "").strip(),
        "device_code": str(row.get("device_code") or "").strip(),
        "ts_device": dt.isoformat(timespec="milliseconds"),
        "ts_epoch": int(float(ts_epoch)),
        "seq": seq,
        "pile_id": str(row.get("pile_id") or "").strip(),
        "quality": quality,
        "source": str(row.get("source") or "can").strip() or "can",
        "values": values,
        "row_hash": str(row.get(HASH_COLUMN) or "").strip(),
    }


def row_is_intact(row: dict) -> bool:
    """行内摘要与内容是否对得上。没有摘要列时视为不可判定，返回 True。"""
    stored = str(row.get(HASH_COLUMN) or "").strip()
    if not stored:
        return True
    return stored == compute_row_hash(row)


def human_preview(row: dict, limit: int = 5) -> str:
    """把一行渲染成一句人话，用于冲突报告。"""
    parts = []
    for k in PARAM_KEYS[:limit]:
        p = PARAM_BY_KEY[k]
        parts.append(f"{p.name}={row.get(k, '')}{p.unit}")
    when = ""
    if row.get("ts_device"):
        try:
            when = fmt_local(row["ts_device"])
        except ValueError:
            when = str(row.get("ts_device"))
    return f"{row.get('record_id', '?')} @ {when}  " + "，".join(parts)
