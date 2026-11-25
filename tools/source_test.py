"""Platform classification and local source checkbox filtering."""
import sys
import testenv
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Config, SiteConfig
from app.models import SearchResult
from app.sources import result_group, site_group
import access_test


class SourceTests(unittest.TestCase):
    def test_actual_platform_overrides_discovery_engine_and_agent_aliases(self):
        cfg = Config()
        for source in ('Agent', 'B站', 'bilibili', '哔哩哔哩', '必应网页'):
            row = SearchResult(title='a', url='https://www.bilibili.com/video/BV123', source=source, source_id='bing')
            self.assertEqual(result_group(row, cfg.sites)[0], 'platform:bilibili')
        row = SearchResult(title='a', url='https://youtu.be/abcdefghijk', source='Agent')
        self.assertEqual(result_group(row, cfg.sites)[0], 'platform:youtube')

    def test_custom_domain_and_host_boundary(self):
        site = SiteConfig(id='custom', name='我的站点', search_url='https://video.example.com/?q={q}')
        self.assertEqual(result_group(SearchResult(title='a', url='https://video.example.com/v/1'), [site]), site_group(site))
        row = SearchResult(title='a', url='https://bilibili.com.evil.example/v', source='Agent')
        self.assertNotEqual(result_group(row, [site])[0], 'platform:bilibili')


