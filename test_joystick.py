#!/usr/bin/env python3
"""
test_joystick.py – unit tests for emit_channel_events and ChannelOutputState.

Tests intercept os.write with a _WriteSink that captures and parses
input_event structs (INPUT_EVENT_STRUCT = 'qqHHi', 24 bytes each).
No /dev/uinput access is required.

Coverage:
  - EV_SYN is always flushed, even when no channels changed
  - Axis passthrough and central deadzone snap-to-centre
  - Axis inversion (ch2 / ABS_Y)
  - Button press and release with hysteresis
  - Hysteresis: value exactly at threshold does not trigger in released state
  - Three-position slider (ch7): LO / MID / HI transitions and hysteresis
  - transition list returned by emit_channel_events
  - Integration: replay 192 kHz recording, assert ch3 and ch4 had press+release
"""

from __future__ import annotations

import os
import struct
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from ppm2hid import (
    ChannelOutputState,
    emit_channel_events,
    PpmDecoder,
    Profile,
    INPUT_EVENT_STRUCT,
    EV_SYN, EV_KEY, EV_ABS,
    SYN_REPORT,
    ABS_X, ABS_Y,
    BTN_SW_CH3, BTN_SW_CH4, BTN_SL_LO, BTN_SL_HI,
)

_PROFILE = Profile()

RECORDING_PATH        = os.path.join(os.path.dirname(__file__), 'testdata', 'ppm_capture_192k.raw')
RECORDING_SAMPLE_RATE = 192_000

EVENT_SIZE = struct.calcsize(INPUT_EVENT_STRUCT)   # 24 bytes


# ── Helpers ───────────────────────────────────────────────────────────────────

class _WriteSink:
    """Replaces os.write; accumulates bytes written to fd=1 (sentinel)."""

    def __init__(self) -> None:
        self._buf = b''

    def write(self, fd: int, data: bytes) -> int:
        self._buf += data
        return len(data)

    def events(self) -> list[tuple[int, int, int]]:
        """Parse captured bytes into (type, code, value) tuples."""
        out = []
        for i in range(0, len(self._buf) - EVENT_SIZE + 1, EVENT_SIZE):
            sec, usec, etype, ecode, evalue = struct.unpack_from(
                INPUT_EVENT_STRUCT, self._buf, i)
            out.append((etype, ecode, evalue))
        return out

    def reset(self) -> None:
        self._buf = b''


def _emit(sink: _WriteSink, state: ChannelOutputState,
          ppm_frame: list[int],
          profile: Profile | None = None) -> list[tuple[int, int, bool]]:
    """Call emit_channel_events with fd=1 while os.write is patched."""
    with patch('ppm2hid.uinput.os.write', side_effect=sink.write):
        return emit_channel_events(1, state, ppm_frame, profile)


def _make_frame(*values: int) -> list[int]:
    """Build a PPM frame list; pads missing channels with their inactive default.

    ch7 (index 6) is the three-position slider: pad with 1100 µs (physical low
    position = no buttons pressed).  All other channels pad with _PROFILE.axis_center_us.
    """
    frame = list(values)
    while len(frame) < len(_PROFILE.channel_map):
        frame.append(1100 if len(frame) == 6 else _PROFILE.axis_center_us)
    return frame


# ── Test cases ────────────────────────────────────────────────────────────────

class TestEvSync(unittest.TestCase):
    """EV_SYN is sent after every call, even when nothing changed."""

    def test_syn_sent_when_nothing_changed(self) -> None:
        sink  = _WriteSink()
        state = ChannelOutputState()
        # First frame: axes at centre (== last-emitted value), buttons at released
        # (no transition). Only EV_SYN should appear.
        _emit(sink, state, _make_frame(_PROFILE.axis_center_us))
        events = sink.events()
        syn_events = [e for e in events if e[0] == EV_SYN]
        self.assertTrue(syn_events, "Expected at least one EV_SYN")
        self.assertEqual(syn_events[-1], (EV_SYN, SYN_REPORT, 0))

    def test_syn_sent_on_second_identical_frame(self) -> None:
        sink  = _WriteSink()
        state = ChannelOutputState()
        frame = _make_frame(_PROFILE.axis_max_us)
        _emit(sink, state, frame)
        sink.reset()
        _emit(sink, state, frame)   # second identical frame
        self.assertIn((EV_SYN, SYN_REPORT, 0), sink.events())


