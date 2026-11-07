# -*- coding: utf-8 -*-
"""阶段 7：U 盘同步 —— 导出、校验、导入、修订，以及"改不出平台看不见的东西"。

这个模块测的是计划书里唯一一条"允许人改数据"的通道，所以用例的重点不在
"改完能不能生效"，而在**改完之后谁看得出来**：

1. 导出包被动过一个字节 → 校验和不一致，当场报出来；
2. 桩号被改 → 整包拒收（里程不能靠 U 盘改，这是数据可信的底线）；
3. 参数被改 → 只生成**修订**，原始值一个字节不动；
4. 改动没带原因/操作人 → 拒绝，修订不允许匿名；
5. 整条链路走完 → 平台侧该记录出现"已修正"，原值仍在，
   点开能看到谁、什么时候、为什么、从多少改到多少。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from common import config, csv_schema, ids
from common.enums import Quality, ReasonCode
from common.logging_setup import quiet
from common.params import PARAM_KEYS
from common.timeutil import now_epoch, now_utc_iso
from server.alarms import AlarmEngine
from server.api import Api
from server import db, seed
from terminal import usb_sync
from terminal.store import LocalStore

quiet()

DEVICE = "GHP9015H"


def read_text(path: str) -> str:
    """读源码用。`open(...).read()` 会把文件句柄漏到 GC 才关，测试里会刷警告。"""
    with open(path, encoding="utf-8") as fp:
        return fp.read()


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


class UsbTestBase(unittest.TestCase):
    """一台终端（临时库 + 临时 U 盘）和一个平台（临时库 + 3 天种子）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.frames_dir = os.path.join(root, "frames")
        self.usb_dir = os.path.join(root, "usb")
        os.makedirs(self.frames_dir, exist_ok=True)
        os.makedirs(self.usb_dir, exist_ok=True)

        self.store = LocalStore(path=os.path.join(root, "local.db"),
                                frames_dir=self.frames_dir)
        self.cfg = config.load_config()
        self.cfg["device"]["device_code"] = "GHP9015H"   # 测试造帧固定此编号：与现场配置隔离
        self.pile = seed.DEFAULT_PILE
        self.pile_ids = seed.pile_list(self.cfg)

        # 3 条已上传（修订要有目标）+ 1 条欠传（U 盘导出最常见的场景：
        # 隧道里传不上去的那几条，得靠盘带出去）
        for i in range(4):
            self.store.append(self.frame(i + 1))

        # 平台侧：种子数据 + 上面这 3 条（走真实上传路径，才谈得上"修订目标已存在"）
        self.conn = db.init_db(os.path.join(root, "platform.sqlite3"))
        self.api = Api(self.cfg, AlarmEngine(config.load_thresholds()))
        with db.transaction(self.conn):
            seed.ensure_piles(self.conn, self.cfg)
            seed.ensure_device(self.conn, self.cfg)
        self.upload_all(self.store.export_rows(scope="all")[:-1])

    def tearDown(self) -> None:
        db.close_conn()
        try:
            self.store.close()
        except Exception:                       # noqa: BLE001 - 临时目录要删，尽力而为
            pass
        self.tmp.cleanup()

    # ---- 构造器 ----
    def frame(self, seq: int, **over) -> dict:
        ts_ms = int(now_epoch() * 1000) + seq * 2000
        rec = {
            "record_id": ids.frame_record_id(DEVICE, ts_ms, seq),
            "device_code": DEVICE,
            "ts_device": now_utc_iso(),
            "ts_epoch": ts_ms,
            "seq": seq,
            "pile_id": self.pile,
            "quality": int(Quality.OK),
            "source": "can",
            "values": _values(**over),
        }
        rec["row_hash"] = csv_schema.compute_row_hash(
            {**{c: rec.get(c, "") for c in csv_schema.META_COLUMNS}, **rec["values"]})
        return rec

    def upload_all(self, rows: list[dict]) -> dict:
        """把给定记录按真实批次送上平台，并把本地状态推到"已上传"。"""
        if not rows:
            return {}
        rids = [r["record_id"] for r in rows]
        body = {"protocol": "1.0", "batch_id": ids.make_batch_id(DEVICE, rids),
                "device_code": DEVICE, "kind": "frame",
                "created_at": now_utc_iso(), "records": rows}
        status, receipt = self.api.upload(self.conn, body)
        self.assertEqual(status, 200, receipt)
        for r in receipt["results"]:
            self.assertEqual(r["result"], "accepted", r)
        with db.transaction(self.conn):
            self.store.conn.execute(
                "UPDATE outbox SET status='uploaded' WHERE record_id IN (%s)"
                % ",".join("?" * len(rids)), rids)
        return receipt

    def export(self, scope: str = "all", dest: str | None = None) -> dict:
        return usb_sync.export_bundle(
            self.store.export_rows(scope=scope), DEVICE,
            dest_dir=dest or self.bundle_dir, scope=scope)

    @property
    def bundle_dir(self) -> str:
        return os.path.join(self.usb_dir, "out", "GHP9015H_20260101_080000")

    def platform_frame(self, record_id: str) -> dict:
        """平台侧看到的一帧：帧字段 + 原值/有效值 + 修订历史，摊平成一层。

        接口本身是把这几样包在 `frame` 里的（前端画抽屉要分得开），
        这里摊平只是让断言读起来像"平台怎么看这一帧"，不是换字段名。
        """
        status, body = self.api.frame_detail(self.conn, {}, record_id)
        self.assertEqual(status, 200, body)
        out = dict(body["frame"])
        out["revisions"] = body["revisions"]
        out["corrected"] = body["corrected"]
        return out


