"""Deterministic workflow regressions; no external network, browser or user config."""
from __future__ import annotations
import os
import testenv
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_sandbox = tempfile.TemporaryDirectory(prefix="rf-regression-")
os.environ["RESOURCE_FINDER_HOME"] = _sandbox.name
from app.agent import AgentServer, _to_result
from app.config import Config, Settings, SiteConfig, cache_dir
from app.models import SearchResult, SiteEvent, normalize_url, parse_count, parse_pub_ts, short_date
from app.net import HttpClient, NetworkError, _clean_proxy
import requests


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        self.server = AgentServer(lambda *args: self.received.append(args), port=0)
        self.assertTrue(self.server.start())
        # Port zero is used to avoid interference with a running user instance.
        self.server._port = self.server._httpd.server_port
        self.server.touch("worker-a")
        self.task = self.server.push_task("demo")
        self.session = requests.Session()
        self.session.trust_env = False

    def tearDown(self):
        self.server.stop()
        self.session.close()

    def post(self, path="results", **data):
        return self.session.post(self.server.url + "/api/" + path,
                                 params={"token": self.server.token}, json=data, timeout=3)

    def test_stream_and_empty_final(self):
        for done, rows in ((False, [{"title": "a", "url": "https://example.com/watch/1"}]), (True, [])):
            response = self.post(task_id=self.task["id"], done=done, results=rows)
            self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.received), 2)
        self.assertTrue(self.received[-1][2]["done"])

    def test_old_task_and_query_mismatch_rejected(self):
        self.assertEqual(self.post(task_id=self.task["id"], query="wrong").status_code, 409)
        self.server.push_task("new")
        for path in ("results", "progress", "error"):
            self.assertEqual(self.post(path, task_id=self.task["id"]).status_code, 409)
        self.assertFalse(self.received)

    def test_missing_task_and_invalid_body_rejected(self):
        self.assertEqual(self.post(results=[]).status_code, 409)
        self.assertEqual(self.post(task_id=self.task["id"], results=5).status_code, 400)
        self.assertEqual(self.post(task_id=self.task["id"], done="false").status_code, 400)

    def test_help_does_not_leak_token(self):
        result = self.session.get(self.server.url + "/api/help", timeout=3)
        self.assertEqual(result.status_code, 403)
        self.assertNotIn(self.server.token, result.text)
        self.assertNotIn("Access-Control-Allow-Origin", result.headers)

    def test_long_poll_pause_preserves_queue(self):
        self.server.clear_tasks()
        found = []
        thread = threading.Thread(target=lambda: found.append(self.server.pop_task(2, "worker-a")))
        thread.start()
        time.sleep(.15)
        self.server.pause()
        task = self.server.push_task("paused")
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(found, [None])
        self.server.resume()
        self.assertEqual(self.server.pop_task(0, "worker-a")["id"], task["id"])

    def test_takeover_cannot_steal_new_task(self):
        self.server.clear_tasks()
        found = []
        thread = threading.Thread(target=lambda: found.append(self.server.pop_task(2, "worker-a")))
        thread.start()
        time.sleep(.15)
        self.server.touch("worker-b")
        task = self.server.push_task("new")
        thread.join(1)
        self.assertEqual(found, [None])
        self.assertEqual(self.server.pop_task(0, "worker-b")["id"], task["id"])
        self.assertFalse(self.server.touch("worker-a"))

    def test_unlimited_exclusion_history(self):
        urls = ["https://example.com/%d" % i for i in range(1501)]
        self.assertEqual(self.server.push_task("x", kind="more", exclude=urls)["exclude"], urls)

    def fetch(self):
        return self.session.get(self.server.url + "/api/task", timeout=3,
                                params={"token": self.server.token, "agent": "worker-a", "wait": 0})

    def test_unconfirmed_task_is_redelivered_with_same_id(self):
        first = self.fetch().json()["task"]
        self.assertIsNone(self.server.pop_task(0, "worker-a"))
        self.server._delivery_at -= 16
        second = self.fetch().json()["task"]
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["delivery_attempt"], 2)
        self.assertEqual(self.server.task_status()["state"], "delivered")

    def test_failed_http_write_requeues_immediately(self):
        handler = self.server._httpd.RequestHandlerClass
        original = handler._send
        def failed_write(request, code, payload):
            if payload.get("task"):
                return False  # Socket closed before client received the JSON.
            return original(request, code, payload)
        with patch.object(handler, "_send", failed_write):
            with self.assertRaises(requests.exceptions.RequestException):
                self.fetch()
        self.assertEqual(self.server.task_status()["state"], "queued")
        self.assertEqual(self.fetch().json()["task"]["id"], self.task["id"])

    def test_ack_prevents_resend_and_duplicate_ack_does_not_revive_done(self):
        task = self.fetch().json()["task"]
        self.assertTrue(self.post("ack", task_id=task["id"], agent="worker-a").json()["acknowledged"])
        self.server._delivery_at -= 16
        self.assertIsNone(self.server.pop_task(0, "worker-a"))
        self.assertEqual(self.server.task_status()["state"], "working")
        self.post(task_id=task["id"], done=True, results=[])
        self.post("ack", task_id=task["id"])
        self.assertEqual(self.server.task_status()["state"], "completed")
        self.assertIsNone(self.server.pop_task(0, "worker-a"))

    def test_old_attempt_failure_cannot_requeue_new_delivery(self):
        first = self.server.pop_task(0, "worker-a")
        self.server._delivery_at -= 16
        second = self.server.pop_task(0, "worker-a")
        self.server.delivery_failed(first["id"], first["delivery_attempt"])
        self.assertEqual(self.server.task_status()["state"], "delivered")
        self.server.acknowledge(second["id"])
        self.server.delivery_failed(second["id"], second["delivery_attempt"])
        self.assertEqual(self.server.task_status()["state"], "working")

    def test_legacy_progress_confirms_receipt(self):
        self.post("progress", task_id=self.task["id"], message="searching")
        self.assertEqual(self.server.task_status()["state"], "working")
        self.assertIsNone(self.server.pop_task(0, "worker-a"))

    def test_status_is_authenticated_and_never_consumes_task(self):
        url = self.server.url + "/api/status"
        self.assertEqual(self.session.get(url, timeout=3).status_code, 403)
        status = self.session.get(url, params={"token": self.server.token}, timeout=3).json()
        self.assertEqual(status["task"]["state"], "queued")
        self.assertEqual(self.fetch().json()["task"]["id"], self.task["id"])

    def test_takeover_preserves_pending_query_without_gui(self):
        self.server.touch("worker-b")
        replacement = self.server.pop_task(0, "worker-b")
        self.assertEqual(replacement["query"], "demo")
        self.assertNotEqual(replacement["id"], self.task["id"])
        self.assertFalse(self.server.accepts(self.task["id"]))

    def test_wrong_post_name_cannot_take_over_and_lose_task(self):
        response = self.post("ack", task_id=self.task["id"], agent="another-name")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.server.owner, "worker-a")
        self.assertEqual(self.server.current_task["id"], self.task["id"])

    def test_reconnect_poll_receives_previously_queued_work(self):
        self.server._poll_times["worker-a"] -= 200
        self.assertFalse(self.server.connected())
        self.assertEqual(self.fetch().json()["task"]["id"], self.task["id"])

    def test_pause_blocks_ack_and_manual_retry(self):
        self.server.pause()
        self.assertIsNone(self.server.retry_task())
        self.assertFalse(self.server.acknowledge(self.task["id"]))


