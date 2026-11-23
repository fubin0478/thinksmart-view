import re
import os
import json
import copy
import wave
import ipaddress
import socket
import stat
import tempfile
import sys
import urllib.parse
import asyncio
import threading
import requests
import subprocess
import numpy as np
import opuslib_next
from io import BytesIO
from contextlib import contextmanager
from core.utils import p3, mp3_decode
from pydub import AudioSegment
from typing import Callable, Any

TAG = __name__


# 绕开 IDE（Trae CN 等）注入的精简版 ffmpeg/ffprobe PATH 占位：调用期间把
# Python 解释器同目录的 bin 加到 PATH 前缀，pydub.utils.which() 会优先找到
# conda env 自带的完整版，调用结束恢复原 PATH，不污染 import 后的全局。
# 环境变量 XIAOZHI_PYDUB_OVERRIDE_FFMPEG=0 时关闭。
#
# 引用计数 + 锁：多线程并发进出 with 块时，只在第一个进入时记下原始 PATH
# 并改写，ref_count 归零时才恢复。这样 finally 不会因多次保存/恢复错位
# 导致 bin 前缀永久残留，也避免 try/finally 契约被多 thread 互相覆盖。
_ffmpeg_path_lock = threading.Lock()
_ffmpeg_ref_count = 0
_ffmpeg_original_path: str | None = None


@contextmanager
def _ffmpeg_path_override():
    if os.environ.get("XIAOZHI_PYDUB_OVERRIDE_FFMPEG", "1") != "1":
        yield
        return
    bin_dir = os.path.dirname(sys.executable)
    ffprobe = os.path.join(bin_dir, "ffprobe")
    ffmpeg = os.path.join(bin_dir, "ffmpeg")
    if not (os.path.isfile(ffprobe) or os.path.isfile(ffmpeg)):
        yield
        return

    global _ffmpeg_ref_count, _ffmpeg_original_path
    with _ffmpeg_path_lock:
        if _ffmpeg_ref_count == 0:
            _ffmpeg_original_path = os.environ.get("PATH", "")
            os.environ["PATH"] = bin_dir + os.pathsep + _ffmpeg_original_path
        _ffmpeg_ref_count += 1
    try:
        yield
    finally:
        with _ffmpeg_path_lock:
            _ffmpeg_ref_count -= 1
            if _ffmpeg_ref_count == 0 and _ffmpeg_original_path is not None:
                os.environ["PATH"] = _ffmpeg_original_path
                _ffmpeg_original_path = None


# 委托给 util.ffmpeg_path_override，import util 后调用方一行就能用
ffmpeg_path_override = _ffmpeg_path_override


def get_local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # Connect to Google's DNS servers
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def is_private_ip(ip_addr):
    """
    Check if an IP address is a private or non-global IP address (compatible with IPv4 and IPv6).

    @param {string} ip_addr - The IP address to check.
    @return {bool} True if the IP address is private or non-global, False otherwise.
    """
    if not isinstance(ip_addr, str) or not ip_addr.strip():
        return False
    try:
        raw = ip_addr.strip().split("%", 1)[0]
        ip_obj = ipaddress.ip_address(raw)
        if getattr(ip_obj, "ipv4_mapped", None) is not None:
            ip_obj = ip_obj.ipv4_mapped
        return (
            not ip_obj.is_global
            or ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_unspecified
            or ip_obj.is_reserved
        )
    except ValueError:
        return False


