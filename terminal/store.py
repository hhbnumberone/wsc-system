# -*- coding: utf-8 -*-
"""车载终端本地存储：原始 CSV + 上传 outbox。

职责分三层，互相不越界：

1. **CSV 原始日志** `data/terminal/frames/frames_YYYYMMDD_HH.csv`
   不可变、带 BOM（Excel 双击不乱码）、按**本地时间**小时轮转。
   这是"本地存储 30 天"这个功能的本体，也是 U 盘导出的数据源。
2. **outbox** `data/terminal/local.db` 的 `outbox` 表
   一条记录一行，连同 16 个参数值一起存。**上传只读 SQLite，不读 CSV** ——
   Windows 上 Excel 打开 CSV 会锁文件（WinError 32），上传链路不能依赖
   一个用户随时可能双击打开的文件。
3. **账本表** `local_batches` / `upload_log` / `revision_queue` / `audit_queue`
   记录每一次上传尝试、每一条修订和审计事件，供终端界面展示与补传。

本模块的核心价值是把三条安全规则钉死，`cleanup()` 和 `reconcile()` 里各写了一半：

* **清理永不删未上传的数据** —— 那份 CSV 是这条数据唯一的副本；
* **INFLIGHT 是会被崩掉的** —— 启动时把超时的 INFLIGHT 收回 PENDING；
* **回执到了但状态没改完也是会崩的** —— 用存下来的回执把行补齐，而不是重传。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Iterable, Iterator, Sequence

from common import config, csv_schema, sqlite_util
from common.enums import BatchStatus, EventKind, RecordResult, UploadStatus
from common.ids import MAX_BATCH_RECORDS
from common.params import PARAM_BY_KEY, PARAM_KEYS, check_value
from common.protocol import Receipt
from common.timeutil import (CST, now_epoch, now_utc_iso, start_of_local_day,
                             to_datetime)

log = logging.getLogger("terminal.store")

SCHEMA_VERSION = 1

#: 参数列（与 params.py 严格对齐）
PARAM_COLUMNS: tuple[str, ...] = tuple(PARAM_KEYS)

#: outbox 的身份列
OUTBOX_BASE: tuple[str, ...] = (
    "record_id", "device_code", "ts_device", "ts_epoch", "seq",
    "pile_id", "quality", "source", "row_hash",
)

#: outbox 的状态列
OUTBOX_STATE: tuple[str, ...] = (
    "status", "csv_file", "batch_id", "attempts", "next_attempt_at",
    "manual", "last_error", "reject_code", "reject_note",
    "created_at", "inflight_at", "uploaded_at",
)

OUTBOX_COLUMNS: tuple[str, ...] = OUTBOX_BASE + PARAM_COLUMNS + OUTBOX_STATE

#: 这些状态的记录**还没到平台上**，它们连带的 CSV 一律不许删。
OPEN_STATUSES: tuple[str, ...] = (
    UploadStatus.PENDING.value,
    UploadStatus.INFLIGHT.value,
    UploadStatus.REJECTED.value,
)

_FILE_GLOB = "frames_*.csv"

#: CSV 文件名里的小时键，用于判断文件属于哪个小时
_NAME_FMT = "%Y%m%d_%H"


def _param_ddl() -> str:
    return ",\n        ".join(f"{k} REAL" for k in PARAM_COLUMNS)


SCHEMA_SQL: tuple[str, ...] = (
    # ---------------- outbox：一条记录一行 ----------------
    f"""
    CREATE TABLE IF NOT EXISTS outbox (
        record_id     TEXT PRIMARY KEY,
        device_code   TEXT NOT NULL,
        ts_device     TEXT NOT NULL,
        ts_epoch      INTEGER NOT NULL,
        seq           INTEGER NOT NULL,
        pile_id       TEXT NOT NULL DEFAULT '',
        quality       INTEGER NOT NULL DEFAULT 0,
        source        TEXT NOT NULL DEFAULT 'can',
        row_hash      TEXT NOT NULL DEFAULT '',
        {_param_ddl()},
        status        TEXT NOT NULL DEFAULT 'pending',
        csv_file      TEXT NOT NULL DEFAULT '',
        batch_id      TEXT NOT NULL DEFAULT '',
        attempts      INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL DEFAULT 0,
        manual        INTEGER NOT NULL DEFAULT 0,
        last_error    TEXT NOT NULL DEFAULT '',
        reject_code   TEXT NOT NULL DEFAULT '',
        reject_note   TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL,
        inflight_at   TEXT,
        uploaded_at   TEXT
    )
    """,
    # 取待传批次走这个索引：状态 + 退避时间 + 时间顺序
    "CREATE INDEX IF NOT EXISTS ix_outbox_queue"
    " ON outbox(status, next_attempt_at, ts_epoch, seq)",
    "CREATE INDEX IF NOT EXISTS ix_outbox_batch ON outbox(batch_id)",
    # 清理时要按 csv_file 反查有没有未上传的行
    "CREATE INDEX IF NOT EXISTS ix_outbox_csv ON outbox(csv_file, status)",
    "CREATE INDEX IF NOT EXISTS ix_outbox_ts ON outbox(ts_epoch)",

    # ---------------- 批次账本 ----------------
    """
    CREATE TABLE IF NOT EXISTS local_batches (
        batch_id       TEXT PRIMARY KEY,
        device_code    TEXT NOT NULL,
        kind           TEXT NOT NULL DEFAULT 'frame',
        created_at     TEXT NOT NULL,
        finished_at    TEXT,
        record_count   INTEGER NOT NULL DEFAULT 0,
        accepted       INTEGER NOT NULL DEFAULT 0,
        duplicated     INTEGER NOT NULL DEFAULT 0,
        rejected       INTEGER NOT NULL DEFAULT 0,
        status         TEXT NOT NULL DEFAULT 'sending',
        attempts       INTEGER NOT NULL DEFAULT 0,
        content_digest TEXT NOT NULL DEFAULT '',
        receipt_json   TEXT,
        note           TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_lbatch_status"
    " ON local_batches(status, created_at DESC)",

    # ---------------- 上传日志（终端界面"上传记录"页） ----------------
    """
    CREATE TABLE IF NOT EXISTS upload_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT NOT NULL,
        batch_id     TEXT NOT NULL DEFAULT '',
        kind         TEXT NOT NULL DEFAULT 'frame',
        record_count INTEGER NOT NULL DEFAULT 0,
        status       TEXT NOT NULL DEFAULT '',
        http_status  INTEGER NOT NULL DEFAULT 0,
        accepted     INTEGER NOT NULL DEFAULT 0,
        duplicated   INTEGER NOT NULL DEFAULT 0,
        rejected     INTEGER NOT NULL DEFAULT 0,
        elapsed_ms   INTEGER NOT NULL DEFAULT 0,
        message      TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ulog_ts ON upload_log(ts DESC)",

    # ---------------- 修订队列（U 盘校对后待上传） ----------------
    """
    CREATE TABLE IF NOT EXISTS revision_queue (
        revision_id   TEXT PRIMARY KEY,
        target_id     TEXT NOT NULL,
        device_code   TEXT NOT NULL,
        patch_json    TEXT NOT NULL,
        reason_code   TEXT NOT NULL,
        reason_note   TEXT NOT NULL DEFAULT '',
        operator_name TEXT NOT NULL DEFAULT '',
        ts_device     TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT 'pending',
        attempts      INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL DEFAULT 0,
        batch_id      TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL,
        uploaded_at   TEXT,
        last_error    TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_revq_status"
    " ON revision_queue(status, next_attempt_at, created_at)",

    # ---------------- 审计队列（导出/导入/手动上传的留痕） ----------------
    """
    CREATE TABLE IF NOT EXISTS audit_queue (
        event_id      TEXT PRIMARY KEY,
        device_code   TEXT NOT NULL,
        kind          TEXT NOT NULL,
        ts_device     TEXT NOT NULL DEFAULT '',
        ts_epoch      INTEGER NOT NULL DEFAULT 0,
        payload_json  TEXT NOT NULL DEFAULT '{}',
        status        TEXT NOT NULL DEFAULT 'pending',
        attempts      INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL DEFAULT 0,
        batch_id      TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL,
        uploaded_at   TEXT,
        last_error    TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_auditq_status"
    " ON audit_queue(status, next_attempt_at, created_at)",

    # ---------------- 平台下发的桩号清单（离线也能校验） ----------------
    """
    CREATE TABLE IF NOT EXISTS piles (
        pile_id   TEXT PRIMARY KEY,
        site_id   TEXT NOT NULL DEFAULT '',
        line      TEXT NOT NULL DEFAULT '',
        mileage_m REAL NOT NULL DEFAULT 0,
        label     TEXT NOT NULL DEFAULT '',
        issued_at TEXT NOT NULL DEFAULT '',
        active    INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_piles_site ON piles(site_id, mileage_m)",

    """
    CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY,
        v TEXT NOT NULL DEFAULT ''
    )
    """,
)

#: 后加的列：(表, 列, 列定义)。演示机上可能已经有旧的 local.db。
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = ()


# --------------------------------------------------------------------------
# CSV 原始日志
# --------------------------------------------------------------------------
class CsvLog:
    """按小时轮转的原始 CSV 日志。

    写入是**追加 + 立即 flush**：进程被杀不丢已写行（只有断电才会丢，
    那属于操作系统页缓存，超出本原型要处理的范围）。
    读取每次重新打开文件 —— 绝不缓存文件句柄，因为用户随时可能在 Excel 里
    打开或关闭这些文件。
    """

    def __init__(self, frames_dir: str | None = None):
        self.dir = os.path.abspath(frames_dir or config.FRAMES_DIR)
        self._fp = None
        self._writer = None
        self._key = ""          # 当前打开的文件对应的小时键
        self._rows = 0          # 本次进程写入的行数
        # 用 RLock 而不是 Lock：`_rotate()` 会在已持锁的 `append_many` 里被调用，
        # 而它需要先关掉旧文件。普通 Lock 在这里会自己把自己锁死。
        self._lock = threading.RLock()

    # ---- 路径 ----
    @staticmethod
    def hour_key(ts: Any) -> str:
        """记录时间对应的本地小时，如 20261005_16。

        用**本地时间**分小时，是为了跟人看的"今天/昨天"对得上；
        跨小时换文件，单文件不会无限长大。
        """
        return to_datetime(ts).astimezone(CST).strftime(_NAME_FMT)

    def name_for(self, ts: Any) -> str:
        return f"frames_{self.hour_key(ts)}.csv"

    def path_for(self, ts: Any) -> str:
        return os.path.join(self.dir, self.name_for(ts))

    def path_of(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def files(self) -> list[str]:
        """按文件名（= 时间）排序的全部 CSV 路径。"""
        try:
            names = os.listdir(self.dir)
        except OSError:
            return []
        got = [n for n in names if n.startswith("frames_") and n.endswith(".csv")]
        return [os.path.join(self.dir, n) for n in sorted(got)]

    @staticmethod
    def day_of(name: str) -> str:
        """从文件名取出日期 'YYYY-MM-DD'。取不到就返回空串。"""
        base = os.path.basename(name)
        if len(base) >= len("frames_YYYYMMDD_HH.csv"):
            d = base[len("frames_"):len("frames_") + 8]
            if d.isdigit():
                return f"{d[:4]}-{d[4:6]}-{d[6:]}"
        return ""

    # ---- 写 ----
    def append(self, record: dict) -> str:
        """追加一条记录，返回所在的 CSV 文件名。"""
        return self.append_many([record])

    def append_many(self, records: Sequence[dict]) -> str:
        """批量追加（同一小时内的记录写进同一个文件）。返回最后写入的文件名。"""
        name = ""
        with self._lock:
            for rec in records:
                name = self.name_for(rec.get("ts_device") or rec.get("ts_epoch") or now_utc_iso())
                if self._writer is None or name != self._key:
                    self._rotate(name)
                self._writer.writerow(csv_schema.row_from_frame(rec))
                self._rows += 1
            if self._writer is not None:
                # 立即 flush：崩溃时最多丢掉"正在写的那一行"
                self._fp.flush()
        return name

    def _rotate(self, name: str) -> None:
        """换到指定的 CSV 文件。**调用方保证已持锁**，所以只调 _close_locked。"""
        self._close_locked()
        self._fp, self._writer = csv_schema.open_append(self.path_of(name))
        self._key = name
        log.debug("CSV 切到 %s", name)

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._fp is not None:
            try:
                self._fp.flush()
                self._fp.close()
            except OSError:  # pragma: no cover - 关机时文件已被系统回收
                pass
            self._fp = None
            self._writer = None
            self._key = ""

    @property
    def rows_written(self) -> int:
        return self._rows

    @property
    def current_name(self) -> str:
        """当前正在写的文件名（没有打开文件时为空串）。"""
        return self._key

    @property
    def is_open(self) -> bool:
        return self._fp is not None

    # ---- 读 ----
    def iter_file(self, path: str) -> Iterator[dict]:
        """逐行读一个 CSV（返回 CSV 行 dict，不是帧记录）。"""
        return csv_schema.iter_rows(path)

    def read_day(self, day: str) -> list[dict]:
        """读某一天（'YYYY-MM-DD'）的全部行。"""
        out: list[dict] = []
        for p in self.files():
            if self.day_of(p) != day:
                continue
            try:
                out.extend(self.iter_file(p))
            except (OSError, ValueError) as exc:
                log.warning("跳过读不动的 CSV %s：%s", os.path.basename(p), exc)
        return out

    def days(self) -> list[str]:
        """本地已有的数据日期，倒序。"""
        seen = {self.day_of(p) for p in self.files()}
        return sorted((d for d in seen if d), reverse=True)


# --------------------------------------------------------------------------
# 本地库
# --------------------------------------------------------------------------
class LocalStore:
    """终端的本地库：outbox + 账本 + 桩号缓存。

    连接按线程持有（见 `common/sqlite_util.py`）。**写操作统一走 `self._lock`**，
    因为上传 worker 和界面线程都会改 outbox，而"挑批 + 占行"必须是原子的。
    """

    def __init__(self, path: str | None = None, frames_dir: str | None = None,
                 cfg: dict | None = None):
        self.cfg = cfg or config.load_config()
        self.path = os.path.abspath(path or config.LOCAL_DB)
        self.csv = CsvLog(frames_dir)
        self._conns = sqlite_util.ThreadLocalConns("terminal")
        self._lock = threading.RLock()
        self._init_schema()

    # ---- 连接 ----
    @property
    def conn(self):
        return self._conns.get(self.path)

    def close(self) -> None:
        self.csv.close()
        self._conns.close()

    def close_thread_conn(self) -> None:
        """关掉**当前线程**持有的那条连接（后台线程退出时调用）。

        连接是按线程持有的，`close()` 只能关掉调用它的那个线程的 ——
        后台线程一死，它的 thread-local 连接就没人管了，只能等 GC 回收，
        期间会甩出一条 `ResourceWarning`。**这不等于关库**：store 归 app 所有，
        采集/上传线程退出了，界面线程还在用它。
        """
        self._conns.close()

    def __enter__(self) -> "LocalStore":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def _init_schema(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        conn = self.conn
        cur = conn.cursor()
        for stmt in SCHEMA_SQL:
            cur.execute(stmt)
        cur.close()
        added = sqlite_util.migrate(conn, ADDED_COLUMNS)
        if added:
            log.info("本地库补列：%s", "、".join(added))
        conn.execute(
            "INSERT INTO meta(k, v) VALUES('schema_version', ?)"
            " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(SCHEMA_VERSION),))

    # ======================================================================
    # 写入：CSV + outbox
    # ======================================================================
    def append(self, record: dict, *, strict: bool = True) -> str:
        """一条记录落盘：写 CSV + 入 outbox。返回 record_id。

        **顺序是先 CSV 后 outbox**。万一中间崩了，CSV 里有、outbox 里没有 ——
        数据"没排上队"但它躺在原始日志里，U 盘导出时还找得回来。
        反过来的话，outbox 里就有一条永远读不出内容的记录，更难收拾。

        `strict=True` 时，物理量程外的值直接抛 ValueError：
        这种值平台一定会拒收，与其排进队里变成一个永久 REJECTED，
        不如在这里就暴露出来 —— 那是采集层或传感器的 bug，不是数据。
        """
        rid = _check_record(record, strict=strict)
        with self._lock:
            # 已经在库里就不再写一遍 CSV —— 重启后重放同一批时会发生
            if self._exists(rid):
                return rid
            name = self.csv.append(record)
            self._insert_outbox(record, name)
        return rid

    def append_many(self, records: Iterable[dict], *, strict: bool = True) -> list[str]:
        """批量落盘。返回实际**新入库**的 record_id 列表。

        整批一起写 CSV（少切几次文件），逐条入 outbox —— 重复的那几条跳过，
        所以这里是"去重后的真正新增数量"，不是"我传进来几条"。
        """
        fresh: list[dict] = []
        with self._lock:
            for rec in records:
                rid = _check_record(rec, strict=strict)
                if self._exists(rid):
                    continue
                fresh.append(rec)
            if not fresh:
                return []
            name = self.csv.append_many(fresh)
            for rec in fresh:
                self._insert_outbox(rec, name)
        return [str(r["record_id"]) for r in fresh]

    def _exists(self, record_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM outbox WHERE record_id=?", (record_id,)).fetchone() is not None

    def _insert_outbox(self, record: dict, csv_name: str) -> None:
        values = record.get("values") or {}
        cols = OUTBOX_BASE + PARAM_COLUMNS + ("status", "csv_file", "created_at")
        row = (
            str(record.get("record_id", "")),
            str(record.get("device_code", "")),
            str(record.get("ts_device", "")),
            int(record.get("ts_epoch", 0)),
            int(record.get("seq", 0)),
            str(record.get("pile_id", "")),
            int(record.get("quality", 0)),
            str(record.get("source", "can")),
            str(record.get("row_hash", "")),
        ) + tuple(float(values[k]) for k in PARAM_COLUMNS) + (
            UploadStatus.PENDING.value, csv_name, now_utc_iso(),
        )
        ph = ",".join("?" * len(cols))
        self.conn.execute(
            f"INSERT OR IGNORE INTO outbox ({','.join(cols)}) VALUES ({ph})", row)

    # ---- 审计 / 修订入队 ----
    def enqueue_audit(self, event: dict) -> str:
        """把一次操作（导出/导入/手动上传/参数变更）记账，等上传。

        审计事件与数据本体分开排队：**即使数据还没传上去，操作本身也要留痕**。
        """
        eid = str(event.get("event_id") or "").strip()
        if not eid:
            raise ValueError("审计事件缺少 event_id")
        self.conn.execute(
            "INSERT OR IGNORE INTO audit_queue(event_id, device_code, kind, ts_device,"
            " ts_epoch, payload_json, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (eid, str(event.get("device_code", "")), str(event.get("kind", "audit")),
             str(event.get("ts_device", "")), int(event.get("ts_epoch", 0)),
             _dumps(event.get("payload") or {}), UploadStatus.PENDING.value, now_utc_iso()))
        return eid

    def enqueue_revision(self, revision: dict) -> str:
        """把一条修订排进队。内容校验（桩号、字段名）交给 usb_sync 和平台。"""
        rid = str(revision.get("revision_id") or "").strip()
        target = str(revision.get("target_record_id") or "").strip()
        if not rid or not target:
            raise ValueError("修订缺少 revision_id 或 target_record_id")
        if not revision.get("reason_code"):
            raise ValueError("修订必须填写原因")
        self.conn.execute(
            "INSERT OR IGNORE INTO revision_queue(revision_id, target_id, device_code,"
            " patch_json, reason_code, reason_note, operator_name, ts_device, status,"
            " created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (rid, target, str(revision.get("device_code", "")),
             _dumps(revision.get("patch") or {}), str(revision["reason_code"]),
             str(revision.get("reason_note", "")), str(revision.get("operator_name", "")),
             str(revision.get("ts_device", "")), UploadStatus.PENDING.value, now_utc_iso()))
        return rid

    # ======================================================================
    # 取批 / 占行 / 回执
    # ======================================================================
    def pending_batch(self, limit: int | None = None,
                      *, now: float | None = None) -> list[dict]:
        """取下一批待传记录，**严格按 (ts_epoch, seq) 升序**。

        补传优先于实时：只要还有欠账，就先按时间顺序把旧数据发完。
        否则平台上会先出现新点、后出现旧点，曲线看起来像时间倒挂。

        退避中的记录（`next_attempt_at` 还没到）会被跳过，
        除非它被人工标了"立即上传"—— 那时 `next_attempt_at` 已被清零。
        """
        n = int(limit or self.cfg["upload"]["batch_max_records"])
        n = max(1, min(n, MAX_BATCH_RECORDS))
        ts = now if now is not None else now_epoch()
        rows = self.conn.execute(
            "SELECT * FROM outbox WHERE status=? AND next_attempt_at<=?"
            " ORDER BY ts_epoch, seq LIMIT ?",
            (UploadStatus.PENDING.value, ts, n)).fetchall()
        return [row_to_record(r) for r in rows]

    def pending_count(self) -> int:
        """待传条数（含退避中的，含 INFLIGHT）。

        界面上的"未上传 N 条"就是它 —— 退避中的也算欠账，
        不能因为"它在等重试"就在界面上消失。
        """
        return sqlite_util.scalar(
            self.conn,
            "SELECT COUNT(*) FROM outbox WHERE status IN (?,?)",
            (UploadStatus.PENDING.value, UploadStatus.INFLIGHT.value))

    def claim(self, record_ids: Sequence[str], batch_id: str,
              *, now_iso: str | None = None) -> list[dict]:
        """把一批记录从 PENDING 改成 INFLIGHT 并返回**真正占到**的那些。

        判据是 `UPDATE` **实际改了哪几行**，而不是"我挑的时候它还是 pending"。
        界面手动上传和后台 worker 可能同时挑中同一批，只有这一步的归属是确定的。

        用 SQLite 的 `RETURNING` 直接拿到本行改动，而不是改完再查一遍
        "这个批次名下有哪些行" —— 后者在同一批被占两次时会误判成占到，
        因为条件 `batch_id=? AND status='inflight'` 对它同样成立。
        """
        ids_ = [str(i) for i in record_ids if i]
        if not ids_:
            return []
        ts = now_iso or now_utc_iso()
        owned: list = []
        with self._lock:
            with sqlite_util.transaction(self.conn):
                for chunk in sqlite_util.chunked(ids_, 300):
                    ph = ",".join("?" * len(chunk))
                    owned.extend(self.conn.execute(
                        f"UPDATE outbox SET status=?, inflight_at=?, batch_id=?,"
                        f" attempts=attempts+1, next_attempt_at=0, manual=0"
                        f" WHERE record_id IN ({ph}) AND status=?"
                        f" RETURNING *",
                        (UploadStatus.INFLIGHT.value, ts, batch_id,
                         *chunk, UploadStatus.PENDING.value)).fetchall())
        owned.sort(key=lambda r: (int(r["ts_epoch"]), int(r["seq"])))
        return [row_to_record(r) for r in owned]

    def register_batch(self, batch_id: str, device_code: str, record_ids: Sequence[str],
                       *, kind: str = EventKind.FRAME.value, content_digest: str = "",
                       note: str = "") -> None:
        """登记一个已发出的批次（SENDING）。"""
        with self._lock:
            self.conn.execute(
                "INSERT INTO local_batches(batch_id, device_code, kind, created_at,"
                " record_count, status, attempts, content_digest, note)"
                " VALUES(?,?,?,?,?,?,1,?,?)"
                " ON CONFLICT(batch_id) DO UPDATE SET"
                " attempts=attempts+1, status=excluded.status",
                (batch_id, device_code, kind, now_utc_iso(), len(record_ids),
                 BatchStatus.SENDING.value, content_digest, note))

    def finish_batch(self, batch_id: str, receipt, *, kind: str = EventKind.FRAME.value,
                     elapsed_ms: int = 0, http_status: int = 200,
                     log_it: bool = True) -> dict:
        """回执到手：按逐条结果改行状态，并记账。

        **可以重复调用**：万一"写完回执、还没改完行"时进程被杀，
        `reconcile()` 会把同一个回执再喂进来一次。所有 UPDATE 都是幂等的
        （已经 uploaded 的行再置一次 uploaded 没有副作用），
        因此 `log_it=False` 让补齐流程不再往上传日志里写第二条。

        返回 {"accepted","duplicated","rejected","uploaded","status"} 计数，供日志用。
        """
        ts = now_utc_iso()
        acc = dup = rej = 0
        with self._lock:
            with sqlite_util.transaction(self.conn):
                for res in receipt.results:
                    rid = str(res.get("record_id", ""))
                    if not rid:
                        continue
                    result = str(res.get("result", ""))
                    if result == RecordResult.REJECTED.value:
                        rej += 1
                        self.conn.execute(
                            "UPDATE outbox SET status=?, reject_code=?, reject_note=?,"
                            " last_error=?, uploaded_at=NULL WHERE record_id=?",
                            (UploadStatus.REJECTED.value,
                             str(res.get("reason_code", "")),
                             str(res.get("message", "")), str(res.get("message", "")), rid))
                    elif result in (RecordResult.ACCEPTED.value, RecordResult.DUPLICATE.value):
                        if result == RecordResult.ACCEPTED.value:
                            acc += 1
                        else:
                            dup += 1
                        self.conn.execute(
                            "UPDATE outbox SET status=?, uploaded_at=?, last_error='',"
                            " reject_code='', reject_note='' WHERE record_id=?",
                            (UploadStatus.UPLOADED.value, ts, rid))
                # 回执里没提到的行（理论上不该有）保持 INFLIGHT，
                # 由 reconcile 超时回收 —— 不猜，不蒙。
                up = acc + dup
                status = (BatchStatus.ACKED.value if up == len(receipt.results) and rej == 0
                          else BatchStatus.PARTIAL.value)
                self.conn.execute(
                    "UPDATE local_batches SET status=?, finished_at=?, accepted=?,"
                    " duplicated=?, rejected=?, receipt_json=?"
                    " WHERE batch_id=?",
                    (status, ts, acc, dup, rej, _dumps(receipt.to_dict()), batch_id))
            if log_it:
                # 回执自带说明就用它 —— 平台拒收的理由要能一路传到终端界面上，
                # 否则操作员只能看到"部分成功"四个字，不知道该改哪里。
                msg = receipt.message or (
                    "重复批次（平台已收过）" if receipt.duplicate_batch else
                    f"全部 {rej} 条被平台拒收" if rej and up == 0 else "")
                self.log_upload(
                    batch_id, kind=kind, record_count=len(receipt.results),
                    status=status, http_status=http_status, accepted=acc,
                    duplicated=dup, rejected=rej, elapsed_ms=elapsed_ms,
                    message=msg)
        return {"accepted": acc, "duplicated": dup, "rejected": rej, "uploaded": up,
                "status": status}

    def backoff_for(self, attempts: int) -> float:
        """第 n 次尝试失败后该等多久。

        指数退避，用完调色板就固定取最后一个值（不再无限增长）——
        展示现场最长的等待是 60 秒，客户不会觉得"卡住了"。
        """
        table = self.cfg["upload"].get("backoff_s") or [2, 4, 8, 15, 30, 60]
        i = max(0, min(int(attempts) - 1, len(table) - 1))
        return float(table[i])

    def release_batch(self, batch_id: str, *, error: str = "",
                      retry_in: float | None = None,
                      now: float | None = None) -> int:
        """回执没拿到（超时/断网/HTTP 错误）：INFLIGHT → PENDING，等下次重试。

        **batch_id 保留不清**。重试时若挑中的还是同一批记录，内容寻址算出来的
        batch_id 一模一样，平台会回 `duplicate: true` —— 这正是我们要的：
        第一次其实送到了、只是回执没回来，也不会重复入库。

        `retry_in=None`（默认）时按这一批里最大的尝试次数自动退避。
        退避策略放在这里而不是上传层：它依赖行上的 `attempts`，
        而那个计数只有本模块知道。
        """
        ts = now if now is not None else now_epoch()
        with self._lock:
            if retry_in is None:
                worst = sqlite_util.scalar(
                    self.conn, "SELECT MAX(attempts) FROM outbox WHERE batch_id=?",
                    (batch_id,), default=1)
                retry_in = self.backoff_for(int(worst))
            with sqlite_util.transaction(self.conn):
                cur = self.conn.execute(
                    "UPDATE outbox SET status=?, inflight_at=NULL, next_attempt_at=?,"
                    " last_error=? WHERE batch_id=? AND status=?",
                    (UploadStatus.PENDING.value, ts + max(0.0, retry_in), error,
                     batch_id, UploadStatus.INFLIGHT.value))
                n = cur.rowcount
                self.conn.execute(
                    "UPDATE local_batches SET status=?, finished_at=?, note=?"
                    " WHERE batch_id=? AND status=?",
                    (BatchStatus.LOST.value, now_utc_iso(), error,
                     batch_id, BatchStatus.SENDING.value))
            return n

    def mark_manual(self, record_ids: Sequence[str] | None = None) -> int:
        """把"立即上传"的**意图**写进库。绝不在这里发 HTTP 请求。

        界面线程只写库，上传 worker 下一拍就会看到：
        `next_attempt_at=0` 让它跳过退避，`manual=1` 记下这是人工要的。
        REJECTED 的行也会被拎回 PENDING —— 用户手工校对后坚持要重传，
        那就给他传，平台会再判一次。
        """
        with self._lock:
            if record_ids is None:
                cur = self.conn.execute(
                    "UPDATE outbox SET manual=1, next_attempt_at=0, status=?"
                    " WHERE status IN (?,?)",
                    (UploadStatus.PENDING.value, UploadStatus.PENDING.value,
                     UploadStatus.REJECTED.value))
            else:
                ids_ = [str(i) for i in record_ids if i]
                if not ids_:
                    return 0
                ph = ",".join("?" * len(ids_))
                cur = self.conn.execute(
                    f"UPDATE outbox SET manual=1, next_attempt_at=0, status=?"
                    f" WHERE record_id IN ({ph}) AND status IN (?,?)",
                    (UploadStatus.PENDING.value, *ids_,
                     UploadStatus.PENDING.value, UploadStatus.REJECTED.value))
            return cur.rowcount

    def has_manual(self) -> bool:
        """有没有人点了"立即上传"。上传 worker 用它决定要不要提前醒来。"""
        return sqlite_util.scalar(
            self.conn, "SELECT COUNT(*) FROM outbox WHERE manual=1") > 0

    def mark_void(self, record_ids: Sequence[str], *, note: str = "") -> int:
        """人工判定无效，不再上传。原始 CSV 仍保留（只追加，不删行）。"""
        ids_ = [str(i) for i in record_ids if i]
        if not ids_:
            return 0
        with self._lock:
            cur = self.conn.execute(
                f"UPDATE outbox SET status=?, reject_note=?, inflight_at=NULL"
                f" WHERE record_id IN ({','.join('?' * len(ids_))})",
                (UploadStatus.VOID.value, note, *ids_))
            return cur.rowcount

    def log_upload(self, batch_id: str, *, kind: str = EventKind.FRAME.value,
                   record_count: int = 0, status: str = "", http_status: int = 0,
                   accepted: int = 0, duplicated: int = 0, rejected: int = 0,
                   elapsed_ms: int = 0, message: str = "") -> None:
        """记一行上传日志。**无论成功失败都记**，终端界面的"上传记录"就靠它。"""
        self.conn.execute(
            "INSERT INTO upload_log(ts, batch_id, kind, record_count, status,"
            " http_status, accepted, duplicated, rejected, elapsed_ms, message)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (now_utc_iso(), batch_id, kind, int(record_count), str(status),
             int(http_status), int(accepted), int(duplicated), int(rejected),
             int(elapsed_ms), str(message)))

    # ======================================================================
    # 崩溃恢复
    # ======================================================================
    def reconcile(self, *, now: float | None = None,
                  timeout_s: float | None = None) -> dict:
        """回收崩溃残留。三件事，**顺序不能换**：

        1. **用已存回执补齐行**。窗口：批次行写完了、outbox 行还没改完时被杀。
           必须先做这一步 —— 这些行的批次已经是终态，它们不是"超时没回执"，
           是"回执到了没落库"。先做 2 会把它们误判成超时、白白重传一遍。
        2. **超时的 INFLIGHT 收回 PENDING**。批次标 LOST。重传安全：
           batch_id 是内容寻址的，真送到了平台也只当重复。
        3. **超时的 SENDING 批次标 LOST**。纯粹是账本，不影响数据。
        """
        ts = now if now is not None else now_epoch()
        timeout = float(timeout_s if timeout_s is not None
                        else self.cfg["upload"]["inflight_timeout_s"])
        report = {"repaired": 0, "requeued": 0, "lost_batches": 0, "batches": []}

        with self._lock:
            # ---- 1. 用回执补齐 ----
            done_batches = self.conn.execute(
                "SELECT batch_id, receipt_json FROM local_batches"
                " WHERE status IN (?,?) AND receipt_json IS NOT NULL",
                (BatchStatus.ACKED.value, BatchStatus.PARTIAL.value)).fetchall()
            for b in done_batches:
                stuck = sqlite_util.scalar(
                    self.conn, "SELECT COUNT(*) FROM outbox WHERE batch_id=? AND status=?",
                    (b["batch_id"], UploadStatus.INFLIGHT.value))
                if not stuck:
                    continue
                try:
                    receipt = Receipt.from_dict(_loads(b["receipt_json"]))
                except (ValueError, TypeError):
                    continue        # 回执坏了，交给第 2 步按超时回收
                self.finish_batch(b["batch_id"], receipt, log_it=False)
                report["repaired"] += stuck
                report["batches"].append({"batch_id": b["batch_id"], "repaired": stuck})
                log.warning("补齐批次 %s：%d 条记录的状态按已存回执回填",
                            b["batch_id"], stuck)

            # ---- 2. 超时 INFLIGHT 收回 ----
            # inflight_at 为空的（老库或异常路径）一律视为超时，宁可早回收。
            cutoff_iso = _iso_from_epoch(ts - timeout)
            stale = self.conn.execute(
                "SELECT batch_id, COUNT(*) AS n FROM outbox"
                " WHERE status=? AND batch_id<>''"
                "   AND (inflight_at IS NULL OR inflight_at<=?)"
                " GROUP BY batch_id",
                (UploadStatus.INFLIGHT.value, cutoff_iso)).fetchall()
            for s in stale:
                # `retry_in=0` 明确绕过退避：重启是"重新开始"，不是"第 N 次重试"。
                # 用默认退避的话，一个重试过 8 次的批次在重启后还要再等 60 秒
                # 才肯动 —— 而操作员重启终端，多半正是为了让数据赶紧上去。
                n = self.release_batch(s["batch_id"], error="上传超时未收到回执",
                                       retry_in=0, now=ts)
                report["requeued"] += n
                log.warning("回收超时上传：批次 %s 的 %d 条记录回到待传队列",
                            s["batch_id"], n)
            # 没有 batch_id 的孤儿行（异常路径才会出现）也要收回，否则永远卡着
            cur = self.conn.execute(
                "UPDATE outbox SET status=?, inflight_at=NULL, next_attempt_at=?"
                " WHERE status=? AND (batch_id='' OR batch_id IS NULL)",
                (UploadStatus.PENDING.value, ts, UploadStatus.INFLIGHT.value))
            report["requeued"] += cur.rowcount

            # ---- 3. 超时 SENDING 批次标 LOST ----
            cutoff_iso = _iso_from_epoch(ts - timeout)
            cur = self.conn.execute(
                "UPDATE local_batches SET status=?, finished_at=?, note=?"
                " WHERE status=? AND created_at<=?",
                (BatchStatus.LOST.value, now_utc_iso(), "发送超时，记录已回到待传队列",
                 BatchStatus.SENDING.value, cutoff_iso))
            report["lost_batches"] = cur.rowcount

        return report

    # ======================================================================
    # 30 天安全清理
    # ======================================================================
    def protected_files(self) -> set[str]:
        """还有未上传记录的 CSV 文件名 —— 这些是**绝对不能删**的。"""
        rows = self.conn.execute(
            f"SELECT DISTINCT csv_file FROM outbox WHERE status IN"
            f" ({','.join('?' * len(OPEN_STATUSES))})", OPEN_STATUSES).fetchall()
        return {r["csv_file"] for r in rows if r["csv_file"]}

    def cleanup(self, *, days: int | None = None, now: float | None = None) -> dict:
        """删掉超过保留期的 CSV 和已上传的 outbox 行。

        **安全规则（这是本方法存在的全部理由）：**

        * 只要文件里还有 pending / inflight / rejected 的记录，无论多老都不删 ——
          那份 CSV 是这条数据**唯一的副本**，删了就真没了。
        * 判定文件年龄要同时满足两个条件：文件系统 mtime 过期 **且** 文件名里
          的日期也过期。取"都过期"是保守方向 —— 设备时钟不准时宁可多留，
          不能少留。
        * 已上传（uploaded / void）的 outbox 行可以删，数据在平台上；
          **REJECTED 的行保留**，那是要人工校对的证据，删了就没法对账了。
        * Excel 正开着文件会删不掉（WinError 32）—— 那不算错误，
          跳过并记下来，下次清理再说。
        """
        n_days = int(days if days is not None else self.cfg["retention"]["csv_days"])
        ts = now if now is not None else now_epoch()
        cutoff = ts - n_days * 86400.0
        cutoff_day = to_datetime(cutoff).astimezone(CST).strftime("%Y-%m-%d")
        protected = self.protected_files()

        report: dict[str, Any] = {
            "days": n_days, "cutoff": _iso_from_epoch(cutoff),
            "deleted": [], "locked": [], "protected": sorted(protected),
            "kept_open": [], "kept_recent": 0, "rows_pruned": 0, "bytes_freed": 0,
        }

        # 判定顺序即优先级：正在写 > 有未上传数据 > 还没到保留期 > 才轮到删。
        # 每一个文件都要落进某一档，报告里不能有"没提到"的文件 ——
        # 否则运维看到"删了 0 个"根本分不清是没到期限还是被保护了。
        opened = self.csv.current_name
        for path in self.csv.files():
            name = os.path.basename(path)
            if name == opened:
                report["kept_open"].append(name)   # 句柄还开着，等轮转后再说
                continue
            if name in protected:
                continue                        # 有未上传数据，前面已列入 protected
            day = CsvLog.day_of(name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > cutoff or (day and day > cutoff_day):
                report["kept_recent"] += 1
                continue
            try:
                size = os.path.getsize(path)
                os.remove(path)
            except PermissionError:
                # Excel 开着 —— 演示时常见，不能因此中断清理
                report["locked"].append(name)
                log.info("CSV 被占用，本次跳过：%s", name)
                continue
            except OSError as exc:
                report["locked"].append(name)
                log.warning("删除 CSV 失败 %s：%s", name, exc)
                continue
            report["deleted"].append(name)
            report["bytes_freed"] += size

        # 已上传的行可以从库里清掉：平台上有。REJECTED 留着对账。
        cutoff_ms = int(cutoff * 1000)
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM outbox WHERE status IN (?,?) AND ts_epoch < ?",
                (UploadStatus.UPLOADED.value, UploadStatus.VOID.value, cutoff_ms))
            report["rows_pruned"] = cur.rowcount
            self.conn.execute("DELETE FROM upload_log WHERE ts < ?",
                              (_iso_from_epoch(cutoff),))
            self.conn.execute(
                "DELETE FROM local_batches WHERE status IN (?,?) AND finished_at < ?",
                (BatchStatus.ACKED.value, BatchStatus.LOST.value,
                 _iso_from_epoch(cutoff)))
        if report["deleted"] or report["rows_pruned"]:
            log.info("清理完成：删 %d 个 CSV（%.1f MB），清 %d 条已上传索引",
                     len(report["deleted"]), report["bytes_freed"] / 1048576.0,
                     report["rows_pruned"])
        return report

    def usage(self) -> dict:
        """本地占用情况，终端界面用。"""
        files = self.csv.files()
        total = 0
        oldest = ""
        for p in files:
            try:
                total += os.path.getsize(p)
            except OSError:
                continue
            d = CsvLog.day_of(p)
            if d and (not oldest or d < oldest):
                oldest = d
        return {
            "files": len(files),
            "bytes": total,
            "mb": round(total / 1048576.0, 2),
            "oldest_day": oldest,
            "days": len(self.csv.days()),
            "current_file": self.csv.current_name,
        }

    # ======================================================================
    # 查询（终端界面用）
    # ======================================================================
    def stats(self) -> dict:
        """各状态计数 + 欠账最久的时间。"""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM outbox GROUP BY status").fetchall()
        by = {r["status"]: r["n"] for r in rows}
        oldest = self.conn.execute(
            "SELECT MIN(ts_device) AS t FROM outbox WHERE status IN (?,?)",
            (UploadStatus.PENDING.value, UploadStatus.INFLIGHT.value)).fetchone()
        return {
            "pending": by.get(UploadStatus.PENDING.value, 0),
            "inflight": by.get(UploadStatus.INFLIGHT.value, 0),
            "uploaded": by.get(UploadStatus.UPLOADED.value, 0),
            "rejected": by.get(UploadStatus.REJECTED.value, 0),
            "void": by.get(UploadStatus.VOID.value, 0),
            "total": sum(by.values()),
            "backlog": (by.get(UploadStatus.PENDING.value, 0)
                        + by.get(UploadStatus.INFLIGHT.value, 0)),
            "oldest_pending": (oldest["t"] if oldest and oldest["t"] else ""),
            "revisions_pending": sqlite_util.scalar(
                self.conn, "SELECT COUNT(*) FROM revision_queue WHERE status=?",
                (UploadStatus.PENDING.value,)),
            "audits_pending": sqlite_util.scalar(
                self.conn, "SELECT COUNT(*) FROM audit_queue WHERE status=?",
                (UploadStatus.PENDING.value,)),
        }

    def recent(self, *, limit: int = 60, status: str = "", param: str = "",
               scope: str = "all") -> list[dict]:
        """最近记录，倒序。终端界面的实时表格用它。"""
        where, args = [], []
        if status:
            where.append("status=?")
            args.append(status)
        if scope == "today":
            where.append("ts_epoch>=?")
            args.append(int(start_of_local_day() * 1000))
        elif scope == "rejected":
            where.append("status=?")
            args.append(UploadStatus.REJECTED.value)
        sql = "SELECT * FROM outbox"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts_epoch DESC, seq DESC LIMIT ?"
        args.append(int(limit))
        out = []
        for r in self.conn.execute(sql, args).fetchall():
            out.append(record_view(r, param=param))
        return out

    def get_record(self, record_id: str) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM outbox WHERE record_id=?", (record_id,)).fetchone()
        return record_view(r) if r else None

    def export_rows(self, *, scope: str = "pending", limit: int = 2000) -> list[dict]:
        """U 盘导出用的整行记录（协议形状），**按时间升序**。

        升序是有意的：导出的 CSV 是要给人看、给人改的，
        从早到晚排才读得下去（界面上的表格反过来，新的在最上面）。
        取的列也比界面多 —— 导出必须带上 PILE 号和全部 16 个参数，
        否则改完导回来就没法逐字段比对了。
        """
        where, args = [], []
        if scope == "pending":
            where.append("status IN (?,?)")
            args += [UploadStatus.PENDING.value, UploadStatus.INFLIGHT.value]
        elif scope == "today":
            where.append("ts_epoch>=?")
            args.append(int(start_of_local_day() * 1000))
        elif scope not in ("all", ""):
            raise ValueError(f"未知导出范围 {scope}")
        sql = "SELECT * FROM outbox"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts_epoch, seq LIMIT ?"
        args.append(int(limit))
        return [row_to_record(r) for r in self.conn.execute(sql, args).fetchall()]

    def rows_by_ids(self, record_ids: Sequence[str]) -> dict[str, dict]:
        """按 record_id 批量取整行（U 盘导入时逐行比对用）。"""
        out: dict[str, dict] = {}
        ids_ = [str(r) for r in record_ids]
        CHUNK = 400
        for i in range(0, len(ids_), CHUNK):
            chunk = ids_[i:i + CHUNK]
            if not chunk:
                continue
            marks = ",".join("?" * len(chunk))
            for row in self.conn.execute(
                    f"SELECT * FROM outbox WHERE record_id IN ({marks})", chunk).fetchall():
                out[row["record_id"]] = row_to_record(row)
        return out

    def uploads(self, *, limit: int = 60, kind: str = "") -> list[dict]:
        sql = "SELECT * FROM upload_log"
        args: list[Any] = []
        if kind:
            sql += " WHERE kind=?"
            args.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        return [{
            "ts": r["ts"],
            "batch_id": r["batch_id"],
            "kind": r["kind"],
            "record_count": r["record_count"],
            "status": r["status"],
            "http_status": r["http_status"],
            "accepted": r["accepted"],
            "duplicated": r["duplicated"],
            "rejected": r["rejected"],
            "elapsed_ms": r["elapsed_ms"],
            "message": r["message"],
        } for r in self.conn.execute(sql, args).fetchall()]

    def batches(self, *, limit: int = 40) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM local_batches ORDER BY created_at DESC LIMIT ?",
            (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    # ---- 桩号缓存（供 terminal/pile.py 用） ----
    def replace_piles(self, piles: Iterable[dict]) -> int:
        """整份替换平台下发的桩号清单。

        整份替换而不是增量合并：平台是清单的唯一权威，它说不发了就不该再留着。
        """
        items = list(piles)
        with self._lock, sqlite_util.transaction(self.conn):
            self.conn.execute("DELETE FROM piles")
            n = sqlite_util.insert_many(
                self.conn,
                "piles",
                ("pile_id", "site_id", "line", "mileage_m", "label", "issued_at", "active"),
                [(str(p.get("pile_id", "")), str(p.get("site_id", "")),
                  str(p.get("line", "")), float(p.get("mileage_m") or 0),
                  str(p.get("label", "")), str(p.get("issued_at", "")),
                  1 if p.get("active", True) else 0) for p in items if p.get("pile_id")])
            self.conn.execute(
                "INSERT INTO meta(k, v) VALUES('piles_synced_at', ?)"
                " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (now_utc_iso(),))
        return n

    def pile_ids(self) -> list[str]:
        return [r["pile_id"] for r in self.conn.execute(
            "SELECT pile_id FROM piles WHERE active=1 ORDER BY mileage_m").fetchall()]

    def piles(self) -> list[dict]:
        """完整桩号清单，**已按里程升序**。终端界面直接拿去渲染可点清单。"""
        return [dict(r) for r in self.conn.execute(
            "SELECT pile_id, site_id, line, mileage_m, label, issued_at"
            " FROM piles WHERE active=1 ORDER BY mileage_m, pile_id").fetchall()]

    def pile_known(self, pile_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM piles WHERE pile_id=? AND active=1", (pile_id,)).fetchone() is not None

    def meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, str(value)))

    # ======================================================================
    # 队列：修订 / 审计
    # ======================================================================
    def pending_revisions(self, limit: int = 100, *, now: float | None = None) -> list[dict]:
        return self._pending_of("revision_queue", limit, now)

    def pending_audits(self, limit: int = 100, *, now: float | None = None) -> list[dict]:
        return self._pending_of("audit_queue", limit, now)

    def _pending_of(self, table: str, limit: int, now: float | None) -> list[dict]:
        ts = now if now is not None else now_epoch()
        id_col = "revision_id" if table == "revision_queue" else "event_id"
        rows = self.conn.execute(
            f"SELECT * FROM {table} WHERE status=? AND next_attempt_at<=?"
            f" ORDER BY created_at, {id_col} LIMIT ?",
            (UploadStatus.PENDING.value, ts, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def claim_queue(self, table: str, ids_: Sequence[str], batch_id: str,
                    *, now_iso: str | None = None) -> list[dict]:
        """队列占行，语义与 `outbox.claim` 完全一致：只认 UPDATE 真正改了哪几行。"""
        id_col = "revision_id" if table == "revision_queue" else "event_id"
        items = [str(i) for i in ids_ if i]
        if not items:
            return []
        owned: list = []
        with self._lock, sqlite_util.transaction(self.conn):
            for chunk in sqlite_util.chunked(items, 300):
                ph = ",".join("?" * len(chunk))
                owned.extend(self.conn.execute(
                    f"UPDATE {table} SET status=?, attempts=attempts+1,"
                    f" next_attempt_at=0, batch_id=?"
                    f" WHERE {id_col} IN ({ph}) AND status=?"
                    f" RETURNING *",
                    (UploadStatus.INFLIGHT.value, batch_id, *chunk,
                     UploadStatus.PENDING.value)).fetchall())
        return [dict(r) for r in owned]

    def finish_queue(self, table: str, results: Sequence[dict], *, batch_id: str = "",
                     elapsed_ms: int = 0) -> dict:
        """按回执改队列行状态。与 finish_batch 一样可以重复调用。"""
        id_col = "revision_id" if table == "revision_queue" else "event_id"
        ts = now_utc_iso()
        acc = dup = rej = 0
        with self._lock, sqlite_util.transaction(self.conn):
            for res in results:
                rid = str(res.get("record_id", ""))
                if not rid:
                    continue
                if res.get("result") == RecordResult.REJECTED.value:
                    rej += 1
                    self.conn.execute(
                        f"UPDATE {table} SET status=?, last_error=? WHERE {id_col}=?",
                        (UploadStatus.REJECTED.value,
                         f"{res.get('reason_code', '')} {res.get('message', '')}".strip(), rid))
                else:
                    if res.get("result") == RecordResult.ACCEPTED.value:
                        acc += 1
                    else:
                        dup += 1
                    self.conn.execute(
                        f"UPDATE {table} SET status=?, uploaded_at=?, last_error='',"
                        f" batch_id=? WHERE {id_col}=?",
                        (UploadStatus.UPLOADED.value, ts, batch_id, rid))
            # 回执没提到的，超时后由 release_queue 回收
        return {"accepted": acc, "duplicated": dup, "rejected": rej}

    def release_queue(self, table: str, batch_id: str, *, error: str = "",
                      retry_in: float | None = None, now: float | None = None) -> int:
        """队列版的重试退避。`retry_in=None` 时按最大尝试次数自动退避。"""
        ts = now if now is not None else now_epoch()
        with self._lock:
            if retry_in is None:
                worst = sqlite_util.scalar(
                    self.conn, f"SELECT MAX(attempts) FROM {table} WHERE batch_id=?",
                    (batch_id,), default=1)
                retry_in = self.backoff_for(int(worst))
            cur = self.conn.execute(
                f"UPDATE {table} SET status=?, next_attempt_at=?, last_error=?"
                f" WHERE batch_id=? AND status=?",
                (UploadStatus.PENDING.value, ts + max(0.0, retry_in), error,
                 batch_id, UploadStatus.INFLIGHT.value))
            return cur.rowcount

    def queue_blocked_by_backlog(self) -> int:
        """有多少条修订因为"目标记录还没上平台"在等。

        修订必须等它改的那条记录先入库 —— 否则平台会回
        "目标记录不存在"，而且是**永久拒收**，白丢一条人工留痕。
        """
        return sqlite_util.scalar(
            self.conn,
            "SELECT COUNT(*) FROM revision_queue WHERE status IN (?,?)",
            (UploadStatus.PENDING.value, UploadStatus.INFLIGHT.value))

    def reconcile_queues(self, *, now: float | None = None,
                         timeout_s: float | None = None) -> dict:
        """队列版的崩溃回收：超时 INFLIGHT 一律回 PENDING。"""
        ts = now if now is not None else now_epoch()
        timeout = float(timeout_s if timeout_s is not None
                        else self.cfg["upload"]["inflight_timeout_s"])
        cutoff = _iso_from_epoch(ts - timeout)
        out = {}
        with self._lock:
            for table in ("revision_queue", "audit_queue"):
                cur = self.conn.execute(
                    f"UPDATE {table} SET status=?, next_attempt_at=?, last_error=?"
                    f" WHERE status=? AND (created_at<=? OR batch_id='')",
                    (UploadStatus.PENDING.value, ts, "上传超时未收到回执",
                     UploadStatus.INFLIGHT.value, cutoff))
                out[table] = cur.rowcount
        return out

    def revisions(self, *, limit: int = 50) -> list[dict]:
        """本地修订记录，终端界面"已校对"列表用。

        排序带 `revision_id` 兜底：`created_at` 秒内精度有限，同一批导入出来的
        几条修订**经常落在同一个时间戳上**，只按它排的话谁在前是未定义的——
        界面上同一批的顺序每次刷新都可能不一样，测试也会时好时坏。
        revision_id 是 `设备:rev:毫秒:序号`，同一批里毫秒相同，退化成按序号排，
        正好是"这一批里最后生成的那条在最前"。
        """
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM revision_queue ORDER BY created_at DESC, revision_id DESC"
            " LIMIT ?", (int(limit),)).fetchall()]

    def set_revision_status(self, revision_id: str, status: str, note: str = "") -> None:
        self.conn.execute(
            "UPDATE revision_queue SET status=?, last_error=? WHERE revision_id=?",
            (status, note, revision_id))


# --------------------------------------------------------------------------
# 行 <-> 记录
# --------------------------------------------------------------------------
def row_to_record(row) -> dict:
    """outbox 行 → 协议里的帧记录形状（用于组批上传）。"""
    return {
        "record_id": row["record_id"],
        "device_code": row["device_code"],
        "ts_device": row["ts_device"],
        "ts_epoch": int(row["ts_epoch"]),
        "seq": int(row["seq"]),
        "pile_id": row["pile_id"],
        "quality": int(row["quality"]),
        "source": row["source"],
        "row_hash": row["row_hash"],
        "values": {k: row[k] for k in PARAM_COLUMNS},
    }


def record_view(row, *, param: str = "") -> dict:
    """outbox 行 → 给界面看的字典（参数值 + 中文名 + 状态中文）。"""
    rec = row_to_record(row)
    rec.update({
        "status": row["status"],
        "status_label": _status_label(row["status"]),
        "attempts": int(row["attempts"]),
        "last_error": row["last_error"] or "",
        "reject_code": row["reject_code"] or "",
        "reject_note": row["reject_note"] or "",
        "csv_file": row["csv_file"] or "",
        "batch_id": row["batch_id"] or "",
        "manual": bool(row["manual"]),
        "uploaded_at": row["uploaded_at"] or "",
        "display": {k: _fmt_param(k, row[k]) for k in PARAM_COLUMNS},
    })
    if param:
        rec["param"] = param
        rec["param_value"] = row[param] if param in PARAM_COLUMNS else None
        rec["param_label"] = _fmt_param(param, row[param]) if param in PARAM_COLUMNS else ""
    return rec


def _status_label(status: str) -> str:
    try:
        return UploadStatus(status).label
    except ValueError:
        return status


def _fmt_param(key: str, value: Any) -> str:
    """按注册表格式化一个参数值。开关量显示"启动/停止"，其余按精度。"""
    p = PARAM_BY_KEY.get(key)
    if p is None or value is None:
        return "" if value is None else str(value)
    try:
        return p.format(float(value))
    except (TypeError, ValueError):
        return str(value)


def _check_record(record: dict, *, strict: bool) -> str:
    """校验一条待入队记录，返回 record_id。不合法时抛 ValueError。

    `strict=True` 时物理量程外的值也拒绝：这种值平台一定会拒收，
    排进队里只会变成一个永久 REJECTED —— 那是采集层或传感器的 bug，
    应该在设备侧就暴露，而不是等上传到平台再发现。
    """
    rid = str(record.get("record_id") or "").strip()
    if not rid:
        raise ValueError("记录缺少 record_id")
    values = record.get("values") or {}
    for k in PARAM_KEYS:
        if k not in values:
            raise ValueError(f"{PARAM_BY_KEY[k].name} 缺失（record_id={rid}）")
        try:
            fv = float(values[k])
        except (TypeError, ValueError):
            raise ValueError(
                f"{PARAM_BY_KEY[k].name} 不是数值：{values[k]!r}（record_id={rid}）"
            ) from None
        if strict:
            msg = check_value(PARAM_BY_KEY[k], fv)
            if msg:
                raise ValueError(f"{msg}（record_id={rid}）")
    return rid


def _iso_from_epoch(ts: float) -> str:
    return to_datetime(ts).isoformat(timespec="milliseconds")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _loads(s: str) -> Any:
    return json.loads(s)
