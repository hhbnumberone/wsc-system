# -*- coding: utf-8 -*-
"""阶段 5：采集循环、喷浆循环判定、终端界面契约。

盯的是三件别处测不到的事：

1. `CycleTracker` 的起喷/停喷判定 —— 终端界面上"喷浆循环"表里的每一行都由它决定；
2. `AcquisitionLoop` 的取舍 —— 没桩号不出数、读不到就跳过、越界就地丢弃、
   迟到要标可疑。这四条决定了平台上的曲线是"有洞但每个点都真"还是"平滑但假的"；
3. 界面契约 —— `app.js` 引用的每个元素 id 都要在 `index.html` 里存在，
   颜色 token 必须是合法的十六进制。没有浏览器可跑，但"打开是一片空白"
   和"颜色写成了 `#2a3purple`"这两种低级错误必须在这里被拦住。

不测的：真实 CAN 收发（本机没有 CAN 卡）、浏览器里的渲染效果。
"""
from __future__ import annotations

import ast
import inspect
import os
import shutil
import tempfile
import textwrap
import threading
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace

from can_bus import build as build_source
from can_bus.base import FakeCan
from common import config, http_util, ids
from common.enums import Quality
from common.machine import FAULT_BY_KEY, FAULT_PRESETS, PUMP_RATE_M3_MIN
from common.params import PARAM_BY_KEY, PARAM_KEYS, validate_values
from common.timeutil import now_epoch, now_utc
from terminal.app import AcquisitionLoop, CycleTracker, LiveState
from terminal.client import PlatformClient
from terminal.network import LinkMonitor, LinkProbe
from terminal.store import LocalStore
from terminal.uploader import HeartbeatThread, Uploader

WEB_DIR = config.TERMINAL_WEB


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fp:
        return fp.read()


def _shift(base, seconds: int):
    """`base` 之后 `seconds` 秒。**不用 timeutil 的固定时区**——
    这里只要一个能被 `datetime` 接受、能相减的时刻，时区无关。"""
    return base + timedelta(seconds=seconds)


#: `threading.Thread` 自己的属性名（公开 + 私有），覆盖 Python 3.10 ~ 3.14。
#: Thread 子类一旦用 `self.X = ...` 占住其中某个名字，就盖掉了解释器自己要用的
#: 那份，症状是线程启停时抛 `AttributeError` 或 `'Event' object is not callable`。
#:
#: **必须写成静态清单。** 不能 `hasattr(threading.Thread, name)` 现查：CPython
#: 在版本之间会增删这些私有属性，`_stop` 就是 3.13 删掉的 —— 现查的话在新版
#: Python 上一片绿，而这份代码要能在别人机器上的旧 Python 上跑。
_THREAD_RESERVED = frozenset({
    # 公开 API
    "start", "run", "join", "is_alive", "name", "ident", "native_id", "daemon",
    "getName", "setName", "isDaemon", "setDaemon",
    # 内部状态与方法
    "_started", "_is_stopped", "_stop", "_delete", "_tstate_lock", "_ident",
    "_bootstrap", "_bootstrap_inner", "_set_ident", "_set_tstate_lock",
    "_reset_internal_locks", "_wait_for_tstate_lock", "_invoke_excepthook",
    "_handle_thread_exception", "_target", "_args", "_kwargs",
    "_initialized", "_daemonic", "_dummy",
})


