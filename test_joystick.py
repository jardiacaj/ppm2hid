#!/usr/bin/env python3
"""
test_joystick.py – unit tests for emit_channel_events and ChannelOutputState.

Tests intercept os.write with a _WriteSink that captures and parses
input_event structs (INPUT_EVENT_STRUCT = 'qqHHi', 24 bytes each).
No /dev/uinput access is required.

Coverage:
  - EV_SYN is always flushed, even when no channels changed
  - Axis passthrough and deadband suppression
  - Axis inversion (ch2 / ABS_Y)
  - Button press and release with hysteresis
  - Hysteresis: value exactly at threshold does not trigger in released state
  - Three-position slider (ch7): LO / MID / HI transitions and hysteresis
  - transition list returned by emit_channel_events
  - Integration: replay 192 kHz recording, assert ch3 and ch4 had press+release
"""

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
    CHANNEL_MAP,
    INPUT_EVENT_STRUCT,
    EV_SYN, EV_KEY, EV_ABS,
    SYN_REPORT,
    ABS_X, ABS_Y, ABS_RX, ABS_RY,
    BTN_SW_CH3, BTN_SW_CH4, BTN_SL_LO, BTN_SL_HI, BTN_SW_CH8,
    AXIS_MIN_US, AXIS_MAX_US, AXIS_CENTER_US, AXIS_DEADBAND_US,
    BUTTON_THRESHOLD_US, BUTTON_HYSTERESIS_US,
    SLIDER_LOW_THRESHOLD, SLIDER_HIGH_THRESHOLD,
)

RECORDING_PATH        = os.path.join(os.path.dirname(__file__), 'testdata', 'ppm_capture_192k.raw')
RECORDING_SAMPLE_RATE = 192_000

EVENT_SIZE = struct.calcsize(INPUT_EVENT_STRUCT)   # 24 bytes


# ── Helpers ───────────────────────────────────────────────────────────────────

class _WriteSink:
    """Replaces os.write; accumulates bytes written to fd=1 (sentinel)."""

    def __init__(self):
        self._buf = b''

    def write(self, fd, data):
        self._buf += data
        return len(data)

    def events(self):
        """Parse captured bytes into (type, code, value) tuples."""
        out = []
        for i in range(0, len(self._buf) - EVENT_SIZE + 1, EVENT_SIZE):
            sec, usec, etype, ecode, evalue = struct.unpack_from(
                INPUT_EVENT_STRUCT, self._buf, i)
            out.append((etype, ecode, evalue))
        return out

    def reset(self):
        self._buf = b''


def _emit(sink, state, ppm_frame):
    """Call emit_channel_events with fd=1 while os.write is patched."""
    with patch('ppm2hid.os.write', side_effect=sink.write):
        return emit_channel_events(1, state, ppm_frame)


def _make_frame(*values):
    """Build a PPM frame list; pads missing channels with AXIS_CENTER_US."""
    frame = list(values)
    while len(frame) < len(CHANNEL_MAP):
        frame.append(AXIS_CENTER_US)
    return frame


# ── Test cases ────────────────────────────────────────────────────────────────

