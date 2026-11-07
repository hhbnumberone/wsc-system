# -*- coding: utf-8 -*-
"""内置虚拟湿喷台车。

**这不是随机数发生器，是一台按工况走的机器。** 上一秒的读数决定下一秒的读数：
工作状态一进喷浆，泵送压力才爬得起来，速凝剂才开始往累计用量里走。客户看演示
时第一眼判断真假，看的就是这些关系对不对 —— 压力跟着工作状态走、用量跟着
喷浆时长走，比任何界面都更能说明"这是真数据"。

**波形形状与 `server/seed.py` 的历史数据一致**：泵送压力都在 11.0±3.2 那条
正弦带上，速凝剂添加速度都在 620±110。历史页和实时页并排放着看不出来是
两套代码生成的 —— 这是刻意的，它们描述的本来就是同一台车。共用常数放在
`common/machine.py`。

**时间尺度按演示压缩过。** 真车一个循环 25~45 分钟（见 seed.py），2 秒采样下
要盯着屏幕等半小时才看到一次状态切换。这里把一轮压到 3 分钟，波形形状和参数
之间的相互关系一个都没改。要调回去改 `SCHEDULE` 一个常量即可。
"""
from __future__ import annotations

import logging
import math
import random
import threading
from datetime import datetime

from common.machine import (FAULT_BY_KEY, PUMP_PRESSURE_WORK_HI,
                            PUMP_PRESSURE_WORK_LO, PUMP_RATE_M3_MIN)
from common.params import PARAM_BY_KEY, PARAM_KEYS

from .base import CanSource

log = logging.getLogger("can_bus.simulator")

# --------------------------------------------------------------------------
# 作业节拍
# --------------------------------------------------------------------------
PHASE_SPRAY = "spray"     # 喷浆：泵送与速凝剂系统全开
PHASE_IDLE = "idle"       # 待机：停泵，收整，等待下一轮
PHASE_MOVE = "move"       # 移机：台车挪到下一格

#: 一轮作业的相位表：(相位, 持续秒数)。
#: **顺序就是物理顺序** —— 喷完这一格 → 停泵收整 → 挪到下一格 → 接着喷。
SCHEDULE: tuple[tuple[str, float], ...] = (
    (PHASE_SPRAY, 120.0),
    (PHASE_IDLE, 24.0),
    (PHASE_MOVE, 36.0),
)

PHASE_LABEL = {PHASE_SPRAY: "喷浆中", PHASE_IDLE: "待机", PHASE_MOVE: "移机中"}

#: 相位表总时长，供界面算"这一轮还剩多久"
CYCLE_S = sum(d for _, d in SCHEDULE)

#: 泵送速度的工作点（m³/h）。额定速率 0.18 m³/min 折算即 10.8 m³/h。
PUMP_SPEED_M3H = PUMP_RATE_M3_MIN * 60.0

#: 速凝剂添加速度的工作点（L/h），与 seed.py 共用。
ACCEL_SPEED_LH = 620.0


