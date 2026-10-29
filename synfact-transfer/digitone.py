"""Transfer object framing for Elekloader's host-only Digitone converter.

The canonical mapping lives in elekloader/mods/fm-tone-st/tools/digitone.py;
vendor/elekloader_digitone.py is an unchanged snapshot (see PROVENANCE.json).
"""
import struct

import elektron_object as E
from elekloader_digitone import DN_ORDER, ST_ORDER, END, FM_TYPE, sound_slots, convert_record


def convert(obj, index=0):
    """Digitone data object -> (Elekloader FM TONE data object, notes)."""
    source = E.parse_object(obj)
    result, notes = convert_record(source['record'])
    if (source['b'] != 9 or source['c'] != 3 or
            source['d'] != struct.unpack_from('>I', source['record'], 4)[0]):
        raise ValueError('The payload is not a Digitone sound object.')
    return E.build_object(result, dict(version=2, a=8, b=13, os='0044', c=3,
                                      d=7, e=0x700 + (index & 0x7F), tail=12)), notes
