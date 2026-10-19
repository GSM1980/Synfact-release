"""Bounded MIDI access: native driver calls run outside the desktop process."""
import multiprocessing
from pathlib import Path
import sys
import threading
import time


class ConnectionLost(RuntimeError):
    pass


def midi_worker(pipe):
    # Import the driver only in its disposable process. WinMM can hang even in
    # get_ports/close_port after a USB device disappears.
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'vendor'))
    import rtmidi
    import stfiles as S

    class NativeDevice(S.Syntakt):
        def __init__(self, input_name, output_name):
            self.trace, self.timeout, self.seq, self._pending = False, 5.0, 1, None
            self.mi, self.mo = rtmidi.MidiIn(), rtmidi.MidiOut()
            if hasattr(self.mi, 'set_buffer_size'):
                self.mi.set_buffer_size(16384, 16)
            self.mi.open_port(self.mi.get_ports().index(input_name))
            self.mo.open_port(self.mo.get_ports().index(output_name))
            self.mi.ignore_types(sysex=False, timing=True, active_sense=True)

    dev = None
    try:
        while True:
            action, args = pipe.recv()
            try:
                if action == 'ports':
                    mi, mo = rtmidi.MidiIn(), rtmidi.MidiOut()
                    try:
                        result = {'inputs': mi.get_ports(), 'outputs': mo.get_ports()}
                    finally:
                        mi.delete()
                        mo.delete()
                elif action == 'open':
                    dev = NativeDevice(*args)
                    result = True
                elif action == 'call':
                    result = dev.call(*args)
                elif action == 'send':
                    dev.mo.send_message(args[0])
                    result = True
                elif action == 'close':
                    if dev:
                        dev.close()
                    pipe.send((True, None))
                    return
                else:
                    raise ValueError('Unknown MIDI operation.')
                pipe.send((True, result))
            except Exception as error:
                pipe.send((False, str(error)))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        pipe.close()


class MidiSession:
    def __init__(self, target=midi_worker):
        self.target = target
        self.process = self.pipe = None
        self.lock = threading.Lock()
        self.interrupted = threading.Event()
        self.failed = False

    def interrupt(self):
        # Safe from HTTP/shutdown threads; only the owning request touches IPC.
        self.interrupted.set()

    def dispose(self):
        process, pipe = self.process, self.pipe
        self.process = self.pipe = None
        if pipe:
            pipe.close()
        if process:
            if process.pid is None:
                process.close()
                return
            if process.is_alive():
                process.terminate()
            process.join(0.25)
            if process.is_alive():
                process.kill()
                process.join(0.25)
            if not process.is_alive():
                process.close()

    def request(self, action, args=(), timeout=4):
        if not self.lock.acquire(timeout=timeout):
            raise ConnectionLost('MIDI service is busy. Retrying connection.')
        try:
            if self.failed or self.interrupted.is_set():
                raise ConnectionLost('MIDI connection closed.')
            if self.process is None:
                context = multiprocessing.get_context('spawn')
                self.pipe, child = context.Pipe()
                self.process = context.Process(target=self.target, args=(child,), daemon=True)
                try:
                    self.process.start()
                finally:
                    child.close()
            deadline = time.monotonic() + timeout
            self.pipe.send((action, args))
            while not self.pipe.poll(0.05):
                if self.interrupted.is_set():
                    raise ConnectionLost('MIDI connection interrupted.')
                if time.monotonic() >= deadline or not self.process.is_alive():
                    raise ConnectionLost('The device stopped responding. Reconnect the USB cable.')
            ok, result = self.pipe.recv()
            if not ok:
                raise ConnectionLost('MIDI connection failed: ' + result)
            return result
        except (ConnectionLost, OSError, EOFError) as error:
            self.failed = True
            self.dispose()
            raise ConnectionLost(str(error)) from error
        finally:
            self.lock.release()

    def close(self):
        try:
            if self.process and not self.failed and not self.interrupted.is_set():
                self.request('close', timeout=0.5)
        except ConnectionLost:
            pass
        finally:
            self.dispose()


class MidiDevice(MidiSession):
    def __init__(self, input_name, output_name):
        super().__init__()
        # Opening is lazy so Manager can publish the interrupt handle first.
        self.names = (input_name, output_name)

    def open(self):
        self.request('open', self.names)

    def call(self, kind, payload=b'', timeout=None):
        seconds = timeout or 5.0
        result = self.request('call', (kind, payload, seconds), timeout=seconds + 0.5)
        if result is None:
            self.failed = True
            self.dispose()
            raise ConnectionLost('The device did not respond. Waiting to reconnect.')
        return result

    def send_message(self, message):
        self.request('send', (message,))


_ports = None
_ports_lock = threading.Lock()


def ports():
    global _ports
    with _ports_lock:
        if _ports is None or _ports.failed:
            _ports = MidiSession()
        session = _ports
    return session.request('ports')


def stop_ports():
    if _ports:
        _ports.interrupt()
