"""Local-only HTTP application. No external web services and no automatic MIDI sends."""
import argparse
import contextlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import queue
import secrets
import threading
import time
from urllib.parse import urlsplit
import uuid
import webbrowser

from core import Manager, Cancelled, ports
from midi_io import ConnectionLost, stop_ports

ROOT = Path(__file__).resolve().parent
WRITE_ACTIONS = {'put', 'clear', 'copy', 'move', 'swap', 'hz_put', 'hz_rm', 'hz_init', 'hz_compact', 'hz_restore', 'machine_put', 'sysex_send', 'sample_put', 'firmware_put'}
ACTIONS = WRITE_ACTIONS | {'connect', 'disconnect', 'list', 'get', 'hz_list', 'hz_get', 'hz_check', 'machine_get', 'sample_list', 'download_group'}


class JobLog:
    def __init__(self, callback):
        self.callback, self.pending = callback, ''

    def write(self, text):
        self.pending += text
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            if line.strip():
                self.callback(line)
        return len(text)

    def flush(self):
        if self.pending.strip():
            self.callback(self.pending)
        self.pending = ''


class Service:
    def __init__(self):
        self.manager, self.jobs, self.lock = Manager(), {}, threading.RLock()
        # At most 32 user jobs; reserve one extra slot for the shutdown sentinel.
        self.queue, self.closed = queue.Queue(maxsize=33), False
        self.last_probe = 0
        self.worker = threading.Thread(target=self.work, daemon=True)
        self.worker.start()

    def submit(self, action, args):
        if action not in ACTIONS:
            raise ValueError('Unknown action.')
        if action in WRITE_ACTIONS and args.get('confirm') is not True:
            raise PermissionError('Confirm the device modification.')
        with self.lock:
            if self.closed:
                raise RuntimeError('The application is closing.')
            if action == 'disconnect':
                self.cancel_pending()
                self.manager.interrupt()
            active = sum(j['status'] in ('queued', 'running') for j in self.jobs.values())
            if active >= 32:
                raise ValueError('The transfer queue is full.')
            key = uuid.uuid4().hex
            self.jobs[key] = {'id': key, 'action': action, 'status': 'queued', 'logs': [],
                              'result': None, 'error': None, 'cancel': False, 'created': time.time()}
            while len(self.jobs) > 100:
                oldest = next(iter(self.jobs))
                if self.jobs[oldest]['status'] in ('queued', 'running'):
                    break
                self.jobs.pop(oldest)
            self.queue.put_nowait((key, action, args))
        return key

    def log(self, key, line):
        with self.lock:
            job = self.jobs[key]
            if str(line).startswith('@progress '):
                job['progress'] = json.loads(str(line)[10:])
                return
            job['logs'].append(str(line))
            job['logs'] = job['logs'][-200:]

    def work(self):
        while True:
            try:
                item = self.queue.get(timeout=0.5)
            except queue.Empty:
                # Probe only between jobs, never interleave RPCs with a transfer.
                if self.manager.dev and time.monotonic() - self.last_probe >= 2:
                    self.last_probe = time.monotonic()
                    try:
                        self.manager.dev.call(1, timeout=1)
                    except Exception:
                        self.connection_lost()
                continue
            if item is None:
                try:
                    self.manager.disconnect()
                finally:
                    self.queue.task_done()
                break
            key, action, args = item
            try:
                with self.lock:
                    job = self.jobs[key]
                    if job['cancel']:
                        job['status'] = 'cancelled'
                        continue
                    job['status'] = 'running'
                logger = JobLog(lambda s: self.log(key, s))
                with contextlib.redirect_stdout(logger):
                    result = self.manager.run(action, args, logger.callback, lambda: job['cancel'])
                    logger.flush()
                    if self.manager.connection_failed():
                        raise ConnectionLost('Device disconnected during the operation.')
                with self.lock:
                    job['result'], job['status'] = result, 'done'
            except Cancelled as error:
                with self.lock:
                    job['status'], job['error'] = 'cancelled', str(error)
            except (Exception, SystemExit) as error:
                with self.lock:
                    job['status'], job['error'] = ('cancelled' if job['cancel'] else 'error'), str(error)
            finally:
                if self.manager.connection_failed():
                    self.connection_lost()
                self.queue.task_done()

    def cancel_pending(self):
        # Caller owns the service lock. Never replay queued writes after USB loss.
        for job in self.jobs.values():
            if job['status'] in ('queued', 'running') and job['action'] != 'disconnect':
                job['cancel'] = True
                if job['status'] == 'queued':
                    job['status'], job['error'] = 'cancelled', 'Connection closed. Submit the operation again.'
        retained = []
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            if item is None or item[1] == 'disconnect':
                retained.append(item)
            self.queue.task_done()
        for item in retained:
            self.queue.put_nowait(item)

    def cancel(self, key):
        with self.lock:
            job = self.jobs[key]
            job['cancel'] = True
            if job['status'] == 'running':
                self.manager.interrupt()

    def connection_lost(self):
        with self.lock:
            self.cancel_pending()
        self.manager.disconnect()

    def snapshot(self):
        with self.lock:
            return {**self.manager.status(), 'jobs': [dict(j, logs=list(j['logs'])) for j in self.jobs.values()]}

    def begin_stop(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.cancel_pending()
            self.manager.interrupt()
            stop_ports()
            self.queue.put_nowait(None)

    def stop(self):
        self.begin_stop()
        self.worker.join()


class Handler(BaseHTTPRequestHandler):
    server_version = 'SynfactTransfer/0.0.2'

    def log_message(self, *_):
        pass

    def respond(self, code, body, mime='application/json', extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', mime + ('; charset=utf-8' if mime in ('application/json', 'text/html', 'text/css', 'text/javascript') else ''))
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self):
        # Loopback binding + exact Host + session secret + same-origin POST.
        host = self.headers.get('Host', '')
        if host != f'127.0.0.1:{self.server.server_port}':
            self.respond(403, {'error': 'Host not allowed.'})
            return False
        if not hmac.compare_digest(self.headers.get('X-Session-Token', ''), self.server.token):
            self.respond(403, {'error': 'Session expired.', 'code': 'session_expired'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin != f'http://{host}':
            self.respond(403, {'error': 'Origin not allowed.'})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith('/api/'):
            if not self.authorized():
                return
            try:
                if path == '/api/state':
                    return self.respond(200, self.server.service.snapshot())
                if path == '/api/ports':
                    return self.respond(200, ports())
                if path.startswith('/api/download/'):
                    key = path.rsplit('/', 1)[1]
                    data, name = self.server.service.manager.downloads[key]
                    from urllib.parse import quote
                    return self.respond(200, data, 'application/octet-stream', {'Content-Disposition': "attachment; filename*=UTF-8''" + quote(name)})
                return self.respond(404, {'error': 'Route not found.'})
            except Exception as error:
                return self.respond(400, {'error': str(error)})
        if self.headers.get('Host', '') != f'127.0.0.1:{self.server.server_port}':
            return self.respond(403, {'error': 'Host not allowed.'})
        files = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}
        if path not in files:
            return self.respond(404, {'error': 'Route not found.'})
        target = ROOT / 'web' / files[path]
        body = target.read_bytes()
        if target.suffix == '.html':
            body = body.replace(b'<head>', b'<head><meta name="session-token" content="' + self.server.token.encode('ascii') + b'">', 1)
        return self.respond(200, body, {'.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html'}[target.suffix])

    def do_POST(self):
        if not self.authorized():
            return
        try:
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.respond(415, {'error': 'JSON required.'})
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 96 * 1024 * 1024:
                return self.respond(413, {'error': 'Empty request or request exceeds 96 MB.'})
            args = json.loads(self.rfile.read(length))
            if not isinstance(args, dict):
                raise ValueError('A JSON object is required.')
            path = urlsplit(self.path).path
            if path == '/api/jobs':
                action = args.pop('action')
                key = self.server.service.submit(action, args)
                return self.respond(202, {'id': key})
            if path == '/api/cancel':
                self.server.service.cancel(args['id'])
                return self.respond(200, {'ok': True})
            return self.respond(404, {'error': 'Route not found.'})
        except PermissionError as error:
            return self.respond(403, {'error': str(error)})
        except Exception as error:
            return self.respond(400, {'error': str(error)})


def make_server(port=8765):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.token, server.service = secrets.token_urlsafe(32), Service()
    return server


def main():
    parser = argparse.ArgumentParser(description='Synfact Transfer · local application')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    try:
        server = make_server(args.port)
    except OSError:
        server = make_server(0)
    url = f'http://127.0.0.1:{server.server_port}/#token={server.token}'
    print('Synfact Transfer · local · Ctrl+C to close\n' + url, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.service.stop()
        server.server_close()


if __name__ == '__main__':
    main()
