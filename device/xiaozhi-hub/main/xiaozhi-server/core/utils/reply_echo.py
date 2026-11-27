"""回声识别过滤：丢掉把设备自己的回复识别成"用户说话"的结果。

开着回声消除的设备（hello 里带 aec）在回复播放时也开着麦克风（realtime 模式），方便用户随时插话。
残留的回声很轻，但语音识别有时仍会把它认成回复里的一小段（例如"也是。"），被当成用户插话，把回复打断。

做法：回复播放期间和播完后几秒内，识别结果里大部分字都出现在这次回复里，就当回声丢掉。
只看字，不看声音大小：回声的音量和用户离设备远近、音量设置都有关，定不出一个可靠的门限。

[ThinkSmart 本地改动] 播完之后才说的话不算回声：喇叭停了，回声最多再拖约 0.4 秒。这句话最后一个有声帧
比播完时刻晚 AFTER_END_USER 秒以上，说明是人在说话（例如新闻结尾问「想听详细内容吗」，
用户马上回答「详细内容」：字全在回复里，以前会被当回声丢掉，第一次说总是没反应）。
播放期间的过滤不变。
"""

import re
import time
from difflib import SequenceMatcher

import numpy as np

# 最后一段回复音频发出后还算"回复期间"的秒数：设备还要把缓冲播完，回声约 0.4 秒后到麦克风，
# VAD 又要等 0.8 秒安静才把这句交给识别
WINDOW_AFTER = 4.0
MIN_SHARE = 0.6  # 识别出的字里，至少这么大比例也出现在回复里，才算回声
AFTER_END_USER = 0.8  # 最后一个有声帧比回复播完晚这么多秒，就是用户在说话，不是回声
_NON_WORD = re.compile(r"[\W_]+")


def _state(conn) -> dict:
    st = getattr(conn, "reply_echo", None)
    if st is None:
        st = {"sid": None, "text": "", "at": 0.0}
        conn.reply_echo = st
    return st


def note_reply(conn, text) -> None:
    """一句回复正在发给设备（text 为 None 表示这轮的音频发完了，只更新时间）。"""
    st = _state(conn)
    if st["sid"] != conn.sentence_id:  # 新的一轮回复：忘掉上一轮的（旧轮次尾包的 None 不冲掉新状态）
        if not text:
            return
        st.update(sid=conn.sentence_id, text="", done_wall=None)
    if text:
        st["text"] += _NON_WORD.sub("", str(text))
        if len(st["text"]) > 4096:
            st["text"] = st["text"][-4096:]
        st["done_wall"] = None
    elif not st["text"]:
        return
    else:
        st["done_wall"] = time.time()  # 这轮音频估计播完的时刻（和 VAD 一样用墙钟）
    st["at"] = time.monotonic()


def during_reply(conn) -> bool:
    st = _state(conn)
    return bool(st["text"]) and (conn.client_is_speaking or time.monotonic() - st["at"] <= WINDOW_AFTER)


def is_echo(conn, text) -> bool:
    """回复期间识别出的这句话是不是回复自己的回声。"""
    if not during_reply(conn):
        return False
    heard = _NON_WORD.sub("", str(text))
    if not heard:
        return False
    done = _state(conn).get("done_wall")
    last_voice = (getattr(conn, "vad_last_voice_time", 0) or 0) / 1000.0
    if done and not conn.client_is_speaking and last_voice - done > AFTER_END_USER:
        return False  # 喇叭停了之后还在说：是人
    blocks = SequenceMatcher(None, heard, _state(conn)["text"], autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks) / len(heard) >= MIN_SHARE


def level_db(frames) -> float:
    """这句话里最响的 60ms 的音量（16kHz PCM 帧），单位 dBFS。只用于日志，方便调参时对照。"""
    raw = b"".join(frames)
    raw = raw[:len(raw) & ~1]
    if not raw:
        return -120.0
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    step = 960
    rms = [np.sqrt(np.mean(pcm[i:i + step] ** 2)) for i in range(0, len(pcm), step)]
    return float(20 * np.log10(max(max(rms), 1e-6)))