# ==========================================================================
# 喷浆循环判定
# ==========================================================================
class CycleTrackerTests(unittest.TestCase):
    """阈值全部走 `cfg`,不在这里写死数字 —— 注册表和配置才是唯一来源。"""

    def setUp(self) -> None:
        self.cfg = config.load_config()
        self.cfg["device"]["device_code"] = "GHP9015H"   # 测试造帧固定此编号：与现场配置隔离
        self.tracker = CycleTracker(self.cfg)
        self.t0 = int(now_epoch() * 1000)

    def _at(self, t: float) -> int:
        """第 t 秒（相对 t0）的毫秒时间戳。"""
        return self.t0 + int(t * 1000)

    def _feed(self, t: float, press: float, *, motor: float = 1.0,
              vol: float = 0.0, pile: str = "ZK105+207") -> dict | None:
        return self.tracker.update(
            {"cylinderPumpingPressure": press, "workStatus": motor, "circulatingPumpingVolume": vol},
            self._at(t), pile_id=pile)

    # ------------------------------------------------------------------
    def test_start_needs_motor_and_pressure(self):
        """电机没转就永远不算开喷 —— 否则停机时的残压会开出循环。"""
        for t in range(0, 60, 2):
            self.assertIsNone(self._feed(t, 9.0, motor=0.0))
        self.assertIsNone(self.tracker.active)

    def test_short_spray_does_not_open_a_cycle(self):
        """持续不到 `cycle_start_hold_s` 的冒一下不算一个循环。"""
        self._feed(0, 9.0)
        self._feed(2, 9.0)
        self.assertIsNone(self.tracker.active, "只持续 2 秒不该开循环")
        self.assertIsNone(self._feed(4, 0.0))       # 压力掉下去
        self.assertIsNone(self.tracker.active)

    def test_full_cycle_spans_from_counter_delta(self):
        """一轮完整喷浆：起止时刻、方量、峰值、均值。"""
        rows = [
            # t,  压力,  累计方量
            (0, 5.0, 10.000),
            (2, 5.0, 10.020),
            (4, 5.0, 10.040),      # 持续 4 秒 ≥ 3 秒 -> 起喷，start_volume=10.040
            (6, 12.0, 10.060),
            (8, 8.0, 10.080),
            (10, 12.0, 10.100),
            (12, 0.4, 10.120),     # 压力掉到阈值下，开始计停喷时长
            (14, 0.4, 10.120),
            (16, 0.4, 10.120),
            (18, 0.4, 10.120),
            (20, 0.4, 10.120),
            (22, 0.4, 10.120),     # 12 -> 22 满 10 秒 -> 结束
        ]
        done = [self._feed(t, p, vol=v) for t, p, v in rows]
        cycle = [c for c in done if c]
        self.assertEqual(len(cycle), 1, "整段只该产出 1 个循环")
        c = cycle[0]
        self.assertEqual(c["pile_id"], "ZK105+207")
        self.assertEqual(c["duration_s"], 18.0)
        # 方量取自累计计数器的差值，不是按时间估的
        self.assertAlmostEqual(c["volume_m3"], 0.080, places=3)
        self.assertAlmostEqual(c["max_pressure"], 12.0, places=2)
        self.assertAlmostEqual(c["avg_pressure"], 4.25, places=2)
        self.assertIsNone(self.tracker.active, "结束后不该还挂着进行中的循环")

    def test_end_waits_for_hold(self):
        """停喷也要持续够久:短暂掉压不算收工。"""
        for t, p, v in [(0, 5.0, 1.0), (2, 5.0, 1.1), (4, 5.0, 1.2)]:
            self._feed(t, p, vol=v)
        self.assertIsNotNone(self.tracker.active)
        # 掉压 8 秒还不该结束（阈值 10 秒）
        for t in range(6, 14, 2):
            self.assertIsNone(self._feed(t, 0.4, vol=1.2))
        self.assertIsNotNone(self.tracker.active)
        done = self._feed(16, 0.4, vol=1.2)          # 6 -> 16 满 10 秒
        self.assertIsNotNone(done)
        self.assertEqual(done["duration_s"], 12.0)   # 从起喷的 t=4 算起

    def test_pressure_dip_mid_spray_is_not_a_cycle_end(self):
        """喷浆中途瞬时不稳不能把一轮拆成两轮。"""
        for t, p in [(0, 5.0), (2, 5.0), (4, 5.0)]:
            self._feed(t, p, vol=1.0)
        self.assertIsNotNone(self.tracker.active)
        for t, p in [(6, 0.5), (8, 0.6)]:            # 连续 4 秒低压，不到 10 秒
            self.assertIsNone(self._feed(t, p, vol=1.1))
        self.assertIsNone(self._feed(10, 11.0, vol=1.2))   # 又起来了
        self.assertIsNotNone(self.tracker.active, "中途掉压不该结束循环")

    def test_snapshot_hides_accumulator(self):
        """给界面看的快照要摘掉内部累加器，只留均值。"""
        for t, p in [(0, 5.0), (2, 5.0), (4, 5.0), (6, 10.0)]:
            self._feed(t, p, vol=1.0)
        snap = self.tracker.snapshot()
        self.assertIsNotNone(snap)
        self.assertNotIn("sum_pressure", snap)
        self.assertIn("avg_pressure", snap)
        self.assertEqual(snap["pile_id"], "ZK105+207")

    def test_none_snapshot_before_any_spray(self):
        self.assertIsNone(self.tracker.snapshot())


