"""
ppm2hid – interactive profile generation wizard (--autogenerate mode)

Guides the user to create a TOML profile for a new RC transmitter:
  Phase 1 – lock channel count from the live PPM stream
  Phase 2 – configure each channel (type, output code, label, …)
  Phase 3 – optionally customise signal timing parameters
  Phase 4 – save the TOML profile to disk
"""

from __future__ import annotations

import argparse
import os
import select
import shutil
import struct
import sys
import termios
import time
import tty

from .audio import start_audio_capture
from .decoder import PpmDecoder
from .display import TerminalUI, _render_oscilloscope
from .profile import Profile, _INPUT_CODE_NAMES


# ── Constants (mirror cli.py) ─────────────────────────────────────────────────

CHANNEL_LOCK_FRAMES   = 5
PPM_DECODE_MAX_CHANNELS = 12
OSCILLOSCOPE_HEIGHT   = 7
_AUDIO_CHUNK_BYTES    = 1024 * 4   # 1024 stereo frames × 4 bytes/frame


# ── Input-code lists shown in wizard prompts ──────────────────────────────────

_ABS_NAMES = [
    'ABS_X', 'ABS_Y', 'ABS_Z', 'ABS_RX', 'ABS_RY', 'ABS_RZ',
    'ABS_THROTTLE', 'ABS_RUDDER', 'ABS_WHEEL', 'ABS_GAS', 'ABS_BRAKE',
]
_BTN_NAMES = [
    'BTN_TRIGGER', 'BTN_THUMB', 'BTN_THUMB2', 'BTN_TOP', 'BTN_TOP2',
    'BTN_PINKIE', 'BTN_BASE', 'BTN_BASE2', 'BTN_BASE3', 'BTN_BASE4',
    'BTN_BASE5', 'BTN_BASE6', 'BTN_DEAD',
    'BTN_SOUTH', 'BTN_EAST', 'BTN_NORTH', 'BTN_WEST',
    'BTN_TL', 'BTN_TR', 'BTN_TL2', 'BTN_TR2',
    'BTN_SELECT', 'BTN_START', 'BTN_MODE', 'BTN_THUMBL', 'BTN_THUMBR',
]


# ── Prompt helpers ────────────────────────────────────────────────────────────

def _prompt(msg: str, default: str = '') -> str:
    """Print a prompt with an optional default and return the stripped response."""
    if default:
        raw = input(f'{msg} [{default}]: ').strip()
        return raw if raw else default
    return input(f'{msg}: ').strip()


def _choose_code(code_list: list[str], used_codes: set[str], prompt: str) -> str:
    """
    Print a numbered list of input-event code names and return the chosen one.
    Accepts a list number or a code name typed directly.
    """
    print()
    for i, name in enumerate(code_list, 1):
        suffix = '  (used)' if name in used_codes else ''
        print(f'  {i:2d}. {name}{suffix}')
    print()
    while True:
        raw = input(f'{prompt}: ').strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(code_list):
                return code_list[idx]
            print(f'  Enter a number between 1 and {len(code_list)}.')
        elif raw.upper() in _INPUT_CODE_NAMES:
            return raw.upper()
        elif raw in _INPUT_CODE_NAMES:
            return raw
        else:
            print(f'  Unknown code {raw!r}. Enter a list number or a code name.')


# ── Phase 1: channel-count lock ───────────────────────────────────────────────

