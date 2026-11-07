# -*- coding: utf-8 -*-
"""演示脚本的自检。

`tools/prove_idempotent.py` 是给人现场演示用的，它对着一个**正在跑的**平台
发请求，所以主体没法进单测。但它有一半是纯逻辑：怎么造一批数据、怎么算
batch_id、怎么把数值摊平成参与 row_hash 的行。这部分能测，而且值得测 ——

它一旦算错，演示现场会以最难查的方式失败：脚本自己造的数据平台不收，
屏幕上显示的是"未被接受"，站在旁边的人会以为**是系统坏了**，
而不是"演示脚本坏了"。这个区别在客户面前很贵。

所以这里钉住三件事：造出来的记录是合法的、batch_id 能从内容重算出来、
篡改函数真的改动了内容（否则第 ③ 回合就是在演空气）。
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

sys.path.insert(0, os.path.join(ROOT, "tools"))

from common import params                                   # noqa: E402
from common.csv_schema import HASHED_COLUMNS, compute_row_hash  # noqa: E402
from common.ids import (is_valid_record_id, make_batch_id,   # noqa: E402
                        parse_record_id, verify_batch_id)
from common.protocol import Batch                            # noqa: E402

import prove_idempotent as demo                              # noqa: E402


class BuildRecordsTests(unittest.TestCase):
    """脚本造出来的数据必须是平台会收的 —— 不然演示的是脚本的 bug。"""

    def setUp(self):
        self.device = "GHP9015H"
        self.pile = "ZK105+307"
        self.records = demo.build_records(self.device, self.pile, n=3)

    def test_record_ids_are_well_formed_and_are_frames(self):
        self.assertEqual(3, len(self.records))
        for r in self.records:
            self.assertTrue(is_valid_record_id(r["record_id"]),
                            f"record_id 不合法：{r['record_id']}")
            self.assertEqual("frame", parse_record_id(r["record_id"])["kind"])
            self.assertEqual(self.device, parse_record_id(r["record_id"])["device_code"])

    def test_record_ids_do_not_collide_within_the_batch(self):
        ids = [r["record_id"] for r in self.records]
        self.assertEqual(len(ids), len(set(ids)), "批内出现了重复 record_id")

    def test_every_registered_param_is_present(self):
        """少一个参数，服务端的 values 校验会拒收整条。"""
        want = set(params.PARAM_KEYS)
        for r in self.records:
            missing = want - set(r["values"])
            self.assertFalse(missing, f"缺少参数：{sorted(missing)}")

    def test_values_are_inside_the_registry_domain(self):
        """造的数据要落在正常域内，否则平台一收就报警，演示会跑偏。"""
        for r in self.records:
            for key, val in r["values"].items():
                p = params.PARAM_BY_KEY[key]
                if p.lo is None or p.hi is None:
                    continue
                if key in ("circulatingPumpingVolume", "acceleratorOnceDosage",
                           "acceleratorCumulativeDosage", "workStatus"):
                    continue        # 计数器和经纬度看的是绝对范围，不是工况域
                self.assertGreaterEqual(float(val), p.lo,
                                        f"{key}={val} 低于正常域下限 {p.lo}")
                self.assertLessEqual(float(val), p.hi,
                                     f"{key}={val} 高于正常域上限 {p.hi}")

    def test_row_hash_covers_exactly_the_schema_columns(self):
        """row_hash 必须覆盖 HASHED_COLUMNS 的**每一列**，一列都不能少。

        这条测试抓到过一个真 bug：脚本原先照记录字典另拼了一个 flat 去算哈希，
        少了 quality 和 source 两列，于是同样一批数据，脚本算出的 row_hash
        和终端算出的不一样 —— 平台会把它判成内容冲突。

        这里不重走脚本的拼法（那样两边一起错还是能对上），
        而是从**协议里收到的记录**倒推：凡是 schema 里的列，
        都必须能从记录里取到值并复现出同一个摘要。
        """
        r = self.records[0]
        flat = {}
        for c in HASHED_COLUMNS:
            if c in params.PARAM_BY_KEY:
                flat[c] = r["values"][c]
            else:
                self.assertIn(c, r, f"记录里缺列 {c}，row_hash 会算错")
                flat[c] = r[c]
        self.assertEqual(r["row_hash"], compute_row_hash(flat))

    def test_timestamps_are_in_the_past(self):
        """时间超前会被平台按 TS_FUTURE 整条拒收（防"未来数据"污染曲线）。"""
        from common.timeutil import now_epoch
        now_ms = int(now_epoch() * 1000)
        for r in self.records:
            self.assertLess(r["ts_epoch"], now_ms,
                            "记录时间落在未来，平台会拒收")

    def test_seq_is_monotonic_within_the_batch(self):
        seqs = [r["seq"] for r in self.records]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))


class BatchIdTests(unittest.TestCase):
    """batch_id 是内容寻址的 —— 这是整个"不重"论证的地基。"""

    def setUp(self):
        self.records = demo.build_records("GHP9015H", "ZK105+307", n=3)
        self.ids = [r["record_id"] for r in self.records]
        self.batch_id = make_batch_id("GHP9015H", self.ids)

    def test_batch_id_verifies_against_its_own_content(self):
        self.assertTrue(verify_batch_id(self.batch_id, self.ids),
                        "脚本组出来的 batch_id 通不过平台的内容寻址校验")

    def test_same_content_gives_the_same_id_regardless_of_order(self):
        """重传时记录顺序可能不同（比如并发取行）。顺序一变 ID 就变的话，
        平台会把它当成新的一批，幂等当场失效。"""
        self.assertEqual(self.batch_id,
                         make_batch_id("GHP9015H", list(reversed(self.ids))))

    def test_a_different_record_set_gives_a_different_id(self):
        other = make_batch_id("GHP9015H", self.ids[:-1])
        self.assertNotEqual(self.batch_id, other)

    def test_the_batch_payload_round_trips(self):
        """脚本发的是 Batch.to_dict() —— 走一遍 from_dict 确认字段没漏。"""
        b = Batch(batch_id=self.batch_id, device_code="GHP9015H",
                  records=self.records, note="自检")
        back = Batch.from_dict(b.to_dict())
        self.assertEqual(b.batch_id, back.batch_id)
        self.assertEqual(self.ids, back.record_ids)
        self.assertEqual(b.content_digest(), back.content_digest())


class TamperTests(unittest.TestCase):
    """第 ③ 回合演的必须是真改动，否则"篡改被拒"就是在演空气。"""

    def test_tamper_changes_a_value_but_not_the_ids(self):
        records = demo.build_records("GHP9015H", "ZK105+307", n=3)
        bad = demo.tamper(records)

        self.assertNotEqual(records[0]["values"]["cylinderPumpingPressure"],
                            bad[0]["values"]["cylinderPumpingPressure"],
                            "tamper() 没改到值，第 ③ 回合证明不了任何事")
        # record_id 不变是**关键**：变了的话 batch_id 也会跟着变，
        # 平台会当成另外一批正常收下，"篡改"这个场景就完全不成立了。
        self.assertEqual([r["record_id"] for r in records],
                         [r["record_id"] for r in bad])

    def test_tamper_does_not_mutate_the_original(self):
        """深拷贝。原地改的话第 ① 回合上传的数据会被第 ③ 回合污染，
        而且前两个回合的回执断言会莫名其妙地飘。"""
        records = demo.build_records("GHP9015H", "ZK105+307", n=3)
        before = records[0]["values"]["cylinderPumpingPressure"]
        demo.tamper(records)
        self.assertEqual(before, records[0]["values"]["cylinderPumpingPressure"])

    def test_the_tampered_batch_keeps_the_same_batch_id(self):
        """这正是"光靠 batch_id 判重不够"的原因，也是第二层
        content_digest 校验存在的理由。"""
        records = demo.build_records("GHP9015H", "ZK105+307", n=3)
        bad = demo.tamper(records)
        i1 = [r["record_id"] for r in records]
        i2 = [r["record_id"] for r in bad]
        self.assertEqual(make_batch_id("GHP9015H", i1),
                         make_batch_id("GHP9015H", i2))

        b1 = Batch(batch_id=make_batch_id("GHP9015H", i1),
                   device_code="GHP9015H", records=records)
        b2 = Batch(batch_id=make_batch_id("GHP9015H", i2),
                   device_code="GHP9015H", records=bad)
        self.assertNotEqual(b1.content_digest(), b2.content_digest(),
                            "改了数值却没改变内容摘要 —— 平台就抓不到篡改了")


if __name__ == "__main__":
    unittest.main()
