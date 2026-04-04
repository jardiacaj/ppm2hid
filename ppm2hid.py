#!/usr/bin/env python3
"""
ppm2joy – RC transmitter PPM audio input → Linux virtual joystick

Captures the PPM signal from the Line In jack (ALC1150, card 0) via
PipeWire/PulseAudio and exposes a /dev/input/js* virtual joystick using
the Linux uinput subsystem.

PPM signal format (this transmitter – positive/high-active):
  HIGH pulse  = channel value  (nominally 1100–1900 µs, centre 1500 µs)
  LOW pulse   = inter-channel separator (~416 µs, constant)
  SYNC pulse  = long HIGH (>3 ms), marks the end/start of each frame

Channel mapping
  ch1  → ABS_STEERING          (steering axis)
  ch2  → ABS_GAS + ABS_BRAKE   (single stick split into gas and brake axes)
  ch3  → BTN_TRIGGER            (button)
  ch4  → BTN_THUMB              (button)
  ch5  → ABS_X                  (auxiliary axis)
  ch6  → ABS_Y                  (auxiliary axis)
  ch7  → BTN_THUMB2 / BTN_TOP  (3-position slider mapped to two buttons)
  ch8  → BTN_TOP2               (button)
  ch9  → BTN_PINKIE             (button)
  ch10 → BTN_BASE               (button)
"""

import argparse
import fcntl
import os
import signal
import struct
import subprocess
import sys
import time


# ── Linux input subsystem constants ──────────────────────────────────────────

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
ABS_Y  = 1   # ch2 – throttle  (joystick axis 1, center=idle, +forward, -back)
ABS_RX = 3   # ch5 – aux axis  (joystick axis 2)
ABS_RY = 4   # ch6 – aux axis  (joystick axis 3)

# Button codes – joystick range (0x120–0x12f); joydev driver requires at least
# one code in this range to create /dev/input/js*
BTN_TRIGGER = 0x120   # ch3
BTN_THUMB   = 0x121   # ch4
BTN_THUMB2  = 0x122   # ch7 low position
BTN_TOP     = 0x123   # ch7 high position
BTN_TOP2    = 0x124   # ch8
BTN_PINKIE  = 0x125   # ch9
BTN_BASE    = 0x126   # ch10

# uinput ioctl numbers – _IOW('U', nr, sizeof(int)) on x86-64
UI_SET_EVBIT   = 0x40045564   # nr=100  enable an event type
UI_SET_KEYBIT  = 0x40045565   # nr=101  enable a key/button code
UI_SET_ABSBIT  = 0x40045567   # nr=103  enable an abs axis code
UI_DEV_CREATE  = 0x5501       # _IO('U', 1)  create the device
UI_DEV_DESTROY = 0x5502       # _IO('U', 2)  destroy the device

UINPUT_MAX_NAME_SIZE = 80
ABS_CNT = 64   # total number of ABS axis slots in uinput_user_dev

# input_event layout on 64-bit Linux:
#   struct timeval  (tv_sec int64 + tv_usec int64)  = 16 bytes
#   type  uint16 + code uint16 + value int32        =  8 bytes
INPUT_EVENT_STRUCT = 'qqHHi'   # 24 bytes total


# ── PPM timing constants ──────────────────────────────────────────────────────

AUDIO_SAMPLE_RATE = 48_000   # Hz – negotiated with PipeWire

# All pulse-length thresholds are expressed in samples at AUDIO_SAMPLE_RATE
def _microseconds_to_samples(us):
    return us * AUDIO_SAMPLE_RATE // 1_000_000

AUDIO_THRESHOLD     = 32_700   # int16 level above which the signal is HIGH
                                # (transmitter clips the ADC near ±32767)

SYNC_MIN_SAMPLES = _microseconds_to_samples(3_000)    # >3 ms HIGH → sync pulse
SYNC_MAX_SAMPLES = _microseconds_to_samples(50_000)   # sanity ceiling

