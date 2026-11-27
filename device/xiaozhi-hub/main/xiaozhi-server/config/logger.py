import os
import sys
import asyncio
from loguru import logger
from config.config_loader import load_config
from config.settings import check_config_file
from core.utils.cache.manager import cache_manager, CacheType

SERVER_VERSION = "0.9.7"
_logger_initialized = False


def get_module_abbreviation(module_name, module_dict):
    """获取模块名称的缩写，如果为空则返回00
    如果名称中包含下划线，则返回下划线后面的前两个字符
    """
    module_value = module_dict.get(module_name, "")
    if not module_value:
        return "00"
    if "_" in module_value:
        parts = module_value.split("_")
        return parts[-1][:2] if parts[-1] else "00"
    return module_value[:2]


def build_module_string(selected_module):
    """构建模块字符串"""
    return (
        get_module_abbreviation("VAD", selected_module)
        + get_module_abbreviation("ASR", selected_module)
        + get_module_abbreviation("LLM", selected_module)
        + get_module_abbreviation("TTS", selected_module)
        + get_module_abbreviation("Memory", selected_module)
        + get_module_abbreviation("Intent", selected_module)
    )


def formatter(record):
    """为没有 tag 的日志添加默认值，并处理动态模块字符串"""
    record["extra"].setdefault("tag", record["name"])
    # 如果没有设置 selected_module，使用默认值
    record["extra"].setdefault("selected_module", "00000000000000")
    # 将 selected_module 从 extra 提取到顶级，以支持 {selected_module} 格式
    record["selected_module"] = record["extra"]["selected_module"]
    return record["message"]


def setup_logging(config=None):
    """从配置文件中读取日志配置，并设置日志输出格式和级别"""
    global _logger_initialized
    if _logger_initialized and config is None:
        return logger
    if config is None:
        check_config_file()
        # 先查缓存，避免在 async 上下文中重复 await load_config
        config = cache_manager.get(CacheType.CONFIG, "main_config")
        if config is None:
            try:
                asyncio.get_running_loop()
                in_loop = True
            except RuntimeError:
                in_loop = False
            if in_loop:
                import threading
                box = [None, None]
                def _load_in_thread():
                    try:
                        box[0] = asyncio.run(load_config())
                    except BaseException as exc:
                        box[1] = exc
                t = threading.Thread(target=_load_in_thread, daemon=True)
                t.start()
                t.join(timeout=10)
                if box[1] is not None:
                    raise box[1]
                config = box[0] or {"log": {}}
            else:
                config = asyncio.run(load_config())
    log_config = config.get("log") or {}

    # 第一次初始化时配置日志
    if not _logger_initialized:
        # 使用默认的模块字符串进行初始化
        logger.configure(
            extra={
                "selected_module": log_config.get("selected_module", "00000000000000"),
            }
        )

        log_format = log_config.get(
            "log_format",
            "<green>{time:YYMMDD HH:mm:ss}</green>[{version}_{extra[selected_module]}][<light-blue>{extra[tag]}</light-blue>]-<level>{level}</level>-<light-green>{message}</light-green>",
        )
        log_format_file = log_config.get(
            "log_format_file",
            "{time:YYYY-MM-DD HH:mm:ss} - {version}_{extra[selected_module]} - {name} - {level} - {extra[tag]} - {message}",
        )
        log_format = log_format.replace("{version}", SERVER_VERSION)
        log_format_file = log_format_file.replace("{version}", SERVER_VERSION)

        log_level = log_config.get("log_level", "INFO")
        log_dir = log_config.get("log_dir", "tmp")
        log_file = log_config.get("log_file", "server.log")
        data_dir = log_config.get("data_dir", "data")

        os.makedirs(log_dir, exist_ok=True)
        os.makedirs(data_dir, exist_ok=True)

        # 配置日志输出
        logger.remove()

        # 输出到控制台
        logger.add(sys.stdout, format=log_format, level=log_level, filter=formatter, backtrace=True, diagnose=False)

        # 输出到文件 - 统一目录，按大小轮转
        # 日志文件完整路径
        log_file_path = os.path.join(log_dir, log_file)

        # 添加日志处理器
        logger.add(
            log_file_path,
            format=log_format_file,
            level=log_level,
            filter=formatter,
            rotation="2 MB",  # eMMC 瘦身：单文件上限 2MB
            retention=2,  # 最多保留 2 份压缩归档（总占用 <= 2.5MB）
            compression="gz",
            encoding="utf-8",
            enqueue=True,  # 异步安全
            backtrace=True,
            diagnose=False,  # 禁止异常栈打印局部变量，防止泄露密钥
        )
        _logger_initialized = True  # 标记为已初始化

    return logger


def create_connection_logger(selected_module_str):
    """为连接创建独立的日志器，绑定特定的模块字符串"""
    return logger.bind(selected_module=selected_module_str)
