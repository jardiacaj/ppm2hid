from __future__ import annotations

import fcntl
import os
import struct
import time

from .constants import (
    EV_KEY, EV_ABS, EV_SYN, SYN_REPORT, BUS_USB,
    UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT, UI_DEV_CREATE, UI_DEV_DESTROY,
    UINPUT_MAX_NAME_SIZE, ABS_CNT, _UINPUT_VENDOR, _UINPUT_PRODUCT,
    INPUT_EVENT_STRUCT,
)
from .profile import Profile


# MARK: - uinput device management

def open_uinput_joystick(profile: Profile | None = None) -> int:
    """
    Create a virtual joystick via /dev/uinput.
    Registers every axis and button code declared in the profile's channel map.
    Returns the open file descriptor.
    """
    if profile is None:
        profile = Profile()
    cm       = profile.channel_map
    axis_min = profile.axis_min_us
    axis_max = profile.axis_max_us

    all_abs = set()
    all_btn = set()
    for ch in cm:
        if ch is None:
            continue
        if ch[0] == 'axis':
            all_abs.add(ch[1])
        elif ch[0] == 'button':
            all_btn.add(ch[1])
        elif ch[0] == 'n_pos':
            for btn_code in ch[1]:
                all_btn.add(btn_code)

    if not any(0x120 <= c <= 0x12f for c in all_btn):
        print('Note: no BTN_JOYSTICK code in profile — '
              'device will be evdev-only (no /dev/input/js*)')

    fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)

    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    for btn_code in sorted(all_btn):
        fcntl.ioctl(fd, UI_SET_KEYBIT, btn_code)

    fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
    for abs_code in sorted(all_abs):
        fcntl.ioctl(fd, UI_SET_ABSBIT, abs_code)

    absmax  = [0] * ABS_CNT
    absmin  = [0] * ABS_CNT
    absfuzz = [0] * ABS_CNT
    absflat = [0] * ABS_CNT

    for abs_code in all_abs:
        absmax[abs_code]  = axis_max
        absmin[abs_code]  = axis_min
        absfuzz[abs_code] = 0              # kernel fuzz disabled; software smoothing + deadzone handle filtering
        absflat[abs_code] = 50             # ~±50 µs flat zone snaps stick-at-rest to zero

    raw_name    = (profile.device_name or 'ppm2joy').encode()[:UINPUT_MAX_NAME_SIZE - 1]
    device_name = (raw_name + b'\x00').ljust(UINPUT_MAX_NAME_SIZE, b'\x00')
    # Pack the kernel uinput_user_dev structure (linux/uinput.h):
    #   char  name[UINPUT_MAX_NAME_SIZE]   — device display name
    #   __u16 id.bustype, id.vendor, id.product, id.version
    #   __u32 ff_effects_max               — 0 = no force-feedback
    #   __s32 absmax[ABS_CNT]              — per-axis maximum values
    #   __s32 absmin[ABS_CNT]              — per-axis minimum values
    #   __s32 absfuzz[ABS_CNT]             — kernel-level noise filter (disabled; we do SW smoothing + deadzone)
    #   __s32 absflat[ABS_CNT]             — kernel flat zone at centre
    uinput_user_dev = struct.pack(
        f'{UINPUT_MAX_NAME_SIZE}s HHHH I {ABS_CNT}i {ABS_CNT}i {ABS_CNT}i {ABS_CNT}i',
        device_name, BUS_USB, _UINPUT_VENDOR, _UINPUT_PRODUCT, 1, 0,
        *absmax, *absmin, *absfuzz, *absflat,
    )
    os.write(fd, uinput_user_dev)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    # Give joydev a moment to attach to the new device before sending events.
    time.sleep(0.1)
    # Send initial "released" state for every button so the kernel's state bitmap
    # matches ChannelOutputState's initial state from the first frame onward.
    for btn_code in sorted(all_btn):
        _write_input_event(fd, EV_KEY, btn_code, 0)
    _flush_events(fd)
    return fd


def destroy_uinput_joystick(fd: int) -> None:
    """Destroy the virtual joystick and close the file descriptor."""
    try:
        fcntl.ioctl(fd, UI_DEV_DESTROY)
    except OSError:
        pass
    os.close(fd)


def _write_input_event(fd: int, event_type: int, event_code: int, event_value: int) -> None:
    # Timestamps (tv_sec, tv_usec) are passed as 0; the kernel overwrites them.
    raw = struct.pack(INPUT_EVENT_STRUCT, 0, 0, event_type, event_code, event_value)
    os.write(fd, raw)


def _flush_events(fd: int) -> None:
    # EV_SYN / SYN_REPORT tells the kernel to deliver all buffered events atomically.
    _write_input_event(fd, EV_SYN, SYN_REPORT, 0)


# MARK: - Channel output state and event emission