# Widest window we accept as a valid channel pulse (wider than AXIS range so
# that edge values are clamped rather than silently dropped)
CHANNEL_MIN_SAMPLES = _microseconds_to_samples(500)    # minimum HIGH pulse to accept as a channel
CHANNEL_MAX_SAMPLES = _microseconds_to_samples(2_100)  # maximum HIGH pulse (below sync threshold)


# ── Axis / button calibration ─────────────────────────────────────────────────

AXIS_MIN_US = 1_100   # µs value reported at one extreme of an axis
AXIS_MAX_US = 1_900   # µs value reported at the other extreme
AXIS_CENTER_US = (AXIS_MIN_US + AXIS_MAX_US) // 2   # 1500 µs

# Suppress axis events smaller than this to remove quantisation noise.
# At 48 kHz one sample ≈ 21 µs; two samples ≈ 42 µs.
AXIS_DEADBAND_US = 42

# Button threshold: channel value above this → button pressed.
# Using the centre point so any deliberate switch position registers.
BUTTON_THRESHOLD_US = AXIS_CENTER_US   # 1500 µs

# Three-position slider zones (ch7).
# Below LOW_THRESH  → low position  (BTN_THUMB2 pressed)
# Above HIGH_THRESH → high position (BTN_TOP pressed)
# Between           → centre        (neither pressed)
SLIDER_LOW_THRESHOLD  = 1_300   # µs
SLIDER_HIGH_THRESHOLD = 1_700   # µs


def samples_to_microseconds(samples):
    return samples * 1_000_000 // AUDIO_SAMPLE_RATE


# ── Channel map ───────────────────────────────────────────────────────────────
#
# Each entry is a tuple whose first element is the channel type.  The
# remaining elements depend on the type:
#
#   ('axis',         abs_code)
#   ('gas_brake',    gas_abs_code, brake_abs_code)
#   ('button',       btn_code)
#   ('three_pos',    low_btn_code, high_btn_code)

CHANNEL_MAP = [
    # ch1 – main steering stick (left/right) → first joystick axis
    ('axis',   ABS_X),

    # ch2 – throttle/brake stick → second joystick axis.
    #        Centre (1500 µs) = idle, forward = positive, back = negative.
    ('axis',   ABS_Y),

    # ch3 – momentary switch → button
    ('button', BTN_TRIGGER),

    # ch4 – momentary switch → button
    ('button', BTN_THUMB),

    # ch5 – auxiliary proportional channel
    ('axis',   ABS_RX),

    # ch6 – auxiliary proportional channel
    ('axis',   ABS_RY),

    # ch7 – three-position slider:
    #   low  position → BTN_THUMB2 pressed
    #   mid  position → neither pressed
    #   high position → BTN_TOP pressed
    ('three_pos', BTN_THUMB2, BTN_TOP),

    # ch8 – momentary switch → button
    ('button',    BTN_TOP2),
    # ch9 and ch10 are not present in this transmitter's PPM signal (8-channel output)
]

# Derived sets of all axis and button codes declared in the channel map
_ALL_ABS_CODES = set()
_ALL_BTN_CODES = set()
for channel_def in CHANNEL_MAP:
    channel_type = channel_def[0]
    if channel_type == 'axis':
        _ALL_ABS_CODES.add(channel_def[1])
    elif channel_type == 'gas_brake':
        _ALL_ABS_CODES.add(channel_def[1])   # gas
        _ALL_ABS_CODES.add(channel_def[2])   # brake
    elif channel_type == 'button':
        _ALL_BTN_CODES.add(channel_def[1])
    elif channel_type == 'three_pos':
        _ALL_BTN_CODES.add(channel_def[1])   # low button
        _ALL_BTN_CODES.add(channel_def[2])   # high button


# ── PPM frame decoder ─────────────────────────────────────────────────────────

