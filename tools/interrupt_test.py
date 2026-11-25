"""Cancellation tests use real localhost requests and owned child processes."""
import json
import testenv
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from access_test import AccessTests, AccessUITests, connect
from app.agent_client import CONTROL_SCRIPT, HELPER_SCRIPT


class InterruptTests(unittest.TestCase):
    setUp = AccessTests.setUp
    tearDown = AccessTests.tearDown
    get = AccessTests.get
    post = AccessTests.post
    ready = AccessTests.ready
    task = AccessTests.task

    def test_cancel_wakes_long_poll_without_consuming_new_task(self):
        old = self.task('只搜 B站 猫')
        old_event = self.server.cancellation(old['id'])
        replies, started = [], threading.Event()
        def watch():
            started.set()
            replies.append(self.get('control', task_id=old['id'], wait=20).json())
        worker = threading.Thread(target=watch)
        worker.start(); self.assertTrue(started.wait(1))
        time.sleep(.1)
        before = time.monotonic()
        new = self.server.push_task('只搜 B站 狗')
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - before, 1)
        self.assertTrue(old_event.is_set())
        self.assertTrue(replies[0]['cancelled'])
        self.assertFalse(self.server.cancellation(new['id']).is_set())
        self.assertEqual(self.get().json()['task']['id'], new['id'])
        for path in ('results', 'error', 'progress'):
            self.assertEqual(self.post(path, task_id=old['id'], done=True, results=[], error='old').status_code, 409)
        self.assertFalse(self.errors or self.rows)

    def test_control_requires_identity_and_cannot_take_over(self):
        task = self.task('只搜 B站 猫')
        self.assertEqual(self.get('control', task_id=task['id'], agent='stranger').status_code, 409)
        self.assertEqual(self.server.owner, self.who)
        self.assertEqual(self.get('control').status_code, 400)
        self.assertEqual(self.get('control', task_id=task['id'], wait='nan').status_code, 400)
        reply = self.http.get(self.server.url + '/api/control', timeout=2)
        self.assertEqual(reply.status_code, 403)

    def test_pause_resume_never_revives_cancelled_worker(self):
        task = self.task('只搜 B站 猫')
        event = self.server.cancellation(task['id'])
        self.server.pause()
        self.assertTrue(event.is_set())
        self.assertTrue(self.get('control', task_id=task['id']).json()['paused'])
        self.server.resume()
        new = self.get().json()['task']
        self.assertNotEqual(task['id'], new['id'])
        self.assertTrue(event.is_set())
        self.assertFalse(self.server.cancellation(new['id']).is_set())

    def test_stream_stops_on_cancel_and_releases_slot(self):
        task = self.task('只搜 B站 猫')
        event = self.server.cancellation(task['id'])
        reads = []
        class Response:
            status_code, headers, encoding = 200, {}, 'utf-8'
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def raise_for_status(self): pass
            def iter_content(self, size):
                reads.append(1); yield b'first'
                event.set()
                reads.append(2); yield b'cancelled'
                reads.append(3); yield b'never'
        with patch('app.agent_access.public_url'), patch('app.agent_access.HttpClient') as client:
            client.return_value.proxy_url.return_value = ''
            client.return_value.get.return_value = Response()
            with self.assertRaisesRegex(ValueError, '取消'):
                self.server.access.fetch('https://example.com/', cancel=event)
            self.assertIs(client.return_value.cancel_event, event)
            client.return_value.close.assert_called_once()
        self.assertEqual(reads, [1, 2])
        self.assertTrue(self.server.access.io_slots.acquire(False))
        self.server.access.io_slots.release()

    def test_search_adapter_receives_shared_cancellation(self):
        task = self.task('只搜 B站 猫')
        event = self.server.cancellation(task['id'])
        def search(ctx, site):
            self.assertIs(ctx.cancel, event)
            self.assertIs(ctx.http.cancel_event, event)
            self.server.push_task('只搜 B站 狗')
            self.assertTrue(ctx.cancelled())
            return []
        with patch('app.adapters.adapter_for') as adapter:
            adapter.return_value.search.side_effect = search
            with self.assertRaisesRegex(ValueError, '取消'):
                self.server.access.search({'task_id': task['id'], 'query': 'cat',
                    'source_id': self.cfg.enabled_sites()[0].id}, cancel=event)

    def test_runner_terminates_owned_search_and_new_task_is_available(self):
        self._run_owned_cancel(False)

    def test_disconnect_runner_terminates_owned_child_and_exits_session(self):
        self._run_owned_cancel(True)

    def _run_owned_cancel(self, disconnect):
        task = self.task('只搜 B站 猫')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            helper, ready, late = root/'runner.py', root/'ready', root/'late'
            helper.write_text(CONTROL_SCRIPT, encoding='utf-8')
            child = root/'worker.py'
            child.write_text('import time\nfrom pathlib import Path\n'
                             f'Path({str(ready)!r}).write_text("ready")\ntime.sleep(20)\n'
                             f'Path({str(late)!r}).write_text("stale")\n', encoding='utf-8')
            proc = subprocess.Popen([sys.executable, str(helper), '--base', self.server.url,
                '--token', self.server.token, '--agent', self.who, '--task-id', task['id'],
                '--run', sys.executable, str(child)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.025)
                self.assertTrue(ready.exists())
                before = time.monotonic()
                if disconnect:
                    self.server.disconnect()
                else:
                    new = self.server.push_task('只搜 B站 狗')
                stdout, stderr = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 0 if disconnect else 75, stderr)
                self.assertLess(time.monotonic() - before, 4)
                self.assertTrue(json.loads(stdout)['cancelled'])
                self.assertFalse(late.exists())
                if disconnect:
                    self.assertTrue(json.loads(stdout)['session_ended'])
                    self.assertEqual(json.loads(stdout)['next'], 'stop')
                    self.assertFalse(json.loads(stdout)['retryable'])
                else:
                    self.assertEqual(self.get().json()['task']['id'], new['id'])
            finally:
                if proc.poll() is None:
                    self.server.clear_tasks()
                    proc.communicate(timeout=6)

    def test_control_helper_is_served_and_compiles(self):
        reply = self.get('control/helper').json()
        self.assertEqual(reply['script'], CONTROL_SCRIPT)
        compile(reply['script'], reply['filename'], 'exec')

    def test_network_helper_preserves_stop_instead_of_returning_to_poll(self):
        self._network_helper_exit(True)

    def test_network_helper_stale_task_still_returns_to_poll(self):
        self._network_helper_exit(False)

    def _network_helper_exit(self, disconnect):
        import io
        from contextlib import redirect_stdout
        task = self.task('只搜 B站 猫')
        token = self.server.token
        if disconnect:
            self.server.disconnect()
        else:
            self.server.push_task('只搜 B站 狗')
        ns = {'__name__': 'helper_test'}
        exec(compile(HELPER_SCRIPT, 'helper.py', 'exec'), ns)
        output = io.StringIO()
        with patch('sys.argv', ['helper.py', '--base', self.server.url, '--token', token,
                              '--agent', self.who, '--task-id', task['id']]), redirect_stdout(output):
            code = ns['main']()
        self.assertEqual(code, 0 if disconnect else 75)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload['session_ended'], disconnect)
        self.assertEqual(payload['next'], 'stop' if disconnect else 'poll')
        self.assertFalse(self.errors)


