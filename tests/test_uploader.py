# -*- coding: utf-8 -*-
"""上传状态机的单测（Phase 4）。

这一组不放假 client —— `InProcessClient` 直接把 `server.api.Api` 包成
`PlatformClient` 的形状，走进程内调用。于是测到的是**真的**平台逻辑：
内容寻址的 batch_id 校验、内容摘要冲突、桩号白名单、幂等重放。
换成一个返回固定回执的假 client，这些一条都测不到，而它们恰恰是
"不丢不重"的全部依据。

对应验证清单：
* **断网缓存** —— 断网期间一条不丢，恢复后按时间顺序补上去。
* **超时重试** —— 没拿到回执就退回待传，退避；且**永不放弃**。
* **INFLIGHT 回收** —— 进程崩了留下的 INFLIGHT 能自己爬回来。
* **补传排序** —— 帧数据先走，修订等目标记录入库，审计排在最后。
* **部分拒收** —— 一条坏数据不连累同批的好数据，也不无限重试。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import config, csv_schema, http_util, ids
from common.enums import BatchStatus, EventKind, ReasonCode, UploadStatus
from common.logging_setup import quiet
from common.params import PARAM_KEYS
from common.protocol import Receipt
from common.timeutil import local_hour_floor, now_epoch, to_datetime
from server import db, seed
from server.alarms import AlarmEngine
from server.api import Api
from terminal.network import LinkMonitor
from terminal.store import LocalStore
from terminal.uploader import Uploader

quiet()   # 本模块会故意制造重试/拒收，别让告警刷掉进度点

DEVICE = "GHP9015H"
PILE_A = "ZK105+207"
PILE_B = "ZK105+211"


# ==========================================================================
# 进程内的"平台"
# ==========================================================================
class InProcessClient:
    """把 `server.api.Api` 包成 `PlatformClient` 的形状。

    每个请求**新建一个连接、用完就关**：不跨线程持有 sqlite 连接，
    于是线程版用例和普通用例可以共用同一个实现，也不会有
    "临时目录删不掉"的尾巴。

    `script` 用来注入故障，按顺序消费：
        {"raise": exc}                 —— 直接抛，平台根本没收到
        {"deliver": exc}               —— 平台收到了，但回执回不来（丢响应）
    """

    def __init__(self, path: str, cfg: dict, *, script: list | None = None):
        self.path = path
        self.cfg = cfg
        self.api = Api(cfg, AlarmEngine(config.load_thresholds()))
        self.script = list(script or [])
        self.calls = 0
        self.sent: list[list[str]] = []      # 每次真正发出去的 record_id 列表
        self.batch_ids: list[str] = []
        self.kinds: list[str] = []

    def _next_fault(self):
        return self.script.pop(0) if self.script else None

    # ---- 与 PlatformClient 同形的两个方法 ----
    def upload(self, batch) -> Receipt:
        self.calls += 1
        fault = self._next_fault()
        if fault and "raise" in fault:
            raise fault["raise"]

        conn = db.connect(self.path)
        try:
            self.sent.append(list(batch.record_ids))
            self.batch_ids.append(batch.batch_id)
            self.kinds.append(batch.kind)
            status, payload = self.api.upload(conn, batch.to_dict())
        finally:
            conn.close()

        if fault and "deliver" in fault:
            # 平台真的收下了（上面这次调用已经入库），只是回执在路上没了
            raise fault["deliver"]
        if status >= 400:
            raise _http_error(status, payload)
        return Receipt.from_dict(payload)

    def heartbeat(self, hb) -> dict:
        conn = db.connect(self.path)
        try:
            return self.api.heartbeat(conn, hb.to_dict())[1]
        finally:
            conn.close()

    def piles(self, site_id: str = "") -> tuple[list[dict], str]:
        conn = db.connect(self.path)
        try:
            _s, payload = self.api.piles(conn, {"site_id": [site_id]} if site_id else {})
        finally:
            conn.close()
        return payload["piles"], payload["issued_at"]

    def check_identity(self, site_id: str, device_code: str) -> dict:
        """与 `PlatformClient.check_identity` 同形：4xx 是平台的答复，不是异常。"""
        q = {}
        if site_id:
            q["site_id"] = [site_id]
        if device_code:
            q["device_code"] = [device_code]
        conn = db.connect(self.path)
        try:
            status, payload = self.api.identity_check(conn, q)
        finally:
            conn.close()
        if status >= 400:
            if 400 <= status < 500:
                return dict(payload, ok=False)
            raise _http_error(status, payload)
        return payload

    def health(self) -> bool:
        return True


def _http_error(status: int, payload: dict) -> http_util.NetworkError:
    """按 http_util 的分类规则造异常，跟真 HTTP 路径保持一致。"""
    return http_util.NetworkError(
        f"平台返回 HTTP {status}：{payload.get('error', '')}",
        retryable=(status in (408, 425, 429) or status >= 500),
        status=status, body=payload)


def timeout_error(msg: str = "网络不可达：TimeoutError: timed out") -> http_util.NetworkError:
    """没连上（或连上了没等到回执）—— 可重试。"""
    return http_util.NetworkError(msg, retryable=True, status=0)


# ==========================================================================
# 用例基类
# ==========================================================================
class UploaderTestBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.platform_db = os.path.join(self.root, "platform.sqlite3")
        self.cfg = config.load_config()
        self.cfg["device"]["device_code"] = "GHP9015H"   # 测试造帧固定此编号：与现场配置隔离

        conn = db.init_db(self.platform_db)
        with db.transaction(conn):
            seed.ensure_piles(conn, self.cfg)
            seed.ensure_device(conn, self.cfg)

        self.store = LocalStore(path=os.path.join(self.root, "local.db"),
                                frames_dir=os.path.join(self.root, "frames"))
        self.client = InProcessClient(self.platform_db, self.cfg)
        self.link = LinkMonitor(self.cfg)
        self.link.observe(True)                     # 探测成功 -> ONLINE
        self.up = Uploader(self.store, self.client, self.link, self.cfg)
        # 起点落在本地整点上：CSV 按本地小时轮转，跟着分钟走的话
        # 这批记录会被拆成两个文件（见 common.timeutil.local_hour_floor）。
        self.base_ms = int((local_hour_floor() - 3600.0) * 1000)

    def tearDown(self):
        self.store.close()
        db.close_conn()
        try:
            self.tmp.cleanup()
        except (OSError, PermissionError):
            pass                                # Windows 上连接可能还没释放干净

    # ---- 构造 ----
    def add(self, count: int, *, start_ms: int | None = None, step_ms: int = 2000,
            pile: str = PILE_A, **over) -> list[str]:
        """往终端塞 count 帧，返回 record_id 列表。"""
        base = self.base_ms if start_ms is None else start_ms
        out = []
        for i in range(count):
            rec = make_frame(i + 1, base + i * step_ms, pile=pile, **over)
            out.append(self.store.append(rec))
        return out

    def drain(self, *, limit: int = 60) -> int:
        """反复跑 `_cycle()` 直到没活干。返回跑了几轮。

        上限是防呆：状态机真有死循环的话，测试要报错而不是挂住。
        """
        for i in range(limit):
            worked, _sent = self.up._cycle()
            if not worked:
                return i
        self.fail(f"{limit} 轮之后还在干活，状态机可能没收敛")

    # ---- 断言 ----
    def server_count(self) -> int:
        conn = db.connect(self.platform_db)
        try:
            return conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        finally:
            conn.close()

    def server_rows(self) -> list:
        conn = db.connect(self.platform_db)
        try:
            return conn.execute(
                "SELECT record_id, ts_epoch, pile_id FROM frames"
                " ORDER BY ts_epoch, seq").fetchall()
        finally:
            conn.close()

    def statuses(self) -> dict:
        rows = self.store.conn.execute(
            "SELECT status, COUNT(*) AS n FROM outbox GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def rows(self) -> list:
        return self.store.conn.execute(
            "SELECT * FROM outbox ORDER BY ts_epoch, seq").fetchall()

    def upload_log(self) -> list[dict]:
        return self.store.uploads(limit=200)


def make_frame(seq: int, ts_ms: int, *, pile: str = PILE_A, **over) -> dict:
    """造一条形状正确的帧记录（与 terminal/app.py 产出的完全一致）。"""
    values = {
        "workStatus": 1.0, "cylinderPumpingPressure": 11.0,
        "pumpingSpeed": 10.8, "circulatingPumpingVolume": 3.2,
        "acceleratorAddSpeed": 620.0, "acceleratorProportion": 5.7,
        "acceleratorOnceDosage": 20.0, "acceleratorCumulativeDosage": 4800.0,
    }
    values.update(over)
    rec = {
        "record_id": ids.frame_record_id(DEVICE, ts_ms, seq),
        "device_code": DEVICE,
        "ts_device": to_datetime(ts_ms).isoformat(timespec="milliseconds"),
        "ts_epoch": ts_ms,
        "seq": seq,
        "pile_id": pile,
        "quality": 0,
        "source": "can",
        "values": {k: float(values[k]) for k in PARAM_KEYS},
    }
    rec["row_hash"] = csv_schema.compute_row_hash(
        {**{c: rec.get(c, "") for c in csv_schema.META_COLUMNS}, **rec["values"]})
    return rec


# ==========================================================================
# 断网缓存
# ==========================================================================
class TestOffline(UploaderTestBase):

    def test_offline_does_not_send_and_keeps_everything(self):
        self.add(20)
        self.link.set_manual_offline(True)

        self.up._cycle()

        self.assertEqual(self.server_count(), 0, "断网期间一个请求都不该发出去")
        self.assertEqual(self.client.calls, 0)
        self.assertEqual(self.store.pending_count(), 20, "断网期间数据必须全留着")
        self.assertEqual(self.up.last["status"], "offline")

    def test_backlog_survives_a_restart(self):
        """断网 + 重启：数据在磁盘上，重启后欠账一条不少。"""
        self.link.set_manual_offline(True)
        self.add(35)
        path, frames = self.store.path, self.store.csv.dir
        self.store.close()

        again = LocalStore(path=path, frames_dir=frames)
        try:
            self.assertEqual(again.pending_count(), 35)
        finally:
            again.close()

    def test_reconnect_drains_the_backlog_in_time_order(self):
        self.link.set_manual_offline(True)
        self.add(30)
        self.up._cycle()
        self.assertEqual(self.server_count(), 0)

        self.link.set_manual_offline(False)
        self.drain()

        self.assertEqual(self.server_count(), 30, "恢复联网后欠账要全部补上")
        self.assertEqual(self.store.pending_count(), 0)
        # 补传严格按时间顺序：平台上不能出现"先新后旧"的倒挂
        sent = [rid for batch in self.client.sent for rid in batch]
        self.assertEqual(len(sent), 30)
        epochs = [int(r.split(":")[1]) for r in sent]
        self.assertEqual(epochs, sorted(epochs), "补传必须按 ts 升序")

    def test_offline_mid_flight_keeps_the_claimed_rows(self):
        """发到一半断网：已经占住的行必须能退回待传，不能卡在 INFLIGHT。"""
        self.add(10)
        self.client.script.append({"raise": timeout_error()})
        self.up._cycle()

        self.assertEqual(self.statuses().get(UploadStatus.INFLIGHT.value, 0), 0,
                         "没有 INFLIGHT 残留在外")
        self.assertEqual(self.store.pending_count(), 10)


# ==========================================================================
# 超时重试
# ==========================================================================
class TestRetry(UploaderTestBase):

    def test_timeout_requeues_and_backs_off(self):
        self.add(10)
        self.client.script.append({"raise": timeout_error()})
        self.up._cycle()

        self.assertEqual(self.server_count(), 0)
        self.assertEqual(self.statuses(), {UploadStatus.PENDING.value: 10})
        # 退避：下次尝试时间必须推到将来，否则就是死循环打平台
        row = self.rows()[0]
        self.assertGreater(int(row["next_attempt_at"]), int(now_epoch()))
        self.assertEqual(int(row["attempts"]), 1)
        self.assertIn("TimeoutError", row["last_error"])
        # 批次账本要标 LOST，界面上才看得出"这批没发成"
        b = self.store.batches(limit=5)[0]
        self.assertEqual(b["status"], BatchStatus.LOST.value)

    def test_failure_is_recorded_in_the_upload_log(self):
        """失败也要留痕 —— 终端界面的"上传记录"要能看见失败。"""
        self.add(5)
        self.client.script.append({"raise": timeout_error()})
        self.up._cycle()

        entry = self.upload_log()[0]
        self.assertEqual(entry["status"], "network_error")
        self.assertEqual(entry["record_count"], 5)
        self.assertEqual(entry["kind"], EventKind.FRAME.value)

    def test_lost_response_is_recognised_as_a_duplicate_on_retry(self):
        """★ 本文件最重要的一条：送到了但回执丢了，重传不会重复入库。

        这一条同时证明了三件事：内容寻址的 batch_id 真的能重算出同一个值、
        平台真的会回原回执、终端真的把 duplicate 当成"已经上去了"。
        """
        self.add(8)
        # 平台收下了（真的入库），但回执在回程丢了
        self.client.script.append({"deliver": timeout_error("连接被重置")})
        self.up._cycle()

        self.assertEqual(self.server_count(), 8, "平台其实已经收下了")
        self.assertEqual(self.statuses(), {UploadStatus.PENDING.value: 8},
                         "终端没拿到回执，就该当没送到")

        # 等退避过去太慢，直接让操作员点一次"立即上传"
        self.store.mark_manual()
        self.up._cycle()

        self.assertEqual(self.server_count(), 8, "重传之后行数必须还是 8")
        self.assertEqual(self.store.pending_count(), 0)
        self.assertEqual(self.statuses().get(UploadStatus.UPLOADED.value), 8)
        self.assertEqual(self.client.sent[0], self.client.sent[1],
                         "重传的批次内容必须与首次完全一致")
        kinds = [b["status"] for b in self.store.batches(limit=5)]
        self.assertIn(BatchStatus.ACKED.value, kinds)

    def test_two_batches_have_different_ids(self):
        """防呆：内容寻址不能退化成"所有批次一个 id"。"""
        self.add(4)
        self.drain()
        self.store.append(make_frame(99, self.base_ms + 900_000))
        self.up._cycle()

        self.assertEqual(len(self.client.batch_ids), 2)
        self.assertNotEqual(self.client.batch_ids[0], self.client.batch_ids[1])
        # batch_id 必须真的是内容算出来的，不是随手拼的
        for bid, rids in zip(self.client.batch_ids, self.client.sent):
            self.assertTrue(ids.verify_batch_id(bid, rids))

    def test_network_errors_never_give_up(self):
        """纯网络错误**不设放弃上限**：断网三天后数据还是要补上去的。

        配置里 `max_attempts` 是 8，这里连撞 12 次，行必须还是待传。
        """
        self.add(3)
        self.client.script.extend({"raise": timeout_error()} for _ in range(12))
        for _ in range(12):
            self.store.mark_manual()          # 跳过退避，模拟"一再重试"
            self.up._cycle()

        self.assertEqual(self.statuses(), {UploadStatus.PENDING.value: 3})
        self.assertGreaterEqual(int(self.rows()[0]["attempts"]), 12)
        self.assertEqual(self.store.pending_count(), 3)

    def test_backoff_grows_then_caps(self):
        table = self.cfg["upload"]["backoff_s"]
        got = [self.store.backoff_for(n) for n in range(1, 9)]
        self.assertEqual(got[:len(table)], [float(x) for x in table])
        self.assertEqual(self.store.backoff_for(99), float(table[-1]),
                         "退避要封顶，不能无限增长")

    def test_local_validation_failure_releases_instead_of_losing(self):
        """本地就发现不该发（空批次 / 超上限）：退回待传，不静默丢。"""
        self.add(3)
        orig = self.client.upload

        def refuse(batch):
            raise ValueError("批次 3 条超过本端上限 1 条")

        self.client.upload = refuse
        try:
            self.up._cycle()
        finally:
            self.client.upload = orig

        self.assertEqual(self.store.pending_count(), 3, "一条都不能丢")
        self.assertEqual(self.statuses(), {UploadStatus.PENDING.value: 3})
        # 而且带上了退避，不会变成"取一次失败一次"的空转
        self.assertGreater(int(self.rows()[0]["next_attempt_at"]), int(now_epoch()))


# ==========================================================================
# INFLIGHT 回收（崩溃恢复）
# ==========================================================================
class TestInflightRecovery(UploaderTestBase):

    def test_recover_requeues_a_crash_left_inflight(self):
        """作业中强杀终端：占住的行必须在重启后自己爬回待传队列。"""
        self.add(12)
        picked = self.store.pending_batch()
        self.store.claim([r["record_id"] for r in picked], "GHP9015H.deadbeefdeadbeef")
        self.assertEqual(self.statuses(), {UploadStatus.INFLIGHT.value: 12})

        up = Uploader(self.store, self.client, self.link, self.cfg)
        rep = up.recover()

        self.assertEqual(rep["requeued"], 12)
        self.assertEqual(rep["repaired"], 0)
        self.assertEqual(self.store.pending_count(), 12)

    def test_recovered_rows_go_up_without_duplication(self):
        self.add(12)
        picked = self.store.pending_batch()
        self.store.claim([r["record_id"] for r in picked], "GHP9015H.deadbeefdeadbeef")

        up = Uploader(self.store, self.client, self.link, self.cfg)
        up.recover()
        # recover 用 retry_in=0 绕过退避：重启是重新开始，不是第 N 次重试
        self.drain()

        self.assertEqual(self.server_count(), 12)
        self.assertEqual(self.store.pending_count(), 0)

    def test_recover_is_idempotent(self):
        """连开两次机、或者恢复流程被重复调用，都不能出问题。"""
        self.add(6)
        self.up.recover()
        rep2 = self.up.recover()

        self.assertEqual(rep2["requeued"], 0)
        self.assertEqual(rep2["repaired"], 0)
        self.assertEqual(self.store.pending_count(), 6)

    def test_recovery_report_is_visible_to_the_ui(self):
        self.add(1)
        picked = self.store.pending_batch()
        self.store.claim([r["record_id"] for r in picked], "GHP9015H.deadbeefdeadbeef")
        self.up.recover()

        snap = self.up.recovered
        for key in ("repaired", "requeued", "lost_batches"):
            self.assertIn(key, snap)

    def test_stale_inflight_does_not_resend_a_delivered_batch(self):
        """回执到了、行状态没改完就被杀 —— 用存下的回执补齐，不重传。"""
        self.add(5)
        picked = self.store.pending_batch()
        rids = [r["record_id"] for r in picked]
        bid = ids.make_batch_id(DEVICE, rids)
        self.store.claim(rids, bid)
        self.store.register_batch(bid, DEVICE, rids)

        # 平台收到并入库了
        conn = db.connect(self.platform_db)
        try:
            from common.protocol import Batch
            batch = Batch(batch_id=bid, device_code=DEVICE,
                          kind=EventKind.FRAME.value, records=picked)
            _status, payload = self.client.api.upload(conn, batch.to_dict())
        finally:
            conn.close()
        receipt = Receipt.from_dict(payload)
        self.store.finish_batch(bid, receipt)          # 回执先落库

        # 然后模拟"行状态还没改完就被杀"：手工把行打回 INFLIGHT
        self.store.conn.execute("UPDATE outbox SET status='inflight'")
        self.assertEqual(self.server_count(), 5)

        up = Uploader(self.store, self.client, self.link, self.cfg)
        rep = up.recover()

        self.assertEqual(rep["repaired"], 5, "应该走补齐，而不是重传")
        self.assertEqual(self.statuses().get(UploadStatus.UPLOADED.value), 5)
        self.assertEqual(self.server_count(), 5)


# ==========================================================================
# 补传排序：帧 -> 修订 -> 审计
# ==========================================================================
class TestOrdering(UploaderTestBase):

    def _revision(self, target: str, *, note: str = "校对一个失真值") -> str:
        return self.store.enqueue_revision({
            "revision_id": ids.revision_record_id(DEVICE, self.base_ms + 500_000, 1),
            "target_record_id": target,
            "device_code": DEVICE,
            "patch": {"cylinderPumpingPressure": 12.5},
            "reason_code": ReasonCode.SENSOR_FAULT.value,
            "reason_note": note,
            "operator_name": "张工",
            "ts_device": to_datetime(self.base_ms + 500_000).isoformat(
                timespec="milliseconds"),
        })

    def test_frames_go_before_revisions(self):
        rids = self.add(9)
        self._revision(rids[0])

        self.drain()

        # 第一次调用必须是帧批；修订批只能出现在帧之后
        self.assertEqual(self.client.kinds[0], EventKind.FRAME.value)
        self.assertIn(EventKind.REVISION.value, self.client.kinds)
        self.assertLess(self.client.kinds.index(EventKind.FRAME.value),
                        self.client.kinds.index(EventKind.REVISION.value))

    def test_revisions_are_held_while_the_frame_backlog_is_open(self):
        """修订必须等目标记录入库 —— 否则平台回"目标记录不存在"，永久拒收。"""
        rids = self.add(9)
        self._revision(rids[8])

        # 只发一批（batch_max_records=200，这里 9 条会一次发完），
        # 所以先手工把批量压小，制造"帧还没发完"的局面
        self.store.cfg["upload"]["batch_max_records"] = 4
        self.up._cycle()

        self.assertEqual(self.client.kinds, [EventKind.FRAME.value],
                         "帧还有欠账时不该碰修订")
        self.assertEqual(len(self.store.pending_revisions()), 1)

        self.store.cfg["upload"]["batch_max_records"] = 200
        self.drain()
        self.assertEqual(len(self.store.pending_revisions()), 0, "帧清完后修订要发出去")

    def test_revision_before_its_frame_is_permanently_rejected(self):
        """把上面那条的"为什么"直接证明给平台看。

        修订先发，平台回"目标记录不存在"，而且是**永久拒收** ——
        所以排序不是性能优化，是正确性要求。
        """
        target = ids.frame_record_id(DEVICE, self.base_ms, 1)
        self._revision(target)                       # 目标帧根本没在库里

        self.drain()

        row = self.store.conn.execute(
            "SELECT * FROM revision_queue").fetchone()
        self.assertEqual(row["status"], UploadStatus.REJECTED.value)
        self.assertIn("不存在", row["last_error"])

    def test_audit_waits_its_turn_but_is_not_lost(self):
        self.add(6)
        self.store.enqueue_audit({
            "event_id": ids.audit_record_id(DEVICE, self.base_ms + 100_000, 1),
            "device_code": DEVICE,
            "kind": "manual_upload",
            "ts_device": to_datetime(self.base_ms + 100_000).isoformat(
                timespec="milliseconds"),
            "ts_epoch": int((self.base_ms + 100_000) / 1000) * 1000,
            "payload": {"operator": "张工", "count": 6},
        })

        self.drain()

        self.assertEqual(self.client.kinds[-1], EventKind.AUDIT.value,
                         "审计排在最后，但一定会发出去")
        row = self.store.conn.execute("SELECT * FROM audit_queue").fetchone()
        self.assertEqual(row["status"], UploadStatus.UPLOADED.value)

    def test_audit_is_uploaded_even_when_no_data_exists(self):
        """没有帧数据时，操作留痕照样要能上去。"""
        self.store.enqueue_audit({
            "event_id": ids.audit_record_id(DEVICE, self.base_ms, 7),
            "device_code": DEVICE,
            "kind": "usb_export",
            "ts_device": to_datetime(self.base_ms).isoformat(timespec="milliseconds"),
            "ts_epoch": self.base_ms,
            "payload": {"operator": "张工", "file": "frames_20261005_09.csv"},
        })

        self.drain()

        self.assertEqual(self.client.kinds, [EventKind.AUDIT.value])
        self.assertEqual(
            self.store.conn.execute("SELECT status FROM audit_queue").fetchone()["status"],
            UploadStatus.UPLOADED.value)


# ==========================================================================
# 部分拒收
# ==========================================================================
class TestRejection(UploaderTestBase):

    def test_one_bad_row_does_not_take_the_batch_down(self):
        """★ 桩号未下发的那一条被拒，同批其他条必须照常入库。"""
        good = self.add(5)
        bad = make_frame(999, self.base_ms + 500_000, pile="ZK999+999")
        self.store.append(bad, strict=False)

        self.drain()

        self.assertEqual(self.server_count(), 5, "好的 5 条都该进去")
        rows = {r["record_id"]: r for r in self.rows()}
        self.assertEqual(rows[bad["record_id"]]["status"], UploadStatus.REJECTED.value)
        self.assertEqual(rows[bad["record_id"]]["reject_code"],
                         ReasonCode.PILE_NOT_ISSUED.value)
        for rid in good:
            self.assertEqual(rows[rid]["status"], UploadStatus.UPLOADED.value)

    def test_a_rejected_row_is_not_retried_forever(self):
        self.add(3)
        bad = make_frame(999, self.base_ms + 500_000, pile="ZK999+999")
        self.store.append(bad, strict=False)
        self.drain()

        self.assertEqual(self.store.pending_count(), 0, "拒收的不该还占着待传")
        worked, _sent = self.up._cycle()
        self.assertFalse(worked, "没有待传数据时不该继续干活")
        self.assertEqual(len(self.client.sent), 1, "不该反复重发已被拒收的批次")

    def test_batch_level_rejection_marks_all_and_moves_on(self):
        """平台整批拒收（4xx + 原因码）：全部标永久失败，且**不再空转**。"""
        self.add(6)
        self.client.script.append({"raise": _http_error(400, {
            "error": "batch_id 与批次内容不符", "code": "SCHEMA"})})

        worked, sent = self.up._cycle()

        self.assertTrue(worked)
        self.assertTrue(sent, "整批拒收也是了结，循环要继续往下走而不是卡住")
        self.assertEqual(self.statuses(), {UploadStatus.REJECTED.value: 6})
        self.assertEqual(self.store.pending_count(), 0)

    def test_rejection_is_visible_in_the_upload_log(self):
        """整批拒收要留痕，而且**理由要能传到界面上**。

        批次状态是 PARTIAL 而不是别的：平台确实回话了（所以不是 LOST），
        只是没有一条进去。真正说明问题的是 rejected 计数、HTTP 状态码
        和平台给的那句理由 —— 操作员照着那句才知道该改什么。
        """
        self.add(2)
        self.client.script.append({"raise": _http_error(409, {
            "error": "内容冲突", "code": ReasonCode.DUPLICATE_CONFLICT.value})})
        self.up._cycle()

        entry = self.upload_log()[0]
        self.assertEqual(entry["status"], BatchStatus.PARTIAL.value)
        self.assertEqual(entry["http_status"], 409)
        self.assertEqual(entry["rejected"], 2)
        self.assertEqual(entry["accepted"], 0)
        self.assertIn("内容冲突", entry["message"], "平台的理由必须带上来")

    def test_manual_upload_revives_rejected_rows(self):
        """人工校对之后坚持要重传，就得让他传 —— 平台会再判一次。"""
        self.add(2)
        bad = make_frame(999, self.base_ms + 500_000, pile="ZK999+999")
        self.store.append(bad, strict=False)
        self.drain()
        self.assertEqual(self.store.pending_count(), 0)

        n = self.store.mark_manual([bad["record_id"]])
        self.assertEqual(n, 1)
        worked, _sent = self.up._cycle()

        self.assertTrue(worked, "被人工拎回来的行必须真的再走一遍")
        self.assertEqual(self.store.pending_count(), 0)
        self.assertEqual(len(self.client.sent), 2, "确实又发了一次")

    def test_rejected_rows_keep_their_original_csv(self):
        """拒收 ≠ 原始记录消失。CSV 是唯一副本，必须还在那里。

        这是"只追加不覆盖"的底线：平台上被拒收，不代表现场那一秒
        没有发生过，原始记录仍然要留得住、拿得出。
        """
        bad = make_frame(999, self.base_ms, pile="ZK999+999")
        self.store.append(bad, strict=False)
        self.drain()
        self.assertEqual(self.statuses().get(UploadStatus.REJECTED.value), 1)

        found = []
        for path in self.store.csv.files():
            found.extend(r.get("record_id") for r in self.store.csv.iter_file(path))
        self.assertIn(bad["record_id"], found,
                      "被平台拒收的原始记录必须还躺在 CSV 里")


# ==========================================================================
# 真跑一次线程
# ==========================================================================
class TestThreadRun(UploaderTestBase):

    def test_run_uploads_then_stops_cleanly(self):
        self.add(15)
        self.up.interval = 0.05
        self.up.start()
        try:
            deadline = time.time() + 10
            while self.store.pending_count() > 0 and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(self.store.pending_count(), 0,
                             "上传线程没能在 10 秒内把欠账清完")
        finally:
            self.up.stop()
            self.up.join(timeout=5)
            self.assertFalse(self.up.is_alive(), "stop() 之后线程要退出")

        self.assertEqual(self.server_count(), 15)
        # 上传线程不拥有 store，停掉它之后本地库必须还能用
        self.assertEqual(self.store.pending_count(), 0)
        self.assertGreaterEqual(len(self.store.uploads(limit=10)), 1)

    def test_run_recovers_before_sending(self):
        """线程起来先做恢复，再开始发 —— 顺序不能反。"""
        self.add(4)
        picked = self.store.pending_batch()
        self.store.claim([r["record_id"] for r in picked], "GHP9015H.deadbeefdeadbeef")

        self.up.interval = 0.05
        self.up.start()
        try:
            deadline = time.time() + 10
            while self.store.pending_count() > 0 and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(self.server_count(), 4)
            self.assertEqual(self.up.recovered.get("requeued"), 4)
        finally:
            self.up.stop()
            self.up.join(timeout=5)

    def test_stop_before_start_is_harmless(self):
        self.up.stop()
        self.up.start()
        self.up.join(timeout=5)
        self.assertFalse(self.up.is_alive())
        self.assertEqual(self.client.calls, 0)


# ==========================================================================
# 心跳
# ==========================================================================
class TestHeartbeat(UploaderTestBase):

    def test_heartbeat_reports_backlog(self):
        from terminal.uploader import HeartbeatThread
        self.add(7)
        hb = HeartbeatThread(self.store, self.client, self.link, self.cfg)
        hb._beat()

        self.assertEqual(hb.snapshot()["sent"], 1)
        self.assertEqual(hb.snapshot()["advice"]["echo_backlog"], 7)

    def test_heartbeat_is_skipped_while_offline(self):
        from terminal.uploader import HeartbeatThread
        hb = HeartbeatThread(self.store, self.client, self.link, self.cfg)
        self.link.set_manual_offline(True)
        hb._beat()

        self.assertEqual(hb.snapshot()["sent"], 0, "断网时不该每 5 秒撞一次超时")
        self.assertEqual(hb.snapshot()["fail"], 0, "没发就不算失败")


if __name__ == "__main__":       # pragma: no cover
    unittest.main(verbosity=2)
