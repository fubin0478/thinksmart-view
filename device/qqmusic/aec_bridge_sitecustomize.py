"""Non-invasive runtime AEC & MCP bridge for standalone thinksmart-qqmusic.service.

Loaded via PYTHONPATH=/usr/local/lib/thinksmart-aec-bridge drop-in; does not modify any
file inside /opt/thinksmart/client/, so XiaoZhi OTA updates (thinksmart-update) work cleanly.

XiaoZhi is developed by someone else and updates on its own, so this bridge must never break it:
- every hook passes the original call through untouched and swallows its own errors;
- if the audio hook fails even once it removes itself and closes the music socket, so
  thinksmart-qqmusic falls back to plain PipeWire playback (music keeps working, only the
  echo cancellation while music plays is lost);
- the music socket is only connected after the audio hook is installed, otherwise the music
  would be fed into a queue nobody reads (silence).
Messages go to the thinksmart-client journal with the prefix [qqmusic-bridge].
"""
import asyncio
import json
import math
import os
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import weakref

SOCK_PATH = '/run/user/1000/qqmusic-aec.sock'
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
FLUSH_MAGIC = b'QQM_FLUSH_BUFFER'
_pcm_q = queue.Queue(maxsize=12)  # 240ms max buffer for tight low-latency sync
_flush_seq = 0
_audio_hooked = threading.Event()  # the socket reader waits for this
_disabled = threading.Event()      # set when the audio hook had to remove itself
_speaker_held = threading.Event()  # set while XiaoZhi holds hardware sink at alarm_volume
_hold_meta = {'pending': 0, 'active': False, 'client': None}


STATUS_PATH = '/run/user/1000/qqmusic-bridge.json'
_status = {'audio_hook': False, 'voice_tools': False, 'disabled': False, 'note': ''}
_status_lock = threading.Lock()


def _log(msg: str) -> None:
    try:
        print(f'[qqmusic-bridge] {msg}', file=sys.stderr, flush=True)
    except Exception:
        pass


def _write_status(**changes) -> None:
    """Health file for humans/other AIs: cat /run/user/1000/qqmusic-bridge.json after a XiaoZhi update."""
    try:
        with _status_lock:
            _status.update(changes)
            if STATUS_PATH == '/run/user/1000/qqmusic-bridge.json' and not _is_client_argv(getattr(sys, 'orig_argv', None) or sys.argv):
                return
            data = dict(_status, xiaozhi=_client_version(), pid=os.getpid(),
                        updated=time.strftime('%Y-%m-%d %H:%M:%S'))
            tmp = f'{STATUS_PATH}.{os.getpid()}.{threading.get_ident()}.tmp'
            created = False
            try:
                if os.path.islink(STATUS_PATH):
                    raise OSError('Refusing symlink status path')
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o644)
                created = True
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    os.fchmod(f.fileno(), 0o644)
                    if os.getuid() == 0:
                        try:
                            os.fchown(f.fileno(), 1000, 1000)
                        except OSError:
                            pass
                    json.dump(data, f, ensure_ascii=False)
                if os.path.islink(STATUS_PATH):
                    raise OSError('Refusing symlink status path')
                os.replace(tmp, STATUS_PATH)
            finally:
                if created:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
    except Exception:
        pass


def _client_version() -> str:
    try:
        return os.path.basename(os.path.realpath('/opt/thinksmart/client/current'))
    except Exception:
        return '?'


def _clear_pcm_q():
    global _flush_seq
    _flush_seq += 1
    while True:
        try:
            _pcm_q.get_nowait()
        except queue.Empty:
            break
    _flush_seq += 1