class TestAxisPassthrough(unittest.TestCase):

    def test_large_move_emits_abs_event(self) -> None:
        sink  = _WriteSink()
        state = ChannelOutputState()
        # Pre-load EMA accumulator so the smoothed value matches the input
        # on the first call (otherwise the first frame lands halfway between
        # the default 1500.0 accumulator and the new value).
        state.axis_smoothed[ABS_X] = float(_PROFILE.axis_max_us)
        _emit(sink, state, _make_frame(_PROFILE.axis_max_us))   # ch1 = 1900
        self.assertIn((EV_ABS, ABS_X, _PROFILE.axis_max_us), sink.events())

    def test_value_inside_deadzone_snaps_to_centre(self) -> None:
        """With a non-zero deadzone, near-centre values snap to the exact centre."""
        profile = Profile()
        profile.axis_deadzone_pct = 10   # half-range 400 µs × 10% = ±40 µs
        sink  = _WriteSink()
        state = ChannelOutputState()
        # Pre-load the EMA accumulator so smoothed_f equals the input on first call.
        state.axis_smoothed[ABS_X] = 1520.0
        # Force last-emitted away from centre so a snap-to-centre is observable.
        state.axis_values[ABS_X]   = 1900
        _emit(sink, state, _make_frame(1520), profile=profile)
        abs_events = [e for e in sink.events() if e[0] == EV_ABS and e[1] == ABS_X]
        self.assertEqual(abs_events, [(EV_ABS, ABS_X, profile.axis_center_us)],
                         "Value inside deadzone should snap to centre")

    def test_value_outside_deadzone_passes_through(self) -> None:
        profile = Profile()
        profile.axis_deadzone_pct = 5    # half-range 400 µs × 5% = ±20 µs
        sink  = _WriteSink()
        state = ChannelOutputState()
        state.axis_smoothed[ABS_X] = 1530.0
        state.axis_values[ABS_X]   = profile.axis_center_us
        _emit(sink, state, _make_frame(1530), profile=profile)
        abs_events = [e for e in sink.events() if e[0] == EV_ABS and e[1] == ABS_X]
        self.assertEqual(abs_events, [(EV_ABS, ABS_X, 1530)],
                         "Value outside deadzone should pass through unchanged")

    def test_default_profile_has_no_deadzone(self) -> None:
        """Default axis_deadzone_pct = 0 disables the snap entirely."""
        self.assertEqual(_PROFILE.axis_deadzone_pct, 0)
        sink  = _WriteSink()
        state = ChannelOutputState()
        state.axis_smoothed[ABS_X] = 1505.0
        state.axis_values[ABS_X]   = 1900
        _emit(sink, state, _make_frame(1505))
        self.assertIn((EV_ABS, ABS_X, 1505), sink.events())


class TestAxisInversion(unittest.TestCase):
    """ch2 (ABS_Y) is inverted: value_us = AXIS_MIN + AXIS_MAX − raw_us."""

    def _ch2_frame(self, raw_us: int) -> list[int]:
        return _make_frame(_PROFILE.axis_center_us, raw_us)

    def test_inversion_at_min(self) -> None:
        expected = _PROFILE.axis_min_us + _PROFILE.axis_max_us - _PROFILE.axis_min_us   # = _PROFILE.axis_max_us
        sink  = _WriteSink()
        state = ChannelOutputState()
        # Pre-load EMA accumulator with the post-inversion target so the
        # smoothed value reaches it on the first call.
        state.axis_smoothed[ABS_Y] = float(expected)
        _emit(sink, state, self._ch2_frame(_PROFILE.axis_min_us))
        self.assertIn((EV_ABS, ABS_Y, expected), sink.events())

    def test_inversion_at_max(self) -> None:
        expected = _PROFILE.axis_min_us + _PROFILE.axis_max_us - _PROFILE.axis_max_us   # = _PROFILE.axis_min_us
        sink  = _WriteSink()
        state = ChannelOutputState()
        state.axis_smoothed[ABS_Y] = float(expected)
        _emit(sink, state, self._ch2_frame(_PROFILE.axis_max_us))
        self.assertIn((EV_ABS, ABS_Y, expected), sink.events())

    def test_inversion_at_centre_emits_centre(self) -> None:
        """Centre raw value, inverted, is still centre."""
        sink  = _WriteSink()
        state = ChannelOutputState()
        state.axis_smoothed[ABS_Y] = float(_PROFILE.axis_center_us)
        # Force last-emitted away from centre so the centre emission is observable.
        state.axis_values[ABS_Y]   = _PROFILE.axis_max_us
        _emit(sink, state, self._ch2_frame(_PROFILE.axis_center_us))
        abs_y = [e for e in sink.events() if e[0] == EV_ABS and e[1] == ABS_Y]
        self.assertEqual(abs_y, [(EV_ABS, ABS_Y, _PROFILE.axis_center_us)])


