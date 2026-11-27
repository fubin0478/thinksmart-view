"""The voice client: wake word → listen → hub replies → follow-up window → idle.

States shown to the page: idle, connecting, listening, thinking, speaking.
- Wake (voice or button): chime, connect if needed, stream the microphone ("realtime": the
  hub cuts sentences with its VAD).
- Once the hub has recognised what was said, the microphone stops uploading until the reply has
  been played: talking during a reply (to someone else, "嗯", a cough) never interrupts it. Only
  the wake word (still listened for meanwhile) or the button does: the reply stops, the hub is told
  how much of it was heard (it keeps only that in the context), and what is said next is a new turn.
  The same while the hub is still recognising what was said (the wake word is listened for again
  once its VAD says the user stopped) or thinking: the question is dropped, whatever of that turn
  still arrives (its text, its reply) is ignored, and the mic opens for a new one at once.
- After a reply the microphone stays open for follow_up_seconds, then stops uploading.
  The connection itself stays until the hub closes it (no speech for ~2 minutes, an exit
  word, the end button), so waking again within that time keeps the conversation.
- The hub keeps every model's conversation on disk (data/chat.db) and gives the next connection
  its recent part, so the page's threads survive a closed connection and a restart: they are loaded
  from the hub (/kiosk/chat/history) when not connected. Clearing them (settings, or saying
  "清除聊天记录") clears the hub's copy too.
- The screen stays on while not idle and blanks after the user's screen-off (device.Screen); the volume
  keys and the mic-mute switch are handled here too (device.Keys).
- Reports (long tasks finishing later) are polled from the hub: when idle the client chimes and
  connects so the hub reads them.
- Countdowns, alarms and reminders (alarms.py) are kept and rung here; the model only sets them
  through device tools. A ring lights the screen, switches the speaker to the alarm volume until it
  stops (the wake word, 停止 or 再睡 on the page; given up on after 10 minutes, a reminder (its chime and words over and over) after 3) and stops whatever
  the conversation was doing. The volume keys then change only that ring's volume.
- Bluetooth speaker (bt.py): a phone's music is mixed under the replies, lowered from the wake to the
  end of the reply, muted while an alarm rings (the phone is asked to pause then and is not resumed).
"""
import asyncio
import io
import json
import logging
import math
import os
import socket
import time
import urllib.parse
from pathlib import Path

import aiohttp
import numpy as np

from . import alarms, assistant, bridge, bt, config, mcp, wake_verify
from .audio import OUT_RATE, Audio, load_wav
from .device import DIM_LEVEL, Keys, Screen, normalize_screen_off
from .hub import Hub
from .kws import Kws
from .opus import Decoder, Encoder

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CALL_FLAG = Path(os.environ.get('XDG_RUNTIME_DIR', '/run/user/1000')) / 'intercom-call-active'
# after echo cancellation, above this the user is probably still talking (keeps the mic open).
# Was 0.012 (-38 dBFS): speech from across the room (~-42 dBFS in the wake word evaluation) fell
# under it, the mic closed after 10 s while the user was mid-sentence and the hub never got the
# end of it. Room noise here is ~-57 dBFS.
SPEECH_RMS = 0.005
# halt, not poweroff: with the power cable in, the board powers straight back on after a poweroff;
# halted it stays dark with the file systems unmounted, so the cable can be pulled safely
HALT = ('sudo', '-n', 'systemctl', 'halt')
WAKE_LISTEN = 8   # seconds to start talking after the wake word, whatever follow_up_seconds is
REBOOT = ('sudo', '-n', 'systemctl', 'reboot')
HALT_AFTER = 3           # seconds to read "正在关机 … 等 10 秒再拔" before the screen goes dark
MAX_LISTEN = 60          # seconds: one wake never keeps the mic open longer (a TV could otherwise)
RECOGNITION_WAIT = 15    # seconds to wait for the hub's text after the user stops (long sentences)
NORMAL_STOP_TAIL = 0.6   # a normal "tts stop" leaves less than this queued; more means aborted
PUNCT = set('，。！？；：、,.!?;:…')
HISTORY_LIMIT = 50       # messages per model shown from the hub's saved conversations
MODEL_POLL = 25          # seconds between asking the hub for its models (also refreshes hub_seen health)
MODEL_RETRY = 3          # until the hub answers (it takes over a minute to start after a boot)
ALARM_VOLUME = 70        # the alarms' own volume until the user sets one
TTS_TIMEOUT = 30         # seconds for the hub to synthesise a reminder's words
PREVIEW_SECONDS = 8      # the settings' 试听 plays at most this much of a tone
CLIP_GAP = 0.8           # seconds of silence after each spoken reminder in a ring
BACKGROUND_DUCKED = 0.2  # a phone's music (Bluetooth speaker) under a conversation

# Hub vendor id -> (logo file, company). Unknown vendors are guessed from the model name.
VENDORS = {
    'claude': ('claude-color', 'Anthropic'), 'openai': ('openai', 'OpenAI'),
    'deepseek': ('deepseek-color', 'DeepSeek'),
    'qwen': ('qwen-color', '阿里通义'), 'doubao': ('doubao-color', '字节豆包'),
    'glm': ('zhipu-color', '智谱'), 'kimi': ('kimi-color', '月之暗面'),
    'grok': ('', 'xAI'),
}
BY_MODEL = [
    (('claude',), 'claude'), (('deepseek',), 'deepseek'),
    (('qwen', 'qwq'), 'qwen'), (('doubao',), 'doubao'), (('glm',), 'glm'),
    (('kimi', 'moonshot'), 'kimi'), (('grok',), 'grok'), (('gpt', 'o1-', 'o3', 'o4-'), 'openai'),
]


def describe(option):
    vendor = option['vendor']
    if vendor not in VENDORS:
        low = (option.get('model') or '').lower()
        vendor = next((v for keys, v in BY_MODEL if any(k in low for k in keys)), vendor)
    logo, company = VENDORS.get(vendor, ('', option.get('label', '')))
    return {'kind': option['kind'], 'vendor': option['vendor'], 'label': option['label'],
            'entry': option['entry'], 'model': option.get('model', ''), 'logo': logo, 'company': company}


def marimba(notes, gap=0.1, peak=0.2):
    """Soft marimba notes as 48 kHz stereo: every prompt sound (wake, goodbye, didn't catch that,
    no hub, a new report) is from this one family. notes: Hz, one every `gap` seconds."""
    dur = 0.45
    t = np.arange(int(OUT_RATE * dur)) / OUT_RATE
    out = np.zeros(int(OUT_RATE * (gap * (len(notes) - 1) + dur)))
    for i, f in enumerate(notes):
        # a struck bar: the fundamental rings, the 4th and ~10th partials only for the strike
        x = (np.sin(2 * np.pi * f * t) * np.exp(-9 * t)
             + 0.35 * np.sin(2 * np.pi * 4 * f * t) * np.exp(-28 * t)
             + 0.12 * np.sin(2 * np.pi * 9.9 * f * t) * np.exp(-45 * t)) * np.minimum(1, t / 0.0015)
        start = int(OUT_RATE * gap * i)
        out[start:start + len(x)] += x
    out[-int(OUT_RATE * 0.015):] *= np.linspace(1, 0, int(OUT_RATE * 0.015))
    x = (out * peak / np.max(np.abs(out))).astype(np.float32)
    return np.repeat(x[:, None], 2, axis=1)


def overlay(base, extra, at):
    """`extra` mixed into `base` starting `at` seconds in (both 48 kHz stereo)."""
    start = int(OUT_RATE * at)
    out = np.zeros((max(len(base), start + len(extra)), 2), np.float32)
    out[:len(base)] += base
    out[start:start + len(extra)] += extra
    return np.clip(out, -1, 1)


ZAI_AT = 0.15   # seconds into the wake chime: just after its second note (0.09 s)
C5, E5, G5, A5, B5, C6, D6, E6 = 523.25, 659.25, 783.99, 880.0, 987.77, 1046.5, 1174.66, 1318.5


def has_default_route():
    """An IPv4 default route (/proc/net/route: destination 00000000), as a hint whether the device is online."""
    try:
        with open('/proc/net/route') as f:
            return any(line.split()[1] == '00000000' for line in f.readlines()[1:] if len(line.split()) > 1)
    except OSError:
        return True   # can't tell: don't claim it is offline


def local_ip():
    """The LAN address the device reaches the network from (the one to type into a computer's browser), or None offline.
    connect() on a UDP socket only picks the route and source address; nothing is sent."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('1.1.1.1', 53))   # a unicast address: a broadcast one is refused without SO_BROADCAST
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith('127.') or ip == '0.0.0.0' else ip

class Events:
    """Fan-out to the page's Server-Sent Events streams."""

    def __init__(self):
        self.queues = set()

    def publish(self, event, data):
        for q in list(self.queues):
            if q.qsize() < 500:
                q.put_nowait((event, data))


