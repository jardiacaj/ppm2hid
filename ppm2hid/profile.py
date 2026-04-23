from __future__ import annotations

import sys

from .constants import (
    ABS_X, ABS_Y, ABS_RX, ABS_RY,
    BTN_SW_CH3, BTN_SW_CH4, BTN_SL_LO, BTN_SL_HI, BTN_SW_CH8,
)

# Registry of kernel input event code names → integer values.
# Used by load_profile() to resolve names in TOML [[channel]] entries.
# Raw integers are also accepted wherever a code name is expected.
_INPUT_CODE_NAMES: dict = {
    # Absolute axes (EV_ABS)
    'ABS_X':        0x00,
    'ABS_Y':        0x01,
    'ABS_Z':        0x02,
    'ABS_RX':       0x03,
    'ABS_RY':       0x04,
    'ABS_RZ':       0x05,
    'ABS_THROTTLE': 0x06,
    'ABS_RUDDER':   0x07,
    'ABS_WHEEL':    0x08,
    'ABS_GAS':      0x09,
    'ABS_BRAKE':    0x0a,
    # BTN_JOYSTICK range (0x120–0x12f) — required for /dev/input/js* creation
    'BTN_TRIGGER': 0x120,
    'BTN_THUMB':   0x121,
    'BTN_THUMB2':  0x122,
    'BTN_TOP':     0x123,
    'BTN_TOP2':    0x124,
    'BTN_PINKIE':  0x125,
    'BTN_BASE':    0x126,
    'BTN_BASE2':   0x127,
    'BTN_BASE3':   0x128,
    'BTN_BASE4':   0x129,
    'BTN_BASE5':   0x12a,
    'BTN_BASE6':   0x12b,
    'BTN_DEAD':    0x12f,
    # BTN_GAMEPAD range (0x130–0x13e) — Xbox-style names
    'BTN_SOUTH':   0x130,   # A
    'BTN_A':       0x130,
    'BTN_EAST':    0x131,   # B
    'BTN_B':       0x131,
    'BTN_NORTH':   0x133,   # Y
    'BTN_Y':       0x133,
    'BTN_WEST':    0x134,   # X
    'BTN_X':       0x134,
    'BTN_TL':      0x136,   # LB (left bumper)
    'BTN_TR':      0x137,   # RB (right bumper)
    'BTN_TL2':     0x138,   # LT (left trigger)
    'BTN_TR2':     0x139,   # RT (right trigger)
    'BTN_SELECT':  0x13a,
    'BTN_START':   0x13b,
    'BTN_MODE':    0x13c,
    'BTN_THUMBL':  0x13d,   # L3
    'BTN_THUMBR':  0x13e,   # R3
}


def _resolve_code(value: int | str) -> int:
    """
    Resolve an input event code name (str) or raw integer to its integer value.
    Raises ValueError for unknown string names.
    """
    if isinstance(value, int):
        return value
    try:
        return _INPUT_CODE_NAMES[value]
    except KeyError:
        raise ValueError(f'unknown input code name: {value!r}')


# MARK: - Transmitter profile

class Profile:
    """
    All configurable parameters for a transmitter.  This is the single source
    of truth for calibration, channel mapping, and display labels.

    ``Profile()`` produces the built-in Absima CR10P / DDF-350 defaults.
    Use ``load_profile(path)`` to override from a TOML file.
    """

    def __init__(self) -> None:
        self.device_name              = ''
        # Signal timing — all in microseconds
        self.axis_min_us              = 1_100
        self.axis_max_us              = 1_900
        self.axis_center_us           = 1_500
        self.axis_deadband_us         = 2
        self.button_threshold_us      = 1_500
        self.button_hysteresis_us     = 21
        self.slider_low_threshold_us  = 1_300
        self.slider_high_threshold_us = 1_700
        self.sync_min_us              = 3_000
        self.sync_max_us              = 50_000
        self.channel_min_us           = 500
        self.channel_max_us           = 2_100
        # Channel map — each entry is a tuple whose first element is the type:
        #   ('axis',  abs_code)                          – proportional axis
        #   ('axis',  abs_code, True)                    – proportional axis, inverted
        #   ('button', btn_code)                         – momentary switch
        #   ('n_pos', (btn_code, ...), (threshold_us, ...))  – n-position slider (n ≤ 6)
        #   None                                         – unmapped slot
        self.channel_map = [
            ('axis',   ABS_X),                                      # ch1 steering
            ('axis',   ABS_Y, True),                                 # ch2 throttle (inverted)
            ('button', BTN_SW_CH3),                                  # ch3 momentary
            ('button', BTN_SW_CH4),                                  # ch4 momentary
            ('axis',   ABS_RX),                                      # ch5 aux axis
            ('axis',   ABS_RY),                                      # ch6 aux axis
            ('n_pos',  (BTN_SL_LO, BTN_SL_HI), (1_300, 1_700)),    # ch7 slider
            ('button', BTN_SW_CH8),                                  # ch8 momentary
        ]
        # Per-channel display labels aligned with channel_map
        self.monitor_labels = ['STR', 'THR', ' c3', ' c4', ' RX', ' RY', ' c7', ' c8']


