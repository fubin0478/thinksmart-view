// The kiosk's base page (127.0.0.1:8090). It renders what the voice client streams on /events and sends the buttons back.
// 2026-11-25 (Claude, the user's decision): 小麦 has no page of its own any more. Waking it shows the capsule and its
// cards over whatever is on screen (home_overlay.js reads #messages and the events from here); this page keeps the settings
// (with the conversation as one of its sections), the model sheet, Wi-Fi, the ringing alarm, toasts and the volume bar, and
// a base screen for when the home page cannot be reached.
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const pad = n => String(n).padStart(2, '0');
const WEEK = '日一二三四五六';
const ui = {
  status: { state: 'idle', connected: false, uploading: false },
  models: { current: null, options: [] },
  thread: { entry: null, messages: [] },
  ptt: false,
  alarms: { synced: true, items: [], missed: [], ringing: null, volume: 70, tone: 'coffee', tones: {} },
  bluetooth: null,
  airplay: null,
};

const SUGGEST = [
  ['今天天气怎么样？', 'ms', '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6L6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4L6 18M18 6l1.4-1.4"/>'],
  ['放首周杰伦的歌', 'mu', '<path d="M9 18V6l11-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>'],
  ['开始专注 25 分钟', 'fo', '<circle cx="12" cy="13.5" r="7"/><path d="M12 10v3.5l2.5 1.5M9.5 3h5"/>'],
  ['服务器状态都正常吗', 'sv', '<rect x="4" y="4" width="16" height="7" rx="2"/><rect x="4" y="13" width="16" height="7" rx="2"/><path d="M8 7.5h.01M8 16.5h.01"/>'],
];

/* ---- clock ---- */
function tick() {
  const now = new Date();
  const hm = pad(now.getHours()) + ':' + pad(now.getMinutes());
  for (const id of ['setClock', 'bigTime', 'ringTime']) if ($(id).textContent !== hm) $(id).textContent = hm;
  const day = `${now.getMonth() + 1}月${now.getDate()}日 星期${WEEK[now.getDay()]}`;
  if ($('bigDate').textContent !== day) $('bigDate').textContent = day;
}

/* ---- sound level for the capsule's wave ---- */
// The mic level (RMS) in decibels, so a quiet voice visibly moves the wave. Measured on the device
// (2026-11-01, 9 listening turns): room noise -74..-80 dBFS with rare blips to -62, normal speech -60..-42 dBFS.
// Noise stays 0, a quiet voice (-55) reaches ~30%, a normal one (-45) ~58%, a loud one (-30) 100%.
const MIC_FLOOR = -66, MIC_FULL = -30;   // dBFS
const micLevel = rms => Math.min(1, Math.max(0, (20 * Math.log10(rms + 1e-6) - MIC_FLOOR) / (MIC_FULL - MIC_FLOOR)));

/* ---- status ---- */
function modelName() {
  const cur = ui.models.current;
  return cur ? cur.label : '模型';
}
const shownState = () => (ui.ptt ? 'listening' : ui.status.state);
// the capsule and its notices (home_overlay.js) read the same data
window.__tsUi = ui;
const uiChanged = () => { try { window.dispatchEvent(new CustomEvent('ts-ui-data')); } catch (e) { /* no overlay */ } };

const SAY = { idle: '说话', connecting: '正在连接', listening: '在听', thinking: '在想', speaking: '插一句' };
function renderStatus() {
  const shown = shownState(), micOff = ui.status.mic === false;
  document.body.dataset.state = shown;
  $('sayText').textContent = ui.ptt ? '松开发送' : micOff && shown === 'idle' ? '麦克风已关' : SAY[shown] || '说话';
  $('btnMic').classList.toggle('live', shown === 'listening');
  $('btnMic').classList.toggle('off', micOff);
  renderPending();
  renderBright();
  renderScreenOff();
  renderVol();
  renderUpdate();
  uiChanged();
}

/* ---- models ---- */
const logoSrc = m => (m && m.logo ? `/static/logos/${m.logo}.svg` : '');

function renderModels() {
  const cur = ui.models.current;
  $('pillLogo').src = logoSrc(cur);
  $('pillLabel').textContent = cur ? cur.label : '选择模型';
  $('pillModel').textContent = cur ? (cur.model || cur.company || '') : '';
  $('setModelName').textContent = cur ? cur.label : '选择模型';
  const grid = $('grid');
  grid.textContent = '';
  for (const m of ui.models.options) {
    const on = cur && cur.entry === m.entry;
    const b = document.createElement('button');
    b.className = 'card' + (on ? ' on' : '');
    b.innerHTML = `<img class="tile" src="${esc(logoSrc(m))}" alt="">` + (on ? '<span class="badge">使用中</span>' : '') +
      `<span><b>${esc(m.label)}</b><small>${esc(m.model || '')}</small><small class="co">${esc(m.company || '')}</small></span>`;
    b.addEventListener('click', () => switchModel(m));
    grid.appendChild(b);
  }
  renderHead();
}

function openSheet(on) {
  $('sheet').classList.toggle('on', on);
}

// The page switches at once and the hub catches up; if it fails, back to what the client last said.
// While a switch is on its way, the client's events about the old model don't flip the page back.
const server = { models: null, thread: null };
const seenThreads = {};   // entry -> the client's messages for that model, shown at once when switching to it
let switching = null;

async function switchModel(m) {
  openSheet(false);
  if (ui.models.current && ui.models.current.entry === m.entry) return;
  switching = m.entry;
  ui.models = Object.assign({}, ui.models, { current: m });
  ui.thread = { entry: m.entry, messages: seenThreads[m.entry] || [] };
  renderModels(); renderThread(); renderStatus();
  let error = null;
  try {
    const r = await (await fetch('/api/model', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                 body: JSON.stringify({ kind: m.kind, vendor: m.vendor, entry: m.entry }) })).json();
    if (r.ok === false) error = r.error || '切换失败';
  } catch (e) {
    error = '切换失败：客户端没有响应';
  }
  if (switching !== m.entry) return;   // another switch since
  switching = null;
  if (error) {
    if (server.models) ui.models = server.models;
    if (server.thread) ui.thread = server.thread;
    renderModels(); renderThread(); renderStatus();
  }
  toast(error || `已切换到 ${m.label}`, error ? 'error' : '');
}

/* ---- orientation-aware sheet touch scroll & smart range sliders ---- */
function sheetPoint(cx, cy) {
  const root = document.documentElement;
  if (!root.classList.contains('ts-portrait')) return { x: cx, y: cy };
  if (root.classList.contains('ts-rot270')) return { x: window.innerHeight - cy, y: cx };
  return { x: cy, y: window.innerWidth - cx };
}
function sliderValueFromEvent(el, e) {
  const r = el.getBoundingClientRect(), padPx = Number(el.dataset.pad) || 24;
  const min = el.min !== '' ? Number(el.min) : 0, max = el.max !== '' ? Number(el.max) : 100;
  const root = document.documentElement;
  let f = 0;
  if (root.classList.contains('ts-portrait') && r.height > r.width) {
    const raw = root.classList.contains('ts-rot270') ? (r.bottom - e.clientY - padPx) : (e.clientY - r.top - padPx);
    f = raw / Math.max(1, r.height - padPx * 2);
  } else {
    f = (e.clientX - r.left - padPx) / Math.max(1, r.width - padPx * 2);
  }
  return Math.round(min + Math.max(0, Math.min(1, f)) * (max - min));
}
// the filled part ends at the middle of the handle (28 px wide)
function setBarPos(el, v) {
  const min = el.min !== '' ? Number(el.min) : 0, max = el.max !== '' ? Number(el.max) : 100;
  const ratio = max > min ? Math.max(0, Math.min(1, ((v != null ? v : min) - min) / (max - min))) : 0;
  el.style.setProperty('--pos', `calc(14px + (100% - 28px) * ${ratio})`);
}

