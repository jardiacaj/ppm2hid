#!/usr/bin/env python3
"""
test_display.py – unit tests for the pure display helper functions.

Tests _axis_bar, _build_monitor_line, and _render_oscilloscope.
No hardware or test recordings required.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from ppm2hid import (
    _axis_bar,
    _build_monitor_line,
    _render_oscilloscope,
    ChannelOutputState,
    Profile,
    BTN_SL_LO, BTN_SL_HI,
    BTN_SW_CH3,
)

_PROFILE = Profile()


# ── Helpers ──────────────────────────��────────────────────��───────────────────

def _full_frame(*overrides: int) -> list[int]:
    """
    Build an 8-channel PPM frame with all channels at _PROFILE.axis_center_us,
    except ch7 (index 6) which defaults to 1100 µs (slider low, no buttons).
    Pass positional overrides for channels 0–N.
    """
    frame = [_PROFILE.axis_center_us] * len(_PROFILE.channel_map)
    frame[6] = 1100   # slider default: physical low = no buttons
    for i, v in enumerate(overrides):
        frame[i] = v
    return frame


# ── _axis_bar ─────────────────────────���──────────────────────────────────────

class TestAxisBar(unittest.TestCase):

    def test_min_value_all_empty(self) -> None:
        bar = _axis_bar(_PROFILE.axis_min_us)
        self.assertEqual(bar, '[░░░░░░]')

    def test_max_value_all_filled(self) -> None:
        bar = _axis_bar(_PROFILE.axis_max_us)
        self.assertEqual(bar, '[██████]')

    def test_centre_value_half_filled(self) -> None:
        # (1500-1100)/(1900-1100) = 0.5 → int(0.5 * 6) = 3 filled
        bar = _axis_bar(_PROFILE.axis_center_us)
        self.assertEqual(bar, '[███░░░]')

    def test_custom_width(self) -> None:
        bar = _axis_bar(_PROFILE.axis_max_us, width=4)
        self.assertEqual(bar, '[████]')

    def test_below_min_clipped_to_empty(self) -> None:
        bar = _axis_bar(_PROFILE.axis_min_us - 100)
        self.assertEqual(bar, '[░░░░░░]')

    def test_above_max_clipped_to_full(self) -> None:
        bar = _axis_bar(_PROFILE.axis_max_us + 100)
        self.assertEqual(bar, '[██████]')

    def test_output_has_fixed_width(self) -> None:
        for us in (_PROFILE.axis_min_us, _PROFILE.axis_center_us, _PROFILE.axis_max_us, 1300, 1700):
            bar = _axis_bar(us, width=6)
            # '[' + 6 block chars + ']' = 8 chars
            self.assertEqual(len(bar), 8, f'Wrong length for {us} µs: {bar!r}')


# ── _build_monitor_line ──────────────────────────────────────────────────��────

class TestBuildMonitorLine(unittest.TestCase):

    def test_axis_at_centre_shows_half_bar(self) -> None:
        line = _build_monitor_line(_full_frame())
        # ch1 (STR) is at _PROFILE.axis_center_us, no inversion
        self.assertIn('STR:[███░░░]', line)

    def test_inverted_axis_mirrors_value(self) -> None:
        # ch2 (THR, index 1) is inverted: _PROFILE.axis_min_us raw → _PROFILE.axis_max_us display
        frame = _full_frame(_PROFILE.axis_center_us, _PROFILE.axis_min_us)   # ch2 at physical min
        line  = _build_monitor_line(frame)
        # display_us = _PROFILE.axis_min_us + _PROFILE.axis_max_us - _PROFILE.axis_min_us = _PROFILE.axis_max_us → all filled
        self.assertIn('THR:[██████]', line)

    def test_button_released_without_state(self) -> None:
        # ch3 (index 2) below threshold → □
        frame = _full_frame(_PROFILE.axis_center_us, _PROFILE.axis_center_us, _PROFILE.axis_min_us)
        line  = _build_monitor_line(frame)
        self.assertIn(' c3:□', line)

    def test_button_pressed_without_state(self) -> None:
        # ch3 (index 2) above threshold → ■
        frame = _full_frame(_PROFILE.axis_center_us, _PROFILE.axis_center_us, _PROFILE.axis_max_us)
        line  = _build_monitor_line(frame)
        self.assertIn(' c3:■', line)

    def test_button_pressed_with_state(self) -> None:
        state = ChannelOutputState()
        state.button_states[BTN_SW_CH3] = True
        frame = _full_frame()   # raw value doesn't matter when state is provided
        line  = _build_monitor_line(frame, state=state)
        self.assertIn(' c3:■', line)

    def test_button_released_with_state(self) -> None:
        state = ChannelOutputState()
        state.button_states[BTN_SW_CH3] = False
        frame = _full_frame()
        line  = _build_monitor_line(frame, state=state)
        self.assertIn(' c3:□', line)

    def test_slider_low_without_state(self) -> None:
        frame = _full_frame()   # ch7 = 1100 µs (≤ _PROFILE.slider_low_threshold_us=1300)
        line  = _build_monitor_line(frame)
        self.assertIn(' c7:LOW', line)

    def test_slider_mid_without_state(self) -> None:
        frame    = _full_frame()
        frame[6] = _PROFILE.slider_low_threshold_us + 1   # just above low threshold → MID
        line     = _build_monitor_line(frame)
        self.assertIn(' c7:MID', line)

    def test_slider_hi_without_state(self) -> None:
        frame    = _full_frame()
        frame[6] = _PROFILE.slider_high_threshold_us + 1   # above high threshold → HI
        line     = _build_monitor_line(frame)
        self.assertIn(' c7:HI ', line)

    def test_slider_with_state_low(self) -> None:
        state = ChannelOutputState()
        state.button_states[BTN_SL_LO] = False
        state.button_states[BTN_SL_HI] = False
        frame = _full_frame()
        line  = _build_monitor_line(frame, state=state)
        self.assertIn(' c7:LOW', line)

    def test_slider_with_state_mid(self) -> None:
        state = ChannelOutputState()
        state.button_states[BTN_SL_LO] = True
        state.button_states[BTN_SL_HI] = False
        frame = _full_frame()
        line  = _build_monitor_line(frame, state=state)
        self.assertIn(' c7:MID', line)

    def test_slider_with_state_hi(self) -> None:
        state = ChannelOutputState()
        state.button_states[BTN_SL_LO] = True
        state.button_states[BTN_SL_HI] = True
        frame = _full_frame()
        line  = _build_monitor_line(frame, state=state)
        self.assertIn(' c7:HI ', line)

    def test_hz_tag_absent_when_zero(self) -> None:
        line = _build_monitor_line(_full_frame(), hz=0.0)
        self.assertNotIn('Hz', line)

    def test_hz_tag_present_when_nonzero(self) -> None:
        line = _build_monitor_line(_full_frame(), hz=50.3)
        self.assertIn('[50Hz]', line)

    def test_short_frame_shows_placeholder_for_missing_axis(self) -> None:
        # Frame with only 2 channels — ch3 onwards should show placeholder
        frame = _full_frame()[:2]
        line  = _build_monitor_line(frame)
        self.assertIn(' c3:?', line)

    def test_short_frame_shows_placeholder_for_missing_slider(self) -> None:
        frame = _full_frame()[:2]
        line  = _build_monitor_line(frame)
        self.assertIn(' c7: -- ', line)


# ── _render_oscilloscope ─────────────────────────────────────────────────────

class TestRenderOscilloscope(unittest.TestCase):

    HEIGHT = 7
    WIDTH  = 72

    def test_empty_samples_returns_correct_row_count(self) -> None:
        rows = _render_oscilloscope([], height=self.HEIGHT, width=self.WIDTH)
        self.assertEqual(len(rows), self.HEIGHT)

    def test_empty_samples_first_row_has_message(self) -> None:
        rows = _render_oscilloscope([], height=self.HEIGHT, width=self.WIDTH)
        self.assertIn('no samples', rows[0])

    def test_single_max_sample_fills_top_row(self) -> None:
        rows = _render_oscilloscope([32767], height=self.HEIGHT, width=self.WIDTH)
        # Max amplitude maps to row 0 (top)
        self.assertIn('█', rows[0])

    def test_single_min_sample_fills_bottom_row(self) -> None:
        rows = _render_oscilloscope([-32768], height=self.HEIGHT, width=self.WIDTH)
        # Min amplitude maps to last row (bottom)
        self.assertIn('█', rows[-1])

    def test_returns_correct_number_of_rows(self) -> None:
        for h in (5, 7, 10):
            rows = _render_oscilloscope([0] * 100, height=h, width=self.WIDTH)
            self.assertEqual(len(rows), h, f'Wrong row count for height={h}')

    def test_each_row_has_correct_width(self) -> None:
        rows = _render_oscilloscope([0] * 200, height=self.HEIGHT, width=self.WIDTH)
        for i, row in enumerate(rows):
            self.assertEqual(len(row), self.WIDTH,
                             f'Row {i} has wrong width: {len(row)}')

    def test_threshold_row_contains_dots_when_signal_above_threshold(self) -> None:
        # All samples at maximum (+32767) with threshold=0.
        # The signal only fills row 0 (top). The threshold row (somewhere in the
        # middle) receives no signal, so it is rendered as '·' (threshold marker).
        samples = [32767] * self.WIDTH
        rows    = _render_oscilloscope(
            samples, threshold=0, height=self.HEIGHT, width=self.WIDTH
        )
        # At least one row (the threshold row, which is not row 0) must have '·'
        self.assertTrue(any('·' in row for row in rows[1:]))

    def test_constant_high_signal_fills_top_row(self) -> None:
        samples = [32767] * self.WIDTH
        rows    = _render_oscilloscope(samples, height=self.HEIGHT, width=self.WIDTH)
        self.assertTrue(all(c == '█' for c in rows[0]))

    def test_constant_low_signal_fills_bottom_row(self) -> None:
        samples = [-32768] * self.WIDTH
        rows    = _render_oscilloscope(samples, height=self.HEIGHT, width=self.WIDTH)
        self.assertTrue(all(c == '█' for c in rows[-1]))


if __name__ == '__main__':
    unittest.main()
