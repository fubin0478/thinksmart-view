import time
import json
import uuid
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.agent import clear_command
from core.agent.call_names import handle_call_name
from core.handle.abortHandle import handleAbortMessage
from core.handle.intentHandler import handle_user_intent, speak_txt
from core.utils import turn_gen
from core.handle.sendAudioHandle import send_stt_message, send_tts_message

TAG = __name__

# 唤醒/文字提问后忽略音频的时长（秒），见 mark_just_woken_up
JUST_WOKEN_UP_SECONDS = 2


def mark_just_woken_up(conn: "ConnectionHandler"):
    """唤醒后短暂忽略收到的音频（防问候语回声），从现在起 JUST_WOKEN_UP_SECONDS 秒内有效。"""
    conn.just_woken_up = True
    conn.just_woken_up_at = time.monotonic()


async def handleAudioMessage(conn: "ConnectionHandler", pcm_frame):
    # 如果设备刚刚被唤醒，短暂忽略VAD检测。
    # 窗口从打上标记时算起（JUST_WOKEN_UP_SECONDS），不是从第一帧音频算起：
    # 文字提问后设备可能很久不送音频，旧写法会让标记一直留着，
    # 下次语音唤醒时头 2 秒被扔掉，第一句识别为空。
    if getattr(conn, "just_woken_up", False):
        if (
            time.monotonic() - getattr(conn, "just_woken_up_at", 0.0)
            < JUST_WOKEN_UP_SECONDS
        ):
            return
        conn.just_woken_up = False
    # 当前片段是否有人说话
    have_voice = conn.vad.is_vad(conn, pcm_frame)
    # 服务端AEC功能需要实时触发打断
    if conn.client_aec and have_voice:
        if conn.client_is_speaking and conn.client_listen_mode != "manual":
            await handleAbortMessage(conn)
    # 设备长时间空闲检测，用于say goodbye
    await no_voice_close_connect(conn, have_voice)
    # 接收音频
    await conn.asr.receive_audio(conn, pcm_frame, have_voice)


async def startToChat(conn: "ConnectionHandler", text, gen=None):
    """gen：这句话说完时的轮次代次（turn_gen）；之后设备打断过就不再回答它。"""
    if gen is not None and turn_gen.stale(conn, gen):
        conn.logger.bind(tag=TAG).info(f"这句话说完后被打断过，不再回答: {text}")
        return
    # 检查输入是否是JSON格式（包含说话人信息）
    speaker_name = None
    actual_text = text

    try:
        # 尝试解析JSON格式的输入
        if text.strip().startswith("{") and text.strip().endswith("}"):
            data = json.loads(text)
            if "speaker" in data and "content" in data:
                speaker_name = data["speaker"]
                actual_content = data["content"]
                conn.logger.bind(tag=TAG).info(f"解析到说话人信息: {speaker_name}")

                # 仅在该说话人首次出现时保留 {"speaker":...} JSON，让模型自然称呼一次；
                # 后续轮降为纯文本，避免每轮重复出现名字诱导模型反复称呼
                if speaker_name not in conn.introduced_speakers:
                    conn.introduced_speakers.add(speaker_name)
                    actual_text = text
                else:
                    actual_text = actual_content
            elif "content" in data:
                # FunASR 等返回 {"content","language","emotion"}：只把识别正文交给 LLM，
                # 否则整段 JSON 会进入对话历史
                actual_text = data["content"]
    except (json.JSONDecodeError, KeyError):
        # 如果解析失败，继续使用原始文本
        pass

    # 保存说话人信息到连接对象
    if speaker_name:
        conn.current_speaker = speaker_name
    else:
        conn.current_speaker = None

    if conn.need_bind:
        await check_bind_device(conn)
        return

    # manual 模式下不打断正在播放的内容
    if conn.client_is_speaking and conn.client_listen_mode != "manual":
        await handleAbortMessage(conn)

    # [xiaozhi-hub] 新的一轮从这里开始：之前的轮次作废；下面呼名、意图识别、发 stt 都要等，
    # 期间设备打断了就不再回答
    gen = turn_gen.bump(conn)

    # [xiaozhi-hub] 呼名：句首"嗨 Claude"先去掉称呼；只有称呼时直接回复，不发给模型
    actual_text = await handle_call_name(conn, actual_text)
    if actual_text is None:
        return

    # [xiaozhi-hub] "清除聊天记录"：先问一遍，说确定才清（不交给模型，各种模型都能用）
    actual_text = await clear_command.handle(conn, actual_text)
    if actual_text is None:
        return

    # 首先进行意图分析，使用实际文本内容
    intent_handled = await handle_user_intent(conn, actual_text)

    if intent_handled:
        # 如果意图已被处理，不再进行聊天
        return

    if turn_gen.stale(conn, gen):
        conn.logger.bind(tag=TAG).info(f"开始回答前被打断，不再回答: {actual_text}")
        return

    # 意图未被处理，继续常规聊天流程，使用实际文本内容
    await send_stt_message(conn, actual_text)

    # 准备开始新会话（若是用户主动新一轮发言而非自动结束提示语，清除遗留的 close_after_chat）
    end_p = (conn.config.get("end_prompt") or {}).get("prompt")
    if not end_p or actual_text != end_p:
        conn.close_after_chat = False
    conn.client_abort = False

    ex = getattr(conn, "executor", None)
    if ex and not (getattr(conn, "stop_event", None) and conn.stop_event.is_set()):
        ex.submit(conn.chat, actual_text, turn=gen)


async def no_voice_close_connect(conn: "ConnectionHandler", have_voice):
    if have_voice or getattr(conn, "client_is_speaking", False):
        conn.last_activity_time = time.time() * 1000
        return
    # 只有在已经初始化过时间戳的情况下才进行超时检查
    if conn.last_activity_time > 0.0:
        no_voice_time = time.time() * 1000 - conn.last_activity_time
        close_connection_no_voice_time = int(
            conn.config.get("close_connection_no_voice_time", 120)
        )
        if (
            not conn.close_after_chat
            and no_voice_time > 1000 * close_connection_no_voice_time
        ):
            conn.close_after_chat = True
            conn.client_abort = False
            end_prompt = conn.config.get("end_prompt") or {}
            if not isinstance(end_prompt, dict):
                end_prompt = {}
            if end_prompt and end_prompt.get("enable", True) is False:
                conn.logger.bind(tag=TAG).info("结束对话，无需发送结束提示语")
                await conn.close()
                return
            # [xiaozhi-hub] 配置了固定告别语（end_prompt.reply）时直接说这句再断开，不发给大模型：
            # 接 Claude Code 时，"请你以时间过得真快开头……"这句假的用户消息会被永久写进对话记录
            farewell = end_prompt.get("reply")
            if farewell:
                if not conn.tts:
                    await conn.close()
                    return
                conn.sentence_id = uuid.uuid4().hex
                conn.client_is_speaking = True
                await send_tts_message(conn, "start")
                speak_txt(conn, farewell, record_dialogue=False)
                return
            prompt = end_prompt.get("prompt")
            if not prompt:
                prompt = "请你以```时间过得真快```未来头，用富有感情、依依不舍的话来结束这场对话吧。！"
            await startToChat(conn, prompt)


async def check_bind_device(conn: "ConnectionHandler"):
    """ThinkSmart 本地自主网关不依赖第三方智控台绑定，仅保留文本兜底避免引用已精简的无头固件音频。"""
    conn.client_abort = False
    text = (
        f"设备绑定码：{conn.bind_code}"
        if conn.bind_code
        else "当前设备由 ThinkSmart 统一管理后台本地纳管。"
    )
    await send_stt_message(conn, text)