class Client:
    _NATIVE_SPEAKER_HOLD = True

    def __init__(self, cfg):
        self.cfg = cfg
        self.events = Events()
        self.loop = None
        self.state = 'idle'
        self.uploading = False
        self.deadline = None          # monotonic: stop listening if nothing is said by then
        self.listen_since = 0.0       # monotonic: when this wake started listening
        self.listen_mode = None       # 'realtime' (the hub's VAD ends it) or 'manual' (push-to-talk)
        self.ptt_held = False         # the mic button is held down (push-to-talk)
        self.powering_off = False     # 关机 was confirmed: the halt is on its way
        self.awaiting_text = False    # the user spoke, the mic is closed, the hub is still recognising
        self.recognising = False      # the hub heard the user stop (its VAD) and is recognising, mic still open
        self.hub_pending = False      # the hub said "vad end" and hasn't answered it yet (text or "empty")
        self.vad_ended = False        # the hub is recognising, kept while the user talks on: the wake word is listened for
        self.aborted_at = None        # monotonic: the last abort, until listening starts again (logged)
        self.abort_pending = None     # monotonic: abort sent, the hub hasn't confirmed it yet (its "tts stop")
        self.question = None          # the user's message of this turn, until its reply starts
        self.heard = False            # anything recognised since this wake (so a listening window that
                                      # times out after it is a follow-up: it ends quietly)
        self.mute_audio = False       # after an abort, drop the old reply's frames
        self.listen_after_reply = False   # the mic was closed for this reply: reopen it when played
        self.reply_samples = 0        # reply audio received since "tts start" (to tell what was heard)
        self.reply_packets = 0
        self.muted_packets = 0
        self.recognised = 0           # count of "stt" messages
        self._reply_gen = 0
        self._verifying_wake = 0
        self._brightness_set_at = 0.0
        self.threads = {}             # model entry -> messages (the hub's saved ones, then this connection's)
        self.history_loaded = False
        self.entry = None             # the model this connection talks to
        self.models = {'current': None, 'options': []}
        self.bot_msg = None
        self.decoder = None
        self._seq = 0
        self._send_q = None
        self.encoder = Encoder(16000, 1)
        self.kws = Kws(str(ROOT / 'models' / 'kws-zh'), cfg['wake_words'],
                       cfg['kws_score'], cfg['kws_threshold'], cfg['kws_paths'], cfg['kws_gain_db'])
        self.verifier = wake_verify.make(cfg['wake_verify'], cfg['wake_words'], cfg['hub_http'])
        self.backlog = None           # mic blocks since a wake word hit, uploaded once listening starts
        self.audio = Audio(self._on_block)
        self.hub = Hub(cfg['hub_ws'], cfg['device_id'], cfg['client_id'], cfg['token'],
                       self._on_json, self._on_audio, self._on_closed)
        sounds = ROOT / 'web' / 'sounds'
        # Wake: two rising marimba notes; goodbye is the same two falling
        self.sounds = {'wake': marimba([G5, D6], 0.09), 'bye': marimba([D6, G5], 0.12, 0.16),
                       'miss': marimba([E6, C6], 0.12, 0.16), 'error': marimba([A5, E5, C5], 0.14)}
        self.sounds['wake_plain'] = self.sounds['wake']
        if (sounds / 'zai.wav').exists():
            # the one-character "在" comes in as the second note rings, over its tail: played after
            # the whole chime it came ~0.4 s later
            self.sounds['wake'] = overlay(self.sounds['wake'], load_wav(sounds / 'zai.wav'), ZAI_AT)
        self.speech_end = 0.0         # monotonic: last time the user was heard talking
        self.tools_listed = asyncio.Event()   # the hub asked for our device tools on this connection
        self.assistant_lock = asyncio.Lock()  # one change of the wake word settings at a time
        self.connecting = asyncio.Lock()      # one connection: a second wake while connecting waits for it
        self.preview_lock = asyncio.Lock()
        self.display = None
        self._silent_close = False
        self.screen = Screen()
        level = cfg.get('brightness')
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            log.warning('invalid brightness %r, using 100', level)
            level = 100
        self.screen.level = max(0, min(100, int(level)))
        try:
            self.screen_off = normalize_screen_off(cfg.get('screen_off'))
        except ValueError:
            log.warning('invalid screen_off %r, using the default', cfg.get('screen_off'))
            self.screen_off = normalize_screen_off(None)
        self.screen.idle = self._screen_off_seconds()   # written by start()'s release()
        self.hub_seen = 0.0           # monotonic: last answer from the hub's HTTP side (health check)
        self.keys = Keys(on_step=self.volume_step, on_mic=self._on_mic)
        self.volume = None            # the device volume 0-100 (keys, voice, the page), as last read or set
        self.volume_lock = asyncio.Lock()   # one change of the speaker's volume at a time
        self.held_volume = None       # a ring or a tone preview has the speaker: the device volume to put back
        self.alarms = alarms.Alarms()
        self.clock_ok = False         # the system clock is synchronised: alarms are judged
        self.ring = None              # the ring going on: {'items', 'notes', 'started', 'volume', 'stop', 'task'}
        self.preview = None           # task playing a ring tone from the settings
        level = cfg.get('alarm_volume', ALARM_VOLUME)
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            log.warning('invalid alarm_volume %r, using %d', level, ALARM_VOLUME)
            level = ALARM_VOLUME
        self.alarm_volume = max(0, min(100, int(level)))
        self.alarm_tone = cfg.get('alarm_tone') if cfg.get('alarm_tone') in alarms.TONES else alarms.DEFAULT_TONE
        self.bt = bt.Bluetooth(self.audio.background, self.events.publish, cfg.get('bluetooth') is not False)
        self.airplay_state = None
        self.airplay_lock = asyncio.Lock()

    # ---- startup ----

    async def start(self):
        self.loop = asyncio.get_running_loop()
        if self._send_q is None:
            self._send_q = asyncio.Queue(maxsize=200)
        self.audio.start()
        self.keys.start()
        self.screen.release()   # a hold left by a previous run must not keep the screen on
        asyncio.create_task(self._sender())
        asyncio.create_task(self._ticker())
        asyncio.create_task(self._model_poller())
        asyncio.create_task(self._alarm_loop())
        asyncio.create_task(self.refresh_airplay())
        await self.bt.start()

    # ---- microphone (worker thread) ----

    @staticmethod
    def _intercom_active():
        return CALL_FLAG.exists()

    def _on_block(self, block, wake_block):
        # While streaming to the hub the wake word isn't needed (speech already reaches it), and
        # the CPU it would take is better left to the hub's speech recognition. wake_block: what the
        # wake word hears (during a reply, echo-cancelled without suppressing the voice; audio.py)
        # (after the hub's VAD said the user stopped, the wake word interrupts the recognition)
        if self._intercom_active():
            self.backlog = None
            return
        kws = self.kws
        if kws is None:
            self.backlog = None
            return
        hit = None if self.uploading and not self.vad_ended else kws.feed(wake_block)
        backlog = self.backlog
        if hit:
            log.info('wake word: %s', hit)
            if self.vad_ended:
                self.uploading = False   # keep what follows for the new question, not the old one
            # keep what follows: checking the hit, the chime and connecting take a moment, and
            # "小麦小麦，今天..." is often said in one go
            self.backlog = []
            audio = kws.recent()
            self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self.voice_wake(hit, audio)))
        elif backlog is not None and not self.uploading:
            backlog.append(block)
            del backlog[:-200]   # 4 s, what the send queue takes at once
        elif self.uploading:
            self.backlog = None
            pkts = [
                self.encoder.encode(np.clip(b * 32768, -32768, 32767).astype(np.int16))
                for b in (backlog or []) + [block]
            ]
            if len(pkts) == 1:
                self.loop.call_soon_threadsafe(self._queue_packet, pkts[0])
            elif pkts:
                self.loop.call_soon_threadsafe(self._queue_packets, pkts)

    def _queue_packet(self, packet):
        if not self._send_q.full():
            self._send_q.put_nowait(packet)

    def _queue_packets(self, packets):
        q = self._send_q
        for p in packets:
            if q.full():
                break
            q.put_nowait(p)

    async def _sender(self):
        while True:
            packet = await self._send_q.get()
            if self.uploading:
                try:
                    await self.hub.send_audio(packet)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception('sender error')

    # ---- state ----

    def set_state(self, state):
        if state != self.state:
            self.state = state
            self.recognising = False
            log.info('state: %s', state)
            # only on a real change: set_state(self.state) just refreshes the page
            if state == 'idle':
                self.screen.release()
            else:
                self.screen.hold()
        self.audio.aec_hold = self.state != 'idle'
        self.events.publish('state', self.status())

    def status(self):
        return {'state': self.state, 'connected': self.hub.connected, 'uploading': self.uploading,
                'recognising': self.recognising or self.awaiting_text,
                'mic': self.keys.mic_on,
                'brightness': self.brightness(), 'screen_off': self.screen_off,
                'volume': self.volume}

    def mic_off_toast(self):
        if self.keys.mic_on:
            return False
        self.play('miss')
        self.events.publish('toast', {'kind': 'info', 'text': '麦克风已关，拨开机身侧面的开关就能说话'})
        return True

    def _on_mic(self, on):
        log.info('mic switch: %s', 'on' if on else 'off')
        if not on:
            self.ptt_held = False
        self.events.publish('toast', {'kind': 'info', 'text': '麦克风已打开' if on else '麦克风已关'})
        if not on and self.uploading:
            asyncio.create_task(self.stop_uploading())
        elif not on and self.state == 'connecting' and not self.in_turn():
            self._reply_gen += 1
            self.set_state('idle')
        else:
            self.set_state(self.state)

    def health(self):
        """For the updater's self-test after installing a version (thinksmart-update)."""
        return {'audio': bool(self.audio._streams) and all(s.active for s in self.audio._streams),
                'hub': self.hub_seen > 0 and time.monotonic() - self.hub_seen < 30}

    def snapshot(self):
        out = {'status': self.status(), 'models': self.models, 'alarms': self.alarm_view(), 'bluetooth': self.bt.view(),
               'thread': {'entry': self.view_entry(), 'messages': self.threads.get(self.view_entry(), [])}}
        if getattr(self, 'airplay_state', None) is not None:
            out['airplay'] = self.airplay_state
        return out

    def view_entry(self):
        cur = self.models.get('current')
        return self.entry or (cur['entry'] if cur else None)

    def play(self, name):
        sound = self.sounds.get(name)
        if sound is not None:
            self.audio.player.push(sound)

    # ---- actions (from the wake word and the page) ----

    async def ensure_connected(self, quiet=False):
        async with self.connecting:
            return await self._connect(quiet=quiet)

    async def _connect(self, quiet=False):
        if getattr(self.hub, '_closing', False):
            await self.hub.close()
        if self.hub.connected:
            return True
        if not quiet:
            self.set_state('connecting')
        self.tools_listed.clear()
        try:
            await self.hub.connect()
        except Exception as e:
            log.warning('cannot reach the hub: %s', e)
            if not quiet:
                self.play('error')
                self.events.publish('toast', {'kind': 'error', 'text': '连不上小智网关，请检查网关是否在运行'})
                self.set_state('idle')
            return False
        self.decoder = None
        # a new connection has no old reply to drop: the hub's own announcements (reports) send no
        # "tts start", so a mute left over from the last abort/end would silence them
        self.mute_audio = False
        if quiet and self.state == 'idle':
            self.events.publish('state', self.status())
        return True

    async def voice_wake(self, label, audio):
        """The wake word model fired. With a second look (wake_verify.py), which takes 1-2 s here,
        the chime still comes at once, but connecting and listening wait for its answer: what is
        said meanwhile is kept (backlog) and sent on, and a false wake ends quietly instead of
        with the hub answering the TV or a "didn't catch that"."""
        my_backlog = self.backlog
        if self._intercom_active():
            log.info('voice_wake suppressed: intercom call active')
        elif not self.verifier or self.ring:
            await self.wake('voice')
        elif self.in_turn():
            # don't cut a reply off for what may not be the wake word
            gen = self._reply_gen
            if (await self._check_wake(label, audio))['ok'] and gen == self._reply_gen and self.in_turn():
                await self.wake('voice')
        elif not self.mic_off_toast():
            self.chime('voice')
            self.screen.poke()
            self.set_state('connecting')
            self._reply_gen += 1
            gen = self._reply_gen
            if not self.hub.connected and hasattr(self, 'ensure_connected') and callable(getattr(self.hub, 'connect', None)):
                asyncio.create_task(self.ensure_connected(quiet=True))
            self._verifying_wake = getattr(self, '_verifying_wake', 0) + 1
            try:
                ok = (await self._check_wake(label, audio))['ok']
            finally:
                self._verifying_wake = max(0, getattr(self, '_verifying_wake', 1) - 1)
            if self.ptt_held or gen != self._reply_gen:
                pass
            elif ok and self.state == 'connecting':
                await self.wake('voice', chimed=True)
            elif self.state == 'connecting':
                log.info('not the wake word after all: back to idle')
                self.set_state('idle')
        if not self.uploading and self.backlog is my_backlog:
            self.backlog = None

    async def _check_wake(self, label, audio):
        return await asyncio.get_running_loop().run_in_executor(None, self.verifier.check, audio, label)

    def chime(self, source):
        kind = self.cfg.get('wake_sound', 'zai')
        if kind != 'off':
            self.play('wake_plain' if kind == 'chime' else 'wake')
        self.events.publish('woke', {'source': source})

    def in_turn(self):
        """The hub is recognising, thinking or speaking: the wake word interrupts it."""
        return (self.state in ('thinking', 'speaking') or self.vad_ended) and self.hub.connected

    async def wake(self, source='button', chimed=False):
        if self._intercom_active():
            log.info('wake (%s) suppressed: intercom call active', source)
            if self.state == 'connecting' and not self.in_turn():
                self.set_state('idle')
            return
        if self.ring:
            await self.stop_alarm()   # the wake word stops a ring (no snooze: "再睡 10 分钟" is said next)
        if self.preview:
            await self._stop_preview()   # a tone being tried: the answer must not queue behind it
        if self.mic_off_toast():
            if self.state == 'connecting' and not self.in_turn():
                self.set_state('idle')
            return
        if self.in_turn():
            await self.abort('wake_word_detected')
        self._reply_gen += 1
        gen = self._reply_gen
        if not chimed:
            self.chime(source)
        self.screen.poke()
        if not await self.ensure_connected():
            return
        if gen != self._reply_gen:
            return
        if self.ring or self._intercom_active() or not self.keys.mic_on:
            if self.state == 'connecting':
                self.set_state('idle')
            return
        if not await self.start_listening('realtime'):
            if gen == self._reply_gen and self.state == 'connecting':
                self.set_state('idle')
            return
        self.heard = False
        self.deadline = time.monotonic() + max(WAKE_LISTEN, self.cfg['follow_up_seconds'])

    async def start_listening(self, mode):
        if not await self.hub.send({'type': 'listen', 'state': 'start', 'mode': mode}) or not self.hub.connected:
            return False
        self.uploading = True
        self.listen_mode = mode
        self.vad_ended = False
        self.listen_since = time.monotonic()
        self.awaiting_text = False
        self.hub_pending = False
        if self.aborted_at is not None:
            log.info('listening again %.0f ms after the abort', (self.listen_since - self.aborted_at) * 1000)
            self.aborted_at = None
        self.set_state('listening')
        return True

    async def stop_uploading(self, missed=False):
        self.uploading = False
        self.deadline = None
        self.vad_ended = self.hub_pending = self.awaiting_text = self.recognising = False
        if missed:
            self.play('miss')
            ww0 = assistant.label_of((self.cfg.get('wake_words') or ['你好小智'])[0]) or '你好小智'
            self.events.publish('toast', {'kind': 'info', 'text': f'没听清，可以再说一次「{ww0}」'})
        self.set_state('idle')

    async def finish_follow_up(self):
        """Silently end an idle follow-up listening window without closing the hub session.
        Refuses if the user is actively/recently speaking or recognition is in flight."""
        now = time.monotonic()
        recently_spoke = (
            self.speech_end > self.listen_since + 0.4
            and (now - self.speech_end) < 1.8
        )
        if (
            self.state != 'listening'
            or not self.uploading
            or not self.heard
            or self.recognising
            or self.vad_ended
            or self.hub_pending
            or recently_spoke
        ):
            return False
        await self.stop_uploading(missed=False)
        return True

    async def abort(self, reason=None):
        self._reply_gen += 1
        now = time.monotonic()
        phase = 'recognising' if self.vad_ended or self.awaiting_text else self.state
        # how much of this reply was actually heard: the hub keeps only that in the context
        played = max(self.reply_samples / OUT_RATE - self.audio.player.buffered(), 0)
        msg = {'type': 'abort', 'played_ms': int(played * 1000)}
        if reason:
            msg['reason'] = reason
        self.audio.player.trim(0)
        self.mute_audio = True
        self.listen_after_reply = False
        if phase == 'recognising':
            # what was just said is dropped (the hub drops it too): stop sending the rest of it
            self.uploading = False
        self.vad_ended = self.recognising = self.awaiting_text = self.hub_pending = False
        self.aborted_at = self.abort_pending = now
        log.info('abort while %s (%s)', phase, reason or 'button')
        await self.hub.send(msg)
        if self.bot_msg:
            self.bot_msg['interrupted'] = True
            self.publish_message(self.bot_msg)
            self.bot_msg = None
        elif self.question:
            # interrupted before any answer: the hub leaves the question out of the context
            self.question['interrupted'] = True
            self.publish_message(self.question)
        self.question = None
        self.set_state('listening' if self.uploading else 'idle')

    async def end(self):
        """End the conversation: stop everything and close the channel, which clears the hub's context."""
        self._reply_gen += 1
        self.ptt_held = False
        if self.in_turn():
            await self.abort()
        self.uploading = self.listen_after_reply = False
        self.vad_ended = self.recognising = self.awaiting_text = self.hub_pending = False
        self.deadline = None
        await self.hub.close()
        self.set_state('idle')

    async def say(self, text):
        """Ask in text instead of voice (the hub answers it like recognised speech)."""
        if not isinstance(text, str):
            text = '' if text is None else str(text)
        text = text.strip()
        if not text:
            return
        if self.in_turn():
            await self.abort()
        else:
            self._reply_gen += 1
            self.uploading = self.listen_after_reply = False
            self.vad_ended = self.recognising = self.awaiting_text = self.hub_pending = False
            self.deadline = None
        gen = self._reply_gen
        if not await self.ensure_connected():
            return
        try:
            # a question sent right after connecting reaches the model before our tool list does,
            # and then "关掉睡眠模式" can't call anything; speech takes longer than this anyway
            await asyncio.wait_for(self.tools_listed.wait(), 2)
        except asyncio.TimeoutError:
            pass
        if gen != self._reply_gen:
            return
        if not self.hub.connected or getattr(self, 'ring', None) or (hasattr(self, '_intercom_active') and self._intercom_active()):
            if self.state == 'connecting':
                self.set_state('idle')
            return   # ended, superseded, or dropped while we waited: stay out of the newer turn's way
        sent = await self.hub.send({'type': 'listen', 'state': 'detect', 'text': text})
        if sent is False or not getattr(self.hub, 'connected', True):
            if gen == self._reply_gen and self.state == 'connecting':
                self.set_state('idle')
            return
        if gen != self._reply_gen:
            return
        self.set_state('thinking')

    async def ptt_start(self):
        if self._intercom_active():
            log.info('ptt_start suppressed: intercom call active')
            return
        if self.mic_off_toast():
            return
        self.ptt_held = True
        if self.in_turn():
            await self.abort()
        else:
            self._reply_gen += 1
            self.uploading = self.listen_after_reply = False
            self.vad_ended = self.recognising = self.awaiting_text = self.hub_pending = False
            self.deadline = None
        gen = self._reply_gen
        if not await self.ensure_connected():
            self.ptt_held = False
            return
        if gen != self._reply_gen:
            self.ptt_held = False
            return
        if (
            not self.hub.connected
            or getattr(self, 'ring', None)
            or (hasattr(self, '_intercom_active') and self._intercom_active())
            or not self.keys.mic_on
        ):
            self.ptt_held = False
            if self.state == 'connecting':
                self.set_state('idle')
            return
        if not self.ptt_held:
            # let go while we were connecting: nothing would ever end a manual listen, so take it
            # as a tap (listen with the hub's VAD)
            await self.wake('button')
            return
        if not await self.start_listening('manual'):
            self.ptt_held = False
            if gen == self._reply_gen and self.state == 'connecting':
                self.set_state('idle')
            return
        if gen != self._reply_gen:
            return
        self.heard = False
        self.deadline = None
        if not self.ptt_held:
            await self.ptt_stop()

    async def ptt_stop(self):
        self.ptt_held = False
        if self.uploading and self.listen_mode == 'manual':
            sent = await self.hub.send({'type': 'listen', 'state': 'stop'})
            self.uploading = False
            if sent is False or not getattr(self.hub, 'connected', True):
                if self.state == 'listening':
                    self.set_state('idle')
                return
            self.awaiting_text = True   # the hub's "nothing recognised" ends it at once
            self.set_state('thinking')
            self.deadline = time.monotonic() + 8   # back to idle if nothing comes back

    # ---- from the hub ----

    def _stale(self, msg):
        """Sent by the hub before it handled our abort (it confirms with "tts stop"): the old turn's
        question or the start of its reply, arriving late. 3 s in case the confirmation got lost."""
        if self.abort_pending is None:
            return False
        if time.monotonic() - self.abort_pending > 3:
            self.abort_pending = None
            return False
        kind, state = msg.get('type'), msg.get('state')
        if kind == 'tts' and state == 'stop' and msg.get('interrupted'):
            log.info('the hub confirmed the abort after %.0f ms', (time.monotonic() - self.abort_pending) * 1000)
            self.abort_pending = None
            return True   # our own abort: what comes next was already decided
        if kind == 'stt' or (kind == 'tts' and state in ('start', 'sentence_start')):
            log.info('dropped from the interrupted turn: %s %s', kind, state or msg.get('text'))
            return True
        return False

    async def _on_json(self, msg):
        kind = msg.get('type')
        if self._stale(msg):
            return
        if kind == 'stt':
            text = (msg.get('text') or '').strip()
            if text.startswith('% '):
                # not speech: the hub's note that the model calls a tool (for the screens of ESP32 boards)
                raw_tool = text[2:].strip()
                log.info('the model calls %s', raw_tool)
                self.events.publish('tool', {
                    'name': (raw_tool.split()[0] if raw_tool else ''),
                    'raw': raw_tool[:240],
                    'source': 'hub',
                    'time': time.time(),
                })
            elif text:
                if self.speech_end > self.listen_since:
                    log.info('recognised %.1f s after the user stopped talking: %s',
                             time.monotonic() - self.speech_end, text)
                else:
                    # said before this listening began (right after the wake word, in its backlog):
                    # the last time we heard talking belongs to an earlier turn
                    log.info('recognised: %s', text)
                # a long sentence the client's deadline closed the mic on is still a spoken question:
                # listen again after its reply too (not after push-to-talk, which never does)
                late = self.awaiting_text and self.listen_mode == 'realtime'
                self.heard = True
                self.awaiting_text = False
                self.recognising = self.vad_ended = False
                self.hub_pending = False
                self.deadline = None
                self.recognised += 1
                self.reply_samples = 0    # an abort before this turn's audio has heard nothing of it
                if late:
                    self.listen_after_reply = True
                if self.uploading:
                    # nothing said during the reply counts; the wake word is listened for instead
                    self.uploading = False
                    self.listen_after_reply = True
                # on the page right away: asking the hub for its models first took 0.7-2 s
                m = self.question = self.add_message('user', text)
                self.set_state('thinking')
                asyncio.create_task(self._refresh_after_question(m))
        elif kind == 'vad':
            # VAD events, a feature asked for in our hello. end: the hub decided the user finished and
            # is recognising; empty: it recognised nothing (a cough, too short, only echo)
            state = msg.get('state')
            if state == 'end':
                self.hub_pending = True
                if self.uploading and not self.recognising:
                    self.recognising = self.vad_ended = True
                    self.set_state(self.state)
            elif state == 'empty':
                self.hub_pending = self.vad_ended = False
                log.info('the hub recognised nothing')
                if self.awaiting_text:
                    # the mic is closed and this was all we were waiting for ("didn't catch that"
                    # after a wake; a follow-up window ends quietly, as when nobody talks in it)
                    self.awaiting_text = False
                    await self.stop_uploading(missed=not self.heard)
                elif self.recognising:
                    # the mic is still open: back to listening
                    self.recognising = False
                    self.set_state(self.state)
        elif kind == 'tts':
            state = msg.get('state')
            if state != 'sentence_start':
                log.info('reply %s (%.1f s queued)', state, self.audio.player.buffered())
            if state == 'stop':
                player = self.audio.player
                log.info('reply audio: %d packets (%d dropped muted), %.2f s; buffer ran dry %d times (last sample %s); '
                         'speaker callback flags %s', self.reply_packets, self.muted_packets, self.reply_samples / OUT_RATE,
                         len(player.ran_out), ' '.join(f'{v:.3f}' for v in player.ran_out[-8:]),
                         player.late[-8:])
                levels = self.audio.take_echo_levels()
                if levels:
                    log.info('echo while it played: mic %s dBFS, cancelled %s, for the wake word %s', *levels)
            if state == 'start':
                # The hub says "start" as soon as it has the question; the model is still
                # thinking until the first sentence arrives
                self._reply_gen += 1
                self.mute_audio = False
                self.decoder = None
                self.bot_msg = None
                self.reply_samples = self.reply_packets = self.muted_packets = 0
                self.audio.player.ran_out.clear()
                self.audio.player.late.clear()
                self.deadline = None
                self.set_state('thinking')
            elif state == 'sentence_start':
                text = (msg.get('text') or '').strip()
                if self.mute_audio and self.abort_pending is None:
                    # the hub confirmed our abort, so this is something new it says on its own: a
                    # report (e.g. the interrupted Grok Bot turn finished) sends no "tts start" to unmute
                    self.mute_audio = False
                if self.uploading and self.listen_mode == 'realtime' and not self.mute_audio:
                    # the hub speaks on its own while we listen (a pending report: no stt, no
                    # "tts start" before it). Like any reply, only the wake word interrupts it,
                    # and the follow-up window opens after it
                    self.uploading = False
                    self.listen_after_reply = True
                    self.recognising = self.vad_ended = self.hub_pending = False
                    self.deadline = None
                if not self.mute_audio:
                    if self.state != 'speaking':
                        self._reply_gen += 1
                    self.set_state('speaking')
                if text and not self.mute_audio:
                    if self.bot_msg is None:
                        self.bot_msg = self.add_message('bot', text)
                        self.question = None
                    else:
                        # the hub sends sentences with their trailing punctuation stripped
                        prev = self.bot_msg['text']
                        self.bot_msg['text'] = prev + ('' if prev[-1:] in PUNCT else '，') + text
                        self.publish_message(self.bot_msg)
            elif state == 'stop':
                # the hub cut the reply off (a newer question); an older hub doesn't say so, but then
                # more than the usual tail is still queued
                if msg.get('interrupted', self.audio.player.buffered() > NORMAL_STOP_TAIL):
                    self.audio.player.trim(0)
                    if self.bot_msg:
                        self.bot_msg['interrupted'] = True
                        self.publish_message(self.bot_msg)
                elif self.question is not None and self.bot_msg is None and not self.mute_audio:
                    # over without a word: the model and the hub's spoken apology both failed to reach
                    # their servers (no network yet after a boot, an outage); say so on the page
                    log.warning('the turn ended without a reply')
                    self.events.publish('toast', {'kind': 'error', 'text': '网络还没连上，稍后再试' if not has_default_route()
                                                  else '没收到回答，可能是网络或模型服务连不上，稍后再试'})
                self.bot_msg = self.question = None
                if self.listen_after_reply:
                    asyncio.create_task(self._listen_after_reply(self._reply_gen))
                elif self.uploading:
                    self.heard = True
                    self.deadline = time.monotonic() + max(3, self.cfg['follow_up_seconds'])
                    self.set_state('listening')
                else:
                    self.set_state('idle')
        elif kind == 'mcp':
            payload = msg.get('payload') or {}
            reply = await mcp.handle(payload, client=self)
            if reply:
                await self.hub.send({'type': 'mcp', 'payload': reply})
                if payload.get('method') == 'tools/call':
                    params = payload.get('params') or {}
                    t_name = str(params.get('name') or '')
                    raw_args = params.get('arguments')
                    if isinstance(raw_args, str):
                        try:
                            raw_args = json.loads(raw_args)
                        except Exception:
                            raw_args = {}
                    res_obj = (reply.get('result') or {}) if isinstance(reply, dict) else {}
                    c_list = res_obj.get('content') if isinstance(res_obj, dict) else None
                    t_txt = str((c_list[0] or {}).get('text') or '')[:600] if isinstance(c_list, list) and c_list else ''
                    self.events.publish('tool', {
                        'name': t_name,
                        'args': raw_args if isinstance(raw_args, dict) else {},
                        'result': t_txt,
                        'error': bool(res_obj.get('isError')) if isinstance(res_obj, dict) else False,
                        'source': 'mcp',
                        'time': time.time(),
                    })
            if payload.get('method') == 'tools/list':
                self.tools_listed.set()
        elif kind == 'chat_history' and msg.get('state') == 'cleared':
            # said "清除聊天记录" and confirmed: the hub has forgotten everything
            self.threads = {}
            self.bot_msg = self.question = None
            self.events.publish('thread', {'entry': self.view_entry(), 'messages': [], 'reset': True})
        elif kind == 'server':
            log.info('server message: %s', msg)
            if msg.get('action') == 'reconnect_idle':
                self._silent_close = True
                asyncio.create_task(self.hub.close())

    async def _refresh_after_question(self, m):
        """The hub may have switched models for this question (called one by name, or changed in its
        admin page): move the question, and anything said after it, to that model's thread."""
        old = m['entry']
        await self.refresh_models()
        thread = self.threads.get(old, [])
        if self.entry == old or m not in thread:
            return
        moved = thread[thread.index(m):]
        del thread[thread.index(m):]
        for x in moved:
            x['entry'] = self.entry
        self.threads.setdefault(self.entry, []).extend(moved)
        self.events.publish('thread', {'entry': self.entry, 'messages': self.threads[self.entry]})

    async def _listen_after_reply(self, gen=None):
        """Reopen the mic for a follow-up once the last of the reply has been played (uploading
        earlier would send the hub its tail)."""
        give_up = time.monotonic() + 3
        while self.audio.player.buffered() > 0.02 and time.monotonic() < give_up:
            if gen is not None and gen != self._reply_gen:
                return
            await asyncio.sleep(0.05)
        if not self.listen_after_reply or not self.hub.connected or (gen is not None and gen != self._reply_gen):
            return   # woken, aborted or ended meanwhile: that already decided what comes next
        self.listen_after_reply = False
        if not self.keys.mic_on or self.cfg['follow_up_seconds'] <= 0:
            # switched off during the reply: nothing would be heard, don't show "listening"
            # (or the user doesn't want it to listen on: 设置 → 小麦 → 回答完继续听 = 不听)
            self.set_state('idle')
            return
        recognised = self.recognised
        if not await self.start_listening('realtime'):
            return
        if self.recognised != recognised:
            # the hub cut this reply off for a newer question that arrived while we reopened
            self.uploading, self.listen_after_reply = False, True
            self.set_state('thinking')
            return
        self.heard = True
        self.deadline = time.monotonic() + self.cfg['follow_up_seconds']

    def _on_audio(self, packet):
        if self.mute_audio:
            self.muted_packets += 1
            return
        if self.decoder is None:
            # Opus decodes to any rate whatever it was encoded at: straight to the output rate
            self.decoder = Decoder(OUT_RATE, 1)
        try:
            mono = self.decoder.decode(packet)
            self.reply_samples += len(mono)
            self.reply_packets += 1
            self.audio.player.push(np.repeat(mono[:, None], 2, axis=1))
        except Exception:
            log.exception('cannot decode reply audio')

    async def _on_closed(self):
        # the conversation is over (the hub's idle timeout, an exit word, the end button)
        verifying = getattr(self, '_verifying_wake', 0) > 0 and self.state == 'connecting' and not self.uploading
        if not verifying:
            self._reply_gen += 1
        silent, self._silent_close = self._silent_close, False
        if not silent and not verifying:
            self.play('bye')
        self.uploading = False
        self.vad_ended = False
        self.abort_pending = self.question = None
        self.listen_after_reply = False
        if not verifying:
            self.deadline = None
        self.awaiting_text = False
        self.mute_audio = False
        self.bot_msg = None
        self.entry = None
        if not verifying:
            self.set_state('idle')
        self.events.publish('thread', {'entry': self.view_entry(), 'messages': self.threads.get(self.view_entry(), [])})
        # the hub's copy has interrupted replies cut to what was heard: show that one
        asyncio.create_task(self.load_history())

    # ---- conversation threads (one per model, like the hub keeps them) ----

    def add_message(self, role, text):
        entry = self.view_entry()
        if entry != self.entry:
            self.entry = entry
            self.events.publish('thread', {'entry': entry, 'messages': self.threads.get(entry, [])})
        self._seq += 1
        m = {'id': self._seq, 'role': role, 'text': text, 'time': time.time(), 'entry': entry}
        self.threads.setdefault(entry, []).append(m)
        del self.threads[entry][:-200]
        self.publish_message(m)
        return m

    def publish_message(self, m):
        self.events.publish('message', m)

    async def load_history(self):
        """Show the recent part of the conversations the hub saved. Not while connected: this
        connection's messages are being added then (it reloads once the connection closes)."""
        if self.hub.connected:
            return
        try:
            data = await self._hub_http('/kiosk/chat/history?' + urllib.parse.urlencode(
                {'device': self.cfg['device_id'], 'limit': HISTORY_LIMIT}))
        except Exception:
            return   # the hub is down or too old to keep conversations
        if not data.get('ok') or self.hub.connected:
            return
        self.threads = {entry: [{'id': f"h{m['id']}", 'role': m['role'], 'text': m['text'], 'time': m['time'],
                                 'entry': entry, **({'interrupted': True} if m.get('interrupted') else {})}
                                for m in items]
                        for entry, items in (data.get('models') or {}).items()}
        self.history_loaded = True
        self.events.publish('thread', {'entry': self.view_entry(), 'messages': self.threads.get(self.view_entry(), []),
                                       'reset': True})

    async def power_off(self):
        """The settings' 关机: end any conversation and halt, so the power can be unplugged without
        leaving /boot (ext2, mounted read-write) to be repaired on the next boot."""
        if self.powering_off:
            return {'ok': True}   # a second tap while the first one is still on its way
        self.powering_off = True
        self.events.publish('power', {'state': 'off'})   # cover the page before ending a conversation (up to 3 s)
        if self.hub.connected:
            await self.end()
        asyncio.create_task(self._halt())
        return {'ok': True}

    async def reboot(self):
        """The settings' 重启设备: as 关机, then the system comes back by itself."""
        if self.powering_off:
            return {'ok': True}
        self.powering_off = True
        self.events.publish('power', {'state': 'reboot'})
        if self.hub.connected:
            await self.end()
        asyncio.create_task(self._halt(REBOOT))
        return {'ok': True}

    async def _halt(self, command=HALT):
        await asyncio.sleep(HALT_AFTER)
        log.info('%s the system (asked in the settings)', 'rebooting' if command is REBOOT else 'halting')
        try:
            p = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.PIPE)
            _, err = await p.communicate()
            failed = p.returncode != 0 and (err.decode(errors='replace').strip() or f'exit {p.returncode}')
        except OSError as e:
            failed = str(e)
        if failed:
            log.error('cannot halt or reboot: %s', failed)
            self.powering_off = False
            self.events.publish('power', {'state': 'failed'})
            self.events.publish('toast', {'kind': 'error', 'text': '没能重启，请稍后再试' if command is REBOOT else '没能关机，请稍后再试'})

    # ---- wake word and listening settings (the settings page; assistant.py) ----

    async def set_assistant(self, changes):
        """Change them now, without a restart; raises ValueError. The keyword spotter reads its
        keywords and scores only when built: build the new one in a worker thread (1-2 s of CPU), then
        swap it in; the microphone thread uses whichever is there."""
        async with self.assistant_lock:
            disk_words = []
            try:
                if config.PATH.exists() and not config.PATH.is_symlink():
                    disk_cfg = json.loads(config.PATH.read_text(encoding='utf-8'))
                    if isinstance(disk_cfg, dict) and isinstance(disk_cfg.get('wake_words'), list):
                        disk_words = [w for w in disk_cfg['wake_words'] if isinstance(w, str)]
            except (OSError, ValueError):
                pass
            patch = assistant.normalize(self.cfg, changes, extra_known=disk_words)
            if not patch:
                return assistant.view(self.cfg)
            cfg = {**self.cfg, **patch}
            kws = None
            if any(k in patch for k in assistant.REBUILD):
                kws = await asyncio.get_running_loop().run_in_executor(None, lambda: Kws(
                    str(ROOT / 'models' / 'kws-zh'), cfg['wake_words'], cfg['kws_score'], cfg['kws_threshold'],
                    cfg['kws_paths'], cfg['kws_gain_db']))
            if 'wake_verify' in patch or 'wake_words' in patch:
                verifier = wake_verify.make(cfg['wake_verify'], cfg['wake_words'], cfg['hub_http'])
                self.verifier = verifier
            config.save(**patch)
            self.cfg.update(patch)
            if kws is not None:
                self.kws = kws
            elif 'kws_gain_db' in patch:
                self.kws.set_gain(cfg['kws_gain_db'])
            if 'wake_sound' in patch and cfg['wake_sound'] != 'off' and self.state == 'idle' and not self.ring:
                self.play('wake_plain' if cfg['wake_sound'] == 'chime' else 'wake')   # let it be heard
            log.info('assistant settings: %s', patch)
            return assistant.view(self.cfg)

    async def clear_history(self, memory_only=False):
        """Clear every model's conversation on the hub (and the page). memory_only: just the summary
        the hub made of the older part, the messages stay."""
        try:
            result = await self._hub_http('/kiosk/chat/clear', {'device': self.cfg['device_id'],
                                                                'memory_only': bool(memory_only)})
        except Exception as e:
            log.warning('cannot clear the chat history: %s', e)
            return {'ok': False, 'error': '连不上小智网关'}
        if result.get('ok') and not memory_only:
            self.threads = {}
            self.bot_msg = self.question = None
            self.events.publish('thread', {'entry': self.view_entry(), 'messages': [], 'reset': True})
        return result

    # ---- screen brightness (the blanker sets the backlight: device.Screen, idle-blanker.py) ----

    def brightness(self):
        if self.display is not None and (time.monotonic() - getattr(self, '_brightness_set_at', 0.0)) >= 2.0:
            try:
                snap = self.display.snapshot()
                if isinstance(snap, dict) and snap.get('level') is not None:
                    return {'level': int(snap['level']), 'dimmed': bool(snap.get('dimmed'))}
            except Exception:
                pass
        return {'level': self.screen.level, 'dimmed': False}

    async def set_brightness(self, level=None, change=None):
        """Page or voice: level (0-100) or change (relative, from the level now)."""
        if level is None:
            if change is None:
                raise ValueError('level or change is needed')
            if isinstance(change, bool):
                raise ValueError('change must be a number')
            f_val = float(change)
            if not math.isfinite(f_val):
                raise ValueError('change must be finite')
            level = self.brightness()['level'] + int(f_val)
        else:
            if isinstance(level, bool):
                raise ValueError('level must be a number')
            f_val = float(level)
            if not math.isfinite(f_val):
                raise ValueError('level must be finite')
            level = int(f_val)
        clamped = max(0, min(100, level))
        self._brightness_set_at = time.monotonic()
        self.screen.brightness(clamped, force=True)
        self.screen.dim(False)
        config.save(brightness=self.screen.level)
        if self.display is not None:
            try:
                self.display.save({'auto': False})
                self.display.clear_preview()
            except Exception:
                pass
        log.info('brightness: %s', self.screen.level)
        self.set_state(self.state)
        return self.brightness()

    # ---- screen page navigation (home / sub-pages / settings via SSE to 127.0.0.1:8090) ----

    def navigate_screen(self, page='home', section=None, city=None):
        raw = str(page or 'home').strip().lower()
        sec = str(section or '').strip().lower()
        page_aliases = {
            'home': 'home', 'main': 'home', 'index': 'home', 'desktop': 'home', 'dashboard': 'home',
            'back': 'home', 'close': 'home', 'exit': 'home', 'close_ui': 'home',
            '主页': 'home', '主界面': 'home', '我们主页': 'home', '我们的主页': 'home', '主屏幕': 'home',
            '主屏': 'home', '首页': 'home', '桌面': 'home', '返回': 'home', '退出': 'home', '关闭': 'home',
            'music': 'music', 'qqmusic': 'music', '音乐': 'music', 'qq音乐': 'music',
            'radio': 'radio', 'fm': 'radio', '电台': 'radio', '广播': 'radio',
            'server': 'server', 'servers': 'server', 'monitor': 'server', '服务器': 'server', '服务器监控': 'server', '监控': 'server',
            'tutor': 'tutor', 'blackboard': 'tutor', '讲题': 'tutor', 'ai讲题': 'tutor', '黑板': 'tutor',
            'weather': 'weather', '天气': 'weather', '天气详情': 'weather',
            'alarms': 'alarms', 'alarm': 'alarms', '闹钟': 'alarms',
            'messages': 'messages', 'message': 'messages', 'inbox': 'messages', '留言': 'messages', '留言板': 'messages',
            'countdown': 'countdown', 'countdowns': 'countdown', '倒数日': 'countdown',
            'timer': 'timer', 'focus': 'timer', '计时': 'timer', '计时器': 'timer', '专注': 'timer', '番茄钟': 'timer',
            'sleep': 'sleep', '睡眠': 'sleep', '助眠': 'sleep',
            'settings': 'settings', 'setting': 'settings', 'config': 'settings', '设置': 'settings', '系统设置': 'settings',
        }
        sec_shortcuts = {
            'chat': 'chat', 'history': 'chat', '对话': 'chat', '对话记录': 'chat', '聊天记录': 'chat',
            'wifi': 'net', 'network': 'net', 'net': 'net', '网络': 'net',
        }
        if raw in sec_shortcuts:
            target = 'settings'
            sec = sec or sec_shortcuts[raw]
        else:
            target = page_aliases.get(raw)
            if not target:
                for kw, mapped in (
                    ('设置', 'settings'),
                    ('主页', 'home'), ('主界面', 'home'), ('主屏', 'home'), ('桌面', 'home'), ('首页', 'home'),
                    ('音乐', 'music'), ('电台', 'radio'), ('服务器', 'server'), ('讲题', 'tutor'),
                    ('天气', 'weather'), ('闹钟', 'alarms'), ('留言', 'messages'), ('倒数日', 'countdown'),
                    ('计时', 'timer'), ('专注', 'timer'), ('睡眠', 'sleep'),
                ):
                    if kw in raw:
                        target = mapped
                        break
            if not target:
                raise ValueError(f'不支持的页面名称: {page}')
        valid_secs = {'display', 'sound', 'net', 'voice', 'chat', 'weather', 'music', 'study', 'sys'}
        sec_aliases = {
            '显示': 'display', '亮度': 'display',
            '声音': 'sound', '音量': 'sound', '蓝牙': 'sound', 'airplay': 'sound', '投屏': 'sound',
            '网络': 'net', 'wifi': 'net', '小智': 'voice', '小麦': 'voice', '语音': 'voice',
            '对话': 'chat', '聊天': 'chat',
            'home': 'weather', '天气音乐': 'weather', '天气': 'weather',
            '音乐': 'music', 'qq音乐': 'music',
            '学习': 'study', '系统': 'sys',
        }
        sec = sec_aliases.get(sec, sec)
        if target == 'settings' and (sec in ('sleep', '睡眠', '助眠') or (not sec and ('睡眠' in raw or '助眠' in raw))):
            target = 'sleep'
            sec = ''
        if target == 'settings' and not sec:
            for kw, s_id in (
                ('显示', 'display'), ('亮度', 'display'),
                ('声音', 'sound'), ('音量', 'sound'), ('蓝牙', 'sound'), ('投屏', 'sound'), ('airplay', 'sound'),
                ('网络', 'net'), ('wifi', 'net'), ('小智', 'voice'), ('小麦', 'voice'), ('语音', 'voice'),
                ('对话', 'chat'), ('聊天', 'chat'), ('学习', 'study'),
                ('音乐', 'music'), ('天气', 'weather'), ('系统', 'sys'),
            ):
                if kw in raw:
                    sec = s_id
                    break
        clean_sec = sec if (target == 'settings' and sec in valid_secs) else ''
        clean_city = str(city or '').strip()[:24] if target == 'weather' else ''
        labels = {
            'home': '设备主页（主界面）', 'music': 'QQ音乐界面', 'radio': '广播电台界面',
            'server': '服务器监控界面', 'tutor': 'AI讲题界面', 'weather': '天气详情页',
            'alarms': '闹钟页面', 'messages': '家庭留言板', 'countdown': '倒数日页面',
            'timer': '计时专注页面', 'sleep': '助眠睡眠页面', 'settings': '系统设置页面',
        }
        try:
            self.screen.dim(False)
            if hasattr(self.screen, 'poke'):
                self.screen.poke()
        except Exception:
            pass
        payload = {'page': target, 'label': labels[target]}
        if clean_sec:
            payload['section'] = clean_sec
        if clean_city:
            payload['city'] = clean_city
        log.info('screen navigate: %s', payload)
        self.events.publish('navigate', payload)
        return {
            'success': True,
            **payload,
            'message': '已回到设备主页（显示时钟、天气、日历与音乐主界面）' if target == 'home' else f"已打开{labels[target]}",
        }

    # ---- auto screen-off (the blanker blanks: device.Screen, idle-blanker.py) ----

    def _screen_off_seconds(self):
        return self.screen_off['minutes'] * 60 if self.screen_off['enabled'] else 0

    async def set_screen_off(self, **changes):
        """Page: enabled and/or minutes (one of device.SCREEN_OFF_MINUTES); raises ValueError."""
        self.screen_off = normalize_screen_off(self.screen_off, changes)
        config.save(screen_off=self.screen_off)
        self.screen.blank_after(self._screen_off_seconds())
        log.info('screen-off: %s', self.screen_off)
        self.set_state(self.state)
        return self.screen_off

    # ---- volume: the device's (keys, voice, the page) and the alarms' own ----

    async def device_volume(self):
        """The device volume, also while a ring has the speaker at the alarm volume."""
        if self.held_volume is not None:
            return self.held_volume
        volume = await mcp.get_volume()
        if volume is not None:
            self.volume = volume
        return volume

    async def set_volume(self, volume, source='page'):
        """The device volume (page, voice, keys). While a ring or a preview has the speaker, it is what
        comes back afterwards."""
        async with self.volume_lock:
            volume = await self._apply_volume(volume)
        self.events.publish('volume', {'volume': volume, 'source': source})
        return volume

    async def _apply_volume(self, volume):
        """set_volume under volume_lock."""
        if isinstance(volume, bool):
            raise ValueError('volume must be a number')
        f_vol = float(volume)
        if not math.isfinite(f_vol):
            raise ValueError('volume must be finite')
        volume = max(0, min(100, int(f_vol)))
        if self.held_volume is not None:
            self.held_volume = volume
            self._write_ring_state()
        else:
            await mcp.set_volume(volume)
        self.volume = volume
        return volume

    async def volume_step(self, delta):
        """A volume key. Key repeat fires every ~100 ms: one change at a time, each from the current
        volume. While an alarm rings only that ring is louder or quieter: the alarm volume setting and
        the device volume (put back when it stops) stay."""
        if self.ring:
            async with self.volume_lock:
                if not self.ring:
                    return
                self.ring['volume'] = max(0, min(100, self.ring['volume'] + delta))
                if self.held_volume is not None:
                    await mcp.set_volume(self.ring['volume'])
                # else the ring hasn't switched the speaker yet: it will, to this volume (_hold_speaker)
            self.events.publish('volume', {'volume': self.ring['volume'], 'source': 'key', 'ring': True})
            return
        async with self.volume_lock:   # read and write as one step, or two quick presses make one
            current = self.held_volume if self.held_volume is not None else await mcp.get_volume()
            if current is None:
                return
            volume = await self._apply_volume(current + delta)
        self.events.publish('volume', {'volume': volume, 'source': 'key'})

    def _ring_state_path(self):
        return self.alarms.dir / 'ring.json'

    def _write_ring_state(self):
        try:
            alarms.write_atomic(self._ring_state_path(), json.dumps({'volume': self.held_volume}).encode())
        except OSError as e:
            log.warning('cannot save the volume to put back: %s', e)

    async def _hold_speaker(self, level):
        """Switch the speaker to `level` (or level() read under the lock: a ring's volume the keys may
        have changed meanwhile) for a ring or a preview; the device volume is saved first, so that a
        crash or a power cut in between still gets it back (_restore_volume)."""
        bridge.notify_speaker_hold_enter(self)
        try:
            async with self.volume_lock:
                level = level() if callable(level) else level
                if self.held_volume is None:
                    before = await mcp.get_volume()
                    self.held_volume = before if before is not None else self.volume
                    if self.held_volume is not None:
                        self._write_ring_state()
                await mcp.set_volume(level)
            bridge.notify_speaker_hold_settled(self)
        except BaseException:
            bridge.notify_speaker_hold_abort(self)
            raise
        finally:
            bridge.notify_speaker_hold_exit()

    async def _release_speaker(self):
        try:
            async with self.volume_lock:
                if self.held_volume is None:
                    return
                volume, self.held_volume = self.held_volume, None
                await mcp.set_volume(volume)
                self.volume = volume
                try:
                    self._ring_state_path().unlink()
                except FileNotFoundError:
                    pass
        finally:
            bridge.notify_speaker_release(self)

    async def _restore_volume(self):
        """At start: a ring that never ended (the client crashed, the power was cut) left the speaker at
        the alarm volume. Put the device volume back."""
        path = self._ring_state_path()
        try:
            before = json.loads(path.read_text())['volume']
        except FileNotFoundError:
            before = None
        except (ValueError, KeyError, TypeError):
            log.warning('%s is unreadable', path)
            before = None
        if isinstance(before, (int, float)) and not isinstance(before, bool):
            log.info('the last ring did not end: volume back to %d', before)
            await mcp.set_volume(before)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        await self.device_volume()

    async def set_alarm_volume(self, volume=None, change=None):
        """Page or voice: volume (0-100) or change (from the alarm volume now)."""
        if volume is None:
            if change is None:
                raise ValueError('volume or change is needed')
            if isinstance(change, bool):
                raise ValueError('change must be a number')
            f_ch = float(change)
            if not math.isfinite(f_ch):
                raise ValueError('change must be finite')
            volume = self.alarm_volume + int(f_ch)
        else:
            if isinstance(volume, bool):
                raise ValueError('volume must be a number')
            f_v = float(volume)
            if not math.isfinite(f_v):
                raise ValueError('volume must be finite')
            volume = int(f_v)
        self.alarm_volume = max(0, min(100, int(volume)))
        config.save(alarm_volume=self.alarm_volume)
        log.info('alarm volume: %d', self.alarm_volume)
        self.publish_alarms()
        return self.alarm_volume

    def set_alarm_tone(self, name):
        if name not in alarms.TONES:
            raise ValueError(f'tone must be one of {list(alarms.TONES)}')
        self.alarm_tone = name
        config.save(alarm_tone=name)
        self.publish_alarms()
        return name

    async def preview_tone(self, name):
        """The settings' 试听: one phrase of the tone at the alarm volume."""
        if name not in alarms.TONES:
            raise ValueError(f'tone must be one of {list(alarms.TONES)}')
        if self.ring:
            return
        # cancel the old one now (a task cancelled before it first runs never would) and let the new
        # one wait for it to put the volume back
        async with self.preview_lock:
            if self.ring:
                return
            previous = self.preview
            if previous:
                previous.cancel()
            self.preview = asyncio.create_task(self._play_preview(name, previous))

    async def _play_preview(self, name, previous=None):
        if previous and not previous.done():
            await asyncio.wait([previous])   # not `await previous`: a cancel of this one must still stop it
        rels = getattr(self, '_preview_releases', None)
        if rels:
            pending_rels = [t for t in list(rels) if not t.done()]
            if pending_rels:
                await asyncio.wait(pending_rels)
        sound = alarms.tone(name)
        n = min(len(sound) - int(OUT_RATE * alarms.TONE_GAP), int(OUT_RATE * PREVIEW_SECONDS))
        sound = sound[:n].copy()                       # a piece: only its first seconds, faded out
        fade = min(n, int(OUT_RATE * 0.6))
        sound[n - fade:] *= np.linspace(1, 0, fade, dtype=np.float32)[:, None]
        try:
            await self._hold_speaker(self.alarm_volume)
            self.audio.player.push(sound)
            await asyncio.sleep(n / OUT_RATE + 0.1)
        finally:
            self.audio.player.trim(0)
            # cancelled (another tap, the settings closed, a ring) halfway through putting the volume
            # back would leave the speaker at the alarm volume: let it finish and track it across chains
            rels = getattr(self, '_preview_releases', None)
            if rels is None:
                rels = self._preview_releases = set()
            rel_task = asyncio.create_task(self._release_speaker())
            rels.add(rel_task)
            rel_task.add_done_callback(rels.discard)
            await asyncio.shield(rel_task)

    async def _stop_preview(self):
        async with self.preview_lock:
            task, self.preview = self.preview, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        rels = getattr(self, '_preview_releases', None)
        if rels:
            pending_rels = [t for t in list(rels) if not t.done()]
            if pending_rels:
                await asyncio.wait(pending_rels)
        async with self.volume_lock:
            pass   # all shielded _release_speaker tasks have put the volume back

    # ---- countdowns, alarms and reminders (alarms.py) ----

    # ---- Bluetooth speaker (bt.py) ----

    def background_target(self):
        """How loud a phone's music plays: muted while an alarm rings or a tone is tried, lowered while
        talking (from the wake to the end of the reply) or a local sound plays, else full."""
        # a preview that played to its end stays in self.preview (finished) until the next one:
        # only one still playing mutes the phone
        if self.ring or (self.preview and not self.preview.done()):
            return 0.0
        if self.state != 'idle' or self.audio.player.buffered() > 0:
            return BACKGROUND_DUCKED
        return 1.0

    async def bluetooth(self, enabled=None, window=None, remove=None, disconnect=None):
        """The page's Bluetooth controls: the switch (stored), the "add a phone" window, forgetting a phone."""
        ok = True
        if enabled is not None:
            ok = await self.bt.set_enabled(bool(enabled))
            config.save(bluetooth=bool(enabled))
        if window is not None:
            was_enabled = self.bt.enabled
            ok = await self.bt.open_window(bool(window))
            if bool(window) and not was_enabled and self.bt.enabled:
                if isinstance(getattr(self, 'cfg', None), dict):
                    self.cfg['bluetooth'] = True
                config.save(bluetooth=True)
        if remove:
            ok = await self.bt.remove(remove)
        if disconnect:
            ok = await self.bt.disconnect(disconnect)
        return ok

    async def refresh_airplay(self):
        from . import net_ctl
        st = await asyncio.get_running_loop().run_in_executor(None, net_ctl.airplay_status_sync)
        self.airplay_state = st
        self.events.publish('airplay', st)
        return st

    async def set_airplay(self, enabled: bool):
        from . import net_ctl
        lock = getattr(self, 'airplay_lock', None)
        if lock is None:
            lock = self.airplay_lock = asyncio.Lock()
        async with lock:
            st = await asyncio.get_running_loop().run_in_executor(None, net_ctl.set_airplay_sync, bool(enabled))
            self.airplay_state = st
            if st.get('ok'):
                config.save(airplay=bool(st.get('enabled')))
            self.events.publish('airplay', st)
            return st

    def alarm_view(self):
        now = time.time()
        ring = self.ring
        return {'synced': self.clock_ok, 'items': [self.alarms.view(i, now) for i in self.alarms.active()],
                'missed': self.alarms.missed[::-1],
                'ringing': ring and {'items': [self.alarms.view(i, now) for i in ring['items']],
                                     'notes': ring['notes'], 'started': ring['started']},
                'volume': self.alarm_volume, 'tone': self.alarm_tone, 'tones': alarms.TONES}

    def publish_alarms(self):
        self.events.publish('alarms', self.alarm_view())

    async def create_alarm(self, kind, label='', say='', repeat=None, **when):
        """From the model (mcp.py). Raises ValueError with a sentence for it."""
        if not alarms.clock_synced():
            raise ValueError('设备时间还没和网络对上（刚开机或没联网），现在设不准，请过一会儿再试')
        item = self.alarms.create(kind, label, say, repeat, **when)
        if kind == 'reminder':
            asyncio.create_task(self._make_clip(item))
        self.publish_alarms()
        return self.alarms.view(item)

    async def delete_alarm(self, item_id):
        """Page or voice. A ringing entry stops ringing first."""
        if self.ring and any(i['id'] == item_id for i in self.ring['items']):
            if len(self.ring['items']) == 1:
                await self.stop_alarm()
            else:
                self.ring['items'] = [i for i in self.ring['items'] if i['id'] != item_id]
        item = self.alarms.remove(item_id)
        self.publish_alarms()
        return item and self.alarms.view(item)

    async def snooze_alarm(self, minutes=alarms.SNOOZE_MINUTES):
        """再睡: the entry ringing now, or rung in the last 15 minutes, rings again in `minutes`
        (hung on that entry: its repeat and words stay). [] when there is none."""
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 60:
            raise ValueError('延后时间要在 1 到 60 分钟之间')
        ringing = list(self.ring['items']) if self.ring else []
        await self.stop_alarm()
        found = self.alarms.snooze(minutes, ringing=ringing)
        self.publish_alarms()
        return [self.alarms.view(i) for i in found]

    def clear_missed(self):
        self.alarms.clear_missed()
        self.publish_alarms()

    async def _make_clip(self, item):
        """A reminder's words, synthesised by the hub now and kept with it: when it is due it is
        played without the hub. If that fails it only rings, and the screen shows the words."""
        text = item['say'] or (f"提醒你，{item['label']}" if item['label'] else '提醒时间到了')
        wav = await self._speech(text)
        if self.alarms.set_clip(item['id'], wav):
            self.publish_alarms()

    def _get_http_sess(self):
        orig = getattr(self, '_orig_aiohttp_cls', None)
        if orig is None:
            orig = aiohttp.ClientSession
            self._orig_aiohttp_cls = orig
        if aiohttp.ClientSession is not orig:
            return None
        loop = asyncio.get_running_loop()
        sess = getattr(self, '_http_sess', None)
        if sess is None or sess.closed or getattr(self, '_http_loop', None) is not loop:
            sess = aiohttp.ClientSession(trust_env=False)
            self._http_sess = sess
            self._http_loop = loop
        return sess

    async def _speech(self, text):
        max_bytes = 8 * 1024 * 1024
        try:
            shared = self._get_http_sess()
            t_out = aiohttp.ClientTimeout(total=TTS_TIMEOUT)

            async def _do_tts(s):
                async with s.post(self.cfg['hub_http'] + '/kiosk/tts', json={'text': text}, timeout=t_out) as r:
                    if r.status == 200:
                        if r.content_length and r.content_length > max_bytes:
                            log.warning('tts clip too large: %s', r.content_length)
                            return None
                        chunks, total = [], 0
                        while True:
                            chunk = await r.content.read(65536)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > max_bytes:
                                log.warning('tts stream too large (>%d)', max_bytes)
                                return None
                            chunks.append(chunk)
                        return b''.join(chunks)
                    log.warning('the hub did not synthesise %r: %s %s', text, r.status, await r.text())
                    return None

            if shared is not None:
                return await _do_tts(shared)
            async with aiohttp.ClientSession(timeout=t_out, trust_env=False) as s:
                return await _do_tts(s)
        except Exception as e:
            log.warning('the hub did not synthesise %r: %s', text, e)
        return None

    async def _alarm_loop(self):
        try:
            await self._restore_volume()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('restore_volume failed')
        last_tidy = 0.0
        while True:
            try:
                now = time.time()
                if self.clock_ok and now - last_tidy > 60:   # by a clock that is right
                    last_tidy = now
                    before = (len(self.alarms.items), len(self.alarms.missed))
                    self.alarms.tidy(now)
                    if self.alarms.refresh(now) or (len(self.alarms.items), len(self.alarms.missed)) != before:
                        self.publish_alarms()
                await self._alarm_tick(now)
            except Exception:
                log.exception('alarm scheduler')
            await asyncio.sleep(1)

    async def _alarm_tick(self, now):
        synced = alarms.clock_synced()
        if synced != self.clock_ok:
            self.clock_ok = synced
            log.info('system clock %s', 'synchronised: alarms on' if synced else 'not synchronised: alarms paused')
            self.publish_alarms()
        if not synced or self._intercom_active():
            return
        if self.ring and (self.ring['closing'] or self.ring['stop'].is_set()):
            return   # the ring is ending: what is due now rings on its own once it has
        before = len(self.alarms.missed)
        items, notes = self.alarms.take_due(now, self.ring['items'] if self.ring else ())
        if items and self.ring:
            # due while another rings: they ring together, and the ring lasts its full time from now
            self.ring['items'] += items
            self.ring['notes'] += notes
            self.ring['fired'].update((i['id'], now) for i in items)
            self.ring['joined'].append((time.monotonic(), all(i['kind'] == 'reminder' for i in items)))
            log.info('joined the ring: %s', [(i['id'], i['kind'], i['label']) for i in items])
            self.publish_alarms()
        elif items:
            self.ring = {'items': items, 'notes': notes, 'started': now, 'volume': self.alarm_volume,
                         'stop': asyncio.Event(), 'joined': [], 'fired': {}, 'closing': False}
            self.ring['task'] = asyncio.create_task(self._ringing(self.ring))
        elif len(self.alarms.missed) != before:
            self.publish_alarms()

    async def _ringing(self, ring):
        answered = False
        note = None
        log.info('ringing: %s%s', [(i['id'], i['kind'], i['label']) for i in ring['items']],
                 f" ({'；'.join(ring['notes'])})" if ring['notes'] else '')
        try:
            self.ptt_held = False
            await self._stop_preview()
            if self.in_turn():
                await self.abort()
            else:
                self._reply_gen += 1
            self.uploading = self.listen_after_reply = False
            self.deadline = None
            self.set_state('idle')
            self.screen.alarm(True)
            self.publish_alarms()
            await self._hold_speaker(lambda: ring['volume'])
            note = asyncio.create_task(self._speech('，'.join(ring['notes']))) if ring['notes'] else None
            clips, phrase, chime = {}, None, None
            start = time.monotonic()
            end, reminder_end, round_over = start + alarms.RING_SECONDS, start + alarms.REMINDER_SECONDS, 0.0
            while True:
                if ring['stop'].is_set():
                    answered = True
                    break
                now = time.monotonic()
                for at, only_reminders in ring['joined']:   # each one that joined rings its full time too
                    end = max(end, at + (alarms.REMINDER_SECONDS if only_reminders else alarms.RING_SECONDS))
                    if only_reminders:
                        reminder_end = max(reminder_end, at + alarms.REMINDER_SECONDS)
                # only reminders: the chime and their words a few times; an alarm or countdown among
                # them: the tone over and over, up to RING_SECONDS
                reminders = all(i['kind'] == 'reminder' for i in ring['items'])
                if reminders and now >= reminder_end and now >= round_over:   # its words finished, not cut off
                    break
                if not reminders and now >= end:
                    break
                if self.audio.player.buffered() < 0.3 and not (reminders and now >= reminder_end):
                    words = []
                    if note and note.done():
                        words += self._clip_sound(note.result())   # once, in the first round it is ready for
                        note = None
                    for item in ring['items']:
                        if item['kind'] == 'reminder' and item.get('clip'):
                            if item['id'] not in clips:
                                clips[item['id']] = self._clip_sound(self.alarms.clip_path(item))
                            words += clips[item['id']]
                    if reminders:
                        chime = alarms.reminder_chime() if chime is None else chime
                        sound = overlay(chime, np.concatenate(words), alarms.CHIME_SPEECH_AT) if words else chime
                        round_over = now + self.audio.player.buffered() + len(sound) / OUT_RATE
                        sound = np.concatenate([sound, np.zeros((int(OUT_RATE * alarms.REMINDER_PAUSE), 2), np.float32)])
                    else:
                        phrase = alarms.tone(self.alarm_tone) if phrase is None else phrase
                        sound = np.concatenate([phrase] + words)
                    self.audio.player.push(sound)
                try:
                    await asyncio.wait_for(ring['stop'].wait(), 0.1)
                    answered = True
                    break
                except asyncio.TimeoutError:
                    pass
            if not answered:
                log.info('nobody stopped the ring in %.0f s: noted as missed', time.monotonic() - start)
        except Exception:
            # whoever awaits this (stop_alarm from the wake word) must not fail with it
            log.exception('the ring failed')
        finally:
            ring['closing'] = True   # nothing joins any more: it would be finished without having rung
            if note is not None and not note.done():
                note.cancel()
            try:
                self.audio.player.trim(0)
            except Exception:
                log.exception('alarm player trim failed')
            try:
                self.screen.alarm(False)
            except Exception:
                log.exception('alarm screen reset failed')
            try:
                await self._release_speaker()
            except Exception:
                log.exception('alarm release_speaker failed')
            try:
                self.alarms.finish(ring['items'], ring['started'], answered, fired=ring['fired'])
            except Exception:
                log.exception('alarm finish failed')
            self.ring = None
            self.publish_alarms()
            self.set_state(self.state)

    @staticmethod
    def _clip_sound(source):
        """A spoken clip (a wav file, or the hub's wav bytes) and a pause after it, as a list to concatenate."""
        if not source:
            return []
        try:
            x = load_wav(io.BytesIO(source) if isinstance(source, bytes) else source)
        except Exception as e:
            log.warning('cannot play a reminder clip: %s', e)
            return []
        return [x, np.zeros((int(OUT_RATE * CLIP_GAP), 2), np.float32)]

    async def stop_alarm(self):
        """停止 on the page, or the wake word: the ring ends (a one-off is done, a repeating one waits
        for its next time)."""
        ring = self.ring
        if not ring:
            return False
        ring['stop'].set()
        await asyncio.shield(ring['task'])
        return True

    # ---- models (the hub's local-only /kiosk/llm endpoints) ----

    async def _hub_http(self, path, body=None):
        shared = self._get_http_sess()
        t_out = aiohttp.ClientTimeout(total=15)

        async def _do_req(s):
            if body is None:
                async with s.get(self.cfg['hub_http'] + path, timeout=t_out) as r:
                    if r.status != 200:
                        raise RuntimeError(f'hub HTTP {r.status} on GET {path}')
                    return await r.json(content_type=None)
            async with s.post(self.cfg['hub_http'] + path, json=body, timeout=t_out) as r:
                if r.status != 200:
                    raise RuntimeError(f'hub HTTP {r.status} on POST {path}')
                return await r.json(content_type=None)

        if shared is not None:
            return await _do_req(shared)
        async with aiohttp.ClientSession(timeout=t_out, trust_env=False) as s:
            return await _do_req(s)

    async def refresh_models(self):
        """Ask the hub for its models; False when it didn't answer (down, or still starting)."""
        try:
            data = await self._hub_http('/kiosk/llm?' + urllib.parse.urlencode({'device': self.cfg['device_id']}))
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        self.hub_seen = time.monotonic()
        try:
            self._apply_models(data)
        except Exception:
            log.exception('apply_models failed')
            return False
        return True

    def _apply_models(self, data):
        options = [describe(o) for o in data.get('options', [])]
        models = {'current': next((o for o in options if o['entry'] == data.get('selected')), None),
                  'options': options}
        if models != self.models:
            self.models = models
            self.events.publish('model', models)
            if self.hub.connected and self.entry and models['current'] and models['current']['entry'] != self.entry:
                self.entry = models['current']['entry']
                self.events.publish('thread', {'entry': self.entry, 'messages': self.threads.get(self.entry, [])})
            elif not self.entry:
                # also while connected with nothing said yet (e.g. just after an interrupt reconnected)
                self.events.publish('thread', {'entry': self.view_entry(), 'messages': self.threads.get(self.view_entry(), [])})

    async def set_model(self, kind, vendor, entry=None):
        # entry names the model on the hub
        if self.in_turn():
            # the answer in flight belongs to the old model: stop it rather than file it under the new one
            await self.abort()
        try:
            result = await self._hub_http('/kiosk/llm/use', {'kind': kind, 'vendor': vendor, 'entry': entry,
                                                             'device': self.cfg['device_id']})
        except Exception as e:
            result = {'ok': False, 'error': str(e)}
        if result.get('ok') and 'options' in result:
            self._apply_models(result)   # the hub answers with the new list (an older hub doesn't)
        else:
            await self.refresh_models()
        return {k: v for k, v in result.items() if k not in ('options', 'selected', 'default', 'kind', 'vendor')}

    async def _model_poller(self):
        # often while the hub is down (after a boot the page would show no models for over a minute),
        # then only to notice changes made in the hub's admin page
        while True:
            up = False
            try:
                up = await self.refresh_models()
                if up and not self.history_loaded:
                    await self.load_history()   # at start, or once the hub is up
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('model_poller error')
            await asyncio.sleep(MODEL_POLL if up else MODEL_RETRY)

    # ---- timers ----

    async def _ticker(self):
        last_level = 0.0
        paused_for = None
        while True:
            await asyncio.sleep(0.1)
            try:
                self.audio.background.target = self.background_target()
                if self.ring and self.ring is not paused_for:
                    paused_for = self.ring
                    if self.bt.streaming():
                        log.info('alarm: asking the phone to pause')
                        await self.bt.pause()
                now = time.monotonic()
                stalled = self.audio.stalled()
                if stalled:
                    # PipeWire restarted (or died): the streams won't come back by themselves. systemd
                    # restarts the service, which waits for PipeWire's socket and opens them again.
                    log.error('audio is dead (%s): exiting to be restarted', stalled)
                    os._exit(1)
                if self.uploading and self.audio.mic_level > SPEECH_RMS:
                    self.speech_end = now
                    if self.recognising:   # talking again: the hub listens on
                        self.recognising = False
                        self.set_state(self.state)
                    if self.deadline:   # still talking
                        self.deadline = min(max(self.deadline, now + 2.5), self.listen_since + MAX_LISTEN)
                if self.uploading and self.listen_mode == 'manual' and now > self.listen_since + MAX_LISTEN:
                    # push-to-talk held (or a release lost) for too long: the hub buffers all of it
                    log.info('push-to-talk open for %d s: stopping it', MAX_LISTEN)
                    await self.ptt_stop()
                if self.deadline and now > self.deadline:
                    if self.state == 'listening' and self.uploading:
                        # a hub that sends VAD events has answered everything it heard unless one is pending.
                        # In a follow-up window (heard) only that counts: an older hub would make any noise
                        # after a reply wait 15 s
                        waiting = self.hub_pending or (not self.heard and not self.hub.features.get('vad_end'))
                        if self.speech_end > self.listen_since and waiting:
                            # they did talk; a long sentence takes the hub a while to recognise, so
                            # close the mic but wait for its text before saying "didn't catch that"
                            # (or, in a follow-up window, before going idle: its answer would be lost)
                            self.uploading = False
                            self.awaiting_text = True
                            self.deadline = now + RECOGNITION_WAIT
                            self.set_state('thinking')
                        else:
                            await self.stop_uploading(missed=not self.heard)
                    elif self.state == 'thinking' and not self.uploading:
                        missed, self.awaiting_text = self.awaiting_text and not self.heard, False
                        await self.stop_uploading(missed=missed)
                if self.state in ('listening', 'speaking') and now - last_level >= 0.1:
                    last_level = now
                    self.events.publish('level', {'mic': round(self.audio.mic_level, 4),
                                                  'out': round(self.audio.player.level, 4)})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('ticker error')


def dumps(data):
    return json.dumps(data, ensure_ascii=False)
