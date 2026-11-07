/* 云平台前端。原生 JS，无构建步骤。
 *
 * 一个文件带三个页面，按 <body data-page="..."> 分派。合在一起是因为这三个页
 * 共用的东西太多（时间格式化、折线图、帧详情抽屉、颜色约定）——
 * 拆成三份必然出现"三处各写一份格式化"的漂移。
 *
 * 平台前端**不自己判定任何业务规则**：越限与否、报警级别、桩号是否合法，
 * 全部由服务端算好放在响应里（见 server/alarms.py）。前端只负责把数摆到屏幕上，
 * 一旦它开始"帮"服务端判断，规则就有了第二个来源。
 */
'use strict';

const $ = (id) => document.getElementById(id);
const PAGE = document.body.dataset.page;

const COLORS = ['#33b1ff', '#ffb454', '#38d39f', '#c3a6ff', '#ff5f6d', '#5ad1e6'];

/* ── 服务看门狗 ────────────────────────────────────────────────────────
 * 这一页发出去之后就和服务器没关系了：黑窗口一关（或者演示压根没起），
 * 浏览器里这一页还留着，看上去一切正常，只是**再也不动了** —— 没有报错、
 * 没有提示，现场只会以为"系统卡死了"。
 * 所以把"连不上"变成一句看得见的话。
 *
 * 这一页全是只读的（查询/看曲线/翻台账），没有用户点击的写操作，
 * 所以不需要终端页那种 clickFailed —— 只判断掉线就够了。
 */
const SVC = { fails: 0, down: false };
const SVC_DOWN_AFTER = 2;      // 轮询连丢两次，就不再等了

const SVC_HINT = '服务没有响应 —— 那个黑窗口是不是被关了？'
  + '重新双击「启动演示.bat」，等它打出"演示环境已就绪"再刷新本页。';

