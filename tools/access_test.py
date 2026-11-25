"""3.0 access regressions: real localhost HTTP, deterministic remote transport."""
import json
import testenv
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from app.agent import AgentServer
from app.agent_access import AgentAccess, external_targets, public_url
from app.config import Config, Settings


def connect(server, who='access-test'):
    control = server.pop_task(0, who)
    if control and control.get('kind') == 'connect':
        server.access.confirm({'challenge': control['challenge'], 'ok': True})
        server.pop_task(0, who)
    return server.access.ready()


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.events, self.errors, self.rows = [], [], []
        self.cfg = Config()
        self.cfg.settings.proxy_mode = 'direct'
        self.server = AgentServer(lambda *a: self.rows.append(a), on_error=self.errors.append,
                                  on_status=self.events.append, port=0, config=self.cfg)
        self.server.start()
        self.http = requests.Session()
        self.http.trust_env = False
        self.who = 'access-test'

    def tearDown(self):
        self.server.stop()
        self.http.close()

    def get(self, path='task', **data):
        return self.http.get(self.server.url + '/api/' + path,
            params={'token': self.server.token, 'agent': self.who, 'wait': 0, **data}, timeout=5)

    def post(self, path, **data):
        return self.http.post(self.server.url + '/api/' + path, params={'token': self.server.token},
                              json={'agent': self.who, **data}, timeout=5)

    def ready(self):
        handshake = self.get().json()['task']
        self.assertEqual(handshake['kind'], 'connect')
        self.assertEqual(self.post('connect', challenge=handshake['challenge'], ok=True).status_code, 200)
        self.assertFalse(self.server.access.ready())
        self.get()
        self.assertTrue(self.server.access.ready())

    def task(self, query='YouTube 风景'):
        self.ready()
        self.server.push_task(query)
        task = self.get().json()['task']
        self.post('ack', task_id=task['id'])
        return task

    def report(self, task, rows):
        return self.post('network/report', task_id=task['id'], check_id=task['network']['check_id'], attempts=rows)

    def successes(self, task, route='native'):
        return [{'url': u, 'route': route, 'ok': True, 'status': 200} for u in task['network']['targets']]

    def test_handshake_requires_read_confirm_and_next_poll(self):
        self.get('status')
        self.assertFalse(self.server.access.ready())
        self.ready()

    def test_paused_poll_waits_without_delivering_or_ending_connection(self):
        self.ready()
        self.server.pause()
        task = self.server.push_task('只搜 B站 猫')
        before = time.monotonic()
        reply = self.get(wait=.35).json()
        self.assertGreaterEqual(time.monotonic() - before, .3)
        self.assertTrue(reply['paused'])
        self.assertEqual(reply['next'], 'poll')
        self.assertIsNone(reply['task'])
        self.assertEqual(self.server.task_status()['queued'], 1)
        self.server.resume()
        self.assertFalse(self.server.access.ready())
        self.assertEqual(self.get().json()['task']['id'], task['id'])
        self.assertTrue(self.server.access.ready())

    def test_resume_wakes_paused_poll_and_disconnect_stops_it(self):
        self.ready()
        self.server.pause()
        replies = []
        thread = threading.Thread(target=lambda: replies.append(self.get(wait=2).json()))
        thread.start()
        time.sleep(.15)
        self.server.resume()
        task = self.server.push_task('只搜 B站 猫')
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(replies[0]['task']['id'], task['id'])
        self.assertTrue(self.server.access.ready())
        self.server.pause()
        thread = threading.Thread(target=lambda: replies.append(self.get(wait=2).json()))
        thread.start()
        time.sleep(.15)
        self.server.disconnect()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(replies[-1]['stop'])

    def test_cancelled_control_watcher_cannot_keep_agent_alive(self):
        task = self.task('只搜 B站 猫')
        self.server.pause()
        old = time.time() - 46
        self.server._get_times[self.who] = old
        self.server._poll_times[self.who] = old
        self.assertTrue(self.get('control', task_id=task['id']).json()['cancelled'])
        self.assertEqual(self.server._poll_times[self.who], old)
        self.assertFalse(self.server.connected())
        self.assertFalse(self.server.access.ready())
        self.assertFalse(self.server.connected_agent()['ready'])
        # A genuine task poll, even while paused, proves the consumer returned.
        self.get()
        self.assertTrue(self.server.access.ready())

    def test_reconnect_handshake_can_complete_while_paused(self):
        self.server.pause()
        self.ready()
        self.assertTrue(self.server.paused)
        self.assertTrue(self.server.access.ready())

    def test_resume_requires_poll_not_stale_progress_or_status(self):
        task = self.task('只搜 B站 猫')
        self.server.pause()
        self.server.clear_tasks()
        self.server.resume()
        self.get('status')
        self.post('progress', task_id=task['id'], message='late')
        self.assertFalse(self.server.access.ready())
        self.get()
        self.assertTrue(self.server.access.ready())

    def test_wrong_challenge_and_old_owner_cannot_unlock(self):
        old = self.get().json()['task']
        self.assertEqual(self.post('connect', challenge='wrong', ok=True).status_code, 409)
        self.who = 'replacement'
        newer = self.get().json()['task']
        self.assertNotEqual(old['id'], newer['id'])
        self.who = 'access-test'
        self.assertEqual(self.post('connect', challenge=old['challenge'], ok=True).status_code, 409)
        self.assertFalse(self.server.access.ready())

    def test_domestic_only_skips_external_check(self):
        task = self.task('只搜国内 B站风景')
        self.assertFalse(task['network']['required'])
        self.assertEqual(self.post('results', task_id=task['id'], done=True, results=[]).status_code, 200)

    def test_external_results_blocked_until_test_passes(self):
        task = self.task()
        self.assertTrue(task['network']['required'])
        self.assertEqual(self.post('results', task_id=task['id'], done=True, results=[]).status_code, 428)
        self.assertTrue(self.report(task, self.successes(task)).json()['usable'])
        self.assertEqual(self.post('results', task_id=task['id'], done=True, results=[]).status_code, 200)

    def test_partial_report_not_enough_and_failure_report_retained(self):
        task = self.task()
        reply = self.report(task, [{'url': u, 'route': 'direct', 'ok': False, 'error': 'blocked by sandbox'}
                                  for u in task['network']['targets']]).json()
        self.assertFalse(reply['usable'])
        self.assertEqual(self.server.task_status()['state'], 'failed')
        self.assertIn('blocked by sandbox', self.errors[0]['error'])
        self.assertFalse(self.server.access.cache)

    def test_cache_is_per_target_owner_and_proxy_configuration(self):
        task = self.task()
        self.report(task, self.successes(task))
        self.server.push_task('YouTube another')
        self.assertFalse(self.get().json()['task']['network']['required'])
        self.cfg.settings.proxy_mode = 'manual'
        self.cfg.settings.proxy_url = 'http://127.0.0.1:9999'
        self.server.push_task('YouTube again')
        self.assertTrue(self.get().json()['task']['network']['required'])
        self.who = 'new-owner'
        self.ready()
        self.assertFalse(self.server.access.cache)

    def test_relay_success_needs_server_receipt(self):
        task = self.task()
        self.assertFalse(self.report(task, self.successes(task, 'relay')).json()['usable'])

    def test_direct_proxy_failure_then_relay_success(self):
        task = self.task()
        rows = []
        for url in task['network']['targets']:
            rows.extend([{'url': url, 'route': r, 'ok': False, 'error': 'unreachable'} for r in ('direct', 'proxy')])
            with patch.object(self.server.access, 'fetch', return_value={'status': 200, 'body': 'page'}):
                receipt = self.post('network/probe', task_id=task['id'], url=url).json()['receipt']
            rows.append({'url': url, 'route': 'relay', 'ok': True, 'status': 200, 'receipt': receipt})
        reply = self.report(task, rows).json()
        self.assertTrue(reply['usable'])
        self.assertTrue(all(r == 'relay' for r in reply['network']['routes'].values()))

    def test_paused_and_stale_report_cannot_change_task(self):
        task = self.task()
        self.server.pause()
        self.assertEqual(self.report(task, self.successes(task)).status_code, 409)
        self.server.resume()
        self.server.push_task('new')
        self.assertEqual(self.report(task, self.successes(task)).status_code, 409)

    def test_pause_during_relay_does_not_block_and_discards_reply(self):
        task = self.task()
        started, release = threading.Event(), threading.Event()
        def slow(*a, **kw):
            started.set(); release.wait(3)
            return {'status': 200, 'body': ''}
        replies = []
        with patch.object(self.server.access, 'fetch', side_effect=slow):
            thread = threading.Thread(target=lambda: replies.append(self.post('network/probe',
                task_id=task['id'], url=task['network']['targets'][0])))
            thread.start(); self.assertTrue(started.wait(2))
            before = time.monotonic(); self.server.pause()
            self.assertLess(time.monotonic() - before, .3)
            release.set(); thread.join(3)
        self.assertEqual(replies[0].status_code, 409)
        self.assertFalse(self.server.access.receipts)

    def test_no_token_or_wrong_owner_cannot_get_proxy_configuration(self):
        task = self.task()
        self.assertEqual(self.http.get(self.server.url + '/api/network/config').status_code, 403)
        self.who = 'intruder'
        self.assertEqual(self.get('network/config', task_id=task['id']).status_code, 409)
        self.assertEqual(self.server.owner, 'access-test')

    def test_private_urls_and_redirect_targets_rejected(self):
        for url in ('file:///etc/passwd', 'http://localhost/', 'http://user:pw@example.com', 'http://example.com:8765'):
            with self.assertRaises(ValueError):
                public_url(url)
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(ValueError):
                public_url('https://example.com/')
        response = requests.Response()
        response.status_code = 302
        response.headers['Location'] = 'http://localhost/private'
        response._content_consumed = True
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.215.14', 443))]), \
             patch('app.net.HttpClient.get', return_value=response):
            with self.assertRaises(ValueError):
                self.server.access.fetch('https://example.com/')

    def test_new_target_requires_a_new_check_and_old_check_rejected(self):
        task = self.task()
        self.report(task, self.successes(task))
        reply = self.post('network/require', task_id=task['id'], targets=['https://vimeo.com/']).json()
        self.assertTrue(reply['network']['required'])
        self.assertEqual(self.report(task, self.successes(task)).status_code, 400)

    def test_helper_is_delivered_as_reviewable_source(self):
        response = self.get('network/helper').json()
        compile(response['script'], response['filename'], 'exec')
        self.assertIn('ProxyHandler({})', response['script'])

    def test_poisoned_dns_uses_known_public_proxy_only_without_direct_fallback(self):
        from app.net import HttpClient
        response = requests.Response()
        response.status_code = 200
        response._content = b'test'
        response._content_consumed = True
        with patch.object(HttpClient, 'proxy_url', return_value='http://127.0.0.1:7897'), \
             patch.object(HttpClient, 'get', return_value=response) as get, \
             patch('socket.getaddrinfo', side_effect=OSError('poisoned local DNS')):
            self.assertEqual(self.server.access.fetch('https://www.youtube.com/', probe=True)['status'], 200)
            self.assertEqual(get.call_args.kwargs['route'], 'proxy_only')
            with self.assertRaises(OSError):
                public_url('https://youtube.com.evil.example/', remote_dns=True)
            with self.assertRaises(ValueError):
                public_url('http://localhost/', remote_dns=True)
        with patch('app.net.detect_proxy', return_value=''), patch('app.net.proxy_alive', return_value=False):
            self.assertEqual(HttpClient(self.cfg.settings).routes_for('proxy_only'), [])

    def test_expired_cache_and_failed_retry_must_retest(self):
        task = self.task()
        self.report(task, self.successes(task))
        for key, value in list(self.server.access.cache.items()):
            self.server.access.cache[key] = (value[0], time.monotonic() - 901)
        self.server.push_task('YouTube again')
        new = self.get().json()['task']
        self.assertTrue(new['network']['required'])
        self.report(new, [])
        self.server.retry_task()
        self.assertTrue(self.get().json()['task']['network']['required'])

    def test_connection_failure_is_not_ready_and_preserves_reason(self):
        control = self.get().json()['task']
        self.post('connect', challenge=control['challenge'], ok=False, error='cannot keep polling')
        self.assertFalse(self.server.access.ready())
        self.assertEqual(self.server.access.last_report, 'cannot keep polling')


