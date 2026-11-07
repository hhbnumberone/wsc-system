# -*- coding: utf-8 -*-
"""U 盘同步：导出 → 人工校对 → 导入 → 生成修订批。

为什么要有这条路：隧道里没信号是常态，但监理当天就要看数据，
U 盘是唯一能把数据带出去的通道。它同时是**整条链路上唯一能改动数据的入口** ——
所以这里每一处设计都围绕一句话：改可以改，但改不出平台看不见的东西。

三条规矩：

1. **导出带清单**。`manifest.json` 里记着每个文件的 sha256、记录条数与 ID 清单。
   回到终端第一件事是校验，文件被谁动过一个字节都当场报出来，
   不靠"看起来差不多"。
2. **导入不覆盖**。逐行比对：内容一致就跳过；不一致就生成一条**修订**
   （改了什么、为什么改、谁改的），排进 `revision_queue` 等上传。
   `frames` 里的原始值一个字节都不动 —— 这是"原值永久保留"在终端侧的落点。
3. **桩号只在平台清单里**。U 盘能改测量值，改不了里程：带回来的桩号
   只要和本机记录不一致，整包拒收，并提示"要改桩号请在界面上从清单里重选"。
   这不是权限设计，是数据可信的前提 —— 里程能自由写，所有方量统计就都没意义了。

本模块只做编排与校验，不碰 HTTP：修订批什么时候发、发给谁，
由 `uploader.py` 按"欠账优先"的次序决定。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from typing import Any, Iterable, Sequence

from common import config, csv_schema, ids
from common.enums import ReasonCode
from common.params import PARAM_BY_KEY, PARAM_KEYS
from common.timeutil import CST, fmt_local, now_epoch, now_utc_iso, to_datetime

log = logging.getLogger("terminal.usb")

#: U 盘包格式版本。将来改字段时靠它区分新旧包
BUNDLE_VERSION = 1

MANIFEST_NAME = "manifest.json"
DATA_NAME = "frames.csv"
README_NAME = "说明.txt"

#: 导入时允许的修订原因（其余原因码属于平台侧判定，不该从 U 盘发起）
IMPORT_REASONS: tuple[str, ...] = (
    ReasonCode.VALUE_DISTORTED.value,
    ReasonCode.SENSOR_FAULT.value,
    ReasonCode.PILE_CORRECTION.value,
    ReasonCode.OTHER.value,
)

#: 不允许通过 U 盘改的字段。桩号在列 —— 见模块开头第 3 条
FROZEN_FIELDS = ("record_id", "device_code", "ts_device", "ts_epoch", "seq", "pile_id")


def usb_root() -> str:
    """演示用的"U 盘"根目录。真机上就是一个盘符，这里是个本地目录。"""
    return config.USB_DIR


def default_out_dir(device_code: str, when: float | None = None) -> str:
    """导出目录名：`<设备>_<本地时间>`。按时间命名，插过好几个盘也分得清。"""
    stamp = to_datetime(when if when is not None else now_epoch()).astimezone(CST)
    return os.path.join(usb_root(), "out",
                        f"{device_code}_{stamp.strftime('%Y%m%d_%H%M%S')}")


# ==========================================================================
# 导出
# ==========================================================================
def export_bundle(rows: Sequence[dict], device_code: str, *,
                  dest_dir: str | None = None, scope: str = "pending",
                  note: str = "", replace: bool = False) -> dict:
    """把记录写成 U 盘包：`frames.csv` + `manifest.json` + `说明.txt`。

    返回一个可以直接丢给界面的结果字典。**不抛异常** —— 现场最不缺的就是
    "U 盘没插"这类失败，界面要的是能照着念的一句话，不是堆栈。
    """
    if not rows:
        return {"ok": False, "exported": 0, "dir": "",
                "message": "没有可导出的记录（待上传队列已清空）"}

    dest = dest_dir or default_out_dir(device_code)
    dest = os.path.abspath(dest)
    try:
        if replace and os.path.isdir(dest):
            shutil.rmtree(dest)
        os.makedirs(dest, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "exported": 0, "dir": dest,
                "message": f"无法写入导出目录：{exc}"}

    csv_path = os.path.join(dest, DATA_NAME)
    try:
        fp, writer = csv_schema.open_new(csv_path)
        try:
            for rec in rows:
                writer.writerow(csv_schema.row_from_frame(rec))
        finally:
            fp.close()
    except OSError as exc:
        return {"ok": False, "exported": 0, "dir": dest,
                "message": f"写 CSV 失败（U 盘拔了？）：{exc}"}

    with open(csv_path, "rb") as fp:
        blob = fp.read()
    ts_list = [int(r.get("ts_epoch", 0)) for r in rows]
    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "device_code": device_code,
        "exported_at": now_utc_iso(),
        "exported_at_local": fmt_local(now_epoch()),
        "scope": scope,
        "note": note,
        "record_count": len(rows),
        "first_ts": min(ts_list) if ts_list else 0,
        "last_ts": max(ts_list) if ts_list else 0,
        "first_local": fmt_local(min(ts_list) / 1000) if ts_list else "",
        "last_local": fmt_local(max(ts_list) / 1000) if ts_list else "",
        "pile_ids": sorted({str(r.get("pile_id", "")) for r in rows}),
        "record_ids": [str(r["record_id"]) for r in rows],
        "files": [{
            "name": DATA_NAME,
            "bytes": len(blob),
            "rows": len(rows),
            "sha256": ids.sha256_hex(blob),
        }],
    }
    with open(os.path.join(dest, MANIFEST_NAME), "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, ensure_ascii=False, indent=2)
    with open(os.path.join(dest, README_NAME), "w", encoding=csv_schema.ENCODING) as fp:
        fp.write(_readme_text(manifest))

    log.info("U 盘导出：%d 条 → %s", len(rows), dest)
    return {"ok": True, "exported": len(rows), "dir": dest,
            "csv": csv_path, "manifest": manifest,
            "message": f"已导出 {len(rows)} 条到 {dest}"}


def _readme_text(m: dict) -> str:
    """给拿 U 盘的人看的一页纸。用带 BOM 的 UTF-8，记事本打开不乱码。"""
    return (
        f"{m['device_code']} 湿喷台车数据导出\r\n"
        f"导出时间：{m['exported_at_local']}\r\n"
        f"数据范围：{m['first_local']} ～ {m['last_local']}（共 {m['record_count']} 条）\r\n"
        f"涉及桩号：{'、'.join(m['pile_ids'])}\r\n"
        "\r\n"
        "【怎么改】\r\n"
        "  用 Excel 打开 frames.csv，只能改参数值（第 9 列之后那些），\r\n"
        "  以及行尾的 row_hash 请**留着别删** —— 终端拿它判断这一行有没有被改过。\r\n"
        "  不要改 record_id / device_code / ts_device / ts_epoch / seq / pile_id，\r\n"
        "  改了会被整包退回（桩号只能从平台下发的清单里选）。\r\n"
        "\r\n"
        "【改完怎么办】\r\n"
        "  把整个文件夹拷回终端，在终端界面点「U 盘导入」。\r\n"
        "  终端会先校验、再逐行比对，只把**改动过的字段**生成修订记录传给平台，\r\n"
        "  原始值在平台和终端都永久保留，谁改的、什么时候、为什么，全程可查。\r\n"
        "\r\n"
        "【别动 manifest.json】\r\n"
        "  它记着每一行文件内容的校验和，改过就对不上了。\r\n"
    )


# ==========================================================================
# 校验与比对（只读，不落库）
# ==========================================================================
def _load_manifest(bundle: str) -> tuple[dict, list[str], list[str]]:
    """读清单并校验文件完整性。返回 (清单, 致命问题, 提示)。

    **校验和对不上不是致命错误**，这一点很容易搞反：这条路本来就是
    "导出 → 人拿 Excel 改 → 导回来"，改了值校验和必然对不上。
    它的用途是**报警**（"这份包被动过"），不是**拦截**。
    真正拦得住的是逐行的校验：桩号、数值、record_id 是否本机所有 ——
    这些才决定"这一行能不能改"。
    只有清单里列的文件**根本不在**时才算致命：数据不全，没法比对。
    """
    path = os.path.join(bundle, MANIFEST_NAME)
    if not os.path.isfile(path):
        return {}, [f"缺少 {MANIFEST_NAME} —— 这不是一个完整的导出包"], []
    try:
        with open(path, "r", encoding="utf-8") as fp:
            m = json.load(fp)
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"{MANIFEST_NAME} 读不出来：{exc}"], []

    errors: list[str] = []
    warnings: list[str] = []
    for f in m.get("files") or []:
        fp_path = os.path.join(bundle, str(f.get("name", "")))
        if not os.path.isfile(fp_path):
            errors.append(f"清单里的 {f.get('name')} 不存在 —— 数据不全，无法比对")
            continue
        with open(fp_path, "rb") as fh:
            got = ids.sha256_hex(fh.read())
        if got != f.get("sha256"):
            warnings.append(
                f"{f.get('name')} 与导出时的校验和不一致（清单 {str(f.get('sha256'))[:12]}，"
                f"实际 {got[:12]}）—— 这份包被改过，下面的结果按改动后的内容算")
    return m, errors, warnings


def _diff_row(local: dict, row: dict) -> list[dict]:
    """比一行的参数值。返回改动列表（只含数值真的变了的字段）。"""
    out = []
    lv = local.get("values") or {}
    for k in PARAM_KEYS:
        raw = row.get(k, "")
        try:
            new = float(raw)
        except (TypeError, ValueError):
            new = None
        old = lv.get(k)
        if new is None or old is None:
            continue
        if abs(float(old) - new) < 1e-9:
            continue
        p = PARAM_BY_KEY[k]
        out.append({"key": k, "name": p.name, "unit": p.unit,
                    "old": float(old), "new": new,
                    "old_text": p.format(float(old)), "new_text": p.format(new)})
    return out


def inspect_bundle(store, bundle: str, *, device_code: str,
                   pile_ids: Iterable[str] | None = None) -> dict:
    """校验 + 逐行比对，**一个字节都不写**。界面用它先出"冲突报告"。

    一行的判定结果只有四种，界面上的处理也各不相同：

    ==========  ================================================
    same        内容一致，跳过（U 盘绕了一圈，数据没变）
    changed     有参数被改过 → 生成修订，原值不动
    unknown     本机库里没有这个 record_id → 拒收，
                多半是别的车的 U 盘或者本机库被清过
    invalid     行本身不合法（缺列、非数值、桩号越界）→ 整包退回
    ==========  ================================================
    """
    bundle = os.path.abspath(bundle)
    if not os.path.isdir(bundle):
        return _report(bundle, ok=False, errors=[f"目录不存在：{bundle}"], counts={},
                       changes=[], manifest={})
    manifest, problems, warnings = _load_manifest(bundle)

    csv_path = os.path.join(bundle, DATA_NAME)
    if not os.path.isfile(csv_path):
        problems.append(f"缺少 {DATA_NAME}")
    if problems:
        return _report(bundle, ok=False, errors=problems, counts={}, changes=[],
                       manifest=manifest, warnings=warnings)

    allowed = set(pile_ids) if pile_ids is not None else None
    counts = {"total": 0, "same": 0, "changed": 0, "unknown": 0, "invalid": 0}
    changes: list[dict] = []
    errors: list[str] = []
    rows: list[dict] = []
    try:
        for raw in csv_schema.iter_rows(csv_path):
            counts["total"] += 1
            try:
                rec = csv_schema.frame_from_row(raw)
            except ValueError as exc:
                counts["invalid"] += 1
                errors.append(f"第 {counts['total'] + 1} 行读不出来：{exc}")
                continue
            if rec["device_code"] and rec["device_code"] != device_code:
                counts["invalid"] += 1
                errors.append(f"{rec['record_id']} 的设备编号是 {rec['device_code']}，"
                              f"不是本机 {device_code}")
                continue
            if allowed is not None and rec["pile_id"] not in allowed:
                counts["invalid"] += 1
                errors.append(
                    f"{rec['record_id']} 的桩号 {rec['pile_id']} 不在平台下发的清单里")
                continue
            rows.append(raw)
    except ValueError as exc:
        return _report(bundle, ok=False, errors=list(problems) + [str(exc)],
                       counts=counts, changes=[], manifest=manifest, warnings=warnings)

    local = store.rows_by_ids([str(r.get("record_id") or "") for r in rows])
    for raw in rows:
        rid = str(raw.get("record_id") or "")
        mine = local.get(rid)
        if mine is None:
            counts["unknown"] += 1
            errors.append(f"{rid} 在本机库里不存在 —— 这份包不是这台车导出的？")
            continue
        if str(raw.get("pile_id") or "") != str(mine.get("pile_id") or ""):
            counts["invalid"] += 1
            errors.append(
                f"{rid} 的桩号被改成了 {raw.get('pile_id')}"
                f"（本机记录是 {mine.get('pile_id')}）—— 桩号不能从 U 盘改，"
                f"如需更正请在终端界面上从平台清单里重选")
            continue
        diffs = _diff_row(mine, raw)
        if not diffs:
            counts["same"] += 1
            continue
        counts["changed"] += 1
        changes.append({
            "record_id": rid,
            "pile_id": mine.get("pile_id", ""),
            "ts_local": fmt_local(mine.get("ts_epoch", 0) / 1000),
            "intact": csv_schema.row_is_intact(raw),
            "diffs": diffs,
        })

    # `ok` 只说一件事：**这份包能不能往下走**（没有错行）。
    # 它**不**表示"里面有改动" —— 那是 `changed`。这两件事混在一起写
    # （`ok = not errors and changed > 0`）会让"原样导出再导回来"这种
    # 完全正常的包报 ok=False，看的人以为包坏了，实际只是没人改过它。
    ok = not errors
    return _report(bundle, ok=ok, errors=errors, counts=counts, changes=changes,
                   manifest=manifest, warnings=warnings)


def _report(bundle: str, *, ok: bool, errors: list[str], counts: dict,
            changes: list[dict], manifest: dict,
            warnings: list[str] | None = None) -> dict:
    return {"bundle": bundle, "ok": ok, "errors": errors, "warnings": warnings or [],
            "counts": counts, "changes": changes, "manifest": manifest,
            "changed": counts.get("changed", 0),
            "same": counts.get("same", 0)}


# ==========================================================================
# 导入：生成修订批
# ==========================================================================
def import_bundle(store, bundle: str, *, device_code: str,
                  pile_ids: Iterable[str] | None = None,
                  operator: str = "", reason_code: str = ReasonCode.VALUE_DISTORTED.value,
                  note: str = "", limit: int = 500) -> dict:
    """把 U 盘上改动过的行转成修订记录排队等上传。

    有一行不合法就**整包不动**（计划书 5.4：宁可少改，不要错改）。
    界面可以先调 `inspect_bundle` 出报告给人看，确认后再调这里。
    """
    if reason_code not in IMPORT_REASONS:
        raise ValueError(f"修订原因不合法：{reason_code}")
    if not operator.strip():
        raise ValueError("必须填写操作人 —— 修订是要追到人的")

    rep = inspect_bundle(store, bundle, device_code=device_code, pile_ids=pile_ids)
    if rep["errors"]:
        return {**rep, "ok": False, "created": 0, "revision_ids": [],
                "message": f"有 {len(rep['errors'])} 处问题，整包未导入"}
    if not rep["changes"]:
        return {**rep, "ok": True, "created": 0, "revision_ids": [],
                "message": "没有检测到改动，不需要导入"}

    now_ms = int(now_epoch() * 1000)
    created: list[str] = []
    for i, ch in enumerate(rep["changes"][:limit], start=1):
        rid = ids.revision_record_id(device_code, now_ms, i)
        patch = {d["key"]: d["new"] for d in ch["diffs"]}
        store.enqueue_revision({
            "revision_id": rid,
            "target_record_id": ch["record_id"],
            "device_code": device_code,
            "patch": patch,
            "reason_code": reason_code,
            "reason_note": note or _auto_note(rep["bundle"], ch),
            "operator_name": operator.strip(),
            # 修订的设备时间用**原记录**的时间：它改的是那条记录，不是此刻
            "ts_device": _ts_of(store, ch["record_id"]),
        })
        created.append(rid)

    log.info("U 盘导入：%d 条记录、%d 条修订待上传（包 %s）",
             len(rep["changes"]), len(created), os.path.basename(rep["bundle"]))
    return {**rep, "ok": True, "created": len(created), "revision_ids": created,
            "message": f"已生成 {len(created)} 条修订，等待上传平台"}


def _auto_note(bundle: str, ch: dict) -> str:
    fields = "、".join(d["name"] for d in ch["diffs"])
    return f"U 盘校对：{fields}（{os.path.basename(bundle)}）"


def _ts_of(store, record_id: str) -> str:
    row = store.conn.execute(
        "SELECT ts_device FROM outbox WHERE record_id=?", (record_id,)).fetchone()
    return str(row["ts_device"]) if row else now_utc_iso()


# ==========================================================================
# 演示助手：模拟"在别的电脑上改了两行"
# ==========================================================================
def simulate_edit(bundle: str, *, key: str = "cylinderPumpingPressure", delta: float = 2.0,
                  count: int = 2, factor: float = 1.0) -> dict:
    """就地改掉包里最后 `count` 行的一个参数值。

    演示用。真机上改动是人拿 Excel 干的；演示要的是 10 秒内能重复一遍，
    而不是当场演示拖根号去算新压力值。它同时会**故意不改 row_hash** ——
    这正是"数据被动过"的样子：终端能看出内容变了，也能看出摘要对不上。

    真机上没人会给你这个函数，所以导入侧的判断一个字都不能松。
    """
    path = os.path.join(bundle, DATA_NAME)
    if not os.path.isfile(path):
        raise ValueError(f"找不到 {path}")
    if key in FROZEN_FIELDS:
        # 演示助手也不许越线：桩号/时间戳这类字段在这条路上根本改不动，
        # 让人以为能改，演示时就等着穿帮
        raise ValueError(f"{key} 不允许通过 U 盘修改")
    rows = list(csv_schema.iter_rows(path))
    if not rows:
        raise ValueError("包里的 CSV 是空的")
    if key not in PARAM_BY_KEY:
        raise ValueError(f"未知参数 {key}")

    targets = rows[-max(1, int(count)):]
    p = PARAM_BY_KEY[key]
    for r in targets:
        old = float(r[key])
        new = old + float(delta) if factor == 1.0 else old * float(factor)
        lo, hi = p.lo, p.hi
        # 夹在正常量程内：演示要的是"改了值"，不是"改成非法值"
        r[key] = round(max(lo, min(hi, new)), p.decimals)

    with open(path, "w", encoding=csv_schema.ENCODING, newline=csv_schema.NEWLINE) as fp:
        w = csv_schema.make_writer(fp)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in csv_schema.HEADER})

    log.warning("演示：已就地修改 %s 的最后 %d 行（%s）", DATA_NAME, len(targets), p.name)
    return {"ok": True, "file": path, "rows": len(targets), "param": key,
            "param_name": p.name,
            "detail": [{"record_id": r["record_id"], "new": r[key]} for r in targets]}


# ==========================================================================
# 目录浏览（界面用它列出可导入的包）
# ==========================================================================
def list_bundles(root: str | None = None, limit: int = 20) -> list[dict]:
    """列出 U 盘上的导出包，新的在前。

    只看 `<U 盘>/out/` 这一层（选定的导出目录）；根目录里直接找包是为了
    支持"直接把盘符路径填进来"的用法，此时不再递归 ——
    递归会把 out 本身也列成一个空包。
    """
    root = root or usb_root()
    out_dir = os.path.join(root, "out")
    bases = [out_dir] if os.path.isdir(out_dir) else [root]
    found: list[dict] = []
    for base in bases:
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base), reverse=True):
            path = os.path.join(base, name)
            if not os.path.isdir(path):
                continue
            m: dict[str, Any] = {}
            mpath = os.path.join(path, MANIFEST_NAME)
            if os.path.isfile(mpath):
                try:
                    with open(mpath, "r", encoding="utf-8") as fp:
                        m = json.load(fp)
                except (OSError, json.JSONDecodeError):
                    m = {}
            has_csv = os.path.isfile(os.path.join(path, DATA_NAME))
            found.append({
                "dir": path, "name": name,
                "device_code": m.get("device_code", ""),
                "exported_local": m.get("exported_at_local", ""),
                "record_count": m.get("record_count", 0),
                "first_local": m.get("first_local", ""),
                "last_local": m.get("last_local", ""),
                "pile_ids": m.get("pile_ids", []),
                "has_manifest": bool(m),
                "has_data": has_csv,
                "modified_local": fmt_local(os.path.getmtime(path)),
            })
            if len(found) >= limit:
                return found
    return found


def reasons() -> list[dict]:
    """导入界面的原因下拉框。"""
    return [{"code": c, "label": ReasonCode(c).label} for c in IMPORT_REASONS]


def latest_bundle(root: str | None = None) -> str:
    """最近一个导出包（演示里"改完再导回来"总是指它）。"""
    found = list_bundles(root, limit=1)
    return found[0]["dir"] if found else ""
