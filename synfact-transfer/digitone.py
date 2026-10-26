"""Host-only Digitone sound import, adapted from dn2st.

The parameter mapping and Sound v7 layout match the existing FM TONE converter.
No firmware images, emulator, MIDI access or third-party runtime are needed.
Chorus and arpeggiator data are deliberately ignored.
"""
import struct

import elektron_object as E

FM_TYPE = 65
# Serialized parameter order, expressed as sound slot numbers.
DN_ORDER = (0, 1, 9, 2, 10, 3, 11, 4, 12, 5, 13, 6, 14, 7, 15, 8, 16,
            *range(17, 50), *range(54, 64), *range(66, 77), 78, 77,
            50, 51, 52, 53, 64, 65)
ST_ORDER = (0, 1, 9, 2, 10, 3, 11, 4, 12, 5, 13, 6, 14, 7, 15, 8, 16,
            *range(17, 36), *range(37, 52), 53, 52, 36, *range(54, 70))
# Digitone slot -> FM TONE / Syntakt slot (SYN, filter, amp, LFO, portamento).
COPY = dict(zip(
    (17, 18, 19, 21, 22, 23, 24, *range(27, 38), 25, 38, 39, 40,
     55, 56, 57, 58, 59, 60, 61, 62, 63, 64,
     66, 67, 68, 69, 71, 72, 74, 73, 76,
     1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 77, 78),
    (18, 19, 20, 22, 23, 26, 25, *range(54, 65), 65, 66, 67, 68,
     28, 29, 30, 32, 33, 34, 35, 37, 38, 31,
     40, 42, 43, 44, 47, 48, 45, 46, 51,
     1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 52, 53)))
# Defaults of Syntakt parameters that have no source in the Digitone sound.
DEFAULTS = {27: 0, 36: 0, 39: 0, 41: 0x4000, 49: 0x100, 50: 0x100}
# Filter TYPE by name. Digitone 1.44 (formatter 40014b6c): OFF, LP2, HP, LP4.
# Syntakt 1.41 tracks 1-8 use FLTR page 9 (view set chosen at 40031de6), whose
# TYPE is LP, HP, EQ 1..EQ 5, OFF (formatter 400781d0). There is no 4-pole LP.
FILTER_TYPE = (7, 0, 1, 0)
END = b'\xba\xce\xf0\x0c'


def sound_slots(record):
    """Read v1..v6 sound parameters; upgrade only fields needed for conversion."""
    if len(record) < 8 or record[:4] != b'\xbe\xef\xba\xce':
        raise ValueError('Invalid Digitone sound record.')
    version = struct.unpack_from('>I', record, 4)[0]
    sizes = {1: 0x11B, 2: 0x11A, 3: 0x11A, 4: 0x12E, 5: 0x12E, 6: 0x12E}
    if version not in sizes:
        raise ValueError(f'Unsupported Digitone sound version {version}.')
    size = sizes[version]
    # Transfer also exports v2 records in a 302-byte buffer. The v2 footer
    # stays at byte 278; the following 20 bytes can be nonzero buffer residue.
    # Check the version's fixed footer offset, never search arbitrary payloads.
    valid_sizes = (size, 0x12E) if version == 2 else (size,)
    if len(record) not in valid_sizes or record[size - 4:size] != END:
        raise ValueError('Invalid Digitone sound record size or footer.')
    params = list(struct.unpack_from('>80H', record, 0x1C))
    if version == 1:
        params[71:73] = [0x3F00, 0x2700]
    if version <= 2:
        params[0] = 0
        params[73:77] = [0x4000] * 4
    if version <= 4:
        params[0] = 0
        params[77] = 0
    if version <= 5:
        # The v6 upgrade disables destinations of the removed LFO waveform 6.
        for wave, dest in ((9, 11), (10, 12)):
            if params[wave] == 6:
                params[dest] = 0
    words = dict(zip(DN_ORDER, params))
    for slot in (4, 12):
        target = words[slot] >> 8
        words[slot] = DN_ORDER[target] << 8 if target < len(DN_ORDER) else 0
    if words[54] > 0x3FF:
        words[54] = 0x300
    return words


def convert(obj, index=0):
    """Digitone data object -> (Syntakt data object, conversion notes)."""
    source = E.parse_object(obj)
    record = source['record']
    words = sound_slots(record)
    if source['b'] != 9 or source['c'] != 3 or source['d'] != struct.unpack_from('>I', record, 4)[0]:
        raise ValueError('The payload is not a Digitone sound object.')
    name = record[12:28].split(b'\0')[0].decode('latin1').upper()
    name = ''.join(c if 32 <= ord(c) < 127 else ' ' for c in name).strip() or 'DN SOUND'
    slots = {**DEFAULTS, **{dest: words[src] for src, dest in COPY.items()}}
    pair = words[20] >> 1
    slots[21], slots[69] = (pair % 19) << 8, (pair // 19) << 8
    slots[27] = FILTER_TYPE[words[54] >> 8] << 8
    notes = ['Digitone sound converted to FM TONE. Chorus and arpeggiator data ignored.']
    if words[54] >> 8 == 3:
        notes.append('Filter LP4 imported as LP (Syntakt has no 4-pole lowpass).')
    for slot in (4, 12):
        target = words[slot] >> 8
        dest = COPY.get(target, 21 if target == 20 else 0)
        slots[slot] = ST_ORDER.index(dest) << 8
        if target and target not in COPY:
            notes.append(f'LFO{1 if slot == 4 else 2} destination remapped to {"RTB1" if dest else "none"}.')
    for slot, neutral, label in ((50, 0x4000, 'RATIO C OFFSET'), (51, 0x4000, 'RATIO A OFFSET'),
                                 (52, 0x4000, 'RATIO B1 OFFSET'), (53, 0x4000, 'RATIO B2 OFFSET'),
                                 (70, 0, 'AMP DRIVE')):
        if words[slot] != neutral:
            notes.append(f'{label} not converted.')
    notes.append('Velocity/controller assignments are not imported; filter and effects may sound different.')
    result = bytearray(263)
    tags = struct.unpack_from('>I', record, 8)[0] & ((1 << 28) - 1)
    struct.pack_into('>III', result, 0, 0xBEEFBACE, 7, tags)
    result[12:28] = name.encode('ascii')[:15].ljust(16, b'\0')
    for position, slot in enumerate(ST_ORDER):
        struct.pack_into('>H', result, 28 + 2 * position, slots.get(slot, 0))
    # Digital voice, native sound flags and default pitch bend range.
    result[172:178] = bytes((FM_TYPE, 2, 0, 0, 1, 2))
    result[252], result[254] = 2, 100
    result[-4:] = END
    return E.build_object(bytes(result), dict(version=2, a=8, b=13, os='0044', c=3,
                                             d=7, e=0x700 + (index & 0x7F), tail=12)), notes
