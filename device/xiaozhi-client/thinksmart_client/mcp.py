"""Device tools the hub's models may call (MCP over the xiaozhi channel): speaker volume, sleep hours,
screen brightness, countdowns / alarms / reminders and their volume.

For alarms the model passes what the user meant (a duration, or a clock time with a date, a repeat
rule, what it is about); the device works out the time point and answers with it for the model to
repeat (alarms.py)."""
import asyncio
import json
import logging
import math
import re

from . import bridge

log = logging.getLogger(__name__)
_NATIVE_ECOSYSTEM_TOOLS = True

TOOLS = [
    {
        'name': 'self.audio_speaker.set_volume',
        'description': '设置设备扬声器的音量。用户说「声音大一点」「音量调到 50」这类话时调用；'
                       '相对调整时先用 get_volume 读当前音量。',
        'inputSchema': {'type': 'object',
                        'properties': {'volume': {'type': 'integer', 'minimum': 0, 'maximum': 100}},
                        'required': ['volume']},
    },
    {
        'name': 'self.audio_speaker.get_volume',
        'description': '读取设备扬声器当前的音量（0-100）。',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'self.screen.set_brightness',
        'description': '调节设备屏幕亮度，两个参数只传一个。亮度 0-100：0 是最暗（仍看得清），100 最亮。'
                       '「屏幕亮一点」change=20，「暗一点」change=-20（「再亮一点」同样传 change，不用先读）；'
                       '「调到最亮」brightness=100，「调到最暗」brightness=0，「亮度调到 60」brightness=60。'
                       '口头调过之后自动切为手动亮度并保持。',
        'inputSchema': {'type': 'object',
                        'properties': {'brightness': {'type': 'integer', 'minimum': 0, 'maximum': 100},
                                       'change': {'type': 'integer', 'minimum': -100, 'maximum': 100,
                                                  'description': '在当前亮度上加减'}}},
    },
    {
        'name': 'self.screen.get_brightness',
        'description': '读取屏幕当前亮度（level: 0-100）。',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'self.screen.navigate',
        'description': (
            '切换设备屏幕显示的页面，或从任意子页面/设置/弹窗回到设备主页（主界面）。'
            '设备的「主页 / 主界面 / 我们主页 / 桌面 / 首页」是默认主屏幕（显示大时钟、实时天气、日历农历、音乐卡片与快捷磁贴）。'
            '【铁律：凡是用户说「回主页」「回主界面」「回我们主页」「回到主页」「回桌面」「打开主页」「退出当前页面」「关闭当前页面」「返回主屏」，'
            '或从音乐/电台/服务器/AI讲题/设置/天气详情/倒数日/闹钟/留言板/计时/睡眠等任意界面要求返回或退出时，必须调用本工具并传 page="home"，'
            '严禁不调用工具只在口头说已经回到主页了！】'
            '同时支持语音直达各页面：主页(home)、QQ音乐(music)、广播电台(radio)、服务器监控(server)、AI讲题(tutor)、'
            '天气详情(weather)、闹钟页(alarms)、家庭留言板(messages)、倒数日(countdown)、计时专注(timer)、助眠睡眠(sleep)、'
            '系统设置(settings，可配 section 打开对应分类：display显示/sound声音与蓝牙投屏/net网络WiFi/voice小智/chat对话记录/weather天气城市/music音乐/study学习/sys系统)。'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'page': {
                    'type': 'string',
                    'enum': ['home', 'music', 'radio', 'server', 'tutor', 'weather', 'alarms', 'messages', 'countdown', 'timer', 'sleep', 'settings'],
                    'description': '目标页面：回主页/主界面/我们主页/桌面填 home',
                },
                'section': {
                    'type': 'string',
                    'enum': ['display', 'sound', 'net', 'voice', 'chat', 'weather', 'music', 'study', 'sys'],
                    'description': '可选：当 page="settings" 时直达的设置分类（如查看聊天对话填 chat，Wi-Fi网络填 net，天气城市填 weather，音乐填 music）',
                },
                'city': {
                    'type': 'string',
                    'description': '可选：当 page="weather" 时指定查看的城市名',
                },
            },
            'required': ['page'],
        },
    },
    {
        'name': 'self.alarm.create',
        'description': '新建设备的响铃闹钟（专管起床叫醒、固定钟点响铃，例如「明早 7 点叫我起床」「定个工作日 6 点半的闹钟」）。\n'
                       '【主系统优先规则】：\n'
                       '1. 用户说「倒计时 N 分钟」「计时 N 分钟」「开个秒表」时，请优先调用主页大屏工具 self.home.timer（在主页圆环实时显示）；\n'
                       '2. 用户说「半小时后提醒我关火」「明早 7 点提醒朵朵带水杯」「晚上 9 点提醒萌萌洗澡」等日常事项提醒时，请优先调用主页看板工具 self.home.reminder；\n'
                       '3. 只有需要大音量持续响铃叫醒的闹钟（kind="alarm"）才调用本工具。只传用户的意思，具体几点由设备算好返回，你照返回的 when（和 repeat）跟用户复述一遍。\n'
                       '某个钟点传 time（24 小时制 HH:MM，「早上 7 点」07:00、「晚上 7 点」19:00），'
                       '是哪天用 days_from_today（今天 0、明天 1、后天 2）或 date（YYYY-MM-DD），都不传就是最近的那个钟点；多久以后传 hours/minutes/seconds。\n'
                       '循环用 repeat：daily 每天；workdays 工作日（按国家放假安排：放假不响，调休上班的周末照响；只要周一到周五、不管放假，就用 weekly 配 weekdays [1,2,3,4,5]）；weekly 每周几配 weekdays（1-7）；monthly 每月几号配 month_day。label 是简短标签。',
        'inputSchema': {'type': 'object',
                        'properties': {'kind': {'type': 'string', 'enum': ['timer', 'alarm', 'reminder']},
                                       'hours': {'type': 'number'}, 'minutes': {'type': 'number'},
                                       'seconds': {'type': 'number'},
                                       'time': {'type': 'string', 'description': 'HH:MM，24 小时制'},
                                       'days_from_today': {'type': 'integer', 'minimum': 0},
                                       'date': {'type': 'string', 'description': 'YYYY-MM-DD'},
                                       'repeat': {'type': 'string', 'enum': ['daily', 'workdays', 'weekly', 'monthly']},
                                       'weekdays': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1, 'maximum': 7}},
                                       'month_day': {'type': 'integer', 'minimum': 1, 'maximum': 31},
                                       'label': {'type': 'string'}, 'say': {'type': 'string'}},
                        'required': ['kind']},
    },
    {
        'name': 'self.alarm.list',
        'description': '列出设备上还没响的闹钟（「我有哪些闹钟」「明天几点叫我」），每条有 id、type、when、in、repeat、label；取消某一条前先用它找到 id。（查主页日常提醒用 self.home.reminder）',
        'inputSchema': {'type': 'object',
                        'properties': {'kind': {'type': 'string', 'enum': ['timer', 'alarm', 'reminder'],
                                                'description': '只看这一种，不传就全部'}}},
    },
    {
        'name': 'self.alarm.cancel',
        'description': '取消设备上的响铃闹钟：传 id（先用 self.alarm.list 找到）；用户明确说「全部闹钟取消」时传 all=true。正在响的也能取消。（取消主页计时器用 self.home.timer，取消日常提醒用 self.home.reminder）',
        'inputSchema': {'type': 'object',
                        'properties': {'id': {'type': 'integer'}, 'all': {'type': 'boolean'},
                                       'kind': {'type': 'string', 'enum': ['timer', 'alarm', 'reminder']}}},
    },
    {
        'name': 'self.alarm.snooze',
        'description': '再睡一会儿：用户叫停刚响的闹钟后说「再睡 10 分钟」「过 5 分钟再叫我」时调用，'
                       '让正在响或 15 分钟内响过的那一条过 minutes 分钟（默认 10）再响一次，内容和循环都不变。'
                       '返回 success=false 表示没有这样的一条，这时问用户要不要设一个几分钟后的闹钟。',
        'inputSchema': {'type': 'object',
                        'properties': {'minutes': {'type': 'integer', 'minimum': 1, 'maximum': 60}}},
    },
    {
        'name': 'self.alarm.set_volume',
        'description': '设置闹钟音量（倒计时、闹钟、提醒响铃的音量，和平时的设备音量分开），两个参数只传一个：'
                       '「闹钟声音大一点」change=10，「闹钟音量调到 80」volume=80。平时说话的音量用 self.audio_speaker.set_volume。',
        'inputSchema': {'type': 'object',
                        'properties': {'volume': {'type': 'integer', 'minimum': 0, 'maximum': 100},
                                       'change': {'type': 'integer', 'minimum': -100, 'maximum': 100}}},
    },
    {
        'name': 'self.alarm.get_volume',
        'description': '读取闹钟音量（0-100）。',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
    *bridge.MUSIC_TOOLS,
]

