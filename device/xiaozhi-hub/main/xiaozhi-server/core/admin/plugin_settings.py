"""插件配置检查：供状态自检判断必填密钥是否已填写。"""

from core.admin import catalog, config_store


def config_key(settings_id: str) -> str:
    spec = catalog.PLUGIN_SETTINGS.get(settings_id)
    if not spec:
        raise KeyError(settings_id)
    return next(p["config"] for p in catalog.PLUGINS if p.get("settings") == settings_id)


def _filled(value) -> bool:
    return not config_store.is_placeholder(value) and not str(value).strip().lower().endswith("-xxx")


def configured(merged: dict, settings_id: str) -> bool:
    spec = catalog.PLUGIN_SETTINGS.get(settings_id)
    if not spec:
        raise KeyError(settings_id)
    saved = (merged.get("plugins") or {}).get(config_key(settings_id))
    saved = saved if isinstance(saved, dict) else {}
    legacy = any(k in saved for k in spec.get("legacy_keys", ()))
    for f in spec["fields"]:
        if not f.get("required"):
            continue
        raw = saved.get(f["key"])
        if f["type"] == "secret":
            if legacy or not _filled(raw):
                return False
        elif not _filled(raw):
            return False
    return True
