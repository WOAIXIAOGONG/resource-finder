"""Continuation protocol, failure-vs-end evidence, channel filters and footer."""
import testenv
import sys
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Config, SiteConfig
from app.models import SearchResult, SiteEvent
from app.adapters import adapter_for
from app.adapters.youtube import YouTubeAdapter
from app.adapters.baidu import BaiduAdapter
from app.adapters.bing import BingAdapter
from app.adapters.bilibili import BilibiliAdapter
from app.adapters.generic import GenericAdapter
from app.adapters.base import SearchContext
from app.adapters.pagination import PageResults
from app.engine import SearchEngine
from app.net import NetworkError
from paging_test import SelectedPagingTests


def video(i):
    return {'videoRenderer': {'videoId': str(i), 'title': {'simpleText': 'Video ' + str(i)}}}


def token(t):
    return {'continuationItemRenderer': {'continuationEndpoint': {'continuationCommand': {'token': t}}}}


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.site = Config().get_site('ytb')
        self.http = Mock()
        cfg = {'INNERTUBE_CONTEXT': {'client': {'clientVersion': 'test', 'clientName': 'WEB'}}}
        data = {'contents': {'sectionListRenderer': {'contents': [video(1), video(2), video(3), token('next')]}}}
        self.http.get_text.return_value = 'ytcfg.set(%s);var ytInitialData = %s;' % (json.dumps(cfg), json.dumps(data))
        self.http.post_json.return_value = {'onResponseReceivedCommands': [
            {'appendContinuationItemsAction': {'continuationItems': [video(4), video(5)]}}]}
        self.adapter = YouTubeAdapter()

    def page(self, page, **kwargs):
        return self.adapter.search(SearchContext('cats', self.http, max_results=2, page=page, **kwargs), self.site)

    def test_youtube_keeps_remainder_retries_same_page_and_uses_token(self):
        self.assertFalse(self.page(1).exhausted)
        self.assertEqual([r.title for r in self.page(2)], ['Video 3'])
        self.http.post_json.assert_not_called()
        final = self.page(3)
        self.assertEqual(len(final), 2)
        self.assertTrue(final.exhausted)
        self.assertEqual(self.page(3), final)
        self.assertEqual(self.http.post_json.call_count, 1)
        self.assertEqual(self.http.post_json.call_args.kwargs['json']['continuation'], 'next')

    def test_main_continuation_wins_over_nested_shelf(self):
        data = {'contents': [{'shelf': {'contents': [token('wrong')]}}, token('main')]}
        self.assertEqual(self.adapter._continuation(data), 'main')

    def test_lost_browser_page_recovers_same_query_without_login_or_duplicates(self):
        from app.net import SearchSessionExpired
        initial={'contents':{'sectionListRenderer':{'contents':[video(1),video(2),token('old')]}}}
        fresh={'contents':{'sectionListRenderer':{'contents':[video(1),video(2),token('fresh')]}}}
        config={'INNERTUBE_CONTEXT':{'client':{'clientVersion':'test'}},'_rf_browser_session':['background','old']}
        browser=Mock(active=True)
        browser.search_page.side_effect=[(initial,config),(fresh,dict(config,_rf_browser_session=['background','new']))]
        browser.continuation.side_effect=[SearchSessionExpired('lost'),{'onResponseReceivedCommands':[
            {'appendContinuationItemsAction':{'continuationItems':[video(3),video(4)]}}]}]
        self.http.settings._login_browser=browser
        self.assertEqual([r.title for r in self.page(1)],['Video 1','Video 2'])
        recovered=self.page(2)
        self.assertEqual([r.title for r in recovered],['Video 3','Video 4'])
        self.assertTrue(recovered.exhausted)
        self.assertEqual(browser.search_page.call_count,2)
        self.assertEqual(browser.continuation.call_args.kwargs['session'],['background','new'])
        self.assertEqual(self.page(2),recovered)

    def test_recovery_does_not_swallow_true_account_requirement(self):
        from app.net import SearchSessionExpired
        from app.login import LoginRequired
        config={'INNERTUBE_CONTEXT':{'client':{'clientVersion':'test'}},'_rf_browser_session':['background','old']}
        browser=Mock(active=True)
        browser.search_page.side_effect=[({'contents':{'sectionListRenderer':{'contents':[video(1),token('old')]}}},config),LoginRequired('Please sign in')]
        browser.continuation.side_effect=SearchSessionExpired('lost')
        self.http.settings._login_browser=browser
        self.page(1)
        with self.assertRaises(LoginRequired):self.page(2)

    def test_recovery_dedup_survives_bounded_lookahead_into_next_request(self):
        from app.net import SearchSessionExpired
        config={'INNERTUBE_CONTEXT':{'client':{'clientVersion':'test'}},'_rf_browser_session':['background','old']}
        def doc(ids,t):return {'contents':{'sectionListRenderer':{'contents':[*(video(i) for i in ids),token(t)]}}}
        def continuation(ids,t):return {'onResponseReceivedCommands':[{'appendContinuationItemsAction':{'continuationItems':[
            *(video(i) for i in ids),*([token(t)] if t else [])]}}]}
        browser=Mock(active=True)
        browser.search_page.side_effect=[(doc(range(1,9),'old'),config),(doc([1,2],'fresh'),config)]
        browser.continuation.side_effect=[SearchSessionExpired('lost'),continuation([3,4],'f2'),continuation([5,6],'f3'),continuation([7,8],'f4'),continuation([9],None)]
        self.http.settings._login_browser=browser
        for page in range(1,5):self.assertEqual(len(self.page(page)),2)
        wait=self.page(5);self.assertEqual(wait,[]);self.assertFalse(wait.exhausted)
        final=self.page(6);self.assertEqual([r.title for r in final],['Video 9']);self.assertTrue(final.exhausted)

    def test_new_shorts_only_page_is_parsed_instead_of_no_video_error(self):
        short={'shortsLockupViewModel':{'onTap':{'innertubeCommand':{'reelWatchEndpoint':{'videoId':'abcdefghijk'}}},
            'overlayMetadata':{'primaryText':{'content':'Nature short'},'secondaryText':{'content':'1234 views'}},
            'thumbnail':{'sources':[{'url':'https://i.ytimg.com/a.jpg','width':100,'height':200}]}}}
        rows=self.adapter._parse_results({'items':[short]},self.site)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0].views,1234)
        self.assertEqual(rows[0].url,'https://www.youtube.com/watch?v=abcdefghijk')

    def test_reel_and_compact_video_variants_remain_videos(self):
        data={'items':[{'reelItemRenderer':{'videoId':'abcdefghijk','headline':{'simpleText':'Short'}}},
                       {'compactVideoRenderer':{'videoId':'12345678901','title':{'simpleText':'Long'}}}]}
        self.assertEqual(len(self.adapter._parse_results(data,self.site)),2)

    def test_legacy_continuation_and_nested_executor_token(self):
        legacy={'continuationContents':{'sectionListContinuation':{'contents':[video(4)],'continuations':[{'nextContinuationData':{'continuation':'legacy'}}]}}}
        items=self.adapter._response_items(legacy)
        self.assertEqual(self.adapter._continuation(items),'legacy')
        nested={'continuationItemRenderer':{'continuationEndpoint':{'commandExecutorCommand':{'commands':[{'continuationCommand':{'token':'nested'}}]}}}}
        self.assertEqual(self.adapter._continuation(nested),'nested')

    def test_empty_command_key_does_not_hide_nonempty_action_key(self):
        data={'onResponseReceivedCommands':[],'onResponseReceivedActions':[{'appendContinuationItemsAction':{'continuationItems':[video(4)]}}]}
        self.assertEqual(len(self.adapter._parse_results(self.adapter._response_items(data),self.site)),1)

    def test_reload_items_response_and_explicit_terminal_empty(self):
        self.page(1);self.page(2)
        self.http.post_json.return_value={'onResponseReceivedEndpoints':[{'reloadContinuationItemsCommand':{'continuationItems':[video(4)]}}]}
        self.assertEqual(self.page(3)[0].title,'Video 4')
        self.assertTrue(self.adapter._valid_empty([{'reloadContinuationItemsCommand':{'continuationItems':[]}}]))

    def test_empty_nested_shelf_is_not_main_list_end(self):
        self.assertFalse(self.adapter._valid_empty({'shelf':{'continuationItems':[]}}))
        self.assertTrue(self.adapter._valid_empty({'sectionListRenderer':{'contents':[{'messageRenderer':{'text':{'simpleText':'No results found'}}}]}}))

    def test_sidebar_tokens_and_videos_do_not_override_primary_search(self):
        data={'twoColumnSearchResultsRenderer':{'primaryContents':{'contents':[video(1),token('main')]},'secondaryContents':{'contents':[video(9),token('sidebar')]}}}
        primary=self.adapter._primary(data)
        self.assertEqual(self.adapter._continuation(primary),'main')
        self.assertEqual(len(self.adapter._parse_results(primary,self.site)),1)

    def test_nonvideo_interstitial_with_cursor_is_followed_in_same_page(self):
        self.page(1);self.page(2)
        self.http.post_json.side_effect=[{'onResponseReceivedCommands':[{'appendContinuationItemsAction':{'continuationItems':[token('skip')]}}]},
            {'onResponseReceivedCommands':[{'appendContinuationItemsAction':{'continuationItems':[video(4)]}}]}]
        rows=self.page(3);self.assertEqual(len(rows),1);self.assertEqual(self.http.post_json.call_count,2)

    def test_failed_continuation_preserves_token_and_page(self):
        self.page(1); self.page(2)
        self.http.post_json.side_effect = NetworkError('timeout')
        with self.assertRaises(NetworkError): self.page(3)
        self.http.post_json.side_effect = None
        self.assertTrue(self.page(3).exhausted)
        self.assertEqual(self.http.post_json.call_count, 2)

    def test_invalid_continuation_is_not_exhaustion(self):
        self.page(1); self.page(2)
        self.http.post_json.return_value = {'error': 'login'}
        with self.assertRaises(NetworkError): self.page(3)

    def test_explicit_empty_continuation_is_end(self):
        self.page(1); self.page(2)
        self.http.post_json.return_value = {'onResponseReceivedCommands': [
            {'appendContinuationItemsAction': {'continuationItems': []}}]}
        self.assertTrue(self.page(3).exhausted)

    def test_generic_youtube_message_is_not_no_results(self):
        self.assertFalse(self.adapter._valid_empty({'messageRenderer': {'text': 'Please sign in'}}))
        self.assertTrue(self.adapter._valid_empty({'messageRenderer': {'text': 'No results found'}}))

    def test_localized_no_more_results_finishes_same_query_without_login(self):
        for text in ['无更多结果', '没有更多结果。', '沒有更多結果', '無更多結果',
                     'No more results', 'No more results found.', '没有找到任何结果']:
            with self.subTest(message=text):
                self.setUp();self.page(1);self.page(2)
                self.http.post_json.return_value = {'onResponseReceivedCommands': [
                    {'appendContinuationItemsAction': {'continuationItems': [
                        {'itemSectionRenderer': {'contents': [
                            {'messageRenderer': {'text': {'runs': [{'text': text}]}}}]}}]}}]}
                final=self.page(3)
                self.assertEqual(final, []);self.assertTrue(final.exhausted)
                self.assertEqual(self.page(3),final);self.assertEqual(self.http.post_json.call_count,1)

    def test_unknown_message_retries_same_cursor_without_login_or_false_end(self):
        self.page(1);self.page(2)
        self.http.post_json.return_value={'onResponseReceivedCommands': [
            {'appendContinuationItemsAction': {'continuationItems': [
                {'itemSectionRenderer': {'contents': [
                    {'messageRenderer': {'text': {'simpleText': 'Temporary page unavailable'}}}]}}]}}]}
        for _ in range(2):
            with self.assertRaisesRegex(NetworkError, '触底可重试'):self.page(3)
        self.assertEqual([c.kwargs['json']['continuation'] for c in self.http.post_json.call_args_list],['next','next'])
        self.http.post_json.return_value={'onResponseReceivedCommands': [
            {'appendContinuationItemsAction': {'continuationItems': [video(4)]}}]}
        self.assertEqual(self.page(3)[0].title,'Video 4')

    def test_real_login_notice_still_requires_login_on_continuation(self):
        from app.login import LoginRequired
        self.page(1);self.page(2)
        self.http.post_json.return_value={'onResponseReceivedCommands': [
            {'appendContinuationItemsAction': {'continuationItems': [
                {'messageRenderer': {'text': {'simpleText': 'Sign in to confirm your age'}}}]}}]}
        with self.assertRaises(LoginRequired):self.page(3)

    def test_terminal_words_in_video_title_or_long_error_do_not_end_search(self):
        for data in [{'videoRenderer': {'title':{'simpleText':'无更多结果'}}},
                     {'messageRenderer': {'text': {'simpleText':'暂时无法获取数据，提示No more results仅供参考'}}}]:
            self.assertFalse(self.adapter._valid_empty(data))

    def test_cancel_does_not_commit_page(self):
        cancelled = threading.Event(); cancelled.set()
        with self.assertRaises(NetworkError): self.page(1, cancel=cancelled)
        self.assertEqual(len(self.page(1)), 2)

    def test_missing_cursor_requires_restart_not_false_end(self):
        with self.assertRaisesRegex(NetworkError, '重新搜索'): self.page(2)

    def test_baidu_legacy_config_and_real_offsets(self):
        site = Config().get_site('baidu')
        self.assertIsInstance(adapter_for(site), BaiduAdapter)
        self.http.get_text.return_value = '<div class="video_list video_short"><a class="video-title" href="https://haokan.baidu.com/v?vid=1">猫的视频</a><img src="https://img.example/a.jpg"></div>'
        rows = adapter_for(site)._fetch(SearchContext('猫', self.http, page=3), site)
        self.assertIn('pn=20', self.http.get_text.call_args.args[0])
        self.assertIn('async=1', self.http.get_text.call_args.args[0])
        self.assertFalse(rows.exhausted)
        self.assertTrue(rows[0].cover)

    def test_baidu_explicit_end_and_challenge(self):
        adapter = BaiduAdapter()
        self.http.get_text.return_value = '<div class="video_noMoreResult">没有更多内容了</div>'
        self.assertTrue(adapter._fetch(SearchContext('猫', self.http), self.site).exhausted)
        self.http.get_text.return_value = '<html>安全验证</html>'
        with self.assertRaises(NetworkError): adapter._fetch(SearchContext('猫', self.http), self.site)

    def test_bing_native_offset_buffer_and_bounded_duplicate_lookahead(self):
        adapter = BingAdapter()
        site = Config().get_site('bing')
        first = PageResults([SearchResult(title=str(n), url='https://youtube.com/watch?v=%s' % n) for n in range(30)], exhausted=False)
        first.next_offset = 31
        second = PageResults([SearchResult(title='new', url='https://youtube.com/watch?v=more')], exhausted=False)
        with patch.object(adapter, '_videos', side_effect=[first, first, second, second, second, second]) as fetch:
            def page(p): return adapter.search(SearchContext('cat', self.http, page=p, max_results=20), site)
            self.assertEqual(len(page(1)), 20)
            self.assertEqual(len(page(2)), 10)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(len(page(3)), 1)
            self.assertEqual(fetch.call_args_list[1].kwargs['first'], 31)
            empty = page(4)
            self.assertEqual(len(empty), 0)
            self.assertFalse(empty.exhausted)
            self.assertIn('重复', empty.message)
            self.assertEqual(fetch.call_count, 6)

    def test_custom_baidu_css_is_not_overwritten(self):
        site = Config().get_site('baidu'); site.css = {'item': '.custom'}
        self.assertIsInstance(adapter_for(site), GenericAdapter)

    def test_generic_unsupported_is_distinct_from_end(self):
        site = SiteConfig(search_url='https://example.com/?q={q}')
        rows = GenericAdapter().search(SearchContext('x', self.http, page=2), site)
        self.assertFalse(rows.supported)
        self.assertIsNone(rows.exhausted)
        self.http.get_text.assert_not_called()

    def test_bing_uses_scroll_endpoint_and_extracts_video_card(self):
        meta = json.dumps({'pgurl': 'https://youtube.com/watch?v=123', 'turl': 'https://img.example/a.jpg'})
        self.http.get_text.return_value = '<div mmeta=\'%s\'><div class="mc_vtvc_title" title="Cat video"></div></div>' % meta
        site = Config().get_site('bing')
        rows = BingAdapter()._videos(SearchContext('cats', self.http, page=3), site, 'cats', first=41)
        self.assertIn('/videos/async/rankedans?', self.http.get_text.call_args.args[0])
        self.assertIn('first=41', self.http.get_text.call_args.args[0])
        self.assertEqual(rows[0].source_id, 'bing')
        self.assertFalse(rows.exhausted)

    def test_bilibili_total_pages_is_end_evidence(self):
        adapter = BilibiliAdapter()
        with patch.object(adapter, '_call', return_value={'numPages': 2, 'result': []}):
            self.assertFalse(adapter.search(SearchContext('猫', self.http, page=1), self.site).exhausted)
            self.assertTrue(adapter.search(SearchContext('猫', self.http, page=2), self.site).exhausted)

    def test_engine_preserves_empty_page_metadata(self):
        cfg = Config(); engine = SearchEngine(cfg, self.http); events = []
        with patch('app.engine.adapter_for') as factory:
            factory.return_value.search.return_value = PageResults(exhausted=True)
            engine.run('猫', events.append, sites=[cfg.get_site('baidu')])
        done = next(e for e in events if e.kind == 'done')
        self.assertEqual(done.results, [])
        self.assertTrue(done.exhausted)


