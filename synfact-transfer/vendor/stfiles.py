#!/usr/bin/env python3
r"""List the files on a live Elektron Syntakt over USB MIDI, the way Elektron
Transfer does (device-0x10 MidiRpc, see docs/MIDI-SYSEX-RPC.md section 11).

`info` and `ls` are read-only (Ping, SoftwareVersion, DeviceUID, DataList).
`get` reads an object (DataRead*). `roundtrip` WRITES one sample slot
(DataWrite*), reads it back and compares; it refuses an occupied slot unless
--force. Close Elektron Transfer first: on Windows only one program can hold
the port.

    uv run --no-project --python 3.12 --with python-rtmidi python tools/stfiles.py --list-ports
    uv run --no-project --python 3.12 --with python-rtmidi python tools/stfiles.py info
    uv run --no-project --python 3.12 --with python-rtmidi python tools/stfiles.py ls /
    uv run --no-project --python 3.12 --with python-rtmidi python tools/stfiles.py ls / -r
    uv run --no-project --python 3.12 --with python-rtmidi python tools/stfiles.py ls /soundbanks/A
    ... ls /samples
    ... get /samples/5 scratch/slot5.obj
    ... roundtrip --gen 65536                 # random .bin into the first free slot
    ... roundtrip my.bin --slot 64 --clear-after
    ... add  --log scratch/stfiles.log  to keep a copy, and  --trace  for raw frames.

Windows/WinMM delivers a long SysEx as several 1024-byte pieces (python-rtmidi's
default buffers); the reader below reassembles them from F0 to F7 and also
asks for bigger buffers (MidiIn.set_buffer_size, python-rtmidi >= 1.6).

Framing (cross-checked against the open-source Elektroid client, which drives
the Syntakt with this protocol):

    F0 00 20 3C 10 00  <8-in-7 encoded message>  F7

The message is *8-in-7 encoded*: each group is one MSB-collector byte (bit 6
belongs to the first of the next 7 bytes) followed by up to 7 data bytes with
the top bit cleared. Section 11 of docs/MIDI-SYSEX-RPC.md reads the first
collector byte as a fixed "cmd" and says the body is raw; that only holds for
messages of 7 bytes or fewer with no byte >= 0x80, which is why Ping and
ReadDir "/" worked. Anything longer (a path like "/soundbanks/A") needs the
encoding. The reply's "cmd 0x24" is the collector carrying two high bits:
the device's own seq_lo and the response bit on the type byte.

Decoded message:

    request:  seq_hi seq_lo  00 00               type        payload...
    reply:    dseq_hi dseq_lo  seq_hi seq_lo     type|0x80   status  payload...

The reply echoes the request seq at bytes 2-3; status 0 means error and is
followed by a NUL-terminated error string.

DataList (0x53) request payload: path (cp1252, NUL) . start u32 BE . end u32 BE
. all u8 (Transfer/Elektroid send 0, 0, 1). Reply: status at byte 5, three
u32 BE at 6..17, entries from byte 18, each:

    name\0  has_children u8  kind u8
      kind 1 (directory): children u32
      kind 2 (object):    slot u32  size u32  operations u16  valid u8  metadata u8
"""
import argparse
import sys
import time

HDR = bytes((0xF0, 0x00, 0x20, 0x3C, 0x10, 0x00))

