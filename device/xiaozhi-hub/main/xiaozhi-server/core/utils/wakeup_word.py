import os
import re
import yaml
import time
import hashlib
import portalocker
from typing import Dict


class FileLock:
    def __init__(self, file, timeout=5):
        self.file = file
        self.timeout = timeout
        self.start_time = None

    def __enter__(self):
        self.start_time = time.time()
        while True:
            try:
                portalocker.lock(self.file, portalocker.LOCK_EX | portalocker.LOCK_NB)
                return self.file
            except portalocker.LockException:
                if time.time() - self.start_time > self.timeout:
                    raise TimeoutError("获取文件锁超时")
                time.sleep(0.1)

    def __exit__(self, exc_type, exc_val, exc_tb):
        portalocker.unlock(self.file)


class WakeupWordsConfig:
    def __init__(self):
        from config.config_loader import get_project_dir
        proj = get_project_dir()
        self.config_file = os.path.join(proj, "data", ".wakeup_words.yaml")
        self.assets_dir = os.path.join(proj, "config", "assets", "wakeup_words")
        self._ensure_directories()
        self._config_cache = None
        self._config_sig = None
        self._last_load_time = 0
        self._cache_ttl = 1  # 缓存有效期（秒）
        self._lock_timeout = 5  # 文件锁超时时间（秒）

    def _ensure_directories(self):
        """确保必要的目录存在"""
        os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
        os.makedirs(self.assets_dir, exist_ok=True)

    def _load_config(self) -> Dict:
        """加载配置文件，使用缓存机制"""
        current_time = time.time()

        # 如果缓存有效，直接返回缓存
        if (
            self._config_cache is not None
            and current_time - self._last_load_time < self._cache_ttl
        ):
            return self._config_cache
        if self._config_cache is not None:
            try:
                st = os.stat(self.config_file)
                if (st.st_mtime_ns, st.st_size) == self._config_sig:
                    self._last_load_time = current_time
                    return self._config_cache
            except OSError:
                pass

        try:
            with open(self.config_file, "a+", encoding="utf-8") as f:
                with FileLock(f, timeout=self._lock_timeout):
                    f.seek(0)
                    content = f.read()
                    loaded = yaml.safe_load(content) if content else {}
                    config = loaded if isinstance(loaded, dict) else {}
                    self._config_cache = config
                    self._last_load_time = current_time
                    try:
                        fst = os.fstat(f.fileno())
                        self._config_sig = (fst.st_mtime_ns, fst.st_size)
                    except OSError:
                        self._config_sig = None
                    return config
        except (TimeoutError, IOError) as e:
            print(f"加载配置文件失败: {e}")
            return {}
        except Exception as e:
            print(f"加载配置文件时发生未知错误: {e}")
            return {}

    def _save_config(self, config: Dict):
        """保存配置到文件，先拿锁再截断写入，防止并发读到空文件"""
        try:
            with open(self.config_file, "a+", encoding="utf-8") as f:
                with FileLock(f, timeout=self._lock_timeout):
                    f.seek(0)
                    f.truncate()
                    yaml.dump(config, f, allow_unicode=True)
                    f.flush()
                    self._config_cache = config
                    self._last_load_time = time.time()
        except (TimeoutError, IOError) as e:
            print(f"保存配置文件失败: {e}")
            raise
        except Exception as e:
            print(f"保存配置文件时发生未知错误: {e}")
            raise

    def get_wakeup_response(self, voice: str) -> Dict:
        voice = hashlib.md5(voice.encode()).hexdigest()
        """获取唤醒词回复配置"""
        config = self._load_config()

        if not config or voice not in config:
            return None

        # 检查文件大小
        file_path = config[voice]["file_path"]
        if not os.path.exists(file_path) or os.stat(file_path).st_size < (15 * 1024):
            return None

        return config[voice]

    def update_wakeup_response(self, voice: str, file_path: str, text: str):
        """更新唤醒词回复配置"""
        try:
            # 过滤表情符号
            filtered_text = re.sub(r'[\U0001F600-\U0001F64F\U0001F900-\U0001F9FF]', '', text)
            
            config = self._load_config()
            voice_hash = hashlib.md5(voice.encode()).hexdigest()
            config[voice_hash] = {
                "voice": voice,
                "file_path": file_path,
                "time": time.time(),
                "text": filtered_text,
            }
            self._save_config(config)
        except Exception as e:
            print(f"更新唤醒词回复配置失败: {e}")
            raise

    def generate_file_path(self, voice: str) -> str:
        """生成音频文件路径，使用voice的哈希值作为文件名（由调用方原子替换）"""
        try:
            voice_hash = hashlib.md5(voice.encode()).hexdigest()
            os.makedirs(self.assets_dir, exist_ok=True)
            return os.path.join(self.assets_dir, f"{voice_hash}.wav")
        except Exception as e:
            print(f"生成音频文件路径失败: {e}")
            raise