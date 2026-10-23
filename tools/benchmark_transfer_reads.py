"""Compare USB MIDI read settings; never sends a device write command.

Run from the repository root with the Transfer Python environment:
    python tools/benchmark_transfer_reads.py --path /samples/1

Each JSON line contains timings and a hash, never the object's contents.
Redirect stdout to a local file under synfact-transfer/work/ to retain results.
"""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'synfact-transfer'))
import core as C
from midi_io import MidiDevice, ports, stop_ports


class ReadOnly:
    def __init__(self, device):
        self.device = device
        self.calls = []
        self.started = time.monotonic()

    def call(self, kind, payload=b'', timeout=None):
        if kind not in (C.S.T_RD_OPEN, C.S.T_RD_PART, C.S.T_RD_CLOSE):
            raise PermissionError('Read benchmark refuses non-read commands.')
        # Close remains available if an object never reaches its final partial.
        if kind != C.S.T_RD_CLOSE and time.monotonic() - self.started > 120:
            raise TimeoutError('Read benchmark exceeded 120 seconds for one object.')
        start = time.perf_counter()
        reply = self.device.call(kind, payload, timeout)
        self.calls.append((kind, time.perf_counter() - start, reply))
        return reply


def measure(device, path, block, rest, repeat):
    reader = ReadOnly(device)
    start = time.perf_counter()
    # stfiles prints transfer details; reserve stdout for machine-readable JSON.
    with contextlib.redirect_stdout(io.StringIO()):
        obj = C.S.data_read(reader, path, block=block, rest=rest, progress=False)
    elapsed = time.perf_counter() - start
    C.validate_object(obj)
    partials = [(seconds, reply) for kind, seconds, reply in reader.calls
                if kind == C.S.T_RD_PART]
    return dict(path=path, block=block, rest=rest, repeat=repeat,
                bytes=len(obj), seconds=elapsed, kib_s=len(obj) / 1024 / elapsed,
                sha256=hashlib.sha256(obj).hexdigest(), partials=len(partials),
                empty_partials=sum(C.S.u32(reply, 23) == 0 for _, reply in partials),
                negotiated_block=C.S.u32(reader.calls[0][2], 10),
                median_rpc_ms=statistics.median(seconds for seconds, _ in partials) * 1000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', action='append', required=True,
                        help='Existing device object; repeat for multiple objects')
    parser.add_argument('--blocks', type=int, nargs='+', default=[2048, 8192])
    parser.add_argument('--rests', type=float, nargs='+', default=[0.05, 0.01, 0])
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--input')
    parser.add_argument('--output')
    args = parser.parse_args()
    if (args.repeat < 1 or any(b < 1 or b > 32768 for b in args.blocks)
            or any(not 0 <= r <= 1 for r in args.rests)):
        parser.error('Use positive block sizes up to 32768 and rests between 0 and 1 second.')
    for path in args.path:
        if re.fullmatch(r'/samples/[1-9][0-9]*', path):
            slot = int(path.rsplit('/', 1)[1])
            if slot == 449 or C.D.RAW_FIRST_N <= slot < C.D.RAW_FIRST_N + C.D.MAX_WINDOWS:
                continue
        C.device_path(path)
    device = None
    try:
        available = ports()
        names = []
        for key, chosen in (('inputs', args.input), ('outputs', args.output)):
            candidates = [chosen] if chosen in available[key] else [
                p for p in available[key] if 'syntakt' in p.lower()] if chosen is None else []
            if len(candidates) != 1:
                parser.error(f'Select one {key} endpoint explicitly; available: {available[key]}')
            names.append(candidates[0])
        device = MidiDevice(*names)
        device.open()
        failures = 0
        for path in args.path:
            reference = None
            for block in args.blocks:
                for rest in args.rests:
                    for repeat in range(args.repeat):
                        try:
                            result = measure(device, path, block, rest, repeat)
                        except (C.S.DeviceError, ValueError) as error:
                            failures += 1
                            print(json.dumps(dict(path=path, block=block, rest=rest,
                                                  repeat=repeat, error=str(error))), flush=True)
                            continue
                        if reference is None:
                            reference = result['sha256']
                        if result['sha256'] != reference:
                            raise RuntimeError('Object changed between reads; benchmark stopped.')
                        print(json.dumps(result), flush=True)
        return 1 if failures else 0
    finally:
        if device is not None:
            device.close()
        stop_ports()


if __name__ == '__main__':
    raise SystemExit(main())
