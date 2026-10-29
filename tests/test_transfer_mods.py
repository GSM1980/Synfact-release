"""Elekloader /mods transfers through the real Data RPC reader/writer; no MIDI."""
import base64
import contextlib
import io
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'synfact-transfer'))
import core as C


def bank(image_bytes=24):
    # Valid module-only SAFE v2 geometry: one named module, no callbacks.
    image = struct.pack('>4I', 16, 0xffffffff, 0xffffffff, 0) + b'TEST\0'
    image = image.ljust(image_bytes, b'\0')
    body = bytes(4) + image  # relocation of the module's name pointer
    audio_at = (80 + len(body) + 15) & ~15
    audio = b'SMA3' + struct.pack('>3I', 0, 400, zlib.adler32(bytes(384))) + bytes(384)
    checksum = sum(v[0] for v in struct.iter_unpack('>I', body)) & 0xffffffff
    zone = b'SMZ4' + struct.pack('>7I', len(image), 1, 0, 0xffffffff, checksum, audio_at - 48, len(audio))
    result = bytearray(b'SAFE' + struct.pack('>11I', 2, C.M.ABI, 0, 0, 48, 4, 0, 0, 1, 0, 0) + zone + body)
    result += bytes(audio_at - len(result)) + audio + bytes(2048 - len(audio))
    struct.pack_into('>2I', result, 12, len(result), zlib.adler32(result[20:]))
    return bytes(result)


class ModsDevice:
    def __init__(self):
        self.requests, self.paths = [], []
        self.obj, self.pending = b'', bytearray()
        self.size, self.block = 0, 2048
        self.corrupt_crc = self.different_data = self.unsupported = False

    def call(self, kind, payload=b'', timeout=None):
        self.requests.append((kind, payload))
        reply = bytes(5) + b'\x01'
        if kind in (C.S.T_RD_OPEN, C.S.T_WR_OPEN):
            path = payload[4:] if kind == C.S.T_WR_OPEN else payload
            path = path.split(b'\0')[0].decode()
            self.paths.append(path)
            if path != '/mods' or self.unsupported:
                raise C.S.DeviceError('Unsupported path')
        if kind == C.S.T_WR_OPEN:
            self.size = C.S.u32(payload, 0)
            self.pending = bytearray()
            return reply + C.S.be32(1)
        if kind == C.S.T_WR_PART:
            job, seq, crc, size = struct.unpack_from('>4I', payload)
            chunk = payload[16:]
            assert job == 1 and len(chunk) == size and C.S.ecrc(chunk) == crc
            self.pending.extend(chunk)
            return reply + struct.pack('>3I', job, seq, len(self.pending))
        if kind == C.S.T_WR_CLOSE:
            if len(self.pending) == self.size:
                self.obj = bytes(self.pending)
            return reply + struct.pack('>2I', 1, len(self.pending))
        if kind == C.S.T_RD_OPEN:
            self.block = C.S.u32(payload, len(b'/mods\0'))
            self.read_obj = self.obj
            if self.different_data:
                self.read_obj = C.M.pack(C.M.unpack(self.obj) + b'\0')
            if self.corrupt_crc:
                changed = bytearray(self.read_obj)
                changed[-12] ^= 1
                self.read_obj = bytes(changed)
            return reply + struct.pack('>2I', 2, self.block) + b'\0'
        if kind == C.S.T_RD_PART:
            job, seq = struct.unpack('>2I', payload)
            chunk = self.read_obj[seq * self.block:(seq + 1) * self.block]
            last = (seq + 1) * self.block >= len(self.read_obj)
            return (reply + struct.pack('>3IB2I', job, seq, 0, last, C.S.ecrc(chunk), len(chunk)) + chunk)
        if kind == C.S.T_RD_CLOSE:
            return reply + struct.pack('>2I', 2, len(self.read_obj))
        raise AssertionError(f'Unexpected command {kind:#x}')


