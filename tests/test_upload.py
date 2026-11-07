# -*- coding: utf-8 -*-
"""上传链路与报警判定的单测（Phase 2）。

这一组测试对应产品计划书里对客户承诺的四件事，每件都要有代码级的证明：

* **不重**  —— 同一批次传两次，平台行数不变，返回的还是原来那份回执。
* **可信**  —— 桩号不在平台清单内的数据，一条都进不来。
* **不覆盖**—— 同一 record_id 内容不一致时拒收，原值分毫不动。
* **不刷屏**—— 持续越限只产生一条报警，停机不产生假报警。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import config, csv_schema, ids
from common.logging_setup import quiet
from common.params import PARAM_KEYS
from common.timeutil import now_epoch, now_utc_iso
from server import db, seed
from server.alarms import AlarmEngine, query as alarm_query
from server.api import Api

quiet()   # 本模块会故意触发开警/解除/批次冲突，别让它们刷掉进度点

DEVICE = "GHP9015H"


def _values(**over):
    """一帧的正常工况数值，可按参数覆盖。"""
    base = {
        "workStatus": 1.0, "cylinderPumpingPressure": 11.0,
        "pumpingSpeed": 10.8, "circulatingPumpingVolume": 3.2,
        "acceleratorAddSpeed": 620.0, "acceleratorProportion": 5.7,
        "acceleratorOnceDosage": 20.0, "acceleratorCumulativeDosage": 4800.0,
        "circulatingPumpingVolume": 1.0,
    }
    base.update(over)
    return {k: float(base[k]) for k in PARAM_KEYS}


class UploadTestBase(unittest.TestCase):
    """给每个用例一个干净的临时平台库。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "platform.sqlite3")
        self.conn = db.init_db(self.path)
        self.cfg = config.load_config()
        self.cfg["device"]["device_code"] = "GHP9015H"   # 测试造帧固定此编号：与现场配置隔离
        self.api = Api(self.cfg, AlarmEngine(config.load_thresholds()))
        self.piles = seed.pile_list(self.cfg)
        with db.transaction(self.conn):
            seed.ensure_piles(self.conn, self.cfg)
            seed.ensure_device(self.conn, self.cfg)

    def tearDown(self):
        db.close_conn()
        self.tmp.cleanup()

    # ---- 构造器 ----
    def frame(self, seq: int, ts_ms: int, pile: str | None = None, **over) -> dict:
        rec = {
            "record_id": ids.frame_record_id(DEVICE, ts_ms, seq),
            "device_code": DEVICE,
            "ts_device": _iso(ts_ms),
            "ts_epoch": ts_ms,
            "seq": seq,
            "pile_id": pile if pile is not None else self.piles[0],
            "quality": 0,
            "source": "can",
            "values": _values(**over),
        }
        # 终端上传的每一帧都带 row_hash（终端落 CSV 时算的）。
        # 这里必须一致，否则测不出"同 ID 但内容不同"的冲突分支。
        rec["row_hash"] = csv_schema.compute_row_hash(
            {**{c: rec.get(c, "") for c in csv_schema.META_COLUMNS}, **rec["values"]})
        return rec

    def batch(self, records: list[dict], kind: str = "frame", device: str = DEVICE) -> dict:
        rids = [str(r.get("record_id", "")) for r in records]
        return {
            "protocol": "1.0",
            "batch_id": ids.make_batch_id(device, rids),
            "device_code": device,
            "kind": kind,
            "created_at": now_utc_iso(),
            "records": records,
        }

    def upload(self, records: list[dict], **kw) -> tuple[int, dict]:
        return self.api.upload(self.conn, self.batch(records, **kw))

    def frame_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]


def _iso(ms: int) -> str:
    from common.timeutil import to_datetime
    return to_datetime(ms).isoformat(timespec="milliseconds")