const elTarget = e => { const t = e && e.target; return t && t.nodeType === 3 ? t.parentElement : t; };
let sheetPs = null, sheetPsRaf = 0, sheetMomRaf = 0, sheetClickBlockUntil = 0, activeSliderDrag = false;
function stopSheetScroll(clearActive = true) {
  if (sheetPsRaf) { cancelAnimationFrame(sheetPsRaf); sheetPsRaf = 0; }
  if (sheetMomRaf) { cancelAnimationFrame(sheetMomRaf); sheetMomRaf = 0; }
  if (clearActive) sheetPs = null;
}
function findSheetScroller(t) {
  for (let n = (t && t.nodeType === 3 ? t.parentElement : t); n && n.nodeType === 1; n = n.parentElement) {
    if (n.classList && (n.classList.contains('sheet') || n.classList.contains('app'))) break;
    const cs = getComputedStyle(n);
    if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll') && n.scrollHeight > n.clientHeight + 2) return n;
  }
  return null;
}
(function initSheetTouchScroll() {
  const flush = p => {
    if (!p || !p.active || p.goal == null) return;
    p.el.scrollTop = p.goal;
    p.goal = null;
  };
  addEventListener('pointerdown', e => {
    stopSheetScroll(true);
    sheetClickBlockUntil = 0;
    if (e.button != null && e.button > 0) return;
    const t = elTarget(e);
    if (!t || !t.closest) return;
    const sh = t.closest('.sheet.on') || (document.documentElement.classList.contains('ts-portrait') && t.closest('.app'));
    if (!sh || t.closest('input[type="text"], input[type="password"], input[type="search"], input[type="range"], .bar, textarea, input[data-rc-range]')) return;
    const sc = findSheetScroller(t);
    if (!sc) return;
    const pt = sheetPoint(e.clientX, e.clientY);
    sheetPs = { el: sc, id: e.pointerId, x0: pt.x, y0: pt.y, ly: pt.y, lt: performance.now(), st0: sc.scrollTop, active: false, v: 0, goal: null };
  }, true);
  addEventListener('pointermove', e => {
    if (activeSliderDrag) { stopSheetScroll(true); return; }
    const p = sheetPs;
    if (!p || e.pointerId !== p.id) return;
    const pt = sheetPoint(e.clientX, e.clientY);
    const dx = pt.x - p.x0, dy = pt.y - p.y0;
    if (!p.active) {
      if (Math.max(Math.abs(dx), Math.abs(dy)) < 8) return;
      if (Math.abs(dy) < Math.abs(dx) * 0.9) { sheetPs = null; return; }
      p.active = true;
      p.el.style.scrollBehavior = 'auto';
      p.x0 = pt.x; p.y0 = pt.y; p.ly = pt.y; p.lt = performance.now(); p.st0 = p.el.scrollTop;
      return;
    }
    p.goal = p.st0 - (pt.y - p.y0);
    if (!sheetPsRaf) sheetPsRaf = requestAnimationFrame(() => { sheetPsRaf = 0; if (sheetPs === p) flush(p); });
    const now = performance.now(), dt = now - p.lt;
    if (dt > 0) {
      const v = (pt.y - p.ly) / dt;
      p.v = dt > 80 ? v : p.v * 0.4 + v * 0.6;
    }
    p.ly = pt.y; p.lt = now;
    if (e.cancelable) e.preventDefault();
  }, { capture: true, passive: false });
  function endSheetScroll(e, cancelled) {
    const p = sheetPs;
    if (!p || e.pointerId !== p.id) return;
    sheetPs = null;
    if (sheetPsRaf) { cancelAnimationFrame(sheetPsRaf); sheetPsRaf = 0; }
    if (!p.active) return;
    flush(p);
    sheetClickBlockUntil = performance.now() + 360;
    if (cancelled || performance.now() - p.lt > 90) return;
    let v = Math.max(-4, Math.min(4, p.v || 0));
    if (Math.abs(v) < 0.3) return;
    const el = p.el;
    let last = performance.now();
    const step = now => {
      const dt = Math.min(34, now - last); last = now;
      v *= Math.pow(0.935, dt / 16);
      el.scrollTop -= v * dt;
      sheetMomRaf = Math.abs(v) > 0.04 ? requestAnimationFrame(step) : 0;
    };
    sheetMomRaf = requestAnimationFrame(step);
  }
  addEventListener('pointerup', e => endSheetScroll(e, false), true);
  addEventListener('pointercancel', e => endSheetScroll(e, true), true);
  addEventListener('click', e => {
    if (sheetClickBlockUntil && performance.now() < sheetClickBlockUntil) {
      e.stopPropagation();
      e.preventDefault();
    }
    sheetClickBlockUntil = 0;
  }, true);
  addEventListener('touchmove', e => {
    const t = elTarget(e);
    if (!e.cancelable || !t || !t.closest || !(t.closest('.sheet.on') || t.closest('.app'))) return;
    if (activeSliderDrag || (sheetPs && sheetPs.active)) {
      e.preventDefault();
    }
  }, { capture: true, passive: false });
})();

function bindSmartSlider(el, onScrub, onCommit) {
  if (!el) return;
  let st = null;
  function detach() {
    removeEventListener('pointermove', onMove, true);
    removeEventListener('pointerup', onUp, true);
    removeEventListener('pointercancel', onCancel, true);
    removeEventListener('blur', abortDrag);
    el.removeEventListener('lostpointercapture', onCancel);
  }
  function abortDrag() {
    if (!st) return;
    const cur = st;
    st = null;
    detach();
    activeSliderDrag = false;
    try { el.releasePointerCapture(cur.id); } catch (err) {}
    if (cur.mode === 'drag') onCommit(cur.val);
  }
  function onMove(e) {
    if (!st || e.pointerId !== st.id) return;
    if (!el.isConnected || el.disabled) { abortDrag(); return; }
    if (e.cancelable) e.preventDefault();
    if (st.sc && st.sc.scrollTop !== st.scTop) st.sc.scrollTop = st.scTop;
    const next = sliderValueFromEvent(el, e);
    if (next !== st.val) {
      st.val = next;
      onScrub(next);
    }
  }
  function finish(e, cancelled) {
    if (!st || e.pointerId !== st.id) return;
    const cur = st;
    st = null;
    detach();
    activeSliderDrag = false;
    try { el.releasePointerCapture(e.pointerId); } catch (err) {}
    const finalVal = (cancelled || !el.isConnected || el.disabled) ? cur.val : sliderValueFromEvent(el, e);
    if (finalVal !== cur.val) onScrub(finalVal);
    onCommit(finalVal);
  }
  function onUp(e) { finish(e, false); }
  function onCancel(e) { finish(e, true); }
  el.addEventListener('mousedown', e => { if (e.button === 0) e.preventDefault(); });
  el.addEventListener('pointerdown', e => {
    if (st || e.isPrimary === false || (e.button != null && e.button > 0) || el.disabled) return;
    if (e.cancelable) e.preventDefault();
    detach();
    stopSheetScroll(true);
    activeSliderDrag = true;
    const sc = findSheetScroller(el);
    const initVal = sliderValueFromEvent(el, e);
    st = { id: e.pointerId, mode: 'drag', val: initVal, sc, scTop: sc ? sc.scrollTop : 0 };
    try { el.setPointerCapture(e.pointerId); } catch (err) { /* fallback without capture */ }
    onScrub(initVal);
    addEventListener('pointermove', onMove, { capture: true, passive: false });
    addEventListener('pointerup', onUp, true);
    addEventListener('pointercancel', onCancel, true);
    addEventListener('blur', abortDrag);
    el.addEventListener('lostpointercapture', onCancel);
  });
}

/* ---- settings: unified smart brightness, theme & orientation ---- */
const dispState = { loaded: false, auto: false, level: 100, lux: null, sensor_ok: false, dimmed: false };
let brightEdit = null, brightPreviewTimer = 0, brightPreviewSent = 0, brightPreviewBusy = false, brightPendingLv = null;
let dispPollTimer = 0;

function currentThemeState() {
  if (window.__tsTheme && typeof window.__tsTheme.get === 'function') return window.__tsTheme.get();
  let mode = 'auto';
  try { if (localStorage.getItem('ts.themeMode') === 'manual') mode = 'manual'; } catch (e) {}
  return { theme: document.documentElement.classList.contains('ts-light') ? 'light' : 'dark', mode };
}
function currentOrientState() {
  if (window.__tsOrient && typeof window.__tsOrient.get === 'function') return window.__tsOrient.get();
  let mode = 'auto';
  try { const m = localStorage.getItem('ts.orientMode'); if (m === 'portrait' || m === 'landscape') mode = m; } catch (e) {}
  return { mode, sensor: null };
}
function brightSetting() {
  const baseLv = dispState.loaded ? dispState.level : ((ui.status.brightness && ui.status.brightness.level != null) ? ui.status.brightness.level : 100);
  const dimmed = dispState.loaded ? dispState.dimmed : !!(ui.status.brightness && ui.status.brightness.dimmed);
  return {
    level: brightEdit !== null ? brightEdit : baseLv,
    auto: brightEdit !== null ? false : !!dispState.auto,
    lux: dispState.lux,
    sensor_ok: !!dispState.sensor_ok,
    dimmed: brightEdit !== null ? false : dimmed,
  };
}
function renderBright() {
  const b = brightSetting();
  if (brightEdit === null) $('brightRange').value = b.level;
  setBarPos($('brightRange'), b.level);
  $('brightNum').textContent = b.level + '%';

  const sensorEl = $('brightSensor');
  if (sensorEl) {
    const luxTxt = (b.sensor_ok && typeof b.lux === 'number') ? `环境光 ${Math.round(b.lux)} lx` : '';
    sensorEl.textContent = luxTxt;
    sensorEl.classList.toggle('auto', !!b.auto);
  }
  const autoBtn = $('brightAutoBtn');
  if (autoBtn) autoBtn.disabled = dispState.loaded && !b.sensor_ok && !b.auto;
  for (const btn of document.querySelectorAll('#brightModeSeg button')) {
    btn.classList.toggle('on', (btn.dataset.brightMode === 'auto') === !!b.auto);
  }
  for (const btn of document.querySelectorAll('#brightPresets button')) {
    const lv = Number(btn.dataset.lv);
    btn.classList.toggle('on', !b.auto && Math.abs(b.level - lv) <= 2);
  }
  const th = currentThemeState();
  const activeThemeBtn = th.mode === 'auto' ? 'auto' : th.theme;
  for (const btn of document.querySelectorAll('#themeSeg button')) {
    btn.classList.toggle('on', btn.dataset.themeBtn === activeThemeBtn);
  }
  const orSt = currentOrientState();
  for (const btn of document.querySelectorAll('#orientSeg button')) {
    btn.classList.toggle('on', btn.dataset.orientBtn === (orSt.mode || 'auto'));
  }

  $('brightNote').textContent = b.auto ? '正在跟着环境光自动调；拖动滑条就改回手动。' : '';
}