def _lock_channel_count(
    audio_source,
    audio_channel: int,
    audio_invert: bool,
    ppm_decoder: PpmDecoder,
) -> int:
    """
    Read PPM frames until the channel count has been stable for
    CHANNEL_LOCK_FRAMES consecutive frames.  Returns the locked count.
    """
    expected: int | None = None
    candidate: int | None = None
    stable: int = 0
    dot_deadline = time.monotonic() + 0.5

    sys.stdout.write('Waiting for PPM signal ')
    sys.stdout.flush()

    while expected is None:
        now = time.monotonic()
        if now >= dot_deadline:
            sys.stdout.write('.')
            sys.stdout.flush()
            dot_deadline = now + 0.5

        raw_audio = audio_source.read(_AUDIO_CHUNK_BYTES)
        if not raw_audio:
            sys.exit('\nerror: audio source ended before a PPM signal was detected')

        ch_off = audio_channel * 2
        for off in range(0, len(raw_audio) - 3, 4):
            sample = struct.unpack_from('<h', raw_audio, off + ch_off)[0]
            if audio_invert:
                sample = -sample
            frame = ppm_decoder.feed(sample)
            if frame is None:
                continue
            n = len(frame)
            if n == candidate:
                stable += 1
                if stable >= CHANNEL_LOCK_FRAMES:
                    expected = n
                    break
            else:
                candidate = n
                stable = 1

    sys.stdout.write('\n')
    return expected  # type: ignore[return-value]


# ── Phase 2: per-channel sweep ────────────────────────────────────────────────

def _read_channel_until_enter(
    audio_source,
    audio_channel: int,
    audio_invert: bool,
    ppm_decoder: PpmDecoder,
    channel_idx: int,
    ui: TerminalUI,
    args: argparse.Namespace,
) -> tuple[int, int]:
    """
    Stream PPM frames from *audio_source*, tracking min/max µs for
    *channel_idx*, until the user presses Enter (or Space).

    Returns (min_us, max_us).  Uses cbreak/raw stdin so keystrokes are
    delivered immediately without the user needing to press Enter twice.
    Live stats are shown either in the TerminalUI status area (when active)
    or as a carriage-return overwrite on stdout.
    """
    min_us = 999_999
    max_us = 0
    osc_buffer: list[int] = []

    old_settings = termios.tcgetattr(sys.stdin.fileno())
    tty.setcbreak(sys.stdin.fileno())
    try:
        while True:
            # Non-blocking check for Enter / Space
            r, _, _ = select.select([sys.stdin], [], [], 0)
            if r:
                ch = sys.stdin.read(1)
                if ch in ('\n', '\r', ' '):
                    break

            raw_audio = audio_source.read(_AUDIO_CHUNK_BYTES)
            if not raw_audio:
                break

            ch_off = audio_channel * 2
            for off in range(0, len(raw_audio) - 3, 4):
                sample = struct.unpack_from('<h', raw_audio, off + ch_off)[0]
                if audio_invert:
                    sample = -sample
                if args.oscilloscope:
                    osc_buffer.append(sample)
                frame = ppm_decoder.feed(sample)
                if frame is None:
                    continue

                osc_frame: list[int] = []
                if args.oscilloscope:
                    osc_frame = osc_buffer[:]
                    osc_buffer.clear()

                if channel_idx >= len(frame):
                    continue

                val = frame[channel_idx]
                if val < min_us:
                    min_us = val
                if val > max_us:
                    max_us = val

                if ui.active:
                    status: list[str] = []
                    if args.monitor:
                        status.append(
                            f'  ch{channel_idx + 1}  '
                            f'{val:5d} µs  '
                            f'[min: {min_us}  max: {max_us}]'
                        )
                    if args.debug:
                        status.extend(ppm_decoder.last_debug_lines)
                    if args.oscilloscope and osc_frame:
                        w = min(72, shutil.get_terminal_size(
                            fallback=(80, 24)).columns - 4)
                        status.extend(_render_oscilloscope(
                            osc_frame, args.threshold, w, OSCILLOSCOPE_HEIGHT))
                    ui.update_status(status)
                else:
                    sys.stdout.write(
                        f'\r  ch{channel_idx + 1}  '
                        f'{val:5d} µs  '
                        f'[min: {min_us:5d}  max: {max_us:5d}]    '
                    )
                    sys.stdout.flush()
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)

    if not ui.active:
        sys.stdout.write('\n')
        sys.stdout.flush()

    if min_us > max_us:   # no frames received
        min_us = 1_100
        max_us = 1_100
    return min_us, max_us


