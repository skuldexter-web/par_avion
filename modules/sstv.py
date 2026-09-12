"""
sstv.py — Slow Scan TV (SSTV) decoder for PAR AVION.

Captures FM-demodulated audio (via rtl_fm piped from an RTL-SDR tuned to
an SSTV calling frequency, e.g. 145.800 MHz for ISS SSTV events, or from
the system microphone/line-in if fed from an external radio), detects
the VIS (Vertical Interval Signaling) header that identifies the SSTV
mode in use, then decodes the audio tone stream into an image using
real per-mode timing constants.

Supported modes: Martin 1, Martin 2, Scottie 1, Scottie 2, Scottie DX,
Robot 36, Robot 72 — covering every mode named in the spec (Martin 1/2,
Scottie 1/2, Robot 36/72) plus Scottie DX, a closely related variant
sharing the same decode path at no extra cost.

Decode approach (standard technique used by slowrx, MMSSTV, QSSTV, etc.):
  1. VIS detection: 300ms 1900Hz leader tone, 10ms 1200Hz break, a
     second 300ms 1900Hz leader, a 30ms 1200Hz start bit, then 7 data
     bits + 1 even-parity bit (30ms each: 1300Hz=0, 1100Hz=1), then a
     30ms 1200Hz stop bit. The 7 data bits form the VIS code identifying
     the mode; the parity bit is checked so a garbled header is rejected
     rather than mis-identified as a mode.
  2. Per-line decode: for each scan line, locate the sync pulse, then
     read each channel's pixel run. Frequency-to-luminance conversion
     uses a *continuous* instantaneous-frequency estimate across the
     whole line (Hilbert transform + phase derivative, sampled at each
     pixel's center), NOT a per-pixel-window frequency estimate — the
     fastest modes (Robot 36) have pixel windows as short as ~6 samples
     at 44.1kHz, far too few samples for a block-based Goertzel/FFT bin
     to resolve 800Hz of bandwidth (bin spacing at that window length is
     roughly 2-3x the entire black-to-white frequency range). Tracking
     phase continuously across the whole line and sampling it at each
     pixel's midpoint sidesteps that resolution floor, at the cost of
     needing a few samples of padding/settling time at each end of the
     line to avoid Hilbert-transform edge artifacts.

Receive-only: decodes audio already present at the input; transmits
nothing.
"""

from __future__ import annotations

import curses
import math
import os
import struct
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

try:
    from scipy.signal import hilbert as _scipy_hilbert
    HAVE_SCIPY = True
except ImportError:
    HAVE_SCIPY = False

from . import radar_ui

# 44.1kHz matches what real SSTV software (slowrx, MMSSTV, QSSTV) uses;
# 22.05kHz was tried first but leaves too few samples per pixel for the
# fastest modes (Robot 36) to resolve reliably even with continuous phase
# tracking — see module docstring.
SAMPLE_RATE = 44100
CAPTURE_DIR_DEFAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "captures", "sstv"
)

# VIS header timing (constant across all SSTV modes)
VIS_LEADER_HZ = 1900.0
VIS_LEADER_SEC = 0.300
VIS_BREAK_HZ = 1200.0
VIS_BREAK_SEC = 0.010
VIS_START_HZ = 1200.0
VIS_START_SEC = 0.030
VIS_BIT_SEC = 0.030
VIS_BIT0_HZ = 1300.0
VIS_BIT1_HZ = 1100.0
VIS_STOP_HZ = 1200.0
VIS_STOP_SEC = 0.030

SYNC_HZ = 1200.0
BLACK_HZ = 1500.0
WHITE_HZ = 2300.0


@dataclass
class ModeSpec:
    """Per-mode SSTV timing/layout, values from Dave Jones (KB4YZ) VIS
    table and the standard slowrx/MMSSTV timing constants."""
    name: str
    vis_code: int
    line_pixels: int
    image_lines: int
    sync_seconds: float
    porch_seconds: float
    pixel_seconds: float
    septr_seconds: float
    layout: str       # "rgb_sequential_linestart" | "rgb_sequential_scottie" | "robot_yuv"