T_PING, T_VERSION, T_UID, T_DATALIST = 0x01, 0x02, 0x03, 0x53
NAMES = {0x01: "Ping", 0x02: "SoftwareVersion", 0x03: "DeviceUID",
         0x04: "Screenshot", 0x05: "StorageSpace", 0x06: "TempoRead",
         0x07: "TempoWrite", 0x09: "Query", 0x10: "FsSampleReadDir",
         0x11: "FsSampleCreateDir", 0x12: "FsSampleDeleteDir",
         0x13: "EnumerateFiles", 0x17: "ListRam", 0x18: "ClearRam",
         0x19: "Assign", 0x20: "DeleteFile", 0x21: "RenameFile",
         0x22: "GetFileInfoPath", 0x23: "GetFileInfoHash",
         0x28: "GetFileInfoPathV2", 0x29: "MemoryCompaction",
         0x30: "OpenRead", 0x31: "CloseRead", 0x32: "Read", 0x36: "ReadV2",
         0x40: "OpenWrite", 0x41: "CloseWrite", 0x42: "Write", 0x46: "WriteV2",
         0x50: "OsUpgradeStart", 0x51: "OsUpgradeWrite", 0x52: "OsUpgradeEnd",
         0x53: "DataList", 0x54: "DataReadOpen", 0x55: "DataReadPartial",
         0x56: "DataReadClose", 0x57: "DataWriteOpen",
         0x58: "DataWritePartial", 0x59: "DataWriteClose", 0x5A: "DataMove",
         0x5B: "DataCopy", 0x5C: "DataClear", 0x5D: "DataSwap",
         0x5E: "DataRename", 0x5F: "DataSetTags"}


# --------------------------------------------------------------- 8-in-7 codec
def encode87(raw):
    out = bytearray()
    for i in range(0, len(raw), 7):
        grp = raw[i:i + 7]
        coll = 0
        for j, b in enumerate(grp):
            if b & 0x80:
                coll |= 0x40 >> j
        out.append(coll)
        out.extend(b & 0x7F for b in grp)
    return bytes(out)


def decode87(packed):
    out = bytearray()
    for i in range(0, len(packed), 8):
        coll = packed[i]
        for j, b in enumerate(packed[i + 1:i + 8]):
            out.append((b & 0x7F) | (0x80 if coll & (0x40 >> j) else 0))
    return bytes(out)


def hexs(b, n=64):
    s = " ".join(f"{x:02x}" for x in b[:n])
    return s + (f" ... ({len(b)}B)" if len(b) > n else "")


def u32(b, o):
    return int.from_bytes(b[o:o + 4], "big")


def cstr(b, o):
    """-> (string, offset after the NUL)."""
    end = b.index(0, o) if 0 in b[o:] else len(b)
    return b[o:end].decode("cp1252", "replace"), end + 1