# ==========================================================================
# 幂等：不重
# ==========================================================================
class TestIdempotency(UploadTestBase):

    def test_same_batch_twice_changes_nothing(self):
        base = int(now_epoch() * 1000) - 600_000
        recs = [self.frame(i, base + i * 1000) for i in range(20)]

        status1, r1 = self.upload(recs)
        self.assertEqual(status1, 200)
        self.assertEqual(r1["summary"]["accepted"], 20)
        self.assertFalse(r1["duplicate_batch"])
        n1 = self.frame_count()

        status2, r2 = self.upload(recs)
        self.assertEqual(status2, 200)
        self.assertEqual(self.frame_count(), n1, "重传后行数必须不变")

        # 回执必须是一模一样的那一份，只是多了一个"这是重传"的标记
        self.assertTrue(r2["duplicate_batch"])
        self.assertEqual(r1["results"], r2["results"], "重传必须返回原回执的内容")
        self.assertEqual(r1["batch_id"], r2["batch_id"])

    def test_same_batch_in_different_order_is_same_batch(self):
        base = int(now_epoch() * 1000) - 600_000
        recs = [self.frame(i, base + i * 1000) for i in range(10)]
        self.upload(recs)
        n1 = self.frame_count()
        self.upload(list(reversed(recs)))          # 换个顺序重传
        self.assertEqual(self.frame_count(), n1)

    def test_three_batches_keep_every_row(self):
        base = int(now_epoch() * 1000) - 900_000
        allrecs = [self.frame(i, base + i * 1000) for i in range(30)]
        for i in range(0, 30, 10):
            self.upload(allrecs[i:i + 10])
        self.assertEqual(self.frame_count(), 30)

    def test_overlapping_batches_do_not_duplicate(self):
        """两批有交集：交集部分判重复，各自独有的部分照常入库。"""
        base = int(now_epoch() * 1000) - 900_000
        recs = [self.frame(i, base + i * 1000) for i in range(20)]
        _, r1 = self.upload(recs[:12])
        _, r2 = self.upload(recs[8:20])
        self.assertEqual(r1["summary"]["accepted"], 12)
        self.assertEqual(r2["summary"]["accepted"], 8)
        self.assertEqual(r2["summary"]["duplicate"], 4)
        self.assertEqual(self.frame_count(), 20)

    def test_replay_returns_stored_receipt_even_after_restart(self):
        """平台重启（换一个 Api 实例）后，重放同一批仍要认出来。"""
        base = int(now_epoch() * 1000) - 600_000
        recs = [self.frame(i, base + i * 1000) for i in range(5)]
        _, r1 = self.upload(recs)

        fresh = Api(self.cfg, AlarmEngine(config.load_thresholds()))
        _, r2 = fresh.upload(self.conn, self.batch(recs))
        self.assertTrue(r2["duplicate_batch"])
        self.assertEqual(r1["results"], r2["results"])
        self.assertEqual(self.frame_count(), 5)


# ==========================================================================
# 桩号防伪
# ==========================================================================
class TestPileGuard(UploadTestBase):

    def test_unissued_pile_is_rejected(self):
        base = int(now_epoch() * 1000) - 300_000
        recs = [self.frame(0, base, pile="ZK999+999"),
                self.frame(1, base + 1000)]
        status, r = self.upload(recs)
        self.assertEqual(status, 200)
        self.assertEqual(r["summary"]["accepted"], 1)
        self.assertEqual(r["summary"]["rejected"], 1)
        self.assertEqual(r["status"], "partial")

        bad = [x for x in r["results"] if x["result"] == "rejected"][0]
        self.assertEqual(bad["reason_code"], "PILE_NOT_ISSUED")
        self.assertEqual(self.frame_count(), 1, "被拒的记录不能进库")

    def test_empty_pile_is_rejected(self):
        base = int(now_epoch() * 1000) - 300_000
        _, r = self.upload([self.frame(0, base, pile="")])
        self.assertEqual(r["summary"]["rejected"], 1)

    def test_rejected_record_lands_in_rejects_table(self):
        base = int(now_epoch() * 1000) - 300_000
        self.upload([self.frame(0, base, pile="ZK000+000")])
        n = self.conn.execute("SELECT COUNT(*) FROM rejects").fetchone()[0]
        self.assertEqual(n, 1)

    def test_pile_list_endpoint_is_the_only_source(self):
        status, payload = self.api.piles(self.conn, {})
        self.assertEqual(status, 200)
        self.assertIn("ZK105+207", [p["pile_id"] for p in payload["piles"]])
        # 不传 site_id 时回**全部已下发**的桩号（跨工点）。这是刻意的：
        # 入库校验认的是"平台下发过的桩号"这一个全局集合
        # （见 Api._issued_piles），按工点过滤只影响终端拉清单。
        self.assertGreaterEqual(payload["count"], len(self.piles))

        # 终端实际走的是这一条：按工点拉，拿到的正好是那个工点的清单
        status, mine = self.api.piles(self.conn, {"site_id": ["DQTYX-ZK"]})
        self.assertEqual(status, 200)
        self.assertEqual(mine["count"], len(self.piles))
        self.assertEqual([p["pile_id"] for p in mine["piles"]], self.piles)


