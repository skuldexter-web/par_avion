"""
airplanes.py — Dump1090 daemon controller & ADS-B decoder for PAR AVION.

Spawns/manages a background dump1090 process (or connects to one already
running), reads its raw-hex TCP stream, decodes Mode S / ADS-B frames with
pyModeS, and maintains a rolling table of visible aircraft with computed
range/bearing from the operator's GPS position.

This module only receives broadcast ADS-B signals (1090MHz), which every
aircraft transmits openly and continuously — the same data FlightRadar24
and similar public trackers use. Nothing here transmits.
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

try:
    import pyModeS as pms
    HAVE_PYMODES = True
except ImportError:
    HAVE_PYMODES = False

from . import radar_ui


DUMP1090_HOST = "127.0.0.1"
DUMP1090_RAW_PORT = 30002  # --net --raw output port


@dataclass
class Aircraft:
    icao: str
    callsign: str = ""
    altitude_ft: Optional[int] = None
    speed_kt: Optional[float] = None
    heading_deg: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    last_seen: float = field(default_factory=time.time)

    def distance_bearing_from(self, ref_lat: float, ref_lon: float):
        """Great-circle distance (nm) and initial bearing (deg) from ref point."""
        if self.lat is None or self.lon is None:
            return None, None
        R_NM = 3440.065
        lat1, lon1, lat2, lon2 = map(
            math.radians, (ref_lat, ref_lon, self.lat, self.lon)
        )
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


class Dump1090Controller:
    """Starts dump1090 as a background subprocess if not already running,
    and can be told to (re)start it explicitly from within the UI."""

    BINARIES = ["dump1090-fa", "dump1090-mutability", "dump1090"]

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.owns_process = False
        self.last_error = ""
        self.launched_binary = ""

    def is_port_open(self) -> bool:
        from . import hardware
        return hardware.is_dump1090_running()

    def ensure_running(self) -> bool:
        """Returns True if the raw port is (or becomes) reachable. If
        something is already listening on 30002 — whether started by us,
        by the user manually, or by a system service — this leaves it
        alone rather than spawning a redundant second instance."""
        if self.is_port_open():
            return True
        return self.spawn()

    def spawn(self) -> bool:
        """Explicitly (re)launch dump1090 with --net --raw. Tries each
        known binary name in turn; records the last error if all fail."""
        for binary in self.BINARIES:
            try:
                proc = subprocess.Popen(
                    [binary, "--net", "--raw", "--quiet"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except FileNotFoundError:
                continue
            except Exception as e:
                self.last_error = f"{binary}: {e}"
                continue

            # Give it a moment to bind its network port, then check both
            # that the process is still alive AND that the port actually
            # opened — a process can stay alive while failing to bind.
            for _ in range(20):  # up to ~4s, checked in small increments
                time.sleep(0.2)
                if proc.poll() is not None:
                    break  # exited already — failed to start
                if self.is_port_open():
                    self.process = proc
                    self.owns_process = True
                    self.launched_binary = binary
                    self.last_error = ""
                    return True

            # Didn't come up in time or exited — capture stderr, clean up.
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            stderr_output = ""
            try:
                if proc.stderr:
                    stderr_output = proc.stderr.read().decode(errors="ignore")[:200]
            except Exception:
                pass
            self.last_error = f"{binary} exited without opening port 30002" + (
                f": {stderr_output}" if stderr_output else ""
            )

        if not self.last_error:
            self.last_error = (
                "No dump1090 binary found on PATH (tried: "
                + ", ".join(self.BINARIES) + "). Run install.sh, or install "
                "dump1090 manually."
            )
        return False

    def shutdown(self) -> None:
        """Gracefully terminate dump1090 if this controller spawned it.
        Never kills an instance we didn't launch ourselves."""
        if self.owns_process and self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
            self.owns_process = False


