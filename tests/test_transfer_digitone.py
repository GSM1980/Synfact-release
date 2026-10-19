"""Offline Digitone imports; device I/O is mocked."""
import base64
import io
import json
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'synfact-transfer'))
import core as C
import digitone as DN


def sound(version=6, **values):
    size = {1: 0x11B, 2: 0x11A, 3: 0x11A}.get(version, 0x12E)
    record = bytearray(size)
    struct.pack_into('>III', record, 0, 0xBEEFBACE, version, 0x201)
    record[12:28] = b'DN TEST'.ljust(16, b'\0')
    slots = {50: 0x4000, 51: 0x4000, 52: 0x4000, 53: 0x4000,
             17: 0x300, 20: (19 * 3 + 7) << 1, 54: 0x200,
             4: DN.DN_ORDER.index(72) << 8, 12: DN.DN_ORDER.index(27) << 8}
    slots.update({int(k): v for k, v in values.items()})
    for i, slot in enumerate(DN.DN_ORDER):
        struct.pack_into('>H', record, 28 + 2 * i, slots.get(slot, 0))
    record[-4:] = DN.END
    return C.E.build_object(record, dict(version=2, a=5, b=9, os='0051',
                                        c=3, d=version, e=0, tail=12))


def package(obj, products=('20', '28'), kind='Sound'):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('manifest.json', json.dumps(dict(ProductType=products, FileType=kind, Payload='sound')))
        z.writestr('sound', obj)
    return buf.getvalue()


class DigitoneImportTests(unittest.TestCase):
    def test_mapping_ratio_filter_lfo_and_tags(self):
        obj, notes = DN.convert(sound())
        C.validate_object(obj)
        rec = C.E.parse_object(obj)['record']
        slots = dict(zip(DN.ST_ORDER, struct.unpack_from('>70H', rec, 28)))
        self.assertEqual((slots[18], slots[21], slots[69], slots[27]), (0x300, 7 << 8, 3 << 8, 3 << 8))
        self.assertEqual((slots[4], slots[12]), (DN.ST_ORDER.index(48) << 8, 54 << 8))
        self.assertEqual(struct.unpack_from('>I', rec, 8)[0], 0x201)
        self.assertEqual(rec[172], 65)
        self.assertTrue(notes)

    def test_chorus_and_arpeggiator_have_no_effect(self):
        original = sound()
        source = C.E.parse_object(sound(**{'75': 0x7F00}))
        rec = bytearray(source['record'])
        rec[0xFE:0x120] = bytes([127]) * (0x120 - 0xFE)
        changed = C.E.build_object(rec, source)
        self.assertEqual(DN.convert(original)[0], DN.convert(changed)[0])

    def test_supported_record_versions(self):
        for version in range(1, 7):
            with self.subTest(version=version):
                C.validate_object(DN.convert(sound(version))[0])

    def test_rejects_wrong_version_and_truncated_record(self):
        for record in (b'', b'\xbe\xef\xba\xce' + struct.pack('>I', 7),
                       C.E.parse_object(sound())['record'][:-1]):
            with self.assertRaises(ValueError):
                DN.sound_slots(record)

    def test_digitone_only_accepted_at_sound_destination(self):
        data = package(sound())
        with self.assertRaises(ValueError):
            C.read_package(data)
        with self.assertRaises(ValueError):
            C.read_package(package(sound(), kind='Project'), allow_digitone=True)
        with self.assertRaises(ValueError):
            C.read_package(package(sound(), products=['99']), allow_digitone=True)
        with self.assertRaises(ValueError):
            C.read_package(package(sound()[:-1]), allow_digitone=True)

    def test_put_converts_before_write_and_verifies_record(self):
        manager = C.Manager()
        manager.dev = object()
        sent = []
        logs = []
        def write(dev, path, parts, **kwargs):
            sent.append(b''.join(parts))
            return True
        args = dict(path='/soundbanks/B/17', data=base64.b64encode(package(sound())).decode())
        with patch.object(C.S, 'data_write', side_effect=write), \
             patch.object(C.S, 'data_read', side_effect=lambda *a, **kw: sent[-1]):
            self.assertEqual(manager.run('put', args, logs.append), {'ok': True})
        self.assertEqual(len(sent), 1)
        self.assertEqual(C.E.parse_object(sent[0])['e'], 0x710)
        self.assertTrue(any('converted' in line for line in logs))

    def test_existing_syntakt_import_is_unchanged(self):
        obj = DN.convert(sound())[0]
        data = package(obj, products=['30'])
        self.assertEqual(C.read_package(data, allow_digitone=True)[1], obj)


if __name__ == '__main__':
    unittest.main()