class SourceUITests(unittest.TestCase):
    setUpClass = classmethod(access_test.AccessUITests.setUpClass.__func__)
    tearDownClass = classmethod(access_test.AccessUITests.tearDownClass.__func__)
    setUp = access_test.AccessUITests.setUp
    tearDown = access_test.AccessUITests.tearDown

    def seed(self):
        self.win.settings.show_thumbnails = False
        self.win._append_results([
            SearchResult(title='B新', url='https://www.bilibili.com/video/BV1', source='Agent', views=20000, pub_ts=time.time()-60),
            SearchResult(title='B旧', url='https://www.bilibili.com/video/BV2', source='B站', views=5, pub_ts=time.time()-999999),
            SearchResult(title='Y', url='https://www.youtube.com/watch?v=abcdefghijk', source='YouTube', views=50000),
        ])

    def only_bili(self):
        self.win._source_all_clicked(False)
        self.win._source_checks['platform:bilibili'].click()

    def test_initial_four_sources_all_checked(self):
        self.assertEqual(len(self.win._source_checks), 4)
        self.assertTrue(all(c.isChecked() for c in self.win._source_checks.values()))

    def test_only_button_switches_in_one_action_and_preserves_site_configuration(self):
        self.seed()
        enabled=[s.id for s in self.win.config.enabled_sites()]
        with patch.object(self.win,'_source_scope_changed',wraps=self.win._source_scope_changed) as changed:
            self.win._source_only_buttons['platform:youtube'].click()
            changed.assert_called_once()
        self.assertEqual([c.result.title for c in self.win._cards],['Y'])
        self.assertEqual(self.win._source_selection,{'platform:youtube'})
        self.assertEqual([s.id for s in self.win._selected_search_sites()],['ytb'])
        self.assertEqual([s.id for s in self.win.config.enabled_sites()],enabled)
        self.win._source_only_buttons['platform:bilibili'].click()
        self.assertEqual(len(self.win._cards),2)
        self.win.source_all.click()
        self.assertEqual(len(self.win._cards),3)

    def test_only_existing_selection_is_noop_and_new_sources_stay_excluded(self):
        self.seed()
        self.win._source_only_buttons['platform:bilibili'].click()
        with patch.object(self.win,'_source_scope_changed') as changed:
            self.win._source_only_buttons['platform:bilibili'].click()
            changed.assert_not_called()
        self.win._append_results([SearchResult(title='new',url='https://vimeo.com/456')])
        self.assertFalse(self.win._source_checks['platform:vimeo'].isChecked())
        self.assertEqual(len(self.win._cards),2)

    def test_removed_source_removes_both_checkbox_and_only_button(self):
        self.win.config.get_site('ytb').enabled=False
        self.win._sync_source_filters()
        self.assertNotIn('platform:youtube',self.win._source_checks)
        self.assertNotIn('platform:youtube',self.win._source_rows)
        self.assertNotIn('platform:youtube',self.win._source_only_buttons)

    def test_single_multi_none_all_and_counts(self):
        self.seed()
        self.only_bili()
        self.assertEqual(len(self.win._cards), 2)
        self.assertTrue(all(self.win._source_key(c.result) == 'platform:bilibili' for c in self.win._cards))
        self.win._source_checks['platform:youtube'].click()
        self.assertEqual(len(self.win._cards), 3)
        self.win._source_all_clicked(False)
        self.assertEqual(len(self.win._all_results), 3)
        self.assertEqual(len(self.win._cards), 0)
        self.assertTrue(self.win.filter_empty_hint.isVisible())
        self.win.source_all.click()
        self.assertEqual(len(self.win._cards), 3)
        self.assertIn('2', self.win._source_checks['platform:bilibili'].text())

    def test_streaming_new_group_preserves_narrowed_selection(self):
        self.seed()
        self.only_bili()
        self.win._append_results([SearchResult(title='V', url='https://vimeo.com/123', source='Agent')])
        self.assertIn('platform:vimeo', self.win._source_checks)
        self.assertFalse(self.win._source_checks['platform:vimeo'].isChecked())
        self.assertEqual(len(self.win._cards), 2)
        self.win._append_results([SearchResult(title='B3', url='https://b23.tv/abc', source='Agent')])
        self.assertEqual(len(self.win._cards), 3)

    def test_default_all_includes_new_groups(self):
        self.seed()
        self.win._append_results([SearchResult(title='V', url='https://vimeo.com/123', source='Agent')])
        self.assertTrue(self.win._source_checks['platform:vimeo'].isChecked())
        self.assertEqual(len(self.win._cards), 4)

    def test_combines_with_time_views_and_clear_resets(self):
        self.seed()
        self.only_bili()
        self.win.time_combo.setCurrentIndex(1)
        self.win.views_combo.setCurrentIndex(1)
        self.assertEqual([c.result.title for c in self.win._cards], ['B新'])
        self.assertIn('2', self.win._source_checks['platform:bilibili'].text())
        self.win._clear_results()
        self.assertIsNone(self.win._source_selection)
        self.assertTrue(all(c.isChecked() for c in self.win._source_checks.values()))

    def test_filter_is_local_and_cancels_pending_scroll(self):
        self.seed()
        used = self.win._agent_budget_used
        self.win._agent_scroll_pending = True
        with patch.object(self.server, 'push_task') as push:
            self.only_bili()
            self.app.processEvents()
            push.assert_not_called()
        self.assertFalse(self.win._agent_scroll_pending)
        self.assertEqual(self.win._agent_budget_used, used)

    def test_disabled_source_with_existing_results_remains_filterable(self):
        self.seed()
        self.win.config.get_site('bili').enabled = False
        self.win._reload_sites()
        self.only_bili()
        self.assertEqual(len(self.win._cards), 2)

    def test_direct_filter_layout_does_not_start_paging(self):
        self.win._set_mode('direct')
        self.seed()
        self.win._query = 'test'
        with patch.object(self.win, '_load_more') as load:
            self.only_bili()
            self.win._on_scroll()
            self.app.processEvents()
            load.assert_not_called()

    def test_popup_multiselect_stays_open_and_escape_closes(self):
        from PySide6.QtCore import Qt, QPoint
        from PySide6.QtTest import QTest
        self.assertFalse(hasattr(self.win, 'group_check'))
        self.assertFalse(self.win.source_menu.isVisible())
        self.seed()
        button = self.win.source_button
        self.win.source_menu.popup(button.mapToGlobal(button.rect().bottomLeft()))
        self.app.processEvents()
        check = self.win._source_checks['platform:youtube']
        QTest.mouseClick(check, Qt.MouseButton.LeftButton, pos=QPoint(12, 17))
        self.app.processEvents()
        self.assertFalse(check.isChecked())
        self.assertTrue(self.win.source_menu.isVisible())
        QTest.keyClick(self.win.source_menu, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertFalse(self.win.source_menu.isVisible())
        self.assertEqual(len(self.win._cards), 2)

    def test_many_sources_scroll_and_search_does_not_change_selection(self):
        self.win.settings.show_thumbnails = False
        self.win._append_results([SearchResult(title='x', url='https://example%d.com/v' % i,
                                    source='平台%d' % i) for i in range(40)])
        button = self.win.source_button
        self.win.source_menu.popup(button.mapToGlobal(button.rect().bottomLeft()))
        self.app.processEvents()
        self.assertLessEqual(self.win.source_scroll.height(), 280)
        self.assertGreater(self.win.source_scroll.verticalScrollBar().maximum(), 0)
        self.win.source_search.setText('平台39')
        self.app.processEvents()
        self.assertEqual(sum(not c.isHidden() for c in self.win._source_checks.values()), 1)
        self.assertIsNone(self.win._source_selection)
        self.assertEqual(len(self.win._cards), 40)
        self.win.source_menu.hide()

    def test_sort_orders_across_platforms_without_group_override(self):
        self.seed()
        self.win.sort_combo.setCurrentIndex(2)
        self.assertEqual([c.result.title for c in self.win._cards], ['Y', 'B新', 'B旧'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
