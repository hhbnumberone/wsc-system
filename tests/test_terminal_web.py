# -*- coding: utf-8 -*-
"""终端界面的路由与资源契约 —— 全部从**真实 HTTP** 打进去。

为什么专门有这个文件：平台侧出过一次 `/api/params` 500，原因是 `Api.__init__`
里 `self.params = ...` 把同名的处理方法盖掉了。当时**单测全绿**，因为所有用例
都是直接调 `api.xxx(...)`，没有一条从路由表走。终端这边 `webui.Handler._dispatch`
是手写 if 链，同样的坑一样存在，所以这里的起步就是起一个真的 `ThreadingHTTPServer`
（端口 0 让系统分配），再用 urllib 打。

四条契约：

1. 路由表里的每个 GET 接口都返回 200 且是 JSON；参数不对返回 400 而不是 500；
2. `app.js` 里出现的每个 `/api/` 路径都在路由表里（反向覆盖，防"界面调了个不存在的接口"）；
3. 三张页面引用的静态资源存在、MIME 正确；
4. 页面上用到的每个 id 都在该页 HTML 里声明，且该页声明了 JS 为它准备的所有 id
   —— 后一条是抓"usb.html 少写一个 div"这类错的主要手段。

不测的：浏览器里的渲染效果（没有浏览器），真实 CAN 收发。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request

from common import config, csv_schema, ids
from common.enums import Quality
from common.params import PARAM_KEYS
from common.timeutil import now_epoch, now_utc_iso
from terminal import webui
from terminal.app import TerminalApp
from terminal.store import LocalStore

WEB_DIR = config.TERMINAL_WEB
DEVICE = "GHP9015H"

# 三张页面，以及各自"专属"的那个 init 函数（app.js 里一个文件服务全部）
PAGES = {"index": "index.html", "records": "records.html", "upload": "upload.html",
         "usb": "usb.html", "revision": "revision.html", "settings": "settings.html"}
PAGE_INIT = {"index": "initIndex", "records": "initRecords", "upload": "initUpload",
             "usb": "initUsb", "revision": "initRevision", "settings": "initSettings"}
# 顶栏/导航由 JS 挂载，这几个函数每个页面都要用到
SHARED_FUNCS = ("mountChrome", "renderTop", "tickTop")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fp:
        return fp.read()


def _func_body(js: str, name: str) -> str:
    """从 app.js 里切出一个顶层函数的函数体。

    只按顶格（第 0 列）的 `function name() {` 找起点、`\\n}\\n` 找终点 ——
    所以 app.js 里这几个函数必须写成顶层函数，不能缩进、不能改成箭头函数。
    这个约束是刻意的：换个写法这里会直接报"找不到"，而不是悄悄少查一段。
    """
    m = re.search(r"^function " + name + r"\([^)]*\) \{", js, re.M)
    if m is None:
        raise AssertionError(f"app.js 里找不到顶层函数 {name}()")
    end = js.index("\n}\n", m.end())
    return js[m.end():end]


def _ids_required(js: str) -> set[str]:
    """**必须**存在的 id：`$('x')` / `getElementById('x')` 拿到 null 就会当场炸。"""
    out = set(re.findall(r"\$\('([^']+)'\)", js))
    out |= set(re.findall(r"getElementById\('([^']+)'\)", js))
    return out


def _ids_optional(js: str) -> set[str]:
    """可选 id：`setText('x', ...)` 在元素不存在时什么都不做。

    这就是 setText 存在的全部理由 —— 顶栏里的"运行 x 小时 · 采样 2 秒/帧"
    只属于首页的参数卡片，U 盘页没有它也不该整页崩掉。
    """
    return set(re.findall(r"setText\('([^']+)'", js))


def _ids_declared(html: str) -> set[str]:
    return set(re.findall(r'id="([^"]+)"', html))


def _values(**over) -> dict:
    """一帧的量程内取值。`store.append` 的 strict 校验会挡下越界的值，
    所以这里不能用"随便填个数"的写法。"""
    base = {
        "workStatus": 1.0, "cylinderPumpingPressure": 11.0,
        "pumpingSpeed": 10.8, "circulatingPumpingVolume": 3.2,
        "acceleratorAddSpeed": 620.0, "acceleratorProportion": 5.7,
        "acceleratorOnceDosage": 20.0, "acceleratorCumulativeDosage": 4800.0,
    }
    base.update(over)
    return {k: float(base[k]) for k in PARAM_KEYS}


# ==========================================================================
# 起一台真的终端，从 HTTP 打
# ==========================================================================
class TerminalWebTestBase(unittest.TestCase):
    """一台终端（临时库 + 临时 U 盘），界面跑在系统分配的端口上。

    监听端口用 0：演示时 8701 有真终端在跑，测试抢端口会变成"时好时坏的失败"。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="wsc-web-")
        # LOCAL_DB / FRAMES_DIR / USB_DIR 是模块级常量，LocalStore 和 usb_sync
        # 都是调用时才读 —— 所以临时改掉再建 app 就够，不用动配置文件。
        cls._saved = {k: getattr(config, k) for k in
                      ("LOCAL_DB", "FRAMES_DIR", "USB_DIR", "LOG_DIR")}
        config.LOCAL_DB = os.path.join(cls.tmp, "local.db")
        config.FRAMES_DIR = os.path.join(cls.tmp, "frames")
        config.USB_DIR = os.path.join(cls.tmp, "usb")
        config.LOG_DIR = os.path.join(cls.tmp, "logs")
        os.makedirs(config.FRAMES_DIR, exist_ok=True)
        os.makedirs(config.USB_DIR, exist_ok=True)
        os.makedirs(config.LOG_DIR, exist_ok=True)
        # 自己造一份日志。不这么做，这条冒烟用例就隐含依赖"这台机器上跑过一次
        # 终端" —— 一份全新解压、还没启动过的副本上，/api/logs 会老老实实返回
        # 400「没有这个日志」，于是自检红得莫名其妙，而代码其实是好的。
        with open(os.path.join(config.LOG_DIR, "terminal.log"),
                  "w", encoding="utf-8") as fp:
            fp.write("2026-01-01 00:00:00 INFO  [terminal] 冒烟测试占位日志\n")

        from server import seed as platform_seed
        cls.piles = platform_seed.pile_list(config.load_config())

        # 桩号清单要**在起 app 之前**塞进本地库：PileRegistry 是在
        # TerminalApp.__init__ 里读缓存的，之后再写库它不会知道。
        with LocalStore() as warm:
            warm.replace_piles([
                {"pile_id": p, "site_id": "DQTYX-ZK", "line": "右线",
                 "mileage_m": 105207 + i * 20, "label": p, "issued_at": now_utc_iso()}
                for i, p in enumerate(cls.piles)])

        cfg = config.load_config()
        cfg["device"]["device_code"] = "GHP9015H"   # 测试造帧固定此编号：与现场配置隔离
        cfg["terminal"]["port"] = 0
        cfg["terminal"]["host"] = "127.0.0.1"
        cls.app = TerminalApp(cfg, with_upload=False)     # 起进程内线程就没意义了
        cls.app.select_pile(cls.piles[1])
        cls._seed()
        cls.httpd = webui.serve(cls.app, cfg)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def _seed(cls) -> None:
        """让每个接口都有东西可返回 —— 空库下很多接口会走"没有数据"的
        早退分支，那条路测不出形状错误。"""
        ts = int(now_epoch() * 1000)
        for seq in range(1, 6):
            values = _values(cylinderPumpingPressure=11.0 + seq * 0.1)
            cls.app.store.append({
                "record_id": ids.frame_record_id(DEVICE, ts + seq * 2000, seq),
                "device_code": DEVICE, "ts_device": now_utc_iso(),
                "ts_epoch": ts + seq * 2000, "seq": seq,
                "pile_id": cls.piles[1], "quality": int(Quality.OK),
                "source": "can", "values": values,
                "row_hash": csv_schema.compute_row_hash(
                    {**{c: "" for c in csv_schema.META_COLUMNS}, **values}),
            })
        # 一个 U 盘导出包：inspect / import / edit 三个接口都要有包可操作
        cls.app.usb_export(scope="all")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        try:
            cls.app.store.close()
        except Exception:                     # noqa: BLE001 - 临时目录要删，尽力而为
            pass
        for k, v in cls._saved.items():
            setattr(config, k, v)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- HTTP ----
    def _request(self, method: str, path: str, body: dict | None = None):
        # 中文参数得先转义：HTTP 请求行只能是 ASCII，urllib 会直接抛
        # UnicodeEncodeError（在客户端就炸了，根本没发出去）
        url = self.base + urllib.parse.quote(path, safe="/?&=%+-")
        data = None
        headers = {"Connection": "close"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return resp.status, raw, resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers.get("Content-Type", "")

    def get_json(self, path: str) -> tuple[int, dict]:
        status, raw, ctype = self._request("GET", path)
        self.assertIn("json", ctype, f"{path} 的 Content-Type 是 {ctype}")
        return status, json.loads(raw.decode("utf-8"))

    def post_json(self, path: str, body: dict) -> tuple[int, dict]:
        status, raw, ctype = self._request("POST", path, body)
        self.assertIn("json", ctype, f"{path} 的 Content-Type 是 {ctype}")
        return status, json.loads(raw.decode("utf-8"))


# ==========================================================================
# 路由：每个接口都要真的能跑
# ==========================================================================
class RouteSmokeTests(TerminalWebTestBase):
    """路由契约。**这个类的存在本身就是一次事故的补丁**（见模块开头）。"""

    GETS = [
        "/api/status",
        "/api/records?limit=5",
        "/api/records?limit=5&status=pending",
        "/api/records?scope=today",
        "/api/records?scope=rejected&param=cylinderPumpingPressure",
        "/api/uploads?limit=5",
        "/api/uploads?kind=frame",
        "/api/batches?limit=5",
        "/api/piles",
        "/api/trend?param=cylinderPumpingPressure&limit=30",
        "/api/trend",
        "/api/params",
        "/api/faults",
        "/api/usb",
        "/api/logs?name=terminal.log&lines=20",
    ]

    # `/api/usb/inspect` 和 `/api/pile` 故意不放进来：它们要参数，
    # 没参数的 400 是**对的**（分别见 test_export_then_inspect_round_trip
    # 和 test_bad_pile_is_refused）。列在这里只会让这条测试变成假的。
    POSTS = [
        ("/api/link", {"offline": True}),
        ("/api/link", {"offline": False}),
        ("/api/upload-now", {}),
        ("/api/fault", {"key": "cylinderPumpingPressure"}),
        ("/api/fault/clear", {}),
    ]

    def test_every_get_route_returns_json(self):
        for path in self.GETS:
            with self.subTest(path=path):
                status, body = self.get_json(path)
                self.assertEqual(status, 200, f"{path} 返回 {status}：{body}")
                self.assertIsInstance(body, dict)

    def test_every_post_route_returns_json(self):
        for path, body in self.POSTS:
            with self.subTest(path=path):
                status, out = self.post_json(path, body)
                self.assertEqual(status, 200, f"{path} 返回 {status}：{out}")

    def test_unknown_route_is_404_not_500(self):
        status, body = self.get_json("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("未知接口", body["error"])

    def test_bad_arguments_are_400_not_500(self):
        """参数不对是调用方的问题。回 500 会让人去翻日志找一个不存在的 bug。"""
        cases = [
            "/api/trend?param=不是参数",
            "/api/logs?name=../local.db",
            "/api/logs?name=没有这个.log",
            "/api/logs?name=terminal.log/../x.log",
            "/api/logs?name=local.db",              # 不是 .log 一律不读
        ]
        for path in cases:
            with self.subTest(path=path):
                status, _ = self.get_json(path)
                self.assertEqual(status, 400, f"{path} 该回 400")

    def test_bad_pile_is_refused(self):
        """桩号不在平台清单里 —— 终端侧就不该认。"""
        status, body = self.post_json("/api/pile", {"pile_id": "ZK999+999"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_import_without_operator_is_refused(self):
        """修订要追到人，没填操作人就不该往下走。"""
        status, body = self.post_json("/api/usb/import", {"operator": "  "})
        self.assertEqual(status, 400)
        self.assertIn("操作人", body["error"])

    def test_export_then_inspect_round_trip(self):
        status, body = self.post_json("/api/usb/export", {"scope": "all", "limit": 500})
        self.assertEqual(status, 200)
        res = body["result"]
        self.assertTrue(res["ok"], res)
        self.assertGreater(res["exported"], 0)

        status, body = self.post_json("/api/usb/inspect", {"bundle": res["dir"]})
        self.assertEqual(status, 200)
        rep = body["report"]
        # 两个信号要分得开：ok 说"包没毛病"，changed 说"里面有改动"
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["errors"], [])
        self.assertEqual(rep["counts"]["same"], res["exported"])
        self.assertEqual(rep["changed"], 0, "原样导出再导入，不该有改动")

    def test_edit_then_inspect_shows_the_change(self):
        """演示助手改两行 → 校验报告要能同时看出"值变了"和"摘要对不上"。"""
        status, body = self.post_json("/api/usb/export", {"scope": "all", "limit": 500})
        bundle = body["result"]["dir"]
        status, body = self.post_json(
            "/api/usb/edit", {"bundle": bundle, "key": "cylinderPumpingPressure", "count": 2})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["result"]["rows"], 2)

        status, body = self.post_json("/api/usb/inspect", {"bundle": bundle})
        rep = body["report"]
        self.assertEqual(rep["changed"], 2)
        self.assertTrue(rep["warnings"], "改过文件必须留下'被动过'的提示")
        self.assertTrue(all(not ch["intact"] for ch in rep["changes"]))
        # changes 里带 pile_id 是为了让人知道"改的是哪条桩号下的记录"，
        # 不是可改字段。可改的只有 diffs 里那几个参数 —— 桩号改不动。
        self.assertNotIn("pile_id",
                         {d["key"] for ch in rep["changes"] for d in ch["diffs"]})

    def test_import_creates_revisions_in_the_queue(self):
        status, body = self.post_json("/api/usb/export", {"scope": "all", "limit": 500})
        bundle = body["result"]["dir"]
        self.post_json("/api/usb/edit",
                       {"bundle": bundle, "key": "acceleratorAddSpeed", "count": 1})
        status, body = self.post_json("/api/usb/import", {
            "bundle": bundle, "operator": "李监理",
            "reason_code": "SENSOR_FAULT", "note": "接头松动"})
        self.assertEqual(status, 200, body)
        res = body["result"]
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["created"], 1)

        status, body = self.get_json("/api/usb")
        self.assertEqual(status, 200)
        self.assertEqual(body["revision_stat"], 1)
        rev = body["revisions"][0]
        self.assertEqual(rev["operator_name"], "李监理")
        self.assertEqual(rev["reason_code"], "SENSOR_FAULT")
        self.assertTrue(rev["target_id"].startswith(DEVICE + ":"),
                        f"修订要挂在一条真实的记录上，而不是 {rev['target_id']}")
        self.assertEqual(rev["status"], "pending", "还没传，就该是 pending")

    def test_fault_panel_shows_and_clears(self):
        status, body = self.get_json("/api/faults")
        self.assertTrue(body["presets"], "故障预置表不该是空的")
        keys = {f["key"] for f in body["presets"]}
        self.assertIn("cylinderPumpingPressure", keys)

        status, body = self.post_json("/api/fault", {"key": "cylinderPumpingPressure"})
        self.assertEqual(status, 200)
        self.assertEqual(body["injected"]["key"], "cylinderPumpingPressure")

        status, body = self.get_json("/api/faults")
        self.assertEqual(len(body["active"]), 1, "注入一个就只该有一条生效")
        self.assertIn("left_s", body["active"][0])

        status, body = self.post_json("/api/fault/clear", {})
        self.assertEqual(body["cleared"], 1)
        status, body = self.get_json("/api/faults")
        self.assertEqual(body["active"], [])


# ==========================================================================
# 静态资源
# ==========================================================================
class StaticAssetTests(TerminalWebTestBase):
    """三个页面 + 两个资源，从 HTTP 取，不看磁盘。"""

    ASSETS = {"index.html": "text/html", "usb.html": "text/html",
              "settings.html": "text/html", "app.js": "javascript",
              "style.css": "text/css"}

    def test_every_asset_is_served_with_a_usable_mime(self):
        for name, expect in self.ASSETS.items():
            with self.subTest(name=name):
                status, raw, ctype = self._request("GET", "/" + name)
                self.assertEqual(status, 200, name)
                self.assertIn(expect, ctype, f"{name} 的 MIME 是 {ctype}")
                self.assertTrue(raw, f"{name} 是空的")

    def test_root_serves_the_index_page(self):
        status, raw, ctype = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b'data-page="index"', raw)

    def test_missing_asset_is_404(self):
        status, _, _ = self._request("GET", "/nope.html")
        self.assertEqual(status, 404)

    def test_pages_are_valid_json_free_html(self):
        """每个页面都必须是完整可解析的最简 HTML 骨架。"""
        for name in self.ASSETS:
            if not name.endswith(".html"):
                continue
            with self.subTest(name=name):
                text = _read(os.path.join(WEB_DIR, name))
                self.assertIn("<!DOCTYPE html>", text)
                self.assertIn("</html>", text)
                self.assertIn('charset="utf-8"', text)
                self.assertIn("app.js", text, "页面没引 app.js，打开就是死的")


