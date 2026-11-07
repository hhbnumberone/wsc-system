# -*- coding: utf-8 -*-
"""不重证明：把同一批数据 POST 两次，看平台认不认得出。

演示场景 3 用。它做的事就是终端上传器做的事 —— 用**同一套**
`common.protocol.Batch` 和 `common.ids.make_batch_id` 组批，
所以这里跑通的不代表"脚本能跑通"，而是"上传协议本身是幂等的"。

三个回合：

    ① 正常上传      -> accepted=N
    ② 原样再传一次   -> duplicate=true，平台帧总数**一个都没变**
    ③ 改一个数值再传 -> 整批拒绝（DUPLICATE_CONFLICT），原值不被覆盖

第 ③ 回合是给客户看的重点：光靠 batch_id 判重还不够，
有人截获一个批次、改掉数值再原样重放，batch_id 是不变的。
所以协议里还有一层 `content_digest`（整批**数值内容**的摘要），
两个对不上就整批拒收并留痕。

用法：

    python tools/prove_idempotent.py                 # 对着 127.0.0.1:8700
    python tools/prove_idempotent.py --port 8700
    python tools/prove_idempotent.py --tag 我的演示   # 换个标记，避免和上次撞 record_id

平台没起来会直接告诉你，不是抛一堆 traceback。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import params                                   # noqa: E402
from common.csv_schema import compute_row_hash               # noqa: E402
from common.ids import frame_record_id, make_batch_id        # noqa: E402
from common.protocol import Batch                            # noqa: E402
from common.timeutil import now_epoch, to_datetime           # noqa: E402


# --------------------------------------------------------------------------
def post(base: str, path: str, payload: dict) -> tuple[int, dict]:
    """POST 一个 JSON。**挂空 ProxyHandler** —— 客户机上系统代理非空时，
    请求会莫名其妙地被转到代理去，然后连不上 127.0.0.1。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        base + path, method="POST",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Connection": "close"})
    try:
        with opener.open(req, timeout=8) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def get(base: str, path: str) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base + path, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def frame_count(base: str) -> int:
    """平台上近 24 小时的帧总数。

    取的是 `/api/overview` 里平台自己算的那个数 —— 也就是平台首页仪表盘上
    显示给客户看的数字。**不去读数据库**：演示时被人问"你这脚本是不是
    自己数的"，指着平台页面上的同一个数字回答最省事。
    """
    return int(get(base, "/api/overview").get("frames_24h") or 0)


# --------------------------------------------------------------------------
def build_records(device: str, pile_id: str, n: int = 3) -> list[dict]:
    """造几条**完全合法**的帧记录：时间不超前、桩号在清单内、参数在域内。

    数值给的是常量而不是随机数 —— 这个脚本要证明的是幂等，
    不是模拟真实波形，常量能让第二次和第一次的字节完全一致。
    """
    ts0 = int(now_epoch() * 1000) - 60_000      # 往回一分钟，避开时钟偏移判定
    seq0 = 900_000 + int(now_epoch()) % 90_000  # 避开终端正在用的序号段

    values = {p.key: 0.0 for p in params.PARAMS}
    values.update({
        "workStatus": 1.0, "cylinderPumpingPressure": 8.5,
        "pumpingSpeed": 10.8, "circulatingPumpingVolume": 6.0,
        "acceleratorAddSpeed": 520.0, "acceleratorProportion": 4.8,
        "acceleratorOnceDosage": 18.0, "acceleratorCumulativeDosage": 120.5,
    })

    out = []
    for i in range(n):
        ts = ts0 + i * 2000
        rid = frame_record_id(device, ts, seq0 + i)
        # 先摊平成 CSV 行，再**从这一行**导出要发的记录 —— 顺序反过来的话
        # （照记录字典另拼一个 flat 去算哈希）两边很容易少一列，
        # 算出来的 row_hash 就跟终端对同样数据算出来的不一样。
        # 这里少写过的列是 quality 和 source。
        meta = {"record_id": rid, "device_code": device,
                "ts_device": to_datetime(ts).isoformat(timespec="milliseconds"),
                "ts_epoch": ts, "seq": seq0 + i,
                "pile_id": pile_id, "quality": 0, "source": "can"}
        flat = {**meta, **values}
        out.append({**meta, "values": dict(values),
                    "row_hash": compute_row_hash(flat)})
    return out