class PpmDecoder:
    """
    Stateful decoder that consumes raw int16 audio samples and emits
    complete PPM frames.

    Call feed(sample) for every audio sample.  It returns a list of channel
    values in microseconds when a complete frame is recognised, or None
    while still accumulating.

    PPM frame structure (this transmitter):
      [SYNC HIGH ≥3 ms] [HIGH ch1] [LOW sep] [HIGH ch2] [LOW sep] … [HIGH chN] [LOW sep]
      Channel value = HIGH_pulse_duration + LOW_separator_duration (in µs)
    """

    def __init__(self, max_channels=10, debug=False):
        self.max_channels = max_channels
        self._debug          = debug
        self._current_level  = None    # 'high' or 'low'
        self._run_length     = 0       # consecutive samples at current level
        self._synced         = False   # True after first sync pulse seen
        self._pending_high   = None    # sample count of last HIGH pulse (awaiting LOW)
        self._frame_channels = []      # channel values accumulating for this frame
        self._frame_count    = 0       # total frames dispatched (debug labelling)
        # Debug state – accumulated during the current frame, rendered on sync
        self._dbg_items      = []      # list of (ch_n, h_smp, l_smp, total_us, clamped_us)
        self._dbg_h_pending  = None    # (ch_n, h_smp) waiting for LOW
        self._dbg_skips      = 0       # non-channel HIGH pulses skipped this frame
        self._dbg_n_lines    = 0       # lines printed last render (for cursor-up)
        self._dbg_last_time  = None    # wall time of last sync, for gap detection

    def feed(self, sample):
        """
        Process one int16 audio sample.
        Returns a list of µs values when a frame completes, else None.
        """
        new_level = 'high' if sample > AUDIO_THRESHOLD else 'low'

        if new_level == self._current_level:
            self._run_length += 1
            return None

        # Level transition: the pulse that just ended has length _run_length
        completed_level  = self._current_level
        completed_length = self._run_length
        self._current_level = new_level
        self._run_length    = 1

        if completed_level is None:
            return None   # very first sample – no pulse to evaluate yet

        return self._process_completed_pulse(completed_level, completed_length)

    def _process_completed_pulse(self, pulse_type, pulse_length_samples):
        """Evaluate a just-completed pulse and update decoder state."""

        if pulse_type == 'high':
            if SYNC_MIN_SAMPLES <= pulse_length_samples <= SYNC_MAX_SAMPLES:
                # Sync pulse: render debug display, then dispatch the accumulated frame
                if self._debug:
                    self._debug_render(pulse_length_samples)
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
                  and CHANNEL_MIN_SAMPLES <= pulse_length_samples <= CHANNEL_MAX_SAMPLES):
                # Valid channel HIGH pulse – wait for the LOW separator
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
            # LOW separator following a HIGH channel pulse
            if self._pending_high is not None and self._synced:
                total_samples = self._pending_high + pulse_length_samples
                total_us      = samples_to_microseconds(total_samples)

                if CHANNEL_MIN_SAMPLES <= total_samples <= CHANNEL_MAX_SAMPLES:
                    clamped_us = max(AXIS_MIN_US, min(AXIS_MAX_US, total_us))
                    if self._debug and self._dbg_h_pending:
                        ch_n, h_smp = self._dbg_h_pending
                        self._dbg_items.append(
                            (ch_n, h_smp, pulse_length_samples, total_us, clamped_us)
                        )
                        self._dbg_h_pending = None
                    if len(self._frame_channels) < self.max_channels:
                        self._frame_channels.append(clamped_us)
                else:
                    if self._debug:
                        self._dbg_skips    += 1
                        self._dbg_h_pending = None

                self._pending_high = None

        return None

    def _debug_render(self, sync_smp):
        """
        Render one fixed-height block of debug info to stderr, overwriting the
        previous block in place so the display never scrolls.

        Layout (max_channels + 2 lines):
          line 0:  frame summary (number, sync pulse, channel count, decoded values,
                   frame interval / signal-gap warning)
          lines 1…max_channels:  one line per channel slot (decoded or blank)
          last line:  skipped-pulse count
        """
        FIXED = self.max_channels + 2
        sync_us = samples_to_microseconds(sync_smp)

        # Measure wall-clock gap since last sync for signal-quality annotation
        now = time.monotonic()
        if self._dbg_last_time is not None:
            gap_s = now - self._dbg_last_time
            if gap_s > 0.2:   # >200 ms between frames = connection problem
                timing_note = f'  *** GAP {gap_s:.2f}s ***'
            else:
                timing_note = f'  {1/gap_s:4.0f}Hz'
        else:
            timing_note = ''
        self._dbg_last_time = now

        vals = '  '.join(str(v) for v in self._frame_channels)
        lines = [
            f'frame {self._frame_count:4d}  '
            f'sync {sync_smp:4d}smp={sync_us:5d}µs  '
            f'{len(self._frame_channels):2d}ch  [{vals}]{timing_note}'
        ]

        for ch_n, h_smp, l_smp, total_us, clamped_us in self._dbg_items:
            h_us = samples_to_microseconds(h_smp)
            l_us = samples_to_microseconds(l_smp)
            clamp = f'  →clamped {clamped_us}' if clamped_us != total_us else ''
            lines.append(
                f'  ch{ch_n:2d}  '
                f'H {h_smp:4d}smp={h_us:5d}µs '
                f'+ L {l_smp:4d}smp={l_us:5d}µs '
                f'= {total_us:5d}µs{clamp}'
            )

        # Pad channel section to a fixed height so every render is the same size
        while len(lines) < FIXED - 1:
            lines.append('')

        lines.append(f'  skipped: {self._dbg_skips}')

        # Move cursor to start of previous render, then overwrite line by line
        if self._dbg_n_lines:
            sys.stderr.write(f'\033[{self._dbg_n_lines}F')
        for line in lines:
            sys.stderr.write(f'\r{line:<79}\033[K\n')
        sys.stderr.flush()
        self._dbg_n_lines = FIXED


