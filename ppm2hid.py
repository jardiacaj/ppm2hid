#!/usr/bin/env python3
"""
ppm2hid – RC transmitter PPM audio input → Linux virtual joystick

Captures the PPM signal from the Line In jack (ALC1150, card 0) via
PipeWire/PulseAudio and exposes a /dev/input/js* virtual joystick using
the Linux uinput subsystem.

PPM signal format (this transmitter – positive/high-active):
  HIGH pulse  = channel value  (nominally 1100–1900 µs, centre 1500 µs)
  LOW pulse   = inter-channel separator (~416 µs, constant)
  SYNC pulse  = long HIGH (>3 ms), marks the end/start of each frame

Channel mapping
  ch1  → ABS_X      (steering, left/right)
  ch2  → ABS_Y      (throttle, inverted: push forward = positive, pull back = negative)
  ch3  → BTN_SW_CH3  (button)
  ch4  → BTN_SW_CH4  (button)
  ch5  → ABS_RX      (auxiliary axis)
  ch6  → ABS_RY      (auxiliary axis)
  ch7  → BTN_SL_LO / BTN_SL_HI  (3-position slider)
  ch8  → BTN_SW_CH8  (button)
"""

import argparse
import fcntl
import os
import select
import shutil
import signal
import struct
import subprocess
import sys
import time
import wave


# MARK: - Linux input subsystem constants

# Event types
EV_SYN = 0   # synchronisation marker
EV_KEY = 1   # key / button events
EV_ABS = 3   # absolute axis events

SYN_REPORT = 0   # flush a frame of events to userspace

BUS_USB = 0x03   # pretend to be a USB device (required by uinput)

# Axis codes used in this mapping.
# Codes with lower numbers appear first in joydev (/dev/input/js*), so the
# primary controls (steering, throttle) are assigned the lowest codes.
ABS_X  = 0   # ch1 – steering  (joystick axis 0)
ABS_Y  = 1   # ch2 – throttle  (joystick axis 1)
ABS_RX = 3   # ch5 – aux axis  (joystick axis 2)
ABS_RY = 4   # ch6 – aux axis  (joystick axis 3)

# Joystick button codes (linux/input-event-codes.h, BTN_JOYSTICK range 0x120–0x12f).
# joydev requires at least one code in this range to create /dev/input/js*.
# The kernel assigns "flight-stick" names to the range (TRIGGER, THUMB, TOP, PINKIE…);
# those names have no relation to this RC transmitter's buttons.
# Code order determines /dev/input/js* button numbering: 0x120 → button 0, etc.
BTN_SW_CH3  = 0x120   # ch3  momentary switch → joystick button 0
BTN_SW_CH4  = 0x121   # ch4  momentary switch → joystick button 1
BTN_SL_LO   = 0x122   # ch7  slider low  → joystick button 2
BTN_SL_HI   = 0x123   # ch7  slider high → joystick button 3
BTN_SW_CH8  = 0x124   # ch8  momentary switch → joystick button 4
BTN_SW_CH9  = 0x125   # ch9  (reserved – not yet in CHANNEL_MAP)
BTN_SW_CH10 = 0x126   # ch10 (reserved – not yet in CHANNEL_MAP)

# uinput ioctl numbers
UI_SET_EVBIT   = 0x40045564
UI_SET_KEYBIT  = 0x40045565
UI_SET_ABSBIT  = 0x40045567
UI_DEV_CREATE  = 0x5501
UI_DEV_DESTROY = 0x5502

UINPUT_MAX_NAME_SIZE = 80
ABS_CNT = 64

# input_event layout on 64-bit Linux: timeval(16) + type/code/value(8) = 24 bytes
INPUT_EVENT_STRUCT = 'qqHHi'


# MARK: - PPM timing constants

AUDIO_SAMPLE_RATE = 48_000   # Hz

def _microseconds_to_samples(us):
    return us * AUDIO_SAMPLE_RATE // 1_000_000

AUDIO_THRESHOLD = 0   # int16 zero-crossing: PPM signal swings between +32767 and −32768
                      # (works at all sample rates since the ADC clips both polarity)
                      # Override with --threshold if your audio path has a DC offset.

SYNC_MIN_SAMPLES    = _microseconds_to_samples(3_000)    # >3 ms → sync pulse
SYNC_MAX_SAMPLES    = _microseconds_to_samples(50_000)

CHANNEL_MIN_SAMPLES = _microseconds_to_samples(500)    # min HIGH to accept as channel
CHANNEL_MAX_SAMPLES = _microseconds_to_samples(2_100)  # max HIGH below sync threshold


# MARK: - Axis / button calibration

AXIS_MIN_US    = 1_100
AXIS_MAX_US    = 1_900
AXIS_CENTER_US = (AXIS_MIN_US + AXIS_MAX_US) // 2   # 1500 µs