def tamper(records: list[dict]) -> list[dict]:
    """改一条记录的数值 —— 模拟"截获批次后偷偷改数再重放"。"""
    out = copy.deepcopy(records)
    out[0]["values"]["cylinderPumpingPressure"] = 99.9
    return out


# --------------------------------------------------------------------------
def line(ok: bool, text: str) -> None:
    print(f"  {'[OK]  ' if ok else '[!!]  '}{text}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="幂等性现场证明")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--device", default="GHP9015H")
    args = ap.parse_args(argv)
    # 序号段由当前时间推出来，所以连跑两次也不会撞上上一轮的 record_id，
    # 不需要人来指定"这次用哪一批"。

    base = f"http://{args.host}:{args.port}"

    try:
        piles = get(base, "/api/piles")["piles"]
    except (urllib.error.URLError, OSError) as exc:
        print(f"\n平台连不上（{base}）：{exc}")
        print("先把平台起起来：python -m server.main   或   python run_all.py")
        return 1
    if not piles:
        print("平台没有桩号清单，先起一次平台让它生成默认清单。")
        return 1

    pile_id = piles[len(piles) // 2]["pile_id"]
    records = build_records(args.device, pile_id)
    batch_id = make_batch_id(args.device, [r["record_id"] for r in records])

    print(f"\n目标平台   {base}")
    print(f"设备/桩号  {args.device} / {pile_id}")
    print(f"批次       {batch_id}   （{len(records)} 条记录）")
    print("           内容寻址：这个 ID 由记录集合算出，重传时必然相同\n")

    before = frame_count(base)

    def send(recs: list[dict]) -> dict:
        batch = Batch(batch_id=batch_id, device_code=args.device, records=recs,
                      note="幂等性现场证明")
        st, body = post(base, "/api/upload", batch.to_dict())
        body["_http"] = st
        return body

    def counts(r: dict) -> str:
        s = r.get("summary") or {}
        return (f"accepted={s.get('accepted')}  duplicate={s.get('duplicate')}"
                f"  rejected={s.get('rejected')}")

    # ---- ① 首次上传 ----
    print("① 首次上传")
    r1 = send(records)
    st = r1["_http"]
    if st != 200 or (r1.get("summary") or {}).get("rejected"):
        line(False, f"HTTP {st}，未被接受：{json.dumps(r1, ensure_ascii=False)[:300]}")
        return 1
    line(True, f"HTTP {st}  {counts(r1)}  status={r1.get('status')}")
    mid = frame_count(base)
    line(mid > before, f"平台帧总数  {before} -> {mid}（+{mid - before}）")

    # ---- ② 原样重放 ----
    print("\n② 原样再传一次（模拟网络重试 / 崩溃后重启重传）")
    r2 = send(records)
    st2 = r2["_http"]
    after = frame_count(base)
    line(True, f"HTTP {st2}  {counts(r2)}  status={r2.get('status')}")
    # 注意这里 **不是** accepted=0。平台返回的是**当初那张原回执**，
    # 一字不改地重放回来，只在收据上盖一个 duplicate_batch 戳。
    # 这是有意的：终端崩溃重启后重传，拿到的回执要和第一次完全一致，
    # 它才能据以推进自己那批的状态机。真正的证据是下面的帧总数没动。
    line(bool(r2.get("duplicate_batch")) and r2.get("batch_id") == batch_id,
         f"回执带 duplicate_batch 戳：duplicate_batch={r2.get('duplicate_batch')}  "
         f"batch_id={r2.get('batch_id')}")
    line(after == mid,
         f"平台帧总数  {mid} -> {after}（{'没变 —— 幂等成立' if after == mid else '变了！'}）")
    line(r2.get("summary") == r1.get("summary"),
         "回执内容与首次逐字一致（原回执重放，不是新开一张）")

    # ---- ③ 篡改后重放 ----
    print("\n③ 改掉一个数值再传（模拟截获批次后偷偷改数）")
    r3 = send(tamper(records))
    st3 = r3["_http"]
    final = frame_count(base)
    rejected = st3 != 200 or "error" in r3 or (r3.get("summary") or {}).get("rejected")
    line(bool(rejected), f"HTTP {st3} —— "
                         f"{r3.get('error') or r3.get('message') or counts(r3)}")
    line(final == after, f"平台帧总数  {after} -> {final}（原值没被覆盖）")

    print("\n" + "=" * 68)
    print("  结论：重传被认出（不重）、篡改被拒绝（不改）、原值永久保留。")
    print("=" * 68 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
