/*
 * On-screen keyboard for the ThinkSmart kiosk (the Weston kiosk has no system keyboard).
 * Letters, digits and symbols: enough for Wi-Fi passwords, URLs and English text.
 *
 * thinksmart-kiosk.py injects this file into every page. Whenever a text field (<input> of a
 * text-like type, <textarea>) gets focus the keyboard slides up from the bottom of the screen and
 * types into it, like a phone's: pages only use plain fields. What a page can tune on the field:
 *   enterkeyhint="go|done|next|send|search"  label of the enter key (default 完成)
 *   data-kb-enter="连接"                      or any label
 *   inputmode="none" / data-kb="off"          no keyboard for this field
 * Enter on an <input> sends a real-looking keydown/keyup "Enter" (and submits its <form>), then
 * hides the keyboard unless the page keeps the focus there; in a <textarea> it types a newline.
 * While it is up, <html> has the class tskb-open and the CSS variable --tskb-height, and window
 * gets 'thinksmart-keyboard' events ({detail: {open, height}}): pages can make room. The focused
 * field is scrolled into view above it.
 *
 * The keyboard by itself, for pages that draw their own fields (old welcome page):
 *   const kb = ThinkSmartKeyboard.create(parentElement, {
 *     enterLabel: '连接',
 *     onKey(ch) {},        // a character was typed
 *     onBackspace() {},
 *     onEnter() {},
 *   });
 *   kb.setEnterEnabled(false);
 *
 * Shift: one tap capitalises the next letter, a second tap locks caps.
 * Only transform/opacity change on press, which the CPU and GPU paths both draw cheaply.
 */