# Suppress axis events smaller than this to remove quantisation noise.
# ≈ 2 samples at the configured sample rate (e.g. 10 µs at 192 kHz, 42 µs at 48 kHz).
AXIS_DEADBAND_US = max(1, 2 * 1_000_000 // AUDIO_SAMPLE_RATE)

BUTTON_THRESHOLD_US   = AXIS_CENTER_US   # value above this → pressed
# Hysteresis applied around button/slider thresholds to prevent toggling from
# 1-sample jitter (~21 µs at 48 kHz).  Once a button is pressed it stays pressed
# until the value falls BELOW (threshold − hysteresis), and vice-versa.
BUTTON_HYSTERESIS_US  = 21               # ≈ 1 sample at 48 kHz
SLIDER_LOW_THRESHOLD  = 1_300            # ch7 low position threshold (µs)
SLIDER_HIGH_THRESHOLD = 1_700            # ch7 high position threshold (µs)


def samples_to_microseconds(samples):
    return samples * 1_000_000 // AUDIO_SAMPLE_RATE


# MARK: - Channel map
#
# Each entry is a tuple whose first element is the channel type:
#
#   ('axis',      abs_code)           – proportional axis, value passed through
#   ('axis',      abs_code, True)     – proportional axis, value inverted
#   ('button',    btn_code)           – momentary switch → button
#   ('three_pos', low_btn, high_btn)  – three-position slider → two buttons

CHANNEL_MAP = [
    # ch1 – main steering stick (left/right) → first joystick axis
    ('axis',   ABS_X),

    # ch2 – throttle/brake stick, inverted so that pushing forward → positive
    ('axis',   ABS_Y, True),

    # ch3 – momentary switch → button
    ('button', BTN_SW_CH3),

    # ch4 – momentary switch → button
    ('button', BTN_SW_CH4),

    # ch5 – auxiliary proportional channel
    ('axis',   ABS_RX),

    # ch6 – auxiliary proportional channel
    ('axis',   ABS_RY),

    # ch7 – three-position slider:
    #   low  position (PPM ~1100 µs) → neither pressed
    #   mid  position (PPM ~1500 µs) → BTN_SL_LO pressed
    #   high position (PPM ~1900 µs) → BTN_SL_LO + BTN_SL_HI pressed
    ('three_pos', BTN_SL_LO, BTN_SL_HI),

    # ch8 – momentary switch → button
    ('button', BTN_SW_CH8),
]

# Derived sets of all axis and button codes declared in the channel map
_ALL_ABS_CODES = set()
_ALL_BTN_CODES = set()
for _ch in CHANNEL_MAP:
    if _ch[0] == 'axis':
        _ALL_ABS_CODES.add(_ch[1])
    elif _ch[0] == 'button':
        _ALL_BTN_CODES.add(_ch[1])
    elif _ch[0] == 'three_pos':
        _ALL_BTN_CODES.add(_ch[1])
        _ALL_BTN_CODES.add(_ch[2])


# MARK: - PPM frame decoder

class PpmDecoder:
    """
    Stateful decoder that consumes raw int16 audio samples and emits
    complete PPM frames.

    Call feed(sample) for every audio sample.  Returns a list of channel
    values in microseconds when a complete frame is recognised, else None.

    After each completed frame the following attributes are updated:
      last_frame_hz    – frame rate computed from sample counts (stable, no jitter)
      last_debug_lines – list of strings for the debug display (when debug=True)
    """

    def __init__(self, max_channels=10, debug=False,
                 sample_rate=AUDIO_SAMPLE_RATE, threshold=AUDIO_THRESHOLD):
        self.max_channels    = max_channels
        self._debug          = debug
        self._sample_rate    = sample_rate
        self._threshold      = threshold
        # Timing thresholds in samples, derived from sample_rate so the decoder
        # works correctly at 48 kHz, 96 kHz, 192 kHz, etc.
        self._sync_min  = sample_rate * 3_000  // 1_000_000
        self._sync_max  = sample_rate * 50_000 // 1_000_000
        self._ch_min    = sample_rate * 500    // 1_000_000
        self._ch_max    = sample_rate * 2_100  // 1_000_000
        self._current_level  = None
        self._run_length     = 0
        self._synced         = False
        self._pending_high   = None
        self._frame_channels = []
        self._frame_count    = 0
        # Debug accumulation state
        self._dbg_items      = []
        self._dbg_h_pending  = None
        self._dbg_skips      = 0
        # Sample-count-based Hz measurement (immune to audio-chunk jitter)
        self._sample_count     = 0
        self._last_sync_sample = None
        self.last_frame_hz     = 0.0
        # Debug lines ready for the caller to display
        self.last_debug_lines  = []

    def feed(self, sample):
        """
        Process one int16 audio sample.
        Returns a list of µs values when a frame completes, else None.
        """
        self._sample_count += 1
        new_level = 'high' if sample > self._threshold else 'low'

        if new_level == self._current_level:
            self._run_length += 1
            return None

        completed_level  = self._current_level
        completed_length = self._run_length
        self._current_level = new_level
        self._run_length    = 1

        if completed_level is None:
            return None

        return self._process_completed_pulse(completed_level, completed_length)

    def _process_completed_pulse(self, pulse_type, pulse_length_samples):
        """Evaluate a just-completed pulse and update decoder state."""

        if pulse_type == 'high':
            if self._sync_min <= pulse_length_samples <= self._sync_max:
                # Sync pulse.  Compute Hz from samples elapsed since last sync.
                if self._last_sync_sample is not None:
                    elapsed = self._sample_count - self._last_sync_sample
                    self.last_frame_hz = (self._sample_rate / elapsed
                                         if elapsed > 0 else 0.0)
                self._last_sync_sample = self._sample_count

                if self._debug:
                    self.last_debug_lines = self._build_debug_lines(pulse_length_samples)
                    self._dbg_items     = []
                    self._dbg_h_pending = None
                    self._dbg_skips     = 0

                completed_frame = (
                    self._frame_channels[:]
                    if len(self._frame_channels) >= 2
                    else None
                )
                self._frame_channels = []
                self._pending_high   = None
                self._synced         = True
                self._frame_count   += 1
                return completed_frame

            elif (self._synced
                  and self._ch_min <= pulse_length_samples <= self._ch_max):
                self._pending_high = pulse_length_samples
                if self._debug:
                    self._dbg_h_pending = (len(self._frame_channels) + 1,
                                           pulse_length_samples)
            else:
                self._pending_high = None
                if self._debug:
                    self._dbg_skips    += 1
                    self._dbg_h_pending = None

        elif pulse_type == 'low':
            if self._pending_high is not None and self._synced:
                total_samples = self._pending_high + pulse_length_samples
                total_us      = self._smp_to_us(total_samples)

                if self._ch_min <= total_samples <= self._ch_max:
                    clamped_us = max(AXIS_MIN_US, min(AXIS_MAX_US, total_us))
                    if self._debug and self._dbg_h_pending:
                        ch_n, h_smp = self._dbg_h_pending
                        self._dbg_items.append(
                            (ch_n, h_smp, pulse_length_samples, total_us, clamped_us))
                        self._dbg_h_pending = None
                    if len(self._frame_channels) < self.max_channels:
                        self._frame_channels.append(clamped_us)
                else:
                    if self._debug:
                        self._dbg_skips    += 1
                        self._dbg_h_pending = None

                self._pending_high = None

        return None

    def _smp_to_us(self, samples):
        return samples * 1_000_000 // self._sample_rate

    def _build_debug_lines(self, sync_smp):
        """Return a fixed-height list of debug strings for the current frame."""
        FIXED   = self.max_channels + 2
        sync_us = self._smp_to_us(sync_smp)
        hz_str  = f'  {self.last_frame_hz:4.0f}Hz' if self.last_frame_hz > 0 else ''
        vals    = '  '.join(str(v) for v in self._frame_channels)

        lines = [
            f'frame {self._frame_count:4d}  '
            f'sync {sync_smp:4d}smp={sync_us:5d}µs  '
            f'{len(self._frame_channels):2d}ch  [{vals}]{hz_str}'
        ]

        for ch_n, h_smp, l_smp, total_us, clamped_us in self._dbg_items:
            h_us  = self._smp_to_us(h_smp)
            l_us  = self._smp_to_us(l_smp)
            clamp = f'  →clamped {clamped_us}' if clamped_us != total_us else ''
            lines.append(
                f'  ch{ch_n:2d}  '
                f'H {h_smp:4d}smp={h_us:5d}µs '
                f'+ L {l_smp:4d}smp={l_us:5d}µs '
                f'= {total_us:5d}µs{clamp}'
            )

        while len(lines) < FIXED - 1:
            lines.append('')

        lines.append(f'  skipped: {self._dbg_skips}')
        return lines


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

    def __init__(self):
        self._initialized = False
        self._height      = 0
        self._fixed_rows  = 0
        self._dbg_n_lines = 0   # cursor-up counter for non-TTY debug rendering

    @property
    def active(self):
        return self._initialized

    def start(self, fixed_rows):
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
        # Confine automatic scrolling to the log area only
        sys.stdout.write(f'\033[1;{log_rows}r')
        # Clear the fixed status area
        for i in range(fixed_rows):
            sys.stdout.write(f'\033[{log_rows + 1 + i};1H\033[2K')
        # Park cursor at the bottom of the log area ready for the first log line
        sys.stdout.write(f'\033[{log_rows};1H')
        sys.stdout.flush()

    def log(self, msg):
        """Write a message to the scrolling log area (or stdout if not active)."""
        if not self._initialized:
            print(msg, flush=True)
            return
        # The cursor lives in the scroll region; writing + newline may scroll it
        sys.stdout.write(f'\r{msg}\033[K\n')
        sys.stdout.flush()

    def update_status(self, lines):
        """Overwrite the fixed status rows at the bottom with `lines`."""
        if not self._initialized:
            return
        log_rows = self._height - self._fixed_rows
        out = ['\0337']   # DEC save cursor position
        for i, line in enumerate(lines[:self._fixed_rows]):
            row = log_rows + 1 + i
            out.append(f'\033[{row};1H\r{line:<79}\033[K')
        out.append('\0338')   # DEC restore cursor position
        sys.stdout.write(''.join(out))
        sys.stdout.flush()

    def stop(self):
        """Reset scroll region and move cursor below the status area."""
        if not self._initialized:
            return
        sys.stdout.write(f'\033[r\033[{self._height};1H\n')
        sys.stdout.flush()
        self._initialized = False

    def render_debug_stderr(self, lines):
        """Non-TTY fallback: write debug lines to stderr using cursor-up overwrite."""
        if self._dbg_n_lines:
            sys.stderr.write(f'\033[{self._dbg_n_lines}F')
        for line in lines:
            sys.stderr.write(f'\r{line:<79}\033[K\n')
        sys.stderr.flush()
        self._dbg_n_lines = len(lines)


# MARK: - uinput device management

def open_uinput_joystick():
    """
    Create a virtual joystick via /dev/uinput.
    Registers every axis and button code declared in CHANNEL_MAP.
    Returns the open file descriptor.
    """
    fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)

    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    for btn_code in sorted(_ALL_BTN_CODES):
        fcntl.ioctl(fd, UI_SET_KEYBIT, btn_code)

    fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
    for abs_code in sorted(_ALL_ABS_CODES):
        fcntl.ioctl(fd, UI_SET_ABSBIT, abs_code)

    absmax  = [0] * ABS_CNT
    absmin  = [0] * ABS_CNT
    absfuzz = [0] * ABS_CNT
    absflat = [0] * ABS_CNT

    for abs_code in _ALL_ABS_CODES:
        absmax[abs_code]  = AXIS_MAX_US
        absmin[abs_code]  = AXIS_MIN_US
        absfuzz[abs_code] = 0              # kernel fuzz disabled; software deadband handles filtering
        absflat[abs_code] = 50             # ~±50 µs flat zone snaps stick-at-rest to zero

    device_name     = b'ppm2joy\x00'.ljust(UINPUT_MAX_NAME_SIZE, b'\x00')
    uinput_user_dev = struct.pack(
        f'{UINPUT_MAX_NAME_SIZE}s HHHH I {ABS_CNT}i {ABS_CNT}i {ABS_CNT}i {ABS_CNT}i',
        device_name, BUS_USB, 0x1209, 0x2641, 1, 0,
        *absmax, *absmin, *absfuzz, *absflat,
    )
    os.write(fd, uinput_user_dev)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    # Give joydev a moment to attach to the new device before sending events.
    time.sleep(0.1)
    # Send initial "released" state for every button so the kernel's state bitmap
    # matches ChannelOutputState's initial state from the first frame onward.
    for btn_code in sorted(_ALL_BTN_CODES):
        _write_input_event(fd, EV_KEY, btn_code, 0)
    _flush_events(fd)
    return fd


