import os
import yaml
from collections.abc import Mapping


def get_project_dir():
    """获取项目根目录"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/"


# [xiaozhi-hub] 有 libyaml 时用 C 解析器：板子上读一遍 config.yaml 从 0.7 秒降到 0.08 秒
# （后台和设备接口每次请求都要读配置，纯 Python 解析会占满一个核、拖慢同时在跑的语音识别）
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def read_config(config_path):
    if not os.path.exists(config_path):
        return {}
    with open(config_path, "r", encoding="utf-8") as file:
        config = yaml.load(file, Loader=_YAML_LOADER)
    return config if isinstance(config, dict) else {}


async def load_config():
    """加载配置文件"""
    from core.utils.cache.manager import cache_manager, CacheType

    # 检查缓存
    cached_config = cache_manager.get(CacheType.CONFIG, "main_config")
    if cached_config is not None:
        return cached_config

    default_config_path = get_project_dir() + "config.yaml"
    custom_config_path = get_project_dir() + "data/.config.yaml"

    # 加载默认配置
    default_config = read_config(default_config_path)
    custom_config = read_config(custom_config_path)
    config = merge_configs(default_config, custom_config)
    # 初始化目录
    ensure_directories(config)

    # 缓存配置
    cache_manager.set(CacheType.CONFIG, "main_config", config)
    return config


def ensure_directories(config):
    """确保所有配置路径存在"""
    if not isinstance(config, dict):
        return
    dirs_to_create = set()
    project_dir = get_project_dir()  # 获取项目根目录
    # 日志文件目录
    log_cfg = config.get("log")
    log_dir = (log_cfg.get("log_dir") if isinstance(log_cfg, dict) else None) or "tmp"
    dirs_to_create.add(log_dir if os.path.isabs(log_dir) else os.path.join(project_dir, log_dir))

    # ASR/TTS模块输出目录
    for module in ["ASR", "TTS"]:
        mod_map = config.get(module)
        if not isinstance(mod_map, dict):
            continue
        for provider in mod_map.values():
            if not isinstance(provider, dict):
                continue
            output_dir = provider.get("output_dir", "")
            if output_dir and isinstance(output_dir, str):
                dirs_to_create.add(output_dir if os.path.isabs(output_dir) else os.path.join(project_dir, output_dir))

    # 根据selected_module创建模型目录
    selected_modules = config.get("selected_module")
    if isinstance(selected_modules, dict):
        for module_type in ["ASR", "LLM", "TTS"]:
            selected_provider = selected_modules.get(module_type)
            if not selected_provider:
                continue
            mod_map = config.get(module_type)
            if not isinstance(mod_map, dict) or selected_provider not in mod_map:
                continue
            provider_config = mod_map.get(selected_provider)
            if not isinstance(provider_config, dict):
                continue
            output_dir = provider_config.get("output_dir")
            if output_dir and isinstance(output_dir, str):
                full_model_dir = output_dir if os.path.isabs(output_dir) else os.path.join(project_dir, output_dir)
                dirs_to_create.add(full_model_dir)

    # 统一创建目录（保留原data目录创建）
    for dir_path in dirs_to_create:
        try:
            os.makedirs(dir_path, exist_ok=True)
        except PermissionError:
            print(f"警告：无法创建目录 {dir_path}，请检查写入权限")


def merge_configs(default_config, custom_config):
    """
    递归合并配置，custom_config优先级更高

    Args:
        default_config: 默认配置
        custom_config: 用户自定义配置

    Returns:
        合并后的配置
    """
    if not isinstance(default_config, Mapping):
        return dict(custom_config) if isinstance(custom_config, Mapping) else {}
    if not isinstance(custom_config, Mapping):
        return dict(default_config)

    merged = dict(default_config)

    for key, value in custom_config.items():
        if (
            key in merged
            and isinstance(merged[key], Mapping)
            and isinstance(value, Mapping)
        ):
            merged[key] = merge_configs(merged[key], value)
        else:
            merged[key] = value

    return merged
