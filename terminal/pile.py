# -*- coding: utf-8 -*-
"""桩号：终端只能从平台下发的清单里**选**，永远不能写。

这是"桩号防伪"三层防线里的第二层（第一层是界面不给输入框，第三层是
服务端入库再校验）。三层各自独立，任何一层单独拎出来都拦得住 ——
因为这个模块一旦被绕过，指标就失去意义了：一个可以随便写的桩号，
等于没有桩号。

具体做法：

* 清单存在本地库的 `piles` 表里（平台下发时整表替换），断网时继续用缓存。
  现场隧道里没信号是常态，不能因为拉不到清单就停产。
* `select()` 只接受清单里的桩号，否则抛 ValueError。故意**抛异常**而不是
  返回 False：这样每个调用点都必须显式处理，不存在"忘了判断"的写法。
* 缓存为空且平台也连不上时，`ready` 为 False —— 这时采集侧不出数
  （见 terminal/app.py）。宁可不出，不可乱出。
* 平台回一个空清单时**拒绝替换**本地缓存。否则平台端一次误操作
  （比如演示前 --reset）就能让终端此后采的每一条都因桩号未下发而被拒收。
"""
from __future__ import annotations

import logging
import re

from common import config, http_util
from common.enums import ReasonCode

log = logging.getLogger("terminal.pile")

#: 桩号里程的解析式。允许 ZK105+207 / K105+207 / 105+207 三种写法，
#: 前缀（ZK/K）不参与排序 —— 同一条隧道里不会混用两种前缀。
_PILE_RE = re.compile(r"^\s*(?P<prefix>[A-Za-z]{0,4})\s*(?P<k>\d{1,3})\s*\+\s*(?P<m>\d{1,3})\s*$")

#: 本地缓存对应的平台清单版本。值等于服务端 `MAX(piles.issued_at)`，
#: 也就是心跳回告里的 `pile_list_version` —— 两边可比才谈得上"该不该刷新"。
META_VERSION = "pile_list_version"

#: 由 store.replace_piles() 维护，这里只读。
META_SYNCED_AT = "piles_synced_at"


def parse_mileage(pile_id: str) -> int | None:
    """桩号 → 里程（米）。解析不了返回 None（排序时排到最后）。"""
    m = _PILE_RE.match(pile_id or "")
    if not m:
        return None
    k = int(m.group("k"))
    mm = int(m.group("m"))
    if mm >= 1000:
        return None
    return k * 1000 + mm


def format_mileage(mileage_m: int, prefix: str = "ZK") -> str:
    """里程（米）→ 桩号。"""
    return f"{prefix}{mileage_m // 1000}+{mileage_m % 1000:03d}"


def sort_key(pile_id: str) -> tuple:
    """排序键：(有没有解析出里程, 里程, 原串)。

    第三项是保险 —— 解析不出来的桩号按原串排，保证排序是**全序**，
    不会因为两个 None 比较而崩，也不会在刷新前后抖动。
    """
    mi = parse_mileage(pile_id)
    return (0 if mi is not None else 1, mi if mi is not None else 0, pile_id)


