"""Bounded upload readback, exact content comparison and atomic publication.

Only the in-memory HZFS RPC simulator receives writes. No MIDI is opened.
"""
import base64
from collections import Counter
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'synfact-transfer'))
import core as C


class Counted(C.H.FakeHZ):
    def __init__(self):
        super().__init__(0x1000*1024)
        self.read_jobs, self.paths, self.received = {}, [], Counter()

    def call(self, kind, payload=b'', timeout=None):
        reply = super().call(kind, payload, timeout)
        if kind == C.S.T_RD_OPEN:
            path = payload.split(b'\0', 1)[0].decode()
            self.paths.append(path)
            self.read_jobs[C.S.u32(reply, 6)] = path
        elif kind == C.S.T_RD_PART:
            self.received[self.read_jobs[C.S.u32(payload, 0)]] += C.S.u32(reply, 23)
        return reply


class SampleReadbackTests(unittest.TestCase):
    def setUp(self):
        silence = contextlib.redirect_stdout(io.StringIO())
        silence.__enter__()
        self.addCleanup(silence.__exit__, None, None, None)
        sleep = patch.object(C.time, 'sleep')
        sleep.start()
        self.addCleanup(sleep.stop)
        self.dev = Counted()
        self.logs = []
        self.transport = C.Transport(self.dev, self.logs.append, lambda: False)
        self.store = C.H.Store(self.transport, C.S, block=2048, rest=0, log=self.logs.append)

    def path(self, k):
        return '/samples/'+str(C.H.RAW_FIRST_N+k)

    def install(self, count=2):
        index = C.H.Index(seq=3, windows=0x1000, next_id=count+1, entries=[dict(
            id=i+1, first=C.H.DATA_FIRST_K+i, blocks=1, bytes=68, frames=2,
            crc=0, created=1, flags=0, name=f'SAMPLE{i}') for i in range(count)])
        for k in C.H.INDEX_K:
            self.dev.card[k] = index.pack().ljust(C.H.WINDOW, b'\0')
        return index

    def upload(self):
        wav = io.BytesIO()
        with wave.open(wav, 'wb') as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(48000)
            writer.writeframes(bytes(128))
        manager = C.Manager()
        manager.dev, manager.profile = self.dev, 'v6'
        with patch.object(C.H, 'Store', return_value=self.store):
            return manager.run('hz_put', dict(name='NEW SAMPLE',
                data=base64.b64encode(wav.getvalue()).decode()), self.logs.append)

    def progress(self, phase):
        return [p for line in self.logs if line.startswith('@progress ')
                and (p := json.loads(line[10:]))['phase'] == phase]

    def test_small_upload_reads_one_partial_for_audio_and_publishes_checked_catalog(self):
        self.install(130)
        result = self.upload()
        self.assertEqual(result['bytes'], 192)
        audio_path = self.path(C.H.DATA_FIRST_K+130)
        self.assertEqual(self.dev.received[audio_path], 2048)
        self.assertEqual(set(self.dev.paths), {self.path(k) for k in C.H.INDEX_K} | {audio_path})
        self.assertEqual(sum(self.dev.received.values()), 32768)
        index = C.H.Index.unpack(self.dev.card[self.dev.writes[-1][0]])
        self.assertEqual(index.seq, 4)
        self.assertEqual(index.entries[-1], result)
        progress = self.progress('Verifying sample')
        self.assertEqual(progress[-1]['current'], 192)
        self.assertEqual(progress[-1]['total'], 192)
        self.assertTrue(all(0 <= p['current'] <= p['total'] == 192 for p in progress))
        self.assertEqual(self.dev.jobs, {})

    def test_prefix_boundaries_and_multiblock_files_compare_all_used_bytes(self):
        k = C.H.DATA_FIRST_K
        for length in (1, 64, 192, 2017, 2018, C.H.WINDOW-1, C.H.WINDOW, C.H.WINDOW+1, 2*C.H.WINDOW+65):
            with self.subTest(length=length):
                expected = (bytes(range(256))*((length+255)//256))[:length]
                self.store.write_at(k, expected)
                self.dev.paths.clear()
                self.dev.received.clear()
                count = -(-length//C.H.WINDOW)
                self.store.verify_blocks(k, count, expected)
                self.assertEqual(len(self.dev.paths), count)
                for i in range(count):
                    used = min(C.H.WINDOW, length-i*C.H.WINDOW)
                    expected_bytes = (C.H.WINDOW+C.D.HEADER+C.D.FOOTER if used == C.H.WINDOW
                                      else min(C.H.WINDOW+C.D.HEADER+C.D.FOOTER,
                                               -(-(C.D.HEADER+max(64, used))//2048)*2048))
                    self.assertEqual(self.dev.received[self.path(k+i)], expected_bytes)
                self.assertEqual(self.dev.jobs, {})

    def test_only_last_partial_window_uses_prefix_full_windows_keep_outer_crc(self):
        body = bytes(C.H.WINDOW+80)
        self.store.write_at(C.H.DATA_FIRST_K, body)
        with patch.object(self.store.reader, 'window', wraps=self.store.reader.window) as full, \
             patch.object(self.store.reader, 'window_prefix', wraps=self.store.reader.window_prefix) as prefix:
            self.store.verify_blocks(C.H.DATA_FIRST_K, 2, body)
        self.assertEqual(full.call_count, 1)
        self.assertEqual(prefix.call_count, 1)
        self.assertEqual(full.call_args.args, (C.H.DATA_FIRST_K,))
        self.assertEqual(prefix.call_args.args[0], C.H.DATA_FIRST_K+1)

    def test_transient_prefix_corruption_or_truncation_recovers_with_full_crc_read(self):
        body = b'A'*192
        k = C.H.DATA_FIRST_K
        self.store.write_at(k, body)
        original = self.dev.call
        for truncated in (False, True):
            injected = [False]
            def call(kind, payload=b'', timeout=None):
                reply = original(kind, payload, timeout)
                if kind == C.S.T_RD_PART and C.S.u32(reply, 23) and not injected[0]:
                    injected[0] = True
                    if truncated:
                        return reply[:-1]
                    reply = bytearray(reply)
                    reply[27+C.D.HEADER] ^= 1
                    return bytes(reply)
                return reply
            with self.subTest(truncated=truncated), patch.object(self.dev, 'call', side_effect=call), \
                 patch.object(self.store.reader, 'window', wraps=self.store.reader.window) as full:
                self.store.verify_blocks(k, 1, body)
            self.assertEqual(full.call_count, 1)
            self.assertEqual(self.dev.jobs, {})
        fallback = self.progress('Verifying sample (full read)')
        self.assertTrue(fallback)
        self.assertTrue(all(p['total'] == 0 for p in fallback))

    def test_persistently_corrupt_sample_never_changes_either_catalog(self):
        self.install()
        originals = {k:self.dev.card[k] for k in C.H.INDEX_K}
        write = self.store.write_at
        def corrupt(k, body):
            write(k, body)
            if k >= C.H.DATA_FIRST_K:
                raw = bytearray(self.dev.card[k])
                raw[len(body)-1] ^= 1
                self.dev.card[k] = bytes(raw)
        with patch.object(self.store, 'write_at', side_effect=corrupt):
            with self.assertRaisesRegex(RuntimeError, 'index NOT changed'):
                self.upload()
        self.assertEqual({k:self.dev.card[k] for k in C.H.INDEX_K}, originals)
        self.assertTrue(all(k >= C.H.DATA_FIRST_K for k, _ in self.dev.writes))
        self.assertEqual(self.dev.jobs, {})

    def test_sample_verification_cancellation_or_disconnect_never_retries_or_commits(self):
        prefix = self.store.reader.window_prefix
        for error in (C.Cancelled('Cancelled'), C.ConnectionLost('Disconnected')):
            self.install()
            originals = {k:self.dev.card[k] for k in C.H.INDEX_K}
            self.dev.writes.clear()
            def fail(*args, **kwargs):
                if kwargs.get('label') == 'sample':
                    raise error
                return prefix(*args, **kwargs)
            with self.subTest(error=type(error).__name__), \
                 patch.object(self.store.reader, 'window_prefix', side_effect=fail), \
                 patch.object(self.store.reader, 'window', wraps=self.store.reader.window) as full:
                with self.assertRaises(type(error)):
                    self.upload()
            self.assertFalse(full.called)
            self.assertEqual({k:self.dev.card[k] for k in C.H.INDEX_K}, originals)
            self.assertTrue(all(k >= C.H.DATA_FIRST_K for k, _ in self.dev.writes))

    def test_cancelled_native_prefix_closes_read_job(self):
        self.store.write_at(C.H.DATA_FIRST_K, b'A'*192)
        original = self.dev.call
        def cancel(kind, payload=b'', timeout=None):
            if kind == C.S.T_RD_PART:
                raise C.Cancelled('Cancelled')
            return original(kind, payload, timeout)
        with patch.object(self.dev, 'call', side_effect=cancel), \
             patch.object(self.store.reader, 'window', wraps=self.store.reader.window) as full:
            with self.assertRaises(C.Cancelled):
                self.store.verify_blocks(C.H.DATA_FIRST_K, 1, b'A'*192)
        self.assertFalse(full.called)
        self.assertEqual(self.dev.jobs, {})

    def test_expected_early_close_diagnostic_does_not_force_full_read(self):
        self.store.write_at(C.H.DATA_FIRST_K, b'A'*192)
        original = self.dev.call
        def call(kind, payload=b'', timeout=None):
            result = original(kind, payload, timeout)
            if kind == C.S.T_RD_CLOSE:
                return bytes((0, 0, 0, 0, 0xd6, 0))+b'Reader did not complete\0'
            return result
        with patch.object(self.dev, 'call', side_effect=call), \
             patch.object(self.store.reader, 'window', wraps=self.store.reader.window) as full:
            self.store.verify_blocks(C.H.DATA_FIRST_K, 1, b'A'*192)
        self.assertFalse(full.called)
        self.assertEqual(self.dev.received[self.path(C.H.DATA_FIRST_K)], 2048)
        self.assertEqual(self.dev.jobs, {})

    def test_missing_prefix_close_reply_falls_back_to_full_validation(self):
        self.store.write_at(C.H.DATA_FIRST_K, b'A'*192)
        original = self.dev.call
        failed = [False]
        def call(kind, payload=b'', timeout=None):
            result = original(kind, payload, timeout)
            if kind == C.S.T_RD_CLOSE and not failed[0]:
                failed[0] = True
                return None
            return result
        with patch.object(self.dev, 'call', side_effect=call), \
             patch.object(self.store.reader, 'window', wraps=self.store.reader.window) as full:
            self.store.verify_blocks(C.H.DATA_FIRST_K, 1, b'A'*192)
        self.assertEqual(full.call_count, 1)
        self.assertEqual(self.dev.jobs, {})

    def test_invalid_extent_is_rejected_before_reading(self):
        for count, data in ((0, b''), (0, b'A'), (2, b'A'), (1, bytes(C.H.WINDOW+1))):
            with self.subTest(count=count, length=len(data)), self.assertRaises(ValueError):
                self.store.verify_blocks(C.H.DATA_FIRST_K, count, data)
        self.assertEqual(self.dev.paths, [])

    def test_complete_read_progress_uses_announced_object_size_not_previous_write(self):
        self.transport.total, self.transport.verifying = 235, True
        self.store.read_blocks(C.H.DATA_FIRST_K, 1, 192)
        progress = self.progress('Verifying')
        data = [p for p in progress if p['current']]
        self.assertEqual(data[0]['current'], 2048)
        self.assertEqual(data[0]['total'], C.H.WINDOW+C.D.HEADER+C.D.FOOTER)
        self.assertLess(data[0]['current'], data[0]['total'])
        self.assertEqual(data[-1]['current'], data[-1]['total'])
        self.assertTrue(all(p['current'] <= p['total'] for p in data))

    def test_fragmented_read_header_does_not_reuse_upload_total(self):
        self.transport.total, self.transport.verifying = 8000, True
        with patch.object(self.dev, 'size', return_value=100):
            obj = C.S.data_read(self.transport, self.path(C.H.DATA_FIRST_K), block=8, rest=0, progress=False)
        self.assertEqual(C.validate_object(obj), obj)
        progress = self.progress('Verifying')
        self.assertEqual([p['total'] for p in progress if 0 < p['current'] < C.D.HEADER], [0, 0, 0])
        self.assertTrue(all(p['total'] == 143 for p in progress if p['current'] >= C.D.HEADER))
        self.assertEqual(progress[-1]['current'], 143)

    def test_sample_progress_scope_restores_previous_metadata_context_on_error(self):
        with self.transport.activity('Checking sample library'):
            with self.assertRaises(C.Cancelled):
                with self.transport.activity('Verifying sample', read_bytes=192):
                    raise C.Cancelled('Cancelled')
            self.assertEqual(self.transport.activity_label, 'Checking sample library')
            self.assertIsNone(self.transport.activity_read_bytes)
        self.assertIsNone(self.transport.activity_label)
        self.assertIsNone(self.transport.activity_read_bytes)


if __name__ == '__main__':
    unittest.main()
