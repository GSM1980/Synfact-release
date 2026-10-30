"""First-install mods uploads with an offline eMMC/RPC emulator; no MIDI access."""
import base64
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'synfact-transfer'))
import core as C


class ModsInstallTests(unittest.TestCase):
    def setUp(self):
        self.manager = C.Manager()
        self.device = C.H.FakeHZ(0x200000)
        self.manager.dev = self.device
        self.mods = b'SMZ4' + struct.pack('>7I', 4, 0, 1, 0, 0, 0, 0) + bytes(8)
        self.logs = []
        self.uploads = []
        original_read = C.S.data_read

        def read(dev, path, **kwargs):
            if path == '/samples/449':
                return b''.join(self.uploads[-1])
            return original_read(dev, path, **kwargs)

        def write(dev, path, parts, **kwargs):
            # Both indexes must be present before the mods write starts.
            self.assertTrue(any(C.H.Index.unpack(self.device.window_bytes(k))
                                for k in C.H.INDEX_K))
            self.uploads.append(parts)
            return True

        for target in (patch.object(C.time, 'sleep', return_value=None),
                       patch.object(C.S, 'data_read', side_effect=read),
                       patch.object(C.S, 'data_write', side_effect=write)):
            target.start()
            self.addCleanup(target.stop)

    def upload(self, data=None, cancelled=lambda: False):
        return self.manager.run('machine_put', {
            'data': base64.b64encode(self.mods if data is None else data).decode(),
            'name': 'mods.smz4',
        }, self.logs.append, cancelled)

    def test_first_install_initializes_then_uploads_despite_stock_profile(self):
        result = self.upload()
        self.assertEqual(result['format'], 'SMZ4')
        self.assertEqual([k for k, _ in self.device.writes], list(C.H.INDEX_K))
        self.assertEqual(C.F.parse_object(b''.join(self.uploads[0]))['data'], self.mods)
        self.assertEqual(self.manager.profile, 'v6')
        self.assertTrue(any('Initializing' in line for line in self.logs))

    def test_existing_storage_and_samples_are_preserved(self):
        index = C.H.Index(seq=12, windows=2048, next_id=3, entries=[dict(
            id=2, first=0x602, blocks=1, bytes=68, frames=2, crc=123,
            created=0, flags=0, name='KEEP')])
        for k in C.H.INDEX_K:
            self.device.card[k] = index.pack().ljust(C.H.WINDOW, b'\0')
        self.device.card[0x602] = b'KEEP MY SAMPLE'.ljust(C.H.WINDOW, b'\0')
        before = dict(self.device.card)
        self.upload()
        self.assertEqual(self.device.card, before)
        self.assertEqual(self.device.writes, [])
        self.assertEqual(len(self.uploads), 1)

    def test_one_valid_index_is_enough_to_preserve_storage(self):
        self.device.card[0x600] = C.H.Index(seq=5, windows=2048).pack().ljust(C.H.WINDOW, b'\0')
        self.upload()
        self.assertEqual(self.device.writes, [])

    def test_invalid_mods_does_not_touch_storage(self):
        with self.assertRaises(ValueError):
            self.upload(b'invalid')
        self.assertEqual(self.device.requests, [])
        self.assertEqual(self.uploads, [])

    def test_read_failure_never_initializes_or_uploads(self):
        with patch.object(self.device, 'call', side_effect=C.S.DeviceError('no reply')):
            with self.assertRaises(RuntimeError):
                self.upload()
        self.assertEqual(self.device.writes, [])
        self.assertEqual(self.uploads, [])

    def test_damaged_indexes_are_not_reformatted(self):
        self.device.card[0x600] = b'HZFS' + bytes(C.H.WINDOW - 4)
        with self.assertRaisesRegex(RuntimeError, 'damaged'):
            self.upload()
        self.assertEqual(self.device.writes, [])
        self.assertEqual(self.uploads, [])

    def test_short_windows_cannot_trigger_initialization(self):
        with patch.object(C.H.D.Reader, 'window', return_value=(b'', 0)):
            with self.assertRaisesRegex(RuntimeError, 'Install the Synfact firmware'):
                self.upload()
        self.assertEqual(self.device.writes, [])
        self.assertEqual(self.uploads, [])

    def test_initialization_failure_stops_mods_upload(self):
        original_call = self.device.call
        def call(typ, payload=b'', timeout=None):
            if typ == C.S.T_WR_OPEN:
                raise C.S.DeviceError('write denied')
            return original_call(typ, payload, timeout)
        with patch.object(self.device, 'call', side_effect=call):
            with self.assertRaises(C.S.DeviceError):
                self.upload()
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.manager.profile, 'stock')

    def test_initialization_readback_failure_stops_mods_upload(self):
        original_call = self.device.call
        def call(typ, payload=b'', timeout=None):
            result = original_call(typ, payload, timeout)
            if typ == C.S.T_WR_CLOSE and self.device.writes:
                # Emulate storage that loses the index just written.
                self.device.card[self.device.writes[-1][0]] = bytes(C.H.WINDOW)
            return result
        with patch.object(self.device, 'call', side_effect=call):
            with self.assertRaisesRegex(RuntimeError, 'did not read back'):
                self.upload()
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.manager.profile, 'stock')

    def test_cancellation_never_initializes_or_uploads(self):
        with self.assertRaises(C.Cancelled):
            self.upload(cancelled=lambda: True)
        self.assertEqual(self.device.writes, [])
        self.assertEqual(self.uploads, [])


if __name__ == '__main__':
    unittest.main()
