"""Bounded index reads retain CRC checks and never initialize on read errors."""
import base64
import contextlib
import io
import json
from pathlib import Path
import struct
import sys
import unittest
import wave
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'synfact-transfer'))
import core as C


class CountingDevice(C.H.FakeHZ):
    def __init__(self):
        super().__init__(0x680 * 1024)
        self.received = 0

    def call(self, kind, payload=b'', timeout=None):
        reply = super().call(kind, payload, timeout)
        if kind == C.S.T_RD_PART:
            self.received += C.S.u32(reply, 23)
        return reply


class IndexReadTests(unittest.TestCase):
    def setUp(self):
        self.device = CountingDevice()
        self.logs = []
        self.transport = C.Transport(self.device, self.logs.append, lambda: False)
        self.store = C.H.Store(self.transport, C.S, block=2048, rest=0, log=self.logs.append)
        silent = contextlib.redirect_stdout(io.StringIO())
        silent.__enter__()
        self.addCleanup(silent.__exit__, None, None, None)
        sleep = patch.object(C.time, 'sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def install_index(self, count=2):
        index = C.H.Index(seq=3, windows=0x680, next_id=count + 1, entries=[dict(
            id=i + 1, first=C.H.DATA_FIRST_K + i, blocks=1, bytes=68, frames=2,
            crc=0, created=1, flags=0, name=f'SAMPLE{i}') for i in range(count)])
        for k in C.H.INDEX_K:
            self.device.card[k] = index.pack().ljust(C.H.WINDOW, b'\0')
        return index

    def phases(self):
        return {json.loads(s.removeprefix('@progress '))['phase']
                for s in self.logs if s.startswith('@progress ')}

    def test_small_indexes_read_one_data_partial_per_copy_and_close_jobs(self):
        expected = self.install_index()
        copies = self.store.read_index_copies()
        self.assertEqual([c.pack() for c in copies], [expected.pack()] * 2)
        self.assertEqual(self.device.received, 4096)
        self.assertEqual(self.device.jobs, {})
        self.assertEqual(self.device.writes, [])
        self.assertEqual(self.phases(), {'Checking sample library'})

    def test_large_index_reads_every_used_entry_and_validates_crc(self):
        expected = self.install_index(100)
        copies = self.store.read_index_copies()
        self.assertEqual([c.pack() for c in copies], [expected.pack()] * 2)
        self.assertEqual(self.device.received, 16384)
        self.assertEqual(self.device.jobs, {})

    def test_expected_partial_close_diagnostic_does_not_force_a_complete_read(self):
        expected = self.install_index()
        original = self.device.call
        def call(kind, payload=b'', timeout=None):
            reply = original(kind, payload, timeout)
            if kind == C.S.T_RD_CLOSE:
                return bytes((0, 0, 0, 0, 0xD6, 0)) + b'Reader did not complete\0'
            return reply
        with patch.object(self.device, 'call', side_effect=call):
            copies = self.store.read_index_copies()
        self.assertEqual([c.pack() for c in copies], [expected.pack()] * 2)
        self.assertEqual(self.device.received, 4096)
        self.assertEqual(self.device.jobs, {})

    def test_corrupt_or_truncated_prefix_retries_as_a_complete_checked_read(self):
        expected = self.install_index()
        original = self.device.call
        for truncate in (False, True):
            injected = [False]
            def call(kind, payload=b'', timeout=None):
                reply = original(kind, payload, timeout)
                if kind == C.S.T_RD_PART and not injected[0] and C.S.u32(reply, 23):
                    injected[0] = True
                    if truncate:
                        return reply[:-1]
                    reply = bytearray(reply)
                    reply[27 + C.D.HEADER + C.H.CRC_OFF] ^= 1
                    return bytes(reply)
                return reply
            with self.subTest(truncate=truncate), patch.object(self.device, 'call', side_effect=call):
                raw, size = self.store.read_index_raw(C.H.INDEX_K[0])
            self.assertEqual(C.H.Index.unpack(raw).pack(), expected.pack())
            self.assertEqual(size, C.H.WINDOW)
            self.assertEqual(self.device.jobs, {})
        self.assertGreater(self.device.received, C.H.WINDOW * 2)

    def test_missing_index_still_requires_full_object_crc_verification(self):
        self.assertEqual(self.store.read_index_copies(), [None, None])
        self.assertGreater(self.device.received, C.H.WINDOW * 2)
        self.assertEqual(self.device.writes, [])

    def test_unavailable_window_is_never_treated_as_an_empty_index(self):
        with patch.object(self.device, 'size', return_value=0):
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                self.store.read_index_copies()
        self.assertEqual(self.device.writes, [])
        self.assertEqual(self.device.jobs, {})

    def test_oversized_index_length_is_not_trusted(self):
        self.install_index()
        for k in C.H.INDEX_K:
            raw = bytearray(self.device.card[k])
            struct.pack_into('>I', raw, 36, 0xffffffff)
            self.device.card[k] = bytes(raw)
        self.assertEqual(self.store.read_index_copies(), [None, None])
        self.assertGreater(self.device.received, C.H.WINDOW * 2)

    def test_cancelled_prefix_closes_job_without_full_retry(self):
        self.install_index()
        original = self.device.call
        def call(kind, payload=b'', timeout=None):
            if kind == C.S.T_RD_PART:
                raise C.Cancelled('Cancelled')
            return original(kind, payload, timeout)
        with patch.object(self.device, 'call', side_effect=call), \
                patch.object(self.store.reader, 'window') as fallback:
            with self.assertRaises(C.Cancelled):
                self.store.read_index_raw(C.H.INDEX_K[0])
        fallback.assert_not_called()
        self.assertEqual(self.device.jobs, {})

    def test_index_commit_distinguishes_metadata_from_sample_transfer(self):
        self.install_index()
        index, copies = self.store.current()
        self.store.commit(index, copies)
        self.assertEqual(self.phases(), {'Checking sample library', 'Updating sample library',
                                        'Verifying sample library'})
        self.assertEqual(C.H.Index.unpack(self.device.card[self.device.writes[0][0]]).seq, 4)

    def test_sample_upload_reads_initial_index_pair_once(self):
        self.install_index()
        wav = io.BytesIO()
        with wave.open(wav, 'wb') as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(48000)
            writer.writeframes(bytes(128))
        manager = C.Manager()
        manager.dev, manager.profile = self.device, 'v6'
        with patch.object(C.H, 'Store', return_value=self.store), \
                patch.object(self.store, 'current', wraps=self.store.current) as current:
            result = manager.run('hz_put', dict(name='NEW SAMPLE',
                                 data=base64.b64encode(wav.getvalue()).decode()), self.logs.append)
        self.assertEqual(current.call_count, 1)
        self.assertEqual(result['id'], 3)
        self.assertNotIn('Downloading', self.phases())
        self.assertIn('Uploading', self.phases())
        self.assertIn('Verifying sample', self.phases())


if __name__ == '__main__':
    unittest.main()
