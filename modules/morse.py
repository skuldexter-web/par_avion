"""
morse.py — CW / Morse code audio decoder & visualizer for PAR AVION.

Decodes Morse/CW audio by envelope-detecting a single audio tone (the
standard technique: energy detection at the operator-set tone frequency,
since CW is on/off keying of a single carrier, unlike SSTV/RTTY's
multi-tone FM). Classifies on/off durations into dits, dahs, intra-
character gaps, inter-character gaps, and word gaps using an adaptive
timing unit (rather than a fixed WPM assumption), since real operators'
sending speed varies.

Signal chain: rtl_fm (narrow FM/CW demod from an RTL-SDR tuned to a HAM
CW sub-band) or the system microphone -> envelope detector -> adaptive
threshold -> on/off duration classifier -> Morse-to-text lookup.

Receive-only: decodes audio already present at the input; transmits
nothing (no keying, no audio output).
"""

from __future__ import annotations

import curses
import statistics
import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional

try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

from . import radar_ui

SAMPLE_RATE = 8000  # CW audio is narrowband; 8kHz is ample and keeps CPU low

# International Morse code table (letters, digits, common punctuation).
MORSE_TABLE = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E",
    "..-.": "F", "--.": "G", "....": "H", "..": "I", ".---": "J",
    "-.-": "K", ".-..": "L", "--": "M", "-.": "N", "---": "O",
    ".--.": "P", "--.-": "Q", ".-.": "R", "...": "S", "-": "T",
    "..-": "U", "...-": "V", ".--": "W", "-..-": "X", "-.--": "Y",
    "--..": "Z",
    "-----": "0", ".----": "1", "..---": "2", "...--": "3", "....-": "4",
    ".....": "5", "-....": "6", "--...": "7", "---..": "8", "----.": "9",
    ".-.-.-": ".", "--..--": ",", "..--..": "?", "-.-.--": "!",
    "-..-.": "/", "-.--.": "(", "-.--.-": ")", ".-...": "&",
    "---...": ":", "-.-.-.": ";", "-...-": "=", ".-.-.": "+",
    "-....-": "-", "..--.-": "_", ".-..-.": '"', "...-..-": "$",
    ".--.-.": "@",
}

DEFAULT_TONE_HZ = 700.0  # conventional CW sidetone/monitor frequency


