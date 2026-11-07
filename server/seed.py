# -*- coding: utf-8 -*-
"""演示种子数据：30 天、1 分钟粒度的历史帧。

**为什么要造历史数据**

平台的历史页、趋势图、循环统计、报警统计都需要时间跨度才有内容。
如果只靠终端实时跑，一小时才 1800 条，30 天的曲线是画不出来的。

**为什么是确定性的**

`random.Random(固定种子)` + 以"日期偏移 + 当天第几分钟"为索引生成 record_id。
所以无论跑几次 `--seed`，得到的是**同一批数据**，`record_id` 不变，
主键冲突会被 `INSERT OR IGNORE` 挡掉 —— 种子过程天然幂等，可以放心重跑。

**波形与真实作业的对应**

一天 6~9 个喷浆循环，分布在 08:00–18:00 班次内。
循环内：电机/风机/速凝剂泵全开，泵送压力 9~15MPa，速凝剂流量 500~750L/h，
累计方量按 0.18 m³/min 递增（一个 35 分钟的循环约 6 m³，对应 4m 进尺、20cm 厚、
25m² 断面的喷射量，量级对得上）。
循环外：全部归零，液压油温缓慢回落 —— 这正是 `params.active_when` 存在的理由，
否则"停机时风路压力为 0"会被判成低限报警刷满整个夜间。
"""
from __future__ import annotations

import logging
import math
import random
from datetime import timedelta

from common import config, csv_schema, ids
from common.enums import Quality, Source
from common.machine import (FAULT_BY_KEY, PILE_STEP_M, PUMP_RATE_M3_MIN,
                            REBOUND_RATE, SPRAY_LOCATIONS)
from common.params import PARAM_KEYS
from common.protocol import Batch
from common.timeutil import CST, now_utc

from . import db

log = logging.getLogger("server.seed")

#: 桩号里程区间（米）与步长。大丘田隧道右线。
#: 起点选 104907 而不是整数：105207 - 104907 = 300 能被 4 整除，
#: 于是计划书和现场截图里出现的 ZK105+207 会**真实存在于下发的桩号清单中** ——
#: 演示时客户照着文档念桩号，界面上就能点出来，不用解释"这个是我们编的"。
MILEAGE_FROM = config.DEFAULT_MILEAGE_FROM
MILEAGE_TO = config.DEFAULT_MILEAGE_TO
MILEAGE_STEP = 4          # 一个喷射循环推进 4m，与台车一次定位覆盖的范围一致

#: 演示默认停在计划书里的那个桩号
DEFAULT_PILE = "ZK105+207"

#: 故意注入的越限事件：(距今天数, 当天分钟, 参数 key, 持续分钟)
#: 这些是给报警页和演示"注入故障"用的真实历史案例，不是随机噪声。
#: **数值不写在这里** —— 从 `common/machine.FAULT_BY_KEY` 取，
#: 与"注入故障"面板共用同一批数值，历史与现场才说得上是同一回事。
EXCURSIONS_AT = (
    (8, 10 * 60 + 12, "cylinderPumpingPressure", 6),   # 堵管前兆，压力飙升
    (5, 14 * 60 + 30, "acceleratorAddSpeed", 8),       # 速凝剂泵抽空，添加速度掉底
    (3, 9 * 60 + 40, "acceleratorProportion", 12),     # 掺量占比异常偏高
    (1, 16 * 60 + 5, "pumpingSpeed", 5),               # 泵送速度骤降
)

#: 展开成 (距今天数, 当天分钟, 参数, 数值, 持续分钟)，供下面的生成器使用。
EXCURSIONS = tuple(
    (day, minute, key, FAULT_BY_KEY[key]["value"], dur_min)
    for day, minute, key, dur_min in EXCURSIONS_AT
)

# GPS 基准点、泵送速率、油温环境温度全部来自 `common/machine.py` —— 与实时
# 模拟器共用一份，否则历史曲线和实时定位会落在两个不同的地方，演示时一眼
# 就看出是假的。


