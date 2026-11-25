# -*- coding: utf-8 -*-
"""资源搜寻器 —— 程序入口。

开发时： python main.py
打包后： 资源搜寻器.exe

还有一个隐藏的自检开关，供打包后验收用（窗口版没有控制台，所以把结果写文件）：

    资源搜寻器.exe --selftest --selfout selftest.txt
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _fix_taskbar_icon() -> None:
    """Windows 任务栏图标跟随窗口图标，而不是 Python 的图标。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "ResourceFinder.App.1")
    except Exception:
        pass


def _arg_value(argv, name, default=""):
    for i, item in enumerate(argv):
        if item == name and i + 1 < len(argv):
            return argv[i + 1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return default


def _run_selftest(out_path: str) -> int:
    """打包环境自检：依赖、配置、网络、Qt 窗口能不能起来。"""
    import io
    import traceback
    import tempfile
    import time

    # Self-test must never overwrite the user's configuration or evict their cache.
    test_home = tempfile.TemporaryDirectory(prefix="rf-frozen-test-")
    os.environ["RESOURCE_FINDER_HOME"] = test_home.name

    lines = []
    ok = True

    def log(msg: str) -> None:
        lines.append(msg)

    try:
        import PySide6
        import requests

        log("PySide6 %s" % PySide6.__version__)
        log("requests %s" % requests.__version__)
        log("python %s" % sys.version.replace("\n", " "))

        from app import config

        log("app_dir    = %s" % config.app_dir())
        log("config_dir = %s" % config.config_dir())
        log("cache_dir  = %s" % config.cache_dir())

        cfg = config.load_config()
        if '--selfcovers' in sys.argv:
            for site in cfg.sites:
                site.enabled = site.builtin == 'youtube'
            cfg.settings.show_thumbnails = True
        log("sites=%d blacklist=%d history=%d"
            % (len(cfg.sites), len(cfg.blacklist), len(cfg.history)))

        from app.net import HttpClient, describe_network

        log(describe_network(cfg.settings))
        http = HttpClient(cfg.settings)
        html = http.get_text("https://www.bilibili.com/", timeout=12.0)
        log("network ok: bilibili html=%d bytes" % len(html))

        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        if '--selfprofiles' in sys.argv:
            from app.selfcheck_profiles import run as profile_smoke
            profile_smoke(app, log, _arg_value(sys.argv, '--selfprofileshots'))
        from app.ui.mainwindow import MainWindow

        win = MainWindow(cfg)
        if '--selfcovers' in sys.argv:
            from app.imagecodec import is_avif, prepare_image
            from PIL import Image, features
            from PySide6.QtGui import QImage
            if not features.check('avif'):
                raise RuntimeError('成品缺少 AVIF 解码器')
            sample = io.BytesIO()
            Image.new('RGB', (64, 48), (25, 90, 180)).save(sample, format='AVIF')
            if QImage.fromData(prepare_image(sample.getvalue())).isNull():
                raise RuntimeError('成品 AVIF 转码自检失败')
            downloaded_formats = {}
            get_bytes = win.http.get_bytes
            def tracked_bytes(url, *a, **kw):
                data = get_bytes(url, *a, **kw)
                downloaded_formats[url] = 'AVIF' if is_avif(data) else 'other'
                return data
            win.http.get_bytes = tracked_bytes
        win.show()
        app.processEvents()
        from app.site_intake import root_domain
        from app.ui.dialogs import SiteDialog
        assert root_domain('https://a.example.co.uk/video') == 'example.co.uk'
        assert root_domain('https://alice.github.io/page') == 'alice.github.io'
        assert not win.site_list.isVisible() and not win.black_list.isVisible()
        win.manage_sites_btn.click()
        app.processEvents()
        assert win.site_list.isVisible()
        win.site_manager.close()
        win.manage_blacklist_btn.click()
        app.processEvents()
        assert win.black_list.isVisible()
        win.site_manager.close()
        intake = SiteDialog(None, None, '', win)
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        for control in (intake.route_combo, intake.limit_spin, win.sort_combo, win.agent_budget_combo, win.resource_combo):
            value = control.currentIndex if hasattr(control, 'currentIndex') else control.value
            before = value()
            event = QWheelEvent(QPointF(5, 5), QPointF(control.mapToGlobal(QPoint(5, 5))),
                QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase, False)
            app.sendEvent(control, event)
            assert value() == before
        log('wheel safety ok: main filters, Agent budget, site dropdown and numeric input preserve values')
        assert not intake.advanced_toggle.isChecked() and intake.advanced.isHidden()
        intake.advanced_toggle.click()
        assert not intake.advanced.isHidden()
        intake.css_item.setText('article')
        intake.advanced_toggle.click()
        assert intake.advanced.isHidden() and intake.css_item.text() == 'article'
        intake.css_item.clear()
        intake.url_edit.setText('https://www.bilibili.com/video/BV12g4E6aEat/?tracking=test')
        intake._on_accept()
        assert intake.result_site.builtin == 'bilibili' and intake.result_site.scope_domain == 'bilibili.com'
        assert 'tracking' not in intake.result_site.search_url
        intake.deleteLater()
        log('site intake ok: bundled PSL, tenant boundaries, hidden lists, both management tabs, paste and save')
        if '--selfresources' in sys.argv:
            from app.models import SearchResult
            from app.adapters import adapter_for
            from app.config import Settings
            assert win.resource_combo.count() == 4 and win.resource_combo.findData('novel') == -1
            assert Settings.from_dict({'resource_type': 'novel'}).resource_type == 'article'
            from app.site_catalog import catalog_sites
            assert len(catalog_sites()) == 13 and all(not s.enabled and s.is_valid() for s in catalog_sites())
            win.settings.show_thumbnails = False
            for kind in ('image', 'article', 'sound', 'video'):
                win.resource_combo.setCurrentIndex(win.resource_combo.findData(kind))
                assert win.settings.resource_type == kind
                assert win.views_combo.isEnabled() == (kind == 'video')
                if kind != 'video':
                    assert adapter_for(cfg.get_site('bing'), kind).resource_type == kind
                assert not win._cards
                win._append_results([SearchResult(title=kind, url='https://example.com/'+kind, resource_type=kind)])
                assert len(win._cards) == 1 and win._cards[0].result.resource_type == kind
            assert not hasattr(win, 'type_filter')
            win._clear_results()
            win.settings.show_thumbnails = True
            log('resources ok: one type selector, four modes, clear on type switch, bundled adapters, video-only views and wheel safety')
        if '--selfpaging' in sys.argv:
            win.settings.show_thumbnails = False
            win.cover_notice.show()
            win.cover_notice.click()
            if not win.settings.show_thumbnails or win.cover_notice.isVisible():
                raise RuntimeError('开启封面按钮失败')
        log("qt ok: window=%dx%d title=%s" % (win.width(), win.height(), win.windowTitle()))
        if '--selfliveness' in sys.argv:
            from app.selfcheck_liveness import run as liveness_smoke
            liveness_smoke(app, win, log)
        if '--selfworker' in sys.argv:
            from app.selfcheck_worker import run as worker_smoke
            worker_smoke(app, win, cfg, log, _arg_value(sys.argv, '--selfworkerpython'))
        if '--selfpairing' in sys.argv:
            from app.selfcheck_pairing import run as pairing_smoke
            pairing_smoke(app, win, log, _arg_value(sys.argv, '--selfpairingshot'))
        if '--selfdurations' in sys.argv:
            from app.selfcheck_durations import run as duration_smoke
            duration_smoke(app, win, log, _arg_value(sys.argv, '--selfdurationshot'))
        if '--selfaigeiauto' in sys.argv:
            from app.selfcheck_aigeiauto import run as aigei_auto_smoke
            aigei_auto_smoke(app,win,log,_arg_value(sys.argv,'--selfaigeiautourl','https://www.aigei.com/item/xia_yu_sheng_1052.html'))
        playback_fixture=_arg_value(sys.argv,'--selfaigeiplayback')
        if playback_fixture:
            from app.selfcheck_aigeiplayback import run as aigei_playback_smoke
            aigei_playback_smoke(app,win,playback_fixture,log)
        if '--selfdownloadcenter' in sys.argv:
            from app.selfcheck_downloadcenter import run as download_center_smoke
            download_center_smoke(app,win,log)
        if '--selfdownloads' in sys.argv:
            from app.downloads import binary, run_process, media_info
            import threading
            for name in ('yt-dlp', 'deno', 'ffmpeg', 'ffprobe'):
                flag = '--version' if name in ('yt-dlp', 'deno') else '-version'
                component_lines = run_process([binary(name), flag], threading.Event())
                log('bundled component ok: ' + name + ' ' + component_lines[0][:140])
            target_url = _arg_value(sys.argv, '--selfdownloadurl')
            if target_url:
                from app.models import SearchResult
                mode = _arg_value(sys.argv, '--selfdownloadmode', 'full')
                kind = _arg_value(sys.argv, '--selfdownloadkind', 'video')
                row=SearchResult(title=_arg_value(sys.argv,'--selfdownloadtitle','打包下载验收'),url=target_url,resource_type=kind)
                options=dict(mode=mode,kind=kind,height=360,container='mp4',audio_format='mp3',pages='first',subtitles=False)
                from app.downloads import platform,inspect_resource
                if platform(row)=='aigei-web':
                    row.extra['aigei_query']=_arg_value(sys.argv,'--selfdownloadhint',row.title)
                    found=inspect_resource(row,cfg.settings,{},threading.Event())
                    if not found.get('web_variants'):raise RuntimeError('网页没有返回可提取版本')
                    selected=found['web_variants'][0]
                    options.update(mode=found['kind'],kind=found['kind'],aigei_variant=selected['key'],extraction_label=selected['label'])
                    log('web extraction: '+selected['label'])
                job_id = win.downloads.add(row, {}, options,
                    os.path.join(test_home.name, 'download-output'))
                deadline = time.monotonic() + 180
                while win.downloads.jobs[job_id]['state'] in ('queued','running','cancelling') and time.monotonic()<deadline:
                    time.sleep(.1);app.processEvents()
                job=win.downloads.jobs[job_id]
                if job['state']!='done':raise RuntimeError('下载验收失败：'+job.get('error',job['state']))
                log('download ok: '+mode+' '+str([os.path.getsize(p) for p in job['files']]))
                if kind=='video':log('download streams: '+str(media_info(job['files'][0],threading.Event())))
        query = _arg_value(sys.argv, "--selfquery")
        kind = _arg_value(sys.argv, '--selftype', 'video')
        if kind == 'novel':
            kind = 'article'
        if kind != 'video':
            win.resource_combo.setCurrentIndex(win.resource_combo.findData(kind))
            for site in cfg.sites:
                site.enabled = site.id == 'bing'
        self_site = _arg_value(sys.argv, '--selfsite')
        if self_site:
            if cfg.get_site(self_site) is None:
                raise RuntimeError('未知的自检站点：' + self_site)
            for site in cfg.sites:
                site.enabled = site.id == self_site
        if query:
            win.search_edit.setText(query)
            win._start_search()
            deadline = time.monotonic() + 90
            while win._searching and time.monotonic() < deadline:
                time.sleep(.1)
                app.processEvents()
            if win._searching or not win._all_results:
                raise RuntimeError("搜索冒烟失败：" + win.status_label.text())
            log("search ok: query=%s cards=%d" % (query, len(win._cards)))
            # Let visible thumbnails finish without requiring any particular host.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not any(c.has_cover for c in win._cards):
                time.sleep(.1)
                app.processEvents()
            log('covers loaded: %d/%d' % (sum(c.has_cover for c in win._cards), len(win._cards)))
            if '--selfcover' in sys.argv and not any(c.has_cover for c in win._cards):
                raise RuntimeError('封面冒烟失败：卡片无可用封面')
            shot = _arg_value(sys.argv, "--selfshot")
            if shot:
                win.grab().save(shot)
            if '--selfsources' in sys.argv:
                total = len(win._all_results)
                expected = sum(win._source_key(r) == 'platform:bilibili' for r in win._all_results)
                if not expected or len(win._source_checks) < 4:
                    raise RuntimeError('来源筛选自检缺少 B站结果或站点选项')
                win._source_all_clicked(False)
                win._source_checks['platform:bilibili'].click()
                app.processEvents()
                if len(win._cards) != expected or any(win._source_key(c.result) != 'platform:bilibili' for c in win._cards):
                    raise RuntimeError('B站来源单选筛选失败')
                source_shot = _arg_value(sys.argv, '--selfsourceshot')
                if source_shot:
                    win.grab().save(source_shot)
                popup_shot = _arg_value(sys.argv, '--selfsourcepopupshot')
                if popup_shot:
                    button = win.source_button
                    win.source_menu.popup(button.mapToGlobal(button.rect().bottomLeft()))
                    app.processEvents()
                    win.source_menu.grab().save(popup_shot)
                    win.source_menu.hide()
                win._source_all_clicked(False)
                app.processEvents()
                if win._cards or len(win._all_results) != total:
                    raise RuntimeError('取消所有来源应隐藏卡片但保留结果')
                win.source_all.click()
                app.processEvents()
                if len(win._cards) != total:
                    raise RuntimeError('全选来源未恢复结果')
                log('sources ok: %d groups; Bilibili-only %d/%d; none/all restores without new search'
                    % (len(win._source_checks), expected, total))
        if '--selfcovers' in sys.argv:
            if len(win._cards) < 8:
                raise RuntimeError('YouTube 真实封面验收需要至少 8 个搜索结果')
            for label, cards in [('first', win._cards[:8]), ('bottom', win._cards[-4:])]:
                if label == 'bottom':
                    win.scroll.verticalScrollBar().setValue(win.scroll.verticalScrollBar().maximum())
                win._pump_thumbs()
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline and not all(c.has_cover for c in cards):
                    time.sleep(.05)
                    app.processEvents()
                if not all(c.has_cover for c in cards):
                    raise RuntimeError('YouTube %s 封面显示失败：%d/%d' % (label, sum(c.has_cover for c in cards), len(cards)))
                log('YouTube covers %s: %d/%d decoded and displayed' % (label, len(cards), len(cards)))
            avif_cards = [c for c in win._cards[:8] if downloaded_formats.get(c.cover_url) == 'AVIF']
            if not avif_cards:
                raise RuntimeError('本次未收到真实 AVIF 封面，无法完成该格式的联网验收')
            for card in avif_cards:
                path = win.thumbs.cached(card.cover_url)
                if not path or QImage(path).isNull():
                    raise RuntimeError('AVIF 原始封面未完成缓存转码')
                with open(path, 'rb') as f:
                    if not f.read(8).startswith(b'\x89PNG'):
                        raise RuntimeError('AVIF 未转换为 PNG 缓存')
            if win.thumbs._host_dead('https://i.ytimg.com/'):
                raise RuntimeError('成功解码后图片服务器仍被错误封锁')
            log('AVIF original covers converted to PNG: %d; host healthy; codec bundled' % len(avif_cards))
            win.scroll.verticalScrollBar().setValue(0)
            app.processEvents()
            shot = _arg_value(sys.argv, '--selfcovershot')
            if shot:
                win.grab().save(shot)
        if '--selfpaging' in sys.argv and query:
            win._source_all_clicked(False)
            win._source_checks['platform:bilibili'].click()
            before = len(win._all_results)
            previous_pages = dict(win._site_pages)
            from PySide6.QtCore import QPoint, QPointF, Qt
            from PySide6.QtGui import QWheelEvent
            bar = win.scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
            event = QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(0, -20), QPoint(),
                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False)
            app.sendEvent(win.scroll.viewport(), event)
            deadline = time.monotonic()+80
            while time.monotonic() < deadline:
                time.sleep(.05); app.processEvents()
                if win._paging_pages and not win._paging:
                    break
            if win._paging or len(win._all_results) <= before or set(win._paging_pages) != {'bili'}:
                raise RuntimeError('常规模式未按 B站单独续搜')
            if any(win._site_pages.get(k) != v for k,v in previous_pages.items() if k != 'bili'):
                raise RuntimeError('未选站点的页码被推进')
            fresh = win._all_results[before:]
            if any(win._source_key(r) != 'platform:bilibili' for r in fresh):
                raise RuntimeError('续搜混入其他来源')
            bar.setValue(0)
            win._pump_thumbs()
            deadline = time.monotonic()+30
            while time.monotonic() < deadline and sum(c.has_cover for c in win._cards[:8]) < min(4, len(win._cards)):
                time.sleep(.05); app.processEvents()
            covers = sum(c.has_cover for c in win._cards[:8])
            if covers < min(4, len(win._cards)):
                raise RuntimeError('常规搜索封面未恢复')
            log('paging ok: Bilibili-only +%d, pages=%s, other cursors unchanged; visible covers=%d'
                % (len(fresh), win._paging_pages, covers))
            shot = _arg_value(sys.argv, '--selfpagingshot')
            if shot:
                win.grab().save(shot)
        if '--selfcontinuation' in sys.argv and query:
            from app.sources import site_group
            from app.models import SiteEvent, normalize_url
            from PySide6.QtCore import QPoint, QPointF, Qt
            from PySide6.QtGui import QWheelEvent
            for site in cfg.enabled_sites():
                key = site_group(site)[0]
                win._source_all_clicked(False)
                win._source_checks[key].click()
                for _ in range(3):
                    if site.id in win._site_exhausted:
                        log('continuation UI terminal: %s has explicit end evidence' % site.id)
                        break
                    before = {normalize_url(r.url) for r in win._all_results}
                    old_pages = dict(win._site_pages)
                    old_plan = win._paging_pages
                    expected = old_pages.get(site.id, 0) + 1
                    bar = win.scroll.verticalScrollBar()
                    bar.setValue(bar.maximum())
                    event = QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(0, -24), QPoint(),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False)
                    app.sendEvent(win.scroll.viewport(), event)
                    deadline = time.monotonic() + 100
                    while time.monotonic() < deadline:
                        time.sleep(.05)
                        app.processEvents()
                        if not win._paging and win._paging_pages is not old_plan:
                            break
                    else:
                        raise RuntimeError('%s 触底续页超时：%s' % (site.display, win.status_label.text()))
                    fresh = [r for r in win._all_results if normalize_url(r.url) not in before]
                    if any(r.source_id != site.id for r in fresh):
                        raise RuntimeError('%s 第 %d 页跨来源加载' % (site.display, expected))
                    if any(win._site_pages.get(k) != v for k, v in old_pages.items() if k != site.id):
                        raise RuntimeError('未选中的站点页码被推进')
                    if win._paging_failed:
                        if win._site_pages.get(site.id, 0) != old_pages.get(site.id, 0) or win._no_more:
                            raise RuntimeError('失败站点跳页或误报到底')
                        if '重试' not in win.pagination_notice.text():
                            raise RuntimeError('站点失败没有底部重试提示')
                        log('continuation UI retryable: %s page=%d unchanged; %s' %
                            (site.id, expected, win.pagination_notice.text()))
                        continue
                    if win._site_pages.get(site.id, 0) != expected:
                        raise RuntimeError('成功站点未推进页码')
                    if not fresh and site.id not in win._site_exhausted:
                        if win._no_more or not win.pagination_notice.isVisible():
                            raise RuntimeError('重复/筛选空页误报到底或没有提示')
                        log('continuation UI duplicate/filtered: %s page=%d; %s' %
                            (site.id, expected, win.pagination_notice.text()))
                    log('continuation UI ok: %s page=%d new=%d visible=%d' %
                        (site.id, expected, len(fresh), len(win._cards)))
            win.scroll.verticalScrollBar().setValue(0)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and sum(c.has_cover for c in win._cards[:8]) < 4:
                time.sleep(.1)
                app.processEvents()
            log('continuation visible covers=%d' % sum(c.has_cover for c in win._cards[:8]))
            # Deterministic terminal event, isolated self-test window only.
            win._record_page_status(SiteEvent(kind='done', site_id=site.id, site_name=site.display, exhausted=True))
            win._show_page_status()
            if '没有更多资源' not in win.pagination_notice.text() or not win.pagination_notice.isVisible():
                raise RuntimeError('末页底部提示不可见')
            log('footer ok: explicit terminal event produces named persistent notice')
            shot = _arg_value(sys.argv, '--selfcontinuationshot')
            if shot:
                win.grab().save(shot)
        if "--selfagent" in sys.argv:
            win._set_mode("agent")
            server = win._agent_server
            from app.agent_contract import AGENT_RULES
            assert AGENT_RULES in server.instructions()
            assert AGENT_RULES in server.task_text()
            assert '--serve 。' not in server.instructions()
            log('agent contract ok: copied help shares understanding/relevance rules, no default bare-serve collector recommendation')
            if not server:
                raise RuntimeError("Agent 收件口未启动")
            with requests.Session() as local:
                local.trust_env = False
                params = {"token": server.token, "agent": "SelfTest", "wait": 0}
                handshake = local.get(server.url + "/api/task", params=params, timeout=3).json()['task']
                if handshake.get('kind') != 'connect' or win.search_edit.isEnabled():
                    raise RuntimeError('接管前输入锁定或握手任务失败')
                local.post(server.url + '/api/connect', params={'token': server.token},
                           json={'agent': 'SelfTest', 'challenge': handshake['challenge'], 'ok': True}, timeout=3).raise_for_status()
                local.get(server.url + '/api/task', params=params, timeout=3).raise_for_status()
                win._tick_agent_bar()
                if not win.search_edit.isEnabled():
                    raise RuntimeError('接管确认后输入未开放')
                win.search_edit.setText('YouTube 网络自检' if '--selfnetwork' in sys.argv else '只搜国内任务派发自检')
                win._start_search()
                app.processEvents()
                if server.task_status()["state"] != "queued" or win.side_busy_spinner._timer.isActive():
                    raise RuntimeError("未领取的任务错误显示为搜索中")
                shot = _arg_value(sys.argv, "--selfshot")
                if shot and not query:
                    win.grab().save(shot)
                task = local.get(server.url + "/api/task", params=params, timeout=3).json()["task"]
                app.processEvents()
                if not task or server.task_status()["state"] != "delivered":
                    raise RuntimeError("Agent 未收到派发任务")
                ack = local.post(server.url + "/api/ack", params={"token": server.token},
                                 json={"task_id": task["id"], "agent": "SelfTest"}, timeout=3)
                app.processEvents()
                if not ack.json().get("acknowledged") or not win.side_busy_spinner._timer.isActive():
                    raise RuntimeError("领取确认没有更新界面")
                old_task = task
                win.search_edit.setText('YouTube 新词网络自检' if '--selfnetwork' in sys.argv else '只搜国内换词自检')
                before = time.monotonic()
                win._start_search()
                control = local.get(server.url + '/api/control',
                                    params={**params, 'task_id': old_task['id']}, timeout=3).json()
                task = local.get(server.url + '/api/task', params=params, timeout=3).json()['task']
                if not control.get('cancelled') or task['id'] == old_task['id'] or task['want'] != 100:
                    raise RuntimeError('旧搜索取消或快速批次派发失败')
                stale = local.post(server.endpoint(), json={'task_id': old_task['id'], 'agent': 'SelfTest',
                                   'done': True, 'results': []}, timeout=3)
                if stale.status_code != 409:
                    raise RuntimeError('旧任务回推未拒绝')
                local.post(server.url + '/api/ack', params=params,
                           json={'task_id': task['id'], 'agent': 'SelfTest'}, timeout=3).raise_for_status()
                app.processEvents()
                helper = local.get(server.url + '/api/control/helper', params=params, timeout=3).json()
                compile(helper['script'], helper['filename'], 'exec')
                log('interrupt ok: new query delivered, old results rejected, cancel helper compiled (%.3fs)' % (time.monotonic() - before))
                if '--selfnetwork' in sys.argv:
                    identity = {'agent': 'SelfTest', 'task_id': task['id']}
                    helper = local.get(server.url + '/api/network/helper', params=params, timeout=4).json()
                    compile(helper['script'], helper['filename'], 'exec')
                    attempts = []
                    for target in task['network']['targets']:
                        probe = local.post(server.url + '/api/network/probe', params=params,
                                           json={**identity, 'url': target}, timeout=100)
                        probe.raise_for_status()
                        data = probe.json()
                        attempts.append({'url': target, 'route': 'relay', 'ok': True,
                                         'status': data['status'], 'receipt': data['receipt']})
                    report = local.post(server.url + '/api/network/report', params=params,
                                        json={**identity, 'check_id': task['network']['check_id'], 'attempts': attempts}, timeout=4)
                    report.raise_for_status()
                    if not report.json().get('usable'):
                        raise RuntimeError('网络检测报告未通过')
                    app.processEvents()
                    source = next(s for s in cfg.enabled_sites() if s.builtin == 'youtube')
                    candidates = local.post(server.url + '/api/network/search', params=params,
                                            json={**identity, 'source_id': source.id, 'query': 'nature scenery'}, timeout=100)
                    candidates.raise_for_status()
                    rows = candidates.json()['results']
                    if not rows:
                        raise RuntimeError('中转搜索没有候选结果')
                    local.post(server.endpoint(), json={**identity, 'done': False,
                        'candidate_ids': [r['candidate_id'] for r in rows[:3]]}, timeout=3).raise_for_status()
                    app.processEvents()
                    log('network ok: helper compiled, relay HTTPS verified, report accepted, YouTube candidates=%d' % len(rows))
                    if '--selfmedia' in sys.argv:
                        deadline = time.monotonic() + 35
                        while time.monotonic() < deadline and not all(c.has_cover for c in win._cards[:3]):
                            time.sleep(.05); app.processEvents()
                        if len(win._cards) < 3 or not all(c.has_cover for c in win._cards[:3]):
                            raise RuntimeError('YouTube 封面未显示')
                        log('media ok: 3 YouTube covers decoded and displayed')
                agent_shot = _arg_value(sys.argv, '--selfagentshot')
                if agent_shot:
                    win.agent_details.show()
                    win._tick_agent_bar()
                    app.processEvents()
                    win.grab().save(agent_shot)
                result = local.post(server.endpoint(), json={"task_id": task["id"],
                                    "agent": "SelfTest", "done": True, "results": []}, timeout=3)
                result.raise_for_status()
                if result.json().get('completion_pending'):
                    log('agent empty/unverified completion correctly refused; supplying terminal-page fixtures for lifecycle smoke')
                    for site in server.access.sources():
                        server.access.search_evidence.record(task['id'],
                            dict(source_id=site.id, query='lifecycle-fixture', page=1),
                            dict(results=[], pagination=dict(exhausted=True, supported=True)))
                    # Only the lifecycle smoke uses these fixtures. --selfworker
                    # separately verifies actual websites and supplied workers.
                    for row in server.access.search_evidence.rows.values():
                        row['exhausted'] = True
                    result = local.post(server.endpoint(), json={'task_id':task['id'],
                        'agent':'SelfTest','done':True,'results':[]},timeout=3)
                    result.raise_for_status()
                app.processEvents()
                if win._agent_busy or server.task_status()["state"] != "completed":
                    raise RuntimeError("收尾后仍然显示搜索中")
                log("agent ok: connect -> confirm -> poll -> input enabled; queued -> delivered -> working -> completed (real local HTTP)")
                win.search_edit.setText('搜索国内的视频')
                win._start_search()
                task = local.get(server.url+'/api/task', params=params, timeout=3).json()['task']
                if task['network']['required'] or task['network']['targets']:
                    raise RuntimeError('国内请求仍触发外网检测')
                local.post(server.url+'/api/ack', params=params,
                           json={'agent':'SelfTest', 'task_id':task['id']}, timeout=3).raise_for_status()
                app.processEvents()
                win.agent_disconnect_btn.click()
                old = local.get(server.url+'/api/task', params=params, timeout=3)
                if (old.status_code != 200 or not old.json().get('stop')
                        or not old.json().get('session_ended') or old.json().get('next') != 'stop'
                        or old.json().get('retryable') is not False or old.json().get('task', {}).get('kind') != 'stop'
                        or not server.running or win.search_edit.isEnabled()):
                    raise RuntimeError('断开 Agent 未撤销旧凭据、锁定输入或保留收件口')
                old.raise_for_status()
                log('terminal stop ok: HTTP 200, task.kind=stop, session_ended=true, next=stop, retryable=false')
                params['token'] = server.token
                handshake = local.get(server.url+'/api/task', params=params, timeout=3).json()['task']
                local.post(server.url+'/api/connect', params=params,
                           json={'agent':'SelfTest', 'challenge':handshake['challenge'], 'ok':True}, timeout=3).raise_for_status()
                local.get(server.url+'/api/task', params=params, timeout=3).raise_for_status()
                win._tick_agent_bar()
                if not win.search_edit.isEnabled():
                    raise RuntimeError('断开后无法重新接入')
                log('routing/disconnect ok: domestic skips foreign checks; button revokes old token; listener stays open; same Agent reconnects')
                if '--selfpause' in sys.argv:
                    win._handoff_to_agent('只搜国内暂停恢复自检')
                    original = local.get(server.url+'/api/task', params=params, timeout=3).json()['task']
                    local.post(server.url+'/api/ack', params=params,
                               json={'agent': 'SelfTest', 'task_id': original['id']}, timeout=3).raise_for_status()
                    win._toggle_agent_pause()
                    for _ in range(3):
                        started = time.monotonic()
                        reply = local.get(server.url+'/api/task', params={**params, 'wait': .4}, timeout=3).json()
                        if time.monotonic() - started < .35 or not reply.get('paused') or reply.get('task'):
                            raise RuntimeError('暂停期间未保持长轮询或错误发放任务')
                    win._toggle_agent_pause()
                    if server.current_task or win.search_edit.isEnabled() or win._agent_busy:
                        raise RuntimeError('继续搜索未等待新轮询确认')
                    local.get(server.url+'/api/task', params=params, timeout=3).raise_for_status()
                    win._tick_agent_bar()
                    task = local.get(server.url+'/api/task', params=params, timeout=3).json()['task']
                    if not task or task['id'] == original['id']:
                        raise RuntimeError('恢复后未创建新任务')
                    win._toggle_agent_pause()
                    server._get_times['SelfTest'] = time.time() - 46
                    server._poll_times['SelfTest'] = time.time() - 46
                    control = local.get(server.url+'/api/control', params={**params, 'task_id': task['id']}, timeout=3).json()
                    win._tick_agent_bar()
                    if server.connected() or '暂未收到' not in win.agent_state.text() or not server.session_ready():
                        raise RuntimeError('暂停静默没有标记状态待确认或错误撤销会话')
                    shot = _arg_value(sys.argv, '--selfpauseshot')
                    if shot:
                        win.agent_details.show()
                        app.processEvents()
                        win.grab().save(shot)
                    local.get(server.url+'/api/task', params=params, timeout=3).raise_for_status()
                    win._toggle_agent_pause()
                    local.get(server.url+'/api/task', params=params, timeout=3).raise_for_status()
                    win._tick_agent_bar()
                    task = local.get(server.url+'/api/task', params=params, timeout=3).json()['task']
                    if not task or not win.search_edit.isEnabled():
                        raise RuntimeError('失联后重新取任务不能恢复搜索')
                    local.post(server.endpoint(), json={'agent': 'SelfTest', 'task_id': task['id'],
                               'done': True, 'results': []}, timeout=3).raise_for_status()
                    app.processEvents()
                    log('pause ok: 3 paused long polls; resume waits for fresh poll; 46s silence marks unconfirmed without revoking session; stale control cannot refresh; reconnect resumes')
                if '--selfliveness' in sys.argv:
                    from PySide6.QtWidgets import QPushButton
                    if any(b.text() == '重新派发' for b in win.findChildren(QPushButton)):
                        raise RuntimeError('重新派发按钮未移除')
                    win.settings.show_thumbnails = False
                    win._handoff_to_agent('只搜国内连接状态自检')
                    task = local.get(server.url+'/api/task', params=params, timeout=3).json()['task']
                    server._get_times['SelfTest'] = time.time() - 600
                    rows = [{'title': '连接状态测试 %d' % i, 'url': 'https://example.com/video/%d' % i}
                            for i in range(100)]
                    reply = local.post(server.endpoint(), json={'agent': 'SelfTest', 'task_id': task['id'],
                        'results': rows, 'done': False}, timeout=3)
                    reply.raise_for_status()
                    app.processEvents()
                    win._tick_agent_bar()
                    if not reply.json().get('limit_reached') or not server.connected() or len(win._cards) != 100:
                        raise RuntimeError('收满 100 条后错误忽略新结果活动')
                    server._poll_times['SelfTest'] = time.time() - 46
                    win._tick_agent_bar()
                    if server.current_task['id'] != task['id'] or not win.search_edit.isEnabled():
                        raise RuntimeError('静默时错误撤销任务或禁用排队输入')
                    if '本批已完成' not in win.agent_state.text():
                        raise RuntimeError('满额静默时没有正确提示等待轮询')
                    win._load_agent_batch()
                    win._tick_agent_bar()
                    following = server.current_task
                    if following['want'] != 100 or len(following['exclude']) != 100 or win.side_busy_spinner._timer.isActive():
                        raise RuntimeError('等待 Agent 时不能按用户请求排队下一批或错误显示搜索中')
                    delivered = local.get(server.url+'/api/task', params=params, timeout=3).json()['task']
                    if delivered['id'] != following['id']:
                        raise RuntimeError('Agent 恢复轮询后未领取保留任务')
                    local.post(server.url+'/api/ack', params=params,
                               json={'agent': 'SelfTest', 'task_id': delivered['id']}, timeout=3).raise_for_status()
                    if '--selfloading' in sys.argv:
                        app.processEvents()
                        win._tick_agent_bar()
                        if (not win.more_loading.isVisible() or not win.more_spinner._timer.isActive()
                                or win.more_label.text() != '正在加载更多资源…'):
                            raise RuntimeError('Agent 续搜底部动画或提示未显示')
                        shot = _arg_value(sys.argv, '--selfloadingshot')
                        if shot:
                            win.grab().save(shot)
                    local.post(server.endpoint(), json={'agent': 'SelfTest', 'task_id': delivered['id'],
                               'results': [{'title': '续搜完成 %d' % i, 'url': 'https://example.com/video/%d' % i}
                                           for i in range(100, 200)], 'done': True}, timeout=3).raise_for_status()
                    app.processEvents()
                    if '--selfloading' in sys.argv:
                        if not win.more_loading.isHidden() or win.more_spinner._timer.isActive():
                            raise RuntimeError('续搜完成后底部动画未停止')
                        log('loading footer ok: real HTTP continuation ACK animates, completion hides and stops timer')
                    log('liveness ok: 10-minute-old GET + 100 fresh results stays online; silence preserves batch; next 100 queued on demand and delivered on return; retry button absent')
                if '--selfmedia' in sys.argv:
                    win.settings.show_thumbnails = True
                    win._handoff_to_agent('国内封面自检')
                    task = local.get(server.url+'/api/task', params=params, timeout=3).json()['task']
                    identity = {'agent': 'SelfTest', 'task_id': task['id']}
                    source = next(s for s in cfg.enabled_sites() if s.builtin == 'bilibili')
                    batches = []
                    for page in (1, 2):
                        reply = local.post(server.url+'/api/network/search', params=params,
                            json={**identity, 'source_id': source.id, 'query': '终末地 小陈', 'page': page}, timeout=100)
                        reply.raise_for_status()
                        batch = reply.json()
                        batches.append(batch)
                    rows = batches[0]['results']
                    if len(rows) < 2:
                        raise RuntimeError('B站自检候选不足')
                    reply = local.post(server.endpoint(), json={**identity, 'done': False,
                        'candidate_ids': [rows[0]['candidate_id']],
                        'results': [{'title': rows[1]['title'], 'url': rows[1]['url'], 'source': '哔哩哔哩'}]}, timeout=3)
                    reply.raise_for_status()
                    app.processEvents()
                    deadline = time.monotonic() + 35
                    while time.monotonic() < deadline and not all(c.has_cover for c in win._cards[:2]):
                        time.sleep(.05); app.processEvents()
                    if len(win._cards) != 2 or not all(c.has_cover for c in win._cards):
                        raise RuntimeError('B站原始或自动补全封面未显示')
                    import json
                    full_size = len(json.dumps(rows, ensure_ascii=False).encode())
                    ids_size = len(json.dumps([r['candidate_id'] for r in rows]).encode())
                    log('media ok: Bilibili original + missing cover recovered and displayed; page ms=%s; metadata bytes=%d -> ids bytes=%d'
                        % ([b['elapsed_ms'] for b in batches], full_size, ids_size))
                    shot = _arg_value(sys.argv, '--selfmediashot')
                    if shot:
                        win.grab().save(os.path.abspath(shot))
                    local.post(server.endpoint(), json={**identity, 'done': True, 'results': []}, timeout=3).raise_for_status()
                    app.processEvents()
                if '--selffill' in sys.argv:
                    win.settings.show_thumbnails = False
                    def fixture_rows(start, count):
                        return [{'title': '数量验证 %d' % i, 'url': 'https://example.com/fill/%d' % i}
                                for i in range(start, start + count)]
                    def submit_fill(task, rows):
                        response = local.post(server.endpoint(), json={'agent': 'SelfTest',
                            'task_id': task['id'], 'results': rows, 'done': True}, timeout=5)
                        response.raise_for_status()
                        app.processEvents()
                        return response.json()
                    def wait_fill(previous):
                        deadline = time.monotonic() + 6
                        while time.monotonic() < deadline:
                            app.processEvents()
                            if server.current_task and server.current_task['id'] != previous['id']:
                                return local.get(server.url+'/api/task', params=params, timeout=3).json()['task']
                            time.sleep(.05)
                        raise RuntimeError('不足目标后未自动补足')
                    win.agent_budget_combo.setCurrentIndex(0)
                    win._handoff_to_agent('国内数量验证')
                    task = local.get(server.url+'/api/task', params=params, timeout=3).json()['task']
                    if task['want'] != 100:
                        raise RuntimeError('限制模式首个目标不是 100')
                    submit_fill(task, fixture_rows(0, 55))
                    task = wait_fill(task)
                    if task['want'] != 45 or len(task['exclude']) != 55:
                        raise RuntimeError('55 条之后未准确补足 45 条')
                    reply = submit_fill(task, fixture_rows(0, 110))
                    if reply['accepted'] != 45 or len(win._all_results) != 100 or win._auto_more_timer.isActive():
                        raise RuntimeError('限制模式未在去重后 100 条停止')
                    log('fill ok: limited 55 -> automatic remaining 45; duplicates excluded; stops at 100')
                    win.agent_budget_combo.setCurrentIndex(1)
                    win._handoff_to_agent('国内无限数量验证')
                    task = local.get(server.url+'/api/task', params=params, timeout=3).json()['task']
                    if task['want'] != 1000:
                        raise RuntimeError('无限模式首个目标不是 1000')
                    append_rows = win._append_results
                    try:
                        # Exercise all 1000 HTTP results without 1000 test-only cover widgets.
                        win._append_results = lambda fresh: win._all_results.extend(fresh)
                        submit_fill(task, fixture_rows(0, 55))
                        task = wait_fill(task)
                        if task['want'] != 945:
                            raise RuntimeError('无限模式 55 条之后未补足 945 条')
                        reply = submit_fill(task, fixture_rows(55, 1000))
                    finally:
                        win._append_results = append_rows
                    if reply['accepted'] != 945 or win._agent_budget_used != 1000 or win._auto_more_timer.isActive():
                        raise RuntimeError('无限模式未在累计 1000 条停止')
                    log('fill ok: unlimited starts at 1000; 55 -> automatic remaining 945; hard stop at 1000')
                if '--selfbudget' in sys.argv:
                    win.agent_budget_combo.setCurrentIndex(0)
                    win.settings.show_thumbnails = False  # Synthetic budget URLs have no covers.
                    win._handoff_to_agent('国内额度自检')
                    task = local.get(server.url+'/api/task', params=params, timeout=3).json()['task']
                    fixture = [{'title':'额度自检 %d' % n, 'url':'https://example.com/selftest/%d' % n} for n in range(105)]
                    reply = local.post(server.endpoint(), json={'agent':'SelfTest', 'task_id':task['id'],
                        'results':fixture, 'done':False}, timeout=3).json()
                    app.processEvents()
                    if reply.get('accepted') != 100 or not reply.get('limit_reached') or win._auto_more_timer.isActive() or len(win._all_results) != 100:
                        raise RuntimeError('限制模式未按 100 条停止')
                    from PySide6.QtCore import QPoint, QPointF, Qt
                    from PySide6.QtGui import QWheelEvent
                    bar = win.scroll.verticalScrollBar()
                    bar.setValue(bar.maximum())
                    event = QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(0, -20), QPoint(),
                                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                        Qt.ScrollPhase.ScrollUpdate, False)
                    app.sendEvent(win.scroll.viewport(), event)
                    for _ in range(3):
                        app.processEvents(); time.sleep(.01)
                    new = local.get(server.url+'/api/task', params=params, timeout=3).json()['task']
                    if not new or new['id'] == task['id'] or new['want'] != 100:
                        raise RuntimeError('触控板向下滚动触底未加载下一批')
                    win.agent_budget_combo.setCurrentIndex(1)
                    server.clear_tasks(); win._agent_busy_done()
                    win._agent_budget_used = 995  # Near-cap fixture; full 1000 stream is covered in budget_test.py.
                    win._agent_fill_pending = True
                    win._auto_agent_more()
                    task = local.get(server.url+'/api/task', params=params, timeout=3).json()['task']
                    if task['want'] != 5:
                        raise RuntimeError('无限模式剩余额度未限制派发数量')
                    fixture = [{'title':'额度自检新资源 %d' % n, 'url':'https://example.com/selftest/new/%d' % n} for n in range(10)]
                    reply = local.post(server.endpoint(), json={'agent':'SelfTest','task_id':task['id'],
                        'results':fixture,'done':False},timeout=3).json()
                    app.processEvents()
                    if reply.get('accepted') != 5 or win._agent_budget_used != 1000 or win._auto_more_timer.isActive():
                        raise RuntimeError('无限模式 1000 条上限失败')
                    win._load_agent_batch()
                    if local.get(server.url+'/api/task', params=params,timeout=3).json()['task'] is not None:
                        raise RuntimeError('达到 1000 条后仍然派发任务')
                    log('budget ok: limited trims to 100; pixel-only bottom scroll loads next 100; unlimited stops at 1000')
                    # Frozen-build acceptance for the reported 16/100 empty-page
                    # loop. Only upstream page bodies are synthetic; HTTP, task
                    # evidence, completion and UI dispatch are the installed code.
                    win.agent_budget_combo.setCurrentIndex(0)
                    for site in win.config.sites:
                        site.enabled = site.id == 'bili'
                    win._handoff_to_agent('只搜B站测试')
                    task = local.get(server.url+'/api/task', params=params, timeout=3).json()['task']
                    original_search = server.access.search
                    try:
                        def empty_page_fixture(data, cancel=None):
                            return dict(ok=True, results=fixture[:16] if data['page']==1 else [],
                                pagination=dict(page=data['page'], exhausted=False, supported=True))
                        fixture = [{'title':'空页停止自检 %d'%n, 'url':'https://www.bilibili.com/video/BVbounded%d'%n} for n in range(16)]
                        server.access.search = empty_page_fixture
                        for page in range(1,5):
                            response = local.post(server.url+'/api/network/search', params={'token':server.token},
                                json=dict(agent='SelfTest', task_id=task['id'], source_id='bili', query='cats', page=page), timeout=3).json()
                            if not response.get('ok'):
                                raise RuntimeError('受限收尾自检搜索失败: %s'%response)
                            if page==1:
                                local.post(server.endpoint(), json=dict(agent='SelfTest', task_id=task['id'],
                                    candidate_ids=[r['candidate_id'] for r in response['results']], done=False), timeout=3).raise_for_status()
                                app.processEvents()
                    finally:
                        server.access.search = original_search
                    reply = local.post(server.endpoint(), json=dict(agent='SelfTest', task_id=task['id'],
                        done=True, exhausted=True, message='连续空页自检'), timeout=3).json()
                    app.processEvents()
                    if not reply.get('done') or not reply.get('search_limited') or reply.get('exhausted') or reply.get('received')!=16:
                        raise RuntimeError('16/100连续空页不能正确受限收尾: %s'%reply)
                    if win._agent_busy or win._auto_more_timer.isActive() or len(win._all_results)!=16 or '本轮已停止' not in win.agent_budget_label.text():
                        raise RuntimeError('受限收尾界面仍忙碌/自动补足/丢失已有结果')
                    log('bounded completion ok: real HTTP 16/100 + 3 empty pages -> done=true, search_limited=true, exhausted=false; UI retains 16 and stops automatic refill')
                    win.agent_budget_combo.setCurrentIndex(1)
                    task = local.get(server.url+'/api/task',params=params,timeout=3).json()['task']
                    if not task or task['want']!=984:
                        raise RuntimeError('提高额度后未按16条已有结果续搜984条')
                    win.agent_budget_combo.setCurrentIndex(0)
                    state = local.get(server.url+'/api/control',params=dict(params,task_id=task['id'],budget_revision=1),timeout=3).json()
                    if state['cancelled'] or not state['budget_changed'] or state['remaining']!=84 or state['current_task_id']!=task['id']:
                        raise RuntimeError('实时降低额度未保留原任务/原计数')
                    fixture=[dict(title='mode fixture',url='https://example.com/mode/%d'%n) for n in range(100)]
                    reply=local.post(server.endpoint(),json=dict(agent='SelfTest',task_id=task['id'],results=fixture),timeout=3).json()
                    app.processEvents()
                    if reply['accepted']!=84 or not reply['done'] or not reply['keep_polling'] or reply['session_ended']:
                        raise RuntimeError('新额度未强制执行或误结束接管')
                    win.agent_budget_combo.setCurrentIndex(1)
                    task=local.get(server.url+'/api/task',params=params,timeout=3).json()['task']
                    if task['want']!=900:raise RuntimeError('已完成100条后未自动派发900条缺额')
                    fixture=[dict(title='pending mode fixture',url='https://example.com/pending-mode/%d'%n) for n in range(50)]
                    local.post(server.endpoint(),json=dict(agent='SelfTest',task_id=task['id'],results=fixture),timeout=3).raise_for_status()
                    win.agent_budget_combo.setCurrentIndex(0)  # Deliberately before processing Qt results.
                    app.processEvents()
                    if win._agent_budget_used!=150 or len(win._all_results)!=150 or win._agent_busy or not server.session_ready():
                        raise RuntimeError('超过100后切换丢失迟到卡片或错误断开接管')
                    log('live mode ok: 16 -> unlimited remaining984 -> same task limited remaining84 -> total100 -> unlimited remaining900 -> received150 switches to limited complete; all150 retained, session kept')
        if '--selflogin' in sys.argv:
            from app.selfcheck_login import run as check_login
            check_login(win, app, log, _arg_value(sys.argv, '--selfloginshot'))
        if '--selfaudit' in sys.argv:
            import threading
            from PySide6.QtCore import Qt
            from app.models import SearchResult, SiteEvent
            from app.agent import AgentServer
            from app.ui.dialogs import SettingsDialog
            win._set_mode('direct')
            win._clear_results()
            win.settings.show_thumbnails = False
            win._query = 'audit fixture'
            win._append_results([SearchResult(title='keep', url='https://www.bilibili.com/video/BVkeep')])
            win._paging = True
            old_gen, old_cancel = win._gen, win._cancel
            win.site_list.item(0).setCheckState(Qt.CheckState.Unchecked)
            win._on_event(old_gen, SiteEvent(kind='done', site_id='bili', results=[
                SearchResult(title='late', url='https://www.bilibili.com/video/BVlate')]))
            if not old_cancel.is_set() or win._paging or len(win._all_results) != 1:
                raise RuntimeError('站点修改后的任务取消或迟到回调拦截失败')
            win._searching = win._restart_after_stop = True
            win._finish_search()
            win._clear_results()
            app.processEvents()
            if win._searching or win._restart_after_stop or win._query:
                raise RuntimeError('清空后意外恢复了待刷新任务')
            log('audit ok: sidebar cancels paging; late callbacks rejected; clear cancels scheduled refresh')

            dialog = SettingsDialog(win.settings, win.http, parent=win)
            dialog.show()
            dialog._refresh_net()
            deadline = time.monotonic() + 35
            while dialog._network_running and time.monotonic() < deadline:
                time.sleep(.05)
                app.processEvents()
            if dialog._network_running or '国内 ' not in dialog.diag_label.text():
                raise RuntimeError('设置网络实测失败：' + dialog.diag_label.text())
            log('audit network: ' + dialog.diag_label.text().replace('\n', '; '))
            dialog.reject()
            dialog.deleteLater()

            listener = AgentServer(lambda *a: None, port=0)
            listener.start()
            replies, failures = [], []
            old_token = listener.token
            def idle_poll():
                try:
                    with requests.Session() as client:
                        client.trust_env = False
                        replies.append(client.get(listener.url + '/api/task', params={
                            'token': old_token, 'agent': 'audit-close', 'wait': 20}, timeout=3).json())
                except Exception as exc:
                    failures.append(str(exc))
            poller = threading.Thread(target=idle_poll, daemon=True)
            poller.start()
            try:
                deadline = time.monotonic() + 2
                while not listener._waiting_polls.get('audit-close') and time.monotonic() < deadline:
                    time.sleep(.01)
                listener.stop()
                poller.join(1)
                if poller.is_alive() or failures or not replies or not replies[0].get('stop'):
                    raise RuntimeError('关闭收件口未及时送达终止消息：' + repr(failures))
                if replies[0].get('next') != 'stop' or replies[0].get('task', {}).get('kind') != 'stop':
                    raise RuntimeError('关闭消息仍会让 Agent 继续轮询')
                log('audit ok: closing listener wakes HTTP long poll with terminal stop')
            finally:
                listener.stop()
            win.settings.show_thumbnails = True
            win._append_results([SearchResult(title='pending close fixture',
                url='https://example.com/video', cover='https://example.com/image.png')])
        win.close()
        if '--selfaudit' in sys.argv:
            from PySide6.QtCore import QCoreApplication, QEvent
            win.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            win._pump_thumbs()
            log('audit close ok: queued thumbnail callback ignores destroyed window')
        app.processEvents()
        log("RESULT: PASS")
    except Exception:
        ok = False
        log("RESULT: FAIL")
        log(traceback.format_exc())

    text = "\n".join(lines)
    if out_path:
        try:
            with io.open(out_path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
    return 0 if ok else 1


def main() -> int:
    if '--update-healthcheck' in sys.argv:
        from app.config import APP_VERSION
        from app.updates import verify_stage
        from PySide6.QtWidgets import QApplication
        from app.ui.mainwindow import MainWindow
        if _arg_value(sys.argv,'--update-healthcheck')!=APP_VERSION:return 2
        verify_stage(os.path.dirname(sys.executable),APP_VERSION,allow_user_data=True)
        application=QApplication([])
        return 0
    _fix_taskbar_icon()
    if "--selftest" in sys.argv:
        try:
            from app.config import app_dir

            default_out = os.path.join(app_dir(), "selftest.txt")
        except Exception:
            default_out = os.path.join(HERE, "selftest.txt")
        return _run_selftest(_arg_value(sys.argv, "--selfout", default_out))

    from app.ui.mainwindow import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
