# PAR AVION

**Tactical RF, ADS-B, Maritime AIS, Satellite & Signal Decoding Suite — for the terminal.**

PAR AVION is an all-in-one CLI dashboard for Kali Linux that turns an
RTL-SDR (and optionally a GPS dongle) into a cyberpunk-styled radar
console: live aircraft (ADS-B), ships (AIS), RF spectrum, broadcast
radio, ISS orbit tracking, SSTV images, and Morse/CW — all rendered
with ANSI/Unicode block characters in your terminal.

Every mode is **receive-only**. ADS-B, AIS, satellite TLE data, and
amateur SSTV/CW transmissions on their conventional calling frequencies
are all publicly broadcast/published information — the same sources
behind sites like FlightRadar24, MarineTraffic, and N2YO. PAR AVION does
not transmit on any RF interface.

---

## Requirements

- Kali Linux (Debian-based; other Debian derivatives will likely work)
- An RTL-SDR dongle (v3/v4, Nooelec, etc.) for Airplanes/Waterfalls/Radio/Maritime/SSTV/Morse modes
- A USB GPS dongle (optional) for auto-centering the radar on your location
- Speakers or headphones (for Radio mode's audio output)
- A microphone (optional) as an alternative SSTV/Morse input source
- Python 3.8+

## Installation

```bash
git clone <this-repo>
cd par_avion
chmod +x install.sh
./install.sh
```

The installer will:

1. Install system packages: `rtl-sdr`, `hackrf`, `dump1090` (mutability/FA,
   or built from source if not packaged), `rtl_ais`, `gpsd`, `gpsd-clients`,
   `sox`, `alsa-utils`, `pulseaudio-utils`, `multimon-ng`
2. Write udev rules so your SDR works without root (`plugdev` group)
3. Blacklist the `dvb_usb_rtl28xxu` kernel module, which otherwise grabs
   RTL-SDR dongles before userspace tools can use them
4. Enable `gpsd.socket` so a plugged-in GPS dongle is picked up automatically
5. Install Python dependencies from `requirements.txt` (numpy, scipy,
   sounddevice, pyModeS, skyfield, etc.)

**After install:** unplug/replug your SDR and GPS devices, then log out and
back in (or `newgrp plugdev`) so the new group membership takes effect. If
the DVB driver was already loaded, a reboot fully releases the device.

## Running

```bash
python3 par_avion.py
```

You'll land on the main menu:

```
==================================================
  PAR AVION — Tactical RF & Telemetry Suite v2.0
==================================================
  [ 1 ] Airplanes  (ADS-B 1090MHz + Dynamic Tactical Radar)
  [ 2 ] Waterfalls (Spectrum Analyzer & Rolling ASCII Waterfall)
  [ 3 ] Radio      (Broadcast AM/FM Audio Demodulator & Tuner)
  [ 4 ] Maritime   (AIS Vessel Tracking + Tactical Marine Radar)
  [ 5 ] ISS        (ISS Orbit Pass Predictor & Spinning Globe)
  [ 6 ] SSTV       (Slow Scan TV Decoder: Martin, Scottie, Robot)
  [ 7 ] Morse      (CW / Morse Code Audio Decoder & Visualizer)
  [ Q ] Quit
==================================================
```

The menu footer shows detected SDR hardware and GPS fix status. Without a
GPS fix, Airplanes/Maritime radar views fall back to a relative spatial
estimation mode (a `[GPS OFFLINE]` banner, contacts placed at stable but
not-to-scale positions) rather than treating (0,0) as your real location.

## Keybindings

| Mode | Keys | Action |
|---|---|---|
| Main menu | `1`-`7` | Enter a mode |
| Main menu | `Q` | Quit |
| Any mode | `Q` / `Esc` | Return to main menu |
| Airplanes | `S` | Start/restart dump1090 (shows why it isn't running) |
| Waterfalls | `<-`/`->` | Tune +/-100 kHz; `Shift+<-`/`Shift+->` fine-tune +/-10 kHz |
| Waterfalls | `Up`/`Down` | Cycle band presets |
| Radio | `Space` | Play/stop audio |
| Radio | `<-`/`->` | Tune by channel step; `Shift+<-`/`Shift+->` fine-tune |
| Radio | `Up`/`Down` | Cycle presets; `+`/`-` volume; `B` toggle FM/AM |
| Maritime | `S` | Restart rtl_ais |
| ISS | `R` | Force TLE refresh from CelesTrak |
| SSTV | `M` | Switch input (rtl_fm SDR / microphone) |
| SSTV | `F` | Cycle SSTV calling frequencies; `R` reset; `S` save image |
| Morse | `M` | Switch input (rtl_fm SDR / microphone) |
| Morse | `F` | Cycle CW calling frequencies; `T` cycle tone Hz; `R` flush |

Airplanes and Maritime both show a live status line explaining exactly
why there's no feed (binary not found, process exited, port in use,
etc.) rather than a bare "no data" — press `S` to retry after fixing
whatever it reports.

**Note on running dump1090 yourself:** if you'd rather manage dump1090
in its own terminal, include `--net --raw` so PAR AVION can connect to
it: `dump1090 --interactive --net --raw`. A plain `dump1090 --interactive`
with no networking flags won't open the port PAR AVION connects to.

## Module Overview

```
par_avion/
├── par_avion.py        Main entry point — banner, menu, mode dispatch
├── install.sh           Kali setup: apt packages, udev, blacklist, pip
├── requirements.txt      Python dependencies
└── modules/
    ├── hardware.py       SDR/GPS auto-detection (lsusb, gpsd, /dev/ttyUSB*)
    ├── radar_ui.py       Shared curses widgets: radar sweep w/ compass
    │                     overlay, GPS-offline fallback, contact list,
    │                     map, table, spinning globe
    ├── airplanes.py      dump1090 controller + ADS-B decode (pyModeS)
    ├── waterfall.py      RTL-SDR FFT spectrum + scrolling waterfall
    ├── radio.py          Broadcast AM/FM audio demod (rtl_fm -> sox play)
    ├── maritime.py        AIS NMEA/AIVDM decode via rtl_ais
    ├── iss.py            NORAD TLE fetch + Skyfield orbit propagation
    ├── sstv.py           SSTV image decode (VIS header + Hilbert-
    │                     transform continuous frequency tracking)
    └── morse.py          CW/Morse audio decode (Goertzel envelope +
                          adaptive timing classifier)
```

Decoded SSTV images save to `captures/sstv/` (PNG if Pillow is installed,
otherwise `.ppm`, readable by any image viewer or ffmpeg).

## Known limitations

- **Morse (CW) decoding** can misread the *first character* of a
  transmission when the sender's speed is far from ~15-20 WPM (the
  model's initial estimate before it calibrates to the actual speed).
  It fails visibly as `?` rather than silently producing a wrong-but-
  plausible letter, and everything after the first character decodes
  correctly once the timing model locks onto the real speed. A proper
  fix (retroactively re-classifying the opening symbol once
  calibration completes) is planned but not yet implemented — an
  initial attempt caused a broader regression and was reverted rather
  than shipped half-working.
- **SSTV Robot 36/72** decoding is accurate to within a few percent
  (verified against synthetic reference signals) rather than pixel-
  perfect, since those modes' pixel timing is only a few audio samples
  wide even at 44.1kHz — inherent to the mode, not something a better
  algorithm fully eliminates.
- **AIS/ADS-B position decoding** requires two frames of opposite CPR
  parity within a 10-second window (per the ADS-B/AIS spec) — aircraft/
  vessels seen only briefly may show up in the data table without a
  plotted position yet.

## Troubleshooting

- **"No SDR devices detected"** — check `lsusb`, confirm the dongle shows a
  Realtek/HackRF vendor ID, and that udev rules were applied (replug device).
- **dump1090/rtl_ais won't start** — these are spawned as background
  subprocesses; if the binaries aren't on your `PATH`, install them manually
  or re-run `install.sh`, which builds them from source as a fallback.
  Airplanes/Maritime mode's status line will say exactly why (binary not
  found, exited immediately, port already in use) — press `S` to retry.