# ==========================================================================
# 采集循环
# ==========================================================================
class _StubApp:
    """只装 `AcquisitionLoop` 真正会碰的那几样东西。

    用真的 `LiveState` / `CycleTracker`（它们是纯内存对象），
    只把 store / source / piles 换成可控的替身 —— 这样测的是采集循环本身，
    不是它周围的一堆线程。
    """

    def __init__(self, cfg: dict, store: LocalStore, source, *, ready: bool = True,
                 current: str = "ZK105+207"):
        self.cfg = cfg
        self.store = store
        self.source = source
        self.live = LiveState()
        self.tracker = CycleTracker(cfg)
        self.piles = SimpleNamespace(ready=ready, current=current)
        self.finished: list[dict] = []

    def on_cycle_end(self, cycle: dict) -> None:
        self.finished.append(cycle)


class AcquisitionLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="wsc_acq_")
        self.cfg = config.load_config()
        self.cfg["device"]["device_code"] = "GHP9015H"   # 测试造帧固定此编号：与现场配置隔离
        self.store = LocalStore(os.path.join(self.tmp, "local.db"),
                                os.path.join(self.tmp, "frames"), cfg=self.cfg)
        # 默认让台车"正在喷浆"：电机转、压力在正常区间。
        # `neutral_values` 把开关量给 0，不显式打开电机就永远开不出循环。
        self.src = FakeCan().set(workStatus=1.0, cylinderPumpingPressure=12.0)
        self.app = _StubApp(self.cfg, self.store, self.src)
        self.loop = AcquisitionLoop(self.app)
        self.interval = self.loop.interval

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tick(self, t: float = 0.0, late: float = 0.0) -> None:
        self.loop._tick(now_epoch() + t, late)


    def _count(self) -> int:
        return self.store.conn.execute(
            "SELECT COUNT(*) AS n FROM outbox").fetchone()["n"]

    # ------------------------------------------------------------------
    def test_no_pile_list_means_no_frame(self):
        """没有清单就不采 —— 别造一批注定被平台拒收的数据。"""
        self.app.piles.ready = False
        self._tick()
        self.assertEqual(self.src.reads, [], "清单都没有就不该去读数据源")
        self.assertEqual(self._count(), 0)
        live = self.app.live.snapshot()
        self.assertFalse(live["ready"])
        self.assertIn("桩号清单", live["message"])

    def test_no_pile_selected_means_no_frame(self):
        """清单到了但还没点选，同样不出数（空 pile_id 一定被拒收）。"""
        self.app.piles.current = ""
        self._tick()
        self.assertEqual(self.src.reads, [])
        self.assertEqual(self._count(), 0)
        self.assertIn("尚未选择作业桩号", self.app.live.snapshot()["message"])

    def test_source_error_skips_one_frame_only(self):
        self.src.fail_next("总线无响应")
        self._tick()
        live = self.app.live.snapshot()
        self.assertEqual(live["read_errors"], 1)
        self.assertIn("总线无响应", live["message"])
        self.assertEqual(self._count(), 0, "读不到就不该有记录")

        self._tick(2.0)                       # 下一帧恢复正常
        self.assertEqual(self._count(), 1)
        live = self.app.live.snapshot()
        self.assertEqual(live["read_errors"], 1, "错误计数不该被清零")
        self.assertEqual(live["message"], "", "恢复了就该把报错横幅收起来")

    def test_impossible_value_is_discarded_not_queued(self):
        """物理上不可能的数值就地丢弃，不排进队里变成永久拒收。"""
        self.src.set(workStatus=0.5)          # 开关量只能是 0 或 1
        self._tick()
        self.assertEqual(self._count(), 0)
        self.assertEqual(self.store.csv.rows_written, 0, "CSV 也不该留下这条")
        live = self.app.live.snapshot()
        self.assertEqual(live["read_errors"], 1)
        self.assertIn("越界", live["message"])

    def test_late_frame_marked_suspect(self):
        """迟到超过半个周期的帧要标可疑，不能装作准时。"""
        self._tick(0.0, late=self.interval + 0.1)
        row = self.store.conn.execute(
            "SELECT quality FROM outbox").fetchone()
        self.assertEqual(int(row["quality"]), int(Quality.SUSPECT))

        self._tick(2.0, late=0.0)
        rows = self.store.conn.execute(
            "SELECT quality FROM outbox ORDER BY ts_epoch").fetchall()
        self.assertEqual(int(rows[-1]["quality"]), int(Quality.OK))

    def test_frame_reaches_csv_and_outbox_with_all_params(self):
        self._tick()
        self.assertEqual(self.store.csv.rows_written, 1)

        rec = self.store.recent(limit=1)[0]
        self.assertEqual(rec["pile_id"], "ZK105+207")
        self.assertEqual(sorted(rec["values"]), sorted(PARAM_KEYS),
                         "16 个参数一个都不能少")
        for k in PARAM_KEYS:
            self.assertAlmostEqual(rec["values"][k], self.src.values[k], places=6)
        self.assertEqual(rec["record_id"],
                         ids.frame_record_id(self.cfg["device"]["device_code"],
                                             rec["ts_epoch"], rec["seq"]))

    def test_seq_continues_after_restart(self):
        """重启后帧序号接着历史最大值往后走，不与库里已有的撞号。"""
        self._tick()
        stored = self.store.conn.execute(
            "SELECT MAX(seq) AS s FROM outbox").fetchone()["s"]
        self.assertEqual(stored, 1)
        again = AcquisitionLoop(self.app)
        self.assertEqual(again._seq, stored + 1)
        self.assertEqual(again._seq, self.loop._seq,
                         "新老循环的下一帧序号必须一致，否则重启会撞号")

    def test_cycle_end_forwarded_to_app(self):
        """采集循环判定出的循环要交给 app（推进桩号、写审计都靠它）。"""
        for i in range(40):
            self.src.set(cylinderPumpingPressure=12.0 if i < 12 else 0.2,
                         circulatingPumpingVolume=1.0 + 0.05 * i)
            self._tick(t=i * self.interval)
        self.assertEqual(len(self.app.finished), 1)
        c = self.app.finished[0]
        self.assertGreater(c["volume_m3"], 0)
        self.assertEqual(c["pile_id"], "ZK105+207")

    def test_run_loop_produces_frames_on_a_grid(self):
        """真的把线程跑起来：节拍对齐固定网格，停得干净。"""
        loop = AcquisitionLoop(self.app)
        loop.interval = 0.05          # 演示是 2 秒；测试里压缩到 50ms
        loop.start()
        time.sleep(0.4)
        loop.stop()
        loop.join(timeout=3.0)
        self.assertFalse(loop.is_alive(), "stop() 之后线程要退得出来")
        n = self._count()
        self.assertGreaterEqual(n, 4, f"0.4 秒里该采到若干帧，实际 {n}")

        rows = self.store.conn.execute(
            "SELECT ts_epoch FROM outbox ORDER BY ts_epoch").fetchall()
        gaps = [b["ts_epoch"] - a["ts_epoch"]
                for a, b in zip(rows, rows[1:])]
        off = [g for g in gaps if abs(g - 50) > 5]
        self.assertEqual(off, [], f"采样间隔应稳定在 50ms 附近，异常间隔 {off}")