MODE_SPECS: List[ModeSpec] = [
    ModeSpec("Martin 1", 0x2C, 320, 256, 0.004862, 0.000572, 0.0004576, 0.000572, "rgb_sequential_linestart"),
    ModeSpec("Martin 2", 0x28, 320, 256, 0.004862, 0.000572, 0.0002288, 0.000572, "rgb_sequential_linestart"),
    ModeSpec("Scottie 1", 0x3C, 320, 256, 0.009, 0.0015, 0.0004320, 0.0015, "rgb_sequential_scottie"),
    ModeSpec("Scottie 2", 0x38, 320, 256, 0.009, 0.0015, 0.0002752, 0.0015, "rgb_sequential_scottie"),
    ModeSpec("Scottie DX", 0x4C, 320, 256, 0.009, 0.0015, 0.00108053, 0.0015, "rgb_sequential_scottie"),
    ModeSpec("Robot 36", 0x08, 320, 240, 0.009, 0.003, 0.0001375, 0.006, "robot_yuv"),
    ModeSpec("Robot 72", 0x0C, 320, 240, 0.009, 0.003, 0.0002875, 0.0047, "robot_yuv"),
]

VIS_LOOKUP = {m.vis_code: m for m in MODE_SPECS}


def _goertzel_mag(samples, sample_rate: int, target_hz: float) -> float:
    """Goertzel algorithm: magnitude of the DFT bin nearest target_hz,
    much cheaper than a full FFT when only a few frequencies matter."""
    n = len(samples)
    if n == 0:
        return 0.0
    k = int(0.5 + (n * target_hz) / sample_rate)
    w = 2 * math.pi * k / n
    cos_w = math.cos(w)
    coeff = 2 * cos_w
    s_prev, s_prev2 = 0.0, 0.0
    for sample in samples:
        s = sample + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    power = s_prev2 ** 2 + s_prev ** 2 - coeff * s_prev * s_prev2
    return math.sqrt(max(0.0, power))


def _instantaneous_freq_hz(samples, sample_rate: int, lo_hz: float = 1000.0, hi_hz: float = 2500.0,
                             bins: int = 30) -> float:
    """Estimate the dominant frequency within [lo_hz, hi_hz] using a bank
    of Goertzel filters. Suitable for VIS header bit detection, where
    each window is a fixed 30ms (over a thousand samples at 44.1kHz) —
    plenty for a block-based bin search to resolve the 200Hz gap between
    the two VIS tone frequencies. NOT suitable for per-pixel SSTV image
    decoding, where windows can be as short as ~6 samples; use
    `line_instantaneous_freq_curve` for that instead (see module
    docstring for why)."""
    if not HAVE_NUMPY or len(samples) == 0:
        return lo_hz
    best_freq = lo_hz
    best_mag = -1.0
    step = (hi_hz - lo_hz) / bins
    for i in range(bins + 1):
        f = lo_hz + i * step
        mag = _goertzel_mag(samples, sample_rate, f)
        if mag > best_mag:
            best_mag = mag
            best_freq = f
    return best_freq


def line_instantaneous_freq_curve(samples, sample_rate: int, smooth_n: int = 3):
    """
    Compute a continuous instantaneous-frequency curve across an entire
    audio segment via the Hilbert transform (analytic signal) and phase
    derivative, then lightly smooth it. This is accurate down to just a
    few samples per "pixel" because it never re-estimates frequency from
    an isolated short window — it tracks phase across the whole segment
    at once and the caller samples the resulting curve at each pixel's
    center. Returns a numpy array the same length as `samples`; the
    first/last few samples are less reliable (Hilbert edge artifacts),
    so callers should pass in a segment with a little padding on both
    sides and avoid sampling within ~5 samples of either end.
    """
    if not HAVE_NUMPY or not HAVE_SCIPY or len(samples) < 4:
        return np.full(len(samples), BLACK_HZ, dtype=np.float64) if HAVE_NUMPY else []

    analytic = _scipy_hilbert(np.asarray(samples, dtype=np.float64))
    phase = np.unwrap(np.angle(analytic))
    inst_freq = np.diff(phase) / (2.0 * np.pi) * sample_rate
    inst_freq = np.concatenate([inst_freq, [inst_freq[-1]]])

    if smooth_n > 1 and len(inst_freq) >= smooth_n:
        kernel = np.ones(smooth_n) / smooth_n
        inst_freq = np.convolve(inst_freq, kernel, mode="same")

    return inst_freq


