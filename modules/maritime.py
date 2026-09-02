"""
maritime.py — AIS decoder & vessel telemetry view for PAR AVION.

AIS (Automatic Identification System) is broadcast in the clear on marine
VHF channels 87B/88B (161.975 / 162.025 MHz) by any vessel required to
carry a transponder — the same public data feeding sites like
MarineTraffic. This module can decode a live SDR feed (via an external
AIS demodulator like `rtl_ais` piping NMEA over UDP) or connect to a
public AIS-over-TCP aggregator the user has configured. Nothing here
transmits.

Two ingest paths are supported:
  1. Local `rtl_ais` process emitting NMEA 0183 AIVDM sentences on UDP.
  2. A pre-configured TCP host:port feed (e.g. a local AIS receiver box).
"""

from __future__ import annotations

import curses
import math
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import radar_ui

AIS_UDP_HOST = "127.0.0.1"
AIS_UDP_PORT = 10110  # rtl_ais default NMEA output port

# AIS 6-bit ASCII table (ITU-R M.1371 / gpsd AIVDM spec, Table 3).
# Index 0-31 -> '@' through '_' (ASCII 64-95); index 32-63 -> ' ' through '?'
# (ASCII 32-63). NOT the same ordering as a plain digits-then-letters table.
_AIS_CHARS = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"


@dataclass
class Vessel:
    mmsi: str
    name: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    sog_kt: Optional[float] = None  # speed over ground
    cog_deg: Optional[float] = None  # course over ground
    nav_status: str = ""
    last_seen: float = field(default_factory=time.time)

    def distance_bearing_from(self, ref_lat: float, ref_lon: float):
        if self.lat is None or self.lon is None:
            return None, None
        R_NM = 3440.065
        lat1, lon1, lat2, lon2 = map(math.radians, (ref_lat, ref_lon, self.lat, self.lon))
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        c = 2 * math.asin(min(1, math.sqrt(a)))
        distance = R_NM * c
        bearing = math.degrees(
            math.atan2(
                math.sin(dlon) * math.cos(lat2),
                math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon),
            )
        ) % 360
        return distance, bearing


def _sixbit_decode(payload: str) -> str:
    """Decode an AIVDM 6-bit-armored payload into a raw bitstring."""
    bits = []
    for ch in payload:
        val = ord(ch) - 48
        if val > 40:
            val -= 8
        bits.append(format(val & 0x3F, "06b"))
    return "".join(bits)


def _bits_to_int(bits: str, signed: bool = False) -> int:
    if not bits:
        return 0
    val = int(bits, 2)
    if signed and bits[0] == "1":
        val -= (1 << len(bits))
    return val


def _bits_to_text(bits: str) -> str:
    chars = []
    for i in range(0, len(bits) - 5, 6):
        code = int(bits[i:i + 6], 2)
        if code < len(_AIS_CHARS):
            chars.append(_AIS_CHARS[code])
    return "".join(chars).strip("@ ").strip()


