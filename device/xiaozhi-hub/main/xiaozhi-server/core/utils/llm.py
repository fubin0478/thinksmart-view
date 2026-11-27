import os
import re
import sys

# 添加项目根目录到Python路径
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "..", ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from config.config_loader import get_project_dir
from config.logger import setup_logging
import importlib

logger = setup_logging()


def create_instance(class_name, *args, **kwargs):
    # 创建LLM实例
    if isinstance(class_name, str) and re.fullmatch(r"[A-Za-z0-9_]+", class_name):
        rel = os.path.join('core', 'providers', 'llm', class_name, f'{class_name}.py')
        if os.path.exists(os.path.join(get_project_dir(), rel)) or os.path.exists(os.path.join(project_root, rel)) or os.path.exists(rel):
            lib_name = f'core.providers.llm.{class_name}.{class_name}'
            if lib_name not in sys.modules:
                sys.modules[lib_name] = importlib.import_module(f'{lib_name}')
            return sys.modules[lib_name].LLMProvider(*args, **kwargs)

    raise ValueError(f"不支持的LLM类型: {class_name}，请检查该配置的type是否设置正确")
