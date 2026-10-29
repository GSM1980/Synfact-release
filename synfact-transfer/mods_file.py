"""Elekloader /mods Data RPC framing and offline SAFE v2 container checks.

Protocol matches elekloader.sdk.syntakt.mods_object. No SAMP header, sample
slot, file padding or HZFS operation is involved. The device loader performs
the executable descriptor/ABI checks before activating a bank on restart.
"""
import hashlib
import struct
import zlib

PATH = '/mods'
MAX_BYTES = 16 * 1024 * 1024
ABI = 0x454c0001


def crc(body):
    return zlib.crc32(body, 0xffffffff) & 0xffffffff


def pack(body):
    if len(body) > MAX_BYTES:
        raise ValueError('/mods file exceeds 16 MiB.')
    header = struct.pack('>IBHH4sIIII', 0xac11d303, 2, 8, 13, b'0094',
                         8, 0xffffffff, 1, len(body)) + b'\0\x0c'
    return header + body + struct.pack('>III', crc(body), len(body), 0xaaa1daaa)


def unpack(obj):
    if len(obj) < 43:
        raise ValueError('Truncated /mods Data object.')
    header, body, footer = obj[:31], obj[31:-12], obj[-12:]
    if (header[:9] != bytes.fromhex('ac11d303020008000d') or
            struct.unpack_from('>I', header, 13)[0] != 8 or
            struct.unpack_from('>I', header, 21)[0] != 1 or
            header[29:] != b'\0\x0c'):
        raise ValueError('Invalid /mods Data object header.')
    if len(body) > MAX_BYTES or struct.unpack_from('>I', header, 25)[0] != len(body):
        raise ValueError('Invalid /mods body length.')
    if footer != struct.pack('>III', crc(body), len(body), 0xaaa1daaa):
        raise ValueError('/mods Data object CRC/footer mismatch.')
    return body


def bank_info(data):
    """Check the SAFE v2 container and transport metadata, without executing it."""
    if len(data) < 80 or len(data) > MAX_BYTES or len(data) % 16:
        raise ValueError('Invalid Elekloader mods size.')
    h = struct.unpack_from('>20I', data)
    if h[:3] != (0x53414645, 2, ABI) or h[3] != len(data) or h[5] != 48:
        raise ValueError('Invalid Elekloader SAFE header or ABI.')
    if zlib.adler32(data[20:]) & 0xffffffff != h[4]:
        raise ValueError('Mods file integrity check failed.')
    features, ui, modules, nmods, descriptors, count = h[6:12]
    if (features & ~7 or count > 16 or nmods > 16 or not (count or nmods) or
            bool(features & 4) != bool(nmods) or (not nmods and modules) or
            (not features & 2 and ui) or (not count and features & 2)):
        raise ValueError('Invalid Elekloader bank directories.')
    magic, image_size, nrel, hooks, init, checksum, audio_offset, audio_size = h[12:20]
    image_start = 80 + 4 * nrel
    image_end = image_start + image_size
    audio_at = 48 + audio_offset
    if (magic != 0x534d5a34 or hooks or init != 0xffffffff or not image_size or
            image_size % 4 or image_end > min(len(data), 0x200000) or
            audio_at % 16 or audio_at < image_end or not 400 <= audio_size <= 723120 or
            audio_at + ((audio_size + 2047) & ~2047) != len(data)):
        raise ValueError('Invalid Elekloader MAIN/AUDIO bounds.')
    if (descriptors % 4 or descriptors + 40 * count > image_size or
            (features & 2 and (ui % 4 or ui + 32 * count > image_size)) or
            (nmods and (modules % 4 or modules + 16 * nmods > image_size))):
        raise ValueError('Invalid Elekloader directory bounds.')
    body_sum = sum(v[0] for v in struct.iter_unpack('>I', data[80:image_end])) & 0xffffffff
    if body_sum != checksum:
        raise ValueError('Mods MAIN checksum mismatch.')
    previous_end = 0
    for (at,) in struct.iter_unpack('>I', data[80:image_start]):
        if at % 2 or at < previous_end or at + 4 > image_size:
            raise ValueError('Invalid Elekloader relocation.')
        if struct.unpack_from('>I', data, image_start + at)[0] >= image_size:
            raise ValueError('Invalid Elekloader relocation target.')
        previous_end = at + 4
    if any(data[image_end:audio_at]) or any(data[audio_at + audio_size:]):
        raise ValueError('Invalid Elekloader alignment padding.')
    audio = data[audio_at:audio_at + audio_size]
    amagic, acount, asize, asum = struct.unpack_from('>4I', audio)
    if (amagic != 0x534d4133 or acount != count or asize != audio_size or
            400 + 16 * count > audio_size or zlib.adler32(audio[16:]) & 0xffffffff != asum):
        raise ValueError('Invalid Elekloader AUDIO header/checksum.')
    machines = [struct.unpack_from('>I', data, image_start + descriptors + 40 * i)[0]
                for i in range(count)]
    audio_types = [struct.unpack_from('>I', audio, 16 + 16 * i)[0] for i in range(count)]
    if (len(set(machines)) != count or any(not 64 <= t < 80 for t in machines) or
            sorted(machines) != sorted(audio_types)):
        raise ValueError('Invalid Elekloader machine types.')
    return dict(bytes=len(data), sites=0, machines=machines, image_bytes=image_size,
                format='SAFE', envelope_version=2, abi=ABI, loader='elekloader',
                modules=nmods, sha256=hashlib.sha256(data).hexdigest(), path=PATH)
