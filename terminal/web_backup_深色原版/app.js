/* 车载终端界面。原生 JS，无构建步骤（本机没有 Node，也不该为了一个演示页引入）。
 *
 * 三个页面共用一个文件，靠 <body data-page="..."> 分派：
 *   index    实时监控（参数、曲线、桩号、循环、记录）
 *   usb      U 盘与修订（导出 / 校验比对 / 导入 / 修订队列）
 *   settings 设置与诊断（故障注入、桩号清单、阈值、日志）
 *
 * 顶栏和导航由 JS 挂上去，不在三个 html 里各写一遍 —— 复制三份的后果是
 * 改了一处忘了另外两处，页面上就会出现两种状态栏。
 *
 * 两条更新通道，故意分开：
 *   状态/表格  —— 1 秒轮询 /api/status，取的是采集线程写好的整份快照
 *   曲线       —— 2 秒轮询 /api/trend，从本地库按时间升序取点
 * 曲线慢一拍是有意的：它读的是**已落盘的原始记录**，不是内存里的当前值。
 * 屏幕上的数在跳、曲线比它晚两秒，这正是"先落盘再上传"该有的样子。
 */
'use strict';

const $ = (id) => document.getElementById(id);

/* ── 本地服务看门狗 ────────────────────────────────────────────────────
 * 这些页面是本地 webui 发出来的，但发完就不管了：用户把那个黑窗口关了
 * （或者演示压根没起），浏览器里这一页还留着，看上去一切正常。
 * 这时候点"模拟断网"不会有任何反应 —— fetch 直接 reject，被轮询的
 * `.catch` 吃掉，现场只会得出"按钮坏了"这个结论。
 * 所以这里把"连不上本地服务"变成屏幕上一句看得见的话。
 */
const SVC = { fails: 0, down: false, stickyUntil: 0 };
const SVC_DOWN_AFTER = 2;      // 1 秒轮询连丢两次，就不再等了

const SVC_HINT = '本地服务没有响应 —— 那个黑窗口是不是被关了？'
  + '重新双击「启动演示.bat」，等它打出"演示环境已就绪"再刷新本页。';

function svcDown() {
  SVC.down = true;
  const b = $('banner');
  if (!b) return;
  b.textContent = SVC_HINT;
  b.className = 'banner bad';
  b.classList.remove('hidden');
}

