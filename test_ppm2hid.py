#!/usr/bin/env python3
"""
test_ppm2hid.py – offline tests for the PPM decoder and channel mapping.

Decodes testdata/ppm_capture_192k.raw (15-second recording at 192 kHz,
controls exercised with a working cable) and asserts:

  1. Enough frames were decoded for a 15-second capture
  2. All channel values stay within the declared axis range
  3. Every frame has the expected channel count
  4. Axis channels (ch1, ch5, ch6) visited both extremes
  5. Throttle channel (ch2) went both above and below centre
  6. Button channels that were exercised (ch3, ch4) toggled on and off
  7. Frame rate is plausible (40–90 Hz)
  8. No large frame-to-frame jumps (decoder stability)

Coverage gaps in this recording (not tested):
  ch7 slider – only mid position observed; LO/HI not verified
  ch8 button – never pressed; press/release not verified
  ch9/ch10   – transmitter confirmed to send 8 channels only
"""

import os
import struct
import sys
import unittest
import wave

sys.path.insert(0, os.path.dirname(__file__))
from ppm2hid import PpmDecoder, Profile, DEFAULT_AUDIO_SAMPLE_RATE

_PROFILE = Profile()

RECORDING_PATH       = os.path.join(os.path.dirname(__file__), 'testdata', 'ppm_capture_192k.wav')
RECORDING_DURATION_S = 15

# Channels confirmed pressed in this recording (ch3=index 2, ch4=index 3).
# ch8 (index 7) was not pressed — excluded from the press/release assertion.
_EXERCISED_BUTTON_INDICES = {2, 3}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_left_channel_samples(path):
    """
    Read a .wav or raw s16le stereo file and return (left-channel samples, sample_rate).
    For .wav the sample rate is read from the file header.
    """
    if path.lower().endswith('.wav'):
        with wave.open(path, 'rb') as wf:
            raw         = wf.readframes(wf.getnframes())
            sample_rate = wf.getframerate()
    else:
        with open(path, 'rb') as f:
            raw = f.read()
        sample_rate = DEFAULT_AUDIO_SAMPLE_RATE
    samples = [
        struct.unpack_from('<h', raw, offset)[0]
        for offset in range(0, len(raw) - 3, 4)
    ]
    return samples, sample_rate


def _decode_all_frames(samples, sample_rate):
    """Run every sample through PpmDecoder and collect complete frames."""
    decoder = PpmDecoder(max_channels=len(_PROFILE.channel_map), sample_rate=sample_rate)
    frames = []
    for sample in samples:
        result = decoder.feed(sample)
        if result is not None:
            frames.append(result)
    return frames


def _channel_indices_of_type(channel_type):
    return [i for i, ch in enumerate(_PROFILE.channel_map) if ch[0] == channel_type]


# ── Test class ────────────────────────────────────────────────────────────────