class AccessUITests(unittest.TestCase):
    def test_paused_silence_preserves_session_and_resume_still_requires_poll(self):
        from app.models import SearchResult
        connect(self.server, 'ui-agent')
        self.win._handoff_to_agent('只搜 B站 猫')
        self.win._on_agent_results({'results': [SearchResult(title='cat', url='https://www.bilibili.com/video/BV1xx411c7mD')],
            'meta': {'task_id': self.win._agent_task_id, 'done': False}})
        self.win._toggle_agent_pause()
        count = len(self.win._all_results)
        self.assertGreater(count, 0)
        self.server._get_times['ui-agent'] = time.time() - 46
        self.server._poll_times['ui-agent'] = time.time() - 46
        self.win._tick_agent_bar()
        self.assertIn('暂未收到', self.win.agent_state.text())
        self.assertEqual(self.win.agent_dot.property('on'), 'false')
        self.assertTrue(self.win.search_edit.isEnabled())
        self.assertFalse(self.win._agent_busy_timer.isActive())
        self.assertEqual(len(self.win._all_results), count)
        self.win._toggle_agent_pause()
        self.assertFalse(self.server.paused)
        self.assertIsNone(self.win._connect_dialog)
        self.assertIsNone(self.server.current_task)
        self.assertFalse(self.win.search_edit.isEnabled())
        self.server.pop_task(0, 'ui-agent')
        self.win._tick_agent_bar()
        self.assertTrue(self.win._agent_busy)
        task = self.server.current_task
        self.win._tick_agent_bar()
        self.assertEqual(self.server.current_task['id'], task['id'])
        self.assertEqual(len(self.win._all_results), count)

    def test_resume_without_agent_response_expires_instead_of_searching(self):
        connect(self.server, 'ui-agent')
        self.win._handoff_to_agent('只搜 B站 猫')
        self.win._toggle_agent_pause()
        self.win._toggle_agent_pause()
        self.assertIn('等待 Agent 恢复轮询', self.win.agent_state.text())
        self.assertFalse(self.win._agent_busy)
        self.assertIsNone(self.server.current_task)
        self.server._get_times['ui-agent'] = time.time() - 46
        self.server._poll_times['ui-agent'] = time.time() - 46
        self.win._tick_agent_bar()
        self.assertIn('等待 Agent 响应', self.win.agent_state.text())
        self.assertFalse(self.win.search_btn.isEnabled())
        self.assertIsNone(self.server.current_task)

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.clipboard_text = cls.app.clipboard().text()

    @classmethod
    def tearDownClass(cls):
        cls.app.clipboard().setText(cls.clipboard_text)

    def setUp(self):
        from app.ui.mainwindow import MainWindow
        self.startup = patch.object(MainWindow, '_startup_checks', lambda self: None)
        self.startup.start()
        self.win = MainWindow(Config())
        self.win._warn = lambda *a: self.errors.append(a)
        self.errors = []
        self.win.show()
        self.win._set_mode('agent')
        self.server = self.win._agent_server

    def tearDown(self):
        if self.win._connect_dialog:
            self.win._connect_dialog.reject()
        from PySide6.QtCore import QCoreApplication, QEvent
        self.win.close()
        self.win.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.win = None
        self.startup.stop()

    def test_copy_opens_waiting_and_input_unlocks_only_after_full_handshake(self):
        self.assertFalse(self.win.search_edit.isEnabled())
        self.win._copy_agent_help()
        dialog = self.win._connect_dialog
        self.assertTrue(dialog.isVisible())
        control = self.server.pop_task(0, 'ui-agent')
        self.server.access.confirm({'challenge': control['challenge'], 'ok': True})
        dialog.refresh()
        self.assertFalse(self.win.search_edit.isEnabled())
        self.assertTrue(dialog.isVisible())
        self.server.pop_task(0, 'ui-agent')
        dialog.refresh(); self.app.processEvents()
        self.assertIsNone(self.win._connect_dialog)
        self.assertTrue(self.win.search_edit.isEnabled())
        self.assertEqual(self.win.empty.title.text(), 'Agent 已接管')

    def test_connect_timeout_remains_locked_and_cancel_returns_direct(self):
        self.win._copy_agent_help()
        dialog = self.win._connect_dialog
        dialog.started -= 121
        dialog.refresh()
        self.assertIn('尚未完成', dialog.title.text())
        self.assertFalse(self.win.search_edit.isEnabled())
        dialog.reject(); self.app.processEvents()
        self.assertFalse(self.win.agent_mode)
        self.assertTrue(self.win.search_edit.isEnabled())

    def test_network_failure_displays_report_and_stops_busy(self):
        connect(self.server)
        self.win._tick_agent_bar()
        self.win._handoff_to_agent('YouTube 风景')
        task = self.server.pop_task(0, 'access-test')
        self.server.acknowledge(task['id'])
        self.app.processEvents()
        self.assertIn('检测', self.win.side_busy_title.text())
        with requests.Session() as local:
            local.trust_env = False
            reply = local.post(self.server.url + '/api/network/report', params={'token': self.server.token},
                json={'agent': 'access-test', 'task_id': task['id'], 'check_id': task['network']['check_id'],
                      'attempts': [{'url': u, 'route': 'native', 'ok': False, 'error': 'policy denied'}
                                   for u in task['network']['targets']]}, timeout=3)
        self.assertEqual(reply.status_code, 200)
        self.app.processEvents()
        self.assertFalse(self.win._agent_busy)
        self.assertIn('policy denied', self.errors[0][1])

    def test_slow_network_check_retains_task_until_actual_report(self):
        connect(self.server)
        self.win._handoff_to_agent('YouTube 风景')
        task = self.server.pop_task(0, 'access-test')
        self.server.acknowledge(task['id'])
        self.server.access.plan['started'] = time.monotonic() - 301
        self.win._sync_agent_delivery()
        self.assertTrue(self.win._agent_busy)
        self.assertFalse(self.server.cancellation(task['id']).is_set())
        self.assertEqual(self.server.current_task['id'],task['id'])
        self.assertFalse(self.errors)

    def test_quiet_session_can_queue_work_without_claiming_agent_is_searching(self):
        connect(self.server)
        self.win._tick_agent_bar()
        self.assertTrue(self.win.search_edit.isEnabled())
        self.server._poll_times[self.server.owner] = time.time() - 100
        self.win._tick_agent_bar()
        self.assertTrue(self.win.search_edit.isEnabled())
        self.assertIn('等待 Agent 响应', self.win.agent_state.text())
        self.win._agent_search('只搜国内猫')
        task = self.server.current_task
        self.win._tick_agent_bar()
        self.assertEqual(self.server.current_task['id'], task['id'])
        self.assertFalse(self.win.side_busy_spinner._timer.isActive())
        self.assertEqual(self.server.pop_task(0, 'access-test')['id'], task['id'])
        self.win._set_mode('direct')
        self.assertTrue(self.win.search_edit.isEnabled())

    def test_full_batch_counts_recent_results_and_quiet_scroll_queues_next_batch(self):
        connect(self.server)
        self.win._handoff_to_agent('只搜国内猫')
        task = self.server.pop_task(0, 'access-test')
        # Reproduce: one old GET, fresh results, then limit changes working -> completed.
        self.server._get_times['access-test'] = time.time() - 600
        rows = [{'title': 'cat %d' % i, 'url': 'https://example.com/video/%d' % i} for i in range(100)]
        with requests.Session() as http:
            http.trust_env = False
            reply = http.post(self.server.endpoint(), json={'agent': 'access-test', 'task_id': task['id'],
                              'results': rows, 'done': False}, timeout=3)
        self.assertTrue(reply.json()['limit_reached'])
        self.app.processEvents()
        self.win._tick_agent_bar()
        self.assertTrue(self.server.connected())
        self.assertEqual(self.server.task_status()['state'], 'completed')
        self.assertEqual(len(self.win._all_results), 100)
        self.assertEqual(self.win._agent_budget_used, 100)
        self.assertNotIn('失联', self.win.agent_state.text())
        self.assertIsNone(self.server.pop_task(0, 'access-test'))
        self.server._poll_times['access-test'] -= 46
        self.win._tick_agent_bar()
        self.assertFalse(self.server.connected())
        self.assertTrue(self.server.session_ready())
        self.assertEqual(self.server.current_task['id'], task['id'])
        self.assertEqual(self.server.task_status()['received'], 100)
        self.assertIn('本批已完成', self.win.agent_state.text())
        self.win._load_agent_batch()
        following = self.server.current_task
        self.assertNotEqual(following['id'], task['id'])
        self.assertEqual((following['want'], len(following['exclude'])), (100, 100))
        self.win._tick_agent_bar()
        self.assertEqual(self.server.current_task['id'], following['id'])
        self.assertFalse(self.win.side_busy_spinner._timer.isActive())
        self.assertEqual(self.server.pop_task(0, 'access-test')['id'], following['id'])

    def test_silence_does_not_revoke_work_and_late_valid_results_resume_ui(self):
        connect(self.server)
        self.win._handoff_to_agent('只搜国内猫')
        task = self.server.pop_task(0, 'access-test')
        self.server.acknowledge(task['id'])
        self.server._poll_times['access-test'] -= 91
        self.win._tick_agent_bar()
        self.assertEqual(self.server.current_task['id'], task['id'])
        self.assertFalse(self.server.cancellation(task['id']).is_set())
        with requests.Session() as http:
            http.trust_env = False
            response = http.post(self.server.endpoint(), json={'agent': 'access-test', 'task_id': task['id'],
                'results': [{'title': 'cat', 'url': 'https://example.com/video/cat'}], 'done': False}, timeout=3)
        self.assertEqual(response.status_code, 200)
        self.app.processEvents()
        self.win._tick_agent_bar()
        self.assertTrue(self.server.connected())
        self.assertEqual(len(self.win._all_results), 1)
        self.assertTrue(self.win.side_busy_spinner._timer.isActive())


