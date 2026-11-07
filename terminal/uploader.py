# -*- coding: utf-8 -*-
"""上传状态机：组批 / 发送 / 回执 / 退避 / 补传排序。

**单 worker 单队列。** 界面上点"立即上传"只往库里写一个意图
（`manual=1, next_attempt_at=0`），绝不在 UI 线程发请求 ——
否则点两下就有两个请求在飞，同一个批次发两次。

一次上传的事务顺序（顺序错了就会丢数据或重数据）：

```
占行(pending→inflight)      ← 归属确定，附带 attempts+1
  ↓
按占到的记录算 batch_id      ← 必须在占行之后算，内容寻址
  ↓
登记批次(sending)
  ↓
发 HTTP
  ↓
拿到合法回执 → 逐条改状态（不丢）
没拿到回执   → 回 pending + 退避（不重：重传算出的 id 还是它）
平台明确拒收 → 永久失败，交人工（不再空转）
```

三个批次的优先级，理由不是"哪个重要"，而是**依赖关系**：

1. **帧数据** —— 一切的基础，而且要尽快把欠账排空
2. **修订批** —— 只有在帧数据不欠账时才发。修订改的是平台上的记录，
   目标记录还没到，平台会回"目标记录不存在"并且**永久拒收**，
   一条人工留痕就白丢了
3. **审计批** —— 排在最后

关于第 3 条要说清楚，免得读代码的人以为出了 bug：**审计批不会插队**。
只要还有帧数据要发，前两步就会把这一轮用掉，审计批一直等到欠账排空才轮到。
这跟"即使数据本体还没传上去，操作本身也要留痕"并不矛盾 —— 留痕留的是
**终端本地**（`audit_queue`，一直在），传上去的时机排在数据后面而已。
一台没有数据要传的终端，审计批是立刻就能发出去的。
"""
from __future__ import annotations

import json
import logging
import threading

from common import config, http_util, ids
from common.enums import BatchStatus, EventKind, ReasonCode
from common.protocol import Batch, Heartbeat, Receipt, bad_result
from common.timeutil import now_epoch, now_utc_iso

log = logging.getLogger("terminal.uploader")

#: 连续这么多轮"有活干但一条也没发出去"，就停下来睡一个周期。
#: 防的是"平台在回一个必然失败的错误、而队列又清不掉"时把 CPU 打满。
_MAX_BARREN_ROUNDS = 3

#: 有能立刻发的活时，两轮之间只等这么久（"补传优先"的节奏）。
_DRAIN_WAIT_S = 0.2


def _rejected_receipt(batch_id: str, record_ids: list[str], code: str,
                      message: str) -> Receipt:
    """给整批构造"全被拒收"的回执。

    平台对**批次级**问题是回 4xx + 一个原因码、没有逐条结果
    （例如 batch_id 算错、整批被判定为篡改）。终端这边要靠它把
    记录标成永久失败，否则这批会永远重传下去。
    """
    return Receipt(
        batch_id=batch_id,
        results=[bad_result(rid, code, message) for rid in record_ids],
        message=message,
    )


