# -*- coding: utf-8 -*-
"""现场录入工点 ID / 设备编号：平台核对 + 终端生效。

这套用例盯的是"录得进去"和"录不进去"这两件事**都**成立：

* 工点不在平台档案里 —— 必须录不进去，而且要把平台的已知工点报回来
  （现场需要知道该填什么，不是只知道"错了"）；
* 设备编号 —— 平台没见过的可以自报，但要把"这台车原来在哪个工点"讲清楚，
  因为那正是转场时要人确认的东西；
* **换编号前必须先结清欠账** —— 这条最要紧，也最容易在重构里被抹掉：
  `record_id` / `batch_id` / `event_id` 里都嵌着设备编号，旧编号下采到的数据
  只有在旧编号下上传才对得上（见 terminal/app.py 的 identity_pending）。

平台侧用真的 `Api`（不是替身），终端侧用真的 `TerminalApp`，
中间只把 HTTP 那一段换成进程内调用 —— 这样测的是"两端确实对得上"，
而不是"两边各自符合自己的假设"。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
# `InProcessClient` 借自 test_uploader：它把真实 `server.api.Api` 包成
# `PlatformClient` 的形状，终端对接的是**真处理程序**而不是替身。
# 直接 import 而不是抄一份 —— 两份替身早晚会走样，那时测的就不是同一个平台了。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import config
from common.timeutil import now_epoch
from server import db, seed
from server.alarms import AlarmEngine
from server.api import Api
from terminal.app import TerminalApp

from test_uploader import InProcessClient

SITE_A = "DQTYX-ZK"          # 大丘田隧道右线（演示设备登记在这里）
SITE_B = "SML-ZK"            # 杉木岭隧道右线（转场的目的地）
DEVICE = "GHP9015H"


# ==========================================================================
# 平台侧：核对接口 + 心跳里的工点对齐
# ==========================================================================
class PlatformCheckTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init_db(os.path.join(self.tmp.name, "platform.sqlite3"))
        self.cfg = config.load_config()
        self.cfg["device"]["device_code"] = "GHP9015H"   # 测试造帧固定此编号：与现场配置隔离
        self.api = Api(self.cfg, AlarmEngine(config.load_thresholds()))
        with db.transaction(self.conn):
            seed.ensure_sites(self.conn, self.cfg)
            seed.ensure_piles(self.conn, self.cfg)
            seed.ensure_device(self.conn, self.cfg)

    def tearDown(self) -> None:
        db.close_conn()
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    def test_unknown_site_is_refused_with_the_known_list(self):
        """工点只能引用不能编造 —— 拒掉，并且告诉现场平台上有什么。"""
        status, body = self.api.identity_check(
            self.conn, {"site_id": ["DQTYX-XX"], "device_code": [DEVICE]})
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "SITE_NOT_ISSUED")
        known = {s["site_id"] for s in body["known_sites"]}
        self.assertEqual(known, {SITE_A, SITE_B})

    def test_unknown_device_is_not_refused(self):
        """设备是硬件，第一次接入时平台无从预知 —— 记录状态，但不拦。"""
        status, body = self.api.identity_check(
            self.conn, {"site_id": [SITE_B], "device_code": ["GHP0001"]})
        self.assertEqual(status, 200)
        self.assertFalse(body["device"]["known"])
        self.assertIsNone(body["move"])
        self.assertTrue(any("没有设备" in w for w in body["warnings"]))

    def test_known_device_elsewhere_reports_a_move(self):
        """转场是常态，但要让人看见"从哪儿来"。"""
        status, body = self.api.identity_check(
            self.conn, {"site_id": [SITE_B], "device_code": [DEVICE]})
        self.assertEqual(status, 200)
        self.assertTrue(body["device"]["known"])
        self.assertEqual(body["move"]["from_site_id"], SITE_A)
        self.assertEqual(body["site"]["site_name"], "杉木岭隧道右线")
        self.assertEqual(body["site"]["pile_count"], 201)
        self.assertTrue(body["site"]["first_pile"].startswith("ZK106"))

    def test_check_is_read_only(self):
        """核对是给人看的一步，人可能看完就反悔 —— 它不许改库。"""
        before = self.conn.execute(
            "SELECT site_id, device_name FROM devices WHERE device_code=?",
            (DEVICE,)).fetchone()
        self.api.identity_check(self.conn,
                                {"site_id": [SITE_B], "device_code": [DEVICE]})
        after = self.conn.execute(
            "SELECT site_id, device_name FROM devices WHERE device_code=?",
            (DEVICE,)).fetchone()
        self.assertEqual(tuple(before), tuple(after))

    def test_missing_params_are_400(self):
        for q in ({}, {"site_id": [SITE_A]}, {"device_code": [DEVICE]}):
            with self.subTest(q=q):
                status, _ = self.api.identity_check(self.conn, q)
                self.assertEqual(status, 400)

    # ------------------------------------------------------------------
    # 心跳：设备归属靠它持续对齐
    # ------------------------------------------------------------------
    def test_heartbeat_does_not_overwrite_a_registered_devices_site(self):
        """心跳不带工点时，绝不能拿平台配置去覆盖设备现有的归属。

        否则终端刚录完新工点，下一个心跳就被平台改回去了 —— 而且是静默的。
        """
        self.api.heartbeat(self.conn, {"device_code": DEVICE,
                                       "ts_device": "2026-01-01T00:00:00.000+08:00"})
        row = self.conn.execute(
            "SELECT site_id, site_name FROM devices WHERE device_code=?",
            (DEVICE,)).fetchone()
        self.assertEqual(row["site_id"], SITE_A)

    def test_heartbeat_adopts_the_reported_site_when_it_is_in_the_ledger(self):
        self.api.heartbeat(self.conn, {"device_code": DEVICE,
                                       "ts_device": "2026-01-01T00:00:00.000+08:00",
                                       "site_id": SITE_B})
        row = self.conn.execute(
            "SELECT site_id, site_name FROM devices WHERE device_code=?",
            (DEVICE,)).fetchone()
        self.assertEqual(row["site_id"], SITE_B)
        self.assertEqual(row["site_name"], "杉木岭隧道右线")

    def test_heartbeat_ignores_a_site_that_is_not_in_the_ledger(self):
        """自报一个不存在的工点：不报错，也不写进去 —— 归属留在原处。"""
        status, _ = self.api.heartbeat(
            self.conn, {"device_code": DEVICE,
                        "ts_device": "2026-01-01T00:00:00.000+08:00",
                        "site_id": "不存在的工点"})
        self.assertEqual(status, 200)
        row = self.conn.execute(
            "SELECT site_id FROM devices WHERE device_code=?",
            (DEVICE,)).fetchone()
        self.assertEqual(row["site_id"], SITE_A)

    def test_heartbeat_does_not_rename_a_known_device(self):
        """设备名是平台台账，设备说什么就是什么的话台账就没意义了。"""
        self.api.heartbeat(self.conn, {"device_code": DEVICE,
                                       "ts_device": "2026-01-01T00:00:00.000+08:00"})
        row = self.conn.execute(
            "SELECT device_name FROM devices WHERE device_code=?",
            (DEVICE,)).fetchone()
        self.assertEqual(row["device_name"], self.cfg["device"]["device_name"])

    def test_pile_version_is_per_site(self):
        """清单版本按工点算 —— 否则另一个工点补发桩号会让本工点每 5 秒重拉一次。"""
        a = self.api._pile_version(self.conn, SITE_A)
        self.assertTrue(a)
        self.conn.execute("UPDATE piles SET issued_at='2099-01-01T00:00:00.000+00:00'"
                          " WHERE site_id=?", (SITE_B,))
        self.assertEqual(self.api._pile_version(self.conn, SITE_A), a)
        self.assertNotEqual(self.api._pile_version(self.conn, SITE_B), a)


# ==========================================================================
# 种子：两个演示工点
# ==========================================================================
class SiteSeedTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.init_db(os.path.join(self.tmp.name, "platform.sqlite3"))
        self.cfg = config.load_config()
        self.cfg["device"]["device_code"] = "GHP9015H"   # 测试造帧固定此编号：与现场配置隔离

    def tearDown(self) -> None:
        db.close_conn()
        self.tmp.cleanup()

    def test_the_two_demo_sites_have_disjoint_piles(self):
        """演示工点的桩号必须互不重叠。

        `piles.pile_id` 是全局主键（不是 (site_id, pile_id)），里程撞上就会
        算出一模一样的桩号，后一个工点的会被 `INSERT OR IGNORE` **静默丢掉** ——
        表现是"这个工点的清单莫名少了一半"，很难查。这条用例把它钉住。
        """
        with db.transaction(self.conn):
            seed.ensure_sites(self.conn, self.cfg)
            seed.ensure_piles(self.conn, self.cfg)
        rows = self.conn.execute(
            "SELECT site_id, COUNT(*) AS n FROM piles GROUP BY site_id").fetchall()
        per_site = {r["site_id"]: r["n"] for r in rows}
        self.assertEqual(len(per_site), 2, f"两个演示工点都要有桩号：{per_site}")
        for sid, n in per_site.items():
            self.assertEqual(n, 201, f"{sid} 的桩号数不对：{n}（撞车会变少）")

    def test_ensure_sites_is_idempotent_and_updatable(self):
        """幂等，但工点名改了要能生效 —— 配置是工点档案的权威来源。"""
        with db.transaction(self.conn):
            seed.ensure_sites(self.conn, self.cfg)
            seed.ensure_sites(self.conn, self.cfg)
        n = self.conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
        self.assertEqual(n, 2)

        renamed = config.load_config()
        renamed["sites"][1]["site_name"] = "杉木岭隧道右线（改名验证）"
        with db.transaction(self.conn):
            seed.ensure_sites(self.conn, renamed)
        got = self.conn.execute("SELECT site_name FROM sites WHERE site_id=?",
                                (SITE_B,)).fetchone()[0]
        self.assertEqual(got, "杉木岭隧道右线（改名验证）")

    def test_pile_list_stays_the_primary_site_only(self):
        """`pile_list()` 仍只回第一个工点 —— 种子数据、演示脚本、
        `--reset` 之后的历史帧都挂在它上面，多塞一个工点会把演示搞乱。"""
        piles = seed.pile_list(self.cfg)
        self.assertEqual(len(piles), 201)
        self.assertIn(seed.DEFAULT_PILE, piles)


# ==========================================================================
# 终端侧：现场录入生效
# ==========================================================================
class ApplyIdentityTests(unittest.TestCase):
    """真的 `TerminalApp`，只把 HTTP 那一段换成进程内调用。

    配置与数据路径全部改到临时目录 —— 这套用例会写 `site.json` / `config.json`，
    不能写到演示用的那一份上去。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.platform_db = os.path.join(root, "platform.sqlite3")
        self.cfg = config.load_config()
        self.cfg["device"]["device_code"] = "GHP9015H"   # 测试造帧固定此编号：与现场配置隔离

        conn = db.init_db(self.platform_db)
        with db.transaction(conn):
            seed.ensure_sites(conn, self.cfg)
            seed.ensure_piles(conn, self.cfg)
            seed.ensure_device(conn, self.cfg)
        db.close_conn()

        self._patches = [
            mock.patch.object(config, "CONFIG_FILE", os.path.join(root, "config.json")),
            mock.patch.object(config, "SITE_FILE", os.path.join(root, "site.json")),
            mock.patch.object(config, "LOCAL_DB", os.path.join(root, "local.db")),
            mock.patch.object(config, "FRAMES_DIR", os.path.join(root, "frames")),
        ]
        for p in self._patches:
            p.start()
        config.write_json(config.CONFIG_FILE, {"device": dict(self.cfg["device"])})

        self.app = TerminalApp(self.cfg, with_upload=False)
        self.platform = InProcessClient(self.platform_db, self.cfg)
        # 全部换成进程内客户端：这些用例不该碰真网络。
        self.app.client = self.platform
        self.app.piles.client = self.platform
        self.app.uploader.client = self.platform
        self.app.hb.client = self.platform

        self.app.piles.refresh(force=True)
        self.app.remember_pile(initial=True)

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        try:
            self.app.store.close()
        except Exception:                     # pragma: no cover
            pass
        self.tmp.cleanup()

    # ---- 小工具 ----
    def _audit_count(self, kind: str) -> int:
        return self.app.store.conn.execute(
            "SELECT COUNT(*) AS n FROM audit_queue WHERE kind=?", (kind,)).fetchone()["n"]

    def _last_record(self) -> dict:
        return dict(self.app.store.conn.execute(
            "SELECT * FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone())

    def _tick(self) -> None:
        self.app.acq._tick(now_epoch())

    # ------------------------------------------------------------------
    def test_apply_swaps_every_copy_of_the_device_code(self):
        """`device_code` 在终端里有 4 份副本，改身份必须一次全换。

        换漏一份的表现是"平台上的设备编号莫名其妙还是旧的"，而且只在那份
        副本负责的那条路径上出现 —— 比如心跳还报着旧编号。
        """
        self.app.apply_identity(SITE_B, "GHP9015H-02")
        for name, obj in (("app", self.app), ("acq", self.app.acq),
                          ("uploader", self.app.uploader), ("hb", self.app.hb)):
            with self.subTest(where=name):
                self.assertEqual(obj.device, "GHP9015H-02")
        self.assertEqual(self.app.hb.site_id, SITE_B)
        self.assertEqual(self.app.piles.site_id, SITE_B)

    def test_a_frame_produced_after_the_swap_carries_the_new_code(self):
        """最能说明问题的一条：换完之后采出来的帧，ID 和字段都是新编号。

        `record_id` 和 `device_code` 必须来自同一个编号 —— 平台按条校验这两者
        一致（`parse_record_id(rid)["device_code"] != batch.device_code` 就整批拒收）。
        """
        self.app.apply_identity(SITE_B, "GHP9015H-02")
        self._tick()
        rec = self._last_record()
        self.assertEqual(rec["device_code"], "GHP9015H-02")
        self.assertTrue(rec["record_id"].startswith("GHP9015H-02:"), rec["record_id"])
        # 桩号也得是**新工点**的，否则一样会被判 PILE_NOT_ISSUED
        self.assertTrue(self.app.piles.known(rec["pile_id"]))
        site = self.app.store.conn.execute(
            "SELECT site_id FROM piles WHERE pile_id=?", (rec["pile_id"],)).fetchone()[0]
        self.assertEqual(site, SITE_B)

    def test_the_pile_list_is_replaced_and_the_current_pile_follows(self):
        """换工点 = 整列换桩号。当前桩号必须跟着走，不能停在旧工点上。"""
        old_pile = self.app.piles.current
        self.assertIn(old_pile, seed.pile_list(self.cfg))

        out = self.app.apply_identity(SITE_B, DEVICE)
        self.assertNotIn(old_pile, self.app.piles.ids(), "旧工点的桩号不该还留在清单里")
        self.assertEqual(len(self.app.piles.ids()), 201)
        self.assertTrue(self.app.piles.current.startswith("ZK106"))
        self.assertEqual(out["to"]["pile_id"], self.app.piles.current)
        self.assertEqual(self.app.store.meta("current_pile"), self.app.piles.current)

    def test_apply_writes_config_and_an_audit_event(self):
        self.app.apply_identity(SITE_B, "GHP9015H-02")

        site = config.load_site()
        self.assertEqual(site["site_id"], SITE_B)
        self.assertEqual(site["site_name"], "杉木岭隧道右线")
        self.assertEqual(site["tunnel"], "杉木岭隧道")

        dev = config.load_config()["device"]
        self.assertEqual(dev["device_code"], "GHP9015H-02")
        # 设备名里嵌着的旧编号要跟着换，否则界面上一直显示一台不存在的车
        self.assertEqual(dev["device_name"], "湿喷台车 GHP9015H-02")

        self.assertEqual(self._audit_count("identity_change"), 1)
        row = self.app.store.conn.execute(
            "SELECT payload_json FROM audit_queue WHERE kind='identity_change'"
        ).fetchone()
        self.assertIn(SITE_A, row["payload_json"])
        self.assertIn(SITE_B, row["payload_json"])

    # ------------------------------------------------------------------
    # 闸门
    # ------------------------------------------------------------------
    def test_apply_is_refused_while_records_are_still_pending(self):
        """**换编号前必须先结清欠账。**

        旧编号下采到的数据，其 record_id 里嵌着旧编号；换了编号再上传，
        平台逐条校验会发现"ID 里的设备"和"批次的设备"对不上，整批拒收。
        所以这时候必须拒绝，而不是让人改完才发现数据传不上去。
        """
        self._tick()                                   # 造一条欠账
        self.assertEqual(self.app.identity_pending()["total"], 1)
        with self.assertRaises(ValueError) as ctx:
            self.app.apply_identity(SITE_B, "GHP9015H-02")
        self.assertIn("欠账", str(ctx.exception))
        # 一个字都没改
        self.assertEqual(self.app.device, DEVICE)
        self.assertEqual(self.app.piles.site_id, SITE_A)
        self.assertEqual(config.load_site()["site_id"], SITE_A)

    def test_apply_is_refused_while_an_audit_is_still_pending(self):
        """留痕事件也算欠账：它挂错设备比没留痕更糟。"""
        self.app.store.enqueue_audit(self.app._audit("manual_upload", {"records": 0}))
        self.assertEqual(self.app.identity_pending()["audits"], 1)
        with self.assertRaises(ValueError):
            self.app.apply_identity(SITE_B, "GHP9015H-02")
        self.assertEqual(self.app.device, DEVICE)

    def test_apply_succeeds_once_the_queues_are_clear(self):
        """把欠账传完就能改 —— 闸门是条件，不是禁止。"""
        self._tick()
        batch = self.app.store.pending_batch()
        self.assertEqual(self.app.uploader._send_frames(), True)
        self.assertTrue(batch)
        self.assertEqual(self.app.identity_pending()["pending"], 0)
        self.app.apply_identity(SITE_B, "GHP9015H-02")
        self.assertEqual(self.app.device, "GHP9015H-02")

    # ------------------------------------------------------------------
    # 拒绝与回滚
    # ------------------------------------------------------------------
    def test_apply_refuses_a_site_the_platform_does_not_know(self):
        with self.assertRaises(ValueError) as ctx:
            self.app.apply_identity("DQTYX-XX", DEVICE)
        msg = str(ctx.exception)
        self.assertIn("DQTYX-XX", msg)
        self.assertIn(SITE_A, msg, "要把平台已建档的工点报出来，现场才知道该填什么")
        self.assertEqual(self.app.piles.site_id, SITE_A)
        self.assertEqual(self.app.device, DEVICE)

    def test_apply_aborts_if_the_new_site_has_no_pile_list(self):
        """新工点没有清单就整个中止 —— 没有清单就切过去，采出来的全会被拒收。"""
        # 撤的是**平台**的清单，不是本地缓存：本地那份刷新时会被整表替换，
        # 只有平台真的一个桩号都没有，才谈得上"拉不到清单"。
        conn = db.connect(self.platform_db)
        try:
            with db.transaction(conn):
                conn.execute("DELETE FROM piles WHERE site_id=?", (SITE_B,))
        finally:
            conn.close()
        with self.assertRaises(ValueError) as ctx:
            self.app.apply_identity(SITE_B, "GHP9015H-02")
        self.assertIn("身份未改动", str(ctx.exception))
        self.assertEqual(self.app.piles.site_id, SITE_A)
        self.assertEqual(self.app.device, DEVICE)
        self.assertTrue(self.app.piles.ready, "旧工点的清单还要能用")
        self.assertTrue(self.app.piles.current.startswith("ZK105"))

    def test_apply_is_refused_when_nothing_changed(self):
        with self.assertRaises(ValueError) as ctx:
            self.app.apply_identity(SITE_A, DEVICE)
        self.assertIn("没有变化", str(ctx.exception))

    def test_apply_refuses_blank_input(self):
        for site, dev in (("", DEVICE), (SITE_B, ""), ("   ", "  ")):
            with self.subTest(site=site, dev=dev):
                with self.assertRaises(ValueError):
                    self.app.apply_identity(site, dev)
        self.assertEqual(self.app.device, DEVICE)


if __name__ == "__main__":        # pragma: no cover
    unittest.main()
