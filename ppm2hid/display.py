from __future__ import annotations

import shutil
import sys

from .constants import DEFAULT_AUDIO_THRESHOLD
from .profile import Profile
from .uinput import ChannelOutputState


# MARK: - Terminal split-view UI

class TerminalUI:
    """
    Split-screen terminal layout when stdout is a TTY:

      ┌─────────────────────────────────────┐  ↑ scrolling log area
      │ 12:34:56 PPM signal detected – 8ch  │    (warnings, errors, info)
      │ 12:34:58 *** SIGNAL GAP 2.3s ***    │
      ├─────────────────────────────────────┤
      │ STR:[███░░░] THR:[███░░░] …  [70Hz] │  ↓ fixed status area
      │ frame  123  sync  196smp …          │    (monitor + debug, no scroll)
      └─────────────────────────────────────┘

    Before start() is called, log() falls back to plain print().
    After start(), log() writes to the scrolling area and update_status()
    overwrites the fixed rows in place.
    """

    def __init__(self) -> None:
        self._initialized = False
        self._height      = 0
        self._fixed_rows  = 0
        self._dbg_n_lines = 0   # cursor-up counter for non-TTY debug rendering

    @property
    def active(self) -> bool:
        return self._initialized

    def start(self, fixed_rows: int) -> None:
        """Reserve `fixed_rows` at the bottom; confine scrolling to the rest."""
        if not sys.stdout.isatty() or fixed_rows == 0:
            return
        size         = shutil.get_terminal_size()
        self._height = size.lines
        self._fixed_rows = fixed_rows
        log_rows     = self._height - fixed_rows
        if log_rows < 3:
            return   # terminal too small – degrade gracefully

        self._initialized = True
        # \033[top;bot r  — DECSTBM: confine terminal scrolling to rows top..bot.
        # Subsequent newlines in that region scroll only within it; the fixed status
        # rows below are never touched by normal text output.
        sys.stdout.write(f'\033[1;{log_rows}r')
        # Clear the fixed status area (move to each row; \033[2K = erase whole line)
        for i in range(fixed_rows):
            sys.stdout.write(f'\033[{log_rows + 1 + i};1H\033[2K')
        # Park cursor at the bottom of the log area ready for the first log line
        sys.stdout.write(f'\033[{log_rows};1H')
        sys.stdout.flush()

    def log(self, msg: str) -> None:
        """Write a message to the scrolling log area (or stdout if not active)."""
        if not self._initialized:
            print(msg, flush=True)
            return
        # The cursor lives in the scroll region; writing + newline may scroll it
        sys.stdout.write(f'\r{msg}\033[K\n')
        sys.stdout.flush()

    def update_status(self, lines: list[str]) -> None:
        """Overwrite the fixed status rows at the bottom with `lines`."""
        if not self._initialized:
            return
        log_rows = self._height - self._fixed_rows
        out = ['\0337']   # ESC 7  — DEC save cursor (position + attributes)
        for i, line in enumerate(lines[:self._fixed_rows]):
            row = log_rows + 1 + i
            # \033[row;1H — absolute cursor position; \033[K — erase to end of line
            out.append(f'\033[{row};1H\r{line:<79}\033[K')
        out.append('\0338')   # ESC 8  — DEC restore cursor (return to log area)
        sys.stdout.write(''.join(out))
        sys.stdout.flush()

    def stop(self) -> None:
        """Reset scroll region and move cursor below the status area."""
        if not self._initialized:
            return
        # \033[r — DECSTBM with no args resets scroll region to full screen
        sys.stdout.write(f'\033[r\033[{self._height};1H\n')
        sys.stdout.flush()
        self._initialized = False

    def render_debug_stderr(self, lines: list[str]) -> None:
        """Non-TTY fallback: write debug lines to stderr using cursor-up overwrite."""
        if self._dbg_n_lines:
            sys.stderr.write(f'\033[{self._dbg_n_lines}F')
        for line in lines:
            sys.stderr.write(f'\r{line:<79}\033[K\n')
        sys.stderr.flush()
        self._dbg_n_lines = len(lines)


# MARK: - Display helpers

