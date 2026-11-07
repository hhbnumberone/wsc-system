# -*- coding: utf-8 -*-
"""一键起整套演示：云平台 + 车载终端。

做的事按顺序就三件，任何一件不满足就停下来把原因说清楚：

1. **端口自检** —— 8700 / 8701 被占了就先说被谁占着、怎么放掉。
   演示现场最难受的不是起不来，而是"看起来起来了"，其实有一个端口还被
   上次那个进程占着，页面上少了半屏数据；
2. **起两个子进程** —— 平台在前、终端在后（终端一起来就要连平台）；
3. **Ctrl+C 优雅退出** —— 给两个子进程发 CTRL_BREAK，让它们走正常的
   收尾流程（关库、flush CSV、传完手上那批），而不是 terminate() 直接抹掉。
   为此 `server/main.py` 和 `terminal/app.py` 都注册了 SIGBREAK 处理。

用法：

    python run_all.py                     # 保持现有数据，直接起
    python run_all.py --reset             # 两端数据全部重来
    python run_all.py --reset --seed      # 再加 30 天平台历史（首次演示用这个）
    python run_all.py --open              # 起来之后自动开浏览器
    python run_all.py --demo              # = --reset --seed --open

**数据在哪**：`data/server/wsc.sqlite3`（平台）、`data/terminal/local.db`
与 `data/terminal/frames/*.csv`（终端）。`--reset` 清的就是这几样；
`usb/out/` 里的导出包不受影响 —— 那是"U 盘"，拔了盘数据还在盘上。
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from common import config            # noqa: E402  （要在 sys.path 之后导）

# 子进程退出后等它自己收尾的时间。给足 15 秒：终端的收尾里有一步是
# "把手上那批传完"，卡在超时上也得让它把 CSV flush 掉。
SHUTDOWN_GRACE_S = 15.0
# 等两端真的能应答再开浏览器。直接开的话浏览器会先显示一次连接失败。
READY_TIMEOUT_S = 25.0


# --------------------------------------------------------------------------
# 端口
# --------------------------------------------------------------------------
def port_in_use(host: str, port: int) -> bool:
    """能不能连上。**不用 bind 探测** —— 那只能看出"能不能绑"，
    而这里的重点是"已经有人占了没有"，连一下最直接。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def owner_of(port: int) -> str:
    """谁占着这个端口。查不到就返回空串 —— 这只是给人看的提示，
    不值得为它引入一个 Windows 专属的硬依赖。"""
    if os.name != "nt":                     # pragma: no cover - 演示机是 Windows
        return ""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=8,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):   # pragma: no cover
        return ""
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].endswith(f":{port}"):
            if parts[3].upper() == "LISTENING":
                return parts[4]
    return ""


def check_ports(cfg: dict) -> list[str]:
    """返回被占用的 [描述]。空列表 = 可以起。"""
    busy = []
    for section, label in (("server", "平台"), ("terminal", "车载终端")):
        host = cfg[section]["host"]
        port = int(cfg[section]["port"])
        if not port_in_use(host, port):
            continue
        pid = owner_of(port)
        busy.append(f"{label} {host}:{port} 已被占用"
                    + (f"（PID {pid}）" if pid else ""))
    return busy


def _advice(cfg: dict) -> str:
    ports = ", ".join(str(int(cfg[s]["port"])) for s in ("server", "terminal"))
    lines = ["先看看是谁占着：", f"    netstat -ano | findstr \"{ports.replace(', ', ' ')}\""]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 子进程
# --------------------------------------------------------------------------
def spawn(name: str, args: list[str]) -> subprocess.Popen:
    """起一个子进程。

    `CREATE_NEW_PROCESS_GROUP` 是必须的：只有进程组组长才能收到
    CTRL_BREAK，否则下面那份优雅退出就无从谈起。
    """
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    print(f"  起 {name}：python -m {' '.join(args)}")
    return subprocess.Popen([sys.executable, "-m", *args], cwd=ROOT,
                            creationflags=flags)


def stop_child(name: str, proc: subprocess.Popen) -> None:
    """先礼后兵：CTRL_BREAK → 等 → 还活着才 kill。"""
    if proc.poll() is not None:
        return
    print(f"  正在停 {name}…")
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:                               # pragma: no cover
            proc.send_signal(signal.SIGTERM)
    except (OSError, ValueError):           # pragma: no cover
        proc.terminate()
    try:
        proc.wait(timeout=SHUTDOWN_GRACE_S)
    except subprocess.TimeoutExpired:       # pragma: no cover - 收尾卡住才会走到
        print(f"  {name} {SHUTDOWN_GRACE_S:.0f} 秒没停干净，强制结束")
        proc.kill()
        proc.wait(timeout=5)


def stop_all(children: dict[str, subprocess.Popen]) -> None:
    # **反序**停：终端先停（它要连平台），平台后停 —— 反过来的话，
    # 终端会在平台已经关门的这段时间里攒一堆"连接失败"的日志，
    # 下次翻日志的人会以为出了故障。
    for name, proc in reversed(list(children.items())):
        stop_child(name, proc)