async function pollDisplayState() {
  clearTimeout(dispPollTimer); dispPollTimer = 0;
  if (!$('settings').classList.contains('on') || document.hidden) return;
  try {
    const r = await fetch('/api/display', { cache: 'no-store' });
    if (r.ok) {
      const j = await r.json();
      Object.assign(dispState, {
        loaded: true,
        auto: !!j.auto,
        level: typeof j.level === 'number' ? j.level : dispState.level,
        lux: typeof j.lux === 'number' ? j.lux : null,
        sensor_ok: !!j.sensor_ok,
        dimmed: !!j.dimmed,
      });
      if (brightEdit === null) renderBright();
    }
  } catch (e) {}
  if ($('settings').classList.contains('on') && !document.hidden) {
    dispPollTimer = setTimeout(pollDisplayState, 2200);
  }
}

function sendBrightPreview(lv) {
  brightPendingLv = lv;
  const fire = async () => {
    if (brightPreviewBusy || brightPendingLv === null) return;
    const v = brightPendingLv;
    brightPendingLv = null;
    brightPreviewBusy = true;
    brightPreviewSent = Date.now();
    try {
      await fetch('/api/display/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ level: v }),
      });
    } catch (e) {} finally {
      brightPreviewBusy = false;
      if (brightPendingLv !== null) sendBrightPreview(brightPendingLv);
    }
  };
  clearTimeout(brightPreviewTimer);
  const wait = Math.max(0, 40 - (Date.now() - brightPreviewSent));
  if (wait === 0 && !brightPreviewBusy) fire();
  else brightPreviewTimer = setTimeout(fire, wait || 40);
}

async function sendDisplayChange(body) {
  try {
    const resp = await fetch('/api/display', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (resp.status === 404 && 'level' in body) {
      const fb = await (await fetch('/api/brightness', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ level: body.level }),
      })).json();
      if (fb.ok === false) toast(fb.error || '没保存上', 'error');
      return fb;
    }
    const r = await resp.json();
    if (r.ok === false) {
      toast(r.error || '没保存上', 'error');
      return r;
    }
    Object.assign(dispState, {
      loaded: true,
      auto: !!r.auto,
      level: typeof r.level === 'number' ? r.level : dispState.level,
      lux: typeof r.lux === 'number' ? r.lux : dispState.lux,
      sensor_ok: 'sensor_ok' in r ? !!r.sensor_ok : dispState.sensor_ok,
      dimmed: !!r.dimmed,
    });
    return r;
  } catch (e) {
    toast('没保存上：客户端没有响应', 'error');
    return { ok: false };
  }
}

bindSmartSlider($('brightRange'), v => {
  brightEdit = v;
  $('brightRange').value = v;
  renderBright();
  sendBrightPreview(v);
}, v => {
  clearTimeout(brightPreviewTimer);
  brightPendingLv = null;
  brightEdit = v;
  $('brightRange').value = v;
  renderBright();
  sendDisplayChange({ level: v }).then(() => {
    if (brightEdit === v) brightEdit = null;
    renderStatus();
  });
});

$('brightModeSeg').addEventListener('click', e => {
  const t = elTarget(e), b = t && t.closest && t.closest('button[data-bright-mode]');
  if (!b || b.disabled) return;
  const wantAuto = b.dataset.brightMode === 'auto';
  brightEdit = null;
  sendDisplayChange({ auto: wantAuto }).then(() => renderBright());
});
$('brightPresets').addEventListener('click', e => {
  const t = elTarget(e), b = t && t.closest && t.closest('button[data-lv]');
  if (!b) return;
  const lv = Number(b.dataset.lv);
  brightEdit = lv;
  $('brightRange').value = lv;
  renderBright();
  sendDisplayChange({ level: lv }).then(() => {
    if (brightEdit === lv) brightEdit = null;
    renderStatus();
  });
});
$('themeSeg').addEventListener('click', e => {
  const t = elTarget(e), b = t && t.closest && t.closest('button[data-theme-btn]');
  if (!b) return;
  const v = b.dataset.themeBtn;
  if (window.__tsTheme && typeof window.__tsTheme.set === 'function') {
    window.__tsTheme.set(v);
  } else {
    const mode = v === 'auto' ? 'auto' : 'manual';
    try { localStorage.setItem('ts.themeMode', mode); } catch (err) {}
    const h = new Date().getHours();
    const isLight = v === 'light' || (v === 'auto' && h >= 7 && h < 18);
    document.documentElement.classList.toggle('ts-light', isLight);
    try { localStorage.setItem('ts.theme', isLight ? 'light' : 'dark'); } catch (err) {}
  }
  renderBright();
});
$('orientSeg').addEventListener('click', e => {
  const t = elTarget(e), b = t && t.closest && t.closest('button[data-orient-btn]');
  if (!b) return;
  const m = b.dataset.orientBtn;
  if (window.__tsOrient && typeof window.__tsOrient.setMode === 'function') {
    window.__tsOrient.setMode(m);
  } else {
    try { localStorage.setItem('ts.orientMode', m); } catch (err) {}
    document.documentElement.classList.toggle('ts-portrait', m === 'portrait');
  }
  renderBright();
});
addEventListener('ts-ui-sync', renderBright);

/* ---- settings: auto screen-off (the client hints the system's screen blanker) ---- */
const offEdit = {};   // a change on its way
function offSetting() {
  return Object.assign({ enabled: true, minutes: 5 }, ui.status.screen_off || {}, offEdit);
}
function renderScreenOff() {
  const s = offSetting();
  $('offSwitch').setAttribute('aria-checked', String(s.enabled));
  $('offMinutes').classList.toggle('off', !s.enabled);
  for (const b of $('offMinutes').children) b.classList.toggle('on', Number(b.dataset.m) === s.minutes);
  $('offNote').textContent = s.enabled ? `${s.minutes} 分钟没人碰、也没在对话就熄屏` : '屏幕一直亮着';
}
async function sendScreenOff(changes) {
  Object.assign(offEdit, changes);
  renderScreenOff();
  try {
    const r = await (await fetch('/api/screen-off', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                      body: JSON.stringify(changes) })).json();
    if (r.ok === false) toast(r.error || '没保存上', 'error');
  } catch (e) {
    toast('没保存上：客户端没有响应', 'error');
  }
  for (const k in changes) if (offEdit[k] === changes[k]) delete offEdit[k];
  renderScreenOff();
}
$('offSwitch').addEventListener('click', () => sendScreenOff({ enabled: !offSetting().enabled }));
for (const b of $('offMinutes').children) b.addEventListener('click', () => sendScreenOff({ minutes: Number(b.dataset.m) }));

/* ---- settings: volumes (the device's, which the keys and voice change too, and the alarms' own) ---- */
function slider(id, numId, get, send, onVal) {
  const el = $(id);
  let edit = null, timer = 0, sent = 0, busy = false, queued = null;
  function render() {
    const v = edit === null ? get() : edit;
    if (edit === null && v != null) el.value = v;
    setBarPos(el, v);
    $(numId).textContent = v == null ? '' : v + '%';
    if (onVal) onVal(v);
  }
  function pump(v) {
    queued = v;
    const fire = async () => {
      if (busy || queued === null) return;
      const cur = queued;
      queued = null;
      busy = true;
      sent = Date.now();
      try { await send(cur); } finally {
        busy = false;
        if (queued !== null) pump(queued);
      }
    };
    clearTimeout(timer);
    const wait = Math.max(0, 50 - (Date.now() - sent));
    if (wait === 0 && !busy) fire();
    else timer = setTimeout(fire, wait || 50);
  }
  bindSmartSlider(el, v => {
    edit = v;
    el.value = v;
    render();
    pump(v);
  }, v => {
    clearTimeout(timer);
    queued = null;
    edit = v;
    el.value = v;
    render();
    Promise.resolve(send(v)).then(() => { if (edit === v) edit = null; render(); });
  });
  return render;
}
async function postJson(path, body) {
  try {
    const r = await (await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                         body: JSON.stringify(body || {}) })).json();
    if (r.ok === false) toast(r.error || '没保存上', 'error');
    return r;
  } catch (e) {
    toast('没保存上：客户端没有响应', 'error');
    return { ok: false };
  }
}
function syncVolPresets(v) {
  const wrap = $('volPresets');
  if (!wrap) return;
  for (const b of wrap.querySelectorAll('button[data-vol]')) {
    b.classList.toggle('on', v != null && Math.abs(v - Number(b.dataset.vol)) <= 2);
  }
}
const renderVol = slider('volRange', 'volNum', () => ui.status.volume,
  v => postJson('/api/volume', { volume: v }), syncVolPresets);
