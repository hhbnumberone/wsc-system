# -*- coding: utf-8 -*-
"""报警判定：边沿触发 + 回滞 + 最短持续时间。

**为什么不能裸阈值判定**

演示环境 2 秒一帧，一个 30 天的种子数据集是四万多帧。如果"超限即报警"，
一个持续越限的参数会立刻产生上万条记录，平台列表直接爆掉，
演示第一分钟就穿帮。真实项目里这叫报警风暴，是必须处理的问题。

**本模块的规则**

1. **边沿触发**：只在"未报警 → 报警"那一次写记录。之后持续越限只累计峰值、
   越限帧数，不再新增行。
2. **最短持续时间**：越限要连续保持 `min_duration_s` 秒才真开警，滤掉单点毛刺。
3. **回滞**：值回到 `[lo+5%量程, hi-5%量程]` 之内才算恢复，
   在边界抖动的信号不会反复开关。
4. **恢复延迟**：连续满足恢复条件 `clear_after_s` 秒才解除，
   停机检修那几秒不产生一堆短命报警。
5. **数据库兜底**：`alarms` 上有部分唯一索引
   `(device_code, param_key) WHERE ended_at IS NULL`。
   判定逻辑万一写错，同一参数也不可能同时存在两条活跃报警。

判定用的计时状态只存在内存里；报警的**身份**（哪一条）以数据库为准 ——
平台重启后内存清零，但库里那条活跃报警还在，`_hydrate` 会把它认领回来，
不会重复开警。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime

from common.params import Param, registry
from common.timeutil import UTC, now_epoch, now_utc_iso, to_datetime

log = logging.getLogger("server.alarms")

#: 活跃报警的峰值回写节流（秒）。越限期间不必每帧都 UPDATE 数据库。
PEAK_WRITE_INTERVAL_S = 5.0


@dataclass
class _State:
    """一个 (设备, 参数) 的判定状态。"""

    active_id: int | None = None        # 数据库里活跃报警的 alarm_id
    hydrated: bool = False              # 是否已从数据库初始化过 active_id
    breach_since: float | None = None   # 本轮连续越限的起始 epoch（秒）
    clear_since: float | None = None    # 本轮连续恢复的起始 epoch（秒）
    peak_value: float | None = None     # 越限最严重时的值
    peak_at: str = ""
    breach_count: int = 0               # 处于报警状态的帧数
    last_peak_write: float = 0.0


@dataclass
class AlarmEvent:
    """判定产生的动作，交给调用方记日志 / 推送界面。"""

    action: str          # "open" | "close"
    device_code: str
    param_key: str
    level: str = ""
    value: float = 0.0
    alarm_id: int | None = None
    message: str = ""

    def __str__(self) -> str:
        tag = "报警" if self.action == "open" else "解除"
        return f"[{tag}] {self.device_code} {self.message or self.param_key}"


def _fmt(p: Param, v: float) -> str:
    return f"{v:.{p.decimals}f}{p.unit}"


def excursion(p: Param, v: float) -> float:
    """越限幅度。范围内为 0，超出越多值越大。

    峰值取"越限幅度最大"的那一帧，而不是简单的最大/最小值 ——
    泵送压力低到 0 和高到 20 都该被记住，但方向相反。
    """
    return max(0.0, v - p.hi, p.lo - v)


def build_message(p: Param, v: float, opening: bool) -> str:
    """生成中文报警描述。"""
    rng = f"{_fmt(p, p.lo)}~{_fmt(p, p.hi)}"
    if opening:
        direction = "低于下限" if v < p.lo else "高于上限"
        return f"{p.name} {_fmt(p, v)}，{direction}（正常范围 {rng}）"
    return f"{p.name} 已恢复正常（当前 {_fmt(p, v)}，正常范围 {rng}）"


def _epoch_to_iso(sec: float) -> str:
    return datetime.fromtimestamp(sec, UTC).isoformat(timespec="milliseconds")


class AlarmEngine:
    """线程安全的报警判定器。

    服务端是 `ThreadingHTTPServer`，上传入库可能并发；这里用一把锁把判定串行化，
    避免两个请求同时给同一参数开警。
    """

    def __init__(self, thresholds: dict | None = None):
        self._params = registry(thresholds)
        self._states: dict[tuple[str, str], _State] = {}
        self._lock = threading.Lock()

    # ---- 只读属性 ----
    @property
    def params(self) -> dict[str, Param]:
        return self._params

    @property
    def alarmable_keys(self) -> tuple[str, ...]:
        return tuple(k for k, p in self._params.items() if p.alarmable)

    # ---- 内部 ----
    def _state(self, key: tuple[str, str]) -> _State:
        st = self._states.get(key)
        if st is None:
            st = _State()
            self._states[key] = st
        return st

    @staticmethod
    def _hydrate(conn: sqlite3.Connection, dev: str, pk: str, st: _State) -> None:
        """首次接触某个 (设备, 参数) 时，从库里认领可能已存在的活跃报警。

        这是崩溃恢复的关键一步：平台重启后内存状态清零，
        若不做这个查询，就会给同一条物理故障再开一条报警。
        """
        if st.hydrated:
            return
        row = conn.execute(
            "SELECT alarm_id, peak_value, peak_at, breach_count FROM alarms"
            " WHERE device_code=? AND param_key=? AND ended_at IS NULL"
            " ORDER BY alarm_id DESC LIMIT 1",
            (dev, pk),
        ).fetchone()
        if row:
            st.active_id = row["alarm_id"]
            st.peak_value = row["peak_value"]
            st.peak_at = row["peak_at"] or ""
            st.breach_count = row["breach_count"] or 1
            log.info("认领平台重启前已存在的活跃报警 #%s（%s / %s）",
                     st.active_id, dev, pk)
        st.hydrated = True

    # ---- 主判定 ----
    def observe(self, conn: sqlite3.Connection, device_code: str, param_key: str,
                value: float, ts_epoch: int, pile_id: str = "",
                active: bool = True) -> list[AlarmEvent]:
        """喂一帧数据，返回本次产生的动作（绝大多数情况下是空列表）。

        `active=False` 表示该参数此刻没有意义（对应子系统停机）。
        这时既不开新警，也把已在活跃的报警**当作恢复**处理 ——
        否则"速凝剂泵停了"会变成一条永远解除不掉的报警。
        """
        p = self._params.get(param_key)
        if p is None or not p.alarmable:
            return []

        ts = float(ts_epoch) / 1000.0
        with self._lock:
            st = self._state((device_code, param_key))
            self._hydrate(conn, device_code, param_key, st)
            if st.active_id is None:
                return self._maybe_open(conn, p, device_code, param_key,
                                        value, ts, ts_epoch, pile_id, st, active)
            return self._maybe_close(conn, p, device_code, param_key, value, ts, st, active)

    def _maybe_open(self, conn, p: Param, dev: str, pk: str, value: float,
                    ts: float, ts_epoch: int, pile_id: str, st: _State,
                    active: bool = True) -> list[AlarmEvent]:
        if not active or not p.raise_cond(value):
            st.breach_since = None          # 不判定或没越限，连续计时清零
            return []
        if st.breach_since is None:
            st.breach_since = ts            # 第一次越限，开始计时
            return []
        if ts - st.breach_since < p.min_duration_s:
            return []                       # 还没持续够，先不报

        msg = build_message(p, value, opening=True)
        cur = conn.execute(
            "INSERT OR IGNORE INTO alarms"
            "(device_code, param_key, level, started_at, peak_value, peak_at,"
            " threshold_lo, threshold_hi, breach_count, message, pile_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (dev, pk, p.alarm_level.value, _epoch_to_iso(st.breach_since),
             value, now_utc_iso(), p.lo, p.hi, 1, msg, pile_id),
        )
        if cur.rowcount == 0:
            # 唯一索引挡住了：库里已有一条活跃报警（崩溃残留或并发）。
            # 认领它，绝不新开第二条。
            row = conn.execute(
                "SELECT alarm_id, peak_value, breach_count FROM alarms"
                " WHERE device_code=? AND param_key=? AND ended_at IS NULL",
                (dev, pk)).fetchone()
            if row is None:                 # pragma: no cover - 理论上不可达
                return []
            st.active_id = row["alarm_id"]
            st.peak_value = row["peak_value"]
            st.breach_count = row["breach_count"] or 1
            log.warning("认领已存在的活跃报警 #%s（%s / %s），不重复开警",
                        st.active_id, dev, pk)
            return []

        st.active_id = cur.lastrowid
        st.peak_value = value
        st.peak_at = now_utc_iso()
        st.breach_count = 1
        st.last_peak_write = ts
        st.clear_since = None
        log.warning("开警 #%s %s %s：%s", st.active_id, dev, p.name, msg)
        return [AlarmEvent("open", dev, pk, p.alarm_level.value, value,
                           st.active_id, msg)]

    def _maybe_close(self, conn, p: Param, dev: str, pk: str, value: float,
                     ts: float, st: _State, active: bool = True) -> list[AlarmEvent]:
        # 记录越限最严重的那一帧
        if st.peak_value is None or excursion(p, value) > excursion(p, st.peak_value):
            st.peak_value = value
            st.peak_at = now_utc_iso()

        # 子系统停机 -> 当作已恢复，走清除流程
        if active and not p.clear_cond(value):
            # 仍在报警状态（含回滞带内）：累计帧数，节流回写
            st.clear_since = None
            st.breach_count += 1
            if ts - st.last_peak_write >= PEAK_WRITE_INTERVAL_S:
                st.last_peak_write = ts
                conn.execute(
                    "UPDATE alarms SET peak_value=?, peak_at=?, breach_count=?"
                    " WHERE alarm_id=?",
                    (st.peak_value, st.peak_at, st.breach_count, st.active_id))
            return []

        if st.clear_since is None:
            st.clear_since = ts             # 刚恢复，开始计时
            return []
        if ts - st.clear_since < p.clear_after_s:
            return []                       # 恢复得还不够久

        alarm_id = st.active_id
        if active:
            msg = build_message(p, value, opening=False)
        else:
            dep = p.active_when[0] if p.active_when else ""
            msg = f"{p.name} 判定已停止（{dep} 停机），报警自动解除"
        conn.execute(
            "UPDATE alarms SET ended_at=?, peak_value=?, peak_at=?, breach_count=?"
            " WHERE alarm_id=?",
            (_epoch_to_iso(st.clear_since), st.peak_value, st.peak_at,
             st.breach_count, alarm_id))

        st.active_id = None
        st.breach_since = None
        st.clear_since = None
        st.peak_value = None
        st.peak_at = ""
        st.breach_count = 0
        log.info("解除 #%s %s %s：%s", alarm_id, dev, p.name, msg)
        return [AlarmEvent("close", dev, pk, p.alarm_level.value, value,
                           alarm_id, msg)]

    # ---- 仅供界面 ----
    def snapshot(self) -> list[dict]:
        """当前内存里的活跃判定（不查库），用于诊断页。"""
        with self._lock:
            out = []
            for (dev, pk), st in self._states.items():
                if st.active_id is None:
                    continue
                p = self._params.get(pk)
                out.append({
                    "device_code": dev, "param_key": pk,
                    "name": p.name if p else pk,
                    "level": p.alarm_level.value if p and p.alarm_level else "",
                    "alarm_id": st.active_id,
                    "peak": st.peak_value,
                    "frames": st.breach_count,
                })
            return out


# --------------------------------------------------------------------------
# 批量喂数与查询
# --------------------------------------------------------------------------
def feed_frames(engine: AlarmEngine, conn: sqlite3.Connection,
                records: list[dict]) -> list[AlarmEvent]:
    """把一批帧按时间顺序喂给判定器，返回所有动作。

    必须先按 ts_epoch 排序再喂。补传的历史数据会让时间"倒着走"，
    倒序喂会把"持续了多少秒"算成负数，判定就废了。
    """
    ordered = sorted(records, key=lambda r: (str(r.get("device_code", "")),
                                             int(r.get("ts_epoch", 0))))
    events: list[AlarmEvent] = []
    keys = engine.alarmable_keys
    for rec in ordered:
        dev = str(rec.get("device_code", ""))
        pile = str(rec.get("pile_id", ""))
        ts_epoch = int(rec.get("ts_epoch", 0))
        vals = rec.get("values") or {}
        for pk in keys:
            if pk not in vals:
                continue
            try:
                v = float(vals[pk])
            except (TypeError, ValueError):
                continue
            p = engine.params[pk]
            events.extend(engine.observe(conn, dev, pk, v, ts_epoch, pile,
                                         active=p.is_active(vals)))
    return events


def close_all_active(conn: sqlite3.Connection, reason: str = "平台重置") -> int:
    """把所有活跃报警标记为已解除。**只用于 --reset 和演示收尾**。"""
    cur = conn.execute("UPDATE alarms SET ended_at=? WHERE ended_at IS NULL",
                       (now_utc_iso(),))
    n = cur.rowcount
    if n:
        log.info("批量解除 %d 条活跃报警（%s）", n, reason)
    return n


def query(conn: sqlite3.Connection, *, device_code: str = "", active_only: bool = False,
          limit: int = 200) -> list[dict]:
    """查报警列表，新的在前。补上中文参数名与时长。"""
    sql = "SELECT * FROM alarms WHERE 1=1"
    args: list = []
    if device_code:
        sql += " AND device_code=?"
        args.append(device_code)
    if active_only:
        sql += " AND ended_at IS NULL"
    sql += " ORDER BY started_at DESC LIMIT ?"
    args.append(int(limit))

    params = registry()
    now = now_epoch()
    out: list[dict] = []
    for row in conn.execute(sql, args).fetchall():
        d = dict(row)
        p = params.get(d["param_key"])
        d["param_name"] = p.name if p else d["param_key"]
        d["param_unit"] = p.unit if p else ""
        d["level_label"] = {"warning": "预警", "critical": "报警"}.get(
            d["level"], d["level"])
        d["active"] = d["ended_at"] is None
        end = now if d["active"] else (
            to_datetime(d["ended_at"]).timestamp() if d["ended_at"] else now)
        try:
            d["duration_s"] = max(0, int(end - to_datetime(d["started_at"]).timestamp()))
        except (ValueError, TypeError):
            d["duration_s"] = 0
        out.append(d)
    return out


def stats(conn: sqlite3.Connection, device_code: str = "") -> dict:
    """报警统计，平台首页用。"""
    where = " WHERE device_code=?" if device_code else ""
    args = (device_code,) if device_code else ()
    total = conn.execute(f"SELECT COUNT(*) FROM alarms{where}", args).fetchone()[0]
    active = conn.execute(
        f"SELECT COUNT(*) FROM alarms{where}{' AND' if where else ' WHERE'}"
        " ended_at IS NULL", args).fetchone()[0]
    day_ago = now_epoch() - 86400
    recent = conn.execute(
        f"SELECT COUNT(*) FROM alarms{where}{' AND' if where else ' WHERE'}"
        " started_at >= ?",
        args + (datetime.fromtimestamp(day_ago, UTC).isoformat(timespec="milliseconds"),)
    ).fetchone()[0]
    return {"total": total, "active": active, "last_24h": recent}