# ------------------------------------------------------------------ transport
class Syntakt:
    def __init__(self, port_sub, trace=False, timeout=3.0):
        import rtmidi
        self.trace = trace
        self.timeout = timeout
        self.seq = 1
        self.mi, self.mo = rtmidi.MidiIn(), rtmidi.MidiOut()
        ins, outs = self.mi.get_ports(), self.mo.get_ports()
        pi = next((i for i, n in enumerate(ins) if port_sub.lower() in n.lower()), None)
        po = next((i for i, n in enumerate(outs) if port_sub.lower() in n.lower()), None)
        if pi is None or po is None:
            raise SystemExit(f"no MIDI port matching {port_sub!r}\n  in:  {ins}\n  out: {outs}")
        self._pending = None                         # SysEx being reassembled
        if hasattr(self.mi, "set_buffer_size"):      # bigger SysEx buffers (WinMM)
            try:
                self.mi.set_buffer_size(16384, 16)   # before open_port
            except Exception as e:
                print(f"# set_buffer_size failed ({e}); relying on reassembly")
        try:
            self.mi.open_port(pi)
            self.mo.open_port(po)
        except Exception as e:
            raise SystemExit(f"cannot open {ins[pi]!r}/{outs[po]!r}: {e}\n"
                             "Is Elektron Transfer (or another MIDI app) still open?")
        self.mi.ignore_types(sysex=False, timing=True, active_sense=True)
        print(f"# ports: in={ins[pi]!r} out={outs[po]!r}")

    def close(self):
        self.mi.close_port()
        self.mo.close_port()

    def _next_sysex(self):
        """Return one complete F0..F7 message, reassembling WinMM pieces, or None."""
        while True:
            m = self.mi.get_message()
            if not m:
                return None
            chunk = bytes(m[0])
            if not chunk:
                continue
            if chunk[0] == 0xF0:
                if self._pending and self.trace:
                    print(f"  .. dropped incomplete SysEx ({len(self._pending)}B)")
                self._pending = bytearray(chunk)
            elif chunk[0] < 0x80 or chunk[0] == 0xF7:   # continuation piece
                if self._pending is None:
                    continue
                self._pending.extend(chunk)
            else:
                continue                             # channel/realtime message
            if self._pending and self._pending[-1] == 0xF7:
                done, self._pending = bytes(self._pending), None
                return done

    def call(self, type_byte, payload=b"", timeout=None):
        """Send one request; return the decoded reply message (bytes) or None."""
        seq = self.seq
        self.seq = (self.seq + 1) & 0x3FFF or 1
        msg = bytes(((seq >> 8) & 0xFF, seq & 0xFF, 0, 0, type_byte)) + payload
        frame = HDR + encode87(msg) + b"\xF7"
        while self.mi.get_message():                 # drain stale input
            pass
        self._pending = None
        if self.trace:
            print(f"  -> {NAMES.get(type_byte, hex(type_byte))} seq={seq}: {hexs(frame)}")
        self.mo.send_message(list(frame))
        end = time.time() + (timeout or self.timeout)
        while time.time() < end:
            data = self._next_sysex()
            if data is None:
                time.sleep(0.002)
                continue
            if len(data) < 8 or data[:6] != HDR:
                if self.trace:
                    print(f"  .. skipped non-Elektron: {hexs(data, 24)}")
                continue
            rx = decode87(data[6:-1])
            if self.trace:
                print(f"  <- {hexs(data)}\n     decoded: {hexs(rx)}")
            if len(rx) < 6:
                continue
            echo = (rx[2] << 8) | rx[3]
            if echo != seq:
                if self.trace:
                    print(f"  .. reply for seq {echo}, waiting for {seq}")
                continue
            if rx[4] != (type_byte | 0x80):
                print(f"  !! unexpected reply type 0x{rx[4]:02x} for 0x{type_byte:02x}")
                return None
            return rx
        print(f"  !! no reply to {NAMES.get(type_byte, hex(type_byte))} within {self.timeout}s")
        return None


# ------------------------------------------------------------------- commands
def cmd_info(dev):
    rx = dev.call(T_PING)
    if rx:
        dev_id, n = rx[5], rx[6]
        caps = rx[7:7 + n]
        name, _ = cstr(rx, 7 + n)
        print(f"device: {name!r}  (product id {dev_id})")
        print(f"advertised commands ({len(caps)}):")
        for c in caps:
            print(f"  0x{c:02x}  {NAMES.get(c, '?')}")
    rx = dev.call(T_VERSION)
    if rx:
        strs = [s.decode("cp1252", "replace") for s in rx[5:].split(b"\x00") if s]
        print(f"software version reply: {strs}   raw: {hexs(rx[5:])}")
    rx = dev.call(T_UID)
    if rx:
        print(f"device uid: {hexs(rx[5:])}")
    return 0


def datalist(dev, path):
    """-> (header u32s, [entry dicts]) or None on error."""
    payload = path.encode("cp1252") + b"\x00" + (0).to_bytes(4, "big") \
        + (0).to_bytes(4, "big") + b"\x01"
    rx = dev.call(T_DATALIST, payload)
    if rx is None:
        return None
    if rx[5] == 0:
        err, _ = cstr(rx, 6)
        print(f"  !! {path}: device says error: {err!r}")
        return None
    head = (u32(rx, 6), u32(rx, 10), u32(rx, 14))
    entries, p = [], 18
    try:
        while p < len(rx):
            name, p = cstr(rx, p)
            has_children, kind = rx[p], rx[p + 1]
            p += 2
            e = {"name": name, "children_flag": has_children, "kind": kind}
            if kind == 1:
                e["children"] = u32(rx, p)
                p += 4
            elif kind == 2:
                e["slot"] = u32(rx, p)
                e["size"] = u32(rx, p + 4)
                e["ops"] = int.from_bytes(rx[p + 8:p + 10], "big")
                e["valid"] = rx[p + 10]
                e["metadata"] = rx[p + 11]
                p += 12
            else:
                print(f"  !! unknown entry kind {kind} at byte {p - 2}; rest: {hexs(rx[p - 2:])}")
                break
            entries.append(e)
    except (IndexError, ValueError) as ex:
        print(f"  !! parse stopped at byte {p}: {ex}; raw tail: {hexs(rx[p:])}")
    return head, entries


