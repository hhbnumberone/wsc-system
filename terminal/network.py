# -*- coding: utf-8 -*-
"""链路状态机 + 模拟断网开关。

**单线程所有权**：状态只由探测线程推进，其他线程只能读快照。原因——
上传 worker 也会汇报成功/失败，如果它也去改状态，两个线程一交叉就会出现
"刚判定离线、又被一次成功改回在线"的抖动，界面上看起来像网络在闪。

模拟断网是给演示用的：现场不可能真的拔网线，但客户一定要看断网补传。
开关**不改链路状态**，只让探测和上传都失败 —— 这样"设备离线"这件事
是自然发生的，而不是演出来的假状态。
"""
from __future__ import annotations

import logging
import threading

from common.enums import LinkState
from common.timeutil import now_epoch, now_utc_iso

log = logging.getLogger("terminal.network")


class LinkMonitor:
    """链路状态机。

    迁移规则：

    * 探测成功 -> `ONLINE`，连续失败计数清零
    * 连续失败 >= `fail_degraded` -> `DEGRADED`（弱网，还能试）
    * 连续失败 >= `fail_offline`  -> `OFFLINE`（先停下来，别再空转）
    * 手动断网                -> `OFFLINE`（最高优先级，探测成功也不算数）

    只有 `ONLINE` 才认为实时上传有意义；`DEGRADED` 仍然尝试上传
    （弱网不等于不通，试一次的成本远低于丢数据的风险）。
    """

    def __init__(self, cfg: dict | None = None):
        from common import config
        self.cfg = cfg or config.load_config()
        net = self.cfg["network"]
        self.fail_degraded = int(net["fail_degraded"])
        self.fail_offline = int(net["fail_offline"])
        self._lock = threading.Lock()
        self._manual_offline = bool(net.get("manual_offline", False))
        self._state = LinkState.OFFLINE          # 启动时先当作离线，探测成功再转
        self._fails = 0
        self._oks = 0
        self._last_ok_epoch = 0.0
        self._last_probe_epoch = 0.0
        self._last_error = ""
        self._since = now_utc_iso()              # 进入当前状态的时间
        self._changes = 0                        # 状态迁移次数，供界面显示

    # ------------------------------------------------------------------
    # 推进（只允许探测线程调用）
    # ------------------------------------------------------------------
    def observe(self, ok: bool, *, error: str = "") -> LinkState:
        """汇报一次探测结果，返回新状态。"""
        with self._lock:
            self._last_probe_epoch = now_epoch()
            if ok:
                self._oks += 1
                self._last_ok_epoch = self._last_probe_epoch
                self._fails = 0
                self._last_error = ""
            else:
                self._fails += 1
                self._last_error = error or "探测失败"
            return self._recompute()

    def set_manual_offline(self, offline: bool) -> LinkState:
        """切换"模拟断网"。演示按钮走这里。"""
        with self._lock:
            self._manual_offline = bool(offline)
            if not offline:
                # 恢复时把失败计数清掉：否则要等 5 次成功才肯回 ONLINE，
                # 演示时看起来像"网线插回去了还是离线"。
                self._fails = 0
                self._last_error = ""
            log.warning("模拟断网：%s", "开启" if offline else "关闭")
            return self._recompute()

    def _recompute(self) -> LinkState:
        """按当前计数推出状态。调用方保证已持锁。"""
        if self._manual_offline:
            new = LinkState.OFFLINE
        elif self._fails >= self.fail_offline:
            new = LinkState.OFFLINE
        elif self._fails >= self.fail_degraded:
            new = LinkState.DEGRADED
        elif self._oks > 0:
            new = LinkState.ONLINE
        else:
            new = LinkState.OFFLINE
        if new is not self._state:
            log.warning("链路 %s -> %s（连续失败 %d 次%s）",
                        self._state.label, new.label, self._fails,
                        f"，{self._last_error}" if self._last_error else "")
            self._state = new
            self._since = now_utc_iso()
            self._changes += 1
        return self._state

    # ------------------------------------------------------------------
    # 读（任何线程）
    # ------------------------------------------------------------------
    @property
    def state(self) -> LinkState:
        with self._lock:
            return self._state

    @property
    def manual_offline(self) -> bool:
        with self._lock:
            return self._manual_offline

    def should_upload(self) -> bool:
        """现在值不值得发一次上传请求。

        `OFFLINE` 时不发 —— 断网还每 2 秒撞一次超时，只会让界面上的
        "最后失败原因"永远停在三秒前的连接超时上，看不出真正的问题。
        `DEGRADED` 要发：弱网不等于不通。
        """
        return self.state is not LinkState.OFFLINE

    def snapshot(self) -> dict:
        """给界面用的快照。**不加锁读私有属性** —— 一次拿全，避免读到半新半旧。"""
        with self._lock:
            return {
                "state": self._state.value,
                "state_label": self._state.label,
                "manual_offline": self._manual_offline,
                "fails": self._fails,
                "oks": self._oks,
                "changes": self._changes,
                "since": self._since,
                "last_ok_epoch": self._last_ok_epoch,
                "last_probe_epoch": self._last_probe_epoch,
                "last_error": self._last_error,
                "age_s": (round(now_epoch() - self._last_ok_epoch, 1)
                          if self._last_ok_epoch else None),
            }


class LinkProbe(threading.Thread):
    """每 `probe_interval_s` 探测一次，推进 `LinkMonitor`。

    单独一个线程而不是搭在上传循环里：上传可能因为退避睡很久，
    链路状态不能跟着一起睡 —— 断网了界面要马上变，不能等下次上传失败。
    """

    def __init__(self, monitor: LinkMonitor, client, cfg: dict | None = None):
        super().__init__(name="link-probe", daemon=True)
        from common import config
        self.cfg = cfg or config.load_config()
        self.monitor = monitor
        self.client = client
        self.interval = float(self.cfg["network"]["probe_interval_s"])
        # 叫 `_stop_evt` 不叫 `_stop`：后者会盖住 `threading.Thread._stop()` 这个
        # 解释器内部方法，在 Python ≤3.12 上线程收尾时抛
        # "'Event' object is not callable"。详见 terminal/app.py 里
        # AcquisitionLoop.__init__ 的说明。
        self._stop_evt = threading.Event()
        self._force = threading.Event()          # 手动触发一次（改配置/手动重连）

    def run(self) -> None:
        while not self._stop_evt.is_set():
            if self.monitor.manual_offline:
                # 模拟断网期间不发请求 —— 真的断网也不会发出去。
                # 但状态还是要推进，让界面和上传循环立刻知道。
                self.monitor.observe(False, error="模拟断网（手动）")
            else:
                try:
                    ok = self.client.health()
                    self.monitor.observe(ok, error="" if ok else "健康检查失败")
                except Exception as exc:  # pragma: no cover - health() 自己不抛
                    self.monitor.observe(False, error=f"{type(exc).__name__}: {exc}")
            self._force.wait(self.interval)
            self._force.clear()

    def poke(self) -> None:
        """催一次探测（点"重连"时用，不用等下一个周期）。"""
        self._force.set()

    def stop(self) -> None:
        self._stop_evt.set()
        self._force.set()