function api(path, opts) {
  // 计数点放在**解析出 JSON 之后**，不是 fetch 之后：连不上会 reject，
  // 端口被别的程序占着则会答一页 HTML 让 r.json() 抛错 —— 对页面来说
  // 这两种都是"刷不出数据"，分开对待只会让后一种继续静默。
  return fetch(path, opts).then((r) => r.json()).then(
    (data) => {
      // 恢复了：先藏横幅，紧接着的 renderTop 会按真实状态重画一遍。
      if (SVC.down) {
        SVC.down = false;
        const b = $('banner');
        if (b) b.classList.add('hidden');
      }
      SVC.fails = 0;
      return data;
    },
    (err) => {
      if (++SVC.fails >= SVC_DOWN_AFTER) svcDown();
      throw err;
    });
}
function post(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

/** 按钮点击的失败处理：**不能静默**。
 *
 * 轮询静默是对的 —— 下一秒还会再试，每次都弹窗只会打断演示。但用户刚点
 * 了一下却什么反馈都没有，他只会认为按钮是坏的，然后来问"为什么没反应"。
 *
 * 横幅借的是顶栏那一条（用户已经认识它），只是先占住 4 秒：否则下一轮
 * 1 秒的 /api/status 立刻就把用户刚看到的原因擦掉了。
 */
/** 把异常翻译成现场看得懂的一句话。
 *
 * fetch 在网络层失败时抛的是 TypeError，Chrome 下 message 就是 "Failed to fetch" ——
 * 这句英文摆到界面上等于没说。SVC.down 是轮询已经确认过掉线，两件事是一回事。 */
function errText(err) {
  const dead = SVC.down || (err && err.name === 'TypeError');
  return dead ? SVC_HINT : String((err && err.message) || err || '本地服务没有响应');
}

function clickFailed(what) {
  return (err) => {
    SVC.stickyUntil = Date.now() + 4000;
    const b = $('banner');
    if (!b) return;
    b.textContent = what + '失败：' + errText(err);
    b.className = 'banner bad';
    b.classList.remove('hidden');
  };
}
function esc(s) {
  return String(s === null || s === undefined ? '' : s).replace(/[&<>"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function clockOf(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return String(iso).slice(11, 19);
  // 后端给的是 UTC ISO；设备时间按东八区显示
  const local = new Date(d.getTime() + (480 + d.getTimezoneOffset()) * 60000);
  return local.toTimeString().slice(0, 8);
}
/** 后端给的多半已经是本地时间字符串了，原样取时分秒就行。 */
function hhmmss(s) {
  if (!s) return '—';
  return String(s).slice(11, 19);
}
function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;      // 顶栏元素缺了就跳过，不让整页崩
}

/* 上传状态文案。值域见 terminal/uploader.py 的 _set_last 与
 * store.log_upload —— 界面不自己发明状态名，也不猜哪个算成功。 */
const UP_LABEL = {
  idle: '待机', sending: '发送中', ok: '成功', error: '本地错误',
  retry: '重试中', rejected: '被拒收', offline: '链路离线',
  acked: '已确认', partial: '部分接收', lost: '无回执', network_error: '网络失败',
};

/* ===================== 顶栏与导航 ===================== */
const NAV = [
  ['index', 'index.html', '实时监控'],
  ['usb', 'usb.html', 'U 盘与修订'],
  ['settings', 'settings.html', '设置与诊断'],
];

function mountChrome(page) {
  const links = NAV.map(([p, href, label]) =>
    '<a href="' + href + '"' + (p === page ? ' class="on"' : '') + '>' + label + '</a>'
  ).join('');
  $('chrome').innerHTML = ''
    + '<div class="head">'
    + '  <div class="topbar">'
    + '    <div class="brand">湿喷台车车载终端<small id="devname"></small></div>'
    + '    <div class="chip pile" title="当前作业桩号">桩号 <b id="pile">—</b></div>'
    + '    <div class="chip" id="phasechip">工况 <b id="phase">—</b></div>'
    + '    <div class="chip"><span class="dot" id="linkdot"></span><span id="linktext">链路检测中</span></div>'
    + '    <div class="chip">未上传 <b id="backlog">0</b> 条</div>'
    + '    <div class="chip" id="lastup">尚未上传</div>'
    + '    <div class="chip">已采 <b id="frames">0</b> 帧</div>'
    + '    <div class="spacer"></div>'
    + '    <div class="clock" id="clock">--:--:--</div>'
    + '  </div>'
    + '  <nav class="nav">' + links + '</nav>'
    + '</div>'
    + '<div class="banner hidden" id="banner"></div>';
}

function renderTop(s) {
  const live = s.live || {};
  setText('devname', (s.device && s.device.device_name) || '');
  setText('pile', live.pile_id || '未选择');
  setText('phase', live.phase_label || '—');
  const pc = $('phasechip');
  if (pc) pc.style.display = live.phase_label ? '' : 'none';

  const link = s.link || {};
  const dot = $('linkdot');
  if (dot) dot.className = 'dot ' + (link.state || 'offline');
  const q = s.queue || {};
  let txt = (link.state_label || '未知') + '（探测 ' + (link.oks || 0) + ' 次成功）';
  if (link.manual_offline) txt = '模拟断网中';
  setText('linktext', txt);

  const bl = $('backlog');
  if (bl) {
    bl.textContent = q.backlog || 0;
    bl.style.color = (q.backlog || 0) > 0 ? '#ffb454' : '';
  }

  const up = s.upload || {};
  if (up.at) {
    const kind = up.kind === 'revision' ? '修订' : (up.kind === 'audit' ? '审计' : '数据');
    let t = '最近' + kind + ' ' + (up.count || 0) + ' 条 · ' + clockOf(up.at)
      + ' · ' + (UP_LABEL[up.status] || up.status);
    if (up.message) t += ' · ' + up.message;
    setText('lastup', t);
  } else {
    setText('lastup', '尚未上传');
  }

  setText('frames', live.frames || 0);
  setText('clock', s.clock || '');
  setText('sampleinfo',
    '运行 ' + (s.uptime_label || '0 秒') + ' · 采样 ' + (s.sample_interval_s || 2) + ' 秒/帧');

  // 刚点过按钮、那一下还失败了 —— 这条消息比"数据源正常"要紧，让它先留几秒
  if (Date.now() < SVC.stickyUntil) return;

  // 横幅：把"为什么没数据"直接写出来
  const banner = $('banner');
  let msg = '', bad = false;
  if (s.source_error) { msg = '数据源不可用：' + s.source_error; bad = true; }
  else if (!live.ready) { msg = live.message || '正在启动…'; }
  else if (live.message) { msg = live.message; bad = true; }
  if (msg) {
    banner.textContent = msg;
    banner.className = 'banner' + (bad ? ' bad' : '');
    banner.classList.remove('hidden');
  } else {
    banner.classList.add('hidden');
  }
}

/** 只刷顶栏。U 盘页和设置页用它，1 秒一次，别把整页重画。 */
function tickTop() {
  return api('/api/status').then(renderTop).catch(() => {});
}

/* ==========================================================================
 * 页 1：实时监控
 * ========================================================================== */
function initIndex() {
  const state = { param: 'pump_pressure', offline: false, renderedPiles: false };

  /* ---- 参数表 ---- */
  function renderParams(live) {
    const box = $('params');
    const groups = live.groups || [];
    if (!groups.length) {
      box.innerHTML = '<div class="note">' + esc(live.message || '等待第一帧数据…') + '</div>';
      return;
    }
    let html = '';
    groups.forEach((g) => {
      html += '<div class="grp"><div class="gt">' + esc(g.name) + '</div>';
      g.params.forEach((p) => {
        const cls = ['prow'];
        if (p.key === state.param) cls.push('sel');
        if (p.over) cls.push('over');
        if (!p.active) cls.push('idle');
        if (p.is_state) cls.push('state');
        // 停机时参数值仍然显示（是 0 就是 0），只是变灰 ——
        // 藏起来的话，客户会以为界面丢了参数
        const vcls = (p.is_state && p.value >= 0.5) ? 'pv on' : 'pv';
        html += '<div class="' + cls.join(' ') + '" data-k="' + p.key + '" title="'
          + esc(p.name + '　正常范围 ' + p.lo + ' ~ ' + p.hi + (p.unit ? ' ' + p.unit : '')) + '">'
          + '<span class="pn">' + esc(p.name) + '</span>'
          + '<span class="' + vcls + '">' + esc(p.display) + '</span>'
          + '<span class="pu">' + esc(p.unit) + '</span></div>';
      });
      html += '</div>';
    });
    box.innerHTML = html;
    box.querySelectorAll('.prow').forEach((el) => {
      el.onclick = () => { state.param = el.dataset.k; renderParams(live); drawChart(); };
    });
  }

  /* ---- 曲线 ---- */
  let lastTrend = null;

  function drawChart() {
    const cv = $('chart');
    const ctx = cv.getContext('2d');
    const w = cv.width, h = cv.height, padL = 52, padR = 12, padT = 12, padB = 22;
    ctx.clearRect(0, 0, w, h);

    const t = lastTrend;
    if (!t || !t.points || t.points.length < 2) {
      ctx.fillStyle = '#8ba0bd';
      ctx.font = '13px "Microsoft YaHei"';
      ctx.fillText('等待数据（每 2 秒采一帧）', padL, h / 2);
      return;
    }
    const vals = t.points.map((p) => p.value);
    let lo = Math.min.apply(null, vals.concat([t.lo]));
    let hi = Math.max.apply(null, vals.concat([t.hi]));
    const span = (hi - lo) || 1;
    lo -= span * 0.12; hi += span * 0.12;

    const X = (i) => padL + (w - padL - padR) * i / (t.points.length - 1);
    const Y = (v) => padT + (h - padT - padB) * (1 - (v - lo) / (hi - lo));

    // 网格与刻度
    ctx.strokeStyle = '#1f2937'; ctx.fillStyle = '#8ba0bd';
    ctx.font = '11px ' + getComputedStyle(document.body).getPropertyValue('--mono');
    ctx.lineWidth = 1;
    for (let k = 0; k <= 4; k++) {
      const v = lo + (hi - lo) * k / 4, y = Y(v);
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
      ctx.fillText(v.toFixed(t.decimals), 6, y + 4);
    }

    // 上下限（注册表里的正常范围，不是随便画的线）
    const band = (v, color) => {
      const y = Y(v);
      ctx.save(); ctx.setLineDash([5, 4]); ctx.strokeStyle = color; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
      ctx.restore();
    };
    band(t.hi, '#ff5f6d');
    band(t.lo, '#ffb454');

    // 折线：未上传的点用虚线画，一眼看出"这段还在本地"
    ctx.lineWidth = 1.8;
    for (let i = 1; i < t.points.length; i++) {
      const a = t.points[i - 1], b = t.points[i];
      ctx.beginPath();
      ctx.setLineDash(b.status === 'uploaded' ? [] : [4, 3]);
      ctx.strokeStyle = b.status === 'uploaded' ? '#33b1ff' : '#7a6bd6';
      ctx.moveTo(X(i - 1), Y(a.value)); ctx.lineTo(X(i), Y(b.value));
      ctx.stroke();
    }
    ctx.setLineDash([]);

    // 最新点
    const last = t.points[t.points.length - 1];
    ctx.fillStyle = '#33b1ff';
    ctx.beginPath(); ctx.arc(X(t.points.length - 1), Y(last.value), 3.2, 0, 6.3); ctx.fill();

    $('charttitle').textContent = t.name + (t.unit ? '（' + t.unit + '）' : '');
    const up = t.points.filter((p) => p.status === 'uploaded').length;
    $('chartstat').textContent = t.points.length + ' 点 · 已上传 ' + up
      + ' · 最新 ' + last.value.toFixed(t.decimals) + ' @ ' + last.clock;
  }

  function drawChartLoop() {
    api('/api/trend?param=' + encodeURIComponent(state.param) + '&limit=90')
      .then((t) => { lastTrend = t; drawChart(); })
      .catch(() => {});
  }

  /* ---- 桩号清单 ---- */
  function renderPiles(list, current, version) {
    state.renderedPiles = true;
    $('pilever').textContent = version ? '清单 ' + list.length + ' 个' : '';
    const box = $('pilelist');
    box.innerHTML = list.map((p) => {
      const mi = p.mileage_m ? (p.mileage_m / 1000).toFixed(3) + ' km' : '';
      return '<div class="pile' + (p.pile_id === current ? ' cur' : '')
        + '" data-p="' + esc(p.pile_id) + '">'
        + '<span>' + esc(p.pile_id) + '</span><span class="mi">' + mi + '</span></div>';
    }).join('') || '<div class="note">平台尚未下发桩号清单</div>';
    box.querySelectorAll('.pile').forEach((el) => {
      el.onclick = () => post('/api/pile', { pile_id: el.dataset.p })
        .then(() => refresh())
        .catch(clickFailed('切换桩号'));
    });
    const cur = box.querySelector('.pile.cur');
    if (cur) cur.scrollIntoView({ block: 'nearest' });
  }

  /* ---- 循环 ---- */
  function renderCycles(live, cycles, cur) {
    if (cur) {
      // 方量是 pump_volume 计数器的差值，不是按时间估的 —— 屏幕上这个数
      // 和循环结束后写进表里的那个数，用的是同一个算法。
      const pv = (live.values || {}).pump_volume;
      const vol = (pv === undefined || pv === null)
        ? null : Math.max(0, pv - (cur.start_volume || 0));
      $('cycnow').innerHTML = '本轮喷浆中：<b>' + esc(cur.pile_id || '—') + '</b>　已喷 '
        + (vol === null ? '—' : vol.toFixed(2) + ' m³')
        + '　峰值 ' + (cur.max_pressure || 0).toFixed(1) + ' MPa';
    } else {
      $('cycnow').textContent = live.phase_label
        ? '当前' + live.phase_label + '，未在喷浆'
        : '尚未开始';
    }
    $('cycstat').textContent = (cycles || []).length ? '最近 ' + cycles.length + ' 轮' : '';
    $('cyclist').innerHTML = (cycles || []).map((c) =>
      '<tr><td>' + esc(c.pile_id || '—') + '</td>'
      + '<td>' + esc(clockOf(c.started_at)) + '</td>'
      + '<td class="num">' + (c.volume_m3 || 0).toFixed(2) + ' m³</td>'
      + '<td class="num">' + Math.round((c.duration_s || 0) / 60) + ' 分</td></tr>'
    ).join('') || '<tr><td colspan="4" class="muted">还没有完成的循环</td></tr>';
  }

  /* ---- 底部两张表 ---- */
  function renderRecords(rows) {
    $('reclist').innerHTML = (rows || []).map((r) => {
      const d = r.display || {};
      return '<tr><td>' + esc(clockOf(r.ts_device)) + '</td>'
        + '<td>' + esc(r.pile_id) + '</td>'
        + '<td class="num">' + esc(d.pump_pressure || '') + '</td>'
        + '<td class="num">' + esc(d.accel_flow || '') + '</td>'
        + '<td class="num">' + esc(d.hyd_oil_temp || '') + '</td>'
        + '<td><span class="tag ' + esc(r.status) + '">' + esc(r.status_label) + '</span></td></tr>';
    }).join('') || '<tr><td colspan="6" class="muted">暂无记录</td></tr>';
  }

  function renderUploads(rows) {
    $('uploadinfo').textContent = (rows || []).length ? '最近 ' + rows.length + ' 批' : '';
    $('uplist').innerHTML = (rows || []).map((u) => {
      const bits = [];
      if (u.accepted) bits.push('接受 ' + u.accepted);
      if (u.duplicated) bits.push('重复 ' + u.duplicated);
      if (u.rejected) bits.push('拒收 ' + u.rejected);
      const kind = u.kind === 'revision' ? '修订' : (u.kind === 'audit' ? '审计' : '数据');
      return '<tr><td>' + esc(clockOf(u.ts)) + '</td>'
        + '<td>' + esc(kind) + '</td>'
        + '<td title="' + esc(u.batch_id) + '">' + esc(String(u.batch_id).slice(-10)) + '</td>'
        + '<td class="num">' + (u.record_count || 0) + '</td>'
        + '<td><span class="tag ' + esc(u.status) + '">'
        + esc(UP_LABEL[u.status] || u.status) + '</span></td>'
        + '<td class="num">' + (u.elapsed_ms ? u.elapsed_ms + ' ms' : '') + '</td>'
        + '<td>' + esc(u.message || bits.join('，')) + '</td></tr>';
    }).join('') || '<tr><td colspan="7" class="muted">还没有上传过</td></tr>';
  }

  /* ---- 主循环 ---- */
  function refresh() {
    return api('/api/status').then((s) => {
      renderTop(s);
      renderParams(s.live || {});
      renderCycles(s.live || {}, s.cycles, s.cycle);
      const q = s.queue || {};
      let qi = '共 ' + (q.total || 0) + ' 帧 · 已传 ' + (q.uploaded || 0);
      if (q.backlog) qi += ' · 欠传 ' + q.backlog;
      if (q.rejected) qi += ' · 拒收 ' + q.rejected;
      if (q.oldest_pending) qi += ' · 最早欠传 ' + String(q.oldest_pending).slice(5, 16).replace('T', ' ');
      $('locstatinfo').textContent = qi;
      const st = s.storage || {};
      $('storage').textContent = (st.files || 0) + ' 个 CSV · ' + (st.mb || 0) + ' MB · 最早 '
        + (st.oldest_day || '—') + ' · 当前文件 ' + (st.current_file || '—');
      state.offline = !!(s.link && s.link.manual_offline);
      const b = $('btn-offline');
      b.textContent = state.offline ? '恢复联网' : '模拟断网';
      b.className = state.offline ? 'on' : 'danger';
      if (!state.renderedPiles || (s.piles && s.piles.ready)) {
        api('/api/piles').then((p) => renderPiles(p.piles, p.current, p.version));
      }
      return Promise.all([api('/api/records?limit=40'), api('/api/uploads?limit=40')]);
    }).then(([recs, ups]) => {
      renderRecords(recs.records);
      renderUploads(ups.uploads);
    }).catch(() => { /* 界面刷不动不该弹窗打断演示，下个周期再试 */ });
  }

  $('btn-offline').onclick = () =>
    post('/api/link', { offline: !state.offline }).then(refresh)
      .catch(clickFailed(state.offline ? '恢复联网' : '模拟断网'));
  $('btn-upload').onclick = () =>
    post('/api/upload-now', {}).then(refresh).catch(clickFailed('手动上传'));

  refresh();
  setInterval(refresh, 1000);
  drawChartLoop();
  setInterval(drawChartLoop, 2000);
}

/* ==========================================================================
 * 页 2：U 盘与修订
 *
 * 这条路存在的理由只有两个，界面上要能看出来：
 *   1) 现场没网：数据装在 U 盘里拿回去，插到有网的机器上补传；
 *   2) 数据被改过：谁改的、改成什么、原来是多少，平台全留痕。
 * 所以页面的顺序就是这条路的顺序：导出 → 校验比对 → 导入 → 修订队列。
 * ========================================================================== */
function initUsb() {
  const st = { bundles: [], bundle: '', report: null, reasons: [] };

  function renderBundles(list) {
    $('bundlecount').textContent = list.length ? list.length + ' 个包' : '';
    $('bundlelist').innerHTML = (list || []).map((b) => {
      const flags = [];
      if (!b.has_manifest) flags.push('<span class="tag rejected">缺清单</span>');
      if (!b.has_data) flags.push('<span class="tag rejected">缺数据</span>');
      if (b.has_manifest && b.has_data) flags.push('<span class="tag ok">完整</span>');
      return '<tr class="pick' + (b.dir === st.bundle ? ' cur' : '')
        + '" data-d="' + esc(b.dir) + '">'
        + '<td>' + esc(String(b.exported_local || b.modified_local || '').slice(5, 16)) + '</td>'
        + '<td class="num">' + (b.record_count || 0) + '</td>'
        + '<td>' + esc(String(b.first_local || '').slice(5, 16)) + ' → '
        + esc(String(b.last_local || '').slice(5, 16)) + '</td>'
        + '<td>' + flags.join(' ') + '</td></tr>';
    }).join('') || '<tr><td colspan="4" class="muted">U 盘上还没有导出包</td></tr>';

    $('bundlelist').querySelectorAll('tr.pick').forEach((tr) => {
      tr.onclick = () => { st.bundle = tr.dataset.d; syncBundlePick(); };
    });

    const sel = $('ins-bundle');
    const keep = st.bundle;
    sel.innerHTML = (list || []).map((b) =>
      '<option value="' + esc(b.dir) + '">' + esc(b.name) + '</option>').join('');
    st.bundle = list.some((b) => b.dir === keep) ? keep : ((list[0] || {}).dir || '');
    syncBundlePick();
  }

  function syncBundlePick() {
    $('ins-bundle').value = st.bundle || '';
    $('bundlepath').textContent = st.bundle || '（未选择）';
    $('bundlelist').querySelectorAll('tr.pick').forEach((tr) => {
      tr.className = 'pick' + (tr.dataset.d === st.bundle ? ' cur' : '');
    });
    ['btn-inspect', 'btn-edit', 'btn-import'].forEach((id) => { $(id).disabled = !st.bundle; });
  }

  function loadUsb() {
    return api('/api/usb').then((d) => {
      st.reasons = d.reasons || [];
      $('usbdir').textContent = d.dir || '';
      $('imp-reason').innerHTML = st.reasons.map((r) =>
        '<option value="' + esc(r.code) + '">' + esc(r.label) + '</option>').join('');
      renderBundles(d.bundles || []);
      renderRevisions(d.revisions || [], d.revision_stat || 0);
    }).catch(() => {});
  }

  /* ---- ① 导出 ---- */
  $('btn-export').onclick = () => {
    const btn = $('btn-export');
    btn.disabled = true;
    post('/api/usb/export', {
      scope: $('exp-scope').value,
      limit: parseInt($('exp-limit').value, 10) || 2000,
    }).then((d) => {
      const r = d.result || {};
      if (!r.ok) {
        $('exp-result').innerHTML = '<div class="banner bad" style="margin:8px 0 0">'
          + esc(r.message || '导出失败') + '</div>';
      } else {
        const m = r.manifest || {};
        const f = (m.files || [{}])[0] || {};
        $('exp-result').innerHTML = '<div class="okbox">'
          + '已导出 <b>' + r.exported + '</b> 条到 <code>' + esc(r.dir) + '</code><br>'
          + '记录时间范围 ' + esc(m.first_local || '—') + ' → ' + esc(m.last_local || '—') + '<br>'
          + '文件摘要 sha256 <code>' + esc(String(f.sha256 || '').slice(0, 24)) + '…</code><br>'
          + '<span class="muted">现在可以把 U 盘拔下来拿到别的电脑上看了。'
          + '改完再插回来走第 ③ 步 —— 改动会被记成修订，原值不动。</span>'
          + '</div>';
      }
    }).catch((e) => {
      $('exp-result').innerHTML = '<div class="banner bad" style="margin:8px 0 0">' + esc(errText(e)) + '</div>';
    }).then(() => { btn.disabled = false; loadUsb(); });
  };

  /* ---- ② 校验比对 ---- */
  function renderReport(rep) {
    const c = rep.counts || {};
    if (!c.total) {
      $('ins-report').innerHTML = '<div class="banner bad" style="margin:0">'
        + esc((rep.errors || ['读不出这份包']).join('；')) + '</div>';
      return;
    }
    const bits = [
      ['共', c.total, ''],
      ['一致', c.same, 'ok'],
      ['有改动', c.changed, 'warn'],
      ['不在本机', c.unknown, 'bad'],
      ['不合法', c.invalid, 'bad'],
    ].map(([label, n, cls]) =>
      '<span class="stat ' + cls + '"><b>' + n + '</b> ' + label + '</span>').join('');

    let warn = (rep.warnings || []).map((w) =>
      '<div class="banner" style="margin:8px 0 0">' + esc(w) + '</div>').join('');
    let err = (rep.errors || []).map((e) =>
      '<div class="banner bad" style="margin:8px 0 0">' + esc(e) + '</div>').join('');

    let rows = '';
    (rep.changes || []).forEach((ch) => {
      (ch.diffs || []).forEach((d, i) => {
        rows += '<tr>'
          + (i === 0 ? '<td rowspan="' + ch.diffs.length + '">' + esc(ch.record_id) + '</td>'
            + '<td rowspan="' + ch.diffs.length + '">' + esc(ch.pile_id) + '</td>'
            + '<td rowspan="' + ch.diffs.length + '">'
            + esc(String(ch.ts_local || '').slice(5, 19)) + '</td>' : '')
          + '<td>' + esc(d.name) + '</td>'
          + '<td class="num dimv">' + esc(d.old_text) + '</td>'
          + '<td class="num newv">' + esc(d.new_text) + '</td></tr>';
      });
      if (!ch.diffs.length) return;
    });
    // 行摘要：改动过的行必然对不上。这是"被动过"最硬的证据
    const tampered = (rep.changes || []).filter((ch) => !ch.intact).length;
    if (tampered) {
      warn += '<div class="note">其中 <b>' + tampered + '</b> 行的行摘要与导出时不一致 —— '
        + '值被改过（这正是改动的样子）。摘要是<b>提示</b>，真正卡住导入的是上面那些错误。</div>';
    }

    $('ins-report').innerHTML = '<div class="stats">' + bits + '</div>' + warn + err
      + (rows ? '<div class="scroll" style="margin-top:8px"><table class="grid">'
        + '<thead><tr><th>记录</th><th>桩号</th><th>时间</th><th>参数</th>'
        + '<th class="num">原值</th><th class="num">改成</th></tr></thead>'
        + '<tbody>' + rows + '</tbody></table></div>' : '')
      + (rep.changed ? '' : '<div class="note">没有检测到改动。'
        + '点「模拟在别的电脑上改 2 行」可以演示一遍。</div>');
  }

  function inspect() {
    if (!st.bundle) return;
    $('btn-inspect').disabled = true;
    post('/api/usb/inspect', { bundle: st.bundle }).then((d) => {
      st.report = d.report;
      renderReport(d.report || {});
    }).catch((e) => {
      $('ins-report').innerHTML = '<div class="banner bad" style="margin:0">' + esc(errText(e)) + '</div>';
    }).then(() => { $('btn-inspect').disabled = false; });
  }
  $('btn-inspect').onclick = inspect;

  $('btn-edit').onclick = () => {
    $('btn-edit').disabled = true;
    post('/api/usb/edit', { bundle: st.bundle, key: $('edit-key').value, delta: 2.0, count: 2 })
      .then((d) => {
        const r = d.result || {};
        $('edit-note').textContent = '已就地改掉最后 ' + (r.rows || 0) + ' 行的'
          + (r.param_name || '') + '（真机上这一步是人拿 Excel 干的）';
      })
      .catch((e) => { $('edit-note').textContent = '演示改动失败：' + errText(e); })
      .then(() => { $('btn-edit').disabled = false; inspect(); });
  };

  /* ---- ③ 导入 ---- */
  $('btn-import').onclick = () => {
    if (!st.bundle) return;
    const op = $('imp-operator').value.trim();
    if (!op) {
      // 这一条不能只靠后端拦：现场最容易漏的就是"谁改的"，先在界面上说清楚
      $('imp-result').innerHTML = '<div class="banner bad" style="margin:8px 0 0">'
        + '请先填写操作人 —— 修订是要追到人的。</div>';
      $('imp-operator').focus();
      return;
    }
    if (!confirm('把这份包里的改动导入为修订？\n\n'
      + '原值不会被覆盖，平台会同时显示原值、新值和这次修订的经手人。')) return;
    const btn = $('btn-import');
    btn.disabled = true;
    post('/api/usb/import', {
      bundle: st.bundle, operator: op,
      reason_code: $('imp-reason').value, note: $('imp-note').value,
    }).then((d) => {
      const r = d.result || {};
      const cls = r.ok ? 'okbox' : 'banner bad';
      const inner = r.ok
        ? '已生成 <b>' + (r.created || 0) + '</b> 条修订，排队等上传。<br>'
          + '<span class="muted">修订批要等欠传的数据先传完再发 —— '
          + '目标记录还没上平台的话，平台会整批拒收。</span>'
        : esc((r.errors || [r.message || '导入失败']).join('；'));
      $('imp-result').innerHTML = '<div class="' + cls + '" style="margin:8px 0 0">' + inner + '</div>';
    }).catch((e) => {
      $('imp-result').innerHTML = '<div class="banner bad" style="margin:8px 0 0">' + esc(errText(e)) + '</div>';
    }).then(() => { btn.disabled = false; loadUsb(); });
  };

  /* ---- ④ 修订队列 ---- */
  function renderRevisions(rows, pending) {
    $('revstat').textContent = pending ? pending + ' 条待上传' : '全部已上传';
    $('revlist').innerHTML = (rows || []).map((r) => {
      let patch = r.patch_json || '{}';
      try {
        const o = JSON.parse(patch);
        patch = Object.keys(o).map((k) => k + '=' + o[k]).join(' ');
      } catch (e) { /* 解析不了就原样显示，至少别把行吞掉 */ }
      return '<tr>'
        + '<td>' + esc(String(r.created_at || '').slice(5, 19).replace('T', ' ')) + '</td>'
        + '<td' + (r.last_error ? ' title="' + esc(r.last_error) + '"' : '') + '>'
        + esc(r.operator_name) + '</td>'
        + '<td>' + esc(r.reason_code) + '</td>'
        // 列名是 target_id（store 的行形状）；协议里那一位叫 target_record_id，
        // 改名发生在 uploader 组批的时候，界面不去替它改
        + '<td title="' + esc(r.target_id) + '">' + esc(patch) + '</td>'
        + '<td><span class="tag ' + esc(r.status) + '">'
        + esc(UP_LABEL[r.status] || r.status) + '</span></td></tr>';
    }).join('') || '<tr><td colspan="5" class="muted">还没有修订记录</td></tr>';
  }

  loadUsb();
  tickTop();
  setInterval(tickTop, 1000);
  setInterval(loadUsb, 4000);
}

/* ==========================================================================
 * 页 3：设置与诊断
 * ========================================================================== */
function initSettings() {
  let presets = [];

  /* ---- 工点与设备（现场录入） ----
   *
   * 两步走是刻意的：先"请平台核对"，通过了才允许"保存并生效"。
   * 中间只要有人动过任何一个输入框，核对结果立刻作废、保存按钮变灰 ——
   * 否则就会出现"核对了 A、保存了 B"这种从界面上看不出来的事。
   *
   * 生效这一步的失败原因必须**原话显示**：平台说"没有这个工点"和
   * "连不上平台"是两回事，现场要做的事也不同。
   */
  let verified = null;                    // 核对通过的那一对值
  let idPrefilled = false;                // 输入框只预填一次，见 loadDevice

  function idInputs() {
    return { site_id: $('id-site').value.trim(),
             device_code: $('id-device').value.trim() };
  }
  function idLine(icon, color, text) {
    return '<div style="color:var(--' + color + ')">' + icon + ' ' + esc(text) + '</div>';
  }
  function idInvalidate() {
    verified = null;
    $('btn-id-apply').disabled = true;
  }
  function idShow(html) { $('id-verdict').innerHTML = html; }

  function idRender(c) {
    if (!c || !c.ok) {
      let h = idLine('✗', 'bad', (c && c.error) || '平台不接受这个工点和设备编号');
      if (c && c.offline) {
        h += '<div class="muted">核对必须联网 —— 这是设计如此：'
          + '工点能不能用由平台说了算，本地猜一个等于没有核对。</div>';
      }
      (c && c.known_sites || []).forEach((s) => {
        h += idLine('·', 'dim', s.site_id + '　' + (s.site_name || '')
          + '　' + (s.project_name || ''));
      });
      return h;
    }
    const site = c.site || {}, dev = c.device || {};
    let h = idLine('✓', 'ok', '工点：' + site.site_name + '（' + (site.line || '')
      + '）　桩号 ' + site.pile_count + ' 个　'
      + site.first_pile + ' ~ ' + site.last_pile);
    h += dev.known
      ? idLine('✓', 'ok', '设备：平台已有档案 —— ' + (dev.device_name || dev.device_code)
          + (dev.site_name ? '，原登记在「' + dev.site_name + '」' : '，此前未登记工点'))
      : idLine('⚠', 'warn', '设备：平台此前没有这个编号的档案，接入后将按自报信息登记');
    (c.warnings || []).forEach((w) => { h += idLine('⚠', 'warn', w); });
    h += '<div class="muted">核对通过。确认无误后点「保存并生效」。</div>';
    return h;
  }

  function idCheck() {
    const v = idInputs();
    if (!v.site_id || !v.device_code) {
      idInvalidate();
      return idShow('<span class="muted">工点 ID 和设备编号都要填。</span>');
    }
    $('btn-id-check').disabled = true;
    idShow('<span class="muted">正在问平台…</span>');
    return post('/api/identity/check', v).then((d) => {
      const c = d.check || {};
      idShow(idRender(c));
      verified = c.ok ? v : null;
      $('btn-id-apply').disabled = !c.ok;
    }).catch((e) => {
      idInvalidate();
      idShow(idLine('✗', 'bad', '核对失败：' + errText(e)));
    }).then(() => { $('btn-id-check').disabled = false; });
  }

  function idApply() {
    const v = idInputs();
    if (!verified || verified.site_id !== v.site_id
        || verified.device_code !== v.device_code) {
      idInvalidate();
      return idShow('<span class="muted">输入改过了，请重新核对。</span>');
    }
    $('btn-id-apply').disabled = true;
    idShow('<span class="muted">正在生效…</span>');
    return post('/api/identity/apply', v).then((d) => {
      if (d.error || !d.result) {         // 闸门没过、平台改主意了，都在这里
        idInvalidate();
        return idShow(idLine('✗', 'bad', '未生效：' + (d.error || '终端没有返回结果')));
      }
      const r = d.result, to = r.to || {}, ps = r.piles || {};
      idShow(idLine('✓', 'ok', '已生效：工点 ' + to.site_id + '（' + (to.site_name || '')
        + '）　设备 ' + to.device_code + '　当前桩号 ' + (to.pile_id || '未选'))
        + idLine('·', 'dim', '桩号清单已换成 ' + ps.count + ' 个（新增 '
          + ps.added + '、撤下 ' + ps.removed + '）')
        + '<div class="muted">留痕事件已排队上传，平台「审计」页可以看到这一条。</div>');
      idInvalidate();
      loadDevice(); loadPiles();        // 顶栏、设备信息、桩号清单立刻重画
    }).catch((e) => {
      idShow(idLine('✗', 'bad', '未生效：' + errText(e)));
      $('btn-id-apply').disabled = false;
    });
  }

  $('btn-id-check').onclick = idCheck;
  $('btn-id-apply').onclick = idApply;
  $('id-site').oninput = idInvalidate;
  $('id-device').oninput = idInvalidate;

  /* ---- 故障注入 ---- */
  function renderFaults(active) {
    $('faultbtns').innerHTML = presets.map((f) =>
      '<button class="fault ' + esc(f.level) + '" data-k="' + esc(f.key) + '" title="'
      + esc(f.detail) + '">' + esc(f.label) + '</button>').join('')
      || '<span class="muted">当前数据源不支持故障注入</span>';
    $('faultbtns').querySelectorAll('button.fault').forEach((b) => {
      b.onclick = () => {
        b.disabled = true;
        post('/api/fault', { key: b.dataset.k }).then(loadFaults)
          .catch(clickFailed('注入故障')).then(() => { b.disabled = false; });
      };
    });

    $('faultlist').innerHTML = (active || []).map((f) => {
      const total = (f.ramp_s || 0) * 2 + (f.hold_s || 0);
      const pct = total ? Math.min(100, 100 * (f.elapsed_s || 0) / total) : 0;
      return '<div class="frow"><div class="fhead">'
        + '<b>' + esc(f.label || f.key) + '</b>'
        + '<span class="muted">目标 ' + esc(f.value) + ' · 剩余 ' + (f.left_s || 0) + ' 秒</span></div>'
        + '<div class="fbar"><i style="width:' + pct.toFixed(0) + '%"></i></div></div>';
    }).join('') || '<div class="muted">当前没有注入的故障 —— 数据是模拟器正常跑的</div>';

    const btn = $('btn-clear-fault');
    btn.disabled = !(active || []).length;
    $('faultstat').textContent = (active || []).length ? (active || []).length + ' 个生效中' : '';
  }

  function loadFaults() {
    return api('/api/faults').then((d) => {
      presets = d.presets || [];
      renderFaults(d.active || []);
    }).catch(() => {});
  }

  $('btn-clear-fault').onclick = () =>
    post('/api/fault/clear', {}).then(loadFaults).catch(clickFailed('清除故障'));

  /* ---- 桩号清单 ---- */
  function loadPiles() {
    return api('/api/piles').then((p) => {
      $('pilever').textContent = p.version ? '清单版本 ' + p.version : '';
      $('setpilelist').innerHTML = (p.piles || []).map((x) =>
        '<div class="pile' + (x.pile_id === p.current ? ' cur' : '') + '" data-p="'
        + esc(x.pile_id) + '">'
        + '<span>' + esc(x.pile_id) + '</span>'
        + '<span class="mi">'
        + (x.mileage_m ? (x.mileage_m / 1000).toFixed(3) + ' km' : '')
        + (x.section ? '　' + esc(x.section) : '') + '</span></div>').join('')
        || '<div class="note">平台尚未下发桩号清单</div>';
      $('setpilelist').querySelectorAll('.pile').forEach((el) => {
        el.onclick = () => post('/api/pile', { pile_id: el.dataset.p })
          .then(loadPiles).catch(clickFailed('切换桩号'));
      });
    }).catch(() => {});
  }

  /* ---- 阈值表（只读） ---- */
  function loadParams() {
    return api('/api/params').then((d) => {
      let rows = '';
      (d.groups || []).forEach((g) => {
        rows += '<tr class="grphd"><td colspan="4">' + esc(g.name) + '</td></tr>';
        (g.params || []).forEach((p) => {
          rows += '<tr><td>' + esc(p.name) + '</td>'
            + '<td class="muted">' + esc(p.key) + '</td>'
            + '<td class="num">' + esc(p.lo) + ' ~ ' + esc(p.hi) + '</td>'
            + '<td>' + esc(p.unit || (p.is_state ? '状态量' : '')) + '</td></tr>';
        });
      });
      $('paramlist').innerHTML = rows || '<tr><td colspan="4" class="muted">读不到参数表</td></tr>';
    }).catch(() => {});
  }

  /* ---- 日志 ---- */
  function loadLogs() {
    const name = $('logfile').value || 'terminal.log';
    return api('/api/logs?name=' + encodeURIComponent(name) + '&lines=200')
      .then((d) => {
        $('logfile').innerHTML = (d.files || []).map((f) =>
          '<option' + (f === d.name ? ' selected' : '') + '>' + esc(f) + '</option>').join('');
        $('loginfo').textContent = d.count + ' 行 · ' + d.dir;
        $('logbody').textContent = (d.lines || []).join('\n') || '（空）';
        $('logbody').scrollTop = $('logbody').scrollHeight;   // 停在最新一行
      }).catch((e) => { $('loginfo').textContent = String(e); });
  }
  $('btn-log').onclick = loadLogs;
  $('logfile').onchange = loadLogs;

  /* ---- 设备信息 ---- */
  function loadDevice() {
    return api('/api/status').then((s) => {
      renderTop(s);
      const dev = s.device || {}, prj = s.project || {}, st = s.storage || {};
      const hb = s.heartbeat || {}, link = s.link || {}, q = s.queue || {};
      const idn = s.identity || {};

      // 身份卡片的"当前值"每次刷新；输入框只在第一帧预填 ——
      // 每 2 秒覆盖一次的话，人正在打字就会被擦掉。
      $('idnow').textContent = '当前：' + (idn.site_id || '—') + ' / '
        + (idn.device_code || '—')
        + (idn.pending && idn.pending.total
           ? '　欠账 ' + idn.pending.total + ' 条（换编号前要先传完）' : '');
      if (!idPrefilled) {
        idPrefilled = true;
        $('id-site').value = idn.site_id || '';
        $('id-device').value = idn.device_code || '';
      }
      const info = [
        ['设备编号', dev.device_code], ['设备名称', dev.device_name], ['型号', dev.model],
        ['工点', prj.site_name], ['项目', prj.project_name],
        ['隧道', (prj.tunnel || '') + ' ' + (prj.line || '')],
        ['当前桩号', (s.live || {}).pile_id],
        ['平台地址', (s.platform || {}).base],
        ['数据源', (s.source || {}).name],
        ['采样周期', (s.sample_interval_s || 0) + ' 秒/帧'],
        ['上传周期', (s.upload_interval_s || 0) + ' 秒/批'],
        ['心跳', hb.sent + ' 次成功 / ' + hb.fail + ' 次失败，每 ' + hb.interval_s + ' 秒'],
        ['链路', (link.state_label || '—') + '（探测 ' + (link.oks || 0) + ' 次成功）'],
        ['平台回告清单版本', (hb.advice || {}).pile_list_version || '—'],
        ['本地库', (st.files || 0) + ' 个 CSV · ' + (st.mb || 0) + ' MB · ' + (st.days || 0) + ' 天'],
        ['最早数据', st.oldest_day], ['当前文件', st.current_file],
        ['队列', '共 ' + (q.total || 0) + ' · 已传 ' + (q.uploaded || 0)
          + ' · 欠传 ' + (q.backlog || 0) + ' · 拒收 ' + (q.rejected || 0)],
      ];
      $('devinfo').innerHTML = info.map(([k, v]) =>
        '<div class="kv"><span>' + esc(k) + '</span><b>'
        + esc(v === undefined || v === null || v === '' ? '—' : v) + '</b></div>').join('');
    }).catch(() => {});
  }

  loadDevice(); loadFaults(); loadPiles(); loadParams(); loadLogs();
  setInterval(loadDevice, 2000);
  setInterval(loadFaults, 1000);
  setInterval(loadPiles, 5000);
  setInterval(loadLogs, 10000);
}

/* ==========================================================================
 * 分派
 * ========================================================================== */
const PAGE = document.body.dataset.page || 'index';
mountChrome(PAGE);
const PAGES = { index: initIndex, usb: initUsb, settings: initSettings };
(PAGES[PAGE] || function () {})();