class AircraftTracker:
    """
    Connects to dump1090's raw beast/avr TCP output and decodes frames
    with pyModeS on a background thread, exposing a thread-safe snapshot
    of currently-tracked aircraft.
    """

    def __init__(self, host: str = DUMP1090_HOST, port: int = DUMP1090_RAW_PORT):
        self.host = host
        self.port = port
        self._aircraft: Dict[str, Aircraft] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.connected = False
        self.last_error = ""
        # Per-ICAO buffer of the most recent even/odd position frames, each
        # entry: {"even": (msg, t), "odd": (msg, t)} — needed because CPR
        # position decoding requires one frame of each parity.
        self._cpr_buffer: Dict[str, dict] = {}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with socket.create_connection((self.host, self.port), timeout=5) as sock:
                    sock.settimeout(1.0)
                    self.connected = True
                    buf = b""
                    while not self._stop.is_set():
                        try:
                            chunk = sock.recv(4096)
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            self._handle_raw_line(line.strip())
            except (ConnectionRefusedError, OSError) as e:
                self.connected = False
                self.last_error = str(e)
                time.sleep(3)

    def _handle_raw_line(self, line: bytes) -> None:
        if not HAVE_PYMODES or not line.startswith(b"*") or not line.endswith(b";"):
            return
        msg = line[1:-1].decode("ascii", errors="ignore")
        if len(msg) < 14:
            return
        try:
            icao = pms.adsb.icao(msg)
            if icao is None:
                return
            df = pms.df(msg)
            if df != 17:  # ADS-B extended squitter only
                return

            with self._lock:
                ac = self._aircraft.setdefault(icao, Aircraft(icao=icao))
                ac.last_seen = time.time()

                tc = pms.adsb.typecode(msg)
                if tc is not None and 1 <= tc <= 4:
                    callsign = pms.adsb.callsign(msg)
                    if callsign:
                        ac.callsign = callsign.strip("_").strip()
                elif tc is not None and (9 <= tc <= 18 or 20 <= tc <= 22):
                    alt = pms.adsb.altitude(msg)
                    if alt is not None:
                        ac.altitude_ft = int(alt)
                    self._buffer_and_decode_position(icao, msg, tc)
                elif tc == 19:
                    vel = pms.adsb.velocity(msg)
                    if vel is not None:
                        spd, hdg, _, _ = vel
                        if spd is not None:
                            ac.speed_kt = spd
                        if hdg is not None:
                            ac.heading_deg = hdg
        except Exception:
            # Malformed/partial frame — pyModeS raises on many edge cases;
            # just drop the frame and keep the stream alive.
            return

    def _buffer_and_decode_position(self, icao: str, msg: str, tc: int) -> None:
        """
        Buffer this airborne-position frame by CPR parity (even/odd) and,
        once both parities are available within a reasonable time window,
        decode a global position via pyModeS and update the Aircraft.
        Must be called with self._lock already held (invoked from within
        _handle_raw_line's locked block).
        """
        try:
            oe_flag = pms.adsb.oe_flag(msg)  # 0 = even, 1 = odd
        except Exception:
            return

        now = time.time()
        entry = self._cpr_buffer.setdefault(icao, {})
        entry["even" if oe_flag == 0 else "odd"] = (msg, now)

        even = entry.get("even")
        odd = entry.get("odd")
        if not even or not odd:
            return
        # CPR position decoding assumes both frames were captured close
        # together (aircraft has moved negligibly); discard stale pairs.
        if abs(even[1] - odd[1]) > 10:
            return

        try:
            position = pms.adsb.position(even[0], odd[0], even[1], odd[1])
        except Exception:
            return

        if position is not None:
            lat, lon = position
            ac = self._aircraft.get(icao)
            if ac is not None:
                ac.lat = lat
                ac.lon = lon

    def snapshot(self, max_age_s: float = 60.0) -> List[Aircraft]:
        now = time.time()
        with self._lock:
            fresh = [a for a in self._aircraft.values() if now - a.last_seen <= max_age_s]
            # prune stale entries
            self._aircraft = {a.icao: a for a in fresh}
            fresh_icaos = set(self._aircraft.keys())
            self._cpr_buffer = {
                icao: buf for icao, buf in self._cpr_buffer.items() if icao in fresh_icaos
            }
            return sorted(fresh, key=lambda a: a.last_seen, reverse=True)


