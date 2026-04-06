from __future__ import annotations

# MARK: - Linux input subsystem constants

# Event types
EV_SYN = 0   # synchronisation marker
EV_KEY = 1   # key / button events
EV_ABS = 3   # absolute axis events

SYN_REPORT = 0   # flush a frame of events to userspace

BUS_USB = 0x03   # pretend to be a USB device (required by uinput)

# Axis codes used in this mapping.
# Codes with lower numbers appear first in joydev (/dev/input/js*), so the
# primary controls (steering, throttle) are assigned the lowest codes.
ABS_X  = 0   # ch1 – steering  (joystick axis 0)
ABS_Y  = 1   # ch2 – throttle  (joystick axis 1)
ABS_RX = 3   # ch5 – aux axis  (joystick axis 2)
ABS_RY = 4   # ch6 – aux axis  (joystick axis 3)

# Joystick button codes (linux/input-event-codes.h, BTN_JOYSTICK range 0x120–0x12f).
# joydev requires at least one code in this range to create /dev/input/js*.
# The kernel assigns "flight-stick" names to the range (TRIGGER, THUMB, TOP, PINKIE…);
# those names have no relation to this RC transmitter's buttons.
# Code order determines /dev/input/js* button numbering: 0x120 → button 0, etc.
BTN_SW_CH3  = 0x120   # ch3  momentary switch → joystick button 0
BTN_SW_CH4  = 0x121   # ch4  momentary switch → joystick button 1
BTN_SL_LO   = 0x122   # ch7  slider low  → joystick button 2
BTN_SL_HI   = 0x123   # ch7  slider high → joystick button 3
BTN_SW_CH8  = 0x124   # ch8  momentary switch → joystick button 4
BTN_SW_CH9  = 0x125   # ch9  (reserved – not yet in the default channel map)
BTN_SW_CH10 = 0x126   # ch10 (reserved – not yet in the default channel map)

# uinput ioctl numbers (linux/uinput.h).
# Before UI_DEV_CREATE the device is configured with a series of ioctls:
#   UI_SET_EVBIT  – declare which event types the device will produce (EV_KEY, EV_ABS, …)
#   UI_SET_KEYBIT – for each button/key code, announce it under EV_KEY
#   UI_SET_ABSBIT – for each axis code, announce it under EV_ABS
# UI_DEV_CREATE then materialises the device node; UI_DEV_DESTROY tears it down.
UI_SET_EVBIT   = 0x40045564
UI_SET_KEYBIT  = 0x40045565
UI_SET_ABSBIT  = 0x40045567
UI_DEV_CREATE  = 0x5501
UI_DEV_DESTROY = 0x5502

UINPUT_MAX_NAME_SIZE = 80
ABS_CNT = 64   # kernel ABS_CNT — total number of absolute axis slots in uinput_user_dev

# USB vendor / product IDs reported by the virtual uinput joystick device.
# VID 0x1209 is the pid.codes community open-source vendor ID (not a real USB vendor).
_UINPUT_VENDOR  = 0x1209
_UINPUT_PRODUCT = 0x2641

# input_event struct layout on 64-bit Linux (linux/input.h):
#   timeval:  tv_sec (q=int64) + tv_usec (q=int64) = 16 bytes  [kernel fills these in]
#   type:     H=uint16  — EV_KEY, EV_ABS, EV_SYN, …
#   code:     H=uint16  — BTN_*, ABS_*, SYN_REPORT, …
#   value:    i=int32   — axis position, button 0/1, or SYN_REPORT 0
INPUT_EVENT_STRUCT = 'qqHHi'

# MARK: - Defaults
#
# All constants below are defaults only.  They can be overridden at runtime:
#
#   DEFAULT_AUDIO_SAMPLE_RATE   --rate N        or read from the WAV header
#   DEFAULT_AUDIO_THRESHOLD     --threshold N
#   DEFAULT_AUDIO_HYSTERESIS    --hysteresis N
#   AXIS_*  BUTTON_*  SLIDER_*         profile [signal] section (--config)

DEFAULT_AUDIO_SAMPLE_RATE = 48_000   # Hz

DEFAULT_AUDIO_THRESHOLD  = 0       # int16 zero-crossing — PPM signal swings ±32768 so this
                           # works at all sample rates.  Raise if your audio path has
                           # a DC offset.
DEFAULT_AUDIO_HYSTERESIS = 4_000   # Schmitt trigger dead zone (±4000 ≈ ±12 % of int16 range).
                           # Noise below this amplitude is ignored.  A full-swing PPM
                           # signal always exceeds it, so no valid pulses are lost.