def is_dir(e):
    return e["kind"] == 1 or (e["kind"] == 2 and e["children_flag"])


def fmt(e):
    if e["kind"] == 1:
        return f"D  {'':>5} {'':>9}  {e['name']}/   ({e['children']} entries)"
    slot = "" if e["children_flag"] else str(e["slot"])
    flags = f"ops={e['ops']:04x} valid={e['valid']} meta={e['metadata']}"
    t = "D" if e["children_flag"] else "F"
    return f"{t}  {slot:>5} {e['size']:>9}  {e['name']}{'/' if e['children_flag'] else ''}   {flags}"


def cmd_ls(dev, path, recursive, depth, _level=0):
    res = datalist(dev, path)
    if res is None:
        return 1
    head, entries = res
    ind = "  " * _level
    print(f"{ind}{path}   [hdr {head[0]} {head[1]} {head[2]}, {len(entries)} entries]")
    for e in entries:
        print(f"{ind}  {fmt(e)}")
    if recursive and _level < depth:
        for e in entries:
            if is_dir(e) and e["name"]:
                child = path.rstrip("/") + "/" + e["name"]
                cmd_ls(dev, child, recursive, depth, _level + 1)
    return 0


# ------------------------------------------------------------ Data object API
# Request/reply layouts (decoded message offsets; reply status at byte 5):
#   DataReadOpen    0x54  path\0 . chunk u32 . compression u8
#                         -> job u32 @6 . chunk u32 @10 . compression u8 @14
#   DataReadPartial 0x55  job u32 . seq u32
#                         -> job @6 . seq @10 . progress u32 @14 . last u8 @18
#                            . hash u32 @19 . size u32 @23 . data @27
#   DataReadClose   0x56  job u32                -> job @6 . size @10
#   DataWriteOpen   0x57  size u32 . path\0      -> job @6
#   DataWritePartial 0x58 job . seq . crc32 . len . data  -> job @6 . seq @10 . total @14
#   DataWriteClose  0x59  job . size             -> job @6 . size @10
#   DataClear       0x5c  path\0
# crc32 is zlib's crc32 seeded with 0xffffffff.
T_RD_OPEN, T_RD_PART, T_RD_CLOSE = 0x54, 0x55, 0x56
T_WR_OPEN, T_WR_PART, T_WR_CLOSE, T_CLEAR = 0x57, 0x58, 0x59, 0x5C
BLOCK = 0x2000          # bytes per partial, as Transfer/Elektroid
REST = 0.05             # pause between partials, as Elektroid


def be32(v):
    return (v & 0xFFFFFFFF).to_bytes(4, "big")


def ecrc(b):
    import zlib
    return zlib.crc32(b, 0xFFFFFFFF) & 0xFFFFFFFF


class DeviceError(Exception):
    pass


def _ok(rx, what):
    if rx is None:
        raise DeviceError(f"{what}: no reply")
    if rx[5] == 0:
        raise DeviceError(f"{what}: device error {cstr(rx, 6)[0]!r}")
    return rx


