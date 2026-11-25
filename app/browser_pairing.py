"""User-initiated loopback pairing. Does not alter browser settings or profiles."""
import html
import json
import os
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import app_dir

_BROWSERS = {'chrome.exe': ('chrome', 'Chrome', 'chrome://extensions/'),
             'msedge.exe': ('edge', 'Edge', 'edge://extensions/'),
             'brave.exe': ('brave', 'Brave', 'brave://extensions/'),
             'vivaldi.exe': ('vivaldi', 'Vivaldi', 'vivaldi://extensions/')}


def running_browsers():
    """Read executable names/paths only; do not inspect cookies or browsing data."""
    if os.name != 'nt':
        return []
    command = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
               "Get-Process -Name chrome,msedge,brave,vivaldi -ErrorAction SilentlyContinue | "
               "Where-Object { $_.Path } | Select-Object -ExpandProperty Path -Unique | ConvertTo-Json -Compress")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8,
        creationflags=subprocess.CREATE_NO_WINDOW)
    raw = result.stdout.decode('utf-8-sig', errors='replace').strip()
    # Windows PowerShell output encoding must be made explicit for Unicode paths.
    if not raw:
        return []
    values = json.loads(raw)
    values = [values] if isinstance(values, str) else values
    found = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, str):
            continue
        path = Path(value)
        meta = _BROWSERS.get(path.name.lower())
        if meta and path.is_file() and not any(row['id'] == meta[0] for row in found):
            found.append(dict(id=meta[0], name=meta[1], settings=meta[2], path=str(path)))
    return found[:4]


def launch_page(browser, url):
    subprocess.Popen([browser['path'], url], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


class PairingAssist:
    def __init__(self, bridge, lifetime=180):
        self.bridge = bridge
        self.address = bridge.connection_info()
        self.nonce = secrets.token_urlsafe(32)
        self.expires = time.monotonic() + lifetime
        self.closed = False
        with bridge.lock:
            self.initial_clients = set(bridge.clients)
        self.browsers = []
        self.extension_dir = str(Path(app_dir(), 'browser-extension').resolve())
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Pairing paths and credentials must never enter HTTP logs.

            def reply(self, code, body, kind='text/plain; charset=utf-8'):
                payload = body.encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('X-Frame-Options', 'DENY')
                self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'nonce-"+owner.nonce+"'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                self.wfile.write(payload)

            def valid(self):
                if (self.headers.get('Host') != owner.origin.removeprefix('http://') or
                    owner.closed or time.monotonic() >= owner.expires or owner.bridge.closed):
                    self.reply(410, 'Connection request expired. Return to Resource Finder.')
                    return False
                if self.path not in (owner.prefix, owner.prefix+'/status', owner.prefix+'/extensions'):
                    self.reply(403, 'Pairing required')
                    return False
                if self.headers.get('Origin') not in (None, owner.origin):
                    self.reply(403, 'Origin rejected')
                    return False
                return True

            def do_GET(self):
                if not self.valid():
                    return
                if self.path == owner.prefix:
                    self.reply(200, owner.page(), 'text/html; charset=utf-8')
                elif self.path == owner.prefix+'/status':
                    with owner.bridge.lock:
                        connected = bool(set(owner.bridge.clients) - owner.initial_clients)
                    self.reply(200, json.dumps({'connected': connected}), 'application/json')
                else:
                    self.reply(405, 'POST required')

            def do_POST(self):
                if not self.valid():
                    return
                if (self.path != owner.prefix+'/extensions' or self.headers.get('Origin') != owner.origin
                        or self.headers.get('X-RF-Nonce') != owner.nonce):
                    self.reply(403, 'Explicit setup action required')
                    return
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 256:
                        raise ValueError()
                    key = json.loads(self.rfile.read(size)).get('browser')
                    browser = next(row for row in owner.browsers if row['id'] == key)
                    launch_page(browser, browser['settings'])
                    self.reply(200, '{}', 'application/json')
                except Exception:
                    self.reply(400, 'Cannot open extension settings')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.origin = 'http://127.0.0.1:%d' % self.server.server_port
        self.prefix = '/rf-connect/' + self.nonce
        self.url = self.origin + self.prefix
        threading.Thread(target=self.server.serve_forever, name='browser-pairing', daemon=True).start()

    def open(self, cancel=None):
        def checkpoint():
            if self.closed or self.bridge.closed or (cancel and cancel.is_set()):
                raise RuntimeError('搜索已取消')
            if time.monotonic() >= self.expires:
                raise RuntimeError('浏览器连接等待已超时，请重新点击“已登录”')
        checkpoint()
        self.browsers = running_browsers()
        checkpoint()
        if not self.browsers:
            raise RuntimeError('未检测到正在运行的受支持浏览器。请先使用 Chrome / Edge 等 Chromium 浏览器登录 YouTube，保持页面打开，再点“已登录”。')
        opened = 0
        for browser in self.browsers:
            checkpoint()
            try:
                launch_page(browser, self.url)
                opened += 1
            except OSError:
                pass
        if not opened:
            raise RuntimeError('浏览器连接助手未能打开，请检查浏览器是否允许打开本机页面。')

    def close(self):
        self.closed = True
        self.server.shutdown()
        self.server.server_close()

    def page(self):
        from .pairing_page import PAGE
        buttons = ''.join('<button data-browser="%s">打开 %s 扩展管理</button>' %
                          (html.escape(row['id']), html.escape(row['name'])) for row in self.browsers)
        return (PAGE.replace('__NONCE__', html.escape(self.nonce, quote=True))
            .replace('__ADDRESS__', html.escape(self.address, quote=True))
            .replace('__DIRECTORY__', html.escape(self.extension_dir, quote=True))
            .replace('__BUTTONS__', buttons))
