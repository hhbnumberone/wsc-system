# -*- coding: utf-8 -*-
"""终端本地存储的单测（Phase 3）。

对应计划书里对客户承诺的三件事，每件都要有代码级的证明：

* **不丢** —— 清理永远不会删掉还没上传的数据（那份 CSV 是唯一副本）。
* **不重** —— 回执没拿到而重传时，算出来的 batch_id 跟原来一样，
             平台认得出这是同一批。
* **崩了能爬起来** —— INFLIGHT 超时回收；回执到了但状态没改完，
             用存下来的回执补齐，而不是重传一遍。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import csv_schema, ids
from common.enums import BatchStatus, UploadStatus
from common.logging_setup import quiet
from common.params import PARAM_KEYS
from common.protocol import Receipt, bad_result, ok_result
from common.timeutil import CST, local_hour_floor, now_epoch, now_utc_iso, to_datetime
from terminal.store import CsvLog, LocalStore

quiet()   # 本模块会故意触发"回收超时上传"这类告警，别让它们刷掉进度点

DEVICE = "GHP9015H"
DAY_MS = 86_400_000


def _values(**over) -> dict:
    base = {
        "workStatus": 1.0, "cylinderPumpingPressure": 11.0,
        "pumpingSpeed": 10.8, "circulatingPumpingVolume": 3.2,
        "acceleratorAddSpeed": 620.0, "acceleratorProportion": 5.7,
        "acceleratorOnceDosage": 20.0, "acceleratorCumulativeDosage": 4800.0,
        "circulatingPumpingVolume": 1.0,
    }
    base.update(over)
    return {k: float(base[k]) for k in PARAM_KEYS}


def _iso(ms: int) -> str:
    return to_datetime(ms).isoformat(timespec="milliseconds")


def _hour_start_ms(ts: float) -> int:
    """ts 所在的**本地整点**（毫秒）。见 common.timeutil.local_hour_floor。"""
    return int(local_hour_floor(ts) * 1000)


def make_frame(seq: int, ts_ms: int, *, pile: str = "ZK105+207", **over) -> dict:
    """造一条形状正确的帧记录（与 terminal/app.py 产出的一致）。"""
    rec = {
        "record_id": ids.frame_record_id(DEVICE, ts_ms, seq),
        "device_code": DEVICE,
        "ts_device": _iso(ts_ms),
        "ts_epoch": ts_ms,
        "seq": seq,
        "pile_id": pile,
        "quality": 0,
        "source": "can",
        "values": _values(**over),
    }
    rec["row_hash"] = csv_schema.compute_row_hash(
        {**{c: rec.get(c, "") for c in csv_schema.META_COLUMNS}, **rec["values"]})
    return rec


class StoreTestBase(unittest.TestCase):
    """每个用例一套干净的临时目录：local.db 和 frames/ 都是新的。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.frames_dir = os.path.join(self.root, "frames")
        self.store = LocalStore(path=os.path.join(self.root, "local.db"),
                                frames_dir=self.frames_dir)
        self.cfg = self.store.cfg

    def tearDown(self):
        self.store.close()
        # Windows 上 sqlite 的 -wal/-shm 可能还开着，删不掉就等解释器退出
        try:
            self.tmp.cleanup()
        except PermissionError:
            shutil.rmtree(self.root, ignore_errors=True)

    # ---- 构造器 ----
    def add(self, count: int = 3, *, start_ms: int | None = None,
            step_ms: int = 60_000, **over) -> list[dict]:
        """写入 count 条记录，返回它们。

        默认起点是**"1 小时前"所在的那个整点**，而不是"恰好 1 小时前"。
        差这一下很关键：CSV 按本地小时轮转，起点要是跟着当前分钟走，
        在每小时的 58~59 分跑测试，这批记录就会跨进下一个小时、
        被拆成两个文件 —— 于是"3 条记录只该产生 1 个文件"这类断言
        会在一天里的某两分钟随机失败。从整点起跳之后，
        只要总跨度不到 1 小时，就一定落在同一个文件里。

        跨度需要超过 1 小时的用例，请显式传 `start_ms`，并且不要
        再假设"只有一个文件"。
        """
        base = _hour_start_ms(now_epoch()) - 3_600_000 if start_ms is None else start_ms
        recs = [make_frame(i, base + i * step_ms, **over) for i in range(count)]
        self.store.append_many(recs)
        return recs

    def age_file(self, name: str, days: float) -> None:
        """把 CSV 的 mtime 推到 days 天前，模拟"这是个老文件"。"""
        p = os.path.join(self.frames_dir, name)
        old = now_epoch() - days * 86400.0
        os.utime(p, (old, old))

    def close_writer(self) -> None:
        """关掉写入句柄，让文件进入"已写完"状态。

        清理只处理已经轮转掉的文件：手里握着写句柄的那个不能删，
        否则下一次 append 会把它重新建出来，看起来像"文件自己长回来了"。
        真实运行时 app.py 一直在写，这个动作等价于"时间跨过了整点"。
        """
        self.store.csv.close()

    def statuses(self) -> dict[str, int]:
        rows = self.store.conn.execute(
            "SELECT status, COUNT(*) AS n FROM outbox GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}


# ==========================================================================
# CSV 原始日志
# ==========================================================================
class TestCsvLog(StoreTestBase):

    def test_csv_roundtrip(self):
        """写进去再读出来，16 个参数和摘要都要对得上。"""
        recs = self.add(3)
        files = self.store.csv.files()
        self.assertEqual(len(files), 1, "同一条记录应该只产生一个文件")

        rows = list(self.store.csv.iter_file(files[0]))
        self.assertEqual(len(rows), 3)
        by_id = {r["record_id"]: r for r in rows}
        for rec in recs:
            row = by_id[rec["record_id"]]
            self.assertEqual(row["pile_id"], rec["pile_id"])
            self.assertEqual(row["device_code"], DEVICE)
            for k in PARAM_KEYS:
                self.assertAlmostEqual(float(row[k]), rec["values"][k], places=6,
                                       msg=f"{k} 往返后数值变了")
            self.assertTrue(csv_schema.row_is_intact(row), "row_hash 必须自洽")
            # 读回来的行要能还原成可上传的帧记录
            back = csv_schema.frame_from_row(row)
            self.assertEqual(back["record_id"], rec["record_id"])
            self.assertEqual(back["values"], rec["values"])

    def test_header_is_ascii_and_has_bom(self):
        """Excel 双击不乱码（BOM），列名全 ASCII（不同区域设置都能开）。"""
        self.add(1)
        path = self.store.csv.files()[0]
        with open(path, "rb") as fp:
            raw = fp.read()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "必须是 utf-8-sig 带 BOM")
        header = raw.split(b"\r\n", 1)[0].decode("utf-8-sig")
        self.assertEqual(header.split(","), list(csv_schema.HEADER))
        self.assertTrue(all(ord(c) < 128 for c in header), f"表头含非 ASCII：{header}")

    def test_appending_does_not_duplicate_header(self):
        """同一个小时里多次写入，表头只能有一行 —— 否则 Excel 打开就是断的。"""
        base = int(now_epoch() * 1000) - 3_600_000
        for i in range(4):
            self.store.append(make_frame(i, base + i * 1000))
        path = self.store.csv.files()[0]
        with open(path, "r", encoding=csv_schema.ENCODING, newline="") as fp:
            lines = [ln for ln in fp.read().splitlines() if ln.strip()]
        self.assertEqual(lines[0].split(","), list(csv_schema.HEADER))
        self.assertEqual(sum(1 for ln in lines if ln.startswith("record_id,")), 1,
                         "表头出现了不止一次")

    def test_hourly_rotation(self):
        """跨小时换文件：单文件不会无限长大，按天翻看也方便。"""
        base = int(now_epoch() * 1000) - 3_600_000
        self.store.append(make_frame(0, base))
        n1 = len(self.store.csv.files())
        self.store.append(make_frame(1, base + 2 * 3_600_000))   # 两小时后
        files = self.store.csv.files()
        self.assertEqual(len(files), 2)
        self.assertGreaterEqual(n1, 1)
        # 文件名里的时间要跟记录时间对得上
        name = os.path.basename(self.store.csv.path_for(base))
        got = CsvLog.day_of(name)
        want = to_datetime(base).astimezone(CST).strftime("%Y-%m-%d")
        self.assertEqual(got, want)

    def test_files_are_sorted_by_time(self):
        base = int(now_epoch() * 1000) - 5 * 3_600_000
        for i in range(5):
            self.store.append(make_frame(i, base + i * 3_600_000))
        files = self.store.csv.files()
        self.assertEqual(files, sorted(files), "文件必须按时间有序，导出才能拼得对")
        self.assertEqual(len(files), 5)

    def test_read_day_groups_by_local_date(self):
        base = int(now_epoch() * 1000) - 2 * DAY_MS
        self.store.append(make_frame(0, base))
        self.store.append(make_frame(1, base + DAY_MS))
        self.store.append(make_frame(2, base + 2 * DAY_MS))
        days = self.store.csv.days()
        self.assertEqual(len(days), 3, f"应该跨 3 天，实际 {days}")
        for d in days:
            self.assertEqual(len(self.store.csv.read_day(d)), 1, f"{d} 应只有 1 条")

    def test_flush_is_immediate(self):
        """写完就能被另一个句柄读到 —— 不 flush 的话崩一次就白写了。"""
        self.add(1)
        path = self.store.csv.files()[0]
        with open(path, "r", encoding=csv_schema.ENCODING, newline="") as fp:
            lines = fp.read().splitlines()
        self.assertEqual(len(lines), 2, "表头 + 1 行数据，必须已经落盘")