def data_read(dev, path, block=BLOCK, compression=0, progress=True):
    rx = _ok(dev.call(T_RD_OPEN, path.encode("cp1252") + b"\0" + be32(block)
                      + bytes((compression,))), f"DataReadOpen {path}")
    job = u32(rx, 6)
    print(f"  read open {path}: job={job} chunk={u32(rx, 10)} compression={rx[14]}")
    out, seq, last = bytearray(), 0, 0
    try:
        while not last:
            time.sleep(REST)
            rx = _ok(dev.call(T_RD_PART, be32(job) + be32(seq), timeout=10),
                     f"DataReadPartial seq {seq}")
            last, size = rx[18], u32(rx, 23)
            chunk = rx[27:27 + size]
            if len(chunk) != size:
                raise DeviceError(f"partial {seq}: announced {size}B, got {len(chunk)}B")
            out += chunk
            if progress:
                print(f"    <- partial {seq}: {size}B (total {len(out)}) "
                      f"progress={u32(rx, 14)} last={last} hash={u32(rx, 19):08x}")
            seq += 1
    finally:
        rx = dev.call(T_RD_CLOSE, be32(job))
        if rx is not None and rx[5]:
            print(f"  read close: job={u32(rx, 6)} size={u32(rx, 10)}")
    return bytes(out)


def data_write(dev, path, parts, block=BLOCK):
    """Write an object made of `parts` (list of bytes); each part starts a new
    partial, as Elektroid does for the sample footer."""
    total = sum(len(p) for p in parts)
    rx = _ok(dev.call(T_WR_OPEN, be32(total) + path.encode("cp1252") + b"\0"),
             f"DataWriteOpen {path}")
    job = u32(rx, 6)
    print(f"  write open {path}: job={job} size={total}")
    seq = sent = 0
    try:
        _write_parts(dev, job, parts, block)
    except DeviceError:
        rx = dev.call(T_WR_CLOSE, be32(job) + be32(total), timeout=10)
        print(f"  (closing job {job} after the error: "
              f"{'ok' if rx is not None and rx[5] else 'refused' if rx is not None else 'no reply'})")
        raise
    rx = _ok(dev.call(T_WR_CLOSE, be32(job) + be32(total), timeout=10), "DataWriteClose")
    print(f"  write close: job={u32(rx, 6)} size={u32(rx, 10)}"
          + ("" if u32(rx, 10) == total else f"  !! expected {total}"))
    return u32(rx, 10) == total


def _write_parts(dev, job, parts, block):
    seq = sent = 0
    for part in parts:
        for off in range(0, len(part), block):
            chunk = part[off:off + block]
            time.sleep(REST)
            rx = _ok(dev.call(T_WR_PART, be32(job) + be32(seq) + be32(ecrc(chunk))
                              + be32(len(chunk)) + chunk, timeout=10),
                     f"DataWritePartial seq {seq}")
            sent += len(chunk)
            dev_total = u32(rx, 14)
            print(f"    -> partial {seq}: {len(chunk)}B, device total {dev_total}"
                  + ("" if dev_total == sent else f"  !! expected {sent}"))
            seq += 1


# Sample object (Syntakt >= 1.40), from Elektroid: header 31B . slot header 64B
# . int16 BE mono 48 kHz frames . footer 12B. CRC covers slot header + frames.
MAX_SAMPLE_BYTES = 48000 * 5 * 2


def build_sample(payload, slot, name, os_ver=b"0082"):
    if len(payload) % 2:
        payload += b"\0"
    frames = len(payload) // 2
    body = (be32(0x53414D50) + bytes(4) + be32(frames)
            + name.encode("cp1252")[:16].ljust(16, b"\0") + bytes(36)) + payload
    header = (be32(0xAC11D303) + bytes((2,)) + (8).to_bytes(2, "big")
              + (13).to_bytes(2, "big") + os_ver[:4].ljust(4, b"0") + be32(8)
              + be32(0xFFFFFFFF) + be32(slot) + be32(len(body)) + bytes((0, 12)))
    # the device stores content_size = len(body) here (verified 1.41; Elektroid sends 12)
    footer = be32(ecrc(body)) + be32(len(body)) + be32(0xAAA1DAAA)
    assert len(header) == 31 and len(body) == 64 + len(payload)
    return header + body, footer