# ── Phase 4: TOML generation ──────────────────────────────────────────────────

def _build_toml(
    device_name: str,
    channel_configs: list[dict],
    signal_params: dict[str, int],
) -> str:
    """Render a TOML profile string from wizard-collected data."""
    lines = [
        '[source]',
        f'device_name = "{device_name}"',
        '',
        '[signal]',
    ]
    p = Profile()
    for field in (
        'axis_min_us', 'axis_max_us', 'axis_center_us',
        'axis_deadband_us', 'button_threshold_us',
        'button_hysteresis_us', 'slider_low_threshold_us',
        'slider_high_threshold_us', 'sync_min_us', 'sync_max_us',
        'channel_min_us', 'channel_max_us',
    ):
        val = signal_params.get(field, getattr(p, field))
        lines.append(f'{field:<32} = {val}')

    for ch in channel_configs:
        lines.append('')
        lines.append('[[channel]]')
        lines.append(f'index = {ch["index"]}')
        ch_type = ch['type']
        lines.append(f'type  = "{ch_type}"')
        if ch_type == 'axis':
            lines.append(f'code  = "{ch["code"]}"')
            if ch.get('invert'):
                lines.append('invert = true')
            lines.append(f'label = "{ch["label"]}"')
        elif ch_type == 'button':
            lines.append(f'code  = "{ch["code"]}"')
            lines.append(f'label = "{ch["label"]}"')
        elif ch_type == 'n_pos':
            codes_str = ', '.join(f'"{c}"' for c in ch['codes'])
            lines.append(f'codes         = [{codes_str}]')
            thresh_str = ', '.join(str(t) for t in ch['thresholds'])
            lines.append(f'thresholds_us = [{thresh_str}]')
            lines.append(f'label = "{ch["label"]}"')

    lines.append('')
    return '\n'.join(lines)


# ── Public entry point ────────────────────────────────────────────────────────

def autogenerate_profile(
    args: argparse.Namespace,
    audio_channel: int,
    audio_invert: bool,
    actual_rate: int,
    source_label: str,
) -> None:
    """
    Interactive wizard that guides the user through creating a TOML profile.

    Called from cli.main() after _setup_audio_source() has determined the
    audio source, channel, and invert flag.
    """
    audio_capture_proc = start_audio_capture(args.audio_source, actual_rate)
    audio_source = audio_capture_proc.stdout

    ppm_decoder = PpmDecoder(
        max_channels=PPM_DECODE_MAX_CHANNELS,
        sample_rate=actual_rate,
        threshold=args.threshold,
        hysteresis=args.hysteresis,
    )

    try:
        _run_wizard(args, audio_source, audio_channel, audio_invert,
                    actual_rate, source_label, ppm_decoder)
    except KeyboardInterrupt:
        print('\nWizard interrupted.')
    finally:
        audio_capture_proc.terminate()
        try:
            audio_capture_proc.wait(timeout=2)
        except Exception:
            audio_capture_proc.kill()