def destroy_uinput_joystick(fd):
    """Destroy the virtual joystick and close the file descriptor."""
    try:
        fcntl.ioctl(fd, UI_DEV_DESTROY)
    except OSError:
        pass
    os.close(fd)


def _write_input_event(fd, event_type, event_code, event_value):
    raw = struct.pack(INPUT_EVENT_STRUCT, 0, 0, event_type, event_code, event_value)
    os.write(fd, raw)


def _flush_events(fd):
    _write_input_event(fd, EV_SYN, SYN_REPORT, 0)


# MARK: - Channel output state and event emission

class ChannelOutputState:
    """Tracks last-emitted values to apply deadband and avoid redundant events."""

    def __init__(self):
        self.axis_values   = {code: AXIS_CENTER_US for code in _ALL_ABS_CODES}
        self.button_states = {code: False           for code in _ALL_BTN_CODES}


def emit_channel_events(fd, state, ppm_frame):
    """
    Convert a decoded PPM frame into uinput events and flush with EV_SYN.

    Axis channels marked with invert=True have their value mirrored around
    the centre point before being emitted.

    Returns a list of (channel_label, pressed) for every button state transition
    that occurred this frame — used by the caller to log button events.
    """
    transitions = []

    for channel_index, channel_def in enumerate(CHANNEL_MAP):
        if channel_index >= len(ppm_frame):
            break

        raw_us       = ppm_frame[channel_index]
        channel_type = channel_def[0]

        if channel_type == 'axis':
            abs_code = channel_def[1]
            invert   = len(channel_def) > 2 and channel_def[2]
            value_us = (AXIS_MIN_US + AXIS_MAX_US - raw_us) if invert else raw_us
            if abs(value_us - state.axis_values[abs_code]) >= AXIS_DEADBAND_US:
                state.axis_values[abs_code] = value_us
                _write_input_event(fd, EV_ABS, abs_code, value_us)

        elif channel_type == 'button':
            btn_code = channel_def[1]
            # Hysteresis: raise threshold to press, lower threshold to release.
            # Prevents 1-sample jitter near 1500 µs from toggling the button.
            hys = BUTTON_HYSTERESIS_US if state.button_states[btn_code] else -BUTTON_HYSTERESIS_US
            pressed = raw_us > BUTTON_THRESHOLD_US - hys
            if pressed != state.button_states[btn_code]:
                state.button_states[btn_code] = pressed
                _write_input_event(fd, EV_KEY, btn_code, int(pressed))
                transitions.append((f'ch{channel_index + 1}', pressed))

        elif channel_type == 'three_pos':
            low_btn_code, high_btn_code = channel_def[1], channel_def[2]
            # Hysteresis applied to each slider threshold independently.
            lo_hys = BUTTON_HYSTERESIS_US if state.button_states[low_btn_code]  else -BUTTON_HYSTERESIS_US
            hi_hys = BUTTON_HYSTERESIS_US if state.button_states[high_btn_code] else -BUTTON_HYSTERESIS_US
            # low (~1100 µs) → 0 buttons; mid (~1500) → BTN_SL_LO; high (~1900) → both
            low_pressed  = raw_us > SLIDER_LOW_THRESHOLD  - lo_hys
            high_pressed = raw_us > SLIDER_HIGH_THRESHOLD - hi_hys
            for btn_code, pressed in ((low_btn_code, low_pressed),
                                      (high_btn_code, high_pressed)):
                if pressed != state.button_states[btn_code]:
                    state.button_states[btn_code] = pressed
                    _write_input_event(fd, EV_KEY, btn_code, int(pressed))
                    transitions.append((f'ch{channel_index + 1}', pressed))

    # Always send EV_SYN – ensures button state reaches readers even when nothing changed
    _flush_events(fd)
    return transitions


