"""3.2 routing, session revocation, and timing regressions."""
import json
import testenv
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import access_test as access
from app.agent_access import external_targets, search_scope
from app.config import Config
from app.net import HttpClient


class RoutingTests(unittest.TestCase):
    setUp = access.AccessTests.setUp
    tearDown = access.AccessTests.tearDown
    get = access.AccessTests.get
    post = access.AccessTests.post
    ready = access.AccessTests.ready
    task = access.AccessTests.task

    def test_domestic_request_skips_foreign_probe_and_filters_sources(self):
        task = self.task('搜索国内的视频')
        self.assertFalse(task['network']['required'])
        self.assertEqual(task['network']['targets'], [])
        self.assertEqual(task['network']['scope'], 'domestic')
        sources = self.get('network/config', task_id=task['id']).json()['sources']
        youtube = next(s for s in self.cfg.enabled_sites() if s.builtin == 'youtube')
        self.assertNotIn(youtube.id, [s['id'] for s in sources])
        reply = self.post('network/search', task_id=task['id'], source_id=youtube.id, query='cat')
        self.assertEqual(reply.status_code, 502)
        self.assertIn('范围', reply.json()['error'])

    def test_scope_variants_and_explicit_mixed_targets(self):
        sites = Config().enabled_sites()
        for query in ('国内的视频', '帮我搜B站视频', '中国大陆网站视频', '只搜国内，不要YouTube'):
            with self.subTest(query=query):
                self.assertEqual(search_scope(query), 'domestic')
                self.assertEqual(external_targets(query, sites), [])
        for query in ('国内外视频', '国内和YouTube视频', '国内视频 https://vimeo.com/'):
            with self.subTest(query=query):
                self.assertEqual(search_scope(query), 'all')
                self.assertTrue(external_targets(query, sites))
        self.assertEqual(search_scope('国外的视频'), 'overseas')
        self.assertTrue(external_targets('搜索国外的视频', sites))
        self.assertEqual(search_scope('风景'), 'all')

    def response(self, code=200, location=''):
        import requests
        r = requests.Response()
        r.status_code, r._content, r._content_consumed = code, b'public page', True
        if location: r.headers['Location'] = location
        return r

    def test_domestic_fetch_uses_direct_first_even_when_proxy_available(self):
        with patch.object(HttpClient, 'proxy_url', return_value='http://127.0.0.1:7897'), \
             patch.object(HttpClient, 'get', return_value=self.response()) as get, \
             patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.215.14', 443))]), \
             patch('app.net.detect_proxy', return_value='http://127.0.0.1:7897'), \
             patch('app.net.proxy_alive', return_value=True):
            for host in ('www.bilibili.com', 'www.baidu.com'):
                self.server.access.fetch('https://' + host + '/', probe=True)
                route = get.call_args.kwargs['route']
                self.assertEqual(route, 'auto')
                self.assertEqual(HttpClient(self.cfg.settings).routes_for(route, host)[0], 'direct')

    def test_redirect_recomputes_domestic_route(self):
        with patch.object(HttpClient, 'proxy_url', return_value='http://127.0.0.1:7897'), \
             patch.object(HttpClient, 'get', side_effect=[self.response(302, 'https://www.bilibili.com/'), self.response()]) as get, \
             patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.215.14', 443))]):
            self.server.access.fetch('https://www.youtube.com/', probe=True)
            self.assertEqual([c.kwargs['route'] for c in get.call_args_list], ['proxy_only', 'auto'])

    def test_explicit_site_route_is_respected(self):
        site = next(s for s in self.cfg.enabled_sites() if s.builtin == 'bilibili')
        with patch.object(HttpClient, 'proxy_url', return_value='http://127.0.0.1:7897'), \
             patch.object(HttpClient, 'get', return_value=self.response()) as get, \
             patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.215.14', 443))]):
            for configured, expected in (('direct', 'direct'), ('proxy', 'proxy_only')):
                site.route = configured
                self.server.access.fetch('https://www.bilibili.com/', probe=True)
                self.assertEqual(get.call_args.kwargs['route'], expected)

    def test_disconnect_revokes_all_old_endpoints_without_closing_server(self):
        task = self.task('国内视频')
        token, port, event = self.server.token, self.server.port, self.server.cancellation(task['id'])
        self.server.disconnect()
        self.assertTrue(event.is_set())
        self.assertTrue(self.server.running)
        self.assertEqual(self.server.port, port)
        self.assertFalse(self.server.owner or self.server.connected() or self.server.paused)
        for path in ('task', 'control', 'status', 'help', 'network/helper', 'control/helper', 'network/config'):
            reply = self.get(path, token=token, task_id=task['id'])
            self.assertEqual(reply.status_code, 200 if path == 'task' else 409, path)
            self.assertTrue(reply.json()['stop'], path)
            self.assertTrue(reply.json()['session_ended'], path)
            self.assertFalse(reply.json()['retryable'], path)
            self.assertEqual(reply.json()['next'], 'stop', path)
            if path == 'task':
                reply.raise_for_status()
                self.assertEqual(reply.json()['task']['kind'], 'stop')
        for path in ('ack', 'results', 'progress', 'error', 'connect', 'network/report'):
            reply = self.http.post(self.server.url+'/api/'+path, params={'token': token},
                json={'agent': self.who, 'task_id': task['id'], 'results': [], 'error': 'old'}, timeout=3)
            self.assertEqual(reply.status_code, 409, path)
            self.assertTrue(reply.json()['stop'], path)
        self.assertFalse(self.errors or self.rows or self.server.owner)
        self.ready()  # Same name can reconnect using new instructions.
        self.assertTrue(self.server.access.ready())
        self.assertTrue(self.get('task', token=token).json()['stop'])

    def test_inflight_long_poll_cannot_claim_work_after_same_name_reconnect(self):
        self.ready()
        token = self.server.token
        replies = []
        worker = threading.Thread(target=lambda: replies.append(self.get('task', token=token, wait=20)))
        worker.start()
        deadline = time.monotonic()+2
        while not self.server.task_status()['waiting_polls'] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(self.server.task_status()['waiting_polls'])
        self.server.disconnect()
        self.ready()
        new = self.server.push_task('国内新视频')
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(replies[0].json()['stop'])
        self.assertEqual(replies[0].status_code, 200)
        self.assertEqual(replies[0].json()['reason'], 'user_disconnected')
        self.assertEqual(self.get().json()['task']['id'], new['id'])

    def test_disconnect_wakes_control_with_stop_not_just_stale(self):
        task = self.task('国内视频')
        token, replies, entered = self.server.token, [], threading.Event()
        def watch():
            entered.set()
            replies.append(self.server.control(self.who, task['id'], 20, token))
        worker = threading.Thread(target=watch); worker.start(); entered.wait(1)
        self.server.disconnect(); worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(replies[0]['stop'])

    def test_disconnect_during_probe_rejects_old_response_and_metrics(self):
        task = self.task()
        entered, release, replies = threading.Event(), threading.Event(), []
        def fetch(*a, **kw):
            entered.set(); release.wait(3)
            return {'ok': True, 'status': 200, 'body': ''}
        with patch.object(self.server.access, 'fetch', side_effect=fetch):
            worker = threading.Thread(target=lambda: replies.append(self.post('network/probe', task_id=task['id'], url=task['network']['targets'][0])))
            worker.start(); self.assertTrue(entered.wait(1))
            self.server.disconnect(); release.set(); worker.join(2)
        self.assertTrue(replies[0].json()['stop'])
        self.assertEqual(self.server.timings()['network_requests'], 0)
        self.assertFalse(self.server.access.receipts)

    def test_timings_record_network_and_first_result_and_reset_on_new_task(self):
        task = self.task('国内视频')
        with patch.object(self.server.access, 'fetch', return_value={'ok': True, 'status': 200, 'body': 'x'}):
            response = self.post('network/fetch', task_id=task['id'], url='https://www.bilibili.com/').json()
        self.assertIn('elapsed_ms', response)
        self.post('results', task_id=task['id'], results=[{'title': 'test', 'url': 'https://www.bilibili.com/video/BV123'}])
        m = self.server.task_status()['timings']
        self.assertIsNotNone(m['queue_ms'])
        self.assertIsNotNone(m['first_result_ms'])
        self.assertEqual(m['network_requests'], 1)
        self.server.push_task('国内新任务')
        self.assertEqual(self.server.timings()['network_requests'], 0)
        self.assertIsNone(self.server.timings()['first_result_ms'])


