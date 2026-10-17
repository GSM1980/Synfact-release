"""HZFS compaction and recovery on the offline RPC device; no MIDI access."""
from pathlib import Path
import contextlib
import io
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'synfact-transfer'))
import core as C
import compaction as K


class CompactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        silent = contextlib.redirect_stdout(io.StringIO())
        silent.__enter__()
        self.addCleanup(silent.__exit__, None, None, None)
        self.root = Path(self.temp.name)
        self.device = C.H.FakeHZ(0x680 * 1024)
        self.store = C.H.Store(self.device, C.S, rest=0, write_rest=0, log=lambda s: None)
        self.original = C.H.Index(seq=10, windows=0x680, next_id=40)
        self.bodies = {}
        for eid, first in ((19, 0x612), (3, 0x605), (11, 0x60a)):
            body, frames = C.H.samp_body(bytes([eid, 0]) * 32, f'SAMPLE{eid}')
            self.bodies[eid] = body
            self.device.card[first] = body.ljust(C.H.WINDOW, b'\0')
            self.original.entries.append(dict(id=eid, first=first, blocks=1,
                bytes=len(body), frames=frames, crc=C.H.crc(body), created=eid,
                flags=0, name=f'SAMPLE{eid}'))
        self.original.slot_map[0] = 19
        self.original.slot_map[30] = 3
        self.original.slot_map[63] = 19
        for k in C.H.INDEX_K:
            self.device.card[k] = self.original.pack().ljust(C.H.WINDOW, b'\0')
        self.backup = self.root / 'backup'

        def make_folder(**kwargs):
            self.backup.mkdir()
            return str(self.backup)

        for mock in (patch.object(K.tempfile, 'mkdtemp', side_effect=make_folder),
                     patch.object(C.time, 'sleep', return_value=None)):
            mock.start()
            self.addCleanup(mock.stop)
        self.report = lambda *args: None

    def assert_packed(self):
        index, copies = self.store.current()
        self.assertEqual(index.next_id, 40)
        self.assertEqual(index.slot_map, self.original.slot_map)
        self.assertEqual([e['id'] for e in index.entries], [3, 11, 19])
        self.assertEqual([e['first'] for e in index.entries], [0x602, 0x603, 0x604])
        for e in index.entries:
            original = next(o for o in self.original.entries if o['id'] == e['id'])
            self.assertEqual({**e, 'first': original['first']}, original)
            self.assertEqual(self.store.read_blocks(e['first'], e['blocks'], e['bytes']), self.bodies[e['id']])
        self.assertEqual(C.H.free_blocks(index, index.windows), C.H.largest_hole(index, index.windows))
        self.assertTrue(all(c.entries == index.entries and c.slot_map == index.slot_map for c in copies))
        self.assertTrue(all(k >= C.H.HZ_FIRST_K for k, _ in self.device.writes))

    def test_compaction_preserves_project_ids_audio_and_metadata(self):
        before = C.H.free_blocks(self.original, self.original.windows)
        result = K.compact(self.store, self.report, lambda: False)
        self.assert_packed()
        self.assertEqual(result['free'], before * C.H.WINDOW)
        self.assertEqual(result['largest'], result['free'])
        self.assertFalse(self.backup.exists())

    def test_corrupt_audio_refuses_to_write(self):
        self.device.card[0x605] = bytes(C.H.WINDOW)
        with self.assertRaisesRegex(ValueError, 'verification'):
            K.compact(self.store, self.report, lambda: False)
        self.assertEqual(self.device.writes, [])

    def test_cancel_during_backup_leaves_device_untouched(self):
        with self.assertRaisesRegex(RuntimeError, 'Device unchanged'):
            K.compact(self.store, self.report, lambda: True)
        self.assertEqual(self.device.writes, [])

    def test_interrupted_rebuild_retains_backup_and_can_restore(self):
        write = self.store.write_at
        def fail_data(k, body):
            if k >= C.H.DATA_FIRST_K:
                raise RuntimeError('USB disconnected')
            return write(k, body)
        with patch.object(self.store, 'write_at', side_effect=fail_data):
            with self.assertRaisesRegex(RuntimeError, 'Recovery backup retained at'):
                K.compact(self.store, self.report, lambda: False)
        self.assertEqual(K.load_backup(self.backup).next_id, 40)
        K.restore(self.store, self.backup, self.report, lambda: False)
        self.assert_packed()
        self.assertTrue(self.backup.exists())

    def test_bad_readback_retains_complete_backup(self):
        read = self.store.read_blocks
        def corrupt_new(first, count, length):
            if first == C.H.DATA_FIRST_K:
                return bytes(length)
            return read(first, count, length)
        with patch.object(self.store, 'read_blocks', side_effect=corrupt_new):
            with self.assertRaisesRegex(RuntimeError, 'read back identical'):
                K.compact(self.store, self.report, lambda: False)
        self.assertEqual(len(K.load_backup(self.backup).entries), 3)

    def test_damaged_backup_refuses_restore_before_any_writes(self):
        with patch.object(self.store, 'write_at', side_effect=RuntimeError('disconnected')):
            with self.assertRaises(RuntimeError):
                K.compact(self.store, self.report, lambda: False)
        (self.backup / '3.samp').write_bytes(b'bad')
        self.device.writes.clear()
        with self.assertRaisesRegex(ValueError, 'verification'):
            K.restore(self.store, self.backup, self.report, lambda: False)
        self.assertEqual(self.device.writes, [])

    def test_capacity_mismatch_refuses_to_write(self):
        with patch.object(self.store, 'capacity', return_value=2048):
            with self.assertRaisesRegex(ValueError, 'capacities'):
                K.compact(self.store, self.report, lambda: False)
        self.assertEqual(self.device.writes, [])

    def test_empty_library_rebuilds_both_indexes(self):
        index = C.H.Index(seq=12, windows=0x680, next_id=40)
        for k in C.H.INDEX_K:
            self.device.card[k] = index.pack().ljust(C.H.WINDOW, b'\0')
        result = K.compact(self.store, self.report, lambda: False)
        self.assertEqual(result['count'], 0)
        self.assertEqual(result['largest'], result['free'])
        _, copies = self.store.current()
        self.assertTrue(all(c.next_id == 40 and not c.entries for c in copies))

    def test_cancel_after_reset_retains_backup_and_recovers(self):
        with self.assertRaisesRegex(RuntimeError, 'Recovery backup retained at'):
            K.compact(self.store, self.report, lambda: bool(self.device.writes))
        K.restore(self.store, self.backup, self.report, lambda: False)
        self.assert_packed()

    def test_restore_works_with_both_device_indexes_corrupted(self):
        with patch.object(self.store, 'write_at', side_effect=RuntimeError('disconnected')):
            with self.assertRaises(RuntimeError):
                K.compact(self.store, self.report, lambda: False)
        for k in C.H.INDEX_K:
            self.device.card[k] = bytes(C.H.WINDOW)
        K.restore(self.store, self.backup, self.report, lambda: False)
        self.assert_packed()

    def test_id_limit_blocks_upload_before_writing(self):
        manager = C.Manager()
        manager.dev, manager.profile = self.device, 'v7'
        for next_id in (0, 32768, 0xffffffff):
            with self.subTest(next_id=next_id):
                index = C.H.Index(windows=0x680, next_id=next_id)
                with patch.object(C.H.Store, 'current', return_value=(index, [index, index])):
                    with self.assertRaisesRegex(ValueError, 'IDs are exhausted'):
                        manager.run('hz_put', {})
        self.assertEqual(self.device.writes, [])

    def test_new_actions_require_confirmation(self):
        import app
        service = app.Service()
        self.addCleanup(service.stop)
        for action in ('hz_compact', 'hz_restore'):
            with self.assertRaises(PermissionError):
                service.submit(action, {})

    def test_compaction_preserves_exhausted_id_counter(self):
        self.original.next_id = 32768
        for k in C.H.INDEX_K:
            self.device.card[k] = self.original.pack().ljust(C.H.WINDOW, b'\0')
        K.compact(self.store, self.report, lambda: False)
        index, _ = self.store.current()
        self.assertEqual(index.next_id, 32768)
        self.assertEqual(index.slot_map, self.original.slot_map)

    def test_multiblock_sample_is_relocated_without_gaps(self):
        entry = next(e for e in self.original.entries if e['id'] == 3)
        body, frames = C.H.samp_body(b'\x12\x34' * (C.H.WINDOW // 2), 'SAMPLE3')
        entry.update(blocks=2, bytes=len(body), frames=frames, crc=C.H.crc(body))
        for off in range(0, len(body), C.H.WINDOW):
            self.device.card[entry['first'] + off // C.H.WINDOW] = body[off:off + C.H.WINDOW].ljust(C.H.WINDOW, b'\0')
        for k in C.H.INDEX_K:
            self.device.card[k] = self.original.pack().ljust(C.H.WINDOW, b'\0')
        K.compact(self.store, self.report, lambda: False)
        index, _ = self.store.current()
        self.assertEqual([e['first'] for e in index.entries], [0x602, 0x604, 0x605])
        moved = index.find('3')
        self.assertEqual(self.store.read_blocks(moved['first'], moved['blocks'], moved['bytes']), body)
        self.assertEqual(C.H.free_blocks(index, index.windows), C.H.largest_hole(index, index.windows))


if __name__ == '__main__':
    unittest.main()
