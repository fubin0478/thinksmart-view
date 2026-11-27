#!/bin/sh
# Force blsp_i2c1 function + pull-up on GPIO2/3 (pmOS DTB has bias-disable;
# combined with the custom i2c_qup driver not re-asserting the mux, SDA/SCL
# end up in gpio input mode with no pull, making bus 1 silent).
set -e
# Pinmux function is 3 (blsp_i2c1) at bits[9:6], drive 2mA/code 0 at bits[5:2], pull-up at bits[1:0].
# Use 0xC3 (func=3, pull=3, drv=0) - matches Lenovo stock config.
if [ -x /bin/busybox ]; then
  DM="/bin/busybox devmem"
else
  DM="devmem"
fi
$DM 0x1002000 32 0xC3
$DM 0x1003000 32 0xC3