# ==========================================================================
# 导出
# ==========================================================================
class ExportTests(UsbTestBase):

    def test_export_writes_csv_manifest_and_readme(self):
        res = self.export()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["exported"], 4)
        for name in (usb_sync.DATA_NAME, usb_sync.MANIFEST_NAME, usb_sync.README_NAME):
            self.assertTrue(os.path.isfile(os.path.join(self.bundle_dir, name)), name)

    def test_pending_scope_exports_only_the_backlog(self):
        """现场最常用的一档：只把传不上去的那几条捎出去。"""
        res = self.export(scope="pending")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["exported"], 1)
        self.assertEqual(res["manifest"]["scope"], "pending")
        self.assertEqual(res["manifest"]["pile_ids"], [self.pile])

    def test_manifest_records_checksum_and_range(self):
        m = self.export()["manifest"]
        self.assertEqual(m["record_count"], 4)
        self.assertEqual(m["device_code"], DEVICE)
        self.assertEqual(m["pile_ids"], [self.pile])
        self.assertEqual(len(m["record_ids"]), 4)
        f = m["files"][0]
        self.assertEqual(f["rows"], 4)
        with open(os.path.join(self.bundle_dir, usb_sync.DATA_NAME), "rb") as fp:
            self.assertEqual(ids.sha256_hex(fp.read()), f["sha256"])
        # 时间范围得是给人看的本地时间，不是 ISO
        self.assertRegex(m["first_local"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_csv_round_trips_through_the_schema(self):
        """导出的 CSV 必须能被 csv_schema 原样读回来 —— 这是导入的前提。"""
        self.export()
        rows = list(csv_schema.iter_rows(os.path.join(self.bundle_dir, usb_sync.DATA_NAME)))
        self.assertEqual(len(rows), 4)
        for raw in rows:
            rec = csv_schema.frame_from_row(raw)
            self.assertEqual(rec["device_code"], DEVICE)
            self.assertEqual(rec["pile_id"], self.pile)
            self.assertTrue(csv_schema.row_is_intact(raw), "刚导出的行摘要就该是对的")

    def test_export_is_chronological(self):
        self.export()
        rows = list(csv_schema.iter_rows(os.path.join(self.bundle_dir, usb_sync.DATA_NAME)))
        ts = [int(r["ts_epoch"]) for r in rows]
        self.assertEqual(ts, sorted(ts), "导出的 CSV 该从早到晚排 —— 给人看的")

    def test_empty_export_is_reported_not_raised(self):
        """没有待传数据是常态，不是异常。"""
        self.store.conn.execute("UPDATE outbox SET status='uploaded'")
        res = usb_sync.export_bundle([], DEVICE, dest_dir=self.bundle_dir)
        self.assertFalse(res["ok"])
        self.assertIn("没有可导出", res["message"])

    def test_scope_rejects_nonsense(self):
        with self.assertRaises(ValueError):
            self.store.export_rows(scope="随便写的范围")


# ==========================================================================
# 校验与比对
# ==========================================================================
class InspectTests(UsbTestBase):

    def test_untouched_bundle_reports_no_changes(self):
        self.export()
        rep = usb_sync.inspect_bundle(self.store, self.bundle_dir,
                                      device_code=DEVICE, pile_ids=self.pile_ids)
        self.assertEqual(rep["counts"]["same"], 4)
        self.assertEqual(rep["changed"], 0)
        self.assertEqual(rep["errors"], [])

    def test_edited_file_raises_a_warning_but_still_imports(self):
        """改过值 → 校验和对不上，但是**警告**，不是拒绝。

        这条路本来就是"导出→人拿 Excel 改→导回来"，改了值校验和必然对不上。
        把它当致命错误，这条通道就永远走不通了。它要起的作用是
        "这份包被动过"这句提示 —— 拦不拦由逐行校验决定。
        """
        self.export()
        usb_sync.simulate_edit(self.bundle_dir, key="cylinderPumpingPressure", delta=2.0, count=1)
        rep = usb_sync.inspect_bundle(self.store, self.bundle_dir,
                                      device_code=DEVICE, pile_ids=self.pile_ids)
        self.assertEqual(rep["errors"], [])
        self.assertTrue(any("被改过" in w for w in rep["warnings"]), rep["warnings"])
        self.assertEqual(rep["changed"], 1)
        self.assertEqual(rep["counts"]["same"], 3)
        self.assertFalse(rep["changes"][0]["intact"],
                         "改动过的行，行摘要就该对不上 —— 这是最有说服力的一处证据")

    def test_truncated_file_is_refused(self):
        """拷到一半拔盘：文件还在，但内容不全。这种必须拦。"""
        self.export()
        path = os.path.join(self.bundle_dir, usb_sync.DATA_NAME)
        with open(path, "rb") as fp:
            blob = fp.read()
        with open(path, "wb") as fp:
            fp.write(blob[:len(blob) // 2])          # 从中间砍断
        rep = usb_sync.inspect_bundle(self.store, self.bundle_dir,
                                      device_code=DEVICE, pile_ids=self.pile_ids)
        self.assertFalse(rep["ok"])
        self.assertTrue(rep["errors"], "半个文件必须报错")

    def test_missing_manifest_is_refused(self):
        self.export()
        os.remove(os.path.join(self.bundle_dir, usb_sync.MANIFEST_NAME))
        rep = usb_sync.inspect_bundle(self.store, self.bundle_dir,
                                      device_code=DEVICE, pile_ids=self.pile_ids)
        self.assertFalse(rep["ok"])
        self.assertIn("不是一个完整的导出包", rep["errors"][0])

    def test_manifest_listing_a_missing_file_is_refused(self):
        """清单里写着有 frames.csv 而它不在 —— 数据不全，没法比对。"""
        self.export()
        os.remove(os.path.join(self.bundle_dir, usb_sync.DATA_NAME))
        rep = usb_sync.inspect_bundle(self.store, self.bundle_dir,
                                      device_code=DEVICE, pile_ids=self.pile_ids)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("不存在" in e for e in rep["errors"]), rep["errors"])

    def test_pile_change_is_refused(self):
        """桩号是底线：U 盘能改测量值，改不了里程。"""
        self.export()
        path = os.path.join(self.bundle_dir, usb_sync.DATA_NAME)
        rows = list(csv_schema.iter_rows(path))
        rows[0]["pile_id"] = self.pile_ids[0]
        self._rewrite(rows, keep_hash=True)
        rep = usb_sync.inspect_bundle(self.store, self.bundle_dir,
                                      device_code=DEVICE, pile_ids=self.pile_ids)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("桩号不能从 U 盘改" in e for e in rep["errors"]),
                        rep["errors"])

    def test_foreign_record_is_refused(self):
        """别的车导出、或者本机库清过之后的包，不能混进来。"""
        self.export()
        path = os.path.join(self.bundle_dir, usb_sync.DATA_NAME)
        rows = list(csv_schema.iter_rows(path))
        rows[0]["record_id"] = ids.frame_record_id(DEVICE, 1, 999999)
        self._rewrite(rows, keep_hash=True)
        rep = usb_sync.inspect_bundle(self.store, self.bundle_dir,
                                      device_code=DEVICE, pile_ids=self.pile_ids)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("在本机库里不存在" in e for e in rep["errors"]),
                        rep["errors"])

    def test_pile_not_issued_is_refused(self):
        """桩号在清单外（平台撤过号）也拦在本机，不必等平台拒收。"""
        self.export()
        path = os.path.join(self.bundle_dir, usb_sync.DATA_NAME)
        rows = list(csv_schema.iter_rows(path))
        rows[0]["pile_id"] = "ZK999+999"
        self._rewrite(rows, keep_hash=True)
        rep = usb_sync.inspect_bundle(self.store, self.bundle_dir,
                                      device_code=DEVICE, pile_ids=self.pile_ids)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("不在平台下发的清单里" in e for e in rep["errors"]),
                        rep["errors"])

    def test_non_numeric_value_is_refused(self):
        self.export()
        path = os.path.join(self.bundle_dir, usb_sync.DATA_NAME)
        rows = list(csv_schema.iter_rows(path))
        rows[1]["cylinderPumpingPressure"] = "正常"
        self._rewrite(rows, keep_hash=True)
        rep = usb_sync.inspect_bundle(self.store, self.bundle_dir,
                                      device_code=DEVICE, pile_ids=self.pile_ids)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("不是数值" in e for e in rep["errors"]), rep["errors"])

    def test_other_device_is_refused(self):
        self.export()
        path = os.path.join(self.bundle_dir, usb_sync.DATA_NAME)
        rows = list(csv_schema.iter_rows(path))
        for r in rows:
            r["device_code"] = "GHP0001"
        self._rewrite(rows, keep_hash=True)
        rep = usb_sync.inspect_bundle(self.store, self.bundle_dir,
                                      device_code=DEVICE, pile_ids=self.pile_ids)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("不是本机" in e for e in rep["errors"]), rep["errors"])

    def _rewrite(self, rows: list[dict], *, keep_hash: bool) -> None:
        """把改过的行写回 CSV。`keep_hash=True` 时故意保留旧摘要，

        这正是"人拿 Excel 改了一下"之后的真实样子：内容变了，摘要还是老的。
        导入侧看得出来的正是这个差。
        """
        path = os.path.join(self.bundle_dir, usb_sync.DATA_NAME)
        with open(path, "w", encoding=csv_schema.ENCODING,
                  newline=csv_schema.NEWLINE) as fp:
            w = csv_schema.make_writer(fp)
            w.writeheader()
            for r in rows:
                if not keep_hash:
                    r[csv_schema.HASH_COLUMN] = csv_schema.compute_row_hash(r)
                w.writerow(r)


