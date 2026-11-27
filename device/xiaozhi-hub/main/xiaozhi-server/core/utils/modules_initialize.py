from typing import Dict, Any
from config.logger import setup_logging
from core.utils import tts, llm, intent, memory, vad, asr

TAG = __name__
logger = setup_logging()

_SECTION_DEFAULTS = {
    "TTS": "EdgeTTS",
    "LLM": "DeepSeekLLM",
    "Intent": "function_call",
    "Memory": "nomem",
    "VAD": "SileroVAD",
    "ASR": "SherpaXASR",
}


def _resolve_module_entry(config: Dict[str, Any], section: str):
    sec_map = config.get(section)
    if not isinstance(sec_map, dict) or not sec_map:
        raise KeyError(f"配置缺少模块分组: {section}")
    sel_map = config.setdefault("selected_module", {})
    name = sel_map.get(section)
    if isinstance(name, str) and isinstance(sec_map.get(name), dict):
        return name, sec_map[name]
    fb = _SECTION_DEFAULTS.get(section)
    if fb and isinstance(sec_map.get(fb), dict):
        chosen = fb
    else:
        chosen = next((k for k, v in sec_map.items() if isinstance(v, dict)), None)
        if chosen is None:
            raise KeyError(f"模块分组 {section} 无可用条目")
    logger.bind(tag=TAG).warning(f"模块 {section} 配置项 {name!r} 已精简或不存在，自动回退为 {chosen}")
    sel_map[section] = chosen
    return chosen, sec_map[chosen]


def initialize_modules(
    logger,
    config: Dict[str, Any],
    init_vad=False,
    init_asr=False,
    init_llm=False,
    init_tts=False,
    init_memory=False,
    init_intent=False,
) -> Dict[str, Any]:
    """
    初始化所有模块组件

    Args:
        config: 配置字典

    Returns:
        Dict[str, Any]: 包含所有初始化后的模块的字典
    """
    modules = {}

    # 初始化TTS模块
    if init_tts:
        modules["tts"] = initialize_tts(config)
        logger.bind(tag=TAG).info(f"初始化组件: tts成功 {config['selected_module']['TTS']}")

    # 初始化LLM模块
    if init_llm:
        select_llm_module, llm_cfg = _resolve_module_entry(config, "LLM")
        llm_type = llm_cfg.get("type", select_llm_module)
        try:
            modules["llm"] = llm.create_instance(llm_type, llm_cfg)
        except ValueError:
            fb = _SECTION_DEFAULTS["LLM"]
            if fb in config.get("LLM", {}) and fb != select_llm_module:
                logger.bind(tag=TAG).warning(f"LLM 类型 {llm_type!r} 不再支持，自动回退为 {fb}")
                config["selected_module"]["LLM"] = select_llm_module = fb
                llm_cfg = config["LLM"][fb]
                modules["llm"] = llm.create_instance(llm_cfg.get("type", fb), llm_cfg)
            else:
                raise
        logger.bind(tag=TAG).info(f"初始化组件: llm成功 {select_llm_module}")

    # 初始化Intent模块
    if init_intent:
        select_intent_module, intent_cfg = _resolve_module_entry(config, "Intent")
        intent_type = intent_cfg.get("type", select_intent_module)
        try:
            modules["intent"] = intent.create_instance(intent_type, intent_cfg)
        except ValueError:
            fb = _SECTION_DEFAULTS["Intent"]
            if fb in config.get("Intent", {}) and fb != select_intent_module:
                logger.bind(tag=TAG).warning(f"Intent 类型 {intent_type!r} 不再支持，自动回退为 {fb}")
                config["selected_module"]["Intent"] = select_intent_module = fb
                intent_cfg = config["Intent"][fb]
                modules["intent"] = intent.create_instance(intent_cfg.get("type", fb), intent_cfg)
            else:
                raise
        logger.bind(tag=TAG).info(f"初始化组件: intent成功 {select_intent_module}")

    # 初始化Memory模块
    if init_memory:
        select_memory_module, mem_cfg = _resolve_module_entry(config, "Memory")
        memory_type = mem_cfg.get("type", select_memory_module)
        try:
            modules["memory"] = memory.create_instance(
                memory_type, mem_cfg, config.get("summaryMemory", None)
            )
        except ValueError:
            fb = _SECTION_DEFAULTS["Memory"]
            if fb in config.get("Memory", {}) and fb != select_memory_module:
                logger.bind(tag=TAG).warning(f"Memory 类型 {memory_type!r} 不再支持，自动回退为 {fb}")
                config["selected_module"]["Memory"] = select_memory_module = fb
                mem_cfg = config["Memory"][fb]
                modules["memory"] = memory.create_instance(
                    mem_cfg.get("type", fb), mem_cfg, config.get("summaryMemory", None)
                )
            else:
                raise
        logger.bind(tag=TAG).info(f"初始化组件: memory成功 {select_memory_module}")

    # 初始化VAD模块
    if init_vad:
        select_vad_module, vad_cfg = _resolve_module_entry(config, "VAD")
        vad_type = vad_cfg.get("type", select_vad_module)
        modules["vad"] = vad.create_instance(vad_type, vad_cfg)
        logger.bind(tag=TAG).info(f"初始化组件: vad成功 {select_vad_module}")

    # 初始化ASR模块
    if init_asr:
        modules["asr"] = initialize_asr(config)
        logger.bind(tag=TAG).info(f"初始化组件: asr成功 {config['selected_module']['ASR']}")
    return modules