def freq_to_luminance(freq_hz: float) -> int:
    """Map an SSTV tone frequency to a 0-255 pixel value (1500Hz=black,
    2300Hz=white, linear in between, per the SSTV convention)."""
    frac = (freq_hz - BLACK_HZ) / (WHITE_HZ - BLACK_HZ)
    return max(0, min(255, int(round(frac * 255))))


class VisDetector:
    """Scans a rolling audio buffer for the SSTV VIS header and, when
    found, returns the decoded ModeSpec."""

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate

    def try_detect(self, buf) -> Optional[ModeSpec]:
        """buf: a numpy float32 array of recent audio samples. Looks for
        the VIS header: 300ms 1900Hz leader, 10ms 1200Hz break, a second
        300ms 1900Hz leader, a 30ms 1200Hz start bit, 7 data bits + 1
        even-parity bit (30ms each, 1300Hz=0 / 1100Hz=1), then a 30ms
        1200Hz stop bit. Returns None if no valid header is found in
        this buffer (caller should keep accumulating and retry)."""
        if not HAVE_NUMPY:
            return None

        leader_samples = int(VIS_LEADER_SEC * self.sample_rate)
        break_samples = int(VIS_BREAK_SEC * self.sample_rate)
        start_samples = int(VIS_START_SEC * self.sample_rate)
        bit_samples = int(VIS_BIT_SEC * self.sample_rate)
        stop_samples = int(VIS_STOP_SEC * self.sample_rate)

        needed = (
            leader_samples + break_samples + leader_samples + start_samples
            + bit_samples * 8  # 7 data bits + 1 parity bit
            + stop_samples
        )
        if len(buf) < needed:
            return None

        # Confirm a strong 1900Hz tone at the start of the buffer (first
        # leader). A real receiver would also re-scan at multiple offsets
        # to find where the leader actually starts; the caller retries
        # this each time new audio arrives, which serves the same purpose
        # at coarser granularity.
        leader_mag = _goertzel_mag(buf[:leader_samples], self.sample_rate, VIS_LEADER_HZ)
        noise_mag = _goertzel_mag(buf[:leader_samples], self.sample_rate, 1900.0 + 400.0)
        if leader_mag < noise_mag * 1.5 or leader_mag < 1.0:
            return None

        pos = leader_samples
        pos += break_samples          # 10ms 1200Hz break
        pos += leader_samples          # second 300ms 1900Hz leader
        pos += start_samples           # 30ms 1200Hz start bit

        bits = []
        for _ in range(8):  # 7 data bits + 1 parity bit, LSB first
            window = buf[pos: pos + bit_samples]
            f0_mag = _goertzel_mag(window, self.sample_rate, VIS_BIT0_HZ)
            f1_mag = _goertzel_mag(window, self.sample_rate, VIS_BIT1_HZ)
            bits.append(1 if f1_mag > f0_mag else 0)
            pos += bit_samples

        data_bits, parity_bit = bits[:7], bits[7]
        vis_code = sum(b << i for i, b in enumerate(data_bits))

        # SSTV uses even parity: the number of 1-bits across all 8 bits
        # (7 data + parity) must be even. A mismatch usually means noise
        # corrupted the header rather than a genuinely different mode, so
        # treat it as "no valid header yet" rather than trusting a
        # possibly-garbled vis_code.
        if (sum(data_bits) + parity_bit) % 2 != 0:
            return None

        return VIS_LOOKUP.get(vis_code)


