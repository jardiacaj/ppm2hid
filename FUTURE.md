# Future ideas

## Test recording gaps

Current recordings and their test files:

| File | Rate | Duration | Cable | Test file |
|------|------|----------|-------|-----------|
| `testdata/ppm_capture_192k.wav` | 192 kHz | 15 s | good | `test_ppm2hid.py` |
| `testdata/ppm_capture.wav` | 48 kHz | 30 s | bad (signal drops) | `test_ppm_bad_cable.py` |

The good-cable recording passes 9/9 checks but two channels were not exercised:

- **ch7 slider** — only mid position seen (1494–1546 µs); LO (< 1300) and HI (> 1700)
  skipped in the test until a new recording covers them.
- **ch8 button** — never pressed (stuck at 1100 µs); press/release skipped in the test.

Test infrastructure for the recordings below is ready — tests auto-skip until the
files exist.  Record with `python3 record_ppm.py --name <name> --duration <s>`.

| Recording | Duration | What to do | Test file |
|-----------|----------|------------|-----------|
| `testdata/noise_tx_off.wav` | 3 s | Transmitter **off**, cable in | `test_noise.py` |
| `testdata/ch01_sweep.wav` | 3 s | Move steering full left→right | `test_channel_sweeps.py` |
| `testdata/ch02_sweep.wav` | 3 s | Move throttle full back→forward | `test_channel_sweeps.py` |
| `testdata/ch03_sweep.wav` | 3 s | Press and release ch3 button | `test_channel_sweeps.py` |
| `testdata/ch04_sweep.wav` | 3 s | Press and release ch4 button | `test_channel_sweeps.py` |
| `testdata/ch05_sweep.wav` | 3 s | Move ch5 aux axis full sweep | `test_channel_sweeps.py` |
| `testdata/ch06_sweep.wav` | 3 s | Move ch6 aux axis full sweep | `test_channel_sweeps.py` |
| `testdata/ch07_sweep.wav` | 3 s | Slider: LOW → MID → HI → MID → LOW | `test_channel_sweeps.py` |
| `testdata/ch08_sweep.wav` | 3 s | Press and release ch8 button | `test_channel_sweeps.py` |
| `testdata/cable_reconnect.wav` | 10 s | Signal on → unplug → reconnect | `test_cable_reconnect.py` |

## Reimplement in rust

## Arduino version

