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

- Python 3.9+ (stdlib only — no `pip install` required)

### Hardware

- RC transmitter with a **PPM trainer port** (3.5 mm mono or stereo jack)
- 3.5 mm cable from the transmitter trainer port to the computer **Line In** jack

## Quick start

```bash
git clone https://github.com/jardiacaj/ppm2hid.git
cd ppm2hid
python3 ppm2hid.py
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
  --threshold N        int16 HIGH threshold (default: 0)
  --rate HZ            Sample rate in Hz (default: 48000)
```

Higher sample rates (`--rate 96000` or `--rate 192000`) improve pulse timing
precision at the cost of higher CPU usage.

## Utilities

**`record_ppm.py`** — auto-detects the PPM audio source and writes a
timestamped raw recording. After recording it prints the exact command to
replay the file through `ppm2hid.py`:

```bash
python3 record_ppm.py          # records until Ctrl-C
```

## Testing

```bash
python3 -m unittest discover -v
```

This discovers and runs all three test files:

- `test_joystick.py` — unit tests for channel-to-HID event mapping (no hardware needed)
- `test_ppm2hid.py` — decoder integration tests against `testdata/ppm_capture_192k.wav`
  (good cable, 192 kHz, 15 s — included in the repository)
- `test_ppm_bad_cable.py` — decoder recovery tests against `testdata/ppm_capture.wav`
  (bad cable, 48 kHz, 30 s — tests signal drop detection and re-synchronisation)

## License

[MIT](LICENSE)
