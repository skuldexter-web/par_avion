"""
iss.py — ISS TLE satellite tracking & pass predictor for PAR AVION.

Downloads current NORAD two-line element (TLE) data for the ISS from
CelesTrak (a standard, publicly-published orbital element source used by
amateur satellite trackers worldwide) and uses Skyfield for orbit
propagation: current sub-satellite lat/lon, and next overhead pass ETA
for the operator's location.
"""

from __future__ import annotations

import curses
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

try:
    from skyfield.api import EarthSatellite, Topos, load
    HAVE_SKYFIELD = True
except ImportError:
    HAVE_SKYFIELD = False

from . import radar_ui

TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE"
ISS_NAME = "ISS (ZARYA)"

_TLE_CACHE_TTL_S = 6 * 3600  # NORAD TLEs are typically refreshed every few hours


@dataclass
class TLEData:
    line1: str
    line2: str
    fetched_at: float


@dataclass
class PassPrediction:
    rise_time: Optional[datetime] = None
    culminate_time: Optional[datetime] = None
    set_time: Optional[datetime] = None
    max_elevation_deg: Optional[float] = None


def fetch_tle(timeout_s: float = 10.0) -> Optional[TLEData]:
    """Downloads the latest ISS TLE from CelesTrak. Returns None on failure
    (caller should fall back to a cached/bundled TLE if available)."""
    try:
        resp = requests.get(TLE_URL, timeout=timeout_s)
        resp.raise_for_status()
        lines = [ln.strip() for ln in resp.text.strip().splitlines() if ln.strip()]
        # Response may or may not include the name line depending on FORMAT
        line1 = next(l for l in lines if l.startswith("1 "))
        line2 = next(l for l in lines if l.startswith("2 "))
        return TLEData(line1=line1, line2=line2, fetched_at=time.time())
    except Exception:
        return None


class ISSTracker:
    def __init__(self, observer_lat: float = 0.0, observer_lon: float = 0.0):
        self.observer_lat = observer_lat
        self.observer_lon = observer_lon
        self.tle: Optional[TLEData] = None
        self.satellite = None
        self.ts = None
        self.last_error = ""

        if HAVE_SKYFIELD:
            self.ts = load.timescale()
        self.refresh_tle()

    def refresh_tle(self) -> bool:
        if self.tle and (time.time() - self.tle.fetched_at) < _TLE_CACHE_TTL_S:
            return True
        fresh = fetch_tle()
        if fresh is None:
            self.last_error = "Could not reach CelesTrak for TLE update."
            return self.tle is not None  # keep using stale cache if we have one
        self.tle = fresh
        if HAVE_SKYFIELD:
            self.satellite = EarthSatellite(fresh.line1, fresh.line2, ISS_NAME, self.ts)
        return True

    def current_subpoint(self):
        """Returns (lat, lon, altitude_km) or (None, None, None) if unavailable."""
        if not HAVE_SKYFIELD or self.satellite is None or self.ts is None:
            return None, None, None
        t = self.ts.now()
        geocentric = self.satellite.at(t)
        subpoint = geocentric.subpoint()
        return subpoint.latitude.degrees, subpoint.longitude.degrees, subpoint.elevation.km

    def next_pass(self, search_hours: float = 48.0) -> PassPrediction:
        """Finds the next overhead pass (elevation-based rise/set) for the
        configured observer location within the search window."""
        pred = PassPrediction()
        if not HAVE_SKYFIELD or self.satellite is None or self.ts is None:
            return pred

        observer = Topos(
            latitude_degrees=self.observer_lat,
            longitude_degrees=self.observer_lon,
        )
        t0 = self.ts.now()
        t1 = self.ts.from_datetime(
            datetime.now(timezone.utc) + timedelta(hours=search_hours)
        )

        try:
            times, events = self.satellite.find_events(
                observer, t0, t1, altitude_degrees=10.0
            )
        except Exception as e:
            self.last_error = f"Pass prediction failed: {e}"
            return pred

        # events: 0=rise, 1=culminate, 2=set — find the first complete rise..set
        for i, event in enumerate(events):
            if event == 0 and pred.rise_time is None:
                pred.rise_time = times[i].utc_datetime()
            elif event == 1 and pred.rise_time is not None and pred.culminate_time is None:
                pred.culminate_time = times[i].utc_datetime()
                difference = self.satellite - observer
                topocentric = difference.at(times[i])
                alt, _, _ = topocentric.altaz()
                pred.max_elevation_deg = alt.degrees
            elif event == 2 and pred.rise_time is not None and pred.set_time is None:
                pred.set_time = times[i].utc_datetime()
                break

        return pred


