"""ThinkSmart local changes for xiaozhi-hub-server, kept OUTSIDE its git checkout.

xiaozhi-hub-server is developed upstream and updated with its own "立即更新" button, which refuses
to update while tracked files are modified. Our local changes therefore live here, in
/opt/thinksmart-addons/xiaozhi-hub-overrides/files/, and the official files stay untouched.
Weather authentication also uses a header so the private key stays out of request URLs.
This file is loaded at interpreter start via the systemd drop-in
~/.config/systemd/user/xiaozhi-hub.service.d/thinksmart-overrides.conf (PYTHONPATH=<this dir>).

Safety switch: a module is replaced only while the official file is still exactly the version our
copy was made from (its git blob id). When upstream changes that file, the official new version
is used instead (upstream improvements are never masked) and the status file says which change
needs re-adapting. Any error here means "use the official files"; it can never stop the hub.

Status: cat /run/user/1000/xiaozhi-hub-overrides.json
To re-base after upstream changed a file: merge our change into a copy of the new official file,
put it under files/, and update BLOB below to `git rev-parse HEAD:<official path>`.
"""
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_PATH = '/run/user/1000/xiaozhi-hub-overrides.json'

# module -> (git blob of the official file our copy is based on, our copy, what it changes)
OVERRIDES = {
    'core.utils.opus_encoder_utils': (
        '8d603e2266d3a7e796e6a3b4000202ea8d1a50e3', 'files/core/utils/opus_encoder_utils.py',
        '下发语音 Opus 码率 24k -> 64k'),
    'core.utils.util': (
        'ef02754cdfde9ec244feeb8d8570f14e1dedde05', 'files/core/utils/util.py',
        'audio_to_data 编码器码率 64k'),
    'core.providers.tts.index_stream': (
        '49af09c3978ddf26ed309a96af62b5bcd84defb7', 'files/core/providers/tts/index_stream.py',
        'IndexStreamTTS：打断、sentence_id、首包缓冲 5 帧防断音、不走代理、音色名 xiaohe'),
    'plugins_func.functions.play_music': (
        'fd21f9f81a57d63929d3ccb43e52ed56bc951770', 'files/plugins_func/functions/play_music.py',
        'play_music 改为调用本机 QQ 音乐服务 127.0.0.1:8091'),
    'core.utils.reply_echo': (
        '49bd3bd8e22df2c43a54b1038fb7b005c7933f1f', 'files/core/utils/reply_echo.py',
        '回声过滤：喇叭停了之后才说的话不当回声（新闻后回答「详细内容」第一次没反应）'),
    'core.providers.asr.openai': (
        'c917cce56d9eab206820e575dfae62137d2f52dc', 'files/core/providers/asr/openai.py',
        'OpenAI 兼容识别：交给本地算力端（LocalASR），不阻塞、有超时、连不上重试一次、不走代理'),
    'plugins_func.functions.web_search': (
        'd80d673a85b23013f39e180561f1fcec8876a65e', 'files/plugins_func/functions/web_search.py',
        '联网搜索：DeepSeek 自带搜索 + 内网搜索服务的秘塔/高德/实时行情（metaso_key、amap_key 在私有配置）'),
    'plugins_func.functions.get_news_from_newsnow': (
        '631b3027d78068c53884e8be579f3d1664e6d590', 'files/plugins_func/functions/get_news_from_newsnow.py',
        '新闻一次列 3 条带编号，第几条讲详情、下一批不重复，详情截 2000 字，原文打不开改用联网搜索'),
    'core.api.base_handler': (
        'db27754391e6fc94cb6ecdf9ff1f4eeecba33a9c', 'files/core/api/base_handler.py',
        '移除通配符 CORS 头，补充 nosniff 与 no-store 安全响应头'),
    'core.api.ota_handler': (
        '1e3b3bd0dc1f3346165a91468045fcd6a84fc792', 'files/core/api/ota_handler.py',
        'OTA 接口：限制请求体 64 KiB、拦截跨域 Origin、收紧固件下载普通文件校验'),
    'core.api.vision_handler': (
        'c3e683cfeca4609e4680b4be721cf24ea824c094', 'files/core/api/vision_handler.py',
        '视觉解释接口：移除 web_test_client 免认证后门、流式限制上传体积、异步执行 VLLM 与错误脱敏'),
    'core.http_server': (
        '7dbf6787f0e32466b54e73b53f6a471274faae45', 'files/core/http_server.py',
        'HTTP 服务：Host 防 DNS 重绑定、/kiosk 与 /admin 跨站 Origin 拦截及 6 MiB 请求体上限'),
    'core.websocket_server': (
        'df5f9abf6f618a4341096774142dd88f418d2589', 'files/core/websocket_server.py',
        'WebSocket 服务：握手阶段拦截外部浏览器跨站劫持（CSWSH）与不可信 Host'),
    'core.admin.auth': (
        'd043084fa2d8ef8c1ab0180e3d408d5d074c2808', 'files/core/admin/auth.py',
        '后台认证：限制密码长度防 scrypt 算力放大、限制活跃会话数、会话交接文件防符号链接原子写入'),
    'core.admin.admin_handler': (
        'c9d6301a1be3270b2d515a6de17b9231d99f2f4f', 'files/core/admin/admin_handler.py',
        '后台接口：登录失败按源限流与单并发校验防占满线程池阻塞唤醒词复核、初始化设密互斥锁'),
    'core.connection': (
        '0f84c8a589d8fb868f45b773364abcc1050a0afc', 'files/core/connection.py',
        '设备连接：仅信任本机反向代理转发头、日志脱敏鉴权请求头、清洗并限制 device-id 长度'),
    'core.handle.textHandler.listenMessageHandler': (
        '5c10c3d83ee76743722c9941013901f41bb0796d', 'files/core/handle/textHandler/listenMessageHandler.py',
        'Listen 消息：容错缺失字段与空唤醒词列表、限制文本长度上限'),
    'core.handle.textHandler.serverMessageHandler': (
        'b9a235888854e2a71541a63846728df602cd7dd5', 'files/core/handle/textHandler/serverMessageHandler.py',
        'Server 消息：非空密钥校验、常量时间比对、缺省字段容错与错误脱敏'),
    'core.agent.call_names': (
        'b3b4ff4b5d9864a38fd062548708d9ced52c77d9', 'files/core/agent/call_names.py',
        '呼名回复音频缓存：0700 目录与 0600 唯一临时文件原子落盘及读取体积校验'),
    'plugins_func.functions.search_from_ragflow': (
        '40abf8c4a66e1b2521e11b40e27e094a639d6556', 'files/plugins_func/functions/search_from_ragflow.py',
        'RAGFlow 查询：默认开启 TLS 证书校验、校验接口地址、流式限制响应体积与分片长度'),
    'plugins_func.functions.get_news_from_chinanews': (
        '94cec7188f19a2270b3bc50611a3ba3160d4a1d7', 'files/plugins_func/functions/get_news_from_chinanews.py',
        '中新网新闻：阻断 XML 实体膨胀与内网 SSRF 抓取、流式限长、全分支详情截断与异步解析'),
}

