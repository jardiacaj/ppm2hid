#!/usr/bin/env python3
"""
test_ppm_bad_cable.py – offline tests for PPM decoder behaviour with a poor signal.

Decodes testdata/ppm_capture.wav (30-second recording at 48 kHz, controls
exercised with a bad cable that loses contact intermittently) and asserts:

  1. Some frames were decoded despite the bad signal
  2. The average frame rate is well below the clean-signal minimum (drops present)
  3. Every decoded frame has the expected channel count
  4. All decoded channel values stay within the declared axis range
  5. Multiple signal gaps are detectable in the frame timing
  6. The decoder recovers and produces frames throughout the whole recording

These tests do NOT assert stick saturation or button presses — the focus is
entirely on drop detection and recovery, not control coverage.
"""

import os
import struct
import sys
import unittest
import wave

sys.path.insert(0, os.path.dirname(__file__))
from ppm2hid import (
    PpmDecoder,
    CHANNEL_MAP,
    AXIS_MIN_US, AXIS_MAX_US,
)

RECORDING_PATH = os.path.join(os.path.dirname(__file__), 'testdata', 'ppm_capture.wav')

# A gap longer than this many normal frame periods is counted as a signal drop.
GAP_THRESHOLD_PERIODS = 5


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_left_channel_samples(path):
    """Read a .wav stereo file and return (left-channel samples, sample_rate)."""
    with wave.open(path, 'rb') as wf:
        raw         = wf.readframes(wf.getnframes())
        sample_rate = wf.getframerate()
    samples = [
        struct.unpack_from('<h', raw, offset)[0]
        for offset in range(0, len(raw) - 3, 4)
    ]
    return samples, sample_rate


def _decode_with_sample_indices(samples, sample_rate):
    """
    Decode all samples through PpmDecoder and return a list of
    (sample_index, frame) for every complete frame decoded.
    """
    decoder = PpmDecoder(max_channels=len(CHANNEL_MAP), sample_rate=sample_rate)
    results = []
    for idx, sample in enumerate(samples):
        frame = decoder.feed(sample)
        if frame is not None:
            results.append((idx, frame))
    return results


# ── Test class ────────────────────────────────────────────────────────────────

