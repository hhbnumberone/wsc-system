# -*- coding: utf-8 -*-
"""车载终端主程序：把采集、落盘、上传、界面装配成一个进程。

    python -m terminal.app [--port 8701] [--reset] [--no-upload]

**这个模块只做装配和生命周期**，不做任何业务判定 —— 采集循环是一次 tick
一帧数据，上传状态机在 `uploader.py`，桩号在 `pile.py`。这样每个部件都能
单独测，装在一起也不会互相污染。

一次 tick 的顺序（顺序本身是有讲究的）：

    读数据源 → 组装帧 → **先落 CSV 再入 outbox** → 循环判定 → 更新界面快照

落盘在上传之前、循环判定在落盘之后：判定用的方量差值取自 `pump_volume`
计数器，必须以"已经落盘的那一帧"为准，否则界面上的循环方量和平台算出来的
会对不上（平台只认上传上去的帧）。

**没有桩号清单就不出数。** 平台没下发清单时，帧的 `pile_id` 只能空着，
而平台会以 `PILE_NOT_ISSUED` 整条拒收 —— 与其造一批注定被拒的数据、
在界面上显示成"上传失败"，不如根本不出数并在界面写明原因。
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time

if __package__ in (None, ""):  # 允许 python terminal/app.py 直接跑
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from can_bus import CanSourceError, build as build_source
from common import config, ids, logging_setup
from common.enums import Quality
from common.machine import PILE_STEP_M, REBOUND_RATE, SPRAY_LOCATIONS
from common.params import KIND_STATE, PARAM_KEYS, groups
from common.timeutil import (fmt_clock, now_epoch, now_utc_iso, to_datetime)

from . import usb_sync
from .client import PlatformClient
from .network import LinkMonitor, LinkProbe
from .pile import PileRegistry, parse_mileage
from .store import LocalStore
from .uploader import HeartbeatThread, Uploader

log = logging.getLogger("terminal")

#: 桩号选择的持久化键。重启后回到上次选的桩号 ——
#: 掌子面只会往前走，重启不该让人重新找一遍。
META_CURRENT_PILE = "current_pile"


# ==========================================================================
# 界面快照
# ==========================================================================
class LiveState:
    """采集线程写给界面的最新状态。

    **单写多读**：采集线程整体替换，界面线程整体取走。一把锁保护一次替换，
    不出现"取到半新半旧"的 16 个参数 —— 那在波形上会显示成一帧尖刺。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._d: dict = {
            "ready": False,
            "message": "正在启动…",
            "ts": "",
            "ts_epoch": 0,
            "clock": "",
            "seq": 0,
            "pile_id": "",
            "quality": int(Quality.OK),
            "values": {},
            "groups": [],
            "phase": "",
            "phase_label": "",
            "phase_left_s": 0.0,
            "cycle": None,
            "cycles": [],
            "frames": 0,
            "read_errors": 0,
            "write_errors": 0,
            "last_error": "",
        }

    def update(self, **kw) -> None:
        with self._lock:
            self._d.update(kw)

    def snapshot(self) -> dict:
        with self._lock:
            d = dict(self._d)
        d["values"] = dict(d.get("values") or {})
        d["groups"] = [dict(g) for g in (d.get("groups") or [])]
        d["cycles"] = list(d.get("cycles") or [])
        return d


def _param_rows(values: dict) -> list[dict]:
    """按注册表分组渲染参数表。阈值只有注册表一个来源，这里不写任何数字。"""
    out = []
    for gname, params in groups().items():
        rows = []
        for p in params:
            v = values.get(p.key)
            if v is None:
                continue
            active = p.is_active(values)
            rows.append({
                "key": p.key, "name": p.name, "unit": p.unit,
                "value": round(float(v), 4),
                "display": p.format(v),
                "lo": p.lo, "hi": p.hi,
                "kind": p.kind,
                "is_state": p.kind == KIND_STATE,
                "alarmable": p.alarmable,
                # 越限 ≠ 报警：真正的报警由平台按持续时间和回滞判定（server/alarms.py）。
                # 这里只是让现场的人一眼看到"哪个数不对劲"。
                "active": active,
                # 只有参与报警判定的参数才标红。`hyd_sys_pressure` 这类
                # 注册表里就没挂报警的参数，越限也不该在界面上吓人。
                "over": bool(p.alarmable and active and p.raise_cond(float(v))),
            })
        if rows:
            out.append({"name": gname, "params": rows})
    return out


