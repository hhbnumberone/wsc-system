# -*- coding: utf-8 -*-
"""配置加载与路径常量。

配置分三份，职责不重叠：

* `config/config.json`    —— 部署参数（端口、采样周期、上传间隔、保留天数）。
                             人工编辑，缺失的键用内置默认值补齐。
* `config/site.json`      —— 运行期写入：工点 ID、设备编号、当前桩号。
                             终端启动时读，切换工点时被平台接口改写。
* `config/thresholds.json`—— 报警阈值覆盖。**只允许覆盖阈值**，
                             参数名/单位/精度不可通过配置篡改（见 params.registry）。

所有路径都从本文件推导，任何模块都不允许自己拼相对路径 ——
因为工作目录会随启动方式变化（双击、命令行、从别的目录启动）。
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any

# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # wsc-system/
CONFIG_DIR = os.path.join(ROOT, "config")
DATA_DIR = os.path.join(ROOT, "data")
USB_DIR = os.path.join(ROOT, "usb")
LOG_DIR = os.path.join(DATA_DIR, "logs")

SERVER_DATA_DIR = os.path.join(DATA_DIR, "server")
TERMINAL_DATA_DIR = os.path.join(DATA_DIR, "terminal")
FRAMES_DIR = os.path.join(TERMINAL_DATA_DIR, "frames")

SERVER_DB = os.path.join(SERVER_DATA_DIR, "wsc.sqlite3")
LOCAL_DB = os.path.join(TERMINAL_DATA_DIR, "local.db")
BATCH_LOG = os.path.join(TERMINAL_DATA_DIR, "uploads.jsonl")

CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
SITE_FILE = os.path.join(CONFIG_DIR, "site.json")
THRESHOLDS_FILE = os.path.join(CONFIG_DIR, "thresholds.json")

SERVER_WEB = os.path.join(ROOT, "server", "web")
TERMINAL_WEB = os.path.join(ROOT, "terminal", "web")


# --------------------------------------------------------------------------
# 默认配置
# --------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    "project": {
        "name": "湿喷台车信息化系统",
        "company": "河南省耿力工程设备有限公司",
        "platform": "耿力大数据平台",
    },
    "site": {
        "site_id": "DQTYX-ZK",
        "site_name": "大丘田隧道右线",
        "project_name": "义龙庆高速",
        "tunnel": "大丘田隧道",
        "line": "右线",
    },
    #: 平台侧的**工点档案**。`site` 是"本机当前所在工点"，这里是"平台一共
    #: 认得哪些工点"—— 现场在终端上录工点 ID 时，平台拿这份名单核对，
    #: 名单外的 ID 一律拒收（与桩号同一个思路：只能引用，不能编造）。
    #:
    #: **两个工点的里程区间必须互不重叠** —— `piles.pile_id` 是全局主键，
    #: 重叠的里程会算出同一个桩号，后一个工点的桩号会被静默丢弃。
    #: 这也正是演示里第二个工点选在另一条隧道、而不是同隧道左线的原因：
    #: 真实隧道的左右线里程几乎重合，在全局主键下会撞车。
    "sites": [
        {
            "site_id": "DQTYX-ZK",
            "site_name": "大丘田隧道右线",
            "project_name": "义龙庆高速",
            "tunnel": "大丘田隧道",
            "line": "右线",
            "mileage_from": 104907,
            "mileage_to": 105707,
        },
        {
            "site_id": "SML-ZK",
            "site_name": "杉木岭隧道右线",
            "project_name": "义龙庆高速",
            "tunnel": "杉木岭隧道",
            "line": "右线",
            "mileage_from": 106000,
            "mileage_to": 106800,
        },
    ],
    "device": {
        "device_code": "GHP9015H",
        "device_name": "湿喷台车 GHP9015H",
        "model": "GHP9015H",
        # 操作手档案。日志数据的每条记录都快照这三个字段（见 params.LOG_FIELDS），
        # 现场换人时在终端"设置与诊断"里改，改完只影响之后的新记录。
        "operator": {
            "cname": "张建国",
            "cphone": "13803880000",
            "cidno": "410102199203154477",
        },
    },
    "can": {
        # sim = 内置虚拟台车（演示用）；real = python-can 真车适配器。
        # 切换数据源只改这一个字，采集与上传逻辑不受影响（见 can_bus/__init__.py）。
        "source": "sim",
        # 模拟器随机种子固定 —— 每次演示的波形一模一样，出问题能原样重现
        "seed": 20261005,
        # ---- 以下仅 real 模式使用 ----
        "bustype": "socketcan",   # Windows 常见 pcan / vector / slcan
        "channel": "can0",
        "bitrate": 500000,
        # 信号超过这么久没更新就不算数（不让最后一帧反复当新数据传上去）
        "max_age_s": 3.0,
        "recv_timeout_s": 0.2,
        # 信号映射表：按设备 DBC 填，见 can_bus/python_can_source.py 的说明。
        # 留空且 source=real 时会明确报错，而不是补零假装有数据。
        "signals": [],
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8700,
    },
    "terminal": {
        "host": "127.0.0.1",
        "port": 8701,
    },
    "acquisition": {
        # 采样周期。真车 CAN 报文约 100ms 一帧，终端按 2 秒聚合一次落盘。
        "sample_interval_s": 2.0,
        "heartbeat_interval_s": 5.0,
        # 喷浆循环判定（见 terminal/app.py）
        "cycle_start_pressure": 3.0,     # 泵送压力高于此值视为在喷浆
        "cycle_start_hold_s": 3.0,       # 且需持续这么久才算开始
        "cycle_end_pressure": 1.0,       # 低于此值视为停喷
        "cycle_end_hold_s": 10.0,        # 且需持续这么久才算结束
    },
    "upload": {
        # 演示用 2 秒，让曲线"实时"动起来。
        # 产品环境按计划书 4.2 节为 60 秒（1 分钟粒度）。改这一个数字即可切换。
        "interval_s": 2.0,
        "batch_max_records": 200,
        "timeout_s": 3.0,
        "max_attempts": 8,
        # 指数退避（秒），用完后固定取最后一个值
        "backoff_s": [2, 4, 8, 15, 30, 60],
        # INFLIGHT 超过这个时间没有回执，视为进程崩溃残留，回滚为 PENDING
        "inflight_timeout_s": 120.0,
        "connect_timeout_s": 2.0,
    },
    "network": {
        "probe_interval_s": 5.0,
        "fail_degraded": 2,   # 连续失败几次算弱网
        "fail_offline": 5,    # 连续失败几次算离线
        "manual_offline": False,
        "probe_path": "/api/health",
    },
    "retention": {
        "csv_days": 30,        # 本地 CSV 保留 30 天（对应截图第 11 项）
        "collect_interval_s": 3600.0,
        # 平台侧原始帧保留天数。演示种子数据跨度 30 天，留足余量。
        "frames_days": 400,
    },
    "alarms": {
        "enabled": True,
        "list_limit": 500,
    },
    "seed": {
        "days": 30,            # 演示种子数据的跨度
        "interval_s": 60,      # 1 分钟粒度
        "seed": 20261005,      # 固定随机种子，保证每次 --seed 得到同一批数据
        "cycles_per_day": 6,
    },
    "ui": {
        "poll_interval_ms": 1000,
        "chart_points": 60,
    },
}


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------
def _deep_merge(base: dict, override: dict) -> dict:
    """override 覆盖 base，嵌套字典递归合并。返回新对象，不改动入参。"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def read_json(path: str, default: Any = None) -> Any:
    """读 JSON。文件不存在或损坏时返回 default（并留下一个 .bad 备份）。"""
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, json.JSONDecodeError):
        try:
            os.replace(path, path + ".bad")
        except OSError:
            pass
        return default


