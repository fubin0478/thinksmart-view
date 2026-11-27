# ThinkSmart View (CD-18781Y) Ubuntu 24.04 ARM64 硬件增强预编译组件

本目录存放针对 **Ubuntu 24.04 Noble (`aarch64-linux-gnu`) + Linux `6.19.5-msm8953`** 交叉编译的 5 个轻量硬件增强组件（由 `device/install.sh` 自动进行 SHA256 校验后部署）：

| 文件 | 部署路径 | 作用说明 | 开源许可说明 |
| --- | --- | --- | --- |
| `s5kc505a_cam_helper` | `/usr/local/bin/s5kc505a_cam_helper` | S5KC505A 5MP MIPI CSI-2 传感器零常驻功耗 `15fps` 源头抽帧软 ISP 采集器（含 2×2 Bayer 解马赛克、AWB、AE、$\gamma=2.2$ 与静态链接 `libturbojpeg` SIMD 压缩，无观看者自动 `STREAMOFF` 休眠）。 | GPL-3.0-or-later（内置静态链接 `libjpeg-turbo` BSD-3-Clause） |
| `apq8053-lenovo-cd-18781y.dtb` | `/boot/apq8053-lenovo-cd-18781y.dtb` | 基于 `thinksmart-ubuntu-20261101.img` 原厂设备树（SHA256 `0136683c...`）打入 `apq8053-camera.dts.patch` 后的设备树，启用 `camss@1b00020`、`cci@1b0c000`、`24MHz mclk0` 及摄像头供电轨。`install.sh` 仅在校验原厂基准哈希一致时自动备份 `.bak` 并替换，杜绝跨内核版本误刷风险。 | GPL-2.0（同 Linux Kernel DTB） |
| `uxplay` | `/usr/local/bin/uxplay` | 基于上游 [FDH2/UxPlay](https://github.com/FDH2/UxPlay) `1.74` 编译，修复 Ubuntu 仓库旧版 `1.68` 对新版 iOS `SPS+PPS` 头花屏 Bug，定制 `WMM AC_VI` 无线队列优先级、`TCP_QUICKACK` 重武装、抗抖动单调时钟及 `48kHz F32LE` 重采样音频管线。 | GPL-3.0-or-later（遵循上游 UxPlay 许可） |
| `libuxplay_autorotate.so` | `/usr/local/lib/libuxplay_autorotate.so` | 通过 `LD_PRELOAD` 注入 `uxplay` 的 GStreamer 视口插件：投屏期间读取机身 `BMA253` 重力传感器，用 ARM64 NEON `8×8` SIMD 转置实现零软件缩放自动横竖屏切换（空闲期零 I2C 轮询）。 | GPL-3.0-or-later |
| `libspa-codec-bluez5-aac.so` | `/usr/lib/aarch64-linux-gnu/spa-0.2/bluez5/libspa-codec-bluez5-aac.so` | 补齐 Ubuntu 24.04 默认未随包分发的 PipeWire `0.3/1.0` BlueZ5 Fraunhofer FDK-AAC 蓝牙编解码插件（运行依赖 `libfdk-aac2`），使 iPhone 蓝牙连接支持原生高清 `AAC`。 | MIT（PipeWire SPA 接口）+ Fraunhofer FDK-AAC License |

> **版本升级提示**：若后续手动升级系统的 `pipewire` 或 `gstreamer1.0` 主版本，建议重新编译对应 ABI 的 `libspa-codec-bluez5-aac.so` 与 `uxplay`。