ALARM_WHEN = ('hours', 'minutes', 'seconds', 'time', 'date', 'days_from_today')


def _alarm_args(args):
    """self.alarm.create's arguments as Client.create_alarm takes them."""
    rule = args.get('repeat')
    if isinstance(rule, dict):
        repeat = dict(rule)
        if repeat.get('days') is None and args.get('weekdays') is not None:
            repeat['days'] = args.get('weekdays')
        if repeat.get('day') is None and args.get('month_day') is not None:
            repeat['day'] = args.get('month_day')
    elif rule:
        repeat = {'type': rule, 'days': args.get('weekdays'), 'day': args.get('month_day')}
    else:
        repeat = None
    return {'kind': args.get('kind'), 'label': args.get('label') or '', 'say': args.get('say') or '',
            'repeat': repeat, **{k: args[k] for k in ALARM_WHEN if args.get(k) is not None}}


def _brief(view):
    return {k: view[k] for k in ('id', 'type', 'label', 'when', 'in', 'repeat') if view.get(k) not in (None, '')}


async def _wpctl(*args):
    try:
        p = await asyncio.create_subprocess_exec(
            'wpctl', *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except OSError:
        return ''
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout=5.0)
        return out.decode()
    except BaseException as exc:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        try:
            await p.wait()
        except Exception:
            pass
        if isinstance(exc, asyncio.TimeoutError):
            return ''
        raise


