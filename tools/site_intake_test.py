"""Whole-site intake, privacy controls, scope boundaries and async edit regressions."""
import testenv
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.site_intake import infer_site, root_domain, search_template, same_site, discover_site
from app.config import SiteConfig, Settings
from app.models import SearchResult
from app.sources import result_groups, site_group
from app.adapters.base import SearchContext
from app.adapters.pagination import PageResults
import access_test


class SiteIntakeTests(unittest.TestCase):
    def test_bili_page_becomes_canonical_site(self):
        site, _ = infer_site('https://www.bilibili.com/video/BV12g4E6aEat/?trackid=abc&spm_id_from=333&vd_source=secret')
        self.assertEqual(site.search_url, 'https://search.bilibili.com/all?keyword={q}')
        self.assertEqual((site.kind, site.builtin, site.scope_domain), ('builtin', 'bilibili', 'bilibili.com'))
        self.assertNotIn('secret', str(site.to_dict()))

    def test_known_platform_aliases_and_share_text(self):
        for url, kind in [('分享 https://b23.tv/abcdef。视频', 'bilibili'),
                          ('https://youtu.be/abcdefghijk?t=1', 'youtube'),
                          ('https://m.youtube.com/shorts/abcdefghijk', 'youtube'),
                          ('www.bilibili.com/video/abc', 'bilibili')]:
            with self.subTest(url=url):
                self.assertEqual(infer_site(url)[0].builtin, kind)

    def test_public_suffix_and_tenant_boundaries(self):
        for url, expected in [('https://video.example.co.uk/watch/1', 'example.co.uk'),
                              ('https://a.example.com.cn/a', 'example.com.cn'),
                              ('https://alice.github.io/page', 'alice.github.io'),
                              ('https://www.食狮.com.cn/a', 'xn--85x722f.com.cn')]:
            self.assertEqual(root_domain(url), expected)

    def test_lookalikes_are_not_builtin(self):
        for url in ['https://bilibili.com.example.com/video', 'https://notyoutube.com/watch']:
            site, _ = infer_site(url)
            self.assertEqual(site.kind, 'domain')
            self.assertFalse(site.builtin)

    def test_invalid_input_is_actionable(self):
        for value in ['', 'hello', 'file:///C:/abc', 'https://localhost/a', 'http://127.0.0.1/',
                      'https://user:password@example.com/', 'https://example.com:8765/']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                infer_site(value)

    def test_manual_template_is_preserved(self):
        url = 'https://video.example.com/search?term={q}&page={page}'
        site, _ = infer_site(url)
        self.assertEqual(site.search_url, url)
        self.assertEqual(site.kind, 'generic')
        self.assertFalse(site.scope_domain)

    def test_schema_search_and_get_form(self):
        schema = '<script type="application/ld+json">{"@type":"WebSite","potentialAction":{"@type":"SearchAction","target":{"urlTemplate":"https://search.example.com/find?q={search_term_string}"}}}</script>'
        self.assertEqual(search_template(schema, 'https://example.com/', 'example.com'), 'https://search.example.com/find?q={q}')
        form = '<form action="/search"><input type="hidden" name="type" value="video"><input type="hidden" name="csrf" value="secret"><input type="search" name="keyword"></form>'
        result = search_template(form, 'https://example.com/', 'example.com')
        self.assertEqual(result, 'https://example.com/search?type=video&keyword={q}')

    def test_no_post_password_or_cross_site_forms(self):
        for attrs, extra in [('method="post"', ''), ('action="https://evil.com/search"', ''),
                             ('action="https://example.com.evil.com/"', ''), ('', '<input type="password">')]:
            self.assertFalse(search_template('<form '+attrs+'><input name="q">'+extra+'</form>', 'https://example.com/', 'example.com'))

    def test_discovery_is_homepage_only_and_falls_back(self):
        with patch('app.agent_access.public_url'), patch('app.net.HttpClient') as client:
            client.return_value.get.side_effect = RuntimeError('offline')
            site, message = discover_site('https://www.example.com/private/video?token=abc', Settings(), threading.Event())
            self.assertEqual(client.return_value.get.call_args.args[0], 'https://example.com/')
            self.assertEqual(site.kind, 'domain')
            self.assertIn('未发现', message)
            client.return_value.close.assert_called_once()

    def test_discovery_uses_declared_native_entry(self):
        with patch('app.agent_access.public_url'), patch('app.net.HttpClient') as client:
            response = client.return_value.get.return_value
            response.__enter__.return_value = response
            response.status_code, response.encoding = 200, 'utf-8'
            response.iter_content.return_value = [b'<form action="/search"><input name="q" type="search"></form>']
            site, _ = discover_site('https://example.com/video/123', Settings(), threading.Event())
            self.assertEqual((site.kind, site.scope_domain, site.search_url), ('generic', 'example.com', 'https://example.com/search?q={q}'))

    def test_duplicate_preserves_platform_identity(self):
        a, _ = infer_site('https://b23.tv/123')
        b, _ = infer_site('https://www.bilibili.com/a')
        self.assertTrue(same_site(a, b))
        a, _ = infer_site('https://video.example.com/watch/1')
        b, _ = infer_site('https://www.example.com/other/path')
        self.assertTrue(same_site(a, b))
        self.assertFalse(same_site(a, infer_site('https://another.com')[0]))

    def test_domain_adapter_filters_and_keeps_paging_and_cancel(self):
        from app.adapters.domain import DomainAdapter
        site, _ = infer_site('https://www.example.com/video/1')
        rows = PageResults([SearchResult(title='yes', url='https://sub.example.com/other/2'),
                            SearchResult(title='no', url='https://example.com.evil.com/v')], exhausted=False)
        ctx = SearchContext('cat', None, page=2)
        ctx.cancelled = lambda: True
        with patch('app.adapters.bing.BingAdapter.search', return_value=rows) as search:
            found = DomainAdapter().search(ctx, site)
            scoped = search.call_args.args[0]
            self.assertEqual(scoped.query, 'site:example.com cat')
            self.assertEqual(scoped.page, 2)
            self.assertTrue(scoped.cancelled())
        self.assertEqual([r.title for r in found], ['yes'])
        self.assertFalse(found.exhausted)

    def test_native_extraction_obeys_domain_before_limit(self):
        from app.adapters.generic import GenericAdapter
        site, _ = infer_site('https://example.com/page')
        html = '<a href="https://foreign.com/video/1"><img src="/1.jpg" alt="Outside video title"></a><a href="https://media.example.com/watch/2"><img src="/2.jpg" alt="Matching video title"></a>'
        found = GenericAdapter().extract(html, 'https://example.com/search', site, 1)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].url, 'https://media.example.com/watch/2')

    def test_agent_scope_rejects_forged_name_or_id(self):
        for site in [infer_site('https://example.com')[0], infer_site('https://bilibili.com')[0]]:
            key = site_group(site)[0]
            fake = SearchResult(title='fake', url='https://foreign.com/watch/1', source=site.display, source_id=site.id)
            self.assertNotIn(key, result_groups(fake, [site]))
            real = SearchResult(title='real', url='https://sub.'+site.scope_domain+'/any/path')
            self.assertIn(key, result_groups(real, [site]))

    def test_config_roundtrip(self):
        site = infer_site('https://example.com/abc')[0]
        self.assertEqual(SiteConfig.from_dict(site.to_dict()), site)
        self.assertFalse(site.validate())
        self.assertTrue(site.is_valid())


