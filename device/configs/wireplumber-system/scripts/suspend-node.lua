-- WirePlumber suspend-node.lua
-- System original with one addition: never suspend the TAS5782M speaker sink.
--
-- Copyright © 2021 Collabora Ltd.
--    @author George Kiagiadakis <george.kiagiadakis@collabora.com>
-- SPDX-License-Identifier: MIT
--
-- Modification: TAS5782M requires 546ms re-init whenever the PCM closes.
-- Short notification sounds (<546ms) are completely silent when the chip
-- re-initializes on every stream open.  Keeping this node permanently idle
-- (never suspended) holds the ALSA PCM open and keeps BCLK running so the
-- chip stays initialized.  All sounds play immediately after the first init.

local TAS_SINK_UCM = "alsa_output.platform-c051000.sound-card.HiFi__hw_cd18781y_0__sink"
local TAS_SINK_SPEAKER = "alsa_output.platform-c051000.sound-card.HiFi__Speaker__sink"
local TAS_SINK_ACP = "alsa_output.platform-c051000.sound-card.playback.0.0"

om = ObjectManager {
  Interest { type = "node",
    Constraint { "media.class", "matches", "Audio/*" }
  },
  Interest { type = "node",
    Constraint { "media.class", "matches", "Video/*" }
  },
}

sources = {}

om:connect("object-added", function (om, node)
  node:connect("state-changed", function (node, old_state, cur_state)
    -- Always clear the current source if any
    local id = node["bound-id"]
    if sources[id] then
      sources[id]:destroy()
      sources[id] = nil
    end

    -- Add a timeout source if idle for at least 5 seconds
    if cur_state == "idle" or cur_state == "error" then
      -- honor "session.suspend-timeout-seconds" if specified
      local timeout =
          tonumber(node.properties["session.suspend-timeout-seconds"]) or 5

      -- TAS5782M speaker: never suspend — keep PCM open, chip initialized
      local name = node.properties["node.name"] or ""
      if name == TAS_SINK_UCM or name == TAS_SINK_SPEAKER or name == TAS_SINK_ACP then
        timeout = 0
      end

      if timeout == 0 then
        return
      end

      -- add idle timeout; multiply by 1000, timeout_add() expects ms
      sources[id] = Core.timeout_add(timeout * 1000, function()
        -- Suspend the node
        -- but check first if the node still exists
        if (node:get_active_features() & Feature.Proxy.BOUND) ~= 0 then
          Log.info(node, "was idle for a while; suspending ...")
          node:send_command("Suspend")
        end

        -- Unref the source
        sources[id] = nil

        -- false (== G_SOURCE_REMOVE) destroys the source so that this
        -- function does not get fired again after 5 seconds
        return false
      end)
    end

  end)
end)

om:activate()