class TestButtonHysteresis(unittest.TestCase):
    """
    Hysteresis thresholds:
      - To press   (from released): raw_us > _PROFILE.button_threshold_us + _PROFILE.button_hysteresis_us
                                    i.e. raw_us > 1500 + 21 = 1521
      - To release (from pressed):  raw_us <= _PROFILE.button_threshold_us - _PROFILE.button_hysteresis_us
                                    i.e. raw_us <= 1479
    """

    PRESS_THRESHOLD   = _PROFILE.button_threshold_us + _PROFILE.button_hysteresis_us   # 1521
    RELEASE_THRESHOLD = _PROFILE.button_threshold_us - _PROFILE.button_hysteresis_us   # 1479

    def _ch3_frame(self, raw_us: int) -> list[int]:
        return _make_frame(_PROFILE.axis_center_us, _PROFILE.axis_center_us, raw_us)

    def test_press_above_threshold(self) -> None:
        sink  = _WriteSink()
        state = ChannelOutputState()
        transitions = _emit(sink, state, self._ch3_frame(self.PRESS_THRESHOLD + 1))
        self.assertIn((EV_KEY, BTN_SW_CH3, 1), sink.events())
        self.assertIn((3, BTN_SW_CH3, True), transitions)

    def test_value_at_press_threshold_does_not_press(self) -> None:
        """raw_us = 1521 — with hys=-21 (released state): threshold = 1521, need > 1521."""
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch3_frame(self.PRESS_THRESHOLD))
        key_events = [e for e in sink.events() if e[0] == EV_KEY]
        self.assertEqual(key_events, [], f"value={self.PRESS_THRESHOLD} should NOT press")

    def test_value_well_above_center_presses(self) -> None:
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch3_frame(1900))   # fully pressed
        self.assertIn((EV_KEY, BTN_SW_CH3, 1), sink.events())

    def test_release_below_threshold(self) -> None:
        sink  = _WriteSink()
        state = ChannelOutputState()
        # Press first
        _emit(sink, state, self._ch3_frame(1900))
        sink.reset()
        # Release: value <= 1479
        transitions = _emit(sink, state, self._ch3_frame(self.RELEASE_THRESHOLD))
        self.assertIn((EV_KEY, BTN_SW_CH3, 0), sink.events())
        self.assertIn((3, BTN_SW_CH3, False), transitions)

    def test_value_just_above_release_threshold_stays_pressed(self) -> None:
        """raw_us = 1480: with hys=+21 (pressed state): threshold = 1479, need <= 1479."""
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch3_frame(1900))   # press
        sink.reset()
        _emit(sink, state, self._ch3_frame(self.RELEASE_THRESHOLD + 1))   # 1480
        key_events = [e for e in sink.events() if e[0] == EV_KEY]
        self.assertEqual(key_events, [], "1480 should stay pressed — inside hysteresis band")

    def test_no_event_when_button_stays_released(self) -> None:
        sink  = _WriteSink()
        state = ChannelOutputState()
        for _ in range(3):
            _emit(sink, state, self._ch3_frame(_PROFILE.axis_center_us))
        key_events = [e for e in sink.events() if e[0] == EV_KEY]
        self.assertEqual(key_events, [], "Stable released button must not emit events")

    def test_no_event_when_button_stays_pressed(self) -> None:
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch3_frame(1900))
        sink.reset()
        for _ in range(3):
            _emit(sink, state, self._ch3_frame(1900))
        key_events = [e for e in sink.events() if e[0] == EV_KEY]
        self.assertEqual(key_events, [], "Stable pressed button must not emit repeat events")

    def test_ch4_independent_of_ch3(self) -> None:
        """ch3 and ch4 use independent state; pressing one does not affect the other."""
        sink  = _WriteSink()
        state = ChannelOutputState()
        ch4_frame = _make_frame(_PROFILE.axis_center_us, _PROFILE.axis_center_us, _PROFILE.axis_center_us, 1900)
        _emit(sink, state, ch4_frame)
        key_events = [e for e in sink.events() if e[0] == EV_KEY]
        self.assertIn((EV_KEY, BTN_SW_CH4, 1), key_events)
        ch3_pressed = any(e == (EV_KEY, BTN_SW_CH3, 1) for e in key_events)
        self.assertFalse(ch3_pressed, "ch3 must not be pressed when only ch4 moves")


