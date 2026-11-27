(function() {
  // Headless voice-UI bridge on the 小智 kiosk page (127.0.0.1:8090).
  // 2026-11-10 (Round 109 S21): bottom QQ音乐 dock/mini-player removed per user preference
  // (all music touch controls & seek bar live on the Home page); keeps only the lightweight
  // open_ui_seq watcher so voice commands ("小麦小麦，打开QQ音乐") still open #tsSubLayer.
  if (window.location.port !== '8090' || window.__tsQqVoiceBridge) return;
  window.__tsQqVoiceBridge = true;

  const API = 'http://127.0.0.1:8091';
  let lastBootId = null;
  let lastSeq = null;
  let tickBusy = false;
  let tickPending = false;
  let tickTimer = 0;
  let tickDueAt = 0;
  let pendingOpenTimer = 0;
  let pendingOpenAt = 0;
  let wantOpenOnWake = false;
  let pendingSubCmd = '';

  function tabToSubCmd(tab) {
    return tab === 'radio' ? 'openRadio' : (tab === 'account' ? 'openAccount' : (tab === 'fav' ? 'openFav' : ''));
  }

  function openQqMusic(retries = 0, subCmd = undefined) {
    clearTimeout(pendingOpenTimer);
    pendingOpenTimer = 0;
    wantOpenOnWake = false;
    if (document.body.classList.contains('ringing')) {
      pendingSubCmd = '';
      return;
    }
    const cmd = subCmd !== undefined ? subCmd : (pendingSubCmd || '');
    if (typeof window.__tsOpenSub === 'function') {
      pendingSubCmd = '';
      pendingOpenAt = 0;
      window.__tsOpenSub(API + '/', cmd);
    } else if (retries < 25) {
      if (retries === 0) pendingOpenAt = Date.now();
      pendingSubCmd = cmd;
      pendingOpenTimer = setTimeout(() => openQqMusic(retries + 1, cmd), 200);
    } else {
      pendingSubCmd = '';
      pendingOpenAt = 0;
    }
  }

  const xzActive = () =>
    (document.body.dataset.state || 'idle') !== 'idle';
  let lastVoiceBusyAt = xzActive() ? Date.now() : 0;
  let lastExtSyncAt = 0;

  function applyMusicStatus(st, fromSelf = false) {
    if (!st || typeof st !== 'object') return;
    if (!fromSelf) lastExtSyncAt = Date.now();
    const seq = Number(st.open_ui_seq) || 0;
    const fresh = st.open_ui_fresh === undefined ? seq > 0 : st.open_ui_fresh === true;
    if (lastSeq === null || (st.boot_id && lastBootId !== null && st.boot_id !== lastBootId)) {
      const restarted = lastBootId !== null && !!st.boot_id && st.boot_id !== lastBootId;
      lastBootId = st.boot_id || lastBootId;
      lastSeq = seq;
      if (((restarted && seq > 0 && fresh) || (st.open_ui_fresh === true && seq > 0 && (lastVoiceBusyAt > 0 || xzActive()))) && !st.alarm_ringing && !document.body.classList.contains('ringing')) {
        openQqMusic(0, tabToSubCmd(st.open_ui_tab));
      }
    } else {
      lastBootId = st.boot_id || lastBootId;
      if (seq !== lastSeq) {
        const increased = seq > lastSeq;
        lastSeq = seq;
        if (increased && fresh && !st.alarm_ringing && !document.body.classList.contains('ringing')) {
          openQqMusic(0, tabToSubCmd(st.open_ui_tab));
        }
      }
    }
  }
  window.__tsQqSyncStatus = applyMusicStatus;

  function scheduleTick(delayMs) {
    const now = Date.now();
    if (xzActive()) lastVoiceBusyAt = now;
    const voiceWindow = xzActive() || (lastVoiceBusyAt > 0 && now - lastVoiceBusyAt < 12000);
    if (delayMs === undefined && !voiceWindow) {
      clearTimeout(tickTimer);
      tickTimer = 0;
      tickDueAt = 0;
      return;
    }
    const ms = delayMs !== undefined ? delayMs : 1200;
    if (tickTimer && delayMs !== 0 && tickDueAt > 0 && tickDueAt - now <= ms) return;
    clearTimeout(tickTimer);
    tickTimer = 0;
    tickDueAt = 0;
    if (document.hidden) return;
    tickDueAt = now + ms;
    tickTimer = setTimeout(() => { tickTimer = 0; tickDueAt = 0; tick(); }, ms);
  }

  async function tick() {
    if (document.hidden) return;
    if (document.documentElement.classList.contains('ts-home') &&
        !document.documentElement.classList.contains('ts-homeload') &&
        !document.documentElement.classList.contains('ts-sub') &&
        !document.documentElement.classList.contains('ts-bopen') &&
        !xzActive() &&
        lastExtSyncAt > 0 && (Date.now() - lastExtSyncAt) < 8000 &&
        !(document.getElementById('settings')?.classList.contains('on') ||
          document.getElementById('wifi')?.classList.contains('on') ||
          document.getElementById('sheet')?.classList.contains('on')) &&
        typeof window.__tsQqSyncStatus === 'function') {
      scheduleTick();
      return;
    }
    if (tickBusy) { tickPending = true; return; }
    tickBusy = true;
    tickPending = false;
    const ctrl = new AbortController();
    const tm = setTimeout(() => ctrl.abort(), 3000);
    try {
      const hdrs = tick.etag ? { 'If-None-Match': tick.etag } : undefined;
      const r = await fetch(API + '/api/music/status?lite=1', { cache: 'no-store', headers: hdrs, signal: ctrl.signal });
      if (r.status === 304) return;
      if (!r.ok) return;
      const et = r.headers.get('ETag');
      if (et) tick.etag = et;
      const st = await r.json();
      if (document.hidden) return;
      applyMusicStatus(st, true);
    } catch (e) {} finally {
      clearTimeout(tm);
      tickBusy = false;
      if (tickPending && !document.hidden) {
        tickPending = false;
        tick();
      } else {
        scheduleTick();
      }
    }
  }

  new MutationObserver(() => {
    if (xzActive()) {
      lastVoiceBusyAt = Date.now();
      scheduleTick(250);
    }
  }).observe(document.body, { attributes: true, attributeFilter: ['data-state', 'class'] });

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      let openNow = false;
      if (wantOpenOnWake) {
        wantOpenOnWake = false;
        if (pendingOpenAt && (Date.now() - pendingOpenAt) < 12000) {
          openNow = true;
          openQqMusic();
        } else {
          pendingSubCmd = '';
          pendingOpenAt = 0;
        }
      }
      if (openNow || xzActive()) scheduleTick(0);
      else scheduleTick();
    } else {
      clearTimeout(tickTimer); tickTimer = 0;
      tickDueAt = 0;
      if (pendingOpenTimer) { clearTimeout(pendingOpenTimer); pendingOpenTimer = 0; wantOpenOnWake = true; }
    }
  });

  tick();
})();
