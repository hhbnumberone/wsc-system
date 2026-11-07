# -*- coding: utf-8 -*-
"""生成《湿喷台车信息化系统 演示使用说明》（仅 pdf）。

读者是**要上手操作这套演示的人**，不是开发。所以全篇只有三件事：
怎么打开、点哪儿看到什么、出问题怎么办。不解释架构 —— 那些在 README.md 里。

    python make_manual.py

样式沿用产品计划书那份（同色系、同字体），放在一起看不出是两套东西。
PDF 走 Word COM 转换，所以这台机器上得装了 Word（实测 Office 16.0）。

**中间产物 docx 只写在临时目录里，转完即删** —— 项目里只留 PDF。
只有转换失败时才把 docx 落到本目录，好让人用 Word 手动「另存为 PDF」。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

DIR = os.path.dirname(os.path.abspath(__file__))
OUT_PDF = os.path.join(DIR, "湿喷台车信息化系统 演示使用说明.pdf")
#: 只在转换失败时才落地的兜底文件名 —— 正常情况下本目录不该出现 docx
FALLBACK_DOCX = os.path.join(DIR, "湿喷台车信息化系统 演示使用说明.docx")

NAVY = (0x1F, 0x4E, 0x79)
BLUE = (0x2E, 0x75, 0xB6)
DARK = (0x26, 0x26, 0x26)
GRAY = (0x59, 0x59, 0x59)
RED = (0xC0, 0x39, 0x2B)
GREEN = (0x1E, 0x7A, 0x3C)

BODY_EA = "宋体"
HEAD_EA = "黑体"
ASCII_F = "Times New Roman"
HEAD_ASCII = "Arial"


# ------------------------------------------------------------------ 工具
def sr(run, size=11, bold=False, color=None, ea=BODY_EA, ascii_f=ASCII_F):
    run.font.size = Pt(size)
    run.font.bold = bold
    if color:
        run.font.color.rgb = RGBColor(*color)
    run.font.name = ascii_f
    rPr = run._element.get_or_add_rPr()
    rf = rPr.get_or_add_rFonts()
    rf.set(qn("w:ascii"), ascii_f)
    rf.set(qn("w:hAnsi"), ascii_f)
    rf.set(qn("w:eastAsia"), ea)


def para(doc, text, size=11, bold=False, align=None, color=None,
         indent=0, before=0, after=6, line=1.4):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    pf.line_spacing = line
    if indent:
        pf.first_line_indent = Pt(size * indent)
    if align is not None:
        p.alignment = align
    sr(p.add_run(text), size=size, bold=bold, color=color)
    return p


def rich(doc, parts, size=11, indent=0, after=6, line=1.4):
    """一段里混排不同粗细/颜色的文字。parts = [(text, bold, color), ...]"""
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_after = Pt(after)
    pf.line_spacing = line
    if indent:
        pf.first_line_indent = Pt(size * indent)
    for t, b, c in parts:
        sr(p.add_run(t), size=size, bold=b, color=c)
    return p


def bullet(doc, parts, size=11, lead="• "):
    """项目符号段落。parts 同 rich()。"""
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.left_indent = Pt(22)
    pf.first_line_indent = Pt(-12)
    pf.space_before = Pt(0)
    pf.space_after = Pt(3)
    pf.line_spacing = 1.4
    sr(p.add_run(lead), size=size)
    for t, b, c in parts:
        sr(p.add_run(t), size=size, bold=b, color=c)
    return p


def step(doc, n, parts, size=11):
    """带序号圆点的步骤行。"""
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.left_indent = Pt(22)
    pf.first_line_indent = Pt(-22)
    pf.space_before = Pt(0)
    pf.space_after = Pt(4)
    pf.line_spacing = 1.4
    sr(p.add_run(f"{n}. "), size=size, bold=True, color=BLUE)
    for t, b, c in parts:
        sr(p.add_run(t), size=size, bold=b, color=c)
    return p


def shade(cell, hexcolor):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hexcolor)
    tcPr.append(shd)


def table(doc, headers, rows, widths, fs=10, hdr_bg="2E75B6", center_cols=None):
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    center_cols = center_cols or []

    for i, h in enumerate(headers):
        c = t.rows[0].cells[i]
        c.text = ""
        p = c.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after = Pt(2)
        p.paragraph_format.line_spacing = 1.15
        sr(p.add_run(h), size=fs, bold=True, color=(255, 255, 255),
           ea=HEAD_EA, ascii_f=HEAD_ASCII)
        shade(c, hdr_bg)

    for ri, row in enumerate(rows):
        cells = t.add_row().cells
        for i, v in enumerate(row):
            c = cells[i]
            c.text = ""
            p = c.paragraphs[0]
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.line_spacing = 1.15
            if i in center_cols:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            sr(p.add_run(str(v)), size=fs)
            if ri % 2 == 1:
                shade(c, "F4F8FC")

    for r in t.rows:
        for i, w in enumerate(widths):
            if i < len(r.cells):
                r.cells[i].width = Cm(w)
    return t


def callout(doc, title, body_parts, fill="FFF4E5", bar=RED):
    """提示框：一个单元格的表格，左边一条色边看着像便签。"""
    t = doc.add_table(rows=1, cols=1)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    c = t.rows[0].cells[0]
    c.text = ""
    c.width = Cm(15.4)
    shade(c, fill)
    p = c.paragraphs[0]
    p.paragraph_format.space_before = Pt(3)
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.line_spacing = 1.3
    sr(p.add_run(title), size=10.5, bold=True, color=bar,
       ea=HEAD_EA, ascii_f=HEAD_ASCII)
    q = c.add_paragraph()
    q.paragraph_format.space_after = Pt(3)
    q.paragraph_format.line_spacing = 1.3
    for txt, b, col in body_parts:
        sr(q.add_run(txt), size=10, bold=b, color=col)
    para(doc, "", after=4)


def code(doc, lines, fs=10):
    """等宽代码块（灰底单元格）。"""
    t = doc.add_table(rows=1, cols=1)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    c = t.rows[0].cells[0]
    c.text = ""
    c.width = Cm(15.4)
    shade(c, "F2F2F2")
    for i, ln in enumerate(lines):
        p = c.paragraphs[0] if i == 0 else c.add_paragraph()
        p.paragraph_format.space_before = Pt(1)
        p.paragraph_format.space_after = Pt(1)
        p.paragraph_format.line_spacing = 1.15
        sr(p.add_run(ln), size=fs, ea="Consolas", ascii_f="Consolas")
    para(doc, "", after=4)


def pagebreak(doc):
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def h1(doc, text):
    return doc.add_heading(text, level=1)


def h2(doc, text):
    return doc.add_heading(text, level=2)


# ------------------------------------------------------------------ 样式
def setup_styles(doc):
    st = doc.styles["Normal"]
    st.font.name = ASCII_F
    st.font.size = Pt(11)
    st.font.color.rgb = RGBColor(*DARK)
    rf = st.element.get_or_add_rPr().get_or_add_rFonts()
    rf.set(qn("w:ascii"), ASCII_F)
    rf.set(qn("w:hAnsi"), ASCII_F)
    rf.set(qn("w:eastAsia"), BODY_EA)
    pf = st.paragraph_format
    pf.line_spacing = 1.4
    pf.space_after = Pt(6)

    for name, size, color, sb, sa in (("Heading 1", 17, NAVY, 18, 10),
                                      ("Heading 2", 13.5, BLUE, 14, 7)):
        s = doc.styles[name]
        s.font.size = Pt(size)
        s.font.bold = True
        s.font.italic = False
        s.font.color.rgb = RGBColor(*color)
        s.font.name = HEAD_ASCII
        r = s.element.get_or_add_rPr().get_or_add_rFonts()
        r.set(qn("w:ascii"), HEAD_ASCII)
        r.set(qn("w:hAnsi"), HEAD_ASCII)
        r.set(qn("w:eastAsia"), HEAD_EA)
        p = s.paragraph_format
        p.space_before = Pt(sb)
        p.space_after = Pt(sa)
        p.line_spacing = 1.25
        p.keep_with_next = True


def setup_page(doc):
    s = doc.sections[0]
    s.page_width = Cm(21.0)
    s.page_height = Cm(29.7)
    s.top_margin = Cm(2.4)
    s.bottom_margin = Cm(2.2)
    s.left_margin = Cm(2.8)
    s.right_margin = Cm(2.8)

    fp = s.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = fp.add_run()
    f1 = OxmlElement("w:fldChar"); f1.set(qn("w:fldCharType"), "begin")
    it = OxmlElement("w:instrText"); it.set(qn("xml:space"), "preserve")
    it.text = "PAGE"
    f2 = OxmlElement("w:fldChar"); f2.set(qn("w:fldCharType"), "end")
    r._r.append(f1); r._r.append(it); r._r.append(f2)
    sr(r, size=9, color=GRAY)

    uf = OxmlElement("w:updateFields")
    uf.set(qn("w:val"), "true")
    doc.settings.element.append(uf)


# ------------------------------------------------------------------ 正文
def cover(doc):
    para(doc, "", after=90)
    para(doc, "湿喷台车信息化系统", size=26, bold=True, color=NAVY,
         align=WD_ALIGN_PARAGRAPH.CENTER, after=6, line=1.2)
    para(doc, "演示使用说明", size=20, bold=True, color=BLUE,
         align=WD_ALIGN_PARAGRAPH.CENTER, after=30, line=1.2)
    para(doc, "河南省耿力工程设备有限公司", size=13, color=GRAY,
         align=WD_ALIGN_PARAGRAPH.CENTER, after=4)
    para(doc, "大丘田隧道右线 · 设备 GHP9015H", size=12, color=GRAY,
         align=WD_ALIGN_PARAGRAPH.CENTER, after=60)

    t = doc.add_table(rows=1, cols=1)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    c = t.rows[0].cells[0]
    c.text = ""
    c.width = Cm(13)
    shade(c, "F4F8FC")
    p = c.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(6)
    sr(p.add_run("本文只讲一件事：怎么把演示跑起来、点哪儿、看什么。\n"
                 "系统原理与设计取舍见项目目录下的 README.md。"),
       size=10.5, color=GRAY)
    pagebreak(doc)


def sec_open(doc):
    h1(doc, "一、怎么打开")

    para(doc, "整套演示就是两个网页：一个在车载终端上，一个在云平台上。"
              "你要做的只有一件事 —— 把服务起起来，然后开浏览器。", after=10)

    h2(doc, "1.1 启动（推荐：双击）")
    para(doc, "打开项目文件夹 wsc-system，双击：", after=6)
    code(doc, ["启动演示.bat        ← 清空旧数据、生成 30 天历史、自动开浏览器",
               "继续启动.bat        ← 保留上次的数据接着看"])
    rich(doc, [("第一次演示、或者想要一个干净的开场，用 ", False, None),
               ("启动演示.bat", True, BLUE),
               ("。只是想再看看上次的东西，用 ", False, None),
               ("继续启动.bat", True, BLUE),
               ("。", False, None)], after=8)

    callout(doc, "！ 那个黑窗口不要关",
            [("启动后会留着一个黑色命令行窗口。", False, None),
             ("演示期间它必须一直开着 —— 关掉它，或者点它右上角的 ×，演示立刻就停了。", True, RED),
             ("演示结束再回到那个窗口按 Ctrl+C。", False, None)])

    para(doc, "窗口里会打印一堆启动日志，最后出现这样一段，就说明好了：", after=6)
    code(doc, ["① 端口自检",
               "  127.0.0.1:8700、127.0.0.1:8701 都空着",
               "② 启动子进程",
               "③ 等服务就绪",
               "  平台就绪：http://127.0.0.1:8700",
               "  终端就绪：http://127.0.0.1:8701"])

    h2(doc, "1.2 打开页面")
    para(doc, "浏览器会自动弹出两个标签页。如果没弹，或者你不小心关掉了，"
              "在浏览器地址栏手工输入这两个地址：", after=8)

    table(doc,
          ["看哪一端", "地址", "是什么"],
          [["车载终端", "http://127.0.0.1:8701", "装在台车上的那块屏，操作都在这里"],
           ["云平台", "http://127.0.0.1:8700", "项目部/公司看的数据中心，只读"]],
          [2.6, 5.6, 7.2], center_cols=[0])

    para(doc, "", after=4)
    callout(doc, "地址一定写 127.0.0.1，不要写 localhost",
            [("这台机器的 localhost 会先解析到 IPv6 的 ::1，偶尔会卡上半秒才通。"
              "写字面量 127.0.0.1 最稳。", False, None)],
            fill="EAF3FB", bar=BLUE)

    h2(doc, "1.3 停止")
    para(doc, "回到那个黑窗口，按 Ctrl+C。", after=6)
    para(doc, "会看到「正在停 终端…」之类的提示，然后它自己收尾 —— "
              "关数据库、把手上那批数据传完。等它打印「已全部停止」再关窗口。"
              "这个过程可能要几秒到十几秒，是正常的，别急着点 ×。", after=10)

    callout(doc, "已经关错了窗口怎么办",
            [("直接点 × 关掉，会留下两个占用端口的进程，下次启动就会报"
              "「8700 已被占用（PID xxxx）」。", False, None),
             ("解决：按 Ctrl+Shift+Esc 打开任务管理器，找到 python.exe 全部结束掉；"
              "或者直接重启电脑。", False, None)])

    # 这里**不加分页**：1.3 只有三行，硬分页会甩出一张大半空白的纸。
    # 章节从新页开始好看，但页面上留一大块空白更难看。


def sec_pages(doc):
    h1(doc, "二、页面上都有什么")

    para(doc, "两端一共六个页面，每个页面只干一件事。"
              "先认识一下，演示的时候才知道往哪儿点。", after=10)

    h2(doc, "2.1 车载终端（127.0.0.1:8701）—— 操作都在这一端")
    table(doc,
          ["页面", "里面有什么", "演示时用它做什么"],
          [["实时监控",
            "实时参数、实时曲线、作业桩号、喷浆循环、\n采集记录、上传记录",
            "讲数据是怎么采上来、传上去的；\n断网演示也在这个页面"],
           ["U 盘与修订",
            "① 导出到 U 盘  ② 校验与比对\n③ 导入为修订  ④ 修订队列",
            "整个「改了数据能追溯」的演示"],
           ["设置与诊断",
            "设备与链路、故障注入、桩号清单、\n参数与阈值（只读）、运行日志",
            "注入故障看报警；出问题时\n在这里看日志"]],
          [2.6, 6.6, 6.2], center_cols=[0])

    para(doc, "", after=6)

    h2(doc, "2.2 云平台（127.0.0.1:8700）—— 只读，给客户看的")
    table(doc,
          ["页面", "里面有什么"],
          [["首页（实时监控）", "实时曲线、设备状态、报警记录、喷浆循环"],
           ["历史数据", "历史曲线、原始记录 —— 演示「修正」标记就在这里看"],
           ["设备与传输", "设备清单、上传批次、拒收明细、操作留痕（审计事件）"]],
          [3.6, 11.8], center_cols=[0])

    para(doc, "", after=8)

    callout(doc, "终端页面上找不到「桩号输入框」，那不是漏做了",
            [("桩号只能从平台下发的那份清单里点选，界面上没有地方能手工敲一个桩号进去。"
              "这是刻意的设计：", False, None),
             ("桩号造不了假。", True, RED),
             ("被客户问到的时候，这就是答案。", False, None)],
            fill="EAF3FB", bar=BLUE)


def sec_demo(doc):
    h1(doc, "三、演示怎么走")

    para(doc, "下面这套顺序大约 12 分钟。每一条都写清楚：点什么、看到什么、"
              "这一条在证明什么。照着做就行。", after=10)

    h2(doc, "3.0 开场（1 分钟）")
    step(doc, 1, [("双击 ", False, None), ("启动演示.bat", True, BLUE),
                  ("，等两个页面自动打开，窗口并排摆好：左边平台，右边终端。", False, None)])

    h2(doc, "3.1 链路是通的（1 分钟）")
    step(doc, 1, [("两边都不用点，让客户看十秒。", False, None)])
    bullet(doc, [("终端首页的曲线在实时滚动，右下角「上传记录」里批次一条条显示 ", False, None),
                 ("ACKED", True, GREEN), ("。", False, None)])
    bullet(doc, [("平台首页设备是", False, None), ("在线", True, GREEN),
                 ("，参数值跟着跳。", False, None)])
    rich(doc, [("说明一句：", False, None),
               ("「CAN 采集 → 车载终端 → 网络 → 平台，这条链路是活的。」", True, NAVY)], after=10)

    h2(doc, "3.2 断网不丢数据（3 分钟）—— 重点")
    step(doc, 1, [("在", False, None), ("终端首页", True, BLUE),
                  ("点红色的「模拟断网」。", False, None)])
    step(doc, 2, [("等 20～30 秒（正好用来讲话）。", False, None)])
    step(doc, 3, [("再点一次那个按钮（这时写着「恢复联网」），看着它把数据补传完。", False, None)])
    para(doc, "", after=2)
    rich(doc, [("断网期间要让客户注意两件事：", True, NAVY)], after=4)
    bullet(doc, [("终端的曲线", False, None), ("照常在走", True, RED),
                 (" —— 采集和上传是两条路，断网只断后面那条。", False, None)])
    bullet(doc, [("「待传」的计数一直往上涨，平台那边的曲线停住不动了。", False, None)])
    rich(doc, [("恢复之后：终端的待传计数掉回 0，平台曲线", False, None),
               ("刚才那段空洞被完整填上", True, GREEN),
               ("，时间戳是连续的。", False, None)], after=6)
    rich(doc, [("这一条证明：", False, None),
               ("数据不丢。", True, RED), ("断网期间一条都没丢。", False, None)], after=10)

    h2(doc, "3.3 重复发送不会重（1.5 分钟）")
    para(doc, "开一个新的命令行窗口，粘这一行：", after=6)
    code(doc, ["cd /d \"d:\\新建文件夹 (2)\\wsc-system\"",
               "python tools\\prove_idempotent.py"])
    rich(doc, [("屏幕上会打出三个回合：同一批数据发两次，", False, None),
               ("平台帧总数一个都没变", True, GREEN),
               ("；第三次把数值改掉再发，", False, None),
               ("被整批拒绝", True, RED), ("。", False, None)], after=6)
    rich(doc, [("这一条证明：", False, None),
               ("不重。", True, RED),
               ("重传会被认出来，不是插进去两份。", False, None)], after=10)

    h2(doc, "3.4 改了数据能追溯（3 分钟）—— 重点")
    step(doc, 1, [("终端切到", False, None), ("「U 盘与修订」", True, BLUE),
                  ("页，在 ① 里点「导出到 U 盘」。", False, None)])
    step(doc, 2, [("用记事本打开导出包里的 CSV，把两行的泵送压力改成明显不同的值"
                   "（比如 8.5 改成 15.0），存盘。", False, None)])
    step(doc, 3, [("回到页面 ②，点「校验与比对」—— 报告里会列出这两处差异。", False, None)])
    step(doc, 4, [("在 ③ 里填操作人（比如「张工」）、选原因（数值失真）、写个备注，"
                   "点「导入为修订」。", False, None)])
    step(doc, 5, [("切到", False, None), ("平台 → 历史数据", True, BLUE),
                  ("，找到那两行。", False, None)])
    rich(doc, [("那两行会带一个", False, None), ("「修正」", True, RED),
               ("标记。点开能看到", False, None),
               ("原值、新值、经手人、原因、时间", True, NAVY),
               (" —— 原值还在，没有被覆盖掉。", False, None)], after=6)
    rich(doc, [("这一条证明：", False, None),
               ("改了能追溯。", True, RED)], after=4)
    callout(doc, "这段一定要配一句话说",
            [("「系统不是不允许改数据 —— 工程上确实会遇到传感器漂移、接线接反。"
              "我们要保证的是：改完以后原值还在，谁改的能追到人。」", False, None)],
            fill="EAF3FB", bar=BLUE)

    h2(doc, "3.5 报警不刷屏（1.5 分钟）")
    step(doc, 1, [("终端切到", False, None), ("「设置与诊断」", True, BLUE),
                  ("页，在「故障注入」里点「泵送压力升高」。", False, None)])
    step(doc, 2, [("等十来秒，看", False, None), ("平台首页的报警记录", True, BLUE),
                  ("。", False, None)])
    rich(doc, [("平台上", False, None),
               ("只多出一条报警", True, GREEN),
               ("（不是十几条）。故障解除后过 10 秒，报警自动清除。", False, None)], after=6)
    rich(doc, [("这一条证明：", False, None),
               ("报警是经过工程处理的 —— 持续越限 3 秒才触发、恢复 10 秒才清除，"
                "不会在阈值附近来回刷屏。", False, None)], after=10)

    h2(doc, "3.6 收尾")
    step(doc, 1, [("回黑窗口按 Ctrl+C，等它打印「已全部停止」。", False, None)])


def sec_trouble(doc):
    h1(doc, "四、出问题了怎么办")

    h2(doc, "4.1 常见现象")
    table(doc,
          ["现象", "多半是什么", "怎么办"],
          [["双击 bat，窗口一闪就没了",
            "没装 Python，或者 Python 不在 PATH 里",
            "命令行敲 python --version 看看。没有就装一个 Python 3"],
           ["提示「8700 已被占用（PID 1234）」",
            "上一次的进程没退干净",
            "任务管理器里结束所有 python.exe；\n或直接重启电脑"],
           ["平台页面打不开",
            "平台进程没起来",
            "看黑窗口里平台那几行日志，\n通常是端口被占"],
           ["终端曲线有值，平台不动",
            "终端处于「离线」状态",
            "看终端状态栏是不是写着离线，\n是就点「恢复联网」"],
           ["点了按钮没反应\n（比如「模拟断网」）",
            "演示没在跑。页面上是上一次留下的\n旧标签页，看着还在，其实早断了",
            "页面顶栏会有一条红字写着\n「服务没有响应」。重新双击\n「启动演示.bat」，再刷新本页"],
           ["补传卡着不走",
            "平台没起来，终端在一直重试",
            "先把平台救活，终端会自己接上"],
           ["整个环境乱了",
            "——",
            "Ctrl+C 停掉，重新双击\n「启动演示.bat」，30 秒的事"]],
          [4.2, 4.4, 6.8])

    para(doc, "", after=8)

    h2(doc, "4.2 演示前十分钟，照这个清单过一遍")
    for txt in [
        "黑窗口能正常起来，最后打印出两个「就绪」。",
        "两个页面都能打开，并排摆好；浏览器按 Ctrl+0 复位缩放。",
        "平台「历史数据」页有 30 天的曲线，不是空的。",
        "终端首页「上传记录」里最近几条都是 ACKED，没有一堆红色错误。",
        "终端「U 盘与修订」页里，导出目录下至少有一个包（没有就先点一次「导出到 U 盘」）。"
        "—— 第 3.4 节要用。",
        "先跑一遍 python tools\\prove_idempotent.py，确认三个回合全打 [OK]。"
        "—— 别当着客户的面第一次跑。",
        "关掉会弹窗的东西：邮件、微信、系统更新提醒。",
    ]:
        bullet(doc, [(txt, False, None)])

    para(doc, "", after=8)

    h2(doc, "4.3 两个提醒")
    callout(doc, "改哪个值，要改得看得出来",
            [("第 3.4 节改数据时，务必改成一个肉眼就能看出差别的值"
              "（比如 8.5 改成 15.0）。改成 8.5 → 8.6 那种，观众看不出来，就白改了。",
              False, None)])
    callout(doc, "不要在有数据正在传的时候去动 U 盘文件夹",
            [("导出包是写完才落盘的，本身是安全的。但万一卡住，现场解释的成本"
              "远高于收益 —— 等它传完再操作。", False, None)],
            fill="EAF3FB", bar=BLUE)


def sec_footer(doc):
    h1(doc, "五、想深入了解")
    para(doc, "本文只覆盖「怎么用」。系统的架构、数据不丢不重的具体做法、"
              "桩号防伪的三层设计、参数表、已知限制这些内容，都在：", after=6)
    code(doc, ["wsc-system\\README.md"])
    para(doc, "README 里还带一份更完整的演示剧本（7 个场景）和一份演示前检查表，"
              "跟本文第三节是同一套流程，只是讲得更细、更适合开发人员看。", after=10)
    para(doc, "另外，项目根目录下还有一份《湿喷台车信息化系统产品计划书》"
              "（PDF），那是给客户看的方案文档，讲的是这套系统"
              "要解决什么问题、包含哪些功能、怎么部署。", after=6)


def main():
    doc = Document()
    setup_styles(doc)
    setup_page(doc)

    cover(doc)
    sec_open(doc)
    sec_pages(doc)
    sec_demo(doc)
    sec_trouble(doc)
    sec_footer(doc)

    # docx 只是转 PDF 的中间产物，写在临时目录里 —— 本目录只留 PDF。
    tmpdir = tempfile.mkdtemp(prefix="wsc_manual_")
    docx_path = os.path.join(tmpdir, "manual.docx")
    try:
        doc.save(docx_path)

        # ---- 转 PDF（走 Word COM，需要本机装了 Word） ----
        try:
            import comtypes.client as cc
            word = cc.CreateObject("Word.Application")
            word.Visible = False
            try:
                d = word.Documents.Open(docx_path, ReadOnly=True)
                # 17 = wdExportFormatPDF
                d.SaveAs(OUT_PDF, FileFormat=17)
                d.Close(False)
            finally:
                word.Quit()
            print(f"已生成 {OUT_PDF}")
        except Exception as exc:        # pragma: no cover - 依赖本机 Word
            print(f"PDF 转换失败（{type(exc).__name__}: {exc}）")
            # 兜底：把 docx 交出来，好让人用 Word 手动「另存为 PDF」
            shutil.move(docx_path, FALLBACK_DOCX)
            print(f"docx 已留在 {FALLBACK_DOCX}")
            print("用 Word 打开它、另存为 PDF，然后删掉即可。")
            return 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