class WetMixTrolley(CanSource):
    """虚拟湿喷台车。

    用法：

        src = WetMixTrolley(seed=20261005)
        values = src.read(now_utc())                   # 8 个参数
        src.inject("cylinderPumpingPressure", 18.6, hold_s=40)   # 注入堵管

    `read(ts)` 只依赖 `ts` 和内部状态，同一条时间序列喂进来必然得到同一串值。
    """

    name = "内置模拟器"

    def __init__(self, *, seed: int = 20261005, device_code: str = "GHP9015H",
                 start_ts: datetime | None = None):
        self.device_code = device_code
        self.seed = seed
        self.detail = f"虚拟湿喷台车 {device_code}（随机种子 {seed}）"
        self.rng = random.Random(seed)
        #: 故障列表由**界面线程改、采集线程读**，必须加锁。
        #: 不锁的后果很具体：演示的人连点两下"注入故障"，两处各读一次
        #: 旧列表再写回，先注入的那个就凭空消失了。
        self._flock = threading.Lock()
        self.reset(start_ts)

    # ==================================================================
    # 状态
    # ==================================================================
    def reset(self, start_ts: datetime | None = None) -> None:
        """回到刚开工的状态。`--reset` 与"重新演示"按钮走这里。"""
        self.rng = random.Random(self.seed)
        self._t0: datetime | None = start_ts
        self._prev_t = 0.0
        self.t = 0.0                 # 从 t0 起算的运行秒数
        self.phase_idx = 0
        self.phase_t = 0.0           # 当前相位已持续秒数
        self.phase = SCHEDULE[0][0]
        self.cycles_done = 0
        self.volume = 0.0            # 累计泵送方量，m³
        self.accel_total = 0.0       # 速凝剂累计用量，L
        self.cycle_vol = 0.0         # 本循环泵送方量，m³
        self.cycle_accel = 0.0       # 本循环速凝剂用量，L
        self._faults: list[dict] = []
        self._last_values: dict[str, float] = {}

    # ==================================================================
    # 读
    # ==================================================================
    def read(self, ts: datetime) -> dict[str, float]:
        """取 `ts` 时刻的一帧。"""
        if self._t0 is None:
            self._t0 = ts
        t = (ts - self._t0).total_seconds()
        if t < 0:
            t = self.t                       # 系统时钟被往回调，当作没动
        step = max(0.0, t - self._prev_t)    # 距上一次采样的真实间隔
        self._prev_t = t
        self.t = t

        self._advance_phase(step)
        frac = min(1.0, self.phase_t / max(1e-6, self._phase_duration()))
        if self.phase == PHASE_SPRAY:
            vals = self._spray(step, frac)
        elif self.phase == PHASE_MOVE:
            vals = self._move()
        else:
            vals = self._idle()

        self._apply_faults(vals)
        self._last_values = dict(vals)
        # 少一个键就让上下游炸出来 —— 半帧数据比没有数据更危险。
        # 这一步同时也是模拟器自己的体检：波形写错了会在这里被拦住。
        self.check_frame(vals)
        return {k: round(float(vals[k]), 6) for k in PARAM_KEYS}

    # ---- 相位推进 ----
    def _phase_duration(self) -> float:
        return SCHEDULE[self.phase_idx][1]

    def _advance_phase(self, step: float) -> None:
        """按真实经过的时间推进相位。

        用**真实间隔**而不是固定步长：采集循环偶尔会晚一拍（机器卡顿、
        本地界面在渲染），相位不能跟着慢下来，否则一轮作业会比配置的
        时长越跑越长，演示时"说好 3 分钟一轮"变成 5 分钟。
        """
        if step <= 0:
            return
        self.phase_t += step
        # while 而不是 if：机器休眠过（笔记本合盖）时可能一口气跨过好几个相位
        while self.phase_t >= self._phase_duration():
            self.phase_t -= self._phase_duration()
            if self.phase == PHASE_SPRAY:
                self.cycles_done += 1     # 喷完一格，进尺推进
                # 循环结束：本循环的两个计数器清零，等下一轮喷浆重新累计
                self.cycle_vol = 0.0
                self.cycle_accel = 0.0
            self.phase_idx = (self.phase_idx + 1) % len(SCHEDULE)
            self.phase = SCHEDULE[self.phase_idx][0]
            log.debug("相位切换 -> %s（已喷 %d 轮）", self.phase, self.cycles_done)

    # ---- 三种工况 ----
    def _noise(self, sigma: float = 1.0) -> float:
        return self.rng.gauss(0.0, sigma)

    @staticmethod
    def _clip(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    def _spray(self, step: float, frac: float) -> dict[str, float]:
        """喷浆中。波形与 seed.py 的循环内分支同形。"""
        speed = self._clip(
            PUMP_SPEED_M3H * (1.0 + 0.06 * math.sin(frac * math.pi)) + 0.25 * self._noise(),
            4.0, 38.0)
        add_speed = self._clip(
            ACCEL_SPEED_LH + 110.0 * math.sin(frac * 2 * math.pi) + 35.0 * self._noise(),
            290.0, 810.0)
        # 占比 = 添加速度(L/h) ÷ 泵送量(m³/h→L/h) × 100
        proportion = self._clip(add_speed / max(1.0, speed * 1000.0) * 100.0, 0.0, 19.5)

        step_vol = PUMP_RATE_M3_MIN * step / 60.0
        step_accel = add_speed * step / 3600.0
        self.volume += step_vol
        self.accel_total += step_accel
        self.cycle_vol += step_vol
        self.cycle_accel += step_accel

        return {
            "workStatus": 1.0,
            "cylinderPumpingPressure": self._clip(
                11.0 + 3.2 * math.sin(frac * math.pi) + 0.55 * self._noise(),
                PUMP_PRESSURE_WORK_LO, PUMP_PRESSURE_WORK_HI),
            "pumpingSpeed": speed,
            "circulatingPumpingVolume": round(self.cycle_vol, 3),
            "acceleratorAddSpeed": add_speed,
            "acceleratorProportion": proportion,
            "acceleratorOnceDosage": round(self.cycle_accel, 2),
            "acceleratorCumulativeDosage": round(self.accel_total, 2),
        }

    def _idle(self) -> dict[str, float]:
        """待机：停泵、停速凝剂泵。两个循环计数器保持清零后的 0。"""
        return {
            "workStatus": 0.0,
            "cylinderPumpingPressure": max(0.0, 0.12 + 0.06 * self._noise()),
            "pumpingSpeed": 0.0,
            "circulatingPumpingVolume": 0.0,
            "acceleratorAddSpeed": 0.0,
            "acceleratorProportion": 0.0,
            "acceleratorOnceDosage": 0.0,
            "acceleratorCumulativeDosage": round(self.accel_total, 2),
        }

    def _move(self) -> dict[str, float]:
        """移机：台车挪向下一个桩号。

        **泵送主电机是停的**，这正是 `cylinderPumpingPressure.active_when`
        存在的理由：停机时"泵送压力为 0"是正常工况，不该报警。
        """
        return {
            "workStatus": 0.0,
            "cylinderPumpingPressure": max(0.0, 0.6 + 0.15 * self._noise()),
            "pumpingSpeed": 0.0,
            "circulatingPumpingVolume": 0.0,
            "acceleratorAddSpeed": 0.0,
            "acceleratorProportion": 0.0,
            "acceleratorOnceDosage": 0.0,
            "acceleratorCumulativeDosage": round(self.accel_total, 2),
        }

    # ==================================================================
    # 故障注入
    # ==================================================================
    def _apply_faults(self, vals: dict) -> None:
        """把注入的故障混进这一帧。

        **混合而不是覆盖**：直接赋值会让曲线在注入瞬间变成一个方波台阶，
        看起来像程序出错；按比例逼近则是一条"压力慢慢爬上去"的坡，
        和真的堵管过程一样。坡分三段：ramp 秒逼近 → 保持 hold 秒 →
        ramp 秒退回自然值。
        """
        if not self._faults:
            return
        with self._flock:
            alive: list[dict] = []
            for f in self._faults:
                el = self.t - f["start"]
                ramp, hold = f["ramp_s"], f["hold_s"]
                if el < ramp:
                    factor = el / ramp
                elif el < ramp + hold:
                    factor = 1.0
                elif el < ramp * 2 + hold:
                    factor = 1.0 - (el - ramp - hold) / ramp
                else:
                    log.info("故障已结束：%s %s 回到 %.3f",
                             PARAM_BY_KEY[f["key"]].name, f["value"],
                             vals.get(f["key"], 0.0))
                    continue
                # 故障值本身也要抖：一条笔平的死线一眼就能看出是手工写进去的。
                # 幅度取 0.4%，与 seed.py 给历史越限事件加的抖动一致。
                target = f["value"] * (1.0 + 0.004 * self._noise())
                natural = float(vals.get(f["key"], 0.0))
                vals[f["key"]] = natural + (target - natural) * factor
                alive.append(f)
            self._faults = alive
            self._apply_prereq(vals)

    def _apply_prereq(self, vals: dict) -> None:
        """把故障参数的判定前提一起打开。

        否则会出现这种情况：台车正在待机，演示的人点了"速凝剂添加速度掉底"，
        平台上**什么都不会发生** —— 因为该参数的 `active_when` 规定停机了
        就不判定。看演示的人只会得出一个结论：这个程序坏了。

        语义上也说得通：能出现"速凝剂异常"这个故障，前提本来就是泵在转。
        """
        for f in self._faults:
            p = PARAM_BY_KEY.get(f["key"])
            if p is None or not p.active_when:
                continue
            pre, threshold = p.active_when
            if float(vals.get(pre, 0.0)) < threshold:
                vals[pre] = 1.0

    def inject(self, key: str, value: float, *, hold_s: float = 40.0,
               ramp_s: float = 8.0) -> dict:
        """注入一个越限值，返回这次注入的说明。

        同一个参数重复注入是**替换**而不是叠加 —— 客户连点两下按钮，
        不该出现 36 MPa 这种物理上不可能的压力。
        """
        p = PARAM_BY_KEY.get(key)
        if p is None:
            raise KeyError(f"{key} 不是注册表里的参数")
        log.warning("注入故障：%s -> %s%s（保持 %.0f 秒）", p.name, value, p.unit, hold_s)
        rec = {"key": key, "name": p.name, "unit": p.unit, "value": float(value),
               "hold_s": float(hold_s), "ramp_s": float(ramp_s),
               "start": self.t, "at": self.t}
        with self._flock:
            self._faults = [f for f in self._faults if f["key"] != key]
            self._faults.append(rec)
        return {k: v for k, v in rec.items() if k not in ("start", "at")}

    def inject_preset(self, key: str) -> dict:
        """按 `common/machine.FAULT_PRESETS` 注入。故障面板的四个按钮走这里。"""
        preset = FAULT_BY_KEY.get(key)
        if preset is None:
            raise KeyError(f"{key} 不在故障预置表里")
        out = self.inject(key, preset["value"], hold_s=preset["hold_s"])
        out.update({"label": preset["label"], "detail": preset["detail"],
                    "level": preset["level"]})
        return out

    def clear_faults(self) -> int:
        with self._flock:
            n = len(self._faults)
            self._faults = []
        if n:
            log.info("清除 %d 个注入的故障", n)
        return n

    def faults(self) -> list[dict]:
        """当前还在生效的注入，供界面显示剩余时间。"""
        with self._flock:
            items = list(self._faults)
        out = []
        for f in items:
            el = self.t - f["start"]
            total = f["ramp_s"] * 2 + f["hold_s"]
            out.append({**{k: v for k, v in f.items() if k not in ("start", "at")},
                        "elapsed_s": round(el, 1),
                        "left_s": round(max(0.0, total - el), 1)})
        return out

    # ==================================================================
    # 状态快照
    # ==================================================================
    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap.update({
            "phase": self.phase,
            "phase_label": PHASE_LABEL.get(self.phase, self.phase),
            "phase_left_s": round(max(0.0, self._phase_duration() - self.phase_t), 1),
            "cycles_done": self.cycles_done,
            "volume_m3": round(self.volume, 2),
            "accel_total_l": round(self.accel_total, 1),
            "seed": self.seed,
            "faults": self.faults(),
        })
        return snap
