# PAR AVION

**Tactical RF, ADS-B, Maritime AIS & Satellite Tracking Suite — for the terminal.**

PAR AVION is an all-in-one CLI dashboard for Kali Linux that turns an
RTL-SDR (and optionally a GPS dongle) into a cyberpunk-styled radar
console: live aircraft (ADS-B), ships (AIS), RF spectrum, and ISS orbit
tracking, all rendered with ANSI/Unicode block characters in your terminal.

Every mode is **receive-only**. ADS-B, AIS, and satellite TLE data are all
publicly broadcast/published information — the same sources behind sites
like FlightRadar24, MarineTraffic, and N2YO. PAR AVION does not transmit
on any RF interface.

---

## Requirements

- Kali Linux (Debian-based; other Debian derivatives will likely work)
- An RTL-SDR dongle (v3/v4, Nooelec, etc.) for Airplanes/Radio/Maritime modes
- A USB GPS dongle (optional) for auto-centering the radar on your location
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
   or built from source if not packaged), `rtl_ais`, `gpsd`, `gpsd-clients`
2. Write udev rules so your SDR works without root (`plugdev` group)
3. Blacklist the `dvb_usb_rtl28xxu` kernel module, which otherwise grabs
   RTL-SDR dongles before userspace tools can use them
4. Enable `gpsd.socket` so a plugged-in GPS dongle is picked up automatically
5. Install Python dependencies from `requirements.txt`

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
  PAR AVION — Tactical RF & Telemetry Suite
==================================================
  [ 1 ] Airplanes  (ADS-B 1090MHz + Green Radar)
  [ 2 ] Radio      (Spectrum Analyzer & Waterfall Display)
  [ 3 ] Maritime   (AIS 161.975MHz / 162.025MHz Vessel Tracking)
  [ 4 ] ISS        (ISS Satellite Real-time Orbit & Radar Pass)
  [ Q ] Quit
==================================================
```

The menu footer shows detected SDR hardware and GPS fix status. If no SDR
is found, Radio mode falls back to a simulated spectrum so the UI is still
explorable; Airplanes/Maritime will show "no feed" until a receiver
(dump1090 / rtl_ais) is actually running.

## Keybindings

| Mode | Keys | Action |
|---|---|---|
| Main menu | `1`–`4` | Enter a mode |
| Main menu | `Q` | Quit |
| Any mode | `Q` / `Esc` | Return to main menu |
| Airplanes | — | Auto-refreshing radar + table, no interaction needed |
| Radio | `←` / `→` | Tune ±100 kHz |
| Radio | `Shift+←` / `Shift+→` | Fine-tune ±10 kHz |
| Radio | `↑` / `↓` | Cycle band presets (433 MHz ISM, FM, Airband, 2m/70cm HAM, 315 MHz) |
| Maritime | — | Auto-refreshing radar + vessel table |
| ISS | `R` | Force TLE refresh from CelesTrak |

## Module Overview

```
par_avion/
├── par_avion.py        Main entry point — banner, menu, mode dispatch
├── install.sh           Kali setup: apt packages, udev, blacklist, pip
├── requirements.txt      Python dependencies
└── modules/
    ├── hardware.py       SDR/GPS auto-detection (lsusb, gpsd, /dev/ttyUSB*)
    ├── radar_ui.py       Shared curses widgets: radar sweep, map, table, globe
    ├── airplanes.py      dump1090 controller + ADS-B decode (pyModeS)
    ├── radio.py          RTL-SDR FFT waterfall + live band tuning
    ├── maritime.py        AIS NMEA/AIVDM decode via rtl_ais
    └── iss.py            NORAD TLE fetch + Skyfield orbit propagation
```

## Troubleshooting

- **"No SDR devices detected"** — check `lsusb`, confirm the dongle shows a
  Realtek/HackRF vendor ID, and that udev rules were applied (replug device).
- **dump1090/rtl_ais won't start** — these are spawned as background
  subprocesses; if the binaries aren't on your `PATH`, install them manually
  or re-run `install.sh`, which builds them from source as a fallback.
- **No GPS fix** — modes fall back to `0,0` as the radar center. Run
  `gpsd -N -D 5 /dev/ttyUSB0` (adjust device) in a separate terminal to
  debug a GPS dongle directly.
- **Garbled terminal after a crash** — run `reset` in your shell to restore
  normal terminal state; `par_avion.py` wraps all modes in exception
  handling to avoid this, but a hard kill (`kill -9`) can still leave the
  terminal in raw mode.

## License

No license specified — add one appropriate for your use case before
distributing.
