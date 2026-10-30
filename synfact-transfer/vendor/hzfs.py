"""HZFS: sample files in the eMMC high zone of a Syntakt running MACHINE LOADER V6-HZ.

    selftest                                   offline: fake device, no MIDI
    ls                                         list files, slot map and free space
    get   NAME|ID  --out FILE.wav [--raw]      read a sample back (WAV 48 kHz mono 16 bit)
    put   FILE.wav [--name NAME] --yes-write-device   copy a sample in
    rm    NAME|ID  --yes-write-device          delete a sample (index only; data left in place)
    fsck  [--deep]                             check the index (and, with --deep, every file)
    init  --yes-write-device                   create an empty index (first use only)

Layout (docs/EMMC_HIGH_ZONE_FAT32.md): the high zone is cut in blocks of 512 KiB, the
raw windows of V6-HZ: block k = bytes [k*0x80000, (k+1)*0x80000), written and read as
Data /samples/(8185+k). Blocks 0x600 and 0x601 hold the index (copies A and B, the one
with the higher valid sequence wins); data blocks are 0x602 .. end of card. Each file is
contiguous, starts on a block, and is a stock-format sample body: 64 B 'SAMP' header
(frames at +8, name at +12) + 16-bit big-endian mono PCM at 48 kHz.

The PC owns the index. A put writes the data into blocks free in BOTH index copies,
reads it back, then commits a new index into the older copy and reads that back: a cut
before the commit leaves the previous state intact. rm only rewrites the index.
Write commands need --yes-write-device. Close Elektron Transfer first. Running any
command against the device is the user's decision.
"""
import argparse
import array
import contextlib
import datetime
import importlib.util
import json
from pathlib import Path
import struct
import sys
import time
import zlib

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # Local application snapshot; original preserved.
import emmc_dump as D  # noqa: E402

WINDOW = D.WINDOW
RAW_FIRST_N = D.RAW_FIRST_N
HZ_FIRST_K = 0x600
INDEX_K = (0x600, 0x601)
DATA_FIRST_K = 0x602
MAGIC, VERSION = b'HZFS', 1
HEAD, MAP_SLOTS, ENTRY = 0x40, 64, 0x40
MAP_OFF = HEAD
CRC_OFF = 0x20                     # header: magic, version, seq, count, block, first, windows, next_id, crc, length
ENTRIES_OFF = MAP_OFF + 4 * MAP_SLOTS
MAX_ENTRIES = (WINDOW - ENTRIES_OFF) // ENTRY
RATE = 48000
SAMP_HEADER = 0x40
ALLOWED_TYPES = (0x54, 0x55, 0x56, 0x57, 0x58, 0x59)    # Data read + Data write only


def crc(data):
    return zlib.crc32(data, 0xFFFFFFFF) & 0xFFFFFFFF


def be32(v):
    return (v & 0xFFFFFFFF).to_bytes(4, 'big')


def u32(b, o):
    return int.from_bytes(b[o:o + 4], 'big')