class SstvImageBuffer:
    """Accumulates decoded RGB rows as they arrive, for live preview and
    final save. Row order and channel assignment are handled by the
    per-mode decoder that writes into this buffer."""

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        if HAVE_NUMPY:
            self.pixels = np.zeros((height, width, 3), dtype=np.uint8)
        else:
            self.pixels = None
        self.rows_completed = 0
        self.last_save_was_png = False  # set by save_png(); tells the
        # caller whether the PNG path was actually used or silently
        # fell back to .ppm (no Pillow installed), so the UI can report
        # the real filename instead of one that doesn't exist on disk.

    def set_row_rgb(self, row: int, r_row, g_row, b_row) -> None:
        if not HAVE_NUMPY or row < 0 or row >= self.height:
            return
        self.pixels[row, :, 0] = r_row
        self.pixels[row, :, 1] = g_row
        self.pixels[row, :, 2] = b_row
        self.rows_completed = max(self.rows_completed, row + 1)

    def save_png(self, path: str) -> bool:
        if not HAVE_NUMPY:
            return False
        try:
            # Avoid a hard dependency on Pillow: write a minimal PPM (which
            # every image viewer and `convert`/ffmpeg can read) if Pillow
            # isn't available, else a proper PNG.
            try:
                from PIL import Image
                Image.fromarray(self.pixels, "RGB").save(path)
                self.last_save_was_png = True
                return True
            except ImportError:
                ppm_path = path.rsplit(".", 1)[0] + ".ppm"
                with open(ppm_path, "wb") as f:
                    f.write(f"P6\n{self.width} {self.height}\n255\n".encode())
                    f.write(self.pixels.tobytes())
                self.last_save_was_png = False
                return True
        except Exception:
            return False