def initialize_tts(config):
    select_tts_module, raw_tts_cfg = _resolve_module_entry(config, "TTS")
    tts_config = raw_tts_cfg.copy()
    tts_config.setdefault("tts_timeout", config.get("tts_timeout", 15))
    tts_type = tts_config.get("type", select_tts_module)
    del_audio = str(config.get("delete_audio", True)).lower() in ("true", "1", "yes")
    try:
        return tts.create_instance(tts_type, tts_config, del_audio)
    except ValueError:
        fb = _SECTION_DEFAULTS["TTS"]
        if fb in config.get("TTS", {}) and fb != select_tts_module:
            logger.bind(tag=TAG).warning(f"TTS 类型 {tts_type!r} 不再支持，自动回退为 {fb}")
            config["selected_module"]["TTS"] = fb
            fb_cfg = config["TTS"][fb].copy()
            fb_cfg.setdefault("tts_timeout", config.get("tts_timeout", 15))
            return tts.create_instance(fb_cfg.get("type", fb), fb_cfg, del_audio)
        raise


def initialize_asr(config):
    select_asr_module, asr_config = _resolve_module_entry(config, "ASR")
    asr_type = asr_config.get("type", select_asr_module)
    # [xiaozhi-hub] 支持热词的识别：带上后台维护的热词（配置顶层 asr_hotwords），
    # 以及呼名里模型的正确名字（如 Claude、DeepSeek）
    from core.agent.asr_text import effective_hotwords, hotwords_supported

    if hotwords_supported(asr_config):
        asr_config = {**asr_config, "hotwords": effective_hotwords(asr_config, config)}
    # [xiaozhi-hub] 豆包流式识别自己判断一句话说完：用后台"说完停顿多久算一句"的时长。
    # 上游写死 200ms，说话中间稍一停顿就会被截成半句
    if asr_config.get("type") == "doubao_stream":
        vad_name = (config.get("selected_module") or {}).get("VAD")
        silence = ((config.get("VAD") or {}).get(vad_name) or {}).get("min_silence_duration_ms")
        if silence:
            asr_config = {**asr_config, "end_window_size": max(200, int(silence))}
    new_asr = asr.create_instance(
        asr_type,
        asr_config,
        str(config.get("delete_audio", True)).lower() in ("true", "1", "yes"),
    )
    logger.bind(tag=TAG).info("ASR模块初始化完成")
    return new_asr