class ChannelOutputState:
    """Tracks last-emitted values to suppress redundant events and feed the EMA accumulator."""

    def __init__(self, channel_map: list | None = None) -> None:
        if channel_map is None:
            channel_map = Profile().channel_map
        abs_codes = set()
        btn_codes = set()
        for ch in channel_map:
            if ch is None:
                continue
            if ch[0] == 'axis':
                abs_codes.add(ch[1])
            elif ch[0] == 'button':
                btn_codes.add(ch[1])
            elif ch[0] == 'n_pos':
                for btn_code in ch[1]:
                    btn_codes.add(btn_code)
        self.axis_values   = {code: 1_500   for code in abs_codes}  # last-sent value (int µs)
        self.axis_smoothed = {code: 1_500.0 for code in abs_codes}  # EMA accumulator (float µs)
        self.button_states = {code: False   for code in btn_codes}


def reset_joystick_to_neutral(fd: int, state: ChannelOutputState,
                               profile: Profile | None = None) -> None:
    """
    Send axis-centre and button-released events for every mapped control,
    then flush with EV_SYN.  Used to put the virtual joystick in a safe resting
    state when the PPM signal is lost.
    """
    if profile is None:
        profile = Profile()
    center = profile.axis_center_us
    for ch in profile.channel_map:
        if ch is None:
            continue
        if ch[0] == 'axis':
            state.axis_values[ch[1]]   = center
            state.axis_smoothed[ch[1]] = float(center)
            _write_input_event(fd, EV_ABS, ch[1], center)
        elif ch[0] == 'button':
            state.button_states[ch[1]] = False
            _write_input_event(fd, EV_KEY, ch[1], 0)
        elif ch[0] == 'n_pos':
            for btn_code in ch[1]:
                state.button_states[btn_code] = False
                _write_input_event(fd, EV_KEY, btn_code, 0)
    _flush_events(fd)


def emit_channel_events(fd: int, state: ChannelOutputState, ppm_frame: list[int],
                        profile: Profile | None = None) -> list[tuple[int, int, bool]]:
    """
    Convert a decoded PPM frame into uinput events and flush with EV_SYN.

    Axis channels marked with invert=True have their value mirrored around
    the centre point before being emitted.

    Returns a list of (channel_label, pressed) for every button state transition
    that occurred this frame — used by the caller to log button events.
    """
    if profile is None:
        profile = Profile()
    cm             = profile.channel_map
    axis_min       = profile.axis_min_us
    axis_max       = profile.axis_max_us
    axis_center    = profile.axis_center_us
    # Central deadzone: ±deadzone_us around centre snaps to centre.
    # axis_deadzone_pct is % of half-range, so multiply by (max-min)/200.
    deadzone_us    = (axis_max - axis_min) * profile.axis_deadzone_pct // 200
    btn_threshold  = profile.button_threshold_us
    btn_hysteresis = profile.button_hysteresis_us

    transitions = []

    for channel_index, channel_def in enumerate(cm):
        if channel_index >= len(ppm_frame):
            break
        if channel_def is None:
            continue

        raw_us       = ppm_frame[channel_index]
        channel_type = channel_def[0]

        if channel_type == 'axis':
            abs_code = channel_def[1]
            invert   = len(channel_def) > 2 and channel_def[2]
            value_us = (axis_min + axis_max - raw_us) if invert else raw_us
            # EMA (α=0.5) suppresses per-sample jitter that arises from audio
            # quantisation without blocking any part of the axis range.
            # The float accumulator is rounded only when comparing / emitting.
            smoothed_f = 0.5 * value_us + 0.5 * state.axis_smoothed[abs_code]
            state.axis_smoothed[abs_code] = smoothed_f
            smoothed = round(smoothed_f)
            if deadzone_us > 0 and abs(smoothed - axis_center) <= deadzone_us:
                smoothed = axis_center
            if smoothed != state.axis_values[abs_code]:
                state.axis_values[abs_code] = smoothed
                _write_input_event(fd, EV_ABS, abs_code, smoothed)

        elif channel_type == 'button':
            btn_code = channel_def[1]
            # Hysteresis: raise threshold to press, lower threshold to release.
            # Prevents 1-sample jitter near 1500 µs from toggling the button.
            hys = btn_hysteresis if state.button_states[btn_code] else -btn_hysteresis
            pressed = raw_us > btn_threshold - hys
            if pressed != state.button_states[btn_code]:
                state.button_states[btn_code] = pressed
                _write_input_event(fd, EV_KEY, btn_code, int(pressed))
                transitions.append((channel_index + 1, btn_code, pressed))

        elif channel_type == 'n_pos':
            codes, thresholds = channel_def[1], channel_def[2]
            for btn_code, thresh_us in zip(codes, thresholds):
                hys = btn_hysteresis if state.button_states[btn_code] else -btn_hysteresis
                pressed = raw_us > thresh_us - hys
                if pressed != state.button_states[btn_code]:
                    state.button_states[btn_code] = pressed
                    _write_input_event(fd, EV_KEY, btn_code, int(pressed))
                    transitions.append((channel_index + 1, btn_code, pressed))

    # Always send EV_SYN – ensures button state reaches readers even when nothing changed
    _flush_events(fd)
    return transitions