def run(stdscr, ref_lat: float = 0.0, ref_lon: float = 0.0) -> None:
    """
    Main loop for Airplanes mode. `stdscr` is the curses root window.
    ref_lat/ref_lon should come from hardware.detect_gps(); caller passes
    a fallback (e.g. 0,0) if no GPS fix is available.

    Keys: [Q] back to menu   [S] (re)start dump1090
    """
    curses.curs_set(0)
    radar_ui.init_colors()

    height, width = stdscr.getmaxyx()
    left_w = width // 2
    right_w = width - left_w

    radar_win = curses.newwin(height - 1, left_w, 0, 0)
    map_win = curses.newwin((height - 1) // 2, right_w, 0, left_w)
    table_win = curses.newwin(height - 1 - (height - 1) // 2, right_w, (height - 1) // 2, left_w)
    status_win = curses.newwin(1, width, height - 1, 0)

    sweep = radar_ui.RadarSweep(height - 1, left_w, radar_ui.PAIR_GREEN)
    wmap = radar_ui.WorldMap((height - 1) // 2, right_w, radar_ui.PAIR_PURPLE)
    table = radar_ui.DataTable(
        height - 1 - (height - 1) // 2, right_w,
        headers=["ICAO", "CALL", "ALT", "SPD", "DIST", "HDG"],
        col_widths=[8, 10, 8, 7, 8, 6],
    )

    controller = Dump1090Controller()
    tracker: Optional[AircraftTracker] = None

    def _status_line(text: str) -> None:
        """Draw an immediate one-line status message and flush it, used
        while a blocking action (like spawning dump1090) is in progress
        so the terminal doesn't appear to hang."""
        status_win.erase()
        try:
            status_win.addstr(0, 0, text[: width - 1], curses.color_pair(radar_ui.PAIR_YELLOW))
        except curses.error:
            pass
        status_win.refresh()

    def _try_connect() -> None:
        nonlocal tracker
        if not HAVE_PYMODES:
            controller.last_error = (
                "pyModeS not importable — pip install \"pyModeS<3,>=2.13\" "
                "--break-system-packages (v3 removed the API this app uses)"
            )
            return
        _status_line(" Checking for dump1090 on port 30002... ")
        if controller.ensure_running():
            if tracker is None:
                tracker = AircraftTracker()
                tracker.start()
        # If ensure_running() failed, controller.last_error explains why;
        # the main loop's status bar will surface it every frame.

    _try_connect()

    stdscr.nodelay(True)
    stdscr.timeout(150)

    try:
        while True:
            key = stdscr.getch()
            if key in (ord("q"), ord("Q"), 27):
                break
            elif key in (ord("s"), ord("S")):
                # Explicit user-triggered (re)start — covers both "nothing
                # was found on PATH the first time" and "dump1090 died
                # mid-session" cases.
                if tracker is not None:
                    tracker.stop()
                    tracker = None
                controller.shutdown()
                _try_connect()

            sweep.tick()
            aircraft = tracker.snapshot() if tracker is not None else []

            contacts = []
            rows = []
            for ac in aircraft[:50]:
                dist, bearing = ac.distance_bearing_from(ref_lat, ref_lon)
                if dist is not None:
                    contacts.append(
                        radar_ui.RadarContact(
                            range_frac=min(1.0, dist / 150.0),
                            bearing_deg=bearing,
                            glyph="✈",
                            label=ac.callsign or ac.icao,
                        )
                    )
                rows.append([
                    ac.icao,
                    ac.callsign or "----",
                    str(ac.altitude_ft) if ac.altitude_ft else "----",
                    f"{ac.speed_kt:.0f}" if ac.speed_kt else "---",
                    f"{dist:.0f}nm" if dist else "---",
                    f"{bearing:.0f}" if bearing else "---",
                ])

            sweep.draw(radar_win, contacts)

            markers = [
                (ac.lat, ac.lon, "✈", ac.callsign or ac.icao)
                for ac in aircraft if ac.lat is not None and ac.lon is not None
            ]
            wmap.draw(map_win, markers)
            table.draw(table_win, rows, title="Live Feed — ADS-B 1090MHz")

            status_win.erase()
            if tracker is not None and tracker.connected:
                conn_status = "CONNECTED"
                if controller.owns_process:
                    conn_status += f" (launched {controller.launched_binary})"
                pair_color = radar_ui.PAIR_GREEN
            elif tracker is not None:
                conn_status = "LINK LOST — retrying..."
                pair_color = radar_ui.PAIR_YELLOW
            else:
                conn_status = f"NOT RUNNING — {controller.last_error or 'press S to start dump1090'}"
                pair_color = radar_ui.PAIR_RED
            try:
                status_win.addstr(
                    0, 0,
                    f" [Q] Quit  [S] Start/Restart dump1090  |  {conn_status}  |  tracked: {len(aircraft)}  "[: width - 1],
                    curses.color_pair(pair_color),
                )
            except curses.error:
                pass
            status_win.noutrefresh()

            curses.doupdate()
    finally:
        if tracker is not None:
            tracker.stop()
        controller.shutdown()
