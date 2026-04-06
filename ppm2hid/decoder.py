from __future__ import annotations

from .constants import (
    DEFAULT_AUDIO_SAMPLE_RATE,
    DEFAULT_AUDIO_THRESHOLD,
    DEFAULT_AUDIO_HYSTERESIS,
)


# MARK: - PPM frame decoder

class PpmDecoder:
    """
    Stateful decoder that consumes raw int16 audio samples and emits
    complete PPM frames.

    Call feed(sample) for every audio sample.  Returns a list of channel
    values in microseconds when a complete frame is recognised, else None.

    After each completed frame the following attributes are updated:
      last_frame_hz    – frame rate computed from sample counts (stable, no jitter)
      last_debug_lines – list of strings for the debug display (when debug=True)
    """

    def __init__(self, max_channels: int = 10, debug: bool = False,
                 sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE,
                 threshold: int = DEFAULT_AUDIO_THRESHOLD,
                 hysteresis: int = DEFAULT_AUDIO_HYSTERESIS,
                 sync_min_us: int = 3_000, sync_max_us: int = 50_000,
                 channel_min_us: int = 500, channel_max_us: int = 2_100,
                 axis_min_us: int = 1_100, axis_max_us: int = 1_900) -> None:
        self.max_channels    = max_channels
        self._debug          = debug
        self._sample_rate    = sample_rate
        self._threshold      = threshold
        self._hysteresis     = hysteresis
        self._axis_min_us    = axis_min_us
        self._axis_max_us    = axis_max_us
        # Timing thresholds in samples, derived from sample_rate so the decoder
        # works correctly at 48 kHz, 96 kHz, 192 kHz, etc.
        self._sync_min  = sample_rate * sync_min_us    // 1_000_000
        self._sync_max  = sample_rate * sync_max_us    // 1_000_000
        self._ch_min    = sample_rate * channel_min_us // 1_000_000
        self._ch_max    = sample_rate * channel_max_us // 1_000_000
        self._current_level  = None
        self._run_length     = 0
        self._synced         = False
        self._pending_high   = None
        self._frame_channels = []
        self._frame_count    = 0
        # Debug accumulation state
        self._dbg_items      = []
        self._dbg_h_pending  = None
        self._dbg_skips      = 0
        # Sample-count-based Hz measurement (immune to audio-chunk jitter)
        self._sample_count     = 0
        self._last_sync_sample = None
        self.last_frame_hz     = 0.0
        # Debug lines ready for the caller to display
        self.last_debug_lines  = []

    def feed(self, sample: int) -> list[int] | None:
        """
        Process one int16 audio sample.
        Returns a list of µs values when a frame completes, else None.

        Uses Schmitt trigger logic when hysteresis > 0: the signal must
        exceed threshold + hysteresis to register HIGH, and drop below
        threshold - hysteresis to register LOW.  Samples in between keep
        the current level, preventing noise from producing phantom pulses.
        """
        self._sample_count += 1
        if sample > self._threshold + self._hysteresis:
            new_level = 'high'
        elif sample < self._threshold - self._hysteresis:
            new_level = 'low'
        else:
            # Dead zone — keep current level (or low before first real edge)
            new_level = self._current_level if self._current_level is not None else 'low'

        if new_level == self._current_level:
            self._run_length += 1
            return None

        completed_level  = self._current_level
        completed_length = self._run_length
        self._current_level = new_level
        self._run_length    = 1

        if completed_level is None:
            return None

        return self._process_completed_pulse(completed_level, completed_length)

    def _process_completed_pulse(self, pulse_type: str, pulse_length_samples: int) -> list[int] | None:
        """
        Evaluate a just-completed pulse and update decoder state.

        PPM encoding on this transmitter (positive/high-active):
          Each channel occupies one HIGH+LOW pair.  The channel value is encoded
          in the *combined* duration (HIGH + LOW), not in the HIGH or LOW alone.
          Typical values: HIGH ≈ 700–1500 µs, LOW ≈ 416 µs (constant separator),
          total ≈ 1100–1900 µs.

        Two-phase measurement:
          1. HIGH pulse arrives → stored in self._pending_high (value not yet known).
          2. The following LOW pulse arrives → total = pending_high + LOW = channel µs.
          Between steps the decoder is "pending"; if anything interrupts the pair
          (another HIGH, an out-of-range pulse, etc.) pending_high is discarded.

        Sync detection:
          A long HIGH (> sync_min, typically >3 ms) is the frame boundary.  Frames
          received before the first sync are discarded (self._synced is False) because
          the decoder does not yet know where in the sequence it joined.
        """

        if pulse_type == 'high':
            if self._sync_min <= pulse_length_samples <= self._sync_max:
                # Sync pulse.  Compute Hz from samples elapsed since last sync.
                if self._last_sync_sample is not None:
                    elapsed = self._sample_count - self._last_sync_sample
                    self.last_frame_hz = (self._sample_rate / elapsed
                                         if elapsed > 0 else 0.0)
                self._last_sync_sample = self._sample_count

                if self._debug:
                    self.last_debug_lines = self._build_debug_lines(pulse_length_samples)
                    self._dbg_items     = []
                    self._dbg_h_pending = None
                    self._dbg_skips     = 0

                completed_frame = (
                    self._frame_channels[:]
                    if len(self._frame_channels) >= 2
                    else None
                )
                self._frame_channels = []
                self._pending_high   = None
                self._synced         = True
                self._frame_count   += 1
                return completed_frame

            elif (self._synced
                  and self._ch_min <= pulse_length_samples <= self._ch_max):
                # Normal channel HIGH — store and wait for the LOW separator
                self._pending_high = pulse_length_samples
                if self._debug:
                    self._dbg_h_pending = (len(self._frame_channels) + 1,
                                           pulse_length_samples)
            else:
                self._pending_high = None
                if self._debug:
                    self._dbg_skips    += 1
                    self._dbg_h_pending = None

        elif pulse_type == 'low':
            if self._pending_high is not None and self._synced:
                # Second phase: add the LOW separator to complete the channel measurement.
                # Both halves must together fall within the expected channel duration range.
                total_samples = self._pending_high + pulse_length_samples
                total_us      = self._smp_to_us(total_samples)

                if self._ch_min <= total_samples <= self._ch_max:
                    clamped_us = max(self._axis_min_us, min(self._axis_max_us, total_us))
                    if self._debug and self._dbg_h_pending:
                        ch_n, h_smp = self._dbg_h_pending
                        self._dbg_items.append(
                            (ch_n, h_smp, pulse_length_samples, total_us, clamped_us))
                        self._dbg_h_pending = None
                    if len(self._frame_channels) < self.max_channels:
                        self._frame_channels.append(clamped_us)
                else:
                    if self._debug:
                        self._dbg_skips    += 1
                        self._dbg_h_pending = None

                self._pending_high = None

        return None

    def _smp_to_us(self, samples: int) -> int:
        return samples * 1_000_000 // self._sample_rate

    def _build_debug_lines(self, sync_smp: int) -> list[str]:
        """Return a fixed-height list of debug strings for the current frame."""
        FIXED   = self.max_channels + 2
        sync_us = self._smp_to_us(sync_smp)
        hz_str  = f'  {self.last_frame_hz:4.0f}Hz' if self.last_frame_hz > 0 else ''
        vals    = '  '.join(str(v) for v in self._frame_channels)

        lines = [
            f'frame {self._frame_count:4d}  '
            f'sync {sync_smp:4d}smp={sync_us:5d}µs  '
            f'{len(self._frame_channels):2d}ch  [{vals}]{hz_str}'
        ]

        for ch_n, h_smp, l_smp, total_us, clamped_us in self._dbg_items:
            h_us  = self._smp_to_us(h_smp)
            l_us  = self._smp_to_us(l_smp)
            clamp = f'  →clamped {clamped_us}' if clamped_us != total_us else ''
            lines.append(
                f'  ch{ch_n:2d}  '
                f'H {h_smp:4d}smp={h_us:5d}µs '
                f'+ L {l_smp:4d}smp={l_us:5d}µs '
                f'= {total_us:5d}µs{clamp}'
            )

        while len(lines) < FIXED - 1:
            lines.append('')

        lines.append(f'  skipped: {self._dbg_skips}')
        return lines