$('volPresets').addEventListener('click', async e => {
  const t = elTarget(e), b = t && t.closest && t.closest('button[data-vol]');
  if (!b) return;
  const lv = Number(b.dataset.vol);
  $('volRange').value = lv;
  $('volNum').textContent = lv + '%';
  setBarPos($('volRange'), lv);
  syncVolPresets(lv);
  const r = await postJson('/api/volume', { volume: lv });
  if (r && r.ok !== false) {
    try { window.dispatchEvent(new CustomEvent('ts-settings-save', { detail: 'ok' })); } catch (err) {}
  }
});
/* ---- countdowns, alarms and reminders (alarms.py keeps and rings them; 主页「闹钟留言」 manages them) ---- */
function dayText(t) {
  const d = new Date(t * 1000), today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.round((new Date(d.getFullYear(), d.getMonth(), d.getDate()) - today) / 86400000);
  return { '-1': '昨天', 0: '今天', 1: '明天', 2: '后天' }[days] || `${d.getMonth() + 1}月${d.getDate()}日 星期${WEEK[d.getDay()]}`;
}
let holdTimer = null;
function cancelMicHold() {
  if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
  if (ui.ptt) { pttEnd(); post('/api/ptt/stop'); }
}
function renderAlarms() {
  const a = ui.alarms;
  if (a.ringing) cancelMicHold();
  document.body.classList.toggle('ringing', !!a.ringing);
  renderRing();
  uiChanged();
}
function renderRing() {
  const r = ui.alarms.ringing;
  if (!r) return;
  const items = r.items || [];
  $('ringKind').textContent = [...new Set(items.map(v => v.type))].join('、') || '闹钟';
  $('ringLabel').textContent = items.map(v => v.label).filter(Boolean).join('、');
  $('ringNote').textContent = (r.notes || []).join('；');
}
$('openChatSecBtn').addEventListener('click', () => showSection('chat'));
$('openNetSecBtn').addEventListener('click', () => showSection('net'));
$('openBackupHintBtn').addEventListener('click', () => showSection('net'));
$('ringStop').addEventListener('click', () => post('/api/alarms/stop'));
$('ringSnooze').addEventListener('click', () => {
  postJson('/api/alarms/snooze').then(r => { if (r.ok) toast('好的，10 分钟后再叫你'); });   // postJson shows the error
});

/* ---- settings: the version (self-maintained: no online update) ---- */
function renderUpdate() {
  const u = ui.update || {}, sys = u.system || {};
  const cur = !ui.version ? '' : sys.current ? `客户端 ${ui.version} · 系统层 ${sys.current}` : `版本 ${ui.version}`;
  $('updSub').textContent = [cur, u.note || '自主维护版 · 已停用第三方在线更新'].filter(Boolean).join(' · ');
}

/* ---- settings: chat history (kept on the hub; clearing also clears the hub's copy) ---- */
let clearArmed = 0, memArmed = 0;   // first tap arms a button, a second one within 5 s clears
function renderClear() {
  $('clearBtn').textContent = Date.now() - clearArmed < 5000 ? '确认清除' : '清除';
  $('clearBtn').classList.toggle('armed', Date.now() - clearArmed < 5000);
  $('clearMemBtn').textContent = Date.now() - memArmed < 5000 ? '确认清除' : '清除记忆';
  $('clearMemBtn').classList.toggle('armed', Date.now() - memArmed < 5000);
}
function agentNote(r) {
  // Claude Code keeps its own context: clearing here doesn't touch it
  return (r.agents || []).length ? `${r.agents.join('和')}自己那边的对话没有动` : '';
}
$('clearBtn').addEventListener('click', async () => {
  if (Date.now() - clearArmed > 5000) {
    clearArmed = Date.now();
    renderClear();
    setTimeout(renderClear, 5100);
    return toast('清除后所有模型的聊天记录都会删掉，再点一下「确认清除」');
  }
  clearArmed = 0;
  renderClear();
  const r = await (await post('/api/history/clear', {}))?.json?.();
  if (!r || !r.ok) return toast((r && r.error) || '没能清除', 'error');
  const note = agentNote(r);
  toast('聊天记录已清除' + (note ? '。' + note : ''));
});
$('clearMemBtn').addEventListener('click', async () => {
  if (Date.now() - memArmed > 5000) {
    memArmed = Date.now();
    renderClear();
    setTimeout(renderClear, 5100);
    return toast('模型会忘掉以前聊过的，聊天记录留着；再点一下「确认清除」');
  }
  memArmed = 0;
  renderClear();
  const r = await (await post('/api/history/clear', { memory_only: true }))?.json?.();
  toast(r && r.ok ? '记忆已清除，聊天记录还在' : (r && r.error) || '没能清除', r && r.ok ? '' : 'error');
});
renderClear();

// The device's address, read each time the sheet opens (it can change with the WiFi)
async function renderIp() {
  let ip = null;
  try { ip = (await (await fetch('/api/network')).json()).ip; } catch (e) { /* shown as not connected */ }
  const url = ip ? `http://${ip}:18091/admin/` : '';
  $('ipNum').textContent = ip ? `${ip}:18091/admin/` : '没有联网';
  $('ipSub').textContent = ip ? `这台设备的地址是 ${ip}` : '连上 Wi-Fi 后这里显示地址';
  try { window.dispatchEvent(new CustomEvent('ts-admin-url', { detail: url })); } catch (e) { /* no settings.js */ }
}
// section: 'display', 'alarm', ... opens there; true: from the top (the home dock, a page's header button)
let setSection = 'display';
function showSection(name) {
  if (name === 'home') name = 'weather';
  const nav = $('setNav').querySelector(`button[data-sec="${name}"]`);
  if (!nav) return;
  if (name !== 'chat') cancelMicHold();
  setSection = name;
  stopSheetScroll(true);
  blurSheetInput();
  for (const b of $('setNav').children) b.classList.toggle('on', b === nav);
  if (nav.scrollIntoView) nav.scrollIntoView({ block: 'nearest' });
  for (const el of document.querySelectorAll('#settingsPanel > .sec')) el.classList.toggle('on', el.dataset.sec === name);
  $('settingsPanel').scrollTop = 0;
  if (name === 'sound') pollAirplay();
  if (name === 'net') {
    renderIp();
    renderWifiCard();
    if (!wifi.nets.length) wifiPrimeCached().then(() => wifiScan());
    else wifiScan();
  }
  if (name === 'chat') scrollDown(true);
  try { window.dispatchEvent(new CustomEvent('ts-settings-section', { detail: name })); } catch (e) { /* no settings.js */ }
}
function openSettingsSheet(section) {
  clearWifiPaneTimer();
  if ($('wifi')?.classList.contains('on') && wifi.busy) { ++wifiOpSeq; wifi.busy = false; wifi.connectId = null; }
  $('wifi')?.classList.remove('on');
  $('sheet')?.classList.remove('on');
  const opened = !$('settings').classList.contains('on');
  $('settings').classList.add('on');
  if (typeof section === 'string') showSection(section);
  else if (section === true || opened) showSection(section === true ? 'display' : setSection);
  renderBright();
  pollDisplayState();
}
$('setNav').addEventListener('click', e => {
  const t = elTarget(e), b = t && t.closest && t.closest('button[data-sec]');
  if (b) showSection(b.dataset.sec);
});
window.__tsOpenSettings = openSettingsSheet;
$('baseDock').addEventListener('click', e => {
  const t = elTarget(e), b = t && t.closest && t.closest('button');
  if (!b) return;
  if (b.dataset.go === 'home') { if (typeof window.__tsGoHome === 'function') window.__tsGoHome(); else location.reload(); }
  else if (b.dataset.go === 'settings') openSettingsSheet(true);
  else if (b.dataset.go === 'wake') post('/api/wake');
  else if (typeof window.__tsOpenSub === 'function') window.__tsOpenSub(`http://127.0.0.1:${b.dataset.go}/`);
});
/* ---- settings: WiFi (the kiosk shell scans and joins: thinksmart-kiosk.py, the same code as its welcome page) ---- */
const shell = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.thinksmart;
const shellWifi = () => !!(shell && window.thinksmartShell && window.thinksmartShell.wifi);
const shellCalls = {};
let shellSeq = 0;
const SHELL_TIMEOUT = 90000;
async function wifiHttpCall(msg) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), msg.cmd === 'connect' ? 65000 : 30000);
  try {
    const isGet = msg.cmd === 'wifi_status' || msg.cmd === 'scan';
    const getUrl = msg.cmd === 'scan' && msg.rescan === false ? '/api/wifi?cmd=scan&rescan=0' : `/api/wifi?cmd=${encodeURIComponent(msg.cmd)}`;
    const r = await fetch(isGet ? getUrl : '/api/wifi', isGet ? {
      cache: 'no-store',
      signal: ctl.signal,
    } : {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(msg),
      signal: ctl.signal,
    });
    const j = await r.json().catch(() => null);
    window.thinksmartReply(Object.assign({ id: msg.id }, j || { ok: false, reason: 'noreply' }));
  } catch (e) {
    window.thinksmartReply({ id: msg.id, ok: false, reason: 'noreply' });
  } finally {
    clearTimeout(t);
  }
}
function shellCall(cmd, args) {
  return new Promise(resolve => {
    const id = ++shellSeq;
    const timer = setTimeout(() => window.thinksmartReply({ id, ok: false, reason: 'noreply' }), SHELL_TIMEOUT);
    shellCalls[id] = { resolve, timer };
    const msg = Object.assign({ id, cmd }, args || {});
    if (shellWifi()) shell.postMessage(JSON.stringify(msg));
    else wifiHttpCall(msg);
  });
}
window.thinksmartReply = function (r) {
  const entry = shellCalls[r.id];
  delete shellCalls[r.id];
  if (entry) {
    clearTimeout(entry.timer);
    entry.resolve(r);
  }
};