class SiteIntakeUITests(unittest.TestCase):
    setUpClass = classmethod(access_test.AccessUITests.setUpClass.__func__)
    tearDownClass = classmethod(access_test.AccessUITests.tearDownClass.__func__)
    setUp = access_test.AccessUITests.setUp
    tearDown = access_test.AccessUITests.tearDown

    def wheel(self, widget, pixel=False, delta=-120):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        pos = widget.rect().center()
        event = QWheelEvent(QPointF(pos), QPointF(widget.mapToGlobal(pos)),
            QPoint(0, -35) if pixel else QPoint(), QPoint() if pixel else QPoint(0, delta),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        self.app.sendEvent(widget, event)
        self.app.processEvents()

    def test_wheel_cannot_change_any_main_or_dialog_value(self):
        from app.ui.dialogs import SiteDialog, SettingsDialog
        from PySide6.QtWidgets import QComboBox, QAbstractSpinBox
        from PySide6.QtTest import QSignalSpy
        from app.ui.controls import WheelSafeComboBox, WheelSafeSpinBox, WheelSafeDoubleSpinBox
        dialogs = [SiteDialog(None, None, '', self.win), SettingsDialog(self.win.settings, self.win.http, parent=self.win)]
        for host in [self.win, *dialogs]:
            host.show()
            self.app.processEvents()
            controls = host.findChildren(QComboBox) + host.findChildren(QAbstractSpinBox)
            self.assertTrue(controls)
            for control in controls:
                self.assertIsInstance(control, (WheelSafeComboBox, WheelSafeSpinBox, WheelSafeDoubleSpinBox))
                value = control.currentIndex if isinstance(control, QComboBox) else control.value
                signal = control.currentIndexChanged if isinstance(control, QComboBox) else control.valueChanged
                spy = QSignalSpy(signal)
                before = value()
                control.setFocus()
                for pixel in [False, True]:
                    for delta in [-120, 120]:
                        self.wheel(control, pixel, delta)
                    if isinstance(control, QAbstractSpinBox):
                        self.wheel(control.lineEdit(), pixel)
                self.assertEqual(value(), before)
                self.assertEqual(spy.count(), 0)
        for dlg in dialogs:
            dlg.reject()
            dlg.deleteLater()

    def test_wheel_over_controls_scrolls_surrounding_dialog(self):
        from app.ui.dialogs import SiteDialog
        dlg = SiteDialog(None, None, '', self.win)
        dlg.show()
        dlg.resize(520, 400)
        dlg.advanced_toggle.click()
        self.app.processEvents()
        bar = dlg.body_scroll.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        for control in [dlg.route_combo, dlg.limit_spin, dlg.limit_spin.lineEdit()]:
            for pixel in [False, True]:
                bar.setValue(0)
                self.wheel(control, pixel)
                self.assertGreater(bar.value(), 0, (type(control).__name__, pixel))
        dlg.reject()
        dlg.deleteLater()

    def test_wheel_safe_controls_keep_keyboard_clicks_and_popup_scrolling(self):
        from app.ui.dialogs import SiteDialog
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        dlg = SiteDialog(None, None, '', self.win)
        dlg.show()
        self.app.processEvents()
        dlg.route_combo.setFocus()
        QTest.keyClick(dlg.route_combo, Qt.Key.Key_Down)
        self.assertEqual(dlg.route_combo.currentIndex(), 1)
        dlg.limit_spin.setFocus()
        QTest.keyClick(dlg.limit_spin, Qt.Key.Key_Up)
        self.assertEqual(dlg.limit_spin.value(), 25)
        dlg.limit_spin.stepDown()
        self.assertEqual(dlg.limit_spin.value(), 24)
        dlg.route_combo.addItems(['选项%d' % n for n in range(50)])
        dlg.route_combo.showPopup()
        self.app.processEvents()
        view = dlg.route_combo.view()
        before = view.verticalScrollBar().value()
        self.wheel(view.viewport())
        self.assertGreater(view.verticalScrollBar().value(), before)
        dlg.route_combo.hidePopup()
        dlg.reject()
        dlg.deleteLater()

    def test_lists_hidden_until_management_opened(self):
        from PySide6.QtWidgets import QFrame
        side = self.win.findChild(QFrame, 'Sidebar')
        self.assertFalse(side.isAncestorOf(self.win.site_list))
        self.assertFalse(side.isAncestorOf(self.win.black_list))
        self.assertFalse(self.win.site_list.isVisible())
        self.assertFalse(self.win.black_list.isVisible())
        self.win.manage_sites_btn.click()
        self.app.processEvents()
        self.assertTrue(self.win.site_list.isVisible())
        self.win.site_manager.close()
        self.win.manage_blacklist_btn.click()
        self.app.processEvents()
        self.assertTrue(self.win.black_list.isVisible())
        self.win.site_manager.close()
        self.assertFalse(self.win.black_list.isVisible())

    def test_paste_and_immediate_save(self):
        from app.ui.dialogs import SiteDialog
        dlg = SiteDialog(None, None, '', self.win)
        dlg.url_edit.setText('https://b23.tv/abcdef')
        dlg._on_accept()
        self.assertEqual(dlg.result_site.builtin, 'bilibili')
        self.assertEqual(dlg.result(), 1)
        dlg.deleteLater()

    def test_advanced_collapses_without_discarding_existing_css(self):
        from app.ui.dialogs import SiteDialog
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        site = SiteConfig(name='自定义', search_url='https://example.com/search?q={q}',
                          css={'item': 'article', 'title': 'h2', 'link': 'a', 'image': 'img'})
        dlg = SiteDialog(site, None, '', self.win)
        dlg.show()
        self.app.processEvents()
        self.assertFalse(dlg.advanced.isVisible())
        self.assertFalse(dlg.advanced_toggle.isChecked())
        QTest.mouseClick(dlg.advanced_toggle, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertTrue(dlg.advanced.isVisible())
        dlg.css_title.setText('h3')
        QTest.keyClick(dlg.advanced_toggle, Qt.Key.Key_Space)
        self.app.processEvents()
        self.assertFalse(dlg.advanced.isVisible())
        dlg._on_accept()
        self.assertEqual(dlg.result_site.css, {**site.css, 'title': 'h3'})
        dlg.deleteLater()

    def test_late_resolution_cannot_overwrite_new_input_or_custom_name(self):
        from app.ui.dialogs import SiteDialog
        dlg = SiteDialog(None, None, '', self.win)
        dlg.url_edit.setText('https://example.com/a')
        old = dlg._intake_revision
        dlg.url_edit.setText('https://youtube.com/watch?v=abcdefghijk')
        dlg.name_edit.setText('我的视频来源')
        dlg._apply_resolution(old, 'https://example.com/a', infer_site('https://example.com')[0], 'old')
        self.assertIsNone(dlg._resolution)
        dlg._begin_resolve()
        self.assertEqual(dlg.name_edit.text(), '我的视频来源')
        self.assertEqual(dlg._collect().builtin, 'youtube')
        dlg.reject()
        self.assertTrue(dlg._intake_cancel.is_set())
        dlg.deleteLater()

    def test_duplicate_add_keeps_existing_custom_settings(self):
        existing = next(s for s in self.win.config.sites if s.builtin == 'bilibili')
        existing.name, existing.enabled, existing.route = '已有自定义名称', False, 'direct'
        count = len(self.win.config.sites)
        fake = SimpleNamespace(result_site=infer_site('https://b23.tv/a')[0], exec=lambda: 1)
        with patch('app.ui.mainwindow.SiteDialog', return_value=fake):
            self.win._add_site()
        self.assertEqual(len(self.win.config.sites), count)
        self.assertEqual((existing.name, existing.route, existing.enabled), ('已有自定义名称', 'direct', True))

    def test_background_discovery_completes_pending_save(self):
        from app.ui.dialogs import SiteDialog
        dlg = SiteDialog(None, self.win.http, '', self.win)
        resolved, message = infer_site('https://example.com/')
        resolved.kind, resolved.search_url = 'generic', 'https://example.com/search?q={q}'
        release = threading.Event()
        def discovery(*args):
            release.wait(2)
            return resolved, message
        with patch('app.site_intake.discover_site', side_effect=discovery):
            dlg.url_edit.setText('https://example.com/video/1')
            dlg._on_accept()
            self.assertFalse(dlg.save_button.isEnabled())
            self.assertIsNone(dlg.result_site)
            release.set()
            deadline = time.monotonic() + 3
            while dlg.result_site is None and time.monotonic() < deadline:
                time.sleep(.01)
                self.app.processEvents()
            self.assertIsNotNone(dlg.result_site)
            self.assertEqual(dlg.result_site.search_url, resolved.search_url)
            self.assertEqual(dlg.result(), 1)
        dlg.deleteLater()


if __name__ == '__main__':
    unittest.main(verbosity=2)
