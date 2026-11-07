# -*- coding: utf-8 -*-
"""阶段 6：平台路由与页面契约。

这个文件是补课补出来的，起因是一个真实事故：`Api.__init__` 里写了
`self.params = registry(...)`，把处理 `/api/params` 的 `params()` 方法盖成了
一个 dict，路由一调就是 `TypeError: 'dict' object is not callable`。
**单测全绿** —— 因为所有用例都是直接 `api.upload(...)`、`api.history(...)`，
没有一条是从路由表走的，谁也没碰过那个名字。

所以这里第一条就是路由冒烟：把 `dispatch()` 认得的每个路径都真调一遍。
第二条是前端契约：`app.js` 里 `$('x')` 引用的 id 必须真在页面里。
前端没有构建步骤，写错一个 id 不报错、不进控制台，只是那一格永远不更新 ——
这种错只能靠把"js 用到的 id"和"html 声明的 id"两集合对起来发现。

不测的：浏览器里的实际渲染、CSS 布局效果。
"""
from __future__ import annotations

import gc
import json
import os
import re
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request

from common import config, http_util
from common.enums import AlarmLevel
from common.logging_setup import quiet
from common.params import PARAM_KEYS
from server import db, seed
from server.alarms import AlarmEngine
from server.api import Api, dispatch

quiet()   # 种子数据会造出历史报警，别让它们刷掉进度点

WEB_DIR = config.SERVER_WEB
DEVICE = "GHP9015H"


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fp:
        return fp.read()


def _get(api: Api, conn, url: str) -> tuple[int, object]:
    """按 `main.py` 的方式走一遍路由：拆 URL → dispatch。"""
    path, q = http_util.parse_qs(url)
    status, payload, _kind = dispatch(api, conn, "GET", path, q, None)
    return status, payload