class RtlAisController:
    """Spawns rtl_ais as a background process if available. Does not bind
    the UDP port itself (AISTracker owns that single bind) — instead
    checks for an already-running rtl_ais process by name, and otherwise
    just tries to launch one and confirms the process stays alive."""

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.owns_process = False
        self.last_error = ""

    def _rtl_ais_process_running(self) -> bool:
        try:
            # pgrep -x matches against the process name only (not the full
            # command line via -f), avoiding false positives from unrelated
            # commands that merely mention "rtl_ais" as an argument.
            result = subprocess.run(
                ["pgrep", "-x", "rtl_ais"], capture_output=True, text=True, timeout=3
            )
            return bool(result.stdout.strip())
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    def ensure_running(self) -> bool:
        if self._rtl_ais_process_running():
            return True
        return self.spawn()

    def spawn(self) -> bool:
        try:
            proc = subprocess.Popen(
                ["rtl_ais", "-P", str(AIS_UDP_PORT)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            self.last_error = (
                "rtl_ais not found on PATH. Run install.sh, or install "
                "manually from github.com/dgiardini/rtl-ais."
            )
            return False
        except Exception as e:
            self.last_error = f"rtl_ais: {e}"
            return False

        for _ in range(15):  # up to ~3s
            time.sleep(0.2)
            if proc.poll() is not None:
                break
        if proc.poll() is None:
            self.process = proc
            self.owns_process = True
            self.last_error = ""
            return True

        stderr_output = ""
        try:
            if proc.stderr:
                stderr_output = proc.stderr.read().decode(errors="ignore")[:200]
        except Exception:
            pass
        self.last_error = "rtl_ais exited immediately" + (
            f": {stderr_output}" if stderr_output else " (no SDR available?)"
        )
        return False

    def shutdown(self) -> None:
        if self.owns_process and self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
            self.owns_process = False


class AISTracker:
    """Listens for AIVDM sentences over UDP (from rtl_ais) and decodes
    position report messages (types 1/2/3) and static/voyage data (type 5)."""

    def __init__(self, host: str = AIS_UDP_HOST, port: int = AIS_UDP_PORT):
        self.host = host
        self.port = port
        self._vessels: Dict[str, Vessel] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.connected = False
        self.last_error = ""
        self._fragment_buffer: Dict[tuple, List[Optional[str]]] = {}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((self.host, self.port))
            sock.settimeout(1.0)
            self.connected = True
        except OSError as e:
            self.connected = False
            self.last_error = f"Could not bind UDP {self.host}:{self.port}: {e}"
            return

        with sock:
            while not self._stop.is_set():
                try:
                    data, _ = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                for line in data.decode("ascii", errors="ignore").splitlines():
                    self._handle_sentence(line.strip())

    def _handle_sentence(self, line: str) -> None:
        if not line.startswith("!AIVDM") and not line.startswith("!AIVDO"):
            return
        parts = line.split(",")
        if len(parts) < 7:
            return
        try:
            frag_count = int(parts[1])
            frag_num = int(parts[2])
            payload = parts[5]
        except (ValueError, IndexError):
            return

        if frag_count > 1:
            # Multi-part message reassembly keyed by the NMEA sequential
            # message ID (field 4), which is shared across all fragments
            # of one logical message. Also fold in the talker/channel
            # (parts[0], parts[4]) so two unrelated multi-part messages
            # that happen to reuse the same seq_id don't collide — but
            # NOT any part of the payload itself, which differs between
            # fragments of the *same* message and would prevent them
            # from ever being recognized as belonging together.
            seq_id = parts[3] or "0"
            channel = parts[4] if len(parts) > 4 else ""
            key = (parts[0], channel, seq_id)
            buf = self._fragment_buffer.setdefault(key, [None] * frag_count)
            if frag_num - 1 >= len(buf):
                # frag_count mismatch between fragments — bail out safely
                # rather than index out of range.
                self._fragment_buffer.pop(key, None)
                return
            buf[frag_num - 1] = payload
            if any(p is None for p in buf):
                return
            payload = "".join(buf)
            del self._fragment_buffer[key]

        self._decode_payload(payload)

    def _decode_payload(self, payload: str) -> None:
        try:
            bits = _sixbit_decode(payload)
            if len(bits) < 38:
                return
            msg_type = _bits_to_int(bits[0:6])
            mmsi = str(_bits_to_int(bits[8:38]))

            with self._lock:
                vessel = self._vessels.setdefault(mmsi, Vessel(mmsi=mmsi))
                vessel.last_seen = time.time()

                if msg_type in (1, 2, 3) and len(bits) >= 143:
                    sog_raw = _bits_to_int(bits[50:60])
                    lon_raw = _bits_to_int(bits[61:89], signed=True)
                    lat_raw = _bits_to_int(bits[89:116], signed=True)
                    cog_raw = _bits_to_int(bits[116:128])

                    vessel.sog_kt = sog_raw / 10.0 if sog_raw != 1023 else None
                    vessel.lon = lon_raw / 600000.0 if lon_raw != 0x6791AC0 else None
                    vessel.lat = lat_raw / 600000.0 if lat_raw != 0x3412140 else None
                    vessel.cog_deg = cog_raw / 10.0 if cog_raw != 3600 else None

                elif msg_type == 5 and len(bits) >= 302:
                    name_bits = bits[112:232]
                    vessel.name = _bits_to_text(name_bits)
        except Exception:
            return

    def snapshot(self, max_age_s: float = 300.0) -> List[Vessel]:
        now = time.time()
        with self._lock:
            fresh = [v for v in self._vessels.values() if now - v.last_seen <= max_age_s]
            self._vessels = {v.mmsi: v for v in fresh}
            return sorted(fresh, key=lambda v: v.last_seen, reverse=True)


def run(stdscr, ref_lat: float = 0.0, ref_lon: float = 0.0) -> None:
    """Keys: [Q] back to menu   [S] (re)start rtl_ais"""
    curses.curs_set(0)
    radar_ui.init_colors()

    height, width = stdscr.getmaxyx()
    radar_h = height - 1
    sweep = radar_ui.RadarSweep(radar_h, width // 2, radar_ui.PAIR_BLUE)
    table = radar_ui.DataTable(
        radar_h, width - width // 2,
        headers=["MMSI", "NAME", "SOG", "COG", "DIST"],
        col_widths=[10, 16, 7, 6, 8],
    )

    radar_win = curses.newwin(radar_h, width // 2, 0, 0)
    table_win = curses.newwin(radar_h, width - width // 2, 0, width // 2)
    status_win = curses.newwin(1, width, height - 1, 0)

    rtl_ais = RtlAisController()
    rtl_ais.ensure_running()  # best-effort; UDP listener starts regardless

    tracker = AISTracker()
    tracker.start()  # listens on UDP:10110 regardless of who's sending to it

    def _restart_feed() -> None:
        status_win.erase()
        try:
            status_win.addstr(0, 0, " Restarting rtl_ais... "[: width - 1],
                               curses.color_pair(radar_ui.PAIR_YELLOW))
        except curses.error:
            pass
        status_win.refresh()
        rtl_ais.shutdown()
        rtl_ais.spawn()

    stdscr.nodelay(True)
    stdscr.timeout(200)

    try:
        while True:
            key = stdscr.getch()
            if key in (ord("q"), ord("Q"), 27):
                break
            elif key in (ord("s"), ord("S")):
                _restart_feed()

            sweep.tick()
            vessels = tracker.snapshot()

            contacts = []
            rows = []
            for v in vessels[:50]:
                dist, bearing = v.distance_bearing_from(ref_lat, ref_lon)
                if dist is not None:
                    contacts.append(
                        radar_ui.RadarContact(
                            range_frac=min(1.0, dist / 40.0),
                            bearing_deg=bearing,
                            glyph="▲",
                            label=v.name or v.mmsi,
                        )
                    )
                rows.append([
                    v.mmsi,
                    (v.name or "UNKNOWN")[:15],
                    f"{v.sog_kt:.1f}kt" if v.sog_kt else "---",
                    f"{v.cog_deg:.0f}" if v.cog_deg else "---",
                    f"{dist:.1f}nm" if dist else "---",
                ])

            sweep.draw(radar_win, contacts)
            table.draw(table_win, rows, title="AIS Vessel Feed — 161.975/162.025MHz")

            status_win.erase()
            if tracker.connected:
                feed_status = "UDP:10110 LISTENING"
                pair_color = radar_ui.PAIR_BLUE
            else:
                feed_status = tracker.last_error or "UDP bind failed — port in use?"
                pair_color = radar_ui.PAIR_RED
            try:
                status_win.addstr(
                    0, 0,
                    f" [Q] Quit  [S] Restart rtl_ais  |  {feed_status}  |  vessels: {len(vessels)}  "[: width - 1],
                    curses.color_pair(pair_color),
                )
            except curses.error:
                pass
            status_win.noutrefresh()
            curses.doupdate()
    finally:
        tracker.stop()
        rtl_ais.shutdown()
