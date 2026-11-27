#!/opt/thinksmart-addons/venv/bin/python
"""ThinkSmart Standalone AI Dynamic Blackboard Micro-Lesson Tutor Service (Port 8096) — R2 Deep Polish.

Features:
- 100% upgrade-isolated from /opt/thinksmart/client and xiaozhi-hub.
- Three problem input channels + On-Screen Voice Recording (thinksmart_mic -> MacASR):
  1. External USB UVC Document Camera (/dev/video* on USB bus, auto-detected, high-res MJPEG snap + crop + text crispness filter)
  2. Phone Intercom photo/text upload relay (/api/tutor/phone_upload)
  3. Built-in 8 classic math/geometry micro-lessons + touch math keypad + voice problem input + 1-tap isomorphic variant generator (举一反三)
- Dual-engine AI Blackboard generator with Brace-Depth Streaming JSON Extractor:
  * Fast Multimodal Mode (GPT-6.1 Terra / Sol + DeepSeek): direct vision + streaming blackboard steps
  * Deep Precision Mode (GPT-6.1 Sol + Terra + DeepSeek failover relay)
- Sequential Semaphore-guarded 24kHz WAV synthesis via local Mac IndexStreamTTS (priority on-demand + retry + zero silent-cache poisoning + natural Chinese math pronunciation normalizer).
"""
import ast
import asyncio
import base64
import collections
import contextlib
import copy
import fcntl
import hashlib
import hmac
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import sys
import threading
try:
    threading.stack_size(1048576)
except (RuntimeError, ValueError):
    pass
import time
import urllib.parse
import uuid

import io
import aiohttp
from aiohttp import web
if '/usr/lib/python3/dist-packages' not in sys.path:
    sys.path.append('/usr/lib/python3/dist-packages')
try:
    from PIL import (
        Image as _PILImage,
        ImageChops as _PILChops,
        ImageFilter as _PILFilter,
        ImageOps as _PILImageOps,
        ImageEnhance as _PILImageEnhance,
    )
    _HAS_PIL = True
except Exception:
    _PILImage = None
    _PILChops = None
    _PILFilter = None
    _PILImageOps = None
    _PILImageEnhance = None
    _HAS_PIL = False

if '/usr/lib/python3/dist-packages' not in sys.path:
    sys.path.append('/usr/lib/python3/dist-packages')
try:
    import yaml as _yaml
except ImportError:
    _yaml = None

PORT = int(os.environ.get('TUTOR_PORT', '8096'))
SHARE_DIR = Path(os.environ.get('TUTOR_SHARE', '/usr/local/share/thinksmart-tutor'))
STATE_DIR = Path(os.environ.get('TUTOR_STATE', '/var/lib/thinksmart-tutor'))
SNAP_DIR = STATE_DIR / 'snaps'
HIST_PATH = STATE_DIR / 'history.json'
PREFS_PATH = STATE_DIR / 'prefs.json'

_URL_CRED_RE = re.compile(r'((?:https?|wss?)://)(?:[^/\s@]+@)?([^\s/\'"<>)\]]+)(?:/[^\s\'"<>)\]]*)?', re.I)
_BEARER_SK_RE = re.compile(r'(Bearer\s+)[A-Za-z0-9._~+/=-]+|\bsk-[A-Za-z0-9._-]{6,}', re.I)


def _redact_secrets(msg: object, *extra_secrets: str) -> str:
    s = str(msg or '')
    for sec in extra_secrets:
        if sec and len(sec) >= 6 and sec != 'none':
            s = s.replace(sec, '***')
    s = _URL_CRED_RE.sub(r'\1\2/***', s)
    s = _BEARER_SK_RE.sub(lambda m: (m.group(1) + '***') if m.group(1) else 'sk-***', s)
    return s


def _norm_mode(val) -> str:
    s = str(val or '').strip().lower()
    if s == 'deep':
        return 'deep'
    if s in ('backup', 'geo'):
        return 'backup'
    return 'fast'


def _mistake_path() -> Path:
    if HIST_PATH.parent != Path('/var/lib/thinksmart-tutor'):
        return HIST_PATH.parent / 'mistake_book.json'
    return STATE_DIR / 'mistake_book.json'


def _cowrite_state_path() -> Path:
    if HIST_PATH.parent != Path('/var/lib/thinksmart-tutor'):
        return HIST_PATH.parent / 'cowrite_state.json'
    return STATE_DIR / 'cowrite_state.json'


STUDY_CFG_PATH = Path(os.environ.get('TUTOR_STUDY_CONFIG', '/var/lib/thinksmart-study/config.json'))
STUDY_SECRET_PATH = Path(os.environ.get('TUTOR_STUDY_SECRET', '/var/lib/thinksmart-study/secret'))
_STUDY_KEY_CACHE = {}


def _study_keys(secret: bytes, direction: str) -> tuple:
    k = (secret, direction)
    pair = _STUDY_KEY_CACHE.get(k)
    if pair is None:
        enc = hmac.new(secret, b'study-vision enc ' + direction.encode(), hashlib.sha256).digest()
        mac = hmac.new(secret, b'study-vision mac ' + direction.encode(), hashlib.sha256).digest()
        pair = (enc, mac)
        if len(_STUDY_KEY_CACHE) >= 16:
            _STUDY_KEY_CACHE.pop(next(iter(_STUDY_KEY_CACHE)))
        _STUDY_KEY_CACHE[k] = pair
    return pair


def _study_xor(data: bytes, enc: bytes, nonce: bytes) -> bytes:
    if not data:
        return b''
    ks = hashlib.shake_256(enc + nonce).digest(len(data))
    return (int.from_bytes(data, 'big') ^ int.from_bytes(ks, 'big')).to_bytes(len(data), 'big')


def _study_seal(secret: bytes, direction: str, obj: dict) -> dict:
    enc, mac = _study_keys(secret, direction)
    nonce, ts = os.urandom(16), int(time.time())
    ct = _study_xor(json.dumps(obj, ensure_ascii=False).encode('utf-8'), enc, nonce)
    tag = hmac.new(mac, b'v1|%d|' % ts + nonce + ct, hashlib.sha256).digest()
    return {
        'v': 1,
        'ts': ts,
        'nonce': base64.b64encode(nonce).decode('ascii'),
        'ct': base64.b64encode(ct).decode('ascii'),
        'tag': base64.b64encode(tag).decode('ascii'),
    }


def _study_unseal(secret: bytes, direction: str, env: dict, max_age: int = 300) -> dict:
    enc, mac = _study_keys(secret, direction)
    if not isinstance(env, dict) or type(env.get('v')) is not int or env['v'] != 1 or type(env.get('ts')) is not int:
        raise PermissionError('bad envelope')
    ts = env['ts']
    if not all(isinstance(env.get(k), str) for k in ('nonce', 'ct', 'tag')):
        raise PermissionError('bad envelope')
    nonce, ct, tag = (base64.b64decode(env[k], validate=True) for k in ('nonce', 'ct', 'tag'))
    if len(nonce) != 16 or len(tag) != 32:
        raise PermissionError('bad envelope')
    want = hmac.new(mac, b'v1|%d|' % ts + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, want) or abs(time.time() - ts) > max_age:
        raise PermissionError('bad reply seal')
    reply = json.loads(_study_xor(ct, enc, nonce))
    if not isinstance(reply, dict):
        raise PermissionError('bad reply payload')
    return reply


_LATEX_MACRO_PATTERNS = tuple(
    (re.compile(pat), rep) for pat, rep in (
        (r'\\(?:dfrac|tfrac)(?![a-zA-Z])', r'\\frac'),
        (r'\\Delta(?![a-zA-Z])', 'Δ'),
        (r'\\triangle(?![a-zA-Z])', '△'),
        (r'\\odot(?![a-zA-Z])', '⊙'),
        (r'\\because(?![a-zA-Z])', '∵'),
        (r'\\therefore(?![a-zA-Z])', '∴'),
        (r'\\(?:neq|ne)(?![a-zA-Z])', '≠'),
        (r'\\(?:geqslant|geq|ge)(?![a-zA-Z])', '≥'),
        (r'\\(?:leqslant|leq|le)(?![a-zA-Z])', '≤'),
        (r'\\pm(?![a-zA-Z])', '±'),
        (r'\\mp(?![a-zA-Z])', '∓'),
        (r'\\times(?![a-zA-Z])', '×'),
        (r'\\div(?![a-zA-Z])', '÷'),
        (r'\\cdot(?![a-zA-Z])', '·'),
        (r'\\approx(?![a-zA-Z])', '≈'),
        (r'\\pi(?![a-zA-Z])', 'π'),
        (r'\\Omega(?![a-zA-Z])', 'Ω'),
        (r'\\alpha(?![a-zA-Z])', 'α'),
        (r'\\beta(?![a-zA-Z])', 'β'),
        (r'\\gamma(?![a-zA-Z])', 'γ'),
        (r'\\theta(?![a-zA-Z])', 'θ'),
        (r'\\lambda(?![a-zA-Z])', 'λ'),
        (r'\\mu(?![a-zA-Z])', 'μ'),
        (r'\\rho(?![a-zA-Z])', 'ρ'),
        (r'\\sim(?![a-zA-Z])', '∽'),
        (r'\\cong(?![a-zA-Z])', '≌'),
        (r'\\perp(?![a-zA-Z])', '⊥'),
        (r'\\parallel(?![a-zA-Z])', '∥'),
        (r'\\angle(?![a-zA-Z])', '∠'),
        (r'\\circ(?![a-zA-Z])', '°'),
        (r'\\infty(?![a-zA-Z])', '∞'),
        (r'\\uparrow(?![a-zA-Z])', '↑'),
        (r'\\downarrow(?![a-zA-Z])', '↓'),
        (r'\\(?:rightarrow|to)(?![a-zA-Z])', '→'),
        (r'\\(sin|cos|tan|cot)(?![a-zA-Z])', r'\1'),
    )
)
_SUB_DIGITS_MAP = str.maketrans('0123456789', '₀₁₂₃₄₅₆₇₈₉')
_SUP_DIGITS_MAP = str.maketrans('0123456789+-', '⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻')


def _cowrite_latex_to_unicode(txt: str) -> str:
    """Convert inline LaTeX math macros into clean readable Grade-9 Unicode so non-KaTeX Co-Write UI & spoiler scrubbers work seamlessly."""
    s = str(txt or '').strip()
    if not s or ('\\' not in s and '$' not in s and '^' not in s and '_' not in s and '°' not in s):
        return s
    s = re.sub(r'\\(?:left|right)\b', '', s)
    s = re.sub(r'\\(?:text|mathrm|mathbf|mathit)\s*\{([^{}]*)\}', r'\1', s)
    s = re.sub(r'\\(?:widehat|overset\{\\frown\})\s*\{([^{}]+)\}', r'弧\1', s)
    s = re.sub(r'\\xlongequal\s*\{([^{}]*)\}', r'=(\1)=', s)
    s = re.sub(r'\\xrightarrow\s*\{([^{}]*)\}', r'→(\1)', s)
    s = re.sub(r'\^\s*\{?\s*\\circ\s*\}?(?![a-zA-Z])', '°', s)
    for rx_ltx, v_uni in _LATEX_MACRO_PATTERNS:
        s = rx_ltx.sub(v_uni, s)
    s = re.sub(r'\^\s*\{?\s*°\s*\}?', '°', s)
    s = re.sub(r'°\s*C(?![A-Za-z])', '℃', s)

    # Flatten sub/superscripts BEFORE \frac/\sqrt so nested braces like \frac{U_{实}^2}{R} decode cleanly.
    s = re.sub(r'\^\s*\{([0-9+\-]+)\}', lambda m: m.group(1).translate(_SUP_DIGITS_MAP), s)
    s = re.sub(r'(?<=[A-Za-z0-9)\]\u4e00-\u9fff])\^([0-9])(?![0-9])', lambda m: m.group(1).translate(_SUP_DIGITS_MAP), s)
    s = re.sub(r'_\s*\{([0-9]+)\}', lambda m: m.group(1).translate(_SUB_DIGITS_MAP), s)
    s = re.sub(r'_\s*\{([A-Za-z\u4e00-\u9fff0-9]{1,8})\}', r'\1', s)
    s = re.sub(r'(?<=[A-Za-z)\]])_([0-9])(?![0-9])', lambda m: m.group(1).translate(_SUB_DIGITS_MAP), s)
    s = re.sub(r'(?<=[A-Za-z)\]])_([A-Za-z])(?![A-Za-z0-9])', r'\1', s)

    def _repl_frac(m):
        num = m.group(1).strip()
        den = m.group(2).strip()
        n_wrap = f'({num})' if re.search(r'[+\-−±∓]', num[1:]) else num
        d_wrap = f'({den})' if re.search(r'[+\-−±∓\s]', den) and len(den) > 1 else den
        return f'{n_wrap}/{d_wrap}'

    for _ in range(2):
        s = re.sub(r'\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}', _repl_frac, s)
        s = re.sub(
            r'\\sqrt\s*\[3\]\s*\{([^{}]+)\}',
            lambda m: f"∛{m.group(1).strip()}" if re.fullmatch(r'[0-9A-Za-z]+', m.group(1).strip()) else f"∛({m.group(1).strip()})",
            s,
        )
        s = re.sub(
            r'\\sqrt\s*\{([^{}]+)\}',
            lambda m: f"√{m.group(1).strip()}" if re.fullmatch(r'[0-9A-Za-z]+', m.group(1).strip()) else f"√({m.group(1).strip()})",
            s,
        )
    s = re.sub(r'\^\s*\{([0-9+\-]+)\}', lambda m: m.group(1).translate(_SUP_DIGITS_MAP), s)
    s = re.sub(r'(?<=[A-Za-z0-9)\]\u4e00-\u9fff])\^([0-9])(?![0-9])', lambda m: m.group(1).translate(_SUP_DIGITS_MAP), s)
    s = re.sub(r'_\s*\{([0-9]+)\}', lambda m: m.group(1).translate(_SUB_DIGITS_MAP), s)
    s = re.sub(r'_\s*\{([A-Za-z\u4e00-\u9fff0-9]{1,8})\}', r'\1', s)
    s = re.sub(r'(?<=[A-Za-z)\]])_([0-9])(?![0-9])', lambda m: m.group(1).translate(_SUB_DIGITS_MAP), s)
    s = re.sub(r'(?<=[A-Za-z)\]])_([A-Za-z])(?![A-Za-z0-9])', r'\1', s)
    s = s.replace('\\(', '').replace('\\)', '').replace('\\[', '').replace('\\]', '').replace('$', '')
    return re.sub(r'\s+', ' ', s).strip()


def _scrub_cowrite_common_prefixes(s: str) -> str:
    """Shared phrase, copula, contrast, choice, dictation, and derivation spoiler scrubbers."""
    # 1. Explicit answer/root/extremum phrases requiring a copula/operator (never mangles '增根', '无解', '因式分解', '选项中')
    s = re.sub(
        r'(?:(?:正确|标准|最终)\s*(?:答案|得数|结果|解|根|选项|分值|汉字|写法|(?:化学|反应|离子|电离)?方程式)|(?<![分理求无增有重判列写配平])(?:答案|得数|结果|方程的解|实根|正解|正确选项|最[大小]值|顶点坐标|(?:化学|反应|离子|电离)方程式))\s*(?:应该?是|应当是|实为|应为|是|为|=|＝|:|：)\s*[^，。；！？,;!?]+',
        '请回查这一步关键条件',
        s,
    )
    s = re.sub(
        r'(?:(?:正确|标准|最终|计算)\s*(?:答案|得数|结果|解|根|分值)|(?<![分理求无增有重判列写配平第])(?:正确答案|正确得数|正确结果|标准答案|最终答案|最终结果|答案|得数|方程的解|实根|正解|最[大小]值))\s+[-−－±]?(?:\d|[√π])[^，。；！？,;!?]*',
        '请回查这一步关键条件',
        s,
    )
    s = re.sub(
        r'(?:取值范围|解集)\s*(?:应该?是|应当是|实为|应为|是|为|=|＝|:|：)\s*[^，。；！？,;!?]+',
        '请回查对应取值范围',
        s,
    )
    s = re.sub(
        r'(?:(?<![A-Za-z0-9_])[a-zA-Z]\s*的值|两根(?:分别)?|方程的两根|相似比|比值|溶质质量分数|质量分数)\s*(?:应该?是|应当是|实为|应为|是|为|=|＝|:|：)\s*(?!0(?![.\d:：/]))[-−－±]?(?:\d|[√π])[^，。；！？,;!?]*',
        '请回查对应数值关系',
        s,
    )
    s = re.sub(
        r'(?:应该?是|应当是|实为|应为|本应为|本应是)\s*(?!0(?![.\d:：/]))[-−－±]?(?:\d+(?:\.\d+)?(?:/\d+)?(?:\s*[:：]\s*\d+(?:\.\d+)?)?|(?:\d*\s*)?[√π]\d*(?:/\d+)?)(?:\s*(?:[°%℃Ω]|[WVJANgmLs]|Pa|Hz|kW|kWh|cm|mm|dm|km|kg|mg|mL|mol|[瓦伏安欧焦牛帕克米秒度]+)(?![A-Za-z]))?(?:\s*(?:和|或|与|、)\s*[-−－±]?(?:\d+(?:\.\d+)?(?:/\d+)?|(?:\d*\s*)?[√π]\d*(?:/\d+)?)(?:\s*(?:[°%℃Ω]|[WVJANgmLs]|Pa|Hz|kW|kWh|cm|mm|dm|km|kg|mg|mL|mol|[瓦伏安欧焦牛帕克米秒度]+)(?![A-Za-z]))?)*',
        '请独立重算核对',
        s,
    )
    s = re.sub(
        r'(?:不是|并非)\s*(?:(?![，。；！？,;!?]).){1,20}[，,]?\s*而是\s*(?!0(?![.\d:：/]))[-−－±]?(?:\d+(?:\.\d+)?(?:/\d+)?(?:\s*[:：]\s*\d+(?:\.\d+)?)?|(?:\d*\s*)?[√π]\d*(?:/\d+)?|[A-Za-z][₁₂₀-₉]?\s*[=＝]\s*[-−－±]?(?:\d|[√π]))[^，。；！？,;!?]*',
        '请重算核对该数值',
        s,
    )
    # English word/tense contrast leaks & verbal derivations
    s = re.sub(
        r'(?:不是|并非)\s*[“"\'\x60]?(?:[a-z]{2,}|[A-Z][a-z]+)(?:\s+[A-Za-z]{2,}){0,2}[”"\'\x60]?\s*[，,]?\s*而是\s*[“"\'\x60]?(?:[a-z]{2,}|[A-Z][a-z]+)(?:\s+[A-Za-z]{2,}){0,3}[”"\'\x60]?',
        '请核对该处词形或时态',
        s,
    )
    s = re.sub(
        r'(?i)\bshould\s+be\s+[“"\'\x60]?(?:[a-z]{2,}(?:\s+[a-z]{2,}){0,2}|-?\d+(?:\.\d+)?(?:/\d+)?)[”"\'\x60]?',
        '请核对该处用法',
        s,
    )
    s = re.sub(
        r'(?:(?:正确|标准|原)?\s*(?:拼写|单词|词汇|词形|写法|短语))\s*(?:(?:应该?是|应当是|实为|应为|是|为|=|＝|:|：)\s*|\s+)[“"\'\x60]?[A-Za-z]{2,}(?:\s+[A-Za-z]{1,}){0,3}[”"\'\x60]?(?=\s|[，。；！？,;!?]|$)',
        '请核对该处单词拼写或词形',
        s,
    )
    s = re.sub(
        r'(?:(?<!不)等于|应得|可得|求得|解得|算出结果)\s*(?!0(?![.\d:：/]))[-−－±]?(?:\d+(?:\.\d+)?(?:/\d+)?|(?:\d*\s*)?[√π]\d*(?:/\d+)?)(?:\s*(?:[°%℃Ω]|[WVJANgmLs]|Pa|Hz|kW|kWh|cm|mm|dm|km|kg|mg|mL|mol|[瓦伏安欧焦牛帕克米秒度]+)(?![A-Za-z]))?',
        '请独立重算核对',
        s,
    )
    # 2. Quoted or unquoted Chinese dictation / character correction leaks ('应改为“沧”字', '应为“长风破浪会有时”中的“沧”', '不是苍而是沧', '误把沧写成苍', '应写沧字')
    s = re.sub(
        r'(?:(?:(?:应该?|需)?(?:改为|改成|更正为|应写|应填|写成|填为|正字为|正确字为|原句(?:应该?|应)?[是为]|正确诗句(?:应该?|应)?[是为])|(?:正确[字词句]?(?:应该?|实)?[是为]|应[是为]))\s*[：:=]?\s*[“"「『《](?=[^”"」』》]*[\u4e00-\u9fff])[^”"」』》]{1,24}[”"」』》](?:\s*[中里]?的\s*[“"「『《]?[\u4e00-\u9fff]{1,4}[”"」』》]?)?\s*字?|(?:应该?|需)?(?:改为|改成|更正为|应写|应填|正字为|正确字为)\s*[：:=]?\s*[\u4e00-\u9fff]{1,4}\s*字|不是\s*[“"「『]?[\u4e00-\u9fff]{1,4}[”"」』]?\s*[，,]?\s*而是\s*[“"「『]?[\u4e00-\u9fff]{1,6}[”"」』]?\s*字?|(?:误)?[把将]\s*[“"「『]?[\u4e00-\u9fff]{1,4}[”"」』]?\s*写成了?\s*[“"「『]?[\u4e00-\u9fff]{1,4}[”"」』]?)',
        '请重点核对该字写法',
        s,
    )
    # 3. Leaked coordinate pairs like '坐标为 (2, -1)' or '顶点 (2, -1)'
    s = re.sub(
        r'(?:顶点坐标|交点坐标|坐标|顶点|交点)\s*(?:应该?是|为|是|=|＝|:|：)?\s*[（(]\s*[-−+±]?(?:\d|[√π])[^)）]{1,18}[)）]',
        '对应坐标请自查',
        s,
    )
    # 4. Choice-letter leaks ('应选 B', '选(C)', '正确选项 A', '正确答案 B') without touching '选择题' or geometry '选 A 点'
    s = re.sub(
        r'(?:正确选项|正确答案|标准答案|答案选|答案|应选|改选|选)\s*[：:=]?\s*[（(]?[A-Da-d][)）]?(?![a-zA-Z])(?!\s*(?:点|处|端|角|边|极|球|管|车|船|容器|物体|滑片|项))',
        '请再核对选项依据',
        s,
    )
    # 5. Explicit ASCII/formula/subscript replacement leaks ('改为 Fe₃O₄', '应补上 O₂↑', '应填 went', '改成 -3') without mangling '得出 k 的值' or '应填 + 还是 -'
    s = re.sub(
        r'(?:(?:应该|应|需)(?:填|写|补上?)|(?:应该|应|需)?(?:改为|改成|更正为|补上|漏写|漏标)|(?<!空)填)\s*[：:=]?\s*(?![+\-−±]\s*(?:还是|或))(?![A-Za-z]\s*(?:的|是|为|与|和|或|在|不|值|号|项|式))[A-Za-z0-9₀-₉⁰-⁹+\-−±√π/.,()↑↓]+(?:\s+[A-Za-z0-9₀-₉⁰-⁹+\-−±√π/.,()↑↓]+){0,3}',
        '请重点核对该处写法',
        s,
    )
    return s


def _scrub_cowrite_spoilers(txt: str, max_len: int = 58) -> str:
    """Strictly strip any leaked final numbers, radicals, coordinates, letter options, Chinese dictation chars, or explicit equations from Co-Write hints while keeping legitimate math terms ('增根', '无解', '因式分解', '选择题', '得出 k', '填 + 还是 -', '= 0', '≠ 0') intact."""
    s = _cowrite_latex_to_unicode(re.sub(r'\s+', ' ', str(txt or '')).strip())
    if not s:
        return '建议回查这一步条件与符号对应关系'
    s = _scrub_cowrite_common_prefixes(s)
    # 6. Keep "≠ 0" / "= 0" / "Δ > 0" / "≥ 0" domain & discriminant checks intact while scrubbing leaked non-zero numbers, inequalities, radicals (2√3), and π!
    s = re.sub(
        r'[=＝≈]\s*[（(]\s*[-−－+±]?(?:\d|[√π])[^()（）=]{1,24}[)）](?:\s*/\s*\d+(?:\.\d+)?)?',
        '= ？',
        s,
    )
    s = re.sub(
        r'[=＝≈]\s*(?!0(?![.\d]))[-−－±]?(?:\d+(?:\.\d+)?(?:\s*[√π]\d*)?(?:/\d+)?%?|(?:\d*\s*)?[√π]\d*(?:/\d+)?)',
        '= ？',
        s,
    )
    s = re.sub(
        r'(?<![A-Za-z0-9_])[-−－+]?(?:\d+(?:\.\d+)?(?:/\d+)?|(?:\d*\s*)?[√π]\d*(?:/\d+)?)\s*([<>＜＞≤≥])\s*([a-zA-Z])\s*([<>＜＞≤≥])\s*[-−－+]?(?:\d+(?:\.\d+)?(?:/\d+)?|(?:\d*\s*)?[√π]\d*(?:/\d+)?)',
        r'？ \1 \2 \3 ？',
        s,
    )
    s = re.sub(
        r'(?<![A-Za-z0-9_Δ+\-−*/^²³])(?<![+\-−*/]\s)([a-zA-Z])\s*([<>＜＞≤≥])\s*(?!0(?![.\d]))[-−－+]?(?:\d+(?:\.\d+)?(?:/\d+)?|(?:\d*\s*)?[√π]\d*(?:/\d+)?)',
        r'\1 \2 ？',
        s,
    )
    s = re.sub(
        r'(?<![A-Za-z0-9_Δ+\-−*/^²³])(?<![+\-−*/]\s)([a-zA-Z])\s*≠\s*(?!0(?![.\d]))[-−－+]?(?:\d+(?:\.\d+)?(?:/\d+)?|(?:\d*\s*)?[√π]\d*(?:/\d+)?)',
        r'\1 ≠ ？',
        s,
    )
    s = re.sub(
        r'(?<!漏乘)(?<!漏乘了)(?<!漏乘\s)(?<!漏乘了\s)\b\d+(?:\.\d+)?\s*[×÷*/]\s*\d+(?:\.\d+)?(?!\s*(?:系数|[A-Za-zπ(√]))',
        '对应式',
        s,
    )
    s = re.sub(r'^[，。；、：:\s]+', '', s).strip()
    return (s[:max_len] if s else '建议回查这一步条件与符号对应关系')


def _scrub_cowrite_formula_tip(txt: str, max_len: int = 66) -> str:
    """Sanitize Co-Write textbook formulas while preserving symbolic coefficients ('S = 1/2 ah', 'Ek = 1/2 mv²'), trig values ('sin30° = 1/2'), and physical constants ('1 kWh = 3.6 × 10⁶ J')."""
    s = _cowrite_latex_to_unicode(re.sub(r'\s+', ' ', str(txt or '')).strip())
    if not s:
        return ''
    s = _scrub_cowrite_common_prefixes(s)
    # Trim chained numeric evaluations (e.g. 'P = U²/R = 0.75W' or 'Ek = 1/2 mv² = 16J') while keeping symbolic LHS
    s = re.sub(
        r'([=＝≈]\s*[^=＝≈，。；,;]*?)\s*[=＝≈]\s*[-−－±]?(?:\d|[√π])[^，。；,;]*',
        lambda m: m.group(1).strip() if re.search(r'[A-Za-zπΔΩ]', m.group(1)) and not re.search(r'\d+(?:\.\d+)?\s*(?:W|V|A|J|N|Ω|cm|mm|kg)\b', m.group(1)) else '= ？',
        s,
    )

    _unit_sfx_re = re.compile(
        r'^\s*(?:Pa|Hz|kW|kWh|cm|mm|dm|km|kg|mg|mL|mol|Ah|[WVJANgLs](?![A-Za-z0-9²³₀-₉⁰-⁹]|\s*[×·*]?\s*[A-Za-zπ(√])|m(?!/|[A-Za-z0-9₀-₉⁰-⁹]|\s*[×·*]?\s*[A-Za-z²³π(√])|m/s|g/cm|[瓦伏安欧焦牛帕克米秒度])'
    )
    _sym_expr_re = re.compile(r'^\s*(?:[×·*]\s*)?(?:[A-Za-zπ(][A-Za-z0-9₀-₉⁰-⁹²³]*|[√])')
    _trig_id_re = re.compile(r'(?:sin|cos|tan|cot)\s*(?:0|30|45|60|90)°\s*$')
    _const_id_re = re.compile(r'(?:1\s*(?:kWh|m/s|g/cm³|atm|标准大气压)|c水|ρ水|g)\s*$')

    def _keep_or_scrub_eq(rx_pat: str, text_in: str) -> str:
        def _repl(m):
            prefix = text_in[:m.start()]
            suffix = text_in[m.end():]
            if _trig_id_re.search(prefix) or _const_id_re.search(prefix):
                return m.group(0)
            if _sym_expr_re.match(suffix) and not _unit_sfx_re.match(suffix):
                return m.group(0)
            return '= ？'
        return re.sub(rx_pat, _repl, text_in)

    s = _keep_or_scrub_eq(
        r'[=＝≈]\s*[（(]\s*[-−－+±]?(?:\d|[√π])[^()（）=]{1,24}[)）](?:\s*/\s*\d+(?:\.\d+)?)?',
        s,
    )
    s = _keep_or_scrub_eq(
        r'[=＝≈]\s*(?!0(?![.\d]))[-−－±]?(?:\d+(?:\.\d+)?(?:\s*[√π]\d*)?(?:/\d+)?%?|(?:\d*\s*)?[√π]\d*(?:/\d+)?)',
        s,
    )
    s = re.sub(
        r'(?<![A-Za-z0-9_])[-−－+]?(?:\d+(?:\.\d+)?(?:/\d+)?|(?:\d*\s*)?[√π]\d*(?:/\d+)?)\s*([<>＜＞≤≥])\s*([a-zA-Z])\s*([<>＜＞≤≥])\s*[-−－+]?(?:\d+(?:\.\d+)?(?:/\d+)?|(?:\d*\s*)?[√π]\d*(?:/\d+)?)',
        r'？ \1 \2 \3 ？',
        s,
    )
    s = re.sub(
        r'(?<![A-Za-z0-9_Δ+\-−*/^²³])(?<![+\-−*/]\s)([a-zA-Z])\s*([<>＜＞≤≥])\s*(?!0(?![.\d]))[-−－+]?(?:\d+(?:\.\d+)?(?:/\d+)?|(?:\d*\s*)?[√π]\d*(?:/\d+)?)',
        r'\1 \2 ？',
        s,
    )
    s = re.sub(
        r'(?<![A-Za-z0-9_Δ+\-−*/^²³])(?<![+\-−*/]\s)([a-zA-Z])\s*≠\s*(?!0(?![.\d]))[-−－+]?(?:\d+(?:\.\d+)?(?:/\d+)?|(?:\d*\s*)?[√π]\d*(?:/\d+)?)',
        r'\1 ≠ ？',
        s,
    )
    s = re.sub(r'^[，。；、：:\s]+', '', s).strip()
    return s[:max_len]


def _norm_teach_mode(val) -> str:
    s = str(val or '').strip().lower()
    return 'diagnose_draft' if s in ('diagnose_draft', 'draft', 'diagnose') else 'standard'


_NON_ENG_MATH_TOKENS = {
    'cm', 'mm', 'dm', 'km', 'kg', 'mg', 'ml', 'kw', 'kwh', 'pa', 'hz',
    'rt', 'ab', 'bc', 'cd', 'ad', 'ac', 'bd', 'ae', 'be', 'ce', 'de',
    'af', 'bf', 'cf', 'df', 'ef', 'fg', 'gh', 'mn', 'pq',
    'oa', 'ob', 'oc', 'od', 'oe', 'of', 'og', 'oh', 'om', 'on', 'op', 'oq',
    'abc', 'acb', 'bac', 'bca', 'cab', 'cba', 'abd', 'abe', 'acd', 'ace', 'ade',
    'bcd', 'bce', 'bde', 'cde', 'bdc', 'dfc', 'deb', 'aec', 'aeb', 'afc', 'afb',
    'ode', 'obd', 'oce', 'oed', 'odc', 'oeb', 'oab', 'obc', 'oca', 'oad', 'oae',
    'aob', 'aoc', 'boc', 'bod', 'cod', 'doe', 'eof', 'abo', 'aco', 'bco', 'aho', 'abcd', 'efgh',
    'sas', 'asa', 'aas', 'sss', 'hl', 'rhs',
    'sin', 'cos', 'tan', 'cot', 'min', 'max', 'mod', 'gcd', 'lcm', 'log', 'ln',
    'h2o', 'co2', 'o2', 'h2', 'n2', 'hcl', 'naoh', 'caco3', 'h2so4', 'mno2', 'kmno4', 'cuso4', 'fe2o3', 'nacl', 'ph',
}

_MATH_GEO_CUE_RE = re.compile(
    r'[△∠∥⊥≌∽▱⊙°±√∵∴π]|三角形|四边形|五边形|六边形|多边形|平行四边形|梯形|矩形|菱形|正方形|长方形|'
    r'圆锥|圆柱|扇形|平分线|辅助线|中垂线|垂直平分|对角线|斜边|直角边|等腰|等边|全等|相似|勾股|'
    r'周长|面积|体积|坐标|象限|抛物线|双曲线|一次函数|二次函数|反比例|方程|不等式|代数式|因式分解|有理数|实数|求证|证明|试判定'
)

_ENG_CUE_RE = re.compile(
    r'英语|英文|语法|时态|现在完成时|一般过去时|一般现在时|过去进行时|被动语态|'
    r'定语从句|宾语从句|状语从句|完形填空|选词填空|主谓一致|单复数|介词|冠词|词形|短文填空|句型转换'
)


_EXPLICIT_ESSAY_CRITIQUE_RE = re.compile(
    r'【作文升格精批】|【(?:英语|语文)?作文批改】|(?:批改|修改|润色|升格)[^。！？\n]{0,16}(?:作文|习作|短文|段落)|作文精批|作文批改|作文草稿|习作草稿|'
    r'看看我写的(?:作文|段落|短文|开头|结尾|英语作文)|英语作文批改',
    re.I,
)


def detect_writing_task(question: str = '') -> str:
    '''Detect Chinese composition or English guided writing tasks (strictly Socratic scaffold, NEVER ghostwrite!).'''
    txt = str(question or '').strip()
    if not txt or _EXPLICIT_ESSAY_CRITIQUE_RE.search(txt):
        return ''
    if re.search(
        r'书面表达|英语作文|英文短文|写一封.*英文信|Write a (?:short )?(?:passage|letter|email|composition|speech)|不少于\s*\d+\s*词',
        txt, re.I,
    ):
        return 'english_essay'
    if re.search(
        r'【[^】]{0,8}(?:作文|习作)】|单元习作|习作题目|习作要求|作文题目|作文要求|'
        r'半命题作文|命题作文|材料作文|话题作文|以[“"《][^”"》]+[”"》]为题|'
        r'(?:题目|文题)[：:]\s*[《“"][^》”"]+[》”"][\s\S]{0,160}(?:不少于\s*\d+\s*字|习作|写具体|真情实感|感情真挚)|'
        r'(?:写一篇|结构完整[^。！？\n]{0,20}|感情真挚[^。！？\n]{0,20}|字数)不少于\s*\d{3,4}\s*字|'
        r'写一篇记叙文|自拟题目.*写一篇|作文审题|选材立意|读后感.*写一篇',
        txt,
    ):
        return 'chinese_essay'
    return ''


def detect_polya_reduction_candidate(question: str = '') -> bool:
    '''Detect pattern-finding, recurrence, or large-index/series math problems ideal for Polya n=1,2,3 toy-case reduction.'''
    q = str(question or '').strip()
    if not q or is_essay_critique_request(q) or detect_writing_task(q) or detect_subject(q) != 'math':
        return False
    return bool(re.search(
        r'第\s*(?:[2-9]\d+|\d{3,}|n)\s*(?:个|项|幅|行|排|层|次|步|组)|'
        r'找规律|火柴棒|裂项|按此规律|照这样(?:排|摆|画|算)下去|'
        r'(?:[+＋\-−×÷^]\s*202[4-9](?!\s*[年·]))|(?:202[4-9]\s*(?:次方|的幂))|'
        r'(?:[\d)）a-zA-Z]\s*[+＋\-−×,，、]\s*(?:……|\.{3,})\s*[+＋\-−×,，、]?\s*[\d(（a-zA-Z])',
        q, re.I,
    ))


def is_essay_critique_request(question: str = '', teach_mode: str = 'standard') -> bool:
    '''Detect requests to critique/upgrade a student's own Chinese/English composition or subjective paragraph draft.'''
    txt = str(question or '').strip()
    if not txt:
        return False
    if _EXPLICIT_ESSAY_CRITIQUE_RE.search(txt):
        return True
    if _norm_teach_mode(teach_mode) == 'diagnose_draft':
        if detect_writing_task(txt):
            return True
        punct_cnt = sum(txt.count(p) for p in ('，', '。', ',', '.', '！', '!'))
        has_exam_or_sci_ask = bool(re.search(
            r'[=＝÷×+−*/∥⊥△∠°℃%Ω？?]|(?<=\d)\s*-\s*(?=\d)|_{2,}|\(\s*\)|（\s*）|(?:^|[\s\n])[A-Da-d]\s*[.．、]|'
            r'求[出得证]?|多少|几[个只本棵元米厘米分米千米秒度份倍天人辆次步]|哪一步(?:算|写|做)?错了|算[得的]对不对|'
            r'请(?:判断|选择|赏析|分析|简述|概括|回答|指出|说明|证明|写出)|为什么|如何变化|怎样变化|好在哪里|'
            r'哪端|哪个|哪项|哪一项|正确的是|错误的是|不正确|不符合题意|'
            r'阅读.*(?:短文|自然段|选文|材料)|加点[字词]|如图所示|如图[，,\s]|已知[：:，]|'
            r'电路|电流|电压|电阻|浮力|压强|凸透镜|化学方程式|集气瓶|洗气瓶|等高线|经纬网|光合作用|呼吸作用|'
            r'鸦片战争|洋务运动|辛亥革命|道德与法治|(?:H2O|H₂O|CO2|CO₂|H2SO4|H₂SO₄|HCl|NaOH|CaCO3)',
            txt,
        ))
        if len(txt) >= 24 and punct_cnt >= 2 and not has_exam_or_sci_ask:
            return True
    return False


def detect_calc_microscope(question: str = '') -> dict:
    '''Detect vertical decimal arithmetic alignment or equation transpose/clearing-denominator problems.'''
    q = str(question or '').strip()
    if not q or is_essay_critique_request(q) or detect_writing_task(q):
        return None
    subj_cm = detect_subject(q)
    is_func_graph = bool(re.search(r'函数|抛物线|双曲线|象限|对称轴|顶点坐标', q)) and not re.search(r'解.*方程', q)
    if subj_cm == 'math' and not is_func_graph and re.search(r'解.*方程|方程.*的解|(?:^|[^\w])(?:\d*x|\(x[+\-]\d+\))\s*[+\-−*/=＝]', q, re.I) and re.search(r'[=＝]|解.*方程', q):
        has_frac = bool(re.search(r'[xXyYzZ0-9)）]\s*/\s*[2-9]|分母|分数方程|分式方程', q))
        return {
            'kind': 'eq_transpose',
            'badge': '🔬 方程移项去分母显微镜' if has_frac else '🔬 方程移项变号显微镜',
            'rule': '移项跨过等号必翻红变号（+ ↔ −）；去分母时常数项绝不漏乘最小公倍数！' if has_frac else '任何项跨过等号「=」这座桥，正负号必须立刻反转（+ 变 −，− 变 +）！',
        }
    q_no_sec = re.sub(r'^\s*\d+\.\d+(?=\s)', '', q)
    dec_hits = re.findall(r'\d+\.\d+', q_no_sec)
    has_dec_op = bool(re.search(r'\d+\.\d+\s*[+\-−＋－×÷*/]|[+\-−＋－×÷*/]\s*\d+\.\d+', q_no_sec))
    has_geo_ctx = bool(re.search(r'函数|坐标|π|3\.14|扇形|圆|速度.*倍|海拔|△|∠|平行|垂直|全等|相似|勾股|长方形|正方形|梯形|三角形|面积|周长|体积', q_no_sec))
    if re.search(r'竖式|列竖式|小数.*(?:加|减|乘|除|计算)', q_no_sec) or (
        subj_cm == 'math'
        and not has_geo_ctx
        and (has_dec_op or len(dec_hits) >= 2)
    ):
        is_mul_div = bool(re.search(r'小数.*(?:乘|除)|\d+\.\d+\s*[×÷*/]', q))
        return {
            'kind': 'decimal_vcalc',
            'badge': '🔢 小数竖式对齐显微镜',
            'rule': '小数乘除先按整数末位对齐再定小数点位置！' if is_mul_div else '小数加减列竖式：必须让「小数点」上下串成一条垂直红线，切勿硬把末位对齐！',
        }
    return None


def detect_geometry_model(question: str = '') -> dict:
    '''Detect 10 classic elementary & junior-high geometry models and return a ZERO-SPOILER skeleton atlas card.'''
    q = str(question or '').strip()
    if not q or is_essay_critique_request(q) or detect_writing_task(q) or detect_subject(q) not in ('math', 'physics'):
        return None
    models = []
    if re.search(r'凹四边形|飞镖模型|凹角', q):
        models.append({
            'id': 'dart_concave',
            'badge': '🧭 飞镖(凹四边形)模型',
            'title': '几何母模型图谱：凹四边形「飞镖模型」',
            'mantra': '条件反射辅助线：连接顶角 A 与凹顶点 D 并延长至 E（或连接底边 BC 封口）！',
            'principle': '底层骨相：延长线把凹角劈成左右两个三角形外角 ⇒ 凹角 ∠BDC 恒等于三个尖角之和（∠A + ∠B + ∠C）！',
            'draw': sanitize_draw_ops([
                {'op': 'polygon', 'pts': [[350, 62], [170, 315], [350, 222], [530, 315]], 'role': 'main'},
                {'op': 'aux_line', 'x1': 350, 'y1': 62, 'x2': 350, 'y2': 345, 'label': '延长劈双外角'},
                {'op': 'point', 'x': 350, 'y': 62, 'label': 'A', 'pos': 'top'},
                {'op': 'point', 'x': 170, 'y': 315, 'label': 'B', 'pos': 'left'},
                {'op': 'point', 'x': 530, 'y': 315, 'label': 'C', 'pos': 'right'},
                {'op': 'point', 'x': 350, 'y': 222, 'label': 'D(∠A+∠B+∠C)', 'pos': 'right'},
            ]),
        })
    elif re.search(r'树叶形|叶形|扇形.*重叠|(?:圆弧|四分之一圆|¼圆).*重叠.*阴影|正方形.*圆弧.*阴影', q):
        models.append({
            'id': 'leaf_inclusion',
            'badge': '🧭 树叶容斥割补模型',
            'title': '几何母模型图谱：双扇形重叠「树叶模型」',
            'mantra': '条件反射辅助线：连接叶尖对角线 AC 劈成两片对称弓形（或用容斥原理直接重叠相减）！',
            'principle': '底层骨相：半片叶 = ¼圆扇形 − 等腰直角△；容斥秒杀式：整片叶 = 2 × S扇形 − S正方形！',
            'draw': sanitize_draw_ops([
                {'op': 'rect', 'x': 230, 'y': 65, 'w': 220, 'h': 220, 'role': 'main', 'label': '边长a'},
                {'op': 'arc', 'x': 230, 'y': 285, 'r': 220, 'a1': -90, 'a2': 0, 'label': '扇形1'},
                {'op': 'arc', 'x': 450, 'y': 65, 'r': 220, 'a1': 90, 'a2': 180, 'label': '扇形2'},
                {'op': 'aux_line', 'x1': 230, 'y1': 65, 'x2': 450, 'y2': 285, 'label': '连叶尖对角线'},
            ]),
        })
    elif re.search(r'将军饮马|最短路径|(?:PA|PB|MA|MB|PC|PD)\s*\+\s*(?:PA|PB|MA|MB|PC|PD).*(?:最小|最短)|周长最小.*(?:动点|对称)', q):
        models.append({
            'id': 'general_drink_horse',
            'badge': '🧭 将军饮马对称模型',
            'title': '几何母模型图谱：轴对称「将军饮马最短路径」',
            'mantra': '条件反射辅助线：作定点 A 关于动点所在直线 l 的「对称镜像点 A′」，连接 A′B 交直线于 P！',
            'principle': '底层骨相：轴对称实现 PA = PA′ 化折为直，利用「两点之间线段最短」一步锁定极值点！',
            'draw': sanitize_draw_ops([
                {'op': 'line', 'x1': 110, 'y1': 225, 'x2': 590, 'y2': 225, 'role': 'main', 'label': '动点所在直线 l'},
                {'op': 'point', 'x': 210, 'y': 115, 'label': '定点A', 'pos': 'top'},
                {'op': 'point', 'x': 210, 'y': 335, 'label': '对称点A′', 'pos': 'bottom'},
                {'op': 'point', 'x': 500, 'y': 95, 'label': '定点B', 'pos': 'top'},
                {'op': 'aux_line', 'x1': 210, 'y1': 115, 'x2': 210, 'y2': 335, 'label': '作垂直对称'},
                {'op': 'aux_line', 'x1': 210, 'y1': 335, 'x2': 500, 'y2': 95, 'label': '连A′B得最短'},
            ]),
        })
    elif re.search(r'猪蹄模型|铅笔头模型|(?:平行|∥).*(?:拐点|折角|折线)|AB\s*∥\s*CD.*∠E', q):
        models.append({
            'id': 'parallel_pig_foot',
            'badge': '🧭 平行线拐点模型',
            'title': '几何母模型图谱：平行线间折角「过拐点作平行」',
            'mantra': '条件反射辅助线：凡见平行线中间夹折角拐点 E，立刻「过拐点 E 作已知平行线的平行线 EF」！',
            'principle': '底层骨相：一条平行线把拐角拆成上下两半，分别利用内错角相等（猪蹄型 ∠E=∠B+∠D）或同旁内角互补秒通！',
            'draw': sanitize_draw_ops([
                {'op': 'line', 'x1': 140, 'y1': 90, 'x2': 560, 'y2': 90, 'role': 'main', 'label': 'AB'},
                {'op': 'line', 'x1': 140, 'y1': 310, 'x2': 560, 'y2': 310, 'role': 'main', 'label': 'CD'},
                {'op': 'line', 'x1': 230, 'y1': 90, 'x2': 360, 'y2': 200, 'role': 'main', 'label': ''},
                {'op': 'line', 'x1': 250, 'y1': 310, 'x2': 360, 'y2': 200, 'role': 'main', 'label': ''},
                {'op': 'aux_line', 'x1': 160, 'y1': 200, 'x2': 550, 'y2': 200, 'label': '过拐点E作EF∥AB'},
                {'op': 'point', 'x': 360, 'y': 200, 'label': 'E', 'pos': 'right'},
            ]),
        })
    elif re.search(r'一线三等角|一线三垂直|K字模型', q):
        models.append({
            'id': 'k_three_angles',
            'badge': '🧭 一线三等角(K字)模型',
            'title': '几何母模型图谱：「一线三等角 / K字全等相似」',
            'mantra': '条件反射辅助线：过斜放直角顶点所在直线的两端点向该直线作垂线，构造左右双直角三角形！',
            'principle': '底层骨相：同一直线上平角 180° 减中间直角得「左锐角 + 右锐角 = 90°」，同角的余角相等秒证 △全等/相似！',
            'draw': sanitize_draw_ops([
                {'op': 'line', 'x1': 110, 'y1': 305, 'x2': 590, 'y2': 305, 'role': 'main', 'label': '一线三直角底线'},
                {'op': 'polygon', 'pts': [[170, 165], [170, 305], [340, 305]], 'role': 'main'},
                {'op': 'polygon', 'pts': [[340, 305], [480, 305], [480, 135]], 'role': 'aux'},
                {'op': 'right_angle', 'x': 170, 'y': 305, 'dx': 1, 'dy': -1, 'size': 14},
                {'op': 'right_angle', 'x': 480, 'y': 305, 'dx': -1, 'dy': -1, 'size': 14},
                {'op': 'point', 'x': 340, 'y': 305, 'label': '90°顶点', 'pos': 'bottom'},
            ]),
        })
    elif re.search(r'手拉手|共顶点.*(?:等边三角形|等腰直角|正方形).*旋转', q):
        models.append({
            'id': 'hand_in_hand',
            'badge': '🧭 共顶点手拉手模型',
            'title': '几何母模型图谱：共顶点旋转「手拉手模型」',
            'mantra': '条件反射辅助线：找准公共顶点 O，把大图形左腰连小图形左腰、大图形右腰连小图形右腰！',
            'principle': '底层骨相：两顶角相等同加（或同减）中间公共角，立刻由 SAS 证出旋转全等，拉手线段必相等！',
            'draw': sanitize_draw_ops([
                {'op': 'polygon', 'pts': [[310, 260], [160, 260], [235, 130]], 'role': 'main', 'label': '左等边△'},
                {'op': 'polygon', 'pts': [[310, 260], [490, 260], [400, 104]], 'role': 'main', 'label': '右等边△'},
                {'op': 'aux_line', 'x1': 160, 'y1': 260, 'x2': 400, 'y2': 104, 'label': '左手拉左手'},
                {'op': 'aux_line', 'x1': 235, 'y1': 130, 'x2': 490, 'y2': 260, 'label': '右手拉右手'},
                {'op': 'point', 'x': 310, 'y': 260, 'label': '共顶点O', 'pos': 'bottom'},
            ]),
        })
    elif re.search(r'沙漏模型|蝴蝶模型|梯形.*对角线.*(?:交于|面积)|平行.*对角线.*面积比', q):
        models.append({
            'id': 'hourglass_butterfly',
            'badge': '🧭 沙漏/蝴蝶模型',
            'title': '几何母模型图谱：平行对角线「沙漏与蝴蝶模型」',
            'mantra': '条件反射视角：紧盯上下平行底边夹住的交叉对角线，上下为沙漏相似，左右为等积蝴蝶双翼！',
            'principle': '底层骨相：上下 △ 面积比 = 平行底边比的平方；左右蝴蝶两翼面积永远相等（同底等高减公共块）！',
            'draw': sanitize_draw_ops([
                {'op': 'polygon', 'pts': [[230, 100], [450, 100], [530, 305], [150, 305]], 'role': 'main'},
                {'op': 'aux_line', 'x1': 230, 'y1': 100, 'x2': 530, 'y2': 305, 'label': ''},
                {'op': 'aux_line', 'x1': 450, 'y1': 100, 'x2': 150, 'y2': 305, 'label': ''},
                {'op': 'poly_fill', 'pts': [[230, 100], [340, 175], [150, 305]], 'role': 'accent'},
                {'op': 'poly_fill', 'pts': [[450, 100], [340, 175], [530, 305]], 'role': 'solve'},
                {'op': 'point', 'x': 340, 'y': 175, 'label': 'O(左翼=右翼)', 'pos': 'top'},
            ]),
        })
    elif re.search(r'一半模型|(?:长方形|矩形|平行四边形).*(?:边上一点|内一点|阴影.*三角形).*面积', q):
        models.append({
            'id': 'half_area',
            'badge': '🧭 等底等高一半模型',
            'title': '几何母模型图谱：长方形/平行四边形「一半模型」',
            'mantra': '条件反射辅助线：若点在内部则过该点作边的平行线分割；若顶点在对边上则直接抓同底等高！',
            'principle': '底层骨相：三角形与平行四边形同底等高时，无论对边顶点 P 滑到哪里，S△ 永远等于外框面积的一半！',
            'draw': sanitize_draw_ops([
                {'op': 'rect', 'x': 160, 'y': 105, 'w': 380, 'h': 195, 'role': 'main', 'label': '外框面积 S'},
                {'op': 'poly_fill', 'pts': [[160, 300], [540, 300], [390, 105]], 'role': 'solve'},
                {'op': 'polygon', 'pts': [[160, 300], [540, 300], [390, 105]], 'role': 'aux', 'label': '阴影恒占 ½S'},
                {'op': 'point', 'x': 390, 'y': 105, 'label': 'P(任意滑动)', 'pos': 'top'},
            ]),
        })
    elif re.search(r'(?:Rt△|直角三角形|坐标原点).*斜边.*高|∠[A-Z]\s*=\s*90.*斜边.*高', q):
        models.append({
            'id': 'pythag_equal_area',
            'badge': '🧭 斜边高等面积模型',
            'title': '几何母模型图谱：直角三角形「斜边高等面积模型」',
            'mantra': '条件反射辅助线：过直角顶点向斜边作垂线 h，立刻列「两直角边积 = 斜边 × 斜边高」！',
            'principle': '底层骨相：同一三角形面积不变 ½ab = ½ch ⇒ h = (a×b) ÷ c，且垂线分出的两个小Rt△与原△母子相似！',
            'draw': sanitize_draw_ops([
                {'op': 'polygon', 'pts': [[180, 95], [180, 305], [510, 305]], 'role': 'main'},
                {'op': 'right_angle', 'x': 180, 'y': 305, 'dx': 1, 'dy': -1, 'size': 15},
                {'op': 'aux_line', 'x1': 180, 'y1': 305, 'x2': 286, 'y2': 162, 'label': 'h = ab÷c'},
                {'op': 'point', 'x': 180, 'y': 95, 'label': 'a', 'pos': 'left'},
                {'op': 'point', 'x': 350, 'y': 305, 'label': 'b', 'pos': 'bottom'},
            ]),
        })
    elif re.search(r'(?:梯形|三角形|△|四边形|平行四边形).*(?:30\s*[°度]|45\s*[°度]|60\s*[°度])', q):
        models.append({
            'id': 'special_angle_alt',
            'badge': '🧭 特殊角作高模型',
            'title': '几何母模型图谱：见 30°/45°/60°「作高构造 Rt△ 模型」',
            'mantra': '条件反射辅助线：绝不画线劈碎特殊角！过特殊角对面的顶点向角的一边作垂线构造直角三角形！',
            'principle': '底层骨相：30°角所对直角边 = 斜边一半（1 : √3 : 2）；45°角构造等腰直角三角形（两直角边相等）！',
            'draw': sanitize_draw_ops([
                {'op': 'line', 'x1': 120, 'y1': 300, 'x2': 540, 'y2': 300, 'role': 'main', 'label': '底边'},
                {'op': 'line', 'x1': 340, 'y1': 135, 'x2': 120, 'y2': 300, 'role': 'main', 'label': '斜腰c'},
                {'op': 'aux_line', 'x1': 340, 'y1': 135, 'x2': 340, 'y2': 300, 'label': '作垂线 h=½c'},
                {'op': 'right_angle', 'x': 340, 'y': 300, 'dx': -1, 'dy': -1, 'size': 15},
                {'op': 'arc', 'x': 120, 'y': 300, 'r': 38, 'a1': -36, 'a2': 0, 'label': '30°'},
            ]),
        })
    return models[0] if models else None


def compute_classical_pause_guide(verse: str) -> str:
    '''Add rhythmic reading pause slashes (e.g. 2/2/3 for 7-char, 2/3 for 5-char) for classical Chinese poetry/prose.'''
    s = str(verse or '').strip()
    if not s:
        return ''
    clauses = re.split(r'([，。！？；,!;?])', s)
    out = []
    for part in clauses:
        if not part:
            continue
        if re.fullmatch(r'[，。！？；,!;?]', part):
            out.append(part)
            continue
        c_only = re.sub(r'\s+', '', part)
        if len(c_only) == 7 and re.fullmatch(r'[\u4e00-\u9fa5]{7}', c_only):
            out.append(f'{c_only[:2]} / {c_only[2:4]} / {c_only[4:]}')
        elif len(c_only) == 5 and re.fullmatch(r'[\u4e00-\u9fa5]{5}', c_only):
            out.append(f'{c_only[:2]} / {c_only[2:]}')
        elif len(c_only) == 6 and re.fullmatch(r'[\u4e00-\u9fa5]{6}', c_only):
            if c_only[0] in '无惟唯虽若盖夫' and c_only[3] == '之':
                out.append(f'{c_only[0]} / {c_only[1:3]} / {c_only[3:]}')
            else:
                out.append(f'{c_only[:2]} / {c_only[2:4]} / {c_only[4:]}')
        elif len(c_only) == 4 and re.fullmatch(r'[\u4e00-\u9fa5]{4}', c_only):
            if c_only[1] in '不无未莫岂非乃亦皆俱' and c_only[0] not in '不无未莫岂非有虽惟唯斯':
                out.append(f'{c_only[0]} / {c_only[1:]}')
            else:
                out.append(f'{c_only[:2]} / {c_only[2:]}')
        else:
            out.append(c_only)
    return ''.join(out)


def extract_read_aloud_target(question: str = '', steps: list = None) -> dict:
    '''Extract target English sentence or Classical Chinese verse for Step-4 Read-Aloud & Rhythm Coaching (ONLY called when solved!).'''
    q = str(question or '').strip()
    if not q or is_essay_critique_request(q) or detect_writing_task(q):
        return None
    subj = detect_subject(q)
    real_steps = [s for s in (steps or []) if isinstance(s, dict) and int(s.get('step') or 0) > 0]
    s3 = next((s for s in real_steps if int(s.get('step') or 0) == 3), {})
    s4 = real_steps[-1] if real_steps else {}

    if subj == 'english' and not detect_writing_task(q):
        ans_word = ''
        for st_cand in reversed(real_steps):
            qz_c = (st_cand or {}).get('quiz') or {}
            if qz_c.get('opts') and 0 <= int(qz_c.get('ans') or 0) < len(qz_c['opts']):
                raw_opt = str(qz_c['opts'][int(qz_c.get('ans') or 0)]).strip()
                raw_opt = re.sub(r'^[ABCabc]\s*[.、．)\uff09]\s*', '', raw_opt)
                raw_opt = re.sub(r'^(?:应?该?(?:填|选|用|改为)|填上?)\s*', '', raw_opt)
                m_en = re.match(r'^([A-Za-z][A-Za-z\s\'\-]{0,28}?)(?:\s*[（(—：:/]|\s*[\u4e00-\u9fa5]|$)', raw_opt)
                if m_en:
                    ans_word = m_en.group(1).strip()
                    break
        # Strip Chinese instructions & leading item numbers from question to isolate English stem
        en_stem = re.sub(r'【[^】]*】', ' ', q)
        en_stem = re.split(r'(?:请判断|请选择|请问|横线处|括号内|翻译|解析)', en_stem)[0].strip()
        has_blank_tok = bool(re.search(r'_{1,}|—+|\(\s*\)|（\s*）', en_stem))
        paren_hint_pat = r'(?:\(|（)\s*[A-Za-z][A-Za-z\s,/\'-]*(?:\)|）)'
        has_paren_hint = bool(re.search(paren_hint_pat, en_stem))
        if has_blank_tok:
            en_stem = re.sub(paren_hint_pat, '', en_stem)
            if ans_word:
                en_stem = re.sub(r'_{1,}|—+|\(\s*\)|（\s*）', f' {ans_word} ', en_stem, count=1)
        elif has_paren_hint and ans_word:
            en_stem = re.sub(paren_hint_pat, f' {ans_word} ', en_stem, count=1)
        en_stem = re.sub(r'[\u4e00-\u9fa5：:]+', ' ', en_stem)
        en_stem = re.sub(r'^\s*(?:\d+\s*[.、．)\uff09]|\(\d+\)|（\d+）)\s*', '', en_stem)
        en_stem = re.sub(r'\s+', ' ', en_stem).strip(' ，。；：,.?!')
        if (len(re.findall(r'[A-Za-z]{2,}', en_stem)) < 3 or '_' in en_stem or (has_paren_hint and not has_blank_tok and not ans_word)) and s4:
            m_s4 = re.search(r'([A-Z][A-Za-z0-9\s,\'\-]{10,90}[.?!]?)', str(s4.get('board') or ''))
            if m_s4 and '_' not in m_s4.group(1) and '(' not in m_s4.group(1):
                en_stem = m_s4.group(1).strip().rstrip('.?!')
        if '(' in en_stem or '（' in en_stem:
            en_stem = re.sub(r'(?:\(|（)\s*([A-Za-z][A-Za-z\s\'-]*?)(?:\s*/[^)）]*)?(?:\)|）)', r' \1 ', en_stem)
            en_stem = re.sub(r'\s+', ' ', en_stem).strip(' ，。；：,.?!')
        if len(re.findall(r'[A-Za-z]{2,}', en_stem)) >= 3 and '_' not in en_stem and not re.search(r'\(\s*\)|（\s*）', en_stem):
            if not en_stem.endswith(('.', '?', '!')):
                en_stem += '.'
            stem_low_toks = set(re.findall(r"[a-z']+", en_stem.lower()))
            ans_low_toks = re.findall(r"[a-z']+", ans_word.lower())
            safe_focus = ans_word[:24] if (ans_low_toks and all(t in stem_low_toks for t in ans_low_toks)) else ''
            return {
                'kind': 'english',
                'badge': '🎙️ 英语原句语感跟读',
                'text': en_stem[:110],
                'display': en_stem[:110],
                'focus': safe_focus,
                'tip': '先点「慢速领读」听清重音与时态词尾，再点「开口跟读」大声读出完整原句！',
            }

    if subj == 'chinese' and re.search(r'古诗|古词|诗句|文言文|古文|炼字|赏析.*[“"「《]|名句|绝句|律诗', q):
        _meta_ban = re.compile(r'手法|角度|情感|主旨|思想|比喻|拟人|生动|形象|描绘|表现|抒发|答案|解析|句意|炼字|结构|呼应|因为|所以|我们|先看|再看|注意|结合|选项|这句|诗中|作者|诗人|通过|写出')
        eye_kws = [m_e.group(1) for m_e in re.finditer(r'[“"「‘\']([\u4e00-\u9fa5]{1,3})[”"」’\']', q)]
        def _strip_inner_emph(s_in: str) -> str:
            s_out = re.sub(r'(?<=[\u4e00-\u9fa5])\s*[“"‘\'「【（(]\s*([\u4e00-\u9fa5]{1,2})\s*[”"’\'」】）)]\s*(?=[\u4e00-\u9fa5])', r'\1', str(s_in or ''))
            return re.sub(r"[‘’'【】]", '', s_out)
        q_norm_v = _strip_inner_emph(q)
        quotes = [
            m.group(1).strip()
            for m in re.finditer(r'[“"「]([\u4e00-\u9fa5，。！？；]{4,90})[”"」]', q_norm_v)
            if len(re.sub(r'[，。！？；]', '', m.group(1))) >= 4 and not _meta_ban.search(m.group(1))
        ]
        if not quotes and isinstance(steps, list):
            step_txts = []
            for st_v in steps:
                if not isinstance(st_v, dict) or int(st_v.get('step') or 0) <= 0:
                    continue
                step_txts.append(_strip_inner_emph(st_v.get('board') or ''))
                for dr_v in (st_v.get('draw') or []):
                    if isinstance(dr_v, dict):
                        step_txts.append(_strip_inner_emph(dr_v.get('text') or dr_v.get('label') or ''))
                step_txts.append(_strip_inner_emph(st_v.get('say') or ''))
            for blob_v in step_txts:
                for m in re.finditer(r'(?<![\u4e00-\u9fa5])([\u4e00-\u9fa5]{5}，[\u4e00-\u9fa5]{5}[。！？]?|[\u4e00-\u9fa5]{7}，[\u4e00-\u9fa5]{7}[。！？]?)(?![\u4e00-\u9fa5])', blob_v):
                    cand_c = m.group(1).strip()
                    if not _meta_ban.search(cand_c):
                        quotes.append(cand_c)
            for blob_v in step_txts:
                for m in re.finditer(r'[“"「]([\u4e00-\u9fa5，。！？；]{5,30})[”"」]', blob_v):
                    cand_v = m.group(1).strip()
                    if len(re.sub(r'[，。！？；]', '', cand_v)) >= 5 and not _meta_ban.search(cand_v):
                        quotes.append(cand_v)
            _prose7_ban = re.compile(r'关注|结合|思考|联系|分析|抓住|品味|想象|理解|体会|感受|观察|回原|原文|诗题|特点|作用|通法|什么|怎样|如何|哪里|为什么')
            for st_v in steps:
                if not isinstance(st_v, dict) or int(st_v.get('step') or 0) <= 0:
                    continue
                cand_lines = [_strip_inner_emph(ln) for ln in str(st_v.get('board') or '').splitlines()]
                for dr_v in (st_v.get('draw') or []):
                    if isinstance(dr_v, dict):
                        cand_lines.append(_strip_inner_emph(dr_v.get('text') or dr_v.get('label') or ''))
                for ln_v in cand_lines:
                    m_ln = re.match(r'^\s*(?:原句[：:]\s*|名句[：:]\s*|诗句[：:]\s*)?([\u4e00-\u9fa5]{5}|[\u4e00-\u9fa5]{7})[。！？]?\s*$', ln_v)
                    if m_ln:
                        c7 = m_ln.group(1)
                        if not _meta_ban.search(c7) and not _prose7_ban.search(c7):
                            quotes.append(c7)
        if quotes:
            verse = max(quotes, key=lambda c: (1 if (eye_kws and any(ek in c for ek in eye_kws)) else 0, len(re.sub(r'[，。！？；]', '', c))))
            if len(re.sub(r'[，。！？；]', '', verse)) > 36:
                couplets = [cp.strip() for cp in re.findall(r'[^。！？；]+[。！？；]?', verse) if len(re.sub(r'[，。！？；]', '', cp)) >= 8]
                if couplets:
                    hit_cp = next((cp for cp in couplets if eye_kws and any(ek in cp for ek in eye_kws)), couplets[0])
                    verse = hit_cp
            paused = compute_classical_pause_guide(verse)
            return {
                'kind': 'classical_cn',
                'badge': '🎙️ 古诗文节奏诵读',
                'text': verse,
                'display': paused,
                'focus': '',
                'tip': '对照「/」停顿节奏字正腔圆诵读一遍，把诗眼关键字读出画面感！',
            }
    return None


def detect_subject(question: str = '', extra_note: str = '', teach_mode: str = 'standard', essay_critique: bool = False) -> str:
    txt = f'{question or ""} {extra_note or ""}'.strip()
    if not txt:
        return 'math'
    has_math_geo = bool(_MATH_GEO_CUE_RE.search(txt)) and not bool(_ENG_CUE_RE.search(txt))
    if essay_critique or is_essay_critique_request(txt, teach_mode):
        en_cnt = len([
            w for w in re.findall(r'[A-Za-z]{2,}', txt)
            if w.lower() not in _NON_ENG_MATH_TOKENS and not (has_math_geo and re.fullmatch(r'(?:Rt)?[A-Z]{1,5}', w))
        ])
        return 'english' if (en_cnt >= 4 or '英语' in txt or '英文' in txt) else 'chinese'
    w_task = detect_writing_task(txt)
    if w_task == 'english_essay':
        return 'english'
    if w_task == 'chinese_essay':
        return 'chinese'
    if re.search(
        r'【[^】]{0,8}化学[^】]{0,6}】|化学方程式|化学反应|配平|质量守恒|化合价|相对分子质量|相对原子质量|溶质质量分数|溶解度|'
        r'中和反应|复分解|置换反应|高锰酸钾|过氧化氢|稀盐酸|稀硫酸|氢氧化钠|碳酸钙|澄清石灰水|'
        r'集气瓶|长进短出|短进长出|洗气|除杂|鉴别物质|'
        r'(?<![A-Za-z])(?:H2O|H₂O|CO2|CO₂|H2SO4|H₂SO₄|HCl|NaOH|CaCO3|CaCO₃|MnO2|MnO₂|KMnO4|CuSO4|Fe2O3)(?![A-Za-z])',
        txt,
    ):
        return 'chemistry'
    if re.search(r'【[^】]{0,8}(?:英语|英文)[^】]{0,6}】', txt):
        return 'english'
    if (
        (re.search(r'【[^】]{0,8}(?:语文|古诗|古文|文言)[^】]{0,6}】', txt) or not _ENG_CUE_RE.search(txt))
        and re.search(
            r'【[^】]{0,8}(?:语文|古诗|古文|文言)[^】]{0,6}】|阅读理解|短文|自然段|修辞手法|比喻|拟人|排比|人物心理|人物形象|思想感情|中心思想|'
            r'概括段意|赏析|表达效果|好在哪里|字.*妙处|加点词|加点字|文言文|古诗|古词|古文|诗句|名句|默写|补写出|绝句|律诗|意象|炼字|'
            r'通假字|古今异义|词类活用|修改病句|病句辨析|成语|字音|字形|仿写句子',
            txt,
        )
    ):
        return 'chinese'
    if re.search(
        r'【[^】]{0,8}物理[^】]{0,6}】|电路|串联|并联|电流表|电压表|电流|电压|电阻|欧姆|Ω|滑动变阻器|小灯泡|'
        r'电功率|额定功率|实际功率|电功|电能|千瓦时|kW·h|kWh|电热水壶|电饭煲|用电器|焦耳定律|比热容|热值|内能|'
        r'电磁铁|通电螺线管|凸透镜|凹透镜|光路|折射角|入射角|平面镜|焦距|实像|虚像|'
        r'受力分析|滑动摩擦|静摩擦|摩擦力|弹簧测力计|浮力|阿基米德|液体压强|大气压|压强|帕斯卡|牛顿第一|'
        r'重力势能|动能|机械效率|滑轮组|动滑轮|定滑轮|杠杆平衡',
        txt,
        re.I,
    ):
        return 'physics'
    if re.search(
        r'【[^】]{0,8}生物[^】]{0,6}】|叶绿体|线粒体|细胞壁|细胞膜|细胞核|显微镜|光合作用|呼吸作用|蒸腾作用|'
        r'血液循环|左心室|右心室|左心房|右心房|肺循环|体循环|反射弧|食物链|生态系统|染色体|基因|消化酶|肺泡|肾小管|肾小球',
        txt,
    ):
        return 'biology'
    if re.search(
        r'【[^】]{0,8}地理[^】]{0,6}】|等高线|经纬网|北纬|南纬|东经|西经|陡崖|鞍部|山脊|山谷|气候类型|'
        r'热带雨林|地中海气候|温带季风|亚热带季风|降水量|板块构造|地球公转|昼夜长短|秦岭|淮河|海拔.*气温',
        txt,
    ):
        return 'geography'
    if re.search(
        r'【[^】]{0,8}历史[^】]{0,6}】|所学历史|历史知识|不平等条约|半殖民地|半封建|鸦片战争|洋务运动|戊戌变法|百日维新|辛亥革命|'
        r'新文化运动|五四运动|抗日战争|解放战争|商鞅变法|秦始皇|汉武帝|唐太宗|科举制|郑和下西洋|虎门销烟|甲午|大清|清政府|'
        r'丝绸之路|工业革命|文艺复兴|新航路开辟|世界大战|历史意义|史料|朝代|君主专制',
        txt,
    ):
        return 'history'
    if re.search(
        r'【[^】]{0,8}(?:道法|道德与法治|政治)[^】]{0,6}】|道德与法治|未成年人保护|宪法.*根本法|依法治国|'
        r'公民基本权利|基本义务|诚实守信.*社会|共同富裕|创新驱动发展|全过程人民民主|民族区域自治|法治观念',
        txt,
    ):
        return 'civics'
    if _ENG_CUE_RE.search(txt):
        return 'english'
    eng_words = [
        w for w in re.findall(r'[A-Za-z]{2,}', txt)
        if w.lower() not in _NON_ENG_MATH_TOKENS and not (has_math_geo and re.fullmatch(r'(?:Rt)?[A-Z]{1,5}', w))
    ]
    if len(eng_words) >= 4:
        return 'english'
    return 'math'


_SAVED_NON_MATH_SUBJECTS = ('physics', 'chemistry', 'english', 'chinese', 'history', 'civics', 'biology', 'geography')


def _resolve_saved_subject(q_txt: str, saved_s: str, teach_mode: str = 'standard', essay_critique: bool = False) -> str:
    sv = str(saved_s or '').strip()
    if not q_txt:
        return sv if (sv in _SAVED_NON_MATH_SUBJECTS or sv == 'math') else 'math'
    det = detect_subject(q_txt, teach_mode=teach_mode, essay_critique=essay_critique)
    if det == 'math' and sv in _SAVED_NON_MATH_SUBJECTS:
        if sv == 'english' and _MATH_GEO_CUE_RE.search(q_txt) and not _ENG_CUE_RE.search(q_txt):
            return 'math'
        return sv
    return det


def detect_reading_traps(question_txt: str) -> list:
    '''Detect high-frequency careless reading traps (unit mismatch, diameter vs radius, reverse question, both-sides/round-trip).'''
    q = str(question_txt or '').strip()
    if not q or is_essay_critique_request(q) or detect_writing_task(q):
        return []
    traps = []

    def _add(kind: str, badge: str, kw: str, tip: str):
        if len(traps) < 3 and not any(t['kind'] == kind for t in traps):
            traps.append({'kind': kind, 'badge': badge, 'kw': kw, 'tip': tip})

    if detect_subject(q) in ('chinese', 'english', 'history', 'civics'):
        m_neg_h = re.search(r'不正确|错误的是|不符合题意|不可能的是', q)
        if m_neg_h:
            _add('reverse_ask', f'⚠️ 选「{m_neg_h.group(0)}」', m_neg_h.group(0), f'审题防坑：看清题干问的是「{m_neg_h.group(0)}」，千万别顺手选成正确项哦！')
        return traps

    # 1. Length / Area / Mass / Time unit mismatches (numeric-prefixed Latin units & grain/verb exclusions)
    len_units = []
    m_km = re.search(r'千米|\d+(?:\.\d+)?\s*km(?![a-zA-Z²³23/])', q, re.I)
    if m_km:
        len_units.append(('千米', m_km.group(0).strip()))
    m_m = re.search(r'(?<![千平方立方厘分毫大玉小糯黑紫红生淘虾纳微])米(?![饭粒粉粥糊桶袋缸线])|\d+(?:\.\d+)?\s*m(?![a-zA-Z²³23/])', q)
    if m_m:
        len_units.append(('米', m_m.group(0).strip()))
    m_dm = re.search(r'分米|\d+(?:\.\d+)?\s*dm(?![a-zA-Z²³23])', q, re.I)
    if m_dm:
        len_units.append(('分米', m_dm.group(0).strip()))
    m_cm = re.search(r'厘米|\d+(?:\.\d+)?\s*cm(?![a-zA-Z²³23])', q, re.I)
    if m_cm:
        len_units.append(('厘米', m_cm.group(0).strip()))
    if len(len_units) >= 2:
        u0, _ = len_units[0]
        u1, kw1 = len_units[1]
        pair = f'{u0}↔{u1}'
        _add('unit_mismatch', f'⚠️ 单位不统一({pair})', kw1, f'审题防坑：题中同时出现了「{u0}」和「{u1}」，动笔列式前一定要先统一单位哦！')

    if ('平方米' in q and ('平方分米' in q or '平方厘米' in q)) or ('cm²' in q and re.search(r'Pa|帕|压强', q)):
        _add('area_unit', '⚠️ 面积单位换算', 'cm²' if 'cm²' in q else ('平方分米' if '平方分米' in q else '平方厘米'), '审题防坑：注意面积单位进率是 100（1m²=100dm²=10000cm²），求压强需化为 m²！')
    has_kg = bool('千克' in q or re.search(r'\d+(?:\.\d+)?\s*kg(?![a-zA-Z/])', q, re.I))
    m_gram = re.search(r'(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百半几每])\s*(?<![千毫])克(?![服难星拉])|\d+(?:\.\d+)?\s*g(?![a-zA-Z/])', q)
    if ('吨' in q and has_kg) or (has_kg and m_gram):
        kw_mass = '千克' if '千克' in q else (m_gram.group(0).strip() if m_gram else '吨')
        _add('mass_unit', '⚠️ 质量单位换算', kw_mass, '审题防坑：注意质量单位不一致（1吨=1000千克，1千克=1000克），先统一再运算！')
    if ('小时' in q and '分钟' in q) or ('km/h' in q and 'm/s' in q):
        _add('time_unit', '⚠️ 时间/速度单位换算', '分钟' if '分钟' in q else 'm/s', '审题防坑：时间与速度单位必须配套（1小时=60分钟，1m/s=3.6km/h）！')
    if re.search(r'kW·h|kWh|千瓦时', q, re.I) and re.search(r'\d+(?:\.\d+)?\s*W(?![a-zA-Z·])|\d+(?:\.\d+)?\s*min(?![a-zA-Z])|分钟', q):
        kw_el = 'kW·h' if 'kW·h' in q else ('kWh' if 'kWh' in q else '千瓦时')
        _add('elec_unit', '⚠️ 电功单位换算(kW与h)', kw_el, '审题防坑：求千瓦时(kW·h)时，功率要先化为千瓦(÷1000)，时间要先化为小时(÷60)！')

    # 2. Diameter vs Radius trap
    if '直径' in q and re.search(r'圆|扇形|圆柱|圆锥|面积|体积|容积|π', q):
        _add('diameter_radius', '⚠️ 给的是直径', '直径', '审题防坑：题目给出的是「直径」，代入圆或扇形面积公式 πr² 前必须先除以 2 求出半径 r！')

    # 3. Reverse / logic / both-sides / round-trip traps
    m_neg = re.search(r'不正确|错误的是|不符合题意|不可能的是', q)
    if m_neg:
        _add('reverse_ask', f'⚠️ 选「{m_neg.group(0)}」', m_neg.group(0), f'审题防坑：看清题干问的是「{m_neg.group(0)}」，千万别顺手选成正确项哦！')
    m_both = re.search(r'两旁|两侧', q)
    if m_both and re.search(r'栽|种|植树|路灯|彩旗|花盆', q):
        _add('both_sides', f'⚠️ 道路{m_both.group(0)}', m_both.group(0), f'审题防坑：注意是小路「{m_both.group(0)}」都要布置，算出一旁数量后最后记得乘 2！')
    m_rt = re.search(r'往返|来回', q)
    if m_rt:
        _add('round_trip', f'⚠️ {m_rt.group(0)}双程', m_rt.group(0), f'审题防坑：注意「{m_rt.group(0)}」包含去和回两趟总路程哦！')
    m_ends_both = re.search(r'(?:两端|两头|首尾)都(?:要)?(?:栽|种|装|安装|插|摆|放)', q)
    if m_ends_both:
        _add('both_ends', '⚠️ 两端都装(段数+1)', m_ends_both.group(0), '审题防坑：首尾两端都要栽/装时，「总数量 = 间隔段数 + 1」，千万别漏加 1！')
    m_ends_none = re.search(r'(?:两端|两头|首尾)都不(?:栽|种|装|安装|插|摆|放)', q)
    if m_ends_none:
        _add('neither_end', '⚠️ 两端不装(段数−1)', m_ends_none.group(0), '审题防坑：首尾两端都不栽/装时，「总数量 = 间隔段数 − 1」！')
    m_saw = re.search(r'(?:锯成|截成|剪成)(?:相等的|同样长的|均匀的)?\s*\d+\s*段', q)
    if m_saw:
        _add('saw_cuts', '⚠️ 锯木段数与次数', m_saw.group(0), '审题防坑：锯木头/剪绳子时「次数 = 段数 − 1」，先画 2 段小图确认！')
    return traps


def detect_motion_sim(question_txt: str) -> dict:
    '''Detect travel/encounter/chase/bridge or moving-point geometry problems for interactive timeline simulation (zero spoiler!).'''
    q = str(question_txt or '').strip()
    if not q or is_essay_critique_request(q) or detect_writing_task(q) or detect_subject(q) not in ('math', 'physics'):
        return None
    if re.search(r'火车|列车|车队', q) and re.search(r'大桥|铁桥|桥|隧道|山洞|涵洞', q):
        return {
            'kind': 'bridge',
            'title': '火车过桥动态推演',
            'a_lbl': '车头',
            'b_lbl': '车尾',
            'r1': 0.72,
            'r2': 0.20,
            'hint': '🎞️ 拖动时间轴盯住车头：从车头上桥(0%)到车尾离桥(100%)，车头走过的总路程 = 桥长 + 车身长！',
        }
    if re.search(r'追上|追及|同向而行|同向开出|去追|落后.*追', q):
        fast_lbl = '快车' if '快车' in q else ('哥哥' if '哥哥' in q else ('甲' if '甲' in q else '快者'))
        slow_lbl = '慢车' if '慢车' in q else ('弟弟' if '弟弟' in q else ('乙' if '乙' in q else '慢者'))
        return {
            'kind': 'chase',
            'title': '同向追及动态推演',
            'a_lbl': fast_lbl,
            'b_lbl': slow_lbl,
            'r1': 0.76,
            'r2': 0.44,
            'gap': 0.32,
            'hint': f'🎞️ 拖动时间轴观察：{fast_lbl}走的路程减去{slow_lbl}走的路程，刚好等于一开始相差的追及距离！',
        }
    if re.search(r'相遇|相向|相对而行|相对开出|迎面|同时从.*出发.*相距', q):
        a_lbl = '客车' if '客车' in q else ('快车' if '快车' in q else ('甲' if '甲' in q else '左端'))
        b_lbl = '货车' if '货车' in q else ('慢车' if '慢车' in q else ('乙' if '乙' in q else '右端'))
        speeds = [float(x) for x in re.findall(r'(\d+(?:\.\d+)?)\s*(?:千米/时|千米/小时|km/h|米/分|米/秒|m/s)', q, re.I)]
        if len(speeds) >= 2 and (speeds[0] + speeds[1]) > 0:
            r1 = round(max(0.22, min(0.78, speeds[0] / (speeds[0] + speeds[1]))), 2)
        else:
            r1 = 0.58
        return {
            'kind': 'meet',
            'title': '相向相遇动态推演',
            'a_lbl': a_lbl,
            'b_lbl': b_lbl,
            'r1': r1,
            'r2': round(1.0 - r1, 2),
            'hint': f'🎞️ 拖动时间轴观察：{a_lbl}与{b_lbl}同时相向出发，100%相遇那一刻两段路程拼起来正好等于全程！',
        }
    if re.search(r'动点\s*[A-Z]?|点\s*[PQM]\s*从.*出发|沿.*每秒.*运动|向终点.*运动', q):
        m_pt = re.search(r'(?:动点|点)\s*([PQM])', q)
        pt_name = m_pt.group(1) if m_pt else 'P'
        return {
            'kind': 'moving_point',
            'title': f'动点 {pt_name}(t) 轨迹推演',
            'a_lbl': pt_name,
            'b_lbl': '',
            'r1': 0.5,
            'r2': 0.5,
            'hint': f'🎞️ 拖动时间 t 观察动点 {pt_name} 运动：先用「速度×t」表示已走线段，再用「总边长−已走」表示剩余线段！',
        }
    return None


def split_multipart_subquestions(question_txt: str) -> dict:
    '''Split multi-part problems with (1)(2)(3) / （1）（2） / ①② into progressive relay sub-questions.'''
    q = str(question_txt or '').strip()
    if len(q) < 28 or is_essay_critique_request(q) or detect_writing_task(q) or re.search(r'(?:^|[\s\n])A\s*[.．、]', q):
        return None
    patterns = [
        (r'(?:^|(?<=[\s。；;：:？?！!，,\n]))(?:（|\()\s*([1-4])\s*(?:）|\))', lambda n: f'第({n})问'),
        (r'(?:^|(?<=[\s。；;：:？?！!，,\n]))([①②③④])', lambda c: f'第{c}问'),
    ]
    cir_to_idx = {'①': 1, '②': 2, '③': 3, '④': 4}
    for pat, lbl_fn in patterns:
        ms = list(re.finditer(pat, q))
        if len(ms) < 2:
            continue
        nums = [cir_to_idx[g] if g in cir_to_idx else (int(g) if g.isascii() and g.isdigit() else 0) for g in (m.group(1) for m in ms)]
        if nums[0] != 1 or nums != list(range(1, len(nums) + 1)):
            continue
        stem_raw_pre = q[:ms[0].start()]
        # Guard against equation-tag circled numbers like '2x + y = 7 ①，x - y = 2 ②'
        if ms[0].group(1) in cir_to_idx and re.search(r'[=＝]\s*[-−]?[A-Za-z0-9.]+\s*$', stem_raw_pre):
            continue
        stem = stem_raw_pre.strip().rstrip('：:；;，,')
        stem = re.sub(
            r'(?:(?<=[。！？!?；;\n])\s*|[，,]\s*)(?:请(?:结合[^：:。！？\n]{0,16})?(?:回答(?:下列问题)?|解答|问|求解|作答)|试(?:回答|求|分析|计算)|回答下列问题|求|问|计算)$',
            '',
            stem,
        ).strip().rstrip('：:；;，,')
        if len(stem) < 8:
            continue
        parts = []
        valid = True
        for i, m in enumerate(ms[:4]):
            p_end = ms[i + 1].start() if i + 1 < len(ms) else len(q)
            raw_after = q[m.end():p_end]
            if re.match(r'^\s*[，,；;。]', raw_after):
                valid = False
                break
            body = raw_after.strip().lstrip('、.．：:').strip()
            if len(body) < 5 or not re.search(r'求|多少|几|为什么|分析|说明|判断|计算|证明|写出|如何|哪|什么|[？?]', body):
                valid = False
                break
            parts.append({'idx': i + 1, 'label': lbl_fn(m.group(1)), 'text': body[:200]})
        if valid and len(parts) >= 2:
            return {'stem': stem[:360], 'parts': parts}
    return None


def detect_clever_calc_tip(expr_or_text: str) -> dict:
    '''Detect mental-math & algebraic shortcuts (25x4, 125x8, near-100, diff-of-squares, common factor) without revealing the result!'''
    s = str(expr_or_text or '').replace('＊', '×').replace('*', '×').replace('／', '÷').replace('/', '÷').replace('－', '-').replace('−', '-')
    if not s:
        return None
    # 1. Difference of squares: a² - b² (guard against decimals & tighter ×/÷ precedence like 3.14 × 10² - 5²)
    for m_sq in re.finditer(r'(?:([×÷])\s*)?(?<![0-9.])(\d+)\s*²\s*-\s*(\d+)\s*²(?![0-9.]|\s*[×÷])', s):
        if not m_sq.group(1) and int(m_sq.group(2)) > int(m_sq.group(3)):
            a, b = m_sq.group(2), m_sq.group(3)
            return {
                'badge': '⚡ 平方差巧算',
                'title': f'发现 {a}² − {b}² 秒杀招',
                'tip': f'不用硬列竖式算大数平方！用平方差公式转化为「({a} + {b}) × ({a} − {b})」，先算括号里的和与差，口算就能秒出！',
            }
    # 2. Golden friend 125 × 8k or 25 × 4k (skip after ÷ or when attached to exponents ²/³)
    for m_mul in re.finditer(r'(?:(÷)\s*)?(?<![0-9.²³])(\d+)\s*×\s*(\d+)(?![0-9.²³])', s):
        if m_mul.group(1):
            continue
        x, y = int(m_mul.group(2)), int(m_mul.group(3))
        if 125 in (x, y):
            other = y if x == 125 else x
            if other >= 8 and other % 8 == 0:
                k = other // 8
                return {
                    'badge': '⚡ 凑整千点金术',
                    'title': '发现好朋友数 125 与 8',
                    'tip': f'别列长竖式！125 的黄金搭档是 8（凑成 1000），把 {other} 拆成「8 × {k}」，先算 125 × 8 再乘 {k}！',
                }
        if 25 in (x, y):
            other = y if x == 25 else x
            if other >= 8 and other % 4 == 0:
                k = other // 4
                return {
                    'badge': '⚡ 凑整百点金术',
                    'title': '发现好朋友数 25 与 4',
                    'tip': f'不用列竖式硬乘！25 的黄金搭档是 4（凑成 100），把 {other} 拆成「4 × {k}」，先凑整百再口算！',
                }
        for near, base, diff_lbl in ((99, 100, '减 1'), (98, 100, '减 2'), (101, 100, '加 1'), (102, 100, '加 2'), (199, 200, '减 1')):
            if near in (x, y) and max(x, y) >= 12:
                other = y if x == near else x
                return {
                    'badge': '⚡ 凑整分配巧算',
                    'title': f'接近整百数 {near} 巧拆法',
                    'tip': f'看到 {near} 离 {base} 只差一点点！把 {near} 看作「{base} {diff_lbl}」，先用 {other} 乘 {base} 再微调，又快又不易错！',
                }
    # 3. Distributive law common factor: A × C ± B × C (strict numeric & operator-precedence boundaries)
    for m_dist in re.finditer(r'(?:([×÷])\s*)?(?<![0-9.²³])(\d+(?:\.\d+)?)\s*×\s*(\d+(?:\.\d+)?)(?![²³])\s*([+\-])\s*(\d+(?:\.\d+)?)\s*×\s*(\d+(?:\.\d+)?)(?![0-9.²³]|\s*[×÷])', s):
        if m_dist.group(1):
            continue
        _, n1, n2, op_ch, n3, n4 = m_dist.groups()
        common = n1 if n1 in (n3, n4) else (n2 if n2 in (n3, n4) else '')
        if common and float(common) != 1.0:
            return {
                'badge': '⚡ 提取公因数巧算',
                'title': f'发现共同因数 {common}',
                'tip': f'加减号两边都乘了 {common}！先用乘法分配律把「{common}」提出来，先算括号里另外两个数的{("和" if op_ch == "+" else "差")}！',
            }
    # 4. Divide by 5 mental trick (multi-digit > 50 beyond multiplication table, no preceding ÷ or trailing ×/÷/²)
    m_div5 = re.search(r'(?:(÷)\s*)?(?<![0-9.²³])(\d{2,})\s*÷\s*5(?![0-9.²³]|\s*[×÷])', s)
    if m_div5 and not m_div5.group(1) and int(m_div5.group(2)) > 50 and int(m_div5.group(2)) % 10 != 0:
        v = m_div5.group(2)
        return {
            'badge': '⚡ 除5翻倍移点术',
            'title': f'{v} ÷ 5 心算妙招',
            'tip': f'除以 5 相当于「先乘 2，再把小数点往左移一位（除以10）」，试试心算校验一下！',
        }
    return None


def _norm_pedagogy(val) -> str:
    s = str(val or '').strip().lower()
    return s if s in ('elem', 'junior') else 'auto'


def resolve_pedagogy_hint(pedagogy: str, question: str = '') -> str:
    ped = _norm_pedagogy(pedagogy)
    w_task = detect_writing_task(question)
    if w_task == 'chinese_essay':
        return (
            '【学科与学段认知适配：当前为「语文作文审题选材与三段式搭积木启发辅导」！'
            '绝对红线：严禁替孩子代写整篇现成范文（任何一步严禁连续写出超过2句成段正文供照抄）！'
            '第1步黑板用 rect+arrow 画「题目题眼词 → 3个选材切口对比」，quiz 三选一引导避开千篇一律俗套（如雨天发烧背去医院），选出以小见大真细节；'
            '第2步黑板用 rect+brace 画「凤头点题 → 猪肚慢镜头细节(动作/神态/心理) → 豹尾升华」结构积木图，quiz 辨析高潮处详略与描写角度；'
            '第3步 quiz 必须加 "force_choice":true，给出平淡流水账句与细节升格句让孩子三选一挑出最生动表达；'
            '第4步提供 3 个点睛好词金句模具 + 扣题与字数自查清单，鼓励孩子独立动笔成文！】'
        )
    if w_task == 'english_essay':
        return (
            '【学科与学段认知适配：当前为「英语书面表达话题写作三段式搭积木启发辅导」！'
            '绝对红线：严禁直接生成整篇现成英语范文让孩子照抄！'
            '第1步用 rect+arrow 拆解「体裁/人称/时态 + 要点清单」，quiz 三选一锁定正确时态与人称；'
            '第2步用 table 或 rect 搭出 Beginning(引入) → Body(First / What is more / Finally 要点拓展) → Ending(总结倡议) 框架；'
            '第3步 quiz 必须加 "force_choice":true，对比中式散装短句与地道高分句型让孩子三选一升级句子；'
            '第4步汇总 3 个高分衔接词组与拼写/主谓一致/标点考场自查清单！】'
        )
    subj = detect_subject(question)
    if subj == 'chemistry':
        return (
            '【学科与学段认知适配：当前面向初中生进行「初中化学微观与实验可视化启发讲题」！'
            '化学方程式配平/质量守恒题用 circle+text 画出反应前后左右各原子个数对比图，引导找最小公倍数或奇偶配平突破口，第3步让孩子自算关键化学计量数或质量；'
            '气体制取/洗气除杂多功能瓶题用 rect(集气瓶)+长短 line(导管)+point(A/B管口)+arrow(气流) 画实验装置图（若问从哪管进气，可在 opts 明确写“从长导管点A进气”便于点图闯关）！严禁直接报答案！】'
        )
    if subj == 'chinese':
        aud = '小学生' if ped == 'elem' else ('初中生' if ped == 'junior' else '小学生与初中生通用')
        return (
            f'【学科与学段认知适配：当前面向{aud}进行「语文阅读理解/古诗文寻证精读启发讲题」（严禁超纲设未知数 x/y，更严禁一上来给出整段标准答案让孩子照抄）！'
            '黑板画布必须用 rect+arrow+brace+text 画出精炼的「原文证据词 → 手法/画面特征 → 深层情感/主旨」寻证导图（每个标签不超过14字）；'
            '第1步引导回原句锁定关键动词/形容词/意象或修辞描写手法；第2步对比辨析“只答表层字面意思”与“深层人物心理/作者情感”；'
            '第3步 quiz 必须加 "force_choice":true，提供3个采分要素完整度不同的凝练答题骨架让孩子选出满分逻辑链；第4步按“①手法分 ✓ ②特征分 ✓ ③情感分 ✓”拆解总结同类阅读通用公式！】'
        )
    if subj == 'physics':
        return (
            '【学科与学段认知适配：当前面向初中生进行「物理可视化启发讲题」！'
            '力学题第1步先画研究对象(rect/circle)与质心(point)，用带标签的 arrow 画出各力方向（如 G、F支、F拉、f、F浮）；'
            '电学题用 rect/circle/line 画闭合串并联回路并用 arrow 标电流路径、辨析串并联分压分流规律；'
            '光学题画主光轴 line、透镜及焦点 F/2F point 与入射/折射光路 arrow！必须一步一设问，注意统一国际单位，严禁直接报出得数！】'
        )
    if subj == 'biology':
        return (
            '【学科与学段认知适配：当前面向中小学进行「生物结构与生理过程可视化启发讲题」！'
            '黑板必须用 circle/rect/point(标A/B/C部位)+arrow 画出清晰的器官腔室、细胞结构、反射弧或物质能量流向图（若问图上哪个部位，可在 opts 明确写“结构点A”支持直接点图闯关）；'
            '若第3步为概念/生理结论辨析（非纯数字计算），quiz 必须加 "force_choice":true 给3个规范生物学术语选项；严禁一上来直接报出答案！】'
        )
    if subj == 'geography':
        return (
            '【学科与学段认知适配：当前面向初中生进行「地理读图与空间因果启发讲题」！'
            '黑板必须用 circle/arc/line/point(标A/B/C位置)+arrow 画出等高线地形示意、经纬网格、地球公转或季风气旋箭头图（支持点选图上点A/B指认地形或位置）；'
            '若第3步为气候/成因/区域特征判断（非温差或比例尺纯数字计算），quiz 必须加 "force_choice":true 给3个规范地理表述选项；严禁直接报答案！】'
        )
    if subj in ('history', 'civics'):
        sub_lbl = '初中历史材料寻证与时空因果' if subj == 'history' else '初中道法材料解读与多角度规范踩分'
        return (
            f'【学科与学段认知适配：当前面向初中生进行「{sub_lbl}启发讲题」（严禁一上来贴出整段标准答案让孩子死记照抄）！'
            '黑板必须用 rect+arrow+brace+text 画出精炼的「材料关键题眼词 → 时空背景/教材核心原理 → 多角度规范术语」思维导图（每个标签不超过14字）；'
            '第1步引导回材料圈出关键时间/转折/主体词；第2步辨析教材核心观点并排除大白话或张冠李戴误区；'
            '第3步 quiz 必须加 "force_choice":true 提供3个角度完整度不同的规范答题骨架供辨析选择（绝不用数字键盘！）；第4步按“①角度一 ✓ ②角度二 ✓”拆解满分采分点！】'
        )
    if subj == 'english':
        aud = '小学生' if ped == 'elem' else ('初中生' if ped == 'junior' else '小学生与初中生通用')
        return (
            f'【学科与学段认知适配：当前面向{aud}进行「英语语法/句型结构可视化启发讲题」（严禁超纲设未知数 x/y）！'
            '时态题必须在黑板画时间轴(arrow/seg_bar)标出 past / now / future 并高亮时间信号词（如 since 2020、already、ago）；'
            '从句或完形长句用 rect 框出主句与从句结构并用 arrow 指向先行词或连接词！'
            '第1步引导找时间词/连接词题眼，第2步对比易混语法陷阱，第3步 quiz 必须加 "force_choice":true 并给3个纯英文词形/短语选项（绝不用数字键盘填英文词！），第4步朗读完整英文原句并总结语法口诀！】'
        )
    if ped == 'auto' and question:
        if re.search(r'函数|坐标|方程|√|根号|x轴|y轴|全等|相似|勾股|Rt△|抛物线|二次|斜边', question):
            ped = 'junior'
        else:
            ped = 'elem'
    vis_extra = ''
    if re.search(r'二次函数|抛物线|反比例函数|双曲线', question or ''):
        vis_extra = '（本题为函数曲线题：请优先配合 axes 与 curve 平滑采样点原语画出精准抛物线/双曲线及对称轴 aux_line、顶点 point！）'
    elif re.search(r'正方体|长方体|圆柱|立体|展开图|相对面|锯成.*表面积|切.*表面积', question or ''):
        vis_extra = '（本题为立体空间题：请优先使用 cube3d（支持 cut:"x"/"y" 标出新增切面及虚线遮挡棱）或 cylinder 画出清晰三维透视图！）'
    elif re.search(r'分段计费|阶梯水费|阶梯电费|出租车|打折.*满减|方案对比|统计表', question or ''):
        vis_extra = '（本题多条件对比：请优先使用原生 table 表格原语对齐列出各档位/方案条件，待求格填“待求？”！）'
    elif re.search(r'条形统计图|折线统计图|统计图', question or ''):
        vis_extra = '（本题为统计图表题：请优先使用 chart 原语画出柱状或折线对比图！）'
    else:
        cm_info = detect_calc_microscope(question or '')
        if cm_info and cm_info['kind'] == 'decimal_vcalc':
            vis_extra = '（本题涉及小数/竖式位值对齐：请优先配合 vcalc 竖式原语展示小数点上下红线对齐，res 留白填 "□"，严禁提前泄露得数！）'
        elif cm_info and cm_info['kind'] == 'eq_transpose':
            vis_extra = '（本题涉及解方程移项/去分母：请用 rect/arrow 标出项跨过等号“=”时翻红变号 (+↔−) 及去分母常数项不漏乘！）'
    if ped == 'elem':
        return f'【学段认知适配：当前面向小学生（如三年级），严禁使用设未知数 x/y 列方程或超纲初中定理！必须全程使用小学生秒懂的线段图、份数法、假设法或几何割补法直观引导，语言亲切生动！{vis_extra}】'
    if ped == 'junior':
        return f'【学段认知适配：当前面向初中生（如备战中考），请使用规范的代数设元找等量关系列方程、函数图像性质或严谨几何辅助线定理推导，注重通性通法与验根检验！{vis_extra}】'
    return (
        '【学段认知适配：识别原题后请严格按学段匹配教法——若为小学应用题或基础几何题（无函数/根号/全等/勾股/方程字眼），'
        '严禁超纲设未知数 x/y 列方程，必须用线段图/份数法/假设法/割补法引导；若含初中函数/勾股/全等/根式/方程，则用规范定理与方程推导；'
        '若图片为物理题则画受力/电路/光路图引导，若为英语题则画时间轴/句子成分框并在第3步加 "force_choice":true 选词通关！】'
    )
HUB_CFG_PATH = Path(os.environ.get(
    'TUTOR_HUB_CFG',
    '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/data/.config.yaml',
))
MUSIC_CTRL_URL = 'http://127.0.0.1:8091/api/music/control'
CALL_FLAG = Path('/run/user/1000/intercom-call-active')
MIRROR_FLAG = Path('/run/user/1000/uxplay-mirroring-active')


def _media_busy() -> bool:
    return (
        CALL_FLAG.exists()
        or MIRROR_FLAG.exists()
        or MIRROR_FLAG.with_name('uxplay-audio-active').exists()
    )

ALLOWED_ORIGINS = {
    'http://127.0.0.1:8096', 'http://localhost:8096',
    'http://127.0.0.1:8090', 'http://localhost:8090',
    'http://127.0.0.1:8095', 'http://localhost:8095',
}

log = logging.getLogger('tutor')


def _etag_bytes(data: bytes) -> str:
    return '"' + hashlib.sha256(data).hexdigest()[:16] + '"'


def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 24000, channels: int = 1, bits: int = 16) -> bytes:
    if pcm_bytes[:4] == b'RIFF' and pcm_bytes[8:12] == b'WAVE':
        return pcm_bytes
    even_pcm = pcm_bytes[:len(pcm_bytes) & ~1]
    byte_rate = sample_rate * channels * (bits // 8)
    block_align = channels * (bits // 8)
    data_len = len(even_pcm)
    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + data_len, b'WAVE',
        b'fmt ', 16, 1, channels, sample_rate, byte_rate, block_align, bits,
        b'data', data_len,
    )
    return header + even_pcm


_FALLBACK_SILENT_WAV = _pcm_to_wav(b'\x00' * 9600)
_FIRST_TOKEN_TIMEOUT_FAST = 22.0
_FIRST_TOKEN_TIMEOUT_DEEP = 28.0
_FIRST_CONTENT_TIMEOUT_FAST = 52.0
_FIRST_CONTENT_TIMEOUT_DEEP = 64.0


def clean_math_for_tts(text: str) -> str:
    """Convert math/geometry notation into natural spoken Chinese for Qwen3-TTS."""
    s = str(text or '').strip()
    if not s:
        return ''
    s = re.sub(r'[\U0001F300-\U0001FAFF\u26A0\u26A1\u2705\u270D\u270F\u2713\u2714\u2728\u274C\u2753\u2757\uFE0F]+', '', s)
    cir_ref_map = {
        '①': '第一步的结果', '②': '第二步的结果', '③': '第三步的结果',
        '④': '第四步的结果', '⑤': '第五步的结果', '⑥': '第六步的结果',
    }
    s = re.sub(
        r'√\s*([①②③④⑤⑥])',
        lambda m: f"根号下{cir_ref_map[m.group(1)]}",
        s,
    )
    s = re.sub(
        r'([×÷+\-−＋－=＝(（]\s*)([①②③④⑤⑥])',
        lambda m: f"{m.group(1)}{cir_ref_map[m.group(2)]}",
        s,
    )
    s = re.sub(
        r'([①②③④⑤⑥])\s*(?=[×÷+\-−＋－])',
        lambda m: cir_ref_map[m.group(1)],
        s,
    )
    # Convert relative atomic mass clauses (e.g. '相对原子质量：H-1，O-16，Ca-40' -> 'H为1，O为16，Ca为40') before minus handling
    s = re.sub(
        r'(相对原子质量[：:\s]*)([A-Za-z0-9\s,，、;；:：.\-—－−]+)',
        lambda m: m.group(1) + re.sub(r'(?<![A-Za-z])([A-Z][a-z]?)\s*[-—－−:：]\s*(\d+(?:\.\d+)?)', r'\1为\2', m.group(2)),
        s,
    )
    # Convert chemical equation condition arrows/double-equals (e.g. '==MnO₂==', '==△==', '==点燃==', '==') into natural spoken Chinese
    s = re.sub(r'[=＝]{2,}\s*(?:△|Δ)\s*[=＝]{2,}', ' 在加热条件下生成 ', s)
    s = re.sub(
        r'[=＝]{2,}\s*([^=＝\n]{1,14}?)\s*[=＝]{2,}',
        lambda m: f' 在{m.group(1).strip()}条件下生成 ',
        s,
    )
    s = re.sub(r'[=＝]{2,}|═+', ' 生成 ', s)
    _cir_num_map = {'①': '1', '②': '2', '③': '3', '④': '4', '⑤': '5', '⑥': '6'}
    # Consume explicit unit suffixes right after circled numbers first ('①式' -> '第1式', '①问' -> '第1问', '①步' -> '第1步')
    s = re.sub(
        r'([①②③④⑤⑥])\s*([式题问项步])',
        lambda m: f"第{_cir_num_map[m.group(1)]}{m.group(2)}",
        s,
    )
    s = re.sub(
        r'(代入\s*)([①②③④⑤⑥])',
        lambda m: f"{m.group(1)}第{_cir_num_map[m.group(2)]}式",
        s,
    )
    # Algebra equation labels like '2x + y = 7 ①，x - y = 2 ②' -> '第1式 / 第2式'
    if re.search(r'方程|[xXyY]\s*[+＋\-−－=＝]', s):
        s = re.sub(
            r'([=＝]\s*[-−－+]?[A-Za-z0-9.]+\s*)([①②③④])(?=\s*[，,；;。]|$)',
            lambda m: f"{m.group(1)}第{_cir_num_map[m.group(2)]}式",
            s,
        )
    s = re.sub(r'([①②③④⑤⑥])(?=[①②③④⑤⑥])', r'\1、', s)
    s = re.sub(
        r'([①②③④⑤⑥])(?=[、，,：:。；;！？!?和与及两三四各])',
        lambda m: f"第{_cir_num_map[m.group(1)]}步",
        s,
    )
    s = re.sub(
        r'(?<=[按把将由用从跟与和及、])([①②③④⑤⑥])',
        lambda m: f"第{_cir_num_map[m.group(1)]}步",
        s,
    )
    s = re.sub(r'第([1-6])步(?=(?:[、，,]\s*第[1-6]步)*[两三四]步|步)', r'第\1', s)
    # Division with remainder ('47 ÷ 6 = 7……5', '7（条）……5（人）', '7...□') -> '余'
    s = re.sub(
        r'(\d+(?:\s*[（(][\u4e00-\u9fa5A-Za-z]{1,4}[）)])?)\s*(?:……|…|\.{3,})\s*(\d+|□|多少)',
        r'\1余\2',
        s,
    )
    # Multi-term ASCII ratios ('3:2:1' -> '3比2比1') & explicit two-term ratios ('比是 3:2')
    s = re.sub(r'(?<!\d)(\d{1,3})\s*:\s*(\d{1,3})\s*:\s*(\d{1,3})(?!\d)', r'\1比\2比\3', s)
    s = re.sub(r'(?<=[比是比为])(\s*)(\d{1,4})\s*:\s*(\d{1,4})(?![:\d])', r'\1\2比\3', s)
    # Clock times ('8:40' -> '8点40分', '9:00' -> '9点整', '10:05' -> '10点零5分') when time cues exist
    if re.search(r'上午|下午|早上|晚上|中午|凌晨|开始|结束|出发|到达|写完|放学|起床|经过|用时|分钟|小时|时刻|钟面', s):
        def _fmt_clock_tts(mc: re.Match) -> str:
            hh, mm = int(mc.group(1)), mc.group(2)
            if mm == '00':
                return f'{hh}点整'
            if mm.startswith('0'):
                return f'{hh}点零{int(mm)}分'
            return f'{hh}点{int(mm)}分'
        s = re.sub(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?![:\d])', _fmt_clock_tts, s)
    # Scientific notation powers of 10 ('10³' -> '10的3次方', '10⁶' -> '10的6次方', '10⁻³' -> '10的负3次方')
    _sup_digit_map = str.maketrans('⁰¹²³⁴⁵⁶⁷⁸⁹⁻', '0123456789-')
    def _fmt_pow10_tts(mp: re.Match) -> str:
        exp_norm = mp.group(1).translate(_sup_digit_map)
        if exp_norm.startswith('-'):
            return f'10的负{exp_norm[1:]}次方'
        return f'10的{exp_norm}次方'
    s = re.sub(r'(?<![0-9])10([⁻]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+)', _fmt_pow10_tts, s)
    # Chemical equation standalone metals adjacent to + / = / 生成 (e.g. 'Zn + H₂SO₄ = ZnSO₄ + H₂↑')
    if re.search(r'[+＋=＝]|生成', s):
        _chem_el_map = {'Zn': '锌', 'Fe': '铁', 'Cu': '铜', 'Mg': '镁', 'Al': '铝', 'Ag': '银', 'Ca': '钙', 'Na': '钠'}
        s = re.sub(
            r'(?<![A-Za-z])(Zn|Fe|Cu|Mg|Al|Ag|Ca|Na)(?![A-Za-z0-9₀-₉])(?=\s*[+＋=＝]|\s*在|\s*生成)|(?<=[+＋=＝]\s)(Zn|Fe|Cu|Mg|Al|Ag|Ca|Na)(?![A-Za-z0-9₀-₉])',
            lambda me: _chem_el_map.get(me.group(1) or me.group(2), me.group(0)),
            s,
        )
    replacements = (
        ('Rt△', '直角三角形'),
        ('RtΔ', '直角三角形'),
        ('S梯形', '梯形面积'),
        ('S△', '三角形面积'),
        ('S扇', '扇形面积'),
        ('S正', '正方形面积'),
        ('①', '第一步，'),
        ('②', '第二步，'),
        ('③', '第三步，'),
        ('④', '第四步，'),
        ('⑤', '第五步，'),
        ('⑥', '第六步，'),
        ('∶', '比'),
        ('℃', '摄氏度'),
        ('°C', '摄氏度'),
        ('kW·h', '千瓦时'),
        ('kWh', '千瓦时'),
        ('g/cm³', '克每立方厘米'),
        ('kg/m³', '千克每立方米'),
        ('N/kg', '牛每千克'),
        ('km/h', '千米每小时'),
        ('m/s²', '米每二次方秒'),
        ('m/s', '米每秒'),
        ('mm²', '平方毫米'),
        ('cm²', '平方厘米'),
        ('dm²', '平方分米'),
        ('km²', '平方千米'),
        ('m²', '平方米'),
        ('mm³', '立方毫米'),
        ('cm³', '立方厘米'),
        ('dm³', '立方分米'),
        ('km³', '立方千米'),
        ('m³', '立方米'),
        ('F浮', '浮力'),
        ('F支', '支持力'),
        ('F拉', '拉力'),
        ('F压', '压力'),
        ('ρ水', '水的密度'),
        ('ρ液', '液体密度'),
        ('ρ物', '物体密度'),
        ('ρ煤油', '煤油密度'),
        ('ρ酒精', '酒精密度'),
        ('ρ', '密度'),
        ('η', '机械效率'),
        ('W有用', '有用功'),
        ('W有', '有用功'),
        ('W额外', '额外功'),
        ('W额', '额外功'),
        ('W总', '总功'),
        ('P实', '实际功率'),
        ('P额', '额定功率'),
        ('U实', '实际电压'),
        ('U额', '额定电压'),
        ('R总', '总电阻'),
        ('I总', '总电流'),
        ('U总', '总电压'),
        ('kPa', '千帕'),
        ('Ω', '欧姆'),
        ('Fe₂(SO₄)₃', '硫酸铁'),
        ('Fe2(SO4)3', '硫酸铁'),
        ('Al₂(SO₄)₃', '硫酸铝'),
        ('Al2(SO4)3', '硫酸铝'),
        ('ZnSO₄', '硫酸锌'),
        ('ZnSO4', '硫酸锌'),
        ('FeSO₄', '硫酸亚铁'),
        ('FeSO4', '硫酸亚铁'),
        ('MgSO₄', '硫酸镁'),
        ('MgSO4', '硫酸镁'),
        ('Na₂SO₄', '硫酸钠'),
        ('Na2SO4', '硫酸钠'),
        ('H₂SO₄', '硫酸'),
        ('H2SO4', '硫酸'),
        ('H₂CO₃', '碳酸'),
        ('H2CO3', '碳酸'),
        ('HNO₃', '硝酸'),
        ('HNO3', '硝酸'),
        ('KClO₃', '氯酸钾'),
        ('KClO3', '氯酸钾'),
        ('CH₃COOH', '醋酸'),
        ('C₂H₅OH', '乙醇'),
        ('Mg(OH)₂', '氢氧化镁'),
        ('Mg(OH)2', '氢氧化镁'),
        ('Cu(OH)₂', '氢氧化铜'),
        ('Cu(OH)2', '氢氧化铜'),
        ('Fe(OH)₃', '氢氧化铁'),
        ('Fe(OH)3', '氢氧化铁'),
        ('Al(OH)₃', '氢氧化铝'),
        ('Al(OH)3', '氢氧化铝'),
        ('Ba(OH)₂', '氢氧化钡'),
        ('Ba(OH)2', '氢氧化钡'),
        ('KOH', '氢氧化钾'),
        ('NH₄Cl', '氯化铵'),
        ('NH4Cl', '氯化铵'),
        ('ZnCl₂', '氯化锌'),
        ('ZnCl2', '氯化锌'),
        ('MgCl₂', '氯化镁'),
        ('MgCl2', '氯化镁'),
        ('BaCl₂', '氯化钡'),
        ('BaCl2', '氯化钡'),
        ('CuCl₂', '氯化铜'),
        ('CuCl2', '氯化铜'),
        ('AlCl₃', '氯化铝'),
        ('AlCl3', '氯化铝'),
        ('AgCl', '氯化银'),
        ('KCl', '氯化钾'),
        ('Al₂O₃', '氧化铝'),
        ('Al2O3', '氧化铝'),
        ('MgO', '氧化镁'),
        ('CuO', '氧化铜'),
        ('CaO', '氧化钙'),
        ('CaCO₃', '碳酸钙'),
        ('CaCO3', '碳酸钙'),
        ('MnO₂', '二氧化锰'),
        ('MnO2', '二氧化锰'),
        ('K₂MnO₄', '锰酸钾'),
        ('K2MnO4', '锰酸钾'),
        ('KMnO₄', '高锰酸钾'),
        ('KMnO4', '高锰酸钾'),
        ('CuSO₄', '硫酸铜'),
        ('CuSO4', '硫酸铜'),
        ('NaOH', '氢氧化钠'),
        ('HCl', '盐酸'),
        ('H₂O₂', '过氧化氢'),
        ('H2O2', '过氧化氢'),
        ('Ca(OH)₂', '氢氧化钙'),
        ('Ca(OH)2', '氢氧化钙'),
        ('NaHCO₃', '碳酸氢钠'),
        ('NaHCO3', '碳酸氢钠'),
        ('Na₂CO₃', '碳酸钠'),
        ('Na2CO3', '碳酸钠'),
        ('BaSO₄', '硫酸钡'),
        ('BaSO4', '硫酸钡'),
        ('AgNO₃', '硝酸银'),
        ('AgNO3', '硝酸银'),
        ('Fe₂O₃', '氧化铁'),
        ('Fe2O3', '氧化铁'),
        ('Fe₃O₄', '四氧化三铁'),
        ('Fe3O4', '四氧化三铁'),
        ('FeCl₃', '氯化铁'),
        ('FeCl3', '氯化铁'),
        ('FeCl₂', '氯化亚铁'),
        ('FeCl2', '氯化亚铁'),
        ('CaCl₂', '氯化钙'),
        ('CaCl2', '氯化钙'),
        ('NaCl', '氯化钠'),
        ('P₂O₅', '五氧化二磷'),
        ('P2O5', '五氧化二磷'),
        ('SiO₂', '二氧化硅'),
        ('SiO2', '二氧化硅'),
        ('SO₂', '二氧化硫'),
        ('SO2', '二氧化硫'),
        ('NO₂', '二氧化氮'),
        ('NO2', '二氧化氮'),
        ('CO₂', '二氧化碳'),
        ('CO2', '二氧化碳'),
        ('NH₃', '氨气'),
        ('NH3', '氨气'),
        ('CH₄', '甲烷'),
        ('CH4', '甲烷'),
        ('Cl₂', '氯气'),
        ('Cl2', '氯气'),
        ('H₂O', '水'),
        ('↑', '气体符号'),
        ('↓', '沉淀符号'),
        ('∠', '角'),
        ('▱', '平行四边形'),
        ('⊙', '圆'),
        ('≌', '全等于'),
        ('∽', '相似于'),
        ('±', '正负'),
        ('△', '三角形'),
        ('Δ', '三角形'),
        ('∥', '平行于'),
        ('⊥', '垂直于'),
        ('∵', '因为'),
        ('∴', '所以'),
        ('≈', '约等于'),
        ('≠', '不等于'),
        ('≤', '小于等于'),
        ('≥', '大于等于'),
        ('＞', '大于'),
        ('＜', '小于'),
        ('÷', '除以'),
        ('×', '乘'),
        ('√', '根号'),
        ('½', '二分之一'),
        ('⅓', '三分之一'),
        ('¼', '四分之一'),
        ('¾', '四分之三'),
        ('²', '的平方'),
        ('³', '的立方'),
        ('°', '度'),
        ('π', '派'),
        ('□', '多少'),
        ('⇒', '推出'),
        ('3-4-5', '3、4、5'),
        ('＋', '加'),
        ('+', '加'),
        ('＝', '等于'),
        ('=', '等于'),
    )
    # Distinguish unary negative Unicode minus (e.g. '= −1.8℃', 'A(−3, 0)', '−5') from binary subtraction ('21 − 1', '总路程 − 甲车路程')
    s = re.sub(
        r'(?:(?<=^)|(?<=[(\uff08\[【{「『,，、;；:：=＝+＋×÷/])|(?<=等于)|(?<=是)|(?<=为)|(?<=得)|(?<=选)|(?<=填))(\s*)[−－](?=[0-9]|[a-zA-Z](?![a-zA-Z]))',
        r'\1负',
        s,
    )
    s = s.replace('−', '减').replace('－', '减')
    s = re.sub(r'_{2,}', ' 横线处 ', s)
    for k, v in replacements:
        s = s.replace(k, v)
    s = re.sub(r'(?<![A-Za-z])mm(?![A-Za-z])', '毫米', s)
    s = re.sub(r'(?<![A-Za-z])cm(?![A-Za-z])', '厘米', s)
    s = re.sub(r'(?<![A-Za-z])dm(?![A-Za-z])', '分米', s)
    s = re.sub(r'(?<![A-Za-z])km(?![A-Za-z])', '千米', s)
    s = re.sub(r'(?<=\d)\s*mL(?![a-zA-Z])', '毫升', s, flags=re.I)
    s = re.sub(r'(?<=\d)\s*kg(?![a-zA-Z/])', '千克', s, flags=re.I)
    s = re.sub(r'(?<=\d)\s*g(?![a-zA-Z/])', '克', s)
    s = re.sub(r'(?<![A-Za-z₂₃₄₅])(?<![A-Za-z]\d)H[2₂]O(?![A-Za-z0-9₂₃₄₅])', '水', s)
    s = re.sub(r'(?<![A-Za-z₂₃₄₅])(?<![A-Za-z]\d)O[2₂](?![A-Za-z0-9₂₃₄₅])', '氧气', s)
    s = re.sub(r'(?<![A-Za-z₂₃₄₅])(?<![A-Za-z]\d)H[2₂](?![A-Za-z0-9₂₃₄₅])', '氢气', s)
    s = re.sub(r'(?<![A-Za-z₂₃₄₅])(?<![A-Za-z]\d)N[2₂](?![A-Za-z0-9₂₃₄₅])', '氮气', s)
    _unit_tail = r'(?![a-zA-Z0-9₂₃₄₅₀₁₆₇₈₉]|\s+[A-Za-z])'
    s = re.sub(r'(?<=\d)\s*min(?![a-zA-Z])', '分钟', s)
    s = re.sub(r'(?<=\d)\s*s(?=\s*(?:内|后|时|[，,。；;：:）)]|$))', '秒', s)
    s = re.sub(r'(?<=\d)\s*m(?=\s*(?:内|高|长|宽|深|远|处|[，,。；;：:）)]|$))(?![a-zA-Z0-9²³₂₃₄₅/]|\s*[+\-−×÷*=＝a-zA-Z])', '米', s)
    s = re.sub(r'(?<![=\-])>(?!=)', '大于', s)
    s = re.sub(r'(?<!<)<(?![=\-])', '小于', s)
    s = re.sub(r'(?<=\d)\s*kW(?![a-zA-Z·])', '千瓦', s)
    s = re.sub(r'(?<=\d)\s*Pa' + _unit_tail, '帕', s)
    s = re.sub(r'(?<=\d)\s*N' + _unit_tail, '牛', s)
    s = re.sub(r'(?<=\d)\s*W' + _unit_tail, '瓦', s)
    s = re.sub(r'(?<=\d)\s*V' + _unit_tail, '伏', s)
    s = re.sub(r'(?<=\d)\s*A' + _unit_tail, '安', s)
    s = re.sub(r'(?<=\d)\s*J' + _unit_tail, '焦', s)
    s = re.sub(r'(?<=\d)\s*t(?=\s*(?:[，,。；;：:）)（(]|$))(?![a-zA-Z0-9])', '吨', s)
    s = re.sub(r'(?<=\d)\s*L(?=\s*(?:[，,。；;：:）)（(水液]|$))(?![a-zA-Z0-9])', '升', s)
    # Convert math center-dot multiplication (e.g. 6·(2x−1), OA·OB) -> 乘 without touching CJK names
    s = re.sub(r'(?<=[0-9a-zA-Z)\uff09²³₀-₉])\s*·\s*(?=[0-9a-zA-Z(\uff08√])', '乘', s)
    # Normalize physics/math subscript digits (e.g. R₁ -> R1, U₂ -> U2) after unit-symbol guard
    s = s.translate(str.maketrans('₀₁₂₃₄₅₆₇₈₉', '0123456789'))
    # Convert parenthesized algebraic fractions like (2x - 1)/3 -> 3分之(2x - 1), and numeric fractions 3/5 -> 5分之3
    s = re.sub(r'([（(][^()（）]{1,24}[)）])\s*/\s*(\d{1,4})(?![0-9/])', lambda m: f'{m.group(2)}分之{m.group(1)}', s)
    s = re.sub(r'(?<![0-9/])(\d{1,3})/(\d{1,4})(?![0-9/])', lambda m: f'{m.group(2)}分之{m.group(1)}', s)
    # Convert ASCII binary subtraction (including after math value suffixes like 'x的平方 - 5x', '2派 - 1', '180度 - 30度', NEVER after '等于/是/为') -> 减
    s = re.sub(r'(?:(?:(?<=[0-9)\uff09°%])|(?<=平方)|(?<=立方)|(?<=次方)|(?<=派)|(?<=度))\s*-\s*(?=[0-9(\uff08√]|[a-zA-Z](?![a-zA-Z]))|(?:(?<=[0-9)\uff09°%])|(?<=平方)|(?<=立方)|(?<=次方)|(?<=派)|(?<=度))\s+-\s+(?=[a-zA-Z])|(?<![A-Za-z])(?<=[a-zA-Z])\s*-\s*(?=[0-9(\uff08√])|(?<=[a-zA-Z])\s+-\s+(?=[0-9(\uff08√])|(?<![A-Za-z])(?<=[a-zA-Z])\s*-\s*(?=[a-zA-Z](?![A-Za-z]))|(?<=[a-zA-Z])\s+-\s+(?=[a-zA-Z]))', '减', s)
    s = re.sub(r'(?<![0-9a-zA-Z)\uff09])-(?=[0-9]|(?=[a-zA-Z](?![A-Za-z])))', '负', s)
    # Deduplicate redundant parenthesized chemical/symbol translations ('氢氧化钠（氢氧化钠）' -> '氢氧化钠', '稀盐酸（盐酸）' -> '稀盐酸')
    s = re.sub(
        r'([\u4e00-\u9fa5]{1,8})\s*[（(]\s*([\u4e00-\u9fa5]{1,8})\s*[）)]',
        lambda mc: mc.group(1) if len(mc.group(2)) >= 2 and mc.group(1).endswith(mc.group(2)) else mc.group(0),
        s,
    )
    # Deduplicate stuttered physics noun+symbol pairs (e.g. '受到的浮力 F浮' -> '受到的浮力', '有用功 W有' -> '有用功')
    s = re.sub(r'(水的密度|液体密度|物体密度|煤油密度|酒精密度|机械效率|有用功|额外功|总功|实际功率|额定功率|额定电压|实际电压|总电阻|总电流|总电压|支持力|浮力|拉力|压力)\s*\1', r'\1', s)
    s = s.replace('气气体符号', '气（气体）')
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:560]


def extract_json_objects_from_buffer(buf: str) -> tuple:
    """Streaming brace-depth JSON extractor supporting NDJSON, multi-line JSON, markdown fences, and JSON arrays."""
    objs = []
    n = len(buf)
    i = 0
    last_consumed = 0
    while i < n:
        if buf[i] != '{':
            i += 1
            continue
        start = i
        depth = 0
        in_str = False
        esc = False
        end = -1
        for j in range(start, n):
            ch = buf[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = j
                        break
        if end == -1:
            # Incomplete JSON object still streaming -> preserve from `start`
            return objs, buf[start:]
        candidate = buf[start:end + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                objs.append(parsed)
                last_consumed = end + 1
                i = end + 1
                continue
        except Exception:
            pass
        i = start + 1
        last_consumed = i
    return objs, buf[last_consumed:]


def _read_regular_bytes(path: Path, max_bytes: int = 2 * 1024 * 1024):
    fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            raise OSError('Not a regular bounded file')
        sig = (st.st_ino, st.st_mtime_ns, st.st_size)
        chunks = []
        rem = max_bytes + 1
        while rem > 0:
            b = os.read(fd, min(65536, rem))
            if not b:
                break
            chunks.append(b)
            rem -= len(b)
        raw = b''.join(chunks)
        if len(raw) > max_bytes:
            raise OSError('File exceeds max_bytes')
        return sig, raw
    finally:
        os.close(fd)


async def _read_resp_bounded(resp, max_bytes: int) -> bytes:
    content = getattr(resp, 'content', None)
    if content is not None and hasattr(content, 'iter_chunked'):
        buf = bytearray()
        async for chunk in content.iter_chunked(65536):
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise ValueError(f'HTTP response exceeds {max_bytes} bytes')
        return bytes(buf)
    if content is not None and hasattr(content, 'read'):
        buf = bytearray()
        while True:
            chunk = await content.read(min(65536, max_bytes - len(buf) + 1))
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise ValueError(f'HTTP response exceeds {max_bytes} bytes')
        return bytes(buf)
    data = await resp.read()
    if len(data) > max_bytes:
        raise ValueError(f'HTTP response exceeds {max_bytes} bytes')
    return data


def _atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink():
        raise OSError('Refusing symlink target')
    tmp = path.parent / f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp'
    created = False
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), mode)
        created = True
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), mode)
            if hasattr(os, 'geteuid') and os.geteuid() == 0:
                pst = os.lstat(str(path.parent))
                if pst.st_uid != 0:
                    with contextlib.suppress(OSError):
                        os.fchown(f.fileno(), pst.st_uid, pst.st_gid)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if path.is_symlink():
            raise OSError('Refusing symlink target')
        os.replace(str(tmp), str(path))
        created = False
    finally:
        if created:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)


def _atomic_write_json(path: Path, obj, mode: int = 0o600):
    data = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    _atomic_write_bytes(path, data, mode=mode)


def _read_safe_json(path: Path, default):
    try:
        _, raw = _read_regular_bytes(path, 2 * 1024 * 1024)
        return json.loads(raw.decode('utf-8'))
    except Exception:
        return default


# ==================== Socratic Checkpoint Sanitizer + Anti-Spoiler Gate + 8 Guided Lessons ====================
def _build_rescue_opts(num_str: str, unit_str: str) -> list:
    s_raw = str(num_str or '').strip()
    u = f' {unit_str}'.rstrip() if unit_str and not unit_str.startswith('°') else unit_str
    m_fr = re.match(r'^(-?\d+)\s*/\s*([1-9]\d*)$', s_raw)
    if m_fr:
        fn, fd = int(m_fr.group(1)), int(m_fr.group(2))
        sign = '-' if fn < 0 else ''
        an = abs(fn)
        fr_cands = []
        for cn, cd in ((an + 1, fd), (max(1, an - 1), fd), (an, fd + 1), (an + 1, fd + 1), (an + 2, fd)):
            if cn <= 0 or cd <= 0:
                continue
            g = math.gcd(cn, cd)
            rn, rd = cn // g, cd // g
            if rd <= 1:
                continue
            cand_s = f'{sign}{rn}/{rd}'
            if cand_s != s_raw and cand_s not in fr_cands and round(rn / rd, 6) != round(an / fd, 6):
                fr_cands.append(cand_s)
        if len(fr_cands) >= 2:
            return [f'{s_raw}{u}', f'{fr_cands[0]}{u}', f'{fr_cands[1]}{u}']
    try:
        val = float(s_raw)
        if '.' not in s_raw:
            cand = []
            for alt in (val * 2 if val != 0 else 10, val + (10 if abs(val) >= 20 else 2), max(1, val / 2) if val > 2 else val + 4, val + 6):
                s_alt = str(int(round(alt)))
                if s_alt != s_raw and s_alt not in cand:
                    cand.append(s_alt)
            return [f'{s_raw}{u}', f'{cand[0]}{u}', f'{cand[1]}{u}']
        dec_p = max(1, min(4, len(s_raw.split('.', 1)[1])))
        step_u = 10 ** (-dec_p)
        cand = []
        dec_alts = (
            val + 10 * step_u,
            val - 10 * step_u if val > 12 * step_u else val + 20 * step_u,
            val + 2 * step_u,
            val * 2 if val != 0 else 5 * step_u,
            val + 5 * step_u,
        )
        for alt in dec_alts:
            if val >= 0 and alt < 0:
                alt = val + 15 * step_u
            s_alt = f'{alt:.{dec_p}f}'
            if s_alt != s_raw and s_alt not in cand and round(float(s_alt), 6) != round(val, 6):
                cand.append(s_alt)
        return [f'{s_raw}{u}', f'{cand[0]}{u}', f'{cand[1]}{u}']
    except Exception:
        return [f'{s_raw}{u}', '再算算', '不确定']


def sanitize_quiz(raw_quiz, step_num: int = 1, rng_seed: str = '') -> dict:
    """Validate Socratic checkpoint: supports 'choice' (trap-contrast + whys) and 'num' (single or chain self-calc)."""
    if not isinstance(raw_quiz, dict):
        return None
    def _norm_keypad_num_unit(val_str: str, u_str: str) -> tuple:
        v_s = re.sub(r'[−－]', '-', str(val_str or '').strip())
        v_s = re.sub(r'^[A-Za-z]\s*[=＝]\s*', '', v_s).strip()
        v_s = re.sub(r'^(-?\d+)\s*/\s*([1-9]\d*)$', r'\1/\2', v_s)
        u_s = str(u_str or '').strip()
        if u_s.startswith('π'):
            if v_s and 'π' not in v_s:
                v_s = f'{v_s}π'
            u_s = u_s[1:].lstrip()
        if v_s and not re.search(r'[√π]', v_s):
            m_gu = re.fullmatch(r'(-?\d+(?:\.\d+)?(?:/[1-9]\d*)?)\s*([%℃°A-Za-z\u4e00-\u9fa5/²³]{1,8})', v_s)
            if m_gu and not m_gu.group(2).startswith('/'):
                v_s = m_gu.group(1)
                if not u_s:
                    u_s = m_gu.group(2)
        elif v_s and re.search(r'[√π]', v_s):
            m_sym = re.fullmatch(r'(±?-?\d*(?:\.\d+)?[√π]\d*(?:/[1-9]\d*)?)\s*([%℃°A-Za-z\u4e00-\u9fa5²³]{1,8})', v_s)
            if m_sym:
                v_s = m_sym.group(1)
                if not u_s:
                    u_s = m_sym.group(2)
            elif u_s and v_s.endswith(u_s):
                v_s = v_s[:-len(u_s)].strip()
        return v_s[:22], u_s[:12]

    q = str(raw_quiz.get('q') or raw_quiz.get('question') or '').strip()[:76]
    method = str(raw_quiz.get('method') or '').strip()[:16]
    raw_num_in = str(raw_quiz.get('num_ans') if raw_quiz.get('num_ans') is not None else '').strip()[:24]
    raw_unit_in = str(raw_quiz.get('unit') or '').strip()[:12]
    num_ans, unit = _norm_keypad_num_unit(raw_num_in, raw_unit_in)
    tip = str(raw_quiz.get('tip') or raw_quiz.get('hint') or '再仔细观察一下左侧黑板上的图形和已知条件哦！').strip()[:82]
    tip2 = str(raw_quiz.get('tip2') or raw_quiz.get('hint2') or '').strip()[:88]

    chain_clean = []
    first_raw_cq = ''
    last_raw_cq = ''
    last_raw_ca = ''
    last_raw_cu = ''
    had_symbolic_rung = False
    if isinstance(raw_quiz.get('chain'), list):
        raw_in_ch = raw_quiz['chain']
        slice_ch = (raw_in_ch[:7] + [raw_in_ch[-1]]) if len(raw_in_ch) > 8 else raw_in_ch[:8]
        for c in slice_ch:
            if not isinstance(c, dict):
                continue
            cq = str(c.get('q') or '').strip()[:62]
            ca_raw = str(c.get('ans') if c.get('ans') is not None else (c.get('num_ans') or '')).strip()[:24]
            cu_raw = str(c.get('unit') or '').strip()[:12]
            ca, cu = _norm_keypad_num_unit(ca_raw, cu_raw)
            ct = str(c.get('tip') or '').strip()[:78]
            if cq:
                if not first_raw_cq:
                    first_raw_cq = cq
                last_raw_cq = cq
            if ca:
                last_raw_ca, last_raw_cu = ca, cu
            if cq and ca and re.fullmatch(r'-?\d+(?:\.\d+)?(?:/[1-9]\d*)?', ca):
                chain_clean.append({'q': cq, 'ans': ca, 'unit': cu, 'tip': ct})
            elif cq and ca:
                had_symbolic_rung = True

    if len(chain_clean) == 1:
        c0 = chain_clean[0]
        if not q or '依次算出' in q or '按步骤' in q:
            q = c0['q']
        if not num_ans:
            num_ans = c0['ans']
        if not unit:
            unit = c0['unit']
        if c0['tip'] and (not raw_quiz.get('tip') or '注意单位' in tip):
            tip = c0['tip']
        chain_clean = []
    elif chain_clean:
        if not q:
            q = chain_clean[0]['q']
        if not num_ans:
            num_ans = chain_clean[-1]['ans']
        if not unit:
            unit = chain_clean[-1]['unit']
    if not q:
        return None

    opts_raw = raw_quiz.get('opts') or raw_quiz.get('options')
    opts = []
    extracted_meta_whys = []
    if isinstance(opts_raw, list):
        _opt_pre_re = re.compile(r'^(?:[（(]\s*([A-Da-d1-4])\s*[)）]|([A-Da-d])\s*[.．、:：]|([①②③④⑤⑥]))\s*')
        _pre_keys = set()
        for _rx in opts_raw[:3]:
            _mp = _opt_pre_re.match(str(_rx or '').strip())
            if _mp:
                _pre_keys.add((_mp.group(1) or _mp.group(2) or _mp.group(3) or '').upper())
        _strip_opt_pre = len(_pre_keys) >= 2
        for x in opts_raw[:3]:
            s_opt = str(x).strip()
            if not s_opt:
                continue
            if _strip_opt_pre:
                s_opt = _opt_pre_re.sub('', s_opt).strip() or s_opt
            meta_note = ''
            # Strip meta error-giveaway parentheticals (e.g. '（误套3-4-5）', '（约分算错）', '（漏除以2）') so kids cannot spot wrong options without doing math!
            m_paren = re.search(r'[（(]([^（）()]*(?:误|错|漏|搞反|陷阱|忘|盲猜)[^（）()]*)[）)]\s*$', s_opt)
            if m_paren:
                meta_note = m_paren.group(1).strip()
                s_opt = s_opt[:m_paren.start()].strip()
            if re.match(r'^(?:误当成|误把|误用|误算为|错当成|错把|错用)\s*', s_opt):
                if not meta_note:
                    meta_note = f'小心陷阱：{s_opt}'
                s_opt = re.sub(r'^(?:误当成|误把|误用|误算为|错当成|错把|错用)\s*', '', s_opt).strip()
            fin_opt = s_opt or str(x).strip()
            max_opt_len = 88 if len(re.findall(r'[A-Za-z]', fin_opt)) >= 8 else 48
            if len(fin_opt) > max_opt_len:
                cut_sp = fin_opt.rfind(' ', max_opt_len - 18, max_opt_len) if max_opt_len >= 80 else -1
                fin_opt = (fin_opt[:cut_sp].rstrip(' ,;:') + '…') if cut_sp > 0 else fin_opt[:max_opt_len]
            opts.append(fin_opt)
            extracted_meta_whys.append(meta_note)

    def _eval_scalar_pi_expr(raw_s: str, cur_u: str = '') -> tuple:
        s0 = str(raw_s or '').strip()
        if 'π' not in s0:
            return '', ''
        u_found = cur_u
        expr_s = s0
        m_u = re.match(r'^(.+?)\s*([%℃°A-Za-z\u4e00-\u9fa5²³]{1,8})\s*$', s0)
        if cur_u and s0.endswith(cur_u):
            expr_s = s0[:-len(cur_u)].strip()
        elif m_u and not m_u.group(2).startswith(('²', '³')) and 'π' not in m_u.group(2):
            expr_s = m_u.group(1).strip()
            if not u_found:
                u_found = m_u.group(2)
        expr_py = (
            expr_s.replace('²', '**2').replace('³', '**3')
            .replace('×', '*').replace('÷', '/')
            .replace('＋', '+').replace('－', '-').replace('−', '-')
            .replace('（', '(').replace('）', ')')
        )
        expr_py = re.sub(r'(\d|\))\s*π', r'\1*pi', expr_py)
        expr_py = re.sub(r'π\s*(\d|\()', r'pi*\1', expr_py)
        expr_py = expr_py.replace('π', 'pi')
        if not re.fullmatch(r'[0-9.+\-*/()pi\s]+', expr_py):
            return '', ''
        def _safe_ev(pi_v: float) -> float:
            tree = ast.parse(expr_py, mode='eval')
            def _walk(node):
                if isinstance(node, ast.Expression):
                    return _walk(node.body)
                if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                    return float(node.value)
                if isinstance(node, ast.Name) and node.id == 'pi':
                    return float(pi_v)
                if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                    v = _walk(node.operand)
                    return v if isinstance(node.op, ast.UAdd) else -v
                if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
                    l_v, r_v = _walk(node.left), _walk(node.right)
                    if isinstance(node.op, ast.Add): return l_v + r_v
                    if isinstance(node.op, ast.Sub): return l_v - r_v
                    if isinstance(node.op, ast.Mult): return l_v * r_v
                    if isinstance(node.op, ast.Div):
                        if abs(r_v) < 1e-12: raise ZeroDivisionError()
                        return l_v / r_v
                    if isinstance(node.op, ast.Pow):
                        if abs(r_v) > 6: raise ValueError()
                        return l_v ** r_v
                raise ValueError('unsupported')
            return _walk(tree)
        try:
            v1, v2, v3 = _safe_ev(1.0), _safe_ev(2.0), _safe_ev(3.0)
        except Exception:
            return '', ''
        if abs(v2 - 2.0 * v1) < 1e-9 and abs(v3 - 3.0 * v1) < 1e-9 and abs(v1) > 1e-9:
            iv = int(round(v1))
            ks = ('' if iv == 1 else ('-' if iv == -1 else str(iv))) if abs(v1 - iv) < 1e-6 else f'{v1:g}'
            return f'{ks}π', u_found
        return '', ''

    sym_rescued = False
    try:
        _raw_ai_sym = int(raw_quiz.get('ans', 0))
    except (TypeError, ValueError):
        _raw_ai_sym = 0
    _right_opt_sym = opts[_raw_ai_sym] if (len(opts) >= 2 and 0 <= _raw_ai_sym < len(opts)) else ''
    has_ineq_right_opt = bool(_right_opt_sym and re.search(r'[A-Za-z]\s*[<>＜＞≤≥≠]\s*[-−－+]?\d', _right_opt_sym))
    has_symbolic_right_opt = bool(_right_opt_sym and (re.search(r'[√π]', _right_opt_sym) or has_ineq_right_opt))
    def _is_simplified_binom_sym(opt_s: str) -> bool:
        core_b = re.sub(r'^[（(]\s*|\s*[）)]\s*[%℃°A-Za-z\u4e00-\u9fa5²³]{0,8}$', '', str(opt_s or '').strip())
        core_b = re.sub(r'\s*[%℃°A-Za-z\u4e00-\u9fa5²³]{1,8}$', '', core_b).strip()
        return bool(re.fullmatch(r'-?\d*(?:\.\d+)?[√π]\d*\s*[+\-＋－−]\s*\d+(?:\.\d+)?(?:/[1-9]\d*)?|-?\d+(?:\.\d+)?\s*[+\-＋－−]\s*\d*(?:\.\d+)?[√π]\d*', core_b))
    has_sym_formula_opts = (
        step_num == 3
        and not raw_quiz.get('force_choice')
        and len(opts) >= 2
        and any(re.search(r'[√π]', o) for o in opts)
        and not all(_is_simplified_binom_sym(o) or not any(ch in o for ch in ('÷', '×', '*', '²', '³')) for o in opts)
        and (any(ch in o for o in opts for ch in ('÷', '×', '＋', '－', '+', '*', '²', '³', '(', '（')) or any(re.search(r'\d\s*-\s*\d', o) for o in opts))
    )
    if had_symbolic_rung and last_raw_ca and not re.fullmatch(r'-?\d+(?:\.\d+)?(?:/[1-9]\d*)?', last_raw_ca):
        chain_clean = []
        num_ans = last_raw_ca
        if not unit and last_raw_cu:
            unit = last_raw_cu
    elif has_symbolic_right_opt and not (re.search(r'[√π]', num_ans) or re.search(r'[<>＜＞≤≥≠]', num_ans)):
        chain_clean = []
        ev_pi_s, ev_pi_u = _eval_scalar_pi_expr(_right_opt_sym, unit) if has_sym_formula_opts else ('', '')
        if ev_pi_s:
            num_ans = ev_pi_s
            if not unit and ev_pi_u:
                unit = ev_pi_u
        else:
            num_ans, _u_sym_tmp = _norm_keypad_num_unit(_right_opt_sym, unit)
            if not unit and _u_sym_tmp:
                unit = _u_sym_tmp
    elif not num_ans and not chain_clean and last_raw_ca and not re.fullmatch(r'-?\d+(?:\.\d+)?(?:/[1-9]\d*)?', last_raw_ca):
        num_ans = last_raw_ca
        if not unit and last_raw_cu:
            unit = last_raw_cu
    if has_sym_formula_opts and not num_ans and _right_opt_sym:
        ev_pi_s2, ev_pi_u2 = _eval_scalar_pi_expr(_right_opt_sym, unit)
        if ev_pi_s2:
            num_ans = ev_pi_s2
            if not unit and ev_pi_u2:
                unit = ev_pi_u2
    # Guard non-keypad answers (e.g. '2√3', '12π', '2√3/3', '±4', '3:2', 'has lived') so kids are never stuck on a 0-9 keypad!
    if num_ans and not re.fullmatch(r'-?\d+(?:\.\d+)?(?:/[1-9]\d*)?', num_ans):
        cleaned_first_q = re.sub(r'^(?:第\s*[①②③④⑤⑥⑦⑧1-8]\s*(?:小)?步[：:，,\s]*|[①②③④⑤⑥⑦⑧]\s*[.、，,：:\s]*)', '', first_raw_cq).strip()
        cleaned_last_q = re.sub(r'^(?:第\s*[①②③④⑤⑥⑦⑧1-8]\s*(?:小)?步[：:，,\s]*|[①②③④⑤⑥⑦⑧]\s*[.、，,：:\s]*)', '', last_raw_cq).strip()
        q_copied_first_rung = bool(cleaned_first_q and cleaned_last_q and cleaned_first_q != cleaned_last_q and (q == first_raw_cq or q == cleaned_first_q))
        if not chain_clean and (any(k_seq in q for k_seq in ('依次算出', '按步骤', '依次手算')) or q_copied_first_rung):
            q = cleaned_last_q if (cleaned_last_q and '系数' not in cleaned_last_q) else '请在草稿本算出最终结果并选出正确项：'
        chain_clean = []
        u_rp = (unit if (unit.startswith('°') or re.match(r'^[\u4e00-\u9fa5]', unit)) else f' {unit}') if unit else ''
        if (len(opts) < 2 or has_sym_formula_opts) and re.search(r'[√π]', num_ans):
            m_rp = re.fullmatch(r'(-?\d*(?:\.\d+)?)([√π]\d*)', num_ans)
            m_frp = re.fullmatch(r'(-?\d*(?:\.\d+)?)([√π]\d*)/([1-9]\d*)', num_ans)
            if m_rp:
                c_txt_rp, sym_rp = m_rp.group(1), m_rp.group(2)
                c_val = -1.0 if c_txt_rp == '-' else (float(c_txt_rp) if c_txt_rp else 1.0)
                def _fmt_rp(cv: float) -> str:
                    iv_r = int(round(cv))
                    cs = ('' if iv_r == 1 else ('-' if iv_r == -1 else str(iv_r))) if abs(cv - iv_r) < 1e-6 else f'{cv:g}'
                    return f'{cs}{sym_rp}{u_rp}'
                d1 = _fmt_rp(c_val * 2 if c_val != 0 else 2)
                d2 = _fmt_rp(c_val + 1 if c_val >= 0 else c_val - 1)
                opts = [f'{num_ans}{u_rp}', d1 if d1 != f'{num_ans}{u_rp}' else f'3{sym_rp}{u_rp}', d2 if d2 not in (f'{num_ans}{u_rp}', d1) else f'4{sym_rp}{u_rp}']
            elif m_frp:
                c_txt_f, sym_f, den_f = m_frp.group(1), m_frp.group(2), int(m_frp.group(3))
                c_prefix = c_txt_f
                d1 = f'{c_prefix}{sym_f}{u_rp}'
                d2 = f'{c_prefix}{sym_f}/{den_f * 2}{u_rp}'
                opts = [f'{num_ans}{u_rp}', d1 if d1 != f'{num_ans}{u_rp}' else f'2{sym_f}/{den_f}{u_rp}', d2]
            else:
                m_bin = re.fullmatch(r'(-?\d*(?:\.\d+)?)([√π]\d*)\s*([+\-＋－−])\s*(\d+(?:\.\d+)?)', num_ans)
                if m_bin:
                    ca_b, sym_b, op_b, cb_b = m_bin.group(1), m_bin.group(2), m_bin.group(3), m_bin.group(4)
                    av_b = -1.0 if ca_b == '-' else (float(ca_b) if ca_b else 1.0)
                    iv2_b = int(round(av_b * 2)) if abs(av_b * 2 - round(av_b * 2)) < 1e-6 else av_b * 2
                    flip_op_b = '+' if op_b in ('-', '－', '−') else '−'
                    norm_op_b = '−' if op_b in ('-', '－', '−') else '+'
                    p_l = '（' if u_rp else ''
                    p_r = f'）{u_rp.lstrip()}' if u_rp else ''
                    opts = [
                        f'{p_l}{ca_b}{sym_b}{norm_op_b}{cb_b}{p_r}',
                        f'{p_l}{iv2_b}{sym_b}{norm_op_b}{cb_b}{p_r}',
                        f'{p_l}{ca_b}{sym_b}{flip_op_b}{cb_b}{p_r}',
                    ]
                else:
                    opts = [f'{num_ans}{u_rp}', f'2×({num_ans}){u_rp}', f'({num_ans})÷2{u_rp}']
            extracted_meta_whys = [''] * len(opts)
            ans_idx = 0
            sym_rescued = True
        elif len(opts) < 2 and re.fullmatch(r'[A-Za-z]\s*[<>＜＞≤≥]\s*-?\d+(?:\.\d+)?(?:/[1-9]\d*)?', num_ans):
            m_iq = re.fullmatch(r'([A-Za-z])\s*([<>＜＞≤≥])\s*(-?\d+(?:\.\d+)?(?:/[1-9]\d*)?)', num_ans)
            iq_v, iq_op, iq_n = m_iq.group(1), m_iq.group(2), m_iq.group(3)
            flip_map_iq = {'<': '>', '>': '<', '＜': '＞', '＞': '＜', '≤': '≥', '≥': '≤'}
            opp_n = iq_n[1:] if iq_n.startswith('-') else (f'-{iq_n}' if iq_n != '0' else '1')
            opts = [f'{iq_v} {iq_op} {iq_n}', f'{iq_v} {flip_map_iq.get(iq_op, ">")} {iq_n}', f'{iq_v} {iq_op} {opp_n}']
            extracted_meta_whys = ['', '注意不等式两边同乘或同除以负数时，不等号方向必须反转哦！', '再检查一下移项与除法的正负号哦！']
            ans_idx = 0
            sym_rescued = True
        elif len(opts) < 2 and re.fullmatch(r'±\s*\d+(?:\.\d+)?(?:/[1-9]\d*)?', num_ans):
            v_pm = re.sub(r'^±\s*', '', num_ans)
            opts = [f'±{v_pm}{u_rp}', f'{v_pm}{u_rp}', f'-{v_pm}{u_rp}']
            extracted_meta_whys = [''] * len(opts)
            ans_idx = 0
            sym_rescued = True
        elif len(opts) < 2 and re.fullmatch(r'\d+\s*[:∶]\s*\d+', num_ans):
            m_rat = re.fullmatch(r'(\d+)\s*([:∶])\s*(\d+)', num_ans)
            ra, sep_r, rb = int(m_rat.group(1)), m_rat.group(2), int(m_rat.group(3))
            d1_r = f'{rb}{sep_r}{ra}' if rb != ra else f'{ra + 1}{sep_r}{rb}'
            d2_r = f'{ra + 1}{sep_r}{rb}' if f'{ra + 1}{sep_r}{rb}' != d1_r else f'{ra}{sep_r}{rb + 1}'
            opts = [f'{ra}{sep_r}{rb}', d1_r, d2_r]
            extracted_meta_whys = [''] * len(opts)
            ans_idx = 0
            sym_rescued = True
        elif len(opts) < 2:
            opts = [f'{num_ans}{u_rp}', '再核对一下符号与系数', '再检查一下化简结果']
            extracted_meta_whys = [''] * len(opts)
            ans_idx = 0
            sym_rescued = True
        if has_ineq_right_opt and any(k_iq in q for k_iq in ('依次算出', '按步骤')):
            q = '请在草稿本先算出分界值，再注意不等号方向选出正确解集：'
        num_ans = ''

    # If Step 3 numeric mode has formula-stuffed options (e.g. '72÷(2×2+2)×...'), replace with clean numeric rescue options
    has_formula_in_opts = bool(num_ans) and (any(ch in o for o in opts for ch in ('÷', '×', '＋', '－', '+', '*', '[', '［', '(', '（')) or any(re.search(r'\d\s*-\s*\d', o) for o in opts))
    if (len(opts) < 2 or has_formula_in_opts) and num_ans:
        opts = _build_rescue_opts(num_ans, unit)
        extracted_meta_whys = [''] * len(opts)
        ans_idx = 0
    elif sym_rescued and len(opts) >= 2:
        ans_idx = 0
    elif len(opts) >= 2:
        try:
            ans_idx = int(raw_quiz.get('ans', 0))
        except (TypeError, ValueError):
            ans_idx = 0
        if not (0 <= ans_idx < len(opts)):
            ans_idx = 0
    elif str(raw_quiz.get('mode') or '').strip().lower() == 'input' and isinstance(raw_quiz.get('ans'), str) and raw_quiz['ans'].strip():
        return {
            'mode': 'input',
            'method': method,
            'q': q,
            'opts': [],
            'ans': raw_quiz['ans'].strip()[:64],
            'whys': [],
            'num_ans': '',
            'unit': '',
            'chain': [],
            'tip': tip,
            'tip2': tip2,
        }
    else:
        return None

    whys_raw = raw_quiz.get('whys') if isinstance(raw_quiz.get('whys'), list) and not has_formula_in_opts and not sym_rescued else []
    whys = []
    for i in range(len(opts)):
        w_txt = str(whys_raw[i]).strip()[:84] if i < len(whys_raw) and whys_raw[i] else ''
        if not w_txt and i != ans_idx and i < len(extracted_meta_whys) and extracted_meta_whys[i]:
            w_txt = f'注意避坑：{extracted_meta_whys[i]}哦！'[:84]
        whys.append(w_txt)

    if raw_quiz.get('no_shuffle') and not has_formula_in_opts and not sym_rescued:
        shuffled_opts = opts
        shuffled_whys = whys
        ans_shuffled = ans_idx
    else:
        correct_val = opts[ans_idx]
        h = int(hashlib.sha256(f'{rng_seed}:{step_num}:{q}'.encode('utf-8')).hexdigest()[:8], 16)
        perm = list(range(len(opts)))
        for i in range(len(opts) - 1, 0, -1):
            j = h % (i + 1)
            h //= (i + 1)
            perm[i], perm[j] = perm[j], perm[i]
        shuffled_opts = [opts[p] for p in perm]
        shuffled_whys = [whys[p] for p in perm]
        ans_shuffled = perm.index(ans_idx)

    mode = 'num' if (num_ans or chain_clean) else 'choice'
    if mode == 'choice' and step_num == 3 and not raw_quiz.get('force_choice'):
        c_txt = shuffled_opts[ans_shuffled]
        if '各' not in q and '分别' not in q:
            m = re.match(r'^\s*([-−－]?\d+(?:\.\d+)?)\s*([^\d\s,，;；/√π=\+\-−－×÷()（）]{0,10})\s*$', c_txt)
            if m:
                mode = 'num'
                num_ans = re.sub(r'[−－]', '-', m.group(1))
                if not unit:
                    unit = m.group(2)

    out_qz = {
        'mode': mode,
        'method': method,
        'q': q,
        'opts': shuffled_opts,
        'ans': ans_shuffled,
        'whys': shuffled_whys,
        'num_ans': num_ans,
        'unit': unit,
        'chain': chain_clean,
        'tip': tip,
        'tip2': tip2,
    }
    tap_t = str(raw_quiz.get('tap_target') or '').strip()[:24]
    if tap_t and mode == 'choice':
        out_qz['tap_target'] = tap_t
        out_qz['tap_prompt'] = str(raw_quiz.get('tap_prompt') or '也可直接在左图点选目标位置闯关').strip()[:42]
    return out_qz


_CN_DIGITS_MAP = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
_CN_UNITS_MAP = {'十': 10, '百': 100, '千': 1000, '万': 10000}
_INT_CN_CHARS = '零一二三四五六七八九'
_MATH_UNIT_PAT = r'份|平方厘米|平方米|平方分米|立方厘米|立方分米|立方米|厘米|分米|米/秒|米|千米/时|千米|欧姆|Ω|安培|安|伏特|伏|瓦特|瓦|牛顿|牛|帕斯卡|帕|焦耳|焦|千瓦时|摄氏度|℃|°C|%|本|只|棵|枚|颗|张|支|块|页|道|辆|人|名|天|岁|盒|箱|袋|千克|克|吨|升|毫升|秒|分钟|小时|度|°|元|个|段|倍'


def _cn_to_num_str(tok: str) -> str:
    """Parse Chinese numeral token (e.g. '六', '十二', '二百八十八', '二点五') into Arabic numeral string."""
    s = str(tok or '').strip()
    if not s:
        return ''
    neg = False
    if s.startswith('负'):
        neg = True
        s = s[1:]
    if '点' in s:
        p_int, p_dec = s.split('点', 1)
        i_str = _cn_to_num_str(p_int) if p_int else '0'
        d_str = ''.join(str(_CN_DIGITS_MAP[c]) for c in p_dec if c in _CN_DIGITS_MAP)
        if i_str and d_str:
            return ('-' if neg else '') + f'{i_str}.{d_str}'
        return ''
    if not any(c in _CN_DIGITS_MAP or c in _CN_UNITS_MAP for c in s):
        return ''
    if not any(c in _CN_UNITS_MAP for c in s):
        out_d = ''.join(str(_CN_DIGITS_MAP[c]) for c in s if c in _CN_DIGITS_MAP)
        return (('-' if neg else '') + out_d) if out_d else ''
    total = 0
    sec = 0
    cur = -1
    for ch in s:
        if ch in _CN_DIGITS_MAP:
            cur = _CN_DIGITS_MAP[ch]
        elif ch == '万':
            sec += cur if cur > 0 else (1 if sec == 0 else 0)
            total += sec * 10000
            sec = 0
            cur = -1
        elif ch in _CN_UNITS_MAP:
            u = _CN_UNITS_MAP[ch]
            if u == 10 and cur <= 0:
                sec += 10
            else:
                sec += max(0, cur) * u
            cur = -1
    if cur > 0:
        sec += cur
    return ('-' if neg else '') + str(total + sec)


def _int_under_10000_to_cn(n: int) -> str:
    if n == 0:
        return '零'
    parts = []
    units = ((1000, '千'), (100, '百'), (10, '十'), (1, ''))
    need_zero = False
    orig = n
    for u_val, u_ch in units:
        d, n = divmod(n, u_val)
        if d > 0:
            if need_zero:
                parts.append('零')
                need_zero = False
            if u_val == 10 and d == 1 and orig < 20:
                parts.append('十')
            else:
                parts.append(_INT_CN_CHARS[d] + u_ch)
        else:
            if parts and n > 0:
                need_zero = True
    return ''.join(parts)


def _num_to_cn_variants(num_str: str) -> list:
    """Return Chinese-numeral equivalents for an Arabic numeral string (e.g. '288' -> ['二百八十八', '两百八十八'])."""
    s = str(num_str or '').strip()
    if not re.match(r'^-?\d+(?:\.\d+)?$', s):
        return []
    neg_p = '负' if s.startswith('-') else ''
    body = s[1:] if neg_p else s
    out = []

    def _add(v: str):
        if v and v not in out:
            out.append(v)

    neg_prefixes = ('负', '零下') if neg_p else ('',)
    if '.' in body:
        ip, dp = body.split('.', 1)
        dp_forms = [dp]
        dp_trim = dp.rstrip('0')
        if dp_trim and dp_trim != dp:
            dp_forms.append(dp_trim)
        if len(ip) <= 5 and len(dp) <= 4:
            iv = int(ip)
            if iv <= 99999:
                i_cn = _int_under_10000_to_cn(iv) if iv < 10000 else (_int_under_10000_to_cn(iv // 10000) + '万' + ('' if iv % 10000 == 0 else (('零' if iv % 10000 < 1000 else '') + _int_under_10000_to_cn(iv % 10000))))
                for dp_cur in dp_forms:
                    d_cn = ''.join(_INT_CN_CHARS[int(c)] for c in dp_cur)
                    for np_c in neg_prefixes:
                        _add(f'{np_c}{i_cn}点{d_cn}')
                        if i_cn == '二':
                            _add(f'{np_c}两点{d_cn}')
                if not dp_trim:
                    for np_c in neg_prefixes:
                        _add(f'{np_c}{i_cn}')
        return out
    iv = int(body)
    if iv > 99999:
        return []
    if iv < 10000:
        base = _int_under_10000_to_cn(iv)
    else:
        hi, lo = divmod(iv, 10000)
        base = _int_under_10000_to_cn(hi) + '万' + ('' if lo == 0 else (('零' if lo < 1000 else '') + _int_under_10000_to_cn(lo)))
    for np_c in neg_prefixes:
        _add(np_c + base)
        if iv == 2:
            _add(np_c + '两')
        elif 10 <= iv <= 19:
            _add(np_c + '一' + base)
        if base.startswith('二') and len(base) >= 2 and base[1] in ('百', '千', '万'):
            _add(np_c + '两' + base[1:])
    return out


def _target_num_regex_alt(num_str: str) -> str:
    s = re.sub(r'[−－]', '-', str(num_str or '').strip())
    alts = []
    if re.fullmatch(r'-?\d+(?:\.\d+)?', s):
        is_neg = s.startswith('-')
        mag = s[1:] if is_neg else s
        mag_canon = mag.rstrip('0').rstrip('.') if '.' in mag else mag
        if not mag_canon:
            mag_canon = '0'
        if '.' in mag_canon:
            mag_pat = rf'{re.escape(mag_canon)}0*'
        elif '.' in mag:
            mag_pat = rf'{re.escape(mag_canon)}(?:\.0+)?'
        else:
            mag_pat = re.escape(mag)
        if is_neg:
            alts.append(rf'(?:[-−－]|负|零下)\s*{mag_pat}')
        else:
            alts.append(mag_pat)
        for cv in _num_to_cn_variants(s):
            esc_cv = re.escape(cv)
            if esc_cv not in alts:
                alts.append(esc_cv)
    elif re.fullmatch(r'-?\d+/[1-9]\d*', s):
        is_neg_f = s.startswith('-')
        fn_s, fd_s = (s[1:] if is_neg_f else s).split('/', 1)
        neg_f_pat = r'(?:[-−－]|负|零下)\s*' if is_neg_f else ''
        alts.append(rf'{neg_f_pat}{re.escape(fn_s)}\s*/\s*{re.escape(fd_s)}')
        alts.append(rf'{neg_f_pat}{re.escape(fd_s)}分之{re.escape(fn_s)}')
        for cd_v in _num_to_cn_variants(fd_s):
            for cn_v in _num_to_cn_variants(fn_s):
                for np_f in (('负', '') if is_neg_f else ('',)):
                    if is_neg_f and not np_f:
                        continue
                    fc_s = re.escape(f'{np_f}{cd_v}分之{cn_v}')
                    if fc_s not in alts:
                        alts.append(fc_s)
    else:
        alts.append(re.escape(s))
    return '(?:' + '|'.join(alts) + ')'


def sanitize_ocr_question(raw_q: str) -> str:
    """Strip student-handwritten trailing answers ('答：288...', trailing '= 288' after '？') from OCR stem."""
    s = str(raw_q or '').strip()
    if not s:
        return ''
    s = re.sub(r'([？?。！!；;\n])\s*[（(]?\s*(?:提示|解题提示|思路提示)[：:][^？?\n]*$', r'\1', s)
    s = re.sub(r'([？?。！!；;\n])\s*(?:答|解|解答|答案)[：:]\s*[^？?]+$', r'\1', s)
    s = re.sub(r'([？?])\s*(?:[=＝]\s*)?-?\d+(?:\.\d+)?\s*(?:' + _MATH_UNIT_PAT + r')?\s*[✓√×]?\s*$', r'\1', s)
    return re.sub(r'\s{3,}', '  ', s).strip()


def _mask_formula_in_level1_tip(tip_txt: str) -> str:
    """Prevent Level-1 conceptual hint ('思路提示') from spoiling veiled formulas before Level-2 '引导列式'."""
    t = str(tip_txt or '').strip()
    if not t:
        return t
    # Pure bare formula like '8 ÷ 2 = ？', '(6 + 14) × 5 ÷ 2 = ？', or '13² - 5² = ？'
    if re.match(r'^[\d.\s+\-−＋－×÷*/()（）²³°π√=＝≈？?□]+$', t):
        return '先对照左侧图示想清这两个量的加减乘除关系（再点一次提示可开启引导列式）！'
    # Replace explicit numeric expressions including parentheses, powers, and radicals like '(6 + 14) × 5 ÷ 2' or '13² − 5²'
    _atom_re = r'(?:[()（）]\s*)*√?\s*\d+(?:\.\d+)?[²³°π]?\s*(?:[()（）]\s*)*'
    t2 = re.sub(
        rf'(?:先?用\s*)?{_atom_re}(?:[×÷+\-−＋－*/]\s*{_atom_re})+(?:\s*[=＝≈]\s*[？?□\d.]+)?',
        '对应已知量按数量关系列式',
        t,
    )
    t2 = re.sub(
        r'(?:先?用\s*)?\d+(?:\.\d+)?[²³]?\s*(?:除以|乘以|乘|加上|减去)\s*\d+(?:\.\d+)?[²³]?',
        '对应已知量按关系运算',
        t2,
    )
    return t2


def _extract_opt_num_unit(c_opt: str) -> tuple:
    """Extract (arabic_num_str, unit_str) from a Step 1/2 option whether written in Arabic or Chinese numerals."""
    s = re.sub(r'[（(][^（）()]*[）)]\s*$', '', str(c_opt or '').strip()).strip()
    m_ar = re.search(rf'(?<![0-9.])([-−－]?\d+(?:\.\d+)?)\s*(?:个)?({_MATH_UNIT_PAT})(?:$|[^0-9])', s)
    if m_ar:
        return re.sub(r'[−－]', '-', m_ar.group(1)), m_ar.group(2)
    m_cn = re.search(rf'([负零〇一二两三四五六七八九十百千万点]+)\s*(?:个)?({_MATH_UNIT_PAT})', s)
    if m_cn:
        n_ar = _cn_to_num_str(m_cn.group(1))
        if n_ar:
            return n_ar, m_cn.group(2)
    return '', ''


def scrub_step_spoilers(step_item: dict) -> dict:
    """Hard anti-spoiler gate: ensure Step 1-3 NEVER leak their own or final numeric answers (Arabic or Chinese numerals) on board, canvas, voice, or tips."""
    if not isinstance(step_item, dict):
        return step_item
    st_num = step_item.get('step')
    qz = step_item.get('quiz')
    if not isinstance(qz, dict) or st_num not in (1, 2, 3):
        return step_item
    if st_num in (1, 2):
        # Guard Step 1/2 board, say, tip, and tip2 from leaking the quiz answer (Arabic OR Chinese numerals!) before the child answers!
        if qz.get('opts') and 0 <= qz.get('ans', 0) < len(qz['opts']):
            c_opt = str(qz['opts'][qz['ans']]).strip()
            c_core = re.sub(r'^(?:[（(]\s*[A-Da-d1-4]\s*[)）]|[A-Da-d]\s*[.．、:：]|[①②③④])\s*', '', c_opt).strip()
            c_core = re.sub(r'[（(][^（）()]*[）)]\s*$', '', c_core).rstrip('。！!. ').strip()
            val12_raw, unit12_raw = _extract_opt_num_unit(c_opt)
            if val12_raw and not re.search(r'[×÷+\-−=]', c_core):
                v_pat = _target_num_regex_alt(val12_raw)
                u_pat = re.escape(unit12_raw) if unit12_raw else ''
                u_opt = rf'(?:\s*(?:个)?{u_pat})?' if u_pat else ''
                step_item['board'] = re.sub(
                    rf'([=＝]|共有|一共是?|等于|还剩|剩下|相差)\s*{v_pat}{u_opt}(?![0-9.])',
                    r'\1 ？',
                    step_item.get('board') or '',
                )
                step_item['say'] = re.sub(
                    rf'(?:共有|一共是?|等于|还剩|剩下|相差|结果是|是)\s*{v_pat}{u_opt}(?![0-9.])',
                    '多少',
                    step_item.get('say') or '',
                )
                for d in step_item.get('draw') or []:
                    if d.get('label'):
                        d['label'] = re.sub(rf'([=＝:：]|共|一共是?|剩|相差)\s*{v_pat}{u_opt}(?![0-9.])', r'\1？', d['label'])
                for tf in ('tip', 'tip2'):
                    if qz.get(tf):
                        qz[tf] = re.sub(rf'([=＝]|等于|得|还剩|剩下|共|一共是?|相差)\s*{v_pat}{u_opt}(?![0-9.])', r'\1 ？', qz[tf])
            for cand_ph in ([c_opt, c_core] if c_core and c_core != c_opt else [c_opt]):
                if len(cand_ph) >= 4:
                    esc_c = re.escape(cand_ph)
                    step_item['board'] = re.sub(rf'([:：=＝]|结论是|答案是|应选|应填)\s*{esc_c}', r'\1 ？', step_item.get('board') or '')
                    step_item['say'] = re.sub(rf'(?:我们可以|应该|首先要|第一步先|答案是|结论是|应选|应填)\s*[^，。！？]{{0,4}}{esc_c}', '先自己动脑筋想一想', step_item.get('say') or '')
                    for d in step_item.get('draw') or []:
                        if d.get('label') and cand_ph in d['label']:
                            d['label'] = re.sub(rf'([:：=＝]|结论是|答案是|应选|应填)\s*{esc_c}', r'\1？', d['label'])
                    for tf in ('tip', 'tip2'):
                        if qz.get(tf) and cand_ph in qz[tf]:
                            qz[tf] = qz[tf].replace(cand_ph, '哪一项关系')
                elif 2 <= len(cand_ph) <= 3 and not re.fullmatch(r'-?\d+(?:\.\d+)?', cand_ph):
                    esc_s12 = re.escape(cand_ph)
                    step_item['board'] = re.sub(rf'(?:结论是|答案是|应选|应填|应该选|应该填)\s*[「“"\x27]?{esc_s12}[」”"\x27]?(?![A-Za-z])', '待你辨析选择', step_item.get('board') or '')
                    step_item['say'] = re.sub(rf'(?:结论是|答案是|应选|应填|应该选|应该填)\s*[「“"\x27]?{esc_s12}[」”"\x27]?(?![A-Za-z])', '留给你来选一选', step_item.get('say') or '')
        say_cur = str(step_item.get('say') or '').strip()
        if len(say_cur) >= 14 and '？' not in say_cur and '?' not in say_cur:
            step_item['say'] = say_cur.rstrip('。！!. ') + '，你觉得应该选哪一项呢？'
        return step_item

    targets = []
    for c in qz.get('chain') or []:
        if c.get('ans') and str(c['ans']) not in targets:
            targets.append(str(c['ans']))
    t_main = str(qz.get('num_ans') or '').strip()
    if not t_main and qz.get('opts') and 0 <= qz.get('ans', 0) < len(qz['opts']):
        m = re.match(r'^\s*([-−－]?\d+(?:\.\d+)?)(?:\s*[^\d/√π]|$)', qz['opts'][qz['ans']])
        if m:
            t_main = re.sub(r'[−－]', '-', m.group(1))
        else:
            v_cn, _ = _extract_opt_num_unit(qz['opts'][qz['ans']])
            if v_cn:
                t_main = v_cn
    if t_main and t_main not in targets:
        targets.append(t_main)
    for target in targets:
        t_pat = _target_num_regex_alt(target)
        step_item['board'] = re.sub(rf'([=＝≈]\s*){t_pat}(?![0-9.])', r'\1□', step_item.get('board') or '')
        for d in step_item.get('draw') or []:
            if d.get('label'):
                d['label'] = re.sub(rf'([=＝:：≈]\s*){t_pat}(?![0-9.])', r'\1□', d['label'])
                d['label'] = re.sub(rf'(?:面积|周长|宽|长|高|斜边|结果|得数)是\s*{t_pat}(?:{_MATH_UNIT_PAT})?', '待自己算出', d['label'])
            if d.get('op') == 'table' and isinstance(d.get('rows'), list):
                d['rows'] = [
                    [re.sub(rf'(?:^|[=＝:：≈]\s*){t_pat}(?:\s*(?:{_MATH_UNIT_PAT}))?$', '待求？', str(cell)) for cell in row]
                    for row in d['rows']
                ]
            if d.get('op') == 'vcalc' and d.get('res'):
                d['res'] = re.sub(rf'(?:^|[=＝≈]\s*){t_pat}(?![0-9.])', '□', str(d['res']))
        step_item['say'] = re.sub(
            rf'(?:等于|得数是|结果是|答案是|算出来是|还剩|剩下|宽是|长是|高是|面积是|周长是|总数是|一共是)\s*{t_pat}\s*(?:{_MATH_UNIT_PAT})?(?![0-9.])',
            '等于多少呢',
            step_item.get('say') or '',
        )
        for tf in ('tip', 'tip2'):
            if qz.get(tf):
                qz[tf] = re.sub(rf'([=＝≈]\s*|等于\s*|得\s*){t_pat}(?![0-9.])', r'\1？', qz[tf])
        for c_item in qz.get('chain') or []:
            if c_item.get('tip') and str(c_item.get('ans') or '') == target:
                c_item['tip'] = re.sub(rf'([=＝≈]\s*|等于\s*|得\s*){t_pat}(?![0-9.])', r'\1？', c_item['tip'])
    # Guard Step 3 spoken audio & Level-1 conceptual tip from dictating the veiled arithmetic formula verbatim!
    if qz.get('mode') == 'num':
        step_item['say'] = re.sub(
            r'(?:先)?用\s*\d+(?:\.\d+)?\s*(?:除以|乘以|乘|加上|加|减去|减)\s*\d+(?:\.\d+)?',
            '自己列式算一算',
            step_item.get('say') or '',
        )
        has_board_veil = bool(re.search(r'[0-9)\uff09]\s*[=＝]\s*□', step_item.get('board') or ''))
        if has_board_veil or '□' not in (step_item.get('board') or ''):
            if qz.get('tip'):
                qz['tip'] = _mask_formula_in_level1_tip(qz['tip'])
            for c_item in qz.get('chain') or []:
                if c_item.get('tip'):
                    c_item['tip'] = _mask_formula_in_level1_tip(c_item['tip'])
        if '□' not in (step_item.get('board') or ''):
            step_item['board'] = ((step_item.get('board') or '').rstrip() + '\n草稿本列式自算：结果 = □').strip()
        say3 = str(step_item.get('say') or '').strip()
        if len(say3) >= 14 and '？' not in say3 and '?' not in say3:
            step_item['say'] = say3.rstrip('。！!. ') + '，你能在草稿本上自己算出得数吗？'
    else:
        if qz.get('opts') and 0 <= qz.get('ans', 0) < len(qz['opts']):
            c_opt3 = str(qz['opts'][qz['ans']]).strip()
            c_core3 = re.sub(r'^(?:[（(]\s*[A-Da-d1-4]\s*[)）]|[A-Da-d]\s*[.．、:：]|[①②③④])\s*', '', re.sub(r'[（(][^（）()]*[）)]\s*$', '', c_opt3)).strip()
            u3_tail = str(qz.get('unit') or '').strip()
            c_bare3 = c_core3[:-len(u3_tail)].strip() if (u3_tail and c_core3.endswith(u3_tail)) else ''
            cands3 = [c_opt3]
            for _cx3 in (c_core3, c_bare3):
                if _cx3 and _cx3 not in cands3:
                    cands3.append(_cx3)
            if c_bare3 and re.search(r'[√π]', c_bare3):
                for _sp3 in (c_bare3, c_bare3.replace('π', '派')):
                    _esc_sp3 = re.escape(_sp3)
                    step_item['say'] = re.sub(
                        rf'(?:等于|得数是|结果是|答案是|面积是|周长是|算出来是)\s*{_esc_sp3}\s*(?:{_MATH_UNIT_PAT})?',
                        '等于多少呢',
                        step_item.get('say') or '',
                    )
            for cand3 in cands3:
                if len(cand3) >= 2:
                    esc3 = re.escape(cand3)
                    if len(cand3) >= 4:
                        step_item['board'] = re.sub(rf'([:：=＝]|结论是|答案是|应填|应选)\s*{esc3}', r'\1 【待你辨析】', step_item.get('board') or '')
                    else:
                        step_item['board'] = re.sub(rf'(?:结论是|答案是|应填|应选)\s*{esc3}(?![A-Za-z])', '待你辨析选择', step_item.get('board') or '')
                    step_item['say'] = re.sub(rf'(?:应该填|应填|应该选|应选|答案是|正确表达是)\s*{esc3}', '留给你来选一选', step_item.get('say') or '')
                    for d3 in step_item.get('draw') or []:
                        if d3.get('label') and len(cand3) >= 4 and cand3 in d3['label']:
                            d3['label'] = re.sub(rf'([:：=＝]|结论是|答案是|应填|应选)\s*{esc3}', r'\1？', d3['label'])
                    for tf3 in ('tip', 'tip2'):
                        if qz.get(tf3) and len(cand3) >= 4 and cand3 in qz[tf3]:
                            qz[tf3] = qz[tf3].replace(cand3, '哪一项表达')
        say3_ch = str(step_item.get('say') or '').strip()
        if len(say3_ch) >= 14 and '？' not in say3_ch and '?' not in say3_ch:
            step_item['say'] = say3_ch.rstrip('。！!. ') + '，结合前面的线索你觉得应该选哪一项呢？'
    return step_item


def scrub_prior_steps_final_spoilers(steps_list: list, question_txt: str = '') -> None:
    """Retroactively scrub final numeric answers from Steps 1 & 2 if LLM leaked Step 3/4's final answer early."""
    if not isinstance(steps_list, list) or len(steps_list) < 2:
        return
    q_given = {re.sub(r'[−－]', '-', x) for x in re.findall(r'[-−－]?\d+(?:\.\d+)?(?:/[1-9]\d*)?', str(question_txt or ''))}
    final_targets = []
    for st in steps_list:
        if not isinstance(st, dict):
            continue
        sn = st.get('step')
        qz = st.get('quiz') if isinstance(st.get('quiz'), dict) else {}
        if sn == 3:
            if qz.get('num_ans'):
                final_targets.append(re.sub(r'[−－]', '-', str(qz['num_ans']).strip()))
            if qz.get('chain'):
                last_c = qz['chain'][-1]
                if isinstance(last_c, dict) and last_c.get('ans'):
                    final_targets.append(re.sub(r'[−－]', '-', str(last_c['ans']).strip()))
        elif sn == 4 and not st.get('is_qa'):
            bd4 = str(st.get('board') or '')
            for m4 in re.finditer(r'[=＝]\s*([-−－]?\d+(?:\.\d+)?(?:/[1-9]\d*)?)\s*(?:\([^()]*\)|（[^（）]*）|[a-zA-Z\u4e00-\u9fa5²³°℃/])*\s*✓', bd4):
                final_targets.append(re.sub(r'[−－]', '-', m4.group(1)))
    clean_finals = []
    for ft in final_targets:
        if ft and ft not in q_given and ft not in ('0', '1', '2', '3') and ft not in clean_finals:
            clean_finals.append(ft)
    if not clean_finals:
        return
    for st in steps_list:
        if not isinstance(st, dict) or st.get('step') not in (1, 2):
            continue
        qz12 = st.get('quiz') if isinstance(st.get('quiz'), dict) else {}
        own_ans = ''
        if qz12.get('opts') and 0 <= qz12.get('ans', 0) < len(qz12['opts']):
            own_ans, _ = _extract_opt_num_unit(qz12['opts'][qz12['ans']])
        for ft in clean_finals:
            if ft == own_ans:
                continue
            t_pat = _target_num_regex_alt(ft)
            st['board'] = re.sub(rf'([=＝≈]|答案是|结果是|面积是|得数是)\s*{t_pat}(?![0-9.])', r'\1 ？', st.get('board') or '')
            st['say'] = re.sub(
                rf'(?:最终|最后)?(?:答案是|结果是|得数是|面积是|算出来是|等于)\s*{t_pat}\s*(?:{_MATH_UNIT_PAT})?(?![0-9.])',
                '最终得数留到后面自己算',
                st.get('say') or '',
            )
            for d in st.get('draw') or []:
                if d.get('label'):
                    d['label'] = re.sub(rf'([=＝:：])\s*{t_pat}(?![0-9.])', r'\1？', d['label'])
                if d.get('op') == 'table' and isinstance(d.get('rows'), list):
                    d['rows'] = [
                        [re.sub(rf'(?:^|[=＝:：]\s*){t_pat}(?:\s*(?:{_MATH_UNIT_PAT}))?$', '待求？', str(cell)) for cell in row]
                        for row in d['rows']
                    ]
                if d.get('op') == 'vcalc' and d.get('res'):
                    d['res'] = re.sub(rf'(?:^|[=＝]\s*){t_pat}(?![0-9.])', '？', str(d['res']))


def synthesize_fallback_quiz(step_num: int, raw_obj: dict, question_txt: str = '', rng_seed: str = '') -> dict:
    """Replace rubber-stamp unlock buttons with genuine Socratic math/strategy checkpoints when LLM omits quiz."""
    board_txt = str((raw_obj or {}).get('board') or '')
    title_txt = str((raw_obj or {}).get('title') or '')
    q_given = {re.sub(r'[−－]', '-', x) for x in re.findall(r'[-−－]?\d+(?:\.\d+)?', str(question_txt or ''))}
    eq_matches = list(re.finditer(
        rf'([^\n=＝:：]{{1,20}}?)\s*[=＝]\s*([-−－]?\d+(?:\.\d+)?)\s*(?:[（(]?\s*({_MATH_UNIT_PAT}|cm²|cm|km/h|km|m²|m)[）)]?)?',
        board_txt,
    ))
    derived = [m for m in eq_matches if re.sub(r'[−－]', '-', m.group(2)) not in q_given]
    pick_m = derived[-1] if derived else (eq_matches[-1] if (eq_matches and step_num >= 2) else None)
    if pick_m:
        raw_lhs = re.sub(r'^[①②③④1-4.\s、]+', '', pick_m.group(1)).strip()
        # Strip explicit arithmetic from question stem so we don't hand out veiled formula
        if re.search(r'[×÷+\-−*/]', raw_lhs):
            lbl_m = re.match(r'^([^0-9(（]+)', raw_lhs)
            lhs_lbl = lbl_m.group(1).strip('：: ') if lbl_m and lbl_m.group(1).strip('：: ') else '这一步未知量'
        else:
            lhs_lbl = raw_lhs[:14] or '这一步结果'
        ans_num = re.sub(r'[−－]', '-', pick_m.group(2))
        u_str = (pick_m.group(3) or '').strip()
        r_opts = _build_rescue_opts(ans_num, u_str)
        if step_num == 3:
            return sanitize_quiz({
                'method': '独立动笔手算',
                'q': f'请在草稿本自己算出：{lhs_lbl}等于多少？',
                'num_ans': ans_num,
                'unit': u_str,
                'opts': r_opts,
                'ans': 0,
                'tip': '对照左侧黑板图示里的数量关系，先想清加减乘除方向再动笔算哦！',
                'tip2': '按分步关系稳稳计算，算完把得数代回原条件检验一下是否合理！',
            }, step_num=3, rng_seed=rng_seed)
        return sanitize_quiz({
            'method': '看图推算搭桥',
            'q': f'结合黑板图示线索，先推算一下「{lhs_lbl}」是多少？',
            'opts': r_opts,
            'ans': 0,
            'whys': [
                '',
                '再对照左侧图示里的份数或几何边角关系核对一下哦！',
                '注意看清是合起来还是平分/求差，代回已知条件试一试！',
            ],
            'tip': '仔细观察左侧图形里的对齐线或已知数据之间的倍数/和差关系！',
            'tip2': '把已知总量和图上的份数对应起来，一步步推出这一中间量！',
        }, step_num=step_num, rng_seed=rng_seed)

    # Subject- & writing-aware conceptual Socratic checkpoint when board has no numeric equation
    is_wr_fb = is_essay_critique_request(question_txt) or bool(detect_writing_task(question_txt))
    subj_fb = detect_subject(question_txt)
    if is_wr_fb:
        if step_num == 1:
            raw_fb = {
                'method': '审题抓题眼',
                'q': '动笔构思或升格这段文字时，第一步最关键的是什么？',
                'opts': [
                    '紧扣核心题眼词，锁定最触动人心的一幕重点详写',
                    '不看题目重心，从头到尾平铺直叙记流水账',
                    '脱离真实生活体验，生搬硬套华丽空洞的套话',
                ],
                'ans': 0,
                'whys': ['', '平铺直叙没有详略起伏，阅卷老师很难看到亮点哦！', '空喊口号缺乏真情实感，不如抓住一个真实细节打动人！'],
                'tip': '好文章贵在“以小见大”：先圈出题眼词，找准最值得放慢镜头的那一刻！',
                'tip2': '对比开头、经过与高潮，把笔墨集中在最有画面感的动作瞬间！',
            }
        elif step_num == 2:
            raw_fb = {
                'method': '病灶与衔接',
                'q': '为了让语句自然流畅、画面连贯，这一步最该留意什么？',
                'opts': [
                    '理顺主谓搭配与前后时态衔接，消除口语重复',
                    '把所有短句一逗到底连成超长句不加标点',
                    '堆砌大量意思重复的形容词掩盖内容单薄',
                ],
                'ans': 0,
                'whys': ['', '一逗到底会让句子结构混乱，断句清晰才好读哦！', '精准传神的动词比堆砌重复形容词更有力量哦！'],
                'tip': '轻声默读一遍原句，听听哪里卡顿、重复或缺少自然过渡！',
                'tip2': '先改通顺准确，再往关键处补神态、动作或环境烘托！',
            }
        else:
            raw_fb = {
                'method': '慢镜头升格',
                'q': '怎样把一句平淡的叙述升级成有画面感的高分细节句？',
                'force_choice': True,
                'opts': [
                    '拆解连续微动作并融入五感与心理变化，首尾扣题',
                    '只用“非常开心、特别难忘”等抽象总结词反复强调',
                    '偏离原本立意，强行拼凑与上下文无关的景物描写',
                ],
                'ans': 0,
                'whys': ['', '抽象大词读者看不见画面，拆出具体动作神态才能身临其境！', '一切环境与细节都要为文章核心情感服务，不能跑题哦！'],
                'tip': '试试把“怎么样”拍成电影慢镜头：手怎么动、眼里看到了什么、心里怎么想！',
                'tip2': '升级完别忘了最后一句轻轻回扣题眼词，让结构首尾圆合！',
            }
        return sanitize_quiz(raw_fb, step_num=step_num, rng_seed=rng_seed)
    if subj_fb in ('chinese', 'english', 'history', 'civics', 'biology', 'geography'):
        if step_num == 1:
            raw_fb = {
                'method': '寻证抓题眼',
                'q': f'面对这道题（{title_txt[:10] or "审题定位"}），第一步最稳妥的切入方式是什么？',
                'opts': [
                    '先圈出题干设问词与原句/材料中的关键标志线索',
                    '不看上下文语境与限定条件，凭第一印象盲目作答',
                    '忽略设问角度要求，随便摘抄一句无关原文充数',
                ],
                'ans': 0,
                'whys': ['', '脱离语境和限定词最容易掉进干扰项陷阱哦！', '答非所问拿不到采分点，先看清题目问的是什么角度！'],
                'tip': '先圈出题干里的关键词，再回到原句或材料里找准对应证据！',
                'tip2': '注意时间标志词、转折词或图例限定条件，它们就是破题钥匙！',
            }
        elif step_num == 2:
            raw_fb = {
                'method': '排除易错坑',
                'q': '结合黑板梳理的线索，推导结论前最需要辨析清楚什么？',
                'opts': [
                    '核对语法规则/学科原理与材料证据是否一一对应',
                    '只看字面眼熟就匆忙下结论，不检查前后逻辑限制',
                    '把表面现象与深层原因/主旨混为一谈',
                ],
                'ans': 0,
                'whys': ['', '出题人常设半对半错的片面干扰项，必须核对完整条件哦！', '分清表层信息与深层原理/主旨，答题才精准不漏点！'],
                'tip': '对比正确规律与常见易混点，看看哪一处细节决定了最终选择！',
                'tip2': '用排除法划掉与材料证据或语法标志矛盾的干扰项！',
            }
        else:
            raw_fb = {
                'method': '规范采分核对',
                'q': '得出结论落笔作答时，怎样确保拿满考场标准采分点？',
                'force_choice': True,
                'opts': [
                    '结合原文/语境依据并运用规范学科术语分点检验',
                    '脱离材料只写笼统套话，或不检查词形拼写与单复数',
                    '只答出一半角度就停笔，不回头核对题干设问词',
                ],
                'ans': 0,
                'whys': ['', '空套术语或拼写变形失误都会白白扣分，代回原句核对最稳！', '看清分值与设问词，确保每个采分维度都完整覆盖！'],
                'tip': '把选定答案代回原句读一读，或对照“依据+术语结论”检查是否完整！',
                'tip2': '最后瞄一眼拼写、大小写、专有名词错别字与分点序号！',
            }
        return sanitize_quiz(raw_fb, step_num=step_num, rng_seed=rng_seed)
    if step_num == 1:
        raw_fb = {
            'method': '审题抓突破口',
            'q': f'面对这道题（{title_txt[:10] or "审题分析"}），第一步最稳妥的破题动作是什么？',
            'opts': [
                '先圈出已知量与目标，找出连接两者的中间桥梁量',
                '不看单位与条件关系，直接把题目里的数字相加',
                '跳过画图和找等量关系，直接盲目拿两个数相乘',
            ],
            'ans': 0,
            'whys': [
                '',
                '不同含义或不同单位的量不能盲目相加，必须先看图找准对应关系哦！',
                '没理清谁是谁的几倍或底高对应关系前盲目相乘，最容易掉进出题人陷阱哦！',
            ],
            'tip': '波利亚解题第一步：先看清已知什么、要求什么，中间缺哪一座桥梁！',
            'tip2': '对照左侧黑板上的图示，找出哪两个已知条件能先拼出一个新的有用信息！',
        }
    elif step_num == 2:
        raw_fb = {
            'method': '辨析关键关系',
            'q': '顺着刚才的图示线索，下一步列式前最需要核对清楚什么？',
            'opts': [
                '核对已知数值对应的准确份数或垂直对应关系',
                '不用管份数有没有多算或漏算，随便挑两个数相除',
                '把题目里的“和”与“差”混为一谈直接套算式',
            ],
            'ans': 0,
            'whys': [
                '',
                '总量必须和它真正对应的份数相匹配才能相除求 1 份哦！',
                '和倍与差倍方向截然相反，一定要对照左图看清是合起来还是多出来哦！',
            ],
            'tip': '看左侧黑板的辅助线或线段标记，确认每个数字到底对应图上的哪一段！',
            'tip2': '找准“谁对应几份”或“哪条高垂直哪条底”，列式就水到渠成啦！',
        }
    else:
        raw_fb = {
            'method': '独立验算核对',
            'q': '动笔算出结果后，怎样检验自己的答案一定正确？',
            'force_choice': True,
            'opts': [
                '把算出的得数代回原题每一个已知条件逆向核对',
                '只要算式写出一个得数就行，不用检查单位和题意',
                '只看数字顺不顺眼，不代回原题验证和差倍数关系',
            ],
            'ans': 0,
            'whys': [
                '',
                '漏看单位或只答半问是最常见的丢分坑，一定要回头核对原题问题哦！',
                '代回原题做逆运算检验，才能百分百揪出计算进退位小失误哦！',
            ],
            'tip': '好习惯胜过快心算：把结果代回题目开头读一遍看看是否严丝合缝！',
            'tip2': '检查两点：① 是否每个条件都满足；② 最终单位和所问目标是否完全一致！',
        }
    return sanitize_quiz(raw_fb, step_num=step_num, rng_seed=rng_seed)


def decompose_oneshot_dump(raw_obj: dict, sess_id: str, question_txt: str = '') -> list:
    """If an LLM ignores the 4-step prompt and dumps multi-equation calculations & final answer in Step 1 without quiz,
    automatically decompose it BEFORE ingestion into 4 strict Socratic gated steps (zero answer leak!)."""
    if not isinstance(raw_obj, dict) or raw_obj.get('quiz'):
        return []
    board_txt = _coerce_multiline_str(raw_obj.get('board'), 600)
    say_txt = _coerce_multiline_str(raw_obj.get('say'), 400)
    combined = board_txt + '\n' + say_txt
    # Extract arithmetic equations like '宽：72 ÷ 6 = 12 (厘米)' or '24 × 12 = 288'
    calc_re = re.compile(
        rf'(?:([①②③④1-4.、\s]*[^\n:：=＝0-9]{{1,12}})[：:\s]*)?'
        rf'(\(?\d+(?:\.\d+)?(?:\s*[×÷+\-−＋－*/]\s*\(?\d+(?:\.\d+)?\)?)+)\s*[=＝]\s*([-−－]?\d+(?:\.\d+)?)'
        rf'\s*(?:[（(]?\s*({_MATH_UNIT_PAT}|cm²|cm|km/h|km|m²|m)[）)]?)?'
    )
    rungs = []
    seen_exprs = set()
    for m in calc_re.finditer(combined):
        raw_lbl = re.sub(r'^[①②③④1-4.、\s]+', '', (m.group(1) or '').strip()).strip('：:=＝ ')
        expr = re.sub(r'\s+', ' ', m.group(2).strip()).replace('*', '×').replace('/', '÷')
        ans_v = re.sub(r'[−－]', '-', m.group(3).strip())
        unit_v = (m.group(4) or '').strip()
        key = (expr, ans_v)
        if key not in seen_exprs:
            seen_exprs.add(key)
            rungs.append({
                'label': raw_lbl[:10] if raw_lbl else f'第{len(rungs) + 1}步量',
                'expr': expr,
                'ans': ans_v,
                'unit': unit_v,
            })
    if not rungs:
        return []
    # Only trigger decomposition when it is truly a calculation answer dump (either >=2 equations, or 1 arithmetic calc + answer language)
    if len(rungs) == 1 and not re.search(r'答[：:]|最终|答案是|结果是|所以|因此', combined):
        return []

    r0 = rungs[0]
    r_last = rungs[-1]
    base_draw = sanitize_draw_ops(raw_obj.get('draw') or [])
    if not base_draw:
        base_draw = build_step0_warmup_draw(question_txt)

    # Flip primary operator of r0['expr'] to construct realistic mathematical distractors for Step 2
    e0 = r0['expr']
    if '÷' in e0:
        w_rel1 = e0.replace('÷', '×', 1)
        w_rel2 = e0.replace('÷', '−', 1)
    elif '×' in e0:
        w_rel1 = e0.replace('×', '÷', 1)
        w_rel2 = e0.replace('×', '+', 1)
    elif '+' in e0 or '＋' in e0:
        w_rel1 = re.sub(r'[+＋]', '−', e0, count=1)
        w_rel2 = f'({e0}) × 2'
    else:
        w_rel1 = re.sub(r'[\-−－]', '+', e0, count=1)
        w_rel2 = f'({e0}) ÷ 2'

    s1_raw = {
        'step': 1,
        'title': '审题拆解 · 找破题桥梁',
        'say': "我们先不急着看最后得数，一起观察已知条件。想一想：要解开这道题，第一步应该先抓哪一个关键量作为突破口呢？",
        'board': f"已知条件梳理：{(question_txt or '结合左侧图示梳理已知量')[:88]}\n思考：第一步应该先求出哪个关键中间量？",
        'quiz': sanitize_quiz({
            'method': '逆向寻桥',
            'q': '从已知条件出发，第一步最应该先求出的突破口是什么？',
            'opts': [
                f"先抓关系求出「{r0['label']}」",
                '不分析数量关系，把题目所有数字直接连乘',
                '跳过中间桥梁，随便挑两个数直接相减当最终答案',
            ],
            'ans': 0,
            'whys': [
                '',
                '不同物理意义的量直接连乘没有实际含义，要先找准第一步桥梁量哦！',
                '跳过中间关系盲目相减最容易掉坑，先求出第一步关键量才能顺藤摸瓜！',
            ],
            'tip': '想想看：题目里的已知条件最直接能帮我们先算出哪一个未知量？',
            'tip2': f"顺着已知条件搭桥，第一步先锁定「{r0['label']}」就能打开局面啦！",
        }, step_num=1, rng_seed=sess_id),
        'draw': base_draw,
    }

    s2_raw = {
        'step': 2,
        'title': '辨析关系 · 选准运算方向',
        'say': f"找准突破口啦！那要求出「{r0['label']}」，应该怎样根据已知条件列式？仔细对比右侧三个关系，别掉进符号陷阱哦？",
        'board': f"第一步目标：求「{r0['label']}」" + (f" ⇒ 再递推求「{r_last['label']}」" if len(rungs) > 1 else '') + "\n思考：符合题意的正确算式关系是哪一个？",
        'quiz': sanitize_quiz({
            'method': '关系辨析',
            'q': f"求「{r0['label']}」时，下面哪一个列式符合题意？",
            'opts': [e0, w_rel1, w_rel2],
            'ans': 0,
            'whys': [
                '',
                '运算符号方向反啦！用代小数字法估一估，这样算出来的大小符合题意吗？',
                '这个算式把总量与部分/份数的关系搞混啦，再对照左图核对一下！',
            ],
            'tip': '先判断要求的量应该比已知数变大还是变小、是合并还是平均分！',
            'tip2': '对照题目每一句话的倍数、和差或公式含义，确认加减乘除符号！',
        }, step_num=2, rng_seed=sess_id),
        'draw': sanitize_draw_ops([{'op': 'note', 'x': 350, 'y': 392, 'label': f"关键桥梁：先理清「{r0['label']}」的数量关系"}]),
    }

    cir_nums = ('①', '②', '③', '④')
    s3_lines = []
    chain_items = []
    for idx_r, rg in enumerate(rungs[:4]):
        c_tag = cir_nums[idx_r]
        u_sfx = f" ({rg['unit']})" if rg['unit'] else ''
        s3_lines.append(f"{c_tag} {rg['label']}：{rg['expr']} = □{u_sfx}")
        chain_items.append({
            'q': f"第{c_tag}步：请算出「{rg['label']}」等于多少？",
            'ans': rg['ans'],
            'unit': rg['unit'],
            'tip': f"根据刚才理清的数量关系，在草稿纸上稳稳算出「{rg['label']}」！",
        })
    s3_raw = {
        'step': 3,
        'title': '链式留白 · 草稿本独立手算',
        'say': '关系全理顺啦！老师把算式结果都留了空白方框，现在请你在草稿纸上自己算出得数，填进右侧闯关核对吧？',
        'board': '\n'.join(s3_lines),
        'quiz': sanitize_quiz({
            'method': '分步链式手算' if len(chain_items) >= 2 else '独立手算',
            'q': '请在草稿本按步骤依次算出结果：' if len(chain_items) >= 2 else f"请在草稿本算出「{r_last['label']}」等于多少？",
            'num_ans': r_last['ans'],
            'unit': r_last['unit'],
            'chain': chain_items if len(chain_items) >= 2 else [],
            'opts': _build_rescue_opts(r_last['ans'], r_last['unit']),
            'ans': 0,
            'tip': '按步骤在草稿纸上对齐数位认真计算，注意进退位与小数点！',
            'tip2': '先算括号里再算乘除最后算加减，每算完一小步代回检查一下量级是否合理！',
        }, step_num=3, rng_seed=sess_id),
        'draw': [],
    }

    s4_lines = ['核对正确：'] + [
        f"{cir_nums[i]} {rg['label']}：{rg['expr']} = {rg['ans']}{' ' + rg['unit'] if rg['unit'] else ''} ✓"
        for i, rg in enumerate(rungs[:4])
    ] + ['检验复盘：将所得结果代回原题全部条件逆向验算无误 ✓']
    s4_raw = {
        'step': 4,
        'title': '解锁核对 · 逆向验算复盘',
        'say': "太棒啦，每一步都是你自己动脑算出来的！最终结果核对无误，养成好习惯把得数代回原题条件逆向检验一遍就满分啦！",
        'board': '\n'.join(s4_lines),
        'quiz': None,
        'draw': sanitize_draw_ops([{'op': 'note', 'x': 350, 'y': 396, 'label': f"破题复盘：先求{r0['label']}搭桥，分步手算再逆向检验！"}]),
    }

    return [
        scrub_step_spoilers(s1_raw),
        scrub_step_spoilers(s2_raw),
        scrub_step_spoilers(s3_raw),
        s4_raw,
    ]


def _is_answer_begging(q_ask: str) -> bool:
    s = str(q_ask or '').strip()
    return bool(re.search(
        r'直接告诉我|直接说答案|告诉我答案|报答案|答案是多少|答案是几|得数是多少|得数是几|结果是多少|结果是几|等于多少|等于几|选哪个|选哪一项|选啥|选[ABCabc123]|填什么|填哪个|怎么填|帮我算出来|直接给得数',
        s,
    ))


def scrub_locked_spoilers_from_qa(
    qa_dict: dict,
    unsolved_steps: list,
    formula_veiled: bool = False,
    strict_no_result: bool = False,
    ensure_question_end: bool = True,
    question_txt: str = '',
) -> dict:
    """Ensure mid-guided follow-up Q&A cannot be jailbroken into revealing current/locked step answers, Chinese numerals, option letters, or veiled formulas."""
    if not isinstance(qa_dict, dict) or (not unsolved_steps and not strict_no_result):
        return qa_dict
    q_given_nums = {re.sub(r'[−－]', '-', x) for x in re.findall(r'[-−－]?\d+(?:\.\d+)?(?:/[1-9]\d*)?', str(question_txt or ''))}
    secret_nums = []
    secret_phrases = []
    short_secret_phrases = []
    for st in (unsolved_steps or []):
        qz = st.get('quiz') if isinstance(st, dict) else None
        if isinstance(qz, dict):
            na_norm = re.sub(r'[−－]', '-', str(qz.get('num_ans') or '').strip())
            if na_norm and na_norm not in secret_nums:
                secret_nums.append(na_norm)
            for c in qz.get('chain') or []:
                ca_norm = re.sub(r'[−－]', '-', str(c.get('ans') or '').strip())
                if ca_norm and ca_norm not in secret_nums:
                    secret_nums.append(ca_norm)
            raw_ans = qz.get('ans')
            if qz.get('opts') and isinstance(raw_ans, int) and not isinstance(raw_ans, bool) and 0 <= raw_ans < len(qz['opts']):
                c_ph = str(qz['opts'][raw_ans]).strip()
                c_core = re.sub(r'^(?:[（(]\s*[A-Da-d1-4]\s*[)）]|[A-Da-d]\s*[.．、:：]|[①②③④])\s*', '', c_ph).strip()
                c_core = re.sub(r'[（(][^（）()]*[）)]\s*$', '', c_core).rstrip('。！!. ').strip()
                v_opt, _ = _extract_opt_num_unit(c_core or c_ph)
                if v_opt and v_opt not in q_given_nums and v_opt not in secret_nums:
                    secret_nums.append(v_opt)
                for ph_cand in ([c_ph, c_core] if c_core and c_core != c_ph else [c_ph]):
                    if len(ph_cand) >= 4 and ph_cand not in secret_phrases:
                        secret_phrases.append(ph_cand)
                if 2 <= len(c_core) <= 3 and not re.fullmatch(r'[-−－]?\d+(?:\.\d+)?', c_core) and c_core not in short_secret_phrases:
                    short_secret_phrases.append(c_core)
            elif isinstance(raw_ans, str) and raw_ans.strip():
                s_ans = raw_ans.strip()
                if (len(s_ans) >= 4 or re.fullmatch(r'[A-Za-z][A-Za-z\s\-\x27]{2,}', s_ans)) and s_ans not in secret_phrases:
                    secret_phrases.append(s_ans)
                elif 1 <= len(s_ans) <= 3 and not re.fullmatch(r'[-−－]?\d+(?:\.\d+)?', s_ans) and s_ans not in short_secret_phrases:
                    short_secret_phrases.append(s_ans)
        bd = str((st or {}).get('board') or '')
        for ln_bd in bd.splitlines():
            if re.match(r'^\s*(?:逆向)?(?:验算|检验|核验)[：:\s]', ln_bd):
                continue
            for m in re.finditer(r'(?:[=＝]\s*|答[：:]\s*[^\d\n]{0,10})([-−－]?\d+(?:\.\d+)?(?:/[1-9]\d*)?)\s*(?:[✓(（a-zA-Z\u4e00-\u9fa5℃°]|$)', ln_bd):
                cand_bd_num = re.sub(r'[−－]', '-', m.group(1))
                if cand_bd_num not in q_given_nums and cand_bd_num not in secret_nums:
                    secret_nums.append(cand_bd_num)
    for ph in secret_phrases:
        for field in ('say', 'board', 'note'):
            if qa_dict.get(field) and ph in str(qa_dict[field]):
                qa_dict[field] = str(qa_dict[field]).replace(ph, '这个关键条件')
    for sh_ph in short_secret_phrases:
        esc_sh = re.escape(sh_ph)
        sh_pat = rf'(?:应该|应|直接|此处|这里|横线处?|答案|结论|最终)?(?:选|选择|填|填入|填写|改为|改成|答案是|结论是|应为|就是)\s*[「“"\x27]?{esc_sh}[」”"\x27]?(?![A-Za-z])'
        for field in ('say', 'board', 'note'):
            if qa_dict.get(field):
                qa_dict[field] = re.sub(sh_pat, '【先自己辨析选一选】', str(qa_dict[field]))
    # Scrub direct option-letter giveaways (e.g. '选A', '答案是B选项', '应该选第1个')
    for field in ('say', 'board', 'note'):
        txt = str(qa_dict.get(field) or '')
        if txt:
            txt = re.sub(r'(?:应该|直接)?(?:选|选择|答案是|答案选)\s*[ABCabc](?:\s*选项)?', '先自己对比选项辨析哦', txt)
            txt = re.sub(r'(?:应该|直接)?(?:选|选择)\s*第\s*[123一二三]\s*[个项]', '先自己对比选项辨析哦', txt)
            qa_dict[field] = txt
    for num in secret_nums:
        t_pat = _target_num_regex_alt(num)
        for field in ('say', 'board', 'note'):
            txt = str(qa_dict.get(field) or '')
            if not txt:
                continue
            txt = re.sub(rf'([=＝≈]\s*){t_pat}(?![0-9.])', r'\1[自己算一算]', txt)
            txt = re.sub(
                rf'(?:(?:最终|最后)(?:的)?\s*)?(?:答案|结果|得数|解|(?:未知数\s*)?[xXyYzZaAbB](?:\s*的值)?|面积|周长|长度|体积|质量|浮力|总数|一共|差|和|宽|长|高)(?:就是|是|等于|为)\s*{t_pat}\s*(?:{_MATH_UNIT_PAT})?(?![0-9.])',
                '答案留给你自己算出哦',
                txt,
            )
            txt = re.sub(
                rf'解得\s*(?:[xXyYzZaAbB]\s*(?:等于|是|为|[=＝])\s*)?{t_pat}\s*(?:{_MATH_UNIT_PAT})?(?![0-9.])',
                '留给你自己解出哦',
                txt,
            )
            if not (len(num) == 1 and num in ('0', '1', '2', '3')):
                txt = re.sub(
                    rf'(?:就是|是|等于|为|得)\s*{t_pat}\s*(?:{_MATH_UNIT_PAT})?(?![0-9.])',
                    '留给你自己算出哦',
                    txt,
                )
            qa_dict[field] = txt
    if strict_no_result:
        # When streaming (before Step 3/4 arrived) or when child begs directly for the answer, scrub ANY '= <num>' or '答案是 <num>'!
        any_num_pat = r'(?:[-−－]?\d+(?:\.\d+)?(?:/[1-9]\d*)?|(?:负|零下)?[零一二两三四五六七八九十百千万点分之]{1,10})'
        for field in ('say', 'board', 'note'):
            txt = str(qa_dict.get(field) or '')
            if not txt:
                continue
            txt = re.sub(rf'([=＝≈]\s*){any_num_pat}(?![0-9.])', r'\1[自己算一算]', txt)
            txt = re.sub(
                rf'(?:(?:最终|最后)(?:的)?\s*)?(?:答案|结果|得数|解|(?:未知数\s*)?[xXyYzZaAbB](?:\s*的值)?)(?:就是|是|等于|为)\s*{any_num_pat}\s*(?:{_MATH_UNIT_PAT})?',
                '得数留给你自己算出哦',
                txt,
            )
            qa_dict[field] = txt
    if formula_veiled:
        for field in ('say', 'board', 'note'):
            txt = str(qa_dict.get(field) or '')
            if not txt:
                continue
            txt = re.sub(r'\d+(?:\.\d+)?\s*[×÷+\-−＋－]\s*\d+(?:\.\d+)?(?:\s*[×÷+\-−＋－]\s*\d+(?:\.\d+)?)?', '[先自己按关系列式]', txt)
            txt = re.sub(r'\d+(?:\.\d+)?\s*(?:除以|乘以|乘|加上|减去)\s*\d+(?:\.\d+)?', '按数量关系自己列式', txt)
            qa_dict[field] = txt
    if isinstance(qa_dict.get('draw'), list):
        def _scrub_draw_str(ds: str) -> str:
            out_d = str(ds or '')
            if not out_d:
                return out_d
            for ph_d in secret_phrases:
                if ph_d in out_d:
                    out_d = out_d.replace(ph_d, '？')
            for sh_d in short_secret_phrases:
                esc_sd = re.escape(sh_d)
                out_d = re.sub(rf'(?:选|填|答案是|结论是|[=＝:：])\s*[「“"\x27]?{esc_sd}[」”"\x27]?(?![A-Za-z])', '？', out_d)
            for num_d in secret_nums:
                if len(num_d) == 1 and num_d in ('0', '1', '2', '3'):
                    continue
                nd_pat = _target_num_regex_alt(num_d)
                out_d = re.sub(rf'(?:^|[=＝:：≈]\s*){nd_pat}(?:\s*(?:{_MATH_UNIT_PAT}))?(?![0-9.])', '？', out_d)
            return out_d

        for d_op in qa_dict['draw']:
            if not isinstance(d_op, dict):
                continue
            if d_op.get('label'):
                d_op['label'] = _scrub_draw_str(d_op['label'])
            if d_op.get('text'):
                d_op['text'] = _scrub_draw_str(d_op['text'])
            if d_op.get('res'):
                d_op['res'] = _scrub_draw_str(d_op['res'])
            if isinstance(d_op.get('rows'), list):
                d_op['rows'] = [[_scrub_draw_str(cell) for cell in row] if isinstance(row, list) else row for row in d_op['rows']]
    say_qa = str(qa_dict.get('say') or '').strip()
    if ensure_question_end and say_qa and '？' not in say_qa and '?' not in say_qa:
        qa_dict['say'] = say_qa.rstrip('。！!. ') + '，顺着这个线索你来试着推下一步好吗？'
    return qa_dict


SAMPLE_PROBLEMS = [
    {
        'id': 'geo_trapezoid_30',
        'cat': 'geometry',
        'grade': '几何辅助线',
        'title': '梯形作高求面积（含30°特殊角）',
        'question': '在梯形 ABCD 中，AD 平行于 BC，上底 AD = 6 厘米，下底 BC = 14 厘米，腰 AB = 10 厘米，∠B = 30°。求梯形 ABCD 的面积。',
        'golden_steps': [
            {
                'step': 1,
                'title': '画图审题 · 找缺失条件',
                'say': '同学们好！我们先画出梯形ABCD：上底6厘米，下底14厘米，斜腰AB长10厘米，角B是30度。先想一想：求梯形面积还缺哪个量？结合30度角该怎么添辅助线呢？',
                'board': '已知：AD ∥ BC\n上底 AD = 6 cm，下底 BC = 14 cm\n斜腰 AB = 10 cm，∠B = 30°\n思考：求面积还缺什么？怎么添辅助线？',
                'quiz': {
                    'method': '逆向寻高',
                    'tap_target': 'A',
                    'tap_prompt': '也可直接在左图点选作垂线的顶点',
                    'q': '求梯形面积还缺“高”，结合 ∠B=30° 怎样作辅助线最妙？',
                    'opts': ['过点A向BC作垂线AE', '直接把斜腰AB当成高', '连接对角线AC与BD'],
                    'ans': 0,
                    'whys': ['', '斜腰 AB 是倾斜的，梯形的高必须和上下底垂直成 90° 哦！', '连对角线没法直接用上 30° 特殊角求高，试试过顶点作垂线！'],
                    'tip': '想一想：梯形的“高”必须和上下底保持什么角度关系？',
                    'tip2': '从顶点 A 向下底 BC 画垂线 AE，既得到了梯形的高，左边又恰好组成含 30° 角的直角三角形！',
                },
                'draw': [
                    {'op': 'line', 'x1': 330, 'y1': 140, 'x2': 510, 'y2': 140, 'role': 'main', 'label': 'AD = 6cm'},
                    {'op': 'line', 'x1': 70, 'y1': 290, 'x2': 490, 'y2': 290, 'role': 'main', 'label': '下底 BC = 14cm'},
                    {'op': 'line', 'x1': 330, 'y1': 140, 'x2': 70, 'y2': 290, 'role': 'main', 'label': 'AB = 10cm'},
                    {'op': 'line', 'x1': 510, 'y1': 140, 'x2': 490, 'y2': 290, 'role': 'main', 'label': ''},
                    {'op': 'point', 'x': 330, 'y': 140, 'label': 'A', 'pos': 'top'},
                    {'op': 'point', 'x': 70, 'y': 290, 'label': 'B', 'pos': 'left'},
                    {'op': 'point', 'x': 490, 'y': 290, 'label': 'C', 'pos': 'right'},
                    {'op': 'point', 'x': 510, 'y': 140, 'label': 'D', 'pos': 'top'},
                    {'op': 'arc', 'x': 70, 'y': 290, 'r': 38, 'a1': -30, 'a2': 0, 'label': '30°'},
                ],
            },
            {
                'step': 2,
                'title': '动笔搭桥 · 30°直角三角形',
                'say': '思路太棒啦！过点A向下底作垂线AE，左边立刻出现直角三角形ABE！回忆一下性质：直角三角形里30度角对着的直角边AE，和斜边10厘米是什么关系？',
                'board': '作辅助线：过 A 作 AE ⊥ BC 于 E\n构造 Rt△ABE（∠AEB = 90°，∠B = 30°）\n思考：斜边 AB = 10 cm ⇒ 高 AE = ？',
                'quiz': {
                    'method': '30°半斜边性质',
                    'q': '在 Rt△ABE 中，∠B=30°，斜边 AB=10cm，高 AE 等于多少？',
                    'opts': ['5 厘米（斜边的一半）', '10 厘米（与斜边相等）', '6 厘米（与上底相等）'],
                    'ans': 0,
                    'whys': ['', '直角三角形里斜边永远是最长的边，直角边 AE 不可能等于 10 厘米哦！', '高 AE 的长度由斜边 AB 和 30° 角决定，并不等于上底 AD 的 6 厘米哦！'],
                    'tip': '回忆几何性质：含 30° 角的直角三角形中，30° 所对直角边是斜边的几分之几？',
                    'tip2': '黄金定理：30° 角所对的直角边等于斜边的一半，用 10 ÷ 2 就能算出高 AE！',
                },
                'draw': [
                    {'op': 'aux_line', 'x1': 330, 'y1': 140, 'x2': 330, 'y2': 290, 'label': '高 AE'},
                    {'op': 'point', 'x': 330, 'y': 290, 'label': 'E', 'pos': 'bottom'},
                    {'op': 'right_angle', 'x': 330, 'y': 290, 'dx': -1, 'dy': -1, 'size': 15},
                ],
            },
            {
                'step': 3,
                'title': '列式留白 · 草稿本独立算得数',
                'say': '完全正确，高AE就是5厘米！现在上底6、下底14、高5都找齐了。老师在黑板上留了空，请先在草稿纸上自己算出面积，把得数填进右侧核对吧！',
                'board': '已推出：高 AE = 10 ÷ 2 = 5 (cm)\n梯形面积公式：S = (上底 + 下底) × 高 ÷ 2\n代入列式：S = (6 + 14) × 5 ÷ 2 = □ (cm²)',
                'quiz': {
                    'method': '梯形公式手算',
                    'q': '请在草稿本算出梯形面积：(6 + 14) × 5 ÷ 2 等于多少？',
                    'num_ans': '50',
                    'unit': '平方厘米',
                    'opts': ['50 平方厘米', '100 平方厘米', '40 平方厘米'],
                    'ans': 0,
                    'tip': '先算括号里上底与下底的和，乘上高之后，千万别漏掉梯形公式最后还要平分哦！',
                    'tip2': '分步拆解：6 + 14 = 20，20 × 5 = 100，最后用 100 ÷ 2 算出得数填进去！',
                },
                'draw': [
                    {'op': 'poly_fill', 'pts': [[330, 140], [70, 290], [330, 290]], 'role': 'accent'},
                    {'op': 'brace', 'x1': 352, 'y1': 140, 'x2': 352, 'y2': 290, 'label': 'AE = 5cm'},
                ],
            },
            {
                'step': 4,
                'title': '解锁验算 · 总结破题口诀',
                'say': '算得又快又准！20乘5除以2正好是50平方厘米！逆向验算：50乘2除以高5等于20，正好等于上底6加下底14！以后遇到30度特殊角，作高构造直角三角形就秒解啦！',
                'board': '核对正确：S = (6 + 14) × 5 ÷ 2 = 50 (cm²) ✓\n逆向验算：50 × 2 ÷ 5 = 20 = 6 + 14 ✓\n母题通法：见 30°/45° 特殊角 → 作垂线构造 Rt△',
                'draw': [
                    {'op': 'poly_fill', 'pts': [[330, 140], [70, 290], [490, 290], [510, 140]], 'role': 'solve'},
                    {'op': 'note', 'x': 340, 'y': 382, 'label': '破题钥匙：见 30° 角作高 AE = 5cm，切勿误把斜腰 10cm 当高！'},
                ],
            },
        ],
    },
    {
        'id': 'word_meet_ratio',
        'cat': 'word',
        'grade': '行程与倍数',
        'title': '两车相遇与速度倍数线段图',
        'question': '甲、乙两车同时从相距 420 千米的两地相对开出，3 小时后相遇。已知甲车的速度是乙车的 1.5 倍，求甲、乙两车每小时各行多少千米？',
        'golden_steps': [
            {
                'step': 1,
                'title': '画相遇路线 · 先抓1小时速度和',
                'say': '我们先画长线段表示两地相距420千米，甲乙相对开出3小时相遇。别急着管1.5倍，先想第一步：两车3小时合走了420千米，那1小时合走多少千米？',
                'board': '已知：总路程 = 420 千米，相遇时间 = 3 小时\n倍数关系：甲速 = 乙速 × 1.5\n第一步思考：两车 1 小时合走多少千米（速度和）？',
                'quiz': {
                    'method': '先抓速度和',
                    'q': '两车 3 小时合走 420 千米，第一步先求出的「速度和」是多少？',
                    'opts': ['140 千米/时（420 ÷ 3）', '280 千米/时（420 ÷ 1.5）', '210 千米/时（420 ÷ 2）'],
                    'ans': 0,
                    'whys': ['', '420 千米是 3 小时的总路程，不能直接拿路程除以速度倍数 1.5 哦！', '是 3 小时相遇不是 2 小时，而且两车快慢不同不能平分路程哦！'],
                    'tip': '相遇问题黄金第一步：用“总路程 ÷ 相遇时间”先算出两车每小时合走多少千米！',
                    'tip2': '把 420 千米平均分到 3 个小时里，算一算 420 ÷ 3 等于多少？',
                },
                'draw': [
                    {'op': 'line', 'x1': 80, 'y1': 125, 'x2': 580, 'y2': 125, 'role': 'main', 'label': ''},
                    {'op': 'point', 'x': 80, 'y': 125, 'label': '甲地A', 'pos': 'top'},
                    {'op': 'point', 'x': 580, 'y': 125, 'label': '乙地B', 'pos': 'top'},
                    {'op': 'point', 'x': 380, 'y': 125, 'label': '相遇C', 'pos': 'top'},
                    {'op': 'arrow', 'x1': 80, 'y1': 92, 'x2': 368, 'y2': 92, 'role': 'aux', 'label': '甲行3小时'},
                    {'op': 'arrow', 'x1': 580, 'y1': 92, 'x2': 392, 'y2': 92, 'role': 'main', 'label': '乙行3小时'},
                    {'op': 'brace', 'x1': 80, 'y1': 152, 'x2': 580, 'y2': 152, 'label': '总路程 420 千米'},
                ],
            },
            {
                'step': 2,
                'title': '画份数线段图 · 辨析和倍份数',
                'say': '没错，速度和是140千米每时！接下来把较慢的乙车画成1份，甲车画成1.5份。仔细看图：速度和140对应的是合起来几份？最容易算错成几份？',
                'board': '已算出：速度和 = 420 ÷ 3 = 140 (km/h)\n画线段图：乙速 = 1 份，甲速 = 1.5 份\n思考：140 km/h 对应一共几份？该怎样列式求 1 份？',
                'quiz': {
                    'method': '和倍份数合一',
                    'q': '乙是 1 份、甲是 1.5 份，速度和 140 km/h 对应几份？怎样求乙速？',
                    'opts': ['共 2.5 份，列式 140 ÷ (1 + 1.5)', '共 1.5 份，列式 140 ÷ 1.5', '差 0.5 份，列式 140 ÷ (1.5 − 1)'],
                    'ans': 0,
                    'whys': ['', '掉进和倍经典陷阱啦！1.5 份只是甲车，140 里还包含乙车的 1 份，必须加 1！', '140 是两车速度之“和”不是速度之“差”，份数应该相加而不是相减哦！'],
                    'tip': '想一想：140 是甲车速度加乙车速度的“总和”，份数也应该把两人合起来哦！',
                    'tip2': '乙 1 份 + 甲 1.5 份 = 2.5 份，所以求 1 份乙速要用 140 ÷ 2.5！',
                },
                'draw': [
                    {'op': 'seg_bar', 'x': 124, 'y': 245, 'w': 170, 'parts': 1, 'label': '乙速：1 份'},
                    {'op': 'seg_bar', 'x': 124, 'y': 308, 'w': 255, 'parts': 3, 'role': 'aux', 'label': '甲速：1.5 份'},
                    {'op': 'aux_line', 'x1': 294, 'y1': 245, 'x2': 294, 'y2': 308, 'label': '多0.5份'},
                ],
            },
            {
                'step': 3,
                'title': '链式留白 · 依次算出乙速与甲速',
                'say': '跨过陷阱啦！两车合起来共2.5份，对应速度和140千米每时。现在请你自己先想列式、再在草稿本分两小步算出乙车和甲车的速度吧！',
                'board': '总份数 = 1 + 1.5 = 2.5 (份)\n① 乙车速度：140 ÷ 2.5 = □ (km/h)\n② 甲车速度：乙速 × 1.5 = □ (km/h)',
                'quiz': {
                    'method': '分步链式手算',
                    'q': '请在草稿本依次算出乙车和甲车的速度：',
                    'num_ans': '84',
                    'unit': '千米/时',
                    'chain': [
                        {'q': '第①步：先求 1 份「乙车速度」140 ÷ 2.5 = ？', 'ans': '56', 'unit': '千米/时', 'tip': '回想第 2 步线段图：两车的速度和正好对应合起来的总份数，求 1 份该用什么运算？'},
                        {'q': '第②步：再求 1.5 份「甲车速度」56 × 1.5 = ？', 'ans': '84', 'unit': '千米/时', 'tip': '已经算出 1 份乙车速度啦，甲车速度是乙车的倍数量，求几倍该怎么列式？'},
                    ],
                    'opts': ['84 千米/时', '90 千米/时', '76 千米/时'],
                    'ans': 0,
                    'tip': '小数除法巧算：把被除数 140 和除数 2.5 同时乘 2，除数就变成整数 5 啦！',
                    'tip2': '① 乙速：把 140 ÷ 2.5 转化为 280 ÷ 5 = ？；② 甲速：把 1.5 倍拆成“1 倍乙速 + 半倍乙速”相加！',
                },
                'draw': [
                    {'op': 'brace', 'x1': 405, 'y1': 238, 'x2': 405, 'y2': 318, 'label': '共 2.5 份 = 140 km/h'},
                ],
            },
            {
                'step': 4,
                'title': '求出甲速并双验算 · 和倍模型复盘',
                'say': '算得真准！乙车每小时56千米，甲车是56乘1.5等于84千米！代回原题双检验：84是56的1.5倍，两车合走140乘3正好420千米！',
                'board': '核对正确：乙速 = 56 km/h ⇒ 甲速 = 56 × 1.5 = 84 (km/h) ✓\n检验1：84 ÷ 56 = 1.5 倍 ✓\n检验2：(84 + 56) × 3 = 420 (km) ✓',
                'draw': [
                    {'op': 'note', 'x': 340, 'y': 392, 'label': '相遇和倍通法：先用 总路程÷时间 求速度和，再除以 (1+倍数) 得1份'},
                ],
            },
        ],
    },
    {
        'id': 'geo_cut_paste_leaf',
        'cat': 'geometry',
        'grade': '几何割补法',
        'title': '正方形与扇形重叠求叶形阴影面积',
        'question': '已知正方形 ABCD 的边长为 10 厘米，分别以点 B 和点 D 为圆心、10 厘米为半径在正方形内画圆弧，求中间重叠的“树叶形”阴影面积。（π取3.14）',
        'golden_steps': [
            {
                'step': 1,
                'title': '画正方形与圆弧 · 寻找对称分割线',
                'say': '我们先画出边长10厘米的正方形和两条四分之一圆弧。中间这片树叶没有现成面积公式，观察对称性，连哪条辅助线能把树叶平分成完全相同的两半？',
                'board': '已知：正方形边长 a = 10 cm\n两段圆弧均为半径 r = 10 cm 的 ¼ 圆\n思考：连哪条辅助线能把“树叶”对称一分为二？',
                'quiz': {
                    'method': '对称轴分割',
                    'tap_target': 'A|C',
                    'tap_prompt': '也可直接在左图点选树叶尖端所在的顶点',
                    'q': '连接哪一条辅助线，可以把中间的“树叶”平分成完全相等的两片半叶？',
                    'opts': ['连接对角线 AC（穿过叶尖）', '连接对角线 BD（穿过圆心）', '连接上下两条边的中点'],
                    'ans': 0,
                    'whys': ['', '连 BD 是横穿叶肚，分出来的两边不是扇形减三角形的标准弓形哦！', '连中点会把圆弧切成不规则碎片，试试把树叶的两个尖角连起来！'],
                    'tip': '观察树叶的两个尖端刚好落在哪两个顶点上？',
                    'tip2': '树叶的两个尖角正好是顶点 A 和顶点 C，连接对角线 AC 就能把树叶沿叶脉对半劈开！',
                },
                'draw': [
                    {'op': 'rect', 'x': 210, 'y': 75, 'w': 240, 'h': 240, 'role': 'main', 'label': '边长 10cm'},
                    {'op': 'point', 'x': 210, 'y': 75, 'label': 'A', 'pos': 'left'},
                    {'op': 'point', 'x': 210, 'y': 315, 'label': 'B', 'pos': 'left'},
                    {'op': 'point', 'x': 450, 'y': 315, 'label': 'C', 'pos': 'right'},
                    {'op': 'point', 'x': 450, 'y': 75, 'label': 'D', 'pos': 'right'},
                    {'op': 'arc', 'x': 210, 'y': 315, 'r': 240, 'a1': -90, 'a2': 0, 'label': '扇形B'},
                    {'op': 'arc', 'x': 450, 'y': 75, 'r': 240, 'a1': 90, 'a2': 180, 'label': '扇形D'},
                ],
            },
            {
                'step': 2,
                'title': '连对角线AC · 拆解半片树叶',
                'say': '好眼力！连上对角线AC后，我们只看圆心在左下角B点的四分之一圆扇形ABC：怎样用两个规则图形相减，求出其中半片树叶的面积？',
                'board': '作辅助线：连接对角线 AC\n聚焦以 B 为圆心、半径 10 cm 的 ¼ 圆扇形 ABC\n思考：半片树叶（弓形）面积 = 谁 − 谁？',
                'quiz': {
                    'method': '弓形割补相减',
                    'q': '聚焦左下角圆心 B 所在的图形，半片树叶（弓形）面积应该怎么求？',
                    'opts': ['¼ 圆扇形 ABC − 直角△ABC', '正方形 ABCD − ¼ 圆扇形 ABC', '¼ 圆扇形 ABC 面积直接除以 2'],
                    'ans': 0,
                    'whys': ['', '正方形减 ¼ 圆得到的是右上角的空白角，不是中间的半片树叶哦！', '弧线是弯曲鼓出去的，弓形和三角形并不一样大，不能直接除以 2 哦！'],
                    'tip': '看左下角：整个 ¼ 圆扇形 ABC 是由“直角三角形 ABC”和“半片树叶”拼成的！',
                    'tip2': '用 ¼ 圆扇形面积减去左下角直角三角形 ABC 的面积，剩下的恰好就是半片树叶！',
                },
                'draw': [
                    {'op': 'aux_line', 'x1': 210, 'y1': 75, 'x2': 450, 'y2': 315, 'label': '对角线 AC'},
                ],
            },
            {
                'step': 3,
                'title': '链式留白 · 分三小步求树叶面积',
                'say': '拆解大成功！为了稳扎稳打不跳步，请你按黑板上的①扇形、②半片叶、③整片树叶三小步，自己在草稿本依次列式算出结果吧！',
                'board': '① ¼ 圆扇形 ABC：3.14 × 10² ÷ 4 = □ (cm²)\n② 半片叶（弓形）：S扇 − (10×10÷2) = □ (cm²)\n③ 整片树叶阴影：半片叶 × 2 = □ (cm²)',
                'quiz': {
                    'method': '分步链式手算',
                    'q': '请在草稿本按三小步依次算出整片树叶面积：',
                    'num_ans': '57',
                    'unit': '平方厘米',
                    'chain': [
                        {'q': '第①步：半径 10 厘米的 ¼ 圆扇形面积：3.14 × 100 ÷ 4 = ？', 'ans': '78.5', 'unit': '平方厘米', 'tip': '扇形是整圆的四分之一：先按圆面积公式 πr² 算整圆，再平分成四份！'},
                        {'q': '第②步：减去直角△ABC 面积后，半片叶：78.5 − 50 = ？', 'ans': '28.5', 'unit': '平方厘米', 'tip': '回想第 2 步割补结论：用左下角 ¼ 圆扇形面积减去等腰直角三角形面积！'},
                        {'q': '第③步：两片半叶拼成整片树叶阴影：28.5 × 2 = ？', 'ans': '57', 'unit': '平方厘米', 'tip': '整片树叶由完全对称的两片半叶拼成，把半片叶面积合起两份即可！'},
                    ],
                    'opts': ['57 平方厘米', '28.5 平方厘米', '43 平方厘米'],
                    'ans': 0,
                    'tip': '按 ①扇形 → ②半片叶 → ③整片叶 一步步稳扎稳打！',
                    'tip2': '① 先算 314 ÷ 4 得 ¼ 圆面积；② 减去直角△面积 (10×10÷2=50) 得半叶；③ 最后乘 2 拼回整叶！',
                },
                'draw': [
                    {'op': 'poly_fill', 'pts': [[210, 75], [210, 315], [450, 315]], 'role': 'accent'},
                ],
            },
            {
                'step': 4,
                'title': '解锁双法验算 · 掌握容斥秒杀',
                'say': '太棒啦，正是57平方厘米！再教你一招容斥法交叉验算：两个78.5的扇形叠在100的正方形里，重叠的树叶被盖了两次，用157减100也是57！',
                'board': '核对正确：(78.5 − 50) × 2 = 57 (cm²) ✓\n容斥法交叉验算：2 × S扇 − S正 = 157 − 100 = 57 (cm²) ✓',
                'draw': [
                    {'op': 'note', 'x': 340, 'y': 386, 'label': '割补+容斥双通法：叶形面积 = 2×(¼圆 − 直角△) = 两个¼圆 − 正方形'},
                ],
            },
        ],
    },
    {
        'id': 'geo_triangle_ext',
        'cat': 'geometry',
        'grade': '几何角度',
        'title': '凹四边形飞镖模型作连接辅助线',
        'question': '如图，在凹四边形 ABCD 中，∠A = 35°，∠B = 25°，∠C = 30°，求凹角 ∠BDC 的度数。',
        'golden_steps': [
            {
                'step': 1,
                'title': '观察飞镖图形 · 思考分割转化',
                'say': '大家看这个像纸飞机一样的凹四边形ABCD，已知角A是35度、角B是25度、角C是30度。凹四边形不能直接套公式，怎么画一条辅助线把它拆成两个三角形？',
                'board': '已知：凹四边形 ABCD\n∠BAC = 35°，∠B = 25°，∠C = 30°\n思考：怎样添辅助线把凹角拆成两个三角形的外角？',
                'quiz': {
                    'method': '飞镖分割转化',
                    'tap_target': 'D',
                    'tap_prompt': '也可直接在左图点选需要穿过的凹角顶点',
                    'q': '怎样添一条辅助线，能最巧妙地利用「三角形外角定理」求 ∠BDC？',
                    'opts': ['连接 AD 并向下延长至点 E', '只取腰 AB 的中点', '连接 BC 把凹角封起来但不知底角'],
                    'ans': 0,
                    'whys': ['', '取边中点无法把角度关联起来，角度问题优先考虑连顶点或延长线哦！', '连 BC 虽然能封成大三角形，但少了两步直接出外角的简便性，试试穿过 D 画延长线！'],
                    'tip': '试试从顶角 A 出发，穿过凹进去的顶点 D 继续向下画延长线！',
                    'tip2': '连接 AD 并延长到 E，不仅把图形分成了左右两个三角形，还把凹角 ∠BDC 刚好劈成了两个外角！',
                },
                'draw': [
                    {'op': 'polygon', 'pts': [[330, 70], [140, 335], [330, 235], [520, 335]], 'role': 'main'},
                    {'op': 'point', 'x': 330, 'y': 70, 'label': 'A(35°)', 'pos': 'top'},
                    {'op': 'point', 'x': 140, 'y': 335, 'label': 'B(25°)', 'pos': 'left'},
                    {'op': 'point', 'x': 520, 'y': 335, 'label': 'C(30°)', 'pos': 'right'},
                    {'op': 'point', 'x': 330, 'y': 235, 'label': 'D', 'pos': 'top'},
                ],
            },
            {
                'step': 2,
                'title': '延长AD至E · 运用三角形外角定理',
                'say': '漂亮！连接AD延长到E，顶角A被分成了左角1和右角2。只看左边三角形ABD：下方的外角BDE等于哪两个内角之和呢？',
                'board': '作辅助线：连接 AD 并延长至 E（∠BAC = ∠1 + ∠2）\n聚焦左侧 △ABD：\n思考：外角 ∠BDE 等于哪两个角相加？',
                'quiz': {
                    'method': '三角形外角定理',
                    'q': '根据三角形外角性质，左侧外角 ∠BDE 等于什么？',
                    'opts': ['∠B + 左顶角 ∠1', '180° − ∠B', '∠B + 右侧 ∠C'],
                    'ans': 0,
                    'whys': ['', '180° 减的是和外角相邻的内角 ∠ADB，而不是远处的 ∠B 哦！', '∠C 在右边的三角形里，不在左边 △ABD 中哦！'],
                    'tip': '回忆定理：三角形的一个外角，等于与它“不相邻”的两个内角之和！',
                    'tip2': '先遮住右边只看左侧 △ABD：找出与外角 ∠BDE 相邻的 ∠ADB，另外那两个不相邻内角是谁？',
                },
                'draw': [
                    {'op': 'aux_line', 'x1': 330, 'y1': 70, 'x2': 330, 'y2': 365, 'label': '延长线 AE'},
                    {'op': 'point', 'x': 330, 'y': 365, 'label': 'E', 'pos': 'bottom'},
                ],
            },
            {
                'step': 3,
                'title': '左右外角合并 · 动笔算出凹角度数',
                'say': '完全正确！左外角是角B加角1，同理右外角是角C加角2。把左右合起来，角1加角2正好拼回完整的角A！快算出凹角BDC等于多少度？',
                'board': '左外角 ∠BDE = ∠B + ∠1，右外角 ∠CDE = ∠C + ∠2\n合并得：∠BDC = ∠B + ∠C + (∠1 + ∠2)\n代入计算：∠BDC = 25° + 30° + 35° = □°',
                'quiz': {
                    'method': '凑整手算',
                    'q': '请算出凹角 ∠BDC 的度数：25 + 30 + 35 等于多少度？',
                    'num_ans': '90',
                    'unit': '°',
                    'opts': ['90°', '80°', '95°'],
                    'ans': 0,
                    'tip': '回想双外角结论：凹角等于三个尖角度数之和，计算时可先把尾数能凑整十的两个角合起来！',
                    'tip2': '25 + 35 = 60，再用 60 + 30 算出度数填进去吧！',
                },
                'draw': [
                    {'op': 'poly_fill', 'pts': [[330, 70], [140, 335], [330, 235]], 'role': 'accent'},
                    {'op': 'arc', 'x': 330, 'y': 235, 'r': 32, 'a1': 28, 'a2': 152, 'label': '外角和'},
                ],
            },
            {
                'step': 4,
                'title': '解锁结论 · 飞镖模型通用规律',
                'say': '答对啦，正好是90度！这就是几何里超好用的飞镖模型：凹角永远等于三个尖角的度数之和，以后遇到飞镖图形也能自己推出来啦！',
                'board': '核对正确：∠BDC = 25° + 30° + 35° = 90° ✓\n模型结论：飞镖凹角 ∠BDC = ∠A + ∠B + ∠C',
                'draw': [
                    {'op': 'poly_fill', 'pts': [[330, 70], [140, 335], [330, 235], [520, 335]], 'role': 'solve'},
                    {'op': 'note', 'x': 330, 'y': 400, 'label': '飞镖模型核心：连顶点延长劈成双外角，凹角 = 三个尖角之和'},
                ],
            },
        ],
    },
    {
        'id': 'word_diff_mult',
        'cat': 'word',
        'grade': '差倍应用题',
        'title': '图书角故事书与科技书（几倍少几）',
        'question': '班级图书角里，故事书的本数比科技书的 3 倍少 12 本，且故事书比科技书多 48 本。科技书和故事书各有多少本？',
        'golden_steps': [
            {
                'step': 1,
                'title': '画1倍线段 · 跨过“补齐变差”陷阱',
                'say': '把科技书画成1份，故事书比3整份少12本。注意最容易错的第一关：如果借给故事书12本把它补满3整份，这时故事书比科技书的差会变大还是变小？变成多少本？',
                'board': '设科技书 = 1 份，故事书 = 3 份 − 12 本\n已知实际差：故事书 − 科技书 = 48 本\n思考：给故事书“补上 12 本”凑满 3 份后，两书相差几本？',
                'quiz': {
                    'method': '补少变整份',
                    'q': '本来故事书比科技书多 48 本，再借给故事书 12 本凑满 3 份后，两书相差几本？',
                    'opts': ['差距变大，相差 60 本', '差距变小，相差 36 本', '差距不变，还是 48 本'],
                    'ans': 0,
                    'whys': ['', '小心掉坑！12 本是补在原本就更长的“故事书”后面，故事书更长了，差是加 12 不是减 12 哦！', '科技书没动，故事书增加了 12 本，两书的差距也会跟着增加 12 本哦！'],
                    'tip': '看黑板上的红色虚线：12 本是接在更长的“故事书”右端往右延长哦！',
                    'tip2': '科技书不动，原本更长的故事书再往右接长 12 本，在原差 48 本基础上应该加 12 还是减 12？',
                },
                'draw': [
                    {'op': 'seg_bar', 'x': 124, 'y': 130, 'w': 130, 'parts': 1, 'label': '科技书:1份'},
                    {'op': 'seg_bar', 'x': 124, 'y': 225, 'w': 330, 'parts': 1, 'role': 'main', 'label': '故事书实际'},
                    {'op': 'aux_line', 'x1': 454, 'y1': 225, 'x2': 514, 'y2': 225, 'label': '补12本'},
                    {'op': 'brace', 'x1': 124, 'y1': 192, 'x2': 514, 'y2': 192, 'label': '补齐后满 3 份'},
                ],
            },
            {
                'step': 2,
                'title': '画对齐虚线 · 找60本对应几份',
                'say': '没掉进减法陷阱，太赞了！补齐后满3份的故事书比1份科技书整整多了60本。看红色对齐虚线右边：多出来的60本对应几整份？',
                'board': '补齐后新差 = 48 + 12 = 60 (本)\n科技书 = 1 份，补齐后故事书 = 3 份\n思考：多出的 60 本对应几份？怎样求 1 份科技书？',
                'quiz': {
                    'method': '对齐消同求差',
                    'q': '满 3 份比 1 份多出来的 60 本对应几份？1 份科技书怎么列式？',
                    'opts': ['对应 2 份，用 60 ÷ (3 − 1)', '对应 3 份，用 60 ÷ 3', '对应 4 份，用 60 ÷ (3 + 1)'],
                    'ans': 0,
                    'whys': ['', '3 份里左边那 1 份和科技书一样长抵消了，右边多出来的只有 3 − 1 = 2 份哦！', '60 本是两书的“差”不是“和”，差倍问题要除以倍数减 1 哦！'],
                    'tip': '看黑板红色对齐虚线右边：3 整份减去左边对齐的 1 份，右边多出来几份？',
                    'tip2': '右边多出来 3 − 1 = 2 份，对应 60 本，所以求 1 份科技书就用 60 ÷ (3 − 1) 列式！',
                },
                'draw': [
                    {'op': 'aux_line', 'x1': 254, 'y1': 120, 'x2': 254, 'y2': 275, 'label': '1份对齐'},
                    {'op': 'brace', 'x1': 254, 'y1': 260, 'x2': 454, 'y2': 260, 'label': '原多 48 本'},
                    {'op': 'brace', 'x1': 254, 'y1': 315, 'x2': 514, 'y2': 315, 'label': '补齐后多 60 本'},
                ],
            },
            {
                'step': 3,
                'title': '链式留白 · 先求科技书再求故事书',
                'say': '思路全通啦！现在我们在草稿本分两小步算：先求1份科技书有多少本，再还回借来的12本算出故事书实际本数！',
                'board': '补齐后新差 = 48 + 12 = 60 (本)\n① 科技书（1份）：60 ÷ (3 − 1) = □ (本)\n② 故事书实际：科技书 × 3 − 12 = □ (本)',
                'quiz': {
                    'method': '分步链式手算',
                    'q': '请依次算出科技书与故事书的实际本数：',
                    'num_ans': '78',
                    'unit': '本',
                    'chain': [
                        {'q': '第①步：先求 1 份「科技书」60 ÷ 2 = ？', 'ans': '30', 'unit': '本', 'tip': '对照左侧对齐虚线：用补齐后的新差除以多出来的份数，就能求出 1 份科技书！'},
                        {'q': '第②步：再求「故事书实际」30 × 3 − 12 = ？', 'ans': '78', 'unit': '本', 'tip': '算出满 3 份的本数后，记得把刚才借来凑整的本数还回去（或直接用科技书加上原差）！'},
                    ],
                    'opts': ['78 本', '90 本', '66 本'],
                    'ans': 0,
                    'tip': '如果算出了 90 本，说明忘了把刚才借来凑整的 12 本还回去哦！',
                    'tip2': '① 先把补齐后的新差 60 平均分成 2 份求科技书；② 再用科技书乘 3 减去借来的 12 本（或科技书加 48）！',
                },
                'draw': [
                    {'op': 'point', 'x': 189, 'y': 130, 'label': '1份', 'pos': 'top'},
                ],
            },
            {
                'step': 4,
                'title': '双条件验算 · 差倍补齐总结',
                'say': '完全正确，科技书30本、故事书78本！把答案代回原题两个条件双验算：78减30等于48本，30乘3减12也是78本，一分不差！',
                'board': '核对正确：科技书 30 本，故事书 78 本 ✓\n验算差：78 − 30 = 48 (本) ✓\n验算倍：30 × 3 − 12 = 78 (本) ✓',
                'draw': [
                    {'op': 'note', 'x': 330, 'y': 392, 'label': '几倍少几通法：先把“少几”补齐使差变大，再用 新差 ÷ (倍数−1) 求1份'},
                ],
            },
        ],
    },
    {
        'id': 'word_chicken_rabbit',
        'cat': 'word',
        'grade': '思维经典',
        'title': '鸡兔同笼抬腿假设图示法',
        'question': '笼子里有若干只鸡和兔。从上面数有 10 个头，从下面数有 28 只脚。鸡和兔各有几只？',
        'golden_steps': [
            {
                'step': 1,
                'title': '画10个头 · 假设全是2脚小鸡',
                'say': '10个头说明鸡和兔一共10只。我们先用经典的“假设法”，假设这10只全都是2只脚的小鸡：图上一共只有几只脚？比题目实际的28只脚少算了几只？',
                'board': '已知：总头数 = 10 只，实际总脚数 = 28 只\n第一步假设：10 只全都是 2 脚鸡\n思考：假设脚数是多少？比 28 只脚少算了几只？',
                'quiz': {
                    'method': '极限全鸡假设',
                    'q': '假设 10 只全是 2 脚鸡，共几只脚？比实际 28 只脚少算了几只？',
                    'opts': ['共 20 只脚，少算了 8 只', '共 20 只脚，少算了 18 只', '共 40 只脚，多算了 12 只'],
                    'ans': 0,
                    'whys': ['', '28 减 20 个位是 8，十位 2 减 2 是 0，少算的是 8 只不是 18 只哦！', '40 只脚是“假设全是 4 脚兔”的算法，现在我们假设全是 2 脚鸡哦！'],
                    'tip': '每只鸡 2 只脚，先算 10 × 2 得假设总脚数，再和实际 28 只脚求差！',
                    'tip2': '先算 10 × 2 得假设总脚数，再用实际 28 只脚减去假设脚数看看少算了几只！',
                },
                'draw': [
                    {'op': 'seg_bar', 'x': 90, 'y': 145, 'w': 500, 'parts': 10, 'label': '假设 10 只全是鸡（每格先画 2 只脚）'},
                    {'op': 'brace', 'x1': 90, 'y1': 185, 'x2': 590, 'y2': 185, 'label': '共 10 个头，实际总脚数 28 只'},
                ],
            },
            {
                'step': 2,
                'title': '追根溯源 · 辨清补脚置换谁',
                'say': '答对啦，少算了8只脚！深入想一步：每把1只2脚鸡换回4脚兔，要补上几只脚？用总脚差8除以单只补脚数，第一步先算出来的是兔子还是小鸡？',
                'board': '总脚差 = 28 − 20 = 8 (只脚)\n思考1：1 只鸡换成 1 只兔，脚数增加几只？\n思考2：总脚差 ÷ 单只脚差，先求出的是谁的只数？',
                'quiz': {
                    'method': '脚差置换原理',
                    'q': '每把 1 只鸡换回兔要补几只脚？用 8 除以它先算出的是谁？',
                    'opts': ['每只补 2 脚，8 ÷ (4−2) 先算出「兔子」', '每只补 4 脚，8 ÷ 4 先算出「兔子」', '每只补 2 脚，8 ÷ (4−2) 先算出「小鸡」'],
                    'ans': 0,
                    'whys': ['', '每格原本已经画了 2 只脚，只需要再补 4 − 2 = 2 只脚就满 4 脚啦，不能除以 4 哦！', '注意最经典陷阱！一开始假设全是鸡，补上脚变成 4 脚动物的正是“兔子”而非小鸡哦！'],
                    'tip': '原来每只已经有 2 只脚了，再补几只脚就变成 4 脚动物（兔子）？',
                    'tip2': '每只补 4 − 2 = 2 只脚就变成兔子，所以总脚差 ÷ 2 第一步算出来的一定是兔子只数！',
                },
                'draw': [
                    {'op': 'seg_bar', 'x': 90, 'y': 275, 'w': 200, 'parts': 4, 'role': 'aux', 'label': '每格补 +2 只脚变兔子'},
                    {'op': 'aux_line', 'x1': 290, 'y1': 145, 'x2': 290, 'y2': 305, 'label': '换兔分界'},
                ],
            },
            {
                'step': 3,
                'title': '链式留白 · 先定兔子再求小鸡',
                'say': '找到换兔原理啦！现在分两小步收官：先算出补脚换出了几只兔子，再用10个头减去兔子得到小鸡只数！',
                'board': '总脚差 = 28 − 20 = 8 (只脚)\n① 先换出兔子：8 ÷ (4 − 2) = □ (只)\n② 再求小鸡：10 − 兔 = □ (只)',
                'quiz': {
                    'method': '分步链式手算',
                    'q': '请依次算出兔子和鸡各有多少只：',
                    'num_ans': '6',
                    'unit': '只',
                    'chain': [
                        {'q': '第①步：8 只脚每只补 2 脚，换出几只「兔子」？', 'ans': '4', 'unit': '只', 'tip': '看总脚差里面包含几个“每只鸡换兔要补的脚数”，就能求出换出了几只兔子！'},
                        {'q': '第②步：共 10 个头，减去 4 只兔，剩几只「鸡」？', 'ans': '6', 'unit': '只', 'tip': '鸡和兔合起来的总头数已知，去掉刚求出的兔子只数，剩下的就是小鸡啦！'},
                    ],
                    'opts': ['6 只', '4 只', '5 只'],
                    'ans': 0,
                    'tip': '假设全是鸡时，第一步除出来的一定是兔子，第二步相减才是鸡！',
                    'tip2': '① 先用总脚差 8 除以单只脚差 2 换出兔子只数；② 再用总头数 10 减去兔子得小鸡！',
                },
                'draw': [
                    {'op': 'brace', 'x1': 90, 'y1': 320, 'x2': 290, 'y2': 320, 'label': '补脚换兔区'},
                    {'op': 'brace', 'x1': 290, 'y1': 225, 'x2': 590, 'y2': 225, 'label': '剩下的全是鸡'},
                ],
            },
            {
                'step': 4,
                'title': '头脚双验算 · 牢记防反口诀',
                'say': '满分通关！鸡6只、兔4只！做鸡兔同笼最后一定要做头脚双验算：6加4等于10个头，12加16等于28只脚，永远不会把鸡和兔写反！',
                'board': '核对正确：鸡 6 只，兔 4 只 ✓\n验算头：6 + 4 = 10 (个) ✓\n验算脚：6×2 + 4×4 = 12 + 16 = 28 (只) ✓',
                'draw': [
                    {'op': 'note', 'x': 340, 'y': 392, 'label': '防错口诀：假设全是鸡，脚差 ÷ 2 先得兔，最后必做头脚双验算！'},
                ],
            },
        ],
    },
    {
        'id': 'word_tree_planting',
        'cat': 'word',
        'grade': '植树间隔模型',
        'title': '马路一旁两端都栽树求总长与棵数',
        'question': '在一条长 120 米的小路一旁栽树，每隔 20 米栽一棵（两端都要栽），一共需要栽多少棵树苗？',
        'golden_steps': [
            {
                'step': 1,
                'title': '画小路全长 · 先分清“段”与“棵”',
                'say': '植树问题最忌讳死记硬背加1减1！我们先画120米的小路，每隔20米分一段。先答第一关：120米除以20米，算出来的是“棵数”还是“间隔段数”？有几段？',
                'board': '已知：小路全长 = 120 米，间距 = 20 米\n要求：两端都要栽树，求总棵数\n第一步辨析：120 ÷ 20 求出的是什么量？等于几？',
                'quiz': {
                    'method': '段与棵辨析',
                    'q': '用全长 120 ÷ 间距 20，第一步直接算出来的是什么？',
                    'opts': ['求出 6 个「间隔段数」', '直接求出最终 6 棵树', '求出 7 个「间隔段数」'],
                    'ans': 0,
                    'whys': ['', '小心经典陷阱！120 ÷ 20 算出的是路被分成了 6“段”，两端都栽时棵数不等于段数哦！', '120 除以 20 等于 6 段，不是 7 段哦！'],
                    'tip': '想一想：120 米里面包含几个 20 米长的“路段”？',
                    'tip2': '总长 ÷ 间距 = 间隔段数，120 ÷ 20 = 6 个间隔段！接下来再找棵数与段数的关系。',
                },
                'draw': [
                    {'op': 'seg_bar', 'x': 90, 'y': 185, 'w': 480, 'parts': 6, 'role': 'main', 'label': '全长 120 米（每格 20 米）'},
                ],
            },
            {
                'step': 2,
                'title': '画小图找规律 · 自己推导出公式',
                'say': '分得真清楚，是6个间隔！如果考场上忘了加1还是减1怎么办？看老师画前两段小图：1段栽2棵，2段栽3棵……你发现两端都栽的规律了吗？',
                'board': '已求出：间隔数 = 120 ÷ 20 = 6 (段)\n画最小图实验（两端都栽）：\n1 段 → 2 棵树；2 段 → 3 棵树',
                'quiz': {
                    'method': '画小图归纳法',
                    'q': '观察小图规律：当「两端都要栽」时，棵数与间隔段数是什么关系？',
                    'opts': ['棵数 = 间隔段数 + 1', '棵数 = 间隔段数', '棵数 = 间隔段数 − 1'],
                    'ans': 0,
                    'whys': ['', '“棵数 = 段数”是只栽一端（或圆形封闭池塘栽树）的规律哦！', '“棵数 = 段数 − 1”是两端都不栽（比如锯木头次数）的规律哦！'],
                    'tip': '看黑板前两格：1 段有 2 个点，2 段有 3 个点，点数比段数多几？',
                    'tip2': '每段开头配 1 棵树，走到最后一段终点还要补栽收尾的 1 棵，所以棵数 = 间隔段数 + 1！',
                },
                'draw': [
                    {'op': 'point', 'x': 90, 'y': 185, 'label': '第1棵', 'pos': 'bottom'},
                    {'op': 'point', 'x': 170, 'y': 185, 'label': '第2棵', 'pos': 'bottom'},
                    {'op': 'point', 'x': 250, 'y': 185, 'label': '第3棵', 'pos': 'bottom'},
                ],
            },
            {
                'step': 3,
                'title': '列式留白 · 算出总棵数',
                'say': '用画小图法自己推出来了，一辈子都不会忘！6个间隔段，两端都要栽，一共需要栽多少棵树苗？快填进右侧核对吧！',
                'board': '规律：两端都栽 ⇒ 棵数 = 间隔段数 + 1\n已知间隔段数 = 6 段\n总棵数 = 6 + 1 = □ (棵)',
                'quiz': {
                    'method': '代入规律手算',
                    'q': '6 个间隔段，两端都栽树，一共需要栽多少棵树苗？',
                    'num_ans': '7',
                    'unit': '棵',
                    'opts': ['7 棵', '6 棵', '8 棵'],
                    'ans': 0,
                    'tip': '回想刚才画 2 段小图发现的规律：两端都要栽树时，最后终点还要补栽 1 棵收尾树哦！',
                    'tip2': '前 6 段每段起点 1 棵共 6 棵，加上终点收尾的 1 棵：6 + 1 = ？',
                },
                'draw': [
                    {'op': 'point', 'x': 330, 'y': 185, 'label': '4', 'pos': 'bottom'},
                    {'op': 'point', 'x': 410, 'y': 185, 'label': '5', 'pos': 'bottom'},
                    {'op': 'point', 'x': 490, 'y': 185, 'label': '6', 'pos': 'bottom'},
                    {'op': 'brace', 'x1': 90, 'y1': 240, 'x2': 555, 'y2': 240, 'label': '前 6 棵各配 1 个间隔'},
                ],
            },
            {
                'step': 4,
                'title': '补齐终点树 · 融会贯通三种植树法',
                'say': '完全正确，加上终点第7棵一共7棵！更厉害的是你学会了“画2段小图找规律”的方法，以后不管两端栽不栽还是锯木头，画两段数一数全搞定！',
                'board': '核对正确：6 + 1 = 7 (棵) ✓\n【一招通吃三模型：画 2 段小图数点】\n两端栽：段+1｜只栽一端/封闭圆：=段｜两端不栽：段−1',
                'draw': [
                    {'op': 'point', 'x': 570, 'y': 185, 'label': '终点第7棵', 'pos': 'bottom'},
                    {'op': 'aux_line', 'x1': 570, 'y1': 135, 'x2': 570, 'y2': 185, 'label': '收尾 +1 棵'},
                    {'op': 'note', 'x': 340, 'y': 382, 'label': '解题大智慧：忘记公式时先画 2 段小图数一数，永远不错！'},
                ],
            },
        ],
    },
    {
        'id': 'func_linear_area',
        'cat': 'func',
        'grade': '初中函数几何',
        'title': '一次函数与坐标轴围成三角形面积',
        'question': '已知一次函数 y = -2x + 6 的图像与 x 轴交于点 A，与 y 轴交于点 B，O 为坐标原点。求 △AOB 的面积及斜边 AB 上的高。',
        'golden_steps': [
            {
                'step': 1,
                'title': '建直角坐标系 · 引导求两轴交点',
                'say': '我们先建立直角坐标系。想求直线 y 等于负2x加6 与 x轴、y轴的交点A和B，应该分别令哪个坐标等于0？动笔算算A、B两点的坐标！',
                'board': '一次函数解析式：y = −2x + 6\n思考：x 轴上的点哪个坐标必为 0？\n      y 轴上的点哪个坐标必为 0？',
                'quiz': {
                    'method': '令零求轴交点',
                    'q': '直线 y = −2x + 6 与 x 轴交点 A、与 y 轴交点 B 的坐标分别是？',
                    'opts': ['A(3, 0)，B(0, 6)', 'A(6, 0)，B(0, 3)', 'A(−3, 0)，B(0, 6)'],
                    'ans': 0,
                    'whys': ['', '把 x 轴和 y 轴令谁为 0 搞反啦！x 轴上是纵坐标 y=0，不是 x=0 哦！', '解 −2x + 6 = 0 时，2x = 6，x 是正 3 不是负 3 哦！'],
                    'tip': '求 x 轴交点令 y = 0 解方程；求 y 轴交点令 x = 0！',
                    'tip2': '令 y = 0 解 −2x + 6 = 0（移项得 2x = 6）求点 A 横坐标；令 x = 0 直接读出点 B 纵坐标！',
                },
                'draw': [
                    {'op': 'axes', 'ox': 220, 'oy': 330, 'xlen': 340, 'ylen': 250, 'unit': 38},
                    {'op': 'point', 'x': 220, 'y': 330, 'label': 'O', 'pos': 'left'},
                ],
            },
            {
                'step': 2,
                'title': '连线求面积与斜边 · 避开勾股陷阱',
                'say': '坐标全对，OA长3，OB长6！现在请你亲自算出第一问直角三角形AOB的面积，再用勾股定理算斜边AB——千万警惕别误套3、4、5哦！',
                'board': '已求出：OA = 3，OB = 6（∠AOB = 90°）\n第一问思考：S△AOB = ½ × OA × OB = ？\n第二问准备：斜边 AB = √(3² + 6²) = ？',
                'quiz': {
                    'method': '防勾股套数坑',
                    'q': '第一问 △AOB 的面积是多少？斜边 AB 化简后是多少？',
                    'opts': ['面积 = 9，斜边 AB = 3√5', '面积 = 9，斜边 AB = 5', '面积 = 18，斜边 AB = 3√5'],
                    'ans': 0,
                    'whys': ['', '抓到高频易错点啦！勾股数 3-4-5 中 5 必须是斜边，这里 6 是直角边，3²+6²=45 哦！', '三角形面积要乘 ½，3 × 6 算出的是长方形面积 18，除以 2 才是 9 哦！'],
                    'tip': '直角三角形面积等于两直角边乘积的一半；求斜边用勾股定理算两直角边平方和再开方化简！',
                    'tip2': '面积用两直角边 3 × 6 ÷ 2 计算；斜边 √45 = √(9 × 5) = √9 × √5 化简成最简二次根式！',
                },
                'draw': [
                    {'op': 'point', 'x': 334, 'y': 330, 'label': 'A(3,0)', 'pos': 'bottom'},
                    {'op': 'point', 'x': 220, 'y': 102, 'label': 'B(0,6)', 'pos': 'left'},
                    {'op': 'line', 'x1': 195, 'y1': 52, 'x2': 365, 'y2': 392, 'role': 'main', 'label': 'y=-2x+6'},
                    {'op': 'poly_fill', 'pts': [[220, 330], [334, 330], [220, 102]], 'role': 'solve'},
                ],
            },
            {
                'step': 3,
                'title': '等面积列式留白 · 独立化简求高OH',
                'say': '漂亮避坑！面积是9，斜边AB等于3倍根号5。过原点O作斜边垂线OH，同一个三角形用底AB乘高OH除以2面积也是9，快化简求出OH吧！',
                'board': '已算出：S△AOB = 9，斜边 AB = 3√5\n利用「等面积法」列方程：\n½ × AB × OH = 9 ⇒ 3√5 × OH = 18 ⇒ OH = □',
                'quiz': {
                    'method': '等面积有理化',
                    'q': '将 OH = 18 ÷ (3√5) 约分并分母有理化，最终结果是？',
                    'force_choice': True,
                    'opts': ['6√5 / 5', '3√5 / 5', '6 / 5'],
                    'ans': 0,
                    'whys': ['', '18 除以 3 等于 6 而不是 3 哦，再检查一下分子！', '分母有理化需要分子、分母“同时”乘 √5，分子 6 也要乘上 √5 哦！'],
                    'tip': '第一步先分子分母同除以 3 得 6 / √5，第二步分子分母同乘 √5 去根号！',
                    'tip2': '先约分得 6 / √5，再分子与分母同时乘 √5（分母 √5 × √5 变成整数 5，分子记得保留 √5）！',
                },
                'draw': [
                    {'op': 'aux_line', 'x1': 220, 'y1': 330, 'x2': 311, 'y2': 284, 'label': '高OH'},
                    {'op': 'point', 'x': 311, 'y': 284, 'label': 'H', 'pos': 'right'},
                    {'op': 'right_angle', 'x': 311, 'y': 284, 'dx': -1, 'dy': 1, 'size': 13},
                ],
            },
            {
                'step': 4,
                'title': '解锁双问核对 · 提炼斜边高秒杀式',
                'say': '两问全对！面积是9，斜边高OH等于5分之6倍根号5！记住等面积法黄金公式：直角三角形斜边上的高，永远等于两直角边乘积除以斜边！',
                'board': '核对正确：\n① S△AOB = 9 ✓   ② 斜边高 OH = 6√5 / 5 ✓\n秒杀通式：Rt△斜边高 h = (a × b) ÷ c',
                'draw': [
                    {'op': 'note', 'x': 390, 'y': 402, 'label': '直角三角形求斜边高黄金通法：OH = (OA × OB) ÷ AB = 6√5 / 5'},
                ],
            },
        ],
    },
]

SAMPLE_ALT_METHODS = {
    'geo_trapezoid_30': {
        'title': '平移腰分割法（平行四边形 + 三角形）',
        'badge': '割补转化法',
        'steps': '过顶点 A 作右腰 DC 的平行线交 BC 于 F：右侧拼成底为 6cm 的平行四边形，左侧得到底为 14−6=8cm、高仍为 5cm 的 △ABF；两部分面积相加 6×5 + 8×5÷2 = 30 + 20 = 50 (cm²)！',
        'compare': '作高法最适合直接套梯形公式；平移腰法把两腰集中到一个三角形里，求未知角度或腰长范围时超级好用！',
        'say': '再教你第二种巧解法：过点A画右腰的平行线，把梯形拆成一个平行四边形和一个三角形，30加20同样算出50平方厘米！',
    },
    'word_meet_ratio': {
        'title': '同时间路程比分配法（路程比 = 速度比）',
        'badge': '比例分配法',
        'steps': '两车同时行 3 小时，所走路程比也等于 1.5 : 1（即 3 : 2，共 5 份）：乙车总路程 = 420÷5×2 = 168 千米 ⇒ 乙速 = 168÷3 = 56 km/h；甲速 = (420−168)÷3 = 84 km/h！',
        'compare': '先求 1 小时速度和数字更小好口算；按比例先分总路程则在追问“相遇点离中点多少千米”时一步秒杀！',
        'say': '换个视角一题多解：两车时间相同，路程比等于速度比3比2，先把420千米按5份分出各自路程再除以3小时，同样是56和84！',
    },
    'geo_cut_paste_leaf': {
        'title': '容斥重叠秒杀法（两扇形和 − 正方形）',
        'badge': '容斥原理法',
        'steps': '左下 ¼ 圆（78.5 cm²）+ 右上 ¼ 圆（78.5 cm²）= 157 cm²；两块扇形把正方形（100 cm²）铺满一层后，唯独中间树叶重叠盖了第 2 层，故 S叶 = 157 − 100 = 57 (cm²)！',
        'compare': '连对角线割补法直观好懂；容斥重叠法不用拆半叶再乘 2，计算步数最少、考场最稳！',
        'say': '第二种方法更过瘾：用容斥原理，两个四分之一圆面积合起来是157，减去正方形面积100，重叠的树叶一步算出57平方厘米！',
    },
    'geo_triangle_ext': {
        'title': '连底边封口法（大△内角和 − 小△内角和）',
        'badge': '整体补形法',
        'steps': '连接 BC 封成大 △ABC：∠ABC + ∠ACB = 180° − 35° = 145°；扣去已知的 25° 与 30°，得下方小 △DBC 两底角和为 145° − 55° = 90°，故凹角 ∠BDC = 180° − 90° = 90°！',
        'compare': '延长线双外角法两步直达最快；连 BC 封口法只用“三角形内角和 180°”就能稳稳推出结论！',
        'say': '如果不画延长线，也可以连接底边BC把凹口封成大三角形，用大小两个三角形内角和180度相减，同样推出90度！',
    },
    'word_diff_mult': {
        'title': '消去一倍对齐法 + 加差速算检验',
        'badge': '对应消元法',
        'steps': '故事书 − 科技书 = (3份 − 12) − 1份 = 2份 − 12 = 48 本 ⇒ 2份 = 60 本 ⇒ 科技书 1份 = 30 本；求故事书直接用加法 30 + 48 = 78 本（比 30×3−12 少算一步乘法）！',
        'compare': '线段图借书补整法最形象直观；求第二个量时直接用“科技书 + 原差 48”口算最快、绝不忘还 12 本！',
        'say': '分享一个提速妙招：算出1份科技书30本后，求故事书直接用30加两书相差的48等于78本，一步加法绝不会忘减12！',
    },
    'word_chicken_rabbit': {
        'title': '吹哨抬腿法（金鸡独立 · 兔子双足站）',
        'badge': '趣味抬腿法',
        'steps': '吹一声哨让每只动物同时抬起 2 只脚（共抬起 10×2 = 20 只脚），地上还剩 28 − 20 = 8 只脚；此时 2 脚鸡已全部坐下，地上站着的全是剩 2 只脚的兔 ⇒ 兔 = 8÷2 = 4 只，鸡 = 6 只！',
        'compare': '全鸡假设法书写最规范；吹哨抬腿法画面感满分，一眼看清地上剩下的脚全属于兔子！',
        'say': '再教你超有趣的吹哨抬腿法：口令一响每只动物抬起2只脚，小鸡全坐下啦，地上剩下的8只脚全是兔子的，8除以2得4只兔！',
    },
    'word_tree_planting': {
        'title': '树段一一打包法（1 树配 1 段 + 终点树）',
        'badge': '配对对应法',
        'steps': '把“每 1 棵树 + 它身后的 20 米路”打成 1 个包：120 米正好打包成 120÷20 = 6 组（含 6 棵树）；走到小路最右端尽头没有路了，补上最后 1 棵收尾终点树：6 + 1 = 7 棵！',
        'compare': '画 2 段小图法最适合考场防忘；一一配对打包法让你从底层原理彻底吃透为什么两端栽要加 1！',
        'say': '还可以用一一打包法理解：每棵树负责它后面的20米路，120米正好分6组6棵树，最后终点再补1棵收尾树就是7棵！',
    },
    'func_linear_area': {
        'title': '射影/相似三角形比例法（Rt△AHO ∽ Rt△ABO）',
        'badge': '几何相似法',
        'steps': '∵ ∠OAH = ∠BAO 且 ∠AHO = ∠AOB = 90°，∴ Rt△AHO ∽ Rt△ABO；由对应边成比例 OH / OB = OA / AB，得 OH = (3 × 6) ÷ (3√5) = 6√5 / 5！',
        'compare': '等面积法计算最简捷；相似三角形母子模型还能顺带求出 AH = 3√5/5、BH = 12√5/5 的分段长！',
        'say': '第二种思路是中考常考的直角三角形母子相似模型：三角形AHO相似于ABO，利用对应边成比例同样秒出5分之6倍根号5！',
    },
}
SAMPLE_ARCHETYPE_MAPS = {
    'geo_trapezoid_30': {
        'title': '母题变脸识破：特殊角作高模型',
        'badge': '🔗 同核不同皮',
        'core_bone': '已知斜边/斜坡长与 30°/45° 倾角求垂直高度或面积 ⇒ 过顶点作垂线构造直角三角形！',
        'headers': ['底层数学骨相', '本题故事皮相', '考场变身①(工程堤坝)', '考场变身②(测高仰角)'],
        'rows': [
            ['斜边与倾角', '斜腰 AB=10cm, ∠B=30°', '迎水斜坡长10米, 坡角30°', '风筝线长10米, 仰角30°'],
            ['关键桥梁量', '作高 AE = 10÷2 = 5cm', '求坝高 h = 10÷2 = 5米', '求垂直高度 = 10÷2 = 5米'],
            ['最终目标式', '(上底+下底)×高÷2', '梯形横截面土方体积', '加上手高求离地总高度'],
        ],
        'insight': '不管考卷画成几何梯形、水库拦河坝、山坡缆车还是放风筝，只要见到斜长配 30° 角，第一反应永远是作垂线拿斜边除以 2！',
        'say': '看穿出题人的变脸术啦！不管是梯形斜腰、水库大坝斜坡还是放风筝，脱掉故事外衣，底层全都是过顶点作垂线，利用30度角对边等于斜边一半！',
    },
    'word_meet_ratio': {
        'title': '母题变脸识破：合作效率和倍模型',
        'badge': '🔗 同核不同皮',
        'core_bone': '已知合作总量与共同时间先求「单位时间效率和」，再按 (1+倍数) 平分求 1 份！',
        'headers': ['底层数学骨相', '本题故事皮相', '考场变身①(师徒工程)', '考场变身②(双管注水)'],
        'rows': [
            ['合作总量÷时间', '420千米 ÷ 3小时 = 速度和', '420个零件 ÷ 3天 = 工效和', '420升水 ÷ 3分钟 = 流量和'],
            ['总份数合一', '乙1份 + 甲1.5份 = 2.5份', '徒弟1份 + 师傅1.5倍 = 2.5份', '细管1份 + 粗管1.5倍 = 2.5份'],
            ['先求标准1份', '140 ÷ 2.5 = 乙速56', '140 ÷ 2.5 = 徒弟日产56', '140 ÷ 2.5 = 细管每分56升'],
        ],
        'insight': '两车相向相遇、师徒合作加工零件、两根水管同时注水，本质一模一样：先用总量除以时间抓“1单位速度和/工效和”，再套和倍公式！',
        'say': '脱掉马甲看骨相！两车相遇、师徒合做零件、双管同时注水其实是同一道母题：第一步永远是用总量除以时间算出效率和，再除以1加倍数！',
    },
    'geo_cut_paste_leaf': {
        'title': '母题变脸识破：双集重叠容斥模型',
        'badge': '🔗 同核不同皮',
        'core_bone': '两个部分合起来盖满全集且中间重叠一层 ⇒ 重叠部分 = A + B − 全集总面积/总人数！',
        'headers': ['底层数学骨相', '本题故事皮相', '考场变身①(班级兴趣班)', '考场变身②(两木条绑接)'],
        'rows': [
            ['集合A + 集合B', '左下¼圆 + 右上¼圆 = 157', '报美术78人 + 报音乐79人', '甲木条78cm + 乙木条79cm'],
            ['外框全集总量', '正方形面积 = 100', '全班无漏报共 100 人', '绑接后总长 = 100cm'],
            ['中间重叠交集', '157 − 100 = 树叶57', '157 − 100 = 两项都报57人', '157 − 100 = 接头重叠57cm'],
        ],
        'insight': '不可思议吧！几何里的树叶阴影面积、统计里的两科都报人数、生活里的两根木棍捆绑重叠长度，底层全是同一个容斥公式 A + B − 总！',
        'say': '跨界大揭秘！几何树叶阴影、班级两科重叠报名、还有两根木条捆绑求接头，底层全都是容斥原理：用两部分之和减去外框总量！',
    },
    'geo_triangle_ext': {
        'title': '母题变脸识破：双外角汇聚模型',
        'badge': '🔗 同核不同皮',
        'core_bone': '凹折处无法直接套三角形公式 ⇒ 一条辅助线劈成左右两个三角形外角相加！',
        'headers': ['底层数学骨相', '本题故事皮相', '考场变身①(五角星折角)', '考场变身②(零件V型槽)'],
        'rows': [
            ['三个已知散角', '∠A=35°, ∠B=25°, ∠C=30°', '星尖角 35°、25°、30°', '机械臂三处偏转角'],
            ['核心转化桥梁', '连AD延长拆为双外角', '连顶点转化到外角三角形', '过凹点分割成双外角'],
            ['不变累加结论', '凹角 = 35°+25°+30° = 90°', '汇聚外角 = 三尖角之和', '检测凹槽角 = 三角之和'],
        ],
        'insight': '无论是纸飞机凹四边形、五角星求内角和，还是工业折板零件检测角，核心钥匙都是「三角形外角等于不相邻两内角之和」！',
        'say': '看透飞镖模型的变身术！不管考卷画成纸飞机、五角星一角还是折板零件，连顶点延长拆成两个外角，凹角永远等于三个尖角之和！',
    },
    'word_diff_mult': {
        'title': '母题变脸识破：几倍少几补齐差倍模型',
        'badge': '🔗 同核不同皮',
        'core_bone': '大数比小数的 k 倍少 m，且已知实际差 d ⇒ 先给大数补上 m 使新差变为 (d+m)，再除以 (k−1)！',
        'headers': ['底层数学骨相', '本题故事皮相', '考场变身①(父子年龄差)', '考场变身②(果园果树)'],
        'rows': [
            ['非整倍条件', '故事书比科技书3倍少12本', '爸爸年龄比儿子3倍少12岁', '苹果树比梨树3倍少12棵'],
            ['补齐后新差', '原差48 + 补12 = 60本', '年龄差48(永恒不变)+12', '原多48棵 + 补种12棵 = 60棵'],
            ['除以(倍数−1)', '60 ÷ (3−1) = 1份30', '60 ÷ (3−1) = 儿子30岁', '60 ÷ (3−1) = 梨树30棵'],
        ],
        'insight': '图书本数、年龄问题（暗含今年差=明年差）、商场价格差，只要出现“比几倍少几”，先虚拟补齐让差变大，立刻化归标准差倍！',
        'say': '掌握差倍变脸表，以后遇到年龄差、果树棵数差或者存款差，只要看到比几倍少几，先把少的部分补齐让差变大，再除以倍数减1就稳赢啦！',
    },
    'word_chicken_rabbit': {
        'title': '母题变脸识破：假设置换模型（鸡兔同笼大家族）',
        'badge': '🔗 同核不同皮',
        'core_bone': '已知「总件数」与「总权重」，两档单重有差 ⇒ 先全假设成低档求总差，再用「总差 ÷ 单档差」换出高档数！',
        'headers': ['底层数学骨相', '本题故事皮相', '考场变身①(租大小船)', '考场变身②(竞赛倒扣分)'],
        'rows': [
            ['已知总个数', '鸡兔共 10 个头', '大小船共租 10 条', '抢答一共 10 道题'],
            ['两档单件值', '鸡2脚 / 兔4脚 (差2)', '小船4人 / 大船6人 (差2)', '对+10分 / 错倒扣5分 (差15)'],
            ['置换核心列式', '总脚差 ÷ (4−2) 先得兔', '总人数差 ÷ (6−4) 先得大船', '总丢分 ÷ (10+5) 先得错题'],
        ],
        'insight': '考场上最爱给鸡兔同笼穿马甲：租大小船、买8角和2元邮票、大人小孩分馒头、甚至答错倒扣分（注意+10到−5单题差是相加15分！），骨相100%相同！',
        'say': '看穿考场最爱考的变脸王！不管是鸡兔同笼、大小船坐人、买不同面值邮票还是竞赛答错倒扣分，底层全都是假设置换法！',
    },
    'word_tree_planting': {
        'title': '母题变脸识破：点数与间隔段数模型',
        'badge': '🔗 同核不同皮',
        'core_bone': '先用「总量 ÷ 间距 = 间隔段数」，再画 2 段极简小图判断首尾两端是 +1、−1 还是相等！',
        'headers': ['底层数学骨相', '本题故事皮相', '考场变身①(锯木头/爬楼)', '考场变身②(大钟敲响)'],
        'rows': [
            ['间隔段数', '120米÷20米 = 6段路', '从1楼爬到7楼 = 6层楼梯', '敲7下之间有 = 6个时间间隔'],
            ['点与段关系', '两端栽树：棵数 = 段+1', '锯成7段木头：刀数 = 段−1', '敲钟耗时只算「间隔数」'],
            ['防坑金钥匙', '画2段数一数：3棵树', '锯成2段只需锯 1 刀', '敲4下有3个间隔先求单隔秒数'],
        ],
        'insight': '植树、安路灯、爬楼梯（1楼到n楼只有n−1层间隔！）、锯木头、敲钟计时，千万别死背公式，随手画 2 段小图数点通杀一切！',
        'say': '植树模型在考场上会变成锯木头、爬楼梯和时钟敲响问题！记住先分清点还是间隔段，画两段小图数一数，再狡猾的变形都骗不了你！',
    },
    'func_linear_area': {
        'title': '母题变脸识破：坐标交点与等面积求距模型',
        'badge': '🔗 同核不同皮',
        'core_bone': '令 x=0、y=0 定直角边 ⇒ 勾股求斜边 ⇒ 利用「同一图形两种面积表达式相等」反求点到直线距离！',
        'headers': ['底层数学骨相', '本题故事皮相', '考场变身①(动点距离)', '考场变身②(立体等体积求高)'],
        'rows': [
            ['两垂直基准量', '坐标轴截距 OA=3, OB=6', '矩形长宽 3 与 6 连对角线', '三棱锥三条两两垂直棱'],
            ['斜向度量计算', '勾股求斜边 AB = 3√5', '对角线 BD = 3√5', '先求底面斜三角形面积'],
            ['不变量反求高', '½·OA·OB = ½·AB·OH', 'S△ABD = ½·BD·h 求垂距', 'V₁ = V₂ 等体积法求点面距'],
        ],
        'insight': '从初中「点到直线的距离」一直到高中立体几何「点到平面的距离」，最省力的通天大道永远是：等面积法 / 等体积法！',
        'say': '这道题背后的等面积思想一路通往高中！不管是求原点到直线的垂线段，还是立体几何求点到平面的距离，抓住面积或体积不变列等式一招制敌！',
    },
}
for _sp_item in SAMPLE_PROBLEMS:
    if _sp_item['id'] in SAMPLE_ALT_METHODS:
        _sp_item['alt_method'] = SAMPLE_ALT_METHODS[_sp_item['id']]
    if _sp_item['id'] in SAMPLE_ARCHETYPE_MAPS:
        _sp_item['archetype_map'] = SAMPLE_ARCHETYPE_MAPS[_sp_item['id']]

SAMPLE_BY_ID = {p['id']: p for p in SAMPLE_PROBLEMS}

TUTOR_SYSTEM_PROMPT = """你是一位精通「波利亚（Pólya）四阶苏格拉底启发式教学法」的金牌数学名师，正在 700×440 的动态电子黑板上给孩子一对一引导讲题。
【教学铁律：严禁替孩子跨过思维坎！严禁一次报出答案！严禁在前3步用阿拉伯数字或中文数字（如“十二”“二百八十八”“六份”）泄露本步或最终得数！必须一步一设问、启发式层层引导！】
请严格按「每步独立一行 JSON（NDJSON）」输出 4 步启发式闯关微课，不要输出 Markdown 代码块(```)，每行一个完整 JSON：
- 第 1~2 步（思路突破与易错坎辨析，使用选择题 quiz）：
  * 【苏格拉底设问红线：第 1~2 步的 say 口播词与 board 只能引导观察已知条件与图形特征并抛出悬念问题，严禁在 say 中提前把本步 quiz 正确选项的答案说漏嘴（例如若 quiz 问“先比较什么→两人相差的钱数”，say 中严禁提前说“我们可以先比较两人原来相差多少”）！必须把判断权留给孩子！】
  * 【干扰项质量红线：严禁编造“铁丝重量/颜色”这类一眼假凑数废选项！严禁在错误选项括号里写“（误套/算错/漏除）”或开头写“误把/错当”自曝答案！3个 opts 必须全是纯净中立的学生常见数学误区表述，字数长度严格相近（相差不超过4字），具体错因一律只写在 whys 数组里！（若输入为拍题图片，第1步JSON请额外带上 "ocr":"仅提取印刷体原题题干，忽略图中学生手写草稿或手写答案"）】
{"step":1,"title":"4~8字标题","say":"口语启发讲解(45~75字，结尾提出本步关键思考问题；数学符号写中文读法如'角B等于30度')","board":"本步已知条件与思考方向(严禁提前写出本步提问的结果或后续算式得数！)","quiz":{"method":"4~6字思维招式名如逆向寻因/数形结合","q":"启发提问(28字内)","opts":["正确思路/关系","真实数学误区1","真实数学误区2"],"ans":0,"whys":["","为什么误区1掉坑了(28字内)","为什么误区2不对(28字内)"],"tip":"一级方向提示(绝不报答案)","tip2":"二级思考支架(只能教方法如移项/凑整/看垂足，严禁写出正确选项或得数！)"},"draw":[本步作图指令]}
- 第 3 步（链式分步留白 + 草稿本独立手算填空）：
  * 【严禁写多层括号套娃繁琐综合算式（如禁止写 72÷(2×2+2)×[72÷(2×2+2)×2]）！】必须按课本规范拆成清爽的 ①、② 分步列式并用「= □」留白（如「① 先求宽：72 ÷ 6 = □ (厘米)\n② 再求长：宽 × 2 = □ (厘米)\n③ 求面积：长 × 宽 = □ (平方厘米)」）！
  * 【严禁在 tip 或 tip2 中泄密任何小步得数（禁止写 '=28' 或 '等于二十八'）！且一级提示 tip（及 chain[].tip）只能点拨“谁和谁是什么数量关系”，严禁直接写出数字算式（把列式留给二级引导）！】
  * 若需要算 2~3 小步才能得出最终得数，或原题要求两个及以上未知量/连环多问（如“各是多少”、“求AB及高CD”），在 quiz 中必须用 "chain" 数组把【尚未求出】的每一问和关键中间量依次纳入提问，让孩子一格一格递进手算，绝不漏问（注意：若某小问如“相当于几倍/几个”已在第 2 步选择题中引导孩子答出，第 3 步直接基于该结论往后算未知量即可，不要在 chain 里重复提问第 2 步刚答过的同一个问题！）：
{"step":3,"title":"链式留白·分步手算","say":"肯定前两步推导，引导孩子按①②目标先自己想列式再动笔算出得数（严禁在口播词中念出具体数字算式如'用72除以6'，也严禁说出得数！）","board":"① 第一小步列式 = □\n② 最终列式 = □","quiz":{"method":"分步链式手算","q":"请在草稿本按步骤依次算出结果：","num_ans":"最终纯数字得数如288","unit":"最终单位如平方厘米","chain":[{"q":"第①步提问(不含算式，24字内)","ans":"12","unit":"厘米","tip":"本小步数量关系提示"},{"q":"第②步提问(不含算式，24字内)","ans":"24","unit":"厘米","tip":"本小步数量关系提示"},{"q":"第③步提问(不含算式，24字内)","ans":"288","unit":"平方厘米","tip":"本小步数量关系提示"}],"opts":["288平方厘米","576平方厘米","144平方厘米"],"ans":0,"tip":"一级防错提示","tip2":"二级分步提示"},"draw":[增量补充作图，标签不得含待算得数]}
  （注：若第3步只需1步简单计算即可得最终答案，chain 写 1 项或直接给 num_ans 即可；若最终结果含根号如 6√5/5 无法填纯数字，则加 "force_choice":true 走选择题）
- 第 4 步（解锁核对 + 逆向代入验算 + 同类通法提炼，不含 quiz）：
{"step":4,"title":"核对验算与方法复盘","say":"表扬孩子，核对各步得数，带孩子把答案代回原题条件逆向检验，总结一句破题口诀","board":"完整各步得数 ✓ + 代回原题检验算式 + 模型通法","draw":[最终高亮与底部 note 口诀条]}

【波利亚 4 步引导心法（重中之重）】：
1. 第1步直接画基准图 + 抓真突破口：【严禁在第1步只放纯 text 文字签不画图，也严禁问“画几条线段表示两种书/用什么图表示”这类字面凑数浅问！】应用题第1步必须直接用 seg_bar 画出各量的基准线段条并用 brace 标出总和或差（不要用细 line 代替 seg_bar），几何题第1步直接画出主图形与已知边角；第1步 quiz 必须直击核心突破口（如“把谁看作标准1份”、“去掉多出的部分后剩下是谁的几倍”、“求目标还缺哪个量、该过哪点作什么辅助线”），绝不一上来报算式。
2. 第2步跨易错坎 + 必画视觉支架：专门把这道题孩子最容易错的陷阱（如“周长是2条长+2条宽共6份而非3份”、“和倍要加1”、“差倍补齐后差变大”、“30°对边是斜边一半”）设计成提问让孩子自己辨析跨过去，绝不要老师在 board 上直接替孩子跨过去！【第2步严禁输出空 draw:[]！必须在原图上画出至少1~2个直观辅助笔迹（如 aux_line 辅助线/等分线、right_angle 直角标、brace 份数括号或 seg_bar 份数条），让孩子能看图数出来或推出来！】
3. 第3步链式拆解真手算：多步计算用 ①②③ 分步留白 + chain 递进手算，既清晰易懂，又让孩子亲手算出每一步！
4. 【画布增量叠加规范】：画布宽700×高440（安全区 x:65..635, y:55..365）。第2~4步默认保留前面步骤已画图形，只输出【新增】的辅助线(aux_line)、括号(brace，尽量放在图形外侧)、填充(poly_fill)或点；严禁把线段图(seg_bar)叠画在几何图形(rect/polygon)内部，也不要在同一条边重复叠加含义相同的 text 标签！若需切换全新图示，必须在该步 draw 第1项加 {"op":"clear"} 清黑板后再画！

支持的 draw 原语：
0. {"op":"clear"} (清空前序画布重画新图时放在首项)
1. {"op":"line","x1":100,"y1":300,"x2":500,"y2":300,"role":"main","label":"BC=14cm"}
2. {"op":"aux_line","x1":300,"y1":150,"x2":300,"y2":300,"label":"高AE"}
3. {"op":"arrow","x1":100,"y1":110,"x2":340,"y2":110,"role":"aux","label":"甲行3h"}
4. {"op":"polygon","pts":[[300,150],[100,300],[500,300]],"role":"main"}
5. {"op":"rect","x":200,"y":90,"w":220,"h":220,"role":"main","label":"边长10cm"}
6. {"op":"point","x":300,"y":150,"label":"A","pos":"top"} (pos: top/bottom/left/right)
7. {"op":"right_angle","x":300,"y":300,"dx":-1,"dy":-1,"size":14}
8. {"op":"arc","x":100,"y":300,"r":34,"a1":-30,"a2":0,"label":"30°"}
9. {"op":"seg_bar","x":124,"y":220,"w":240,"parts":3,"role":"main","label":"甲:3份"}
10. {"op":"brace","x1":100,"y1":330,"x2":500,"y2":330,"label":"总和420km"}
11. {"op":"poly_fill","pts":[[300,150],[100,300],[300,300]],"role":"accent"} (role: accent/solve)
12. {"op":"circle","cx":340,"cy":210,"r":90,"label":"r=5cm"}
13. {"op":"axes","ox":240,"oy":320,"xlen":300,"ylen":230,"unit":36}
14. {"op":"text","x":340,"y":120,"label":"关键关系"}
15. {"op":"note","x":350,"y":400,"label":"一句具体破题口诀(10~22字，勿只写口诀二字)"}
16. {"op":"curve","pts":[[160,110],[220,230],[280,290],[340,230],[400,110]],"role":"main","label":"y=x²-4x+3"} (抛物线/双曲线/生理曲线平滑样条)
17. {"op":"chart","kind":"bar","x":110,"y":90,"w":460,"h":230,"cats":["甲","乙","丙"],"vals":[15,25,20],"label":"数量统计"} (kind: bar/line)
18. {"op":"cube3d","x":190,"y":135,"w":220,"h":150,"dx":64,"dy":-46,"cut":"","label":"长方体"} (自带虚线遮挡棱；切拼表面积题可设 cut:"x" 或 "y" 高亮2个新截面)
19. {"op":"cylinder","x":240,"y":105,"w":180,"h":195,"label":"圆柱"} (自带底面后半虚线弧)
20. {"op":"table","x":85,"y":90,"w":530,"headers":["项目","单价","数量/总价"],"rows":[["基础段","8元","3km"],["超出段","2元/km","待求？"]],"label":"条件对比表"}
21. {"op":"vcalc","x":205,"y":88,"a":"12.5","op_sym":"+","b":"3.75","res":"□","rule":"小数点上下红线对齐","label":"竖式位值对齐"}
（跨学科全九科补充：物理画受力/电路/光路图；化学画左右原子守恒或洗气瓶导管图；生物画细胞/器官腔室/循环路径图；地理画等高线/经纬网/季风图；英语画时间轴或从句框且第3步加 "force_choice":true；语文阅读古诗、历史材料、道法辨析题画「材料证据词→原理手法→情感/规范结论」导图且第3步一律加 "force_choice":true 给采分骨架选项，绝不用数字键盘！）"""

TUTOR_DRAFT_DIAGNOSE_PROMPT = """你是一位温暖敏锐、精通「苏格拉底错因诊断法」的金牌名师，正在 700×440 的动态电子黑板上帮孩子批改【原题 + 手写草稿/算式】并一步步引导他自己订正！
【核心铁律：绝不能抹杀孩子的努力，也绝不能直接报出订正后的正确得数！必须先肯定草稿里写对的部分，再启发孩子自己揪出错在哪一步、为什么错、最后亲手算出正确答案！】
请严格按「每步独立一行 JSON（NDJSON）」输出 4 步错因诊断微课（不要输出 Markdown 代码块 ```）：
- 第 1 步（先夸亮点 + 还原草稿定位偏航步，选择题 quiz）：
  * 先真诚表扬孩子草稿中做对的细节（如找对了某个已知数、画对了草图或第一小步方向正确），在 board 中按「原题条件」与「手写草稿还原：①... ②...」清晰列出（严禁提前写出订正得数！），在 draw 中画出标准几何图/线段图/受力图；
  * 第 1 步 JSON 必须额外带 "ocr":"仅提取纯净印刷体原题题干（不含学生手写草稿与答案）"；
  * quiz 提出 3 选 1 启发问题：让孩子自己观察判断草稿是从哪一步开始掉进陷阱/偏离题意的（3个 opts 中立等长，不带括号自曝标签）！
- 第 2 步（红笔标出症结 + 辨析正确数量/几何关系，选择题 quiz）：
  * 在画布上必须新增 1~2 个高亮辅助元素（如 aux_line、role:"aux" 的 brace/seg_bar/arrow），直观标出草稿漏掉或搞反的关系；
  * quiz 用 3 选 1 引导孩子想清：为什么原草稿那样列式不对？正确的等量/几何/语法关系应该是什么？（严禁在 say/board/tip 中直接泄密最终数字结果！）
- 第 3 步（订正留白 = □ + 亲手重算通关）：
  * board 写出清晰的订正分步算式并用「= □」留白（多步用 chain 递进填空，数字题给 num_ans，根式或英语选词题加 "force_choice":true），让孩子自己算出正确结果！
- 第 4 步（原草稿误区 vs 订正验算对比复盘，不含 quiz）：
  * board 清晰列出「❌ 原草稿误区 → ✅ 订正算式与逆向检验 ✓」，在 draw 中放底部 {"op":"note","x":350,"y":398,"label":"一句具体防错口诀(10~22字)"}（不要在 board 里写 'note：...'，也严禁只写“防错口诀”四字占位符）！
（补充：若拍到的是孩子的【语文/英语手写作文或主观简答段落】，严禁替孩子重写整篇范文！请改为：①先表扬孩子习作中的闪光词句并3选1诊断结构立意短板 → ②揪出1处错别字/病句/英语时态三单错误让孩子3选1订正 → ③挑出1句平淡流水账加 "force_choice":true 让孩子3选1升级为五感慢镜头/高分从句 → ④原句与升格句对照复盘！）
【JSON 字段与画布铁律（严禁把 board 写成数组！）】：
- 每行一个独立 JSON，board 必须是单字符串（多行用 \\n 换行，严禁输出 JSON 数组 [...]！第3步等号左边严禁写 □，如写「间隔数 + 1 = □」而非「□ + 1 = □」，多步手算必须填 chain 数组）：
  第1~2步：{"step":1,"title":"4~8字标题","say":"口语点拨(结尾设问)","board":"多行字符串用\\n分隔","quiz":{"method":"4~6字招式","q":"提问","opts":["选项1","选项2","选项3"],"ans":0,"whys":["","错因1","错因2"],"tip":"一级提示","tip2":"二级支架"},"draw":[...]}
  第3步：{"step":3,"title":"订正留白·亲手重算","say":"...","board":"① 第一步：... = □\\n② 第二步：... = □","quiz":{"method":"分步订正手算","q":"请按步骤依次算出正确结果：","num_ans":"最终纯数字","unit":"单位","chain":[{"q":"第①步提问","ans":"20","unit":"个","tip":"提示"},{"q":"第②步提问","ans":"21","unit":"棵","tip":"提示"}],"opts":["21棵","20棵","19棵"],"ans":0,"tip":"一级提示","tip2":"二级支架"},"draw":[...]}
  第4步：{"step":4,"title":"误区对照·逆向验算","say":"...","board":"❌ 原草稿误区：...\\n✅ 订正算式：... ✓\\n✅ 逆向检验：... ✓","draw":[{"op":"note","x":350,"y":398,"label":"两端栽树段加一，代回原题验总长"}]}
- 画布 700×440 默认跨步增量保留！第2~4步只画新增辅助元素（或首项放 {"op":"clear"} 清屏重画）；常用原语：seg_bar(x,y,w,parts,role,label)、brace(x1,y1,x2,y2,label)、line/aux_line/arrow(x1,y1,x2,y2,label)、rect(x,y,w,h,label)、polygon/poly_fill(pts:[[x,y],...])、point(x,y,label,pos)、right_angle(x,y,dx,dy)、circle(cx,cy,r,label)、text(x,y,label)、note(x,y,label)、table(x,y,w,headers,rows)、vcalc(x,y,a,op_sym,b,res,rule)。"""

TUTOR_ESSAY_CRITIQUE_PROMPT = """你是一位温暖细腻、擅长「苏格拉底赏识升格精批法」的中小学语文/英语金牌写作导师，正在 700×440 的动态电子黑板上帮孩子精批【孩子自己写的作文草稿/片段】。
【核心铁律：严禁大包大揽替孩子重写整篇范文供照抄（任何一步不得连续代写超过2句正文）！必须立足孩子自己的原句，先放大闪光点，再一步一设问引导孩子亲手改病句、把平淡流水账升级为高分细节句！】
请严格按「每步独立一行 JSON（NDJSON）」输出 4 步习作升格精批微课（不要输出 Markdown 代码块 ```）：
- 第 1 步（闪光点放大镜 + 结构详略把脉，选择题 quiz）：
  * 真诚摘录表扬孩子草稿里写得好的 1~2 个词句或真实细节，在 draw 中用 rect+arrow 画出本文当前结构导图（标出哪里饱满、哪里略显平铺直叙）；
  * quiz 提出 3 选 1 启发问题：让孩子自己诊断这段习作最值得重点升级的地方（如高潮动作一笔带过缺慢镜头、结尾未扣题升华、或英语段落缺衔接词与时态统一，3个 opts 等长中立）！
- 第 2 步（字词病句 / 语法时态显微镜，选择题 quiz）：
  * 聚焦草稿中的 1 处典型语病、用词重复、错别字或英语时态/主谓一致/中式英语瑕疵（若原句无硬伤则挑1处标点或衔接瑕疵），在画布用 role:"aux" 标出症结；
  * quiz 用 3 选 1 引导孩子自己辨析选出最规范准确的修改方案！
- 第 3 步（平淡句慢镜头 / 高分句型升格实战，必加 "force_choice":true）：
  * 摘出孩子草稿中最平淡的一句“流水账”（如“我很累但坚持跑完了”或“I like reading very much”），引导加入动作分解/五感细节/修辞或英语高分从句；
  * quiz 必须带 "force_choice":true（绝不用数字键盘！），给出 3 个升格候选句（1个自然生动贴合原意的升格句 + 2个浮夸堆砌或偏离原意的干扰项）让孩子亲自选出点睛之笔！
- 第 4 步（原句 vs 升格示范对照卡 + 二次动笔清单，不含 quiz）：
  * board 清晰展示「✏️ 你的原句 → 🌟 升格示范句（限1~2句）」及 3 条考场二次修改自查清单，在 draw 中放底部 {"op":"note","x":350,"y":398,"label":"一句具体升格口诀(10~22字，严禁只写升格口诀四字)"}！
【JSON 字段与画布铁律（严禁把 board 写成数组！）】：
- 每行一个独立 JSON，board 必须是单字符串（多行用 \\n 换行，严禁输出 JSON 数组 [...]）：
  第1~3步：{"step":1,"title":"4~8字标题","say":"口语点拨(结尾设问)","board":"多行字符串用\\n分隔","quiz":{"force_choice":true,"method":"4~6字技法","q":"提问","opts":["选项1","选项2","选项3"],"ans":0,"whys":["","原因1","原因2"],"tip":"一级提示","tip2":"二级支架"},"draw":[...]}
  第4步：{"step":4,"title":"原句升格·对照复盘","say":"...","board":"✏️ 你的原句：...\\n🌟 升格示范：...\\n✅ 自查清单：...","draw":[{"op":"note","x":350,"y":398,"label":"慢放连续微动作，首尾回扣题眼词"}]}
- 画布 700×440 默认跨步保留，重画新导图时在 draw 首项加 {"op":"clear"}；常用原语：rect(x,y,w,h,role,label)、arrow/aux_line(x1,y1,x2,y2,label)、text(x,y,label)、note(x,y,label)、table(x,y,w,headers,rows)。"""


def _coerce_multiline_str(val, max_len: int = 380, strip_note_prefix: bool = False) -> str:
    """Safely coerce LLM string or list-of-strings (including stringified Python/JSON list repr) into clean \n-joined text."""
    items = None
    if isinstance(val, (list, tuple)):
        items = val
    elif isinstance(val, str):
        s_trim = val.strip()
        if len(s_trim) >= 4 and s_trim[0] == '[' and s_trim[-1] == ']':
            for parser in (json.loads, ast.literal_eval):
                try:
                    parsed = parser(s_trim)
                    if isinstance(parsed, (list, tuple)) and all(isinstance(x, (str, int, float)) for x in parsed):
                        items = parsed
                        break
                except Exception:
                    pass
    if items is not None:
        lines = [str(x).strip() for x in items if str(x).strip()]
        out = '\n'.join(lines)
    else:
        out = str(val or '')
    if strip_note_prefix:
        out = re.sub(r'[；;]\s*(?=[①②③④⑤⑥⑦⑧])', '\n', out)
    if strip_note_prefix and '\n' in out:
        kept = [re.sub(r'^note\s*[：:]\s*', '口诀：', ln, flags=re.I) for ln in out.split('\n')]
        out = '\n'.join(kept)
    return out.strip()[:max_len]


def _clamp(val, lo, hi, default=0):
    if isinstance(val, bool):
        return default
    try:
        v = float(val)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(v):
        return default
    return max(lo, min(hi, v))


def sanitize_draw_ops(raw_ops) -> list:
    """Sanitize and normalize LLM draw ops, accepting common schema variants safely."""
    if not isinstance(raw_ops, list):
        return []
    out = []
    for item in raw_ops[:18]:
        if not isinstance(item, dict):
            continue
        op = str(item.get('op') or item.get('type') or '').strip().lower()
        lbl = str(item.get('label') if item.get('label') is not None else (item.get('text') or '')).strip()[:46]
        role = str(item.get('role') or ('aux' if item.get('dashed') else 'main')).strip()
        if role not in ('main', 'aux', 'accent', 'solve'):
            role = 'main'

        # Normalize from/to & pt/coord/pos_xy coordinate aliases if LLM used them
        p_from = item.get('from') if isinstance(item.get('from'), (list, tuple)) and len(item.get('from')) >= 2 else None
        p_to = item.get('to') if isinstance(item.get('to'), (list, tuple)) and len(item.get('to')) >= 2 else None
        p_pt = None
        for pk in ('pt', 'coord', 'xy', 'position'):
            pv = item.get(pk)
            if isinstance(pv, (list, tuple)) and len(pv) >= 2:
                p_pt = pv
                break
        if p_pt is None and isinstance(item.get('pos'), (list, tuple)) and len(item['pos']) >= 2:
            p_pt = item['pos']
        x1_raw = item.get('x1', p_from[0] if p_from else None)
        y1_raw = item.get('y1', p_from[1] if p_from else None)
        x2_raw = item.get('x2', p_to[0] if p_to else None)
        y2_raw = item.get('y2', p_to[1] if p_to else None)
        px_raw = item.get('x', item.get('cx', p_pt[0] if p_pt else x1_raw))
        py_raw = item.get('y', item.get('cy', p_pt[1] if p_pt else y1_raw))
        span_w_raw = item.get('w', item.get('width', item.get('len', item.get('length'))))
        if span_w_raw is None and x1_raw is not None and x2_raw is not None:
            with contextlib.suppress(TypeError, ValueError):
                span_w_raw = abs(float(x2_raw) - float(x1_raw))
                if item.get('x') is None:
                    px_raw = min(float(x1_raw), float(x2_raw))
        span_h_raw = item.get('h', item.get('height'))
        if span_h_raw is None and y1_raw is not None and y2_raw is not None:
            with contextlib.suppress(TypeError, ValueError):
                span_h_raw = abs(float(y2_raw) - float(y1_raw))
                if item.get('y') is None and span_h_raw > 0:
                    py_raw = min(float(y1_raw), float(y2_raw))

        if op in ('clear', 'reset'):
            out.append({'op': 'clear'})
        elif op in ('line', 'aux_line', 'brace', 'arrow'):
            out.append({
                'op': op,
                'x1': round(_clamp(x1_raw, 24, 676, 100), 1),
                'y1': round(_clamp(y1_raw, 24, 420, 200), 1),
                'x2': round(_clamp(x2_raw, 24, 676, 400), 1),
                'y2': round(_clamp(y2_raw, 24, 420, 200), 1),
                'role': 'aux' if op == 'aux_line' else role,
                'label': lbl,
            })
        elif op == 'rect':
            rw_r = round(_clamp(span_w_raw, 24, 520, 200), 1)
            rh_r = round(_clamp(span_h_raw, 24, 340, 160), 1)
            out.append({
                'op': 'rect',
                'x': round(_clamp(px_raw, 24, max(24.0, 676.0 - rw_r), 180), 1),
                'y': round(_clamp(py_raw, 24, max(24.0, 416.0 - rh_r), 80), 1),
                'w': rw_r,
                'h': rh_r,
                'role': role,
                'label': lbl,
            })
        elif op in ('polygon', 'triangle', 'poly_fill'):
            pts_raw = item.get('pts') or item.get('points') or item.get('vertices')
            if isinstance(pts_raw, list) and len(pts_raw) >= 3:
                pts = []
                for p in pts_raw[:12]:
                    if isinstance(p, (list, tuple)) and len(p) >= 2:
                        pts.append([round(_clamp(p[0], 20, 680, 200), 1), round(_clamp(p[1], 20, 420, 200), 1)])
                if len(pts) >= 3:
                    norm_op = 'poly_fill' if op == 'poly_fill' else 'polygon'
                    out.append({
                        'op': norm_op,
                        'pts': pts,
                        'role': (role if role in ('accent', 'solve') else 'accent') if norm_op == 'poly_fill' else role,
                        'label': lbl,
                    })
        elif op == 'point':
            pos = str(item.get('pos') if isinstance(item.get('pos'), str) else 'top').strip()
            if pos not in ('top', 'bottom', 'left', 'right'):
                pos = 'top'
            out.append({
                'op': 'point',
                'x': round(_clamp(px_raw, 24, 676, 200), 1),
                'y': round(_clamp(py_raw, 24, 420, 200), 1),
                'label': lbl[:18],
                'pos': pos,
            })
        elif op == 'right_angle':
            out.append({
                'op': 'right_angle',
                'x': round(_clamp(px_raw, 24, 676, 200), 1),
                'y': round(_clamp(py_raw, 24, 420, 200), 1),
                'dx': -1 if _clamp(item.get('dx'), -1, 1, -1) < 0 else 1,
                'dy': -1 if _clamp(item.get('dy'), -1, 1, -1) < 0 else 1,
                'size': round(_clamp(item.get('size'), 8, 24, 14), 1),
            })
        elif op == 'arc':
            out.append({
                'op': 'arc',
                'x': round(_clamp(item.get('x', item.get('cx')), 24, 676, 200), 1),
                'y': round(_clamp(item.get('y', item.get('cy')), 24, 420, 200), 1),
                'r': round(_clamp(item.get('r', item.get('radius')), 18, 280, 34), 1),
                'a1': round(_clamp(item.get('a1', item.get('start_angle')), -360, 360, -40), 1),
                'a2': round(_clamp(item.get('a2', item.get('end_angle')), -360, 360, 0), 1),
                'label': lbl[:16],
            })
        elif op == 'seg_bar':
            sw_b = round(_clamp(span_w_raw, 40, 560, 220), 1)
            out.append({
                'op': 'seg_bar',
                'x': round(_clamp(px_raw, 30, max(30.0, 670.0 - sw_b), 100), 1),
                'y': round(_clamp(py_raw, 40, 380, 200), 1),
                'w': sw_b,
                'parts': int(_clamp(item.get('parts'), 1, 16, 1)),
                'role': role,
                'label': lbl,
            })
        elif op == 'circle':
            cr_c = round(_clamp(item.get('r', item.get('radius')), 16, 170, 80), 1)
            out.append({
                'op': 'circle',
                'cx': round(_clamp(item.get('cx', item.get('x')), 24.0 + cr_c, 676.0 - cr_c, 350), 1),
                'cy': round(_clamp(item.get('cy', item.get('y')), 28.0 + cr_c, 412.0 - cr_c, 210), 1),
                'r': cr_c,
                'label': lbl,
            })
        elif op == 'axes':
            out.append({
                'op': 'axes',
                'ox': round(_clamp(item.get('ox'), 60, 620, 240), 1),
                'oy': round(_clamp(item.get('oy'), 80, 390, 320), 1),
                'xlen': round(_clamp(item.get('xlen'), 100, 520, 320), 1),
                'ylen': round(_clamp(item.get('ylen'), 80, 320, 240), 1),
                'unit': round(_clamp(item.get('unit'), 20, 70, 36), 1),
            })
        elif op in ('text', 'label'):
            if lbl and not re.match(r'^(?:按.{0,5}依次计算|依次列式计算|草稿本计算|分步列式计算|动笔算一算|依次计算)$', lbl):
                tx = round(_clamp(px_raw, 40, 660, 350), 1)
                ty = round(_clamp(py_raw, 30, 390, 120), 1)
                split_m = re.match(r'^(\S{1,8})\s{3,}(\S{1,8})$', lbl)
                if split_m:
                    out.append({'op': 'text', 'x': round(_clamp(tx - 48, 40, 660, tx), 1), 'y': ty, 'role': role, 'label': split_m.group(1)})
                    out.append({'op': 'text', 'x': round(_clamp(tx + 48, 40, 660, tx), 1), 'y': ty, 'role': role, 'label': split_m.group(2)})
                else:
                    out.append({
                        'op': 'text',
                        'x': tx,
                        'y': ty,
                        'role': role,
                        'label': re.sub(r'\s{2,}', ' ', lbl),
                    })
        elif op in ('curve', 'spline', 'parabola'):
            pts_raw = item.get('pts') or item.get('points')
            if isinstance(pts_raw, list) and len(pts_raw) >= 3:
                c_pts = []
                for p in pts_raw[:40]:
                    if isinstance(p, (list, tuple)) and len(p) >= 2:
                        c_pts.append([round(_clamp(p[0], 24, 676, 200), 1), round(_clamp(p[1], 24, 420, 200), 1)])
                if len(c_pts) >= 3:
                    out.append({'op': 'curve', 'pts': c_pts, 'role': role, 'label': lbl[:32]})
        elif op in ('chart', 'bar_chart', 'line_chart'):
            cats_in = item.get('cats') or item.get('categories') or item.get('labels') or []
            vals_in = item.get('vals') or item.get('values') or item.get('data') or []
            if isinstance(cats_in, list) and isinstance(vals_in, list) and len(vals_in) >= 2:
                vals = [round(_clamp(v, -9999, 99999, 0), 2) for v in vals_in[:8]]
                cats = [str(cats_in[i] if i < len(cats_in) else f'项{i + 1}').strip()[:10] for i in range(len(vals))]
                kind = 'line' if (op == 'line_chart' or str(item.get('kind') or '').lower() == 'line') else 'bar'
                cw_ch = round(_clamp(item.get('w', item.get('width')), 180, 560, 460), 1)
                ch_ch = round(_clamp(item.get('h', item.get('height')), 110, 290, 230), 1)
                out.append({
                    'op': 'chart',
                    'kind': kind,
                    'x': round(_clamp(item.get('x'), 50, max(50.0, 676.0 - cw_ch), 110), 1),
                    'y': round(_clamp(item.get('y'), 45, max(45.0, 396.0 - ch_ch), 86), 1),
                    'w': cw_ch,
                    'h': ch_ch,
                    'cats': cats,
                    'vals': vals,
                    'label': lbl[:32],
                })
        elif op in ('cube3d', 'cuboid', 'cube'):
            cut_dir = str(item.get('cut') or '').strip().lower()
            if cut_dir not in ('', 'x', 'y'):
                cut_dir = ''
            cw_3d = round(_clamp(item.get('w', item.get('width')), 60, 360, 210), 1)
            ch_3d = round(_clamp(item.get('h', item.get('height')), 50, 230, 145), 1)
            cdx_3d = round(_clamp(item.get('dx'), 24, 120, 62), 1)
            cx_3d = round(_clamp(item.get('x'), 40, max(40.0, 676.0 - cw_3d - cdx_3d), 185), 1)
            cy_3d = round(_clamp(item.get('y'), 85, max(85.0, 416.0 - ch_3d), 145), 1)
            cdy_3d = round(max(32.0 - cy_3d, _clamp(item.get('dy'), -90, -20, -44)), 1)
            out.append({
                'op': 'cube3d',
                'x': cx_3d,
                'y': cy_3d,
                'w': cw_3d,
                'h': ch_3d,
                'dx': cdx_3d,
                'dy': cdy_3d,
                'cut': cut_dir,
                'role': role,
                'label': lbl[:32],
            })
        elif op == 'cylinder':
            cw_cyl = round(_clamp(item.get('w', item.get('width')), 60, 300, 180), 1)
            ch_cyl = round(_clamp(item.get('h', item.get('height')), 60, 250, 190), 1)
            out.append({
                'op': 'cylinder',
                'x': round(_clamp(item.get('x'), 60, max(60.0, 676.0 - cw_cyl), 240), 1),
                'y': round(_clamp(item.get('y'), 65, max(65.0, 416.0 - ch_cyl), 105), 1),
                'w': cw_cyl,
                'h': ch_cyl,
                'role': role,
                'label': lbl[:32],
            })
        elif op == 'table':
            hdrs_in = item.get('headers') or item.get('cols') or []
            rows_in = item.get('rows') or []
            if isinstance(rows_in, list) and len(rows_in) >= 1:
                clean_rows = []
                col_cnt = max(2, min(5, len(hdrs_in) if isinstance(hdrs_in, list) and hdrs_in else len(rows_in[0]) if isinstance(rows_in[0], list) else 2))
                hdrs = [str(hdrs_in[c] if isinstance(hdrs_in, list) and c < len(hdrs_in) else f'列{c + 1}').strip()[:14] for c in range(col_cnt)]
                for r_item in rows_in[:5]:
                    if isinstance(r_item, list):
                        row_cells = []
                        for c in range(col_cnt):
                            cs = str(r_item[c] if c < len(r_item) else '').strip()
                            c_lim = 28 if len(re.findall(r'[A-Za-z]', cs)) >= 4 else 22
                            row_cells.append(cs[:c_lim])
                        clean_rows.append(row_cells)
                if clean_rows:
                    tw_tbl = round(_clamp(item.get('w', item.get('width')), 240, 600, 530), 1)
                    th_tbl = (1 + len(clean_rows)) * 36.0
                    out.append({
                        'op': 'table',
                        'x': round(_clamp(item.get('x'), 40, max(40.0, 676.0 - tw_tbl), 85), 1),
                        'y': round(_clamp(item.get('y'), 45, max(45.0, 420.0 - th_tbl), 82), 1),
                        'w': tw_tbl,
                        'headers': hdrs,
                        'rows': clean_rows,
                        'label': lbl[:28],
                    })
        elif op in ('vcalc', 'vertical_calc'):
            a_str = str(item.get('a') if item.get('a') is not None else (item.get('top') or '')).strip()[:12]
            b_str = str(item.get('b') if item.get('b') is not None else (item.get('bottom') or '')).strip()[:12]
            if a_str and b_str:
                raw_sym = str(item.get('op_sym') or item.get('operator') or '+').strip()[:2]
                sym_map = {'*': '×', 'x': '×', 'X': '×', '-': '−', '－': '−', '＋': '+', '/': '÷'}
                op_sym = sym_map.get(raw_sym, raw_sym if raw_sym in ('+', '−', '×', '÷') else '+')
                res_str = str(item.get('res') if item.get('res') is not None else (item.get('result') or '□')).strip()[:14] or '□'
                rule_str = str(item.get('rule') or '').strip()[:32]
                out.append({
                    'op': 'vcalc',
                    'x': round(_clamp(item.get('x'), 80, 440, 210), 1),
                    'y': round(_clamp(item.get('y'), 55, 240, 88), 1),
                    'a': a_str,
                    'op_sym': op_sym,
                    'b': b_str,
                    'res': res_str,
                    'rule': rule_str,
                    'label': lbl[:28],
                })
        elif op in ('note', 'highlight_tag'):
            if lbl and not re.fullmatch(r'(?:升格|防错|防再错|核心|破题|总结|本题|方法|写作)?口诀(?:总结|提炼)?', lbl):
                out.append({
                    'op': 'note',
                    'x': round(_clamp(item.get('x'), 120, 580, 350), 1),
                    'y': round(_clamp(item.get('y'), 40, 420, 396), 1),
                    'label': lbl[:52],
                })
    return out


# ==================== On-Screen Voice Recorder (thinksmart_mic -> MacASR) ====================
class TutorVoiceRecorder:
    """Demand-driven 16kHz mono PCM capture from thinksmart_mic (0% idle CPU, max 25s)."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.proc = None
        self.buf = bytearray()
        self.reader_task = None
        self.started_mono = 0.0
        self._recording = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    async def _kill_proc_locked(self):
        proc, rtask = self.proc, self.reader_task
        self.proc = None
        self.reader_task = None
        self._recording = False
        self.started_mono = 0.0
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.6)
            except BaseException as exc:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                if isinstance(exc, asyncio.CancelledError):
                    raise
        if rtask is not None and not rtask.done():
            rtask.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await rtask

    async def start(self):
        async with self.lock:
            if _media_busy():
                raise RuntimeError('当前正在视频通话或投屏，暂时无法录音')
            await self._kill_proc_locked()
            self.buf = bytearray()
            env = dict(os.environ, XDG_RUNTIME_DIR='/run/user/1000', PULSE_SERVER='unix:/run/user/1000/pulse/native')
            self.proc = await asyncio.create_subprocess_exec(
                '/usr/bin/parec',
                '--device=thinksmart_mic',
                '--format=s16le',
                '--rate=16000',
                '--channels=1',
                '--latency-msec=40',
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self.started_mono = time.monotonic()
            self._recording = True
            proc = self.proc

            async def _drain():
                max_bytes = 25 * 32000  # 25s @ 16kHz s16le
                try:
                    while proc.stdout and len(self.buf) < max_bytes and (time.monotonic() - self.started_mono) < 26.0:
                        if _media_busy():
                            break
                        try:
                            chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=1.5)
                        except asyncio.TimeoutError:
                            continue
                        if not chunk:
                            break
                        take = min(len(chunk), max_bytes - len(self.buf))
                        self.buf.extend(chunk[:take])
                except BaseException:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    raise
                finally:
                    self._recording = False
                    if proc.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            proc.terminate()
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=0.4)
                        except (asyncio.TimeoutError, ProcessLookupError):
                            with contextlib.suppress(ProcessLookupError):
                                proc.kill()
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(proc.wait(), timeout=0.3)

            self.reader_task = asyncio.create_task(_drain())

    async def stop_and_get_wav(self) -> tuple:
        async with self.lock:
            dur = max(0.0, time.monotonic() - self.started_mono) if self.started_mono else 0.0
            await self._kill_proc_locked()
            raw_pcm = bytes(self.buf)
            self.buf.clear()
        if len(raw_pcm) < 16000 or dur < 0.6:  # < 0.5s
            raise ValueError('说话时间太短啦，请按住或点开始说完再点完成')
        return _pcm_to_wav(raw_pcm, sample_rate=16000, channels=1, bits=16), round(dur, 1)

    async def cancel(self):
        async with self.lock:
            await self._kill_proc_locked()
            self.buf.clear()


# ==================== External USB UVC Document Camera Manager ====================
class UVCCameraManager:
    STREAM_IDLE_SEC = 6.5
    AF_SETTLE_SEC = 1.95

    def __init__(self):
        self._lock = asyncio.Lock()
        self._stream_lock = asyncio.Lock()
        self._res_cache = {}
        self._last_frame = None
        self._last_frame_ts = 0.0
        self._last_frame_meta = None
        self._stream_proc = None
        self._stream_dev = ''
        self._stream_res = ''
        self._stream_low_fps = False
        self._prefer_low_fps = False
        self._last_preview_raw_ts = 0.0
        self._stream_start_ts = 0.0
        self._last_access_ts = 0.0
        self._raw_stream_jpg = None
        self._raw_stream_ts = 0.0
        self._raw_stream_seq = 0
        self._frame_ev = None
        self._reader_task = None
        self._watchdog_task = None
        self._frozen_raw_jpg = None
        self._frozen_seq = 0
        prefs = _read_safe_json(PREFS_PATH, {})
        if not isinstance(prefs, dict):
            prefs = {}
        rot = int(prefs.get('cam_rotate', 0) or 0)
        self.rotate = rot if rot in (0, 90, 180, 270) else 0
        zm = float(prefs.get('cam_zoom', 1.0) or 1.0)
        self.zoom = zm if zm in (1.0, 1.5, 2.0) else 1.0
        self.bright_boost = bool(prefs.get('cam_bright', False))

    def _prefs_lock(self_unused=None):
        @contextlib.contextmanager
        def _cm():
            PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
            lp = PREFS_PATH.with_name(f'.{PREFS_PATH.name}.lock')
            fd = os.open(str(lp), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
        return _cm()

    def _save_cam_prefs(self):
        with contextlib.suppress(Exception), self._prefs_lock():
            prefs = _read_safe_json(PREFS_PATH, {})
            if not isinstance(prefs, dict):
                prefs = {}
            if (
                prefs.get('cam_rotate') == self.rotate
                and prefs.get('cam_zoom') == self.zoom
                and prefs.get('cam_bright') is bool(self.bright_boost)
            ):
                return
            prefs['cam_rotate'] = self.rotate
            prefs['cam_zoom'] = self.zoom
            prefs['cam_bright'] = self.bright_boost
            _atomic_write_json(PREFS_PATH, prefs)

    def set_rotate(self, deg=None) -> int:
        if deg is None or isinstance(deg, bool):
            self.rotate = (self.rotate + 90) % 360
        else:
            try:
                val = int(deg) % 360
                self.rotate = val if val in (0, 90, 180, 270) else 0
            except (TypeError, ValueError, OverflowError):
                self.rotate = (self.rotate + 90) % 360
        self.clear_frame_cache()
        self._save_cam_prefs()
        return self.rotate

    def set_zoom(self, zoom_val=None) -> float:
        if zoom_val is None:
            self.zoom = 1.5 if self.zoom < 1.25 else (2.0 if self.zoom < 1.75 else 1.0)
        else:
            zv = round(float(zoom_val), 1)
            self.zoom = zv if zv in (1.0, 1.5, 2.0) else 1.0
        self._raw_stream_seq += 1
        self.clear_frame_cache()
        self._save_cam_prefs()
        return self.zoom

    async def set_bright_boost(self, on=None) -> bool:
        self.bright_boost = (not self.bright_boost) if on is None else bool(on)
        self._raw_stream_seq += 1
        self.clear_frame_cache()
        self._save_cam_prefs()
        cam = self.discover()
        if cam['connected'] and self._is_real_chardev(cam['dev']):
            await self._apply_exposure_ctrls(cam['dev'])
        return self.bright_boost

    async def set_freeze(self, on=None) -> bool:
        want = (self._frozen_raw_jpg is None) if on is None else bool(on)
        if not want:
            self._frozen_raw_jpg = None
            self._raw_stream_seq += 1
            self.clear_frame_cache()
            return False
        cam = self.discover()
        if not cam['connected']:
            raise LookupError('未检测到 USB 高拍仪')
        res_info = await self.probe_resolutions(cam['dev'], cam['name'])
        if self._raw_stream_jpg is None and res_info.get('input_fmt') == 'mjpeg' and self._is_real_chardev(cam['dev']):
            await self._ensure_stream(cam['dev'], self._desired_stream_res(res_info))
        if not self._raw_stream_jpg and res_info.get('input_fmt') != 'mjpeg':
            raw_fb = None
            if self._last_frame and (self.rotate == 0 and self.zoom <= 1.02 and not self.bright_boost):
                raw_fb = self._last_frame
            if not raw_fb:
                raw_fb = await self.capture_jpeg(high_res=True, crop_box=None, pencil_boost=False, raw_untransformed=True)
            if raw_fb:
                self._raw_stream_jpg = raw_fb
        if not self._raw_stream_jpg:
            raise RuntimeError('当前尚无可用高拍仪画面可定格')
        self._frozen_raw_jpg = self._raw_stream_jpg
        self._raw_stream_seq += 1
        self._frozen_seq = self._raw_stream_seq
        self.clear_frame_cache()
        return True

    async def _apply_exposure_ctrls(self, dev_path: str):
        if self.bright_boost:
            await self._run_v4l2_ctrl(dev_path, 'contrast=49')
            await self._run_v4l2_ctrl(dev_path, 'backlight_compensation=8')
            await self._run_v4l2_ctrl(dev_path, 'brightness=34')
        else:
            await self._run_v4l2_ctrl(dev_path, 'contrast=45')
            await self._run_v4l2_ctrl(dev_path, 'backlight_compensation=2')
            await self._run_v4l2_ctrl(dev_path, 'brightness=27')

    def _desired_stream_res(self, res_info: dict, force_hi: bool = False) -> str:
        if force_hi or self.zoom > 1.02:
            return res_info['hi_res']
        return res_info.get('preview_res') or res_info['hi_res']

    def clear_frame_cache(self):
        self._last_frame = None
        self._last_frame_ts = 0.0
        self._last_frame_meta = None
        self._raw_preview_cache_seq = -1
        self._raw_preview_cache_jpg = None

    async def stop_stream(self):
        proc = self._stream_proc
        self._stream_proc = None
        self._stream_dev = ''
        self._stream_res = ''
        self._stream_low_fps = False
        self._raw_stream_jpg = None
        self._raw_stream_ts = 0.0
        rt = self._reader_task
        wt = self._watchdog_task
        self._reader_task = None
        self._watchdog_task = None
        if proc is not None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(proc.wait(), timeout=1.5)
        cur = asyncio.current_task()
        for t in (rt, wt):
            if t is not None and t is not cur and not t.done():
                t.cancel()
                with contextlib.suppress(BaseException):
                    await t

    @staticmethod
    def _is_real_chardev(dev_path: str) -> bool:
        try:
            return stat.S_ISCHR(os.stat(dev_path).st_mode)
        except OSError:
            return False

    async def _run_v4l2_ctrl(self, dev_path: str, ctrl_expr: str):
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                '/usr/bin/v4l2-ctl', '-d', dev_path, f'--set-ctrl={ctrl_expr}',
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=1.5)
        except BaseException as exc:
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
            if isinstance(exc, asyncio.CancelledError):
                raise

    @staticmethod
    def _find_jpeg_eoi(buf: bytearray) -> int:
        n = len(buf)
        if n < 4 or buf[0] != 0xFF or buf[1] != 0xD8:
            return -1
        pos = 2
        while pos + 1 < n:
            if buf[pos] != 0xFF:
                return buf.find(b'\xff\xd9', 2)
            while pos + 1 < n and buf[pos + 1] == 0xFF:
                pos += 1
            if pos + 1 >= n:
                return -1
            marker = buf[pos + 1]
            if marker == 0xD9:
                return pos
            if marker == 0x00:
                pos += 2
                continue
            if marker == 0x01 or (0xD0 <= marker <= 0xD8):
                pos += 2
                continue
            if marker == 0xDA:
                if pos + 3 >= n:
                    return -1
                seg_len = (buf[pos + 2] << 8) | buf[pos + 3]
                if seg_len < 2:
                    return buf.find(b'\xff\xd9', 2)
                return buf.find(b'\xff\xd9', pos + 2 + seg_len)
            if pos + 3 >= n:
                return -1
            seg_len = (buf[pos + 2] << 8) | buf[pos + 3]
            if seg_len < 2:
                return buf.find(b'\xff\xd9', 2)
            pos += 2 + seg_len
        return -1

    def discover(self) -> dict:
        sys_v4l = Path('/sys/class/video4linux')
        if not sys_v4l.exists():
            return {'connected': False, 'dev': '', 'name': '', 'preview_res': '1280x720', 'hi_res': '1920x1080', 'rotate': self.rotate, 'zoom': self.zoom, 'bright_boost': self.bright_boost, 'frozen': False}
        candidates = []
        for entry in sorted(sys_v4l.iterdir(), key=lambda p: p.name):
            try:
                real = os.path.realpath(str(entry))
                if '/usb' not in real:
                    continue
                idx_p = entry / 'index'
                if idx_p.exists() and idx_p.read_text().strip() != '0':
                    continue
                dev_path = f'/dev/{entry.name}'
                if not os.path.exists(dev_path):
                    continue
                name = (entry / 'name').read_text(encoding='utf-8', errors='replace').strip() if (entry / 'name').exists() else 'USB 高拍仪'
                is_aux = 1 if re.search(r'\b(ir|meta|metadata|infrared|depth)\b', name, re.I) else 0
                candidates.append((is_aux, dev_path, name))
            except OSError:
                continue
        if not candidates:
            return {'connected': False, 'dev': '', 'name': '', 'preview_res': '1280x720', 'hi_res': '1920x1080', 'rotate': self.rotate, 'zoom': self.zoom, 'bright_boost': self.bright_boost, 'frozen': False}
        candidates.sort(key=lambda item: (item[0], item[1]))
        _, dev_path, name = candidates[0]
        cache_key = (dev_path, name)
        res_info = self._res_cache.get(cache_key)
        if not res_info:
            res_info = {'preview_res': '1280x720', 'hi_res': '1920x1080', 'input_fmt': 'mjpeg'}
            self._res_cache[cache_key] = res_info
        return {
            'connected': True,
            'dev': dev_path,
            'name': name,
            'preview_res': res_info['preview_res'],
            'hi_res': res_info['hi_res'],
            'rotate': self.rotate,
            'zoom': self.zoom,
            'bright_boost': self.bright_boost,
            'frozen': self._frozen_raw_jpg is not None,
        }

    async def probe_resolutions(self, dev_path: str, name: str) -> dict:
        cache_key = (dev_path, name)
        if cache_key in self._res_cache and self._res_cache[cache_key].get('probed'):
            return self._res_cache[cache_key]
        preview_res = '1280x720'
        hi_res = '1920x1080'
        input_fmt = 'mjpeg'
        try:
            proc = await asyncio.create_subprocess_exec(
                '/usr/bin/v4l2-ctl', '-d', dev_path, '--list-formats-ext',
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(proc.wait(), timeout=1.5)
                raise
            txt = out.decode('utf-8', errors='replace')
            blocks = re.split(r'(?=\[\d+\]:)', txt)
            mjpg_sizes = []
            raw_sizes = []
            for blk in blocks:
                if not blk.strip():
                    continue
                is_mjpg = ("'MJPG'" in blk or 'Motion-JPEG' in blk)
                is_raw = any(tok in blk for tok in ("'YUYV'", "'UYVY'", "'NV12'", 'YUYV'))
                if not is_mjpg and not is_raw and len(blocks) > 1:
                    continue
                target_list = mjpg_sizes if is_mjpg else raw_sizes
                for m in re.finditer(r'Size:\s+(?:Discrete|Stepwise)\s+(\d+)x(\d+)', blk):
                    w, h = int(m.group(1)), int(m.group(2))
                    if 640 <= w <= 4800 and 480 <= h <= 3600:
                        target_list.append((w * h, f'{w}x{h}'))
            if mjpg_sizes:
                mjpg_sizes = sorted(set(mjpg_sizes))
                input_fmt = 'mjpeg'
                # Prefer 4:3 full-sensor modes (A4/B5 textbook proportions) over cropped 16:9 modes when available
                four_three = [
                    (px, s) for px, s in mjpg_sizes
                    if 1.28 <= (int(s.split('x')[0]) / max(1, int(s.split('x')[1]))) <= 1.38
                ]
                max_all_px = mjpg_sizes[-1][0]
                max_43_px = four_three[-1][0] if four_three else 0
                if four_three and (max_43_px >= 900000 or max_43_px >= max_all_px * 0.45):
                    sweet_hi = [s for px, s in four_three if 2200000 <= px <= 6500000]
                    safe_43 = [s for px, s in four_three if px <= 9000000]
                    hi_res = sweet_hi[-1] if sweet_hi else (safe_43[-1] if safe_43 else four_three[0][1])
                    mid_43 = [s for px, s in four_three if 700000 <= px <= 2200000]
                    preview_res = mid_43[-1] if mid_43 else four_three[0][1]
                else:
                    safe_hi = [s for px, s in mjpg_sizes if px <= 9000000]
                    hi_res = safe_hi[-1] if safe_hi else mjpg_sizes[0][1]
                    mid = [s for px, s in mjpg_sizes if 700000 <= px <= 1600000]
                    preview_res = mid[-1] if mid else mjpg_sizes[0][1]
            elif raw_sizes:
                raw_sizes = sorted(set(raw_sizes))
                input_fmt = ''
                safe_hi = [s for px, s in raw_sizes if px <= 5200000]
                hi_res = safe_hi[-1] if safe_hi else raw_sizes[0][1]
                mid = [s for px, s in raw_sizes if 300000 <= px <= 1000000]
                preview_res = mid[-1] if mid else raw_sizes[0][1]
        except Exception:
            pass
        info = {'preview_res': preview_res, 'hi_res': hi_res, 'input_fmt': input_fmt, 'probed': True}
        self._res_cache[cache_key] = info
        return info

    async def _stream_reader_loop(self, proc, dev_path: str):
        buf = bytearray()
        try:
            while self._stream_proc is proc:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > 4 * 1024 * 1024:
                    del buf[:-1024 * 1024]
                while True:
                    soi = buf.find(b'\xff\xd8')
                    if soi < 0:
                        buf.clear()
                        break
                    if soi > 0:
                        del buf[:soi]
                    eoi = self._find_jpeg_eoi(buf)
                    if eoi < 0:
                        break
                    jpg = bytes(buf[:eoi + 2])
                    del buf[:eoi + 2]
                    if len(jpg) >= 1024:
                        self._raw_stream_jpg = jpg
                        self._raw_stream_ts = time.monotonic()
                        self._raw_stream_seq += 1
                        if self._frame_ev is not None:
                            self._frame_ev.set()
        except BaseException:
            pass
        finally:
            if self._stream_proc is proc:
                self._stream_proc = None
                if self._frame_ev is not None:
                    self._frame_ev.set()
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(proc.wait(), timeout=1.0)

    async def _stream_idle_watchdog(self, proc):
        try:
            while self._stream_proc is proc:
                await asyncio.sleep(1.5)
                if self._stream_proc is not proc:
                    break
                if (time.monotonic() - self._last_access_ts) > self.STREAM_IDLE_SEC or not os.path.exists(self._stream_dev):
                    async with self._stream_lock:
                        if self._stream_proc is proc and (
                            (time.monotonic() - self._last_access_ts) > self.STREAM_IDLE_SEC
                            or not os.path.exists(self._stream_dev)
                        ):
                            await self.stop_stream()
                            break
        except BaseException:
            pass

    async def _ensure_stream(self, dev_path: str, stream_res: str, low_fps: bool = False) -> bool:
        self._last_access_ts = time.monotonic()
        async with self._stream_lock:
            preview_active = (time.monotonic() - getattr(self, '_last_preview_raw_ts', 0.0)) < 3.5
            fps_ok = (bool(low_fps) == bool(self._stream_low_fps)) or (low_fps and not self._stream_low_fps and preview_active)
            if (
                self._stream_proc is not None
                and self._stream_proc.returncode is None
                and self._stream_dev == dev_path
                and self._stream_res == stream_res
                and fps_ok
                and self._raw_stream_jpg is not None
                and (time.monotonic() - self._raw_stream_ts) < 4.5
            ):
                return True
            await self.stop_stream()
            await self._run_v4l2_ctrl(dev_path, 'focus_automatic_continuous=1')
            await self._run_v4l2_ctrl(dev_path, 'sharpness=46')
            await self._apply_exposure_ctrls(dev_path)
            self._frame_ev = asyncio.Event()
            self._raw_preview_cache_seq = -1
            self._raw_preview_cache_jpg = None
            self._raw_preview_cache_dim = ''
            w_s, h_s = (stream_res.split('x') + ['1920', '1080'])[:2]
            fps_parm = '5' if low_fps else ('15' if (int(w_s) * int(h_s)) >= 2400000 else '30')
            cmd = [
                '/usr/bin/v4l2-ctl', '-d', dev_path,
                f'--set-fmt-video=width={w_s},height={h_s},pixelformat=MJPG',
                f'--set-parm={fps_parm}',
                '--stream-mmap=3', '--stream-to=-',
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
            except Exception:
                return False
            self._stream_proc = proc
            self._stream_dev = dev_path
            self._stream_res = stream_res
            self._stream_low_fps = bool(low_fps)
            self._stream_start_ts = time.monotonic()
            self._last_access_ts = self._stream_start_ts
            self._reader_task = asyncio.create_task(self._stream_reader_loop(proc, dev_path))
            self._watchdog_task = asyncio.create_task(self._stream_idle_watchdog(proc))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._frame_ev.wait(), timeout=2.5)
            ok_frame = self._raw_stream_jpg is not None and len(self._raw_stream_jpg) >= 1024
            if not ok_frame:
                await self.stop_stream()
            return ok_frame

    @staticmethod
    def _pil_fast_preview_sync(raw_jpg: bytes, zoom: float = 1.0, bright_boost: bool = False):
        im = _PILImage.open(io.BytesIO(raw_jpg))
        req_w = int(round(640 * max(1.0, zoom)))
        req_h = int(round(480 * max(1.0, zoom)))
        im.draft('RGB', (req_w, req_h))
        im.load()
        if im.mode != 'RGB':
            im = im.convert('RGB')
        if zoom > 1.02:
            zw = max(64, int(round(im.width / zoom)))
            zh = max(48, int(round(im.height / zoom)))
            zx = max(0, (im.width - zw) // 2)
            zy = max(0, (im.height - zh) // 2)
            im = im.crop((zx, zy, zx + zw, zy + zh))
        if im.width > 720:
            im = im.resize((720, max(2, round(im.height * 720 / im.width))), _PILImage.Resampling.BILINEAR)
        if bright_boost and _PILImageOps is not None:
            im = _PILImageOps.autocontrast(im, cutoff=(0, 2))
        out = io.BytesIO()
        im.save(out, format='JPEG', quality=75, subsampling=2)
        return out.getvalue(), f'{im.width}x{im.height}'

    @staticmethod
    def _pil_transform_sync(raw_jpg: bytes, rot: int, crop_box: dict, high_res: bool, zoom: float = 1.0, bright_boost: bool = False, pencil_boost: bool = False) -> bytes:
        im = _PILImage.open(io.BytesIO(raw_jpg))
        if not high_res and crop_box is None and zoom <= 1.02:
            im.draft('RGB', (800, 600))
        im.load()
        if im.mode != 'RGB':
            im = im.convert('RGB')
        if zoom > 1.02:
            zw = max(64, int(round(im.width / zoom)))
            zh = max(48, int(round(im.height / zoom)))
            zx = max(0, (im.width - zw) // 2)
            zy = max(0, (im.height - zh) // 2)
            im = im.crop((zx, zy, zx + zw, zy + zh))
        W0, H0 = im.size
        if isinstance(crop_box, dict):
            cx = _clamp(crop_box.get('x'), 0.0, 0.92, 0.0)
            cy = _clamp(crop_box.get('y'), 0.0, 0.96, 0.0)
            cw = _clamp(crop_box.get('w'), 0.08, 1.0 - cx, 1.0)
            ch = _clamp(crop_box.get('h'), 0.04, 1.0 - cy, 1.0)
            if cw < 0.995 or ch < 0.995 or cx > 0.005 or cy > 0.005:
                x2_n, y2_n = cx + cw, cy + ch
                if rot == 90:
                    ux1, uy1, ux2, uy2 = cy, 1.0 - x2_n, y2_n, 1.0 - cx
                elif rot == 180:
                    ux1, uy1, ux2, uy2 = 1.0 - x2_n, 1.0 - y2_n, 1.0 - cx, 1.0 - cy
                elif rot == 270:
                    ux1, uy1, ux2, uy2 = 1.0 - y2_n, cx, 1.0 - cy, x2_n
                else:
                    ux1, uy1, ux2, uy2 = cx, cy, x2_n, y2_n
                px1 = max(0, min(W0 - 2, int(round(ux1 * W0))))
                py1 = max(0, min(H0 - 2, int(round(uy1 * H0))))
                px2 = max(px1 + 2, min(W0, int(round(ux2 * W0))))
                py2 = max(py1 + 2, min(H0, int(round(uy2 * H0))))
                im = im.crop((px1, py1, px2, py2))
        if rot == 90:
            im = im.transpose(_PILImage.Transpose.ROTATE_270)
        elif rot == 180:
            im = im.transpose(_PILImage.Transpose.ROTATE_180)
        elif rot == 270:
            im = im.transpose(_PILImage.Transpose.ROTATE_90)
        max_w = 1600 if high_res else 960
        if im.width > max_w:
            im = im.resize((max_w, max(2, round(im.height * max_w / im.width))), _PILImage.Resampling.BILINEAR)
        if _PILImageOps is not None and _PILImageEnhance is not None:
            if pencil_boost:
                im = _PILImageOps.autocontrast(im, cutoff=(1, 3))
                im = _PILImageEnhance.Contrast(im).enhance(1.36)
            elif bright_boost:
                im = _PILImageOps.autocontrast(im, cutoff=(0, 3))
                im = _PILImageEnhance.Brightness(im).enhance(1.06)
        if high_res:
            im = im.filter(_PILFilter.UnsharpMask(radius=1.3, percent=115, threshold=2))
        out = io.BytesIO()
        im.save(out, format='JPEG', quality=90 if high_res else 78)
        return out.getvalue()

    async def capture_preview_raw(self, after_seq: int = -1, wait_new: bool = False):
        cam = self.discover()
        if not cam['connected']:
            raise LookupError('未检测到 USB 高拍仪')
        if self._frozen_raw_jpg is not None:
            f_seq = self._frozen_seq or self._raw_stream_seq or 1
            f_key = (f_seq, self.zoom, self.bright_boost)
            if after_seq >= 0 and f_seq <= after_seq:
                return None, f_seq, ''
            if getattr(self, '_raw_preview_cache_seq', -1) == f_key and getattr(self, '_raw_preview_cache_jpg', None):
                return self._raw_preview_cache_jpg, f_seq, self._raw_preview_cache_dim
            if _HAS_PIL:
                jpg_p, dim_p = await asyncio.to_thread(self._pil_fast_preview_sync, self._frozen_raw_jpg, self.zoom, self.bright_boost)
                self._raw_preview_cache_seq = f_key
                self._raw_preview_cache_jpg = jpg_p
                self._raw_preview_cache_dim = dim_p
                return jpg_p, f_seq, dim_p
            return self._frozen_raw_jpg, f_seq, ''
        self._last_access_ts = time.monotonic()
        self._last_preview_raw_ts = self._last_access_ts
        res_info = await self.probe_resolutions(cam['dev'], cam['name'])
        if res_info.get('input_fmt') == 'mjpeg' and self._is_real_chardev(cam['dev']):
            want_res = self._desired_stream_res(res_info)
            ok_s = await self._ensure_stream(cam['dev'], want_res)
            if ok_s and self._raw_stream_jpg:
                if wait_new and after_seq >= 0 and self._raw_stream_seq <= after_seq and self._frame_ev is not None:
                    self._frame_ev.clear()
                    if self._raw_stream_seq <= after_seq:
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(self._frame_ev.wait(), timeout=0.22)
                seq = self._raw_stream_seq
                s_key = (seq, self.zoom, self.bright_boost)
                if after_seq >= 0 and seq <= after_seq:
                    return None, seq, ''
                if getattr(self, '_raw_preview_cache_seq', -1) == s_key and getattr(self, '_raw_preview_cache_jpg', None):
                    return self._raw_preview_cache_jpg, seq, self._raw_preview_cache_dim
                if _HAS_PIL:
                    async with self._lock:
                        s_key = (seq, self.zoom, self.bright_boost)
                        if getattr(self, '_raw_preview_cache_seq', -1) == s_key and getattr(self, '_raw_preview_cache_jpg', None):
                            return self._raw_preview_cache_jpg, seq, self._raw_preview_cache_dim
                        jpg_p, dim_p = await asyncio.to_thread(self._pil_fast_preview_sync, self._raw_stream_jpg, self.zoom, self.bright_boost)
                        self._raw_preview_cache_seq = s_key
                        self._raw_preview_cache_jpg = jpg_p
                        self._raw_preview_cache_dim = dim_p
                        return jpg_p, seq, dim_p
                return self._raw_stream_jpg, seq, want_res
        jpg_fb = await self.capture_jpeg(high_res=False)
        return jpg_fb, int(time.monotonic() * 1000), ''

    @staticmethod
    def _pil_direct_gray_thumb_sync(raw_jpg: bytes, rot: int, tw: int, th: int, zoom: float = 1.0) -> bytes:
        if not _HAS_PIL or not raw_jpg:
            return b''
        with _PILImage.open(io.BytesIO(raw_jpg)) as im:
            if zoom <= 1.02:
                with contextlib.suppress(Exception):
                    im.draft('L', (max(tw, th) * 2, max(tw, th) * 2))
            g = im.convert('L')
            if zoom > 1.02:
                zw = max(64, int(round(g.width / zoom)))
                zh = max(48, int(round(g.height / zoom)))
                zx = max(0, (g.width - zw) // 2)
                zy = max(0, (g.height - zh) // 2)
                g = g.crop((zx, zy, zx + zw, zy + zh))
            if rot == 90:
                g = g.transpose(_PILImage.Transpose.ROTATE_270)
            elif rot == 180:
                g = g.transpose(_PILImage.Transpose.ROTATE_180)
            elif rot == 270:
                g = g.transpose(_PILImage.Transpose.ROTATE_90)
            return g.resize((tw, th), _PILImage.BILINEAR).tobytes()

    async def capture_gray_thumb(self, thumb_w: int = 160, thumb_h: int = 120) -> bytes:
        """Ultra-low-CPU 5fps direct grayscale thumbnail grab for CoWriteMonitor (zero RGB / JPEG re-encode)."""
        cam = self.discover()
        if not cam['connected']:
            return b''
        if self._frozen_raw_jpg is not None:
            await self.set_freeze(False)
        res_info = await self.probe_resolutions(cam['dev'], cam['name'])
        if res_info.get('input_fmt') == 'mjpeg' and self._is_real_chardev(cam['dev']) and _HAS_PIL:
            async with self._lock:
                want_res = res_info.get('preview_res') or res_info['hi_res']
                ok_s = await self._ensure_stream(cam['dev'], want_res, low_fps=True)
                if ok_s and self._raw_stream_jpg:
                    if (time.monotonic() - self._raw_stream_ts) > 1.3 and self._frame_ev is not None:
                        self._frame_ev.clear()
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(self._frame_ev.wait(), timeout=0.8)
                    raw_src = self._raw_stream_jpg
                    if raw_src:
                        return await asyncio.to_thread(self._pil_direct_gray_thumb_sync, raw_src, self.rotate, thumb_w, thumb_h, self.zoom)
        frame_jpg = await self.capture_jpeg(high_res=False, crop_box=None, pencil_boost=False)
        return await asyncio.to_thread(CoWriteMonitor._make_gray_thumb_sync, frame_jpg) 

    async def refocus(self) -> dict:
        cam = self.discover()
        if not cam['connected']:
            raise LookupError('未检测到 USB 高拍仪')
        res_info = await self.probe_resolutions(cam['dev'], cam['name'])
        if res_info.get('input_fmt') == 'mjpeg' and self._is_real_chardev(cam['dev']):
            await self._ensure_stream(cam['dev'], self._desired_stream_res(res_info))
        await self._run_v4l2_ctrl(cam['dev'], 'focus_automatic_continuous=0')
        await asyncio.sleep(0.12)
        await self._run_v4l2_ctrl(cam['dev'], 'focus_automatic_continuous=1')
        self._stream_start_ts = time.monotonic()
        self._last_access_ts = self._stream_start_ts
        self.clear_frame_cache()
        return cam

    async def _transform_jpeg_bytes(self, raw_jpg: bytes, vf_parts: list, high_res: bool) -> bytes:
        if not vf_parts:
            return raw_jpg
        qv = '2' if high_res else '4'
        cmd = [
            '/usr/bin/ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-threads', '1', '-filter_threads', '1', '-filter_complex_threads', '1',
            '-f', 'mjpeg', '-i', 'pipe:0', '-frames:v', '1',
            '-vf', ','.join(vf_parts), '-threads', '1', '-q:v', qv,
            '-f', 'image2', '-vcodec', 'mjpeg', 'pipe:1',
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(input=raw_jpg), timeout=5.0)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(proc.wait(), timeout=1.5)
            raise
        if proc.returncode != 0 or len(out) < 256:
            raise RuntimeError(f"高拍仪画面处理失败: {err.decode('utf-8', 'replace')[:120]}")
        return out

    async def capture_jpeg(self, high_res: bool = False, crop_box: dict = None, pencil_boost: bool = False, raw_untransformed: bool = False) -> bytes:
        cam = self.discover()
        if not cam['connected'] and self._frozen_raw_jpg is None:
            raise LookupError('未检测到 USB 高拍仪（机身底部 Type-C 口无 5V 供电输出，请通过带外接 5V 供电的拓展坞或 OTG 供电转接线连接高拍仪，或直接使用手机拍题）')
        use_rot = 0 if raw_untransformed else self.rotate
        use_zoom = 1.0 if raw_untransformed else self.zoom
        use_bright = False if raw_untransformed else self.bright_boost
        if self._frozen_raw_jpg is not None and _HAS_PIL:
            return await asyncio.to_thread(
                self._pil_transform_sync, self._frozen_raw_jpg, use_rot, crop_box, high_res, use_zoom, use_bright, pencil_boost,
            )
        now_m = time.monotonic()
        meta_now = (use_rot, use_zoom, use_bright, bool(pencil_boost))
        if (not raw_untransformed and not high_res and crop_box is None and self._last_frame
                and getattr(self, '_last_frame_meta', None) == meta_now
                and (now_m - self._last_frame_ts) < 0.25):
            self._last_access_ts = now_m
            return self._last_frame
        res_info = await self.probe_resolutions(cam['dev'], cam['name'])
        size_str = res_info['hi_res'] if high_res else res_info['preview_res']
        input_fmt = res_info.get('input_fmt', 'mjpeg')
        vf_parts = []
        if use_zoom > 1.02:
            zf = f'{use_zoom:.2f}'
            vf_parts.append(f'crop=iw/{zf}:ih/{zf}:(iw-iw/{zf})/2:(ih-ih/{zf})/2')
        if use_rot == 90:
            vf_parts.append('transpose=1')
        elif use_rot == 180:
            vf_parts.append('hflip,vflip')
        elif use_rot == 270:
            vf_parts.append('transpose=2')
        if isinstance(crop_box, dict):
            cx = _clamp(crop_box.get('x'), 0.0, 0.92, 0.0)
            cy = _clamp(crop_box.get('y'), 0.0, 0.96, 0.0)
            cw = _clamp(crop_box.get('w'), 0.08, 1.0 - cx, 1.0)
            ch = _clamp(crop_box.get('h'), 0.04, 1.0 - cy, 1.0)
            if cw < 0.995 or ch < 0.995 or cx > 0.005 or cy > 0.005:
                vf_parts.append(f'crop=iw*{cw:.4f}:ih*{ch:.4f}:iw*{cx:.4f}:ih*{cy:.4f}')
        max_w = 1600 if high_res else 1280
        vf_parts.append(f"scale='min({max_w},iw)':-2")
        if high_res:
            # Crisp document sharpening for small subscripts, exponents, and chemical formulas
            vf_parts.append('unsharp=3:3:0.75:3:3:0.0')

        # Fast continuous-stream path for real UVC MJPEG hardware (keeps continuous AF & AE converged!)
        if input_fmt == 'mjpeg' and self._is_real_chardev(cam['dev']):
            async with self._lock:
                want_res = self._desired_stream_res(res_info, force_hi=high_res)
                live_res = self._stream_res if (not high_res and self._stream_proc is not None and self._stream_proc.returncode is None and self._stream_dev == cam['dev'] and self._raw_stream_jpg) else want_res
                use_low_fps = bool(getattr(self, '_prefer_low_fps', False) or (self._stream_low_fps and self._stream_proc is not None and self._stream_proc.returncode is None))
                ok_stream = await self._ensure_stream(cam['dev'], live_res, low_fps=use_low_fps)
                if ok_stream and self._raw_stream_jpg:
                    if high_res:
                        # Ensure VCM autofocus & auto-exposure have streamed uninterrupted for AF_SETTLE_SEC
                        elapsed = time.monotonic() - self._stream_start_ts
                        if elapsed < self.AF_SETTLE_SEC:
                            await asyncio.sleep(self.AF_SETTLE_SEC - elapsed)
                    elif (time.monotonic() - self._raw_stream_ts) > 1.2 and self._frame_ev is not None:
                        self._frame_ev.clear()
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(self._frame_ev.wait(), timeout=0.8)
                    raw_src = self._raw_stream_jpg
                    if raw_src:
                        if _HAS_PIL:
                            out = await asyncio.to_thread(
                                self._pil_transform_sync, raw_src, use_rot, crop_box, high_res, use_zoom, use_bright, pencil_boost,
                            )
                        elif not high_res and crop_box is None and use_rot == 0 and len(raw_src) <= 650000:
                            out = raw_src
                        else:
                            out = await self._transform_jpeg_bytes(raw_src, vf_parts, high_res=high_res)
                        if not raw_untransformed and not high_res and crop_box is None:
                            self._last_frame = out
                            self._last_frame_ts = time.monotonic()
                            self._last_frame_meta = meta_now
                        return out

        def _make_ffmpeg_cmd(fmt_opt: str, res_opt: str) -> list:
            c = [
                '/usr/bin/ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
                '-threads', '1', '-filter_threads', '1', '-filter_complex_threads', '1',
                '-f', 'v4l2',
            ]
            if fmt_opt:
                c.extend(['-input_format', fmt_opt])
            if res_opt:
                c.extend(['-video_size', res_opt])
            c.extend(['-i', cam['dev']])
            if high_res:
                c.extend(['-ss', '1.60' if self._is_real_chardev(cam['dev']) else '0.18'])
            c.extend(['-frames:v', '1', '-vf', ','.join(vf_parts), '-threads', '1', '-q:v', '3', '-f', 'image2', '-vcodec', 'mjpeg', 'pipe:1'])
            return c

        async def _exec_capture(c: list):
            proc = await asyncio.create_subprocess_exec(
                *c, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                return proc, *(await asyncio.wait_for(proc.communicate(), timeout=6.5))
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(proc.wait(), timeout=1.5)
                raise

        async with self._lock:
            proc, out, err = await _exec_capture(_make_ffmpeg_cmd(input_fmt, size_str))
            if (proc.returncode != 0 or len(out) < 256) and (input_fmt or size_str):
                proc, out, err = await _exec_capture(_make_ffmpeg_cmd('', ''))
            if proc.returncode != 0 or len(out) < 256:
                raise RuntimeError(f"高拍仪抓拍失败: {err.decode('utf-8', 'replace')[:120]}")
            if not raw_untransformed and not high_res and crop_box is None:
                self._last_frame = out
                self._last_frame_ts = time.monotonic()
                self._last_frame_meta = meta_now
            return out


# ==================== Dedicated Phone Snap-to-Tutor Studio Manager (VPN Tunnel + 4-Page Homework Tray) ====================
class PhoneStudioManager:
    MAX_PAGES = 4

    def __init__(self):
        self._lock = asyncio.Lock()
        self._qr_cache = {}
        self._rot_cache = {}
        self._on_page_rotated = None
        self._on_page_removed = None
        self.last_mobile_mono = 0.0
        self._fb_rotate = 0
        self._fb_uploaded_at = 0
        self._fb_note = ''
        self._fb_last_crop = None
        prefs = _read_safe_json(PREFS_PATH, {})
        if not isinstance(prefs, dict):
            prefs = {}
        ps = prefs.get('phone_stage') if isinstance(prefs.get('phone_stage'), dict) else {}
        self.seq = max(0, int(ps.get('seq') or 0))
        self.pages = []
        raw_pages = ps.get('pages') if isinstance(ps.get('pages'), list) else []
        for item in raw_pages[:self.MAX_PAGES]:
            if not isinstance(item, dict):
                continue
            pid = str(item.get('id') or '').strip()
            if not re.fullmatch(r'[a-zA-Z0-9_]{2,24}', pid):
                continue
            pf = self._page_file(pid)
            try:
                if not pf.exists() or pf.is_symlink() or pf.stat().st_size < 128:
                    continue
            except OSError:
                continue
            rot = int(item.get('rotate') or 0)
            dc_clean = []
            for dc in (item.get('done_crops') or [])[:8]:
                if isinstance(dc, dict) and dc.get('sid') and isinstance(dc.get('crop'), dict):
                    sc = self._sanitize_crop_box(dc['crop'])
                    if sc:
                        dc_clean.append({'sid': str(dc['sid'])[:24], 'crop': sc})
            cm_clean = []
            for cm in (item.get('check_marks') or [])[:12]:
                if isinstance(cm, dict) and isinstance(cm.get('box'), dict):
                    sb = self._sanitize_crop_box(cm['box'])
                    if sb and cm.get('status') in ('ok', 'wrong', 'blank'):
                        cm_rec = {
                            'no': str(cm.get('no') or '')[:12],
                            'status': cm['status'],
                            'box': sb,
                            'note': str(cm.get('note') or '')[:36],
                        }
                        if cm.get('cowrite_id'):
                            cm_rec['cowrite_id'] = str(cm['cowrite_id'])[:24]
                        cm_clean.append(cm_rec)
            self.pages.append({
                'id': pid,
                'seq': int(item.get('seq') or self.seq or 1),
                'rotate': rot if rot in (0, 90, 180, 270) else 0,
                'uploaded_at': int(item.get('uploaded_at') or 0),
                'uploaded_ms': float(item.get('uploaded_ms') or item.get('uploaded_at') or 0),
                'note': str(item.get('note') or '')[:120],
                'last_crop': self._sanitize_crop_box(item['last_crop']) if isinstance(item.get('last_crop'), dict) else None,
                'done_crops': dc_clean,
                'check_marks': cm_clean,
                'check_summary': str(item.get('check_summary') or '')[:80],
                'check_seq': int(item.get('check_seq') or 0),
                'client_key': str(item.get('client_key') or '')[:32],
            })
        if not self.pages:
            leg = STATE_DIR / 'phone_stage_latest.jpg'
            try:
                if leg.exists() and not leg.is_symlink() and leg.stat().st_size >= 128:
                    mig_id = 'p_migrated'
                    mig_path = self._page_file(mig_id)
                    os.replace(str(leg), str(mig_path))
                    rot = int(ps.get('rotate') or 0)
                    self.pages.append({
                        'id': mig_id,
                        'seq': max(1, self.seq),
                        'rotate': rot if rot in (0, 90, 180, 270) else 0,
                        'uploaded_at': int(ps.get('uploaded_at') or int(time.time())),
                        'uploaded_ms': float(ps.get('uploaded_at') or int(time.time())),
                        'note': str(ps.get('note') or '')[:120],
                        'last_crop': self._sanitize_crop_box(ps['last_crop']) if isinstance(ps.get('last_crop'), dict) else None,
                        'client_key': '',
                    })
            except OSError:
                pass
        req_act = str(ps.get('active_id') or '').strip()
        if any(pg['id'] == req_act for pg in self.pages):
            self.active_id = req_act
        else:
            self.active_id = self.pages[-1]['id'] if self.pages else ''
        self._entry_url = str(os.environ.get('TUTOR_MOBILE_URL') or prefs.get('mobile_entry_url') or '').strip()
        self._prefs_sig = self._stat_prefs_sig()

    @staticmethod
    def _stat_prefs_sig():
        try:
            st = os.lstat(str(PREFS_PATH))
            return (st.st_ino, st.st_mtime_ns, st.st_size) if stat.S_ISREG(st.st_mode) else None
        except OSError:
            return None

    def _sync_prefs_url(self):
        if os.environ.get('TUTOR_MOBILE_URL'):
            return
        sig = self._stat_prefs_sig()
        if sig == getattr(self, '_prefs_sig', None):
            return
        self._prefs_sig = sig
        prefs = _read_safe_json(PREFS_PATH, {})
        if isinstance(prefs, dict):
            self._entry_url = str(prefs.get('mobile_entry_url') or '').strip()

    @staticmethod
    def _is_supported_image(raw_bytes: bytes) -> bool:
        if not raw_bytes or len(raw_bytes) < 128:
            return False
        return (
            raw_bytes[:3] == b'\xff\xd8\xff'
            or raw_bytes[:8] == b'\x89PNG\r\n\x1a\n'
            or (raw_bytes[:4] == b'RIFF' and raw_bytes[8:12] == b'WEBP')
        )

    @staticmethod
    def _sanitize_crop_box(crop_box: dict) -> dict:
        if not isinstance(crop_box, dict):
            return None
        cx = round(_clamp(crop_box.get('x'), 0.0, 0.96, 0.06), 4)
        cy = round(_clamp(crop_box.get('y'), 0.0, 0.96, 0.12), 4)
        cw = round(_clamp(crop_box.get('w'), 0.04, 1.0 - cx, 0.88), 4)
        ch = round(_clamp(crop_box.get('h'), 0.04, 1.0 - cy, 0.56), 4)
        return {'x': cx, 'y': cy, 'w': cw, 'h': ch}

    def _page_file(self, pid: str) -> Path:
        if not re.fullmatch(r'[a-zA-Z0-9_]{2,24}', str(pid or '')):
            raise ValueError('非法作业页编号')
        return STATE_DIR / f'phone_stage_{pid}.jpg'

    def _valid_pages(self) -> list:
        valid = []
        for pg in self.pages:
            try:
                pf = self._page_file(pg['id'])
                if pf.exists() and not pf.is_symlink() and pf.stat().st_size >= 128:
                    valid.append(pg)
            except (OSError, ValueError):
                continue
        if len(valid) != len(self.pages):
            self.pages = valid
        return valid

    def _active_page(self) -> dict:
        valid = self._valid_pages()
        if not valid:
            self.active_id = ''
            return None
        for pg in valid:
            if pg['id'] == self.active_id:
                return pg
        self.active_id = valid[-1]['id']
        return valid[-1]

    @property
    def stage_path(self) -> Path:
        act = self._active_page()
        if act:
            return self._page_file(act['id'])
        return STATE_DIR / 'phone_stage_latest.jpg'

    @property
    def rotate(self) -> int:
        act = self._active_page()
        return act['rotate'] if act else self._fb_rotate

    @rotate.setter
    def rotate(self, val: int):
        v = int(val or 0) % 360
        v = v if v in (0, 90, 180, 270) else 0
        act = self._active_page()
        if act:
            act['rotate'] = v
        else:
            self._fb_rotate = v

    @property
    def uploaded_at(self) -> int:
        act = self._active_page()
        return act['uploaded_at'] if act else self._fb_uploaded_at

    @uploaded_at.setter
    def uploaded_at(self, val: int):
        act = self._active_page()
        if act:
            act['uploaded_at'] = int(val or 0)
        else:
            self._fb_uploaded_at = int(val or 0)

    @property
    def note(self) -> str:
        act = self._active_page()
        return act['note'] if act else self._fb_note

    @note.setter
    def note(self, val: str):
        s = str(val or '')[:120]
        act = self._active_page()
        if act:
            act['note'] = s
        else:
            self._fb_note = s

    @property
    def last_crop(self):
        act = self._active_page()
        return act['last_crop'] if act else self._fb_last_crop

    @last_crop.setter
    def last_crop(self, val):
        act = self._active_page()
        if act:
            act['last_crop'] = val
        else:
            self._fb_last_crop = val

    def has_page(self) -> bool:
        return self._active_page() is not None

    def entry_url(self) -> str:
        self._sync_prefs_url()
        return self._entry_url or '/tutor/'

    def mark_mobile_seen(self, entry_base: str = ''):
        self.last_mobile_mono = time.monotonic()
        self._sync_prefs_url()
        eb = str(entry_base or '').strip().rstrip('/')
        if eb.startswith('https://') and len(eb) <= 120 and not any(c in eb for c in (' ', '\t', '\r', '\n', '"', "'", '<', '>')):
            cand = eb + '/tutor/'
            if cand != self._entry_url:
                self._entry_url = cand
                self._save_prefs()

    def is_mobile_online(self) -> bool:
        return self.last_mobile_mono > 0 and (time.monotonic() - self.last_mobile_mono) < 120.0

    def _save_prefs(self):
        with contextlib.suppress(Exception):
            lp = PREFS_PATH.with_name(f'.{PREFS_PATH.name}.lock')
            PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lp), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._sync_prefs_url()
                prefs = _read_safe_json(PREFS_PATH, {})
                if not isinstance(prefs, dict):
                    prefs = {}
                if not os.environ.get('TUTOR_MOBILE_URL'):
                    if self._entry_url:
                        prefs['mobile_entry_url'] = self._entry_url
                    else:
                        prefs.pop('mobile_entry_url', None)
                act = self._active_page()
                prefs['phone_stage'] = {
                    'seq': self.seq,
                    'active_id': self.active_id,
                    'pages': self.pages[:self.MAX_PAGES],
                    'rotate': act['rotate'] if act else 0,
                    'uploaded_at': act['uploaded_at'] if act else 0,
                    'note': act['note'] if act else '',
                    'last_crop': act['last_crop'] if act else None,
                }
                _atomic_write_json(PREFS_PATH, prefs)
                self._prefs_sig = self._stat_prefs_sig()
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    async def normalize_to_jpeg(self, raw_bytes: bytes) -> bytes:
        if not self._is_supported_image(raw_bytes):
            raise ValueError('仅支持 JPEG / PNG / WebP 题目图片')
        if raw_bytes[:3] == b'\xff\xd8\xff':
            return raw_bytes
        return await self.enhance_jpeg(raw_bytes, crop_box=None, rotate_deg=0, crisp=False)

    def find_page_id_by_client_key(self, client_key: str) -> str:
        ck = re.sub(r'[^a-zA-Z0-9_-]', '', str(client_key or ''))[:32]
        if not ck:
            return ''
        hit = next((pg for pg in self._valid_pages() if pg.get('client_key') == ck), None)
        return hit['id'] if hit else ''

    def save_page(self, img_bytes: bytes, note: str = '', crop_box: dict = None, client_key: str = '', page_id: str = '', rot_steps: int = 0, exclude_sid: str = '') -> str:
        if not img_bytes or len(img_bytes) < 128:
            raise ValueError('上传图片内容为空或已损坏')
        if not self._is_supported_image(img_bytes):
            raise ValueError('仅支持 JPEG / PNG / WebP 题目图片')
        STATE_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
        ck = re.sub(r'[^a-zA-Z0-9_-]', '', str(client_key or ''))[:32]
        req_pid = str(page_id or '').strip()
        valid = self._valid_pages()
        existing = (
            next((pg for pg in valid if req_pid and pg['id'] == req_pid), None)
            or next((pg for pg in valid if ck and pg.get('client_key') == ck), None)
        )
        pid = existing['id'] if existing else f"p{int(time.time()) % 100000}_{uuid.uuid4().hex[:5]}"
        p = self._page_file(pid)
        if p.exists() and p.is_symlink():
            raise OSError('Refusing symlink stage target')
        tmp = STATE_DIR / f'.phone_stage.{os.getpid()}.{uuid.uuid4().hex[:4]}.tmp'
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(img_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(p))
        finally:
            with contextlib.suppress(OSError):
                if tmp.exists():
                    tmp.unlink()
        self.seq += 1
        c_clean = self._sanitize_crop_box(crop_box) if isinstance(crop_box, dict) else None
        now_f = round(time.time(), 6)
        now_ts = int(now_f)
        clean_note = str(note or '').strip()[:120]
        r_cw = int(rot_steps or 0) % 4
        if existing:
            existing['seq'] = self.seq
            existing['rotate'] = 0
            existing['uploaded_at'] = now_ts
            existing['uploaded_ms'] = now_f
            if clean_note:
                existing['note'] = clean_note
            if c_clean:
                existing['last_crop'] = c_clean
            existing.setdefault('done_crops', [])
            if r_cw:
                for dc in existing.get('done_crops') or []:
                    if exclude_sid and dc.get('sid') == exclude_sid:
                        continue
                    if isinstance(dc.get('crop'), dict):
                        dc_c = dc['crop']
                        for _ in range(r_cw):
                            dc_c = self._rotate_crop_cw90(dc_c)
                        dc['crop'] = dc_c
                for cm in existing.get('check_marks') or []:
                    if isinstance(cm.get('box'), dict):
                        cm_b = cm['box']
                        for _ in range(r_cw):
                            cm_b = self._rotate_crop_cw90(cm_b)
                        cm['box'] = cm_b
                if callable(self._on_page_rotated):
                    with contextlib.suppress(Exception):
                        self._on_page_rotated(pid, r_cw)
            else:
                existing['check_marks'] = []
                existing['check_summary'] = ''
                existing['check_error'] = ''
        else:
            evicted_ids = []
            while len(self.pages) >= self.MAX_PAGES:
                ev = self.pages.pop(0)
                evicted_ids.append(ev['id'])
                with contextlib.suppress(OSError, ValueError):
                    ef = self._page_file(ev['id'])
                    if ef.exists() and not ef.is_symlink():
                        ef.unlink()
            for ev_id in evicted_ids:
                if callable(self._on_page_removed):
                    with contextlib.suppress(Exception):
                        self._on_page_removed(ev_id, False)
            self.pages.append({
                'id': pid,
                'seq': self.seq,
                'rotate': 0,
                'uploaded_at': now_ts,
                'uploaded_ms': now_f,
                'note': clean_note,
                'last_crop': c_clean,
                'done_crops': [],
                'check_marks': [],
                'check_summary': '',
                'check_error': '',
                'client_key': ck,
            })
        self.active_id = pid
        self._rot_cache.clear()
        self._save_prefs()
        return pid

    @staticmethod
    def _crop_iou(a: dict, b: dict) -> float:
        if not isinstance(a, dict) or not isinstance(b, dict):
            return 0.0
        ax1, ay1 = float(a.get('x', 0)), float(a.get('y', 0))
        ax2, ay2 = ax1 + float(a.get('w', 0)), ay1 + float(a.get('h', 0))
        bx1, by1 = float(b.get('x', 0)), float(b.get('y', 0))
        bx2, by2 = bx1 + float(b.get('w', 0)), by1 + float(b.get('h', 0))
        ix = max(0.0, min(ax2, bx2) - max(ax1, bx1))
        iy = max(0.0, min(ay2, by2) - max(ay1, by1))
        inter = ix * iy
        if inter <= 0:
            return 0.0
        union = max(1e-6, (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter)
        return inter / union

    def record_done_crop(self, page_id: str, sid: str, crop_box: dict):
        s_clean = str(sid or '').strip()[:24]
        if not s_clean or not isinstance(crop_box, dict):
            return
        c_clean = self._sanitize_crop_box(crop_box)
        if not c_clean or (c_clean['w'] > 0.94 and c_clean['h'] > 0.94):
            return
        valid = self._valid_pages()
        pid = str(page_id or '').strip()
        pg = next((x for x in valid if x['id'] == pid), None) if pid else self._active_page()
        if not pg:
            return
        dlist = list(pg.get('done_crops') or [])
        updated = False
        for item in dlist:
            if item.get('sid') == s_clean or self._crop_iou(item.get('crop'), c_clean) >= 0.52:
                item['sid'] = s_clean
                item['crop'] = c_clean
                updated = True
                break
        if not updated:
            dlist.append({'sid': s_clean, 'crop': c_clean})
        pg['done_crops'] = dlist[-8:]
        pg['last_crop'] = c_clean
        self._save_prefs()

    def prune_deleted_sessions(self, sid: str = '', clear_all: bool = False):
        changed = False
        for pg in self.pages:
            old_dc = pg.get('done_crops') or []
            if not old_dc:
                continue
            if clear_all:
                pg['done_crops'] = []
                changed = True
            elif sid:
                new_dc = [d for d in old_dc if d.get('sid') != sid]
                if len(new_dc) != len(old_dc):
                    pg['done_crops'] = new_dc
                    changed = True
        if changed:
            self._save_prefs()

    def set_check_marks(self, page_id: str, marks: list, summary: str = ''):
        valid = self._valid_pages()
        pid = str(page_id or '').strip()
        pg = next((x for x in valid if x['id'] == pid), None) if pid else self._active_page()
        if not pg:
            return
        pg['check_marks'] = list(marks or [])[:12]
        pg['check_summary'] = str(summary or '').strip()[:80]
        pg['check_error'] = ''
        pg['check_seq'] = int(pg.get('check_seq') or 0) + 1
        self.seq += 1
        pg['seq'] = self.seq
        self._save_prefs()

    def update_active_crop(self, crop_box: dict):
        act = self._active_page()
        if act and isinstance(crop_box, dict):
            new_crop = self._sanitize_crop_box(crop_box)
            if act.get('last_crop') != new_crop:
                act['last_crop'] = new_crop
                self._save_prefs()

    def select_page(self, page_id: str) -> bool:
        pid = str(page_id or '').strip()
        valid = self._valid_pages()
        if not any(pg['id'] == pid for pg in valid):
            return False
        if self.active_id != pid:
            self.active_id = pid
            self.seq += 1
            self._save_prefs()
        return True

    def select_page_by_index(self, page_num: int) -> bool:
        valid = self._valid_pages()
        if 1 <= int(page_num or 0) <= len(valid):
            return self.select_page(valid[int(page_num) - 1]['id'])
        return False

    @staticmethod
    def _rotate_crop_cw90(c: dict) -> dict:
        if not isinstance(c, dict):
            return None
        x = _clamp(c.get('x'), 0.0, 0.96, 0.06)
        y = _clamp(c.get('y'), 0.0, 0.96, 0.12)
        w = _clamp(c.get('w'), 0.04, 1.0 - x, 0.88)
        h = _clamp(c.get('h'), 0.04, 1.0 - y, 0.56)
        nx = round(_clamp(1.0 - (y + h), 0.0, 0.96, 0.06), 4)
        ny = round(_clamp(x, 0.0, 0.96, 0.12), 4)
        nw = round(_clamp(h, 0.04, 1.0 - nx, 0.56), 4)
        nh = round(_clamp(w, 0.04, 1.0 - ny, 0.88), 4)
        return {'x': nx, 'y': ny, 'w': nw, 'h': nh}

    def set_rotate(self, deg=None) -> int:
        act = self._active_page()
        old_rot = act['rotate'] if act else self._fb_rotate
        if deg is None or isinstance(deg, bool):
            new_rot = (old_rot + 90) % 360
        else:
            try:
                val = int(deg) % 360
                new_rot = val if val in (0, 90, 180, 270) else 0
            except (TypeError, ValueError, OverflowError):
                new_rot = (old_rot + 90) % 360
        self.rotate = new_rot
        steps_cw = ((new_rot - old_rot) % 360) // 90
        if steps_cw:
            if self.last_crop:
                c_cur = self.last_crop
                for _ in range(steps_cw):
                    c_cur = self._rotate_crop_cw90(c_cur)
                self.last_crop = c_cur
            if act and act.get('done_crops'):
                for dc in act['done_crops']:
                    if isinstance(dc.get('crop'), dict):
                        dc_c = dc['crop']
                        for _ in range(steps_cw):
                            dc_c = self._rotate_crop_cw90(dc_c)
                        dc['crop'] = dc_c
            if act and act.get('check_marks'):
                for cm in act['check_marks']:
                    if isinstance(cm.get('box'), dict):
                        cm_b = cm['box']
                        for _ in range(steps_cw):
                            cm_b = self._rotate_crop_cw90(cm_b)
                        cm['box'] = cm_b
            if act and callable(self._on_page_rotated):
                with contextlib.suppress(Exception):
                    self._on_page_rotated(act['id'], steps_cw)
        if act:
            self.seq += 1
            act['seq'] = self.seq
        self._save_prefs()
        return self.rotate

    def clear_page(self, page_id: str = '', clear_all: bool = False):
        pid = str(page_id or '').strip()
        if pid and not clear_all:
            kept = []
            for pg in self._valid_pages():
                if pg['id'] == pid:
                    with contextlib.suppress(OSError, ValueError):
                        pf = self._page_file(pid)
                        if pf.exists() and not pf.is_symlink():
                            pf.unlink()
                else:
                    kept.append(pg)
            self.pages = kept
            if self.active_id == pid:
                self.active_id = self.pages[-1]['id'] if self.pages else ''
            if callable(self._on_page_removed):
                with contextlib.suppress(Exception):
                    self._on_page_removed(pid, False)
        else:
            for pg in list(self.pages):
                with contextlib.suppress(OSError, ValueError):
                    pf = self._page_file(pg['id'])
                    if pf.exists() and not pf.is_symlink():
                        pf.unlink()
            with contextlib.suppress(OSError):
                leg = STATE_DIR / 'phone_stage_latest.jpg'
                if leg.exists() and not leg.is_symlink():
                    leg.unlink()
            self.pages = []
            self.active_id = ''
            if callable(self._on_page_removed):
                with contextlib.suppress(Exception):
                    self._on_page_removed('', True)
        self.seq += 1
        self._fb_rotate = 0
        self._fb_uploaded_at = 0
        self._fb_note = ''
        self._fb_last_crop = None
        self._rot_cache.clear()
        self._save_prefs()

    def public_state(self) -> dict:
        act = self._active_page()
        hp = act is not None
        valid = self._valid_pages()
        url = self.entry_url()
        pages_out = [
            {
                'id': pg['id'],
                'idx': i + 1,
                'rotate': pg['rotate'],
                'uploaded_at': pg['uploaded_at'],
                'note': pg.get('note') or '',
                'last_crop': pg.get('last_crop'),
                'done_crops': pg.get('done_crops') or [],
                'done_count': len(pg.get('done_crops') or []),
                'check_marks': pg.get('check_marks') or [],
                'check_summary': pg.get('check_summary') or '',
                'check_error': pg.get('check_error') or '',
                'check_seq': int(pg.get('check_seq') or 0),
                'active': pg['id'] == (act['id'] if act else ''),
                'page_url': f"/api/tutor/phone_stage/page.jpg?id={pg['id']}&seq={pg['seq']}&rot={pg['rotate']}",
            }
            for i, pg in enumerate(valid)
        ]
        return {
            'has_page': hp,
            'seq': self.seq if hp else 0,
            'active_id': act['id'] if hp else '',
            'page_count': len(pages_out),
            'pages': pages_out,
            'rotate': act['rotate'] if hp else self._fb_rotate,
            'uploaded_at': act['uploaded_at'] if hp else 0,
            'note': act['note'] if hp else '',
            'last_crop': act['last_crop'] if hp else None,
            'done_crops': (act.get('done_crops') or []) if hp else [],
            'done_count': len(act.get('done_crops') or []) if hp else 0,
            'check_marks': (act.get('check_marks') or []) if hp else [],
            'check_summary': (act.get('check_summary') or '') if hp else '',
            'check_error': (act.get('check_error') or '') if hp else '',
            'mobile_online': self.is_mobile_online(),
            'entry_ready': url.startswith('https://') or url.startswith('http://'),
            'entry_hint': '已接入专属 VPN 加密隧道（居家与外出通用）',
            'page_url': f"/api/tutor/phone_stage/page.jpg?id={act['id']}&seq={act['seq']}&rot={act['rotate']}" if hp else '',
        }

    async def get_qr_svg(self) -> bytes:
        target = self.entry_url()
        if target in self._qr_cache:
            return self._qr_cache[target]
        if os.path.exists('/usr/bin/qrencode'):
            with contextlib.suppress(Exception):
                proc = await asyncio.create_subprocess_exec(
                    '/usr/bin/qrencode', '-t', 'SVG', '-m', '1', '-o', '-', target,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
                except BaseException:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    with contextlib.suppress(BaseException):
                        await asyncio.wait_for(proc.wait(), timeout=1.5)
                    raise
                if proc.returncode == 0 and b'<svg' in out:
                    self._qr_cache = {target: out}
                    return out
        fallback = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 180 180">'
            '<rect width="180" height="180" fill="#ffffff"/>'
            '<rect x="16" y="16" width="44" height="44" fill="none" stroke="#000" stroke-width="8"/>'
            '<rect x="28" y="28" width="20" height="20" fill="#000"/>'
            '<rect x="120" y="16" width="44" height="44" fill="none" stroke="#000" stroke-width="8"/>'
            '<rect x="132" y="28" width="20" height="20" fill="#000"/>'
            '<rect x="16" y="120" width="44" height="44" fill="none" stroke="#000" stroke-width="8"/>'
            '<rect x="28" y="132" width="20" height="20" fill="#000"/>'
            '<text x="90" y="98" text-anchor="middle" font-size="14" font-weight="700" fill="#0b7470">/tutor/</text>'
            '</svg>'
        ).encode('utf-8')
        return fallback

    async def enhance_jpeg(
        self, raw_bytes: bytes, crop_box: dict = None, rotate_deg: int = 0, crisp: bool = True, pencil_boost: bool = False,
    ) -> bytes:
        if not raw_bytes or len(raw_bytes) < 128:
            raise ValueError('图片大小不合法')
        is_img_magic = (
            raw_bytes[:3] == b'\xff\xd8\xff'
            or raw_bytes[:8] == b'\x89PNG\r\n\x1a\n'
            or (raw_bytes[:4] == b'RIFF' and raw_bytes[8:12] == b'WEBP')
        )
        if not is_img_magic:
            raise ValueError('仅支持 JPEG / PNG / WebP 题目图片')
        if not os.path.exists('/usr/bin/ffmpeg'):
            return raw_bytes
        vf_parts = []
        rot = int(rotate_deg or 0) % 360
        if rot == 90:
            vf_parts.append('transpose=1')
        elif rot == 180:
            vf_parts.append('hflip,vflip')
        elif rot == 270:
            vf_parts.append('transpose=2')
        has_crop = False
        if isinstance(crop_box, dict):
            cx = _clamp(crop_box.get('x'), 0.0, 0.92, 0.0)
            cy = _clamp(crop_box.get('y'), 0.0, 0.96, 0.0)
            cw = _clamp(crop_box.get('w'), 0.08, 1.0 - cx, 1.0)
            ch = _clamp(crop_box.get('h'), 0.04, 1.0 - cy, 1.0)
            if cw < 0.995 or ch < 0.995 or cx > 0.005 or cy > 0.005:
                vf_parts.append(f'crop=iw*{cw:.4f}:ih*{ch:.4f}:iw*{cx:.4f}:ih*{cy:.4f}')
                has_crop = True
        vf_parts.append("scale='min(1600,iw)':-2")
        if has_crop:
            # Pad narrow 1-line strips vertically with white margin so vision encoders & snapModal get a clean ~4.5:1 card
            vf_parts.append("pad=iw:'max(ih,trunc(iw*0.22/2)*2)':0:'(oh-ih)/2':white")
        if pencil_boost:
            vf_parts.append('eq=contrast=1.24:gamma=0.76,unsharp=3:3:0.80:3:3:0.0')
        elif crisp:
            vf_parts.append('unsharp=3:3:0.55:3:3:0.0')
        cmd = [
            '/usr/bin/ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-threads', '1', '-filter_threads', '1', '-filter_complex_threads', '1',
            '-i', 'pipe:0', '-frames:v', '1',
            '-vf', ','.join(vf_parts), '-threads', '1', '-q:v', '3', '-f', 'image2', '-vcodec', 'mjpeg', 'pipe:1',
        ]
        async with self._lock:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(raw_bytes), timeout=5.5)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(proc.wait(), timeout=1.5)
                raise
            if proc.returncode == 0 and len(out) >= 128:
                return out
            if not has_crop and rot == 0 and raw_bytes[:3] == b'\xff\xd8\xff':
                return raw_bytes
            raise RuntimeError(f"题目图片裁剪处理失败: {err.decode('utf-8', 'replace')[:100]}")

    async def get_rotated_stage_jpeg(self, page_id: str = '') -> bytes:
        valid = self._valid_pages()
        pid = str(page_id or '').strip()
        pg = next((x for x in valid if x['id'] == pid), None) if pid else self._active_page()
        if not pg:
            raise LookupError('大屏暂未收到手机上传的整页作业图')
        _, raw = _read_regular_bytes(self._page_file(pg['id']), 12 * 1024 * 1024)
        rot = int(pg.get('rotate') or 0) % 360
        if rot == 0:
            return raw
        ckey = (pg['id'], pg['seq'], rot)
        if ckey in self._rot_cache:
            return self._rot_cache[ckey]
        rot_jpg = await self.enhance_jpeg(raw, crop_box=None, rotate_deg=rot, crisp=False)
        if len(self._rot_cache) >= 2:
            self._rot_cache.clear()
        self._rot_cache[ckey] = rot_jpg
        return rot_jpg


def build_step0_warmup_draw(question_txt: str) -> list:
    """Build an instant 0ms Polya Stage-1 '已知条件 -> 动脑搭桥 -> 所求目标' visual mind-map for Step 0."""
    q = str(question_txt or '').strip()
    if not q:
        return [{'op': 'note', 'x': 350, 'y': 220, 'label': 'AI 老师正在识图拆解已知条件与所求目标…'}]
    q_clean = re.sub(r'^\s*(?:\d+\s*[.、．)\uff09]\s*)?(?:【[^】]{1,14}】\s*)?', '', q).strip() or q
    subj_0 = detect_subject(q)
    is_en_cloze = (
        subj_0 == 'english'
        and bool(re.search(r'_{2,}|\(\s*[A-Za-z]+\s*\)', q_clean))
        and not bool(re.search(r'请(?:判断|选择|回答|分析)|应填|横线处|哪一项', q_clean))
    )
    clauses = [] if is_en_cloze else [
        c_cl for c_cl in (
            re.sub(r'^[”’」』）)\s]+|[“‘「『（(\s]+$', '', c.strip())
            for c in re.split(r'(?:[，。；！？,;!\?\n]|(?<!\d)\.(?!\d)|\s+(?=第\([1-4]\)问|第[①②③④]问))+', q_clean)
        ) if c_cl
    ]
    if is_en_cloze:
        conds = [q_clean]
        goal_c = '抓时间标志与主谓一致填准词形'
    elif len(clauses) >= 2:
        q_idx = next(
            (i for i in range(1, len(clauses)) if re.search(
                r'(?:^求|求[出得]?|多少|几[只本棵个元秒米份度厘米小时天人辆步次倍枚颗张支块页道名岁盒箱袋克升]|怎样|如何|是否|证明|等于几|是几|'
                r'请(?:判断|选择|赏析|分析|简述|概括|回答|指出|说明)|好在哪里|为什么|哪端|哪个|哪项|哪一项|应填|是什么)',
                clauses[i],
            )),
            len(clauses) - 1,
        )
        if (
            q_idx >= 2
            and re.match(r'^(?:如果|若|假设|要是)', clauses[q_idx - 1])
            and not re.search(r'(?:为|等于|[=＝])\s*-?\d', clauses[q_idx - 1])
        ):
            q_idx -= 1
        goal_c = '，'.join(clauses[q_idx:])
        conds = clauses[:q_idx]
    else:
        m_sp0 = re.match(r'^(.{4,24}?[》”」中里时])\s*(.{3,})$', q_clean)
        if m_sp0:
            conds = [m_sp0.group(1).strip()]
            goal_c = m_sp0.group(2).strip()
        else:
            goal_c = q_clean
            conds = []
    # Merge pure scene-header fragments (like '如图', '已知', '在 Rt△ABC 中', '在梯形 ABCD 中') into the next condition so key numeric conditions are never dropped!
    merged = []
    buf_c = ''
    for c in conds:
        is_scene_hdr = (len(c) <= 4 and not re.search(r'\d', c)) or bool(
            not re.search(r'\d|[=＝∥⊥°]', c) and re.match(r'^(?:如图|已知|照这样计算|照此计算|按这样计算|依此类推|据此计算|在\s*[A-Za-z0-9△▱⊙Rt\u4e00-\u9fa5]{1,12}\s*[中里内])$', c)
        )
        if is_scene_hdr:
            buf_c = (buf_c + ' ' + c).strip()
        else:
            merged.append((buf_c + ' ' + c).strip() if buf_c else c)
            buf_c = ''
    if buf_c:
        if merged:
            merged[-1] = f'{merged[-1]} {buf_c}'
        else:
            merged.append(buf_c)
    shown_conds = merged[:4] if merged else [q_clean[:18] if q_clean != goal_c else '通读题干抓关键标志词']
    ys_map = {1: [200], 2: [165, 235], 3: [135, 200, 265], 4: [115, 172, 228, 285]}
    ys = ys_map.get(len(shown_conds), [115, 172, 228, 285])
    if subj_0 == 'english':
        cond_pre, bridge_lbl = '语境', '找语法标志词'
    elif subj_0 in ('chinese', 'history', 'civics'):
        cond_pre, bridge_lbl = '线索', '寻证析角度'
    elif subj_0 in ('physics', 'chemistry', 'biology', 'geography'):
        cond_pre, bridge_lbl = '已知', '找科学规律'
    else:
        cond_pre, bridge_lbl = '已知', '找数量关系'
    ops = []
    for idx_c, txt_c in enumerate(shown_conds):
        short_c = txt_c[:18] + ('…' if len(txt_c) > 18 else '')
        ops.append({'op': 'text', 'x': 165, 'y': ys[idx_c], 'role': 'main', 'label': f'{cond_pre}{idx_c + 1}：{short_c}'})
    ops.append({'op': 'arrow', 'x1': 290, 'y1': 200, 'x2': 386, 'y2': 200, 'role': 'aux', 'label': bridge_lbl})
    short_g = goal_c[:17] + ('…' if len(goal_c) > 17 else '')
    ops.append({'op': 'text', 'x': 542, 'y': 200, 'role': 'aux', 'label': f'目标：{short_g}'})
    ops.append({'op': 'note', 'x': 350, 'y': 378, 'label': '审题热身：边听读题边思考——从已知通向目标还缺哪座“中间桥”？'})
    return sanitize_draw_ops(ops)


def build_step0_scaffold(question_txt: str, teach_mode: str = 'standard', essay_critique: bool = False, from_ocr: bool = False) -> dict:
    q = str(question_txt or '').strip()
    tmode = _norm_teach_mode(teach_mode)
    is_ec = bool(essay_critique) or is_essay_critique_request(q, tmode)
    w_task = '' if is_ec else detect_writing_task(q)
    q_no_tag = re.sub(r'^\s*(?:【[^】]{1,14}】\s*)+', '', q).strip() or q
    if len(q_no_tag) > 320:
        cut_cands = [q_no_tag.rfind(sep, 90, 320) for sep in ('。', '？', '！', '；', '，', ';', ',')]
        best_cut = max(cut_cands)
        q_head = q_no_tag[:best_cut] if best_cut > 0 else q_no_tag[:320]
    else:
        q_head = q_no_tag
    q_for_tts = re.sub(r'[=＝]\s*[？?□]?\s*$', ' 等于多少', q_head)
    q_spoken = clean_math_for_tts(q_for_tts).rstrip('。！？.!?，,；; ')
    if is_ec:
        return {
            'title': '品读原稿 · 找闪光点与升格处',
            'say': '好嘞，老师正在认真品读你写的习作片段，我们先找出写得生动的闪光亮点，再一起看看哪一处慢镜头细节还能升格得更出彩！',
            'board': f"【习作原稿品读】\n{q}\n品读主线：先夸闪光亮点 ⇒ 定位平淡句 ⇒ 五感慢镜头升格",
            'draw': sanitize_draw_ops([
                {'op': 'rect', 'x': 70, 'y': 145, 'w': 160, 'h': 72, 'role': 'main', 'label': '① 找闪光亮点'},
                {'op': 'arrow', 'x1': 238, 'y1': 181, 'x2': 272, 'y2': 181, 'role': 'aux', 'label': ''},
                {'op': 'rect', 'x': 280, 'y': 145, 'w': 160, 'h': 72, 'role': 'aux', 'label': '② 揪平淡流水句'},
                {'op': 'arrow', 'x1': 448, 'y1': 181, 'x2': 482, 'y2': 181, 'role': 'aux', 'label': ''},
                {'op': 'rect', 'x': 490, 'y': 145, 'w': 160, 'h': 72, 'role': 'main', 'label': '③ 细节动作升格'},
                {'op': 'note', 'x': 350, 'y': 378, 'label': '不替写不包办：保留你的真实原意，自己选出最打动人的升格句！'},
            ]),
        }
    if w_task:
        return {
            'title': '审题立意 · 搭三段式写作骨架',
            'say': f"好嘞，我们一起来构思这篇写作任务：{q_spoken}。先抓准题眼词和选材切口，一步步搭起开头、高潮细节与结尾骨架。",
            'board': f"【审题与选材构思】\n{q}\n构思主线：抓题眼避俗套 ⇒ 凤头点题 ⇒ 猪肚慢镜头 ⇒ 豹尾升华",
            'draw': sanitize_draw_ops([
                {'op': 'rect', 'x': 70, 'y': 145, 'w': 160, 'h': 72, 'role': 'main', 'label': '① 审题抓题眼'},
                {'op': 'arrow', 'x1': 238, 'y1': 181, 'x2': 272, 'y2': 181, 'role': 'aux', 'label': ''},
                {'op': 'rect', 'x': 280, 'y': 145, 'w': 160, 'h': 72, 'role': 'aux', 'label': '② 选材慢镜头'},
                {'op': 'arrow', 'x1': 448, 'y1': 181, 'x2': 482, 'y2': 181, 'role': 'aux', 'label': ''},
                {'op': 'rect', 'x': 490, 'y': 145, 'w': 160, 'h': 72, 'role': 'main', 'label': '③ 豹尾点睛升华'},
                {'op': 'note', 'x': 350, 'y': 378, 'label': '启发搭积木构思：拒绝千篇一律范文，写出属于你自己的真细节！'},
            ]),
        }
    if tmode == 'diagnose_draft':
        hdr_d = '【原题与草稿对照】' if from_ocr else '【草稿错因诊断】'
        return {
            'title': '审阅草稿 · 先夸亮点再找错因',
            'say': f"好嘞，老师正在仔细看你的解题草稿：{q_spoken}。我们先肯定你写对的步骤，再顺藤摸瓜找找哪一步关系拐了小弯。",
            'board': f"{hdr_d}\n{q}\n诊断思考：先核对写对的思路 ⇒ 再排查符号、份数或单位偏差",
            'draw': build_step0_warmup_draw(q),
        }
    hdr_s = '【识别原题】' if from_ocr else '【审题拆解】'
    is_hum_s0 = detect_subject(q) in ('chinese', 'english', 'history', 'civics')
    return {
        'title': '审题寻证 · 抓线索与设问' if is_hum_s0 else '审题拆解 · 找已知与目标',
        'say': (
            f"好嘞，我们一起来看这道题：{q_spoken}。边听老师读题，边圈出题干里的关键线索与设问角度，先想一想从哪儿切入。"
            if is_hum_s0 else
            f"好嘞，我们一起来看这道题：{q_spoken}。边听老师读题，边看看黑板上的已知条件和所求目标，先想一想中间缺哪座桥。"
        ),
        'board': (
            f"{hdr_s}\n{q}\n思考：题干线索与设问要求之间该抓哪个突破口？"
            if is_hum_s0 else
            f"{hdr_s}\n{q}\n思考：已知条件与所求目标之间需要搭什么桥？"
        ),
        'draw': build_step0_warmup_draw(q),
    }


def dedupe_step3_redundant_rungs(prev_steps: list, step_item: dict) -> dict:
    """If Step 3 chain[0] repeats the exact question already answered in Step 1/2 choice quiz, fold chain[0] into known board context."""
    if not isinstance(step_item, dict) or step_item.get('step') != 3:
        return step_item
    qz = step_item.get('quiz')
    if not isinstance(qz, dict) or qz.get('mode') != 'num':
        return step_item
    chain = qz.get('chain')
    if not isinstance(chain, list) or len(chain) < 2:
        return step_item
    r0 = chain[0]
    r0_q = re.sub(r'^第[①②③1-9一二三]步[，,：:\s]*', '', str(r0.get('q') or '')).strip()
    r0_ans = str(r0.get('ans') or '').strip()
    if not r0_q or not r0_ans:
        return step_item
    cn_map = {'1': '一', '2': '二两', '3': '三', '4': '四', '5': '五', '6': '六', '7': '七', '8': '八', '9': '九'}
    ans_tokens = [r0_ans] + list(cn_map.get(r0_ans, ''))
    norm_q0 = set(re.sub(r'[\s？?，,。：:0-9]', '', r0_q))
    if len(norm_q0) < 4:
        return step_item
    is_dup = False
    for ps in prev_steps or []:
        if not isinstance(ps, dict) or ps.get('step') not in (1, 2):
            continue
        pqz = ps.get('quiz')
        if not isinstance(pqz, dict) or pqz.get('mode') != 'choice':
            continue
        opts = pqz.get('opts') or []
        ans_i = pqz.get('ans', 0)
        if not (0 <= ans_i < len(opts)):
            continue
        right_opt = str(opts[ans_i])
        if not any(tok in right_opt for tok in ans_tokens):
            continue
        norm_pq = set(re.sub(r'[\s？?，,。：:0-9]', '', str(pqz.get('q') or '')))
        if norm_pq and len(norm_q0 & norm_pq) / max(1, min(len(norm_q0), len(norm_pq))) >= 0.7:
            is_dup = True
            break
    if not is_dup:
        return step_item

    bd = str(step_item.get('board') or '')
    if '□' in bd:
        bd = bd.replace('□', f'{r0_ans} ✓', 1)
        bd = re.sub(r'(?:第\s*)?(?:①|1)\s*(?:小)?(?:步)?(?:算出)?(?:的)?(?:结果|得数|答案|数值|值|商|和|差|积|倍数)', r0_ans, bd)
        bd = re.sub(r'(第\s*①\s*(?:小)?步)|①(?!\s*(?:小)?步)', lambda m: m.group(1) or r0_ans, bd)
        step_item['board'] = bd

    rem_chain = chain[1:]
    if len(rem_chain) == 1:
        only = rem_chain[0]
        qz['q'] = only.get('q') or qz.get('q') or ''
        qz['num_ans'] = str(only.get('ans') or qz.get('num_ans') or '')
        qz['unit'] = str(only.get('unit') or qz.get('unit') or '')
        if only.get('tip'):
            qz['tip'] = only['tip']
        qz['chain'] = []
    else:
        qz['chain'] = rem_chain
    return step_item


def enrich_step_draw_scaffolds(prev_steps: list, step_item: dict) -> dict:
    """Auto-inject missing perpendicular right_angle marker when an altitude/垂线 aux_line lands on a base edge."""
    if not isinstance(step_item, dict):
        return step_item
    cur_ops = step_item.get('draw') or []
    if not cur_ops:
        return step_item
    board_txt = str(step_item.get('board') or '') + ' ' + str(step_item.get('say') or '')
    has_perp_ctx = bool(re.search(r'[⊥]|垂直|垂线|垂足|作高|斜边上的高', board_txt))
    all_ops = []
    for ps in prev_steps or []:
        if isinstance(ps, dict) and ps.get('step', 0) > 0:
            for po in ps.get('draw') or []:
                if isinstance(po, dict) and po.get('op') == 'clear':
                    all_ops = []
                else:
                    all_ops.append(po)
    for co in cur_ops:
        if isinstance(co, dict) and co.get('op') == 'clear':
            all_ops = []
        else:
            all_ops.append(co)

    segs = []
    ra_pts = []
    for o in all_ops:
        op_t = o.get('op')
        if op_t == 'right_angle':
            ra_pts.append((float(o.get('x', 0)), float(o.get('y', 0))))
        elif op_t == 'line':
            segs.append((float(o['x1']), float(o['y1']), float(o['x2']), float(o['y2'])))
        elif op_t == 'rect':
            rx, ry, rw, rh = float(o['x']), float(o['y']), float(o['w']), float(o['h'])
            segs.extend([(rx, ry, rx + rw, ry), (rx + rw, ry, rx + rw, ry + rh), (rx + rw, ry + rh, rx, ry + rh), (rx, ry + rh, rx, ry)])
        elif op_t == 'polygon' and isinstance(o.get('pts'), list) and len(o['pts']) >= 3:
            pts = o['pts']
            for i in range(len(pts)):
                p1, p2 = pts[i], pts[(i + 1) % len(pts)]
                segs.append((float(p1[0]), float(p1[1]), float(p2[0]), float(p2[1])))

    # Normalize multi-row seg_bar total braces ('共75本'/'共72枚') to a right-side vertical curly brace so horizontal/left total braces never mislead or collide!
    seg_bars = [o for o in all_ops if o.get('op') == 'seg_bar']
    if len(seg_bars) >= 2:
        sb_ys = sorted(float(sb['y']) for sb in seg_bars)
        if sb_ys[-1] - sb_ys[0] >= 32:
            min_sb_x = min(float(sb['x']) for sb in seg_bars)
            max_sb_r = max(float(sb['x']) + float(sb['w']) for sb in seg_bars)
            for o in cur_ops:
                if o.get('op') != 'brace':
                    continue
                blbl = str(o.get('label') or '')
                if re.search(r'(?:共|一共|合起来|总[和数本枚只棵元人量长])', blbl) and not re.search(r'(?:差|多|少|剩余|剩下|等长|相等|观察|单)', blbl):
                    bx1, by1, bx2, by2 = float(o['x1']), float(o['y1']), float(o['x2']), float(o['y2'])
                    is_horiz = abs(bx2 - bx1) > abs(by2 - by1)
                    is_left_collide = not is_horiz and bx1 < min_sb_x + 20
                    if is_horiz or is_left_collide:
                        rx = round(min(568.0, max_sb_r + 22.0), 1)
                        o['x1'] = rx
                        o['x2'] = rx
                        o['y1'] = round(sb_ys[0] - 8.0, 1)
                        o['y2'] = round(sb_ys[-1] + 8.0, 1)

    added = []
    for o in cur_ops:
        if o.get('op') != 'aux_line':
            continue
        lbl = str(o.get('label') or '')
        if not (has_perp_ctx or re.search(r'高|垂', lbl)):
            continue
        ax1, ay1, ax2, ay2 = float(o['x1']), float(o['y1']), float(o['x2']), float(o['y2'])
        alen = (ax2 - ax1) ** 2 + (ay2 - ay1) ** 2
        if alen < 400:
            continue
        # Check which endpoint of aux_line is the perpendicular foot on an existing segment (skip only if THAT foot already has a right_angle!)
        best_foot = None
        for is_end2, (px, py, ox, oy) in enumerate(((ax2, ay2, ax1, ay1), (ax1, ay1, ax2, ay2))):
            if any((rx - px) ** 2 + (ry - py) ** 2 <= 484 for rx, ry in ra_pts):
                continue
            vx, vy = ox - px, oy - py
            vlen = (vx * vx + vy * vy) ** 0.5
            for sx1, sy1, sx2, sy2 in segs:
                ex, ey = sx2 - sx1, sy2 - sy1
                elen2 = ex * ex + ey * ey
                if elen2 < 400:
                    continue
                t = ((px - sx1) * ex + (py - sy1) * ey) / elen2
                if 0.04 <= t <= 0.96:
                    proj_x, proj_y = sx1 + t * ex, sy1 + t * ey
                    dist = ((px - proj_x) ** 2 + (py - proj_y) ** 2) ** 0.5
                    ortho = abs(vx * ex + vy * ey) / (vlen * (elen2 ** 0.5))
                    if dist <= 16.0 and ortho <= 0.28:
                        fx, fy = round(proj_x, 1), round(proj_y, 1)
                        if is_end2 == 0:
                            o['x2'], o['y2'] = fx, fy
                        else:
                            o['x1'], o['y1'] = fx, fy
                        for po in cur_ops:
                            if po.get('op') == 'point' and ((float(po['x']) - px) ** 2 + (float(po['y']) - py) ** 2) <= 144:
                                po['x'], po['y'] = fx, fy
                        best_foot = (fx, fy, -1 if vx < 0 else 1, -1 if vy < 0 else 1)
                        break
            if best_foot:
                break
        if best_foot:
            ra_op = {'op': 'right_angle', 'x': round(best_foot[0], 1), 'y': round(best_foot[1], 1), 'dx': best_foot[2], 'dy': best_foot[3], 'size': 13.0}
            added.append(ra_op)
            ra_pts.append((best_foot[0], best_foot[1]))
    if added:
        step_item['draw'] = cur_ops + added

    # Auto-infer spatial tap_target on Step 1-3 geometry & science/geography choice checkpoints when right option uniquely names a drawn point
    qz_st = step_item.get('quiz')
    if isinstance(qz_st, dict) and step_item.get('step') in (1, 2, 3) and qz_st.get('mode') == 'choice' and not qz_st.get('tap_target'):
        drawn_pts = {}
        for o in all_ops:
            if o.get('op') == 'point' and o.get('label'):
                m_pt = re.match(r'^([A-Z])(?![a-zA-Z])', str(o['label']).strip())
                if m_pt:
                    drawn_pts[m_pt.group(1)] = True
        opts_l = qz_st.get('opts') or []
        ans_i = qz_st.get('ans', 0)
        if len(drawn_pts) >= 2 and 0 <= ans_i < len(opts_l):
            r_opt = str(opts_l[ans_i])
            w_opts = ' '.join(str(opts_l[i]) for i in range(len(opts_l)) if i != ans_i)
            m_act = re.search(
                r'(?:过点|穿过(?:顶点|点)?|以(?:顶点|点)?|延长[A-Z]?至点?|(?:导管|管口|结构|部位|位置|山脚|山峰|顶点|点)\s*)([A-Z])(?![a-zA-Z])'
                r'|(?<![A-Za-z])([A-Z])\s*(?:点|处|端|管|口|部位|位置)',
                r_opt,
            )
            cand_pt = (m_act.group(1) or m_act.group(2)) if m_act else ''
            other_drawn_in_r = any(
                p != cand_pt and re.search(rf'(?<![A-Za-z]){p}(?![A-Za-z])', r_opt)
                for p in drawn_pts
            )
            cand_in_w = bool(cand_pt and re.search(
                rf'(?:点|导管|管口|结构|部位|位置|山脚|山峰)\s*{cand_pt}(?![a-zA-Z])|(?<![A-Za-z]){cand_pt}\s*(?:点|处|端|管|口|部位|位置)',
                w_opts,
            ))
            if cand_pt and cand_pt in drawn_pts and not other_drawn_in_r and not cand_in_w:
                qz_st['tap_target'] = cand_pt
                qz_st['tap_prompt'] = (
                    '也可直接在左图点选目标顶点闯关'
                    if re.search(r'过点|穿过|顶点|延长', r_opt) else
                    '也可直接在左图点选目标位置闯关'
                )
    return step_item


# ==================== Core AI Tutor Engine ====================
def _dedupe_history_entries(items: list) -> list:
    out = []
    seen_text_keys = set()
    for h in items:
        if not isinstance(h, dict) or not h.get('id'):
            continue
        q_norm = re.sub(r'\s+', ' ', str(h.get('question') or '')).strip()
        img_u = str(h.get('image_url') or '').strip()
        if q_norm and not img_u:
            tkey = (q_norm, _norm_mode(h.get('mode')))
            if tkey in seen_text_keys:
                continue
            seen_text_keys.add(tkey)
        out.append(h)
    return out[:20]


class CoWriteMonitor:
    """Grade-9 Silent Live Homework Co-Writer & Error-Hint Board (100% Zero-Audio).

    4-Stage Funnel:
      ① Lenovo 160x120 grayscale frame-diff motion/pause detector (19.2 KB buffer, 1.5s tick)
      ② .110 Qwen3.5-9B-Q8_0 local vision gatekeeper via encrypted /study/v1 bridge
      ③ Cloud flagship Grade-9 reviewer (gpt-6.1-sol -> gpt-6.1-terra -> deepseek) ONLY on newly finished items
      ④ Glanceable silent board cards + on-demand crop modal + 1-tap Socratic blackboard handoff
    """
    THUMB_W = 160
    THUMB_H = 120
    GRID_X = 10
    GRID_Y = 8
    POLL_INTERVAL_SEC = 1.5
    PAUSE_SETTLE_SEC = 3.8
    PAUSE_MODE_MAP = {'fast': 2.4, 'std': 3.8, 'deep': 6.5}
    MIN_GATE_INTERVAL_SEC = 8.0
    MAX_SESSION_SEC = 9000.0      # 2.5h auto-sleep ceiling
    MAX_IDLE_DESK_SEC = 1500.0    # 25m zero-motion auto-sleep
    MAX_BOARD_ITEMS = 30
    MAX_COWRITE_SNAPS = 30
    CONFIDENCE_FLOOR = 0.85

    def __init__(self, engine):
        self.eng = engine
        self.enabled = False
        self.phase = 'off'  # off | watching | writing | gatekeeping | reviewing
        self.subject = 'auto'  # auto | math | phys_chem | english
        self.pause_mode = 'std'  # fast (2.4s) | std (3.8s) | deep (6.5s)
        self.pencil_boost = False
        self.rotate_hint = 0
        self.lighting_warn = ''
        self.page_epoch = 1
        self.seq = 1
        self.started_at = 0
        self.started_mono = 0.0
        self.last_motion_mono = 0.0
        self.last_gate_mono = 0.0
        self.last_check_ts = 0
        self.last_note = '开启后静默守护书写，停笔约 4 秒自动巡检新写完的题目（全程零语音不打断）'
        self.gate_engine = ''
        self.prev_thumb = b''
        self.anchor_thumb = b''
        self.saw_activity = False
        self._still_streak = 0
        self._err_streak = 0
        self._backoff_until_mono = 0.0
        self._last_disp_wake_mono = 0.0
        self._last_stage_page_id = ''
        self._last_archive_key = ''
        self._has_more_pending = False
        self._clear_gen = 0
        self._watch_task = None
        self._pipeline_lock = asyncio.Lock()
        self._study_cfg_sig = None
        self._study_cfg_url = ''
        self._study_sec_sig = None
        self._study_sec_bytes = None
        self._loaded_state_path = None
        self._last_state_written = None
        self.items = []
        self.recent_sessions = []
        self.stats = {'gate_calls': 0, 'review_calls': 0, 'unclear_skipped': 0, 'dark_skipped': 0, 'blur_skipped': 0}
        self._ensure_loaded()

    @staticmethod
    def _norm_subject(val) -> str:
        s = str(val or '').strip().lower()
        return s if s in ('auto', 'math', 'phys_chem', 'english', 'chinese') else 'auto'

    @classmethod
    def _norm_pause_mode(cls, val) -> str:
        s = str(val or '').strip().lower()
        return s if s in cls.PAUSE_MODE_MAP else 'std'

    _CIRCLED_MAP = str.maketrans({
        '（': '(', '）': ')', '［': '[', '］': ']',
        '①': '(1)', '②': '(2)', '③': '(3)', '④': '(4)', '⑤': '(5)',
        '⑥': '(6)', '⑦': '(7)', '⑧': '(8)', '⑨': '(9)', '⑩': '(10)',
    })

    @classmethod
    def _norm_prob_key(cls, no_str: str) -> str:
        s = re.sub(r'\s+', '', str(no_str or '').strip()).translate(cls._CIRCLED_MAP)
        sec_pre = ''
        m_sec = re.match(r'^(?:第?([一二三四五六])(?:大题|[、.．])?|(选择|填空|解答|计算|实验|阅读|完形)题?)', s)
        if m_sec:
            if m_sec.group(1):
                sec_pre = {'一': 's1:', '二': 's2:', '三': 's3:', '四': 's4:', '五': 's5:', '六': 's6:'}.get(m_sec.group(1), '')
            elif m_sec.group(2):
                sec_pre = {'选择': 'mc:', '填空': 'fb:', '解答': 'qa:', '计算': 'ca:', '实验': 'ex:', '阅读': 'rd:', '完形': 'cz:'}.get(m_sec.group(2), '')
            s_tail = s[m_sec.end():]
        else:
            s_tail = s
        s_tail = re.sub(r'第\(?([0-9a-zA-Z]+)\)?小?问', r'(\1)', s_tail)
        s_tail = re.sub(r'(\d)[.．、:：题第小问]+(?=\([0-9a-zA-Z]+\))', r'\1', s_tail)
        m = re.search(r'(\d+(?:[-~～—]\d+)?(?:\([0-9a-zA-Z]+\))*)', s_tail)
        if m:
            norm_m = re.sub(r'[~～—]+', '-', m.group(1))
            return (sec_pre + norm_m)[:18]
        return (sec_pre + s_tail)[:18] if s_tail else s[:18]

    @staticmethod
    def _box_iou(b1: dict, b2: dict) -> float:
        if not isinstance(b1, dict) or not isinstance(b2, dict):
            return 0.0
        try:
            ax1, ay1 = float(b1['x']), float(b1['y'])
            ax2, ay2 = ax1 + float(b1['w']), ay1 + float(b1['h'])
            bx1, by1 = float(b2['x']), float(b2['y'])
            bx2, by2 = bx1 + float(b2['w']), by1 + float(b2['h'])
        except (KeyError, TypeError, ValueError):
            return 0.0
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        if ix2 <= ix1 or iy2 <= iy1:
            return 0.0
        inter = (ix2 - ix1) * (iy2 - iy1)
        union = max(1e-6, (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter)
        return inter / union

    @classmethod
    def _matches_checked_entry(cls, pkey: str, box: dict, entry: dict, stage_page_id: str = '') -> bool:
        e_pid = str(entry.get('stage_page_id') or '').strip()
        s_pid = str(stage_page_id or '').strip()
        if s_pid and e_pid and s_pid != e_pid:
            return False
        eb = entry.get('box')
        if isinstance(box, dict) and isinstance(eb, dict):
            iou = cls._box_iou(box, eb)
            if iou >= 0.44:
                epk = str(entry.get('prob_key') or '')
                # Never merge distinct numbered problems or subquestions (e.g. 1 vs 2, 15(1) vs 15(2)) when padded boxes overlap!
                if (
                    pkey and epk and pkey != epk
                    and pkey not in ('本题', '框选题') and epk not in ('本题', '框选题')
                    and not pkey.startswith('crop_') and not epk.startswith('crop_')
                ):
                    return iou >= 0.84
                return True
            if pkey and entry.get('prob_key') == pkey:
                cy1 = float(box.get('y', 0.0)) + float(box.get('h', 0.0)) * 0.5
                cy2 = float(eb.get('y', 0.0)) + float(eb.get('h', 0.0)) * 0.5
                return abs(cy1 - cy2) < 0.28
            return False
        return bool(pkey and entry.get('prob_key') == pkey)

    def _maybe_advance_epoch_for_stage_page(self, stage_page_id: str):
        pid = str(stage_page_id or '').strip()[:28]
        if not pid:
            return
        has_other_on_epoch = any(
            int(it.get('page_epoch') or 1) == self.page_epoch
            and str(it.get('stage_page_id') or '').strip()
            and str(it.get('stage_page_id') or '').strip() != pid
            for it in self.items
        )
        if (self._last_stage_page_id and self._last_stage_page_id != pid and has_other_on_epoch) or has_other_on_epoch:
            self.page_epoch += 1
            self.anchor_thumb = b''
            self._has_more_pending = False
        self._last_stage_page_id = pid

    @staticmethod
    def _safe_int(val, default: int = 0) -> int:
        try:
            return int(val)
        except (TypeError, ValueError, OverflowError):
            return default

    def _ensure_loaded(self):
        sp = _cowrite_state_path()
        if self._loaded_state_path == sp:
            return
        self._loaded_state_path = sp
        raw = _read_safe_json(sp, {})
        if not isinstance(raw, dict):
            return
        self.subject = self._norm_subject(raw.get('subject'))
        self.pause_mode = self._norm_pause_mode(raw.get('pause_mode'))
        self.pencil_boost = bool(raw.get('pencil_boost'))
        self.page_epoch = max(1, self._safe_int(raw.get('page_epoch'), 1))
        self.seq = max(1, self._safe_int(raw.get('seq'), 1))
        self._last_stage_page_id = str(raw.get('last_stage_page_id') or '')[:28]
        self._last_archive_key = str(raw.get('last_archive_key') or '')[:400]
        st_in = raw.get('stats') if isinstance(raw.get('stats'), dict) else {}
        for k in ('gate_calls', 'review_calls', 'unclear_skipped', 'dark_skipped', 'blur_skipped'):
            self.stats[k] = max(0, self._safe_int(st_in.get(k), 0))
        cleaned = []
        raw_items = raw.get('items') if isinstance(raw.get('items'), list) else []
        for it in raw_items[:self.MAX_BOARD_ITEMS]:
            if not isinstance(it, dict) or not it.get('id') or it.get('status') not in ('ok', 'warn'):
                continue
            sn = str(it.get('snap_name') or '').strip()
            if sn and not re.fullmatch(r'c0_[0-9a-f]{12}\.jpg', sn):
                sn = ''
            cleaned.append({
                'id': str(it['id'])[:20],
                'no': str(it.get('no') or '本题')[:16],
                'prob_key': str(it.get('prob_key') or self._norm_prob_key(it.get('no') or ''))[:18],
                'page_epoch': max(1, self._safe_int(it.get('page_epoch'), 1)),
                'status': it['status'],
                'subject': str(it.get('subject') or 'math')[:12],
                'topic': str(it.get('topic') or '')[:22],
                'step_where': str(it.get('step_where') or '')[:20],
                'checkpoint': str(it.get('checkpoint') or '')[:64],
                'deep_hint': str(it.get('deep_hint') or '')[:84],
                'formula_tip': str(it.get('formula_tip') or '')[:80],
                'warn_step_where': str(it.get('warn_step_where') or '')[:20],
                'warn_checkpoint': str(it.get('warn_checkpoint') or '')[:64],
                'warn_deep_hint': str(it.get('warn_deep_hint') or '')[:84],
                'warn_formula_tip': str(it.get('warn_formula_tip') or (it.get('formula_tip') if it['status'] == 'warn' else '') or '')[:80],
                'ocr_stem': str(it.get('ocr_stem') or '')[:180],
                'snap_name': sn,
                'box': PhoneStudioManager._sanitize_crop_box(it['box']) if isinstance(it.get('box'), dict) else None,
                'ts': max(0, self._safe_int(it.get('ts'), 0)),
                'ts_ms': float(it.get('ts_ms') or max(0, self._safe_int(it.get('ts'), 0))),
                'resolved': bool(it.get('resolved')),
                'corrected_pass': bool(it.get('corrected_pass')),
                'starred': bool(it.get('starred')),
                'enrolled_mistake': bool(it.get('enrolled_mistake')),
                'mistake_id': str(it.get('mistake_id') or '')[:24],
                'ever_graduated': bool(it.get('ever_graduated')),
                'blackboard_solved': bool(it.get('blackboard_solved')),
                'variant_passed': bool(it.get('variant_passed')),
                'stage_page_id': str(it.get('stage_page_id') or '')[:28],
                'orig_stage_page_id': str(it.get('orig_stage_page_id') or it.get('stage_page_id') or '')[:28],
            })
        self.items = cleaned
        rec_hist = []
        raw_recs = raw.get('recent_sessions') if isinstance(raw.get('recent_sessions'), list) else []
        for rs in raw_recs[:7]:
            if not isinstance(rs, dict):
                continue
            with contextlib.suppress(TypeError, ValueError):
                tot_r = max(1, int(rs.get('total') or 0))
                rec_hist.append({
                    'ts': int(rs.get('ts') or 0),
                    'total': tot_r,
                    'first_pass_ok': max(0, int(rs.get('first_pass_ok') or 0)),
                    'accuracy_pct': max(0, min(100, int(rs.get('accuracy_pct') or 0))),
                    'corrected': max(0, int(rs.get('corrected') or 0)),
                    'mastered': max(0, self._safe_int(rs.get('mastered'), 0)),
                    'focus_min': max(0, self._safe_int(rs.get('focus_min'), 0)),
                    'weak_topics': [str(t)[:18] for t in (rs.get('weak_topics') or [])[:3] if str(t).strip()],
                })
        self.recent_sessions = rec_hist

    def _save_state(self):
        self._ensure_loaded()
        if len(self.items) > self.MAX_BOARD_ITEMS:
            evicted = self.items[self.MAX_BOARD_ITEMS:]
            self.items = self.items[:self.MAX_BOARD_ITEMS]
            keep_snaps = {it.get('snap_name') for it in self.items if it.get('snap_name')}
            if hasattr(self.eng, '_ensure_mistakes_loaded'):
                self.eng._ensure_mistakes_loaded()
            for m in (getattr(self.eng, 'mistakes', ()) or ()):
                if isinstance(m, dict) and str(m.get('image_url') or '').startswith('/api/tutor/snap/c0_'):
                    keep_snaps.add(str(m['image_url']).rsplit('/', 1)[-1])
            for ev in evicted:
                sn_ev = ev.get('snap_name') or ''
                if sn_ev and sn_ev not in keep_snaps:
                    with contextlib.suppress(OSError):
                        (SNAP_DIR / sn_ev).unlink(missing_ok=True)
        self.recent_sessions = self.recent_sessions[:7]
        payload = {
            'subject': self.subject,
            'pause_mode': self.pause_mode,
            'pencil_boost': bool(self.pencil_boost),
            'page_epoch': self.page_epoch,
            'last_stage_page_id': self._last_stage_page_id,
            'last_archive_key': self._last_archive_key,
            'seq': self.seq,
            'stats': self.stats,
            'recent_sessions': self.recent_sessions,
            'items': self.items,
        }
        st_path = _cowrite_state_path()
        raw_json = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        cache_key = (str(st_path), raw_json)
        if getattr(self, '_last_state_written', None) == cache_key and st_path.exists():
            return
        with contextlib.suppress(Exception):
            _atomic_write_bytes(st_path, raw_json.encode('utf-8'), mode=0o600)
            self._last_state_written = cache_key

    @classmethod
    def _make_gray_thumb_sync(cls, jpeg_bytes: bytes) -> bytes:
        if not _HAS_PIL or not jpeg_bytes:
            return b''
        with _PILImage.open(io.BytesIO(jpeg_bytes)) as im:
            with contextlib.suppress(Exception):
                im.draft('L', (cls.THUMB_W, cls.THUMB_H))
            g = im.convert('L').resize((cls.THUMB_W, cls.THUMB_H), _PILImage.BILINEAR)
            return g.tobytes()

    def _poke_display_wake(self, force: bool = False) -> bool:
        """Keep screen awake & WebKit unfrozen via PREVIEW_SOCKET while student is actively writing or receiving silent hints."""
        if CALL_FLAG.exists() or MIRROR_FLAG.exists():
            return False
        now_m = time.monotonic()
        min_gap = 2.0 if force else 20.0
        if self._last_disp_wake_mono > 0 and (now_m - self._last_disp_wake_mono) < min_gap:
            return False
        sock_p = os.environ.get('TUTOR_DISPLAY_SOCK', '/run/user/1000/thinksmart-display.sock')
        if not sock_p or not os.path.exists(sock_p):
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as ch:
                ch.setblocking(False)
                ch.sendto(b'{"wake":true}', sock_p)
            self._last_disp_wake_mono = now_m
            return True
        except OSError:
            return False

    def _sync_page_check_marks(self, page_id: str = '', clear_if_empty: bool = False):
        """Mirror Co-Write items onto PhoneStudio worksheet check_marks so #viewPhone & mobile.html show live green/amber boxes."""
        pid = str(page_id or '').strip()
        if not pid:
            return
        ps = getattr(self.eng, 'phone_studio', None)
        if ps is None:
            return
        matched = [
            it for it in self.items
            if it.get('stage_page_id') == pid and isinstance(it.get('box'), dict)
        ]
        if not matched:
            pg_cur = next((p for p in (getattr(ps, 'pages', None) or ()) if isinstance(p, dict) and p.get('id') == pid), None)
            if pg_cur is not None and (clear_if_empty or str(pg_cur.get('check_summary') or '').startswith('静默伴写')):
                with contextlib.suppress(Exception):
                    ps.set_check_marks(pid, [], summary='')
            return
        marks = []
        ok_n = 0
        fix_n = 0
        for it in reversed(matched[:12]):
            is_ok = (it.get('status') == 'ok' or bool(it.get('resolved')))
            if is_ok:
                ok_n += 1
            else:
                fix_n += 1
            marks.append({
                'no': str(it.get('no') or '本题')[:10],
                'status': 'ok' if is_ok else 'wrong',
                'box': it['box'],
                'note': str(it.get('checkpoint') or ('作答正确 ✓' if is_ok else '建议回查关键步骤'))[:36],
                'cowrite_id': str(it.get('id') or ''),
            })
        summary = f"静默伴写已核 {len(marks)} 题：全对/已订正 {ok_n} 题" + (f"，待自查 {fix_n} 题" if fix_n > 0 else " ✓")
        with contextlib.suppress(Exception):
            ps.set_check_marks(pid, marks, summary=summary)

    def on_stage_page_rotated(self, page_id: str, steps_cw: int):
        pid = str(page_id or '').strip()
        steps = int(steps_cw or 0) % 4
        if not pid or not steps:
            return
        self._ensure_loaded()
        changed = False
        for it in self.items:
            if it.get('stage_page_id') == pid and isinstance(it.get('box'), dict):
                bx = it['box']
                for _ in range(steps):
                    bx = PhoneStudioManager._rotate_crop_cw90(bx)
                it['box'] = bx
                changed = True
        if changed:
            self.seq += 1
            self._save_state()

    def on_stage_page_removed(self, page_id: str = '', clear_all: bool = False):
        self._ensure_loaded()
        pid = str(page_id or '').strip()
        changed = False
        for it in self.items:
            it_pid = str(it.get('stage_page_id') or '').strip()
            it_orig_pid = str(it.get('orig_stage_page_id') or '').strip()
            if it_pid and (clear_all or it_pid == pid):
                it['stage_page_id'] = ''
                changed = True
            if it_orig_pid and (clear_all or it_orig_pid == pid):
                it['orig_stage_page_id'] = ''
                changed = True
        if clear_all or (pid and self._last_stage_page_id == pid):
            self._last_stage_page_id = ''
            changed = True
        if changed:
            self.seq += 1
            self._save_state()

    @classmethod
    def _make_gate_jpeg_sync(cls, jpeg_bytes: bytes, max_side: int = 728) -> bytes:
        """Downscale Gatekeeper copy to <=728px long-edge (~390 ViT tokens vs 736, 2x faster GPU eval) while Stage-3 keeps full hi_jpg."""
        if not _HAS_PIL or not jpeg_bytes:
            return jpeg_bytes
        with _PILImage.open(io.BytesIO(jpeg_bytes)) as im:
            iw, ih = im.size
            if max(iw, ih) <= max_side and len(jpeg_bytes) <= 165000:
                return jpeg_bytes
            with contextlib.suppress(Exception):
                im.draft('RGB', (max_side, max_side))
            rgb = im.convert('RGB')
            rw, rh = rgb.size
            if max(rw, rh) > max_side:
                sc = float(max_side) / float(max(rw, rh))
                rgb = rgb.resize((max(48, int(round(rw * sc))), max(48, int(round(rh * sc)))), _PILImage.BILINEAR)
            buf = io.BytesIO()
            rgb.save(buf, format='JPEG', quality=78, optimize=True)
            return buf.getvalue()

    @classmethod
    def _frame_quality_metrics_sync(cls, jpeg_bytes: bytes) -> dict:
        """C-speed <1ms Pillow ambient darkness, glare & edge-sharpness pre-check on 160x120 grayscale."""
        if not _HAS_PIL or not jpeg_bytes:
            return {'mean_luma': 140.0, 'contrast_span': 80, 'edge_mean': 12.0, 'glare_ratio': 0.0}
        with _PILImage.open(io.BytesIO(jpeg_bytes)) as im:
            with contextlib.suppress(Exception):
                im.draft('L', (cls.THUMB_W, cls.THUMB_H))
            g = im.convert('L').resize((cls.THUMB_W, cls.THUMB_H), _PILImage.BILINEAR)
            raw = g.tobytes()
            n = max(1, len(raw))
            mean_luma = sum(raw) / float(n)
            hist = g.histogram()
            acc = 0
            p5, p95 = 0, 255
            t5, t95 = int(n * 0.05), int(n * 0.95)
            got5 = False
            for i, c in enumerate(hist):
                acc += c
                if not got5 and acc >= t5:
                    p5 = i
                    got5 = True
                if acc >= t95:
                    p95 = i
                    break
            c_raw = g.crop((16, 12, cls.THUMB_W - 16, cls.THUMB_H - 12)).tobytes()
            glare_ratio = sum(1 for v in c_raw if v >= 251) / float(max(1, len(c_raw)))
            edge_mean = 12.0
            if _PILFilter is not None:
                with contextlib.suppress(Exception):
                    ed = g.filter(_PILFilter.FIND_EDGES).crop((16, 12, cls.THUMB_W - 16, cls.THUMB_H - 12)).tobytes()
                    if ed:
                        edge_mean = sum(ed) / float(len(ed))
            return {
                'mean_luma': round(mean_luma, 2),
                'contrast_span': max(0, p95 - p5),
                'edge_mean': round(edge_mean, 2),
                'glare_ratio': round(glare_ratio, 3),
            }

    @classmethod
    def _thumb_diff_metrics(cls, b1: bytes, b2: bytes) -> dict:
        n = cls.THUMB_W * cls.THUMB_H
        if len(b1) != n or len(b2) != n:
            return {'mean_diff': 99.0, 'changed_cells': cls.GRID_X * cls.GRID_Y, 'max_cell_diff': 99.0, 'max_px_diff': 255, 'ink_pixels': n}
        # Normalize global ambient luminance drift so cloud/lamp shifts never trigger false ink detection
        shift = int(round((sum(b2) - sum(b1)) / float(n)))
        cw = cls.THUMB_W // cls.GRID_X
        ch = cls.THUMB_H // cls.GRID_Y
        cell_area = cw * ch
        total_abs = 0
        changed_cells = 0
        max_cell = 0.0
        max_px = 0
        ink_px = 0
        w = cls.THUMB_W
        for gy in range(cls.GRID_Y):
            y0 = gy * ch
            for gx in range(cls.GRID_X):
                x0 = gx * cw
                csum = 0
                for yy in range(y0, y0 + ch):
                    row_off = yy * w + x0
                    for xx in range(row_off, row_off + cw):
                        d = abs((b2[xx] - b1[xx]) - shift)
                        csum += d
                        if d > max_px:
                            max_px = d
                        if d >= 20:
                            ink_px += 1
                total_abs += csum
                cmean = csum / float(cell_area)
                if cmean > max_cell:
                    max_cell = cmean
                if cmean >= 5.2:
                    changed_cells += 1
        return {
            'mean_diff': round(total_abs / float(n), 3),
            'changed_cells': changed_cells,
            'max_cell_diff': round(max_cell, 3),
            'max_px_diff': max_px,
            'ink_pixels': ink_px,
        }

    @classmethod
    def _box_local_diff(cls, b1: bytes, b2: bytes, box: dict) -> float:
        n = cls.THUMB_W * cls.THUMB_H
        if len(b1) != n or len(b2) != n or not isinstance(box, dict):
            return 0.0
        x0 = max(0, min(cls.THUMB_W - 4, int(float(box.get('x', 0.0)) * cls.THUMB_W)))
        y0 = max(0, min(cls.THUMB_H - 4, int(float(box.get('y', 0.0)) * cls.THUMB_H)))
        x1 = max(x0 + 4, min(cls.THUMB_W, int((float(box.get('x', 0.0)) + float(box.get('w', 1.0))) * cls.THUMB_W)))
        y1 = max(y0 + 4, min(cls.THUMB_H, int((float(box.get('y', 0.0)) + float(box.get('h', 1.0))) * cls.THUMB_H)))
        shift = int(round((sum(b2) - sum(b1)) / float(n)))
        acc = 0
        cnt = (x1 - x0) * (y1 - y0)
        for yy in range(y0, y1):
            off = yy * cls.THUMB_W
            for xx in range(x0, x1):
                acc += abs((b2[off + xx] - b1[off + xx]) - shift)
        return acc / float(max(1, cnt))

    @classmethod
    def _expanded_revision_box(cls, box: dict) -> dict:
        if not isinstance(box, dict):
            return box
        fx = _clamp(box.get('x'), 0.0, 0.95, 0.0)
        fy = _clamp(box.get('y'), 0.0, 0.95, 0.0)
        fw = _clamp(box.get('w'), 0.05, 1.0 - fx, 1.0)
        fh = _clamp(box.get('h'), 0.05, 1.0 - fy, 1.0)
        nx = max(0.0, fx - 0.02)
        ny = max(0.0, fy - 0.03)
        nw = min(1.0 - nx, fw + 0.04)
        nh = min(1.0 - ny, (fy - ny) + fh + 0.09)
        return PhoneStudioManager._sanitize_crop_box({'x': nx, 'y': ny, 'w': nw, 'h': nh}) or box

    @classmethod
    def _has_ink_outside_boxes(cls, b1: bytes, b2: bytes, boxes: list) -> bool:
        n = cls.THUMB_W * cls.THUMB_H
        if len(b1) != n or len(b2) != n:
            return True
        rects = []
        for box in (boxes or ()):
            if not isinstance(box, dict):
                continue
            x0 = max(0, min(cls.THUMB_W - 1, int(float(box.get('x', 0.0)) * cls.THUMB_W)))
            y0 = max(0, min(cls.THUMB_H - 1, int(float(box.get('y', 0.0)) * cls.THUMB_H)))
            x1 = max(x0 + 1, min(cls.THUMB_W, int((float(box.get('x', 0.0)) + float(box.get('w', 1.0))) * cls.THUMB_W)))
            y1 = max(y0 + 1, min(cls.THUMB_H, int((float(box.get('y', 0.0)) + float(box.get('h', 1.0))) * cls.THUMB_H)))
            rects.append((x0, y0, x1, y1))
        if not rects:
            return True
        shift = int(round((sum(b2) - sum(b1)) / float(n)))
        acc = 0
        ink_px = 0
        max_px = 0
        cnt = 0
        for yy in range(cls.THUMB_H):
            off = yy * cls.THUMB_W
            row_rects = [(x0, x1) for (x0, y0, x1, y1) in rects if y0 <= yy < y1]
            for xx in range(cls.THUMB_W):
                if any(rx0 <= xx < rx1 for rx0, rx1 in row_rects):
                    continue
                cnt += 1
                d = abs((b2[off + xx] - b1[off + xx]) - shift)
                acc += d
                if d > max_px:
                    max_px = d
                if d >= 20:
                    ink_px += 1
        if cnt < 120:
            return False
        return (acc / float(cnt)) >= 2.6 or (ink_px >= 3 and max_px >= 26)

    @classmethod
    def _box_has_ink_change(cls, b1: bytes, b2: bytes, box: dict) -> bool:
        n = cls.THUMB_W * cls.THUMB_H
        if len(b1) != n or len(b2) != n or not isinstance(box, dict):
            return False
        x0 = max(0, min(cls.THUMB_W - 4, int(float(box.get('x', 0.0)) * cls.THUMB_W)))
        y0 = max(0, min(cls.THUMB_H - 4, int(float(box.get('y', 0.0)) * cls.THUMB_H)))
        x1 = max(x0 + 4, min(cls.THUMB_W, int((float(box.get('x', 0.0)) + float(box.get('w', 1.0))) * cls.THUMB_W)))
        y1 = max(y0 + 4, min(cls.THUMB_H, int((float(box.get('y', 0.0)) + float(box.get('h', 1.0))) * cls.THUMB_H)))
        shift = int(round((sum(b2) - sum(b1)) / float(n)))
        acc = 0
        ink_px = 0
        max_px = 0
        cnt = max(1, (x1 - x0) * (y1 - y0))
        for yy in range(y0, y1):
            off = yy * cls.THUMB_W
            for xx in range(x0, x1):
                d = abs((b2[off + xx] - b1[off + xx]) - shift)
                acc += d
                if d > max_px:
                    max_px = d
                if d >= 20:
                    ink_px += 1
        return (acc / float(cnt)) >= 3.2 or (ink_px >= 3 and max_px >= 26)

    @staticmethod
    def _crop_problem_jpeg_sync(full_jpg: bytes, box: dict, pencil_boost: bool = False, extra_top: float = 0.0, extra_bottom: float = 0.0) -> bytes:
        if not _HAS_PIL or not full_jpg:
            return full_jpg
        with _PILImage.open(io.BytesIO(full_jpg)) as im:
            im = im.convert('RGB')
            iw, ih = im.size
            if isinstance(box, dict) and iw >= 64 and ih >= 64:
                bx = _clamp(box.get('x'), 0.0, 0.94, 0.0)
                by = _clamp(box.get('y'), 0.0, 0.95, 0.0)
                bw = _clamp(box.get('w'), 0.06, 1.0 - bx, 1.0)
                bh = _clamp(box.get('h'), 0.05, 1.0 - by, 1.0)
                # Generous context margin so Grade-9 parent stems, left-margin '解：/答：', figures, and final lines are never clipped
                if bx <= 0.42 or bw >= 0.48:
                    x0, x1 = int(round(0.005 * iw)), int(round(0.995 * iw))
                else:
                    x0 = max(0, int(round((bx - 0.08) * iw)))
                    x1 = min(iw, int(round((bx + bw + 0.06) * iw)))
                top_pad = 0.155 + max(0.0, min(0.25, float(extra_top or 0.0)))
                bot_pad = 0.105 + max(0.0, min(0.22, float(extra_bottom or 0.0)))
                y0 = max(0, int(round((by - top_pad) * ih)))
                y1 = min(ih, int(round((by + bh + bot_pad) * ih)))
                if x1 - x0 >= 48 and y1 - y0 >= 36:
                    im = im.crop((x0, y0, x1, y1))
            cw, ch = im.size
            if cw > 1360:
                nh = max(36, int(round(ch * (1360.0 / cw))))
                im = im.resize((1360, nh), _PILImage.LANCZOS)
                cw, ch = im.size
            # Flat-field desk-lamp & hand shadow normalization (C-speed libimaging, preserves red/blue/black ink!)
            if _PILChops is not None and _PILFilter is not None and _PILImageOps is not None and cw >= 64 and ch >= 36:
                with contextlib.suppress(Exception):
                    sw, sh = max(8, cw // 12), max(6, ch // 12)
                    bg_small = im.convert('L').resize((sw, sh), _PILImage.BILINEAR)
                    bg_small = bg_small.filter(_PILFilter.MaxFilter(5)).filter(_PILFilter.BoxBlur(2))
                    lo_b, hi_b = bg_small.getextrema()
                    if hi_b >= 115 and (hi_b - lo_b) >= 22:
                        bg_l = bg_small.resize((cw, ch), _PILImage.BILINEAR)
                        bg_rgb = _PILImage.merge('RGB', (bg_l, bg_l, bg_l))
                        ink_diff = _PILChops.subtract(bg_rgb, im)
                        im = _PILImageOps.invert(ink_diff)
            min_h = int(round(cw * 0.20))
            if ch < min_h:
                padded = _PILImage.new('RGB', (cw, min_h), (255, 255, 255))
                padded.paste(im, (0, (min_h - ch) // 2))
                im = padded
            if _PILImageOps is not None:
                with contextlib.suppress(Exception):
                    im = _PILImageOps.autocontrast(im, cutoff=0.35)
            # Adaptive faint-pencil white-anchored LUT: darkens mid-gray 2B pencil strokes while keeping paper >=236 clean white
            with contextlib.suppress(Exception):
                h_g = im.convert('L').histogram()
                tot_px = max(1, im.size[0] * im.size[1])
                dark_ratio = sum(h_g[:105]) / float(tot_px)
                pencil_ratio = sum(h_g[125:198]) / float(tot_px)
                ink_gain = 1.32 if pencil_boost else (1.18 if (dark_ratio < 0.032 and pencil_ratio >= 0.018) else 1.0)
                if ink_gain > 1.01:
                    lut = [
                        c if c >= 236 else max(
                            0,
                            min(
                                255,
                                int(round(255.0 - (255.0 - c) * (1.0 + (ink_gain - 1.0) * min(1.0, (236 - c) / 24.0)))),
                            ),
                        )
                        for c in range(256)
                    ]
                    im = im.point(lut * 3)
            if pencil_boost and _PILImageEnhance is not None:
                with contextlib.suppress(Exception):
                    im = _PILImageEnhance.Contrast(im).enhance(1.22)
            if _PILFilter is not None:
                with contextlib.suppress(Exception):
                    im = im.filter(_PILFilter.UnsharpMask(radius=1.1, percent=118, threshold=3))
            buf = io.BytesIO()
            im.save(buf, format='JPEG', quality=85, optimize=True)
            return buf.getvalue()

    def _save_cowrite_snap(self, crop_jpg: bytes) -> str:
        if not crop_jpg:
            return ''
        SNAP_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
        snap_name = f"c0_{uuid.uuid4().hex[:12]}.jpg"
        _atomic_write_bytes(SNAP_DIR / snap_name, crop_jpg, mode=0o600)
        self._prune_cowrite_snaps(keep_extra=snap_name)
        return snap_name

    def _prune_cowrite_snaps(self, keep_extra: str = ''):
        if not SNAP_DIR.is_dir():
            return
        c_entries = []
        for p in SNAP_DIR.glob('c0_*.jpg'):
            with contextlib.suppress(OSError):
                st = p.lstat()
                if stat.S_ISREG(st.st_mode):
                    c_entries.append((st.st_mtime_ns, p))
        c_entries.sort(key=lambda x: x[0], reverse=True)
        protected = {keep_extra} if keep_extra else set()
        if hasattr(self.eng, '_ensure_mistakes_loaded'):
            self.eng._ensure_mistakes_loaded()
        mb_protected = set()
        for m in getattr(self.eng, 'mistakes', ()) or ():
            if isinstance(m, dict):
                iu = str(m.get('image_url') or '')
                if iu.startswith('/api/tutor/snap/c0_'):
                    mb_protected.add(iu.rsplit('/', 1)[-1])
        for it in self.items[:self.MAX_BOARD_ITEMS]:
            if it.get('snap_name'):
                protected.add(it['snap_name'])
        existing_on_disk = set()
        orphan_kept = 0
        for _, p in c_entries:
            if p.name in mb_protected or p.name in protected:
                existing_on_disk.add(p.name)
                continue
            if orphan_kept < 2 and (len(existing_on_disk) + orphan_kept) < self.MAX_COWRITE_SNAPS:
                orphan_kept += 1
                existing_on_disk.add(p.name)
                continue
            with contextlib.suppress(OSError):
                p.unlink(missing_ok=True)
        for it in self.items:
            sn_cur = it.get('snap_name') or ''
            if sn_cur and sn_cur not in existing_on_disk and not (SNAP_DIR / sn_cur).is_file():
                it['snap_name'] = ''

    def _load_study_bridge_creds(self) -> tuple:
        cfg_p = Path(os.environ.get('TUTOR_STUDY_CONFIG', str(STUDY_CFG_PATH)))
        sec_p = Path(os.environ.get('TUTOR_STUDY_SECRET', str(STUDY_SECRET_PATH)))
        try:
            st_c = os.lstat(str(cfg_p))
            st_s = os.lstat(str(sec_p))
            if not (stat.S_ISREG(st_c.st_mode) and stat.S_ISREG(st_s.st_mode)):
                return '', None
            c_sig = (st_c.st_ino, st_c.st_mtime_ns, st_c.st_size)
            s_sig = (st_s.st_ino, st_s.st_mtime_ns, st_s.st_size)
            if c_sig != self._study_cfg_sig:
                _, raw_c = _read_regular_bytes(cfg_p, 65536)
                c_obj = json.loads(raw_c.decode('utf-8'))
                v_url = str(c_obj.get('vision_url') or '').strip() if isinstance(c_obj, dict) else ''
                self._study_cfg_url = v_url if v_url.startswith(('http://', 'https://')) else ''
                self._study_cfg_sig = c_sig
            if s_sig != self._study_sec_sig:
                _, raw_s = _read_regular_bytes(sec_p, 4096)
                hex_s = raw_s.decode('utf-8').strip()
                self._study_sec_bytes = bytes.fromhex(hex_s) if len(hex_s) >= 32 else None
                self._study_sec_sig = s_sig
            if self._study_cfg_url and self._study_sec_bytes:
                return self._study_cfg_url, self._study_sec_bytes
        except Exception:
            return '', None
        return '', None

    def _checked_entries_for_current_page(self, cur_thumb: bytes = b'', manual: bool = False, stage_page_id: str = '') -> list:
        self._ensure_loaded()
        s_pid = str(stage_page_id or '').strip()
        entries = []
        for it in self.items:
            if int(it.get('page_epoch') or 1) != self.page_epoch:
                continue
            it_pid = str(it.get('stage_page_id') or '').strip()
            if s_pid and it_pid and s_pid != it_pid:
                continue
            # On manual '立即巡检', always allow unresolved warn items to be re-verified (catches tiny +/- sign fixes!).
            # On automatic watch ticks, allow re-checking when noticeable ink/erasing occurred inside its box.
            if it.get('status') == 'warn' and not it.get('resolved'):
                if manual:
                    continue
                if (
                    cur_thumb
                    and self.anchor_thumb
                    and isinstance(it.get('box'), dict)
                    and self._box_has_ink_change(self.anchor_thumb, cur_thumb, self._expanded_revision_box(it['box']))
                ):
                    continue
            entries.append({
                'no': str(it.get('no') or '').strip(),
                'prob_key': str(it.get('prob_key') or self._norm_prob_key(it.get('no') or '')),
                'box': it.get('box') if isinstance(it.get('box'), dict) else None,
                'stage_page_id': it_pid,
            })
        return entries[:24]

    def _checked_labels_for_current_page(self, cur_thumb: bytes = b'', manual: bool = False) -> list:
        out = []
        for e in self._checked_entries_for_current_page(cur_thumb, manual=manual):
            if e['no'] and e['no'] not in out:
                out.append(e['no'])
        return out[:18]

    @staticmethod
    def _normalize_gate_box(b_raw, is_xyxy: bool = False) -> dict:
        """Accept [x,y,w,h] in 0..1 floats OR Qwen-VL [xmin,ymin,xmax,ymax] in 0..1000 / 0..100 / 0..1 without clamping >1 to 0.96."""
        if isinstance(b_raw, dict):
            try:
                fx, fy, fw, fh = float(b_raw['x']), float(b_raw['y']), float(b_raw['w']), float(b_raw['h'])
            except (KeyError, TypeError, ValueError):
                return None
            mx = max(fx, fy, fw, fh)
            if mx > 1.5:
                sc = 1000.0 if mx > 100.0 else 100.0
                fx, fy, fw, fh = fx / sc, fy / sc, fw / sc, fh / sc
            return PhoneStudioManager._sanitize_crop_box({'x': fx, 'y': fy, 'w': fw, 'h': fh})
        if isinstance(b_raw, (list, tuple)) and len(b_raw) >= 4:
            try:
                v0, v1, v2, v3 = float(b_raw[0]), float(b_raw[1]), float(b_raw[2]), float(b_raw[3])
            except (TypeError, ValueError):
                return None
            mx = max(v0, v1, v2, v3)
            if mx > 1.5:
                sc = 1000.0 if mx > 100.0 else 100.0
                v0, v1, v2, v3 = v0 / sc, v1 / sc, v2 / sc, v3 / sc
                if v2 > v0 and v3 > v1:
                    return PhoneStudioManager._sanitize_crop_box({'x': v0, 'y': v1, 'w': v2 - v0, 'h': v3 - v1})
            if v2 > v0 and v3 > v1 and (is_xyxy or (v0 + v2 > 1.08 and v1 + v3 > 1.08)):
                return PhoneStudioManager._sanitize_crop_box({'x': v0, 'y': v1, 'w': v2 - v0, 'h': v3 - v1})
            return PhoneStudioManager._sanitize_crop_box({'x': v0, 'y': v1, 'w': v2, 'h': v3})
        return None

    async def _call_gatekeeper(self, gate_jpg: bytes, checked_entries: list, subject: str) -> dict:
        subj_map = {
            'math': '九年级数学（代数方程/函数/圆与相似几何）',
            'phys_chem': '九年级物理与化学（电学/欧姆定律/电功率/化学方程式）',
            'english': '九年级英语（语法填空/完形阅读/词汇句型）',
            'chinese': '九年级语文与文科（古诗文理解性默写/文言实词/病句与阅读选择）',
            'auto': '九年级全科（数学/物理/化学/英语/语文古诗文）',
        }
        subj_desc = subj_map.get(subject, subj_map['auto'])
        already_labels = []
        for ce in (checked_entries or []):
            lbl = ce.get('no') if isinstance(ce, dict) else str(ce or '')
            if lbl and lbl not in already_labels:
                already_labels.append(lbl)
        done_str = '、'.join(already_labels[:18]) if already_labels else '无'
        prompt = (
            '你是初三（九年级）高拍仪「静默伴写守门员」。请观察这张桌面作业图片，只输出一行紧凑严格JSON（不要代码块，不要解释）：\n'
            '格式：{"hb":false,"wi":false,"rot":0,"more":false,"sum":"8字内状态","nc":[{"no":"实际题号","s":"math|phys|chem|eng|chi","box":[左x,顶y,宽w,高h]}]}（注意：box 四项必须填 0.00~1.00 之间的小数比例，严禁大于 1！）\n'
            '严格准则：\n'
            '1. 纸面明显倒置填 "rot":180，横置需顺时针转填 90 或 270，正向填 0。\n'
            '2. 手掌/手指/笔杆正在遮挡作答文字设 "hb":true,"nc":[]。\n'
            '3. 最新动笔题只写到一半未收尾（停在 "="、"+"、"-"、"解："、半截分式/诗句/单词或选择括号留空）设 "wi":true,"nc":[]（后续空白未动笔题忽略）。\n'
            '4. "nc" 提取所有「既有印刷题干、又有已写完手写答案」且不在已核列表的新完成题（无论做对做错都必须提取！每次最多 4 道，超 4 道设 "more":true；多小问标 "15(1)"/"15(2)" 或合并 "第15题"；连排选择题栏/填空表如 1~5 填完可合并标 "选择1-5"；不同大题标 "一、1"/"二、1"）；【纯手写草稿纸或已横线/斜线划掉作废区严禁提取！】\n'
            '5. box:[左x,顶y,宽w,高h] 顶 y 向上包住大题公共题干与配图，高 h 向下包住手写最后一行！\n'
            f'学科偏好：{subj_desc}；已核题号：[{done_str}]'
        )
        compact_gate = await asyncio.to_thread(self._make_gate_jpeg_sync, gate_jpg)
        b64 = base64.b64encode(compact_gate).decode('ascii')
        raw_text = ''
        v_url, secret = self._load_study_bridge_creds()
        if v_url and secret and self.eng.http is not None:
            try:
                env_req = _study_seal(secret, 'req', {
                    'prompt': prompt,
                    'rotate': 0,
                    'max_tokens': 280,
                    'images_b64': [b64],
                })
                async with self.eng.http.post(
                    v_url,
                    json=env_req,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=34.0, sock_connect=6.0),
                ) as resp:
                    if resp.status == 200:
                        env_resp = json.loads((await _read_resp_bounded(resp, 262144)).decode('utf-8'))
                        reply = _study_unseal(secret, 'resp', env_resp)
                        if reply.get('re') != env_req['nonce']:
                            raise PermissionError('gatekeeper nonce mismatch')
                        if reply.get('ok') and str(reply.get('text') or '').strip():
                            raw_text = str(reply['text']).strip()
                            self.gate_engine = 'local_qwen3.5_9b'
                        else:
                            raise RuntimeError(f"bridge_err:{reply.get('error')}")
                    else:
                        raise RuntimeError(f'bridge_http_{resp.status}')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('CoWrite local gatekeeper fallback to fast cloud (%s)', _redact_secrets(exc, v_url))

        if not raw_text:
            msgs = [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': prompt},
                    {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + b64}},
                ],
            }]
            raw_text = await self.eng._post_chat_once('fast', msgs, temperature=0.1, timeout_s=28.0)
            self.gate_engine = 'cloud_fallback'

        self.stats['gate_calls'] = int(self.stats.get('gate_calls') or 0) + 1
        cleaned_txt = re.sub(r'<think>.*?</think>', '', raw_text, flags=re.S)
        objs, _ = extract_json_objects_from_buffer(cleaned_txt)
        obj = next(
            (o for o in objs if isinstance(o, dict) and any(k in o for k in ('nc', 'new_completed', 'hb', 'hand_blocking'))),
            objs[0] if objs else {},
        )
        if not isinstance(obj, dict):
            obj = {}
        cands = []
        raw_nc = obj.get('nc') if isinstance(obj.get('nc'), list) else (
            obj.get('new_completed') if isinstance(obj.get('new_completed'), list) else []
        )
        norm_entries = [
            ce if isinstance(ce, dict) else {'no': str(ce), 'prob_key': self._norm_prob_key(str(ce)), 'box': None}
            for ce in (checked_entries or [])
        ]
        subj_alias = {
            'phys': 'physics', 'phy': 'physics', 'chem': 'chemistry',
            'eng': 'english', 'en': 'english', 'chi': 'chinese', 'zh': 'chinese',
        }
        for idx, it in enumerate(raw_nc[:4]):
            if not isinstance(it, dict):
                continue
            is_b2d = ('box' not in it) and ('box_2d' in it or 'bbox_2d' in it)
            b_raw = it.get('box') if 'box' in it else (it.get('box_2d') if 'box_2d' in it else it.get('bbox_2d'))
            s_box = self._normalize_gate_box(b_raw, is_xyxy=is_b2d)
            if not s_box:
                continue
            no_lbl = str(it.get('no') or f'第{idx + 1}题').strip()[:16]
            pkey = self._norm_prob_key(no_lbl)
            if (
                any(self._matches_checked_entry(pkey, s_box, ce) for ce in norm_entries)
                or any(self._matches_checked_entry(pkey, s_box, prev_c) for prev_c in cands)
            ):
                continue
            raw_s = str(it.get('s') or it.get('subject') or 'math').strip().lower()
            subj_it = subj_alias.get(raw_s, raw_s)
            if subj_it not in ('math', 'physics', 'chemistry', 'english', 'chinese', 'other'):
                subj_it = 'math'
            cands.append({
                'no': no_lbl,
                'prob_key': pkey or no_lbl,
                'subject': subj_it,
                'box': s_box,
                'stem_brief': str(it.get('stem_brief') or '').strip()[:32],
            })
        need_rot = 0
        with contextlib.suppress(TypeError, ValueError):
            rv = int(obj.get('rot') if 'rot' in obj else (obj.get('need_rotate') or 0))
            if rv in (90, 180, 270):
                need_rot = rv
        return {
            'hand_blocking': bool(obj.get('hb') if 'hb' in obj else obj.get('hand_blocking')),
            'writing_incomplete': bool(obj.get('wi') if 'wi' in obj else obj.get('writing_incomplete')),
            'need_rotate': need_rot,
            'has_more': bool(obj.get('more') if 'more' in obj else obj.get('has_more')) or len(raw_nc) > 4,
            'page_summary': str(obj.get('sum') if 'sum' in obj else (obj.get('page_summary') or '')).strip()[:26],
            'new_completed': cands,
        }

    _STRONG_MATH_CW_RE = re.compile(
        r'分式方程|一元二次方程|二次函数|反比例函数|一次函数|不等式组|因式分解|判别式|韦达定理|根与系数|'
        r'圆周角|圆心角|切线|相似三角形|全等三角形|勾股定理|锐角三角函数|抛物线|对称轴|增根|去分母|'
        r'(?:sin|cos|tan)\s*\d|[xXyY]\s*[²³^]|Δ\s*[=><≥≤]'
    )
    _STRONG_CHI_CW_RE = re.compile(r'古诗|古文|文言|默写|补写出|名句|修辞|病句|阅读理解|加点字|加点词')

    @classmethod
    def _infer_item_subject(cls, cand: dict, raw_obj_subj: str, ocr_stem: str, topic: str, existing: dict = None) -> str:
        subj_alias_up = {'phys': 'physics', 'phy': 'physics', 'chem': 'chemistry', 'eng': 'english', 'en': 'english', 'chi': 'chinese', 'zh': 'chinese'}
        raw_s = subj_alias_up.get(str(raw_obj_subj or '').strip().lower(), str(raw_obj_subj or '').strip().lower())
        combo = f"{ocr_stem} {topic}".strip()
        det_s = detect_subject(combo)
        if cls._STRONG_MATH_CW_RE.search(combo) and not cls._STRONG_CHI_CW_RE.search(combo) and det_s not in ('physics', 'chemistry'):
            return 'math'
        if det_s in ('chemistry', 'physics', 'english', 'chinese'):
            return det_s
        if raw_s in ('math', 'physics', 'chemistry', 'english', 'chinese'):
            return raw_s
        if existing is not None and existing.get('subject') in ('math', 'physics', 'chemistry', 'english', 'chinese'):
            return str(existing['subject'])
        cand_s = str(cand.get('subject') or 'math').strip()
        return cand_s if cand_s in ('math', 'physics', 'chemistry', 'english', 'chinese') else 'math'

    def _upsert_reviewed_candidate(self, cand: dict, obj: dict, crop_jpg: bytes) -> dict:
        verdict = str(obj.get('verdict') or '').strip().lower()
        conf = 0.0
        with contextlib.suppress(TypeError, ValueError):
            conf = float(obj.get('confidence') if obj.get('confidence') is not None else 0.0)
        if verdict not in ('ok', 'warn') or conf < self.CONFIDENCE_FLOOR:
            self.stats['unclear_skipped'] = int(self.stats.get('unclear_skipped') or 0) + 1
            return None

        no_final = str(obj.get('no') or cand['no']).strip()[:16] or cand['no']
        pkey = self._norm_prob_key(no_final) or cand['prob_key']
        topic = _cowrite_latex_to_unicode(str(obj.get('topic') or cand.get('stem_brief') or '步骤核查').strip())[:22]
        ocr_stem = _cowrite_latex_to_unicode(sanitize_ocr_question(str(obj.get('ocr_stem') or cand.get('stem_brief') or '').strip()))[:180]
        raw_cp = str(obj.get('checkpoint') or '').strip()
        raw_dh = str(obj.get('deep_hint') or '').strip()
        raw_sw = str(obj.get('step_where') or '').strip()
        raw_ft = str(obj.get('formula_tip') or '').strip()
        if verdict == 'warn':
            checkpoint = _scrub_cowrite_spoilers(raw_cp, max_len=58)
            deep_hint = _scrub_cowrite_spoilers(raw_dh, max_len=80) if raw_dh else ''
            if deep_hint == checkpoint:
                deep_hint = ''
            formula_tip = _scrub_cowrite_formula_tip(raw_ft, max_len=66) if raw_ft else ''
            if formula_tip in (checkpoint, deep_hint):
                formula_tip = ''
            step_where = _scrub_cowrite_spoilers(raw_sw, max_len=18) if raw_sw else ''
        else:
            checkpoint = (_cowrite_latex_to_unicode(re.sub(r'\s+', ' ', raw_cp))[:36] or '步骤严谨，推导与结果正确 ✓')
            deep_hint = ''
            formula_tip = ''
            step_where = ''

        snap_name = self._save_cowrite_snap(crop_jpg)
        now_f = round(time.time(), 6)
        now_ts = int(now_f)
        stage_pid = str(cand.get('stage_page_id') or '').strip()[:28]

        def _is_cross_photo_warn_revision(x: dict) -> bool:
            if not stage_pid or x.get('status') != 'warn' or x.get('resolved'):
                return False
            x_pid = str(x.get('stage_page_id') or '').strip()
            if not x_pid or x_pid == stage_pid:
                return False
            if str(x.get('prob_key') or '') != pkey or str(x.get('subject') or 'math') != str(cand.get('subject') or 'math'):
                return False
            if not (isinstance(cand.get('box'), dict) and isinstance(x.get('box'), dict) and self._box_iou(cand['box'], x['box']) >= 0.44):
                return False
            s1, s2 = str(x.get('ocr_stem') or '').strip(), str(ocr_stem or '').strip()
            if s1 and s2:
                c1, c2 = set(re.sub(r'\s+', '', s1)), set(re.sub(r'\s+', '', s2))
                if len(c1 & c2) / float(max(1, min(len(c1), len(c2)))) < 0.35:
                    return False
            else:
                t1, t2 = str(x.get('topic') or '').strip(), str(topic or '').strip()
                if t1 and t2 and not (t1 in t2 or t2 in t1 or len(set(t1) & set(t2)) >= 2):
                    return False
            return True

        existing = next(
            (
                x for x in self.items
                if (cand.get('_force_id') and x.get('id') == cand['_force_id'])
                or (
                    int(x.get('page_epoch') or 1) == self.page_epoch
                    and self._matches_checked_entry(pkey, cand.get('box'), x, stage_page_id=stage_pid)
                )
                or _is_cross_photo_warn_revision(x)
            ),
            None,
        )
        prev_stage_pid = str(existing.get('stage_page_id') or '').strip() if existing is not None else ''
        raw_obj_subj = str(obj.get('subject') or obj.get('s') or '').strip().lower()
        eff_subj = self._infer_item_subject(cand, raw_obj_subj, ocr_stem, topic, existing=existing)
        if existing is not None:
            was_warn = (existing.get('status') == 'warn' or existing.get('corrected_pass'))
            prev_is_warn = (existing.get('status') == 'warn')
            if verdict == 'warn':
                w_cp, w_dh, w_sw, w_ft = checkpoint, deep_hint, step_where, formula_tip
            else:
                w_cp = (existing.get('checkpoint') if prev_is_warn else '') or existing.get('warn_checkpoint') or ''
                w_dh = (existing.get('deep_hint') if prev_is_warn else '') or existing.get('warn_deep_hint') or ''
                w_sw = (existing.get('step_where') if prev_is_warn else '') or existing.get('warn_step_where') or ''
                w_ft = (existing.get('formula_tip') if prev_is_warn else '') or existing.get('warn_formula_tip') or ''
            old_snap = existing.get('snap_name') or ''
            if old_snap and old_snap != snap_name and not existing.get('enrolled_mistake'):
                with contextlib.suppress(OSError):
                    (SNAP_DIR / old_snap).unlink(missing_ok=True)
            existing.update({
                'no': no_final,
                'prob_key': pkey,
                'status': verdict,
                'subject': eff_subj,
                'topic': topic,
                'step_where': step_where,
                'checkpoint': checkpoint,
                'deep_hint': deep_hint,
                'formula_tip': formula_tip,
                'warn_step_where': w_sw,
                'warn_checkpoint': w_cp,
                'warn_deep_hint': w_dh,
                'warn_formula_tip': w_ft,
                'ocr_stem': ocr_stem or existing.get('ocr_stem') or '',
                'snap_name': snap_name,
                'box': cand.get('box') or existing.get('box'),
                'ts': now_ts,
                'ts_ms': now_f,
                'resolved': False,
                'blackboard_solved': False,
                'variant_passed': False if verdict == 'warn' else bool(existing.get('variant_passed')),
                'ever_graduated': False if verdict == 'warn' else bool(existing.get('ever_graduated')),
                'corrected_pass': bool(was_warn and verdict == 'ok'),
                'starred': bool(existing.get('starred')),
                'stage_page_id': stage_pid or existing.get('stage_page_id') or '',
                'orig_stage_page_id': str(existing.get('orig_stage_page_id') or prev_stage_pid or stage_pid)[:28],
                'page_epoch': int(existing.get('page_epoch') or self.page_epoch),
            })
            if existing.get('enrolled_mistake') and existing.get('mistake_id') and verdict == 'warn':
                if hasattr(self.eng, '_ensure_mistakes_loaded'):
                    self.eng._ensure_mistakes_loaded()
                mb_hit = next((m for m in (getattr(self.eng, 'mistakes', None) or ()) if isinstance(m, dict) and m.get('id') == existing['mistake_id']), None)
                if mb_hit is not None:
                    mb_hit['subject'] = eff_subj
                    mb_hit['weak_focus'] = f"{topic}：{checkpoint}"[:64]
                    if mb_hit.get('graduated'):
                        mb_hit['graduated'] = False
                        mb_hit['stage'] = 0
                        mb_hit['next_review_at'] = now_ts + 86400
                    if snap_name:
                        if old_snap and old_snap != snap_name:
                            with contextlib.suppress(OSError):
                                (SNAP_DIR / old_snap).unlink(missing_ok=True)
                        mb_hit['image_url'] = f'/api/tutor/snap/{snap_name}'
                    mb_hit['updated_at'] = now_ts
                    self.eng._save_mistakes()
            self.items = [existing] + [x for x in self.items if x is not existing]
            rec = existing
        else:
            rec = {
                'id': 'cw_' + uuid.uuid4().hex[:10],
                'no': no_final,
                'prob_key': pkey,
                'page_epoch': self.page_epoch,
                'status': verdict,
                'subject': eff_subj,
                'topic': topic,
                'step_where': step_where,
                'checkpoint': checkpoint,
                'deep_hint': deep_hint,
                'formula_tip': formula_tip,
                'warn_step_where': step_where if verdict == 'warn' else '',
                'warn_checkpoint': checkpoint if verdict == 'warn' else '',
                'warn_deep_hint': deep_hint if verdict == 'warn' else '',
                'warn_formula_tip': formula_tip if verdict == 'warn' else '',
                'ocr_stem': ocr_stem,
                'snap_name': snap_name,
                'box': cand.get('box'),
                'ts': now_ts,
                'ts_ms': now_f,
                'resolved': False,
                'corrected_pass': False,
                'starred': False,
                'enrolled_mistake': False,
                'mistake_id': '',
                'ever_graduated': False,
                'blackboard_solved': False,
                'variant_passed': False,
                'stage_page_id': stage_pid,
                'orig_stage_page_id': stage_pid,
            }
            self.items.insert(0, rec)
        self.seq += 1
        self._save_state()
        if prev_stage_pid and prev_stage_pid != rec.get('stage_page_id'):
            self._sync_page_check_marks(prev_stage_pid, clear_if_empty=True)
        if rec.get('stage_page_id'):
            self._sync_page_check_marks(rec['stage_page_id'])
        return rec

    async def _fetch_review_eval(self, hi_jpg: bytes, cand: dict, subject_pref: str, pencil_boost: bool = False) -> tuple:
        extra_top = 0.14 if re.search(r'\([2-9]\)|[②③④⑤⑥]', str(cand.get('prob_key') or cand.get('no') or '')) else (0.03 if cand.get('_force_id') else 0.0)
        extra_bot = 0.09 if cand.get('_force_id') else 0.0
        crop_jpg = await asyncio.to_thread(self._crop_problem_jpeg_sync, hi_jpg, cand.get('box'), pencil_boost, extra_top, extra_bot)
        b64 = base64.b64encode(crop_jpg).decode('ascii')
        subj_hint = {
            'math': '初三数学',
            'phys_chem': '初三物理/化学',
            'english': '初三英语',
            'chinese': '初三语文（古诗文默写/文言文/阅读）',
        }.get(subject_pref, '初三全科')
        target_desc = (
            '框选区域内的主作答题目（请自动识别实际印刷题号填入 "no"）'
            if cand.get('_crop_direct') else
            f'目标题号：{cand["no"]}'
        )
        prompt = (
            f'你是经验丰富的{subj_hint}把关名师，正在进行「零打扰静默伴写复核」。请仔细核对这张裁剪图中的【印刷原题（含大题公共题干/配图/选项）】与【学生手写解答过程及结论】（{target_desc}；裁剪图上下若带入相邻其他题号或空白未做题，或学生用横线/斜线明确划掉的废草稿，请直接忽略，仅核对保留的最终作答）：\n'
            '1. 先在 check_calc 中逐步独立严谨推算正确解与限制条件（重点排查初三高频失分点：一元二次方程 Δ 判别式/漏负根/含参二次项系数≠0、分式方程去分母漏乘常数项及检验分母≠0增根、二次函数对称轴/开口与自变量区间最值、圆的切线/相似比对应关系与多解分类讨论、物理欧姆定律与电功率串并联状态对应及单位换算、化学方程式配平/反应条件/↑↓符号、英语时态语态主谓一致/非谓语词形/完形阅读上下文逻辑、语文古诗文理解性默写同音形近错别字/通假字/文言实词释义）。若框内为连排客观题组（如选择题 1~5 或多空填空），请逐小题/逐空独立核算！\n'
            '2. 【宁可漏报，绝不冤枉】：若整张图仅为无印刷题干的纯草稿演算、原题关键条件被裁切缺失、字迹模糊难以辨认、或学生明显只写到一半未完成，必须返回 "verdict":"unclear"！只有对原题与手写步骤均有 ≥85% 把握（confidence ≥ 0.85）时，才返回 "ok"（全部步骤与结论正确）或 "warn"（存在明确计算失误、漏解、未验增根、符号/单位/配平错误、古诗文错别字或选错）。\n'
            '3. 【渐进两级防剧透点拨】：当 verdict="warn" 时，提供两级苏格拉底自查提示——\n'
            '   - step_where（10字内）：精确定位需自查的步骤、哪半句，若为连号客观题组则指明具体哪一空（如 "第3小题" / "第2空"）；\n'
            '   - checkpoint（第1级方向提醒，32字内）：温和点出需回查哪一步、哪半句或哪个隐含条件；\n'
            '   - deep_hint（第2级追问拆解，64字内）：进一步用针对该步符号/系数/定义域、或古诗文该字词义与偏旁部首的具体反问帮孩子定位问题；\n'
            '   - formula_tip（第3级通用底座，48字内）：仅写出对应课本通用字母公式、几何定理条件或英语语法规则（如 Δ=b²-4ac、切线垂直于过切点半径、since+过去时间点用have/has done，严禁代入本题数字或泄底；古诗文仅提示篇目出处与句意修辞，严禁出现目标汉字）；\n'
            '   【铁律】以上所有提示均严禁泄露正确得数、正确选项字母（A/B/C/D）、正确方程式或古诗文正确汉字本身！另附 ocr_stem 记录识别到的纯净原题干（120字内，不含答案）。\n'
            '只输出一行严格JSON（不要代码块）：'
            '{"check_calc":"内部严谨演算","ocr_stem":"纯净原题干文字(120字内)","verdict":"ok|warn|unclear","confidence":0.95,"subject":"math|physics|chemistry|english|chinese","no":"第3题","topic":"考点简称(10字内)","step_where":"需自查位置(10字内，全对留空)","checkpoint":"第1级方向提示(32字内，全对写简短肯定)","deep_hint":"第2级具体启发反问(64字内，全对留空)","formula_tip":"对应通用公式/定理(48字内，全对留空)"}'
        )
        msgs = [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + b64}},
            ],
        }]
        raw_txt = await self.eng._post_chat_once('deep', msgs, temperature=0.1, timeout_s=36.0)
        self.stats['review_calls'] = int(self.stats.get('review_calls') or 0) + 1
        objs, _ = extract_json_objects_from_buffer(raw_txt)
        obj = next((o for o in objs if isinstance(o, dict) and 'verdict' in o), objs[0] if objs else {})
        if not isinstance(obj, dict):
            obj = {}
        return obj, crop_jpg

    async def _review_single_item(self, hi_jpg: bytes, cand: dict, subject_pref: str, pencil_boost: bool = False) -> dict:
        obj, crop_jpg = await self._fetch_review_eval(hi_jpg, cand, subject_pref, pencil_boost=pencil_boost)
        return self._upsert_reviewed_candidate(cand, obj, crop_jpg)

    async def _run_funnel_once(
        self, gate_jpg: bytes, hi_jpg_getter, cur_thumb: bytes = b'', manual: bool = False, pencil_boost: bool = None, stage_page_id: str = '',
    ) -> int:
        eff_pencil = self.pencil_boost if pencil_boost is None else bool(pencil_boost)
        async with self._pipeline_lock:
            self._ensure_loaded()
            if stage_page_id:
                self._maybe_advance_epoch_for_stage_page(stage_page_id)
            else:
                self._last_stage_page_id = ''
            run_epoch = self.page_epoch
            run_clear_gen = self._clear_gen
            q_m = await asyncio.to_thread(self._frame_quality_metrics_sync, gate_jpg)
            if q_m['mean_luma'] < 26.0:
                self.stats['dark_skipped'] = int(self.stats.get('dark_skipped') or 0) + 1
                self.lighting_warn = 'dark'
                self.last_note = '💡 环境光线偏暗或镜头受遮挡，建议打开台灯或点提示条开启高拍仪补光'
                if self.enabled:
                    self.phase = 'watching'
                self.seq += 1
                return 0
            if (
                (q_m['mean_luma'] > 249.0 and q_m['contrast_span'] < 14)
                or (
                    not manual
                    and q_m.get('glare_ratio', 0.0) >= 0.34
                    and q_m['mean_luma'] >= 228.0
                    and q_m['edge_mean'] < 3.6
                )
            ):
                self.lighting_warn = 'glare'
                self.last_note = '☀️ 纸面局部反光过强，请微调台灯角度避免眩光遮挡字迹'
                if self.enabled:
                    self.phase = 'watching'
                self.seq += 1
                return 0
            self.lighting_warn = ''
            if not manual and q_m['edge_mean'] < 2.1:
                self.stats['blur_skipped'] = int(self.stats.get('blur_skipped') or 0) + 1
                self.last_note = '🤚 抓拍瞬间纸面微晃或对焦未稳，待纸面停稳后自动重核'
                if self.enabled:
                    self.phase = 'watching'
                self.seq += 1
                return 0
            if self.enabled:
                self.phase = 'gatekeeping'
            auto_rev_hits = []
            if not manual and cur_thumb and self.anchor_thumb:
                s_pid_cur = str(stage_page_id or '').strip()
                for it_ar in self.items:
                    if len(auto_rev_hits) >= 4:
                        break
                    if it_ar.get('status') != 'warn' or it_ar.get('resolved') or int(it_ar.get('page_epoch') or 1) != self.page_epoch:
                        continue
                    it_pid_ar = str(it_ar.get('stage_page_id') or '').strip()
                    if s_pid_cur and it_pid_ar and s_pid_cur != it_pid_ar:
                        continue
                    bx_ar = it_ar.get('box')
                    if isinstance(bx_ar, dict) and self._box_has_ink_change(self.anchor_thumb, cur_thumb, self._expanded_revision_box(bx_ar)):
                        auto_rev_hits.append(it_ar)
            auto_rev_cands = [
                {
                    '_force_id': h['id'],
                    'no': h['no'],
                    'prob_key': h.get('prob_key') or self._norm_prob_key(h['no']),
                    'subject': h.get('subject') or 'math',
                    'box': h['box'],
                    'stem_brief': h.get('topic') or '',
                    'stage_page_id': stage_page_id or str(h.get('stage_page_id') or '').strip(),
                }
                for h in auto_rev_hits
            ]
            pure_auto_rev = False
            if (
                auto_rev_cands
                and not self._has_more_pending
                and not self._has_ink_outside_boxes(
                    self.anchor_thumb,
                    cur_thumb,
                    [self._expanded_revision_box(h['box']) for h in auto_rev_hits],
                )
            ):
                pure_auto_rev = True
                self.last_gate_mono = time.monotonic()
                self.last_check_ts = int(time.time())
                self._err_streak = 0
                self._backoff_until_mono = 0.0
                gate_res = {
                    'hand_blocking': False,
                    'writing_incomplete': False,
                    'need_rotate': 0,
                    'has_more': False,
                    'page_summary': '',
                    'new_completed': auto_rev_cands,
                }
            else:
                checked_entries = self._checked_entries_for_current_page(cur_thumb, manual=manual, stage_page_id=stage_page_id)
                for h in auto_rev_hits:
                    hk = str(h.get('prob_key') or self._norm_prob_key(h.get('no') or ''))
                    if not any(ce.get('prob_key') == hk and ce.get('box') == h.get('box') for ce in checked_entries):
                        checked_entries.append({
                            'no': str(h.get('no') or '').strip(),
                            'prob_key': hk,
                            'box': h.get('box'),
                            'stage_page_id': str(h.get('stage_page_id') or '').strip(),
                        })
                gate_res = await self._call_gatekeeper(gate_jpg, checked_entries, self.subject)
            if self._clear_gen != run_clear_gen or self.page_epoch != run_epoch:
                if self.enabled:
                    self.phase = 'watching'
                self.seq += 1
                return 0
            if not pure_auto_rev:
                self.last_gate_mono = time.monotonic()
                self.last_check_ts = int(time.time())
                self._err_streak = 0
                self._backoff_until_mono = 0.0
                if gate_res.get('need_rotate') in (90, 180, 270):
                    self.rotate_hint = int(gate_res['need_rotate'])
                elif gate_res.get('new_completed'):
                    self.rotate_hint = 0

                if gate_res['hand_blocking'] and not manual:
                    self.last_note = '检测到手部遮挡题目，等手移开停稳后自动复核'
                    if self.enabled:
                        self.phase = 'watching'
                    self.seq += 1
                    return 0

                cands = list(gate_res['new_completed'])
                if auto_rev_cands:
                    cands = (
                        auto_rev_cands
                        + [
                            c for c in cands
                            if not any(self._matches_checked_entry(c['prob_key'], c.get('box'), ac) for ac in auto_rev_cands)
                        ]
                    )[:4]
            else:
                cands = auto_rev_cands
            if not cands:
                self._has_more_pending = False
                if cur_thumb:
                    self.saw_activity = False
                    # Do NOT overwrite anchor_thumb while writing_incomplete or rotated: preserves full problem ink diff once student finishes!
                    if not gate_res['writing_incomplete'] and self.rotate_hint not in (90, 180, 270):
                        self.anchor_thumb = cur_thumb
                if self.rotate_hint in (90, 180, 270):
                    self.last_note = f'🔄 检测到作业纸方向可能偏转 {self.rotate_hint}°，可点提示条一键旋转摆正'
                elif gate_res['writing_incomplete']:
                    self.last_note = '检测到题目仍在书写推导中，写完停笔后再静默核查'
                else:
                    self.last_note = gate_res['page_summary'] or '本页已完成题均已核对，继续书写下一题即可'
                if self.enabled:
                    self.phase = 'watching'
                self.seq += 1
                return 0

            if self.enabled:
                self.phase = 'reviewing'
            self.last_note = f"发现 {len(cands)} 道新写完题目，名师模型正在并行静默验算…"
            self.seq += 1

            hi_jpg = await hi_jpg_getter()
            if len(cands) == 1:
                eval_list = [await self._fetch_review_eval(hi_jpg, cands[0], self.subject, pencil_boost=eff_pencil)]
            else:
                sem = asyncio.Semaphore(2)

                async def _bounded_eval(c_item):
                    async with sem:
                        return await self._fetch_review_eval(hi_jpg, c_item, self.subject, pencil_boost=eff_pencil)

                eval_list = await asyncio.gather(*[_bounded_eval(c) for c in cands], return_exceptions=True)
                for ev_r in eval_list:
                    if isinstance(ev_r, asyncio.CancelledError):
                        raise ev_r

            if self._clear_gen != run_clear_gen or self.page_epoch != run_epoch:
                if self.enabled:
                    self.phase = 'watching'
                self.seq += 1
                return 0

            updated_cnt = 0
            partial_err = None
            for cand, ev_item in zip(cands, eval_list):
                if isinstance(ev_item, BaseException):
                    partial_err = ev_item
                    log.warning('CoWrite single-item review failed, keeping healthy siblings: %s', _redact_secrets(ev_item))
                    continue
                obj, crop_jpg = ev_item
                if stage_page_id:
                    cand['stage_page_id'] = stage_page_id
                rec = self._upsert_reviewed_candidate(cand, obj, crop_jpg)
                if rec is not None:
                    updated_cnt += 1
            if updated_cnt == 0 and partial_err is not None:
                raise partial_err

            has_more = bool(gate_res.get('has_more') or (gate_res.get('writing_incomplete') and not pure_auto_rev)) or (partial_err is not None)
            self._has_more_pending = has_more
            if cur_thumb and not has_more:
                self.anchor_thumb = cur_thumb
                self.saw_activity = False
            elif has_more:
                self.saw_activity = True
            if self.enabled:
                self.phase = 'watching'
            if updated_cnt > 0:
                self._poke_display_wake(force=True)
                warn_n = sum(1 for x in self.items if x.get('status') == 'warn' and not x.get('resolved'))
                more_sfx = '（本页剩余已完成题将在下一拍继续自动核查）' if has_more else ''
                if pure_auto_rev:
                    self.last_note = (
                        '🎉 检测到纸面订正，已静默自动复查通过 ✓'
                        if warn_n == 0 else f'🔄 已自动复查纸面修改（当前仍有 {warn_n} 处建议自查）'
                    )
                else:
                    self.last_note = (
                        f'已静默核对新完成题目（当前有 {warn_n} 处易错点建议自查）{more_sfx}'
                        if warn_n > 0 else f'已静默核对新完成题目，步骤与结论均正确 ✓{more_sfx}'
                    )
            else:
                self.last_note = '字迹或题干暂不够清晰完整，为免误报已静默跳过'
            self.seq += 1
            return updated_cnt

    async def _watch_loop(self):
        try:
            while self.enabled:
                await asyncio.sleep(self.POLL_INTERVAL_SEC)
                if not self.enabled:
                    break
                now_m = time.monotonic()
                if self.started_mono > 0 and (now_m - self.started_mono) >= self.MAX_SESSION_SEC:
                    await self.stop(reason='静默伴写已满 2.5 小时，已自动休眠释放高拍仪')
                    return
                if self.last_motion_mono > 0 and (now_m - self.last_motion_mono) >= self.MAX_IDLE_DESK_SEC:
                    await self.stop(reason='桌面连续 25 分钟无书写动静，伴写已自动休眠省电')
                    return
                if self.eng.is_lesson_in_progress():
                    # Pause USB camera polling & gatekeeper while student is actively solving an interactive blackboard lesson
                    self.last_motion_mono = now_m
                    if self.phase != 'watching':
                        self.phase = 'watching'
                        self.seq += 1
                    continue
                if self._backoff_until_mono > now_m:
                    continue
                cam_info = self.eng.camera.discover()
                if not cam_info.get('connected'):
                    changed_nc = False
                    if self.phase != 'watching':
                        self.phase = 'watching'
                        changed_nc = True
                    idle_nc_note = '等待接入 USB 高拍仪（也可点「立即巡检」核查手机已传作业）'
                    if not self.items and not self.last_check_ts and self.last_note != idle_nc_note:
                        self.last_note = idle_nc_note
                        changed_nc = True
                    if changed_nc:
                        self.seq += 1
                    continue
                if self._pipeline_lock.locked():
                    continue
                try:
                    if hasattr(self.eng.camera, 'capture_gray_thumb'):
                        thumb = await self.eng.camera.capture_gray_thumb(self.THUMB_W, self.THUMB_H)
                    else:
                        f_tmp = await self.eng.camera.capture_jpeg(high_res=False, crop_box=None, pencil_boost=self.pencil_boost)
                        thumb = await asyncio.to_thread(self._make_gray_thumb_sync, f_tmp)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.debug('CoWrite frame poll skipped: %s', _redact_secrets(exc))
                    continue
                if not thumb:
                    continue

                now_m = time.monotonic()
                if not self.prev_thumb:
                    self.prev_thumb = thumb
                    self.last_motion_mono = now_m
                    self.saw_activity = True
                    self._still_streak = 0
                    self.phase = 'watching'
                    continue

                step_diff = self._thumb_diff_metrics(self.prev_thumb, thumb)
                self.prev_thumb = thumb
                has_micro_ink = (step_diff.get('ink_pixels', 0) >= 3 and step_diff.get('max_px_diff', 0) >= 26)
                is_moving = (
                    step_diff['mean_diff'] >= 2.0
                    or step_diff['changed_cells'] >= 2
                    or step_diff['max_cell_diff'] >= 9.0
                    or has_micro_ink
                )
                if is_moving:
                    self.last_motion_mono = now_m
                    self.saw_activity = True
                    self._still_streak = 0
                    self._poke_display_wake(force=False)
                    if self.phase != 'writing':
                        self.phase = 'writing'
                        self.last_note = '正在动笔书写中，静默不打扰…'
                        self.seq += 1
                    continue

                if step_diff['changed_cells'] == 0 and step_diff['max_cell_diff'] < 4.5 and not has_micro_ink:
                    self._still_streak += 1
                else:
                    self._still_streak = 0

                if self.phase == 'writing' and (now_m - self.last_motion_mono) >= 1.4:
                    self.phase = 'watching'
                    self.seq += 1

                if not self._has_more_pending:
                    if not self.saw_activity or self._still_streak < 2:
                        continue
                    eff_settle = self.PAUSE_SETTLE_SEC if self.PAUSE_SETTLE_SEC < 1.0 else self.PAUSE_MODE_MAP.get(self.pause_mode, 3.8)
                    if (now_m - self.last_motion_mono) < eff_settle:
                        continue
                if self.last_gate_mono > 0 and (now_m - self.last_gate_mono) < self.MIN_GATE_INTERVAL_SEC:
                    continue

                if self.anchor_thumb and not self._has_more_pending:
                    anc_diff = self._thumb_diff_metrics(self.anchor_thumb, thumb)
                    # Major page flip (>52% of 80 grid cells changed): advance page_epoch automatically!
                    if anc_diff['changed_cells'] >= 42:
                        self.page_epoch += 1
                        self.anchor_thumb = b''
                    elif (anc_diff['changed_cells'] < 1 or anc_diff['max_cell_diff'] < 5.2) and not (
                        anc_diff.get('ink_pixels', 0) >= 3 and anc_diff.get('max_px_diff', 0) >= 26
                    ):
                        self.saw_activity = False
                        continue

                async def _get_hi():
                    return await self.eng.camera.capture_jpeg(high_res=True, crop_box=None, pencil_boost=self.pencil_boost)

                try:
                    gate_jpg = await self.eng.camera.capture_jpeg(high_res=False, crop_box=None, pencil_boost=self.pencil_boost)
                    await self._run_funnel_once(gate_jpg, _get_hi, cur_thumb=thumb, manual=False, pencil_boost=self.pencil_boost)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._err_streak = min(5, self._err_streak + 1)
                    self._backoff_until_mono = time.monotonic() + min(40.0, 8.0 * (2 ** (self._err_streak - 1)))
                    self.phase = 'watching' if self.enabled else 'off'
                    self.last_note = f'刚才巡检网络波动，稍后停笔将自动重试（{_redact_secrets(exc)[:28]}）'
                    self.seq += 1
        except asyncio.CancelledError:
            pass

    async def start(self, subject: str = None, pause_mode: str = None) -> dict:
        self._ensure_loaded()
        if subject is not None:
            self.subject = self._norm_subject(subject)
        if pause_mode is not None:
            self.pause_mode = self._norm_pause_mode(pause_mode)
        now_m = time.monotonic()
        self.enabled = True
        self.phase = 'watching'
        self.started_at = int(time.time())
        self.started_mono = now_m
        self.last_motion_mono = now_m
        self.prev_thumb = b''
        self.anchor_thumb = b''
        self.saw_activity = True
        self._still_streak = 0
        self._err_streak = 0
        self._backoff_until_mono = 0.0
        self.eng.camera._prefer_low_fps = True
        if getattr(self.eng.camera, '_frozen_raw_jpg', None) is not None:
            with contextlib.suppress(Exception):
                await self.eng.camera.set_freeze(False)
        self._poke_display_wake(force=True)
        cam_ok = bool(self.eng.camera.discover().get('connected'))
        self.last_note = (
            '👀 静默伴写已开启：动笔期间绝不打扰，写完停笔约 4 秒自动静默核对'
            if cam_ok else '静默伴写待命：接入 USB 高拍仪自动伴写，或点下方「立即巡检」核查已传作业页'
        )
        self.seq += 1
        self._save_state()
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = self.eng._spawn(self._watch_loop(), session_work=False)
        return self.public_state()

    async def stop(self, reason: str = '') -> dict:
        self._ensure_loaded()
        was_on = self.enabled
        self.enabled = False
        self.phase = 'off'
        self.prev_thumb = b''
        self.anchor_thumb = b''
        self.saw_activity = False
        self._still_streak = 0
        self.eng.camera._prefer_low_fps = False
        t = self._watch_task
        self._watch_task = None
        cur_t = None
        with contextlib.suppress(RuntimeError):
            cur_t = asyncio.current_task()
        if t is not None and not t.done() and t is not cur_t:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        arch_before = (len(self.recent_sessions), self._last_archive_key, dict(self.recent_sessions[0]) if self.recent_sessions else None)
        self._archive_current_session_if_meaningful()
        arch_changed = (len(self.recent_sessions), self._last_archive_key, dict(self.recent_sessions[0]) if self.recent_sessions else None) != arch_before
        if was_on:
            with contextlib.suppress(Exception):
                await self.eng.camera.stop_stream()
            self.last_note = reason or '静默伴写已暂停，高拍仪实时流已释放'
            self.seq += 1
        if was_on or arch_changed:
            self._save_state()
        return self.public_state()

    async def toggle(self, enabled=None, subject: str = None, pencil_boost=None, pause_mode: str = None) -> dict:
        self._ensure_loaded()
        cfg_changed = False
        if isinstance(pencil_boost, bool) and self.pencil_boost != pencil_boost:
            self.pencil_boost = pencil_boost
            cfg_changed = True
        if pause_mode is not None:
            npm = self._norm_pause_mode(pause_mode)
            if self.pause_mode != npm:
                self.pause_mode = npm
                cfg_changed = True
        if subject is not None:
            nsubj = self._norm_subject(subject)
            if self.subject != nsubj:
                self.subject = nsubj
                cfg_changed = True
        if enabled is None and (subject is not None or isinstance(pencil_boost, bool) or pause_mode is not None):
            self.seq += 1
            self._save_state()
            return self.public_state()
        want = (not self.enabled) if enabled is None else bool(enabled)
        res = await (self.start(subject=subject, pause_mode=pause_mode) if want else self.stop())
        if cfg_changed and not want:
            self._save_state()
        return res

    async def check_now(self, subject: str = None, image_bytes: bytes = None, pencil_boost: bool = None, stage_page_id: str = '') -> dict:
        self._ensure_loaded()
        if self.enabled:
            self.last_motion_mono = time.monotonic()
        changed_cfg = False
        if subject is not None:
            self.subject = self._norm_subject(subject)
            changed_cfg = True
        if isinstance(pencil_boost, bool) and self.pencil_boost != pencil_boost:
            self.pencil_boost = pencil_boost
            changed_cfg = True
        if changed_cfg:
            self._save_state()
        eff_p = self.pencil_boost if pencil_boost is None else bool(pencil_boost)
        if image_bytes is not None:
            norm_jpg = await self.eng.phone_studio.normalize_to_jpeg(image_bytes)
            thumb = await asyncio.to_thread(self._make_gray_thumb_sync, norm_jpg)
            cnt = await self._run_funnel_once(
                norm_jpg, lambda: asyncio.sleep(0, result=norm_jpg), cur_thumb=thumb, manual=True, pencil_boost=eff_p,
            )
            return {'ok': True, 'checked_new': cnt, 'note': self.last_note, 'cowrite': self.public_state()}

        req_pid_cw = str(stage_page_id or '').strip()
        if req_pid_cw:
            if not self.eng.phone_studio.select_page(req_pid_cw):
                raise LookupError('未找到指定的暂存作业页，请先上传作业照片')
            pg_jpg = await self.eng.phone_studio.get_rotated_stage_jpeg(req_pid_cw)
            thumb = await asyncio.to_thread(self._make_gray_thumb_sync, pg_jpg)
            cnt = await self._run_funnel_once(
                pg_jpg, lambda: asyncio.sleep(0, result=pg_jpg), cur_thumb=thumb, manual=True, pencil_boost=eff_p, stage_page_id=req_pid_cw,
            )
            return {'ok': True, 'source': 'phone_stage', 'checked_new': cnt, 'note': self.last_note, 'cowrite': self.public_state()}

        cam_ok = bool(self.eng.camera.discover().get('connected'))
        if cam_ok:
            if getattr(self.eng.camera, '_frozen_raw_jpg', None) is not None:
                with contextlib.suppress(Exception):
                    await self.eng.camera.set_freeze(False)
            gate_jpg = await self.eng.camera.capture_jpeg(high_res=False, crop_box=None, pencil_boost=eff_p)
            thumb = await asyncio.to_thread(self._make_gray_thumb_sync, gate_jpg)

            async def _cam_hi():
                return await self.eng.camera.capture_jpeg(high_res=True, crop_box=None, pencil_boost=eff_p)

            cnt = await self._run_funnel_once(gate_jpg, _cam_hi, cur_thumb=thumb, manual=True, pencil_boost=eff_p)
            return {'ok': True, 'source': 'camera', 'checked_new': cnt, 'note': self.last_note, 'cowrite': self.public_state()}

        if self.eng.phone_studio.has_page():
            act_pid_cw = str(self.eng.phone_studio.active_id or '').strip()
            pg_jpg = await self.eng.phone_studio.get_rotated_stage_jpeg()
            thumb = await asyncio.to_thread(self._make_gray_thumb_sync, pg_jpg)
            cnt = await self._run_funnel_once(
                pg_jpg, lambda: asyncio.sleep(0, result=pg_jpg), cur_thumb=thumb, manual=True, pencil_boost=eff_p, stage_page_id=act_pid_cw,
            )
            return {'ok': True, 'source': 'phone_stage', 'checked_new': cnt, 'note': self.last_note, 'cowrite': self.public_state()}

        raise LookupError('未检测到高拍仪画面，也没有「手机拍题」暂存页；请先连接高拍仪或手机传一页作业')

    @staticmethod
    def _is_fresh_revision_page(act_pg: dict, hit: dict) -> bool:
        """Compare page upload time against item check time at sub-second precision (unless ts was manually overridden)."""
        if not isinstance(act_pg, dict) or not isinstance(hit, dict):
            return False
        h_ts = int(hit.get('ts') or 0)
        h_ms = float(hit.get('ts_ms') or 0.0)
        p_ms = float(act_pg.get('uploaded_ms') or 0.0)
        if h_ms > 0.0 and p_ms > 0.0 and int(h_ms) == h_ts:
            return p_ms > h_ms
        return int(act_pg.get('uploaded_at') or 0) > h_ts

    def _resolve_recheck_stage_page_id(self, hit: dict) -> str:
        """Pick bound Phone Studio page ID, or a newer unscanned revision page uploaded for this item."""
        eff_pid = str(hit.get('stage_page_id') or '').strip()
        cur_act_id = str(self.eng.phone_studio.active_id or '').strip()
        pages = [p for p in self.eng.phone_studio.pages if isinstance(p, dict)]
        act_pg = next((p for p in pages if p.get('id') == cur_act_id), None)
        page_ids_alive = {p.get('id') for p in pages}
        act_is_orig_scan = bool(
            cur_act_id
            and any((x.get('orig_stage_page_id') or x.get('stage_page_id')) == cur_act_id for x in self.items)
        )
        if (
            cur_act_id
            and cur_act_id != eff_pid
            and act_pg is not None
            and self._is_fresh_revision_page(act_pg, hit)
            and not act_is_orig_scan
        ):
            return cur_act_id
        if eff_pid and eff_pid in page_ids_alive:
            return eff_pid
        return cur_act_id

    async def recheck_item(self, item_id: str, image_bytes: bytes = None, pencil_boost: bool = None) -> dict:
        """Skip Stage-2 Gatekeeper and directly re-crop + re-verify one specific problem after student revision."""
        self._ensure_loaded()
        iid = str(item_id or '').strip()
        hit = next((x for x in self.items if x.get('id') == iid), None)
        if not hit:
            raise LookupError('未找到该题目记录')
        eff_p = self.pencil_boost if pencil_boost is None else bool(pencil_boost)
        async with self._pipeline_lock:
            run_clear_gen = self._clear_gen
            eff_pid_rc = str(hit.get('stage_page_id') or '').strip()
            old_pid_rc = eff_pid_rc
            cam_conn_rc = bool(self.eng.camera.discover().get('connected'))
            has_ph_rc = self.eng.phone_studio.has_page()
            if image_bytes is not None:
                hi_jpg = await self.eng.phone_studio.normalize_to_jpeg(image_bytes)
            elif has_ph_rc and (not cam_conn_rc or bool(eff_pid_rc)):
                eff_pid_rc = self._resolve_recheck_stage_page_id(hit)
                if eff_pid_rc:
                    self.eng.phone_studio.select_page(eff_pid_rc)
                hi_jpg = await self.eng.phone_studio.get_rotated_stage_jpeg(eff_pid_rc)
            elif cam_conn_rc:
                if getattr(self.eng.camera, '_frozen_raw_jpg', None) is not None:
                    with contextlib.suppress(Exception):
                        await self.eng.camera.set_freeze(False)
                hi_jpg = await self.eng.camera.capture_jpeg(high_res=True, crop_box=None, pencil_boost=eff_p)
            else:
                raise LookupError('未检测到高拍仪或已传作业页，请先连接高拍仪或上传修改后的作业页')
            if self.enabled:
                self.last_motion_mono = time.monotonic()
            q_m_rc = await asyncio.to_thread(self._frame_quality_metrics_sync, hi_jpg)
            if q_m_rc['mean_luma'] < 24.0:
                self.stats['dark_skipped'] = int(self.stats.get('dark_skipped') or 0) + 1
                self.lighting_warn = 'dark'
                self.last_note = '💡 环境光线偏暗或镜头受遮挡，建议打开台灯或开启高拍仪补光后再点复查'
                self.seq += 1
                return {'ok': True, 'updated': False, 'note': self.last_note, 'cowrite': self.public_state()}
            if q_m_rc['mean_luma'] > 249.0 and q_m_rc['contrast_span'] < 14:
                self.lighting_warn = 'glare'
                self.last_note = '☀️ 纸面局部反光过强，请微调台灯角度避免眩光后再点复查'
                self.seq += 1
                return {'ok': True, 'updated': False, 'note': self.last_note, 'cowrite': self.public_state()}
            self.lighting_warn = ''
            if self.enabled:
                self.phase = 'reviewing'
            cand = {
                '_force_id': hit['id'],
                'no': hit['no'],
                'prob_key': hit.get('prob_key') or self._norm_prob_key(hit['no']),
                'subject': hit.get('subject') or 'math',
                'box': hit.get('box') if isinstance(hit.get('box'), dict) else {'x': 0.02, 'y': 0.05, 'w': 0.96, 'h': 0.90},
                'stem_brief': hit.get('topic') or '',
                'stage_page_id': eff_pid_rc,
            }
            try:
                obj, crop_jpg = await self._fetch_review_eval(hi_jpg, cand, self.subject, pencil_boost=eff_p)
                if self._clear_gen != run_clear_gen:
                    return {'ok': True, 'updated': False, 'note': self.last_note, 'cowrite': self.public_state()}
                rec = self._upsert_reviewed_candidate(cand, obj, crop_jpg)
                if rec is not None and old_pid_rc and old_pid_rc != eff_pid_rc:
                    self._sync_page_check_marks(old_pid_rc, clear_if_empty=True)
            finally:
                if self.enabled:
                    self.phase = 'watching'
            if rec is None:
                self.last_note = f"{hit['no']} 位置或字迹暂不够清晰，若纸张移动过请点右侧「立即巡检」"
                self.seq += 1
                return {'ok': True, 'updated': False, 'note': self.last_note, 'cowrite': self.public_state()}
            self._poke_display_wake(force=True)
            if rec['status'] == 'ok':
                self.last_note = f"🎉 {rec['no']} 订正复查通过！步骤与结果完全正确 ✓"
            else:
                self.last_note = f"⚠️ {rec['no']} 复查仍有易错点：{rec['checkpoint']}"
            self.seq += 1
            return {
                'ok': True,
                'updated': True,
                'verdict': rec['status'],
                'note': self.last_note,
                'cowrite': self.public_state(),
            }

    async def recheck_all_warns(self, image_bytes: bytes = None, pencil_boost: bool = None) -> dict:
        """One-capture batch parallel recheck for up to 4 unresolved warn items (0 gatekeeper calls)."""
        self._ensure_loaded()
        epoch_warns = [
            x for x in self.items
            if x.get('status') == 'warn' and not x.get('resolved') and int(x.get('page_epoch') or 1) == self.page_epoch
        ]
        all_warns = [x for x in self.items if x.get('status') == 'warn' and not x.get('resolved')]
        cam_conn_bw = bool(self.eng.camera.discover().get('connected'))
        has_ph_bw = self.eng.phone_studio.has_page()
        has_ph_warns = any(bool(str(x.get('stage_page_id') or '').strip()) for x in all_warns)
        use_phone_pool = (image_bytes is None and has_ph_bw and (not cam_conn_bw or has_ph_warns))
        if use_phone_pool:
            seen_ids = {x['id'] for x in epoch_warns}
            targets = (epoch_warns + [x for x in all_warns if x['id'] not in seen_ids])[:4]
        else:
            targets = (epoch_warns or all_warns)[:4]
        if not targets:
            return {
                'ok': True,
                'checked_count': 0,
                'passed_count': 0,
                'still_warn_count': 0,
                'note': '当前没有待复查的易错题 ✓',
                'cowrite': self.public_state(),
            }
        eff_p = self.pencil_boost if pencil_boost is None else bool(pencil_boost)
        async with self._pipeline_lock:
            run_clear_gen = self._clear_gen
            shared_hi_jpg = b''
            hi_by_pid = {}
            target_pids = []
            cam_conn_bw = bool(self.eng.camera.discover().get('connected'))
            has_ph_bw = self.eng.phone_studio.has_page()
            if image_bytes is not None:
                shared_hi_jpg = await self.eng.phone_studio.normalize_to_jpeg(image_bytes)
                target_pids = [str(h.get('stage_page_id') or '').strip() for h in targets]
            elif has_ph_bw or cam_conn_bw:
                need_cam = False
                for hit_t in targets:
                    bound_pid_t = str(hit_t.get('stage_page_id') or '').strip()
                    if has_ph_bw and (not cam_conn_bw or bool(bound_pid_t)):
                        eff_pid_t = self._resolve_recheck_stage_page_id(hit_t)
                        if eff_pid_t not in hi_by_pid:
                            hi_by_pid[eff_pid_t] = await self.eng.phone_studio.get_rotated_stage_jpeg(eff_pid_t)
                        target_pids.append(eff_pid_t)
                    else:
                        need_cam = True
                        target_pids.append('')
                if need_cam:
                    if getattr(self.eng.camera, '_frozen_raw_jpg', None) is not None:
                        with contextlib.suppress(Exception):
                            await self.eng.camera.set_freeze(False)
                    shared_hi_jpg = await self.eng.camera.capture_jpeg(high_res=True, crop_box=None, pencil_boost=eff_p)
                    if hi_by_pid:
                        hi_by_pid[''] = shared_hi_jpg
                if len(hi_by_pid) == 1 and not need_cam:
                    self.eng.phone_studio.select_page(next(iter(hi_by_pid)))
                if not shared_hi_jpg and target_pids:
                    shared_hi_jpg = hi_by_pid.get(target_pids[0], b'')
            else:
                raise LookupError('未检测到高拍仪或已传作业页，请先连接高拍仪或上传修改后的作业页')
            if self.enabled:
                self.last_motion_mono = time.monotonic()
            skipped_bad_pages = 0
            if hi_by_pid:
                bad_pids = {}
                for pid_q, jpg_q in hi_by_pid.items():
                    qm_q = await asyncio.to_thread(self._frame_quality_metrics_sync, jpg_q)
                    if qm_q['mean_luma'] < 24.0:
                        bad_pids[pid_q] = 'dark'
                    elif qm_q['mean_luma'] > 249.0 and qm_q['contrast_span'] < 14:
                        bad_pids[pid_q] = 'glare'
                kept_pairs = [(h, p) for h, p in zip(targets, target_pids) if p not in bad_pids]
                skipped_bad_pages = len(targets) - len(kept_pairs)
                if not kept_pairs:
                    first_bad = bad_pids.get(target_pids[0], 'dark')
                    if first_bad == 'dark':
                        self.stats['dark_skipped'] = int(self.stats.get('dark_skipped') or 0) + 1
                        self.lighting_warn = 'dark'
                        self.last_note = '💡 环境光线偏暗或镜头受遮挡，建议打开台灯或开启高拍仪补光后再批量复查'
                    else:
                        self.lighting_warn = 'glare'
                        self.last_note = '☀️ 纸面局部反光过强，请微调台灯角度避免眩光后再批量复查'
                    self.seq += 1
                    return {'ok': True, 'checked_count': 0, 'passed_count': 0, 'still_warn_count': len(targets), 'note': self.last_note, 'cowrite': self.public_state()}
                targets = [h for h, _ in kept_pairs]
                target_pids = [p for _, p in kept_pairs]
            else:
                q_m_rc = await asyncio.to_thread(self._frame_quality_metrics_sync, shared_hi_jpg)
                if q_m_rc['mean_luma'] < 24.0:
                    self.stats['dark_skipped'] = int(self.stats.get('dark_skipped') or 0) + 1
                    self.lighting_warn = 'dark'
                    self.last_note = '💡 环境光线偏暗或镜头受遮挡，建议打开台灯或开启高拍仪补光后再批量复查'
                    self.seq += 1
                    return {'ok': True, 'checked_count': 0, 'passed_count': 0, 'still_warn_count': len(targets), 'note': self.last_note, 'cowrite': self.public_state()}
                if q_m_rc['mean_luma'] > 249.0 and q_m_rc['contrast_span'] < 14:
                    self.lighting_warn = 'glare'
                    self.last_note = '☀️ 纸面局部反光过强，请微调台灯角度避免眩光后再批量复查'
                    self.seq += 1
                    return {'ok': True, 'checked_count': 0, 'passed_count': 0, 'still_warn_count': len(targets), 'note': self.last_note, 'cowrite': self.public_state()}
            self.lighting_warn = ''
            if self.enabled:
                self.phase = 'reviewing'
            cands = [
                {
                    '_force_id': hit['id'],
                    'no': hit['no'],
                    'prob_key': hit.get('prob_key') or self._norm_prob_key(hit['no']),
                    'subject': hit.get('subject') or 'math',
                    'box': hit.get('box') if isinstance(hit.get('box'), dict) else {'x': 0.02, 'y': 0.05, 'w': 0.96, 'h': 0.90},
                    'stem_brief': hit.get('topic') or '',
                    'stage_page_id': t_pid,
                }
                for hit, t_pid in zip(targets, target_pids)
            ]
            sem = asyncio.Semaphore(2)

            async def _bounded_recheck(c_item):
                async with sem:
                    pg_jpg_c = hi_by_pid.get(c_item.get('stage_page_id') or '', shared_hi_jpg)
                    return await self._fetch_review_eval(pg_jpg_c, c_item, self.subject, pencil_boost=eff_p)

            try:
                eval_list = await asyncio.gather(*[_bounded_recheck(c) for c in cands], return_exceptions=True)
                for ev_r in eval_list:
                    if isinstance(ev_r, asyncio.CancelledError):
                        raise ev_r
                if self._clear_gen != run_clear_gen:
                    return {'ok': True, 'checked_count': 0, 'passed_count': 0, 'still_warn_count': 0, 'note': self.last_note, 'cowrite': self.public_state()}
                checked_cnt = 0
                passed_cnt = 0
                still_warn_cnt = 0
                old_pids = set()
                for hit, cand, ev_item in zip(targets, cands, eval_list):
                    if isinstance(ev_item, BaseException):
                        log.warning('CoWrite batch recheck skipped %s: %s', hit.get('no'), _redact_secrets(ev_item))
                        continue
                    old_p = str(hit.get('stage_page_id') or '').strip()
                    obj, crop_jpg = ev_item
                    rec = self._upsert_reviewed_candidate(cand, obj, crop_jpg)
                    if rec is None:
                        continue
                    checked_cnt += 1
                    if rec['status'] == 'ok':
                        passed_cnt += 1
                    else:
                        still_warn_cnt += 1
                    if old_p and old_p != rec.get('stage_page_id'):
                        old_pids.add(old_p)
                for old_p in old_pids:
                    self._sync_page_check_marks(old_p, clear_if_empty=True)
            finally:
                if self.enabled:
                    self.phase = 'watching'
            if checked_cnt > 0:
                self._poke_display_wake(force=True)
                bad_sfx = f'（另有 {skipped_bad_pages} 题所在页过暗或反光暂跳过）' if skipped_bad_pages > 0 else ''
                if passed_cnt > 0 and still_warn_cnt == 0:
                    self.last_note = f'🎉 批量复查 {checked_cnt} 题全部订正通过！步骤与结果正确 ✓{bad_sfx}'
                elif passed_cnt > 0:
                    self.last_note = f'🎉 批量复查：{passed_cnt} 题已改对通过，仍有 {still_warn_cnt} 题建议再核{bad_sfx}'
                else:
                    self.last_note = f'已完成 {checked_cnt} 题批量复查，仍有 {still_warn_cnt} 处待进一步自查{bad_sfx}'
            else:
                self.last_note = '待复查题目的字迹暂不够清晰，若纸张移动过请点「立即巡检」'
            if skipped_bad_pages > 0:
                still_warn_cnt += skipped_bad_pages
            self.seq += 1
            return {
                'ok': True,
                'checked_count': checked_cnt,
                'passed_count': passed_cnt,
                'still_warn_count': still_warn_cnt,
                'note': self.last_note,
                'cowrite': self.public_state(),
            }

    async def check_crop(
        self,
        crop_box: dict,
        no_hint: str = '',
        subject: str = None,
        page_id: str = '',
        image_bytes: bytes = None,
        pencil_boost: bool = None,
    ) -> dict:
        """Directly verify one user-boxed problem on dense 2-column A3 mock exams (~6s, bypassing Stage-2 Gatekeeper)."""
        self._ensure_loaded()
        s_box = PhoneStudioManager._sanitize_crop_box(crop_box) if isinstance(crop_box, dict) else None
        if not s_box:
            raise ValueError('请先拖动绿框圈出要静默核查的题目区域')
        changed_cfg = False
        if subject is not None:
            self.subject = self._norm_subject(subject)
            changed_cfg = True
        if isinstance(pencil_boost, bool) and self.pencil_boost != pencil_boost:
            self.pencil_boost = pencil_boost
            changed_cfg = True
        if changed_cfg:
            self._save_state()
        eff_p = self.pencil_boost if pencil_boost is None else bool(pencil_boost)
        pid_req = str(page_id or '').strip()
        async with self._pipeline_lock:
            run_clear_gen = self._clear_gen
            eff_pid_cc = ''
            if image_bytes is not None:
                hi_jpg = await self.eng.phone_studio.normalize_to_jpeg(image_bytes)
            elif pid_req:
                if not self.eng.phone_studio.select_page(pid_req):
                    raise LookupError('未找到指定的暂存作业页，请先上传作业照片')
                eff_pid_cc = pid_req
                hi_jpg = await self.eng.phone_studio.get_rotated_stage_jpeg(pid_req)
            elif bool(self.eng.camera.discover().get('connected')):
                if getattr(self.eng.camera, '_frozen_raw_jpg', None) is not None:
                    with contextlib.suppress(Exception):
                        await self.eng.camera.set_freeze(False)
                hi_jpg = await self.eng.camera.capture_jpeg(high_res=True, crop_box=None, pencil_boost=eff_p)
            elif self.eng.phone_studio.has_page():
                eff_pid_cc = str(self.eng.phone_studio.active_id or '').strip()
                hi_jpg = await self.eng.phone_studio.get_rotated_stage_jpeg()
            else:
                raise LookupError('未检测到高拍仪或已传作业页，请先连接高拍仪或手机传图')
            if eff_pid_cc:
                self._maybe_advance_epoch_for_stage_page(eff_pid_cc)
            else:
                self._last_stage_page_id = ''
            if self.enabled:
                self.last_motion_mono = time.monotonic()
            q_m = await asyncio.to_thread(self._frame_quality_metrics_sync, hi_jpg)
            if q_m['mean_luma'] < 24.0:
                self.stats['dark_skipped'] = int(self.stats.get('dark_skipped') or 0) + 1
                self.lighting_warn = 'dark'
                self.last_note = '💡 环境光线偏暗，建议打开台灯或开启高拍仪补光后再框选核查'
                self.seq += 1
                return {'ok': True, 'updated': False, 'note': self.last_note, 'cowrite': self.public_state()}
            if q_m['mean_luma'] > 249.0 and q_m['contrast_span'] < 14:
                self.lighting_warn = 'glare'
                self.last_note = '☀️ 纸面局部反光过强，请微调台灯角度避免眩光后再框选核查'
                self.seq += 1
                return {'ok': True, 'updated': False, 'note': self.last_note, 'cowrite': self.public_state()}
            self.lighting_warn = ''
            if self.enabled:
                self.phase = 'reviewing'
            clean_no = str(no_hint or '').strip()[:16] or '框选题'
            subj_c = self.subject if self.subject in ('math', 'english', 'chinese') else ('physics' if self.subject == 'phys_chem' else 'math')
            cand = {
                '_crop_direct': not bool(str(no_hint or '').strip()),
                'no': clean_no,
                'prob_key': self._norm_prob_key(clean_no) or f"crop_{int(round(s_box['x'] * 100))}_{int(round(s_box['y'] * 100))}",
                'subject': subj_c,
                'box': s_box,
                'stem_brief': '',
                'stage_page_id': eff_pid_cc,
            }
            try:
                obj, crop_jpg = await self._fetch_review_eval(hi_jpg, cand, self.subject, pencil_boost=eff_p)
                if self._clear_gen != run_clear_gen:
                    return {'ok': True, 'updated': False, 'note': self.last_note, 'cowrite': self.public_state()}
                rec = self._upsert_reviewed_candidate(cand, obj, crop_jpg)
            finally:
                if self.enabled:
                    self.phase = 'watching'
            self.last_check_ts = int(time.time())
            if rec is None:
                self.last_note = '框选区域未包含完整印刷题干与已写完手写答案（或字迹偏淡），为防误报已跳过'
                self.seq += 1
                return {'ok': True, 'updated': False, 'note': self.last_note, 'cowrite': self.public_state()}
            self._poke_display_wake(force=True)
            if rec['status'] == 'ok':
                self.last_note = f"✅ {rec['no']} 框选核查通过：步骤与结论正确 ✓"
            else:
                self.last_note = f"⚠️ {rec['no']} 建议自查：{rec['checkpoint']}"
            self.seq += 1
            return {
                'ok': True,
                'updated': True,
                'verdict': rec['status'],
                'item_id': rec['id'],
                'note': self.last_note,
                'cowrite': self.public_state(),
            }

    async def variant_from_item(self, item_id: str, mode: str = 'fast', silent: bool = True, solo_challenge: bool = False) -> dict:
        """Generate a 1-tap silent isomorphic Grade-9 trap drill directly from a Co-Write card."""
        self._ensure_loaded()
        iid = str(item_id or '').strip()
        hit = next((x for x in self.items if x.get('id') == iid), None)
        if not hit:
            raise LookupError('未找到该题目记录')
        stem = str(hit.get('ocr_stem') or '').strip()
        topic = str(hit.get('topic') or '初三核心考点').strip()
        raw_cp_v = hit.get('checkpoint') if hit.get('status') == 'warn' else (hit.get('warn_checkpoint') or hit.get('checkpoint'))
        cp = re.sub(r'[\s✓✔]+$', '', str(raw_cp_v or '')).strip()
        if '步骤严谨' in cp or '推导与结果正确' in cp:
            cp = ''
        subj = str(hit.get('subject') or 'math').strip()
        if not stem and hit.get('snap_name'):
            with contextlib.suppress(Exception):
                _, snap_raw = _read_regular_bytes(SNAP_DIR / hit['snap_name'], 8 * 1024 * 1024)
                ocr_q = await self.eng._ocr_question_from_image(snap_raw, extra_note='')
                stem = sanitize_ocr_question(ocr_q)[:180]
                if stem:
                    hit['ocr_stem'] = stem
                    self._save_state()
        ref_txt = stem or f"九年级{topic}典型题"
        need_calc = subj not in ('english', 'chinese')
        calc_fld = '"check_calc":"内部完整验算确保数据合理整除且无歧义",' if need_calc else ''
        var_rule = (
            '请面向九年级（初三）学生，保持完全相同的古诗文/文言/语言运用或英语语法考点与易错字词陷阱，更换全新设问情境或语境，新编一道严谨清晰的【初三靶向巩固变式题】（严禁在题干中直接附带答案）！'
            if not need_calc else
            '请面向九年级（初三）学生，更换全新数字/参数或实验情境，专门针对同一易错陷阱新编一道难度相当、题干严谨清晰的【初三靶向巩固变式题】（严禁在题干中附带答案或提示）！'
        )
        prompt = (
            f"原题参考：「{ref_txt}」\n"
            f"核心考点：「{topic}」；需重点检验的易错点：「{cp or '关键边界条件与符号'}」。\n"
            f"{var_rule}\n"
            f'只输出一行严格JSON（不要代码块）：{{{calc_fld}"title":"同类变式：{topic[:8]}","question":"完整变式题干文字"}}'
        )
        raw_v = await self.eng._post_chat_once('aux', [{'role': 'user', 'content': prompt}], temperature=0.42, timeout_s=34.0)
        objs, _ = extract_json_objects_from_buffer(raw_v)
        v_obj = objs[0] if objs and isinstance(objs[0], dict) else {}
        new_q = sanitize_ocr_question(str(v_obj.get('question') or '').strip())
        if len(new_q) < 6:
            raise RuntimeError('同类变式题生成未就绪，请重试一次')
        v_subj_tag = {
            'chinese': '【初中语文】', 'english': '【初中英语】',
            'physics': '【初中物理】', 'chemistry': '【初中化学】',
        }.get(subj, '')
        if v_subj_tag and not new_q.startswith('【') and detect_subject(new_q) == 'math':
            new_q = f'{v_subj_tag}{new_q}'
        v_title = str(v_obj.get('title') or f"同类变式：{topic}"[:28]).strip()[:32]
        sess = self.eng.start_live_problem(
            question=new_q,
            mode=_norm_mode(mode or 'fast'),
            title=v_title,
            source='cowrite',
            open_ui=True,
            pedagogy='junior',
            teach_mode='standard',
            mistake_id=str(hit.get('mistake_id') or '').strip(),
            solo_challenge=bool(solo_challenge),
            silent_tts=bool(silent),
            cowrite_item_id=hit['id'],
        )
        self.eng.open_ui_target = 'board'
        return {'ok': True, 'silent': bool(silent), 'session': sess}

    def _archive_current_session_if_meaningful(self):
        if len(self.items) < 2:
            return
        cur_key = ','.join(sorted(str(x.get('id') or '') for x in self.items if x.get('id')))[:400]
        dg = self.build_digest()
        entry = {
            'ts': int(time.time()),
            'total': len(self.items),
            'first_pass_ok': int(dg['first_pass_ok']),
            'accuracy_pct': int(dg['accuracy_pct']),
            'corrected': int(dg['corrected_count']),
            'mastered': int(dg.get('mastered_count') or 0),
            'focus_min': int(dg.get('focus_min') or 1),
            'weak_topics': list(dg['weak_topics'][:3]),
        }
        if cur_key and cur_key == self._last_archive_key and self.recent_sessions:
            self.recent_sessions[0] = entry
        else:
            self.recent_sessions = ([entry] + self.recent_sessions)[:7]
            self._last_archive_key = cur_key

    def resolve_action(self, item_id: str = '', resolved=None, clear_all: bool = False, new_page: bool = False, star=None) -> dict:
        self._ensure_loaded()
        if self.enabled:
            self.last_motion_mono = time.monotonic()
        if clear_all:
            self._archive_current_session_if_meaningful()
            prev_pids = {str(it.get('stage_page_id') or '').strip() for it in self.items if str(it.get('stage_page_id') or '').strip()}
            if hasattr(self.eng, '_ensure_mistakes_loaded'):
                self.eng._ensure_mistakes_loaded()
            mb_snaps = {
                str(m.get('image_url') or '').rsplit('/', 1)[-1]
                for m in (getattr(self.eng, 'mistakes', ()) or ())
                if isinstance(m, dict) and str(m.get('image_url') or '').startswith('/api/tutor/snap/c0_')
            }
            for it in self.items:
                sn = it.get('snap_name') or ''
                if sn and sn not in mb_snaps and not it.get('enrolled_mistake'):
                    with contextlib.suppress(OSError):
                        (SNAP_DIR / sn).unlink(missing_ok=True)
            self.items = []
            self._last_archive_key = ''
            self._clear_gen += 1
            self.page_epoch += 1
            self._last_stage_page_id = ''
            self._has_more_pending = False
            self.anchor_thumb = b''
            self.saw_activity = True
            self.last_note = '已清空伴写看板记录，可放入新一页作业继续书写'
            self.seq += 1
            self._save_state()
            for pid_cl in prev_pids:
                self._sync_page_check_marks(pid_cl, clear_if_empty=True)
            return self.public_state()
        if new_page:
            self.page_epoch += 1
            self._last_stage_page_id = ''
            self._has_more_pending = False
            self.anchor_thumb = b''
            self.saw_activity = True
            self.last_note = f'已切换至新作业页（第 {self.page_epoch} 页），新页第 1 题起将重新核对'
            self.seq += 1
            self._save_state()
            return self.public_state()
        iid = str(item_id or '').strip()
        hit = next((x for x in self.items if x.get('id') == iid), None)
        if not hit:
            raise LookupError('未找到该伴写记录')
        if star is not None:
            hit['starred'] = bool(star)
        if resolved is None and star is None:
            resolved = True
        if resolved is not None:
            hit['resolved'] = bool(resolved)
        self.seq += 1
        self._save_state()
        if hit.get('stage_page_id'):
            self._sync_page_check_marks(hit['stage_page_id'])
        return self.public_state()

    def mark_blackboard_solved(self, item_id: str = '', mistake_id: str = '', is_variant: bool = False) -> bool:
        self._ensure_loaded()
        iid = str(item_id or '').strip()
        mid = str(mistake_id or '').strip()
        if not iid and not mid:
            return False
        hits = [
            x for x in self.items
            if (iid and x.get('id') == iid) or (mid and str(x.get('mistake_id') or '').strip() == mid)
        ]
        if not hits:
            return False
        pids = set()
        for hit in hits:
            hit['resolved'] = True
            hit['blackboard_solved'] = True
            if is_variant:
                hit['variant_passed'] = True
            if hit.get('stage_page_id'):
                pids.add(hit['stage_page_id'])
        self.seq += 1
        self._save_state()
        for pid in pids:
            self._sync_page_check_marks(pid)
        return True

    def _graduated_mistake_ids(self) -> set:
        if hasattr(self.eng, '_ensure_mistakes_loaded'):
            self.eng._ensure_mistakes_loaded()
        return {
            str(m.get('id') or '').strip()
            for m in (getattr(self.eng, 'mistakes', None) or ())
            if isinstance(m, dict) and m.get('graduated') and m.get('id')
        }

    def _is_item_graduated(self, item: dict, grad_mids=None) -> bool:
        if bool(item.get('ever_graduated')):
            return True
        mid_x = str(item.get('mistake_id') or '').strip()
        if not mid_x:
            return False
        g_set = self._graduated_mistake_ids() if grad_mids is None else grad_mids
        return mid_x in g_set

    def _is_item_enrollable(self, item: dict, grad_mids=None) -> bool:
        return (
            (item.get('status') == 'warn' or bool(item.get('corrected_pass')) or bool(item.get('starred')))
            and not item.get('enrolled_mistake')
            and not self._is_item_graduated(item, grad_mids)
        )

    def enroll_mistake(self, item_id: str = '', enroll_all_warns: bool = False) -> dict:
        self._ensure_loaded()
        grad_mids = self._graduated_mistake_ids()
        if enroll_all_warns:
            targets = [x for x in self.items if self._is_item_enrollable(x, grad_mids)]
        else:
            iid = str(item_id or '').strip()
            hit = next((x for x in self.items if x.get('id') == iid), None)
            if not hit:
                raise LookupError('未找到该伴写记录')
            targets = [hit]
        if not targets:
            return {
                'ok': True,
                'enrolled_count': 0,
                'enrolled_ids': [],
                'already_graduated': False,
                'mistake_summary': self.eng.public_mistake_summary(),
                'cowrite': self.public_state(),
            }
        now_ts = int(time.time())
        enrolled_cnt = 0
        enrolled_ids = []
        for it in targets:
            if self._is_item_graduated(it, grad_mids):
                continue
            mid_cur = str(it.get('mistake_id') or '').strip()
            existing_mb = (
                next((m for m in self.eng.mistakes if isinstance(m, dict) and m.get('id') == mid_cur), None)
                if mid_cur else None
            )
            if not enroll_all_warns and existing_mb is not None and it.get('enrolled_mistake'):
                continue
            stem_q = str(it.get('ocr_stem') or '').strip()
            topic_s = str(it.get('topic') or '初三易错点').strip()
            no_s = str(it.get('no') or '本题').strip()
            is_w_cur = (it.get('status') == 'warn')
            cp_raw = it.get('checkpoint') if is_w_cur else (it.get('warn_checkpoint') or it.get('checkpoint'))
            dh_raw = it.get('deep_hint') if is_w_cur else (it.get('warn_deep_hint') or it.get('deep_hint'))
            sw_raw = it.get('step_where') if is_w_cur else (it.get('warn_step_where') or it.get('step_where'))
            ft_raw = it.get('formula_tip') if is_w_cur else (it.get('warn_formula_tip') or it.get('formula_tip'))
            cp_s = re.sub(r'[\s✓✔]+$', '', str(cp_raw or '')).strip()
            dh_s = re.sub(r'[\s✓✔]+$', '', str(dh_raw or '')).strip()
            sw_s = re.sub(r'[\s✓✔]+$', '', str(sw_raw or '')).strip()
            ft_s = re.sub(r'[\s✓✔]+$', '', str(ft_raw or '')).strip()
            if not cp_s or '步骤严谨' in cp_s or '推导与结果正确' in cp_s:
                cp_s = (
                    '重点标记巩固：独立复盘关键条件与推导步骤'
                    if (it.get('starred') and not is_w_cur and not it.get('corrected_pass')) else
                    f'请重点回查{topic_s}的关键易错条件'
                )
            if '步骤严谨' in dh_s or '推导与结果正确' in dh_s:
                dh_s = ''
            if '步骤严谨' in ft_s or '推导与结果正确' in ft_s:
                ft_s = ''
            if not stem_q or len(stem_q) < 6:
                stem_q = f"【初三伴写错题·{topic_s}】{no_s}（手写易错环节：{sw_s or topic_s}；自查要点：{cp_s}）"
            mid = mid_cur or ('mb_' + hashlib.sha256(f"{stem_q}|{it['id']}".encode('utf-8')).hexdigest()[:10])
            if existing_mb is None:
                existing_mb = next((m for m in self.eng.mistakes if isinstance(m, dict) and m.get('id') == mid), None)
            sn = str(it.get('snap_name') or '').strip()
            img_u = f'/api/tutor/snap/{sn}' if sn else ''
            subj_m = it.get('subject') if it.get('subject') in ('math', 'physics', 'chemistry', 'english', 'chinese') else detect_subject(stem_q)
            rev_steps = [
                {
                    'step': 1,
                    'title': f'回看原题 · {topic_s[:10]}',
                    'say': f'我们一起回看这道伴写收录的{topic_s}易错题，先聚焦刚才需要留意的检查点：{cp_s}',
                    'board': f"【原题线索】{stem_q}\n【易错定位】{sw_s or topic_s}\n【自查方向】{cp_s}",
                    'quiz': None,
                    'draw': build_step0_warmup_draw(stem_q),
                },
                {
                    'step': 2,
                    'title': '深度自查 · 避坑巩固',
                    'say': dh_s or cp_s or '请结合原题条件独立重算核对一遍，点击下方「换数复测」即可检验是否彻底掌握！',
                    'board': f"【进阶追问】{dh_s or cp_s}" + (f"\n【关联定理】{ft_s}" if ft_s else '') + "\n建议直接点右侧「换数复测」或「先独做」检验同类题掌握度",
                    'quiz': None,
                    'draw': [],
                },
            ]
            has_quiz_steps = bool(existing_mb and any(isinstance(s, dict) and s.get('quiz') for s in (existing_mb.get('steps') or [])))
            keep_grad = bool(existing_mb and existing_mb.get('graduated'))
            entry = {
                'id': mid,
                'orig_sid': str(existing_mb.get('orig_sid') or f'm:{mid}') if has_quiz_steps else f'm:{mid}',
                'title': f"{'⭐' if it.get('starred') else ''}{no_s}·{topic_s}"[:34],
                'question': stem_q[:600],
                'subject': subj_m,
                'pedagogy': 'junior',
                'mode': 'deep',
                'teach_mode': 'diagnose_draft',
                'weak_step': int(existing_mb.get('weak_step') or 2) if existing_mb else 2,
                'weak_focus': (str(existing_mb.get('weak_focus') or '') or f"{topic_s}：{cp_s}")[:64] if existing_mb else (f"{topic_s}：{cp_s}")[:64],
                'error_tag': f"伴写·{topic_s[:10]}",
                'stage': int(existing_mb.get('stage') or 0) if existing_mb else 0,
                'graduated': keep_grad,
                'retest_pass_count': int(existing_mb.get('retest_pass_count') or 0) if existing_mb else 0,
                'created_at': int(existing_mb.get('created_at') or now_ts) if existing_mb else now_ts,
                'updated_at': now_ts,
                'next_review_at': (0 if keep_grad else int(existing_mb.get('next_review_at') or (now_ts + 86400))) if existing_mb else (now_ts + 86400),
                'image_url': img_u or (str(existing_mb.get('image_url') or '') if existing_mb else ''),
                'steps': copy.deepcopy(existing_mb['steps']) if has_quiz_steps else rev_steps,
            }
            self.eng.mistakes = [m for m in self.eng.mistakes if m.get('id') != mid]
            self.eng.mistakes.insert(0, entry)
            it['enrolled_mistake'] = True
            it['mistake_id'] = mid
            enrolled_cnt += 1
            enrolled_ids.append(mid)
        if enrolled_cnt > 0:
            self.eng._save_mistakes()
            self.seq += 1
            self._save_state()
        already_grad = bool((not enroll_all_warns) and len(targets) == 1 and enrolled_cnt == 0 and self._is_item_graduated(targets[0], grad_mids))
        return {
            'ok': True,
            'enrolled_count': enrolled_cnt,
            'enrolled_ids': enrolled_ids,
            'already_graduated': already_grad,
            'mistake_summary': self.eng.public_mistake_summary(),
            'cowrite': self.public_state(),
        }

    async def explain_item(self, item_id: str, mode: str = 'deep', pedagogy: str = 'junior', silent: bool = True) -> dict:
        self._ensure_loaded()
        iid = str(item_id or '').strip()
        hit = next((x for x in self.items if x.get('id') == iid), None)
        if not hit:
            raise LookupError('未找到该题目记录')
        img_bytes = None
        sn = str(hit.get('snap_name') or '').strip()
        if sn and re.fullmatch(r'c0_[0-9a-f]{12}\.jpg', sn):
            with contextlib.suppress(OSError):
                _, img_bytes = _read_regular_bytes(SNAP_DIR / sn, 8 * 1024 * 1024)
        if not img_bytes and self.eng.phone_studio.has_page() and isinstance(hit.get('box'), dict):
            with contextlib.suppress(Exception):
                pg_raw = await self.eng.phone_studio.get_rotated_stage_jpeg(str(hit.get('stage_page_id') or '').strip())
                img_bytes = await asyncio.to_thread(self._crop_problem_jpeg_sync, pg_raw, hit['box'])
        if not img_bytes and not hit.get('ocr_stem'):
            raise LookupError('该题原图裁剪已过期清理，请重新巡检本页后再点讲题')
        use_ped = 'junior'
        subj_tag = {
            'chinese': '【初中语文】', 'english': '【初中英语】',
            'physics': '【初中物理】', 'chemistry': '【初中化学】',
        }.get(str(hit.get('subject') or '').strip(), '')
        is_w_ex = (hit.get('status') == 'warn')
        w_sw = re.sub(r'[\s✓✔]+$', '', str((hit.get('step_where') if is_w_ex else hit.get('warn_step_where')) or '')).strip()
        w_cp = re.sub(r'[\s✓✔]+$', '', str((hit.get('checkpoint') if is_w_ex else hit.get('warn_checkpoint')) or '')).strip()
        w_ft = re.sub(r'[\s✓✔]+$', '', str((hit.get('formula_tip') if is_w_ex else hit.get('warn_formula_tip')) or '')).strip()
        if '步骤严谨' in w_cp or '推导与结果正确' in w_cp:
            w_cp = ''
        pinpoint_clause = (
            f"\n【伴写前置诊断锚点：学生在「{w_sw or hit.get('topic') or '关键步'}」存在易错点（提示：{w_cp}），请第1步先简短肯定已写对的前置思路，第2步直接围绕该易错处启发设问引导订正，严禁提前泄底！】"
            if w_cp else ''
        )
        q_prompt = (
            f"{subj_tag}请对这道九年级题目（{hit.get('no') or '本题'}·{hit.get('topic') or '步骤诊断'}）及手写解答进行草稿错因启发式诊断"
            + (f"（原题参考：{hit['ocr_stem']}）" if hit.get('ocr_stem') else '')
            + pinpoint_clause
        )
        sess = self.eng.start_live_problem(
            question=q_prompt,
            image_bytes=img_bytes,
            mode=_norm_mode(mode or 'deep'),
            title=f"{hit.get('no') or '本题'} 伴写错因精讲"[:32],
            source='cowrite',
            open_ui=True,
            pedagogy=use_ped,
            teach_mode='diagnose_draft',
            silent_tts=bool(silent),
            cowrite_item_id=hit['id'],
        )
        raw_rec = self.eng.sessions.get(sess['id'])
        if raw_rec is not None and raw_rec.get('steps') and int(raw_rec['steps'][0].get('step', -1)) == 0 and (hit.get('ocr_stem') or w_cp):
            if hit.get('ocr_stem') and not raw_rec.get('question'):
                raw_rec['question'] = str(hit['ocr_stem'])[:220]
            s0 = raw_rec['steps'][0]
            s0['title'] = f"伴写定位 · {(w_sw or hit.get('topic') or '关键步')[:12]}"
            b_lines = []
            if hit.get('ocr_stem'):
                b_lines.append(f"【已核题干】{hit['ocr_stem'][:140]}")
            if w_cp:
                b_lines.append(f"【自查定位】{w_sw or hit.get('topic') or '关键步'}：{w_cp}")
            if w_ft:
                b_lines.append(f"【关联定理】{w_ft}")
            b_lines.append('正在为你生成针对性 4 步交互拆解…')
            s0['board'] = '\n'.join(b_lines)
            if hit.get('ocr_stem'):
                s0['draw'] = build_step0_warmup_draw(hit['ocr_stem'])
            raw_rec['seq'] = int(raw_rec.get('seq') or 1) + 1
            sess = self.eng.public_session(raw_rec['id']) or sess
        self.eng.open_ui_target = 'board'
        return {'ok': True, 'silent': bool(silent), 'session': sess}

    def build_digest(self) -> dict:
        tot = len(self.items)
        subj_counts = {}
        topic_freq = {}
        for x in self.items:
            sj = str(x.get('subject') or 'math').strip()
            subj_counts[sj] = subj_counts.get(sj, 0) + 1
        for rs in self.recent_sessions[:7]:
            for tp in (rs.get('weak_topics') or ()):
                t_s = str(tp or '').strip()
                if t_s:
                    topic_freq[t_s] = topic_freq.get(t_s, 0) + 1
        grad_ids = self._graduated_mistake_ids()
        m_list = [m for m in (getattr(self.eng, 'mistakes', None) or ()) if isinstance(m, dict)]
        cw_active_mids = {
            str(m.get('id') or '').strip()
            for m in m_list
            if not m.get('graduated') and str(m.get('error_tag') or '').startswith('伴写·') and m.get('id')
        }
        unrec_sprint_ids = {
            str(x.get('id') or '')
            for x in self.items
            if self._is_item_enrollable(x, grad_ids)
            and str(x.get('mistake_id') or '').strip() not in cw_active_mids
        }
        sprintable_cnt = len(cw_active_mids) + len(unrec_sprint_ids)
        if tot == 0:
            rec_empty = [tp for tp, cnt in sorted(topic_freq.items(), key=lambda kv: -kv[1]) if cnt >= 2][:3]
            return {
                'accuracy_pct': 100, 'first_pass_ok': 0, 'corrected_count': 0,
                'weak_topics': [], 'unrecorded_warn_count': 0,
                'recurring_topics': rec_empty, 'subject_counts': {},
                'focus_min': 0, 'starred_count': 0, 'mastered_count': 0,
                'sprintable_count': sprintable_cnt,
            }
        first_ok = sum(1 for x in self.items if x.get('status') == 'ok' and not x.get('corrected_pass'))
        corr_cnt = sum(1 for x in self.items if x.get('corrected_pass') or (x.get('status') == 'warn' and x.get('resolved')))
        weak_topics = []
        for x in self.items:
            if x.get('status') == 'warn' or x.get('corrected_pass'):
                tp = str(x.get('topic') or '').strip()
                if tp and tp not in weak_topics:
                    weak_topics.append(tp)
        for tp in weak_topics:
            topic_freq[tp] = topic_freq.get(tp, 0) + 1
        rec_topics = [tp for tp, cnt in sorted(topic_freq.items(), key=lambda kv: -kv[1]) if cnt >= 2][:3]
        unrec_cnt = sum(1 for x in self.items if self._is_item_enrollable(x, grad_ids))
        now_t = int(time.time())
        ts_vals = [int(x.get('ts') or 0) for x in self.items if int(x.get('ts') or 0) > 0]
        t_lo = self.started_at if (self.enabled and self.started_at > 0 and (not ts_vals or self.started_at <= min(ts_vals))) else (min(ts_vals) if ts_vals else now_t)
        t_hi = now_t if self.enabled else (max(ts_vals) if ts_vals else t_lo)
        focus_min = max(1, min(180, int(round(max(60, t_hi - t_lo) / 60.0))))
        starred_cnt = sum(1 for x in self.items if x.get('starred'))
        mastered_cnt = sum(1 for x in self.items if x.get('variant_passed'))
        return {
            'accuracy_pct': int(round(100.0 * first_ok / max(1, tot))),
            'first_pass_ok': first_ok,
            'corrected_count': corr_cnt,
            'weak_topics': weak_topics[:4],
            'unrecorded_warn_count': unrec_cnt,
            'recurring_topics': rec_topics,
            'subject_counts': subj_counts,
            'focus_min': focus_min,
            'starred_count': starred_cnt,
            'mastered_count': mastered_cnt,
            'sprintable_count': sprintable_cnt,
        }

    def build_study_brief(self) -> str:
        """Build a spoiler-free evening study summary for parent WeChat copy or end-of-night review."""
        self._ensure_loaded()
        tot = len(self.items)
        if tot == 0:
            return ''
        dg = self.build_digest()
        subj_cn_map = {'math': '数学', 'physics': '物理', 'chemistry': '化学', 'english': '英语', 'chinese': '语文'}
        parts = [
            f"【初三静默伴写小结】共核 {tot} 题 · 一次全对率 {dg['accuracy_pct']}% · 专注 {dg['focus_min']} 分钟"
            + (f" · 已订正 {dg['corrected_count']} 题" if dg['corrected_count'] else '')
            + (f" · 👑变式吃透 {dg['mastered_count']} 题" if dg.get('mastered_count') else '')
        ]
        sc_items = [
            f"{subj_cn_map[k]} {v}题"
            for k, v in (dg.get('subject_counts') or {}).items()
            if k in subj_cn_map and int(v or 0) > 0
        ]
        if sc_items:
            parts.append('📚 涉及科目：' + ' / '.join(sc_items))
        focus_items = [
            x for x in self.items
            if x.get('status') == 'warn' or x.get('corrected_pass') or x.get('starred')
        ][:5]
        for x in focus_items:
            s_cn = subj_cn_map.get(str(x.get('subject') or 'math'), '综合')
            tp = str(x.get('topic') or '关键步').strip()
            cp_raw = (x.get('warn_checkpoint') or x.get('checkpoint')) if x.get('status') != 'warn' else x.get('checkpoint')
            cp_s = re.sub(r'[\s✓✔]+$', '', str(cp_raw or '')).strip()[:44]
            if x.get('variant_passed'):
                st_lbl = '👑变式已通关'
            elif x.get('corrected_pass') or x.get('resolved'):
                st_lbl = '🎉已订正'
            else:
                st_lbl = '⚠️待自查'
            parts.append(f"• {x.get('no') or '本题'} [{s_cn}·{tp}]：{cp_s or '建议复盘'}（{st_lbl}）")
        rec_t = dg.get('recurring_topics') or []
        if rec_t:
            parts.append('🔁 高频复现易错点：' + ' / '.join(rec_t[:3]))
        return '\n'.join(parts)

    def mobile_brief(self) -> dict:
        st = self.public_state()
        prioritized = [
            x for _, x in sorted(
                enumerate(st['items']),
                key=lambda pair: (
                    0 if (pair[1].get('status') == 'warn' and not pair[1].get('resolved')) else 1,
                    0 if pair[1].get('starred') else 1,
                    -int(pair[1].get('ts') or 0),
                    pair[0],
                ),
            )
        ]
        return {
            'enabled': st['enabled'],
            'phase': st['phase'],
            'subject': st['subject'],
            'pause_mode': st['pause_mode'],
            'pause_sec': st['pause_sec'],
            'camera_connected': bool(self.eng.camera.discover().get('connected')),
            'lighting_warn': st['lighting_warn'],
            'page_epoch': st['page_epoch'],
            'seq': st['seq'],
            'last_note': st['last_note'],
            'ok_count': st['ok_count'],
            'warn_open_count': st['warn_open_count'],
            'warn_resolved_count': st['warn_resolved_count'],
            'total_count': st['total_count'],
            'digest': st['digest'],
            'study_brief': st['study_brief'],
            'recent_sessions': st['recent_sessions'][:3],
            'recent_items': [
                {
                    'id': x['id'],
                    'no': x['no'],
                    'subject': x['subject'],
                    'status': x['status'],
                    'topic': x['topic'],
                    'step_where': x['step_where'],
                    'checkpoint': x['checkpoint'],
                    'deep_hint': x['deep_hint'],
                    'formula_tip': x.get('formula_tip') or '',
                    'warn_step_where': x.get('warn_step_where') or '',
                    'warn_checkpoint': x.get('warn_checkpoint') or '',
                    'warn_deep_hint': x.get('warn_deep_hint') or '',
                    'warn_formula_tip': x.get('warn_formula_tip') or '',
                    'has_snap': bool(x.get('snap_url')),
                    'resolved': x['resolved'],
                    'corrected_pass': x['corrected_pass'],
                    'starred': bool(x.get('starred')),
                    'blackboard_solved': bool(x.get('blackboard_solved')),
                    'variant_passed': bool(x.get('variant_passed')),
                    'mistake_graduated': bool(x.get('mistake_graduated')),
                }
                for x in prioritized[:8]
            ],
        }

    def public_state(self) -> dict:
        self._ensure_loaded()
        grad_mids = self._graduated_mistake_ids()
        ok_cnt = sum(1 for x in self.items if x.get('status') == 'ok')
        warn_open = sum(1 for x in self.items if x.get('status') == 'warn' and not x.get('resolved'))
        warn_resolved = sum(1 for x in self.items if x.get('status') == 'warn' and x.get('resolved'))
        out_items = []
        for x in self.items[:self.MAX_BOARD_ITEMS]:
            sn = x.get('snap_name') or ''
            mid_x = str(x.get('mistake_id') or '').strip()
            out_items.append({
                'id': x['id'],
                'no': x['no'],
                'page_epoch': int(x.get('page_epoch') or 1),
                'status': x['status'],
                'subject': x.get('subject') or 'math',
                'topic': x.get('topic') or '',
                'step_where': x.get('step_where') or '',
                'checkpoint': x.get('checkpoint') or '',
                'deep_hint': x.get('deep_hint') or '',
                'formula_tip': x.get('formula_tip') or '',
                'warn_step_where': x.get('warn_step_where') or '',
                'warn_checkpoint': x.get('warn_checkpoint') or '',
                'warn_deep_hint': x.get('warn_deep_hint') or '',
                'warn_formula_tip': x.get('warn_formula_tip') or '',
                'snap_url': f'/api/tutor/snap/{sn}' if sn else '',
                'ts': int(x.get('ts') or 0),
                'resolved': bool(x.get('resolved')),
                'corrected_pass': bool(x.get('corrected_pass')),
                'starred': bool(x.get('starred')),
                'enrolled_mistake': bool(x.get('enrolled_mistake')),
                'mistake_id': mid_x,
                'blackboard_solved': bool(x.get('blackboard_solved')),
                'variant_passed': bool(x.get('variant_passed')),
                'mistake_graduated': self._is_item_graduated(x, grad_mids),
            })
        return {
            'enabled': bool(self.enabled),
            'phase': self.phase if self.enabled else ('reviewing' if self._pipeline_lock.locked() else 'off'),
            'subject': self.subject,
            'pause_mode': self.pause_mode,
            'pause_sec': self.PAUSE_MODE_MAP.get(self.pause_mode, 3.8),
            'pencil_boost': bool(self.pencil_boost),
            'rotate_hint': int(self.rotate_hint),
            'lighting_warn': str(self.lighting_warn or ''),
            'page_epoch': self.page_epoch,
            'active_stage_page_id': self._last_stage_page_id,
            'seq': self.seq,
            'started_at': self.started_at if self.enabled else 0,
            'last_check_ts': self.last_check_ts,
            'last_note': self.last_note,
            'gate_engine': self.gate_engine,
            'ok_count': ok_cnt,
            'warn_open_count': warn_open,
            'warn_resolved_count': warn_resolved,
            'total_count': len(out_items),
            'digest': self.build_digest(),
            'study_brief': self.build_study_brief(),
            'recent_sessions': list(self.recent_sessions[:7]),
            'stats': dict(self.stats),
            'items': out_items,
        }




def _dictation_state_path() -> Path:
    if HIST_PATH.parent != Path('/var/lib/thinksmart-tutor'):
        return HIST_PATH.parent / 'dictation_state.json'
    return STATE_DIR / 'dictation_state.json'


def _dictation_snap_dir() -> Path:
    if HIST_PATH.parent != Path('/var/lib/thinksmart-tutor') and SNAP_DIR == Path('/var/lib/thinksmart-tutor/snaps'):
        return HIST_PATH.parent / 'snaps'
    return SNAP_DIR


PRESET_DICTATION_PACKS = {
    'elem_zh': {
        'title': '小学语文高频易错词',
        'items': [
            {'word': '光合作用', 'hint': '植物利用阳光制造养分的过程'},
            {'word': '蜿蜒', 'hint': '形容山脉或小路弯弯曲曲延伸'},
            {'word': '懊恼', 'hint': '做错事后心里悔恨、烦恼'},
            {'word': '羡慕', 'hint': '看见别人有长处而希望自己也有'},
            {'word': '叮嘱', 'hint': '再三嘱咐、交代'},
            {'word': '朦胧', 'hint': '月光或视线模糊不清的样子'},
            {'word': '窟窿', 'hint': '物体上的洞或孔'},
            {'word': '敏捷', 'hint': '动作或思维迅速而灵敏'},
            {'word': '黎明', 'hint': '天快要亮或刚亮的时候'},
            {'word': '威武', 'hint': '气势强大、庄严有力'},
        ],
    },
    'elem_en': {
        'title': '小学核心英语词汇',
        'items': [
            {'word': 'library', 'hint': 'n. 图书馆'},
            {'word': 'Wednesday', 'hint': 'n. 星期三（注意首字母大写）'},
            {'word': 'vegetable', 'hint': 'n. 蔬菜'},
            {'word': 'umbrella', 'hint': 'n. 雨伞'},
            {'word': 'different', 'hint': 'adj. 不同的'},
            {'word': 'hospital', 'hint': 'n. 医院'},
            {'word': 'exercise', 'hint': 'v./n. 锻炼；练习'},
            {'word': 'careful', 'hint': 'adj. 仔细的；小心的'},
            {'word': 'favourite', 'hint': 'adj. 最喜爱的'},
            {'word': 'take care of', 'hint': 'phr. 照顾；爱护'},
        ],
    },
    'junior_zh': {
        'title': '初中语文中考易错词',
        'items': [
            {'word': '锲而不舍', 'hint': '不停地镂刻，比喻做事有恒心'},
            {'word': '人迹罕至', 'hint': '偏僻荒凉、很少有人到的地方'},
            {'word': '妇孺皆知', 'hint': '妇女和小孩子都知道，形容众所周知'},
            {'word': '鞠躬尽瘁', 'hint': '恭敬谨慎，竭尽心力去效劳'},
            {'word': '惟妙惟肖', 'hint': '描写或模仿得非常逼真传神'},
            {'word': '因地制宜', 'hint': '根据各地具体情况制定合适措施'},
            {'word': '销声匿迹', 'hint': '不再公开讲话，不再出头露面'},
            {'word': '锋芒毕露', 'hint': '锐气和才华全部显露出来'},
            {'word': '抑扬顿挫', 'hint': '声音高低起伏、停顿转折很有节奏'},
            {'word': '相得益彰', 'hint': '互相配合映衬，双方长处更显著'},
        ],
    },
    'junior_en': {
        'title': '九年级核心高频英语',
        'items': [
            {'word': 'environment', 'hint': 'n. 自然环境；生态环境'},
            {'word': 'pronunciation', 'hint': 'n. 发音；读音'},
            {'word': 'knowledge', 'hint': 'n. 知识；学问'},
            {'word': 'convenient', 'hint': 'adj. 便利的；方便的'},
            {'word': 'responsible', 'hint': 'adj. 有责任心的；负责的'},
            {'word': 'government', 'hint': 'n. 政府'},
            {'word': 'temperature', 'hint': 'n. 温度；体温'},
            {'word': 'achievement', 'hint': 'n. 成就；成绩'},
            {'word': 'communicate', 'hint': 'v. 交流；沟通'},
            {'word': 'pay attention to', 'hint': 'phr. 注意；关注'},
        ],
    },
}


def _detect_dict_lang(word: str) -> str:
    return 'en' if re.search(r'[A-Za-z]{2,}', str(word or '')) else 'zh'


def _scrub_dict_hint(word: str, hint: str) -> str:
    w = str(word or '').strip()
    h = re.sub(r'\s+', ' ', str(hint or '')).strip()
    if not h or not w:
        return ''
    if len(w) >= 2:
        h = re.sub(re.escape(w), '___', h, flags=re.I)
    return h[:56].strip()


def _mask_dict_word(word: str, lang: str) -> tuple:
    w = str(word or '').strip()
    if lang == 'en':
        parts = [p for p in re.split(r'\s+', w) if p]
        letter_cnt = sum(sum(1 for ch in p if ch.isalpha()) for p in parts) or len(w)
        if len(parts) > 1:
            mask = '   '.join(' '.join('_' for _ in range(min(12, len(p)))) for p in parts[:5])
        else:
            mask = ' '.join('_' for _ in range(min(14, max(3, letter_cnt))))
        return letter_cnt, mask
    ch_cnt = len(w)
    mask = ' '.join('□' for _ in range(min(12, max(1, ch_cnt))))
    return ch_cnt, mask


def parse_dictation_items(raw_text: str, max_items: int = 36) -> list:
    txt = str(raw_text or '').strip()
    if not txt:
        return []
    txt = re.sub(
        r'^(?:今天|今日|本周|今晚|语文|英语|请|帮我|开始|来|进行)*(?:自助)?(?:听写|默写|报听写|报词)(?:作业|任务|词语|单词|词表|内容|如下)?[：:\s]*',
        '',
        txt,
        count=1,
    ).strip()
    if not txt:
        return []
    # Normalize bullets/numbering at line starts or after delimiters
    lines = re.split(r'[\r\n；;]+', txt)
    raw_tokens = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        # Remove leading category labels like "语文：" or "英语单词："
        ln = re.sub(r'^(?:语文|英语|生词|词语|单词|短语|古诗|默写)[：:]\s*', '', ln)
        # Split on Chinese 顿号 or commas outside parentheses
        segs = re.split(r'[、，,]+(?![^（(]*[）)])', ln)
        for seg in segs:
            s = seg.strip()
            if not s:
                continue
            s = re.sub(r'^(?:\(?\d{1,2}\)?[\.、）\)\s]+|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮]\s*|[-*•]\s*)', '', s).strip()
            if not s:
                continue
            # Pure Chinese space-separated words (e.g. "光合作用 蜿蜒 懊恼")
            if not re.search(r'[A-Za-z]', s) and not re.search(r'[:：=—]', s) and re.search(r'\s', s):
                for sub in re.split(r'\s+(?![^（(]*[）)])', s):
                    if sub.strip():
                        raw_tokens.append(sub.strip())
            else:
                raw_tokens.append(s)

    items = []
    seen = set()
    for tok in raw_tokens:
        word, hint = '', ''
        # Case 1: word (hint) or word（hint）
        m_par = re.match(r'^(.+?)[（(]([^（）()]{1,56})[）)]\s*$', tok)
        if m_par:
            word, hint = m_par.group(1).strip(), m_par.group(2).strip()
        else:
            # Case 2: word : hint  or  word — hint  or  word = hint (avoid breaking hyphenated English like well-known)
            m_sep = re.match(r'^(.+?)(?:\s*[：:=—]\s*|\s+-\s+)(.+)$', tok)
            if m_sep:
                word, hint = m_sep.group(1).strip(), m_sep.group(2).strip()
            else:
                # Case 3: English word/phrase followed by Chinese gloss ("take care of 照顾" or "environment n. 环境")
                m_en_zh = re.match(
                    r"^([A-Za-z][A-Za-z0-9'\-\s]{0,42}?)(?:\s+((?:(?:n|v|adj|adv|prep|phr|vt|vi)\.\s*)?[\u4e00-\u9fff][^A-Za-z]{0,40}))$",
                    tok,
                )
                if m_en_zh:
                    word, hint = m_en_zh.group(1).strip(), m_en_zh.group(2).strip()
                else:
                    word = tok.strip()
        word = re.sub(r'\s+', ' ', word).strip(' "“”‘’。；;，,、')
        if not word or len(word) > 48:
            continue
        key = word.lower()
        if key in seen:
            continue
        seen.add(key)
        lang = _detect_dict_lang(word)
        items.append({
            'word': word,
            'hint': _scrub_dict_hint(word, hint),
            'lang': lang,
        })
        if len(items) >= max_items:
            break
    return items


def _prep_dictation_jpeg_sync(raw_bytes: bytes, max_side: int = 1440, pencil_boost: bool = False) -> bytes:
    if not raw_bytes or len(raw_bytes) < 128:
        raise ValueError('图片数据不完整，请重新拍照')
    if _HAS_PIL:
        try:
            with _PILImage.open(io.BytesIO(raw_bytes)) as im:
                im.load()
                im = _PILImageOps.exif_transpose(im)
                if im.mode != 'RGB':
                    im = im.convert('RGB')
                w, h = im.size
                if w < 32 or h < 32:
                    raise ValueError('图片尺寸过小')
                if max(w, h) > max_side:
                    scale = max_side / float(max(w, h))
                    im = im.resize((max(1, int(round(w * scale))), max(1, int(round(h * scale)))), _PILImage.Resampling.LANCZOS)
                if pencil_boost:
                    im = _PILImageOps.autocontrast(im, cutoff=1)
                    im = _PILImageEnhance.Contrast(im).enhance(1.32)
                    im = _PILImageEnhance.Sharpness(im).enhance(1.35)
                buf = io.BytesIO()
                im.save(buf, format='JPEG', quality=86, optimize=True)
                return buf.getvalue()
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError('图片格式无法识别，请上传清晰的 JPG/PNG 照片') from exc
    if raw_bytes[:3] == b'\xff\xd8\xff' or raw_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return raw_bytes
    raise ValueError('图片格式无法识别')


class DictationManager:
    """Self-service Chinese/English dictation player + photo sheet grader + mistake-book closed loop."""

    def __init__(self, eng: 'TutorEngine'):
        self.eng = eng
        self._loaded_path = None
        self.seq = 0
        self.session_id = ''
        self.title = ''
        self.phase = 'idle'  # idle | ready | graded
        self.items = []
        self.current_idx = 0
        self.interval_sec = 8
        self.repeat_count = 2
        self.en_mode = 'both'  # both | en_only | zh_prompt
        self.revealed = False
        self.summary = ''
        self.snap_name = ''
        self.created_at = 0
        self.updated_at = 0
        self.graded_at = 0
        self._last_touch_mono = 0.0
        self._grading_lock = asyncio.Lock()
        self._extracting = 0

    def _norm_interval(self, val) -> int:
        try:
            iv = int(val)
        except (TypeError, ValueError):
            return self.interval_sec if self.interval_sec in (0, 5, 8, 12, 15) else 8
        return iv if iv in (0, 5, 8, 12, 15) else max(0, min(25, iv))

    def _norm_repeat(self, val) -> int:
        try:
            rv = int(val)
        except (TypeError, ValueError):
            return self.repeat_count if self.repeat_count in (1, 2, 3) else 2
        return max(1, min(3, rv))

    def _norm_en_mode(self, val) -> str:
        s = str(val or '').strip().lower()
        return s if s in ('both', 'en_only', 'zh_prompt') else (self.en_mode if self.en_mode in ('both', 'en_only', 'zh_prompt') else 'both')

    def _ensure_loaded(self):
        sp = _dictation_state_path()
        if self._loaded_path == sp:
            return
        self._loaded_path = sp
        raw = _read_safe_json(sp, None)
        if not isinstance(raw, dict) or not isinstance(raw.get('items'), list):
            return
        clean_items = []
        for it in raw.get('items', [])[:36]:
            if not isinstance(it, dict):
                continue
            w = str(it.get('word') or '').strip()[:48]
            if not w:
                continue
            lang = it.get('lang') if it.get('lang') in ('zh', 'en') else _detect_dict_lang(w)
            gst = str(it.get('grade_status') or '').strip()
            clean_items.append({
                'word': w,
                'hint': _scrub_dict_hint(w, it.get('hint') or ''),
                'lang': lang,
                'grade_status': gst if gst in ('ok', 'wrong', 'missing') else '',
                'written': str(it.get('written') or '').strip()[:64],
                'grade_note': str(it.get('grade_note') or '').strip()[:96],
                'enrolled_mistake': bool(it.get('enrolled_mistake')),
                'mistake_id': str(it.get('mistake_id') or '').strip()[:28],
            })
        if not clean_items:
            return
        self.session_id = str(raw.get('session_id') or f'dc_{int(time.time())}')[:32]
        self.title = str(raw.get('title') or '自助听写')[:36]
        ph = str(raw.get('phase') or 'ready')
        self.phase = ph if ph in ('ready', 'graded') else 'ready'
        self.items = clean_items
        self.current_idx = max(0, min(len(clean_items) - 1, int(raw.get('current_idx') or 0)))
        self.interval_sec = self._norm_interval(raw.get('interval_sec', 8))
        self.repeat_count = self._norm_repeat(raw.get('repeat_count', 2))
        self.en_mode = self._norm_en_mode(raw.get('en_mode', 'both'))
        self.revealed = bool(raw.get('revealed')) or (self.phase == 'graded')
        self.summary = str(raw.get('summary') or '')[:160]
        self.snap_name = str(raw.get('snap_name') or '')[:40]
        self.created_at = int(raw.get('created_at') or 0)
        self.updated_at = int(raw.get('updated_at') or 0)
        self.graded_at = int(raw.get('graded_at') or 0)
        self.seq = max(1, int(raw.get('seq') or 1))

    def _save_state(self):
        sp = _dictation_state_path()
        self._loaded_path = sp
        self.updated_at = int(time.time())
        self._last_touch_mono = time.monotonic()
        self.seq += 1
        payload = {
            'seq': self.seq,
            'session_id': self.session_id,
            'title': self.title,
            'phase': self.phase,
            'current_idx': self.current_idx,
            'interval_sec': self.interval_sec,
            'repeat_count': self.repeat_count,
            'en_mode': self.en_mode,
            'revealed': self.revealed,
            'summary': self.summary,
            'snap_name': self.snap_name,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'graded_at': self.graded_at,
            'items': self.items,
        }
        with contextlib.suppress(Exception):
            _atomic_write_json(sp, payload)

    def has_items(self) -> bool:
        self._ensure_loaded()
        return len(self.items) > 0

    def is_busy(self) -> bool:
        if self._grading_lock.locked() or getattr(self, '_extracting', 0) > 0:
            return True
        if self.phase == 'ready' and self.items and self._last_touch_mono > 0:
            if (time.monotonic() - self._last_touch_mono) < 1800.0:
                return True
        return False

    def _install_session(self, raw_items: list, title: str, interval_sec=None, repeat_count=None, en_mode=None):
        now_ts = int(time.time())
        self.session_id = f'dc_{now_ts}_{os.urandom(3).hex()}'
        self.title = str(title or '中英文自助听写').strip()[:36]
        self.phase = 'ready'
        self.items = [
            {
                'word': str(x['word']).strip()[:48],
                'hint': _scrub_dict_hint(x['word'], x.get('hint') or ''),
                'lang': x.get('lang') if x.get('lang') in ('zh', 'en') else _detect_dict_lang(x['word']),
                'grade_status': '',
                'written': '',
                'grade_note': '',
                'enrolled_mistake': False,
                'mistake_id': str(x.get('mistake_id') or '')[:28],
            }
            for x in raw_items[:36]
            if str(x.get('word') or '').strip()
        ]
        self.current_idx = 0
        if interval_sec is not None:
            self.interval_sec = self._norm_interval(interval_sec)
        if repeat_count is not None:
            self.repeat_count = self._norm_repeat(repeat_count)
        if en_mode is not None:
            self.en_mode = self._norm_en_mode(en_mode)
        self.revealed = False
        self.summary = ''
        self.snap_name = ''
        self.created_at = now_ts
        self.graded_at = 0
        self._save_state()
        self._schedule_prefetch(0)

    def spoken_text_for(self, idx: int, pass_no: int = 1) -> str:
        self._ensure_loaded()
        if not (0 <= idx < len(self.items)):
            raise LookupError('未找到该听写词条')
        it = self.items[idx]
        word = it['word']
        hint = it.get('hint') or ''
        lang = it.get('lang') or 'zh'
        no = idx + 1
        p = max(1, int(pass_no or 1))
        if lang == 'en':
            if self.phase != 'graded' and not self.revealed and self.en_mode == 'zh_prompt' and hint:
                return f'第 {no} 个，请默写英文：{hint}' if p == 1 else f'再来一遍，{hint}'
            if self.en_mode == 'both' and hint:
                return f'第 {no} 个，{word}。{hint}' if p == 1 else f'再来一遍，{word}'
            return f'第 {no} 个，{word}' if p == 1 else f'再来一遍，{word}'
        # Chinese: if single character and hint exists, include brief disambiguating hint on pass 1
        if len(word) == 1 and hint and p == 1:
            return f'第 {no} 个，{word}，{hint}'
        return f'第 {no} 个，{word}' if p == 1 else f'再来一遍，{word}'

    def _schedule_prefetch(self, start_idx: int = 0):
        if not self.eng.tts_http or not self.items:
            return
        texts = []
        for i in range(max(0, start_idx), min(len(self.items), start_idx + 2)):
            with contextlib.suppress(Exception):
                texts.append(self.spoken_text_for(i, 1))
                if self.repeat_count >= 2:
                    texts.append(self.spoken_text_for(i, 2))
        if not texts:
            return

        async def _pf():
            for t in texts:
                with contextlib.suppress(Exception):
                    await self.eng.synthesize_wav(t, priority=False)

        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop()
            self.eng._spawn(_pf(), session_work=False)

    def start_from_text(self, raw_text: str, title: str = '', interval_sec=None, repeat_count=None, en_mode=None) -> dict:
        self._ensure_loaded()
        parsed = parse_dictation_items(raw_text)
        if not parsed:
            raise ValueError('未能识别出有效的听写字词，请用空格、逗号或换行分隔词语')
        en_cnt = sum(1 for x in parsed if x['lang'] == 'en')
        auto_title = title or (
            f'英语听写（{len(parsed)}词）' if en_cnt == len(parsed)
            else (f'中英混合听写（{len(parsed)}词）' if en_cnt > 0 else f'语文听写（{len(parsed)}词）')
        )
        self._install_session(parsed, auto_title, interval_sec=interval_sec, repeat_count=repeat_count, en_mode=en_mode)
        return self.public_state()

    def start_from_pack(self, pack_id: str, interval_sec=None, repeat_count=None, en_mode=None) -> dict:
        self._ensure_loaded()
        pid = str(pack_id or '').strip()
        if pid == 'mistakes':
            self.eng._ensure_mistakes_loaded()
            cand = []
            for m in self.eng.mistakes:
                if m.get('graduated'):
                    continue
                if str(m.get('error_tag') or '').startswith('听写·') and m.get('dict_word'):
                    w = str(m.get('dict_word') or '').strip()
                    if w:
                        cand.append({'word': w, 'hint': str(m.get('dict_hint') or ''), 'mistake_id': m.get('id', '')})
            if not cand:
                raise LookupError('错题本里暂时还没有待巩固的听写易错词，可先选上方词表或拍课本词表开始哦')
            self._install_session(cand[:20], f'错题本易错词抽查（{len(cand[:20])}词）', interval_sec=interval_sec, repeat_count=repeat_count, en_mode=en_mode)
            return self.public_state()
        pack = PRESET_DICTATION_PACKS.get(pid)
        if not pack:
            raise LookupError('未找到该内置听写词库')
        self._install_session(copy.deepcopy(pack['items']), pack['title'], interval_sec=interval_sec, repeat_count=repeat_count, en_mode=en_mode)
        return self.public_state()

    async def extract_and_start(self, image_bytes: bytes = None, use_camera: bool = False, title: str = '',
                                interval_sec=None, repeat_count=None, en_mode=None) -> dict:
        self._ensure_loaded()
        self._extracting += 1
        try:
            if use_camera and not image_bytes:
                image_bytes = await self.eng.camera.capture_jpeg(high_res=True, crop_box=None, pencil_boost=False)
            clean_jpg = await asyncio.to_thread(_prep_dictation_jpeg_sync, image_bytes, 1360, False)
            b64 = base64.b64encode(clean_jpg).decode('ascii')
            prompt = (
                '请从这张课本词语表/英语词汇表/老师听写清单照片中提取待听写的词语或英文单词短语（最多提取30个）。\n'
                '规则：\n'
                '1. 若为英文单词表，word 填纯英文单词或短语，hint 填对应词性与中文释义（如 "n. 环境"），hint 中严禁包含英文原词！\n'
                '2. 若为语文生字词表，word 填要听写的汉字词语，hint 填简短不露原字的意思提示（14字内，严禁出现 word 中的原字！）。\n'
                '3. 忽略页码、单元标题等非词汇杂项。\n'
                '只输出一行合法JSON（不要代码块）：{"title":"8字内词表标题","items":[{"word":"词语或单词","hint":"不剧透原字的释义提示"}]}'
            )
            messages = [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': prompt},
                    {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + b64}},
                ],
            }]
            content = await self.eng._post_chat_once('fast', messages, temperature=0.1, timeout_s=28.0)
            objs, _ = extract_json_objects_from_buffer(content)
            extracted = []
            ext_title = title
            if objs and isinstance(objs[0].get('items'), list):
                if not ext_title:
                    ext_title = str(objs[0].get('title') or '').strip()
                seen = set()
                for raw_it in objs[0]['items'][:36]:
                    if isinstance(raw_it, str):
                        w, h = raw_it.strip(), ''
                    elif isinstance(raw_it, dict):
                        w = str(raw_it.get('word') or '').strip()
                        h = str(raw_it.get('hint') or raw_it.get('meaning') or '').strip()
                    else:
                        continue
                    w = re.sub(r'\s+', ' ', w).strip(' "“”‘’。；;，,、')
                    if not w or len(w) > 48 or w.lower() in seen:
                        continue
                    seen.add(w.lower())
                    extracted.append({'word': w, 'hint': _scrub_dict_hint(w, h), 'lang': _detect_dict_lang(w)})
            if not extracted:
                extracted = parse_dictation_items(content)
            if not extracted:
                raise ValueError('未能从照片中识别出清晰的词语表，请对准课本词表或调亮光线重试')
            self._install_session(
                extracted,
                ext_title or f'拍词表听写（{len(extracted)}词）',
                interval_sec=interval_sec, repeat_count=repeat_count, en_mode=en_mode,
            )
            return self.public_state()
        finally:
            self._extracting = max(0, self._extracting - 1)

    def control(self, action: str, body: dict = None) -> dict:
        self._ensure_loaded()
        b = body if isinstance(body, dict) else {}
        act = str(action or b.get('action') or '').strip().lower()
        if act == 'clear':
            self.phase = 'idle'
            self.items = []
            self.current_idx = 0
            self.revealed = False
            self.summary = ''
            self.snap_name = ''
            self._save_state()
            return self.public_state()
        if not self.items:
            raise LookupError('当前还没有待听写的词表')
        if act == 'step':
            if 'idx' in b and b.get('idx') is not None:
                self.current_idx = max(0, min(len(self.items) - 1, int(b['idx'])))
            else:
                delta = int(b.get('delta') or 0)
                self.current_idx = max(0, min(len(self.items) - 1, self.current_idx + delta))
            self._save_state()
            self._schedule_prefetch(self.current_idx)
            return self.public_state()
        if act == 'config':
            if 'interval_sec' in b:
                self.interval_sec = self._norm_interval(b['interval_sec'])
            if 'repeat_count' in b:
                self.repeat_count = self._norm_repeat(b['repeat_count'])
            if 'en_mode' in b:
                self.en_mode = self._norm_en_mode(b['en_mode'])
            self._save_state()
            self._schedule_prefetch(self.current_idx)
            return self.public_state()
        if act == 'reveal':
            self.revealed = bool(b['revealed']) if 'revealed' in b else (not self.revealed)
            self._save_state()
            return self.public_state()
        if act == 'retry_wrong':
            wrongs = [
                {'word': it['word'], 'hint': it.get('hint') or '', 'lang': it.get('lang') or 'zh', 'mistake_id': it.get('mistake_id') or ''}
                for it in self.items
                if it.get('grade_status') in ('wrong', 'missing')
            ]
            if not wrongs:
                raise ValueError('本次听写全部正确，没有需要重默的错词哦！')
            self._install_session(wrongs, f'错词重默巩固（{len(wrongs)}词）')
            return self.public_state()
        raise ValueError('不支持的听写控制指令')

    def _save_dict_snap(self, jpg_bytes: bytes) -> str:
        sdir = _dictation_snap_dir()
        sdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = f'dc_{int(time.time()):x}_{os.urandom(4).hex()}.jpg'
        _atomic_write_bytes(sdir / name, jpg_bytes, mode=0o600)
        keep = {name}
        if isinstance(getattr(self, 'snap_name', None), str) and self.snap_name:
            keep.add(self.snap_name)
        for it in (getattr(self, 'items', None) or ()):
            if isinstance(it, dict):
                cu = str(it.get('crop_url') or '')
                if 'dc_' in cu:
                    keep.add(cu.rsplit('/', 1)[-1].split('?', 1)[0])
        eng = getattr(self, 'eng', None)
        for m in list(getattr(eng, 'mistakes', None) or ())[:120]:
            if isinstance(m, dict):
                iu = str(m.get('image_url') or '')
                if 'dc_' in iu:
                    keep.add(iu.rsplit('/', 1)[-1].split('?', 1)[0])
        with contextlib.suppress(OSError):
            snaps = sorted(sdir.glob('dc_*.jpg'), key=lambda p: p.stat().st_mtime, reverse=True)
            for old_p in snaps[6:]:
                if old_p.name not in keep:
                    old_p.unlink(missing_ok=True)
        return name

    async def grade_sheet(self, image_bytes: bytes = None, use_camera: bool = False, pencil_boost: bool = True) -> dict:
        self._ensure_loaded()
        if not self.items:
            raise LookupError('请先生成或投送一份听写词表，再拍照批改')
        async with self._grading_lock:
            my_sid = self.session_id
            items_len = len(self.items)
            if items_len == 0:
                raise LookupError('请先生成或投送一份听写词表，再拍照批改')
            if use_camera and not image_bytes:
                image_bytes = await self.eng.camera.capture_jpeg(high_res=True, crop_box=None, pencil_boost=pencil_boost)
            clean_jpg = await asyncio.to_thread(_prep_dictation_jpeg_sync, image_bytes, 1480, pencil_boost)
            snap_name = await asyncio.to_thread(self._save_dict_snap, clean_jpg)
            b64 = base64.b64encode(clean_jpg).decode('ascii')
            target_lines = '\n'.join(f"{i + 1}. {it['word']}" + (f"（{it['hint']}）" if it.get('hint') else '') for i, it in enumerate(self.items))
            prompt = (
                '你是一位严谨又温柔的中小学语文/英语听写批改老师。下面是本次听写的【标准答案顺序表】：\n'
                f'{target_lines}\n\n'
                '请仔细核对照片中学生手写的听写本内容（学生可能横排、竖排、分两列书写，也可能涂改后在旁边重写，请以未划掉的最终手写为准）：\n'
                '1. 逐项核对汉字笔画偏旁（注意同音别字、多一笔少一笔）及英文字母拼写、大小写与词组空格。\n'
                '2. 每个序号返回 status："ok"（完全正确）、"wrong"（有错别字或拼写错误）、"missing"（未写或漏写）。\n'
                '3. written 填学生实际写出的字词（漏写填空串 ""）；若 status 为 "wrong"，note 用 24 字内精准指出错在哪里（例如："把『锲』左边的金字旁漏写了" 或 "environment 中间漏了字母 n"）；全对时 note 填空串 ""。\n'
                '4. summary 用一句 45 字内温暖鼓励的话总结整体表现与字迹工整度。\n'
                '只输出一行合法JSON（不要代码块）：{"summary":"总结评语","items":[{"idx":1,"status":"ok","written":"...","note":""}]}'
            )
            messages = [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': prompt},
                    {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + b64}},
                ],
            }]
            content = await self.eng._post_chat_once('fast', messages, temperature=0.1, timeout_s=34.0)
            if self.session_id != my_sid or len(self.items) != items_len:
                raise RuntimeError('听写词表已更新，本次旧批改已跳过')
            objs, _ = extract_json_objects_from_buffer(content)
            if not objs or not isinstance(objs[0].get('items'), list):
                raise RuntimeError('听写照片识别未返回有效结果，请将听写本放平、对焦清晰后重试')
            obj = objs[0]
            by_idx = {}
            for g in obj.get('items', []):
                if not isinstance(g, dict):
                    continue
                try:
                    ix = int(g.get('idx'))
                except (TypeError, ValueError):
                    continue
                if 1 <= ix <= len(self.items):
                    by_idx[ix] = g

            now_ts = int(time.time())
            for i, it in enumerate(self.items):
                g = by_idx.get(i + 1) or {}
                st_raw = str(g.get('status') or '').strip().lower()
                wr = str(g.get('written') or '').strip()[:64]
                nt = str(g.get('note') or '').strip()[:96]
                if st_raw not in ('ok', 'wrong', 'missing'):
                    if not wr:
                        st_raw = 'missing'
                    elif wr.lower() == it['word'].lower():
                        st_raw = 'ok'
                    else:
                        st_raw = 'wrong'
                if st_raw == 'ok':
                    nt = ''
                elif st_raw == 'missing' and not nt:
                    nt = '本项漏写，请补默巩固'
                elif st_raw == 'wrong' and not nt:
                    nt = f"误写为「{wr}」，请对照标准字形/拼写订正" if wr else '拼写或笔画有误，请订正'
                it['grade_status'] = st_raw
                it['written'] = wr
                it['grade_note'] = nt
                # Auto-advance mistake graduation if previously enrolled dictation mistake was written 100% right!
                if st_raw == 'ok' and it.get('mistake_id'):
                    self.eng._ensure_mistakes_loaded()
                    m_hit = next((m for m in self.eng.mistakes if m.get('id') == it['mistake_id']), None)
                    if m_hit and not m_hit.get('graduated'):
                        if int(m_hit.get('stage') or 0) <= 0:
                            m_hit['stage'] = 1
                            m_hit['retest_pass_count'] = 1
                            m_hit['updated_at'] = now_ts
                            m_hit['next_review_at'] = now_ts + 3 * 86400
                        else:
                            m_hit['stage'] = 2
                            m_hit['retest_pass_count'] = int(m_hit.get('retest_pass_count') or 1) + 1
                            m_hit['graduated'] = True
                            m_hit['updated_at'] = now_ts
                            m_hit['next_review_at'] = 0
                        self.eng._save_mistakes()

            ok_c = sum(1 for x in self.items if x.get('grade_status') == 'ok')
            wr_c = sum(1 for x in self.items if x.get('grade_status') == 'wrong')
            ms_c = sum(1 for x in self.items if x.get('grade_status') == 'missing')
            def_sum = (
                f'太棒啦！全部 {len(self.items)} 个字词一次全对，书写满分！'
                if ok_c == len(self.items) else
                f'共 {len(self.items)} 个词，写对 {ok_c} 个，有 {wr_c + ms_c} 个需要重点订正巩固哦！'
            )
            self.summary = str(obj.get('summary') or def_sum).strip()[:160] or def_sum
            self.snap_name = snap_name
            self.phase = 'graded'
            self.revealed = True
            self.graded_at = now_ts
            self._save_state()
            return self.public_state()

    def enroll_mistakes(self, idx: int = None) -> dict:
        self._ensure_loaded()
        self.eng._ensure_mistakes_loaded()
        if idx is not None:
            if not (0 <= idx < len(self.items)):
                raise LookupError('未找到该听写词条')
            targets = [self.items[idx]]
        else:
            targets = [it for it in self.items if it.get('grade_status') in ('wrong', 'missing')]
        if not targets:
            return {
                'ok': True,
                'enrolled_count': 0,
                'enrolled_ids': [],
                'dictation': self.public_state(),
                'mistake_summary': self.eng.public_mistake_summary(),
            }

        now_ts = int(time.time())
        enrolled_cnt = 0
        enrolled_ids = []
        snap_u = f'/api/tutor/snap/{self.snap_name}' if self.snap_name else ''
        for it in targets:
            word = it['word']
            lang = it.get('lang') or _detect_dict_lang(word)
            hint = it.get('hint') or ('核心高频英语词汇' if lang == 'en' else '重点易错字词')
            ch_len, mask = _mask_dict_word(word, lang)
            wr = it.get('written') or ''
            gn_raw = it.get('grade_note') or ('注意字母组合拼写' if lang == 'en' else '注意易错偏旁与笔画')
            gn_safe = str(gn_raw)
            if lang == 'en':
                if word:
                    gn_safe = re.sub(re.escape(word), '该词', gn_safe, flags=re.I)
                    for tok_w in re.findall(r'[A-Za-z]{4,}', word):
                        gn_safe = re.sub(rf'\b{re.escape(tok_w)}\b', '该词', gn_safe, flags=re.I)
            else:
                if word:
                    gn_safe = gn_safe.replace(word, '该词')
                    for ch_w in word:
                        if '\u4e00' <= ch_w <= '\u9fff' and ch_w not in wr:
                            gn_safe = gn_safe.replace(ch_w, '某字')
            mid = 'mb_' + hashlib.sha256(f"dict|{lang}|{word.lower()}".encode('utf-8')).hexdigest()[:10]
            existing = next((m for m in self.eng.mistakes if isinstance(m, dict) and m.get('id') == mid), None)
            if lang == 'en':
                q_stem = f"【英语词汇默写】请根据释义「{hint}」准确默写英文单词或短语（共 {ch_len} 个字母：{mask}；上次易错点：{gn_safe}）。"
                subj = 'english'
            else:
                q_stem = f"【语文字词默写】请根据词义提示「{hint}」准确书写该 {ch_len} 字词语（上次易错点：{gn_safe}）。"
                subj = 'chinese'
            steps = [
                {
                    'step': 1,
                    'title': '错因对比 · 拆解记忆',
                    'say': f"我们先把易错处记牢：上次{('写成了' + wr) if wr else '漏写了'}，{gn_safe}。",
                    'board': f"【释义线索】{hint}\n【上次书写】{wr or '（漏写）'}\n【订正要点】{gn_safe}",
                    'quiz': None,
                    'draw': [],
                },
                {
                    'step': 2,
                    'title': '闭卷默写通关',
                    'say': f"现在合上本子，根据释义「{hint}」把正确的字词输入或默写出来吧！",
                    'board': f"【闭卷检验】释义：{hint}\n结构提示：{mask}",
                    'quiz': {
                        'mode': 'input',
                        'q': f'请写出「{hint}」对应的正确词语/英文：',
                        'ans': word,
                        'tip': f"{gn_safe}（共 {ch_len} {'个字母' if lang == 'en' else '个字'}：{mask}）",
                    },
                    'draw': [],
                },
            ]
            safe_title_hint = hint[:12] if (hint and word not in hint) else mask[:12]
            entry = {
                'id': mid,
                'orig_sid': f'm:{mid}',
                'title': f"听写·{safe_title_hint}"[:32],
                'question': q_stem[:600],
                'dict_word': word,
                'dict_hint': it.get('hint') or '',
                'subject': subj,
                'pedagogy': 'auto',
                'mode': 'fast',
                'teach_mode': 'diagnose_draft',
                'weak_step': 2,
                'weak_focus': gn_safe[:64],
                'error_tag': '听写·英语拼写' if lang == 'en' else '听写·易错字词',
                'stage': 0,
                'graduated': False,
                'retest_pass_count': int(existing.get('retest_pass_count') or 0) if existing else 0,
                'created_at': int(existing.get('created_at') or now_ts) if existing else now_ts,
                'updated_at': now_ts,
                'next_review_at': now_ts + 86400,
                'image_url': snap_u or (str(existing.get('image_url') or '') if existing else ''),
                'steps': steps,
            }
            self.eng.mistakes = [m for m in self.eng.mistakes if m.get('id') != mid]
            self.eng.mistakes.insert(0, entry)
            it['enrolled_mistake'] = True
            it['mistake_id'] = mid
            enrolled_cnt += 1
            enrolled_ids.append(mid)
        if enrolled_cnt > 0:
            self.eng._save_mistakes()
            self._save_state()
        return {
            'ok': True,
            'enrolled_count': enrolled_cnt,
            'enrolled_ids': enrolled_ids,
            'mistake_summary': self.eng.public_mistake_summary(),
            'dictation': self.public_state(),
        }

    def public_state(self) -> dict:
        self._ensure_loaded()
        show_ans = bool(self.revealed or self.phase == 'graded')
        pub_items = []
        ok_c = wr_c = ms_c = 0
        for i, it in enumerate(self.items):
            lang = it.get('lang') or 'zh'
            ch_len, mask = _mask_dict_word(it['word'], lang)
            gs = it.get('grade_status') or ''
            if gs == 'ok':
                ok_c += 1
            elif gs == 'wrong':
                wr_c += 1
            elif gs == 'missing':
                ms_c += 1
            pub_items.append({
                'idx': i + 1,
                'lang': lang,
                'char_len': ch_len,
                'mask': mask,
                'hint': it.get('hint') or '',
                'word': it['word'] if show_ans else '',
                'grade_status': gs,
                'written': it.get('written') or '' if self.phase == 'graded' else '',
                'grade_note': it.get('grade_note') or '' if self.phase == 'graded' else '',
                'enrolled_mistake': bool(it.get('enrolled_mistake')),
                'mistake_id': it.get('mistake_id') or '',
            })
        tot = len(pub_items)
        acc = int(round(ok_c * 100.0 / tot)) if (tot > 0 and self.phase == 'graded') else 0
        return {
            'seq': self.seq,
            'session_id': self.session_id,
            'title': self.title,
            'phase': self.phase if tot > 0 else 'idle',
            'total_count': tot,
            'current_idx': self.current_idx if tot > 0 else 0,
            'interval_sec': self.interval_sec,
            'repeat_count': self.repeat_count,
            'en_mode': self.en_mode,
            'revealed': show_ans,
            'summary': self.summary,
            'snap_url': f'/api/tutor/snap/{self.snap_name}' if self.snap_name else '',
            'ok_count': ok_c,
            'wrong_count': wr_c,
            'missing_count': ms_c,
            'accuracy_pct': acc,
            'grading': self._grading_lock.locked(),
            'packs': [
                {'id': k, 'title': v['title'], 'count': len(v['items'])}
                for k, v in PRESET_DICTATION_PACKS.items()
            ],
            'items': pub_items,
        }

    def mobile_brief(self) -> dict:
        st = self.public_state()
        wrong_words = [
            {'idx': i + 1, 'word': it['word'], 'written': it.get('written') or '', 'note': it.get('grade_note') or '', 'status': it.get('grade_status')}
            for i, it in enumerate(self.items)
            if it.get('grade_status') in ('wrong', 'missing')
        ]
        return {
            'seq': st['seq'],
            'session_id': st['session_id'],
            'title': st['title'],
            'phase': st['phase'],
            'total_count': st['total_count'],
            'current_idx': st['current_idx'],
            'interval_sec': st['interval_sec'],
            'repeat_count': st['repeat_count'],
            'en_mode': st['en_mode'],
            'ok_count': st['ok_count'],
            'wrong_count': st['wrong_count'],
            'missing_count': st['missing_count'],
            'accuracy_pct': st['accuracy_pct'],
            'summary': st['summary'],
            'preview_words': [it['word'] for it in self.items[:10]],
            'wrong_items': wrong_words[:16],
        }


class TutorEngine:
    def __init__(self):
        self.camera = UVCCameraManager()
        self.phone_studio = PhoneStudioManager()
        self.recorder = TutorVoiceRecorder()
        self.cowrite = CoWriteMonitor(self)
        self.dictation = DictationManager(self)
        self.phone_studio._on_page_rotated = self.cowrite.on_stage_page_rotated
        self.phone_studio._on_page_removed = self.cowrite.on_stage_page_removed
        self.sessions = collections.OrderedDict()
        self._page_check_inflight = {}
        self.active_session_id = ''
        self.tts_cache = collections.OrderedDict()
        self._tts_inflight = {}
        self._tts_sem = asyncio.Semaphore(1)
        self._tts_busy = False
        self._tts_woken_ev = None
        self._tts_priority_waiters = 0
        self._tts_hi_q = collections.deque()
        self._tts_lo_q = collections.deque()
        self.bg_tasks = set()
        self._session_work_tasks = set()
        self.open_ui_seq = 0
        self._open_ui_seq_floor = self._read_open_ui_seq_floor()
        self.open_ui_target = ''
        self.open_ui_pencil = None
        self._open_ui_mono = 0.0
        self._cam_was_connected = bool(self.camera.discover().get('connected'))
        self._cfg_cache = None
        self._cfg_sig = None
        self._tts_cache_bytes = 0
        self.http: aiohttp.ClientSession = None
        self.tts_http: aiohttp.ClientSession = None
        self.sprints = collections.OrderedDict()
        self.last_active_mono = time.monotonic()
        self._inflight_reqs = 0
        self.parent_nudge_seq = 0
        self.latest_parent_nudge = None
        self._mistakes_loaded_path = None
        self._mistakes_sig = None
        self.mistakes = []
        self._hist_auto_sync = True
        self._hist_loaded_path = None
        self._hist_sig = None
        self.history = []
        self._ensure_history_loaded()
        self._ensure_mistakes_loaded()
        self._ui_ver_cache = (0.0, '')
        with contextlib.suppress(Exception):
            p_init = _read_safe_json(PREFS_PATH, {})
            if isinstance(p_init, dict):
                saved_sid = str(p_init.get('active_session_id') or '').strip()
                if saved_sid and self._get_or_hydrate_session(saved_sid) is not None:
                    self.active_session_id = saved_sid

    @staticmethod
    def _file_stat_sig(p: Path):
        try:
            st = os.lstat(p)
            if stat.S_ISREG(st.st_mode):
                return (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            pass
        return None

    def _ensure_history_loaded(self):
        if not getattr(self, '_hist_auto_sync', False):
            return
        hp = HIST_PATH
        cur_sig = self._file_stat_sig(hp)
        prev_path = getattr(self, '_hist_loaded_path', None)
        if prev_path != hp or (cur_sig is not None and cur_sig != getattr(self, '_hist_sig', None)):
            is_reload = (prev_path == hp)
            raw_hist = _read_safe_json(hp, [])
            if not isinstance(raw_hist, list):
                raw_hist = []
            self.history = _dedupe_history_entries(raw_hist)
            if len(self.history) != len(raw_hist) and cur_sig is not None:
                with contextlib.suppress(Exception):
                    _atomic_write_json(hp, self.history)
                cur_sig = self._file_stat_sig(hp)
            self._hist_loaded_path = hp
            self._hist_sig = cur_sig
            if is_reload and isinstance(getattr(self, 'sessions', None), dict):
                for k in [
                    sid_k for sid_k, s_v in list(self.sessions.items())
                    if not str(sid_k).startswith('m:')
                    and (not isinstance(s_v, dict) or (s_v.get('status') not in ('thinking', 'streaming') and not s_v.get('silent_preview')))
                ]:
                    self.sessions.pop(k, None)

    def _ensure_mistakes_loaded(self):
        mp = _mistake_path()
        cur_sig = self._file_stat_sig(mp)
        prev_path = getattr(self, '_mistakes_loaded_path', None)
        if prev_path != mp or (cur_sig is not None and cur_sig != getattr(self, '_mistakes_sig', None)):
            is_reload = (prev_path == mp)
            raw_m = _read_safe_json(mp, [])
            self.mistakes = [x for x in raw_m if isinstance(x, dict) and x.get('id')] if isinstance(raw_m, list) else []
            self._mistakes_loaded_path = mp
            self._mistakes_sig = cur_sig
            if is_reload and isinstance(getattr(self, 'sessions', None), dict):
                for k in [sid_k for sid_k in list(self.sessions.keys()) if str(sid_k).startswith('m:')]:
                    self.sessions.pop(k, None)

    def _save_mistakes(self):
        self._ensure_mistakes_loaded()
        self.mistakes = self.mistakes[:80]
        mp = _mistake_path()
        with contextlib.suppress(Exception):
            _atomic_write_json(mp, self.mistakes)
        self._mistakes_sig = self._file_stat_sig(mp)

    def _is_cowrite_silent_active(self) -> bool:
        cw = getattr(self, 'cowrite', None)
        if not isinstance(cw, CoWriteMonitor):
            return False
        cw._ensure_loaded()
        return bool(cw.enabled or len(cw.items) > 0)

    def send_parent_nudge(self, text: str, sender: str = '家长') -> dict:
        msg = re.sub(r'\s+', ' ', str(text or '')).strip()[:80]
        if not msg:
            raise ValueError('请输入要发给孩子的鼓励或提醒内容')
        self.parent_nudge_seq += 1
        target_sid = str(self.active_session_id or '').strip()
        nudge = {
            'seq': self.parent_nudge_seq,
            'text': msg,
            'sender': str(sender or '家长').strip()[:10] or '家长',
            'ts': int(time.time()),
            'mono': time.monotonic(),
            'target_sid': target_sid,
        }
        self.latest_parent_nudge = nudge
        if target_sid and target_sid in self.sessions:
            self.sessions[target_sid]['parent_nudge'] = {
                k: nudge[k] for k in ('seq', 'text', 'sender', 'ts')
            }
            self.sessions[target_sid]['seq'] += 1
        return {k: nudge[k] for k in ('seq', 'text', 'sender', 'ts')}

    def get_fresh_parent_nudge(self, for_sid: str = None) -> dict:
        n = self.latest_parent_nudge
        if not n or (time.monotonic() - float(n.get('mono') or 0)) > 180.0:
            return None
        t_sid = str(n.get('target_sid') or '').strip()
        if for_sid is not None and t_sid and str(for_sid).strip() != t_sid:
            return None
        return {k: n[k] for k in ('seq', 'text', 'sender', 'ts')}

    def _sync_cowrite_graduated(self, mid: str, graduated: bool):
        cw_mgr = getattr(self, 'cowrite', None)
        if not mid or not isinstance(cw_mgr, CoWriteMonitor):
            return
        cw_mgr._ensure_loaded()
        changed = False
        for it in cw_mgr.items:
            if it.get('mistake_id') == mid and bool(it.get('ever_graduated')) != bool(graduated):
                it['ever_graduated'] = bool(graduated)
                changed = True
        if changed:
            cw_mgr.seq += 1
            cw_mgr._save_state()

    def _enroll_or_refresh_mistake(self, sess: dict, prog: dict) -> dict:
        self._ensure_mistakes_loaded()
        if not isinstance(sess, dict) or sess.get('silent_preview'):
            return None
        if sess.get('essay_critique') or detect_writing_task(sess.get('question') or '') or is_essay_critique_request(sess.get('question') or '', sess.get('teach_mode') or 'standard'):
            return None
        q_txt = re.sub(r'\s+', ' ', str(sess.get('question') or '')).strip()
        real_steps = [s for s in (sess.get('steps') or []) if int(s.get('step') or 0) > 0]
        if not q_txt or not real_steps:
            return None
        now_ts = int(time.time())
        t_mode = _norm_teach_mode(sess.get('teach_mode'))
        w_step = int(prog.get('weak_step') or 0)
        raw_tot_g = prog.get('total_gates')
        tot_g = int(raw_tot_g) if raw_tot_g is not None else 3
        ft_cnt = int(prog.get('first_try_count') or 0) if ('first_try_count' in prog) else (tot_g if w_step == 0 else 0)
        peeked = bool(prog.get('peeked'))
        solved = bool(prog.get('solved'))
        solo_ace = bool(prog.get('solo_ace'))

        retest_mid = str(sess.get('mistake_id') or '').strip()
        if retest_mid:
            hit = next((m for m in self.mistakes if m.get('id') == retest_mid), None)
            if not hit:
                retest_mid = ''
            elif not solved:
                if peeked or w_step > 0 or int(prog.get('step_attempts') or 0) >= 3:
                    hit['stage'] = 0
                    hit['graduated'] = False
                    if w_step > 0:
                        hit['weak_step'] = w_step
                    hit['updated_at'] = now_ts
                    hit['next_review_at'] = now_ts + 86400
                    self._save_mistakes()
                    self._sync_cowrite_graduated(retest_mid, False)
                return None
        if retest_mid:
            clean_pass = (not peeked) and w_step == 0 and (solo_ace or ft_cnt >= tot_g)
            hit['updated_at'] = now_ts
            if clean_pass:
                if int(hit.get('stage') or 0) <= 0:
                    hit['stage'] = 1
                    hit['retest_pass_count'] = 1
                    hit['graduated'] = False
                    hit['next_review_at'] = now_ts + 3 * 86400
                    self._save_mistakes()
                    return {
                        'type': 'stage1_pass',
                        'id': retest_mid,
                        'stage': 1,
                        'graduated': False,
                        'msg': '🎯 首轮脱敏复测一次全对！3天后完成二次复测即可荣誉销号！',
                    }
                hit['stage'] = 2
                hit['retest_pass_count'] = int(hit.get('retest_pass_count') or 1) + 1
                hit['graduated'] = True
                hit['next_review_at'] = 0
                self._save_mistakes()
                self._sync_cowrite_graduated(retest_mid, True)
                return {
                    'type': 'graduated',
                    'id': retest_mid,
                    'stage': 2,
                    'graduated': True,
                    'msg': '🏅 两次脱敏换数复测全部独立满星，本题已从错题本「荣誉销号」！',
                }
            hit['stage'] = 0
            hit['graduated'] = False
            if w_step > 0:
                hit['weak_step'] = w_step
            hit['next_review_at'] = now_ts + 86400
            self._save_mistakes()
            self._sync_cowrite_graduated(retest_mid, False)
            return {
                'type': 'retest_retry',
                'id': retest_mid,
                'stage': 0,
                'graduated': False,
                'msg': '📌 本次复测还借了一点提示，已留在错题本明天继续巩固！',
            }

        st_att_now = int(prog.get('step_attempts') or 0)
        is_weak = (
            t_mode == 'diagnose_draft'
            or peeked
            or (not solved and st_att_now >= 3)
            or (solved and not solo_ace and (w_step > 0 or ft_cnt < tot_g))
        )
        if not solved and st_att_now >= 3 and w_step <= 0:
            w_step = int(prog.get('current_step') or 3)
        if not is_weak:
            return None
        if t_mode == 'diagnose_draft':
            err_tag = f'草稿找错因·第{w_step}关' if w_step > 0 else '草稿找错因'
        elif peeked:
            err_tag = '曾看家长全览'
        elif w_step > 0:
            err_tag = f'第{w_step}关薄弱'
        else:
            err_tag = '提示辅助通关'

        w_focus = ''
        if w_step > 0:
            st_hit = next((s for s in real_steps if int(s.get('step') or 0) == w_step), None)
            if st_hit:
                qz_h = st_hit.get('quiz') or {}
                w_focus = f"{st_hit.get('title', '')}（{qz_h.get('q', '')}）".strip('（） ')[:64]

        cw_item_ref = str(sess.get('cowrite_item_id') or '').strip()
        cw_hit = None
        cw_mgr = getattr(self, 'cowrite', None)
        if cw_item_ref and isinstance(cw_mgr, CoWriteMonitor):
            cw_mgr._ensure_loaded()
            cw_hit = next((x for x in cw_mgr.items if x.get('id') == cw_item_ref), None)

        mid = (
            str(cw_hit.get('mistake_id') or '').strip()
            if (t_mode == 'diagnose_draft' and cw_hit and cw_hit.get('mistake_id'))
            else ('mb_' + hashlib.sha256(q_txt.encode('utf-8')).hexdigest()[:10])
        )
        existing = next((m for m in self.mistakes if m.get('id') == mid), None)
        if cw_hit is not None:
            err_tag = f"伴写·{str(cw_hit.get('topic') or '错因诊断').strip()[:10]}"
        ex_img = str(existing.get('image_url') or '') if existing else ''
        cw_snap = f"/api/tutor/snap/{cw_hit['snap_name']}" if (t_mode == 'diagnose_draft' and cw_hit and cw_hit.get('snap_name')) else ''
        keep_img = ex_img if ex_img.startswith('/api/tutor/snap/c0_') else (cw_snap or sess.get('image_url') or ex_img or '')
        clean_draft_review = bool(
            t_mode == 'diagnose_draft'
            and existing is not None
            and solved
            and not peeked
            and w_step == 0
            and (solo_ace or ft_cnt >= tot_g)
        )
        keep_grad_dr = bool(existing and existing.get('graduated')) if clean_draft_review else False
        entry = {
            'id': mid,
            'orig_sid': sess['id'],
            'title': str(sess.get('title') or q_txt[:18])[:36],
            'question': q_txt[:600],
            'subject': detect_subject(q_txt, teach_mode=t_mode, essay_critique=bool(sess.get('essay_critique'))),
            'pedagogy': _norm_pedagogy(sess.get('pedagogy')),
            'mode': _norm_mode(sess.get('mode')) if sess.get('mode') != 'golden' else 'fast',
            'teach_mode': t_mode,
            'weak_step': w_step or (int(existing.get('weak_step') or 0) if existing else 0),
            'weak_focus': w_focus or (str(existing.get('weak_focus') or '') if existing else ''),
            'error_tag': err_tag,
            'stage': int(existing.get('stage') or 0) if clean_draft_review else 0,
            'graduated': keep_grad_dr,
            'retest_pass_count': int(existing.get('retest_pass_count') or 0) if clean_draft_review else 0,
            'created_at': int(existing.get('created_at') or now_ts) if existing else now_ts,
            'updated_at': now_ts,
            'next_review_at': (0 if keep_grad_dr else int(existing.get('next_review_at') or (now_ts + 86400))) if clean_draft_review else (now_ts + 86400),
            'image_url': keep_img,
            'steps': copy.deepcopy(sess.get('steps') or [])[:10],
        }
        self.mistakes = [m for m in self.mistakes if m.get('id') != mid]
        self.mistakes.insert(0, entry)
        self._save_mistakes()
        if cw_hit is not None and (not cw_hit.get('enrolled_mistake') or cw_hit.get('mistake_id') != mid):
            cw_hit['enrolled_mistake'] = True
            cw_hit['mistake_id'] = mid
            cw_mgr.seq += 1
            cw_mgr._save_state()
        return {'type': 'enrolled', 'id': mid, 'stage': entry['stage'], 'graduated': entry['graduated'], 'error_tag': err_tag}

    def public_mistakes(self) -> list:
        self._ensure_mistakes_loaded()
        now_ts = int(time.time())
        out = []
        for m in self.mistakes[:50]:
            grad = bool(m.get('graduated'))
            nxt = int(m.get('next_review_at') or 0)
            due = (not grad) and (nxt <= now_ts)
            out.append({
                'id': m['id'],
                'orig_sid': m.get('orig_sid') or '',
                'session_ref': f"m:{m['id']}",
                'title': m.get('title') or '错题记录',
                'question': m.get('question') or '',
                'subject': _resolve_saved_subject(
                    m.get('question') or '',
                    m.get('subject') or '',
                    teach_mode=_norm_teach_mode(m.get('teach_mode')),
                    essay_critique=bool(m.get('essay_critique')),
                ),
                'weak_step': int(m.get('weak_step') or 0),
                'weak_focus': m.get('weak_focus') or '',
                'error_tag': m.get('error_tag') or '待巩固',
                'stage': int(m.get('stage') or 0),
                'graduated': grad,
                'due_now': due,
                'retest_pass_count': int(m.get('retest_pass_count') or 0),
                'created_at': int(m.get('created_at') or 0),
                'updated_at': int(m.get('updated_at') or 0),
                'next_review_at': nxt,
            })
        return out

    def public_mistake_summary(self) -> dict:
        items = self.public_mistakes()
        active = [x for x in items if not x['graduated']]
        due = [x for x in active if x['due_now']]
        grad = [x for x in items if x['graduated']]
        return {
            'total_count': len(items),
            'active_count': len(active),
            'due_count': len(due),
            'graduated_count': len(grad),
            'top_title': (due[0]['title'] if due else (active[0]['title'] if active else '')),
            'top_id': (due[0]['id'] if due else (active[0]['id'] if active else '')),
        }

    def delete_mistake(self, mid: str = '', clear_graduated: bool = False, clear_all: bool = False) -> dict:
        self._ensure_mistakes_loaded()
        cleared_grad_mids = set()
        if clear_all:
            self.mistakes = []
        elif clear_graduated:
            cleared_grad_mids = {
                str(m.get('id') or '').strip()
                for m in self.mistakes
                if isinstance(m, dict) and m.get('graduated') and m.get('id')
            }
            self.mistakes = [m for m in self.mistakes if not m.get('graduated')]
        elif mid:
            self.mistakes = [m for m in self.mistakes if m.get('id') != mid]
        self._save_mistakes()
        alive_mids = {m.get('id') for m in self.mistakes if isinstance(m, dict) and m.get('id')}
        cw_mgr = getattr(self, 'cowrite', None)
        cw_pub = None
        if isinstance(cw_mgr, CoWriteMonitor):
            cw_mgr._ensure_loaded()
            cw_changed = False
            for it in cw_mgr.items:
                mid_it = str(it.get('mistake_id') or '').strip()
                if clear_all:
                    if it.get('enrolled_mistake') or mid_it or it.get('ever_graduated'):
                        it['enrolled_mistake'] = False
                        it['mistake_id'] = ''
                        it['ever_graduated'] = False
                        cw_changed = True
                    continue
                if clear_graduated and mid_it and mid_it in cleared_grad_mids and not it.get('ever_graduated'):
                    it['ever_graduated'] = True
                    cw_changed = True
                elif (not clear_graduated) and mid and mid_it == mid and it.get('ever_graduated'):
                    it['ever_graduated'] = False
                    cw_changed = True
                if (it.get('enrolled_mistake') or mid_it) and mid_it not in alive_mids:
                    it['enrolled_mistake'] = False
                    it['mistake_id'] = ''
                    cw_changed = True
            if cw_changed:
                cw_mgr.seq += 1
                cw_mgr._save_state()
            cw_pub = cw_mgr.public_state()
        dc_mgr = getattr(self, 'dictation', None)
        dc_pub = None
        if isinstance(dc_mgr, DictationManager):
            dc_mgr._ensure_loaded()
            dc_changed = False
            for it in dc_mgr.items:
                mid_dc = str(it.get('mistake_id') or '').strip()
                if (clear_all and (it.get('enrolled_mistake') or mid_dc)) or ((it.get('enrolled_mistake') or mid_dc) and mid_dc not in alive_mids):
                    it['enrolled_mistake'] = False
                    it['mistake_id'] = ''
                    dc_changed = True
            if dc_changed:
                dc_mgr.seq += 1
                dc_mgr._save_state()
            dc_pub = dc_mgr.public_state()
        out_del = {'ok': True, 'mistakes': self.public_mistakes(), 'summary': self.public_mistake_summary()}
        if cw_pub is not None:
            out_del['cowrite'] = cw_pub
        if dc_pub is not None:
            out_del['dictation'] = dc_pub
        return out_del

    async def _ensure_cowrite_mistake_stem(self, m_it: dict, save: bool = False) -> bool:
        if not isinstance(m_it, dict):
            return False
        q_cur = str(m_it.get('question') or '').strip()
        iu_cur = str(m_it.get('image_url') or '').strip()
        if not (q_cur.startswith('【初三伴写错题·') and iu_cur.startswith('/api/tutor/snap/c0_')):
            return False
        sn_m = iu_cur.rsplit('/', 1)[-1]
        if not re.fullmatch(r'c0_[0-9a-f]{12}\.jpg', sn_m):
            return False
        with contextlib.suppress(Exception):
            _, raw_sn = _read_regular_bytes(SNAP_DIR / sn_m, 8 * 1024 * 1024)
            ocr_m = sanitize_ocr_question(await self._ocr_question_from_image(raw_sn, extra_note=''))
            if len(ocr_m) >= 6:
                m_it['question'] = ocr_m[:600]
                if save:
                    self._save_mistakes()
                return True
        return False

    async def retest_mistake(self, mid: str = '', mode: str = 'fast', pedagogy: str = 'auto', open_ui: bool = False, solo_challenge: bool = False, silent_tts=None) -> dict:
        self._ensure_mistakes_loaded()
        now_ts = int(time.time())
        target = None
        if mid:
            target = next((m for m in self.mistakes if m.get('id') == mid), None)
        else:
            active = [m for m in self.mistakes if not m.get('graduated')]
            due = [m for m in active if int(m.get('next_review_at') or 0) <= now_ts]
            target = due[0] if due else (active[0] if active else None)
        if not target:
            raise LookupError('错题本里暂时没有待复测的题目哦')
        await self._ensure_cowrite_mistake_stem(target, save=True)
        is_cw_target = str(target.get('error_tag') or '').startswith('伴写·')
        cw_en_rt = self._is_cowrite_silent_active()
        eff_sil_rt = bool(silent_tts) if isinstance(silent_tts, bool) else bool(is_cw_target or cw_en_rt)
        use_ped = 'junior' if is_cw_target else (
            pedagogy if pedagogy in ('elem', 'junior') else (target.get('pedagogy') or 'auto')
        )
        ped_hint = resolve_pedagogy_hint(use_ped, target['question'])
        w_step = int(target.get('weak_step') or 0)
        w_focus = str(target.get('weak_focus') or target.get('error_tag') or '核心易错关系')[:64]
        stage_lbl = '二次终极销号复测' if int(target.get('stage') or 0) >= 1 else '首轮脱敏换数复测'
        m_is_ec = bool(target.get('essay_critique')) or is_essay_critique_request(target.get('question') or '', target.get('teach_mode') or '')
        m_subj = _resolve_saved_subject(
            target.get('question') or '',
            target.get('subject') or '',
            teach_mode=_norm_teach_mode(target.get('teach_mode')),
            essay_critique=bool(target.get('essay_critique')),
        )
        retest_rule = (
            '请围绕相同记叙/应用写作技法，更换生活场景新给出一段含典型平淡流水账与1处小语病的学生短段落（冠以【作文升格精批】前缀，50~80字），供孩子实战复测病句修改与慢镜头细节升格！'
            if m_is_ec else (
                '请保持完全相同的核心考点模型与易错陷阱，更换生活背景和全部已知数字，新编一道【脱敏换数复测变式题】（严禁保留原题相同得数，防止背答案；必须先在 check_calc 内部完整验算确保除得尽、逻辑严密）！'
                if m_subj in ('math', 'physics', 'chemistry') else
                '请保持完全相同的核心语法/阅读赏析/学科概念考点与易错陷阱，更换全新语境句子、文本素材或情境条件，新编一道【同考点脱敏复测变式题】（严禁保留原题原句或原选项答案，防止死记硬背）！'
            )
        )
        rt_need_calc = (not m_is_ec) and m_subj in ('math', 'physics', 'chemistry')
        rt_calc_fld = '"check_calc":"内部验算整除过程",' if rt_need_calc else ''
        prompt = (
            f"原错题：「{target['question']}」\n"
            f"{ped_hint}\n"
            f"孩子上次薄弱点：「{w_focus}」。现在进行【{stage_lbl}】：{retest_rule}\n"
            f'只输出一行JSON（不要代码块）：{{{rt_calc_fld}"title":"复测：...(10字内)","question":"完整复测题目文字"}}'
        )
        content = await self._post_chat_once('aux', [{'role': 'user', 'content': prompt}], temperature=0.4, timeout_s=34.0)
        objs, _ = extract_json_objects_from_buffer(content)
        if not objs or not objs[0].get('question'):
            raise RuntimeError('复测题生成格式异常，请重试一次')
        r_title = str(objs[0].get('title') or f"复测：{target.get('title', '错题巩固')}")[:28]
        r_q = sanitize_ocr_question(str(objs[0]['question']).strip())
        if m_is_ec and not is_essay_critique_request(r_q, 'diagnose_draft'):
            r_q = '【作文升格精批】' + r_q
        else:
            rt_tag = {'chinese': '【初中语文】', 'english': '【初中英语】', 'physics': '【初中物理】', 'chemistry': '【初中化学】'}.get(m_subj, '') if use_ped == 'junior' else ''
            if rt_tag and not r_q.startswith('【') and detect_subject(r_q) == 'math':
                r_q = f'{rt_tag}{r_q}'
        new_sess = self.start_live_problem(
            question=r_q,
            mode=_norm_mode(mode or target.get('mode')),
            title=r_title,
            source='mistake_retest',
            open_ui=open_ui,
            pedagogy=use_ped,
            teach_mode='diagnose_draft' if m_is_ec else 'standard',
            mistake_id=target['id'],
            solo_challenge=bool(solo_challenge),
            silent_tts=eff_sil_rt,
        )
        return {'ok': True, 'mistake_id': target['id'], 'stage': int(target.get('stage') or 0), 'session': new_sess}

    def _trim_sessions(self, max_len: int = 16):
        while len(self.sessions) > max_len:
            evict_id = next(
                (
                    k for k, v in self.sessions.items()
                    if k != self.active_session_id
                    and (not isinstance(v, dict) or v.get('status') not in ('thinking', 'streaming'))
                ),
                None,
            )
            if evict_id is None:
                break
            self.sessions.pop(evict_id, None)

    def _get_or_hydrate_session(self, sid: str) -> dict:
        s_clean = str(sid or '').strip()
        if not s_clean:
            return None
        self._ensure_history_loaded()
        if s_clean.startswith('m:'):
            self._ensure_mistakes_loaded()
        sess = self.sessions.get(s_clean)
        if sess is not None:
            self.sessions.move_to_end(s_clean)
            return sess
        hist_hit = next((h for h in self.history if h.get('id') == s_clean), None)
        if hist_hit:
            sess = dict(
                hist_hit,
                progress=copy.deepcopy(hist_hit.get('progress') or {}),
                steps=copy.deepcopy(hist_hit.get('steps') or []),
                status='ready',
                seq=2,
                t0_mono=time.monotonic(),
            )
            self.sessions[s_clean] = sess
            self._trim_sessions()
            return sess
        if s_clean.startswith('m:'):
            self._ensure_mistakes_loaded()
            m_hit = next((m for m in self.mistakes if f"m:{m['id']}" == s_clean), None)
            if m_hit:
                is_cw_hyd = str(m_hit.get('error_tag') or '').startswith('伴写·')
                cw_en_hyd = self._is_cowrite_silent_active()
                sess = {
                    'id': s_clean,
                    'title': m_hit.get('title') or '错题回看',
                    'question': m_hit.get('question') or '',
                    'subject': str(m_hit.get('subject') or ''),
                    'mode': m_hit.get('mode') or 'fast',
                    'source': 'cowrite' if is_cw_hyd else 'mistake_replay',
                    'teach_mode': _norm_teach_mode(m_hit.get('teach_mode')),
                    'essay_critique': bool(m_hit.get('essay_critique')) or is_essay_critique_request(m_hit.get('question') or '', m_hit.get('teach_mode') or ''),
                    'pedagogy': m_hit.get('pedagogy') or 'auto',
                    'mistake_id': '',
                    'image_url': m_hit.get('image_url') or '',
                    'stage_page_id': '',
                    'status': 'ready',
                    'error': '',
                    'created_at': m_hit.get('created_at') or 0,
                    't0_mono': time.monotonic(),
                    'metrics': {},
                    'progress': {'weak_step': int(m_hit.get('weak_step') or 0)},
                    'steps': copy.deepcopy(m_hit.get('steps') or []),
                    'seq': 2,
                    'silent_start': bool(is_cw_hyd or cw_en_hyd),
                }
                for st_m in sess['steps']:
                    qz_m = st_m.get('quiz') if isinstance(st_m, dict) else None
                    if isinstance(qz_m, dict) and 'mode' not in qz_m:
                        st_m['quiz'] = sanitize_quiz(dict(qz_m, no_shuffle=True), int(st_m.get('step') or 1), s_clean)
                self.sessions[s_clean] = sess
                self._trim_sessions()
                return sess
        return None

    async def generate_alt_method(self, sid: str, pedagogy: str = 'auto', silent_tts=None) -> dict:
        sess = self._get_or_hydrate_session(sid)
        if not sess:
            raise LookupError('未找到当前微课会话')
        eff_sil_alt = bool(silent_tts) if isinstance(silent_tts, bool) else bool(sess.get('silent_start'))
        prog = sess.get('progress') or {}
        if not (prog.get('solved') or prog.get('peeked')):
            raise ValueError('请先独立闯关解锁最终步骤后，再揭晓「一题多解」对比卡哦！')
        sp_id = str(sess.get('sample_id') or '')
        if sp_id and sp_id in SAMPLE_ALT_METHODS and not sess.get('alt_method'):
            sess['alt_method'] = copy.deepcopy(SAMPLE_ALT_METHODS[sp_id])
        if isinstance(sess.get('alt_method'), dict) and sess['alt_method'].get('steps'):
            alt_cached = sess['alt_method']
            if alt_cached.get('say') and not eff_sil_alt:
                self._spawn(self.synthesize_wav(alt_cached['say'][:220]), session_work=True)
            return {'ok': True, 'alt_method': alt_cached}
        q_txt = str(sess.get('question') or '').strip()
        is_ec_alt = bool(sess.get('essay_critique')) or bool(detect_writing_task(q_txt))
        subj_alt = detect_subject(q_txt, teach_mode=sess.get('teach_mode') or 'standard', essay_critique=bool(sess.get('essay_critique')))
        cur_board = '\n'.join(str(s.get('board') or '') for s in (sess.get('steps') or []) if int(s.get('step') or 0) > 0)
        use_ped_alt = 'junior' if sess.get('source') == 'cowrite' else (pedagogy if pedagogy in ('elem', 'junior') else _norm_pedagogy(sess.get('pedagogy')))
        ped_hint = resolve_pedagogy_hint(use_ped_alt, q_txt)
        if is_ec_alt:
            alt_task = (
                '孩子已完成本题审题/升格闯关！请提供【换种写法·第二种升格切入角度】（例如若刚才用动作五感慢镜头，这里可展示环境侧面烘托法、欲扬先抑对比法或倒叙悬念切入法；严禁代写整篇范文，仅给1~2句55字内点睛示范与技法拆解），并对比两种写法各自最适合的考场情境。\n'
                '只输出一行JSON（不要代码块）：{"title":"第二种升格技法(12字内)","badge":"技法拓展","steps":"第二种写法拆解与1句短示范(95字内)","compare":"两种写法各自优势与适用场景对比(55字内)","say":"55~80字温暖口语点拨第二种升格妙招"}'
            )
        elif subj_alt in ('chinese', 'english', 'history', 'civics', 'biology', 'geography'):
            alt_task = (
                '孩子已成功闯关答对本题！请提供【多维切入·第二种破题/验证视角】（如英语语境逻辑法vs标志词结构法、语文换词比较法vs意象还原法、文综时空轴定位vs主体反推法、理综结构功能印证vs对照排除法），并对比两种视角如何交叉验证防丢分。\n'
                '只输出一行JSON（不要代码块）：{"title":"第二种破题视角(12字内)","badge":"多维印证","steps":"第二种切入角度2~3步分析与结论核对(95字内)","compare":"两种分析视角互补优势对比(55字内)","say":"55~80字温暖口语讲解第二种验证思路"}'
            )
        else:
            alt_task = (
                '孩子已经成功闯关算出答案！请为他提供【一题多解】第二种截然不同但同样巧妙的解法（注意遵守上方学段限制，小学禁用超纲方程，可对比假设法/倒推法/容斥法/等积法/比例法等），并对比两种方法各自适用场景。\n'
                '只输出一行JSON（不要代码块）：{"title":"第二种巧解名称(12字内)","badge":"4~6字方法标签","steps":"第二种解法核心2~3步推导与相同得数验证(95字内)","compare":"两种解法优劣与适用题型对比(55字内)","say":"55~80字温暖口语讲解第二种思路与对比"}'
            )
        prompt = (
            f"原题：「{q_txt}」\n"
            f"{ped_hint}\n"
            f"刚才第一套板书：\n{cur_board[:500]}\n\n"
            f"{alt_task}"
        )
        if not eff_sil_alt:
            await self.pause_music_if_playing()
        content = await self._post_chat_once('aux', [{'role': 'user', 'content': prompt}], temperature=0.3, timeout_s=32.0)
        objs, _ = extract_json_objects_from_buffer(content)
        obj = objs[0] if objs else {}
        def_title = '换个视角点亮细节' if is_ec_alt else ('多维视角交叉验证' if subj_alt in ('chinese', 'english', 'history', 'civics', 'biology', 'geography') else '换个视角巧解')
        def_cmp = (
            '两种写法各有千秋：慢镜头抓特写细节，侧面烘托或对比更能拉满情感张力！'
            if is_ec_alt else (
                '考场上用正面分析得出结论后，再用第二种视角反向印证，稳拿满分不踩坑！'
                if subj_alt in ('chinese', 'english', 'history', 'civics', 'biology', 'geography') else
                '两种思路殊途同归，考场上可用第二种方法快速交叉验算！'
            )
        )
        def_say = (
            '同一个片段换一种描写镜头，画面张力又不一样啦，考场上灵活搭配更出彩哦！'
            if is_ec_alt else (
                '我们换一个学科视角再印证一遍，两个角度指向同一个结论，答题卡上稳稳拿满啦！'
                if subj_alt in ('chinese', 'english', 'history', 'civics', 'biology', 'geography') else
                '我们还可以换一个角度思考这道题，两种方法算出了一模一样的结果，互相印证更稳啦！'
            )
        )
        alt = {
            'title': str(obj.get('title') or def_title)[:26],
            'badge': str(obj.get('badge') or ('技法拓展' if is_ec_alt else '思维拓展'))[:12],
            'steps': _coerce_multiline_str(obj.get('steps') or obj.get('board') or content[:140], 220),
            'compare': _coerce_multiline_str(obj.get('compare') or obj.get('note') or def_cmp, 120),
            'say': _coerce_multiline_str(obj.get('say') or def_say, 220),
        }
        sess['alt_method'] = alt
        sess['seq'] = int(sess.get('seq') or 1) + 1
        if not str(sid).startswith('m:'):
            self._save_to_history(sess)
        if not eff_sil_alt:
            self._spawn(self.synthesize_wav(alt['say'][:220]), session_work=True)
        return {'ok': True, 'alt_method': alt}

    @staticmethod
    def _scrub_page_check_spoilers(note_txt: str) -> str:
        s = re.sub(r'\s+', ' ', str(note_txt or '')).strip()
        if not s:
            return '点此开启 4 步启发订正'
        s = re.sub(r'(?:正确)?(?:答案|得数|结果|选项)(?:应该|应)?(?:是|为|=|＝|:|：)?\s*[^，。；,;]+', '请点此自查关键关系', s)
        s = re.sub(r"(?:应该|应)?(?:填|改为|改成|选|选择)\s*[A-Za-z0-9\u4e00-\u9fa5'/\-\s]{1,22}", '请点此启发自查', s)
        s = re.sub(r'[=＝≈]\s*[-−－]?\d+(?:\.\d+)?(?:/\d+)?%?', '= ？', s)
        s = re.sub(r'\d+(?:\.\d+)?\s*[×÷+\-−*/]\s*\d+(?:\.\d+)?', '列式关系', s)
        return s[:32] or '点此开启 4 步启发订正'

    async def check_staged_worksheet_page(self, page_id: str = '', pencil_boost: bool = False) -> dict:
        if page_id:
            self.phone_studio.select_page(page_id)
        if not self.phone_studio.has_page():
            raise LookupError('请先上传一整页已作答的作业图片')
        act_pg = self.phone_studio._active_page()
        act_pid = self.phone_studio.active_id
        orig_seq = act_pg.get('seq') if act_pg else None
        p_boost = bool(pencil_boost)
        ck_key = (act_pid, orig_seq, p_boost)
        inflight = getattr(self, '_page_check_inflight', None)
        if isinstance(inflight, dict):
            fut = inflight.get(ck_key)
            if fut is not None and not fut.done():
                return await asyncio.shield(fut)
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            fut.add_done_callback(lambda f: None if f.cancelled() else f.exception())
            inflight[ck_key] = fut
        else:
            fut = None
        try:
            res = await self._run_check_staged_worksheet_page(act_pid, orig_seq, p_boost)
            if fut is not None and not fut.done():
                fut.set_result(res)
            return res
        except BaseException as exc:
            if fut is not None and not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            if isinstance(inflight, dict) and inflight.get(ck_key) is fut:
                inflight.pop(ck_key, None)

    async def _run_check_staged_worksheet_page(self, act_pid: str, orig_seq, pencil_boost: bool) -> dict:
        jpg = await self.phone_studio.get_rotated_stage_jpeg(act_pid)
        if pencil_boost:
            jpg = await self.phone_studio.enhance_jpeg(jpg, crop_box=None, rotate_deg=0, crisp=True, pencil_boost=True)
        b64 = base64.b64encode(jpg).decode('ascii')
        prompt = (
            '请仔细扫描这张整页学生作业图片中的每一道题及其手写解答，进行「先批后讲」快速筛查：\n'
            '1. 对手写答案完全正确的题标 status="ok"；对手写算错/答错的题标 status="wrong"；对留空未写的题标 status="blank"。\n'
            '2. 给出每道题（含题干与手写区）在整张图中的归一化矩形坐标 box:[x,y,w,h]（范围 0~1，保留3位小数）。\n'
            '3. 【严格防剧透铁律】：wrong 或 blank 题目的 note 只能写错在哪个环节（如“周长与半周长关系需核对”、“单位换算需留意”），严禁在 note 中写出正确答案数字或正确算式！\n'
            '只输出一行JSON（不要代码块）：{"summary":"共识别X题：做对Y题，待订正Z题","items":[{"no":"第1题","status":"ok","box":[0.06,0.10,0.88,0.18],"note":"列式与得数全对"},{"no":"第2题","status":"wrong","box":[0.06,0.34,0.88,0.22],"note":"份数关系需核对"}]}'
        )
        messages = [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + b64}},
            ],
        }]
        content = await self._post_chat_once('fast', messages, temperature=0.15, timeout_s=32.0)
        objs, _ = extract_json_objects_from_buffer(content)
        obj = objs[0] if objs else {}
        raw_items = obj.get('items') if isinstance(obj.get('items'), list) else []
        clean_marks = []
        ok_cnt = 0
        fix_cnt = 0
        for idx, it in enumerate(raw_items[:10]):
            if not isinstance(it, dict):
                continue
            st = str(it.get('status') or '').strip().lower()
            if st not in ('ok', 'wrong', 'blank'):
                st = 'wrong'
            b_raw = it.get('box')
            if isinstance(b_raw, (list, tuple)) and len(b_raw) >= 4:
                b_dict = {'x': b_raw[0], 'y': b_raw[1], 'w': b_raw[2], 'h': b_raw[3]}
            elif isinstance(b_raw, dict):
                b_dict = b_raw
            else:
                continue
            s_box = self.phone_studio._sanitize_crop_box(b_dict)
            if not s_box:
                continue
            no_lbl = str(it.get('no') or f'第{idx + 1}题').strip()[:10]
            raw_note = str(it.get('note') or '').strip()
            note_clean = (raw_note[:28] or '作答正确 ✓') if st == 'ok' else self._scrub_page_check_spoilers(raw_note)
            if st == 'ok':
                ok_cnt += 1
            else:
                fix_cnt += 1
            clean_marks.append({'no': no_lbl, 'status': st, 'box': s_box, 'note': note_clean})
        summary = str(obj.get('summary') or f'共识别 {len(clean_marks)} 题：做对 {ok_cnt} 题，待订正/作答 {fix_cnt} 题').strip()[:76]
        cur_pg = next((pg for pg in self.phone_studio._valid_pages() if pg['id'] == act_pid), None)
        if not cur_pg or (orig_seq is not None and cur_pg.get('seq') != orig_seq):
            log.info('Discarding stale worksheet check marks for replaced/removed page %s', act_pid)
            return {
                'ok': True,
                'stale': True,
                'page_id': act_pid,
                'summary': '作业页已更新，旧批改已跳过',
                'ok_count': 0,
                'fix_count': 0,
                'check_marks': [],
                'phone_studio': self.phone_studio.public_state(),
            }
        self.phone_studio.set_check_marks(act_pid, clean_marks, summary=summary)
        first_fix = next((m for m in clean_marks if m['status'] != 'ok'), None)
        if first_fix and self.phone_studio.active_id == act_pid:
            self.phone_studio.update_active_crop(first_fix['box'])
        return {
            'ok': True,
            'page_id': act_pid,
            'summary': summary,
            'ok_count': ok_cnt,
            'fix_count': fix_cnt,
            'check_marks': clean_marks,
            'phone_studio': self.phone_studio.public_state(),
        }

    async def _build_sprint_question_spec(self, item: dict, mode: str, pedagogy: str) -> tuple:
        if item.get('mid'):
            mb_src = next((m for m in self.mistakes if m.get('id') == item['mid']), None)
            if mb_src and await self._ensure_cowrite_mistake_stem(mb_src, save=True):
                item['question'] = mb_src['question']
        q_orig = str(item.get('question') or '').strip()
        use_ped = pedagogy if pedagogy in ('elem', 'junior') else (item.get('pedagogy') or 'auto')
        ped_hint = resolve_pedagogy_hint(use_ped, q_orig)
        focus = str(item.get('weak_focus') or item.get('error_tag') or item.get('grade') or '核心易错陷阱')[:60]
        sp_is_ec = bool(item.get('essay_critique')) or is_essay_critique_request(q_orig, item.get('teach_mode') or '')
        sp_subj = _resolve_saved_subject(q_orig, item.get('subject') or '', teach_mode=item.get('teach_mode') or 'standard', essay_critique=sp_is_ec)
        sp_rule = (
            '请围绕相同写作技法，更换场景新给出一段含平淡流水账与1处小语病的学生短段落（冠以【作文升格精批】前缀，50~80字），供考前升格特训！'
            if sp_is_ec else (
                '请保持完全相同的考点模型与易错陷阱，更换场景与全部数字，新编一道单题单问的【考前特训脱敏变式题】（勿拆(1)(2)多小问，务必先在 check_calc 验算确保整除无误）！'
                if sp_subj in ('math', 'physics', 'chemistry') else
                '请保持完全相同的语法/阅读/古诗文/学科核心考点与易错陷阱，更换全新语境素材与设问，新编一道单题单问的【考前特训同考点变式题】（严禁保留原句答案）！'
            )
        )
        sp_calc_fld = '"check_calc":"内部验算整除过程",' if ((not sp_is_ec) and sp_subj in ('math', 'physics', 'chemistry')) else ''
        prompt = (
            f"母题：「{q_orig}」\n{ped_hint}\n"
            f"考前靶向点：「{focus}」。{sp_rule}\n"
            f'只输出一行JSON（不要代码块）：{{{sp_calc_fld}"title":"特训：...(10字内)","question":"完整特训题目文字"}}'
        )
        try:
            content = await self._post_chat_once('aux', [{'role': 'user', 'content': prompt}], temperature=0.4, timeout_s=28.0)
            objs, _ = extract_json_objects_from_buffer(content)
            if objs and objs[0].get('question'):
                sp_q_out = sanitize_ocr_question(str(objs[0]['question']).strip())
                if sp_is_ec and not is_essay_critique_request(sp_q_out, 'diagnose_draft'):
                    sp_q_out = '【作文升格精批】' + sp_q_out
                elif use_ped == 'junior':
                    sp_tag = {'chinese': '【初中语文】', 'english': '【初中英语】', 'physics': '【初中物理】', 'chemistry': '【初中化学】'}.get(sp_subj, '')
                    if sp_tag and not sp_q_out.startswith('【') and detect_subject(sp_q_out) == 'math':
                        sp_q_out = f'{sp_tag}{sp_q_out}'
                return str(objs[0].get('title') or '考前靶向特训')[:26], sp_q_out, use_ped
        except Exception as exc:
            log.warning('Sprint question generation fell back to base question: %s', _redact_secrets(exc))
        return f"特训：{str(item.get('title') or '经典母题')[:18]}", q_orig, use_ped

    async def start_mistake_sprint(self, count: int = 3, mode: str = 'fast', pedagogy: str = 'auto', solo_challenge: bool = False, silent_tts: bool = False, prefer_cowrite: bool = False) -> dict:
        self._ensure_mistakes_loaded()
        n_target = max(1, min(3, int(count or 3)))
        now_ts = int(time.time())
        cw_board_mids = set()
        if prefer_cowrite:
            cw_sp = getattr(self, 'cowrite', None)
            if isinstance(cw_sp, CoWriteMonitor):
                cw_sp._ensure_loaded()
                grad_sp = cw_sp._graduated_mistake_ids()
                if any(cw_sp._is_item_enrollable(x, grad_sp) for x in cw_sp.items):
                    cw_sp.enroll_mistake(enroll_all_warns=True)
                cw_board_mids = {str(x.get('mistake_id') or '').strip() for x in cw_sp.items if str(x.get('mistake_id') or '').strip()}
        active = [
            m for m in self.mistakes
            if not m.get('graduated')
            and not m.get('essay_critique')
            and not detect_writing_task(m.get('question') or '')
            and not is_essay_critique_request(m.get('question') or '', m.get('teach_mode') or 'standard')
        ]
        if prefer_cowrite:
            active.sort(key=lambda m: (
                0 if m.get('id') in cw_board_mids else (1 if str(m.get('error_tag') or '').startswith('伴写·') else 2),
                0 if int(m.get('next_review_at') or 0) <= now_ts else 1,
                int(m.get('stage') or 0),
                -int(m.get('updated_at') or 0),
            ))
        else:
            active.sort(key=lambda m: (0 if int(m.get('next_review_at') or 0) <= now_ts else 1, int(m.get('stage') or 0), -int(m.get('updated_at') or 0)))
        picked = []
        seen_subj_tags = set()
        for m in active:
            if len(picked) >= n_target:
                break
            tag_k = (m.get('subject') or 'math', m.get('error_tag') or '')
            if tag_k not in seen_subj_tags or len(active) <= n_target:
                seen_subj_tags.add(tag_k)
                if m.get('question'):
                    picked.append({'mid': m['id'], 'title': m.get('title') or '错题巩固', 'question': m.get('question') or '', 'subject': m.get('subject') or '', 'pedagogy': m.get('pedagogy') or 'auto', 'teach_mode': m.get('teach_mode') or 'standard', 'essay_critique': bool(m.get('essay_critique')), 'weak_focus': m.get('weak_focus') or m.get('error_tag') or ''})
        for m in active:
            if len(picked) >= n_target:
                break
            if m.get('question') and not any(p['mid'] == m['id'] for p in picked):
                picked.append({'mid': m['id'], 'title': m.get('title') or '错题巩固', 'question': m.get('question') or '', 'subject': m.get('subject') or '', 'pedagogy': m.get('pedagogy') or 'auto', 'teach_mode': m.get('teach_mode') or 'standard', 'essay_critique': bool(m.get('essay_critique')), 'weak_focus': m.get('weak_focus') or m.get('error_tag') or ''})
        if picked and len(picked) < n_target and (prefer_cowrite or silent_tts or pedagogy == 'junior' or all(p.get('pedagogy') == 'junior' for p in picked)):
            base_cnt = len(picked)
            while len(picked) < n_target:
                src_p = picked[(len(picked) - base_cnt) % base_cnt]
                picked.append(dict(
                    src_p,
                    title=f"{src_p['title'][:20]}·进阶",
                    weak_focus=f"{src_p.get('weak_focus') or '核心陷阱'}（换情境二阶变形加固）"[:60],
                ))
        if not picked and prefer_cowrite:
            raise ValueError('当前暂无可组卷的伴写易错题或重点题')
        if not picked and pedagogy == 'junior':
            picked.append({'mid': '', 'title': '初三判别式热身', 'question': '解一元二次方程：x² - 5x + 6 = 0，并检验两根之和与两根之积。', 'pedagogy': 'junior', 'weak_focus': '初三核心代数'})
        elif not (prefer_cowrite or (picked and pedagogy == 'junior')):
            for sp in SAMPLE_PROBLEMS:
                if len(picked) >= n_target:
                    break
                if not any(p['question'] == sp['question'] for p in picked):
                    picked.append({'mid': '', 'title': sp['title'], 'question': sp['question'], 'pedagogy': pedagogy, 'weak_focus': sp['grade']})

        sprint_id = 'sp_' + uuid.uuid4().hex[:8]
        t0, q0, ped0 = await self._build_sprint_question_spec(picked[0], mode=mode, pedagogy=pedagogy)
        s_meta = {'sprint_id': sprint_id, 'idx': 1, 'total': len(picked)}
        sess0 = self.start_live_problem(
            question=q0, mode=_norm_mode(mode), title=f"[1/{len(picked)}] {t0}"[:32],
            source='sprint', pedagogy=ped0, teach_mode='diagnose_draft' if picked[0].get('essay_critique') else 'standard',
            mistake_id=picked[0]['mid'], solo_challenge=bool(solo_challenge), sprint_meta=s_meta,
            silent_tts=bool(silent_tts),
        )
        self.sprints[sprint_id] = {
            'id': sprint_id,
            'items': picked,
            'idx': 0,
            'current_sid': sess0['id'],
            'results': [],
            'mode': _norm_mode(mode),
            'pedagogy': _norm_pedagogy(pedagogy),
            'solo_challenge': bool(solo_challenge),
            'silent_tts': bool(silent_tts),
            'created_at': now_ts,
        }
        while len(self.sprints) > 8:
            self.sprints.popitem(last=False)
        return {'ok': True, 'sprint_id': sprint_id, 'idx': 1, 'total': len(picked), 'silent': bool(silent_tts), 'session': sess0}

    async def advance_mistake_sprint(self, sprint_id: str, mode: str = 'fast', pedagogy: str = 'auto', solo_challenge=None, silent_tts=None) -> dict:
        sp = self.sprints.get(str(sprint_id or '').strip())
        if not sp or not sp.get('items'):
            raise LookupError('未找到该考前特训卷，请重新发起特训')
        if sp.get('_advancing'):
            raise RuntimeError('正在生成下一题，请稍候')
        sp['_advancing'] = True
        try:
            cur_sid = sp.get('current_sid') or ''
            cur_pub = self.public_session(cur_sid) if cur_sid else None
            cur_prog = (cur_pub or {}).get('progress') or {}
            cur_idx = sp['idx']
            cur_item = sp['items'][cur_idx]
            raw_sp_tot = cur_prog.get('total_gates')
            tot_g = int(raw_sp_tot) if raw_sp_tot is not None else 3
            clean_ok = bool(cur_prog.get('solved')) and (not cur_prog.get('peeked')) and int(cur_prog.get('weak_step') or 0) == 0 and (
                bool(cur_prog.get('solo_ace')) or int(cur_prog.get('first_try_count') or 0) >= tot_g
            )
            res_entry = {
                'idx': cur_idx + 1,
                'title': cur_item['title'],
                'focus': cur_item.get('weak_focus') or '核心关系',
                'clean_pass': clean_ok,
                'solved': bool(cur_prog.get('solved')),
                'weak_step': int(cur_prog.get('weak_step') or 0),
            }
            if cur_idx + 1 < len(sp['items']):
                nxt_idx = cur_idx + 1
                nxt_item = sp['items'][nxt_idx]
                use_m = _norm_mode(mode or sp.get('mode'))
                use_p = 'junior' if (sp.get('pedagogy') == 'junior' or nxt_item.get('pedagogy') == 'junior') else (pedagogy if pedagogy in ('elem', 'junior') else (sp.get('pedagogy') or 'auto'))
                tn, qn, ped_n = await self._build_sprint_question_spec(nxt_item, mode=use_m, pedagogy=use_p)
                if len(sp['results']) <= cur_idx:
                    sp['results'].append(res_entry)
                sp['idx'] = nxt_idx
                s_meta = {'sprint_id': sprint_id, 'idx': nxt_idx + 1, 'total': len(sp['items'])}
                use_solo = bool(solo_challenge) if isinstance(solo_challenge, bool) else bool(sp.get('solo_challenge'))
                eff_sil = bool(silent_tts) if isinstance(silent_tts, bool) else bool(sp.get('silent_tts'))
                sp['solo_challenge'] = use_solo
                sp['silent_tts'] = eff_sil
                sess_n = self.start_live_problem(
                    question=qn, mode=use_m, title=f"[{nxt_idx + 1}/{len(sp['items'])}] {tn}"[:32],
                    source='sprint', pedagogy=ped_n, teach_mode='diagnose_draft' if nxt_item.get('essay_critique') else 'standard',
                    mistake_id=nxt_item['mid'], solo_challenge=use_solo, sprint_meta=s_meta, silent_tts=eff_sil,
                )
                sp['current_sid'] = sess_n['id']
                return {'ok': True, 'completed': False, 'sprint_id': sprint_id, 'idx': nxt_idx + 1, 'total': len(sp['items']), 'silent': eff_sil, 'session': sess_n}
            if len(sp['results']) <= cur_idx:
                sp['results'].append(res_entry)
        finally:
            sp['_advancing'] = False

        clean_cnt = sum(1 for r in sp['results'] if r['clean_pass'])
        tot_cnt = len(sp['results'])
        weak_list = [f"第{r['idx']}题《{r['title'][:10]}》({r['focus'][:12]})" for r in sp['results'] if not r['clean_pass']]
        advice = (
            f'🏆 {tot_cnt} 题全部一次独立秒杀！同类易错点已稳稳拿下，明天自信上考场！'
            if clean_cnt == tot_cnt else
            f"💪 特训完成（一次满星 {clean_cnt}/{tot_cnt} 题）！考前重点再瞄一眼：{'、'.join(weak_list[:2])}！"
        )
        report = {
            'sprint_id': sprint_id,
            'total': tot_cnt,
            'clean_count': clean_cnt,
            'results': sp['results'],
            'advice': advice,
        }
        return {'ok': True, 'completed': True, 'report': report}

    async def get_foundation_capsule(self, sid: str, pedagogy: str = 'auto', silent_tts=None) -> dict:
        '''Return a 60-second foundational concept/formula visual micro-capsule with ZERO spoilers of the active problem.'''
        pub = self.public_session(sid)
        if not pub:
            raise LookupError('请先打开一道题目，再查看底层概念急救包')
        sess = self._get_or_hydrate_session(sid)
        eff_sil_f = bool(silent_tts) if isinstance(silent_tts, bool) else bool((sess or {}).get('silent_start'))
        if sess and isinstance(sess.get('foundation_capsule'), dict):
            c_hit = sess['foundation_capsule']
            if not eff_sil_f:
                self._spawn(self.synthesize_wav(f"{c_hit.get('title', '')}。{c_hit.get('principle', '')}{c_hit.get('apply_tip', '')}"[:220]), session_work=True)
            return {'ok': True, 'capsule': c_hit}
        q = str(pub.get('question') or '')
        subj = str(pub.get('subject') or detect_subject(q, teach_mode=pub.get('teach_mode') or 'standard', essay_critique=bool(pub.get('essay_critique'))))
        is_ec_cap = bool(pub.get('essay_critique') or pub.get('writing_task')) or is_essay_critique_request(q, pub.get('teach_mode') or 'standard') or bool(detect_writing_task(q))
        cap = None
        if not is_ec_cap and subj == 'math' and re.search(r'梯形', q):
            cap = {
                'title': '梯形面积为什么要「除以 2」？',
                'principle': '把两个完全一样的梯形一正一反拼在一起，刚好严丝合缝拼成一个底为「上底 + 下底」、高不变的平行四边形！',
                'example': '小模型：平行四边形面积 = (上底 + 下底) × 高，1 个梯形正好占它的一半，所以必须 ÷ 2！',
                'apply_tip': '回原题先找准互相平行的上底、下底，以及垂直于底边的那条高！',
                'draw': [
                    {'op': 'polygon', 'pts': [[90, 290], [270, 290], [220, 130], [130, 130]], 'role': 'main', 'label': '原梯形'},
                    {'op': 'polygon', 'pts': [[270, 290], [360, 290], [410, 130], [220, 130]], 'role': 'aux', 'label': '倒扣全等梯形'},
                    {'op': 'aux_line', 'x1': 130, 'y1': 130, 'x2': 130, 'y2': 290, 'label': '高h'},
                    {'op': 'brace', 'x1': 90, 'y1': 316, 'x2': 360, 'y2': 316, 'label': '拼成底 = 上底 + 下底'},
                ],
            }
        elif not is_ec_cap and subj == 'math' and re.search(r'长方形|正方形', q) and re.search(r'周长|围成|篱笆|铁丝', q):
            cap = {
                'title': '长方形周长与「1组长+宽」的秘密',
                'principle': '围成长方形一圈有 4 条边：包含 2 条长和 2 条宽，千万不能把周长直接当成「1条长 + 1条宽」！',
                'example': '小模型：若周长是 10 厘米，先除以 2 得到「1条长 + 1条宽 = 5 厘米」，再按倍数分份！',
                'apply_tip': '回原题检查：已知一圈总长时，先把周长除以 2 拆出单组「长 + 宽」最稳！',
                'draw': [
                    {'op': 'rect', 'x': 150, 'y': 120, 'w': 280, 'h': 150, 'role': 'main', 'label': '长'},
                    {'op': 'line', 'x1': 150, 'y1': 120, 'x2': 430, 'y2': 120, 'role': 'solve', 'label': '1条长'},
                    {'op': 'line', 'x1': 430, 'y1': 120, 'x2': 430, 'y2': 270, 'role': 'solve', 'label': '1条宽'},
                    {'op': 'note', 'x': 350, 'y': 340, 'label': '黄金第一步：长 + 宽 = 长方形周长 ÷ 2'},
                ],
            }
        elif not is_ec_cap and subj == 'math' and re.search(r'Rt△|直角三角形|勾股|斜边', q):
            cap = {
                'title': '勾股定理与「斜边高·等面积法」',
                'principle': '同一个直角三角形，既可以用「两直角边相乘 ÷ 2」算面积，也可以把斜边当底用「斜边 × 斜边高 ÷ 2」算面积！',
                'example': '小模型：3-4-5 直角三角形中，3² + 4² = 5²；且两直角边积 3×4 一定等于 5 × 斜边高！',
                'apply_tip': '回原题：先用勾股定理求未知边，再列等面积等式求斜边上的高！',
                'draw': [
                    {'op': 'polygon', 'pts': [[160, 110], [160, 300], [460, 300]], 'role': 'main'},
                    {'op': 'right_angle', 'x': 160, 'y': 300, 'dx': 1, 'dy': -1, 'size': 15},
                    {'op': 'aux_line', 'x1': 160, 'y1': 300, 'x2': 268, 'y2': 178, 'label': '斜边高h'},
                    {'op': 'text', 'x': 430, 'y': 150, 'label': 'a²+b²=c² 且 a×b=c×h'},
                ],
            }
        elif not is_ec_cap and subj == 'math' and re.search(r'正方体|长方体|圆柱|展开图|相对面|表面积.*(?:锯|切)|(?:锯|切).*表面积', q):
            cap = {
                'title': '立体切拼「一刀多 2 面」与展开图相对面口诀',
                'principle': '① 把长方体/圆柱每切开 1 刀，切口左右同时露出 2 个完全相同的新截面（切 n 刀多 2n 个面）！② 正方体展开图找相对面记住：「同层隔一格相对，异层 Z 字两端相对」！',
                'example': '小模型：正方体 6 个面，锯 1 刀分成两个长方体，总表面积比原来平白多出 2 个切面面积！',
                'apply_tip': '回原题数清一共切了几刀（截面数 = 刀数 × 2），并看清是平行于哪组面切的！',
                'draw': [
                    {'op': 'cube3d', 'x': 190, 'y': 145, 'w': 240, 'h': 140, 'dx': 64, 'dy': -44, 'cut': 'x', 'label': '切1刀露出2个新截面'},
                ],
            }
        elif not is_ec_cap and subj == 'math' and re.search(r'二次函数|抛物线', q):
            cap = {
                'title': '二次函数抛物线顶点与对称轴核心规律',
                'principle': '抛物线 y = ax² + bx + c 关于直线 x = −b/(2a) 左右完全轴对称！a > 0 开口向上有最低点，a < 0 开口向下有最高点！',
                'example': '小模型：若抛物线与 x 轴交于 (-1, 0) 和 (3, 0)，对称轴必在两交点正中间 x = (-1+3)÷2 = 1！',
                'apply_tip': '回原题先锁定对称轴与顶点坐标，利用左右对称点横坐标之和等于对称轴的 2 倍破题！',
                'draw': [
                    {'op': 'axes', 'ox': 250, 'oy': 290, 'xlen': 280, 'ylen': 210, 'unit': 40},
                    {'op': 'curve', 'pts': [[190, 110], [250, 230], [310, 270], [370, 230], [430, 110]], 'role': 'main', 'label': '抛物线'},
                    {'op': 'aux_line', 'x1': 310, 'y1': 85, 'x2': 310, 'y2': 320, 'label': '对称轴'},
                ],
            }
        elif not is_ec_cap and subj == 'math' and re.search(r'栽|植树|锯成|剪成|路灯|爬楼', q):
            cap = {
                'title': '间隔问题：画「2 段极简小图」永不背错',
                'principle': '不用死背加 1 还是减 1！随手画一条只有 2 小段的短线数一数：两端都种有 3 个点（点比段多 1），锯断成 2 段只需锯 1 刀（刀比段少 1）！',
                'example': '小模型：总长 ÷ 间距 = 间隔段数；两端栽树 = 段数 + 1，锯木次数 = 段数 − 1。',
                'apply_tip': '回原题先算出有几个「间隔段」，再看两端条件决定 +1 还是 −1（注意是否两旁都栽）！',
                'draw': [
                    {'op': 'seg_bar', 'x': 150, 'y': 200, 'w': 360, 'parts': 2, 'role': 'main', 'label': '2个间隔段'},
                    {'op': 'point', 'x': 150, 'y': 200, 'label': '树1', 'pos': 'top'},
                    {'op': 'point', 'x': 330, 'y': 200, 'label': '树2', 'pos': 'top'},
                    {'op': 'point', 'x': 510, 'y': 200, 'label': '树3', 'pos': 'top'},
                ],
            }
        elif not is_ec_cap and subj == 'math' and detect_calc_microscope(q) and detect_calc_microscope(q)['kind'] == 'decimal_vcalc':
            cap = {
                'title': '小数竖式对齐显微镜：认准小数点，绝不看末位！',
                'principle': '整数加减末位就是个位；但小数位数不同时（如一位小数加两位小数），只有让「小数点」上下串成一条线，相同数位（个位对个位、十分位对十分位）才真正对齐！',
                'example': '极简示范：2.4 + 1.35 中，先把 2.4 末尾补 0 看作 2.40，小数点上下对齐再相加！',
                'apply_tip': '回原题在草稿纸先画好上下对齐的小数点，缺位补 0，进退位稳拿满分！',
                'draw': sanitize_draw_ops([
                    {'op': 'vcalc', 'x': 210, 'y': 76, 'a': '2.40', 'op_sym': '+', 'b': '1.35', 'res': '？', 'rule': '小数点上下串成红虚线，空位补0', 'label': '相同数位对齐示范'},
                ]),
            }
        elif not is_ec_cap and subj == 'math' and detect_calc_microscope(q) and detect_calc_microscope(q)['kind'] == 'eq_transpose':
            cap = {
                'title': '解方程显微镜：移项过桥必变号 & 去分母不漏乘',
                'principle': '等号「=」就像天平中央的一座桥：任何项从桥左边移到右边（或右移左），实质是两边同时做逆运算，所以正负号必须立刻反转（+ 变 −，− 变 +）！',
                'example': '极简示范：2x + 3 = 9 ⇒ 把 +3 移过等号桥变成 −3，得 2x = 9 − 3！（若有分母，常数项也要乘最小公倍数！）',
                'apply_tip': '回原题每移一项先圈出它前面的符号，跨过等号立刻变号再合并同类项！',
                'draw': sanitize_draw_ops([
                    {'op': 'rect', 'x': 120, 'y': 115, 'w': 200, 'h': 70, 'role': 'main', 'label': '原式：2x + 3 = 9'},
                    {'op': 'arrow', 'x1': 320, 'y1': 150, 'x2': 415, 'y2': 150, 'role': 'aux', 'label': '+3 跨等号变 −3'},
                    {'op': 'rect', 'x': 415, 'y': 115, 'w': 200, 'h': 70, 'role': 'solve', 'label': '移项：2x = 9 − 3'},
                    {'op': 'note', 'x': 350, 'y': 315, 'label': '铁律：移项跨桥必变号；去分母每一项（含整数项）全要乘！'},
                ]),
            }
        elif not is_ec_cap and subj == 'math' and re.search(r'给.*一样多|移多补少', q):
            cap = {
                'title': '移多补少：为什么不是把差直接全给对方？',
                'principle': '多的人每拿出 1 份给少的人，多的人减 1、少的人加 1，两人的差距一下子缩小了 2 份！',
                'example': '小模型：甲有 6 个苹果、乙有 2 个（相差 4 个），甲只需给乙 4 ÷ 2 = 2 个，两人就都是 4 个啦！',
                'apply_tip': '回原题记住口诀：「先求原相差，再把差平分（÷2）才是移动量」！',
                'draw': [
                    {'op': 'seg_bar', 'x': 120, 'y': 165, 'w': 340, 'parts': 1, 'role': 'main', 'label': '多的人'},
                    {'op': 'seg_bar', 'x': 120, 'y': 255, 'w': 180, 'parts': 1, 'role': 'main', 'label': '少的人'},
                    {'op': 'aux_line', 'x1': 380, 'y1': 145, 'x2': 380, 'y2': 275, 'label': '把多出的一半移下去'},
                ],
            }
        elif not is_ec_cap and subj == 'physics' and re.search(r'电路|电流|电压|电阻|欧姆|串联|并联|滑动变阻器|电功率|Ω', q):
            cap = {
                'title': '欧姆定律与串并联电路黄金法则',
                'principle': '串联电路只有一条路：电流处处相等（I=I₁=I₂），总电压等于各部分电压之和（U=U₁+U₂），总电阻越串越大（R=R₁+R₂）！',
                'example': '欧姆定律核心公式：I = U ÷ R（求同一导体的电流、电压、电阻必须一一对应同一时刻同一元件）！',
                'apply_tip': '回原题先顺着电源正极画一圈，看清电流有几条路径、电表测的是谁！',
                'draw': [
                    {'op': 'rect', 'x': 150, 'y': 130, 'w': 380, 'h': 160, 'role': 'main', 'label': '串联闭合回路'},
                    {'op': 'arrow', 'x1': 190, 'y1': 130, 'x2': 340, 'y2': 130, 'role': 'aux', 'label': '电流I处处相等'},
                    {'op': 'text', 'x': 340, 'y': 210, 'label': 'I = U / R · 串联分压正比于电阻'},
                ],
            }
        elif not is_ec_cap and subj == 'chemistry' and re.search(r'化学方程式|质量守恒|配平|相对分子质量', q) and not re.search(r'哪端通入|长导管|短导管|洗气瓶|集气瓶', q):
            cap = {
                'title': '质量守恒定律与化学方程式定量核心',
                'principle': '化学反应前后「原子的种类、数目、质量」绝对不变！各物质参加反应的质量比恒等于「相对分子质量 × 化学计量数」之比！',
                'example': '小模型：先配平左右原子个数，再把相对分子质量乘系数对齐写在化学式正下方列比例！',
                'apply_tip': '回原题先核对化学方程式系数是否配平，再找准已知纯净物质量对应的比例关系！',
                'draw': [
                    {'op': 'rect', 'x': 115, 'y': 140, 'w': 195, 'h': 84, 'role': 'main', 'label': '反应前原子总数'},
                    {'op': 'arrow', 'x1': 318, 'y1': 182, 'x2': 382, 'y2': 182, 'role': 'aux', 'label': '原子重组守恒'},
                    {'op': 'rect', 'x': 390, 'y': 140, 'w': 195, 'h': 84, 'role': 'solve', 'label': '反应后原子总数'},
                ],
            }
        if cap is None:
            use_ped_cap = 'junior' if (sess or pub).get('source') == 'cowrite' else (pedagogy if pedagogy in ('elem', 'junior') else _norm_pedagogy((sess or pub).get('pedagogy')))
            ped_hint = resolve_pedagogy_hint(use_ped_cap, q)
            cap_rule = (
                '请用一个与原题不同场景的「1句话平淡句 vs 升格慢镜头句」极简对比小例子讲透核心写作技法（【严禁替原题代写段落或泄露原题答案】），并附 2~3 条简洁的 700×440 draw 导图框原语。'
                if is_ec_cap else (
                    '请用一个最简短易懂的日常语境迷你小例子讲透本题背后的核心语法/修辞/学科概念（【严禁出现原题原句答案或直接泄露选项】！），并附 2~3 条简洁的 700×440 draw 导图框原语。'
                    if subj in ('chinese', 'english', 'history', 'civics', 'biology', 'geography') else
                    '请用一个最简单直观的生活小例子（只用 1、2、3 这类极简玩具小数字，【严禁出现原题数字，更严禁透露原题任何步骤算式或答案】！）讲透底层原理，并附 2~4 条简洁的 700×440 draw 作图原语。'
                )
            )
            prompt = (
                f"原题：「{q}」\n{ped_hint}\n"
                '孩子正在做这道题，想花 60 秒补一下本题背后的【前置基础概念/核心方法原理】。'
                f'{cap_rule}\n'
                '只输出一行JSON（不要代码块）：{"title":"底层概念标题(14字内)","principle":"核心原理一句话讲透(55字内)","example":"极简直观小例子(45字内)","apply_tip":"回原题怎么找突破口(36字内)","draw":[{"op":"rect","x":180,"y":130,"w":320,"h":140,"label":"核心模型"}]}'
            )
            content = await self._post_chat_once('aux', [{'role': 'user', 'content': prompt}], temperature=0.25, timeout_s=24.0)
            objs, _ = extract_json_objects_from_buffer(content)
            obj = objs[0] if objs else {}
            unsolved = pub.get('steps') or []
            raw_cap_draw = copy.deepcopy(obj.get('draw')) if isinstance(obj.get('draw'), list) and obj.get('draw') else None
            scrubbed = scrub_locked_spoilers_from_qa({
                'title': str(obj.get('title') or '核心概念微课')[:22],
                'say': str(obj.get('principle') or '先抓准已知量与目标量之间的基本定义关系。')[:140],
                'board': str(obj.get('example') or '用最小数字画图试一试就能验证规律！')[:120],
                'note': str(obj.get('apply_tip') or '回到原题先标出已知条件再列关系式！')[:80],
                'draw': raw_cap_draw or [],
            }, unsolved, formula_veiled=False, strict_no_result=False, ensure_question_end=False, question_txt=q)
            cap = {
                'title': scrubbed['title'],
                'principle': scrubbed['say'],
                'example': scrubbed['board'],
                'apply_tip': scrubbed['note'],
                'draw': sanitize_draw_ops(scrubbed.get('draw') or [
                    {'op': 'rect', 'x': 170, 'y': 140, 'w': 360, 'h': 130, 'role': 'main', 'label': scrubbed['title'][:12]},
                ]),
            }
        if isinstance(cap.get('draw'), list):
            cap['draw'] = sanitize_draw_ops(cap['draw'])
        if not eff_sil_f:
            self._spawn(self.synthesize_wav(f"{cap['title']}。{cap['principle']}{cap['apply_tip']}"[:220]), session_work=True)
        if sess is not None:
            sess['foundation_capsule'] = cap
            if not str(sid).startswith('m:'):
                self._save_to_history(sess)
        return {'ok': True, 'capsule': cap}

    async def get_exam_rubric(self, sid: str, pedagogy: str = 'auto', silent_tts=None) -> dict:
        '''Generate standard full-marks exam paper writing template & 3 anti-deduction checkpoints (strictly gated until solved!).'''
        pub = self.public_session(sid)
        if not pub:
            raise LookupError('未找到该讲题记录')
        prog_rub = pub.get('progress') or {}
        if not (prog_rub.get('solved') or prog_rub.get('peeked')):
            raise ValueError('请先独立完成本题闯关，通关后即可查看考场满分卷面书写模板哦！')
        sess = self._get_or_hydrate_session(sid)
        eff_sil_rub = bool(silent_tts) if isinstance(silent_tts, bool) else bool((sess or pub).get('silent_start'))
        if sess and isinstance(sess.get('exam_rubric'), dict) and sess['exam_rubric'].get('script'):
            if sess['exam_rubric'].get('say') and not eff_sil_rub:
                self._spawn(self.synthesize_wav(sess['exam_rubric']['say'][:220]), session_work=True)
            return {'ok': True, 'exam_rubric': sess['exam_rubric']}

        q = str(pub.get('question') or '')
        subj = str(pub.get('subject') or detect_subject(q, teach_mode=pub.get('teach_mode') or 'standard', essay_critique=bool(pub.get('essay_critique'))))
        ped = 'junior' if (sess or pub).get('source') == 'cowrite' else (pedagogy if pedagogy in ('elem', 'junior') else _norm_pedagogy((sess or pub).get('pedagogy')))
        steps = [s for s in (pub.get('steps') or []) if int(s.get('step') or 0) > 0 and not s.get('is_qa')]
        s3 = next((s for s in steps if s.get('step') == 3), steps[-2] if len(steps) >= 2 else {})
        s4 = steps[-1] if steps else {}
        qz3 = (s3 or {}).get('quiz') or {}

        # Fast deterministic exam formatting for golden samples or clear multi-step math/science lessons, or LLM polish
        ped_hint = resolve_pedagogy_hint(ped, q)
        ref_board = f"第3步：{s3.get('board', '')}\n第4步：{s4.get('board', '')}"
        if pub.get('essay_critique') or pub.get('writing_task'):
            style_rule = (
                '必须按考场高分作文卷面与点睛升格规范展示（严禁代写整篇范文！）：①列出考场排版格式示范（标题居中、每段首行空两格或英文齐头分段、凤头点题句+高潮慢镜头细节示范句+豹尾升华句，限4~5行骨架） ②给出卷面涂改扣分、偏离题眼与标点一逗到底 3 条阅卷红线！'
            )
        else:
            style_rule = {
                'physics': '必须严格遵守中考物理计算规范：①先写“解：” ②必先写出原始物理字母公式（如由 I = U / R 得），严禁不写公式直接代纯数字！③代入数据时必须统一国际单位并带上单位运算（结果直接写单位，严禁像小学应用题那样给单位加括号）④简明作答！',
                'chemistry': '必须严格遵守中考化学方程式计算规范：①“解：设……的质量为 x”（注意未知数 x 后面严禁带单位 g！）②写出配平的化学方程式 ③在对应化学式正下方对齐写出相对分子质量×系数、已知质量与 x ④列比例式求解（结果带 g）⑤写“答：……”！',
                'chinese': '必须按语文阅卷采分规范分点书写：“①点明手法/角度 + ②结合原文词句具体分析特征 + ③点出深层情感/主旨”，条理标清序号！',
                'english': '必须按英语考场满分规范展示：标准句型/词形填写 + 关键语法自查（时态、三单/主谓一致、大小写与标点）！',
                'history': '必须按历史材料题“①序号化分点 + 史实/材料信息提取 + 规范历史术语结论（忌大白话）”展示标准卷面！',
                'civics': '必须按道法主观题“①教材核心观点先行 + ②结合材料具体行为分析（多主体/多角度）”展示标准卷面！',
                'biology': '必须按初中生物识图/实验探究阅卷规范：①结构与生理名词写准教材标准科学术语（严防同音错别字）②实验探究题写明对照变量与单一变量原则 ③因果简答按“结构特征 → 生理功能/现象”分条书写！',
                'geography': '必须按初中地理读图与区域综合题阅卷规范：①先核对图例、经纬网方向与等值线递变规律 ②成因与特征按自然要素（地形/气候/水文）与人文要素序号化分点书写 ③使用规范地理学科术语！',
            }.get(subj, '')
        if not style_rule:
            is_jr = ped == 'junior' or bool(re.search(r'函数|方程|Rt△|勾股|全等|相似|坐标|√', q))
            style_rule = (
                '必须按初中中考数学大题阅卷标准书写：代数应用题写齐“解：设……为 x(含单位) → 根据题意列方程 → 解得 x=… →（分式/实际问题必写检验）→ 答”；几何题写规范“解/证明：∵ … ∴ … (定理依据)”！'
                if is_jr else
                '必须按小学数学考场满分应用题规范书写：分步或综合算式（综合算式先算加减务必加小括号，每步得数后括号内写准单位名称！）→ 最后另起一行写完整“答：……”（把题目所问完整答出，一题多问切勿漏答第二问）！'
            )

        prompt = (
            f"原题：「{q}」\n参考推导：\n{ref_board}\n"
            f"【卷面规范要求】：{style_rule}\n"
            '请为孩子生成一份可直接模仿抄写习惯的【考场满分卷面标准书写示范】以及【3条阅卷老师最常扣过程分的避坑红线】。\n'
            '只输出一行JSON（不要代码块）：{"badge":"满分卷面规范","title":"考场标准书写示范","script":"按真实考卷分行排版的完整规范书写过程（用 \\n 换行，控制在4~6行内）","checkpoints":["防扣分点1(24字内)","防扣分点2(24字内)","防扣分点3(24字内)"],"say":"35~55字温暖口播叮嘱最关键的1~2个防扣过程分细节"}'
        )
        try:
            content = await self._post_chat_once('aux', [{'role': 'user', 'content': prompt}], temperature=0.2, timeout_s=24.0)
            objs, _ = extract_json_objects_from_buffer(content)
            obj = objs[0] if objs else {}
        except Exception:
            obj = {}
        cks_raw = obj.get('checkpoints') if isinstance(obj.get('checkpoints'), list) else []
        cks = [str(x).strip()[:42] for x in cks_raw[:3] if str(x).strip()]
        if len(cks) < 2:
            if pub.get('essay_critique') or pub.get('writing_task'):
                cks = ['标题居中，每段首行空两格（英文齐头或缩进统一）', '高潮段必有动作/五感慢镜头，忌平铺直叙流水账', '首尾呼应紧扣题眼词，字迹工整忌大团涂改']
            elif subj == 'physics':
                cks = ['必先写原始物理公式（如 I=U/R），直接代数字零分', '代入前先换算为国际标准单位，结果单位不加括号', '多问计算标清角标（如 U₁、R总）防混淆']
            elif subj == 'chemistry':
                cks = ['「设…质量为 x」未知数 x 后严禁带单位 g', '化学方程式必须配平并标齐反应条件与 ↑↓', '相对分子质量乘化学计量数对齐写在化学式正下方']
            elif subj == 'english':
                cks = ['仔细核对时态助动词与主谓一致（第三人称单数）', '检查不规则动词过去式/过去分词拼写与大小写', '代回完整原句默读一遍，检验介词搭配与句末标点']
            elif subj == 'chinese':
                cks = ['按「①手法/角度 ②结合原文词句分析 ③情感主旨」分点标号', '切忌脱离文本空套术语，必须引用原文关键词作证', '看清分值分条作答，字迹清爽不超答题卡边框']
            elif subj in ('history', 'civics'):
                cks = ['采用「① ② ③」序号化分点，教材核心术语先行', '史论结合/观点扣材料，严禁整段照抄材料或只写大白话', '看清多主体/多角度设问要求，避免单一维度漏采分点']
            elif subj in ('biology', 'geography'):
                cks = ['专有名词/结构名称写准规范科学术语，严防同音错别字', '读图题先核对图例、方向标与横纵坐标单位', '因果简答按「条件变化 → 中间机制 → 最终表现」写完整']
            elif ped == 'elem':
                cks = ['每步算式得数末尾必须在括号里写准单位名称', '综合算式若需先算加减，千万别漏写小括号', '最后必须写完整「答：……」，两问都要答全']
            else:
                cks = ['设元或几何作高需先写清「解：设…」或辅助线交代', '关键定理推导写明「∵ … ∴ …」，切勿跳步丢过程分', '算完检查单位、取值范围及分式方程检验语']
        ans3_txt = ''
        ch3 = qz3.get('chain') if isinstance(qz3.get('chain'), list) else []
        if ch3 and isinstance(ch3[-1], dict) and ch3[-1].get('ans'):
            ans3_txt = f"{ch3[-1]['ans']}{ch3[-1].get('unit') or qz3.get('unit') or ''}"
        elif qz3.get('num_ans'):
            ans3_txt = f"{qz3['num_ans']}{qz3.get('unit') or ''}"
        elif isinstance(qz3.get('opts'), list) and 0 <= int(qz3.get('ans') or 0) < len(qz3['opts']):
            ans3_txt = str(qz3['opts'][int(qz3.get('ans') or 0)]).strip()
        fallback_script = str(s4.get('board') or '').strip() or (
            f"规范作答要点：{ans3_txt}" if (pub.get('essay_critique') or pub.get('writing_task') or subj in ('chinese', 'english', 'history', 'civics', 'biology', 'geography'))
            else f"解：按题意分步列式求解得 {ans3_txt or '所求结果'}\n答：{ans3_txt or '核对无误'}。"
        )
        rubric = {
            'badge': str(obj.get('badge') or '📝 考场满分规范')[:18],
            'title': str(obj.get('title') or '考场标准书写示范')[:24],
            'script': _coerce_multiline_str(obj.get('script') or fallback_script, 420),
            'checkpoints': cks[:3],
            'say': str(obj.get('say') or (
                f"构思升格完成啦，写进作文本时记得注意：{cks[0]}，卷面加分更出彩哦！"
                if (pub.get('essay_critique') or pub.get('writing_task')) else (
                    f"核心要点答准啦，上考场落笔记得注意：{cks[0]}，把采分点稳稳拿满哦！"
                    if subj in ('chinese', 'english', 'history', 'civics', 'biology', 'geography') else
                    f"得数算对啦，上考场记得注意：{cks[0]}，把步骤分稳稳拿满哦！"
                )
            ))[:160],
        }
        if sess is not None:
            sess['exam_rubric'] = rubric
            if not str(sid).startswith('m:'):
                self._save_to_history(sess)
        if not eff_sil_rub:
            self._spawn(self.synthesize_wav(rubric['say'][:220]), session_work=True)
        return {'ok': True, 'exam_rubric': rubric}

    async def get_archetype_mapping(self, sid: str, pedagogy: str = 'auto', silent_tts=None) -> dict:
        '''Generate post-solve cross-domain Skin-vs-Bone Archetype Mapping Table (strictly gated until solved!).'''
        pub = self.public_session(sid)
        if not pub:
            raise LookupError('未找到该讲题记录')
        prog_arch = pub.get('progress') or {}
        if not (prog_arch.get('solved') or prog_arch.get('peeked')):
            raise ValueError('请先独立完成本题闯关，通关后即可揭秘「同核不同皮·母题变脸图谱」哦！')
        sess = self._get_or_hydrate_session(sid)
        eff_sil_am = bool(silent_tts) if isinstance(silent_tts, bool) else bool((sess or pub).get('silent_start'))
        sp_id = str((sess or pub).get('sample_id') or '')
        if sp_id and sp_id in SAMPLE_ARCHETYPE_MAPS and not (sess and sess.get('archetype_map')):
            am_built = copy.deepcopy(SAMPLE_ARCHETYPE_MAPS[sp_id])
            if sess is not None:
                sess['archetype_map'] = am_built
            if not eff_sil_am:
                self._spawn(self.synthesize_wav(am_built['say'][:220]), session_work=True)
            return {'ok': True, 'archetype_map': am_built}
        if sess and isinstance(sess.get('archetype_map'), dict) and sess['archetype_map'].get('rows'):
            cached_am = sess['archetype_map']
            if cached_am.get('say') and not eff_sil_am:
                self._spawn(self.synthesize_wav(cached_am['say'][:220]), session_work=True)
            return {'ok': True, 'archetype_map': cached_am}

        q_txt = str(pub.get('question') or '').strip()
        is_ec_am = bool(pub.get('essay_critique') or pub.get('writing_task'))
        subj_am = str(pub.get('subject') or detect_subject(q_txt, teach_mode=pub.get('teach_mode') or 'standard', essay_critique=bool(pub.get('essay_critique'))))
        use_ped_am = 'junior' if (sess or pub).get('source') == 'cowrite' else (pedagogy if pedagogy in ('elem', 'junior') else _norm_pedagogy((sess or pub).get('pedagogy')))
        ped_hint = resolve_pedagogy_hint(use_ped_am, q_txt)
        cur_board = '\n'.join(str(s.get('board') or '') for s in (pub.get('steps') or []) if int(s.get('step') or 0) > 0)
        if is_ec_am:
            arch_task = (
                '孩子已完成这段素材的审题/升格！请帮他掌握考场高分绝招「一材多用·母题迁移图谱」：提炼这段真实生活素材的底层情感与细节母核，并展示如何只微调开头点题句与结尾升华句，就无缝迁移到考场另外 2 道高频作文题（严禁代写整篇范文）！\n'
                '只输出一行JSON（不要代码块）：{"title":"一材多用：考场迁移图谱","badge":"🔗 一材多用","core_bone":"一句话提炼该素材可跨题复用的核心细节与情感母核(55字内)","headers":["迁移维度","本题立意切口","高频考题①","高频考题②"],"rows":[["适用文题","本题题目/场景","成长励志类考题","温情瞬间类考题"],["核心慢镜头","保留高光动作细节","聚焦咬牙突破瞬间","聚焦眼神动作温暖"],["首尾扣题词","紧扣本题题眼","换扣坚韧/蜕变","换扣陪伴/懂得"]],"insight":"一句话总结考场如何一材多用不跑题(55字内)","say":"55~80字口语教孩子怎样把打磨好的一个精品素材灵活套进不同考场作文题"}'
            )
            def_hdrs = ['迁移维度', '本题切口', '高频考题①', '高频考题②']
            def_rows = [
                ['核心慢镜头', '保留高光细节', '聚焦突破瞬间', '聚焦触动瞬间'],
                ['首尾点题钩', '紧扣本题题眼', '转向成长蜕变', '转向感恩懂得'],
            ]
        elif subj_am in ('chinese', 'english', 'history', 'civics', 'biology', 'geography'):
            arch_task = (
                '孩子已成功解出这道题！请帮他拆穿出题人的「同核不同皮·母题变脸魔术」：提炼本题脱掉具体语境/材料外衣后的底层语法/阅读/学科考点骨相，并举出考场上最常换皮出现的另外 2 种变身设问，按 4 列表格逐行一一对应映射！\n'
                '只输出一行JSON（不要代码块）：{"title":"母题变脸识破：...考点(14字内)","badge":"🔗 同核不同皮","core_bone":"一句话讲透脱掉语境马甲后的底层考点骨相(55字内)","headers":["底层考点要素","本题语境皮相","考场变身①","考场变身②"],"rows":[["标志线索","本题关键信号","变身①常见信号","变身②常见信号"],["易错陷阱","本题干扰项","变身①干扰项","变身②干扰项"],["满分通法","...","...","..."]],"insight":"一句话总结如何一眼识破同类变身设问(55字内)","say":"55~80字生动口语带孩子看穿这三种问法其实考的是同一个底层考点"}'
            )
            def_hdrs = ['底层考点', '本题语境', '考场变身①', '考场变身②']
            def_rows = [
                ['题眼信号词', '本题标志线索', '同类新语境①', '同类新材料②'],
                ['满分答题链', '定位依据→结论', '找准标志→排除', '扣准术语→分点'],
            ]
        else:
            arch_task = (
                '孩子已经成功解出这道题！请帮他拆穿出题人的「同核不同皮·母题变脸魔术」：提炼这道题脱掉故事外衣后的底层数学/物理/学科骨相，并举出考场上最常伪装成的另外 2 种跨场景变身题，按 4 列表格逐行一一对应映射！\n'
                '只输出一行JSON（不要代码块）：{"title":"母题变脸识破：...模型(14字内)","badge":"🔗 同核不同皮","core_bone":"一句话讲透脱掉故事马甲后的底层骨相(55字内)","headers":["底层骨相要素","本题故事皮相","考场变身①","考场变身②"],"rows":[["要素1(8字内)","本题对应(12字内)","变身①对应(12字内)","变身②对应(12字内)"],["要素2","...","...","..."],["破题通法","...","...","..."]],"insight":"一句话总结如何一眼看穿同类马甲题(55字内)","say":"55~80字生动口语带孩子看穿这三种题其实是同一道母题"}'
            )
            def_hdrs = ['底层骨相', '本题皮相', '考场变身①', '考场变身②']
            def_rows = [
                ['核心不变量', '本题已知条件', '生活工程场景', '竞赛综合场景'],
                ['破题桥梁', '按底层关系转化', '同构等量代换', '逆向检验核对'],
            ]
        prompt = (
            f"原题：「{q_txt}」\n{ped_hint}\n"
            f"本题板书核心推导：\n{cur_board[:460]}\n\n"
            f"{arch_task}"
        )
        if not eff_sil_am:
            await self.pause_music_if_playing()
        content = await self._post_chat_once('aux', [{'role': 'user', 'content': prompt}], temperature=0.3, timeout_s=24.0)
        objs, _ = extract_json_objects_from_buffer(content)
        obj = objs[0] if objs else {}
        hdrs_in = obj.get('headers') if isinstance(obj.get('headers'), list) and len(obj.get('headers')) >= 3 else def_hdrs
        col_n = min(4, max(3, len(hdrs_in)))
        hdrs = [str(hdrs_in[i]).strip()[:16] for i in range(col_n)]
        rows_clean = []
        for r in (obj.get('rows') or [])[:4]:
            if isinstance(r, list) and len(r) >= 2:
                r_cells = []
                for i in range(col_n):
                    rc_s = str(r[i] if i < len(r) else '').strip()
                    if len(rc_s) > 38:
                        sp_cut = rc_s.rfind(' ', 22, 38)
                        rc_s = (rc_s[:sp_cut].rstrip(' ,;:/／') + '…') if sp_cut > 0 else (rc_s[:37].rstrip(' ,;:/／、') + '…')
                    r_cells.append(rc_s)
                rows_clean.append(r_cells)
        if not rows_clean:
            rows_clean = def_rows
        def_am_title = '一材多用：考场迁移图谱' if is_ec_am else ('母题变脸识破：同考点迁移' if subj_am in ('chinese', 'english', 'history', 'civics', 'biology', 'geography') else '母题变脸识破：跨场景同构模型')
        def_am_bone = (
            '打磨透一个有画面感的真实细节片段，微调首尾点题词即可跨题复用！'
            if is_ec_am else (
                '抓住脱掉材料外衣后的核心考点标志词与采分逻辑，新语境也能一眼认出！'
                if subj_am in ('chinese', 'english', 'history', 'civics', 'biology', 'geography') else
                '抓住已知量与目标量之间的底层不变关系，无论故事背景怎么换都能一招破题！'
            )
        )
        def_am_ins = (
            '考场遇到新半命题或话题作文别慌：保留打磨好的高潮慢镜头，首尾紧扣新题眼词就不跑题！'
            if is_ec_am else (
                '脱掉新材料外衣，先圈标志词锁定底层考点，吃透一题就能举一反三！'
                if subj_am in ('chinese', 'english', 'history', 'civics', 'biology', 'geography') else
                '脱掉题目里的生活故事马甲，认准底层的数量关系骨架，做一道题就能通杀十道变身题！'
            )
        )
        def_am_say = (
            '学会一材多用啦！把这个打磨好的慢镜头细节装进素材百宝箱，考场上遇到不同题目只要微调首尾点题句就能稳拿高分！'
            if is_ec_am else (
                '看穿出题人的变脸术啦！不管语境材料怎么换，核心考点标志一抓一个准！'
                if subj_am in ('chinese', 'english', 'history', 'civics', 'biology', 'geography') else
                '看穿出题人的变脸术啦！不管题目换成什么生活故事，底层的数量关系骨架完全一样，抓住核心桥梁就秒解啦！'
            )
        )
        am = {
            'title': str(obj.get('title') or def_am_title)[:24],
            'badge': str(obj.get('badge') or ('🔗 一材多用' if is_ec_am else '🔗 同核不同皮'))[:12],
            'core_bone': str(obj.get('core_bone') or def_am_bone)[:110],
            'headers': hdrs,
            'rows': rows_clean,
            'insight': str(obj.get('insight') or def_am_ins)[:110],
            'say': str(obj.get('say') or def_am_say)[:200],
        }
        if sess is not None:
            sess['archetype_map'] = am
            sess['seq'] = int(sess.get('seq') or 1) + 1
            if not str(sid).startswith('m:'):
                self._save_to_history(sess)
        if not eff_sil_am:
            self._spawn(self.synthesize_wav(am['say'][:220]), session_work=True)
        return {'ok': True, 'archetype_map': am}

    async def evaluate_read_aloud(self, sid: str, spoken_text: str, silent_tts=None) -> dict:
        '''Evaluate student spoken read-aloud fluency/completeness for English sentences & Classical Chinese poetry.'''
        pub = self.public_session(sid)
        if not pub:
            raise LookupError('未找到该讲题记录')
        sess_ra = self._get_or_hydrate_session(sid)
        eff_sil_ra = bool(silent_tts) if isinstance(silent_tts, bool) else bool((sess_ra or pub).get('silent_start'))
        prog_ra = pub.get('progress') or {}
        if not (prog_ra.get('solved') or prog_ra.get('peeked')):
            raise ValueError('请先完成本题闯关解锁完整原句，再来跟读闯关哦！')
        target = extract_read_aloud_target(pub.get('question') or '', pub.get('steps') or [])
        if not target:
            raise ValueError('当前题目无需语感跟读评测')
        spoken_raw = str(spoken_text or '').strip()[:200]
        if not spoken_raw:
            raise ValueError('没听清跟读声音，请靠近屏幕大声朗读一遍哦')

        if target['kind'] == 'english':
            _en_num_map = {
                'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5',
                'six': '6', 'seven': '7', 'eight': '8', 'nine': '9', 'ten': '10',
                'eleven': '11', 'twelve': '12', 'thirteen': '13', 'fourteen': '14', 'fifteen': '15',
                'sixteen': '16', 'seventeen': '17', 'eighteen': '18', 'nineteen': '19', 'twenty': '20',
                'thirty': '30', 'forty': '40', 'fifty': '50', 'sixty': '60', 'seventy': '70',
                'eighty': '80', 'ninety': '90', 'hundred': '100',
            }

            def _norm_en_contractions(t_in: str) -> str:
                t_low = t_in.lower().replace('’', "'")
                for k_c, v_c in (
                    ("won't", 'will not'), ("can't", 'can not'), ("shan't", 'shall not'),
                    ("n't", ' not'), ("'re", ' are'), ("'ve", ' have'), ("'ll", ' will'),
                    ("i'm", 'i am'), ("it's", 'it is'), ("he's", 'he is'), ("she's", 'she is'),
                    ("that's", 'that is'), ("there's", 'there is'), ("what's", 'what is'), ("let's", 'let us'),
                ):
                    t_low = t_low.replace(k_c, v_c)
                return t_low

            def _tok_en(t_in: str) -> list:
                return [_en_num_map.get(w, w) for w in re.findall(r"[a-z0-9']+", _norm_en_contractions(t_in))]

            exp_tokens = _tok_en(target['text'])
            got_tokens = _tok_en(spoken_raw)
            got_set = set(got_tokens)
            matched = sum(1 for w in exp_tokens if w in got_set)
            ratio = matched / max(1, len(exp_tokens))
            focus_toks = _tok_en(target.get('focus') or '')
            focus_ok = all(fw in got_set for fw in focus_toks) if focus_toks else True
            passed = ratio >= 0.72 and focus_ok
            missed = [w for w in exp_tokens if w not in got_set][:3]
            if passed:
                badge = '🌟 英语语感之星'
                comment = f"发音清晰饱满，完整读出了「{target['text']}」，核心语法词组稳稳上口！"
                say = '哇，读得又流利又地道，时态和连读的语感太棒啦，给你点个大大的赞！'
            else:
                badge = '🎙️ 再读一遍更地道'
                miss_hint = f"（重点留意词：{' '.join(missed)}）" if missed else ''
                comment = f"已经读出了大部分内容{miss_hint}！先点「慢速领读」听清重音和词尾，再大声跟读一次就满分啦！"
                say = '开口大声读非常棒！个别单词的尾音再读饱满一点就完美啦，跟着慢速领读再试一次吧！'
        else:
            _cn_homo_groups = (
                '洲州', '只知织支', '重崇虫', '还环桓', '庐芦炉卢', '唯惟维', '坐座', '反返',
                '度渡镀', '畔伴绊', '啼题提', '销消霄宵', '采彩睬', '暮幕慕墓', '峰锋烽',
                '壁璧碧', '蓝篮兰栏', '园圆原源缘', '长常偿尝', '生升声胜', '清青轻倾',
                '陋漏露', '馨新薪心欣', '铭明鸣名', '灵玲零铃凌', '斯私思司丝', '鸿洪红虹', '儒如茹',
            )
            _cn_homo_map = {ch_h: grp[0] for grp in _cn_homo_groups for ch_h in grp}
            exp_chars = re.findall(r'[\u4e00-\u9fa5]', target['text'])
            got_list = re.findall(r'[\u4e00-\u9fa5]', spoken_raw)
            got_canon = {_cn_homo_map.get(ch, ch) for ch in got_list}
            matched = sum(1 for ch in exp_chars if _cn_homo_map.get(ch, ch) in got_canon)
            ratio = matched / max(1, len(exp_chars))
            passed = ratio >= 0.78 and len(got_list) >= max(4, int(len(exp_chars) * 0.72))
            missed_cn = []
            for ch in exp_chars:
                if _cn_homo_map.get(ch, ch) not in got_canon and ch not in missed_cn:
                    missed_cn.append(ch)
                if len(missed_cn) >= 3:
                    break
            couplets_cn = [
                re.findall(r'[\u4e00-\u9fa5]', seg)
                for seg in re.split(r'[。！？；\n]+', target['text'])
                if len(re.findall(r'[\u4e00-\u9fa5]', seg)) >= 8
            ]
            partial_couplet_ok = (
                not passed
                and len(couplets_cn) >= 2
                and len(got_list) <= int(len(exp_chars) * 0.72)
                and any(
                    sum(1 for ch in cp if _cn_homo_map.get(ch, ch) in got_canon) / len(cp) >= 0.85
                    for cp in couplets_cn
                )
            )
            if passed:
                badge = '🌟 古诗文诵读名家'
                comment = f"字正腔圆！按「{target['display']}」的停顿节奏把意境全都读出来啦！"
                say = '字正腔圆，停顿节奏特别有古诗文的韵味，画面感一下子就出来啦！'
            elif partial_couplet_ok:
                badge = '🎙️ 请连读整段诗句'
                comment = f"这句名句读得很准！请从头把整段「{target['display']}」完整连读一遍就满分啦！"
                say = '这句读得非常准！再来从第一句开始，把屏幕上整段诗句完整诵读一遍就通关啦！'
            else:
                badge = '🎙️ 注意字音与停顿'
                miss_cn_txt = f"（留意「{'、'.join(missed_cn)}」单字读音）" if missed_cn else ''
                comment = f"气势不错{miss_cn_txt}！对照「{target['display']}」的斜线停顿，放慢一点再诵读一遍吧！"
                say = '声音很洪亮！注意看屏幕上的斜线停顿节奏，把每个字咬准再慢读一遍就满分啦！'

        if passed:
            self.update_progress(sid, {'read_aloud_ok': True})
        if not eff_sil_ra:
            self._spawn(self.synthesize_wav(say[:220]), session_work=True)
        return {
            'ok': True,
            'read_aloud': {
                'pass': passed,
                'badge': badge,
                'score': int(round(min(100, ratio * 100))),
                'comment': comment,
                'say': say,
                'display': target['display'],
            },
            'session': self.public_session(sid),
        }

    async def get_polya_reduction(self, sid: str, pedagogy: str = 'auto', silent_tts=None) -> dict:
        '''Polya Special-Case Toy Sandbox: reduce large/complex problems to n=1, 2, 3 tiny cases to discover the pattern without spoiling the answer!'''
        pub = self.public_session(sid)
        if not pub:
            raise LookupError('请先打开一道题目')
        sess = self._get_or_hydrate_session(sid)
        eff_sil_pl = bool(silent_tts) if isinstance(silent_tts, bool) else bool((sess or pub).get('silent_start'))
        if sess and isinstance(sess.get('polya_sandbox'), dict):
            sb_hit = sess['polya_sandbox']
            if not eff_sil_pl:
                self._spawn(self.synthesize_wav(f"遇到大数别怕，我们先退一步看最小的三个例子。{sb_hit.get('rule_q', '')}"[:220]), session_work=True)
            return {'ok': True, 'sandbox': sb_hit}
        q = str(pub.get('question') or '')
        use_ped_pl = 'junior' if (sess or pub).get('source') == 'cowrite' else (pedagogy if pedagogy in ('elem', 'junior') else _norm_pedagogy((sess or pub).get('pedagogy')))
        ped_hint = resolve_pedagogy_hint(use_ped_pl, q)
        prompt = (
            f"原题：「{q}」\n{ped_hint}\n"
            '孩子面对这道题的大数字或复杂结构有点发怵。请运用波利亚《怎样解题》中的【特例化降维破冰法】：'
            '把原题临时缩小成最简单的 n=1、n=2、n=3 三个迷你双胞胎小特例列出来让孩子一眼看出递增/不变规律，并配一张直观的 table 或 seg_bar 对比图（【绝对红线：严禁出现原题最终答案数字，只展示前3个极简小特例并抛出找规律问题】）！\n'
            '只输出一行JSON（不要代码块）：{"title":"🪄 波利亚降维试算：先看最小3个例子","cases":["特例① (最小情况)：...","特例② (往后一步)：...","特例③ (再往后一步)：..."],"rule_q":"对比上面3个小例子，每多1步数量是怎么变化的？","bridge":"抓到这个固定变化规律后，再套回原题大数字就能一招算穿！","draw":[{"op":"table","x":90,"y":85,"w":520,"headers":["序号","小特例图示","对应数量"],"rows":[["第1个","基础项","..."],["第2个","多1步","..."],["第3个","多2步","..."]],"label":"降维找规律表"}]}'
        )
        content = await self._post_chat_once('aux', [{'role': 'user', 'content': prompt}], temperature=0.25, timeout_s=32.0)
        objs, _ = extract_json_objects_from_buffer(content)
        obj = objs[0] if objs else {}
        unsolved = pub.get('steps') or []
        raw_cases = obj.get('cases') if isinstance(obj.get('cases'), list) else ['特例①：先把数字换成 1 画图试一试', '特例②：换成 2 看看增加了多少', '特例③：换成 3 验证每次增加的固定规律']
        joined_cases = '\n'.join(str(c).strip()[:64] for c in raw_cases[:3])
        raw_sb_draw = copy.deepcopy(obj.get('draw')) if isinstance(obj.get('draw'), list) and obj.get('draw') else None
        scrubbed = scrub_locked_spoilers_from_qa({
            'title': str(obj.get('title') or '🪄 波利亚降维找规律')[:24],
            'say': str(obj.get('rule_q') or '对比这 3 个最小特例，你发现每往后一步固定的变化规律了吗？')[:130],
            'board': joined_cases,
            'note': str(obj.get('bridge') or '先在小数字上抓准规律，再代回原题！')[:84],
            'draw': raw_sb_draw or [],
        }, unsolved, formula_veiled=False, strict_no_result=False, question_txt=q)
        sandbox = {
            'title': scrubbed['title'],
            'cases': [ln for ln in scrubbed['board'].split('\n') if ln.strip()][:3],
            'rule_q': scrubbed['say'],
            'bridge': scrubbed['note'],
            'draw': sanitize_draw_ops(scrubbed.get('draw') or [
                {'op': 'table', 'x': 95, 'y': 90, 'w': 510, 'headers': ['小特例', '观察重点'], 'rows': [['n = 1', '看起始基准量'], ['n = 2', '看增加第1个差量'], ['n = 3', '验证固定递增规律']], 'label': '波利亚降维表'},
            ]),
        }
        if not eff_sil_pl:
            self._spawn(self.synthesize_wav(f"遇到大数别怕，我们先退一步看最小的三个例子。{sandbox['rule_q']}"[:220]), session_work=True)
        if sess is not None:
            sess['polya_sandbox'] = sandbox
            if not str(sid).startswith('m:'):
                self._save_to_history(sess)
        return {'ok': True, 'sandbox': sandbox}

    async def inspect_board_scratch(self, sid: str, image_bytes: bytes, step_idx: int = 0, pedagogy: str = 'auto', solo_mode: bool = False, silent_tts=None) -> dict:
        '''Inspect student hand-drawn chalk strokes/calculations on the blackboard without revealing answers.'''
        sess = self._get_or_hydrate_session(sid)
        if not sess:
            raise LookupError('未找到当前微课会话')
        eff_sil_sc = bool(silent_tts) if isinstance(silent_tts, bool) else bool(sess.get('silent_start'))
        if not image_bytes or len(image_bytes) < 128 or len(image_bytes) > 3 * 1024 * 1024:
            raise ValueError('手写板书快照无效')
        image_bytes = await self.phone_studio.normalize_to_jpeg(image_bytes)
        all_steps = sess.get('steps') or []
        safe_idx = 0 if solo_mode else (max(0, min(len(all_steps) - 1, int(step_idx or 0))) if all_steps else 0)
        cur_st = all_steps[safe_idx] if all_steps else {}
        eff_ped_sc = 'junior' if sess.get('source') == 'cowrite' else (pedagogy if pedagogy in ('elem', 'junior') else _norm_pedagogy(sess.get('pedagogy')))
        ped_hint = resolve_pedagogy_hint(eff_ped_sc, sess.get('question') or '')
        b64 = base64.b64encode(image_bytes).decode('ascii')
        stage_ctx_ln = (
            '当前处于【去拐杖独立实战模式】（请只点评孩子手写辅助线/草稿的思路方向或符号细节，严禁剧透分步中间量或最终答案）'
            if solo_mode else
            f"当前闯关步骤：第 {cur_st.get('step', 1)} 步「{cur_st.get('title', '')}」，问题：{(cur_st.get('quiz') or {}).get('q', '')}"
        )
        prompt = (
            f"原题：「{sess.get('question') or sess.get('title') or ''}」\n"
            f"{stage_ctx_ln}\n"
            f"{ped_hint}\n"
            '图片是电子黑板及孩子刚刚用粉笔在黑板上亲手画的辅助线/圈注/手写算式草稿。'
            '请你像身边温柔智慧的老师一样仔细看一眼孩子的手写笔迹：先肯定他画对或想对的地方，再指出笔迹里是否有符号、份数、垂足位置或单位小偏差，并在结尾用一个启发小问题引导他下一步（【绝对红线：严禁说出本步或最终数字得数、严禁写出等号后的结果、严禁报出选项字母 A/B/C】）！\n'
            '只输出一行JSON（不要代码块）：{"title":"👀 手写思路点评","say":"45~78字温暖口语针对性点拨，结尾带启发反问","board":"✓ 亮点肯定：...\\n🧭 下一步小提醒：...(严禁含未解锁得数)","note":"一句话点拨"}'
        )
        messages = [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + b64}},
            ],
        }]
        if not eff_sil_sc:
            await self.pause_music_if_playing()
        content = await self._post_chat_once('aux', messages, temperature=0.2, timeout_s=26.0)
        objs, _ = extract_json_objects_from_buffer(content)
        obj = objs[0] if objs else {'title': '👀 手写思路点评', 'say': '老师看到你在黑板上的演算啦，思路方向很棒，再对照已知条件核对一下每一份的关系吧？', 'board': '已查看黑板手写笔迹，请顺着已知条件继续推导！', 'note': ''}
        unsolved_steps = all_steps[safe_idx:] if all_steps else []
        cur_qz_sc = (cur_st.get('quiz') or {}) if isinstance(cur_st, dict) else {}
        sc_veil_formula = bool(solo_mode or cur_qz_sc.get('mode') == 'num')
        inline_qa = scrub_locked_spoilers_from_qa({
            'title': str(obj.get('title') or '👀 手写思路点评')[:24],
            'say': _coerce_multiline_str(obj.get('say') or '老师看到你的粉笔演算啦，继续顺着图上的关系想一想吧？', 260),
            'board': _coerce_multiline_str(obj.get('board') or obj.get('say') or '', 260),
            'note': str(obj.get('note') or '')[:46],
            'at_idx': safe_idx,
        }, unsolved_steps, formula_veiled=sc_veil_formula, strict_no_result=True, question_txt=str(sess.get('question') or ''))
        sess['active_qa'] = inline_qa
        sess['seq'] += 1
        if not eff_sil_sc:
            self._spawn(self.synthesize_wav(inline_qa['say'], priority=False), session_work=True)
        return {'ok': True, 'inline_qa': inline_qa, 'session': self.public_session(sid)}

    def _extract_subq_conclusion(self, sess_or_pub: dict, part_lbl: str) -> str:
        steps = (sess_or_pub or {}).get('steps') or []
        for st in reversed(steps):
            if not isinstance(st, dict) or st.get('is_qa'):
                continue
            qz = st.get('quiz')
            if isinstance(qz, dict):
                chain = qz.get('chain')
                if isinstance(chain, list) and chain and chain[-1].get('ans'):
                    last_r = chain[-1]
                    return f"{part_lbl}已求出：{last_r['ans']}{last_r.get('unit') or qz.get('unit') or ''}"
                if qz.get('num_ans'):
                    return f"{part_lbl}已求出：{qz['num_ans']}{qz.get('unit') or ''}"
                opts = qz.get('opts')
                ans_i = qz.get('ans', 0)
                if isinstance(opts, list) and 0 <= ans_i < len(opts) and int(st.get('step') or 0) >= 3:
                    return f"{part_lbl}已证结论：{str(opts[ans_i]).strip()[:26]}"
        for st in reversed(steps):
            if not isinstance(st, dict) or st.get('is_qa'):
                continue
            bd = str(st.get('board') or '')
            m_oks = [
                x.strip() for x in re.findall(r'([^\n✓]{2,32}✓)', bd)
                if not re.search(r'^(?:✅\s*)?(?:逆向)?(?:验算|检验\d*|复盘)|复盘(?:核验|检验)|核对无误|等量关系|自查一遍', x.strip())
            ]
            if m_oks:
                return f"{part_lbl}结论：{m_oks[-1]}"
        return f"{part_lbl}已完成推导"

    async def advance_subquestion(self, sid: str, target_idx: int = None, mode: str = 'fast', pedagogy: str = 'auto', solo_challenge: bool = False, silent_tts=None) -> dict:
        pub = self.public_session(sid)
        if not pub or not isinstance(pub.get('subq_meta'), dict):
            raise LookupError('当前题目不含多小问递进拆分信息')
        meta = copy.deepcopy(pub['subq_meta'])
        parts = meta.get('parts') or []
        cur_idx = int(meta.get('cur_idx') or 1)
        nxt_idx = int(target_idx) if target_idx is not None else (cur_idx + 1)
        if nxt_idx < 1 or nxt_idx > len(parts):
            raise ValueError('已经是最后一小问啦')
        proven = list(meta.get('proven') or [])
        prog_sq = pub.get('progress') or {}
        if (prog_sq.get('solved') or prog_sq.get('peeked')) and cur_idx < nxt_idx:
            cur_lbl = parts[cur_idx - 1]['label']
            concl = self._extract_subq_conclusion(pub, cur_lbl)
            proven = [p for p in proven if not p.startswith(cur_lbl)]
            proven.append(concl)
        nxt_part = parts[nxt_idx - 1]
        new_meta = {
            'full_question': meta.get('full_question') or '',
            'stem': meta.get('stem') or '',
            'parts': parts,
            'cur_idx': nxt_idx,
            'total': len(parts),
            'proven': proven[:4],
        }
        cur_tmode = _norm_teach_mode(pub.get('teach_mode'))
        use_ped_sq = 'junior' if pub.get('source') == 'cowrite' else (pedagogy if pedagogy in ('elem', 'junior') else _norm_pedagogy(pub.get('pedagogy')))
        sp_meta_sq = copy.deepcopy(pub['sprint_meta']) if isinstance(pub.get('sprint_meta'), dict) else None
        raw_cur_sq = self.sessions.get(sid) or {}
        eff_sil_sq = bool(silent_tts) if isinstance(silent_tts, bool) else bool(raw_cur_sq.get('silent_start') or pub.get('silent_start'))
        new_sess = self.start_live_problem(
            question=meta.get('full_question') or pub.get('question') or '',
            mode=_norm_mode(mode or pub.get('mode')),
            title=f"[{nxt_part['label']}/{len(parts)}问] {meta.get('stem', '')[:12]}…",
            source=pub.get('source') or 'subq_relay',
            pedagogy=use_ped_sq,
            teach_mode=cur_tmode,
            mistake_id=pub.get('mistake_id') or '',
            subq_meta=new_meta,
            solo_challenge=bool(solo_challenge) and cur_tmode != 'diagnose_draft',
            sprint_meta=sp_meta_sq,
            silent_tts=eff_sil_sq,
            cowrite_item_id=str(pub.get('cowrite_item_id') or ''),
        )
        if new_sess and new_sess.get('id') in self.sessions:
            rec_sq = self.sessions[new_sess['id']]
            if pub.get('image_url') and not rec_sq.get('image_url'):
                rec_sq['image_url'] = pub['image_url']
                new_sess['image_url'] = pub['image_url']
            if pub.get('stage_page_id') and not rec_sq.get('stage_page_id'):
                rec_sq['stage_page_id'] = pub['stage_page_id']
                new_sess['stage_page_id'] = pub['stage_page_id']
            if sp_meta_sq and sp_meta_sq.get('sprint_id') in self.sprints:
                self.sprints[sp_meta_sq['sprint_id']]['current_sid'] = new_sess['id']
        return {'ok': True, 'subq_meta': new_meta, 'session': new_sess}

    async def prepare_print_sheet(self, ids: list = None, sheet_mode: str = 'original', pedagogy: str = 'auto', from_cowrite: bool = False) -> dict:
        self._ensure_mistakes_loaded()
        cw_board_mids = []
        if from_cowrite:
            cw_ps = getattr(self, 'cowrite', None)
            if isinstance(cw_ps, CoWriteMonitor):
                cw_ps._ensure_loaded()
                grad_ps = cw_ps._graduated_mistake_ids()
                if any(cw_ps._is_item_enrollable(x, grad_ps) for x in cw_ps.items):
                    cw_ps.enroll_mistake(enroll_all_warns=True)
                cw_board_mids = [str(x.get('mistake_id') or '').strip() for x in cw_ps.items if str(x.get('mistake_id') or '').strip()]
        id_list = [str(x).strip() for x in (ids or []) if str(x).strip()][:6]
        chosen = []
        if id_list:
            for mid in id_list:
                hit = next((m for m in self.mistakes if m.get('id') == mid), None)
                if hit:
                    chosen.append(hit)
        if not chosen and from_cowrite:
            for mid in cw_board_mids:
                if len(chosen) >= 6:
                    break
                hit = next((m for m in self.mistakes if m.get('id') == mid and not m.get('graduated') and m not in chosen), None)
                if hit:
                    chosen.append(hit)
            if not chosen:
                chosen = [m for m in self.mistakes if not m.get('graduated') and str(m.get('error_tag') or '').startswith('伴写·')][:6]
        if not chosen:
            active = [m for m in self.mistakes if not m.get('graduated')]
            chosen = (active if active else self.mistakes)[:4]
        if not chosen and self.active_session_id:
            act_p = self.public_session(self.active_session_id)
            if act_p and act_p.get('question') and not act_p.get('essay_critique') and not act_p.get('writing_task'):
                chosen = [{'id': 'act', 'title': act_p['title'], 'question': act_p['question'], 'subject': act_p.get('subject') or 'math', 'error_tag': '当前练习题', 'steps': act_p.get('steps') or []}]
        if not chosen:
            chosen = [
                {'id': sp['id'], 'title': sp['title'], 'question': sp['question'], 'subject': 'math', 'error_tag': sp['grade'], 'steps': sp['golden_steps']}
                for sp in SAMPLE_PROBLEMS[:3]
            ]

        def _extract_check_key(m_entry: dict) -> str:
            steps_m = m_entry.get('steps') or []
            q_m_txt = str(m_entry.get('question') or '')
            ask_clauses = [cl.strip() for cl in re.split(r'[，。；！？,;!\?\n]+', q_m_txt) if re.search(r'多少|几', cl)]
            ask_nouns = []
            if len(ask_clauses) >= 2:
                for acl in ask_clauses:
                    m_an = re.search(r'([\u4e00-\u9fa5]{1,6}?)(?:是|为|等于|有|共|一共|各是)?(?:多少|几)', acl)
                    if m_an:
                        n_clean = re.sub(r'^(?:求|算出|请问|这个|该|那么|则)?(?:[\u4e00-\u9fa5]{1,4}的)?', '', m_an.group(1)).strip()
                        if n_clean and n_clean not in ask_nouns:
                            ask_nouns.append(n_clean)
            for st in reversed(steps_m):
                if not isinstance(st, dict) or st.get('is_qa'):
                    continue
                qz = st.get('quiz')
                if isinstance(qz, dict):
                    ch_m = qz.get('chain')
                    if isinstance(ch_m, list) and ch_m and ch_m[-1].get('ans'):
                        lr = ch_m[-1]
                        last_str = f"{lr['ans']}{lr.get('unit') or qz.get('unit') or ''}"
                        if len(ask_nouns) >= 2 and len(ch_m) >= 2:
                            parts_ans = []
                            used_nouns = set()
                            for r_i, r_it in enumerate(ch_m):
                                if not isinstance(r_it, dict) or not r_it.get('ans'):
                                    continue
                                rq_txt = str(r_it.get('q') or '')
                                hit_n = next(
                                    (an for an in ask_nouns if an not in used_nouns and re.search(rf'{re.escape(an)}(?:是|为|等于|有|共|多少|几)', rq_txt)),
                                    '',
                                )
                                if hit_n:
                                    used_nouns.add(hit_n)
                                    parts_ans.append(f"{hit_n}{r_it['ans']}{r_it.get('unit') or qz.get('unit') or ''}")
                                elif r_i == len(ch_m) - 1 and parts_ans:
                                    parts_ans.append(last_str)
                            if len(parts_ans) >= 2:
                                return '，'.join(parts_ans[:3])
                        return last_str
                    if qz.get('num_ans'):
                        return f"{qz['num_ans']}{qz.get('unit') or ''}"
                    opts_m = qz.get('opts')
                    ans_m = qz.get('ans', 0)
                    if isinstance(opts_m, list) and 0 <= ans_m < len(opts_m) and int(st.get('step') or 0) >= 2:
                        return str(opts_m[ans_m]).strip()[:30]
            for st in reversed(steps_m):
                if not isinstance(st, dict) or st.get('is_qa'):
                    continue
                bd = str(st.get('board') or '')
                m_oks = [
                    x.strip() for x in re.findall(r'([^\n✓]{2,26}✓)', bd)
                    if not re.search(r'^(?:✅\s*)?(?:逆向)?(?:验算|检验\d*|复盘)|复盘(?:核验|检验)|核对无误|等量关系|自查一遍|换数复测|先独做|掌握度|请将草稿', x.strip())
                ]
                if m_oks:
                    return m_oks[-1]
            wf_m = str(m_entry.get('weak_focus') or '').strip()
            if wf_m and str(m_entry.get('error_tag') or '').startswith('伴写·'):
                ft_chk = ''
                for st_f in steps_m:
                    if isinstance(st_f, dict):
                        m_ft = re.search(r'【关联定理】([^\n]+)', str(st_f.get('board') or ''))
                        if m_ft:
                            ft_chk = m_ft.group(1).strip()
                            break
                if ft_chk:
                    return f"自查：{wf_m[:17]}（{ft_chk[:11]}）"[:34]
                return f"自查：{wf_m[:28]}"
            return '请结合原题独立验算核对'

        dirty_mistakes = False
        for m_it in chosen[:6]:
            if await self._ensure_cowrite_mistake_stem(m_it, save=False):
                dirty_mistakes = True
        if dirty_mistakes:
            self._save_mistakes()

        is_desens = str(sheet_mode or '').strip().lower() in ('desensitized', 'variant', 'retest')
        if not is_desens:
            items_out = [
                {
                    'idx': i + 1,
                    'title': _cowrite_latex_to_unicode(str(m.get('title') or f'第{i + 1}题'))[:24],
                    'subject': str(m.get('subject') or detect_subject(m.get('question') or '')),
                    'is_writing': bool(m.get('essay_critique') or detect_writing_task(m.get('question') or '')),
                    'tag': str(m.get('error_tag') or '重点巩固')[:16],
                    'question': _cowrite_latex_to_unicode(str(m.get('question') or '').strip()),
                    'ans_check': _cowrite_latex_to_unicode(_extract_check_key(m))[:34],
                }
                for i, m in enumerate(chosen[:6])
            ]
            return {
                'ok': True,
                'sheet_mode': 'original',
                'title': '初三伴写易错原题重练清卷' if from_cowrite else 'AI 错题原题重练清卷',
                'items': items_out,
            }

        chosen_slice = chosen[:6]
        batches = [chosen_slice[k:k + 2] for k in range(0, len(chosen_slice), 2)]

        async def _desens_batch(b_items: list) -> list:
            src_lines = '\n'.join(
                f"{idx_b + 1}. [{'初中九年级·' if (m_b.get('pedagogy') == 'junior' or str(m_b.get('error_tag') or '').startswith('伴写·')) else ''}{m_b.get('subject') or detect_subject(m_b.get('question') or '')}·{m_b.get('weak_focus') or m_b.get('error_tag', '易错题')}] {m_b.get('question', '')}"
                for idx_b, m_b in enumerate(b_items)
            )
            b_prompt = (
                f"请将以下 {len(b_items)} 道错题逐一进行【同考点脱敏改编】（数学/理化计算题请保持相同模型更换生活场景与数字并内部验算确保整除无误；语文/英语/史地政生题请保持完全相同的语法/阅读/概念考点与易错点并更换全新语境句子或材料，严禁保留原句答案），供家长打印 A4 纸质清卷给孩子重练：\n"
                f"{src_lines}\n\n"
                '只输出一行JSON（不要代码块）：{"items":[{"title":"简短题名","question":"脱敏改编后的新题完整题干(绝不含答案)","ans_check":"仅供页脚折叠核对的标准答案或采分点(22字内)"}]}'
            )
            c_txt = await asyncio.wait_for(
                self._post_chat_once('aux', [{'role': 'user', 'content': b_prompt}], temperature=0.35, timeout_s=22.0),
                timeout=24.0,
            )
            b_objs, _ = extract_json_objects_from_buffer(c_txt)
            b_gen = (b_objs[0].get('items') if (b_objs and isinstance(b_objs[0].get('items'), list)) else []) if b_objs else []
            return [b_gen[j] if j < len(b_gen) and isinstance(b_gen[j], dict) else None for j in range(len(b_items))]

        batch_results = await asyncio.gather(*[_desens_batch(b) for b in batches], return_exceptions=True)
        gen_list = []
        for b_idx, b_res in enumerate(batch_results):
            if isinstance(b_res, list):
                gen_list.extend(b_res)
            else:
                gen_list.extend([None] * len(batches[b_idx]))
        items_out = []
        for i, m in enumerate(chosen[:6]):
            g = gen_list[i] if i < len(gen_list) and isinstance(gen_list[i], dict) and gen_list[i].get('question') else None
            m_subj_ps = str(m.get('subject') or detect_subject(m.get('question') or ''))
            tag_pre_ps = '同考新编·' if m_subj_ps in ('chinese', 'english', 'history', 'civics', 'biology', 'geography') else '换数新编·'
            orig_q_ps = str(m.get('question') or '').strip()
            gen_q_ps = sanitize_ocr_question(str((g or {}).get('question') or '')) if g else ''
            used_new_q = bool(gen_q_ps and gen_q_ps != orig_q_ps)
            ans_chk_ps = (
                str((g or {}).get('ans_check') or (g or {}).get('answer') or (g or {}).get('ans') or '请独立验算核对')
                if used_new_q else
                str((g or {}).get('ans_check') or _extract_check_key(m))
            )
            items_out.append({
                'idx': i + 1,
                'title': _cowrite_latex_to_unicode(str((g or {}).get('title') or m.get('title') or f'变式第{i + 1}题'))[:24],
                'subject': m_subj_ps,
                'is_writing': bool(m.get('essay_critique') or detect_writing_task(m.get('question') or '')),
                'tag': (tag_pre_ps + str(m.get('error_tag') or '巩固'))[:16],
                'question': _cowrite_latex_to_unicode(gen_q_ps or sanitize_ocr_question(orig_q_ps)),
                'ans_check': _cowrite_latex_to_unicode(ans_chk_ps.strip())[:34],
            })
        return {
            'ok': True,
            'sheet_mode': 'desensitized',
            'title': '初三伴写同考点脱敏重练清卷' if from_cowrite else 'AI 脱敏换数提分清卷',
            'items': items_out,
        }

    @staticmethod
    def _read_open_ui_seq_floor() -> int:
        sig_raw = str(os.environ.get('TUTOR_OPEN_SIGNAL', '/run/user/1000/thinksmart-tutor-open.json') or '').strip()
        if not sig_raw:
            return 0
        with contextlib.suppress(OSError, ValueError, TypeError):
            sig_p = Path(sig_raw)
            if sig_p.is_file() and not sig_p.is_symlink():
                obj = json.loads(sig_p.read_text(encoding='utf-8'))
                if isinstance(obj, dict):
                    return max(0, int(obj.get('open_ui_seq') or 0))
        return 0

    def trigger_open_ui(self, target_view: str = '', pencil_boost=None):
        self.open_ui_seq = max(self.open_ui_seq, getattr(self, '_open_ui_seq_floor', 0)) + 1
        self._open_ui_seq_floor = self.open_ui_seq
        self.open_ui_target = target_view if target_view in ('board', 'phone', 'camera', 'cowrite', 'samples', 'dictation') else ''
        self.open_ui_pencil = bool(pencil_boost) if isinstance(pencil_boost, bool) else None
        self._open_ui_mono = time.monotonic()
        with contextlib.suppress(OSError):
            sig_raw = str(os.environ.get('TUTOR_OPEN_SIGNAL', '/run/user/1000/thinksmart-tutor-open.json') or '').strip()
            if not sig_raw:
                return
            sig_p = Path(sig_raw)
            if sig_p.parent.is_dir() and not sig_p.parent.is_symlink() and not sig_p.is_symlink():
                tmp_p = sig_p.with_name(f'.{sig_p.name}.{os.getpid()}.tmp')
                payload = json.dumps({'ts': round(time.time(), 3), 'open_ui_seq': self.open_ui_seq, 'target': self.open_ui_target}, separators=(',', ':')).encode('utf-8')
                fd = os.open(str(tmp_p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                try:
                    if os.getuid() == 0 and str(sig_p).startswith('/run/user/1000/'):
                        with contextlib.suppress(OSError):
                            os.fchown(fd, 1000, 1000)
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                os.replace(tmp_p, sig_p)

    def poll_camera_hotplug(self) -> dict:
        cam = self.camera.discover()
        is_conn = bool(cam.get('connected'))
        if is_conn and not self._cam_was_connected:
            self._cam_was_connected = True
            if not self.is_lesson_in_progress():
                cw_hp = getattr(self, 'cowrite', None)
                self.trigger_open_ui('cowrite' if (isinstance(cw_hp, CoWriteMonitor) and cw_hp.enabled) else 'camera')
        elif not is_conn and self._cam_was_connected:
            self._cam_was_connected = False
            self.camera._frozen_raw_jpg = None
            self.camera.clear_frame_cache()
            self._spawn(self.camera.stop_stream())
        return cam

    def is_lesson_in_progress(self) -> bool:
        if not self.active_session_id:
            return False
        act = self.sessions.get(self.active_session_id)
        if not act:
            return False
        if act.get('status') == 'error':
            return False
        if act.get('status') in ('thinking', 'streaming'):
            return (time.monotonic() - act.get('t0_mono', 0)) < 180.0
        prog = act.get('progress') or {}
        if prog.get('solved'):
            return False
        return (time.monotonic() - act.get('t0_mono', 0)) < 600.0

    def is_open_ui_fresh(self) -> bool:
        if CALL_FLAG.exists() or MIRROR_FLAG.exists():
            return False
        return self.open_ui_seq > 0 and (time.monotonic() - self._open_ui_mono) < 15.0

    def _spawn(self, coro, session_work: bool = False):
        t = asyncio.create_task(coro)
        self.bg_tasks.add(t)
        t.add_done_callback(self.bg_tasks.discard)
        if session_work:
            self._session_work_tasks.add(t)
            t.add_done_callback(self._session_work_tasks.discard)
        return t

    def _cancel_previous_session_work(self):
        cur_t = None
        with contextlib.suppress(RuntimeError):
            cur_t = asyncio.current_task()
        for t in list(self._session_work_tasks):
            if not t.done() and t is not cur_t:
                t.cancel()
        self._session_work_tasks = {t for t in self._session_work_tasks if t is cur_t and not t.done()}

    def _load_gateway_cfg(self) -> dict:
        try:
            st = os.lstat(str(HUB_CFG_PATH))
            if not stat.S_ISREG(st.st_mode):
                raise OSError('Refusing non-regular gateway config')
            sig = (st.st_ino, st.st_mtime_ns, st.st_size)
            cached_cfg = getattr(self, '_cfg_cache', None)
            if cached_cfg is not None and sig == getattr(self, '_cfg_sig', None):
                return cached_cfg
            sig, raw_bytes = _read_regular_bytes(HUB_CFG_PATH, 512 * 1024)
            txt = raw_bytes.decode('utf-8')
            if txt.lstrip().startswith('{'):
                raw = json.loads(txt)
            elif _yaml is not None:
                raw = _yaml.load(txt, Loader=getattr(_yaml, 'CSafeLoader', _yaml.SafeLoader)) or {}
            else:
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            self._cfg_cache = raw
            self._cfg_sig = sig
            return raw
        except Exception as e:
            log.warning('Failed to load gateway config: %s', _redact_secrets(e))
            return getattr(self, '_cfg_cache', None) or {}

    def _load_tutor_models(self) -> dict:
        try:
            st = os.lstat(str(PREFS_PATH))
            if not stat.S_ISREG(st.st_mode):
                return {}
            sig = (st.st_ino, st.st_mtime_ns, st.st_size)
            if getattr(self, '_tm_sig', None) == sig and isinstance(getattr(self, '_tm_cache', None), dict):
                return self._tm_cache
            prefs = _read_safe_json(PREFS_PATH, {})
            tm = prefs.get('tutor_models') if isinstance(prefs, dict) and isinstance(prefs.get('tutor_models'), dict) else {}
            self._tm_sig = sig
            self._tm_cache = tm
            return tm
        except Exception:
            return getattr(self, '_tm_cache', None) or {}

    @staticmethod
    def _normalize_llm_v1_url(raw_u) -> str:
        b = str(raw_u or '').strip().rstrip('/')
        if not b:
            return ''
        return b if b.endswith(('/v1', '/v3', '/v4', '/openai')) else b + '/v1'

    @staticmethod
    def _usable_llm_key(raw_k) -> str:
        k = str(raw_k or '').strip()
        if not k or k.lower().startswith(('sk-replace', 'your-', 'replace-')) or '你的' in k or '填写' in k or 'placeholder' in k.lower():
            return ''
        return k

    def _resolve_llm_chain(self, kind: str) -> tuple:
        """Resolve (v1_url, api_key, [(model_name, extra_params), ...]).

        Production routing locked per user decision (GPT + DeepSeek defaults, configurable via prefs.json):
        - Primary ('fast' & 'aux'): gpt-6.1-terra -> gpt-6.1-sol -> deepseek-v4.1-flash-sg
        - Deep ('deep'): gpt-6.1-sol -> gpt-6.1-terra -> deepseek-v4.1-flash-sg
        - Backup ('backup'): deepseek-v4.1-flash-sg -> gpt-6.1-terra -> gpt-6.1-sol
        """
        cfg = self._load_gateway_cfg()
        llms = cfg.get('LLM') or {}
        sel_name = str((cfg.get('selected_module') or {}).get('LLM') or '').strip()
        sel_d = llms.get(sel_name) if (sel_name and sel_name != 'ClaudeCLI' and isinstance(llms.get(sel_name), dict)) else {}
        sel_v1 = self._normalize_llm_v1_url(sel_d.get('url') or sel_d.get('base_url'))
        sel_key = self._usable_llm_key(sel_d.get('api_key'))
        sel_mod = str(sel_d.get('model_name') or '').strip()

        oa_d = llms.get('OpenAILLM') if isinstance(llms.get('OpenAILLM'), dict) else {}
        ds_d = llms.get('DeepSeekLLM') if isinstance(llms.get('DeepSeekLLM'), dict) else {}
        oa_v1 = self._normalize_llm_v1_url(oa_d.get('url') or oa_d.get('base_url'))
        oa_key = self._usable_llm_key(oa_d.get('api_key'))
        ds_v1 = self._normalize_llm_v1_url(ds_d.get('url') or ds_d.get('base_url'))
        ds_key = self._usable_llm_key(ds_d.get('api_key'))

        tm = self._load_tutor_models()
        has_custom_tm = any(str(tm.get(k) or '').strip() for k in ('fast', 'deep', 'backup'))
        cands = (
            [(oa_d, oa_v1, oa_key), (sel_d, sel_v1, sel_key), (ds_d, ds_v1, ds_key)]
            if has_custom_tm
            else [(sel_d, sel_v1, sel_key), (oa_d, oa_v1, oa_key), (ds_d, ds_v1, ds_key)]
        )
        d, v1, api_key = {}, '', ''
        for cd, cv1, ckey in cands:
            if cv1 and ckey:
                d, v1, api_key = cd, cv1, ckey
                break
        if not v1:
            for k_p, v in llms.items():
                if k_p == 'ClaudeCLI' or not isinstance(v, dict):
                    continue
                tv1 = self._normalize_llm_v1_url(v.get('url') or v.get('base_url'))
                tkey = self._usable_llm_key(v.get('api_key'))
                if tv1 and tkey:
                    d, v1, api_key = v, tv1, tkey
                    break
        if not v1:
            d = sel_d or oa_d or ds_d or {}
            v1 = self._normalize_llm_v1_url(d.get('url') or d.get('base_url'))
            api_key = self._usable_llm_key(d.get('api_key'))
        cfg_model = str(d.get('model_name') or '')

        non_gem_models = [
            str((v or {}).get('model_name') or '')
            for k_p, v in llms.items()
            if isinstance(v, dict) and 'gemini' not in str(k_p).lower()
        ]
        all_models_str = ' '.join(non_gem_models)
        is_mock = ('mock-' in all_models_str) or not all_models_str.strip()
        if is_mock:
            return v1, (api_key or str(d.get('api_key') or '')), [(cfg_model, {})]

        ds_cfg = llms.get('DeepSeekLLM') if isinstance(llms.get('DeepSeekLLM'), dict) else {}
        ds_raw = str((ds_cfg or {}).get('model_name') or '').strip()
        ds_model = ds_raw if (ds_raw and 'gemini' not in ds_raw.lower()) else 'deepseek-v4.1-flash-sg'

        xz_mod = sel_mod if (sel_mod and 'gemini' not in sel_mod.lower()) else ''
        if not has_custom_tm and xz_mod and not (sel_name in ('', 'OpenAILLM') and oa_v1 and oa_key and str(oa_d.get('model_name') or '').strip() == 'gpt-6.1-terra'):
            m_fast = xz_mod
            m_deep = xz_mod
            m_bak = ds_raw if (ds_raw and ds_v1 and ds_key and 'gemini' not in ds_raw.lower()) else xz_mod
        else:
            fb_def = ds_raw if (ds_v1 and ds_key and not (oa_v1 and oa_key)) else ''
            m_fast = str(tm.get('fast') or '').strip() or xz_mod or fb_def or 'gpt-6.1-terra'
            m_deep = str(tm.get('deep') or '').strip() or xz_mod or fb_def or 'gpt-6.1-sol'
            m_bak = str(tm.get('backup') or '').strip() or (ds_raw or ds_model)

        def _ex(m: str) -> dict:
            return {'reasoning_effort': 'low'} if m.startswith(('gpt-', 'o1', 'o3', 'o4')) else {}

        if kind == 'deep':
            chain = [(m_deep, _ex(m_deep)), (m_fast, _ex(m_fast)), (m_bak, _ex(m_bak))]
        elif kind in ('backup', 'geo'):
            chain = [(m_bak, _ex(m_bak)), (m_fast, _ex(m_fast)), (m_deep, _ex(m_deep))]
        else:
            chain = [(m_fast, _ex(m_fast)), (m_deep, _ex(m_deep)), (m_bak, _ex(m_bak))]
        return v1, api_key, chain

    def _resolve_llm_target(self, kind: str) -> tuple:
        v1, api_key, chain = self._resolve_llm_chain(kind)
        pref_model, extra = chain[0]
        fb_model = chain[1][0] if len(chain) > 1 else pref_model
        return v1, api_key, pref_model, fb_model, extra

    def _llm_endpoint(self, kind: str) -> tuple:
        v1, api_key, chain = self._resolve_llm_chain(kind)
        return v1, api_key, chain[0][0]

    def _iter_llm_targets(self, kind: str) -> list:
        v1, api_key, chain = self._resolve_llm_chain(kind)
        targets = []
        seen = set()
        cfg = self._load_gateway_cfg()
        llms = cfg.get('LLM') or {}
        sel_name = str((cfg.get('selected_module') or {}).get('LLM') or '').strip()
        sel_cfg = llms.get(sel_name) if (sel_name and sel_name != 'ClaudeCLI' and isinstance(llms.get(sel_name), dict)) else {}
        sel_v1 = self._normalize_llm_v1_url((sel_cfg or {}).get('url') or (sel_cfg or {}).get('base_url'))
        sel_key = self._usable_llm_key((sel_cfg or {}).get('api_key'))
        sel_mod = str((sel_cfg or {}).get('model_name') or '').strip()

        ds_cfg = llms.get('DeepSeekLLM') if isinstance(llms.get('DeepSeekLLM'), dict) else {}
        ds_v1 = self._normalize_llm_v1_url((ds_cfg or {}).get('url') or (ds_cfg or {}).get('base_url'))
        ds_key = self._usable_llm_key((ds_cfg or {}).get('api_key'))
        ds_mod = str((ds_cfg or {}).get('model_name') or '').strip()

        for m_name, ex in chain:
            is_ds = bool(m_name == ds_mod or m_name.lower().startswith('deepseek'))
            if m_name and m_name == sel_mod and sel_v1 and sel_key:
                use_v1, use_key = sel_v1, sel_key
            elif is_ds and ds_v1 and ds_key:
                use_v1, use_key = ds_v1, ds_key
            else:
                use_v1, use_key = v1, api_key
            if use_v1 and use_key and m_name and (use_v1, m_name) not in seen:
                seen.add((use_v1, m_name))
                targets.append((use_v1, use_key, m_name, ex))
        non_gem_m = ' '.join(
            str((v or {}).get('model_name') or '')
            for k_p, v in llms.items()
            if isinstance(v, dict) and 'gemini' not in str(k_p).lower()
        )
        is_mock = ('mock-' in non_gem_m) or not non_gem_m.strip()
        if not is_mock:
            prov_order = []
            for p_k in (sel_name, 'DeepSeekLLM', 'OpenAILLM', *llms.keys()):
                if p_k and p_k != 'ClaudeCLI' and p_k not in prov_order:
                    prov_order.append(p_k)
            for prov_key in prov_order:
                d_sec = llms.get(prov_key)
                if not isinstance(d_sec, dict):
                    continue
                v1_sec = self._normalize_llm_v1_url(d_sec.get('url') or d_sec.get('base_url'))
                key_sec = self._usable_llm_key(d_sec.get('api_key'))
                mod_sec = str(d_sec.get('model_name') or '').strip()
                if not mod_sec or 'gemini' in mod_sec.lower():
                    continue
                ex_sec = {'reasoning_effort': 'low'} if mod_sec.startswith(('gpt-', 'o1', 'o3', 'o4')) else {}
                if v1_sec and key_sec and (v1_sec, mod_sec) not in seen:
                    seen.add((v1_sec, mod_sec))
                    targets.append((v1_sec, key_sec, mod_sec, ex_sec))
        cd_map = getattr(self, '_llm_cooldown_until', None)
        if cd_map is None:
            cd_map = {}
            self._llm_cooldown_until = cd_map
        now_m = time.monotonic()
        active = [t for t in targets if cd_map.get((t[0], t[2]), 0.0) <= now_m]
        if active:
            return active
        return sorted(targets, key=lambda t: 1 if (cd_map.get((t[0], t[2]), 0.0) - now_m) > 40.0 else 0)

    def _mark_llm_target_cooldown(self, tv1: str, model_name: str, status_code: int = 0, transient: bool = False) -> None:
        cd_map = getattr(self, '_llm_cooldown_until', None)
        if cd_map is None:
            cd_map = {}
            self._llm_cooldown_until = cd_map
        if status_code in (400, 401, 403, 404, 429):
            cd_map[(tv1, model_name)] = time.monotonic() + 90.0
        elif transient or status_code >= 500:
            cd_map[(tv1, model_name)] = max(cd_map.get((tv1, model_name), 0.0), time.monotonic() + 35.0)

    @staticmethod
    def _humanize_llm_error(last_err: str) -> str:
        if '429' in last_err or 'usage_limit' in last_err or 'rate' in last_err.lower():
            return 'AI 老师当前通道请求繁忙，请稍等片刻再点重试'
        if 'Timeout' in last_err or 'timeout' in last_err:
            return 'AI 老师思考响应超时，请点击重试一次'
        return 'AI 老师暂时开小差了，请稍后重试一次'

    async def _post_chat_once(self, kind: str, messages: list, temperature: float = 0.25, timeout_s: float = 26.0) -> str:
        targets = self._iter_llm_targets(kind)
        if not targets:
            raise RuntimeError('未配置讲题大模型')
        last_err = ''
        tkey = targets[0][1]
        deadline = time.monotonic() + max(timeout_s * 1.35, timeout_s + 8.0)
        for idx, (tv1, tkey, m_try, ex_try) in enumerate(targets):
            rem = deadline - time.monotonic()
            if rem <= 1.0:
                break
            per_try = min(rem, max(8.0, min(timeout_s * 0.68, timeout_s - 6.0))) if (idx == 0 and len(targets) > 1) else min(rem, timeout_s)
            eff_ex = {'reasoning_effort': 'low'} if (kind != 'deep' and not ex_try and m_try.lower().startswith('deepseek')) else ex_try
            body = {'model': m_try, 'temperature': temperature, **eff_ex, 'messages': messages}
            try:
                async with self.http.post(
                    tv1 + '/chat/completions',
                    json=body,
                    headers={'Authorization': f'Bearer {tkey}', 'User-Agent': 'ThinkSmartTutor/1.0'},
                    timeout=aiohttp.ClientTimeout(total=per_try, sock_connect=6),
                ) as resp:
                    raw_txt = (await _read_resp_bounded(resp, 1024 * 1024)).decode('utf-8', errors='replace')
                    if resp.status == 200:
                        data = json.loads(raw_txt)
                        content = ((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
                        if content.strip():
                            return content
                        last_err = f'{m_try}: empty_content'
                    else:
                        self._mark_llm_target_cooldown(tv1, m_try, resp.status)
                        last_err = f'{m_try} HTTP {resp.status}: {_redact_secrets(raw_txt[:100], tkey)}'
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, (asyncio.TimeoutError, aiohttp.ClientError)):
                    self._mark_llm_target_cooldown(tv1, m_try, transient=True)
                last_err = f'{m_try} {type(exc).__name__}: {_redact_secrets(exc, tkey)}'
            if idx + 1 < len(targets):
                log.warning('Model %s failed (%s), failing over to %s', m_try, _redact_secrets(last_err, tkey), targets[idx + 1][2])
        log.warning('All LLM targets failed for kind=%s: %s', kind, _redact_secrets(last_err, tkey))
        raise RuntimeError(self._humanize_llm_error(last_err))

    def _tts_endpoint(self) -> tuple:
        cfg = self._load_gateway_cfg()
        t = (cfg.get('TTS') or {}).get('IndexStreamTTS') or {}
        return str(t.get('api_url') or '').strip(), str(t.get('voice') or 'xiaohe').strip()

    def _asr_endpoint(self) -> tuple:
        cfg = self._load_gateway_cfg()
        a = (cfg.get('ASR') or {}).get('MacASR') or {}
        base = str(a.get('base_url') or '').rstrip('/')
        if base.lower().endswith('/audio/transcriptions'):
            base = base[:-len('/audio/transcriptions')].rstrip('/')
        v1 = base if base.endswith('/v1') else (base + '/v1' if base else '')
        return v1, str(a.get('api_key') or 'none'), str(a.get('model_name') or 'Qwen3-ASR-1.7B')

    async def pause_music_if_playing(self):
        if not self.http:
            return
        with contextlib.suppress(Exception):
            async with self.http.post(
                MUSIC_CTRL_URL,
                json={'action': 'pause'},
                headers={'Host': '127.0.0.1:8091'},
                timeout=aiohttp.ClientTimeout(total=1.5),
            ) as _:
                pass

    def _wake_next_tts_waiter(self):
        self._tts_woken_ev = None
        for q in (self._tts_hi_q, self._tts_lo_q):
            while q:
                ev = q.popleft()
                if not ev.is_set():
                    self._tts_woken_ev = ev
                    ev.set()
                    return

    @contextlib.asynccontextmanager
    async def _acquire_tts_slot(self, priority: bool = False, key: str = ''):
        if priority:
            self._tts_priority_waiters += 1
        ev_slot = None
        try:
            while self._tts_busy or getattr(self, '_tts_woken_ev', None) is not None or (not priority and (self._tts_priority_waiters > 0 or bool(self._tts_hi_q))):
                ev = asyncio.Event()
                ev_slot = ev
                if key:
                    self._tts_wait_evs = getattr(self, '_tts_wait_evs', {})
                    self._tts_wait_evs[key] = ev
                q_ref = self._tts_hi_q if priority else self._tts_lo_q
                q_ref.append(ev)
                try:
                    await ev.wait()
                except BaseException:
                    for qr in (self._tts_hi_q, self._tts_lo_q):
                        with contextlib.suppress(ValueError):
                            qr.remove(ev)
                    if key and hasattr(self, '_tts_wait_evs'):
                        self._tts_wait_evs.pop(key, None)
                    if ev is getattr(self, '_tts_woken_ev', None):
                        self._tts_woken_ev = None
                        if not self._tts_busy:
                            self._wake_next_tts_waiter()
                    elif ev.is_set() and not self._tts_busy and getattr(self, '_tts_woken_ev', None) is None:
                        self._wake_next_tts_waiter()
                    raise
                if key and hasattr(self, '_tts_wait_evs'):
                    self._tts_wait_evs.pop(key, None)
                if ev is getattr(self, '_tts_woken_ev', None):
                    self._tts_woken_ev = None
                    break
            while self._tts_busy or (getattr(self, '_tts_woken_ev', None) is not None):
                ev2 = asyncio.Event()
                self._tts_hi_q.append(ev2)
                try:
                    await ev2.wait()
                except BaseException:
                    with contextlib.suppress(ValueError):
                        self._tts_hi_q.remove(ev2)
                    if ev2 is getattr(self, '_tts_woken_ev', None):
                        self._tts_woken_ev = None
                        if not self._tts_busy:
                            self._wake_next_tts_waiter()
                    elif ev2.is_set() and not self._tts_busy and getattr(self, '_tts_woken_ev', None) is None:
                        self._wake_next_tts_waiter()
                    raise
                if ev2 is getattr(self, '_tts_woken_ev', None):
                    self._tts_woken_ev = None
                    break
            self._tts_busy = True
        finally:
            if key and hasattr(self, '_tts_wait_evs') and self._tts_wait_evs.get(key) is ev_slot:
                self._tts_wait_evs.pop(key, None)
            if priority:
                self._tts_priority_waiters = max(0, self._tts_priority_waiters - 1)
        try:
            yield
        finally:
            self._tts_busy = False
            self._wake_next_tts_waiter()

    async def synthesize_wav(self, text: str, priority: bool = False) -> bytes:
        spoken = clean_math_for_tts(text)
        if not spoken:
            return _pcm_to_wav(b'\x00' * 4800)
        key = hashlib.sha256(spoken.encode('utf-8')).hexdigest()[:20]
        if key in self.tts_cache:
            self.tts_cache.move_to_end(key)
            return self.tts_cache[key]
        inflight = self._tts_inflight.get(key)
        if inflight is not None:
            if priority and hasattr(self, '_tts_wait_evs'):
                w_ev = self._tts_wait_evs.get(key)
                if w_ev is not None and w_ev in self._tts_lo_q:
                    with contextlib.suppress(ValueError):
                        self._tts_lo_q.remove(w_ev)
                    self._tts_hi_q.append(w_ev)
            try:
                return await asyncio.shield(inflight)
            except asyncio.CancelledError:
                if not inflight.cancelled():
                    raise
                if key in self.tts_cache:
                    self.tts_cache.move_to_end(key)
                    return self.tts_cache[key]

        fut = asyncio.get_running_loop().create_future()
        self._tts_inflight[key] = fut
        try:
            api_url, voice = self._tts_endpoint()
            if '_tts_endpoint' in getattr(self, '__dict__', {}) and api_url:
                use_hub_tts = False
            else:
                cfg_tts = self._load_gateway_cfg()
                sel_tts = str(((cfg_tts.get('selected_module') or {}).get('TTS')) or '').strip() if isinstance(cfg_tts, dict) else ''
                use_hub_tts = (not api_url) or (sel_tts not in ('', 'IndexStreamTTS'))
            hub_client = self.http or self.tts_http
            if (use_hub_tts and not hub_client) or (not use_hub_tts and not self.tts_http):
                wav = _FALLBACK_SILENT_WAV
                if not fut.done():
                    fut.set_result(wav)
                return wav
            if not priority and getattr(self, '_tts_down_until', 0.0) > time.monotonic():
                wav = _FALLBACK_SILENT_WAV
                if not fut.done():
                    fut.set_result(wav)
                return wav

            # Priority-aware sequential slot: on-demand step/hint/QA voice cuts ahead of background prefetch!
            async with self._acquire_tts_slot(priority=priority, key=key):
                if key in self.tts_cache:
                    wav = self.tts_cache[key]
                    if not fut.done():
                        fut.set_result(wav)
                    return wav
                if not priority and getattr(self, '_tts_down_until', 0.0) > time.monotonic():
                    wav = _FALLBACK_SILENT_WAV
                    if not fut.done():
                        fut.set_result(wav)
                    return wav
                last_err = None
                pcm = b''
                wav_direct = b''
                if use_hub_tts:
                    try:
                        async with hub_client.post(
                            'http://127.0.0.1:16688/kiosk/tts',
                            json={'text': spoken},
                            headers={'Host': '127.0.0.1:16688'},
                            timeout=aiohttp.ClientTimeout(total=20, connect=2.5, sock_connect=2.0, sock_read=16),
                        ) as resp:
                            if resp.status != 200:
                                raise RuntimeError(f'Hub TTS HTTP {resp.status}')
                            raw_hub = await _read_resp_bounded(resp, 4 * 1024 * 1024)
                        if raw_hub[:4] == b'RIFF' and len(raw_hub) >= 400:
                            wav_direct = raw_hub
                            self._tts_down_until = 0.0
                        elif len(raw_hub) >= 400:
                            pcm = raw_hub[:-1] if (len(raw_hub) & 1) else raw_hub
                            self._tts_down_until = 0.0
                        else:
                            raise RuntimeError('empty_hub_tts')
                    except Exception as exc:
                        last_err = exc
                        if isinstance(exc, (aiohttp.ClientConnectorError, asyncio.TimeoutError)):
                            self._tts_down_until = time.monotonic() + 4.0
                else:
                    for attempt in range(2):
                        try:
                            async with self.tts_http.post(
                                api_url,
                                json={'text': spoken, 'character': voice},
                                timeout=aiohttp.ClientTimeout(total=28, connect=2.5, sock_connect=2.0, sock_read=18),
                            ) as resp:
                                if resp.status != 200:
                                    raise RuntimeError(f'HTTP {resp.status}')
                                pcm = await _read_resp_bounded(resp, 4 * 1024 * 1024)
                            if len(pcm) & 1:
                                pcm = pcm[:-1]
                            if len(pcm) >= 400:
                                self._tts_down_until = 0.0
                                break
                            raise RuntimeError('empty_pcm')
                        except Exception as exc:
                            last_err = exc
                            if isinstance(exc, (aiohttp.ClientConnectorError, asyncio.TimeoutError)):
                                self._tts_down_until = time.monotonic() + 4.0
                                if isinstance(exc, aiohttp.ClientConnectorError):
                                    break
                            if attempt == 0:
                                await asyncio.sleep(0.15)
                if not wav_direct and len(pcm) < 400:
                    raise RuntimeError(_redact_secrets(f'{type(last_err).__name__}: {last_err}'))

            wav = wav_direct if wav_direct else _pcm_to_wav(pcm, sample_rate=24000, channels=1, bits=16)
            prev_wav = self.tts_cache.pop(key, None)
            if prev_wav is not None:
                self._tts_cache_bytes = max(0, getattr(self, '_tts_cache_bytes', 0) - len(prev_wav))
            self.tts_cache[key] = wav
            self._tts_cache_bytes = getattr(self, '_tts_cache_bytes', 0) + len(wav)
            while len(self.tts_cache) > 20 or (len(self.tts_cache) > 1 and self._tts_cache_bytes > 6 * 1024 * 1024):
                _, ev_wav = self.tts_cache.popitem(last=False)
                self._tts_cache_bytes = max(0, self._tts_cache_bytes - len(ev_wav))
            if not fut.done():
                fut.set_result(wav)
            return wav
        except Exception as e:
            log.warning('TTS synthesis transient fallback (%s: %s) — not caching silence', type(e).__name__, _redact_secrets(e))
            wav = _FALLBACK_SILENT_WAV
            if not fut.done():
                fut.set_result(wav)
            return wav
        except BaseException:
            if not fut.done():
                fut.cancel()
                fut._log_traceback = False
            raise
        finally:
            if self._tts_inflight.get(key) is fut:
                self._tts_inflight.pop(key, None)

    async def _prefetch_steps_tts_sequentially(self, steps: list):
        for st in steps:
            say = st.get('say') if isinstance(st, dict) else ''
            if say:
                await self.synthesize_wav(say, priority=False)

    async def _warm_golden_step1_cache(self):
        await asyncio.sleep(0.4)
        for sp in SAMPLE_PROBLEMS:
            g_steps = sp.get('golden_steps') or []
            if g_steps and g_steps[0].get('say'):
                with contextlib.suppress(Exception):
                    await self.synthesize_wav(g_steps[0]['say'], priority=False)

    def _new_session_record(self, title: str, question: str, mode: str, source: str, image_id: str = '') -> dict:
        sid = uuid.uuid4().hex[:12]
        rec = {
            'id': sid,
            'title': title[:42] or '数学微课讲题',
            'question': question[:600],
            'mode': mode,
            'source': source,
            'image_url': f'/api/tutor/snap/{image_id}.jpg' if image_id else '',
            'status': 'thinking',
            'error': '',
            'created_at': int(time.time()),
            't0_mono': time.monotonic(),
            'metrics': {'first_step_s': None, 'total_s': None, 'model': ''},
            'steps': [],
            'seq': 1,
        }
        self.sessions[sid] = rec
        self.active_session_id = sid
        self._trim_sessions()
        return rec

    def _save_to_history(self, sess: dict):
        self._ensure_history_loaded()
        real_steps = [s for s in (sess.get('steps') or []) if s.get('step', 0) > 0]
        prev_h = next((h for h in self.history if h.get('id') == sess['id']), None)
        prog = sess.get('progress') or (prev_h.get('progress') if prev_h else None) or {}
        if not real_steps and not (prog.get('solved') and sess.get('steps')):
            return
        entry = {
            'id': sess['id'],
            'title': sess['title'],
            'question': sess['question'],
            'subject': str(sess.get('subject') or detect_subject(sess.get('question') or '', teach_mode=_norm_teach_mode(sess.get('teach_mode')), essay_critique=bool(sess.get('essay_critique'))))[:16],
            'mode': sess['mode'],
            'source': sess['source'],
            'teach_mode': _norm_teach_mode(sess.get('teach_mode')),
            'essay_critique': bool(sess.get('essay_critique')),
            'sample_id': sess.get('sample_id') or '',
            'mistake_id': sess.get('mistake_id') or '',
            'pedagogy': _norm_pedagogy(sess.get('pedagogy')),
            'sprint_meta': sess.get('sprint_meta'),
            'subq_meta': sess.get('subq_meta'),
            'solo_challenge': bool(sess.get('solo_challenge')),
            'alt_method': sess.get('alt_method'),
            'exam_rubric': sess.get('exam_rubric'),
            'archetype_map': sess.get('archetype_map'),
            'foundation_capsule': sess.get('foundation_capsule'),
            'polya_sandbox': sess.get('polya_sandbox'),
            'image_url': sess.get('image_url') or '',
            'stage_page_id': sess.get('stage_page_id') or '',
            'silent_start': bool(sess.get('silent_start')),
            'cowrite_item_id': str(sess.get('cowrite_item_id') or '')[:24],
            'created_at': sess['created_at'],
            'metrics': sess.get('metrics') or {},
            'progress': prog,
            'steps': sess['steps'][:10],
        }
        self.history = [h for h in self.history if h.get('id') != sess['id']]
        self.history.insert(0, entry)
        self.history = _dedupe_history_entries(self.history)
        with contextlib.suppress(Exception):
            _atomic_write_json(HIST_PATH, self.history)
        self._hist_loaded_path = HIST_PATH
        self._hist_sig = self._file_stat_sig(HIST_PATH)
        if _norm_teach_mode(sess.get('teach_mode')) == 'diagnose_draft':
            self._enroll_or_refresh_mistake(sess, prog)

    def update_progress(self, sid: str, patch: dict) -> dict:
        if not sid or not isinstance(patch, dict):
            return {'ok': False}
        prog_clean = {}
        if 'solved' in patch:
            prog_clean['solved'] = bool(patch['solved'])
        if 'peeked' in patch:
            prog_clean['peeked'] = bool(patch['peeked'])
        if 'feynman_ok' in patch:
            prog_clean['feynman_ok'] = bool(patch['feynman_ok'])
        if 'solo_ace' in patch:
            prog_clean['solo_ace'] = bool(patch['solo_ace'])
        def _p_int(v):
            if v is None or isinstance(v, bool):
                return None
            try:
                return int(v)
            except (TypeError, ValueError, OverflowError):
                return None

        if 'solo_sec' in patch:
            iv = _p_int(patch['solo_sec'])
            if iv is not None:
                prog_clean['solo_sec'] = max(1, min(3600, iv))
        if 'read_aloud_ok' in patch:
            prog_clean['read_aloud_ok'] = bool(patch['read_aloud_ok'])
        if 'weak_step' in patch:
            iv = _p_int(patch['weak_step'])
            if iv is not None:
                prog_clean['weak_step'] = max(0, min(6, iv))
        if 'first_try_count' in patch:
            iv = _p_int(patch['first_try_count'])
            if iv is not None:
                prog_clean['first_try_count'] = max(0, min(10, iv))
        if 'total_gates' in patch:
            iv = _p_int(patch['total_gates'])
            if iv is not None:
                prog_clean['total_gates'] = max(0, min(10, iv))
        if 'current_step' in patch:
            iv = _p_int(patch['current_step'])
            if iv is not None:
                prog_clean['current_step'] = max(1, min(6, iv))
        if 'step_attempts' in patch:
            iv = _p_int(patch['step_attempts'])
            if iv is not None:
                prog_clean['step_attempts'] = max(0, min(20, iv))
        if 'step_title' in patch:
            prog_clean['step_title'] = str(patch.get('step_title') or '').strip()[:28]
        if patch.get('reset_run'):
            sess_rst = self._get_or_hydrate_session(sid)
            if sess_rst is not None:
                prev_rst = sess_rst.get('progress') or {}
                sess_rst['progress'] = {
                    'solved': False,
                    'current_step': max(1, min(6, prog_clean.get('current_step') or 1)),
                    'step_attempts': 0,
                    'weak_step': _p_int(prev_rst.get('weak_step')) or 0,
                }
                sess_rst['active_qa'] = None
                sess_rst['_replaying'] = True
                sess_rst['seq'] = int(sess_rst.get('seq') or 1) + 1
                if sid in self.sessions and hasattr(self.sessions, 'move_to_end'):
                    self.sessions.move_to_end(sid)
                self.active_session_id = sid
            return {'ok': True}
        if prog_clean.get('solved'):
            prog_clean['current_step'] = max(prog_clean.get('current_step') or 4, 4)
            prog_clean['step_attempts'] = 0
        re_redeemed = bool(prog_clean.get('solved') and patch.get('peeked') is False)
        sess = self._get_or_hydrate_session(sid)
        replaying_unfinished = bool(sess is not None and sess.get('_replaying') and not prog_clean.get('solved'))
        if sess is not None and prog_clean.get('solved'):
            sess.pop('_replaying', None)
        merged_prog = None
        if sess is not None:
            cur = dict(sess.get('progress') or {})
            if cur.get('peeked') and not re_redeemed:
                prog_clean['peeked'] = True
            if cur.get('feynman_ok'):
                prog_clean['feynman_ok'] = True
            if cur.get('solo_ace'):
                prog_clean['solo_ace'] = True
            if cur.get('read_aloud_ok'):
                prog_clean['read_aloud_ok'] = True
            cur.update(prog_clean)
            sess['progress'] = cur
            merged_prog = cur
        changed_hist = False
        hist_ref = None
        if not replaying_unfinished:
            for h in self.history:
                if h.get('id') == sid:
                    prev_h = dict(h.get('progress') or {})
                    cur_h = dict(prev_h)
                    pc = dict(prog_clean)
                    if prev_h.get('peeked') and not re_redeemed:
                        pc['peeked'] = True
                    if prev_h.get('feynman_ok'):
                        pc['feynman_ok'] = True
                    if prev_h.get('solo_ace'):
                        pc['solo_ace'] = True
                    if prev_h.get('read_aloud_ok'):
                        pc['read_aloud_ok'] = True
                    cur_h.update(pc)
                    h['progress'] = cur_h
                    hist_ref = h
                    if merged_prog is None:
                        merged_prog = cur_h
                    if cur_h != prev_h:
                        changed_hist = True
        is_m_replay = str(sid).startswith('m:')
        if changed_hist:
            with contextlib.suppress(Exception):
                _atomic_write_json(HIST_PATH, self.history)
            self._hist_loaded_path = HIST_PATH
            self._hist_sig = self._file_stat_sig(HIST_PATH)
        elif sess is not None and prog_clean.get('solved') and not sess.get('silent_preview') and not is_m_replay:
            self._save_to_history(sess)
        mistake_ev = None
        target_rec = sess or hist_ref
        if target_rec is not None and merged_prog is not None and ('solved' in prog_clean or 'peeked' in prog_clean or int(prog_clean.get('step_attempts') or 0) >= 3) and not is_m_replay:
            mistake_ev = self._enroll_or_refresh_mistake(target_rec, merged_prog)
        if prog_clean.get('solved') and target_rec is not None:
            cw_ref = getattr(self, 'cowrite', None)
            if isinstance(cw_ref, CoWriteMonitor):
                cw_iid = str(target_rec.get('cowrite_item_id') or '').strip()
                cw_mid = str(target_rec.get('mistake_id') or '').strip()
                eff_done = merged_prog or prog_clean
                if (cw_iid or cw_mid) and not bool(eff_done.get('peeked')):
                    is_var_win = (
                        _norm_teach_mode(target_rec.get('teach_mode')) != 'diagnose_draft'
                        or str(target_rec.get('source') or '') in ('variant', 'sprint', 'mistake_retest')
                    )
                    cw_ref.mark_blackboard_solved(item_id=cw_iid, mistake_id=cw_mid, is_variant=is_var_win)
        out = {'ok': True}
        if mistake_ev:
            out['mistake_event'] = mistake_ev
        eff_prog = merged_prog or prog_clean
        if eff_prog.get('solved') or eff_prog.get('peeked'):
            pub_solved = self.public_session(sid)
            if pub_solved:
                if pub_solved.get('read_aloud_target'):
                    out['read_aloud_target'] = pub_solved['read_aloud_target']
                if pub_solved.get('clever_calc'):
                    out['clever_calc'] = pub_solved['clever_calc']
        return out

    def delete_history(self, sid: str = '', clear_all: bool = False) -> dict:
        self._ensure_history_loaded()
        if clear_all:
            self.history = []
            self.sessions.clear()
            self.active_session_id = ''
        elif sid:
            self.history = [h for h in self.history if h.get('id') != sid]
            self.sessions.pop(sid, None)
            if self.active_session_id == sid:
                self.active_session_id = ''
        self.phone_studio.prune_deleted_sessions(sid=sid, clear_all=clear_all)
        with contextlib.suppress(Exception):
            _atomic_write_json(HIST_PATH, self.history)
        self._hist_loaded_path = HIST_PATH
        self._hist_sig = self._file_stat_sig(HIST_PATH)
        return {'ok': True, 'count': len(self.history)}

    def start_golden_sample(self, sample_id: str, open_ui: bool = False, prefetch_tts: bool = True, solo_challenge: bool = False) -> dict:
        sp = SAMPLE_BY_ID.get(sample_id)
        if not sp:
            raise LookupError('未找到该样题')
        self._cancel_previous_session_work()
        sess = self._new_session_record(sp['title'], sp['question'], 'golden', 'sample')
        steps = []
        for idx, raw_st in enumerate(sp['golden_steps'], 1):
            steps.append({
                'step': idx,
                'title': raw_st['title'],
                'say': raw_st['say'],
                'board': raw_st['board'],
                'quiz': sanitize_quiz(raw_st.get('quiz'), idx, sample_id),
                'draw': sanitize_draw_ops(raw_st['draw']),
            })
        sess['sample_id'] = sample_id
        sess['solo_challenge'] = bool(solo_challenge)
        sess['steps'] = steps
        sess['status'] = 'ready'
        sess['metrics'] = {'first_step_s': 0.02, 'total_s': 0.02, 'model': '内置金牌教研微课'}
        sess['seq'] += 1
        if open_ui:
            self.trigger_open_ui()
        if prefetch_tts:
            self._spawn(self.pause_music_if_playing())
            self._spawn(self._prefetch_steps_tts_sequentially(steps), session_work=True)
        return self.public_session(sess['id'])

    def start_live_problem(self, question: str = '', image_bytes: bytes = None,
                           mode: str = 'fast', title: str = '', source: str = 'custom',
                           open_ui: bool = False, pedagogy: str = 'auto',
                           teach_mode: str = 'standard', mistake_id: str = '',
                           subq_meta: dict = None, solo_challenge: bool = False,
                           sprint_meta: dict = None, silent_tts: bool = False,
                           cowrite_item_id: str = '') -> dict:
        clean_q = str(question or '').strip()
        if not clean_q and not image_bytes:
            raise ValueError('请提供题目文字或题目图片')
        use_mode = _norm_mode(mode)
        use_ped = _norm_pedagogy(pedagogy)
        use_tmode = _norm_teach_mode(teach_mode)
        if use_tmode == 'standard' and clean_q and re.search(
            r'哪一步(?:算|写|做)?错了|算[得的]对不对|帮我(?:看看|检查).*草稿|看看我(?:写|算)的(?:草稿|算式|过程)|找错因',
            clean_q,
        ):
            use_tmode = 'diagnose_draft'

        auto_subq = subq_meta
        if auto_subq is None and clean_q and not image_bytes:
            sp_q = split_multipart_subquestions(clean_q)
            if sp_q and len(sp_q['parts']) >= 2:
                auto_subq = {
                    'full_question': clean_q,
                    'stem': sp_q['stem'],
                    'parts': sp_q['parts'],
                    'cur_idx': 1,
                    'total': len(sp_q['parts']),
                    'proven': [],
                }
        llm_clean_q = clean_q
        disp_q = '' if image_bytes else clean_q
        if isinstance(auto_subq, dict) and auto_subq.get('parts'):
            c_idx_sq = max(1, min(len(auto_subq['parts']), int(auto_subq.get('cur_idx') or 1)))
            cur_p = auto_subq['parts'][c_idx_sq - 1]
            prov_list = auto_subq.get('proven') or []
            prov_txt = ('\n【前问已证条件：' + '；'.join(prov_list) + '】') if prov_list else ''
            stem_sq = auto_subq['stem'].rstrip('。；;：:，, ')
            disp_q = f"{stem_sq}。{cur_p['label']}：{cur_p['text']}"
            llm_clean_q = (
                f"题干公共条件：{auto_subq['stem']}{prov_txt}\n"
                f"【压轴大题拆小问模式（共{len(auto_subq['parts'])}问）：当前请只聚焦用 4 步启发式引导攻克 {cur_p['label']}：「{cur_p['text']}」，切勿在本轮提前解答其他小问！】"
            )
        init_q = '' if image_bytes else disp_q
        is_essay_crit = is_essay_critique_request(clean_q, use_tmode)
        eff_tmode = 'diagnose_draft' if is_essay_crit else use_tmode
        eff_solo = bool(solo_challenge) and (not is_essay_crit) and (not detect_writing_task(clean_q)) and (use_tmode != 'diagnose_draft')

        # In-flight deduplication: prevent double-taps from spawning duplicate concurrent LLM streams while respecting mode/pedagogy/subq/solo changes!
        if init_q and not image_bytes and self.active_session_id:
            act = self.sessions.get(self.active_session_id)
            if (
                act
                and act.get('question') == init_q
                and act.get('mode') == use_mode
                and _norm_teach_mode(act.get('teach_mode')) == eff_tmode
                and _norm_pedagogy(act.get('pedagogy')) == use_ped
                and bool(act.get('solo_challenge')) == eff_solo
                and bool(act.get('silent_start')) == bool(silent_tts)
                and act.get('status') in ('thinking', 'streaming')
                and (time.monotonic() - act.get('t0_mono', 0)) < 45.0
            ):
                if open_ui:
                    self.trigger_open_ui()
                return self.public_session(act['id'])

        self._cancel_previous_session_work()
        image_id = ''
        if image_bytes:
            SNAP_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
            image_id = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
            img_path = SNAP_DIR / f'{image_id}.jpg'
            _atomic_write_bytes(img_path, image_bytes, mode=0o600)
            snap_entries = []
            for sp_p in SNAP_DIR.glob('*.jpg'):
                if sp_p.name.startswith(('c0_', 'dc_')):
                    continue
                with contextlib.suppress(OSError):
                    st_sp = sp_p.lstat()
                    if stat.S_ISREG(st_sp.st_mode):
                        snap_entries.append((st_sp.st_mtime_ns, sp_p))
            snap_entries.sort(key=lambda x: x[0], reverse=True)
            protected_snaps = set()
            for _src_list in (list(self.sessions.values()), self.history, getattr(self, 'mistakes', ())):
                for _it in (_src_list or ()):
                    if isinstance(_it, dict):
                        _iu = str(_it.get('image_url') or '')
                        if _iu.startswith('/api/tutor/snap/'):
                            protected_snaps.add(_iu.rsplit('/', 1)[-1])
            for idx_sp, (_, old_p) in enumerate(snap_entries[25:], start=25):
                if idx_sp < 120 and old_p.name in protected_snaps:
                    continue
                with contextlib.suppress(OSError):
                    old_p.unlink(missing_ok=True)

        default_img_t = '习作升格精批微课' if is_essay_crit else ('草稿错因诊断微课' if use_tmode == 'diagnose_draft' else '拍题动态黑板微课')
        clean_title = '习作升格精批微课' if (is_essay_crit and title.strip() == '草稿错因诊断微课') else title.strip()
        auto_title = clean_title if clean_title else (init_q[:18] + ('…' if len(init_q) > 18 else '') if init_q else default_img_t)
        sess = self._new_session_record(auto_title, init_q, use_mode, source, image_id=image_id)
        sess['subject'] = detect_subject(clean_q, teach_mode=eff_tmode, essay_critique=is_essay_crit) if clean_q else ''
        sess['teach_mode'] = eff_tmode
        sess['essay_critique'] = bool(is_essay_crit)
        sess['solo_challenge'] = eff_solo
        sess['pedagogy'] = use_ped
        sess['mistake_id'] = str(mistake_id or '').strip()[:28]
        sess['silent_start'] = bool(silent_tts)
        sess['cowrite_item_id'] = str(cowrite_item_id or '').strip()[:24]
        if isinstance(sprint_meta, dict):
            sess['sprint_meta'] = copy.deepcopy(sprint_meta)
        if isinstance(auto_subq, dict):
            sess['subq_meta'] = auto_subq
        if init_q:
            sc0 = build_step0_scaffold(init_q, teach_mode=use_tmode, essay_critique=is_essay_crit, from_ocr=False)
            sess['steps'].append({
                'step': 0,
                'title': sc0['title'],
                'say': sc0['say'],
                'board': sc0['board'],
                'draw': sc0['draw'],
            })
            sess['status'] = 'streaming'
            sess['seq'] += 1
        elif image_bytes:
            is_draft = (use_tmode == 'diagnose_draft')
            say0 = (
                '好嘞，老师正在认真看你的手写草稿和原题，马上先夸夸你写对的地方，再一起找找哪一步能优化！'
                if is_draft else
                '好嘞，老师已经看到拍下来的题目啦，我们先拆解已知条件与所求目标，马上在黑板上画图引导。'
            )
            note_suffix = f'\n【补充要求】{clean_q}' if clean_q else ''
            sess['steps'].append({
                'step': 0,
                'title': '审阅草稿 · 定位关键步' if is_draft else '看图审题 · 拆解条件',
                'say': say0,
                'board': ('正在识别原题与手写草稿步骤…' if is_draft else '正在识别题目图形与已知数据…') + note_suffix,
                'draw': build_step0_warmup_draw(''),
            })
            sess['status'] = 'streaming'
            sess['seq'] += 1

        if open_ui:
            self.trigger_open_ui()
        if not silent_tts:
            self._spawn(self.pause_music_if_playing())
        self._spawn(self._run_live_pipeline(sess, llm_clean_q, image_bytes, use_mode, use_ped), session_work=True)
        return self.public_session(sess['id'])

    async def generate_variant_problem(
        self, sid: str, mode: str = 'fast', difficulty: str = 'same', pedagogy: str = 'auto', weak_step: int = 0, weak_focus: str = '', solo_challenge: bool = False, silent_tts=None,
    ) -> dict:
        pub = self.public_session(sid)
        if not pub or not pub.get('question'):
            raise LookupError('请先打开一道题目，再生成同类变式题')
        is_harder = str(difficulty or '').strip().lower() == 'harder'
        mb_mid_v = str(pub.get('mistake_id') or '').strip()
        is_cw_mb = False
        if mb_mid_v:
            self._ensure_mistakes_loaded()
            is_cw_mb = any(
                m.get('id') == mb_mid_v and str(m.get('error_tag') or '').startswith('伴写·')
                for m in (self.mistakes or ())
            )
        use_ped_var = 'junior' if (pub.get('source') == 'cowrite' or is_cw_mb) else (pedagogy if pedagogy in ('elem', 'junior') else _norm_pedagogy(pub.get('pedagogy')))
        ped_hint = resolve_pedagogy_hint(use_ped_var, pub['question'])
        w_step = int(weak_step or 0)
        if not is_harder and w_step <= 0:
            w_step = int((pub.get('progress') or {}).get('weak_step') or 0)
        focus_txt = str(weak_focus or '').strip()[:64]
        if w_step > 0 and not focus_txt:
            st_hit = next((s for s in (pub.get('steps') or []) if s.get('step') == w_step), None)
            if st_hit:
                qz_h = st_hit.get('quiz') or {}
                focus_txt = f"{st_hit.get('title', '')}（{qz_h.get('q', '')}）".strip('（） ')[:64]
        subj_var = str(pub.get('subject') or detect_subject(pub['question']))
        is_non_math = subj_var != 'math' or bool(pub.get('essay_critique')) or bool(pub.get('writing_task'))
        if pub.get('essay_critique'):
            diff_instruction = '请围绕同类记叙/应用写作技法，新给出一段含典型平淡流水账与1处小语病的学生短段落（冠以【作文升格精批】前缀，50~80字），供孩子实战练习赏识修改与细节升格！'
            prefix_lbl = '进阶升格练' if is_harder else '同类升格练'
        elif pub.get('writing_task'):
            is_en_wr = pub.get('writing_task') == 'english_essay'
            diff_instruction = (
                '请围绕相近的话题体裁与书信/短文框架，更换全新生活主题，新拟一道初中/小学【英语书面表达写作变式题】（题干中明确写出“【英语书面表达】Write a short passage...”及不少于60/80词要求，严禁直接附带范文）！'
                if is_en_wr else
                '请围绕相同的立意选材与慢镜头细节训练目标，更换全新题目情境，新拟一道中小学【语文半命题或命题作文构思变式题】（题干中明确写出“【语文作文】请以《…》为题…不少于500字”，严禁附带范文）！'
            )
            prefix_lbl = '进阶写作练' if is_harder else '同类写作练'
        elif is_non_math:
            if is_harder:
                diff_instruction = '请保持完全相同的学科核心考点与解题逻辑，结合跨情境综合材料或巧妙设问新编一道【同考点进阶拔高题】（若含理化计算请先在 check_calc 内部验算确保整除合理）！'
                prefix_lbl = '进阶练'
            elif w_step > 0:
                diff_instruction = f"孩子刚才主要在「第{w_step}关：{focus_txt or '核心概念辨析'}」卡壳！请更换全新语境或实验/文本素材，专门针对该薄弱点新编一道【同考点靶向巩固题】（若含计算请先在 check_calc 验算）！"
                prefix_lbl = f'靶向练(第{w_step}关)'
            else:
                diff_instruction = '请保持完全相同的学科考点、语法/阅读/实验/读图模型与易错陷阱，更换全新语境素材与数据，新编一道【同考点巩固变式题】（若含计算请先在 check_calc 验算）！'
                prefix_lbl = '变式练'
        elif is_harder:
            diff_instruction = (
                '请基于这道题的核心数学模型，为孩子新编一道【进阶拔高变式题】（在保持同模型的基础上巧妙增加一个小弯，例如逆向倒推求原条件、增加单位换算、或把“几倍少几”与“几倍多几”灵活变形），务必先自己在 check_calc 中完整心算一遍确保所有结果整除、几何边长合法！'
            )
            prefix_lbl = '进阶练'
        elif w_step > 0:
            diff_instruction = (
                f"孩子刚才做原题时，主要在「第{w_step}关：{focus_txt or '关键数量关系辨析'}」卡壳出错！请专门针对这一薄弱思维坎，为孩子新编一道生活场景新颖、专门强化训练该薄弱关系的【靶向巩固变式题】，务必先自己在 check_calc 中完整心算一遍确保所有结果除得尽、几何长度合法！"
            )
            prefix_lbl = f'靶向练(第{w_step}关)'
        else:
            diff_instruction = (
                '请基于这道题相同的数学模型与解题方法，为孩子新编一道生活场景新颖、难度相当的【同类巩固变式题】，务必先自己在 check_calc 中完整心算一遍确保所有结果除得尽、几何长度合法！'
            )
            prefix_lbl = '变式练'
        need_calc_chk = not (pub.get('essay_critique') or pub.get('writing_task') or subj_var in ('chinese', 'english', 'history', 'civics'))
        calc_json_fld = '"check_calc":"内部验算整除过程",' if need_calc_chk else ''
        prompt = (
            f"原题：「{pub['question']}」\n"
            f"{ped_hint}\n"
            f"{diff_instruction}\n"
            f'只输出一行JSON（不要代码块）：{{{calc_json_fld}"title":"{prefix_lbl}：...(10字内)","question":"完整变式题目文字"}}'
        )
        content = await self._post_chat_once('aux', [{'role': 'user', 'content': prompt}], temperature=0.45, timeout_s=36.0)
        objs, _ = extract_json_objects_from_buffer(content)
        if not objs or not objs[0].get('question'):
            raise RuntimeError('变式题生成格式异常，请再试一次')
        v_title = str(objs[0].get('title') or f'{prefix_lbl}变式题')[:26]
        v_q = sanitize_ocr_question(str(objs[0]['question']).strip())
        is_var_ec = bool(pub.get('essay_critique'))
        pub_wt_var = str(pub.get('writing_task') or '')
        if is_var_ec and not is_essay_critique_request(v_q, 'diagnose_draft'):
            v_q = '【作文升格精批】' + v_q
        elif pub_wt_var and not detect_writing_task(v_q):
            v_q = ('【英语书面表达】' if pub_wt_var == 'english_essay' else '【语文作文】') + v_q
        elif use_ped_var == 'junior':
            gv_tag = {'chinese': '【初中语文】', 'english': '【初中英语】', 'physics': '【初中物理】', 'chemistry': '【初中化学】'}.get(subj_var, '')
            if gv_tag and not v_q.startswith('【') and detect_subject(v_q) == 'math':
                v_q = f'{gv_tag}{v_q}'
        eff_sil_var = bool(silent_tts) if isinstance(silent_tts, bool) else bool((self.sessions.get(sid) or pub or {}).get('silent_start'))
        var_src = 'cowrite' if (pub.get('source') == 'cowrite' or is_cw_mb) else 'variant'
        new_sess = self.start_live_problem(
            question=v_q, mode=_norm_mode(mode), title=v_title, source=var_src,
            pedagogy=use_ped_var, teach_mode='diagnose_draft' if is_var_ec else 'standard',
            mistake_id=mb_mid_v,
            solo_challenge=bool(solo_challenge), silent_tts=eff_sil_var,
            cowrite_item_id=str(pub.get('cowrite_item_id') or '') if var_src == 'cowrite' else '',
        )
        return {'ok': True, 'difficulty': 'harder' if is_harder else 'same', 'weak_step': w_step, 'session': new_sess}

    async def _ocr_image_opening(self, image_bytes: bytes, extra_note: str = '', teach_mode: str = 'standard') -> tuple:
        b64 = base64.b64encode(image_bytes).decode('ascii')
        note_clause = f'（用户补充要求：{extra_note}，若图中有多问或多题请按该要求提取对应题目）' if extra_note else ''
        ocr_instruction = (
            f'请同时观察图片中的原题或学生手写内容{note_clause}：若图中是学生【手写作文/习作段落】，"ocr" 请提取手写习作原文前220字；若图中是【印刷原题+手写解题草稿】，"ocr" 只提取不含手写答案的纯净原题题干（严禁在 say 中报出正确答案）！只输出一行JSON（不要代码块）：{{"title":"8字内诊断标题","ocr":"纯净原题题干或手写习作原文","say":"老师看到你写的内容啦，非常认真！我们先一起来看看亮点与关键思路..."}}'
            if _norm_teach_mode(teach_mode) == 'diagnose_draft' else
            f'请准确识别图片中的各学科原题题干{note_clause}（忽略图片中学生手写的算式草稿或手写答案，严禁把答案抄入ocr或say！），并生成老师开场读题词。只输出一行JSON（不要代码块）：{{"title":"8字内题型标题","ocr":"不含手写答案的纯净原题题干","say":"同学们好，我们一起来看这道题：...(自然口语读出已知条件与问题，严禁说出答案，50字左右)"}}'
        )
        messages = [{
            'role': 'user',
            'content': [
                {
                    'type': 'text',
                    'text': ocr_instruction,
                },
                {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + b64}},
            ],
        }]
        content = await self._post_chat_once('aux', messages, temperature=0.1, timeout_s=16.0)
        objs, _ = extract_json_objects_from_buffer(content)
        if objs:
            obj = objs[0]
            return (
                str(obj.get('title') or '拍题微课')[:24],
                sanitize_ocr_question(obj.get('ocr') or ''),
                str(obj.get('say') or '').strip(),
            )
        fb_ocr = sanitize_ocr_question(content)
        if not fb_ocr or re.search(r'无法识别|看不清|太模糊|抱歉|sorry|cannot\s+read', fb_ocr, re.I):
            fb_ocr = ''
        return ('拍题微课', fb_ocr[:400], '')

    async def _run_live_pipeline(self, sess: dict, clean_q: str, image_bytes: bytes, use_mode: str, use_ped: str = 'auto'):
        t0 = sess['t0_mono']
        tts_q = asyncio.Queue()

        async def _ordered_tts_worker():
            while True:
                txt = await tts_q.get()
                if txt is None:
                    break
                if not sess.get('silent_start'):
                    await self.synthesize_wav(txt)

        tts_worker = self._spawn(_ordered_tts_worker(), session_work=True)
        ocr_task = None
        use_tmode = _norm_teach_mode(sess.get('teach_mode'))
        is_essay_crit = bool(sess.get('essay_critique')) or is_essay_critique_request(clean_q, use_tmode)
        sys_prompt = (
            TUTOR_ESSAY_CRITIQUE_PROMPT if is_essay_crit else
            (TUTOR_DRAFT_DIAGNOSE_PROMPT if use_tmode == 'diagnose_draft' else TUTOR_SYSTEM_PROMPT)
        )
        _m_subj_tag = re.match(r'^(【初中(?:语文|英语|物理|化学)】)', clean_q or '')
        _forced_subj_tag = _m_subj_tag.group(1) if _m_subj_tag else ''
        if image_bytes:
            async def _bg_ocr_fill():
                with contextlib.suppress(Exception):
                    ocr_title, ocr_q, _ = await self._ocr_image_opening(image_bytes, extra_note=clean_q, teach_mode=use_tmode)
                    if ocr_q and not sess.get('question'):
                        if _forced_subj_tag and not ocr_q.startswith('【') and detect_subject(ocr_q) == 'math':
                            ocr_q = f'{_forced_subj_tag}{ocr_q}'
                        sess['question'] = ocr_q[:600]
                        if is_essay_critique_request(ocr_q, use_tmode):
                            sess['essay_critique'] = True
                            sess['solo_challenge'] = False
                        elif detect_writing_task(ocr_q):
                            sess['solo_challenge'] = False
                        sess['title'] = (ocr_title if ocr_title and ocr_title != '拍题微课' else (ocr_q[:18] + ('…' if len(ocr_q) > 18 else '')))[:24]
                        if sess['steps'] and sess['steps'][0].get('step') == 0:
                            sc_bg = build_step0_scaffold(sess['question'], teach_mode=use_tmode, essay_critique=bool(sess.get('essay_critique')), from_ocr=True)
                            sess['steps'][0]['title'] = sc_bg['title']
                            sess['steps'][0]['say'] = sc_bg['say']
                            sess['steps'][0]['board'] = sc_bg['board']
                            sess['steps'][0]['draw'] = sc_bg['draw']
                        sess['seq'] += 1
            ocr_task = self._spawn(_bg_ocr_fill(), session_work=True)
        try:
            if sess['steps'] and sess['steps'][0].get('say'):
                tts_q.put_nowait(sess['steps'][0]['say'])

            stream_targets = self._iter_llm_targets(use_mode)
            if not stream_targets:
                raise RuntimeError(f'未配置 {use_mode} 讲题大模型')

            ped_hint = resolve_pedagogy_hint(use_ped, clean_q)
            r_traps = detect_reading_traps(clean_q)
            if r_traps:
                ped_hint += '\n【审题防粗心重点提醒：本题含 ' + '；'.join(t['badge'] for t in r_traps) + '，请务必在第1~2步引导孩子注意该审题坑！】'
            subj_init = detect_subject(clean_q)
            is_choice_s3_subj = bool(detect_writing_task(clean_q)) or subj_init in ('chinese', 'english', 'history', 'civics')
            s3_gate_desc = '第3步 quiz 必须加 "force_choice":true 用三选一完成核心辨析/升格（严禁使用数字填空 □），第4步再揭晓完整解析复盘' if is_choice_s3_subj else '数值计算题第3步列式用 = □ 留白让孩子自己算（语文/英语/文综/写作题第3步加 "force_choice":true 三选一），第4步再核对复盘'
            if image_bytes:
                b64 = base64.b64encode(image_bytes).decode('ascii')
                extra_hint = f'\n补充说明：{clean_q}' if clean_q else ''
                img_task_txt = (
                    f'请仔细识别图片中孩子的【手写习作/段落草稿】，严格按每行一个JSON（NDJSON）输出4步「习作赏识升格精批」微课（严禁重写整篇范文！第1步表扬闪光词句并3选1诊断结构详略，第2步揪出1处错别字/病句/语法瑕疵3选1订正，第3步挑1句平淡句加 "force_choice":true 三选一升格慢镜头细节，第4步原句与升格句对照复盘；第1步JSON请带 "ocr":"识别出的手写习作原文前200字"）：{extra_hint}'
                    if is_essay_crit else (
                        f'{ped_hint}\n请仔细同时识别图片中的【印刷原题】与【孩子手写草稿/作答】，严格按每行一个JSON（NDJSON）输出4步「拍草稿找错因」启发订正微课（第1步先表扬写对部分并让孩子选出哪一步开始偏航，第2步标出错因辨析正确关系，第3步数值题用 = □ 留白重算、文科/英语题加 "force_choice":true 三选一订正，第4步对比原草稿误区与正确复盘；若图中识别到的是手写作文段落，则按闪光点肯定→改病句→force_choice升格平淡句→对照复盘输出；第1步JSON必须带 "ocr":"纯净原题或习作原文"）：{extra_hint}'
                        if use_tmode == 'diagnose_draft' else
                        f'{ped_hint}\n请仔细识别图片中的题目，并严格按每行一个JSON（NDJSON）流式输出4步启发式闯关动态黑板微课（第1~3步必须带 quiz 互动提问，{s3_gate_desc}；第1步JSON请额外附带 "ocr":"识别出的完整原题文字" 字段）：{extra_hint}'
                    )
                )
                user_content = [
                    {'type': 'text', 'text': img_task_txt},
                    {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + b64}},
                ]
            else:
                if is_essay_crit:
                    txt_task_hdr = (
                        f'请针对以下孩子的习作草稿/段落，按每行一个JSON（NDJSON）流式输出4步「习作赏识升格精批」微课'
                        f'（严禁重写整篇范文！第1步夸亮点+诊断结构，第2步揪病句/错词/语法错误，第3步加 "force_choice":true 三选一升格平淡句，第4步原句与升格句对照复盘）：\n{clean_q}'
                    )
                elif use_tmode == 'diagnose_draft':
                    txt_task_hdr = f'{ped_hint}\n请针对以下题目与学生作答草稿，按每行一个JSON（NDJSON）流式输出4步「草稿找错因」苏格拉底订正微课（第1~3步带 quiz，{s3_gate_desc}）：\n{clean_q}'
                else:
                    txt_task_hdr = f'{ped_hint}\n请严格按每行一个JSON（NDJSON）流式输出4步启发式闯关动态黑板微课（第1~3步必须带 quiz 互动提问，{s3_gate_desc}）：\n{clean_q}'
                user_content = txt_task_hdr

            parsed_count = 0
            replaced_step0_note = False
            buf = ''

            def _ingest_obj(obj: dict):
                nonlocal parsed_count, replaced_step0_note
                if not isinstance(obj, dict) or ('say' not in obj and 'board' not in obj):
                    return
                ocr_txt = sanitize_ocr_question(obj.get('ocr') or '')
                if ocr_txt and not sess['question']:
                    if _forced_subj_tag and not ocr_txt.startswith('【') and detect_subject(ocr_txt) == 'math':
                        ocr_txt = f'{_forced_subj_tag}{ocr_txt}'
                    sess['question'] = ocr_txt[:600]
                    if is_essay_critique_request(ocr_txt, use_tmode):
                        sess['essay_critique'] = True
                        sess['solo_challenge'] = False
                    elif detect_writing_task(ocr_txt):
                        sess['solo_challenge'] = False
                    sess['title'] = ocr_txt[:18] + ('…' if len(ocr_txt) > 18 else '')
                    if sess['steps'] and sess['steps'][0].get('step') == 0:
                        sc_in = build_step0_scaffold(sess['question'], teach_mode=use_tmode, essay_critique=bool(sess.get('essay_critique')), from_ocr=True)
                        sess['steps'][0]['title'] = sc_in['title']
                        sess['steps'][0]['say'] = sc_in['say']
                        sess['steps'][0]['board'] = sc_in['board']
                        sess['steps'][0]['draw'] = sc_in['draw']
                if sess['metrics']['first_step_s'] is None:
                    sess['metrics']['first_step_s'] = round(time.monotonic() - t0, 2)
                if not replaced_step0_note and sess['steps'] and sess['steps'][0].get('step') == 0:
                    if not sess['steps'][0].get('draw') or len(sess['steps'][0]['draw']) == 1:
                        sc_fb0 = build_step0_scaffold(sess.get('question') or '', teach_mode=use_tmode, essay_critique=bool(sess.get('essay_critique')), from_ocr=True)
                        sess['steps'][0]['draw'] = sc_fb0['draw']
                    replaced_step0_note = True
                # Guard against 1-shot unguided answer dump: if Step 1 arrives without quiz and dumps full arithmetic solution, split into 4 Socratic steps immediately!
                if parsed_count == 0 and obj.get('quiz') is None:
                    rescued_steps = decompose_oneshot_dump(obj, sess['id'], sess.get('question') or clean_q)
                    if rescued_steps:
                        for rs_st in rescued_steps:
                            parsed_count += 1
                            rs_enriched = enrich_step_draw_scaffolds(sess['steps'], dedupe_step3_redundant_rungs(sess['steps'], rs_st))
                            sess['steps'].append(rs_enriched)
                            if rs_enriched.get('say'):
                                tts_q.put_nowait(rs_enriched['say'])
                        scrub_prior_steps_final_spoilers(sess['steps'], sess.get('question') or clean_q)
                        sess['status'] = 'streaming'
                        sess['seq'] += 1
                        return
                parsed_count += 1
                raw_qz_in = obj.get('quiz')
                q_now_ing = sess.get('question') or clean_q
                eff_ec_ing = bool(sess.get('essay_critique')) or is_essay_crit
                eff_wr_ing = eff_ec_ing or bool(detect_writing_task(q_now_ing))
                if parsed_count == 3 and isinstance(raw_qz_in, dict):
                    subj_now = detect_subject(q_now_ing, teach_mode=use_tmode, essay_critique=eff_ec_ing)
                    if eff_wr_ing or (subj_now in ('chinese', 'english', 'history', 'civics', 'biology', 'geography') and '□' not in str(obj.get('board') or '')):
                        raw_qz_in = dict(raw_qz_in, force_choice=True)
                quiz_obj = sanitize_quiz(raw_qz_in, parsed_count, sess['id'])
                if quiz_obj is None and parsed_count in (1, 2, 3):
                    quiz_obj = synthesize_fallback_quiz(parsed_count, obj, q_now_ing, sess['id'])
                _fb_titles = {1: '审题抓突破口', 2: '辨析关键关系', 3: '独立列式通关', 4: '核对验算复盘'}
                raw_st_title = str(obj.get('title') or obj.get('step_title') or obj.get('heading') or obj.get('name') or _fb_titles.get(parsed_count, f'第 {parsed_count} 步')).strip()
                subj_ing_t = detect_subject(q_now_ing, teach_mode=use_tmode, essay_critique=eff_ec_ing)
                if eff_wr_ing and re.search(r'重算|手算|算式|验算', raw_st_title):
                    raw_st_title = '细节慢镜头升格' if parsed_count == 3 else '原句升格·对照复盘'
                elif subj_ing_t in ('chinese', 'english', 'history', 'civics') and (quiz_obj is None or quiz_obj.get('mode') == 'choice'):
                    if parsed_count == 4 and '核对验算' in raw_st_title:
                        raw_st_title = raw_st_title.replace('核对验算', '要点复盘')
                    elif parsed_count == 3 and re.search(r'重算|分步手算', raw_st_title):
                        raw_st_title = '核心要点辨析'
                board_ing = _coerce_multiline_str(obj.get('board'), 380, strip_note_prefix=True)
                if eff_wr_ing:
                    board_ing = re.sub(r'\s*[=＝]\s*□(?=\s|$)', '', board_ing)
                    board_ing = board_ing.replace('订正算式', '词句订正')
                step_item = enrich_step_draw_scaffolds(sess['steps'], dedupe_step3_redundant_rungs(sess['steps'], scrub_step_spoilers({
                    'step': parsed_count,
                    'title': str(raw_st_title).strip()[:24],
                    'say': _coerce_multiline_str(obj.get('say'), 280),
                    'board': board_ing,
                    'quiz': quiz_obj,
                    'draw': sanitize_draw_ops(obj.get('draw') or []),
                })))
                sess['steps'].append(step_item)
                if parsed_count >= 3:
                    scrub_prior_steps_final_spoilers(sess['steps'], sess.get('question') or clean_q)
                sess['status'] = 'streaming'
                sess['seq'] += 1
                if step_item['say']:
                    tts_q.put_nowait(step_item['say'])

            last_http_err = ''
            for idx, (tv1, tkey, m_try, ex_try) in enumerate(stream_targets):
                sess['metrics']['model'] = m_try
                buf = ''
                payload = {
                    'model': m_try,
                    'stream': True,
                    'temperature': 0.2,
                    **ex_try,
                    'messages': [
                        {'role': 'system', 'content': sys_prompt},
                        {'role': 'user', 'content': user_content},
                    ],
                }
                try:
                    first_tok_limit = _FIRST_TOKEN_TIMEOUT_DEEP if use_mode == 'deep' else _FIRST_TOKEN_TIMEOUT_FAST
                    first_content_limit = _FIRST_CONTENT_TIMEOUT_DEEP if use_mode == 'deep' else _FIRST_CONTENT_TIMEOUT_FAST
                    chunk_idle_limit = 28.0
                    got_first_tok = False
                    got_first_content = False
                    now_mono = time.monotonic()
                    first_tok_deadline = now_mono + first_tok_limit
                    first_content_deadline = now_mono + max(first_tok_limit, first_content_limit)
                    async with self.http.post(
                        tv1 + '/chat/completions',
                        json=payload,
                        headers={'Authorization': f'Bearer {tkey}', 'User-Agent': 'ThinkSmartTutor/1.0'},
                        timeout=aiohttp.ClientTimeout(total=92 if use_mode == 'deep' else 78, sock_connect=6, sock_read=30),
                    ) as resp:
                        if resp.status != 200:
                            err_body = _redact_secrets((await resp.text())[:140], tkey)
                            self._mark_llm_target_cooldown(tv1, m_try, resp.status)
                            last_http_err = _redact_secrets(f'{m_try} HTTP {resp.status}: {err_body}', tkey)
                            if idx + 1 < len(stream_targets) and parsed_count == 0:
                                log.warning('Stream model %s failed (%s), failing over to %s', m_try, last_http_err, stream_targets[idx + 1][2])
                                continue
                            log.warning('All stream targets failed: %s', last_http_err)
                            raise RuntimeError(self._humanize_llm_error(last_http_err))
                        import codecs as _codecs
                        utf8_dec = _codecs.getincrementaldecoder('utf-8')(errors='replace')
                        sse_rem = ''
                        while True:
                            if not got_first_tok:
                                rem_w = first_tok_deadline - time.monotonic()
                                if rem_w <= 0:
                                    raise asyncio.TimeoutError('first_token_timeout')
                                read_wait = min(chunk_idle_limit, max(0.05, rem_w))
                            elif not got_first_content:
                                rem_c = first_content_deadline - time.monotonic()
                                if rem_c <= 0:
                                    raise asyncio.TimeoutError('first_content_timeout')
                                read_wait = min(chunk_idle_limit, max(0.05, rem_c))
                            else:
                                read_wait = chunk_idle_limit
                            if hasattr(resp.content, 'readany'):
                                raw_bytes = await asyncio.wait_for(resp.content.readany(), timeout=read_wait)
                            else:
                                raw_bytes = await asyncio.wait_for(resp.content.read(8192), timeout=read_wait)
                            if not raw_bytes:
                                sse_tail = sse_rem + utf8_dec.decode(b'', final=True)
                                sse_lines = sse_tail.splitlines() if sse_tail else []
                                sse_rem = ''
                            else:
                                sse_text = sse_rem + utf8_dec.decode(raw_bytes, final=False)
                                if sse_text.endswith(('\n', '\r')):
                                    sse_lines = sse_text.splitlines()
                                    sse_rem = ''
                                else:
                                    parts_ln = sse_text.splitlines(keepends=True)
                                    if parts_ln and not parts_ln[-1].endswith(('\n', '\r')):
                                        sse_rem = parts_ln.pop()
                                    else:
                                        sse_rem = ''
                                    sse_lines = [p_ln.rstrip('\r\n') for p_ln in parts_ln]
                            done_flag = False
                            for raw_ln in sse_lines:
                                ln = raw_ln.strip()
                                if not ln.startswith('data:'):
                                    continue
                                ds = ln[5:].strip()
                                if ds == '[DONE]':
                                    done_flag = True
                                    break
                                try:
                                    j = json.loads(ds)
                                except Exception:
                                    continue
                                chs = j.get('choices') or []
                                if not chs:
                                    continue
                                delta = chs[0].get('delta') or {}
                                ef = delta.get('extra_fields')
                                r_tok = delta.get('reasoning_content') or (ef.get('reasoning_content') if isinstance(ef, dict) else '') or ''
                                if r_tok:
                                    got_first_tok = True
                                c = delta.get('content') or ''
                                if c:
                                    got_first_tok = True
                                    got_first_content = True
                                    buf += c
                                    if len(buf) > 131072:
                                        buf = buf[-65536:]
                                    found_objs, buf = extract_json_objects_from_buffer(buf)
                                    for fo in found_objs:
                                        _ingest_obj(fo)
                            if done_flag or not raw_bytes:
                                break
                    if buf.strip():
                        found_objs, _ = extract_json_objects_from_buffer(buf)
                        for fo in found_objs:
                            _ingest_obj(fo)
                        buf = ''
                    if parsed_count > 0:
                        break
                    self._mark_llm_target_cooldown(tv1, m_try, transient=True)
                    last_http_err = f'{m_try}: empty_stream'
                    if idx + 1 < len(stream_targets):
                        log.warning('Stream model %s returned 0 steps, failing over to %s', m_try, stream_targets[idx + 1][2])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if isinstance(exc, (asyncio.TimeoutError, aiohttp.ClientError)):
                        self._mark_llm_target_cooldown(tv1, m_try, transient=True)
                    last_http_err = _redact_secrets(f'{m_try} {type(exc).__name__}: {exc}', tkey)
                    if idx + 1 < len(stream_targets) and parsed_count == 0:
                        log.warning('Stream model %s errored (%s), failing over to %s', m_try, last_http_err, stream_targets[idx + 1][2])
                        continue
                    if image_bytes and parsed_count == 0:
                        log.warning('Stream model %s errored on image (%s), checking OCR text fallback', m_try, last_http_err)
                        break
                    raise RuntimeError(self._humanize_llm_error(last_http_err))

            # OCR-assisted text fallback: if multimodal stream returned 0 JSON steps (e.g. narrow/partial crop), rescue via recognized OCR text on active model!
            if parsed_count == 0 and image_bytes:
                if ocr_task and not ocr_task.done():
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(ocr_task, timeout=3.5)
                ocr_q_fb = str(sess.get('question') or '').strip()
                fb_targets = self._iter_llm_targets(use_mode)
                if ocr_q_fb and fb_targets:
                    fb_v1, fb_key, m_fb, ex_fb = fb_targets[0]
                    sess['metrics']['model'] = m_fb
                    fb_is_ec = bool(sess.get('essay_critique')) or is_essay_critique_request(ocr_q_fb, use_tmode)
                    fb_sys = TUTOR_ESSAY_CRITIQUE_PROMPT if fb_is_ec else (TUTOR_DRAFT_DIAGNOSE_PROMPT if use_tmode == 'diagnose_draft' else TUTOR_SYSTEM_PROMPT)
                    fb_prompt = (
                        f"请针对以下识别出的习作草稿片段，严格按每行一个JSON（NDJSON）输出4步「习作赏识升格精批」微课（严禁重写整篇范文，第3步加 force_choice:true 三选一升格平淡句）：\n{ocr_q_fb}"
                        if fb_is_ec else (
                            f"{resolve_pedagogy_hint(use_ped, ocr_q_fb)}\n"
                            f"请针对以下识别出的题目与学生草稿内容，严格按每行一个JSON（NDJSON）流式输出4步「草稿找错因」苏格拉底订正微课（第1步先肯定写对部分并找偏航点，第2步辨析错因，第3步计算题用 = □ 留白、文科或英语题加 force_choice:true，第4步复盘对照）：\n{ocr_q_fb}"
                            if use_tmode == 'diagnose_draft' else
                            f"{resolve_pedagogy_hint(use_ped, ocr_q_fb)}\n"
                            f"请根据以下识别出的题目文字（若末尾个别字被裁切请按各学科常见题意自然补全），严格按每行一个JSON（NDJSON）流式输出4步启发式闯关动态黑板微课（第1~3步必须带 quiz 互动提问，第3步计算题用 = □ 留白、文科或英语题加 force_choice:true，第4步再核对复盘）：\n{ocr_q_fb}"
                        )
                    )
                    with contextlib.suppress(Exception):
                        buf = ''
                        async with self.http.post(
                            fb_v1 + '/chat/completions',
                            json={'model': m_fb, 'stream': True, 'temperature': 0.2, **ex_fb, 'messages': [
                                {'role': 'system', 'content': fb_sys},
                                {'role': 'user', 'content': fb_prompt},
                            ]},
                            headers={'Authorization': f'Bearer {fb_key}', 'User-Agent': 'ThinkSmartTutor/1.0'},
                            timeout=aiohttp.ClientTimeout(total=65, sock_connect=8, sock_read=40),
                        ) as resp_fb:
                            if resp_fb.status == 200:
                                import codecs as _codecs
                                fb_dec = _codecs.getincrementaldecoder('utf-8')(errors='replace')
                                fb_rem = ''
                                fb_done = False
                                async for raw_b in resp_fb.content.iter_any():
                                    fb_txt = fb_rem + fb_dec.decode(raw_b, final=False)
                                    if fb_txt.endswith(('\n', '\r')):
                                        fb_lines, fb_rem = fb_txt.splitlines(), ''
                                    else:
                                        fb_parts = fb_txt.splitlines(keepends=True)
                                        fb_rem = fb_parts.pop() if (fb_parts and not fb_parts[-1].endswith(('\n', '\r'))) else ''
                                        fb_lines = [p_l.rstrip('\r\n') for p_l in fb_parts]
                                    for raw_l in fb_lines:
                                        ls = raw_l.strip()
                                        if not ls.startswith('data:'):
                                            continue
                                        ds_fb = ls[5:].strip()
                                        if ds_fb == '[DONE]':
                                            fb_done = True
                                            break
                                        with contextlib.suppress(Exception):
                                            j_fb = json.loads(ds_fb)
                                            c_fb = (((j_fb.get('choices') or [{}])[0].get('delta') or {}).get('content')) or ''
                                            if c_fb:
                                                buf += c_fb
                                                if len(buf) > 131072:
                                                    buf = buf[-65536:]
                                                f_objs, buf = extract_json_objects_from_buffer(buf)
                                                for fo in f_objs:
                                                    _ingest_obj(fo)
                                    if fb_done:
                                        break
                                fb_tail = (fb_rem + fb_dec.decode(b'', final=True)).strip()
                                if not fb_done and fb_tail.startswith('data:'):
                                    ds_fb = fb_tail[5:].strip()
                                    if ds_fb and ds_fb != '[DONE]':
                                        with contextlib.suppress(Exception):
                                            j_fb = json.loads(ds_fb)
                                            c_fb = (((j_fb.get('choices') or [{}])[0].get('delta') or {}).get('content')) or ''
                                            if c_fb:
                                                buf += c_fb
                                                f_objs, buf = extract_json_objects_from_buffer(buf)
                                                for fo in f_objs:
                                                    _ingest_obj(fo)
                                if buf.strip():
                                    f_objs, _ = extract_json_objects_from_buffer(buf)
                                    for fo in f_objs:
                                        _ingest_obj(fo)

            if parsed_count == 0:
                raise RuntimeError(self._humanize_llm_error(last_http_err) if last_http_err else '模型未返回有效分步黑板脚本，请重试一次')
            if parsed_count == 1:
                q_ctx = sess.get('question') or clean_q
                is_non_calc_fb = is_essay_crit or bool(detect_writing_task(q_ctx)) or detect_subject(q_ctx) in ('chinese', 'english', 'history', 'civics', 'biology', 'geography')
                s2_fb = scrub_step_spoilers({
                    'step': 2,
                    'title': '辨析关键要点' if is_non_calc_fb else '理清关系 · 独立列式手算',
                    'say': '顺着刚才找到的突破口，结合上下文线索再辨析一下关键要点吧？' if is_non_calc_fb else '顺着刚才找到的突破口，请结合已知条件在草稿本上列式算一算，算好后核对思路吧？',
                    'board': '顺着第 1 步突破口继续辨析关键依据与采分要点' if is_non_calc_fb else '顺着第 1 步突破口继续推导：\n草稿本列式自算：结果 = □',
                    'quiz': synthesize_fallback_quiz(2, {'board': '', 'title': '要点辨析' if is_non_calc_fb else '关系推导'}, q_ctx, sess['id']),
                    'draw': [],
                })
                s3_fb = {
                    'step': 3,
                    'title': '解锁复盘 · 对照原题核验' if is_non_calc_fb else '解锁复盘 · 代回原题检验',
                    'say': '思考得非常认真！落笔后记得回扣题干设问与原句语境再核对一遍哦！' if is_non_calc_fb else '思考得非常认真！算出得数后，记得代回原题每一个已知条件逆向核对一遍哦！',
                    'board': '复盘核验：对照题干设问与材料依据逐点核对无误 ✓' if is_non_calc_fb else '复盘检验：请将草稿本算出的得数代回原题条件核对单位与等量关系 ✓',
                    'quiz': None,
                    'draw': sanitize_draw_ops([{'op': 'note', 'x': 350, 'y': 396, 'label': '好习惯：落笔后回扣原题语境与采分要点自查一遍！' if is_non_calc_fb else '好习惯：算出结果后代回原题条件逆向检验！'}]),
                }
                sess['steps'].extend([s2_fb, s3_fb])
                parsed_count = 3
            elif parsed_count == 3 and sess['steps'] and sess['steps'][-1].get('quiz') is not None:
                q_ctx_end = sess.get('question') or clean_q
                is_ec_end = bool(sess.get('essay_critique')) or is_essay_crit
                is_wr_end = bool(detect_writing_task(q_ctx_end))
                is_hum_end = is_ec_end or is_wr_end or detect_subject(q_ctx_end) in ('chinese', 'english', 'history', 'civics', 'biology', 'geography')
                next_st_no = parsed_count + 1
                s_end_fb = {
                    'step': next_st_no,
                    'title': '升格对照 · 赏析复盘' if is_ec_end else ('构思收官 · 动笔锦囊' if is_wr_end else ('解锁复盘 · 对照原题核验' if is_hum_end else '解锁复盘 · 代回原题检验')),
                    'say': (
                        '太棒啦！把闪光点保留住，再换上刚才挑出的细节好句，整段习作立刻生动传神！快对照黑板朗读体会一下吧！'
                        if is_ec_end else (
                            '构思骨架和细节镜头都搭好啦！现在顺着黑板提纲一气呵成动笔写下来吧！'
                            if is_wr_end else (
                                '闯关成功！落笔后记得回扣题干关键词与材料依据再自查一遍哦！'
                                if is_hum_end else
                                '闯关成功！把算出的结果代回原题每一个已知条件逆向核对一遍，养成满分验算好习惯！'
                            )
                        )
                    ),
                    'board': (
                        '习作升格复盘：\n① 保留原文真情实感闪光句 ✓\n② 订正瑕疵并用五感慢镜头替换平淡句 ✓'
                        if is_ec_end else (
                            '写作提纲复盘：中心立意明确 → 慢镜头细节饱满 → 首尾点题升华 ✓'
                            if is_wr_end else (
                                '复盘核验：回扣题干设问与原句语境逐点核对无误 ✓'
                                if is_hum_end else
                                '复盘检验：将求得结果代回原题条件逆向验算单位与等量关系无误 ✓'
                            )
                        )
                    ),
                    'quiz': None,
                    'draw': sanitize_draw_ops([{'op': 'note', 'x': 350, 'y': 396, 'label': '好习惯：对照黑板要点完整复盘一遍！' if is_hum_end else '好习惯：算出得数后代回原题逆向检验！'}]),
                }
                sess['steps'].append(s_end_fb)
                parsed_count = next_st_no
                tts_q.put_nowait(s_end_fb['say'])

            if ocr_task and not ocr_task.done():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(ocr_task, timeout=2.5)
            if not sess.get('question') and len(sess['steps']) >= 2:
                s1 = sess['steps'][1]
                fb_q = str(s1.get('board') or s1.get('say') or sess.get('title') or '').replace('\n', ' ').strip()
                if fb_q:
                    sess['question'] = fb_q[:300]
                    if sess['steps'][0].get('step') == 0 and '正在识别' in str(sess['steps'][0].get('board') or ''):
                        sc_end = build_step0_scaffold(sess['question'], teach_mode=use_tmode, essay_critique=bool(sess.get('essay_critique')), from_ocr=True)
                        sess['steps'][0]['title'] = sc_end['title']
                        sess['steps'][0]['board'] = sc_end['board']
                        sess['steps'][0]['draw'] = sc_end['draw']

            sess['metrics']['total_s'] = round(time.monotonic() - t0, 2)
            sess['status'] = 'ready'
            sess['seq'] += 1
            self._save_to_history(sess)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            safe_err = _redact_secrets(e)
            log.warning('Tutor live pipeline failed: %s', safe_err)
            sess['status'] = 'error'
            sess['error'] = safe_err[:180]
            sess['seq'] += 1
        finally:
            if ocr_task and not ocr_task.done():
                ocr_task.cancel()
            tts_q.put_nowait(None)
            if sess.get('status') != 'ready' and not tts_worker.done():
                tts_worker.cancel()

    async def ask_followup(
        self, sid: str, ask_text: str, upto_idx: int = None, pedagogy: str = 'auto', formula_veiled: bool = False, unsolved_gate: bool = False, silent_tts=None,
    ) -> dict:
        sess = self._get_or_hydrate_session(sid)
        if not sess:
            raise LookupError('未找到当前微课会话')
        q_ask = str(ask_text or '').strip()[:160]
        if not q_ask:
            raise ValueError('请输入或说出追问内容')
        all_steps = sess.get('steps', [])
        sess_streaming = sess.get('status') in ('thinking', 'streaming')
        if sess_streaming and upto_idx is None:
            upto_idx = max(0, len(all_steps) - 1)
        elif unsolved_gate and upto_idx is None and all_steps:
            upto_idx = 0
        has_quiz_at_or_after = (
            upto_idx is not None
            and 0 <= upto_idx < len(all_steps)
            and any(isinstance((st or {}).get('quiz'), dict) for st in all_steps[upto_idx:])
            and not bool((sess.get('progress') or {}).get('solved'))
        )
        is_mid_guided = bool(
            sess_streaming
            or (unsolved_gate and upto_idx is not None and 0 <= upto_idx < len(all_steps))
            or (upto_idx is not None and 0 <= upto_idx < len(all_steps) - 1)
            or has_quiz_at_or_after
        )
        safe_upto = max(0, min(len(all_steps) - 1, upto_idx if upto_idx is not None else 0)) if all_steps else 0
        vis_steps = all_steps[:safe_upto + 1] if (is_mid_guided and all_steps) else all_steps
        ctx_lines = []
        for i_v, s in enumerate(vis_steps):
            if s.get('step', 0) <= 0:
                continue
            bd_v = str(s.get('board') or '')
            if is_mid_guided and formula_veiled and i_v == len(vis_steps) - 1 and '□' in bd_v:
                bd_v = re.sub(r'([：:])\s*[^=\n＝]+[=＝]\s*□', r'\1 [列式遮蔽中] = □', bd_v)
            ctx_lines.append(f"第{s['step']}步({s['title']}): {bd_v} | 讲解:{s['say']}")
        steps_ctx = '\n'.join(ctx_lines) or '（当前处于第0步读题审题热身阶段，尚未展示后续推导板书）'
        q_full_ask = str(sess.get('question') or '')
        subj_ask = str(sess.get('subject') or detect_subject(q_full_ask))
        is_ec_ask = bool(sess.get('essay_critique')) or bool(detect_writing_task(q_full_ask))
        eff_ped_ask = 'junior' if sess.get('source') == 'cowrite' else (pedagogy if pedagogy in ('elem', 'junior') else _norm_pedagogy(sess.get('pedagogy')))
        ped_hint = resolve_pedagogy_hint(eff_ped_ask, q_full_ask)
        begging_now = _is_answer_begging(q_ask)
        analogy_way = '贴近生活的小场景例子' if (is_ec_ask or subj_ask in ('chinese', 'english', 'history', 'civics', 'biology', 'geography')) else '超级简单的生活小数字类比'
        veil_extra = '且当前步算式尚处于遮蔽独立列式阶段，请用生活小例子讲清核心关系，严禁直接写出原题数字算式！' if formula_veiled else ''
        beg_extra = (
            f'【特别注意：孩子正在直接向你索要答案！你必须先温柔肯定他敢于提问，明确告诉他“直接报答案会抢走你闯关成功的成就感哦”，然后绝不报出任何答案、得数或选项字母，改用一个{analogy_way}点透关键线索，并在结尾抛出一个他一眼就能答出来的小问题引导他迈出第一步！】'
            if begging_now else ''
        )
        anti_spoil = (
            f'【核心红线：学生目前正在启发闯关途中独立思考，严禁在回答中用阿拉伯数字或中文数字直接说出当前未通关步骤或本题最终得数、严禁写出等号后的计算结果、也严禁直接报出选择题答案（如选A/B/C）！{veil_extra}{beg_extra}结尾必须用一个简短的启发式小问题引导孩子自己思考！】\n'
            if is_mid_guided else ''
        )
        prompt = (
            f"原题：{sess.get('question', '')}\n"
            f"{ped_hint}\n"
            f"当前已解锁板书：\n{steps_ctx}\n\n"
            f"学生追问：「{q_ask}」\n"
            f"{anti_spoil}"
            '请用温柔易懂的口语专门点拨解答孩子的这个疑问，只输出一行JSON（不要代码块）：'
            '{"title":"点拨：...(8字内)","say":"55~85字自然口语启发点拨，符号用中文读法，结尾带启发反问","board":"关键思路线索补充(2~3行，严禁含未解锁得数)","note":"一句话点透思考方向"}'
        )
        eff_sil_ask = bool(silent_tts) if isinstance(silent_tts, bool) else bool(sess.get('silent_start'))
        if not eff_sil_ask:
            await self.pause_music_if_playing()
        content = await self._post_chat_once('aux', [{'role': 'user', 'content': prompt}], temperature=0.25, timeout_s=24.0)
        objs, _ = extract_json_objects_from_buffer(content)
        obj = objs[0] if objs else {'title': '老师点拨', 'say': content[:120], 'board': content[:160], 'note': ''}
        if is_mid_guided:
            # Include all_steps[safe_upto:] (the CURRENT unsolved checkpoint + all locked steps!) AND enable strict_no_result when streaming or begging!
            unsolved_steps = all_steps[safe_upto:] if all_steps else []
            inline_qa = scrub_locked_spoilers_from_qa({
                'title': str(obj.get('title') or '老师启发点拨')[:24],
                'say': _coerce_multiline_str(obj.get('say') or '直接说答案就少了自己破案的乐趣啦，我们先顺着图上的已知条件找找第一步线索好吗？', 280),
                'board': f"【追问点拨】{q_ask}\n{_coerce_multiline_str(obj.get('board'), 260)}",
                'note': str(obj.get('note') or '')[:46],
                'at_idx': safe_upto,
            }, unsolved_steps, formula_veiled=formula_veiled or sess_streaming, strict_no_result=sess_streaming or begging_now, question_txt=q_full_ask)
            sess['active_qa'] = inline_qa
            sess['seq'] += 1
            if not eff_sil_ask:
                self._spawn(self.synthesize_wav(inline_qa['say'], priority=False), session_work=True)
            return {'ok': True, 'inline_qa': inline_qa, 'session': self.public_session(sid)}
        next_num = len(sess['steps']) + 1
        qa_step = {
            'step': next_num,
            'is_qa': True,
            'title': str(obj.get('title') or '老师补充答疑')[:24],
            'say': _coerce_multiline_str(obj.get('say') or '我们再把这一步仔细理一遍。', 280),
            'board': f"【追问】{q_ask}\n{_coerce_multiline_str(obj.get('board'), 260)}",
            'draw': sanitize_draw_ops([{'op': 'note', 'x': 350, 'y': 402, 'label': str(obj.get('note') or q_ask)[:46]}]),
        }
        sess['steps'].append(qa_step)
        sess['seq'] += 1
        if not str(sid).startswith('m:'):
            self._save_to_history(sess)
        if not eff_sil_ask:
            self._spawn(self.synthesize_wav(qa_step['say'], priority=False), session_work=True)
        return {'ok': True, 'step_index': len(sess['steps']) - 1, 'session': self.public_session(sid)}

    async def evaluate_feynman_explanation(self, sid: str, student_text: str, pedagogy: str = 'auto', silent_tts=None) -> dict:
        sess = self._get_or_hydrate_session(sid)
        if not sess:
            raise LookupError('未找到当前微课会话')
        eff_sil_fy = bool(silent_tts) if isinstance(silent_tts, bool) else bool(sess.get('silent_start'))
        prog_f = sess.get('progress') or {}
        if not (prog_f.get('solved') or prog_f.get('peeked')):
            raise ValueError('请先独立完成本题闯关，通关后再开启费曼小讲师复盘哦！')
        st_txt = str(student_text or '').strip()[:220]
        if not st_txt:
            raise ValueError('请先开口讲讲这道题的关键思路哦')
        steps = sess.get('steps') or []
        key_board = '\n'.join(str(s.get('board') or '') for s in steps[-2:])
        q_feyn = str(sess.get('question') or '')
        subj_feyn = str(sess.get('subject') or detect_subject(q_feyn))
        is_ec_feyn = bool(sess.get('essay_critique')) or bool(detect_writing_task(q_feyn))
        eff_ped_fy = 'junior' if sess.get('source') == 'cowrite' else (pedagogy if pedagogy in ('elem', 'junior') else _norm_pedagogy(sess.get('pedagogy')))
        ped_hint = resolve_pedagogy_hint(eff_ped_fy, q_feyn)
        if is_ec_feyn:
            feyn_criterion = '请判断孩子是否讲出了本次习作构思或升格的【核心写作秘诀（如抓题眼选材、改病句原因、或用动作五感慢镜头代替流水账）】（只要讲出关键升格思路即判 pass=true；若只敷衍重复原句或只说“改好了”未提原因则判 pass=false 并温柔引导他补一句“加了什么细节让句子变生动”）。'
        elif subj_feyn in ('english', 'chinese', 'history', 'civics', 'biology', 'geography', 'chemistry', 'physics') and subj_feyn != 'math':
            feyn_criterion = '请判断孩子是否真正讲出了本题的【核心语法标志/文本证据/科学原理或因果判断依据】（只要用自己的话讲出了“为什么这样选/算/分析”的关键依据即判 pass=true；如果只报出最终得数/选项字母或只背答案词却没讲理由，又或者把原理说反了，则判 pass=false 并温柔启发他补上一句“根据什么线索推出的”）。'
        else:
            feyn_criterion = '请判断孩子是否真正讲出了本题的【核心数量关系或几何转化理由】（只要讲出了“为什么这样转化/列式”的关键桥梁即判 pass=true；如果只报出一个最终得数，或者只机械念了一遍纯数字算式却完全没解释这些数字代表什么关系，又或者把关键关系说反了，则判 pass=false 并温柔启发他补上一句“为什么要这样算”）。'
        prompt = (
            '你是一位温暖智慧的全科金牌导师。孩子刚完成本题闯关，正在用「费曼学习法」把破题关键讲给你听：\n'
            f"{ped_hint}\n"
            f"原题：{q_feyn}\n"
            f"核心步骤参考：\n{key_board}\n"
            f"孩子的口述复盘：「{st_txt}」\n"
            f"{feyn_criterion}\n"
            '只输出一行JSON（不要代码块）：{"pass":true,"badge":"金牌小讲师","say":"45~70字温暖口语点评鼓励或温柔引导补充","comment":"精炼书面评语(42字内)"}'
        )
        if not eff_sil_fy:
            await self.pause_music_if_playing()
        content = await self._post_chat_once('aux', [{'role': 'user', 'content': prompt}], temperature=0.25, timeout_s=22.0)
        objs, _ = extract_json_objects_from_buffer(content)
        obj = objs[0] if objs else {'pass': False, 'badge': '💡 再试着讲一次', 'say': '刚才网络有点波动没听完整，能把为什么这样算的关键理由再讲一遍吗？', 'comment': '请再补充一下核心判断依据哦！'}
        passed = bool(obj.get('pass'))
        feynman_res = {
            'pass': passed,
            'student_text': st_txt,
            'badge': str(obj.get('badge') or ('👑 金牌小讲师' if passed else '💡 再补一句更完美'))[:18],
            'say': str(obj.get('say') or '讲得很不错！')[:220],
            'comment': str(obj.get('comment') or obj.get('say') or '')[:90],
        }
        if passed:
            self.update_progress(sid, {'solved': True, 'feynman_ok': True} if prog_f.get('solved') else {'feynman_ok': True})
        if not eff_sil_fy:
            self._spawn(self.synthesize_wav(feynman_res['say'], priority=False), session_work=True)
        return {'ok': True, 'feynman': feynman_res, 'session': self.public_session(sid)}

    async def transcribe_voice_wav(self, wav_bytes: bytes) -> str:
        v1, api_key, model_name = self._asr_endpoint()
        if '_asr_endpoint' in getattr(self, '__dict__', {}) and v1:
            use_hub_asr = False
        else:
            cfg_asr = self._load_gateway_cfg()
            sel_asr = str(((cfg_asr.get('selected_module') or {}).get('ASR')) or '').strip() if isinstance(cfg_asr, dict) else ''
            use_hub_asr = (not v1) or (sel_asr not in ('', 'MacASR'))
        if use_hub_asr:
            pcm_bytes = wav_bytes
            rate = 16000
            if wav_bytes[:4] == b'RIFF':
                try:
                    import wave as _wave
                    with _wave.open(io.BytesIO(wav_bytes), 'rb') as wf:
                        rate = wf.getframerate() or 16000
                        ch = wf.getnchannels() or 1
                        samp = wf.getsampwidth() or 2
                        pcm_raw = wf.readframes(wf.getnframes())
                    if ch > 1:
                        pcm_raw = b''.join(pcm_raw[i:i + samp] for i in range(0, len(pcm_raw), samp * ch))
                    pcm_bytes = pcm_raw
                except Exception:
                    pcm_bytes = wav_bytes[44:] if len(wav_bytes) > 44 else wav_bytes
            try:
                async with self.http.post(
                    'http://127.0.0.1:16688/kiosk/asr',
                    data=pcm_bytes,
                    headers={'Content-Type': f'audio/L16; rate={rate}', 'Host': '127.0.0.1:16688'},
                    timeout=aiohttp.ClientTimeout(total=10, sock_connect=2.5),
                ) as resp:
                    if resp.status == 200:
                        data = json.loads((await _read_resp_bounded(resp, 256 * 1024)).decode('utf-8', errors='replace'))
                        return str(data.get('text') or '').strip()
                    if not v1:
                        raise RuntimeError('未配置语音识别服务')
                    raise RuntimeError(f'语音识别返回 HTTP {resp.status}')
            except asyncio.TimeoutError:
                raise RuntimeError('语音识别请求超时，请稍后再试')
            except RuntimeError:
                raise
            except (aiohttp.ClientError, OSError, ValueError):
                if not v1:
                    raise RuntimeError('未配置语音识别服务')
                raise RuntimeError('语音识别网络异常，请稍后再试')
        form = aiohttp.FormData()
        form.add_field('model', model_name)
        form.add_field('file', wav_bytes, filename='ask.wav', content_type='audio/wav')
        try:
            async with self.http.post(
                v1 + '/audio/transcriptions',
                data=form,
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=aiohttp.ClientTimeout(total=10, sock_connect=2.5),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f'语音识别返回 HTTP {resp.status}')
                data = json.loads((await _read_resp_bounded(resp, 256 * 1024)).decode('utf-8', errors='replace'))
        except asyncio.TimeoutError:
            raise RuntimeError('语音识别请求超时，请稍后再试')
        except (aiohttp.ClientError, OSError, ValueError):
            raise RuntimeError('语音识别网络异常，请稍后再试')
        return str(data.get('text') or '').strip()

    def public_session(self, sid: str) -> dict:
        def _ensure_r5_steps(steps_in):
            for st in steps_in or []:
                if isinstance(st.get('board'), (list, tuple)) or (isinstance(st.get('board'), str) and st['board'].lstrip().startswith('[')):
                    st['board'] = _coerce_multiline_str(st.get('board'), 380, strip_note_prefix=True)
                qz = st.get('quiz')
                if isinstance(qz, dict) and 'mode' not in qz:
                    st['quiz'] = sanitize_quiz(dict(qz, no_shuffle=True), int(st.get('step') or 1), sid)
            return steps_in

        self._ensure_history_loaded()
        if str(sid or '').startswith('m:'):
            self._ensure_mistakes_loaded()
        sess = self.sessions.get(sid)
        if not sess:
            hist_hit = next((h for h in self.history if h.get('id') == sid), None)
            if not hist_hit and str(sid or '').startswith('m:'):
                self._ensure_mistakes_loaded()
                m_hit = next((m for m in self.mistakes if f"m:{m['id']}" == sid), None)
                if m_hit:
                    _ensure_r5_steps(m_hit.get('steps'))
                    m_q = m_hit.get('question') or ''
                    m_tm = _norm_teach_mode(m_hit.get('teach_mode'))
                    m_ec = bool(m_hit.get('essay_critique')) or is_essay_critique_request(m_q, m_tm)
                    m_steps_cp = copy.deepcopy(m_hit.get('steps') or [])
                    is_cw_mb = str(m_hit.get('error_tag') or '').startswith('伴写·')
                    cw_en_mb = self._is_cowrite_silent_active()
                    eff_m_subj = _resolve_saved_subject(m_q, m_hit.get('subject') or '', teach_mode=m_tm, essay_critique=m_ec)
                    return {
                        'id': sid,
                        'title': m_hit.get('title') or '错题回看',
                        'question': m_q,
                        'subject': eff_m_subj,
                        'writing_task': detect_writing_task(m_q),
                        'polya_candidate': detect_polya_reduction_candidate(m_q),
                        'geo_model': detect_geometry_model(m_q),
                        'calc_microscope': detect_calc_microscope(m_q),
                        'reading_traps': detect_reading_traps(m_q),
                        'motion_sim': detect_motion_sim(m_q),
                        'clever_calc': None,
                        'solo_challenge': False,
                        'archetype_map': None,
                        'read_aloud_target': None,
                        'sprint_meta': None,
                        'subq_meta': None,
                        'mode': m_hit.get('mode') or 'fast',
                        'source': 'cowrite' if is_cw_mb else 'mistake_replay',
                        'teach_mode': m_tm,
                        'essay_critique': m_ec,
                        'pedagogy': _norm_pedagogy(m_hit.get('pedagogy')),
                        'mistake_id': '',
                        'alt_method': None,
                        'exam_rubric': None,
                        'parent_nudge': self.get_fresh_parent_nudge(sid),
                        'image_url': m_hit.get('image_url') or '',
                        'stage_page_id': '',
                        'status': 'ready',
                        'error': '',
                        'created_at': m_hit.get('created_at') or 0,
                        'metrics': {},
                        'progress': {'weak_step': int(m_hit.get('weak_step') or 0)},
                        'steps': m_steps_cp,
                        'active_qa': None,
                        'seq': 1,
                        'silent_start': bool(is_cw_mb or cw_en_mb),
                        'silent_preview': False,
                        'preview_step': 0,
                    }
            if not hist_hit:
                return None
            _ensure_r5_steps(hist_hit.get('steps'))
            h_prog = hist_hit.get('progress') or {}
            h_unlocked = bool(h_prog.get('solved') or h_prog.get('peeked'))
            h_q = hist_hit.get('question') or ''
            h_tm = _norm_teach_mode(hist_hit.get('teach_mode'))
            h_ec = bool(hist_hit.get('essay_critique')) or is_essay_critique_request(h_q, h_tm)
            eff_h_subj = _resolve_saved_subject(h_q, hist_hit.get('subject') or '', teach_mode=h_tm, essay_critique=h_ec)
            return dict(
                hist_hit,
                subject=eff_h_subj,
                writing_task=detect_writing_task(h_q),
                essay_critique=h_ec,
                polya_candidate=detect_polya_reduction_candidate(h_q),
                geo_model=detect_geometry_model(h_q),
                calc_microscope=detect_calc_microscope(h_q),
                reading_traps=detect_reading_traps(h_q),
                motion_sim=detect_motion_sim(h_q),
                clever_calc=detect_clever_calc_tip('\n'.join(str((s or {}).get('board') or '') for s in (hist_hit.get('steps') or []))) if h_unlocked else None,
                sprint_meta=hist_hit.get('sprint_meta'),
                subq_meta=hist_hit.get('subq_meta'),
                teach_mode=h_tm,
                pedagogy=_norm_pedagogy(hist_hit.get('pedagogy')),
                mistake_id=hist_hit.get('mistake_id') or '',
                solo_challenge=bool(hist_hit.get('solo_challenge')) and not h_unlocked,
                alt_method=hist_hit.get('alt_method') if h_unlocked else None,
                exam_rubric=hist_hit.get('exam_rubric') if h_unlocked else None,
                archetype_map=hist_hit.get('archetype_map') if h_unlocked else None,
                read_aloud_target=extract_read_aloud_target(h_q, hist_hit.get('steps') or []) if h_unlocked else None,
                parent_nudge=self.get_fresh_parent_nudge(hist_hit.get('id') or ''),
                status='ready',
                error='',
                seq=1,
                silent_start=bool(hist_hit.get('silent_start')),
                cowrite_item_id=str(hist_hit.get('cowrite_item_id') or ''),
                silent_preview=False,
                preview_step=0,
                active_qa=None,
                progress=h_prog,
            )
        _ensure_r5_steps(sess.get('steps'))
        s_prog = sess.get('progress') or {}
        s_unlocked = bool(s_prog.get('solved') or s_prog.get('peeked'))
        eff_s_act = _resolve_saved_subject(
            sess.get('question') or '',
            sess.get('subject') or '',
            teach_mode=_norm_teach_mode(sess.get('teach_mode')),
            essay_critique=bool(sess.get('essay_critique')),
        )
        return {
            'id': sess['id'],
            'title': sess['title'],
            'question': sess['question'],
            'subject': eff_s_act,
            'writing_task': detect_writing_task(sess.get('question') or ''),
            'polya_candidate': detect_polya_reduction_candidate(sess.get('question') or ''),
            'geo_model': detect_geometry_model(sess.get('question') or ''),
            'calc_microscope': detect_calc_microscope(sess.get('question') or ''),
            'reading_traps': detect_reading_traps(sess.get('question') or ''),
            'motion_sim': detect_motion_sim(sess.get('question') or ''),
            'clever_calc': detect_clever_calc_tip('\n'.join(str((s or {}).get('board') or '') for s in (sess.get('steps') or []))) if s_unlocked else None,
            'sprint_meta': sess.get('sprint_meta'),
            'subq_meta': sess.get('subq_meta'),
            'mode': sess['mode'],
            'source': sess['source'],
            'teach_mode': _norm_teach_mode(sess.get('teach_mode')),
            'pedagogy': _norm_pedagogy(sess.get('pedagogy')),
            'essay_critique': bool(sess.get('essay_critique')),
            'solo_challenge': bool(sess.get('solo_challenge')) and not s_unlocked,
            'mistake_id': sess.get('mistake_id') or '',
            'sample_id': sess.get('sample_id') or '',
            'alt_method': sess.get('alt_method') if s_unlocked else None,
            'exam_rubric': sess.get('exam_rubric') if s_unlocked else None,
            'archetype_map': sess.get('archetype_map') if s_unlocked else None,
            'read_aloud_target': extract_read_aloud_target(sess.get('question') or '', sess.get('steps') or []) if s_unlocked else None,
            'parent_nudge': sess.get('parent_nudge') or self.get_fresh_parent_nudge(sess['id']),
            'image_url': sess.get('image_url') or '',
            'stage_page_id': sess.get('stage_page_id') or '',
            'status': sess.get('status') or 'ready',
            'error': sess.get('error') or '',
            'created_at': sess.get('created_at') or 0,
            'metrics': sess.get('metrics') or {},
            'progress': s_prog,
            'steps': sess['steps'],
            'active_qa': sess.get('active_qa'),
            'seq': sess['seq'],
            'silent_start': bool(sess.get('silent_start')),
            'cowrite_item_id': str(sess.get('cowrite_item_id') or ''),
            'silent_preview': bool(sess.get('silent_preview')),
            'preview_step': int(sess.get('preview_step') or 0),
            'preview_chain_idx': int(sess.get('preview_chain_idx') or 0),
            'preview_formula_picker': bool(sess.get('preview_formula_picker')),
            'preview_feynman_puzzle': bool(sess.get('preview_feynman_puzzle')),
            'preview_feynman_ring': int(sess.get('preview_feynman_ring') or 0),
            'preview_hint_lv': int(sess.get('preview_hint_lv') or 0),
            'preview_canvas_tap': str(sess.get('preview_canvas_tap') or ''),
            'preview_snap_modal': str(sess.get('preview_snap_modal') or ''),
        }


# 2026-11-20 Claude：机身重力感应（BMA253，走 iio），给 AI 讲题页自动横竖用；按文件找，不写死 iio:device0
_ACCEL_CACHE = {'dir': None, 'checked': 0.0}


def _accel_dir():
    d = _ACCEL_CACHE.get('dir')
    if d is not None:
        return d
    now = time.monotonic()
    if _ACCEL_CACHE.get('checked') and now - _ACCEL_CACHE['checked'] < 30.0:
        return None
    _ACCEL_CACHE['checked'] = now
    base = Path(os.environ.get('TUTOR_IIO_DIR', '/sys/bus/iio/devices'))
    try:
        for cand in sorted(base.iterdir()):
            if (cand / 'in_accel_x_raw').exists() and (cand / 'in_accel_y_raw').exists():
                _ACCEL_CACHE['dir'] = cand
                return cand
    except OSError:
        pass
    return None


_TUTOR_ENGINE_KEY = web.AppKey('tutor_engine')


def make_app(engine: TutorEngine = None) -> web.Application:
    eng = engine or TutorEngine()
    static_cache = {}

    @web.middleware
    async def security_guard(request: web.Request, handler):
        eng.last_active_mono = time.monotonic()
        host = (request.headers.get('Host') or '').lower()
        sn = request.transport.get_extra_info('sockname') if request.transport else None
        srv_port = sn[1] if isinstance(sn, tuple) and len(sn) >= 2 else PORT
        valid_hosts = {
            f'127.0.0.1:{PORT}', f'localhost:{PORT}',
            f'127.0.0.1:{srv_port}', f'localhost:{srv_port}',
        }
        if host not in valid_hosts:
            raise web.HTTPForbidden()
        origin = request.headers.get('Origin')
        is_iframe_get = (
            origin is None
            and request.method in ('GET', 'HEAD')
            and request.headers.get('Sec-Fetch-Dest') == 'iframe'
            and request.path == '/'
        )
        if request.headers.get('Sec-Fetch-Site') == 'cross-site' and not ((origin and origin in ALLOWED_ORIGINS) or is_iframe_get):
            raise web.HTTPForbidden()
        if origin is not None and origin not in ALLOWED_ORIGINS and origin != f'http://127.0.0.1:{srv_port}':
            raise web.HTTPForbidden()
        if request.method == 'OPTIONS':
            resp = web.Response(status=204)
        else:
            base_inf = getattr(eng, '_inflight_reqs', 0)
            if not isinstance(base_inf, int):
                base_inf = 0
            with contextlib.suppress(Exception):
                eng._inflight_reqs = base_inf + 1
            try:
                resp = await handler(request)
            finally:
                cur_end = getattr(eng, '_inflight_reqs', 1)
                with contextlib.suppress(Exception):
                    eng._inflight_reqs = max(0, (cur_end if isinstance(cur_end, int) else 1) - 1)
                    eng.last_active_mono = time.monotonic()
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        if origin in ('http://127.0.0.1:8090', 'http://localhost:8090', 'http://127.0.0.1:8095', 'http://localhost:8095'):
            resp.headers['Access-Control-Allow-Origin'] = origin
            resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, If-None-Match'
            resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        if resp.status == 304:
            resp.headers['Content-Length'] = '0'
        return resp

    app = web.Application(client_max_size=8 * 1024 * 1024, middlewares=[security_guard])

    async def on_startup(_app):
        eng.http = aiohttp.ClientSession(trust_env=False)
        eng.tts_http = aiohttp.ClientSession(
            trust_env=False,
            connector=aiohttp.TCPConnector(limit=2, keepalive_timeout=3.5),
        )
        with contextlib.suppress(Exception):
            p_idx = SHARE_DIR / 'index.html'
            if p_idx.is_file():
                st_i = p_idx.stat()
                d_i = p_idx.read_bytes()
                static_cache['index'] = (st_i.st_mtime_ns, d_i, _etag_bytes(d_i))
            eng._ensure_history_loaded()
            eng._ensure_mistakes_loaded()
        if engine is None and os.environ.get('TUTOR_WARM_GOLDEN', '0') == '1':
            eng._spawn(eng._warm_golden_step1_cache(), session_work=False)

    async def on_cleanup(_app):
        cw_c = getattr(eng, 'cowrite', None)
        if isinstance(cw_c, CoWriteMonitor):
            await cw_c.stop()
        await eng.camera.stop_stream()
        await eng.recorder.cancel()
        for t in list(eng.bg_tasks):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if eng.http:
            await eng.http.close()
        if eng.tts_http:
            await eng.tts_http.close()

    async def on_shutdown(_app):
        eng._cancel_previous_session_work()
        for t in list(eng.bg_tasks):
            t.cancel()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.on_cleanup.append(on_cleanup)

    async def serve_index(request: web.Request):
        p = SHARE_DIR / 'index.html'
        st = p.stat()
        cached = static_cache.get('index')
        if not cached or cached[0] != st.st_mtime_ns:
            data = p.read_bytes()
            cached = (st.st_mtime_ns, data, _etag_bytes(data))
            static_cache['index'] = cached
        _, data, etag = cached
        if request.headers.get('If-None-Match') == etag:
            return web.Response(status=304, headers={'ETag': etag})
        return web.Response(
            body=data,
            content_type='text/html',
            charset='utf-8',
            headers={
                'ETag': etag,
                'Cache-Control': 'no-cache',
                'Content-Security-Policy': (
                    "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob: data:; "
                    "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                    "frame-ancestors http://127.0.0.1:8090 http://localhost:8090"
                ),
            },
        )

    async def _json_dict(request: web.Request, allow_empty: bool = True) -> dict:
        try:
            raw = await request.read()
        except Exception:
            raise web.HTTPBadRequest(
                text=json.dumps({'ok': False, 'error': '请求格式无效'}, ensure_ascii=False),
                content_type='application/json',
            )
        if not raw or not raw.strip():
            if allow_empty:
                return {}
            raise web.HTTPBadRequest(
                text=json.dumps({'ok': False, 'error': '请求格式无效'}, ensure_ascii=False),
                content_type='application/json',
            )
        try:
            obj = json.loads(
                raw.decode('utf-8'),
                parse_constant=lambda _c: (_ for _ in ()).throw(ValueError('non-finite')),
            )
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest(
                text=json.dumps({'ok': False, 'error': '请求格式无效'}, ensure_ascii=False),
                content_type='application/json',
            )
        if not isinstance(obj, dict):
            raise web.HTTPBadRequest(
                text=json.dumps({'ok': False, 'error': '请求格式无效'}, ensure_ascii=False),
                content_type='application/json',
            )
        return obj

    async def api_state(request: web.Request):
        cam = eng.poll_camera_hotplug()
        now_ui_m = time.monotonic()
        _uc = getattr(eng, '_ui_ver_cache', None)
        ui_ts, ui_ver = _uc if (isinstance(_uc, tuple) and len(_uc) == 2) else (0.0, '')
        if (now_ui_m - ui_ts) >= 5.0 or not ui_ver:
            ui_ver = ''
            with contextlib.suppress(OSError):
                ui_ver = str(int((SHARE_DIR / 'index.html').stat().st_mtime_ns))
            eng._ui_ver_cache = (now_ui_m, ui_ver)
        cw_obj = getattr(eng, 'cowrite', None)
        cw_is_mon = isinstance(cw_obj, CoWriteMonitor)
        if cw_is_mon:
            cw_obj._ensure_loaded()
        dc_obj = getattr(eng, 'dictation', None)
        if isinstance(dc_obj, DictationManager):
            dc_obj._ensure_loaded()
        eng._ensure_history_loaded()
        eng._ensure_mistakes_loaded()
        nudge_now = eng.get_fresh_parent_nudge()
        open_fresh = eng.is_open_ui_fresh()
        act_raw = eng.sessions.get(eng.active_session_id) if eng.active_session_id else None
        h0 = eng.history[0] if eng.history else None
        m0 = eng.mistakes[0] if eng.mistakes else None
        etag_src = '|'.join((
            ui_ver,
            f"{cam.get('connected')}:{cam.get('dev')}:{cam.get('rotate')}:{cam.get('zoom')}:{cam.get('bright_boost')}:{cam.get('frozen')}",
            str(eng.phone_studio.seq),
            f"{cw_obj.seq}:{cw_obj.enabled}:{cw_obj.phase}:{cw_obj._pipeline_lock.locked()}" if cw_is_mon else '0',
            '1' if eng.recorder.is_recording else '0',
            f"{len(eng.history)}:{(h0 or {}).get('id', '')}:{json.dumps((h0 or {}).get('progress') or {}, sort_keys=True)}",
            f"{len(eng.mistakes)}:{(m0 or {}).get('id', '')}:{max((int(m.get('updated_at') or 0) for m in eng.mistakes), default=0)}:{sum(1 for m in eng.mistakes if m.get('graduated'))}:{sum(int(m.get('retest_pass_count') or 0) for m in eng.mistakes)}",
            str((nudge_now or {}).get('seq') or 0),
            eng.active_session_id or '',
            f"{(act_raw or {}).get('seq', 0)}:{(act_raw or {}).get('status', '')}:{json.dumps((act_raw or {}).get('progress') or {}, sort_keys=True)}",
            f"{eng.open_ui_seq}:{eng.open_ui_target}:{eng.open_ui_pencil}:{'1' if open_fresh else '0'}",
            f"{dc_obj.seq}:{dc_obj._grading_lock.locked()}" if isinstance(dc_obj, DictationManager) else '0',
        )).encode('utf-8')
        etag = _etag_bytes(etag_src)
        if request.headers.get('If-None-Match') == etag:
            return web.Response(status=304, headers={'ETag': etag, 'Cache-Control': 'no-cache'})

        samples = [
            {'id': p['id'], 'cat': p['cat'], 'grade': p['grade'], 'title': p['title'], 'question': p['question']}
            for p in SAMPLE_PROBLEMS
        ]
        hist_brief = [
            {
                'id': h['id'], 'title': h['title'], 'question': h['question'],
                'mode': h.get('mode', ''), 'created_at': h.get('created_at', 0),
                'progress': h.get('progress') or {},
                'steps_count': len([s for s in (h.get('steps') or []) if s.get('step', 0) > 0]),
            }
            for h in eng.history[:15]
        ]
        act = eng.public_session(eng.active_session_id) if eng.active_session_id else None
        cw_pub = cw_obj.public_state() if cw_is_mon else {
            'enabled': False, 'phase': 'off', 'subject': 'auto', 'page_epoch': 1, 'seq': 0, 'items': [],
            'ok_count': 0, 'warn_open_count': 0, 'warn_resolved_count': 0, 'total_count': 0,
        }
        return web.json_response({
            'ok': True,
            'camera': cam,
            'phone_studio': eng.phone_studio.public_state(),
            'cowrite': cw_pub,
            'dictation': dc_obj.public_state() if isinstance(dc_obj, DictationManager) else {'phase': 'idle', 'total_count': 0, 'items': []},
            'recording': eng.recorder.is_recording,
            'samples': samples,
            'history': hist_brief,
            'mistakes': eng.public_mistakes(),
            'mistake_summary': eng.public_mistake_summary(),
            'parent_nudge': nudge_now,
            'active_session': act,
            'open_ui_seq': eng.open_ui_seq,
            'open_ui_target': eng.open_ui_target,
            'open_ui_pencil': eng.open_ui_pencil,
            'open_ui_fresh': open_fresh,
            'ui_ver': ui_ver,
        }, headers={'ETag': etag, 'Cache-Control': 'no-cache'})

    async def api_badge(request: web.Request):
        cam = eng.poll_camera_hotplug()
        if request.query.get('hotplug') == '1' and cam.get('connected') and not eng.is_lesson_in_progress() and eng.open_ui_seq == 0:
            cw_hb = getattr(eng, 'cowrite', None)
            eng.trigger_open_ui('cowrite' if (isinstance(cw_hb, CoWriteMonitor) and cw_hb.enabled) else 'camera')
        return web.json_response({
            'ok': True,
            'open_ui_seq': eng.open_ui_seq,
            'open_ui_fresh': eng.is_open_ui_fresh(),
            'active_session_id': eng.active_session_id,
            'camera_connected': bool(cam.get('connected')),
        })

    async def api_cam_frame(request: web.Request):
        try:
            if request.query.get('raw') == '1':
                after_s = -1
                with contextlib.suppress(ValueError, TypeError):
                    after_s = int(request.query.get('after', '-1'))
                jpg, seq, dim = await eng.camera.capture_preview_raw(after_seq=after_s, wait_new=(request.query.get('wait') == '1'))
                if jpg is None:
                    return web.Response(status=204, headers={'Cache-Control': 'no-store', 'X-Cam-Seq': str(seq)})
                hdrs = {'Cache-Control': 'no-store', 'X-Cam-Seq': str(seq)}
                if dim:
                    hdrs['X-Cam-Size'] = dim
                return web.Response(body=jpg, content_type='image/jpeg', headers=hdrs)
            jpg = await eng.camera.capture_jpeg(high_res=False)
            return web.Response(body=jpg, content_type='image/jpeg', headers={'Cache-Control': 'no-store'})
        except LookupError as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)}, status=404)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)}, status=500)

    async def api_cam_rotate(request: web.Request):
        body = await _json_dict(request)
        deg = body.get('rotate') if 'rotate' in body else None
        rot = eng.camera.set_rotate(deg)
        if isinstance(getattr(eng, 'cowrite', None), CoWriteMonitor):
            eng.cowrite.rotate_hint = 0
            eng.cowrite.prev_thumb = b''
            eng.cowrite.anchor_thumb = b''
            eng.cowrite.seq += 1
        return web.json_response({'ok': True, 'rotate': rot, 'camera': eng.camera.discover()})

    async def api_cam_refocus(_request: web.Request):
        try:
            if eng.camera._frozen_raw_jpg is not None:
                await eng.camera.set_freeze(False)
            cam = await eng.camera.refocus()
            return web.json_response({'ok': True, 'camera': cam})
        except LookupError as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)}, status=404)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:120]}, status=500)

    async def api_cam_ctrl(request: web.Request):
        body = await _json_dict(request)
        try:
            if 'zoom' in body:
                eng.camera.set_zoom(None if body['zoom'] == 'cycle' else body['zoom'])
                if isinstance(getattr(eng, 'cowrite', None), CoWriteMonitor):
                    eng.cowrite.prev_thumb = b''
                    eng.cowrite.anchor_thumb = b''
            if 'bright_boost' in body:
                await eng.camera.set_bright_boost(None if body['bright_boost'] == 'toggle' else bool(body['bright_boost']))
                if isinstance(getattr(eng, 'cowrite', None), CoWriteMonitor) and eng.camera.bright_boost:
                    eng.cowrite.lighting_warn = ''
                    eng.cowrite.seq += 1
            if 'freeze' in body:
                await eng.camera.set_freeze(None if body['freeze'] == 'toggle' else bool(body['freeze']))
            return web.json_response({'ok': True, 'camera': eng.camera.discover()})
        except LookupError as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)}, status=404)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:120]}, status=400)

    async def api_cam_stage_page(request: web.Request):
        body = await _json_dict(request)
        p_boost = bool(body.get('pencil_boost'))
        do_check = bool(body.get('check_page'))
        note_q = str(body.get('note') or '').strip()[:120]
        c_box = body.get('crop') if isinstance(body.get('crop'), dict) else None
        try:
            full_jpg = await eng.camera.capture_jpeg(high_res=True, crop_box=None, pencil_boost=False)
            if eng.camera._frozen_raw_jpg is not None:
                await eng.camera.set_freeze(False)
            pid = eng.phone_studio.save_page(full_jpg, note=note_q, crop_box=c_box)
            if do_check:
                chk = await eng.check_staged_worksheet_page(page_id=pid, pencil_boost=p_boost)
                return web.json_response(dict(chk, page_id=pid))
            return web.json_response({
                'ok': True,
                'page_id': pid,
                'phone_studio': eng.phone_studio.public_state(),
            })
        except LookupError as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)}, status=404)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=500)

    async def api_snap_image(request: web.Request):
        name = request.match_info.get('name', '')
        if not re.fullmatch(r'[0-9a-f_]{8,32}\.jpg', name):
            raise web.HTTPBadRequest()
        p = SNAP_DIR / name
        try:
            _, raw_jpg = _read_regular_bytes(p, 8 * 1024 * 1024)
        except OSError:
            raise web.HTTPNotFound()
        return web.Response(body=raw_jpg, content_type='image/jpeg', headers={'Cache-Control': 'private, max-age=3600'})

    async def api_start(request: web.Request):
        body = await _json_dict(request, allow_empty=False)
        sample_id = str(body.get('sample_id') or '').strip()
        live = bool(body.get('live'))
        mode = _norm_mode(body.get('mode'))
        ped = _norm_pedagogy(body.get('pedagogy'))
        t_mode = _norm_teach_mode(body.get('teach_mode'))
        open_ui = bool(body.get('open_ui'))
        solo_ch = bool(body.get('solo_challenge'))
        try:
            if sample_id and not live:
                sess = eng.start_golden_sample(sample_id, open_ui=open_ui, solo_challenge=solo_ch)
                return web.json_response({'ok': True, 'session': sess})
            if sample_id and live:
                sp = SAMPLE_BY_ID.get(sample_id)
                if not sp:
                    return web.json_response({'ok': False, 'error': '未找到样题'}, status=404)
                sess = eng.start_live_problem(
                    question=sp['question'], mode=mode, title=sp['title'], source='sample_live', open_ui=open_ui, pedagogy=ped, solo_challenge=solo_ch,
                )
                return web.json_response({'ok': True, 'session': sess})
            if body.get('use_camera'):
                crop_box = body.get('crop') if isinstance(body.get('crop'), dict) else None
                p_boost_cam = bool(body.get('pencil_boost'))
                jpg = await eng.camera.capture_jpeg(high_res=True, crop_box=crop_box, pencil_boost=p_boost_cam)
                if eng.camera._frozen_raw_jpg is not None:
                    await eng.camera.set_freeze(False)
                sess = eng.start_live_problem(
                    question=str(body.get('question') or '').strip(),
                    image_bytes=jpg, mode=mode, title='草稿错因诊断微课' if t_mode == 'diagnose_draft' else '高拍仪拍题微课', source='doccam', open_ui=open_ui, pedagogy=ped, teach_mode=t_mode, solo_challenge=solo_ch, silent_tts=bool(body.get('silent')),
                )
                return web.json_response({'ok': True, 'session': sess})
            if body.get('use_phone_stage'):
                req_pid = str(body.get('page_id') or '').strip()
                if req_pid:
                    eng.phone_studio.select_page(req_pid)
                if not eng.phone_studio.has_page():
                    return web.json_response({'ok': False, 'error': '请先用手机扫码上传整页作业图'}, status=400)
                crop_box = body.get('crop') if isinstance(body.get('crop'), dict) else None
                p_boost = bool(body.get('pencil_boost'))
                if crop_box:
                    eng.phone_studio.update_active_crop(crop_box)
                _, raw_page = _read_regular_bytes(eng.phone_studio.stage_path, 12 * 1024 * 1024)
                jpg = await eng.phone_studio.enhance_jpeg(
                    raw_page, crop_box=crop_box, rotate_deg=eng.phone_studio.rotate, crisp=True, pencil_boost=p_boost,
                )
                note_q = str(body.get('question') or '').strip()
                sess = eng.start_live_problem(
                    question=note_q,
                    image_bytes=jpg, mode=mode, title='草稿错因诊断微课' if t_mode == 'diagnose_draft' else '手机拍题微课', source='phone_crop', open_ui=open_ui, pedagogy=ped, teach_mode=t_mode, solo_challenge=solo_ch, silent_tts=bool(body.get('silent')),
                )
                if sess and sess['id'] in eng.sessions:
                    act_pid = eng.phone_studio.active_id
                    eng.sessions[sess['id']]['stage_page_id'] = act_pid
                    sess['stage_page_id'] = act_pid
                    if crop_box:
                        eng.phone_studio.record_done_crop(act_pid, sess['id'], crop_box)
                return web.json_response({'ok': True, 'session': sess})
            img_b64 = str(body.get('image_b64') or '').strip()
            retry_snap = str(body.get('retry_snap') or '').strip()
            img_bytes = None
            if img_b64:
                if ',' in img_b64[:64]:
                    img_b64 = img_b64.split(',', 1)[1]
                img_bytes = base64.b64decode(img_b64)
                if len(img_bytes) < 128 or len(img_bytes) > 6 * 1024 * 1024:
                    return web.json_response({'ok': False, 'error': '图片大小不合法'}, status=400)
                try:
                    img_bytes = await eng.phone_studio.normalize_to_jpeg(img_bytes)
                except (ValueError, RuntimeError):
                    return web.json_response({'ok': False, 'error': '暂不支持该图片格式，请重新拍照或截图上传'}, status=400)
            elif retry_snap:
                m_snap = re.fullmatch(r'/api/tutor/snap/([0-9a-f_]{8,32}\.jpg)', retry_snap)
                if not m_snap:
                    return web.json_response({'ok': False, 'error': '原题截图路径不合法'}, status=400)
                try:
                    _, img_bytes = _read_regular_bytes(SNAP_DIR / m_snap.group(1), 8 * 1024 * 1024)
                except OSError:
                    return web.json_response({'ok': False, 'error': '原题截图已过期，请回高拍仪重新拍题'}, status=404)
            mid_st = str(body.get('mistake_id') or '').strip()[:40]
            sp_meta_in = body.get('sprint_meta')
            sp_meta_clean = None
            if isinstance(sp_meta_in, dict) and str(sp_meta_in.get('sprint_id') or '').strip():
                with contextlib.suppress(TypeError, ValueError):
                    sp_meta_clean = {
                        'sprint_id': str(sp_meta_in['sprint_id']).strip()[:40],
                        'idx': max(1, min(10, int(sp_meta_in.get('idx') or 1))),
                        'total': max(1, min(10, int(sp_meta_in.get('total') or 1))),
                    }
            sess = eng.start_live_problem(
                question=str(body.get('question') or ''),
                image_bytes=img_bytes, mode=mode,
                title=str(body.get('title') or ''),
                source=str(body.get('source') or 'custom'),
                open_ui=open_ui, pedagogy=ped, teach_mode=t_mode,
                mistake_id=mid_st,
                solo_challenge=solo_ch,
                sprint_meta=sp_meta_clean,
                silent_tts=bool(body.get('silent')),
                cowrite_item_id=str(body.get('cowrite_item_id') or '').strip(),
            )
            if sp_meta_clean and sp_meta_clean['sprint_id'] in eng.sprints:
                eng.sprints[sp_meta_clean['sprint_id']]['current_sid'] = sess['id']
            return web.json_response({'ok': True, 'session': sess})
        except (ValueError, LookupError) as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)}, status=400)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:180]}, status=500)

    async def api_variant(request: web.Request):
        body = await _json_dict(request, allow_empty=False)
        sid = str(body.get('id') or eng.active_session_id).strip()
        mode = _norm_mode(body.get('mode'))
        diff = str(body.get('difficulty') or 'same').strip().lower()
        ped = _norm_pedagogy(body.get('pedagogy'))
        w_step = 0
        with contextlib.suppress(TypeError, ValueError):
            w_step = max(0, min(6, int(body.get('weak_step') or 0)))
        w_focus = str(body.get('weak_focus') or '').strip()
        solo_ch = bool(body.get('solo_challenge'))
        sil_opt = body.get('silent') if isinstance(body.get('silent'), bool) else None
        try:
            res = await eng.generate_variant_problem(sid, mode=mode, difficulty=diff, pedagogy=ped, weak_step=w_step, weak_focus=w_focus, solo_challenge=solo_ch, silent_tts=sil_opt)
            return web.json_response(res)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)

    async def api_history_delete(request: web.Request):
        body = await _json_dict(request, allow_empty=False)
        res = eng.delete_history(sid=str(body.get('id') or ''), clear_all=bool(body.get('clear_all')))
        return web.json_response(res)

    async def api_voice_record(request: web.Request):
        body = await _json_dict(request, allow_empty=False)
        act = str(body.get('action') or '').strip().lower()
        try:
            if act == 'start':
                await eng.pause_music_if_playing()
                await eng.recorder.start()
                return web.json_response({'ok': True, 'recording': True})
            if act == 'cancel':
                await eng.recorder.cancel()
                return web.json_response({'ok': True, 'recording': False})
            if act == 'stop':
                wav_bytes, dur = await eng.recorder.stop_and_get_wav()
                text = await eng.transcribe_voice_wav(wav_bytes)
                if not text:
                    return web.json_response({'ok': False, 'error': '没听清楚内容，请靠近屏幕再说一次'}, status=400)
                return web.json_response({'ok': True, 'text': text, 'duration': dur})
            return web.json_response({'ok': False, 'error': '不支持的录音指令'}, status=400)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)

    async def api_phone_upload(request: web.Request):
        if request.headers.get('X-TS-Internal') != '1' or request.headers.get('Origin') is not None:
            raise web.HTTPForbidden()
        eng.phone_studio.mark_mobile_seen(request.headers.get('X-Tutor-Entry-Base') or '')
        body = await _json_dict(request, allow_empty=False)
        img_b64 = str(body.get('image_b64') or '').strip()
        question = str(body.get('question') or '').strip()
        mode = _norm_mode(body.get('mode'))
        ped = _norm_pedagogy(body.get('pedagogy'))
        img_bytes = None
        if img_b64:
            if ',' in img_b64[:64]:
                img_b64 = img_b64.split(',', 1)[1]
            try:
                img_bytes = base64.b64decode(img_b64)
            except Exception:
                return web.json_response({'ok': False, 'error': '图片编码无效'}, status=400)
            if len(img_bytes) < 128 or len(img_bytes) > 6 * 1024 * 1024:
                return web.json_response({'ok': False, 'error': '图片大小不合法'}, status=400)
            try:
                img_bytes = await eng.phone_studio.normalize_to_jpeg(img_bytes)
            except (ValueError, RuntimeError):
                return web.json_response({'ok': False, 'error': '暂不支持该图片格式，请重新拍照或截图上传'}, status=400)
            with contextlib.suppress(Exception):
                eng.phone_studio.save_page(img_bytes, note=question)
                img_bytes = await eng.phone_studio.enhance_jpeg(img_bytes, crisp=True)
        try:
            t_mode = _norm_teach_mode(body.get('teach_mode'))
            sess = eng.start_live_problem(
                question=question, image_bytes=img_bytes, mode=mode,
                title='草稿错因诊断微课' if t_mode == 'diagnose_draft' else '手机传题微课', source='phone', open_ui=True, pedagogy=ped, teach_mode=t_mode,
            )
            if img_bytes and sess and sess['id'] in eng.sessions:
                eng.sessions[sess['id']]['stage_page_id'] = eng.phone_studio.active_id
                sess['stage_page_id'] = eng.phone_studio.active_id
            eng.open_ui_target = 'board'
            return web.json_response({'ok': True, 'session': sess, 'message': '已投屏到家里大屏开始 AI 启发式黑板讲题！'})
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)

    async def api_phone_qr_svg(_request: web.Request):
        svg = await eng.phone_studio.get_qr_svg()
        return web.Response(body=svg, content_type='image/svg+xml', headers={'Cache-Control': 'no-store'})

    async def api_phone_stage_page(request: web.Request):
        try:
            pid = str(request.query.get('id') or '').strip()
            jpg = await eng.phone_studio.get_rotated_stage_jpeg(pid)
            return web.Response(body=jpg, content_type='image/jpeg', headers={'Cache-Control': 'no-store'})
        except LookupError as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)}, status=404)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:120]}, status=500)

    async def api_phone_stage_rotate(request: web.Request):
        body = await _json_dict(request)
        pid_rot = str(body.get('page_id') or body.get('id') or '').strip()
        if pid_rot and not eng.phone_studio.select_page(pid_rot):
            return web.json_response({'ok': False, 'error': '未找到该作业页'}, status=404)
        deg = body.get('rotate') if 'rotate' in body else None
        if deg is None and body.get('step_deg') is not None:
            with contextlib.suppress(TypeError, ValueError):
                deg = ((eng.phone_studio.rotate or 0) + int(body['step_deg'])) % 360
        rot = eng.phone_studio.set_rotate(deg)
        if isinstance(getattr(eng, 'cowrite', None), CoWriteMonitor):
            eng.cowrite.rotate_hint = 0
            eng.cowrite.seq += 1
        return web.json_response({'ok': True, 'rotate': rot, 'phone_studio': eng.phone_studio.public_state()})

    async def api_phone_stage_select(request: web.Request):
        body = await _json_dict(request)
        pid = str(body.get('id') or '').strip()
        if not eng.phone_studio.select_page(pid):
            return web.json_response({'ok': False, 'error': '未找到该作业页'}, status=404)
        return web.json_response({'ok': True, 'phone_studio': eng.phone_studio.public_state()})

    async def api_phone_stage_clear(request: web.Request):
        body = await _json_dict(request)
        pid = str(body.get('id') or '').strip()
        clear_all = bool(body.get('clear_all'))
        eng.phone_studio.clear_page(page_id=pid, clear_all=clear_all or not pid)
        return web.json_response({'ok': True, 'phone_studio': eng.phone_studio.public_state()})

    async def api_phone_stage_check_page(request: web.Request):
        body = await _json_dict(request)
        pid = str(body.get('id') or '').strip()
        p_boost = bool(body.get('pencil_boost'))
        try:
            res = await eng.check_staged_worksheet_page(page_id=pid, pencil_boost=p_boost)
            return web.json_response(res)
        except LookupError as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)}, status=400)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=500)

    async def api_mobile_asset(request: web.Request):
        if request.headers.get('X-TS-Internal') != '1' or request.headers.get('Origin') is not None:
            raise web.HTTPForbidden()
        eng.phone_studio.mark_mobile_seen(request.headers.get('X-Tutor-Entry-Base') or '')
        name = request.match_info.get('name', '')
        mime_map = {
            'mobile.html': 'text/html',
            'manifest.json': 'application/manifest+json',
            'favicon.svg': 'image/svg+xml',
            'apple-touch-icon.png': 'image/png',
        }
        if name not in mime_map:
            raise web.HTTPNotFound()
        p = SHARE_DIR / name
        if not p.exists() or p.is_symlink():
            raise web.HTTPNotFound()
        st = p.stat()
        ckey = f'mob:{name}'
        cached = static_cache.get(ckey)
        if not cached or cached[0] != st.st_mtime_ns:
            data = p.read_bytes()
            cached = (st.st_mtime_ns, data, _etag_bytes(data))
            static_cache[ckey] = cached
        _, data, etag = cached
        if request.headers.get('If-None-Match') == etag:
            return web.Response(status=304, headers={'ETag': etag})
        hdrs = {'ETag': etag, 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer'}
        if name == 'mobile.html':
            hdrs['Cache-Control'] = 'no-cache'
            hdrs['Content-Security-Policy'] = (
                "default-src 'self'; img-src 'self' data: blob:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
            )
            return web.Response(body=data, content_type='text/html', charset='utf-8', headers=hdrs)
        hdrs['Cache-Control'] = 'max-age=86400'
        return web.Response(body=data, content_type=mime_map[name], headers=hdrs)

    async def api_mobile_state(request: web.Request):
        if request.headers.get('X-TS-Internal') != '1' or request.headers.get('Origin') is not None:
            raise web.HTTPForbidden()
        eng.phone_studio.mark_mobile_seen(request.headers.get('X-Tutor-Entry-Base') or '')
        act_pub = eng.public_session(eng.active_session_id) if eng.active_session_id else None
        act_brief = None
        if act_pub:
            real_steps = [s for s in (act_pub.get('steps') or []) if int(s.get('step') or 0) > 0]
            prog = act_pub.get('progress') or {}
            st_cur = act_pub.get('status') or 'ready'
            s_tot = max(4, len(real_steps)) if st_cur in ('thinking', 'streaming') else (len(real_steps) or 4)
            c_step = min(max(s_tot, 4), max(1, int(prog.get('current_step') or 1)))
            act_brief = {
                'id': act_pub['id'],
                'title': act_pub['title'],
                'question': act_pub.get('question') or '',
                'status': st_cur,
                'error': str(act_pub.get('error') or '')[:80],
                'source': act_pub.get('source') or '',
                'step_total': s_tot,
                'current_step': c_step,
                'solved': bool(prog.get('solved')),
                'peeked': bool(prog.get('peeked')),
                'feynman_ok': bool(prog.get('feynman_ok')),
                'solo_ace': bool(prog.get('solo_ace')),
                'solo_sec': int(prog.get('solo_sec') or 0),
                'read_aloud_ok': bool(prog.get('read_aloud_ok')),
                'weak_step': int(prog.get('weak_step') or 0),
                'step_attempts': int(prog.get('step_attempts') or 0),
                'step_title': str(prog.get('step_title') or ''),
                'teach_mode': _norm_teach_mode(act_pub.get('teach_mode')),
                'essay_critique': bool(act_pub.get('essay_critique')),
                'writing_task': str(act_pub.get('writing_task') or ''),
                'subject': str(act_pub.get('subject') or 'math'),
                'silent_start': bool(act_pub.get('silent_start')),
            }
        ps_pub = eng.phone_studio.public_state()
        cw_m_obj = getattr(eng, 'cowrite', None)
        cw_mob = cw_m_obj.mobile_brief() if isinstance(cw_m_obj, CoWriteMonitor) else {
            'enabled': False, 'phase': 'off', 'total_count': 0, 'ok_count': 0, 'warn_open_count': 0, 'recent_items': [],
        }
        all_pub_mbs = eng.public_mistakes()
        cw_pub_mbs = [m for m in all_pub_mbs if not m.get('graduated') and str(m.get('error_tag') or '').startswith('伴写·')][:6]
        cw_mb_ids = {m['id'] for m in cw_pub_mbs}
        mob_mbs = (cw_pub_mbs + [m for m in all_pub_mbs if m.get('id') not in cw_mb_ids])[:18]
        raw_b = json.dumps({
            'ok': True,
            'mistake_summary': eng.public_mistake_summary(),
            'mistakes': mob_mbs,
            'cowrite': cw_mob,
            'dictation': eng.dictation.mobile_brief() if isinstance(getattr(eng, 'dictation', None), DictationManager) else {'phase': 'idle', 'total_count': 0},
            'has_staged_page': ps_pub['has_page'],
            'staged_seq': ps_pub['seq'],
            'staged_at': ps_pub['uploaded_at'],
            'page_count': ps_pub['page_count'],
            'active_page_id': ps_pub['active_id'],
            'pages': [
                {
                    'id': pg['id'],
                    'idx': pg['idx'],
                    'uploaded_at': pg['uploaded_at'],
                    'note': pg['note'],
                    'last_crop': pg['last_crop'],
                    'done_count': pg['done_count'],
                    'done_crops': pg['done_crops'],
                    'check_marks': pg.get('check_marks') or [],
                    'check_summary': pg.get('check_summary') or '',
                    'check_error': pg.get('check_error') or '',
                    'check_seq': int(pg.get('check_seq') or 0),
                    'active': pg['active'],
                }
                for pg in ps_pub['pages']
            ],
            'active_lesson': act_brief,
        }, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        et = _etag_bytes(raw_b)
        hdrs = {'ETag': et, 'Cache-Control': 'no-store'}
        if request.headers.get('If-None-Match') == et:
            return web.Response(status=304, headers=hdrs)
        return web.Response(body=raw_b, content_type='application/json', headers=hdrs)

    async def api_mobile_page_jpg(request: web.Request):
        if request.headers.get('X-TS-Internal') != '1' or request.headers.get('Origin') is not None:
            raise web.HTTPForbidden()
        eng.phone_studio.mark_mobile_seen(request.headers.get('X-Tutor-Entry-Base') or '')
        if not eng.phone_studio.has_page():
            raise web.HTTPNotFound()
        pid = str(request.query.get('id') or '').strip()
        try:
            data = await eng.phone_studio.get_rotated_stage_jpeg(pid)
        except LookupError:
            raise web.HTTPNotFound()
        return web.Response(body=data, content_type='image/jpeg', headers={'Cache-Control': 'no-store'})

    async def api_mobile_upload(request: web.Request):
        if request.headers.get('X-TS-Internal') != '1' or request.headers.get('Origin') is not None:
            raise web.HTTPForbidden()
        eng.phone_studio.mark_mobile_seen(request.headers.get('X-Tutor-Entry-Base') or '')
        body = await _json_dict(request, allow_empty=False)
        act = str(body.get('action') or 'explain_now').strip().lower()
        question = str(body.get('question') or '').strip()
        mode = _norm_mode(body.get('mode'))
        ped = _norm_pedagogy(body.get('pedagogy'))
        crisp = bool(body.get('crisp', True))
        pencil_boost = bool(body.get('pencil_boost'))
        crop_box = body.get('crop_box') if isinstance(body.get('crop_box'), dict) else None
        client_key = str(body.get('client_page_id') or '').strip()[:32]
        req_page_id = str(body.get('page_id') or '').strip()

        def _dec_b64(val):
            s = str(val or '').strip()
            if not s:
                return None
            if ',' in s[:64]:
                s = s.split(',', 1)[1]
            raw = base64.b64decode(s)
            if len(raw) < 128 or len(raw) > 5 * 1024 * 1024:
                raise ValueError('图片大小不合法')
            return raw

        try:
            if act == 'clear_stage':
                eng.phone_studio.clear_page(page_id=req_page_id, clear_all=not req_page_id)
                return web.json_response({
                    'ok': True,
                    'opened_phone_view': False,
                    'message': '已移除该页作业' if req_page_id else '已清空大屏暂存的全部作业页',
                })
            if act == 'select_page':
                if not eng.phone_studio.select_page(req_page_id):
                    return web.json_response({'ok': False, 'error': '未找到该作业页'}, status=404)
                return web.json_response({'ok': True, 'opened_phone_view': False, 'message': '已切换大屏当前作业页'})
            if act == 'open_phone_view':
                if req_page_id:
                    eng.phone_studio.select_page(req_page_id)
                eng.trigger_open_ui('phone')
                if not eng.phone_studio.has_page():
                    return web.json_response({'ok': True, 'opened_phone_view': True, 'message': '已在大屏打开「手机拍题」扫码引导页'})
                return web.json_response({'ok': True, 'opened_phone_view': True, 'message': '家里大屏已切换到整页作业框选台！'})
            if act == 'prepare_print_sheet':
                res_ps = await eng.prepare_print_sheet(
                    ids=body.get('ids') if isinstance(body.get('ids'), list) else None,
                    sheet_mode=str(body.get('sheet_mode') or 'original'),
                    pedagogy=ped,
                    from_cowrite=bool(body.get('from_cowrite')),
                )
                return web.json_response({**res_ps, 'opened_phone_view': False})
            if act == 'cowrite_enroll_warns':
                cw_obj_e = getattr(eng, 'cowrite', None)
                if not isinstance(cw_obj_e, CoWriteMonitor):
                    return web.json_response({'ok': False, 'error': '伴写模块未就绪'}, status=400)
                res_ce = cw_obj_e.enroll_mistake(enroll_all_warns=True)
                cnt_ce = int(res_ce.get('enrolled_count') or 0)
                all_m_ce = eng.public_mistakes()
                cw_m_ce = [m for m in all_m_ce if not m.get('graduated') and str(m.get('error_tag') or '').startswith('伴写·')][:6]
                cw_ids_ce = {m['id'] for m in cw_m_ce}
                mob_m_ce = (cw_m_ce + [m for m in all_m_ce if m.get('id') not in cw_ids_ce])[:18]
                return web.json_response({
                    'ok': True,
                    'opened_phone_view': False,
                    'enrolled_count': cnt_ce,
                    'enrolled_ids': list(res_ce.get('enrolled_ids') or []),
                    'mistakes': mob_m_ce,
                    'mistake_summary': res_ce['mistake_summary'],
                    'cowrite': cw_obj_e.mobile_brief(),
                    'message': f'📌 已将 {cnt_ce} 道静默伴写易错题收录进错题本，可点下方直接生成 A4 重练清卷！' if cnt_ce > 0 else '✓ 当前伴写易错题已全部在错题本中',
                })
            if act == 'parent_nudge':
                nudge = eng.send_parent_nudge(body.get('text') or question, sender=str(body.get('sender') or '家长'))
                cw_nd = getattr(eng, 'cowrite', None)
                act_s_nd = eng.sessions.get(eng.active_session_id) if eng.active_session_id else None
                is_sil_nd = bool(
                    (act_s_nd and act_s_nd.get('silent_start'))
                    or (not eng.is_lesson_in_progress() and isinstance(cw_nd, CoWriteMonitor) and (cw_nd.enabled or len(cw_nd.items) > 0))
                )
                if isinstance(cw_nd, CoWriteMonitor) and is_sil_nd:
                    cw_nd._poke_display_wake(force=True)
                return web.json_response({
                    'ok': True,
                    'opened_phone_view': False,
                    'silent': is_sil_nd,
                    'nudge': nudge,
                    'message': '💌 静默锦囊已轻声贴到大屏看板（零语音不打扰）！' if is_sil_nd else '💌 鼓励锦囊已投送到大屏并温柔播报给孩子！',
                })
            def _consume_shielded_exc(fut: asyncio.Task):
                if not fut.cancelled():
                    exc_bg = fut.exception()
                    if exc_bg:
                        log.warning('Background shielded mobile task failed: %s', _redact_secrets(exc_bg))

            async def _await_mobile_shielded(coro, soft_timeout: float = 12.2, session_work: bool = False):
                if session_work:
                    eng._cancel_previous_session_work()
                bg_t = eng._spawn(coro, session_work=session_work)
                retrieved = False
                try:
                    done_set, _ = await asyncio.wait({bg_t}, timeout=soft_timeout)
                    if bg_t in done_set:
                        retrieved = True
                        return True, bg_t.result()
                    return False, None
                finally:
                    if not retrieved:
                        bg_t.add_done_callback(_consume_shielded_exc)

            if act == 'dictation_start':
                raw_txt_d = str(body.get('words') or body.get('text') or question or '').strip()
                pack_d = str(body.get('pack_id') or '').strip()
                iv_d = body.get('interval_sec')
                rp_d = body.get('repeat_count')
                em_d = body.get('en_mode')
                ttl_d = str(body.get('title') or '').strip()
                img_b64_d = body.get('crop_b64') or body.get('page_b64')
                if img_b64_d and not raw_txt_d and not pack_d:
                    raw_img_d = _dec_b64(img_b64_d)

                    async def _do_mob_dict_ext():
                        st_de = await eng.dictation.extract_and_start(
                            image_bytes=raw_img_d, title=ttl_d,
                            interval_sec=iv_d, repeat_count=rp_d, en_mode=em_d,
                        )
                        eng.trigger_open_ui('dictation')
                        return st_de

                    fin_de, res_de = await _await_mobile_shielded(_do_mob_dict_ext(), soft_timeout=22.0, session_work=False)
                    if not fin_de:
                        eng.trigger_open_ui('dictation')
                        return web.json_response({
                            'ok': True,
                            'pending': True,
                            'opened_phone_view': True,
                            'message': '🎧 正在从课本照片提取听写词表，几秒内自动投屏到大屏听写台！',
                        })
                    return web.json_response({
                        'ok': True,
                        'opened_phone_view': True,
                        'dictation': eng.dictation.mobile_brief(),
                        'message': f"🎧 已从照片提取 {res_de['total_count']} 个字词并投送到大屏自助听写台！",
                    })
                if pack_d:
                    st_dp = eng.dictation.start_from_pack(pack_d, interval_sec=iv_d, repeat_count=rp_d, en_mode=em_d)
                else:
                    st_dp = eng.dictation.start_from_text(raw_txt_d, title=ttl_d, interval_sec=iv_d, repeat_count=rp_d, en_mode=em_d)
                eng.trigger_open_ui('dictation')
                return web.json_response({
                    'ok': True,
                    'opened_phone_view': True,
                    'dictation': eng.dictation.mobile_brief(),
                    'message': f"🎧 已将 {st_dp['total_count']} 个听写词投送到家里大屏，孩子点「开始播报」即可听写！",
                })
            if act == 'dictation_grade':
                img_b64_g = body.get('crop_b64') or body.get('page_b64')
                if not img_b64_g:
                    return web.json_response({'ok': False, 'error': '请先拍一张孩子的听写本照片再点批改'}, status=400)
                raw_img_g = _dec_b64(img_b64_g)

                async def _do_mob_dict_grd():
                    st_dg = await eng.dictation.grade_sheet(image_bytes=raw_img_g, use_camera=False, pencil_boost=True)
                    eng.trigger_open_ui('dictation')
                    return st_dg

                fin_dg, res_dg = await _await_mobile_shielded(_do_mob_dict_grd(), soft_timeout=26.0, session_work=False)
                if not fin_dg:
                    eng.trigger_open_ui('dictation')
                    return web.json_response({
                        'ok': True,
                        'pending': True,
                        'opened_phone_view': True,
                        'message': '📸 正在逐字批改听写本照片，几秒内同步批改报告到大屏与手机！',
                    })
                return web.json_response({
                    'ok': True,
                    'opened_phone_view': True,
                    'dictation': eng.dictation.mobile_brief(),
                    'message': f"✅ 批改完成：{res_dg['summary']}",
                })
            if act == 'dictation_ctl':
                sub_dc = str(body.get('sub_action') or '').strip().lower()
                if sub_dc == 'open_ui':
                    eng.trigger_open_ui('dictation')
                    return web.json_response({'ok': True, 'opened_phone_view': True, 'dictation': eng.dictation.mobile_brief(), 'message': '已在大屏打开自助听写台'})
                if sub_dc == 'enroll_mistakes':
                    res_em_d = eng.dictation.enroll_mistakes()
                    cnt_em_d = int(res_em_d.get('enrolled_count') or 0)
                    return web.json_response({
                        'ok': True,
                        'opened_phone_view': False,
                        'enrolled_count': cnt_em_d,
                        'dictation': eng.dictation.mobile_brief(),
                        'mistake_summary': res_em_d.get('mistake_summary', eng.public_mistake_summary()),
                        'message': f'📌 已将 {cnt_em_d} 个听写易错词收入错题本！' if cnt_em_d > 0 else '✓ 当前错词已在错题本中',
                    })
                if sub_dc == 'retry_wrong':
                    st_rw = eng.dictation.control('retry_wrong', body)
                    eng.trigger_open_ui('dictation')
                    return web.json_response({
                        'ok': True,
                        'opened_phone_view': True,
                        'dictation': eng.dictation.mobile_brief(),
                        'message': f"🔁 已在大屏开启 {st_rw['total_count']} 个错词单独重默！",
                    })
                st_ctl = eng.dictation.control(sub_dc or 'config', body)
                return web.json_response({'ok': True, 'opened_phone_view': False, 'dictation': eng.dictation.mobile_brief(), 'state': st_ctl})
            if act == 'trigger_mistake_retest':
                cw_rt_m = getattr(eng, 'cowrite', None)
                act_s_rt = eng.public_session(eng.active_session_id) if eng.active_session_id else None
                sil_rt_m = bool(body.get('silent')) if isinstance(body.get('silent'), bool) else bool(
                    (act_s_rt and act_s_rt.get('silent_start'))
                    or (isinstance(cw_rt_m, CoWriteMonitor) and (cw_rt_m.enabled or len(cw_rt_m.items) > 0))
                )

                async def _do_mob_retest():
                    r_mr = await eng.retest_mistake(
                        mid=str(body.get('mistake_id') or '').strip(),
                        mode=mode,
                        pedagogy=ped,
                        open_ui=True,
                        solo_challenge=bool(body.get('solo_challenge')),
                        silent_tts=sil_rt_m,
                    )
                    eng.trigger_open_ui('board')
                    return r_mr

                fin_mr, res_m = await _await_mobile_shielded(_do_mob_retest(), session_work=True)
                if not fin_mr:
                    return web.json_response({
                        'ok': True,
                        'pending': True,
                        'opened_phone_view': True,
                        'message': '🎯 AI 老师正在精编脱敏复测题，几秒内自动投屏到家里大屏！',
                    })
                resp_mr = {
                    'ok': True,
                    'silent': sil_rt_m,
                    'opened_phone_view': True,
                    'session': {
                        'id': res_m['session']['id'],
                        'title': res_m['session']['title'],
                        'status': res_m['session']['status'],
                        'source': res_m['session'].get('source', 'mistake_retest'),
                        'silent_start': bool(res_m['session'].get('silent_start')),
                    },
                    'message': '🎯 已在大屏静默投屏一道错题脱敏换数复测题（零语音不打扰）！' if sil_rt_m else '🎯 已在大屏投屏一道错题脱敏换数复测题！',
                }
                if isinstance(cw_rt_m, CoWriteMonitor):
                    resp_mr['cowrite'] = cw_rt_m.mobile_brief()
                return web.json_response(resp_mr)
            if act == 'trigger_variant':
                sid_v = str(body.get('session_id') or eng.active_session_id).strip()
                diff_v = 'harder' if str(body.get('difficulty') or '').lower() == 'harder' else 'same'
                cw_vr_m = getattr(eng, 'cowrite', None)
                act_s_vr = eng.public_session(sid_v) if sid_v else None
                sil_vr_m = bool(body.get('silent')) if isinstance(body.get('silent'), bool) else bool(
                    (act_s_vr and act_s_vr.get('silent_start'))
                    or (isinstance(cw_vr_m, CoWriteMonitor) and (cw_vr_m.enabled or len(cw_vr_m.items) > 0))
                )

                async def _do_mob_variant():
                    r_vr = await eng.generate_variant_problem(
                        sid_v,
                        mode=mode,
                        difficulty=diff_v,
                        pedagogy=ped,
                        solo_challenge=bool(body.get('solo_challenge')),
                        silent_tts=sil_vr_m,
                    )
                    eng.trigger_open_ui('board')
                    return r_vr

                fin_vr, res_v = await _await_mobile_shielded(_do_mob_variant(), session_work=True)
                if not fin_vr:
                    return web.json_response({
                        'ok': True,
                        'pending': True,
                        'opened_phone_view': True,
                        'message': '🎯 AI 老师正在精编同类巩固练习，几秒内自动投屏到家里大屏！',
                    })
                v_sess = res_v['session']
                v_msg = (
                    '已在大屏为孩子投屏一道同类习作升格练习！'
                    if v_sess.get('essay_critique') else (
                        '已在大屏为孩子投屏一道同类写作构思练习！'
                        if v_sess.get('writing_task') else
                        '已在大屏为孩子投屏一道同类巩固变式题！'
                    )
                )
                resp_vr = {
                    'ok': True,
                    'silent': sil_vr_m,
                    'opened_phone_view': True,
                    'session': {
                        'id': v_sess['id'],
                        'title': v_sess['title'],
                        'status': v_sess['status'],
                        'source': v_sess.get('source', 'variant'),
                        'silent_start': bool(v_sess.get('silent_start')),
                    },
                    'message': ('🔇 ' + v_msg.rstrip('！') + '（零语音不打扰）！') if sil_vr_m else v_msg,
                }
                if isinstance(cw_vr_m, CoWriteMonitor):
                    resp_vr['cowrite'] = cw_vr_m.mobile_brief()
                return web.json_response(resp_vr)
            if act == 'cowrite_snap':
                cw_sn = getattr(eng, 'cowrite', None)
                if not isinstance(cw_sn, CoWriteMonitor):
                    return web.json_response({'ok': False, 'error': '伴写模块未就绪'}, status=400)
                cw_sn._ensure_loaded()
                iid_sn = str(body.get('id') or '').strip()
                hit_sn = next((x for x in cw_sn.items if x.get('id') == iid_sn), None)
                if not hit_sn:
                    return web.json_response({'ok': False, 'error': '未找到该伴写题目记录'}, status=404)
                sn_file = str(hit_sn.get('snap_name') or '').strip()
                raw_sn = None
                if sn_file and re.fullmatch(r'c0_[0-9a-f]{12}\.jpg', sn_file):
                    with contextlib.suppress(OSError):
                        _, raw_sn = _read_regular_bytes(SNAP_DIR / sn_file, 6 * 1024 * 1024)
                is_w_sn = (hit_sn.get('status') == 'warn')
                is_grad_sn = cw_sn._is_item_graduated(hit_sn)
                return web.json_response({
                    'ok': True,
                    'opened_phone_view': False,
                    'has_snap': bool(raw_sn),
                    'item': {
                        'id': hit_sn['id'],
                        'no': hit_sn.get('no') or '本题',
                        'status': hit_sn.get('status') or 'ok',
                        'subject': hit_sn.get('subject') or 'math',
                        'topic': hit_sn.get('topic') or '',
                        'step_where': hit_sn.get('step_where') if is_w_sn else (hit_sn.get('warn_step_where') or ''),
                        'checkpoint': hit_sn.get('checkpoint') if is_w_sn else (hit_sn.get('warn_checkpoint') or hit_sn.get('checkpoint') or ''),
                        'deep_hint': hit_sn.get('deep_hint') if is_w_sn else (hit_sn.get('warn_deep_hint') or ''),
                        'formula_tip': hit_sn.get('formula_tip') if is_w_sn else (hit_sn.get('warn_formula_tip') or ''),
                        'resolved': bool(hit_sn.get('resolved')),
                        'corrected_pass': bool(hit_sn.get('corrected_pass')),
                        'starred': bool(hit_sn.get('starred')),
                        'enrolled_mistake': bool(hit_sn.get('enrolled_mistake')),
                        'blackboard_solved': bool(hit_sn.get('blackboard_solved')),
                        'variant_passed': bool(hit_sn.get('variant_passed')),
                        'mistake_graduated': is_grad_sn,
                    },
                    'snap_b64': ('data:image/jpeg;base64,' + base64.b64encode(raw_sn).decode('ascii')) if raw_sn else '',
                })
            if act == 'cowrite_remote_ctl':
                cw_rc = getattr(eng, 'cowrite', None)
                if not isinstance(cw_rc, CoWriteMonitor):
                    return web.json_response({'ok': False, 'error': '伴写模块未就绪'}, status=400)
                sub_act = str(body.get('sub_action') or '').strip().lower()
                if sub_act == 'toggle':
                    en_req = body.get('enabled') if isinstance(body.get('enabled'), bool) else None
                    subj_req = str(body.get('subject')) if 'subject' in body and body.get('subject') is not None else None
                    pm_req = str(body.get('pause_mode')) if 'pause_mode' in body and body.get('pause_mode') is not None else None
                    st_t = await cw_rc.toggle(enabled=en_req, subject=subj_req, pause_mode=pm_req)
                    if st_t.get('enabled'):
                        eng.trigger_open_ui('cowrite')
                    return web.json_response({
                        'ok': True,
                        'silent': True,
                        'opened_phone_view': False,
                        'cowrite': cw_rc.mobile_brief(),
                        'message': '👀 已在大屏开启高拍仪静默伴写守护（零语音不打扰）' if st_t.get('enabled') else '已暂停高拍仪静默伴写并释放摄像头',
                    })
                if sub_act == 'pace':
                    pm_req = cw_rc._norm_pause_mode(body.get('pause_mode'))
                    await cw_rc.toggle(enabled=None, pause_mode=pm_req)
                    pace_sec = cw_rc.PAUSE_MODE_MAP.get(pm_req, 3.8)
                    return web.json_response({
                        'ok': True,
                        'silent': True,
                        'opened_phone_view': False,
                        'cowrite': cw_rc.mobile_brief(),
                        'message': f'⏱ 已将伴写停笔等待设为 {pace_sec} 秒',
                    })
                if sub_act == 'new_page':
                    cw_rc.resolve_action(new_page=True)
                    return web.json_response({
                        'ok': True,
                        'silent': True,
                        'opened_phone_view': False,
                        'cowrite': cw_rc.mobile_brief(),
                        'message': f'📄 已切换至新作业页（第 {cw_rc.page_epoch} 页）',
                    })
                if sub_act == 'star_one':
                    iid_one = str(body.get('id') or '').strip()
                    hit_s = next((x for x in cw_rc.items if x.get('id') == iid_one), None)
                    if not hit_s:
                        return web.json_response({'ok': False, 'error': '未找到该伴写题目记录'}, status=404)
                    want_s = body['star'] if isinstance(body.get('star'), bool) else (not bool(hit_s.get('starred')))
                    cw_rc.resolve_action(item_id=iid_one, resolved=None, star=bool(want_s))
                    return web.json_response({
                        'ok': True,
                        'silent': True,
                        'opened_phone_view': False,
                        'starred': bool(hit_s.get('starred')),
                        'cowrite': cw_rc.mobile_brief(),
                        'message': '⭐ 已将该题标为重点复盘题' if hit_s.get('starred') else '已取消重点标记',
                    })
                if sub_act == 'resolve_one':
                    iid_one = str(body.get('id') or '').strip()
                    hit_r = next((x for x in cw_rc.items if x.get('id') == iid_one), None)
                    if not hit_r:
                        return web.json_response({'ok': False, 'error': '未找到该伴写题目记录'}, status=404)
                    want_r = body['resolved'] if isinstance(body.get('resolved'), bool) else True
                    cw_rc.resolve_action(item_id=iid_one, resolved=bool(want_r))
                    return web.json_response({
                        'ok': True,
                        'silent': True,
                        'opened_phone_view': False,
                        'resolved': bool(hit_r.get('resolved')),
                        'cowrite': cw_rc.mobile_brief(),
                        'message': '✓ 已将该题标为纸面已订正' if hit_r.get('resolved') else '已恢复为待自查状态',
                    })
                if sub_act == 'explain_one':
                    iid_one = str(body.get('id') or '').strip()
                    res_ex = await cw_rc.explain_item(iid_one, mode=_norm_mode(body.get('mode') or 'deep'), pedagogy='junior', silent=True)
                    eng.trigger_open_ui('board')
                    return web.json_response({
                        'ok': True,
                        'silent': True,
                        'opened_phone_view': False,
                        'session': res_ex.get('session'),
                        'cowrite': cw_rc.mobile_brief(),
                        'message': '🔇 已在家里大屏静默展开本题错因拆解（零语音不打扰）',
                    })
                if sub_act == 'variant_one':
                    iid_one = str(body.get('id') or '').strip()
                    prev_cw_v1 = int(cw_rc.seq)

                    async def _do_cw_var_one():
                        r_vr = await cw_rc.variant_from_item(
                            iid_one,
                            mode=_norm_mode(body.get('mode') or 'fast'),
                            silent=True,
                            solo_challenge=bool(body.get('solo_challenge')),
                        )
                        eng.trigger_open_ui('board')
                        return r_vr

                    fin_v1, res_vr = await _await_mobile_shielded(_do_cw_var_one(), session_work=True)
                    if not fin_v1:
                        return web.json_response({
                            'ok': True,
                            'pending': True,
                            'silent': True,
                            'prev_cowrite_seq': prev_cw_v1,
                            'opened_phone_view': False,
                            'cowrite': cw_rc.mobile_brief(),
                            'message': '🎯 AI 老师正在为大屏静默精编本题同类变式，几秒内自动投送！',
                        })
                    return web.json_response({
                        'ok': True,
                        'silent': True,
                        'opened_phone_view': False,
                        'session': res_vr.get('session'),
                        'cowrite': cw_rc.mobile_brief(),
                        'message': '⚡ 已在家里大屏静默投送本题先独做变式（零语音）' if body.get('solo_challenge') else '🎯 已在家里大屏静默投送本题同类变式题（零语音）',
                    })
                if sub_act == 'explain_top_warn':
                    cw_rc._ensure_loaded()
                    hit_top = next((x for x in cw_rc.items if x.get('status') == 'warn' and not x.get('resolved')), None) or next(
                        (x for x in cw_rc.items if x.get('status') == 'warn' or x.get('starred')), None
                    )
                    if not hit_top:
                        return web.json_response({'ok': False, 'error': '当前没有待自查错题'}, status=404)
                    res_top = await cw_rc.explain_item(hit_top['id'], mode=_norm_mode(body.get('mode') or 'deep'), pedagogy='junior', silent=True)
                    eng.trigger_open_ui('board')
                    return web.json_response({
                        'ok': True,
                        'silent': True,
                        'opened_phone_view': False,
                        'session': res_top.get('session'),
                        'cowrite': cw_rc.mobile_brief(),
                        'message': f"🔇 已在家里大屏静默展开 {hit_top.get('no') or '首道待查题'} 错因拆解（零语音）",
                    })
                if sub_act == 'sprint_warns':
                    cw_rc._ensure_loaded()
                    grad_ids = cw_rc._graduated_mistake_ids()
                    if any(cw_rc._is_item_enrollable(x, grad_ids) for x in cw_rc.items):
                        cw_rc.enroll_mistake(enroll_all_warns=True)
                        grad_ids = cw_rc._graduated_mistake_ids()
                    eng._ensure_history_loaded()
                    eng._ensure_mistakes_loaded()
                    has_cw_src = any(
                        not m.get('graduated') and str(m.get('error_tag') or '').startswith('伴写·')
                        for m in eng.mistakes
                    ) or any(
                        bool(x.get('mistake_id')) and not cw_rc._is_item_graduated(x, grad_ids)
                        for x in cw_rc.items
                    )
                    if not has_cw_src:
                        return web.json_response({'ok': False, 'error': '当前暂无可特训的伴写易错题或重点题'}, status=404)

                    async def _do_cw_sprint():
                        res_sp = await eng.start_mistake_sprint(
                            count=3,
                            mode=_norm_mode(body.get('mode') or 'fast'),
                            pedagogy='junior',
                            solo_challenge=bool(body.get('solo_challenge')),
                            silent_tts=True,
                            prefer_cowrite=True,
                        )
                        eng.trigger_open_ui('board')
                        return res_sp

                    fin_sw, res_sw = await _await_mobile_shielded(_do_cw_sprint(), session_work=True)
                    if not fin_sw:
                        return web.json_response({
                            'ok': True,
                            'pending': True,
                            'silent': True,
                            'opened_phone_view': False,
                            'cowrite': cw_rc.mobile_brief(),
                            'message': '🚀 AI 老师正在为大屏静默组卷初三伴写连闯，几秒内自动开闯！',
                        })
                    return web.json_response({
                        'ok': True,
                        'pending': False,
                        'silent': True,
                        'opened_phone_view': False,
                        'sprint_id': res_sw.get('sprint_id'),
                        'total': res_sw.get('total'),
                        'session': res_sw.get('session'),
                        'cowrite': cw_rc.mobile_brief(),
                        'message': '🚀 已在家里大屏开启初三伴写易错静默连闯（零语音不打扰）！',
                    })
                if sub_act in ('scan_desk', 'recheck_warns', 'recheck_one'):
                    prev_cw_s = cw_rc.seq
                    iid_one = str(body.get('id') or '').strip()

                    async def _run_rc_scan():
                        if sub_act == 'recheck_one':
                            return await cw_rc.recheck_item(iid_one)
                        if sub_act == 'recheck_warns':
                            return await cw_rc.recheck_all_warns()
                        if req_page_id:
                            return await cw_rc.check_now(stage_page_id=req_page_id)
                        return await cw_rc.check_now()

                    fin_rc, res_rc = await _await_mobile_shielded(_run_rc_scan(), soft_timeout=12.2, session_work=False)
                    if not fin_rc:
                        return web.json_response({
                            'ok': True,
                            'pending': True,
                            'silent': True,
                            'prev_cowrite_seq': prev_cw_s,
                            'opened_phone_view': False,
                            'message': '🔍 已触发静默核查，正在后台并行验算（零语音），几秒内自动同步结果！',
                        })
                    return web.json_response({
                        'ok': True,
                        'pending': False,
                        'silent': True,
                        'opened_phone_view': False,
                        'cowrite': cw_rc.mobile_brief(),
                        'message': res_rc.get('note') or '已完成静默核查',
                    })
                return web.json_response({'ok': False, 'error': '未知伴写遥控指令'}, status=400)

            rot_steps_in = 0
            with contextlib.suppress(TypeError, ValueError):
                rot_steps_in = int(body.get('rot_steps') or 0) % 4
            page_bytes = _dec_b64(body.get('page_b64'))
            crop_bytes = _dec_b64(body.get('crop_b64'))
            target_sid_in = str(body.get('session_id') or eng.active_session_id).strip() if act == 'stage_silent' else ''
            saved_pid = ''
            if page_bytes:
                page_bytes = await eng.phone_studio.normalize_to_jpeg(page_bytes)
                saved_pid = eng.phone_studio.save_page(
                    page_bytes, note=question, crop_box=crop_box, client_key=client_key, page_id=req_page_id,
                    rot_steps=rot_steps_in, exclude_sid=target_sid_in,
                )
            elif req_page_id:
                eng.phone_studio.select_page(req_page_id)
                if crop_box and not rot_steps_in:
                    eng.phone_studio.update_active_crop(crop_box)
            if crop_bytes:
                crop_bytes = await eng.phone_studio.normalize_to_jpeg(crop_bytes)
            if act == 'stage_silent':
                if not page_bytes:
                    return web.json_response({'ok': False, 'error': '缺少整页图片数据'}, status=400)
                target_sid = str(body.get('session_id') or eng.active_session_id).strip()
                act_s = eng.sessions.get(target_sid) if target_sid else None
                if act_s and act_s.get('source') == 'phone':
                    act_s['stage_page_id'] = saved_pid
                    if crop_box:
                        eng.phone_studio.record_done_crop(saved_pid, act_s['id'], crop_box)
                    if act_s.get('status') == 'ready':
                        eng._save_to_history(act_s)
                return web.json_response({
                    'ok': True,
                    'staged_seq': eng.phone_studio.seq,
                    'page_id': saved_pid,
                    'page_count': len(eng.phone_studio.pages),
                    'opened_phone_view': False,
                })
            if act == 'check_page':
                target_pid = saved_pid or req_page_id or eng.phone_studio.active_id
                if not target_pid and not eng.phone_studio.has_page():
                    return web.json_response({'ok': False, 'error': '请先选择或拍摄一整页已作答的作业照片'}, status=400)
                eff_pid_ck = target_pid or eng.phone_studio.active_id
                prev_pg_ck = next((x for x in eng.phone_studio.pages if x.get('id') == eff_pid_ck), None)
                prev_chk_seq = int((prev_pg_ck or {}).get('check_seq') or 0)

                async def _do_mob_check():
                    try:
                        r_ck = await eng.check_staged_worksheet_page(page_id=target_pid, pencil_boost=pencil_boost)
                        if not eng.is_lesson_in_progress():
                            eng.trigger_open_ui('phone')
                        return r_ck
                    except Exception as exc:
                        pg_err = next((x for x in eng.phone_studio.pages if x.get('id') == eff_pid_ck), None)
                        if pg_err is not None:
                            pg_err['check_error'] = _redact_secrets(exc)[:80]
                            pg_err['check_seq'] = int(pg_err.get('check_seq') or 0) + 1
                            eng.phone_studio.seq += 1
                            pg_err['seq'] = eng.phone_studio.seq
                            eng.phone_studio._save_prefs()
                        raise

                fin_ck, res_chk = await _await_mobile_shielded(_do_mob_check())
                if not fin_ck:
                    return web.json_response({
                        'ok': True,
                        'pending': True,
                        'page_id': eff_pid_ck,
                        'prev_check_seq': prev_chk_seq,
                        'opened_phone_view': not eng.is_lesson_in_progress(),
                        'message': '📋 AI 老师正在逐题精细巡检，几秒内自动在画面标出批改结果！',
                    })
                return web.json_response(dict(
                    res_chk,
                    opened_phone_view=not eng.is_lesson_in_progress(),
                    message=f"📋 整页批改完成：{res_chk['summary']}（点击橙色错题框即可启动启发订正）！",
                ))
            if act == 'cowrite_check':
                target_pid_cw = saved_pid or req_page_id or eng.phone_studio.active_id
                if req_page_id and not saved_pid and not page_bytes:
                    if not eng.phone_studio.select_page(req_page_id):
                        return web.json_response({'ok': False, 'error': '未找到指定的暂存作业页，请先上传作业照片'}, status=404)
                elif target_pid_cw:
                    eng.phone_studio.select_page(target_pid_cw)
                if not eng.phone_studio.has_page() and not page_bytes:
                    return web.json_response({'ok': False, 'error': '请先选择或拍摄一整页已作答的作业照片'}, status=400)
                subj_cw = str(body['subject']) if 'subject' in body and body.get('subject') is not None else None
                cw_scope = str(body.get('cowrite_scope') or '').strip().lower()
                is_single_crop_cw = False if cw_scope == 'page' else bool(
                    cw_scope == 'crop' or (
                        isinstance(crop_box, dict)
                        and (float(crop_box.get('w', 1.0)) < 0.90 or float(crop_box.get('h', 1.0)) < 0.82)
                    )
                )

                async def _do_mob_cowrite():
                    if is_single_crop_cw:
                        r_cw = await eng.cowrite.check_crop(
                            crop_box=crop_box,
                            no_hint=question[:16] if question else '',
                            subject=subj_cw,
                            page_id=target_pid_cw,
                            pencil_boost=pencil_boost,
                        )
                    else:
                        if target_pid_cw and (
                            bool(req_page_id)
                            or eng.phone_studio.active_id != target_pid_cw
                            or bool(eng.camera.discover().get('connected'))
                        ):
                            r_cw = await eng.cowrite.check_now(subject=subj_cw, pencil_boost=pencil_boost, stage_page_id=target_pid_cw)
                        else:
                            r_cw = await eng.cowrite.check_now(subject=subj_cw, pencil_boost=pencil_boost)
                    if not eng.is_lesson_in_progress():
                        eng.trigger_open_ui('cowrite')
                    return r_cw

                prev_cw_seq_m = int(eng.cowrite.seq)
                fin_cw, res_cw = await _await_mobile_shielded(_do_mob_cowrite())
                if not fin_cw:
                    if not eng.is_lesson_in_progress():
                        eng.trigger_open_ui('cowrite')
                    return web.json_response({
                        'ok': True,
                        'pending': True,
                        'page_id': target_pid_cw,
                        'prev_cowrite_seq': prev_cw_seq_m,
                        'opened_phone_view': not eng.is_lesson_in_progress(),
                        'message': '👀 初三双核模型正在静默核查本页步骤，几秒内自动投送到大屏伴写看板（零语音打扰）！',
                    })
                cw_pub_m = res_cw.get('cowrite') or {}
                pg_cw_now = next((p for p in eng.phone_studio.pages if p.get('id') == target_pid_cw), None)
                return web.json_response({
                    'ok': True,
                    'page_id': target_pid_cw,
                    'cowrite': cw_pub_m,
                    'check_marks': list((pg_cw_now or {}).get('check_marks') or []),
                    'opened_phone_view': not eng.is_lesson_in_progress(),
                    'message': f"👀 已投送大屏静默伴写看板：已核 {cw_pub_m.get('total_count', 0)} 题（待自查 {cw_pub_m.get('warn_open_count', 0)} 处）！",
                })
            if act == 'stage_only':
                if not page_bytes:
                    return web.json_response({'ok': False, 'error': '请先选择要上传的整页作业图片'}, status=400)
                in_prog = eng.is_lesson_in_progress()
                if not in_prog:
                    eng.trigger_open_ui('phone')
                cnt = len(eng.phone_studio.pages)
                return web.json_response({
                    'ok': True,
                    'staged_seq': eng.phone_studio.seq,
                    'page_id': saved_pid,
                    'page_count': cnt,
                    'opened_phone_view': not in_prog,
                    'message': (
                        f'已存入大屏作业夹（共 {cnt} 页，未打断当前闯关，做完本题可翻页选题）！'
                        if in_prog else f'已投到大屏作业夹（共 {cnt} 页），可在屏幕上翻页拖动绿框逐题开讲！'
                    ),
                })

            target_img = crop_bytes
            if not target_img and page_bytes:
                target_img = await eng.phone_studio.enhance_jpeg(page_bytes, crop_box=crop_box, crisp=crisp, pencil_boost=pencil_boost)
            t_mode = _norm_teach_mode(body.get('teach_mode'))
            sess = eng.start_live_problem(
                question=question, image_bytes=target_img, mode=mode,
                title='草稿错因诊断微课' if t_mode == 'diagnose_draft' else '手机拍题微课',
                source='phone', open_ui=True, pedagogy=ped, teach_mode=t_mode,
            )
            ck_pid = eng.phone_studio.find_page_id_by_client_key(client_key) if client_key else ''
            bind_pid = saved_pid or req_page_id or ck_pid or (eng.phone_studio.active_id if not crop_bytes else '')
            if sess and sess['id'] in eng.sessions and bind_pid:
                eng.sessions[sess['id']]['stage_page_id'] = bind_pid
                sess['stage_page_id'] = bind_pid
                if bind_pid and crop_box and (page_bytes or not rot_steps_in):
                    eng.phone_studio.record_done_crop(bind_pid, sess['id'], crop_box)
            eng.open_ui_target = 'board'
            return web.json_response({
                'ok': True,
                'session': {'id': sess['id'], 'title': sess['title'], 'status': sess['status']},
                'message': '已投屏到大屏开启「草稿找错因」4步启发订正！' if t_mode == 'diagnose_draft' else '已投屏到家里大屏开始 4 步启发式黑板讲题！',
            })
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)

    async def api_session(request: web.Request):
        sid = request.query.get('id', '') or eng.active_session_id
        if request.query.get('activate') == '1' and sid:
            if eng._get_or_hydrate_session(sid) is not None:
                eng.active_session_id = sid
        pub = eng.public_session(sid)
        if not pub:
            return web.json_response({'ok': False, 'error': '未找到讲题记录'}, status=404)
        return web.json_response({'ok': True, 'session': pub})

    async def api_tts(request: web.Request):
        sid = request.query.get('sid', '')
        try:
            step_idx = int(request.query.get('idx', '0'))
        except ValueError:
            raise web.HTTPBadRequest()
        pub = eng.public_session(sid)
        if not pub:
            raise web.HTTPNotFound()
        if step_idx == -1:
            qa = pub.get('active_qa') or {}
            if not qa.get('say'):
                raise web.HTTPNotFound()
            say = qa['say']
        elif 0 <= step_idx < len(pub['steps']):
            say = pub['steps'][step_idx].get('say') or ''
        else:
            raise web.HTTPNotFound()
        wav = await eng.synthesize_wav(say, priority=True)
        cc_hdr = 'no-store' if wav == _FALLBACK_SILENT_WAV else 'private, max-age=600'
        return web.Response(body=wav, content_type='audio/wav', headers={'Cache-Control': cc_hdr})

    async def api_orient(_request: web.Request):
        # 2026-11-20 Claude：只回三个原始读数，方向由页面判断（防抖、放平不转）；读不到就 sensor:false
        acc = _accel_dir()
        if acc is not None:
            try:
                x, y, z = (int((acc / f'in_accel_{a}_raw').read_text().strip()) for a in ('x', 'y', 'z'))
                return web.json_response({'ok': True, 'sensor': True, 'x': x, 'y': y, 'z': z}, headers={'Cache-Control': 'no-store'})
            except (OSError, ValueError):
                _ACCEL_CACHE['dir'] = None
        return web.json_response({'ok': True, 'sensor': False}, headers={'Cache-Control': 'no-store'})

    async def api_tts_hint(request: web.Request):
        txt = str(request.query.get('text') or '').strip()[:420]
        if not txt:
            raise web.HTTPBadRequest()
        wav = await eng.synthesize_wav(txt, priority=True)
        cc_hdr = 'no-store' if wav == _FALLBACK_SILENT_WAV else 'private, max-age=600'
        return web.Response(body=wav, content_type='audio/wav', headers={'Cache-Control': cc_hdr})

    async def api_progress(request: web.Request):
        body = await _json_dict(request)
        sid = str(body.get('id') or eng.active_session_id).strip()
        return web.json_response(eng.update_progress(sid, body))

    async def api_ask(request: web.Request):
        body = await _json_dict(request, allow_empty=False)
        sid = str(body.get('id') or eng.active_session_id).strip()
        ask_txt = str(body.get('question') or '').strip()
        ped = _norm_pedagogy(body.get('pedagogy'))
        f_veiled = bool(body.get('formula_veiled'))
        u_gate = bool(body.get('unsolved_gate'))
        sil_ask = body.get('silent') if isinstance(body.get('silent'), bool) else None
        upto_raw = body.get('upto_idx') if body.get('upto_idx') is not None else body.get('step_idx')
        upto_idx = None
        if upto_raw is not None:
            with contextlib.suppress(TypeError, ValueError):
                upto_idx = int(upto_raw)
        try:
            res = await eng.ask_followup(sid, ask_txt, upto_idx=upto_idx, pedagogy=ped, formula_veiled=f_veiled, unsolved_gate=u_gate, silent_tts=sil_ask)
            return web.json_response(res)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)

    async def api_feynman(request: web.Request):
        body = await _json_dict(request, allow_empty=False)
        sid = str(body.get('id') or eng.active_session_id).strip()
        st_txt = str(body.get('text') or '').strip()
        ped = _norm_pedagogy(body.get('pedagogy'))
        sil_fy = body.get('silent') if isinstance(body.get('silent'), bool) else None
        try:
            res = await eng.evaluate_feynman_explanation(sid, st_txt, pedagogy=ped, silent_tts=sil_fy)
            return web.json_response(res)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)

    async def api_mistakes(_request: web.Request):
        return web.json_response({
            'ok': True,
            'mistakes': eng.public_mistakes(),
            'summary': eng.public_mistake_summary(),
        })

    async def api_mistake_retest(request: web.Request):
        body = await _json_dict(request)
        mid = str(body.get('id') or body.get('mistake_id') or '').strip()
        mode = _norm_mode(body.get('mode'))
        ped = _norm_pedagogy(body.get('pedagogy'))
        solo_ch = bool(body.get('solo_challenge'))
        sil_mr = body.get('silent') if isinstance(body.get('silent'), bool) else None
        try:
            res = await eng.retest_mistake(
                mid=mid, mode=mode, pedagogy=ped,
                open_ui=bool(body.get('open_ui')), solo_challenge=solo_ch, silent_tts=sil_mr,
            )
            return web.json_response(res)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)

    async def api_mistake_delete(request: web.Request):
        body = await _json_dict(request)
        return web.json_response(eng.delete_mistake(
            mid=str(body.get('id') or '').strip(),
            clear_graduated=bool(body.get('clear_graduated')),
            clear_all=bool(body.get('clear_all')),
        ))

    async def api_mistake_sprint_start(request: web.Request):
        body = await _json_dict(request)
        try:
            cnt = 3
            with contextlib.suppress(TypeError, ValueError):
                cnt = int(body.get('count') or 3)
            mode = _norm_mode(body.get('mode'))
            ped = _norm_pedagogy(body.get('pedagogy'))
            solo_ch = bool(body.get('solo_challenge'))
            sil_sp = bool(body.get('silent'))
            pref_cw = bool(body.get('prefer_cowrite'))
            res = await eng.start_mistake_sprint(count=cnt, mode=mode, pedagogy=ped, solo_challenge=solo_ch, silent_tts=sil_sp, prefer_cowrite=pref_cw)
            return web.json_response(res)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)

    async def api_mistake_sprint_next(request: web.Request):
        body = await _json_dict(request)
        sp_id = str(body.get('sprint_id') or '').strip()
        mode = _norm_mode(body.get('mode'))
        ped = _norm_pedagogy(body.get('pedagogy'))
        raw_solo = body.get('solo_challenge')
        solo_opt = bool(raw_solo) if isinstance(raw_solo, bool) else None
        sil_nx = body.get('silent') if isinstance(body.get('silent'), bool) else None
        try:
            res = await eng.advance_mistake_sprint(sp_id, mode=mode, pedagogy=ped, solo_challenge=solo_opt, silent_tts=sil_nx)
            return web.json_response(res)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)

    async def api_alt_method(request: web.Request):
        body = await _json_dict(request)
        sid = str(body.get('id') or eng.active_session_id).strip()
        ped = _norm_pedagogy(body.get('pedagogy'))
        sil_alt = body.get('silent') if isinstance(body.get('silent'), bool) else None
        try:
            res = await eng.generate_alt_method(sid, pedagogy=ped, silent_tts=sil_alt)
            return web.json_response(res)
        except (ValueError, LookupError) as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=500)

    async def api_foundation(request: web.Request):
        body = await _json_dict(request)
        sid = str(body.get('id') or eng.active_session_id).strip()
        ped = _norm_pedagogy(body.get('pedagogy'))
        sil_f = body.get('silent') if isinstance(body.get('silent'), bool) else None
        try:
            res = await eng.get_foundation_capsule(sid, pedagogy=ped, silent_tts=sil_f)
            return web.json_response(res)
        except (ValueError, LookupError) as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=500)

    async def api_exam_rubric(request: web.Request):
        body = await _json_dict(request)
        sid = str(body.get('id') or eng.active_session_id).strip()
        ped = _norm_pedagogy(body.get('pedagogy'))
        sil_rub = body.get('silent') if isinstance(body.get('silent'), bool) else None
        try:
            res = await eng.get_exam_rubric(sid, pedagogy=ped, silent_tts=sil_rub)
            return web.json_response(res)
        except (ValueError, LookupError) as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=500)

    async def api_polya_simplify(request: web.Request):
        body = await _json_dict(request)
        sid = str(body.get('id') or eng.active_session_id).strip()
        ped = _norm_pedagogy(body.get('pedagogy'))
        sil_pl = body.get('silent') if isinstance(body.get('silent'), bool) else None
        try:
            res = await eng.get_polya_reduction(sid, pedagogy=ped, silent_tts=sil_pl)
            return web.json_response(res)
        except (ValueError, LookupError) as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=500)

    async def api_archetype_map(request: web.Request):
        body = await _json_dict(request)
        sid = str(body.get('id') or eng.active_session_id).strip()
        ped = _norm_pedagogy(body.get('pedagogy'))
        sil_am = body.get('silent') if isinstance(body.get('silent'), bool) else None
        try:
            res = await eng.get_archetype_mapping(sid, pedagogy=ped, silent_tts=sil_am)
            return web.json_response(res)
        except (ValueError, LookupError) as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=500)

    async def api_read_aloud(request: web.Request):
        body = await _json_dict(request)
        sid = str(body.get('id') or eng.active_session_id).strip()
        spoken = str(body.get('text') or '').strip()
        sil_ra = body.get('silent') if isinstance(body.get('silent'), bool) else None
        try:
            res = await eng.evaluate_read_aloud(sid, spoken, silent_tts=sil_ra)
            return web.json_response(res)
        except (ValueError, LookupError) as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=500)

    async def api_check_scratch(request: web.Request):
        body = await _json_dict(request)
        sid = str(body.get('id') or eng.active_session_id).strip()
        ped = _norm_pedagogy(body.get('pedagogy'))
        st_idx = 0
        with contextlib.suppress(TypeError, ValueError):
            st_idx = int(body.get('step_idx') or 0)
        img_b64 = str(body.get('image_b64') or '').strip()
        if ',' in img_b64[:64]:
            img_b64 = img_b64.split(',', 1)[1]
        solo_m = bool(body.get('solo_mode'))
        sil_sc = body.get('silent') if isinstance(body.get('silent'), bool) else None
        try:
            img_bytes = base64.b64decode(img_b64) if img_b64 else b''
            res = await eng.inspect_board_scratch(sid, img_bytes, step_idx=st_idx, pedagogy=ped, solo_mode=solo_m, silent_tts=sil_sc)
            return web.json_response(res)
        except (ValueError, LookupError) as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=500)

    async def api_subquestion_next(request: web.Request):
        body = await _json_dict(request)
        sid = str(body.get('id') or eng.active_session_id).strip()
        t_idx = body.get('target_idx')
        if t_idx is not None:
            with contextlib.suppress(TypeError, ValueError):
                t_idx = int(t_idx)
        mode = _norm_mode(body.get('mode'))
        ped = _norm_pedagogy(body.get('pedagogy'))
        solo_ch = bool(body.get('solo_challenge'))
        sil_sq = body.get('silent') if isinstance(body.get('silent'), bool) else None
        try:
            res = await eng.advance_subquestion(sid, target_idx=t_idx, mode=mode, pedagogy=ped, solo_challenge=solo_ch, silent_tts=sil_sq)
            return web.json_response(res)
        except (ValueError, LookupError) as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=500)

    async def api_tool(request: web.Request):
        if request.headers.get('X-TS-Internal') != '1' or request.headers.get('Origin') is not None:
            raise web.HTTPForbidden()
        body = await _json_dict(request, allow_empty=False)
        act = str(body.get('action') or 'open').strip().lower()
        q = str(body.get('question') or body.get('query') or '').strip()
        mode = _norm_mode(body.get('mode'))
        ped = _norm_pedagogy(body.get('pedagogy'))
        if act in ('open', 'open_ui') and not q:
            if body.get('sample_id'):
                silent = bool(body.get('silent_preview'))
                sess = eng.start_golden_sample(str(body['sample_id']), open_ui=True, prefetch_tts=not silent)
                if silent:
                    eng.sessions[sess['id']]['silent_preview'] = True
                    p_st = body.get('preview_step')
                    eng.sessions[sess['id']]['preview_step'] = int(p_st) if p_st is not None else 2
                    if body.get('preview_chain_idx') is not None:
                        eng.sessions[sess['id']]['preview_chain_idx'] = int(body['preview_chain_idx'])
                    if body.get('preview_formula_picker'):
                        eng.sessions[sess['id']]['preview_formula_picker'] = True
                    if body.get('preview_feynman_puzzle'):
                        eng.sessions[sess['id']]['preview_feynman_puzzle'] = True
                    if body.get('preview_feynman_ring') is not None:
                        eng.sessions[sess['id']]['preview_feynman_ring'] = int(body['preview_feynman_ring'])
                    if body.get('preview_hint_lv') is not None:
                        eng.sessions[sess['id']]['preview_hint_lv'] = int(body['preview_hint_lv'])
                    if body.get('preview_canvas_tap'):
                        eng.sessions[sess['id']]['preview_canvas_tap'] = str(body['preview_canvas_tap'])[:24]
                return web.json_response({'ok': True, 'session_id': sess['id'], 'text': '已打开样题预览。'})
            if body.get('session_id'):
                sid_req = str(body['session_id']).strip()
                pub_s = eng.public_session(sid_req)
                if pub_s:
                    eng._get_or_hydrate_session(sid_req)
                    eng.active_session_id = sid_req
                    if body.get('silent_preview'):
                        eng.sessions[sid_req]['silent_preview'] = True
                        p_st = body.get('preview_step')
                        eng.sessions[sid_req]['preview_step'] = int(p_st) if p_st is not None else 2
                        if body.get('preview_chain_idx') is not None:
                            eng.sessions[sid_req]['preview_chain_idx'] = int(body['preview_chain_idx'])
                        if body.get('preview_formula_picker'):
                            eng.sessions[sid_req]['preview_formula_picker'] = True
                        if body.get('preview_feynman_puzzle'):
                            eng.sessions[sid_req]['preview_feynman_puzzle'] = True
                        if body.get('preview_feynman_ring') is not None:
                            eng.sessions[sid_req]['preview_feynman_ring'] = int(body['preview_feynman_ring'])
                        if body.get('preview_hint_lv') is not None:
                            eng.sessions[sid_req]['preview_hint_lv'] = int(body['preview_hint_lv'])
                        if body.get('preview_canvas_tap'):
                            eng.sessions[sid_req]['preview_canvas_tap'] = str(body['preview_canvas_tap'])[:24]
                        if body.get('preview_snap_modal'):
                            eng.sessions[sid_req]['preview_snap_modal'] = str(body['preview_snap_modal'])[:12]
                    eng.trigger_open_ui()
                    return web.json_response({'ok': True, 'session_id': sid_req, 'text': '已打开历史会话预览。'})
            if body.get('clear_active'):
                eng.active_session_id = ''
            req_view = str(body.get('open_view') or '').strip().lower()
            p_boost_opt = body.get('pencil_boost') if isinstance(body.get('pencil_boost'), bool) else None
            eng.trigger_open_ui(req_view if req_view in ('phone', 'camera', 'cowrite', 'samples', 'board') else '', pencil_boost=p_boost_opt)
            return web.json_response({'ok': True, 'text': '好的，已在屏幕上打开 AI 动态黑板讲题页面。'})
        if act == 'cowrite_toggle':
            cw_t = getattr(eng, 'cowrite', None)
            if isinstance(cw_t, CoWriteMonitor):
                want_en = body.get('enabled') if isinstance(body.get('enabled'), bool) else True
                st_cw = await cw_t.toggle(enabled=want_en, subject=body.get('subject'))
                if want_en:
                    eng.trigger_open_ui('cowrite')
                return web.json_response({
                    'ok': True,
                    'cowrite': st_cw,
                    'text': '已在大屏开启静默伴写看板，孩子动笔期间零语音不打扰，写完停笔会自动在大屏给出核对提示。' if want_en else '已关闭静默伴写看板。',
                })
        if act in ('open_phone', 'phone_snap'):
            req_pg = body.get('page')
            pg_idx = 0
            if req_pg is not None and not isinstance(req_pg, bool):
                with contextlib.suppress(TypeError, ValueError):
                    pg_idx = int(req_pg)
            if pg_idx <= 0 and q:
                m_pg = re.search(r'第\s*([1-4一二三四])\s*[页张]', q)
                if m_pg:
                    pg_idx = {'一': 1, '二': 2, '三': 3, '四': 4}.get(m_pg.group(1), int(m_pg.group(1)) if m_pg.group(1).isdigit() else 0)
            if pg_idx > 0:
                eng.phone_studio.select_page_by_index(pg_idx)
        if act == 'open_phone':
            eng.trigger_open_ui('phone')
            if eng.phone_studio.has_page():
                return web.json_response({'ok': True, 'text': '已在屏幕上打开「手机拍题」整页框选台，用手指拖动绿框圈出题目点开讲即可。'})
            return web.json_response({'ok': True, 'text': '已在屏幕上打开「手机拍题」二维码页，手机开启 VPN 扫码即可拍照或选微信作业图投屏讲解。'})
        if act == 'phone_snap':
            if not eng.phone_studio.has_page():
                eng.trigger_open_ui('phone')
                return web.json_response({'ok': False, 'text': '大屏上还没有手机上传的作业图，已为您打开手机拍题二维码页，请先用手机扫码传图哦。'})
            try:
                _, raw_page = _read_regular_bytes(eng.phone_studio.stage_path, 12 * 1024 * 1024)
                # If spoken instruction specifies a numbered problem or full page, send the full page so LLM never misses a question outside last_crop!
                use_full_for_q = bool(q and re.search(r'第\s*[0-9一二三四五六七八九十]+\s*[题道]|下一[题道]|整[页张]', q))
                c_box = None if use_full_for_q else eng.phone_studio.last_crop
                jpg = await eng.phone_studio.enhance_jpeg(
                    raw_page, crop_box=c_box, rotate_deg=eng.phone_studio.rotate, crisp=True,
                )
                t_ph = 'diagnose_draft' if re.search(r'草稿|找错|哪一步错了|批改|算[得的]对不对|习作草稿|润色|升格|看看我写的', q) and not (detect_writing_task(q) and not re.search(r'批改|修改|润色|升格|看看我写的|草稿', q)) else 'standard'
                sil_ph = eng._is_cowrite_silent_active()
                sess = eng.start_live_problem(question=q, image_bytes=jpg, mode=mode, title='草稿错因诊断微课' if t_ph == 'diagnose_draft' else '手机拍题微课', source='voice_phone', open_ui=True, pedagogy=ped, teach_mode=t_ph, silent_tts=sil_ph)
                if sess and sess['id'] in eng.sessions:
                    act_pid = eng.phone_studio.active_id
                    eng.sessions[sess['id']]['stage_page_id'] = act_pid
                    sess['stage_page_id'] = act_pid
                    if c_box:
                        eng.phone_studio.record_done_crop(act_pid, sess['id'], c_box)
                eng.open_ui_target = 'board'
                return web.json_response({'ok': True, 'session_id': sess['id'], 'silent': bool(sess.get('silent_start')), 'text': '已识别刚才手机传来的题目并在大屏电子黑板启动分步启发引导（请只简短回复一句让孩子看屏幕思考闯关，切勿报出答案或重复长篇讲解）。'})
            except Exception as e:
                eng.trigger_open_ui('phone')
                return web.json_response({'ok': False, 'text': _redact_secrets(e)[:120]})
        def _consume_voice_exc(fut: asyncio.Task):
            if not fut.cancelled():
                exc_bg = fut.exception()
                if exc_bg:
                    log.warning('Background shielded voice task failed: %s', _redact_secrets(exc_bg))

        async def _await_voice_shielded(coro, soft_timeout: float = 21.5, session_work: bool = False):
            if session_work:
                eng._cancel_previous_session_work()
            bg_t = eng._spawn(coro, session_work=session_work)
            retrieved = False
            try:
                done_set, _ = await asyncio.wait({bg_t}, timeout=soft_timeout)
                if bg_t in done_set:
                    retrieved = True
                    return True, bg_t.result()
                return False, None
            finally:
                if not retrieved:
                    bg_t.add_done_callback(_consume_voice_exc)

        if act == 'mistake_retest':
            try:
                async def _do_v_retest():
                    r_mr_v = await eng.retest_mistake(mode=mode, pedagogy=ped, open_ui=True)
                    eng.open_ui_target = 'board'
                    return r_mr_v

                fin_mr_v, res_mr = await _await_voice_shielded(_do_v_retest(), session_work=True)
                if not fin_mr_v:
                    eng.trigger_open_ui('board')
                    return web.json_response({
                        'ok': True,
                        'pending': True,
                        'text': '正在从错题本挑题换数生成复测卷，几秒内自动投屏到大屏电子黑板！',
                    })
                return web.json_response({
                    'ok': True,
                    'session_id': res_mr['session']['id'],
                    'silent': bool((res_mr.get('session') or {}).get('silent_start')),
                    'text': '已从错题本挑出一道薄弱题并换数出题，快看大屏电子黑板独立闯关复测吧！',
                })
            except Exception as e:
                eng.trigger_open_ui('samples')
                return web.json_response({'ok': False, 'text': _redact_secrets(e)[:120]})
        if act in ('camera_snap', 'camera_check_page'):
            try:
                if act == 'camera_check_page' or re.search(r'先批后讲|批改整[页张]|检查整[页张]|整[页张]批改|看看这[一]?[页张]作业', q):
                    full_jpg_v = await eng.camera.capture_jpeg(high_res=True, crop_box=None, pencil_boost=True)
                    pid_v = eng.phone_studio.save_page(full_jpg_v)

                    async def _do_v_cam_chk():
                        c_v = await eng.check_staged_worksheet_page(page_id=pid_v, pencil_boost=True)
                        eng.trigger_open_ui('phone')
                        return c_v

                    fin_cc_v, chk_v = await _await_voice_shielded(_do_v_cam_chk(), session_work=False)
                    if not fin_cc_v:
                        eng.trigger_open_ui('phone')
                        return web.json_response({
                            'ok': True,
                            'pending': True,
                            'page_id': pid_v,
                            'text': '已用高拍仪抓拍整页作业，正在后台逐题巡检，几秒内自动在大屏标出批改结果！',
                        })
                    return web.json_response({
                        'ok': True,
                        'page_id': pid_v,
                        'text': f"已用高拍仪拍下整页作业并完成逐题巡检（{chk_v.get('summary', '已标出批改结果')}），请看大屏点击待订正题目开始闯关！",
                    })
                t_cam = 'diagnose_draft' if re.search(r'草稿|找错|哪一步错了|批改|算[得的]对不对|习作草稿|润色|升格|看看我写的', q) and not (detect_writing_task(q) and not re.search(r'批改|修改|润色|升格|看看我写的|草稿', q)) else 'standard'
                solo_cam = bool(body.get('solo_challenge')) if t_cam == 'standard' else False
                sil_cam = eng._is_cowrite_silent_active()
                jpg = await eng.camera.capture_jpeg(high_res=True)
                sess = eng.start_live_problem(question=q, image_bytes=jpg, mode=mode, title='草稿错因诊断微课' if t_cam == 'diagnose_draft' else '高拍仪拍题', source='voice_cam', open_ui=True, pedagogy=ped, teach_mode=t_cam, solo_challenge=solo_cam, silent_tts=sil_cam)
                eng.open_ui_target = 'board'
                return web.json_response({'ok': True, 'session_id': sess['id'], 'silent': bool(sess.get('silent_start')), 'text': '已用高拍仪拍下题目并在大屏电子黑板启动分步启发引导（请只简短回复一句让孩子看屏幕思考闯关，切勿报出答案或重复长篇讲解以免抢话）。'})
            except LookupError as e:
                eng.trigger_open_ui('camera')
                return web.json_response({'ok': False, 'text': _redact_secrets(e)})
            except Exception as e:
                eng.trigger_open_ui('camera')
                return web.json_response({'ok': False, 'text': _redact_secrets(e)})
        if act == 'dictation' or (len(q) <= 120 and re.search(r'^(?:请|帮我|开始|打开|进入|我要|来)*(?:自助)?(?:英语|语文)?(?:自助听写|报听写|报词|听写模式|听写页面|听写台)|批改.*听写本', q)):
            try:
                if re.search(r'批改|改一下|看看对不对|拍.*批', q) and eng.dictation.has_items():
                    async def _do_v_dgrade():
                        r_dg = await eng.dictation.grade_sheet(use_camera=True, pencil_boost=True)
                        eng.trigger_open_ui('dictation')
                        return r_dg

                    fin_dg_v, res_dg_v = await _await_voice_shielded(_do_v_dgrade(), session_work=False)
                    if not fin_dg_v:
                        eng.trigger_open_ui('dictation')
                        return web.json_response({'ok': True, 'pending': True, 'text': '已用高拍仪拍下听写本，正在逐词核对笔画与拼写，几秒内在大屏显示批改结果！'})
                    return web.json_response({'ok': True, 'text': f"听写本批改完成：{res_dg_v.get('summary', '请看大屏订正提示')}。"})
                cleaned_words = re.sub(r'^(?:请|帮我|开始|打开|进入|来|我要)*(?:自助)?(?:英语|语文)?(?:听写|报听写|报词|默写单词)[：:\s]*', '', q).strip()
                if cleaned_words and not re.fullmatch(r'(?:模式|页面|功能|台|吧|啦|呀|哦)+', cleaned_words):
                    st_dv = eng.dictation.start_from_text(cleaned_words)
                    eng.trigger_open_ui('dictation')
                    return web.json_response({'ok': True, 'text': f"已在大屏自助听写台准备好 {st_dv['total_count']} 个词语，点击屏幕开始播报即可听写，写完还能直接拍照秒批！"})
                eng.trigger_open_ui('dictation')
                return web.json_response({'ok': True, 'text': '已在大屏打开中英文自助听写台，可点选词库、用高拍仪拍课本词表，或用手机粘贴微信群听写词开始播报。'})
            except Exception as exc_dv:
                eng.trigger_open_ui('dictation')
                return web.json_response({'ok': False, 'text': _redact_secrets(exc_dv)[:120]})
        if q:
            cw_nl = getattr(eng, 'cowrite', None)
            if isinstance(cw_nl, CoWriteMonitor) and len(q) <= 36 and re.search(
                r'静默伴写|伴写看板|(?:开启|打开|启动|进入|关闭|停止|退出|结束|关掉|暂停|清空|切换)(?:高拍仪|作业|静默|数学|物理|化学|理化|英语|语文|古诗|全科|快检|深思|标准|模式|到|成){0,3}伴写|伴写.*(?:巡检|复查|重查|换新页|换一页|翻页|清空|错题|收录|特训|连闯|闯关|讲错题|拆解|数学|物理|化学|理化|英语|语文|古诗|全科|快检|快一点|快点|深思|慢一点|慢点)|(?:巡检|复查|重查|清空|特训|连闯|拆解).*伴写',
                q,
            ):
                def _parse_nl_cw_prefs(q_txt: str) -> tuple:
                    subj_m_nl = None
                    if re.search(r'理化|物理|化学', q_txt):
                        subj_m_nl = 'phys_chem'
                    elif re.search(r'数学', q_txt):
                        subj_m_nl = 'math'
                    elif re.search(r'英语|英文', q_txt):
                        subj_m_nl = 'english'
                    elif re.search(r'语文|古诗|文言', q_txt):
                        subj_m_nl = 'chinese'
                    elif re.search(r'全科', q_txt):
                        subj_m_nl = 'auto'
                    pace_m_nl = None
                    if re.search(r'快检|快一点|快点|快速', q_txt):
                        pace_m_nl = 'fast'
                    elif re.search(r'深思|慢一点|慢点|慢检', q_txt):
                        pace_m_nl = 'deep'
                    elif re.search(r'标准节奏|默认节奏', q_txt):
                        pace_m_nl = 'std'
                    return subj_m_nl, pace_m_nl

                nl_subj, nl_pace = _parse_nl_cw_prefs(q)
                if re.search(r'复查|重查', q):
                    try:
                        async def _do_v_rc():
                            r_rv = await cw_nl.recheck_all_warns()
                            eng.trigger_open_ui('cowrite')
                            return r_rv

                        fin_rv_v, res_rv_v = await _await_voice_shielded(_do_v_rc(), session_work=False)
                        if not fin_rv_v:
                            eng.trigger_open_ui('cowrite')
                            return web.json_response({
                                'ok': True,
                                'pending': True,
                                'cowrite': cw_nl.public_state(),
                                'text': '已在后台启动静默伴写易错题批量复查，几秒内自动同步到大屏看板。',
                            })
                        return web.json_response({
                            'ok': True,
                            'cowrite': res_rv_v.get('cowrite'),
                            'text': f"已在静默伴写看板完成易错题批量复查：{res_rv_v.get('note', '请看屏幕')}。",
                        })
                    except Exception as exc_rv:
                        eng.trigger_open_ui('cowrite')
                        return web.json_response({'ok': False, 'text': _redact_secrets(exc_rv)[:120]})
                if re.search(r'巡检', q):
                    try:
                        if nl_pace:
                            await cw_nl.toggle(enabled=None, pause_mode=nl_pace)

                        async def _do_v_ck():
                            r_ck = await cw_nl.check_now(subject=body.get('subject') or nl_subj)
                            eng.trigger_open_ui('cowrite')
                            return r_ck

                        fin_ck_v, res_ck_v = await _await_voice_shielded(_do_v_ck(), session_work=False)
                        if not fin_ck_v:
                            eng.trigger_open_ui('cowrite')
                            return web.json_response({
                                'ok': True,
                                'pending': True,
                                'cowrite': cw_nl.public_state(),
                                'text': '已在后台启动静默伴写本页核查，几秒内自动同步到大屏看板。',
                            })
                        return web.json_response({
                            'ok': True,
                            'cowrite': res_ck_v.get('cowrite'),
                            'text': f"已在静默伴写看板完成本页核查：{res_ck_v.get('note', '请看屏幕提示')}。",
                        })
                    except Exception as exc_v:
                        eng.trigger_open_ui('cowrite')
                        return web.json_response({'ok': False, 'text': _redact_secrets(exc_v)[:120]})
                if re.search(r'清空', q):
                    st_clr = cw_nl.resolve_action(clear_all=True)
                    eng.trigger_open_ui('cowrite')
                    return web.json_response({'ok': True, 'cowrite': st_clr, 'text': '已清空静默伴写看板记录，可以放入新一页作业继续书写。'})
                if re.search(r'换新页|换一页|新的一页|翻页', q):
                    st_np = cw_nl.resolve_action(new_page=True)
                    eng.trigger_open_ui('cowrite')
                    return web.json_response({'ok': True, 'cowrite': st_np, 'text': f"已切换到第 {st_np.get('page_epoch', 1)} 页作业，新页第 1 题起将重新核对。"})
                if re.search(r'记.*错题|收录.*错题|错题本', q):
                    res_em = cw_nl.enroll_mistake(enroll_all_warns=True)
                    eng.trigger_open_ui('cowrite')
                    return web.json_response({'ok': True, 'cowrite': res_em.get('cowrite'), 'text': f"已将 {res_em.get('enrolled_count', 0)} 道伴写易错题收入错题本。"})
                if re.search(r'特训|连闯|闯关', q):
                    try:
                        async def _do_v_sp():
                            r_sp = await eng.start_mistake_sprint(
                                count=3, mode='fast', pedagogy='junior',
                                solo_challenge=bool(re.search(r'独做', q)), silent_tts=True, prefer_cowrite=True,
                            )
                            eng.trigger_open_ui('board')
                            return r_sp

                        fin_sp_v, res_sp_nl = await _await_voice_shielded(_do_v_sp(), session_work=True)
                        if not fin_sp_v:
                            eng.trigger_open_ui('board')
                            return web.json_response({
                                'ok': True,
                                'pending': True,
                                'silent': True,
                                'text': '正在为大屏静默组卷初三伴写易错 3 题连闯，几秒内自动开闯。',
                            })
                        return web.json_response({
                            'ok': True,
                            'session_id': res_sp_nl['session']['id'],
                            'sprint_id': res_sp_nl.get('sprint_id'),
                            'silent': True,
                            'text': '已在大屏静默开启初三伴写易错 3 题连闯，请看屏幕独立闯关。',
                        })
                    except Exception as exc_sp_nl:
                        eng.trigger_open_ui('cowrite')
                        return web.json_response({'ok': False, 'text': _redact_secrets(exc_sp_nl)[:120]})
                if re.search(r'讲错题|讲首题|拆解', q):
                    cw_nl._ensure_loaded()
                    hit_nl = next((x for x in cw_nl.items if x.get('status') == 'warn' and not x.get('resolved')), None) or next(
                        (x for x in cw_nl.items if x.get('status') == 'warn' or x.get('starred')), None
                    )
                    if not hit_nl:
                        eng.trigger_open_ui('cowrite')
                        return web.json_response({'ok': False, 'text': '当前伴写看板还没有待拆解的错题，写完停笔会自动核对哦。'})
                    try:
                        res_ex_nl = await cw_nl.explain_item(hit_nl['id'], mode='deep', pedagogy='junior', silent=True)
                        eng.trigger_open_ui('board')
                        return web.json_response({
                            'ok': True,
                            'session_id': res_ex_nl['session']['id'],
                            'silent': True,
                            'text': f"已在大屏静默展开{hit_nl.get('no') or '待查错题'}的分步错因引导。",
                        })
                    except Exception as exc_ex_nl:
                        eng.trigger_open_ui('cowrite')
                        return web.json_response({'ok': False, 'text': _redact_secrets(exc_ex_nl)[:120]})
                has_off_kw = bool(re.search(r'关闭|停止|退出|结束|关掉|暂停', q))
                has_on_kw = bool(re.search(r'开启|打开|启动|进入', q))
                if has_off_kw:
                    want_en_q = False
                elif has_on_kw:
                    want_en_q = True
                elif (nl_subj is not None or nl_pace is not None) and cw_nl.enabled:
                    want_en_q = None
                else:
                    want_en_q = True
                st_cw_q = await cw_nl.toggle(enabled=want_en_q, subject=body.get('subject') or nl_subj, pause_mode=nl_pace)
                if want_en_q or want_en_q is None:
                    eng.trigger_open_ui('cowrite')
                if want_en_q is False:
                    nl_reply = '已关闭静默伴写看板并释放高拍仪。'
                elif want_en_q is None:
                    nl_reply = '已按要求调整静默伴写学科或停笔节奏，继续静默守护。'
                else:
                    nl_reply = '已在大屏开启静默伴写看板，写题期间不出声打扰，停笔会自动在大屏提示易错检查点。'
                return web.json_response({'ok': True, 'cowrite': st_cw_q, 'text': nl_reply})
            t_voice = 'diagnose_draft' if re.search(r'草稿|找错|哪一步错了|批改|算[得的]对不对|习作草稿|润色|升格|看看我写的', q) and not (detect_writing_task(q) and not re.search(r'批改|修改|润色|升格|看看我写的|草稿', q)) else 'standard'
            sess = eng.start_live_problem(question=q, mode=mode, source='voice', open_ui=True, pedagogy=ped, teach_mode=t_voice)
            return web.json_response({'ok': True, 'session_id': sess['id'], 'text': '已在大屏电子黑板启动分步启发引导讲题（请只简短回复一句让孩子看屏幕思考闯关，严禁说出答案或长篇复述以免与黑板老师抢话）。'})
        eng.trigger_open_ui()
        return web.json_response({'ok': True, 'text': '已打开 AI 讲题页面。'})

    async def api_cowrite_toggle(request: web.Request):
        body = await _json_dict(request)
        en_val = body.get('enabled') if isinstance(body.get('enabled'), bool) else None
        subj_val = str(body.get('subject')) if 'subject' in body and body.get('subject') is not None else None
        pb_val = body.get('pencil_boost') if isinstance(body.get('pencil_boost'), bool) else None
        pm_val = str(body.get('pause_mode')) if 'pause_mode' in body and body.get('pause_mode') is not None else None
        try:
            st = await eng.cowrite.toggle(enabled=en_val, subject=subj_val, pencil_boost=pb_val, pause_mode=pm_val)
            return web.json_response({'ok': True, 'cowrite': st})
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)

    async def api_cowrite_check_now(request: web.Request):
        body = await _json_dict(request)
        subj_val = str(body.get('subject')) if 'subject' in body and body.get('subject') is not None else None
        p_boost = body.get('pencil_boost') if isinstance(body.get('pencil_boost'), bool) else None
        pid_val = str(body.get('page_id') or '').strip()
        img_b64 = str(body.get('image_b64') or '').strip()
        raw_img = None
        if img_b64:
            if ',' in img_b64[:64]:
                img_b64 = img_b64.split(',', 1)[1]
            try:
                raw_img = base64.b64decode(img_b64)
            except Exception:
                return web.json_response({'ok': False, 'error': '图片编码无效'}, status=400)
            if len(raw_img) < 128 or len(raw_img) > 6 * 1024 * 1024:
                return web.json_response({'ok': False, 'error': '图片大小不合法'}, status=400)
        try:
            kw_cw_ck = {'subject': subj_val, 'image_bytes': raw_img, 'pencil_boost': p_boost}
            if pid_val:
                kw_cw_ck['stage_page_id'] = pid_val
            res = await eng.cowrite.check_now(**kw_cw_ck)
            return web.json_response(res)
        except LookupError as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=404)
        except (ValueError, RuntimeError) as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=500)

    async def api_cowrite_recheck_item(request: web.Request):
        body = await _json_dict(request, allow_empty=False)
        iid = str(body.get('id') or '').strip()
        p_boost = body.get('pencil_boost') if isinstance(body.get('pencil_boost'), bool) else None
        rec_all = bool(body.get('recheck_all')) or (iid == '__all__')
        img_b64 = str(body.get('image_b64') or '').strip()
        raw_img = None
        if img_b64:
            if ',' in img_b64[:64]:
                img_b64 = img_b64.split(',', 1)[1]
            try:
                raw_img = base64.b64decode(img_b64)
            except Exception:
                return web.json_response({'ok': False, 'error': '图片编码无效'}, status=400)
            if len(raw_img) < 128 or len(raw_img) > 6 * 1024 * 1024:
                return web.json_response({'ok': False, 'error': '图片大小不合法'}, status=400)
        try:
            res = await (
                eng.cowrite.recheck_all_warns(image_bytes=raw_img, pencil_boost=p_boost)
                if rec_all else
                eng.cowrite.recheck_item(iid, image_bytes=raw_img, pencil_boost=p_boost)
            )
            return web.json_response(res)
        except LookupError as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=404)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)

    async def api_cowrite_check_crop(request: web.Request):
        body = await _json_dict(request, allow_empty=False)
        c_box = body.get('crop') if isinstance(body.get('crop'), dict) else body.get('crop_box')
        no_h = str(body.get('no') or body.get('question') or '').strip()[:16]
        subj_v = str(body.get('subject')) if 'subject' in body and body.get('subject') is not None else None
        pid_v = str(body.get('page_id') or '').strip()
        p_boost = body.get('pencil_boost') if isinstance(body.get('pencil_boost'), bool) else None
        img_b64 = str(body.get('image_b64') or '').strip()
        raw_img = None
        if img_b64:
            if ',' in img_b64[:64]:
                img_b64 = img_b64.split(',', 1)[1]
            try:
                raw_img = base64.b64decode(img_b64)
            except Exception:
                return web.json_response({'ok': False, 'error': '图片编码无效'}, status=400)
            if len(raw_img) < 128 or len(raw_img) > 6 * 1024 * 1024:
                return web.json_response({'ok': False, 'error': '图片大小不合法'}, status=400)
        try:
            res = await eng.cowrite.check_crop(
                crop_box=c_box, no_hint=no_h, subject=subj_v, page_id=pid_v, image_bytes=raw_img, pencil_boost=p_boost,
            )
            return web.json_response(res)
        except LookupError as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=404)
        except ValueError as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=500)

    async def api_cowrite_variant(request: web.Request):
        body = await _json_dict(request, allow_empty=False)
        iid = str(body.get('id') or '').strip()
        mode = _norm_mode(body.get('mode') or 'fast')
        silent = bool(body.get('silent', True))
        solo = bool(body.get('solo_challenge'))
        try:
            res = await eng.cowrite.variant_from_item(iid, mode=mode, silent=silent, solo_challenge=solo)
            return web.json_response(res)
        except LookupError as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=404)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)

    async def api_cowrite_resolve(request: web.Request):
        body = await _json_dict(request)
        try:
            has_star_b = isinstance(body.get('star'), bool)
            res_v = body['resolved'] if isinstance(body.get('resolved'), bool) else (None if has_star_b else True)
            st = eng.cowrite.resolve_action(
                item_id=str(body.get('id') or ''),
                resolved=res_v,
                clear_all=bool(body.get('clear_all')),
                new_page=bool(body.get('new_page')),
                star=(body['star'] if has_star_b else None),
            )
            return web.json_response({'ok': True, 'cowrite': st})
        except LookupError as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:120]}, status=404)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:120]}, status=400)

    async def api_cowrite_enroll_mistake(request: web.Request):
        body = await _json_dict(request)
        try:
            res = eng.cowrite.enroll_mistake(
                item_id=str(body.get('id') or ''),
                enroll_all_warns=bool(body.get('enroll_all_warns')),
            )
            return web.json_response(res)
        except LookupError as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:120]}, status=404)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:120]}, status=400)

    async def api_cowrite_explain(request: web.Request):
        body = await _json_dict(request, allow_empty=False)
        iid = str(body.get('id') or '').strip()
        mode = _norm_mode(body.get('mode') or 'deep')
        ped = _norm_pedagogy(body.get('pedagogy'))
        silent = bool(body.get('silent', True))
        try:
            res = await eng.cowrite.explain_item(iid, mode=mode, pedagogy=ped, silent=silent)
            return web.json_response(res)
        except LookupError as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=404)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)

    async def api_dictation_start(request: web.Request):
        body = await _json_dict(request, allow_empty=False)
        try:
            iv = body.get('interval_sec')
            rp = body.get('repeat_count')
            em = body.get('en_mode')
            ttl = str(body.get('title') or '').strip()
            if body.get('from_camera'):
                st = await eng.dictation.extract_and_start(
                    use_camera=True, title=ttl, interval_sec=iv, repeat_count=rp, en_mode=em,
                )
            elif body.get('image_b64'):
                raw_s = str(body.get('image_b64') or '').strip()
                if ',' in raw_s[:64]:
                    raw_s = raw_s.split(',', 1)[1]
                st = await eng.dictation.extract_and_start(
                    image_bytes=base64.b64decode(raw_s), title=ttl, interval_sec=iv, repeat_count=rp, en_mode=em,
                )
            elif body.get('pack_id'):
                st = eng.dictation.start_from_pack(str(body['pack_id']), interval_sec=iv, repeat_count=rp, en_mode=em)
            else:
                st = eng.dictation.start_from_text(
                    str(body.get('text') or body.get('words') or ''), title=ttl,
                    interval_sec=iv, repeat_count=rp, en_mode=em,
                )
            if body.get('open_ui'):
                eng.trigger_open_ui('dictation')
            return web.json_response({'ok': True, 'dictation': st})
        except LookupError as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=404)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)

    async def api_dictation_control(request: web.Request):
        body = await _json_dict(request, allow_empty=False)
        try:
            st = eng.dictation.control(str(body.get('action') or ''), body)
            return web.json_response({'ok': True, 'dictation': st})
        except LookupError as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=404)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)

    async def api_dictation_tts(request: web.Request):
        try:
            idx = int(request.query.get('idx', '0'))
            pass_no = int(request.query.get('pass', '1'))
        except ValueError:
            raise web.HTTPBadRequest()
        try:
            spoken = eng.dictation.spoken_text_for(idx, pass_no)
        except LookupError:
            raise web.HTTPNotFound()
        wav = await eng.synthesize_wav(spoken, priority=True)
        cc_hdr = 'no-store' if wav == _FALLBACK_SILENT_WAV else 'private, max-age=600'
        return web.Response(body=wav, content_type='audio/wav', headers={'Cache-Control': cc_hdr})

    async def api_dictation_grade(request: web.Request):
        body = await _json_dict(request)
        use_cam = bool(body.get('use_camera', not body.get('image_b64')))
        p_boost = bool(body.get('pencil_boost', True))
        img_bytes = None
        if body.get('image_b64'):
            raw_s = str(body.get('image_b64') or '').strip()
            if ',' in raw_s[:64]:
                raw_s = raw_s.split(',', 1)[1]
            try:
                img_bytes = base64.b64decode(raw_s)
            except Exception:
                return web.json_response({'ok': False, 'error': '图片编码无效'}, status=400)
        try:
            st = await eng.dictation.grade_sheet(image_bytes=img_bytes, use_camera=use_cam, pencil_boost=p_boost)
            return web.json_response({'ok': True, 'dictation': st, 'mistakes': eng.public_mistakes(), 'mistake_summary': eng.public_mistake_summary()})
        except LookupError as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=404)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)

    async def api_dictation_enroll_mistakes(request: web.Request):
        body = await _json_dict(request)
        idx_val = None
        if body.get('idx') is not None and not isinstance(body.get('idx'), bool):
            try:
                idx_val = int(body['idx'])
            except (TypeError, ValueError):
                return web.json_response({'ok': False, 'error': '序号无效'}, status=400)
        try:
            res = eng.dictation.enroll_mistakes(idx=idx_val)
            res['mistakes'] = eng.public_mistakes()
            return web.json_response(res)
        except LookupError as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=404)
        except Exception as e:
            return web.json_response({'ok': False, 'error': _redact_secrets(e)[:160]}, status=400)

    async def _opt_ok(_request: web.Request):
        return web.Response(status=204)

    app.router.add_get('/', serve_index)
    app.router.add_get('/api/tutor/state', api_state)
    app.router.add_get('/api/tutor/badge', api_badge)
    app.router.add_get('/api/tutor/camera/frame.jpg', api_cam_frame)
    app.router.add_post('/api/tutor/camera/rotate', api_cam_rotate)
    app.router.add_post('/api/tutor/camera/refocus', api_cam_refocus)
    app.router.add_post('/api/tutor/camera/ctrl', api_cam_ctrl)
    app.router.add_post('/api/tutor/camera/stage_page', api_cam_stage_page)
    app.router.add_post('/api/tutor/cowrite/toggle', api_cowrite_toggle)
    app.router.add_post('/api/tutor/cowrite/check_now', api_cowrite_check_now)
    app.router.add_post('/api/tutor/cowrite/check_crop', api_cowrite_check_crop)
    app.router.add_post('/api/tutor/cowrite/recheck_item', api_cowrite_recheck_item)
    app.router.add_post('/api/tutor/cowrite/variant', api_cowrite_variant)
    app.router.add_post('/api/tutor/cowrite/resolve', api_cowrite_resolve)
    app.router.add_post('/api/tutor/cowrite/enroll_mistake', api_cowrite_enroll_mistake)
    app.router.add_post('/api/tutor/cowrite/explain', api_cowrite_explain)
    app.router.add_post('/api/tutor/dictation/start', api_dictation_start)
    app.router.add_post('/api/tutor/dictation/control', api_dictation_control)
    app.router.add_get('/api/tutor/dictation/tts', api_dictation_tts)
    app.router.add_post('/api/tutor/dictation/grade', api_dictation_grade)
    app.router.add_post('/api/tutor/dictation/enroll_mistakes', api_dictation_enroll_mistakes)
    app.router.add_get('/api/tutor/phone_qr.svg', api_phone_qr_svg)
    app.router.add_get('/api/tutor/phone_stage/page.jpg', api_phone_stage_page)
    app.router.add_post('/api/tutor/phone_stage/rotate', api_phone_stage_rotate)
    app.router.add_post('/api/tutor/phone_stage/select', api_phone_stage_select)
    app.router.add_post('/api/tutor/phone_stage/clear', api_phone_stage_clear)
    app.router.add_post('/api/tutor/phone_stage/check_page', api_phone_stage_check_page)
    app.router.add_get('/api/tutor/mobile_asset/{name}', api_mobile_asset)
    app.router.add_get('/api/tutor/mobile/state', api_mobile_state)
    app.router.add_get('/api/tutor/mobile/page.jpg', api_mobile_page_jpg)
    app.router.add_post('/api/tutor/mobile/upload', api_mobile_upload)
    app.router.add_get('/api/tutor/snap/{name}', api_snap_image)
    app.router.add_get('/api/tutor/session', api_session)
    app.router.add_get('/api/tutor/tts', api_tts)
    app.router.add_get('/api/tutor/tts_hint', api_tts_hint)
    app.router.add_get('/api/tutor/orient', api_orient)
    app.router.add_post('/api/tutor/progress', api_progress)
    app.router.add_post('/api/tutor/start', api_start)
    app.router.add_post('/api/tutor/variant', api_variant)
    app.router.add_post('/api/tutor/history/delete', api_history_delete)
    app.router.add_get('/api/tutor/mistakes', api_mistakes)
    app.router.add_post('/api/tutor/mistakes/retest', api_mistake_retest)
    app.router.add_post('/api/tutor/mistakes/delete', api_mistake_delete)
    app.router.add_post('/api/tutor/mistakes/sprint_start', api_mistake_sprint_start)
    app.router.add_post('/api/tutor/mistakes/sprint_next', api_mistake_sprint_next)
    app.router.add_post('/api/tutor/alt_method', api_alt_method)
    app.router.add_post('/api/tutor/foundation', api_foundation)
    app.router.add_post('/api/tutor/exam_rubric', api_exam_rubric)
    app.router.add_post('/api/tutor/polya_simplify', api_polya_simplify)
    app.router.add_post('/api/tutor/archetype_map', api_archetype_map)
    app.router.add_post('/api/tutor/read_aloud', api_read_aloud)
    app.router.add_post('/api/tutor/check_scratch', api_check_scratch)
    app.router.add_post('/api/tutor/subquestion/next', api_subquestion_next)
    app.router.add_post('/api/tutor/voice_record', api_voice_record)
    app.router.add_post('/api/tutor/ask', api_ask)
    app.router.add_post('/api/tutor/feynman', api_feynman)
    app.router.add_post('/api/tutor/phone_upload', api_phone_upload)
    app.router.add_post('/api/tutor/tool', api_tool)
    app.router.add_route('OPTIONS', '/api/tutor/badge', _opt_ok)
    app[_TUTOR_ENGINE_KEY] = eng
    return app


def _systemd_listen_socket():
    """Return non-blocking listening socket passed by systemd socket activation (fd=3), else None."""
    try:
        pid_ok = os.environ.get('LISTEN_PID') == str(os.getpid())
        fds = int(os.environ.get('LISTEN_FDS', '0') or '0')
    except ValueError:
        return None
    if not pid_ok or fds < 1:
        return None
    os.environ.pop('LISTEN_PID', None)
    os.environ.pop('LISTEN_FDS', None)
    os.environ.pop('LISTEN_FDNAMES', None)
    sock = socket.socket(family=socket.AF_INET, type=socket.SOCK_STREAM, fileno=3)
    sock.setblocking(False)
    return sock


def _tutor_busy(eng: TutorEngine) -> bool:
    cw_b = getattr(eng, 'cowrite', None)
    if (
        any(not t.done() for t in eng.bg_tasks)
        or getattr(eng, '_inflight_reqs', 0) > 0
        or bool(eng._page_check_inflight)
        or bool(eng.recorder.is_recording)
        or (eng.camera._stream_proc is not None)
        or (isinstance(cw_b, CoWriteMonitor) and (bool(cw_b.enabled) or cw_b._pipeline_lock.locked()))
        or (isinstance(getattr(eng, 'dictation', None), DictationManager) and eng.dictation.is_busy())
    ):
        return True
    if eng.is_lesson_in_progress() and (time.monotonic() - getattr(eng, 'last_active_mono', 0.0)) < 1800.0:
        return True
    return False


def _save_tutor_active_pref(eng: TutorEngine):
    with contextlib.suppress(Exception):
        eng._ensure_history_loaded()
        act_sid = str(getattr(eng, 'active_session_id', '') or '').strip()
        if act_sid:
            act_sess = eng.sessions.get(act_sid)
            if isinstance(act_sess, dict) and not act_sess.get('silent_preview') and not str(act_sid).startswith('m:'):
                eng._save_to_history(act_sess)
        lp = PREFS_PATH.with_name(f'.{PREFS_PATH.name}.lock')
        PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lp), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            prefs = _read_safe_json(PREFS_PATH, {})
            if not isinstance(prefs, dict):
                prefs = {}
            if prefs.get('active_session_id', '') != act_sid:
                if act_sid:
                    prefs['active_session_id'] = act_sid
                else:
                    prefs.pop('active_session_id', None)
                _atomic_write_json(PREFS_PATH, prefs)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


async def _serve_tutor():
    sd_sock = _systemd_listen_socket()
    app = make_app()
    eng: TutorEngine = app[_TUTOR_ENGINE_KEY]
    runner = web.AppRunner(app, access_log=None, handle_signals=False)
    await runner.setup()
    stop_ev = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_ev.set)

    idle_task = None
    try:
        if sd_sock is not None:
            site = web.SockSite(runner, sd_sock, shutdown_timeout=5.0)
            await site.start()
            idle_sec = max(0, int(os.environ.get('TUTOR_IDLE_EXIT_SEC', '420') or '420'))
            logging.info('tutor socket-activated mode (idle auto-exit=%ds)', idle_sec)
            if idle_sec > 0:
                async def _idle_watch():
                    while not stop_ev.is_set():
                        await asyncio.sleep(min(15.0, max(0.5, idle_sec / 3.0)))
                        if _tutor_busy(eng):
                            eng.last_active_mono = time.monotonic()
                            continue
                        if time.monotonic() - eng.last_active_mono >= idle_sec:
                            logging.info('tutor idle for %ds, parking cleanly until next request', idle_sec)
                            _save_tutor_active_pref(eng)
                            stop_ev.set()
                            return
                idle_task = asyncio.create_task(_idle_watch())
        else:
            site = web.TCPSite(runner, host='127.0.0.1', port=PORT, reuse_address=True, shutdown_timeout=5.0)
            await site.start()
        await stop_ev.wait()
    finally:
        if sd_sock is not None:
            _save_tutor_active_pref(eng)
        if idle_task is not None:
            idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await idle_task
        await runner.cleanup()


if __name__ == '__main__':
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s tutor: %(message)s')
    STATE_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    asyncio.run(_serve_tutor())