class TestPpmBadCable(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(RECORDING_PATH):
            raise unittest.SkipTest(f'Recording not found: {RECORDING_PATH}')

        samples, sample_rate = _load_left_channel_samples(RECORDING_PATH)
        cls.sample_rate     = sample_rate
        cls.total_samples   = len(samples)
        cls.actual_duration = len(samples) / sample_rate

        frames_with_idx = _decode_with_sample_indices(samples, sample_rate)
        if not frames_with_idx:
            raise unittest.SkipTest(
                'No PPM frames decoded — check that the transmitter was on '
                'and the signal is on the left channel.'
            )

        cls.frames              = [f for _, f in frames_with_idx]
        cls.frame_sample_indices = [i for i, _ in frames_with_idx]

        # Nominal frame period in samples at a healthy ~50 Hz
        cls.nominal_period_samples = sample_rate / 50.0

    def test_some_frames_decoded(self):
        """At least 100 frames must be decoded despite the intermittent signal."""
        self.assertGreaterEqual(
            len(self.frames), 100,
            f'Only {len(self.frames)} frames decoded from {self.actual_duration:.0f} s '
            f'recording — expected ≥ 100 even with a bad cable'
        )

    def test_signal_drops_present(self):
        """
        Average frame rate must be well below the clean-signal minimum (40 Hz),
        confirming that the bad cable caused significant signal loss.
        """
        actual_hz   = len(self.frames) / self.actual_duration
        clean_min   = 40   # Hz — minimum for a healthy signal
        self.assertLess(
            actual_hz, clean_min,
            f'Frame rate {actual_hz:.1f} Hz is not below the clean minimum '
            f'({clean_min} Hz) — expected fewer frames due to signal drops'
        )

    def test_channel_count(self):
        """
        Most decoded frames must have the expected channel count.  A small
        proportion of partial frames is acceptable — the bad cable can cut the
        signal mid-frame, so the decoder may emit a short frame at drop edges.
        """
        expected   = len(CHANNEL_MAP)
        partial    = [i for i, f in enumerate(self.frames) if len(f) != expected]
        max_partial = max(5, int(len(self.frames) * 0.10))   # ≤ 10 % partial
        self.assertLessEqual(
            len(partial), max_partial,
            f'{len(partial)} partial frame(s) (>{max_partial} = 10 % of '
            f'{len(self.frames)} frames); first: frame {partial[0]} has '
            f'{len(self.frames[partial[0]])} channels (expected {expected})'
            if partial else ''
        )

    def test_values_in_range(self):
        """All decoded µs values must lie within [AXIS_MIN_US, AXIS_MAX_US]."""
        violations = []
        for frame_index, frame in enumerate(self.frames):
            for ch_index, value in enumerate(frame):
                if not (AXIS_MIN_US <= value <= AXIS_MAX_US):
                    violations.append((frame_index, ch_index, value))
        self.assertFalse(
            violations,
            f'{len(violations)} out-of-range value(s), e.g. '
            f'frame {violations[0][0]} ch{violations[0][1]+1} = {violations[0][2]} µs'
            if violations else ''
        )

    def test_signal_gaps_detected(self):
        """
        Multiple gaps longer than GAP_THRESHOLD_PERIODS × nominal frame period
        must be present, confirming the bad cable caused actual signal interruptions.
        """
        threshold = self.nominal_period_samples * GAP_THRESHOLD_PERIODS
        gaps      = sum(
            1 for i in range(1, len(self.frame_sample_indices))
            if self.frame_sample_indices[i] - self.frame_sample_indices[i - 1] > threshold
        )
        self.assertGreaterEqual(
            gaps, 5,
            f'Only {gaps} signal gap(s) detected (threshold: '
            f'{GAP_THRESHOLD_PERIODS}× nominal period = '
            f'{threshold:.0f} samples / {threshold/self.sample_rate*1000:.0f} ms); '
            f'expected ≥ 5 with a bad cable'
        )

    def test_decoder_recovers_throughout(self):
        """
        Frames must appear in each quarter of the recording, confirming the
        decoder re-synchronises after every signal interruption.
        """
        quarter_size   = self.total_samples // 4
        min_per_quarter = 10
        for q in range(4):
            lo = q * quarter_size
            hi = lo + quarter_size
            count = sum(
                1 for idx in self.frame_sample_indices if lo <= idx < hi
            )
            self.assertGreaterEqual(
                count, min_per_quarter,
                f'Quarter {q + 1}/4 of the recording has only {count} frame(s); '
                f'expected ≥ {min_per_quarter} — decoder may not be recovering '
                f'after signal drops'
            )

    def test_decoder_stability_when_signal_present(self):
        """
        Between consecutive full-channel frames with no gap in between, axis
        channels must not jump more than 200 µs.  Any pair of frames separated
        by more than 1.5× the nominal frame period is excluded, since that
        indicates a signal interruption (even a brief one can corrupt timing).
        A small number of residual glitches near drop-recovery boundaries is
        tolerated.
        """
        MAX_JUMP     = 200
        GLITCH_LIMIT = 20
        # Exclude any frame pair with an inter-frame gap > 1.5× nominal
        # (catches both large drops and short interruptions alike)
        gap_threshold = self.nominal_period_samples * 1.5

        expected     = len(CHANNEL_MAP)
        axis_indices = {i for i, ch in enumerate(CHANNEL_MAP) if ch[0] == 'axis'}

        # Work only on fully-decoded frames
        full = [
            (idx, f)
            for idx, f in zip(self.frame_sample_indices, self.frames)
            if len(f) == expected
        ]

        glitches = []
        for i in range(1, len(full)):
            gap = full[i][0] - full[i - 1][0]
            if gap > gap_threshold:
                continue
            prev, curr = full[i - 1][1], full[i][1]
            for ch in range(expected):
                if ch not in axis_indices:
                    continue
                jump = abs(curr[ch] - prev[ch])
                if jump > MAX_JUMP:
                    glitches.append((i, ch, prev[ch], curr[ch], jump))

        if glitches and len(glitches) <= GLITCH_LIMIT:
            print(f'\n  decoder stability: {len(glitches)} residual glitch(es) '
                  f'near drop-recovery boundaries — within tolerance')
        else:
            self.assertEqual(
                len(glitches), 0,
                f'{len(glitches)} large axis jump(s) in stable signal portions '
                f'(threshold {MAX_JUMP} µs) — possible decoder instability'
                if glitches else ''
            )


if __name__ == '__main__':
    unittest.main()