const WIFI_REASONS = {
  password: '密码不对，请再试一次',
  notfound: '找不到这个网络，可能离得太远或名称有误',
  timeout: '连接超时，请靠近路由器后再试',
  noreply: '设备没有回应，请过一会儿再看看网络状态',
  invalid: '网络名称或密码格式不正确',
};
const WIFI_STAGES = { scan: '正在搜索网络…', join: '正在连接路由器…', auth: '正在验证密码…', ip: '密码正确，正在获取网络地址…' };
const wifi = { nets: [], target: null, ssid: '', pw: '', showPw: false, busy: false, connectId: null };
let wifiPaneTimer = 0;
let wifiScanBusy = false, wifiOpSeq = 0;
const clearWifiPaneTimer = () => { if (wifiPaneTimer) { clearTimeout(wifiPaneTimer); wifiPaneTimer = 0; } };
const signalText = s => s > 75 ? '信号很好' : s > 50 ? '信号良好' : s > 25 ? '信号一般' : '信号较弱';

async function renderWifiCard() {
  $('wifiBtn').hidden = false;
  const r = await shellCall('wifi_status');
  $('wifiSub').textContent = !r.ok ? '正在准备无线网卡状态…' : r.ssid ? `已连接 ${r.ssid} · ${signalText(r.signal)}` : '未连接 Wi-Fi · 请在下方点选网络填密码连接';
}
function openWifi() {
  $('wifi').classList.add('on');
  wifiList();
  wifiScan();
}
function openWifiJoinDirect(net, hidden) {
  $('wifi').classList.add('on');
  wifiJoin(net, hidden);
}
function closeWifi() {
  if (wifi.busy) return;   // a connection attempt is on: it rolls back by itself if it fails
  clearWifiPaneTimer();
  if (document.activeElement) document.activeElement.blur();
  $('wifi').classList.remove('on');
  renderWifiCard();
  renderIp();
  wifiScan();
}
function wifiPane(id) {
  for (const p of document.querySelectorAll('.w-pane')) p.classList.toggle('on', p.id === id);
}
function wifiList() {
  clearWifiPaneTimer();
  if (document.activeElement) document.activeElement.blur();
  wifiPane('wList');
  renderNets();
}
async function wifiPrimeCached() {
  if (wifi.nets.length) return;
  try {
    const r = await fetch('/api/wifi?cmd=scan&rescan=0', { cache: 'no-store' });
    const j = await r.json().catch(() => null);
    if (j && j.ok && Array.isArray(j.networks) && j.networks.length && !wifi.nets.length) {
      wifi.nets = j.networks;
      renderNets();
    }
  } catch (e) {}
}
async function wifiScan() {
  if (wifiScanBusy) return;
  wifiScanBusy = true;
  $('wScan').classList.add('spin');
  $('netWifiScanBtn').disabled = true;
  $('netWifiScanBtn').textContent = '搜索中…';
  if (!wifi.nets.length) {
    $('wNets').innerHTML = '<div class="w-empty">正在搜索附近的 Wi-Fi…</div>';
    $('netWifiList').innerHTML = '<div class="w-empty">正在搜索附近的 Wi-Fi…</div>';
  }
  try {
    const r = await shellCall('scan');
    wifi.nets = r.ok ? r.networks : [];
    renderNets(r.ok ? '' : '搜索失败，点刷新再试');
    renderWifiCard();
  } finally {
    wifiScanBusy = false;
    $('wScan').classList.remove('spin');
    $('netWifiScanBtn').disabled = false;
    $('netWifiScanBtn').textContent = '刷新';
  }
}
function bars(signal) {
  const lit = signal > 75 ? 4 : signal > 50 ? 3 : signal > 25 ? 2 : 1;
  return '<span class="bars">' + [1, 2, 3, 4].map(i => `<i class="${i <= lit ? 'lit' : ''}"></i>`).join('') + '</span>';
}
const LOCK = '<svg class="w-lock" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>';
function renderNets(error) {
  const html = (error || !wifi.nets.length)
    ? `<div class="w-empty">${esc(error || '附近没有搜到 Wi-Fi，可点「手动添加」输入名称和密码')}</div>`
    : wifi.nets.map((n, i) =>
        `<button type="button" class="w-net${n.active ? ' cur' : ''}" data-i="${i}">${bars(n.signal)}<span class="name">${esc(n.ssid)}</span>` +
        (n.active ? '<span class="meta">已连接</span>' : n.saved ? '<span class="meta saved">已保存</span>' : '<span class="meta saved">选我连接</span>') +
        (n.secure ? LOCK : '') + '</button>').join('');
  $('wNets').innerHTML = html;
  $('netWifiList').innerHTML = html;
}
$('wNets').addEventListener('click', e => {
  const t = elTarget(e), b = t && t.closest && t.closest('.w-net');
  if (b && wifi.nets[Number(b.dataset.i)]) wifiJoin(wifi.nets[Number(b.dataset.i)], false);
});
$('netWifiList').addEventListener('click', e => {
  const t = elTarget(e), b = t && t.closest && t.closest('.w-net');
  if (b && wifi.nets[Number(b.dataset.i)]) openWifiJoinDirect(wifi.nets[Number(b.dataset.i)], false);
});
$('netWifiScanBtn').addEventListener('click', wifiScan);
$('netWifiHiddenBtn').addEventListener('click', () => openWifiJoinDirect(null, true));

