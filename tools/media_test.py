"""Candidate transport, connection reuse and asynchronous cover regressions."""
import sys
import testenv
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.agent import _to_result
from app.models import SearchResult
from app.config import Settings
from app.covers import cover_url, video_identity
from app.thumbcache import ThumbCache
import access_test


class CandidateTests(unittest.TestCase):
    setUp = access_test.AccessTests.setUp
    tearDown = access_test.AccessTests.tearDown
    get = access_test.AccessTests.get
    post = access_test.AccessTests.post
    ready = access_test.AccessTests.ready
    task = access_test.AccessTests.task

    def search(self, task, count=1):
        site = self.server.access.sources()[0]
        rows = [SearchResult(title='video %d' % i, url='https://www.bilibili.com/video/BVtest%d' % i,
                    source='bilibili', cover='https://i0.hdslb.com/%d.jpg' % i) for i in range(count)]
        with patch('app.adapters.adapter_for', return_value=Mock(search=Mock(return_value=rows))):
            reply = self.post('network/search', task_id=task['id'], source_id=site.id, query='test')
        self.assertEqual(reply.status_code, 200, reply.text)
        return reply.json()['results']

    def test_candidate_ids_preserve_metadata_and_deduplicate(self):
        task = self.task('国内视频')
        row = self.search(task)[0]
        result = self.post('results', task_id=task['id'], candidate_ids=[row['candidate_id']]*2, done=False)
        self.assertEqual(result.json()['accepted'], 1)
        self.assertEqual(self.rows[-1][0][0].cover, row['cover'])
        self.assertEqual(self.rows[-1][0][0].title, row['title'])

    def test_unknown_and_previous_task_ids_do_not_accept_partial_results(self):
        task = self.task('国内视频')
        row = self.search(task)[0]
        reply = self.post('results', task_id=task['id'], candidate_ids=[row['candidate_id'], 'bad'])
        self.assertEqual(reply.status_code, 400)
        self.assertFalse(self.rows)
        new = self.server.push_task('国内新词')
        self.assertEqual(self.post('results', task_id=new['id'], candidate_ids=[row['candidate_id']]).status_code, 400)

    def test_candidate_transport_obeys_hard_limit(self):
        self.ready()
        task = self.server.push_task('国内视频', want=2)
        rows = self.search(task, 4)
        reply = self.post('results', task_id=task['id'], candidate_ids=[r['candidate_id'] for r in rows], done=False)
        self.assertEqual(reply.json()['accepted'], 2)
        self.assertTrue(reply.json()['limit_reached'])

    def test_compact_search_keeps_original_cover_on_server(self):
        task = self.task('国内视频')
        row = SearchResult(title='test', url='https://example.com/watch/a', cover='https://example.com/a.jpg')
        with patch('app.adapters.adapter_for', return_value=Mock(search=Mock(return_value=[row]))):
            reply = self.post('network/search', task_id=task['id'], query='test', compact=True,
                              source_id=self.server.access.sources()[0].id)
        result = reply.json()['results'][0]
        self.assertNotIn('cover', result)
        self.assertEqual(self.post('results', task_id=task['id'], candidate_ids=[result['candidate_id']]).json()['accepted'], 1)
        self.assertEqual(self.rows[-1][0][0].cover, row.cover)

    def test_channel_candidate_pagination_metadata_and_scoped_fetch(self):
        from app.adapters.pagination import PageResults
        self.ready()
        task = self.server.push_task('国内视频', {'source_keys': ['platform:bing']})
        row = SearchResult(title='found via bing', url='https://www.bilibili.com/video/BVtest', source_id='bing')
        with patch('app.adapters.adapter_for', return_value=Mock(search=Mock(return_value=PageResults([row], exhausted=True)))):
            reply = self.post('network/search', task_id=task['id'], query='test', compact=True, source_id='bing')
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertTrue(reply.json()['pagination']['exhausted'])
        cid = reply.json()['results'][0]['candidate_id']
        self.assertEqual(self.post('results', task_id=task['id'], candidate_ids=[cid], done=False).json()['accepted'], 1)
        with patch.object(self.server.access, 'fetch', return_value={'ok': True, 'body': ''}) as fetch:
            self.assertEqual(self.post('network/fetch', task_id=task['id'], url=row.url).status_code, 200)
            self.assertEqual(self.post('network/fetch', task_id=task['id'], url='https://www.youtube.com/watch?v=other').status_code, 400)
        fetch.assert_called_once()

    def test_search_reuses_worker_session_across_http_requests(self):
        task = self.task('国内视频')
        site = self.server.access.sources()[0]
        sessions = []
        def run(ctx, site):
            sessions.append(ctx.http._session(''))
            self.assertFalse(ctx.cancelled())
            return []
        with patch('app.adapters.adapter_for', return_value=Mock(search=run)):
            for page in (1, 2):
                self.assertEqual(self.post('network/search', task_id=task['id'], source_id=site.id,
                                           query='test', page=page).status_code, 200)
        self.assertIs(sessions[0], sessions[1])


