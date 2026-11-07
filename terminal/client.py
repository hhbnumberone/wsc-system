# -*- coding: utf-8 -*-
"""终端对平台的**唯一** HTTP 出口。

为什么单独成模块：上传状态机最怕"某处顺手发了个请求"。所有跟平台说话的地方
都必须经过这里，超时、重试语义、错误分类才能只写一遍、只调一次。

错误分类（这是本模块的全部价值）：

| 情况 | 异常 | 上传层该怎么办 |
|---|---|---|
| 连不上、超时、连接被重置 | `NetworkError(retryable=True)` | 回 PENDING，退避重试 |
| 平台 5xx / 429 | `NetworkError(retryable=True)` | 同上 |
| 平台 4xx 且带原因码 | `NetworkError(retryable=False)` | **别再重试**：要么拆批，要么交人工 |
| 200 但 body 不是合法回执 | `NetworkError(retryable=True)` | 当作网络错误 —— 没拿到回执就等于没送到 |

最后一条最容易写错。HTTP 200 不等于"平台收下了"；
只有解析出合法回执才改本地状态，否则一次半截响应就能让终端把数据标成已上传。
"""
from __future__ import annotations

import logging

from common import config, http_util
from common.ids import MAX_BATCH_RECORDS
from common.protocol import Batch, Heartbeat, Receipt

log = logging.getLogger("terminal.client")


class PlatformClient:
    """平台的 HTTP 客户端。一个实例可以被多个线程用（无状态）。"""

    def __init__(self, cfg: dict | None = None, base: str | None = None):
        self.cfg = cfg or config.load_config()
        self.base = (base or config.url_base("server", self.cfg)).rstrip("/")
        up = self.cfg["upload"]
        self.timeout = float(up["timeout_s"])
        self.connect_timeout = float(up.get("connect_timeout_s", 2.0))
        self._max_batch = min(int(up["batch_max_records"]), MAX_BATCH_RECORDS)

    # ------------------------------------------------------------------
    # 设备侧三个接口
    # ------------------------------------------------------------------
    def upload(self, batch: Batch) -> Receipt:
        """上传一批。成功返回回执，失败抛 `NetworkError`。

        **只有解析出合法回执才返回。** body 缺字段、类型不对，
        `Receipt.from_dict` 会抛 ValueError —— 这里把它转成可重试的网络错误，
        因为"回执读不懂"和"回执没收到"对终端是同一件事：状态不能改。
        """
        if not batch.records:
            raise ValueError("空批次不该发出去")
        if len(batch.records) > self._max_batch:
            raise ValueError(
                f"批次 {len(batch.records)} 条超过本端上限 {self._max_batch} 条")
        payload = batch.to_dict()
        obj = http_util.post_json(f"{self.base}/api/upload", payload, timeout=self.timeout)
        try:
            receipt = Receipt.from_dict(obj)
        except (ValueError, TypeError) as exc:
            raise http_util.NetworkError(
                f"平台响应解析失败，当作没收到回执：{exc}", retryable=True,
                status=200) from None
        if receipt.batch_id != batch.batch_id:
            # 平台回的是别的批次的回执 —— 宁可不认，也不能把状态改错
            raise http_util.NetworkError(
                f"回执的 batch_id 对不上：期望 {batch.batch_id}，收到 {receipt.batch_id}",
                retryable=True, status=200)
        return receipt

    def heartbeat(self, hb: Heartbeat) -> dict:
        """上报心跳。返回平台的回告（桩号清单版本、建议上传间隔）。"""
        return http_util.post_json(f"{self.base}/api/heartbeat", hb.to_dict(),
                                   timeout=self.connect_timeout + 1.0)

    def piles(self, site_id: str = "") -> tuple[list[dict], str]:
        """拉平台下发的桩号清单。返回 (清单, 版本时间)。

        终端**只能**从这个清单里选桩号（见 terminal/pile.py）。
        """
        params = {"site_id": site_id} if site_id else None
        obj = http_util.get_json(f"{self.base}/api/piles", timeout=self.timeout,
                                 params=params)
        piles = obj.get("piles")
        if not isinstance(piles, list):
            raise http_util.NetworkError("桩号清单响应格式不对：缺少 piles 数组",
                                        retryable=True, status=200)
        return [p for p in piles if isinstance(p, dict) and p.get("pile_id")], \
            str(obj.get("issued_at", ""))

    def check_identity(self, site_id: str, device_code: str) -> dict:
        """现场录入工点/设备编号时，请平台核对。**判定权在平台。**

        返回平台的原话（工点档案、桩号范围、设备登记情况、警告）。

        这里刻意把**平台的拒绝**和**网络故障**分开：

        * 平台明确回复"没有这个工点"（409）—— 这不是异常，是一条要显示给
          现场看的结果，连它附带的 `known_sites`（平台已建档的工点）一起
          原样返回，界面才能告诉人"该填什么"。
          4xx 一律按这个处理：平台已经表态了，重试没有意义。
        * 连不上、超时 —— 照旧抛 `NetworkError`。那件事的处置完全不同：
          要重试或者去查链路，而不是让人改输入框里的字。

        与 `pile.check_import` 同一个思路：能说清"哪儿不对"的，就不要抛异常。
        """
        try:
            return http_util.get_json(
                f"{self.base}/api/identity/check", timeout=self.timeout,
                params={"site_id": site_id, "device_code": device_code})
        except http_util.NetworkError as exc:
            if exc.body and 400 <= exc.status < 500:
                return dict(exc.body, ok=False)
            raise

    def health(self) -> bool:
        """连通性探测。不抛异常 —— 链路状态机靠它每几秒问一次。"""
        return http_util.probe(f"{self.base}/api/health", timeout=self.connect_timeout)

    def server_time(self) -> str:
        """取平台时间。空串表示探测失败（不要抛，调用方多半在兜底路径上）。"""
        try:
            obj = http_util.get_json(f"{self.base}/api/health",
                                     timeout=self.connect_timeout)
        except http_util.NetworkError:
            return ""
        return str(obj.get("server_time", ""))
