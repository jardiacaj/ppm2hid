# Contributing to ppm2hid

## Prerequisites

- Linux (uinput is Linux-only — this is intentional, not a gap)
- Python 3.11+
- `parecord` for recording live PPM audio (`pulseaudio-utils` package)
- RC transmitter with PPM trainer port + 3.5 mm cable to Line In

No virtual environment or `pip install` is needed — the project uses the
standard library only.

## Clone and run

```bash
git clone https://github.com/jardiacaj/ppm2hid.git
cd ppm2hid
python3 ppm2hid.py --no-joystick --monitor   # decode without /dev/uinput
```

## Running tests

```bash
python3 -m unittest discover -v
```

Both `test_joystick.py` (unit tests, no hardware needed) and `test_ppm2hid.py`
(integration tests against a bundled recording) are discovered and run.

## Recording new test data

```bash
python3 record_ppm.py
```

Auto-detects the PPM source and writes a timestamped raw file. Exercise all
controls during the recording so the decoder test can verify each channel.

## Coding style

- **stdlib only** — no external runtime dependencies. This is a deliberate
  design choice so the tool works anywhere Python 3.11+ is available without a
  `pip install`.
- PEP 8 formatting.
- Use `# MARK: - Section` comments to delimit logical sections within a file.

## Contributor License Agreement

By submitting a pull request you agree that your contribution is licensed under
the same [MIT License](LICENSE) that covers this project. See
[CLA.md](CLA.md) for details.

## Pull request process

1. Fork the repository and create a branch from `main`.
2. Make your changes and ensure `python3 -m unittest discover -v` passes.
3. Add or update tests if the change affects behaviour.
4. Update `README.md` if any CLI flags changed.
5. Open a pull request against `main`.

## Platform note

ppm2hid is Linux-only by design: it depends on `uinput` (Linux kernel) and
`parecord` (PipeWire/PulseAudio). Pull requests adding macOS or Windows support
are welcome, but must not break the Linux path or add mandatory external
dependencies.
