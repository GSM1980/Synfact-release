"""USB read pacing, independent operation settings and progress accounting."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'synfact-transfer'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import core as C
import benchmark_transfer_reads as B


class ReadDevice:
    def __init__(self, obj, *, empty=1, max_block=8192):
        self.obj, self.empty, self.max_block = obj, empty, max_block
        self.offset, self.closed, self.requests = 0, False, []

    def call(self, kind, payload=b'', timeout=None):
        self.requests.append(kind)
        reply = bytearray(27)
        reply[5] = 1
        reply[6:10] = C.S.be32(1)
        if kind == C.S.T_RD_OPEN:
            self.block = min(C.S.u32(payload, len(payload) - 5), self.max_block)
            reply[10:14] = C.S.be32(self.block)
        elif kind == C.S.T_RD_PART:
            if self.empty:
                self.empty -= 1
                return bytes(reply)
            part = self.obj[self.offset:self.offset + self.block]
            self.offset += len(part)
            reply[18] = int(self.offset == len(self.obj))
            reply[23:27] = C.S.be32(len(part))
            return bytes(reply) + part
        elif kind == C.S.T_RD_CLOSE:
            self.closed = True
        else:
            raise AssertionError('Unexpected device write')
        return bytes(reply)


class TransferPerformanceTests(unittest.TestCase):
    def setUp(self):
        silence = contextlib.redirect_stdout(io.StringIO())
        silence.__enter__()
        self.addCleanup(silence.__exit__, None, None, None)
        self.obj = C.E.build_object(bytes(range(256)) * 100,
                                    dict(version=2, a=8, b=13, os='0044', c=3, d=7, e=0, tail=12))

    def test_fast_read_handles_empty_partials_and_negotiated_smaller_blocks(self):
        dev = ReadDevice(self.obj, empty=2, max_block=4096)
        with patch.object(C.S.time, 'sleep') as sleep:
            result = C.S.data_read(dev, '/samples/1', block=8192, rest=0, progress=False)
        self.assertEqual(C.validate_object(result), self.obj)
        sleep.assert_not_called()
        self.assertTrue(dev.closed)

    def test_cancelled_read_still_closes_job(self):
        dev = ReadDevice(self.obj)
        call = dev.call
        def cancel(kind, *args, **kwargs):
            if kind == C.S.T_RD_PART:
                raise C.Cancelled('Cancelled')
            return call(kind, *args, **kwargs)
        dev.call = cancel
        with self.assertRaises(C.Cancelled):
            C.S.data_read(dev, '/samples/1', block=8192, rest=0)
        self.assertTrue(dev.closed)

    def test_readers_keep_their_own_pacing_and_leave_write_default_unchanged(self):
        default = C.S.REST
        dev = C.H.FakeHZ(0x200000)
        first = C.D.Reader(dev, C.S, 8192, 0)
        second = C.D.Reader(dev, C.S, 2048, 0.02)
        self.assertEqual(C.S.REST, default)
        def read(device, path, **kwargs):
            n = int(path.rsplit('/', 1)[1])
            return C.H.data_object(n, b'CHECKED')
        with patch.object(C.S, 'data_read', side_effect=read) as mocked:
            self.assertEqual(first.window(0)[0], b'CHECKED')
            self.assertEqual(second.window(0)[0], b'CHECKED')
            self.assertEqual(first.window(0)[0], b'CHECKED')
        self.assertEqual([c.kwargs['rest'] for c in mocked.call_args_list], [0, 0.02, 0])
        self.assertEqual([c.kwargs['block'] for c in mocked.call_args_list], [8192, 2048, 8192])

    def test_write_pacing_and_separate_footer_survive_fast_reader(self):
        C.D.Reader(object(), C.S, 8192, 0)
        chunks = []
        def call(kind, payload=b'', timeout=None):
            reply = bytearray(18)
            reply[5] = 1
            reply[6:10] = C.S.be32(1)
            if kind == C.S.T_WR_PART:
                chunks.append(payload[16:])
                reply[14:18] = C.S.be32(sum(map(len, chunks)))
            elif kind == C.S.T_WR_CLOSE:
                reply[10:14] = C.S.be32(sum(map(len, chunks)))
            return bytes(reply)
        dev = type('Writer', (), {'call': staticmethod(call)})()
        with patch.object(C.S.time, 'sleep') as sleep:
            self.assertTrue(C.S.data_write(dev, '/samples/1', [self.obj[:-12], self.obj[-12:]],
                                          block=8192, rest=C.WRITE_REST))
        self.assertEqual(b''.join(chunks), self.obj)
        self.assertEqual(chunks[-1], self.obj[-12:])
        self.assertTrue(all(c.args == (0.05,) for c in sleep.call_args_list))
        self.assertEqual(sleep.call_count, len(chunks))

    def test_download_uses_fast_settings_and_keeps_integrity_validation(self):
        manager = C.Manager()
        manager.dev = ReadDevice(self.obj)
        with patch.object(C.S, 'data_read', return_value=self.obj) as read:
            manager.run('get', dict(path='/soundbanks/A/1'))
        self.assertEqual(read.call_args.kwargs['block'], 2048)
        self.assertEqual(read.call_args.kwargs['rest'], 0)
        damaged = bytearray(self.obj)
        damaged[40] ^= 1
        with patch.object(C.S, 'data_read', return_value=bytes(damaged)):
            with self.assertRaisesRegex(ValueError, 'CRC'):
                manager.run('get', dict(path='/soundbanks/A/1'))

    def test_upload_and_verification_have_separate_rate_timers(self):
        now = [0.0]
        logs = []
        def call(kind, payload=b'', timeout=None):
            now[0] += 0.5
            reply = bytearray(27)
            reply[5] = 1
            if kind == C.S.T_WR_PART:
                reply[14:18] = C.S.be32(8192)
            elif kind == C.S.T_RD_PART:
                reply[18] = 1
                reply[23:27] = C.S.be32(8192)
            return bytes(reply)
        dev = type('Device', (), {'call': staticmethod(call)})()
        transfer = C.Transport(dev, logs.append, lambda: False)
        with patch.object(C.time, 'monotonic', side_effect=lambda: now[0]):
            transfer.call(C.S.T_WR_OPEN, C.S.be32(8192))
            transfer.call(C.S.T_WR_PART)
            transfer.call(C.S.T_WR_CLOSE)
            now[0] += 20  # Processing time between phases must not dilute read speed.
            transfer.call(C.S.T_RD_OPEN)
            transfer.call(C.S.T_RD_PART)
            transfer.call(C.S.T_RD_CLOSE)
        progress = [json.loads(s.removeprefix('@progress ')) for s in logs if s.startswith('@progress ')]
        done = [p for p in progress if p['current'] == 8192]
        self.assertEqual([p['phase'] for p in done], ['Uploading', 'Verifying'])
        self.assertEqual([p['kib_s'] for p in done], [16, 16])
        self.assertEqual(len([s for s in logs if 'KiB/s).' in s]), 2)

    def test_truncated_or_corrupt_fast_read_restarts_with_conservative_settings(self):
        damaged = bytearray(self.obj)
        damaged[40] ^= 1
        for failure in (C.S.DeviceError('Truncated partial'), bytes(damaged)):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(C.S, 'data_read', side_effect=[failure, self.obj]) as read:
                    self.assertEqual(C.read_object(object(), '/samples/1'), self.obj)
                self.assertEqual([(c.kwargs['block'], c.kwargs['rest']) for c in read.call_args_list],
                                 [(2048, 0), (2048, 0.05)])

    def test_cancelled_fast_read_is_never_retried(self):
        with patch.object(C.S, 'data_read', side_effect=C.Cancelled('Cancelled')) as read:
            with self.assertRaises(C.Cancelled):
                C.read_object(object(), '/samples/1')
        self.assertEqual(read.call_count, 1)

    def test_raw_reader_falls_back_on_integrity_failure_but_not_cancellation(self):
        dev = C.H.FakeHZ(0x200000)
        reader = C.D.Reader(dev, C.S, 8192, 0, log=lambda text: None)
        obj = C.H.data_object(C.D.RAW_FIRST_N, b'CHECKED')
        with patch.object(C.S, 'data_read', side_effect=[ValueError('CRC'), obj]) as read:
            self.assertEqual(reader.window(0)[0], b'CHECKED')
        self.assertEqual([(c.kwargs['block'], c.kwargs['rest']) for c in read.call_args_list],
                         [(8192, 0), (2048, 0.05)])
        with patch.object(C.S, 'data_read', side_effect=C.Cancelled('Cancelled')) as read:
            with self.assertRaises(C.Cancelled):
                reader.window(0)
        self.assertEqual(read.call_count, 1)

    def test_read_benchmark_rejects_all_write_commands_before_device_call(self):
        dev = ReadDevice(self.obj)
        guard = B.ReadOnly(dev)
        for kind in (0x07, 0x50, 0x51, 0x52, 0x57, 0x58, 0x59, 0x5A, 0x5C, 0x5E):
            with self.subTest(kind=kind):
                with self.assertRaises(PermissionError):
                    guard.call(kind)
        self.assertEqual(dev.requests, [])


if __name__ == '__main__':
    unittest.main()