def get_ip_info(ip_addr, logger):
    try:
        # 导入全局缓存管理器
        from core.utils.cache.manager import cache_manager, CacheType

        raw_ip = str(ip_addr).strip() if ip_addr else ""
        if raw_ip and not is_private_ip(raw_ip):
            try:
                ip_obj = ipaddress.ip_address(raw_ip.split("%", 1)[0])
                cache_key = str(ip_obj.ipv4_mapped or ip_obj)
            except ValueError:
                cache_key = ""
        else:
            cache_key = ""
        # 先从缓存获取
        cached_ip_info = cache_manager.get(CacheType.IP_INFO, cache_key)
        if cached_ip_info is not None:
            return cached_ip_info

        # 缓存未命中，调用API（直连国内接口，避免走海外代理把所在地识别成代理节点）
        url = f"https://whois.pconline.com.cn/ipJson.jsp?json=true&ip={urllib.parse.quote(cache_key, safe=':')}"
        with requests.get(url, timeout=(2, 4), stream=True, proxies={"http": None, "https": None}) as r:
            r.raise_for_status()
            if hasattr(r, "iter_content"):
                buf = bytearray()
                for chunk in r.iter_content(chunk_size=16384):
                    if chunk:
                        buf.extend(chunk)
                        if len(buf) > 65536:
                            raise ValueError("ipJson response too large")
                enc = r.encoding or "gb18030"
                resp = json.loads(buf.decode(enc, errors="replace"))
            else:
                resp = r.json()
        ip_info = {"city": resp.get("city") if isinstance(resp, dict) else None}

        # 存入缓存
        cache_manager.set(CacheType.IP_INFO, cache_key, ip_info)
        return ip_info
    except Exception as e:
        logger.bind(tag=TAG).error(f"Error getting client ip info: {e}")
        return {}


def write_json_file(file_path, data):
    """将数据原子写入 JSON 文件"""
    target = os.fspath(file_path)
    if os.path.islink(target):
        raise OSError(f"Refusing symlink JSON target: {target}")
    parent = os.path.dirname(os.path.abspath(target)) or "."
    os.makedirs(parent, exist_ok=True)
    mode = 0o600
    try:
        st = os.lstat(target)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"Refusing non-regular JSON target: {target}")
        mode = stat.S_IMODE(st.st_mode)
    except FileNotFoundError:
        pass
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(target)}.", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            os.fchmod(file.fileno(), mode)
            json.dump(data, file, ensure_ascii=False, indent=4)
            file.flush()
            os.fsync(file.fileno())
        if os.path.islink(target):
            raise OSError(f"Refusing symlink JSON target: {target}")
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def remove_punctuation_and_length(text):
    # 全角符号和半角符号的Unicode范围
    full_width_punctuations = (
        "！＂＃＄％＆＇（）＊＋，－。／：；＜＝＞？＠［＼］＾＿｀｛｜｝～"
    )
    half_width_punctuations = r'!"#$%&\'()*+,-./:;<=>?@[\]^_`{|}~'
    space = " "  # 半角空格
    full_width_space = "　"  # 全角空格

    # 去除全角和半角符号以及空格
    result = "".join(
        [
            char
            for char in text
            if char not in full_width_punctuations
            and char not in half_width_punctuations
            and char not in space
            and char not in full_width_space
        ]
    )

    if result == "Yeah":
        return 0, ""
    return len(result), result


def check_model_key(modelType, modelKey):
    if "你" in modelKey:
        return f"配置错误: {modelType} 的 API key 未设置,当前值为: {modelKey}"
    return None


def parse_string_to_list(value, separator=";"):
    """
    将输入值转换为列表
    Args:
        value: 输入值，可以是 None、字符串或列表
        separator: 分隔符，默认为分号
    Returns:
        list: 处理后的列表
    """
    if value is None or value == "":
        return []
    elif isinstance(value, str):
        return [item.strip() for item in value.split(separator) if item.strip()]
    elif isinstance(value, list):
        return value
    return []


