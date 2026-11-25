"""Baidu's video search uses /sf/vsearch with pn offsets of ten (page.js)."""
import re
from dataclasses import replace
from urllib.parse import urlencode
from bs4 import BeautifulSoup
from .base import Adapter
from .generic import GenericAdapter
from .pagination import PageResults, stream_state
from ..models import SearchResult, parse_pub_ts
from ..net import NetworkError


class BaiduAdapter(Adapter):
    name = 'baidu'

    def search(self, ctx, site):
        state = stream_state(ctx, site)
        with state['lock']:
            page = max(1, ctx.page)
            if page in state['pages']:
                return state['pages'][page]
            if page != len(state['pages']) + 1:
                raise NetworkError('百度视频续页会话已过期，请重新搜索')
            buffer = list(state['buffer'])
            native_page = state.get('native_page', 1)
            ended = state.get('ended', False)
            if not buffer and not ended:
                native = self._fetch(replace(ctx, page=native_page), site)
                buffer = list(native)
                ended = native.exhausted is True
                native_page += 1
            if ctx.cancelled():
                raise NetworkError('已停止')
            out, remaining = buffer[:ctx.max_results], buffer[ctx.max_results:]
            result = PageResults(out, exhausted=ended and not remaining)
            state.update(buffer=remaining, ended=ended, native_page=native_page)
            state['pages'][page] = result
            return result

    def _fetch(self, ctx, site):
        # The server fixes native pages at ten; do not advance by the UI limit.
        params = {'pd': 'video', 'tn': 'vsearch', 'ie': 'utf-8', 'wd': ctx.query,
                  'pn': (max(1, ctx.page) - 1) * 10}
        if ctx.page > 1:
            params['async'] = 1
        url = 'https://www.baidu.com/sf/vsearch?' + urlencode(params)
        html = ctx.http.get_text(url, site)
        soup = BeautifulSoup(html, 'html.parser')
        rows = []
        for card in soup.select('.video_list.video_short'):
            a = card.select_one('a.video-title[href]')
            if a is None:
                continue
            title, link = a.get_text(' ', strip=True), a['href'].strip()
            if not title or not link.startswith(('http://', 'https://')):
                continue
            if link.startswith('http://www.baidu.com/'):
                link = 'https://' + link[len('http://'):]
            img = card.select_one('img')
            timer = card.select_one('.video_play_timer')
            rows.append(SearchResult(title=title, url=link, source=site.display, source_id=site.id,
                cover=GenericAdapter._img_src(img), duration=timer.get_text(strip=True) if timer else '',
                pub_ts=parse_pub_ts(card.get_text(' ', strip=True))))
        text = soup.get_text(' ', strip=True)
        ended = bool(soup.select_one('.video_noMoreResult')) or bool(
            re.search(r'没有找到与|没有更多内容|找到相关视频\s*0\s*条', text))
        if not rows and not ended:
            raise NetworkError('百度视频未返回可解析的视频或结束标记，可能需要验证，滚动可重试')
        ctx.log('%s 第 %d 页 → %d 条' % (site.display, ctx.page, len(rows)))
        return PageResults(rows, exhausted=ended)
