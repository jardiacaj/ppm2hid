#!/usr/bin/env python3
"""
test_channel_sweeps.py – per-channel coverage tests against short sweep recordings.

Each test class requires one WAV file in testdata/ that covers a single
transmitter channel through all its positions.  Missing recordings are skipped.

Recordings required (all 192 kHz, ~3 s each):

  testdata/ch01_sweep.wav  ch1 steering axis  – full left → right → left sweep
  testdata/ch02_sweep.wav  ch2 throttle axis  – full back → forward → back sweep
  testdata/ch03_sweep.wav  ch3 button         – release → press → release
  testdata/ch04_sweep.wav  ch4 button         – release → press → release
  testdata/ch05_sweep.wav  ch5 aux axis       – full sweep
  testdata/ch06_sweep.wav  ch6 aux axis       – full sweep
  testdata/ch07_sweep.wav  ch7 slider         – LOW → MID → HI → MID → LOW
  testdata/ch08_sweep.wav  ch8 button         – release → press → release

Record each with (transmitter ON, move only the target control):
  python3 record_ppm.py --name ch01_sweep --duration 3
  python3 record_ppm.py --name ch02_sweep --duration 3
  … etc.
"""

import os
import struct
import sys
import unittest
import wave

sys.path.insert(0, os.path.dirname(__file__))
from ppm2hid import PpmDecoder, Profile

_PROFILE = Profile()

_TESTDATA = os.path.join(os.path.dirname(__file__), 'testdata')


# ── Shared helpers ────────────────────────────────────────────────────────────

def _load_samples(path):
    """Return (left-channel int16 samples, sample_rate) from a stereo WAV."""
    with wave.open(path, 'rb') as wf:
        raw  = wf.readframes(wf.getnframes())
        rate = wf.getframerate()
    return [struct.unpack_from('<h', raw, o)[0] for o in range(0, len(raw) - 3, 4)], rate


def _decode_channel(path, ch_index):
    """Decode all frames from *path* and return the values for *ch_index*."""
    samples, rate = _load_samples(path)
    decoder = PpmDecoder(max_channels=len(_PROFILE.channel_map), sample_rate=rate)
    values = []
    for s in samples:
        frame = decoder.feed(s)
        if frame is not None and len(frame) > ch_index:
            values.append(frame[ch_index])
    return values


# ── Assertion helpers ─────────────────────────────────────────────────────────

_AXIS_SATURATION = 0.80   # must reach within this fraction of full range

def _check_axis_full_range(tc, values, label):
    required_span = int((_PROFILE.axis_max_us - _PROFILE.axis_min_us) * _AXIS_SATURATION)
    span = max(values) - min(values)
    lo, hi = min(values), max(values)
    tc.assertGreaterEqual(
        span, required_span,
        f'{label}: spanned only {span} µs (min={lo}, max={hi}); '
        f'expected ≥ {required_span} µs — move the control to full extremes',
    )
    tc.assertLessEqual(
        lo, _PROFILE.axis_min_us + 200,
        f'{label}: never reached low end (minimum observed: {lo} µs)',
    )
    tc.assertGreaterEqual(
        hi, _PROFILE.axis_max_us - 200,
        f'{label}: never reached high end (maximum observed: {hi} µs)',
    )


def _check_button_toggled(tc, values, label):
    pressed  = any(v > _PROFILE.button_threshold_us for v in values)
    released = any(v <= _PROFILE.button_threshold_us for v in values)
    tc.assertTrue(pressed,
                  f'{label}: button never pressed (no value > {_PROFILE.button_threshold_us} µs)')
    tc.assertTrue(released,
                  f'{label}: button never released (no value ≤ {_PROFILE.button_threshold_us} µs)')


def _check_slider_all_positions(tc, values, label):
    saw_lo  = any(v < _PROFILE.slider_low_threshold_us for v in values)
    saw_mid = any(_PROFILE.slider_low_threshold_us <= v <= _PROFILE.slider_high_threshold_us for v in values)
    saw_hi  = any(v > _PROFILE.slider_high_threshold_us for v in values)
    tc.assertTrue(saw_lo,
                  f'{label}: LOW position (< {_PROFILE.slider_low_threshold_us} µs) never seen '
                  f'(range: {min(values)}–{max(values)} µs)')
    tc.assertTrue(saw_mid,
                  f'{label}: MID position ({_PROFILE.slider_low_threshold_us}–{_PROFILE.slider_high_threshold_us} µs) '
                  f'never seen')
    tc.assertTrue(saw_hi,
                  f'{label}: HIGH position (> {_PROFILE.slider_high_threshold_us} µs) never seen')


