#!/usr/bin/env python3
"""
record_ppm.py – Record a raw PPM audio capture for use as test data.

Captures stereo s16le audio from the PPM source and writes it to a raw file.
The resulting file can be replayed with:

    python ppm2hid.py --file testdata/ppm_<timestamp>_<rate>k.raw --rate <rate>

By default the file is saved to testdata/ with an auto-generated name that
encodes the timestamp and sample rate so recordings are easy to identify.

Usage examples:
    python record_ppm.py                         # auto-detect source, 192 kHz
    python record_ppm.py --rate 48000            # lower sample rate
    python record_ppm.py --duration 15           # stop after 15 seconds
    python record_ppm.py -o my_recording.raw     # custom output path
    python record_ppm.py --device alsa_input.X   # skip auto-detect
"""

import argparse
import datetime
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from ppm2hid import discover_ppm_source, AUDIO_SAMPLE_RATE

TESTDATA_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'testdata')
DEFAULT_RATE  = 192_000
CHUNK_BYTES   = 8192


def main():
    ap = argparse.ArgumentParser(
        description='Record raw PPM audio for use as test data',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='The output file can be replayed with:\n'
               '  python ppm2hid.py --file <path> --rate <rate>',
    )
    ap.add_argument(
        '-d', '--device', default=None,
        help='PipeWire/PulseAudio source name (default: auto-detect)',
    )
    ap.add_argument(
        '--rate', type=int, default=DEFAULT_RATE, metavar='HZ',
        help=f'Sample rate in Hz (default: {DEFAULT_RATE}); '
             f'192000 gives the best timing resolution for test data',
    )
    ap.add_argument(
        '--duration', type=float, default=None, metavar='SECONDS',
        help='Stop automatically after this many seconds (default: Ctrl-C)',
    )
    ap.add_argument(
        '-o', '--output', default=None, metavar='PATH',
        help='Output file path (default: testdata/ppm_YYYYMMDD_HHMMSS_<rate>k.raw)',
    )
    args = ap.parse_args()

    # Resolve device
    if args.device is None:
        args.device, _, _ = discover_ppm_source(args.rate)
        if args.device is None:
            sys.exit(
                'error: no PPM source detected automatically\n'
                '       specify one with --device (see: pactl list sources short)'
            )

    # Resolve output path
    if args.output is None:
        os.makedirs(TESTDATA_DIR, exist_ok=True)
        stamp    = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        rate_tag = f'{args.rate // 1000}k'
        args.output = os.path.join(TESTDATA_DIR, f'ppm_{stamp}_{rate_tag}.raw')

    print(f'Device  : {args.device}')
    print(f'Rate    : {args.rate} Hz  (s16le stereo)')
    print(f'Output  : {args.output}')
    if args.duration:
        print(f'Duration: {args.duration:.1f} s')
    print('Recording … Ctrl-C to stop\n')

    proc = subprocess.Popen(
        [
            'parecord',
            f'--device={args.device}',
            '--format=s16le',
            f'--rate={args.rate}',
            '--channels=2',
            '--raw',
            '--latency-msec=20',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    start_time    = time.monotonic()
    bytes_written = 0

    def finish(signum=None, frame=None):
        proc.terminate()
        proc.wait()
        elapsed  = time.monotonic() - start_time
        mb       = bytes_written / 1_048_576
        print(f'\n\nSaved {elapsed:.1f} s  ({mb:.1f} MB)  →  {args.output}')
        print(f'\nReplay with:')
        print(f'  python ppm2hid.py --file {args.output} --rate {args.rate}')
        sys.exit(0)

    signal.signal(signal.SIGINT,  finish)
    signal.signal(signal.SIGTERM, finish)

    with open(args.output, 'wb') as out:
        while True:
            chunk = proc.stdout.read(CHUNK_BYTES)
            if not chunk:
                break
            out.write(chunk)
            bytes_written += len(chunk)

            elapsed = time.monotonic() - start_time
            mb      = bytes_written / 1_048_576
            sys.stdout.write(f'\r  {elapsed:6.1f} s  {mb:5.1f} MB')
            sys.stdout.flush()

            if args.duration and elapsed >= args.duration:
                break

    finish()


if __name__ == '__main__':
    main()