def _socket_reader():
    _audio_hooked.wait()
    try:
        import numpy as np
    except Exception as e:
        _log(f'numpy unavailable ({e!r}); music will use plain playback')
        return
    block = 960 * 2 * 4  # 7680 bytes = 20ms of 48kHz stereo float32
    while not _disabled.is_set():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
                except Exception:
                    pass
                s.settimeout(1.0)
                s.connect(SOCK_PATH)
                buf = bytearray()
                while not _disabled.is_set():
                    try:
                        chunk = s.recv(block - len(buf))
                    except socket.timeout:
                        continue
                    if not chunk:
                        _clear_pcm_q()
                        break
                    buf.extend(chunk)
                    if len(buf) == block:
                        raw_bytes = bytes(buf)
                        buf.clear()
                        if FLUSH_MAGIC in raw_bytes:
                            _clear_pcm_q()
                            if raw_bytes[:16] != FLUSH_MAGIC:
                                break
                            continue
                        arr = np.frombuffer(raw_bytes, dtype=np.float32).reshape(-1, 2).copy()
                        np.nan_to_num(arr, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
                        np.clip(arr, -1.0, 1.0, out=arr)
                        # Block naturally to apply backpressure to ffmpeg (zero frame drops!)
                        my_seq = _flush_seq
                        while my_seq == _flush_seq and not _disabled.is_set():
                            try:
                                _pcm_q.put(arr, timeout=0.02 if _speaker_held.is_set() else 0.05)
                                break
                            except queue.Full:
                                try:
                                    peek = s.recv(16384, socket.MSG_PEEK | socket.MSG_DONTWAIT)
                                    if peek == b'' or FLUSH_MAGIC in peek:
                                        _clear_pcm_q()
                                        break
                                except (BlockingIOError, InterruptedError):
                                    pass
                                except OSError:
                                    _clear_pcm_q()
                                    break
                                if _speaker_held.is_set():
                                    try:
                                        _pcm_q.get_nowait()
                                    except queue.Empty:
                                        pass
                                continue
        except Exception:
            time.sleep(1.0)
    _clear_pcm_q()
    _log('music socket closed; thinksmart-qqmusic now plays through PipeWire directly')


def _install_audio_hook(mod) -> None:
    import numpy as np
    Player = mod.Player
    orig_pull = Player.pull
    state = {'rem': np.zeros((0, 2), dtype=np.float32), 'last_flush': 0}

    def _disable(reason: str) -> None:
        if _disabled.is_set():
            return
        _disabled.set()
        try:
            Player.pull = orig_pull
        except Exception:
            pass
        _clear_pcm_q()
        _write_status(audio_hook=False, disabled=True, note=reason)
        _log(f'audio hook removed itself: {reason} (XiaoZhi {_client_version()}); '
             'XiaoZhi is untouched, music falls back to plain playback')

    def wrapped_pull(self, n, *args, **kwargs):
        out = orig_pull(self, n, *args, **kwargs)
        if _disabled.is_set():
            return out
        try:
            if state['last_flush'] != _flush_seq:
                state['last_flush'] = _flush_seq
                state['rem'] = np.zeros((0, 2), dtype=np.float32)
            if _speaker_held.is_set() and _hold_meta['pending'] == 0:
                cref = _hold_meta['client']
                cobj = cref() if cref is not None else None
                if cobj is not None and hasattr(cobj, 'held_volume') and cobj.held_volume is None:
                    _hold_meta['active'] = False
                    _speaker_held.clear()
            if not _speaker_held.is_set() and self.level == 0.0 and not self._sounding:
                m_got = 0
                while m_got < n:
                    if state['last_flush'] != _flush_seq:
                        state['last_flush'] = _flush_seq
                        state['rem'] = np.zeros((0, 2), dtype=np.float32)
                        break
                    rem = state['rem']
                    if len(rem) > 0:
                        take = min(n - m_got, len(rem))
                        out[m_got:m_got + take] = rem[:take]
                        state['rem'] = rem[take:]
                        m_got += take
                    else:
                        try:
                            state['rem'] = _pcm_q.get_nowait()
                        except queue.Empty:
                            break
                if m_got > 0:
                    # Crucial: update level and last_sound so Audio._work runs WebRTC AEC
                    # (process_near + linear wake-word canceller) while music is playing!
                    self.level = float(np.sqrt(np.mean(out[:m_got] ** 2)))
                    self.last_sound = time.monotonic()
            else:
                # Player is outputting its own audio (e.g. alarm ring, tone preview, report chime):
                # drain n music frames per pull so the Unix socket never stalls into pacat fallback.
                dropped = 0
                while dropped < n:
                    if state['last_flush'] != _flush_seq:
                        state['last_flush'] = _flush_seq
                        state['rem'] = np.zeros((0, 2), dtype=np.float32)
                        break
                    rem = state['rem']
                    if len(rem) > 0:
                        take = min(n - dropped, len(rem))
                        state['rem'] = rem[take:]
                        dropped += take
                    else:
                        try:
                            state['rem'] = _pcm_q.get_nowait()
                        except queue.Empty:
                            break
        except Exception as e:
            _disable(f'{type(e).__name__}: {e}')
        return out

    Player.pull = wrapped_pull
    _audio_hooked.set()
    _write_status(audio_hook=True)
    _log(f'audio hook installed (XiaoZhi {_client_version()})')


_AMBIENT_IDS = ['rain', 'waves', 'forest', 'stream', 'night', 'sleep_piano', 'fireplace', 'library', 'singing_bowl',
                'white_noise', 'pink_noise', 'brown_noise', 'off']  # 2026-11-12: three generated noises
_AMBIENT_ALL_IDS = _AMBIENT_IDS[:-1] + ['deep_study', 'cozy_read', 'deep_sleep', 'off']
_AMBIENT_STOP_WORDS = frozenset({'off', 'stop', 'none', 'cancel', '0', 'false', '关闭', '关掉', '停止', '取消', '静音', '不叠加', '无'})
_AMBIENT_STOP_PREFIXES = ('关闭', '关掉', '停止', '取消', '别放', '停掉', '不要')
_AMBIENT_ALIASES = {
    '深度自习': 'deep_study', '沉浸自习': 'deep_study', '深度自习配方': 'deep_study',
    '围炉夜读': 'cozy_read', '阅读放空': 'cozy_read', '围炉夜读配方': 'cozy_read',
    '深度入眠': 'deep_sleep', '深度助眠': 'deep_sleep', '深度入眠配方': 'deep_sleep',
    '雨声': 'rain', '细雨': 'rain', '窗外细雨': 'rain', '下雨': 'rain', '下雨声': 'rain', '自然音': 'rain', '环境音': 'rain',
    '白噪音': 'white_noise', '柔和白噪音': 'white_noise', '粉噪音': 'pink_noise', '粉红噪音': 'pink_noise', '粉噪': 'pink_noise',
    '褐噪': 'brown_noise', '褐噪音': 'brown_noise', '静心褐噪': 'brown_noise', '低沉褐噪音': 'brown_noise', '棕噪音': 'brown_noise',
    '海浪': 'waves', '大海': 'waves', '海岸慢浪': 'waves', '潮汐': 'waves', '潮汐声': 'waves', '海浪声': 'waves',
    '森林': 'forest', '森林微风': 'forest', '微风': 'forest', '微风声': 'forest', '林间微风': 'forest', '山谷微风': 'forest', '风声': 'forest', '鸟鸣': 'forest',
    '溪流': 'stream', '溪流声': 'stream', '流水': 'stream', '流水声': 'stream', '山涧溪流': 'stream', '潺潺流水': 'stream', '溪水': 'stream',
    '虫鸣': 'night', '虫鸣声': 'night', '夏夜虫鸣': 'night',
    '轻琴': 'sleep_piano', '雨夜轻琴': 'sleep_piano', '助眠钢琴': 'sleep_piano', '睡眠钢琴': 'sleep_piano', '钢琴': 'sleep_piano', '助眠': 'sleep_piano', '助眠音': 'sleep_piano', '催眠': 'sleep_piano',
    '壁炉': 'fireplace', '壁炉声': 'fireplace', '柴火': 'fireplace', '柴火声': 'fireplace', '壁炉柴火': 'fireplace', '篝火': 'fireplace', '篝火声': 'fireplace', '烤火': 'fireplace', 'fire': 'fireplace',
    '书房': 'library', '图书馆': 'library', '翻书': 'library', '翻书声': 'library', '写字': 'library', '书房自习': 'library', '自习室': 'library',
    '颂钵': 'singing_bowl', '颂钵声': 'singing_bowl', '禅意颂钵': 'singing_bowl', '冥想': 'singing_bowl', '冥想音': 'singing_bowl', '钵音': 'singing_bowl', 'bowl': 'singing_bowl',
}


def _norm_ambient_arg(raw, default: str = '', allow_recipes: bool = True) -> str:
    if raw is False or raw == 0:
        return 'off'
    if raw is None or isinstance(raw, bool):
        return default
    s = str(raw).strip().lower().strip(' ，。！？,.!?《》"\'“”')
    if not s:
        return default
    if s in _AMBIENT_STOP_WORDS or any(s.startswith(p) and len(s) > len(p) for p in _AMBIENT_STOP_PREFIXES):
        return 'off'
    s = _AMBIENT_ALIASES.get(s, s)
    allowed = _AMBIENT_ALL_IDS if allow_recipes else _AMBIENT_IDS
    if s in allowed:
        return s
    for suf in ('环境音', '自然音', '白噪音', '背景音', '助眠音', '音效', '声音', '配方'):
        if s.endswith(suf) and len(s) > len(suf):
            stem = s[:-len(suf)].strip()
            mapped = _AMBIENT_ALIASES.get(stem, stem)
            if mapped in allowed:
                return mapped
    return default

MUSIC_TOOLS = [
    {
        'name': 'self.music.play',
        'description': '使用独立音乐与电台服务播放歌曲、歌手、排行榜、我喜欢的歌、个人歌单、U盘音乐，或播放高音质音乐电台（音乐之声/怀旧金曲广播/亚洲音乐台/亚洲经典台/亚洲粤语台/亚洲热歌台/上海动感101/上海最爱103.7/广东音乐之声/深圳飞扬971/北京音乐97.4/怀旧好声音/经典音乐台/上海经典947/爵士轻音乐台/古典音乐广播/欧美金曲电台/浙江动听968/江苏经典流行/四川岷江音乐/湖北楚天音乐/草原天籁民歌/香港电台二台/澳门绿邨电台等）、全国与本地广播（中国之声/经济之声/上海新闻广播/天津相声广播/北京交通103.9/北京交通广播/北京综合广播/大湾区之声/香港电台一台普通话台等）、少儿故事与英语名著（米小圈上学记/牛津树英语/新概念英语/唐诗三百首/声律启蒙/福尔摩斯探案/红楼梦有声书/凯叔讲故事/成语故事/十万个为什么/恐龙探险故事/西游记故事/睡前故事等，支持自动断点续播）、相声评书（郭德纲相声/岳云鹏相声/马三立/刘宝瑞单口/单田芳评书/三国演义评书/水浒传评书/刘兰芳岳飞传/杨家将/三侠五义包公等）。当用户说「播放周杰伦的晴天」「放音乐电台」「放亚洲音乐台」「放爵士轻音乐台」「放中国之声」「放天津相声广播」「放北京交通广播」「讲个儿童故事」「放米小圈」「放牛津树英语」「来段郭德纲相声」「继续听三国演义评书」时调用。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': '歌名、歌手名、歌单名、电台/故事/评书名（如音乐之声/亚洲音乐台/怀旧金曲广播/爵士轻音乐台/中国之声/天津相声广播/北京交通广播/米小圈/牛津树英语/郭德纲相声/三国演义评书）或指令，随机填 random'},
            },
            'required': ['query'],
        },
    },
    {
        'name': 'self.music.control',
        'description': '控制音乐与电台播放：暂停(pause)、继续(resume)、停止(stop)、下一首/下一台(next)、上一首/上一台(prev)、单曲循环(single)、列表循环(loop)、随机播放(shuffle)、打开音乐界面(open_ui)、打开电台界面(open_radio)。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'enum': ['pause', 'resume', 'stop', 'next', 'prev', 'single', 'loop', 'shuffle', 'open_ui', 'open_radio']},
            },
            'required': ['action'],
        },
    },
    # round 109 (2026-11-09)
    {
        'name': 'self.music.sleep_timer',
        'description': '音乐和助眠声音的定时关闭（睡眠定时）：最后 5 分钟音量慢慢变小，然后停止。当用户说「半小时后关音乐」「20分钟后停止播放」「放到23点半」「放一整夜」「听完这首就停」「再放三首就关」「取消定时关闭」时调用。minutes、until、all_night、tracks 只填一个；取消时 cancel 填 true。用户说要睡觉了时优先用 play_music 的睡眠模式。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'minutes': {'type': 'integer', 'description': '多少分钟后停止，1-240'},
                'tracks': {'type': 'integer', 'description': '再播几首后停止（包含正在播的这首），听完这首就停填 1'},
                'until': {'type': 'string', 'description': '几点停止，24 小时制 HH:MM，例如 23:30'},
                'all_night': {'type': 'boolean', 'description': '放一整夜：半小时后自动调小，早上 6:30 停止'},
                'cancel': {'type': 'boolean', 'description': '取消定时关闭'},
            },
        },
    },
    {
        'name': 'self.study.focus',
        'description': '番茄专注计时：屏幕显示倒计时，到点用语音提醒休息，专注期间默认自动开启学习陪伴。当用户说「开始专注25分钟」「我要学习半小时」「开个番茄钟」「结束专注」「不专注了」时调用。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'enum': ['start', 'stop']},
                'minutes': {'type': 'integer', 'description': '专注多少分钟，默认 25，范围 1-180'},
                'break_minutes': {'type': 'integer', 'description': '休息多少分钟，默认 5，范围 0-60'},
                'with_study': {'type': 'boolean', 'description': '是否同步开启学习陪伴，默认 true'},
                'ambient': {'type': 'string', 'enum': _AMBIENT_ALL_IDS, 'description': '可选专注背景自然音或混音配方'},
            },
            'required': ['action'],
        },
    },
    {
        'name': 'self.music.ambient',
        'description': '播放或关闭高音质自然环境音/双层混音配方：窗外细雨(rain)、海岸慢浪(waves)、林间微风(forest)、山涧溪流(stream)、夏夜虫鸣(night)、雨夜轻琴(sleep_piano)、壁炉柴火(fireplace)、书房自习(library)、禅意颂钵(singing_bowl)、深度自习配方(deep_study)、围炉夜读配方(cozy_read)、深度入眠配方(deep_sleep)、关闭(off)。支持可选 layer 叠加第二层环境音与 layer_gain(0.1-1.0)。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'preset': {'type': 'string', 'enum': _AMBIENT_ALL_IDS},
                'layer': {'type': 'string', 'enum': _AMBIENT_IDS, 'description': '可选叠加的第二层氛围音，不叠加填 off'},
                'layer_gain': {'type': 'number', 'description': '第二层叠加比例 0.1-1.0，默认 0.45'},
            },
            'required': ['preset'],
        },
    },
    {
        'name': 'self.home.timer',
        'description': '主页大屏计时器（倒计时提醒或正向秒表计次）：开始(start)、暂停(pause)、继续(resume)、计次分段(lap)、增减时长(add)、结束取消(stop)。支持 countdown（倒计时）和 stopwatch（正向计时/秒表）。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'enum': ['start', 'pause', 'resume', 'lap', 'add', 'stop']},
                'mode': {'type': 'string', 'enum': ['countdown', 'stopwatch'], 'description': 'countdown 为倒计时，stopwatch 为从0累计的正向秒表'},
                'minutes': {'type': 'number', 'description': '倒计时或增减分钟数，范围 0.1-180'},
                'seconds': {'type': 'integer', 'description': '可选精确秒数（倒计时 5-10800 秒，或 add 增减秒数）'},
                'label': {'type': 'string', 'description': '简短用途标签，如 做题限时、做卷测速、沉浸自习、练琴'},
                'ambient': {'type': 'string', 'enum': _AMBIENT_ALL_IDS, 'description': '可选计时背景自然音或混音配方'},
            },
            'required': ['action'],
        },
    },
    # 2026-11-12 (Claude): the message board and timed reminders
    {
        'name': 'self.home.message',
        'description': '家里的留言板（屏幕和家人手机之间）。用户要给家人留言时（如「给爸爸留言，我到家了」「告诉妈妈我作业写完了」）action 填 send，text 填留言内容，from 填说话的人是谁（知道的话）；用户问「有没有新留言」「读一下留言」时 action 填 read，会把未读留言读出来并标成已读。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'enum': ['send', 'read']},
                'text': {'type': 'string', 'description': '留言内容（send 时必填，200 字以内）'},
                'from': {'type': 'string', 'enum': ['爸爸', '妈妈', '姐姐', '妹妹'], 'description': '谁留的言（知道的话）'},
            },
            'required': ['action'],
        },
    },
    {
        'name': 'self.home.reminder',
        'description': '定时提醒：到点在屏幕上弹出并朗读。比如「明早7点提醒妹妹带作业」「半小时后提醒我关火」「晚上9点提醒姐姐洗澡」：action 填 add，text 填要提醒的事，who 填被提醒的人（提醒自己可不填），时间用 day（today / tomorrow / 后天 或 YYYY-MM-DD）加 time（24 小时制 HH:MM），或者用 in_minutes（几分钟后）。「有哪些提醒」action 填 list；「取消带作业的提醒」action 填 cancel，keyword 填关键词。每天、工作日或每周几都要提醒的（如「每天晚上9点提醒姐姐洗澡」「工作日早上7点10分提醒妹妹带水杯」「每周一提醒带红领巾」）：repeat 填 daily / workdays / weekly，weekly 时 weekdays 填星期几（如 一、一三五），time 填 HH:MM。叫醒起床、要响铃的请用闹钟工具。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'enum': ['add', 'list', 'cancel']},
                'text': {'type': 'string', 'description': '要提醒的事'},
                'who': {'type': 'string', 'enum': ['爸爸', '妈妈', '姐姐', '妹妹'], 'description': '提醒谁'},
                'day': {'type': 'string', 'description': 'today、tomorrow、后天，或 YYYY-MM-DD'},
                'time': {'type': 'string', 'description': '24 小时制 HH:MM，例如 07:00、21:30'},
                'in_minutes': {'type': 'integer', 'description': '几分钟后提醒（和 day/time 二选一）'},
                'keyword': {'type': 'string', 'description': '取消时用的关键词'},
                'repeat': {'type': 'string', 'enum': ['once', 'daily', 'workdays', 'weekly'], 'description': '重复：once 只一次（默认）、daily 每天、workdays 工作日（周一到周五）、weekly 每周'},
                'weekdays': {'type': 'string', 'description': 'repeat 为 weekly 时提醒的星期几，如 一、一三五、周六日'},
            },
            'required': ['action'],
        },
    },
    # 2026-11-12 (Antigravity): server health, US-optimized traffic quota, 3-carrier latency, cross-server compare, local tunnels, offline events, and voice screen open
    {
        'name': 'self.server.status',
        'description': '查询或打开哪吒服务器监控状态、跨机器横向对比（哪台服务器延迟最低/最快、哪台负载最高/磁盘最满）、主力节点剩余月流量与重置时间、主力节点或任意单机三网（联通/电信/移动）实时延迟与丢包、单台服务器负载与回程波形（如主力节点/亚太节点/欧洲节点/备用节点）、本机反向隧道与Clash代理健康态、近期掉线与恢复记录、到期时间或年度续费账单总花销。当用户问「服务器都正常吗」「哪台服务器延迟最低」「哪台负载最高」「亚太节点电信延迟多少」「隧道正常吗」「最近掉过线吗」「主力节点还剩多少流量」「现在三网延迟多少」「备用节点怎么样」「服务器一年多少钱」「打开主力节点电信波形」「打开服务器账单」时调用；若用户要求在屏幕上打开或查看面板/波形/账单，将 open_ui 设为 true，并把用户提到的机器与运营商原词填入 query。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'focus': {
                    'type': 'string',
                    'enum': ['overview', 'traffic', 'carriers', 'server', 'compare', 'expiry', 'billing', 'events'],
                    'description': '查询侧重点：整体状态(overview)、主力节点流量(traffic)、主力节点三网延迟(carriers)、指定机器或指定运营商(server)、跨机对比排行(compare)、到期时间(expiry)、年费账单总花销(billing)、掉线与恢复记录(events)',
                },
                'query': {
                    'type': 'string',
                    'description': '可选关键词或原问题，如 哪台延迟最低、哪台负载最高、香港节点电信、亚太节点移动、主力节点、海外节点、隧道状态、掉线记录、年费、打开面板',
                },
                'open_ui': {
                    'type': 'boolean',
                    'description': '用户要求在屏幕上打开/切到服务器大盘、某台机器指定运营商波形图、三网延迟或集群账单总览时填 true；仅口头询问填 false',
                },
            },
        },
    },
    {
        'name': 'self.home.countdown',
        'description': (
            '管理家里屏幕上的「倒数日 / 生日 / 纪念日 / 累计正数日」（只管家里自己记的重要日子，不含法定节假日）。'
            '当用户问「还有多少天中考」「最近家里有什么日子」「妈妈生日还有几天」、或说「帮我加个倒数日…」「记一下农历十月十二妈妈生日」'
            '「记个正数日从今天开始背单词」「把期末冲刺置顶到主页」「删掉期末考倒数日」「打开倒数日页面」时调用。'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'action': {
                    'type': 'string',
                    'enum': ['list', 'query', 'add', 'update', 'delete', 'pin', 'unpin', 'open', 'close'],
                    'description': 'list(查全部)/query(查某条还有几天)/add(新增)/update(修改已有日期)/delete(删除)/pin(置顶到主页)/unpin(取消置顶)/open(打开倒数日页面)/close(关闭倒数日回主页)',
                },
                'title': {
                    'type': 'string',
                    'description': '名称（不超过24字），如 期末冲刺、妈妈生日、结婚纪念日、每日坚持阅读',
                },
                'keyword': {
                    'type': 'string',
                    'description': '查询、置顶或删除时的关键词，如 中考、妈妈生日、纪念日',
                },
                'kind': {
                    'type': 'string',
                    'enum': ['once', 'yearly_solar', 'yearly_lunar', 'countup'],
                    'description': 'once=单次目标倒数日，yearly_solar=每年公历生日/纪念日，yearly_lunar=每年农历生日/纪念日，countup=从某天起累计正数日',
                },
                'date': {
                    'type': 'string',
                    'description': '公历日期 YYYY-MM-DD 或 MM-DD（如 2027-06-20、05-20、今天、明天）',
                },
                'lunar_date': {
                    'type': 'string',
                    'description': '农历月日文本，如 十月十二、正月十五、腊月廿八、闰六月十五',
                },
                'lunar_month': {'type': 'integer', 'description': '农历月份 1-12'},
                'lunar_day': {'type': 'integer', 'description': '农历日期 1-30'},
                'lunar_leap': {'type': 'boolean', 'description': '是否闰月'},
                'start_year': {'type': 'integer', 'description': '可选起始年份（如出生年 2014 或结婚年 2015，用于计算周岁/周年）'},
                'category': {
                    'type': 'string',
                    'enum': ['exam', 'birthday', 'anniv', 'life'],
                    'description': '分类：exam(考试日程)/birthday(生日)/anniv(纪念日)/life(生活纪念)',
                },
                'pinned': {'type': 'boolean', 'description': '是否置顶到屏幕主页'},
                'open_ui': {'type': 'boolean', 'description': '同时在屏幕上打开倒数日页面时填 true'},
            },
            'required': ['action'],
        },
    },
    {
        'name': 'self.tutor.explain',
        'description': (
            '使用屏幕上的「AI动态黑板讲题」系统给孩子进行苏格拉底启发式画图讲题、拍草稿找错因、手写习作升格精批或错题换数复测（全九科支持语、数、英、物、化、生、地、史、道法及中英文启发式作文审题与习作批改、找规律降维与错题复测）。'
            '【铁律：凡是孩子或用户口头询问某道数学/物理/化学/生物/地理/历史/道法/语文阅读古诗/英语语法题怎么算、怎么做、怎么赏析，'
            '或说「打开AI讲题」「打开手机拍题」「讲一下刚才手机传过来的题」「看看我的草稿哪一步错了」「看看我写的作文怎么改」「考我一道错题本里的题」时，必须调用本工具（文字题填 action=explain 与完整 question；错题复测填 action=mistake_retest），严禁在语音中直接说出算式或最终答案！】'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'action': {
                    'type': 'string',
                    'enum': ['open', 'open_phone', 'phone_snap', 'explain', 'camera_snap', 'mistake_retest'],
                    'description': 'open(打开AI讲题主页)/open_phone(打开手机扫码拍题与整页框选页)/phone_snap(直接讲解或批改刚才手机上传的题目/草稿)/explain(在动态黑板上画图讲解数学/物理/英语文字题)/camera_snap(用USB高拍仪抓拍桌面题目或草稿开讲)/mistake_retest(从错题本挑题脱敏换数复测)',
                },
                'question': {
                    'type': 'string',
                    'description': '要讲解的题目内容（action=explain时填写）',
                },
                'mode': {
                    'type': 'string',
                    'enum': ['fast', 'deep', 'backup'],
                    'description': 'fast(默认主力GPT-6.1-Terra) / deep(深度讲解GPT-6.1-Sol) / backup(备用Gemini-3.8-Flash)',
                },
                'pedagogy': {
                    'type': 'string',
                    'enum': ['auto', 'elem', 'junior'],
                    'description': 'auto(智能适龄) / elem(小学画图份数法，适合小学生使用，禁用x方程) / junior(初中方程与几何定理法，适合初中生使用)',
                },
                'page': {
                    'type': 'integer',
                    'description': '可选作业夹页码（1-4），如用户说打开第2页作业或讲第2页的题时填 2',
                },
            },
            'required': ['action'],
        },
    },
]
_MY_TOOLS = ('self.music.play', 'self.music.control', 'self.music.sleep_timer', 'self.study.focus', 'self.music.ambient', 'self.home.timer',
             'self.home.message', 'self.home.reminder', 'self.home.countdown', 'self.server.status', 'self.tutor.explain')


