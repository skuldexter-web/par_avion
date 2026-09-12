#!/usr/bin/env python3
"""
PAR AVION — Tactical RF & Telemetry Suite v2.0
Main CLI entry point and TUI engine.

Usage:
    python3 par_avion.py

A cyberpunk-styled curses menu that dispatches into one of seven modes:
  1) Airplanes  — ADS-B 1090MHz decode via dump1090, tactical radar + map
  2) Waterfalls — RTL-SDR spectrum analyzer, rolling ASCII waterfall
  3) Radio      — Broadcast AM/FM audio demodulator & tuner
  4) Maritime   — AIS vessel tracking via rtl_ais, tactical marine radar
  5) ISS        — NORAD TLE orbit propagation + pass predictor
  6) SSTV       — Slow Scan TV image decoder (Martin/Scottie/Robot)
  7) Morse      — CW/Morse code audio decoder & visualizer

All operational modes are receive-only: they decode signals that are
already broadcast in the clear (ADS-B, AIS, published TLE data, amateur
SSTV/CW transmissions on their conventional calling frequencies) and
render/demodulate them locally. No mode transmits on any RF interface.
"""

from __future__ import annotations

import curses
import os
import sys

# Ensure this script's own directory is on sys.path so `modules` resolves
# regardless of the caller's cwd or how the script was invoked (absolute
# path, symlink, etc.) — normally Python does this automatically, but it
# can be bypassed by some launchers/wrappers.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules import airplanes, hardware, iss, maritime, morse, radar_ui, radio, sstv, waterfall

BANNER = [
    r" ██▓███   ▄▄▄       ██▀███      ▄▄▄       ██▒   █▓ ██▓ ▒█████   ███▄    █ ",
    r"▓██░  ██▒▒████▄    ▓██ ▒ ██▒   ▒████▄    ▓██░   █▒▓██▒▒██▒  ██▒ ██ ▀█   █ ",
    r"▓██░ ██▓▒▒██  ▀█▄  ▓██ ░▄█ ▒   ▒██  ▀█▄   ▓██  █▒░▒██▒▒██░  ██▒▓██  ▀█ ██▒",
    r"▒██▄█▓▒ ▒░██▄▄▄▄██ ▒██▀▀█▄     ░██▄▄▄▄██   ▒██ █░░░██░▒██   ██░▓██▒  ▐▌██▒",
    r"▒██▒ ░  ░ ▓█   ▓██▒░██▓ ▒██▒    ▓█   ▓██▒   ▒▀█░  ░██░░ ████▓▒░▒██░   ▓██░",
    r"▒▓▒░ ░  ░ ▒▒   ▓▒█░░ ▒▓ ░▒▓░    ▒▒   ▓▒█░   ░ ▐░  ░▓  ░ ▒░▒░▒░ ░ ▒░   ▒ ▒ ",
    r"░▒ ░      ▒   ▒▒ ░  ░▒ ░ ▒░     ▒   ▒▒ ░   ░ ░░   ▒ ░  ░ ▒ ▒░ ░ ░░   ░ ▒░",
    r"░░        ░   ▒     ░░   ░      ░   ▒        ░░   ▒ ░░ ░ ░ ▒     ░   ░ ░ ",
    r"              ░  ░   ░              ░  ░       ░   ░      ░ ░           ░ ",
]

TAGLINE = "— Tactical RF & Telemetry Suite v2.0 —"

MENU_ITEMS = [
    ("1", "Airplanes", "ADS-B 1090MHz + Dynamic Tactical Radar"),
    ("2", "Waterfalls", "Spectrum Analyzer & Rolling ASCII Waterfall"),
    ("3", "Radio", "Broadcast AM/FM Audio Demodulator & Tuner"),
    ("4", "Maritime", "AIS Vessel Tracking + Tactical Marine Radar"),
    ("5", "ISS", "ISS Orbit Pass Predictor & Spinning Globe"),
    ("6", "SSTV", "Slow Scan TV Decoder: Martin, Scottie, Robot"),
    ("7", "Morse", "CW / Morse Code Audio Decoder & Visualizer"),
    ("Q", "Quit", ""),
]