# ------------------------------------------------------------------ index
class Index:
    def __init__(self, seq=0, windows=0, next_id=1, entries=None, slot_map=None):
        self.seq, self.windows, self.next_id = seq, windows, next_id
        self.entries = entries or []          # dicts: id first blocks bytes frames crc created flags name
        self.slot_map = slot_map or [0] * MAP_SLOTS     # slot 65+i -> entry id (phase 2)

    def pack(self):
        if len(self.entries) > MAX_ENTRIES:
            raise ValueError('index full')
        body = bytearray(ENTRIES_OFF + ENTRY * len(self.entries))
        struct.pack_into(f'>{MAP_SLOTS}I', body, MAP_OFF, *self.slot_map)
        for n, e in enumerate(self.entries):
            name = e['name'].encode('utf-8')[:32]
            struct.pack_into('>8I32s', body, ENTRIES_OFF + n * ENTRY, e['id'], e['first'], e['blocks'], e['bytes'],
                             e['frames'], e['crc'], e['created'], e['flags'], name)
        body[0:4] = MAGIC
        struct.pack_into('>9I', body, 4, VERSION, self.seq, len(self.entries), WINDOW, DATA_FIRST_K, self.windows,
                         self.next_id, 0, len(body))
        struct.pack_into('>I', body, CRC_OFF, crc(bytes(body)))      # crc of everything, crc field as 0
        return bytes(body)

    @classmethod
    def unpack(cls, raw):
        """-> Index or None if this copy is not a valid HZFS index."""
        if len(raw) < HEAD or raw[:4] != MAGIC:
            return None
        version, seq, count, block, first, windows, next_id, check, length = struct.unpack_from('>9I', raw, 4)
        if (version != VERSION or block != WINDOW or first != DATA_FIRST_K or count > MAX_ENTRIES
                or length != ENTRIES_OFF + ENTRY * count or length > len(raw)):
            return None
        if crc(raw[:CRC_OFF] + bytes(4) + raw[CRC_OFF + 4:length]) != check:
            return None
        slot_map = list(struct.unpack_from(f'>{MAP_SLOTS}I', raw, MAP_OFF))
        entries = []
        for n in range(count):
            f = struct.unpack_from('>8I32s', raw, ENTRIES_OFF + n * ENTRY)
            entries.append(dict(id=f[0], first=f[1], blocks=f[2], bytes=f[3], frames=f[4], crc=f[5], created=f[6],
                                flags=f[7], name=f[8].rstrip(b'\0').decode('utf-8', 'replace')))
        return cls(seq, windows, next_id, entries, slot_map)

    def used(self):
        return [(e['first'], e['first'] + e['blocks']) for e in self.entries]

    def find(self, key):
        for e in self.entries:
            if e['name'] == key or (key.isdigit() and e['id'] == int(key)):
                return e
        raise KeyError(f'no file {key!r}')