def _sleep_body(args_: dict) -> dict:
    # 2026-11-12: voice timers fade over their last 5 minutes (a third of shorter ones); until HH:MM; all night
    if args_.get('cancel') is True:
        return {'cancel': True}
    if args_.get('all_night') is True:
        return {'night': True}
    until = args_.get('until')
    if isinstance(until, str) and 0 < len(until.strip()) <= 8:
        return {'until': until.strip(), 'fade': 300}
    for key, lo, hi in (('minutes', 1, 240), ('tracks', 1, 50)):
        v = args_.get(key)
        if isinstance(v, bool) or v is None:
            continue
        try:
            n = int(float(v))
        except (TypeError, ValueError, OverflowError):
            continue
        if lo <= n <= hi:
            return {key: n, 'fade': 300} if key == 'minutes' else {key: n}
    return {'minutes': 30, 'fade': 300}


def _focus_body(args_: dict) -> dict:
    raw_act = str(args_.get('action') or 'start').strip().lower()
    if raw_act in ('stop', 'cancel', 'end', 'off', '结束', '取消', '停止', '关闭') or any(
        k in raw_act for k in ('stop', 'cancel', '结束', '取消', '停止', '关闭')
    ):
        return {'action': 'stop'}
    v = args_.get('minutes', 25)
    try:
        n = int(float(v)) if not isinstance(v, bool) else 25
    except (TypeError, ValueError, OverflowError):
        n = 25
    out = {'action': 'start', 'minutes': n if 1 <= n <= 180 else 25}
    bv = args_.get('break_minutes')
    if bv is not None and not isinstance(bv, bool):
        try:
            bn = int(float(bv))
            if 0 <= bn <= 60:
                out['break_minutes'] = bn
        except (TypeError, ValueError, OverflowError):
            pass
    ws = args_.get('with_study')
    if isinstance(ws, bool):
        out['with_study'] = ws
    amb = _norm_ambient_arg(args_.get('ambient'), default='', allow_recipes=True)
    if amb in _AMBIENT_ALL_IDS:
        out['ambient'] = amb
    return out