class InterruptUITests(unittest.TestCase):
    setUpClass = classmethod(AccessUITests.setUpClass.__func__)
    tearDownClass = classmethod(AccessUITests.tearDownClass.__func__)
    setUp = AccessUITests.setUp
    tearDown = AccessUITests.tearDown

    def test_submit_during_search_switches_immediately_and_ignores_old_messages(self):
        connect(self.server)
        self.win._tick_agent_bar()
        self.win.search_edit.setText('只搜 B站 猫')
        self.win._start_search()
        old = self.server.pop_task(0, 'access-test')
        self.assertEqual(old['want'], 100)
        self.server.acknowledge(old['id'])
        self.app.processEvents()
        self.assertTrue(self.win.search_edit.isEnabled())
        event = self.server.cancellation(old['id'])
        self.win.search_edit.setText('只搜 B站 狗')
        self.win._start_search()
        new = self.server.pop_task(0, 'access-test')
        self.assertEqual(new['query'], '只搜 B站 狗')
        self.assertTrue(event.is_set())
        self.win._on_agent_error({'task_id': old['id'], 'error': 'old failure'})
        self.assertFalse(self.errors)
        self.assertEqual(self.win._agent_task_id, new['id'])
        self.assertFalse(self.win._valid_agent_message({'task_id': old['id']}))


