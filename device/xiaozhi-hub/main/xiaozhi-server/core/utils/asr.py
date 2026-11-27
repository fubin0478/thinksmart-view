import importlib
import logging
import os
import sys
import time
import wave
import uuid
from abc import ABC, abstractmethod
from typing import Optional, Tuple, List
from core.providers.asr.base import ASRProviderBase
import re
from config.config_loader import get_project_dir
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

def create_instance(class_name: str, *args, **kwargs) -> ASRProviderBase:
    """工厂方法创建ASR实例"""
    if isinstance(class_name, str) and re.fullmatch(r"[A-Za-z0-9_]+", class_name):
        rel = os.path.join('core', 'providers', 'asr', f'{class_name}.py')
        if os.path.exists(os.path.join(get_project_dir(), rel)) or os.path.exists(rel):
            lib_name = f'core.providers.asr.{class_name}'
            if lib_name not in sys.modules:
                sys.modules[lib_name] = importlib.import_module(f'{lib_name}')
            return sys.modules[lib_name].ASRProvider(*args, **kwargs)

    raise ValueError(f"不支持的ASR类型: {class_name}，请检查该配置的type是否设置正确")