def _axis_bar(value_us: int, width: int = 6, axis_min: int = 1_100, axis_max: int = 1_900) -> str:
    """Fixed-width ASCII bar showing position within [axis_min, axis_max]."""
    fraction = (value_us - axis_min) / (axis_max - axis_min)
    filled   = int(max(0.0, min(1.0, fraction)) * width)
    return '[' + '█' * filled + '░' * (width - filled) + ']'


def _build_monitor_line(ppm_frame: list[int], state: ChannelOutputState | None = None,
                        hz: float = 0.0, profile: Profile | None = None) -> str:
    """
    Return a compact one-line summary of all decoded controls.

    Axes show the post-inversion value.  Button/slider indicators reflect the
    actual joystick state from `state` (after hysteresis) when provided, or
    fall back to a simple threshold comparison against the raw PPM value.
    """
    if profile is None:
        profile = Profile()
    cm       = profile.channel_map
    labels   = profile.monitor_labels
    axis_min = profile.axis_min_us
    axis_max = profile.axis_max_us
    btn_thr  = profile.button_threshold_us

    parts = []
    for channel_index, channel_def in enumerate(cm):
        if channel_def is None:
            continue
        label        = labels[channel_index] if channel_index < len(labels) else f' c{channel_index + 1}'
        channel_type = channel_def[0]

        if channel_index >= len(ppm_frame):
            if channel_type == 'axis':
                parts.append(f'{label}:[------]')
            elif channel_type == 'n_pos':
                parts.append(f'{label}:--')
            else:
                parts.append(f'{label}:?')
            continue

        raw_us = ppm_frame[channel_index]

        if channel_type == 'axis':
            invert     = len(channel_def) > 2 and channel_def[2]
            display_us = (axis_min + axis_max - raw_us) if invert else raw_us
            parts.append(f'{label}:{_axis_bar(display_us, axis_min=axis_min, axis_max=axis_max)}')

        elif channel_type == 'button':
            btn_code = channel_def[1]
            if state is not None:
                pressed = state.button_states[btn_code]
            else:
                pressed = raw_us > btn_thr
            parts.append(f'{label}:{"■" if pressed else "□"}')

        elif channel_type == 'n_pos':
            codes, thresholds = channel_def[1], channel_def[2]
            if state is not None:
                pos = sum(1 for btn in codes if state.button_states[btn])
            else:
                pos = sum(1 for t in thresholds if raw_us > t)
            parts.append(f'{label}:P{pos}({raw_us})')

    hz_tag = f'  [{hz:.0f}Hz]' if hz > 0 else ''
    return ' '.join(parts) + hz_tag


def _render_oscilloscope(samples: list[int], threshold: int = DEFAULT_AUDIO_THRESHOLD,
                          width: int = 72, height: int = 7) -> list[str]:
    """
    Render *samples* as a fixed-size ASCII oscilloscope waveform.

    Returns a list of *height* strings.  Each character column covers a bucket
    of samples; the character for each (column, row) pair is:
      '█' – signal amplitude spans this row in this bucket (min-max fill)
      '·' – signal is absent and this row is the threshold level
      ' ' – signal is absent
    """
    if not samples:
        return [' (no samples)'] + [' ' * width] * (height - 1)

    n     = len(samples)
    b_min = []   # per-column minimum amplitude
    b_max = []   # per-column maximum amplitude
    for col in range(width):
        start  = col * n // width
        end    = max(start + 1, (col + 1) * n // width)
        bucket = samples[start:end]
        b_min.append(min(bucket))
        b_max.append(max(bucket))

    # Map int16 amplitude to row index: row 0 = top (+32767), row h-1 = bottom (-32768)
    def amp_to_row(amp):
        frac = (amp + 32768) / 65535          # 0.0 … 1.0
        return int((1.0 - frac) * (height - 1) + 0.5)

    thr_row = amp_to_row(threshold)

    rows = []
    for row in range(height):
        chars = []
        for col in range(width):
            top = amp_to_row(b_max[col])   # high amplitude → low row index
            bot = amp_to_row(b_min[col])   # low  amplitude → high row index
            if top <= row <= bot:
                chars.append('█')
            elif row == thr_row:
                chars.append('·')
            else:
                chars.append(' ')
        rows.append(''.join(chars))
    return rows
