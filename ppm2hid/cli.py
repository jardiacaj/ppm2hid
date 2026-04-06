"""
ppm2hid – RC transmitter PPM audio input → Linux virtual joystick

Captures a PPM signal from a Line In jack via PipeWire/PulseAudio and
exposes a /dev/input/js* virtual joystick using the Linux uinput subsystem.

PPM signal format (positive/high-active — the most common convention):
  HIGH pulse  = channel value  (nominally 1100–1900 µs, centre 1500 µs)
  LOW pulse   = inter-channel separator (~400 µs, constant)
  SYNC pulse  = long HIGH (>3 ms), marks the end/start of each frame

Both positive and inverted (LOW-active) signals are detected automatically.
Channel mapping and signal timing are configured via a TOML profile (--config);
the built-in defaults match a typical 8-channel RC transmitter.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import struct
import subprocess
import sys
import time
from typing import IO, Any

from . import __version__
from .alsa import switch_alsa_input_to_line_in, restore_alsa_input_sources
from .audio import (
    open_audio_file, _get_file_sample_rate,
    start_audio_capture, discover_ppm_source,
    probe_file_for_ppm,
)
from .constants import DEFAULT_AUDIO_SAMPLE_RATE, DEFAULT_AUDIO_THRESHOLD, DEFAULT_AUDIO_HYSTERESIS
from .decoder import PpmDecoder
from .display import TerminalUI, _build_monitor_line, _render_oscilloscope
from .profile import Profile, load_profile
from .uinput import (
    open_uinput_joystick, destroy_uinput_joystick,
    ChannelOutputState, reset_joystick_to_neutral, emit_channel_events,
)


# MARK: - Entry point

# The decoder buffers up to this many channels per frame.  Higher than
# len(profile.channel_map) so that extra channels from the transmitter (e.g. ch9/ch10)
# appear in --debug / --monitor output even if not yet mapped to joystick events.
PPM_DECODE_MAX_CHANNELS = 12

# Number of consecutive frames with the same channel count before that count
# is accepted as the expected value.  Frames outside the locked count are skipped.
CHANNEL_LOCK_FRAMES = 5

# Wall-clock gap between decoded frames above this threshold triggers a log warning.
SIGNAL_GAP_THRESHOLD_S = 0.2

# Gap longer than this resets the virtual joystick to neutral (axis centre,
# all buttons released).  Must be ≥ SIGNAL_GAP_THRESHOLD_S.
SIGNAL_NEUTRAL_THRESHOLD_S = 0.5

OSCILLOSCOPE_HEIGHT = 7   # rows used by the --oscilloscope waveform display


def _parse_args() -> argparse.Namespace:
    """Build the CLI argument parser and return the parsed Namespace."""
    ap = argparse.ArgumentParser(
        description='PPM RC transmitter audio input → Linux virtual joystick'
    )
    source_group = ap.add_mutually_exclusive_group()
    source_group.add_argument(
        '-d', '--device', default=None,
        help='PipeWire/PulseAudio source device name (default: auto-detect)',
    )
    source_group.add_argument(
        '-f', '--file', default=None, metavar='PATH',
        help='Read from a raw s16le stereo recording instead of a live audio source',
    )
    ap.add_argument(
        '-m', '--monitor', action='store_true',
        help='Show live channel values in a fixed status line',
    )
    ap.add_argument(
        '--no-mixer', action='store_true',
        help="Don't modify the ALSA Input Source mixer control",
    )
    ap.add_argument(
        '--no-joystick', action='store_true',
        help='Decode and display PPM frames without creating a virtual joystick '
             '(useful for testing without /dev/uinput access)',
    )
    ap.add_argument(
        '--oscilloscope', action='store_true',
        help='Show an ASCII waveform of the raw audio for each decoded frame',
    )
    ap.add_argument(
        '--no-realtime', action='store_true',
        help='With --file: consume the recording as fast as possible instead of '
             'at the original sample rate (default: real-time playback)',
    )
    ap.add_argument(
        '--debug', action='store_true',
        help='Show raw pulse timing in a fixed debug display',
    )
    ap.add_argument(
        '--threshold', type=int, default=DEFAULT_AUDIO_THRESHOLD,
        metavar='N',
        help=f'int16 midpoint for HIGH/LOW detection (default: {DEFAULT_AUDIO_THRESHOLD}); '
             f'adjust if the audio path has a DC offset',
    )
    ap.add_argument(
        '--hysteresis', type=int, default=DEFAULT_AUDIO_HYSTERESIS,
        metavar='N',
        help=f'int16 dead zone around --threshold (default: {DEFAULT_AUDIO_HYSTERESIS}); '
             'signal must exceed threshold+N to register HIGH and drop below '
             'threshold-N to register LOW — filters noise when the transmitter is off',
    )
    ap.add_argument(
        '--rate', type=int, default=DEFAULT_AUDIO_SAMPLE_RATE,
        metavar='HZ',
        help=f'Audio sample rate in Hz (default: {DEFAULT_AUDIO_SAMPLE_RATE}); '
             f'higher rates (96000, 192000) improve timing precision',
    )
    ap.add_argument(
        '--config', default=None, metavar='PATH',
        help='TOML transmitter profile (default: built-in Absima CR10P mapping)',
    )
    ap.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    return ap.parse_args()


def _setup_audio_source(
    args: argparse.Namespace,
) -> tuple[int, bool, int, str, bool]:
    """
    Probe or configure the audio source.

    For --file mode: probes the recording for a PPM signal and detects channel/invert.
    For live mode: optionally switches the ALSA mixer, then auto-detects or uses --device.

    Returns (audio_channel, audio_invert, actual_rate, source_label, mixer_was_modified).
    Calls sys.exit() if the source cannot be determined.
    """
    if args.file:
        if not os.path.exists(args.file):
            sys.exit(f'error: file not found: {args.file}')
        actual_rate = _get_file_sample_rate(args.file, args.rate)
        print(f'Probing {args.file} for PPM signal … ', end='', flush=True)
        result = probe_file_for_ppm(args.file, actual_rate, args.threshold, args.hysteresis)
        if result is None:
            sys.exit(
                'no PPM signal found\n'
                '       check --rate matches the recording (see: record_ppm.py --help)'
            )
        audio_channel, audio_invert = result
        ch_name  = 'left' if audio_channel == 0 else 'right'
        inv_note = ', inverted' if audio_invert else ''
        print(f'found ({ch_name} channel{inv_note})')
        return audio_channel, audio_invert, actual_rate, args.file, False

    # Live capture
    mixer_was_modified = False
    if not args.no_mixer:
        print('Switching ALSA Input Source → Line In …')
        switch_alsa_input_to_line_in()
        mixer_was_modified = True

    audio_channel = 0
    audio_invert  = False
    if args.device is None:
        args.device, audio_channel, audio_invert = discover_ppm_source(
            args.rate, args.threshold, args.hysteresis)
        if args.device is None:
            sys.exit(
                'error: no PPM source detected automatically\n'
                '       specify one with --device (see: pactl list sources short)'
            )
    return audio_channel, audio_invert, args.rate, args.device, mixer_was_modified


def _decode_loop(
    args: argparse.Namespace,
    audio_source: IO[bytes],
    audio_channel: int,
    audio_invert: bool,
    actual_rate: int,
    ppm_decoder: PpmDecoder,
    output_state: ChannelOutputState,
    uinput_fd: int | None,
    profile: Profile,
    ui: TerminalUI,
) -> None:
    """
    The main audio-decode-emit loop.

    Reads audio chunks from *audio_source*, decodes PPM frames, emits uinput events,
    and updates the terminal display.  Returns when the source is exhausted.
    Callers should catch KeyboardInterrupt.
    """
    frames_decoded: int = 0
    last_frame_time: float | None = None
    in_signal_gap: bool = False
    # Channel count stability locking
    expected_ch_count: int | None = None
    candidate_ch_count: int | None = None
    stable_count: int = 0
    # Oscilloscope buffering (only when --oscilloscope is active)
    osc_buffer: list[int] = []
    osc_frame_samples: list[int] = []

    real_time_file: bool = bool(args.file and not args.no_realtime)

    AUDIO_CHUNK_BYTES = 1024 * 4   # 1024 stereo frames × 4 bytes/frame (2ch × 2 bytes/sample)
    chunk_duration_s  = AUDIO_CHUNK_BYTES / 4 / actual_rate   # wall-clock time per chunk
    next_chunk_deadline = time.monotonic()

    ui.log('Waiting for PPM signal … (Ctrl-C to quit)')
    if real_time_file:
        ui.log('Real-time playback enabled (--no-realtime to disable)')

    while True:
        raw_audio = audio_source.read(AUDIO_CHUNK_BYTES)
        if not raw_audio:
            ui.log('End of recording.' if args.file else 'Audio capture ended unexpectedly')
            break

        channel_byte_offset = audio_channel * 2
        for byte_offset in range(0, len(raw_audio) - 3, 4):
            sample = struct.unpack_from('<h', raw_audio, byte_offset + channel_byte_offset)[0]
            if audio_invert:
                sample = -sample
            completed_frame = ppm_decoder.feed(sample)

            if args.oscilloscope:
                osc_buffer.append(sample)

            if completed_frame is None:
                continue

            if args.oscilloscope:
                osc_frame_samples = osc_buffer[:]
                osc_buffer.clear()

            # ── Signal gap detection ──────────────────────────────────────
            now   = time.monotonic()
            gap_s = (now - last_frame_time) if last_frame_time is not None else 0.0
            last_frame_time = now

            if gap_s > SIGNAL_GAP_THRESHOLD_S and not real_time_file:
                ui.log(f'*** SIGNAL GAP {gap_s:.1f}s ***')
                in_signal_gap = True
            elif in_signal_gap:
                ui.log('Signal restored')
                in_signal_gap = False

            # ── Channel count stability locking ───────────────────────────
            # The decoder may emit short frames at start-up or after signal gaps
            # (sync seen but fewer channels received).  Require CHANNEL_LOCK_FRAMES
            # consecutive frames with the same count before accepting that count;
            # then skip any frame that deviates from it.
            ch_count = len(completed_frame)

            if expected_ch_count is None:
                if ch_count == candidate_ch_count:
                    stable_count += 1
                    if stable_count >= CHANNEL_LOCK_FRAMES:
                        expected_ch_count = ch_count
                        ui.log(f'Channel count locked: {ch_count}')
                else:
                    candidate_ch_count = ch_count
                    stable_count       = 1
                continue   # discard frames received before channel count is locked
            elif ch_count != expected_ch_count:
                ui.log(
                    f'WARNING: channel count {ch_count} ≠ expected '
                    f'{expected_ch_count} — frame skipped'
                )
                continue

            # ── Joystick output ───────────────────────────────────────────
            frames_decoded += 1
            if frames_decoded == 1:
                ui.log(f'PPM signal detected — {ch_count} channels')

            if uinput_fd is not None:
                btn_transitions = emit_channel_events(uinput_fd, output_state, completed_frame, profile)
                for ch_num, btn_code, pressed in btn_transitions:
                    # Show transmitter channel, HID key code, and joystick button number
                    # (joydev assigns button N to BTN_JOYSTICK+N, i.e. 0x120+N)
                    if 0x120 <= btn_code <= 0x12f:
                        hid_label = f'EV_KEY 0x{btn_code:03x} (joystick btn {btn_code - 0x120})'
                    else:
                        hid_label = f'EV_KEY 0x{btn_code:03x}'
                    state_str = 'PRESS  ▶' if pressed else 'release ◀'
                    ui.log(f'ch{ch_num} → {hid_label}: {state_str}')

            # ── Display update ────────────────────────────────────────────
            if ui.active:
                status: list[str] = []
                if args.monitor:
                    status.append(
                        _build_monitor_line(completed_frame, output_state,
                                            ppm_decoder.last_frame_hz, profile)
                    )
                if args.debug:
                    status.extend(ppm_decoder.last_debug_lines)
                if args.oscilloscope and osc_frame_samples:
                    osc_width = min(72, shutil.get_terminal_size(fallback=(80, 24)).columns - 4)
                    status.extend(_render_oscilloscope(
                        osc_frame_samples, args.threshold, osc_width, OSCILLOSCOPE_HEIGHT
                    ))
                ui.update_status(status)
            else:
                if args.monitor:
                    line = _build_monitor_line(completed_frame, output_state,
                                               ppm_decoder.last_frame_hz, profile)
                    sys.stdout.write(f'\r{line}\033[K')
                    sys.stdout.flush()
                if args.debug and ppm_decoder.last_debug_lines:
                    ui.render_debug_stderr(ppm_decoder.last_debug_lines)

        # ── Neutral reset on prolonged signal absence ─────────────────────
        # Checked once per chunk so the reset fires even when the signal is
        # completely absent and the inner per-frame loop never fires.
        if (uinput_fd is not None and not real_time_file
                and last_frame_time is not None and not in_signal_gap
                and time.monotonic() - last_frame_time > SIGNAL_NEUTRAL_THRESHOLD_S):
            in_signal_gap = True
            ui.log('*** SIGNAL LOST — joystick reset to neutral ***')
            reset_joystick_to_neutral(uinput_fd, output_state, profile)

        # ── Real-time throttle (file mode only) ───────────────────────────
        if real_time_file:
            next_chunk_deadline += chunk_duration_s
            sleep_s = next_chunk_deadline - time.monotonic()
            if sleep_s > 0:
                time.sleep(sleep_s)


def main() -> None:
    args = _parse_args()

    # Load transmitter profile
    if args.config:
        try:
            profile = load_profile(args.config)
        except (ValueError, KeyError) as exc:
            sys.exit(f'error: invalid profile {args.config!r}: {exc}')
        except OSError as exc:
            sys.exit(f'error: cannot read profile {args.config!r}: {exc}')
    else:
        profile = Profile()

    if not args.no_joystick and not os.path.exists('/dev/uinput'):
        sys.exit('error: /dev/uinput not found – is the uinput kernel module loaded?\n'
                 '       use --no-joystick to run without creating a virtual device')

    # Probe or configure audio source
    audio_channel, audio_invert, actual_rate, source_label, mixer_was_modified = \
        _setup_audio_source(args)

    # Create virtual joystick (or skip with --no-joystick)
    if profile.device_name:
        print(f'Profile: {profile.device_name}')
    uinput_fd: int | None = None
    if args.no_joystick:
        print('Virtual joystick: disabled (--no-joystick)')
    else:
        print('Creating virtual joystick … ', end='', flush=True)
        try:
            uinput_fd = open_uinput_joystick(profile)
        except PermissionError:
            sys.exit('error: cannot open /dev/uinput – check ACL or group membership\n'
                     '       use --no-joystick to run without creating a virtual device')
        print('ok')

    # Build terminal UI with the right number of fixed status rows
    fixed_rows = 0
    if args.monitor:
        fixed_rows += 1
    if args.debug:
        fixed_rows += PPM_DECODE_MAX_CHANNELS + 2
    if args.oscilloscope:
        fixed_rows += OSCILLOSCOPE_HEIGHT
    ui = TerminalUI()
    if fixed_rows:
        ui.start(fixed_rows)

    # Mutable handles accessed by the shutdown() closure
    audio_capture_proc: subprocess.Popen[bytes] | None = None
    audio_file: Any = None

    def shutdown(signum: int | None = None, frame: Any = None) -> None:
        ui.stop()
        print('\nShutting down …')
        if audio_capture_proc:
            audio_capture_proc.terminate()
        if audio_file:
            audio_file.close()
        if uinput_fd is not None:
            destroy_uinput_joystick(uinput_fd)
        if mixer_was_modified:
            print('Restoring ALSA Input Source …')
            restore_alsa_input_sources()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    channel_name = 'left' if audio_channel == 0 else 'right'
    inv_note     = ', inverted' if audio_invert else ''
    ui.log(f'{"File" if args.file else "Capturing from"}: {source_label}  '
           f'({actual_rate} Hz, {channel_name} channel{inv_note})')

    # Open audio stream
    if args.file:
        audio_file, actual_rate = open_audio_file(args.file, actual_rate)
        audio_source: IO[bytes] = audio_file
    else:
        audio_capture_proc = start_audio_capture(args.device, actual_rate)
        audio_source = audio_capture_proc.stdout  # type: ignore[assignment]

    ppm_decoder = PpmDecoder(
        max_channels=PPM_DECODE_MAX_CHANNELS, debug=args.debug,
        sample_rate=actual_rate, threshold=args.threshold,
        hysteresis=args.hysteresis,
        sync_min_us=profile.sync_min_us, sync_max_us=profile.sync_max_us,
        channel_min_us=profile.channel_min_us, channel_max_us=profile.channel_max_us,
        axis_min_us=profile.axis_min_us, axis_max_us=profile.axis_max_us,
    )
    output_state = ChannelOutputState(profile.channel_map)

    try:
        _decode_loop(args, audio_source, audio_channel, audio_invert, actual_rate,
                     ppm_decoder, output_state, uinput_fd, profile, ui)
    except KeyboardInterrupt:
        pass

    shutdown()