# Written only once one of these modules is actually imported (i.e. inside the hub itself), so other
# Python processes that inherit PYTHONPATH (install/update scripts) never touch the status file.
# index_stream is imported at the first conversation, so it stays "not_loaded_yet" until then.
_status = {name: {'state': 'not_loaded_yet', 'change': entry[2]} for name, entry in OVERRIDES.items()}
_status_lock = threading.Lock()


def _is_hub_process() -> bool:
    if os.environ.get('THINKSMART_FORCE_HUB_STATUS') == '1':
        return True
    orig = getattr(sys, 'orig_argv', None) or sys.argv
    if any(x in ('-c', 'pytest', 'unittest', 'py_compile', 'pip') for x in orig[1:4]):
        return False
    candidates = list(orig[1:]) + list(sys.argv[:1])
    return any(os.path.basename(a or '') == 'app.py' for a in candidates)


def _update_status(fullname: str, info: dict):
    try:
        with _status_lock:
            _status[fullname] = info
            if not _is_hub_process():
                return
            data = {
                'pid': os.getpid(),
                'updated': time.strftime('%Y-%m-%d %H:%M:%S'),
                'modules': {k: dict(v) for k, v in _status.items()},
            }
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
                    json.dump(data, f, ensure_ascii=False, indent=1)
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


def _git_blob(path):
    with open(path, 'rb') as f:
        data = f.read()
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


class _OverrideFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        entry = OVERRIDES.get(fullname)
        if entry is None:
            return None
        try:
            blob, ours, what = entry
            official = importlib.machinery.PathFinder.find_spec(fullname, path)
            if official is None or not official.origin:
                return None
            actual = _git_blob(official.origin)
            if actual != blob:
                _update_status(fullname, {
                    'state': 'official',
                    'change': what,
                    'note': f'官方文件已更新（{actual[:10]}，原基准 {blob[:10]}），'
                            '本次改用官方版本，这项本地改动暂未生效，需要重新适配',
                })
                return None
            ours_path = os.path.join(HERE, ours)
            with open(ours_path, 'rb') as f:          # missing or broken copy -> official module
                compile(f.read(), ours_path, 'exec')
            loader = importlib.machinery.SourceFileLoader(fullname, ours_path)
            spec = importlib.util.spec_from_file_location(fullname, ours_path, loader=loader)
            _update_status(fullname, {'state': 'applied', 'change': what})
            return spec
        except Exception as e:  # never break the hub: fall back to the official module
            _update_status(fullname, {'state': 'official', 'change': entry[2], 'note': f'加载出错：{e!r}'})
            return None


try:
    if not any(isinstance(f, _OverrideFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _OverrideFinder())
except Exception:
    pass
