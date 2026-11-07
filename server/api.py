# -*- coding: utf-8 -*-
"""平台全部 HTTP 路由。

分两类，权限模型不同：

**设备侧（终端调用）**
    GET  /api/health                  连通性探测，终端据此判断链路
    GET  /api/piles                   桩号清单下发 —— 桩号只能从这里来
    GET  /api/identity/check          现场录入工点/设备编号时的核对（只读）
    POST /api/heartbeat               心跳
    POST /api/upload                  ★ 数据上传，全局唯一入口

**业务侧（平台页面调用）**
    /api/overview /api/devices /api/frames /api/series /api/history
    /api/alarms /api/cycles /api/batches /api/revisions /api/frame/<id>
    /api/audit /api/rejects /api/params /api/export.csv

────────────────────────────────────────────────────────────────
上传接口的幂等设计（整个系统最关键的一段）
────────────────────────────────────────────────────────────────
1. `batch_id` 是内容寻址的（见 common/ids.py），同一批数据重传得到同一个 ID。
2. 进来先查 `batches` 表。**查到就直接返回当初那份回执**，一个字都不改，
   只把 `duplicate_batch` 置为 true。终端看到的就是"我早就传过了"。
3. 查不到才真正处理。整个处理包在 `BEGIN IMMEDIATE` 里 ——
   两个并发的重复请求会被 SQLite 串行化，第二个进来时第一个已提交，
   于是走第 2 步返回原回执，不会产生两份数据。
4. 记录级用 `INSERT OR IGNORE`（主键 record_id）。这是最后一道保险：
   即便前面的判重有漏洞，也不可能写出重复行。
5. 原始值永不覆盖。`record_id` 已存在但 `row_hash` 不同 → 整条拒收
   `DUPLICATE_CONFLICT`，写进 `rejects` 表等人工处理，绝不 UPDATE。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
from typing import Any

from common import config, http_util
from common.enums import BatchStatus, EventKind, ReasonCode, RecordResult
from common.ids import (MAX_BATCH_RECORDS, is_valid_batch_id, is_valid_record_id,
                        parse_record_id, verify_batch_id)
from common.machine import PILE_STEP_M, REBOUND_RATE, SPRAY_LOCATIONS
from common.params import PARAM_BY_KEY, PARAM_KEYS, as_dicts, check_value, registry
from common.protocol import (HEARTBEAT_TIMEOUT_S, MAX_CLOCK_SKEW_S, Batch,
                             Heartbeat, Receipt, bad_result, ok_result)
from common.timeutil import CST, fmt_local, now_epoch, now_utc_iso, to_datetime

from . import alarms as alarms_mod
from . import db

log = logging.getLogger("server.api")


def _pile_metre(pile: str) -> float | None:
    """"ZK105+207" → 105207.0。取不出数字时返回 None（日志里程留空）。"""
    digits = "".join(ch for ch in str(pile or "") if ch.isdigit())
    return float(digits) if digits else None


class Api:
    """路由处理器。持有报警引擎与配置快照。"""

    def __init__(self, cfg: dict | None = None, engine: alarms_mod.AlarmEngine | None = None):
        self.cfg = cfg or config.load_config()
        self.thresholds = config.load_thresholds()
        # 名字里带 registry 不是啰嗦：`self.params` 会和下面那个处理 /api/params
        # 的 `params()` 方法撞名，属性把方法盖掉之后路由一调就是
        # "TypeError: 'dict' object is not callable"。
        self.param_registry = registry(self.thresholds)
        self.engine = engine or alarms_mod.AlarmEngine(self.thresholds)
        self.stopping = False          # 一键关闭进行中：随 overview/health 带给页面

    # ==================================================================
    # 设备侧
    # ==================================================================
    def health(self, conn) -> tuple[int, dict]:
        return 200, {
            "ok": True,
            "service": "wsc-platform",
            "server_time": now_utc_iso(),
            "epoch": now_epoch(),
            "protocol": "1.0",
            "device_count": conn.execute(
                "SELECT COUNT(*) FROM devices").fetchone()[0],
        }

    def piles(self, conn, q: dict) -> tuple[int, dict]:
        """下发桩号清单。终端**只能**从这个清单里选桩号。

        这是桩号防伪的第一道源头：清单里没有的里程，
        终端界面上根本点不出来，CSV 导入时也会被判冲突。
        """
        site_id = http_util.qs_one(q, "site_id", "")
        sql = "SELECT pile_id, site_id, line, mileage_m, label, issued_at FROM piles WHERE active=1"
        args: list = []
        if site_id:
            sql += " AND site_id=?"
            args.append(site_id)
        sql += " ORDER BY mileage_m"
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        return 200, {
            "site_id": site_id,
            "count": len(rows),
            "issued_at": now_utc_iso(),
            "piles": rows,
        }

    def identity_check(self, conn, q: dict) -> tuple[int, dict]:
        """现场录入工点/设备编号时的**核对口**。只读，不改任何东西。

        判定权完全在平台，分两半，宽严不同：

        * **工点** —— 必须是平台档案里有的。没有就 409 拒掉，并回已知工点列表。
          工点决定数据的归属和桩号范围，是个"档案"，终端只能引用不能编造。
        * **设备** —— 平台不知道的设备编号记为 `known: false`，**不拒**。
          设备是硬件本身，第一次接入时平台无从预知；终端会据此提示
          "平台此前没有这台设备的档案"。它此前登记在别的工点时会返回 `move`，
          现场看到的就是"转场确认"。

        这个接口**不写库**：核对是给人看的一步，用户可能核对了又反悔。
        设备归属的实际变更走心跳（见 `heartbeat`），好处是持续对齐 ——
        一次性通知只要那次请求失败，平台上的归属就永远停在旧工点。
        """
        site_id = http_util.qs_one(q, "site_id", "").strip()
        device_code = http_util.qs_one(q, "device_code", "").strip()
        if not site_id:
            return 400, {"error": "缺少 site_id"}
        if not device_code:
            return 400, {"error": "缺少 device_code"}

        site = conn.execute(
            "SELECT * FROM sites WHERE site_id=? AND active=1", (site_id,)).fetchone()
        if site is None:
            known = [dict(r) for r in conn.execute(
                "SELECT site_id, site_name, project_name, line FROM sites"
                " WHERE active=1 ORDER BY site_id").fetchall()]
            return 409, {
                "error": f"平台没有工点 {site_id} 的档案，不能录入",
                "code": ReasonCode.SITE_NOT_ISSUED.value,
                "known_sites": known,
            }

        stat = conn.execute(
            "SELECT COUNT(*) AS n, MIN(mileage_m) AS lo, MAX(mileage_m) AS hi"
            " FROM piles WHERE site_id=? AND active=1", (site_id,)).fetchone()
        first = conn.execute(
            "SELECT pile_id FROM piles WHERE site_id=? AND active=1"
            " ORDER BY mileage_m LIMIT 1", (site_id,)).fetchone()
        last = conn.execute(
            "SELECT pile_id FROM piles WHERE site_id=? AND active=1"
            " ORDER BY mileage_m DESC LIMIT 1", (site_id,)).fetchone()

        dev = conn.execute("SELECT * FROM devices WHERE device_code=?",
                           (device_code,)).fetchone()
        out: dict[str, Any] = {
            "ok": True,
            "server_time": now_utc_iso(),
            "site": {
                "site_id": site["site_id"], "site_name": site["site_name"],
                "project_name": site["project_name"], "tunnel": site["tunnel"],
                "line": site["line"],
                "pile_count": int(stat["n"] or 0),
                "first_pile": first["pile_id"] if first else "",
                "last_pile": last["pile_id"] if last else "",
                "pile_list_version": self._pile_version(conn, site_id),
            },
            "device": {
                "device_code": device_code,
                "known": dev is not None,
                "device_name": dev["device_name"] if dev else "",
                "model": dev["model"] if dev else "",
                "site_id": dev["site_id"] if dev else "",
                "site_name": dev["site_name"] if dev else "",
                "last_seen": dev["last_seen"] if dev else None,
                "live_state": self._live_state(dict(dev)) if dev else "offline",
            },
            "move": None,
            "warnings": [],
        }
        if dev is None:
            out["warnings"].append(
                f"平台此前没有设备 {device_code} 的档案，接入后将按自报信息登记")
        elif dev["site_id"] and dev["site_id"] != site_id:
            out["move"] = {"from_site_id": dev["site_id"],
                           "from_site_name": dev["site_name"]}
            out["warnings"].append(
                f"设备原登记在「{dev['site_name'] or dev['site_id']}」，"
                f"本次为转场：生效后归属与桩号清单都会换到"
                f"「{site['site_name']}」")
        if int(stat["n"] or 0) == 0:
            out["warnings"].append(
                "该工点尚未下发桩号清单，生效后采集不会出数（等待平台下发）")
        return 200, out

    def heartbeat(self, conn, payload: dict) -> tuple[int, dict]:
        hb = Heartbeat.from_dict(payload)
        if not hb.device_code:
            return 400, {"error": "缺少 device_code"}
        ts = now_utc_iso()
        # 设备自报的工点 —— **只认平台档案里有的**。自报一个档案外的工点
        # 不报错，只是被忽略：设备还在正常干活，没必要为一个人为写错的
        # 字段让它掉线；而归属留在原处比写进去一个不存在的工点诚实。
        site = self._known_site(conn, hb.site_id)
        with db.transaction(conn):
            cur = conn.execute(
                "UPDATE devices SET last_seen=?, backlog=?, link_state=?, version=?"
                " WHERE device_code=?",
                (ts, hb.buffer_backlog, hb.link_state, hb.version, hb.device_code))
            if cur.rowcount == 0:
                # 未知设备自动建档：**终端的 device_code 是它自报的**，
                # 所以设备名不再拿平台配置去套（那会把一台陌生设备
                # 标成平台配置里那台车的名字），工点也只用设备自报的。
                conn.execute(
                    "INSERT OR IGNORE INTO devices(device_code, device_name, model,"
                    " site_id, site_name, last_seen, link_state, backlog, version,"
                    " note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (hb.device_code, hb.device_code, "",
                     site["site_id"] if site else "",
                     site["site_name"] if site else "",
                     ts, hb.link_state, hb.buffer_backlog, hb.version,
                     "终端自报，未核实"))
            elif site:
                # 已有档案：只跟着设备自报的工点走，**绝不改设备名与型号** ——
                # 那是平台台账，设备说什么就是什么的话台账就没意义了。
                conn.execute(
                    "UPDATE devices SET site_id=?, site_name=? WHERE device_code=?",
                    (site["site_id"], site["site_name"], hb.device_code))
        return 200, {
            "ok": True,
            "server_time": ts,
            "echo_backlog": hb.buffer_backlog,
            # 平台可以顺着心跳回告终端几件事：桩号清单有没有更新、要不要降频。
            # 版本按**设备实际所在工点**算（自报了且档案里有），设备没自报时
            # 退回全局 —— 老终端不发 site_id，行为与从前一致。
            "pile_list_version": self._pile_version(
                conn, site["site_id"] if site else ""),
            "upload_interval_s": self.cfg["upload"]["interval_s"],
        }

    @staticmethod
    def _known_site(conn, site_id: str) -> dict | None:
        """这个工点是不是平台档案里的。空串直接返回 None（设备没自报）。"""
        sid = (site_id or "").strip()
        if not sid:
            return None
        row = conn.execute(
            "SELECT site_id, site_name FROM sites WHERE site_id=? AND active=1",
            (sid,)).fetchone()
        return dict(row) if row else None

    def upload(self, conn, payload: dict) -> tuple[int, dict]:
        """★ 数据上传。幂等的实现见模块头部说明。"""
        try:
            batch = Batch.from_dict(payload)
        except (AttributeError, TypeError, ValueError) as exc:
            return 400, {"error": f"报文格式错误：{exc}"}

        # ---- 结构性校验（整批级别，不写库） ----
        if not is_valid_batch_id(batch.batch_id):
            return 400, {"error": f"batch_id 格式不合法：{batch.batch_id!r}"}
        if not batch.device_code:
            return 400, {"error": "缺少 device_code"}
        if not batch.records:
            return 400, {"error": "批次内没有记录"}
        if len(batch.records) > MAX_BATCH_RECORDS:
            return 413, {"error": f"单批记录数 {len(batch.records)} 超过上限 "
                                  f"{MAX_BATCH_RECORDS}，请分批重传",
                         "code": ReasonCode.BATCH_TOO_LARGE.value}
        if not verify_batch_id(batch.batch_id, batch.record_ids):
            return 400, {"error": "batch_id 与批次内容不符（内容寻址校验失败）"}

        # ---- 幂等闸门 ----
        digest = batch.content_digest()
        status, payload = self._replay(conn, batch.batch_id, digest)
        if payload is not None:
            return status, payload

        with db.transaction(conn):
            # 事务内再查一次：并发的重复请求可能刚被另一个线程写进去
            status, payload = self._replay(conn, batch.batch_id, digest)
            if payload is not None:
                return status, payload

            receipt = self._process_batch(conn, batch)
            conn.execute(
                "INSERT OR IGNORE INTO batches(batch_id, device_code, kind, created_at,"
                " ts_server, record_count, accepted, duplicated, rejected, status,"
                " note, receipt_json, content_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (batch.batch_id, batch.device_code, batch.kind, batch.created_at,
                 now_utc_iso(), len(batch.records), receipt.accepted,
                 receipt.duplicated, receipt.rejected, receipt.status,
                 batch.note, json.dumps(receipt.to_dict(), ensure_ascii=False),
                 digest))
            conn.execute(
                "UPDATE devices SET total_frames = total_frames + ?, last_seen=?"
                " WHERE device_code=?",
                (receipt.accepted, now_utc_iso(), batch.device_code))

        log.info("批次 %s 入库：新增 %d / 重复 %d / 拒收 %d",
                 batch.batch_id, receipt.accepted, receipt.duplicated, receipt.rejected)
        return 200, receipt.to_dict()

    def _replay(self, conn, batch_id: str, digest: str) -> tuple[int, dict | None]:
        """幂等闸门。返回 (状态码, 响应体)；响应体为 None 表示"新批次，继续处理"。

        三种情况：
        * 没见过这个 batch_id          -> (200, None)，继续走入库流程
        * 见过，且内容摘要一致          -> (200, 原回执 + duplicate_batch)，什么都不写
        * 见过，但内容摘要对不上        -> (409, 冲突)，**这是篡改信号，必须报出来**
        """
        row = conn.execute(
            "SELECT receipt_json, content_digest FROM batches WHERE batch_id=?",
            (batch_id,)).fetchone()
        if row is None or not row["receipt_json"]:
            return 200, None

        stored_digest = row["content_digest"] or ""
        if stored_digest and digest and stored_digest != digest:
            log.error("批次 %s 重放但内容摘要不一致：库内 %s / 本次 %s —— 疑似被篡改",
                      batch_id, stored_digest[:12], digest[:12])
            self._record_reject(conn, batch_id, "", "", ReasonCode.DUPLICATE_CONFLICT,
                                f"批次 {batch_id} 已存在且内容摘要不一致（疑似篡改），"
                                "本次上传整批拒绝", {"expected": stored_digest,
                                                     "actual": digest},
                                now_utc_iso())
            return 409, {
                "error": "该 batch_id 已存在，但本次上传的内容与已入库的不一致，整批拒绝",
                "code": ReasonCode.DUPLICATE_CONFLICT.value,
                "batch_id": batch_id,
                "hint": "如需修正数据，请走 U 盘校对流程生成修订批，不要直接改写后重传",
            }

        stored = json.loads(row["receipt_json"])
        stored["duplicate_batch"] = True
        stored["message"] = "该批次此前已接收，返回原回执"
        return 200, stored

    # ==================================================================
    # 批次处理
    # ==================================================================
    def _process_batch(self, conn: sqlite3.Connection, batch: Batch) -> Receipt:
        kind = batch.kind or EventKind.FRAME.value
        if kind == EventKind.REVISION.value:
            return self._process_revisions(conn, batch)
        if kind == EventKind.AUDIT.value:
            return self._process_audit(conn, batch)
        return self._process_frames(conn, batch)

    def _process_frames(self, conn: sqlite3.Connection, batch: Batch) -> Receipt:
        ts_server = now_utc_iso()
        now_ms = int(now_epoch() * 1000)
        issued_piles = self._issued_piles(conn)

        order: list[str] = []                    # 保持与请求一致的输出顺序
        verdict: dict[str, dict] = {}            # record_id -> 结果
        incoming: list[tuple[dict, str]] = []    # 通过校验、待入库的 (记录, row_hash)
        rejected: list[tuple[dict, str, ReasonCode, str]] = []

        def reject(rec: dict, rid: str, code: ReasonCode, msg: str) -> None:
            """记一条拒收：既进本次回执，也落 `rejects` 表。

            落表很重要 —— 终端拿到回执后只知道"被拒了"，
            运维人员要能在平台上看清是哪条、为什么、怎么修。
            """
            verdict[rid] = bad_result(rid, code, msg)
            rejected.append((rec, rid, code, msg))

        for rec in batch.records:
            rid = str(rec.get("record_id", ""))
            # 批内自重复：同一个 record_id 出现两次，第二条直接判重复
            if rid and rid in verdict:
                order.append(rid)
                verdict[rid] = ok_result(rid, duplicate=True)
                continue
            if rid:
                order.append(rid)

            # --- 逐条校验。任何一条不过只拒这一条，不影响整批 ---
            if not is_valid_record_id(rid):
                reject(rec, rid, ReasonCode.SCHEMA, f"record_id 格式不合法：{rid!r}")
                continue
            parsed = parse_record_id(rid)
            if parsed["kind"] != "frame":
                reject(rec, rid, ReasonCode.SCHEMA, "帧批次里混入了非帧记录")
                continue
            if parsed["device_code"] != batch.device_code:
                reject(rec, rid, ReasonCode.SCHEMA,
                       f"record_id 里的设备编号 {parsed['device_code']} 与本批 "
                       f"device_code {batch.device_code} 不符")
                continue

            ts_epoch = int(rec.get("ts_epoch") or parsed["ts_epoch"])
            if ts_epoch - now_ms > MAX_CLOCK_SKEW_S * 1000:
                reject(rec, rid, ReasonCode.TS_FUTURE,
                       f"采集时间 {fmt_local(ts_epoch)} 超前服务器 "
                       f"{int((ts_epoch - now_ms) / 1000)} 秒，拒绝入库")
                continue

            pile_id = str(rec.get("pile_id", "")).strip()
            if pile_id not in issued_piles:
                reject(rec, rid, ReasonCode.PILE_NOT_ISSUED,
                       f"桩号 {pile_id!r} 不在平台下发清单内，拒绝入库")
                continue

            values = rec.get("values") or {}
            if not isinstance(values, dict):
                reject(rec, rid, ReasonCode.SCHEMA, "values 不是对象")
                continue
            errs = self._validate_values(values)
            if errs:
                reject(rec, rid, ReasonCode.SCHEMA, "；".join(errs[:3]))
                continue

            verdict[rid] = None                  # 通过校验，待判重
            incoming.append((rec, str(rec.get("row_hash", ""))))

        # --- 一次查出已存在的同 ID，区分"重复"与"内容冲突" ---
        existing = self._existing_hashes(conn, [r["record_id"] for r, _ in incoming])
        fresh: list[tuple] = []
        for rec, rh in incoming:
            rid = rec["record_id"]
            if rid in existing:
                if not rh or existing[rid] == rh:
                    verdict[rid] = ok_result(rid, duplicate=True)
                else:
                    # 原值已在平台上，且内容对不上 —— 绝不覆盖，转人工
                    reject(rec, rid, ReasonCode.DUPLICATE_CONFLICT,
                           "同一 record_id 已在平台存在且内容不一致；原值永久保留，"
                           "如需修正请走 U 盘校对流程（修订留痕）")
                continue
            fresh.append(db.frame_row(rec, batch.batch_id, ts_server))

        # --- 拒收明细落表 ---
        for rec, rid, code, msg in rejected:
            self._record_reject(conn, batch.batch_id, rid, batch.device_code,
                                code, msg, rec, ts_server)

        # --- 真正写入 ---
        accepted_ids: set[str] = set()
        if fresh:
            before = conn.total_changes
            conn.executemany(
                f"INSERT OR IGNORE INTO frames ({','.join(db.FRAME_COLUMNS)})"
                f" VALUES ({','.join('?' * len(db.FRAME_COLUMNS))})", fresh)
            written = conn.total_changes - before
            ids = [r[0] for r in fresh]
            if written == len(fresh):
                accepted_ids = set(ids)
            else:
                # 极少见的竞态：有行被别的请求先写进去了。
                # 以"这行现在是不是挂着我的 batch_id"为准，逐个确认，
                # 不猜、不按数量摊派。
                log.warning("批次 %s 预插入 %d 行、实际写入 %d 行，逐行复核归属",
                            batch.batch_id, len(fresh), written)
                accepted_ids = self._ids_owned_by_batch(conn, ids, batch.batch_id)

        accepted_records: list[dict] = []
        for rec, _ in incoming:
            rid = rec["record_id"]
            if rid in accepted_ids:
                verdict[rid] = ok_result(rid)
                rec["batch_id"] = batch.batch_id
                accepted_records.append(rec)
            elif verdict.get(rid) is None:
                verdict[rid] = ok_result(rid, duplicate=True)

        # --- 报警判定：只喂真正新入库的帧 ---
        if accepted_records and self.cfg["alarms"]["enabled"]:
            for ev in alarms_mod.feed_frames(self.engine, conn, accepted_records):
                log.warning("%s", ev)

        # --- 喷浆循环：从新入库的帧里推出来 ---
        if accepted_records:
            self._update_cycles(conn, accepted_records)

        results = [verdict[r] for r in order if verdict.get(r) is not None]
        # 理论上不会有 None 残留；真有的话说明有 rid 没走到判定，宁可报错也不静默丢
        assert len(results) == len(order), \
            f"批次 {batch.batch_id} 有记录未产生判定结果"

        status = (BatchStatus.PARTIAL.value
                  if any(r.get("result") == RecordResult.REJECTED.value for r in results)
                  else BatchStatus.ACKED.value)
        return Receipt(batch_id=batch.batch_id, results=results,
                       status=status, server_time=ts_server)

    def _process_revisions(self, conn: sqlite3.Connection, batch: Batch) -> Receipt:
        """修订批。只追加，绝不 UPDATE `frames` 的参数列。"""
        ts_server = now_utc_iso()
        results: list[dict] = []
        applied = 0
        touched: set[str] = set()

        for rec in batch.records:
            rid = str(rec.get("record_id", ""))          # 这里是 revision_id
            target = str(rec.get("target_record_id", ""))
            patch = rec.get("patch") or {}
            reason = str(rec.get("reason_code", ""))
            if not is_valid_record_id(rid) or not is_valid_record_id(target):
                results.append(bad_result(rid, ReasonCode.SCHEMA, "修订 ID 或目标 ID 不合法"))
                continue
            if not isinstance(patch, dict) or not patch:
                results.append(bad_result(rid, ReasonCode.SCHEMA, "patch 为空"))
                continue
            if not reason:
                results.append(bad_result(rid, ReasonCode.SCHEMA, "必须填写修订原因"))
                continue

            # 只允许改测量值；桩号与时间戳不可通过修订改（见计划书 5.3）
            forbidden = [k for k in patch if k in ("ts_device", "ts_epoch", "record_id",
                                                   "device_code", "seq")]
            if forbidden:
                results.append(bad_result(
                    rid, ReasonCode.SCHEMA,
                    "以下字段不允许修订：" + "、".join(forbidden)))
                continue

            bad_fields = [k for k in patch if k not in PARAM_BY_KEY]
            if bad_fields:
                results.append(bad_result(
                    rid, ReasonCode.SCHEMA,
                    "未知参数：" + "、".join(bad_fields)))
                continue

            row = conn.execute(
                "SELECT record_id FROM frames WHERE record_id=?", (target,)).fetchone()
            if row is None:
                results.append(bad_result(
                    rid, ReasonCode.SCHEMA, f"目标记录 {target} 不存在于平台"))
                continue

            value_errors = []
            clean_patch: dict[str, float] = {}
            for k, v in patch.items():
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    value_errors.append(f"{PARAM_BY_KEY[k].name} 不是数值")
                    continue
                msg = check_value(PARAM_BY_KEY[k], fv)
                if msg:
                    value_errors.append(msg)
                    continue
                clean_patch[k] = fv
            if value_errors:
                results.append(bad_result(rid, ReasonCode.RANGE, "；".join(value_errors)))
                continue

            cur = conn.execute(
                "INSERT OR IGNORE INTO frame_revisions(revision_id, record_id,"
                " device_code, patch_json, reason_code, reason_note, operator_name,"
                " ts_device, ts_server, batch_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (rid, target, batch.device_code, json.dumps(clean_patch, ensure_ascii=False),
                 reason, str(rec.get("reason_note", "")),
                 str(rec.get("operator_name", "")),
                 str(rec.get("ts_device", "")), ts_server, batch.batch_id))
            if cur.rowcount == 0:
                results.append(ok_result(rid, duplicate=True))
                continue

            results.append(ok_result(rid))
            applied += 1
            touched.add(target)
            self._recompute_revision_projection(conn, target, rid, ts_server)

        status = (BatchStatus.PARTIAL.value
                  if any(r.get("result") == RecordResult.REJECTED.value for r in results)
                  else BatchStatus.ACKED.value)
        if applied:
            log.info("修订批 %s：应用 %d 条，涉及 %d 条原始记录",
                     batch.batch_id, applied, len(touched))
        return Receipt(batch_id=batch.batch_id, results=results,
                       status=status, server_time=ts_server)

    def _process_audit(self, conn: sqlite3.Connection, batch: Batch) -> Receipt:
        """审计事件。即使数据本体还没传上来，操作本身也要留痕。

        事件的标识取 `record_id`：批次级的内容寻址校验只看这个字段
        （见 `Batch.record_ids`），审计记录也必须带上它。
        `event_id` 作为兼容字段保留 —— 老终端只发 event_id 时不至于丢数据。
        """
        ts_server = now_utc_iso()
        results: list[dict] = []
        for rec in batch.records:
            eid = str(rec.get("record_id") or rec.get("event_id") or "")
            if not eid:
                results.append(bad_result(eid, ReasonCode.SCHEMA,
                                          "审计事件缺少 record_id / event_id"))
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO audit_events(event_id, device_code, kind,"
                " ts_device, ts_epoch, payload_json, ts_server, batch_id)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (eid, batch.device_code, str(rec.get("kind", "audit")),
                 str(rec.get("ts_device", "")), int(rec.get("ts_epoch", 0)),
                 json.dumps(rec.get("payload") or {}, ensure_ascii=False),
                 ts_server, batch.batch_id))
            results.append(ok_result(eid, duplicate=(cur.rowcount == 0)))
        return Receipt(batch_id=batch.batch_id, results=results, server_time=ts_server)

    # ---- 批次处理用的小工具 ----
    def _validate_values(self, values: dict) -> list[str]:
        errs = []
        for k in PARAM_KEYS:
            if k not in values:
                errs.append(f"{PARAM_BY_KEY[k].name} 缺失")
                continue
            try:
                v = float(values[k])
            except (TypeError, ValueError):
                errs.append(f"{PARAM_BY_KEY[k].name} 不是数值")
                continue
            msg = check_value(PARAM_BY_KEY[k], v)
            if msg:
                errs.append(msg)
        return errs

    @staticmethod
    def _issued_piles(conn) -> set[str]:
        return {r[0] for r in conn.execute(
            "SELECT pile_id FROM piles WHERE active=1").fetchall()}

    @staticmethod
    def _pile_version(conn, site_id: str = "") -> str:
        """清单版本，等于该工点各桩号 `issued_at` 的最大值。

        **要按工点分开算**：终端拿这个值跟"自己那份清单的版本"比，不同就重拉。
        如果这里回全局最大值，那么另一个工点补发桩号时，本工点的终端会
        每 5 秒重拉一次永远对不上的清单 —— 白跑，且日志上看着像一直在更新。
        """
        sid = (site_id or "").strip()
        if sid:
            row = conn.execute(
                "SELECT MAX(issued_at) AS m FROM piles WHERE site_id=?", (sid,)).fetchone()
        else:
            row = conn.execute("SELECT MAX(issued_at) AS m FROM piles").fetchone()
        return row["m"] or ""

    @staticmethod
    def _existing_hashes(conn, record_ids: list[str]) -> dict[str, str]:
        """批量查已存在的 record_id -> row_hash。分块避免 SQL 变量数上限。"""
        out: dict[str, str] = {}
        CHUNK = 400
        for i in range(0, len(record_ids), CHUNK):
            chunk = record_ids[i:i + CHUNK]
            if not chunk:
                continue
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(
                    f"SELECT record_id, row_hash FROM frames WHERE record_id IN ({marks})",
                    chunk).fetchall():
                out[row["record_id"]] = row["row_hash"]
        return out

    @staticmethod
    def _ids_owned_by_batch(conn, record_ids: list[str], batch_id: str) -> set[str]:
        """复核这批 record_id 究竟有哪些真的挂在指定 batch_id 下。

        只在 `INSERT OR IGNORE` 写入行数与预期不符（并发竞态）时才调用。
        不靠数量摊派，直接问数据库，避免把别人的数据算成自己入库的。
        """
        out: set[str] = set()
        CHUNK = 400
        for i in range(0, len(record_ids), CHUNK):
            chunk = record_ids[i:i + CHUNK]
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(
                    f"SELECT record_id FROM frames WHERE record_id IN ({marks})"
                    " AND batch_id=?", chunk + [batch_id]).fetchall():
                out.add(row["record_id"])
        return out

    @staticmethod
    def _record_reject(conn, batch_id: str, rid: str, dev: str,
                       code: ReasonCode, message: str, payload: dict, ts_server: str) -> None:
        conn.execute(
            "INSERT INTO rejects(batch_id, record_id, device_code, reason_code,"
            " message, payload_json, ts_server) VALUES(?,?,?,?,?,?,?)",
            (batch_id, rid, dev, code.value, message,
             json.dumps(payload, ensure_ascii=False, default=str)[:4000], ts_server))

    @staticmethod
    def _recompute_revision_projection(conn, record_id: str, revision_id: str,
                                       ts_server: str) -> None:
        """把该记录的所有修订按时间顺序合并，写进 `frames.rev_json`。

        这只是一份**缓存投影**，任何时候都能从 `frame_revisions` 重算出来。
        原始参数列（原始的 pump_pressure 等）一个字节都不动 ——
        所以"原值永久保留"这条承诺是物理成立的，不是口头约定。
        """
        rows = conn.execute(
            "SELECT patch_json, ts_server FROM frame_revisions"
            " WHERE record_id=? ORDER BY ts_server, revision_id", (record_id,)).fetchall()
        merged: dict[str, float] = {}
        for r in rows:
            try:
                merged.update(json.loads(r["patch_json"]))
            except (json.JSONDecodeError, TypeError):  # pragma: no cover
                continue
        conn.execute(
            "UPDATE frames SET rev_count=?, rev_json=?, latest_rev_id=?, last_rev_at=?"
            " WHERE record_id=?",
            (len(rows), json.dumps(merged, ensure_ascii=False) if merged else None,
             revision_id, ts_server, record_id))

    # ==================================================================
    # ==================================================================
    # 日志数据（按喷浆循环派生）
    # ==================================================================
    def _update_cycles(self, conn: sqlite3.Connection, records: list[dict]) -> None:
        """从已入库的帧里推出日志数据记录（字段见 params.LOG_FIELDS）。

        判定规则（阈值来自 config.acquisition，与终端 CycleTracker 一致）：
          workStatus 喷浆 且 泵送压力 > cycle_start_pressure → 开始
          泵送压力 < cycle_end_pressure 持续 cycle_end_hold_s  → 结束
        身份字段在循环**开始**时快照（之后换操作手不改历史）；
        用量、里程与实喷方量在**结束**时按帧数据折算。
        方量取 `circulatingPumpingVolume` 计数器在起止时刻的差值。
        """
        acq = self.cfg["acquisition"]
        op = dict(self.cfg["device"].get("operator") or {})
        site_id = str(config.load_site().get("site_id", ""))
        for rec in sorted(records, key=lambda r: int(r.get("ts_epoch", 0))):
            vals = rec.get("values") or {}
            dev = rec.get("device_code", "")
            try:
                press = float(vals.get("cylinderPumpingPressure", 0))
                vol = float(vals.get("circulatingPumpingVolume", 0))
                working = float(vals.get("workStatus", 0))
            except (TypeError, ValueError):
                continue
            ts_epoch = int(rec.get("ts_epoch", 0))
            ts_iso = str(rec.get("ts_device", ""))
            pile = str(rec.get("pile_id", ""))

            active = conn.execute(
                "SELECT cycle_id, ts_epoch, volume_m3, pile_id, locationMetre"
                " FROM cycles WHERE device_code=? AND ended_at IS NULL"
                " ORDER BY started_at DESC LIMIT 1", (dev,)).fetchone()

            spraying = (working >= 0.5 and press > acq["cycle_start_pressure"])
            if active is None:
                if spraying:
                    seq = conn.execute(
                        "SELECT COUNT(*) FROM cycles WHERE device_code=?",
                        (dev,)).fetchone()[0]
                    conn.execute(
                        "INSERT OR IGNORE INTO cycles(cycle_id, device_code, pile_id,"
                        " started_at, ts_epoch, volume_m3, frame_count, max_pressure,"
                        " batch_id, workAreaId, cname, cphone, cidno, locationMetre,"
                        " sprayLocation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f"{dev}:cyc:{ts_epoch}", dev, pile, ts_iso, ts_epoch,
                         vol, 1, press, str(rec.get("batch_id", "")),
                         site_id, str(op.get("cname", "")), str(op.get("cphone", "")),
                         str(op.get("cidno", "")), _pile_metre(pile),
                         SPRAY_LOCATIONS[seq % len(SPRAY_LOCATIONS)]))
                continue

            if press < acq["cycle_end_pressure"]:
                # 收工那一帧属于"循环结束"而不是"循环内"，不计数、不计峰值。
                # 收工时把均值与用量一并算出来 —— 循环记录一天也就几十条，
                # 这几条聚合查询的开销可以忽略，换来的是报表能直接用。
                agg = conn.execute(
                    "SELECT AVG(cylinderPumpingPressure) AS p,"
                    " AVG(acceleratorAddSpeed) AS a"
                    " FROM frames WHERE device_code=? AND ts_epoch BETWEEN ? AND ?",
                    (dev, int(active["ts_epoch"]), ts_epoch)).fetchone()
                # 停机帧上循环计数器已清零，闭合时的方量与用量必须取
                # **最后一个喷浆帧**的值，否则整个循环会被算成 0。
                last = conn.execute(
                    "SELECT acceleratorOnceDosage AS once,"
                    " acceleratorCumulativeDosage AS cum,"
                    " circulatingPumpingVolume AS vol"
                    " FROM frames WHERE device_code=? AND ts_epoch BETWEEN ? AND ?"
                    " AND workStatus >= 0.5"
                    " ORDER BY ts_epoch DESC LIMIT 1",
                    (dev, int(active["ts_epoch"]), ts_epoch)).fetchone()
                vol_last = float(last["vol"]) if last and last["vol"] is not None else vol
                vol_done = max(0.0, vol_last - float(active["volume_m3"] or 0))
                dur_h = max(0.0, (ts_epoch - int(active["ts_epoch"])) / 3600000.0)
                # 累计工时是**单调递增的累计列**：接续值取已结束记录里的最大值，
                # 不能 SUM —— 对累计列求和是把每一行的"至今累计"再加一遍，
                # 会指数式膨胀（210 条种子就能加出 22 万小时）。
                prev_h = conn.execute(
                    "SELECT COALESCE(MAX(accumulatedWorkingHours),0) FROM cycles"
                    " WHERE device_code=? AND ended_at IS NOT NULL AND cycle_id<>?",
                    (dev, active["cycle_id"])).fetchone()[0]
                loc = active["locationMetre"]
                conn.execute(
                    "UPDATE cycles SET ended_at=?, volume_m3=?,"
                    " avg_pressure=?, avg_accel=?,"
                    " pile_id=COALESCE(NULLIF(pile_id,''),?),"
                    " endMetre=?, actualSprayingVolume=?,"
                    " acceleratorOnceDosage=?, acceleratorCumulativeDosage=?,"
                    " accumulatedWorkingHours=? WHERE cycle_id=?",
                    (ts_iso, vol_done,
                     agg["p"] if agg else None, agg["a"] if agg else None,
                     pile,
                     (float(loc) + PILE_STEP_M) if loc is not None else None,
                     round(vol_done * (1.0 - REBOUND_RATE), 2),
                     float(last["once"]) if last and last["once"] is not None else None,
                     float(last["cum"]) if last and last["cum"] is not None else None,
                     round(prev_h + dur_h, 2),
                     active["cycle_id"]))
                continue

            conn.execute(
                "UPDATE cycles SET frame_count=frame_count+1,"
                " max_pressure=MAX(COALESCE(max_pressure,0), ?) WHERE cycle_id=?",
                (press, active["cycle_id"]))

    # ==================================================================
    # 业务侧查询
    # ==================================================================
    def overview(self, conn, q: dict) -> tuple[int, dict]:
        now_ms = int(now_epoch() * 1000)
        cutoff = now_ms - 86400 * 1000
        devices = [dict(r) for r in conn.execute("SELECT * FROM devices").fetchall()]
        for d in devices:
            d["live_state"] = self._live_state(d)
        online = sum(1 for d in devices if d["live_state"] == "online")
        frames_24h = conn.execute(
            "SELECT COUNT(*) FROM frames WHERE ts_epoch >= ?", (cutoff,)).fetchone()[0]
        volume = conn.execute(
            "SELECT COALESCE(SUM(volume_m3),0) FROM cycles WHERE ts_epoch >= ?",
            (cutoff,)).fetchone()[0]
        cycles_24h = conn.execute(
            "SELECT COUNT(*) FROM cycles WHERE ts_epoch >= ?", (cutoff,)).fetchone()[0]
        al = alarms_mod.stats(conn)
        return 200, {
            "server_time": now_utc_iso(),
            "device_total": len(devices),
            "device_online": online,
            "frames_24h": frames_24h,
            "cycles_24h": cycles_24h,
            "volume_24h": round(float(volume), 2),
            "alarms": al,
            # 工程名取自**工点档案表**，不是 `config/site.json`。
            # 那个文件是"本机身份"，终端现场改工点时会写它 —— 平台页头
            # 要是跟着它走，一台终端转场就能把整个平台改名。
            "project": self._project(conn),
            "sites": [dict(r) for r in conn.execute(
                "SELECT site_id, site_name, project_name, tunnel, line FROM sites"
                " WHERE active=1 ORDER BY site_id").fetchall()],
            "backlog_total": sum(int(d.get("backlog") or 0) for d in devices),
        }

    @staticmethod
    def _project(conn) -> dict:
        """平台页头显示的工程信息。工点表为空时退回配置文件。"""
        row = conn.execute(
            "SELECT site_id, site_name, project_name, tunnel, line FROM sites"
            " WHERE active=1 ORDER BY created_at, site_id LIMIT 1").fetchone()
        if row is None:
            return config.load_site()
        d = dict(row)
        # `name` 是页头标题用的字段，原来由 config 的 project 段提供
        d["name"] = d.get("project_name") or d.get("site_name") or ""
        return d

    def devices(self, conn, q: dict) -> tuple[int, dict]:
        out = []
        for r in conn.execute("SELECT * FROM devices ORDER BY device_code").fetchall():
            d = dict(r)
            d["live_state"] = self._live_state(d)
            d["state_label"] = {"online": "在线", "degraded": "弱网",
                                "offline": "离线"}[d["live_state"]]
            d["last_seen_local"] = fmt_local(d["last_seen"]) if d["last_seen"] else "—"
            out.append(d)
        return 200, {"devices": out, "count": len(out)}

    @staticmethod
    def _live_state(d: dict) -> str:
        """设备在线与否由**心跳新鲜度**决定，不信库里存的那个状态字段。

        原因：那字段是设备最后一次上报时写的，设备掉线后没人来改它，
        界面就会一直显示"在线"。以时间反推才可靠。
        """
        if not d.get("last_seen"):
            return "offline"
        try:
            age = now_epoch() - to_datetime(d["last_seen"]).timestamp()
        except (ValueError, TypeError):
            return "offline"
        if age > HEARTBEAT_TIMEOUT_S:
            return "offline"
        return "degraded" if d.get("link_state") == "degraded" else "online"

    def frames(self, conn, q: dict) -> tuple[int, dict]:
        dev = http_util.qs_one(q, "device_code")
        limit = min(http_util.qs_int(q, "limit", 100), 2000)
        sql = "SELECT * FROM frames"
        args: list = []
        conds = []
        if dev:
            conds.append("device_code=?")
            args.append(dev)
        frm = http_util.qs_one(q, "from")
        to = http_util.qs_one(q, "to")
        if frm:
            conds.append("ts_epoch >= ?")
            args.append(int(to_datetime(frm).timestamp() * 1000))
        if to:
            conds.append("ts_epoch <= ?")
            args.append(int(to_datetime(to).timestamp() * 1000))
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY ts_epoch DESC LIMIT ?"
        args.append(limit)
        rows = [self._frame_out(r) for r in conn.execute(sql, args).fetchall()]
        return 200, {"frames": rows, "count": len(rows)}

    def series(self, conn, q: dict) -> tuple[int, dict]:
        """曲线数据。默认取最近 10 分钟，必要时抽稀到指定点数。"""
        dev = http_util.qs_one(q, "device_code")
        keys = [k for k in http_util.qs_one(q, "params").split(",") if k]
        if not keys:
            keys = ["cylinderPumpingPressure", "pumpingSpeed", "acceleratorAddSpeed"]
        keys = [k for k in keys if k in PARAM_BY_KEY]
        if not keys:
            return 400, {"error": "参数名不合法"}
        minutes = http_util.qs_float(q, "minutes", 10.0)
        max_points = min(http_util.qs_int(q, "max_points", 120), 1000)

        since = int((now_epoch() - minutes * 60) * 1000)
        sql = ("SELECT ts_epoch, " + ",".join(keys) + ", rev_json FROM frames"
               " WHERE ts_epoch >= ?")
        args: list = [since]
        if dev:
            sql += " AND device_code=?"
            args.append(dev)
        sql += " ORDER BY ts_epoch"
        rows = conn.execute(sql, args).fetchall()

        step = max(1, len(rows) // max_points) if rows else 1
        series = {k: [] for k in keys}
        series["ts"] = []
        for i, r in enumerate(rows):
            if i % step:
                continue
            series["ts"].append(r["ts_epoch"])
            overlay = self._overlay(r["rev_json"])
            for k in keys:
                v = overlay.get(k, r[k])
                series[k].append(None if v is None else round(float(v), 4))
        reg = self.param_registry
        return 200, {
            "device_code": dev,
            # lo/hi 同样取生效阈值：前端拿它画量程参考线，
            # 画错线比不画更糟 —— 看着在线内的点其实已经报警了。
            "params": [{"key": k, "name": reg[k].name,
                        "unit": reg[k].unit,
                        "lo": reg[k].lo, "hi": reg[k].hi,
                        "decimals": reg[k].decimals,
                        "alarmable": reg[k].alarmable} for k in keys],
            "points": len(series["ts"]),
            "raw_points": len(rows),
            "series": series,
        }

    def history(self, conn, q: dict) -> tuple[int, dict]:
        """历史聚合。按小时或按天分桶，给历史页出图。"""
        dev = http_util.qs_one(q, "device_code")
        days = min(http_util.qs_int(q, "days", 7), 90)
        bucket = http_util.qs_one(q, "bucket", "hour")
        param = http_util.qs_one(q, "param", "cylinderPumpingPressure")
        if param not in PARAM_BY_KEY:
            return 400, {"error": f"未知参数 {param}"}
        width_ms = 3600_000 if bucket == "hour" else 86_400_000
        since = int((now_epoch() - days * 86400) * 1000)

        sql = (f"SELECT (ts_epoch / {width_ms}) * {width_ms} AS bucket,"
               f" COUNT(*) AS n, AVG({param}) AS avg_v, MIN({param}) AS min_v,"
               f" MAX({param}) AS max_v FROM frames WHERE ts_epoch >= ?")
        args: list = [since]
        if dev:
            sql += " AND device_code=?"
            args.append(dev)
        sql += " GROUP BY bucket ORDER BY bucket"
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]

        # 每桶再补一条报警数
        for r in rows:
            r["alarms"] = conn.execute(
                "SELECT COUNT(*) FROM alarms WHERE started_at >= ? AND started_at < ?",
                (to_datetime(r["bucket"]).isoformat(timespec="milliseconds"),
                 to_datetime(r["bucket"] + width_ms).isoformat(timespec="milliseconds"))
            ).fetchone()[0]
            for k in ("avg_v", "min_v", "max_v"):
                r[k] = None if r[k] is None else round(float(r[k]), 3)
            r["label"] = to_datetime(r["bucket"]).astimezone(CST).strftime(
                "%m-%d %H:00" if bucket == "hour" else "%Y-%m-%d")

        total = conn.execute(
            "SELECT COUNT(*) FROM frames WHERE ts_epoch >= ?", (since,)).fetchone()[0]
        return 200, {
            "bucket": bucket, "days": days, "param": param,
            "param_name": PARAM_BY_KEY[param].name,
            "unit": PARAM_BY_KEY[param].unit,
            "buckets": rows, "total_frames": total,
        }

    def alarms(self, conn, q: dict) -> tuple[int, dict]:
        dev = http_util.qs_one(q, "device_code")
        active_only = http_util.qs_one(q, "active_only") == "1"
        limit = min(http_util.qs_int(q, "limit", 200), 1000)
        rows = alarms_mod.query(conn, device_code=dev, active_only=active_only, limit=limit)
        return 200, {"alarms": rows, "count": len(rows),
                     "stats": alarms_mod.stats(conn, dev)}

    def cycles(self, conn, q: dict) -> tuple[int, dict]:
        dev = http_util.qs_one(q, "device_code")
        limit = min(http_util.qs_int(q, "limit", 100), 1000)
        sql = "SELECT * FROM cycles"
        args: list = []
        if dev:
            sql += " WHERE device_code=?"
            args.append(dev)
        sql += " ORDER BY started_at DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in conn.execute(sql, args).fetchall():
            d = dict(r)
            d["started_local"] = fmt_local(d["started_at"])
            d["ended_local"] = fmt_local(d["ended_at"]) if d["ended_at"] else "进行中"
            d["duration_s"] = (max(0, int(to_datetime(d["ended_at"]).timestamp()
                                          - to_datetime(d["started_at"]).timestamp()))
                               if d["ended_at"] else None)
            out.append(d)
        return 200, {"cycles": out, "count": len(out)}

    def batches(self, conn, q: dict) -> tuple[int, dict]:
        dev = http_util.qs_one(q, "device_code")
        limit = min(http_util.qs_int(q, "limit", 100), 1000)
        sql = "SELECT * FROM batches"
        args: list = []
        if dev:
            sql += " WHERE device_code=?"
            args.append(dev)
        sql += " ORDER BY ts_server DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in conn.execute(sql, args).fetchall():
            d = dict(r)
            d.pop("receipt_json", None)
            d["ts_server_local"] = fmt_local(d["ts_server"])
            out.append(d)
        return 200, {"batches": out, "count": len(out)}

    def revisions(self, conn, q: dict) -> tuple[int, dict]:
        dev = http_util.qs_one(q, "device_code")
        limit = min(http_util.qs_int(q, "limit", 100), 1000)
        sql = ("SELECT r.*, f.ts_device AS target_ts, f.pile_id AS pile_id,"
               " f.rev_count AS current_rev_count FROM frame_revisions r"
               " LEFT JOIN frames f ON f.record_id = r.record_id")
        args: list = []
        if dev:
            sql += " WHERE r.device_code=?"
            args.append(dev)
        sql += " ORDER BY r.ts_server DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in conn.execute(sql, args).fetchall():
            d = dict(r)
            try:
                d["patch"] = json.loads(d["patch_json"])
            except (json.JSONDecodeError, TypeError):
                d["patch"] = {}
            d["patch_text"] = "、".join(
                f"{PARAM_BY_KEY[k].name}={v}{PARAM_BY_KEY[k].unit}"
                for k, v in d["patch"].items() if k in PARAM_BY_KEY)
            d["ts_server_local"] = fmt_local(d["ts_server"])
            d["reason_label"] = ReasonCode(d["reason_code"]).label \
                if d["reason_code"] in ReasonCode._value2member_map_ else d["reason_code"]
            out.append(d)
        return 200, {"revisions": out, "count": len(out)}

    def frame_detail(self, conn, q: dict, record_id: str) -> tuple[int, dict]:
        """单帧详情：原值 / 修订 / 有效值三份并列，供"修正"标记点开看。"""
        row = conn.execute("SELECT * FROM frames WHERE record_id=?",
                           (record_id,)).fetchone()
        if row is None:
            return 404, {"error": f"记录 {record_id} 不存在"}
        d = dict(row)
        revs = []
        for r in conn.execute(
                "SELECT * FROM frame_revisions WHERE record_id=?"
                " ORDER BY ts_server, revision_id", (record_id,)).fetchall():
            item = dict(r)
            try:
                item["patch"] = json.loads(item["patch_json"])
            except (json.JSONDecodeError, TypeError):
                item["patch"] = {}
            item["ts_server_local"] = fmt_local(item["ts_server"])
            item["reason_label"] = ReasonCode(item["reason_code"]).label \
                if item["reason_code"] in ReasonCode._value2member_map_ else item["reason_code"]
            revs.append(item)
        return 200, {
            "frame": self._frame_out(d, with_raw=True),
            "revisions": revs,
            "corrected": len(revs) > 0,
        }

    def audit(self, conn, q: dict) -> tuple[int, dict]:
        dev = http_util.qs_one(q, "device_code")
        limit = min(http_util.qs_int(q, "limit", 200), 1000)
        sql = "SELECT * FROM audit_events"
        args: list = []
        if dev:
            sql += " WHERE device_code=?"
            args.append(dev)
        sql += " ORDER BY ts_epoch DESC, ts_server DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in conn.execute(sql, args).fetchall():
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload_json"])
            except (json.JSONDecodeError, TypeError):
                d["payload"] = {}
            d["ts_server_local"] = fmt_local(d["ts_server"])
            d["kind_label"] = {"usb_export": "U 盘导出", "usb_import": "U 盘导入",
                               "manual_upload": "手动补传", "revision": "数据修订",
                               "void": "记录作废", "device_reset": "终端重置",
                               "link_change": "链路切换"}.get(d["kind"], d["kind"])
            out.append(d)
        return 200, {"events": out, "count": len(out)}

    def rejects(self, conn, q: dict) -> tuple[int, dict]:
        limit = min(http_util.qs_int(q, "limit", 100), 1000)
        rows = []
        for r in conn.execute(
                "SELECT * FROM rejects ORDER BY ts_server DESC LIMIT ?", (limit,)).fetchall():
            d = dict(r)
            d["ts_server_local"] = fmt_local(d["ts_server"])
            d["reason_label"] = ReasonCode(d["reason_code"]).label \
                if d["reason_code"] in ReasonCode._value2member_map_ else d["reason_code"]
            rows.append(d)
        uniq: dict[str, int] = {}
        for r in conn.execute("SELECT reason_code, COUNT(*) c FROM rejects GROUP BY reason_code"):
            uniq[r["reason_code"]] = r["c"]
        return 200, {"rejects": rows, "count": len(rows), "by_reason": uniq}

    def params(self, conn, q: dict) -> tuple[int, dict]:
        # 发**生效**的注册表（阈值覆盖已合并），不是模块里的默认值 ——
        # 界面上的"正常范围"必须和真正触发报警的那条线是同一条。
        reg = self.param_registry
        return 200, {
            "params": as_dicts([reg[k] for k in PARAM_KEYS]),
            "thresholds": self.thresholds,
        }

    def export_csv(self, conn, q: dict) -> tuple[int, str]:
        """导出帧数据为 CSV（utf-8-sig，Excel 直接打开不乱码）。"""
        dev = http_util.qs_one(q, "device_code")
        limit = min(http_util.qs_int(q, "limit", 5000), 50000)
        keys = [k for k in http_util.qs_one(q, "params").split(",") if k in PARAM_BY_KEY]
        sql = "SELECT * FROM frames"
        args: list = []
        if dev:
            sql += " WHERE device_code=?"
            args.append(dev)
        sql += " ORDER BY ts_epoch DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()

        buf = io.StringIO()
        head = ["record_id", "device_code", "ts_local", "ts_device", "pile_id", "quality",
                "source", "rev_count"] + keys
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(head)
        for r in rows:
            overlay = self._overlay(r["rev_json"])
            w.writerow([r["record_id"], r["device_code"], fmt_local(r["ts_device"]),
                        r["ts_device"], r["pile_id"], r["quality"], r["source"],
                        r["rev_count"]]
                       + [overlay.get(k, r[k]) for k in keys])
        return 200, "﻿" + buf.getvalue()

    # ---- 输出转换 ----
    @staticmethod
    def _overlay(rev_json: Any) -> dict:
        """解析修订投影。空值返回空 dict。"""
        if not rev_json:
            return {}
        try:
            d = json.loads(rev_json)
            return d if isinstance(d, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _frame_out(self, row: sqlite3.Row, with_raw: bool = False) -> dict:
        """把一行帧转成给前端的结构：`values` 是有效值，`raw` 是原始值。"""
        d = dict(row)
        raw = {k: d.get(k) for k in PARAM_KEYS}
        overlay = self._overlay(d.get("rev_json"))
        effective = {k: (overlay.get(k, raw[k])) for k in PARAM_KEYS}
        out: dict[str, Any] = {
            "record_id": d["record_id"],
            "device_code": d["device_code"],
            "ts_device": d["ts_device"],
            "ts_epoch": d["ts_epoch"],
            "ts_local": fmt_local(d["ts_device"]),
            "pile_id": d["pile_id"],
            "quality": d["quality"],
            "source": d["source"],
            "rev_count": d.get("rev_count") or 0,
            "corrected": (d.get("rev_count") or 0) > 0,
            "values": effective,
            "display": {k: PARAM_BY_KEY[k].format(effective[k]) for k in PARAM_KEYS},
        }
        for k in PARAM_KEYS:
            out[k] = effective[k]
        if with_raw:
            out["raw"] = raw
        return out


# ==================================================================
# 路由表
# ==================================================================
def dispatch(api: Api, conn: sqlite3.Connection, method: str,
             path: str, q: dict, body: dict | None) -> tuple[int, Any, str]:
    """返回 (状态码, 响应体, 内容类型)。响应体是 dict 时按 JSON 发，bytes 原样发。"""
    if path == "/api/health":
        status, payload = api.health(conn)
        if api.stopping:
            payload = dict(payload, stopping=True)
        return (status, payload, "json")
    if path == "/api/piles":
        return (*api.piles(conn, q), "json")
    if path == "/api/identity/check":
        return (*api.identity_check(conn, q), "json")
    if path == "/api/params":
        return (*api.params(conn, q), "json")

    if method == "POST":
        payload = body or {}
        if path == "/api/shutdown":
            _shutdown_platform(api, broadcast=bool((body or {}).get("broadcast")))
            return 200, {"stopping": True}, "json"
        if path == "/api/heartbeat":
            return (*api.heartbeat(conn, payload), "json")
        if path == "/api/upload":
            return (*api.upload(conn, payload), "json")
        return 404, {"error": f"未知接口 POST {path}"}, "json"

    if path == "/api/overview":
        status, payload = api.overview(conn, q)
        if api.stopping:
            payload = dict(payload, stopping=True)
        return (status, payload, "json")
    if path == "/api/devices":
        return (*api.devices(conn, q), "json")
    if path == "/api/frames":
        return (*api.frames(conn, q), "json")
    if path == "/api/series":
        return (*api.series(conn, q), "json")
    if path == "/api/history":
        return (*api.history(conn, q), "json")
    if path == "/api/alarms":
        return (*api.alarms(conn, q), "json")
    if path == "/api/cycles":
        return (*api.cycles(conn, q), "json")
    if path == "/api/batches":
        return (*api.batches(conn, q), "json")
    if path == "/api/revisions":
        return (*api.revisions(conn, q), "json")
    if path == "/api/audit":
        return (*api.audit(conn, q), "json")
    if path == "/api/rejects":
        return (*api.rejects(conn, q), "json")
    if path == "/api/export.csv":
        status, data = api.export_csv(conn, q)
        return status, data, "csv"
    if path.startswith("/api/frame/"):
        rid = path[len("/api/frame/"):]
        return (*api.frame_detail(conn, q, rid), "json")

    return 404, {"error": f"未知接口 {method} {path}"}, "json"


def _shutdown_platform(api, broadcast: bool = False) -> None:
    """一键关闭：页面先看到 stopping 标记并关闭，再走 main._bye 收尾退出。"""
    api.stopping = True              # overview / health 立刻带回给页面
    import signal as _sig
    import threading as _th
    import time as _t

    def _bye():
        try:
            if not broadcast:                  # 通知终端也置标记、按同样节奏退出
                from common import config as _cfg
                from common import http_util as _hu
                _hu.post_json(_cfg.url_base("terminal", api.cfg) + "/api/shutdown",
                              {"broadcast": True}, timeout=2)
        except Exception:
            pass                               # 终端不在了也无妨，run_all 会兜底
        _t.sleep(4.0)                          # 页面轮询最长 3s，留足看到标记的时间
        try:
            _sig.raise_signal(_sig.SIGBREAK)   # 触发 main 里装好的信号处理
        except Exception:
            import os as _os
            _os._exit(0)                       # 兜底：数据在 sqlite，WAL 可恢复
    _th.Thread(target=_bye, name="shutdown", daemon=True).start()
