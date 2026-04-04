# ppm2hid

Read an RC transmitter PPM signal from a computer's audio Line In jack and
expose it as a Linux virtual joystick via the `uinput` subsystem.

## How it works

RC transmitters encode stick and switch positions as a PPM (Pulse Position
Modulation) signal.  When the transmitter's trainer port is connected to the
Line In jack of a sound card, `ppm2hid` captures the audio, decodes the pulse
timing, and creates a `/dev/input/js*` joystick device that any game or
simulator can use.

## Requirements

- Linux with `uinput` kernel module loaded (`modprobe uinput`)
- PipeWire or PulseAudio (`parecord`)
- Python 3.8+
- Read/write access to `/dev/uinput` (ACL or `uinput` group)

## Usage

```bash
python3 ppm2hid.py [--device PIPEWIRE_SOURCE] [--monitor] [--no-mixer]
```

- `--device`    PipeWire/PulseAudio source name (default: `alsa_input.pci-0000_00_1f.3.analog-stereo`)
- `--monitor`   Print live channel values to stdout
- `--no-mixer`  Skip automatic ALSA Input Source switching to Line In

The program switches the ALSA `Input Source` mixer control to `Line` on
startup and restores it on exit.

## Channel mapping

| PPM ch | Control | uinput event |
|--------|---------|-------------|
| 1 | Steering | `ABS_STEERING` |
| 2 | Throttle/brake | `ABS_GAS` / `ABS_BRAKE` (stick split at centre) |
| 3 | Button | `BTN_TRIGGER` |
| 4 | Button | `BTN_THUMB` |
| 5 | Auxiliary axis | `ABS_X` |
| 6 | Auxiliary axis | `ABS_Y` |
| 7 | 3-position slider | `BTN_THUMB2` (low) / `BTN_TOP` (high) |
| 8 | Button | `BTN_TOP2` |

## PPM signal format

This project expects **positive/high-active** PPM:

```
[HIGH ≥3 ms sync] [HIGH ch1] [LOW ~416 µs] [HIGH ch2] [LOW ~416 µs] …
```

Channel value = HIGH pulse duration + LOW separator duration (nominally 1100–1900 µs).

## Testing

Record 30 seconds of PPM audio while exercising all controls, then run:

```bash
python3 test_ppm2hid.py
```

See the test file for the expected recording command.

## License

MIT
