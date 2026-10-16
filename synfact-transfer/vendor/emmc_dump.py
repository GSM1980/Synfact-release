"""READ-ONLY eMMC snapshot through MACHINE LOADER V5-DUMP (Data /samples/N, N = 8185 + k).

Window k = bytes [k*0x80000, (k+1)*0x80000) of the card. The device answers size 0 past
the card's last sector, so the capacity is found with cheap probes (open + one partial).

    selftest                          offline: fake device, no MIDI
    capacity                          find the card size (a few probes)
    sample  --out DIR [--windows ..]  read a default spread of windows (or the given list)
    dump    --out DIR --first K --last K   read a range; resumes (skips windows already saved)
    report  DIR                       offline: classify the saved windows, write report.txt/json

This tool only sends Data READ requests (DataReadOpen/Partial/Close). It has no write,
clear, copy, move, swap or rename command, and the V5-DUMP firmware also marks every
/samples/N >= 481 read-only. It ONLY makes sense on a unit running V5-DUMP. Close
Elektron Transfer first. Running it against the device is the user's decision.
The snapshot contains the user's own data (projects, samples, sounds) and Elektron
data: keep it on this computer, never in Git.
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys
import time
import zlib

HERE = Path(__file__).resolve().parent
STFILES = HERE / 'stfiles.py'  # Local application snapshot; original preserved.

RAW_FIRST_N = 8185
WINDOW = 0x80000
MAX_WINDOWS = 0x2000
HEADER, FOOTER = 31, 12
OBJECT_MAGIC, FOOTER_MAGIC = 0xAC11D303, 0xAAA1DAAA
READ_TYPES = (0x54, 0x55, 0x56)                 # DataReadOpen / Partial / Close
DEFAULT_OUT = HERE.parents[1] / 'emmc-snapshots'

# Known 1.41 regions (docs/EMMC_STORAGE_1.41.md), in windows of 512 KiB
REGIONS = ((0x000, 0x004, 'MmcFs superblock + bitmaps'), (0x004, 0x008, 'sounds 8x256x1 KiB'),
           (0x008, 0x048, 'stock samples /samples/1..64'), (0x048, 0x1C8, 'extended slots /samples/65..448'),
           (0x1C8, 0x1E8, 'V4 machine file /samples/449..480'), (0x1E8, 0x1F0, 'MRAM state image A'),
           (0x1F0, 0x1F8, 'MRAM state image B'), (0x1F8, 0x200, 'temporary project (MmcFs 0x80)'),
           (0x200, 0x600, 'projects 1..128 x 4 MiB'), (0x600, MAX_WINDOWS, 'high zone (no 1.41 access)'))
MAGICS = {b'SAMP': 'SAMP slot header', b'COKI': 'COKI state image', b'\xbe\xef\xba\xce': 'MmcFs superblock'}


def region(k):
    return next(name for lo, hi, name in REGIONS if lo <= k < hi)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def ecrc(data):
    return zlib.crc32(data, 0xFFFFFFFF) & 0xFFFFFFFF


def be32(v):
    return (v & 0xFFFFFFFF).to_bytes(4, 'big')


def u32(b, o):
    return int.from_bytes(b[o:o + 4], 'big')


def load_stfiles():
    spec = importlib.util.spec_from_file_location('stfiles_ref', STFILES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReadOnly:
    """Wraps any transport with call(type, payload, timeout): refuses every non-read request."""

    def __init__(self, dev):
        self.dev = dev

    def call(self, type_byte, payload=b'', timeout=None):
        if type_byte not in READ_TYPES:
            raise PermissionError(f'emmc_dump is read-only: refused request type 0x{type_byte:02x}')
        return self.dev.call(type_byte, payload, timeout=timeout)


def unpack_object(obj, n):
    if len(obj) < HEADER + FOOTER:
        raise ValueError(f'/samples/{n}: object too short ({len(obj)} B)')
    header, body, footer = obj[:HEADER], obj[HEADER:-FOOTER], obj[-FOOTER:]
    if u32(header, 0) != OBJECT_MAGIC or u32(header, 25) != len(body) or u32(header, 21) != n:
        raise ValueError(f'/samples/{n}: unexpected object header')
    if u32(footer, 8) != FOOTER_MAGIC or u32(footer, 4) != len(body) or u32(footer, 0) != ecrc(body):
        raise ValueError(f'/samples/{n}: bad footer/checksum')
    return body


class Reader:
    def __init__(self, dev, st, block, rest, retries=2, log=print):
        self.dev, self.st, self.block, self.retries, self.log = ReadOnly(dev), st, block, retries, log
        self.dev_is_real = not isinstance(dev, FakeV5)
        st.REST = rest                      # pause between partials (stfiles default 0.05 s)

    def window(self, k):
        n = RAW_FIRST_N + k
        for attempt in range(self.retries + 1):
            try:
                start = time.perf_counter()
                body = unpack_object(self.st.data_read(self.dev, f'/samples/{n}', block=self.block, progress=False), n)
                return body, time.perf_counter() - start
            except (self.st.DeviceError, ValueError) as error:
                self.log(f'  window {k:#06x}: {error} (attempt {attempt + 1})')
        raise RuntimeError(f'window {k:#x} failed after {self.retries + 1} attempts')

    def window_size(self, k):
        """Open + first partial + close -> body size announced by the object header
        (31 B header at the start of the first partial: magic, ..., index @21, size @25).
        Does not rely on the partial's 'last' flag (the real device may send a separate
        empty final partial)."""
        st, n = self.st, RAW_FIRST_N + k
        rx = st._ok(self.dev.call(st.T_RD_OPEN, f'/samples/{n}'.encode() + b'\0' + be32(self.block) + b'\0'),
                    f'DataReadOpen /samples/{n}')
        job = u32(rx, 6)
        try:
            # As stfiles.data_read: pause before each partial and keep asking until the 31 B
            # object header is complete (the real device answers an empty first partial
            # when asked immediately, 2026-10-13).
            chunk, seq, last = b'', 0, 0
            while len(chunk) < HEADER and not last and seq < 64:
                time.sleep(max(st.REST, 0.05) if self.dev_is_real else st.REST)
                rx = st._ok(self.dev.call(st.T_RD_PART, be32(job) + be32(seq), timeout=10),
                            f'DataReadPartial {seq}')
                last = rx[18]
                chunk += rx[27:27 + u32(rx, 23)]
                seq += 1
            if len(chunk) < HEADER or u32(chunk, 0) != OBJECT_MAGIC or u32(chunk, 21) != n:
                raise RuntimeError(f'/samples/{n}: no object header after {seq} partials '
                                   f'(last={last}): {chunk[:31].hex()}')
            size = u32(chunk, 25)
            self.log(f'  probe window {k:#06x} (/samples/{n}, byte {k * WINDOW:#010x}): {size:#x} B')
            return size
        finally:
            self.dev.call(st.T_RD_CLOSE, be32(job))

    def nonempty(self, k):
        return self.window_size(k) > 0

    def capacity(self):
        """Binary search for the first empty window (sizes are monotone: full, then 0)
        -> (windows with data, exact card bytes)."""
        if not self.nonempty(0):
            raise RuntimeError('window 0 is empty: is V5-DUMP installed and the card ready?')
        lo, hi = 0, MAX_WINDOWS                # lo has data, hi is past the end
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.nonempty(mid):
                lo = mid
            else:
                hi = mid
        if hi == MAX_WINDOWS:
            raise RuntimeError('every probe reported data up to 4 GiB: not a plausible card, stopping')
        return hi, lo * WINDOW + self.window_size(lo)


class Snapshot:
    """DIR/w{k:04x}.bin + DIR/index.json (sha256, size, seconds per window)."""

    def __init__(self, out):
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.index_path = self.out / 'index.json'
        self.index = json.loads(self.index_path.read_text()) if self.index_path.exists() else dict(windows={})

    def path(self, k):
        return self.out / f'w{k:04x}.bin'

    def has(self, k):
        entry = self.index['windows'].get(f'{k:04x}')
        return entry is not None and self.path(k).exists() and sha(self.path(k).read_bytes()) == entry['sha256']

    def save(self, k, body, seconds):
        self.path(k).write_bytes(body)
        self.index['windows'][f'{k:04x}'] = dict(size=len(body), sha256=sha(body), seconds=round(seconds, 3),
                                                 region=region(k),
                                                 at=datetime.datetime.now().isoformat(timespec='seconds'))
        self.flush()

    def flush(self):
        self.index_path.write_text(json.dumps(self.index, indent=1, sort_keys=True) + '\n', encoding='utf-8')


def default_sample(windows):
    """A spread over every region, plus first/last window of the card."""
    picks = {0, 1, 4, 8, 0x47, 0x48, 0x49, 0x1C7, 0x1C8, 0x1C9, 0x1E8, 0x1F0, 0x1F8, 0x200, 0x201, 0x5FF}
    picks |= set(range(0x48, 0x1C8, 0x20))
    picks |= set(range(0x600, windows, 0x40)) | {windows - 1}
    return sorted(k for k in picks if 0 <= k < windows)


def read_windows(reader, snap, windows, log=print):
    total_bytes, total_seconds = 0, 0.0
    for n, k in enumerate(windows, 1):
        if snap.has(k):
            log(f'[{n}/{len(windows)}] window {k:#06x} already saved')
            continue
        body, seconds = reader.window(k)
        snap.save(k, body, seconds)
        total_bytes += len(body)
        total_seconds += seconds
        rate = total_bytes / total_seconds / 1024 if total_seconds else 0
        log(f'[{n}/{len(windows)}] window {k:#06x} {region(k)}: {len(body)} B in {seconds:.1f} s '
            f'({rate:.1f} KiB/s average) {classify(body)["kind"]}')
    return total_bytes, total_seconds


def classify(body):
    if not body:
        return dict(kind='empty')
    if body.count(0) == len(body):
        return dict(kind='all 00')
    if body.count(0xFF) == len(body):
        return dict(kind='all FF')
    sectors = [body[i:i + 512] for i in range(0, len(body), 512)]
    blank = sum(1 for s in sectors if s.count(0) == len(s) or s.count(0xFF) == len(s))
    found = {}
    for magic, name in MAGICS.items():
        offsets = [i for i in range(0, len(body), 512) if body[i:i + len(magic)] == magic]
        if offsets:
            found[name] = offsets[:8]
    return dict(kind='data', blank_sectors=blank, sectors=len(sectors), magics=found)


def report(directory, log=print):
    snap = Snapshot(directory)
    rows = []
    for key in sorted(snap.index['windows']):
        k = int(key, 16)
        body = snap.path(k).read_bytes()
        info = classify(body)
        rows.append(dict(window=f'{k:#06x}', start=f'{k * WINDOW:#010x}', region=region(k), size=len(body), **info))
    text = [f'eMMC snapshot report: {directory}', f'{len(rows)} windows of 512 KiB', '']
    for r in rows:
        extra = '' if r['kind'] != 'data' else \
            f" ({r['blank_sectors']}/{r['sectors']} blank sectors{', ' if r['magics'] else ''}" \
            f"{', '.join(f'{m} at {[hex(o) for o in offs]}' for m, offs in r['magics'].items())})"
        text.append(f"{r['window']} {r['start']} {r['region']:<36} {r['kind']}{extra}")
    (Path(directory) / 'report.json').write_text(json.dumps(rows, indent=1) + '\n', encoding='utf-8')
    (Path(directory) / 'report.txt').write_text('\n'.join(text) + '\n', encoding='utf-8')
    for line in text:
        log(line)
    return rows


# ------------------------------------------------------------------ offline fake device
class FakeV5:
    """Object-level model of the Data read API of V5-DUMP over a sparse fake card."""

    def __init__(self, sectors, card=None, trailing_empty=True, empty_first=True):
        """trailing_empty: 'last' only on a separate empty partial after the data; False:
        'last' on the final data partial. empty_first: the first partial of each job
        carries no data and last=0 (what the real device did on 2026-10-13)."""
        self.sectors, self.card, self.jobs, self.next_job = sectors, card or {}, {}, 1
        self.trailing_empty, self.empty_first = trailing_empty, empty_first
        self.requests = []

    def size(self, n):
        k = n - RAW_FIRST_N
        if not 0 <= k < MAX_WINDOWS:
            return None
        left = self.sectors - (k << 10)
        return 0 if left <= 0 else min(WINDOW, left * 512)

    def obj(self, n):
        size = self.size(n)
        k = n - RAW_FIRST_N
        body = bytes(self.card.get(k, bytes(WINDOW))[:size])
        header = (be32(OBJECT_MAGIC) + bytes((2,)) + (8).to_bytes(2, 'big') + (13).to_bytes(2, 'big') + b'0094'
                  + be32(8) + be32(0xFFFFFFFF) + be32(n) + be32(len(body)) + bytes((0, 12)))
        return header + body + be32(ecrc(body)) + be32(len(body)) + be32(FOOTER_MAGIC)

    def call(self, type_byte, payload=b'', timeout=None):
        self.requests.append(type_byte)
        if type_byte == 0x54:
            path = payload[:payload.index(0)].decode()
            n = int(path.rsplit('/', 1)[1])
            if self.size(n) is None:
                return bytes((0, 0, 0, 0, 0xD4, 0)) + b'sample slot out of range\0'
            job, self.next_job = self.next_job, self.next_job + 1
            self.jobs[job] = (self.obj(n), u32(payload, payload.index(0) + 1))
            return bytes((0, 0, 0, 0, 0xD4, 1)) + be32(job) + be32(self.jobs[job][1]) + b'\0'
        if type_byte == 0x55:
            job, seq = u32(payload, 0), u32(payload, 4)
            data, block = self.jobs[job]
            if self.empty_first:
                if seq == 0:
                    return (bytes((0, 0, 0, 0, 0xD5, 1)) + be32(job) + be32(seq) + be32(0) + bytes((0,))
                            + be32(0) + be32(0))
                seq -= 1
            chunk = data[seq * block:(seq + 1) * block]
            last = int((seq * block >= len(data)) if self.trailing_empty else ((seq + 1) * block >= len(data)))
            return (bytes((0, 0, 0, 0, 0xD5, 1)) + be32(job) + be32(seq) + be32(0) + bytes((last,))
                    + be32(0) + be32(len(chunk)) + chunk)
        if type_byte == 0x56:
            job = u32(payload, 0)
            self.jobs.pop(job, None)
            return bytes((0, 0, 0, 0, 0xD6, 1)) + be32(job) + be32(0)
        raise AssertionError(f'fake device: unexpected request type {type_byte:#x}')


def selftest(log=print):
    import os
    import random
    import tempfile
    st = load_stfiles()
    rng = random.Random(5)
    sectors = 0x3A4000 + 0x123                     # partial last window
    card = {0: b'\xbe\xef\xba\xce' + bytes(WINDOW - 4), 8: b'SAMP' + os.urandom(WINDOW - 4),
            0x600: bytes([0xFF]) * WINDOW, 0xE90: bytes(rng.randrange(256) for _ in range(0x123 * 512))}
    dev = FakeV5(sectors, card)
    reader = Reader(dev, st, block=0x2000, rest=0, log=lambda *_: None)
    windows, card_bytes = reader.capacity()
    assert (windows, card_bytes) == (0xE91, sectors * 512), (hex(windows), hex(card_bytes))
    with tempfile.TemporaryDirectory() as tmp:
        snap = Snapshot(tmp)
        plan = [0, 8, 0x600, 0xE90, 0x48]
        read_windows(reader, snap, plan, log=lambda *_: None)
        for k in plan:
            expected = card.get(k, bytes(WINDOW))[:dev.size(RAW_FIRST_N + k)]
            assert snap.path(k).read_bytes() == expected, hex(k)
        before = len(dev.requests)
        read_windows(reader, Snapshot(tmp), plan, log=lambda *_: None)          # resume: nothing re-read
        assert len(dev.requests) == before
        rows = {r['window']: r for r in report(tmp, log=lambda *_: None)}
        assert rows['0x0000']['magics'] == {'MmcFs superblock': [0]}
        assert rows['0x0600']['kind'] == 'all FF' and rows['0x0048']['kind'] == 'all 00'
    assert set(dev.requests) <= set(READ_TYPES)
    try:
        ReadOnly(dev).call(0x57, b'')
        raise AssertionError('write request was not refused')
    except PermissionError:
        pass
    log(f'selftest OK: capacity {windows} windows ({windows * WINDOW / 2**20:.1f} MiB rounded up), '
        f'{len(dev.requests)} read requests, no other request type')
    return 0


BENCH_CONFIGS = ((0x800, 0.05), (0x800, 0.0), (0x1000, 0.0), (0x2000, 0.0), (0x4000, 0.0), (0x8000, 0.0))


def bench(dev, st, k, reference, configs=BENCH_CONFIGS, log=print):
    """Read window k once per (block, rest), no retries; compare with `reference` bytes."""
    results = []
    for block, rest in configs:
        reader = Reader(dev, st, block, rest, retries=0, log=log)
        try:
            body, seconds = reader.window(k)
            ok = body == reference
            results.append(dict(block=block, rest=rest, seconds=round(seconds, 2),
                                kib_s=round(len(body) / seconds / 1024, 1), identical=ok))
        except Exception as error:                      # report and go on with the next config
            results.append(dict(block=block, rest=rest, error=str(error)[:200]))
        r = results[-1]
        log(f'BENCH block {block:5d} rest {rest:.2f}: ' + (f"{r['seconds']:6.1f} s {r['kib_s']:7.1f} KiB/s "
            f"{'IDENTICAL' if r['identical'] else 'MISMATCH'}" if 'error' not in r else f"ERROR {r['error']}"))
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('selftest')
    for name in ('capacity', 'sample', 'dump'):
        p = sub.add_parser(name)
        p.add_argument('--port', default='Syntakt')
        p.add_argument('--block', type=int, default=0x800, help='bytes per partial (2048 proven on this host)')
        p.add_argument('--rest', type=float, default=0.05, help='seconds between partials (stfiles default)')
        if name != 'capacity':
            p.add_argument('--out', default=str(DEFAULT_OUT / datetime.date.today().isoformat()))
            p.add_argument('--windows-total', type=lambda s: int(s, 0),
                           help='skip the capacity probe and use this number of windows')
        if name == 'sample':
            p.add_argument('--windows', help='comma-separated window numbers (hex with 0x allowed)')
        if name == 'dump':
            p.add_argument('--first', type=lambda s: int(s, 0), default=0)
            p.add_argument('--last', type=lambda s: int(s, 0), help='inclusive; default: last window of the card')
    p = sub.add_parser('report')
    p.add_argument('dir')
    p = sub.add_parser('bench')
    p.add_argument('--port', default='Syntakt')
    p.add_argument('--window', type=lambda s: int(s, 0), default=0x1C8)
    p.add_argument('--reference', required=True, help='saved window file (e.g. snapshot/w01c8.bin)')
    args = ap.parse_args(argv)

    if args.cmd == 'selftest':
        return selftest()
    if args.cmd == 'report':
        report(args.dir)
        return 0
    st = load_stfiles()
    dev = st.Syntakt(args.port)
    try:
        if args.cmd == 'bench':
            results = bench(dev, st, args.window, Path(args.reference).read_bytes())
            out = Path(args.reference).parent / f'bench-{datetime.datetime.now():%Y%m%d-%H%M%S}.json'
            out.write_text(json.dumps(results, indent=1) + '\n', encoding='utf-8')
            return 0
        reader = Reader(dev, st, args.block, args.rest)
        if args.cmd == 'capacity':
            windows, card_bytes = reader.capacity()
            print(f'card: {card_bytes:#x} B = {card_bytes // 512:#x} sectors = {card_bytes / 2**20:.1f} MiB; '
                  f'{windows} windows of 512 KiB; high zone from 0x30000000: '
                  f'{max(0, card_bytes - 0x30000000) / 2**20:.1f} MiB')
            return 0
        windows = args.windows_total or reader.capacity()[0]
        print(f'card: {windows} windows ({windows * WINDOW / 2**20:.1f} MiB)', flush=True)
        snap = Snapshot(args.out)
        snap.index.update(windows_total=windows, block=args.block, rest=args.rest)
        snap.flush()
        if args.cmd == 'sample':
            plan = [int(x, 0) for x in args.windows.split(',')] if args.windows else default_sample(windows)
        else:
            last = windows - 1 if args.last is None else min(args.last, windows - 1)
            plan = list(range(args.first, last + 1))
        done, seconds = read_windows(reader, snap, plan)
        print(f'read {done} B in {seconds:.0f} s ({done / seconds / 1024 if seconds else 0:.1f} KiB/s); '
              f'snapshot in {snap.out}; run "report {snap.out}" for the summary')
        report(snap.out)
        return 0
    finally:
        dev.close()


if __name__ == '__main__':
    sys.exit(main())