# ==========================================================================
# 页 ↔ JS 的 id 契约
# ==========================================================================
class PageContractTests(unittest.TestCase):
    """没有浏览器，只能静态查。但查得出"页面少写一个 id"这类致命错。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.js = _read(os.path.join(WEB_DIR, "app.js"))
        cls.css = _read(os.path.join(WEB_DIR, "style.css"))
        cls.html = {p: _read(os.path.join(WEB_DIR, f)) for p, f in PAGES.items()}

    def test_every_page_is_dispatched(self):
        """每个页面的 data-page 都要在 app.js 的分派表里有对应实现。"""
        dispatch = re.search(r"const PAGES = \{([^}]*)\}", self.js)
        self.assertIsNotNone(dispatch, "找不到 app.js 的分派表 PAGES")
        for page in PAGES:
            self.assertIn(page + ":", dispatch.group(1), f"分派表里没有 {page}")
            self.assertIn(f'data-page="{page}"', self.html[page],
                          f"{PAGES[page]} 没声明 data-page={page}")

    def test_nav_links_point_at_real_pages(self):
        links = re.findall(r"\[[\w']+,\s*'([\w.]+\.html)'", self.js)
        self.assertTrue(links)
        for href in links:
            self.assertTrue(os.path.isfile(os.path.join(WEB_DIR, href)),
                            f"导航指向了不存在的 {href}")

    def test_each_page_ids_cover_its_own_init_function(self):
        """每页的 init 用到的 id，该页必须都声明了。"""
        for page, fn in PAGE_INIT.items():
            used = _ids_required(_func_body(self.js, fn))
            declared = _ids_declared(self.html[page])
            missing = sorted(used - declared)
            self.assertEqual(missing, [],
                             f"{PAGES[page]} 缺少 {fn} 要用的 id：{missing}")

    def test_shared_helpers_only_require_ids_every_page_has(self):
        """顶栏那几个函数三页共用 —— 它们**硬取**的 id 必须是每页都有的。

        顶栏由 mountChrome 用 innerHTML 挂出来，所以"声明"来自 app.js 自己；
        除此之外的（比如首页独有的 sampleinfo）只能走 setText 那种可空取法，
        见 test_shared_helpers_do_not_hard_require_page_specific_ids。
        """
        in_js_html = _ids_declared(self.js)
        for fn in SHARED_FUNCS:
            for i in sorted(_ids_required(_func_body(self.js, fn)) - in_js_html):
                for page in PAGES:
                    self.assertIn(i, _ids_declared(self.html[page]),
                                  f"{fn} 硬取了 #{i}，而 {PAGES[page]} 里没有")

    def _all_declared(self) -> set[str]:
        """三页 HTML 加上 app.js 里挂载顶栏那段的 id，合起来。"""
        out = _ids_declared(self.js)
        for text in self.html.values():
            out |= _ids_declared(text)
        return out

    def test_shared_helpers_do_not_hard_require_page_specific_ids(self):
        """共用函数不许硬取单页独有的元素。

        `$('sampleinfo')` 在 U 盘页会拿到 null，紧接着 `.textContent =` 就是
        TypeError，整页死在第一帧 —— 而这正是 setText 要解决的问题。
        """
        declared = self._all_declared()
        for fn in SHARED_FUNCS:
            for i in sorted(_ids_required(_func_body(self.js, fn))):
                self.assertIn(i, declared, f"{fn} 引用了谁都没声明的 #{i}")

    def test_every_settext_target_is_declared_somewhere(self):
        """setText 允许目标不存在，但不允许**拼错** —— 那会静默什么都不做。"""
        missing = sorted(_ids_optional(self.js) - self._all_declared())
        self.assertEqual(missing, [], f"setText 写到了不存在的 id：{missing}")

    def test_every_api_path_in_js_is_served(self):
        """反向覆盖：界面调的每个接口，服务端都得真的有。

        比前一条更要紧 —— 界面调一个不存在的接口，页面上只会静悄悄什么都不动。
        """
        src = _read(os.path.join(config.ROOT, "terminal", "webui.py"))
        # 连字符也要算进路径字符：漏了它 `/api/upload-now` 会被截成
        # `/api/upload`，于是这条测试报一个根本不存在的接口名
        called = set(re.findall(r"['\"](/api/[\w./-]+)", self.js))
        called |= set(re.findall(r"['\"](/api/[\w./-]+)", "".join(self.html.values())))
        self.assertTrue(called, "app.js 里一个 /api/ 路径都没找到，正则该改了吧")
        for path in sorted(called):
            self.assertIn(f'"{path}"', src, f"界面调了 {path}，但 webui 里没有")

    def test_route_table_covers_the_page_contract(self):
        """webui 的每条静态路由分支都要能被上面的 PageContractTests 看到。"""
        src = _read(os.path.join(config.ROOT, "terminal", "webui.py"))
        for path in ("/api/status", "/api/records", "/api/uploads", "/api/batches",
                     "/api/piles", "/api/trend", "/api/params", "/api/faults",
                     "/api/usb", "/api/logs", "/api/link", "/api/upload-now",
                     "/api/pile", "/api/fault", "/api/fault/clear",
                     "/api/usb/export", "/api/usb/inspect", "/api/usb/import",
                     "/api/usb/edit"):
            self.assertIn(f'"{path}"', src, f"webui 里没有 {path}")

    def test_hex_color_tokens_are_well_formed(self):
        """`#2a3purple` 这种混进字母的残缺 token 浏览器只会静默忽略整条声明。"""
        for name, text in [("app.js", self.js), ("style.css", self.css)] + \
                          [(f, self.html[p]) for p, f in PAGES.items()]:
            for m in re.finditer(r"#[0-9][0-9a-zA-Z]*", text):
                tok = m.group(0)
                self.assertRegex(
                    tok, r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$",
                    f"{name} 里的颜色 token 不合法：{tok}")

    def test_css_covers_every_tag_class_js_emits(self):
        """JS 里拼出来的状态标签 class 都要有样式，否则状态在界面上无色可辨。"""
        for cls in ("uploaded", "pending", "inflight", "rejected", "void",
                    "acked", "partial", "lost", "sending",
                    "ok", "error", "retry", "offline", "network_error", "idle"):
            self.assertIn(f".tag.{cls}", self.css, f"缺少 .tag.{cls} 的样式")

    def test_css_covers_every_class_the_pages_use(self):
        """页面和 JS 里出现的 class 都要有样式，否则那一块在界面上没有形状。"""
        for cls in ("head", "nav", "lbl", "muted", "okbox", "stats", "stat",
                    "pick", "kvs", "kv", "frow", "fhead", "fbar", "logbox",
                    "pil", "dimv", "newv", "grphd", "fault",
                    "prow", "grp", "pile", "banner", "hidden", "note", "chip",
                    "card", "body", "wrap", "scroll", "grid", "num"):
            self.assertIn("." + cls, self.css, f"缺少 .{cls} 的样式")

    # 允许存在的输入框，一个不多。**桩号仍然不在其中** —— 而且不是"没写上"，
    # 是三层防线里最外面的一层：终端界面根本没有可输入桩号的地方。
    #
    # 2026-10 新增的两个（工点 ID / 设备编号）是有意放行的，它们和桩号的区别
    # 不是"重要性"而是**来源**：
    #   * 桩号是**测量结果**，写错了没有任何东西能纠正它 —— 所以只能从平台
    #     下发的清单里点选，本地一个字节都不许输入。
    #   * 工点 ID 和设备编号是**引用**：填进去之后必须过平台核对
    #     （/api/identity/check），平台不认的工点根本存不下来。
    #     换句话说，这两个框里能填什么，仍然由平台说了算。
    # 往这个集合里加东西之前先问一句：这个框填错了，谁来兜？
    ALLOWED_INPUTS = {"imp-operator", "imp-note", "id-site", "id-device"}

    def test_the_only_text_inputs_are_the_operator_and_the_note(self):
        """白名单而不是黑名单。

        黑名单（"不许出现 pile 字样"）挡不住 `id="zhuanghao"` 这种写法，
        而且这个页面上到处都在**谈论**桩号，"输入桩号"四个字本身就出现在
        说明文字里。所以这里反过来：把允许的输入框列全，多一个就报错。
        """
        seen = set()
        for page, text in self.html.items():
            for m in re.finditer(r"<input[^>]*>", text):
                tag = m.group(0)
                mid = re.search(r'id="([^"]+)"', tag)
                self.assertIsNotNone(mid, f"{PAGES[page]} 里有个没写 id 的输入框：{tag}")
                seen.add(mid.group(1))
        self.assertEqual(seen, self.ALLOWED_INPUTS,
                         "终端界面上的输入框只有操作人、备注、工点 ID、设备编号四个")

    def test_index_page_has_no_input_at_all(self):
        """首页连一个输入框都没有 —— 现场作业就靠这一页，它不该能打字。"""
        self.assertNotIn("<input", self.html["index"])
        self.assertNotIn("<textarea", self.html["index"])