def parse_sample(obj):
    """-> dict with the pieces of a sample object read back from the device."""
    if len(obj) < 31 + 64 + 12:
        raise DeviceError(f"object too short ({len(obj)}B)")
    h, body, f = obj[:31], obj[31:-12], obj[-12:]
    return {"magic": u32(h, 0), "hver": h[4], "family": int.from_bytes(h[5:7], "big"),
            "device": int.from_bytes(h[7:9], "big"), "os": h[9:13],
            "ftype": u32(h, 13), "fver": u32(h, 17), "index": u32(h, 21),
            "psize": u32(h, 25), "compressed": h[29], "fsize": h[30],
            "smagic": u32(body, 0), "frames": u32(body, 8),
            "name": body[12:28].rstrip(b"\0").decode("cp1252", "replace"),
            "payload": body[64:], "crc_ok": u32(f, 0) == ecrc(body),
            "fmagic": u32(f, 8)}


def cmd_get(dev, path, out):
    data = data_read(dev, path)
    with open(out, "wb") as fh:
        fh.write(data)
    print(f"saved {len(data)}B to {out}")
    if path.startswith("/samples/"):
        s = parse_sample(data)
        print(f"  sample: name={s['name']!r} frames={s['frames']} crc_ok={s['crc_ok']} "
              f"os={s['os']!r} index={s['index']} compressed={s['compressed']}")
    return 0


