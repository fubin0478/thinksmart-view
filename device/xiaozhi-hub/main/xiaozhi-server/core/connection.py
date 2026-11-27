import os
import sys
import copy
import json
import re
import uuid
import time
import queue
import asyncio
import threading
import traceback
import subprocess
import websockets
import opuslib_next
import numpy as np

from core.utils.util import (
    extract_json_from_string,
)
from typing import Dict, Any
from collections import deque
from core.utils.modules_initialize import (
    initialize_modules,
    initialize_tts,
    initialize_asr,
)
from core.providers.tts.default import DefaultTTS
from concurrent.futures import ThreadPoolExecutor
from core.utils.dialogue import Message, Dialogue
from core.providers.asr.dto.dto import InterfaceType
from core.handle.textHandle import handleTextMessage
from core.providers.tools.unified_tool_handler import UnifiedToolHandler
from plugins_func.loadplugins import auto_import_modules
from plugins_func.register import Action, ActionResponse, all_function_registry, module_func_map
from core.auth import AuthenticationError

_LAST_CONN_TRIM_MONO = 0.0


def _trim_conn_heap() -> bool:
    global _LAST_CONN_TRIM_MONO
    now_m = time.monotonic()
    if now_m - _LAST_CONN_TRIM_MONO < 30.0:
        return False
    _LAST_CONN_TRIM_MONO = now_m
    try:
        import ctypes
        import gc
        gc.collect()
        ctypes.CDLL('libc.so.6').malloc_trim(0)
        return True
    except Exception:
        return False
from core.providers.tts.dto.dto import ContentType, TTSMessageDTO, SentenceType
from config.logger import setup_logging, build_module_string, create_connection_logger
from core.utils.prompt_manager import PromptManager
from core.utils.util import get_system_error_response
from core.utils import reply_progress, textUtils, turn_gen
from core.utils.tool_markup import ToolMarkupFilter, strip_tool_markup
from core.agent import chat_history, llm_router


TAG = __name__

auto_import_modules("plugins_func.functions")


class TTSException(RuntimeError):
    pass

# direct_answer 虚拟工具定义
# 不是真实工具，是路由机制：将"调不调工具"的二选一变为"调哪个"的多选，防止小模型误触发真实工具
DIRECT_ANSWER_TOOL = {
    "type": "function",
    "function": {
        "name": "direct_answer",
        "description": "当用户的请求不匹配其他任何工具时，可用此选项直接回复。将回复内容写在response参数里。",
        "parameters": {
            "type": "object",
            "properties": {
                "response": {
                    "type": "string",
                    "description": "你回复用户的完整内容",
                },
            },
            "required": ["response"],
        },
    },
}


_SENSITIVE_HEADER_KEYS = frozenset({
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "secret",
    "x-auth-token",
})


def _redact_headers(headers: Dict[str, Any]) -> Dict[str, Any]:
    safe = {}
    for k, v in (headers or {}).items():
        ks = str(k)
        kl = ks.lower()
        if kl in _SENSITIVE_HEADER_KEYS or any(
            tok in kl for tok in ("secret", "token", "password", "authorization", "cookie")
        ):
            safe[ks] = "***"
        else:
            vs = str(v)
            safe[ks] = vs[:256] if len(vs) > 256 else vs
    return safe


