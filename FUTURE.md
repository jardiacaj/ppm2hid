# Small ones
* support inverted signal (LOW-active PPM) — add `--invert` flag that negates each sample
  before threshold comparison, or just use `--threshold -1` with the current design

# Future ideas

## Test recording gaps

`testdata/ppm_capture_192k.raw` (192 kHz, 15 s, good cable) was recorded and
passes 9/9 test checks, but two channels were not exercised:

- **ch7 slider** — only mid position seen (1494–1546 µs); LO (< 1300) and HI (> 1700)
  skipped in the test until a new recording covers them.
- **ch8 button** — never pressed (stuck at 1100 µs); press/release skipped in the test.

## Add audio input visualization

Render an oscilloscope-like graph of the unprocessed audio signal pertaining to each frame so manual analysis can be made.

## Profile configuration files

Make the channel-to-axis/button mapping fully configurable via a config file
(TOML) so different transmitters can be supported without code changes.
The current `CHANNEL_MAP` and timing constants are hardcoded for one specific
RC car transmitter.

Example config structure:

```toml
[source]
device_name = "Absima CR10P / Dumbo RC DDF-350"

[signal]
sync_min_us   = 3000
channel_min_us = 500
axis_min_us   = 1100
axis_max_us   = 1900
axis_center_us = 1500
deadband_us   = 42

[[channel]]
type = "axis"
code = "ABS_STEERING"

[[channel]]
type = "gas_brake"
gas_code   = "ABS_GAS"
brake_code = "ABS_BRAKE"

[[channel]]
type = "button"
code = "BTN_TRIGGER"
```

## Learning / auto-calibration mode

An interactive mode that helps a user build a profile for their transmitter:

1. Detect the audio source receiving PPM (auto-discovery, see below).
2. Count channels automatically over several frames.
3. Walk the user through moving each control and detect type (axis / button /
   multi-position) from the observed value range.
4. Write a profile file ready for use.

## Auto-discovery of PPM audio source

Enumerate PipeWire/PulseAudio sources, capture a short burst from each, and
identify which one contains a valid PPM signal (sync pulse + regular channel
cadence) — similar to what was done manually during development.

## Broader configurability

Expose timing constants and thresholds as command-line flags or config keys
while keeping the current values as sensible defaults, so the tool adapts to
transmitters with different PPM parameters without code edits.

## Help me choose a license that welcomes contributions and allows commercial use

## Write a contributions guide

## Reimplement in rust

## Set up GH workflows for building, testing, releasing