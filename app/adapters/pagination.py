"""Pagination evidence travels with a list, before filtering and deduplication."""
from collections import OrderedDict
import threading
import time


class PageResults(list):
    def __init__(self, rows=(), *, exhausted=None, supported=True, message=''):
        super().__init__(rows)
        self.exhausted = exhausted
        self.supported = supported
        self.message = message


_lock = threading.RLock()


def stream_state(ctx, site):
    """Per-client, per-query cursor; retries reuse a page, never consume it twice."""
    key = (site.id, site.search_url, site.route, ctx.query, ctx.max_results, ctx.resource_type)
    with _lock:
        cache = getattr(ctx.http, '_pagination_cache', None)
        if not isinstance(cache, OrderedDict):
            cache = ctx.http._pagination_cache = OrderedDict()
        now = time.monotonic()
        state = cache.get(key)
        if ctx.reset or state is None or now - state['at'] > 1800:
            state = dict(at=now, lock=threading.RLock(), pages={}, token=None,
                         buffer=[], config={}, initialized=False)
            cache[key] = state
        cache.move_to_end(key)
        while len(cache) > 16:
            cache.popitem(last=False)
        return state