def _sanitize_device_id(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    cleaned = re.sub(r"[\x00-\x1f\x7f/\\]+", "", raw.strip())
    if not cleaned or cleaned in (".", ".."):
        return None
    return cleaned[:128]


class ConnectionHandler:
    def __init__(
            self,
            config: Dict[str, Any],
            _vad,
            _asr,
            _llm,
            _memory,
            _intent,
            server=None,
    ):
        self.common_config = config
        self.config = copy.deepcopy(config)
        self.session_id = str(uuid.uuid4())
        self.logger = setup_logging()
        self.server = server  # 保存server实例的引用

        self.need_bind = False  # 是否需要绑定设备
        self.bind_completed_event = asyncio.Event()
        self.bind_code = None  # 绑定设备的验证码
        self.last_bind_prompt_time = 0  # 上次播放绑定提示的时间戳(秒)
        self.bind_prompt_interval = 60  # 绑定提示播放间隔(秒)

        self.read_config_from_api = False

        self.websocket: websockets.ServerConnection | None = None
        self.headers = None
        self.device_id = None
        self.client_ip = None
        self.prompt = None
        self.welcome_msg = None
        self.audio_format = "opus"
        self.sample_rate = 24000  # 默认采样率，从客户端 hello 消息中动态更新

        # 客户端状态相关
        self.client_abort = False
        self.client_is_speaking = False
        self.client_listen_mode = "auto"
        self.client_aec = False  # 是否启用了服务端AEC

        # 线程任务相关
        self.loop = None  # 在 handle_connection 中获取运行中的事件循环
        self.stop_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=5)

        # 依赖的组件
        self.vad = None
        self.asr = None
        self.tts = None
        self._asr = _asr
        self._vad = _vad
        self.llm = _llm
        self.memory = _memory
        self.intent = _intent

        # vad相关变量
        self.client_audio_buffer = bytearray()
        self.client_have_voice = False
        self.client_voice_window = deque(maxlen=5)
        self.first_activity_time = 0.0  # 记录首次活动的时间（毫秒）
        self.last_activity_time = 0.0  # 统一的活动时间戳（毫秒）
        self.vad_last_voice_time = 0.0  # 记录用户最后一次说话的时间（毫秒）
        self.client_voice_stop = False
        self.last_is_voice = False

        # asr相关变量
        # 因为实际部署时可能会用到公共的本地ASR，不能把变量暴露给公共ASR
        # 所以涉及到ASR的变量，需要在这里定义，属于connection的私有变量
        self.asr_audio = []  # 存储PCM帧列表，供VAD和ASR共享
        self.asr_audio_queue = queue.Queue()
        self.current_speaker = None  # 存储当前说话人
        self.introduced_speakers = set()  # 已"首次引入"的说话人，控制只在首轮带名字
        self.system_introduced_speakers = set()  # 已在 system 注入过身份的说话人，控制 system 身份只首轮出现

        # llm相关变量
        self.dialogue = Dialogue()

        # tts相关变量
        self.sentence_id = None
        # 处理TTS响应没有文本返回
        self.tts_MessageText = ""

        self.func_handler = None

        self.cmd_exit = self.config["exit_commands"]

        # 是否在聊天结束后关闭连接
        self.close_after_chat = False
        self.load_function_plugin = False
        self.intent_type = "nointent"

        self.timeout_seconds = (
                int(self.config.get("close_connection_no_voice_time", 120)) + 60
        )  # 在原来第一道关闭的基础上加60秒，进行二道关闭
        self.timeout_task = None

        # {"mcp":true} 表示启用MCP功能
        self.features = None

        # 标记连接是否来自MQTT
        self.conn_from_mqtt_gateway = False

        # 初始化提示词管理器
        self.prompt_manager = PromptManager(self.config, self.logger)

        # 初始化通话状态
        self.calling = False
        # 标记当前是否为来电接听模式
        self.incoming_call = None

    async def handle_connection(self, ws: websockets.ServerConnection):
        try:
            # 获取运行中的事件循环（必须在异步上下文中）
            self.loop = asyncio.get_running_loop()
            self._loop_thread = threading.current_thread()

            # 获取并验证headers（仅本机反向代理转发才信任 x-real-ip / x-forwarded-for，日志脱敏鉴权头）
            self.headers = {str(k).lower(): v for k, v in ws.request.headers.items()}
            peer_ip = ws.remote_address[0] if getattr(ws, "remote_address", None) else ""
            real_ip = (
                (self.headers.get("x-real-ip") or self.headers.get("x-forwarded-for"))
                if peer_ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
                else None
            )
            if real_ip:
                self.client_ip = real_ip.split(",")[0].strip()[:64] or peer_ip
            else:
                self.client_ip = peer_ip
            self.logger.bind(tag=TAG).info(
                f"{self.client_ip} conn - Headers: {_redact_headers(self.headers)}"
            )

            self.device_id = _sanitize_device_id(self.headers.get("device-id", None))
            # [xiaozhi-hub] 通知 LLM 有新连接（claude_cli 按设备保存会话），切到这台设备上次用的模型（呼名），
            # 并登记到管理后台
            from core.agent import llm_router
            await llm_router.on_conn_open_async(self)
            from core.admin import connections as hub_connections
            hub_connections.register(self)

            # 认证通过,继续处理
            self.websocket = ws

            # 检查是否来自MQTT连接
            request_path = ws.request.path
            self.conn_from_mqtt_gateway = request_path.endswith("?from=mqtt_gateway")
            if self.conn_from_mqtt_gateway:
                self.logger.bind(tag=TAG).info("连接来自:MQTT网关")

            # 初始化活动时间戳
            self.first_activity_time = time.time() * 1000
            self.last_activity_time = time.time() * 1000

            # 启动超时检查任务
            self.timeout_task = asyncio.create_task(self._check_timeout())

            # 启动AEC缓存清理任务
            self._aec_cache_cleanup_task = asyncio.create_task(self._check_aec_cache_expiry())

            self.welcome_msg = self.config["xiaozhi"]
            self.welcome_msg["session_id"] = self.session_id

            # 从配置中读取采样率
            self.sample_rate = self.welcome_msg["audio_params"]["sample_rate"]
            self.logger.bind(tag=TAG).info(f"配置输出音频采样率为: {self.sample_rate}")

            # 在后台初始化配置和组件（完全不阻塞主循环）
            asyncio.create_task(self._background_initialize())

            try:
                async for message in self.websocket:
                    await self._route_message(message)
            except websockets.exceptions.ConnectionClosed:
                self.logger.bind(tag=TAG).info("客户端断开连接")

        except AuthenticationError as e:
            self.logger.bind(tag=TAG).error(f"Authentication failed: {str(e)}")
            return
        except Exception as e:
            stack_trace = traceback.format_exc()
            self.logger.bind(tag=TAG).error(f"Connection error: {str(e)}-{stack_trace}")
            return
        finally:
            try:
                await self._save_and_close(ws)
            except Exception as final_error:
                self.logger.bind(tag=TAG).error(f"最终清理时出错: {final_error}")
                # 确保即使保存记忆失败，也要关闭连接
                try:
                    await self.close(ws)
                except Exception as close_error:
                    self.logger.bind(tag=TAG).error(
                        f"强制关闭连接时出错: {close_error}"
                    )

    async def _save_and_close(self, ws):
        """保存记忆并关闭连接"""
        try:
            # 守护线程2：走老流程记忆保存（仅记忆，不含标题）
            if self.memory:
                # 使用线程池异步保存记忆
                def save_memory_task():
                    try:
                        # 创建新事件循环（避免与主循环冲突）
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(
                            self.memory.save_memory(
                                # [xiaozhi-hub] 工具调用示例是假对话，不能进记忆（上游 mem_local_short 会把它们总结进去）
                                [m for m in self.dialogue.dialogue if not m.is_temporary], self.session_id
                            )
                        )
                    except Exception as e:
                        self.logger.bind(tag=TAG).error(f"保存记忆失败: {e}")
                    finally:
                        try:
                            loop.close()
                        except Exception:
                            pass

                # 启动线程保存记忆，不等待完成
                threading.Thread(target=save_memory_task, daemon=True).start()
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"保存记忆失败: {e}")
        finally:
            # 立即关闭连接，不等待记忆保存完成
            try:
                await self.close(ws)
            except Exception as close_error:
                self.logger.bind(tag=TAG).error(
                    f"保存记忆后关闭连接失败: {close_error}"
                )

    async def _discard_message_with_bind_prompt(self):
        """丢弃消息并检查是否需要播放绑定提示"""
        current_time = time.time()
        # 检查是否需要播放绑定提示
        if current_time - self.last_bind_prompt_time >= self.bind_prompt_interval:
            self.last_bind_prompt_time = current_time
            # 复用现有的绑定提示逻辑
            from core.handle.receiveAudioHandle import check_bind_device

            asyncio.create_task(check_bind_device(self))

    async def _route_message(self, message):
        """消息路由"""
        # 检查是否已经获取到真实的绑定状态
        if not self.bind_completed_event.is_set():
            # 还没有获取到真实状态，等待直到获取到真实状态或超时
            try:
                await asyncio.wait_for(self.bind_completed_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                # 超时仍未获取到真实状态，丢弃消息
                await self._discard_message_with_bind_prompt()
                return

        # 已经获取到真实状态，检查是否需要绑定
        if self.need_bind:
            # 需要绑定，丢弃消息
            await self._discard_message_with_bind_prompt()
            return

        # 不需要绑定，继续处理消息

        if isinstance(message, str):
            await handleTextMessage(self, message)
        elif isinstance(message, bytes):
            if self.vad is None or self.asr is None:
                return

            # 处理来自MQTT网关的音频包
            if self.conn_from_mqtt_gateway and len(message) >= 16:
                handled = await self._process_mqtt_audio_message(message)
                if handled:
                    return

            # 入口处直接解码PCM，避免VAD和ASR重复解码
            pcm_frame = self._decode_opus_packet(message)
            if pcm_frame:
                self.asr_audio_queue.put(pcm_frame)

    async def _process_mqtt_audio_message(self, message):
        """
        处理来自MQTT网关的音频消息，解析16字节头部并提取音频数据，在入队前进行AEC处理

        Args:
            message: 包含头部的音频消息

        Returns:
            bool: 是否成功处理了消息
        """
        try:
            # 解析timestamp
            timestamp = int.from_bytes(message[8:12], "big")

            audio_data = message[16:]
            # 入口直接解码PCM
            pcm_frame = self._decode_opus_packet(audio_data)
            if not pcm_frame:
                return True

            # AEC处理：如果timestamp>0且启用了AEC
            if timestamp > 0 and self.client_aec:
                pcm_frame = self._apply_aec(timestamp, pcm_frame)

            self.asr_audio_queue.put(pcm_frame)
            return True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"解析WebSocket音频包失败: {e}")

        # 处理失败，返回False表示需要继续处理
        return False

    def _apply_aec(self, timestamp: int, pcm_frame: bytes) -> bytes:
        """应用AEC处理 - 综合算法：互相关延迟估计 + Wiener滤波 + 频谱减法"""
        try:
            if not pcm_frame or len(pcm_frame) == 0:
                return pcm_frame

            if not hasattr(self, "aec_audio_cache") or not self.aec_audio_cache:
                return pcm_frame

            mic_audio = np.frombuffer(pcm_frame, dtype=np.int16).astype(np.float32)
            mic_rms = np.sqrt(np.mean(mic_audio ** 2))

            if mic_rms < 100:
                return pcm_frame

            sorted_timestamps = sorted(self.aec_audio_cache.keys())
            if len(sorted_timestamps) < 2:
                return pcm_frame

            # ========== 匹配参考帧（对数功率谱匹配） ==========
            n = len(mic_audio)

            # 找最接近的timestamp作为起点
            closest_idx = min(range(len(sorted_timestamps)), key=lambda i: abs(sorted_timestamps[i] - timestamp))

            # 预计算 mic_audio 的对数功率谱（循环内共用，避免重复FFT）
            mic_window = np.hanning(n)
            mic_fft = np.fft.rfft(mic_audio * mic_window)
            mic_psd = np.abs(mic_fft) ** 2
            mic_log_psd = 10 * np.log10(mic_psd + 1e-8)
            mic_P_xx = np.dot(mic_log_psd, mic_log_psd)

            # 用对数功率谱匹配找最佳帧：前后各找2帧
            best_corr = -1
            best_ref_idx = closest_idx
            best_ref_rms = 0.0

            for offset in range(-2, 3):  # T-2, T-1, T, T+1, T+2
                test_idx = closest_idx + offset
                if test_idx < 0 or test_idx >= len(sorted_timestamps):
                    continue
                test_ts = sorted_timestamps[test_idx]
                test_ref = np.frombuffer(self.aec_audio_cache[test_ts], dtype=np.int16).astype(np.float32)
                test_ref_rms = np.sqrt(np.mean(test_ref ** 2))
                if test_ref_rms < 50:
                    continue

                # 对齐参考信号长度再做对数功率谱相关性（兼容客户端 20ms 与服务端 60ms 帧长差异）
                if len(test_ref) > n:
                    test_ref = test_ref[:n]
                elif len(test_ref) < n:
                    test_ref = np.pad(test_ref, (0, n - len(test_ref)))
                test_fft = np.fft.rfft(test_ref * mic_window)
                test_psd = np.abs(test_fft) ** 2
                test_log_psd = 10 * np.log10(test_psd + 1e-8)
                P_xy = np.dot(mic_log_psd, test_log_psd)
                P_yy = np.dot(test_log_psd, test_log_psd)
                corr = abs(P_xy) / (np.sqrt(mic_P_xx) * np.sqrt(P_yy) + 1e-8)

                if corr > best_corr:
                    best_corr = corr
                    best_ref_idx = test_idx
                    best_ref_rms = test_ref_rms

            best_ts = sorted_timestamps[best_ref_idx]
            best_ref = np.frombuffer(self.aec_audio_cache[best_ts], dtype=np.int16).astype(np.float32)
            ref_rms = best_ref_rms

            if ref_rms < 50:
                return pcm_frame

            # 对齐参考信号（直接截取相同长度）
            aligned_ref = best_ref[:n]
            if len(aligned_ref) < n:
                aligned_ref = np.pad(aligned_ref, (0, n - len(aligned_ref)))

            # ========== 频域 AEC 处理（谱减法） ==========
            # 时域信号经过声学路径后相位失真，导致时域相关性低且P_xy正负不定
            # 频域幅度谱不受相位影响，对数功率谱相关性稳定在0.97+
            # 公式：result_mag = max(|mic_fft| - |ref_fft| * scale * coef, 0)

            mic_mag = np.abs(mic_fft)
            mic_phase = np.angle(mic_fft)
            ref_fft = np.fft.rfft(aligned_ref * np.hanning(n))
            ref_mag = np.abs(ref_fft)

            # 频域计算回声比例 scale
            scale = np.sum(mic_mag * ref_mag) / (np.dot(ref_mag, ref_mag) + 1e-8)

            # 自适应系数：根据scale和coh动态调整
            # scale大（回声强）-> coef大；coh高（匹配准）-> coef大
            raw_coef = 1.0 + scale * 3 + (best_corr - 0.97) * 30
            coef = max(0.5, min(3.0, raw_coef))

            # 谱减法（过减 + 半波整流）
            echo_mag = ref_mag * scale * coef
            result_mag = np.maximum(mic_mag - echo_mag * 1.5, mic_mag * 0.1)

            # 保留相位重建信号
            result_fft = result_mag * np.exp(1j * mic_phase)
            output = np.fft.irfft(result_fft, n)

            # 高置信度是纯回声时，再压一下确保VAD检测不到
            if best_corr >= 0.97 and ref_rms > 500:
                output = output * 0.3

            # 后处理：限幅
            output = np.clip(output, -32768, 32767)

            # 转换为bytes
            result = output.astype(np.int16).tobytes()

            return result

        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"[AEC] 处理失败: {e}")
            return pcm_frame

    def _decode_opus_packet(self, opus_packet: bytes) -> bytes:
        """
        解码Opus数据包为PCM数据

        Args:
            opus_packet: Opus编码的音频数据

        Returns:
            bytes: 解码后的PCM数据，失败返回None
        """
        try:
            if not opus_packet or len(opus_packet) == 0:
                return None

            self._init_connection_state(self)
            pcm_frame = self._connection_opus_decoder.decode(opus_packet, 960)
            return pcm_frame
        except Exception as e:
            self.logger.bind(tag=TAG).debug(f"Opus解码失败: {e}")
            return None

    def _init_connection_state(self, conn):
        """为连接初始化独立的Opus解码器"""
        if not hasattr(conn, "_connection_opus_decoder"):
            conn._connection_opus_decoder = opuslib_next.Decoder(16000, 1)

    async def handle_restart(self, message):
        """处理服务器重启请求"""
        try:

            self.logger.bind(tag=TAG).info("收到服务器重启指令，准备执行...")

            # 发送确认响应
            await self.websocket.send(
                json.dumps(
                    {
                        "type": "server",
                        "status": "success",
                        "message": "服务器重启中...",
                        "content": {"action": "restart"},
                    }
                )
            )

            # 异步执行重启操作
            def restart_server():
                """实际执行重启的方法"""
                time.sleep(1)
                self.logger.bind(tag=TAG).info("执行服务器重启...")
                if os.environ.get("INVOCATION_ID"):
                    import signal as _sig
                    os.kill(os.getpid(), _sig.SIGTERM)
                    return
                subprocess.Popen(
                    [sys.executable, "app.py"],
                    stdin=sys.stdin,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                    start_new_session=True,
                )
                os._exit(0)

            # 使用线程执行重启避免阻塞事件循环
            threading.Thread(target=restart_server, daemon=True).start()

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"重启失败: {str(e)}")
            await self.websocket.send(
                json.dumps(
                    {
                        "type": "server",
                        "status": "error",
                        "message": f"Restart failed: {str(e)}",
                        "content": {"action": "restart"},
                    }
                )
            )

    def _is_closing(self) -> bool:
        return bool(
            getattr(self, "_closed", False)
            or (getattr(self, "stop_event", None) is not None and self.stop_event.is_set())
        )

    def _initialize_components(self):
        try:
            if self._is_closing():
                return
            on_worker = (
                getattr(self, "loop", None) is not None
                and self.loop.is_running()
                and threading.current_thread() is not getattr(self, "_loop_thread", None)
            )
            if self.tts is None:
                self.tts = self._initialize_tts()
            if self._is_closing():
                if self.tts and getattr(self, "loop", None) and self.loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.tts.close(), self.loop)
                return
            # 打开语音合成通道
            tts_fut = asyncio.run_coroutine_threadsafe(
                self.tts.open_audio_channels(self), self.loop
            )
            if on_worker:
                try:
                    tts_fut.result(timeout=5)
                except Exception as e:
                    self.logger.bind(tag=TAG).warning(f"等待TTS通道就绪异常: {e}")
            if self.need_bind:
                self.bind_completed_event.set()
                return
            self.selected_module_str = build_module_string(
                self.config.get("selected_module", {})
            )
            self.logger = create_connection_logger(self.selected_module_str)

            """初始化组件"""
            if self.config.get("prompt") is not None:
                user_prompt = self.config["prompt"]
                # 使用快速提示词进行初始化
                prompt = self.prompt_manager.get_quick_prompt(user_prompt, self.device_id)
                self.change_system_prompt(prompt)
                self.logger.bind(tag=TAG).info(
                    f"快速初始化组件: prompt成功 {prompt[:50]}..."
                )

            """初始化本地组件"""
            if self._is_closing():
                if self.tts:
                    if getattr(self, "loop", None) and self.loop.is_running():
                        asyncio.run_coroutine_threadsafe(self.tts.close(), self.loop)
                    elif getattr(self.tts, "opus_encoder", None):
                        self.tts.opus_encoder.close()
                return
            if self.vad is None:
                self.vad = self._vad
            if self.asr is None:
                self.asr = self._initialize_asr()
            if self._is_closing():
                if getattr(self, "loop", None) and self.loop.is_running():
                    if self.tts:
                        asyncio.run_coroutine_threadsafe(self.tts.close(), self.loop)
                    if self.asr:
                        asyncio.run_coroutine_threadsafe(self.asr.close(), self.loop)
                return

            # 打开语音识别通道
            asr_fut = asyncio.run_coroutine_threadsafe(
                self.asr.open_audio_channels(self), self.loop
            )
            if on_worker:
                try:
                    asr_fut.result(timeout=5)
                except Exception as e:
                    self.logger.bind(tag=TAG).warning(f"等待ASR通道就绪异常: {e}")

            if self._is_closing():
                if getattr(self, "loop", None) and self.loop.is_running():
                    if self.tts:
                        asyncio.run_coroutine_threadsafe(self.tts.close(), self.loop)
                    if self.asr:
                        asyncio.run_coroutine_threadsafe(self.asr.close(), self.loop)
                return
            """加载记忆"""
            self._initialize_memory()
            """加载意图识别"""
            self._initialize_intent()
            if self._is_closing():
                if self.func_handler and getattr(self, "loop", None) and self.loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.func_handler.cleanup(), self.loop)
                return
            """更新系统提示词"""
            self._init_prompt_enhancement()
            """注入工具调用few-shot示例（仅function_call模式）"""
            self._inject_tool_call_fewshot()

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"实例化组件失败: {e}")

    def _init_prompt_enhancement(self):

        # 更新上下文信息
        self.prompt_manager.update_context_info(self, self.client_ip)
        enhanced_prompt = self.prompt_manager.build_enhanced_prompt(
            self.config["prompt"],
            self.device_id,
            self.client_ip,
            emoji_enabled=(self.features or {}).get("emoji", True),
        )
        if enhanced_prompt:
            self.change_system_prompt(enhanced_prompt)
            self.logger.bind(tag=TAG).debug("系统提示词已增强更新")

    def _inject_tool_call_fewshot(self):
        """注入工具调用 few-shot 示例到对话历史（is_temporary）。
        [xiaozhi-hub] 发给模型时由 dialogue.render_fewshot 写成系统提示末尾的一段文字、注明不是真实对话，
        不当成对话轮次发（否则模型会当成用户真说过）；不存聊天记录库、不进记忆。
        """
        if self.intent_type != "function_call":
            return
        if not hasattr(self, "func_handler") or self.func_handler is None:
            return

        tools = self.func_handler.get_functions()
        if not tools:
            return

        tool_names = {t.get("function", {}).get("name") for t in tools}

        # === few-shot 示例（is_temporary）===
        # 展示 direct_answer 携带 response 参数的用法，一次调用完成回复

        # 示例1：direct_answer（回复内容写在 response 参数里，无需递归）
        da_tc_id = "fewshot_da_001"
        self.dialogue.put(Message(role="user", content="给我讲个故事吧", is_temporary=True))
        self.dialogue.put(Message(
            role="assistant",
            tool_calls=[{
                "id": da_tc_id,
                "function": {"arguments": '{"response": "好呀，你想听什么类型的呀？童话、冒险还是搞笑的？选一个我给你开讲~"}', "name": "direct_answer"},
                "type": "function", "index": 0,
            }],
            is_temporary=True,
        ))
        self.dialogue.put(Message(
            role="tool", tool_call_id=da_tc_id,
            content="已直接回复", is_temporary=True,
        ))

        # 示例2：真实工具调用（handle_exit_intent）
        if "handle_exit_intent" in tool_names:
            tc_id = "fewshot_exit_001"
            self.dialogue.put(Message(role="user", content="拜拜", is_temporary=True))
            self.dialogue.put(Message(
                role="assistant",
                tool_calls=[{
                    "id": tc_id,
                    "function": {"arguments": '{"say_goodbye": "再见，下次再聊~"}', "name": "handle_exit_intent"},
                    "type": "function", "index": 0,
                }],
                is_temporary=True,
            ))
            self.dialogue.put(Message(
                role="tool", tool_call_id=tc_id,
                content="退出意图已处理", is_temporary=True,
            ))
            self.dialogue.put(Message(
                role="assistant", content="再见，下次再聊~", is_temporary=True,
            ))

        self.logger.bind(tag=TAG).debug("已注入工具调用 few-shot 示例")

    def _initialize_tts(self):
        """初始化TTS"""
        tts = None
        if not self.need_bind:
            tts = initialize_tts(self.config)

        if tts is None:
            tts = DefaultTTS(self.config, delete_audio_file=True)

        return tts

    def _initialize_asr(self):
        """初始化ASR"""
        if (
                self._asr is not None
                and hasattr(self._asr, "interface_type")
                and self._asr.interface_type == InterfaceType.LOCAL
        ):
            # 如果公共ASR是本地服务，则直接返回
            # 因为本地一个实例ASR，可以被多个连接共享
            asr = self._asr
        else:
            # 如果公共ASR是远程服务，则初始化一个新实例
            # 因为远程ASR，涉及到websocket连接和接收线程，需要每个连接一个实例
            asr = initialize_asr(self.config)

        return asr


    async def _background_initialize(self):
        """在后台初始化配置和组件（完全不阻塞主循环）"""
        try:
            self.need_bind = False
            self.bind_completed_event.set()
            self.executor.submit(self._initialize_components)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"后台初始化失败: {e}")

    def _initialize_memory(self):
        if self.memory is None:
            return
        self.memory.init_memory(
            role_id=self.device_id,
            llm=self.llm,
            summary_memory=self.config.get("summaryMemory", None),
            save_to_file=True,
        )

    def _initialize_intent(self):
        if self.intent is None:
            return
        sel_intent = (self.config.get("selected_module") or {}).get("Intent") or "function_call"
        self.intent_type = ((self.config.get("Intent") or {}).get(sel_intent) or {}).get("type", "function_call")
        if self.intent_type == "function_call":
            self.load_function_plugin = True
        if self.intent_type == "nointent":
            return

        """加载统一工具处理器"""
        self.func_handler = UnifiedToolHandler(self)

        # 异步初始化工具处理器（在工作线程中等待完成，确保后续 few-shot 注入拿到完整工具表）
        if hasattr(self, "loop") and self.loop:
            init_fut = asyncio.run_coroutine_threadsafe(self.func_handler._initialize(), self.loop)
            if self.loop.is_running() and threading.current_thread() is not getattr(self, "_loop_thread", None):
                try:
                    init_fut.result(timeout=10)
                except Exception as e:
                    self.logger.bind(tag=TAG).warning(f"等待工具处理器初始化异常: {e}")

    def change_system_prompt(self, prompt):
        self.prompt = prompt
        # 更新系统prompt至上下文
        self.dialogue.update_system_message(self.prompt)

    def chat(self, query, depth=0, turn=None):
        # [xiaozhi-hub] 一轮结束时，被打断的回复在对话里换成播出去的部分（reply_progress）
        if depth > 0:
            return self._chat(query, depth)
        # [xiaozhi-hub] turn：开始这一轮时的代次（turn_gen）。排队等线程期间被打断了就不做
        if turn is None:
            turn = turn_gen.current(self)
        elif turn_gen.stale(self, turn):
            self.logger.bind(tag=TAG).info(f"这一轮还没开始就被打断了，不问模型: {query}")
            return None
        sentence_id = str(uuid.uuid4().hex)
        turn_gen.bind(self, sentence_id, turn)
        if depth == 0:
            self.sentence_id = sentence_id
        pinned_dlg = getattr(self, "dialogue", None)
        pinned_llm = getattr(self, "llm", None)
        try:
            return self._chat(query, depth, sentence_id, turn_dialogue=pinned_dlg, turn_llm=pinned_llm)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"chat 处理未捕获异常: {e}", exc_info=True)
            reply_progress.cut(self, 0, sentence_id=sentence_id)
            if not self._superseded(sentence_id):
                if self.tts is None and not self._is_closing():
                    try:
                        self.tts = self._initialize_tts()
                    except Exception:
                        pass
                if self.tts is not None:
                    if getattr(self, "_tts_first_sent_sid", None) != sentence_id:
                        self._tts_first_sent_sid = sentence_id
                        self.tts.tts_text_queue.put(
                            TTSMessageDTO(
                                sentence_id=sentence_id,
                                sentence_type=SentenceType.FIRST,
                                content_type=ContentType.ACTION,
                            )
                        )
                    self.tts.tts_text_queue.put(
                        TTSMessageDTO(
                            sentence_id=sentence_id,
                            sentence_type=SentenceType.MIDDLE,
                            content_type=ContentType.TEXT,
                            content_detail=get_system_error_response(self.config),
                        )
                    )
                    self.tts.tts_text_queue.put(
                        TTSMessageDTO(
                            sentence_id=sentence_id,
                            sentence_type=SentenceType.LAST,
                            content_type=ContentType.ACTION,
                        )
                    )
            return None
        finally:
            reply_progress.finish_turn(self, sentence_id)
            # [xiaozhi-hub] 这一轮写进 data/chat.db；截掉的旧对话攒够了就在后台总结成记忆
            if pinned_dlg is not None and pinned_dlg is not getattr(self, "dialogue", None):
                chat_history.sync(self, pinned_dlg)
            chat_history.sync(self)
            chat_history.maybe_summarize(self)

    def _put_reply(self, sentence_id, text):
        """[xiaozhi-hub] 把这一轮的回复写进对话；被打断后不写（由 reply_progress 写播出去的部分）。"""
        reply_progress.put(self, sentence_id, Message(role="assistant", content=text))

    def _superseded(self, sentence_id):
        """[xiaozhi-hub] 这一轮已被打断或已开始新的一轮：旧的一轮不能再往合成和对话里塞内容。"""
        return self.client_abort or self.sentence_id != sentence_id or turn_gen.stale(self, sentence_id=sentence_id)

    def _chat(self, query, depth=0, sentence_id=None, turn_dialogue=None, turn_llm=None):
        # 保存当前任务的sentence_id到局部变量，避免被新任务覆盖
        current_sentence_id = None
        if self.tts is None:
            self.tts = self._initialize_tts()
        active_dlg = turn_dialogue if turn_dialogue is not None else self.dialogue
        active_llm = turn_llm if turn_llm is not None else self.llm

        if query is not None:
            self.logger.bind(tag=TAG).info(f"大模型收到用户消息: {query}")

        # 为最顶层时新建会话ID和发送FIRST请求
        if depth == 0:
            self._active_turn_dlg = active_dlg
            self._active_turn_llm = active_llm
            current_sentence_id = sentence_id or str(uuid.uuid4().hex)
            self.sentence_id = current_sentence_id  # 更新共享属性
            # [xiaozhi-hub] 模型还没配置好：马上告诉用户，不去请求、也不写进对话
            not_ready = llm_router.not_ready_reply(self)
            if not_ready:
                self.logger.bind(tag=TAG).warning(f"模型还没配置好，不请求: {query}")
                for sentence_type, content_type, detail in (
                        (SentenceType.FIRST, ContentType.ACTION, None),
                        (SentenceType.MIDDLE, ContentType.TEXT, not_ready),
                        (SentenceType.LAST, ContentType.ACTION, None)):
                    self.tts.tts_text_queue.put(TTSMessageDTO(
                        sentence_id=current_sentence_id, sentence_type=sentence_type,
                        content_type=content_type, content_detail=detail))
                return None
            user_msg = Message(role="user", content=query)
            active_dlg.put(user_msg)
            reply_progress.note_turn(self, current_sentence_id, user_msg, dialogue=active_dlg)
            self._tts_first_sent_sid = current_sentence_id
            self.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=current_sentence_id,
                    sentence_type=SentenceType.FIRST,
                    content_type=ContentType.ACTION,
                )
            )
        else:
            # 递归调用时，使用当前的sentence_id
            # [xiaozhi-hub] 由调用方带进来：打断后 self.sentence_id 已经换掉，不能再拿它当这一轮的
            current_sentence_id = sentence_id or self.sentence_id

        # 设置最大递归深度，避免无限循环，可根据实际需求调整
        MAX_DEPTH = 5
        force_final_answer = False  # 标记是否强制最终回答
        max_depth_hint = None

        if depth >= MAX_DEPTH:
            self.logger.bind(tag=TAG).debug(
                f"已达到最大工具调用深度 {MAX_DEPTH}，将强制基于现有信息回答"
            )
            force_final_answer = True
            # 添加系统指令，要求 LLM 基于现有信息回答（构造完请求立即移除，不留在对话历史里）
            max_depth_hint = Message(
                role="user",
                content="[系统提示] 已达到最大工具调用次数限制，请你基于目前已经获取的所有信息，直接给出最终答案。不要再尝试调用任何工具。",
            )
            active_dlg.put(max_depth_hint)

        # Define intent functions
        functions = None
        # 达到最大深度时，禁用工具调用，强制 LLM 直接回答
        if (
                self.intent_type == "function_call"
                and getattr(self, "func_handler", None) is not None
                and not force_final_answer
        ):
            functions = list(self.func_handler.get_functions())
            # 仅在第一层调用时注入 direct_answer 虚拟工具
            # 递归调用（depth>0）不注入，避免模型在生成文本回复时再次调 direct_answer 导致循环
            if functions is not None and depth == 0:
                functions.append(DIRECT_ANSWER_TOOL)

        response_message = []

        try:
            # 使用带记忆的对话
            memory_str = None
            # 仅当query非空（代表用户询问）时查询记忆
            if self.memory is not None and query:
                future = asyncio.run_coroutine_threadsafe(
                    self.memory.query_memory(query), self.loop
                )
                memory_str = future.result()
            # [xiaozhi-hub] 没配记忆模块（nomem）时，用聊天记录里被截掉部分的总结
            if not memory_str:
                memory_str = chat_history.memory_for(self) or memory_str

            # 仅在该说话人首次出现时把身份注入 system，之后靠对话历史首轮保留，
            # 避免每轮在 system 重复出现名字诱导模型反复称呼
            speaker_for_system = None
            cs = (self.current_speaker or "").strip()
            if cs and cs != "未知说话人" and cs not in self.system_introduced_speakers:
                self.system_introduced_speakers.add(cs)
                speaker_for_system = cs

            llm_messages = active_dlg.get_llm_dialogue_with_memory(
                memory_str, {}, speaker_for_system
            )
            if self.intent_type == "function_call" and functions:
                # 使用支持functions的streaming接口
                llm_responses = active_llm.response_with_functions(
                    self.session_id,
                    llm_messages,
                    functions=functions,
                )
            else:
                llm_responses = active_llm.response(
                    self.session_id,
                    llm_messages,
                )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"LLM 处理出错 {query}: {e}")
            if depth == 0:
                reply_progress.cut(self, 0, sentence_id=current_sentence_id)
                if not self._superseded(current_sentence_id):
                    self.tts.tts_text_queue.put(
                        TTSMessageDTO(
                            sentence_id=current_sentence_id,
                            sentence_type=SentenceType.LAST,
                            content_type=ContentType.ACTION,
                        )
                    )
            elif not self._superseded(current_sentence_id):
                err_txt = get_system_error_response(self.config)
                self.tts.tts_text_queue.put(
                    TTSMessageDTO(
                        sentence_id=current_sentence_id,
                        sentence_type=SentenceType.MIDDLE,
                        content_type=ContentType.TEXT,
                        content_detail=err_txt,
                    )
                )
                self._put_reply(current_sentence_id, err_txt)
            return None
        finally:
            if max_depth_hint is not None and max_depth_hint in active_dlg.dialogue:
                active_dlg.dialogue.remove(max_depth_hint)

        # 处理流式响应
        tool_call_flag = False
        # 支持多个并行工具调用 - 使用列表存储
        tool_calls_list = []  # 格式: [{"id": "", "name": "", "arguments": ""}]
        content_arguments = ""
        pending_tool_prefix = ""
        # [xiaozhi-hub] 模型漏进回复文字的工具调用标记（如 DeepSeek 的 DSML），送合成和写对话前去掉
        markup_filter = ToolMarkupFilter()
        emotion_flag = True
        try:
            try:
                for response in llm_responses:
                    if self._superseded(current_sentence_id):
                        break
                    if self.intent_type == "function_call" and functions:
                        if isinstance(response, dict):
                            content = response.get("content")
                            tools_call = response.get("tool_calls")
                        else:
                            content, tools_call = response
                        if content is not None and len(content) > 0:
                            content_arguments += content

                        if tools_call is not None and len(tools_call) > 0:
                            tool_call_flag = True
                            pending_tool_prefix = ""
                            self._merge_tool_calls(tool_calls_list, tools_call)
                        elif not tool_call_flag and not response_message:
                            stripped_args = content_arguments.lstrip()
                            if stripped_args.startswith("<tool_call>"):
                                tool_call_flag = True
                                pending_tool_prefix = ""
                                content = ""
                            elif not stripped_args or "<tool_call>".startswith(stripped_args):
                                if content:
                                    pending_tool_prefix += content
                                    content = ""
                            elif pending_tool_prefix:
                                content = pending_tool_prefix + (content or "")
                                pending_tool_prefix = ""

                        # 流式提取 direct_answer 的 response 参数，实时送 TTS
                        # 使用安全缓冲区，防止 JSON 闭合符号泄漏到 TTS
                        _DA_STREAM_BUFFER = 5
                        for tc in tool_calls_list:
                            if tc["name"] == "direct_answer" and tc.get("arguments"):
                                da_text = self._extract_direct_answer_response(tc["arguments"])
                                if emotion_flag and da_text and da_text.strip():
                                    if (
                                        (self.features or {}).get("emoji", True)
                                        and getattr(self, "loop", None)
                                        and self.loop.is_running()
                                    ):
                                        asyncio.run_coroutine_threadsafe(
                                            textUtils.get_emotion(self, da_text),
                                            self.loop,
                                        )
                                    emotion_flag = False
                                sent_len = tc.get("_da_sent", 0)
                                if da_text and len(da_text) > sent_len:
                                    tail_w = da_text[-5:]
                                    hold = (
                                        0
                                        if (
                                            da_text[-1] in "，。！？；：,!?;"
                                            and not any(c in '"\'}）\\' for c in tail_w)
                                        )
                                        else _DA_STREAM_BUFFER
                                    )
                                    safe_end = max(sent_len, len(da_text) - hold)
                                    if safe_end > sent_len:
                                        new_part = da_text[sent_len:safe_end]
                                        if new_part:
                                            tc["_da_sent"] = safe_end
                                            # [xiaozhi-hub] 变形的 DSML 标记接口认不出，会留在 response 参数里
                                            new_part = tc.setdefault("_da_filter", ToolMarkupFilter()).feed(new_part)
                                        if new_part:
                                            self.tts.tts_text_queue.put(
                                                TTSMessageDTO(
                                                    sentence_id=current_sentence_id,
                                                    sentence_type=SentenceType.MIDDLE,
                                                    content_type=ContentType.TEXT,
                                                    content_detail=new_part,
                                                )
                                            )
                    else:
                        content = response

                    if content:
                        content = markup_filter.feed(content)

                    # 在llm回复中获取情绪表情，一轮对话只在开头获取一次
                    if emotion_flag and content is not None and content.strip():
                        if (
                            (self.features or {}).get("emoji", True)
                            and getattr(self, "loop", None)
                            and self.loop.is_running()
                        ):
                            asyncio.run_coroutine_threadsafe(
                                textUtils.get_emotion(self, content),
                                self.loop,
                            )
                        emotion_flag = False

                    if content is not None and len(content) > 0:
                        if not tool_call_flag:
                            response_message.append(content)
                            self.tts.tts_text_queue.put(
                                TTSMessageDTO(
                                    sentence_id=current_sentence_id,
                                    sentence_type=SentenceType.MIDDLE,
                                    content_type=ContentType.TEXT,
                                    content_detail=content,
                                )
                            )
            finally:
                close_gen = getattr(llm_responses, "close", None)
                if callable(close_gen):
                    try:
                        close_gen()
                    except Exception:
                        pass
            # [xiaozhi-hub] 过滤器为了等标记收全扣住的末尾
            tail_pref = markup_filter.feed(pending_tool_prefix) if (pending_tool_prefix and not tool_call_flag) else ""
            pending_tool_prefix = ""
            content = tail_pref + markup_filter.flush()
            if content and not tool_call_flag and not self._superseded(current_sentence_id):
                response_message.append(content)
                self.tts.tts_text_queue.put(
                    TTSMessageDTO(
                        sentence_id=current_sentence_id,
                        sentence_type=SentenceType.MIDDLE,
                        content_type=ContentType.TEXT,
                        content_detail=content,
                    )
                )
            if markup_filter.removed:
                self.logger.bind(tag=TAG).warning(
                    f"回复文字里去掉了 {markup_filter.removed} 个工具调用标记"
                )
        except Exception as e:
            if self._superseded(current_sentence_id):
                # [xiaozhi-hub] 打断时主动断开了模型的流式请求（llm.abort），读流报错是预期的，不播报错误
                self.logger.bind(tag=TAG).info(f"这一轮已被打断，模型请求已断开: {type(e).__name__}")
                return None
            self.logger.bind(tag=TAG).error(f"LLM stream processing error: {e}")
            err_txt = get_system_error_response(self.config)
            if depth == 0 and not "".join(response_message).strip() and not tool_call_flag:
                reply_progress.cut(self, 0, sentence_id=current_sentence_id)
            elif depth > 0:
                self._put_reply(current_sentence_id, "".join(response_message) + err_txt)
            self.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=current_sentence_id,
                    sentence_type=SentenceType.MIDDLE,
                    content_type=ContentType.TEXT,
                    content_detail=err_txt,
                )
            )
            if depth == 0:
                self.tts.tts_text_queue.put(
                    TTSMessageDTO(
                        sentence_id=current_sentence_id,
                        sentence_type=SentenceType.LAST,
                        content_type=ContentType.ACTION,
                    )
                )
            return
        # [xiaozhi-hub] 被打断或已开始新的一轮：模型说到一半的工具调用不再执行（参数可能都没收全），也不写对话
        if self._superseded(current_sentence_id):
            self.logger.bind(tag=TAG).info(
                "这一轮已被打断，丢掉模型后面的输出" + (f"，不执行工具 {[tc['name'] for tc in tool_calls_list]}" if tool_call_flag else ""))
            return None
        # 处理function call
        if tool_call_flag:
            bHasError = False
            # 处理基于文本的工具调用格式
            if len(tool_calls_list) == 0 and content_arguments:
                a = extract_json_from_string(content_arguments)
                if a is not None:
                    try:
                        content_arguments_json = json.loads(a)
                        raw_args = content_arguments_json.get("arguments", {})
                        arg_str = (
                            raw_args
                            if isinstance(raw_args, str)
                            else json.dumps(raw_args if raw_args is not None else {}, ensure_ascii=False)
                        )
                        tool_calls_list.append(
                            {
                                "id": str(uuid.uuid4().hex),
                                "name": content_arguments_json["name"],
                                "arguments": arg_str,
                            }
                        )
                    except Exception as e:
                        bHasError = True
                else:
                    bHasError = True
                if bHasError:
                    self.logger.bind(tag=TAG).error(
                        f"function call error: {content_arguments}"
                    )
                    if depth == 0 and not "".join(response_message).strip():
                        reply_progress.cut(self, 0, sentence_id=current_sentence_id)

            if not bHasError and len(tool_calls_list) > 0:
                # 处理 direct_answer 虚拟工具
                direct_answer_calls = [tc for tc in tool_calls_list if tc["name"] == "direct_answer"]
                real_tool_calls = [tc for tc in tool_calls_list if tc["name"] != "direct_answer"]

                if direct_answer_calls:
                    self.logger.bind(tag=TAG).debug(
                        f"模型选择 direct_answer，流式已播报，写入对话历史"
                    )
                    any_da_stored = False
                    for tc in direct_answer_calls:
                        da_response = self._extract_direct_answer_response(tc.get("arguments", "{}"))
                        if da_response:
                            # 刷新流式缓冲区中未发送的部分（保留已发送前缀后的前导空格）
                            sent_len = tc.get("_da_sent", 0)
                            remaining = da_response[sent_len:]
                            if remaining:
                                cleaned_tail = self._clean_response_garbage(remaining)
                                remaining = (
                                    (" " + cleaned_tail if remaining[0] == " " and cleaned_tail else cleaned_tail)
                                    if sent_len > 0
                                    else cleaned_tail
                                )
                            da_filter = tc.get("_da_filter") or ToolMarkupFilter()
                            remaining = da_filter.feed(remaining) + da_filter.flush()
                            if remaining:
                                self.tts.tts_text_queue.put(
                                    TTSMessageDTO(
                                        sentence_id=current_sentence_id,
                                        sentence_type=SentenceType.MIDDLE,
                                        content_type=ContentType.TEXT,
                                        content_detail=remaining,
                                    )
                                )
                            if da_filter.removed:
                                self.logger.bind(tag=TAG).warning(
                                    f"direct_answer 回复里去掉了 {da_filter.removed} 个工具调用标记"
                                )
                            # 写入对话历史
                            da_response = self._clean_response_garbage(strip_tool_markup(da_response))
                            if da_response and da_response.strip():
                                self.tts.store_tts_text(current_sentence_id, da_response)
                                self._put_reply(current_sentence_id, da_response)
                                any_da_stored = True

                    if not real_tool_calls:
                        if depth == 0:
                            if not any_da_stored:
                                reply_progress.cut(self, 0, sentence_id=current_sentence_id)
                            if not self._superseded(current_sentence_id):
                                self.tts.tts_text_queue.put(
                                    TTSMessageDTO(
                                        sentence_id=current_sentence_id,
                                        sentence_type=SentenceType.LAST,
                                        content_type=ContentType.ACTION,
                                    )
                                )
                        return

                    tool_calls_list = real_tool_calls

            tool_calls_list = [tc for tc in tool_calls_list if isinstance(tc, dict) and tc.get("name")]
            if not bHasError and len(tool_calls_list) > 0:
                self.logger.bind(tag=TAG).debug(
                    f"检测到 {len(tool_calls_list)} 个工具调用"
                )

                # LLM 流式阶段已播报过的文本
                streamed_text = ""
                if len(response_message) > 0:
                    streamed_text = "".join(response_message)
                    self.tts.store_tts_text(current_sentence_id, streamed_text)
                    self._put_reply(current_sentence_id, streamed_text)
                response_message.clear()

                # 收集所有工具调用的 Future（提前标记本轮已进入工具阶段，防止执行中途被打断时整轮提问与结果被误删）
                reply_progress.note_tool_start(self, current_sentence_id)
                futures_with_data = []
                for tool_call_data in tool_calls_list:
                    self.logger.bind(tag=TAG).debug(
                        f"function_name={tool_call_data['name']}, function_id={tool_call_data['id']}, function_arguments={tool_call_data['arguments']}"
                    )

                    # 使用公共方法上报工具调用（防御截断/非法 JSON 参数导致 _chat 漏发 SentenceType.LAST）
                    try:
                        parsed_args = json.loads(tool_call_data.get("arguments") or "{}")
                        if isinstance(parsed_args, dict):
                            tool_input = parsed_args
                        else:
                            tool_input = {}
                            tool_call_data["arguments"] = "{}"
                    except Exception:
                        tool_input = {}
                        tool_call_data["arguments"] = "{}"

                    future = asyncio.run_coroutine_threadsafe(
                        self.func_handler.handle_llm_function_call(
                            self, tool_call_data
                        ),
                        self.loop,
                    )
                    futures_with_data.append((future, tool_call_data, tool_input))

                tool_call_timeout = int(self.config.get("tool_call_timeout", 30))
                tool_results = []

                for future, tool_call_data, tool_input in futures_with_data:
                    try:
                        result = future.result(timeout=tool_call_timeout)
                        tool_results.append((result, tool_call_data))

                    except Exception as e:
                        future.cancel()
                        self.logger.bind(tag=TAG).error(
                            f"工具调用超时或异常: {tool_call_data['name']}, 错误: {e}"
                        )
                        tool_results.append((
                            ActionResponse(action=Action.ERROR, result="哎呀，网络遇到点问题，请稍后再试下！"),
                            tool_call_data
                        ))

                # 统一处理工具调用结果
                if tool_results:
                    self._handle_function_result(tool_results, depth=depth, streamed_text=streamed_text,
                                                 sentence_id=current_sentence_id)

        # 存储对话内容
        if len(response_message) > 0:
            text_buff = "".join(response_message)
            if text_buff.strip():
                self.tts.store_tts_text(current_sentence_id, text_buff)
                self._put_reply(current_sentence_id, text_buff)
            elif not tool_call_flag:
                if depth == 0:
                    reply_progress.cut(self, 0, sentence_id=current_sentence_id)
                elif not self._superseded(current_sentence_id):
                    self._put_reply(current_sentence_id, "好的。")
        elif not tool_call_flag:
            if depth == 0:
                reply_progress.cut(self, 0, sentence_id=current_sentence_id)
            elif not self._superseded(current_sentence_id):
                self._put_reply(current_sentence_id, "好的。")

        if depth == 0:
            if not self._superseded(current_sentence_id):
                self.tts.tts_text_queue.put(
                    TTSMessageDTO(
                        sentence_id=current_sentence_id,
                        sentence_type=SentenceType.LAST,
                        content_type=ContentType.ACTION,
                    )
                )
            # 使用lambda延迟计算，只有在DEBUG级别时才执行get_llm_dialogue()
            self.logger.bind(tag=TAG).debug(
                lambda: json.dumps(
                    self.dialogue.get_llm_dialogue(), indent=4, ensure_ascii=False
                )
            )

        return True

    def _handle_function_result(self, tool_results, depth, streamed_text="", sentence_id=None):
        sentence_id = sentence_id or self.sentence_id
        need_llm_tools = []
        record_tools = []
        handled_reply = False

        for result, tool_call_data in tool_results:
            if result is None:
                continue
            if result.action in [
                Action.RESPONSE,
                Action.NOTFOUND,
                Action.ERROR,
            ]:
                text = result.response if result.response else result.result
                if text:
                    handled_reply = True
                    if streamed_text and text in streamed_text:
                        self.logger.bind(tag=TAG).debug(
                            f"Skipping duplicate TTS for tool {tool_call_data['name']}, already streamed"
                        )
                    elif not self._superseded(sentence_id):
                        self.tts.tts_one_sentence(self, ContentType.TEXT, content_detail=text)
                        self.tts.store_tts_text(sentence_id or self.sentence_id, text)
                    self._put_reply(sentence_id, text)
            elif result.action == Action.REQLLM:
                need_llm_tools.append((result, tool_call_data))
            elif result.action == Action.RECORD:
                record_tools.append((result, tool_call_data))
            else:
                pass

        if not need_llm_tools and not record_tools and not handled_reply and not (streamed_text and streamed_text.strip()):
            if depth == 0:
                reply_progress.drop_empty_turn(self, sentence_id)
            elif not self._superseded(sentence_id):
                self._put_reply(sentence_id, "好的。")

        # Action.RECORD：写入完整工具调用链（assistant(tool_calls) → tool(result) → assistant(response)）
        # 模型从历史中学到工具调用模式，不额外调用LLM
        if record_tools:
            for _, tool_call_data in record_tools:
                if not tool_call_data.get("id"):
                    tool_call_data["id"] = str(uuid.uuid4())
            # 构造 assistant 消息（含 tool_calls），记录"模型调用了哪些工具"
            all_tool_calls = [
                {
                    "id": tool_call_data["id"],
                    "function": {
                        "arguments": (
                            "{}"
                            if tool_call_data["arguments"] == ""
                            else tool_call_data["arguments"]
                        ),
                        "name": tool_call_data["name"],
                    },
                    "type": "function",
                    "index": idx,
                }
                for idx, (_, tool_call_data) in enumerate(record_tools)
            ]
            reply_progress.put(self, sentence_id, Message(role="assistant", tool_calls=all_tool_calls), reply=False)

            # 写入每条工具的执行结果，记录"工具返回了什么"
            for result, tool_call_data in record_tools:
                text = result.result or ""
                reply_progress.put(
                    self, sentence_id, reply=False, message=Message(
                        role="tool",
                        tool_call_id=tool_call_data["id"],
                        content=text,
                    )
                )

            # 用固定文本作为最终回复，补全标准三段式，保证下一条消息是 user 而非接 tool
            response_parts = []
            for result, _ in record_tools:
                resp = result.response or result.result
                if resp:
                    response_parts.append(resp)
            if response_parts:
                self._put_reply(sentence_id, "，".join(response_parts))

        if need_llm_tools:
            for _, tool_call_data in need_llm_tools:
                if not tool_call_data.get("id"):
                    tool_call_data["id"] = str(uuid.uuid4())
            all_tool_calls = [
                {
                    "id": tool_call_data["id"],
                    "function": {
                        "arguments": (
                            "{}"
                            if tool_call_data["arguments"] == ""
                            else tool_call_data["arguments"]
                        ),
                        "name": tool_call_data["name"],
                    },
                    "type": "function",
                    "index": idx,
                }
                for idx, (_, tool_call_data) in enumerate(need_llm_tools)
            ]
            reply_progress.put(self, sentence_id, Message(role="assistant", tool_calls=all_tool_calls), reply=False)

            for result, tool_call_data in need_llm_tools:
                text = result.result
                content = str(text) if (text is not None and len(str(text)) > 0) else "(无返回内容)"
                reply_progress.put(
                    self, sentence_id, reply=False, message=Message(
                        role="tool",
                        tool_call_id=tool_call_data["id"],
                        content=content,
                    )
                )

            # [xiaozhi-hub] 工具执行期间被打断：结果照样留在对话里，但不再为这一轮去问模型
            if not self._superseded(sentence_id):
                self._chat(None, depth=depth + 1, sentence_id=sentence_id, turn_dialogue=getattr(self, "_active_turn_dlg", None), turn_llm=getattr(self, "_active_turn_llm", None))

    def clearSpeakStatus(self):
        self.client_is_speaking = False
        self.logger.bind(tag=TAG).debug(f"清除服务端讲话状态")

    async def close(self, ws=None):
        """资源清理方法"""
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            if getattr(self, "mcp_client", None) and hasattr(self.mcp_client, "cancel_all_pending"):
                self.mcp_client.cancel_all_pending(ConnectionError("连接已关闭"))
            # [xiaozhi-hub] 从管理后台注销；通知这个连接用过的每个 LLM 连接关闭（claude_cli 结束对应的 Claude Code 进程）
            from core.admin import connections as hub_connections
            from core.agent import llm_router
            hub_connections.unregister(self)
            llm_router.on_conn_close(self)
            chat_history.sync_all(self)

            # 清理 VAD 连接资源
            if (
                    hasattr(self, "vad")
                    and self.vad
                    and hasattr(self.vad, "release_conn_resources")
            ):
                self.vad.release_conn_resources(self)

            # 清理opus解码器
            for _dec_attr in ("_connection_opus_decoder", "_send_opus_decoder"):
                if hasattr(self, _dec_attr):
                    try:
                        delattr(self, _dec_attr)
                    except Exception:
                        pass

            # 清理音频缓冲区
            if hasattr(self, "audio_buffer"):
                self.audio_buffer.clear()

            # 取消超时任务（避免 timeout_task 自等待 RuntimeError）
            if self.timeout_task and not self.timeout_task.done() and self.timeout_task is not asyncio.current_task():
                self.timeout_task.cancel()
                try:
                    await self.timeout_task
                except asyncio.CancelledError:
                    pass
                self.timeout_task = None

            # 取消AEC缓存清理任务
            if hasattr(self, "_aec_cache_cleanup_task") and self._aec_cache_cleanup_task and not self._aec_cache_cleanup_task.done():
                self._aec_cache_cleanup_task.cancel()
                try:
                    await self._aec_cache_cleanup_task
                except asyncio.CancelledError:
                    pass
                self._aec_cache_cleanup_task = None

            # 清理AEC缓存
            if hasattr(self, "aec_audio_cache"):
                self.aec_audio_cache.clear()
                self.aec_audio_cache_time.clear()

            # 清理工具处理器资源
            if hasattr(self, "func_handler") and self.func_handler:
                try:
                    await self.func_handler.cleanup()
                except Exception as cleanup_error:
                    self.logger.bind(tag=TAG).error(
                        f"清理工具处理器时出错: {cleanup_error}"
                    )

            # 触发停止事件
            if self.stop_event:
                self.stop_event.set()

            # 清空任务队列
            self.clear_queues()

            # 关闭WebSocket连接
            try:
                if ws:
                    # 安全地检查WebSocket状态并关闭
                    try:
                        if hasattr(ws, "closed") and not ws.closed:
                            await ws.close()
                        elif hasattr(ws, "state") and ws.state.name != "CLOSED":
                            await ws.close()
                        else:
                            # 如果没有closed属性，直接尝试关闭
                            await ws.close()
                    except Exception:
                        # 如果关闭失败，忽略错误
                        pass
                elif self.websocket:
                    try:
                        if (
                                hasattr(self.websocket, "closed")
                                and not self.websocket.closed
                        ):
                            await self.websocket.close()
                        elif (
                                hasattr(self.websocket, "state")
                                and self.websocket.state.name != "CLOSED"
                        ):
                            await self.websocket.close()
                        else:
                            # 如果没有closed属性，直接尝试关闭
                            await self.websocket.close()
                    except Exception:
                        # 如果关闭失败，忽略错误
                        pass
            except Exception as ws_error:
                self.logger.bind(tag=TAG).error(f"关闭WebSocket连接时出错: {ws_error}")

            if self.tts:
                try:
                    await self.tts.close()
                except Exception as tts_error:
                    self.logger.bind(tag=TAG).error(f"关闭TTS时出错: {tts_error}")
            if self.asr:
                try:
                    await self.asr.close()
                except Exception as asr_error:
                    self.logger.bind(tag=TAG).error(f"关闭ASR时出错: {asr_error}")

            # 最后关闭线程池（避免阻塞）
            if self.executor:
                try:
                    self.executor.shutdown(wait=False)
                except Exception as executor_error:
                    self.logger.bind(tag=TAG).error(
                        f"关闭线程池时出错: {executor_error}"
                    )
                self.executor = None
            self.logger.bind(tag=TAG).info("连接资源已释放")
            _trim_conn_heap()
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"关闭连接时出错: {e}")
        finally:
            # 确保停止事件被设置
            if self.stop_event:
                self.stop_event.set()

    def clear_queues(self):
        """清空所有任务队列"""
        if self.tts:
            self.logger.bind(tag=TAG).debug(
                f"开始清理: TTS队列大小={self.tts.tts_text_queue.qsize()}, 音频队列大小={self.tts.tts_audio_queue.qsize()}"
            )

            # 使用非阻塞方式清空队列
            for q in [
                self.tts.tts_text_queue,
                self.tts.tts_audio_queue,
            ]:
                if not q:
                    continue
                while True:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break

            # 重置音频流控器（取消后台任务并清空队列）
            if hasattr(self, "audio_rate_controller") and self.audio_rate_controller:
                self.audio_rate_controller.reset()
                self.logger.bind(tag=TAG).debug("已重置音频流控器")

            self.logger.bind(tag=TAG).debug(
                f"清理结束: TTS队列大小={self.tts.tts_text_queue.qsize()}, 音频队列大小={self.tts.tts_audio_queue.qsize()}"
            )

    def reset_audio_states(self):
        """
        重置所有音频相关状态(VAD + ASR)
        """
        # Reset VAD states
        self.client_audio_buffer.clear()
        self.client_have_voice = False
        self.client_voice_stop = False
        self.client_voice_window.clear()
        self.last_is_voice = False
        self.vad_last_voice_time = 0.0

        # Clear ASR buffers
        self.asr_audio.clear()
        self.asr_voice_end = 0
        self.asr_segment_start = 0
        self.asr_segments = []

        if getattr(self, "_vad_state", None) is not None:
            self._vad_state.fill(0)
        if getattr(self, "_vad_context", None) is not None:
            self._vad_context.fill(0)

        self.logger.bind(tag=TAG).debug("All audio states reset.")

    def chat_and_close(self, text):
        """Chat with the user and then close the connection"""
        try:
            # Use the existing chat method
            self.chat(text)

            # After chat is complete, close the connection
            self.close_after_chat = True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Chat and close error: {str(e)}")

    async def _check_timeout(self):
        """检查连接超时"""
        try:
            while not self.stop_event.is_set():
                last_activity_time = self.last_activity_time
                if self.need_bind:
                    last_activity_time = self.first_activity_time

                # 检查是否超时（只有在时间戳已初始化的情况下）
                if last_activity_time > 0.0:
                    current_time = time.time() * 1000
                    if current_time - last_activity_time > self.timeout_seconds * 1000:
                        if not self.stop_event.is_set():
                            self.logger.bind(tag=TAG).info("连接超时，准备关闭")
                            # 设置停止事件，防止重复处理
                            self.stop_event.set()
                            # 使用 try-except 包装关闭操作，确保不会因为异常而阻塞
                            try:
                                await self.close(self.websocket)
                            except Exception as close_error:
                                self.logger.bind(tag=TAG).error(
                                    f"超时关闭连接时出错: {close_error}"
                                )
                        break
                # 每10秒检查一次，避免过于频繁
                await asyncio.sleep(10)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"超时检查任务出错: {e}")
        finally:
            self.logger.bind(tag=TAG).info("超时检查任务已退出")

    async def _check_aec_cache_expiry(self):
        """定期清理过期的AEC缓存"""
        try:
            while not self.stop_event.is_set():
                if hasattr(self, "aec_audio_cache") and self.aec_audio_cache:
                    current_time = time.time()
                    expired_keys = [
                        ts for ts, cache_time in list(self.aec_audio_cache_time.items())
                        if current_time - cache_time > 120  # 2分钟过期
                    ]
                    for ts in expired_keys:
                        self.aec_audio_cache.pop(ts, None)
                        self.aec_audio_cache_time.pop(ts, None)
                    if expired_keys:
                        self.logger.bind(tag=TAG).debug(f"[AEC] 清理过期缓存 {len(expired_keys)} 条")
                # 每30秒检查一次
                await asyncio.sleep(30)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"AEC缓存清理任务出错: {e}")

    _DA_INC_ESC_RE = re.compile(r'(\\+)(?:u[0-9a-fA-F]{0,3})?$')
    _DA_HS_ESC_RE = re.compile(r'(\\+)u[dD][89abAB][0-9a-fA-F]{2}$')
    _DA_UNESC_RE = re.compile(
        r'\\u([dD][89abAB][0-9a-fA-F]{2})\\u([dD][c-fC-F][0-9a-fA-F]{2})|\\(u[0-9a-fA-F]{4}|["\\/bfnrt])'
    )
    _DA_ESC_MAP = {'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}

    @staticmethod
    def _extract_direct_answer_response(arguments_str):
        """从 direct_answer 的参数中提取 response 值。
        优先使用 json.loads 标准解析，流式阶段 fallback 到字符串提取。
        """
        if not arguments_str:
            return ""
        # 仅在 JSON 已闭合时尝试 json.loads，避免流式每个 token 抛 JSONDecodeError
        if arguments_str.rstrip().endswith("}"):
            try:
                data = json.loads(arguments_str)
                if isinstance(data, dict) and "response" in data:
                    return "" if data["response"] is None else str(data["response"])
            except (json.JSONDecodeError, TypeError):
                pass
        # Fallback：流式阶段 JSON 可能不完整，使用字符串提取
        marker = '"response": "'
        idx = arguments_str.find(marker)
        if idx < 0:
            marker = '"response":"'
            idx = arguments_str.find(marker)
        if idx < 0:
            return ""
        start = idx + len(marker)
        raw = arguments_str[start:]
        # 去掉末尾未转义的 JSON 闭合符号（如果已完整）及流式截断的不完整转义序列
        if raw.endswith('"}') and (len(raw[:-2]) - len(raw[:-2].rstrip('\\'))) % 2 == 0:
            raw = raw[:-2]
        elif raw.endswith('"') and (len(raw[:-1]) - len(raw[:-1].rstrip('\\'))) % 2 == 0:
            raw = raw[:-1]
        m_inc = ConnectionHandler._DA_INC_ESC_RE.search(raw)
        if m_inc and len(m_inc.group(1)) % 2 == 1:
            raw = raw[:m_inc.start() + len(m_inc.group(1)) - 1]
        m_hs = ConnectionHandler._DA_HS_ESC_RE.search(raw)
        if m_hs and len(m_hs.group(1)) % 2 == 1:
            raw = raw[:m_hs.start() + len(m_hs.group(1)) - 1]
        if '\\' not in raw:
            return raw

        def _decode_esc(m):
            if m.group(1):
                return chr(0x10000 + ((int(m.group(1), 16) - 0xD800) << 10) + (int(m.group(2), 16) - 0xDC00))
            g3 = m.group(3)
            if g3.startswith('u'):
                cp = int(g3[1:], 16)
                return '\ufffd' if 0xD800 <= cp <= 0xDFFF else chr(cp)
            return ConnectionHandler._DA_ESC_MAP[g3]

        return ConnectionHandler._DA_UNESC_RE.sub(_decode_esc, raw)

    @staticmethod
    def _clean_response_garbage(text):
        """清理 response 中可能泄漏的 JSON 闭合符号。
        模型有时会在 response 内容中生成 JSON 闭合字符（如 ）"}} 或 '})，
        这些不是故事内容的一部分，需要去除。
        """
        if not text:
            return text
        # 清理独立一行的 JSON 闭合垃圾（如 ）"}}  '}}  "}}  }}  } ）
        _garbage_chars = frozenset('")\'}）')
        lines = text.split('\n')
        cleaned = []
        for line in lines:
            stripped = line.strip()
            if stripped and len(stripped) <= 8 and all(c in _garbage_chars for c in stripped):
                continue
            cleaned.append(line)
        result = '\n'.join(cleaned)
        # 清理末尾残留的 JSON 闭合符号
        result = re.sub(r'["\'}\]]+$', '', result.rstrip()).rstrip()
        return result

    def _merge_tool_calls(self, tool_calls_list, tools_call):
        """合并工具调用列表

        Args:
            tool_calls_list: 已收集的工具调用列表
            tools_call: 新的工具调用
        """
        for tool_call in tools_call:
            if isinstance(tool_call, dict):
                fn = tool_call.get("function") or {}
                fn_name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
                fn_args = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", None)
                tool_index = tool_call.get("index")
                tc_id = tool_call.get("id")
            else:
                fn = getattr(tool_call, "function", None)
                fn_name = getattr(fn, "name", None) if fn is not None else None
                fn_args = getattr(fn, "arguments", None) if fn is not None else None
                tool_index = getattr(tool_call, "index", None)
                tc_id = getattr(tool_call, "id", None)
            if tool_index is None:
                matched_idx = next(
                    (i for i, existing in enumerate(tool_calls_list) if tc_id and existing.get("id") == tc_id),
                    None,
                )
                if matched_idx is not None:
                    tool_index = matched_idx
                elif fn_name:
                    # 有 function_name，说明是新的工具调用
                    tool_index = len(tool_calls_list)
                else:
                    tool_index = len(tool_calls_list) - 1 if tool_calls_list else 0

            try:
                tool_index = max(0, min(int(tool_index), 15))
            except (TypeError, ValueError):
                tool_index = min(len(tool_calls_list), 15)
            # 确保列表有足够的位置（兼容流式稀疏/乱序 index）
            while len(tool_calls_list) <= tool_index:
                tool_calls_list.append({"id": "", "name": "", "arguments": ""})

            # 更新工具调用信息
            if tc_id:
                tool_calls_list[tool_index]["id"] = tc_id
            if fn_name:
                tool_calls_list[tool_index]["name"] = fn_name
            if fn_args:
                tool_calls_list[tool_index]["arguments"] += fn_args
