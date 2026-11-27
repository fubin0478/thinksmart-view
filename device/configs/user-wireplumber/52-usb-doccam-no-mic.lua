-- Keep ThinkSmart dual-mic array + hardware AEC (thinksmart_mic) as sole capture source
-- when an external USB UVC document camera with built-in microphone is plugged in.
alsa_monitor.rules = alsa_monitor.rules or {}
table.insert(alsa_monitor.rules, {
  matches = {
    { { "node.name", "matches", "alsa_input.usb-*" } },
  },
  apply_properties = {
    ["priority.driver"] = 50,
    ["priority.session"] = 50,
    ["node.disabled"] = true,
  },
})
