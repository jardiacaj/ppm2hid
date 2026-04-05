#!/usr/bin/env python3
"""
test_profile.py – unit tests for load_profile() and the Profile class.

No hardware or audio recordings required.
"""

import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from ppm2hid import load_profile, Profile, ChannelOutputState, _resolve_code

# Path to the bundled example profile
ABSIMA_PROFILE = os.path.join(os.path.dirname(__file__), 'profiles', 'absima_cr10p.toml')


def _write_toml(content):
    """Write *content* to a temporary file and return its path."""
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False)
    f.write(content)
    f.close()
    return f.name


class TestResolvCode(unittest.TestCase):

    def test_known_string_resolves(self):
        self.assertEqual(_resolve_code('BTN_SOUTH'), 0x130)

    def test_integer_passthrough(self):
        self.assertEqual(_resolve_code(0x130), 0x130)

    def test_unknown_string_raises(self):
        with self.assertRaises(ValueError):
            _resolve_code('BTN_UNKNOWN')

    def test_alias_resolves(self):
        # BTN_A is an alias for BTN_SOUTH
        self.assertEqual(_resolve_code('BTN_A'), _resolve_code('BTN_SOUTH'))


class TestDefaultProfile(unittest.TestCase):

    def test_default_profile_calibration(self):
        p = Profile()
        self.assertEqual(p.axis_min_us,              1_100)
        self.assertEqual(p.axis_max_us,              1_900)
        self.assertEqual(p.axis_center_us,           1_500)
        self.assertEqual(p.axis_deadband_us,         42)
        self.assertEqual(p.button_threshold_us,      1_500)
        self.assertEqual(p.button_hysteresis_us,     21)
        self.assertEqual(p.slider_low_threshold_us,  1_300)
        self.assertEqual(p.slider_high_threshold_us, 1_700)
        self.assertEqual(len(p.channel_map),         8)


class TestLoadProfileAbsima(unittest.TestCase):
    """Tests against the bundled absima_cr10p.toml profile."""

    @classmethod
    def setUpClass(cls):
        cls.profile = load_profile(ABSIMA_PROFILE)

    def test_device_name(self):
        self.assertIn('Absima', self.profile.device_name)

    def test_axis_min_us(self):
        self.assertEqual(self.profile.axis_min_us, 1100)

    def test_channel_count(self):
        self.assertEqual(len(self.profile.channel_map), 8)

    def test_no_none_entries(self):
        # All 8 channels are mapped in this profile
        self.assertTrue(all(ch is not None for ch in self.profile.channel_map))

    def test_ch1_is_axis(self):
        ch = self.profile.channel_map[0]
        self.assertEqual(ch[0], 'axis')
        self.assertEqual(ch[1], 0x00)   # ABS_X

    def test_ch2_is_axis(self):
        ch = self.profile.channel_map[1]
        self.assertEqual(ch[0], 'axis')
        self.assertEqual(ch[1], 0x01)   # ABS_Y
        self.assertEqual(len(ch), 2)    # not inverted

    def test_ch7_is_three_pos(self):
        ch = self.profile.channel_map[6]
        self.assertEqual(ch[0], 'three_pos')
        self.assertEqual(ch[1], 0x136)  # BTN_TL
        self.assertEqual(ch[2], 0x137)  # BTN_TR

    def test_monitor_labels_count(self):
        self.assertEqual(len(self.profile.monitor_labels), 8)

    def test_ch1_label(self):
        self.assertEqual(self.profile.monitor_labels[0], 'STR')

    def test_channel_map_from_profile_initialises_state(self):
        state = ChannelOutputState(self.profile.channel_map)
        # ABS_X (0x00) should be in axis_values
        self.assertIn(0x00, state.axis_values)
        # BTN_TL (0x136) should be in button_states
        self.assertIn(0x136, state.button_states)


class TestLoadProfileValidation(unittest.TestCase):

    def test_unknown_code_raises(self):
        toml = _write_toml(
            '[[channel]]\nindex = 1\ntype = "button"\ncode = "BTN_UNKNOWN"\n'
        )
        try:
            with self.assertRaises(ValueError):
                load_profile(toml)
        finally:
            os.unlink(toml)

    def test_unknown_channel_type_raises(self):
        toml = _write_toml(
            '[[channel]]\nindex = 1\ntype = "gas_brake"\ncode = "ABS_GAS"\n'
        )
        try:
            with self.assertRaises(ValueError):
                load_profile(toml)
        finally:
            os.unlink(toml)

    def test_raw_integer_code_accepted(self):
        toml = _write_toml(
            '[[channel]]\nindex = 1\ntype = "button"\ncode = 304\n'
        )
        try:
            p = load_profile(toml)
            self.assertEqual(p.channel_map[0], ('button', 304))
        finally:
            os.unlink(toml)

    def test_missing_index_raises(self):
        toml = _write_toml(
            '[[channel]]\ntype = "button"\ncode = "BTN_SOUTH"\n'
        )
        try:
            with self.assertRaises(ValueError):
                load_profile(toml)
        finally:
            os.unlink(toml)

    def test_duplicate_index_raises(self):
        toml = _write_toml(
            '[[channel]]\nindex = 1\ntype = "button"\ncode = "BTN_SOUTH"\n'
            '[[channel]]\nindex = 1\ntype = "button"\ncode = "BTN_EAST"\n'
        )
        try:
            with self.assertRaises(ValueError):
                load_profile(toml)
        finally:
            os.unlink(toml)

    def test_channel_ordering_by_index(self):
        # Define ch2 first, ch1 second — channel_map should be in index order
        toml = _write_toml(
            '[[channel]]\nindex = 2\ntype = "button"\ncode = "BTN_EAST"\n'
            '[[channel]]\nindex = 1\ntype = "button"\ncode = "BTN_SOUTH"\n'
        )
        try:
            p = load_profile(toml)
            self.assertEqual(p.channel_map[0], ('button', 0x130))  # index=1 → BTN_SOUTH
            self.assertEqual(p.channel_map[1], ('button', 0x131))  # index=2 → BTN_EAST
        finally:
            os.unlink(toml)

    def test_gap_in_index_leaves_none(self):
        # Indices 1 and 3 defined, index 2 absent
        toml = _write_toml(
            '[[channel]]\nindex = 1\ntype = "button"\ncode = "BTN_SOUTH"\n'
            '[[channel]]\nindex = 3\ntype = "button"\ncode = "BTN_NORTH"\n'
        )
        try:
            p = load_profile(toml)
            self.assertEqual(len(p.channel_map), 3)
            self.assertIsNone(p.channel_map[1])
        finally:
            os.unlink(toml)

    def test_missing_channel_code_raises(self):
        toml = _write_toml(
            '[[channel]]\nindex = 1\ntype = "button"\n'
        )
        try:
            with self.assertRaises((KeyError, ValueError)):
                load_profile(toml)
        finally:
            os.unlink(toml)

    def test_signal_section_overrides_defaults(self):
        toml = _write_toml('[signal]\naxis_min_us = 900\naxis_max_us = 2100\n')
        try:
            p = load_profile(toml)
            self.assertEqual(p.axis_min_us, 900)
            self.assertEqual(p.axis_max_us, 2100)
            # Unset fields keep defaults
            self.assertEqual(p.axis_center_us, Profile().axis_center_us)
        finally:
            os.unlink(toml)


if __name__ == '__main__':
    unittest.main()