// mode: 'type' (enter a password), 'saved' (a saved network: connect or forget), 'info' (the one
// connected: forget). Saved networks wait for a tap on 连接 so 忘记 stays reachable.
function wifiJoin(net, hidden) {
  clearWifiPaneTimer();
  ++wifiOpSeq;
  wifi.busy = false;
  wifi.connectId = null;
  wifi.target = { ssid: net ? net.ssid : '', secure: net ? net.secure : true, hidden, saved: !!(net && net.saved),
                  mode: net && net.active ? 'info' : net && net.saved ? 'saved' : 'type' };
  wifi.ssid = wifi.target.ssid;
  wifi.pw = '';
  wifi.showPw = false;
  const t = wifi.target, q = net ? signalText(net.signal) : '';
  $('wJoinTitle').textContent = hidden ? '添加隐藏网络' : t.ssid;
  $('wJoinHint').textContent = hidden ? '输入网络名称和密码（没有密码就留空），然后点键盘上的「连接」'
    : t.mode === 'info' ? '已连接 · ' + q
    : t.mode === 'saved' ? '已保存的网络 · ' + q + '，点「连接」用保存的密码连接'
    : t.secure ? '加密网络 · ' + q + '，输入密码后点键盘上的「连接」' : '开放网络 · ' + q;
  wifiMsg('');
  wifiPane('wJoin');
  renderJoin();
  if (t.mode === 'type' && !t.secure && !hidden) wifiConnect();   // open network: nothing to type
  else wifiFocus();
}
const typing = () => wifi.target.mode === 'type';
// the system keyboard (injected by the kiosk shell) comes up with the focused field
function wifiFocus() {
  if (!typing() || wifi.busy) return;
  const t = wifi.target;
  const el = t.hidden && !wifi.ssid ? $('wSsid') : t.secure ? $('wPw') : $('wSsid');
  setTimeout(() => el.focus(), 50);
}
function canConnect() {
  const t = wifi.target;
  // a hidden network may be open: an empty password is allowed there, 1-7 characters never are; WPA is 8-63 chars and SSID max 32 UTF-8 bytes
  const ssidBytes = new TextEncoder().encode(wifi.ssid || '').length;
  const pwLen = (wifi.pw || '').length;
  const pwOk = t.hidden ? (pwLen === 0 || (pwLen >= 8 && pwLen <= 63)) : (!t.secure || (pwLen >= 8 && pwLen <= 63));
  return !wifi.busy && (!t.hidden || (ssidBytes > 0 && ssidBytes <= 32)) && pwOk;
}
function renderJoin() {
  const t = wifi.target;
  if ($('wSsid').value !== wifi.ssid) $('wSsid').value = wifi.ssid;
  if ($('wPw').value !== wifi.pw) $('wPw').value = wifi.pw;
  $('wPw').type = wifi.showPw ? 'text' : 'password';
  $('wPw').placeholder = t.hidden ? '没有密码就留空' : '至少 8 位';
  $('wSsid').disabled = $('wPw').disabled = wifi.busy;
  $('wEye').textContent = wifi.showPw ? '隐藏' : '显示';
  $('wFieldSsid').style.display = typing() && t.hidden ? '' : 'none';
  $('wFieldPw').style.display = typing() && t.secure ? '' : 'none';
  $('wForget').hidden = !(t.saved || t.mode === 'info');
  $('wJoinBtn').hidden = t.mode === 'info' || t.mode === 'done' || (t.mode === 'type' && !t.secure && !t.hidden);
  $('wJoinBtn').disabled = wifi.busy || (t.mode === 'type' && !canConnect());
  $('wForget').disabled = wifi.busy;
}
function wifiMsg(text, kind) {
  $('wMsg').className = 'w-msg' + (kind ? ' ' + kind : '');
  $('wMsg').innerHTML = (kind === 'wait' ? '<span class="w-spin"></span>' : '') + esc(text);
}
async function wifiConnect() {
  clearWifiPaneTimer();
  const t = wifi.target;
  if (!t || (t.mode !== 'saved' && !canConnect())) return;
  const opId = ++wifiOpSeq;
  const targetSsid = wifi.ssid;
  wifi.busy = true;
  renderJoin();
  const usingSaved = t.mode === 'saved';
  wifiMsg(usingSaved ? `正在用已保存的密码连接 ${targetSsid}…` : `正在连接 ${targetSsid}…`, 'wait');
  wifi.connectId = shellSeq + 1;   // the id shellCall() is about to use: progress reports carry it
  const r = await shellCall('connect', { ssid: targetSsid, password: usingSaved || !t.secure ? '' : wifi.pw, hidden: !!t.hidden });
  if (opId !== wifiOpSeq || wifi.target !== t) return;
  wifi.connectId = null;
  wifi.busy = false;
  if (r.ok) {
    t.mode = 'info';
    t.saved = true;
    wifiMsg(`已连接 ${targetSsid}`, 'ok');
    renderJoin();
    clearWifiPaneTimer();
    const curTarget = wifi.target;
    wifiPaneTimer = setTimeout(() => {
      wifiPaneTimer = 0;
      if (!wifi.busy && wifi.target === curTarget && $('wifi').classList.contains('on') && $('wJoin').classList.contains('on')) {
        wifiList(); wifiScan();
      }
    }, 1500);
    return;
  }
  // whatever went wrong (the shell already went back to the previous network), let the user type a password next
  if (t.secure) t.mode = 'type';
  if (r.reason === 'password') wifi.pw = '';
  wifiMsg(usingSaved && r.reason === 'password' ? '保存的密码已失效，请重新输入'
    : WIFI_REASONS[r.reason] || ('连接失败' + (r.detail ? '：' + r.detail : '')) + '，已连回原来的网络', 'bad');
  renderJoin();
  wifiFocus();
}
window.thinksmartProgress = function (p) {
  if (wifi.busy && p.id === wifi.connectId && WIFI_STAGES[p.stage]) wifiMsg(WIFI_STAGES[p.stage], 'wait');
};
async function wifiForget() {
  if (wifi.busy || !wifi.target) return;
  clearWifiPaneTimer();
  const opId = ++wifiOpSeq;
  const curTarget = wifi.target;
  const ssid = curTarget.ssid;
  wifi.busy = true;
  wifiMsg(`正在忘记 ${ssid}…`, 'wait');
  renderJoin();
  const r = await shellCall('forget', { ssid });
  if (opId !== wifiOpSeq || wifi.target !== curTarget) return;
  wifi.busy = false;
  if (!r.ok) {
    wifiMsg(`没能忘记 ${ssid}，请再试一次`, 'bad');
    return renderJoin();
  }
  curTarget.saved = false;
  wifi.target.mode = 'done';
  wifiMsg(`已忘记 ${ssid}，下次连接需要重新输入密码`, 'ok');
  renderJoin();
  wifi.nets = wifi.nets.map(n => n.ssid === ssid ? Object.assign({}, n, { saved: false }) : n);
  clearWifiPaneTimer();
  wifiPaneTimer = setTimeout(() => {
    wifiPaneTimer = 0;
    if (!wifi.busy && wifi.target === curTarget && $('wifi').classList.contains('on') && $('wJoin').classList.contains('on')) {
      wifiList(); wifiScan();
    }
  }, 1400);
}
$('wifiBtn').addEventListener('click', openWifi);
$('wClose').addEventListener('click', closeWifi);
$('wScan').addEventListener('click', wifiScan);
$('wHidden').addEventListener('click', () => wifiJoin(null, true));
$('wBack').addEventListener('click', () => { if (!wifi.busy) wifiList(); });
$('wForget').addEventListener('click', wifiForget);
$('wJoinBtn').addEventListener('click', wifiConnect);
// On pointerdown, not click: cancelling a touch's pointerdown (to keep the field focused and the system
// keyboard up) makes WebKitGTK send no click at all, so a click handler never ran on the device's screen
// (a mouse in a desktop browser still clicks). The on-screen keyboard's keys work the same way.
$('wEye').addEventListener('pointerdown', e => { e.preventDefault(); wifi.showPw = !wifi.showPw; renderJoin(); });
$('wSsid').addEventListener('input', () => {
  let v = $('wSsid').value;
  const enc = new TextEncoder();
  while (enc.encode(v).length > 32) v = Array.from(v).slice(0, -1).join('');
  if ($('wSsid').value !== v) $('wSsid').value = v;
  wifi.ssid = v;
  if (wifi.target && wifi.target.mode === 'type') $('wJoinBtn').disabled = !canConnect();
});
$('wPw').addEventListener('input', () => {
  let v = $('wPw').value;
  if (v.length > 63) {
    v = v.slice(0, 63);
    const c = v.charCodeAt(v.length - 1);
    if (c >= 0xD800 && c <= 0xDBFF) v = v.slice(0, -1);
  }
  if ($('wPw').value !== v) $('wPw').value = v;
  wifi.pw = v;
  if (wifi.target && wifi.target.mode === 'type') $('wJoinBtn').disabled = !canConnect();
});
$('wSsid').addEventListener('keydown', e => {
  if (e.key !== 'Enter') return;
  e.preventDefault();   // keep the keyboard up: on to the password
  if (wifi.target.secure) $('wPw').focus();
});
$('wPw').addEventListener('keydown', e => {
  if (e.key !== 'Enter') return;
  if (!canConnect()) { e.preventDefault(); return; }   // keep typing
  wifiConnect();
});

/* ---- settings: Bluetooth speaker (bt.py; pairing only in the 3-minute "add a phone" window) ---- */
function btPlaying(d) {
  const song = [d.title, d.artist].filter(Boolean).join(' · ');
  return song || '正在播放';
}
function renderBluetooth() {
  const b = ui.bluetooth;
  const on = !!(b && b.available && b.enabled);
  if (!b) return;
  $('btSwitch').setAttribute('aria-checked', String(on));
  $('btSwitch').disabled = !b.available;
  $('btCard').classList.toggle('off', !on);
  $('btName').textContent = on && b.name ? `手机上显示为「${b.name}」。放歌时叫小智或闹钟响，歌声会自动变小。` : '';
  let note;
  if (!b.available) note = '找不到蓝牙，或者蓝牙服务没有运行';
  else if (!b.enabled) note = '打开后手机可以连上来放歌';
  else if (b.window) note = `正在等手机连接，还剩 ${b.window} 秒：在手机蓝牙里点「${b.name}」`;
  else note = '连过的手机在蓝牙设置里点一下就能连上';
  if (on && b.direct) note += ' 注意：系统层版本较旧，手机的声音没经过客户端，放歌时很难唤醒。';
  $('btNote').textContent = note;
  const phones = b.devices || [];
  $('btList').innerHTML = phones.length ? phones.map(d =>
    `<div class="al phone${d.streaming ? ' live' : ''}"><i>手机</i><div class="al-text"><b>${esc(d.name)}</b>` +
    `<span${d.streaming ? ' class="live"' : ''}>${d.streaming ? esc(btPlaying(d)) : d.connected ? '已连接' : '已配对，没连着'}</span></div>` +
    (btIsArmed(d.address) ? `<button class="bt al-del armed" data-addr="${esc(d.address)}">确认忘记</button></div>`
                          : `<button class="bt al-del" data-addr="${esc(d.address)}">忘记</button></div>`)).join('')
    : '<div class="alarm-empty">还没有连过的手机</div>';
  $('btAdd').classList.toggle('on', !!b.window);
  $('btAdd').textContent = b.window ? `停止等待（${b.window} 秒）` : '＋ 添加手机';
}
// 「忘记」 needs a second tap within 4 s. Kept by address, not on the button: while 添加手机 waits
// the list is redrawn every second (the countdown), which would drop a state kept on the DOM.
let btArmed = null;   // {addr, until}
const btIsArmed = addr => !!btArmed && btArmed.addr === addr && Date.now() < btArmed.until;
async function btPost(body) {
  return postJson('/api/bluetooth', body);   // {ok: false} after it has shown the error
}
$('btSwitch').addEventListener('click', () => btPost({ enabled: !(ui.bluetooth && ui.bluetooth.enabled) }));
$('btAdd').addEventListener('click', () => btPost({ window: !(ui.bluetooth && ui.bluetooth.window) }));
$('btList').addEventListener('click', e => {
  const t = elTarget(e), b = t && t.closest && t.closest('.al-del');
  if (!b) return;
  if (!btIsArmed(b.dataset.addr)) {
    btArmed = { addr: b.dataset.addr, until: Date.now() + 4000 };
    renderBluetooth();
    setTimeout(renderBluetooth, 4100);
    return;
  }
  btArmed = null;
  btPost({ remove: b.dataset.addr }).then(r => { if (r && r.ok !== false) toast('已忘记，这台手机要重新配对才能连'); });
});

