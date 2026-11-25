"""Live provider parsing plus missing-duration-to-visible-badge frozen smoke."""
import time


def run(app, win, log, shot=''):
    from .adapters.youtube import YouTubeAdapter
    from .adapters.base import SearchContext
    site = win.config.get_site('ytb')
    adapter = YouTubeAdapter()
    rows = []
    for page in (1, 2):
        batch = adapter.search(SearchContext('喝水 血糖', win.http, page=page), site)
        rows.extend(batch)
        log('YouTube duration live page %d: %d/%d with duration' % (
            page, sum(bool(r.duration) for r in batch), len(batch)))
        if batch.exhausted:
            break
    known = [row for row in rows if row.duration]
    if len(known) < 3:
        raise RuntimeError('真实 YouTube 搜索缺少可核对的时长')
    expected = known[0].duration
    known[0].duration = ''
    win._clear_results()
    win._append_results(known[:4])
    win._pump_thumbs()
    deadline = time.monotonic() + 35
    while not known[0].duration and time.monotonic() < deadline:
        time.sleep(.05)
        app.processEvents()
    if known[0].duration != expected:
        raise RuntimeError('后台时长补全与原搜索页不一致：%s / %s' % (known[0].duration, expected))
    card = win._cards[0]
    if card.time_badge.isHidden() or card.time_badge.text() != expected:
        raise RuntimeError('时长已取回但角标未显示')
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not all(c.has_cover for c in win._cards):
        time.sleep(.05)
        app.processEvents()
    if shot:
        win.grab().save(shot)
    log('YouTube missing duration recovered: %s; visible badge PASS; covers %d/%d' % (
        expected, sum(c.has_cover for c in win._cards), len(win._cards)))
    win._clear_results()
