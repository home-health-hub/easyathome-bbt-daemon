#!/usr/bin/bash
# Installs easyathome-bbt-daemon: creates a venv, installs the package from
# this checkout, seeds the config, creates the service user, and installs
# the systemd units (daemon service enabled; API service installed but not
# enabled, since it's opt-in). Re-running is safe: it skips steps that are
# already done (existing config, existing user) and upgrades the rest.
set -e

if [[ "${EUID}" -ne 0 ]]; then
    echo "This script must be run as root (e.g. with sudo)." >&2
    exit 1
fi

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    echo "Usage: sudo ./install.sh"
    echo "Installs easyathome-bbt-daemon as a systemd service. No options."
    exit 0
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/easyathome-bbt-daemon"
CONFIG_DIR="/etc/easyathome-bbt-daemon"
SERVICE_USER="easyathome-bbt-daemon"

echo "==> Creating virtual environment at ${INSTALL_DIR}/venv"
python3 -m venv "${INSTALL_DIR}/venv"

echo "==> Installing easyathome-bbt-daemon from ${REPO_DIR}"
"${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install --quiet "${REPO_DIR}"

echo "==> Linking commands into /usr/bin"
ln -sf "${INSTALL_DIR}/venv/bin/easyathome-bbt-daemon" /usr/bin/easyathome-bbt-daemon
ln -sf "${INSTALL_DIR}/venv/bin/easyathome-bbt-api" /usr/bin/easyathome-bbt-api

echo "==> Creating service user"
if ! id "${SERVICE_USER}" &>/dev/null; then
    useradd --system --no-create-home --group "${SERVICE_USER}"
fi

echo "==> Seeding config"
mkdir -p "${CONFIG_DIR}"
if [[ -f "${CONFIG_DIR}/config.ini" ]]; then
    echo "    ${CONFIG_DIR}/config.ini already exists, leaving its contents as-is."
else
    cp "${REPO_DIR}/config/easyathome-bbt-daemon.ini.example" "${CONFIG_DIR}/config.ini"
    echo "    Wrote ${CONFIG_DIR}/config.ini -- edit it (address and device_timezone"
    echo "    are required) before starting the service."
fi
# The config can hold a real API token, so it's only readable by the
# service account -- applied every run, not just on first write, in case
# it was ever loosened.
chown "${SERVICE_USER}:${SERVICE_USER}" "${CONFIG_DIR}/config.ini"
chmod 600 "${CONFIG_DIR}/config.ini"

echo "==> Installing systemd units"
cp "${REPO_DIR}/systemd/easyathome-bbt-daemon.service" /etc/systemd/system/
cp "${REPO_DIR}/systemd/easyathome-bbt-api.service" /etc/systemd/system/
systemctl daemon-reload

echo "==> Done. Edit ${CONFIG_DIR}/config.ini (address and device_timezone are"
echo "    required), then start the service:"
echo "        sudo systemctl enable --now easyathome-bbt-daemon"
echo "        journalctl -u easyathome-bbt-daemon -f"
echo "==> Since the config is now owned by ${SERVICE_USER} (mode 600), running the CLI"
echo "    by hand needs sudo -u, e.g.:"
echo "        sudo -u ${SERVICE_USER} easyathome-bbt-daemon --config ${CONFIG_DIR}/config.ini --check-config"
echo "==> The HTTP API is installed but not enabled (opt-in). To turn it on:"
echo "        sudo systemctl enable --now easyathome-bbt-api"