# ==========================================================================
# 喷浆循环判定
# ==========================================================================
class CycleTracker:
    """喷浆循环判定：工作状态 + 泵送压力起来 → 开始；压力落下去 → 结束。

    **三个地方都要能算，且必须算得一样**

    终端（这里）算出循环是为了本地界面显示和推进桩号；平台侧
    （`server/api.py::_process_frames`）拿到帧后会用同样的阈值再算一遍，
    那个才是日志数据的依据；`params.py` 提供参数定义。三处共用
    `config.acquisition` 的四个阈值，改一处三处都变。

    结束时按 `params.LOG_FIELDS` 产出日志数据的物理字段（身份字段由
    `TerminalApp.on_cycle_end` 快照补齐）。方量取
    `circulatingPumpingVolume` 计数器的差值，判定点漂移几秒不影响方量。
    """

    def __init__(self, cfg: dict):
        acq = cfg["acquisition"]
        self.start_pressure = float(acq["cycle_start_pressure"])
        self.start_hold_s = float(acq["cycle_start_hold_s"])
        self.end_pressure = float(acq["cycle_end_pressure"])
        self.end_hold_s = float(acq["cycle_end_hold_s"])
        self.active: dict | None = None
        self._above_since: float | None = None
        self._below_since: float | None = None
        self._n_done = 0          # 已完成的循环数：喷射部位按它轮换
        self._work_hours = 0.0    # 循环累计工作时长（小时）

    def update(self, values: dict, ts_epoch_ms: int, *, pile_id: str = "") -> dict | None:
        """喂一帧。返回**刚刚结束**的循环（如果有）。"""
        ts = ts_epoch_ms / 1000.0
        try:
            press = float(values.get("cylinderPumpingPressure", 0.0))
            working = float(values.get("workStatus", 0.0))
            vol = float(values.get("circulatingPumpingVolume", 0.0))
        except (TypeError, ValueError):
            return None

        spraying = working >= 0.5 and press > self.start_pressure
        if spraying:
            self._below_since = None
            if self._above_since is None:
                self._above_since = ts
        else:
            self._above_since = None

        if self.active is None:
            if (self._above_since is not None
                    and ts - self._above_since >= self.start_hold_s):
                loc = parse_mileage(pile_id)
                self.active = {
                    "started_at": to_datetime(ts).isoformat(timespec="seconds"),
                    "ts_epoch": ts_epoch_ms,
                    "pile_id": pile_id,
                    "locationMetre": loc,
                    "start_volume": vol,
                    "max_pressure": press,
                    "sum_pressure": 0.0,
                    "frames": 0,
                }
                log.info("喷浆循环开始：%s（%s）", pile_id or "未选桩号",
                         fmt_clock(ts))
            return None

        cur = self.active
        # 速凝剂两个用量随帧跟进：计数器在本循环内单调增长，结束帧的值即整循环用量。
        # 只在**喷浆帧**上跟进 —— 闭合判定发生在停机帧上，而模拟器停机时
        # 循环计数器已清零，拿停机帧的值会把整个循环算成 0。
        if working >= 0.5:
            cur["last_vol"] = vol
            cur["accel_once"] = float(values.get("acceleratorOnceDosage",
                                                 cur.get("accel_once", 0.0)) or 0.0)
            cur["accel_cum"] = float(values.get("acceleratorCumulativeDosage",
                                                cur.get("accel_cum", 0.0)) or 0.0)
        if press < self.end_pressure:
            if self._below_since is None:
                self._below_since = ts
            if ts - self._below_since >= self.end_hold_s:
                cur["ended_at"] = to_datetime(ts).isoformat(timespec="seconds")
                cur["duration_s"] = round(ts - cur["ts_epoch"] / 1000.0, 1)
                vol_last = cur.pop("last_vol", vol)
                cur["volume_m3"] = round(max(0.0, vol_last - cur["start_volume"]), 3)
                cur["avg_pressure"] = round(
                    cur["sum_pressure"] / cur["frames"], 2) if cur["frames"] else None
                cur["max_pressure"] = round(cur["max_pressure"], 2)
                cur.pop("sum_pressure", None)
                # ---- 日志数据字段（params.LOG_FIELDS 的物理部分）----
                loc = cur.pop("locationMetre", None)
                cur["endMetre"] = (
                    round(float(loc) + PILE_STEP_M, 1) if loc is not None else "")
                cur["actualSprayingVolume"] = round(
                    cur["volume_m3"] * (1.0 - REBOUND_RATE), 3)
                cur["acceleratorOnceDosage"] = round(cur.pop("accel_once", 0.0), 2)
                cur["acceleratorCumulativeDosage"] = round(cur.pop("accel_cum", 0.0), 2)
                cur["sprayLocation"] = SPRAY_LOCATIONS[
                    self._n_done % len(SPRAY_LOCATIONS)]
                self._work_hours += cur["duration_s"] / 3600.0
                cur["accumulatedWorkingHours"] = round(self._work_hours, 2)
                self._n_done += 1
                self.active = None
                self._below_since = None
                log.info("喷浆循环结束：%s 方量 %.2f m³，历时 %.0f 秒，峰值 %.1f MPa",
                         cur["pile_id"] or "未选桩号", cur["volume_m3"],
                         cur["duration_s"], cur["max_pressure"])
                return cur
        else:
            self._below_since = None
            cur["max_pressure"] = max(cur["max_pressure"], press)
        cur["sum_pressure"] += press
        cur["frames"] += 1
        return None

    def snapshot(self) -> dict | None:
        """正在进行的循环（供界面显示"本次已喷 xx 方"）。"""
        if not self.active:
            return None
        cur = dict(self.active)
        cur.pop("sum_pressure", None)
        cur["avg_pressure"] = round(
            self.active["sum_pressure"] / self.active["frames"], 2) \
            if self.active["frames"] else None
        return cur