(function () {
  if (window.ThinkSmartKeyboard) return;  // injected and also loaded by the page
  const LAYOUTS = {
    abc: [
      'qwertyuiop'.split(''),
      'asdfghjkl'.split(''),
      ['{shift}', ...'zxcvbnm'.split(''), '{bksp}'],
      ['{123}', '{space}', '{enter}'],
    ],
    num: [
      '1234567890'.split(''),
      ['-', '/', ':', ';', '(', ')', '$', '&', '@', '"'],
      ['{sym}', '.', ',', '?', '!', "'", '_', '{bksp}'],
      ['{abc}', '{space}', '{enter}'],
    ],
    sym: [
      ['[', ']', '{', '}', '#', '%', '^', '*', '+', '='],
      ['\\', '|', '~', '<', '>', '€', '£', '¥', '`', '·'],
      ['{123}', '.', ',', '?', '!', "'", '_', '{bksp}'],
      ['{abc}', '{space}', '{enter}'],
    ],
  };
  const LABEL = { '{shift}': '⇧', '{bksp}': '⌫', '{123}': '123', '{sym}': '#+=', '{abc}': 'ABC', '{space}': '空格' };

  const CSS = `
  .tskb { display: flex; flex-direction: column; gap: 10px; padding: 14px; border-radius: 22px;
          background: #0c1224f2; border: 1px solid #ffffff1a; user-select: none; -webkit-user-select: none; }
  .tskb-row { display: flex; gap: 8px; justify-content: center; }
  .tskb-key { flex: 1 1 0; max-width: 96px; height: 60px; border: 0; border-radius: 12px; background: #ffffff14;
              color: #eef2ff; font: 500 24px/1 "Noto Sans CJK SC", sans-serif; display: grid; place-items: center;
              transition: transform .06s, background .06s; }
  .tskb-key:active { transform: scale(.94); background: #ffffff2e; }
  .tskb-key.fn { background: #ffffff0a; font-size: 18px; color: #a9b4d6; max-width: 130px; flex: 1.5 1 0; }
  .tskb-key.on { background: #6c8cff; color: #fff; }
  .tskb-key.lock { background: #5ee0ff; color: #0b1020; }
  .tskb-key.space { flex: 5 1 0; max-width: none; font-size: 18px; color: #a9b4d6; }
  .tskb-key.enter { flex: 2 1 0; max-width: 220px; background: linear-gradient(135deg, #6c8cff, #5ee0ff); color: #0b1020;
                    font-size: 20px; font-weight: 700; }
  .tskb-key.enter[disabled] { opacity: .35; }
  .tskb-dock { position: fixed; left: 0; right: 0; bottom: 0; z-index: 2147483647; padding: 0 10px 10px;
               transform: translateY(105%); transition: transform .18s ease-out; pointer-events: none; }
  .tskb-dock.open { transform: none; pointer-events: auto; }
  .tskb-dock .tskb { max-width: 1180px; margin: 0 auto; box-shadow: 0 -8px 40px #0008; }
  `;

  function create(parent, opts) {
    opts = opts || {};
    if (!document.getElementById('tskb-style')) {
      const style = document.createElement('style');
      style.id = 'tskb-style';
      style.textContent = CSS;
      document.head.appendChild(style);
    }
    const root = document.createElement('div');
    root.className = 'tskb';
    parent.appendChild(root);

    let layout = 'abc', shift = 0;  // shift: 0 off, 1 next letter, 2 locked
    let enterEnabled = true, renderedLayout = '', keyBtns = [], enterBtn = null;

    function render() {
      if (renderedLayout !== layout) {
        renderedLayout = layout;
        root.textContent = '';
        keyBtns = [];
        enterBtn = null;
        for (const keys of LAYOUTS[layout]) {
          const row = document.createElement('div');
          row.className = 'tskb-row';
          for (const k of keys) {
            const b = document.createElement('button');
            b.type = 'button';
            b.dataset.key = k;
            b.className = 'tskb-key';
            if (k === '{space}') b.classList.add('space');
            else if (k === '{enter}') { b.classList.add('enter'); b.disabled = !enterEnabled; enterBtn = b; }
            else if (k.startsWith('{')) b.classList.add('fn');
            if (k === '{shift}' && shift) b.classList.add(shift === 2 ? 'lock' : 'on');
            b.textContent = k === '{enter}' ? (opts.enterLabel || '完成')
              : LABEL[k] || (shift && k.length === 1 ? k.toUpperCase() : k);
            // pointerdown, not click: no 300 ms delay and the field keeps its look
            b.addEventListener('pointerdown', e => { e.preventDefault(); press(k); });
            keyBtns.push(b);
            row.appendChild(b);
          }
          root.appendChild(row);
        }
        return;
      }
      for (const b of keyBtns) {
        const k = b.dataset.key;
        if (k === '{shift}') {
          b.classList.toggle('on', shift === 1);
          b.classList.toggle('lock', shift === 2);
        } else if (k && k.length === 1) {
          b.textContent = shift ? k.toUpperCase() : k;
        }
      }
    }

    let lastShift = 0;
    function press(k) {
      const prevLayout = layout, prevShift = shift;
      if (k === '{shift}') {
        const now = Date.now();
        shift = shift === 0 ? 1 : (shift === 1 && now - lastShift < 400 ? 2 : 0);
        lastShift = now;
      } else if (k === '{123}') layout = 'num';
      else if (k === '{sym}') layout = 'sym';
      else if (k === '{abc}') layout = 'abc';
      else if (k === '{bksp}') opts.onBackspace && opts.onBackspace();
      else if (k === '{enter}') { if (enterEnabled && opts.onEnter) opts.onEnter(); }
      else {
        const ch = k === '{space}' ? ' ' : (shift ? k.toUpperCase() : k);
        opts.onKey && opts.onKey(ch);
        if (shift === 1) shift = 0;
      }
      if (layout !== prevLayout || shift !== prevShift) render();
    }

    render();
    return {
      el: root,
      setEnterEnabled(on) {
        const next = !!on;
        if (enterEnabled === next) return;
        enterEnabled = next;
        if (enterBtn) enterBtn.disabled = !enterEnabled;
      },
      setEnterLabel(text) {
        opts.enterLabel = text;
        this.label = text;
        if (enterBtn) enterBtn.textContent = text || '完成';
      },
      reset() { lastShift = 0; if (layout !== 'abc' || shift !== 0) { layout = 'abc'; shift = 0; render(); } },
      destroy() { root.remove(); },
    };
  }

  /* ---- system keyboard: follows the focused text field ---- */
  const TEXT_TYPES = ['text', 'password', 'search', 'url', 'email', 'tel', 'number', ''];
  const ENTER_LABELS = { go: '前往', done: '完成', next: '下一项', send: '发送', search: '搜索', enter: '换行' };
  let dock = null, kb = null, field = null, hideTimer = 0, watchTimer = 0;

  function editable(el) {
    if (!el || el.disabled || el.readOnly) return false;
    if (el.getAttribute('inputmode') === 'none' || (el.dataset && el.dataset.kb === 'off')) return false;
    if (el.tagName === 'TEXTAREA') return true;
    return el.tagName === 'INPUT' && TEXT_TYPES.includes((el.getAttribute('type') || '').toLowerCase());
  }

  function changed(el) {
    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText' }));
  }

  function insert(text) {
    const el = field;
    if (!el) return;
    const max = el.maxLength >= 0 ? el.maxLength : Infinity;
    if ((el.getAttribute('type') || '').toLowerCase() === 'number' && !/^[0-9.+-]$/.test(text)) return;
    // number/email inputs have no selection API: append at the end
    let start, end;
    try { start = el.selectionStart; end = el.selectionEnd; } catch (e) { start = null; }
    if (start == null) {
      if (el.value.length + text.length > max) return;
      try {
        if (document.activeElement === el && typeof document.execCommand === 'function' && document.execCommand('insertText', false, text)) return;
      } catch (e) {}
      el.value += text;
    } else {
      if (el.value.length - (end - start) + text.length > max) return;
      el.setRangeText(text, start, end, 'end');
    }
    changed(el);
  }

  function backspace() {
    const el = field;
    if (!el) return;
    let start, end;
    try { start = el.selectionStart; end = el.selectionEnd; } catch (e) { start = null; }
    if (start == null) {
      if (!el.value.length && !(el.validity && el.validity.badInput)) return;
      try {
        if (document.activeElement === el && typeof document.execCommand === 'function' && document.execCommand('delete', false)) return;
      } catch (e) {}
      const cps = Array.from(el.value);
      if (!cps.length) return;
      cps.pop();
      el.value = cps.join('');
    } else if (start !== end) {
      el.setRangeText('', start, end, 'end');
    } else if (start > 0) {
      const v = el.value;
      const hi = start >= 2 ? v.charCodeAt(start - 2) : 0;
      const lo = v.charCodeAt(start - 1);
      const step = (hi >= 0xD800 && hi <= 0xDBFF && lo >= 0xDC00 && lo <= 0xDFFF) ? 2 : 1;
      el.setRangeText('', start - step, start, 'end');
    } else return;
    changed(el);
  }

  function enter() {
    const el = field;
    if (!el) return;
    if (el.tagName === 'TEXTAREA') return insert('\n');
    const opts = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true };
    const go = el.dispatchEvent(new KeyboardEvent('keydown', opts));
    el.dispatchEvent(new KeyboardEvent('keyup', opts));
    if (go && el.form) {
      if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit();
    }
    // the page may have moved the focus on (next field) or kept it; otherwise we are done
    setTimeout(() => { if (document.activeElement === el && go) el.blur(); }, 0);
  }

  function enterLabel(el) {
    return (el.dataset && el.dataset.kbEnter) || ENTER_LABELS[el.getAttribute('enterkeyhint')] ||
      (el.tagName === 'TEXTAREA' ? '换行' : '完成');
  }

  function announce(open) {
    const h = open && dock ? dock.offsetHeight : 0;
    document.documentElement.classList.toggle('tskb-open', open);
    document.documentElement.style.setProperty('--tskb-height', h + 'px');
    window.dispatchEvent(new CustomEvent('thinksmart-keyboard', { detail: { open, height: h } }));
  }

  function show(el) {
    clearTimeout(hideTimer);
    if (!dock) {
      dock = document.createElement('div');
      dock.className = 'tskb-dock';
      // taps on the keyboard never take the focus away from the field
      dock.addEventListener('pointerdown', e => e.preventDefault());
      dock.addEventListener('mousedown', e => e.preventDefault());
      document.body.appendChild(dock);
      kb = create(dock, { onKey: insert, onBackspace: backspace, onEnter: enter });
    }
    const label = enterLabel(el);
    if (field !== el || kb.label !== label) { kb.reset(); kb.setEnterLabel(label); }
    field = el;
    if (!watchTimer) {
      watchTimer = setInterval(() => {
        if (field && (!field.isConnected || field.disabled || field.readOnly ||
                      !field.getClientRects().length || getComputedStyle(field).visibility === 'hidden')) {
          const f = field;
          hide();
          f.blur();
        }
      }, 500);
    }
    const wasOpen = dock.classList.contains('open');
    dock.classList.add('open');
    if (!wasOpen) announce(true);
    // keep the field visible above the keyboard
    setTimeout(() => {
      if (field !== el) return;
      const r = el.getBoundingClientRect(), top = window.innerHeight - dock.offsetHeight - 16;
      if (r.bottom > top) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }, 200);
  }

  function hide() {
    field = null;
    if (watchTimer) {
      clearInterval(watchTimer);
      watchTimer = 0;
    }
    if (dock && dock.classList.contains('open')) {
      dock.classList.remove('open');
      announce(false);
    }
  }

  document.addEventListener('focusin', e => {
    if (editable(e.target)) show(e.target);
  }, true);
  document.addEventListener('focusout', e => {
    if (e.target !== field) return;
    // focus moving straight to another field: don't flicker
    hideTimer = setTimeout(() => { if (!editable(document.activeElement)) hide(); }, 60);
  }, true);
  // watchTimer in show()/hide() closes the keyboard if its focused field disappears

  window.ThinkSmartKeyboard = { create, hide, auto: true };
})();