class PaginationUITests(unittest.TestCase):
    setUpClass = classmethod(SelectedPagingTests.setUpClass.__func__)
    tearDownClass = classmethod(SelectedPagingTests.tearDownClass.__func__)
    setUp = SelectedPagingTests.setUp
    tearDown = SelectedPagingTests.tearDown
    seed = SelectedPagingTests.seed
    wheel = SelectedPagingTests.wheel
    wait = SelectedPagingTests.wait

    def youtube_notice_scroll(self, message, terminal):
        self.win._set_mode('direct');self.win.settings.show_thumbnails=False
        site=self.win.config.get_site('ytb')
        for configured in self.win.config.sites:configured.enabled=configured.id==site.id
        first={'contents':{'sectionListRenderer':{'contents':[video(1),token('next')]}}}
        cfg={'INNERTUBE_CONTEXT':{'client':{'clientName':'WEB','clientVersion':'test'}}}
        html='ytcfg.set(%s);var ytInitialData = %s;' % (json.dumps(cfg),json.dumps(first))
        response={'onResponseReceivedCommands':[{'appendContinuationItemsAction':{'continuationItems':[
            {'itemSectionRenderer':{'contents':[{'messageRenderer':{'text':{'runs':[{'text':message}]}}}]}}]}}]}
        with patch.object(self.win.http,'get_text',return_value=html), patch.object(self.win.http,'post_json',return_value=response) as post, patch.object(self.win.logins,'require',wraps=self.win.logins.require) as require:
            events=[];self.win.engine.run('cats',events.append,self.win._cancel,sites=[site])
            self.win._append_results(next(e.results for e in events if e.kind=='done'))
            self.win._query='cats';self.win._site_pages={site.id:1}
            for n in range(3):
                self.wheel()
                self.wait(lambda: not self.win._paging and post.call_count>=(1 if terminal else n+1))
            require.assert_not_called()
            self.assertFalse(self.win._login_dialogs)
            self.assertEqual(len(self.win._all_results),1)
            self.assertEqual(post.call_count,1 if terminal else 3)
            if terminal:
                self.assertIn(site.id,self.win._site_exhausted)
                self.assertIn('YouTube',self.win.pagination_notice.text())
                self.assertIn('没有更多资源',self.win.pagination_notice.text())
            else:
                self.assertNotIn(site.id,self.win._site_exhausted)
                self.assertEqual(self.win._site_pages[site.id],1)
                self.assertIn('重试',self.win.pagination_notice.text())
                self.assertTrue(all(c.kwargs['json']['continuation']=='next' for c in post.call_args_list))

    def test_real_terminal_message_stops_repeated_bottom_scroll_without_login_popup(self):
        self.youtube_notice_scroll('无更多结果',True)

    def test_unknown_notice_repeated_scroll_keeps_results_and_retries_without_login_popup(self):
        self.youtube_notice_scroll('Temporary page unavailable',False)

    def finish_page(self, **kw):
        self.win._paging = True
        self.win._paging_new = 0
        self.win._paging_failed = False
        self.win._paging_pages = {'bili': 2}
        self.win._paging_source_keys = {'platform:bilibili'}
        self.win._on_paging_event(SiteEvent(kind='done', site_id='bili', **kw))
        self.win._on_paging_event(SiteEvent(kind='finish'))

    def test_duplicates_and_unknown_empty_do_not_exhaust(self):
        self.seed()
        self.finish_page(results=list(self.win._all_results))
        self.assertNotIn('bili', self.win._site_exhausted)
        self.assertFalse(self.win._no_more)

    def test_partial_failure_preserves_successful_page_counter(self):
        self.seed()
        self.win._site_pages['ytb'] = 2
        self.win._page = 2
        self.win._paging = True
        self.win._paging_new = 1
        self.win._paging_failed = True
        self.win._finish_paging()
        self.assertEqual(self.win._page, 2)
        self.assertEqual(self.win._site_pages['bili'], 1)
        self.finish_page(results=[], exhausted=None)
        self.assertFalse(self.win._no_more)

    def test_confirmed_end_shows_named_footer_and_toast_once(self):
        self.seed()
        with patch.object(self.win, '_toast_show') as toast:
            self.finish_page(exhausted=True)
            self.assertIn('没有更多资源', self.win.pagination_notice.text())
            self.assertIn('哔哩哔哩', self.win.pagination_notice.text())
            self.assertTrue(self.win._no_more)
            toast.assert_called_once()
            self.win._load_more()
            toast.assert_called_once()

    def test_unsupported_does_not_claim_end(self):
        self.seed()
        self.finish_page(pagination_supported=False, pagination_message='尚未配置分页地址')
        self.assertNotIn('bili', self.win._site_exhausted)
        self.assertIn('尚未配置', self.win.pagination_notice.text())

    def test_confirmed_end_does_not_display_stale_retry_message(self):
        self.seed()
        self.finish_page(exhausted=True)
        self.win._site_page_notes['bili'] = '旧请求失败，再次触底可重试'
        self.win._show_page_status()
        self.assertIn('没有更多资源', self.win.pagination_notice.text())
        self.assertNotIn('重试', self.win.pagination_notice.text())

    def test_bing_discovered_youtube_stays_visible_and_can_continue(self):
        self.seed()
        row = SearchResult(title='cat', url='https://youtube.com/watch?v=abc', source_id='bing')
        self.win._append_results([row])
        self.win._source_all_clicked(False)
        self.win._source_checks['platform:bing'].click()
        self.assertEqual(len(self.win._cards), 1)
        self.assertEqual([s.id for s in self.win._selected_search_sites()], ['bing'])
        self.assertTrue(self.win._matches_source(row, {'platform:youtube'}))

    def test_duplicate_retains_discovery_channel(self):
        self.seed()
        self.win._append_results([SearchResult(title='duplicate', url=self.win._all_results[0].url, source_id='bing')])
        self.win._source_all_clicked(False)
        self.win._source_checks['platform:bing'].click()
        self.assertEqual(len(self.win._cards), 1)


del SelectedPagingTests
if __name__ == '__main__':
    unittest.main(verbosity=2)