# ==========================================================================
# 采集循环
# ==========================================================================
class AcquisitionLoop(threading.Thread):
    """按固定节拍采样。**终端里唯一读数据源的线程。**"""

    def __init__(self, app: "TerminalApp"):
        super().__init__(name="acquisition", daemon=True)
        self.app = app
        cfg = app.cfg
        self.interval = float(cfg["acquisition"]["sample_interval_s"])
        self.device = str(cfg["device"]["device_code"])
        self.store = app.store
        self.source = app.source
        self.live = app.live
        self.cycles = app.tracker
        # 名字必须是 `_stop_evt`，**不能图省事写成 `_stop`**：`threading.Thread._stop()`
        # 是解释器的内部方法，Python ≤3.12 上还在。被这个属性盖住之后，线程结束、
        # 解释器回收线程状态时会去调 `self._stop()`，拿到的是个 Event，直接抛
        # `TypeError: 'Event' object is not callable`。
        #
        # 阴险的地方在于**新版 Python 上完全看不出问题**：3.13 起 CPython 删掉了
        # 那个内部方法，盖不盖都一样。所以这份代码在 3.14 上 347 个测试全绿，
        # 换 3.12 跑就崩掉 6 个线程用例 —— 问题不在测试，在名字。
        # （同一类坑见 uploader.py 里 `_started` 那条注释。）
        self._stop_evt = threading.Event()
        self._seq = self._initial_seq()
        self._rounds = 0
        self._frames = 0
        self._read_errors = 0
        self._write_errors = 0

    def _initial_seq(self) -> int:
        """帧序号从库里已有的最大值之后接着走。

        序号**不需要**全局唯一：`record_id` 里还有毫秒时间戳
        （见 `common/ids.frame_record_id`），同一个毫秒内的两帧才需要序号区分。
        所以这里只求"比历史大"，重启后接着数就行。
        """
        try:
            row = self.store.conn.execute(
                "SELECT MAX(seq) AS s FROM outbox").fetchone()
            return int(row["s"] or 0) + 1
        except Exception:  # pragma: no cover - 库坏了也不该拦住采集
            return 1

    # ------------------------------------------------------------------
    def run(self) -> None:
        """采样节拍。

        **采样时刻对齐到固定网格**（`base_epoch + n×间隔`），而不是"每次醒来
        的时刻" —— 后者会带上调度抖动，曲线的时间轴上全是毛刺，平台侧按
        等间隔做聚合也吃亏。

        同时记录**这一拍实际晚了多少**：`late_s` 超过半个周期就把这一帧标成
        可疑。晚到的读数和它声称的时刻对不上，这件事必须在数据里留痕，
        而不是让曲线看起来一切正常。
        """
        log.info("采集已启动：%s，每 %.1f 秒一帧", self.source.name, self.interval)
        base = time.monotonic()
        base_epoch = now_epoch()
        n = 0
        try:
            while not self._stop_evt.is_set():
                target = base + n * self.interval
                late = max(0.0, time.monotonic() - target)
                if late > self.interval:
                    # 落后太多（机器休眠、断点调试）就重排网格。
                    # **不补采**：那段时间确实没采到，凭空造出一串"当时"的帧
                    # 就是把没测过的数写成实测数据 —— 缺口留着，平台上看得见。
                    log.warning("采集落后 %.1f 秒，重排采样网格（不补采）", late)
                    base = time.monotonic() - n * self.interval
                    base_epoch = now_epoch() - n * self.interval
                    late = 0.0
                n += 1
                self._tick(base_epoch + n * self.interval, late)
                self._rounds += 1
                self._stop_evt.wait(max(0.0, base + n * self.interval - time.monotonic()))
        finally:
            self.store.close_thread_conn()

    def _tick(self, ts_epoch: float, late_s: float = 0.0) -> None:
        ts = to_datetime(ts_epoch)

        # ---- 没桩号就不采（见模块开头） ----
        # 两种"没桩号"都要拦住：清单没下发，和清单有了但没点选。缺了后半个
        # 判断，"清单已到、还没选桩号"的那几秒会产出一批 pile_id 为空的帧 ——
        # 它们在平台上必定 PILE_NOT_ISSUED 整条拒收，白占一份 outbox。
        piles = self.app.piles
        if not piles.ready:
            self.live.update(ready=False, message="未取得平台下发的桩号清单，暂不采集",
                             clock=fmt_clock(ts_epoch))
            return
        if not piles.current:
            self.live.update(ready=False, clock=fmt_clock(ts_epoch),
                             message="尚未选择作业桩号，暂不采集（在桩号清单里点选一个）")
            return

        # ---- 读数据源 ----
        try:
            values = self.source.read(ts)
        except CanSourceError as exc:
            self._read_errors += 1
            self.live.update(ready=True, message=f"数据源异常：{exc}",
                             read_errors=self._read_errors,
                             last_error=str(exc), clock=fmt_clock(ts_epoch))
            log.warning("跳过一帧：%s", exc)
            return
        except Exception as exc:  # pragma: no cover - 驱动层面的意外
            log.exception("数据源读取异常")
            self._read_errors += 1
            self.live.update(ready=True, message=f"数据源异常：{exc}",
                             read_errors=self._read_errors,
                             last_error=f"{type(exc).__name__}: {exc}",
                             clock=fmt_clock(ts_epoch))
            return

        # ---- 组装 ----
        ts_ms = int(ts_epoch * 1000)
        # 设备编号**读一次用到底**：`record_id` 和 `device_code` 必须来自同一个
        # 编号，否则现场刚好在这一瞬改了设备编号（见 TerminalApp.apply_identity）
        # 就会造出"ID 是旧编号、字段是新编号"的帧 —— 平台按条校验这一致性，
        # 整批拒收，而终端只看到"又被拒了"，查不出原因。
        device = self.device
        # 迟到采到的帧标成可疑：值可能已经不是它声称的那个时刻的了。
        # 与其悄悄当成正常数据，不如让平台和界面上都看得出来。
        quality = int(Quality.SUSPECT) if late_s > self.interval / 2 else int(Quality.OK)
        record = {
            "record_id": ids.frame_record_id(device, ts_ms, self._seq),
            "device_code": device,
            "ts_device": ts.isoformat(timespec="milliseconds"),
            "ts_epoch": ts_ms,
            "seq": self._seq,
            "pile_id": self.app.piles.current,
            "quality": quality,
            "source": "can",
            "values": {k: float(values[k]) for k in PARAM_KEYS},
        }
        self._seq += 1

        # ---- 落盘：CSV 先，outbox 后（顺序见 store.append 的说明） ----
        try:
            self.store.append(record)
        except ValueError as exc:
            # strict 校验拦下的值：采集层或传感器的 bug，不是数据。
            # 这种值平台一定会拒收，与其排进队里变成永久 REJECTED，不如就地暴露。
            self._read_errors += 1
            self.live.update(ready=True, read_errors=self._read_errors,
                             message=f"数值越界，已丢弃：{exc}", last_error=str(exc),
                             clock=fmt_clock(ts_epoch))
            log.warning("丢弃越界帧：%s", exc)
            return
        except Exception as exc:  # pragma: no cover - 磁盘满/权限
            log.exception("落盘失败")
            self._write_errors += 1
            self.live.update(ready=True, write_errors=self._write_errors,
                             message=f"落盘失败：{exc}", last_error=str(exc))
            return

        # ---- 循环判定 ----
        done = self.cycles.update(record["values"], ts_ms,
                                  pile_id=record["pile_id"])
        if done is not None:
            self.app.on_cycle_end(done)

        # ---- 界面快照 ----
        # 工况（喷浆/待机/移机）只有模拟器知道；真车数据源没有这个概念，
        # 取不到就留空，界面自然不显示那一段。
        src = self.source.snapshot()
        self._frames += 1
        self.live.update(
            ready=True, message="", ts=record["ts_device"], ts_epoch=ts_ms,
            clock=fmt_clock(ts_epoch), seq=record["seq"],
            pile_id=record["pile_id"], quality=quality,
            values=record["values"], groups=_param_rows(record["values"]),
            frames=self._frames, read_errors=self._read_errors,
            write_errors=self._write_errors, last_error="",
            cycle=self.cycles.snapshot(),
            phase=src.get("phase", ""), phase_label=src.get("phase_label", ""),
            phase_left_s=src.get("phase_left_s", 0.0),
        )

    def stop(self) -> None:
        self._stop_evt.set()