# ── Mixin base ────────────────────────────────────────────────────────────────

class _SweepMixin:
    """
    Shared setup for per-channel sweep test classes.

    Subclasses must define:
      RECORDING_PATH  – absolute path to the WAV file
      CHANNEL_INDEX   – 0-based index in _PROFILE.channel_map
    """

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(cls.RECORDING_PATH):
            raise FileNotFoundError(
                f'Recording not found: {cls.RECORDING_PATH}\n'
                f'Record with (transmitter ON, move only ch{cls.CHANNEL_INDEX + 1}):\n'
                f'  python3 record_ppm.py --name '
                f'ch{cls.CHANNEL_INDEX + 1:02d}_sweep --duration 3'
            )
        cls.values = _decode_channel(cls.RECORDING_PATH, cls.CHANNEL_INDEX)
        if not cls.values:
            raise unittest.SkipTest(
                f'No frames decoded from {cls.RECORDING_PATH} — '
                'check transmitter is on and signal is on the left channel'
            )


# ── Per-channel test classes ──────────────────────────────────────────────────

class TestCh01SteeringSweep(_SweepMixin, unittest.TestCase):
    """ch1 – steering axis, full left-to-right sweep."""
    RECORDING_PATH = os.path.join(_TESTDATA, 'ch01_sweep.wav')
    CHANNEL_INDEX  = 0

    def test_full_range(self):
        _check_axis_full_range(self, self.values, 'ch1 (steering)')


class TestCh02ThrottleSweep(_SweepMixin, unittest.TestCase):
    """ch2 – throttle axis, full back-to-forward sweep."""
    RECORDING_PATH = os.path.join(_TESTDATA, 'ch02_sweep.wav')
    CHANNEL_INDEX  = 1

    def test_full_range(self):
        _check_axis_full_range(self, self.values, 'ch2 (throttle)')


class TestCh03ButtonSweep(_SweepMixin, unittest.TestCase):
    """ch3 – momentary button, press and release."""
    RECORDING_PATH = os.path.join(_TESTDATA, 'ch03_sweep.wav')
    CHANNEL_INDEX  = 2

    def test_pressed_and_released(self):
        _check_button_toggled(self, self.values, 'ch3')


class TestCh04ButtonSweep(_SweepMixin, unittest.TestCase):
    """ch4 – momentary button, press and release."""
    RECORDING_PATH = os.path.join(_TESTDATA, 'ch04_sweep.wav')
    CHANNEL_INDEX  = 3

    def test_pressed_and_released(self):
        _check_button_toggled(self, self.values, 'ch4')


class TestCh05AuxAxisSweep(_SweepMixin, unittest.TestCase):
    """ch5 – auxiliary axis, full sweep."""
    RECORDING_PATH = os.path.join(_TESTDATA, 'ch05_sweep.wav')
    CHANNEL_INDEX  = 4

    def test_full_range(self):
        _check_axis_full_range(self, self.values, 'ch5 (aux axis)')


class TestCh06AuxAxisSweep(_SweepMixin, unittest.TestCase):
    """ch6 – auxiliary axis, full sweep."""
    RECORDING_PATH = os.path.join(_TESTDATA, 'ch06_sweep.wav')
    CHANNEL_INDEX  = 5

    def test_full_range(self):
        _check_axis_full_range(self, self.values, 'ch6 (aux axis)')


class TestCh07SliderSweep(_SweepMixin, unittest.TestCase):
    """ch7 – three-position slider, LOW → MID → HI → MID → LOW."""
    RECORDING_PATH = os.path.join(_TESTDATA, 'ch07_sweep.wav')
    CHANNEL_INDEX  = 6

    def test_all_positions(self):
        _check_slider_all_positions(self, self.values, 'ch7 (slider)')


class TestCh08ButtonSweep(_SweepMixin, unittest.TestCase):
    """ch8 – momentary button, press and release."""
    RECORDING_PATH = os.path.join(_TESTDATA, 'ch08_sweep.wav')
    CHANNEL_INDEX  = 7

    def test_pressed_and_released(self):
        _check_button_toggled(self, self.values, 'ch8')


if __name__ == '__main__':
    unittest.main()
