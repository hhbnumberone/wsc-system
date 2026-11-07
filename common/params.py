# -*- coding: utf-8 -*-
"""监测参数注册表 —— 全系统唯一事实来源。

模拟器产帧、终端 CSV 表头与界面、服务端入库校验与报警判定、平台前端展示，
全部从本模块取定义。任何地方都不允许再硬编码参数名、单位或阈值。

字段清单来源：耿力路机信息化上传数据内容（2026-10 甲方提供字段表）。

* **实时数据**随帧采集：身份与时间类字段（eqpid/workAreaId/cname/cphone/
  cidno/collectTime/currentMileage）分别由 device_code、工点档案、设备档案、
  ts_device、pile_id 承载，不进数值注册表；数值注册表只收 8 个随帧变化的量。
* **日志数据**按喷浆循环生成（见 terminal/app.py 的 CycleTracker 与
  server/api.py 的派生逻辑），15 个字段的中文对照见 LOG_FIELDS。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from .enums import AlarmLevel

# 参数类型：模拟量 / 开关量 / 累计量
KIND_ANALOG = "analog"
KIND_STATE = "state"
KIND_COUNTER = "counter"


@dataclass(frozen=True)
class Param:
    """一个监测参数的定义。"""

    key: str                     # ASCII 键名，同时是 CSV 列名与数据库列名
    name: str                    # 中文名
    unit: str                    # 单位（开关量为空串）
    lo: float                    # 正常范围下限
    hi: float                    # 正常范围上限
    decimals: int = 2            # 显示精度
    kind: str = KIND_ANALOG
    alarm_level: AlarmLevel | None = None   # None 表示该参数不参与报警
    hyst: float = 0.05           # 回滞，取正常量程的百分比
    min_duration_s: float = 3.0  # 持续越限多久才触发报警
    clear_after_s: float = 10.0  # 恢复正常多久才清除报警
    #: 判定前提：(开关量 key, 阈值)。该开关量低于阈值时本参数**不参与判定**。
    #: 例：(“workStatus”, 0.5) 表示没在喷浆时本参数不参与判定。
    #: 没有它，停机时"泵送压力=0"会被判成低限报警，演示一开始就满屏告警。
    active_when: tuple[str, float] | None = None
    note: str = ""

    # ---- 派生属性 ----
    @property
    def span(self) -> float:
        return self.hi - self.lo

    @property
    def alarmable(self) -> bool:
        return self.alarm_level is not None

    def is_active(self, values: dict) -> bool:
        """本参数此刻是否有意义（对应子系统在运行）。

        没有 `active_when` 的参数恒为有意义 —— 比如累计用量，
        只要设备上电就该看。
        """
        if self.active_when is None:
            return True
        key, threshold = self.active_when
        try:
            return float(values.get(key, 0)) >= threshold
        except (TypeError, ValueError):
            return False

    # ---- 判定 ----
    def in_range(self, v: float) -> bool:
        """是否在正常范围内。"""
        return self.lo <= v <= self.hi

    def raise_cond(self, v: float) -> bool:
        """满足触发条件（越限）。"""
        return v < self.lo or v > self.hi

    def clear_cond(self, v: float) -> bool:
        """满足清除条件。比触发条件更严格，形成回滞带，避免边界抖动反复告警。"""
        band = self.span * self.hyst
        return (self.lo + band) <= v <= (self.hi - band)

    def format(self, v: float | int | None) -> str:
        """按精度格式化数值，用于界面与导出。"""
        if v is None:
            return ""
        if self.kind in (KIND_STATE,):
            return "喷浆中" if float(v) >= 0.5 else "停机"
        return f"{float(v):.{self.decimals}f}"


# --------------------------------------------------------------------------
# 参数清单（帧里随采随传的数值量）
# --------------------------------------------------------------------------
PARAMS: tuple[Param, ...] = (
    # —— 工作状态 ——
    # 喷浆中=1 / 停机=0（待机与移机都算停机）。它同时是泵送类参数的判定前提。
    Param("workStatus", "工作状态", "", 0.0, 1.0, 0, kind=KIND_STATE),

    # —— 泵送系统 ——
    # 下限取 3MPa 而不是 0：工作状态下压力掉到 3 以下意味着失压/空打，
    # 同样要报警。停机时靠 active_when 挡住，不会因为"没开机所以压力是 0"误报。
    Param("cylinderPumpingPressure", "泵送压力", "MPa", 3.0, 16.0, 2,
          alarm_level=AlarmLevel.CRITICAL, min_duration_s=3.0, clear_after_s=10.0,
          active_when=("workStatus", 0.5),
          note="压力过高是堵管前兆，过低是失压空打，两头都要看；停机时不判定"),
    Param("pumpingSpeed", "泵送速度", "m³/h", 3.0, 40.0, 1,
          alarm_level=AlarmLevel.WARNING, min_duration_s=5.0,
          active_when=("workStatus", 0.5),
          note="喷浆中泵送速度掉到下限以下说明堵管或吸空"),
    Param("circulatingPumpingVolume", "循环泵送方量", "m³", 0.0, 1e5, 2,
          kind=KIND_COUNTER, note="本循环已泵送的方量，循环结束后清零重新累计"),

    # —— 速凝剂系统 ——
    Param("acceleratorAddSpeed", "速凝剂添加速度", "L/h", 250.0, 900.0, 1,
          alarm_level=AlarmLevel.CRITICAL, min_duration_s=3.0,
          active_when=("workStatus", 0.5),
          note="掺量直接影响初凝时间与回弹率，中断会造成废喷；停机时不判定"),
    Param("acceleratorProportion", "速凝剂占比", "%", 0.0, 20.0, 1,
          alarm_level=AlarmLevel.WARNING, min_duration_s=5.0,
          active_when=("workStatus", 0.5),
          note="添加速度折算到泵送量的百分比，掺量异常会拖垮凝固质量"),
    Param("acceleratorOnceDosage", "速凝剂单次用量", "L", 0.0, 1e4, 1,
          kind=KIND_COUNTER, note="本循环已用的速凝剂量，循环结束后清零"),
    Param("acceleratorCumulativeDosage", "速凝剂累计用量", "L", 0.0, 1e7, 1,
          kind=KIND_COUNTER, note="设备投产以来的速凝剂累计用量"),
)

PARAM_BY_KEY: dict[str, Param] = {p.key: p for p in PARAMS}
PARAM_KEYS: tuple[str, ...] = tuple(p.key for p in PARAMS)
ALARM_PARAMS: tuple[Param, ...] = tuple(p for p in PARAMS if p.alarmable)

#: 日志数据（按喷浆循环生成）的 15 个字段对照 —— 耿力路机日志数据字段表。
#: eqpid 承载在 device_code 列上；collectTime/addTime 即循环的起止时间。
LOG_FIELDS: tuple[tuple[str, str], ...] = (
    ("eqpid", "设备ID"),
    ("workAreaId", "工区ID"),
    ("cname", "操作手姓名"),
    ("cphone", "电话号码"),
    ("cidno", "身份证号码"),
    ("collectTime", "开始时间"),
    ("addTime", "结束时间"),
    ("locationMetre", "定位里程"),
    ("endMetre", "结束里程"),
    ("actualSprayingVolume", "实喷方量"),
    ("acceleratorOnceDosage", "速凝剂单次用量"),
    ("acceleratorCumulativeDosage", "速凝剂累计用量"),
    ("circulatingPumpingVolume", "循环泵送方量"),
    ("sprayLocation", "喷射部位"),
    ("accumulatedWorkingHours", "循环累计工作时长"),
)
LOG_FIELD_NAMES: dict[str, str] = dict(LOG_FIELDS)

#: 只读：默认阈值快照，用于生成 config/thresholds.json
DEFAULT_THRESHOLDS: dict[str, dict] = {
    p.key: {
        "name": p.name,
        "lo": p.lo,
        "hi": p.hi,
        "hyst": p.hyst,
        "min_duration_s": p.min_duration_s,
        "clear_after_s": p.clear_after_s,
        "alarm_level": p.alarm_level.value if p.alarm_level else None,
    }
    for p in PARAMS
}


def registry(overrides: dict[str, dict] | None = None) -> dict[str, Param]:
    """返回参数注册表；overrides 可覆盖阈值（来自 config/thresholds.json）。

    overrides 的形状与 DEFAULT_THRESHOLDS 一致，但只允许覆盖阈值相关字段，
    参数名/单位/精度不可通过配置篡改。
    """
    if not overrides:
        return dict(PARAM_BY_KEY)

    allowed = {"lo", "hi", "hyst", "min_duration_s", "clear_after_s"}
    out = dict(PARAM_BY_KEY)
    for key, ov in overrides.items():
        base = out.get(key)
        if base is None:
            continue
        patch = {k: v for k, v in ov.items() if k in allowed}
        if "alarm_level" in ov and ov["alarm_level"]:
            patch["alarm_level"] = AlarmLevel(ov["alarm_level"])
        if patch:
            out[key] = replace(base, **patch)
    return out


def check_value(p: Param, v: float) -> str | None:
    """库内校验单个数值，返回 None 表示通过，否则返回中文错误说明。

    注意：这里只做**物理量程**校验（明显不可能的数值），正常/报警范围属于
    业务判定，由 server/alarms.py 负责，不在这里拒收。区别很重要——
    越限数据必须入库并触发报警，而不是被丢弃。
    """
    if p.kind == KIND_STATE:
        if v not in (0.0, 1.0):
            return f"{p.name} 开关量只能为 0 或 1，实际 {v}"
        return None
    if p.kind == KIND_COUNTER:
        if v < 0:
            return f"{p.name} 累计量不能为负，实际 {v}"
        return None
    # 模拟量：允许超出正常范围（用于报警），但拒绝物理上不可能的数值
    if abs(v) > 1e7:
        return f"{p.name} 数值超出物理量程，实际 {v}"
    return None


def validate_values(values: dict[str, float]) -> list[tuple[str, str]]:
    """校验一整帧。返回 [(参数 key, 中文错误说明)]，空列表表示全部通过。"""
    errors: list[tuple[str, str]] = []
    for key in PARAM_KEYS:
        if key not in values:
            errors.append((key, f"{PARAM_BY_KEY[key].name} 缺失"))
            continue
        try:
            v = float(values[key])
        except (TypeError, ValueError):
            errors.append((key, f"{PARAM_BY_KEY[key].name} 不是数值：{values[key]!r}"))
            continue
        msg = check_value(PARAM_BY_KEY[key], v)
        if msg:
            errors.append((key, msg))
    return errors


def groups() -> dict[str, list[Param]]:
    """按业务分组返回参数，供终端实时页分栏展示。"""
    return {
        "泵送系统": [PARAM_BY_KEY[k] for k in
                     ("cylinderPumpingPressure", "pumpingSpeed",
                      "circulatingPumpingVolume")],
        "速凝剂系统": [PARAM_BY_KEY[k] for k in
                       ("acceleratorAddSpeed", "acceleratorProportion",
                        "acceleratorOnceDosage", "acceleratorCumulativeDosage")],
        "工作状态": [PARAM_BY_KEY["workStatus"]],
    }


def as_dicts(params: Iterable[Param]) -> list[dict]:
    """序列化成前端可直接消费的结构。"""
    return [
        {
            "key": p.key, "name": p.name, "unit": p.unit,
            "lo": p.lo, "hi": p.hi, "decimals": p.decimals,
            "kind": p.kind,
            "alarmable": p.alarmable,
            "alarm_level": p.alarm_level.value if p.alarm_level else None,
            "active_when": list(p.active_when) if p.active_when else None,
            "note": p.note,
        }
        for p in params
    ]
