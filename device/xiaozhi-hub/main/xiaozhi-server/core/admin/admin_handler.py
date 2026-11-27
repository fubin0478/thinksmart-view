"""统一后台接管后的精简 Kiosk HTTP 接口：仅保留 /admin/ 引导跳转页、410 停用占位和本机 /kiosk/* 内部控制接口。"""

import asyncio
import atexit
import copy
import functools
import io
import json
import os
import sys
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aiohttp import web

from config.logger import setup_logging
from core.admin import catalog, config_store, connections, kiosk_voice, pages, pipeline, updater
from core.admin.status import collect_status
from core.agent import chat_history, llm_router
from core.utils.modules_initialize import initialize_modules

TAG = __name__

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="admin")
atexit.register(_POOL.shutdown, False)

logger = setup_logging()
_LOCAL_STATIC = Path(__file__).parent / "static"
STATIC_DIR = _LOCAL_STATIC if _LOCAL_STATIC.is_dir() else (updater.SERVER_DIR / "core" / "admin" / "static")
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
KIOSK_ASR_MAX_BYTES = 16000 * 2 * 10  # /kiosk/asr 最多 10 秒音频
KIOSK_TTS_MAX_CHARS = 200  # /kiosk/tts 一次最多合成字数
KIOSK_TTS_RATE = 24000     # /kiosk/tts 返回的采样率：24 kHz


def _wav(pcm: bytes, rate: int) -> bytes:
    """16 位单声道 PCM 加上 WAV 文件头。"""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


