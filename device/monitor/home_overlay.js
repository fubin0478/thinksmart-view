// ThinkSmart kiosk overlay (round 109, 2026-11-09). Injected by thinksmart-kiosk.py into every
// top-level page as a WebKit UserScript (this file is ours; XiaoZhi's files stay untouched).
//  * 127.0.0.1:8090 (XiaoZhi):
//    - Lays our home page (thinksmart-home, 127.0.0.1:8095) over XiaoZhi as a persistent frame
//      (#tsHomeLayer, z-index 9000) so returning home is instant with zero XiaoZhi flash.
//    - Opens sub-pages (QQ音乐 :8091, 服务器监控 :8092, etc.) in an upper full-screen frame
//      (#tsSubLayer, z-index 9100) instead of unloading :8090, so 「回桌面」 / 「主页」 reveals the
//      already-warm home page immediately without reloading :8090 or flashing XiaoZhi.
//    - Opens XiaoZhi's 「设置」 / 「WiFi」 sheets (z-index 9200+) directly over #tsHomeLayer when
//      tapped from the home dock, so closing 「设置」 returns straight to the home page.
//    - 「小智」 hides #tsHomeLayer; a wake word / alarm shows XiaoZhi automatically and the home
//      page comes back 30 s after that conversation (3 min after the last touch when opened by hand).
//    - While covered, XiaoZhi's DOM is hidden and its animation frames / CSS animations are parked.
//    - If the home service does not answer, XiaoZhi simply stays usable (retried every 30 s).
//  * Unread phone messages (#tsMsgCard, z-index 9500) sit above home and sub-pages until 「收到」.
//  * 8092 (server monitor): brightness button in the header; when framed inside #tsSubLayer,
//    「主页」 and 「亮度」 message the parent overlay instead of navigating top-level.
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
  const sun = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/></svg>';
  const house = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 11 12 4l8.5 7"/><path d="M5.5 9.5V20h13V9.5"/><path d="M10 20v-5.5h4V20"/></svg>';
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
  #tsSubErr p{margin:0;font-size:22px;font-weight:600;color:#9fb2cd}
  #tsSubErr button{height:54px;padding:0 28px;border:1px solid #8cb4ff40;border-radius:16px;background:#1b2b45;color:#eef4ff;font:700 18px -apple-system,"Noto Sans CJK SC",sans-serif;cursor:pointer;touch-action:manipulation}
  .app{contain:layout style paint;transition:visibility 0s linear 0s}
  html.ts-homeload .app{visibility:hidden!important;pointer-events:none!important;transition:none!important}
  html.ts-xz-covered .app{visibility:hidden!important;pointer-events:none!important;transition:visibility 0s linear .14s}
  html.ts-xz-covered body>*:not(#tsHomeLayer):not(#tsSubLayer):not(#tsMsgCard):not(#tsVoicePill):not(#tsBrightnessPanel):not(#tsBrightnessShade):not(#settings):not(#wifi):not(#toast):not(#hud):not(#powerOff):not(#ring):not(#tsRotCover):not(#tsVoiceCard):not(#tsOrHint):not(.tskb-dock):not(style):not(script){visibility:hidden!important;pointer-events:none!important;transition:visibility 0s linear .14s}
  html.ts-home #settings,html.ts-home #wifi,html.ts-sub #settings,html.ts-sub #wifi{z-index:9200;contain:strict;transform:translate3d(0,0,0);backface-visibility:hidden;transition:opacity .115s cubic-bezier(.4,0,.2,1)!important}
  html.ts-home #wifi,html.ts-sub #wifi{z-index:9205}
  html.ts-home #settings.on,html.ts-home #wifi.on,html.ts-sub #settings.on,html.ts-sub #wifi.on{transition:opacity .14s cubic-bezier(.22,1,.36,1)!important}
  html.ts-sub #settings,html.ts-sub #wifi{transform:none;transition:none!important}
  html.ts-home #settings .sheet-bar,html.ts-home #settings .grid,html.ts-home #settings .panel,html.ts-home #wifi .sheet-bar,html.ts-home #wifi .panel,html.ts-sub #settings .sheet-bar,html.ts-sub #settings .grid,html.ts-sub #settings .panel,html.ts-sub #wifi .sheet-bar,html.ts-sub #wifi .panel{transform:none!important;transition:none!important}
  html.ts-home #toast,html.ts-sub #toast{z-index:9300}
  html.ts-home #hud,html.ts-sub #hud{z-index:9600}
  html.ts-home #powerOff,html.ts-sub #powerOff{z-index:9900}
  html.ts-xz-paused .app,html.ts-xz-paused .app *,html.ts-xz-paused .app *::before,html.ts-xz-paused .app *::after{animation:none!important;transition:none!important;will-change:auto!important}
  #tsHomeBtn,#tsBrightness{height:48px;padding:0 19px;display:flex;align-items:center;gap:9px;border:1px solid #9ec2ff29;border-radius:15px;background:#19253aeb;color:#eaf2ff;font:700 18.5px -apple-system,BlinkMacSystemFont,"Noto Sans CJK SC",sans-serif;-webkit-tap-highlight-color:transparent;touch-action:manipulation;cursor:pointer;flex-shrink:0;box-shadow:0 8px 22px rgba(0,0,0,.28);transition:transform .14s cubic-bezier(.22,1,.36,1),background-color .14s ease}
  #tsHomeBtn{height:56px;padding:0 22px;border-radius:18px;font-size:20px}
  #tsHomeBtn:active,#tsBrightness:active{background:#263d5b;transform:scale(.95);transition:transform .05s ease-out,background-color .05s ease}#tsHomeBtn svg,#tsBrightness svg{width:22px;height:22px;color:#81cefc;flex-shrink:0}#tsHomeBtn svg{width:24px;height:24px}
  #tsHomeBtn.fixed{position:fixed;top:20px;right:24px;z-index:15}
  #tsVoicePill{position:fixed;top:14px;left:50%;z-index:9450;transform:translate3d(-50%,-14px,0);display:flex;align-items:center;gap:10px;height:46px;padding:0 20px;border-radius:999px;border:1px solid #355075;background:#101a2b;color:#eef5ff;font:700 18px/24px -apple-system,BlinkMacSystemFont,"Noto Sans CJK SC",sans-serif;box-shadow:0 6px 16px rgba(0,0,0,.32);opacity:0;visibility:hidden;pointer-events:none;cursor:pointer;touch-action:manipulation;transition:opacity .13s ease,transform .13s cubic-bezier(.22,1,.36,1),visibility 0s linear .13s}
  #tsVoicePill.show{opacity:1;visibility:visible;pointer-events:auto;transform:translate3d(-50%,0,0);transition:opacity .13s ease,transform .13s cubic-bezier(.22,1,.36,1),visibility 0s linear 0s}
  #tsVoicePill i{width:10px;height:10px;border-radius:50%;background:#3ddc97;flex-shrink:0}
  #tsVoicePill[data-st="connecting"] i{background:#5ab8ff}
  #tsVoicePill[data-st="thinking"] i{background:#f5c04a}
  #tsVoicePill[data-st="speaking"] i{background:#7c8cff}
  #tsBrightness.monitor-brightness{height:48px;border-radius:13px;font-size:19.5px;font-weight:700;padding:0 19px;gap:9px;border:1px solid #9ec2ff29;background:#1a2840eb;color:#eaf2ff;box-shadow:none}
  #tsBrightness.monitor-brightness svg{width:22px;height:22px}
  @keyframes tsMsgDrop{from{opacity:0;transform:translate(-50%,-14px)}to{opacity:1;transform:translate(-50%,0)}}
  @keyframes tsAckRing{from{opacity:.85;transform:scale(.96)}to{opacity:0;transform:scale(1.14)}}
  #tsBrightnessPanel{position:fixed;z-index:10001;left:50%;top:50%;transform:translate3d(-50%,calc(-50% + 8px),0);backface-visibility:hidden;width:520px;height:424px;max-width:calc(100% - 32px);max-height:calc(100% - 32px);border:1px solid #9cc2ff42;border-radius:26px;box-shadow:0 10px 28px #0008;background:#142033;overflow:hidden;display:block;opacity:0;visibility:hidden;pointer-events:none;contain:strict;transition:opacity .115s cubic-bezier(.4,0,.2,1),transform .115s cubic-bezier(.4,0,.2,1),visibility 0s linear .115s}
  #tsBrightnessPanel.open{opacity:1;visibility:visible;pointer-events:auto;transform:translate3d(-50%,-50%,0);transition:opacity .145s cubic-bezier(.22,1,.36,1),transform .155s cubic-bezier(.22,1,.36,1),visibility 0s linear 0s}
  #tsBrightnessPanel iframe{position:relative;z-index:1;border:0;width:100%;height:100%;display:block;opacity:0;pointer-events:none;transition:opacity .1s ease}
  #tsBrightnessPanel.ready iframe{opacity:1;pointer-events:auto}
  #tsBrightFallback{position:absolute;inset:0;z-index:2;display:none;flex-direction:column;align-items:center;justify-content:center;gap:18px;color:#b8cee9;font:700 20px -apple-system,"Noto Sans CJK SC",sans-serif}
  #tsBrightnessPanel.open:not(.ready) #tsBrightFallback{display:flex}
  #tsBrightFallback button{height:48px;padding:0 26px;border:1px solid #8cb4ff40;border-radius:14px;background:#1b2b45;color:#eef4ff;font:700 17px -apple-system,"Noto Sans CJK SC",sans-serif;cursor:pointer;touch-action:manipulation}
  #tsBrightnessShade{position:fixed;inset:0;background:rgba(2,6,14,.64);z-index:10000;display:block;opacity:0;visibility:hidden;pointer-events:none;transition:opacity .115s cubic-bezier(.4,0,.2,1),visibility 0s linear .115s}
  #tsBrightnessShade.open{opacity:1;visibility:visible;pointer-events:auto;transition:opacity .135s cubic-bezier(.22,1,.36,1),visibility 0s linear 0s}
  #tsMsgCard{position:fixed;z-index:9500;top:22px;left:50%;transform:translateX(-50%);width:800px;max-width:calc(100% - 40px);display:none;gap:20px;align-items:center;padding:20px 24px 20px 28px;border-radius:24px;background:#1b2436;border:1px solid #ffc85766;box-shadow:0 10px 26px #0008;font-family:-apple-system,BlinkMacSystemFont,"Noto Sans CJK SC",sans-serif;color:#eef3fb;-webkit-user-select:none;user-select:none;contain:layout paint}
  #tsMsgCard.show{display:flex;animation:tsMsgDrop .26s cubic-bezier(.22,1,.36,1) backwards}
  #tsMsgCard .tm-b{flex:1;min-width:0}
  #tsMsgCard .tm-h{font-size:18px;font-weight:700;color:#ffd98a;display:flex;gap:8px;align-items:center}
  #tsMsgCard .tm-tag{display:inline-flex;align-items:center;gap:7px}
  #tsMsgCard .tm-tag svg{width:20px;height:20px;flex-shrink:0}
  #tsMsgCard .tm-t{font-size:28px;font-weight:700;line-height:1.42;margin-top:8px;word-break:break-all;max-height:162px;overflow:hidden;white-space:pre-line}
  #tsMsgCard button{position:relative;height:70px;min-width:132px;padding:0 26px;border:0;border-radius:18px;background:#f5c04a;color:#0a101c;font:800 22px -apple-system,"Noto Sans CJK SC",sans-serif;cursor:pointer;touch-action:manipulation;flex-shrink:0;-webkit-tap-highlight-color:transparent;transition:transform .14s cubic-bezier(.22,1,.36,1)}
  #tsMsgCard.show button::after{display:none}
  #tsMsgCard.settled button::after{animation:none;will-change:auto}
  #tsMsgCard button:active{filter:brightness(.92);transform:scale(.95);transition:transform .05s ease-out}
  #tsMsgCard button:disabled{opacity:.6}
  @media(max-width:620px){#tsBrightnessPanel{width:calc(100% - 24px)}}
  @media(prefers-reduced-motion:reduce){#tsHomeLayer,#tsSubLayer,#tsSubLayer *,#tsMsgCard *,#tsMsgCard *::after,#tsBrightnessPanel,#tsBrightnessShade{animation:none!important;transition:none!important}}
  /* 2026-11-12: brightness panel taller for the 深色 / 浅色 row and in the home page's colours; light versions of the overlay layers */
  #tsBrightnessPanel{height:500px;background:#121a2b;border-color:#232e45}
  html.ts-light #tsHomeLayer,html.ts-light #tsHomeLayer iframe,html.ts-light #tsSubLayer,html.ts-light #tsSubLayer iframe,html.ts-light #tsSubErr{background:#e3dbce}
  html.ts-light #tsSubLoading{background:#e3dbce;color:#424449}
  html.ts-light #tsSubLoading i{background:#0f6d63}
  html.ts-light #tsSubErr{color:#18191b}html.ts-light #tsSubErr p{color:#424449}
  html.ts-light #tsSubErr button,html.ts-light #tsBrightFallback button{background:#f6f2ea;border-color:#d6cdbf;color:#18191b}
  html.ts-light #tsBrightnessPanel{background:#f6f2ea;border-color:#d6cdbf;box-shadow:0 0 0 1px #d6cdbf,0 10px 26px rgba(28,24,18,.20)}
  html.ts-light #tsBrightFallback{color:#46484d}
  html.ts-light #tsBrightnessShade{background:rgba(24,22,19,.42)}
  html.ts-light #tsMsgCard{background:#f6f2ea;border-color:#dab984;color:#18191b;box-shadow:0 0 0 1px #dab984,0 10px 24px rgba(28,24,18,.18)}
  html.ts-light #tsMsgCard .tm-h{color:#84510a}
  html.ts-light #tsMsgCard button{background:#a26008;color:#faf8f4}
  html.ts-light #tsVoicePill{background:#f6f2ea;border-color:#d6cdbf;color:#18191b;box-shadow:0 6px 16px rgba(28,24,18,.16)}
  html.ts-light #tsVoicePill i{background:#216e37}
  html.ts-light #tsVoicePill[data-st="connecting"] i{background:#2b65bf}
  html.ts-light #tsVoicePill[data-st="thinking"] i{background:#a26008}
  html.ts-light #tsVoicePill[data-st="speaking"] i{background:#3b4c77}

  /* ===== 2026-11-20 Claude：机器竖起来时整套页面转成竖屏（小智自己的界面不转）=====
     主页、音乐、服务器的页面框转 90°（页面里拿到 800×1280，按各自的竖版排）；AI 讲题自己转，这里不转它的框；
     亮度面板绕自己的中心转；留言卡和小麦状态条摆到竖屏的顶上。右边朝上 = ts-portrait，左边朝上再加 ts-rot270。 */
  html.ts-portrait #tsHomeLayer iframe,html.ts-portrait #tsSubLayer>iframe:not([data-port="8096"]),html.ts-portrait #tsSubLoading,html.ts-portrait #tsSubErr{position:absolute;inset:auto;left:0;top:0;width:100vh;height:100vw;transform-origin:0 0;transform:translateX(100vw) rotate(90deg)}
  html.ts-portrait.ts-rot270 #tsHomeLayer iframe,html.ts-portrait.ts-rot270 #tsSubLayer>iframe:not([data-port="8096"]),html.ts-portrait.ts-rot270 #tsSubLoading,html.ts-portrait.ts-rot270 #tsSubErr{transform:translateY(100vh) rotate(-90deg)}
  html.ts-portrait #tsBrightnessPanel{transform:translate3d(-50%,-50%,0) rotate(90deg) translate3d(0,8px,0)}
  html.ts-portrait #tsBrightnessPanel.open{transform:translate3d(-50%,-50%,0) rotate(90deg)}
  html.ts-portrait.ts-rot270 #tsBrightnessPanel{transform:translate3d(-50%,-50%,0) rotate(-90deg) translate3d(0,8px,0)}
  html.ts-portrait.ts-rot270 #tsBrightnessPanel.open{transform:translate3d(-50%,-50%,0) rotate(-90deg)}
  @keyframes tsMsgDropP{from{opacity:0;transform:rotate(90deg) translate(-50%,8px)}to{opacity:1;transform:rotate(90deg) translate(-50%,22px)}}
  @keyframes tsMsgDropQ{from{opacity:0;transform:rotate(-90deg) translate(-50%,8px)}to{opacity:1;transform:rotate(-90deg) translate(-50%,22px)}}
  html.ts-portrait #tsMsgCard{top:50%;left:100%;width:760px;max-width:none;transform-origin:0 0;transform:rotate(90deg) translate(-50%,22px)}
  html.ts-portrait #tsMsgCard.show{animation-name:tsMsgDropP}
  html.ts-portrait.ts-rot270 #tsMsgCard{left:0;transform:rotate(-90deg) translate(-50%,22px)}
  html.ts-portrait.ts-rot270 #tsMsgCard.show{animation-name:tsMsgDropQ}
  html.ts-portrait #tsMsgCard .tm-t{max-height:200px}
  html.ts-portrait #tsVoicePill{top:50%;left:100%;width:max-content;white-space:nowrap;transform-origin:0 0;transform:rotate(90deg) translate(-50%,0)}
  html.ts-portrait #tsVoicePill.show{transform:rotate(90deg) translate(-50%,14px)}
  html.ts-portrait.ts-rot270 #tsVoicePill{left:0;transform:rotate(-90deg) translate(-50%,0)}
  html.ts-portrait.ts-rot270 #tsVoicePill.show{transform:rotate(-90deg) translate(-50%,14px)}
  #tsBrightnessPanel{height:566px}
  /* 第二轮：转的时候先用底色盖住，新的排版画好再淡出，看不到半截的页面 */
  #tsRotCover{position:fixed;inset:0;z-index:9990;background:#0a111e;opacity:0;visibility:hidden;pointer-events:none;transition:opacity .13s ease,visibility 0s linear .13s}
  #tsRotCover.on{opacity:1;visibility:visible;transition:none}
  html.ts-light #tsRotCover{background:#e3dbce}
  /* 竖屏时小麦的话显示在顶上的卡片里（不用看横着的小麦界面）；点一下收起 */
  #tsVoiceCard{position:fixed;z-index:9440;display:none;width:744px;padding:16px 22px 18px;border-radius:22px;background:#101a2b;border:1px solid #355075;color:#eef5ff;font-family:-apple-system,BlinkMacSystemFont,"Noto Sans CJK SC",sans-serif;box-shadow:0 8px 22px rgba(0,0,0,.32);-webkit-user-select:none;user-select:none;cursor:pointer}
  html.ts-portrait #tsVoiceCard.show{display:block;top:50%;left:100%;transform-origin:0 0;transform:rotate(90deg) translate(-50%,76px)}
  html.ts-portrait.ts-rot270 #tsVoiceCard.show{left:0;transform:rotate(-90deg) translate(-50%,76px)}
  #tsVoiceCard .vc-q{font-size:18px;line-height:26px;color:#a9bad3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  #tsVoiceCard .vc-q:empty{display:none}
  #tsVoiceCard .vc-a{margin-top:6px;font-size:24px;line-height:35px;font-weight:700;display:-webkit-box;-webkit-line-clamp:5;-webkit-box-orient:vertical;overflow:hidden}
  #tsVoiceCard .vc-a:empty{display:none}
  html.ts-light #tsVoiceCard{background:#f6f2ea;border-color:#d6cdbf;color:#18191b;box-shadow:0 6px 16px rgba(28,24,18,.16)}
  html.ts-light #tsVoiceCard .vc-q{color:#43454a}
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
  html.ts-xz-covered #ring{z-index:9700}
  html.ts-portrait #ring,html.ts-portrait #powerOff{inset:auto;left:0;top:0;width:100vh;height:100vw;transform-origin:0 0;transform:translateX(100vw) rotate(90deg)}
  html.ts-portrait.ts-rot270 #ring,html.ts-portrait.ts-rot270 #powerOff{transform:translateY(100vh) rotate(-90deg)}
  html.ts-portrait #settings,html.ts-portrait #wifi{inset:auto;left:0;top:0;width:100vh;height:100vw;transform-origin:0 0;transform:translateX(100vw) rotate(90deg)}
  html.ts-portrait.ts-rot270 #settings,html.ts-portrait.ts-rot270 #wifi{transform:translateY(100vh) rotate(-90deg)}
  html.ts-portrait #settings .grid{grid-template-columns:repeat(2,1fr)}
  html.ts-portrait .tskb-dock{left:0;top:0;right:auto;bottom:auto;width:100vh;padding:0 8px 8px;transform-origin:0 0;transform:rotate(90deg) translateY(6%)}
  html.ts-portrait .tskb-dock.open{transform:rotate(90deg) translateY(-100%)}
  html.ts-portrait.ts-rot270 .tskb-dock{left:100vw;top:100vh;transform:rotate(-90deg) translateY(6%)}
  html.ts-portrait.ts-rot270 .tskb-dock.open{transform:rotate(-90deg) translateY(-100%)}
  html.ts-portrait .tskb-dock .tskb{max-width:none;padding:10px;gap:8px;border-radius:20px}
  html.ts-portrait .tskb-dock .tskb-row{gap:6px}
  html.ts-portrait .tskb-dock .tskb-key{height:54px;border-radius:11px;font-size:22px;font-weight:700}
  html.ts-portrait .tskb-dock .tskb-key.fn,html.ts-portrait .tskb-dock .tskb-key.space{font-size:17px;font-weight:700}
  html.ts-portrait .tskb-dock .tskb-key.enter{font-size:19px;font-weight:700}
  html.ts-portrait #ring .ring-label{max-width:740px}
  html.ts-portrait #ring .ring-time{font-size:150px}
  html.ts-portrait #ring .ring-btns button{min-width:330px}
  html.ts-portrait #ring .ring-btns{flex-direction:column;gap:18px;align-items:center}
  html.ts-portrait #toast{top:50%;left:100%;width:max-content;max-width:740px;transform-origin:0 0;transform:rotate(90deg) translate(-50%,88px)}
  html.ts-portrait #toast.on{transform:rotate(90deg) translate(-50%,104px)}
  html.ts-portrait.ts-rot270 #toast{left:0;transform:rotate(-90deg) translate(-50%,88px)}
  html.ts-portrait.ts-rot270 #toast.on{transform:rotate(-90deg) translate(-50%,104px)}
  html.ts-portrait #hud{transform:translate(-50%,-50%) rotate(90deg) scale(.92)}
  html.ts-portrait #hud.on{transform:translate(-50%,-50%) rotate(90deg) scale(1)}
  html.ts-portrait.ts-rot270 #hud{transform:translate(-50%,-50%) rotate(-90deg) scale(.92)}
  html.ts-portrait.ts-rot270 #hud.on{transform:translate(-50%,-50%) rotate(-90deg) scale(1)}
  `;
  document.head.appendChild(style);

  /* ---------------- brightness panel (monitor header button; home tile via postMessage) ---------------- */
  let brightBtn = null, brightShade = null, brightPanel = null, brightFrame = null, homeFrameRef = null;
  let previousFocus = null, brightReady = false, brightFailed = false, brightStartedAt = 0, brightPauseTimer = 0, brightRetryTimer = 0, brightRetries = 0;
  let brightFbTx = null, orReleaseChildTouch = () => {};
  function brightnessOpen() { return !!(brightPanel && brightPanel.isConnected && brightPanel.classList.contains('open')); }
  function syncBrightnessVis(vis) {
    try { brightFrame?.contentWindow?.postMessage({ ts: 'display', visible: vis }, MON); } catch (e) {}
  }
  function loadBrightnessSrc(bframe, prewarm) {
    clearTimeout(brightRetryTimer); brightRetryTimer = 0;
    brightReady = false; brightFailed = false; brightStartedAt = Date.now();
    if (brightFbTx) brightFbTx.textContent = '正在加载亮度设置…';
    brightPanel?.classList.remove('ready');
    if (prewarm) bframe.name = 'ts-display-prewarm';
    bframe.src = MON + '/display';
  }
  function ensureBrightnessDom(prewarm = false) {
    if (!brightShade || !brightPanel || !brightShade.isConnected || !brightPanel.isConnected) {
      $('tsBrightnessShade')?.remove(); $('tsBrightnessPanel')?.remove();
      const shade = document.createElement('div'); shade.id = 'tsBrightnessShade'; shade.onclick = closeBrightness;
      const panel = document.createElement('div'); panel.id = 'tsBrightnessPanel'; panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-label', '屏幕亮度设置');
      const fb = document.createElement('div'); fb.id = 'tsBrightFallback';
      const fbTx = document.createElement('span'); fbTx.textContent = '正在加载亮度设置…';
      const fbBtn = document.createElement('button'); fbBtn.type = 'button'; fbBtn.textContent = '关闭'; fbBtn.onclick = closeBrightness;
      fb.append(fbTx, fbBtn);
      const bframe = document.createElement('iframe'); bframe.title = '屏幕亮度设置';
      bframe.addEventListener('load', () => {
        setTimeout(() => {
          if (brightFrame !== bframe || brightReady) return;
          brightFailed = true;
          if (brightRetries < 6) {
            brightRetries++;
            clearTimeout(brightRetryTimer);
            brightRetryTimer = setTimeout(() => {
              brightRetryTimer = 0;
              if (brightFrame === bframe && !brightReady) loadBrightnessSrc(bframe, !brightnessOpen());
            }, brightnessOpen() ? 1200 : 2000);
          } else if (brightFbTx) {
            brightFbTx.textContent = '亮度服务暂不可用，请关闭后重试';
          }
        }, 220);
      });
      panel.append(bframe, fb); document.body.append(shade, panel);
      brightShade = shade; brightPanel = panel; brightFrame = bframe; brightFbTx = fbTx;
      loadBrightnessSrc(bframe, prewarm);
    }
    return { shade: brightShade, panel: brightPanel, bframe: brightFrame };
  }
  function closeBrightness(restoreFocus = true) {
    if (!brightPanel?.classList.contains('open') && !brightShade?.classList.contains('open')) return;
    orReleaseChildTouch();
    brightPanel?.classList.remove('open'); brightShade?.classList.remove('open'); brightBtn?.setAttribute('aria-expanded', 'false');
    clearTimeout(brightPauseTimer);
    brightPauseTimer = setTimeout(() => { brightPauseTimer = 0; if (!brightnessOpen()) syncBrightnessVis(false); }, 125);
    const pf = previousFocus; previousFocus = null;
    if (restoreFocus) pf?.focus?.();
  }
  function openBrightness() {
    if (brightnessOpen()) return closeBrightness();
    clearTimeout(brightPauseTimer); brightPauseTimer = 0;
    brightRetries = 0;
    previousFocus = document.activeElement;
    const { shade, panel, bframe } = ensureBrightnessDom(false);
    if (bframe) {
      try { bframe.name = ''; } catch (e) {}
      if (brightFailed || (!brightReady && Date.now() - brightStartedAt > 2000)) {
        loadBrightnessSrc(bframe, false);
      }
    }
    shade.classList.add('open'); panel.classList.add('open');
    brightBtn?.setAttribute('aria-expanded', 'true');
    syncBrightnessVis(true);
  }
  window.addEventListener('message', e => {
    const bwin = brightFrame?.contentWindow;
    if (bwin && e.origin === MON && e.source === bwin) {
      if (e.data?.thinksmart === 'displayReady') {
        clearTimeout(brightRetryTimer); brightRetryTimer = 0;
        brightReady = true; brightFailed = false; brightRetries = 0;
        brightPanel?.classList.add('ready');
        syncBrightnessVis(brightnessOpen());
      } else if (e.data?.thinksmart === 'closeBrightness') closeBrightness();
    }
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeBrightness(); });
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
      brightBtn.setAttribute('aria-label', '调节屏幕亮度'); brightBtn.setAttribute('aria-expanded', 'false'); brightBtn.innerHTML = sun + '亮度';
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
    async function inboxRequest(path, opt = {}) {
      const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 8000);
      try {
        const r = await fetch(HOME + path, Object.assign({ cache: 'no-store' }, opt, { signal: ctl.signal }));
        if (!r.ok) throw new Error('inbox');
        return await r.json();
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
          if (j?.cd_open_fresh && !document.body.classList.contains('ringing')) {
            closeBrightness(false);
            forceCloseSheets();
            if (subOpen) closeSub(true);
            else { autoReturn = false; setView('home'); }
            const sendOpenCd = () => { try { homeFrameRef?.contentWindow?.postMessage({ ts: 'home', openCountdown: true }, HOME); } catch (e) {} };
            sendOpenCd();
            setTimeout(sendOpenCd, 140);
          }
        }
        const cdCloseSeq = Number(j?.cd_close_seq) || 0;
        if (lastCdCloseSeq === null) {
          lastCdCloseSeq = cdCloseSeq;
        } else if (cdCloseSeq > lastCdCloseSeq) {
          lastCdCloseSeq = cdCloseSeq;
          if (j?.cd_close_fresh && !document.body.classList.contains('ringing')) {
            closeBrightness(false);
            forceCloseSheets();
            if (subOpen) closeSub(true);
            else { autoReturn = false; setView('home'); }
            const sendCloseCd = () => { try { homeFrameRef?.contentWindow?.postMessage({ ts: 'home', closeCountdown: true }, HOME); } catch (e) {} };
            sendCloseCd();
            setTimeout(sendCloseCd, 140);
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
  let settingsEl = $('settings'), wifiEl = $('wifi'), armXzStill = () => {};
  const getSettings = () => settingsEl || (settingsEl = $('settings'));
  const getWifi = () => wifiEl || (wifiEl = $('wifi'));

  let view = 'home';
  try { sessionStorage.setItem('tsView', 'home'); } catch (e) {}
  let homeOk = false, autoReturn = false, lastTouch = Date.now(), lastBusy = 0, readyTimer = 0, xzQuietTimer = 0;
  let subOpen = false, subUrl = '', subShowTimer = 0, activeSub = null;
  const subEntries = new Map(), subPauseTimers = new Map(), subEvictTimers = new Map();
  const BUSY = new Set(['listening', 'thinking', 'speaking']);
  const isBusy = () => BUSY.has(document.body.dataset.state || 'idle') || document.body.classList.contains('ringing');
  // 2026-11-20 Claude：竖屏时小麦被唤醒不切到横着的小麦界面（顶上的状态条和文字卡照常显示），闹钟在当前页面上转正；
  // 横屏时和以前一样。主页的「小麦」磁贴在竖屏时直接叫醒小麦听你说，叫不醒再打开小麦界面。
  const xzAuto = () => !root.classList.contains('ts-portrait');
  function wakeXzPortrait() {
    fetch('/api/wake', { method: 'POST' }).then(r => { if (!r.ok) throw new Error('wake'); }).catch(() => { autoReturn = false; setView('xiaozhi'); });
  }
  const sheetOpen = () => !!(getSettings()?.classList.contains('on') || getWifi()?.classList.contains('on'));
  isHomeActiveView = () => view === 'home' && homeOk && !subOpen && !sheetOpen();

  // 「主页」 button on XiaoZhi's page (top bar, falls back to a fixed corner)
  const homeBtn = document.createElement('button'); homeBtn.id = 'tsHomeBtn'; homeBtn.setAttribute('aria-label', '回到主页');
  homeBtn.innerHTML = house + '主页'; homeBtn.style.display = 'none';
  const bar = document.querySelector('.top .right');
  if (bar) bar.prepend(homeBtn); else { homeBtn.classList.add('fixed'); document.body.appendChild(homeBtn); }

  // Floating voice status pill when XiaoZhi is covered (sub-pages / settings / connecting)
  const voicePill = document.createElement('button');
  voicePill.id = 'tsVoicePill';
  voicePill.type = 'button';
  voicePill.setAttribute('aria-label', '展开语音对话界面');
  voicePill.innerHTML = '<i aria-hidden="true"></i><span id="tsVoicePillTxt">小麦在听</span>';
  document.body.appendChild(voicePill);
  const voicePillTxt = $('tsVoicePillTxt');
  const VOICE_PILL_LABELS = {
    connecting: '小麦正在连接…',
    listening: '小麦在听，请直接说 · 点此展开',
    thinking: '小麦正在思考… · 点此展开',
    speaking: '小麦正在回答 · 点此展开',
  };
  function syncVoicePill() {
    const st = document.body.dataset.state || 'idle';
    const covered = root.classList.contains('ts-xz-covered') || sheetOpen();
    let lbl = covered && !document.body.classList.contains('ringing') ? (VOICE_PILL_LABELS[st] || '') : '';
    if (lbl && !xzAuto()) lbl = lbl.replace(' · 点此展开', '');
    if (lbl) {
      if (voicePill.dataset.st !== st) voicePill.dataset.st = st;
      if (voicePillTxt.textContent !== lbl) voicePillTxt.textContent = lbl;
      if (!voicePill.classList.contains('show')) voicePill.classList.add('show');
    } else if (voicePill.classList.contains('show')) {
      voicePill.classList.remove('show');
    }
  }
  voicePill.onclick = () => {
    if (!xzAuto()) { vcClosed = false; syncVoiceCard(); return; }
    if (subOpen) closeSub(false);
    closeBrightness(false);
    forceCloseSheets();
    autoReturn = true;
    setView('xiaozhi');
  };

  // Guard XiaoZhi's 1s clock tick from unconditionally dirtying unchanged text nodes (saves 59/60 CPU repaints/min)
  const tcDesc = Object.getOwnPropertyDescriptor(Node.prototype, 'textContent');
  if (tcDesc?.get && tcDesc?.set) {
    for (const id of ['time', 'bigTime', 'bigDate', 'greet', 'ringTime', 'timerText']) {
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

  // park XiaoZhi's animation frames while it is hidden behind the home page or a sub-page
  const nativeRaf = window.requestAnimationFrame.bind(window), nativeCaf = window.cancelAnimationFrame.bind(window);
  const parked = new Map(), moved = new Map(); let parkSeq = 0;
  window.requestAnimationFrame = cb => {
    if ((!root.classList.contains('ts-xz-covered') && !root.classList.contains('ts-homeload')) || sheetOpen()) return nativeRaf(cb);
    if (parked.size >= 64) parked.delete(parked.keys().next().value);
    const id = -(++parkSeq); parked.set(id, cb); return id;
  };
  window.cancelAnimationFrame = id => {
    if (typeof id === 'number' && id < 0) { parked.delete(id); const n = moved.get(id); if (n !== undefined) { moved.delete(id); nativeCaf(n); } }
    else nativeCaf(id);
  };
  const flushParked = () => { moved.clear(); for (const [id, cb] of parked) moved.set(id, nativeRaf(ts => { moved.delete(id); cb(ts); })); parked.clear(); };

  let homeVisTimer = 0, lastHomeVis = null, xzPauseTimer = 0, subFadeTimer = 0, subOpaqueTimer = 0;
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
    const wasCov = root.classList.contains('ts-xz-covered');
    const showHome = view === 'home' && homeOk;
    if (homeOk) root.classList.remove('ts-homeload');
    const homeload = root.classList.contains('ts-homeload');
    const xzCov = showHome || subOpen || homeload;
    root.classList.toggle('ts-home', showHome);
    root.classList.toggle('ts-xz-covered', xzCov);
    clearTimeout(xzPauseTimer);
    if (xzCov) {
      if (sheetOpen() && parked.size) flushParked();
      if (!root.classList.contains('ts-xz-paused')) {
        if (homeload) root.classList.add('ts-xz-paused');
        else xzPauseTimer = setTimeout(() => { xzPauseTimer = 0; if (root.classList.contains('ts-xz-covered')) root.classList.add('ts-xz-paused'); }, 150);
      }
    } else {
      root.classList.remove('ts-xz-paused');
      flushParked();
      if (wasCov) armXzStill();
    }
    syncHomeVis(showHome && !subOpen && !sheetOpen());
    syncInboxSchedule();
    syncXzQuietTimer();
    const wantDisp = homeOk ? '' : 'none';
    if (homeBtn.style.display !== wantDisp) homeBtn.style.display = wantDisp;
    syncVoicePill();
  }
  function setView(v) {
    view = v;
    try { sessionStorage.setItem('tsView', v); } catch (e) {}
    if (v === 'xiaozhi') lastTouch = Date.now();
    if (v === 'home') closeBrightness(false);
    apply();
  }
  const subRetryCounts = new Map(), subRetryTimers = new Map();
  function forceCloseSheets() {
    const s = getSettings(), w = getWifi(), ae = document.activeElement;
    if (ae && ((s && s.contains(ae)) || (w && w.contains(ae)))) { try { ae.blur(); } catch (e) {} }
    s?.classList.remove('on');
    w?.classList.remove('on');
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
    closeBrightness(false);
    forceCloseSheets();
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
    autoReturn = false;
    view = 'home';
    try { sessionStorage.setItem('tsView', 'home'); } catch (e) {}
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
    const subLoadMs = (entry.port === '8096' || entry.port === '8097') ? 12000 : 8000;
    subShowTimer = setTimeout(() => {
      if (subOpen && activeSub === entry && !entry.ready) {
        entry.failed = true;
        entry.loading = false;
        subErr.classList.add('show');
      }
    }, subLoadMs);
    return true;
  }
  function scheduleSubEvict(entry) {
    if (!entry || entry.port !== '8096') return;
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
    if (toHome && !document.body.classList.contains('ringing')) {
      if (isBusy() && xzAuto()) {
        autoReturn = true;
        view = 'xiaozhi';
      } else {
        autoReturn = false;
        view = 'home';
        try { sessionStorage.setItem('tsView', 'home'); } catch (e) {}
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
    setTimeout(() => { if (!brightnessOpen() && homeOk && !isBusy()) ensureBrightnessDom(true); }, 2300);
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

  let bootRetryTimer = 0, bootDeadline = 0, bootCycles = 0;
  frame.addEventListener('load', () => {
    clearTimeout(bootRetryTimer);
    if (!homeOk && Date.now() < bootDeadline) {
      bootRetryTimer = setTimeout(() => { if (!homeOk && Date.now() < bootDeadline) { lastHomeVis = null; frame.src = HOME + '/?t=' + Date.now(); } }, 650);
    }
  });
  function loadHome() {
    homeOk = false;
    lastHomeVis = null;
    bootCycles++;
    bootDeadline = Date.now() + 14000;
    root.classList.toggle('ts-homeload', view === 'home');   // dark cover while loading: no flash of XiaoZhi
    apply();
    frame.src = HOME + '/?t=' + Date.now();
    clearTimeout(readyTimer);
    clearTimeout(bootRetryTimer);
    readyTimer = setTimeout(() => {
      if (homeOk) return;
      clearTimeout(bootRetryTimer);
      root.classList.remove('ts-homeload'); apply();   // home service down: XiaoZhi stays usable
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
          clearTimeout(subShowTimer);
          const subReloadMs = (matchedSub.port === '8096' || matchedSub.port === '8097') ? 12000 : 8000;
          subShowTimer = setTimeout(() => {
            if (subOpen && activeSub === matchedSub && !matchedSub.ready) {
              matchedSub.failed = true;
              matchedSub.loading = false;
              subErr.classList.add('show');
            }
          }, subReloadMs);
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
        if (d?.ts === 'sub' && d.cmd === 'brightness') { openBrightness(); return; }
      }
      return;
    }
    if (e.origin !== HOME || e.source !== frame.contentWindow) return;
    const d = e.data;
    if (!d || d.ts !== 'home') return;
    if (d.ready) { homeOk = true; lastHomeVis = null; clearTimeout(readyTimer); clearTimeout(bootRetryTimer); if (!subOpen && isBusy() && xzAuto()) { autoReturn = true; view = 'xiaozhi'; } apply(); scheduleIdlePrewarm(); return; }
    if (d.cmd === 'xiaozhi') {
      if (d.then === 'settings') {
        closeBrightness(false);
        bindSheetObs();
        $('btnSettings')?.click();
        bindSheetObs();
        if (sheetOpen()) { flushParked(); apply(); }
      } else if (!xzAuto() && !document.body.classList.contains('ringing')) {
        wakeXzPortrait();
      } else {
        autoReturn = false;
        setView('xiaozhi');
      }
    } else if (d.cmd === 'navigate' && okSubUrl(d.url)) {
      openSub(d.url, d.subCmd);
    } else if (d.cmd === 'brightness') {
      openBrightness();
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
        else { autoReturn = false; setView('home'); }
      }
    }
  });
  homeBtn.onclick = () => { autoReturn = false; setView('home'); };
  const observedSheets = new WeakSet();
  const sheetObs = new MutationObserver(() => {
    if (!sheetOpen() && !subOpen) {
      if (view === 'xiaozhi' && homeOk && !xzAuto()) {
        autoReturn = false;
        setView('home');
        return;
      }
      if (isBusy() && view === 'home' && homeOk && xzAuto()) {
        autoReturn = true;
        closeBrightness(false);
        setView('xiaozhi');
        return;
      }
    }
    apply();
  });
  const bindSheetObs = () => {
    for (const el of [getSettings(), getWifi()]) {
      if (el && !observedSheets.has(el)) {
        observedSheets.add(el);
        sheetObs.observe(el, { attributes: true, attributeFilter: ['class'] });
      }
    }
  };
  bindSheetObs();

  // wake word / alarm -> XiaoZhi; quiet again -> back home
  let prevBodyState = document.body.dataset.state || 'idle';
  new MutationObserver(() => {
    const curState = document.body.dataset.state || 'idle';
    if (curState !== prevBodyState) {
      prevBodyState = curState;
      if (curState === 'speaking' || curState === 'idle' || curState === 'thinking') scheduleVoiceSrvCheck();
    }
    syncVoicePill();
    if (!isBusy()) return;
    lastBusy = Date.now();
    stopOverlayClip();
    try { frame.contentWindow?.postMessage({ ts: 'home', stopClip: true }, HOME); } catch (e) {}
    if (document.body.classList.contains('ringing')) {
      closeBrightness(false);
      forceCloseSheets();
      if (!xzAuto() && view === 'xiaozhi' && homeOk) { autoReturn = false; setView('home'); }
      if (!xzAuto()) return;   // 竖屏：小麦的闹钟卡片在当前页面上面转正显示，不切页面
      closeSub(false);
      autoReturn = true;
      setView('xiaozhi');
      return;
    }
    if (!subOpen && !sheetOpen() && view === 'home' && homeOk && xzAuto()) {
      autoReturn = true;
      closeBrightness(false);
      setView('xiaozhi');
    }
  }).observe(document.body, { attributes: true, attributeFilter: ['data-state', 'class'] });
  for (const ev of ['pointerdown', 'touchstart', 'keydown']) {
    addEventListener(ev, e => {
      if ((card && card.contains(e.target)) || voicePill.contains(e.target)) return;
      lastTouch = Date.now();
      if (view === 'xiaozhi') autoReturn = false;
    }, { capture: true, passive: true });
  }
  function syncXzQuietTimer() {
    const want = !subOpen && view === 'xiaozhi' && homeOk && !document.hidden;
    if (want) {
      if (!xzQuietTimer) xzQuietTimer = setInterval(() => {
        if (subOpen || view !== 'xiaozhi' || !homeOk || document.hidden) { syncXzQuietTimer(); return; }
        if (isBusy() || brightnessOpen() || sheetOpen()) { lastBusy = Date.now(); return; }
        const quiet = Date.now() - Math.max(lastTouch, lastBusy);
        if (quiet >= (autoReturn ? 30000 : 180000)) { autoReturn = false; setView('home'); }
      }, 5000);
    } else if (xzQuietTimer) {
      clearInterval(xzQuietTimer); xzQuietTimer = 0;
    }
  }
  document.addEventListener('visibilitychange', syncXzQuietTimer);

  // XiaoZhi idle stillness (kept from the previous overlay): after 60 s untouched while idle,
  // detach XiaoZhi's CSS animations; any touch or state change resumes them at once.
  const still = document.createElement('style');
  still.textContent = 'html.ts-still .app *,html.ts-still .app *::before,html.ts-still .app *::after{animation:none!important;transition:none!important;will-change:auto!important}';
  document.head.appendChild(still);
  let stillTimer = 0;
  const idle = () => (document.body.dataset.state || 'idle') === 'idle' && !document.body.classList.contains('ringing');
  const arm = () => {
    if (document.body.classList.contains('ringing')) closeBrightness(false);
    if (root.classList.contains('ts-xz-covered')) {
      if (stillTimer) { clearTimeout(stillTimer); stillTimer = 0; }
      return;
    }
    clearTimeout(stillTimer); stillTimer = 0;
    if (root.classList.contains('ts-still')) root.classList.remove('ts-still');
    if (idle()) stillTimer = setTimeout(() => { stillTimer = 0; if (idle() && !root.classList.contains('ts-xz-covered')) root.classList.add('ts-still'); }, 60000);
  };
  armXzStill = arm;
  for (const ev of ['pointerdown', 'keydown', 'wheel', 'touchstart']) addEventListener(ev, arm, { capture: true, passive: true });
  new MutationObserver(arm).observe(document.body, { attributes: true, attributeFilter: ['data-state', 'class'] });
  arm();

  /* ---------------- 2026-11-20 Claude：机身重力感应 → 整套页面横竖自动转（小智自己的界面不转）----------------
     每 0.45 秒读一次主页服务的 /api/home/orient（BMA253 原始读数）；读到新方向就 0.15 秒后再读一次，两次一致才转。
     拿在手里晃动（加速度不是 1g 左右，或两次读数差得多）、放平、斜 45° 左右时不转；手指按在屏幕上时等松手再转。
     转的时候先用底色盖住，新的排版画好再淡出。方向：跟随机器 / 竖版 / 横版（亮度面板、AI 讲题设置里都能改），
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
  // 1g ≈ 512：右边朝上 y<0 → p90，左边朝上 y>0 → p270，横放 → l；放平（xy 都小）、斜 45° 左右、拿着晃时不算
  function orClassify(x, y, z) {
    const ax = Math.abs(x), ay = Math.abs(y), g = Math.sqrt(x * x + y * y + z * z);
    if (g < 400 || g > 640) return '';
    if (Math.max(ax, ay) < 220 || Math.abs(ax - ay) < 120) return '';
    if (ay > ax) return y < 0 ? 'p90' : 'p270';
    return 'l';
  }
  const orWanted = () => (orMode === 'landscape' ? 'l' : (orMode === 'portrait' ? orPSeen : orSeen));
  function orSend(win, origin) { try { win?.postMessage({ ts: 'orient', cur: orCur, mode: orMode }, origin); } catch (e) {} }
  function orBroadcast() {
    for (const v of subEntries.values()) if (v.port === '8096') orSend(v.frame.contentWindow, v.origin);
    orSend(brightFrame?.contentWindow, MON);
  }
  const orCoverEl = document.createElement('div'); orCoverEl.id = 'tsRotCover'; orCoverEl.setAttribute('aria-hidden', 'true');
  const orHintEl = document.createElement('button'); orHintEl.id = 'tsOrHint'; orHintEl.type = 'button';
  const vcEl = document.createElement('div'); vcEl.id = 'tsVoiceCard'; vcEl.setAttribute('role', 'status');
  vcEl.innerHTML = '<div class="vc-q" id="tsVcQ"></div><div class="vc-a" id="tsVcA"></div>';
  document.body.append(orCoverEl, vcEl, orHintEl);
  function orSetClasses(o) {
    orCur = o;
    root.classList.toggle('ts-portrait', o !== 'l');
    root.classList.toggle('ts-rot270', o === 'p270');
    syncVoicePill();
  }
  // 转成竖屏：横着的小麦界面收起（状态条和文字卡接着显示）；转回横屏：正在对话或闹钟在响就照横屏的老规矩显示小麦
  function orSyncVoiceView() {
    if (!homeOk) return;
    if (!xzAuto()) {
      if (view === 'xiaozhi' && !sheetOpen()) { autoReturn = false; setView('home'); }
    } else if (document.body.classList.contains('ringing')) {
      if (view !== 'xiaozhi') { closeSub(false); closeBrightness(false); forceCloseSheets(); autoReturn = true; setView('xiaozhi'); }
    } else if (isBusy() && view === 'home' && !subOpen && !sheetOpen()) { autoReturn = true; setView('xiaozhi'); }
    syncVoiceCard();
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
    if (!root.classList.contains('ts-xz-covered') || document.hidden) {   // 看得见的是小麦界面（它不转）或后台隐藏：直接换，不用盖
      clearTimeout(orCoverT); orCoverT = 0;
      orCoverEl.classList.remove('on');
      orSetClasses(o); orBroadcast(); orSyncVoiceView(); return;
    }
    const seq = ++orGoSeq;
    clearTimeout(orCoverT);
    orCoverEl.classList.add('on');
    orRaf1 = nativeRaf(() => nativeRaf(() => {
      if (seq !== orGoSeq) return;
      orRaf1 = 0;
      const latest = orWanted();
      if (latest !== orCur) { orSetClasses(latest); orBroadcast(); orSyncVoiceView(); }
      else orBroadcast();
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
  // 竖屏时小麦说的话：从小麦页面里读最后一问一答（只读文字），显示在顶上的卡片里
  const xzBox = $('messages');
  let vcHideT = 0, vcClosed = false, vcSince = null;
  const msgText = el => { if (!el) return ''; const c = el.cloneNode(true); c.querySelectorAll('.ts,.cut,img').forEach(n => n.remove()); return (c.textContent || '').trim(); };
  function syncVoiceCard() {
    const want = !xzAuto() && root.classList.contains('ts-xz-covered') && !document.body.classList.contains('ringing') && !!xzBox;
    if (!want) { vcEl.classList.remove('show'); return; }
    if (isBusy()) { clearTimeout(vcHideT); vcHideT = 0; }
    // 只看这次对话（叫醒之后）新出来的话，不把上一轮的旧对话拿出来
    let all = [...xzBox.querySelectorAll('.msg:not(.pending)')];
    if (vcSince !== null) { const i = all.findIndex(el => el.dataset.id === vcSince); if (i >= 0) all = all.slice(i + 1); }
    const users = all.filter(el => el.classList.contains('user')), bots = all.filter(el => el.classList.contains('bot'));
    const lastU = users[users.length - 1], lastB = bots[bots.length - 1];
    const bAfterU = lastB && (!lastU || (lastU.compareDocumentPosition(lastB) & Node.DOCUMENT_POSITION_FOLLOWING));
    const q = msgText(lastU), a = bAfterU ? msgText(lastB.querySelector('.body') || lastB) : '';
    const qEl = $('tsVcQ'), aEl = $('tsVcA');
    if (qEl.textContent !== (q ? '你：' + q : '')) qEl.textContent = q ? '你：' + q : '';
    if (aEl.textContent !== a) aEl.textContent = a;
    const has = !!(q || a) && !vcClosed;
    if (has && isBusy()) vcEl.classList.add('show');
    else if (has && vcEl.classList.contains('show') && !vcHideT) vcHideT = setTimeout(() => { vcHideT = 0; vcEl.classList.remove('show'); }, 10000);
    else if (!has) vcEl.classList.remove('show');
  }
  vcEl.onclick = () => { vcClosed = true; vcEl.classList.remove('show'); };
  let vcSyncT = 0;
  const scheduleVoiceCard = () => { if (vcSyncT) return; vcSyncT = setTimeout(() => { vcSyncT = 0; if (!xzAuto()) syncVoiceCard(); }, 48); };
  if (xzBox) new MutationObserver(() => { if (!xzAuto()) scheduleVoiceCard(); }).observe(xzBox, { childList: true, subtree: true, characterData: true });
  let vcPrevBusy = false;
  new MutationObserver(() => {
    const b = isBusy();
    if (b && !vcPrevBusy && !document.body.classList.contains('ringing')) {   // 新的一次对话：卡片重新出来，只显示这次的话
      vcClosed = false;
      const ms = xzBox ? xzBox.querySelectorAll('.msg:not(.pending)') : [];
      vcSince = ms.length ? (ms[ms.length - 1].dataset.id || null) : null;
    }
    vcPrevBusy = b;
    syncVoiceCard();
  }).observe(document.body, { attributes: true, attributeFilter: ['data-state', 'class'] });
  document.addEventListener('visibilitychange', () => { if (document.hidden) { orTopDown = false; orTouchOn(false); } else orSchedule(30); });
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

  if (isBusy() && xzAuto()) { autoReturn = true; view = 'xiaozhi'; }
  loadHome();
})();