class CoverTests(unittest.TestCase):
    def test_youtube_all_card_shapes_recover_omitted_cover_from_exact_video_id(self):
        from app.adapters.youtube import YouTubeAdapter
        from app.config import Config
        from app.covers import youtube_cover
        adapter=YouTubeAdapter();site=Config().get_site('ytb');vid='abcdefghijk'
        payload={'contents':[
            {'videoRenderer':{'videoId':vid,'title':{'simpleText':'normal'}}},
            {'lockupViewModel':{'contentId':vid,'contentType':'VIDEO','metadata':{'lockupMetadataViewModel':{'title':{'content':'new card'}}}}},
            {'reelItemRenderer':{'videoId':vid,'headline':{'simpleText':'short'},'lengthText':{'simpleText':'0:42'}}}]}
        for card in payload['contents']:
            result=adapter._parse_results(card,site)
            self.assertEqual(len(result),1)
            self.assertEqual(result[0].cover,'https://i.ytimg.com/vi/'+vid+'/hqdefault.jpg')
            if 'reelItemRenderer' in card:
                self.assertEqual(result[0].duration,'0:42')
        self.assertEqual(youtube_cover('bad/../id'),'')
        self.assertEqual(youtube_cover(vid,'//example.com/thumb.jpg'),'https://example.com/thumb.jpg')

    def test_aliases_protocol_and_malformed_address(self):
        for field in ('cover', 'pic', 'thumbnail', 'thumbnail_url'):
            row = _to_result({'title': 'a', 'url': 'https://example.com/v', field: '//i0.hdslb.com/a.jpg'})
            self.assertEqual(row.cover, 'https://i0.hdslb.com/a.jpg')
        self.assertEqual(cover_url('https://[bad'), '')
        self.assertEqual(cover_url('file:///tmp/a'), '')
        self.assertEqual(video_identity('https://bilibili.com.evil/video/BV123'), ('', ''))

    def test_missing_bilibili_cover_is_recovered_and_cached(self):
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtGui import QImage
        buf = QByteArray()
        device = QBuffer(buf)
        device.open(QIODevice.OpenModeFlag.WriteOnly)
        image = QImage(48, 32, QImage.Format.Format_RGB32)
        image.fill(0xff123456)
        image.save(device, 'PNG')
        data = bytes(buf) + b'\0'*1000
        http = Mock()
        http.get_json.return_value = {'code': 0, 'data': {'pic': '//i0.hdslb.com/a.jpg'}}
        http.get_bytes.return_value = data
        with tempfile.TemporaryDirectory() as root:
            cache = ThumbCache(Settings(), http, root=root)
            try:
                for _ in range(2):
                    done, results = threading.Event(), []
                    def callback(url, path):
                        results.append(path)
                        done.set()
                    cache.request_video('https://www.bilibili.com/video/BV123', callback)
                    self.assertTrue(done.wait(3))
                    self.assertTrue(results[0])
                    self.assertFalse(QImage(results[0]).isNull())
                self.assertEqual(http.get_json.call_count, 1)
                self.assertEqual(http.get_bytes.call_count, 1)
                self.assertNotIn('avif', http.get_bytes.call_args.kwargs['headers']['Accept'])
            finally:
                cache._pool.shutdown(wait=True)


class MediaUITests(unittest.TestCase):
    setUpClass = classmethod(access_test.AccessUITests.setUpClass.__func__)
    tearDownClass = classmethod(access_test.AccessUITests.tearDownClass.__func__)
    setUp = access_test.AccessUITests.setUp
    tearDown = access_test.AccessUITests.tearDown

    def test_append_does_not_remove_existing_layout_items(self):
        self.win.settings.show_thumbnails = False
        self.win._append_results([SearchResult(title='a', url='https://example.com/a')])
        first = self.win._cards[0]
        with patch.object(self.win.flow, 'takeAt', wraps=self.win.flow.takeAt) as take:
            self.win._append_results([SearchResult(title='b', url='https://example.com/b')])
            take.assert_not_called()
        self.assertIs(self.win._cards[0], first)
        self.assertEqual(self.win.flow.count(), 2)

    def test_missing_cover_is_requested_off_ui_and_old_callback_is_ignored(self):
        self.win.settings.show_thumbnails = True
        self.win._append_results([SearchResult(title='a', url='https://www.bilibili.com/video/BV123')])
        with patch.object(self.win.thumbs, 'request_video') as resolve:
            self.win._pump_thumbs()
            resolve.assert_called_once()
            callback = resolve.call_args.kwargs['done']
        self.win._clear_results()
        callback('video:old', '/no/such/file')
        self.app.processEvents()
        self.assertFalse(self.win._cards)

    def test_failed_metadata_is_cooled_down_and_does_not_fetch_arbitrary_hosts(self):
        http = Mock()
        http.get_json.side_effect = RuntimeError('blocked')
        with tempfile.TemporaryDirectory() as root:
            cache = ThumbCache(Settings(), http, root=root)
            try:
                for url in ['https://www.bilibili.com/video/BV123']*2 + ['http://127.0.0.1/video/BV123']:
                    done, results = threading.Event(), []
                    cache.request_video(url, lambda u, p: (results.append(p), done.set()))
                    self.assertTrue(done.wait(3))
                    self.assertEqual(results, [None])
                self.assertEqual(http.get_json.call_count, 1)
            finally:
                cache._pool.shutdown(wait=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
