#!/usr/bin/env bash
#
# install.sh — PAR AVION automated setup for Kali Linux
#
# Installs system packages (rtl-sdr, hackrf, dump1090, gpsd), sets up
# udev rules for non-root SDR USB access, blacklists the conflicting
# dvb_usb_rtl28xxu kernel driver, and installs Python dependencies.
#
# Usage:
#   chmod +x install.sh
#   ./install.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=============================================="
echo "  PAR AVION — Installation"
echo "=============================================="

if [[ $EUID -ne 0 ]]; then
    echo "This script needs root for package installs and udev rules."
    echo "Re-running with sudo..."
    exec sudo bash "$0" "$@"
fi

REAL_USER="${SUDO_USER:-$USER}"

echo ""
echo "[1/6] Updating package lists..."
apt-get update -qq

echo ""
echo "[2/6] Installing system dependencies..."
# dump1090-mutability is the common Kali/Debian package name; dump1090-fa
# (FlightAware's fork) is offered as a fallback since it's not always in
# the default repos. We try both, non-fatal if one is unavailable.
apt-get install -y \
    rtl-sdr \
    librtlsdr-dev \
    hackrf \
    libhackrf-dev \
    libusb-1.0-0-dev \
    pkg-config \
    gpsd \
    gpsd-clients \
    sox \
    libsox-fmt-all \
    alsa-utils \
    pulseaudio-utils \
    multimon-ng \
    python3-pip \
    python3-dev \
    python3-numpy \
    python3-scipy \
    python3-sounddevice \
    build-essential \
    git \
    || true

# apt-cache show can report a "candidate" for a package name that's only
# referenced in another package's metadata (no real .deb behind it), so we
# check policy for an actual installable candidate rather than trusting
# `apt-cache show`'s exit code alone.
_has_candidate() {
    apt-cache policy "$1" 2>/dev/null | grep -q "Candidate: (none)" && return 1
    apt-cache policy "$1" 2>/dev/null | grep -q "Candidate:" && return 0
    return 1
}

if _has_candidate dump1090-mutability && apt-get install -y dump1090-mutability; then
    :
elif _has_candidate dump1090-fa && apt-get install -y dump1090-fa; then
    :
else
    echo "  ! No packaged dump1090 available (dump1090-mutability / dump1090-fa)."
    echo "    Building dump1090 (FlightAware fork) from source instead..."
    TMP_DIR="$(mktemp -d)"
    git clone --depth 1 https://github.com/flightaware/dump1090.git "$TMP_DIR/dump1090"
    make -C "$TMP_DIR/dump1090"
    install -m 0755 "$TMP_DIR/dump1090/dump1090" /usr/local/bin/dump1090
    rm -rf "$TMP_DIR"
fi

# rtl_ais for maritime AIS demodulation — often bundled with rtl-sdr tools,
# but build from source if the binary isn't present after package install.
if ! command -v rtl_ais >/dev/null 2>&1; then
    echo "  rtl_ais not found — building from source..."
    TMP_DIR="$(mktemp -d)"
    git clone --depth 1 https://github.com/dgiardini/rtl-ais.git "$TMP_DIR/rtl-ais" || \
        git clone --depth 1 https://github.com/Guenael/rtl-ais.git "$TMP_DIR/rtl-ais" || true
    if [[ -d "$TMP_DIR/rtl-ais" ]]; then
        make -C "$TMP_DIR/rtl-ais" || echo "  ! rtl_ais build failed; Maritime mode will show 'no feed' until installed manually."
        [[ -f "$TMP_DIR/rtl-ais/rtl_ais" ]] && install -m 0755 "$TMP_DIR/rtl-ais/rtl_ais" /usr/local/bin/rtl_ais
    fi
    rm -rf "$TMP_DIR"
fi

echo ""
echo "[3/6] Setting up udev rules for non-root SDR access..."
UDEV_RULES_FILE="/etc/udev/rules.d/20-par-avion-sdr.rules"
cat > "$UDEV_RULES_FILE" <<'EOF'
# PAR AVION — non-root USB access for common SDR hardware
# RTL-SDR (Realtek RTL2832U based dongles, v3/v4, Nooelec, NESDR, etc.)
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0666"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", GROUP="plugdev", MODE="0666"
# HackRF One
SUBSYSTEM=="usb", ATTRS{idVendor}=="1d50", ATTRS{idProduct}=="6089", GROUP="plugdev", MODE="0666"
SUBSYSTEM=="usb", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="6089", GROUP="plugdev", MODE="0666"
EOF
echo "  Wrote $UDEV_RULES_FILE"

udevadm control --reload-rules
udevadm trigger

if getent group plugdev >/dev/null 2>&1; then
    usermod -aG plugdev "$REAL_USER" || true
    echo "  Added $REAL_USER to 'plugdev' group (log out/in to take effect)."
fi

echo ""
echo "[4/6] Blacklisting conflicting kernel driver (dvb_usb_rtl28xxu)..."
BLACKLIST_FILE="/etc/modprobe.d/20-par-avion-blacklist-rtl.conf"
cat > "$BLACKLIST_FILE" <<'EOF'
# PAR AVION — prevent the DVB-T kernel driver from claiming RTL-SDR
# dongles before userspace tools (rtl_sdr, dump1090, etc.) can use them.
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF
echo "  Wrote $BLACKLIST_FILE"

# Unload it now if already loaded, so a reboot isn't strictly required.
if lsmod | grep -q dvb_usb_rtl28xxu; then
    modprobe -r dvb_usb_rtl28xxu 2>/dev/null || \
        echo "  ! Could not hot-unload dvb_usb_rtl28xxu (in use). Reboot to fully apply blacklist."
fi

echo ""
echo "[5/6] Enabling gpsd service (socket-activated, off until a device is plugged in)..."
systemctl enable gpsd.socket >/dev/null 2>&1 || true
systemctl restart gpsd.socket >/dev/null 2>&1 || true

echo ""
echo "[6/6] Installing Python dependencies..."
pip3 install --break-system-packages -r "$SCRIPT_DIR/requirements.txt"

echo ""
echo "=============================================="
echo "  Installation complete."
echo "=============================================="
echo ""
echo "Next steps:"
echo "  1. Unplug and replug your SDR/GPS USB devices so udev rules apply."
echo "  2. If dvb_usb_rtl28xxu was in use, reboot to fully release the device."
echo "  3. Log out/in (or run 'newgrp plugdev') to pick up the plugdev group."
echo "  4. Launch:  python3 $SCRIPT_DIR/par_avion.py"
echo ""