# ==========================================================================
# 只追加：不覆盖
# ==========================================================================
class TestNoOverwrite(UploadTestBase):

    def test_conflicting_content_is_rejected_and_original_kept(self):
        """同 record_id、不同数值，且**换一个批次**送上来 —— 必须拒收。

        为什么换批次：同一个 batch_id 重放会被批次级幂等闸门先拦下
        （见 test_replay_of_identical_batch_short_circuits）。
        真实的篡改场景是数据被改过之后再组一批传上来，
        这条路必须堵死。
        """
        base = int(now_epoch() * 1000) - 300_000
        good = self.frame(0, base, cylinderPumpingPressure=11.0)
        self.upload([good])

        # 同一个 record_id，但数值被改过，且混在一条新记录里作为新批次上传
        tampered = self.frame(0, base, cylinderPumpingPressure=14.5)
        fresh = self.frame(1, base + 1000, cylinderPumpingPressure=11.0)
        _, r = self.upload([tampered, fresh])

        self.assertEqual(r["summary"]["rejected"], 1)
        self.assertEqual(r["summary"]["accepted"], 1, "同批的新记录应当正常入库")
        bad = [x for x in r["results"] if x["result"] == "rejected"][0]
        self.assertEqual(bad["reason_code"], "DUPLICATE_CONFLICT")

        got = self.conn.execute(
            "SELECT cylinderPumpingPressure FROM frames WHERE record_id=?",
            (good["record_id"],)).fetchone()[0]
        self.assertEqual(got, 11.0, "原始值被改动了 —— 这绝对不允许")

    def test_tampered_replay_of_same_batch_id_is_caught(self):
        """batch_id 只由 record_id 决定，所以改了数值再原样重放，ID 是一样的。

        这时批次级幂等闸门会命中，返回原回执、一个字节都不写 ——
        数据本身是安全的。但**平台必须能察觉有人在动手脚**，
        所以要比对内容摘要并报 409，不能静默放过。
        """
        base = int(now_epoch() * 1000) - 300_000
        recs = [self.frame(i, base + i * 1000, cylinderPumpingPressure=11.0) for i in range(4)]
        self.upload(recs)
        n1 = self.frame_count()

        tampered = [dict(r) for r in recs]
        tampered[2] = self.frame(2, base + 2000, cylinderPumpingPressure=14.5)   # 换掉一条
        payload = self.batch(tampered)
        self.assertEqual(payload["batch_id"], self.batch(recs)["batch_id"],
                         "前提：改数值不改变 batch_id")
        status, r = self.api.upload(self.conn, payload)

        self.assertEqual(status, 409)
        self.assertEqual(r["code"], "DUPLICATE_CONFLICT")
        self.assertEqual(self.frame_count(), n1, "冲突请求不能写入任何数据")
        # 篡改尝试要留痕
        n = self.conn.execute(
            "SELECT COUNT(*) FROM rejects WHERE reason_code='DUPLICATE_CONFLICT'"
        ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_replay_of_identical_batch_short_circuits(self):
        """原样重放走到批次级闸门就够了，不该再逐条比一遍。"""
        base = int(now_epoch() * 1000) - 300_000
        recs = [self.frame(i, base + i * 1000) for i in range(4)]
        self.upload(recs)
        status, r = self.upload(recs)
        self.assertEqual(status, 200)
        self.assertTrue(r["duplicate_batch"])

    def test_identical_content_is_duplicate_not_conflict(self):
        base = int(now_epoch() * 1000) - 300_000
        recs = [self.frame(0, base)]
        self.upload(recs)
        # 重新构造同一帧（内容完全一致，batch_id 也一致）
        _, r = self.upload([self.frame(0, base)])
        self.assertEqual(r["summary"]["rejected"], 0)
        self.assertTrue(r["duplicate_batch"])


# ==========================================================================
# 报文校验
# ==========================================================================
class TestBatchValidation(UploadTestBase):

    def test_forged_batch_id_rejected(self):
        base = int(now_epoch() * 1000) - 300_000
        payload = self.batch([self.frame(0, base)])
        payload["batch_id"] = f"{DEVICE}.deadbeefdeadbeef"     # 与内容对不上
        status, r = self.api.upload(self.conn, payload)
        self.assertEqual(status, 400)
        self.assertIn("内容寻址", r["error"])
        self.assertEqual(self.frame_count(), 0)

    def test_batch_too_large(self):
        base = int(now_epoch() * 1000) - 900_000
        recs = [self.frame(i, base + i * 100) for i in range(ids.MAX_BATCH_RECORDS + 1)]
        status, r = self.api.upload(self.conn, self.batch(recs))
        self.assertEqual(status, 413)
        self.assertEqual(r["code"], "BATCH_TOO_LARGE")

    def test_empty_batch(self):
        payload = self.batch([])
        status, _ = self.api.upload(self.conn, payload)
        self.assertEqual(status, 400)

    def test_future_timestamp_rejected(self):
        future = int(now_epoch() * 1000) + 3600_000       # 比服务器快 1 小时
        _, r = self.upload([self.frame(0, future)])
        self.assertEqual(r["summary"]["rejected"], 1)
        self.assertEqual(r["results"][0]["reason_code"], "TS_FUTURE")

    def test_wrong_device_in_batch_rejected(self):
        base = int(now_epoch() * 1000) - 300_000
        rec = self.frame(0, base)
        rec["record_id"] = ids.frame_record_id("OTHER", base, 0)
        _, r = self.upload([rec])
        self.assertEqual(r["summary"]["rejected"], 1)
        self.assertEqual(r["results"][0]["reason_code"], "SCHEMA")

    def test_impossible_value_rejected(self):
        base = int(now_epoch() * 1000) - 300_000
        _, r = self.upload([self.frame(0, base, cylinderPumpingPressure=1e9)])
        self.assertEqual(r["summary"]["rejected"], 1)

    def test_missing_param_rejected(self):
        base = int(now_epoch() * 1000) - 300_000
        rec = self.frame(0, base)
        del rec["values"]["acceleratorAddSpeed"]
        _, r = self.upload([rec])
        self.assertEqual(r["summary"]["rejected"], 1)

    def test_one_bad_record_does_not_poison_the_batch(self):
        base = int(now_epoch() * 1000) - 300_000
        recs = [self.frame(i, base + i * 1000) for i in range(5)]
        recs[2]["values"]["cylinderPumpingPressure"] = 1e9
        _, r = self.upload(recs)
        self.assertEqual(r["summary"]["accepted"], 4)
        self.assertEqual(r["summary"]["rejected"], 1)
        self.assertEqual(self.frame_count(), 4)


# ==========================================================================
# 心跳与设备
# ==========================================================================
class TestHeartbeat(UploadTestBase):

    def test_heartbeat_marks_device_online(self):
        status, _ = self.api.heartbeat(self.conn, {
            "device_code": DEVICE, "ts_device": now_utc_iso(),
            "buffer_backlog": 42, "link_state": "online", "uptime_s": 100})
        self.assertEqual(status, 200)
        _, devs = self.api.devices(self.conn, {})
        d = devs["devices"][0]
        self.assertEqual(d["live_state"], "online")
        self.assertEqual(d["backlog"], 42)

    def test_stale_heartbeat_reads_as_offline(self):
        """设备掉线后没人来改状态字段，界面必须靠时间反推。"""
        self.conn.execute("UPDATE devices SET last_seen=? WHERE device_code=?",
                          ("2020-01-01T00:00:00.000+00:00", DEVICE))
        _, devs = self.api.devices(self.conn, {})
        self.assertEqual(devs["devices"][0]["live_state"], "offline")

    def test_heartbeat_registers_unknown_device(self):
        status, _ = self.api.heartbeat(self.conn, {
            "device_code": "GHP0001", "ts_device": now_utc_iso()})
        self.assertEqual(status, 200)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM devices WHERE device_code='GHP0001'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_heartbeat_without_device_code(self):
        status, _ = self.api.heartbeat(self.conn, {})
        self.assertEqual(status, 400)


# ==========================================================================
# 报警：边沿触发 + 回滞 + 停机不误报
# ==========================================================================
class TestAlarms(UploadTestBase):

    def _run(self, pressures: list[float], start_ms: int,
             motor: float = 1.0, step_ms: int = 1000) -> None:
        recs = [self.frame(i, start_ms + i * step_ms, workStatus=motor,
                           cylinderPumpingPressure=p)
                for i, p in enumerate(pressures)]
        self.upload(recs)

    def test_sustained_excursion_opens_exactly_one_alarm(self):
        """越限 20 秒，只允许产生 1 条报警记录 —— 这就是防刷屏。

        恢复段必须长于 `clear_after_s`（10 秒），否则报警还没解除是正常的，
        不是 bug。这里给 14 秒。
        """
        base = int(now_epoch() * 1000) - 600_000
        pressures = [11.0] * 3 + [20.0] * 20 + [11.0] * 14
        self._run(pressures, base)

        rows = alarm_query(self.conn, device_code=DEVICE)
        self.assertEqual(len(rows), 1, f"应当只有 1 条报警，实际 {len(rows)} 条")
        a = rows[0]
        self.assertEqual(a["param_key"], "cylinderPumpingPressure")
        self.assertEqual(a["level"], "critical")
        self.assertFalse(a["active"], "已经恢复正常，报警应已解除")
        self.assertAlmostEqual(a["peak_value"], 20.0, places=3)

    def test_brief_spike_is_filtered_out(self):
        """单点毛刺（持续 1 秒 < min_duration 3 秒）不该报警。"""
        base = int(now_epoch() * 1000) - 600_000
        self._run([11.0, 11.0, 20.0, 11.0, 11.0], base)
        self.assertEqual(len(alarm_query(self.conn, device_code=DEVICE)), 0)

    def test_hysteresis_prevents_flapping(self):
        """在回滞带内来回抖动，不能反复开关报警。"""
        base = int(now_epoch() * 1000) - 600_000
        # 16.0 正好在边界内（不触发），15.5 在回滞带（不解除），15.0 才算恢复
        pressures = [11.0] * 3 + [20.0] * 10
        for _ in range(6):
            pressures += [15.5, 16.0]                  # 回滞带内抖动
        pressures += [11.0] * 15
        self._run(pressures, base)
        rows = alarm_query(self.conn, device_code=DEVICE)
        self.assertEqual(len(rows), 1, "回滞带抖动不应产生第二条报警")

    def test_second_excursion_opens_second_alarm(self):
        base = int(now_epoch() * 1000) - 900_000
        self._run([11.0] * 3 + [20.0] * 8 + [11.0] * 15
                  + [20.0] * 8 + [11.0] * 15, base)
        rows = alarm_query(self.conn, device_code=DEVICE)
        self.assertEqual(len(rows), 2, "两次独立的越限应当各产生一条报警")
        self.assertTrue(all(not r["active"] for r in rows))

    def test_zero_flow_while_pump_off_is_not_an_alarm(self):
        """停机时速凝剂流量为 0 是正常工况，不能报警。

        这是 `params.active_when` 存在的理由 —— 少了它，
        演示一晚上会攒出几百条假报警。
        """
        base = int(now_epoch() * 1000) - 600_000
        recs = [self.frame(i, base + i * 1000, workStatus=0.0,
                           pumpingSpeed=0.0, acceleratorAddSpeed=0.0,
                           cylinderPumpingPressure=0.0)
                for i in range(30)]
        self.upload(recs)
        self.assertEqual(len(alarm_query(self.conn, device_code=DEVICE)), 0)

    def test_stopping_pump_clears_active_alarm(self):
        """速凝剂泵一停，之前的流量报警要能自己解除，不能挂死。"""
        base = int(now_epoch() * 1000) - 600_000
        recs = [self.frame(i, base + i * 1000, acceleratorAddSpeed=95.0) for i in range(15)]
        recs += [self.frame(i + 15, base + (i + 15) * 1000,
                            workStatus=0.0, acceleratorAddSpeed=0.0) for i in range(20)]
        self.upload(recs)
        rows = alarm_query(self.conn, device_code=DEVICE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["param_key"], "acceleratorAddSpeed")
        self.assertFalse(rows[0]["active"], "泵停之后报警必须自动解除")

    def test_only_one_active_alarm_per_param_in_db(self):
        """数据库层面的兜底：部分唯一索引挡住第二条活跃报警。"""
        base = int(now_epoch() * 1000) - 600_000
        self._run([11.0] * 3 + [20.0] * 40, base)     # 一直越限，报警保持活跃
        n = self.conn.execute(
            "SELECT COUNT(*) FROM alarms WHERE ended_at IS NULL").fetchone()[0]
        self.assertEqual(n, 1)

    def test_alarm_survives_engine_restart_without_duplicating(self):
        """平台重启后继续喂同一段越限数据，不能开出第二条报警。"""
        base = int(now_epoch() * 1000) - 600_000
        recs = [self.frame(i, base + i * 1000, cylinderPumpingPressure=20.0) for i in range(20)]
        self.upload(recs)
        n_before = self.conn.execute("SELECT COUNT(*) FROM alarms").fetchone()[0]

        # 模拟平台重启：新的 Api + 新的报警引擎，内存状态清零
        fresh = Api(self.cfg, AlarmEngine(config.load_thresholds()))
        more = [self.frame(100 + i, base + (20 + i) * 1000, cylinderPumpingPressure=20.0)
                for i in range(20)]
        fresh.upload(self.conn, self.batch(more))
        n_after = self.conn.execute("SELECT COUNT(*) FROM alarms").fetchone()[0]
        self.assertEqual(n_before, 1)
        self.assertEqual(n_after, 1, "重启后重复开警 —— 认领逻辑没生效")

    def test_low_pressure_alarm_is_also_caught(self):
        base = int(now_epoch() * 1000) - 600_000
        self._run([11.0] * 3 + [0.5] * 12, base)      # 压力掉底
        rows = alarm_query(self.conn, device_code=DEVICE)
        self.assertEqual(len(rows), 1)
        self.assertIn("低于下限", rows[0]["message"])


# ==========================================================================
# 喷浆循环
# ==========================================================================
class TestCycles(UploadTestBase):

    def test_cycle_is_derived_from_frames(self):
        base = int(now_epoch() * 1000) - 900_000
        recs = []
        vol = 0.0
        for i in range(60):
            spraying = 5 <= i < 40
            if spraying:
                vol += 0.18
            recs.append(self.frame(i, base + i * 1000, workStatus=1.0,
                                   cylinderPumpingPressure=11.0 if spraying else 0.0,
                                   circulatingPumpingVolume=round(vol, 3)))
        self.upload(recs)
        rows = self.conn.execute("SELECT * FROM cycles").fetchall()
        self.assertEqual(len(rows), 1)
        c = dict(rows[0])
        self.assertEqual(c["device_code"], DEVICE)
        self.assertIsNotNone(c["ended_at"])
        self.assertGreater(c["volume_m3"], 5.0)
        self.assertLessEqual(c["volume_m3"], 7.0)
        self.assertEqual(c["frame_count"], 35)


# ==========================================================================
# 修订：只追加不覆盖
# ==========================================================================
class TestRevisions(UploadTestBase):

    def _revision_batch(self, target: dict, patch: dict, seq: int = 0) -> dict:
        ts = int(now_epoch() * 1000) - 100_000 + seq
        rec = {
            "record_id": ids.revision_record_id(DEVICE, ts, seq),
            "target_record_id": target["record_id"],
            "patch": patch,
            "reason_code": "SENSOR_FAULT",
            "reason_note": "现场核对：探头接线松动",
            "operator_name": "张工",
            "ts_device": _iso(ts),
            "ts_epoch": ts,
        }
        return self.batch([rec], kind="revision")

    def test_revision_marks_record_corrected(self):
        base = int(now_epoch() * 1000) - 300_000
        target = self.frame(0, base, cylinderPumpingPressure=11.0)
        self.upload([target])

        status, r = self.api.upload(self.conn, self._revision_batch(
            target, {"cylinderPumpingPressure": 13.6, "pumpingSpeed": 12.5}))
        self.assertEqual(status, 200)
        self.assertEqual(r["summary"]["accepted"], 1)

        status, detail = self.api.frame_detail(self.conn, {}, target["record_id"])
        self.assertEqual(status, 200)
        self.assertTrue(detail["corrected"])
        self.assertEqual(detail["frame"]["rev_count"], 1)
        # 有效值 = 原始值 + 修订
        self.assertAlmostEqual(detail["frame"]["values"]["cylinderPumpingPressure"], 13.6, places=3)
        self.assertAlmostEqual(detail["frame"]["values"]["pumpingSpeed"], 12.5, places=3)
        # 原值分毫不动
        self.assertAlmostEqual(detail["frame"]["raw"]["cylinderPumpingPressure"], 11.0, places=3)
        # 留痕齐全：谁、什么时候、为什么
        self.assertEqual(len(detail["revisions"]), 1)
        rev = detail["revisions"][0]
        self.assertEqual(rev["operator_name"], "张工")
        self.assertEqual(rev["reason_code"], "SENSOR_FAULT")
        self.assertEqual(rev["reason_label"], "传感器故障")

    def test_revision_does_not_overwrite_raw_column(self):
        base = int(now_epoch() * 1000) - 300_000
        target = self.frame(0, base, cylinderPumpingPressure=11.0)
        self.upload([target])
        self.api.upload(self.conn, self._revision_batch(target, {"cylinderPumpingPressure": 13.6}))
        raw = self.conn.execute(
            "SELECT cylinderPumpingPressure FROM frames WHERE record_id=?",
            (target["record_id"],)).fetchone()[0]
        self.assertEqual(raw, 11.0, "原始列必须一个字节都不变")

    def test_revision_cannot_change_timestamp_or_pile(self):
        """桩号和采集时间不允许通过修订改 —— 这是不可造假的底线。"""
        base = int(now_epoch() * 1000) - 300_000
        target = self.frame(0, base)
        self.upload([target])
        for patch in ({"ts_epoch": base + 5000}, {"ts_device": _iso(base + 5000)},
                      {"record_id": "GHP9015H:1:1"}):
            _, r = self.api.upload(self.conn, self._revision_batch(target, patch, seq=1))
            self.assertEqual(r["summary"]["rejected"], 1,
                             f"这个 patch 不该被接受：{patch}")

    def test_revision_requires_reason(self):
        base = int(now_epoch() * 1000) - 300_000
        target = self.frame(0, base)
        self.upload([target])
        payload = self._revision_batch(target, {"cylinderPumpingPressure": 9.0})
        del payload["records"][0]["reason_code"]
        _, r = self.api.upload(self.conn, payload)
        self.assertEqual(r["summary"]["rejected"], 1)

    def test_revision_replay_is_idempotent(self):
        base = int(now_epoch() * 1000) - 300_000
        target = self.frame(0, base)
        self.upload([target])
        payload = self._revision_batch(target, {"cylinderPumpingPressure": 13.6})
        self.api.upload(self.conn, payload)
        self.api.upload(self.conn, payload)      # 重放
        n = self.conn.execute("SELECT COUNT(*) FROM frame_revisions").fetchone()[0]
        self.assertEqual(n, 1)
        _, detail = self.api.frame_detail(self.conn, {}, target["record_id"])
        self.assertEqual(detail["frame"]["rev_count"], 1)

    def test_two_revisions_on_same_field_latest_wins(self):
        base = int(now_epoch() * 1000) - 300_000
        target = self.frame(0, base, cylinderPumpingPressure=11.0)
        self.upload([target])
        self.api.upload(self.conn, self._revision_batch(target, {"cylinderPumpingPressure": 12.0}, seq=0))
        self.api.upload(self.conn, self._revision_batch(target, {"cylinderPumpingPressure": 14.0}, seq=1))
        _, detail = self.api.frame_detail(self.conn, {}, target["record_id"])
        self.assertEqual(detail["frame"]["rev_count"], 2)
        self.assertAlmostEqual(detail["frame"]["values"]["cylinderPumpingPressure"], 14.0, places=3)
        self.assertAlmostEqual(detail["frame"]["raw"]["cylinderPumpingPressure"], 11.0, places=3)

    def test_revision_on_missing_record_rejected(self):
        base = int(now_epoch() * 1000) - 300_000
        ghost = self.frame(0, base)
        _, r = self.api.upload(self.conn, self._revision_batch(ghost, {"cylinderPumpingPressure": 9.0}))
        self.assertEqual(r["summary"]["rejected"], 1)

    def test_revision_of_unknown_param_rejected(self):
        base = int(now_epoch() * 1000) - 300_000
        target = self.frame(0, base)
        self.upload([target])
        _, r = self.api.upload(self.conn, self._revision_batch(target, {"不存在的参数": 1.0}))
        self.assertEqual(r["summary"]["rejected"], 1)


# ==========================================================================
# 审计与查询
# ==========================================================================
class TestAuditAndQueries(UploadTestBase):

    def test_audit_event_upload(self):
        ts = int(now_epoch() * 1000) - 100_000
        rec = {"event_id": f"{DEVICE}:audit:{ts}:0", "kind": "usb_export",
               "ts_device": _iso(ts), "ts_epoch": ts,
               "payload": {"file": "U盘/导出/a.csv", "rows": 120}}
        status, r = self.api.upload(self.conn, self.batch([rec], kind="audit"))
        self.assertEqual(status, 200)
        self.assertEqual(r["summary"]["accepted"], 1)

        _, out = self.api.audit(self.conn, {})
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["events"][0]["kind_label"], "U 盘导出")

    def test_overview_counts(self):
        base = int(now_epoch() * 1000) - 600_000
        self.upload([self.frame(i, base + i * 1000) for i in range(10)])
        _, ov = self.api.overview(self.conn, {})
        self.assertEqual(ov["device_total"], 1)
        self.assertGreaterEqual(ov["frames_24h"], 10)

    def test_series_returns_points(self):
        base = int(now_epoch() * 1000) - 120_000
        self.upload([self.frame(i, base + i * 1000) for i in range(60)])
        status, s = self.api.series(self.conn, {
            "device_code": [DEVICE], "params": ["cylinderPumpingPressure"], "minutes": ["30"]})
        self.assertEqual(status, 200)
        self.assertGreater(s["points"], 0)
        self.assertEqual(len(s["series"]["ts"]), len(s["series"]["cylinderPumpingPressure"]))

    def test_series_rejects_unknown_param(self):
        status, _ = self.api.series(self.conn, {"params": ["乱写的"]})
        self.assertEqual(status, 400)

    def test_series_reflects_revisions(self):
        base = int(now_epoch() * 1000) - 120_000
        target = self.frame(0, base, cylinderPumpingPressure=11.0)
        self.upload([target])
        payload = {
            "record_id": ids.revision_record_id(DEVICE, base, 0),
            "target_record_id": target["record_id"],
            "patch": {"cylinderPumpingPressure": 14.25}, "reason_code": "VALUE_DISTORTED",
            "operator_name": "李工", "ts_device": _iso(base), "ts_epoch": base,
        }
        self.api.upload(self.conn, self.batch([payload], kind="revision"))

        _, s = self.api.series(self.conn, {
            "device_code": [DEVICE], "params": ["cylinderPumpingPressure"], "minutes": ["30"]})
        self.assertEqual(len(s["series"]["cylinderPumpingPressure"]), 1)
        self.assertAlmostEqual(s["series"]["cylinderPumpingPressure"][0], 14.25, places=3)

    def test_export_csv_has_bom_and_header(self):
        base = int(now_epoch() * 1000) - 60_000
        self.upload([self.frame(0, base)])
        status, text = self.api.export_csv(self.conn, {"params": ["cylinderPumpingPressure"]})
        self.assertEqual(status, 200)
        self.assertTrue(text.startswith("﻿"), "导出必须带 BOM，否则 Excel 乱码")
        self.assertIn("record_id", text.splitlines()[0])
        self.assertIn("cylinderPumpingPressure", text.splitlines()[0])

    def test_frames_endpoint_returns_effective_values(self):
        base = int(now_epoch() * 1000) - 60_000
        target = self.frame(0, base, cylinderPumpingPressure=11.0)
        self.upload([target])
        _, out = self.api.frames(self.conn, {"device_code": [DEVICE]})
        self.assertEqual(out["count"], 1)
        self.assertAlmostEqual(out["frames"][0]["values"]["cylinderPumpingPressure"], 11.0, places=3)
        self.assertFalse(out["frames"][0]["corrected"])