# ==========================================================================
# outbox 入队
# ==========================================================================
class TestOutbox(StoreTestBase):

    def test_append_creates_pending_row_with_values(self):
        self.add(2)
        self.assertEqual(self.statuses(), {"pending": 2})
        row = self.store.conn.execute("SELECT * FROM outbox LIMIT 1").fetchone()
        self.assertEqual(row["status"], UploadStatus.PENDING.value)
        self.assertEqual(row["device_code"], DEVICE)
        self.assertTrue(row["csv_file"], "必须记下这条记录在哪份 CSV 里")
        self.assertEqual(row["cylinderPumpingPressure"], 11.0)
        self.assertEqual(row["attempts"], 0)

    def test_duplicate_append_is_ignored_everywhere(self):
        """同一条记录写两次：库里有 1 行，CSV 也只有 1 行。"""
        rec = make_frame(0, int(now_epoch() * 1000) - 3_600_000)
        self.store.append(rec)
        self.store.append(rec)
        self.store.append_many([rec])
        self.assertEqual(self.statuses(), {"pending": 1})
        rows = list(self.store.csv.iter_file(self.store.csv.files()[0]))
        self.assertEqual(len(rows), 1, "重复落盘不能往 CSV 里塞第二行")

    def test_append_many_returns_only_new_ids(self):
        recs = self.add(3)
        again = self.store.append_many(recs)
        self.assertEqual(again, [], "重复的记录不该被算成新增")
        fresh = self.store.append_many(
            [make_frame(9, int(now_epoch() * 1000) - 1_800_000)])
        self.assertEqual(len(fresh), 1)

    def test_impossible_value_is_refused_at_the_device(self):
        """物理量程外的值在设备侧就拒收。

        平台一定会拒收这种值，排进队里只会变成一个永久 REJECTED；
        那是采集层或传感器的 bug，应该在源头暴露。
        """
        rec = make_frame(0, int(now_epoch() * 1000) - 3_600_000)
        rec["values"]["workStatus"] = 7.0        # 开关量只能是 0/1
        with self.assertRaises(ValueError) as ctx:
            self.store.append(rec)
        self.assertIn("开关量", str(ctx.exception))
        self.assertEqual(self.statuses(), {}, "拒收的记录不能留下半条")
        self.assertEqual(self.store.csv.files(), [], "也不该产生空 CSV 文件")

    def test_missing_param_is_refused(self):
        rec = make_frame(0, int(now_epoch() * 1000) - 3_600_000)
        del rec["values"]["pumpingSpeed"]
        with self.assertRaises(ValueError) as ctx:
            self.store.append(rec)
        self.assertIn("泵送速度", str(ctx.exception))

    def test_row_to_record_gives_back_what_we_put_in(self):
        """上传用的记录必须和落盘时一模一样，否则 row_hash 会对不上。"""
        recs = self.add(1)
        _row = self.store.conn.execute("SELECT * FROM outbox LIMIT 1").fetchone()
        back = self.store.pending_batch()[0]
        self.assertEqual(back["record_id"], recs[0]["record_id"])
        self.assertEqual(back["ts_epoch"], recs[0]["ts_epoch"])
        self.assertEqual(back["pile_id"], recs[0]["pile_id"])
        self.assertEqual(back["row_hash"], recs[0]["row_hash"])
        self.assertEqual(back["values"], recs[0]["values"])

    def test_values_survive_the_float_roundtrip_exactly(self):
        """16 个值经 SQLite REAL 存取后必须逐位相等 —— 摘要算法依赖这一点。"""
        recs = self.add(1)
        back = self.store.pending_batch()[0]
        for k in PARAM_KEYS:
            self.assertEqual(back["values"][k], recs[0]["values"][k], f"{k} 变了")