class TestSliderThreePos(unittest.TestCase):
    """
    ch7 (index 6) is a three-position slider encoded as two buttons:
      low  position (PPM ~1100 µs) → neither pressed
      mid  position (PPM ~1500 µs) → BTN_SL_LO pressed
      high position (PPM ~1900 µs) → BTN_SL_LO + BTN_SL_HI pressed

    BTN_SL_LO: pressed when raw_us > _PROFILE.slider_low_threshold_us (mid and high positions)
      press threshold (released):  raw_us > _PROFILE.slider_low_threshold_us  + HYSTERESIS = 1321
      release threshold (pressed): raw_us <= _PROFILE.slider_low_threshold_us − HYSTERESIS = 1279

    BTN_SL_HI: pressed when raw_us > _PROFILE.slider_high_threshold_us (high position only)
      press threshold (released):  raw_us > _PROFILE.slider_high_threshold_us + HYSTERESIS = 1721
      release threshold (pressed): raw_us <= _PROFILE.slider_high_threshold_us − HYSTERESIS = 1679
    """

    SL_LO_PRESS_US   = _PROFILE.slider_low_threshold_us  + _PROFILE.button_hysteresis_us   # 1321
    SL_LO_RELEASE_US = _PROFILE.slider_low_threshold_us  - _PROFILE.button_hysteresis_us   # 1279
    SL_HI_PRESS_US   = _PROFILE.slider_high_threshold_us + _PROFILE.button_hysteresis_us   # 1721
    SL_HI_RELEASE_US = _PROFILE.slider_high_threshold_us - _PROFILE.button_hysteresis_us   # 1679

    def _ch7_frame(self, raw_us: int) -> list[int]:
        frame = [_PROFILE.axis_center_us] * len(_PROFILE.channel_map)
        frame[6] = raw_us
        return frame

    def test_low_position_presses_neither(self) -> None:
        """PPM ~1100 µs → no buttons pressed."""
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch7_frame(1100))
        key_events = [e for e in sink.events() if e[0] == EV_KEY]
        self.assertEqual(key_events, [])

    def test_low_threshold_not_crossed(self) -> None:
        """raw_us = 1321 exactly: need > 1321 to press BTN_SL_LO."""
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch7_frame(self.SL_LO_PRESS_US))
        key_events = [e for e in sink.events() if e[0] == EV_KEY]
        self.assertEqual(key_events, [])

    def test_mid_position_presses_btnsllo_only(self) -> None:
        """PPM ~1500 µs → BTN_SL_LO pressed, BTN_SL_HI not."""
        sink  = _WriteSink()
        state = ChannelOutputState()
        transitions = _emit(sink, state, self._ch7_frame(_PROFILE.axis_center_us))
        self.assertIn((EV_KEY, BTN_SL_LO, 1), sink.events())
        self.assertNotIn((EV_KEY, BTN_SL_HI, 1), sink.events())
        self.assertIn((7, BTN_SL_LO, True), transitions)

    def test_mid_to_low_releases_btnsllo(self) -> None:
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch7_frame(_PROFILE.axis_center_us))            # enter mid
        sink.reset()
        _emit(sink, state, self._ch7_frame(self.SL_LO_RELEASE_US))     # return to low
        self.assertIn((EV_KEY, BTN_SL_LO, 0), sink.events())

    def test_high_position_presses_both(self) -> None:
        """PPM ~1900 µs → BTN_SL_LO + BTN_SL_HI pressed."""
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch7_frame(1900))
        self.assertIn((EV_KEY, BTN_SL_LO, 1), sink.events())
        self.assertIn((EV_KEY, BTN_SL_HI, 1), sink.events())

    def test_high_to_mid_releases_btnsllhi_only(self) -> None:
        """Dropping from high to mid releases BTN_SL_HI, BTN_SL_LO stays pressed."""
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch7_frame(1900))                      # enter high
        sink.reset()
        _emit(sink, state, self._ch7_frame(self.SL_HI_RELEASE_US))     # drop to mid
        self.assertIn((EV_KEY, BTN_SL_HI, 0), sink.events())
        self.assertNotIn((EV_KEY, BTN_SL_LO, 0), sink.events())
        self.assertTrue(state.button_states[BTN_SL_LO])

    def test_high_to_low_releases_both(self) -> None:
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch7_frame(1900))                      # enter high
        sink.reset()
        _emit(sink, state, self._ch7_frame(self.SL_LO_RELEASE_US))     # go to low
        self.assertIn((EV_KEY, BTN_SL_HI, 0), sink.events())
        self.assertIn((EV_KEY, BTN_SL_LO, 0), sink.events())

    def test_btnsllhi_never_pressed_without_btnsllo(self) -> None:
        """BTN_SL_HI pressed implies BTN_SL_LO pressed — invariant across full sweep."""
        sink  = _WriteSink()
        state = ChannelOutputState()
        for us in range(_PROFILE.axis_min_us, _PROFILE.axis_max_us + 1, 10):
            _emit(sink, state, self._ch7_frame(us))
            lo = state.button_states[BTN_SL_LO]
            hi = state.button_states[BTN_SL_HI]
            self.assertFalse(hi and not lo,
                             f"BTN_SL_HI pressed without BTN_SL_LO at {us} µs")