class AdminHandler:
    _PROXY_FWD_HEADERS = ("X-Forwarded-For", "Forwarded", "X-Real-IP", "CF-Connecting-IP", "True-Client-IP")

    def __init__(self, config: dict, ws_server=None, config_path: Path = None,
                 gateway_updater: "updater.Updater" = None):
        self.config = config
        self.ws_server = ws_server
        self.config_path = config_path
        self.updater = gateway_updater or updater.Updater()
        self.logger = logger

    def register_kiosk_only(self, app: web.Application) -> None:
        """统一后台接管模式：保留 /admin/ 引导跳转与本机 /kiosk/* 接口，停用旧 /admin/api/*。"""
        app.add_routes(
            [
                web.get("/admin", self.handle_index),
                web.get("/admin/", self.handle_index),
                web.route("*", "/admin/api/{tail:.*}", self.handle_legacy_admin_disabled),
                web.get("/kiosk/llm", self.handle_kiosk_llm),
                web.post("/kiosk/llm/use", self.handle_kiosk_llm_use),
                web.post("/kiosk/asr", self.handle_kiosk_asr),
                web.post("/kiosk/tts", self.handle_kiosk_tts),
                web.get("/kiosk/chat/history", self.handle_kiosk_chat_history),
                web.post("/kiosk/chat/clear", self.handle_kiosk_chat_clear),
                web.post("/kiosk/internal/reload", self.handle_kiosk_internal_reload),
                web.get("/kiosk/internal/memory", self.handle_kiosk_internal_memory),
                web.post("/kiosk/internal/memory", self.handle_kiosk_internal_memory_save),
                web.get("/kiosk/internal/diag", self.handle_kiosk_internal_diag),
                web.post("/kiosk/internal/diag", self.handle_kiosk_internal_diag_post),
                web.get("/kiosk/internal/voice", self.handle_kiosk_internal_voice),
                web.post("/kiosk/internal/voice", self.handle_kiosk_internal_voice_save),
            ]
        )

    async def handle_legacy_admin_disabled(self, request: web.Request):
        return self._json(
            {
                "ok": False,
                "error": "原语音网关独立后台接口已停用，请统一使用 ThinkSmart 管理后台 (:18091/admin/)",
                "unified_admin": ":18091/admin/",
            },
            410,
        )

    @staticmethod
    def _json(data, status=200):
        return web.json_response(data, status=status, headers=SECURITY_HEADERS)

    def _error(self, message, status):
        return self._json({"ok": False, "error": message}, status)

    @staticmethod
    async def _blocking(func, *args, **kwargs):
        return await asyncio.get_running_loop().run_in_executor(_POOL, functools.partial(func, *args, **kwargs))

    @staticmethod
    async def _read_json(request: web.Request):
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="需要 application/json")
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="JSON 格式错误")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="JSON 格式错误")
        return body

    async def handle_index(self, request: web.Request):
        html = await self._blocking((STATIC_DIR / "index.html").read_text, encoding="utf-8")
        return web.Response(text=html, content_type="text/html", headers=SECURITY_HEADERS)

    def _merged(self) -> dict:
        if self.config_path:
            return config_store.load_merged(custom_path=self.config_path)
        return config_store.load_merged()

    def _running(self, section: str):
        return (self.config.get("selected_module") or {}).get(section)

    async def _apply(self, section: str) -> dict:
        merged = await self._blocking(self._merged)
        new_section = merged.get(section, {})
        new_selected = (merged.get("selected_module") or {}).get(section)
        if not new_selected:
            return {
                "applied": False, "failed": True, "error": f"未选择 {section} 模块",
                "note": f"已保存，但加载失败：未选择 {section} 模块",
            }

        def commit(sel=new_selected):
            self.config[section] = new_section
            self.config.setdefault("selected_module", {})[section] = sel

        if section == "TTS" or self.ws_server is None:
            commit()
            if section == "TTS":
                return {"applied": True, "note": "已保存，设备重新连接后生效"}
            return {"applied": False, "note": "已保存，重启服务后生效"}
        flags = {"LLM": "init_llm", "Intent": "init_intent", "ASR": "init_asr"}
        attr = {"LLM": "_llm", "Intent": "_intent", "ASR": "_asr"}[section]
        key = {"LLM": "llm", "Intent": "intent", "ASR": "asr"}[section]
        candidate = {
            **self.config,
            section: new_section,
            "selected_module": {**self.config.get("selected_module", {}), section: new_selected},
        }
        try:
            async with self.ws_server.config_lock:
                modules = await self._blocking(initialize_modules, self.logger, candidate, **{flags[section]: True})
                commit((candidate.get("selected_module") or {}).get(section) or new_selected)
                old = getattr(self.ws_server, attr, None)
                setattr(self.ws_server, attr, modules[key])
            self._retire(old, modules[key])
            return {"applied": True, "note": "已生效，设备重新连接后使用新配置"}
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"热加载 {section} 失败: {e}")
            error = str(e).strip().rstrip("。.") or type(e).__name__
            return {
                "applied": False, "failed": True, "error": error,
                "note": f"已保存，但加载失败：{error}。服务仍在使用原来的配置，请检查后重试",
            }

    def _retire(self, old, new) -> None:
        if old is None or old is new or not callable(getattr(old, "shutdown", None)):
            return
        try:
            old.shutdown()
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"旧实例退役出错: {e}")

    def _push_config(self, key: str, value) -> None:
        self.config[key] = copy.deepcopy(value)
        for item in connections.snapshot():
            conn = connections.get(item["session_id"])
            if conn is not None:
                conn.config[key] = copy.deepcopy(value)

    @staticmethod
    def _from_this_machine(request: web.Request) -> bool:
        if any(h in request.headers for h in AdminHandler._PROXY_FWD_HEADERS):
            return False
        return request.remote in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    async def handle_kiosk_llm(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        merged = await self._blocking(self._merged)
        return self._json({"ok": True, **await self._kiosk_models(merged, request.query.get("device"))})

    async def _kiosk_models(self, merged: dict, device: str = None) -> dict:
        data = pages.describe_llm(merged)
        allow_gem = llm_router.gemini_enabled(merged)
        options = [
            {"kind": "api", "vendor": v["id"], "label": v["label"], "entry": v["entry"], "model": v["model"]}
            for v in data["vendors"]
            if v.get("configured") and (allow_gem or v["id"] != "gemini")
        ]
        grok_ent = (merged.get("LLM") or {}).get("GrokLLM")
        if isinstance(grok_ent, dict) and grok_ent.get("type") == "openai":
            g_url = str(grok_ent.get("base_url") or grok_ent.get("url") or "").strip()
            g_mod = str(grok_ent.get("model_name") or "").strip()
            g_key = str(grok_ent.get("api_key") or "").strip()
            if g_url and g_mod and g_key and not config_store.is_placeholder(g_key) and not any(o["entry"] == "GrokLLM" for o in options):
                options.append({"kind": "api", "vendor": "grok", "label": "xAI Grok", "entry": "GrokLLM", "model": g_mod})
        selected = data["selected"]
        if device:
            name = (llm_router.store().get(device) or {}).get("model")
            if name and llm_router.model_ready(merged, name) and any(o["entry"] == name for o in options):
                selected = name
        return {
            "selected": selected, "default": data["selected"],
            "kind": data["kind"], "vendor": data["vendor"], "options": options,
        }

    async def handle_kiosk_llm_use(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        body = await self._read_json(request)
        device = str(body.get("device") or "")
        if not device:
            return self._error("缺少 device", 400)
        merged = await self._blocking(self._merged)
        kind = body.get("kind")
        if kind == "api" and str(body.get("vendor") or "").lower() == "gemini" and not llm_router.gemini_enabled(merged):
            return self._error("已按策略停用 Gemini 模型，请使用 GPT 或 DeepSeek 模型", 400)
        if kind == "sync":
            return self._json({"ok": True, "applied": True, "note": "已同步模型列表",
                               **await self._kiosk_models(merged, device)})
        if kind == "default":
            default_name = (merged.get("selected_module") or {}).get("LLM")
            await self._blocking(llm_router.store().forget, device)
            if default_name:
                for item in connections.snapshot():
                    conn = connections.get(item["session_id"])
                    if conn is not None and item["device_id"] == device:
                        try:
                            await self._blocking(llm_router.switch, conn, default_name, remember=False)
                            ws_llm = getattr(self.ws_server, "_llm", None)
                            if ws_llm is not None and conn.llm is not ws_llm:
                                llm_router._attach(conn, ws_llm)
                                conn.llm = ws_llm
                        except Exception as e:
                            self.logger.bind(tag=TAG).warning(f"恢复设备 {device} 默认模型失败: {e}")
            return self._json({"ok": True, "applied": True, "note": "已恢复跟随后台默认模型",
                               **await self._kiosk_models(merged, device)})
        req_vid = str(body.get("vendor") or "")
        if req_vid == "grok":
            g_ent = (merged.get("LLM") or {}).get("GrokLLM") or {}
            g_ok = (
                g_ent.get("type") == "openai"
                and bool(str(g_ent.get("base_url") or g_ent.get("url") or "").strip())
                and bool(str(g_ent.get("model_name") or "").strip())
                and not config_store.is_placeholder(g_ent.get("api_key"))
            )
            if not g_ok:
                return self._error("xAI Grok 还没配置好，请先填写接口地址、模型和 API Key 并保存", 400)
            name = "GrokLLM"
        else:
            vendor = catalog.VENDOR_BY_ID.get(req_vid)
            if vendor is None:
                return self._error("不支持这个厂商", 404)
            name = vendor["entry"]
            if not pages.describe_vendor(vendor, merged)["configured"]:
                return self._error(f"{vendor['label']} 还没配置好，请先填写接口地址、模型和 API Key 并保存", 400)
        running = self.config.setdefault("LLM", {})
        if running.get(name) != merged["LLM"][name]:
            running[name] = copy.deepcopy(merged["LLM"][name])
        before = llm_router.store().get(device)
        await self._blocking(llm_router.store().put, device, {"model": name})
        for item in connections.snapshot():
            conn = connections.get(item["session_id"])
            if conn is None or item["device_id"] != device:
                continue
            try:
                ok = await self._blocking(llm_router.switch, conn, name, remember=False)
                error = "" if ok is not False else "模型没有配置好"
            except Exception as e:
                ok, error = False, str(e).strip() or type(e).__name__
            if ok is False:
                self.logger.bind(tag=TAG).error(f"在线连接 {item['session_id']} 切换模型 {name} 失败: {error}")
                if before:
                    await self._blocking(llm_router.store().put, device,
                                         {k: v for k, v in before.items() if k != "last_active"})
                else:
                    await self._blocking(llm_router.store().forget, device)
                return self._error(f"切换失败：{error}", 500)
        self.logger.bind(tag=TAG).info(f"设备 {device} 切换模型为 {name}（只这台设备）")
        return self._json({"ok": True, "applied": True, "note": "已切换，这台设备之后都用它",
                           **await self._kiosk_models(merged, device)})

    async def handle_kiosk_asr(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        asr = getattr(self.ws_server, "_asr", None)
        if not callable(getattr(asr, "recognize_pcm", None)):
            return self._error("当前语音识别不是本机模型", 501)
        content = getattr(request, "content", None)
        pcm = await content.read(KIOSK_ASR_MAX_BYTES + 1) if (content is not None and hasattr(content, "read")) else await request.read()
        if len(pcm) < 2 or len(pcm) > KIOSK_ASR_MAX_BYTES:
            return self._error("要 10 秒以内的 16kHz 单声道 16 位 PCM", 400)
        started = time.monotonic()
        text = await self._blocking(asr.recognize_pcm, pcm)
        return self._json({"ok": True, "text": text, "ms": round((time.monotonic() - started) * 1000)})

    async def handle_kiosk_tts(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        body = await self._read_json(request)
        text = str(body.get("text") or "").strip()
        if not text or len(text) > KIOSK_TTS_MAX_CHARS:
            return self._error(f"text 要 1 到 {KIOSK_TTS_MAX_CHARS} 个字", 400)
        started = time.monotonic()
        try:
            pcm = await self._blocking(self._synthesize, text)
        except NotImplementedError:
            return self._error("当前语音合成不支持整句合成", 501)
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"/kiosk/tts 合成失败: {e}")
            pcm = None
        if not pcm:
            return self._error("合成失败，请检查语音合成服务能否访问", 502)
        self.logger.bind(tag=TAG).info(f"/kiosk/tts 合成 {len(text)} 字，用时 {time.monotonic() - started:.1f}s")
        return web.Response(body=_wav(pcm, KIOSK_TTS_RATE), content_type="audio/wav", headers=SECURITY_HEADERS)

    def _synthesize(self, text: str) -> bytes:
        import socket
        import urllib.request as _ureq
        from core.providers.tts.dto.dto import InterfaceType
        from core.utils.modules_initialize import initialize_tts

        sel_tts = str((self.config.get("selected_module") or {}).get("TTS") or "")
        tts_map = self.config.get("TTS") or {}
        if sel_tts == "IndexStreamTTS":
            idx_cfg = tts_map.get("IndexStreamTTS") or {}
            a_url = str(idx_cfg.get("api_url") or "http://127.0.0.1:11996/tts").strip()
            voice = str(idx_cfg.get("private_voice") or idx_cfg.get("voice") or "xiaohe").strip()
            if a_url.startswith(("http://", "https://")):
                op = _ureq.build_opener(_ureq.ProxyHandler({}))
                req = _ureq.Request(
                    a_url,
                    data=json.dumps({"text": text, "character": voice}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with op.open(req, timeout=20) as resp:
                    raw = resp.read(4 * 1024 * 1024)
                if raw[:4] == b"RIFF" and len(raw) > 44:
                    from core.utils.util import decode_audio_bytes
                    dec = decode_audio_bytes(raw, "wav", KIOSK_TTS_RATE)
                    raw = dec if dec else raw[44:]
                if len(raw) % 2 == 1:
                    raw = raw[:-1]
                if len(raw) >= 400:
                    return raw
            return b""
        elif sel_tts == "OpenAITTS":
            oa_cfg = tts_map.get("OpenAITTS") or {}
            a_url = str(oa_cfg.get("api_url") or "https://api.openai.com/v1/audio/speech").strip()
            mod = str(oa_cfg.get("model") or "tts-1").strip()
            voi = str(oa_cfg.get("voice") or "alloy").strip()
            raw_ak = str(oa_cfg.get("api_key") or "").strip()
            ak = "" if config_store.is_placeholder(raw_ak) else raw_ak
            try:
                spd = max(0.5, min(2.0, float(oa_cfg.get("speed") if oa_cfg.get("speed") is not None else 1.0)))
            except (TypeError, ValueError):
                spd = 1.0
            if a_url.startswith(("http://", "https://")):
                prx = {}
                if "api.openai.com" in a_url.lower():
                    try:
                        with socket.create_connection(("127.0.0.1", 7890), timeout=0.3):
                            prx = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
                    except OSError:
                        prx = {}
                hdrs = {"Content-Type": "application/json"}
                if ak:
                    try:
                        ak.encode("latin-1")
                        hdrs["Authorization"] = f"Bearer {ak}"
                    except UnicodeEncodeError:
                        pass
                try:
                    op = _ureq.build_opener(_ureq.ProxyHandler(prx))
                    req = _ureq.Request(
                        a_url,
                        data=json.dumps({"model": mod, "input": text, "voice": voi, "response_format": "pcm", "speed": spd}).encode("utf-8"),
                        headers=hdrs,
                        method="POST",
                    )
                    with op.open(req, timeout=20) as resp:
                        raw = resp.read(4 * 1024 * 1024)
                    if raw[:4] == b"RIFF" and len(raw) > 44:
                        from core.utils.util import decode_audio_bytes
                        dec = decode_audio_bytes(raw, "wav", KIOSK_TTS_RATE)
                        if dec:
                            return dec
                        raw = raw[44:]
                    if len(raw) % 2 == 1:
                        raw = raw[:-1]
                    if len(raw) >= 400:
                        return raw
                except Exception:
                    pass

        tts = initialize_tts(self.config)
        try:
            if tts.interface_type != InterfaceType.NON_STREAM:
                raise NotImplementedError
            return tts.synthesize_pcm(text, KIOSK_TTS_RATE)
        finally:
            enc = getattr(tts, "opus_encoder", None)
            if enc is not None and callable(getattr(enc, "close", None)):
                try:
                    enc.close()
                except Exception:
                    pass

    async def handle_kiosk_chat_history(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        device = request.query.get("device") or ""
        if not device:
            return self._error("缺少 device", 400)
        cfg = chat_history.settings(await self._blocking(self._merged))
        try:
            limit = max(1, min(int(request.query.get("limit") or cfg["display_limit"]), 500))
        except ValueError:
            return self._error("limit 要是数字", 400)
        models = await self._blocking(chat_history.history, device, limit)
        return self._json({"ok": True, "models": models})

    async def handle_kiosk_chat_clear(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        body = await self._read_json(request)
        device = str(body.get("device") or "")
        if not device:
            return self._error("缺少 device", 400)
        online = [c for c in (connections.get(i["session_id"]) for i in connections.snapshot()
                              if i["device_id"] == device) if c is not None]
        merged = await self._blocking(self._merged)
        result = await self._blocking(chat_history.clear, device, merged, bool(body.get("memory_only")), online)
        return self._json({"ok": True, **result})

    async def handle_kiosk_internal_reload(self, request: web.Request):
        if (
            not self._from_this_machine(request)
            or request.headers.get("X-TS-Internal-Reload") != "1"
            or "Origin" in request.headers
        ):
            return self._error("仅限本机内部服务调用", 403)
        body = await self._read_json(request)
        return self._json({"ok": True, "results": await self._kiosk_reload(body.get("sections") or [], body)})

    async def _kiosk_reload(self, raw_secs, body) -> dict:
        sections = [s for s in raw_secs if s in ("LLM", "TTS", "ASR", "Intent")]
        merged = await self._blocking(self._merged)
        self._push_config("asr_corrections", merged.get("asr_corrections") or {})
        self._push_config("asr_hotwords", merged.get("asr_hotwords") or [])
        self._push_config("chat_history", merged.get("chat_history") or {})
        self._push_config("call_names", merged.get("call_names") or {})
        for k in (
            "prompt", "tts_audio_prebuffer_count", "tts_timeout", "tool_call_timeout", "exit_commands",
            "close_connection_no_voice_time", "end_prompt", "delete_audio",
            "wakeup_words", "enable_greeting", "enable_wakeup_words_response_cache",
            "enable_stop_tts_notify", "enable_websocket_ping", "tts_audio_send_delay",
            "system_error_response", "plugins", "admin", "VAD", "Intent",
        ):
            if k in merged:
                self._push_config(k, merged[k])
        if "LLM" in raw_secs:
            to_retire = []
            new_llms = merged.get("LLM") or {}
            with llm_router._pool_lock:
                for p_name, (p_cfg, p_inst) in list(llm_router._pool.items()):
                    if p_cfg != new_llms.get(p_name):
                        llm_router._pool.pop(p_name, None)
                        to_retire.append(p_inst)
            for p_inst in to_retire:
                self._retire(p_inst, None)
        if "exit_commands" in merged:
            ex_cmds = merged.get("exit_commands") or []
            for item in connections.snapshot():
                conn = connections.get(item["session_id"])
                if conn is not None and hasattr(conn, "cmd_exit"):
                    conn.cmd_exit = list(ex_cmds) if isinstance(ex_cmds, list) else ex_cmds
        if "chat_history" in merged:
            ch_cfg = chat_history.settings(merged)
            win = (int(ch_cfg["max_messages"]), int(ch_cfg["max_tokens"]))
            eff_win = win if ch_cfg["enabled"] else None
            for item in connections.snapshot():
                conn = connections.get(item["session_id"])
                if conn is None:
                    continue
                cur_m = item.get("model") or (merged.get("selected_module") or {}).get("LLM") or ""
                d_main = getattr(conn, "dialogue", None)
                if d_main is not None:
                    d_main.window = eff_win
                    if ch_cfg["enabled"] and getattr(d_main, "chat_key", None) is None and cur_m:
                        await self._blocking(chat_history.load, conn, d_main, cur_m)
                hr = getattr(conn, "hub_route", None) or {}
                for m_name, d_sub in (hr.get("dialogues") or {}).items():
                    if d_sub is not None:
                        d_sub.window = eff_win
                        if ch_cfg["enabled"] and getattr(d_sub, "chat_key", None) is None:
                            await self._blocking(chat_history.load, conn, d_sub, m_name)
        if "close_connection_no_voice_time" in merged:
            try:
                idle_sec = int(merged.get("close_connection_no_voice_time") or 120)
                for item in connections.snapshot():
                    conn = connections.get(item["session_id"])
                    if conn is not None and hasattr(conn, "timeout_seconds"):
                        conn.timeout_seconds = idle_sec + 60
            except (TypeError, ValueError):
                pass
        if "call_names" in raw_secs and "ASR" not in sections:
            if pages.describe_hotwords(merged).get("supported"):
                sections.append("ASR")
        results = {}
        if "VAD" in raw_secs:
            vad_name = (merged.get("selected_module") or {}).get("VAD") or "SileroVAD"
            vad_cfg = ((merged.get("VAD") or {}).get(vad_name)) or {}
            vad_ms = vad_cfg.get(pages.VAD_SILENCE_FIELD)
            vad_thr = vad_cfg.get("threshold")
            vad_thr_low = vad_cfg.get("threshold_low")
            instances = [getattr(self.ws_server, "_vad", None)]
            for item in connections.snapshot():
                conn = connections.get(item["session_id"])
                if conn is not None:
                    instances.append(getattr(conn, "vad", None))
                    if isinstance(vad_ms, int) and 200 <= vad_ms <= 5000:
                        asr = getattr(conn, "asr", None)
                        if getattr(asr, "interface_type", None) is not None and hasattr(asr, "end_window_size"):
                            asr.end_window_size = max(200, vad_ms)
            for vad in instances:
                if vad is None:
                    continue
                if isinstance(vad_ms, int) and 200 <= vad_ms <= 5000 and hasattr(vad, "silence_threshold_ms"):
                    vad.silence_threshold_ms = vad_ms
                try:
                    if vad_thr is not None and hasattr(vad, "vad_threshold"):
                        fv = float(vad_thr)
                        if 0.05 <= fv <= 0.95:
                            vad.vad_threshold = fv
                    if vad_thr_low is not None and hasattr(vad, "vad_threshold_low"):
                        fl = float(vad_thr_low)
                        if 0.02 <= fl <= 0.90:
                            vad.vad_threshold_low = fl
                    if hasattr(vad, "vad_threshold") and hasattr(vad, "vad_threshold_low") and vad.vad_threshold_low >= vad.vad_threshold:
                        vad.vad_threshold_low = max(0.02, round(vad.vad_threshold - 0.15, 2))
                except (TypeError, ValueError):
                    pass
            results["VAD"] = {"applied": True}
        for sec in sections:
            results[sec] = await self._apply(sec)
        if "Intent" in raw_secs or "plugins" in raw_secs or "Intent" in sections:
            for item in connections.snapshot():
                conn = connections.get(item["session_id"])
                fh = getattr(conn, "func_handler", None) if conn is not None else None
                if fh is not None and callable(getattr(fh, "upload_functions_desc", None)):
                    fh.upload_functions_desc()
        if "TTS" in sections:
            await self._blocking(self._clear_voice_caches, True)
        if "prompt" in merged:
            from core.utils.cache.manager import cache_manager, CacheType
            cache_manager.clear(CacheType.DEVICE_PROMPT)
        if body.get("reset_device_llm"):
            default_llm = (merged.get("selected_module") or {}).get("LLM")
            new_prompt = merged.get("prompt")
            st = llm_router.store()

            def _clear_all_device_llm():
                with st._lock:
                    data = st._read_all()
                    if data.get(st.namespace):
                        data[st.namespace] = {}
                        st._write_all(data)

            await self._blocking(_clear_all_device_llm)
            for item in connections.snapshot():
                conn = connections.get(item["session_id"])
                if conn is not None and default_llm:
                    try:
                        await self._blocking(llm_router.switch, conn, default_llm, remember=False)
                        ws_llm = getattr(self.ws_server, "_llm", None)
                        if ws_llm is not None and conn.llm is not ws_llm:
                            llm_router._attach(conn, ws_llm)
                            conn.llm = ws_llm
                        if isinstance(new_prompt, str) and new_prompt.strip() and getattr(conn, "dialogue", None):
                            conn.config["prompt"] = new_prompt
                            await self._blocking(conn._init_prompt_enhancement)
                    except Exception as e:
                        self.logger.bind(tag=TAG).warning(f"热同步在线连接默认模型失败: {e}")
        elif isinstance(merged.get("prompt"), str) and merged["prompt"].strip():
            new_prompt = merged["prompt"]
            for item in connections.snapshot():
                conn = connections.get(item["session_id"])
                if conn is not None and getattr(conn, "dialogue", None):
                    try:
                        conn.config["prompt"] = new_prompt
                        await self._blocking(conn._init_prompt_enhancement)
                    except Exception as e:
                        self.logger.bind(tag=TAG).warning(f"热更新在线连接系统提示词失败: {e}")
        if body.get("recycle_idle"):
            for item in connections.snapshot():
                conn = connections.get(item["session_id"])
                ws = getattr(conn, "websocket", None) if conn is not None else None
                if ws is not None and not getattr(conn, "client_is_speaking", False) and not getattr(conn, "client_have_voice", False):
                    try:
                        await ws.send(json.dumps({"type": "server", "action": "reconnect_idle"}))
                    except Exception:
                        pass
        self.logger.bind(tag=TAG).info(f"本机内部触发配置热加载: sections={sections or 'dict-only'}")
        return results

    async def handle_kiosk_internal_voice(self, request: web.Request):
        if (
            not self._from_this_machine(request)
            or request.headers.get("X-TS-Internal-Reload") != "1"
            or "Origin" in request.headers
        ):
            return self._error("仅限本机内部服务调用", 403)
        merged = await self._blocking(self._merged)
        return self._json({"ok": True, **kiosk_voice.describe(merged)})

    async def handle_kiosk_internal_voice_save(self, request: web.Request):
        if (
            not self._from_this_machine(request)
            or request.headers.get("X-TS-Internal-Reload") != "1"
            or "Origin" in request.headers
        ):
            return self._error("仅限本机内部服务调用", 403)
        body = await self._read_json(request)
        try:
            merged = await self._blocking(self._merged)
            result = await self._blocking(kiosk_voice.save, body.get("values"), merged, self.config_path)
        except ValueError as e:
            return self._error(str(e), 400)
        note = "没有修改"
        if result["changed"]:
            applied = await self._kiosk_reload(result["sections"], {"recycle_idle": True})
            if result["tts"]:
                await self._blocking(self._clear_voice_caches)
            note = next((r["note"] for r in applied.values() if isinstance(r, dict) and r.get("failed")), "")
            self.logger.bind(tag=TAG).info(f"大屏设置更新了语音参数: {', '.join(result['changed'])}")
        merged = await self._blocking(self._merged)
        return self._json({"ok": True, "changed": result["changed"], "note": note, **kiosk_voice.describe(merged)})

    async def handle_kiosk_internal_memory(self, request: web.Request):
        if (
            not self._from_this_machine(request)
            or request.headers.get("X-TS-Internal-Reload") != "1"
            or "Origin" in request.headers
        ):
            return self._error("仅限本机内部服务调用", 403)
        merged = await self._blocking(self._merged)
        devices = await self._blocking(chat_history.describe_memories, merged)
        return self._json({"ok": True, "devices": devices})

    async def handle_kiosk_internal_memory_save(self, request: web.Request):
        if (
            not self._from_this_machine(request)
            or request.headers.get("X-TS-Internal-Reload") != "1"
            or "Origin" in request.headers
        ):
            return self._error("仅限本机内部服务调用", 403)
        body = await self._read_json(request)
        device = str(body.get("device") or "")
        summary = body.get("summary")
        if not device or not isinstance(summary, str):
            return self._error("缺少 device 或 summary", 400)
        if len(summary) > 5000:
            return self._error("长期记忆最多 5000 字", 400)
        version = body.get("version")
        ok, memory = await self._blocking(
            chat_history.set_memory,
            device,
            summary,
            int(version) if isinstance(version, int) and not isinstance(version, bool) else None,
        )
        if not ok:
            return self._json({"ok": False, "error": "编辑期间小智自动更新了记忆，请刷新查看最新内容后再改", "memory": memory}, 409)
        return self._json({"ok": True, "memory": memory})

    async def handle_kiosk_internal_diag(self, request: web.Request):
        if (
            not self._from_this_machine(request)
            or request.headers.get("X-TS-Internal-Reload") != "1"
            or "Origin" in request.headers
        ):
            return self._error("仅限本机内部服务调用", 403)
        from core.agent import asr_text
        from core.agent.call_names import learner

        merged_d = await self._blocking(self._merged)
        st_d = await self._blocking(collect_status, self.config, merged_d)
        ver = await self._blocking(self.updater.version)
        upd_st = await self._blocking(self.updater.status)
        return self._json({
            "ok": True,
            "asr_recent": asr_text.recent(),
            "call_learning": learner.status(),
            "hub_version": ver.get("current") or {},
            "hub_check": None,
            "hub_update": upd_st,
            "hub_checks": st_d.get("checks") or [],
            "active_sessions": len(connections.snapshot()),
        })

    def _clear_voice_caches(self, keep_wakeup_yaml: bool = False) -> None:
        from core.agent import call_names
        from core.utils import tts_phrase_cache

        call_names.clear_cache()
        tts_phrase_cache.clear()
        cdir = call_names._cache_dir
        if cdir.is_dir():
            for fp in cdir.glob("*.opus"):
                fp.unlink(missing_ok=True)
        data_d = Path(self.config_path).parent if self.config_path else (updater.SERVER_DIR / "data")
        ww_y = data_d / ".wakeup_words.yaml"
        if not keep_wakeup_yaml and ww_y.is_file():
            ww_y.unlink(missing_ok=True)
        ww_assets = data_d.parent / "config" / "assets" / "wakeup_words"
        if ww_assets.is_dir():
            for fp in ww_assets.glob("*.wav"):
                fp.unlink(missing_ok=True)

    async def handle_kiosk_internal_diag_post(self, request: web.Request):
        if (
            not self._from_this_machine(request)
            or request.headers.get("X-TS-Internal-Reload") != "1"
            or "Origin" in request.headers
        ):
            return self._error("仅限本机内部服务调用", 403)
        from core.agent.call_names import learner

        body = await self._read_json(request)
        act = str(body.get("action") or "").strip()
        if act == "learn_start":
            target = str(body.get("target") or "").strip()[:64] or None
            learner.start(session_id=None, count=3, timeout=90, target=target)
            return self._json({"ok": True, "call_learning": learner.status()})
        if act == "learn_stop":
            learner.stop()
            return self._json({"ok": True, "call_learning": learner.status()})
        if act == "clear_tts_cache":
            await self._blocking(self._clear_voice_caches, bool(body.get("keep_wakeup_yaml")))
            return self._json({"ok": True, "note": "已清理唤醒词与呼名预合成音频缓存"})
        if act == "preview_prompt":
            import re as _re

            def _build_pv():
                merged_p = self._merged()
                p_ov = body.get("prompt")
                if isinstance(p_ov, str):
                    merged_p = {**merged_p, "prompt": p_ov[:8000]}
                desc_p = pipeline.describe_prompt(merged_p)
                segs = desc_p.get("segments") or []
                base_seg = next((s for s in segs if s.get("id") == "base"), None) or {}
                tools_seg = next((s for s in segs if s.get("id") == "tools"), None) or {}
                raw_txt = str(base_seg.get("text") or "")
                clean_txt = _re.sub(r"\x01[^\x02]*\x02([^\x03]*)\x03", r"\1", raw_txt)
                return {
                    "template": pipeline._template_name(merged_p),
                    "rendered": clean_txt,
                    "tools": str(tools_seg.get("text") or ""),
                }

            pv = await self._blocking(_build_pv)
            return self._json({"ok": True, **pv})

        if act == "restart_hub":
            if connections.snapshot() and not body.get("force"):
                return self._error("当前有活跃语音对话连接，请等对话结束后再重启网关", 409)

            def _schedule_hub_restart():
                import shutil as _sh
                import signal as _sig
                import subprocess as _sp
                if sys.platform.startswith("linux") and os.environ.get("INVOCATION_ID") and _sh.which("systemd-run"):
                    _sp.Popen(
                        ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                         "bash", "-c", "sleep 0.25 && systemctl --user restart xiaozhi-hub.service"],
                        stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, start_new_session=True,
                    )
                else:
                    os.kill(os.getpid(), _sig.SIGTERM)

            asyncio.get_running_loop().call_later(0.15, _schedule_hub_restart)
            return self._json({"ok": True, "note": "小智语音网关正在平滑重启"})
        return self._error("不支持的诊断动作", 400)
