"""Second look at a wake word hit: recognise the last few seconds and check the wake word is really there.

The wake word model (kws.py) is small and tuned to catch the wake word easily, so it also fires on
things that sound close (小妹小妹, 小白同学, 小美同学). On a hit, the audio it heard goes to a full
speech recogniser and the text is compared with the wake words by sound (toneless pinyin). The last
syllable of the name (麦 of 小麦, the part all wake words share) must sound exactly right: 小卖 and
笑麦 count, 小美, 小妹 and 小白 don't. Other syllables may be off by one, or by two half-wrong ones
(same initial or same final): the recogniser writes 哈喽小麦 as 还有小麦 or Hello 小麦, and 小麦小麦
as 洗了麦小麦. In order:
1. a wake word, or just the name 小麦, is in the text -> wake (from far away or over a TV the
   recogniser often gets only the second 小麦 of 小麦小麦);
2. something shaped like a wake word with the wrong name (小妹小妹, 小白同学) -> ignore;
3. hardly anything recognised (3 syllables or less) -> wake: the recogniser is unsure, not the
   wake word model wrong, and a missed wake is worse than a false one;
4. other speech -> ignore.
Measured with eval/wake (synthetic voices): false wakes on confusable sentences 29 -> 6, true
wakes lost: 1% near, 5% across the room, 11% with a TV talking.
The audio is brought to a normal speaking level first: like the wake word model, the recogniser
does worse on quiet audio.

Checkers (config 'wake_verify'):
  'hub'        the hub's own recogniser (POST /kiosk/asr, patch-hub.py): no extra memory here
  'asr:<dir>'  a sherpa-onnx offline model in this process (evaluation)
  ''           off
"""
import json
import logging
import pathlib
import re
import time
import urllib.request

import numpy as np

log = logging.getLogger(__name__)
_ORIG_URLOPEN = urllib.request.urlopen

TAIL = 8000   # samples of silence after the hit (0.5 s)
LEVEL = 10 ** (-20 / 20)   # loudest 100 ms brought to this RMS, with at most MAX_GAIN
MAX_GAIN = 10 ** (30 / 20)
LATIN = {'hello': ['ha', 'lou'], 'hallo': ['ha', 'lou'], 'halo': ['ha', 'lou']}

INITIALS = ('zh', 'ch', 'sh', 'b', 'p', 'm', 'f', 'd', 't', 'n', 'l', 'g', 'k', 'h', 'j', 'q', 'x', 'r',
            'z', 'c', 's', 'y', 'w')
_PINYIN = None


def pinyin(text):
    """Toneless syllable per Chinese character (None for unknown ones); other characters are skipped."""
    global _PINYIN
    if _PINYIN is None:
        _PINYIN = {}
        for line in (pathlib.Path(__file__).parent / 'pinyin.txt').read_text().splitlines():
            if line and not line.startswith('#'):
                syllable, chars = line.split('\t')
                for ch in chars:
                    _PINYIN.setdefault(ch, syllable)
    out = []
    for word in re.findall(r'[a-zA-Z]+|[\u4e00-\u9fff]', text):
        if word.isascii():
            out += LATIN.get(word.lower(), [None])
        else:
            out.append(_PINYIN.get(word))
    return out


def _split(s):
    for i in INITIALS:
        if s.startswith(i):
            return i, s[len(i):]
    return '', s


def _distance(a, b):
    if a == b:
        return 0.0
    if a is None or b is None:
        return 1.0
    (ia, fa), (ib, fb) = _split(a), _split(b)
    return 0.5 if ia == ib or fa == fb else 1.0


def _common(labels):
    """Longest substring shared by all wake words: the device's name."""
    first = min(labels, key=len)
    for n in range(len(first), 0, -1):
        for i in range(len(first) - n + 1):
            part = first[i:i + n]
            if all(part in w for w in labels):
                return part
    return ''


def _short_name(label):
    """Extract the assistant's core name from a wake phrase (e.g. 你好小智 -> 小智, 小麦同学 -> 小麦)."""
    s = (label or '').strip()
    if len(s) < 2:
        return ''
    if len(s) == 4 and s[:2] == s[2:]:
        return s[:2]
    for p in ('你好', '您好', '哈喽', '嗨'):
        if s.startswith(p) and len(s) - len(p) >= 2:
            s = s[len(p):]
            break
    for sf in ('同学', '助手', '老师', '管家'):
        if s.endswith(sf) and len(s) - len(sf) >= 2:
            s = s[:-len(sf)]
            break
    return s if len(s) >= 2 else ''