# ==========================================================================
# 取批 / 占行
# ==========================================================================
class TestClaim(StoreTestBase):

    def test_pending_batch_is_fifo_by_time(self):
        """补传优先：乱序写入，取出来必须按采集时间升序。"""
        base = int(now_epoch() * 1000) - 3_600_000
        for seq in (3, 1, 2, 0):
            self.store.append(make_frame(seq, base + seq * 1000))
        got = [r["seq"] for r in self.store.pending_batch()]
        self.assertEqual(got, [0, 1, 2, 3], "取批顺序乱了会导致平台曲线时间倒挂")

    def test_claim_marks_inflight_and_returns_owned_rows(self):
        recs = self.add(3)
        rids = [r["record_id"] for r in recs]
        bid = ids.make_batch_id(DEVICE, rids)
        claimed = self.store.claim(rids, bid)
        self.assertEqual(len(claimed), 3)
        self.assertEqual(self.statuses(), {"inflight": 3})
        row = self.store.conn.execute(
            "SELECT * FROM outbox WHERE record_id=?", (rids[0],)).fetchone()
        self.assertEqual(row["batch_id"], bid)
        self.assertEqual(row["attempts"], 1)
        self.assertTrue(row["inflight_at"])

    def test_claim_is_exclusive(self):
        """第二次占同一批必须占到 0 条 —— 判据是 UPDATE 的行数，不是"我以为"。"""
        recs = self.add(3)
        rids = [r["record_id"] for r in recs]
        bid = ids.make_batch_id(DEVICE, rids)
        self.assertEqual(len(self.store.claim(rids, bid)), 3)
        self.assertEqual(self.store.claim(rids, bid), [], "同一批不能被占两次")
        self.assertEqual(self.store.claim(rids, "other.batch"), [])

    def test_claim_skips_rows_another_batch_owns(self):
        """两批有交集时，只占到还属于本批的那部分。"""
        recs = self.add(4)
        rids = [r["record_id"] for r in recs]
        first = rids[:2]
        bid1 = ids.make_batch_id(DEVICE, first)
        self.assertEqual(len(self.store.claim(first, bid1)), 2)

        overlap = rids[1:4]              # 第 2 条已被 bid1 占了
        bid2 = ids.make_batch_id(DEVICE, overlap)
        got = self.store.claim(overlap, bid2)
        self.assertEqual({r["record_id"] for r in got}, set(rids[2:4]))

    def test_claim_increments_attempts(self):
        recs = self.add(1)
        rid = recs[0]["record_id"]
        self.store.claim([rid], "b.1")
        self.store.release_batch("b.1")
        self.store.claim([rid], "b.1")
        row = self.store.conn.execute(
            "SELECT attempts FROM outbox WHERE record_id=?", (rid,)).fetchone()
        self.assertEqual(row["attempts"], 2, "每次尝试都要计数，否则退避算不准")

    def test_backoff_defers_picking(self):
        recs = self.add(2)
        rids = [r["record_id"] for r in recs]
        self.store.claim(rids, "b.1")
        self.store.release_batch("b.1", retry_in=60.0, now=now_epoch())
        self.assertEqual(self.store.pending_batch(), [], "退避中不该被取走")
        self.assertEqual(len(self.store.pending_batch(now=now_epoch() + 61)), 2)
        self.assertEqual(self.store.pending_count(), 2,
                         "退避中的也算欠账，界面上不能消失")

    def test_manual_upload_bypasses_backoff_and_backs_off_rejection(self):
        """用户点了"立即上传"就该立刻试，哪怕之前被判过拒收。"""
        recs = self.add(2)
        rids = [r["record_id"] for r in recs]
        self.store.claim(rids, "b.1")
        self.store.release_batch("b.1", retry_in=3600.0)
        self.store.conn.execute(
            "UPDATE outbox SET status=? WHERE record_id=?",
            (UploadStatus.REJECTED.value, rids[0]))

        self.assertEqual(self.store.mark_manual([rids[0]]), 1)
        self.assertTrue(self.store.has_manual())
        got = [r["record_id"] for r in self.store.pending_batch()]
        self.assertIn(rids[0], got, "人工指定重传的记录必须马上出现")
        # pending_batch 只是"看一眼"，不会改变归属；占行才清 manual
        self.assertTrue(self.store.has_manual(), "取批不改状态，标记要留着")
        self.assertEqual(len(self.store.claim([rids[0]], "b.2")), 1)
        self.assertFalse(self.store.has_manual(), "占行后 manual 标记要清掉")

    def test_void_rows_never_upload_again(self):
        recs = self.add(2)
        self.store.mark_void([recs[0]["record_id"]], note="传感器坏了，人工判废")
        got = [r["record_id"] for r in self.store.pending_batch()]
        self.assertEqual(got, [recs[1]["record_id"]])
        self.assertEqual(self.store.stats()["void"], 1)

    def test_pending_batch_respects_max_records(self):
        self.add(10)
        self.assertEqual(len(self.store.pending_batch(limit=4)), 4)