class SstvDecoder:
    """
    Runs VIS detection then per-mode line decoding against a live audio
    stream. Feed raw float32 audio samples via `feed()`; poll `snapshot()`
    for the current partial image and status.
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.vis_detector = VisDetector(sample_rate)
        self._buf: List[float] = []
        self._lock = threading.Lock()
        self.state = "listening"  # listening | decoding | done
        self.mode: Optional[ModeSpec] = None
        self.image: Optional[SstvImageBuffer] = None
        self.current_row = 0
        self.last_error = ""
        self._decode_pos = 0  # sample index within buffer where decode resumes

    def feed(self, samples) -> None:
        with self._lock:
            self._buf.extend(samples)
            # Cap buffer growth once we're purely decoding forward.
            max_len = self.sample_rate * 60
            if len(self._buf) > max_len:
                trim = len(self._buf) - max_len
                self._buf = self._buf[trim:]
                self._decode_pos = max(0, self._decode_pos - trim)

    def _samples_view(self):
        if not HAVE_NUMPY:
            return []
        return np.array(self._buf, dtype=np.float32)

    def process(self) -> None:
        """Call periodically (e.g. once per UI tick) to advance decoding."""
        with self._lock:
            buf = self._samples_view()

        if not HAVE_NUMPY or len(buf) == 0:
            return

        if self.state == "listening":
            detected = self.vis_detector.try_detect(buf)
            if detected is not None:
                self.mode = detected
                self.image = SstvImageBuffer(detected.line_pixels, detected.image_lines)
                self.current_row = 0
                header_samples = int(
                    (VIS_LEADER_SEC + VIS_BREAK_SEC + VIS_LEADER_SEC + VIS_START_SEC
                     + VIS_BIT_SEC * 8 + VIS_STOP_SEC) * self.sample_rate
                )
                self._decode_pos = header_samples
                self.state = "decoding"
            else:
                # Keep only the most recent ~1s to bound the search window
                # while still catching a header that starts mid-buffer.
                with self._lock:
                    keep = self.sample_rate * 2
                    if len(self._buf) > keep:
                        self._buf = self._buf[-keep:]
            return

        if self.state == "decoding" and self.mode is not None and self.image is not None:
            self._decode_available_lines(buf)

    def _decode_available_lines(self, buf) -> None:
        mode = self.mode
        line_samples = int(
            (mode.sync_seconds + mode.porch_seconds + mode.pixel_seconds * mode.line_pixels * 3
             + mode.septr_seconds * 2) * self.sample_rate
        )
        # Extra samples of context on each side of the line so the
        # Hilbert-transform frequency curve has settling room and we
        # never sample a pixel's frequency from right at a buffer edge
        # (see line_instantaneous_freq_curve's docstring).
        pad = max(8, int(0.001 * self.sample_rate))  # ~1ms of padding
        while self.current_row < mode.image_lines:
            start = self._decode_pos
            end = start + line_samples
            padded_start = max(0, start - pad)
            padded_end = end + pad
            if padded_end > len(buf):
                break
            line = buf[padded_start:padded_end]
            line_offset = start - padded_start  # where the real line begins within `line`
            self._decode_one_line(line, line_offset, mode, self.current_row)
            self._decode_pos = end
            self.current_row += 1

        if self.current_row >= mode.image_lines:
            self.state = "done"

    def _decode_one_line(self, line, line_offset: int, mode: ModeSpec, row: int) -> None:
        sr = self.sample_rate
        pixel_n = max(1, int(mode.pixel_seconds * sr))
        sync_n = int(mode.sync_seconds * sr)
        porch_n = int(mode.porch_seconds * sr)
        septr_n = int(mode.septr_seconds * sr)
        w = mode.line_pixels

        # One continuous frequency curve for the ENTIRE (padded) line,
        # computed once — not re-estimated per pixel window. This is
        # what makes fast modes like Robot 36 (as few as ~6 samples per
        # pixel) resolvable at all; see module docstring.
        freq_curve = line_instantaneous_freq_curve(line, sr)

        def sample_freq_at(sample_index: int) -> float:
            idx = line_offset + sample_index
            idx = max(0, min(len(freq_curve) - 1, idx))
            return float(freq_curve[idx])

        def read_channel(offset: int):
            values = []
            for px in range(w):
                center = offset + px * pixel_n + pixel_n // 2
                f = sample_freq_at(center)
                values.append(freq_to_luminance(f))
            return values, offset + w * pixel_n

        if mode.layout == "rgb_sequential_linestart":
            # Martin: sync -> porch -> G -> septr -> B -> septr -> R
            pos = sync_n + porch_n
            g, pos = read_channel(pos)
            pos += septr_n
            b, pos = read_channel(pos)
            pos += septr_n
            r, pos = read_channel(pos)
            self.image.set_row_rgb(row, r, g, b)

        elif mode.layout == "rgb_sequential_scottie":
            # Scottie: sync sits mid-line (between B and R); channel order
            # per line is G -> septr -> B -> SYNC -> porch -> septr -> R.
            pos = 0
            g, pos = read_channel(pos)
            pos += septr_n
            b, pos = read_channel(pos)
            pos += sync_n + porch_n
            r, pos = read_channel(pos)
            self.image.set_row_rgb(row, r, g, b)

        elif mode.layout == "robot_yuv":
            # Robot 36/72: sync -> porch -> Y (full width) -> septr ->
            # alternating Cr/Cb (half width, chroma shared with adjacent
            # row in the real spec; here each row gets its own reduced-
            # width chroma pass reconstructed to full width for display).
            pos = sync_n + porch_n
            y_vals, pos = read_channel(pos)
            pos += septr_n
            chroma_width = w // 2
            chroma_pixel_n = max(1, int(mode.pixel_seconds * sr))
            chroma_vals = []
            for px in range(chroma_width):
                center = pos + px * chroma_pixel_n + chroma_pixel_n // 2
                f = sample_freq_at(center)
                chroma_vals.append(freq_to_luminance(f) - 128)
            # Upsample chroma to full width (nearest-neighbor).
            chroma_full = [chroma_vals[min(len(chroma_vals) - 1, px * chroma_width // w)]
                           for px in range(w)] if chroma_vals else [0] * w

            # Convert YUV-ish values to RGB for display (BT.601-style,
            # approximate — sufficient for a live terminal preview).
            r_row, g_row, b_row = [], [], []
            for px in range(w):
                yv = y_vals[px]
                uv = chroma_full[px]
                r_row.append(max(0, min(255, int(yv + 1.402 * uv))))
                g_row.append(max(0, min(255, int(yv - 0.344 * uv))))
                b_row.append(max(0, min(255, int(yv + 1.772 * uv))))
            self.image.set_row_rgb(row, r_row, g_row, b_row)

    def snapshot(self):
        return self.state, self.mode, self.image, self.current_row


class AudioCaptureController:
    """
    Captures audio for SSTV decoding via one of two paths:
      - rtl_fm piped from an SDR tuned to an SSTV frequency (e.g. HAM 2m,
        or 145.800MHz during ISS SSTV events), demodulated as narrow FM.
      - the system's default input device via `sounddevice`, for feeding
        audio from an external radio's speaker/headphone output.
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.rtl_fm_proc: Optional[subprocess.Popen] = None
        self._stream = None
        self.mode = "none"  # "rtl_fm" | "mic" | "none"
        self.last_error = ""

    def start_rtl_fm(self, freq_hz: int, decoder: SstvDecoder) -> bool:
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
            assert self.rtl_fm_proc is not None
            bytes_per_sample = 2  # rtl_fm outputs signed 16-bit PCM
            chunk_samples = 2048
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

    def start_mic(self, decoder: SstvDecoder) -> bool:
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