/* ---- settings: AirPlay mirroring & audio switch (thinksmart-airplay.service) ---- */
let airplayBusy = false;
function renderAirplay() {
  const ap = ui.airplay;
  const swEl = $('airplaySwitch'), noteEl = $('airplayNote');
  if (!swEl || !noteEl) return;
  if (!ap) {
    noteEl.textContent = '正在读取状态…';
    return;
  }
  const on = !!(ap.available && ap.enabled);
  swEl.setAttribute('aria-checked', String(on));
  swEl.disabled = !ap.available || airplayBusy;
  const devName = ap.name || 'ThinkSmart View';
  if (!ap.available) noteEl.textContent = '未检测到投屏组件（uxplay）';
  else if (!on) noteEl.textContent = '已关闭 · 打开后苹果设备可无线投屏画面或隔空播放音频';
  else if (ap.mirroring) noteEl.textContent = `正在投屏画面 · 设备名「${devName}」`;
  else if (ap.streaming) noteEl.textContent = `正在隔空播放音频 · 设备名「${devName}」`;
  else noteEl.textContent = `已开启 · iPhone / iPad 控制中心点「屏幕镜像」选「${devName}」即可连接`;
}
async function pollAirplay() {
  try {
    const r = await fetch('/api/airplay', { cache: 'no-store' });
    if (r.ok) {
      ui.airplay = await r.json();
      renderAirplay();
    }
  } catch (e) {}
}
$('airplaySwitch').addEventListener('click', async () => {
  if (airplayBusy) return;
  const want = !(ui.airplay && ui.airplay.enabled);
  airplayBusy = true;
  if (ui.airplay) ui.airplay = Object.assign({}, ui.airplay, { enabled: want });
  renderAirplay();
  try { window.dispatchEvent(new CustomEvent('ts-settings-save', { detail: 'busy' })); } catch (e) {}
  try {
    const r = await postJson('/api/airplay', { enabled: want });
    if (r && typeof r.enabled === 'boolean') ui.airplay = r;
    if (r && r.ok !== false) {
      try { window.dispatchEvent(new CustomEvent('ts-settings-save', { detail: 'ok' })); } catch (e) {}
    } else {
      try { window.dispatchEvent(new CustomEvent('ts-settings-save', { detail: '' })); } catch (e) {}
      await pollAirplay();
    }
  } finally {
    airplayBusy = false;
    renderAirplay();
  }
});


// 关机: halts the system (it stays dark with the power cable in), so unplugging can't damage it
let powerArmed = 0;
function renderPower() {
  $('powerBtn').textContent = Date.now() - powerArmed < 5000 ? '确认关机' : '关机';
  $('powerBtn').classList.toggle('armed', Date.now() - powerArmed < 5000);
}
$('powerBtn').addEventListener('click', async () => {
  if (Date.now() - powerArmed > 5000) {
    powerArmed = Date.now();
    renderPower();
    setTimeout(renderPower, 5100);
    return toast('关机后要拔掉电源，等 30 秒再插上才会开机，再点一下「确认关机」');
  }
  powerArmed = 0;
  renderPower();
  const r = await (await post('/api/power-off', {}))?.json?.();
  if (!r || !r.ok) toast((r && r.error) || '没能关机', 'error');
});
function blurSheetInput() {
  const ae = document.activeElement;
  if (ae && ae.closest && ae.closest('#settings, #wifi, #sheet') && typeof ae.blur === 'function') ae.blur();
  window.ThinkSmartKeyboard?.hide?.();
}
function closeBaseSheets() {
  cancelMicHold();
  stopSheetScroll(true);
  blurSheetInput();
  clearWifiPaneTimer();
  if (wifi.busy) { ++wifiOpSeq; wifi.busy = false; wifi.connectId = null; }
  clearTimeout(dispPollTimer); dispPollTimer = 0;
  $('settings')?.classList.remove('on');
  $('wifi')?.classList.remove('on');
  $('sheet')?.classList.remove('on');
}
window.__tsCloseBaseSheets = closeBaseSheets;
$('settingsClose').addEventListener('click', () => {
  cancelMicHold();
  stopSheetScroll(true);
  blurSheetInput();
  clearTimeout(dispPollTimer); dispPollTimer = 0;
  $('settings').classList.remove('on');
});

/* ---- 设置 → 对话: the current model's conversation ---- */
function entryModel(entry) {
  return ui.models.options.find(o => o.entry === entry) || ui.models.current;
}

function renderHead() {
  const n = ui.thread.messages.length;
  $('headMeta').textContent = n ? `${n} 条` : '';
  $('chatEmpty').hidden = n > 0 || !!$('pending');
}

function fmtTime(t) {
  const d = new Date(t * 1000);
  return pad(d.getHours()) + ':' + pad(d.getMinutes());
}

// A reply grows sentence by sentence; attach optional native rich media card container (.msg-rich)
function fillMsg(el, m) {
  const html = esc(m.text) + (m.interrupted ? '<span class="cut">已打断</span>' : '') + `<span class="ts">${fmtTime(m.time)}</span>`;
  if (m.role === 'bot') {
    el.querySelector('.body-txt').innerHTML = html;
    const rc = el.querySelector('.msg-rich');
    if (rc && typeof window.__tsDecorateBotMsg === 'function') window.__tsDecorateBotMsg(rc, m);
  } else el.innerHTML = html;
}

function msgEl(m) {
  const el = document.createElement('div');
  el.className = 'msg ' + m.role;
  el.dataset.id = m.id;
  if (m.role === 'bot') {
    el.innerHTML = `<img class="tile" src="${esc(logoSrc(entryModel(m.entry)))}" alt=""><div class="body"><div class="body-txt"></div><div class="msg-rich"></div></div>`;
  }
  fillMsg(el, m);
  return el;
}