class PlatformTestBase(unittest.TestCase):
    """临时库 + 3 天种子数据。种子是幂等的，所以多个用例共享也不脏。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.conn = db.init_db(os.path.join(cls.tmp.name, "platform.sqlite3"))
        cls.cfg = config.load_config()
        cls.cfg["device"]["device_code"] = "GHP9015H"   # 测试造帧固定此编号：与现场配置隔离
        cls.api = Api(cls.cfg, AlarmEngine(config.load_thresholds()))
        with db.transaction(cls.conn):
            # 工点档案要排在桩号前面：`identity_check` 的判定依据就是它，
            # 少了这一步 `sites` 是空表，核对接口会把所有工点都当成没档案。
            seed.ensure_sites(cls.conn, cls.cfg)
            seed.ensure_piles(cls.conn, cls.cfg)
            seed.ensure_device(cls.conn, cls.cfg)
            seed.seed_history(cls.cfg, days=3, conn=cls.conn)
        cls.record_id = cls.conn.execute(
            "SELECT record_id FROM frames ORDER BY ts_epoch LIMIT 1").fetchone()[0]

    @classmethod
    def tearDownClass(cls) -> None:
        db.close_conn()
        # 起过 HTTP 服务的用例，请求线程也各开过一条连接。线程结束后它们
        # 才失去引用，而 sqlite3.Connection 自带引用环，得等 GC 才真的放
        # 文件句柄；Windows 上句柄没放就删临时目录，会 WinError 32。
        gc.collect()
        cls.tmp.cleanup()


# ==========================================================================
# 路由冒烟：每一条都得能答上来
# ==========================================================================
class RouteSmokeTests(PlatformTestBase):
    """`dispatch()` 认得的每条 GET 路由都真调一次。

    允许的状态码按路由分别列出 —— 400/404 是业务答案，500 才是 bug。
    """

    ROUTES = (
        "/api/health",
        "/api/piles",
        "/api/piles?site_id=DQTYX-ZK",
        "/api/identity/check?site_id=DQTYX-ZK&device_code=GHP9015H",
        "/api/identity/check?site_id=不存在的工点&device_code=GHP9015H",
        "/api/identity/check?site_id=DQTYX-ZK",
        "/api/params",
        "/api/overview",
        "/api/devices",
        "/api/frames?limit=5",
        "/api/frames?device_code=GHP9015H&limit=5",
        "/api/series?params=cylinderPumpingPressure,acceleratorAddSpeed&minutes=60&max_points=120",
        "/api/series?params=不存在的参数",
        "/api/history?param=cylinderPumpingPressure&bucket=hour&days=3",
        "/api/history?param=cylinderPumpingPressure&bucket=day&days=30",
        "/api/history?param=不存在的参数",
        "/api/alarms?limit=10",
        "/api/alarms?active_only=1",
        "/api/cycles?limit=10",
        "/api/batches?limit=10",
        "/api/revisions?limit=10",
        "/api/audit?limit=10",
        "/api/rejects?limit=10",
        "/api/export.csv?limit=100&params=cylinderPumpingPressure,acceleratorAddSpeed",
        "/api/frame/不存在的记录",
    )

    def _call(self, url: str) -> tuple[int, object]:
        try:
            return _get(self.api, self.conn, url)
        except Exception as exc:                     # noqa: BLE001 —— 冒烟就是要抓住一切
            self.fail(f"路由 {url} 抛异常：{type(exc).__name__}: {exc}")

    def test_every_route_answers_not_500(self):
        for url in self.ROUTES:
            with self.subTest(route=url):
                status, _ = self._call(url)
                self.assertLess(status, 500, f"{url} 返回 {status}")

    #: 只收 POST、且不做冒烟调用的（shutdown 一调就把服务关了），
    #: 由 test_upload.py 与专项用例直接覆盖
    POST_ONLY = {"/api/heartbeat", "/api/upload", "/api/shutdown"}

    def test_route_list_covers_the_dispatch_table(self):
        """反向检查：`api.py` 路由表里新增的路径必须补进上面的冒烟清单。

        少了这条，冒烟清单会慢慢烂成"只盖住当年那几条"，
        而新加的路由恰好是最容易写错的。
        """
        src = _read(os.path.join(config.ROOT, "server", "api.py"))
        declared = set(re.findall(r'path == "(/api/[^"]+)"', src))
        declared |= set(re.findall(r'path\.startswith\("(/api/[^"]+)"\)', src))
        covered = {u.split("?")[0] for u in self.ROUTES}
        # 以 "/" 结尾的是前缀匹配（/api/frame/），由详情用例单独覆盖
        missing = sorted(p for p in declared
                         if p not in covered and not p.endswith("/")
                         and p not in self.POST_ONLY)
        self.assertEqual(missing, [], f"这些路由没有冒烟用例：{missing}")

    def test_frame_detail_accepts_a_raw_record_id(self):
        """record_id 里带冒号（设备:毫秒:序号），路径里必须原样传。

        前端因此**不能**用 encodeURIComponent —— `%3A` 不会被服务端还原成 `:`，
        拼出来就是个查不到的 id。这条用例把这个约定钉住。
        """
        self.assertIn(":", self.record_id)
        status, body = _get(self.api, self.conn, "/api/frame/" + self.record_id)
        self.assertEqual(status, 200)
        self.assertEqual(body["frame"]["record_id"], self.record_id)

    def test_unknown_route_is_not_a_crash(self):
        status, body = _get(self.api, self.conn, "/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)


class RoutePayloadTests(PlatformTestBase):
    """路由返回的字段名就是前端读的那些名字，错一个字界面就空一格。"""

    def test_params_exposes_the_effective_registry(self):
        """`/api/params` 是事故现场。它必须返回全部 16 个参数。"""
        status, body = _get(self.api, self.conn, "/api/params")
        self.assertEqual(status, 200)
        keys = [p["key"] for p in body["params"]]
        self.assertEqual(keys, list(PARAM_KEYS))
        for p in body["params"]:
            for field in ("key", "name", "unit", "lo", "hi", "decimals", "alarmable"):
                self.assertIn(field, p, f"参数 {p.get('key')} 少了 {field}")
        self.assertIsInstance(body["thresholds"], dict)

    def test_overview_keys(self):
        status, body = _get(self.api, self.conn, "/api/overview")
        self.assertEqual(status, 200)
        for k in ("server_time", "device_total", "device_online", "frames_24h",
                  "cycles_24h", "volume_24h", "alarms", "project", "backlog_total"):
            self.assertIn(k, body)
        for k in ("total", "active", "last_24h"):
            self.assertIn(k, body["alarms"])
        for k in ("project_name", "site_name"):
            self.assertIn(k, body["project"])

    def test_devices_keys(self):
        status, body = _get(self.api, self.conn, "/api/devices")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["count"], 1)
        d = body["devices"][0]
        for k in ("device_code", "device_name", "model", "site_name", "note",
                  "live_state", "state_label", "last_seen_local", "backlog",
                  "link_state"):
            self.assertIn(k, d)
        self.assertIn(d["live_state"], ("online", "degraded", "offline"))

    def test_series_keys(self):
        status, body = _get(self.api, self.conn,
                            "/api/series?params=cylinderPumpingPressure&minutes=1440&max_points=200")
        self.assertEqual(status, 200)
        for k in ("params", "points", "raw_points", "series"):
            self.assertIn(k, body)
        p = body["params"][0]
        for k in ("key", "name", "unit", "lo", "hi", "decimals", "alarmable"):
            self.assertIn(k, p)
        # 曲线画的是 ts/values 两条等长数组，长度不一致前端会画歪
        self.assertEqual(len(body["series"]["ts"]), body["points"])
        self.assertEqual(len(body["series"]["cylinderPumpingPressure"]), body["points"])

    def test_history_buckets_have_labels_and_alarm_counts(self):
        for bucket, days in (("hour", 3), ("day", 30)):
            with self.subTest(bucket=bucket):
                status, body = _get(
                    self.api, self.conn,
                    f"/api/history?param=cylinderPumpingPressure&bucket={bucket}&days={days}")
                self.assertEqual(status, 200)
                self.assertTrue(body["buckets"], "种子数据下不该是空桶")
                b = body["buckets"][0]
                for k in ("bucket", "n", "avg_v", "min_v", "max_v", "alarms", "label"):
                    self.assertIn(k, b)
                self.assertIn(body["param_name"], ("泵送压力",))
                # 标签必须能直接画在坐标轴上，不能是空的
                self.assertTrue(all(x["label"] for x in body["buckets"]))
                # 桶按时间递增，前端按顺序连线
                bs = [x["bucket"] for x in body["buckets"]]
                self.assertEqual(bs, sorted(bs))

    def test_alarm_rows_carry_what_the_table_shows(self):
        status, body = _get(self.api, self.conn, "/api/alarms?limit=50")
        self.assertEqual(status, 200)
        self.assertTrue(body["alarms"], "种子数据里注入了 4 次越限，报警表不该是空的")
        a = body["alarms"][0]
        for k in ("param_name", "param_unit", "level", "level_label", "active",
                  "duration_s", "peak_value", "pile_id", "started_at"):
            self.assertIn(k, a)
        self.assertIn(a["level"], [x.value for x in AlarmLevel])
        self.assertEqual(a["active"], a["ended_at"] is None)

    def test_cycles_carry_preformatted_local_times(self):
        status, body = _get(self.api, self.conn, "/api/cycles?limit=10")
        self.assertEqual(status, 200)
        self.assertTrue(body["cycles"])
        c = body["cycles"][0]
        for k in ("started_at", "started_local", "ended_local", "duration_s",
                  "volume_m3", "max_pressure", "pile_id"):
            self.assertIn(k, c)
        # 前端直接把 started_local 贴进表格，不做二次格式化
        self.assertRegex(c["started_local"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_batches_revisions_audit_rejects_keys(self):
        status, body = _get(self.api, self.conn, "/api/batches?limit=5")
        self.assertEqual(status, 200)
        for k in ("ts_server_local", "batch_id", "kind", "record_count",
                  "accepted", "duplicated", "rejected"):
            self.assertIn(k, body["batches"][0])
        self.assertNotIn("receipt_json", body["batches"][0], "回执原文不该发给前端")

        status, body = _get(self.api, self.conn, "/api/audit?limit=5")
        self.assertEqual(status, 200)
        for k in ("ts_device", "ts_server_local", "kind", "kind_label", "payload"):
            self.assertIn(k, body["events"][0])

        status, body = _get(self.api, self.conn, "/api/revisions?limit=5")
        self.assertEqual(status, 200)
        self.assertIn("count", body)

        status, body = _get(self.api, self.conn, "/api/rejects?limit=5")
        self.assertEqual(status, 200)
        self.assertIn("by_reason", body)

    def test_export_csv_is_excel_safe(self):
        status, text = _get(self.api, self.conn,
                            "/api/export.csv?limit=50&params=cylinderPumpingPressure")
        self.assertEqual(status, 200)
        self.assertTrue(text.startswith("﻿"), "缺 BOM，Excel 打开会乱码")
        first = text.lstrip("﻿").splitlines()[0]
        self.assertIn("record_id", first)
        self.assertIn("cylinderPumpingPressure", first)
        # 表头是 ASCII 列名，中文名在注册表里 —— 这样 Excel 处理表头不会出岔
        self.assertTrue(first.isascii(), f"CSV 表头混进了非 ASCII：{first}")


# ==========================================================================
# 页面契约：js 用的每个 id 都得在页面里
# ==========================================================================
class WebAssetTests(unittest.TestCase):

    PAGES = {
        "overview": "index.html",
        "alarms": "alarms.html",
        "history": "history.html",
        "frames": "frames.html",
        "devices": "devices.html",
        "audit": "audit.html",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.js = _read(os.path.join(WEB_DIR, "app.js"))
        cls.css = _read(os.path.join(WEB_DIR, "style.css"))
        cls.html = {p: _read(os.path.join(WEB_DIR, f)) for p, f in cls.PAGES.items()}

    # ---- 辅助 ----
    def _page_body(self, page: str) -> str:
        """截出某个页面的初始化函数体。

        顶层函数以第 0 列的 `}` 收尾，内层函数都是缩进的，所以第一个
        第 0 列 `}` 就是它的结束 —— 不用引 JS 解析器。
        """
        name = {"overview": "initOverview", "alarms": "initAlarms",
                "history": "initHistory", "frames": "initFrames",
                "devices": "initDevices", "audit": "initAudit"}[page]
        m = re.search(r"^function " + name + r"\(\) \{", self.js, re.M)
        self.assertIsNotNone(m, f"app.js 里找不到 {name}")
        end = self.js.index("\n}\n", m.end())
        return self.js[m.start():end]

    @staticmethod
    def _ids_used(src: str) -> set[str]:
        used = set(re.findall(r"\$\('([^']+)'\)", src))
        used |= set(re.findall(r"getElementById\('([^']+)'\)", src))
        # kpi('k-dev', ...) / rows('alarms', ...) 把 id 当第一个参数传
        used |= set(re.findall(r"(?:kpi|rows)\('([^']+)'", src))
        return used

    # ---- 用例 ----
    def test_every_id_used_by_a_page_exists_in_that_page(self):
        for page, html in self.html.items():
            declared = set(re.findall(r'id="([^"]+)"', html))
            missing = sorted(self._ids_used(self._page_body(page)) - declared)
            self.assertEqual(missing, [],
                             f"{self.PAGES[page]} 里没有这些 id：{missing}")

    def test_no_id_is_used_but_declared_nowhere(self):
        """id 拼错成别的名字时，上面那条会漏掉（它只比对所在页）。

        这里做全量反向检查：js 里出现过的 id 至少得在某一页存在，
        且页面之间不许借用（比如 history 页用了总览页的 k-dev）。
        """
        all_declared: dict[str, str] = {}
        for page, html in self.html.items():
            for i in re.findall(r'id="([^"]+)"', html):
                all_declared[i] = page
        for page in self.PAGES:
            for i in self._ids_used(self._page_body(page)):
                self.assertIn(i, all_declared, f"{page} 用了不存在的 id {i}")
                self.assertEqual(all_declared[i], page,
                                 f"{page} 用了属于 {all_declared[i]} 的 id {i}")

    def test_shared_helpers_only_use_ids_their_pages_declare(self):
        """共用函数按"谁可能调它"分两档定规矩：

        - 通用工具（`banner` / `setSite` / `kpi` …）三页都会调，
          所以它们只能碰三页**都有**的 id（顶栏、横幅这一层）；
        - 帧详情抽屉那三个只有 history 页调，只需要 history 声明了就行 ——
          `#drawer` 这层遮罩没必要出现在总览页。

        分错档的后果是同一类：某个页面上点一下，控制台报
        "Cannot read properties of null"，而页面看上去一切正常。
        """
        pages = {p: set(re.findall(r'id="([^"]+)"', h)) for p, h in self.html.items()}
        everywhere = set.intersection(*pages.values())
        groups = [
            (("api", "esc", "cst", "dur", "kpi", "rows", "banner", "fit",
              "drawLines", "drawBars", "setSite", "clockTick", "fmtRaw",
              "kindLabel"), everywhere, "每一页"),
            (("openFrame", "renderFrameDetail", "closeDrawer"),
             pages["history"], "history 页"),
        ]
        for names, allowed, where in groups:
            for fn in names:
                m = re.search(r"^function " + fn + r"\(", self.js, re.M)
                self.assertIsNotNone(m, f"app.js 里找不到 {fn}")
                end = self.js.index("\n}\n", m.end())
                missing = sorted(self._ids_used(self.js[m.start():end]) - allowed)
                self.assertEqual(
                    missing, [], f"{fn}() 引用了 {where}没有的 id：{missing}")

    def test_pages_reference_existing_assets(self):
        for page, html in self.html.items():
            refs = re.findall(r'<(?:script|link)[^>]*?(?:src|href)="([^"]+)"', html)
            self.assertTrue(refs, f"{page} 一个静态资源都没引用")
            for ref in refs:
                if ref.startswith(("http://", "https://", "//", "#", "data:")):
                    continue
                # 缓存破除参数（style.css?v=1102）不算路径的一部分
                path = os.path.join(WEB_DIR, ref.lstrip("/").split("?")[0])
                self.assertTrue(os.path.isfile(path), f"{page} 引用了不存在的 {ref}")


    def test_static_mime_types_are_browser_usable(self):
        cases = {"index.html": "text/html", "app.js": "javascript",
                 "style.css": "text/css"}
        for name, expect in cases.items():
            got = http_util.guess_mime(os.path.join(WEB_DIR, name))
            self.assertIn(expect, got or "", f"{name} 的 MIME 是 {got}")

    def test_css_covers_every_tag_class_the_api_can_return(self):
        """状态标签的颜色来自后端枚举，枚举值变了样式必须跟上。

        后端能返回的状态：设备 `live_state` 三态、报警 `level` 两级，
        加上 js 里写死的 corrected/dim/rejected。
        """
        for cls in ("online", "degraded", "offline", "warning", "critical",
                    "corrected", "dim", "rejected"):
            self.assertIn(f".tag.{cls}", self.css, f"缺少 .tag.{cls} 的样式")
        for cls in ("online", "degraded", "offline"):
            self.assertIn(f".dot.{cls}", self.css, f"缺少 .dot.{cls} 的样式")

    def test_css_classes_used_by_js_exist(self):
        for cls in ("mask", "drawer", "banner", "hidden", "note", "empty",
                    "corrected", "legend", "kpi", "kpi .v"):
            self.assertIn("." + cls, self.css, f"app.js 用到 .{cls} 但样式里没有")

    def test_hex_color_tokens_are_well_formed(self):
        for name, text in (("style.css", self.css), ("app.js", self.js)):
            for m in re.finditer(r"#[0-9][0-9a-zA-Z]*", text):
                tok = m.group(0)
                self.assertRegex(
                    tok, r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$",
                    f"{name} 里的颜色 token 不合法：{tok}")

    def test_js_does_not_url_encode_record_ids(self):
        """`openFrame` 必须原样拼 record_id（见上面的冒烟用例）。

        写一句注释提醒可以，真加上 encodeURIComponent 就会静默查不到。
        """
        m = re.search(r"^function openFrame\(", self.js, re.M)
        end = self.js.index("\n}\n", m.end())
        body = self.js[m.start():end]
        self.assertIn("/api/frame/' + recordId", body)
        self.assertNotIn("encodeURIComponent(recordId)", body)

    def test_no_page_calls_an_endpoint_the_server_does_not_serve(self):
        """前端有构建步骤时这种错会在打包期炸；这里只能手工对表。"""
        src = _read(os.path.join(config.ROOT, "server", "api.py"))
        declared = set(re.findall(r'path == "(/api/[^"]+)"', src))
        declared |= {p.rstrip("/") for p in
                     re.findall(r'path\.startswith\("(/api/[^"]+)"\)', src)}
        used = set(re.findall(r"'/api/([a-z_.]+)", self.js))
        for name in sorted(used):
            path = "/api/" + name
            ok = path in declared or any(path.startswith(d) for d in declared)
            self.assertTrue(ok, f"app.js 调了服务端没有的 {path}")


# ==========================================================================
# HTTP 层：状态码必须真的发出去
# ==========================================================================
class HttpStatusTests(PlatformTestBase):
    """起真的 `ThreadingHTTPServer`，看**响应行**里的状态码。

    为什么单开一条路：上面所有用例走的都是 `dispatch()`，**状态码是它的
    返回值，不是发出去的东西**。`main.py` 里曾经有这样一段：

        status, payload = self._jsonable(payload)   # 而这个函数 return 200, payload

    于是 API 回的 400 / 409 到了线上全变成 200，`dispatch()` 的用例照样全绿。
    后果是实打实的：终端靠 4xx 分辨"平台明确拒收"（不该再重试）和
    "没连上"（该退避重传）。平台永远回 200 的话，一次整批拒收要重传 8 次、
    耗时几分钟才落成 REJECTED，而且原因码在重传路上就丢了。

    所以这里不测 `dispatch()` 返回什么，测**客户端收到什么**。
    """

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from server import main as main_mod
        # 处理线程会自己 `db.get_conn()`，它按 `config.SERVER_DB` 找人 ——
        # 指向本类的临时库，别让测试往演示库里写。
        cls._saved_db = config.SERVER_DB
        config.SERVER_DB = os.path.join(cls.tmp.name, "platform.sqlite3")
        cls._saved_api = main_mod.Handler.api
        main_mod.Handler.api = cls.api
        cls.httpd = main_mod.Server(("127.0.0.1", 0), main_mod.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        import threading
        cls._thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        from server import main as main_mod
        main_mod.Handler.api = cls._saved_api
        config.SERVER_DB = cls._saved_db
        super().tearDownClass()

    def _request(self, method: str, path: str, body: dict | None = None):
        url = self.base + urllib.parse.quote(path, safe="/?&=%+-")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Connection": "close"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8") or "{}"
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"raw": raw[:200]}

    # ---- 用例 ----
    def test_a_refusal_arrives_as_409_not_200(self):
        """工点不在档案里 —— 终端要靠这个 409 认出"平台不同意"。"""
        status, body = self._request(
            "GET", "/api/identity/check?site_id=不存在的工点&device_code=GHP9015H")
        self.assertEqual(status, 409, f"收到 {status}：{body}")
        self.assertIn("error", body)
        self.assertTrue(body.get("known_sites"))

    def test_a_bad_request_arrives_as_400(self):
        status, body = self._request(
            "GET", "/api/identity/check?site_id=DQTYX-ZK")
        self.assertEqual(status, 400, f"收到 {status}：{body}")
        self.assertIn("error", body)

    def test_a_success_still_arrives_as_200(self):
        status, body = self._request(
            "GET", "/api/identity/check?site_id=DQTYX-ZK&device_code=GHP9015H")
        self.assertEqual(status, 200)
        self.assertIs(body.get("ok"), True)

    def test_an_unknown_api_path_arrives_as_404(self):
        status, _ = self._request("GET", "/api/nope")
        self.assertEqual(status, 404)

    def test_a_rejected_batch_arrives_as_4xx_not_200(self):
        """空批次：`dispatch` 早就要拒，终端要能一眼看出不用再传。"""
        status, body = self._request("POST", "/api/upload", {})
        self.assertGreaterEqual(status, 400, f"收到 {status}：{body}")
        self.assertLess(status, 500, f"收到 {status}：{body}")
        self.assertIn("error", body)