def problems(index):
    """Consistency checks of one index -> list of messages (empty = consistent)."""
    out, spans, names, ids = [], [], set(), set()
    for e in index.entries:
        lo, hi = e['first'], e['first'] + e['blocks']
        if not (DATA_FIRST_K <= lo < hi <= index.windows):
            out.append(f"{e['name']}: blocks {lo:#x}..{hi:#x} outside the data area")
        if e['blocks'] != -(-e['bytes'] // WINDOW) or e['bytes'] != SAMP_HEADER + 2 * e['frames']:
            out.append(f"{e['name']}: size fields inconsistent")
        if e['name'] in names or e['id'] in ids:
            out.append(f"{e['name']}: duplicate name or id")
        names.add(e['name'])
        ids.add(e['id'])
        if e['id'] >= index.next_id:
            out.append(f"{e['name']}: id {e['id']} >= next_id {index.next_id}")
        spans.append((lo, hi, e['name']))
    spans.sort()
    for (a_lo, a_hi, a), (b_lo, b_hi, b) in zip(spans, spans[1:]):
        if b_lo < a_hi:
            out.append(f'{a} and {b} overlap')
    for slot, eid in enumerate(index.slot_map):
        if eid and eid not in ids:
            out.append(f'slot {65 + slot} -> missing id {eid}')
    return out


def allocate(blocks, indexes, windows):
    """First fit over blocks free in EVERY given index copy."""
    used = sorted({span for index in indexes if index for span in index.used()})
    k = DATA_FIRST_K
    for lo, hi in used:
        if lo - k >= blocks:
            return k
        k = max(k, hi)
    if windows - k >= blocks:
        return k
    raise ValueError(f'no {blocks} contiguous free blocks ({blocks * WINDOW / 2**20:.1f} MiB); '
                     f'free total {free_blocks(indexes[0], windows) * WINDOW / 2**20:.1f} MiB')


def free_blocks(index, windows):
    return windows - DATA_FIRST_K - sum(hi - lo for lo, hi in index.used())


def largest_hole(index, windows):
    k, best = DATA_FIRST_K, 0
    for lo, hi in sorted(index.used()):
        best, k = max(best, lo - k), max(k, hi)
    return max(best, windows - k)


# ------------------------------------------------------------------ audio
def read_wav(path):
    """-> (rate, mono samples as floats in [-1, 1)). PCM 8/16/24/32 bit, float 32, extensible."""
    data = Path(path).read_bytes()
    if data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise ValueError(f'{path}: not a RIFF/WAVE file')
    fmt = pcm = None
    pos = 12
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], int.from_bytes(data[pos + 4:pos + 8], 'little')
        body = data[pos + 8:pos + 8 + size]
        if cid == b'fmt ':
            fmt = body
        elif cid == b'data':
            pcm = body
        pos += 8 + size + (size & 1)
    if fmt is None or pcm is None:
        raise ValueError(f'{path}: missing fmt or data chunk')
    tag, channels, rate = struct.unpack_from('<HHI', fmt, 0)
    bits = struct.unpack_from('<H', fmt, 14)[0]
    if tag == 0xFFFE:
        tag = struct.unpack_from('<H', fmt, 24)[0]
    width = bits // 8
    usable = len(pcm) - len(pcm) % (width * channels)
    pcm = pcm[:usable]
    if tag == 1 and bits == 16:
        values, scale = array.array('h', pcm), 32768.0
    elif tag == 1 and bits == 32:
        values, scale = array.array('i', pcm), 2147483648.0
    elif tag == 3 and bits == 32:
        values, scale = array.array('f', pcm), 1.0
    elif tag == 1 and bits == 8:
        values, scale = [b - 128 for b in pcm], 128.0
    elif tag == 1 and bits == 24:
        values = [int.from_bytes(pcm[i:i + 3], 'little', signed=True) for i in range(0, len(pcm), 3)]
        scale = 8388608.0
    else:
        raise ValueError(f'{path}: unsupported WAV format tag {tag} / {bits} bit')
    if sys.byteorder != 'little' and isinstance(values, array.array):
        values.byteswap()
    if channels == 1:
        mono = [v / scale for v in values]
    else:
        mono = [sum(values[i:i + channels]) / (channels * scale) for i in range(0, len(values), channels)]
    return rate, mono


def resample(samples, rate):
    """4-point Hermite interpolation to 48 kHz (no anti-alias filter: downsampling from
    above 48 kHz can alias; prepare such files at 48 kHz for best quality)."""
    if rate == RATE or not samples:
        return samples
    step, n = rate / RATE, len(samples)
    out_len = int(n * RATE / rate)
    s = samples
    out = []
    for i in range(out_len):
        x = i * step
        j = int(x)
        t = x - j
        y0, y1 = s[j - 1] if j else s[0], s[j]
        y2 = s[j + 1] if j + 1 < n else s[-1]
        y3 = s[j + 2] if j + 2 < n else s[-1]
        c1 = 0.5 * (y2 - y0)
        c2 = y0 - 2.5 * y1 + 2 * y2 - 0.5 * y3
        c3 = 0.5 * (y3 - y0) + 1.5 * (y1 - y2)
        out.append(((c3 * t + c2) * t + c1) * t + y1)
    return out


def to_pcm16be(samples):
    values = array.array('h', (max(-32768, min(32767, round(v * 32768.0))) for v in samples))
    if sys.byteorder == 'little':
        values.byteswap()
    return values.tobytes()


def samp_body(pcm_be, name):
    frames = len(pcm_be) // 2
    header = bytearray(SAMP_HEADER)
    header[0:4] = b'SAMP'
    struct.pack_into('>I', header, 8, frames)
    header[12:28] = name.encode('cp1252', 'replace')[:16].ljust(16, b'\0')
    return bytes(header) + pcm_be, frames