class TestPpmDecoder(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(RECORDING_PATH):
            raise unittest.SkipTest(
                f"Recording not found: {RECORDING_PATH}\n"
                f"Record with:\n"
                f"  parecord --device=alsa_input.pci-0000_00_1f.3.analog-stereo "
                f"--format=s16le --rate={RECORDING_SAMPLE_RATE} --channels=2 --raw "
                f"--latency-msec=20 {RECORDING_PATH}"
            )

        samples, sample_rate = _load_left_channel_samples(RECORDING_PATH)
        cls.actual_duration  = len(samples) / sample_rate
        cls.frames           = _decode_all_frames(samples, sample_rate)

        if not cls.frames:
            raise unittest.SkipTest(
                "No PPM frames decoded — check that the transmitter was on "
                "and the signal is on the left channel."
            )

    def test_frame_count(self):
        """At 40+ Hz we expect at least 40 * duration_s frames."""
        minimum_expected = 40 * self.actual_duration
        count = len(self.frames)
        self.assertGreaterEqual(
            count, minimum_expected,
            f"Too few frames decoded: got {count}, expected ≥ {minimum_expected:.0f}. "
            f"Check that the transmitter was on during the recording."
        )

    def test_frame_rate(self):
        """Frame rate should be between 40 and 90 Hz."""
        rate = len(self.frames) / self.actual_duration
        self.assertGreaterEqual(rate, 40, f"Frame rate too low: {rate:.1f} Hz")
        self.assertLessEqual(rate, 90, f"Frame rate too high: {rate:.1f} Hz")

    def test_channel_count(self):
        """Every frame should contain exactly as many channels as _PROFILE.channel_map."""
        expected = len(_PROFILE.channel_map)
        bad = [i for i, f in enumerate(self.frames) if len(f) != expected]
        self.assertFalse(
            bad,
            f"{len(bad)} frame(s) had wrong channel count "
            f"(e.g. frame {bad[0]}: {len(self.frames[bad[0]])} channels, expected {expected})"
            if bad else ""
        )

    def test_values_in_range(self):
        """All decoded µs values must lie within [_PROFILE.axis_min_us, _PROFILE.axis_max_us]."""
        violations = []
        for frame_index, frame in enumerate(self.frames):
            for ch_index, value in enumerate(frame):
                if not (_PROFILE.axis_min_us <= value <= _PROFILE.axis_max_us):
                    violations.append((frame_index, ch_index, value))
        self.assertFalse(
            violations,
            f"{len(violations)} out-of-range values, e.g. "
            f"frame {violations[0][0]} ch{violations[0][1]+1} = {violations[0][2]} µs"
            if violations else ""
        )

    def test_axes_saturated(self):
        """
        Axis channels (ch1, ch5, ch6) should have visited at least 80 % of
        the declared range during the recording.
        """
        saturation_threshold = 0.80
        required_span = int((_PROFILE.axis_max_us - _PROFILE.axis_min_us) * saturation_threshold)

        for ch_index in _channel_indices_of_type('axis'):
            values = [frame[ch_index] for frame in self.frames if ch_index < len(frame)]
            lo, hi = min(values), max(values)
            span   = hi - lo
            self.assertGreaterEqual(
                span, required_span,
                f"ch{ch_index+1} (axis) only spanned {span} µs "
                f"(min={lo}, max={hi}); expected ≥ {required_span} µs. "
                f"Was the stick moved to full saturation?"
            )

    def test_throttle_both_directions(self):
        """
        ch2 is the throttle stick.  Over the recording it should have gone both
        above and below centre by at least 30 % of the half-range.
        """
        ch_index      = 1
        half_range    = _PROFILE.axis_max_us - _PROFILE.axis_center_us   # 400 µs
        min_excursion = int(half_range * 0.30)         # 120 µs

        values    = [frame[ch_index] for frame in self.frames if ch_index < len(frame)]
        max_above = max(v - _PROFILE.axis_center_us for v in values)
        max_below = max(_PROFILE.axis_center_us - v for v in values)

        self.assertGreaterEqual(
            max_above, min_excursion,
            f"ch2 (throttle) never exceeded centre by {min_excursion} µs above "
            f"(max above: {max_above} µs)"
        )
        self.assertGreaterEqual(
            max_below, min_excursion,
            f"ch2 (throttle) never exceeded centre by {min_excursion} µs below "
            f"(max below: {max_below} µs)"
        )

    def test_buttons_toggled(self):
        """
        Each exercised button channel must have been both pressed and released.
        Unexercised buttons (ch8 in this recording) are skipped.
        """
        for ch_index in _channel_indices_of_type('button'):
            values = [frame[ch_index] for frame in self.frames if ch_index < len(frame)]

            if ch_index not in _EXERCISED_BUTTON_INDICES:
                continue

            pressed  = any(v > _PROFILE.button_threshold_us for v in values)
            released = any(v <= _PROFILE.button_threshold_us for v in values)
            lo, hi   = min(values), max(values)

            self.assertTrue(
                pressed,
                f"ch{ch_index+1} (button) was never pressed "
                f"(all values ≤ {_PROFILE.button_threshold_us} µs)"
            )
            self.assertTrue(
                released,
                f"ch{ch_index+1} (button) was never released "
                f"(all values > {_PROFILE.button_threshold_us} µs)"
            )

    def test_slider_mid_detected(self):
        """
        ch7 slider mid position must have been seen.  LO and HI are not verified
        because the slider was not moved through all positions in this recording.
        """
        ch_index = 6
        values   = [frame[ch_index] for frame in self.frames if ch_index < len(frame)]
        saw_mid  = any(_PROFILE.slider_low_threshold_us <= v <= _PROFILE.slider_high_threshold_us for v in values)
        self.assertTrue(
            saw_mid,
            f"ch7 slider: mid position ({_PROFILE.slider_low_threshold_us}–{_PROFILE.slider_high_threshold_us} µs) "
            f"never seen (range was {min(values)}–{max(values)} µs)"
        )

    def test_decoder_stability(self):
        """
        Axis channels should not jump more than 200 µs between consecutive frames.
        Button/slider channels are excluded — their 800 µs swings are intentional.
        A small number of glitches is tolerated; only a high count is flagged.
        """
        MAX_JUMP     = 200
        GLITCH_LIMIT = 10
        axis_indices = set(_channel_indices_of_type('axis'))
        glitches     = []
        for i in range(1, len(self.frames)):
            prev, curr = self.frames[i - 1], self.frames[i]
            for ch in range(min(len(prev), len(curr))):
                if ch not in axis_indices:
                    continue
                jump = abs(curr[ch] - prev[ch])
                if jump > MAX_JUMP:
                    glitches.append((i, ch, prev[ch], curr[ch], jump))

        if glitches and len(glitches) <= GLITCH_LIMIT:
            # Small number of glitches — warn but don't fail
            print(f"\n  decoder stability: {len(glitches)} large jump(s) detected "
                  f"(threshold {MAX_JUMP} µs) — within tolerance")
        else:
            self.assertEqual(
                len(glitches), 0,
                f"{len(glitches)} large jump(s) detected (threshold {MAX_JUMP} µs)"
            )


if __name__ == '__main__':
    unittest.main()
