# -*- coding: utf-8 -*-
"""平台侧数据库：建表、连接管理、原子写入。

约定：

* **连接按线程持有**（`threading.local`）。本机 SQLite 编译为 `threadsafety=3`
  （serialized），但仍按线程分开连接 —— 一是避免跨线程用同一个连接导致
  `ProgrammingError`，二是让每个线程有独立事务边界。
* **WAL + busy_timeout=5000**：读（页面轮询）与写（上传入库）不会互相阻塞到报
  `database is locked`。
* **原始值永不覆盖**。`frames` 里的参数列一旦写入就不再改；校对过的数据写进
  `frame_revisions`，`frames.rev_count` / `frames.rev_json` 只是它的缓存投影，
  任何时候都能从修订表重建。平台展示的"有效值 = 原始值 + 最新修订"。
* 所有时间既有 `ts_device`（设备采集，权威）也有 `ts_server`（平台入库，仅审计）。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any

from common import config, sqlite_util
from common.params import PARAM_KEYS

log = logging.getLogger("server.db")

SCHEMA_VERSION = 1

#: 帧表的参数列（与 params.py 严格对齐，由建表语句自动生成）
PARAM_COLUMNS: tuple[str, ...] = tuple(PARAM_KEYS)

#: 帧表完整列顺序 —— 批量插入时按这个顺序拼 SQL
FRAME_COLUMNS: tuple[str, ...] = (
    "record_id", "device_code", "ts_device", "ts_epoch", "seq",
    "pile_id", "quality", "source", "row_hash",
    "batch_id", "ts_server",
    "rev_count", "rev_json", "latest_rev_id", "last_rev_at",
) + PARAM_COLUMNS


def _param_ddl() -> str:
    return ",\n    ".join(f"{k} REAL" for k in PARAM_COLUMNS)


SCHEMA_SQL: tuple[str, ...] = (
    # ---------------- 帧数据 ----------------
    f"""
    CREATE TABLE IF NOT EXISTS frames (
        record_id     TEXT PRIMARY KEY,
        device_code   TEXT NOT NULL,
        ts_device     TEXT NOT NULL,
        ts_epoch      INTEGER NOT NULL,
        seq           INTEGER NOT NULL,
        pile_id       TEXT NOT NULL DEFAULT '',
        quality       INTEGER NOT NULL DEFAULT 0,
        source        TEXT NOT NULL DEFAULT 'can',
        row_hash      TEXT NOT NULL DEFAULT '',
        batch_id      TEXT NOT NULL DEFAULT '',
        ts_server     TEXT NOT NULL,
        -- 修订投影：原始参数列永不改动，这里是叠加层
        rev_count     INTEGER NOT NULL DEFAULT 0,
        rev_json      TEXT,
        latest_rev_id TEXT,
        last_rev_at   TEXT,
        {_param_ddl()}
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_frames_dev_ts ON frames(device_code, ts_epoch)",
    "CREATE INDEX IF NOT EXISTS ix_frames_pile ON frames(device_code, pile_id, ts_epoch)",
    "CREATE INDEX IF NOT EXISTS ix_frames_batch ON frames(batch_id)",

    # ---------------- 批次 ----------------
    """
    CREATE TABLE IF NOT EXISTS batches (
        batch_id      TEXT PRIMARY KEY,
        device_code   TEXT NOT NULL,
        kind          TEXT NOT NULL DEFAULT 'frame',
        created_at    TEXT NOT NULL DEFAULT '',
        ts_server     TEXT NOT NULL,
        record_count  INTEGER NOT NULL DEFAULT 0,
        accepted      INTEGER NOT NULL DEFAULT 0,
        duplicated    INTEGER NOT NULL DEFAULT 0,
        rejected      INTEGER NOT NULL DEFAULT 0,
        status        TEXT NOT NULL DEFAULT 'acked',
        note          TEXT NOT NULL DEFAULT '',
        receipt_json  TEXT,
        -- 整批数值内容的摘要。重放同一 batch_id 时用它比对，
        -- 数值对不上就报 409，而不是静默返回原回执放过一次篡改尝试。
        content_digest TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_batches_dev ON batches(device_code, ts_server)",

    # ---------------- 工点档案（平台签发，终端只能引用） ----------------
    """
    CREATE TABLE IF NOT EXISTS sites (
        site_id       TEXT PRIMARY KEY,
        site_name     TEXT NOT NULL DEFAULT '',
        project_name  TEXT NOT NULL DEFAULT '',
        tunnel        TEXT NOT NULL DEFAULT '',
        line          TEXT NOT NULL DEFAULT '',
        active        INTEGER NOT NULL DEFAULT 1,
        created_at    TEXT NOT NULL DEFAULT ''
    )
    """,

    # ---------------- 桩号清单（平台下发，终端只能从中选） ----------------
    """
    CREATE TABLE IF NOT EXISTS piles (
        pile_id     TEXT PRIMARY KEY,
        site_id     TEXT NOT NULL DEFAULT '',
        line        TEXT NOT NULL DEFAULT '',
        mileage_m   REAL NOT NULL DEFAULT 0,
        label       TEXT NOT NULL DEFAULT '',
        issued_at   TEXT NOT NULL DEFAULT '',
        active      INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_piles_site ON piles(site_id, mileage_m)",

    # ---------------- 设备 ----------------
    """
    CREATE TABLE IF NOT EXISTS devices (
        device_code   TEXT PRIMARY KEY,
        device_name   TEXT NOT NULL DEFAULT '',
        model         TEXT NOT NULL DEFAULT '',
        site_id       TEXT NOT NULL DEFAULT '',
        site_name     TEXT NOT NULL DEFAULT '',
        last_seen     TEXT,
        link_state    TEXT NOT NULL DEFAULT 'offline',
        backlog       INTEGER NOT NULL DEFAULT 0,
        total_frames  INTEGER NOT NULL DEFAULT 0,
        version       TEXT NOT NULL DEFAULT '',
        note          TEXT NOT NULL DEFAULT ''
    )
    """,

    # ---------------- 报警 ----------------
    """
    CREATE TABLE IF NOT EXISTS alarms (
        alarm_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        device_code   TEXT NOT NULL,
        param_key     TEXT NOT NULL,
        level         TEXT NOT NULL,
        started_at    TEXT NOT NULL,
        ended_at      TEXT,
        peak_value    REAL,
        peak_at       TEXT,
        threshold_lo  REAL,
        threshold_hi  REAL,
        breach_count  INTEGER NOT NULL DEFAULT 1,
        message       TEXT NOT NULL DEFAULT '',
        pile_id       TEXT NOT NULL DEFAULT '',
        acked         INTEGER NOT NULL DEFAULT 0
    )
    """,
    # 关键约束：同一 (设备, 参数) 同时只允许一条未解除的报警。
    # 用部分唯一索引在数据库层面钉死 —— 哪怕判定逻辑写错了也不会刷屏。
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_alarms_active
        ON alarms(device_code, param_key) WHERE ended_at IS NULL
    """,
    "CREATE INDEX IF NOT EXISTS ix_alarms_dev ON alarms(device_code, started_at DESC)",

    # ---------------- 修订（只追加，不覆盖） ----------------
    """
    CREATE TABLE IF NOT EXISTS frame_revisions (
        revision_id   TEXT PRIMARY KEY,
        record_id     TEXT NOT NULL,
        device_code   TEXT NOT NULL,
        patch_json    TEXT NOT NULL,
        reason_code   TEXT NOT NULL,
        reason_note   TEXT NOT NULL DEFAULT '',
        operator_name TEXT NOT NULL DEFAULT '',
        ts_device     TEXT NOT NULL DEFAULT '',
        ts_server     TEXT NOT NULL,
        batch_id      TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_rev_record ON frame_revisions(record_id, ts_server)",
    "CREATE INDEX IF NOT EXISTS ix_rev_dev ON frame_revisions(device_code, ts_server DESC)",

    # ---------------- 审计事件 ----------------
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        event_id     TEXT PRIMARY KEY,
        device_code  TEXT NOT NULL,
        kind         TEXT NOT NULL,
        ts_device    TEXT NOT NULL DEFAULT '',
        ts_epoch     INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '{}',
        ts_server    TEXT NOT NULL,
        batch_id     TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_audit_dev ON audit_events(device_code, ts_epoch DESC)",

    # ---------------- 拒收明细（给人工校对用） ----------------
    """
    CREATE TABLE IF NOT EXISTS rejects (
        reject_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id     TEXT NOT NULL DEFAULT '',
        record_id    TEXT NOT NULL DEFAULT '',
        device_code  TEXT NOT NULL DEFAULT '',
        reason_code  TEXT NOT NULL DEFAULT '',
        message      TEXT NOT NULL DEFAULT '',
        payload_json TEXT,
        ts_server    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_rejects_dev ON rejects(device_code, ts_server DESC)",

    # ---------------- 日志数据（按喷浆循环生成，字段见 params.LOG_FIELDS） ----------------
    """
    CREATE TABLE IF NOT EXISTS cycles (
        cycle_id     TEXT PRIMARY KEY,
        device_code  TEXT NOT NULL,
        pile_id      TEXT NOT NULL DEFAULT '',
        started_at   TEXT NOT NULL,
        ended_at     TEXT,
        ts_epoch     INTEGER NOT NULL DEFAULT 0,
        volume_m3    REAL NOT NULL DEFAULT 0,
        frame_count  INTEGER NOT NULL DEFAULT 0,
        avg_pressure REAL,
        avg_accel    REAL,
        max_pressure REAL,
        band_json    TEXT,
        batch_id     TEXT NOT NULL DEFAULT '',
        -- 日志数据身份快照与里程/用量字段
        workAreaId   TEXT NOT NULL DEFAULT '',
        cname        TEXT NOT NULL DEFAULT '',
        cphone       TEXT NOT NULL DEFAULT '',
        cidno        TEXT NOT NULL DEFAULT '',
        locationMetre REAL,
        endMetre     REAL,
        actualSprayingVolume REAL,
        acceleratorOnceDosage REAL,
        acceleratorCumulativeDosage REAL,
        sprayLocation TEXT NOT NULL DEFAULT '',
        accumulatedWorkingHours REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_cycles_dev ON cycles(device_code, started_at DESC)",

    # ---------------- 杂项 ----------------
    """
    CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY,
        v TEXT NOT NULL DEFAULT ''
    )
    """,
)


# --------------------------------------------------------------------------
# 连接（实现见 common/sqlite_util.py，与终端共用一份）
# --------------------------------------------------------------------------
_conns = sqlite_util.ThreadLocalConns("platform")

#: 显式事务。连接是 autocommit 模式，必须手动 BEGIN/COMMIT。
transaction = sqlite_util.transaction
insert_many = sqlite_util.insert_many


def connect(path: str | None = None) -> sqlite3.Connection:
    """新建一个连接并设好 pragma。调用方负责关闭。"""
    return sqlite_util.connect(path or config.SERVER_DB)


def get_conn(path: str | None = None) -> sqlite3.Connection:
    """取当前线程的连接，没有就建一个。"""
    return _conns.get(path or config.SERVER_DB)


def close_conn() -> None:
    """关闭当前线程的连接。线程退出前调用。"""
    _conns.close()


# --------------------------------------------------------------------------
# 建表 / 重置
# --------------------------------------------------------------------------
#: 后加的列：(表, 列, 列定义)。`CREATE TABLE IF NOT EXISTS` 不会给已存在的表补列，
#: 所以老的演示库需要用 ALTER 补上。演示机可能已经跑过一次，不能假设是空库。
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("batches", "content_digest", "TEXT NOT NULL DEFAULT ''"),
    # 日志数据字段（2026-10 字段表更替）
    ("cycles", "workAreaId", "TEXT NOT NULL DEFAULT ''"),
    ("cycles", "cname", "TEXT NOT NULL DEFAULT ''"),
    ("cycles", "cphone", "TEXT NOT NULL DEFAULT ''"),
    ("cycles", "cidno", "TEXT NOT NULL DEFAULT ''"),
    ("cycles", "locationMetre", "REAL"),
    ("cycles", "endMetre", "REAL"),
    ("cycles", "actualSprayingVolume", "REAL"),
    ("cycles", "acceleratorOnceDosage", "REAL"),
    ("cycles", "acceleratorCumulativeDosage", "REAL"),
    ("cycles", "sprayLocation", "TEXT NOT NULL DEFAULT ''"),
    ("cycles", "accumulatedWorkingHours", "REAL"),
)


def init_db(path: str | None = None) -> sqlite3.Connection:
    """建表 + 补列（幂等）。返回一个可用连接。"""
    conn = get_conn(path)
    cur = conn.cursor()
    for stmt in SCHEMA_SQL:
        cur.execute(stmt)
    cur.close()
    added = sqlite_util.migrate(conn, ADDED_COLUMNS)
    if added:
        log.info("数据库补列：%s", "、".join(added))
    conn.execute(
        "INSERT INTO meta(k, v) VALUES('schema_version', ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (str(SCHEMA_VERSION),),
    )
    return conn


def reset_db(path: str | None = None) -> sqlite3.Connection:
    """删库重建。**只在 --reset 时调用**，会丢掉所有数据。"""
    p = os.path.abspath(path or config.SERVER_DB)
    close_conn()
    for suffix in ("", "-wal", "-shm"):
        f = p + suffix
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass
    return init_db(p)


def table_counts(path: str | None = None) -> dict[str, int]:
    """各表行数，用于启动自检和演示前检查。"""
    return sqlite_util.table_counts(get_conn(path))


# --------------------------------------------------------------------------
# 写入助手
# --------------------------------------------------------------------------
def frame_row(rec: dict, batch_id: str, ts_server: str) -> tuple:
    """把一条帧记录 dict 转成与 FRAME_COLUMNS 对齐的元组。"""
    values: dict[str, Any] = rec.get("values") or {}
    base = (
        rec.get("record_id", ""),
        rec.get("device_code", ""),
        rec.get("ts_device", ""),
        int(rec.get("ts_epoch", 0)),
        int(rec.get("seq", 0)),
        rec.get("pile_id", ""),
        int(rec.get("quality", 0)),
        rec.get("source", "can"),
        rec.get("row_hash", ""),
        batch_id,
        ts_server,
        0,          # rev_count
        None,       # rev_json
        None,       # latest_rev_id
        None,       # last_rev_at
    )
    return base + tuple(_num(values.get(k)) for k in PARAM_COLUMNS)


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default
