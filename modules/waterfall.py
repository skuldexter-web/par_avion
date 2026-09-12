"""
waterfall.py — CLI spectrum waterfall analyzer for PAR AVION.

(Formerly radio.py in v1 — renamed since v2 introduces a separate
`radio.py` for broadcast AM/FM audio demodulation. This module is the
Waterfalls menu entry: FFT power spectrum + scrolling ANSI-gradient
waterfall display, no audio output.)

Reads raw IQ samples from an RTL-SDR via pyrtlsdr, computes an FFT power
spectrum, and renders a scrolling ANSI-gradient waterfall (black -> blue ->
cyan -> green -> yellow -> red) plus a live spectrum line. Supports live
retuning with arrow keys across ISM, FM broadcast, airband, and HAM ranges.

Receive-only: this reads whatever signal is already present at the
antenna. It does not key up a transmitter.
"""

from __future__ import annotations

import curses
import time
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

try:
    from rtlsdr import RtlSdr
    HAVE_RTLSDR = True
except Exception:
    # Broader than ImportError on purpose: pyrtlsdr raises AttributeError
    # (or other errors) at import time if the installed librtlsdr.so is
    # older/newer than what the Python bindings expect (missing symbols
    # like rtlsdr_set_dithering). Either way, fall back to simulated mode
    # rather than crashing the whole app.
    RtlSdr = None
    HAVE_RTLSDR = False


# ---------------------------------------------------------------------------
# Band presets: (name, center_freq_hz, sample_rate_hz)
# ---------------------------------------------------------------------------
BAND_PRESETS = [
    ("433 MHz ISM", 433_100_000, 2_048_000),
    ("FM Broadcast", 100_000_000, 2_400_000),
    ("Airband (AM)", 124_000_000, 2_048_000),
    ("2m HAM", 146_000_000, 2_048_000),
    ("70cm HAM", 435_000_000, 2_048_000),
    ("315 MHz ISM", 315_000_000, 2_048_000),
]

TUNE_STEP_HZ = 100_000  # coarse step for left/right; shift for fine step
TUNE_STEP_FINE_HZ = 10_000

WATERFALL_GRADIENT = [
    (curses.COLOR_BLACK, " "),
    (curses.COLOR_BLUE, "░"),
    (curses.COLOR_CYAN, "▒"),
    (curses.COLOR_GREEN, "▓"),
    (curses.COLOR_YELLOW, "█"),
    (curses.COLOR_RED, "█"),
]


def init_gradient_colors(base_pair_start: int = 20) -> List[int]:
    """Initialize a run of color pairs for the waterfall gradient."""
    pairs = []
    for i, (color, _) in enumerate(WATERFALL_GRADIENT):
        pair_id = base_pair_start + i
        try:
            curses.init_pair(pair_id, color, curses.COLOR_BLACK)
        except curses.error:
            pass
        pairs.append(pair_id)
    return pairs


@dataclass
class TunerState:
    band_index: int = 0
    center_freq_hz: int = BAND_PRESETS[0][1]
    sample_rate_hz: int = BAND_PRESETS[0][2]
    gain: str = "auto"

    def apply_preset(self, index: int) -> None:
        index = index % len(BAND_PRESETS)
        _, freq, rate = BAND_PRESETS[index]
        self.band_index = index
        self.center_freq_hz = freq
        self.sample_rate_hz = rate

    def nudge(self, delta_hz: int) -> None:
        self.center_freq_hz = max(24_000_000, min(1_766_000_000, self.center_freq_hz + delta_hz))


class SDRReader:
    """Wraps pyrtlsdr; falls back to synthetic noise if no hardware present
    so the UI remains testable/demoable without a dongle attached."""

    def __init__(self, tuner: TunerState):
        self.tuner = tuner
        self.sdr: Optional["RtlSdr"] = None
        self.simulated = not HAVE_RTLSDR
        if HAVE_RTLSDR:
            try:
                self.sdr = RtlSdr()
                self._apply_tuning()
            except Exception:
                self.sdr = None
                self.simulated = True

    def _apply_tuning(self) -> None:
        if self.sdr is None:
            return
        self.sdr.sample_rate = self.tuner.sample_rate_hz
        self.sdr.center_freq = self.tuner.center_freq_hz
        self.sdr.gain = "auto" if self.tuner.gain == "auto" else float(self.tuner.gain)

    def retune(self) -> None:
        self._apply_tuning()

    def read_power_spectrum(self, n_bins: int) -> np.ndarray:
        """Returns a normalized (0..1) power spectrum array of n_bins bins."""
        if self.simulated or self.sdr is None:
            return self._simulate_spectrum(n_bins)
        try:
            samples = self.sdr.read_samples(8 * 1024)
            windowed = samples * np.hanning(len(samples))
            spectrum = np.fft.fftshift(np.fft.fft(windowed))
            power = 20 * np.log10(np.abs(spectrum) + 1e-9)
            # Resample to n_bins
            power = np.interp(
                np.linspace(0, len(power) - 1, n_bins),
                np.arange(len(power)),
                power,
            )
            power -= power.min()
            if power.max() > 0:
                power /= power.max()
            return power
        except Exception:
            return self._simulate_spectrum(n_bins)

    def _simulate_spectrum(self, n_bins: int) -> np.ndarray:
        """Synthetic noise floor + a couple of drifting fake carriers, used
        only when no SDR hardware is connected, so the TUI still runs."""
        t = time.time()
        x = np.linspace(-1, 1, n_bins)
        noise = np.random.normal(0.15, 0.05, n_bins)
        carrier1 = 0.8 * np.exp(-((x - 0.3 * np.sin(t * 0.3)) ** 2) / 0.002)
        carrier2 = 0.5 * np.exp(-((x + 0.5) ** 2) / 0.001)
        spectrum = np.clip(noise + carrier1 + carrier2, 0, 1)
        return spectrum

    def close(self) -> None:
        if self.sdr is not None:
            try:
                self.sdr.close()
            except Exception:
                pass