# ==========================================================================
# 线程生命周期
# ==========================================================================
class ThreadLifecycleTests(unittest.TestCase):
    """终端后台线程的构造与启停。

    **回归点**：`HeartbeatThread` 曾经把一个名叫 `_started` 的属性设成时间戳，
    与 `threading.Thread._started`（一个 Event）撞名，于是 `start()` 直接抛
    `AttributeError: 'float' object has no attribute 'is_set'` —— 终端起不来。
    这种"能构造、不能启动"的错误必须在测试里现形，而不是等演示时。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="wsc_thr_")
        self.cfg = config.load_config()
        self.cfg["device"]["device_code"] = "GHP9015H"   # 测试造帧固定此编号：与现场配置隔离
        self.store = LocalStore(os.path.join(self.tmp, "local.db"),
                                os.path.join(self.tmp, "frames"), cfg=self.cfg)
        self.client = PlatformClient(self.cfg)
        self.link = LinkMonitor(self.cfg)
        # 手动断网 = 所有线程都不发请求,测试零网络、零等待
        self.link.set_manual_offline(True)
        self.src = FakeCan()
        self.app = _StubApp(self.cfg, self.store, self.src)
        self.app.hb = None
        self.app.remember_pile = lambda **kw: ""

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _threads(self) -> list[threading.Thread]:
        hb = HeartbeatThread(self.store, self.client, self.link, self.cfg)
        self.app.hb = hb
        return [
            AcquisitionLoop(self.app),
            LinkProbe(self.link, self.client, self.cfg),
            hb,
            Uploader(self.store, self.client, self.link, self.cfg),
        ]

    def test_thread_internals_not_shadowed(self):
        for t in self._threads():
            self.assertIsInstance(
                t._started, threading.Event,
                f"{type(t).__name__} 覆盖了 Thread 的私有属性 _started")

    def test_no_thread_subclass_shadows_thread_internals(self):
        """任何 Thread 子类都不许用 `self.X = ...` 占 `Thread` 自己的名字。

        **这条必须是静态检查，不能靠 hasattr 现查。** CPython 会在版本之间
        增删这些私有属性 —— `_stop` 就是 3.13 删掉的。现查的话，新版 Python
        上根本查不出问题；而这份代码是要拿到别人机器上、用别人那个 Python 跑的。

        （这正是它抓到的第一只虫子：`self._stop = Event()` 在 3.14 上 347 个
        测试全绿，在 3.12 上崩掉 6 个线程用例。）
        """
        from terminal import app as app_mod
        from terminal import network as net_mod
        from terminal import uploader as up_mod

        bad: list[str] = []
        checked = 0
        for mod in (app_mod, net_mod, up_mod):
            for _name, cls in inspect.getmembers(mod, inspect.isclass):
                if cls is threading.Thread or not issubclass(cls, threading.Thread):
                    continue
                checked += 1
                tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Assign):
                        targets = node.targets
                    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                        targets = [node.target]
                    else:
                        continue
                    for tgt in targets:
                        if (isinstance(tgt, ast.Attribute)
                                and isinstance(tgt.value, ast.Name)
                                and tgt.value.id == "self"
                                and tgt.attr in _THREAD_RESERVED):
                            bad.append(f"{cls.__name__}.{tgt.attr}")

        self.assertGreaterEqual(checked, 5,
                                "没扫描到线程类 —— 检查范围写窄了，这条测试就没意义了")
        self.assertEqual(
            bad, [],
            "这些属性名会和 threading.Thread 自己的内部属性撞名，"
            "换一个 Python 版本就可能抛 'Event' object is not callable / "
            f"AttributeError：{bad}")

    def test_start_and_stop_round_trip(self):
        ts = self._threads()
        for t in ts:
            t.start()
        time.sleep(0.3)
        for t in ts:
            if hasattr(t, "stop"):
                t.stop()
        for t in ts:
            t.join(timeout=3.0)
            self.assertFalse(t.is_alive(),
                             f"{type(t).__name__} 停止后仍在运行")

    def test_probe_does_not_touch_network_when_manually_offline(self):
        """模拟断网时探测线程不发请求 —— 真断网也不会发出去。"""
        probe = LinkProbe(self.link, self.client, self.cfg)
        probe.interval = 0.05
        calls: list[int] = []
        self.client.health = lambda *a, **k: (calls.append(1), True)[1]
        probe.start()
        time.sleep(0.3)
        probe.stop()
        probe.join(timeout=3.0)
        self.assertEqual(calls, [], "手动断网期间不该调 health()")
        stats = self.link.snapshot()
        self.assertGreater(stats["fails"], 0, "状态还是要推进：界面得立刻变红")


# ==========================================================================
# 界面契约（没有浏览器，只能静态查）
# ==========================================================================
class WebAssetsTests(unittest.TestCase):
    """`terminal/web/` 的资源基本格式。

    页 ↔ JS 的 id 契约、导航指向、接口覆盖那些**不在这里** —— 它们要按页面
    分别检查（三张页面共用一个 app.js），在 `tests/test_terminal_web.py` 里，
    那个文件是起真服务从 HTTP 打的。这里只留不需要分页面的那几条。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.css = _read(os.path.join(WEB_DIR, "style.css"))

    def test_static_mime_types_are_browser_usable(self):
        """MIME 错了浏览器会拒绝执行 —— 页面会"能打开但什么都没发生"。"""
        cases = {"index.html": "text/html", "app.js": "javascript",
                 "style.css": "text/css"}
        for name, expect in cases.items():
            got = http_util.guess_mime(os.path.join(WEB_DIR, name))
            self.assertIn(expect, got or "", f"{name} 的 MIME 是 {got}")


