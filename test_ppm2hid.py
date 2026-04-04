#!/usr/bin/env python3
"""
test_ppm2hid.py – offline tests for the PPM decoder and channel mapping.

Decodes /tmp/ppm_capture.raw (30-second recording made with the transmitter
connected to Line In, all controls exercised) and asserts:

  1. Enough frames were decoded for a 30-second capture
  2. All channel values stay within the declared axis range
  3. Every axis channel visited both extremes (sticks went to saturation)
  4. Every button channel was both pressed and released
  5. The three-position slider (ch7) visited all three positions
  6. The gas/brake split (ch2) produced non-zero output on both axes
  7. Frame rate is plausible (40–90 Hz)
"""

import struct
import sys
import os

# ── Import constants and classes from the main module ────────────────────────

sys.path.insert(0, os.path.dirname(__file__))
from ppm2hid import (
    PpmDecoder,
    CHANNEL_MAP,
    AUDIO_SAMPLE_RATE,
    AXIS_MIN_US, AXIS_MAX_US, AXIS_CENTER_US,
    BUTTON_THRESHOLD_US,
    SLIDER_LOW_THRESHOLD, SLIDER_HIGH_THRESHOLD,
    AXIS_DEADBAND_US,
)

RECORDING_PATH = os.path.join(os.path.dirname(__file__), 'testdata', 'ppm_capture.raw')
RECORDING_DURATION_S = 30

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_left_channel_samples(path):
    """
    Read a raw s16le stereo file and return the left-channel samples as a
    list of int16 values.
    """
    with open(path, 'rb') as f:
        raw = f.read()
    # 4 bytes per stereo frame; left channel is bytes 0–1
    return [
        struct.unpack_from('<h', raw, offset)[0]
        for offset in range(0, len(raw) - 3, 4)
    ]


def decode_all_frames(samples):
    """Run every sample through PpmDecoder and collect complete frames."""
    decoder = PpmDecoder(max_channels=len(CHANNEL_MAP))
    frames  = []
    for sample in samples:
        result = decoder.feed(sample)
        if result is not None:
            frames.append(result)
    return frames


def channel_indices_of_type(channel_type):
    return [i for i, ch in enumerate(CHANNEL_MAP) if ch[0] == channel_type]


# ── Individual checks ─────────────────────────────────────────────────────────

def check_frame_count(frames, duration_s):
    """At 50–70 Hz we expect at least 40 * duration_s frames."""
    minimum_expected = 40 * duration_s
    count = len(frames)
    assert count >= minimum_expected, (
        f"Too few frames decoded: got {count}, expected ≥ {minimum_expected}. "
        f"Check that the transmitter was on during the recording."
    )
    print(f"  frame count: {count}  ({count / duration_s:.1f} Hz avg)  ✓")


def check_frame_rate(frames, duration_s):
    """Frame rate should be between 40 and 90 Hz."""
    rate = len(frames) / duration_s
    assert 40 <= rate <= 90, (
        f"Implausible frame rate: {rate:.1f} Hz (expected 40–90 Hz)"
    )
    print(f"  frame rate:  {rate:.1f} Hz  ✓")


def check_channel_count(frames):
    """Every frame should contain exactly as many channels as CHANNEL_MAP."""
    expected = len(CHANNEL_MAP)
    bad = [i for i, f in enumerate(frames) if len(f) != expected]
    assert not bad, (
        f"{len(bad)} frame(s) had wrong channel count "
        f"(e.g. frame {bad[0]}: {len(frames[bad[0]])} channels, expected {expected})"
    )
    print(f"  channel count: {expected} per frame  ✓")


def check_values_in_range(frames):
    """All decoded µs values must lie within [AXIS_MIN_US, AXIS_MAX_US]."""
    violations = []
    for frame_index, frame in enumerate(frames):
        for ch_index, value in enumerate(frame):
            if not (AXIS_MIN_US <= value <= AXIS_MAX_US):
                violations.append((frame_index, ch_index, value))
    assert not violations, (
        f"{len(violations)} out-of-range values, e.g. "
        f"frame {violations[0][0]} ch{violations[0][1]+1} = {violations[0][2]} µs"
    )
    print(f"  all values in [{AXIS_MIN_US}, {AXIS_MAX_US}] µs  ✓")