# ==========================================================================
# 后台小线程
# ==========================================================================
class PileWatcher(threading.Thread):
    """看心跳里的清单版本，平台换清单了就重拉。

    单独一个线程而不是搭在采集循环里：拉清单要走 HTTP，可能卡三秒。
    采集不能因为平台慢就停摆 —— 现场最需要的恰恰是"平台不通的时候本地还在采"。
    """

    def __init__(self, app: "TerminalApp", interval_s: float = 15.0):
        super().__init__(name="pile-watch", daemon=True)
        self.app = app
        self.interval = interval_s
        # 叫 `_stop_evt` 而不叫 `_stop` 的原因，见 AcquisitionLoop.__init__ 的说明。
        self._stop_evt = threading.Event()

    def run(self) -> None:
        try:
            while not self._stop_evt.wait(self.interval):
                try:
                    self.tick()
                except Exception:  # pragma: no cover - 后台线程不能死
                    log.exception("桩号清单检查异常")
        finally:
            self.app.store.close_thread_conn()

    def tick(self) -> None:
        advice = self.app.hb.snapshot().get("advice") or {}
        version = str(advice.get("pile_list_version", ""))
        self.app.piles.note_advice(version)
        if self.app.piles.needs_refresh(version):
            log.info("平台桩号清单已更新（%s），重新拉取", version or "无版本号")
            self.app.piles.refresh()
            self.app.remember_pile()

    def stop(self) -> None:
        self._stop_evt.set()


class RetentionThread(threading.Thread):
    """按保留期清理本地 CSV。**绝不会删掉还有未上传数据的文件**（见 store.cleanup）。"""

    def __init__(self, app: "TerminalApp"):
        super().__init__(name="retention", daemon=True)
        self.app = app
        self.interval = float(app.cfg["retention"]["collect_interval_s"])
        # 叫 `_stop_evt` 而不叫 `_stop` 的原因，见 AcquisitionLoop.__init__ 的说明。
        self._stop_evt = threading.Event()

    def run(self) -> None:
        try:
            while not self._stop_evt.wait(self.interval):
                try:
                    rep = self.app.store.cleanup()
                    self.app.live.update(cleanup=rep)
                except Exception:  # pragma: no cover
                    log.exception("本地清理异常")
        finally:
            self.app.store.close_thread_conn()

    def stop(self) -> None:
        self._stop_evt.set()