# ==========================================================================
# 基础数据
# ==========================================================================
def pile_id_for(mileage_m: int) -> str:
    """里程（米）转桩号，例如 105207 -> ZK105+207。"""
    return f"ZK{mileage_m // 1000}+{mileage_m % 1000:03d}"


def ensure_sites(conn, cfg: dict | None = None) -> int:
    """把配置里的工点档案写进平台库。

    **幂等**，每次启动都跑（不像桩号那样只在空库时生成）：工点名、线别这类
    信息改了配置重启就该生效，不能因为"库里已经有工点了"而永远不更新。

    这份名单就是现场录入工点 ID 时的核对依据 —— 名单外的工点，
    终端录不进去（见 server/api.py 的 `identity_check`）。
    """
    cfg = cfg or config.load_config()
    sites = config.load_sites(cfg)
    now = now_utc().isoformat(timespec="milliseconds")
    n = 0
    for s in sites:
        # upsert 而不是 INSERT OR IGNORE：配置是工点档案的权威来源，
        # 改名要能生效。桩号则相反（见 ensure_piles），只增不删。
        conn.execute(
            "INSERT INTO sites(site_id, site_name, project_name, tunnel, line,"
            " active, created_at) VALUES(?,?,?,?,?,1,?)"
            " ON CONFLICT(site_id) DO UPDATE SET site_name=excluded.site_name,"
            " project_name=excluded.project_name, tunnel=excluded.tunnel,"
            " line=excluded.line, active=1",
            (s["site_id"], s.get("site_name", ""), s.get("project_name", ""),
             s.get("tunnel", ""), s.get("line", ""), now))
        n += 1
    log.info("工点档案：%d 个（%s）", n,
             "、".join(f"{s['site_id']} {s.get('site_name', '')}" for s in sites))
    return n


def ensure_piles(conn, cfg: dict | None = None, *, replace: bool = False) -> int:
    """生成**每个工点**的桩号清单。清单由平台下发，终端只能从中选（桩号防伪的源头）。

    只增不删：改了配置里的里程区间，新里程的桩号会被补上，旧的不动 ——
    撤桩号会让已经采到的数据变成"桩号未下发"而被拒收，那是数据事故，
    不该由改一行配置引起。要重来请走 `--reset`。
    """
    cfg = cfg or config.load_config()
    sites = config.load_sites(cfg)
    issued = now_utc().isoformat(timespec="milliseconds")
    total = 0

    for site in sites:
        site_id = str(site["site_id"])
        lo = int(site.get("mileage_from", MILEAGE_FROM))
        hi = int(site.get("mileage_to", MILEAGE_TO))
        line = site.get("line", "")
        if replace:
            conn.execute("DELETE FROM piles WHERE site_id=?", (site_id,))

        rows = []
        for m in range(lo, hi + 1, MILEAGE_STEP):
            pid = pile_id_for(m)
            rows.append((pid, site_id, line, float(m), f"{line} {pid}", issued, 1))
        db.insert_many(conn, "piles",
                       ("pile_id", "site_id", "line", "mileage_m", "label",
                        "issued_at", "active"), rows)
        total += len(rows)
        log.info("桩号清单：%s 共 %d 个（%s ~ %s）", site_id, len(rows),
                 pile_id_for(lo), pile_id_for(hi))
    return total


def ensure_device(conn, cfg: dict | None = None) -> None:
    """登记设备。终端连上来之前，平台首页也得有东西显示。"""
    cfg = cfg or config.load_config()
    dev = cfg["device"]
    site = config.load_site()
    conn.execute(
        "INSERT OR IGNORE INTO devices(device_code, device_name, model, site_id,"
        " site_name, link_state, note) VALUES(?,?,?,?,?,?,?)",
        (dev["device_code"], dev["device_name"], dev.get("model", ""),
         site.get("site_id", ""), site.get("site_name", ""), "offline",
         "演示设备，数据由内置 CAN 模拟器产生"))