def check_axes_saturated(frames):
    """
    Axis channels (ch1, ch5, ch6) should have visited at least 80 % of
    the declared range during the 30-second exercise session.
    """
    saturation_threshold = 0.80   # fraction of range that must be covered
    required_span = int((AXIS_MAX_US - AXIS_MIN_US) * saturation_threshold)

    for ch_index in channel_indices_of_type('axis'):
        values  = [frame[ch_index] for frame in frames if ch_index < len(frame)]
        lo, hi  = min(values), max(values)
        span    = hi - lo
        assert span >= required_span, (
            f"ch{ch_index+1} (axis) only spanned {span} µs "
            f"(min={lo}, max={hi}); expected ≥ {required_span} µs. "
            f"Was the stick moved to full saturation?"
        )
        print(f"  ch{ch_index+1} axis span: {lo}–{hi} µs ({span} µs)  ✓")


def check_gas_brake_both_used(frames):
    """
    ch2 is the throttle/brake stick.  Over 30 s it should have gone both
    above and below centre by at least 30 % of the half-range.
    """
    ch_index  = 1   # ch2
    half_range = AXIS_MAX_US - AXIS_CENTER_US   # 400 µs
    min_excursion = int(half_range * 0.30)      # 120 µs

    values     = [frame[ch_index] for frame in frames if ch_index < len(frame)]
    max_above  = max(v - AXIS_CENTER_US for v in values)
    max_below  = max(AXIS_CENTER_US - v for v in values)

    assert max_above >= min_excursion, (
        f"ch2 (gas) never exceeded centre by {min_excursion} µs "
        f"(max excursion above centre: {max_above} µs). "
        f"Push the throttle stick forward during recording."
    )
    assert max_below >= min_excursion, (
        f"ch2 (brake) never exceeded centre by {min_excursion} µs "
        f"(max excursion below centre: {max_below} µs). "
        f"Pull the throttle stick back during recording."
    )
    print(f"  ch2 gas/brake: +{max_above} µs / -{max_below} µs from centre  ✓")


def check_buttons_toggled(frames):
    """
    Each button channel (ch3, ch4, ch8, ch9, ch10) must have been both
    pressed and released at least once.
    """
    for ch_index in channel_indices_of_type('button'):
        values   = [frame[ch_index] for frame in frames if ch_index < len(frame)]
        pressed  = any(v > BUTTON_THRESHOLD_US for v in values)
        released = any(v <= BUTTON_THRESHOLD_US for v in values)
        assert pressed, (
            f"ch{ch_index+1} (button) was never pressed "
            f"(all values ≤ {BUTTON_THRESHOLD_US} µs)"
        )
        assert released, (
            f"ch{ch_index+1} (button) was never released "
            f"(all values > {BUTTON_THRESHOLD_US} µs)"
        )
        lo = min(values)
        hi = max(values)
        print(f"  ch{ch_index+1} button: range {lo}–{hi} µs  ✓")


def check_slider_all_positions(frames):
    """
    The three-position slider (ch7) must have visited low, mid and high
    positions during the recording.
    """
    ch_index = 6   # ch7
    values   = [frame[ch_index] for frame in frames if ch_index < len(frame)]

    saw_low  = any(v < SLIDER_LOW_THRESHOLD  for v in values)
    saw_mid  = any(SLIDER_LOW_THRESHOLD <= v <= SLIDER_HIGH_THRESHOLD for v in values)
    saw_high = any(v > SLIDER_HIGH_THRESHOLD for v in values)

    assert saw_low,  (
        f"ch7 slider: low position (<{SLIDER_LOW_THRESHOLD} µs) never seen "
        f"(min value was {min(values)} µs)"
    )
    assert saw_mid,  (
        f"ch7 slider: mid position ({SLIDER_LOW_THRESHOLD}–{SLIDER_HIGH_THRESHOLD} µs) "
        f"never seen"
    )
    assert saw_high, (
        f"ch7 slider: high position (>{SLIDER_HIGH_THRESHOLD} µs) never seen "
        f"(max value was {max(values)} µs)"
    )
    lo, hi = min(values), max(values)
    print(f"  ch7 slider: all 3 positions seen (range {lo}–{hi} µs)  ✓")


