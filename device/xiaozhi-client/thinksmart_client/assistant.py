"""Settings page, 「小麦」: the wake words, how easily they fire and how long to keep listening.

The client owns these (config.json). A change takes effect at once: app.Client builds a new keyword
spotter (kws.py) in a worker thread and swaps it in, no restart.
"""

# the wake words that were tested on this device (config.DEFAULTS has the reasons); a word outside
# this list can stay if it is already in config.json, but can't be added from the page
PRESETS = (
    ('n ǐ h ǎo x iǎo zh ì @你好小智', '你好小智'),
    ('x iǎo m ài x iǎo m ài @小麦小麦', '小麦小麦'),
    ('x iǎo m ài t óng x ué @小麦同学', '小麦同学'),
)
# name -> (lowest, highest, decimals)
LIMITS = {
    'kws_threshold': (0.10, 0.60, 2),
    'kws_score': (1.0, 4.0, 1),
    'kws_gain_db': (0, 24, 0),
    'kws_paths': (4, 16, 0),
    'follow_up_seconds': (0, 30, 0),
}
LABELS = {'kws_threshold': '唤醒灵敏度', 'kws_score': '唤醒词加权', 'kws_gain_db': '远场增益',
          'kws_paths': '搜索宽度', 'follow_up_seconds': '回答完继续听'}
WAKE_SOUNDS = ('zai', 'chime', 'off')   # 叮+「在」, 只叮一声, 不出声
REBUILD = ('wake_words', 'kws_threshold', 'kws_score', 'kws_paths')   # these need a new keyword spotter


def label_of(word):
    return word.split('@', 1)[1].strip() if '@' in word else word.strip()


def _num(cfg, key):
    lo, hi, digits = LIMITS[key]
    try:
        v = float(cfg.get(key))
    except (TypeError, ValueError):
        v = lo
    v = max(lo, min(hi, v))
    return round(v, digits) if digits else int(v)


def view(cfg):
    """What the settings page shows."""
    on = [w for w in cfg.get('wake_words') or [] if isinstance(w, str) and w.strip()]
    words = list(PRESETS) + [(w, label_of(w)) for w in on if w not in [p[0] for p in PRESETS]]
    return {'wake_words': [{'raw': raw, 'label': label, 'on': raw in on} for raw, label in words],
            'wake_verify': str(cfg.get('wake_verify') or '') == 'hub',
            'wake_sound': cfg.get('wake_sound') if cfg.get('wake_sound') in WAKE_SOUNDS else 'zai',
            'limits': {k: list(v[:2]) for k, v in LIMITS.items()},
            **{k: _num(cfg, k) for k in LIMITS}}


def normalize(cfg, changes, extra_known=None):
    """{config key: value} of what really changes. Raises ValueError with a sentence for the page."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError('没有要保存的内容')
    out = {}
    for key, v in changes.items():
        if key == 'wake_words':
            known = (
                [p[0] for p in PRESETS]
                + [w for w in (cfg.get('wake_words') or []) if isinstance(w, str)]
                + [w for w in (extra_known or []) if isinstance(w, str)]
            )
            if not isinstance(v, list) or any(w not in known for w in v):
                raise ValueError('唤醒词只能从列表里选')
            words = [w for i, w in enumerate(v) if w not in v[:i]]
            if not words:
                raise ValueError('至少要留一个唤醒词')
            out[key] = words
        elif key == 'wake_verify':
            if not isinstance(v, bool):
                raise ValueError('唤醒二次确认要是开或关')
            out[key] = 'hub' if v else ''
        elif key == 'wake_sound':
            if v not in WAKE_SOUNDS:
                raise ValueError('没有这个提示音')
            out[key] = v
        elif key in LIMITS:
            lo, hi, digits = LIMITS[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
                raise ValueError(f'{LABELS[key]}要是数字')
            if not lo <= v <= hi:
                raise ValueError(f'{LABELS[key]}要在 {lo}～{hi} 之间')
            out[key] = round(float(v), digits) if digits else int(v)
        else:
            raise ValueError(f'不支持的设置项：{key}')
    return {k: v for k, v in out.items() if v != cfg.get(k)}
