#!/bin/sh
# Redirect /lib/firmware/venus.mdt from stock Lenovo modem-partition firmware
# (downstream HFI protocol -> upstream driver gets "Unsupported property: 0" -> EIO)
# to the upstream Qualcomm venus-4.2 firmware that matches MSM8953 HFI 3XX.
set -eu

SRC=/lib/firmware/qcom/venus-4.2/venus.mbn.zst
if [ ! -e "$SRC" ]; then
    echo "missing $SRC (install linux-firmware)" >&2
    exit 1
fi

# Remove the stock symlinks (they point to /run/msm-firmware-loader modem blobs).
for f in venus.mdt venus.b00 venus.b01 venus.b02 venus.b03 venus.b04; do
    if [ -L /lib/firmware/$f ] || [ -e /lib/firmware/$f ]; then
        rm -f /lib/firmware/$f
    fi
done

# Provide the upstream blob under the name the driver requests.
# Kernel fw loader also tries <name>.zst automatically.
ln -sfn "$SRC" /lib/firmware/venus.mdt.zst

echo "venus firmware redirected:"
ls -la /lib/firmware/venus.* 2>/dev/null || true