const chatOn = () => setSection === 'chat' && $('settings').classList.contains('on');
// force: the section was just opened. Otherwise only when the end is in view: reading older messages isn't pulled away.
function scrollDown(force) {
  const box = $('messages');
  if (force || (chatOn() && box.scrollHeight - box.scrollTop - box.clientHeight < 260)) box.scrollTop = box.scrollHeight;
}
const dayKey = t => { const d = new Date(t * 1000); return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`; };
function dayEl(t) {
  const el = document.createElement('div');
  el.className = 'day';
  el.textContent = dayText(t);
  return el;
}

function renderThread() {
  const box = $('messages');
  box.textContent = '';
  let day = '';
  for (const m of ui.thread.messages) {
    if (m.time && dayKey(m.time) !== day) { day = dayKey(m.time); box.appendChild(dayEl(m.time)); }
    box.appendChild(msgEl(m));
  }
  markLive();
  renderPending();
  renderHead();
  scrollDown(true);
}

function upsertMessage(m) {
  if (m.entry !== ui.thread.entry) return;
  const i = ui.thread.messages.findIndex(x => x.id === m.id);
  // a rich card belongs to the answer its tool was called for: the tool of this turn, kept while the answer grows
  if (m.role === 'user' && i < 0) ui.lastTool = null;
  if (m.role === 'bot') {
    const had = i >= 0 ? ui.thread.messages[i]._tool : null;
    if (had) m._tool = had;
    else if (ui.lastTool && Math.abs((m.time || (Date.now() / 1000)) - (ui.lastTool.time || 0)) < 35) m._tool = ui.lastTool;
  }
  const box = $('messages');
  if (i >= 0) {
    ui.thread.messages[i] = m;
    const el = box.querySelector(`[data-id="${m.id}"]`);
    if (el) fillMsg(el, m);
  } else {
    const last = ui.thread.messages[ui.thread.messages.length - 1];
    if (m.time && (!last || !last.time || dayKey(last.time) !== dayKey(m.time))) box.insertBefore(dayEl(m.time), $('pending'));
    ui.thread.messages.push(m);
    const end = box.scrollHeight - box.scrollTop - box.clientHeight < 260;
    box.insertBefore(msgEl(m), $('pending'));
    if (end && chatOn()) box.scrollTop = box.scrollHeight;
  }
  markLive();
  renderHead();
  scrollDown();
}

function markLive() {
  // the reply being spoken right now: its logo gets a ring
  const box = $('messages');
  box.querySelectorAll('.live').forEach(el => el.classList.remove('live'));
  const last = ui.thread.messages[ui.thread.messages.length - 1];
  if (ui.status.state === 'speaking' && last && last.role === 'bot') {
    const el = box.querySelector(`[data-id="${last.id}"]`);
    if (el) el.classList.add('live');
  }
}

// Placeholder at the end of the conversation while the user talks or the model thinks
function renderPending() {
  const shown = shownState();
  // recognising: the user stopped and the hub is turning it into text (it can take a few seconds)
  const kind = ui.status.recognising && !ui.ptt ? 'user recog' : shown === 'listening' ? (ui.ptt ? 'user ptt' : 'user') :
    shown === 'thinking' ? 'bot' : '';
  let el = $('pending');
  if (el && el.dataset.kind === kind) return;
  if (el) el.remove();
  if (!kind) return;
  el = document.createElement('div');
  el.id = 'pending';
  el.dataset.kind = kind;
  el.className = 'msg pending ' + kind;
  if (kind.startsWith('user')) {
    el.innerHTML = `<span class="wave">${'<b></b>'.repeat(5)}</span>${kind === 'user recog' ? '正在识别…' : ui.ptt ? '说完松开' : '正在听…'}`;
  } else {
    el.innerHTML = `<img class="tile" src="${esc(logoSrc(entryModel(ui.thread.entry)))}" alt="">` +
      `<div class="body"><span class="dots"><i></i><i></i><i></i></span><span class="label">正在思考</span></div>`;
  }
  const box = $('messages'), end = box.scrollHeight - box.scrollTop - box.clientHeight < 260;
  box.appendChild(el);
  if (end && chatOn()) box.scrollTop = box.scrollHeight;
  renderHead();
}

/* ---- overlays ---- */
let toastTimer = null;
function toast(text, kind) {
  const t = $('toast');
  t.textContent = text;
  t.className = 'toast on' + (kind === 'error' ? ' error' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = 'toast' + (kind === 'error' ? ' error' : ''); }, kind === 'error' ? 4500 : 2500);
}

let hudTimer = null;
function showVolume(v) {
  $('hudFill').style.transform = `scaleX(${Math.max(0, Math.min(100, v)) / 100})`;
  $('hudNum').textContent = v;
  $('hud').classList.add('on');
  clearTimeout(hudTimer);
  hudTimer = setTimeout(() => $('hud').classList.remove('on'), 1600);
}

/* ---- events from the client ---- */
const recentBotTools = new Map();
function saveBotTools(msgs) {
  if (!Array.isArray(msgs)) return;
  for (const m of msgs) {
    if (m && m.role === 'bot' && m._tool && m.text) {
      const k = `${m.entry || ''}\n${m.text}`;
      recentBotTools.delete(k);
      recentBotTools.set(k, m._tool);
      while (recentBotTools.size > 32) recentBotTools.delete(recentBotTools.keys().next().value);
    }
  }
}
function restoreBotTools(msgs) {
  if (!Array.isArray(msgs)) return;
  for (const m of msgs) {
    if (m && m.role === 'bot' && !m._tool && m.text) {
      const t = recentBotTools.get(`${m.entry || ''}\n${m.text}`);
      if (t) m._tool = t;
    }
  }
}
const handlers = {
  snapshot(s) {
    if (ui.build && s.build && s.build !== ui.build) return location.reload();   // client restarted (deployed)
    ui.build = s.build;
    pttEnd();
    saveBotTools(ui.thread.messages);
    restoreBotTools(s.thread && s.thread.messages);
    ui.status = s.status; ui.models = server.models = s.models; ui.thread = server.thread = s.thread;
    switching = null;
    for (const k in seenThreads) delete seenThreads[k];
    seenThreads[s.thread.entry] = s.thread.messages;
    if (s.update) { ui.version = s.version; ui.update = s.update; }
    if (s.alarms) ui.alarms = s.alarms;
    if (s.bluetooth) ui.bluetooth = s.bluetooth;
    if (s.airplay) ui.airplay = s.airplay;
    renderModels(); renderThread(); renderStatus(); renderAlarms(); renderBluetooth(); renderAirplay();
  },
  power(p) {
    const reboot = p.state === 'reboot';
    $('powerOffTitle').textContent = reboot ? '正在重启' : '正在关机';
    $('powerOffSub').textContent = reboot ? '大约 1 分钟后自动亮起来' : '屏幕变黑后等 10 秒再拔掉电源';
    $('powerOff').classList.toggle('on', reboot || p.state === 'off');
  },
  update(u) {
    ui.update = u;
    renderUpdate();
  },
  state(s) {
    ui.status = s; renderStatus(); markLive(); renderHead();
  },
  model(m) {
    server.models = m;
    if (switching && (!m.current || m.current.entry !== switching)) return;
    ui.models = m; renderModels(); renderStatus();
  },
  thread(t) {
    saveBotTools(ui.thread.messages);
    if (t.reset) {
      for (const k in seenThreads) delete seenThreads[k];   // cleared, or the hub's history loaded
      if (!t.messages || !t.messages.length) recentBotTools.clear();
    }
    restoreBotTools(t.messages);
    server.thread = t;
    seenThreads[t.entry] = t.messages;
    if (switching && t.entry !== switching) return;
    ui.thread = t; renderThread();
  },
  message: m => upsertMessage(m),
  toast: t => toast(t.text, t.kind),
  volume(v) {
    // a ring's own volume (keys while it rings) is shown, but isn't the device volume
    if (!v.ring) { ui.status.volume = v.volume; renderVol(); }
    if (v.source !== 'page') showVolume(v.volume);
  },
  alarms(a) {
    ui.alarms = a; renderAlarms();
  },
  bluetooth(b) {
    ui.bluetooth = b; renderBluetooth();
  },
  airplay(ap) {
    ui.airplay = ap; renderAirplay();
  },
  navigate(n) {
    if (!n || !n.page || document.body.classList.contains('ringing')) return;
    if (typeof window.__tsNavigate === 'function') {
      window.__tsNavigate(n);
      return;
    }
    const p = n.page;
    if (p === 'home') {
      closeBaseSheets();
      if (typeof window.__tsGoHome === 'function') window.__tsGoHome();
    } else if (p === 'music' && typeof window.__tsOpenSub === 'function') {
      window.__tsOpenSub('http://127.0.0.1:8091/');
    } else if (p === 'radio' && typeof window.__tsOpenSub === 'function') {
      window.__tsOpenSub('http://127.0.0.1:8091/', 'openRadio');
    } else if (p === 'server' && typeof window.__tsOpenSub === 'function') {
      window.__tsOpenSub('http://127.0.0.1:8092/', 'overview');
    } else if (p === 'tutor' && typeof window.__tsOpenSub === 'function') {
      window.__tsOpenSub('http://127.0.0.1:8096/');
    } else if (p === 'settings') {
      if (typeof window.__tsCloseSub === 'function') window.__tsCloseSub(false);
      if (typeof window.__tsOpenUnifiedSettings === 'function') window.__tsOpenUnifiedSettings(n.section || true);
      else if (typeof window.__tsOpenSettings === 'function') window.__tsOpenSettings(n.section || true);
    } else if (p === 'weather' && typeof window.__tsOpenHomeWeather === 'function') {
      window.__tsOpenHomeWeather(n.city || '');
    } else if (p === 'alarms' && typeof window.__tsOpenHomeAlarms === 'function') {
      window.__tsOpenHomeAlarms();
    } else if (p === 'messages' && typeof window.__tsOpenHomeMessages === 'function') {
      window.__tsOpenHomeMessages();
    } else if (p === 'countdown' && typeof window.__tsOpenHomeCountdown === 'function') {
      window.__tsOpenHomeCountdown();
    } else if (p === 'timer' && typeof window.__tsOpenHomeFocus === 'function') {
      window.__tsOpenHomeFocus();
    } else if (p === 'sleep' && typeof window.__tsOpenHomeSleep === 'function') {
      window.__tsOpenHomeSleep();
    }
  },
  tool(t) {
    const card = window.__tsHasCard;   // a lookup (the time, a search) doesn't push out a tool that has a card
    if (!(typeof card === 'function' && card(ui.lastTool) && !card(t))) ui.lastTool = t;
    try { window.dispatchEvent(new CustomEvent('ts-voice-tool', { detail: t })); } catch (e) {}
  },
  level(l) {
    const v = ui.status.state === 'speaking' ? Math.min(1, l.out * 6) : micLevel(l.mic);
    if (ui.status.state === 'listening' || ui.status.state === 'speaking') {
      const now = performance.now();
      if (!ui._lvlSentAt || now - ui._lvlSentAt >= 48) {
        ui._lvlSentAt = now;
        try { window.dispatchEvent(new CustomEvent('ts-voice-level', { detail: { v, state: ui.status.state } })); } catch (e) {}
      }
    }
  },
};

function connectEvents() {
  const es = new EventSource('/events');
  for (const [name, fn] of Object.entries(handlers)) es.addEventListener(name, e => fn(JSON.parse(e.data)));
}

/* ---- buttons ---- */
function post(path, body) {
  const opts = { method: 'POST' };
  if (body) Object.assign(opts, { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  return fetch(path, opts).catch(() => toast('客户端没有响应', 'error'));
}

// Mic: a tap wakes (or interrupts a reply and listens); holding it is push-to-talk
function pttStart() {
  ui.ptt = true;
  $('btnMic').classList.add('hold');
  renderStatus();
}
function pttEnd() {
  ui.ptt = false;
  $('btnMic').classList.remove('hold');
  renderStatus();
}
addEventListener('blur', cancelMicHold);
document.addEventListener('visibilitychange', () => { if (document.hidden) cancelMicHold(); });
$('btnMic').addEventListener('pointerdown', e => {
  if (e.isPrimary === false || (e.pointerType === 'mouse' && e.button !== 0)) return;
  e.preventDefault();
  if (!ui.status.connected && ui.status.state === 'idle') fetch('/api/prewarm', { method: 'POST' }).catch(() => {});
  if (holdTimer) clearTimeout(holdTimer);
  holdTimer = setTimeout(() => {
    holdTimer = null;
    pttStart();
    post('/api/ptt/start');
  }, 350);
});
function release() {
  if (holdTimer) {
    clearTimeout(holdTimer);
    holdTimer = null;
    post('/api/wake');
  } else if (ui.ptt) {
    pttEnd();
    post('/api/ptt/stop');
  }
}
$('btnMic').addEventListener('pointerup', release);
$('btnMic').addEventListener('pointercancel', release);
$('btnMic').addEventListener('pointerleave', () => {
  if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
  else if (ui.ptt) release();
});
$('modelPill').addEventListener('click', () => openSheet(true));
$('setModel').addEventListener('click', () => openSheet(true));
$('sheetClose').addEventListener('click', () => openSheet(false));
document.addEventListener('contextmenu', e => e.preventDefault());

for (const [q, tint, icon] of SUGGEST) {
  const b = document.createElement('button');
  b.className = 'chip';
  b.innerHTML = `<i style="background:var(--${tint}-soft);color:var(--${tint}-ink)"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${icon}</svg></i>${esc(q)}`;
  b.addEventListener('click', () => post('/api/text', { text: q }));
  $('chips').appendChild(b);
}

tick();
setInterval(tick, 1000);
renderStatus();
renderThread();
connectEvents();
setTimeout(() => {
  pollDisplayState();
  pollAirplay();
  renderIp();
  wifiPrimeCached();
}, 2100);