function api(path) {
  // 计数点放在**拿到能解析的 JSON 之后**。连不上、端口被别的程序占着
  // （答一页 HTML）、HTTP 错误，对页面来说都是同一件事：刷不出数据。
  // 只认其中一种，另外几种就会继续静默。
  return fetch(path).then((r) => {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(
    (data) => {
      // 恢复后不用特意清横幅：紧接着的加载器会按真实状态重画。
      if (SVC.down) { SVC.down = false; banner(''); }
      SVC.fails = 0;
      return data;
    },
    (err) => {
      if (++SVC.fails >= SVC_DOWN_AFTER && !SVC.down) {
        SVC.down = true;
        banner(SVC_HINT, true);
      }
      throw err;
    });
}
function esc(s) {
  return String(s === null || s === undefined ? '' : s).replace(/[&<>"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/* 后端给的时间有两种：已经转好本地时间的 `*_local` 字段（直接用），
 * 和 UTC ISO 原串（这里补 +08:00 再显示）。设备在现场，看的是北京时间。 */
function cst(iso, withDate) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return String(iso).slice(0, 19).replace('T', ' ');
  const t = new Date(d.getTime() + (480 + d.getTimezoneOffset()) * 60000);
  const hh = t.toTimeString().slice(0, 8);
  return withDate === false ? hh : t.toISOString().slice(5, 10) + ' ' + hh;
}
function dur(sec) {
  if (sec === null || sec === undefined) return '进行中';
  const s = Math.max(0, Math.round(sec));
  if (s < 60) return s + ' 秒';
  if (s < 3600) return Math.floor(s / 60) + ' 分';
  return Math.floor(s / 3600) + ' 时 ' + Math.floor(s % 3600 / 60) + ' 分';
}
/** 写一张 KPI 卡片。
 *
 * id 挂在哪儿，三个页面并不一致（历史遗留）：总览页挂在卡片上，设备和历史页
 * 挂在 .v 上，而设备页的 d-dev 又跟总览一样挂在卡片上。所以不能假定 id 就是卡片。
 *
 * 原来假定了，于是 `box.querySelector('.v')` 在历史和设备页返回 null，
 * 赋值时抛 TypeError，**把整个 .then 链打断** —— 表现不是报错，是那几个 KPI
 * 停在「—」，后面的历史曲线、上传批次表、拒收明细干脆不渲染，静悄悄地少半页。
 */
function kpi(id, value, sub, cls) {
  const el = $(id);
  if (!el) return;
  const onValue = el.classList.contains('v');
  const card = onValue ? el.parentElement : el;
  const v = onValue ? el : el.querySelector('.v');
  if (!card || !v) return;
  v.innerHTML = value;
  if (sub !== undefined && sub !== null) {
    const s = card.querySelector('.s');
    if (s) s.innerHTML = sub;
  }
  // cls 只在明确传了的时候才动 class。老代码是无条件覆盖的，那样会把 HTML 上
  // 写死的 accent / ok / warn 一并抹掉，而历史页有几个调用根本不传 cls。
  if (cls !== undefined && card.classList.contains('kpi')) {
    card.className = 'card kpi' + (cls ? ' ' + cls : '');
  }
}
function rows(tbody, html, cols, empty) {
  const el = $(tbody);
  el.innerHTML = html || '<tr><td colspan="' + cols + '" class="empty">' + esc(empty || '暂无数据') + '</td></tr>';
}
function banner(msg, bad) {
  const b = $('banner');
  if (!b) return;
  b.textContent = msg || '';
  b.className = 'banner' + (bad ? ' bad' : '') + (msg ? '' : ' hidden');
}

/* ===================== 画图 ===================== */

/** 按设备像素比调整画布，返回 CSS 像素下的绘图上下文与尺寸。
 *
 * 高度只在**第一次**从 HTML 的 height 属性取，之后认这里记下来的那个值。
 * 不能每轮都去 getAttribute('height')：下面那行把 cv.height 设成 h*dpr，
 * 改的就是同一个 height 属性，下一轮再读回来已经是上一轮的两倍 ——
 * 250 → 500 → 1000 → ……，曲线每重画一次页面就长高一截，最后几万像素。
 *
 * 宽度没这个毛病，它读的是 clientWidth（实际渲染宽度），不是属性。
 * 所以只有高度会失控，而且 dpr=1 时看不出来（乘 1 等于没乘），
 * 一定是在 125% / 150% 缩放的机器上才暴露。
 */
function fit(cv) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(320, cv.clientWidth || cv.width);
  if (!cv.dataset.cssHeight) {
    cv.dataset.cssHeight = String(parseInt(cv.getAttribute('height'), 10) || 240);
  }
  const h = parseInt(cv.dataset.cssHeight, 10);
  cv.style.height = h + 'px';
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
  }
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

/** 折线图：多条曲线共用一套坐标轴，可画上下限虚线。 */
function drawLines(cv, points, series, opts) {
  const o = opts || {};
  const { ctx, w, h } = fit(cv);
  const padL = 56, padR = 14, padT = 12, padB = 24;
  ctx.font = '11px Consolas, monospace';

  if (!points || points.length < 2) {
    ctx.fillStyle = '#8ba0bd';
    ctx.font = '13px "Microsoft YaHei"';
    ctx.fillText(o.empty || '暂无数据', padL, h / 2);
    return;
  }
  const shown = series.filter((s) => !s.hidden && s.values.some((v) => v !== null));
  let lo = Infinity, hi = -Infinity;
  shown.forEach((s) => s.values.forEach((v) => {
    if (v === null || v === undefined) return;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }));
  if (!isFinite(lo)) { lo = 0; hi = 1; }
  if (hi - lo < 1e-9) hi = lo + 1;
  const span = hi - lo;
  lo -= span * 0.12; hi += span * 0.12;

  const X = (i) => padL + (w - padL - padR) * i / (points.length - 1);
  const Y = (v) => padT + (h - padT - padB) * (1 - (v - lo) / (hi - lo));

  ctx.strokeStyle = '#1e293b';
  ctx.fillStyle = '#8ba0bd';
  ctx.lineWidth = 1;
  for (let k = 0; k <= 4; k++) {
    const v = lo + (hi - lo) * k / 4, y = Y(v);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.fillText(v.toFixed(2), 6, y + 4);
  }
  // 正常的量程参考线（服务端给的注册表阈值，不是前端拍的）
  shown.forEach((s) => {
    if (s.lo === undefined || s.lo === null) return;
    ctx.save();
    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = '#4a3a16';
    [s.lo, s.hi].forEach((v) => {
      const y = Y(v);
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    });
    ctx.restore();
  });

  shown.forEach((s) => {
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.7;
    ctx.beginPath();
    let started = false;
    s.values.forEach((v, i) => {
      if (v === null || v === undefined) { started = false; return; }
      const x = X(i), y = Y(v);
      if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
    });
    ctx.stroke();
  });

  ctx.fillStyle = '#8ba0bd';
  ctx.font = '11px "Microsoft YaHei"';
  const t0 = points[0], t1 = points[points.length - 1];
  const label = (t) => {
    const d = new Date(t + 28800000);
    return d.toISOString().slice(o.dayAxis ? 5 : 11, o.dayAxis ? 10 : 16).replace('T', ' ');
  };
  ctx.fillText(label(t0), padL, h - 8);
  const last = label(t1);
  ctx.fillText(last, w - padR - ctx.measureText(last).width, h - 8);
}

/** 柱状图 + 上下须线。用于历史聚合（每桶均值 + 极值）。 */
function drawBars(cv, buckets, opts) {
  const o = opts || {};
  const { ctx, w, h } = fit(cv);
  const padL = 56, padR = 14, padT = 12, padB = 26;
  ctx.font = '11px Consolas, monospace';
  if (!buckets || !buckets.length) {
    ctx.fillStyle = '#8ba0bd';
    ctx.font = '13px "Microsoft YaHei"';
    ctx.fillText(o.empty || '该区间没有数据', padL, h / 2);
    return;
  }
  let lo = Infinity, hi = -Infinity;
  buckets.forEach((b) => {
    [b.min_v, b.max_v, b.avg_v].forEach((v) => {
      if (v === null || v === undefined) return;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    });
  });
  if (!isFinite(lo)) { lo = 0; hi = 1; }
  if (hi - lo < 1e-9) hi = lo + 1;
  const span = hi - lo;
  lo -= span * 0.12; hi += span * 0.12;

  const bw = (w - padL - padR) / buckets.length;
  const Y = (v) => padT + (h - padT - padB) * (1 - (v - lo) / (hi - lo));

  ctx.strokeStyle = '#1e293b';
  ctx.fillStyle = '#8ba0bd';
  ctx.lineWidth = 1;
  for (let k = 0; k <= 4; k++) {
    const v = lo + (hi - lo) * k / 4, y = Y(v);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.fillText(v.toFixed(2), 6, y + 4);
  }
  if (o.lo !== undefined && o.lo !== null) {
    ctx.save(); ctx.setLineDash([5, 4]); ctx.strokeStyle = '#6b551f';
    [o.lo, o.hi].forEach((v) => {
      const y = Y(v);
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    });
    ctx.restore();
  }

  buckets.forEach((b, i) => {
    const cx = padL + bw * (i + 0.5);
    if (b.avg_v === null || b.avg_v === undefined) return;
    if (b.alarms) {                       // 有报警的时段铺一层底
      ctx.fillStyle = '#4a1d23';
      ctx.fillRect(padL + bw * i + 0.5, padT, bw - 1, h - padT - padB);
    }
    const yAvg = Y(b.avg_v), yMin = Y(b.min_v), yMax = Y(b.max_v);
    ctx.strokeStyle = '#7a6bd6';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(cx, yMax); ctx.lineTo(cx, yMin);
    ctx.moveTo(cx - 3, yMax); ctx.lineTo(cx + 3, yMax);
    ctx.moveTo(cx - 3, yMin); ctx.lineTo(cx + 3, yMin);
    ctx.stroke();
    ctx.fillStyle = '#33b1ff';
    const barW = Math.max(2, Math.min(18, bw * 0.55));
    ctx.globalAlpha = 0.85;
    ctx.fillRect(cx - barW / 2, Math.min(yAvg, Y(lo)), barW, Math.abs(Y(lo) - yAvg));
    ctx.globalAlpha = 1;
  });

  ctx.fillStyle = '#8ba0bd';
  ctx.font = '11px "Microsoft YaHei"';
  const step = Math.max(1, Math.ceil(buckets.length / 8));
  buckets.forEach((b, i) => {
    if (i % step) return;
    const label = String(b.label || '');
    const cx = padL + bw * (i + 0.5);
    ctx.fillText(label, cx - ctx.measureText(label).width / 2, h - 8);
  });
}

/* ===================== 帧详情抽屉 ===================== */

function closeDrawer() {
  const d = $('drawer');
  if (d) { d.className = 'hidden'; d.innerHTML = ''; }
}

function openFrame(recordId) {
  const d = $('drawer');
  if (!d) return;
  d.className = 'mask';
  d.innerHTML = '<div class="drawer"><button class="x" onclick="closeDrawer()">关闭</button>'
    + '<p class="empty">读取中…</p></div>';
  d.onclick = (e) => { if (e.target === d) closeDrawer(); };
  // record_id 里带冒号（设备:毫秒:序号），**不能** encodeURIComponent ——
  // 编码后服务端拿到的就不是库里那个 id 了
  api('/api/frame/' + recordId).then((r) => renderFrameDetail(r)).catch((e) => {
    d.innerHTML = '<div class="drawer"><button class="x" onclick="closeDrawer()">关闭</button>'
      + '<p class="empty">读取失败：' + esc(e.message) + '</p></div>';
  });
}

function renderFrameDetail(r) {
  const d = $('drawer');
  const f = r.frame;
  const revs = r.revisions || [];
  const patch = {};
  revs.forEach((v) => Object.assign(patch, v.patch || {}));
  let diff = '';
  Object.keys(patch).forEach((k) => {
    const raw = f.raw[k], eff = f.values[k];
    diff += '<tr><td>' + esc(k) + '</td>'
      + '<td class="num old">' + esc(fmtRaw(raw)) + '</td>'
      + '<td class="num new">' + esc(fmtRaw(eff)) + '</td></tr>';
  });
  let hist = '';
  revs.forEach((v) => {
    hist += '<tr><td>' + esc(v.ts_server_local) + '</td>'
      + '<td>' + esc(v.reason_label || v.reason_code) + '</td>'
      + '<td>' + esc(v.operator_name || '—') + '</td>'
      + '<td>' + esc(v.patch_text || '') + '</td>'
      + '<td>' + esc(v.reason_note || '') + '</td></tr>';
  });

  d.innerHTML = '<div class="drawer">'
    + '<button class="x" onclick="closeDrawer()">关闭</button>'
    + '<h2>记录详情' + (r.corrected ? ' <span class="tag corrected">已修正</span>' : '') + '</h2>'
    + '<div class="sub">' + esc(f.record_id) + '</div>'
    + '<div class="kv" style="margin-top:10px">'
    + '<dt>设备</dt><dd>' + esc(f.device_code) + '</dd>'
    + '<dt>设备时间</dt><dd>' + esc(f.ts_local) + '</dd>'
    + '<dt>桩号</dt><dd>' + esc(f.pile_id) + '</dd>'
    + '<dt>来源</dt><dd>' + esc(f.source) + '</dd>'
    + '<dt>数据质量</dt><dd>' + (f.quality === 0 ? '正常' : f.quality === 1 ? '可疑（采样迟到）' : '无效') + '</dd>'
    + '<dt>修订次数</dt><dd>' + (f.rev_count || 0) + '</dd>'
    + '</div>'
    + (diff ? '<h3>原值 / 有效值</h3><table class="grid diff"><thead><tr>'
        + '<th>参数</th><th class="num">原始值</th><th class="num">有效值</th></tr></thead>'
        + '<tbody>' + diff + '</tbody></table>' : '')
    + '<h3>修订历史</h3>'
    + (hist ? '<table class="grid"><thead><tr><th>平台收到</th><th>原因</th>'
        + '<th>操作人</th><th>改成</th><th>说明</th></tr></thead><tbody>' + hist
        + '</tbody></table>'
      : '<p class="note">这条记录没有被修改过。原始值就是有效值 —— 平台里'
        + '任何修改都是<strong>新增一条修订</strong>，不会覆盖这一行。</p>')
    + '<h3>全部参数</h3><table class="grid"><thead><tr><th>参数</th><th class="num">有效值</th>'
    + '<th class="num">原始值</th></tr></thead><tbody>'
    + Object.keys(f.values).map((k) => '<tr><td>' + esc(k) + '</td>'
        + '<td class="num">' + esc(fmtRaw(f.values[k])) + '</td>'
        + '<td class="num">' + esc(fmtRaw(f.raw ? f.raw[k] : null)) + '</td></tr>').join('')
    + '</tbody></table>'
    + '</div>';
}

function fmtRaw(v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  return isNaN(n) ? String(v) : (Math.abs(n) >= 1000 ? n.toFixed(1) : n.toFixed(2));
}

/* ===================== 通用头部 ===================== */
function setSite(project) {
  if (!project) return;
  const parts = [project.project_name, project.site_name].filter(Boolean);
  const el = $('site');
  if (el) el.innerHTML = esc(parts.join(' · '));
}
function clockTick() {
  const el = $('clock');
  if (el) el.textContent = cst(new Date().toISOString(), false);
}

const OVERVIEW_POLL = 3000;
const DEVICES_POLL = 5000;

/* ===================== 运行总览 ===================== */
function initOverview() {
  const hidden = new Set();
  const state = { params: [], win: '10' };

  $('win').onchange = () => {
    state.win = $('win').value;
    loadSeries();
  };

  function loadHead() {
    return api('/api/overview').then((o) => {
      setSite(o.project);
      const online = o.device_online, total = o.device_total;
      kpi('k-dev', online + ' <small>/ ' + total + '</small>',
          online === total ? '全部在线' : (total - online) + ' 台离线',
          online === total ? 'ok' : (online ? 'warn' : 'bad'));
      kpi('k-frames', o.frames_24h.toLocaleString(),
          '约 ' + Math.round(o.frames_24h * 2 / 60) + ' 分钟的数据量 · 采样 2 秒/帧', 'accent');
      kpi('k-cycles', o.cycles_24h + ' <small>轮</small>',
          '累计 ' + o.volume_24h.toFixed(2) + ' m³ 喷射量', 'ok');
      const a = o.alarms || {};
      kpi('k-alarm', (a.active || 0) + ' <small>未恢复</small>',
          '近 24 小时 ' + (a.last_24h || 0) + ' 次 · 累计 ' + (a.total || 0) + ' 次',
          a.active ? 'bad' : '');
      if (o.backlog_total) {
        banner('设备侧还有 ' + o.backlog_total + ' 条记录未上传（断网缓存或正在补传）', false);
      } else {
        banner('');
      }
    });
  }

  function loadDevices() {
    return api('/api/devices').then((d) => {
      $('dev-count').textContent = d.count + ' 台';
      $('devbox').innerHTML = d.devices.map((x) => {
        const st = x.live_state;
        return '<div><div class="devtop">'
          + '<span class="nm">' + esc(x.device_name || x.device_code) + '</span>'
          + '<span class="code">' + esc(x.device_code) + '</span>'
          + '<span class="spacer"></span>'
          + '<span class="tag ' + st + '"><span class="dot ' + st + '"></span> '
          + esc(x.state_label) + '</span></div>'
          + '<dl class="kv" style="margin:8px 0 0">'
          + '<dt>最后心跳</dt><dd>' + esc(x.last_seen_local) + '</dd>'
          + '<dt>工况</dt><dd>' + esc(x.link_state === 'offline' ? '—' : (x.link_state || '—')) + '</dd>'
          + '<dt>未上传</dt><dd>' + (x.backlog || 0) + ' 条</dd>'
          + '<dt>工点</dt><dd style="font-family:inherit">'
          + esc(x.site_name || '—') + '</dd>'
          + '</dl></div>';
      }).join('') || '<div class="empty">还没有设备接入</div>';
    });
  }

  function loadAlarms() {
    const q = $('actonly').checked ? '?active_only=1' : '';
    return api('/api/alarms' + q).then((a) => {
      const s = a.stats || {};
      $('alarm-stat').textContent = '未恢复 ' + (s.active || 0) + ' · 24 小时 '
        + (s.last_24h || 0) + ' · 累计 ' + (s.total || 0);
      rows('alarms', a.alarms.map((x) => {
        const lv = x.level === 'critical' ? '报警' : '预警';
        return '<tr><td>' + esc(cst(x.started_at)) + '</td>'
          + '<td>' + esc(x.param_name) + '</td>'
          + '<td><span class="tag ' + esc(x.level) + '">' + lv + '</span></td>'
          + '<td class="num">' + esc(fmtRaw(x.peak_value)) + ' ' + esc(x.param_unit) + '</td>'
          + '<td class="num">' + esc(dur(x.duration_s)) + '</td>'
          + '<td>' + (x.active ? '<span class="tag critical">未恢复</span>'
                               : '<span class="tag dim">已恢复</span>') + '</td>'
          + '<td>' + esc(x.pile_id || '—') + '</td></tr>';
      }).join(''), 7, '没有报警记录 —— 参数都在正常范围内');
    });
  }

  function loadCycles() {
    return api('/api/cycles?limit=30').then((c) => {
      const done = (c.cycles || []).filter((x) => x.ended_at);
      $('cyc-stat').textContent = done.length ? '最近 ' + done.length + ' 轮' : '';
      rows('cycles', (c.cycles || []).map((x) => '<tr>'
        + '<td>' + esc(x.started_local) + '</td>'
        + '<td>' + esc(x.pile_id || '—') + '</td>'
        + '<td class="num">' + (x.volume_m3 === null ? '—' : Number(x.volume_m3).toFixed(2)) + '</td>'
        + '<td class="num">' + esc(dur(x.duration_s)) + '</td>'
        + '<td class="num">' + esc(fmtRaw(x.max_pressure)) + '</td></tr>').join(''),
        5, '还没有完成的喷浆循环');
    });
  }

  function loadSeries() {
    return api('/api/series?params=pump_pressure,accel_flow,hyd_oil_temp&minutes='
               + state.win + '&max_points=200')
      .then((s) => {
        state.params = s.params;
        $('series-stat').textContent = s.raw_points > s.points
          ? s.raw_points + ' 点抽稀到 ' + s.points + ' 点' : s.points + ' 点';
        const ts = s.series.ts;
        const series = s.params.map((p, i) => ({
          key: p.key, name: p.name, unit: p.unit, lo: p.lo, hi: p.hi,
          color: COLORS[i % COLORS.length],
          values: s.series[p.key],
          hidden: hidden.has(p.key),
        }));
        // 三条曲线量纲差得远（MPa / L·h⁻¹ / ℃），同轴只看得清趋势 ——
        // 这里刻意不做双轴：双轴图很容易让人把两条线的高度当成同一件事。
        drawLines($('chart'), ts, series, { empty: '该时间窗内还没有数据' });
        $('legend').innerHTML = series.map((x) =>
          '<span class="k' + (x.hidden ? ' off' : '') + '" data-k="' + x.key + '">'
          + '<i style="background:' + x.color + '"></i>' + esc(x.name) + '（' + esc(x.unit)
          + '） 正常 ' + x.lo + '~' + x.hi + '</span>').join('');
        $('legend').querySelectorAll('.k').forEach((el) => {
          el.onclick = () => {
            const k = el.dataset.k;
            if (hidden.has(k)) hidden.delete(k); else hidden.add(k);
            loadSeries();
          };
        });
      });
  }

  $('actonly').onchange = loadAlarms;

  function tick() {
    clockTick();
    loadHead().catch(() => {});
    loadAlarms().catch(() => {});
    loadCycles().catch(() => {});
  }
  tick();
  setInterval(tick, OVERVIEW_POLL);
  setInterval(() => { loadDevices().catch(() => {}); }, OVERVIEW_POLL * 2);
  setInterval(() => { loadSeries().catch(() => {}); }, OVERVIEW_POLL * 2 + 1000);
  api('/api/params').then((p) => {
    const pp = (p.params || []).find((x) => x.key === 'pump_pressure');
    if (pp) $('interval').textContent = '2';
  });
}

/* ===================== 历史数据 ===================== */
function initHistory() {
  let param = 'pump_pressure';

  api('/api/params').then((p) => {
    $('param').innerHTML = (p.params || []).map((x) =>
      '<option value="' + esc(x.key) + '"' + (x.key === param ? ' selected' : '') + '>'
      + esc(x.name) + (x.unit ? '（' + esc(x.unit) + '）' : '') + '</option>').join('');
    load();
  });

  function load() {
    param = $('param').value || param;
    const bucket = $('bucket').value;
    const days = $('days').value;
    loadChart(param, bucket, days);
    loadFrames();
  }

  function loadChart(p, bucket, days) {
    return api('/api/history?param=' + encodeURIComponent(p) + '&bucket=' + bucket
               + '&days=' + days).then((h) => {
      $('h-stat').textContent = h.buckets.length + ' 个数据点 · 共 '
        + h.total_frames.toLocaleString() + ' 条原始记录';
      const b = h.buckets || [];
      const mins = b.reduce((a, x) => a + (x.min_v === null ? 0 : x.min_v), 0);
      const maxs = b.reduce((a, x) => a + (x.max_v === null ? 0 : x.max_v), 0);
      const avgs = b.reduce((a, x) => a + (x.avg_v || 0), 0);
      const alarms = b.reduce((a, x) => a + (x.alarms || 0), 0);
      kpi('h-frames', h.total_frames.toLocaleString(), '最近 ' + days + ' 天 · 粒度'
          + (bucket === 'hour' ? ' 1 小时' : ' 1 天'));
      kpi('h-alarms', String(alarms), alarms ? '在曲线里用红底标出' : '区间内没有报警',
          alarms ? 'bad' : '');
      drawBars($('hchart'), b, {
        lo: h.buckets.length ? undefined : undefined,
        empty: '该区间没有数据 —— 先用 --seed 生成演示数据，或让终端跑一会儿',
      });
      // 这里**不要**再去重置 h-volume / h-rev。那两张卡各有各的主人：
      // 方量归 loadCycles，修订数归 loadRevisions。曲线接口比它们慢，
      // 顺手写一个「—」进去，等于把人家刚填好的数又擦掉 —— 页面打开后
      // 那两格会长时间停在「—」，看着像坏了。HTML 里本来就是「—」。
      $('hlegend').innerHTML = '<span><i style="background:#33b1ff"></i>均值</span>'
        + '<span><i style="background:#7a6bd6"></i>极值范围</span>'
        + '<span><i style="background:#4a1d23"></i>有报警的时段</span>';
      $('h-range').textContent = h.param_name + (h.unit ? '（' + h.unit + '）' : '');
      return h;
    });
  }

  function loadFrames() {
    return api('/api/frames?limit=200').then((f) => {
      const corr = (f.frames || []).filter((x) => x.corrected).length;
      $('f-stat').textContent = '最近 ' + f.count + ' 条 · 其中 '
        + corr + ' 条被修正过';
      $('export').href = '/api/export.csv?limit=5000&params='
        + encodeURIComponent('pump_pressure,accel_flow,hyd_oil_temp,motor_current');
      rows('frames', (f.frames || []).map((x) => {
        const d = x.display || {};
        return '<tr' + (x.corrected ? ' class="corrected"' : '') + '>'
          + '<td>' + esc(String(x.ts_local).slice(5)) + '</td>'
          + '<td>' + esc(x.pile_id || '—') + '</td>'
          + '<td class="num">' + esc(d.pump_pressure || '') + '</td>'
          + '<td class="num">' + esc(d.accel_flow || '') + '</td>'
          + '<td class="num">' + esc(d.hyd_oil_temp || '') + '</td>'
          + '<td class="num">' + esc(d.motor_current || '') + '</td>'
          + '<td>' + esc(x.source) + '</td>'
          + '<td>' + (x.corrected ? '<span class="tag corrected">已修正</span>'
                                  : '<span class="tag dim">原始</span>')
          + (x.quality ? ' <span class="tag warning">可疑</span>' : '') + '</td>'
          + '<td><button class="x" data-r="' + esc(x.record_id) + '">详情</button></td>'
          + '</tr>';
      }).join(''), 9, '还没有记录');
      $('frames').querySelectorAll('button[data-r]').forEach((b) => {
        b.onclick = () => openFrame(b.dataset.r);
      });
    });
  }

  // 循环表给历史页补一个"区间方量"，用循环表的方量求和（帧里没有方量）
  function loadCycles() {
    return api('/api/cycles?limit=500').then((c) => {
      const days = Number($('days').value);
      const cutoff = Date.now() - days * 86400000;
      const inRange = (c.cycles || []).filter((x) => {
        const t = Date.parse(x.started_at);
        return !isNaN(t) && t >= cutoff && x.ended_at;
      });
      const vol = inRange.reduce((a, x) => a + (Number(x.volume_m3) || 0), 0);
      kpi('h-volume', vol.toFixed(2) + ' <small>m³</small>',
          inRange.length + ' 个循环 · 平均 ' + (inRange.length ? (vol / inRange.length).toFixed(2) : '0.00') + ' m³/轮');
      // h-rev 不在这里写：那是「被修正的记录」，只有 loadRevisions 知道。
      // 这里写循环条数，跟 loadRevisions 抢同一格，谁后到显谁，全看网络心情。
    });
  }

  function loadRevisions() {
    return api('/api/revisions?limit=100').then((r) => {
      kpi('h-rev', String(r.count), r.count ? '被修改过的记录数' : '所有记录都保持原始值');
    });
  }

  ['param', 'bucket', 'days'].forEach((id) => { $(id).onchange = load; });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

  clockTick();
  setInterval(clockTick, 1000);
  load();
  loadRevisions().catch(() => {});
  loadCycles().catch(() => {});
  setInterval(() => {
    loadChart($('param').value, $('bucket').value, $('days').value).catch(() => {});
    loadCycles().catch(() => {});
  }, 30000);
  api('/api/overview').then((o) => setSite(o.project)).catch(() => {});
}

/* ===================== 设备与传输 ===================== */
function initDevices() {
  function loadHead() {
    return api('/api/overview').then((o) => {
      setSite(o.project);
      kpi('d-dev', o.device_online + ' <small>/ ' + o.device_total + '</small>',
          o.device_online === o.device_total ? '全部在线' : '有设备掉线',
          o.device_online === o.device_total ? 'ok' : 'warn');
      kpi('d-backlog', String(o.backlog_total),
          o.backlog_total ? '恢复联网后会自动补传' : '没有欠账', o.backlog_total ? 'warn' : 'ok');
      if (o.backlog_total) {
        banner('设备侧仍有 ' + o.backlog_total + ' 条未上传，平台曲线会出现空洞 —— '
               + '这是真实缺口，不是丢包', false);
      } else {
        banner('');
      }
    });
  }

  function loadDevices() {
    return api('/api/devices').then((d) => {
      rows('devices', d.devices.map((x) => '<tr>'
        + '<td>' + esc(x.device_name || x.device_code) + '<br>'
        + '<span class="note">' + esc(x.device_code) + ' · ' + esc(x.model || '') + '</span></td>'
        + '<td><span class="tag ' + esc(x.live_state) + '">'
        + '<span class="dot ' + esc(x.live_state) + '"></span> ' + esc(x.state_label)
        + '</span></td>'
        + '<td>' + esc(x.last_seen_local) + '</td>'
        + '<td>' + esc(x.link_state || '—') + '</td>'
        + '<td class="num">' + (x.backlog || 0) + '</td>'
        + '<td>' + esc(x.site_name || '—') + '</td>'
        + '<td class="note">' + esc(x.note || '') + '</td></tr>').join(''),
        7, '还没有设备接入');
    });
  }

  function loadBatches() {
    return api('/api/batches?limit=60').then((b) => {
      $('b-stat').textContent = b.count + ' 批';
      let acc = 0, dup = 0, rej = 0;
      (b.batches || []).forEach((x) => {
        acc += x.accepted || 0; dup += x.duplicated || 0; rej += x.rejected || 0;
      });
      kpi('d-batch', b.count + ' <small>批</small>',
          '接受 ' + acc + ' · 重复 ' + dup + ' · 拒收 ' + rej, 'accent');
      rows('batches', (b.batches || []).map((x) => '<tr>'
        + '<td>' + esc(x.ts_server_local) + '</td>'
        + '<td class="note" style="font-family:var(--mono)">'
        + esc(String(x.batch_id).slice(-16)) + '</td>'
        + '<td>' + esc(kindLabel(x.kind)) + '</td>'
        + '<td class="num">' + (x.record_count || 0) + '</td>'
        + '<td class="num">' + (x.accepted || 0) + '</td>'
        + '<td class="num">' + (x.duplicated ? '<span class="tag dim">' + x.duplicated + '</span>' : 0) + '</td>'
        + '<td class="num">' + (x.rejected ? '<span class="tag rejected">' + x.rejected + '</span>' : 0) + '</td>'
        + '</tr>').join(''), 7, '还没有收到上传批次');
    });
  }

  function loadRejects() {
    return api('/api/rejects?limit=100').then((r) => {
      const by = r.by_reason || {};
      const brief = Object.keys(by).map((k) => k + ' ' + by[k]).join(' · ');
      $('r-stat').textContent = r.count ? brief : '';
      kpi('d-reject', String(r.count), r.count ? brief : '没有任何记录被拒收', r.count ? 'bad' : 'ok');
      rows('rejects', (r.rejects || []).map((x) => '<tr>'
        + '<td>' + esc(x.ts_server_local) + '</td>'
        + '<td class="note" style="font-family:var(--mono)">' + esc(x.record_id || '—') + '</td>'
        + '<td><span class="tag rejected">' + esc(x.reason_label || x.reason_code) + '</span></td>'
        + '<td class="note">' + esc(x.note || '') + '</td></tr>').join(''),
        4, '没有拒收记录 —— 所有上送数据都通过了校验');
    });
  }

  function loadAudit() {
    return api('/api/audit?limit=80').then((a) => {
      $('a-stat').textContent = a.count + ' 条';
      rows('audit', (a.events || []).map((x) => '<tr>'
        + '<td>' + esc(cst(x.ts_device)) + '</td>'
        + '<td>' + esc(x.ts_server_local) + '</td>'
        + '<td>' + esc(x.kind_label || x.kind) + '</td>'
        + '<td class="note">' + esc(JSON.stringify(x.payload || {})) + '</td></tr>').join(''),
        4, '还没有操作事件');
    });
  }

  function tick() {
    clockTick();
    [loadHead, loadDevices, loadBatches, loadRejects, loadAudit].forEach((f) => {
      f().catch(() => {});
    });
  }
  tick();
  setInterval(tick, DEVICES_POLL);
}

function kindLabel(kind) {
  return { frame: '数据帧', revision: '数据修订', audit: '操作事件' }[kind] || kind || '—';
}

/* ===================== 分派 ===================== */
const PAGES = { overview: initOverview, history: initHistory, devices: initDevices };
(window.PAGES_INIT = PAGES[PAGE] || function () {})();
