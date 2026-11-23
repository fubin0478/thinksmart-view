if libcamera_monitor then
  libcamera_monitor.enabled = false
end

-- Single-user embedded kiosk: disable D-Bus ReserveDevice1 acquisition to avoid
-- wp_reserve_device_own_name assertion and duplicate ALSA monitor teardown at boot.
alsa_monitor.properties = alsa_monitor.properties or {}
alsa_monitor.properties["alsa.reserve"] = false
alsa_monitor.rules = alsa_monitor.rules or {}

-- Enable ACP+UCM for CD-18781Y now that TAS5782M I2C is working (2026-05-19)
table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "device.name", "equals", "alsa_card.platform-c051000.sound-card" },
    },
  },
  apply_properties = {
    ["api.alsa.use-acp"] = true,
    ["api.alsa.use-ucm"] = true,
    ["api.acp.auto-profile"] = true,
    ["api.acp.auto-port"] = true,
    ["api.alsa.disable-mmap"] = true,
  },
})

-- Disable non-speaker outputs (MultiMedia3, VoiceMMode, and ACP pro-audio hw:0,2 / hw:0,4)
table.insert(alsa_monitor.rules, {
  matches = {
    { { "node.name", "equals", "alsa_output.platform-c051000.sound-card.playback.2.0" } },
    { { "node.name", "equals", "alsa_output.platform-c051000.sound-card.playback.4.0" } },
    { { "node.name", "equals", "alsa_output.platform-c051000.sound-card.pro-output-2" } },
    { { "node.name", "equals", "alsa_output.platform-c051000.sound-card.pro-output-4" } },
    { { "node.name", "equals", "alsa_input.platform-c051000.sound-card.capture.4.0" } },
    { { "node.name", "matches", "alsa_input.platform-c051000.sound-card.pro-input-*" } },
  },
  apply_properties = {
    ["node.disabled"] = true,
  },
})

-- Conservative params for msm8953 ALSA backend
table.insert(alsa_monitor.rules, {
  matches = {
    { { "node.name", "matches", "alsa_output.*" } },
    { { "node.name", "matches", "alsa_input.*" } },
  },
  apply_properties = {
    ["api.alsa.disable-mmap"] = true,
    ["api.alsa.period-size"] = 1024,
    ["api.alsa.headroom"] = 8192,
  },
})
