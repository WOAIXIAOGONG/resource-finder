# -*- coding: utf-8 -*-
"""封面图缓存：后台线程下载、落到本地磁盘、限制总大小。

GUI 只认识「本地文件名」，所以下载完成后再回主线程加载 QPixmap。
磁盘结构：%LOCALAPPDATA%\\ResourceFinder\\cache\\thumbs\\ab\\<sha1>.img
扩展名统一 .img —— Qt 按文件内容识别格式，不用去猜 URL 后缀。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Optional, Set

from .config import thumbs_dir
from .net import HttpClient, NetworkError
from .covers import cover_url, video_identity, resolve_cover
from .imagecodec import prepare_image, ImageDecodeError


class ThumbCache:
    def __init__(self, settings, http: HttpClient, root: str = "",
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.settings = settings
        self.http = http
        self.root = root or thumbs_dir()
        self.log = log or (lambda msg: None)
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, int(getattr(settings, "thumb_workers", 8) or 8)),
            thread_name_prefix="thumb",
        )
        self._inflight: Set[str] = set()
        self._lock = threading.Lock()
        self._broken: Dict[str, float] = {}  # 短暂失败可在一分钟后重试
        self._failed_hosts: Dict[str, int] = {}
        self._host_failed_at: Dict[str, float] = {}
        self._callbacks = {}
        self._resolved = {}
        self._workers = max(1, int(getattr(settings, "thumb_workers", 8) or 8))

    # ------------------------------------------------------------------ #
    def path_for(self, url: str) -> str:
        digest = hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()
        folder = os.path.join(self.root, digest[:2])
        return os.path.join(folder, digest + ".img")

    def cached(self, url: str) -> Optional[str]:
        p = self.path_for(url)
        try:
            if os.path.getsize(p) > 0:
                return p
        except OSError:
            pass
        return None

    # ------------------------------------------------------------------ #
    def request(self, url: str, referer: str = "",
                done: Optional[Callable[[str, Optional[str]], None]] = None) -> Optional[str]:
        """有缓存立刻返回路径；否则排队下载，完成后回调 done(url, path|None)。

        回调在后台线程执行，GUI 侧必须自己切回主线程（用信号）。
        """
        url = cover_url(url)
        if not url:
            if done:
                done(url, None)
            return None
        hit = self.cached(url)
        if hit:
            if done:
                done(url, hit)
            return hit
        with self._lock:
            if done:
                self._callbacks.setdefault(url, []).append(done)
            if url in self._inflight:
                return None                       # 同一张图正在下，回调会统一触发
            if time.monotonic() - self._broken.get(url, -1e9) < 60:
                # 之前失败过：立刻告诉界面「没有封面」，否则卡片会永远停在「加载中…」
                callbacks = self._callbacks.pop(url, [])
            else:
                callbacks = None
                self._broken.pop(url, None)
                self._inflight.add(url)
        if callbacks is not None:
            for callback in callbacks:
                try:
                    callback(url, None)
                except Exception:
                    pass
            return None
        try:
            self._pool.submit(self._download, url, referer, None)
        except RuntimeError:
            self._complete(url, None)
        return None

    def request_video(self, video_url, done=None):
        """Resolve known providers in the thumbnail pool, never on the GUI thread."""
        key = 'video:' + video_url
        if not video_identity(video_url)[0]:
            if done:
                done(key, None)
            return
        with self._lock:
            if done:
                self._callbacks.setdefault(key, []).append(done)
            if key in self._inflight:
                return
            cooling = time.monotonic() - self._broken.get(key, -1e9) < 60
            self._inflight.add(key)
        if cooling:
            self._complete(key, None)
            return
        def resolve():
            try:
                with self._lock:
                    url = self._resolved.get(video_url, '')
                url = url or resolve_cover(video_url, self.http)
                if not url:
                    raise ValueError('视频元数据未提供可用封面')
                with self._lock:
                    self._resolved[video_url] = url
                    while len(self._resolved) > 1000:
                        self._resolved.pop(next(iter(self._resolved)))
                def complete(_url, path):
                    if not path:
                        with self._lock:
                            self._broken[key] = time.monotonic()
                            self._resolved.pop(video_url, None)
                    self._complete(key, path)
                self.request(url, referer=video_url, done=complete)
            except Exception as exc:
                with self._lock:
                    self._broken[key] = time.monotonic()
                self.log('封面补全失败 %s：%s' % (_short(video_url), exc))
                self._complete(key, None)
        try:
            self._pool.submit(resolve)
        except RuntimeError:
            self._complete(key, None)

    def _download(self, url: str, referer: str,
                  done: Optional[Callable[[str, Optional[str]], None]]) -> None:
        path: Optional[str] = None
        tmp = ""
        try:
            if self._host_dead(url):
                # Skipping is not a new network failure and must not move the deadline.
                self.log('封面暂缓 %s：图片服务器暂不可达，冷却后可重试' % _short(url))
                return
            site = _RefSite(referer) if referer else None
            data = self.http.get_bytes(url, site, timeout=8.0, max_bytes=8 * 1024 * 1024,
                                       headers={'Accept': 'image/webp,image/png,image/jpeg,image/gif'})
            # HTTP succeeded. A bad image or disk error does not mean its host is down.
            self._note_host(url, True)
            data = prepare_image(data)
            target = self.path_for(url)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = target + "." + uuid.uuid4().hex + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
            path = target
        except Exception as exc:              # noqa: BLE001
            if isinstance(exc, NetworkError) and (not exc.status or exc.status == 429 or exc.status >= 500):
                self._note_host(url, False)
            with self._lock:
                self._broken[url] = time.monotonic()
            self.log("封面失败 %s：%s" % (_short(url), exc))
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            self._complete(url, path)

    def _complete(self, url, path):
        with self._lock:
            self._inflight.discard(url)
            callbacks = self._callbacks.pop(url, [])
        for callback in callbacks:
            try:
                callback(url, path)
            except Exception:
                pass

    def reconfigure(self):
        workers = max(1, int(self.settings.thumb_workers or 8))
        if workers != self._workers:
            old = self._pool
            self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="thumb")
            self._workers = workers
            old.shutdown(wait=False)  # Finish queued callbacks; do not strand waiting cards.

    # ------------------------------------------------------------------ #
    @staticmethod
    def _looks_like_image(data: bytes) -> bool:
        try:
            prepare_image(data)
            return True
        except ImageDecodeError:
            return False

    def _note_host(self, url: str, ok: bool) -> None:
        host = _host(url)
        if not host:
            return
        with self._lock:
            if ok:
                self._failed_hosts.pop(host, None)
                self._host_failed_at.pop(host, None)
            else:
                now = time.monotonic()
                if now - self._host_failed_at.get(host, -1e9) >= 60:
                    self._failed_hosts.pop(host, None)
                self._failed_hosts[host] = self._failed_hosts.get(host, 0) + 1
                # Freeze the deadline once the circuit opens, even with requests in flight.
                if self._failed_hosts[host] <= 4:
                    self._host_failed_at[host] = now

    def _host_dead(self, url: str) -> bool:
        host = _host(url)
        with self._lock:
            if time.monotonic() - self._host_failed_at.get(host, 0) >= 60:
                self._failed_hosts.pop(host, None)
                self._host_failed_at.pop(host, None)
                return False
            return self._failed_hosts.get(host, 0) >= 4

    # ------------------------------------------------------------------ #
    def size_bytes(self) -> int:
        total = 0
        for base, _dirs, files in os.walk(self.root):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(base, name))
                except OSError:
                    pass
        return total

    def evict(self, max_mb: int = 300) -> int:
        """超过上限时按最后访问时间删最旧的，返回删除的文件数。"""
        limit = max(50, int(max_mb)) * 1024 * 1024
        entries = []
        total = 0
        for base, _dirs, files in os.walk(self.root):
            for name in files:
                p = os.path.join(base, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                entries.append((st.st_atime or st.st_mtime, st.st_size, p))
                total += st.st_size
        if total <= limit:
            return 0
        entries.sort()
        removed = 0
        for _atime, size, p in entries:
            if total <= limit * 0.8:
                break
            try:
                os.unlink(p)
                total -= size
                removed += 1
            except OSError:
                pass
        return removed

    def clear(self) -> int:
        removed = 0
        for base, _dirs, files in os.walk(self.root):
            for name in files:
                try:
                    os.unlink(os.path.join(base, name))
                    removed += 1
                except OSError:
                    pass
        with self._lock:
            self._broken.clear()
            self._failed_hosts.clear()
            self._host_failed_at.clear()
        return removed

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class _RefSite:
    """只为给 HttpClient 传一个 Referer。"""

    def __init__(self, referer: str) -> None:
        self.referer = referer
        self.headers: Dict[str, str] = {}
        self.encoding = ""
        self.route = "auto"


def _host(url: str) -> str:
    try:
        from urllib.parse import urlsplit

        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _short(url: str) -> str:
    return url[:80] + ("…" if len(url) > 80 else "")