def _format_eta(target: Optional[datetime]) -> str:
    if target is None:
        return "unknown"
    now = datetime.now(timezone.utc)
    delta = target - now
    if delta.total_seconds() < 0:
        return "now"
    total_min = int(delta.total_seconds() // 60)
    h, m = divmod(total_min, 60)
    return f"{h:02d}h {m:02d}m"


def run(stdscr, ref_lat: float = 0.0, ref_lon: float = 0.0) -> None:
    curses.curs_set(0)
    radar_ui.init_colors()

    height, width = stdscr.getmaxyx()
    globe_w = width // 2
    globe_win = curses.newwin(height - 1, globe_w, 0, 0)
    info_win = curses.newwin(height - 1, width - globe_w, 0, globe_w)
    status_win = curses.newwin(1, width, height - 1, 0)
    globe = radar_ui.SpinningGlobe(height - 1, globe_w, radar_ui.PAIR_CYAN)

    def _status_line(text: str) -> None:
        status_win.erase()
        try:
            status_win.addstr(0, 0, text[: width - 1], curses.color_pair(radar_ui.PAIR_YELLOW))
        except curses.error:
            pass
        status_win.refresh()

    _status_line(" Fetching ISS TLE from CelesTrak... ")
    tracker = ISSTracker(observer_lat=ref_lat, observer_lon=ref_lon)
    pass_pred = tracker.next_pass()
    last_pass_check = time.time()

    stdscr.nodelay(True)
    stdscr.timeout(200)

    while True:
        key = stdscr.getch()
        if key in (ord("q"), ord("Q"), 27):
            break
        elif key == ord("r"):
            _status_line(" Refreshing TLE... ")
            tracker.refresh_tle()
            pass_pred = tracker.next_pass()

        # Periodically recompute the next pass (every 5 minutes)
        if time.time() - last_pass_check > 300:
            pass_pred = tracker.next_pass()
            last_pass_check = time.time()

        globe.tick()
        lat, lon, alt_km = tracker.current_subpoint()
        globe.draw(globe_win, sub_lat=lat, sub_lon=lon)

        info_win.erase()
        info_win.border()
        info_win.addstr(1, 2, " ISS — ORBITAL TELEMETRY ",
                         curses.color_pair(radar_ui.PAIR_CYAN) | curses.A_BOLD)

        row = 3
        if lat is not None:
            info_win.addstr(row, 2, f"Sub-satellite point: {lat:+.2f}, {lon:+.2f}")
            row += 1
            info_win.addstr(row, 2, f"Altitude: {alt_km:.1f} km")
            row += 2
        else:
            reason = "skyfield not installed" if not HAVE_SKYFIELD else "no TLE data"
            info_win.addstr(row, 2, f"Position unavailable ({reason})")
            row += 2

        info_win.addstr(row, 2, "NEXT PASS (your location):",
                         curses.A_UNDERLINE)
        row += 1
        info_win.addstr(row, 2, f"Rise:   {_format_eta(pass_pred.rise_time)}")
        row += 1
        info_win.addstr(row, 2, f"Peak:   {_format_eta(pass_pred.culminate_time)}"
                                 + (f"  (max el {pass_pred.max_elevation_deg:.0f}°)"
                                    if pass_pred.max_elevation_deg else ""))
        row += 1
        info_win.addstr(row, 2, f"Set:    {_format_eta(pass_pred.set_time)}")
        row += 2

        tle_age = "never fetched" if not tracker.tle else \
            f"{int((time.time() - tracker.tle.fetched_at) / 60)} min ago"
        info_win.addstr(row, 2, f"TLE fetched: {tle_age}")
        if tracker.last_error:
            row += 1
            info_win.addstr(row, 2, tracker.last_error[: (width - globe_w) - 4],
                             curses.color_pair(radar_ui.PAIR_YELLOW))

        info_win.noutrefresh()

        status_win.erase()
        status_win.addstr(0, 0, " [Q] Quit  |  [R] Refresh TLE  ",
                           curses.color_pair(radar_ui.PAIR_CYAN))
        status_win.noutrefresh()
        curses.doupdate()
