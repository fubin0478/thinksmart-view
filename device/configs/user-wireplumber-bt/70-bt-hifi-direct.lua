-- Hi-Fi PipeWire Bluetooth Speaker codec & resampler config (loads at 70, after /etc/wireplumber/bluetooth.lua.d/60-thinksmart-speaker.lua).
-- Preserves 60-thinksmart-speaker.lua's media-source-role="input" and node.autoconnect=false so XiaoZhi 0.1.6's
-- WebRTC & wake-word AEC, conversation ducking, alarm muting, and Bluetooth UI work as designed, while enabling
-- HD A2DP codecs (AAC bitratemode=5, LDAC HQ, aptX HD, SBC-XQ) and 256-tap sinc resampling (quality=10).
-- Single-user appliance: keep BlueZ A2DP endpoints persistent across thinksmart-kiosk tty1 session restarts
-- and avoid probing absent UPower D-Bus on every session transition.
bluez_monitor.properties["with-logind"] = false
bluez_monitor.properties["bluez5.roles"] = "[ a2dp_sink ]"
bluez_monitor.properties["bluez5.hfphsp-backend"] = "none"
bluez_monitor.properties["bluez5.headset-roles"] = "[ ]"
bluez_monitor.properties["bluez5.codecs"] = "[ aac ldac aptx_hd aptx sbc_xq sbc ]"
bluez_monitor.properties["bluez5.enable-sbc-xq"] = true
bluez_monitor.properties["bluez5.enable-hw-volume"] = true
bluez_monitor.properties["bluez5.a2dp.aac.bitratemode"] = 5
bluez_monitor.properties["bluez5.a2dp.ldac.quality"] = "hq"

table.insert(bluez_monitor.rules, {
  matches = {
    {
      { "device.name", "matches", "bluez_card.*" },
    },
  },
  apply_properties = {
    ["bluez5.auto-connect"] = "[ a2dp_sink ]",
    ["bluez5.hw-volume"] = "[ a2dp_sink ]",
  },
})

table.insert(bluez_monitor.rules, {
  matches = {
    {
      { "node.name", "matches", "bluez_input.*" },
    },
  },
  apply_properties = {
    ["bluez5.media-source-role"] = "input",
    ["node.autoconnect"] = false,
    ["session.suspend-timeout-seconds"] = 0,
    ["priority.session"] = 0,
    ["priority.driver"] = 0,
    ["resample.quality"] = 10,
    ["dither.method"] = "wannamaker3",
  },
})
