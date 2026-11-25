"""Read ordinary browser pages through the bundled Windows accessibility helper."""
import json
import ctypes
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import threading
import time

from .config import app_dir
from .durations import duration_seconds, duration_text


def accessible_duration(row):
    title, label = str(row.get('title') or ''), str(row.get('label') or '')
    suffix = label[len(title):].strip() if title and label.startswith(title) else ''
    clock = duration_text(suffix)
    if clock:
        return clock
    parts = [(r'(\d+)\s*(?:小时|小時|hours?\b)', 3600),
             (r'(\d+)\s*(?:分钟|分鐘|minutes?\b)', 60),
             (r'(\d+)\s*(?:秒钟|秒鐘|秒|seconds?\b)', 1)]
    seconds = sum(int(m.group(1))*factor for pattern, factor in parts
                  for m in re.finditer(pattern, suffix, re.I))
    if seconds:
        return duration_seconds(seconds)
    return next((duration_text(t) for t in row.get('text', []) if duration_text(t)), '')


def renderers(rows):
    out = []
    for row in rows:
        vid, title = str(row.get('id') or ''), str(row.get('title') or '')
        if not re.fullmatch('[A-Za-z0-9_-]{11}', vid) or not title:
            continue
        text = row.get('text') or []
        views = next((t for t in text if re.search(r'\d.*(?:views?|次观看|次觀看|次播放)', t, re.I)), '')
        published = next((t for t in text if re.search(r'(?:\d+\s*(?:分钟|小时|天|周|個?月|年)前|\bago\b)', t)), '')
        out.append({'videoRenderer': {'videoId': vid, 'title': {'simpleText': title[:600]},
            'thumbnail': {'thumbnails': [{'url': 'https://i.ytimg.com/vi/'+vid+'/hqdefault.jpg'}]},
            'ownerText': {'runs': [{'text': str(row.get('author') or '')[:200]}]},
            'lengthText': {'simpleText': accessible_duration(row)},
            'viewCountText': {'simpleText': views}, 'publishedTimeText': {'simpleText': published}}})
    return out


