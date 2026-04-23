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

Tested hardware: **Absima CR10P / Dumbo RC DDF-350** transmitter with **Absima R10WP** receiver (identical to DumboRC P10F(G)).

Related project: [ppm2hid_arduino](https://github.com/jardiacaj/ppm2hid_arduino) — equivalent firmware for Arduino Leonardo / Pro Micro; reads PPM directly from the receiver without an audio interface and works on any OS.

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
python3 -m ppm2hid --profile profiles/absima_cr10p.toml
```

The tool auto-detects the audio source carrying the PPM signal, creates a
virtual joystick, and runs until interrupted with Ctrl-C.

## CLI reference

```
Required:
  --profile PATH       TOML transmitter profile

Source (mutually exclusive):
  -s, --audio-source NAME       PipeWire/PulseAudio source name (default: auto-detect)
  -r, --audio-recording PATH   Replay a .wav or raw s16le stereo recording
                               (WAV: sample rate is read from the file header)

Display:
  -m, --monitor        Live channel values in a fixed status line
  --oscilloscope       ASCII waveform of the raw audio per decoded frame
  --debug              Raw pulse timing display

Behaviour:
  --no-joystick        Decode without opening /dev/uinput
  --no-realtime        With --audio-recording: consume as fast as possible
  --threshold N        int16 midpoint for HIGH/LOW detection (default: 0)
  --hysteresis N       int16 dead zone around --threshold (default: 4000)
  --rate HZ            Sample rate in Hz (default: 48000)
```

Higher sample rates (`--rate 96000` or `--rate 192000`) improve pulse timing
precision at the cost of higher CPU usage.

## High sample rate capture (96 kHz / 192 kHz)

Higher sample rates improve PPM pulse timing precision:

| Sample rate | Time per sample | Pulse timing error | Joystick axis steps* |
|-------------|-----------------|-------------------|----------------------|
| 48 000 Hz   | ~20.8 µs        | ±10 µs            | ~38                  |
| 96 000 Hz   | ~10.4 µs        | ±5 µs             | ~77                  |
| 192 000 Hz  | ~5.2 µs         | ±2.5 µs           | ~154                 |

\* Across the default 800 µs axis range (1100–1900 µs), after EMA smoothing with `axis_deadband_us = 2`.
The previous default of `axis_deadband_us = 42` capped resolution at ~19 steps regardless of sample rate.
For reference, a hardware Arduino (ATmega32U4, 4 µs `micros()` resolution) achieves ~200 steps.

### Why `--rate` alone is not enough

When you pass `--rate 192000`, `parecord` *requests* 192 000 samples/s from
PipeWire.  If PipeWire's internal clock is running at 48 000 Hz (its default),
PipeWire captures from ALSA at 48 kHz and upsamples the data before handing it
to `parecord`.  The 192 000 samples you receive are interpolated — the
underlying timing resolution is still ~20 µs, not the expected ~5 µs.

To get genuine high-rate capture three things must align:

1. **The sound card** must support the target rate (many built-in codecs and
   USB docks top out at 48 kHz; dedicated USB audio interfaces usually reach
   96–192 kHz).
2. **WirePlumber** (PipeWire's session manager) must open the ALSA device at
   the target rate.
3. **PipeWire's graph clock** should run at the same rate so no resampling
   occurs inside the graph.

### Step 1 — Check sound card support

```bash
# List all capture devices and their supported rates (run while idle):
cat /proc/asound/card*/stream*
```

Look for a `Capture:` section with your target rate in `Rates:`.  If 192 kHz
is absent, that hardware cannot do it — use a different interface.

To identify which card is your Line In source:

```bash
pactl list sources short
```

### Step 2 — Configure WirePlumber

WirePlumber controls how PipeWire opens ALSA devices.  Create a user-level
drop-in (no root required):

```bash
mkdir -p ~/.config/wireplumber/wireplumber.conf.d/
```

```
# ~/.config/wireplumber/wireplumber.conf.d/50-alsa-192k.conf
monitor.alsa.rules = [
  {
    matches = [{ node.name = "~alsa_input.*" }]
    actions = {
      update-props = {
        audio.rate          = 192000
        audio.allowed-rates = [ 48000 96000 192000 ]
      }
    }
  }
]
```

`audio.allowed-rates` lets PipeWire fall back when other applications need a
different rate — the capture device stays at 192 kHz while PipeWire resamples
for other consumers.

To restrict the rule to a specific card, match on `node.name` or
`alsa.card_name` (visible in `pactl list sources`).

### Step 3 — Configure PipeWire's graph clock

```bash
mkdir -p ~/.config/pipewire/pipewire.conf.d/
```

```
# ~/.config/pipewire/pipewire.conf.d/92-high-rate.conf
context.properties = {
  default.clock.rate          = 192000
  default.clock.allowed-rates = [ 48000 96000 192000 ]
}
```

### Step 4 — Restart PipeWire

```bash
systemctl --user restart pipewire pipewire-pulse wireplumber
```

### Step 5 — Verify

Start `ppm2hid` with `--rate 192000`, then in another terminal:

```bash
# Should show "rate: 192000" while ppm2hid is running:
cat /proc/asound/card*/pcm*c/sub*/hw_params

# Check the active PipeWire graph rate:
pw-top
```

If `hw_params` still shows `rate: 48000`, either the card does not support
192 kHz or the WirePlumber rule did not match — verify the node name with
`pactl list sources short` and adjust the `matches` filter accordingly.

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
python3 -m ppm2hid --profile profiles/absima_cr10p.toml
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
axis_deadband_us         = 2      # axis events suppressed within ±deadband of last EMA-smoothed value
button_threshold_us      = 1500   # raw PPM value above which a button is "pressed"
button_hysteresis_us     = 21     # hysteresis around button_threshold to prevent jitter
slider_low_threshold_us  = 1300   # three_pos alias default: LOW→MID boundary
slider_high_threshold_us = 1700   # three_pos alias default: MID→HI boundary
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

#### N-position slider

Maps a multi-position physical slider to n−1 buttons using a cumulative
encoding: button[k] is pressed in positions k+1 through n−1.  Up to 6
positions (5 buttons) are supported.

```toml
[[channel]]
index         = 7
type          = "n_pos"
codes         = ["BTN_TL", "BTN_TR"]   # n−1 button codes for n positions
thresholds_us = [1300, 1700]           # n−1 thresholds (optional; auto-computed if omitted)
label         = " c7"
```

With the example above (3 positions):

| Slider position | BTN_TL | BTN_TR |
|-----------------|--------|--------|
| Low  (~1100 µs) | off    | off    |
| Mid  (~1500 µs) | **on** | off    |
| High (~1900 µs) | **on** | **on** |

When `thresholds_us` is omitted, the thresholds are distributed evenly across
`[axis_min_us, axis_max_us]`.

> **Legacy alias:** `type = "three_pos"` with `low_code` / `high_code` / optional
> `low_threshold_us` / `high_threshold_us` is still accepted and internally
> converted to `n_pos`.

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