def _call_study_focus(body: dict) -> str:
    payload = json.dumps(body).encode('utf-8')
    req_home = urllib.request.Request(
        'http://127.0.0.1:8095/api/home/focus',
        data=payload,
        headers={'Content-Type': 'application/json', 'Host': '127.0.0.1:8095', 'X-TS-Internal': '1'},
        method='POST',
    )
    try:
        with _NO_PROXY_OPENER.open(req_home, timeout=8) as r:
            return r.read(65536).decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(4096).decode('utf-8', errors='replace').strip()
        finally:
            e.close()
        raise RuntimeError(detail or str(e)) from None
    except urllib.error.URLError:
        pass
    req = urllib.request.Request(
        'http://127.0.0.1:8093/api/study/focus',
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with _NO_PROXY_OPENER.open(req, timeout=10) as r:
            return r.read(65536).decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(4096).decode('utf-8', errors='replace').strip()
        finally:
            e.close()
        raise RuntimeError(detail or str(e)) from None


_TIMER_ACT_ALIASES = {
    'start': 'start', '开始': 'start',
    'pause': 'pause', '暂停': 'pause',
    'resume': 'resume', 'continue': 'resume', '继续': 'resume',
    'lap': 'lap', '计次': 'lap', '分段': 'lap',
    'add': 'add', '加时': 'add', '增加': 'add',
    'stop': 'stop', 'cancel': 'stop', 'reset': 'stop', 'ack': 'stop',
    'end': 'stop', 'clear': 'stop', 'off': 'stop',
    '取消': 'stop', '停止': 'stop', '关闭': 'stop', '重置': 'stop', '结束': 'stop',
}


def _call_home_timer(args_: dict) -> str:
    raw_act = str(args_.get('action') or 'start').strip().lower()
    act = _TIMER_ACT_ALIASES.get(raw_act, raw_act)
    if act not in ('start', 'pause', 'resume', 'lap', 'add', 'stop'):
        raise ValueError(f'未知计时指令: {raw_act}')
    body = {'action': act}
    if act == 'start':
        lbl = str(args_.get('label') or '').strip()[:20]
        mode = str(args_.get('mode') or '').strip().lower()
        if mode == 'stopwatch' or any(k in lbl for k in ('正向', '秒表', '测速')):
            body['mode'] = 'stopwatch'
        else:
            raw_s = args_.get('seconds')
            sec = None
            if raw_s is not None and not isinstance(raw_s, bool):
                try:
                    sf = float(raw_s)
                    if math.isfinite(sf) and sf > 0:
                        sec = int(round(max(5.0, min(10800.0, sf))))
                except (TypeError, ValueError, OverflowError):
                    sec = None
            if sec is None:
                raw_m = args_.get('minutes', 15)
                try:
                    mf = float(raw_m) if not isinstance(raw_m, bool) else 15.0
                except (TypeError, ValueError, OverflowError):
                    mf = 15.0
                if not math.isfinite(mf) or mf <= 0:
                    mf = 15.0
                sec = int(round(max(5.0, min(10800.0, mf * 60.0))))
            body['seconds'] = sec
        if lbl:
            body['label'] = lbl
        amb = _norm_ambient_arg(args_.get('ambient'), default='', allow_recipes=True)
        if amb in _AMBIENT_ALL_IDS:
            body['ambient'] = amb
    elif act == 'add':
        raw_s = args_.get('seconds')
        delta = None
        if raw_s is not None and not isinstance(raw_s, bool):
            try:
                sf = float(raw_s)
                if math.isfinite(sf):
                    delta = int(round(sf))
            except (TypeError, ValueError, OverflowError):
                delta = None
        if delta is None:
            raw_m = args_.get('minutes', 1)
            try:
                mf = float(raw_m) if not isinstance(raw_m, bool) else 1.0
            except (TypeError, ValueError, OverflowError):
                mf = 1.0
            if not math.isfinite(mf):
                mf = 1.0
            delta = int(round(mf * 60.0))
        delta = max(-3600, min(3600, delta))
        body['seconds'] = delta if delta != 0 else 60
    req = urllib.request.Request(
        'http://127.0.0.1:8095/api/home/timer',
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Host': '127.0.0.1:8095', 'X-TS-Internal': '1'},
        method='POST',
    )
    try:
        with _NO_PROXY_OPENER.open(req, timeout=8) as r:
            return r.read(65536).decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(4096).decode('utf-8', errors='replace').strip()
        finally:
            e.close()
        raise RuntimeError(detail or str(e)) from None


_FAMILY = ('爸爸', '妈妈', '姐姐', '妹妹')


def _post_home_tool(body: dict) -> str:
    """2026-11-12: the home service's message board / reminder endpoint; returns the sentence 小麦 should say."""
    req = urllib.request.Request(
        'http://127.0.0.1:8095/api/home/messages/tool',
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Host': '127.0.0.1:8095', 'X-TS-Internal': '1'},
        method='POST',
    )
    try:
        with _NO_PROXY_OPENER.open(req, timeout=8) as r:
            data = json.loads(r.read(65536).decode('utf-8', errors='replace'))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(4096).decode('utf-8', errors='replace').strip()
        finally:
            e.close()
        raise RuntimeError(detail or str(e)) from None
    if not isinstance(data, dict):
        raise RuntimeError('留言板没有回应')
    return str(data.get('text') or ('好的' if data.get('ok') else '没有办成'))


def _call_home_message(args_: dict) -> str:
    act = str(args_.get('action') or 'send').strip().lower()
    if act in ('read', 'list', 'check', '读', '读留言', '查看'):
        return _post_home_tool({'action': 'read'})
    text = str(args_.get('text') or '').strip()
    if not text:
        raise ValueError('留言内容是空的')
    who = args_.get('from') if args_.get('from') in _FAMILY else ''
    return _post_home_tool({'action': 'send', 'text': text[:200], 'from': who})


def _call_home_reminder(args_: dict) -> str:
    act = str(args_.get('action') or 'add').strip().lower()
    if act in ('list', 'reminders', '查看', '列表'):
        return _post_home_tool({'action': 'reminders'})
    if act in ('cancel', 'delete', 'remove', '取消', '删除'):
        return _post_home_tool({'action': 'cancel', 'keyword': str(args_.get('keyword') or args_.get('text') or '').strip()[:20]})
    body = {'action': 'remind', 'text': str(args_.get('text') or '').strip()[:200],
            'who': args_.get('who') if args_.get('who') in _FAMILY else ''}
    for k in ('day', 'time', 'at'):
        if isinstance(args_.get(k), str) and args_[k].strip():
            body[k] = args_[k].strip()[:20]
    if args_.get('in_minutes') is not None and not isinstance(args_.get('in_minutes'), bool):
        body['in_minutes'] = args_['in_minutes']
    if args_.get('repeat') in ('daily', 'workdays', 'weekly'):
        body['repeat'] = args_['repeat']
        if isinstance(args_.get('weekdays'), str) and args_['weekdays'].strip():
            body['weekdays'] = args_['weekdays'].strip()[:20]
    if not body['text']:
        raise ValueError('要提醒什么事没听清')
    return _post_home_tool(body)

def _call_home_countdown(args_: dict) -> str:
    act_raw = str(args_.get('action') or 'list').strip().lower()
    act_map = {
        '查看': 'list', '列表': 'list', '查询': 'query', '查': 'query',
        '添加': 'add', '新增': 'add', '新建': 'add', '记': 'add',
        '修改': 'update', '更新': 'update', '编辑': 'update', '改': 'update',
        '删除': 'delete', '取消': 'delete', '移除': 'delete',
        '置顶': 'pin', '取消置顶': 'unpin',
        '打开': 'open', '打开页面': 'open',
        '关闭': 'close', '关闭页面': 'close', '退出': 'close', '返回': 'close',
    }
    act = act_map.get(act_raw, act_raw)
    body = {'action': act}
    for k in ('title', 'keyword', 'kind', 'date', 'lunar_date', 'category', 'who'):
        if isinstance(args_.get(k), str) and args_[k].strip():
            body[k] = args_[k].strip()[:40]
    for ik in ('month', 'day', 'lunar_month', 'lunar_day', 'start_year'):
        v = args_.get(ik)
        if v is not None and not isinstance(v, bool):
            try:
                body[ik] = int(v)
            except (TypeError, ValueError):
                pass
    for bk in ('lunar_leap', 'pinned', 'open_ui', 'repeat'):
        if bk in args_ and isinstance(args_.get(bk), bool):
            body[bk] = args_[bk]
    q_txt = f"{body.get('title', '')} {body.get('keyword', '')}"
    if act in ('list', 'query') and any(k in q_txt for k in ('打开', '看页面', '看看倒数日', '调出', '切到')):
        body['open_ui'] = True
    req = urllib.request.Request(
        'http://127.0.0.1:8095/api/home/countdowns/tool',
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Host': '127.0.0.1:8095', 'X-TS-Internal': '1'},
        method='POST',
    )
    try:
        with _NO_PROXY_OPENER.open(req, timeout=8) as r:
            data = json.loads(r.read(65536).decode('utf-8', errors='replace'))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(4096).decode('utf-8', errors='replace').strip()
        finally:
            e.close()
        raise RuntimeError(detail or str(e)) from None
    if not isinstance(data, dict):
        raise RuntimeError('倒数日服务没有回应')
    return str(data.get('text') or ('好的' if data.get('ok') else '没有办成'))



_SERVER_OPEN_KW = ('打开', '看面板', '看看面板', '看波形', '波形图', '看详情', '切到', '切屏', '调出', '大盘', '显示面板', '展开面板', '弹出来', '点开')


def _call_server_status(args_: dict) -> str:
    focus = str(args_.get('focus') or '').strip()[:24]
    query = str(args_.get('query') or '').strip()[:80]
    open_ui = bool(args_.get('open_ui') is True or any(k in query for k in _SERVER_OPEN_KW))
    params = {'focus': focus, 'query': query}
    if open_ui:
        params['open_ui'] = '1'
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f'http://127.0.0.1:8092/api/voice_summary?{qs}',
        headers={'Host': '127.0.0.1:8092'},
        method='GET',
    )
    try:
        with _NO_PROXY_OPENER.open(req, timeout=10) as r:
            data = json.loads(r.read(65536).decode('utf-8', errors='replace'))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(4096).decode('utf-8', errors='replace').strip()
        finally:
            e.close()
        raise RuntimeError(detail or str(e)) from None
    if not isinstance(data, dict) or not data.get('text'):
        raise RuntimeError('服务器监控暂时没有回应')
    return str(data['text'])


