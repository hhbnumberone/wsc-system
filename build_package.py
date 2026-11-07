# -*- coding: utf-8 -*-
"""把整个系统打成一个能直接发出去的文件夹。

刻意**不用** shutil.copytree(ROOT, ROOT/12345)：目标目录在源目录里面，
整树递归会把自己套进去。这里按清单逐项复制。

同理也不往包里带：__pycache__（字节码绑解释器版本，换了机器就是废的）、
data/（运行期数据，对方那边 --demo 会重新播种）、data/logs（里面全是我这台
机器的绝对路径）。
"""
from __future__ import annotations

import io
import os
import shutil
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(ROOT, "12345")

# 要带走的源码目录
DIRS = ["common", "can_bus", "server", "terminal", "config", "tools", "tests"]
# 单文件
FILES = ["run_all.py", "README.md"]
# 说明书：源在根目录，进包后统一收到「文档」子目录。
# **只带 PDF** —— make_manual*.py 已经是"只出 PDF"，docx 是转 PDF 的临时中间产物，
# 不会留在源码目录，这里也就不该再列它（列了会走下面那句 missing 提示，白报一次警）。
DOCS = [
    "湿喷台车信息化系统 11项功能操作说明.pdf",
    "湿喷台车信息化系统 演示使用说明.pdf",
]
# 这里原来是一句静默过滤 `[d for d in DOCS if exists(d)]`，而我把上面第四份的
# 文件名打成了"湿喷**喷**台车"，于是它一声不吭地少复制了一份 PDF。过滤可以，
# 但缺了什么必须喊出来。
missing = [d for d in DOCS if not os.path.exists(os.path.join(ROOT, d))]
for d in missing:
    print(f"  !! 缺文档 {d}")
DOCS = [d for d in DOCS if d not in missing]

SKIP_NAMES = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIX = (".pyc", ".pyo")


def ignore(_dir, names):
    return [n for n in names if n in SKIP_NAMES or n.endswith(SKIP_SUFFIX)]


def wipe(rel: str) -> str:
    p = os.path.join(PKG, rel)
    if os.path.isdir(p):
        shutil.rmtree(p)
    return p


def main() -> int:
    os.makedirs(PKG, exist_ok=True)

    # runtime/ 是已经解压好的便携 Python，复制时整个跳过
    for d in DIRS:
        src = os.path.join(ROOT, d)
        if not os.path.isdir(src):
            print(f"  !! 缺目录 {d}")
            continue
        dst = wipe(d)
        shutil.copytree(src, dst, ignore=ignore)
        n = sum(len(f) for _, _, f in os.walk(dst))
        print(f"  目录 {d:10s} {n:3d} 个文件")

    for f in FILES:
        src = os.path.join(ROOT, f)
        if not os.path.exists(src):
            print(f"  !! 缺文件 {f}")
            continue
        wipe(f)
        shutil.copy2(src, os.path.join(PKG, f))
        print(f"  文件 {f}")

    docdir = os.path.join(PKG, "文档")
    os.makedirs(docdir, exist_ok=True)
    for d in DOCS:
        shutil.copy2(os.path.join(ROOT, d), os.path.join(docdir, d))
        print(f"  文档 {d}")

    # "U 盘"挂载点。留空目录，运行时会往 usb/out/ 里写导出包
    os.makedirs(os.path.join(PKG, "usb", "out"), exist_ok=True)
    print("  目录 usb/out   （空的 U 盘挂载点）")

    print("\n源码部分完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
