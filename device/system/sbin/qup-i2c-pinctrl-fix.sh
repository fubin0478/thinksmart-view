#!/bin/sh
# Fix 78b5000.i2c pinctrl state: GPIO2/3 lose blsp_i2c1 mux after boot.
# Unbind/rebind restores it; power/control=on prevents regression.
QUP_PATH=/sys/bus/platform/devices/78b5000.i2c
DRIVER_PATH=/sys/bus/platform/drivers/i2c_qup
DEV=78b5000.i2c

# Unbind
echo "$DEV" > "$DRIVER_PATH/unbind" 2>/dev/null
sleep 0.3

# Rebind (triggers pinctrl_bind_pins → sets GPIO2/3 to blsp_i2c1)
echo "$DEV" > "$DRIVER_PATH/bind" 2>/dev/null
sleep 0.5

# Prevent PM runtime autosuspend (keeps GPIO2/3 in blsp_i2c1 mode)
echo on > "$QUP_PATH/power/control" 2>/dev/null
logger -t qup-i2c-fix "78b5000.i2c pinctrl restored, PM autosuspend disabled"
