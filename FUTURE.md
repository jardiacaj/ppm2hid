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

Record a new `testdata/ppm_capture_full.wav` exercising all controls, update
`test_ppm2hid.py` to use it, and activate the two skipped assertions.

## Reimplement in rust

## Arduino version

## Recording for each button/axis
