"""
ppm2hid – RC transmitter PPM audio input → Linux virtual joystick
"""

from __future__ import annotations

__version__ = '0.1.0'

# Re-export the full public API so that existing imports of the form
#   from ppm2hid import PpmDecoder, Profile, …
# continue to work without change after the move to a package.

from .constants import (
    EV_SYN, EV_KEY, EV_ABS, SYN_REPORT, BUS_USB,
    ABS_X, ABS_Y, ABS_RX, ABS_RY,
    BTN_SW_CH3, BTN_SW_CH4, BTN_SL_LO, BTN_SL_HI, BTN_SW_CH8,
    BTN_SW_CH9, BTN_SW_CH10,
    INPUT_EVENT_STRUCT,
    DEFAULT_AUDIO_SAMPLE_RATE, DEFAULT_AUDIO_THRESHOLD, DEFAULT_AUDIO_HYSTERESIS,
)
from .decoder import PpmDecoder
from .profile import Profile, load_profile, _resolve_code
from .uinput import (
    open_uinput_joystick, destroy_uinput_joystick,
    ChannelOutputState, reset_joystick_to_neutral, emit_channel_events,
)
from .alsa import switch_alsa_input_to_line_in, restore_alsa_input_sources
from .audio import (
    open_audio_file, start_audio_capture,
    probe_source_for_ppm, probe_file_for_ppm, discover_ppm_source,
)
from .display import TerminalUI, _axis_bar, _build_monitor_line, _render_oscilloscope
from .cli import main

__all__ = [
    '__version__',
    # constants
    'EV_SYN', 'EV_KEY', 'EV_ABS', 'SYN_REPORT', 'BUS_USB',
    'ABS_X', 'ABS_Y', 'ABS_RX', 'ABS_RY',
    'BTN_SW_CH3', 'BTN_SW_CH4', 'BTN_SL_LO', 'BTN_SL_HI', 'BTN_SW_CH8',
    'BTN_SW_CH9', 'BTN_SW_CH10',
    'INPUT_EVENT_STRUCT',
    'DEFAULT_AUDIO_SAMPLE_RATE', 'DEFAULT_AUDIO_THRESHOLD', 'DEFAULT_AUDIO_HYSTERESIS',
    # decoder
    'PpmDecoder',
    # profile
    'Profile', 'load_profile', '_resolve_code',
    # uinput
    'open_uinput_joystick', 'destroy_uinput_joystick',
    'ChannelOutputState', 'reset_joystick_to_neutral', 'emit_channel_events',
    # alsa
    'switch_alsa_input_to_line_in', 'restore_alsa_input_sources',
    # audio
    'open_audio_file', 'start_audio_capture',
    'probe_source_for_ppm', 'probe_file_for_ppm', 'discover_ppm_source',
    # display
    'TerminalUI', '_axis_bar', '_build_monitor_line', '_render_oscilloscope',
    # cli
    'main',
]