def check_ffmpeg_installed() -> bool:
    """
    检查当前环境中是否已正确安装并可执行 ffmpeg。

    Returns:
        bool: 如果 ffmpeg 正常可用，返回 True；否则抛出 ValueError 异常。

    Raises:
        ValueError: 当检测到 ffmpeg 未安装或依赖缺失时，抛出详细的提示信息。
    """
    try:
        # 尝试执行 ffmpeg 命令
        result = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,  # 非零退出码会触发 CalledProcessError
            timeout=5,
        )

        output = (result.stdout + result.stderr).lower()
        if "ffmpeg version" in output:
            return True

        # 如果未检测到版本信息，也视为异常情况
        raise ValueError("未检测到有效的 ffmpeg 版本输出。")

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        # 提取错误输出
        stderr_output = ""
        if isinstance(e, subprocess.CalledProcessError):
            stderr_output = (e.stderr or "").strip()
        else:
            stderr_output = str(e).strip()

        # 构建基础错误提示
        error_msg = [
            "❌ 检测到 ffmpeg 无法正常运行。\n",
            "建议您：",
            "1. 确认已正确激活 conda 环境；",
            "2. 查阅项目安装文档，了解如何在 conda 环境中安装 ffmpeg。\n",
        ]

        # 🎯 针对具体错误信息提供额外提示
        if "libiconv.so.2" in stderr_output:
            error_msg.append("⚠️ 发现缺少依赖库：libiconv.so.2")
            error_msg.append("解决方法：在当前 conda 环境中执行：")
            error_msg.append("   conda install -c conda-forge libiconv\n")
        elif (
            "no such file or directory" in stderr_output
            and "ffmpeg" in stderr_output.lower()
        ):
            error_msg.append("⚠️ 系统未找到 ffmpeg 可执行文件。")
            error_msg.append("解决方法：在当前 conda 环境中执行：")
            error_msg.append("   conda install -c conda-forge ffmpeg\n")
        else:
            error_msg.append("错误详情：")
            error_msg.append(stderr_output or "未知错误。")

        # 抛出详细异常信息
        raise ValueError("\n".join(error_msg)) from e


def extract_json_from_string(input_string):
    """提取字符串中的 JSON 部分"""
    pattern = r"(\{.*\})"
    match = re.search(pattern, input_string, re.DOTALL)  # 添加 re.DOTALL
    if match:
        return match.group(1)  # 返回提取的 JSON 字符串
    return None


def audio_to_data_stream(
    audio_file_path, is_opus=True, callback: Callable[[Any], Any] = None, sample_rate=16000, opus_encoder=None
) -> None:
    with _ffmpeg_path_override():
        _audio_to_data_stream_inner(
            audio_file_path, is_opus, callback, sample_rate, opus_encoder
        )


def _audio_to_data_stream_inner(
    audio_file_path, is_opus, callback, sample_rate, opus_encoder
):
    file_type = os.path.splitext(audio_file_path)[1]
    if file_type:
        file_type = file_type.lstrip(".")
    # -nostdin：避免 ffmpeg 阻塞等 stdin
    audio = AudioSegment.from_file(
        audio_file_path, format=file_type, parameters=["-nostdin"]
    )
    audio = audio.set_channels(1).set_frame_rate(sample_rate).set_sample_width(2)
    pcm_to_data_stream(audio.raw_data, is_opus, callback, sample_rate, opus_encoder)


async def audio_to_data(
    audio_file_path: str, is_opus: bool = True, use_cache: bool = True
) -> list[bytes]:
    """
    将音频文件转换为Opus/PCM编码的帧列表
    Args:
        audio_file_path: 音频文件路径
        is_opus: 是否进行Opus编码
        use_cache: 是否使用缓存
    """
    from core.utils.cache.manager import cache_manager
    from core.utils.cache.config import CacheType

    # 生成缓存键，包含文件路径和编码类型
    cache_key = f"{audio_file_path}:{is_opus}"

    # 尝试从缓存获取结果
    if use_cache:
        cached_result = cache_manager.get(CacheType.AUDIO_DATA, cache_key)
        if cached_result is not None:
            return cached_result

    def _sync_audio_to_data():
        # 获取文件后缀名
        file_type = os.path.splitext(audio_file_path)[1]
        if file_type:
            file_type = file_type.lstrip(".")
        # 读取音频文件，-nostdin 参数：不要从标准输入读取数据，否则FFmpeg会阻塞
        with _ffmpeg_path_override():
            audio = AudioSegment.from_file(
                audio_file_path, format=file_type, parameters=["-nostdin"]
            )

        # 转换为单声道/16kHz采样率/16位小端编码（确保与编码器匹配）
        audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)

        # 获取原始PCM数据（16位小端）
        raw_data = audio.raw_data

        encoder = None
        if is_opus:
            # 初始化Opus编码器
            encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)
            encoder.bitrate = 64000

        try:
            # 编码参数
            frame_duration = 60  # 60ms per frame
            frame_size = int(16000 * frame_duration / 1000)  # 960 samples/frame

            datas = []
            # 按帧处理所有音频数据（包括最后一帧可能补零）
            for i in range(0, len(raw_data), frame_size * 2):  # 16bit=2bytes/sample
                # 获取当前帧的二进制数据
                chunk = raw_data[i : i + frame_size * 2]

                # 如果最后一帧不足，补零
                if len(chunk) < frame_size * 2:
                    chunk += b"\x00" * (frame_size * 2 - len(chunk))

                if is_opus:
                    # 转换为numpy数组处理
                    np_frame = np.frombuffer(chunk, dtype=np.int16)
                    # 编码Opus数据
                    frame_data = encoder.encode(np_frame.tobytes(), frame_size)
                else:
                    frame_data = chunk if isinstance(chunk, bytes) else bytes(chunk)

                datas.append(frame_data)

            return datas
        finally:
            if encoder is not None:
                try:
                    del encoder
                except Exception:
                    pass

    loop = asyncio.get_running_loop()
    # 在单独的线程中执行同步的音频处理操作
    result = await loop.run_in_executor(None, _sync_audio_to_data)

    # 将结果存入缓存，使用配置中定义的TTL（10分钟）
    if use_cache:
        cache_manager.set(CacheType.AUDIO_DATA, cache_key, result)

    return result


