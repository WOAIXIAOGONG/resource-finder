"""Read duration from provider metadata; never infer it from a title or thumbnail."""
from __future__ import annotations

import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .covers import video_identity


def duration_seconds(value):
    try:
        value = float(value)
        if not math.isfinite(value) or not 0 < value < 1e9:
            return ''
        seconds = int(value)
    except (TypeError, ValueError, OverflowError):
        return ''
    if not seconds:
        return ''
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return ('%d:%02d:%02d' % (hours, minutes, seconds) if hours
            else '%d:%02d' % (minutes, seconds))


def duration_text(value):
    if isinstance(value, dict):
        value = value.get('simpleText') or value.get('content') or ''.join(
            str(r.get('text') or '') for r in value.get('runs', []) if isinstance(r, dict))
    text = re.sub('[\u200e\u200f\u202a-\u202e\u2066-\u2069]', '', str(value or '')).strip()
    return text if re.fullmatch(r'(?:\d+:)?\d+:[0-5]\d', text) else ''


def youtube_result_duration(node):
    """Only inspect duration fields and thumbnail badges of this exact result."""
    text = duration_text(node.get('lengthText'))
    if text:
        return text
    for overlay in node.get('thumbnailOverlays') or []:
        status = overlay.get('thumbnailOverlayTimeStatusRenderer') or {}
        if status.get('style') not in ('LIVE', 'UPCOMING'):
            text = duration_text(status.get('text'))
            if text:
                return text
    thumb = ((node.get('contentImage') or {}).get('thumbnailViewModel') or {})
    for overlay in thumb.get('overlays') or []:
        for badge in (overlay.get('thumbnailOverlayBadgeViewModel') or {}).get('thumbnailBadges') or []:
            text = duration_text((badge.get('thumbnailBadgeViewModel') or {}).get('text'))
            if text:
                return text
    return ''


def youtube_player_duration(html, video_id):
    for marker in re.finditer(r'(?:\bytInitialPlayerResponse|window\[[\"\']ytInitialPlayerResponse[\"\']\])\s*=\s*', html):
        try:
            data, _ = json.JSONDecoder().raw_decode(html[marker.end():])
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        details = data.get('videoDetails') or {}
        if details.get('videoId') != video_id or details.get('isLive'):
            continue
        live = ((data.get('microformat') or {}).get('playerMicroformatRenderer') or {}).get('liveBroadcastDetails') or {}
        if live.get('isLiveNow'):
            continue
        return duration_seconds(details.get('lengthSeconds'))
    return ''


def resolve_duration(url, http):
    provider, vid = video_identity(url)
    if provider == 'youtube':
        raw = http.get_bytes('https://www.youtube.com/watch?v=' + vid + '&hl=zh-CN',
                             timeout=12, max_bytes=6 * 1024 * 1024, allow_redirects=False)
        return youtube_player_duration(raw.decode('utf-8', errors='replace'), vid)
    if provider == 'bilibili':
        data = http.get_json('https://api.bilibili.com/x/web-interface/view',
            params={'bvid': vid} if vid.startswith('BV') else {'aid': vid[2:]},
            headers={'Referer': 'https://www.bilibili.com/'}, timeout=12)
        if data.get('code') == 0:
            return duration_seconds((data.get('data') or {}).get('duration'))
    return ''


class DurationCache:
    """Two workers, bounded queue, deduplication and a short failure cooldown."""

    def __init__(self, http):
        self.http = http
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='duration')
        self._lock = threading.Lock()
        self._cache = {}
        self._pending = {}
        self._epoch = 0
        self._closed = False

    def request(self, url, done):
        identity = video_identity(url)
        if not identity[0]:
            return False
        with self._lock:
            if self._closed:
                return False
            hit = self._cache.get(identity)
            if hit and (hit[0] or time.monotonic() - hit[1] < 60):
                cached = hit[0]
            else:
                cached = None
                key = (self._epoch, identity)
                if key in self._pending:
                    self._pending[key].append(done)
                    return True
                if len(self._pending) >= 24:
                    return False
                self._pending[key] = [done]
        if cached is not None:
            done(cached)
            return True
        try:
            self._pool.submit(self._fetch, key, url)
        except RuntimeError:
            with self._lock:
                self._pending.pop(key, None)
            return False
        return True

    def _fetch(self, key, url):
        with self._lock:
            if self._closed or key[0] != self._epoch:
                return
        try:
            text = resolve_duration(url, self.http)
        except Exception:
            text = ''
        with self._lock:
            if self._closed or key[0] != self._epoch:
                return
            self._cache[key[1]] = (text, time.monotonic())
            while len(self._cache) > 1000:
                self._cache.pop(next(iter(self._cache)))
            callbacks = self._pending.pop(key, [])
        for callback in callbacks:
            try:
                callback(text)
            except (RuntimeError, ReferenceError):
                pass  # Window may already have been destroyed.

    def reset(self):
        with self._lock:
            self._epoch += 1
            self._pending.clear()

    def shutdown(self):
        with self._lock:
            self._closed = True
        self.reset()
        self._pool.shutdown(wait=False, cancel_futures=True)
