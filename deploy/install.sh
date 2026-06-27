#!/usr/bin/env bash
set -euo pipefail

prefix="${PREFIX:-/usr/local}"
systemd_dir="${SYSTEMD_DIR:-/etc/systemd/system}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

install -Dm0644 "$repo_root/deploy/ccc-layered-mountd.service" \
  "$systemd_dir/ccc-layered-mountd.service"

python -m pip install "$repo_root"

systemctl daemon-reload
cat <<'MSG'
Installed ccc-layered-mountd.service.
Review /etc/systemd/system/ccc-layered-mountd.service and then start manually:
  sudo systemctl start ccc-layered-mountd
  sudo systemctl status ccc-layered-mountd
Enable persistence only after validating a new managed parent path.
MSG
