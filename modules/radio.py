"""
radio.py — Broadcast AM/FM audio demodulator & tuner for PAR AVION.

Tunes an RTL-SDR to a broadcast FM (88.0-108.0 MHz) or AM frequency and
streams demodulated audio to the system's speakers by piping `rtl_fm`
(part of the rtl-sdr package) into `sox`'s `play` command — the standard
receive-only chain used throughout the SDR hobbyist community. This
module does not transmit; it only tunes a receiver and plays back
whatever audio the SDR demodulates.

Requires: `rtl_fm` (from rtl-sdr) and `play` (from sox) on PATH — both
installed by install.sh.
"""

from __future__ import annotations

import curses
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import radar_ui

# ---------------------------------------------------------------------------
# Band limits and presets
# ---------------------------------------------------------------------------
FM_BAND_HZ = (88_000_000, 108_000_000)
AM_BAND_HZ = (530_000, 1_700_000)

FM_STEP_HZ = 200_000   # broadcast FM channel spacing (US); 100kHz elsewhere
AM_STEP_HZ = 10_000    # broadcast AM channel spacing (US); 9kHz elsewhere
FINE_STEP_HZ = 10_000

PRESETS: List[Tuple[str, int, str]] = [
    ("FM 88.5", 88_500_000, "fm"),
    ("FM 94.9", 94_900_000, "fm"),
    ("FM 101.1", 101_100_000, "fm"),
    ("FM 107.9", 107_900_000, "fm"),
    ("AM 1010", 1_010_000, "am"),
    ("AM 1350", 1_350_000, "am"),
]


@dataclass
class TunerState:
    mode: str = "fm"  # "fm" or "am"
    freq_hz: int = 101_100_000
    volume_pct: int = 70
    squelch_db: int = 0  # 0 = squelch off

    def step_hz(self) -> int:
        return FM_STEP_HZ if self.mode == "fm" else AM_STEP_HZ

    def band_limits(self) -> Tuple[int, int]:
        return FM_BAND_HZ if self.mode == "fm" else AM_BAND_HZ

    def nudge(self, delta_hz: int) -> None:
        lo, hi = self.band_limits()
        self.freq_hz = max(lo, min(hi, self.freq_hz + delta_hz))

    def toggle_mode(self) -> None:
        # FM and AM occupy completely different frequency ranges (MHz vs
        # kHz), so simply clamping freq_hz into the new band's limits
        # would silently snap to whatever edge happens to be nearest in
        # raw Hz terms (e.g. FM 101.1MHz -> AM 1700kHz, the AM band's
        # max) rather than anything resembling a real station. Jump to
        # the first preset for the new mode instead, which is always a
        # real, useful frequency.
        new_mode = "am" if self.mode == "fm" else "fm"
        self.mode = new_mode
        first_preset = next((p for p in PRESETS if p[2] == new_mode), None)
        if first_preset is not None:
            self.freq_hz = first_preset[1]
        else:
            lo, hi = self.band_limits()
            self.freq_hz = max(lo, min(hi, self.freq_hz))

    def apply_preset(self, index: int) -> None:
        index = index % len(PRESETS)
        _, freq, mode = PRESETS[index]
        self.mode = mode
        self.freq_hz = freq