def _call_tutor_tool(args_: dict) -> str:
    m_raw = str(args_.get('mode') or '').strip().lower()
    p_raw = str(args_.get('pedagogy') or '').strip().lower()
    payload = {
        'action': str(args_.get('action') or 'open').strip(),
        'question': str(args_.get('question') or '').strip(),
        'mode': m_raw if m_raw in ('deep', 'backup') else 'fast',
        'pedagogy': p_raw if p_raw in ('elem', 'junior') else 'auto',
    }
    raw_pg = args_.get('page')
    if raw_pg is not None and not isinstance(raw_pg, bool):
        try:
            pg_i = int(raw_pg)
            if 1 <= pg_i <= 4:
                payload['page'] = pg_i
        except (TypeError, ValueError):
            pass
    req = urllib.request.Request(
        'http://127.0.0.1:8096/api/tutor/tool',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Host': '127.0.0.1:8096', 'X-TS-Internal': '1'},
        method='POST',
    )
    req_timeout = 26 if payload['action'] == 'mistake_retest' else 12
    try:
        with _NO_PROXY_OPENER.open(req, timeout=req_timeout) as r:
            data = json.loads(r.read(65536).decode('utf-8', errors='replace'))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(4096).decode('utf-8', errors='replace').strip()
        finally:
            e.close()
        raise RuntimeError(detail or str(e)) from None
    if not isinstance(data, dict) or not data.get('text'):
        raise RuntimeError('AI讲题服务暂时没有回应')
    return str(data['text'])


