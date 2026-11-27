// 设置页里属于整机的部分（2026-11-27）：小智的唤醒 / 音色 / 记忆、天气（城市搜索）、音乐、学习、系统。
// 一栏打开时才去读（GET /api/settings?part=…），改一项存一项（POST /api/settings），界面先变、没存上再变回来。
// 亮度、主题、音量、熄屏、Wi-Fi、蓝牙、AirPlay、清除、关机、对话在 app.js 里。
(() => {
  const PART = { voice: 'assistant', weather: 'home', music: 'music', study: 'study', sys: 'system' };
  const D = {};            // last known values per part: assistant, voice, music, home, study, system
  const loadedAt = {};
  const pending = {};      // part -> { values, timer }: changes waiting to be sent
  const inflight = {};
  const advOpen = { wake: false, listen: false, edgeVoices: false, study: false };
  let cur = 'display';
  let saveBadgeTimer = 0;
  const elTarget = e => { const t = e && e.target; return t && t.nodeType === 3 ? t.parentElement : t; };

  function setSaveBadge(mode) {
    const el = $('setSavePill');
    if (!el) return;
    clearTimeout(saveBadgeTimer);
    saveBadgeTimer = 0;
    if (mode === 'busy') {
      el.textContent = '保存中…';
      el.className = 'save-pill on busy';
    } else if (mode === 'ok') {
      el.textContent = '已保存';
      el.className = 'save-pill on';
      saveBadgeTimer = setTimeout(() => { el.className = 'save-pill'; }, 1600);
    } else {
      el.className = 'save-pill';
    }
  }
  addEventListener('ts-settings-save', e => setSaveBadge(e.detail || 'ok'));

  const api = async (path, body) => {
    const ctl = new AbortController(), t = setTimeout(() => ctl.abort(), 25000);
    try {
      const r = await fetch(path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: ctl.signal }
                                       : { cache: 'no-store', signal: ctl.signal });
      const j = await r.json().catch(() => null);
      return j || { ok: false, error: '没有响应，稍后再试' };
    } catch (e) {
      return { ok: false, error: '没有响应，稍后再试' };
    } finally { clearTimeout(t); }
  };
  const take = j => { for (const k of ['assistant', 'voice', 'music', 'home', 'study', 'system']) if (k in j && (j.ok || j[k] !== null || D[k] === undefined)) D[k] = j[k]; };

  const failedPart = {};
  async function load(part, force) {
    if (!force && loadedAt[part] && Date.now() - loadedAt[part] < 5000) return;
    const j = await api('/api/settings?part=' + part);
    if (j.ok) { take(j); loadedAt[part] = Date.now(); delete failedPart[part]; } else if (!(part in loadedAt)) { D[part === 'assistant' ? 'assistant' : part] = null; failedPart[part] = true; }
    paint();
  }
  function change(part, values, ms = 0) {
    const p = pending[part] || (pending[part] = { values: {}, timer: 0 });
    Object.assign(p.values, values);
    clearTimeout(p.timer);
    setSaveBadge('busy');
    p.timer = setTimeout(() => flush(part), ms);
    paint();
  }
  const inflightVals = {};
  async function flush(part) {
    const p = pending[part];
    if (!p || inflight[part]) return;
    clearTimeout(p.timer);
    delete pending[part];
    inflight[part] = true;
    inflightVals[part] = p.values;
    setSaveBadge('busy');
    try {
      const j = await api('/api/settings', { part, values: p.values });
      if (!pending[part]) take(j);
      if (!j.ok) { setSaveBadge(''); toast(j.error || '没保存上', 'error'); }
      else {
        if (!pending[part]) setSaveBadge('ok');
        if (j.note) toast(j.note);
      }
      if (!j.ok && !(part in j)) load(part, true);
    } finally {
      delete inflight[part];
      delete inflightVals[part];
      paint();
      if (pending[part]) flush(part);
    }
  }
  const val = (part, key, fallback) => (
    pending[part] && key in pending[part].values ? pending[part].values[key]
      : inflightVals[part] && key in inflightVals[part] ? inflightVals[part][key] : fallback
  );

  /* ---- 控件 ---- */
  const SIGN = ['M6 12h12', 'M12 6v12M6 12h12'].map(d => `<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="${d}"/></svg>`);
  const chev = open => `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="${open ? 'M5 15l7-7 7 7' : 'M5 9l7 7 7-7'}"/></svg>`;
  const sw = (part, key, on, label) => `<button class="switch" role="switch" aria-checked="${!!val(part, key, on)}" aria-label="${esc(label)}" data-p="${part}" data-k="${key}" data-c="sw"><i></i></button>`;
  const seg = (part, key, now, opts) => `<div class="seg" role="group">${opts.map(([v, t]) => `<button type="button" class="${String(val(part, key, now)) === String(v) ? 'on' : ''}" data-p="${part}" data-k="${key}" data-v="${esc(v)}" data-c="seg">${esc(t)}</button>`).join('')}</div>`;
  const step = (part, key, now, lo, hi, by, fmt) => {
    const v = val(part, key, now);
    return `<div class="step"><button type="button" data-p="${part}" data-k="${key}" data-c="step" data-d="${-by}" data-lo="${lo}" data-hi="${hi}" data-now="${v}" aria-label="减"${v <= lo + 1e-6 ? ' disabled' : ''}>${SIGN[0]}</button><b>${esc(fmt(v))}</b><button type="button" data-p="${part}" data-k="${key}" data-c="step" data-d="${by}" data-lo="${lo}" data-hi="${hi}" data-now="${v}" aria-label="加"${v >= hi - 1e-6 ? ' disabled' : ''}>${SIGN[1]}</button></div>`;
  };
  const row = (title, sub, ctl, cls = '') => `<div class="rw ${cls}"><div class="lb"><b>${esc(title)}</b>${sub ? `<span>${esc(sub)}</span>` : ''}</div>${ctl}</div>`;
  const grp = (t, small) => `<div class="grp">${esc(t)}${small ? `<small>${esc(small)}</small>` : ''}</div>`;
  const box = inner => `<div class="box">${inner}</div>`;
  const adv = (key, title, summary, inner) => `<button type="button" class="adv-toggle" data-c="adv" data-v="${key}" aria-expanded="${!!advOpen[key]}"><span>${esc(title)}<small>${esc(summary)}</small></span>${chev(!!advOpen[key])}</button><div class="adv-body${advOpen[key] ? ' open' : ''}">${inner}</div>`;
  const put = (id, html) => { const el = $(id); if (el && el.__html !== html) { el.__html = html; el.innerHTML = html; } };
  const down = what => `<div class="loading">${esc(what)}没有响应，过一会儿再打开</div>`;

  /* ---- 小智 ---- */
  const THR_LO = .10, THR_HI = .60;
  const thrToPct = t => Math.round((THR_HI - t) / (THR_HI - THR_LO) * 100);
  const pctToThr = p => Math.round((THR_HI - p / 100 * (THR_HI - THR_LO)) * 100) / 100;
  const thrWord = t => t <= .17 ? '很灵敏' : t <= .24 ? '灵敏' : t <= .34 ? '适中' : '不易误醒';
  function syncThrPresets(t) {
    const rowEl = $('thrRow');
    if (!rowEl) return;
    const grpIdx = t > 0.34 ? '0.40' : t > 0.19 ? '0.25' : '0.16';
    for (const b of rowEl.querySelectorAll('button[data-c="thr-preset"]')) {
      b.classList.toggle('on', b.dataset.v === grpIdx);
    }
  }
  function paintVoice() {
    const r0 = $('thrRange');
    if (r0 && r0.__drag) return;
    const a = D.assistant, v = D.voice;
    if (!a) return put('voiceBody', (loadedAt.assistant || failedPart.assistant) ? (a === null ? down('语音助手') : '<div class="loading">正在读取…</div>') : '<div class="loading">正在读取…</div>');
    const words = val('assistant', 'wake_words', a.wake_words.filter(w => w.on).map(w => w.raw));
    const curThr = val('assistant', 'kws_threshold', a.kws_threshold);
    const kwsGain = val('assistant', 'kws_gain_db', a.kws_gain_db);
    const kwsScore = val('assistant', 'kws_score', a.kws_score);
    const kwsPaths = val('assistant', 'kws_paths', a.kws_paths);
    let h = grp('唤醒');
    h += box(`<div class="rw col"><div class="lb"><b>唤醒词</b><span>已选 ${words.length} 个 · 可以多选，至少留一个</span></div><div class="opts three">${a.wake_words.map(w => `<button type="button" class="${words.includes(w.raw) ? 'on' : ''}" data-c="word" data-v="${esc(w.raw)}">${esc(w.label)}</button>`).join('')}</div></div>`
      + `<div class="rw col" id="thrRow"><div class="bar-hd" style="margin:0"><b>灵敏度</b><span id="thrTxt"></span></div><div class="bar"><input type="range" class="range" id="thrRange" min="0" max="100" step="1" aria-label="唤醒灵敏度" data-pad="14"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M8 8.5v7M16 8.5v7M4 11v2M20 11v2"/></svg><span class="bv" id="thrNum"></span></div><div class="opts three" role="group" aria-label="灵敏度预设"><button type="button" data-c="thr-preset" data-v="0.40" class="${curThr > 0.34 ? 'on' : ''}">防误醒</button><button type="button" data-c="thr-preset" data-v="0.25" class="${curThr > 0.19 && curThr <= 0.34 ? 'on' : ''}">平衡推荐</button><button type="button" data-c="thr-preset" data-v="0.16" class="${curThr <= 0.19 ? 'on' : ''}">高灵敏</button></div></div>`
      + row('唤醒二次确认', '醒来前再听一遍，更准但慢 2 秒左右', sw('assistant', 'wake_verify', a.wake_verify, '唤醒二次确认'))
      + adv('wake', '高级唤醒调优', `放大 ${kwsGain} dB · 加权 ${Number(kwsScore).toFixed(1)} · 细致 ${kwsPaths}`,
        row('远处说话放大', '离得远、声音小时调大', step('assistant', 'kws_gain_db', a.kws_gain_db, 0, 24, 2, x => x + ' dB'))
        + row('唤醒词加权', '越大越容易醒', step('assistant', 'kws_score', a.kws_score, 1, 4, .5, x => Number(x).toFixed(1)))
        + row('识别细致度', '越大越不漏听，也多用点处理器', step('assistant', 'kws_paths', a.kws_paths, 4, 16, 2, x => x))));
    h += grp('听和答');
    const fu = val('assistant', 'follow_up_seconds', a.follow_up_seconds);
    const asr = v && v.asr && v.asr.engines.length > 1 ? `<div class="rw col"><div class="lb"><b>听写用谁</b><span>把说的话转成文字</span></div><div class="opts">${v.asr.engines.map(e => `<button type="button" class="${e.id === val('assistant', 'asr_engine', v.asr.selected) ? 'on' : ''}" data-c="seg" data-p="assistant" data-k="asr_engine" data-v="${esc(e.id)}">${esc(e.label)}<small>${esc(e.note)}</small></button>`).join('')}</div></div>` : '';
    const listenAdv = v ? adv('listen', '高级收音与断句',
      `停顿 ${(val('assistant', 'silence_ms', v.vad.silence_ms) / 1000).toFixed(1)} 秒 · 门槛 ${Number(val('assistant', 'vad_threshold', v.vad.threshold)).toFixed(2)} · 结束 ${val('assistant', 'no_voice_close_sec', v.session.no_voice_close_sec)} 秒`,
      row('说完停多久算说完', '停顿多、说得慢就调长', step('assistant', 'silence_ms', v.vad.silence_ms, 300, 2000, 100, x => (x / 1000).toFixed(1) + ' 秒'))
      + row('收音门槛', '环境吵就调高，免得把杂音当说话', step('assistant', 'vad_threshold', v.vad.threshold, .2, .8, .05, x => Number(x).toFixed(2)))
      + row('多久没说话算聊完', '聊完后再叫，从新的话题开始', step('assistant', 'no_voice_close_sec', v.session.no_voice_close_sec, 30, 300, 30, x => x + ' 秒'))) : '';
    h += box(row('应答提示音', '喊醒时的声音', seg('assistant', 'wake_sound', a.wake_sound, [['zai', '叮＋在'], ['chime', '只叮一声'], ['off', '不出声']]))
      + row('回答完继续听', fu ? '这段时间里不用再喊小智' : '每次都要先喊小智', step('assistant', 'follow_up_seconds', a.follow_up_seconds, 0, 30, 2, x => x ? x + ' 秒' : '不听'))
      + asr + listenAdv);
    if (v) {
      const t = v.tts, eng = val('assistant', 'tts_engine', t.selected);
      h += grp('音色', '改完下一次对话生效');
      let tb = t.engines.length > 1 ? `<div class="rw col"><div class="lb"><b>用哪个声音</b></div><div class="opts${t.engines.length === 3 ? ' three' : ''}">${t.engines.map(e => `<button type="button" class="${e.id === eng ? 'on' : ''}" data-c="seg" data-p="assistant" data-k="tts_engine" data-v="${esc(e.id)}">${esc(e.label)}<small>${esc(e.id === 'IndexStreamTTS' ? '最自然' : e.id === 'EdgeTTS' ? '音色多' : '自定义')}</small></button>`).join('')}</div></div>` : '';
      if (eng === 'EdgeTTS') {
        const ev = val('assistant', 'edge_voice', t.edge.voice);
        const curV = t.edge_voices.find(x => x.value === ev) || t.edge_voices[0];
        const curVSub = curV ? (curV.group === '普通话' ? curV.gender + '声 · ' + String(curV.style || '').split(/[，、]/)[0] : curV.group) : '';
        tb += `<button type="button" class="rw tap" data-c="adv" data-v="edgeVoices" aria-expanded="${!!advOpen.edgeVoices}"><div class="lb"><b>选择音色</b><span>点开可换普通话、粤语或方言</span></div><span class="val"><b>${esc(curV ? curV.name : ev)}</b>${curVSub ? ' · ' + esc(curVSub) : ''}${chev(!!advOpen.edgeVoices)}</span></button>`
          + `<div class="adv-body${advOpen.edgeVoices ? ' open' : ''}"><div class="rw col"><div class="opts">${t.edge_voices.map(x => `<button type="button" class="${x.value === ev ? 'on' : ''}" data-c="seg" data-p="assistant" data-k="edge_voice" data-v="${esc(x.value)}">${esc(x.name)}<small>${esc(x.group === '普通话' ? x.gender + '声 · ' + String(x.style || '').split(/[，、]/)[0] : x.group)}</small></button>`).join('')}</div></div></div>`
          + row('语速', '', step('assistant', 'edge_rate', t.edge.rate, -50, 50, 5, x => (x > 0 ? '+' : '') + x + '%'))
          + row('音调', '', step('assistant', 'edge_pitch', t.edge.pitch, -50, 50, 5, x => (x > 0 ? '+' : '') + x))
          + row('说话音量', '', step('assistant', 'edge_volume', t.edge.volume, -50, 100, 10, x => (x > 0 ? '+' : '') + x + '%'));
      } else if (eng === 'OpenAITTS' && t.openai) tb += row('语速', '', step('assistant', 'openai_speed', t.openai.speed, .5, 2, .1, x => Number(x).toFixed(1) + ' 倍'));
      h += box(tb || row('音色', '', `<span class="val"><b>${esc(t.voice || '默认')}</b></span>`));
      const histOn = !!val('assistant', 'history', v.history.enabled);
      h += grp('记忆');
      h += box(row('记住聊过的', '断开、重启后还接得上', sw('assistant', 'history', v.history.enabled, '记住聊过的'))
        + row('每次带上最近几句', '越多越连贯，也越慢', step('assistant', 'max_messages', v.history.max_messages, 2, 40, 2, x => x + ' 句'), histOn ? '' : 'off')
        + row('总结成长期记忆', '更早的对话自动浓缩', sw('assistant', 'summary', v.history.summary, '总结成长期记忆'), histOn ? '' : 'off')
        + row('叫名字换模型', '说出模型名就换它来答', sw('assistant', 'call_names', v.call_names, '叫名字换模型')));
    } else h += `<p class="note">语音网关正在启动，音色和记忆过一会儿再看。</p>`;
    put('voiceBody', h);
    const r = $('thrRange');
    if (r && !r.__bound) {
      r.__bound = true;
      bindSmartSlider(r, p => { r.__drag = true; r.value = p; setBarPos(r, p); thrLabel(pctToThr(p)); syncThrPresets(pctToThr(p)); },
                      p => { r.__drag = false; r.value = p; setBarPos(r, p); thrLabel(pctToThr(p)); syncThrPresets(pctToThr(p)); change('assistant', { kws_threshold: pctToThr(p) }, 250); });
    }
    if (r && !r.__drag) { const t = val('assistant', 'kws_threshold', a.kws_threshold); r.value = thrToPct(t); setBarPos(r, thrToPct(t)); thrLabel(t); syncThrPresets(t); }
  }
  function thrLabel(t) { $('thrTxt').textContent = thrWord(t) + (t <= .14 ? '，可能会误醒' : ''); $('thrNum').textContent = thrToPct(t); }

  /* ---- 天气：输入搜索找城市（拼音 / 中文均可，不重建输入框以保住焦点与虚拟键盘） ---- */
  const wxSearch = {
    main: { q: '', items: [], busy: false, err: '', seq: 0, timer: 0 },
    extra: { q: '', items: [], busy: false, err: '', seq: 0, timer: 0 },
  };
  async function runCitySearch(slot, immediate) {
    const st = wxSearch[slot];
    const inp = $(slot === 'main' ? 'wxMainInp' : 'wxExtraInp');
    const clr = $(slot === 'main' ? 'wxMainClear' : 'wxExtraClear');
    const q = (inp ? inp.value : '').trim();
    st.q = q;
    if (clr) clr.hidden = !inp || !inp.value.length;
    clearTimeout(st.timer);
    if (!q) {
      ++st.seq;
      st.items = [];
      st.busy = false;
      st.err = '';
      paintWeatherResults(slot);
      return;
    }
    const exec = async () => {
      const mySeq = ++st.seq;
      st.busy = true;
      st.err = '';
      paintWeatherResults(slot);
      const j = await api('/api/settings/cities?q=' + encodeURIComponent(q));
      if (mySeq !== st.seq) return;
      st.busy = false;
      if (j && j.ok) {
        st.items = Array.isArray(j.items) ? j.items : [];
        st.err = st.items.length ? '' : `没找到与「${q}」匹配的城市，可试试拼音（如 beijing）或区县名`;
      } else {
        st.items = [];
        st.err = (j && j.error) || '搜索失败，请稍后再试';
      }
      paintWeatherResults(slot);
    };
    if (immediate) exec();
    else st.timer = setTimeout(exec, 260);
  }
  function clearCitySearch(slot) {
    const st = wxSearch[slot];
    clearTimeout(st.timer);
    ++st.seq;
    st.q = '';
    st.items = [];
    st.busy = false;
    st.err = '';
    const inp = $(slot === 'main' ? 'wxMainInp' : 'wxExtraInp');
    const clr = $(slot === 'main' ? 'wxMainClear' : 'wxExtraClear');
    if (inp && inp.value) inp.value = '';
    if (clr) clr.hidden = true;
    paintWeatherResults(slot);
  }
  function paintWeatherResults(slot) {
    const h0 = D.home;
    if (!h0) return;
    const city = val('home', 'city', h0.city) || '';
    const ex = val('home', 'extra_cities', h0.extra_cities) || [];
    const st = wxSearch[slot];
    const resId = slot === 'main' ? 'wxMainRes' : 'wxExtraRes';
    if (st.busy) {
      put(resId, '<div class="city-empty">正在搜索匹配的城市…</div>');
      return;
    }
    if (st.err) {
      put(resId, `<div class="city-empty">${esc(st.err)}</div>`);
      return;
    }
    if (!st.items.length) {
      put(resId, '');
      return;
    }
    const act = slot === 'main' ? 'pick-main-city' : 'add-extra-city';
    const html = st.items.map(it => {
      const name = String(it.name || '');
      const on = slot === 'main' ? (name === city) : (ex.includes(name) || name === city);
      const sub = slot === 'main'
        ? (name === city ? '当前主城市' : (it.sub || '点击设为主页天气'))
        : (name === city ? '已是主页城市' : ex.includes(name) ? '已在常看列表' : (it.sub ? '＋ ' + it.sub : '＋ 点击添加'));
      return `<button type="button" class="city-hit${on ? ' on' : ''}" data-c="${act}" data-v="${esc(name)}"><b>${esc(name)}</b><small>${esc(sub)}</small></button>`;
    }).join('');
    put(resId, html);
  }
  function paintWeather() {
    const h0 = D.home;
    const msgEl = $('wxStateMsg'), bodyEl = $('weatherBody');
    if (!msgEl || !bodyEl) return;
    if (h0 === undefined) {
      msgEl.hidden = false;
      bodyEl.hidden = true;
      put('wxStateMsg', '正在读取…');
      return;
    }
    if (h0 === null) {
      msgEl.hidden = false;
      bodyEl.hidden = true;
      put('wxStateMsg', (loadedAt.home || failedPart.home) ? '主页服务没有响应，过一会儿再打开' : '正在读取…');
      return;
    }
    msgEl.hidden = true;
    bodyEl.hidden = false;
    const city = val('home', 'city', h0.city) || '—';
    const ex = val('home', 'extra_cities', h0.extra_cities) || [];
    if ($('wxMainCur').textContent !== city) $('wxMainCur').textContent = city;
    const cntTxt = `${ex.length} / 8`;
    if ($('wxExtraCount').textContent !== cntTxt) $('wxExtraCount').textContent = cntTxt;
    const tagsHtml = ex.length
      ? ex.map(c => `<button type="button" class="city-tag" data-c="rm-extra-city" data-v="${esc(c)}" aria-label="移除 ${esc(c)}"><span>${esc(c)}</span><i>×</i></button>`).join('')
      : '<span class="city-empty">还没有常看城市，在下方输入搜索即可添加</span>';
    put('wxExtraTags', tagsHtml);
    paintWeatherResults('main');
    paintWeatherResults('extra');
  }
  for (const [slot, inpId, btnId, clrId] of [
    ['main', 'wxMainInp', 'wxMainBtn', 'wxMainClear'],
    ['extra', 'wxExtraInp', 'wxExtraBtn', 'wxExtraClear'],
  ]) {
    $(inpId).addEventListener('input', () => runCitySearch(slot, false));
    $(inpId).addEventListener('keydown', e => {
      if (e.key === 'Enter') {
        e.preventDefault();
        runCitySearch(slot, true);
      }
    });
    $(btnId).addEventListener('click', () => runCitySearch(slot, true));
    $(clrId).addEventListener('click', () => clearCitySearch(slot));
  }

  /* ---- 音乐（独立一栏） ---- */
  function paintMusic() {
    const m = D.music;
    if (m === undefined) return;
    let h = '';
    if (m) {
      h += grp('播放与音质', m.logged_in ? (m.vip ? 'QQ 音乐已登录 · 会员' : 'QQ 音乐已登录') : '还没登录 QQ 音乐');
      h += box(row('在线音质', '', seg('music', 'quality_level', m.quality_level, [['lossless', '无损'], ['hq', '高品质'], ['standard', '标准']]))
        + row('播放顺序', '', seg('music', 'play_mode', m.play_mode, [['loop', '列表循环'], ['single', '单曲'], ['shuffle', '随机']]))
        + row('电台和听书', '', seg('music', 'radio_audio_mode', m.radio_audio_mode, [['hifi', '高保真'], ['voice', '听人声'], ['raw', '原声']]))
        + row('缓存上限', '超过就清掉最早的', step('music', 'max_cache_mb', m.max_cache_mb, 50, 500, 50, x => x + ' MB'))
        + `<button type="button" class="rw tap" data-c="open-music"><div class="lb"><b>QQ 音乐、电台与助眠声景</b><span>${m.logged_in ? '打开曲库、76 台广播或白噪音' : '打开并扫码登录 QQ 音乐账号'}</span></div><span class="val">${m.logged_in ? '打开音乐' : '扫码登录'}<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 5l7 7-7 7"/></svg></span></button>`);
    } else if ((failedPart.music && m === null) || (loadedAt.music && m === null)) h += down('音乐服务');
    put('musicBody', h || '<div class="loading">正在读取…</div>');
  }

  /* ---- 学习 ---- */
  function paintStudy() {
    const s = D.study;
    if (s === undefined) return;
    if (!s) return put('studyBody', down('学习陪伴'));
    const sec = x => x >= 60 && x % 60 === 0 ? x / 60 + ' 分钟' : x + ' 秒';
    const bf = val('study', 'burst_frames', s.burst_frames);
    const cd = val('study', 'cooldown', s.cooldown);
    put('studyBody', grp('学习陪伴', s.enabled ? '正在陪伴' : '在「专注睡眠」里开关') + box(
      `<button type="button" class="rw tap" data-c="open-focus"><div class="lb"><b>专注计时与坐姿守护</b><span>在主页「专注睡眠」里开启陪伴或番茄钟</span></div><span class="val">打开专注<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 5l7 7-7 7"/></svg></span></button>`
      + row('多久看一次', '摄像头看一眼坐姿和专注', step('study', 'interval', s.interval, 30, 300, 30, sec))
      + row('自动关闭', '', step('study', 'auto_off_hours', s.auto_off_hours, 0, 12, 1, x => x ? x + ' 小时后' : '不自动'))
      + adv('study', '高级识别与提醒间隔', `连拍 ${bf} 张 · 冷却 ${sec(cd)}`,
        row('每次连拍', '几张一起判断，免得误报', step('study', 'burst_frames', s.burst_frames, 1, 4, 1, x => x + ' 张'))
        + row('连拍间隔', '', step('study', 'burst_gap', s.burst_gap, 1, 8, 1, x => x + ' 秒'))
        + row('同一件事多久再提醒', '', step('study', 'cooldown', s.cooldown, 60, 1800, 60, sec))
        + row('两次提醒至少隔', '', step('study', 'min_gap', s.min_gap, 30, 600, 30, sec))))
      + grp('AI 讲题') + box(`<button type="button" class="rw tap" data-c="tutor"><div class="lb"><b>讲题设置</b><span>年级、讲解声音、高拍仪方向和补光，在讲题页右上角</span></div><span class="val">打开讲题<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 5l7 7-7 7"/></svg></span></button>`));
  }

  /* ---- 系统 ---- */
  const gb = n => (n / 1073741824).toFixed(1) + ' GB';
  function paintSys() {
    const s = D.system;
    if (!s) return;
    const up = s.uptime == null ? '—' : s.uptime >= 86400 ? `${Math.floor(s.uptime / 86400)} 天 ${Math.floor(s.uptime % 86400 / 3600)} 小时` : s.uptime >= 3600 ? `${Math.floor(s.uptime / 3600)} 小时 ${Math.floor(s.uptime % 3600 / 60)} 分` : `${Math.floor(s.uptime / 60)} 分钟`;
    const f = (k, v, cls = '', extra = '') => `<div class="fact"><small>${esc(k)}</small><b class="${cls}">${esc(v)}</b>${extra}</div>`;
    let diskText = '—', diskBar = '';
    if (s.disk && s.disk.total > 0) {
      const pct = Math.min(100, Math.max(0, Math.round(s.disk.used / s.disk.total * 100)));
      const bcls = pct >= 85 ? 'bad' : pct >= 70 ? 'warn' : '';
      diskText = `${gb(s.disk.used)} / ${gb(s.disk.total)} · ${pct}%`;
      diskBar = `<div class="fact-bar"><i class="${bcls}" style="width:${pct}%"></i></div>`;
    }
    put('facts', f('语音', s.audio && s.hub ? '正常' : s.audio ? '网关没响应' : '音频异常', s.audio && s.hub ? 'ok' : 'bad')
      + f('麦克风', s.mic === false ? '机身开关已关' : '已打开', s.mic === false ? 'bad' : 'ok')
      + f('地址', s.ip || '没有联网') + f('开机', up)
      + f('存储', diskText, '', diskBar)
      + f('内存 · 温度', `${s.mem ? '空闲 ' + gb(s.mem.available) : '—'}${s.temp != null ? ` · ${s.temp}°` : ''}`));
  }

  function paint() {
    if (!$('settings').classList.contains('on')) return;
    if (cur === 'voice') paintVoice();
    else if (cur === 'weather') paintWeather();
    else if (cur === 'music') paintMusic();
    else if (cur === 'study') paintStudy();
    else if (cur === 'sys') paintSys();
  }

  /* ---- 点按 ---- */
  $('settingsPanel').addEventListener('click', e => {
    const t = elTarget(e);
    const b = t && t.closest && t.closest('[data-c]');
    if (!b) return;
    const { c, p, k } = b.dataset;
    if (c === 'sw') change(p, { [k]: b.getAttribute('aria-checked') !== 'true' });
    else if (c === 'seg') {
      if (p === 'assistant' && k === 'edge_voice') advOpen.edgeVoices = false;
      if (!b.classList.contains('on')) change(p, { [k]: b.dataset.v });
      else if (p === 'assistant' && k === 'edge_voice') paint();
    } else if (c === 'thr-preset') {
      const thr = Number(b.dataset.v);
      change('assistant', { kws_threshold: thr }, 120);
    } else if (c === 'adv') {
      advOpen[b.dataset.v] = !advOpen[b.dataset.v];
      paint();
    } else if (c === 'step') {
      if (b.disabled) return;
      const lo = +b.dataset.lo, hi = +b.dataset.hi, d = +b.dataset.d, now = +val(p, k, +b.dataset.now);
      const next = Math.max(lo, Math.min(hi, Math.round((now + d) * 100) / 100));
      if (next !== now) change(p, { [k]: next }, p === 'assistant' ? 900 : 500);
    } else if (c === 'word') {
      const on = val('assistant', 'wake_words', D.assistant.wake_words.filter(w => w.on).map(w => w.raw));
      const next = on.includes(b.dataset.v) ? on.filter(w => w !== b.dataset.v) : [...on, b.dataset.v];
      if (!next.length) return toast('至少要留一个唤醒词');
      change('assistant', { wake_words: D.assistant.wake_words.map(w => w.raw).filter(w => next.includes(w)) }, 900);
    } else if (c === 'pick-main-city') {
      const chosen = b.dataset.v;
      if (!chosen) return;
      blurSheetInput();
      clearCitySearch('main');
      change('home', { city: chosen });
    } else if (c === 'add-extra-city') {
      const chosen = b.dataset.v;
      if (!chosen || !D.home) return;
      const mainCity = val('home', 'city', D.home.city) || '';
      if (chosen === mainCity) return toast(`「${chosen}」已经是主页天气城市`);
      const ex = (val('home', 'extra_cities', D.home.extra_cities) || []).slice();
      if (ex.includes(chosen)) return toast(`「${chosen}」已在常看城市里`);
      if (ex.length >= 8) return toast('最多 8 个，请先点 × 移除一个');
      ex.push(chosen);
      blurSheetInput();
      clearCitySearch('extra');
      change('home', { extra_cities: ex });
    } else if (c === 'rm-extra-city') {
      if (!D.home) return;
      const ex = (val('home', 'extra_cities', D.home.extra_cities) || []).filter(x => x !== b.dataset.v);
      change('home', { extra_cities: ex }, 200);
    } else if (c === 'tutor') { $('settingsClose').click(); if (window.__tsOpenSub) window.__tsOpenSub('http://127.0.0.1:8096/'); }
    else if (c === 'open-music') { $('settingsClose').click(); if (window.__tsOpenSub) window.__tsOpenSub('http://127.0.0.1:8091/'); }
    else if (c === 'open-weather') { $('settingsClose').click(); if (window.__tsOpenHomeWeather) window.__tsOpenHomeWeather(val('home', 'city', D.home && D.home.city) || ''); }
    else if (c === 'open-focus') { $('settingsClose').click(); if (window.__tsOpenHomeFocus) window.__tsOpenHomeFocus(); }
  });
  // 重启：点两下
  const armed = new WeakMap();
  for (const b of document.querySelectorAll('#settingsPanel [data-act]')) b.addEventListener('click', async () => {
    if (!(armed.get(b) > Date.now())) {
      armed.set(b, Date.now() + 5000); b.textContent = '确认重启'; b.classList.add('armed');
      return setTimeout(() => { if (!(armed.get(b) > Date.now())) { b.textContent = '重启'; b.classList.remove('armed'); } }, 5100);
    }
    armed.delete(b); b.textContent = '重启'; b.classList.remove('armed'); b.disabled = true;
    const j = await api('/api/settings', { part: 'system', values: { action: b.dataset.act } });
    toast(j.ok ? j.note : (j.error || '没能重启'), j.ok ? '' : 'error');
    setTimeout(() => { b.disabled = false; }, 8000);
  });

  /* ---- 打开 / 换栏：读这一栏要的 ---- */
  addEventListener('ts-settings-section', e => {
    cur = e.detail;
    for (const p of [].concat(PART[cur] || [])) load(p);
    paint();
  });
  let sysTimer = 0;
  new MutationObserver(() => {
    const on = $('settings').classList.contains('on');
    clearInterval(sysTimer); sysTimer = 0;
    if (on) { for (const p of [].concat(PART[cur] || [])) load(p); sysTimer = setInterval(() => { if (cur === 'sys' && !document.hidden) load('system', true); }, 10000); paint(); }
    else {
      for (const p of Object.keys(pending)) flush(p);
      clearCitySearch('main');
      clearCitySearch('extra');
      advOpen.wake = advOpen.listen = advOpen.edgeVoices = advOpen.study = false;
      setSaveBadge('');
    }
  }).observe($('settings'), { attributes: true, attributeFilter: ['class'] });

  /* ---- 管理后台的二维码 + 全栏设置静默常驻预热 ---- */
  let qrUrl = null, qrWantUrl = '', qrScript = null;
  function ensureQrLib(done) {
    if (typeof window.qrcode === 'function') { if (done) done(); return; }
    if (qrScript) { if (done) qrScript.addEventListener('load', done, { once: true }); return; }
    const s = document.createElement('script');
    qrScript = s;
    s.src = '/qrcode.js';
    s.onload = () => { qrScript = null; if (done) done(); };
    s.onerror = () => { s.remove(); qrScript = null; qrUrl = null; };
    document.head.appendChild(s);
  }
  addEventListener('ts-admin-url', e => {
    const nextUrl = e.detail || '';
    qrWantUrl = nextUrl;
    if (nextUrl === qrUrl && (!nextUrl || typeof window.qrcode === 'function')) return;
    const draw = () => {
      if (!qrWantUrl) { qrUrl = ''; $('adminQr').innerHTML = ''; return; }
      if (typeof window.qrcode !== 'function') { $('adminQr').innerHTML = ''; return; }
      try {
        const q = window.qrcode(0, 'M');
        q.addData(qrWantUrl);
        q.make();
        $('adminQr').innerHTML = q.createSvgTag({ cellSize: 4, margin: 0, scalable: true });
        qrUrl = qrWantUrl;
      } catch (err) { qrUrl = null; $('adminQr').innerHTML = ''; }
    };
    if (!nextUrl || typeof window.qrcode === 'function') return draw();
    ensureQrLib(draw);
  });

  async function prewarmAllSettings() {
    ensureQrLib();
    const j = await api('/api/settings?part=all');
    if (j && j.ok) {
      take(j);
      const now = Date.now();
      for (const p of ['assistant', 'home', 'music', 'study', 'system']) {
        if (D[p] !== undefined && D[p] !== null) loadedAt[p] = now;
      }
      paint();
    }
  }
  setTimeout(prewarmAllSettings, 1800);
})();
