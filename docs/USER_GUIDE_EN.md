# Post-Install Configuration & LLM Selection Guide

**[🇨🇳 简体中文](USER_GUIDE.md) | [🇺🇸 English](USER_GUIDE_EN.md)** · **[🏠 Back to English Overview](../README_EN.md)**

Once `sudo bash device/install.sh --apply && sudo reboot` completes, hardware drivers, the unified desktop home, **Top Dynamic Voice Capsule & Rich Cards**, **9-Section Touch Settings**, **Unified Web Admin Console (`/admin/`)**, **AI Blackboard Tutor (`/tutor/`)**, 76 radio/audiobook streams, sleep soundscapes, statutory-holiday calendar, LAN video intercom, AirPlay, and Bluetooth speaker mode are all resident and ready.

---

## 1. Required vs. Out-of-the-Box Features

| Category | Feature Module | What You Need | Required? | Cost Reference |
| --- | --- | --- | --- | --- |
| **Core Required** | **XiaoZhi Voice Assistant (`你好小智`), Screen Navigation & AI Tutor Fallback** | One cloud LLM API key (DeepSeek / Qwen / Zhipu GLM / Doubao / OpenAI-compatible) | **Yes (only required item)** | Zhipu `glm-4-flash` is **free**; typical home use is ~$0.2–$1/month |
| **Recommended for Tutor** | **AI Blackboard Tutor (Phone Photo / Doc-Cam Silent Co-Write / Dictation)** | 3-tier Vision (VL) + Reasoning LLM keys (**inherits XiaoZhi's LLM if left blank**) + **externally powered 5V/PD USB Hub** for USB Doc-Cam/drives | Strongly recommended for photo homework | `qwen-vl-plus` / free `glm-4v-flash` / `gpt-4o` |
| **Recommended** | **Live Weather, 24h Curve, AQI & Sunrise/Sunset Auto-Theme** | **QWeather** `API Host` + `API Key` ([dev.qweather.com](https://dev.qweather.com/)) | Recommended | Free personal developer tier |
| **Optional** | **QQ Music SQ FLAC & Playlists** | Scan QR code on the Music screen with mobile QQ / WeChat (auto-renews session) | Optional (76 radio streams work without login) | Free / VIP for SQ FLAC |
| **Zero-Config** | **9-Section Touch Settings (Wi-Fi / AirPlay / Pinyin City Search), Admin Console (`admin/admin`), Full Backup/Restore, 76 Radio Streams, Noise Mixer, Offline Sherpa-ONNX ASR & 14 EdgeTTS Voices** | **No external accounts or keys required** | Ready out of the box | **$0** |

---

## 2. On-Screen 9-Section Settings & Unified Web Admin

1. **On-Screen 9-Section Settings** (tap **Settings** on the bottom Dock or say *"你好小智，打开设置"*):
   - **Display & Sound**: Responsive touch sliders for brightness (with ALS auto-dimming) and volume, theme/orientation controls, plus instant toggles for **AirPlay Mirroring** and **Bluetooth Speaker** mode.
   - **Network**: Scan and join Wi-Fi networks directly on screen, or scan the QR code to open the Web Admin Console on your phone.
   - **XiaoZhi & Chat**: Configure wake words, sensitivity, follow-up window, active LLM, and 14 key-free EdgeTTS voices.
   - **Weather / Music / Study / System**: Search any Chinese city/county by **Pinyin (e.g., `beijing`, `suzhou`) or Chinese name** for home weather and up to 8 favorite cities; adjust audio quality; inspect CPU temperature, RAM, and disk stats, or safely reboot/power off.
2. **Unified Web Admin Console (`http://<device-lan-ip>:18091/admin/`)**:
   - **Default Login**: **`admin` / `admin`** (LAN-only by default; WAN access defaults to **`外网:关` / `WAN: OFF`** returning HTTP `403` until manually toggled).
   - **One-Click System Backup & Migration**: Export a **Sanitized Backup** (secrets stripped) or **Full Migration Backup** (including hot-backed `chat.db`, sessions, alarms, cities, and keys) and restore onto any unit with SHA256 verification.

---

## 3. AI Blackboard Tutor & LLM Pairing Tips

- **Hardware Setup**: Stand the device upright in **Portrait (`800×1280`)** orientation and always connect USB Document Cameras or USB flash drives through an **externally powered 5V/PD USB Hub**.
- **Voice Chat LLM (`< 0.6s` TTFT)**: Use fast tool-capable models (`deepseek-chat`, `qwen-flash`, `glm-4-flash`, `gpt-4o-mini`)—avoid setting slow reasoning models as the daily voice assistant.
- **AI Tutor 3-Tier Models**: Pair a Vision-Language model (`qwen-vl-max` / `qwen-vl-plus` / `gpt-4o`) as **Primary Fast**, a reasoning model (`deepseek-reasoner` / `qwen3-max`) as **Deep Reasoning**, and an independent provider as **Backup**.
- **Optional Apple Silicon Companion (`mac-companion/`)**: Run `bash mac-companion/install-mac.sh` on an M-series Mac for local `Qwen3-ASR` + `Qwen3-TTS` streaming voice cloning and `Qwen3.5-9B` E2EE study-vision inference.