# ==========================================================================
# 回执
# ==========================================================================
class TestReceipts(StoreTestBase):

    def _send(self, n: int = 3) -> tuple[str, list[dict]]:
        recs = self.add(n)
        rids = [r["record_id"] for r in recs]
        bid = ids.make_batch_id(DEVICE, rids)
        claimed = self.store.claim(rids, bid)
        self.store.register_batch(bid, DEVICE, rids)
        self.assertEqual(len(claimed), n)
        return bid, recs

    def test_accepted_marks_uploaded(self):
        bid, recs = self._send(3)
        rids = [r["record_id"] for r in recs]
        self.store.finish_batch(bid, Receipt(batch_id=bid,
                                            results=[ok_result(r) for r in rids]))
        self.assertEqual(self.statuses(), {"uploaded": 3})
        b = self.store.conn.execute(
            "SELECT * FROM local_batches WHERE batch_id=?", (bid,)).fetchone()
        self.assertEqual(b["status"], BatchStatus.ACKED.value)
        self.assertEqual(b["accepted"], 3)
        self.assertTrue(b["receipt_json"], "回执必须存下来 —— 崩溃后的补齐全靠它")
        self.assertEqual(self.store.stats()["backlog"], 0)

    def test_duplicate_is_treated_as_delivered(self):
        """平台说"这批我收过了"，对终端来说就是送到了。"""
        bid, recs = self._send(2)
        rids = [r["record_id"] for r in recs]
        r = Receipt(batch_id=bid, duplicate_batch=True,
                    results=[ok_result(x, duplicate=True) for x in rids])
        out = self.store.finish_batch(bid, r)
        self.assertEqual(out["duplicated"], 2)
        self.assertEqual(out["status"], BatchStatus.ACKED.value)
        self.assertEqual(self.statuses(), {"uploaded": 2})

    def test_rejected_stays_for_human_and_keeps_cause(self):
        bid, recs = self._send(2)
        rids = [r["record_id"] for r in recs]
        r = Receipt(batch_id=bid, status=BatchStatus.PARTIAL.value, results=[
            ok_result(rids[0]),
            bad_result(rids[1], "PILE_NOT_ISSUED", "桩号 ZK999+999 未由平台下发"),
        ])
        self.store.finish_batch(bid, r)
        self.assertEqual(self.statuses(), {"uploaded": 1, "rejected": 1})
        row = self.store.conn.execute(
            "SELECT * FROM outbox WHERE record_id=?", (rids[1],)).fetchone()
        self.assertEqual(row["reject_code"], "PILE_NOT_ISSUED")
        self.assertIn("未由平台下发", row["reject_note"])
        b = self.store.conn.execute(
            "SELECT status FROM local_batches WHERE batch_id=?", (bid,)).fetchone()
        self.assertEqual(b["status"], BatchStatus.PARTIAL.value)
        # 被拒的记录仍然算"欠账" —— 它没上平台，且需要人工处理
        self.assertEqual(self.store.stats()["rejected"], 1)

    def test_finish_batch_is_idempotent(self):
        """崩溃后 reconcile 会再喂一次同一个回执，不能出岔子。"""
        bid, recs = self._send(2)
        rids = [r["record_id"] for r in recs]
        r = Receipt(batch_id=bid, results=[ok_result(x) for x in rids])
        a = self.store.finish_batch(bid, r)
        b = self.store.finish_batch(bid, r, log_it=False)
        self.assertEqual(a["accepted"], b["accepted"])
        self.assertEqual(self.statuses(), {"uploaded": 2})
        self.assertEqual(
            self.store.conn.execute("SELECT COUNT(*) FROM upload_log").fetchone()[0], 1,
            "补齐流程不该往上传日志里再写一条")

    def test_partial_rejection_does_not_re_upload_accepted_rows(self):
        bid, recs = self._send(3)
        rids = [r["record_id"] for r in recs]
        r = Receipt(batch_id=bid, status=BatchStatus.PARTIAL.value, results=[
            ok_result(rids[0]), ok_result(rids[1]),
            bad_result(rids[2], "RANGE", "数值超量程"),
        ])
        self.store.finish_batch(bid, r)
        leftover = [x["record_id"] for x in self.store.pending_batch()]
        self.assertEqual(leftover, [], "已收的 2 条不该回到待传队列")

    def test_release_keeps_batch_id_so_retry_is_recognised(self):
        """★ 关键性质：回执没拿到而重传，batch_id 必须跟上次一模一样。

        否则"其实送到了但回执丢了"会被当成新批次重复入库。
        """
        bid, recs = self._send(3)
        rids = [r["record_id"] for r in recs]
        self.store.release_batch(bid, error="连接超时", retry_in=0.0)
        self.assertEqual(self.statuses(), {"pending": 3})

        again = self.store.pending_batch()
        self.assertEqual([r["record_id"] for r in again], rids,
                         "重传必须还是原来那几条、原来的顺序")
        rebatch = ids.make_batch_id(DEVICE, [r["record_id"] for r in again])
        self.assertEqual(rebatch, bid, "内容寻址：同一批记录必须算出同一个 batch_id")

        b = self.store.conn.execute(
            "SELECT status, note FROM local_batches WHERE batch_id=?", (bid,)).fetchone()
        self.assertEqual(b["status"], BatchStatus.LOST.value)
        self.assertIn("超时", b["note"])

    def test_upload_log_records_success_and_failure(self):
        bid, recs = self._send(2)
        rids = [r["record_id"] for r in recs]
        self.store.finish_batch(bid, Receipt(batch_id=bid,
                                            results=[ok_result(x) for x in rids]),
                               elapsed_ms=42)
        self.store.log_upload("net.fail", record_count=5, status="network_error",
                              http_status=0, message="连接被拒绝", elapsed_ms=3000)
        log = self.store.uploads()
        self.assertEqual(len(log), 2)
        self.assertEqual(log[0]["status"], "network_error")
        self.assertEqual(log[0]["message"], "连接被拒绝")
        self.assertEqual(log[1]["elapsed_ms"], 42)
        self.assertEqual(log[1]["record_count"], 2)

    def test_batches_list_is_newest_first(self):
        bid, _ = self._send(1)
        self.assertEqual([b["batch_id"] for b in self.store.batches()], [bid])