class ModsTransferTests(unittest.TestCase):
    def setUp(self):
        self.device = ModsDevice()
        self.manager = C.Manager()
        self.manager.dev = self.device
        self.logs = []
        self.data = bank()
        silence = contextlib.redirect_stdout(io.StringIO())
        silence.__enter__()
        self.addCleanup(silence.__exit__, None, None, None)
        sleep = patch.object(C.time, 'sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def upload(self, data=None, cancelled=lambda: False):
        return self.manager.run('machine_put', dict(name='test.smz4',
            data=base64.b64encode(self.data if data is None else data).decode()),
            self.logs.append, cancelled)

    def test_drop_upload_uses_only_mods_and_verifies_exact_bank(self):
        with patch.object(C.H, 'Store', side_effect=AssertionError('Mods must not access HZFS')):
            info = self.upload()
        self.assertEqual(info['path'], '/mods')
        self.assertEqual(info['loader'], 'elekloader')
        self.assertEqual(self.device.paths, ['/mods', '/mods'])
        self.assertEqual(C.M.unpack(self.device.obj), self.data)
        self.assertEqual(self.device.obj[31:-12], self.data)
        self.assertEqual(self.manager.profile, 'v7')
        self.assertTrue(any('verified' in line for line in self.logs))

    def test_larger_than_legacy_sample_slot_is_supported(self):
        data = bank(0x81000)
        self.assertGreater(len(data), C.F.SLOT_BYTES - C.F.HEADER)
        self.upload(data)
        self.assertEqual(C.M.unpack(self.device.obj), data)

    def test_download_does_not_depend_on_cached_stock_profile(self):
        self.device.obj = C.M.pack(self.data)
        result = self.manager.run('machine_get', {}, self.logs.append)
        self.assertEqual(result['path'], '/mods')
        self.assertEqual(self.manager.downloads[result['download']][0], self.data)
        self.assertEqual(self.device.paths, ['/mods'])

    def test_profile_detection_reads_mods_without_legacy_or_hzfs_probe(self):
        self.device.obj = C.M.pack(self.data)
        with patch.object(C.H, 'Store', side_effect=AssertionError('Unexpected HZFS probe')):
            self.assertEqual(self.manager.detect_profile(self.logs.append), 'v7')
        self.assertEqual(self.device.paths, ['/mods'])

    def test_invalid_bank_is_rejected_before_any_rpc(self):
        broken = bytearray(self.data)
        broken[-1] = 1
        for data in (b'invalid', broken, bytes(C.M.MAX_BYTES + 1)):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                self.upload(data)
        self.assertEqual(self.device.requests, [])

    def test_container_errors_are_rejected_even_with_valid_outer_checksum(self):
        for offset, value in ((8, 0), (24, 8), (36, 17), (40, 0xfffffffc),
                              (52, 0xfffffffc), (60, 1), (68, 1), (80, 1)):
            changed = bytearray(self.data)
            struct.pack_into('>I', changed, offset, value)
            struct.pack_into('>I', changed, 16, zlib.adler32(changed[20:]))
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.upload(changed)
        self.assertEqual(self.device.requests, [])

    def test_legacy_format_inspection_also_reports_mods_endpoint(self):
        legacy = b'SMZ4' + struct.pack('>7I', 4, 0, 1, 0, 0, 0, 0) + bytes(8)
        self.assertEqual(C.machine_info(legacy)['path'], '/mods')

    def test_readback_checks_data_and_crc_and_does_not_report_success(self):
        for mode in ('different_data', 'corrupt_crc'):
            with self.subTest(mode=mode):
                setattr(self.device, mode, True)
                self.logs.clear()
                with self.assertRaises((ValueError, RuntimeError)):
                    self.upload()
                self.assertFalse(any('uploaded and verified' in line for line in self.logs))
                setattr(self.device, mode, False)

    def test_unsupported_endpoint_has_no_sample_slot_fallback(self):
        self.device.unsupported = True
        with self.assertRaises(C.S.DeviceError):
            self.upload()
        self.assertEqual(self.device.paths, ['/mods'])

    def test_unconfirmed_write_never_starts_verification(self):
        with patch.object(C.S, 'data_write', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'not confirmed'):
                self.upload()
        self.assertEqual(self.device.paths, [])

    def test_cancel_before_write_has_no_device_access(self):
        with self.assertRaises(C.Cancelled):
            self.upload(cancelled=lambda: True)
        self.assertEqual(self.device.requests, [])

    def test_cancel_during_upload_closes_partial_job(self):
        with self.assertRaises(C.Cancelled):
            self.upload(cancelled=lambda: any(k == C.S.T_WR_PART for k, _ in self.device.requests))
        self.assertEqual(self.device.requests[-1][0], C.S.T_WR_CLOSE)
        self.assertEqual(self.device.obj, b'')
        self.assertEqual(self.device.paths, ['/mods'])

    def test_native_object_framing_keeps_odd_payload_and_rejects_invalid_objects(self):
        data = b'opaque odd!'
        obj = C.M.pack(data)
        self.assertEqual(C.M.unpack(obj), data)
        self.assertEqual(obj[31:-12], data)
        for offset in (0, 4, 8, 13, 21, 25, 29, 30, len(obj) - 12, len(obj) - 1):
            changed = bytearray(obj)
            changed[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                C.M.unpack(changed)


if __name__ == '__main__':
    unittest.main()
