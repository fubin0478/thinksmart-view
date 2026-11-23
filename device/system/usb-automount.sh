#!/bin/bash
# Called by thinksmart-usb-mount@.service, outside udev's mount namespace.
set -euo pipefail
ACTION="${1:-}"
DEVNAME="${2:-}"
[[ "$DEVNAME" =~ ^sd[a-z]+[0-9]*$ ]] || exit 2
DEV="/dev/$DEVNAME"
MNT="/mnt/usb"

exec 9>/run/lock/thinksmart-usb.lock
if [[ "$ACTION" == "remove" ]]; then
  flock -w 5 -x 9 || flock -w 10 -x 9 || exit 0
else
  flock -w 15 -x 9
fi
CURRENT=$(timeout 3 findmnt -krn -M "$MNT" -o SOURCE 9>&- 2>/dev/null | head -n1 || true)
CUR_DEV="${CURRENT%%\\*}"

case "$ACTION" in
  add)
    [[ -b "$DEV" ]] || exit 1
    if [[ "$DEVNAME" =~ ^sd[a-z]+$ ]] && compgen -G "/sys/class/block/$DEVNAME/${DEVNAME}[0-9]*" >/dev/null; then
      echo "[usb] Ignoring partitioned whole disk $DEV"
      exit 0
    fi
    PART_TYPE=$(timeout 3 udevadm info --query=property --name="$DEV" 9>&- 2>/dev/null | sed -n 's/^ID_PART_ENTRY_TYPE=//p' | head -n1 || true)
    if [[ "${PART_TYPE,,}" == "c12a7328-f81f-11d2-ba4b-00a0c93ec93b" || "${PART_TYPE,,}" == "0xef" ]]; then
      echo "[usb] Ignoring EFI System Partition $DEV ($PART_TYPE)"
      exit 0
    fi
    if [[ -n "$CURRENT" ]]; then
      if [[ -n "$CUR_DEV" && -b "$CUR_DEV" ]] && timeout 2 stat "$MNT/." 9>&- >/dev/null 2>&1; then
        if [[ "$(readlink -m "$CUR_DEV")" != "$(readlink -m "$DEV")" ]]; then
          echo "[usb] $MNT already mounted from $CURRENT; leaving it in place"
          exit 0
        fi
        echo "[usb] $DEV already mounted at $MNT; keeping in place"
        exit 0
      fi
      timeout -k 1 3 umount "$MNT" 9>&- 2>/dev/null || umount -l "$MNT" 9>&- 2>/dev/null || true
    fi
    mkdir -p "$MNT"
    FSTYPE=$(timeout 8 blkid -o value -s TYPE "$DEV" 9>&- || true)
    OPTIONS="rw,noatime,nodev,nosuid,noexec"
    case "$FSTYPE" in
      vfat) OPTIONS+=",uid=1000,gid=1000,umask=022,utf8" ;;
      exfat|ntfs) OPTIONS+=",uid=1000,gid=1000,umask=022" ;;
      ext2|ext3|ext4) ;;
      *) echo "[usb] Ignoring unsupported filesystem on $DEV: $FSTYPE"; exit 0 ;;
    esac
    if ! timeout 10 mount -t "$FSTYPE" -o "$OPTIONS" "$DEV" "$MNT" 9>&- 2>/dev/null; then
      if ! { [[ "$FSTYPE" == "ntfs" ]] && timeout 10 mount -t ntfs3 -o "$OPTIONS" "$DEV" "$MNT" 9>&- 2>/dev/null; }; then
        timeout 10 mount -t "$FSTYPE" -o "${OPTIONS/#rw,/ro,}" "$DEV" "$MNT" 9>&- 2>/dev/null || {
          [[ "$FSTYPE" == "ntfs" ]] && timeout 10 mount -t ntfs3 -o "${OPTIONS/#rw,/ro,}" "$DEV" "$MNT" 9>&-
        }
      fi
    fi
    echo "[usb] Mounted $DEV ($FSTYPE) at $MNT"
    ;;
  remove)
    # An unrelated disk/partition removal must never unmount the current drive.
    if [[ -n "$CUR_DEV" && "$(readlink -m "$CUR_DEV")" == "$(readlink -m "$DEV")" ]]; then
      if [[ ! -b "$DEV" ]]; then
        umount -l "$MNT" 9>&- 2>/dev/null || true
      else
        timeout -k 1 3 umount "$MNT" 9>&- 2>/dev/null || umount -l "$MNT" 9>&- 2>/dev/null || true
      fi
      echo "[usb] Unmounted $DEV from $MNT"
    fi
    ;;
  *) exit 2 ;;
esac