def _power_to_gradient_index(value: float) -> int:
    idx = int(value * (len(WATERFALL_GRADIENT) - 1))
    return max(0, min(len(WATERFALL_GRADIENT) - 1, idx))


def run(stdscr) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(80)
    curses.start_color()
    curses.use_default_colors()
    gradient_pairs = init_gradient_colors()

    tuner = TunerState()
    tuner.apply_preset(0)
    reader = SDRReader(tuner)

    height, width = stdscr.getmaxyx()
    header_h = 3
    spectrum_h = 6
    waterfall_h = height - header_h - spectrum_h - 1
    n_bins = width - 2

    waterfall_history: List[np.ndarray] = []

    try:
        while True:
            key = stdscr.getch()
            if key in (ord("q"), ord("Q"), 27):
                break
            elif key == curses.KEY_LEFT:
                tuner.nudge(-TUNE_STEP_HZ)
                reader.retune()
            elif key == curses.KEY_RIGHT:
                tuner.nudge(TUNE_STEP_HZ)
                reader.retune()
            elif key == curses.KEY_SLEFT:
                tuner.nudge(-TUNE_STEP_FINE_HZ)
                reader.retune()
            elif key == curses.KEY_SRIGHT:
                tuner.nudge(TUNE_STEP_FINE_HZ)
                reader.retune()
            elif key in (curses.KEY_UP, curses.KEY_DOWN):
                delta = 1 if key == curses.KEY_UP else -1
                tuner.apply_preset(tuner.band_index + delta)
                reader.retune()

            spectrum = reader.read_power_spectrum(n_bins)
            waterfall_history.insert(0, spectrum)
            if len(waterfall_history) > waterfall_h:
                waterfall_history.pop()

            stdscr.erase()

            # Header
            band_name = BAND_PRESETS[tuner.band_index][0]
            mode_tag = "SIMULATED (no SDR detected)" if reader.simulated else "LIVE"
            stdscr.addstr(0, 1, f" PAR AVION — Waterfalls  [{mode_tag}] ",
                          curses.A_BOLD)
            stdscr.addstr(1, 1,
                          f" Band: {band_name}   Freq: {tuner.center_freq_hz/1e6:.4f} MHz   "
                          f"Rate: {tuner.sample_rate_hz/1e6:.3f} MSPS ")
            stdscr.addstr(2, 1, " ←/→ tune 100kHz  Shift+←/→ tune 10kHz  ↑/↓ band preset  Q quit ")

            # Spectrum line (bar chart)
            for col in range(min(n_bins, spectrum.shape[0])):
                bar_height = int(spectrum[col] * spectrum_h)
                for row in range(bar_height):
                    y = header_h + spectrum_h - 1 - row
                    idx = _power_to_gradient_index(spectrum[col])
                    try:
                        stdscr.addch(y, col + 1, WATERFALL_GRADIENT[idx][1],
                                     curses.color_pair(gradient_pairs[idx]))
                    except curses.error:
                        pass

            # Waterfall (scrolling history, newest at top)
            for row_i, row_spectrum in enumerate(waterfall_history):
                y = header_h + spectrum_h + row_i
                if y >= height - 1:
                    break
                for col in range(min(n_bins, row_spectrum.shape[0])):
                    idx = _power_to_gradient_index(row_spectrum[col])
                    try:
                        stdscr.addch(y, col + 1, WATERFALL_GRADIENT[idx][1],
                                     curses.color_pair(gradient_pairs[idx]))
                    except curses.error:
                        pass

            stdscr.refresh()
    finally:
        reader.close()