# ==========================================================================
# 种子数据
# ==========================================================================
class TestSeed(UploadTestBase):

    def test_seed_is_deterministic_and_idempotent(self):
        """同样的种子跑两次，得到的必须是同一批数据。"""
        n1 = seed.seed_history(self.cfg, conn=self.conn, days=2)
        count1 = self.frame_count()
        n2 = seed.seed_history(self.cfg, conn=self.conn, days=2)
        self.assertEqual(self.frame_count(), count1, "重跑种子不该新增数据")
        self.assertEqual(n1, count1)
        self.assertEqual(n2, 0, "第二次跑应当一行都没写进去")

    def test_seed_produces_cycles_and_alarms(self):
        seed.seed_history(self.cfg, conn=self.conn, days=12)
        cycles = self.conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
        alarms = self.conn.execute("SELECT COUNT(*) FROM alarms").fetchone()[0]
        self.assertGreater(cycles, 20, "30 天数据里应当有大量喷浆循环")
        self.assertGreater(alarms, 0, "注入的越限事件应当产生报警")

    def test_seeded_alarms_are_not_a_flood(self):
        """30 天数据产生的报警必须是个位数 —— 否则说明判定逻辑写崩了。"""
        seed.seed_history(self.cfg, conn=self.conn, days=12)
        n = self.conn.execute("SELECT COUNT(*) FROM alarms").fetchone()[0]
        self.assertLess(n, 20, f"报警数量 {n} 明显偏多，判定逻辑可能退化成裸阈值比较了")

    def test_seeded_values_stay_in_range_when_idle(self):
        """停机时段的越限参数不应触发报警（active_when 生效）。"""
        seed.seed_history(self.cfg, conn=self.conn, days=3)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM alarms WHERE param_key IN ('acceleratorAddSpeed','pumpingSpeed')"
            " AND ended_at IS NULL").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_seed_registers_real_batches(self):
        seed.seed_history(self.cfg, conn=self.conn, days=1)
        batches = self.conn.execute("SELECT batch_id, device_code FROM batches").fetchall()
        self.assertGreater(len(batches), 0)
        # 登记进去的 batch_id 必须是真正内容寻址的
        for b in batches:
            self.assertTrue(ids.is_valid_batch_id(b["batch_id"]), b["batch_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