# ==========================================================================
# 崩溃恢复
# ==========================================================================
class TestReconcile(StoreTestBase):

    def _inflight(self, n: int = 3) -> tuple[str, list[str]]:
        recs = self.add(n)
        rids = [r["record_id"] for r in recs]
        bid = ids.make_batch_id(DEVICE, rids)
        self.store.claim(rids, bid)
        self.store.register_batch(bid, DEVICE, rids)
        return bid, rids

    def test_stale_inflight_is_requeued(self):
        """进程崩了：INFLIGHT 超时没人管，必须能自动回到待传队列。"""
        bid, rids = self._inflight(3)
        t = now_epoch()
        rep = self.store.reconcile(now=t + 200, timeout_s=120)
        self.assertEqual(rep["requeued"], 3)
        self.assertEqual(self.statuses(), {"pending": 3})
        self.assertEqual(self.store.stats()["inflight"], 0)
        self.assertEqual(len(self.store.pending_batch(now=t + 201)), 3)
        b = self.store.conn.execute(
            "SELECT status FROM local_batches WHERE batch_id=?", (bid,)).fetchone()
        self.assertEqual(b["status"], BatchStatus.LOST.value)

    def test_fresh_inflight_is_left_alone(self):
        """还在等回执的那批不能动 —— 乱回收会导致同一批被发两次。"""
        self._inflight(2)
        rep = self.store.reconcile(now=now_epoch() + 30, timeout_s=120)
        self.assertEqual(rep["requeued"], 0)
        self.assertEqual(self.statuses(), {"inflight": 2})

    def test_crash_after_writing_receipt_repairs_instead_of_resending(self):
        """★ 关键窗口：批次行已经写成 ACKED 存了回执，但 outbox 行还没改完就被杀了。

        这时**不能重传**（数据已经在平台上），要用存下来的回执把行补齐。
        """
        bid, rids = self._inflight(3)
        receipt = Receipt(batch_id=bid, results=[ok_result(x) for x in rids])
        # 手工构造崩溃现场：批次行已经写成终态并存了回执，outbox 行还卡在 INFLIGHT
        self.store.conn.execute(
            "UPDATE local_batches SET status=?, receipt_json=?, finished_at=?"
            " WHERE batch_id=?",
            (BatchStatus.ACKED.value, json.dumps(receipt.to_dict(), ensure_ascii=False),
             now_utc_iso(), bid))
        self.assertEqual(self.statuses(), {"inflight": 3}, "前提：行还卡在 INFLIGHT")

        rep = self.store.reconcile(now=now_epoch() + 10)
        self.assertEqual(rep["repaired"], 3)
        self.assertEqual(rep["requeued"], 0, "有回执的批次不该被当成超时重传")
        self.assertEqual(self.statuses(), {"uploaded": 3})
        self.assertEqual(self.store.pending_batch(), [])
        self.assertEqual(
            self.store.conn.execute("SELECT COUNT(*) FROM upload_log").fetchone()[0], 0,
            "补齐不是一次上传，不该往上传日志里记")

    def test_sending_batch_times_out_to_lost(self):
        recs = self.add(1)
        bid = ids.make_batch_id(DEVICE, [recs[0]["record_id"]])
        self.store.register_batch(bid, DEVICE, [recs[0]["record_id"]])
        # 一条都没占到 —— 模拟"登记了批次但发请求前就崩了"
        self.store.conn.execute(
            "UPDATE local_batches SET created_at=? WHERE batch_id=?",
            (to_datetime(now_epoch() - 600).isoformat(timespec="milliseconds"), bid))
        rep = self.store.reconcile(now=now_epoch(), timeout_s=120)
        self.assertEqual(rep["lost_batches"], 1)
        b = self.store.conn.execute(
            "SELECT status FROM local_batches WHERE batch_id=?", (bid,)).fetchone()
        self.assertEqual(b["status"], BatchStatus.LOST.value)

    def test_orphan_inflight_without_batch_id_is_requeued(self):
        """异常路径留下的孤儿行（没有 batch_id）也要能收回来，否则永远卡着。"""
        self.add(1)
        self.store.conn.execute(
            "UPDATE outbox SET status=?, batch_id='', inflight_at=NULL",
            (UploadStatus.INFLIGHT.value,))
        rep = self.store.reconcile(now=now_epoch())
        self.assertEqual(rep["requeued"], 1)
        self.assertEqual(self.statuses(), {"pending": 1})

    def test_reconcile_is_safe_to_run_repeatedly(self):
        self._inflight(2)
        t = now_epoch() + 200
        first = self.store.reconcile(now=t, timeout_s=120)
        second = self.store.reconcile(now=t, timeout_s=120)
        self.assertEqual(first["requeued"], 2)
        self.assertEqual(second["requeued"], 0, "第二次不能重复计数")
        self.assertEqual(self.statuses(), {"pending": 2})

    def test_reconcile_after_restart_on_a_real_store(self):
        """换一个 LocalStore 实例（= 进程重启）后，未传数据仍在队列里。"""
        recs = self.add(3)
        self.store.close()
        reopened = LocalStore(path=os.path.join(self.root, "local.db"),
                              frames_dir=self.frames_dir)
        try:
            self.assertEqual(reopened.stats()["pending"], 3)
            got = [r["record_id"] for r in reopened.pending_batch()]
            self.assertEqual(got, [r["record_id"] for r in recs], "重启后顺序不能乱")
        finally:
            reopened.close()