def write_wav(path, pcm_be):
    values = array.array('h', pcm_be)
    if sys.byteorder == 'little':
        values.byteswap()
    data = values.tobytes()
    head = (b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVE' + b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, RATE,
            RATE * 2, 2, 16) + b'data' + struct.pack('<I', len(data)))
    Path(path).write_bytes(head + data)


# ------------------------------------------------------------------ device access
class Guard:
    """Transport wrapper: Data read/write requests only."""

    def __init__(self, dev):
        self.dev = dev

    def call(self, type_byte, payload=b'', timeout=None):
        if type_byte not in ALLOWED_TYPES:
            raise PermissionError(f'hzfs: refused request type 0x{type_byte:02x}')
        return self.dev.call(type_byte, payload, timeout=timeout)

    def activity(self, label):
        method = getattr(self.dev, 'activity', None)
        return method(label) if method else contextlib.nullcontext()


def data_object(n, body):
    header = (be32(D.OBJECT_MAGIC) + bytes((2,)) + (8).to_bytes(2, 'big') + (13).to_bytes(2, 'big') + b'0094'
              + be32(8) + be32(0xFFFFFFFF) + be32(n) + be32(len(body)) + bytes((0, 12)))
    return header + body + be32(crc(body)) + be32(len(body)) + be32(D.FOOTER_MAGIC)


