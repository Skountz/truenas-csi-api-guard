#!/bin/bash
# truenas-csi-api-guard deploy script
# Run locally to install or update the guard on a TrueNAS host.
#
# Usage:
#   bash deploy.sh <user@truenas-host>
#
# Example:
#   bash deploy.sh admin@192.168.1.10
#
# PROXY_DIR is read from config.yaml (proxy_dir field). Edit config.yaml before running.

set -e

TRUENAS_HOST="${1}"
SCRIPT_DIR="$(dirname "$(realpath "$0")")"
SERVICE_NAME="truenas-csi-api-guard"

if [ -z "${TRUENAS_HOST}" ]; then
    echo "Usage: bash deploy.sh <user@truenas-host>"
    echo "Example: bash deploy.sh admin@192.168.1.10"
    echo "PROXY_DIR is read from config.yaml (proxy_dir field)"
    exit 1
fi

PROXY_DIR="$(python3 -c "import yaml; print(yaml.safe_load(open('${SCRIPT_DIR}/config.yaml'))['proxy_dir'])" 2>/dev/null)"
if [ -z "${PROXY_DIR}" ]; then
    echo "Error: could not read proxy_dir from config.yaml"
    exit 1
fi

BOOT_SCRIPT="${PROXY_DIR}/boot.sh"

echo "[deploy] Installing ${SERVICE_NAME} to ${TRUENAS_HOST}:${PROXY_DIR}"

TMPDIR_REMOTE="/tmp/${SERVICE_NAME}-deploy-$$"

# ---------------------------------------------------------------------------
# 1. Copy proxy files (scp to /tmp — no sudo needed)
# ---------------------------------------------------------------------------
echo "[deploy] Fetching dependencies locally..."
python3 -m pip install --quiet --target "${SCRIPT_DIR}/lib" websockets
find "${SCRIPT_DIR}/lib" -name "*.so" -delete  # strip host-compiled extensions — incompatible with TrueNAS Linux, websockets falls back to pure Python

echo "[deploy] Copying files..."
ssh "${TRUENAS_HOST}" "mkdir -p '${TMPDIR_REMOTE}'"

for f in proxy.py boot.sh; do
    scp "${SCRIPT_DIR}/${f}" "${TRUENAS_HOST}:${TMPDIR_REMOTE}/${f}"
done
scp -r "${SCRIPT_DIR}/lib" "${TRUENAS_HOST}:${TMPDIR_REMOTE}/lib"

scp "${SCRIPT_DIR}/config.yaml" "${TRUENAS_HOST}:${TMPDIR_REMOTE}/config.yaml"

# ---------------------------------------------------------------------------
# 2. All privileged operations in one sudo session (-t for PTY)
# ---------------------------------------------------------------------------
echo "[deploy] Running privileged setup (sudo password may be required)..."
ssh -t "${TRUENAS_HOST}" "
    set -e

    # Install files
    sudo mkdir -p '${PROXY_DIR}'
    sudo mv '${TMPDIR_REMOTE}/proxy.py' '${TMPDIR_REMOTE}/boot.sh' '${PROXY_DIR}/'
    sudo rm -rf '${PROXY_DIR}/lib'
    sudo mv '${TMPDIR_REMOTE}/lib' '${PROXY_DIR}/lib'
    sudo chmod +x '${BOOT_SCRIPT}'

    sudo mv '${TMPDIR_REMOTE}/config.yaml' '${PROXY_DIR}/config.yaml'
    sudo sed -i 's|proxy_dir:.*|proxy_dir: ${PROXY_DIR}|' '${PROXY_DIR}/config.yaml'
    echo '[deploy] config.yaml deployed'

    rm -rf '${TMPDIR_REMOTE}'

    # Register boot.sh as a TrueNAS Post-Init script (idempotent)
    echo '[deploy] Registering post-init script...'
    EXISTING=\$(sudo midclt call initshutdownscript.query '[[\"script\", \"=\", \"${BOOT_SCRIPT}\"]]' 2>/dev/null || echo '[]')
    if [ \"\${EXISTING}\" = '[]' ]; then
        sudo midclt call initshutdownscript.create '{
            \"type\": \"SCRIPT\",
            \"script\": \"${BOOT_SCRIPT}\",
            \"when\": \"POSTINIT\",
            \"enabled\": true,
            \"comment\": \"${SERVICE_NAME}\"
        }'
        echo '[deploy] Post-init script registered'
    else
        echo '[deploy] Post-init script already registered, skipping'
    fi

    # Start the service now
    echo '[deploy] Starting service...'
    sudo bash '${BOOT_SCRIPT}'
"

PORT=$(python3 -c "import yaml; print(yaml.safe_load(open('${SCRIPT_DIR}/config.yaml'))['listen_port'])" 2>/dev/null || echo "8443")

echo ""
echo "[deploy] Done. Verify with:"
echo "  ssh ${TRUENAS_HOST} systemctl status ${SERVICE_NAME}"
echo "  ssh ${TRUENAS_HOST} journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "Point your CSI driver at: wss://<truenas-ip>:${PORT}/api/current"
