"""Host-side object builder/parser for FILES V1 extended slots. OFFLINE ONLY.

Produces/reads the same Data-object layout as a stock sample (31 B header . 64 B
slot header . payload . 12 B footer) so the unmodified Data API can carry it:
    stfiles.py put-obj /samples/65 my_file.obj      (device must run FILES V1)
    stfiles.py get     /samples/65 back.obj
This module sends nothing. Extended slots are N=65..480, 512 KiB apart; a file
longer than 512 KiB - 64 B simply owns the following slot numbers as well.

Slot header (64 B, device reads only the first three fields):
    +0x00 'SAMP'   +0x08 u32 BE frames (payload bytes = 2*frames, odd sizes padded)
    +0x0c name[16] +0x20 u32 BE exact payload length   +0x24 'FILE' marker
    +0x28 name continuation (24 B, cp1252, NUL padded)
"""
import struct
import zlib

SLOT_BYTES = 0x80000
HEADER = 0x40
FIRST_N, LAST_N = 65, 480
MRAM_START = 0x0F400000
BASE_ADDRESS = 0x380000       # byte address of /samples/N = N*0x80000 + 0x380000
MAGIC_OBJECT = 0xAC11D303
FOOTER_MAGIC = 0xAAA1DAAA


def crc(data):
    return zlib.crc32(data, 0xFFFFFFFF) & 0xFFFFFFFF


def address(n):
    return n * SLOT_BYTES + BASE_ADDRESS


def slots_needed(length):
    return -(-(HEADER + length + (length & 1)) // SLOT_BYTES)


def check_span(n, length):
    """Raise if the file would not fit between /samples/n and the MRAM image."""
    if not FIRST_N <= n <= LAST_N:
        raise ValueError(f'extended slot numbers are {FIRST_N}..{LAST_N}')
    end = address(n) + HEADER + length + (length & 1)
    if end > MRAM_START:
        raise ValueError(f'{end:#x} would pass the MRAM image at {MRAM_START:#x}')
    return slots_needed(length)


def build_object(n, data, name='FILE', os_version=b'0094'):
    """-> (header+body, footer) like stfiles.build_sample, for any length."""
    check_span(n, len(data))
    payload = data + b'\0' * (len(data) & 1)
    label = name.encode('cp1252')
    body = bytearray(HEADER)
    body[0:4] = b'SAMP'
    struct.pack_into('>I', body, 8, len(payload) // 2)
    body[12:28] = label[:16].ljust(16, b'\0')
    struct.pack_into('>I', body, 0x20, len(data))
    body[0x24:0x28] = b'FILE'
    body[0x28:0x40] = label[16:40].ljust(24, b'\0')
    body = bytes(body) + payload
    header = (struct.pack('>I', MAGIC_OBJECT) + bytes((2,)) + (8).to_bytes(2, 'big') + (13).to_bytes(2, 'big')
              + os_version[:4].ljust(4, b'0') + struct.pack('>I', 8) + struct.pack('>I', 0xFFFFFFFF)
              + struct.pack('>I', n) + struct.pack('>I', len(body)) + bytes((0, 12)))
    footer = struct.pack('>III', crc(body), len(body), FOOTER_MAGIC)
    assert len(header) == 31 and len(footer) == 12
    return header + body, footer


def parse_object(obj):
    """Parse an object read back with `stfiles.py get` -> dict (data is the exact file)."""
    if len(obj) < 31 + HEADER + 12:
        raise ValueError(f'object too short ({len(obj)} B)')
    header, body, footer = obj[:31], obj[31:-12], obj[-12:]
    if struct.unpack('>I', header[25:29])[0] != len(body):
        raise ValueError('header payload size does not match the body')
    if body[:4] != b'SAMP':
        raise ValueError('not a SAMP-style slot body')
    frames = struct.unpack('>I', body[8:12])[0]
    payload = body[HEADER:HEADER + 2 * frames]
    exact = struct.unpack('>I', body[0x20:0x24])[0] if body[0x24:0x28] == b'FILE' else len(payload)
    if exact > len(payload):
        raise ValueError('declared exact length exceeds the payload')
    name = (body[12:28].rstrip(b'\0') + body[0x28:0x40].rstrip(b'\0')).decode('cp1252', 'replace') \
        if body[0x24:0x28] == b'FILE' else body[12:28].rstrip(b'\0').decode('cp1252', 'replace')
    return dict(slot=struct.unpack('>I', header[21:25])[0], frames=frames, name=name, data=payload[:exact],
                crc_ok=struct.unpack('>I', footer[:4])[0] == crc(body),
                footer_ok=struct.unpack('>I', footer[8:])[0] == FOOTER_MAGIC and
                struct.unpack('>I', footer[4:8])[0] == len(body))