class AudioTunerController:
    """
    Manages the rtl_fm -> play subprocess pipeline. rtl_fm demodulates
    FM/AM to a raw PCM stream on stdout; play (sox) reads that stream
    and outputs it to the system's default audio device.
    """

    def __init__(self):
        self.rtl_fm_proc: Optional[subprocess.Popen] = None
        self.play_proc: Optional[subprocess.Popen] = None
        self.last_error = ""
        self.tools_available = self._check_tools()

    @staticmethod
    def _check_tools() -> bool:
        return shutil.which("rtl_fm") is not None and shutil.which("play") is not None

    def missing_tools(self) -> List[str]:
        missing = []
        if shutil.which("rtl_fm") is None:
            missing.append("rtl_fm (package: rtl-sdr)")
        if shutil.which("play") is None:
            missing.append("play (package: sox)")
        return missing

    def start(self, tuner: TunerState) -> bool:
        self.stop()
        if not self.tools_available:
            self.last_error = "Missing tools: " + ", ".join(self.missing_tools())
            return False

        # rtl_fm demodulation mode: plain "fm" (not the "wbfm" shorthand)
        # for broadcast FM, with de-emphasis explicitly enabled via
        # -E deemp — de-emphasis undoes the treble boost FM broadcast
        # transmitters apply before transmission, and without it audio
        # sounds thin/harsh even though the frequency demodulation
        # itself is otherwise correct. The "wbfm" shorthand documented
        # by rtl_fm (-M fm -s 170k -o 4 -A fast -r 32k -l 0 -E deemp)
        # already includes deemp, but bundles a fixed sample/output rate
        # with it — using plain "fm" plus explicit flags keeps our own
        # -s/-r choices unambiguous rather than depending on how rtl_fm's
        # argument parser resolves a later flag against an earlier
        # shorthand's implied value.
        if tuner.mode == "fm":
            demod_args = ["-M", "fm", "-s", "200000", "-r", "48000", "-E", "deemp"]
        else:
            demod_args = ["-M", "am", "-s", "48000", "-r", "48000"]

        rtl_fm_cmd = [
            "rtl_fm",
            "-f", str(tuner.freq_hz),
            *demod_args,
        ]
        if tuner.squelch_db != 0:
            rtl_fm_cmd += ["-l", str(tuner.squelch_db)]

        # play reads raw signed 16-bit little-endian PCM at 48kHz mono
        # from stdin (matching rtl_fm's -r 48000 output) and adjusts
        # volume via sox's vol effect (0.0-ish to 1.0+ multiplier).
        vol_multiplier = max(0.0, min(2.0, tuner.volume_pct / 100.0))
        play_cmd = [
            "play", "-q", "-t", "raw", "-r", "48000", "-es", "-b", "16", "-c", "1",
            "-", "vol", f"{vol_multiplier:.2f}",
        ]

        try:
            self.rtl_fm_proc = subprocess.Popen(
                rtl_fm_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            self.play_proc = subprocess.Popen(
                play_cmd, stdin=self.rtl_fm_proc.stdout, stderr=subprocess.DEVNULL
            )
            # Allow rtl_fm_proc to receive SIGPIPE if play_proc exits, per
            # the standard subprocess piping idiom.
            if self.rtl_fm_proc.stdout:
                self.rtl_fm_proc.stdout.close()
            self.last_error = ""
            return True
        except FileNotFoundError as e:
            self.last_error = f"Tool not found: {e}"
            self.stop()
            return False
        except Exception as e:
            self.last_error = f"Failed to start audio pipeline: {e}"
            self.stop()
            return False

    def is_running(self) -> bool:
        return (
            self.rtl_fm_proc is not None
            and self.rtl_fm_proc.poll() is None
            and self.play_proc is not None
            and self.play_proc.poll() is None
        )

    def check_died(self) -> Optional[str]:
        """Returns an error string if either process died unexpectedly,
        else None."""
        if self.rtl_fm_proc is not None and self.rtl_fm_proc.poll() is not None:
            stderr = ""
            try:
                if self.rtl_fm_proc.stderr:
                    stderr = self.rtl_fm_proc.stderr.read().decode(errors="ignore")[:200]
            except Exception:
                pass
            return f"rtl_fm exited (code {self.rtl_fm_proc.returncode})" + (
                f": {stderr}" if stderr else " — no SDR available?"
            )
        if self.play_proc is not None and self.play_proc.poll() is not None:
            return f"play (sox) exited (code {self.play_proc.returncode}) — check audio output device"
        return None

    def stop(self) -> None:
        for proc in (self.play_proc, self.rtl_fm_proc):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self.rtl_fm_proc = None
        self.play_proc = None


def run(stdscr) -> None:
    """
    Keys:
      [Q] back to menu        [Space] play/pause (start/stop audio)
      [<-]/[->] tune (channel step)   [Shift+<-]/[Shift+->] fine tune 10kHz
      [Up]/[Down] cycle presets   [+]/[-] volume    [B] toggle FM/AM band
    """
    curses.curs_set(0)
    radar_ui.init_colors()

    tuner = TunerState()
    controller = AudioTunerController()
    playing = False

    height, width = stdscr.getmaxyx()

    def _draw() -> None:
        stdscr.erase()
        died_reason = controller.check_died() if playing else None
        currently_playing = playing and died_reason is None

        try:
            stdscr.addstr(0, 1, " PAR AVION — Radio / Broadcast Tuner ", curses.A_BOLD)

            band_lo, band_hi = tuner.band_limits()
            if tuner.mode == "fm":
                freq_display = f"{tuner.freq_hz/1e6:.3f} MHz"
                range_display = f"{band_lo/1e6:.1f}-{band_hi/1e6:.1f} MHz"
            else:
                freq_display = f"{tuner.freq_hz/1e3:.0f} kHz"
                range_display = f"{band_lo/1e3:.0f}-{band_hi/1e3:.0f} kHz"

            stdscr.addstr(2, 1, f" Band: {tuner.mode.upper()}   Freq: {freq_display}   Range: {range_display} ")
            stdscr.addstr(3, 1, f" Volume: {tuner.volume_pct}%   "
                                 f"Status: {'PLAYING' if currently_playing else 'STOPPED'} ")

            if not controller.tools_available:
                stdscr.addstr(5, 1, " Missing required tools:", curses.color_pair(radar_ui.PAIR_RED) | curses.A_BOLD)
                for i, tool in enumerate(controller.missing_tools()):
                    stdscr.addstr(6 + i, 3, f"- {tool}", curses.color_pair(radar_ui.PAIR_RED))
                stdscr.addstr(6 + len(controller.missing_tools()) + 1, 1,
                              " Run install.sh, or install manually.", curses.color_pair(radar_ui.PAIR_YELLOW))
            elif died_reason:
                stdscr.addstr(5, 1, f" Audio pipeline stopped: {died_reason[:width-3]}",
                              curses.color_pair(radar_ui.PAIR_RED))

            stdscr.addstr(8, 1, " Presets:", curses.A_UNDERLINE)
            for i, (label, freq, mode) in enumerate(PRESETS):
                marker = ">" if (mode == tuner.mode and freq == tuner.freq_hz) else " "
                stdscr.addstr(9 + i, 3, f"{marker} {label}")

            help_y = height - 3
            stdscr.addstr(help_y, 1,
                          " [Space] Play/Stop  [<-/->] Tune  [Shift+<-/->] Fine  ")
            stdscr.addstr(help_y + 1, 1,
                          " [Up/Down] Presets  [+/-] Volume  [B] FM/AM  [Q] Quit ")
        except curses.error:
            pass

        stdscr.refresh()

    def _restart_if_playing() -> None:
        nonlocal playing
        if playing:
            playing = controller.start(tuner)

    stdscr.nodelay(True)
    stdscr.timeout(200)

    try:
        while True:
            _draw()
            key = stdscr.getch()

            if key in (ord("q"), ord("Q"), 27):
                break
            elif key == ord(" "):
                if playing:
                    controller.stop()
                    playing = False
                else:
                    playing = controller.start(tuner)
            elif key == curses.KEY_LEFT:
                tuner.nudge(-tuner.step_hz())
                _restart_if_playing()
            elif key == curses.KEY_RIGHT:
                tuner.nudge(tuner.step_hz())
                _restart_if_playing()
            elif key == curses.KEY_SLEFT:
                tuner.nudge(-FINE_STEP_HZ)
                _restart_if_playing()
            elif key == curses.KEY_SRIGHT:
                tuner.nudge(FINE_STEP_HZ)
                _restart_if_playing()
            elif key == curses.KEY_UP:
                current = next(
                    (i for i, p in enumerate(PRESETS) if p[1] == tuner.freq_hz and p[2] == tuner.mode),
                    -1,
                )
                tuner.apply_preset(current + 1)
                _restart_if_playing()
            elif key == curses.KEY_DOWN:
                current = next(
                    (i for i, p in enumerate(PRESETS) if p[1] == tuner.freq_hz and p[2] == tuner.mode),
                    0,
                )
                tuner.apply_preset(current - 1)
                _restart_if_playing()
            elif key in (ord("+"), ord("=")):
                tuner.volume_pct = min(150, tuner.volume_pct + 5)
                _restart_if_playing()
            elif key == ord("-"):
                tuner.volume_pct = max(0, tuner.volume_pct - 5)
                _restart_if_playing()
            elif key in (ord("b"), ord("B")):
                was_playing = playing
                if was_playing:
                    controller.stop()
                tuner.toggle_mode()
                if was_playing:
                    playing = controller.start(tuner)

            if playing and controller.check_died():
                playing = False
    finally:
        controller.stop()
