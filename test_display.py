#!/usr/bin/env python3
"""
test_display.py – unit tests for the pure display helper functions.

Tests _axis_bar, _build_monitor_line, and _render_oscilloscope.
No hardware or test recordings required.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from ppm2hid import (
    _axis_bar,
    _build_monitor_line,
    _render_oscilloscope,
    ChannelOutputState,
    CHANNEL_MAP,
    AXIS_MIN_US, AXIS_MAX_US, AXIS_CENTER_US,
    BUTTON_THRESHOLD_US,
    SLIDER_LOW_THRESHOLD, SLIDER_HIGH_THRESHOLD,
    BTN_SL_LO, BTN_SL_HI,
    BTN_SW_CH3,
)


# ── Helpers ──────────────────────────��────────────────────��───────────────────

def _full_frame(*overrides):
    """
    Build an 8-channel PPM frame with all channels at AXIS_CENTER_US,
    except ch7 (index 6) which defaults to 1100 µs (slider low, no buttons).
    Pass positional overrides for channels 0–N.
    """
    frame = [AXIS_CENTER_US] * len(CHANNEL_MAP)
    frame[6] = 1100   # slider default: physical low = no buttons
    for i, v in enumerate(overrides):
        frame[i] = v
    return frame


# ── _axis_bar ─────────────────────────���──────────────────────────────────────

class TestAxisBar(unittest.TestCase):

    def test_min_value_all_empty(self):
        bar = _axis_bar(AXIS_MIN_US)
        self.assertEqual(bar, '[░░░░░░]')

    def test_max_value_all_filled(self):
        bar = _axis_bar(AXIS_MAX_US)
        self.assertEqual(bar, '[██████]')

    def test_centre_value_half_filled(self):
        # (1500-1100)/(1900-1100) = 0.5 → int(0.5 * 6) = 3 filled
        bar = _axis_bar(AXIS_CENTER_US)
        self.assertEqual(bar, '[███░░░]')

    def test_custom_width(self):
        bar = _axis_bar(AXIS_MAX_US, width=4)
        self.assertEqual(bar, '[████]')

    def test_below_min_clipped_to_empty(self):
        bar = _axis_bar(AXIS_MIN_US - 100)
        self.assertEqual(bar, '[░░░░░░]')

    def test_above_max_clipped_to_full(self):
        bar = _axis_bar(AXIS_MAX_US + 100)
        self.assertEqual(bar, '[██████]')

    def test_output_has_fixed_width(self):
        for us in (AXIS_MIN_US, AXIS_CENTER_US, AXIS_MAX_US, 1300, 1700):
            bar = _axis_bar(us, width=6)
            # '[' + 6 block chars + ']' = 8 chars
            self.assertEqual(len(bar), 8, f'Wrong length for {us} µs: {bar!r}')


# ── _build_monitor_line ──────────────────────────────────────────────────��────

class TestBuildMonitorLine(unittest.TestCase):

    def test_axis_at_centre_shows_half_bar(self):
        line = _build_monitor_line(_full_frame())
        # ch1 (STR) is at AXIS_CENTER_US, no inversion
        self.assertIn('STR:[███░░░]', line)

    def test_inverted_axis_mirrors_value(self):
        # ch2 (THR, index 1) is inverted: AXIS_MIN_US raw → AXIS_MAX_US display
        frame = _full_frame(AXIS_CENTER_US, AXIS_MIN_US)   # ch2 at physical min
        line  = _build_monitor_line(frame)
        # display_us = AXIS_MIN_US + AXIS_MAX_US - AXIS_MIN_US = AXIS_MAX_US → all filled
        self.assertIn('THR:[██████]', line)

    def test_button_released_without_state(self):
        # ch3 (index 2) below threshold → □
        frame = _full_frame(AXIS_CENTER_US, AXIS_CENTER_US, AXIS_MIN_US)
        line  = _build_monitor_line(frame)
        self.assertIn(' c3:□', line)

    def test_button_pressed_without_state(self):
        # ch3 (index 2) above threshold → ■
        frame = _full_frame(AXIS_CENTER_US, AXIS_CENTER_US, AXIS_MAX_US)
        line  = _build_monitor_line(frame)
        self.assertIn(' c3:■', line)

    def test_button_pressed_with_state(self):
        state = ChannelOutputState()
        state.button_states[BTN_SW_CH3] = True
        frame = _full_frame()   # raw value doesn't matter when state is provided
        line  = _build_monitor_line(frame, state=state)
        self.assertIn(' c3:■', line)

    def test_button_released_with_state(self):
        state = ChannelOutputState()
        state.button_states[BTN_SW_CH3] = False
        frame = _full_frame()
        line  = _build_monitor_line(frame, state=state)
        self.assertIn(' c3:□', line)

    def test_slider_low_without_state(self):
        frame = _full_frame()   # ch7 = 1100 µs (≤ SLIDER_LOW_THRESHOLD=1300)
        line  = _build_monitor_line(frame)
        self.assertIn(' c7:LOW', line)

    def test_slider_mid_without_state(self):
        frame    = _full_frame()
        frame[6] = SLIDER_LOW_THRESHOLD + 1   # just above low threshold → MID
        line     = _build_monitor_line(frame)
        self.assertIn(' c7:MID', line)

    def test_slider_hi_without_state(self):
        frame    = _full_frame()
        frame[6] = SLIDER_HIGH_THRESHOLD + 1   # above high threshold → HI
        line     = _build_monitor_line(frame)
        self.assertIn(' c7:HI ', line)

    def test_slider_with_state_low(self):
        state = ChannelOutputState()
        state.button_states[BTN_SL_LO] = False
        state.button_states[BTN_SL_HI] = False
        frame = _full_frame()
        line  = _build_monitor_line(frame, state=state)
        self.assertIn(' c7:LOW', line)

    def test_slider_with_state_mid(self):
        state = ChannelOutputState()
        state.button_states[BTN_SL_LO] = True
        state.button_states[BTN_SL_HI] = False
        frame = _full_frame()
        line  = _build_monitor_line(frame, state=state)
        self.assertIn(' c7:MID', line)

    def test_slider_with_state_hi(self):
        state = ChannelOutputState()
        state.button_states[BTN_SL_LO] = True
        state.button_states[BTN_SL_HI] = True
        frame = _full_frame()
        line  = _build_monitor_line(frame, state=state)
        self.assertIn(' c7:HI ', line)

    def test_hz_tag_absent_when_zero(self):
        line = _build_monitor_line(_full_frame(), hz=0.0)
        self.assertNotIn('Hz', line)

    def test_hz_tag_present_when_nonzero(self):
        line = _build_monitor_line(_full_frame(), hz=50.3)
        self.assertIn('[50Hz]', line)

    def test_short_frame_shows_placeholder_for_missing_axis(self):
        # Frame with only 2 channels — ch3 onwards should show placeholder
        frame = _full_frame()[:2]
        line  = _build_monitor_line(frame)
        self.assertIn(' c3:?', line)

    def test_short_frame_shows_placeholder_for_missing_slider(self):
        frame = _full_frame()[:2]
        line  = _build_monitor_line(frame)
        self.assertIn(' c7: -- ', line)


# ── _render_oscilloscope ─────────────────────────────────────────────────────

class TestRenderOscilloscope(unittest.TestCase):

    HEIGHT = 7
    WIDTH  = 72

    def test_empty_samples_returns_correct_row_count(self):
        rows = _render_oscilloscope([], height=self.HEIGHT, width=self.WIDTH)
        self.assertEqual(len(rows), self.HEIGHT)

    def test_empty_samples_first_row_has_message(self):
        rows = _render_oscilloscope([], height=self.HEIGHT, width=self.WIDTH)
        self.assertIn('no samples', rows[0])

    def test_single_max_sample_fills_top_row(self):
        rows = _render_oscilloscope([32767], height=self.HEIGHT, width=self.WIDTH)
        # Max amplitude maps to row 0 (top)
        self.assertIn('█', rows[0])

    def test_single_min_sample_fills_bottom_row(self):
        rows = _render_oscilloscope([-32768], height=self.HEIGHT, width=self.WIDTH)
        # Min amplitude maps to last row (bottom)
        self.assertIn('█', rows[-1])

    def test_returns_correct_number_of_rows(self):
        for h in (5, 7, 10):
            rows = _render_oscilloscope([0] * 100, height=h, width=self.WIDTH)
            self.assertEqual(len(rows), h, f'Wrong row count for height={h}')

    def test_each_row_has_correct_width(self):
        rows = _render_oscilloscope([0] * 200, height=self.HEIGHT, width=self.WIDTH)
        for i, row in enumerate(rows):
            self.assertEqual(len(row), self.WIDTH,
                             f'Row {i} has wrong width: {len(row)}')

    def test_threshold_row_contains_dots_when_signal_above_threshold(self):
        # All samples at maximum (+32767) with threshold=0.
        # The signal only fills row 0 (top). The threshold row (somewhere in the
        # middle) receives no signal, so it is rendered as '·' (threshold marker).
        samples = [32767] * self.WIDTH
        rows    = _render_oscilloscope(
            samples, threshold=0, height=self.HEIGHT, width=self.WIDTH
        )
        # At least one row (the threshold row, which is not row 0) must have '·'
        self.assertTrue(any('·' in row for row in rows[1:]))

    def test_constant_high_signal_fills_top_row(self):
        samples = [32767] * self.WIDTH
        rows    = _render_oscilloscope(samples, height=self.HEIGHT, width=self.WIDTH)
        self.assertTrue(all(c == '█' for c in rows[0]))

    def test_constant_low_signal_fills_bottom_row(self):
        samples = [-32768] * self.WIDTH
        rows    = _render_oscilloscope(samples, height=self.HEIGHT, width=self.WIDTH)
        self.assertTrue(all(c == '█' for c in rows[-1]))


if __name__ == '__main__':
    unittest.main()