# ==========================================================================
# 导入：生成修订
# ==========================================================================
class ImportTests(UsbTestBase):

    def test_changed_value_becomes_a_revision(self):
        self.export()
        usb_sync.simulate_edit(self.bundle_dir, key="cylinderPumpingPressure", delta=2.5, count=2)
        res = usb_sync.import_bundle(self.store, self.bundle_dir, device_code=DEVICE,
                                     pile_ids=self.pile_ids, operator="张工",
                                     reason_code=ReasonCode.VALUE_DISTORTED.value,
                                     note="堵管时压力表跳了一下，按记录仪改正")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["created"], 2)
        self.assertEqual(res["same"], 2)

        rows = self.store.revisions(limit=10)
        self.assertEqual(len(rows), 2)
        # 按目标查找，不按下标取：同一批的两条修订 created_at 可能落在
        # 同一微秒上，`revisions()` 的顺序此时不该被当成契约来用。
        by_target = {r["target_id"]: r for r in rows}
        self.assertEqual(set(by_target), {ch["record_id"] for ch in res["changes"]})
        r = by_target[res["changes"][0]["record_id"]]
        self.assertEqual(r["operator_name"], "张工")
        self.assertEqual(r["reason_code"], "VALUE_DISTORTED")
        self.assertIn("cylinderPumpingPressure", json.loads(r["patch_json"]))

    def test_import_does_not_touch_the_original_row(self):
        """修订是新增一条，不是覆盖。终端侧的原始值一个字节都不能动。"""
        self.export()
        before = self.store.get_record(self._first_rid())
        usb_sync.simulate_edit(self.bundle_dir, key="cylinderPumpingPressure", delta=3.0, count=1)
        usb_sync.import_bundle(self.store, self.bundle_dir, device_code=DEVICE,
                               pile_ids=self.pile_ids, operator="张工")
        after = self.store.get_record(self._first_rid())
        self.assertEqual(before["values"], after["values"])
        self.assertEqual(before["row_hash"], after["row_hash"])

    def test_import_requires_operator_and_reason(self):
        self.export()
        usb_sync.simulate_edit(self.bundle_dir, key="pumpingSpeed", delta=1.0, count=1)
        with self.assertRaises(ValueError):
            usb_sync.import_bundle(self.store, self.bundle_dir, device_code=DEVICE,
                                   pile_ids=self.pile_ids, operator="  ")
        with self.assertRaises(ValueError):
            usb_sync.import_bundle(self.store, self.bundle_dir, device_code=DEVICE,
                                   pile_ids=self.pile_ids, operator="张工",
                                   reason_code="PILE_NOT_ISSUED")   # 拒收原因码，不是修订原因

    def test_one_bad_row_blocks_the_whole_import(self):
        """有一行不合法就整包不动 —— 宁可少改，不要错改。"""
        self.export()
        usb_sync.simulate_edit(self.bundle_dir, key="cylinderPumpingPressure", delta=2.0, count=2)
        path = os.path.join(self.bundle_dir, usb_sync.DATA_NAME)
        rows = list(csv_schema.iter_rows(path))
        rows[0]["pile_id"] = "ZK999+999"
        self._rewrite(rows)
        res = usb_sync.import_bundle(self.store, self.bundle_dir, device_code=DEVICE,
                                     pile_ids=self.pile_ids, operator="张工")
        self.assertFalse(res["ok"])
        self.assertEqual(res["created"], 0)
        self.assertEqual(self.store.revisions(), [])

    def test_unchanged_bundle_creates_nothing(self):
        self.export()
        res = usb_sync.import_bundle(self.store, self.bundle_dir, device_code=DEVICE,
                                     pile_ids=self.pile_ids, operator="张工")
        self.assertTrue(res["ok"])
        self.assertEqual(res["created"], 0)
        self.assertIn("没有检测到改动", res["message"])

    def test_full_diff_is_reported_for_the_ui(self):
        self.export()
        usb_sync.simulate_edit(self.bundle_dir, key="acceleratorProportion", delta=7.0, count=1)
        rep = usb_sync.inspect_bundle(self.store, self.bundle_dir,
                                      device_code=DEVICE, pile_ids=self.pile_ids)
        ch = rep["changes"][0]
        self.assertEqual(ch["pile_id"], self.pile)
        self.assertEqual(len(ch["diffs"]), 1)
        d = ch["diffs"][0]
        for k in ("key", "name", "unit", "old", "new", "old_text", "new_text"):
            self.assertIn(k, d)
        self.assertAlmostEqual(d["new"] - d["old"], 7.0, places=3)
        self.assertFalse(ch["intact"], "改过之后行摘要就该对不上了")

    def test_simulate_edit_refuses_frozen_fields(self):
        self.export()
        for key in ("pile_id", "record_id", "ts_device", "seq"):
            with self.assertRaises(ValueError):
                usb_sync.simulate_edit(self.bundle_dir, key=key)

    def test_edited_value_stays_inside_the_scale(self):
        """演示助手把值夹在正常量程内 —— 它要演示的是"改动"，
        不是"改成非法值"（那种会被平台按 RANGE 拒收，故事就跑偏了）。"""
        self.export()
        out = usb_sync.simulate_edit(self.bundle_dir, key="cylinderPumpingPressure",
                                     delta=999.0, count=3)
        self.assertEqual(out["rows"], 3)
        for d in out["detail"]:
            self.assertLessEqual(d["new"], 16.0)

    def _first_rid(self) -> str:
        return self.store.export_rows(scope="all")[0]["record_id"]

    def _rewrite(self, rows: list[dict]) -> None:
        path = os.path.join(self.bundle_dir, usb_sync.DATA_NAME)
        with open(path, "w", encoding=csv_schema.ENCODING,
                  newline=csv_schema.NEWLINE) as fp:
            w = csv_schema.make_writer(fp)
            w.writeheader()
            for r in rows:
                w.writerow(r)


