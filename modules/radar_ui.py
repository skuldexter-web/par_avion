"""
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
    distance_nm: Optional[float] = None


_COMPASS_8PT = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def bearing_to_compass(bearing_deg: float) -> str:
    """Convert a bearing in degrees to an 8-point compass label."""
    idx = int(((bearing_deg % 360) + 22.5) // 45) % 8
    return _COMPASS_8PT[idx]


class RadarSweep:
    """
    Renders a circular radar grid with range rings, crosshairs, a rotating
    sweep line, and a fixed N/E/S/W compass overlay. Contacts are plotted
    as glyphs on the grid; when GPS is unavailable, contacts are instead
    scattered at pseudo-random (but stable per-contact) grid positions and
    a warning banner is shown, since true bearing/range cannot be computed
    without a reference origin.

    A contact list panel (append via `draw`'s `contact_list_width`) shows
    each contact's compass direction and distance in text form alongside
    the grid, since small terminal cells make on-grid labels hard to read
    at a glance.
    """

    def __init__(self, height: int, width: int, color_pair: int = PAIR_GREEN,
                 title: str = ""):
        self.height = height
        self.width = width
        self.color_pair = color_pair
        self.title = title
        self.sweep_angle = 0.0
        self.sweep_speed_deg = 6.0  # degrees per tick

    def tick(self) -> None:
        self.sweep_angle = (self.sweep_angle + self.sweep_speed_deg) % 360

    def _center(self) -> tuple:
        # Leave a blank row at the top for the title bar, matching the
        # tactical-scope look of a labeled header above the scope itself.
        top_margin = 1 if self.title else 0
        usable_h = self.height - top_margin
        return top_margin + usable_h // 2, self.width // 2

    def _radius(self) -> float:
        top_margin = 1 if self.title else 0
        usable_h = self.height - top_margin
        # Reserve room for the compass labels, which are drawn just
        # outside the outermost ring (compass_r = r_max + 1) — without
        # this margin the E/W letters land right at the panel edge (or
        # past it) and the range-ring labels along the crosshair have no
        # clean space either. Reserve 3 columns horizontally (label +
        # gap) and 2 rows vertically.
        return min(self.width / 2 - 4, (usable_h / 2 - 3) * 2)

    def draw(self, win, contacts: Optional[Sequence[RadarContact]] = None,
              gps_available: bool = True, max_range_nm: Optional[float] = None) -> None:
        """
        If gps_available is False, `contacts`' range_frac/bearing_deg are
        ignored and replaced with a stable pseudo-random position derived
        from each contact's label/glyph (so a given aircraft/vessel stays
        in the same spot frame-to-frame instead of jittering), and a
        warning banner is drawn across the top of the grid.

        max_range_nm, if given, is printed as range-ring labels along the
        horizontal crosshair (e.g. "20NM 40NM 60NM" for a 60NM max range,
        one label per ring) — purely cosmetic, doesn't affect placement
        math (callers are expected to have already normalized each
        contact's range_frac against this same max range).
        """
        cy, cx = self._center()
        r_max = self._radius()
        attr = curses.color_pair(self.color_pair)

        win.erase()

        if self.title:
            title_text = f"\u25c4 {self.title} \u25ba"
            tx = max(0, (self.width - len(title_text)) // 2)
            try:
                win.addstr(0, tx, title_text, attr | curses.A_BOLD)
            except curses.error:
                pass

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

        win.attroff(attr)

        # Range-ring labels directly on the horizontal crosshair line
        # (e.g. "20NM  40NM  60NM"), matching the reference scope's
        # readout — placed AFTER the crosshair/rings are drawn (below)
        # so the label text overwrites the line/dot characters at those
        # exact columns rather than competing with them for the same
        # cells one row off, which visually collided with the circular
        # grid's dots.
        if max_range_nm and max_range_nm > 0:
            for ring in range(1, 5):
                ring_r = r_max * ring / 4
                ring_range = max_range_nm * ring / 4
                label = f"{ring_range:.0f}NM"
                lx = cx + int(ring_r) - len(label) // 2
                if 0 <= cy < self.height and 0 <= lx and lx + len(label) < self.width:
                    try:
                        win.addstr(cy, lx, label, attr)
                    except curses.error:
                        pass

        win.attron(attr)
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

        # Compass overlay: N top-center, E right-center, S bottom-center,
        # W left-center, drawn just outside the outermost range ring.
        # E/W are offset one row above center (not on row cy itself) so
        # they don't collide with the range-ring labels, which are drawn
        # directly on the horizontal crosshair (row cy) — see below.
        win.attron(attr | curses.A_BOLD)
        compass_r = r_max + 1
        top_margin = 1 if self.title else 0
        compass_row_offset = 1
        try:
            win.addstr(max(top_margin, cy - int(compass_r * 0.5)), cx, "N")
        except curses.error:
            pass
        try:
            win.addstr(cy - compass_row_offset, min(self.width - 2, cx + int(compass_r)), "E")
        except curses.error:
            pass
        try:
            win.addstr(min(self.height - 1, cy + int(compass_r * 0.5)), cx, "S")
        except curses.error:
            pass
        try:
            win.addstr(cy - compass_row_offset, max(0, cx - int(compass_r) - 1), "W")
        except curses.error:
            pass
        win.attroff(attr | curses.A_BOLD)

        # Contacts (drawn in a distinct bright attr so they pop off the grid)
        if contacts:
            win.attron(attr | curses.A_BOLD)
            for c in contacts:
                if gps_available:
                    ang = math.radians(c.bearing_deg)
                    # Clamp defensively: callers normalize range_frac
                    # against the same max_range_nm used for the ring
                    # labels above, but clamp here too so a contact
                    # beyond the displayed range still shows at the
                    # outer ring rather than being silently skipped or
                    # drawn off-grid.
                    r = max(0.0, min(1.0, c.range_frac)) * r_max
                else:
                    # Stable pseudo-random placement keyed on the contact's
                    # own identity, so it doesn't jump around every frame.
                    seed = sum(ord(ch) for ch in (c.label or c.glyph)) or 1
                    ang = math.radians((seed * 47) % 360)
                    r = r_max * (0.25 + ((seed * 13) % 100) / 133.0)
                y = cy - int(r * math.cos(ang) * 0.5)
                x = cx + int(r * math.sin(ang))
                if 0 <= y < self.height and 0 <= x < self.width - len(c.label) - 2:
                    try:
                        # Blank the label's footprint (plus a 1-char pad
                        # on each side) first so it reads cleanly against
                        # the background dot/line grid instead of
                        # visually merging with it — a label drawn
                        # straight over "·" and "─" characters with no
                        # clearing was hard to read once several contacts
                        # landed inside the ring pattern, and blanking
                        # only the label's own width left grid dots on
                        # immediately-adjacent cells still visually
                        # touching the text.
                        pad_x = max(0, x - 1)
                        blank_width = min(self.width - pad_x, 1 + len(c.label) + 2)
                        win.addstr(y, pad_x, " " * blank_width)
                        win.addstr(y, x, c.glyph)
                        if c.label:
                            win.addstr(y, x + 1, c.label[: self.width - x - 2])
                    except curses.error:
                        pass
            win.attroff(attr | curses.A_BOLD)

        # GPS-offline warning banner across the top of the grid.
        if not gps_available:
            banner = "[GPS OFFLINE - RELATIVE SPATIAL ESTIMATION ACTIVE]"
            bx = max(0, (self.width - len(banner)) // 2)
            top_margin = 1 if self.title else 0
            try:
                win.addstr(top_margin, bx, banner[: self.width], curses.color_pair(PAIR_RED) | curses.A_BOLD)
            except curses.error:
                pass

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


class StatusBlock:
    """
    Compact bottom-of-panel status readout (STATUS / CONTACTS / RANGE /
    INTERVAL / NEXT UPDATE style), matching a classic ATC-scope status
    strip. Purely a label:value list — the caller supplies whatever
    rows are relevant to that mode.
    """

    def __init__(self, height: int, width: int, color_pair: int = PAIR_GREEN):
        self.height = height
        self.width = width
        self.color_pair = color_pair

    def draw(self, win, rows: Sequence[tuple]) -> None:
        """rows: sequence of (label, value) pairs, one per line."""
        win.erase()
        attr = curses.color_pair(self.color_pair)
        for i, (label, value) in enumerate(rows[: self.height]):
            line = f"{label}: {value}"
            try:
                win.addstr(i, 0, line[: self.width - 1], attr)
            except curses.error:
                pass
        win.noutrefresh()


class ContactListPanel:
    """
    Text sidebar listing each radar contact's compass direction and
    distance — a readable complement to the on-grid glyphs, since 8-point
    compass + range in a fixed-width list is easier to scan than labels
    crammed onto a small polar grid.
    """

    def __init__(self, height: int, width: int, color_pair: int = PAIR_GREEN):
        self.height = height
        self.width = width
        self.color_pair = color_pair

    def draw(self, win, contacts: Sequence[RadarContact], gps_available: bool = True,
              title: str = "CONTACTS") -> None:
        win.erase()
        win.border()
        attr = curses.color_pair(self.color_pair)
        try:
            win.addstr(0, 2, f" {title} ", attr | curses.A_BOLD)
        except curses.error:
            pass

        if not gps_available:
            try:
                win.addstr(1, 2, "NO GPS FIX"[: self.width - 4], curses.color_pair(PAIR_RED))
            except curses.error:
                pass
            row_start = 2
        else:
            row_start = 1

        max_rows = self.height - row_start - 1
        for i, c in enumerate(contacts[:max_rows]):
            direction = bearing_to_compass(c.bearing_deg) if gps_available else "--"
            dist_str = f"{c.distance_nm:.1f}nm" if c.distance_nm is not None else "---"
            label = (c.label or "")[: max(0, self.width - 14)]
            line = f"{c.glyph} {direction:<3}{dist_str:>8}  {label}"
            try:
                win.addstr(row_start + i, 2, line[: self.width - 4], attr)
            except curses.error:
                pass
        win.noutrefresh()


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
                 col_widths: Sequence[int], color_pair: int = PAIR_WHITE_DIM,
                 reserved_bottom_rows: int = 0):
        self.height = height
        self.width = width
        self.headers = headers
        self.col_widths = col_widths
        self.color_pair = color_pair
        # Rows of vertical space to leave blank at the bottom of the
        # panel (e.g. for a StatusBlock drawn as a derived sub-window
        # over the same area) — without this, a long enough feed would
        # draw table rows straight through whatever's reserved below.
        self.reserved_bottom_rows = reserved_bottom_rows

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

        max_rows = self.height - 3 - self.reserved_bottom_rows
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