# ── uinput device management ──────────────────────────────────────────────────

def open_uinput_joystick():
    """
    Create a virtual joystick via /dev/uinput.

    Registers every axis and button code declared in CHANNEL_MAP, writes
    the uinput_user_dev configuration struct, and calls UI_DEV_CREATE.
    Returns the open file descriptor.
    """
    fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)

    # Register all button codes (EV_KEY must be enabled before UI_SET_KEYBIT)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    for btn_code in sorted(_ALL_BTN_CODES):
        fcntl.ioctl(fd, UI_SET_KEYBIT, btn_code)

    # Register all axis codes (EV_ABS must be enabled before UI_SET_ABSBIT)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
    for abs_code in sorted(_ALL_ABS_CODES):
        fcntl.ioctl(fd, UI_SET_ABSBIT, abs_code)

    # Build the uinput_user_dev configuration arrays.
    # Each array has ABS_CNT=64 slots indexed by axis code.
    absmax  = [0] * ABS_CNT
    absmin  = [0] * ABS_CNT
    absfuzz = [0] * ABS_CNT   # kernel-side deadband (suppresses micro-jitter)
    absflat = [0] * ABS_CNT   # kernel-side flat zone around centre

    for abs_code in _ALL_ABS_CODES:
        absmax[abs_code]  = AXIS_MAX_US
        absmin[abs_code]  = AXIS_MIN_US
        absfuzz[abs_code] = AXIS_DEADBAND_US
        absflat[abs_code] = 8   # ±8 µs flat zone at centre (cosmetic)

    device_name = b'ppm2joy\x00'.ljust(UINPUT_MAX_NAME_SIZE, b'\x00')

    uinput_user_dev = struct.pack(
        # Format: name(80s) bustype vendor product version ff_effects_max
        #         absmax[64] absmin[64] absfuzz[64] absflat[64]
        f'{UINPUT_MAX_NAME_SIZE}s HHHH I {ABS_CNT}i {ABS_CNT}i {ABS_CNT}i {ABS_CNT}i',
        device_name,
        BUS_USB, 0x1209, 0x2641, 1,   # vendor/product IDs for ppm2joy
        0,                             # ff_effects_max (no force-feedback)
        *absmax, *absmin, *absfuzz, *absflat,
    )
    os.write(fd, uinput_user_dev)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    return fd