def write_json(path: str, obj: Any, *, indent: int = 2) -> None:
    """原子写 JSON：先写临时文件再替换，避免断电留下半个文件。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(obj, fp, ensure_ascii=False, indent=indent)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, path)


def ensure_dirs() -> None:
    """建好所有运行期目录。启动时调用一次。"""
    for d in (CONFIG_DIR, DATA_DIR, USB_DIR, LOG_DIR, SERVER_DATA_DIR,
              TERMINAL_DATA_DIR, FRAMES_DIR, os.path.join(USB_DIR, "out")):
        os.makedirs(d, exist_ok=True)


def ensure_default_files() -> None:
    """首次运行时把 config.json / thresholds.json 落盘，方便用户直接改。"""
    ensure_dirs()
    if not os.path.isfile(CONFIG_FILE):
        write_json(CONFIG_FILE, DEFAULTS)
    if not os.path.isfile(THRESHOLDS_FILE):
        from .params import DEFAULT_THRESHOLDS
        write_json(THRESHOLDS_FILE, DEFAULT_THRESHOLDS)


def load_config(path: str | None = None) -> dict:
    """读配置并与默认值合并。缺失的键自动补齐，不会因为少写一行就崩。"""
    user = read_json(path or CONFIG_FILE, {}) or {}
    return _deep_merge(DEFAULTS, user)


def load_site() -> dict:
    """读工点信息。文件不存在时返回默认值（不改写，由调用方决定是否持久化）。"""
    return _deep_merge(DEFAULTS["site"], read_json(SITE_FILE, {}) or {})


def save_site(site: dict) -> None:
    """把工点信息落盘。

    调用者只有一处：车载终端现场录入工点/设备编号并**通过平台核对**之后
    （见 terminal/app.py 的 apply_identity）。平台进程从不写这个文件 ——
    它是**本机身份**，不是平台档案；平台档案在 `sites` 表里。
    """
    write_json(SITE_FILE, site)


def load_sites(cfg: dict) -> list[dict]:
    """平台认得的所有工点档案（`config.json` 的 `sites` 列表）。

    列表为空或缺失时退回"单个工点"—— 即 `site` 段加上默认里程区间，
    这样删掉配置里的 `sites` 也不会把平台弄成没有工点的状态。
    """
    raw = cfg.get("sites")
    if isinstance(raw, list) and raw:
        return [dict(s) for s in raw if isinstance(s, dict) and s.get("site_id")]
    fallback = dict(DEFAULTS["site"])
    fallback["mileage_from"] = DEFAULT_MILEAGE_FROM
    fallback["mileage_to"] = DEFAULT_MILEAGE_TO
    return [fallback]


#: `sites` 缺失时的兜底里程区间。与 server/seed.py 的同名常量一致 ——
#: 放在这里是为了让 `load_sites` 能在不 import server 的前提下兜底
#: （common 不能反向依赖 server）。
DEFAULT_MILEAGE_FROM = 104907
DEFAULT_MILEAGE_TO = 105707


def save_device(device: dict) -> None:
    """把设备信息写回 `config.json` 的 `device` 段。

    **只改 device 一段**，其余键原样保留：这是"就地改一个字段"，
    不是"用内存里的合并结果覆盖用户的配置文件"。后者会把所有默认值
    一起写进 config.json，用户下次想改默认值就改不动了。
    """
    raw = read_json(CONFIG_FILE, {}) or {}
    if not isinstance(raw, dict):  # pragma: no cover - 文件被改成数组等
        raw = {}
    raw["device"] = dict(device)
    write_json(CONFIG_FILE, raw)


def load_thresholds() -> dict:
    """读阈值覆盖。返回空 dict 表示全部用注册表默认值。"""
    return read_json(THRESHOLDS_FILE, {}) or {}


# --------------------------------------------------------------------------
# 便捷访问
# --------------------------------------------------------------------------
def url_base(section: str, cfg: dict | None = None) -> str:
    """拼出某个服务（server / terminal）的基地址。"""
    c = (cfg or load_config())[section]
    host = c.get("host", "127.0.0.1")
    if host in ("localhost", "::1", "0.0.0.0", ""):
        host = "127.0.0.1"      # 见 http_util 模块开头关于 IPv6 的说明
    return f"http://{host}:{int(c['port'])}"
