# -*- coding: utf-8 -*-
"""模拟"对方刚解压完，双击启动演示.bat"。

两件事值得说明：

1. **先删掉 data/** —— 这是"刚解压的包"和"我这台跑过的包"唯一的差别，
   也是最容易漏测的一步：很多程序在空数据目录上会直接起不来。

2. **换端口跑** —— 8700/8701 上现在有用户自己那份演示在跑，不能去打扰它。
   本脚本临时把包内 config.json 的端口改成 8790/8791，测完还原。
   端口的可配置性本来就是要求之一，正好一起验了。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(ROOT, "12345")
BAT = os.path.join(PKG, "启动演示.bat")
CFG = os.path.join(PKG, "config", "config.json")

SPORT, TPORT = 8790, 8791
SERVER = f"http://127.0.0.1:{SPORT}"
TERM = f"http://127.0.0.1:{TPORT}"


def hit(url: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", "Connection": "close"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"_err": str(e)}


def main() -> int:
    original = io.open(CFG, encoding="utf-8").read()
    cfg = json.loads(original)
    cfg["server"]["port"] = SPORT
    cfg["terminal"]["port"] = TPORT
    io.open(CFG, "w", encoding="utf-8").write(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    print(f"① 临时改端口为 {SPORT}/{TPORT}（避开你正在跑的那份演示）")

    data = os.path.join(PKG, "data")
    if os.path.isdir(data):
        shutil.rmtree(data)
    print("   并删除 data/ —— 模拟刚解压的状态")

    proc = subprocess.Popen(["cmd", "/c", BAT], cwd=PKG,
                            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    ok = True
    try:
        print(f"\n② 已双击 启动演示.bat（cmd PID {proc.pid}）")

        print("\n③ 等待两端就绪（--demo 要生成 30 天数据）")
        deadline = time.monotonic() + 150
        ready: set[str] = set()
        while time.monotonic() < deadline and len(ready) < 2:
            if proc.poll() is not None:
                print(f"   !! cmd 已退出，退出码 {proc.returncode} —— 见上面它的输出")
                return 1
            for label, url in (("平台", f"{SERVER}/api/health"),
                               ("终端", f"{TERM}/api/status")):
                if label not in ready and hit(url)[0] == 200:
                    ready.add(label)
                    print(f"   {label}就绪：{url}")
            if len(ready) < 2:
                time.sleep(0.5)
        if len(ready) < 2:
            print(f"   !! 超时，只就绪了 {ready}")
            return 1

        print("\n④ 平台功能抽查")
        for label, path in (("运行总览", "/api/overview"),
                            ("帧数据", "/api/frames?limit=5"),
                            ("喷浆循环", "/api/cycles"),
                            ("设备清单", "/api/devices"),
                            ("桩号清单", "/api/piles"),
                            ("报警", "/api/alarms"),
                            ("审计", "/api/audit?limit=5"),
                            ("批次", "/api/batches")):
            st, body = hit(SERVER + path)
            if st != 200:
                ok = False
            n = ""
            for k in ("count", "total"):
                if isinstance(body, dict) and k in body:
                    n = f" {k}={body[k]}"
                    break
            print(f"   {'✓' if st == 200 else '✗'} {label:8s} {st}{n}")

        st, body = hit(SERVER + "/api/devices")
        devs = body.get("devices", []) if isinstance(body, dict) else []
        if devs:
            print(f"     设备台账：{devs[0].get('device_code')} @ {devs[0].get('site_id')}"
                  f"  note={devs[0].get('note')!r}")

        print("\n⑤ 终端功能抽查")
        for label, path in (("状态", "/api/status"),
                            ("记录", "/api/records?limit=5"),
                            ("桩号", "/api/piles"),
                            ("参数", "/api/params"),
                            ("故障", "/api/faults"),
                            ("U盘", "/api/usb")):
            st, body = hit(TERM + path)
            if st != 200:
                ok = False
            print(f"   {'✓' if st == 200 else '✗'} {label:8s} {st}")

        print("\n⑥ 新功能：现场录入工点与设备编号")
        # 终端侧：归一化成 200，判定在 check.ok / check.code 里
        st, body = hit(TERM + "/api/identity/check", "POST",
                       {"site_id": "SML-ZK", "device_code": "GHP9015H"})
        c = body.get("check", {}) if isinstance(body, dict) else {}
        # 键是 `move`（{from_site_id, from_site_name}），不是 moved_from。
        mv = c.get("move") or {}
        print(f"   终端核对 SML-ZK      → {st}  ok={c.get('ok')} "
              f"转场自={mv.get('from_site_name')} 桩号={c.get('site', {}).get('pile_count')} "
              f"首桩={c.get('site', {}).get('first_pile')}")
        if st != 200 or not c.get("ok") or not mv:
            ok = False
            print("      !! 期望 ok=True 且带转场提示")

        st, body = hit(TERM + "/api/identity/check", "POST",
                       {"site_id": "DQTYX-XX", "device_code": "GHP9015H"})
        c = body.get("check", {}) if isinstance(body, dict) else {}
        known = [s.get("site_id") for s in (c.get("known_sites") or [])]
        print(f"   终端核对不存在的工点 → {st}  ok={c.get('ok')} code={c.get('code')} 已知={known}")
        if c.get("ok") is not False or c.get("code") != "SITE_NOT_ISSUED":
            ok = False

        # 平台侧原文：这里才是 409
        q = urllib.parse.urlencode({"site_id": "DQTYX-XX", "device_code": "GHP9015H"})
        st, body = hit(f"{SERVER}/api/identity/check?{q}")
        print(f"   平台原文判定         → {st}  （期望 409）{str(body)[:80]}")
        if st != 409:
            ok = False
            print("      !! 平台的 409 没发出来")

        st, body = hit(TERM + "/api/identity/check", "POST",
                       {"site_id": "SML-ZK", "device_code": "NEW-99001"})
        c = body.get("check", {}) if isinstance(body, dict) else {}
        dev = c.get("device") or {}
        warn = (c.get("warnings") or [""])[0]
        print(f"   终端核对新设备       → {st}  ok={c.get('ok')} "
              f"平台认识={dev.get('known')}")
        print(f"      平台提示：{warn}")
        if dev.get("known") is not False:
            ok = False
            print("      !! 新设备本该是 known=False")

        print("\n⑦ 网页能否打开")
        for label, base, path in (("平台首页", SERVER, "/"),
                                  ("平台历史", SERVER, "/history"),
                                  ("平台设备", SERVER, "/devices"),
                                  ("终端首页", TERM, "/"),
                                  ("终端设置", TERM, "/settings.html"),
                                  ("终端U盘", TERM, "/usb.html")):
            try:
                with urllib.request.urlopen(base + path, timeout=8) as r:
                    n = len(r.read())
                print(f"   ✓ {label:8s} {r.status}  {n} 字节")
            except Exception as e:
                ok = False
                print(f"   ✗ {label:8s} {e}")

        print("\n⑧ 数据落盘情况")
        for rel in ("data/server/wsc.sqlite3", "data/terminal/local.db",
                    "data/logs/server.log", "data/logs/terminal.log"):
            p = os.path.join(PKG, rel)
            mark = "✓" if os.path.exists(p) else "✗"
            if not os.path.exists(p):
                ok = False
            size = f"{os.path.getsize(p)/1024:.0f} KB" if os.path.exists(p) else "-"
            print(f"   {mark} {rel:32s} {size}")

    finally:
        print("\n⑨ 停止并还原端口")
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True)
        time.sleep(2)
        # 被 kill 掉的子进程不会自己收尾，端口可能还 TIME_WAIT 一会儿，
        # 不影响还原，只要把配置文件写回去。
        io.open(CFG, "w", encoding="utf-8").write(original)
        back = io.open(CFG, encoding="utf-8").read()
        print("   端口配置已还原：", json.loads(back)["server"]["port"],
              json.loads(back)["terminal"]["port"])
        if back != original:
            print("   !! 还原后的配置和原始文件不一致")

    print("\n" + "=" * 60)
    print("  结果：" + ("全部通过 —— 解压即可用" if ok else "有失败项，见上"))
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