class DisconnectUITests(unittest.TestCase):
    setUpClass = classmethod(access.AccessUITests.setUpClass.__func__)
    tearDownClass = classmethod(access.AccessUITests.tearDownClass.__func__)
    setUp = access.AccessUITests.setUp
    tearDown = access.AccessUITests.tearDown

    def test_disconnect_button_preserves_results_and_requires_new_handshake(self):
        from app.models import SearchResult
        access.connect(self.server)
        self.win._tick_agent_bar()
        self.win._handoff_to_agent('国内视频')
        task = self.server.pop_task(0, 'access-test')
        self.server.acknowledge(task['id'])
        self.win._on_agent_results({'results': [SearchResult(title='保留结果', url='https://www.bilibili.com/video/BV123')],
            'query': '国内视频', 'meta': {'task_id': task['id'], 'done': False}})
        count = len(self.win._all_results)
        self.assertGreater(count, 0)
        event = self.server.cancellation(task['id'])
        self.win.agent_disconnect_btn.click()
        self.app.processEvents()
        self.assertTrue(event.is_set())
        self.assertFalse(self.win._agent_busy or self.win._auto_more_timer.isActive())
        self.assertEqual(len(self.win._all_results), count)
        self.assertFalse(self.win.search_edit.isEnabled())
        self.assertFalse(self.win.agent_disconnect_btn.isEnabled())
        self.assertTrue(self.server.running)
        self.assertIn('已断开', self.win.agent_state.text())
        access.connect(self.server); self.win._tick_agent_bar()
        self.assertTrue(self.win.search_edit.isEnabled())
        self.assertTrue(self.win.agent_disconnect_btn.isEnabled())


if __name__ == '__main__':
    os.environ['RESOURCE_FINDER_HOME'] = tempfile.mkdtemp(prefix='rf-routing-test-')
    unittest.main(verbosity=2)