def load_profile(path: str) -> Profile:
    """
    Load a TOML transmitter profile from *path* and return a Profile.
    Requires Python 3.11+ (tomllib).
    """
    try:
        import tomllib
    except ImportError:
        sys.exit('--config requires Python 3.11+ (or: pip install tomli)')

    with open(path, 'rb') as f:
        data = tomllib.load(f)

    p = Profile()

    if src := data.get('source', {}):
        p.device_name = src.get('device_name', '')

    if sig := data.get('signal', {}):
        for field in ('axis_min_us', 'axis_max_us', 'axis_center_us',
                      'axis_deadband_us', 'button_threshold_us',
                      'button_hysteresis_us', 'slider_low_threshold_us',
                      'slider_high_threshold_us', 'sync_min_us', 'sync_max_us',
                      'channel_min_us', 'channel_max_us'):
            if field in sig:
                setattr(p, field, int(sig[field]))

    channels = data.get('channel', [])
    if channels:
        seen = {}
        for ch in channels:
            if 'index' not in ch:
                raise ValueError("each [[channel]] must have an 'index' field")
            idx = int(ch['index'])
            if idx < 1:
                raise ValueError(f'channel index must be ≥ 1, got {idx}')
            if idx in seen:
                raise ValueError(f'duplicate channel index {idx}')
            seen[idx] = ch

        max_idx = max(seen)
        p.channel_map    = [None] * max_idx
        p.monitor_labels = [f' c{i + 1}' for i in range(max_idx)]

        for idx, ch in seen.items():
            i       = idx - 1
            ch_type = ch.get('type')
            label   = ch.get('label', f' c{idx}')
            p.monitor_labels[i] = label
            if ch_type == 'axis':
                code   = _resolve_code(ch['code'])
                invert = bool(ch.get('invert', False))
                p.channel_map[i] = ('axis', code, invert) if invert else ('axis', code)
            elif ch_type == 'button':
                code = _resolve_code(ch['code'])
                p.channel_map[i] = ('button', code)
            elif ch_type == 'n_pos':
                codes_raw = ch.get('codes', [])
                if not codes_raw:
                    raise ValueError(f'channel {idx}: n_pos requires a "codes" list')
                if not 1 <= len(codes_raw) <= 5:
                    raise ValueError(
                        f'channel {idx}: n_pos "codes" must have 1–5 entries, got {len(codes_raw)}'
                    )
                codes = tuple(_resolve_code(c) for c in codes_raw)
                thresholds_raw = ch.get('thresholds_us')
                if thresholds_raw is not None:
                    if len(thresholds_raw) != len(codes):
                        raise ValueError(
                            f'channel {idx}: thresholds_us must have {len(codes)} entries'
                        )
                    thresholds = tuple(int(t) for t in thresholds_raw)
                else:
                    n = len(codes) + 1
                    span = p.axis_max_us - p.axis_min_us
                    thresholds = tuple(p.axis_min_us + span * k // n for k in range(1, n))
                p.channel_map[i] = ('n_pos', codes, thresholds)
            elif ch_type == 'three_pos':
                lo = _resolve_code(ch['low_code'])
                hi = _resolve_code(ch['high_code'])
                lo_thresh = int(ch.get('low_threshold_us', p.slider_low_threshold_us))
                hi_thresh = int(ch.get('high_threshold_us', p.slider_high_threshold_us))
                p.channel_map[i] = ('n_pos', (lo, hi), (lo_thresh, hi_thresh))
            else:
                raise ValueError(f'channel {idx}: unknown type {ch_type!r}')

    return p