# ==========================================================================
# 30 天安全清理
# ==========================================================================
class TestCleanup(StoreTestBase):

    def test_cleanup_never_deletes_a_file_with_pending_rows(self):
        """★ 核心承诺：只要还有一条没上传的数据，那份 CSV 无论多老都不删。

        那是这条数据**唯一的副本** —— 删了就真没了。
        """
        old = int(now_epoch() * 1000) - 90 * DAY_MS
        recs = self.add(2, start_ms=old)
        name = os.path.basename(self.store.csv.files()[0])
        self.age_file(name, 90)

        # 一条上传成功、一条还欠着 —— 同一个文件，谁都不能删
        rids = [r["record_id"] for r in recs]
        self.store.claim(rids, "b.1")
        self.store.finish_batch("b.1", Receipt(batch_id="b.1", results=[
            ok_result(rids[0]), bad_result(rids[1], "RANGE", "超量程")]))
        self.assertEqual(self.statuses(), {"uploaded": 1, "rejected": 1})

        rep = self.store.cleanup(days=30, now=now_epoch())
        self.assertEqual(rep["deleted"], [], "有未上传记录的文件不能删")
        self.assertEqual(len(self.store.csv.files()), 1, "文件必须还在")
        self.assertIn(name, rep["protected"])
        self.assertEqual(self.store.csv.files()[0], os.path.join(self.frames_dir, name))

    def test_cleanup_never_deletes_pending(self):
        old = int(now_epoch() * 1000) - 120 * DAY_MS
        self.add(1, start_ms=old)
        self.age_file(os.path.basename(self.store.csv.files()[0]), 120)
        rep = self.store.cleanup(days=30, now=now_epoch())
        self.assertEqual(rep["deleted"], [])
        self.assertEqual(self.store.stats()["pending"], 1)

    def test_cleanup_deletes_fully_uploaded_old_files(self):
        """数据在平台上、且过了保留期 —— 本地副本可以清了。"""
        old = int(now_epoch() * 1000) - 60 * DAY_MS
        recs = self.add(1, start_ms=old)
        self.age_file(os.path.basename(self.store.csv.files()[0]), 60)
        rid = recs[0]["record_id"]
        self.store.claim([rid], "b.1")
        self.store.finish_batch("b.1", Receipt(batch_id="b.1",
                                              results=[ok_result(rid)]))
        self.close_writer()                      # 文件已经写完（时间跨过整点）
        rep = self.store.cleanup(days=30, now=now_epoch())
        self.assertEqual(len(rep["deleted"]), 1)
        self.assertGreater(rep["bytes_freed"], 0)
        self.assertEqual(self.store.csv.files(), [])
        self.assertEqual(self.store.stats()["uploaded"], 0, "已上传的索引行一并清掉")

    def test_cleanup_keeps_recent_files(self):
        recs = self.add(2)                       # 默认是"1 小时前"
        rids = [r["record_id"] for r in recs]
        self.store.claim(rids, "b.1")
        self.store.finish_batch("b.1", Receipt(batch_id="b.1",
                                              results=[ok_result(x) for x in rids]))
        self.close_writer()
        rep = self.store.cleanup(days=30, now=now_epoch())
        self.assertEqual(rep["deleted"], [])
        self.assertEqual(rep["kept_recent"], 1)
        self.assertEqual(len(self.store.csv.files()), 1)

    def test_cleanup_keeps_a_file_whose_mtime_is_recent(self):
        """设备时钟不准时宁可多留：文件名说它 90 天前，但文件是刚写的。

        判定要同时满足"mtime 过期"和"文件名日期过期"，取保守的那一边。
        """
        old = int(now_epoch() * 1000) - 90 * DAY_MS
        recs = self.add(1, start_ms=old)
        _name = os.path.basename(self.store.csv.files()[0])
        rid = recs[0]["record_id"]
        self.store.claim([rid], "b.1")
        self.store.finish_batch("b.1", Receipt(batch_id="b.1",
                                              results=[ok_result(rid)]))
        self.close_writer()   # mtime 停在"刚刚"（append 之后的值），只有文件名是老的
        rep = self.store.cleanup(days=30, now=now_epoch())
        self.assertEqual(rep["deleted"], [], "mtime 还新，不能删")
        self.assertEqual(rep["kept_recent"], 1)

    def test_cleanup_skips_the_file_currently_being_written(self):
        """正在写的文件不能删 —— 下一次 append 会把它重新建出来，等于丢数据。"""
        old = int(now_epoch() * 1000) - 90 * DAY_MS
        recs = self.add(1, start_ms=old)         # 这个文件现在是"正在写的"
        name = self.store.csv.current_name
        self.assertTrue(name, "应该有一个正在写的文件")
        rid = recs[0]["record_id"]
        self.store.claim([rid], "b.1")
        self.store.finish_batch("b.1", Receipt(batch_id="b.1",
                                              results=[ok_result(rid)]))
        self.age_file(name, 90)                  # 连 mtime 一起推老，只剩"正在写"这一层保护
        rep = self.store.cleanup(days=30, now=now_epoch())
        self.assertEqual(rep["deleted"], [], "正在写的文件不能删")
        self.assertEqual(rep["kept_open"], [name], "报告里要说清它为什么没被处理")
        self.assertEqual(len(self.store.csv.files()), 1)

    def test_cleanup_keeps_rejected_rows_as_evidence(self):
        """拒收的记录是人工对账的依据，清理不能把证据删掉。"""
        old = int(now_epoch() * 1000) - 120 * DAY_MS
        recs = self.add(1, start_ms=old)
        self.age_file(os.path.basename(self.store.csv.files()[0]), 120)
        rid = recs[0]["record_id"]
        self.store.claim([rid], "b.1")
        self.store.finish_batch("b.1", Receipt(batch_id="b.1", results=[
            bad_result(rid, "PILE_NOT_ISSUED", "桩号未下发")]))
        rep = self.store.cleanup(days=30, now=now_epoch())
        self.assertEqual(rep["deleted"], [], "有拒收记录的文件也要留着")
        self.assertEqual(self.store.stats()["rejected"], 1)

    @unittest.skipUnless(sys.platform == "win32",
                         "只有 Windows 会锁住被占用的文件（WinError 32）")
    def test_cleanup_reports_locked_files_without_raising(self):
        """Excel 开着文件时删不掉（WinError 32），不能因此让清理崩掉。

        现场演示时这是最常见的意外：用户双击 CSV 用 Excel 看了一眼，
        清理任务正好跑到 —— 必须跳过并记下来，而不是抛异常。
        """
        old = int(now_epoch() * 1000) - 60 * DAY_MS
        recs = self.add(1, start_ms=old)
        name = os.path.basename(self.store.csv.files()[0])
        rid = recs[0]["record_id"]
        self.store.claim([rid], "b.1")
        self.store.finish_batch("b.1", Receipt(batch_id="b.1",
                                              results=[ok_result(rid)]))
        self.store.csv.close()                   # 关掉自己的句柄，只留 Excel 那个
        self.age_file(name, 60)
        handle = open(os.path.join(self.frames_dir, name), "r",
                      encoding=csv_schema.ENCODING, newline="")
        try:
            rep = self.store.cleanup(days=30, now=now_epoch())
        finally:
            handle.close()
        self.assertIn(name, rep["locked"])
        self.assertEqual(rep["deleted"], [])
        self.assertEqual(len(self.store.csv.files()), 1)

    def test_cleanup_with_no_files_is_a_noop(self):
        rep = self.store.cleanup(days=30)
        self.assertEqual(rep["deleted"], [])
        self.assertEqual(rep["rows_pruned"], 0)

    def test_usage_reports_size_and_oldest_day(self):
        base = int(now_epoch() * 1000) - 2 * DAY_MS
        self.add(1, start_ms=base)
        self.add(1, start_ms=base + DAY_MS)
        u = self.store.usage()
        self.assertEqual(u["files"], 2)
        self.assertGreater(u["bytes"], 0)
        self.assertEqual(u["days"], 2)
        want = to_datetime(base).astimezone(CST).strftime("%Y-%m-%d")
        self.assertEqual(u["oldest_day"], want)