class PileRegistry:
    """桩号清单 + 当前桩号。**进程内只有一个实例**（terminal/app.py 持有）。"""

    def __init__(self, store, client, cfg: dict | None = None,
                 site_id: str | None = None):
        self.cfg = cfg or config.load_config()
        self.store = store
        self.client = client
        self.site_id = site_id if site_id is not None else \
            config.load_site().get("site_id", "")
        self._rows: list[dict] = []
        self._current = ""
        self._last_error = ""
        self._advice_version = ""      # 心跳里平台报的清单版本
        self._load_cache()

    # ==================================================================
    # 启动时从本地缓存恢复
    # ==================================================================
    def _load_cache(self) -> None:
        # store.piles() 已经按里程升序；这里再排一次是为了不依赖它的实现
        # （桩号的顺序是界面上给客户看的东西，不能靠下游 SQL 的 ORDER BY 保证）。
        self._rows = sorted(self.store.piles(), key=lambda r: sort_key(str(r.get("pile_id", ""))))
        if self._rows:
            log.info("桩号缓存：%d 个（%s ~ %s）", len(self._rows),
                     self._rows[0]["pile_id"], self._rows[-1]["pile_id"])

    # ==================================================================
    # 读
    # ==================================================================
    @property
    def ready(self) -> bool:
        """有清单可用。False 时采集侧不出数。"""
        return bool(self._rows)

    @property
    def current(self) -> str:
        """当前作业桩号。空串表示还没选（或还没拿到清单）。"""
        return self._current

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def version(self) -> str:
        """本地缓存对应的平台清单版本。"""
        return self.store.meta(META_VERSION)

    def all(self) -> list[dict]:
        """全部桩号，按里程升序。**界面用它渲染可点清单。**"""
        return [dict(r) for r in self._rows]

    def ids(self) -> list[str]:
        return [str(r.get("pile_id", "")) for r in self._rows]

    def known(self, pile_id: str) -> bool:
        """这个桩号是不是平台下发的。"""
        return bool(pile_id) and self.store.pile_known(pile_id)

    def snapshot(self) -> dict:
        return {
            "ready": self.ready,
            "current": self._current,
            "count": len(self._rows),
            "version": self.version,
            "synced_at": self.store.meta(META_SYNCED_AT),
            "last_error": self._last_error,
            "first": self._rows[0]["pile_id"] if self._rows else "",
            "last": self._rows[-1]["pile_id"] if self._rows else "",
        }

    # ==================================================================
    # 选择
    # ==================================================================
    def select(self, pile_id: str) -> str:
        """切换当前桩号。**不在清单里就抛 ValueError。**

        抛异常是刻意的：桩号是这套系统里唯一"人可以选择但不能编造"的东西，
        唯一该走的路是界面上的清单。任何绕过清单的调用都是 bug，
        应该在开发阶段就炸出来，而不是悄悄写进库里。
        """
        pid = (pile_id or "").strip()
        if not pid:
            raise ValueError("桩号不能为空")
        if not self.known(pid):
            raise ValueError(
                f"桩号 {pid} 不在平台下发的清单内，不能选用"
                f"（清单共 {len(self._rows)} 个，"
                f"{self._rows[0]['pile_id'] if self._rows else '空'} ~ "
                f"{self._rows[-1]['pile_id'] if self._rows else '空'}）")
        if pid != self._current:
            log.info("切换桩号：%s -> %s", self._current or "(未选)", pid)
        self._current = pid
        return pid

    def advance(self, step: int = 1) -> str:
        """推进到清单里的下一个桩号（喷射循环结束时调用）。

        走到清单尽头就停在最后一个，**不回卷**：掌子面只会往前走，
        回卷到洞口会在曲线里凭空造出一段往回跳的桩号，一眼就是假的。
        """
        if not self._rows:
            raise ValueError("还没有桩号清单，无法推进")
        ids_ = self.ids()
        if self._current in ids_:
            i = ids_.index(self._current)
        else:
            i = 0
        nxt = ids_[max(0, min(i + int(step), len(ids_) - 1))]
        if nxt == self._current:
            log.info("已到清单末端 %s，桩号不再推进", nxt)
        self._current = nxt
        return nxt

    # ==================================================================
    # 同步
    # ==================================================================
    def needs_refresh(self, platform_version: str) -> bool:
        """平台心跳里带回来的版本号跟本地不一致 —— 清单更新了。"""
        pv = (platform_version or "").strip()
        if not pv:
            return False          # 平台没说，不猜
        return pv != self.version

    def refresh(self, *, force: bool = False) -> dict:
        """拉平台清单并整表替换本地缓存。**不抛异常** —— 调用方是后台线程。

        失败时保留旧缓存：现场拉不到清单是常态，但清单本身不会因此失效。
        """
        cur = self.version
        if not force and self._rows and not self.needs_refresh(self._advice_version):
            return {"ok": True, "changed": False, "count": len(self._rows),
                    "version": cur, "reason": "本地清单已是最新"}
        try:
            rows, _issued_at = self.client.piles(self.site_id)
        except http_util.NetworkError as exc:
            self._last_error = str(exc)
            log.warning("拉取桩号清单失败，继续使用本地缓存（%d 个）：%s",
                        len(self._rows), exc)
            return {"ok": False, "changed": False, "count": len(self._rows),
                    "version": cur, "reason": str(exc)[:200]}

        if not rows:
            # 空清单不替换。见模块开头：一次平台误操作不该让终端此后全被拒收。
            msg = "平台回了一个空清单，保留本地缓存不替换"
            self._last_error = msg
            log.error("%s（当前 %d 个）", msg, len(self._rows))
            return {"ok": False, "changed": False, "count": len(self._rows),
                    "version": cur, "reason": msg}

        # 版本取各行 issued_at 的最大值 —— 跟服务端心跳回告的
        # `pile_list_version`（`SELECT MAX(issued_at) FROM piles`）是同一个值，
        # 这样 needs_refresh() 的字符串比较才有意义。
        version = max((str(r.get("issued_at", "")) for r in rows), default="")

        old = set(self.ids())
        new = {str(r.get("pile_id", "")) for r in rows if r.get("pile_id")}
        added, removed = sorted(new - old, key=sort_key), sorted(old - new, key=sort_key)

        # 两步不是同一个事务：万一中间崩了，库里会出现"新清单 + 旧版本号"，
        # 于是 needs_refresh() 为真，下一轮自动重拉，自己就好了。
        self.store.replace_piles(rows)
        self.store.set_meta(META_VERSION, version)
        self._load_cache()
        self._last_error = ""
        self._advice_version = version

        # 当前桩号被平台撤了 —— 必须换一个，否则后续记录会带着一个
        # 已经失效的桩号上传，然后被平台逐条拒收。
        if self._current and self._current not in new:
            log.warning("当前桩号 %s 已不在新清单里，自动切到 %s",
                        self._current, self.ids()[0])
            self._current = self.ids()[0]

        log.info("桩号清单已更新：%d 个（新增 %s、移除 %s）", len(rows),
                 "、".join(added) or "无", "、".join(removed) or "无")
        return {"ok": True, "changed": True, "count": len(rows), "version": version,
                "added": added, "removed": removed, "reason": ""}

    def note_advice(self, version: str) -> None:
        """记下心跳里平台报的清单版本（terminal/app.py 从 HeartbeatThread 取）。"""
        self._advice_version = str(version or "")

    # ==================================================================
    # 导入校验（防线第二层的落点）
    # ==================================================================
    def check_import(self, rows) -> dict:
        """校验一批外来记录（U 盘导入 / 手工 CSV）的桩号。

        返回冲突报告而不是抛异常 —— 导入是批量操作，用户需要看到
        "哪几行不对、为什么"，一次改完再导，而不是第一行就中断。
        """
        ids_ = set(self.ids())
        conflicts: list[dict] = []
        total = 0
        for i, rec in enumerate(rows):
            total += 1
            pid = str(rec.get("pile_id", "")).strip()
            if not pid:
                conflicts.append({"row": i + 1, "pile_id": "",
                                  "reason": "桩号为空",
                                  "code": ReasonCode.PILE_NOT_ISSUED.value})
            elif pid not in ids_:
                conflicts.append({"row": i + 1, "pile_id": pid,
                                  "reason": f"桩号 {pid} 不在平台下发的清单内",
                                  "code": ReasonCode.PILE_NOT_ISSUED.value})
        return {
            "total": total,
            "ok": total - len(conflicts),
            "conflicts": conflicts,
            "accept": not conflicts,
            "list_count": len(ids_),
            "list_version": self.version,
        }