class Uploader(threading.Thread):
    """上传 worker。整个进程只有一个。"""

    def __init__(self, store, client, link, cfg: dict | None = None):
        super().__init__(name="uploader", daemon=True)
        self.cfg = cfg or config.load_config()
        self.store = store
        self.client = client
        self.link = link
        self.device = str(self.cfg["device"]["device_code"])
        self.interval = float(self.cfg["upload"]["interval_s"])
        self.max_attempts = int(self.cfg["upload"]["max_attempts"])
        # 叫 `_stop_evt` 不叫 `_stop`：后者会盖住 `threading.Thread._stop()` 这个
        # 解释器内部方法，在 Python ≤3.12 上线程收尾时抛
        # "'Event' object is not callable"。详见 terminal/app.py 里
        # AcquisitionLoop.__init__ 的说明。
        self._stop_evt = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._last = {
            "at": "", "batch_id": "", "kind": "", "count": 0,
            "status": "idle", "message": "", "http_status": 0,
        }
        self._recovered: dict = {}      # 启动时 reconcile 的报告，给界面看

    # ==================================================================
    # 供界面/其他线程调用
    # ==================================================================
    def poke(self) -> None:
        """催一次（点了"立即上传"、恢复联网、改了配置）。"""
        self._wake.set()

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake.set()

    @property
    def last(self) -> dict:
        with self._lock:
            return dict(self._last)

    @property
    def recovered(self) -> dict:
        with self._lock:
            return dict(self._recovered)

    def _set_last(self, **kw) -> None:
        with self._lock:
            self._last.update(kw)

    # ==================================================================
    # 主循环
    # ==================================================================
    def run(self) -> None:
        try:
            self.recover()
        except Exception as exc:  # pragma: no cover - 恢复失败不该拖垮上传
            log.error("启动恢复出错：%s", exc, exc_info=True)

        barren = 0
        try:
            while not self._stop_evt.is_set():
                try:
                    worked, sent = self._cycle()
                except Exception as exc:  # pragma: no cover - 单轮异常不该终止线程
                    log.error("上传循环异常：%s", exc, exc_info=True)
                    worked, sent = False, False

                if sent:
                    barren = 0
                    continue                   # 刚发成功，马上接着发下一批
                if worked:
                    barren += 1
                    if barren < _MAX_BARREN_ROUNDS:
                        continue               # 还有活，但都没发出去，再试一轮
                barren = 0

                # 该睡多久：有**现在就能发**的就连着发，没有就等一个周期。
                # "补传优先"不需要单独的模式开关 —— 取批永远是 (ts_epoch, seq)
                # 升序，欠账天然排在最前面；这里只是把节奏加快。
                #
                # 判据用 `pending_batch(limit=1)` 而不是 `pending_count()`：
                # 后者把退避中的也算进来，会让线程在"要等 60 秒"的退避期里
                # 每 0.2 秒空转一次。前者尊重 next_attempt_at，退避没到就是没活。
                due = bool(self.store.pending_batch(limit=1))
                wait = (_DRAIN_WAIT_S if (due and self.link.should_upload())
                        else self.interval)
                self._wake.wait(wait)
                self._wake.clear()
        finally:
            # 关的是**本线程自己那条连接**，不是关库 —— store 归 App 所有，
            # 采集线程和本地界面还在用它。线程一死，它的 thread-local 连接
            # 就没人管了，只能等 GC 回收。
            self.store.close_thread_conn()

    # ==================================================================
    # 启动恢复
    # ==================================================================
    def recover(self) -> dict:
        """回收上次进程崩溃留下的残局。**启动时第一件事。**

        两件：帧队列的 INFLIGHT 回收 + 用已存回执补齐行状态；
        修订/审计队列的 INFLIGHT 同样回收。

        `timeout_s=0`：**本进程还没发过任何请求**，所以此刻凡是 INFLIGHT 的行，
        按定义都属于上一个已经死掉的进程，一律立即收回。用默认的 120 秒超时
        会让重启后的前两分钟里，那批数据既不上传也不在待传队列里 ——
        操作员重启终端恰恰是为了让数据赶紧上去，这两分钟的空白最难解释。
        """
        rep = self.store.reconcile(timeout_s=0)
        rep.update(self.store.reconcile_queues(timeout_s=0))
        with self._lock:
            self._recovered = rep
        if rep.get("repaired") or rep.get("requeued") or rep.get("lost_batches"):
            log.warning("启动恢复：补齐 %d 条、回收 %d 条、%d 个批次标为丢失",
                        rep.get("repaired", 0), rep.get("requeued", 0),
                        rep.get("lost_batches", 0))
        return rep

    # ==================================================================
    # 一轮
    # ==================================================================
    def _cycle(self) -> tuple[bool, bool]:
        """跑一轮。返回 (有没有找到活, 这批是不是已经了结)。

        第二步的 `sent` 不是"发成功了"，而是**"这批不用再管了"**：
        平台确认收下，或者平台明确拒收（记录已标 REJECTED，不会再被取到）。
        两种情况都该马上接着发下一批。

        反过来，网络失败和本地校验失败都返回 False：那批记录已经退回
        待传队列并带上了退避，立刻重取只会取到同一批或者取空，
        该按正常节奏睡一觉。
        """
        if not self.link.should_upload():
            self._set_last(status="offline", message=self.link.state.label)
            return False, False

        # 1) 帧数据：一切的基础，欠账先排空
        out = self._send_frames()
        if out is not None:
            return True, out

        # 2) 修订批：必须等目标记录先上平台，否则会被永久拒收
        if self.store.pending_count() == 0:
            out = self._send_revisions()
            if out is not None:
                return True, out

        # 3) 审计批：排在最后。与记录本身无依赖关系，但没有数据要传时才轮到它
        out = self._send_audits()
        if out is not None:
            return True, out

        return False, False

    # ------------------------------------------------------------------
    # 帧数据
    # ------------------------------------------------------------------
    def _send_frames(self) -> bool | None:
        """None = 没活干；True = 了结；False = 要重试。"""
        picked = self.store.pending_batch()
        if not picked:
            return None
        # 设备编号**只在开头读一次**，本批从头到尾用这一个值。
        # 读两次的话，现场刚好在这一瞬改了设备编号，就会出现
        # "batch_id 用旧编号算、device_code 用新编号发"的批次 ——
        # 平台按条校验 `record_id` 里的编号与批次的 device_code 是否一致，
        # 对不上就整批拒收，而那批数据在终端看来只是"又被拒了"。
        device = self.device

        # 先占行，再按"真正占到的"这一组算 batch_id。
        # 顺序反过来的话，占行只成功了一部分时，算出来的 id 跟内容对不上，
        # 平台的内容寻址校验会直接回 400，白跑一趟。
        provisional = ids.make_batch_id(device, [r["record_id"] for r in picked])
        record_ids = [r["record_id"] for r in picked]
        owned = self.store.claim(record_ids, provisional)
        if not owned:
            return None                     # 被别人占了（单 worker 下不该发生）
        record_ids = [r["record_id"] for r in owned]
        batch_id = ids.make_batch_id(device, record_ids)

        batch = Batch(batch_id=batch_id, device_code=device,
                      kind=EventKind.FRAME.value, records=owned,
                      note=f"终端第 {owned[0].get('seq', 0)}~{owned[-1].get('seq', 0)} 帧")
        self.store.register_batch(batch_id, device, record_ids,
                                  kind=EventKind.FRAME.value,
                                  content_digest=batch.content_digest())
        return self._transmit(batch, record_ids, kind=EventKind.FRAME.value)

    # ------------------------------------------------------------------
    # 修订批
    # ------------------------------------------------------------------
    def _send_revisions(self) -> bool | None:
        rows = self.store.pending_revisions()
        if not rows:
            return None
        device = self.device               # 理由同 _send_frames：一批只读一次
        rev_ids = [r["revision_id"] for r in rows]
        owned = self.store.claim_queue("revision_queue", rev_ids,
                                       ids.make_batch_id(device, rev_ids))
        if not owned:
            return None
        rev_ids = [r["revision_id"] for r in owned]
        batch_id = ids.make_batch_id(device, rev_ids)
        records = [{
            # 协议里 revision 批的 record_id 位置放的是 revision_id（见 server/api.py）
            "record_id": r["revision_id"],
            "target_record_id": r["target_id"],
            "patch": _loads(r["patch_json"]),
            "reason_code": r["reason_code"],
            "reason_note": r["reason_note"],
            "operator_name": r["operator_name"],
            "ts_device": r["ts_device"],
        } for r in owned]
        batch = Batch(batch_id=batch_id, device_code=device,
                      kind=EventKind.REVISION.value, records=records,
                      note=f"U 盘校对修订 {len(records)} 条")
        self.store.register_batch(batch_id, device, rev_ids,
                                  kind=EventKind.REVISION.value,
                                  content_digest=batch.content_digest())
        return self._transmit(batch, rev_ids, kind=EventKind.REVISION.value)

    # ------------------------------------------------------------------
    # 审计批
    # ------------------------------------------------------------------
    def _send_audits(self) -> bool | None:
        rows = self.store.pending_audits()
        if not rows:
            return None
        device = self.device               # 理由同 _send_frames：一批只读一次
        ev_ids = [r["event_id"] for r in rows]
        owned = self.store.claim_queue("audit_queue", ev_ids,
                                       ids.make_batch_id(device, ev_ids))
        if not owned:
            return None
        ev_ids = [r["event_id"] for r in owned]
        batch_id = ids.make_batch_id(device, ev_ids)
        records = [{
            # `record_id` 必须有，哪怕审计事件本来叫 event_id：
            # 平台对**任何**批次都先做内容寻址校验，而校验只看
            # `records[].record_id`。少这个字段，整批会被判"batch_id 与内容不符"
            # 而无一例外地拒收 —— 审计留痕就一条都上不去了。
            "record_id": r["event_id"],
            "event_id": r["event_id"],
            "kind": r["kind"],
            "ts_device": r["ts_device"],
            "ts_epoch": r["ts_epoch"],
            "payload": _loads(r["payload_json"]),
        } for r in owned]
        batch = Batch(batch_id=batch_id, device_code=device,
                      kind=EventKind.AUDIT.value, records=records,
                      note=f"操作留痕 {len(records)} 条")
        self.store.register_batch(batch_id, device, ev_ids,
                                  kind=EventKind.AUDIT.value,
                                  content_digest=batch.content_digest())
        return self._transmit(batch, ev_ids, kind=EventKind.AUDIT.value)

    # ==================================================================
    # 发送 + 按结果改状态
    # ==================================================================
    def _transmit(self, batch: Batch, record_ids: list[str], *, kind: str) -> bool:
        """发一批并按结果改状态。返回"这批是不是已经了结"。"""
        started = now_epoch()
        self._set_last(at=now_utc_iso(), batch_id=batch.batch_id, kind=kind,
                       count=len(batch.records), status="sending", message="")
        try:
            receipt = self.client.upload(batch)
        except http_util.NetworkError as exc:
            elapsed = int((now_epoch() - started) * 1000)
            return self._on_network_error(batch, record_ids, exc, kind, elapsed)
        except ValueError as exc:
            # 本地就发现不该发（空批次、超上限）—— 这是我们自己的 bug，
            # 记下来并把记录退回待传，但不要静默丢掉。
            # 退回时会带上退避，所以不会变成"取一次失败一次"的空转。
            elapsed = int((now_epoch() - started) * 1000)
            log.error("批次 %s 本地校验失败：%s", batch.batch_id, exc)
            self._release(batch.batch_id, record_ids, f"本地校验失败：{exc}", kind,
                          elapsed)
            self._set_last(status="error", message=str(exc))
            return False

        elapsed = int((now_epoch() - started) * 1000)
        out = self._apply_receipt(batch, receipt, record_ids, kind=kind,
                                  elapsed=elapsed)
        if out["rejected"]:
            log.warning("批次 %s：%d 条被平台拒收（已标为需人工处理）",
                        batch.batch_id, out["rejected"])
        self._set_last(at=now_utc_iso(), batch_id=batch.batch_id, kind=kind,
                       count=len(record_ids), status="ok", http_status=200,
                       message=(f"新增 {out['accepted']} / 重复 {out['duplicated']}"
                                f" / 拒收 {out['rejected']}"))
        return True

    def _apply_receipt(self, batch: Batch, receipt, record_ids: list[str], *,
                       kind: str, elapsed: int, http_status: int = 200) -> dict:
        """把回执落进库里，并记一行上传日志。

        帧批改 `outbox`，修订/审计批改各自的队列表 —— **只有最后改表这一步分岔**。
        回执的形状、幂等要求、计数口径三种批完全一样，所以判断只写一份；
        之前那种"哪里都调 finish_batch"的写法会让队列行占住 INFLIGHT 再也回不来
        （outbox 表里根本没有它们）。
        """
        queue = _queue_of(kind)
        if queue == "outbox":
            out = self.store.finish_batch(batch.batch_id, receipt, kind=kind,
                                          elapsed_ms=elapsed, http_status=http_status)
        else:
            out = self.store.finish_queue(queue, receipt.results,
                                          batch_id=batch.batch_id, elapsed_ms=elapsed)
            # finish_queue 不写上传日志（它的账里没有 http_status 这些），这里补上
            self.store.log_upload(
                batch.batch_id, kind=kind, record_count=len(record_ids),
                status=(BatchStatus.ACKED.value if not out["rejected"]
                        else BatchStatus.PARTIAL.value),
                http_status=http_status, accepted=out["accepted"],
                duplicated=out["duplicated"], rejected=out["rejected"],
                elapsed_ms=elapsed, message=receipt.message)
        out.setdefault("status", "")
        return out

    def _on_network_error(self, batch: Batch, record_ids: list[str],
                          exc: http_util.NetworkError, kind: str,
                          elapsed: int) -> bool:
        """网络/协议错误的分流。**这里是"不丢"和"不空转"的分界线。**"""
        if exc.retryable:
            # 没拿到回执 = 当没送到。回 PENDING，退避后重传。
            #
            # 纯网络错误**永远继续重试**，不设放弃上限：断网三天之后
            # 数据还是要补上去的，那正是这套系统存在的理由。
            # 会一直试下去的代价可控 —— 退避封顶 60 秒。
            self._release(batch.batch_id, record_ids, str(exc), kind, elapsed)
            self._set_last(status="retry", http_status=exc.status,
                           message=str(exc)[:200])
            log.warning("上传失败（将重试）：批次 %s，%s", batch.batch_id, exc)
            return False

        # 平台明确拒收整批。重试一万次也是同样的结果，不能再撞了。
        reason = exc.code or ReasonCode.SCHEMA.value
        message = f"平台拒收整批（HTTP {exc.status} {reason}）：{exc.body.get('error') or exc}"
        if reason == ReasonCode.BATCH_TOO_LARGE.value:
            # 正常路径上摸不到这里：pending_batch 的批量已经
            # min(cfg, MAX_BATCH_RECORDS) 夹过一遍了。真出现说明
            # 配置被改成了超过协议上限，得让人看见。
            log.error("批次 %s 超过协议上限 %d 条 —— 请把"
                      " upload.batch_max_records 改小", batch.batch_id, ids.MAX_BATCH_RECORDS)
        log.error("批次 %s 被平台永久拒收：%s", batch.batch_id, message)

        # 造一份"全被拒收"的回执喂给同一套落库逻辑，好让这些行变成 REJECTED
        # 并停止重试（留在 PENDING 会永远撞同一面墙）。
        receipt = _rejected_receipt(batch.batch_id, record_ids, reason, message)
        self._apply_receipt(batch, receipt, record_ids, kind=kind,
                            elapsed=elapsed, http_status=exc.status)
        self._set_last(status="rejected", http_status=exc.status, message=message[:200])
        return True

    def _release(self, batch_id: str, record_ids: list[str], error: str, kind: str,
                 elapsed: int) -> int:
        """退回待传队列，并按尝试次数自动退避。返回回收了几条。"""
        queue = _queue_of(kind)
        if queue == "outbox":
            n = self.store.release_batch(batch_id, error=error)
        else:
            n = self.store.release_queue(queue, batch_id, error=error)
        self.store.log_upload(batch_id, kind=kind, record_count=len(record_ids),
                              status="network_error", elapsed_ms=elapsed,
                              message=error[:200])
        return n