# ==========================================================================
# 应用
# ==========================================================================
class TerminalApp:
    """终端进程的全部部件。"""

    def __init__(self, cfg: dict | None = None, *, reset: bool = False,
                 with_upload: bool = True):
        config.ensure_default_files()
        self.cfg = cfg or config.load_config()
        self.device = str(self.cfg["device"]["device_code"])
        self.with_upload = with_upload
        if reset:
            self._reset_local()

        self.store = LocalStore(cfg=self.cfg)
        self.client = PlatformClient(self.cfg)
        self.link = LinkMonitor(self.cfg)
        self.piles = PileRegistry(self.store, self.client, self.cfg)
        self.live = LiveState()
        self.tracker = CycleTracker(self.cfg)
        self._cycles: list[dict] = []
        self._clock = threading.Lock()
        #: 现场录入身份（工点/设备编号）时串行化。改身份要动 4 份副本 +
        #: 桩号清单 + 两个配置文件，两个人同时点"保存并生效"必然会撕裂。
        self._identity_lock = threading.Lock()

        # 数据源建不起来**不能**让终端起不来：现场最常见的情况就是 CAN 卡
        # 还没插好。这时候要的是界面明确显示"没数据"，而不是整个终端黑掉。
        self.source_error = ""
        try:
            self.source = build_source(self.cfg)
        except CanSourceError as exc:
            self.source_error = str(exc)
            log.error("数据源不可用：%s", exc)
            from can_bus.base import FakeCan
            self.source = FakeCan(name="不可用")

        self.acq = AcquisitionLoop(self)
        self.probe = LinkProbe(self.link, self.client, self.cfg)
        self.uploader = Uploader(self.store, self.client, self.link, self.cfg)
        self.hb = HeartbeatThread(self.store, self.client, self.link, self.cfg)
        self.pilewatch = PileWatcher(self)
        self.retention = RetentionThread(self)
        self._threads = [self.acq, self.probe, self.hb, self.pilewatch, self.retention]
        if with_upload:
            self._threads.append(self.uploader)

    # ------------------------------------------------------------------
    @staticmethod
    def _reset_local() -> None:
        """清空本地库与 CSV。`--reset` 走这里，只动终端自己的数据。"""
        for path in (config.LOCAL_DB, config.LOCAL_DB + "-wal", config.LOCAL_DB + "-shm"):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as exc:  # pragma: no cover
                log.warning("删除 %s 失败：%s", path, exc)
        frames = config.FRAMES_DIR
        if os.path.isdir(frames):
            for name in os.listdir(frames):
                if name.endswith(".csv"):
                    try:
                        os.remove(os.path.join(frames, name))
                    except OSError:  # pragma: no cover - Excel 开着
                        log.warning("CSV 被占用，未能删除：%s", name)
        log.warning("--reset：已清空终端本地库与 CSV")

    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动全部后台线程。**上传线程先做崩溃回收，再开始跑。**"""
        if self.with_upload:
            self.uploader.recover()
        self._bootstrap()
        for t in self._threads:
            t.start()

    def _bootstrap(self) -> None:
        """起跑前的准备：桩号清单、当前桩号。"""
        if not self.piles.ready:
            self.piles.refresh()          # 不抛异常，失败就留着空清单
        self.remember_pile(initial=True)
        if not self.piles.ready:
            log.warning("尚未取得桩号清单：采集将等待平台下发后再开始")
        elif not self.piles.current:
            log.warning("未选择桩号：请在终端界面点选作业桩号")

    def remember_pile(self, *, initial: bool = False) -> str:
        """把当前桩号落到本地库，重启后不必重新找。

        没选过就默认选**清单里最深的那个** —— 掌子面只会往前走，
        隧道越挖越深，这是唯一一个不需要问人的默认值。
        """
        cur = self.piles.current
        if not cur:
            cand = self.store.meta(META_CURRENT_PILE, "")
            if not cand and initial:
                cand = (self.piles.ids() or [""])[-1]
            if cand:
                try:
                    cur = self.piles.select(cand)
                except ValueError:
                    # 存着的桩号已经被平台撤掉了，退到最深处
                    ids_ = self.piles.ids()
                    if ids_:
                        cur = self.piles.select(ids_[-1])
        if cur:
            self.store.set_meta(META_CURRENT_PILE, cur)
        return cur

    # ---- 动作（界面调用） ----
    def select_pile(self, pile_id: str) -> str:
        """切换桩号。不在清单里由 `piles.select` 抛 ValueError。"""
        old = self.store.meta(META_CURRENT_PILE, "")
        cur = self.piles.select(pile_id)
        self.store.set_meta(META_CURRENT_PILE, cur)
        self.store.enqueue_audit(self._audit(
            "pile_change", {"pile_id": cur, "from": old}))
        return cur

    # ---- 身份（工点 / 设备编号）----
    def identity_pending(self) -> dict:
        """换身份前必须结清的欠账：三个队列的非终态行数。

        **为什么连留痕和修订也算**：它们的 `batch_id` 是用设备编号算出来的
        （见 common/ids.make_batch_id），旧编号下产生的事件只有在旧编号下
        上传才对得上；换完再传，轻则被平台判内容不符，重则把一条历史事件
        挂到新编号名下 —— 留痕挂错人，比没留痕更糟。
        """
        st = self.store.stats()
        p = {"pending": st["pending"], "inflight": st["inflight"],
             "revisions": st["revisions_pending"], "audits": st["audits_pending"]}
        p["total"] = sum(p.values())
        return p

    def apply_identity(self, site_id: str, device_code: str) -> dict:
        """现场录入的工点 ID / 设备编号**生效**。两侧都过平台核对，落配置，留痕。

        步骤顺序是刻意的 —— 每一步都能单独失败而不留半截状态：

        1. **闸门**：三个队列还有欠账就不许换（理由见 `identity_pending`）。
        2. **平台核对**：工点必须在平台档案里，否则拒。设备编号可以新自报。
        3. **先换桩号清单**：新工点的清单拉不到就整个中止，`site_id` 回滚。
           没有清单就切过去的话，采出来的每一条都会因桩号未下发被拒收 ——
           那比"没改成"糟得多。
        4. **换身份**：4 份副本、当前桩号、配置文件、留痕事件。

        抛 `ValueError` 表示"没改成"（webui 会转成 400 带原因给界面看）；
        抛 `http_util.NetworkError` 表示连不上平台 —— 那是另一回事，
        界面要提示的是查网络而不是改输入。
        """
        new_site = (site_id or "").strip()
        new_dev = (device_code or "").strip()
        if not new_site:
            raise ValueError("工点 ID 不能为空")
        if not new_dev:
            raise ValueError("设备编号不能为空")

        with self._identity_lock:
            old_site = self.piles.site_id
            old_dev = self.device
            if new_site == old_site and new_dev == old_dev:
                raise ValueError(
                    f"工点和设备编号都没有变化（{old_site} / {old_dev}）")

            # ---- 1. 闸门 ----
            pend = self.identity_pending()
            if pend["total"]:
                raise ValueError(
                    f"还有 {pend['total']} 条欠账没传完（待传 {pend['pending']}、"
                    f"发送中 {pend['inflight']}、修订 {pend['revisions']}、"
                    f"留痕 {pend['audits']}）。旧编号的数据只能以旧编号上传，"
                    f"这时候换编号，平台会按设备不匹配把它们整批拒收。"
                    f"请先点『立即上传』，等欠账清空后再改。")

            # ---- 2. 平台核对（判定权在平台） ----
            verdict = self.client.check_identity(new_site, new_dev)
            if not verdict.get("ok"):
                raise ValueError(self._refusal_text(verdict))

            # ---- 3. 先换清单 ----
            self.piles.site_id = new_site
            rep = self.piles.refresh(force=True)
            if not rep.get("ok"):
                # 清单没换成功 —— 缓存里还是旧工点的，把 site_id 也拨回去，
                # 让"本地状态"整体停在旧身份上，而不是一半新一半旧。
                self.piles.site_id = old_site
                raise ValueError(
                    f"新工点的桩号清单没拉到（{rep.get('reason', '')}）。"
                    f"身份未改动 —— 没有清单就不该切过去，"
                    f"否则采出来的每一条都会因桩号未下发被平台拒收。")

            # ---- 4. 生效 ----
            info = verdict.get("site") or {}
            # 生产者先切：它是唯一往库里写帧的线程。
            self.acq.device = new_dev
            self.device = new_dev
            self.uploader.device = new_dev
            self.hb.device = new_dev
            self.hb.site_id = new_site

            # 当前桩号基本不可能还在新清单里（refresh 已自动切到新清单第一个）。
            # 走 select_pile 落库 + 留痕，不另写一套。
            old_pile = self.store.meta(META_CURRENT_PILE, "")
            new_pile = self.piles.current
            if new_pile and new_pile != old_pile:
                self.select_pile(new_pile)

            site = config.load_site()
            for k in ("site_id", "site_name", "project_name", "tunnel", "line"):
                if info.get(k):
                    site[k] = info[k]
            config.save_site(site)

            dev = dict(self.cfg.get("device") or {})
            dev["device_code"] = new_dev
            # 设备名里通常嵌着编号（"湿喷台车 GHP9015H"）。换了编号还留着
            # 旧名字，界面上会一直显示一台不存在的车。只替换编号那一截。
            name = str(dev.get("device_name", ""))
            if old_dev and old_dev in name:
                dev["device_name"] = name.replace(old_dev, new_dev)
            config.save_device(dev)
            self.cfg["device"] = dev          # 让 snapshot() 立刻反映，不等重启

            self.store.enqueue_audit(self._audit("identity_change", {
                "from": {"site_id": old_site, "device_code": old_dev,
                         "pile_id": old_pile},
                "to": {"site_id": new_site, "device_code": new_dev,
                       "site_name": info.get("site_name", ""),
                       "pile_id": new_pile},
                "source": "terminal_ui",
            }))
            log.warning("现场录入身份已生效：工点 %s -> %s，设备 %s -> %s（桩号 %s）",
                        old_site, new_site, old_dev, new_dev, new_pile or "无")

            self.uploader.poke()      # 新编号下的数据尽早传上去
            self.probe.poke()
            return {
                "ok": True,
                "from": {"site_id": old_site, "device_code": old_dev,
                         "pile_id": old_pile},
                "to": {"site_id": new_site, "device_code": new_dev,
                       "site_name": info.get("site_name", ""),
                       "line": info.get("line", ""),
                       "pile_count": info.get("pile_count", 0),
                       "first_pile": info.get("first_pile", ""),
                       "last_pile": info.get("last_pile", ""),
                       "pile_id": new_pile},
                "piles": {"count": rep.get("count", 0),
                          "added": len(rep.get("added") or []),
                          "removed": len(rep.get("removed") or [])},
            }

    @staticmethod
    def _refusal_text(verdict: dict) -> str:
        """把平台的拒绝拼成一句能直接显示给人看的话。

        工点不在档案里时，平台会附上已知工点列表 —— 现场正需要知道"该填什么"，
        所以把它拼进消息里，而不是只回一句"工点不存在"。
        """
        msg = str(verdict.get("error") or "平台不接受这个工点或设备编号")
        known = verdict.get("known_sites") or []
        if known:
            msg += "。平台已建档的工点：" + "、".join(
                f"{s.get('site_id')}（{s.get('site_name') or s.get('line') or ''}）"
                for s in known)
        return msg

    def on_cycle_end(self, cycle: dict) -> None:
        """一轮喷完：补身份快照，记进本地列表 + 推进桩号。

        日志数据的身份字段（workAreaId/cname/cphone/cidno）在这里快照 ——
        以后换操作手、换工区，不能改写已经产生的记录。
        """
        op = dict(self.cfg["device"].get("operator") or {})
        cycle["workAreaId"] = self.piles.site_id
        cycle["cname"] = str(op.get("cname", ""))
        cycle["cphone"] = str(op.get("cphone", ""))
        cycle["cidno"] = str(op.get("cidno", ""))
        with self._clock:
            self._cycles.insert(0, cycle)
            del self._cycles[12:]
        # 喷完这一格就挪到下一格 —— 推进只发生在清单内部（pile.advance），
        # 越不过平台下发的范围，这就是桩号防伪在运行期的体现。
        before = self.piles.current
        nxt = self.piles.advance()
        if nxt and nxt != before:
            self.store.set_meta(META_CURRENT_PILE, nxt)
            log.info("循环完成，作业面推进：%s -> %s", before, nxt)
        self.store.enqueue_audit(self._audit(
            "cycle_end", {k: v for k, v in cycle.items() if k != "sum_pressure"}))

    def set_manual_offline(self, offline: bool) -> str:
        """演示用的"拔网线"。不改链路状态本身，只让请求真的失败。"""
        state = self.link.set_manual_offline(offline)
        self.store.enqueue_audit(self._audit(
            "link_switch", {"manual_offline": bool(offline)}))
        if not offline:
            self.uploader.poke()
            self.probe.poke()
        return state.value

    def upload_now(self) -> int:
        """把待传数据插队上传。**只写意图，不在界面线程里发请求。**"""
        n = self.store.mark_manual()
        if n:
            log.info("手动上传：%d 条插队", n)
            self.uploader.poke()
        self.store.enqueue_audit(self._audit("manual_upload", {"records": n}))
        return n

    def inject_fault(self, key: str) -> dict:
        """注入一个预置故障（见 common/machine.FAULT_PRESETS）。"""
        if not hasattr(self.source, "inject_preset"):
            raise ValueError(f"当前数据源（{self.source.name}）不支持故障注入")
        out = self.source.inject_preset(key)
        self.store.enqueue_audit(self._audit("fault_inject", out))
        return out

    def clear_faults(self) -> int:
        if not hasattr(self.source, "clear_faults"):
            return 0
        n = self.source.clear_faults()
        if n:
            self.store.enqueue_audit(self._audit("fault_clear", {"count": n}))
        return n

    # ---- U 盘同步（见 usb_sync 模块开头关于这条通道的说明） ----
    def usb_export(self, *, scope: str = "pending", limit: int = 2000,
                   dest_dir: str | None = None) -> dict:
        """把待传数据导到 U 盘。导出动作本身也留痕。"""
        rows = self.store.export_rows(scope=scope, limit=limit)
        res = usb_sync.export_bundle(rows, self.device, dest_dir=dest_dir,
                                     scope=scope, note=self.piles.current or "")
        if res.get("ok"):
            m = res["manifest"]
            self.store.enqueue_audit(self._audit("usb_export", {
                "records": res["exported"], "dir": res["dir"], "scope": scope,
                "first": m["first_local"], "last": m["last_local"],
                "sha256": m["files"][0]["sha256"][:16],
            }))
        return res

    def usb_inspect(self, bundle: str) -> dict:
        """只校验、只比对，不落库。界面先给人看冲突报告。"""
        return usb_sync.inspect_bundle(self.store, bundle, device_code=self.device,
                                       pile_ids=self.piles.ids())

    def usb_import(self, bundle: str, *, operator: str,
                   reason_code: str = "VALUE_DISTORTED", note: str = "") -> dict:
        """把 U 盘上改动过的行转成修订批，排队等上传。"""
        res = usb_sync.import_bundle(
            self.store, bundle, device_code=self.device, pile_ids=self.piles.ids(),
            operator=operator, reason_code=reason_code, note=note)
        self.store.enqueue_audit(self._audit("usb_import", {
            "bundle": os.path.basename(bundle), "created": res.get("created", 0),
            "changed": res.get("changed", 0), "same": res.get("same", 0),
            "problems": len(res.get("errors") or []),
            "operator": operator, "reason": reason_code,
        }))
        if res.get("created"):
            # 修订批要等目标记录先上平台（uploader 里的次序），
            # 这里只负责把它叫醒，别让它等下一次心跳
            self.uploader.poke()
        return res

    def _audit(self, kind: str, payload: dict) -> dict:
        """造一条审计事件。

        审计**和数据本体分开排队**：即使数据还没传上去，操作本身也要留痕
        （见 store.enqueue_audit）。时间戳用设备时钟，和帧记录同一套。
        """
        ts_ms = int(now_epoch() * 1000)
        return {
            "event_id": ids.audit_record_id(self.device, ts_ms, self.audit_seq()),
            "device_code": self.device,
            "kind": kind,
            "ts_device": now_utc_iso(),
            "ts_epoch": ts_ms,
            "payload": payload,
        }

    def audit_seq(self) -> int:
        with self._clock:
            self._audit_n = getattr(self, "_audit_n", 0) + 1
            return self._audit_n

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        """界面要的全部状态。一次取全，避免界面上出现自相矛盾的组合。"""
        live = self.live.snapshot()
        st = self.store.stats()
        return {
            "server_time": now_utc_iso(),
            "clock": fmt_clock(now_epoch()),
            "device": {
                "device_code": self.device,
                "device_name": self.cfg["device"].get("device_name", ""),
                "model": self.cfg["device"].get("model", ""),
            },
            "project": config.load_site(),
            "identity": {
                # 界面拿它渲染"工区与设备"卡片。`device_code` 用运行期的
                # `self.device`（改完立刻反映），不是 cfg 里那份启动快照。
                "site_id": self.piles.site_id,
                "device_code": self.device,
                "pending": self.identity_pending(),
                # 日志数据里的身份字段（params.LOG_FIELDS）
                "workAreaId": self.piles.site_id,
                "cname": str((self.cfg["device"].get("operator") or {}).get("cname", "")),
                "cphone": str((self.cfg["device"].get("operator") or {}).get("cphone", "")),
                "cidno": str((self.cfg["device"].get("operator") or {}).get("cidno", "")),
            },
            "platform": {"base": self.client.base},
            "live": live,
            "source": self.source.snapshot(),
            "source_error": self.source_error,
            "piles": self.piles.snapshot(),
            "link": self.link.snapshot(),
            "upload": self.uploader.last,
            "heartbeat": self.hb.snapshot(),
            "queue": {
                "pending": st["pending"], "inflight": st["inflight"],
                "uploaded": st["uploaded"], "rejected": st["rejected"],
                "void": st["void"], "total": st["total"],
                "backlog": st["backlog"], "oldest_pending": st["oldest_pending"],
                "revisions_pending": st["revisions_pending"],
                "audits_pending": st["audits_pending"],
            },
            "storage": self.store.usage(),
            "cycles": list(self._cycles),
            "cycle": self.tracker.snapshot(),
            "sample_interval_s": float(self.cfg["acquisition"]["sample_interval_s"]),
            "upload_interval_s": float(self.cfg["upload"]["interval_s"]),
        }

    def stop(self) -> None:
        """优雅退出：先停采集（不再产生新数据），再停上传，最后关库。

        顺序反过来的话，上传线程可能在关库之后还去读 outbox。
        """
        log.info("正在停止终端…")
        self.acq.stop()
        self.probe.stop()
        self.hb.stop()
        self.pilewatch.stop()
        self.retention.stop()
        self.uploader.stop()
        self.acq.join(timeout=self.cfg["acquisition"]["sample_interval_s"] + 2.0)
        try:
            self.source.close()
        except Exception:  # pragma: no cover
            pass
        self.store.close()
        log.info("终端已停止")

    # ---- 上下文管理（测试用） ----
    def __enter__(self) -> "TerminalApp":
        return self

    def __exit__(self, *exc) -> bool:
        self.stop()
        return False


# ==========================================================================
# 入口
# ==========================================================================
def main(argv: list[str] | None = None) -> int:
    logging_setup.enable_utf8_stdout()
    cfg = config.load_config()

    ap = argparse.ArgumentParser(description="湿喷台车信息化系统 — 车载终端")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--reset", action="store_true", help="清空终端本地库与 CSV")
    ap.add_argument("--no-upload", action="store_true", help="只采集，不上传（排查用）")
    ap.add_argument("--no-ui", action="store_true", help="不开本地界面")
    args = ap.parse_args(argv)

    if args.port:
        cfg["terminal"]["port"] = args.port
    log_dir = config.LOG_DIR
    for name in ("terminal", "terminal.client", "terminal.store", "terminal.uploader",
                 "terminal.pile", "terminal.network", "terminal.webui",
                 "can_bus", "can_bus.simulator"):
        logging_setup.setup(name, log_dir, logging.INFO)
    logging_setup.setup("http.access", log_dir, logging.WARNING, to_file=False)
    logging_setup.install_excepthook(log)

    app = TerminalApp(cfg, reset=args.reset, with_upload=not args.no_upload)
    app.start()

    httpd = None
    if not args.no_ui:
        from . import webui
        httpd = webui.serve(app, cfg)
        app.live.update(ui_url=httpd.ui_url)

    stop = threading.Event()

    def _bye(signum, frame):  # pragma: no cover - 交互式
        log.info("收到退出信号")
        stop.set()

    # SIGBREAK 只有 Windows 有，但没有它 `run_all.py` 就只能 terminate()——
    # 那是直接抹掉进程，收尾流程（关库、flush CSV、传完手上那批）全跳过。
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is None:            # pragma: no cover - 非 Windows
            continue
        try:
            signal.signal(sig, _bye)
        except (ValueError, OSError):  # pragma: no cover - 非主线程
            pass

    try:
        while not stop.is_set():
            stop.wait(0.5)
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