@dataclass
class TimingModel:
    """
    Adaptive Morse timing: tracks the running estimate of one 'unit'
    (a dit's duration) from observed on/off durations, since real CW
    speed varies operator-to-operator and even within one transmission.
    Standard ratios (per ITU-R M.1677-1 / conventional Morse timing):
      dah = 3 units, intra-character gap = 1 unit,
      inter-character gap = 3 units, word gap = 7 units.

    Bootstraps its initial unit_sec from the first several observed mark
    durations rather than trusting a fixed prior: with a fixed 15WPM
    prior, a sender at a very different real speed (e.g. 30WPM) has
    their first dah misclassified as a dit before the model ever gets a
    chance to correct, and that wrong classification then poisons the
    running estimate rather than fixing it (dits only ever get *slower*
    corrections from later dits, never a single sharp recalibration).
    Collecting a small pool of raw marks first and bootstrapping from
    their minimum (dits are always the shortest mark in any Morse
    signal, by definition of the code) avoids that failure mode.
    """
    unit_sec: float = 0.08  # fallback guess (~15 WPM) if bootstrap can't run
    _recent_dit_samples: Deque[float] = field(default_factory=lambda: deque(maxlen=20))
    _bootstrap_marks: List[float] = field(default_factory=list)
    _bootstrapped: bool = False
    _bootstrap_count: int = 6  # marks to collect before committing to a unit

    def classify_mark(self, duration_sec: float) -> str:
        """An 'on' (tone-present) duration: dit or dah."""
        if not self._bootstrapped:
            return self._bootstrap_classify(duration_sec)
        if duration_sec < self.unit_sec * 2:
            self._observe_dit(duration_sec)
            return "."
        return "-"

    def _bootstrap_classify(self, duration_sec: float) -> str:
        self._bootstrap_marks.append(duration_sec)
        if len(self._bootstrap_marks) < self._bootstrap_count:
            # Not enough data yet to safely tell dits from dahs. Use the
            # fallback prior just for this one symbol's returned glyph
            # (best-effort placeholder — see note below); the real
            # correction happens once bootstrap completes and later
            # decoding self-corrects via the normal path.
            return "." if duration_sec < self.unit_sec * 2 else "-"

        # Enough marks collected: the shortest one observed is, by
        # definition of Morse timing, a dit (dahs are always 3x a dit
        # in the same transmission). Seed unit_sec from it directly
        # rather than from a possibly-wrong running median.
        self.unit_sec = min(self._bootstrap_marks)
        self._bootstrapped = True
        self._recent_dit_samples.append(self.unit_sec)
        # Re-return this call's own classification using the now-correct
        # unit — this symbol itself was the shortest (or tied), so it's
        # correctly a dit.
        return "." if duration_sec < self.unit_sec * 2 else "-"

    def classify_space(self, duration_sec: float) -> str:
        """An 'off' (silence) duration: intra-char / inter-char / word gap."""
        if duration_sec < self.unit_sec * 2:
            return ""            # within a character — no separator
        elif duration_sec < self.unit_sec * 5:
            return "char_gap"    # between characters
        else:
            return "word_gap"    # between words

    def _observe_dit(self, duration_sec: float) -> None:
        self._recent_dit_samples.append(duration_sec)
        if len(self._recent_dit_samples) >= 3:
            # Median is robust to the occasional dah misclassified along
            # the way (e.g. unusually-sent Morse, timing jitter).
            self.unit_sec = statistics.median(self._recent_dit_samples)

    def estimated_wpm(self) -> float:
        # PARIS standard: 1 WPM = 1.2s per unit (50 units per "PARIS").
        if self.unit_sec <= 0:
            return 0.0
        return 1.2 / self.unit_sec


def _goertzel_mag(samples, sample_rate: int, target_hz: float) -> float:
    """Single-bin Goertzel magnitude. Appropriate here (unlike SSTV's
    per-pixel decode) because CW is single-tone on/off keying — each
    envelope window only needs the energy at one known frequency, not
    fine frequency resolution, so there's no window-length-vs-resolution
    problem to worry about the way there was for SSTV's fast modes."""
    if not HAVE_NUMPY:
        return 0.0
    n = len(samples)
    if n == 0:
        return 0.0
    import math
    k = int(0.5 + (n * target_hz) / sample_rate)
    w = 2 * math.pi * k / n
    coeff = 2 * math.cos(w)
    s_prev, s_prev2 = 0.0, 0.0
    for sample in samples:
        s = sample + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    power = s_prev2 ** 2 + s_prev ** 2 - coeff * s_prev * s_prev2
    return max(0.0, power) ** 0.5


class EnvelopeDetector:
    """Extracts an on/off keying envelope from raw audio at a target
    tone frequency using windowed Goertzel-magnitude energy detection."""

    def __init__(self, sample_rate: int, tone_hz: float, window_ms: float = 10.0):
        self.sample_rate = sample_rate
        self.tone_hz = tone_hz
        self.window_n = max(4, int(sample_rate * window_ms / 1000.0))

    def set_tone(self, tone_hz: float) -> None:
        self.tone_hz = tone_hz

    def envelope(self, samples) -> List[float]:
        """Returns one magnitude value per window_n-sample chunk."""
        if not HAVE_NUMPY:
            return []
        mags = []
        n = len(samples)
        for start in range(0, n - self.window_n + 1, self.window_n):
            window = samples[start:start + self.window_n]
            mags.append(_goertzel_mag(window, self.sample_rate, self.tone_hz))
        return mags