async def get_volume():
    m = re.search(r'([\d.]+)', await _wpctl('get-volume', '@DEFAULT_AUDIO_SINK@'))
    return round(float(m.group(1)) * 100) if m else None


async def set_volume(volume):
    await _wpctl('set-volume', '@DEFAULT_AUDIO_SINK@', f'{max(0, min(100, int(volume))) / 100:.2f}')


def _dumps(value):
    return json.dumps(value, ensure_ascii=False)


async def handle(payload, client=None):
    """One JSON-RPC request from the hub; returns the reply payload (None for notifications)."""
    method, req_id, params = payload.get('method'), payload.get('id'), payload.get('params') or {}
    if req_id is None:
        return None

    def result(value):
        return {'jsonrpc': '2.0', 'id': req_id, 'result': value}

    def error(message):
        return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': -32601, 'message': message}}

    if method == 'initialize':
        return result({'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                       'serverInfo': {'name': 'thinksmart-client', 'version': '0.1.0'}})
    if method == 'tools/list':
        return result({'tools': TOOLS})
    if method == 'tools/call':
        name = params.get('name')
        raw_args = params.get('arguments')
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except Exception:
                raw_args = {}
        args = raw_args if isinstance(raw_args, dict) else {}
        if name == 'self.music.control' and client and str(args.get('action') or '').strip().lower() in ('close_ui', 'close', 'home', 'back', 'exit', '回主页', '主界面'):
            try:
                return result({'content': [{'type': 'text', 'text': _dumps(client.navigate_screen('home'))}], 'isError': False})
            except Exception as e:
                log.exception('music close_ui navigate failed')
                return result({'content': [{'type': 'text', 'text': str(e)}], 'error': str(e), 'isError': True})
        if name in bridge._MY_TOOLS:
            try:
                txt = await bridge.dispatch_ecosystem_tool(name, args)
                return result({'content': [{'type': 'text', 'text': txt}], 'isError': False})
            except Exception as e:
                log.exception('ecosystem tool %s failed', name)
                return result({'content': [{'type': 'text', 'text': str(e)}], 'error': str(e), 'isError': True})
        try:
            if name == 'self.audio_speaker.set_volume' and client:
                text = json.dumps({'success': True, 'volume': await client.set_volume(args['volume'], 'voice')})
            elif name == 'self.audio_speaker.get_volume' and client:
                text = json.dumps({'volume': await client.device_volume()})
            elif name == 'self.screen.set_brightness' and client:
                text = json.dumps({'success': True, **await client.set_brightness(
                    level=args.get('brightness'), change=args.get('change'))})
            elif name == 'self.screen.get_brightness' and client:
                text = json.dumps(client.brightness())
            elif name == 'self.screen.navigate' and client:
                text = _dumps(client.navigate_screen(
                    page=args.get('page', 'home'),
                    section=args.get('section'),
                    city=args.get('city'),
                ))
            elif name == 'self.alarm.create' and client:
                # 线上真实语音会话若误把纯倒计时发给 self.alarm.create(kind='timer')，优先桥接到主页大屏圆环计时器
                if (
                    args.get('kind') == 'timer'
                    and not args.get('time')
                    and not args.get('date')
                    and args.get('days_from_today') is None
                    and not args.get('repeat')
                    and not any(isinstance(args.get(k), bool) for k in ('hours', 'minutes', 'seconds'))
                    and getattr(getattr(client, 'hub', None), 'connected', False)
                ):
                    try:
                        raw_s = (
                            float(args.get('hours') or 0) * 3600
                            + float(args.get('minutes') or 0) * 60
                            + float(args.get('seconds') or 0)
                        )
                        tot_s = int(round(raw_s)) if math.isfinite(raw_s) else -1
                    except (TypeError, ValueError, OverflowError):
                        tot_s = -1
                    if 5 <= tot_s <= 10800:
                        try:
                            htxt = await bridge.dispatch_ecosystem_tool('self.home.timer', {
                                'action': 'start',
                                'mode': 'countdown',
                                'seconds': tot_s,
                                'label': str(args.get('label') or '').strip()[:24],
                            })
                            if htxt and '失败' not in htxt and '不可用' not in htxt:
                                return result({'content': [{'type': 'text', 'text': _dumps({
                                    'success': True, 'routed': 'self.home.timer',
                                    'type': '主页倒计时', 'seconds': tot_s,
                                    'label': str(args.get('label') or '').strip(), 'message': htxt,
                                })}], 'isError': False})
                        except Exception:
                            pass
                text = _dumps({'success': True, **_brief(await client.create_alarm(**_alarm_args(args)))})
            elif name == 'self.alarm.list' and client:
                view = client.alarm_view()
                items = [_brief(i) for i in view['items'] if not args.get('kind') or i['kind'] == args['kind']]
                text = _dumps({'items': items, 'missed_recently': len(view['missed']),
                               **({} if view['synced'] else {'note': '设备时间还没同步，闹钟暂停中'})})
            elif name == 'self.alarm.cancel' and client:
                if args.get('all'):
                    ids = [i['id'] for i in client.alarm_view()['items'] if not args.get('kind') or i['kind'] == args['kind']]
                elif args.get('id') is not None:
                    raw_id = args['id']
                    if isinstance(raw_id, bool):
                        raise ValueError('id 要是整数')
                    ids = [int(raw_id)]
                else:
                    raise ValueError('要传 id，或者 all=true')
                gone = [_brief(v) for v in [await client.delete_alarm(i) for i in ids] if v]
                text = _dumps({'success': True, 'cancelled': gone} if gone or args.get('all') else
                              {'success': False, 'reason': f"没有 id 为 {args['id']} 的，先用 self.alarm.list 查"})
            elif name == 'self.alarm.snooze' and client:
                raw_min = args.get('minutes')
                if raw_min is None:
                    minutes = 10
                elif isinstance(raw_min, bool) or not isinstance(raw_min, int) or not 1 <= raw_min <= 60:
                    raise ValueError('延后时间要在 1 到 60 分钟之间')
                else:
                    minutes = raw_min
                found = await client.snooze_alarm(minutes)
                text = _dumps({'success': True, 'minutes': minutes, 'snoozed': [_brief(v) for v in found]} if found else
                              {'success': False, 'reason': '现在没有正在响、也没有 15 分钟内响过的闹钟',
                               'suggest': f'问用户要不要设一个 {minutes} 分钟后的闹钟（self.alarm.create）'})
            elif name == 'self.alarm.set_volume' and client:
                text = json.dumps({'success': True, 'volume': await client.set_alarm_volume(
                    volume=args.get('volume'), change=args.get('change'))})
            elif name == 'self.alarm.get_volume' and client:
                text = json.dumps({'volume': client.alarm_volume})
            else:
                return error(f'Unknown tool: {name}')
        except Exception as e:
            log.exception('tool %s failed', name)
            return result({'content': [{'type': 'text', 'text': str(e)}], 'error': str(e), 'isError': True})
        return result({'content': [{'type': 'text', 'text': text}], 'isError': False})
    return error(f'Method not implemented: {method}')
