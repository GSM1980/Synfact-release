"""Elektron Transfer sound files (.dnsnd Digitone, .stsnd Syntakt). Host side, no device.

File = ZIP { manifest.json, <payload name> }. The payload is a Data-API object, the
same layout files-v1/files_host.py writes for /samples (verified on Transfer files):
    header 31 B: u32 0xAC11D303, u8 2, u16 a, u16 b, char[4] OS string,
                 u32 c, u32 d, u32 e, u32 raw length, u8 compressed, u8 0x0C
    body       : compressed -> chain of {u32 BE length, LZ4 block}, ended by a u32 0 (Transfer
                 1.10.4 walks it until the 0 length -- Transfer.exe+1eaf0 -- and reads past the
                 buffer, crashing while "calculating checksum", if the terminator is missing)
    footer 12 B: u32 CRC-32 of the body with initial value 0xFFFFFFFF (as files_host.crc), u32 body length, u32 0xAAA1DAAA
"""
import io
import json
import struct
import zipfile
import zlib

MAGIC_OBJECT, FOOTER_MAGIC = 0xAC11D303, 0xAAA1DAAA
HEADER, FOOTER = 31, 12


def lz4_block_decompress(src, size):
    """Plain LZ4 block format decoder (no frame), output exactly `size` bytes."""
    out, i = bytearray(), 0
    while i < len(src):
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= len(src):
            break
        offset = src[i] | src[i + 1] << 8
        i += 2
        if not offset or offset > len(out):
            raise ValueError('bad LZ4 offset')
        match = (token & 15) + 4
        if token & 15 == 15:
            while True:
                b = src[i]
                i += 1
                match += b
                if b != 255:
                    break
        for _ in range(match):
            out.append(out[-offset])
    if len(out) != size:
        raise ValueError(f'LZ4: {len(out)} bytes, expected {size}')
    return bytes(out)


def lz4_block_compress_literals(data):
    """Valid LZ4 block holding `data` as literals only (no matches): trivially correct."""
    n = len(data)
    token = min(n, 15) << 4
    out = bytearray([token])
    if n >= 15:
        rest = n - 15
        while rest >= 255:
            out.append(255)
            rest -= 255
        out.append(rest)
    return bytes(out + data)


def block_chain(body):
    """The compressed body's blocks, walked as Transfer does; it must end, exactly at the end of
    the body, with a u32 0."""
    blocks, pos = [], 0
    while True:
        if pos + 4 > len(body):
            raise ValueError('compressed body: block chain without its 0 terminator')
        length = struct.unpack_from('>I', body, pos)[0]
        pos += 4
        if length == 0:
            break
        blocks.append(body[pos:pos + length])
        pos += length
    if pos != len(body):
        raise ValueError('compressed body: data after the 0 terminator')
    return blocks


def lz4_block_decompress_any(src):
    """LZ4 block of unknown output size (multi-block bodies)."""
    out, i = bytearray(), 0
    while i < len(src):
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                lit += src[i]
                i += 1
                if src[i - 1] != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= len(src):
            break
        offset = src[i] | src[i + 1] << 8
        i += 2
        match = (token & 15) + 4
        if token & 15 == 15:
            while True:
                match += src[i]
                i += 1
                if src[i - 1] != 255:
                    break
        for _ in range(match):
            out.append(out[-offset])
    return bytes(out)


def parse_object(obj):
    """-> dict(header fields, record bytes)."""
    if struct.unpack_from('>I', obj, 0)[0] != MAGIC_OBJECT:
        raise ValueError('not an Elektron data object')
    v, a, b = struct.unpack_from('>BHH', obj, 4)
    os_string = obj[9:13].decode('ascii')
    c, d, e, raw_len = struct.unpack_from('>4I', obj, 13)
    compressed, tail = obj[29], obj[30]
    body, footer = obj[HEADER:-FOOTER], obj[-FOOTER:]
    crc, body_len, magic = struct.unpack('>3I', footer)
    if magic != FOOTER_MAGIC or body_len != len(body):
        raise ValueError('bad object footer')
    if zlib.crc32(body, 0xFFFFFFFF) & 0xFFFFFFFF != crc:
        raise ValueError('object CRC mismatch')
    if compressed:
        blocks = block_chain(body)
        record = lz4_block_decompress(b''.join(blocks), raw_len) if len(blocks) == 1 else \
            b''.join(lz4_block_decompress_any(b) for b in blocks)[:raw_len]
    else:
        record = bytes(body[:raw_len])
    return dict(version=v, a=a, b=b, os=os_string, c=c, d=d, e=e, raw_len=raw_len, compressed=compressed,
                tail=tail, record=record)


def build_object(record, template):
    """New object for `record` with the header fields of `template` (parse_object dict)."""
    block = lz4_block_compress_literals(record)
    body = struct.pack('>I', len(block)) + block + struct.pack('>I', 0)
    header = (struct.pack('>IBHH', MAGIC_OBJECT, template['version'], template['a'], template['b']) +
              template['os'].encode('ascii') +
              struct.pack('>4I', template['c'], template['d'], template['e'], len(record)) + bytes((1, template['tail'])))
    assert len(header) == HEADER
    return header + body + struct.pack('>3I', zlib.crc32(body, 0xFFFFFFFF) & 0xFFFFFFFF, len(body), FOOTER_MAGIC)


def read_sound_file(path):
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read('manifest.json'))
        obj = z.read(manifest['Payload'])
    return manifest, parse_object(obj)


def write_sound_file(path, manifest, obj):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('manifest.json', json.dumps(manifest, indent=2).replace('\n', '\r\n'))
        z.writestr(manifest['Payload'], obj)
    with open(path, 'wb') as f:
        f.write(buf.getvalue())