class HelperTests(unittest.TestCase):
    def test_helper_automatically_tries_all_routes_and_reports(self):
        from app.agent_client import HELPER_SCRIPT
        ns = {'__name__': 'helper_test'}
        exec(compile(HELPER_SCRIPT, 'helper.py', 'exec'), ns)
        calls, report = [], []
        class Reply:
            def __init__(self, data): self.data = json.dumps(data).encode()
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self, *a): return self.data
        class Opener:
            def open(self, req, **kw):
                url = req.full_url
                calls.append(url)
                if not url.startswith('http://127.0.0.1'):
                    raise OSError('Agent network blocked')
                if '/config?' in url:
                    return Reply({'network': {'required': True, 'check_id': 'c', 'targets': ['https://www.youtube.com/']},
                                  'proxy_url': 'http://127.0.0.1:9999'})
                if '/probe?' in url:
                    return Reply({'ok': True, 'status': 200, 'receipt': 'verified'})
                if '/report?' in url:
                    report.append(json.loads(req.data)); return Reply({'ok': True, 'usable': True})
                return Reply({'ok': True})
        with patch('urllib.request.build_opener', return_value=Opener()), \
             patch('sys.argv', ['helper.py', '--base', 'http://127.0.0.1:8765', '--token', 'test',
                                '--agent', 'tester', '--task-id', 't1']):
            self.assertEqual(ns['main'](), 0)
        self.assertEqual([a['route'] for a in report[0]['attempts']], ['proxy', 'direct', 'relay'])
        self.assertEqual(report[0]['attempts'][-1]['receipt'], 'verified')


if __name__ == '__main__':
    import os, tempfile
    os.environ['RESOURCE_FINDER_HOME'] = tempfile.mkdtemp(prefix='rf-access-test-')
    unittest.main(verbosity=2)
