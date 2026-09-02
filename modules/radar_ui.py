radar_ui.py — Shared curses rendering primitives for PAR AVION.

Provides:
  - Color pair initialization (green radar, purple map, blue maritime, cyan ISS)
  - A rotating/sweeping circular radar grid renderer
  - A simple ASCII world map with plottable lat/lon markers
  - A generic scrolling data table renderer
  - A spinning ASCII globe (used by ISS mode)

Everything here draws to a passed-in curses window; nothing here owns
the main event loop, so it can be reused across all four operational modes.
"""

from __future__ import annotations

import curses
import math
import time
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence


# ---------------------------------------------------------------------------
# Color pairs
# ---------------------------------------------------------------------------
PAIR_GREEN = 1
PAIR_PURPLE = 2
PAIR_BLUE = 3
PAIR_CYAN = 4
PAIR_YELLOW = 5
PAIR_RED = 6
PAIR_WHITE_DIM = 7


def init_colors() -> None:
    """Call once after curses.initscr()/curses.start_color()."""
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(PAIR_GREEN, curses.COLOR_GREEN, -1)
    curses.init_pair(PAIR_PURPLE, curses.COLOR_MAGENTA, -1)
    curses.init_pair(PAIR_BLUE, curses.COLOR_BLUE, -1)
    curses.init_pair(PAIR_CYAN, curses.COLOR_CYAN, -1)
    curses.init_pair(PAIR_YELLOW, curses.COLOR_YELLOW, -1)
    curses.init_pair(PAIR_RED, curses.COLOR_RED, -1)
    curses.init_pair(PAIR_WHITE_DIM, curses.COLOR_WHITE, -1)


# ---------------------------------------------------------------------------
# Radar sweep
# ---------------------------------------------------------------------------
@dataclass
class RadarContact:
    """A single blip on the radar, in polar coords relative to center."""
    range_frac: float   # 0.0 (center) .. 1.0 (edge)
    bearing_deg: float  # 0 = north/up, clockwise
    glyph: str = "•"
    label: str = ""


class RadarSweep:
    """
    Renders a circular radar grid with range rings, crosshairs, and a
    rotating sweep line. Contacts are plotted as glyphs; a fading trail
    of recent sweep angles gives the "phosphor" look.
    """

    def __init__(self, height: int, width: int, color_pair: int = PAIR_GREEN):
        self.height = height
        self.width = width
        self.color_pair = color_pair
        self.sweep_angle = 0.0
        self.sweep_speed_deg = 6.0  # degrees per tick

    def tick(self) -> None:
        self.sweep_angle = (self.sweep_angle + self.sweep_speed_deg) % 360

    def _center(self) -> tuple:
        return self.height // 2, self.width // 2

    def _radius(self) -> float:
        # Terminal cells are ~2x taller than wide; compress vertical radius.
        return min(self.width / 2 - 2, (self.height / 2 - 2) * 2)

    def draw(self, win, contacts: Optional[Sequence[RadarContact]] = None) -> None:
        cy, cx = self._center()
        r_max = self._radius()
        attr = curses.color_pair(self.color_pair)

        win.erase()
        win.attron(attr)

        # Range rings (4 concentric circles)
        for ring in range(1, 5):
            r = r_max * ring / 4
            self._draw_ellipse(win, cy, cx, r)

        # Crosshairs
        for x in range(max(0, cx - int(r_max)), min(self.width, cx + int(r_max) + 1)):
            win.addch(cy, x, curses.ACS_HLINE if hasattr(curses, "ACS_HLINE") else "-")
        for y in range(max(0, cy - int(r_max / 2)), min(self.height, cy + int(r_max / 2) + 1)):
            try:
                win.addch(y, cx, curses.ACS_VLINE if hasattr(curses, "ACS_VLINE") else "|")
            except curses.error:
                pass

        # Sweep line (fading trail effect via 3 angles behind the leading edge)
        for offset, ch in ((0, "█"), (8, "▓"), (16, "▒"), (24, "░")):
            ang = math.radians(self.sweep_angle - offset)
            for r in range(1, int(r_max)):
                y = cy - int(r * math.cos(ang) * 0.5)
                x = cx + int(r * math.sin(ang))
                if 0 <= y < self.height and 0 <= x < self.width:
                    try:
                        win.addch(y, x, ch)
                    except curses.error:
                        pass

        win.attroff(attr)

        # Contacts (drawn in a distinct bright attr so they pop off the grid)
        if contacts:
            win.attron(attr | curses.A_BOLD)
            for c in contacts:
                ang = math.radians(c.bearing_deg)
                r = c.range_frac * r_max
                y = cy - int(r * math.cos(ang) * 0.5)
                x = cx + int(r * math.sin(ang))
                if 0 <= y < self.height and 0 <= x < self.width - len(c.label) - 2:
                    try:
                        win.addstr(y, x, c.glyph)
                        if c.label:
                            win.addstr(y, x + 1, c.label[: self.width - x - 2])
                    except curses.error:
                        pass
            win.attroff(attr | curses.A_BOLD)

        win.noutrefresh()

    @staticmethod
    def _draw_ellipse(win, cy: int, cx: int, r: float) -> None:
        steps = 72
        for i in range(steps):
            ang = 2 * math.pi * i / steps
            y = cy - int(r * math.cos(ang) * 0.5)
            x = cx + int(r * math.sin(ang))
            try:
                win.addch(y, x, "·")
            except curses.error:
                pass


# ---------------------------------------------------------------------------
# ASCII world map with markers
# ---------------------------------------------------------------------------
class WorldMap:
    """
    Very low-res ASCII world map used as a backdrop for lat/lon markers.
    This is intentionally schematic (a dotted grid), not a coastline atlas —
    accurate coastlines are out of scope for a terminal-cell resolution map.
    """

    def __init__(self, height: int, width: int, color_pair: int = PAIR_PURPLE):
        self.height = height
        self.width = width
        self.color_pair = color_pair

    def latlon_to_cell(self, lat: float, lon: float) -> tuple:
        x = int((lon + 180) / 360 * self.width)
        y = int((90 - lat) / 180 * self.height)
        return max(0, min(self.height - 1, y)), max(0, min(self.width - 1, x))

    def draw(self, win, markers: Iterable[tuple]) -> None:
        """markers: iterable of (lat, lon, glyph, label)"""
        attr = curses.color_pair(self.color_pair)
        win.erase()
        win.attron(attr | curses.A_DIM)
        # Latitude/longitude grid lines every ~30 degrees
        for lat in range(-60, 90, 30):
            y, _ = self.latlon_to_cell(lat, 0)
            for x in range(self.width):
                try:
                    win.addch(y, x, ".")
                except curses.error:
                    pass
        for lon in range(-150, 180, 30):
            _, x = self.latlon_to_cell(0, lon)
            for y in range(self.height):
                try:
                    win.addch(y, x, ".")
                except curses.error:
                    pass
        win.attroff(attr | curses.A_DIM)

        win.attron(attr | curses.A_BOLD)
        for lat, lon, glyph, label in markers:
            y, x = self.latlon_to_cell(lat, lon)
            try:
                win.addstr(y, x, glyph)
                if label and x + len(label) + 1 < self.width:
                    win.addstr(y, x + 1, label)
            except curses.error:
                pass
        win.attroff(attr | curses.A_BOLD)
        win.noutrefresh()


# ---------------------------------------------------------------------------
# Scrolling data table
# ---------------------------------------------------------------------------
class DataTable:
    """Renders a bordered, column-aligned, scrollable feed of rows."""

    def __init__(self, height: int, width: int, headers: Sequence[str],
                 col_widths: Sequence[int], color_pair: int = PAIR_WHITE_DIM):
        self.height = height
        self.width = width
        self.headers = headers
        self.col_widths = col_widths
        self.color_pair = color_pair

    def draw(self, win, rows: Sequence[Sequence[str]], title: str = "") -> None:
        win.erase()
        win.border()
        if title:
            win.addstr(0, 2, f" {title} ", curses.color_pair(self.color_pair) | curses.A_BOLD)

        header_line = "".join(h.ljust(w) for h, w in zip(self.headers, self.col_widths))
        try:
            win.addstr(1, 2, header_line[: self.width - 4], curses.color_pair(self.color_pair) | curses.A_UNDERLINE)
        except curses.error:
            pass

        max_rows = self.height - 3
        for i, row in enumerate(rows[-max_rows:]):
            line = "".join(str(c).ljust(w) for c, w in zip(row, self.col_widths))
            try:
                win.addstr(2 + i, 2, line[: self.width - 4], curses.color_pair(self.color_pair))
            except curses.error:
                pass
        win.noutrefresh()


# ---------------------------------------------------------------------------
# Spinning ASCII globe (ISS mode)
# ---------------------------------------------------------------------------
_GLOBE_SHADES = " .:-=+*#%@"


class SpinningGlobe:
    """
    A crude 3D-projected sphere rendered with shading characters, rotated
    over time to give a "spinning globe" effect in a small panel.
    """

    def __init__(self, height: int, width: int, color_pair: int = PAIR_CYAN):
        self.height = height
        self.width = width
        self.color_pair = color_pair
        self.rotation = 0.0

    def tick(self, speed: float = 0.12) -> None:
        self.rotation += speed

    def draw(self, win, sub_lat: Optional[float] = None, sub_lon: Optional[float] = None) -> None:
        """sub_lat/sub_lon: ISS's current sub-satellite point, marked with 'X'."""
        attr = curses.color_pair(self.color_pair)
        win.erase()
        win.attron(attr)

        cy, cx = self.height / 2, self.width / 2
        radius = min(self.height, self.width / 2) / 2 - 1

        for row in range(self.height):
            for col in range(self.width):
                # Normalize to unit circle in "screen" space (x squashed for aspect)
                ny = (row - cy) / radius
                nx = (col - cx) / (radius * 2)
                d2 = nx * nx + ny * ny
                if d2 > 1.0:
                    continue
                nz = math.sqrt(1.0 - d2)
                # Simple rotating light source for shading
                light = math.sin(self.rotation) * nx + math.cos(self.rotation) * nz
                shade_idx = int((light + 1) / 2 * (len(_GLOBE_SHADES) - 1))
                shade_idx = max(0, min(len(_GLOBE_SHADES) - 1, shade_idx))
                ch = _GLOBE_SHADES[shade_idx]
                try:
                    win.addch(row, col, ch)
                except curses.error:
                    pass

        win.attroff(attr)

        if sub_lat is not None and sub_lon is not None:
            # Project the sub-satellite point onto the same sphere using
            # current rotation as the globe's "front-facing" longitude.
            lat_r = math.radians(sub_lat)
            lon_r = math.radians(sub_lon) + self.rotation
            x3 = math.cos(lat_r) * math.sin(lon_r)
            y3 = math.sin(lat_r)
            z3 = math.cos(lat_r) * math.cos(lon_r)
            if z3 > 0:  # only draw if on the visible (near) hemisphere
                row = int(cy - y3 * radius)
                col = int(cx + x3 * radius * 2)
                if 0 <= row < self.height and 0 <= col < self.width:
                    try:
                        win.addstr(row, col, "X", curses.color_pair(PAIR_YELLOW) | curses.A_BOLD)
                    except curses.error:
                        pass

        win.noutrefresh()


def draw_banner(win, text_lines: Sequence[str], color_pair: int = PAIR_GREEN) -> None:
    """Draw a multi-line ASCII banner centered horizontally in win."""
    height, width = win.getmaxyx()
    attr = curses.color_pair(color_pair) | curses.A_BOLD
    win.attron(attr)
    for i, line in enumerate(text_lines):
        x = max(0, (width - len(line)) // 2)
        if i < height:
            try:
                win.addstr(i, x, line)
            except curses.error:
                pass
    win.attroff(attr)
