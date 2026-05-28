#!/bin/bash
# truenas-csi-api-guard boot script
# Registered as a Post-Init script via TrueNAS UI or deploy.sh.
# Self-locates using $0 — no hardcoded paths.

set -e

PROXY_DIR="$(dirname "$(realpath "$0")")"
SERVICE_NAME="truenas-csi-api-guard"
SYSTEMD_TARGET="/etc/systemd/system/${SERVICE_NAME}.service"
DEPS="${PROXY_DIR}/lib"
CONFIG="${PROXY_DIR}/config.yaml"

echo "[truenas-csi-api-guard] PROXY_DIR=${PROXY_DIR}"

# Generate the systemd unit dynamically so paths are always correct
cat > "${SYSTEMD_TARGET}" <<UNIT
[Unit]
Description=TrueNAS CSI WebSocket Proxy
After=network-online.target middlewared.service wg-quick.target
Wants=network-online.target wg-quick.target

[Service]
Type=simple
Environment=PYTHONPATH=${DEPS}
ExecStart=python3 ${PROXY_DIR}/proxy.py ${CONFIG}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
User=root

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo "[truenas-csi-api-guard] Started successfully"
