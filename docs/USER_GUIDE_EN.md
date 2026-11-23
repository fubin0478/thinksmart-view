# Post-Install Configuration & LLM Selection Guide

**[🇨🇳 简体中文](USER_GUIDE.md) | [🇺🇸 English](USER_GUIDE_EN.md)** · **[🏠 Back to English Overview](../README_EN.md)**

Once `sudo bash device/install.sh` completes, hardware drivers, the smart desktop UI, **Unified Web Admin Console (`/admin/`)**, **AI Interactive Blackboard Tutor (`/tutor/`)**, 76 radio/audiobook streams, sleep soundscapes, calendar/countdowns, LAN video intercom, AirPlay, and Bluetooth audio are all ready.

This guide explains **Admin Console credentials & LAN/WAN security rules**, **AI Tutor dual input modes & hardware setup (Portrait + Powered USB Hub)**, and **recommended API keys & LLM pairings (Cloud vs. Local Apple Silicon)**.

---

## 1. Quick Overview: Required vs. Zero-Config Out of the Box

| Category | Feature Module | What You Need | Required? | Cost Reference |
| --- | --- | --- | --- | --- |
| **Core Required** | **XiaoZhi Voice Chat, Screen Control & AI Tutor Fallback** | Any cloud LLM API key (DeepSeek / Qwen / Zhipu GLM / Doubao / OpenAI) | **Yes (only item strictly needed)** | Zhipu `glm-4-flash` is **free**; typical home usage is **~1–5 RMB/month** |
| **Recommended for Tutoring** | **AI Blackboard Tutor (Phone Photo / Doc-Cam Co-Write)** | Vision (VL) + Reasoning 3-tier model keys (**inherits XiaoZhi's LLM by default if unset**); **externally powered USB Hub** for Doc-Cam/USB drives | Strongly recommended for photo homework | `qwen-vl-plus` / `glm-4v-flash` (free tier available) |
| **Recommended** | **Live Weather, 24h Curve, AQI & Auto Sunrise Light Theme** | **QWeather** `API Host` + `API Key` ([dev.qweather.com](https://dev.qweather.com/)) | Strongly recommended | Free personal developer quota covers 15-min polling |
| **Recommended** | **QQ Music Lossless FLAC & Liked Playlists** | Scan QR code directly on screen with mobile QQ / WeChat | Optional (free songs & 76 radio streams work without login) | Free / VIP for SQ FLAC |
| **Enhanced** | **Real-Time Web Search, Nearby Places & Driving Routes** | DeepSeek key + **Metaso Search key** + **AMap Web Service key** | Optional | High free daily quota on AMap & Metaso starter credits |
| **Advanced** | **AI Posture Companion (3-Frame Burst Focus Monitor)** | Always-on Apple Silicon Mac **or** cloud vision key (`qwen-vl-plus` / `glm-4v-flash`) | Optional | Free on local Mac or Zhipu `glm-4v-flash` |
| **Advanced** | **Nezha Server Probe Dashboard** | Self-hosted Nezha panel host + `servers.json` | Optional | Self-hosted |
| **Advanced** | **Remote Home View & WAN Admin Toggle** | Tailscale / ZeroTier mesh, or cloud VPS for reverse SSH tunnel | Optional (LAN works directly) | Free personal Tailscale |
| **Zero-Config** | **Admin Console (`admin/admin`), 76 Radio Streams, Noise Mixer, Calendar, Countdowns, Message Board, Pomodoro, AirPlay, Bluetooth, Offline Sherpa-ONNX ASR & EdgeTTS** | **No external accounts or API keys needed** | Out of the box | **$0** |

---

## 2. Unified Admin Console, AI Tutor Setup & Hardware Best Practices

### 1. Unified Web Admin Console (`http://<device-lan-ip>:18091/admin/`)

> [!IMPORTANT]
> **Initial Login & LAN/WAN Security Policy (Auto-Initialized on First Install; Upgrades Preserve Existing State)**
> - **Default Username**: `admin`
> - **Default Password**: `admin`
> - **LAN-Only Login by Default**: To prevent exposing device management to the public internet, `/admin/` permits only LAN subnets (`192.168.x.x` / `10.x.x.x` / VPN subnets in `ADMIN_VPN_NETS`) and loopback access by default.
> - **WAN Access OFF by Default (`外网:关` / `WAN: OFF`, returns HTTP `403`)**: Remote/tunnel access to `/admin/` is blocked until explicitly enabled. When remote configuration is needed, open `/admin/` from LAN (or VPN) and click the **`外网:关` button** in the top bar or bottom-left sidebar to toggle WAN access manually. Passwordless reverse-tunnel rules remain unchanged.

**First-Time Setup Workflow**:
1. From a phone or laptop on the same LAN, open **`http://<device-lan-ip>:18091/admin/`** and log in with **`admin` / `admin`**. Change the password right away from the top-right account menu or over SSH:
   ```bash
   printf 'admin:YourStrongPassword\n' | sudo /usr/local/sbin/thinksmart-set-admin-password
   ```
2. Navigate to **Settings → Large Models**:
   - Select your primary XiaoZhi conversation provider, enter your API key, and save.
3. Scroll down on the same page to **AI Blackboard Tutor (`AI 拍题黑板老师`)**:
   - Configure dedicated models (`Primary Fast`, `Deep Reasoning`, and `Backup`) and click **Test Tutor Model** (details below).
4. Navigate to **Settings → Voice**:
   - Keep built-in **`EdgeTTS`** (14 key-free neural voices) and **`Sherpa-ONNX`** (on-device offline bilingual ASR) if you do not run a local Mac voice server.
5. Navigate to **Settings → Weather & Search**:
   - Save QWeather and search keys, and manage countdowns, family messages, posture study mode, local music, and Clash nodes right from the web UI.

*(Note: Upstream XiaoZhi admin `:16688/admin` stays available as a fallback, while `:18091/admin/` is the unified control surface for everything.)*

---

### 2. AI Blackboard Tutor (`/tutor/`): Two Input Modes, Portrait Layout & Powered USB Hub

#### ① Two Ways to Submit Questions

| Input Mode | Entry & Workflow | Best For |
| --- | --- | --- |
| **Mode 1: Phone Photo Upload** | Open `http://<device-lan-ip>:18091/tutor/` on your phone (or scan the QR code on the device's Phone Photo tab), take a picture with 4-corner perspective crop and de-shadowing, and push it straight to the ThinkSmart View screen for animated blackboard teaching. | Single questions from printed papers, notebooks, or textbooks anywhere in the house. |
| **Mode 2: USB Document Camera + Silent Co-Write** | Point an overhead UVC Document Camera (`高拍仪`) at your desk notebook. Switch to the **Doc-Cam** tab on screen for **touch-drag cropping** of a single problem, or enable **Silent Co-Write** — silently checks completed problems a few seconds after pen pause with zero voice interruptions, pinning mistake alerts (`⚠️ Self-Check`) directly to exact steps. | Evening homework sessions requiring quiet, continuous checking & mistake tracking. |

#### ② Hardware Ergonomics & Power Requirements (Read Carefully)

1. **Portrait Orientation (`800×1280`) Strongly Recommended**:
   - Placing the ThinkSmart View upright clears the bottom Type-C port cleanly and gives a tall vertical canvas ideal for A4/B5 question stems, SVG geometry sketches, interactive choices, and Co-Write mistake lists.
   - The onboard `BMA253` accelerometer automatically rotates the display (`0°/90°/180°/270°`), and you can lock Portrait or Landscape from the top toolbar.
2. **Externally Powered USB Hub Required for Doc-Cam & USB Drives**:
   - The bottom Type-C port operates in OTG Host mode with tight current limits. Connecting a bus-powered UVC Document Camera (with LED fill light & autofocus motor) or high-draw USB flash drive via an unpowered adapter frequently causes brownouts, black frames, or disconnects.
   - **Always connect your Doc-Cam or USB flash drive through an externally powered (5V / PD) USB Hub.** Built-in `thinksmart-usb-host` and `thinksmart-usb-watchdog` continuously monitor USB host mode and self-heal unexpected UVC resets.

#### ③ Why AI Tutor Needs Separate LLMs (Even Though It Defaults to XiaoZhi's Model)

- **Default Inheritance**: If you leave AI Tutor models blank in Admin Settings, `thinksmart-tutor` automatically falls back to XiaoZhi's active LLM (`selected_module.LLM`) so text and built-in sample lessons work immediately.
- **Why Separate Models Matter**:
  - **XiaoZhi Voice Chat** prioritizes **sub-600ms Time-To-First-Token (`TTFT`)** and reliable tool calling (`deepseek-chat`, `qwen-flash`, `glm-4-flash`).
  - **AI Blackboard Tutor** requires **accurate handwritten/formula image OCR (Vision-Language)**, strict coordinate-based SVG geometry rendering, and rigorous multi-step math/science reasoning. Fast text-only chat models lack vision support or skip subtle geometry constraints!
- **Recommended 3-Tier Tutor Setup (`:18091/admin/` → Settings → Large Models → AI Tutor)**:
  1. **Primary Fast Model**: Balanced vision-language + structured output model (e.g., `qwen-vl-max` / `qwen-vl-plus`, `gpt-4o`, `gemini-2.5-flash`) for instant photo parsing and step-by-step SVG lessons.
  2. **Deep Reasoning Model**: Chain-of-thought reasoning model (e.g., `deepseek-reasoner`, `qwen3-max`) used on demand when tapping **Deep Explain** for competition/olympiad geometry or multi-variable problems.
  3. **Backup Model**: Independent provider endpoint for automatic failover on rate limits or timeouts.

---

### 3. QWeather API Setup (Weather Card & Sunrise/Sunset Auto-Theme)

1. Sign in at [QWeather Developer Console](https://console.qweather.com/), check your custom **API Host** (e.g., `abc123xyz.re.qweatherapi.com`), generate an **API Key**, and activate the free developer subscription.
2. Enter them in `:18091/admin/` → **Settings → Weather & Search**, or persist via systemd drop-in:
   ```bash
   sudo mkdir -p /etc/systemd/system/thinksmart-home.service.d
   sudo tee /etc/systemd/system/thinksmart-home.service.d/local.conf >/dev/null <<'EOF'
   [Service]
   Environment=QWEATHER_API_HOST=your-host.re.qweatherapi.com
   Environment=QWEATHER_API_KEY=your_qweather_api_key
   Environment=WEATHER_DEFAULT_CITY=北京
   Environment=WEATHER_EXTRA_CITIES=上海,广州,深圳
   EOF
   sudo systemctl daemon-reload && sudo systemctl restart thinksmart-home.service
   ```

---

### 4. Voice Web Search & Local Maps (Optional)

In `:18091/admin/` → **Settings → Weather & Search** (or via CLI below), configure DeepSeek search summarization + **Metaso Search (`metaso.cn`)** + **AMap Web Service (`lbs.amap.com`)**:
```bash
printf '%s\n%s\n' 'YOUR_METASO_KEY_OR_EMPTY' 'YOUR_AMAP_WEB_KEY_OR_EMPTY' | sudo /usr/local/sbin/thinksmart-set-search-keys
```

---

### 5. QQ Music QR Login & Powered USB Drive Playback (Optional)

Tap **Music** on screen → bottom-left **Account** → scan the QR code with mobile QQ or WeChat to sync VIP SQ FLAC streaming. For local FLAC collections on a USB flash drive, plug the drive into your **externally powered USB Hub**.

---

### 6. Server Probes & Safe Remote Access (Optional)

- **Nezha Probes**: Set `NEZHA_HOST` in `/etc/systemd/system/thinksmart-monitor.service.d/local.conf` and edit `/usr/local/share/thinksmart-monitor/servers.json` using [`servers.example.json`](../device/monitor/servers.example.json).
- **Remote Access**: Use **Tailscale / ZeroTier** for private LAN IP access (`:18091/admin/`, `:18091/tutor/`), or deploy [`thinksmart-intercom-tunnel.sh`](../device/intercom/thinksmart-intercom-tunnel.sh) against a cloud VPS. `/admin/` stays locked (`403`) over any WAN tunnel until you manually turn on the **WAN toggle button** inside the Admin Console.

---

## 3. Model Pairing Guide: Cloud-Only vs. Apple Silicon Companion

### Option A: Pure Cloud Setup (Recommended for 90% of Users — Zero Extra Hardware)

1. **XiaoZhi Voice Assistant (Prioritize Fast TTFT < 0.6s & Tool Calling)**:
   - `deepseek-chat` (DeepSeek), `qwen-flash` / `qwen-plus` (Alibaba Cloud DashScope), free `glm-4-flash` (Zhipu), or `doubao-seed-2-0-lite-260215` (Volcengine).
   - *Avoid setting slow reasoning models (e.g., `deepseek-reasoner`, `o1/o3`) as XiaoZhi's daily voice assistant—save those for AI Tutor's **Deep Reasoning** slot!*
2. **AI Blackboard Tutor (Prioritize Vision OCR + Geometry Reasoning in `:18091/admin/`)**:
   - **Primary Fast VL**: `qwen-vl-max`, `qwen-vl-plus`, `glm-4v-flash` (free), `gpt-4o`, or `gemini-2.5-flash`.
   - **Deep Reasoning**: `deepseek-reasoner` or `qwen3-max`.
3. **Built-In Free On-Device Speech (Ready Out of the Box)**:
   - **Offline ASR (`SherpaXASR`)**: Preinstalled `167 MB` INT8 Chinese/English punctuated Zipformer model + `SileroVAD` + wake word model running locally with zero API fees.
   - **Built-In `EdgeTTS`**: 14 neural voices usable across XiaoZhi chat, message board announcements, Pomodoro cues, and study reminders without API keys.
4. **Cloud Vision Posture Monitoring (No Mac Required)**:
   - Run stdlib [`vision_bridge.py`](../mac-companion/study-vision/vision_bridge.py) locally on the ThinkSmart View pointing to Zhipu `glm-4v-flash` or Qwen `qwen-vl-plus`.

### Option B: Always-On Apple Silicon Mac (Unified Memory Sizing)

| Mac Unified Memory | Recommended Components | Memory Profile & Notes |
| --- | --- | --- |
| **8 GB** | **`voice` primary process only** (`Qwen3-ASR-1.7B-8bit` + `Qwen3-TTS-1.7B-8bit`) | `~3.8–4.5 GB` resident; offload vision & Tutor reasoning to cloud APIs. |
| **16 GB (Sweet Spot)** | **`voice` + idle-unloading `tts2` + `Qwen3-VL-8B-Q4_K_M`** | `~4.5 GB` idle / `~10.5 GB` active study sessions (10-min auto sleep); cloud LLMs still recommended for chat & heavy math. |
| **24 GB–32 GB+** | **Full `mac-companion` + local `Ollama` / `LM Studio`** | Point custom LLM endpoints in `:18091/admin/` to `http://<Mac-LAN-IP>:11434/v1` for a 100% offline home setup. |