def destroy_uinput_joystick(fd):
    """Destroy the virtual joystick and close the uinput file descriptor."""
    try:
        fcntl.ioctl(fd, UI_DEV_DESTROY)
    except OSError:
        pass
    os.close(fd)


def _write_input_event(fd, event_type, event_code, event_value):
    """Write a single struct input_event to the uinput file descriptor."""
    raw = struct.pack(INPUT_EVENT_STRUCT, 0, 0, event_type, event_code, event_value)
    os.write(fd, raw)


def _flush_events(fd):
    """Send EV_SYN/SYN_REPORT so the kernel delivers the queued events."""
    _write_input_event(fd, EV_SYN, SYN_REPORT, 0)


# ── Channel output state and event emission ───────────────────────────────────

class ChannelOutputState:
    """
    Tracks the last-emitted value for every channel so we can apply the
    software deadband and avoid redundant button events.

    axis_values    – dict {abs_code: last_emitted_µs}
    button_states  – dict {btn_code: True/False (pressed)}
    """

    def __init__(self):
        # Initialise axes at centre; buttons as released
        self.axis_values   = {code: AXIS_CENTER_US for code in _ALL_ABS_CODES}
        self.button_states = {code: False           for code in _ALL_BTN_CODES}


def emit_channel_events(fd, state, ppm_frame):
    """
    Convert a decoded PPM frame into uinput events.

    Iterates over CHANNEL_MAP, processes each channel value according to
    its type, and writes EV_ABS / EV_KEY events only when the value has
    changed enough to warrant an update.  Ends with EV_SYN.
    """
    for channel_index, channel_def in enumerate(CHANNEL_MAP):
        if channel_index >= len(ppm_frame):
            break   # transmitter sent fewer channels than mapped

        raw_us       = ppm_frame[channel_index]
        channel_type = channel_def[0]

        if channel_type == 'axis':
            abs_code = channel_def[1]
            if abs(raw_us - state.axis_values[abs_code]) >= AXIS_DEADBAND_US:
                state.axis_values[abs_code] = raw_us
                _write_input_event(fd, EV_ABS, abs_code, raw_us)

        elif channel_type == 'button':
            btn_code = channel_def[1]
            pressed  = raw_us > BUTTON_THRESHOLD_US
            if pressed != state.button_states[btn_code]:
                state.button_states[btn_code] = pressed
                _write_input_event(fd, EV_KEY, btn_code, int(pressed))

        elif channel_type == 'three_pos':
            low_btn_code, high_btn_code = channel_def[1], channel_def[2]
            # Determine which of three zones the slider is in
            if raw_us < SLIDER_LOW_THRESHOLD:
                low_pressed, high_pressed = True, False
            elif raw_us > SLIDER_HIGH_THRESHOLD:
                low_pressed, high_pressed = False, True
            else:
                low_pressed, high_pressed = False, False

            for btn_code, pressed in ((low_btn_code, low_pressed),
                                      (high_btn_code, high_pressed)):
                if pressed != state.button_states[btn_code]:
                    state.button_states[btn_code] = pressed
                    _write_input_event(fd, EV_KEY, btn_code, int(pressed))

    # Always send EV_SYN to mark the end of this frame's event batch.
    # Sending unconditionally (even when nothing changed) ensures the kernel
    # delivers pending events and keeps button state in sync with readers.
    _flush_events(fd)


# ── ALSA mixer helpers ────────────────────────────────────────────────────────

_saved_input_sources = {}   # {channel_index: original_source_name}