# ==========================================================================
# 端到端：改 → 传 → 平台上出现"修正"标记与原值
# ==========================================================================
class EndToEndTests(UsbTestBase):
    """计划书 5.4 的验收线：导出 → 改 → 导入 → 重传 → 平台可追溯。"""

    def test_platform_shows_correction_and_keeps_the_original(self):
        rid = self.store.export_rows(scope="all")[0]["record_id"]
        before = self.platform_frame(rid)
        self.assertFalse(before["corrected"])
        self.assertEqual(before["revisions"], [])

        self.export()
        usb_sync.simulate_edit(self.bundle_dir, key="cylinderPumpingPressure", delta=2.0, count=1)
        res = usb_sync.import_bundle(
            self.store, self.bundle_dir, device_code=DEVICE, pile_ids=self.pile_ids,
            operator="李监理", reason_code=ReasonCode.SENSOR_FAULT.value,
            note="压力传感器接头松动，按记录仪数据修正")
        self.assertEqual(res["created"], 1)

        # 修订批走真实上传路径
        rev = self.store.revisions(limit=1)[0]
        body = {"protocol": "1.0",
                "batch_id": ids.make_batch_id(DEVICE, [rev["revision_id"]]),
                "device_code": DEVICE, "kind": "revision",
                "created_at": now_utc_iso(),
                "records": [{
                    "record_id": rev["revision_id"],
                    "target_record_id": rid,
                    "patch": json.loads(rev["patch_json"]),
                    "reason_code": rev["reason_code"],
                    "reason_note": rev["reason_note"],
                    "operator_name": rev["operator_name"],
                    "ts_device": rev["ts_device"],
                }]}
        status, receipt = self.api.upload(self.conn, body)
        self.assertEqual(status, 200, receipt)
        self.assertEqual(receipt["results"][0]["result"], "accepted", receipt)

        after = self.platform_frame(rid)
        # 三份并存：原始值没动、有效值变了、修订历史留痕
        self.assertTrue(after["corrected"])
        self.assertEqual(after["raw"]["cylinderPumpingPressure"],
                         before["raw"]["cylinderPumpingPressure"])
        self.assertAlmostEqual(after["values"]["cylinderPumpingPressure"],
                               before["values"]["cylinderPumpingPressure"] + 2.0, places=6)
        self.assertEqual(len(after["revisions"]), 1)
        rv = after["revisions"][0]
        self.assertEqual(rv["operator_name"], "李监理")
        self.assertEqual(rv["reason_label"], "传感器故障")
        self.assertIn("压力传感器", rv["reason_note"])

    def test_revision_on_a_missing_record_is_rejected(self):
        """目标记录还没上平台就发修订，平台必须拒收而不是默默收下。

        终端侧的次序（欠账优先、修订等帧传完）就是为了避开这种情况。
        """
        rec = self.frame(99)          # 这条从没上传过
        rev_id = ids.revision_record_id(DEVICE, int(now_epoch() * 1000), 1)
        body = {"protocol": "1.0",
                "batch_id": ids.make_batch_id(DEVICE, [rev_id]),
                "device_code": DEVICE, "kind": "revision",
                "created_at": now_utc_iso(),
                "records": [{
                    "record_id": rev_id,
                    "target_record_id": rec["record_id"],       # 从没上传过
                    "patch": {"cylinderPumpingPressure": 12.0},
                    "reason_code": "VALUE_DISTORTED",
                    "operator_name": "张工",
                    "ts_device": rec["ts_device"],
                }]}
        status, receipt = self.api.upload(self.conn, body)
        self.assertEqual(status, 200)
        self.assertEqual(receipt["results"][0]["result"], "rejected")
        self.assertIn("不存在于平台", receipt["results"][0]["message"])