def _call_music_ambient(args_: dict) -> str:
    preset = _norm_ambient_arg(args_.get('preset'), default='rain', allow_recipes=True)
    fwd = {'preset': preset}
    lay = _norm_ambient_arg(args_.get('layer'), default='', allow_recipes=False)
    if lay in _AMBIENT_IDS:
        fwd['layer'] = lay
    raw_lg = args_.get('layer_gain')
    if raw_lg is not None and not isinstance(raw_lg, bool):
        try:
            lg_f = float(raw_lg)
            if math.isfinite(lg_f):
                if lg_f > 1.001:
                    lg_f /= 100.0
                fwd['layer_gain'] = round(max(0.10, min(1.00, lg_f)), 2)
        except (TypeError, ValueError, OverflowError):
            pass
    req_home = urllib.request.Request(
        'http://127.0.0.1:8095/api/home/music',
        data=json.dumps({'action': 'ambient', **fwd}).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Host': '127.0.0.1:8095', 'X-TS-Internal': '1'},
        method='POST',
    )
    try:
        with _NO_PROXY_OPENER.open(req_home, timeout=6) as r:
            return r.read(65536).decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(4096).decode('utf-8', errors='replace').strip()
        finally:
            e.close()
        raise RuntimeError(detail or str(e)) from None
    except urllib.error.URLError:
        pass
    return _call_qqmusic_api('/api/music/ambient', fwd)


