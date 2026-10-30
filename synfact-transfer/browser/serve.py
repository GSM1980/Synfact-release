"""Development-only browser port server, bound to localhost."""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import mimetypes
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('Cross-Origin-Embedder-Policy', 'require-corp')
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/':
            body = (ROOT / 'web/index.html').read_text(encoding='utf8')
            body = body.replace('<head>', '<head><meta name="session-token" content="browser">', 1)
            body = body.replace('href="style.css"', 'href="/web/style.css"')
            body = body.replace('<script src="app.js" defer></script>', '<script type="module" src="/browser/native-client.mjs"></script>')
            data = body.encode('utf8')
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
            return
        if path.startswith('/browser/'):
            target = (ROOT / path.lstrip('/')).resolve()
            if not target.is_relative_to((ROOT / 'browser').resolve()):
                self.send_error(404); return
        elif path in ('/web/app.js', '/web/style.css'):
            target = ROOT / path.lstrip('/')
        else:
            self.send_error(404); return
        if not target.is_file():
            self.send_error(404); return
        data = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
        if target.suffix == '.mjs': mime = 'text/javascript'
        if target.suffix == '.wasm': mime = 'application/wasm'
        self.send_response(200); self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 8766), Handler)
    print('Browser port: http://127.0.0.1:8766/', flush=True)
    webbrowser.open('http://127.0.0.1:8766/')
    server.serve_forever()
