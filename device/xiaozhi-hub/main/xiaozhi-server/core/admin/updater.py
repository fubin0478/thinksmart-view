"""自主维护版本信息桩：已彻底移除第三方 Git 在线更新与外部仓库轮询。"""

from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = SERVER_DIR.parents[1]
SELF_MANAGED_MESSAGE = "当前小智网关已纳入 ThinkSmart View 本地自主维护仓库管理，已停用第三方 GitHub 在线更新。"


class UpdateError(Exception):
    """在线更新已永久停用。"""


class Updater:
    def __init__(self, root: Path = REPO_ROOT, state_dir: Path = None, *, self_managed: bool = True):
        self.root = Path(root)
        self.self_managed = True
        self.state_dir = Path(state_dir) if state_dir else SERVER_DIR / "tmp"
        self.last_check = None
        self.running_commit = "1.0.0-self"

    def version(self) -> dict:
        info = {"commit": "1.0.0-self", "short": "self-owned", "date": "", "subject": "ThinkSmart View 自主维护版"}
        return {
            "supported": False,
            "self_managed": True,
            "message": SELF_MANAGED_MESSAGE,
            "source": {"remote": "local", "branch": "main", "url": ""},
            "current": info,
            "running": info,
            "dirty": [],
        }

    def check(self) -> dict:
        v = self.version()
        res = {**v, "state": "self_managed", "up_to_date": True, "behind": 0, "ahead": 0, "commits": []}
        self.last_check = res
        return res

    def start(self) -> dict:
        raise UpdateError(SELF_MANAGED_MESSAGE)

    def status(self) -> dict:
        return {"state": "self_managed", "self_managed": True, "message": SELF_MANAGED_MESSAGE}