_PREVIEW_SHADES = " .:-=+*#%@"


def _render_ascii_preview(win, image: SstvImageBuffer, rows_done: int) -> None:
    """Render a coarse ASCII/grayscale preview of the image-so-far,
    downsampled to fit the terminal window."""
    win_h, win_w = win.getmaxyx()
    win_h = max(1, win_h - 1)
    win_w = max(1, win_w - 1)
    if not HAVE_NUMPY or image.pixels is None or rows_done == 0:
        return

    for out_row in range(win_h):
        src_row = int(out_row * rows_done / win_h)
        if src_row >= rows_done:
            break
        for out_col in range(win_w):
            src_col = int(out_col * image.width / win_w)
            r, g, b = image.pixels[src_row, src_col]
            lum = int(0.299 * r + 0.587 * g + 0.114 * b)
            shade = _PREVIEW_SHADES[min(len(_PREVIEW_SHADES) - 1, lum * len(_PREVIEW_SHADES) // 256)]
            try:
                win.addch(out_row, out_col, shade)
            except curses.error:
                pass


def run(stdscr) -> None:
    """
    Keys: [Q] back to menu   [M] switch input source (rtl_fm SDR / mic)
          [F] set rtl_fm frequency (cycles common SSTV calling freqs)
          [R] reset/restart listening for a new VIS header
          [S] save current image to captures/sstv/
    """
    curses.curs_set(0)
    radar_ui.init_colors()

    capture_dir_error = ""
    try:
        os.makedirs(CAPTURE_DIR_DEFAULT, exist_ok=True)
    except OSError as e:
        capture_dir_error = f"Cannot create captures dir: {e}"

    decoder = SstvDecoder()
    capture = AudioCaptureController()

    sstv_freqs = [
        ("14.230 MHz (HF SSTV calling)", 14_230_000),
        ("145.800 MHz (ISS SSTV)", 145_800_000),
        ("144.500 MHz (2m SSTV calling, IARU R1)", 144_500_000),
    ]
    freq_idx = 0
    input_source = "rtl_fm"  # or "mic"

    def _start_capture() -> None:
        if input_source == "rtl_fm":
            capture.start_rtl_fm(sstv_freqs[freq_idx][1], decoder)
        else:
            capture.start_mic(decoder)

    if not HAVE_NUMPY:
        capture.last_error = "numpy not installed — SSTV decoding unavailable"
    else:
        _start_capture()

    height, width = stdscr.getmaxyx()
    preview_win = curses.newwin(height - 6, width, 0, 0)
    status_win = curses.newwin(6, width, height - 6, 0)

    stdscr.nodelay(True)
    stdscr.timeout(150)

    saved_message = ""  # transient feedback shown after pressing S

    try:
        while True:
            key = stdscr.getch()
            if key in (ord("q"), ord("Q"), 27):
                break
            elif key in (ord("m"), ord("M")):
                input_source = "mic" if input_source == "rtl_fm" else "rtl_fm"
                _start_capture()
            elif key in (ord("f"), ord("F")):
                freq_idx = (freq_idx + 1) % len(sstv_freqs)
                if input_source == "rtl_fm":
                    _start_capture()
            elif key in (ord("r"), ord("R")):
                decoder.state = "listening"
                decoder.mode = None
                decoder.image = None
                decoder.current_row = 0
                saved_message = ""
            elif key in (ord("s"), ord("S")):
                if capture_dir_error:
                    saved_message = capture_dir_error
                elif decoder.image is not None:
                    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                    mode_slug = (decoder.mode.name.lower().replace(" ", "") if decoder.mode else "sstv")
                    path = os.path.join(CAPTURE_DIR_DEFAULT, f"{mode_slug}-{ts}.png")
                    if decoder.image.save_png(path):
                        actual_path = path if decoder.image.last_save_was_png else path.rsplit(".", 1)[0] + ".ppm"
                        saved_message = f"Saved: {os.path.basename(actual_path)}"
                    else:
                        saved_message = "Save FAILED — check captures/sstv/ permissions"
                else:
                    saved_message = "Nothing to save yet"

            decoder.process()
            state, mode, image, current_row = decoder.snapshot()

            preview_win.erase()
            preview_win.border()
            if image is not None:
                _render_ascii_preview(preview_win, image, current_row)
            else:
                msg = "Listening for VIS header (1900Hz leader tone)..."
                try:
                    preview_win.addstr(2, 2, msg)
                except curses.error:
                    pass
            preview_win.noutrefresh()

            status_win.erase()
            status_win.border()
            src_label = f"rtl_fm @ {sstv_freqs[freq_idx][0]}" if input_source == "rtl_fm" else "microphone"
            try:
                status_win.addstr(1, 2, f" Input: {src_label} ", curses.color_pair(radar_ui.PAIR_CYAN))
                if mode is not None:
                    pct = int(100 * current_row / mode.image_lines) if mode.image_lines else 0
                    status_win.addstr(2, 2, f" Mode: {mode.name}   Progress: {current_row}/{mode.image_lines} lines ({pct}%) ",
                                       curses.color_pair(radar_ui.PAIR_GREEN))
                    if state == "done":
                        status_win.addstr(2, 2 + 60, " DECODE COMPLETE — press S to save ",
                                           curses.color_pair(radar_ui.PAIR_YELLOW) | curses.A_BOLD)
                elif capture.last_error:
                    status_win.addstr(2, 2, f" {capture.last_error[:width-4]} ", curses.color_pair(radar_ui.PAIR_RED))
                if saved_message:
                    msg_color = radar_ui.PAIR_RED if "FAILED" in saved_message else radar_ui.PAIR_GREEN
                    status_win.addstr(3, 2, f" {saved_message[:width-4]} ", curses.color_pair(msg_color))
                status_win.addstr(4, 2,
                                   " [M] Input  [F] Freq  [R] Reset  [S] Save  [Q] Quit ",
                                   curses.color_pair(radar_ui.PAIR_CYAN))
            except curses.error:
                pass
            status_win.noutrefresh()

            curses.doupdate()
    finally:
        capture.stop()