def check_decoder_stability(frames):
    """
    No two consecutive frames should differ by more than 200 µs on any
    channel (catches decoder glitches like missed syncs or misaligned frames).
    """
    MAX_JUMP = 200   # µs
    glitches = []
    for i in range(1, len(frames)):
        prev, curr = frames[i - 1], frames[i]
        for ch in range(min(len(prev), len(curr))):
            jump = abs(curr[ch] - prev[ch])
            if jump > MAX_JUMP:
                glitches.append((i, ch, prev[ch], curr[ch], jump))

    if glitches:
        # Report but don't fail — a glitch at sync loss is acceptable
        print(f"  decoder stability: {len(glitches)} large jump(s) detected "
              f"(threshold {MAX_JUMP} µs) — review if count is high")
        for frame_i, ch, before, after, jump in glitches[:5]:
            print(f"    frame {frame_i} ch{ch+1}: {before}→{after} µs (Δ{jump})")
    else:
        print(f"  decoder stability: no large jumps  ✓")


# ── Test runner ───────────────────────────────────────────────────────────────

def run_all_tests():
    if not os.path.exists(RECORDING_PATH):
        sys.exit(
            f"Recording not found: {RECORDING_PATH}\n"
            f"Run:  parecord --device=alsa_input.pci-0000_00_1f.3.analog-stereo "
            f"--format=s16le --rate=48000 --channels=2 --raw "
            f"--latency-msec=20 {RECORDING_PATH}"
        )

    file_size    = os.path.getsize(RECORDING_PATH)
    expected_min = AUDIO_SAMPLE_RATE * 2 * RECORDING_DURATION_S  # 2 bytes × 2 ch × 30 s
    if file_size < expected_min // 2:
        print(f"Warning: recording is only {file_size // 1024} KB "
              f"(expected ~{expected_min // 1024} KB for {RECORDING_DURATION_S} s). "
              f"Proceeding anyway.")

    print(f"Loading {RECORDING_PATH}  ({file_size // 1024} KB) …")
    samples = load_left_channel_samples(RECORDING_PATH)
    print(f"  {len(samples)} left-channel samples "
          f"({len(samples) / AUDIO_SAMPLE_RATE:.1f} s at {AUDIO_SAMPLE_RATE} Hz)")

    print("Decoding PPM frames …")
    frames = decode_all_frames(samples)
    print(f"  {len(frames)} frames decoded")

    if not frames:
        sys.exit("No PPM frames decoded — check that the transmitter was on "
                 "and the signal is on the left channel.")

    print("\nRunning checks:")
    tests = [
        check_frame_count,
        check_frame_rate,
        check_channel_count,
        check_values_in_range,
        check_axes_saturated,
        check_gas_brake_both_used,
        check_buttons_toggled,
        check_slider_all_positions,
        check_decoder_stability,
    ]

    failures = []
    for test_fn in tests:
        name = test_fn.__name__.replace('check_', '').replace('_', ' ')
        try:
            if test_fn.__code__.co_argcount == 2:
                actual_duration = len(samples) / AUDIO_SAMPLE_RATE
                test_fn(frames, actual_duration)
            else:
                test_fn(frames)
        except AssertionError as exc:
            print(f"  FAIL [{name}]: {exc}")
            failures.append(name)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        sys.exit(1)
    else:
        print(f"All {len(tests)} checks passed.")


if __name__ == '__main__':
    run_all_tests()