def _run_wizard(
    args: argparse.Namespace,
    audio_source,
    audio_channel: int,
    audio_invert: bool,
    actual_rate: int,
    source_label: str,
    ppm_decoder: PpmDecoder,
) -> None:
    # ── Phase 1: lock channel count ───────────────────────────────────
    ch_count = _lock_channel_count(
        audio_source, audio_channel, audio_invert, ppm_decoder)

    ch_name  = 'left' if audio_channel == 0 else 'right'
    inv_note = ', inverted' if audio_invert else ', normal polarity'
    print()
    print('PPM signal locked:')
    print(f'  Channels  : {ch_count}')
    if ppm_decoder.last_frame_hz > 0:
        print(f'  Frame rate: ~{ppm_decoder.last_frame_hz:.0f} Hz')
    print(f'  Source    : {source_label} ({ch_name} channel{inv_note})')
    print()

    # ── Start TerminalUI if display flags are active ──────────────────
    fixed_rows = 0
    if args.monitor:
        fixed_rows += 1
    if args.debug:
        fixed_rows += PPM_DECODE_MAX_CHANNELS + 2
    if args.oscilloscope:
        fixed_rows += OSCILLOSCOPE_HEIGHT
    ui = TerminalUI()
    if fixed_rows:
        ui.start(fixed_rows)

    try:
        channel_configs = _configure_channels(
            args, audio_source, audio_channel, audio_invert,
            ppm_decoder, ch_count, ui,
        )
    finally:
        ui.stop()

    # ── Phase 3: signal timing ────────────────────────────────────────
    signal_params = _configure_signal_timing()

    # ── Phase 4: save profile ─────────────────────────────────────────
    print()
    device_name = _prompt('Device name (e.g. "Absima CR10P")')

    if args.output:
        out_path = args.output
    else:
        out_path = _prompt('Output file path (e.g. profiles/mycontroller.toml)')

    if os.path.exists(out_path):
        confirm = _prompt(f'{out_path} already exists. Overwrite? [y/N]', 'N').lower()
        if confirm != 'y':
            sys.exit('Aborted.')

    toml_str = _build_toml(device_name, channel_configs, signal_params)
    with open(out_path, 'w') as f:
        f.write(toml_str)
    print(f'Profile saved to {out_path}')


def _configure_channels(
    args: argparse.Namespace,
    audio_source,
    audio_channel: int,
    audio_invert: bool,
    ppm_decoder: PpmDecoder,
    ch_count: int,
    ui: TerminalUI,
) -> list[dict]:
    """Walk the user through configuring each channel. Returns a list of channel dicts."""
    channel_configs: list[dict] = []
    used_abs: set[str] = set()
    used_btn: set[str] = set()

    i = 1
    while i <= ch_count:
        ui.log(f'── Channel {i} ' + '─' * (44 - len(str(i))))

        # Ask type
        _TYPES = [
            ('axis',                    'axis'),
            ('button',                  'button'),
            ('Multi-position slider',   'n_pos'),
            ('skip',                    'skip'),
        ]
        _TYPE_KEYS = [k for _, k in _TYPES]
        print()
        for n, (label, _) in enumerate(_TYPES, 1):
            print(f'  {n}. {label}')
        print()
        while True:
            raw = _prompt('Type?').lower()
            if raw in _TYPE_KEYS:
                raw_type = raw
                break
            # also accept display label (case-insensitive)
            matches = [k for lbl, k in _TYPES if lbl.lower() == raw]
            if matches:
                raw_type = matches[0]
                break
            if raw.isdigit() and 1 <= int(raw) <= len(_TYPES):
                raw_type = _TYPE_KEYS[int(raw) - 1]
                break
            print('  Please enter a number or a type name.')

        if raw_type == 'skip':
            i += 1
            continue

        # Sweep
        ui.log(f'Move ch{i} through its FULL range, then press Enter…')
        min_us, max_us = _read_channel_until_enter(
            audio_source, audio_channel, audio_invert, ppm_decoder,
            i - 1, ui, args,
        )
        print(f'  Observed range: {min_us}–{max_us} µs ({max_us - min_us} µs span)')

        # Build channel config
        cfg: dict = {'index': i}

        if raw_type == 'axis':
            code = _choose_code(_ABS_NAMES, used_abs,
                                 'Output axis code (list number or name)')
            used_abs.add(code)
            invert_raw = _prompt('Invert? [y/N]', 'N').lower()
            label = _prompt('Label (≤4 chars)', f'c{i}')[:4]
            cfg.update({
                'type': 'axis', 'code': code,
                'invert': invert_raw == 'y', 'label': label,
            })

        elif raw_type == 'button':
            code = _choose_code(_BTN_NAMES, used_btn,
                                 'Output button code (list number or name)')
            used_btn.add(code)
            label = _prompt('Label (≤4 chars)', f'c{i}')[:4]
            cfg.update({'type': 'button', 'code': code, 'label': label})

        elif raw_type == 'n_pos':
            while True:
                n_raw = _prompt('Number of positions (2–6)')
                if n_raw.isdigit() and 2 <= int(n_raw) <= 6:
                    n_positions = int(n_raw)
                    break
                print('  Enter a number between 2 and 6.')

            n_buttons = n_positions - 1
            p = Profile()
            span = p.axis_max_us - p.axis_min_us
            auto_thresholds = [
                p.axis_min_us + span * k // n_positions
                for k in range(1, n_positions)
            ]

            print(f'  Auto-spaced thresholds: {auto_thresholds}')
            use_auto = _prompt('Accept auto-spaced thresholds? [Y/n]', 'Y').lower()

            codes: list[str] = []
            thresholds: list[int] = []
            for k in range(n_buttons):
                print(f'\n  Boundary {k + 1} of {n_buttons} '
                      f'(between position {k} and {k + 1}):')
                btn_code = _choose_code(
                    _BTN_NAMES, used_btn,
                    f'  Button code for pos{k}→pos{k + 1}',
                )
                used_btn.add(btn_code)
                codes.append(btn_code)

                if use_auto != 'n':
                    thresholds.append(auto_thresholds[k])
                else:
                    while True:
                        t_raw = _prompt(
                            f'  Threshold µs for boundary {k + 1}',
                            str(auto_thresholds[k]),
                        )
                        try:
                            thresholds.append(int(t_raw))
                            break
                        except ValueError:
                            print('  Enter an integer value in µs.')

            label = _prompt('Label (≤4 chars)', f'c{i}')[:4]
            cfg.update({
                'type': 'n_pos', 'codes': codes,
                'thresholds': thresholds, 'label': label,
            })

        # Show summary and confirm
        _print_channel_summary(cfg)
        confirm = _prompt('Accept? [Y/n/redo]', 'Y').lower()
        if confirm == 'redo' or confirm == 'r':
            continue   # redo this channel without incrementing i
        if confirm == 'n':
            i += 1     # skip this channel
            continue

        channel_configs.append(cfg)
        i += 1

    return channel_configs