class DataTests(unittest.TestCase):
    def test_empty_sites_remain_empty(self):
        self.assertEqual(Config.from_dict({"sites": []}).sites, [])
        self.assertEqual(len(Config.from_dict({}).sites), len(Config().sites))

    def test_bad_setting_does_not_discard_user_sites(self):
        data = Config().to_dict()
        data["sites"][0]["name"] = "我的站点"
        data["settings"]["workers"] = "bad"
        data["settings"]["timeout"] = float("nan")
        data["version"] = "bad"
        cfg = Config.from_dict(data)
        self.assertEqual(cfg.sites[0].name, "我的站点")
        self.assertEqual(cfg.settings.workers, 6)

    def test_cover_waiters_and_retry_after_cooldown(self):
        from app.thumbcache import ThumbCache
        from unittest.mock import Mock
        import tempfile
        http = Mock()
        entered, release = threading.Event(), threading.Event()
        def download(*args, **kwargs):
            entered.set()
            release.wait(3)
            raise NetworkError("temporary")
        http.get_bytes.side_effect = download
        with tempfile.TemporaryDirectory() as root:
            cache = ThumbCache(Settings(), http, root=root)
            calls = []
            url = "https://example.com/image.png"
            cache.request(url, done=lambda *a: calls.append(1))
            self.assertTrue(entered.wait(2))
            cache.request(url, done=lambda *a: calls.append(2))
            release.set()
            deadline = time.monotonic()+3
            while len(calls) < 2 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(calls, [1, 2])
            cache.request(url, done=lambda *a: calls.append(3))
            self.assertEqual(calls, [1, 2, 3])
            self.assertEqual(http.get_bytes.call_count, 1)
            cache._broken[url] -= 61
            cache.request(url, done=lambda *a: calls.append(4))
            deadline = time.monotonic()+3
            while len(calls) < 4 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(http.get_bytes.call_count, 2)
            cache.shutdown()

    def test_test_cache_is_isolated(self):
        self.assertTrue(Path(cache_dir()).is_relative_to(_sandbox.name))

    def test_video_aliases_and_query_order(self):
        self.assertEqual(normalize_url("https://youtu.be/Abc123?si=zzz"),
                         normalize_url("https://www.youtube.com/shorts/Abc123"))
        self.assertEqual(normalize_url("https://example.com/v?b=2&a=1"),
                         normalize_url("https://example.com/v?a=1&b=2"))
        self.assertNotEqual(normalize_url("https://bilibili.com/video/BV1?p=1"),
                            normalize_url("https://bilibili.com/video/BV1?p=2"))

    def test_dates_counts_and_unsafe_links(self):
        now = datetime(2026, 11, 23, 0, 1).timestamp()
        self.assertEqual(short_date(now - 120, now), "昨天")
        self.assertEqual(parse_pub_ts("1700000000000"), 1700000000)
        self.assertEqual(parse_pub_ts("2026-02-31"), 0)
        self.assertEqual(parse_count(float("nan")), 0)
        self.assertEqual(parse_count(-3), 0)
        for url in ("javascript:alert(1)", "file:///C:/test", "https://[bad"):
            self.assertIsNone(_to_result({"title": "unsafe", "url": url}))

    def test_first_page_template(self):
        from app.adapters.base import SearchContext
        from app.adapters.generic import GenericAdapter
        from unittest.mock import Mock
        http = Mock()
        http.get_text.return_value = ""
        site = SiteConfig(name="test", search_url="https://example.com?q={q}&page={page}")
        GenericAdapter().search(SearchContext(query="a b", http=http), site)
        self.assertEqual(http.get_text.call_args.args[0], "https://example.com?q=a+b&page=1")

    def test_site_route_and_worker_cancellation(self):
        client = HttpClient(Settings())
        from unittest.mock import Mock
        session = Mock()
        session.get.return_value.status_code = 200
        with patch.object(client, "routes_for", return_value=["direct"]) as routes, patch.object(client, "_session", return_value=session):
            client.get("https://example.com", SiteConfig(route="direct"))
            self.assertEqual(routes.call_args.args[0], "direct")
            client.cancel_event = threading.Event()
            client.cancel_event.set()
            seen = []
            thread = threading.Thread(target=lambda: seen.append(client.cancel_event))
            thread.start(); thread.join()
            self.assertEqual(seen, [None])
            with self.assertRaises(NetworkError):
                client.get("https://example.com")
        self.assertEqual(_clean_proxy("https://user:pass@[::1]:8888/path"), "https://user:pass@[::1]:8888")


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.original_clipboard_text = cls.app.clipboard().text()

    @classmethod
    def tearDownClass(cls):
        from PySide6.QtCore import QCoreApplication, QEvent
        cls.app.clipboard().setText(cls.original_clipboard_text)
        cls.app.closeAllWindows()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        cls.app.processEvents()

    def setUp(self):
        from app.ui.mainwindow import MainWindow
        # Keep the established queue/UI component suite on the legacy protocol.
        # AccessTests and AccessUITests below cover the real 3.0 handshake and network gate.
        self.legacy_server = patch('app.ui.mainwindow.AgentServer',
            lambda *a, **kw: AgentServer(*a, **{k: v for k, v in kw.items() if k != 'config'}))
        self.legacy_server.start()
        self.startup = patch.object(MainWindow, "_startup_checks", lambda self: None)
        self.startup.start()
        self.win = MainWindow(Config())
        self.win.settings.show_thumbnails = False
        self.win._warn = lambda *args: None
        self.win._set_mode("agent")
        self.win._agent_server.touch("regression")
        self.win._handoff_to_agent("first")

    def tearDown(self):
        from PySide6.QtCore import QCoreApplication, QEvent
        self.win.close()
        self.win.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.win = None
        self.startup.stop()
        self.legacy_server.stop()

    def push(self, rows=(), done=False, task_id=None, query=""):
        self.win._on_agent_results({"results": list(rows), "query": query,
            "meta": {"task_id": task_id or self.win._agent_task_id, "done": done}})

    def row(self, n=1):
        return SearchResult(title="Video %d" % n, url="https://example.com/watch/%d" % n)

    def test_empty_final_uses_whole_round_and_duplicate_final_ignored(self):
        self.win.settings.agent_search_mode = 'unlimited'
        self.win._agent_grant_end = 1000
        self.push([self.row(), self.row()])
        self.assertEqual(self.win._agent_round_added, 1)
        self.push(done=True)
        self.assertFalse(self.win._agent_busy)
        self.assertTrue(self.win._auto_more_timer.isActive())
        self.win._auto_more_timer.stop()
        self.push(done=True)
        self.assertFalse(self.win._auto_more_timer.isActive())
        self.win._auto_agent_more()
        self.push([self.row(2)])
        self.push(done=True)
        self.assertFalse(self.win._no_more)
        self.win._auto_agent_more()
        self.push(done=True)
        self.assertFalse(self.win._no_more)  # One empty task is not exhaustion evidence.
        self.assertTrue(self.win._auto_more_timer.isActive())

    def test_old_results_errors_and_queued_callbacks_cannot_mix(self):
        old = self.win._agent_task_id
        self.win._handoff_to_agent("second")
        self.push([self.row()], task_id=old)
        self.win._on_agent_error({"task_id": old, "error": "old"})
        self.assertFalse(self.win._all_results)
        self.assertTrue(self.win._agent_busy)
        self.push([self.row()], query="first")
        self.assertFalse(self.win._all_results)

    def test_pause_stops_timer_and_resume_issues_new_task(self):
        old = self.win._agent_task_id
        self.win._toggle_agent_pause()
        self.assertFalse(self.win._agent_busy_timer.isActive())
        self.win._auto_agent_more()
        self.win._ask_agent_more()
        self.assertIsNone(self.win._agent_server.current_task)
        self.push([self.row()], task_id=old)
        self.assertFalse(self.win._all_results)
        self.win._toggle_agent_pause()
        self.assertFalse(self.win._agent_busy)
        self.win._agent_server.pop_task(0, 'regression')
        self.win._tick_agent_bar()
        self.assertNotEqual(self.win._agent_task_id, old)
        self.assertTrue(self.win._agent_busy)

    def test_progress_refreshes_inactivity_timer(self):
        self.win._agent_busy_timer.start(50)
        self.win._agent_busy_timer.setInterval(150000)
        self.win._on_agent_status({"task_id": self.win._agent_task_id, "message": "working"})
        self.assertGreater(self.win._agent_busy_timer.remainingTime(), 140000)

    def test_new_query_button_refresh_and_filter_protocol(self):
        from PySide6.QtTest import QTest
        from PySide6.QtCore import Qt
        self.win.search_edit.setText("second")
        self.assertTrue(self.win.search_btn.isEnabled())
        QTest.mouseClick(self.win.search_btn, Qt.MouseButton.LeftButton)
        self.assertEqual(self.win._query, "second")
        old = self.win._agent_task_id
        self.win.time_combo.setCurrentIndex(2)
        self.win._refresh_search()
        self.assertNotEqual(self.win._agent_task_id, old)
        self.assertEqual(self.win._agent_server.current_task["filters"]["time"], "7d")

    def test_clear_and_mode_switch_invalidate_tasks(self):
        old = self.win._agent_task_id
        self.win._clear_results()
        self.push([self.row()], task_id=old)
        self.assertFalse(self.win._all_results)
        self.assertFalse(self.win._agent_busy)
        self.assertFalse(self.win._query)
        self.win._handoff_to_agent("new")
        old = self.win._agent_task_id
        self.win._set_mode("direct")
        self.push([self.row()], task_id=old)
        self.assertFalse(self.win._all_results)

    def test_cards_reused_and_legacy_copy_setting_cannot_hijack_click(self):
        from PySide6.QtTest import QTest
        from PySide6.QtCore import Qt
        self.push([self.row()])
        card = self.win._cards[0]
        self.push([self.row(2)])
        self.assertIs(self.win._cards[0], card)
        opened, copied = [], []
        self.win._open_link = opened.append
        self.win._copy_link = copied.append
        self.win.settings.open_in_browser = True
        QTest.mouseClick(card, Qt.MouseButton.LeftButton)
        self.win.settings.open_in_browser = False
        QTest.mouseClick(card, Qt.MouseButton.LeftButton)
        self.assertEqual(opened, [card.result.url, card.result.url])
        self.assertEqual(copied, [])

    def test_cover_click_opens_browser_and_preserves_clipboard(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtGui import QDesktopServices
        self.push([self.row()])
        self.win.settings.open_in_browser = False
        self.win.show()
        self.app.processEvents()
        card = self.win._cards[0]
        self.app.clipboard().setText("preserve my clipboard")
        with patch.object(QDesktopServices, "openUrl", return_value=True) as browser:
            QTest.mouseClick(card.cover, Qt.MouseButton.LeftButton)
            self.assertEqual(browser.call_count, 1)
            self.assertEqual(browser.call_args.args[0].toString(), card.result.url)
        self.assertEqual(self.app.clipboard().text(), "preserve my clipboard")
        with patch.object(QDesktopServices, "openUrl", return_value=False):
            self.win._open_link(card.result.url)
        self.assertEqual(self.app.clipboard().text(), "preserve my clipboard")

    def test_composer_enter_shift_enter_and_ime(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtGui import QInputMethodEvent
        field = self.win.search_edit
        field.returnPressed.disconnect()
        sent = []
        field.returnPressed.connect(lambda: sent.append(field.text()))
        field.setText("line1")
        field.moveCursor(field.textCursor().MoveOperation.End)
        QTest.keyClick(field, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        QTest.keyClicks(field, "line2")
        self.assertEqual(field.text(), "line1\nline2")
        self.assertEqual(sent, [])
        self.app.sendEvent(field, QInputMethodEvent("测试", []))
        QTest.keyClick(field, Qt.Key.Key_Return)
        self.assertEqual(sent, [])
        commit = QInputMethodEvent()
        commit.setCommitString("测试")
        self.app.sendEvent(field, commit)
        QTest.keyClick(field, Qt.Key.Key_Return)
        self.assertEqual(sent, ["line1\nline2测试"])

    def test_bottom_composer_remains_visible_in_both_modes(self):
        from PySide6.QtCore import QPoint
        self.win.resize(1000, 660)
        self.win.show()
        for mode in ("direct", "agent"):
            self.win._set_mode(mode)
            self.app.processEvents()
            field = self.win.search_edit
            top = field.mapTo(self.win, QPoint()).y()
            stack_bottom = self.win.stack.mapTo(self.win, self.win.stack.rect().bottomLeft()).y()
            bottom = self.win.composer.mapTo(self.win, self.win.composer.rect().bottomLeft()).y()
            self.assertGreater(top, stack_bottom)
            self.assertLess(bottom, self.win.height())
            self.assertTrue(field.isVisible())

    def test_explicit_copy_never_opens_browser(self):
        from types import SimpleNamespace
        # Test clipboard policy deterministically. uitest.py separately checks
        # real Windows clipboard writes and asynchronous HTTP redirect resolution.
        contents = ['']
        clipboard = SimpleNamespace(text=lambda: contents[0],
            setText=lambda text: contents.__setitem__(0, text))
        opened = []
        self.win._open_link = opened.append
        self.win.settings.open_in_browser = True
        with patch('app.ui.mainwindow.QGuiApplication.clipboard', return_value=clipboard):
            self.win._copy_link("https://example.com/video")
            self.assertEqual(clipboard.text(), "https://example.com/video")
            self.assertFalse(opened)
            clipboard.setText("user copied something else")
            self.win._on_resolved("https://example.com/video", "https://example.com/final")
            self.assertEqual(clipboard.text(), "user copied something else")

    def test_clear_during_paging_rejects_late_page(self):
        self.win._set_mode("direct")
        self.win._paging = True
        old = self.win._gen
        cancel = self.win._cancel
        self.win._clear_results()
        self.win._on_event(old, SiteEvent(kind="done", results=[self.row()]))
        self.assertTrue(cancel.is_set())
        self.assertFalse(self.win._all_results)

    def test_page_failure_is_retryable(self):
        self.win._set_mode("direct")
        self.win._paging = True
        self.win._page = 2
        self.win._paging_new = 0
        self.win._paging_failed = False
        self.win._on_paging_event(SiteEvent(kind="error", error="timeout"))
        self.win._on_paging_event(SiteEvent(kind="finish"))
        self.assertFalse(self.win._no_more)
        self.assertEqual(self.win._page, 1)

    def test_connected_does_not_mean_task_received(self):
        self.assertTrue(self.win._agent_server.connected())
        self.assertIn("Agent 尚未领取", self.win.side_busy_title.text())
        self.assertFalse(self.win.side_busy_spinner._timer.isActive())
        self.assertFalse(self.win._agent_busy_timer.isActive())
        self.win._agent_busy_timeout()
        self.assertEqual(self.win._agent_server.current_task["id"], self.win._agent_task_id)

    def test_http_ack_switches_ui_from_waiting_to_working(self):
        server = self.win._agent_server
        with requests.Session() as session:
            session.trust_env = False
            task = session.get(server.url + "/api/task", timeout=3,
                params={"token": server.token, "agent": "regression", "wait": 0}).json()["task"]
            self.app.processEvents()
            self.assertIn("待 Agent 确认", self.win.side_busy_title.text())
            self.assertFalse(self.win.side_busy_spinner._timer.isActive())
            response = session.post(server.url + "/api/ack", timeout=3,
                params={"token": server.token}, json={"task_id": task["id"], "agent": "regression"})
            self.assertTrue(response.json()["acknowledged"])
            self.app.processEvents()
            self.assertIn("正在搜索", self.win.side_busy_title.text())
            self.assertTrue(self.win.side_busy_spinner._timer.isActive())
            self.assertTrue(self.win._agent_busy_timer.isActive())

    def test_takeover_callback_does_not_replace_delivered_task(self):
        server = self.win._agent_server
        server.touch("replacement")
        task = server.pop_task(0, "replacement")
        server.acknowledge(task["id"])
        self.app.processEvents()
        self.assertEqual(server.current_task["id"], task["id"])
        self.assertEqual(self.win._agent_task_id, task["id"])
        self.assertEqual(server.task_status()["state"], "working")

    def test_delayed_takeover_callback_preserves_newer_query(self):
        server = self.win._agent_server
        server.touch("replacement")
        self.win._handoff_to_agent("newer query")
        task_id = self.win._agent_task_id
        self.app.processEvents()
        self.assertEqual(self.win._query, "newer query")
        self.assertEqual(server.current_task["id"], task_id)
        self.assertEqual(server.pop_task(0, "replacement")["id"], task_id)

    def test_unconfirmed_task_redelivers_without_retry_button(self):
        from PySide6.QtWidgets import QPushButton
        server = self.win._agent_server
        first = server.pop_task(0, "regression")
        self.assertFalse(any(b.text() == '重新派发' for b in self.win.findChildren(QPushButton)))
        server._delivery_at -= 16
        self.assertEqual(server.pop_task(0, "regression")["id"], first["id"])
        self.assertFalse(self.win.side_busy_spinner._timer.isActive())


if __name__ == "__main__":
    from update_test import UpdateTests, UpdateUITests
    from shutdown_test import ShutdownTests, ShutdownUITests
    from liveness_test import LivenessTests, LivenessUITests
    from background_browser_test import BackgroundBrowserTests, BackgroundTransportTests
    from release_safety_test import ReleaseSafetyTests
    from native_browser_test import NativeBrowserTests
    from pairing_test import PairingTests, AutoConnectionTests, PairingUITests
    from duration_test import DurationTests, DurationUITests
    from download_test import DownloadTests, DownloadMediaTests, DownloadUITests
    from sound_test import SoundTests, SoundUITests
    from aigei_download_test import SoundDownloadTests, SoundDownloadUITests
    from aigei_browser_test import AigeiBrowserTests
    from aigei_mcp_test import MCPTests
    from resource_test import ResourceTests, ResourceUITests, ResourceProtocolTests
    from profile_test import ProfileTests, ProfileUITests
    from site_intake_test import SiteIntakeTests, SiteIntakeUITests
    from browser_bridge_test import BrowserBridgeTests
    from login_test import LoginTests, LoginServerTests, LoginUITests
    from fill_test import FillUITests, FillServerTests
    from completion_test import CompletionTests, CompletionServerTests
    from session_test import SessionTests
    from worker_test import WorkerTests
    from contract_test import ContractTests, ContractUITests
    from qa_test import WorkflowQATests, ServerShutdownQATests, SettingsQATests
    from thumbnail_test import ThumbnailTests
    from continuation_test import ContinuationTests, PaginationUITests
    from paging_test import SelectedPagingTests, ScopedAgentTests
    from source_test import SourceTests, SourceUITests
    from media_test import CandidateTests, CoverTests, MediaUITests
    from access_test import AccessTests, AccessUITests, HelperTests
    from interrupt_test import InterruptTests, InterruptUITests, FastHelperTests
    from routing_test import RoutingTests, DisconnectUITests
    from budget_test import BudgetServerTests, BudgetUITests
    # Metadata recovery is independently mocked in duration_test; never make
    # incidental real provider requests from deterministic workflow fixtures.
    with patch('app.durations.resolve_duration', return_value=''):
        program = unittest.main(verbosity=2, exit=False)
    # Collect Python/Qt callback cycles while QApplication is still alive.
    # Do not leave these cycles for interpreter-shutdown GC, where the QA run
    # observed a native access violation after all assertions had passed.
    import gc
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    application = QApplication.instance()
    if application:
        application.closeAllWindows()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()
    gc.collect()
    print('GUI cleanup completed before interpreter shutdown', flush=True)
    sys.exit(0 if program.result.wasSuccessful() else 1)
