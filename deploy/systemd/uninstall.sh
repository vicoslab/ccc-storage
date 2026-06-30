#!/usr/bin/env bash
set -euo pipefail

systemd_dir="${SYSTEMD_DIR:-/etc/systemd/system}"

if systemctl is-active --quiet ccc-storage-mountd; then
  systemctl stop ccc-storage-mountd
fi
if systemctl is-enabled --quiet ccc-storage-mountd 2>/dev/null; then
  systemctl disable ccc-storage-mountd
fi
rm -f "$systemd_dir/ccc-storage-mountd.service"
systemctl daemon-reload

echo "Removed ccc-storage-mountd.service. Python package uninstall is left explicit."