def cmd_roundtrip(dev, args):
    import hashlib
    import os
    if args.file:
        src = open(args.file, "rb").read()
        label = args.file
    else:
        src = os.urandom(args.gen)
        label = os.path.join(os.path.dirname(args.log or "."), "roundtrip-in.bin")
        with open(label, "wb") as fh:
            fh.write(src)
        print(f"generated {len(src)} random bytes -> {label}")
    if not src:
        raise SystemExit("empty input")
    if len(src) > MAX_SAMPLE_BYTES:
        if not args.allow_oversize:
            raise SystemExit(f"{len(src)}B > {MAX_SAMPLE_BYTES}B (5 s of 48 kHz mono 16-bit); "
                             "--allow-oversize to try anyway")
        print(f"!! OVERSIZE TEST: {len(src)}B = {len(src) / MAX_SAMPLE_BYTES:.2f}x the "
              f"documented 5 s limit")

    res = datalist(dev, "/samples")
    if res is None:
        raise SystemExit("cannot list /samples")
    _, entries = res
    used = {e["slot"]: e for e in entries if e["kind"] == 2 and e["valid"]}
    slot = args.slot
    if slot is None:
        slot = next((n for n in range(1, 65) if n not in used), None)
        if slot is None:
            raise SystemExit("no free sample slot (1-64); pass --slot N --force")
        print(f"using free slot {slot}")
    elif not 1 <= slot <= 64:
        print(f"slot {slot} out of range 1-64")
        return 1
    elif slot in used and not args.force:
        print(f"SLOT OCCUPIED: slot {slot} holds {used[slot]['name']!r} "
              f"({used[slot]['size']}B). Use another slot or --force "
              f"(it is backed up first and restored at the end).")
        return 3
    path = f"/samples/{slot}"
    outdir = os.path.dirname(args.log or ".")

    backup = None
    if slot in used:
        print(f"\n== BACKUP {path} ({used[slot]['name']!r})")
        backup = data_read(dev, path, block=args.block)
        bfile = os.path.join(outdir, f"backup-slot{slot}.obj")
        with open(bfile, "wb") as fh:
            fh.write(backup)
        print(f"  {len(backup)}B saved to {bfile}")
        if len(backup) < 31 + 64 + 12:
            print("  backup looks too short; not touching the slot")
            return 1

    os_ver = b"0082"
    rx = dev.call(T_VERSION)
    if rx is not None and rx[5:9].isdigit():
        os_ver = bytes(rx[5:9])                  # e.g. b"0094" on 1.41
    obj, footer = build_sample(src, slot, args.name, os_ver)
    sha_in = hashlib.sha256(src).hexdigest()
    same = False
    stage = "write"
    try:
        print(f"\n== WRITE {label} ({len(src)}B = {len(src) / 96000:.2f} s of 48 kHz mono, "
              f"object {len(obj) + len(footer)}B, sha256 {sha_in[:16]}) -> {path}")
        t0 = time.time()
        data_write(dev, path, [obj, footer], block=args.block)
        print(f"  {time.time() - t0:.1f}s")

        stage = "list"
        time.sleep(0.3)
        print(f"\n== LIST /samples after write")
        res = datalist(dev, "/samples")
        for e in (res[1] if res else []):
            if e.get("slot") == slot:
                print(f"  {fmt(e)}")

        stage = "read back"
        print(f"\n== READ BACK {path}")
        t0 = time.time()
        back = data_read(dev, path, block=args.block)
        print(f"  {time.time() - t0:.1f}s, {len(back)}B")
        dump = os.path.join(outdir, f"roundtrip-slot{slot}.obj")
        with open(dump, "wb") as fh:
            fh.write(back)
        print(f"  raw object saved to {dump}")

        stage = "compare"
        s = parse_sample(back)
        got = s["payload"][:len(src)]
        sha_out = hashlib.sha256(got).hexdigest()
        print(f"\n  header: magic={s['magic']:08x} ver={s['hver']} family={s['family']} "
              f"device={s['device']} os={s['os']!r} type={s['ftype']} index={s['index']} "
              f"payload_size={s['psize']} compressed={s['compressed']}")
        print(f"  slot header: magic={s['smagic']:08x} frames={s['frames']} name={s['name']!r}")
        print(f"  footer: crc_ok={s['crc_ok']} magic={s['fmagic']:08x}")
        print(f"  payload: sent {len(src)}B, got back {len(s['payload'])}B")
        print(f"  identical object: {back == obj + footer}")
        same = got == src and len(s["payload"]) >= len(src)
        if not same:
            n = min(len(got), len(src))
            diff = next((i for i in range(n) if got[i] != src[i]), n)
            print(f"  first difference at byte {diff}: sent {hexs(src[diff:diff + 16])}"
                  f" / got {hexs(got[diff:diff + 16])}")
        print(f"\nRESULT: {'OK, same bytes' if same else 'MISMATCH'}  "
              f"in={sha_in[:16]} out={sha_out[:16]} ({len(src)}B)")
    except DeviceError as e:
        print(f"\nRESULT: FAILED during {stage}: {e}")
    finally:
        time.sleep(0.5)
        if backup is not None:
            print(f"\n== RESTORE {path} from backup")
            try:
                data_write(dev, path, [backup[:-12], backup[-12:]], block=args.block)
                again = data_read(dev, path, block=args.block, progress=False)
                print(f"restored {path}: "
                      f"{'OK, identical to backup' if again == backup else '!! DIFFERS from backup'}"
                      f"  (backup file kept: backup-slot{slot}.obj)")
            except DeviceError as e:
                print(f"!! RESTORE FAILED: {e}\n   the original is in backup-slot{slot}.obj; "
                      f"put it back with:  stfiles.py put-obj {path} backup-slot{slot}.obj")
        elif args.clear_after:
            rx = dev.call(T_CLEAR, path.encode("cp1252") + b"\0")
            print(f"cleared {path}: {'ok' if rx is not None and rx[5] else 'FAILED'}")
    return 0 if same else 2