# ==========================================================================
# 修订 / 审计队列
# ==========================================================================
class TestQueues(StoreTestBase):

    def test_audit_event_is_queued_separately_from_data(self):
        """即使数据本体还没传上去，操作本身也要留痕。"""
        self.store.enqueue_audit({
            "event_id": ids.audit_record_id(DEVICE, int(now_epoch() * 1000), 1),
            "device_code": DEVICE, "kind": "usb_export",
            "ts_device": now_utc_iso(), "ts_epoch": int(now_epoch() * 1000),
            "payload": {"files": 3, "rows": 120},
        })
        self.assertEqual(self.store.stats()["audits_pending"], 1)
        got = self.store.pending_audits()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["kind"], "usb_export")
        self.assertIn("120", got[0]["payload_json"])

    def test_revision_requires_a_reason(self):
        with self.assertRaises(ValueError) as ctx:
            self.store.enqueue_revision({
                "revision_id": ids.revision_record_id(DEVICE, int(now_epoch() * 1000), 1),
                "target_record_id": "GHP9015H:1700000000000:1",
                "patch": {"cylinderPumpingPressure": 12.0},
            })
        self.assertIn("原因", str(ctx.exception))

    def test_revision_queue_claim_and_finish(self):
        recs = self.add(1)
        rid = ids.revision_record_id(DEVICE, int(now_epoch() * 1000), 1)
        self.store.enqueue_revision({
            "revision_id": rid, "target_record_id": recs[0]["record_id"],
            "device_code": DEVICE, "patch": {"cylinderPumpingPressure": 12.5},
            "reason_code": "SENSOR_FAULT", "reason_note": "压力传感器漂移",
            "operator_name": "张工",
        })
        claim = self.store.claim_queue("revision_queue", [rid], "rev.batch")
        self.assertEqual(len(claim), 1)
        self.assertEqual(claim[0]["status"], UploadStatus.INFLIGHT.value)
        self.assertEqual(self.store.claim_queue("revision_queue", [rid], "rev.2"), [])

        self.store.finish_queue("revision_queue", [ok_result(rid)], batch_id="rev.batch")
        self.assertEqual(self.store.revisions()[0]["status"], UploadStatus.UPLOADED.value)
        self.assertEqual(self.store.stats()["revisions_pending"], 0)

    def test_stale_queue_inflight_is_requeued(self):
        rid = ids.revision_record_id(DEVICE, int(now_epoch() * 1000), 1)
        self.store.enqueue_revision({
            "revision_id": rid, "target_record_id": "GHP9015H:1700000000000:1",
            "device_code": DEVICE, "patch": {"cylinderPumpingPressure": 12.5},
            "reason_code": "OTHER",
        })
        self.store.claim_queue("revision_queue", [rid], "rev.batch")
        rep = self.store.reconcile_queues(now=now_epoch() + 200, timeout_s=120)
        self.assertEqual(rep["revision_queue"], 1)
        self.assertEqual(self.store.stats()["revisions_pending"], 1)