- **apt says a package "has no installation candidate"** — Kali's package
  set changes over time; `install.sh` checks for a real installable
  candidate before trying `dump1090-mutability`/`dump1090-fa` and falls
  back to building from source automatically.
- **Radio/SSTV/Morse audio pipeline won't start** — these use `rtl_fm`
  (from the `rtl-sdr` package) piped into `sox`'s `play`; the on-screen
  status line names whichever tool is missing. Check `which rtl_fm` and
  `which play`.
- **`ModuleNotFoundError` for `pyModeS.adsb`** — pyModeS v3 removed the
  function-per-field API this app uses. `requirements.txt` pins
  `pyModeS<3,>=2.13`; if you installed pyModeS separately, run
  `pip3 install "pyModeS<3,>=2.13" --break-system-packages --force-reinstall`.
- **`pyrtlsdr` import fails with an `AttributeError` about a missing
  symbol** — apt's `librtlsdr` on Kali is often older than what recent
  `pyrtlsdr` releases expect. `requirements.txt` pins `pyrtlsdr==0.2.93`
  for compatibility.
- **Started dump1090 manually and PAR AVION still says NOT RUNNING** —
  make sure you passed `--net --raw`. A plain `--interactive` instance
  doesn't open the network port PAR AVION connects to.
- **No GPS fix** — Airplanes/Maritime radar falls back to relative
  spatial estimation with a `[GPS OFFLINE]` banner. Run
  `gpsd -N -D 5 /dev/ttyUSB0` (adjust device) in a separate terminal to
  debug a GPS dongle directly.
- **Garbled terminal after a crash** — run `reset` in your shell to restore
  normal terminal state; `par_avion.py` wraps all modes in exception
  handling to avoid this, but a hard kill (`kill -9`) can still leave the
  terminal in raw mode.