class TestTransitions(unittest.TestCase):
    """emit_channel_events returns only the transitions that occurred."""

    def test_no_transitions_when_nothing_changes(self) -> None:
        sink  = _WriteSink()
        state = ChannelOutputState()
        t = _emit(sink, state, _make_frame(_PROFILE.axis_center_us))
        self.assertEqual(t, [])

    def test_single_button_press_returns_one_transition(self) -> None:
        sink  = _WriteSink()
        state = ChannelOutputState()
        t = _emit(sink, state, _make_frame(_PROFILE.axis_center_us, _PROFILE.axis_center_us, 1900))
        self.assertEqual(len(t), 1)
        self.assertEqual(t[0], (3, BTN_SW_CH3, True))

    def test_press_then_release_returns_separate_transitions(self) -> None:
        sink  = _WriteSink()
        state = ChannelOutputState()
        t_press   = _emit(sink, state, _make_frame(_PROFILE.axis_center_us, _PROFILE.axis_center_us, 1900))
        t_release = _emit(sink, state, _make_frame(_PROFILE.axis_center_us, _PROFILE.axis_center_us, 1100))
        self.assertEqual(t_press,   [(3, BTN_SW_CH3, True)])
        self.assertEqual(t_release, [(3, BTN_SW_CH3, False)])


# ── Integration test ──────────────────────────────────────────────────────────

class TestIntegrationRecording(unittest.TestCase):
    """
    Replay the 192 kHz recording through PpmDecoder + emit_channel_events.
    ch3 and ch4 must have had at least one press and one release each.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(RECORDING_PATH):
            raise FileNotFoundError(
                f"Recording not found: {RECORDING_PATH}  "
                f"(run: python3 record_ppm.py --name ppm_capture_192k --duration 15)"
            )
        with open(RECORDING_PATH, 'rb') as f:
            raw = f.read()
        cls.samples = [
            struct.unpack_from('<h', raw, offset)[0]
            for offset in range(0, len(raw) - 3, 4)
        ]

    def test_ch3_and_ch4_press_and_release(self) -> None:
        decoder = PpmDecoder(max_channels=len(_PROFILE.channel_map),
                             sample_rate=RECORDING_SAMPLE_RATE)
        state = ChannelOutputState()
        sink  = _WriteSink()

        btn_events = {BTN_SW_CH3: [], BTN_SW_CH4: []}

        for sample in self.samples:
            frame = decoder.feed(sample)
            if frame is None:
                continue
            with patch('ppm2hid.uinput.os.write', side_effect=sink.write):
                emit_channel_events(1, state, frame)
            for e in sink.events():
                etype, ecode, evalue = e
                if etype == EV_KEY and ecode in btn_events:
                    btn_events[ecode].append(evalue)
            sink.reset()

        for btn_code, name in ((BTN_SW_CH3, 'BTN_SW_CH3'), (BTN_SW_CH4, 'BTN_SW_CH4')):
            evs = btn_events[btn_code]
            self.assertTrue(evs, f"{name}: no events at all — button never changed state")
            self.assertIn(1, evs, f"{name}: button was never pressed (value=1 never seen)")
            self.assertIn(0, evs, f"{name}: button was never released (value=0 never seen)")
            # First event must be a press (not a spurious release from initial state)
            self.assertEqual(evs[0], 1,
                             f"{name}: first event should be a press, got {evs[0]}")
            print(f"  {name}: {len(evs)} event(s) — "
                  f"presses={evs.count(1)}, releases={evs.count(0)}  ✓")


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    unittest.main(verbosity=2)