class Store:
    def __init__(self, dev, st, block=0x2000, rest=0.0, write_block=0x2000, write_rest=0.0, log=print):
        self.dev, self.st, self.log = Guard(dev), st, log
        self.reader = D.Reader(dev, st, block, rest, log=log)
        self.write_block, self.write_rest = write_block, write_rest
        self.windows = None

    def activity(self, label):
        return self.dev.activity(label)

    # -- raw blocks
    def read_blocks(self, first, count, length):
        out = bytearray()
        for k in range(first, first + count):
            body, _ = self.reader.window(k)
            out += body
        return bytes(out[:length])

    def write_at(self, k, body):
        if k < HZ_FIRST_K:
            raise PermissionError('hzfs writes only into the high zone')
        if self.windows and k + -(-len(body) // WINDOW) > self.windows:
            raise ValueError('write would pass the end of the card')
        st, n = self.st, RAW_FIRST_N + k
        obj = data_object(n, body)
        rx = st._ok(self.dev.call(st.T_WR_OPEN, be32(len(obj)) + f'/samples/{n}'.encode() + b'\0'),
                    f'DataWriteOpen /samples/{n}')
        job, seq, sent = u32(rx, 6), 0, 0
        start = time.perf_counter()
        # Header+body in blocks, then the 12 B footer in a partial of its own, as Transfer,
        # Elektroid and stfiles do: with the footer glued to the last body bytes the
        # device answered "Footer was not processed" at close (hardware, 2026-10-13).
        head, footer = obj[:-D.FOOTER], obj[-D.FOOTER:]
        chunks = [head[off:off + self.write_block] for off in range(0, len(head), self.write_block)] + [footer]
        try:
            for chunk in chunks:
                time.sleep(self.write_rest)
                rx = st._ok(self.dev.call(st.T_WR_PART, be32(job) + be32(seq) + be32(crc(chunk)) + be32(len(chunk))
                                          + chunk, timeout=10), f'DataWritePartial {seq}')
                sent += len(chunk)
                if u32(rx, 14) != sent:
                    raise st.DeviceError(f'device total {u32(rx, 14)} != sent {sent}')
                seq += 1
        finally:
            rx = self.dev.call(st.T_WR_CLOSE, be32(job) + be32(len(obj)), timeout=10)
        if rx is None or not rx[5] or u32(rx, 10) != len(obj):
            detail = 'no reply' if rx is None else (f'device error {st.cstr(rx, 6)[0]!r}' if not rx[5]
                                                    else f'device size {u32(rx, 10)} != sent {len(obj)}')
            raise st.DeviceError(f'DataWriteClose /samples/{n}: not confirmed ({detail})')
        seconds = time.perf_counter() - start
        self.log(f'  wrote {len(body)} B at block {k:#06x} in {seconds:.1f} s ({len(body) / seconds / 1024:.0f} KiB/s)')

    # -- index
    def capacity(self):
        if self.windows is None:
            with self.activity('Checking storage capacity'):
                self.windows, _ = self.reader.capacity()
        return self.windows

    def read_index_raw(self, k, phase='Checking sample library'):
        """Read only used index bytes, retaining full validation for absent copies."""
        def required(raw):
            if raw[:4] != MAGIC:
                return HEAD
            count, length = u32(raw, 12), u32(raw, 36)
            return length if count <= MAX_ENTRIES and length == ENTRIES_OFF + ENTRY * count else HEAD

        with self.activity(phase):
            try:
                raw = self.reader.window_prefix(k, required)
                if Index.unpack(raw) is not None:
                    return raw, WINDOW
            except (ValueError, self.st.DeviceError) as error:
                if not getattr(error, 'retryable', True):
                    raise
                self.log(f'Checking index with a complete read: {error}')
            # Before declaring a copy missing/damaged, validate the whole
            # object's CRC. A failed prefix must never trigger initialization.
            raw, _ = self.reader.window(k)
            return raw, len(raw)

    def read_index_copies(self):
        copies = []
        for k in INDEX_K:
            raw, size = self.read_index_raw(k)
            if size != WINDOW:
                raise ValueError('Extended sample storage window is unavailable.')
            copies.append(Index.unpack(raw))
        return copies

    def current(self):
        copies = self.read_index_copies()
        valid = [c for c in copies if c]
        if not valid:
            raise RuntimeError('no valid HZFS index in blocks 0x600/0x601: run "init" first')
        best = max(valid, key=lambda c: c.seq)
        return best, copies

    def commit(self, index, copies):
        """Write `index` (seq + 1) over the older copy, then read it back."""
        seqs = [c.seq if c else -1 for c in copies]        # snapshot: `index` may be one of the copies
        target = 0 if seqs[0] <= seqs[1] else 1             # the older (or invalid) copy; A on a tie
        index.seq = max(seqs) + 1 if max(seqs) >= 0 else 1
        body = index.pack()
        with self.activity('Updating sample library'):
            self.write_at(INDEX_K[target], body)
        back, _ = self.read_index_raw(INDEX_K[target], 'Verifying sample library')
        if back[:len(body)] != body or Index.unpack(back) is None:
            raise RuntimeError(f'index copy {"AB"[target]} did not read back identical')
        self.log(f'  index copy {"AB"[target]} committed, sequence {index.seq}')


# ------------------------------------------------------------------ commands
def cmd_init(store):
    windows = store.capacity()
    copies = store.read_index_copies()
    if any(copies):
        raise RuntimeError('an HZFS index already exists; refusing to re-initialise')
    index = Index(seq=0, windows=windows)
    for target in (0, 1):                     # both copies, same content, sequence 1
        index.seq = 1
        body = index.pack()
        with store.activity('Initializing sample library'):
            store.write_at(INDEX_K[target], body)
        back, _ = store.read_index_raw(INDEX_K[target], 'Verifying sample library')
        if Index.unpack(back) is None:
            raise RuntimeError('fresh index did not read back')
    store.log(f'HZFS initialised: {windows - DATA_FIRST_K} data blocks '
              f'({(windows - DATA_FIRST_K) * WINDOW / 2**20:.0f} MiB)')


def cmd_ls(store, log=print):
    index, copies = store.current()
    log(f'HZFS sequence {index.seq} (copies: A {copies[0].seq if copies[0] else "invalid"}, '
        f'B {copies[1].seq if copies[1] else "invalid"}), {len(index.entries)} files')
    log(f"{'id':>4}  {'name':<32} {'seconds':>8} {'MiB':>7} {'blocks':>12}  slots")
    for e in sorted(index.entries, key=lambda e: e['first']):
        slots = [str(65 + i) for i, eid in enumerate(index.slot_map) if eid == e['id']]
        log(f"{e['id']:>4}  {e['name']:<32} {e['frames'] / RATE:>8.2f} {e['bytes'] / 2**20:>7.2f} "
            f"{e['first']:#06x}+{e['blocks']:<5} {','.join(slots)}")
    free = free_blocks(index, index.windows)
    log(f'free {free * WINDOW / 2**20:.1f} MiB, largest contiguous hole '
        f'{largest_hole(index, index.windows) * WINDOW / 2**20:.1f} MiB')
    return index


def cmd_put(store, wav, name=None, *, state=None):
    rate, mono = read_wav(wav)
    name = name or Path(wav).stem
    if len(name.encode('utf-8')) > 32:
        raise ValueError('name longer than 32 bytes')
    if rate != RATE:
        store.log(f'  resampling {rate} Hz -> {RATE} Hz (Hermite{", source above 48 kHz may alias" if rate > RATE else ""})')
    pcm = to_pcm16be(resample(mono, rate))
    body, frames = samp_body(pcm, name)
    index, copies = state if state is not None else store.current()
    if any(e['name'] == name for e in index.entries):
        raise ValueError(f'a file named {name!r} already exists (rm it first)')
    blocks = -(-len(body) // WINDOW)
    first = allocate(blocks, copies, index.windows)
    store.log(f'put {name!r}: {frames / RATE:.2f} s, {len(body)} B -> blocks {first:#06x}..{first + blocks - 1:#06x}')
    store.write_at(first, body)
    back = store.read_blocks(first, blocks, len(body))
    if back != body:
        raise RuntimeError('data did not read back identical; index NOT changed')
    entry = dict(id=index.next_id, first=first, blocks=blocks, bytes=len(body), frames=frames, crc=crc(body),
                 created=int(time.time()), flags=0, name=name)
    index.entries.append(entry)
    index.next_id += 1
    if problems(index):
        raise RuntimeError(f'refusing to commit an inconsistent index: {problems(index)}')
    store.commit(index, copies)
    return entry


def cmd_get(store, key, out, raw=False):
    index, _ = store.current()
    e = index.find(key)
    body = store.read_blocks(e['first'], e['blocks'], e['bytes'])
    if crc(body) != e['crc'] or body[:4] != b'SAMP' or u32(body, 8) != e['frames']:
        raise RuntimeError(f"{e['name']}: data does not match its index entry (crc/header)")
    if raw:
        Path(out).write_bytes(body)
    else:
        write_wav(out, body[SAMP_HEADER:])
    store.log(f"get {e['name']!r}: {e['frames'] / RATE:.2f} s -> {out}")
    return body


def cmd_rm(store, key):
    index, copies = store.current()
    e = index.find(key)
    index.entries.remove(e)
    index.slot_map = [0 if eid == e['id'] else eid for eid in index.slot_map]
    store.commit(index, copies)
    store.log(f"rm {e['name']!r}: blocks {e['first']:#06x}+{e['blocks']} are free (data left in place)")


def cmd_fsck(store, deep=False):
    index, copies = store.current()
    issues = problems(index)
    for name, copy in zip('AB', copies):
        if copy is None:
            issues.append(f'index copy {name} invalid (the other one is used)')
    if index.windows != store.capacity():
        issues.append(f'index says {index.windows} blocks, card has {store.capacity()}')
    if deep:
        for e in index.entries:
            body = store.read_blocks(e['first'], e['blocks'], e['bytes'])
            if crc(body) != e['crc'] or body[:4] != b'SAMP':
                issues.append(f"{e['name']}: data crc/header mismatch")
    for line in issues or ['HZFS consistent']:
        store.log(line)
    return issues


# ------------------------------------------------------------------ offline fake device
class FakeHZ(D.FakeV5):
    """FakeV5 (reads) + Data writes with the V6-HZ permission model."""

    def __init__(self, sectors, **kw):
        super().__init__(sectors, **kw)
        self.wjobs, self.writes, self.partials = {}, [], {}

    def window_bytes(self, k):
        return self.card.get(k, bytes(WINDOW))

    def call(self, type_byte, payload=b'', timeout=None):
        if type_byte not in (0x57, 0x58, 0x59):
            return super().call(type_byte, payload, timeout)
        self.requests.append(type_byte)
        ok = lambda t, extra=b'': bytes((0, 0, 0, 0, t | 0x80, 1)) + extra  # noqa: E731
        if type_byte == 0x57:
            total = u32(payload, 0)
            n = int(payload[4:payload.index(0, 4)].decode().rsplit('/', 1)[1])
            k = n - RAW_FIRST_N
            if not (HZ_FIRST_K <= k < D.MAX_WINDOWS):
                return bytes((0, 0, 0, 0, 0xD7, 0)) + b'Write: Permission denied\0'
            job, self.next_job = self.next_job, self.next_job + 1
            self.wjobs[job] = (k, n, total, bytearray())
            return ok(0x57, be32(job))
        if type_byte == 0x58:
            job, seq, check, length = u32(payload, 0), u32(payload, 4), u32(payload, 8), u32(payload, 12)
            chunk = payload[16:16 + length]
            if crc(chunk) != check:
                return bytes((0, 0, 0, 0, 0xD8, 0)) + b'crc\0'
            self.wjobs[job][3].extend(chunk)
            self.partials.setdefault(job, []).append(len(chunk))
            return ok(0x58, be32(job) + be32(seq) + be32(len(self.wjobs[job][3])))
        job = u32(payload, 0)
        k, n, total, data = self.wjobs.pop(job)
        sizes = self.partials.pop(job, [])
        if len(sizes) > 1 and sizes[-1] != D.FOOTER:       # observed on hardware 2026-10-13
            return bytes((0, 0, 0, 0, 0xD9, 0)) + b'Footer was not processed\0'
        body = bytes(data[D.HEADER:-D.FOOTER])
        if len(data) != total or D.unpack_object(bytes(data), n) != body:
            return bytes((0, 0, 0, 0, 0xD9, 0)) + b'bad object\0'
        if (k << 10) + -(-len(body) // 512) > self.sectors:
            return bytes((0, 0, 0, 0, 0xD9, 0)) + b'write past card\0'
        for off in range(0, len(body), WINDOW):
            w = k + off // WINDOW
            piece = body[off:off + WINDOW]
            old = bytearray(self.window_bytes(w))
            old[:len(piece)] = piece
            self.card[w] = bytes(old)
        self.writes.append((k, len(body)))
        return ok(0x59, be32(job) + be32(total))


def sine_wav(path, seconds, rate=48000, channels=1, bits=16):
    import math
    n = int(seconds * rate)
    frames = []
    for i in range(n):
        v = 0.5 * math.sin(2 * math.pi * 440 * i / rate)
        frames.append([v] * channels)
    if bits == 16:
        data = b''.join(struct.pack('<h', int(v * 32767)) for f in frames for v in f)
        tag = 1
    else:
        data = b''.join(struct.pack('<f', v) for f in frames for v in f)
        tag = 3
    fmt = struct.pack('<HHIIHH', tag, channels, rate, rate * channels * bits // 8, channels * bits // 8, bits)
    Path(path).write_bytes(b'RIFF' + struct.pack('<I', 4 + 8 + len(fmt) + 8 + len(data)) + b'WAVE' + b'fmt '
                           + struct.pack('<I', len(fmt)) + fmt + b'data' + struct.pack('<I', len(data)) + data)


def selftest(log=print):
    import tempfile
    st = D.load_stfiles()
    quiet = lambda *_: None  # noqa: E731
    dev = FakeHZ(0x3A4000)
    store = Store(dev, st, rest=0, log=quiet)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cmd_init(store)
        sine_wav(tmp / 'a.wav', 0.5)
        sine_wav(tmp / 'b.wav', 12.0, rate=44100, channels=2, bits=32)
        sine_wav(tmp / 'c.wav', 3.0)
        a = cmd_put(store, tmp / 'a.wav')
        b = cmd_put(store, tmp / 'b.wav', name='long pad')
        c = cmd_put(store, tmp / 'c.wav')
        assert (a['first'], b['first'], c['first']) == (0x602, 0x603, 0x603 + b['blocks'])
        cmd_get(store, 'long pad', tmp / 'b-back.wav')
        rate, back = read_wav(tmp / 'b-back.wav')
        assert rate == RATE and abs(len(back) - 12.0 * RATE) <= 1
        cmd_rm(store, 'long pad')
        # a longer file does not fit in the freed hole: goes after c, never over c
        sine_wav(tmp / 'd.wav', 20.0)
        d = cmd_put(store, tmp / 'd.wav')
        assert d['first'] == c['first'] + c['blocks'], hex(d['first'])
        # a short one reuses the hole left by "long pad"
        e = cmd_put(store, tmp / 'a.wav', name='short')
        assert e['first'] == b['first']
        assert cmd_fsck(store, deep=True) == []
        names = [x['name'] for x in cmd_ls(store, log=quiet).entries]
        assert sorted(names) == ['a', 'c', 'd', 'short']
        assert all(k >= HZ_FIRST_K for k, _ in dev.writes)
    assert set(dev.requests) <= set(ALLOWED_TYPES)
    log(f'selftest OK: init, put x5 (44.1 kHz stereo float resampled), get, rm, hole reuse, fsck; '
        f'{len(dev.writes)} writes, all in the high zone')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('selftest')
    parsers = {name: sub.add_parser(name) for name in ('ls', 'get', 'put', 'rm', 'fsck', 'init')}
    parsers['get'].add_argument('key')
    parsers['get'].add_argument('--out', required=True)
    parsers['get'].add_argument('--raw', action='store_true', help='save the SAMP body instead of a WAV')
    parsers['put'].add_argument('wav')
    parsers['put'].add_argument('--name')
    parsers['rm'].add_argument('key')
    parsers['fsck'].add_argument('--deep', action='store_true')
    for name in ('put', 'rm', 'init'):
        parsers[name].add_argument('--yes-write-device', action='store_true', help='required: writes the eMMC')
    for p in parsers.values():
        p.add_argument('--port', default='Syntakt')
        p.add_argument('--block', type=int, default=0x800,
                       help='read partial size (2048: 8 KiB read replies get lost on this Windows host at times)')
        p.add_argument('--rest', type=float, default=0.0, help='pause between read partials')
        p.add_argument('--write-block', type=int, default=0x2000)
        p.add_argument('--write-rest', type=float, default=0.0)
    args = ap.parse_args(argv)
    if args.cmd == 'selftest':
        return selftest()
    if args.cmd in ('put', 'rm', 'init') and not args.yes_write_device:
        raise SystemExit(f'refusing: "{args.cmd}" writes the eMMC; pass --yes-write-device')
    st = D.load_stfiles()
    dev = st.Syntakt(args.port)
    try:
        store = Store(dev, st, args.block, args.rest, args.write_block, args.write_rest)
        store.capacity()
        if args.cmd == 'init':
            cmd_init(store)
        elif args.cmd == 'ls':
            cmd_ls(store)
        elif args.cmd == 'get':
            cmd_get(store, args.key, args.out, args.raw)
        elif args.cmd == 'put':
            cmd_put(store, args.wav, args.name)
        elif args.cmd == 'rm':
            cmd_rm(store, args.key)
        elif args.cmd == 'fsck':
            return 1 if cmd_fsck(store, args.deep) else 0
        return 0
    finally:
        dev.close()


if __name__ == '__main__':
    sys.exit(main())