def _print_channel_summary(cfg: dict) -> None:
    """Print a one-line summary of a channel configuration."""
    idx  = cfg['index']
    ctyp = cfg['type']
    if ctyp == 'axis':
        inv = ' (inverted)' if cfg.get('invert') else ''
        print(f'  ch{idx}: axis → {cfg["code"]}{inv}  label={cfg["label"]}')
    elif ctyp == 'button':
        print(f'  ch{idx}: button → {cfg["code"]}  label={cfg["label"]}')
    elif ctyp == 'n_pos':
        pairs = ', '.join(
            f'pos{k}→{c}@{t}µs'
            for k, (c, t) in enumerate(zip(cfg['codes'], cfg['thresholds']))
        )
        print(f'  ch{idx}: n_pos  {pairs}  label={cfg["label"]}')


def _configure_signal_timing() -> dict[str, int]:
    """
    Optionally let the user customise signal timing parameters.
    Returns a dict of field→value (may be empty if user accepts defaults).
    """
    p = Profile()
    print()
    print('Signal timing — press Enter to keep the default for each value,')
    print('or type a new integer value in µs.')
    print()

    fields = (
        'axis_min_us', 'axis_max_us', 'axis_center_us',
        'axis_deadband_us', 'button_threshold_us',
        'button_hysteresis_us', 'slider_low_threshold_us',
        'slider_high_threshold_us', 'sync_min_us', 'sync_max_us',
        'channel_min_us', 'channel_max_us',
    )
    params: dict[str, int] = {}
    for field in fields:
        default = getattr(p, field)
        raw = _prompt(f'  {field}', str(default))
        try:
            params[field] = int(raw)
        except ValueError:
            params[field] = default
    return params