# ==========================================================================
# 桩号缓存
# ==========================================================================
class TestPileCache(StoreTestBase):

    def test_replace_and_validate(self):
        """桩号清单整份替换 —— 平台说不发了就不该再留着。"""
        self.store.replace_piles([
            {"pile_id": "ZK105+203", "mileage_m": 105203, "line": "右线"},
            {"pile_id": "ZK105+207", "mileage_m": 105207, "line": "右线"},
        ])
        self.assertEqual(self.store.pile_ids(), ["ZK105+203", "ZK105+207"])
        self.assertTrue(self.store.pile_known("ZK105+207"))
        self.assertFalse(self.store.pile_known("ZK999+999"))

        self.store.replace_piles([{"pile_id": "ZK105+211", "mileage_m": 105211}])
        self.assertEqual(self.store.pile_ids(), ["ZK105+211"])
        self.assertFalse(self.store.pile_known("ZK105+207"), "整份替换，旧的不能留")

    def test_sync_time_is_recorded(self):
        self.store.replace_piles([{"pile_id": "ZK105+207", "mileage_m": 105207}])
        self.assertTrue(self.store.meta("piles_synced_at"), "要记下清单是什么时候同步的")


# ==========================================================================
# 界面查询
# ==========================================================================
class TestViews(StoreTestBase):

    def test_stats_counts_every_state(self):
        recs = self.add(4)
        rids = [r["record_id"] for r in recs]
        self.store.claim(rids[:3], "b.1")
        self.store.finish_batch("b.1", Receipt(batch_id="b.1", results=[
            ok_result(rids[0]), bad_result(rids[1], "RANGE", "超量程"),
            ok_result(rids[2])]))
        self.store.mark_void([rids[3]])
        s = self.store.stats()
        self.assertEqual((s["uploaded"], s["rejected"], s["void"], s["pending"]),
                         (2, 1, 1, 0))
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["backlog"], 0)

    def test_stats_reports_oldest_backlog(self):
        old = int(now_epoch() * 1000) - 5 * DAY_MS
        recs = self.add(1, start_ms=old)
        s = self.store.stats()
        self.assertEqual(s["oldest_pending"], recs[0]["ts_device"])

    def test_recent_is_newest_first_and_carries_display_strings(self):
        self.add(3)
        rows = self.store.recent(limit=3)
        self.assertEqual(len(rows), 3)
        ts = [r["ts_epoch"] for r in rows]
        self.assertEqual(ts, sorted(ts, reverse=True))
        self.assertEqual(rows[0]["status_label"], "未上传")
        # 开关量显示成中文，模拟量按注册表精度显示
        self.assertEqual(rows[0]["display"]["workStatus"], "喷浆中")
        self.assertEqual(rows[0]["display"]["cylinderPumpingPressure"], "11.00")

    def test_recent_can_filter_by_status(self):
        recs = self.add(2)
        self.store.mark_void([recs[0]["record_id"]], note="作废")
        void_rows = self.store.recent(status=UploadStatus.VOID.value)
        self.assertEqual(len(void_rows), 1)
        self.assertEqual(void_rows[0]["record_id"], recs[0]["record_id"])
        self.assertEqual(void_rows[0]["reject_note"], "作废")

    def test_get_record(self):
        recs = self.add(1)
        got = self.store.get_record(recs[0]["record_id"])
        self.assertIsNotNone(got)
        self.assertEqual(got["pile_id"], "ZK105+207")
        self.assertIsNone(self.store.get_record("nope:0:0"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
