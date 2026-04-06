# ppm2hid

[![CI](https://github.com/jardiacaj/ppm2hid/actions/workflows/ci.yml/badge.svg)](https://github.com/jardiacaj/ppm2hid/actions/workflows/ci.yml)

Read an RC transmitter PPM signal from a computer's audio Line In jack and
expose it as a Linux virtual joystick via the `uinput` subsystem.

## How it works

RC transmitters encode stick and switch positions as a PPM (Pulse Position
Modulation) signal. When the transmitter's trainer port is connected to the
Line In jack of a sound card, `ppm2hid` captures the audio stream with
`parecord`, decodes the pulse timing in Python, and creates a `/dev/input/js*`
joystick device that any game or simulator can use directly.

The audio source is auto-detected — the tool probes each available
PipeWire/PulseAudio source and picks the one carrying a valid PPM signal.
Inverted (LOW-active) PPM signals are also detected and handled automatically.

Tested hardware: **Absima CR10P / Dumbo RC DDF-350** transmitter.

## Requirements

### System

- **Linux** — `uinput` is a Linux kernel feature; macOS/Windows are not supported.
- **`uinput` kernel module** — load once with `sudo modprobe uinput`, or add
  `uinput` to `/etc/modules-load.d/` to persist across reboots.
- **`/dev/uinput` write access** — without root, add a udev rule:
  ```
  # /etc/udev/rules.d/99-uinput.rules
  KERNEL=="uinput", GROUP="uinput", MODE="0660"
  ```
  Then create the group and add yourself:
  ```bash
  sudo groupadd -f uinput
  sudo usermod -aG uinput $USER
  # log out and back in (or: newgrp uinput)
  ```
- **PipeWire or PulseAudio** with the `parecord` utility.  
  On Debian/Ubuntu: `sudo apt install pulseaudio-utils`  
  On Fedora: `sudo dnf install pulseaudio-utils`

### Python

- Python 3.11+ (stdlib only — no `pip install` required)

### Hardware

- RC transmitter with a **PPM trainer port** (3.5 mm mono or stereo jack)
- 3.5 mm cable from the transmitter trainer port to the computer **Line In** jack

## Quick start

```bash
git clone https://github.com/jardiacaj/ppm2hid.git
cd ppm2hid
python3 -m ppm2hid
```

The tool auto-detects the audio source carrying the PPM signal, creates a
virtual joystick, and runs until interrupted with Ctrl-C.

## CLI reference

```
Source (mutually exclusive):
  -d, --device NAME    PipeWire/PulseAudio source name (default: auto-detect)
  -f, --file PATH      Replay a .wav or raw s16le stereo recording
                       (WAV: sample rate is read from the file header)

Display:
  -m, --monitor        Live channel values in a fixed status line
  --oscilloscope       ASCII waveform of the raw audio per decoded frame
  --debug              Raw pulse timing display

Behaviour:
  --no-joystick        Decode without opening /dev/uinput
  --no-mixer           Skip ALSA Input Source switching
  --no-realtime        With --file: consume as fast as possible
  --threshold N        int16 midpoint for HIGH/LOW detection (default: 0)
  --hysteresis N       int16 dead zone around --threshold (default: 4000)
  --rate HZ            Sample rate in Hz (default: 48000)
```

Higher sample rates (`--rate 96000` or `--rate 192000`) improve pulse timing
precision at the cost of higher CPU usage.

## Utilities

**`record_ppm.py`** — auto-detects the PPM audio source and writes a WAV
recording. After recording it prints the exact command to replay the file
through `ppm2hid`:

```bash
python3 record_ppm.py                             # records until Ctrl-C
python3 record_ppm.py --name sweep --duration 3   # 3 s → testdata/sweep.wav
```

## Profiles

A profile configures the channel mapping and signal timing for your transmitter.

```bash
python3 -m ppm2hid --config profiles/absima_cr10p.toml
```

See `profiles/absima_cr10p.toml` for a working example with all sections and fields.

### `[source]` section

```toml
[source]
device_name = "My Transmitter"   # shown at startup; optional
```

### `[signal]` section

All timing values are in microseconds (µs).  Fields not listed keep the built-in
Absima CR10P defaults.

```toml
[signal]
axis_min_us              = 1100   # minimum expected channel pulse width
axis_max_us              = 1900   # maximum expected channel pulse width
axis_center_us           = 1500   # neutral / centre value for axes
axis_deadband_us         = 42     # axis events suppressed within ±deadband of last sent value
button_threshold_us      = 1500   # raw PPM value above which a button is "pressed"
button_hysteresis_us     = 21     # hysteresis around button_threshold to prevent jitter
slider_low_threshold_us  = 1300   # three_pos: LOW→MID boundary
slider_high_threshold_us = 1700   # three_pos: MID→HI boundary
sync_min_us              = 3000   # shortest pulse treated as a frame sync
sync_max_us              = 50000  # longest pulse treated as a frame sync
channel_min_us           = 500    # shortest pulse treated as a valid channel value
channel_max_us           = 2100   # longest pulse treated as a valid channel value
```

### `[[channel]]` entries

Each PPM channel is described by one `[[channel]]` entry.  `index` is required
and 1-based.  Entries may appear in any order; gaps leave `None` slots (silently
skipped during output).

#### Proportional axis

```toml
[[channel]]
index  = 1
type   = "axis"
code   = "ABS_X"    # axis code name or raw integer
invert = false      # if true, value is mirrored around centre (optional, default false)
label  = "STR"      # display label in --monitor (optional)
```

Available axis codes: `ABS_X`, `ABS_Y`, `ABS_Z`, `ABS_RX`, `ABS_RY`, `ABS_RZ`,
`ABS_THROTTLE`, `ABS_RUDDER`, `ABS_WHEEL`, `ABS_GAS`, `ABS_BRAKE`.

#### Momentary button

```toml
[[channel]]
index = 3
type  = "button"
code  = "BTN_SOUTH"   # button code name or raw integer
label = " c3"
```

Xbox-style gamepad codes (0x130+): `BTN_SOUTH` (A), `BTN_EAST` (B), `BTN_NORTH` (Y),
`BTN_WEST` (X), `BTN_TL` / `BTN_TR` (bumpers), `BTN_TL2` / `BTN_TR2` (triggers),
`BTN_SELECT`, `BTN_START`, `BTN_MODE`, `BTN_THUMBL` / `BTN_THUMBR` (stick clicks).

Joystick codes (0x120+): `BTN_TRIGGER`, `BTN_THUMB`, `BTN_THUMB2`, `BTN_TOP`,
`BTN_TOP2`, `BTN_PINKIE`, `BTN_BASE` … `BTN_BASE6`, `BTN_DEAD`.
At least one code in this range (0x120–0x12f) is required for a `/dev/input/js*`
device to appear; gamepad-only profiles produce an evdev-only device.

Raw integer codes are accepted for any field that takes a code name.

#### Three-position slider

```toml
[[channel]]
index     = 7
type      = "three_pos"
low_code  = "BTN_TL"    # button sent when slider moves from LOW to MID
high_code = "BTN_TR"    # button sent when slider moves from MID to HI
label     = " c7"
# Per-channel threshold overrides (optional — defaults to [signal] values):
low_threshold_us  = 1300
high_threshold_us = 1700
```

The slider maps to two buttons: `low_code` is pressed in MID and HI positions;
`high_code` is additionally pressed in the HI position.

## Testing

```bash
python3 -m unittest discover -v
```

Test files and what they require:

| Test file | Recording needed | Included |
|-----------|-----------------|----------|
| `test_joystick.py` | none (unit tests) | — |
| `test_display.py` | none (unit tests) | — |
| `test_profile.py` | none (unit tests) | — |
| `test_ppm2hid.py` | `testdata/ppm_capture_192k.wav` | yes |
| `test_ppm_bad_cable.py` | `testdata/ppm_capture.wav` | yes |
| `test_noise.py` | `testdata/noise_tx_off.wav` | yes |
| `test_channel_sweeps.py` | `testdata/ch01_sweep.wav` … `ch08_sweep.wav` | yes |
| `test_cable_reconnect.py` | `testdata/cable_reconnect.wav` | yes |

Tests that require a recording fail if the file is absent. To produce
missing recordings see the instructions at the top of each test file.

## License

[MIT](LICENSE)
