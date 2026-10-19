"""USB loss and blocked native-driver simulations. No MIDI ports are opened."""
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'synfact-transfer'))
import app
import core
from midi_io import ConnectionLost, MidiSession


def fake_driver(pipe):
    try:
        while True:
            action, args = pipe.recv()
            if action == 'crash':
                return
            if action == 'hang':
                pipe.recv()  # Simulate a native call that never returns.
                return
            pipe.send((True, args))
            if action == 'close':
                return
    except EOFError:
        pass
    finally:
        pipe.close()


class ProcessIsolationTests(unittest.TestCase):
    def session(self):
        session = MidiSession(target=fake_driver)
        self.addCleanup(session.close)
        self.assertEqual(session.request('echo', ('ready',)), ('ready',))
        return session

    def test_native_hang_has_a_deadline_and_process_is_discarded(self):
        session = self.session()
        start = time.monotonic()
        with self.assertRaises(ConnectionLost):
            session.request('hang', timeout=0.15)
        self.assertLess(time.monotonic() - start, 2)
        self.assertTrue(session.failed)
        self.assertIsNone(session.process)
        replacement = self.session()
        self.assertEqual(replacement.request('echo', ('reconnected',)), ('reconnected',))

    def test_disconnect_interrupts_a_request_without_waiting_for_timeout(self):
        session = self.session()
        errors = []
        def blocked():
            try:
                session.request('hang', timeout=30)
            except ConnectionLost as error:
                errors.append(error)
        thread = threading.Thread(target=blocked, daemon=True)
        thread.start()
        session.interrupt()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)

    def test_driver_crash_does_not_leave_the_parent_waiting(self):
        session = self.session()
        with self.assertRaises(ConnectionLost):
            session.request('crash')
        self.assertIsNone(session.process)


class FakeDevice:
    def __init__(self, *names):
        self.interrupted = threading.Event()
        self.failed = False
        self.closed = False

    def open(self):
        pass

    def call(self, kind, payload=b'', timeout=None):
        if kind == 1:
            return bytes(7) + b'Syntakt\0'
        if kind == 2:
            return bytes(5) + b'1.41\0'

    def interrupt(self):
        self.interrupted.set()

    def close(self):
        self.closed = True


def wait_job(service, key):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = next(j for j in service.snapshot()['jobs'] if j['id'] == key)
        if job['status'] not in ('queued', 'running'):
            return job
        time.sleep(0.01)
    raise AssertionError('Job did not finish.')


class ConnectionLifecycleTests(unittest.TestCase):
    def service(self):
        service = app.Service()
        self.addCleanup(service.stop)
        return service

    def test_failed_handshake_clears_all_connection_state_and_can_retry(self):
        manager = core.Manager()
        with patch.object(core, 'MidiDevice', FakeDevice):
            with patch.object(manager, 'detect_profile', side_effect=ConnectionLost('USB removed')):
                with self.assertRaises(ConnectionLost):
                    manager.run('connect', dict(input='in', output='out'))
            self.assertEqual(manager.status(), dict(connected=False, mode='disconnected',
                                                    profile='stock', identity='', input='', output=''))
            result = manager.run('connect', dict(input='in', output='out', profile='stock'))
            self.assertTrue(result['connected'])
            manager.disconnect()

    def test_disconnect_preempts_active_job_discards_queued_write_and_reconnects(self):
        service = self.service()
        device = FakeDevice()
        service.manager.dev = device
        started = threading.Event()
        original = service.manager.run
        writes = []
        def run(action, args, *rest):
            if action == 'list':
                started.set()
                if not device.interrupted.wait(3):
                    raise AssertionError('Disconnect did not interrupt the operation.')
                device.failed = True
                raise ConnectionLost('USB removed')
            if action == 'put':
                writes.append(args)
            return original(action, args, *rest)
        with patch.object(service.manager, 'run', side_effect=run), patch.object(core, 'MidiDevice', FakeDevice):
            active = service.submit('list', {})
            self.assertTrue(started.wait(2))
            queued = service.submit('put', dict(confirm=True))
            disconnected = service.submit('disconnect', {})
            self.assertEqual(wait_job(service, disconnected)['status'], 'done')
            self.assertEqual(wait_job(service, active)['status'], 'cancelled')
            self.assertEqual(wait_job(service, queued)['status'], 'cancelled')
            self.assertEqual(writes, [])
            self.assertFalse(service.snapshot()['connected'])
            connected = service.submit('connect', dict(input='new in', output='new out', profile='stock'))
            self.assertEqual(wait_job(service, connected)['status'], 'done')
            self.assertEqual(service.snapshot()['input'], 'new in')

    def test_shutdown_during_lost_connection_keeps_the_shutdown_sentinel(self):
        service = self.service()
        device = FakeDevice()
        service.manager.dev = device
        started = threading.Event()
        def run(*args):
            started.set()
            device.interrupted.wait(3)
            device.failed = True
            raise ConnectionLost('USB removed during shutdown')
        with patch.object(service.manager, 'run', side_effect=run):
            service.submit('list', {})
            self.assertTrue(started.wait(2))
            service.begin_stop()
            service.worker.join(2)
            self.assertFalse(service.worker.is_alive())
            self.assertTrue(device.closed)

    def test_idle_health_check_detects_disconnected_device(self):
        service = self.service()
        device = FakeDevice()
        service.manager.dev = device
        def call(*args, **kwargs):
            device.failed = True
            raise ConnectionLost('No response')
        device.call = call
        deadline = time.monotonic() + 2
        while service.snapshot()['connected'] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(service.snapshot()['connected'])
        self.assertTrue(device.closed)


if __name__ == '__main__':
    unittest.main()