def audio_bytes_to_data_stream(
    audio_bytes, file_type, is_opus, callback: Callable[[Any], Any], sample_rate=16000, opus_encoder=None
) -> None:
    """
    直接用音频二进制数据转为opus/pcm数据，支持wav、mp3、p3
    """
    if file_type == "p3":
        # 直接用p3解码
        return p3.decode_opus_from_bytes_stream(audio_bytes, callback)
    else:
        raw_data = decode_audio_bytes(audio_bytes, file_type, sample_rate)
        pcm_to_data_stream(raw_data, is_opus, callback, sample_rate, opus_encoder)


def decode_audio_bytes(audio_bytes, file_type, sample_rate=16000) -> bytes:
    """[xiaozhi-hub] 音频文件字节 → 16 位单声道 PCM。MP3 先用进程内的 libmpg123（快），不行再用 ffmpeg。"""
    if file_type == "mp3":
        raw_data = mp3_decode.decode_mp3(audio_bytes, sample_rate)
        if raw_data is not None:
            return raw_data
    # 其他格式用pydub
    with _ffmpeg_path_override():
        audio = AudioSegment.from_file(
            BytesIO(audio_bytes), format=file_type, parameters=["-nostdin"]
        )
    audio = audio.set_channels(1).set_frame_rate(sample_rate).set_sample_width(2)
    return audio.raw_data


def pcm_to_data_stream(raw_data, is_opus=True, callback: Callable[[Any], Any] = None, sample_rate=16000, opus_encoder=None):
    """
    将PCM数据流式编码为Opus或直接输出PCM

    Args:
        raw_data: PCM原始数据
        is_opus: 是否编码为Opus
        callback: 回调函数
        sample_rate: 采样率
        opus_encoder: OpusEncoderUtils对象(推荐提供以保持编码器状态连续)
    """
    encoder = None
    using_temp_encoder = False
    if is_opus and opus_encoder is None:
        encoder = opuslib_next.Encoder(sample_rate, 1, opuslib_next.APPLICATION_AUDIO)
        encoder.bitrate = 64000
        using_temp_encoder = True

    try:
        # 编码参数
        frame_duration = 60  # 60ms per frame
        frame_size = int(sample_rate * frame_duration / 1000)  # samples/frame

        # 按帧处理所有音频数据（包括最后一帧可能补零）
        for i in range(0, len(raw_data), frame_size * 2):  # 16bit=2bytes/sample
            # 获取当前帧的二进制数据
            chunk = raw_data[i : i + frame_size * 2]

            # 如果最后一帧不足，补零
            if len(chunk) < frame_size * 2:
                chunk += b"\x00" * (frame_size * 2 - len(chunk))

            if is_opus:
                if using_temp_encoder:
                    # 使用临时编码器（仅用于独立音频场景）
                    np_frame = np.frombuffer(chunk, dtype=np.int16)
                    frame_data = encoder.encode(np_frame.tobytes(), frame_size)
                    callback(frame_data)
                else:
                    # 使用外部编码器（TTS流式场景,保持状态连续）
                    is_last = (i + frame_size * 2 >= len(raw_data))
                    opus_encoder.encode_pcm_to_opus_stream(chunk, end_of_stream=is_last, callback=callback)
            else:
                # PCM模式,直接输出
                frame_data = chunk if isinstance(chunk, bytes) else bytes(chunk)
                callback(frame_data)
    finally:
        if encoder is not None:
            try:
                del encoder
            except Exception:
                pass


