"""Offline Digitone imports; device I/O is mocked."""
import base64
import io
import hashlib
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


def sound(version=6, *, record_tail=b'', **values):
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
    record.extend(record_tail)
    return C.E.build_object(record, dict(version=2, a=5, b=9, os='0051',
                                        c=3, d=version, e=0, tail=12))


def package(obj, products=('24', '30'), kind='Sound'):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('manifest.json', json.dumps(dict(ProductType=products, FileType=kind, Payload='sound')))
        z.writestr('sound', obj)
    return buf.getvalue()


class DigitoneImportTests(unittest.TestCase):
    def test_vendored_elekloader_converter_matches_provenance(self):
        root = Path(__file__).resolve().parents[1] / 'synfact-transfer'
        provenance = json.loads((root / 'PROVENANCE.json').read_text())
        module = next(m for m in provenance['modules'] if m['file'] == 'vendor/elekloader_digitone.py')
        actual = hashlib.sha256((root / module['file']).read_bytes()).hexdigest()
        self.assertEqual(actual, module['copy_sha256'])
        self.assertEqual(actual, module['source_sha256'])

    def test_digitone_detection_uses_payload_with_shared_product_types(self):
        original = sound(version=4)
        expected = DN.convert(original, index=16)[0]
        for products in (('24', '30'), ('24',), ('20', '28'), ('20', '28', '30'), ('99',), ()):
            with self.subTest(products=products):
                manifest, obj = C.read_package(package(original, products=products),
                                               allow_digitone=True, index=16)
                self.assertEqual(obj, expected)
                self.assertEqual(manifest['ProductType'], ['30'])

    def test_dnsnd_extension_selects_conversion_regardless_of_manifest_products(self):
        data = package(sound(version=4), products=['99'])
        for filename in ('CHAPPET DL.dnsnd', 'PLUCKY EÅ.DNSND'):
            with self.subTest(filename=filename):
                self.assertEqual(C.read_package(data, allow_digitone=True, filename=filename)[1],
                                 DN.convert(sound(version=4))[0])
                with self.assertRaises(ValueError):
                    C.read_package(data, filename=filename)

    def test_mapping_ratio_filter_lfo_and_tags(self):
        obj, notes = DN.convert(sound())
        C.validate_object(obj)
        rec = C.E.parse_object(obj)['record']
        slots = dict(zip(DN.ST_ORDER, struct.unpack_from('>71H', rec, 28)))
        self.assertEqual((slots[18], slots[21], slots[69], slots[27]), (0x300, 64 * 88, 0x4000, 1 << 8))
        self.assertEqual((slots[4], slots[12]), (DN.ST_ORDER.index(48) << 8, 54 << 8))
        self.assertEqual(struct.unpack_from('>I', rec, 8)[0], 0x201)
        self.assertEqual(rec[172], 65)
        self.assertTrue(notes)

    def test_amp_attack_minimum_preserves_longer_and_fractional_attacks(self):
        for attack in (0, 1, 0x7ff, 0x800, 0x801, 0x2000, 0x7f00):
            with self.subTest(attack=attack):
                obj, notes = DN.convert(sound(**{'66': attack, '67': 0x2345,
                                                '68': 0x5678, '69': 0x1234, '76': 0}))
                rec = C.E.parse_object(obj)['record']
                slots = dict(zip(DN.ST_ORDER, struct.unpack_from('>71H', rec, 28)))
                self.assertEqual(slots[40], max(attack, 0x800))
                self.assertEqual((slots[39], slots[41], slots[50]), (0, 0, 0x100))
                self.assertEqual((slots[42], slots[43], slots[44], slots[51]),
                                 (0x2345, 0x5678, 0x1234, 0))
                self.assertEqual(any('AMP ATK raised' in n for n in notes), attack < 0x800)

    def test_syn1_ratio_b_all_pairs_survive_current_value_codec(self):
        for raw in range(722):
            with self.subTest(raw=raw):
                rec = C.E.parse_object(DN.convert(sound(**{'20': raw}))[0])['record']
                slots = dict(zip(DN.ST_ORDER, struct.unpack_from('>71H', rec, 28)))
                self.assertEqual(slots[21], (raw // 2) * 88)
                self.assertEqual(2 * (slots[21] // 88), raw & ~1)
                self.assertEqual(slots[69], 0x4000)

    def test_ratio_offsets_and_lfo_destinations_use_elekloader_slots(self):
        for source, target in ((17, 18), (18, 19), (19, 20), (20, 21), (21, 22),
                               (22, 23), (23, 26), (24, 25), (50, 69), (51, 17),
                               (52, 24), (53, 70), (25, 65), (27, 54), (40, 68)):
            with self.subTest(source=source):
                values = {'4': DN.DN_ORDER.index(source) << 8,
                          '12': DN.DN_ORDER.index(source) << 8,
                          '50': 0x1234, '51': 0x2346, '52': 0x3456, '53': 0x4568}
                obj, notes = DN.convert(sound(**values))
                rec = C.E.parse_object(obj)['record']
                slots = dict(zip(DN.ST_ORDER, struct.unpack_from('>71H', rec, 28)))
                self.assertEqual((slots[4], slots[12]), (DN.ST_ORDER.index(target) << 8,) * 2)
                self.assertEqual(tuple(slots[i] for i in (69, 17, 24, 70)),
                                 (0x1234, 0x2346, 0x3456, 0x4568))
                self.assertEqual(struct.unpack_from('>H', rec, 0xa8)[0], 0x4568)
                self.assertFalse(any('OFFSET' in n or 'remapped' in n for n in notes))

    def test_filter_type_maps_by_name_to_digital_track_filter(self):
        # Digitone OFF, LP2, HP, LP4 -> Syntakt tracks 1-8: LP 0, HP 1, OFF 7.
        for kind, expected in ((0, 7), (1, 0), (2, 1), (3, 0), (4, 0)):
            with self.subTest(kind=kind):
                obj, notes = DN.convert(sound(**{'54': kind << 8}))
                rec = C.E.parse_object(obj)['record']
                slots = dict(zip(DN.ST_ORDER, struct.unpack_from('>70H', rec, 28)))
                self.assertEqual(slots[27], expected << 8)
                self.assertEqual(any('LP4' in note for note in notes), kind >= 3)

    def test_filter_parameters_copied_word_for_word(self):
        # Digitone FREQ, RESO, ENV, ATK, DEC, SUS, REL, BASE, WDTH, DEL.
        pairs = ((55, 28), (56, 29), (57, 30), (58, 32), (59, 33), (60, 34),
                 (61, 35), (62, 37), (63, 38), (64, 31))
        values = {str(dn): (0x11 + i) << 8 | i for i, (dn, _) in enumerate(pairs)}
        rec = C.E.parse_object(DN.convert(sound(**values))[0])['record']
        slots = dict(zip(DN.ST_ORDER, struct.unpack_from('>70H', rec, 28)))
        for dn, st in pairs:
            self.assertEqual(slots[st], values[str(dn)], (dn, st))
        self.assertEqual(slots[36], 0)

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

    def test_v2_transfer_buffer_preserves_conversion(self):
        original = sound(version=2)
        # BASIC SQUARE exported by Transfer has a 302-byte buffer containing
        # a 282-byte v2 record and a nonzero 20-byte suffix after its footer.
        padded = sound(version=2, record_tail=bytes(range(20)))
        self.assertEqual(DN.convert(padded), DN.convert(original))
        for filename in ('B001 BASIC SQUARE.dnsnd', ''):
            with self.subTest(filename=filename):
                manifest, obj = C.read_package(package(padded), allow_digitone=True,
                                               filename=filename, index=16)
                self.assertEqual(obj, DN.convert(original, index=16)[0])
                self.assertEqual(manifest['ProductType'], ['30'])
                C.validate_object(obj)

    def test_transfer_buffer_requires_v2_size_and_fixed_footer(self):
        padded = C.E.parse_object(sound(version=2, record_tail=bytes(range(20))))['record']
        bad_footer = bytearray(padded)
        bad_footer[278] ^= 1
        bad_footer[-4:] = DN.END
        for record in (padded[:-1], padded + b'\0', bad_footer,
                       C.E.parse_object(sound(version=2))['record'] + b'\0',
                       C.E.parse_object(sound(version=1, record_tail=bytes(19)))['record'],
                       C.E.parse_object(sound(version=3, record_tail=bytes(20)))['record']):
            with self.subTest(length=len(record), version=record[7]):
                with self.assertRaisesRegex(ValueError, 'size or footer'):
                    DN.sound_slots(record)
                with self.assertRaisesRegex(ValueError, 'size'):
                    C.read_package(package(C.E.build_object(record, C.E.parse_object(sound(version=record[7])))),
                                   allow_digitone=True)

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
            C.read_package(package(sound()[:-1]), allow_digitone=True)

    def test_shared_product_types_preserve_payload_validation(self):
        damaged = bytearray(sound())
        damaged[40] ^= 1
        with self.assertRaisesRegex(ValueError, 'CRC'):
            C.read_package(package(damaged), allow_digitone=True)
        source = C.E.parse_object(sound())
        source['b'] = 10
        with self.assertRaisesRegex(ValueError, 'not compatible'):
            C.read_package(package(C.E.build_object(source['record'], source)),
                           allow_digitone=True)

    def test_put_converts_before_write_and_verifies_record(self):
        manager = C.Manager()
        manager.dev = object()
        sent = []
        logs = []
        def write(dev, path, parts, **kwargs):
            sent.append(b''.join(parts))
            return True
        args = dict(path='/soundbanks/B/17', filename='TEST.DNSND',
                    data=base64.b64encode(package(sound(version=2, record_tail=bytes(range(20))),
                                                 products=['99'])).decode())
        with patch.object(C.S, 'data_write', side_effect=write), \
             patch.object(C.S, 'data_read', side_effect=lambda *a, **kw: sent[-1]):
            self.assertEqual(manager.run('put', args, logs.append), {'ok': True})
        self.assertEqual(len(sent), 1)
        self.assertEqual(C.E.parse_object(sent[0])['e'], 0x710)
        self.assertTrue(any('converted' in line for line in logs))

    def test_existing_syntakt_import_is_unchanged(self):
        obj = DN.convert(sound())[0]
        for products in (('30',), ('24', '30'), ('20', '28', '30')):
            with self.subTest(products=products):
                data = package(obj, products=products)
                self.assertEqual(C.read_package(data, allow_digitone=True)[1], obj)


if __name__ == '__main__':
    unittest.main()