class FastHelperTests(unittest.TestCase):
    def test_network_helper_cancels_without_waiting_for_blocked_probe(self):
        ns = {'__name__': 'test_helper'}
        exec(compile(HELPER_SCRIPT, 'helper.py', 'exec'), ns)
        blocked, release, finished = threading.Event(), threading.Event(), threading.Event()
        calls = []
        class Reply:
            status = 200
            def __init__(self, data): self.data = json.dumps(data).encode()
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self, *a): return self.data
        class Opener:
            def open(self, req, **kwargs):
                url = req.full_url
                calls.append(url)
                if '/config?' in url:
                    return Reply({'network': {'required': True, 'targets': ['https://example.com/']}})
                if '/control?' in url:
                    blocked.wait(1)
                    return Reply({'cancelled': True, 'stale': True})
                if url == 'https://example.com/':
                    blocked.set(); release.wait(3); finished.set()
                return Reply({'ok': True})
        with patch('urllib.request.build_opener', return_value=Opener()), patch('sys.argv',
            ['helper.py', '--base', 'http://127.0.0.1:8765', '--token', 'test', '--agent', 'test', '--task-id', 't1']):
            try:
                before = time.monotonic()
                self.assertEqual(ns['main'](), 75)
                self.assertLess(time.monotonic() - before, 1.5)
                self.assertFalse(any('/report?' in u or '/error?' in u for u in calls))
            finally:
                release.set(); finished.wait(1)

    def test_targets_run_in_parallel_and_proxy_success_skips_direct(self):
        ns = {'__name__': 'test_helper'}
        exec(compile(HELPER_SCRIPT, 'helper.py', 'exec'), ns)
        barrier = threading.Barrier(3)
        reports, external = [], []
        targets = ['https://a.example/', 'https://b.example/', 'https://c.example/']
        class Reply:
            status = 200
            def __init__(self, data): self.data = json.dumps(data).encode()
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self, *a): return self.data
        class Opener:
            def open(self, req, **kwargs):
                url = req.full_url
                if url in targets:
                    external.append(url)
                    barrier.wait(timeout=2)  # Serial implementation fails deterministically.
                    return Reply({})
                if '/config?' in url:
                    return Reply({'network': {'required': True, 'check_id': 'c', 'targets': targets},
                                  'proxy_url': 'http://127.0.0.1:9999'})
                if '/report?' in url:
                    reports.append(json.loads(req.data)); return Reply({'usable': True})
                return Reply({'ok': True})
        with patch('urllib.request.build_opener', return_value=Opener()), patch('sys.argv',
            ['helper.py', '--base', 'http://127.0.0.1:8765', '--token', 'test', '--agent', 'test', '--task-id', 't1']):
            self.assertEqual(ns['main'](), 0)
        self.assertCountEqual(external, targets)
        self.assertEqual([row['route'] for row in reports[0]['attempts']], ['proxy'] * 3)


if __name__ == '__main__':
    os.environ['RESOURCE_FINDER_HOME'] = tempfile.mkdtemp(prefix='rf-interrupt-test-')
    unittest.main(verbosity=2)
