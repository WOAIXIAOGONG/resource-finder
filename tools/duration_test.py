"""Duration source parsing, asynchronous recovery and badge lifecycle regressions."""
import testenv
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.durations import DurationCache, duration_text, duration_seconds, youtube_player_duration, resolve_duration
from app.adapters.youtube import YouTubeAdapter
from app.config import Config
from app.models import SearchResult
import access_test

URL = 'https://www.youtube.com/watch?v=NRamoL8Kbqg'


class DurationTests(unittest.TestCase):
    def test_renderer_overlay_and_runs_are_duration_sources(self):
        site = Config().get_site('ytb'); adapter = YouTubeAdapter()
        base = {'videoId': 'NRamoL8Kbqg', 'title': {'simpleText': 'Title with 23:45'}}
        overlay = {'thumbnailOverlayTimeStatusRenderer': {'style': 'DEFAULT', 'text': {'runs': [{'text': '8:43'}]}}}
        self.assertEqual(adapter._to_result({**base, 'thumbnailOverlays': [overlay]}, site).duration, '8:43')
        self.assertEqual(adapter._to_result({**base, 'lengthText': {'runs': [{'text': '1:02:03'}]}}, site).duration, '1:02:03')
        self.assertEqual(adapter._to_result(base, site).duration, '')

    def test_lockup_duration_and_live_badges(self):
        site = Config().get_site('ytb'); adapter = YouTubeAdapter()
        base = {'contentId': 'NRamoL8Kbqg', 'metadata': {'lockupMetadataViewModel': {'title': {'content': 'Video'}}}}
        def lockup(text):
            return {**base, 'contentImage': {'thumbnailViewModel': {'overlays': [
                {'thumbnailOverlayBadgeViewModel': {'thumbnailBadges': [
                    {'thumbnailBadgeViewModel': {'text': {'content': text}}}]}}]}}}
        self.assertEqual(adapter._from_lockup(lockup('\u202a8:43\u202c'), site).duration, '8:43')
        self.assertEqual(adapter._from_lockup(lockup('LIVE'), site).duration, '')

    def test_player_matches_exact_video_and_ignores_recommendations(self):
        player = {'videoDetails': {'videoId': 'NRamoL8Kbqg', 'lengthSeconds': '523', 'isLiveContent': True}}
        html = 'var ytInitialPlayerResponse = ' + json.dumps(player) + ';'
        self.assertEqual(youtube_player_duration(html, 'NRamoL8Kbqg'), '8:43')
        self.assertEqual(youtube_player_duration(html, 'other'), '')
        self.assertEqual(youtube_player_duration('var ytInitialData={"lengthSeconds":"523"};', 'NRamoL8Kbqg'), '')
        player['videoDetails']['isLive'] = True
        self.assertEqual(youtube_player_duration('ytInitialPlayerResponse='+json.dumps(player), 'NRamoL8Kbqg'), '')

    def test_bad_player_and_nonfinite_seconds_remain_unknown(self):
        for value in [None, 0, -1, 'nan', 'inf', {}, 'garbage']:
            self.assertEqual(duration_seconds(value), '')
        self.assertEqual(duration_seconds(3723), '1:02:03')
        self.assertEqual(youtube_player_duration('ytInitialPlayerResponse = broken;', 'NRamoL8Kbqg'), '')
        self.assertEqual(duration_text('标题 8:43'), '')
        self.assertEqual(duration_text('8:99'), '')

    def test_provider_requests_are_fixed_bounded_and_unrelated_hosts_not_fetched(self):
        http = Mock()
        http.get_bytes.return_value = b'ytInitialPlayerResponse={"videoDetails":{"videoId":"NRamoL8Kbqg","lengthSeconds":"523"}};'
        self.assertEqual(resolve_duration(URL, http), '8:43')
        self.assertFalse(http.get_bytes.call_args.kwargs['allow_redirects'])
        self.assertEqual(http.get_bytes.call_args.kwargs['max_bytes'], 6*1024*1024)
        http.reset_mock()
        for url in ['http://127.0.0.1/watch?v=NRamoL8Kbqg', 'https://youtube.com.evil/watch?v=NRamoL8Kbqg']:
            self.assertEqual(resolve_duration(url, http), '')
        http.get_bytes.assert_not_called(); http.get_json.assert_not_called()
        http.get_json.return_value = {'code': 0, 'data': {'duration': 3723}}
        self.assertEqual(resolve_duration('https://www.bilibili.com/video/BV123', http), '1:02:03')

    def test_cache_coalesces_aliases_and_reuses_success(self):
        cache = DurationCache(Mock()); entered = threading.Event(); release = threading.Event(); done = threading.Event(); values = []
        def fetch(*args): entered.set(); release.wait(3); return '8:43'
        def callback(text):
            values.append(text)
            if len(values) == 2: done.set()
        try:
            with patch('app.durations.resolve_duration', side_effect=fetch) as resolve:
                self.assertTrue(cache.request(URL, callback)); self.assertTrue(entered.wait(2))
                self.assertTrue(cache.request('https://youtu.be/NRamoL8Kbqg', callback)); release.set()
                self.assertTrue(done.wait(2)); cache.request(URL, values.append)
                self.assertEqual(values, ['8:43']*3); self.assertEqual(resolve.call_count, 1)
        finally: release.set(); cache.shutdown(); cache._pool.shutdown(wait=True)

    def test_failed_recovery_has_cooldown_and_later_retry(self):
        cache = DurationCache(Mock()); done = threading.Event(); values = []
        def callback(text): values.append(text); done.set()
        try:
            with patch('app.durations.resolve_duration', side_effect=[RuntimeError('offline'), '8:43']) as resolve:
                cache.request(URL, callback); self.assertTrue(done.wait(2)); done.clear()
                cache.request(URL, callback); self.assertTrue(done.wait(2)); self.assertEqual(resolve.call_count, 1)
                for key, (text, at) in list(cache._cache.items()): cache._cache[key] = (text, at-61)
                done.clear(); cache.request(URL, callback); self.assertTrue(done.wait(2))
                self.assertEqual(values, ['', '', '8:43'])
        finally: cache.shutdown(); cache._pool.shutdown(wait=True)

    def test_reset_and_shutdown_discard_inflight_callbacks(self):
        cache = DurationCache(Mock()); entered = threading.Event(); release = threading.Event(); values = []
        def fetch(*args): entered.set(); release.wait(3); return '8:43'
        try:
            with patch('app.durations.resolve_duration', side_effect=fetch):
                cache.request(URL, values.append); self.assertTrue(entered.wait(2)); cache.reset(); release.set()
                cache.shutdown(); cache._pool.shutdown(wait=True)
            self.assertEqual(values, []); self.assertFalse(cache.request(URL, values.append))
        finally: release.set(); cache.shutdown()

    def test_queue_is_bounded_while_two_workers_are_busy(self):
        cache = DurationCache(Mock()); release = threading.Event()
        try:
            with patch('app.durations.resolve_duration', side_effect=lambda *a: (release.wait(3), '')[1]):
                accepted = [cache.request('https://youtube.com/watch?v=%011d' % n, lambda t: None) for n in range(30)]
                self.assertEqual(sum(accepted), 24)
                cache.reset(); release.set(); cache._pool.shutdown(wait=True)
        finally: release.set(); cache.shutdown()