# --------------------------------------------------------------------------
# 就绪探测
# --------------------------------------------------------------------------
def wait_ready(cfg: dict, children: dict[str, subprocess.Popen]) -> bool:
    """等两端的健康接口都能应答（或发现有谁已经死了）。"""
    targets = {
        "平台": (cfg["server"], "/api/health"),
        "终端": (cfg["terminal"], "/api/status"),
    }
    deadline = time.monotonic() + READY_TIMEOUT_S
    pending = dict(targets)
    while pending and time.monotonic() < deadline:
        for name, proc in children.items():
            if proc.poll() is not None:
                print(f"\n!! {name} 启动失败（退出码 {proc.returncode}），见上面它的输出")
                return False
        for label, (sec, path) in list(pending.items()):
            url = f"http://{sec['host']}:{int(sec['port'])}{path}"
            try:
                with urllib.request.urlopen(url, timeout=1.5) as resp:
                    if resp.status == 200:
                        print(f"  {label}就绪：{url.replace(path, '')}")
                        pending.pop(label)
            except (urllib.error.URLError, OSError, ValueError):
                pass
        if pending:
            time.sleep(0.4)
    if pending:
        print(f"\n!! 等了 {READY_TIMEOUT_S:.0f} 秒还没就绪：{'、'.join(pending)}")
        return False
    return True


# --------------------------------------------------------------------------
def banner(cfg: dict, args) -> None:
    srv = f"http://{cfg['server']['host']}:{int(cfg['server']['port'])}"
    trm = f"http://{cfg['terminal']['host']}:{int(cfg['terminal']['port'])}"
    print()
    print("=" * 68)
    print("  湿喷台车信息化系统 —— 演示环境已就绪")
    print("-" * 68)
    print(f"  云平台    {srv}        实时监控 / 历史查询 / 设备台账")
    print(f"  车载终端  {trm}        实时监控 / U 盘与修订 / 设置与诊断")
    print(f"  数据目录  {config.DATA_DIR}")
    print("-" * 68)
    print("  演示脚本见 README.md「演示剧本」一节。最小闭环：")
    print("    终端 模拟断网 → 平台该设备转离线 → 恢复 → 补传把曲线空洞填上")
    print("    终端 U 盘与修订 → 导出 → 模拟改 2 行 → 校验比对 → 导入为修订")
    print("    平台 历史查询 → 该行有「修正」标记 → 点开看原值/新值/经手人")
    print("    终端 设置与诊断 → 注入泵送压力越限 → 平台只多出一条报警")
    print("-" * 68)
    print("  ！这个黑窗口不要关、不要点右上角的 ×，关上演示就停了。")
    print("    演示结束再回到这个窗口按 Ctrl+C。")
    if args.reset:
        print("-" * 68)
        print("  注意：本次是 --reset 起的，两端历史数据已清空")
        if not args.seed:
            print("        平台没有历史数据，历史查询页会是空的（加 --seed 可补 30 天）")
    print("=" * 68)
    print("  Ctrl+C 停止（两个子进程会收到退出信号并走正常收尾）")
    print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="湿喷台车信息化系统 — 一键启动",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="端口改了 config/config.json 也照样适用，这里读的就是它。")
    ap.add_argument("--reset", action="store_true", help="清空两端本地数据后启动")
    ap.add_argument("--seed", action="store_true", help="生成 30 天平台历史数据")
    ap.add_argument("--open", action="store_true", help="就绪后自动打开两个页面")
    ap.add_argument("--demo", action="store_true",
                    help="首次演示的一步到位：等于 --reset --seed --open")
    args = ap.parse_args(argv)
    if args.demo:
        args.reset = args.seed = args.open = True

    cfg = config.load_config()
    config.ensure_default_files()

    print("\n① 端口自检")
    busy = check_ports(cfg)
    if busy:
        print("  起不来：")
        for line in busy:
            print("    " + line)
        print()
        print(_advice(cfg))
        print("  放掉之后重跑本命令即可。")
        return 1
    ports = "、".join(f"{cfg[s]['host']}:{int(cfg[s]['port'])}"
                     for s in ("server", "terminal"))
    print(f"  {ports} 都空着")

    print("\n② 启动子进程")
    server_args = ["server.main"]
    if args.reset:
        server_args.append("--reset")
    if args.seed:
        server_args.append("--seed")
    terminal_args = ["terminal.app"]
    if args.reset:
        terminal_args.append("--reset")

    children: dict[str, subprocess.Popen] = {}
    try:
        children["平台"] = spawn("平台", server_args)
        # 终端一起来就要连平台。不等它先听上，终端头几秒的心跳会白失败一轮，
        # 界面上"链路探测失败 3 次"的痕迹会留到演示开始之后。
        time.sleep(1.2)
        children["终端"] = spawn("终端", terminal_args)

        print("\n③ 等服务就绪")
        if not wait_ready(cfg, children):
            stop_all(children)
            return 1
    except KeyboardInterrupt:
        pass

    banner(cfg, args)
    if args.open:
        webbrowser.open(f"http://{cfg['server']['host']}:{int(cfg['server']['port'])}")
        webbrowser.open(f"http://{cfg['terminal']['host']}:{int(cfg['terminal']['port'])}")

    # 主循环：盯着两个子进程，谁先没了就把另一个也停掉。
    # 只起一半的演示环境比完全起不来更难查 —— 页面上有数据，但另一端的
    # 操作全部无效，现场会先怀疑是代码 bug。
    code = 0
    try:
        while True:
            for name, proc in children.items():
                rc = proc.poll()
                if rc is not None:
                    print(f"\n!! {name} 已退出（退出码 {rc}），另一端也停掉")
                    code = rc or 1
                    stop_all(children)
                    return code
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C")
    finally:
        stop_all(children)
    print("已全部停止")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
