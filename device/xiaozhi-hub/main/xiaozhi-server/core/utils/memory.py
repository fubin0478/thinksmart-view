import os
import re
import sys
import importlib
from config.config_loader import get_project_dir
from config.logger import setup_logging

logger = setup_logging()


def create_instance(class_name, *args, **kwargs):
    if isinstance(class_name, str) and re.fullmatch(r"[A-Za-z0-9_]+", class_name):
        rel = os.path.join("core", "providers", "memory", class_name, f"{class_name}.py")
        if os.path.exists(os.path.join(get_project_dir(), rel)) or os.path.exists(rel):
            lib_name = f"core.providers.memory.{class_name}.{class_name}"
            if lib_name not in sys.modules:
                sys.modules[lib_name] = importlib.import_module(f"{lib_name}")
            return sys.modules[lib_name].MemoryProvider(*args, **kwargs)

    raise ValueError(f"不支持的记忆服务类型: {class_name}")