def pile_list(cfg: dict | None = None) -> list[str]:
    """桩号清单（纯计算，不查库）。终端与种子数据共用同一份定义。"""
    return [pile_id_for(m) for m in
            range(MILEAGE_FROM, MILEAGE_TO + 1, MILEAGE_STEP)]


# ==========================================================================
# 波形
# ==========================================================================
class _Gen:
    """一台虚拟湿喷台车的状态机。逐分钟产出一帧。"""

    def __init__(self, rng: random.Random, device_code: str, piles: list[str]):
        self.rng = rng
        self.device = device_code
        self.piles = piles
        # 从计划书里出现的那个桩号起步，演示时与文档对得上
        try:
            self.pile_idx = piles.index(DEFAULT_PILE)
        except ValueError:  # pragma: no cover - 清单被改过才会走到
            self.pile_idx = 0
        self.seq = 0
        self.volume = 0.0            # 累计泵送方量，跨天连续
        self.accel_total = 0.0       # 速凝剂累计用量，L，跨天连续
        self.cycle_vol = 0.0         # 本循环泵送方量
        self.cycle_accel = 0.0       # 本循环速凝剂用量

    # ---- 单帧 ----
    def step(self, day_start_local, minute_of_day: int, cycle: tuple[int, int] | None,
             override: tuple[str, float] | None) -> dict:
        rng = self.rng
        n = rng.gauss
        in_cycle = cycle is not None

        if in_cycle:
            start, dur = cycle
            phase = (minute_of_day - start) / max(1, dur)

            speed = clip(PUMP_RATE_M3_MIN * 60.0
                         * (1.0 + 0.06 * math.sin(phase * math.pi)) + n(0, 0.25),
                         4.0, 38.0)
            add_speed = clip(620.0 + 110.0 * math.sin(phase * 2 * math.pi) + n(0, 35.0),
                             290.0, 810.0)
            proportion = clip(add_speed / max(1.0, speed * 1000.0) * 100.0, 0.0, 19.5)

            step_vol = max(0.02, PUMP_RATE_M3_MIN + n(0, 0.035))
            self.volume += step_vol
            self.cycle_vol += step_vol
            step_accel = add_speed / 60.0          # L/h × (1 min)
            self.accel_total += step_accel
            self.cycle_accel += step_accel

            vals = {
                "workStatus": 1.0,
                "cylinderPumpingPressure": clip(
                    11.0 + 3.2 * math.sin(phase * math.pi) + n(0, 0.55), 5.5, 15.4),
                "pumpingSpeed": speed,
                "circulatingPumpingVolume": round(self.cycle_vol, 3),
                "acceleratorAddSpeed": add_speed,
                "acceleratorProportion": proportion,
                "acceleratorOnceDosage": round(self.cycle_accel, 2),
                "acceleratorCumulativeDosage": round(self.accel_total, 2),
            }
            # 循环的最后一分钟：喷完这一格就前进一格，循环计数器清零重新累计。
            # 注意不能写 phase >= 1.0 —— 分钟粒度下 phase 的最大值是
            # (dur-1)/dur，永远到不了 1，按那个写循环就永远不会推进。
            if minute_of_day >= start + dur - 1:
                self.pile_idx = min(self.pile_idx + 1, len(self.piles) - 1)
                self.cycle_vol = 0.0
                self.cycle_accel = 0.0
        else:
            # 停机：泵送与速凝剂全部归零，循环计数器保持 0
            vals = {
                "workStatus": 0.0,
                "cylinderPumpingPressure": max(0.0, n(0.12, 0.06)),
                "pumpingSpeed": 0.0,
                "circulatingPumpingVolume": 0.0,
                "acceleratorAddSpeed": 0.0,
                "acceleratorProportion": 0.0,
                "acceleratorOnceDosage": 0.0,
                "acceleratorCumulativeDosage": round(self.accel_total, 2),
            }

        # 注入的越限事件：直接覆盖，其余字段保持真实工况
        if override:
            vals[override[0]] = override[1]

        ts_local = day_start_local + timedelta(minutes=minute_of_day)
        ts = ts_local.astimezone(CST)
        ts_epoch = int(ts.timestamp() * 1000)
        rec = {
            "record_id": ids.frame_record_id(self.device, ts_epoch, self.seq),
            "device_code": self.device,
            "ts_device": ts.isoformat(timespec="milliseconds"),
            "ts_epoch": ts_epoch,
            "seq": self.seq,
            "pile_id": self.piles[self.pile_idx % len(self.piles)],
            # 偶发可疑帧，让"数据质量"这一列在界面上真的有两种取值
            "quality": int(Quality.SUSPECT) if rng.random() < 0.004 else int(Quality.OK),
            "source": Source.SEED.value,
            "values": {k: round(float(vals[k]), 6) for k in PARAM_KEYS},
        }
        rec["row_hash"] = csv_schema.compute_row_hash(
            {**{c: rec.get(c, "") for c in csv_schema.META_COLUMNS}, **rec["values"]})
        self.seq += 1
        return rec


def clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _plan_day(rng: random.Random, day_offset: int) -> list[tuple[int, int]]:
    """规划当天的喷浆循环：(起始分钟, 持续分钟)。班次 08:00–18:00。"""
    cycles: list[tuple[int, int]] = []
    t = 8 * 60 + rng.randint(0, 25)
    end_of_shift = 18 * 60
    while t < end_of_shift:
        dur = rng.randint(25, 45)
        if t + dur > end_of_shift + 20:
            break
        cycles.append((t, dur))
        t += dur + rng.randint(18, 42)

    # 保证注入事件落在某个循环内 —— 否则"停机时速凝剂流量异常"讲不通
    for off, minute, _param, _value, _hold in EXCURSIONS:
        if off != day_offset:
            continue
        if not any(s <= minute < s + d for s, d in cycles):
            cycles.append((max(0, minute - 8), 40))
            cycles.sort()
    return cycles


# ==========================================================================
# 主入口
# ==========================================================================
def seed_history(cfg: dict | None = None, days: int | None = None,
                 conn=None) -> int:
    """生成并写入指定天数的历史数据。返回**本次实际新写入**的帧数。

    幂等：`record_id` 由"日期 + 分钟 + 序号"决定，重复调用只会有 0 行新增。

    `conn` 可传入既有连接（测试用临时库）；不传则用平台默认数据库。
    """
    cfg = cfg or config.load_config()
    sc = cfg["seed"]
    days = int(days if days is not None else sc["days"])
    rng = random.Random(int(sc["seed"]))
    device_code = cfg["device"]["device_code"]
    piles = pile_list(cfg)
    gen = _Gen(rng, device_code, piles)

    conn = conn if conn is not None else db.get_conn()
    today_local = now_utc().astimezone(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    total_frames = 0
    total_cycles = 0
    # 日志数据的身份快照与跨天累计（喷射部位轮转、累计工作时长）
    work_area_id = str(config.load_site().get("site_id", ""))
    operator = dict(cfg["device"].get("operator") or {})
    spray_seq = 0
    work_hours = 0.0

    for offset in range(days, 0, -1):
        day_local = today_local - timedelta(days=offset)
        cycles = _plan_day(rng, offset)
        overrides: dict[int, tuple[str, float]] = {}
        for off, minute, param, value, hold in EXCURSIONS:
            if off != offset:
                continue
            for m in range(minute, minute + hold):
                # 让覆盖值带一点抖动，看起来像真实传感器而不是常量
                overrides[m] = (param, round(value + rng.gauss(0, abs(value) * 0.004), 4))

        records: list[dict] = []
        for minute in range(24 * 60):
            cyc = next(((s, d) for s, d in cycles if s <= minute < s + d), None)
            records.append(gen.step(day_local, minute, cyc, overrides.get(minute)))

        # 分块入库，每天一个事务，避免一个超长事务把 WAL 撑爆
        with db.transaction(conn):
            written = _insert_day(conn, records, device_code)
            n_cyc, spray_seq, work_hours = _insert_cycles(
                conn, records, device_code, cycles, work_area_id=work_area_id,
                operator=operator, spray_seq=spray_seq, work_hours=work_hours)
            total_cycles += n_cyc
            _insert_audit(conn, records, device_code)
        total_frames += written

        if offset % 5 == 0 or offset == 1:
            log.info("种子进度：已生成 %d 天，累计写入 %d 帧", days - offset + 1, total_frames)

    with db.transaction(conn):
        db.set_meta(conn, "seeded_at", now_utc().isoformat(timespec="milliseconds"))
        db.set_meta(conn, "seed_days", str(days))
    # 最后喂一遍报警判定，把历时 30 天的报警记录也造出来
    _feed_alarms(conn, device_code, days, today_local)

    log.info("种子完成：%d 天 / %d 帧 / %d 个喷浆循环", days, total_frames, total_cycles)
    return total_frames


def _insert_day(conn, records: list[dict], device_code: str) -> int:
    """把一天的帧按 200 条一批写库，并登记对应的上传批次。

    批次的 `batch_id` 是用真实的 record_id 算出来的内容寻址 ID，
    不是随便编的字符串 —— 这样"上传批次"页展示的就是真实成立的批次，
    随时可以拿其中任何一批去重放，验幂等。
    """
    written = 0
    CHUNK = 200
    for i in range(0, len(records), CHUNK):
        chunk = records[i:i + CHUNK]
        rids = [r["record_id"] for r in chunk]
        bid = ids.make_batch_id(device_code, rids)
        ts_server = chunk[-1]["ts_device"]

        rows = [db.frame_row(r, bid, ts_server) for r in chunk]
        before = conn.total_changes
        conn.executemany(
            f"INSERT OR IGNORE INTO frames ({','.join(db.FRAME_COLUMNS)})"
            f" VALUES ({','.join('?' * len(db.FRAME_COLUMNS))})", rows)
        added = conn.total_changes - before
        written += added

        # 内容摘要也要写进去 —— 否则这些种子批次被重放时无法校验，
        # "重放但内容不符"这条防线对它们就是空的。
        digest = Batch(batch_id=bid, device_code=device_code,
                       records=chunk).content_digest()
        conn.execute(
            "INSERT OR IGNORE INTO batches(batch_id, device_code, kind, created_at,"
            " ts_server, record_count, accepted, duplicated, rejected, status, note,"
            " content_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (bid, device_code, "frame", chunk[0]["ts_device"], ts_server,
             len(chunk), len(chunk), 0, 0, "acked", "演示种子数据", digest))
    return written