class NativeBrowser:
    def __init__(self, settings):
        self.settings = settings
        self.active = False
        self.worker = None
        self.responses = None
        self.lock = threading.RLock()
        self.hwnd = 0
        self.tab = ''
        self.session = ''
        self.seen = set()
        self.cache = {}
        self.prepared = None
        self.last_page = None
        self.query = ''
        self.closed = False

    def _stop(self):
        worker, self.worker = self.worker, None
        self.active = False
        if worker:
            if worker.poll() is None:
                worker.kill()  # Only our helper, never the user's browser.
            worker.wait(timeout=3)
            for stream in (worker.stdin, worker.stdout, worker.stderr):
                if stream:
                    stream.close()

    def _call(self, op, cancel=None, **data):
        deadline = time.monotonic() + self.settings.timeout
        while not self.lock.acquire(timeout=.1):
            self._check(cancel, deadline)
            if self.closed:
                raise RuntimeError('工具已关闭')
        try:
            self._check(cancel, deadline)
            if self.closed:
                raise RuntimeError('工具已关闭')
            if not self.worker or self.worker.poll() is not None:
                path = Path(app_dir())/'vendor/browser-reader/browser-reader.exe'
                if not path.is_file():
                    raise RuntimeError('浏览器页面读取组件缺失，请重新解压完整工具包。无需安装扩展。')
                self.worker = subprocess.Popen([str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding='utf-8', bufsize=1,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.responses = queue.Queue()
                worker, responses = self.worker, self.responses
                def receive():
                    try:
                        for line in worker.stdout:
                            if len(line) > 4_000_000:
                                responses.put(RuntimeError('浏览器页面结果过大')); break
                            responses.put(line)
                    except (OSError, ValueError):
                        pass
                    finally:
                        responses.put(RuntimeError('浏览器页面读取组件已停止'))
                threading.Thread(target=receive, name='browser-accessibility', daemon=True).start()
            if os.name == 'nt':
                # The foreground app may explicitly allow its own helper to
                # focus the browser; never disable Windows foreground locking.
                ctypes.windll.user32.AllowSetForegroundWindow(self.worker.pid)
            self.worker.stdin.write(json.dumps(dict(op=op, **data), ensure_ascii=False)+'\n')
            self.worker.stdin.flush()
            while True:
                self._check(cancel, deadline)
                try:
                    raw = self.responses.get(timeout=.1)
                    break
                except queue.Empty:
                    pass
            if isinstance(raw, Exception):
                raise raw
            reply = json.loads(raw)
            if reply.get('ok') is not True:
                raise RuntimeError(str(reply.get('error') or '无法读取浏览器页面')[:900])
            return reply['data']
        except Exception:
            self._stop()
            raise
        finally:
            self.lock.release()

    @staticmethod
    def _check(cancel, deadline):
        if cancel and cancel.is_set():
            raise RuntimeError('搜索已取消')
        if time.monotonic() >= deadline:
            raise RuntimeError('浏览器页面读取超时，请保持浏览器与网站页面打开后重试')

    def ensure_ready(self, cancel=None):
        data = self._call('scan', cancel)
        self.hwnd = int(data['hwnd'])
        self.tab = str(data['tab'])
        self.active = True

    def _items(self, rows):
        fresh = []
        for item in renderers(rows):
            vid = item['videoRenderer']['videoId']
            if vid not in self.seen:
                self.seen.add(vid); fresh.append(item)
        return fresh

    @staticmethod
    def _continuation(token):
        return {'continuationItemRenderer': {'continuationEndpoint': {'continuationCommand': {'token': token}}}}

    def search_page(self, query, cancel=None):
        with self.lock:
            self._check(cancel, time.monotonic()+self.settings.timeout)
            if self.closed:
                raise RuntimeError('工具已关闭')
            if self.prepared:
                saved, self.prepared = self.prepared, None
                if saved[0] == query and time.monotonic()-saved[3] < 30 and self.active:
                    return saved[1], saved[2]
            if not self.active:
                self.ensure_ready(cancel)
            data = self._call('open', cancel, hwnd=self.hwnd, tab=self.tab, query=query)
            self.tab = str(data.get('tab') or self.tab)
            self.query = query
            self.session = secrets.token_hex(16)
            self.seen, self.cache = set(), {}
            token = 'native-'+secrets.token_hex(16)
            self.next_token = token
            items = self._items(data.get('rows') or [])
            if not items:
                raise RuntimeError('当前浏览器搜索页未显示可读取的视频，请在官网完成必要验证后重试')
            items.append(self._continuation(token))
            self.active = True
            page = {'contents': {'itemSectionRenderer': {'contents': items}}}
            config = {
                'INNERTUBE_CONTEXT': {'client': {'clientName': 'WEB', 'clientVersion': 'rf-accessibility'}},
                '_rf_browser_session': ['native', self.session]}
            self.last_page = (query, page, config, time.monotonic())
            return page, config

    def remember_verification(self, query):
        if self.last_page and self.last_page[0] == query:
            self.prepared = self.last_page

    def continuation(self, body, cancel=None, session=None):
        with self.lock:
            if session != ['native', self.session] or self.closed:
                raise RuntimeError('浏览器读取会话已变化，请重新搜索；不会读取其他窗口的内容')
            token = body.get('continuation')
            if token in self.cache:
                return self.cache[token]
            if token != self.next_token:
                raise RuntimeError('浏览器续页标记已过期，请重新搜索')
            data = self._call('more', cancel, hwnd=self.hwnd, tab=self.tab,
                              known=','.join(sorted(self.seen)), query=self.query)
            self.tab = str(data.get('tab') or self.tab)
            self.active = True
            items = self._items(data.get('rows') or [])
            self.next_token = 'native-'+secrets.token_hex(16)
            if not data.get('ended'):
                items.append(self._continuation(self.next_token))
            reply = {'onResponseReceivedCommands': [{'appendContinuationItemsAction': {'continuationItems': items}}]}
            if len(items) == 1 and 'continuationItemRenderer' in items[0]:
                reply['_rf_no_new'] = True
            self.cache[token] = reply
            while len(self.cache) > 16:
                self.cache.pop(next(iter(self.cache)))
            return reply

    def close(self):
        self.closed = True
        if self.worker and self.worker.poll() is None:
            try:
                self.worker.stdin.write(json.dumps(dict(op='shutdown'))+'\n')
                self.worker.stdin.flush()
                self.responses.get(timeout=1)
            except (OSError, queue.Empty, ValueError):
                pass
        self._stop()