def cmd_put_obj(dev, path, file):
    """Write a raw object file (e.g. a backup-slotN.obj) back to `path`."""
    obj = open(file, "rb").read()
    data_write(dev, path, [obj[:-12], obj[-12:]])
    again = data_read(dev, path, progress=False)
    print(f"{path}: {'OK, identical to file' if again == obj else '!! differs from file'}")
    return 0 if again == obj else 2


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)
            st.flush()

    def flush(self):
        for st in self.streams:
            st.flush()


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", default=argparse.SUPPRESS, help="MIDI port name substring (default Syntakt)")
    common.add_argument("--trace", action="store_true", default=argparse.SUPPRESS, help="print raw frames")
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="reply timeout, s (default 3)")
    common.add_argument("--log", default=argparse.SUPPRESS, help="also write all output to this file")
    ap = argparse.ArgumentParser(description=__doc__, parents=[common],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-ports", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the request frames, open no port")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("info", parents=[common], help="ping, version, uid")
    ls = sub.add_parser("ls", parents=[common], help="DataList a path")
    ls.add_argument("path", nargs="?", default="/")
    ls.add_argument("-r", "--recursive", action="store_true")
    ls.add_argument("--depth", type=int, default=3)
    g = sub.add_parser("get", parents=[common], help="read one object to a file")
    g.add_argument("path")
    g.add_argument("out")
    rt = sub.add_parser("roundtrip", parents=[common],
                        help="WRITE a .bin into a sample slot, read it back, compare")
    rt.add_argument("file", nargs="?", help="input .bin (else --gen)")
    rt.add_argument("--gen", type=int, default=65536,
                    help="random bytes to generate when no file is given")
    rt.add_argument("--slot", type=int, help="sample slot 1-64 (default: first free)")
    rt.add_argument("--force", action="store_true", help="allow overwriting a used slot")
    rt.add_argument("--name", default="RT TEST", help="sample name (16 chars)")
    rt.add_argument("--block", type=int, default=BLOCK, help="bytes per partial")
    rt.add_argument("--clear-after", action="store_true", help="DataClear the slot at the end")
    rt.add_argument("--allow-oversize", action="store_true",
                    help="send more than the documented 5 s (480000 B) to see what the device does")
    po = sub.add_parser("put-obj", parents=[common], help="write a raw object file (e.g. a backup) to a path")
    po.add_argument("path")
    po.add_argument("file")
    args = ap.parse_args()
    for k, v in (("port", "Syntakt"), ("trace", False), ("timeout", 3.0), ("log", None)):
        if not hasattr(args, k):
            setattr(args, k, v)

    if args.log:
        sys.stdout = Tee(sys.__stdout__, open(args.log, "w", encoding="utf-8"))
    print(f"# {time.strftime('%Y-%m-%d %H:%M:%S')}  stfiles {' '.join(sys.argv[1:])}")

    if args.list_ports:
        import rtmidi
        print("in: ", rtmidi.MidiIn().get_ports())
        print("out:", rtmidi.MidiOut().get_ports())
        return 0

    if args.dry_run:
        path = getattr(args, "path", "/")
        for t, pl in ((T_PING, b""), (T_DATALIST, path.encode("cp1252") + b"\0"
                                      + bytes(8) + b"\x01")):
            msg = bytes((0, 1, 0, 0, t)) + pl
            frame = HDR + encode87(msg) + bytes((0xF7,))
            print(f"{NAMES[t]}: {hexs(frame, 200)}")
        return 0

    dev = Syntakt(args.port, trace=args.trace, timeout=args.timeout)
    try:
        if args.cmd == "ls":
            return cmd_ls(dev, args.path, args.recursive, args.depth)
        if args.cmd == "get":
            return cmd_get(dev, args.path, args.out)
        if args.cmd == "roundtrip":
            return cmd_roundtrip(dev, args)
        if args.cmd == "put-obj":
            return cmd_put_obj(dev, args.path, args.file)
        return cmd_info(dev)
    except DeviceError as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        dev.close()


if __name__ == "__main__":
    sys.exit(main())