# ==========================================================================
# U 盘目录浏览与说明文件
# ==========================================================================
class BundleListingTests(UsbTestBase):

    def test_list_finds_the_latest_bundle(self):
        self.export()
        self.export(dest=os.path.join(self.usb_dir, "out", "GHP9015H_20260102_090000"))
        found = usb_sync.list_bundles(self.usb_dir, limit=5)
        names = [f["name"] for f in found]
        self.assertIn("GHP9015H_20260101_080000", names)
        self.assertIn("GHP9015H_20260102_090000", names)
        # 不能把 out 目录本身也当成一个包列出来
        self.assertNotIn("out", names)
        self.assertEqual(names[0], "GHP9015H_20260102_090000", "新的该排在前面")
        top = found[0]
        self.assertEqual(top["record_count"], 4)
        self.assertTrue(top["has_manifest"] and top["has_data"])

    def test_latest_bundle_points_at_a_real_export(self):
        self.assertEqual(usb_sync.latest_bundle(self.usb_dir), "")
        self.export()
        self.assertEqual(os.path.abspath(usb_sync.latest_bundle(self.usb_dir)),
                         os.path.abspath(self.bundle_dir))

    def test_readme_is_written_with_a_bom(self):
        """说明文件是给现场的人看的，记事本打开不能是乱码。"""
        self.export()
        path = os.path.join(self.bundle_dir, usb_sync.README_NAME)
        with open(path, "rb") as fp:
            raw = fp.read()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "说明.txt 缺 BOM")
        text = raw.decode(csv_schema.ENCODING)
        self.assertIn("桩号", text)
        self.assertIn("row_hash", text)

    def test_reason_choices_are_revision_reasons_only(self):
        codes = {r["code"] for r in usb_sync.reasons()}
        self.assertIn("VALUE_DISTORTED", codes)
        self.assertNotIn("PILE_NOT_ISSUED", codes, "拒收原因码不该出现在修订下拉框里")
        for r in usb_sync.reasons():
            self.assertTrue(r["label"])


