# -*- coding: utf-8 -*-
"""生成《湿喷台车信息化系统 11 项功能操作说明》（仅 pdf）。

与 make_manual.py 的分工：

* make_manual.py —— 按**流程**写：怎么打开、页面上有什么、演示剧本、出问题怎么办。
* 本文件         —— 按**功能**写：产品计划书第 3 章的 11 项功能，一项一节，
                    每节都给「终端端操作」与「平台端操作」两栏可直接照做的步骤。

读者是要在两端页面上逐条演示、逐条核对的人。所以每一节的落点都必须是
**界面上真实存在的按钮和表格**，不是设计意图。凡是原型做得比计划书少的地方，
在「说明」框里明说，不糊过去 —— 演示现场被客户问到才解释，比事先讲清楚难看得多。

    python make_manual_11.py

样式沿用产品计划书与演示使用说明那两份（同色系、同字体）。
PDF 走 Word COM 转换，需要本机装了 Word（实测 Office 16.0）。

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
OUT_PDF = os.path.join(DIR, "湿喷台车信息化系统 11项功能操作说明.pdf")
#: 只在转换失败时才落地的兜底文件名 —— 正常情况下本目录不该出现 docx
FALLBACK_DOCX = os.path.join(DIR, "湿喷台车信息化系统 11项功能操作说明.docx")

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


def emit(p, text, size=11, bold=False, color=None):
    """把文本写进段落，`**这样**` 的部分渲染成粗体。

    正文里到处是「界面上的原文」——按钮名、表头、状态标签。全用引号会糊成一片，
    所以留一个极轻的标记：成对的星号之间加粗，其余照常。
    """
    for i, seg in enumerate(text.split("**")):
        if seg:
            sr(p.add_run(seg), size=size, bold=(bold or i % 2 == 1), color=color)


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
    emit(p, text, size=size, bold=bold, color=color)
    return p


def rich(doc, parts, size=11, indent=0, after=6, line=1.4, before=0, left=0):
    """一段里混排不同粗细/颜色的文字。parts = [(text, bold, color), ...]"""
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    pf.line_spacing = line
    if indent:
        pf.first_line_indent = Pt(size * indent)
    if left:
        pf.left_indent = Pt(left)
    for t, b, c in parts:
        sr(p.add_run(t), size=size, bold=b, color=c)
    return p


def bullet(doc, parts, size=11, lead="• ", left=22):
    """项目符号段落。parts 同 rich()。"""
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.left_indent = Pt(left)
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


def st(doc, n, text, size=11):
    """纯文本步骤（省得每次写 [(text, False, None)]）。"""
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.left_indent = Pt(22)
    pf.first_line_indent = Pt(-22)
    pf.space_before = Pt(0)
    pf.space_after = Pt(4)
    pf.line_spacing = 1.4
    sr(p.add_run(f"{n}. "), size=size, bold=True, color=BLUE)
    emit(p, text, size=size)
    return p


def bl(doc, text, size=11, lead="• "):
    """二级要点（缩进比步进更深，用在步骤内部再分小点）。"""
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.left_indent = Pt(34)
    pf.first_line_indent = Pt(-12)
    pf.space_before = Pt(0)
    pf.space_after = Pt(3)
    pf.line_spacing = 1.4
    sr(p.add_run(lead), size=size)
    emit(p, text, size=size)
    return p


def side(doc, text, color=NAVY, before=10):
    """「终端端」「平台端」这类侧标。"""
    return rich(doc, [(text, True, color)], size=11.5, before=before, after=4)


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


def callout(doc, title, body_parts, fill="FFF4E5", bar=RED, size=10):
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
    sr(p.add_run(title), size=size + 0.5, bold=True, color=bar,
       ea=HEAD_EA, ascii_f=HEAD_ASCII)
    q = c.add_paragraph()
    q.paragraph_format.space_after = Pt(3)
    q.paragraph_format.line_spacing = 1.3
    for txt, b, col in body_parts:
        # 走 emit 而不是 sr：正文里那套 `**加粗**` 的写法在提示框里也该管用，
        # 否则同一句话放进框里就会漏出四个星号。
        emit(q, txt, size=size, bold=b, color=col)
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


def plan_quote(doc, text):
    """「计划书原文」框 —— 每条功能都从计划书那句原话开始，方便对上口径。"""
    return callout(doc, "计划书原文", [(text, False, None)],
                   fill="F4F8FC", bar=BLUE, size=9.5)


def passing(doc, items):
    """通过标准。"""
    side(doc, "通过标准", color=GREEN, before=8)
    for it in items:
        bl(doc, it, lead="✓ ")


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


# ------------------------------------------------------------------ 封面
def cover(doc):
    para(doc, "", after=80)
    para(doc, "湿喷台车信息化系统", size=26, bold=True, color=NAVY,
         align=WD_ALIGN_PARAGRAPH.CENTER, after=6, line=1.2)
    para(doc, "11 项功能操作说明", size=20, bold=True, color=BLUE,
         align=WD_ALIGN_PARAGRAPH.CENTER, after=30, line=1.2)
    para(doc, "河南省耿力工程设备有限公司", size=13, color=GRAY,
         align=WD_ALIGN_PARAGRAPH.CENTER, after=4)
    para(doc, "大丘田隧道右线 · 设备 GHP9015H", size=12, color=GRAY,
         align=WD_ALIGN_PARAGRAPH.CENTER, after=50)

    t = doc.add_table(rows=1, cols=1)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    c = t.rows[0].cells[0]
    c.text = ""
    c.width = Cm(13.5)
    shade(c, "F4F8FC")
    p = c.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.line_spacing = 1.35
    sr(p.add_run("本文按《产品计划书》第 3 章的 11 项功能逐条编排。\n"
                 "每一项都分成「终端端操作」与「平台端操作」两栏，\n"
                 "写在界面上的哪个位置、点哪个按钮、看哪一格数字。"),
       size=10.5, color=GRAY)
    pagebreak(doc)


# ------------------------------------------------------------------ 一、开始之前
def sec_start(doc):
    h1(doc, "一、开始之前")

    para(doc, "全文只涉及两个网页：车载终端（操作端）和云平台（查看端）。"
              "先把它们起起来，后面的 11 条都在里面点。", after=10)

    h2(doc, "1.1 启动")
    para(doc, "打开项目文件夹 wsc-system，双击：", after=6)
    code(doc, ["启动演示.bat        ← 清空旧数据、生成 30 天历史、自动开浏览器",
               "继续启动.bat        ← 保留上次的数据接着看"])
    rich(doc, [("要一个干净的开场用 ", False, None), ("启动演示.bat", True, BLUE),
               ("；只是想接着看上次的东西用 ", False, None),
               ("继续启动.bat", True, BLUE), ("。", False, None)], after=8)

    callout(doc, "！ 那个黑窗口不要关",
            [("启动后会留着一个黑色命令行窗口。", False, None),
             ("演示期间它必须一直开着——关掉它，或者点它右上角的 ×，演示立刻就停了。",
              True, RED),
             ("演示结束再回到那个窗口按 Ctrl+C。", False, None)])

    h2(doc, "1.2 两个地址")
    table(doc,
          ["看哪一端", "地址", "是什么"],
          [["车载终端", "http://127.0.0.1:8701", "装在台车上的那块屏，操作都在这里"],
           ["云平台", "http://127.0.0.1:8700", "项目部/公司看的数据中心，只读"]],
          [2.6, 5.6, 7.2], center_cols=[0])
    para(doc, "", after=4)
    callout(doc, "地址一定写 127.0.0.1，不要写 localhost",
            [("这台机器的 localhost 会先解析到 IPv6 的 ::1，偶尔卡半秒才通。"
              "写字面量 127.0.0.1 最稳。", False, None)],
            fill="EAF3FB", bar=BLUE)

    h2(doc, "1.3 六个页面")
    para(doc, "两端各三个页面。先认一下门牌，后面每一节都会指名道姓。", after=8)

    table(doc,
          ["端", "页面", "里面有什么"],
          [["终端\n8701", "实时监控",
            "实时参数、实时曲线、作业桩号、喷浆循环、\n采集记录、上传记录、模拟断网/立即上传按钮"],
           ["", "U 盘与修订", "① 导出 ② 校验比对 ③ 导入为修订 ④ 修订队列"],
           ["", "设置与诊断",
            "设备与链路、故障注入（演示用）、桩号清单、\n参数与阈值（只读）、运行日志"],
           ["平台\n8700", "运行总览",
            "设备在线/采集/喷浆/报警四个 KPI、实时曲线、\n设备状态、报警记录、喷浆循环"],
           ["", "历史数据",
            "区间记录数/方量/报警/被修正数、历史曲线、\n原始记录表、导出 CSV"],
           ["", "设备与传输",
            "设备/接收批次/终端欠传/被拒收、设备清单、\n上传批次、拒收明细、操作留痕（审计事件）"]],
          [1.5, 2.4, 11.5], center_cols=[0])
    para(doc, "", after=6)

    callout(doc, "点了按钮没反应？先看页面顶栏",
            [("两个页面顶部都会出现一条红字横幅，写着「服务没有响应 —— 那个黑窗口"
              "是不是被关了？」。", False, None),
             ("看到它就重新双击「启动演示.bat」，等它打出「演示环境已就绪」，再刷新本页。",
              True, None)],
            fill="FDECEA", bar=RED)


# ------------------------------------------------------------------ 功能 1
def f01(doc):
    h2(doc, "功能 1　网络自适应接入")
    plan_quote(doc, "设备开机后自动检测现场网络环境，选择信号质量更好的链路接入，无需人工干预："
                    "自动识别并选择 4G 或者 WiFi；自动建立本地设备与远程服务器的连接；"
                    "连接状态与信号强度在终端界面实时显示。")

    side(doc, "终端端（车载屏 127.0.0.1:8701）")
    st(doc, 1, "打开 127.0.0.1:8701，停在「实时监控」页。")
    st(doc, 2, "看顶栏中间那个带圆点的徽标，这就是链路指示：文字是「在线（探测 N 次成功）」"
               "表示链路已建立；「弱网」是黄点（连续两次探测失败）；「离线」是灰点"
               "（连续五次失败，或者手动断网）。")
    st(doc, 3, "看它自动降级、自动回升：点右下「演示操作」区里的【模拟断网】。"
               "圆点立刻转灰，文字变「模拟断网中」。再点同一个按钮（此时它的文字已经变成"
               "【恢复联网】），几秒内徽标自己回到「在线」——不用刷新页面，也不用重启。")
    st(doc, 4, "要看细节，进「设置与诊断」页 →「设备与链路」卡片，逐行有：设备编号、"
               "平台地址、数据源、心跳（成功/失败次数）、链路（探测成功次数）、"
               "平台回告清单版本。")

    side(doc, "平台端（127.0.0.1:8700）")
    st(doc, 1, "打开「运行总览」页，最上面一排四个 KPI 的第一个就是「设备在线」。"
               "终端断网时它变 0，恢复后回到 1。")
    st(doc, 2, "要看是哪个设备、链路什么样，到「设备与传输」页 →「设备清单」表："
               "设备 / 状态 / 最后心跳 / 链路 / 欠传 / 工点 / 说明。")
    st(doc, 3, "注意平台的判定口径：设备在不在线看的是**心跳新鲜度**，不是终端自报的状态字段"
               "——终端瞒不了平台。")

    passing(doc, [
        "终端的链路徽标与平台「设备清单」里的状态一致；",
        "断网到恢复全程不用重启任何东西，恢复后 1~2 个心跳周期内自动转回在线。",
    ])

    callout(doc, "说明：原型与计划书的差别",
            [("原型只有一条链路（本机回环），没有真的 4G/WiFi 双链路切换，也没有信号强度条。"
              "「自动选路」在这里体现为一条链路状态机：在线 / 弱网 / 离线三态，"
              "连续失败自动降级、恢复自动回升。", False, None),
             ("接真车时把探测目标换成两个网卡即可，状态机与界面不用改。", True, None)],
            fill="FFFFFF", bar=GRAY)


# ------------------------------------------------------------------ 功能 2
def f02(doc):
    h2(doc, "功能 2　工点与设备身份管理")
    plan_quote(doc, "系统支持现场录入工点 ID 及设备编号。工点 ID 与设备编号共同构成数据的归属标识，"
                    "数据上传至平台后自动归入对应标段、工区与设备档案，避免多台设备、"
                    "多个工点的数据混同。")

    side(doc, "终端端（车载屏 127.0.0.1:8701）")
    st(doc, 1, "进「设置与诊断」页，**最上面那张卡片**就是「工点与设备（现场录入）」。"
               "它有一个「工点 ID」输入框和一个「设备编号」输入框，下面两个按钮："
               "「1. 请平台核对」「2. 保存并生效」。这正是计划书说的现场录入 —— "
               "在车载屏上直接打字，不用去开记事本改配置文件。")
    st(doc, 2, "在「工点 ID」里填 DQTYX-ZK、「设备编号」里填 GHP9015H-02，"
               "点「1. 请平台核对」。**核对必须联网**：工点能不能用由平台说了算，"
               "本地猜一个等于没有核对。")
    st(doc, 3, "核对结果直接显示在卡片里：工点名、线别、桩号个数与里程区间，"
               "设备那一行会写明「平台已有档案，原登记在××」还是"
               "「平台此前没有这个编号的档案，接入后将按自报信息登记」。"
               "有转场会单独提示一行。")
    st(doc, 4, "确认无误再点「2. 保存并生效」。**中途改过任何一个输入框，"
               "这个按钮会立刻变灰**，必须重新核对 —— 不留「核对了 A、保存了 B」的空子。")
    st(doc, 5, "生效后回「实时监控」页看：顶栏工点变了，「作业桩号」整列换成了新工点的"
               "里程段，当前桩号自动落到新清单的第一个。")

    side(doc, "平台端（127.0.0.1:8700）")
    st(doc, 1, "「设备与传输」页 →「设备清单」表：新编号作为一条新档案出现，"
               "「工点」列是刚录入的工点，「说明」列写着「终端自报，未核实」——"
               "平台没有把它当成已核实的信息。")
    st(doc, 2, "「审计」页最上面一条是 identity_change，点开能看到"
               "原工点 / 原编号 / 原桩号 → 新工点 / 新编号 / 新桩号，"
               "以及来源标注「终端界面录入」。改了什么、什么时候改的，都在这里。")
    st(doc, 3, "「运行总览」页右侧「设备状态」卡片里新旧两台车都在。"
               "老档案不会被删掉 —— 它名下那些历史数据还得靠这个编号查。")

    passing(doc, [
        "全程没打开过任何配置文件，身份是在车载屏上填出来、由平台核对后生效的；",
        "生效后终端顶栏工点、桩号清单里程段、当前桩号三者同步换成新工点；",
        "平台「设备清单」出现新编号，「审计」页出现一条 identity_change 留痕。",
    ])

    side(doc, "两个必须试的反例", color=RED, before=8)
    bl(doc, "**工点不能自己编**：把工点 ID 改成 DQTYX-XX（平台没有这个工点）再点核对，"
            "会当场被 409 拒掉，并把平台上现有的工点列出来给你挑。"
            "工点决定数据归属和桩号范围，是个「档案」，终端只能引用不能编造 ——"
            "和桩号是同一个道理。", lead="✗ ")
    bl(doc, "**换编号前必须先结清欠账**：先点「立即上传」旁边的断网，"
            "攒几条没传上去的数据，再回来点「2. 保存并生效」，会被拦住，"
            "提示还有几条欠账没传完。原因是每条数据的 ID 里都嵌着设备编号，"
            "换了编号再传，平台按设备不匹配会整批拒收。把欠账传完再改就行。", lead="✗ ")

    callout(doc, "说明：原型与计划书的差别",
            [("**这一条已经是真的了**：录入在车载屏上完成，判定权在平台，"
              "设备编号改动留痕可查。", False, None),
             ("原型里设备编号是**允许改**的（要过核对 + 留痕），"
              "而不是锁死只读。真车出厂后编号印在铭牌上、司机不该随便动，"
              "这一点按现场管理要求收紧即可 —— 把界面上的输入框改成只读，"
              "平台侧一个字都不用改。", True, None),
             ("平台侧目前没有做「工点 / 设备台账管理」的界面：工点档案来自"
              "config\\site.json（改完重启平台即生效），设备档案由终端自报或心跳自动建立。"
              "要给人用的台账管理页，是界面上加一张表的事。", True, GRAY)],
            fill="FFFFFF", bar=GRAY)


# ------------------------------------------------------------------ 功能 3
def f03(doc):
    h2(doc, "功能 3　施工桩号自动获取")
    plan_quote(doc, "现场不提供桩号的手工输入入口；操作人员根据当前施工里程值，"
                    "在终端屏幕上点击对应位置；桩号由系统平台按里程自动匹配后下发，"
                    "现场无法人为录入或修改。")

    side(doc, "终端端（车载屏 127.0.0.1:8701）")
    st(doc, 1, "「实时监控」页右侧「作业桩号」卡片。这是一列**可点的桩号**"
               "（形如 ZK105+207，下面小字是 105.207 km），不是输入框。")
    st(doc, 2, "点其中一行，该行立刻高亮成当前桩号，顶栏的「桩号」徽标同步变化。")
    st(doc, 3, "然后把整页找一遍：没有任何可以打字输桩号的地方。"
               "卡片下面写着「桩号只能从平台下发的清单里点选」——这是设计出来的，不是漏做了。")
    st(doc, 4, "「设置与诊断」页右下「桩号清单」卡片是同一份清单（点选效果一样），"
               "右上角显示清单版本号。")

    side(doc, "平台端（127.0.0.1:8700）")
    st(doc, 1, "清单由平台生成并下发。终端启动时来取一次，之后按版本号判断要不要刷新；"
               "断网期间用本地缓存，照样能点。")
    st(doc, 2, "越权桩号的最后一道拦截在平台：「设备与传输」页 →「拒收明细」表。"
               "记录里的桩号不在平台下发清单内 → 整条拒收，原因码 PILE_NOT_ISSUED"
               "（桩号未由平台下发）。")
    st(doc, 3, "正常情况下这张表是空的——那是好事。要亲眼看见拦截确实在工作，"
               "在命令行跑这条用例：")
    code(doc, ["cd wsc-system",
               "python -m unittest tests.test_upload.TestPileGuard -v"])
    st(doc, 4, "归档口径：「运行总览」的报警记录表、喷浆循环表，以及「历史数据」的"
               "原始记录表，桩号列都取自这条通道。")

    passing(doc, [
        "终端只能选出清单内的桩号，造不出清单外的；",
        "平台拒收明细出现 PILE_NOT_ISSUED，说明最后一层拦截真的在工作。",
    ])

    callout(doc, "清单是怎么来的",
            [("演示环境里，平台启动时按里程每 4 米一个自动生成。默认工点 DQTYX-ZK 是"
              "104 907 ~ 105 707，共 201 个（ZK104+907 … ZK105+707）。"
              "另外还配了一个工点 SML-ZK（106 000 ~ 106 800，同样是 201 个），"
              "专门留着演示功能 2 的「转场」—— 换工点时整列桩号会一次性换掉。", False, None),
             ("真实项目中这份清单来自项目部的里程台账，由平台下发；终端无论如何改不了里程。",
              True, None)],
            fill="FFFFFF", bar=GRAY)


# ------------------------------------------------------------------ 功能 4
def f04(doc):
    h2(doc, "功能 4　CAN 总线实时数据采集")
    plan_quote(doc, "作业过程中，系统通过 CAN 总线接收设备的实时作业数据。"
                    "采集对象为设备控制系统本身输出的运行参数，"
                    "不额外加装影响设备结构的传感器，采集过程对设备原有操作无干扰。")

    side(doc, "终端端（车载屏 127.0.0.1:8701）")
    st(doc, 1, "「实时监控」页左上「监测实时数据」卡片就是全部参数：大臂回转速度、大臂角度、"
               "大臂伸缩、小臂伸缩、泵送压力、泵送风路、速凝剂泵状态、速凝剂流量、液压油温、"
               "液压系统压力、电机启动、电机电流、风机状态、GPS 经纬度，另有累计泵送方量"
               "（喷浆循环的方量就是它的差值）。标题右侧写着「运行 … · 采样 2 秒/帧」。")
    st(doc, 2, "点左边任意参数名，中间「实时曲线」就切到该参数。图例：蓝色实测值、"
               "红色上限、橙色下限。")
    st(doc, 3, "「设置与诊断」页 →「参数与阈值（只读）」表：参数 / 键名 / 正常范围 / 单位。"
               "这张表就是采集、CSV 表头、平台判定共用的那一份注册表，界面上不另写一套，"
               "也不提供改阈值——现场把报警线改没了，比不方便严重得多。")

    side(doc, "平台端（127.0.0.1:8700）")
    st(doc, 1, "「运行总览」→「实时曲线」：时间窗可选最近 10 分钟 / 30 分钟 / 2 小时 / 24 小时，"
               "图例上可以点选要看的参数。")
    st(doc, 2, "「历史数据」→「历史曲线」：参数 + 粒度（按小时 / 按天）+ 范围"
               "（最近 1 / 3 / 7 / 30 / 90 天）。柱是每桶均值，上下须线是区间最大/最小值，"
               "灰底柱表示该时段有报警。")

    passing(doc, [
        "同一时刻，终端与平台显示的同一参数值一致；",
        "采样间隔在两端都标着 2 秒/帧。",
    ])

    callout(doc, "换真车数据源（演示用 sim，真车用 real）",
            [("打开 config\\config.json，把 can.source 从 \"sim\" 改成 \"real\"，"
              "再填 bustype / channel / bitrate 和 signals 信号表"
              "（对应 can_bus\\python_can_source.py，python-can 库惰性导入）。"
              "采集、上传、界面代码一行都不用改。", False, None),
             ("要注意的是：signals 信号表目前是空的，而且没有在真车上测过——"
              "这是上真车之前的第一件事。", True, RED)],
            fill="FDECEA", bar=RED)


# ------------------------------------------------------------------ 功能 5
def f05(doc):
    h2(doc, "功能 5　数据上传与上传结果回执")
    plan_quote(doc, "系统对 CAN 采集的原始数据进行解析、清洗，并按时间序列组织，"
                    "在有网络状态下实时上传至远程服务器，每次上传自动记录上传结果。"
                    "上传不是「发出即认为成功」，而是有明确的结果记录。")

    side(doc, "终端端（车载屏 127.0.0.1:8701）")
    st(doc, 1, "「实时监控」页右下「上传记录」表：时间 / 类型（数据、修订、审计）/ 批次 / 条数 / "
               "结果 / 耗时 / 说明。**结果这一列就是回执**：已确认、部分接收、无回执、网络失败。")
    st(doc, 2, "顶栏「最近上传」徽标显示最近一批的条数与结果；旁边「未上传 N 条」是当前欠传。")
    st(doc, 3, "想马上看一次：点「演示操作」区的【立即上传】。它只写一个「插队」的意图，"
               "真正的请求由上传线程去发，界面不会卡。")

    side(doc, "平台端（127.0.0.1:8700）")
    st(doc, 1, "「设备与传输」页 →「上传批次」表：平台收妥时间 / 批次号 / 类型 / 条数 / 接受 / "
               "重复 / 拒收。批次号是内容寻址的（整批 record_id 排序后取 sha1 前 16 位），"
               "同一批数据重发时批次号不变。")
    st(doc, 2, "要验证「重复上送不会重复入库」，演示跑着的时候另开一个命令行，执行：")
    code(doc, ["cd wsc-system",
               "python tools\\prove_idempotent.py"])
    st(doc, 3, "这个脚本会用同一批数据连传两次，再传一次被改过数值的，三个回合："
               "① 正常上传 → 接受 N 条；② 原样重传 → 标「重复」，平台帧总数一个都没变；"
               "③ 改一个数值再传 → 整批拒收（DUPLICATE_CONFLICT），原值不被覆盖。")
    st(doc, 4, "「运行总览」→「近 24 小时采集」KPI 是平台入库的帧数。")

    passing(doc, [
        "终端显示某批「已确认」时，平台「上传批次」里就有对应的一条，接受数等于条数；",
        "两端的欠传数最终都归零。",
    ])


# ------------------------------------------------------------------ 功能 6
def f06(doc):
    h2(doc, "功能 6　断网本地缓存")
    plan_quote(doc, "在无网络状态下，系统将数据以 .CSV 格式保存至本地，并标记为未上传状态。"
                    "本地数据不因断电、断网而丢失。")

    side(doc, "终端端（车载屏 127.0.0.1:8701）")
    st(doc, 1, "「实时监控」页 →「演示操作」区，点【模拟断网】。按钮文字变成【恢复联网】，"
               "顶栏徽标变成「模拟断网中」。")
    st(doc, 2, "然后跟着看四处：")
    bl(doc, "中间「实时曲线」照走——曲线读的是本地库里的原始记录，和上传是两条路；",
       lead="　a) ")
    bl(doc, "顶栏「已采 N 帧」继续涨；", lead="　b) ")
    bl(doc, "顶栏「未上传 N 条」开始持续增加；", lead="　c) ")
    bl(doc, "下方「采集记录（本地库）」表里，新出现的行状态标签是**未上传**。", lead="　d) ")
    st(doc, 3, "磁盘上的证据：wsc-system\\data\\terminal\\frames\\ 下按小时轮转的 .csv 文件。"
               "用 Excel 直接打开不乱码（utf-8-sig 带 BOM），列名是 ASCII 的，"
               "中文名在平台侧映射——这样 Excel 不会锁文件，也不会有编码问题。")

    side(doc, "平台端（127.0.0.1:8700）")
    st(doc, 1, "「运行总览」→「设备在线」变 0，「设备状态」卡片里该设备转为离线。")
    st(doc, 2, "实时曲线不再推进——平台确实收不到新数据。")

    passing(doc, [
        "断网期间终端照采照存，平台确实没有新数据；",
        "终端「未上传」条数持续增长。",
    ])


# ------------------------------------------------------------------ 功能 7
def f07(doc):
    h2(doc, "功能 7　通信中断点续传")
    plan_quote(doc, "作业过程中如通信不稳定，硬件会自动暂停数据上传；待通讯恢复后自动重新上传，"
                    "并将已上传与未上传的数据明确区分，避免重复上传或漏传。")

    side(doc, "终端端（车载屏 127.0.0.1:8701）")
    st(doc, 1, "接着上一条，让断网持续一会儿（30 秒以上，攒出看得见的欠传量）。")
    st(doc, 2, "点【恢复联网】（就是刚才那个按钮）。")
    st(doc, 3, "跟着看三处：")
    bl(doc, "顶栏徽标几秒内回到「在线」；", lead="　a) ")
    bl(doc, "「未上传 N 条」开始往下掉，一直掉到 0；", lead="　b) ")
    bl(doc, "「上传记录」表里连续出现多批「已确认」。", lead="　c) ")
    st(doc, 4, "注意补传的**顺序**：欠传期间按时间顺序先补旧的，补完才恢复实时——"
               "所以平台曲线不会出现时间倒挂。")

    side(doc, "平台端（127.0.0.1:8700）")
    st(doc, 1, "「设备与传输」页 →「终端欠传」KPI 降到 0。")
    st(doc, 2, "「上传批次」表里是刚补传的这一串批次：「接受」等于条数，「重复」为 0。")
    st(doc, 3, "「运行总览」的实时曲线：断网那段是空洞，恢复后空洞被按**原时间戳**填上，"
               "不是堆在当前时刻。")

    passing(doc, [
        "一条不丢：欠传归零；一条不重：「重复」列为 0，平台行数等于应有条数；",
        "曲线时间轴连续，没有倒挂。",
    ])

    callout(doc, "这两件事各由一层机制兜底",
            [("不重：record_id 是主键，批次号是内容寻址的，重放只会拿到上一份回执；", False, None),
             ("不丢：先落盘再上传，回执到了才改状态——所以断电、强杀进程都不会丢，"
              "重启后自动续传。", False, None)],
            fill="F1F8F2", bar=GREEN)


# ------------------------------------------------------------------ 功能 8
def f08(doc):
    h2(doc, "功能 8　数据筛选与手动上传")
    plan_quote(doc, "系统支持在用户界面对数据按条件进行筛选，并对筛选结果手动上传，"
                    "便于现场人员在网络条件良好时（如设备驶出隧道后）主动补传。")

    side(doc, "终端端（车载屏 127.0.0.1:8701）")
    st(doc, 1, "筛选入口在「U 盘与修订」页 →「① 导出到 U 盘」卡片上，两个下拉：")
    bl(doc, "「范围」：待上传的 / 今天的 / 全部；", lead="　")
    bl(doc, "「条数」：500 / 2000 / 10000。", lead="　")
    st(doc, 2, "手动上传在「实时监控」页 →「演示操作」区，点【立即上传】："
               "把当前所有欠传记录打上「手动」标记插队，下一个上传周期就发出去，"
               "不用等退避时间。")
    st(doc, 3, "结果看「上传记录」表；欠传看顶栏「未上传 N 条」。")

    side(doc, "平台端（127.0.0.1:8700）")
    st(doc, 1, "「设备与传输」页 →「上传批次」表，按时间倒序能看到刚补上来的批次。")
    st(doc, 2, "同一页的「操作留痕（审计事件）」表里会出现一条 manual_upload。"
               "手动补传这个**动作**本身也会留痕，即使数据本体还没传上来。")

    passing(doc, [
        "点【立即上传】后一个上传周期内欠传数明显下降；",
        "平台批次表与操作留痕表同时出现对应记录。",
    ])

    callout(doc, "说明：筛选维度只做到一层",
            [("原型的手动上传是「整批欠传插队」，没做多条件组合筛选"
              "（按桩号、时间段、参数区间）。U 盘页那两个下拉是当前版本唯一的筛选维度。",
              False, None),
             ("生产环境要按现场习惯补这一层。", True, None)],
            fill="FFFFFF", bar=GRAY)


# ------------------------------------------------------------------ 功能 9
def f09(doc):
    h2(doc, "功能 9　U 盘导出、校对与重传")
    plan_quote(doc, "作业过程中如遇到个别传感器损坏或者数据失真，可通过 U 盘将作业文件导出；"
                    "由技术人员对数据进行修正、优化；再通过 U 盘将作业文件导入终端，"
                    "重新进行数据上传。不会因为个别传感器故障就让整段施工记录作废；"
                    "同时该操作需要人工介入并留痕，便于事后核查。")

    para(doc, "这一条在终端「U 盘与修订」页上，页面本身就是按四步排的。", after=8)

    side(doc, "终端端（车载屏 127.0.0.1:8701 → U 盘与修订）")
    st(doc, 1, "① 导出到 U 盘：选范围和条数 →【导出到 U 盘】。会在 U 盘目录下写三个文件："
               "frames.csv（数据）、manifest.json（清单，含 sha256 与时间范围）、"
               "说明.txt（给现场的人看的）。「U 盘上的包」表列出已导出的包，"
               "点一行选中它——下面两步都针对选中的这个包。")
    st(doc, 2, "② 校验与比对：【校验并比对（不写库）】只读地核对 sha256 与时间范围。"
               "想看「数据被改过会怎样」，选一个参数（泵送压力 / 速凝剂流量 / 液压油温 / "
               "泵送风路压力），点【模拟在别的电脑上改 2 行】——真机上这一步是人拿 Excel 干的；"
               "它**故意不改 row_hash**，所以「内容变了」和「摘要对不上」两处证据都能看到。")
    st(doc, 3, "③ 导入为修订：填「操作人」（必填）、选「原因」"
               "（传感器故障 / 数据失真 / 桩号更正 / 其他）、可填「备注」→【导入为修订】。"
               "导入**不覆盖原值**：平台收到的是「某条记录、某个参数、从多少改成多少、"
               "谁改的、为什么」。有一行不合法就整包不动——宁可少改，不要错改。")
    st(doc, 4, "④ 修订队列：看生成时间 / 操作人 / 原因 / 改动 / 状态。修订批要等欠传的数据"
               "先传完再发——目标记录还没上平台的话，平台会整批拒收。")

    side(doc, "平台端（127.0.0.1:8700）")
    st(doc, 1, "「历史数据」页 →「原始记录」表：改过的行带「已修正」标签；表头右侧写着"
               "「最近 N 条 · 其中 M 条被修正过」。")
    st(doc, 2, "点开该行详情：抽屉里有「已修正」标签、来源、数据质量、修订次数，"
               "以及一块**「原值 / 有效值」对照表**——原值永久保留，从来没有被覆盖。")
    st(doc, 3, "「历史数据」页顶部「被修正的记录」KPI 计数。")
    st(doc, 4, "「设备与传输」页 →「操作留痕（审计事件）」：U 盘导出、导入、修订都是独立事件，"
               "带设备时间与平台收到时间。")

    passing(doc, [
        "平台出现「已修正」标记，点开能看到原值、有效值、经手人、原因；",
        "原始值没有被抹掉，导出/导入/修订三件事在操作留痕里都能查到。",
    ])

    callout(doc, "对客户的说法",
            [("不是不允许改，而是谁改的、什么时候、为什么、改成什么，平台全留痕，"
              "原值永久保存。", True, None)],
            fill="F1F8F2", bar=GREEN)


# ------------------------------------------------------------------ 功能 10
def f10(doc):
    h2(doc, "功能 10　施工现场可视化")
    plan_quote(doc, "所有施工数据及数据上传结果在施工现场均可视。操作人员在车载显示屏上即可查看"
                    "当前施工参数、历史记录与上传状态，管理人员现场即可核对，"
                    "无需返回项目部查询。")

    side(doc, "终端端（车载屏 127.0.0.1:8701）——现场那一块屏上能看到的全部")
    st(doc, 1, "「实时监控」页：实时参数（15 项）、实时曲线（点参数名切换）、作业桩号（点选）、"
               "喷浆循环（本轮进行中：桩号 / 已喷方量 / 峰值压力；下方历史循环表：桩号 / 开始 / "
               "方量 / 历时）、采集记录（本地库，每行带上传状态）、上传记录（每批结果与耗时）。")
    st(doc, 2, "顶栏一条状态带：桩号 / 工况 / 链路 / 未上传条数 / 最近上传 / 已采帧数 / 时钟。")
    st(doc, 3, "页面底部一行本地存储：几个 CSV、多少 MB、最早一天、当前文件。")
    st(doc, 4, "另外两页：U 盘与修订（四步校对重传）、设置与诊断（身份、故障注入、桩号清单、"
               "参数表、日志）。")

    side(doc, "平台端（127.0.0.1:8700）——项目部/公司看的那一端")
    st(doc, 1, "「运行总览」：设备在线 / 近 24 小时采集 / 近 24 小时喷浆 / 报警 四个 KPI；"
               "实时曲线（时间窗可切）、设备状态、报警记录（可勾「只看未恢复」）、喷浆循环。")
    st(doc, 2, "「历史数据」：区间记录数 / 区间方量 / 区间报警 / 被修正的记录；历史曲线；"
               "原始记录表，右上角可导出 CSV。")
    st(doc, 3, "「设备与传输」：设备 / 接收批次 / 终端欠传 / 被拒收 四个 KPI；"
               "设备清单、上传批次、拒收明细、操作留痕四张表。")

    passing(doc, [
        "同一时刻两端显示同一个桩号、同一批上传结果；",
        "现场人员在车载屏上就能回答「这批数据传上去没有」，不用打电话回项目部。",
    ])


# ------------------------------------------------------------------ 功能 11
def f11(doc):
    h2(doc, "功能 11　本地数据留存")
    plan_quote(doc, "本地数据自动保持 30 天。超出留存周期后按策略滚动清理，不影响终端存储空间；"
                    "云端数据长期保存，本地留存作为短期兜底手段。")

    side(doc, "终端端（车载屏 127.0.0.1:8701）")
    st(doc, 1, "「实时监控」页最下面一行：本地存储：N 个 CSV · X MB · 最早 <日期> · "
               "当前文件 <文件名>。")
    st(doc, 2, "「设置与诊断」页 →「设备与链路」里的「本地库」「最早数据」「当前文件」三行"
               "是同一件事的展开。")
    st(doc, 3, "改留存天数：config\\config.json → retention.csv_days（默认 30）；"
               "collect_interval_s 是清理线程的跑动间隔。改完重启生效。")

    side(doc, "平台端（127.0.0.1:8700）")
    st(doc, 1, "平台侧的保留天数在同一个文件里：retention.frames_days（默认 400 天），"
               "由平台启动时的清理线程按天删除过期帧。")
    st(doc, 2, "「历史数据」页「范围」最大可选最近 90 天——平台侧的数据比本地留存久得多。")

    passing(doc, [
        "本地留存天数不超过设定值；",
        "清理**永不删未上传的数据**——「未上传」的记录会一直留着，直到传上去为止；",
        "平台能查到 30 天以上的历史（启动时 --seed 生成的就是 30 天）。",
    ])

    callout(doc, "为什么「永不删 pending」值得单独说",
            [("本地留存的意义是兜底。如果清理线程顺手把没传出去的数据也删了，"
              "兜底就变成了销毁证据——所以清理只动已上传的行。", False, None)],
            fill="F1F8F2", bar=GREEN)


# ------------------------------------------------------------------ 三、速查
def sec_cheat(doc):
    pagebreak(doc)
    h1(doc, "三、一页速查")
    para(doc, "演示时忘词就看这一张。左边是功能，右边是两端各自的入口。", after=10)

    table(doc,
          ["序", "功能项", "终端端（8701）入口", "平台端（8700）入口"],
          [["1", "网络自适应接入",
            "顶栏链路徽标；\n「演示操作」→ 模拟断网/恢复联网",
            "运行总览 → 设备在线 KPI；\n设备与传输 → 设备清单"],
           ["2", "工点与设备身份管理",
            "设置与诊断 → 工点与设备（现场录入）：\n填工点 ID + 设备编号 →\n「1. 请平台核对」→「2. 保存并生效」",
            "设备与传输 → 设备清单（工点列、说明列）；\n审计 → identity_change 留痕"],
           ["3", "施工桩号自动获取",
            "实时监控 → 作业桩号（只能点，不能输）；\n设置与诊断 → 桩号清单",
            "设备与传输 → 拒收明细\n（PILE_NOT_ISSUED）"],
           ["4", "CAN 总线实时数据采集",
            "实时监控 → 监测实时数据 / 实时曲线；\n设置与诊断 → 参数与阈值",
            "运行总览 → 实时曲线；\n历史数据 → 历史曲线"],
           ["5", "数据上传与上传结果回执",
            "实时监控 → 上传记录（结果列）；\n顶栏「最近上传」徽标",
            "设备与传输 → 上传批次；\n命令行 tools\\prove_idempotent.py"],
           ["6", "断网本地缓存",
            "实时监控 → 模拟断网；\n采集记录（状态列：未上传）",
            "运行总览 → 设备在线转 0、设备状态转离线"],
           ["7", "通信中断点续传",
            "点恢复联网 → 看未上传掉到 0；\n上传记录连续已确认",
            "设备与传输 → 终端欠传归零、\n上传批次接受=条数"],
           ["8", "数据筛选与手动上传",
            "U 盘与修订 → ① 范围/条数下拉；\n实时监控 → 立即上传",
            "设备与传输 → 上传批次、\n操作留痕（manual_upload）"],
           ["9", "U 盘导出、校对与重传",
            "U 盘与修订 → ①导出 ②校验比对\n③导入为修订 ④修订队列",
            "历史数据 → 原始记录「已修正」、\n记录详情原值/有效值"],
           ["10", "施工现场可视化",
            "实时监控整页 + 顶栏状态带\n+ 底部本地存储",
            "运行总览 / 历史数据 / 设备与传输\n三页十二张表"],
           ["11", "本地数据留存",
            "实时监控底部；设置与诊断 → 本地库；\nconfig.json → retention.csv_days",
            "config.json → retention.frames_days；\n历史数据 → 范围最多 90 天"]],
          [0.8, 3.0, 5.8, 5.8], fs=9, center_cols=[0])


# ------------------------------------------------------------------ 四、动线
def sec_flow(doc):
    h1(doc, "四、把 11 条串成一条演示动线")
    para(doc, "按这个顺序走一遍，11 条功能每条都会自然出现一次，"
              "不用来回切页面找。全程约 8 分钟。", after=10)

    st(doc, 1, "双击「启动演示.bat」，等窗口打出「演示环境已就绪」，两个页面自动打开。"
               "（这是功能 2、3 的起点：平台按工点生成并下发桩号清单。）")
    st(doc, 2, "终端停在「实时监控」页看半分钟：参数在跳、曲线在走、喷浆循环在进行中。"
               "——功能 4、10。")
    st(doc, 3, "点一个桩号，顶栏「桩号」跟着变。（功能 3）")
    st(doc, 4, "点【模拟断网】，等 30 秒：看「未上传」往上涨、曲线照走、采集记录标「未上传」；"
               "切到平台看设备转离线、曲线停住。——功能 1、6。")
    st(doc, 5, "点【恢复联网】：看「未上传」掉到 0、上传记录连续「已确认」；"
               "到平台「上传批次」核对接受数与重复数，看曲线空洞被按原时间戳填上。"
               "——功能 5、7。")
    st(doc, 6, "另开一个命令行，跑 python tools\\prove_idempotent.py：三个回合证明不重、"
               "篡改会被整批拒收。（功能 5）")
    st(doc, 7, "终端「U 盘与修订」页走四步：导出 → 模拟改 2 行 → 校验比对 → 导入为修订；"
               "回平台「历史数据」看「已修正」标记和原值/有效值对照。——功能 9。")
    st(doc, 8, "平台「设备与传输」页把四张表过一遍：设备清单、上传批次、拒收明细、操作留痕。"
               "——功能 5、7、8、9 的落点。")
    st(doc, 9, "收尾前回终端看一眼底部那行本地存储（几个 CSV、多少 MB、最早一天），"
               "对应功能 11；然后回黑窗口按 Ctrl+C 停掉演示。")

    para(doc, "", after=10)
    callout(doc, "想再深入一层",
            [("系统的架构、数据不丢不重的具体做法、桩号防伪的三层设计、参数表、"
              "以及已知限制，都在项目目录下的 README.md。", False, None),
             ("还有一份《演示使用说明》，讲的是怎么打开、页面上有什么、出问题怎么办；"
              "本文讲的是 11 项功能各自在两端怎么操作。两份配合看。", False, None)],
            fill="EAF3FB", bar=BLUE)


def main():
    doc = Document()
    setup_styles(doc)
    setup_page(doc)

    cover(doc)
    sec_start(doc)

    h1(doc, "二、11 项功能逐条操作")
    para(doc, "本章按《产品计划书》3.2 ~ 3.12 的顺序，一条一节。"
              "每一节先说计划书原话，再分「终端端」和「平台端」给步骤，"
              "最后是「通过标准」——演示时照着念、照着点即可。", after=8)

    for fn in (f01, f02, f03, f04, f05, f06, f07, f08, f09, f10, f11):
        fn(doc)

    sec_cheat(doc)
    sec_flow(doc)

    # docx 只是转 PDF 的中间产物，写在临时目录里 —— 本目录只留 PDF。
    tmpdir = tempfile.mkdtemp(prefix="wsc_manual11_")
    docx_path = os.path.join(tmpdir, "manual11.docx")
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