def _queue_of(kind: str) -> str:
    if kind == EventKind.REVISION.value:
        return "revision_queue"
    if kind == EventKind.AUDIT.value:
        return "audit_queue"
    return "outbox"


def _loads(s: str) -> dict:
    try:
        obj = json.loads(s or "{}")
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


# ==========================================================================
# 心跳
# ==========================================================================
class HeartbeatThread(threading.Thread):
    """每 `heartbeat_interval_s` 上报一次。

    平台靠它判断设备在线；`buffer_backlog` 告诉平台"设备在线但数据滞后"——
    这两种情况在界面上要分开显示：一种是设备没了，一种是数据没上来。
    """

    def __init__(self, store, client, link, cfg: dict | None = None):
        super().__init__(name="heartbeat", daemon=True)
        self.cfg = cfg or config.load_config()
        self.store = store
        self.client = client
        self.link = link
        self.device = str(self.cfg["device"]["device_code"])
        #: 设备当前所在工点，随心跳上报 —— 平台靠它持续对齐设备归属。
        #: 与 `device` 一样是运行期可变的（现场在终端上改工点，见
        #: terminal/app.py 的 apply_identity），所以这里存的是副本，
        #: 由 apply_identity 统一同步。
        self.site_id = str(config.load_site().get("site_id", ""))
        self.interval = float(self.cfg["acquisition"]["heartbeat_interval_s"])
        # 叫 `_stop_evt` 不叫 `_stop` —— 跟下面 `_started` 是同一类坑，见它的说明。
        self._stop_evt = threading.Event()
        # 不叫 `_started`：那是 threading.Thread 的私有属性（一个 Event），
        # 被这里覆盖成时间戳之后 `Thread.start()` 会直接抛 AttributeError。
        self._boot_epoch = now_epoch()
        self._sent = 0
        self._fail = 0
        self._last_reply: dict = {}
        self._advice: dict = {}          # 平台回告（桩号清单版本、建议间隔）

    def run(self) -> None:
        try:
            while not self._stop_evt.is_set():
                self._beat()
                self._stop_evt.wait(self.interval)
        finally:
            self.store.close_thread_conn()

    def _beat(self) -> None:
        hb = Heartbeat(
            device_code=self.device,
            ts_device=now_utc_iso(),
            buffer_backlog=self.store.pending_count(),
            link_state=self.link.state.value,
            uptime_s=int(now_epoch() - self._boot_epoch),
            site_id=self.site_id,
        )
        if not self.link.should_upload():
            # 断网时连心跳都不发 —— 省得每 5 秒撞一次超时。
            # 平台那边靠 HEARTBEAT_TIMEOUT_S 自己会把它判成离线。
            return
        try:
            reply = self.client.heartbeat(hb)
        except http_util.NetworkError as exc:
            self._fail += 1
            log.debug("心跳失败（第 %d 次）：%s", self._fail, exc)
            return
        self._fail = 0
        self._sent += 1
        self._last_reply = reply
        # 平台顺带回告两件事：桩号清单要不要重拉、上传间隔要不要改
        self._advice = {
            "pile_list_version": str(reply.get("pile_list_version", "")),
            "upload_interval_s": reply.get("upload_interval_s"),
            "echo_backlog": reply.get("echo_backlog"),
        }

    def stop(self) -> None:
        self._stop_evt.set()

    def snapshot(self) -> dict:
        return {
            "sent": self._sent,
            "fail": self._fail,
            "interval_s": self.interval,
            "uptime_s": int(now_epoch() - self._boot_epoch),
            "advice": dict(self._advice),
        }