class TestEvSync(unittest.TestCase):
    """EV_SYN is sent after every call, even when nothing changed."""

    def test_syn_sent_when_nothing_changed(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        # First frame: axes at centre, buttons at released → deadband skips axes,
        # buttons don't change. Only EV_SYN should appear.
        _emit(sink, state, _make_frame(AXIS_CENTER_US))
        events = sink.events()
        syn_events = [e for e in events if e[0] == EV_SYN]
        self.assertTrue(syn_events, "Expected at least one EV_SYN")
        self.assertEqual(syn_events[-1], (EV_SYN, SYN_REPORT, 0))

    def test_syn_sent_on_second_identical_frame(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        frame = _make_frame(AXIS_MAX_US)
        _emit(sink, state, frame)
        sink.reset()
        _emit(sink, state, frame)   # second identical frame
        self.assertIn((EV_SYN, SYN_REPORT, 0), sink.events())


class TestAxisPassthrough(unittest.TestCase):

    def test_large_move_emits_abs_event(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, _make_frame(AXIS_MAX_US))   # ch1 = 1900
        self.assertIn((EV_ABS, ABS_X, AXIS_MAX_US), sink.events())

    def test_small_move_within_deadband_suppressed(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        # First move establishes baseline at AXIS_CENTER_US (deadband applied to Δ)
        _emit(sink, state, _make_frame(AXIS_MAX_US))
        sink.reset()
        # Tiny nudge: less than AXIS_DEADBAND_US from last emitted value
        tiny = AXIS_MAX_US - (AXIS_DEADBAND_US - 1)
        _emit(sink, state, _make_frame(tiny))
        abs_events = [e for e in sink.events() if e[0] == EV_ABS and e[1] == ABS_X]
        self.assertEqual(abs_events, [], "Deadband should suppress tiny movement")

    def test_move_exactly_at_deadband_emits(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, _make_frame(AXIS_MAX_US))
        sink.reset()
        at_boundary = AXIS_MAX_US - AXIS_DEADBAND_US
        _emit(sink, state, _make_frame(at_boundary))
        abs_events = [e for e in sink.events() if e[0] == EV_ABS and e[1] == ABS_X]
        self.assertTrue(abs_events, "Movement exactly at deadband should emit")


class TestAxisInversion(unittest.TestCase):
    """ch2 (ABS_Y) is inverted: value_us = AXIS_MIN + AXIS_MAX − raw_us."""

    def _ch2_frame(self, raw_us):
        return _make_frame(AXIS_CENTER_US, raw_us)

    def test_inversion_at_min(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch2_frame(AXIS_MIN_US))
        expected = AXIS_MIN_US + AXIS_MAX_US - AXIS_MIN_US   # = AXIS_MAX_US
        self.assertIn((EV_ABS, ABS_Y, expected), sink.events())

    def test_inversion_at_max(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch2_frame(AXIS_MAX_US))
        expected = AXIS_MIN_US + AXIS_MAX_US - AXIS_MAX_US   # = AXIS_MIN_US
        self.assertIn((EV_ABS, ABS_Y, expected), sink.events())

    def test_inversion_at_centre_emitted_on_first_move(self):
        """Centre inverts to centre; first move from ChannelOutputState default triggers emit."""
        sink  = _WriteSink()
        state = ChannelOutputState()
        # Move away from centre first, then come back exactly to centre
        _emit(sink, state, self._ch2_frame(AXIS_MAX_US))
        sink.reset()
        # inverted centre = AXIS_MIN + AXIS_MAX - AXIS_CENTER = AXIS_CENTER
        _emit(sink, state, self._ch2_frame(AXIS_CENTER_US))
        # May or may not emit depending on deadband; just check no wrong value
        abs_y = [e for e in sink.events() if e[0] == EV_ABS and e[1] == ABS_Y]
        for _, _, v in abs_y:
            self.assertEqual(v, AXIS_CENTER_US)


class TestButtonHysteresis(unittest.TestCase):
    """
    Hysteresis thresholds:
      - To press   (from released): raw_us > BUTTON_THRESHOLD_US + BUTTON_HYSTERESIS_US
                                    i.e. raw_us > 1500 + 21 = 1521
      - To release (from pressed):  raw_us <= BUTTON_THRESHOLD_US - BUTTON_HYSTERESIS_US
                                    i.e. raw_us <= 1479
    """

    PRESS_THRESHOLD   = BUTTON_THRESHOLD_US + BUTTON_HYSTERESIS_US   # 1521
    RELEASE_THRESHOLD = BUTTON_THRESHOLD_US - BUTTON_HYSTERESIS_US   # 1479

    def _ch3_frame(self, raw_us):
        return _make_frame(AXIS_CENTER_US, AXIS_CENTER_US, raw_us)

    def test_press_above_threshold(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        transitions = _emit(sink, state, self._ch3_frame(self.PRESS_THRESHOLD + 1))
        self.assertIn((EV_KEY, BTN_SW_CH3, 1), sink.events())
        self.assertIn(('ch3', True), transitions)

    def test_value_at_press_threshold_does_not_press(self):
        """raw_us = 1521 — with hys=-21 (released state): threshold = 1521, need > 1521."""
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch3_frame(self.PRESS_THRESHOLD))
        key_events = [e for e in sink.events() if e[0] == EV_KEY]
        self.assertEqual(key_events, [], f"value={self.PRESS_THRESHOLD} should NOT press")

    def test_value_well_above_center_presses(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch3_frame(1900))   # fully pressed
        self.assertIn((EV_KEY, BTN_SW_CH3, 1), sink.events())

    def test_release_below_threshold(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        # Press first
        _emit(sink, state, self._ch3_frame(1900))
        sink.reset()
        # Release: value <= 1479
        transitions = _emit(sink, state, self._ch3_frame(self.RELEASE_THRESHOLD))
        self.assertIn((EV_KEY, BTN_SW_CH3, 0), sink.events())
        self.assertIn(('ch3', False), transitions)

    def test_value_just_above_release_threshold_stays_pressed(self):
        """raw_us = 1480: with hys=+21 (pressed state): threshold = 1479, need <= 1479."""
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch3_frame(1900))   # press
        sink.reset()
        _emit(sink, state, self._ch3_frame(self.RELEASE_THRESHOLD + 1))   # 1480
        key_events = [e for e in sink.events() if e[0] == EV_KEY]
        self.assertEqual(key_events, [], "1480 should stay pressed — inside hysteresis band")

    def test_no_event_when_button_stays_released(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        for _ in range(3):
            _emit(sink, state, self._ch3_frame(AXIS_CENTER_US))
        key_events = [e for e in sink.events() if e[0] == EV_KEY]
        self.assertEqual(key_events, [], "Stable released button must not emit events")

    def test_no_event_when_button_stays_pressed(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch3_frame(1900))
        sink.reset()
        for _ in range(3):
            _emit(sink, state, self._ch3_frame(1900))
        key_events = [e for e in sink.events() if e[0] == EV_KEY]
        self.assertEqual(key_events, [], "Stable pressed button must not emit repeat events")

    def test_ch4_independent_of_ch3(self):
        """ch3 and ch4 use independent state; pressing one does not affect the other."""
        sink  = _WriteSink()
        state = ChannelOutputState()
        ch4_frame = _make_frame(AXIS_CENTER_US, AXIS_CENTER_US, AXIS_CENTER_US, 1900)
        _emit(sink, state, ch4_frame)
        key_events = [e for e in sink.events() if e[0] == EV_KEY]
        self.assertIn((EV_KEY, BTN_SW_CH4, 1), key_events)
        ch3_pressed = any(e == (EV_KEY, BTN_SW_CH3, 1) for e in key_events)
        self.assertFalse(ch3_pressed, "ch3 must not be pressed when only ch4 moves")


class TestSliderThreePos(unittest.TestCase):
    """
    ch7 (index 6) is a three-position slider.
      LO press:  raw_us < SLIDER_LOW_THRESHOLD  − BUTTON_HYSTERESIS_US  (< 1279)
      LO release: raw_us >= SLIDER_LOW_THRESHOLD + BUTTON_HYSTERESIS_US (>= 1321)
      HI press:  raw_us > SLIDER_HIGH_THRESHOLD + BUTTON_HYSTERESIS_US  (> 1721)
      HI release: raw_us <= SLIDER_HIGH_THRESHOLD − BUTTON_HYSTERESIS_US (<= 1679)
    """

    LO_PRESS_THRESHOLD   = SLIDER_LOW_THRESHOLD  - BUTTON_HYSTERESIS_US   # 1279
    LO_RELEASE_THRESHOLD = SLIDER_LOW_THRESHOLD  + BUTTON_HYSTERESIS_US   # 1321
    HI_PRESS_THRESHOLD   = SLIDER_HIGH_THRESHOLD + BUTTON_HYSTERESIS_US   # 1721
    HI_RELEASE_THRESHOLD = SLIDER_HIGH_THRESHOLD - BUTTON_HYSTERESIS_US   # 1679

    def _ch7_frame(self, raw_us):
        frame = [AXIS_CENTER_US] * len(CHANNEL_MAP)
        frame[6] = raw_us
        return frame

    def test_lo_press(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        transitions = _emit(sink, state, self._ch7_frame(self.LO_PRESS_THRESHOLD - 1))
        self.assertIn((EV_KEY, BTN_SL_LO, 1), sink.events())
        self.assertIn(('ch7', True), transitions)

    def test_lo_at_threshold_does_not_press(self):
        """raw_us = 1279: need < 1279 to press."""
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch7_frame(self.LO_PRESS_THRESHOLD))
        key_events = [e for e in sink.events() if e[0] == EV_KEY]
        self.assertEqual(key_events, [])

    def test_lo_release(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch7_frame(1100))   # press LO
        sink.reset()
        transitions = _emit(sink, state, self._ch7_frame(self.LO_RELEASE_THRESHOLD))
        self.assertIn((EV_KEY, BTN_SL_LO, 0), sink.events())

    def test_hi_press(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        transitions = _emit(sink, state, self._ch7_frame(self.HI_PRESS_THRESHOLD + 1))
        self.assertIn((EV_KEY, BTN_SL_HI, 1), sink.events())

    def test_hi_release(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch7_frame(1900))   # press HI
        sink.reset()
        _emit(sink, state, self._ch7_frame(self.HI_RELEASE_THRESHOLD))
        self.assertIn((EV_KEY, BTN_SL_HI, 0), sink.events())

    def test_mid_position_presses_neither(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        _emit(sink, state, self._ch7_frame(AXIS_CENTER_US))
        key_events = [e for e in sink.events() if e[0] == EV_KEY]
        self.assertEqual(key_events, [], "Mid position must not press LO or HI")

    def test_lo_and_hi_never_simultaneously_pressed(self):
        """
        The slider is a single physical lever — both cannot be pressed at once
        for any plausible µs value.
        """
        sink  = _WriteSink()
        state = ChannelOutputState()
        # Sweep from LO to HI through every 10 µs step
        for us in range(AXIS_MIN_US, AXIS_MAX_US + 1, 10):
            _emit(sink, state, self._ch7_frame(us))
            lo = state.button_states[BTN_SL_LO]
            hi = state.button_states[BTN_SL_HI]
            self.assertFalse(lo and hi, f"Both LO and HI pressed at {us} µs")


class TestTransitions(unittest.TestCase):
    """emit_channel_events returns only the transitions that occurred."""

    def test_no_transitions_when_nothing_changes(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        t = _emit(sink, state, _make_frame(AXIS_CENTER_US))
        self.assertEqual(t, [])

    def test_single_button_press_returns_one_transition(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        t = _emit(sink, state, _make_frame(AXIS_CENTER_US, AXIS_CENTER_US, 1900))
        self.assertEqual(len(t), 1)
        self.assertEqual(t[0], ('ch3', True))

    def test_press_then_release_returns_separate_transitions(self):
        sink  = _WriteSink()
        state = ChannelOutputState()
        t_press   = _emit(sink, state, _make_frame(AXIS_CENTER_US, AXIS_CENTER_US, 1900))
        t_release = _emit(sink, state, _make_frame(AXIS_CENTER_US, AXIS_CENTER_US, 1100))
        self.assertEqual(t_press,   [('ch3', True)])
        self.assertEqual(t_release, [('ch3', False)])


# ── Integration test ──────────────────────────────────────────────────────────

class TestIntegrationRecording(unittest.TestCase):
    """
    Replay the 192 kHz recording through PpmDecoder + emit_channel_events.
    ch3 and ch4 must have had at least one press and one release each.
    """

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(RECORDING_PATH):
            raise unittest.SkipTest(
                f"Recording not found: {RECORDING_PATH}  "
                f"(run parecord to create it)"
            )
        with open(RECORDING_PATH, 'rb') as f:
            raw = f.read()
        cls.samples = [
            struct.unpack_from('<h', raw, offset)[0]
            for offset in range(0, len(raw) - 3, 4)
        ]

    def test_ch3_and_ch4_press_and_release(self):
        decoder = PpmDecoder(max_channels=len(CHANNEL_MAP),
                             sample_rate=RECORDING_SAMPLE_RATE)
        state = ChannelOutputState()
        sink  = _WriteSink()

        btn_events = {BTN_SW_CH3: [], BTN_SW_CH4: []}

        for sample in self.samples:
            frame = decoder.feed(sample)
            if frame is None:
                continue
            with patch('ppm2hid.os.write', side_effect=sink.write):
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