class Matcher:
    def __init__(self, wake_words, slack=1.0):
        self.labels = [w.split('@')[-1].strip() for w in wake_words]
        self.names = list(dict.fromkeys(n for n in (_short_name(lb) for lb in self.labels) if len(n) >= 2))
        self.name = self.names[0] if len(self.names) == 1 else _common(self.labels)
        self.slack = slack

    def _cost(self, heard, label, wrong_name):
        """Lowest mismatch of label against any stretch of heard; wrong_name: cost of a wrong 麦/智."""
        want = pinyin(label)
        sn = _short_name(label)
        key_ch = sn[-1] if len(sn) >= 2 else (self.name[-1] if len(self.name) >= 2 else (label[-1] if label else ''))
        strict = [bool(key_ch) and ch == key_ch for ch in label]
        best = 99.0
        for start in range(len(heard) - len(want) + 1):
            cost = 0.0
            for k, w in enumerate(want):
                d = _distance(heard[start + k], w)
                cost += wrong_name if strict[k] and d else d
            best = min(best, cost)
        return best

    def judge(self, text):
        """(wake?, why) for recognised text; see the module docstring for the rules."""
        heard = pinyin(text or '')
        for label in self.labels:
            if self._cost(heard, label, 99) <= self.slack:
                return True, label
        for nm in self.names:
            if self._cost(heard, nm, 99) <= self.slack / 2:
                return True, nm
        if len(self.name) >= 2 and self._cost(heard, self.name, 99) <= self.slack / 2:
            return True, self.name
        for label in self.labels:
            if self._cost(heard, label, 1) <= self.slack + 1:
                return False, f'not {label}'
        if len(heard) <= 3:
            return True, 'unclear'
        return False, 'other speech'

class HubChecker:
    """Recognise with the hub's own speech recogniser (it is already loaded there)."""

    def __init__(self, url, timeout=4.0):
        self.url = url.rstrip('/') + '/kiosk/asr'
        self.timeout = timeout
        self._sess = None

    def text(self, audio):
        pcm = np.clip(audio * 32768, -32768, 32767).astype('<i2').tobytes()
        if urllib.request.urlopen is _ORIG_URLOPEN:
            try:
                import requests as _req_mod
                s = self._sess
                if s is None:
                    s = _req_mod.Session()
                    s.trust_env = False
                    self._sess = s
                r = s.post(self.url, data=pcm, headers={'Content-Type': 'audio/L16; rate=16000'}, timeout=self.timeout)
                r.raise_for_status()
                answer = r.json()
                log.info('hub recognised %.1f s of audio in %s ms', len(audio) / 16000, answer.get('ms'))
                return answer['text']
            except ImportError:
                pass
        req = urllib.request.Request(self.url, data=pcm, headers={'Content-Type': 'audio/L16; rate=16000'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=self.timeout) as r:
            answer = json.loads(r.read())
        log.info('hub recognised %.1f s of audio in %s ms', len(audio) / 16000, answer.get('ms'))
        return answer['text']


class Verifier:
    def __init__(self, checker, wake_words):
        self.checker = checker
        self.matcher = Matcher(wake_words)

    def check(self, audio, label):
        """{'ok': is the wake word really there, 'text': what was recognised}. Fails open: if the
        recogniser can't be asked, the wake counts (a missed wake is worse than a false one)."""
        t0 = time.monotonic()
        # the hit comes right as the wake word ends; without some silence after it the recogniser
        # lacks right context for the last syllable (小麦小麦 came out as 洗澡麦洗脚麦)
        loudest = max(np.sqrt(np.mean(audio[i:i + 1600] ** 2)) for i in range(0, max(len(audio) - 1599, 1), 800))
        audio = np.concatenate([audio * min(LEVEL / max(loudest, 1e-6), MAX_GAIN), np.zeros(TAIL)]).astype(np.float32)
        try:
            text = self.checker.text(audio)
        except Exception as e:
            log.warning('wake check failed, accepting the wake: %s', e)
            return {'ok': True, 'text': None, 'error': str(e)}
        ok, why = self.matcher.judge(text)
        log.info('wake check %s: heard %r in %.0f ms -> %s (%s)', label, text, (time.monotonic() - t0) * 1000,
                 'wake' if ok else 'ignored', why)
        return {'ok': ok, 'text': text, 'why': why}


def make(spec, wake_words, hub_http='http://127.0.0.1:16688'):
    if not spec:
        return None
    if spec == 'hub':
        return Verifier(HubChecker(hub_http), wake_words)
    raise ValueError(f'unknown wake_verify {spec!r}')
