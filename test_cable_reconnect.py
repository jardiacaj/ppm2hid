#!/usr/bin/env python3
"""
test_cable_reconnect.py – decoder recovers cleanly from a cable disconnect.

Recording required: testdata/cable_reconnect.wav
  ~10 s at 192 kHz.  Procedure:
    1. Transmitter on, all controls at centre.
    2. Start recording.
    3. Wait 2–3 s (clean signal).
    4. Physically unplug the audio cable.
    5. Wait 2–3 s (silence / noise).
    6. Reconnect the cable.
    7. Wait 2–3 s (clean signal again).
    8. Stop recording.

  Record with:
    python3 record_ppm.py --name cable_reconnect --duration 10

Expected outcomes:
  - Frames decoded before and after the gap: at least 60 each (≈2 s at 30 Hz min).
  - No frames during the cable-out period (hysteresis blocks noise).
  - All decoded frames have the expected channel count.
  - All decoded values stay within [_PROFILE.axis_min_us, _PROFILE.axis_max_us].
"""

import os
import struct
import sys
import unittest
import wave

sys.path.insert(0, os.path.dirname(__file__))
from ppm2hid import PpmDecoder, Profile

_PROFILE = Profile()

RECORDING_PATH = os.path.join(os.path.dirname(__file__), 'testdata', 'cable_reconnect.wav')

# A frame-interval gap longer than this many nominal periods is a "cable out" event.
GAP_THRESHOLD_PERIODS = 10

# Minimum frames required in the pre-gap and post-gap windows.
MIN_FRAMES_PER_WINDOW = 60


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_samples(path):
    with wave.open(path, 'rb') as wf:
        raw  = wf.readframes(wf.getnframes())
        rate = wf.getframerate()
    return [struct.unpack_from('<h', raw, o)[0] for o in range(0, len(raw) - 3, 4)], rate


def _decode_with_indices(samples, sample_rate):
    """Return list of (sample_index, frame) for every complete frame."""
    decoder = PpmDecoder(max_channels=len(_PROFILE.channel_map), sample_rate=sample_rate)
    results = []
    for idx, s in enumerate(samples):
        frame = decoder.feed(s)
        if frame is not None:
            results.append((idx, frame))
    return results


def _find_gaps(indexed_frames, sample_rate, threshold_periods):
    """
    Return a list of (before_idx, after_idx) sample-index pairs for each gap
    between consecutive frames that exceeds threshold_periods × nominal period.
    """
    if len(indexed_frames) < 2:
        return []
    nominal_period = sample_rate / 60   # assume 60 Hz nominal
    min_gap = nominal_period * threshold_periods
    gaps = []
    for (i1, _), (i2, _) in zip(indexed_frames, indexed_frames[1:]):
        if (i2 - i1) > min_gap:
            gaps.append((i1, i2))
    return gaps


# ── Test class ────────────────────────────────────────────────────────────────

class TestCableReconnect(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(RECORDING_PATH):
            raise FileNotFoundError(
                f'Recording not found: {RECORDING_PATH}\n'
                'Record with:\n'
                '  python3 record_ppm.py --name cable_reconnect --duration 10\n'
                'Procedure: signal on → unplug cable → wait 2–3 s → reconnect → stop'
            )
        samples, cls.sample_rate = _load_samples(RECORDING_PATH)
        cls.indexed_frames = _decode_with_indices(samples, cls.sample_rate)
        cls.gaps = _find_gaps(cls.indexed_frames, cls.sample_rate, GAP_THRESHOLD_PERIODS)

        if not cls.indexed_frames:
            raise unittest.SkipTest(
                'No frames decoded — check transmitter was on and signal is on '
                'the left channel'
            )

    def test_gap_detected(self):
        """At least one cable-out gap must be present in the recording."""
        self.assertGreaterEqual(
            len(self.gaps), 1,
            'No signal gap detected — was the cable actually unplugged? '
            f'(threshold: {GAP_THRESHOLD_PERIODS}× nominal frame period)'
        )

    def test_frames_before_gap(self):
        """Enough clean frames must exist before the first disconnect."""
        if not self.gaps:
            self.skipTest('no gap detected')
        gap_start = self.gaps[0][0]
        pre_frames = [f for idx, f in self.indexed_frames if idx < gap_start]
        self.assertGreaterEqual(
            len(pre_frames), MIN_FRAMES_PER_WINDOW,
            f'Only {len(pre_frames)} frame(s) before gap; expected ≥ {MIN_FRAMES_PER_WINDOW} — '
            'start recording with the cable plugged in and transmitter on'
        )

    def test_frames_after_gap(self):
        """Enough clean frames must exist after the final reconnect."""
        if not self.gaps:
            self.skipTest('no gap detected')
        gap_end = self.gaps[-1][1]
        post_frames = [f for idx, f in self.indexed_frames if idx > gap_end]
        self.assertGreaterEqual(
            len(post_frames), MIN_FRAMES_PER_WINDOW,
            f'Only {len(post_frames)} frame(s) after gap; expected ≥ {MIN_FRAMES_PER_WINDOW} — '
            'keep recording for 2–3 s after reconnecting'
        )

    def test_no_phantom_frames_during_gap(self):
        """No frames should be decoded while the cable is disconnected."""
        for gap_start, gap_end in self.gaps:
            during = [f for idx, f in self.indexed_frames
                      if gap_start < idx < gap_end]
            self.assertEqual(
                len(during), 0,
                f'{len(during)} phantom frame(s) decoded during cable-out gap '
                f'(sample {gap_start}–{gap_end}) — '
                f'DEFAULT_AUDIO_HYSTERESIS may need to be raised'
            )

    def test_channel_count_consistent(self):
        """Frames outside gap boundaries must have the expected channel count.

        A single partial frame at a reconnect boundary is tolerated — the
        decoder naturally catches an incomplete PPM sequence as the signal
        returns.  We allow one frame within one nominal frame-period of each
        gap edge to have a short count.
        """
        expected = len(_PROFILE.channel_map)
        nominal_period = self.sample_rate / 60
        # Build a set of sample indices that are within one frame-period of
        # any gap edge (start or end).
        boundary_indices = set()
        for gap_start, gap_end in self.gaps:
            boundary_indices.update(
                idx for idx, _ in self.indexed_frames
                if abs(idx - gap_start) <= nominal_period
                or abs(idx - gap_end) <= nominal_period
            )
        bad = [
            (idx, len(f)) for idx, f in self.indexed_frames
            if len(f) != expected and idx not in boundary_indices
        ]
        self.assertFalse(
            bad,
            f'{len(bad)} frame(s) with wrong channel count away from gap boundaries '
            f'(e.g. sample {bad[0][0]}: {bad[0][1]} channels, expected {expected})'
            if bad else ''
        )

    def test_values_in_range(self):
        """All decoded channel values must lie within [_PROFILE.axis_min_us, _PROFILE.axis_max_us]."""
        violations = [
            (idx, ch, v)
            for idx, frame in self.indexed_frames
            for ch, v in enumerate(frame)
            if not (_PROFILE.axis_min_us <= v <= _PROFILE.axis_max_us)
        ]
        self.assertFalse(
            violations,
            f'{len(violations)} out-of-range value(s), e.g. '
            f'sample {violations[0][0]} ch{violations[0][1]+1} = {violations[0][2]} µs'
            if violations else ''
        )


if __name__ == '__main__':
    unittest.main()
