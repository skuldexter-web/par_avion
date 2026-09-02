"""
hardware.py — Hardware auto-detection for PAR AVION.

Detects connected SDR devices (RTL-SDR v3/v4, HackRF One, Nooelec variants)
via USB descriptors, and locates a GPS source either through gpsd or a
serial /dev/ttyUSB* device, so the radar display can center on the
operator's real-world position.

All detection here is passive/read-only: it enumerates USB devices and
reads local system state. It does not transmit on any interface.
"""

from __future__ import annotations

import glob
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Known SDR USB vendor:product IDs (from lsusb), mapped to friendly names.
# ---------------------------------------------------------------------------
KNOWN_SDR_DEVICES = {
    "0bda:2838": "RTL-SDR (Realtek RTL2832U) — v3/v4 generic",
    "0bda:2832": "RTL-SDR (Realtek RTL2832U) — legacy",
    "1d50:6089": "HackRF One",
    "1209:6089": "HackRF One (alt VID/PID)",
    "0403:6014": "Nooelec / FTDI-based SDR bridge",
}

GPS_VENDOR_HINTS = ("u-blox", "prolific", "gps", "usb-serial", "ch340", "cp210")


@dataclass
class SDRDevice:
    vid_pid: str
    name: str
    bus: str = ""
    device: str = ""

    def __str__(self) -> str:
        return f"{self.name} [{self.vid_pid}] (bus {self.bus} dev {self.device})"


@dataclass
class GPSSource:
    kind: str                  # "gpsd" or "serial"
    path: Optional[str] = None  # e.g. /dev/ttyUSB0, only for serial
    lat: Optional[float] = None
    lon: Optional[float] = None
    fix: bool = False

    def __str__(self) -> str:
        if self.fix and self.lat is not None:
            return f"{self.kind} fix @ {self.lat:.5f}, {self.lon:.5f}"
        return f"{self.kind} (no fix yet)"


@dataclass
class HardwareReport:
    sdrs: list = field(default_factory=list)
    gps: Optional[GPSSource] = None
    dump1090_running: bool = False
    errors: list = field(default_factory=list)


def _run(cmd: list) -> str:
    """Run a shell command safely, returning stdout or '' on failure."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5, check=False
        )
        return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


def detect_sdr_devices() -> list:
    """Enumerate connected SDR-like USB devices via lsusb."""
    devices = []
    output = _run(["lsusb"])
    if not output:
        return devices

    for line in output.splitlines():
        # Example line: "Bus 001 Device 004: ID 0bda:2838 Realtek Semiconductor Corp. RTL2838 DVB-T"
        match = re.match(
            r"Bus (\d+) Device (\d+): ID ([0-9a-f]{4}:[0-9a-f]{4})", line
        )
        if not match:
            continue
        bus, dev, vid_pid = match.groups()
        if vid_pid in KNOWN_SDR_DEVICES:
            devices.append(
                SDRDevice(
                    vid_pid=vid_pid,
                    name=KNOWN_SDR_DEVICES[vid_pid],
                    bus=bus,
                    device=dev,
                )
            )
    return devices


def check_dvb_driver_conflict() -> bool:
    """
    Returns True if the dvb_usb_rtl28xxu kernel module is loaded, which
    will block userspace RTL-SDR access unless blacklisted (see install.sh).
    """
    output = _run(["lsmod"])
    return "dvb_usb_rtl28xxu" in output


def detect_gps_via_gpsd() -> Optional[GPSSource]:
    """Attempt to pull a position from a running gpsd daemon."""
    try:
        import gpsd  # type: ignore

        gpsd.connect()
        packet = gpsd.get_current()
        if packet.mode >= 2:  # 2D or 3D fix
            return GPSSource(
                kind="gpsd",
                lat=packet.lat,
                lon=packet.lon,
                fix=True,
            )
        return GPSSource(kind="gpsd", fix=False)
    except Exception:
        return None


def detect_gps_via_serial() -> Optional[GPSSource]:
    """
    Fall back to scanning /dev/ttyUSB* / /dev/ttyACM* for a device that
    looks like a GPS dongle, without actually opening/parsing NMEA here
    (that's left to the caller / gpsd) — this just reports candidate ports.
    """
    candidates = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    if not candidates:
        return None
    # Prefer the first candidate; a real deployment could probe each at
    # common baud rates (4800/9600) looking for a "$GP" or "$GN" NMEA sentence.
    return GPSSource(kind="serial", path=candidates[0], fix=False)


def detect_gps() -> Optional[GPSSource]:
    gps = detect_gps_via_gpsd()
    if gps is not None:
        return gps
    return detect_gps_via_serial()


def is_dump1090_running(raw_port: int = 30002, host: str = "127.0.0.1") -> bool:
    """
    Checks whether dump1090's --net --raw TCP port is actually accepting
    connections — not just whether a process named "dump1090" exists.
    A dump1090 instance started without --net (e.g. plain --interactive)
    will match a process-name check but has no raw port to connect to,
    so a port probe is the only reliable signal here.
    """
    import socket as _socket

    try:
        with _socket.create_connection((host, raw_port), timeout=1.0):
            return True
    except OSError:
        return False


def full_report() -> HardwareReport:
    """Run all detection routines and assemble a HardwareReport."""
    report = HardwareReport()

    report.sdrs = detect_sdr_devices()
    if not report.sdrs:
        report.errors.append(
            "No SDR devices detected. Check `lsusb` and cabling; "
            "ensure udev rules from install.sh have been applied "
            "(replug device after install)."
        )

    if check_dvb_driver_conflict():
        report.errors.append(
            "dvb_usb_rtl28xxu kernel driver is loaded — this will claim "
            "the RTL-SDR before userspace tools can. Blacklist it "
            "(install.sh does this) and reboot or `rmmod dvb_usb_rtl28xxu`."
        )

    report.gps = detect_gps()
    if report.gps is None:
        report.errors.append(
            "No GPS source found (gpsd not running, no /dev/ttyUSB* "
            "candidates). Radar will center on a manually-set fallback "
            "location instead of live GPS."
        )

    report.dump1090_running = is_dump1090_running()

    return report


def wait_for_gps_fix(timeout_s: float = 10.0) -> Optional[GPSSource]:
    """Poll gpsd briefly for a fix, used when entering Airplanes mode."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        gps = detect_gps_via_gpsd()
        if gps and gps.fix:
            return gps
        time.sleep(1)
    return None


if __name__ == "__main__":
    # Quick standalone diagnostic: `python3 -m modules.hardware`
    rep = full_report()
    print("SDR devices:")
    for d in rep.sdrs:
        print(f"  - {d}")
    print(f"GPS: {rep.gps}")
    print(f"dump1090 running: {rep.dump1090_running}")
    if rep.errors:
        print("\nWarnings:")
        for e in rep.errors:
            print(f"  ! {e}")
