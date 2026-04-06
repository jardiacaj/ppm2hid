#!/usr/bin/env python3
"""
test_noise.py – PpmDecoder must not produce frames from electrical noise.

Recording required: testdata/noise_tx_off.wav
  ~3 s at 192 kHz, transmitter OFF, cable connected to Line In.

  Record with:
    python3 record_ppm.py --device <device> --name noise_tx_off --duration 3

  (Auto-detect requires the transmitter to be on, so pass --device explicitly.
   Run 'pactl list sources short' to find the Line In source name.)
"""

from __future__ import annotations

import os
import struct
import sys
import unittest
import wave

sys.path.insert(0, os.path.dirname(__file__))
from ppm2hid import PpmDecoder, Profile, DEFAULT_AUDIO_HYSTERESIS

_PROFILE = Profile()

RECORDING_PATH = os.path.join(os.path.dirname(__file__), 'testdata', 'noise_tx_off.wav')

_SKIP_MSG = (
    f'Recording not found: {RECORDING_PATH}\n'
    'Record with (transmitter OFF, cable plugged in):\n'
    '  python3 record_ppm.py --device <device> --name noise_tx_off --duration 3'
)


def _load_stereo_samples(path: str) -> tuple[list[int], list[int], int]:
    """Return (left_samples, right_samples, sample_rate) from a stereo WAV."""
    with wave.open(path, 'rb') as wf:
        raw  = wf.readframes(wf.getnframes())
        rate = wf.getframerate()
    left  = [struct.unpack_from('<h', raw, o)[0] for o in range(0, len(raw) - 3, 4)]
    right = [struct.unpack_from('<h', raw, o)[0] for o in range(2, len(raw) - 1, 4)]
    return left, right, rate


class TestNoiseRejection(unittest.TestCase):
    """Decoder with default hysteresis must be silent when the transmitter is off."""

    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(RECORDING_PATH):
            raise FileNotFoundError(_SKIP_MSG)
        cls.left, cls.right, cls.sample_rate = _load_stereo_samples(RECORDING_PATH)

    def _decode(self, samples):
        decoder = PpmDecoder(sample_rate=self.sample_rate)
        return [f for s in samples if (f := decoder.feed(s)) is not None]

    def test_no_frames_left_channel(self) -> None:
        """Left channel noise must not produce any PPM frames."""
        frames = self._decode(self.left)
        self.assertEqual(
            len(frames), 0,
            f'{len(frames)} phantom frame(s) decoded from left-channel noise — '
            f'DEFAULT_AUDIO_HYSTERESIS={DEFAULT_AUDIO_HYSTERESIS} may need to be raised, or '
            f'the recording was made with the transmitter on',
        )

    def test_no_frames_right_channel(self) -> None:
        """Right channel noise must not produce any PPM frames."""
        frames = self._decode(self.right)
        self.assertEqual(
            len(frames), 0,
            f'{len(frames)} phantom frame(s) decoded from right-channel noise',
        )


if __name__ == '__main__':
    unittest.main()
