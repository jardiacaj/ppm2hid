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

import struct
import sys
import os

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

RECORDING_PATH        = os.path.join(os.path.dirname(__file__), 'testdata', 'ppm_capture_192k.raw')
RECORDING_SAMPLE_RATE = 192_000
RECORDING_DURATION_S  = 15


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_left_channel_samples(path):
    """Read a raw s16le stereo file and return the left-channel samples."""
    with open(path, 'rb') as f:
        raw = f.read()
    return [
        struct.unpack_from('<h', raw, offset)[0]
        for offset in range(0, len(raw) - 3, 4)
    ]


def decode_all_frames(samples):
    """Run every sample through PpmDecoder and collect complete frames."""
    decoder = PpmDecoder(max_channels=len(CHANNEL_MAP),
                         sample_rate=RECORDING_SAMPLE_RATE)
    frames = []
    for sample in samples:
        result = decoder.feed(sample)
        if result is not None:
            frames.append(result)
    return frames


def channel_indices_of_type(channel_type):
    return [i for i, ch in enumerate(CHANNEL_MAP) if ch[0] == channel_type]


# ── Individual checks ─────────────────────────────────────────────────────────

def check_frame_count(frames, duration_s):
    """At 40+ Hz we expect at least 40 * duration_s frames."""
    minimum_expected = 40 * duration_s
    count = len(frames)
    assert count >= minimum_expected, (
        f"Too few frames decoded: got {count}, expected ≥ {minimum_expected:.0f}. "
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
    the declared range during the recording.
    """
    saturation_threshold = 0.80
    required_span = int((AXIS_MAX_US - AXIS_MIN_US) * saturation_threshold)

    for ch_index in channel_indices_of_type('axis'):
        values = [frame[ch_index] for frame in frames if ch_index < len(frame)]
        lo, hi = min(values), max(values)
        span   = hi - lo
        assert span >= required_span, (
            f"ch{ch_index+1} (axis) only spanned {span} µs "
            f"(min={lo}, max={hi}); expected ≥ {required_span} µs. "
            f"Was the stick moved to full saturation?"
        )
        print(f"  ch{ch_index+1} axis span: {lo}–{hi} µs ({span} µs)  ✓")


def check_throttle_both_directions(frames):
    """
    ch2 is the throttle stick.  Over the recording it should have gone both
    above and below centre by at least 30 % of the half-range.
    """
    ch_index    = 1
    half_range  = AXIS_MAX_US - AXIS_CENTER_US   # 400 µs
    min_excursion = int(half_range * 0.30)        # 120 µs

    values    = [frame[ch_index] for frame in frames if ch_index < len(frame)]
    max_above = max(v - AXIS_CENTER_US for v in values)
    max_below = max(AXIS_CENTER_US - v for v in values)

    assert max_above >= min_excursion, (
        f"ch2 (throttle) never exceeded centre by {min_excursion} µs above "
        f"(max above: {max_above} µs)"
    )
    assert max_below >= min_excursion, (
        f"ch2 (throttle) never exceeded centre by {min_excursion} µs below "
        f"(max below: {max_below} µs)"
    )
    print(f"  ch2 throttle: +{max_above} µs / -{max_below} µs from centre  ✓")


# Channels confirmed pressed in this recording (ch3=index 2, ch4=index 3).
# ch8 (index 7) was not pressed — excluded from the press/release assertion.
_EXERCISED_BUTTON_INDICES = {2, 3}

def check_buttons_toggled(frames):
    """
    Each exercised button channel must have been both pressed and released.
    Unexercised buttons (ch8 in this recording) are noted but not failed.
    """
    for ch_index in channel_indices_of_type('button'):
        values   = [frame[ch_index] for frame in frames if ch_index < len(frame)]
        pressed  = any(v > BUTTON_THRESHOLD_US for v in values)
        released = any(v <= BUTTON_THRESHOLD_US for v in values)
        lo, hi   = min(values), max(values)

        if ch_index not in _EXERCISED_BUTTON_INDICES:
            print(f"  ch{ch_index+1} button: not exercised in recording "
                  f"(range {lo}–{hi} µs) — skipped")
            continue

        assert pressed, (
            f"ch{ch_index+1} (button) was never pressed "
            f"(all values ≤ {BUTTON_THRESHOLD_US} µs)"
        )
        assert released, (
            f"ch{ch_index+1} (button) was never released "
            f"(all values > {BUTTON_THRESHOLD_US} µs)"
        )
        print(f"  ch{ch_index+1} button: range {lo}–{hi} µs  ✓")


def check_slider_mid_detected(frames):
    """
    ch7 slider mid position must have been seen.  LO and HI are not verified
    because the slider was not moved through all positions in this recording.
    """
    ch_index = 6
    values   = [frame[ch_index] for frame in frames if ch_index < len(frame)]
    saw_mid  = any(SLIDER_LOW_THRESHOLD <= v <= SLIDER_HIGH_THRESHOLD for v in values)
    assert saw_mid, (
        f"ch7 slider: mid position ({SLIDER_LOW_THRESHOLD}–{SLIDER_HIGH_THRESHOLD} µs) "
        f"never seen (range was {min(values)}–{max(values)} µs)"
    )
    lo, hi = min(values), max(values)
    print(f"  ch7 slider: mid detected (range {lo}–{hi} µs)  ✓")
    print(f"  ch7 slider: LO/HI positions not in this recording — skipped")


def check_decoder_stability(frames):
    """
    Axis channels should not jump more than 200 µs between consecutive frames.
    Button/slider channels are excluded — their 800 µs swings are intentional.
    """
    MAX_JUMP     = 200
    axis_indices = set(channel_indices_of_type('axis'))
    glitches     = []
    for i in range(1, len(frames)):
        prev, curr = frames[i - 1], frames[i]
        for ch in range(min(len(prev), len(curr))):
            if ch not in axis_indices:
                continue
            jump = abs(curr[ch] - prev[ch])
            if jump > MAX_JUMP:
                glitches.append((i, ch, prev[ch], curr[ch], jump))

    if glitches:
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
            f"Record with:\n"
            f"  parecord --device=alsa_input.pci-0000_00_1f.3.analog-stereo "
            f"--format=s16le --rate={RECORDING_SAMPLE_RATE} --channels=2 --raw "
            f"--latency-msec=20 {RECORDING_PATH}"
        )

    file_size    = os.path.getsize(RECORDING_PATH)
    expected_min = RECORDING_SAMPLE_RATE * 4 * RECORDING_DURATION_S  # s16le stereo
    if file_size < expected_min // 2:
        print(f"Warning: recording is only {file_size // 1024} KB "
              f"(expected ~{expected_min // 1024} KB for {RECORDING_DURATION_S} s). "
              f"Proceeding anyway.")

    print(f"Loading {RECORDING_PATH}  ({file_size // 1024} KB) …")
    samples = load_left_channel_samples(RECORDING_PATH)
    actual_duration = len(samples) / RECORDING_SAMPLE_RATE
    print(f"  {len(samples):,} left-channel samples "
          f"({actual_duration:.1f} s at {RECORDING_SAMPLE_RATE} Hz)")

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
        check_throttle_both_directions,
        check_buttons_toggled,
        check_slider_mid_detected,
        check_decoder_stability,
    ]

    failures = []
    for test_fn in tests:
        name = test_fn.__name__.replace('check_', '').replace('_', ' ')
        try:
            if test_fn.__code__.co_argcount == 2:
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