def opus_datas_to_wav_bytes(opus_datas, sample_rate=16000, channels=1):
    """
    将opus帧列表解码为wav字节流
    """
    decoder = opuslib_next.Decoder(sample_rate, channels)
    try:
        pcm_datas = []

        frame_duration = 60  # ms
        frame_size = int(sample_rate * frame_duration / 1000)  # 960

        for opus_frame in opus_datas:
            # 解码为PCM（返回bytes，2字节/采样点）
            pcm = decoder.decode(opus_frame, frame_size)
            pcm_datas.append(pcm)

        pcm_bytes = b"".join(pcm_datas)

        # 写入wav字节流
        wav_buffer = BytesIO()
        with wave.open(wav_buffer, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)  # 16bit
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        return wav_buffer.getvalue()
    finally:
        if decoder is not None:
            try:
                del decoder
            except Exception:
                pass


def check_vad_update(before_config, new_config):
    if (
        not isinstance(new_config, dict)
        or not isinstance(before_config, dict)
        or not isinstance(new_config.get("selected_module"), dict)
        or new_config["selected_module"].get("VAD") is None
    ):
        return False
    before_sel = before_config.get("selected_module") or {}
    current_vad_module = before_sel.get("VAD") if isinstance(before_sel, dict) else None
    new_vad_module = new_config["selected_module"]["VAD"]
    if not current_vad_module:
        return True
    before_vad_cfg = ((before_config.get("VAD") or {}).get(current_vad_module)) or {}
    new_vad_cfg = ((new_config.get("VAD") or {}).get(new_vad_module)) or {}
    current_vad_type = (
        current_vad_module
        if not isinstance(before_vad_cfg, dict) or "type" not in before_vad_cfg
        else before_vad_cfg["type"]
    )
    new_vad_type = (
        new_vad_module
        if not isinstance(new_vad_cfg, dict) or "type" not in new_vad_cfg
        else new_vad_cfg["type"]
    )
    return current_vad_type != new_vad_type


def check_asr_update(before_config, new_config):
    if (
        not isinstance(new_config, dict)
        or not isinstance(before_config, dict)
        or not isinstance(new_config.get("selected_module"), dict)
        or new_config["selected_module"].get("ASR") is None
    ):
        return False
    before_sel = before_config.get("selected_module") or {}
    current_asr_module = before_sel.get("ASR") if isinstance(before_sel, dict) else None
    new_asr_module = new_config["selected_module"]["ASR"]

    # 如果模块名称不同，就需要更新
    if current_asr_module != new_asr_module:
        return True

    # 如果模块名称相同，再比较类型
    before_asr_cfg = ((before_config.get("ASR") or {}).get(current_asr_module)) or {}
    new_asr_cfg = ((new_config.get("ASR") or {}).get(new_asr_module)) or {}
    current_asr_type = (
        current_asr_module
        if not isinstance(before_asr_cfg, dict) or "type" not in before_asr_cfg
        else before_asr_cfg["type"]
    )
    new_asr_type = (
        new_asr_module
        if not isinstance(new_asr_cfg, dict) or "type" not in new_asr_cfg
        else new_asr_cfg["type"]
    )
    return current_asr_type != new_asr_type