class MorseDecoder:
    """
    Feeds raw audio in, maintains a running envelope, adaptively picks an
    on/off threshold (since absolute signal level varies with SDR gain/
    distance), classifies mark/space durations via TimingModel, and
    assembles decoded characters into a rolling text buffer.
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE, tone_hz: float = DEFAULT_TONE_HZ):
        self.sample_rate = sample_rate
        self.detector = EnvelopeDetector(sample_rate, tone_hz)
        self.timing = TimingModel()
        self._buf: List[float] = []
        self._lock = threading.Lock()

        self._current_symbol = ""     # accumulating dits/dahs for one letter
        self.decoded_text: Deque[str] = deque(maxlen=500)
        self._state = "idle"          # "idle" (silence) | "mark" (tone on)
        self._state_start_windows = 0
        self._window_sec = self.detector.window_n / sample_rate
        self._threshold: Optional[float] = None
        self._recent_mags: Deque[float] = deque(maxlen=200)
        # Windows observed before any threshold could be established yet
        # (e.g. the very first chunk of audio happens to be pure tone or
        # pure silence, with no contrast to derive a threshold from).
        # Held here and classified once a threshold becomes available,
        # rather than being discarded/misclassified as silence — which
        # previously corrupted the first character whenever the initial
        # chunk had no contrast in it (see module change history).
        self._pending_mags: List[float] = []

        self.last_char = ""
        self.total_chars = 0

    def set_tone_hz(self, tone_hz: float) -> None:
        self.detector.set_tone(tone_hz)

    def feed(self, samples) -> None:
        with self._lock:
            self._buf.extend(samples)
            max_len = self.sample_rate * 5
            if len(self._buf) > max_len:
                self._buf = self._buf[-max_len:]

    def process(self) -> None:
        """Call periodically to advance decoding on newly buffered audio."""
        if not HAVE_NUMPY:
            return
        with self._lock:
            if not self._buf:
                return
            samples = np.array(self._buf, dtype=np.float32)
            self._buf = []

        mags = self.detector.envelope(samples)
        if not mags:
            return

        # Any windows held back from a previous call (because no
        # threshold existed yet) are classified together with this
        # batch, in original arrival order, once a threshold is
        # available — see _pending_mags' definition in __init__.
        self._pending_mags.extend(mags)
        for mag in mags:
            self._recent_mags.append(mag)

        # Seed/refresh the threshold from the accumulated pending batch
        # (which may span more than one process() call if earlier calls
        # had no contrast to derive one from) before classifying anything.
        self._update_threshold(batch_hint=self._pending_mags)

        if self._threshold is None:
            # Still no contrast anywhere in the pending backlog — nothing
            # can be classified yet; keep accumulating and try again next
            # call rather than guessing. Cap growth so a genuinely flat
            # signal (no contrast ever) doesn't grow this unboundedly.
            if len(self._pending_mags) > 2000:
                self._pending_mags = self._pending_mags[-1000:]
            return

        for mag in self._pending_mags:
            is_mark = mag > self._threshold
            self._advance_state(is_mark)
        self._pending_mags = []

    def _update_threshold(self, batch_hint: Optional[List[float]] = None) -> None:
        # Prefer computing min/max across the just-arrived batch (see
        # process()) so a threshold is available for every window in
        # that batch, not just ones after the Nth sample ever seen.
        # Falls back to the rolling history for calls with no batch hint.
        source = batch_hint if batch_hint else self._recent_mags
        if len(source) < 2:
            return
        lo = min(source)
        hi = max(source)
        if hi - lo < 1e-6:
            return
        self._threshold = lo + (hi - lo) * 0.35

    def _advance_state(self, is_mark: bool) -> None:
        new_state = "mark" if is_mark else "idle"
        if new_state == self._state:
            self._state_start_windows += 1
            return

        # State transition: the just-finished run's duration determines
        # what it was (dit/dah if we were in "mark"; a gap type if we
        # were in "idle").
        duration_sec = self._state_start_windows * self._window_sec
        if duration_sec > 0:
            self._classify_and_emit(self._state, duration_sec)

        self._state = new_state
        self._state_start_windows = 1

    def _classify_and_emit(self, state: str, duration_sec: float) -> None:
        """Classify one finished mark/space run and, for a completed
        character (on a char/word gap), append it to decoded_text."""
        if state == "mark":
            symbol = self.timing.classify_mark(duration_sec)
            self._current_symbol += symbol
        elif state == "idle":
            gap = self.timing.classify_space(duration_sec)
            if gap in ("char_gap", "word_gap") and self._current_symbol:
                letter = MORSE_TABLE.get(self._current_symbol, "?")
                self.decoded_text.append(letter)
                self.last_char = letter
                self.total_chars += 1
                self._current_symbol = ""
            if gap == "word_gap":
                self.decoded_text.append(" ")

    def flush_pending(self) -> None:
        """Force-decode any in-progress symbol, including a mark that
        never got its closing state transition (e.g. the signal simply
        stopped mid-dit/dah, or the user asked to reset). Without this,
        a transmission's final element can be silently dropped since
        _advance_state() only classifies a mark once it sees the
        following silence."""
        if self._state == "mark" and self._state_start_windows > 0:
            duration_sec = self._state_start_windows * self._window_sec
            self._classify_and_emit("mark", duration_sec)
            self._state = "idle"
            self._state_start_windows = 0

        if self._current_symbol:
            letter = MORSE_TABLE.get(self._current_symbol, "?")
            self.decoded_text.append(letter)
            self.last_char = letter
            self.total_chars += 1
            self._current_symbol = ""

    def text(self) -> str:
        return "".join(self.decoded_text)


class AudioCaptureController:
    """Captures audio for Morse decoding via rtl_fm (SDR) or microphone,
    mirroring sstv.py's AudioCaptureController for consistency."""

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.rtl_fm_proc: Optional[subprocess.Popen] = None
        self._stream = None
        self.mode = "none"
        self.last_error = ""

    def start_rtl_fm(self, freq_hz: int, decoder: MorseDecoder) -> bool:
        self.stop()
        try:
            self.rtl_fm_proc = subprocess.Popen(
                ["rtl_fm", "-f", str(freq_hz), "-M", "fm", "-s", str(self.sample_rate)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            self.last_error = "rtl_fm not found on PATH (package: rtl-sdr)"
            return False
        except Exception as e:
            self.last_error = f"rtl_fm: {e}"
            return False

        self.mode = "rtl_fm"

        def _pump():
            import struct
            assert self.rtl_fm_proc is not None
            bytes_per_sample = 2
            chunk_samples = 1024
            while self.rtl_fm_proc and self.rtl_fm_proc.poll() is None:
                raw = self.rtl_fm_proc.stdout.read(chunk_samples * bytes_per_sample)
                if not raw:
                    break
                count = len(raw) // bytes_per_sample
                if count == 0:
                    continue
                ints = struct.unpack(f"<{count}h", raw[: count * bytes_per_sample])
                floats = [v / 32768.0 for v in ints]
                decoder.feed(floats)

        threading.Thread(target=_pump, daemon=True).start()
        return True

    def start_mic(self, decoder: MorseDecoder) -> bool:
        self.stop()
        try:
            import sounddevice as sd
        except ImportError:
            self.last_error = "sounddevice not installed (pip: python3-sounddevice)"
            return False
        try:
            def _callback(indata, frames, time_info, status):
                decoder.feed(indata[:, 0].tolist())

            self._stream = sd.InputStream(
                samplerate=self.sample_rate, channels=1, callback=_callback
            )
            self._stream.start()
            self.mode = "mic"
            return True
        except Exception as e:
            self.last_error = f"microphone input: {e}"
            return False

    def stop(self) -> None:
        if self.rtl_fm_proc is not None:
            self.rtl_fm_proc.terminate()
            try:
                self.rtl_fm_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.rtl_fm_proc.kill()
            self.rtl_fm_proc = None
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self.mode = "none"


def run(stdscr) -> None:
    """
    Keys: [Q] back to menu   [M] switch input source (rtl_fm SDR / mic)
          [F] set rtl_fm frequency (cycles common CW calling freqs)
          [T] cycle tone frequency (for mistuned signals)
          [R] flush/reset current symbol buffer
    """
    curses.curs_set(0)
    radar_ui.init_colors()

    decoder = MorseDecoder()
    capture = AudioCaptureController()

    cw_freqs = [
        ("7.030 MHz (40m CW)", 7_030_000),
        ("14.030 MHz (20m CW)", 14_030_000),
        ("3.530 MHz (80m CW)", 3_530_000),
        ("21.030 MHz (15m CW)", 21_030_000),
    ]
    tone_options = [500.0, 600.0, 700.0, 800.0, 1000.0]
    freq_idx = 0
    tone_idx = 2  # 700Hz default
    input_source = "rtl_fm"

    def _start_capture() -> None:
        if input_source == "rtl_fm":
            capture.start_rtl_fm(cw_freqs[freq_idx][1], decoder)
        else:
            capture.start_mic(decoder)

    if not HAVE_NUMPY:
        capture.last_error = "numpy not installed — Morse decoding unavailable"
    else:
        _start_capture()

    height, width = stdscr.getmaxyx()
    text_win = curses.newwin(height - 6, width, 0, 0)
    status_win = curses.newwin(6, width, height - 6, 0)

    stdscr.nodelay(True)
    stdscr.timeout(100)

    try:
        while True:
            key = stdscr.getch()
            if key in (ord("q"), ord("Q"), 27):
                break
            elif key in (ord("m"), ord("M")):
                input_source = "mic" if input_source == "rtl_fm" else "rtl_fm"
                _start_capture()
            elif key in (ord("f"), ord("F")):
                freq_idx = (freq_idx + 1) % len(cw_freqs)
                if input_source == "rtl_fm":
                    _start_capture()
            elif key in (ord("t"), ord("T")):
                tone_idx = (tone_idx + 1) % len(tone_options)
                decoder.set_tone_hz(tone_options[tone_idx])
            elif key in (ord("r"), ord("R")):
                decoder.flush_pending()

            decoder.process()

            text_win.erase()
            text_win.border()
            try:
                text_win.addstr(0, 2, " DECODED TEXT ", curses.color_pair(radar_ui.PAIR_GREEN) | curses.A_BOLD)
            except curses.error:
                pass
            full_text = decoder.text()
            # Word-wrap into the window, showing the most recent content.
            wrap_width = max(10, width - 4)
            lines = []
            remaining = full_text
            while remaining:
                lines.append(remaining[:wrap_width])
                remaining = remaining[wrap_width:]
            max_rows = height - 6 - 2
            for i, line in enumerate(lines[-max_rows:]):
                try:
                    text_win.addstr(1 + i, 2, line, curses.color_pair(radar_ui.PAIR_GREEN))
                except curses.error:
                    pass
            text_win.noutrefresh()

            status_win.erase()
            status_win.border()
            src_label = f"rtl_fm @ {cw_freqs[freq_idx][0]}" if input_source == "rtl_fm" else "microphone"
            try:
                status_win.addstr(1, 2, f" Input: {src_label}   Tone: {tone_options[tone_idx]:.0f}Hz ",
                                   curses.color_pair(radar_ui.PAIR_CYAN))
                status_win.addstr(2, 2,
                                   f" Est. WPM: {decoder.timing.estimated_wpm():.0f}   "
                                   f"Chars decoded: {decoder.total_chars}   Last: '{decoder.last_char}' ",
                                   curses.color_pair(radar_ui.PAIR_GREEN))
                if capture.last_error:
                    status_win.addstr(3, 2, f" {capture.last_error[:width-4]} ", curses.color_pair(radar_ui.PAIR_RED))
                status_win.addstr(4, 2,
                                   " [M] Input  [F] Freq  [T] Tone  [R] Flush  [Q] Quit ",
                                   curses.color_pair(radar_ui.PAIR_CYAN))
            except curses.error:
                pass
            status_win.noutrefresh()

            curses.doupdate()
    finally:
        capture.stop()