# MARK: - ALSA mixer helpers

_saved_input_sources = {}

def _amixer_find_input_source_numids(alsa_card):
    """
    Return a list of numids for 'Input Source' controls on the given card,
    one per capture channel, in index order.
    """
    result = subprocess.run(
        ['amixer', '-c', str(alsa_card), 'controls'],
        capture_output=True, text=True,
    )
    numids = []
    for line in result.stdout.splitlines():
        if "name='Input Source'" in line:
            for part in line.split(','):
                if part.startswith('numid='):
                    numids.append(int(part.split('=')[1]))
    return sorted(numids)

def _amixer_cset_numid(alsa_card, numid, value):
    subprocess.run(
        ['amixer', '-c', str(alsa_card), 'cset', f'numid={numid}', value],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

def _amixer_cget_numid(alsa_card, numid):
    """Return the current enum item name for the given numid."""
    result = subprocess.run(
        ['amixer', '-c', str(alsa_card), 'cget', f'numid={numid}'],
        capture_output=True, text=True,
    )
    items = {}
    current_index = None
    for line in result.stdout.splitlines():
        line = line.strip()
        m_item = line.startswith('; Item #')
        if m_item:
            idx   = int(line.split('#')[1].split(' ')[0])
            label = line.split("'")[1]
            items[idx] = label
        elif line.startswith(': values='):
            current_index = int(line.split('=')[1])
    if current_index is not None:
        return items.get(current_index)
    return None

def switch_alsa_input_to_line_in(alsa_card=0):
    """Save current ALSA Input Source settings, then switch all channels to Line."""
    numids = _amixer_find_input_source_numids(alsa_card)
    for i, numid in enumerate(numids):
        original = _amixer_cget_numid(alsa_card, numid)
        if original:
            _saved_input_sources[i] = (numid, original)
        _amixer_cset_numid(alsa_card, numid, 'Line')

def restore_alsa_input_sources(alsa_card=0):
    """Restore ALSA Input Source settings saved before we changed them."""
    fallback_defaults = ['Rear Mic', 'Front Mic']
    numids = _amixer_find_input_source_numids(alsa_card)
    for i, numid in enumerate(numids):
        if i in _saved_input_sources:
            _, value = _saved_input_sources[i]
        else:
            value = fallback_defaults[i] if i < len(fallback_defaults) else 'Rear Mic'
        _amixer_cset_numid(alsa_card, numid, value)


# MARK: - Audio file helpers (WAV + raw)

def _validate_wav(wf):
    """Raise wave.Error if wf is not s16le stereo."""
    if wf.getnchannels() != 2:
        raise wave.Error(f'expected stereo (2 ch), got {wf.getnchannels()}')
    if wf.getsampwidth() != 2:
        raise wave.Error(f'expected 16-bit samples, got {wf.getsampwidth() * 8}-bit')


class _WavSource:
    """Wraps wave.Wave_read to expose a .read(n_bytes) interface."""

    def __init__(self, wf):
        self._wf = wf
        self._bytes_per_frame = wf.getnchannels() * wf.getsampwidth()

    @property
    def sample_rate(self):
        return self._wf.getframerate()

    def read(self, n_bytes):
        n_frames = max(1, n_bytes // self._bytes_per_frame)
        return self._wf.readframes(n_frames)

    def close(self):
        self._wf.close()


def open_audio_file(path, hint_rate=AUDIO_SAMPLE_RATE):
    """
    Open a .wav or raw s16le stereo audio file.
    Returns (source, actual_sample_rate).
    For .wav files the sample rate is read from the file header; hint_rate is ignored.
    """
    if path.lower().endswith('.wav'):
        wf = wave.open(path, 'rb')
        _validate_wav(wf)
        return _WavSource(wf), wf.getframerate()
    return open(path, 'rb'), hint_rate


def _get_file_sample_rate(path, hint_rate=AUDIO_SAMPLE_RATE):
    """Return the sample rate from a .wav header, or hint_rate for raw files."""
    if path.lower().endswith('.wav'):
        try:
            with wave.open(path, 'rb') as wf:
                return wf.getframerate()
        except (wave.Error, OSError):
            pass
    return hint_rate


# MARK: - Audio capture via PipeWire/PulseAudio

def start_audio_capture(pipewire_source_name, sample_rate=AUDIO_SAMPLE_RATE):
    """Launch parecord and return the Popen handle (raw s16le stereo)."""
    return subprocess.Popen(
        [
            'parecord',
            f'--device={pipewire_source_name}',
            '--format=s16le',
            f'--rate={sample_rate}',
            '--channels=2',
            '--raw',
            '--latency-msec=5',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


# MARK: - PPM source auto-discovery

def list_pipewire_sources():
    """Return non-monitor PipeWire/PulseAudio source names via pactl."""
    try:
        result = subprocess.run(
            ['pactl', 'list', 'sources', 'short'],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    sources = []
    for line in result.stdout.splitlines():
        parts = line.split('\t')
        if len(parts) >= 2:
            name = parts[1]
            if not name.endswith('.monitor'):
                sources.append(name)
    return sources


def probe_source_for_ppm(source_name, sample_rate=AUDIO_SAMPLE_RATE,
                          threshold=AUDIO_THRESHOLD, duration_s=0.5):
    """
    Capture a short burst from *source_name* and return the channel index
    (0 = left, 1 = right) where a valid PPM frame is detected, or None if no
    PPM signal is found on either channel.
    """
    # Bytes to read: duration × sample_rate × 2 channels × 2 bytes/sample
    probe_bytes = int(duration_s * sample_rate * 4)
    probe_bytes = (probe_bytes + 3) & ~3   # align to stereo frame boundary

    try:
        proc = subprocess.Popen(
            [
                'parecord',
                f'--device={source_name}',
                '--format=s16le',
                f'--rate={sample_rate}',
                '--channels=2',
                '--raw',
                '--latency-msec=50',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None

    try:
        deadline = time.monotonic() + duration_s + 2
        chunks   = []
        total    = 0
        while total < probe_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not select.select([proc.stdout], [], [], remaining)[0]:
                break   # timeout — source produced no audio
            chunk = proc.stdout.read1(probe_bytes - total)
            if not chunk:   # EOF
                break
            chunks.append(chunk)
            total += len(chunk)
        raw_audio = b''.join(chunks)
    except Exception:
        raw_audio = b''
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    if len(raw_audio) < 8:
        return None

    # s16le stereo layout: [L0 L1 R0 R1] per frame; channel byte offsets are 0 and 2.
    # Try normal then inverted on each channel; prefer normal (invert=False first).
    for channel_index, channel_byte_offset in enumerate((0, 2)):
        for invert in (False, True):
            decoder = PpmDecoder(sample_rate=sample_rate, threshold=threshold)
            for byte_offset in range(0, len(raw_audio) - 3, 4):
                sample = struct.unpack_from('<h', raw_audio, byte_offset + channel_byte_offset)[0]
                if invert:
                    sample = -sample
                if decoder.feed(sample) is not None:
                    return channel_index, invert
    return None


def probe_file_for_ppm(file_path, sample_rate=AUDIO_SAMPLE_RATE,
                        threshold=AUDIO_THRESHOLD, duration_s=0.5):
    """
    Read the first *duration_s* seconds of a .wav or raw s16le stereo file and
    return (channel, invert) where a valid PPM frame is detected, or None if no
    PPM signal is found.  For .wav files the sample rate is read from the header.
    """
    try:
        if file_path.lower().endswith('.wav'):
            with wave.open(file_path, 'rb') as wf:
                _validate_wav(wf)
                sample_rate = wf.getframerate()
                raw_audio   = wf.readframes(int(duration_s * sample_rate))
        else:
            probe_bytes = int(duration_s * sample_rate * 4)
            probe_bytes = (probe_bytes + 3) & ~3
            with open(file_path, 'rb') as f:
                raw_audio = f.read(probe_bytes)
    except (wave.Error, OSError):
        return None
    if len(raw_audio) < 8:
        return None
    for channel_index, channel_byte_offset in enumerate((0, 2)):
        for invert in (False, True):
            decoder = PpmDecoder(sample_rate=sample_rate, threshold=threshold)
            for byte_offset in range(0, len(raw_audio) - 3, 4):
                sample = struct.unpack_from('<h', raw_audio, byte_offset + channel_byte_offset)[0]
                if invert:
                    sample = -sample
                if decoder.feed(sample) is not None:
                    return channel_index, invert
    return None


def discover_ppm_source(sample_rate=AUDIO_SAMPLE_RATE, threshold=AUDIO_THRESHOLD):
    """
    Enumerate PipeWire/PulseAudio sources and return ``(source_name, channel, invert)``
    for the first source that carries a valid PPM signal (channel 0 = left, 1 = right;
    invert=True if the signal is LOW-active), or ``(None, None, False)`` if none found.
    """
    sources = list_pipewire_sources()
    if not sources:
        print('Auto-discovery: no PipeWire/PulseAudio sources found')
        return None, None, False

    print(f'Auto-discovery: probing {len(sources)} source(s) for PPM signal …')
    for source in sources:
        print(f'  {source} … ', end='', flush=True)
        result = probe_source_for_ppm(source, sample_rate=sample_rate, threshold=threshold)
        if result is not None:
            channel, invert = result
            ch_name  = 'left' if channel == 0 else 'right'
            inv_note = ', inverted' if invert else ''
            print(f'PPM detected ({ch_name} channel{inv_note})')
            return source, channel, invert
        print('no signal')

    print('Auto-discovery: no PPM source found')
    return None, None, False


# MARK: - Display helpers

def _axis_bar(value_us, width=6):
    """Fixed-width ASCII bar showing position within [AXIS_MIN_US, AXIS_MAX_US]."""
    fraction = (value_us - AXIS_MIN_US) / (AXIS_MAX_US - AXIS_MIN_US)
    filled   = int(max(0.0, min(1.0, fraction)) * width)
    return '[' + '█' * filled + '░' * (width - filled) + ']'

_MONITOR_LABELS = ['STR', 'THR', ' c3', ' c4', ' RX', ' RY', ' c7', ' c8', ' c9', 'c10']

def _build_monitor_line(ppm_frame, state=None, hz=0.0):
    """
    Return a compact one-line summary of all decoded controls.

    Axes show the post-inversion value.  Button/slider indicators reflect the
    actual joystick state from `state` (after hysteresis) when provided, or
    fall back to a simple threshold comparison against the raw PPM value.
    """
    parts = []
    for channel_index, channel_def in enumerate(CHANNEL_MAP):
        label        = _MONITOR_LABELS[channel_index]
        channel_type = channel_def[0]

        if channel_index >= len(ppm_frame):
            if channel_type == 'axis':
                parts.append(f'{label}:[------]')
            elif channel_type == 'three_pos':
                parts.append(f'{label}: -- ')
            else:
                parts.append(f'{label}:?')
            continue

        raw_us = ppm_frame[channel_index]

        if channel_type == 'axis':
            invert     = len(channel_def) > 2 and channel_def[2]
            display_us = (AXIS_MIN_US + AXIS_MAX_US - raw_us) if invert else raw_us
            parts.append(f'{label}:{_axis_bar(display_us)}')

        elif channel_type == 'button':
            btn_code = channel_def[1]
            if state is not None:
                pressed = state.button_states[btn_code]
            else:
                pressed = raw_us > BUTTON_THRESHOLD_US
            parts.append(f'{label}:{"■" if pressed else "□"}')

        elif channel_type == 'three_pos':
            lo, hi = channel_def[1], channel_def[2]
            if state is not None:
                if state.button_states[hi]:    # both pressed → physical high
                    pos = 'HI '
                elif state.button_states[lo]:  # only lo pressed → physical mid
                    pos = 'MID'
                else:                          # neither → physical low/rest
                    pos = 'LOW'
            else:
                if raw_us > SLIDER_HIGH_THRESHOLD:
                    pos = 'HI '
                elif raw_us > SLIDER_LOW_THRESHOLD:
                    pos = 'MID'
                else:
                    pos = 'LOW'
            parts.append(f'{label}:{pos}({raw_us})')

    hz_tag = f'  [{hz:.0f}Hz]' if hz > 0 else ''
    return ' '.join(parts) + hz_tag


def _render_oscilloscope(samples, threshold=AUDIO_THRESHOLD, width=72, height=7):
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


# MARK: - Entry point

# The decoder buffers up to this many channels per frame.  Higher than
# len(CHANNEL_MAP) so that extra channels from the transmitter (e.g. ch9/ch10)
# appear in --debug / --monitor output even if not yet mapped to joystick events.
PPM_DECODE_MAX_CHANNELS = 12

# Number of consecutive frames with the same channel count before that count
# is accepted as the expected value.  Frames outside the locked count are skipped.
CHANNEL_LOCK_FRAMES = 5

# Wall-clock gap between decoded frames above this threshold triggers a log warning.
SIGNAL_GAP_THRESHOLD_S = 0.2

OSCILLOSCOPE_HEIGHT = 7   # rows used by the --oscilloscope waveform display


def main():
    argument_parser = argparse.ArgumentParser(
        description='PPM RC transmitter audio input → Linux virtual joystick'
    )
    source_group = argument_parser.add_mutually_exclusive_group()
    source_group.add_argument(
        '-d', '--device', default=None,
        help='PipeWire/PulseAudio source device name (default: auto-detect)',
    )
    source_group.add_argument(
        '-f', '--file', default=None, metavar='PATH',
        help='Read from a raw s16le stereo recording instead of a live audio source',
    )
    argument_parser.add_argument(
        '-m', '--monitor', action='store_true',
        help='Show live channel values in a fixed status line',
    )
    argument_parser.add_argument(
        '--no-mixer', action='store_true',
        help="Don't modify the ALSA Input Source mixer control",
    )
    argument_parser.add_argument(
        '--no-joystick', action='store_true',
        help='Decode and display PPM frames without creating a virtual joystick '
             '(useful for testing without /dev/uinput access)',
    )
    argument_parser.add_argument(
        '--oscilloscope', action='store_true',
        help='Show an ASCII waveform of the raw audio for each decoded frame',
    )
    argument_parser.add_argument(
        '--no-realtime', action='store_true',
        help='With --file: consume the recording as fast as possible instead of '
             'at the original sample rate (default: real-time playback)',
    )
    argument_parser.add_argument(
        '--debug', action='store_true',
        help='Show raw pulse timing in a fixed debug display',
    )
    argument_parser.add_argument(
        '--threshold', type=int, default=AUDIO_THRESHOLD,
        metavar='N',
        help=f'int16 level above which the signal is HIGH (default: {AUDIO_THRESHOLD}); '
             f'increase if noise causes spurious frames when transmitter is off',
    )
    argument_parser.add_argument(
        '--rate', type=int, default=AUDIO_SAMPLE_RATE,
        metavar='HZ',
        help=f'Audio sample rate in Hz (default: {AUDIO_SAMPLE_RATE}); '
             f'higher rates (96000, 192000) improve timing precision',
    )
    args = argument_parser.parse_args()

    if not args.no_joystick and not os.path.exists('/dev/uinput'):
        sys.exit('error: /dev/uinput not found – is the uinput kernel module loaded?\n'
                 '       use --no-joystick to run without creating a virtual device')

    mixer_was_modified = False
    audio_channel      = 0    # 0 = left, 1 = right
    audio_invert       = False
    audio_capture_proc = None
    audio_file         = None

    actual_rate = args.rate   # overridden below for .wav files

    if args.file:
        if not os.path.exists(args.file):
            sys.exit(f'error: file not found: {args.file}')
        actual_rate = _get_file_sample_rate(args.file, args.rate)
        print(f'Probing {args.file} for PPM signal … ', end='', flush=True)
        result = probe_file_for_ppm(args.file, actual_rate, args.threshold)
        if result is None:
            sys.exit(
                'no PPM signal found\n'
                '       check --rate matches the recording (see: record_ppm.py --help)'
            )
        audio_channel, audio_invert = result
        ch_name  = 'left' if audio_channel == 0 else 'right'
        inv_note = ', inverted' if audio_invert else ''
        print(f'found ({ch_name} channel{inv_note})')
        audio_source_label = args.file
    else:
        if not args.no_mixer:
            print('Switching ALSA Input Source → Line In …')
            switch_alsa_input_to_line_in()
            mixer_was_modified = True

        if args.device is None:
            args.device, audio_channel, audio_invert = discover_ppm_source(args.rate, args.threshold)
            if args.device is None:
                sys.exit(
                    'error: no PPM source detected automatically\n'
                    '       specify one with --device (see: pactl list sources short)'
                )
        audio_source_label = args.device

    uinput_fd = None
    if args.no_joystick:
        print('Virtual joystick: disabled (--no-joystick)')
    else:
        print('Creating virtual joystick … ', end='', flush=True)
        try:
            uinput_fd = open_uinput_joystick()
        except PermissionError:
            sys.exit('error: cannot open /dev/uinput – check ACL or group membership\n'
                     '       use --no-joystick to run without creating a virtual device')
        print('ok')

    # Calculate how many rows the fixed status area needs
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

    def shutdown(signum=None, frame=None):
        ui.stop()
        print('\nShutting down …')
        if audio_capture_proc:
            audio_capture_proc.terminate()
        if audio_file:
            audio_file.close()
        if uinput_fd is not None:
            destroy_uinput_joystick(uinput_fd)
        if mixer_was_modified:
            print('Restoring ALSA Input Source …')
            restore_alsa_input_sources()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    channel_name = 'left' if audio_channel == 0 else 'right'
    inv_note     = ', inverted' if audio_invert else ''
    ui.log(f'{"File" if args.file else "Capturing from"}: {audio_source_label}  '
           f'({actual_rate} Hz, {channel_name} channel{inv_note})')

    if args.file:
        audio_file, actual_rate = open_audio_file(args.file, actual_rate)
        audio_source = audio_file
    else:
        audio_capture_proc = start_audio_capture(args.device, actual_rate)
        audio_source       = audio_capture_proc.stdout

    ppm_decoder    = PpmDecoder(max_channels=PPM_DECODE_MAX_CHANNELS, debug=args.debug,
                               sample_rate=actual_rate, threshold=args.threshold)
    output_state   = ChannelOutputState()
    frames_decoded = 0
    last_frame_time    = None
    in_signal_gap      = False
    # Channel count stability locking
    expected_ch_count  = None
    candidate_ch_count = None
    stable_count       = 0
    # Oscilloscope buffering (only when --oscilloscope is active)
    osc_buffer       = []   # accumulates samples for the current frame
    osc_frame_samples = []  # samples from the last completed frame

    real_time_file = args.file and not args.no_realtime

    ui.log('Waiting for PPM signal … (Ctrl-C to quit)')
    if real_time_file:
        ui.log('Real-time playback enabled (--no-realtime to disable)')

    AUDIO_CHUNK_BYTES  = 1024 * 4   # 1024 stereo frames × 4 bytes each
    chunk_duration_s   = AUDIO_CHUNK_BYTES / 4 / actual_rate
    next_chunk_deadline = time.monotonic()

    try:
        while True:
            raw_audio = audio_source.read(AUDIO_CHUNK_BYTES)
            if not raw_audio:
                if args.file:
                    ui.log('End of recording.')
                else:
                    ui.log('Audio capture ended unexpectedly')
                break

            channel_byte_offset = audio_channel * 2
            for byte_offset in range(0, len(raw_audio) - 3, 4):
                sample = struct.unpack_from('<h', raw_audio, byte_offset + channel_byte_offset)[0]
                if audio_invert:
                    sample = -sample
                completed_frame = ppm_decoder.feed(sample)

                if args.oscilloscope:
                    osc_buffer.append(sample)

                if completed_frame is None:
                    continue

                if args.oscilloscope:
                    osc_frame_samples = osc_buffer[:]
                    osc_buffer.clear()

                # ── Signal gap detection ──────────────────────────────────────
                now   = time.monotonic()
                gap_s = (now - last_frame_time) if last_frame_time is not None else 0.0
                last_frame_time = now

                if gap_s > SIGNAL_GAP_THRESHOLD_S and not real_time_file:
                    ui.log(f'*** SIGNAL GAP {gap_s:.1f}s ***')
                    in_signal_gap = True
                elif in_signal_gap:
                    ui.log('Signal restored')
                    in_signal_gap = False

                # ── Channel count stability locking ───────────────────────────
                ch_count = len(completed_frame)

                if expected_ch_count is None:
                    if ch_count == candidate_ch_count:
                        stable_count += 1
                        if stable_count >= CHANNEL_LOCK_FRAMES:
                            expected_ch_count = ch_count
                            ui.log(f'Channel count locked: {ch_count}')
                    else:
                        candidate_ch_count = ch_count
                        stable_count       = 1
                elif ch_count != expected_ch_count:
                    ui.log(
                        f'WARNING: channel count {ch_count} ≠ expected '
                        f'{expected_ch_count} — frame skipped'
                    )
                    continue

                # ── Joystick output ───────────────────────────────────────────
                frames_decoded += 1
                if frames_decoded == 1:
                    ui.log(f'PPM signal detected — {ch_count} channels')

                if uinput_fd is not None:
                    btn_transitions = emit_channel_events(uinput_fd, output_state, completed_frame)
                    if btn_transitions:
                        for ch_label, pressed in btn_transitions:
                            ui.log(f'BTN {ch_label}: {"PRESS  ▶" if pressed else "release ◀"}')

                # ── Display update ────────────────────────────────────────────
                if ui.active:
                    status = []
                    if args.monitor:
                        status.append(
                            _build_monitor_line(completed_frame, output_state,
                                                ppm_decoder.last_frame_hz)
                        )
                    if args.debug:
                        status.extend(ppm_decoder.last_debug_lines)
                    if args.oscilloscope and osc_frame_samples:
                        osc_width = min(72, shutil.get_terminal_size(fallback=(80, 24)).columns - 4)
                        status.extend(_render_oscilloscope(
                            osc_frame_samples, args.threshold, osc_width, OSCILLOSCOPE_HEIGHT
                        ))
                    ui.update_status(status)
                else:
                    if args.monitor:
                        line = _build_monitor_line(completed_frame, output_state,
                                                   ppm_decoder.last_frame_hz)
                        sys.stdout.write(f'\r{line}\033[K')
                        sys.stdout.flush()
                    if args.debug and ppm_decoder.last_debug_lines:
                        ui.render_debug_stderr(ppm_decoder.last_debug_lines)

            # ── Real-time throttle (file mode only) ───────────────────────────
            if real_time_file:
                next_chunk_deadline += chunk_duration_s
                sleep_s = next_chunk_deadline - time.monotonic()
                if sleep_s > 0:
                    time.sleep(sleep_s)

    except KeyboardInterrupt:
        pass

    shutdown()


if __name__ == '__main__':
    main()
