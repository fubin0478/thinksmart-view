import os
import re
import sys
from config.config_loader import get_project_dir
from config.logger import setup_logging
import importlib

logger = setup_logging()


def create_instance(class_name, *args, **kwargs):
    # 创建intent实例
    if isinstance(class_name, str) and re.fullmatch(r"[A-Za-z0-9_]+", class_name):
        rel = os.path.join('core', 'providers', 'intent', class_name, f'{class_name}.py')
        if os.path.exists(os.path.join(get_project_dir(), rel)) or os.path.exists(rel):
            lib_name = f'core.providers.intent.{class_name}.{class_name}'
            if lib_name not in sys.modules:
                sys.modules[lib_name] = importlib.import_module(f'{lib_name}')
            return sys.modules[lib_name].IntentProvider(*args, **kwargs)

    raise ValueError(f"不支持的intent类型: {class_name}，请检查该配置的type是否设置正确")