def _amixer_cset(alsa_card, control_name, value):
    subprocess.run(
        ['amixer', '-c', str(alsa_card), 'cset', f'name={control_name}', value],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

def _amixer_cget(alsa_card, control_name):
    result = subprocess.run(
        ['amixer', '-c', str(alsa_card), 'cget', f'name={control_name}'],
        capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith('item0=') or line.startswith(': values='):
            return line.split('=', 1)[1].strip().strip("'")
    return None

def switch_alsa_input_to_line_in(alsa_card=0):
    """Save current ALSA input sources, then switch both capture channels to Line In."""
    for channel_index in range(2):
        original = _amixer_cget(alsa_card, f'Input Source,{channel_index}')
        if original:
            _saved_input_sources[channel_index] = original
        _amixer_cset(alsa_card, f'Input Source,{channel_index}', 'Line')

def restore_alsa_input_sources(alsa_card=0):
    """Restore ALSA input sources to the values saved before we changed them."""
    fallback_defaults = {0: 'Rear Mic', 1: 'Front Mic'}
    for channel_index in range(2):
        source = _saved_input_sources.get(channel_index,
                                          fallback_defaults[channel_index])
        _amixer_cset(alsa_card, f'Input Source,{channel_index}', source)


# ── Audio capture via PipeWire/PulseAudio ─────────────────────────────────────

def start_audio_capture(pipewire_source_name):
    """
    Launch parecord and return the Popen handle.
    Output: raw signed-16-bit little-endian stereo at AUDIO_SAMPLE_RATE Hz.
    """
    return subprocess.Popen(
        [
            'parecord',
            f'--device={pipewire_source_name}',
            '--format=s16le',
            f'--rate={AUDIO_SAMPLE_RATE}',
            '--channels=2',
            '--raw',
            '--latency-msec=20',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


# ── Monitor display ───────────────────────────────────────────────────────────

def _axis_bar(value_us, width=6):
    """Fixed-width ASCII progress bar for an axis value in µs."""
    fraction = (value_us - AXIS_MIN_US) / (AXIS_MAX_US - AXIS_MIN_US)
    filled   = int(max(0.0, min(1.0, fraction)) * width)
    return '[' + '█' * filled + '░' * (width - filled) + ']'

# Short labels for each channel index (used in monitor line)
_MONITOR_LABELS = ['STR', 'THR', ' c3', ' c4', ' RX', ' RY', ' c7', ' c8', ' c9', 'c10']

def print_monitor_line(ppm_frame, gap_s=0.0):
    """
    Print a fixed-width one-line summary of all decoded controls.
    Appends a signal-quality tag: frame rate when healthy, or a gap warning
    when no frame has been received for more than 200 ms.

    Layout example:
      STR:[██████] THR:[██████]  c3:■  c4:□  RX:[------]  RY:[------]  c7:MID  c8:□  [70Hz]
    """
    parts = []
    for channel_index, channel_def in enumerate(CHANNEL_MAP):
        label        = _MONITOR_LABELS[channel_index]
        channel_type = channel_def[0]

        if channel_index >= len(ppm_frame):
            # Channel not present in this frame
            if channel_type == 'axis':
                parts.append(f'{label}:[------]')
            elif channel_type == 'three_pos':
                parts.append(f'{label}: -- ')
            else:
                parts.append(f'{label}:?')
            continue

        raw_us = ppm_frame[channel_index]

        if channel_type == 'axis':
            # e.g. "STR:[██████]"  — 10 chars
            parts.append(f'{label}:{_axis_bar(raw_us)}')

        elif channel_type == 'button':
            # "c3:■" or "c3:□"  — 4 chars
            pressed = raw_us > BUTTON_THRESHOLD_US
            parts.append(f'{label}:{"■" if pressed else "□"}')

        elif channel_type == 'three_pos':
            # Fixed 3-char position label  — "c7:LO " = 7 chars
            if raw_us < SLIDER_LOW_THRESHOLD:
                pos = 'LO '
            elif raw_us > SLIDER_HIGH_THRESHOLD:
                pos = 'HI '
            else:
                pos = 'MID'
            parts.append(f'{label}:{pos}')

    # Signal quality tag appended after channel data
    if gap_s > 0.2:
        tag = f'  [*** GAP {gap_s:.1f}s ***]'
    elif gap_s > 0:
        tag = f'  [{1/gap_s:.0f}Hz]'
    else:
        tag = ''

    # \r overwrites the current line; \033[K clears to end of line
    print(f'\r{" ".join(parts)}{tag}\033[K', end='', flush=True)


# ── Entry point ───────────────────────────────────────────────────────────────

DEFAULT_PIPEWIRE_SOURCE = 'alsa_input.pci-0000_00_1f.3.analog-stereo'
# How many PPM channels the decoder buffers per frame.  Higher than len(CHANNEL_MAP)
# so that extra channels (e.g. ch9/ch10 if the transmitter sends them) show up in
# --debug and --monitor output even though they are not yet mapped to joystick events.
PPM_DECODE_MAX_CHANNELS = 12

def main():
    argument_parser = argparse.ArgumentParser(
        description='PPM RC transmitter audio input → Linux virtual joystick'
    )
    argument_parser.add_argument(
        '-d', '--device', default=DEFAULT_PIPEWIRE_SOURCE,
        help='PipeWire/PulseAudio source device name',
    )
    argument_parser.add_argument(
        '-m', '--monitor', action='store_true',
        help='Print live channel values to stdout',
    )
    argument_parser.add_argument(
        '--no-mixer', action='store_true',
        help="Don't modify the ALSA Input Source mixer control",
    )
    argument_parser.add_argument(
        '--debug', action='store_true',
        help='Print raw pulse decisions to stderr (sync, channel, skip) for signal analysis',
    )
    args = argument_parser.parse_args()

    if not os.path.exists('/dev/uinput'):
        sys.exit('error: /dev/uinput not found – is the uinput kernel module loaded?')

    # Switch the onboard audio input mux to the Line In jack
    if not args.no_mixer:
        print('Switching ALSA Input Source → Line In …')
        switch_alsa_input_to_line_in()

    # Create the virtual joystick device
    print('Creating virtual joystick … ', end='', flush=True)
    try:
        uinput_fd = open_uinput_joystick()
    except PermissionError:
        sys.exit('error: cannot open /dev/uinput – check ACL or group membership')
    print('ok')

    # Register shutdown handler so we always clean up on SIGINT/SIGTERM
    audio_capture_proc = None

    def shutdown(signum=None, frame=None):
        print('\nShutting down …')
        if audio_capture_proc:
            audio_capture_proc.terminate()
        destroy_uinput_joystick(uinput_fd)
        if not args.no_mixer:
            print('Restoring ALSA Input Source …')
            restore_alsa_input_sources()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start audio capture subprocess
    print(f'Capturing from: {args.device}')
    audio_capture_proc = start_audio_capture(args.device)

    ppm_decoder    = PpmDecoder(max_channels=PPM_DECODE_MAX_CHANNELS, debug=args.debug)
    output_state   = ChannelOutputState()
    frames_decoded = 0
    last_frame_time = None   # wall time of the most recently decoded frame

    print('Waiting for PPM signal … (Ctrl-C to quit)')

    # Read audio in chunks of 1024 stereo frames = 4096 bytes
    AUDIO_CHUNK_BYTES = 1024 * 4

    try:
        while True:
            raw_audio = audio_capture_proc.stdout.read(AUDIO_CHUNK_BYTES)
            if not raw_audio:
                print('\nAudio capture ended unexpectedly')
                break

            # Each stereo frame is 4 bytes: [left_lo, left_hi, right_lo, right_hi]
            # We only use the left channel (bytes 0–1 of each frame)
            for byte_offset in range(0, len(raw_audio) - 3, 4):
                left_sample = struct.unpack_from('<h', raw_audio, byte_offset)[0]
                completed_frame = ppm_decoder.feed(left_sample)

                if completed_frame is None:
                    continue

                now = time.monotonic()
                gap_s = (now - last_frame_time) if last_frame_time is not None else 0.0
                last_frame_time = now

                frames_decoded += 1
                if frames_decoded == 1:
                    print(f'\nPPM signal detected — {len(completed_frame)} channels')
                    if args.monitor:
                        print()

                emit_channel_events(uinput_fd, output_state, completed_frame)

                if args.monitor:
                    print_monitor_line(completed_frame, gap_s)

    except KeyboardInterrupt:
        pass

    shutdown()


if __name__ == '__main__':
    main()
