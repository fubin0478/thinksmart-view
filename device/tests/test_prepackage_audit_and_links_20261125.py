#!/opt/thinksmart-addons/venv/bin/python
"""Pre-package audit, whole-system backup/export+restore, installer check, and settings cross-links suite (2026-11-25)."""
import importlib.util
import io
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
sys.dont_write_bytecode = True

HUB_SRV = pathlib.Path("/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server")
HUB_PY = str(HUB_SRV / ".venv/bin/python")
CLI_ROOT = pathlib.Path("/opt/thinksmart/client/current")
ADMIN_PY = pathlib.Path("/usr/local/bin/thinksmart-admin.py")
ADMIN_HTML = pathlib.Path("/usr/local/share/thinksmart-admin/index.html")
BACKUP_CLI = pathlib.Path("/usr/local/sbin/thinksmart-backup")
HOME_HTML = pathlib.Path("/usr/local/share/thinksmart-home/index.html")
OVERLAY_JS = pathlib.Path("/usr/local/share/thinksmart-monitor/home_overlay.js")


def _load_mod(name: str, path: str, extra_env: dict | None = None):
    if extra_env:
        with mock.patch.dict(os.environ, extra_env):
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPrepackageAuditAndLinks(unittest.IsolatedAsyncioTestCase):
    def test_01_hub_standalone_admin_api_disabled_and_config_pruned(self):
        probe = r"""
import asyncio, json, sys
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
sys.path.insert(0, '/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server')
from core.admin import register_admin_routes

async def run():
    app = web.Application()
    register_admin_routes(app, {'selected_module': {'LLM': 'DeepSeekLLM'}, 'LLM': {}})
    async with TestClient(TestServer(app)) as cl:
        r_idx = await cl.get('/admin/')
        assert r_idx.status == 200 and '18091/admin/' in await r_idx.text()
        r_api = await cl.get('/admin/api/state')
        assert r_api.status == 410, r_api.status
        j_api = await r_api.json()
        assert j_api.get('ok') is False and ':18091/admin/' in j_api.get('error', '')
        r_post = await cl.post('/admin/api/setup', json={'password': 'x'})
        assert r_post.status == 410
        r_llm = await cl.get('/kiosk/llm?device=probe')
        assert r_llm.status == 200
    print('HUB_ADMIN_LOCKDOWN_OK')

asyncio.run(run())
"""
        r = subprocess.run([HUB_PY, "-B", "-c", probe], cwd="/tmp", capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, f"{r.stderr}\n{r.stdout}")
        self.assertIn("HUB_ADMIN_LOCKDOWN_OK", r.stdout)

        cfg_txt = (HUB_SRV / "config.yaml").read_text(encoding="utf-8")
        for pruned_tok in ("mem0ai", "powermem", "GeminiLLM", "DifyLLM", "CozeLLM", "PaddleSpeechTTS", "FishSpeech", "机车"):
            self.assertNotIn(pruned_tok, cfg_txt)
        for keep_tok in ("EdgeTTS", "SherpaXASR", "DeepSeekLLM", "OpenAILLM", "小麦小麦"):
            self.assertIn(keep_tok, cfg_txt)

        mcp_j = json.loads((HUB_SRV / "mcp_server_settings.json").read_text(encoding="utf-8"))
        self.assertEqual(mcp_j, {"mcpServers": {}})

        self.assertFalse((CLI_ROOT / "web/logos/grok.svg").exists())
        self.assertFalse((HUB_SRV / "config/assets/wakeup_words/508d2e738622efe3bada2cc2fa99004b.wav").exists())

    async def test_02_admin_backup_export_sanitization_validation_and_restore(self):
        from aiohttp.test_utils import TestClient, TestServer

        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            root = pathlib.Path(td)
            adm_d = root / "admin"
            hub_d = root / "hub"
            (hub_d / "data").mkdir(parents=True)
            (hub_d / "config.yaml").write_text("selected_module:\n  LLM: DeepSeekLLM\n", encoding="utf-8")
            (hub_d / "data" / ".config.yaml").write_text(
                '{"selected_module": {"LLM": "DeepSeekLLM"}, "LLM": {"DeepSeekLLM": {"base_url": "https://api.deepseek.com", "model_name": "deepseek-v4-flash", "api_key": "sk-secret-abc-123"}}}',
                encoding="utf-8",
            )
            db_p = hub_d / "data" / "chat.db"
            conn = sqlite3.connect(str(db_p))
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, msg TEXT)")
            conn.execute("INSERT INTO t (msg) VALUES ('hello backup')")
            conn.commit()
            conn.close()

            cli_cfg = root / "client" / "config.json"
            cli_cfg.parent.mkdir(parents=True)
            cli_cfg.write_text(json.dumps({"device_id": "dev-1", "wake_sound": "zai"}), encoding="utf-8")
            (cli_cfg.parent / "alarms.json").write_text(json.dumps({"tone": "coffee", "items": [{"id": 1}]}), encoding="utf-8")

            home_d = root / "home"
            home_d.mkdir(parents=True)
            (home_d / "config.json").write_text(json.dumps({"city": "杭州", "qweather_key": "qw-secret-999"}), encoding="utf-8")
            (home_d / "countdowns.json").write_text(json.dumps([{"id": "c1", "title": "测试日"}]), encoding="utf-8")

            qq_d = root / "qqmusic"
            qq_d.mkdir(parents=True)
            (qq_d / "config.json").write_text(json.dumps({"quality_level": "lossless", "cookies_str": "qq_secret_str", "uin": "123456", "user_profile": {"nickname": "Tester"}}), encoding="utf-8")
            (qq_d / "session.json").write_text(json.dumps({"cookie": "qq-secret-cookie"}), encoding="utf-8")

            study_d = root / "study"
            study_d.mkdir(parents=True)
            (study_d / "config.json").write_text(json.dumps({"interval": 120, "enabled": False}), encoding="utf-8")
            (study_d / "secret").write_text("0" * 64, encoding="utf-8")

            tutor_d = root / "tutor"
            tutor_d.mkdir(parents=True)
            (tutor_d / "prefs.json").write_text(json.dumps({"grade": "八年级", "mobile_entry_url": "https://203.0.113.99:18443/tutor/"}), encoding="utf-8")
            (tutor_d / "mistake_book.json").write_text(json.dumps([{"id": "m1"}]), encoding="utf-8")

            mon_d = root / "monitor"
            mon_d.mkdir(parents=True)
            (mon_d / "display.json").write_text(json.dumps({"auto": True}), encoding="utf-8")

            adm_d.mkdir(parents=True)
            (adm_d / "auth.json").write_text(json.dumps({"username": "admin", "password_hash": "scrypt$1"}), encoding="utf-8")
            (home_d / "holidays_extra.json").write_text(json.dumps({"2027-01-01": "off"}), encoding="utf-8")
            (hub_d / "data" / ".mcp_server_settings.json").write_text(json.dumps({"mcpServers": {"s": {"token": "tok-sec"}}}), encoding="utf-8")
            (hub_d / "data" / "agent_sessions.json").write_text(json.dumps({"dev-1": "sess-1"}), encoding="utf-8")
            restore_bk = root / "restore-bk"
            env_ov = {
                "ADMIN_STATE": str(adm_d),
                "ADMIN_HUB_DIR": str(hub_d),
                "ADMIN_HUB_PY": "/nonexistent",
                "ADMIN_HOME_STATE": str(home_d),
                "ADMIN_QQ_CFG_DIR": str(qq_d),
                "ADMIN_STUDY_STATE": str(study_d),
                "ADMIN_TUTOR_STATE": str(tutor_d),
                "ADMIN_MONITOR_STATE": str(mon_d),
                "ADMIN_CLIENT_CFG": str(cli_cfg),
                "ADMIN_UPDATE_STATE": str(root / "upd"),
                "ADMIN_RESTORE_BACKUP_DIR": str(restore_bk),
            }
            adm = _load_mod("ts_adm_backup_test", str(ADMIN_PY), env_ov)
            app = adm.make_app(adm.Admin())
            hdr = {"X-TS-Peer": "127.0.0.1", "X-TS-Admin": "1"}

            async with TestClient(TestServer(app)) as cl:
                ri = await cl.get("/admin/api/backup/info", headers={"X-TS-Peer": "127.0.0.1"})
                self.assertEqual(ri.status, 200)
                ji = await ri.json()
                self.assertEqual(ji["format"], "thinksmart-backup-v1")
                self.assertGreaterEqual(ji["total_files"], 10)

                # 1. Sanitized export strips secrets & omits session/secret/db
                rs = await cl.get("/admin/api/backup/export?mode=sanitized", headers={"X-TS-Peer": "127.0.0.1"})
                self.assertEqual(rs.status, 200)
                san_bytes = await rs.read()
                with tarfile.open(fileobj=io.BytesIO(san_bytes), mode="r:gz") as tf:
                    names = tf.getnames()
                    self.assertIn("BACKUP_MANIFEST.json", names)
                    self.assertIn("hub/config.sanitized.yaml", names)
                    self.assertNotIn("qqmusic/session.json", names)
                    self.assertNotIn("study/secret", names)
                    hub_san = tf.extractfile("hub/config.sanitized.yaml").read().decode("utf-8")
                    self.assertNotIn("sk-secret-abc-123", hub_san)
                    self.assertIn("deepseek-v4-flash", hub_san)
                    home_san = tf.extractfile("home/config.json").read().decode("utf-8")
                    self.assertNotIn("qw-secret-999", home_san)
                    self.assertIn("杭州", home_san)
                    qq_san = tf.extractfile("qqmusic/config.json").read().decode("utf-8")
                    self.assertNotIn("qq_secret_str", qq_san)
                    self.assertNotIn("123456", qq_san)
                    tutor_san = tf.extractfile("tutor/prefs.json").read().decode("utf-8")
                    self.assertNotIn("203.0.113.99", tutor_san)

                # 2. Full export includes sqlite snapshot and credentials (but never sessions.json)
                rf = await cl.get("/admin/api/backup/export?mode=full", headers={"X-TS-Peer": "127.0.0.1"})
                self.assertEqual(rf.status, 200)
                full_bytes = await rf.read()
                with tarfile.open(fileobj=io.BytesIO(full_bytes), mode="r:gz") as tf:
                    fnames = tf.getnames()
                    self.assertIn("hub/chat.db", fnames)
                    self.assertIn("hub/.mcp_server_settings.json", fnames)
                    self.assertIn("hub/agent_sessions.json", fnames)
                    self.assertIn("home/holidays_extra.json", fnames)
                    self.assertIn("admin/auth.json", fnames)
                    self.assertIn("qqmusic/session.json", fnames)
                    self.assertIn("study/secret", fnames)
                    self.assertNotIn("admin/sessions.json", fnames)

                # 3. Reject malicious traversal archive & tampered checksum
                bad_buf = io.BytesIO()
                with tarfile.open(fileobj=bad_buf, mode="w:gz") as tf:
                    ti = tarfile.TarInfo("../evil.txt")
                    ti.size = 3
                    tf.addfile(ti, io.BytesIO(b"bad"))
                r_bad = await cl.post("/admin/api/backup/import", data=bad_buf.getvalue(), headers=hdr)
                self.assertEqual(r_bad.status, 400)

                tamper_buf = io.BytesIO()
                with tarfile.open(fileobj=io.BytesIO(san_bytes), mode="r:gz") as src_tf, tarfile.open(fileobj=tamper_buf, mode="w:gz") as dst_tf:
                    for m in src_tf.getmembers():
                        payload = src_tf.extractfile(m).read()
                        if m.name == "client/config.json":
                            payload = payload.replace(b'"zai"', b'"off"', 1)
                        m2 = tarfile.TarInfo(m.name)
                        m2.size = len(payload)
                        dst_tf.addfile(m2, io.BytesIO(payload))
                r_tmp = await cl.post("/admin/api/backup/import", data=tamper_buf.getvalue(), headers=hdr)
                self.assertEqual(r_tmp.status, 400)
                self.assertIn("SHA256", (await r_tmp.json())["error"])

                # 4. Mutate local state, restore sanitized archive -> restores values while preserving API key + creating pre-restore backup + 0644 on client/config.json
                cli_cfg.write_text(json.dumps({"device_id": "dev-1", "wake_sound": "off"}), encoding="utf-8")
                (home_d / "config.json").write_text(json.dumps({"city": "上海", "qweather_key": "qw-secret-999"}), encoding="utf-8")
                r_imp = await cl.post("/admin/api/backup/import", data=san_bytes, headers=hdr)
                self.assertEqual(r_imp.status, 200, await r_imp.text())
                ji_imp = await r_imp.json()
                self.assertTrue(ji_imp["ok"])
                self.assertEqual(json.loads(cli_cfg.read_text(encoding="utf-8"))["wake_sound"], "zai")
                self.assertEqual((cli_cfg.stat().st_mode & 0o777), 0o644)
                restored_home = json.loads((home_d / "config.json").read_text(encoding="utf-8"))
                self.assertEqual(restored_home["city"], "杭州")
                self.assertEqual(restored_home["qweather_key"], "qw-secret-999")
                restored_hub = json.loads((hub_d / "data/.config.yaml").read_text(encoding="utf-8")) if (hub_d / "data/.config.yaml").read_text(encoding="utf-8").lstrip().startswith("{") else adm._load_yaml_file(hub_d / "data/.config.yaml")
                self.assertEqual(restored_hub["LLM"]["DeepSeekLLM"]["api_key"], "sk-secret-abc-123")
                self.assertTrue(any(restore_bk.iterdir()))

            # 5. Verify thinksmart-backup CLI works end-to-end against isolated env
            out_tgz = root / "cli-export.tar.gz"
            cli_env = {**os.environ, **env_ov, "THINKSMART_ADMIN_PY": str(ADMIN_PY)}
            p_exp = subprocess.run([str(BACKUP_CLI), "export", "--mode", "full", "--out", str(out_tgz)], env=cli_env, capture_output=True, text=True, timeout=20)
            self.assertEqual(p_exp.returncode, 0, p_exp.stderr)
            self.assertTrue(out_tgz.is_file())
            p_ins = subprocess.run([str(BACKUP_CLI), "inspect", str(out_tgz)], env=cli_env, capture_output=True, text=True, timeout=20)
            self.assertEqual(p_ins.returncode, 0, p_ins.stderr)
            self.assertEqual(json.loads(p_ins.stdout)["mode"], "full")

    def test_03_settings_cross_links_css_invariants_and_jsc_syntax(self):
        v2 = CLI_ROOT / "web/v2"
        cli_html = (v2 / "index.html").read_text(encoding="utf-8")
        cli_js = (v2 / "app.js").read_text(encoding="utf-8")
        set_js = (v2 / "settings.js").read_text(encoding="utf-8")
        ov_js = OVERLAY_JS.read_text(encoding="utf-8")
        home_html = HOME_HTML.read_text(encoding="utf-8")
        adm_html = ADMIN_HTML.read_text(encoding="utf-8")

        # Device 9-section cross-links & settings structure
        for tok in ('data-go="home"', 'id="airplayCard"', 'id="airplaySwitch"',
                    'id="netWifiList"', 'id="wxMainInp"', 'id="wxExtraInp"',
                    'id="openChatSecBtn"', 'id="openNetSecBtn"', 'id="openBackupHintBtn"'):
            self.assertIn(tok, cli_html)
        for fn in ("__tsGoHome", "__tsOpenHomeSleep", "__tsOpenHomeFocus", "__tsOpenHomeWeather"):
            self.assertIn(fn, ov_js)
        self.assertIn("d.openSleep", home_html)
        for tok in ('data-c="open-weather"', 'data-c="open-music"', 'data-c="open-focus"'):
            self.assertIn(tok, cli_html + set_js)

        # Admin cross-links & backup card
        for tok in ('id="btnJumpChatHist"', 'id="btnJumpBackupCard"', 'id="btnOpenTutorTab"', 'id="btnOpenCdTab"', 'id="btnOpenStudyTab"',
                    'id="btnOpenCallTab"', 'id="btnOpenSongsTab"', 'id="cfgBackupCard"',
                    'id="btnExportSanitized"', 'id="btnExportFull"', 'id="btnImportBackup"', 'id="backupFileInp"', 'chkKeepSecrets'):
            self.assertIn(tok, adm_html)

        # Default wake word is 「你好小智」 across client, hub, and unified admin
        cli_cfg_py = (CLI_ROOT / "thinksmart_client/config.py").read_text(encoding="utf-8")
        self.assertIn("'wake_words': ['n ǐ h ǎo x iǎo zh ì @你好小智']", cli_cfg_py)
        cli_asst_py = (CLI_ROOT / "thinksmart_client/assistant.py").read_text(encoding="utf-8")
        self.assertIn("('n ǐ h ǎo x iǎo zh ì @你好小智', '你好小智')", cli_asst_py)
        adm_py = ADMIN_PY.read_text(encoding="utf-8")
        self.assertIn("{'raw': 'n ǐ h ǎo x iǎo zh ì @你好小智', 'label': '你好小智（默认推荐）'}", adm_py)
        cfg_yml = (HUB_SRV / "config.yaml").read_text(encoding="utf-8")
        self.assertRegex(cfg_yml, r'wakeup_words:\s*\n\s*-\s*"?你好小智"?')

        # Admin CSS strict integer px & 400/700 font-weight check
        css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", adm_html, flags=re.S))
        for w in re.findall(r"font-weight:\s*(\d+)", css):
            self.assertIn(w, ("400", "700"))
        for px in re.findall(r"font(?:-size)?:\s*([\d.]+)px", css):
            self.assertNotIn(".", px)

        # JS syntax validation with WebKit jsc if available
        jsc = "/usr/bin/jsc"
        if os.path.isfile(jsc):
            for jf in (v2 / "app.js", v2 / "settings.js", OVERLAY_JS):
                r = subprocess.run([jsc, "-c", str(jf)], capture_output=True, text=True, timeout=10)
                self.assertEqual(r.returncode, 0, f"JS syntax error in {jf}: {r.stderr} {r.stdout}")
            for h_name, h_txt in (("admin", adm_html), ("home", home_html)):
                blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", h_txt, flags=re.S)
                self.assertTrue(blocks)
                with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=True) as tf:
                    tf.write("\n;\n".join(blocks))
                    tf.flush()
                    r = subprocess.run([jsc, "-c", tf.name], capture_output=True, text=True, timeout=10)
                    self.assertEqual(r.returncode, 0, f"JS syntax error in {h_name}: {r.stderr} {r.stdout}")

    def test_04_synced_hardware_audio_and_systemd_configs_present_on_device(self):
        required_live_paths = [
            "/etc/systemd/user/idle-blanker.service",
            "/etc/systemd/user/vcnl4200-proximity-daemon.service",
            "/home/ubuntu/.config/systemd/user/tas5782m-preinit.service",
            "/etc/wireplumber/bluetooth.lua.d/60-thinksmart-speaker.lua",
            "/home/ubuntu/.config/wireplumber/main.lua.d/52-usb-doccam-no-mic.lua",
            "/home/ubuntu/.config/wireplumber/main.lua.d/99-zz-cd18781y-profile.lua",
            "/home/ubuntu/.config/wireplumber/bluetooth.lua.d/70-bt-hifi-direct.lua",
            "/home/ubuntu/.config/pipewire/pipewire.conf.d/10-thinksmart-rate.conf",
            "/home/ubuntu/.config/pipewire/pipewire.conf.d/20-hifi-audio.conf",
            "/home/ubuntu/.config/pipewire/client.conf.d/20-hifi-audio.conf",
            "/home/ubuntu/.config/pipewire/pipewire-pulse.conf.d/20-hifi-audio.conf",
            "/etc/systemd/system/bluetooth.service.d/no-sap.conf",
            "/etc/systemd/system/NetworkManager-wait-online.service.d/thinksmart.conf",
            "/etc/systemd/system/mihomo.service.d/hardening.conf",
            "/usr/local/bin/thinksmart-intercom-tunnel.sh",
            "/usr/local/bin/thinksmart-intercom-tunnel-jp.sh",
            "/opt/thinksmart-addons/vendor/lunar_python/__init__.py",
            "/usr/local/sbin/thinksmart-backup",
        ]
        for p in required_live_paths:
            if os.geteuid() != 0 and not os.access(os.path.dirname(p), os.X_OK):
                self.assertTrue(os.path.exists(os.path.dirname(p)), f"Missing live package dir: {os.path.dirname(p)}")
                continue
            self.assertTrue(os.path.exists(p), f"Missing live package path: {p}")


if __name__ == "__main__":
    unittest.main()