class DurationUITests(unittest.TestCase):
    setUpClass = classmethod(access_test.AccessUITests.setUpClass.__func__)
    tearDownClass = classmethod(access_test.AccessUITests.tearDownClass.__func__)
    setUp = access_test.AccessUITests.setUp
    tearDown = access_test.AccessUITests.tearDown

    def test_visible_missing_duration_is_queued_and_badge_updates_without_thumbnail(self):
        self.win.settings.show_thumbnails = False
        row = SearchResult(title='Video', url=URL); self.win._append_results([row]); callbacks = []
        with patch.object(self.win.durations, 'request', side_effect=lambda u, done: (callbacks.append(done), True)[1]):
            self.win._pump_thumbs(); self.assertEqual(len(callbacks), 1)
            card = self.win._cards[0]; self.assertTrue(card.time_badge.isHidden())
            thread = threading.Thread(target=lambda: callbacks[0]('8:43')); thread.start(); thread.join()
            self.app.processEvents()
            self.assertEqual(row.duration, '8:43'); self.assertEqual(card.time_badge.text(), '8:43')
            self.assertFalse(card.time_badge.isHidden())
            self.assertLessEqual(card.time_badge.geometry().right(), card.cover.width())
            self.assertGreater(card.time_badge.x(), card.download_hint.geometry().right())

    def test_existing_duration_and_other_resource_types_are_not_queried(self):
        self.win.settings.show_thumbnails = False
        self.win._append_results([SearchResult(title='Known', url=URL, duration='8:43'),
            SearchResult(title='Image', url='https://youtu.be/abcdefghijk', resource_type='image')])
        with patch.object(self.win.durations, 'request') as request:
            self.win._pump_thumbs(); request.assert_not_called()

    def test_old_search_callback_is_ignored_even_when_new_url_matches(self):
        self.win.settings.show_thumbnails = False
        old = SearchResult(title='old', url=URL); self.win._append_results([old])
        with patch.object(self.win.durations, 'request', return_value=True) as request:
            self.win._pump_thumbs(); callback = request.call_args.args[1]
        self.win._clear_results(); new = SearchResult(title='new', url=URL); self.win._append_results([new])
        with patch.object(self.win.durations, 'request', return_value=False):
            callback('8:43'); self.app.processEvents()
        self.assertEqual(new.duration, ''); self.assertTrue(self.win._cards[0].time_badge.isHidden())

    def test_duplicate_result_can_supply_previously_missing_duration(self):
        self.win.settings.show_thumbnails = False
        self.win._append_results([SearchResult(title='first', url=URL)])
        self.win._append_results([SearchResult(title='again', url=URL, duration='8:43')])
        self.assertEqual(len(self.win._cards), 1)
        self.assertEqual(self.win._cards[0].time_badge.text(), '8:43')


if __name__ == '__main__':
    unittest.main(verbosity=2)
