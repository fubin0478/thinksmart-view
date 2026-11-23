alsa_monitor = alsa_monitor or {}
alsa_monitor.rules = alsa_monitor.rules or {}

-- Select UCM HiFi directly at card initialization so ACP never auto-probes pro-audio (hw:0,4).
table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "device.name", "equals", "alsa_card.platform-c051000.sound-card" },
    },
  },
  apply_properties = {
    ["api.acp.auto-profile"] = false,
    ["device.profile"] = "HiFi",
  },
})