# ==========================================================================
# 模拟器 / 种子数据 / 故障注入 三方一致
# ==========================================================================
class MachineAgreementTests(unittest.TestCase):
    """演示的"历史"和"实时"必须是同一台车，故障注入必须真的越过阈值。"""

    def test_seed_and_simulator_share_the_machine_constants(self):
        from server import seed
        self.assertEqual(seed.PUMP_RATE_M3_MIN, PUMP_RATE_M3_MIN)
        # 种子数据的越限数值直接取自故障预置，两边不可能写岔
        for day, minute, key, value, dur in seed.EXCURSIONS:
            self.assertEqual(value, FAULT_BY_KEY[key]["value"])
            self.assertGreater(dur, 0)

    def test_fault_presets_actually_exceed_the_alarm_range(self):
        """每个预置故障都必须真的越限，否则"注入"了平台上什么也不会发生。"""
        for f in FAULT_PRESETS:
            p = PARAM_BY_KEY[f["key"]]
            self.assertTrue(p.alarmable, f"{p.name} 不参与报警，注入它没意义")
            self.assertTrue(p.raise_cond(float(f["value"])),
                            f"{p.name} 预置值 {f['value']} 落在正常范围 "
                            f"{p.lo}~{p.hi} 内，注入后不会报警")
            self.assertGreater(float(f["hold_s"]), 0)

    def test_fault_presets_have_distinct_keys(self):
        keys = [f["key"] for f in FAULT_PRESETS]
        self.assertEqual(len(keys), len(set(keys)), "故障预置的 key 有重复")

    def test_simulator_frame_has_every_registered_key(self):
        """一帧都不能少键 —— 少一个键，服务端会按"缺失"整条拒收。"""
        src = build_source(config.load_config())
        try:
            v = src.read(now_utc())
        finally:
            src.close()
        self.assertEqual(set(v), set(PARAM_BY_KEY))
        self.assertIn(v["workStatus"], (0.0, 1.0))

    def test_simulator_frames_pass_registry_validation(self):
        """跑满一轮的每一个采样都得过注册表校验，一帧都不能越界。"""
        src = build_source(config.load_config())
        t0 = now_utc()
        bad = []
        try:
            for i in range(0, 400, 2):
                bad.extend(validate_values(src.read(_shift(t0, i))))
        finally:
            src.close()
        self.assertEqual(bad, [], f"模拟器产出了非法值：{bad[:5]}")

    def test_seed_pile_list_contains_the_documented_pile(self):
        """计划书里念的那个桩号必须真的在平台下发的清单里。"""
        from server import seed
        piles = seed.pile_list()
        self.assertIn(seed.DEFAULT_PILE, piles)
        self.assertIn("ZK105+207", piles)
        self.assertEqual(piles, sorted(piles), "清单要按里程有序")


# ==========================================================================
if __name__ == "__main__":  # pragma: no cover
    unittest.main()