def _insert_cycles(conn, records: list[dict], device_code: str,
                   cycles: list[tuple[int, int]], *, work_area_id: str = "",
                   operator: dict | None = None, spray_seq: int = 0,
                   work_hours: float = 0.0) -> tuple[int, int, float]:
    """按规划的循环区间造日志数据记录，字段见 params.LOG_FIELDS。

    方量取 circulatingPumpingVolume 计数器的实际差值；实喷方量按回弹率折算。
    返回 (写入条数, 喷射部位轮转序号, 累计工作时长) —— 三个值跨天接续。
    """
    op = operator or {}
    n = 0
    done = spray_seq
    hours = work_hours
    for start, dur in cycles:
        lo = max(0, start)
        hi = min(len(records), start + dur)
        if hi <= lo:
            continue
        seg = records[lo:hi]
        first, last = seg[0], seg[-1]
        v0 = float(first["values"]["circulatingPumpingVolume"])
        v1 = float(last["values"]["circulatingPumpingVolume"])
        vol = round(max(0.0, v1 - v0), 2)
        dur_s = (len(seg) - 1) * 60
        hours += dur_s / 3600.0
        try:
            loc = float(str(first["pile_id"]).replace("ZK", "").replace("+", ""))
        except ValueError:  # pragma: no cover - 桩号格式异常时里程留空
            loc = None
        conn.execute(
            "INSERT OR IGNORE INTO cycles(cycle_id, device_code, pile_id, started_at,"
            " ended_at, ts_epoch, volume_m3, frame_count, avg_pressure, avg_accel,"
            " max_pressure, batch_id, workAreaId, cname, cphone, cidno, locationMetre,"
            " endMetre, actualSprayingVolume, acceleratorOnceDosage,"
            " acceleratorCumulativeDosage, sprayLocation, accumulatedWorkingHours)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{device_code}:cyc:{first['ts_epoch']}", device_code, first["pile_id"],
             first["ts_device"], last["ts_device"], first["ts_epoch"],
             vol, len(seg),
             round(sum(r["values"]["cylinderPumpingPressure"] for r in seg) / len(seg), 3),
             round(sum(r["values"]["acceleratorAddSpeed"] for r in seg) / len(seg), 2),
             round(max(r["values"]["cylinderPumpingPressure"] for r in seg), 3), "",
             work_area_id,
             str(op.get("cname", "")), str(op.get("cphone", "")),
             str(op.get("cidno", "")),
             loc, (loc + PILE_STEP_M) if loc is not None else None,
             round(vol * (1.0 - REBOUND_RATE), 2),
             round(float(last["values"]["acceleratorOnceDosage"]), 2),
             round(float(last["values"]["acceleratorCumulativeDosage"]), 2),
             SPRAY_LOCATIONS[done % len(SPRAY_LOCATIONS)],
             round(hours, 2)))
        done += 1
        n += 1
    return n, done, hours