def filter_sensitive_info(config: dict) -> dict:
    """
    过滤配置中的敏感信息
    Args:
        config: 原始配置字典
    Returns:
        过滤后的配置字典
    """
    if not isinstance(config, dict):
        return config
    sensitive_keys = (
        "api_key",
        "personal_access_token",
        "access_token",
        "token",
        "secret",
        "access_key_secret",
        "secret_key",
        "metaso_key",
        "amap_key",
        "app_key",
        "private_key",
        "auth_key",
        "password",
        "passwd",
        "cookie",
    )

    url_secret_params = sensitive_keys + ("key", "sig", "signature", "credential")

    def _redact_url_secrets(raw_str: str) -> str:
        s_strip = raw_str.strip()
        if not s_strip.startswith(("http://", "https://", "ws://", "wss://")):
            return raw_str
        try:
            parts = urllib.parse.urlsplit(s_strip)
        except ValueError:
            return raw_str
        changed = False
        netloc = parts.netloc
        if parts.username is not None or parts.password is not None:
            host_port = netloc.rsplit("@", 1)[-1]
            netloc = f"***@{host_port}"
            changed = True
        query = parts.query
        if query:
            q_items = urllib.parse.parse_qsl(query, keep_blank_values=True)
            new_q = []
            for qk, qv in q_items:
                qk_low = qk.lower()
                if any(sp in qk_low for sp in url_secret_params):
                    new_q.append((qk, "***"))
                    changed = True
                else:
                    new_q.append((qk, qv))
            if changed:
                query = urllib.parse.urlencode(new_q)
        if not changed:
            return raw_str
        return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))

    def _filter_value(val):
        if isinstance(val, dict):
            return _filter_dict(val)
        if isinstance(val, list):
            return [_filter_value(i) for i in val]
        if isinstance(val, tuple):
            return tuple(_filter_value(i) for i in val)
        if isinstance(val, str):
            try:
                json_data = json.loads(val)
                if isinstance(json_data, (dict, list)):
                    return json.dumps(_filter_value(json_data), ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                pass
            return _redact_url_secrets(val)
        return val

    def _filter_dict(d: dict) -> dict:
        filtered = {}
        for k, v in d.items():
            k_low = str(k).lower()
            if any(sensitive in k_low for sensitive in sensitive_keys):
                filtered[k] = "***"
            else:
                filtered[k] = _filter_value(v)
        return filtered

    return _filter_dict(copy.deepcopy(config))


def get_vision_url(config: dict) -> str:
    """获取 vision URL

    Args:
        config: 配置字典

    Returns:
        str: vision URL
    """
    server_config = config["server"]
    vision_explain = server_config.get("vision_explain", "")
    if "你的" in vision_explain:
        local_ip = get_local_ip()
        port = int(server_config.get("http_port", 8003))
        vision_explain = f"http://{local_ip}:{port}/mcp/vision/explain"
    return vision_explain


def is_valid_image_file(file_data: bytes) -> bool:
    """
    检查文件数据是否为有效的图片格式

    Args:
        file_data: 文件的二进制数据

    Returns:
        bool: 如果是有效的图片格式返回True，否则返回False
    """
    if not isinstance(file_data, (bytes, bytearray)):
        return False
    # 常见图片格式的魔数（文件头）；WEBP需校验RIFF+WEBP，避免误判WAV/AVI
    image_signatures = (
        b"\xff\xd8\xff",
        b"\x89PNG\r\n\x1a\n",
        b"GIF87a",
        b"GIF89a",
        b"BM",
        b"II*\x00",
        b"MM\x00*",
    )
    if any(file_data.startswith(sig) for sig in image_signatures):
        return True
    if len(file_data) >= 12 and file_data[:4] == b"RIFF" and file_data[8:12] == b"WEBP":
        return True
    return False


def sanitize_tool_name(name: str) -> str:
    """Sanitize tool names for OpenAI compatibility."""
    # 支持中文、英文字母、数字、下划线和连字符
    return re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]", "_", name)


def validate_mcp_endpoint(mcp_endpoint: str) -> bool:
    """
    校验MCP接入点格式

    Args:
        mcp_endpoint: MCP接入点字符串

    Returns:
        bool: 是否有效
    """
    # 1. 检查是否以ws开头
    if not mcp_endpoint.startswith("ws"):
        return False

    # 2. 检查是否包含key、call字样
    if "key" in mcp_endpoint.lower() or "call" in mcp_endpoint.lower():
        return False

    # 3. 检查是否包含/mcp/字样
    if "/mcp/" not in mcp_endpoint:
        return False

    return True

def get_system_error_response(config: dict) -> str:
    """获取系统错误时的回复

    Args:
        config: 配置字典

    Returns:
        str: 系统错误时的回复
    """
    return config.get("system_error_response", "主人，小智现在有点忙，我们稍后再试吧。")