# ==========================================================================
# 与终端其余部件的关系
# ==========================================================================
class WiringTests(unittest.TestCase):
    """U 盘这条路不能绕过既有的次序与留痕规则。"""

    def test_app_exposes_the_usb_actions(self):
        from terminal.app import TerminalApp
        for name in ("usb_export", "usb_import", "usb_inspect"):
            self.assertTrue(callable(getattr(TerminalApp, name, None)), name)

    def test_webui_has_the_usb_routes(self):
        path = os.path.join(config.ROOT, "terminal", "webui.py")
        src = read_text(path)
        for route in ("/api/usb", "/api/usb/export", "/api/usb/inspect",
                      "/api/usb/import", "/api/usb/edit", "/api/logs"):
            self.assertIn(f'"{route}"', src, f"webui 里没有 {route}")

    def test_usb_code_never_writes_frames_directly(self):
        """usb_sync 只能入队修订，不许碰 frames/outbox 的值。"""
        src = read_text(os.path.join(config.ROOT, "terminal", "usb_sync.py"))
        for bad in ("UPDATE outbox", "INSERT INTO outbox", "DELETE FROM outbox",
                    "UPDATE frames"):
            self.assertNotIn(bad, src, f"usb_sync 里不该出现 {bad}")

    def test_uploader_sends_revisions_only_after_the_backlog_clears(self):
        """次序规则写在 uploader 里，这里读代码确认它还在。

        这条不是形式主义：修订批若抢在数据前面发，平台会以
        "目标记录不存在"整批拒收，而拒收是永久失败 —— 终端界面上
        会一直挂着几条传不上去的修订，演示时很难解释。
        """
        src = read_text(os.path.join(config.ROOT, "terminal", "uploader.py"))
        self.assertIn("self.store.pending_count() == 0", src)