def _call_qqmusic_api(path: str, body: dict) -> str:
    req = urllib.request.Request(
        f'http://127.0.0.1:8091{path}',
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with _NO_PROXY_OPENER.open(req, timeout=18) as r:
            return r.read(65536).decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(4096).decode('utf-8', errors='replace').strip()
        finally:
            e.close()
        raise RuntimeError(detail or str(e)) from None


def _install_mcp_hook(mcp_mod) -> None:
    existing = {t.get('name') for t in mcp_mod.TOOLS if isinstance(t, dict)}
    mcp_mod.TOOLS.extend(t for t in MUSIC_TOOLS if t['name'] not in existing)
    orig_handle = mcp_mod.handle

    async def wrapped_handle(payload, *args, **kwargs):
        try:
            mine = (isinstance(payload, dict) and payload.get('method') == 'tools/call'
                    and (payload.get('params') or {}).get('name') in _MY_TOOLS
                    and payload.get('id') is not None)
        except Exception:
            mine = False
        if mine:
            params = payload.get('params') or {}
            name = params.get('name')
            args_ = params.get('arguments')
            if isinstance(args_, str):
                try:
                    args_ = json.loads(args_)
                except Exception:
                    args_ = {}
            if not isinstance(args_, dict):
                args_ = {}
            req_id = payload.get('id')
            try:
                loop = asyncio.get_running_loop()
                if name == 'self.music.play':
                    q_arg = str(args_.get('query') or '').strip()[:120] or 'random'
                    txt = await loop.run_in_executor(
                        None, _call_qqmusic_api, '/api/music/play', {'query': q_arg}
                    )
                elif name == 'self.music.sleep_timer':
                    txt = await loop.run_in_executor(None, _call_qqmusic_api, '/api/music/sleep', _sleep_body(args_))
                elif name == 'self.study.focus':
                    txt = await loop.run_in_executor(None, _call_study_focus, _focus_body(args_))
                elif name == 'self.music.ambient':
                    txt = await loop.run_in_executor(None, _call_music_ambient, args_)
                elif name == 'self.home.timer':
                    txt = await loop.run_in_executor(None, _call_home_timer, args_)
                elif name == 'self.home.message':
                    txt = await loop.run_in_executor(None, _call_home_message, args_)
                elif name == 'self.home.reminder':
                    txt = await loop.run_in_executor(None, _call_home_reminder, args_)
                elif name == 'self.home.countdown':
                    txt = await loop.run_in_executor(None, _call_home_countdown, args_)
                elif name == 'self.server.status':
                    txt = await loop.run_in_executor(None, _call_server_status, args_)
                elif name == 'self.tutor.explain':
                    txt = await loop.run_in_executor(None, _call_tutor_tool, args_)
                else:
                    act_arg = str(args_.get('action') or '').strip().lower()[:32] or 'stop'
                    txt = await loop.run_in_executor(
                        None, _call_qqmusic_api, '/api/music/control', {'action': act_arg}
                    )
                return {'jsonrpc': '2.0', 'id': req_id, 'result': {'content': [{'type': 'text', 'text': txt}], 'isError': False}}
            except Exception as e:
                return {'jsonrpc': '2.0', 'id': req_id, 'result': {'content': [{'type': 'text', 'text': str(e)}], 'error': str(e), 'isError': True}}
        # everything else goes to XiaoZhi exactly as it was called
        return await orig_handle(payload, *args, **kwargs)

    mcp_mod.handle = wrapped_handle
    _write_status(voice_tools=True)
    _log(f'voice music tools installed (XiaoZhi {_client_version()})')


def _install_speaker_hold_hook(app_mod) -> None:
    Client = app_mod.Client
    orig_hold = Client._hold_speaker
    orig_release = Client._release_speaker

    async def wrapped_hold_speaker(self, *args, **kwargs):
        try:
            _hold_meta['client'] = weakref.ref(self)
        except TypeError:
            pass
        _hold_meta['pending'] += 1
        _speaker_held.set()
        _clear_pcm_q()
        try:
            res = await orig_hold(self, *args, **kwargs)
            actually_held = not hasattr(self, 'held_volume') or getattr(self, 'held_volume', None) is not None
            if actually_held:
                _hold_meta['active'] = True
                _speaker_held.set()
            elif _hold_meta['pending'] <= 1 and not _hold_meta['active']:
                _speaker_held.clear()
            return res
        except BaseException:
            if _hold_meta['pending'] <= 1 and not _hold_meta['active'] and getattr(self, 'held_volume', None) is None:
                _speaker_held.clear()
            raise
        finally:
            _hold_meta['pending'] = max(0, _hold_meta['pending'] - 1)

    async def wrapped_release_speaker(self, *args, **kwargs):
        try:
            return await orig_release(self, *args, **kwargs)
        finally:
            if getattr(self, 'held_volume', None) is None:
                _hold_meta['active'] = False
                if _hold_meta['pending'] == 0:
                    _speaker_held.clear()

    Client._hold_speaker = wrapped_hold_speaker
    Client._release_speaker = wrapped_release_speaker


def _hook_watcher():
    hooked_audio = False
    hooked_mcp = False
    hooked_app = False
    started = time.monotonic()
    warned = False
    while not (hooked_audio and hooked_mcp and hooked_app):
        if not hooked_audio:
            mod = sys.modules.get('thinksmart_client.audio')
            if mod is not None and hasattr(mod, 'Player') and hasattr(mod.Player, 'pull'):
                try:
                    _install_audio_hook(mod)
                except Exception as e:
                    _write_status(audio_hook=False, disabled=True, note=f'{type(e).__name__}: {e}')
                    _log(f'audio hook not installed: {e!r}; music uses plain playback')
                hooked_audio = True
        if not hooked_mcp:
            mcp_mod = sys.modules.get('thinksmart_client.mcp')
            if mcp_mod is not None and hasattr(mcp_mod, 'TOOLS') and hasattr(mcp_mod, 'handle'):
                try:
                    _install_mcp_hook(mcp_mod)
                except Exception as e:
                    _write_status(voice_tools=False, note=f'{type(e).__name__}: {e}')
                    _log(f'voice music tools not installed: {e!r}')
                hooked_mcp = True
        if not hooked_app:
            app_mod = sys.modules.get('thinksmart_client.app')
            if (app_mod is not None and hasattr(app_mod, 'Client')
                    and hasattr(app_mod.Client, '_hold_speaker')
                    and hasattr(app_mod.Client, '_release_speaker')):
                try:
                    _install_speaker_hold_hook(app_mod)
                except Exception as e:
                    _log(f'speaker hold hook not installed: {e!r}')
                hooked_app = True
        elapsed = time.monotonic() - started
        if elapsed > 60 and not warned:
            warned = True
            missing = [
                name for ok, name in (
                    (hooked_audio, 'thinksmart_client.audio.Player'),
                    (hooked_mcp, 'thinksmart_client.mcp.handle'),
                    (hooked_app, 'thinksmart_client.app.Client'),
                ) if not ok
            ]
            _log(f'XiaoZhi {_client_version()} internals changed? still waiting for ' + ' '.join(missing))
        time.sleep(0.2 if elapsed < 60 else 5.0)


def _is_client_argv(argv) -> bool:
    if os.environ.get('THINKSMART_FORCE_QQMUSIC_BRIDGE') == '1':
        return True
    if not argv:
        return False
    if any(x == '--selftest' or x.endswith(('bt_helper.py', 'thinksmart-update')) for x in argv):
        return False
    if any(x in ('-c', 'pytest', 'unittest', 'py_compile', 'pip') for x in argv[1:4]):
        return False
    if any(argv[i] == '-m' and argv[i + 1] == 'thinksmart_client' for i in range(1, len(argv) - 1)):
        return True
    return os.path.basename(argv[0] or '') in ('thinksmart-client', 'thinksmart_client')


def _start():
    orig_argv = getattr(sys, 'orig_argv', None) or sys.argv
    if not _is_client_argv(orig_argv):
        return
    # Diagnostics only: `kill -USR2 <thinksmart-client pid>` dumps every thread's Python stack to the
    # journal. XiaoZhi does not use SIGUSR2; nothing happens unless someone sends it.
    try:
        import faulthandler
        import signal
        faulthandler.register(signal.SIGUSR2, file=sys.stderr, all_threads=True)
    except Exception:
        pass
    _write_status()
    try:
        threading.Thread(target=_socket_reader, daemon=True, name='qqmusic-bridge-sock').start()
        threading.Thread(target=_hook_watcher, daemon=True, name='qqmusic-bridge-hook').start()
    except Exception as e:
        _log(f'bridge not started: {e!r}')


_start()