def draw_menu(stdscr, hw_report, gps_lat, gps_lon) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()

    banner_win_h = len(BANNER) + 2
    banner_win = curses.newwin(banner_win_h, width, 1, 0)
    radar_ui.draw_banner(banner_win, BANNER, radar_ui.PAIR_GREEN)
    tagline_x = max(0, (width - len(TAGLINE)) // 2)
    try:
        banner_win.addstr(len(BANNER), tagline_x, TAGLINE,
                           curses.color_pair(radar_ui.PAIR_CYAN))
    except curses.error:
        pass
    banner_win.noutrefresh()

    menu_top = banner_win_h + 2
    divider = "=" * min(width - 2, 54)
    menu_x = max(0, (width - len(divider)) // 2)

    lines = [divider, "  PAR AVION — Tactical RF & Telemetry Suite v2.0", divider]
    for key, label, desc in MENU_ITEMS:
        if desc:
            lines.append(f"  [ {key} ] {label:<10} ({desc})")
        else:
            lines.append(f"  [ {key} ] {label}")
    lines.append(divider)

    for i, line in enumerate(lines):
        y = menu_top + i
        if y >= height - 4:
            break
        try:
            stdscr.addstr(y, menu_x, line, curses.color_pair(radar_ui.PAIR_GREEN))
        except curses.error:
            pass

    # Hardware status footer
    status_y = min(height - 3, menu_top + len(lines) + 1)
    sdr_status = f"SDR: {len(hw_report.sdrs)} detected" if hw_report.sdrs else "SDR: NONE DETECTED"
    if gps_lat is not None and gps_lon is not None:
        gps_status = f"GPS: {gps_lat:.4f},{gps_lon:.4f}"
    else:
        gps_status = "GPS: NO FIX (radar modes will use relative spatial estimation)"
    try:
        stdscr.addstr(status_y, menu_x, sdr_status, curses.color_pair(radar_ui.PAIR_YELLOW))
        stdscr.addstr(status_y + 1, menu_x, gps_status, curses.color_pair(radar_ui.PAIR_YELLOW))
    except curses.error:
        pass

    if hw_report.errors and status_y + 3 < height:
        try:
            stdscr.addstr(status_y + 3, menu_x, "! " + hw_report.errors[0][:width - menu_x - 4],
                          curses.color_pair(radar_ui.PAIR_RED))
        except curses.error:
            pass

    stdscr.noutrefresh()
    curses.doupdate()


def main(stdscr) -> None:
    curses.curs_set(0)
    curses.noecho()
    curses.cbreak()
    stdscr.keypad(True)
    radar_ui.init_colors()

    hw_report = hardware.full_report()
    gps_lat, gps_lon = None, None
    if hw_report.gps and hw_report.gps.fix:
        gps_lat, gps_lon = hw_report.gps.lat, hw_report.gps.lon

    stdscr.nodelay(False)
    stdscr.timeout(-1)

    while True:
        draw_menu(stdscr, hw_report, gps_lat, gps_lon)
        key = stdscr.getch()
        ch = chr(key).upper() if 0 <= key < 256 else ""

        if ch == "Q":
            break
        elif ch == "1":
            _safe_dispatch(airplanes.run, stdscr, gps_lat, gps_lon)
        elif ch == "2":
            _safe_dispatch(waterfall.run, stdscr)
        elif ch == "3":
            _safe_dispatch(radio.run, stdscr)
        elif ch == "4":
            _safe_dispatch(maritime.run, stdscr, gps_lat, gps_lon)
        elif ch == "5":
            _safe_dispatch(iss.run, stdscr, gps_lat, gps_lon)
        elif ch == "6":
            _safe_dispatch(sstv.run, stdscr)
        elif ch == "7":
            _safe_dispatch(morse.run, stdscr)
        # Re-check hardware occasionally in case devices were hot-plugged
        # while sitting at the menu (cheap enough to just redo each loop).
        hw_report = hardware.full_report()
        if hw_report.gps and hw_report.gps.fix:
            gps_lat, gps_lon = hw_report.gps.lat, hw_report.gps.lon
        else:
            gps_lat, gps_lon = None, None


def _safe_dispatch(fn, *args) -> None:
    """Run a mode's `run()` function, ensuring curses state is sane even
    if the mode raises, so we always return cleanly to the main menu."""
    stdscr = args[0]
    try:
        fn(*args)
    except curses.error:
        pass
    except Exception as e:
        stdscr.nodelay(False)
        stdscr.erase()
        try:
            stdscr.addstr(0, 0, f"Mode crashed: {e}")
            stdscr.addstr(1, 0, "Press any key to return to menu...")
        except curses.error:
            pass
        stdscr.refresh()
        stdscr.getch()
    finally:
        stdscr.nodelay(False)
        curses.curs_set(0)


def check_python_version() -> None:
    if sys.version_info < (3, 8):
        print("PAR AVION requires Python 3.8+.")
        sys.exit(1)


if __name__ == "__main__":
    check_python_version()
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    finally:
        print("PAR AVION — session terminated. All background processes cleaned up.")
