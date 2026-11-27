// ThinkSmart kiosk overlay (round 109, 2026-11-09). Injected by thinksmart-kiosk.py into every
// top-level page as a WebKit UserScript.
//  * 127.0.0.1:8090 (the base page; the voice client serves it):
//    - Lays our home page (thinksmart-home, 127.0.0.1:8095) over it as a persistent frame (#tsHomeLayer, z-index 9000).
//    - Opens sub-pages (QQ音乐 :8091, 服务器监控 :8092, AI讲题 :8096) in an upper full-screen frame (#tsSubLayer, z-index 9100),
//      so 「主页」 reveals the already-warm home page immediately.
//    - Opens the settings / model / Wi-Fi sheets (z-index 9200+) directly over whatever is on screen.
//    - 2026-11-25 (Claude, the user's decision): 小麦 has no page of its own. Waking it shows a capsule at the top that
//      grows into a card (question, answer, and a card for what was done); the conversation is in 设置 → 对话.
//      Alarms ring over the current page; missed alarms and running countdowns show as small notices.
//    - If the home service does not answer, the base page's own clock and shortcuts stay usable (retried every 15 s).
//  * Unread phone messages (#tsMsgCard, z-index 9500) sit above home and sub-pages until 「收到」.
//  * 8092 (server monitor): settings button in the header; when framed inside #tsSubLayer,
//    「主页」 and 「设置」 message the parent overlay instead of navigating top-level.
(() => {
  'use strict';
  if (!['127.0.0.1', 'localhost'].includes(location.hostname) || window.__tsHomeOverlay) return;
  const port = location.port, isXZ = port === '8090', isMusic = port === '8091', isMon = port === '8092';
  if (!isXZ && !isMusic && !isMon) return;
  window.__tsHomeOverlay = true;
  const framed = window.parent && window.parent !== window;
  const HOME = 'http://127.0.0.1:8095', MON = 'http://127.0.0.1:8092';
  function okSubUrl(u) {
    try {
      const p = new URL(u);
      return p.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(p.hostname) && ['8091', '8092', '8096'].includes(p.port)
        && p.pathname === '/' && !p.username && !p.password && !p.search && !p.hash;
    } catch (e) { return false; }
  }
  const $ = id => document.getElementById(id);
  const gear = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/></svg>';   // 和主页「设置」磁贴同一个图标
  const msgIco = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h16v11H8l-4 4z"/><path d="M8 9h8M8 12.5h5"/></svg>';

  const style = document.createElement('style');
  style.textContent = `
  #tsHomeLayer{position:fixed;inset:0;z-index:9000;background:#0a111e;display:block;opacity:0;visibility:hidden;pointer-events:none;contain:strict;transform:translate3d(0,0,0);backface-visibility:hidden;transition:opacity .12s cubic-bezier(.4,0,.2,1),visibility 0s linear .12s}
  #tsHomeLayer iframe{border:0;width:100%;height:100%;display:block;background:#0a111e;backface-visibility:hidden}
  html.ts-homeload #tsHomeLayer{opacity:1;visibility:visible;pointer-events:auto;transition:none}
  html.ts-homeload #tsHomeLayer iframe{opacity:0;pointer-events:none}
  html.ts-home #tsHomeLayer{opacity:1;visibility:visible;pointer-events:auto;transition:opacity .135s cubic-bezier(.22,1,.36,1),visibility 0s linear 0s}
  html.ts-sub-opaque #tsHomeLayer{visibility:hidden!important;pointer-events:none!important;transition:none!important}
  #tsSubLayer{position:fixed;inset:0;z-index:9100;background:#0a111e;display:block;opacity:0;visibility:hidden;pointer-events:none;contain:strict;transform:translate3d(0,0,0);backface-visibility:hidden;transition:opacity .115s cubic-bezier(.4,0,.2,1),visibility 0s linear .115s}
  #tsSubLayer iframe{position:absolute;inset:0;border:0;width:100%;height:100%;display:block;visibility:hidden;pointer-events:none;z-index:0;background:#0a111e;backface-visibility:hidden}
  #tsSubLayer.ready iframe.active,#tsSubLayer iframe.leaving{visibility:visible;z-index:1}
  html.ts-sub #tsSubLayer.ready iframe.active{pointer-events:auto}
  #tsSubLoading{position:absolute;inset:0;z-index:2;display:none;align-items:center;justify-content:center;flex-direction:column;gap:20px;background:radial-gradient(500px 350px at 50% 45%,#1b3154,#0a111e);color:#b8cee9;font:700 23px -apple-system,"Noto Sans CJK SC",sans-serif;pointer-events:none}
  html.ts-sub #tsSubLayer:not(.ready) #tsSubLoading{display:flex}
  #tsSubLoading .ts-dots{display:flex;gap:10px}#tsSubLoading i{width:9px;height:9px;border-radius:50%;background:#81cefc}
  html.ts-sub #tsSubLayer:not(.ready) #tsSubLoading i{animation:tsLoad 1.2s steps(8) infinite alternate}
  #tsSubLoading i:nth-child(2){animation-delay:.2s}#tsSubLoading i:nth-child(3){animation-delay:.4s}
  @keyframes tsLoad{from{opacity:.25;transform:translateY(0)}to{opacity:1;transform:translateY(-5px)}}
  html.ts-sub #tsSubLayer{opacity:1;visibility:visible;pointer-events:auto;transition:opacity .135s cubic-bezier(.22,1,.36,1),visibility 0s linear 0s}
  #tsSubErr{position:absolute;inset:0;z-index:3;display:none;flex-direction:column;align-items:center;justify-content:center;gap:18px;background:#0a111e;color:#e8f0fc;font-family:-apple-system,BlinkMacSystemFont,"Noto Sans CJK SC",sans-serif}
  #tsSubErr.show{display:flex}
  #tsSubErr p{margin:0;font-size:22px;font-weight:700;color:#9fb2cd}
  #tsSubErr button{height:54px;padding:0 28px;border:1px solid #8cb4ff40;border-radius:16px;background:#1b2b45;color:#eef4ff;font:700 18px -apple-system,"Noto Sans CJK SC",sans-serif;cursor:pointer;touch-action:manipulation}
  .app{contain:layout style paint;transition:visibility 0s linear 0s}
  html.ts-homeload .app{visibility:hidden!important;pointer-events:none!important;transition:none!important}
  html.ts-xz-covered .app{visibility:hidden!important;pointer-events:none!important;transition:visibility 0s linear .14s}
  html.ts-xz-covered body>*:not(#tsHomeLayer):not(#tsSubLayer):not(#tsMsgCard):not(#tsVoicePill):not(#tsNotice):not(#sheet):not(#settings):not(#wifi):not(#toast):not(#hud):not(#powerOff):not(#ring):not(#tsRotCover):not(#tsVoiceCard):not(#tsOrHint):not(.tskb-dock):not(style):not(script){visibility:hidden!important;pointer-events:none!important;transition:visibility 0s linear .14s}
  #settings,#wifi,#sheet{z-index:9200}
  #wifi{z-index:9205}
  #sheet{z-index:9210}
  #toast{z-index:9820}
  #hud{z-index:9850}
  #powerOff{z-index:9900}
  html.ts-home #settings,html.ts-home #wifi,html.ts-sub #settings,html.ts-sub #wifi{contain:layout style paint;transform:translate3d(0,0,0);backface-visibility:hidden;transition:opacity .115s cubic-bezier(.4,0,.2,1)!important}
  html.ts-home #settings.on,html.ts-home #wifi.on,html.ts-sub #settings.on,html.ts-sub #wifi.on{transition:opacity .14s cubic-bezier(.22,1,.36,1)!important}
  html.ts-sub #settings,html.ts-sub #wifi{transform:none;transition:none!important}
  html.ts-home #settings .sheet-bar,html.ts-home #settings .grid,html.ts-home #settings .panel,html.ts-home #wifi .sheet-bar,html.ts-home #wifi .panel,html.ts-sub #settings .sheet-bar,html.ts-sub #settings .grid,html.ts-sub #settings .panel,html.ts-sub #wifi .sheet-bar,html.ts-sub #wifi .panel{transform:none!important;transition:none!important}
  #tsBrightness{height:48px;padding:0 19px;display:flex;align-items:center;gap:9px;border:1px solid #9ec2ff29;border-radius:15px;background:#19253a;color:#eaf2ff;font:700 19px -apple-system,BlinkMacSystemFont,"Noto Sans CJK SC",sans-serif;-webkit-tap-highlight-color:transparent;touch-action:manipulation;cursor:pointer;flex-shrink:0;box-shadow:0 8px 22px rgba(0,0,0,.28);transition:transform .14s cubic-bezier(.22,1,.36,1),background-color .14s ease}
  #tsBrightness:active{background:#263d5b;transform:scale(.95);transition:transform .05s ease-out,background-color .05s ease}#tsBrightness svg{width:22px;height:22px;color:#81cefc;flex-shrink:0}
  #tsBrightness.monitor-brightness{height:48px;border-radius:13px;font-size:19px;font-weight:700;padding:0 19px;gap:9px;border:1px solid #9ec2ff29;background:#1a2840;color:#eaf2ff;box-shadow:none}
  #tsBrightness.monitor-brightness svg{width:22px;height:22px}
  @keyframes tsMsgDrop{from{opacity:0;transform:translate(-50%,-14px)}to{opacity:1;transform:translate(-50%,0)}}
  @keyframes tsAckRing{from{opacity:.85;transform:scale(.96)}to{opacity:0;transform:scale(1.14)}}
  #tsMsgCard{position:fixed;z-index:9500;top:22px;left:50%;transform:translateX(-50%);width:800px;max-width:calc(100% - 40px);display:none;gap:20px;align-items:center;padding:20px 24px 20px 28px;border-radius:24px;background:#1b2436;border:1px solid #ffc85766;box-shadow:0 10px 26px #0008;font-family:-apple-system,BlinkMacSystemFont,"Noto Sans CJK SC",sans-serif;color:#eef3fb;-webkit-user-select:none;user-select:none;contain:layout paint}
  #tsMsgCard.show{display:flex;animation:tsMsgDrop .26s cubic-bezier(.22,1,.36,1) backwards}
  #tsMsgCard .tm-b{flex:1;min-width:0}
  #tsMsgCard .tm-h{font-size:18px;font-weight:700;color:#ffd98a;display:flex;gap:8px;align-items:center}
  #tsMsgCard .tm-tag{display:inline-flex;align-items:center;gap:7px}
  #tsMsgCard .tm-tag svg{width:20px;height:20px;flex-shrink:0}
  #tsMsgCard .tm-t{font-size:28px;font-weight:700;line-height:1.42;margin-top:8px;word-break:break-all;max-height:162px;overflow:hidden;white-space:pre-line}
  #tsMsgCard button{position:relative;height:70px;min-width:132px;padding:0 26px;border:0;border-radius:18px;background:#f5c04a;color:#0a101c;font:700 22px -apple-system,"Noto Sans CJK SC",sans-serif;cursor:pointer;touch-action:manipulation;flex-shrink:0;-webkit-tap-highlight-color:transparent;transition:transform .14s cubic-bezier(.22,1,.36,1),opacity .14s ease}
  #tsMsgCard.show button::after{display:none}
  #tsMsgCard.settled button::after{animation:none;will-change:auto}
  #tsMsgCard button:active{opacity:.9;transform:scale(.95);transition:transform .05s ease-out}
  #tsMsgCard button:disabled{opacity:.6}
  @media(prefers-reduced-motion:reduce){#tsHomeLayer,#tsSubLayer,#tsSubLayer *,#tsMsgCard *,#tsMsgCard *::after{animation:none!important;transition:none!important}}
  /* 2026-11-12: brightness panel taller for the 深色 / 浅色 row and in the home page's colours; light versions of the overlay layers */
  html.ts-light #tsHomeLayer,html.ts-light #tsHomeLayer iframe,html.ts-light #tsSubLayer,html.ts-light #tsSubLayer iframe,html.ts-light #tsSubErr{background:#e3dbce}
  html.ts-light #tsSubLoading{background:#e3dbce;color:#424449}
  html.ts-light #tsSubLoading i{background:#0f6d63}
  html.ts-light #tsSubErr{color:#18191b}html.ts-light #tsSubErr p{color:#424449}
  html.ts-light #tsSubErr button,html.ts-light #tsBrightFallback button{background:#f6f2ea;border-color:#d6cdbf;color:#18191b}
  html.ts-light #tsBrightness,html.ts-light #tsBrightness.monitor-brightness{background:#f6f2ea;border-color:#d6cdbf;color:#18191b;box-shadow:0 6px 16px rgba(28,24,18,.12)}
  html.ts-light #tsBrightness svg{color:#0f6d63}
  html.ts-light #tsMsgCard{background:#f6f2ea;border-color:#dab984;color:#18191b;box-shadow:0 0 0 1px #dab984,0 10px 24px rgba(28,24,18,.18)}
  html.ts-light #tsMsgCard .tm-h{color:#84510a}
  html.ts-light #tsMsgCard button{background:#a26008;color:#faf8f4}

  /* ===== 2026-11-20 Claude：机器竖起来时整套页面转成竖屏（8090 底页和设置在 app.css 里自己转）=====
     主页、音乐、服务器的页面框转 90°（页面里拿到 800×1280，按各自的竖版排）；AI 讲题自己转，这里不转它的框；
     留言卡、灵动胶囊和卡片摆到竖屏的顶上。右边朝上 = ts-portrait，左边朝上再加 ts-rot270。 */
  html.ts-portrait #tsHomeLayer iframe,html.ts-portrait #tsSubLayer>iframe:not([data-port="8096"]),html.ts-portrait #tsSubLoading,html.ts-portrait #tsSubErr{position:absolute;inset:auto;left:0;top:0;width:100vh;height:100vw;transform-origin:0 0;transform:translateX(100vw) rotate(90deg)}
  html.ts-portrait.ts-rot270 #tsHomeLayer iframe,html.ts-portrait.ts-rot270 #tsSubLayer>iframe:not([data-port="8096"]),html.ts-portrait.ts-rot270 #tsSubLoading,html.ts-portrait.ts-rot270 #tsSubErr{transform:translateY(100vh) rotate(-90deg)}
  @keyframes tsMsgDropP{from{opacity:0;transform:rotate(90deg) translate(-50%,8px)}to{opacity:1;transform:rotate(90deg) translate(-50%,22px)}}
  @keyframes tsMsgDropQ{from{opacity:0;transform:rotate(-90deg) translate(-50%,8px)}to{opacity:1;transform:rotate(-90deg) translate(-50%,22px)}}
  html.ts-portrait #tsMsgCard{top:50%;left:100%;width:760px;max-width:none;transform-origin:0 0;transform:rotate(90deg) translate(-50%,22px)}
  html.ts-portrait #tsMsgCard.show{animation-name:tsMsgDropP}
  html.ts-portrait.ts-rot270 #tsMsgCard{left:0;transform:rotate(-90deg) translate(-50%,22px)}
  html.ts-portrait.ts-rot270 #tsMsgCard.show{animation-name:tsMsgDropQ}
  html.ts-portrait #tsMsgCard .tm-t{max-height:200px}
  /* 第二轮：转的时候先用底色盖住，新的排版画好再淡出，看不到半截的页面 */
  #tsRotCover{position:fixed;inset:0;z-index:9990;background:#0a111e;opacity:0;visibility:hidden;pointer-events:none;transition:opacity .13s ease,visibility 0s linear .13s}
  #tsRotCover.on{opacity:1;visibility:visible;transition:none}
  html.ts-light #tsRotCover{background:#e3dbce}
  /* 方向锁住后机器转了：按机器现在的方向摆一个提示，点一下改成跟随机器 */
  #tsOrHint{position:fixed;z-index:9800;display:none;height:58px;padding:0 24px;border:1px solid #9ec2ff40;border-radius:999px;background:#19253a;color:#eaf2ff;font:700 19px/24px -apple-system,BlinkMacSystemFont,"Noto Sans CJK SC",sans-serif;box-shadow:0 8px 22px rgba(0,0,0,.32);align-items:center;gap:10px;white-space:nowrap;cursor:pointer;touch-action:manipulation}
  #tsOrHint svg{width:24px;height:24px;color:#81cefc;flex:none}
  #tsOrHint.show{display:flex}
  #tsOrHint[data-phys="l"]{top:84px;left:50%;transform:translateX(-50%)}
  #tsOrHint[data-phys="p90"]{top:50%;left:100%;transform-origin:0 0;transform:rotate(90deg) translate(-50%,84px)}
  #tsOrHint[data-phys="p270"]{top:50%;left:0;transform-origin:0 0;transform:rotate(-90deg) translate(-50%,84px)}
  html.ts-light #tsOrHint{background:#f6f2ea;border-color:#d6cdbf;color:#18191b;box-shadow:0 6px 16px rgba(28,24,18,.16)}
  html.ts-light #tsOrHint svg{color:#0f6d63}
  /* 小麦的闹钟、提示条、音量条、关机提示盖在我们的页面上时，竖屏也转正 */
  #ring,html.ts-xz-covered #ring{z-index:9700}
  html.ts-portrait #ring,html.ts-portrait #powerOff{inset:auto;left:0;top:0;width:100vh;height:100vw;transform-origin:0 0;transform:translateX(100vw) rotate(90deg)}
  html.ts-portrait.ts-rot270 #ring,html.ts-portrait.ts-rot270 #powerOff{transform:translateY(100vh) rotate(-90deg)}
  html.ts-portrait #settings,html.ts-portrait #wifi{inset:auto;left:0;top:0;width:100vh;height:100vw;transform-origin:0 0;transform:translateX(100vw) rotate(90deg)}
  html.ts-portrait.ts-rot270 #settings,html.ts-portrait.ts-rot270 #wifi{transform:translateY(100vh) rotate(-90deg)}
  html.ts-portrait .tskb-dock{left:0;top:0;right:auto;bottom:auto;width:100vh;padding:0 8px 8px;transform-origin:0 0;transform:rotate(90deg) translateY(6%)}
  html.ts-portrait .tskb-dock.open{transform:rotate(90deg) translateY(-100%)}
  html.ts-portrait.ts-rot270 .tskb-dock{left:100vw;top:100vh;transform:rotate(-90deg) translateY(6%)}
  html.ts-portrait.ts-rot270 .tskb-dock.open{transform:rotate(-90deg) translateY(-100%)}
  html.ts-portrait .tskb-dock .tskb{max-width:none;padding:12px 10px;gap:8px;border-radius:26px}
  html.ts-portrait .tskb-dock .tskb-row{gap:6px}
  html.ts-portrait .tskb-dock .tskb-key{height:58px;border-radius:14px;font-size:24px}
  html.ts-portrait .tskb-dock .tskb-key.fn,html.ts-portrait .tskb-dock .tskb-key.space{font-size:18px}
  html.ts-portrait .tskb-dock .tskb-key.enter{font-size:20px}
  html.ts-portrait #hud{transform:translate(-50%,-50%) rotate(90deg) scale(.92)}
  html.ts-portrait #hud.on{transform:translate(-50%,-50%) rotate(90deg) scale(1)}
  html.ts-portrait.ts-rot270 #hud{transform:translate(-50%,-50%) rotate(-90deg) scale(.92)}
  html.ts-portrait.ts-rot270 #hud.on{transform:translate(-50%,-50%) rotate(-90deg) scale(1)}
  /* ===== 2026-11-25 Claude：灵动胶囊 + 富媒体卡片 =====
     叫醒小麦时在当前画面顶上出现一颗胶囊；听到话就长成一张卡片（同一块面，不叠层、不透明），里面是问题、回答和这件事的卡片。
     颜色全用 8090 底页的变量（app.css），跟着深浅色走；只动 transform / opacity。竖屏按机器的方向转过来。 */
  html{--isl-line:#2c3a57}
  html.ts-light{--isl-line:#d6cdbf}
  #tsVoicePill,#tsVoiceCard,#tsNotice{font-family:"Noto Sans CJK SC","PingFang SC",sans-serif;color:var(--t1);background:var(--card);box-shadow:0 0 0 1px var(--isl-line),var(--shadow);-webkit-user-select:none;user-select:none;opacity:0;visibility:hidden;pointer-events:none}
  #tsVoicePill{position:fixed;top:16px;left:50%;z-index:9450;display:flex;align-items:center;gap:12px;height:60px;padding:0 9px;border-radius:30px;white-space:nowrap;cursor:pointer;touch-action:manipulation;transform:translate3d(-50%,-20px,0);transition:opacity .15s ease,transform .22s cubic-bezier(.22,1,.36,1),visibility 0s linear .22s}
  #tsVoicePill.show{opacity:1;visibility:visible;pointer-events:auto;transform:translate3d(-50%,var(--isl-y,0px),0);transition:opacity .15s ease,transform .22s cubic-bezier(.22,1,.36,1),visibility 0s linear 0s}
  #tsVoicePillTxt{font-size:20px;line-height:28px;font-weight:700}
  .vp-orb{position:relative;flex:none;width:42px;height:42px;border-radius:50%;overflow:hidden}
  .vp-orb u{position:absolute;inset:0;border-radius:50%;background:radial-gradient(circle at 34% 28%,#f2f4ff 0%,#b4bde8 20%,#6f7ab8 58%,#3a4274 100%)}
  [data-st="connecting"] .vp-orb u{background:radial-gradient(circle at 34% 28%,#fff4e0 0%,#ffc56e 20%,#d0741f 58%,#7a3c10 100%)}
  [data-st="listening"] .vp-orb u{background:radial-gradient(circle at 34% 28%,#ecfdff 0%,#7ff3ff 20%,#1597d6 58%,#0a5286 100%)}
  [data-st="thinking"] .vp-orb u{background:radial-gradient(circle at 34% 28%,#fbefff 0%,#e2a8ff 20%,#8447ef 58%,#45218f 100%)}
  [data-st="speaking"] .vp-orb u{background:radial-gradient(circle at 34% 28%,#f0f6ff 0%,#9cc4ff 18%,#4d6bff 52%,#3a2fb8 100%)}
  .vp-orb i{position:absolute;top:16px;width:5px;height:9px;border-radius:3px;background:#0e1638;transform-origin:50% 60%}
  .vp-orb i:nth-of-type(1){left:14px}.vp-orb i:nth-of-type(2){right:14px}
  .show .vp-orb i{animation:tsBlink 4.6s infinite}
  [data-st="listening"] .vp-orb i{animation:none;transform:scale(1.15)}
  [data-st="thinking"] .vp-orb i{animation:none;transform:translate(-3px,-3px) scaleY(.6)}
  @keyframes tsBlink{0%,92%,100%{transform:scaleY(1)}95%{transform:scaleY(.1)}}
  .vp-wave{display:none;align-items:center;gap:4px;height:24px}
  [data-st="listening"] .vp-wave,[data-st="speaking"] .vp-wave{display:inline-flex}
  .vp-wave b{width:4px;height:22px;border-radius:2px;background:#19b4f0;transform:scaleY(.2);will-change:transform}
  [data-st="speaking"] .vp-wave b{background:#6c86ff}
  html.ts-light .vp-wave b{background:#0b7ea6}
  html.ts-light [data-st="speaking"] .vp-wave b{background:#3b5fd0}
  .vp-dots{display:none;align-items:center;gap:6px;height:24px}
  [data-st="thinking"] .vp-dots,[data-st="connecting"] .vp-dots{display:inline-flex}
  .vp-dots b{width:7px;height:7px;border-radius:50%;background:#a86bff;opacity:.35}
  [data-st="connecting"] .vp-dots b{background:#f0962e}
  .show .vp-dots b{animation:tsDot 1.2s infinite}
  .show .vp-dots b:nth-child(2){animation-delay:.16s}.show .vp-dots b:nth-child(3){animation-delay:.32s}
  @keyframes tsDot{0%,70%,100%{opacity:.3}35%{opacity:1}}
  .vp-acts{display:inline-flex;align-items:center;gap:8px;margin-left:2px}
  .vp-btn{position:relative;flex:none;width:42px;height:42px;padding:0;border:0;border-radius:50%;display:grid;place-items:center;background:var(--ctl);color:var(--t1);cursor:pointer;touch-action:manipulation;transition:transform .12s}
  .vp-btn::before{content:"";position:absolute;inset:-6px -4px}
  .vp-btn:active{transform:scale(.92)}
  .vp-btn svg{width:22px;height:22px}
  .vp-btn.vp-stop{background:var(--ms-soft);color:var(--ms-ink)}

  /* 小提示：错过的闹钟（和胶囊一样大）；正在跑的倒计时（小一号，贴着上沿） */
  #tsNotice{position:fixed;top:16px;left:50%;z-index:9430;display:flex;align-items:center;gap:12px;height:60px;padding:0 9px;border-radius:30px;white-space:nowrap;cursor:pointer;touch-action:manipulation;transform:translate3d(-50%,-20px,0);transition:opacity .15s ease,transform .22s cubic-bezier(.22,1,.36,1),visibility 0s linear .22s}
  #tsNotice.show{opacity:1;visibility:visible;pointer-events:auto;transform:translate3d(-50%,var(--isl-y,0px),0);transition:opacity .15s ease,transform .22s cubic-bezier(.22,1,.36,1),visibility 0s linear 0s}
  #tsNotice .ni{flex:none;width:42px;height:42px;border-radius:50%;display:grid;place-items:center;background:var(--fo-soft);color:var(--fo-ink)}
  #tsNotice .ni svg{width:24px;height:24px}
  #tsNotice b{font-size:20px;line-height:28px;font-weight:700;font-variant-numeric:tabular-nums}
  #tsNotice button{flex:none;height:42px;padding:0 18px;border:0;border-radius:21px;background:var(--ctl);color:var(--t1);font:700 18px/24px "Noto Sans CJK SC","PingFang SC",sans-serif;cursor:pointer;touch-action:manipulation;transition:transform .12s}
  #tsNotice button:active{transform:scale(.95)}
  #tsNotice.mini{top:5px;height:38px;gap:8px;padding:0 14px 0 6px;border-radius:19px}
  #tsNotice.mini .ni{width:26px;height:26px}
  #tsNotice.mini .ni svg{width:17px;height:17px}
  #tsNotice.mini b{font-size:18px;line-height:24px}
  #tsNotice.mini .nt{font-size:16px;line-height:24px;color:var(--t2)}
  #tsVoiceCard{position:fixed;z-index:9440;top:16px;left:50%;width:780px;max-width:calc(100vw - 40px);max-height:calc(100vh - 32px - var(--isl-y,0px));padding:12px 14px 16px;border-radius:30px;display:flex;flex-direction:column;overflow:hidden;transform:translate3d(-50%,-12px,0);transition:opacity .16s ease,transform .24s cubic-bezier(.22,1,.36,1),visibility 0s linear .24s}
  #tsVoiceCard.show{opacity:1;visibility:visible;pointer-events:auto;transform:translate3d(-50%,var(--isl-y,0px),0);transition:opacity .16s ease,transform .24s cubic-bezier(.22,1,.36,1),visibility 0s linear 0s}
  #tsVoiceCard .vc-head{flex:none;display:flex;align-items:center;gap:12px;height:46px}
  #tsVoiceCard .vc-st{font-size:18px;line-height:26px;font-weight:700;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
  #tsVoiceCard .vp-acts{margin-left:auto}
  #tsVoiceCard .vc-src{flex:none;padding:0 12px;border-radius:999px;background:var(--ctl);color:var(--t2);font-size:15px;line-height:30px}
  #tsVoiceCard .vc-src:empty{display:none}
  #tsVoiceCard .vc-q{flex:none;margin:10px 8px 0;font-size:18px;line-height:26px;color:var(--t2);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  #tsVoiceCard .vc-q:empty{display:none}
  #tsVoiceCard .vc-a{flex:0 1 auto;min-height:0;max-height:252px;margin:6px 8px 0;font-size:23px;line-height:36px;overflow-y:auto;scrollbar-width:none;overscroll-behavior:contain;word-break:break-word;white-space:pre-wrap}
  #tsVoiceCard .vc-a::-webkit-scrollbar{display:none}
  #tsVoiceCard .vc-a.short{font-weight:700}
  #tsVoiceCard .vc-a:empty{display:none}
  #tsVoiceCard .vc-rich{flex:none;margin:14px 0 0}
  #tsVoiceCard .vc-rich:empty{display:none}
  #tsVoiceCard .vc-chips{flex:none;display:flex;flex-wrap:wrap;gap:10px;margin:12px 4px 0}
  #tsVoiceCard .vc-chips:empty{display:none}
  #tsVoiceCard .vc-chips button{height:46px;padding:0 18px;border:0;border-radius:23px;background:var(--ctl);color:var(--t1);font:700 17px/24px "Noto Sans CJK SC","PingFang SC",sans-serif;cursor:pointer;touch-action:manipulation}
  #tsVoiceCard .vc-chips button:active{transform:scale(.96)}
  #tsVoiceCard .vc-left{position:absolute;left:0;right:0;bottom:0;height:4px;transform-origin:0 50%;transform:scaleX(0);background:var(--sel);opacity:.55}
  #tsVoiceCard.leaving .vc-left{animation:tsLeft var(--left-ms,12s) linear forwards}
  @keyframes tsLeft{from{transform:scaleX(1)}to{transform:scaleX(0)}}
  html.ts-portrait #tsVoicePill{top:50%;left:100%;width:max-content;white-space:nowrap;transform-origin:0 0;transform:rotate(90deg) translate3d(-50%,-6px,0)}
  html.ts-portrait #tsVoicePill.show{transform:rotate(90deg) translate3d(-50%,calc(16px + var(--isl-y,0px)),0)}
  html.ts-portrait.ts-rot270 #tsVoicePill{left:0;transform:rotate(-90deg) translate3d(-50%,-6px,0)}
  html.ts-portrait.ts-rot270 #tsVoicePill.show{transform:rotate(-90deg) translate3d(-50%,calc(16px + var(--isl-y,0px)),0)}
  html.ts-portrait #tsNotice{top:50%;left:100%;width:max-content;transform-origin:0 0;transform:rotate(90deg) translate3d(-50%,-6px,0)}
  html.ts-portrait #tsNotice.show{transform:rotate(90deg) translate3d(-50%,calc(16px + var(--isl-y,0px)),0)}
  html.ts-portrait #tsNotice.mini.show{transform:rotate(90deg) translate3d(-50%,calc(5px + var(--isl-y,0px)),0)}
  html.ts-portrait.ts-rot270 #tsNotice{left:0;transform:rotate(-90deg) translate3d(-50%,-6px,0)}
  html.ts-portrait.ts-rot270 #tsNotice.show{transform:rotate(-90deg) translate3d(-50%,calc(16px + var(--isl-y,0px)),0)}
  html.ts-portrait.ts-rot270 #tsNotice.mini.show{transform:rotate(-90deg) translate3d(-50%,calc(5px + var(--isl-y,0px)),0)}
  html.ts-portrait #tsVoiceCard{top:50%;left:100%;width:768px;max-width:none;max-height:calc(100vw - 32px - var(--isl-y,0px));transform-origin:0 0;transform:rotate(90deg) translate3d(-50%,4px,0)}
  html.ts-portrait #tsVoiceCard.show{transform:rotate(90deg) translate3d(-50%,calc(16px + var(--isl-y,0px)),0)}
  html.ts-portrait.ts-rot270 #tsVoiceCard{left:0;transform:rotate(-90deg) translate3d(-50%,4px,0)}
  html.ts-portrait.ts-rot270 #tsVoiceCard.show{transform:rotate(-90deg) translate3d(-50%,calc(16px + var(--isl-y,0px)),0)}
  html.ts-portrait #tsVoiceCard .vc-a{max-height:432px}

  /* 卡片本体：胶囊里和小麦对话里同一套 */
  .rc{border-radius:22px;background:var(--inset);padding:16px 18px;font-family:"Noto Sans CJK SC","PingFang SC",sans-serif;color:var(--t1)}
  .msg-rich .rc{background:var(--ctl)}
  .rc button{font-family:inherit;border:0;cursor:pointer;touch-action:manipulation}
  .rc .rh{display:flex;align-items:center;gap:16px;min-width:0}
  .rc .ri{flex:none;width:60px;height:60px;border-radius:18px;display:grid;place-items:center;background:var(--gr-soft);color:var(--gr-ink)}
  .rc .ri svg{width:30px;height:30px}
  .rc .ri.mu{background:var(--mu-soft);color:var(--mu-ink)}.rc .ri.fo{background:var(--fo-soft);color:var(--fo-ink)}.rc .ri.ms{background:var(--ms-soft);color:var(--ms-ink)}.rc .ri.sv{background:var(--sv-soft);color:var(--sv-ink)}.rc .ri.vi{background:var(--vi-soft);color:var(--vi-ink)}.rc .ri.xz{background:var(--xz-soft);color:var(--xz-ink)}
  .rc .rt{flex:1;min-width:0}
  .rc .rt b{display:block;font-size:22px;line-height:30px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .rc .rt span{display:block;font-size:16px;line-height:23px;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .msg-rich .rc .rt span{white-space:normal;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
  .msg-rich .rc .wx-t{font-size:46px;line-height:52px}
  .msg-rich .rc .wx-i svg{width:56px;height:56px}
  .rc .ra{flex:none;display:flex;align-items:center;gap:10px}
  .rc .rb{flex:none;height:50px;padding:0 20px;border-radius:16px;background:var(--ctl);color:var(--t1);font-size:18px;font-weight:700;white-space:nowrap}
  .msg-rich .rc .rb,.msg-rich .rc .rk{background:var(--card)}
  .rc .rb.arm{background:var(--bad-soft);color:var(--bad-ink);box-shadow:inset 0 0 0 2px var(--bad)}
  .rc .rb:active,.rc .rk:active{transform:scale(.95)}
  .rc .rk{flex:none;width:54px;height:54px;border-radius:50%;display:grid;place-items:center;background:var(--ctl);color:var(--t1);padding:0}
  .rc .rk svg{width:26px;height:26px;fill:currentColor}
  .rc .rk.go{width:64px;height:64px;background:var(--t1);color:var(--card)}
  .rc .rk.go svg{width:30px;height:30px}
  .rc .tag{flex:none;padding:0 12px;border-radius:999px;background:var(--ctl);color:var(--t2);font-size:15px;line-height:30px;white-space:nowrap}
  .rc .tag.ok{background:var(--mu-soft);color:var(--mu-ink)}.rc .tag.mid{background:var(--ms-soft);color:var(--ms-ink)}.rc .tag.bad{background:var(--bad-soft);color:var(--bad-ink)}
  .rc .wx-i svg{width:68px;height:68px;display:block}
  .rc .wx-t{flex:none;font-size:58px;line-height:64px;font-weight:700;letter-spacing:-2px;font-variant-numeric:tabular-nums}
  .rc .wx-d{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:14px}
  .rc .wx-d div{padding:10px 4px 9px;border-radius:16px;background:var(--card);text-align:center}
  .msg-rich .rc .wx-d div{background:var(--inset)}
  .rc .wx-d div.on,.msg-rich .rc .wx-d div.on{background:var(--sel-soft);box-shadow:inset 0 0 0 2px var(--sel)}
  .rc .wx-d .on small{color:var(--sel-ink);font-weight:700}
  .rc .wx-d small{display:block;font-size:15px;line-height:21px;color:var(--t2)}
  .rc .wx-d svg{width:38px;height:38px;display:block;margin:3px auto 2px}
  .rc .wx-d b{display:block;font-size:17px;line-height:24px;font-weight:700;white-space:nowrap}
  .rc .wx-d b i{font-style:normal;font-weight:400;color:var(--t3)}
  .rc .fl{fill:#ebf6ff}.rc .fg{stroke:#c6d4ea;fill:none}
  html.ts-light .rc .fl{fill:#7fb2de}html.ts-light .rc .fg{stroke:#8fa1b8}
  html.ts-light #vg-cloud stop:nth-child(1){stop-color:#e6ecf4}html.ts-light #vg-cloud stop:nth-child(2){stop-color:#cdd7e4}html.ts-light #vg-cloud stop:nth-child(3){stop-color:#aebdd1}
  html.ts-light #vg-cloud-back stop:nth-child(1){stop-color:#c3cfde}html.ts-light #vg-cloud-back stop:nth-child(2){stop-color:#98a9c0}
  html.ts-light #vg-cloud-dark stop:nth-child(1){stop-color:#b7c4d6}html.ts-light #vg-cloud-dark stop:nth-child(2){stop-color:#8395b1}
  html.ts-light #vg-moon stop:nth-child(1){stop-color:#f2d27a}html.ts-light #vg-moon stop:nth-child(2){stop-color:#d9a02b}
  .rc .wx-x{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
  .rc .cv{flex:none;width:84px;height:84px;border-radius:20px;overflow:hidden;background:var(--mu-soft);color:var(--mu-ink);display:grid;place-items:center;padding:0}
  .rc .cv img{width:100%;height:100%;object-fit:cover;display:block}
  .rc .cv svg{width:38px;height:38px}
  .rc .pb{height:6px;margin-top:10px;border-radius:3px;background:var(--ctl2);overflow:hidden}
  .rc .pb i{display:block;height:100%;border-radius:3px;background:var(--mu);transform-origin:0 50%}
  .rc .big{flex:none;font-size:54px;line-height:60px;font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:-1px}
  .rc .lst{margin-top:12px;border-top:1px solid var(--line)}
  .rc .lst>*{display:flex;align-items:center;gap:14px;width:100%;min-height:58px;padding:8px 2px;border-top:1px solid var(--line);background:none;color:inherit;text-align:left;font-size:18px;line-height:26px}
  .rc .lst>*:first-child{border-top:0}
  .rc .lst b{font-weight:700;font-variant-numeric:tabular-nums}
  .rc .lst span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .rc .lst em{flex:none;font-style:normal;font-size:16px;color:var(--t2)}
  .rc.nw{padding:4px 18px}
  .rc.nw .lst{margin:0;border:0}
  .rc.nw .lst i{flex:none;width:34px;height:34px;border-radius:12px;display:grid;place-items:center;font-style:normal;font-size:17px;font-weight:700;background:var(--sv-soft);color:var(--sv-ink)}
  .rc.nw .lst span{white-space:normal;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;font-size:20px;line-height:30px;padding:4px 0}
  .rc .nodes{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:14px}
  .rc .nodes div{display:flex;align-items:center;gap:8px;min-width:0;height:44px;padding:0 12px;border-radius:14px;background:var(--card);font-size:16px}
  .msg-rich .rc .nodes div{background:var(--inset)}
  .rc .nodes i{flex:none;width:9px;height:9px;border-radius:50%;background:var(--ok)}
  .rc .nodes .off i{background:var(--bad)}
  .rc .nodes span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  /* 竖版「设置 → 对话」里卡片只有 474 宽：服务器排两列，闹钟的按钮换到下一行，空气质量并进说明，音乐的封面和按钮收小、歌名可以两行 */
  .rc .aqs{display:none;font-style:normal}
  html.ts-portrait .msg-rich .rc .nodes{grid-template-columns:repeat(2,1fr)}
  html.ts-portrait .msg-rich .rc.sch .rh{flex-wrap:wrap;row-gap:14px}
  html.ts-portrait .msg-rich .rc.sch .ra{flex:1 0 100%}
  html.ts-portrait .msg-rich .rc.sch .rb{flex:1}
  html.ts-portrait .msg-rich .rc .ra .tag{display:none}
  html.ts-portrait .msg-rich .rc .aqs{display:inline}
  html.ts-portrait .msg-rich .rc.mus .rh{gap:14px}
  html.ts-portrait .msg-rich .rc.mus .cv{width:68px;height:68px;border-radius:18px}
  html.ts-portrait .msg-rich .rc.mus .rt b{white-space:normal;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;font-size:20px;line-height:27px}
  html.ts-portrait .msg-rich .rc.mus .ra{gap:6px}
  html.ts-portrait .msg-rich .rc.mus .rk{width:46px;height:46px}
  html.ts-portrait .msg-rich .rc.mus .rk.go{width:56px;height:56px}
  .rc.one{display:flex;align-items:center;gap:14px;padding:14px}
  .rc.one .bar{flex:1;min-width:0;margin:0}
  .rc.one .rb{height:72px;border-radius:20px}
  .rc .bar input{touch-action:none}
  .rc .bar input::-webkit-slider-runnable-track{background:linear-gradient(90deg,var(--fill) 0 var(--pos,50%),var(--ctl) var(--pos,50%) 100%)}
  /* 提示条放到下面（和主页一样），音量条居中；竖屏按机器的方向转 */
  html.ts-portrait #toast{left:150px;top:50%;bottom:auto;width:max-content;max-width:700px;transform-origin:0 0;transform:rotate(90deg) translate(-50%,-100%) translateY(12px)}
  html.ts-portrait #toast.on{transform:rotate(90deg) translate(-50%,-100%)}
  html.ts-portrait.ts-rot270 #toast{left:calc(100% - 150px);transform:rotate(-90deg) translate(-50%,-100%) translateY(12px)}
  html.ts-portrait.ts-rot270 #toast.on{transform:rotate(-90deg) translate(-50%,-100%)}
  `;
  document.head.appendChild(style);

  /* ---------------- unified settings trigger (legacy standalone /display iframe retired) ---------------- */
  let brightBtn = null, brightFrame = null, homeFrameRef = null, orReleaseChildTouch = () => {};
  function brightnessOpen() { return false; }
  function closeBrightness() { orReleaseChildTouch(); }
  function openBrightness() {
    if (typeof window.__tsOpenUnifiedSettings === 'function') window.__tsOpenUnifiedSettings(true);
    else $('btnSettings')?.click();
  }
  if (isMon) {
    const subParentOrigin = (() => {
      try {
        const o = document.referrer ? new URL(document.referrer).origin : '';
        if (o === 'http://127.0.0.1:8090' || o === 'http://localhost:8090') return o;
      } catch (e) {}
      return 'http://127.0.0.1:8090';
    })();
    const homeEl = document.querySelector('.home');
    if (framed && homeEl) {
      homeEl.addEventListener('click', e => {
        e.preventDefault();
        window.parent.postMessage({ ts: 'sub', cmd: 'close' }, subParentOrigin);
      });
    }
    if (!$('tsBrightness')) {
      brightBtn = document.createElement('button'); brightBtn.id = 'tsBrightness'; brightBtn.className = 'monitor-brightness';
      brightBtn.setAttribute('aria-label', '打开系统设置'); brightBtn.setAttribute('aria-expanded', 'false'); brightBtn.innerHTML = gear + '设置';
      brightBtn.onclick = framed ? () => window.parent.postMessage({ ts: 'sub', cmd: 'brightness' }, subParentOrigin) : openBrightness;
      if (homeEl) homeEl.after(brightBtn); else document.body.appendChild(brightBtn);
    }
    if (framed) window.parent.postMessage({ ts: 'sub', cmd: 'ready' }, subParentOrigin);
  }

  /* ---------------- message card (unread phone messages, until 「收到」) ---------------- */
  let card = null, dismissInboxById = () => {}, triggerInboxPoll = () => {}, isHomeActiveView = () => false, syncInboxSchedule = () => {}, stopOverlayClip = () => {};
  if (!framed) {
    card = document.createElement('div'); card.id = 'tsMsgCard'; card.setAttribute('role', 'alert');
    card.innerHTML = `<div class="tm-b"><div class="tm-h"><span class="tm-tag">${msgIco}新留言</span><span id="tsMsgMeta"></span></div><div class="tm-t" id="tsMsgText"></div></div><button id="tsMsgAck">收到</button>`;
    document.body.appendChild(card);
    const msgMetaEl = $('tsMsgMeta'), msgTextEl = $('tsMsgText'), msgAckEl = $('tsMsgAck');
    let shownMsg = null, msgBusy = false, msgStillTimer = 0, unreadCache = [], unreadTotal = 0, inboxTimer = 0, inboxNextDue = 0;
    function armInboxTimer(ms) {
      clearTimeout(inboxTimer); inboxTimer = 0; inboxNextDue = 0;
      if (document.hidden) return;
      const delay = ms !== undefined ? ms : (isHomeActiveView() ? 30000 : 5000);
      inboxNextDue = Date.now() + delay;
      inboxTimer = setTimeout(() => { inboxTimer = 0; inboxNextDue = 0; pollInbox(); }, delay);
    }
    syncInboxSchedule = () => {
      if (document.hidden) { clearTimeout(inboxTimer); inboxTimer = 0; inboxNextDue = 0; return; }
      if (!inboxTimer) armInboxTimer();
      else if (!isHomeActiveView() && inboxNextDue - Date.now() > 5000) armInboxTimer(5000);
    };
    const settleMessage = () => {
      clearTimeout(msgStillTimer); card.classList.remove('settled');
      msgStillTimer = setTimeout(() => card.classList.add('settled'), 30000);
    };
    card.addEventListener('pointerdown', settleMessage, { passive: true });
    let inboxEtag = '', inboxLastJson = null;
    async function inboxRequest(path, opt = {}) {
      const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 8000);
      try {
        const isGet = !opt.method || String(opt.method).toUpperCase() === 'GET';
        if (!isGet) { inboxEtag = ''; inboxLastJson = null; }
        const hdrs = Object.assign({}, opt.headers || {});
        if (isGet && inboxEtag && inboxLastJson && path === '/api/home/inbox') {
          hdrs['If-None-Match'] = inboxEtag;
        }
        const r = await fetch(HOME + path, Object.assign({ cache: 'no-store' }, opt, { headers: hdrs, signal: ctl.signal }));
        if (r.status === 304 && isGet && inboxLastJson && path === '/api/home/inbox') return inboxLastJson;
        if (!r.ok) throw new Error('inbox');
        const data = await r.json();
        if (isGet && path === '/api/home/inbox') {
          const et = r.headers.get('ETag') || '';
          if (et) { inboxEtag = et; inboxLastJson = data; }
        }
        return data;
      } finally { clearTimeout(timer); }
    }
    const pad = n => String(n).padStart(2, '0');
    function renderUnreadCard() {
      const m = unreadCache[0];
      if (!m) {
        retryMsgId = null;
        if (shownMsg !== null || card.classList.contains('show')) { shownMsg = null; clearTimeout(msgStillTimer); card.classList.remove('show'); }
        return;
      }
      const d = new Date(m.at * 1000);
      const count = Math.max(unreadTotal, unreadCache.length);
      const dd = m.due ? new Date(m.due * 1000) : d;
      const from = m.due ? `提醒${m.who || ''}` : (m.who ? `${m.who}发来` : '来自手机');
      const meta = `${pad(dd.getHours())}:${pad(dd.getMinutes())} · ${from}${count > 1 ? ` · 还有 ${count - 1} 条` : ''}`;
      const shownText = m.kind === 'voice' && m.transcript ? `“${m.transcript}”` : m.text;
      if (msgTextEl.textContent !== shownText) msgTextEl.textContent = shownText;
      if (msgMetaEl.textContent !== meta) msgMetaEl.textContent = meta;
      if (shownMsg !== m.id) { if (retryMsgId && retryMsgId !== m.id) retryMsgId = null; settleMessage(); }
      const wantAckLbl = retryMsgId === m.id
        ? (m.kind === 'voice' ? '▶ 重试' : '重试')
        : (m.kind === 'voice' ? `▶ 播放 · ${Math.max(1, Math.round(Number(m.duration) || 3))}″` : (m.due ? '知道了' : '收到'));
      if (msgAckEl.textContent !== wantAckLbl) msgAckEl.textContent = wantAckLbl;
      shownMsg = m.id; if (!card.classList.contains('show')) card.classList.add('show');
    }
    let inboxFetching = false, inboxPending = false, inboxAckEpoch = 0, retryMsgId = null, overlayClipUntil = 0, lastCdOpenSeq = null, lastCdCloseSeq = null;
    stopOverlayClip = () => {
      if (Date.now() < overlayClipUntil) {
        overlayClipUntil = 0;
        inboxRequest('/api/home/messages/play', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"stop":true}' }).catch(() => {});
      }
    };
    dismissInboxById = id => {
      inboxAckEpoch++;
      if (retryMsgId === id) retryMsgId = null;
      const idx = unreadCache.findIndex(x => x.id === id);
      if (idx >= 0) {
        unreadCache.splice(idx, 1);
        unreadTotal = Math.max(0, unreadTotal - 1);
        renderUnreadCard();
      } else if (shownMsg === id) {
        shownMsg = null; clearTimeout(msgStillTimer); card.classList.remove('show');
      }
    };
    async function pollInbox() {
      if (document.hidden) { clearTimeout(inboxTimer); inboxTimer = 0; inboxNextDue = 0; return; }
      if (msgBusy) { armInboxTimer(1000); return; }
      if (inboxFetching) { inboxPending = true; return; }
      inboxFetching = true;
      const myEpoch = inboxAckEpoch;
      try {
        const j = await inboxRequest('/api/home/inbox');
        if (myEpoch !== inboxAckEpoch || msgBusy) return;
        if (typeof handleTutorWake === 'function') handleTutorWake(j?.tutor_wake);
        const cdSeq = Number(j?.cd_open_seq) || 0;
        if (lastCdOpenSeq === null) {
          lastCdOpenSeq = cdSeq;
        } else if (cdSeq > lastCdOpenSeq) {
          lastCdOpenSeq = cdSeq;
          if (isXZ && j?.cd_open_fresh && !document.body.classList.contains('ringing')) {
            closeBrightness(false);
            forceCloseSheets();
            if (subOpen) closeSub(true);
            else showHome();
            const sendOpenCd = () => { try { homeFrameRef?.contentWindow?.postMessage({ ts: 'home', openCountdown: true }, HOME); } catch (e) {} };
            sendOpenCd();
            if (!homeOk) setTimeout(sendOpenCd, 140);
          }
        }
        const cdCloseSeq = Number(j?.cd_close_seq) || 0;
        if (lastCdCloseSeq === null) {
          lastCdCloseSeq = cdCloseSeq;
        } else if (cdCloseSeq > lastCdCloseSeq) {
          lastCdCloseSeq = cdCloseSeq;
          if (isXZ && j?.cd_close_fresh && !document.body.classList.contains('ringing')) {
            closeBrightness(false);
            forceCloseSheets();
            if (subOpen) closeSub(true);
            else showHome();
            const sendCloseCd = () => { try { homeFrameRef?.contentWindow?.postMessage({ ts: 'home', closeCountdown: true }, HOME); } catch (e) {} };
            sendCloseCd();
            if (!homeOk) setTimeout(sendCloseCd, 140);
          }
        }
        const nextTotal = Number(j?.count) || 0;
        if (nextTotal === 0 && unreadTotal === 0 && unreadCache.length === 0 && shownMsg === null) return;
        unreadCache = Array.isArray(j?.unread) ? j.unread.slice() : [];
        unreadTotal = nextTotal || unreadCache.length;
        renderUnreadCard();
      } catch (e) {} finally {
        inboxFetching = false;
        if (inboxPending && !document.hidden && !msgBusy) {
          inboxPending = false;
          pollInbox();
        } else {
          armInboxTimer();
        }
      }
    }
    triggerInboxPoll = () => { pollInbox(); };
    msgAckEl.onclick = async () => {
      if (!shownMsg || msgBusy) return;
      const targetId = shownMsg;
      const targetItem = unreadCache.find(x => x.id === targetId) || null;
      retryMsgId = null;
      msgBusy = true;
      msgAckEl.disabled = true;
      dismissInboxById(targetId);
      try { homeFrameRef?.contentWindow?.postMessage({ ts: 'home', inboxAck: targetId, kind: targetItem?.kind, duration: targetItem?.duration, due: !!targetItem?.due }, HOME); } catch (e) {}
      try {
        const j = await inboxRequest('/api/home/inbox/ack', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: targetId }) });
        if (!j.ok) throw new Error('ack');
        if (targetItem?.kind === 'voice') overlayClipUntil = Date.now() + Math.max(1, Math.min(60, Number(targetItem.duration) || 4)) * 1000 + 1200;
        setTimeout(pollInbox, 200);
      } catch (e) {
        overlayClipUntil = 0;
        msgBusy = false;
        msgAckEl.disabled = false;
        retryMsgId = targetId;
        if (targetItem && !unreadCache.some(x => x.id === targetId)) {
          unreadCache.unshift(targetItem);
          unreadTotal = Math.max(unreadTotal + 1, unreadCache.length);
        }
        renderUnreadCard();
        try { homeFrameRef?.contentWindow?.postMessage({ ts: 'home', inboxRefresh: true, inboxAckFail: targetId }, HOME); } catch (err) {}
      } finally { msgBusy = false; msgAckEl.disabled = false; }
    };
    pollInbox();
    document.addEventListener('visibilitychange', () => { if (!document.hidden) pollInbox(); else syncInboxSchedule(); });
    // 留言卡占着最上面时，灵动胶囊和卡片让到它下面
    let msgGap = '', msgGapRaf = 0;
    const applyMsgGap = () => {
      msgGapRaf = 0;
      const g = card.classList.contains('show') ? `${card.offsetHeight + 14}px` : '';
      if (g !== msgGap) {
        msgGap = g;
        if (g) document.documentElement.style.setProperty('--isl-y', g);
        else document.documentElement.style.removeProperty('--isl-y');
      }
    };
    const syncMsgGap = () => {
      if (!card.classList.contains('show') && !msgGap) return;
      if (!msgGapRaf) msgGapRaf = requestAnimationFrame(applyMsgGap);
    };
    new MutationObserver(syncMsgGap).observe(card, { attributes: true, attributeFilter: ['class'], subtree: true, childList: true, characterData: true });
  }

  if (!isXZ) return;

  /* ---------------- home & sub-page layers over XiaoZhi ---------------- */
  const root = document.documentElement;
  const layer = document.createElement('div'); layer.id = 'tsHomeLayer';
  const frame = document.createElement('iframe'); frame.title = '主页';
  homeFrameRef = frame;
  layer.appendChild(frame);

  const subLayer = document.createElement('div'); subLayer.id = 'tsSubLayer';
  const subLoading = document.createElement('div'); subLoading.id = 'tsSubLoading';
  subLoading.innerHTML = '<span id="tsSubLabel">正在打开应用</span><span class="ts-dots" aria-hidden="true"><i></i><i></i><i></i></span>';
  const subErr = document.createElement('div'); subErr.id = 'tsSubErr';
  subErr.innerHTML = '<p>页面暂时无法打开</p><button type="button" id="tsSubBack">返回主页</button>';
  subLayer.append(subLoading, subErr);
  document.body.append(layer, subLayer);
  const subLabelEl = $('tsSubLabel');
  let settingsEl = $('settings'), wifiEl = $('wifi');
  const getSettings = () => settingsEl || (settingsEl = $('settings'));
  const getWifi = () => wifiEl || (wifiEl = $('wifi'));

  let homeOk = false, readyTimer = 0;
  let subOpen = false, subUrl = '', subShowTimer = 0, activeSub = null;
  const subEntries = new Map(), subPauseTimers = new Map(), subEvictTimers = new Map();
  const BUSY = new Set(['listening', 'thinking', 'speaking']);
  const isBusy = () => BUSY.has(document.body.dataset.state || 'idle') || document.body.classList.contains('ringing');
  // 主页的「小麦」磁贴：叫醒它，在当前画面上弹出胶囊
  function wakeXiaomai() {
    const u = window.__tsUi, say = t => { if (typeof window.toast === 'function') window.toast(t, 'error'); };
    if (u && u.status && u.status.mic === false) { say('麦克风已关，拨开机身侧面的开关再说'); return; }
    fetch('/api/wake', { method: 'POST' }).then(r => { if (!r.ok) throw new Error('wake'); }).catch(() => say('小智没有响应，过几秒再试'));
  }
  const sheetOpen = () => !!(getSettings()?.classList.contains('on') || getWifi()?.classList.contains('on') || $('sheet')?.classList.contains('on'));
  isHomeActiveView = () => homeOk && !subOpen && !sheetOpen();

  // 灵动胶囊：小麦被叫醒时出现在当前画面最上面；有话就长成卡片（#tsVoiceCard），胶囊收起
  const ORB = '<span class="vp-orb" aria-hidden="true"><u></u><i></i><i></i></span>';
  const WAVE = id => `<span class="vp-wave"${id ? ` id="${id}"` : ''} aria-hidden="true"><b></b><b></b><b></b><b></b><b></b></span><span class="vp-dots" aria-hidden="true"><b></b><b></b><b></b></span>`;
  const VP_EXPAND = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4.5h12v8.5H8.5L4 16.5z"/><path d="M19.5 9.5V20l-3.5-3H10"/></svg>';
  const VP_STOP = '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6.5" y="6.5" width="11" height="11" rx="2.6"/></svg>';
  const VP_CLOSE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 15l6-6 6 6" stroke-linejoin="round"/></svg>';
  const voicePill = document.createElement('div');
  voicePill.id = 'tsVoicePill';
  voicePill.setAttribute('role', 'region');
  voicePill.setAttribute('aria-label', '小智语音灵动胶囊');
  voicePill.innerHTML = ORB + '<span id="tsVoicePillTxt">我在听</span>' + '<span class="vp-wave" id="tsVpWave" aria-hidden="true"><b></b><b></b><b></b><b></b><b></b></span><span class="vp-dots" aria-hidden="true"><b></b><b></b><b></b></span>'
    + `<span class="vp-acts"><button type="button" class="vp-btn vp-exp" id="tsVpExpand" aria-label="看对话">${VP_EXPAND}</button><button type="button" class="vp-btn vp-stop" id="tsVpStop" aria-label="停下">${VP_STOP}</button></span>`;
  document.body.appendChild(voicePill);
  const voicePillTxt = $('tsVoicePillTxt');
  let vcShown = false, vcTool = () => '', vcSync = () => {};   // 卡片那段在后面，先留好钩子
  const isRecognising = () => { try { return !!(typeof ui !== 'undefined' && ui.status && ui.status.recognising); } catch (e) { return false; } };
  const modelLabel = () => { try { return typeof modelName === 'function' ? modelName() : ''; } catch (e) { return ''; } };
  function voiceStateText(st) {
    if (st === 'listening') return isRecognising() ? '听到了，正在识别' : '我在听';
    if (st === 'thinking') return vcTool() || (modelLabel() && modelLabel() !== '模型' ? `${modelLabel()} 正在想` : '正在想');
    return { connecting: '正在连接', speaking: '正在回答' }[st] || '';
  }
  function syncVoicePill() {
    const st = document.body.dataset.state || 'idle';
    const lbl = !vcShown && !document.body.classList.contains('ringing') ? voiceStateText(st) : '';
    if (lbl) {
      if (voicePill.dataset.st !== st) voicePill.dataset.st = st;
      if (voicePillTxt.textContent !== lbl) voicePillTxt.textContent = lbl;
      if (!voicePill.classList.contains('show')) voicePill.classList.add('show');
    } else if (voicePill.classList.contains('show')) {
      voicePill.classList.remove('show');
    }
  }
  const voiceWaves = [...voicePill.querySelectorAll('.vp-wave b')];
  addEventListener('ts-voice-level', e => {
    if (!voicePill.classList.contains('show') && !vcShown) return;
    const v = Math.max(0, Math.min(1, Number(e.detail?.v) || 0)), t = performance.now() / 160;
    const on = vcShown ? 5 : 0;
    for (let i = 0; i < 5; i++) {
      const b = voiceWaves[on + i];
      if (b) b.style.transform = `scaleY(${(0.2 + v * (0.42 + 0.38 * Math.abs(Math.sin(t + i * 1.1)))).toFixed(2)})`;
    }
  });
  function stopVoice() {
    const st = document.body.dataset.state || 'idle';
    fetch(st === 'speaking' || st === 'thinking' ? '/api/abort' : '/api/end', { method: 'POST' }).catch(() => {});
  }
  const openChat = () => openUnifiedSettings('chat');   // 看全部对话：设置里的「对话」
  $('tsVpExpand').onclick = e => { e.stopPropagation(); openChat(); };
  $('tsVpStop').onclick = e => { e.stopPropagation(); stopVoice(); };
  voicePill.onclick = () => { if (typeof window.__tsShowVoiceCard === 'function') window.__tsShowVoiceCard(); };

  // 小提示：错过的闹钟和提醒、正在跑的倒计时。只在主页上、没在对话的时候；数据来自底座页（window.__tsUi）
  const noticeEl = document.createElement('div');
  noticeEl.id = 'tsNotice';
  noticeEl.setAttribute('role', 'status');
  document.body.appendChild(noticeEl);
  const NI = {
    missed: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 1.5h-15zM10 20.5a2.2 2.2 0 0 0 4 0"/></svg>',
    timer: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="13.5" r="7.5"/><path d="M12 10v3.5l2.5 1.5M9.5 3h5"/></svg>',
  };
  let noticeKey = '', noticeTick = 0;
  const nEsc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const nPad = n => String(n).padStart(2, '0');
  const nHm = t => { const d = new Date(t * 1000); return nPad(d.getHours()) + ':' + nPad(d.getMinutes()); };
  const nLeft = t => { const s = Math.max(0, Math.round(t - Date.now() / 1000)), h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return h ? `${h}:${nPad(m)}:${nPad(s % 60)}` : `${nPad(m)}:${nPad(s % 60)}`; };
  function noticeSpec() {
    const u = window.__tsUi;
    if (!u || vcShown || !isHomeActiveView() || document.body.classList.contains('ringing') || (document.body.dataset.state || 'idle') !== 'idle') return null;
    const a = u.alarms || {}, missed = a.missed || [];
    const timer = (a.items || []).filter(v => v.kind === 'timer' && v.at > Date.now() / 1000).sort((x, y) => x.at - y.at)[0];
    if (timer) return { k: 'timer', key: `t${timer.id}:${timer.at}:${timer.label || ''}`, at: timer.at, text: String(timer.label || '').slice(0, 10) };
    if (missed.length) {
      const m = missed[0], kind = { timer: '倒计时', alarm: '闹钟', reminder: '提醒' }[m.kind] || '闹钟';
      return { k: 'missed', key: `m${missed.length}:${m.at}`, text: `错过 ${nHm(m.at)} ${m.label ? String(m.label).slice(0, 12) : '的' + kind}${missed.length > 1 ? ` 等 ${missed.length} 条` : ''}` };
    }
    return null;
  }
  function syncNotice() {
    const n = noticeSpec(), key = n ? n.key : '';
    if (key !== noticeKey) {
      noticeKey = key;
      if (n) {
        noticeEl.className = n.k === 'timer' ? 'mini show' : 'show';
        noticeEl.dataset.k = n.k;
        noticeEl.innerHTML = `<span class="ni">${NI[n.k]}</span>`
          + (n.k === 'timer' ? `<b data-at="${n.at}">${nLeft(n.at)}</b>${n.text ? `<span class="nt">${nEsc(n.text)}</span>` : ''}` : `<b>${nEsc(n.text)}</b>`)
          + (n.k === 'missed' ? '<button type="button" data-n="missed-ok">知道了</button>' : '');
      } else noticeEl.classList.remove('show');
      clearInterval(noticeTick); noticeTick = 0;
      if (n && n.k === 'timer') noticeTick = setInterval(() => { const b = noticeEl.querySelector('[data-at]'); if (!b) return; const s = nLeft(Number(b.dataset.at)); if (b.textContent !== s) b.textContent = s; if (Number(b.dataset.at) < Date.now() / 1000) syncNotice(); }, 1000);
    }
  }
  const elTarget = e => { const t = e && e.target; return t && t.nodeType === 3 ? t.parentElement : t; };
  noticeEl.onclick = e => {
    const nt = elTarget(e);
    if (nt && nt.closest && nt.closest('[data-n="missed-ok"]')) fetch('/api/alarms/missed/clear', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).catch(() => {});
    else window.__tsOpenHomeAlarms();
  };
  addEventListener('ts-ui-data', syncNotice);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) syncNotice(); });

  // Guard the base page's 1s clock tick from unconditionally dirtying unchanged text nodes (saves 59/60 CPU repaints/min)
  const tcDesc = Object.getOwnPropertyDescriptor(Node.prototype, 'textContent');
  if (tcDesc?.get && tcDesc?.set) {
    for (const id of ['bigTime', 'bigDate', 'ringTime']) {
      const el = $(id);
      if (el) {
        try {
          Object.defineProperty(el, 'textContent', {
            configurable: true,
            get() { return tcDesc.get.call(this); },
            set(v) { const s = String(v ?? ''); if (tcDesc.get.call(this) !== s) tcDesc.set.call(this, s); }
          });
        } catch (e) {}
      }
    }
  }

  const nativeRaf = window.requestAnimationFrame.bind(window), nativeCaf = window.cancelAnimationFrame.bind(window);

  let homeVisTimer = 0, lastHomeVis = null, subFadeTimer = 0, subOpaqueTimer = 0;
  const syncSubOpaque = on => {
    clearTimeout(subOpaqueTimer); subOpaqueTimer = 0;
    if (!on) { root.classList.remove('ts-sub-opaque'); return; }
    subOpaqueTimer = setTimeout(() => { subOpaqueTimer = 0; if (subOpen && subLayer.classList.contains('ready')) root.classList.add('ts-sub-opaque'); }, 155);
  };
  function syncHomeVis(vis, immediate = false) {
    clearTimeout(homeVisTimer);
    const send = () => {
      if (lastHomeVis === vis) return;
      lastHomeVis = vis;
      try { frame.contentWindow?.postMessage({ ts: 'home', visible: vis }, HOME); } catch (e) {}
    };
    if (vis || immediate) send();
    else homeVisTimer = setTimeout(send, 150);
  }
  function syncSubVis(entry, vis, immediate = false) {
    if (!entry) return;
    clearTimeout(subPauseTimers.get(entry.url));
    subPauseTimers.delete(entry.url);
    const send = () => {
      try { entry.frame.contentWindow?.postMessage({ ts: 'sub', visible: vis }, entry.origin); } catch (e) {}
    };
    if (vis || immediate) send();
    else subPauseTimers.set(entry.url, setTimeout(() => { subPauseTimers.delete(entry.url); send(); }, 130));
  }

  function apply() {
    if (homeOk) root.classList.remove('ts-homeload');
    root.classList.toggle('ts-home', homeOk);
    root.classList.toggle('ts-xz-covered', homeOk || subOpen || root.classList.contains('ts-homeload'));
    syncHomeVis(homeOk && !subOpen && !sheetOpen());
    syncInboxSchedule();
    syncVoicePill();
    vcSync();       // 打开「对话」时卡片收起（对话本身就在眼前），回来还在说就再出来
    syncNotice();
  }
  function showHome() {
    closeBrightness(false);
    apply();
  }
  const subRetryCounts = new Map(), subRetryTimers = new Map();
  function forceCloseSheets() {
    const s = getSettings(), w = getWifi(), ae = document.activeElement;
    if (ae && ((s && s.contains(ae)) || (w && w.contains(ae)))) { try { ae.blur(); } catch (e) {} }
    if (typeof window.__tsCloseBaseSheets === 'function') window.__tsCloseBaseSheets();
    s?.classList.remove('on');
    w?.classList.remove('on');
    $('sheet')?.classList.remove('on');
  }
  function armSubShowTimer(entry) {
    clearTimeout(subShowTimer);
    const subLoadMs = (entry.port === '8096' || entry.port === '8097') ? 12000 : 8000;
    subShowTimer = setTimeout(() => {
      if (subOpen && activeSub === entry && !entry.ready) {
        if (subRetryTimers.has(entry.url) && (subRetryCounts.get(entry.url) || 0) <= 8) {
          armSubShowTimer(entry);
          return;
        }
        clearTimeout(subRetryTimers.get(entry.url));
        subRetryTimers.delete(entry.url);
        entry.failed = true;
        entry.loading = false;
        subErr.classList.add('show');
      }
    }, subLoadMs);
  }
  function attachSubLoadGuard(entry, title, winName) {
    entry.frame.addEventListener('load', () => {
      setTimeout(() => {
        if (subEntries.get(entry.url) !== entry || entry.ready) return;
        entry.loading = false;
        entry.failed = true;
        const n = (subRetryCounts.get(entry.url) || 0) + 1;
        subRetryCounts.set(entry.url, n);
        if (subOpen && activeSub === entry) {
          if (n <= 8) {
            clearTimeout(subRetryTimers.get(entry.url));
            subRetryTimers.set(entry.url, setTimeout(() => {
              subRetryTimers.delete(entry.url);
              if (subOpen && activeSub === entry && !entry.ready) {
                entry.failed = false;
                entry.loading = true;
                entry.startedAt = Date.now();
                subErr.classList.remove('show');
                armSubShowTimer(entry);
                entry.frame.src = entry.url;
              }
            }, 1000));
          } else {
            clearTimeout(subShowTimer);
            subErr.classList.add('show');
          }
        } else {
          entry.frame.remove();
          subEntries.delete(entry.url);
          if (n <= 8) {
            clearTimeout(subRetryTimers.get(entry.url));
            subRetryTimers.set(entry.url, setTimeout(() => {
              subRetryTimers.delete(entry.url);
              prewarmSub(entry.url, title, winName);
            }, 2200));
          }
        }
      }, 250);
    });
  }
  function openSub(url, subCmd = '') {
    if (!okSubUrl(url)) return false;
    const cleanSubCmd = (typeof subCmd === 'string' && /^[a-zA-Z0-9_-]{1,24}$/.test(subCmd)) ? subCmd : '';
    const u = new URL(url), key = u.origin + '/';
    closeBrightness(false);
    forceCloseSheets();
    if (subOpen && activeSub && activeSub.url === key) {
      if (cleanSubCmd) {
        if (activeSub.ready) {
          try { activeSub.frame.contentWindow?.postMessage({ ts: 'sub', cmd: cleanSubCmd }, activeSub.origin); } catch (e) {}
        } else {
          activeSub.pendingCmd = cleanSubCmd;
        }
      }
      return true;
    }
    clearTimeout(subFadeTimer); subFadeTimer = 0;
    clearTimeout(subRetryTimers.get(key)); subRetryTimers.delete(key);
    clearTimeout(subEvictTimers.get(key)); subEvictTimers.delete(key);
    subRetryCounts.delete(key);
    if (activeSub && activeSub.url !== key) {
      const prev = activeSub;
      orReleaseChildTouch();
      clearTimeout(subRetryTimers.get(prev.url));
      subRetryTimers.delete(prev.url);
      prev.frame.classList.remove('active', 'leaving');
      if (prev.ready) {
        syncSubVis(prev, false, true);
        scheduleSubEvict(prev);
        if (prev.port === '8092') setTimeout(pollServerBadge, 350);
      } else {
        prev.frame.remove();
        subEntries.delete(prev.url);
      }
    }
    let entry = subEntries.get(key);
    if (entry && (entry.failed || (!entry.ready && (!entry.loading || Date.now() - entry.startedAt > 7000)))) {
      entry.frame.remove();
      subEntries.delete(key);
      entry = null;
    }
    if (!entry) {
      const f = document.createElement('iframe');
      const title = u.port === '8091' ? 'QQ音乐' : (u.port === '8096' ? 'AI讲题' : '服务器监控');
      const winName = u.port === '8091' ? 'ts-prewarm' : (u.port === '8096' ? 'ts-tutor-prewarm' : 'ts-monitor-prewarm');
      f.title = title;
      f.dataset.port = u.port;
      subLayer.insertBefore(f, subLoading);
      entry = { url: key, origin: u.origin, port: u.port, frame: f, ready: false, loading: false, failed: false, startedAt: 0, pendingCmd: '' };
      subEntries.set(key, entry);
      attachSubLoadGuard(entry, title, winName);
    } else if (!entry.ready) {
      try { entry.frame.name = ''; } catch (e) {}
    }
    entry.pendingCmd = cleanSubCmd;
    activeSub = entry;
    for (const v of subEntries.values()) {
      v.frame.classList.remove('leaving');
      v.frame.classList.toggle('active', v === entry);
    }
    subOpen = true;
    subUrl = key;
    clearTimeout(subShowTimer);
    subErr.classList.remove('show');
    if (entry.ready) {
      subLayer.classList.add('ready', 'warm');
      root.classList.add('ts-sub');
      syncSubOpaque(true);
      apply();
      syncSubVis(entry, true, true);
      if (entry.pendingCmd) {
        const cmdToSend = entry.pendingCmd; entry.pendingCmd = '';
        try { entry.frame.contentWindow?.postMessage({ ts: 'sub', cmd: cmdToSend }, entry.origin); } catch (e) {}
      }
      return true;
    }
    syncSubOpaque(false);
    subLayer.classList.remove('ready', 'warm');
    subLabelEl.textContent = u.port === '8091' ? '正在打开 QQ音乐' : (u.port === '8096' ? '正在打开 AI讲题' : '正在连接服务器');
    root.classList.add('ts-sub');
    apply();
    if (!entry.loading) {
      entry.loading = true;
      entry.startedAt = Date.now();
      entry.frame.src = key;
    }
    // A load event also fires for an HTTP error page. Only our child page's handshake is ready.
    armSubShowTimer(entry);
    return true;
  }
  function scheduleSubEvict(entry) {
    if (!entry || entry.port !== '8096' || !window.__tsAllowSubEvict) return;
    const evictUrl = entry.url;
    clearTimeout(subEvictTimers.get(evictUrl));
    subEvictTimers.set(evictUrl, setTimeout(() => {
      subEvictTimers.delete(evictUrl);
      if ((!subOpen || activeSub !== entry) && subEntries.get(evictUrl) === entry) {
        clearTimeout(subPauseTimers.get(evictUrl));
        subPauseTimers.delete(evictUrl);
        try { entry.frame.src = 'about:blank'; } catch (e) {}
        entry.frame.remove();
        subEntries.delete(evictUrl);
      }
    }, 180000));
  }
  function closeSub(toHome = true) {
    if (!subOpen) return;
    syncSubOpaque(false);
    orReleaseChildTouch();
    const cur = activeSub;
    const smoothExit = toHome && !document.body.classList.contains('ringing') && !!(cur && cur.ready);
    subOpen = false;
    subUrl = '';
    activeSub = null;
    clearTimeout(subShowTimer);
    clearTimeout(subFadeTimer); subFadeTimer = 0;
    subLayer.classList.remove('ready', 'warm');
    subErr.classList.remove('show');
    closeBrightness(false);
    if (cur) {
      clearTimeout(subRetryTimers.get(cur.url));
      subRetryTimers.delete(cur.url);
      clearTimeout(subPauseTimers.get(cur.url));
      subPauseTimers.delete(cur.url);
      clearTimeout(subEvictTimers.get(cur.url));
      subEvictTimers.delete(cur.url);
      cur.frame.classList.remove('active');
      if (cur.ready) {
        if (smoothExit) {
          cur.frame.classList.add('leaving');
          subFadeTimer = setTimeout(() => { subFadeTimer = 0; cur.frame.classList.remove('leaving'); }, 125);
        } else {
          cur.frame.classList.remove('leaving');
        }
        syncSubVis(cur, false, !smoothExit);
        scheduleSubEvict(cur);
      } else {
        cur.frame.classList.remove('leaving');
        cur.frame.remove();
        subEntries.delete(cur.url);
      }
    }
    apply();
    root.classList.remove('ts-sub');
    if (toHome && cur && cur.port === '8092') setTimeout(pollServerBadge, 350);
  }
  let prewarmScheduled = false;
  function prewarmSub(key, title, winName) {
    if (subOpen || !homeOk || isBusy() || subEntries.has(key)) return;
    const u = new URL(key);
    const f = document.createElement('iframe');
    f.title = title;
    f.dataset.port = u.port;
    f.name = winName;
    subLayer.insertBefore(f, subLoading);
    const entry = { url: key, origin: u.origin, port: u.port, frame: f, ready: false, loading: true, failed: false, startedAt: Date.now() };
    subEntries.set(key, entry);
    attachSubLoadGuard(entry, title, winName);
    f.src = key;
  }
  let srvBadgeTimer = 0, srvBadgeBusy = false, srvBadgeForcePending = false;
  let lastSrvBadgeSig = '', lastSrvOpenSeq = null, srvVoicePollT1 = 0, srvVoicePollT2 = 0;
  let lastTutorOpenSeq = null, lastTutorWake = null, tutorBadgeTimer = 0, tutorBadgeBusy = false, tutorBadgeForcePending = false, tutorBadgeHotplugPending = false;
  function handleTutorWake(tw) {
    if (typeof tw !== 'string' || !tw) return;
    if (lastTutorWake === null) { lastTutorWake = tw; return; }
    if (tw === lastTutorWake) return;
    const s1 = lastTutorWake.indexOf(':');
    const oldSig = s1 >= 0 ? lastTutorWake.slice(0, s1) : '';
    const oldUsb = s1 >= 0 ? lastTutorWake.slice(s1 + 1) : '';
    const s2 = tw.indexOf(':');
    const newSig = s2 >= 0 ? tw.slice(0, s2) : '';
    const newUsb = s2 >= 0 ? tw.slice(s2 + 1) : '';
    lastTutorWake = tw;
    const sigChanged = newSig !== oldSig && newSig !== '0';
    const isHotplug = !oldUsb && !!newUsb;
    if (sigChanged || isHotplug) pollTutorBadge(true, isHotplug);
  }
  function scheduleIdlePrewarm() {
    if (prewarmScheduled) return;
    prewarmScheduled = true;
    // Staggered idle prewarm: QQ音乐 (:8091), Server Monitor skeleton (:8092/ with ts-monitor-prewarm
    // which reads static /assets/servers.json ONLY — never /api/state, keeping Nezha WS 100% idle!),
    // and Brightness (:8092/display). Every dock item opens in <1ms on first tap!
    setTimeout(() => prewarmSub('http://127.0.0.1:8091/', 'QQ音乐', 'ts-prewarm'), 1100);
    setTimeout(() => prewarmSub('http://127.0.0.1:8092/', '服务器监控', 'ts-monitor-prewarm'), 1700);
    setTimeout(() => prewarmSub('http://127.0.0.1:8096/', 'AI讲题', 'ts-tutor-prewarm'), 2300);
    clearTimeout(srvBadgeTimer);
    srvBadgeTimer = setTimeout(pollServerBadge, 3200);
  }
  async function pollServerBadge(forceCheck = false) {
    clearTimeout(srvBadgeTimer); srvBadgeTimer = 0;
    if (document.hidden || !homeOk) return;
    if (srvBadgeBusy) {
      if (forceCheck) srvBadgeForcePending = true;
      return;
    }
    srvBadgeBusy = true;
    try {
      if (isHomeActiveView() || forceCheck) {
        const ctl = new AbortController(), t = setTimeout(() => ctl.abort(), 5000);
        try {
          const r = await fetch(MON + '/api/badge', { cache: 'no-store', signal: ctl.signal });
          if (r.ok) {
            const j = await r.json();
            const sig = `${j?.text || ''}:${j?.amber ? 1 : 0}:${j?.bad ? 1 : 0}`;
            if (sig !== lastSrvBadgeSig) {
              lastSrvBadgeSig = sig;
              try { frame.contentWindow?.postMessage({ ts: 'home', srvBadge: { text: j?.text || '', amber: !!j?.amber, bad: !!j?.bad } }, HOME); } catch (e) {}
            }
            const seq = Number(j?.open_ui_seq) || 0;
            if (lastSrvOpenSeq === null) {
              lastSrvOpenSeq = seq;
              if (forceCheck && seq > 0 && j?.open_ui_fresh && !document.body.classList.contains('ringing')) {
                openSub(MON + '/', j.open_ui_cmd || 'overview');
              }
            } else if (seq > lastSrvOpenSeq) {
              lastSrvOpenSeq = seq;
              if (j?.open_ui_fresh && !document.body.classList.contains('ringing')) {
                openSub(MON + '/', j.open_ui_cmd || 'overview');
              }
            }
          }
        } catch (e) {} finally { clearTimeout(t); }
      }
    } finally {
      srvBadgeBusy = false;
      clearTimeout(srvBadgeTimer); srvBadgeTimer = 0;
      if (!document.hidden && homeOk) {
        if (srvBadgeForcePending) {
          srvBadgeForcePending = false;
          srvBadgeTimer = setTimeout(() => pollServerBadge(true), 80);
        } else {
          const srvPollMs = (lastSrvBadgeSig && !lastSrvBadgeSig.endsWith(':0:0')) ? 60000 : 240000;
          srvBadgeTimer = setTimeout(pollServerBadge, srvPollMs);
        }
      } else {
        srvBadgeForcePending = false;
      }
    }
  }
  async function pollTutorBadge(forceCheck = false, hotplug = false) {
    clearTimeout(tutorBadgeTimer); tutorBadgeTimer = 0;
    if (document.hidden || !homeOk || !forceCheck) return;
    if (tutorBadgeBusy) {
      tutorBadgeForcePending = true;
      if (hotplug) tutorBadgeHotplugPending = true;
      return;
    }
    tutorBadgeBusy = true;
    try {
      const inTutorNow = subOpen && activeSub && activeSub.url === 'http://127.0.0.1:8096/';
      if ((!isBusy() && !inTutorNow && !sheetOpen()) || forceCheck) {
        const ctl = new AbortController(), t = setTimeout(() => ctl.abort(), 10000);
        try {
          const url = 'http://127.0.0.1:8096/api/tutor/badge' + (hotplug ? '?hotplug=1' : '');
          const r = await fetch(url, { cache: 'no-store', signal: ctl.signal });
          if (r.ok) {
            const j = await r.json();
            const seq = Number(j?.open_ui_seq) || 0;
            if (lastTutorOpenSeq === null) {
              lastTutorOpenSeq = seq;
              if (seq > 0 && j?.open_ui_fresh && !document.body.classList.contains('ringing')) {
                openSub('http://127.0.0.1:8096/');
              }
            } else if (seq > lastTutorOpenSeq) {
              lastTutorOpenSeq = seq;
              if (j?.open_ui_fresh && !document.body.classList.contains('ringing')) {
                openSub('http://127.0.0.1:8096/');
              }
            }
          }
        } catch (e) {} finally { clearTimeout(t); }
      }
    } finally {
      tutorBadgeBusy = false;
      clearTimeout(tutorBadgeTimer); tutorBadgeTimer = 0;
      if (!document.hidden && homeOk && tutorBadgeForcePending) {
        const hp = tutorBadgeHotplugPending;
        tutorBadgeForcePending = false;
        tutorBadgeHotplugPending = false;
        tutorBadgeTimer = setTimeout(() => pollTutorBadge(true, hp), 80);
      } else {
        tutorBadgeForcePending = false;
        tutorBadgeHotplugPending = false;
      }
    }
  }
  function scheduleVoiceSrvCheck() {
    if (document.hidden || !homeOk) return;
    clearTimeout(srvVoicePollT1); clearTimeout(srvVoicePollT2);
    srvVoicePollT1 = setTimeout(() => { pollServerBadge(true); triggerInboxPoll(); }, 140);
    srvVoicePollT2 = setTimeout(() => { pollServerBadge(true); triggerInboxPoll(); }, 920);
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && homeOk) { pollServerBadge(); } });
  /* ---------------- 深色 / 浅色 (2026-11-12): picked in the brightness panel, remembered here (8090 storage),
     sent to the home page, the sub pages and the panel; each of them also caches it for a flash-free start ---------------- */
  const THEME_KEY = 'ts.theme';
  let uiTheme = 'dark';
  try { const t = localStorage.getItem(THEME_KEY); if (t === 'light' || t === 'dark') uiTheme = t; } catch (e) {}
  // 2026-11-13 自动: 浅色 from sunrise to sunset (the home page sends the times; 07:00 / 18:00 until it has), 深色 at night.
  // It is the default; picking 深色 or 浅色 in the brightness panel turns it off, 自动 turns it back on.
  const MODE_KEY = 'ts.themeMode', SUN_KEY = 'ts.sun';
  let themeMode = 'auto', sunMin = null;
  try { if (localStorage.getItem(MODE_KEY) === 'manual') themeMode = 'manual'; } catch (e) {}
  const okSun = (r, s) => Number.isFinite(r) && Number.isFinite(s) && r >= 240 && r <= 570 && s >= 930 && s <= 1260;
  try { const j = JSON.parse(localStorage.getItem(SUN_KEY) || 'null'); if (j && okSun(j.rise, j.set)) sunMin = { rise: j.rise, set: j.set }; } catch (e) {}
  const autoTheme = () => { const n = new Date(), m = n.getHours() * 60 + n.getMinutes(), r = sunMin ? sunMin.rise : 420, s = sunMin ? sunMin.set : 1080; return m >= r && m < s ? 'light' : 'dark'; };
  if (themeMode === 'auto') { uiTheme = autoTheme(); try { localStorage.setItem(THEME_KEY, uiTheme); } catch (e) {} }
  root.classList.toggle('ts-light', uiTheme === 'light');
  const sendTheme = (win, origin, kind) => { try { win?.postMessage({ ts: kind, theme: uiTheme, mode: themeMode }, origin); } catch (e) {} };
  function broadcastTheme() {
    sendTheme(frame.contentWindow, HOME, 'home');
    for (const v of subEntries.values()) sendTheme(v.frame.contentWindow, v.origin, 'sub');
    sendTheme(brightFrame?.contentWindow, MON, 'display');
    try { window.dispatchEvent(new CustomEvent('ts-ui-sync')); } catch (e) {}
  }
  function setUiTheme(t) {
    if (t !== 'light' && t !== 'dark') return;
    if (t !== uiTheme) {
      uiTheme = t;
      try { localStorage.setItem(THEME_KEY, t); } catch (e) {}
      root.classList.toggle('ts-light', t === 'light');
    }
    broadcastTheme();
  }
  function applyAutoTheme() { if (themeMode === 'auto' && autoTheme() !== uiTheme) setUiTheme(autoTheme()); }
  function setThemeMode(m) { themeMode = m === 'auto' ? 'auto' : 'manual'; try { localStorage.setItem(MODE_KEY, themeMode); } catch (e) {} }
  function setSun(rise, set) {
    if (!okSun(rise, set)) return;
    sunMin = { rise, set };
    try { localStorage.setItem(SUN_KEY, JSON.stringify(sunMin)); } catch (e) {}
    applyAutoTheme();
  }
  setInterval(() => { if (!document.hidden) applyAutoTheme(); }, 60000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) applyAutoTheme(); });
  window.addEventListener('message', e => {
    const d = e.data;
    if (e.source === frame.contentWindow) {
      if (e.origin === HOME && d?.ts === 'home' && d.ready) sendTheme(frame.contentWindow, HOME, 'home');
      return;
    }
    const bwin = brightFrame?.contentWindow;
    if (bwin && e.source === bwin) {
      if (e.origin !== MON) return;
      if (d?.thinksmart === 'displayReady') sendTheme(bwin, MON, 'display');
      else if (d?.thinksmart === 'setTheme') {
        if (d.theme === 'auto') { setThemeMode('auto'); applyAutoTheme(); broadcastTheme(); }
        else if (d.theme === 'light' || d.theme === 'dark') { setThemeMode('manual'); setUiTheme(d.theme); }
      }
      return;
    }
    for (const v of subEntries.values()) {
      if (e.source === v.frame.contentWindow && e.origin === v.origin) {
        if (d?.ts === 'sub' && d.cmd === 'ready') sendTheme(v.frame.contentWindow, v.origin, 'sub');
        return;
      }
    }
  });

  $('tsSubBack').onclick = () => closeSub(true);
  window.__tsOpenSub = openSub;
  window.__tsCloseSub = closeSub;
  window.__tsTheme = {
    get: () => ({ theme: uiTheme, mode: themeMode, sun: sunMin }),
    set: v => {
      if (v === 'auto') { setThemeMode('auto'); applyAutoTheme(); broadcastTheme(); }
      else if (v === 'light' || v === 'dark') { setThemeMode('manual'); setUiTheme(v); }
    },
  };
  // section：分类名（'alarm'、'sound'…）直接开到那一类；true 从「显示」开始
  function openUnifiedSettings(section = true) {
    closeBrightness(false);
    bindSheetObs();
    if (typeof window.__tsOpenSettings === 'function') window.__tsOpenSettings(section);
    else $('btnSettings')?.click();
    bindSheetObs();
    if (sheetOpen()) apply();
  }
  window.__tsOpenUnifiedSettings = openUnifiedSettings;
  let vcDismissCard = () => {};
  window.__tsGoHome = () => {
    closeBrightness(false);
    forceCloseSheets();
    if (subOpen) closeSub(true);
    else showHome();
    const sendGoHome = () => { try { frame.contentWindow?.postMessage({ ts: 'home', goHome: true }, HOME); } catch (e) {} };
    sendGoHome();
    if (!homeOk) setTimeout(sendGoHome, 120);
  };
  window.__tsNavigate = nav => {
    if (document.body.classList.contains('ringing')) return;
    const p = (nav && nav.page) || 'home';
    vcDismissCard();
    if (p === 'home') window.__tsGoHome();
    else if (p === 'music') openSub('http://127.0.0.1:8091/');
    else if (p === 'radio') openSub('http://127.0.0.1:8091/', 'openRadio');
    else if (p === 'server') openSub('http://127.0.0.1:8092/', 'overview');
    else if (p === 'tutor') openSub('http://127.0.0.1:8096/');
    else if (p === 'settings') {
      if (subOpen) closeSub(false);
      openUnifiedSettings(nav.section || true);
    }
    else if (p === 'weather') window.__tsOpenHomeWeather(nav.city || '');
    else if (p === 'alarms') window.__tsOpenHomeAlarms();
    else if (p === 'messages') window.__tsOpenHomeMessages();
    else if (p === 'countdown') window.__tsOpenHomeCountdown();
    else if (p === 'timer') window.__tsOpenHomeFocus();
    else if (p === 'sleep') window.__tsOpenHomeSleep();
  };
  window.__tsOpenHomePanel = payload => {
    window.__tsGoHome();
    if (!payload || typeof payload !== 'object') return;
    const msg = Object.assign({ ts: 'home' }, payload);
    const send = () => { try { frame.contentWindow?.postMessage(msg, HOME); } catch (e) {} };
    send();
    if (!homeOk) setTimeout(send, 120);
  };
  // 闹钟、睡眠、专注、天气、倒数日都在主页里：设置、胶囊和小提示都从这里直达
  window.__tsOpenHomeAlarms = () => { window.__tsOpenHomePanel({ openAlarms: true }); };
  window.__tsOpenHomeSleep = () => window.__tsOpenHomePanel({ openSleep: true });
  window.__tsOpenHomeFocus = () => window.__tsOpenHomePanel({ openTimer: true });
  window.__tsOpenHomeWeather = city => window.__tsOpenHomePanel({ openWeather: true, city: city || '' });
  window.__tsOpenHomeCountdown = () => window.__tsOpenHomePanel({ openCountdown: true });
  window.__tsOpenHomeMessages = () => window.__tsOpenHomePanel({ openMessages: true });

  let bootRetryTimer = 0, bootDeadline = 0, bootCycles = 0;
  frame.addEventListener('load', () => {
    clearTimeout(bootRetryTimer);
    if (!homeOk && Date.now() < bootDeadline) {
      bootRetryTimer = setTimeout(() => { if (!homeOk && Date.now() < bootDeadline) { lastHomeVis = null; frame.src = HOME + '/?t=' + Date.now(); } }, 650);
    }
  });
  function baseNote() { const el = $('baseNote'); if (el) el.textContent = bootCycles > 1 ? '主页暂时打不开，正在重试。下面的可以直接用' : '主页正在打开…'; }
  function loadHome() {
    homeOk = false;
    lastHomeVis = null;
    bootCycles++;
    bootDeadline = Date.now() + 14000;
    if (bootCycles === 1) {   // a plain cover for the first seconds; after that the base page's clock shows while we retry
      root.classList.add('ts-homeload');
      setTimeout(() => { if (!homeOk) { root.classList.remove('ts-homeload'); baseNote(); apply(); } }, 5000);
    }
    baseNote();
    apply();
    frame.src = bootCycles === 1 ? HOME + '/' : HOME + '/?t=' + Date.now();
    clearTimeout(readyTimer);
    clearTimeout(bootRetryTimer);
    readyTimer = setTimeout(() => {
      if (homeOk) return;
      clearTimeout(bootRetryTimer);
      root.classList.remove('ts-homeload'); baseNote(); apply();   // home service down: the base page stays usable
      setTimeout(loadHome, bootCycles <= 3 ? 2500 : 15000);
    }, 14500);
  }
  window.addEventListener('message', e => {
    let matchedSub = null;
    for (const v of subEntries.values()) {
      if (e.source === v.frame.contentWindow && e.origin === v.origin) { matchedSub = v; break; }
    }
    if (matchedSub) {
      const d = e.data;
      if (d?.ts === 'sub' && d.cmd === 'reloading') {
        matchedSub.ready = false;
        matchedSub.failed = false;
        matchedSub.loading = true;
        matchedSub.startedAt = Date.now();
        subRetryCounts.delete(matchedSub.url);
        if (subOpen && activeSub === matchedSub) {
          syncSubOpaque(false);
          subLayer.classList.remove('ready');
          subErr.classList.remove('show');
          armSubShowTimer(matchedSub);
        }
        return;
      }
      if (d?.ts === 'sub' && d.cmd === 'ready') {
        clearTimeout(subRetryTimers.get(matchedSub.url));
        subRetryTimers.delete(matchedSub.url);
        subRetryCounts.delete(matchedSub.url);
        matchedSub.ready = true;
        matchedSub.loading = false;
        matchedSub.failed = false;
        if (subOpen && activeSub === matchedSub) {
          clearTimeout(subShowTimer);
          subErr.classList.remove('show');
          subLayer.classList.add('ready');
          syncSubOpaque(true);
          syncSubVis(matchedSub, true, true);
          if (matchedSub.pendingCmd) {
            const cmdToSend = matchedSub.pendingCmd; matchedSub.pendingCmd = '';
            try { matchedSub.frame.contentWindow?.postMessage({ ts: 'sub', cmd: cmdToSend }, matchedSub.origin); } catch (e) {}
          }
        } else {
          syncSubVis(matchedSub, false, true);
        }
        return;
      }
      if (subOpen && activeSub === matchedSub) {
        if (d?.type === 'CLOSE_QQMUSIC_OVERLAY' || (d?.ts === 'sub' && d.cmd === 'close')) { closeSub(true); return; }
        if (d?.ts === 'sub' && d.cmd === 'brightness') { openUnifiedSettings(true); return; }
      }
      return;
    }
    if (e.origin !== HOME || e.source !== frame.contentWindow) return;
    const d = e.data;
    if (!d || d.ts !== 'home') return;
    if (d.ready) { homeOk = true; lastHomeVis = null; clearTimeout(readyTimer); clearTimeout(bootRetryTimer); apply(); scheduleIdlePrewarm(); return; }
    if (d.cmd === 'xiaozhi') {
      if (d.then === 'settings') {
        openUnifiedSettings(true);
      } else if (!document.body.classList.contains('ringing')) {
        wakeXiaomai();
      }
    } else if (d.cmd === 'navigate' && okSubUrl(d.url)) {
      openSub(d.url, d.subCmd);
    } else if (d.cmd === 'brightness') {
      openUnifiedSettings(true);
    } else if (d.cmd === 'sun') {
      setSun(d.rise, d.set);
    } else if (d.cmd === 'inboxAck' && typeof d.id === 'string') {
      dismissInboxById(d.id);
    } else if (d.cmd === 'inboxSync') {
      triggerInboxPoll();
    } else if (d.cmd === 'tutorWake' && typeof d.wake === 'string') {
      handleTutorWake(d.wake);
    } else if (d.cmd === 'musicOpenSync' && d.st && typeof window.__tsQqSyncStatus === 'function') {
      window.__tsQqSyncStatus(d.st);
    } else if (d.cmd === 'timerDone') {
      if (!document.body.classList.contains('ringing') && !isBusy()) {
        closeBrightness(false);
        forceCloseSheets();
        if (subOpen) closeSub(true);
        else showHome();
      }
    }
  });
  const observedSheets = new WeakSet();
  const sheetObs = new MutationObserver(() => {
    apply();
  });
  const bindSheetObs = () => {
    for (const el of [getSettings(), getWifi(), $('sheet')]) {
      if (el && !observedSheets.has(el)) {
        observedSheets.add(el);
        sheetObs.observe(el, { attributes: true, attributeFilter: ['class'] });
      }
    }
  };
  bindSheetObs();

  // 叫醒：胶囊；响铃：盖在当前页面上面，不切页面
  let prevBodyState = document.body.dataset.state || 'idle';
  new MutationObserver(() => {
    const curState = document.body.dataset.state || 'idle';
    if (curState !== prevBodyState) {
      prevBodyState = curState;
      if (curState === 'speaking' || curState === 'idle' || curState === 'thinking') scheduleVoiceSrvCheck();
    }
    syncVoicePill();
    syncNotice();
    if (!isBusy()) return;
    stopOverlayClip();
    try { frame.contentWindow?.postMessage({ ts: 'home', stopClip: true }, HOME); } catch (e) {}
    if (document.body.classList.contains('ringing')) {
      closeBrightness(false);
      forceCloseSheets();   // 闹钟（#ring）盖在当前页面上面，停下后还在原来的页面
    }
  }).observe(document.body, { attributes: true, attributeFilter: ['data-state', 'class'] });

  /* ---------------- 2026-11-20 Claude：机身重力感应 → 整套页面横竖自动转（2026-11-25 起设置、选模型、Wi-Fi、底座也转）----------------
     每 0.45 秒读一次主页服务的 /api/home/orient（BMA253 原始读数）；读到新方向就 0.15 秒后再读一次，两次一致才转。
     拿在手里晃动（加速度不是 1g 左右，或两次读数差得多）、放平、斜 45° 左右时不转；手指按在屏幕上时等松手再转。
     转的时候先用底色盖住，新的排版画好再淡出。方向：跟随机器 / 竖版 / 横版（「设置 → 显示」和 AI 讲题设置里都能改），
     记在 8090 的 localStorage；锁住方向后机器转了，按机器现在的方向给一个「点这里改为跟随机器」的提示。
     AI 讲题的页面自己会转（它有自己的竖版），这里只把方向告诉它。整段包在 try 里：万一出错，主页照样加载。 */
  try {
  const OR_MODE_KEY = 'ts.orientMode', OR_SEEN_KEY = 'ts.orientSeen', OR_PSEEN_KEY = 'ts.portraitSeen';
  let orMode = 'auto', orSeen = 'l', orPSeen = 'p90', orCur = '', orCand = '', orCandN = 0, orTimer = 0, orSensor = null;
  let orLast = null, orStillN = 0, orTouch = 0, orTouchT = 0, orPending = false, orCoverT = 0, orHintT = 0, orHintFor = '', orEtag = '', orErrN = 0;
  try {
    const m = localStorage.getItem(OR_MODE_KEY); if (m === 'portrait' || m === 'landscape') orMode = m;
    const s = localStorage.getItem(OR_SEEN_KEY); if (s === 'p90' || s === 'p270' || s === 'l') orSeen = s;
    const p = localStorage.getItem(OR_PSEEN_KEY); if (p === 'p90' || p === 'p270') orPSeen = p;
  } catch (e) {}
  // 1g ≈ 512：右边朝上 y<0 → p90，左边朝上 y>0 → p270，横放（含 ~70° 立放与 ~25° 斜躺面）→ l；放平（xy 都小）、斜 45° 左右、拿着晃时不算
  function orClassify(x, y, z) {
    const ax = Math.abs(x), ay = Math.abs(y), g = Math.sqrt(x * x + y * y + z * z);
    if (g < 400 || g > 640) return '';
    if (Math.max(ax, ay) < 150 || Math.abs(ax - ay) < 100) return '';
    if (ay > ax) return y < 0 ? 'p90' : 'p270';
    return 'l';
  }
  const orWanted = () => (orMode === 'landscape' ? 'l' : (orMode === 'portrait' ? orPSeen : orSeen));
  function orSend(win, origin) { try { win?.postMessage({ ts: 'orient', cur: orCur, mode: orMode }, origin); } catch (e) {} }
  function orBroadcast() {
    for (const v of subEntries.values()) if (v.port === '8096') orSend(v.frame.contentWindow, v.origin);
    orSend(brightFrame?.contentWindow, MON);
    try { window.dispatchEvent(new CustomEvent('ts-ui-sync')); } catch (e) {}
  }
  const orCoverEl = document.createElement('div'); orCoverEl.id = 'tsRotCover'; orCoverEl.setAttribute('aria-hidden', 'true');
  const orHintEl = document.createElement('button'); orHintEl.id = 'tsOrHint'; orHintEl.type = 'button';
  const vcEl = document.createElement('div'); vcEl.id = 'tsVoiceCard'; vcEl.setAttribute('role', 'status');
  vcEl.innerHTML = `<div class="vc-head">${ORB}<span class="vc-st" id="tsVcState"></span>${WAVE('')}<span class="vc-src" id="tsVcSrc"></span>`
    + `<span class="vp-acts"><button type="button" class="vp-btn vp-exp" id="tsVcExpand" aria-label="看对话">${VP_EXPAND}</button><button type="button" class="vp-btn vp-stop" id="tsVcStop" aria-label="停下">${VP_STOP}</button><button type="button" class="vp-btn" id="tsVcClose" aria-label="收起">${VP_CLOSE}</button></span></div>`
    + '<div class="vc-q" id="tsVcQ"></div><div class="vc-a" id="tsVcA"></div><div class="vc-rich" id="tsVcRich"></div><div class="vc-chips" id="tsVcChips"></div><i class="vc-left" aria-hidden="true"></i>';
  voiceWaves.push(...vcEl.querySelectorAll('.vp-wave b'));
  let vgDone = false;
  document.body.append(orCoverEl, vcEl, orHintEl);
  function orSetClasses(o) {
    orCur = o;
    root.classList.toggle('ts-portrait', o !== 'l');
    root.classList.toggle('ts-rot270', o === 'p270');
    syncVoicePill();
    syncNotice();
  }
  // 转完以后卡片跟着转
  function orSyncVoiceView() {
    if (homeOk) syncVoiceCard();
  }
  let orGoSeq = 0, orRaf1 = 0;
  function cancelOrRaf() {
    ++orGoSeq;
    if (orRaf1) { nativeCaf(orRaf1); orRaf1 = 0; }
  }
  function orGo() {
    cancelOrRaf();
    const o = orWanted();
    if (o === orCur) {
      clearTimeout(orCoverT); orCoverT = 0;
      orCoverEl.classList.remove('on');
      return;
    }
    if (document.hidden) {   // 后台隐藏：直接换，不用盖
      clearTimeout(orCoverT); orCoverT = 0;
      orCoverEl.classList.remove('on');
      orSetClasses(o); orBroadcast(); orSyncVoiceView(); return;
    }
    const seq = ++orGoSeq;
    clearTimeout(orCoverT);
    orCoverEl.classList.add('on');
    orCoverT = setTimeout(() => { orCoverT = 0; orCoverEl.classList.remove('on'); }, 650);
    orRaf1 = nativeRaf(() => nativeRaf(() => {
      if (seq !== orGoSeq) return;
      orRaf1 = 0;
      const latest = orWanted();
      if (latest !== orCur) { orSetClasses(latest); orBroadcast(); orSyncVoiceView(); }
      else orBroadcast();
      clearTimeout(orCoverT);
      orCoverT = setTimeout(() => orCoverEl.classList.remove('on'), 120);   // 页面按新尺寸排好、画好再淡出
    }));
  }
  let orTopDown = false;
  const orTouching = () => orTouch && Date.now() - orTouch < 6000;
  orReleaseChildTouch = () => { if (!orTopDown && orTouch) orTouchOn(false); };
  function orTouchOn(on) {
    clearTimeout(orTouchT);
    if (on) {
      orTouch = Date.now();
      orTouchT = setTimeout(() => { orTouch = 0; orTopDown = false; if (orPending) { orPending = false; orGo(); } }, 6000);
      return;
    }
    orTouch = 0;
    if (orPending) orTouchT = setTimeout(() => { orPending = false; orGo(); }, 300);
  }
  function orRequest() { if (orTouching()) orPending = true; else orGo(); }
  // 锁住方向时机器转了：提示按机器现在的方向摆，6 秒后自己收起；同一次转动只提示一次
  function orHintCheck() {
    const lockedP = orMode === 'portrait', lockedL = orMode === 'landscape';
    const physP = orSeen !== 'l';
    const mismatch = (lockedL && physP) || (lockedP && !physP);
    if (!mismatch) { orHintFor = ''; orHintEl.classList.remove('show'); return; }
    if (orHintFor === orSeen) return;
    orHintFor = orSeen;
    orHintEl.dataset.phys = orSeen;
    orHintEl.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="3" width="10" height="18" rx="2"/><path d="M3 9a9 9 0 0 1 4-5M21 15a9 9 0 0 1-4 5"/></svg>'
      + (lockedL ? '屏幕方向已固定为横版 · 点这里改为跟随机器' : '屏幕方向已固定为竖版 · 点这里改为跟随机器');
    orHintEl.classList.add('show');
    clearTimeout(orHintT); orHintT = setTimeout(() => orHintEl.classList.remove('show'), 6000);
  }
  orHintEl.onclick = () => { orHintEl.classList.remove('show'); orSetMode('auto'); };
  function orSetMode(m) {
    if (m !== 'auto' && m !== 'portrait' && m !== 'landscape') return;
    orMode = m;
    try { localStorage.setItem(OR_MODE_KEY, m); } catch (e) {}
    orHintFor = ''; orHintEl.classList.remove('show');
    orGo();
    if (!orRaf1) orBroadcast();
    orSchedule(30);
  }
  function orSchedule(ms) {
    clearTimeout(orTimer);
    const autoMs = orStillN >= 12 ? 3500 : (orStillN >= 4 ? 1800 : 900);
    orTimer = setTimeout(orPoll, ms != null ? ms : (orSensor === false ? 30000 : (orMode === 'auto' ? autoMs : 12000)));
  }
  async function orPoll() {
    orTimer = 0;
    if (document.hidden) return;   // 看不见时不读，回到前台再读
    let next = null;
    const ctl = new AbortController(), to = setTimeout(() => ctl.abort(), 2500);
    try {
      const hdrs = (orEtag && !orCand) ? { 'If-None-Match': orEtag } : undefined;
      const r = await fetch(HOME + '/api/home/orient', { cache: 'no-store', headers: hdrs, signal: ctl.signal });
      if (r.status === 304) {
        orErrN = 0;
        orSensor = true;
        orStillN = Math.min(20, orStillN + 1);
        orCand = ''; orCandN = 0;
      } else if (!r.ok) {
        orEtag = '';
        next = Math.min(8000, 1500 * ++orErrN);
      } else {
        const j = await r.json().catch(() => null);
        orErrN = 0;
        orSensor = !!(j && j.sensor);
        const respEtag = orSensor ? (r.headers.get('ETag') || '') : '';
        orEtag = '';
        if (orSensor) {
          const x = Number(j.x) || 0, y = Number(j.y) || 0, z = Number(j.z) || 0;
          const delta = orLast ? (Math.abs(x - orLast.x) + Math.abs(y - orLast.y) + Math.abs(z - orLast.z)) : 999;
          const moving = !!orLast && Math.abs(x - orLast.x) + Math.abs(y - orLast.y) + Math.abs(z - orLast.z) > 160;
          orStillN = delta <= 16 ? Math.min(20, orStillN + 1) : 0;
          orLast = { x, y, z };
          const c = moving ? '' : orClassify(x, y, z);
          if (c && c !== orSeen) {
            if (c === orCand) orCandN += 1; else { orCand = c; orCandN = 1; }
            if (orCandN >= 2) {
              orSeen = c; orCand = ''; orCandN = 0;
              if (c !== 'l') orPSeen = c;
              try { localStorage.setItem(OR_SEEN_KEY, c); if (c !== 'l') localStorage.setItem(OR_PSEEN_KEY, c); } catch (e) {}
              orHintCheck();
              if (orMode === 'auto' || (orMode === 'portrait' && c !== 'l')) orRequest();
            } else next = 150;                       // 读到新方向：0.15 秒后再读一次确认
          } else if (moving) next = 200;            // 还在动：稍后再看
          else { orCand = ''; orCandN = 0; }
          if (!moving && !orCand && (c === orSeen || (Math.abs(x) < 160 && Math.abs(y) < 160))) orEtag = respEtag;
        }
      }
    } catch (e) {
      orEtag = '';
      next = Math.min(8000, 1500 * ++orErrN);
    } finally { clearTimeout(to); }
    orSchedule(next);
  }
  // ---------------- 灵动胶囊的卡片（2026-11-25 Claude 改版，横竖屏通用）----------------
  // 内容来自 8090 底页的对话（#messages，也就是「设置 → 对话」）+ 工具事件（app.js 的 ts-voice-tool）+ 本机状态（/api/rich/context，只读）。
  // 有工具事件才出对应的卡片（天气 / 音乐 / 闹钟 / 计时 / 倒数日 / 留言 / 新闻 / 服务器 / 讲题 / 音量亮度 / 睡眠），没有就只显示问答。
  const xzBox = $('messages');
  let vcHideT = 0, vcClosed = false, vcSince = null, vcSinceAt = 0, vcTurnTool = null, vcRichSig = '', vcRichFetchSeq = 0, vcSpec = null, vcCtx = null, vcCtxSig = '';
  let vcLastQ = '', vcPollT = 0, vcTickT = 0, vcScrollBy = 0;
  const escHtml = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const SKIP_MSG_CLS = /\b(?:ts|cut|msg-rich)\b/;
  const walkMsg = n => {
    if (!n) return '';
    if (n.nodeType === 3) return n.nodeValue || '';
    if (n.nodeType !== 1 || n.tagName === 'IMG' || SKIP_MSG_CLS.test(n.className || '')) return '';
    let s = '';
    for (let c = n.firstChild; c; c = c.nextSibling) s += walkMsg(c);
    return s;
  };
  const msgText = el => el ? walkMsg(el).trim() : '';
  const okImg = u => typeof u === 'string' && !/[\x00-\x20\x7f\\]/.test(u) && /^(?:https?:\/\/(?:[a-z0-9-]+\.)*(?:gtimg\.cn|qq\.com|qlogo\.cn|qpic\.cn)\/|data:image\/(?:png|jpeg|jpg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=]+$|http:\/\/(?:127\.0\.0\.1|localhost):8091\/api\/music\/radio\/emblem\/[A-Za-z0-9_-]+\.svg$)/i.test(u);
  const line = (d, w = 2) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
  const RC_ICONS = {
    music: line('<path d="M9 18V6l11-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>'),
    alarm: line('<circle cx="12" cy="13.5" r="7"/><path d="M12 10v3.5l2.5 1.5M4 5.5 6.5 3M20 5.5 17.5 3"/>'),
    timer: line('<circle cx="12" cy="13.5" r="7.5"/><path d="M12 10v3.5l2.5 1.5M9.5 3h5"/>'),
    cd: line('<rect x="4" y="5" width="16" height="15" rx="3"/><path d="M8 3v4M16 3v4M4 10h16M9 15l2 2 4-4"/>'),
    msg: line('<path d="M4 5h16v11H8l-4 4z"/><path d="M8 9h8M8 12.5h5"/>'),
    server: line('<rect x="4" y="4" width="16" height="7" rx="2"/><rect x="4" y="13" width="16" height="7" rx="2"/><path d="M8 7.5h.01M8 16.5h.01M12 7.5h4M12 16.5h4"/>'),
    tutor: line('<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 16l-2 4M16 16l2 4M7.5 12.5l3.5-4.5 3 2.5 3-3.5"/>'),
    moon: line('<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>'),
    vol: line('<path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"/>'),
    sun: line('<circle cx="12" cy="12" r="4.2"/><path d="M12 2v2.4M12 19.6V22M2 12h2.4M19.6 12H22M4.9 4.9l1.7 1.7M17.4 17.4l1.7 1.7M4.9 19.1l1.7-1.7M17.4 6.6l1.7-1.7"/>'),
    prev: '<svg viewBox="0 0 24 24"><path d="M6 5h2.4v14H6zM20 5v14L9.5 12z"/></svg>', next: '<svg viewBox="0 0 24 24"><path d="M15.6 5H18v14h-2.4zM4 5l10.5 7L4 19z"/></svg>',
    play: '<svg viewBox="0 0 24 24"><path d="M7 4.5v15l12.5-7.5z"/></svg>', pause: '<svg viewBox="0 0 24 24"><path d="M6.5 5h3.8v14H6.5zM13.7 5h3.8v14h-3.8z"/></svg>',
  };
  // 天气图标和主页是同一套（渐变自己带一份，id 用 vg- 开头）
  const VG_DEFS = '<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>'
    + '<radialGradient id="vg-sun" cx="38%" cy="34%" r="65%"><stop offset="0%" stop-color="#fff4b2"/><stop offset="48%" stop-color="#ffd24c"/><stop offset="100%" stop-color="#ff9d1c"/></radialGradient>'
    + '<linearGradient id="vg-ray" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#ffe484"/><stop offset="100%" stop-color="#ffb129"/></linearGradient>'
    + '<linearGradient id="vg-moon" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#fff4c2"/><stop offset="100%" stop-color="#f5c65d"/></linearGradient>'
    + '<linearGradient id="vg-cloud" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#f5f9ff"/><stop offset="58%" stop-color="#d8e4f6"/><stop offset="100%" stop-color="#b8cbe6"/></linearGradient>'
    + '<linearGradient id="vg-cloud-back" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#cad8ee"/><stop offset="100%" stop-color="#92a7c8"/></linearGradient>'
    + '<linearGradient id="vg-cloud-dark" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#d0ddf1"/><stop offset="100%" stop-color="#8b9ebe"/></linearGradient>'
    + '<linearGradient id="vg-rain" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#8cd6ff"/><stop offset="100%" stop-color="#3ca3ff"/></linearGradient>'
    + '<linearGradient id="vg-bolt" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#fff19e"/><stop offset="55%" stop-color="#ffd03b"/><stop offset="100%" stop-color="#ff9914"/></linearGradient></defs></svg>';
  const WX = {
    sun: '<circle cx="32" cy="32" r="17.5" fill="#ffc94d" fill-opacity=".16"/><circle cx="32" cy="32" r="12.5" fill="url(#vg-sun)"/><g stroke="url(#vg-ray)" stroke-width="4.2" stroke-linecap="round"><path d="M32 6.5v6.5M32 51v6.5M6.5 32H13M51 32h6.5M14 14l4.6 4.6M45.4 45.4 50 50M14 50l4.6-4.6M45.4 18.6 50 14"/></g>',
    moon: '<circle cx="34" cy="32" r="24" fill="#ffe7a3" fill-opacity=".1"/><path d="M41.5 6a26.2 26.2 0 1 0 16.6 39A22 22 0 0 1 41.5 6z" fill="url(#vg-moon)"/>',
    cloud: '<path d="M25 39h19a8.5 8.5 0 0 0 .6-17 11.5 11.5 0 0 0-22 3.2A7 7 0 0 0 25 39z" transform="translate(6 -6)" fill="url(#vg-cloud-back)" opacity=".72"/><path d="M18 50h28a11.5 11.5 0 0 0 1-22.9 15.5 15.5 0 0 0-29.8 4.2A9.5 9.5 0 0 0 18 50z" fill="url(#vg-cloud)"/>',
    partly: '<circle cx="23" cy="23" r="14" fill="#ffc94d" fill-opacity=".14"/><circle cx="23" cy="23" r="9.8" fill="url(#vg-sun)"/><g stroke="url(#vg-ray)" stroke-width="3.6" stroke-linecap="round"><path d="M23 6.5v4.2M6.5 23h4.2M11.3 11.3l3 3M34.7 11.3l-3 3M11.3 34.7l3-3"/></g><path d="M23 52h24a10.5 10.5 0 0 0 .9-20.9 13.8 13.8 0 0 0-26.6 4A8.5 8.5 0 0 0 23 52z" fill="url(#vg-cloud)"/>',
    partlyN: '<path d="M30 9a13.2 13.2 0 1 0 8.4 19.4A11.2 11.2 0 0 1 30 9z" fill="url(#vg-moon)"/><path d="M23 52h24a10.5 10.5 0 0 0 .9-20.9 13.8 13.8 0 0 0-26.6 4A8.5 8.5 0 0 0 23 52z" fill="url(#vg-cloud)"/>',
    rain: '<path d="M19 40h27a11 11 0 0 0 1-22 15 15 0 0 0-29 4 9 9 0 0 0 1 18z" fill="url(#vg-cloud-dark)"/><path d="M23 46l-3.2 9M33.5 46l-3.2 9M44 46l-3.2 9" stroke="url(#vg-rain)" stroke-width="4" stroke-linecap="round"/>',
    thunder: '<path d="M19 39h27a11 11 0 0 0 1-22 15 15 0 0 0-29 4 9 9 0 0 0 1 18z" fill="url(#vg-cloud-dark)"/><path d="M34 39l-7.5 11.5h7.2l-3.2 10.5 11-14.5h-7.2L38 39z" fill="url(#vg-bolt)"/>',
    snow: '<path d="M19 40h27a11 11 0 0 0 1-22 15 15 0 0 0-29 4 9 9 0 0 0 1 18z" fill="url(#vg-cloud)"/><g class="fl"><circle cx="23" cy="49.5" r="3.1"/><circle cx="33" cy="55" r="3.1"/><circle cx="43" cy="49.5" r="3.1"/></g>',
    fog: '<path class="fg" d="M13 22h38M9 31h46M13 40h38M19 49h26" stroke-width="4.4" stroke-linecap="round"/>',
    dust: '<path d="M10 24h30a5.8 5.8 0 1 0-5.8-5.8M10 34h40a5.8 5.8 0 1 1-5.8 5.8M10 44h23" stroke="#e0a44f" stroke-width="4.3" stroke-linecap="round" fill="none"/>',
  };
  const wxKind = code => { const c = parseInt(code, 10) || 999; return c === 100 || c === 900 ? 'sun' : c === 150 ? 'moon' : c >= 101 && c <= 103 ? 'partly' : c >= 151 && c <= 153 ? 'partlyN' : c >= 302 && c <= 304 ? 'thunder' : c >= 300 && c < 400 ? 'rain' : c >= 400 && c < 500 ? 'snow' : [503, 504, 507, 508].includes(c) ? 'dust' : c >= 500 && c < 600 ? 'fog' : 'cloud'; };
  const wxIcon = code => `<svg viewBox="0 0 64 64">${WX[wxKind(code)]}</svg>`;

  function parseToolArgs(tool) {
    const raw = tool && (tool.args !== undefined ? tool.args : tool.arguments);
    if (!raw) return {};
    if (typeof raw === 'object') return raw;
    if (typeof raw === 'string') { try { return JSON.parse(raw) || {}; } catch (e) { return {}; } }
    return {};
  }
  function isNavOnlyTool(tool) {
    const tn = (tool && tool.name) || '';
    if (!tn) return false;
    if (tn === 'self.screen.navigate' || tn === 'self_screen_navigate') return true;
    const args = parseToolArgs(tool);
    const act = String(args.action || '').trim().toLowerCase();
    if ((tn === 'self.music.control' || tn === 'self_music_control') && ['close_ui', 'close', 'home', 'back', 'exit', '回主页', '主界面', 'open_ui', 'open_radio', '打开电台', 'open_fav', '打开我喜欢', 'open_account'].includes(act)) return true;
    if ((tn === 'self.home.countdown' || tn === 'self_home_countdown') && act === 'close') return true;
    return false;
  }
  function detectRichSpec(q, a, tool) {
    const tn = (tool && tool.name) || '';
    if (tool && tool.error) return null;
    if (isNavOnlyTool(tool)) return null;
    if (tn.startsWith('self.music.') || tn === 'play_music') return { kind: 'music', tool };
    if (tn === 'get_weather') return { kind: 'weather', tool, city: extractCity(q, a) };
    if (tn === 'self.alarm.set_volume' || tn === 'self.alarm.get_volume') return { kind: 'volume', alarm: true, tool };
    // 说「倒计时 5 分钟」会转到主页的计时器（mcp.py），卡片也跟着它
    if (tn === 'self.alarm.create' && /"routed":\s*"self\.home\.timer"/.test((tool && tool.result) || '')) return { kind: 'timer', tool };
    if (tn.startsWith('self.alarm.')) return { kind: 'schedule', tool };
    if (tn === 'self.home.timer' || tn === 'self.study.focus') return { kind: 'timer', tool };
    if (tn === 'self.home.countdown') return { kind: 'countdown', tool };
    if (tn === 'self.home.message' || tn === 'self.home.reminder') return { kind: 'message', tool };
    if (tn === 'self.server.status') return { kind: 'server', tool };
    if (tn === 'self.tutor.explain') return { kind: 'tutor', tool };
    if (tn.startsWith('self.audio_speaker.')) return { kind: 'volume', tool };
    if (tn === 'self.screen.set_brightness' || tn === 'self.screen.get_brightness' || tn === 'self_screen_set_brightness' || tn === 'self_screen_get_brightness') return { kind: 'brightness', tool };
    if (tn.startsWith('get_news')) return { kind: 'news', tool };
    if (a && /(天气|气温|下雨|下雪|空气质量)/.test(q || '')) return { kind: 'weather', city: extractCity(q, a) };
    return null;
  }
  const CITY_RE = /(呼和浩特|包头|鄂尔多斯|临河|巴彦淖尔|乌兰察布|赤峰|通辽|呼伦贝尔|乌海|阿拉善|锡林浩特|北京|天津|上海|重庆|石家庄|唐山|秦皇岛|保定|张家口|承德|太原|大同|沈阳|大连|长春|吉林|哈尔滨|齐齐哈尔|南京|苏州|无锡|常州|扬州|徐州|杭州|宁波|温州|绍兴|合肥|黄山|福州|厦门|泉州|南昌|景德镇|济南|青岛|烟台|威海|郑州|洛阳|开封|武汉|宜昌|长沙|张家界|广州|深圳|珠海|佛山|东莞|南宁|桂林|北海|海口|三亚|成都|九寨沟|贵阳|昆明|大理|丽江|西双版纳|拉萨|西安|延安|兰州|敦煌|西宁|银川|乌鲁木齐|喀什|香港|澳门|台北)/;
  function extractCity(q, a) {
    const m = CITY_RE.exec(q || '') || CITY_RE.exec(a || '');
    return m ? m[1] : '';
  }
  // 问的是哪一天：天气卡里把那一天标出来
  const askedDay = (q, daily) => {
    const t = q || '';
    if (/大后天/.test(t)) return 3;
    if (/后天/.test(t)) return 2;
    if (/明[天早晚日]/.test(t)) return 1;
    if (/今[天早晚日]|现在/.test(t)) return 0;
    const m = /(?:周|星期|礼拜)([一二三四五六日天])/.exec(t);
    if (!m) return -1;
    const want = '日一二三四五六'.indexOf(m[1] === '天' ? '日' : m[1]);
    return (daily || []).findIndex((d, i) => { const p = String(d.date || '').split('-').map(Number); return p.length === 3 && new Date(p[0], p[1] - 1, p[2]).getDay() === want; });
  };
  const newsItems = a => {   // 「1，……2，……3，……」
    const out = [], re = /(?:^|[。！？\s；;])\s*(\d)[，,、.．]\s*/g, txt = a || '';
    let m, at = [], k = 1;
    while ((m = re.exec(txt))) if (+m[1] === k) { at.push([m.index + m[0].length, m.index + (/^\d/.test(m[0]) ? 0 : 1)]); k++; }
    for (let i = 0; i < at.length; i++) { const t = txt.slice(at[i][0], i + 1 < at.length ? at[i + 1][1] : undefined).trim().replace(/[。；;]\s*$/, ''); if (t) out.push(t); }
    return { intro: at.length ? txt.slice(0, at[0][1]).trim() : txt, items: out.slice(0, 6) };
  };
  const WK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
  const dayName = (iso, i) => { if (i === 0) return '今天'; if (i === 1) return '明天'; const p = String(iso || '').split('-').map(Number); return p.length === 3 ? WK[new Date(p[0], p[1] - 1, p[2]).getDay()] : ''; };
  const secTxt = v => { const s = Math.max(0, Math.round(v)), h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), p = n => String(n).padStart(2, '0'); return h ? `${h}:${p(m)}:${p(s % 60)}` : `${p(m)}:${p(s % 60)}`; };
  const leftTxt = at => secTxt(at - Date.now() / 1000);
  const inTxt = at => { const m = Math.max(0, Math.round((at - Date.now() / 1000) / 60)); return m < 1 ? '马上响' : m < 60 ? `${m} 分钟后响` : m < 1440 ? `${Math.round(m / 60)} 小时后响` : `${Math.round(m / 1440)} 天后响`; };
  const repTxt = r => String(r || '').replace(/（.*?）/g, '');
  const atTxt = at => { const d = new Date(at * 1000); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); };
  const dayTxt = at => { const d = new Date(at * 1000), t = new Date(); t.setHours(0, 0, 0, 0); const n = Math.round((new Date(d.getFullYear(), d.getMonth(), d.getDate()) - t) / 86400000); return n === 0 ? '今天' : n === 1 ? '明天' : n === 2 ? '后天' : `${d.getMonth() + 1}月${d.getDate()}日`; };
  const spd = v => { v = Number(v) || 0; return v >= 1048576 ? (v / 1048576).toFixed(1) + ' MB/s' : Math.round(v / 1024) + ' KB/s'; };
  const tb = v => v >= 1e12 ? (v / 1e12).toFixed(2) + ' TB' : Math.round(v / 1e9) + ' GB';
  const rBtn = (act, text, extra = '') => `<button type="button" class="rb" data-rc-act="${act}"${extra}>${text}</button>`;
  const head = (ico, tint, title, sub, acts = '') => `<div class="rh"><span class="ri ${tint}">${ico}</span><div class="rt"><b>${escHtml(title)}</b>${sub ? `<span>${escHtml(sub)}</span>` : ''}</div>${acts ? `<div class="ra">${acts}</div>` : ''}</div>`;
  const slider = (id, ico, v) => `<div class="bar"><input type="range" class="range" data-rc-range="${id}" min="0" max="100" value="${v}" data-pad="14" aria-label="调节">${ico}<span class="bv">${v}%</span></div>`;

  function buildRichHtml(spec, ctx, q, a) {
    if (!spec) return '';
    const k = spec.kind, home = (ctx && ctx.home) || {}, dev = (ctx && ctx.device) || {};
    if (k === 'weather') {
      const w = ctx && ctx.weather;
      if (!w || !w.now || w.now.temp == null) return '';
      const n = w.now, sub = [n.feels != null && n.feels !== '' ? `体感 ${n.feels}°` : '', n.wind_dir ? `${n.wind_dir}${n.wind_scale ? ' ' + n.wind_scale + ' 级' : ''}` : '', n.humidity ? `湿度 ${n.humidity}%` : ''].filter(Boolean).join(' · ');
      const aqt = w.air && w.air.aqi ? `空气${escHtml(w.air.category || '')} ${escHtml(w.air.aqi)}` : '';
      const aq = aqt ? `<span class="tag ${w.air.category === '优' ? 'ok' : w.air.category === '良' ? 'mid' : 'bad'}">${aqt}</span>` : '';
      const hot = askedDay(q, w.daily);
      const days = (w.daily || []).slice(0, 5).map((d, i) => `<div${i === hot ? ' class="on"' : ''}><small>${escHtml(dayName(d.date, i))}</small>${wxIcon(d.icon)}<b>${escHtml(d.max)}°<i> ${escHtml(d.min)}°</i></b></div>`).join('');
      return `<div class="rc"><div class="rh"><span class="wx-i">${wxIcon(n.icon)}</span><span class="wx-t">${escHtml(n.temp)}°</span><div class="rt"><b>${escHtml(w.city || '')} ${escHtml(n.text || '')}</b><span>${escHtml(sub)}${aqt ? `<i class="aqs">${sub ? ' · ' : ''}${aqt}</i>` : ''}</span></div><div class="ra">${aq}${rBtn('open-weather', '详情', ` data-rc-city="${escHtml(w.city || '')}"`)}</div></div>`
        + (w.alert ? `<div class="wx-x"><span class="tag bad">${escHtml(w.alert)}</span></div>` : '') + (days ? `<div class="wx-d">${days}</div>` : '') + '</div>';
    }
    if (k === 'music') {
      const m = home.music || {}, t = m.track || {}, amb = m.ambient, playing = !!((amb && !m.paused) || (m.playing && !m.paused));
      const title = amb ? (amb.primary_name || amb.name || '自然声景') : t.title;
      if (!title) return `<div class="rc">${head(RC_ICONS.music, 'mu', '音乐和电台', '', rBtn('open-music', '打开'))}</div>`;
      const sub = amb ? (amb.layer_name ? `叠加 ${amb.layer_name}` : '自然声景') : t.missing ? 'U盘已拔出' : t.artist || '';
      const pct = !amb && m.duration > 0 && !t.is_live ? Math.max(0, Math.min(1, (m.elapsed || 0) / m.duration)) : null;
      return `<div class="rc mus"><div class="rh"><button type="button" class="cv" data-rc-act="open-music" aria-label="打开音乐">${!amb && okImg(t.cover) ? `<img src="${escHtml(t.cover)}" alt="">` : RC_ICONS.music}</button><div class="rt"><b>${escHtml(title)}</b><span>${escHtml(sub)}</span>${pct != null ? `<div class="pb"><i style="transform:scaleX(${pct.toFixed(3)})"></i></div>` : ''}</div>`
        + `<div class="ra"><button type="button" class="rk" data-rc-act="music-prev" aria-label="上一首">${RC_ICONS.prev}</button><button type="button" class="rk go" data-rc-act="${playing ? 'music-pause' : 'music-resume'}" aria-label="${playing ? '暂停' : '播放'}">${playing ? RC_ICONS.pause : RC_ICONS.play}</button><button type="button" class="rk" data-rc-act="music-next" aria-label="下一首">${RC_ICONS.next}</button></div></div></div>`;
    }
    if (k === 'schedule') {
      const list = (dev.alarms || []).filter(x => x && x.at).slice(0, 4), f = list[0];
      if (!f) return `<div class="rc">${head(RC_ICONS.alarm, 'fo', '没有闹钟和提醒', '', rBtn('open-alarms', '闹钟'))}</div>`;
      const kindTxt = { timer: '倒计时', alarm: '闹钟', reminder: '提醒' }[f.kind] || '闹钟';
      const big = f.kind === 'timer' ? `<span data-rc-left="${f.at}">${leftTxt(f.at)}</span>` : atTxt(f.at);
      const sub = f.kind === 'timer' ? `${atTxt(f.at)} 响` : [dayTxt(f.at), repTxt(f.repeat), inTxt(f.at)].filter(Boolean).join(' · ');
      return `<div class="rc sch"><div class="rh"><span class="ri fo">${f.kind === 'timer' ? RC_ICONS.timer : RC_ICONS.alarm}</span><span class="big">${big}</span><div class="rt"><b>${escHtml(f.label || kindTxt)}</b><span>${escHtml(sub)}</span></div><div class="ra">${rBtn('alarm-del', '删除', ` data-rc-id="${f.id}"`)}${rBtn('open-alarms', '全部')}</div></div>`
        + (list.length > 1 ? `<div class="lst">${list.slice(1).map(x => `<div><b>${x.kind === 'timer' ? `<span data-rc-left="${x.at}">${leftTxt(x.at)}</span>` : atTxt(x.at)}</b><span>${escHtml(x.label || { timer: '倒计时', alarm: '闹钟', reminder: '提醒' }[x.kind] || '')}</span><em>${escHtml(x.kind === 'timer' ? atTxt(x.at) + ' 响' : dayTxt(x.at))}</em></div>`).join('')}</div>` : '') + '</div>';
    }
    if (k === 'timer') {
      const f = home.study && home.study.focus, t = home.timer, focus = !!(f && f.phase), on = focus ? f : t && t.phase ? t : null;
      if (!on) return `<div class="rc">${head(RC_ICONS.timer, 'fo', '计时和专注', '现在没有在计时', rBtn('open-timer', '打开'))}</div>`;
      const run = focus || on.phase === 'running', up = !focus && on.mode === 'stopwatch', now = Date.now() / 1000, sec = Math.max(0, Number(up ? on.elapsed : on.remaining) || 0);
      const big = on.phase === 'done' ? '' : up ? `<span class="big"${run ? ` data-rc-up="${Math.round(now - sec)}"` : ''}>${secTxt(sec)}</span>` : `<span class="big"${run ? ` data-rc-left="${Math.round(now + sec)}"` : ''}>${secTxt(sec)}</span>`;
      const label = focus ? (on.phase === 'break' ? '休息一下' : '专注中') : on.label || (up ? '正向计时' : '倒计时');
      const sub = on.phase === 'done' ? '时间到' : on.phase === 'paused' ? '已暂停' : focus ? (on.phase === 'break' ? '休息完继续' : '到点提醒休息') : up ? '正在计时' : `${atTxt(now + sec)} 响`;
      return `<div class="rc"><div class="rh"><span class="ri fo">${RC_ICONS.timer}</span>${big}<div class="rt"><b>${escHtml(label)}</b><span>${sub}</span></div><div class="ra">${rBtn('open-timer', '打开')}</div></div></div>`;
    }
    if (k === 'countdown') {
      const list = ((ctx && ctx.countdowns) || []).slice(0, 4);
      if (!list.length) return `<div class="rc">${head(RC_ICONS.cd, 'fo', '倒数日', '还没有记下日子', rBtn('open-countdown', '打开'))}</div>`;
      const days = c => Number(c.days) || 0, rel = c => (c.kind === 'countup' ? `已经 ${days(c)} 天` : days(c) === 0 ? '就是今天' : days(c) > 0 ? `还有 ${days(c)} 天` : `过去 ${-days(c)} 天`);
      const c = list[0], d = days(c), md = String(c.target_date || '').split('-').map(Number);
      const sub = [rel(c), md.length === 3 ? `${md[1]}月${md[2]}日 ${c.weekday || ''}`.trim() : '', c.anniv_label || ''].filter(Boolean).join(' · ');
      return `<div class="rc"><div class="rh"><span class="ri fo">${RC_ICONS.cd}</span>${d !== 0 ? `<span class="big">${Math.abs(d)}</span>` : ''}<div class="rt"><b>${escHtml(c.title || '倒数日')}</b><span>${escHtml(sub)}</span></div><div class="ra">${rBtn('open-countdown', '打开')}</div></div>`
        + (list.length > 1 ? `<div class="lst">${list.slice(1).map(x => `<div><span>${escHtml(x.title || '')}</span><em>${rel(x)}</em></div>`).join('')}</div>` : '') + '</div>';
    }
    if (k === 'message') {
      const r = ((home.messages && home.messages.recent) || [])[0], rem = spec.tool && spec.tool.name === 'self.home.reminder';
      const title = r ? (r.kind === 'voice' && r.transcript ? r.transcript : r.text) : rem ? '提醒' : '留言板';
      const sub = r ? [r.who || r.from || '', r.due ? dayTxt(r.due) + ' ' + atTxt(r.due) + ' 提醒' : r.at ? atTxt(r.at) : ''].filter(Boolean).join(' · ') : String((spec.tool && spec.tool.result) || '').slice(0, 60);
      return `<div class="rc">${head(RC_ICONS.msg, 'ms', title, sub, rBtn('open-messages', '留言板'))}</div>`;
    }
    if (k === 'news') {
      const n = newsItems(a);
      if (n.items.length < 2) return '';
      return `<div class="rc nw"><div class="lst">${n.items.map((t, i) => `<button type="button" data-rc-act="say" data-rc-say="详细说说第${i + 1}条"><i>${i + 1}</i><span>${escHtml(t)}</span></button>`).join('')}</div></div>`;
    }
    if (k === 'server') {
      const s = ctx && ctx.servers, b = (ctx && ctx.server) || {}, nodes = (s && s.nodes) || [], up = nodes.filter(x => x.alive).length;
      if (!nodes.length) return `<div class="rc">${head(RC_ICONS.server, 'sv', '服务器', b.text || '', rBtn('open-server', '打开'))}</div>`;
      const tr = s.traffic && s.traffic.limit ? `本月流量 ${tb(s.traffic.used)} / ${tb(s.traffic.limit)}` : '';
      const car = (s.carriers || []).filter(c => c.ms != null).map(c => `${c.name} ${Math.round(c.ms)}`).join(' · ');
      return `<div class="rc"><div class="rh"><span class="ri sv">${RC_ICONS.server}</span><div class="rt"><b>${up === nodes.length ? `${up} 台全部在线` : `${up} / ${nodes.length} 台在线`}</b><span>${escHtml([tr, car ? car + ' ms' : ''].filter(Boolean).join('，'))}</span></div><div class="ra">${rBtn('open-server', '打开')}</div></div><div class="nodes">${nodes.slice(0, 8).map(x => `<div class="${x.alive ? '' : 'off'}"><i></i><span>${escHtml(x.name)}</span></div>`).join('')}</div></div>`;
    }
    if (k === 'tutor') return `<div class="rc">${head(RC_ICONS.tutor, 'vi', 'AI 讲题', '在黑板上一步一步讲', rBtn('open-tutor', '打开黑板'))}</div>`;
    if (k === 'volume') return `<div class="rc one">${slider(spec.alarm ? 'alarmvol' : 'vol', spec.alarm ? RC_ICONS.alarm : RC_ICONS.vol, Number((spec.alarm ? dev.alarm_volume : dev.volume) ?? 50))}${rBtn(spec.alarm ? 'open-alarms' : 'open-set-sound', spec.alarm ? '闹钟' : '更多')}</div>`;
    if (k === 'brightness') return `<div class="rc one">${slider('bright', RC_ICONS.sun, Number((dev.brightness && dev.brightness.level) ?? 50))}${rBtn('open-set-display', '更多')}</div>`;
    return '';
  }
  const CHIPS = { weather: ['后天呢？', '这周末呢？', '要穿什么？'], music: ['换一首', '声音小一点'], news: ['再来几条'], server: ['哪台延迟最低？'] };
  const NEEDS = { weather: 1, music: 1, schedule: 1, timer: 1, countdown: 1, message: 1, server: 1, volume: 1, brightness: 1 };
  const ctxKind = k => (k === 'volume' || k === 'brightness' || k === 'schedule' ? 'device' : k);

  async function handleRichAction(btn, refresh) {
    const act = btn.dataset.rcAct;
    if (!act) return;
    const post = (u, b) => fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b || {}) }).then(r => r.json()).catch(() => ({ ok: false }));
    if (act.startsWith('music-')) {
      const j = await post('/api/rich/music', { action: act.slice(6) });
      if (j && j.ok === false && typeof window.toast === 'function') window.toast((j.music && j.music.message) || '没控制成，打开音乐看看', 'error');
      if (refresh) { setTimeout(refresh, 120); setTimeout(refresh, 900); }
      return;
    }
    if (act === 'alarm-del') {
      if (!btn.classList.contains('arm')) { btn.classList.add('arm'); btn.textContent = '确认删除'; setTimeout(() => { if (btn.isConnected) { btn.classList.remove('arm'); btn.textContent = '删除'; } }, 4000); return; }
      await post('/api/alarms/delete', { id: Number(btn.dataset.rcId) });
      if (refresh) refresh();
      return;
    }
    if (act === 'say') { vcClosed = false; post('/api/text', { text: btn.dataset.rcSay || btn.textContent }); return; }
    vcClosed = true; vcShow(false);
    if (act === 'open-music') openSub('http://127.0.0.1:8091/');
    else if (act === 'open-server') openSub('http://127.0.0.1:8092/');
    else if (act === 'open-tutor') openSub('http://127.0.0.1:8096/');
    else if (act === 'open-settings') openUnifiedSettings(true);
    else if (act.startsWith('open-set-')) openUnifiedSettings(act.slice(9));
    else if (act === 'open-alarms') window.__tsOpenHomeAlarms();
    else if (act === 'open-weather' || act === 'open-timer' || act === 'open-countdown' || act === 'open-messages') {
      closeBrightness(false); forceCloseSheets();
      if (subOpen) closeSub(true); else showHome();
      const msg = act === 'open-weather' ? { ts: 'home', openWeather: true, city: btn.dataset.rcCity || '' }
        : act === 'open-timer' ? { ts: 'home', openTimer: true } : act === 'open-messages' ? { ts: 'home', openMessages: true } : { ts: 'home', openCountdown: true };
      const send = () => { try { frame.contentWindow?.postMessage(msg, HOME); } catch (e) {} };
      send(); if (!homeOk) setTimeout(send, 140);
    }
  }
  // 卡片里的大滑条（音量 / 亮度）：和设置页同一套算法（竖屏也对），手指松开就解绑
  function bindRanges(el) {
    if (typeof window.sliderValueFromEvent !== 'function') return;
    for (const r of el.querySelectorAll('input[data-rc-range]')) {
      const num = r.parentElement.querySelector('.bv'), which = r.dataset.rcRange;
      const url = which === 'bright' ? '/api/display' : which === 'alarmvol' ? '/api/alarm-volume' : '/api/volume';
      const send = (u, v) => fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(which === 'bright' ? { level: v } : { volume: v }) }).catch(() => {});
      let id = null, last = 0, lastVal = Number(r.value) || 0;
      const usable = () => {
        if (!r.isConnected || r.disabled) return false;
        const rect = r.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      };
      const show = v => { lastVal = v; r.value = v; window.setBarPos(r, v); if (num) num.textContent = v + '%'; };
      const detach = () => {
        removeEventListener('pointermove', move, true);
        removeEventListener('pointerup', end, true);
        removeEventListener('pointercancel', end, true);
        removeEventListener('blur', abort);
        r.removeEventListener('lostpointercapture', end);
      };
      const abort = () => {
        if (id === null) return;
        const pid = id;
        id = null;
        r.__drag = false;
        detach();
        try { r.releasePointerCapture(pid); } catch (err) {}
        if (el.__pendingRich) { const pr = el.__pendingRich; el.__pendingRich = null; fillRich(el, pr[0], pr[1], pr[2], pr[3]); }
      };
      const move = e => {
        if (e.pointerId !== id) return;
        if (!usable()) { abort(); return; }
        if (e.cancelable) e.preventDefault();
        const v = window.sliderValueFromEvent(r, e);
        show(v); vcTouch();
        if (Date.now() - last > 160) { last = Date.now(); send(which === 'bright' ? '/api/display/preview' : url, v); }
      };
      const end = e => {
        if (e.pointerId !== id) return;
        id = null;
        r.__drag = false;
        detach();
        try { r.releasePointerCapture(e.pointerId); } catch (err) {}
        if (!r.isConnected) return;
        const v = (e.type === 'pointercancel' || e.type === 'lostpointercapture' || !usable()) ? lastVal : window.sliderValueFromEvent(r, e);
        show(v); send(url, v);
        if (el.__pendingRich) { const pr = el.__pendingRich; el.__pendingRich = null; fillRich(el, pr[0], pr[1], pr[2], pr[3]); }
      };
      show(lastVal);
      r.__abort = abort;
      r.addEventListener('mousedown', e => { if (e.button === 0) e.preventDefault(); });
      r.addEventListener('pointerdown', e => {
        if (e.button > 0 || id !== null || !usable()) return;
        if (e.cancelable) e.preventDefault();
        id = e.pointerId;
        r.__drag = true;
        addEventListener('pointermove', move, { capture: true, passive: false });
        addEventListener('pointerup', end, true);
        addEventListener('pointercancel', end, true);
        addEventListener('blur', abort);
        r.addEventListener('lostpointercapture', end);
        try { r.setPointerCapture(e.pointerId); } catch (err) {}
        move(e);
      });
    }
  }
  function fillRich(el, spec, ctx, q, a) {
    const html = buildRichHtml(spec, ctx, q, a);
    if (el.__rc === html) { el.__pendingRich = null; return; }
    const activeR = spec ? el.querySelector('input[data-rc-range]') : null;
    if (activeR && activeR.__drag && el.__rcKind === spec.kind) {
      el.__pendingRich = [spec, ctx, q, a];
      return;
    }
    el.__pendingRich = null;
    for (const oldR of el.querySelectorAll('input[data-rc-range]')) {
      if (typeof oldR.__abort === 'function') oldR.__abort();
    }
    if (!vgDone) { vgDone = true; document.body.insertAdjacentHTML('beforeend', VG_DEFS); }
    el.__rc = html;
    el.__rcKind = spec ? spec.kind : '';
    el.innerHTML = html;
    for (const im of el.querySelectorAll('.cv img')) im.onerror = () => { const b = im.parentElement; if (b) b.innerHTML = RC_ICONS.music; };   // 封面取不到就用图标
    bindRanges(el);
  }

  async function refreshRichCard(el, spec, q, a, force = false) {
    if (!el) return;
    if (!spec) { vcSpec = null; vcCtx = null; vcRichSig = ''; vcCtxSig = ''; fillRich(el, null); return; }
    const sig = `${spec.kind}:${spec.city || ''}:${spec.alarm ? 1 : 0}:${(spec.tool && spec.tool.name) || ''}:${(spec.tool && spec.tool.time) || ''}`;
    const fresh = sig !== vcRichSig;
    vcRichSig = sig; vcSpec = spec;
    if (!NEEDS[spec.kind]) { fillRich(el, spec, null, q, a); return; }
    if (!fresh && !force) { if (vcCtxSig === sig) fillRich(el, spec, vcCtx, q, a); return; }   // 回答一句一句长出来时不重复去读
    if (fresh) { vcCtx = null; vcCtxSig = ''; }
    const seq = ++vcRichFetchSeq;
    const read = async () => {
      const qs = new URLSearchParams({ kind: ctxKind(spec.kind) });
      if (spec.city) qs.set('city', spec.city);
      try { const r = await fetch('/api/rich/context?' + qs.toString(), { cache: 'no-store' }); return r.ok ? await r.json() : null; } catch (e) { return null; }
    };
    const ctx = await read();
    if (seq !== vcRichFetchSeq || vcRichSig !== sig) return;
    if (ctx) { vcCtx = ctx; vcCtxSig = sig; }
    fillRich(el, spec, vcCtx, q, a);
    syncVoiceCard();
    if (fresh && (spec.kind === 'music' || spec.kind === 'timer' || spec.kind === 'schedule' || spec.kind === 'message'))   // 动作做完要一会儿：再读两次
      for (const ms of [1300, 3800]) setTimeout(() => { if (vcRichSig === sig) refreshRichCard(el, vcSpec, vcQEl.textContent, vcA(), true); }, ms);
  }
  const vcA = () => $('tsVcA')?.textContent || '';

  // 「设置 → 对话」里：只给这次真调用过工具的回答配卡片（以前的对话不配，免得拿现在的状态配旧话）
  window.__tsHasCard = t => !!(t && detectRichSpec('', '', t));
  const askedBefore = m => { try { const l = ui.thread.messages, i = l.findIndex(x => x.id === m.id); for (let k = (i < 0 ? l.length : i) - 1; k >= 0; k--) if (l[k].role === 'user') return l[k].text || ''; } catch (e) {} return ''; };
  function decorate(rcEl, spec, q, text, sig) {
    rcEl.__sig = sig; rcEl.__spec = spec; rcEl.__q = q; rcEl.__text = text;
    if (!NEEDS[spec.kind]) { rcEl.__ctx = null; fillRich(rcEl, spec, null, rcEl.__q || '', text); return; }
    const qs = new URLSearchParams({ kind: ctxKind(spec.kind) });
    if (spec.city) qs.set('city', spec.city);
    fetch('/api/rich/context?' + qs.toString(), { cache: 'no-store' }).then(r => (r.ok ? r.json() : null))
      .then(ctx => {
        if (rcEl.__sig !== sig) return;
        const end = xzBox && xzBox.scrollHeight - xzBox.scrollTop - xzBox.clientHeight < 80;
        rcEl.__ctx = ctx; fillRich(rcEl, spec, ctx, rcEl.__q || '', rcEl.__text);
        if (end) xzBox.scrollTop = xzBox.scrollHeight;   // 卡片晚一步长出来，别留在屏幕外面
      }).catch(() => {});
  }
  window.__tsDecorateBotMsg = (rcEl, m) => {
    const spec = m && m._tool ? detectRichSpec('', '', m._tool) : null;
    if (!spec) { if (rcEl.innerHTML) fillRich(rcEl, null); rcEl.__rc = ''; rcEl.__sig = ''; return; }
    const q = askedBefore(m);
    if (spec.kind === 'weather') spec.city = extractCity(q, m.text);
    const sig = `${m.id}:${m._tool.time}:${spec.city || ''}`;
    if (rcEl.__sig !== sig) { decorate(rcEl, spec, q, m.text, sig); return; }
    rcEl.__q = q;
    rcEl.__text = m.text;
    if (rcEl.__ctx !== undefined) fillRich(rcEl, spec, rcEl.__ctx, rcEl.__q || '', m.text);
  };
  // 卡片里的倒计时、正计时每秒走字：胶囊开着时由 vcShow 走，「设置 → 对话」开着时在这里走
  function tickCards(root) {
    for (const el of root.querySelectorAll('[data-rc-left],[data-rc-up]')) { const t = el.dataset.rcUp ? secTxt(Date.now() / 1000 - Number(el.dataset.rcUp)) : leftTxt(Number(el.dataset.rcLeft)); if (el.textContent !== t) el.textContent = t; }
  }
  if (xzBox) setInterval(() => { if (!document.hidden && chatOpen()) tickCards(xzBox); }, 1000);
  document.addEventListener('click', e => {
    const ct = elTarget(e);
    const btn = ct && ct.closest && ct.closest('.msg-rich [data-rc-act]');
    if (!btn) return;
    e.stopPropagation();
    const rcEl = btn.closest('.msg-rich');
    handleRichAction(btn, () => { if (rcEl && rcEl.isConnected && rcEl.__spec) decorate(rcEl, rcEl.__spec, rcEl.__q || '', rcEl.__text, rcEl.__sig); });
  });

  /* 卡片的出现、停留、收起 */
  const vcQEl = $('tsVcQ'), vcAEl = $('tsVcA'), vcREl = $('tsVcRich'), vcSt = $('tsVcState'), vcSrc = $('tsVcSrc'), vcChips = $('tsVcChips');
  function vcShow(on) {
    if (vcShown === on) return;
    vcShown = on;
    vcEl.classList.toggle('show', on);
    if (!on) { vcEl.classList.remove('leaving'); clearTimeout(vcHideT); vcHideT = 0; clearInterval(vcPollT); vcPollT = 0; clearInterval(vcTickT); vcTickT = 0; }
    else {
      clearInterval(vcPollT); vcPollT = setInterval(() => { if (vcSpec && ['music', 'timer'].includes(vcSpec.kind) && !document.hidden) refreshRichCard(vcREl, vcSpec, vcQEl.textContent, vcA(), true); }, 5000);
      clearInterval(vcTickT); vcTickT = setInterval(() => tickCards(vcEl), 1000);
    }
    syncVoicePill();
    syncNotice();
  }
  function vcLeaveIn(ms) {
    clearTimeout(vcHideT);
    vcEl.classList.remove('leaving');
    void vcEl.offsetWidth;
    vcEl.style.setProperty('--left-ms', ms + 'ms');
    vcEl.classList.add('leaving');
    vcHideT = setTimeout(() => { vcHideT = 0; vcShow(false); }, ms);
  }
  function vcTouch() { if (vcHideT) vcLeaveIn(12000); }   // 手指在卡片上：重新计时
  const TOOL_DOING = { web_search: '正在联网搜索', get_weather: '正在查天气', get_news_from_newsnow: '正在看新闻', play_music: '正在找歌', get_lunar: '正在看日历', 'self.server.status': '正在看服务器', 'self.tutor.explain': '正在打开黑板', 'self.screen.navigate': '正在切换页面' };
  vcTool = () => (vcTurnTool && !vcTurnTool.error ? (TOOL_DOING[vcTurnTool.name] || (vcTurnTool.name.startsWith('get_news') ? '正在看新闻' : vcTurnTool.name.startsWith('self.music.') ? '正在准备音乐' : '')) : '');
  // 正开着设置里的「对话」：对话就在眼前，只留胶囊
  const chatOpen = () => !!document.querySelector('#settings.on #settingsPanel > .sec.on[data-sec="chat"]');
  const sheetOpen = () => !!document.querySelector('#settings.on, #wifi.on, #sheet.on');
  let vcLastQText = '';
  addEventListener('ts-settings-section', () => { syncVoiceCard(); });
  function syncVoiceCard() {
    const busy = isBusy(), st = document.body.dataset.state || 'idle';
    const want = !document.body.classList.contains('ringing') && !!xzBox && !chatOpen() && (busy || !sheetOpen());
    if (!want) { vcShow(false); syncVoicePill(); return; }
    let all = [...xzBox.querySelectorAll('.msg:not(.pending)')];
    if (vcSince !== null) {
      const i = all.findIndex(el => el.dataset.id === vcSince);
      if (i >= 0) all = all.slice(i + 1);
      else {
        const msgs = window.__tsUi?.thread?.messages || [];
        const byId = new Map(msgs.map(m => [String(m.id), Number(m.time) || 0]));
        all = all.filter(el => (byId.get(String(el.dataset.id || '')) || 0) > vcSinceAt - 0.5);
      }
    }
    const users = all.filter(el => el.classList.contains('user')), bots = all.filter(el => el.classList.contains('bot'));
    const lastU = users[users.length - 1], lastB = bots[bots.length - 1];
    const bAfterU = lastB && (!lastU || (lastU.compareDocumentPosition(lastB) & Node.DOCUMENT_POSITION_FOLLOWING));
    const q = msgText(lastU), a = bAfterU ? msgText(lastB.querySelector('.body-txt') || lastB.querySelector('.body') || lastB) : '';
    const qid = lastU ? lastU.dataset.id : '';
    if (qid !== vcLastQ) {   // 新的一句话：上一句的工具和卡片不带过来；id 从 123 变成 h456（load_history 重载同句话）时保留工具
      const sameTurn = !!(vcLastQ && qid && !String(vcLastQ).startsWith('h') && String(qid).startsWith('h') && q === vcLastQText);
      vcLastQ = qid;
      vcLastQText = q;
      if (!sameTurn && vcTurnTool) {
        const uMsg = (window.__tsUi?.thread?.messages || []).find(m => String(m.id) === String(qid));
        const uTime = Number(uMsg && uMsg.time) || 0;
        if ((uTime > 0 && (vcTurnTool.time || 0) < uTime - 0.05) || (Date.now() / 1000 - (vcTurnTool.time || 0) > 2)) vcTurnTool = null;
      }
      if (qid && busy) vcClosed = false;
      if (!sameTurn) vcScrollBy = 0;
    } else {
      vcLastQText = q;
    }
    const spec = (q || a || vcTurnTool) ? detectRichSpec(q, a, vcTurnTool) : null;
    const news = spec && spec.kind === 'news' ? newsItems(a) : null;
    const showA = news && news.items.length >= 2 ? news.intro : a;
    if (vcQEl.textContent !== q) vcQEl.textContent = q;
    if (vcAEl.textContent !== showA) {
      vcAEl.textContent = showA;
      vcAEl.classList.toggle('short', showA.length > 0 && showA.length <= 34);
      if (!vcScrollBy) vcAEl.scrollTop = vcAEl.scrollHeight;   // 正在往下说：跟着滚，手指拉过就不跟
    }
    refreshRichCard(vcREl, spec, q, a, false);
    vcEl.dataset.st = st;
    const stTxt = voiceStateText(st) || (modelLabel() && modelLabel() !== '模型' ? `${modelLabel()} 的回答` : '小智的回答'); if (vcSt.textContent !== stTxt) vcSt.textContent = stTxt;
    const src = vcTurnTool ? (vcTurnTool.name === 'web_search' ? '联网搜索' : vcTurnTool.name.startsWith('get_news') ? '新闻' : '') : '';
    if (vcSrc.textContent !== src) vcSrc.textContent = src;
    $('tsVcStop').style.display = busy ? '' : 'none';
    const chips = a && spec && (!busy || st === 'listening') ? CHIPS[spec.kind] || [] : [];
    const chipHtml = chips.map(c => `<button type="button" data-rc-act="say" data-rc-say="${escHtml(c)}">${escHtml(c)}</button>`).join('');
    if (vcChips.__h !== chipHtml) { vcChips.__h = chipHtml; vcChips.innerHTML = chipHtml; }
    const has = !!(q || a || (spec && vcREl.innerHTML)) && !vcClosed;
    if (!has) { vcShow(false); syncVoicePill(); return; }
    if (busy) { clearTimeout(vcHideT); vcHideT = 0; vcEl.classList.remove('leaving'); vcShow(true); }
    else if (vcShown && !vcHideT && st !== 'connecting') vcLeaveIn(spec && vcREl.innerHTML ? 16000 : 11000);
    syncVoicePill();
    syncNotice();
  }
  vcSync = syncVoiceCard;
  vcDismissCard = () => { vcClosed = true; vcShow(false); };
  window.__tsShowVoiceCard = () => { vcClosed = false; syncVoiceCard(); };
  addEventListener('ts-voice-tool', e => {
    const t = e.detail || null;
    if (t && isNavOnlyTool(t)) {
      vcTurnTool = t;
      syncVoicePill();
      return;
    }
    // 一次回答里可能连着调几个工具：读数类的（get_time 之类）不盖掉已经有卡片的
    if (t && vcTurnTool && detectRichSpec('', '', vcTurnTool) && !detectRichSpec('', '', t) && t.name !== 'web_search') return;
    vcTurnTool = t;
    vcClosed = false;
    syncVoiceCard();
  });
  $('tsVcClose').onclick = e => { e.stopPropagation(); vcClosed = true; vcShow(false); };
  $('tsVcExpand').onclick = e => { e.stopPropagation(); openChat(); };
  $('tsVcStop').onclick = e => { e.stopPropagation(); stopVoice(); };
  vcEl.addEventListener('pointerdown', vcTouch, { passive: true });
  vcEl.onclick = e => {
    const vt = elTarget(e);
    const btn = vt && vt.closest && vt.closest('[data-rc-act]');
    if (!btn) return;
    e.stopPropagation();
    handleRichAction(btn, () => refreshRichCard(vcREl, vcSpec, vcQEl.textContent, vcA(), true));
  };
  // 回答太长时手指能拉：竖屏转过的层里系统自带的滚动方向是反的，自己算
  (() => {
    let p = null;
    const pt = e => (typeof window.sheetPoint === 'function' ? window.sheetPoint(e.clientX, e.clientY) : { x: e.clientX, y: e.clientY });
    vcAEl.addEventListener('pointerdown', e => { p = { id: e.pointerId, y: pt(e).y, st: vcAEl.scrollTop, on: false }; });
    addEventListener('pointermove', e => {
      if (!p || e.pointerId !== p.id) return;
      const dy = pt(e).y - p.y;
      if (!p.on && Math.abs(dy) < 8) return;
      p.on = true; vcScrollBy = 1; vcAEl.scrollTop = p.st - dy;
    }, true);
    for (const ev of ['pointerup', 'pointercancel']) addEventListener(ev, () => { p = null; }, true);
    vcAEl.addEventListener('touchmove', e => { if (p && p.on && e.cancelable) e.preventDefault(); }, { passive: false });
  })();
  let vcSyncT = 0;
  const scheduleVoiceCard = () => { if (vcSyncT) return; vcSyncT = setTimeout(() => { vcSyncT = 0; syncVoiceCard(); }, 48); };
  if (xzBox) new MutationObserver(() => scheduleVoiceCard()).observe(xzBox, { childList: true, subtree: true, characterData: true });
  let vcPrevBusy = false;
  new MutationObserver(() => {
    const b = isBusy();
    if (b && !vcPrevBusy && !document.body.classList.contains('ringing') && !vcShown) {   // 新的一次对话：只显示这次的话（卡片还在时接着说，不清空）
      vcClosed = false;
      vcTurnTool = null;
      refreshRichCard(vcREl, null);
      const ms = xzBox ? xzBox.querySelectorAll('.msg:not(.pending)') : [];
      vcSince = ms.length ? (ms[ms.length - 1].dataset.id || null) : null;
      vcSinceAt = Date.now() / 1000;
    }
    vcPrevBusy = b;
    syncVoiceCard();
  }).observe(document.body, { attributes: true, attributeFilter: ['data-state', 'class'] });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      orTopDown = false;
      orTouchOn(false);
      if (orCoverEl.classList.contains('on')) {
        cancelOrRaf();
        clearTimeout(orCoverT); orCoverT = 0;
        orCoverEl.classList.remove('on');
        const latest = orWanted();
        if (latest !== orCur) { orSetClasses(latest); orBroadcast(); orSyncVoiceView(); }
      }
    } else orSchedule(30);
  });
  addEventListener('blur', () => { orTopDown = false; orTouchOn(false); });
  for (const ev of ['pointerdown', 'pointerup', 'pointercancel']) addEventListener(ev, e => { if (e.isPrimary !== false) { orTopDown = ev === 'pointerdown'; orTouchOn(orTopDown); } }, true);
  window.addEventListener('message', e => {
    const d = e.data;
    if (!d || typeof d !== 'object') return;
    if (d.ts === 'touch') {                        // 页面里的手指按下 / 松开（只认自己的页面框）
      let ok = (e.source === frame.contentWindow && e.origin === HOME) || (brightFrame && e.source === brightFrame.contentWindow && e.origin === MON);
      if (!ok) for (const v of subEntries.values()) if (e.source === v.frame.contentWindow && e.origin === v.origin) ok = true;
      if (ok) orTouchOn(!!d.on);
      return;
    }
    const bwin = brightFrame?.contentWindow;
    if (bwin && e.source === bwin) {
      if (e.origin !== MON) return;
      if (d.thinksmart === 'displayReady') orSend(bwin, MON);
      else if (d.thinksmart === 'setOrient') orSetMode(d.mode);
      return;
    }
    for (const v of subEntries.values()) {
      if (v.port === '8096' && e.source === v.frame.contentWindow && e.origin === v.origin) {
        if (d.ts === 'sub' && d.cmd === 'ready') orSend(v.frame.contentWindow, v.origin);
        else if (d.ts === 'sub' && d.cmd === 'orientMode') orSetMode(d.mode);
        return;
      }
    }
  });
  window.__tsOrient = { get: () => ({ mode: orMode, cur: orCur, seen: orSeen, sensor: orSensor, touching: !!orTouching() }), setMode: orSetMode };
  orSetClasses(orWanted());
  orSchedule(500);
  } catch (e) {}

  loadHome();
})();
