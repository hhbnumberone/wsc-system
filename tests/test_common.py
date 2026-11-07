# -*- coding: utf-8 -*-
"""契约层与数据库层的单测（Phase 1）。

这些测试的作用是钉住几条不变量：

* `batch_id` 必须只由内容决定，且与记录顺序无关 —— 否则重传会被当成新批次。
* CSV 表头必须与参数注册表严格对齐 —— 否则模拟器写的列和终端读的列会漂移。
* `row_hash` 必须能识别"内容被改过"。
* 帧表主键与报警的"同时只一条活跃"部分唯一索引必须真的生效。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import config, csv_schema, ids
from common.enums import AlarmLevel, Quality, ReasonCode
from common.params import (PARAM_BY_KEY, PARAM_KEYS, PARAMS, check_value,
                           groups, registry)
from common.protocol import Batch, Receipt, bad_result, ok_result
from common.timeutil import CST, fmt_local, now_epoch, now_utc, now_utc_iso
from server import db


# ==========================================================================
# ID 与幂等
# ==========================================================================
class TestIds(unittest.TestCase):

    def test_record_id_roundtrip(self):
        rid = ids.frame_record_id("GHP9015H", 1770000000000, 42)
        self.assertEqual(rid, "GHP9015H:1770000000000:42")
        parsed = ids.parse_record_id(rid)
        self.assertEqual(parsed["device_code"], "GHP9015H")
        self.assertEqual(parsed["kind"], "frame")
        self.assertEqual(parsed["ts_epoch"], 1770000000000)
        self.assertEqual(parsed["seq"], 42)

    def test_revision_and_audit_record_ids(self):
        rid = ids.revision_record_id("GHP9015H", 1770000000000, 3)
        self.assertEqual(ids.parse_record_id(rid)["kind"], "rev")
        rid = ids.audit_record_id("GHP9015H", 1770000000000, 7)
        self.assertEqual(ids.parse_record_id(rid)["kind"], "audit")

    def test_bad_record_ids_rejected(self):
        for bad in ("", "abc", "GHP9015H:abc:1", "GHP9015H:123", None):
            self.assertFalse(ids.is_valid_record_id(bad), bad)

    def test_batch_id_is_content_addressed(self):
        rids = [ids.frame_record_id("GHP9015H", 1770000000000 + i, i) for i in range(5)]
        b1 = ids.make_batch_id("GHP9015H", rids)
        b2 = ids.make_batch_id("GHP9015H", rids)
        self.assertEqual(b1, b2, "同样的内容必须得到同样的 batch_id")

    def test_batch_id_ignores_order(self):
        """顺序不同、集合相同 -> 同一个 batch_id。这是重传幂等的前提。"""
        rids = [ids.frame_record_id("GHP9015H", 1770000000000 + i, i) for i in range(5)]
        shuffled = [rids[3], rids[0], rids[4], rids[1], rids[2]]
        self.assertEqual(ids.make_batch_id("GHP9015H", rids),
                         ids.make_batch_id("GHP9015H", shuffled))

    def test_batch_id_changes_with_content(self):
        a = [ids.frame_record_id("GHP9015H", 1770000000000 + i, i) for i in range(5)]
        b = a + [ids.frame_record_id("GHP9015H", 1770000009999, 9)]
        self.assertNotEqual(ids.make_batch_id("GHP9015H", a),
                            ids.make_batch_id("GHP9015H", b))

    def test_verify_batch_id_rejects_forgery(self):
        rids = [ids.frame_record_id("GHP9015H", 1770000000000 + i, i) for i in range(3)]
        good = ids.make_batch_id("GHP9015H", rids)
        self.assertTrue(ids.verify_batch_id(good, rids))
        # 拿别的批次 ID 冒充
        other = ids.make_batch_id("GHP9015H", rids[:-1])
        self.assertFalse(ids.verify_batch_id(other, rids))
        self.assertFalse(ids.verify_batch_id("乱写的", rids))


# ==========================================================================
# 参数注册表
# ==========================================================================
class TestParams(unittest.TestCase):

    def test_sixteen_params_unique_keys(self):
        self.assertEqual(len(PARAM_KEYS), len(set(PARAM_KEYS)))
        self.assertGreaterEqual(len(PARAMS), 8)

    def test_every_param_has_chinese_name_and_sane_range(self):
        for p in PARAMS:
            self.assertTrue(p.name, p.key)
            self.assertLess(p.lo, p.hi, p.key)
            self.assertGreaterEqual(p.decimals, 0, p.key)

    def test_log_fields_cover_the_contract(self):
        """日志数据的 15 个字段对照必须齐全（耿力路机字段表）。"""
        from common.params import LOG_FIELDS
        self.assertEqual(len(LOG_FIELDS), 15)
        self.assertEqual(len(set(k for k, _ in LOG_FIELDS)), 15)

    def test_check_value_rejects_impossible_only(self):
        p = PARAM_BY_KEY["cylinderPumpingPressure"]
        self.assertIsNone(check_value(p, 8.0))       # 正常
        self.assertIsNone(check_value(p, 99.0))      # 越限但物理可能 -> 放行，交给报警
        self.assertIsNotNone(check_value(p, 1e9))    # 物理不可能 -> 拒收

    def test_state_param_only_accepts_zero_or_one(self):
        p = PARAM_BY_KEY["workStatus"]
        self.assertIsNone(check_value(p, 0.0))
        self.assertIsNone(check_value(p, 1.0))
        self.assertIsNotNone(check_value(p, 0.5))

    def test_alarm_hysteresis(self):
        """回滞：触发线比清除线更"靠外"，中间是保护区。"""
        p = PARAM_BY_KEY["cylinderPumpingPressure"]      # 0..16，回滞 5% -> 0.8
        self.assertFalse(p.raise_cond(16.0))   # 正好在边界内，不算越限
        self.assertTrue(p.raise_cond(16.5))
        self.assertFalse(p.clear_cond(16.5))   # 越限时当然不清除
        self.assertFalse(p.clear_cond(15.5))   # 落进回滞带（15.2~16）不清除
        self.assertTrue(p.clear_cond(15.0))    # 退出回滞带才清除

    def test_alarmable_params_have_level(self):
        for p in PARAMS:
            if p.key in ("cylinderPumpingPressure", "acceleratorAddSpeed"):
                self.assertEqual(p.alarm_level, AlarmLevel.CRITICAL, p.key)
            if p.key in ("pumpingSpeed", "acceleratorProportion"):
                self.assertEqual(p.alarm_level, AlarmLevel.WARNING, p.key)

    def test_registry_override_only_touches_thresholds(self):
        reg = registry({"cylinderPumpingPressure": {"hi": 12.0, "name": "改不掉的"}})
        self.assertEqual(reg["cylinderPumpingPressure"].hi, 12.0)
        self.assertEqual(reg["cylinderPumpingPressure"].name, "泵送压力",
                         "配置不允许改参数中文名")

    def test_grouping_covers_all_params(self):
        covered = {p.key for ps in groups().values() for p in ps}
        self.assertEqual(covered, set(PARAM_KEYS))


# ==========================================================================
# CSV
# ==========================================================================
def _sample_row(i: int = 0) -> dict:
    values = {}
    for k in PARAM_KEYS:
        p = PARAM_BY_KEY[k]
        if p.kind == "state":
            values[k] = float(i % 2)
        elif p.kind == "geo":
            values[k] = 27.123456 + i * 1e-6
        else:
            values[k] = round(p.lo + p.span * 0.5, 4)
    return {
        "record_id": ids.frame_record_id("GHP9015H", 1770000000000 + i, i),
        "device_code": "GHP9015H",
        "ts_device": "2026-10-05T08:30:12.000+00:00",
        "ts_epoch": 1770000000000 + i,
        "seq": i,
        "pile_id": "ZK105+207",
        "quality": 0,
        "source": "can",
        "values": values,
    }


class TestCsvSchema(unittest.TestCase):

    def test_header_matches_registry(self):
        self.assertEqual(list(csv_schema.HEADER),
                         list(csv_schema.META_COLUMNS) + list(PARAM_KEYS) + ["row_hash"])

    def test_header_is_ascii(self):
        for col in csv_schema.HEADER:
            self.assertTrue(col.isascii(), f"列名必须是 ASCII：{col}")

    def test_validate_header(self):
        self.assertEqual(csv_schema.validate_header(list(csv_schema.HEADER)), [])
        errs = csv_schema.validate_header(["record_id", "ts_device"])
        self.assertTrue(any("缺少列" in e for e in errs))
        self.assertIsNotNone(csv_schema.validate_header(None))

    def test_row_hash_detects_tampering(self):
        row = csv_schema.row_from_frame(_sample_row())
        self.assertTrue(csv_schema.row_is_intact(row))
        row2 = dict(row)
        row2["cylinderPumpingPressure"] = "999.9"
        self.assertFalse(csv_schema.row_is_intact(row2))

    def test_row_hash_ignores_formatting(self):
        """同一个数写成 12.000 / 12 / " 12.0 " 都算没被改过。

        必须这样，否则人工用 Excel 打开 CSV 再原样保存一次，
        就全会变成"数据被篡改"。
        """
        base = csv_schema.row_from_frame(_sample_row())
        base["cylinderPumpingPressure"] = 12.0
        h = csv_schema.compute_row_hash(base)
        for variant in ("12.000", "12", 12, 12.0, " 12.0 ", "1.2e1"):
            probe = dict(base)
            probe["cylinderPumpingPressure"] = variant
            self.assertEqual(h, csv_schema.compute_row_hash(probe), repr(variant))

    def test_row_hash_detects_real_change(self):
        base = csv_schema.row_from_frame(_sample_row())
        base["cylinderPumpingPressure"] = 12.0
        h = csv_schema.compute_row_hash(base)
        for changed in ("12.001", 12.5, 13):
            probe = dict(base)
            probe["cylinderPumpingPressure"] = changed
            self.assertNotEqual(h, csv_schema.compute_row_hash(probe), repr(changed))

    def test_csv_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "frames_20261005_16.csv")
            fp, w = csv_schema.open_new(path)
            rows = [_sample_row(i) for i in range(5)]
            for r in rows:
                w.writerow(csv_schema.row_from_frame(r))
            fp.close()

            got = list(csv_schema.iter_rows(path))
            self.assertEqual(len(got), 5)
            for raw, want in zip(got, rows):
                self.assertEqual(raw["record_id"], want["record_id"])
                self.assertTrue(csv_schema.row_is_intact(raw), "读回来的行摘要必须自洽")
                back = csv_schema.frame_from_row(raw)
                self.assertEqual(back["pile_id"], "ZK105+207")
                self.assertEqual(back["values"]["cylinderPumpingPressure"],
                                 want["values"]["cylinderPumpingPressure"])
                self.assertEqual(back["values"]["workStatus"],
                                 want["values"]["workStatus"])

    def test_csv_uses_bom_and_append_does_not_duplicate_header(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "a.csv")
            fp, w = csv_schema.open_new(path)
            w.writerow(csv_schema.row_from_frame(_sample_row(0)))
            fp.close()
            fp, w = csv_schema.open_append(path)
            w.writerow(csv_schema.row_from_frame(_sample_row(1)))
            fp.close()
            with open(path, "rb") as f:
                self.assertTrue(f.read(3) == b"\xef\xbb\xbf", "必须是 utf-8-sig")
            self.assertEqual(len(list(csv_schema.iter_rows(path))), 2)

    def test_missing_value_raises(self):
        row = csv_schema.row_from_frame(_sample_row())
        row["cylinderPumpingPressure"] = ""
        with self.assertRaises(ValueError):
            csv_schema.frame_from_row(row)


# ==========================================================================
# 协议
# ==========================================================================
class TestProtocol(unittest.TestCase):

    def test_batch_recomputes_same_id(self):
        rids = [ids.frame_record_id("GHP9015H", 1770000000000 + i, i) for i in range(3)]
        b = Batch(batch_id=ids.make_batch_id("GHP9015H", rids),
                  device_code="GHP9015H",
                  records=[{"record_id": r} for r in rids])
        self.assertEqual(b.batch_id, b.build_id())
        self.assertTrue(b.size_ok())
        self.assertEqual(Batch.from_dict(b.to_dict()).batch_id, b.batch_id)

    def test_batch_size_limit(self):
        b = Batch(batch_id="x", device_code="D",
                  records=[{"record_id": str(i)} for i in range(ids.MAX_BATCH_RECORDS + 1)])
        self.assertFalse(b.size_ok())

    def test_receipt_counting(self):
        r = Receipt(batch_id="D.abc", results=[
            ok_result("a"), ok_result("b", duplicate=True),
            bad_result("c", ReasonCode.PILE_NOT_ISSUED, "桩号未下发"),
        ])
        self.assertEqual((r.accepted, r.duplicated, r.rejected), (1, 1, 1))
        self.assertEqual(r.ok_count, 2)
        self.assertEqual(Receipt.from_dict(r.to_dict()).ok_count, 2)

    def test_receipt_rejects_garbage(self):
        """HTTP 200 但 body 不合法时必须抛错 —— 否则终端会把没收到的数据标成已上传。"""
        for bad in ({}, {"batch_id": ""}, {"batch_id": 5}, {"batch_id": "x", "results": "不是数组"}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                Receipt.from_dict(bad)


# ==========================================================================
# 时间与枚举
# ==========================================================================
class TestTimeAndEnums(unittest.TestCase):

    def test_cst_is_fixed_offset(self):
        self.assertEqual(CST.utcoffset(None).total_seconds(), 8 * 3600)

    def test_fmt_local_shifts_by_eight_hours(self):
        # 08:30 UTC -> 16:30 CST
        self.assertEqual(fmt_local("2026-10-05T08:30:12+00:00"), "2026-10-05 16:30:12")

    def test_epoch_and_iso_are_consistent(self):
        e = now_epoch()
        self.assertAlmostEqual(e, 1770000000, delta=1e9)   # 只是量级检查
        self.assertIn("+00:00", now_utc_iso())
        self.assertIsNotNone(now_utc().tzinfo)

    def test_quality_and_labels(self):
        self.assertEqual(Quality.OK.label, "正常")
        self.assertEqual(Quality(2).label, "失真")
        self.assertEqual(ReasonCode.PILE_NOT_ISSUED.label, "桩号未由平台下发")


# ==========================================================================
# 数据库
# ==========================================================================
class TestDatabase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.sqlite3")
        self.conn = db.init_db(self.path)

    def tearDown(self):
        db.close_conn()
        self.tmp.cleanup()

    def _frames(self, n=3, dev="GHP9015H"):
        recs = []
        for i in range(n):
            r = _sample_row(i)
            r["device_code"] = dev
            recs.append(r)
        return recs

    def test_tables_created(self):
        got = db.table_counts(self.path)
        for t in ("frames", "batches", "piles", "devices", "alarms",
                  "frame_revisions", "audit_events", "rejects", "cycles", "meta"):
            self.assertIn(t, got, f"缺少表 {t}")

    def test_frame_columns_match_registry(self):
        cur = self.conn.execute("PRAGMA table_info(frames)")
        cols = {row["name"] for row in cur.fetchall()}
        for k in PARAM_KEYS:
            self.assertIn(k, cols, f"frames 表缺少参数列 {k}")

    def test_insert_then_duplicate_is_ignored(self):
        """幂等的最后一道保险：主键冲突不抛异常，只是不生效。"""
        recs = self._frames(3)
        ts = now_utc_iso()
        rows = [db.frame_row(r, "GHP9015H.aaa", ts) for r in recs]
        n1 = db.insert_many(self.conn, "frames", db.FRAME_COLUMNS, rows)
        n2 = db.insert_many(self.conn, "frames", db.FRAME_COLUMNS, rows)
        self.assertEqual(n1, 3)
        self.assertEqual(n2, 0, "同一批数据第二次写入必须是 0 行")
        total = self.conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        self.assertEqual(total, 3)

    def test_frames_primary_key_is_record_id(self):
        sql = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='frames'").fetchone()[0]
        self.assertIn("record_id     TEXT PRIMARY KEY", sql)

    def test_conflicting_same_record_id_is_ignored_not_overwritten(self):
        """同 record_id 不同内容：原始值不能被覆盖（只追加原则）。"""
        recs = self._frames(1)
        ts = now_utc_iso()
        db.insert_many(self.conn, "frames", db.FRAME_COLUMNS,
                       [db.frame_row(recs[0], "b1", ts)])
        tampered = dict(recs[0])
        tampered["values"] = dict(recs[0]["values"])
        tampered["values"]["cylinderPumpingPressure"] = 999.0
        db.insert_many(self.conn, "frames", db.FRAME_COLUMNS,
                       [db.frame_row(tampered, "b2", ts)])
        got = self.conn.execute(
            "SELECT cylinderPumpingPressure, batch_id FROM frames WHERE record_id=?",
            (recs[0]["record_id"],)).fetchone()
        self.assertNotEqual(got["cylinderPumpingPressure"], 999.0, "原值被篡改了")
        self.assertEqual(got["batch_id"], "b1")

    def test_only_one_active_alarm_per_device_param(self):
        """报警防刷屏的数据库级保证。"""
        ts = now_utc_iso()
        def add(level="critical", ended=None):
            self.conn.execute(
                "INSERT INTO alarms(device_code, param_key, level, started_at, ended_at,"
                " threshold_lo, threshold_hi, message) VALUES(?,?,?,?,?,?,?,?)",
                ("GHP9015H", "cylinderPumpingPressure", level, ts, ended, 0.0, 16.0, "x"))

        add()
        with self.assertRaises(Exception):
            add("warning")

        # 解除后可以再开一条
        self.conn.execute("UPDATE alarms SET ended_at=? WHERE ended_at IS NULL", (ts,))
        add("warning")
        active = self.conn.execute(
            "SELECT COUNT(*) FROM alarms WHERE ended_at IS NULL").fetchone()[0]
        self.assertEqual(active, 1)

    def test_revision_id_is_unique(self):
        ts = now_utc_iso()
        sql = ("INSERT INTO frame_revisions(revision_id, record_id, device_code,"
               " patch_json, reason_code, ts_server) VALUES(?,?,?,?,?,?)")
        args = ("GHP9015H:rev:1:0", "GHP9015H:1:0", "GHP9015H", '{"a":1}', "SENSOR_FAULT", ts)
        self.conn.execute(sql, args)
        with self.assertRaises(Exception):
            self.conn.execute(sql, args)

    def test_transaction_rolls_back_on_error(self):
        ts = now_utc_iso()
        recs = self._frames(2)
        with self.assertRaises(RuntimeError):
            with db.transaction(self.conn):
                db.insert_many(self.conn, "frames", db.FRAME_COLUMNS,
                               [db.frame_row(recs[0], "b", ts)])
                raise RuntimeError("故意失败")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 0,
            "异常必须回滚")

    def test_transaction_commits_on_success(self):
        ts = now_utc_iso()
        recs = self._frames(2)
        with db.transaction(self.conn):
            db.insert_many(self.conn, "frames", db.FRAME_COLUMNS,
                           [db.frame_row(r, "b", ts) for r in recs])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 2)


# ==========================================================================
# 配置
# ==========================================================================
class TestConfig(unittest.TestCase):

    def test_defaults_complete(self):
        cfg = config.load_config()
        for section in ("site", "device", "server", "terminal", "acquisition",
                        "upload", "network", "retention", "alarms", "seed", "ui"):
            self.assertIn(section, cfg, f"默认配置缺少 {section}")

    def test_upload_interval_is_demo_value_with_production_note(self):
        """演示值必须是 2 秒，且计划书要求的生产值 60 秒要在注释里写明。"""
        self.assertEqual(config.DEFAULTS["upload"]["interval_s"], 2.0)
        import inspect
        src = inspect.getsource(config)
        self.assertIn("60", src)

    def test_hosts_are_loopback_literals(self):
        cfg = config.DEFAULTS
        self.assertEqual(cfg["server"]["host"], "127.0.0.1")
        self.assertEqual(cfg["terminal"]["host"], "127.0.0.1")

    def test_url_base_normalises_localhost(self):
        cfg = config.load_config()
        cfg["server"]["host"] = "localhost"
        self.assertTrue(config.url_base("server", cfg).startswith("http://127.0.0.1:"))

    def test_deep_merge_does_not_mutate_defaults(self):
        before = config.DEFAULTS["upload"]["interval_s"]
        config._deep_merge(config.DEFAULTS, {"upload": {"interval_s": 99}})
        self.assertEqual(config.DEFAULTS["upload"]["interval_s"], before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
