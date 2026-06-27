#!/usr/bin/env bash
set -euo pipefail

systemd_dir="${SYSTEMD_DIR:-/etc/systemd/system}"

if systemctl is-active --quiet ccc-layered-mountd; then
  systemctl stop ccc-layered-mountd
fi
if systemctl is-enabled --quiet ccc-layered-mountd 2>/dev/null; then
  systemctl disable ccc-layered-mountd
fi
rm -f "$systemd_dir/ccc-layered-mountd.service"
systemctl daemon-reload

echo "Removed ccc-layered-mountd.service. Python package uninstall is left explicit."
