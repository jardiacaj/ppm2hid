from __future__ import annotations

import select
import struct
import subprocess
import time
import wave
from typing import Any

from .constants import (
    DEFAULT_AUDIO_SAMPLE_RATE,
    DEFAULT_AUDIO_THRESHOLD,
    DEFAULT_AUDIO_HYSTERESIS,
)
from .decoder import PpmDecoder


# MARK: - Audio file helpers (WAV + raw)

def _validate_wav(wf: wave.Wave_read) -> None:
    """Raise wave.Error if wf is not s16le stereo."""
    if wf.getnchannels() != 2:
        raise wave.Error(f'expected stereo (2 ch), got {wf.getnchannels()}')
    if wf.getsampwidth() != 2:
        raise wave.Error(f'expected 16-bit samples, got {wf.getsampwidth() * 8}-bit')


class _WavSource:
    """Wraps wave.Wave_read to expose a .read(n_bytes) interface."""

    def __init__(self, wf: wave.Wave_read) -> None:
        self._wf = wf
        self._bytes_per_frame = wf.getnchannels() * wf.getsampwidth()

    @property
    def sample_rate(self) -> int:
        return self._wf.getframerate()

    def read(self, n_bytes: int) -> bytes:
        n_frames = max(1, n_bytes // self._bytes_per_frame)
        return self._wf.readframes(n_frames)

    def close(self) -> None:
        self._wf.close()


def open_audio_file(path: str, hint_rate: int = DEFAULT_AUDIO_SAMPLE_RATE) -> tuple[Any, int]:
    """
    Open a .wav or raw s16le stereo audio file.
    Returns (source, actual_sample_rate).
    For .wav files the sample rate is read from the file header; hint_rate is ignored.
    """
    if path.lower().endswith('.wav'):
        wf = wave.open(path, 'rb')
        _validate_wav(wf)
        return _WavSource(wf), wf.getframerate()
    return open(path, 'rb'), hint_rate


def _get_file_sample_rate(path: str, hint_rate: int = DEFAULT_AUDIO_SAMPLE_RATE) -> int:
    """Return the sample rate from a .wav header, or hint_rate for raw files."""
    if path.lower().endswith('.wav'):
        try:
            with wave.open(path, 'rb') as wf:
                return wf.getframerate()
        except (wave.Error, OSError):
            pass
    return hint_rate


# MARK: - Audio capture via PipeWire/PulseAudio

def start_audio_capture(pipewire_source_name: str,
                        sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE) -> subprocess.Popen[bytes]:
    """Launch parecord and return the Popen handle (raw s16le stereo)."""
    return subprocess.Popen(
        [
            'parecord',
            f'--device={pipewire_source_name}',
            '--format=s16le',
            f'--rate={sample_rate}',
            '--channels=2',
            '--raw',
            '--latency-msec=5',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


# MARK: - PPM source auto-discovery

def list_pipewire_sources() -> list[str]:
    """Return non-monitor PipeWire/PulseAudio source names via pactl."""
    try:
        result = subprocess.run(
            ['pactl', 'list', 'sources', 'short'],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    sources = []
    for line in result.stdout.splitlines():
        parts = line.split('\t')
        if len(parts) >= 2:
            name = parts[1]
            if not name.endswith('.monitor'):
                sources.append(name)
    return sources


def probe_source_for_ppm(source_name: str, sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE,
                          threshold: int = DEFAULT_AUDIO_THRESHOLD,
                          hysteresis: int = DEFAULT_AUDIO_HYSTERESIS,
                          duration_s: float = 0.5) -> tuple[int, bool] | None:
    """
    Capture a short burst from *source_name* and return the channel index
    (0 = left, 1 = right) where a valid PPM frame is detected, or None if no
    PPM signal is found on either channel.
    """
    # Bytes to read: duration × sample_rate × 2 channels × 2 bytes/sample
    probe_bytes = int(duration_s * sample_rate * 4)
    probe_bytes = (probe_bytes + 3) & ~3   # align to stereo frame boundary

    try:
        proc = subprocess.Popen(
            [
                'parecord',
                f'--device={source_name}',
                '--format=s16le',
                f'--rate={sample_rate}',
                '--channels=2',
                '--raw',
                '--latency-msec=50',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None

    try:
        deadline = time.monotonic() + duration_s + 2
        chunks   = []
        total    = 0
        while total < probe_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not select.select([proc.stdout], [], [], remaining)[0]:
                break   # timeout — source produced no audio
            chunk = proc.stdout.read1(probe_bytes - total)
            if not chunk:   # EOF
                break
            chunks.append(chunk)
            total += len(chunk)
        raw_audio = b''.join(chunks)
    except (OSError, ValueError, struct.error):
        raw_audio = b''
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    if len(raw_audio) < 8:
        return None

    # s16le stereo layout: [L0 L1 R0 R1] per frame; channel byte offsets are 0 and 2.
    # Try normal then inverted on each channel; prefer normal (invert=False first).
    for channel_index, channel_byte_offset in enumerate((0, 2)):
        for invert in (False, True):
            decoder = PpmDecoder(sample_rate=sample_rate, threshold=threshold,
                                 hysteresis=hysteresis)
            for byte_offset in range(0, len(raw_audio) - 3, 4):
                sample = struct.unpack_from('<h', raw_audio, byte_offset + channel_byte_offset)[0]
                if invert:
                    sample = -sample
                if decoder.feed(sample) is not None:
                    return channel_index, invert
    return None


def probe_file_for_ppm(file_path: str, sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE,
                        threshold: int = DEFAULT_AUDIO_THRESHOLD,
                        hysteresis: int = DEFAULT_AUDIO_HYSTERESIS,
                        duration_s: float = 0.5) -> tuple[int, bool] | None:
    """
    Read the first *duration_s* seconds of a .wav or raw s16le stereo file and
    return (channel, invert) where a valid PPM frame is detected, or None if no
    PPM signal is found.  For .wav files the sample rate is read from the header.
    """
    try:
        if file_path.lower().endswith('.wav'):
            with wave.open(file_path, 'rb') as wf:
                _validate_wav(wf)
                sample_rate = wf.getframerate()
                raw_audio   = wf.readframes(int(duration_s * sample_rate))
        else:
            probe_bytes = int(duration_s * sample_rate * 4)
            probe_bytes = (probe_bytes + 3) & ~3
            with open(file_path, 'rb') as f:
                raw_audio = f.read(probe_bytes)
    except (wave.Error, OSError):
        return None
    if len(raw_audio) < 8:
        return None
    for channel_index, channel_byte_offset in enumerate((0, 2)):
        for invert in (False, True):
            decoder = PpmDecoder(sample_rate=sample_rate, threshold=threshold,
                                 hysteresis=hysteresis)
            for byte_offset in range(0, len(raw_audio) - 3, 4):
                sample = struct.unpack_from('<h', raw_audio, byte_offset + channel_byte_offset)[0]
                if invert:
                    sample = -sample
                if decoder.feed(sample) is not None:
                    return channel_index, invert
    return None


def discover_ppm_source(sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE,
                         threshold: int = DEFAULT_AUDIO_THRESHOLD,
                         hysteresis: int = DEFAULT_AUDIO_HYSTERESIS) -> tuple[str | None, int | None, bool]:
    """
    Enumerate PipeWire/PulseAudio sources and return ``(source_name, channel, invert)``
    for the first source that carries a valid PPM signal (channel 0 = left, 1 = right;
    invert=True if the signal is LOW-active), or ``(None, None, False)`` if none found.
    """
    sources = list_pipewire_sources()
    if not sources:
        print('Auto-discovery: no PipeWire/PulseAudio sources found')
        return None, None, False

    print(f'Auto-discovery: probing {len(sources)} source(s) for PPM signal …')
    for source in sources:
        print(f'  {source} … ', end='', flush=True)
        result = probe_source_for_ppm(source, sample_rate=sample_rate, threshold=threshold,
                                      hysteresis=hysteresis)
        if result is not None:
            channel, invert = result
            ch_name  = 'left' if channel == 0 else 'right'
            inv_note = ', inverted' if invert else ''
            print(f'PPM detected ({ch_name} channel{inv_note})')
            return source, channel, invert
        print('no signal')

    print('Auto-discovery: no PPM source found')
    return None, None, False