def _insert_audit(conn, records: list[dict], device_code: str) -> None:
    """每天一条"U 盘自动备份"审计事件，让审计页在演示前就不是空的。"""
    if not records:
        return
    last = records[-1]
    eid = f"{device_code}:audit:{last['ts_epoch']}:0"
    conn.execute(
        "INSERT OR IGNORE INTO audit_events(event_id, device_code, kind, ts_device,"
        " ts_epoch, payload_json, ts_server, batch_id) VALUES(?,?,?,?,?,?,?,?)",
        (eid, device_code, "usb_export", last["ts_device"], last["ts_epoch"],
         f'{{"path": "U盘/导出/帧数据_{last["ts_device"][:10]}.csv",'
         f' "rows": {len(records)}, "sha256": "自动备份，每日 23:59 触发"}}',
         last["ts_device"], ""))


def _feed_alarms(conn, device_code: str, days: int, today_local) -> None:
    """把种子帧喂给报警判定器，生成真实的历史报警记录。

    分天喂：判定状态在内存里，跨天连续喂与一次性喂结果一致，
    但分天的日志更好看，也避免单次事务过大。
    """
    from .alarms import AlarmEngine, feed_frames

    engine = AlarmEngine(config.load_thresholds())
    cols = ",".join(PARAM_KEYS)
    for offset in range(days, 0, -1):
        day_start = today_local - timedelta(days=offset)
        lo = int(day_start.astimezone(CST).timestamp() * 1000)
        hi = int((day_start + timedelta(days=1)).astimezone(CST).timestamp() * 1000)
        rows = conn.execute(
            f"SELECT record_id, device_code, ts_epoch, ts_device, pile_id, {cols}"
            " FROM frames WHERE device_code=? AND ts_epoch >= ? AND ts_epoch < ?"
            " ORDER BY ts_epoch", (device_code, lo, hi)).fetchall()
        recs = [{
            "record_id": r["record_id"], "device_code": r["device_code"],
            "ts_epoch": r["ts_epoch"], "ts_device": r["ts_device"],
            "pile_id": r["pile_id"],
            "values": {k: r[k] for k in PARAM_KEYS},
        } for r in rows]
        with db.transaction(conn):
            feed_frames(engine, conn, recs)

    n = conn.execute("SELECT COUNT(*) FROM alarms").fetchone()[0]
    log.info("报警判定完成：历史区间共产生 %d 条报警记录", n)
