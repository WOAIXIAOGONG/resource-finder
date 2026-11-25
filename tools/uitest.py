# -*- coding: utf-8 -*-
"""界面级自动化验收：真实创建 MainWindow，模拟点击、复制、切换主题。

    python tools/uitest.py            # 全部用例（含联网搜索）
    python tools/uitest.py --offline  # 只跑不联网的部分

用临时目录当配置目录，不会污染你自己的白名单/黑名单。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import time
import traceback

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="rf-uitest-")
os.environ["RESOURCE_FINDER_HOME"] = _TMP

from PySide6.QtCore import Qt                              # noqa: E402
from PySide6.QtGui import QGuiApplication                  # noqa: E402
from PySide6.QtTest import QTest                           # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox    # noqa: E402

RESULTS = []
SHOTS = os.path.join(HERE, "shots")


def check(name, fn):
    t0 = time.time()
    try:
        detail = fn() or ""
        RESULTS.append((True, name))
        print("  [OK]   %-28s %5.2fs  %s" % (name, time.time() - t0, detail))
    except Exception as exc:                       # noqa: BLE001
        RESULTS.append((False, name))
        print("  [FAIL] %-28s %5.2fs  %s" % (name, time.time() - t0, str(exc)[:200]))
        if os.environ.get("RF_UITEST_TRACE"):
            traceback.print_exc()


def wait_until(cond, timeout=90.0, step=0.2):
    """轮询等待。

    注意：这里必须用 time.sleep 而不是 QTest.qWait —— qWait 在循环里会不停
    处理事件、抓 GIL，把后台搜索线程饿死（实测一次搜索从 7s 拖到 40s+）。
    """
    app = QApplication.instance()
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
        if app is not None:
            app.processEvents()
    return False


def shot(widget, name):
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, name)
    widget.grab().save(path)
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("-q", "--query", default="风景")
    args = ap.parse_args()

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("ResourceFinderTest")

    # 拦掉所有弹窗，记录内容，避免测试卡住
    popups = []

    def _warn(parent, title, text, *a, **k):
        popups.append((title, text))
        return QMessageBox.StandardButton.Ok

    def _info(parent, title, text, *a, **k):
        popups.append((title, text))
        return QMessageBox.StandardButton.Ok

    QMessageBox.warning = staticmethod(_warn)      # type: ignore[assignment]
    QMessageBox.information = staticmethod(_info)  # type: ignore[assignment]
    QMessageBox.critical = staticmethod(_warn)     # type: ignore[assignment]

    from app import config as app_config
    from app.config import Config
    from app.ui import theme as ui_theme
    from app.ui.mainwindow import MainWindow
    # Existing component/adapter checks retain the legacy queue fixture.
    # tools/regression.py also runs access_test's real 3.0 HTTP and UI integration checks.
    import app.ui.mainwindow as mw_module
    from app.agent import AgentServer
    mw_module.AgentServer = lambda *a, **kw: AgentServer(
        *a, **{k: v for k, v in kw.items() if k != 'config'})

    app.setStyleSheet(ui_theme.stylesheet("dark"))
    startup_check = MainWindow._startup_checks
    if args.offline:
        MainWindow._startup_checks = lambda self: None
    win = MainWindow(Config())
    win.resize(1440, 900)
    win.show()
    QTest.qWait(400)

    print("\n[启动]")
    check("主窗口创建", lambda: "标题=%s" % win.windowTitle())
    check("白名单包含默认站点",
          lambda: _expect(len(win.config.sites) == len(app_config.default_sites()),
                          "实际 %d 个：%s" % (len(win.config.sites),
                                             [s.name for s in win.config.sites])))
    check("管理窗口保留默认站点", lambda: _expect(win.site_list.count() == len(app_config.default_sites()) and not win.site_list.isVisible(),
                                          "实际 %d 行" % win.site_list.count()))
    check("空状态可见", lambda: _expect(win.stack.currentIndex() == 0, "stack=%d" % win.stack.currentIndex()))

    print("\n[空关键词 / 黑名单]")

    def _empty_query():
        win.search_edit.setText("   ")
        before = win._searching
        win._start_search()
        return _expect(not win._searching and not before, "状态栏=%s" % win.status_label.text())

    check("空关键词不搜索", _empty_query)

    def _black_block():
        win.config.add_blackword("测试禁词")
        win.search_edit.setText("测试禁词")
        popups.clear()
        win._start_search()
        return _expect(bool(popups) and not win._searching,
                       "弹窗=%s" % (popups[0][0] if popups else "无"))

    check("黑名单拦截关键词", _black_block)

    def _black_bad_word_kept():
        win.config.remove_blackword("测试禁词")
        return _expect("测试禁词" not in win.config.blacklist, "已移除")

    check("黑名单可删除", _black_bad_word_kept)

    def _builtin_editable():
        from app.config import builtin_sites
        from app.ui.dialogs import SiteDialog

        bili = builtin_sites()[0]
        dlg = SiteDialog(bili, win.http, args.query, win)
        enabled = dlg.url_edit.isEnabled() and dlg.advanced.isEnabled()
        url = dlg.url_edit.text()
        prefilled = "bilibili.com" in url
        hint = dlg._builtin_hint.isVisibleTo(dlg)
        dlg.deleteLater()
        return _expect(enabled and prefilled, "enabled=%s url=%s hint=%s"
                       % (enabled, url[:44], hint))

    check("内置站点地址可编辑", _builtin_editable)

    def _builtin_to_generic():
        from app.config import builtin_sites
        from app.ui.dialogs import SiteDialog

        bili = builtin_sites()[0]
        dlg = SiteDialog(bili, win.http, args.query, win)
        same = dlg._collect()
        dlg.deleteLater()
        dlg2 = SiteDialog(bili, win.http, args.query, win)
        dlg2.url_edit.setText("https://www.example.com/search?w={q}")
        changed = dlg2._collect()
        dlg2.deleteLater()
        ok = (same.kind == "builtin" and same.builtin == "bilibili"
              and changed.kind == "generic" and changed.builtin == ""
              and changed.search_url.startswith("https://www.example.com"))
        return _expect(ok, "保持=%s/%s 改后=%s/%s" % (same.kind, same.builtin,
                                                      changed.kind, changed.builtin))

    check("内置站点改地址后按通用抓取", _builtin_to_generic)

    print("\n[筛选 / 提示 / 清空]")

    def _seed_results():
        import time as _t
        from app.models import SearchResult

        now = _t.time()
        rows = [
            SearchResult(title="新视频", url="https://v.example.com/new", source="哔哩哔哩",
                         source_id="bili", views=1000, pub_ts=now - 3600),
            SearchResult(title="老视频", url="https://v.example.com/old", source="哔哩哔哩",
                         source_id="bili", views=900000, pub_ts=now - 400 * 86400),
            SearchResult(title="中等视频", url="https://v.example.com/mid", source="YouTube",
                         source_id="ytb", views=50000, pub_ts=now - 3 * 86400),
            SearchResult(title="没日期", url="https://v.example.com/nodate", source="必应网页",
                         source_id="bing", views=0, pub_ts=0),
        ]
        win._clear_results(keep_empty=True)
        win._add_cards("bili", rows[:2])
        win._add_cards("ytb", rows[2:3])
        win._add_cards("bing", rows[3:])
        return "卡片=%d" % len(win._cards)

    check("载入筛选样本", _seed_results)

    def _filter_time():
        win.time_combo.setCurrentIndex(1)          # 24 小时内
        titles = [c.result.title for c in win._cards]
        exists = "没日期" not in titles and "新视频" in titles
        win.time_combo.setCurrentIndex(0)
        return _expect(exists, "筛选后=%s" % titles)

    check("按时间筛选", _filter_time)

    def _filter_views():
        win.views_combo.setCurrentIndex(1)         # 1 万以上
        titles = sorted(c.result.title for c in win._cards)
        exists = "老视频" in titles and "中等视频" in titles and "新视频" not in titles
        win.views_combo.setCurrentIndex(0)
        return _expect(exists, "筛选后=%s" % titles)

    check("按播放量筛选", _filter_views)

    def _sort_views():
        win.sort_combo.setCurrentIndex(2)          # 播放最多
        order = [c.result.title for c in win._cards]
        win.sort_combo.setCurrentIndex(0)
        return _expect(order[0] == "老视频", "顺序=%s" % order)

    check("按播放量排序", _sort_views)

    def _copy_toast():
        from PySide6.QtWidgets import QApplication as _QApp
        win._copy_link("https://v.example.com/new")
        QTest.qWait(120)
        text = win._toast.label.text()
        clip = _QApp.clipboard().text()
        visible = win._toast.isVisible()
        win._toast.hide()
        return _expect(visible and "已复制" in text and clip.endswith("/new"),
                       "toast=%s clip=%s" % (text, clip))

    check("复制链接弹出提示", _copy_toast)

    def _clear_resets_empty():
        win._clear_results()
        QTest.qWait(120)
        title = win.empty.title.text()
        return _expect(win.stack.currentIndex() == 0 and "找到下一份灵感" in title,
                       "空状态=%s" % title)

    check("清空后回到初始空状态", _clear_resets_empty)

    # ---------------- Agent 模式（全部走 127.0.0.1，离线也能测） ---------------- #
    def _mode_switch():
        win._set_mode("agent")
        QTest.qWait(150)
        on = win.agent_bar.isVisible() and win.agent_btn.isChecked()
        text = win.search_btn.text()
        running = bool(win._agent_server and win._agent_server.running)
        win._set_mode("direct")
        QTest.qWait(120)
        off = (not win.agent_bar.isVisible()) and win.direct_btn.isChecked()
        return _expect(on and off and text == "AI 搜索" and running,
                       "agentBar=%s btn=%s server=%s" % (on, text, running))

    check("切换 Agent 模式", _mode_switch)

    def _agent_ping():
        import requests as _rq
        win._set_mode("agent")
        QTest.qWait(200)
        srv = win._agent_server
        _expect(srv is not None and srv.running, "收件口没起来")
        r = _rq.get(srv.url + "/api/ping", timeout=3)
        data = r.json()
        bad = _rq.post(srv.url + "/api/results", json={"results": []}, timeout=3)
        win._set_mode("direct")
        return _expect(r.status_code == 200 and data.get("ok") and bad.status_code == 403,
                       "ping=%s 无token=%s" % (r.status_code, bad.status_code))

    check("Agent 收件口可用且需 token", _agent_ping)

    def _agent_push():
        import requests as _rq
        win._clear_results()
        win._set_mode("agent")
        QTest.qWait(200)
        srv = win._agent_server
        win._handoff_to_agent("测试推送")
        payload = {"task_id": srv.current_task["id"], "done": True, "query": "测试推送", "results": [
            {"title": "Agent 推进来的测试视频", "url": "https://example.com/v/1",
             "source": "Agent", "duration": "3:21", "views": 12345},
            {"title": "没有链接的会被丢掉"},
        ]}
        r = _rq.post(srv.endpoint(), json=payload, timeout=3)
        got = wait_until(lambda: any(c.title.text().startswith("Agent 推进来")
                                     for c in win._cards), timeout=5)
        n = len(win._cards)
        win._set_mode("direct")
        return _expect(r.status_code == 200 and got and n == 1,
                       "http=%s 卡片=%d 结果=%s" % (r.status_code, n, r.json()))

    check("Agent 推送结果后立刻出卡片", _agent_push)

    def _intent():
        from app.agent import parse_intent
        data = parse_intent("帮我找最近一周播放量高一点的风景延时视频")
        kws = data.get("keywords") or []
        return _expect(kws[:1] == ["风景延时"] and data.get("time") == "7d",
                       "%s" % data)

    check("大白话解析（无本地模型时的规则回退）", _intent)

    def _agent_task():
        import requests as _rq
        win._set_mode("agent")
        QTest.qWait(200)
        srv = win._agent_server
        # 假装 Agent 刚刚来取过任务（connected() 靠最近一次轮询时间判断）
        srv.touch("测试 Agent")
        win.search_edit.setText("找几个最近的 4K 风景延时")
        win._start_search()
        QTest.qWait(300)
        task = srv.current_task or {}
        clip = QGuiApplication.clipboard().text()
        # 端口 /api/task 也应该能取到同一个任务
        r = _rq.get(srv.url + "/api/task", params={"token": srv.token, "wait": 1, "agent": srv.owner},
                    timeout=5)
        got = (r.json() or {}).get("task") or {}
        win._set_mode("direct")
        return _expect(task.get("query") == "找几个最近的 4K 风景延时"
                       and "/api/task" in clip
                       and got.get("id") == task.get("id"),
                       "task=%s 剪贴板含接口=%s 取回=%s"
                       % (task.get("id"), "/api/task" in clip, got.get("id")))

    check("Agent 模式下点搜索会派任务给 Agent", _agent_task)

    def _agent_blacklist():
        import requests as _rq
        win._clear_results()
        win.config.blacklist.append("禁词")
        win._set_mode("agent")
        QTest.qWait(200)
        srv = win._agent_server
        win._handoff_to_agent("黑名单测试")
        payload = {"task_id": srv.current_task["id"], "done": True, "query": "黑名单测试", "results": [
            {"title": "这条带 禁词 会被拦", "url": "https://example.com/bad/1"},
            {"title": "这条是干净的视频", "url": "https://example.com/ok/1"},
        ]}
        r = _rq.post(srv.endpoint(), json=payload, timeout=3)
        got = wait_until(lambda: any(c.title.text().startswith("这条是干净")
                                     for c in win._cards), timeout=5)
        titles = [c.title.text() for c in win._cards]
        blocked = [t for t in titles if "禁词" in t]
        win.config.blacklist.remove("禁词")
        win._set_mode("direct")
        return _expect(r.status_code == 200 and got and not blocked,
                       "卡片=%s" % titles)

    check("Agent 推来的结果也会被黑名单拦下", _agent_blacklist)

    def _agent_no_conn():
        """没有 Agent 接管时点搜索：必须弹窗，不许偷偷用关键词搜。"""
        win._clear_results()
        win._set_mode("agent")
        QTest.qWait(200)
        srv = win._agent_server
        srv._owner = ""; srv._poll_times.clear()            # 明确「没人接管」
        seen = {}

        class _Stub:
            PASTE, HANDOFF, LOCAL, CANCEL = 1, 2, 3, 0
            choice = 0

            def __init__(self, text, check_connected=None, task_id="", parent=None):
                seen["text"] = text
                seen["task_id"] = task_id
                self.choice = _Stub.choice

            def exec(self):
                return 0

        import app.ui.mainwindow as mw_mod
        original = mw_mod.AgentWaitDialog
        mw_mod.AgentWaitDialog = _Stub
        try:
            _Stub.choice = _Stub.PASTE     # 用户选择「去粘贴」
            win.search_edit.setText("随便说点什么")
            win._start_search()
            QTest.qWait(300)
            clip = QGuiApplication.clipboard().text()
            no_cards = len(win._cards) == 0
            queued = (srv.current_task or {}).get("query")
            _Stub.choice = _Stub.LOCAL     # 再来一次，选择本地搜索
            win._set_mode("agent")
            win.search_edit.setText("再随便说点什么")
            started = {}
            original_ai = win._ai_search

            def _fake_ai():
                started["local"] = True

            win._ai_search = _fake_ai
            try:
                win._start_search()
                QTest.qWait(200)
            finally:
                win._ai_search = original_ai
            cancelled = srv.current_task is None
        finally:
            mw_mod.AgentWaitDialog = original
            win._set_mode("direct")
        return _expect(no_cards and queued == "随便说点什么" and "/api/task" in clip
                       and started.get("local") and cancelled,
                       "卡片=%d 队列=%s 剪贴板含接口=%s 本地搜=%s 已撤任务=%s"
                       % (len(win._cards), queued, "/api/task" in clip,
                          bool(started.get("local")), cancelled))

    check("没接入 Agent 时点搜索会弹窗而不是偷偷搜", _agent_no_conn)

    def _agent_name():
        if not win.agent_mode:
            win._set_mode("agent")
        srv = win._agent_server
        if srv is None or not srv.running:
            win._start_agent_server()
            srv = win._agent_server
        if srv is None:
            raise AssertionError("收件口没起来")
        # 模拟 Codex CLI 来取任务：带 agent 名字
        threading.Thread(target=lambda: srv.pop_task(0.4, agent="Codex CLI"),
                         daemon=True).start()

        def _seen():
            win._tick_agent_bar()          # 不等 2s 定时器，直接刷
            return "Codex CLI" in win.agent_state.text()

        okay = wait_until(_seen, timeout=6)
        state = win.agent_state.text()
        srv.clear_tasks()
        return _expect(okay, "状态=%s" % state)

    check("Agent 接管时显示是谁", _agent_name)

    def _agent_busy():
        from app.models import SearchResult
        srv = win._agent_server
        srv.touch("测试 Agent")
        srv.clear_tasks()
        win._set_mode("agent")
        win._agent_busy_done()
        before = len(win._cards)
        win._handoff_to_agent("忙碌遮罩测试")
        task = srv.current_task or {}
        query_set = win._query == "忙碌遮罩测试"
        busy_on = win._agent_busy and win.side_busy.isVisible()
        disabled = win.search_btn.isEnabled()  # 换词时按钮必须可点
        win._on_agent_results({"results": [
            SearchResult(title="遮罩测试卡片", url="https://v.example.com/busy-test",
                         source="Agent", source_id="agent", author="测试")],
            "query": "忙碌遮罩测试", "meta": {"task_id": str(task.get("id") or "")}})
        busy_off = (not win._agent_busy) and (not win.side_busy.isVisible())
        enabled = win.search_btn.isEnabled()
        grew = len(win._cards) == 1
        srv.clear_tasks()
        win._agent_busy_done()
        win._set_mode("direct")            # 后面的搜索用例要在直接模式下跑
        return _expect(busy_on and disabled and busy_off and enabled and grew,
                       "忙碌=%s 按钮禁用=%s 结果后恢复=%s 按钮可用=%s 新卡片=%s"
                       % (busy_on, disabled, busy_off, enabled, grew))

    check("派任务后显示搜索遮罩，结果到达后收起", _agent_busy)

    def _agent_more():
        from app.models import SearchResult
        srv = win._agent_server
        if not win.agent_mode:
            win._set_mode("agent")
        srv.touch("测试 Agent")
        srv.clear_tasks()
        win._agent_busy_done()
        win._query = "更多测试"
        # 本批已满后触底才授权下一批；少于目标时只补足缺额。
        win._agent_budget_used = win._agent_grant_end = 100
        win._agent_batch_start = 0
        win._all_results = [
            SearchResult(title="已有 %d" % i, url="https://v.example.com/m%d" % i,
                         source="Agent", source_id="agent") for i in range(3)
        ]
        win._load_agent_batch()
        task = srv.current_task or {}
        ok = (task.get("kind") == "more" and int(task.get("want") or 0) == 100
              and len(task.get("exclude") or []) >= 3
              and win._agent_busy and win.side_busy.isVisible())
        detail = "kind=%s want=%s exclude=%d 忙碌=%s" % (
            task.get("kind"), task.get("want"), len(task.get("exclude") or []),
            win._agent_busy)
        srv.clear_tasks()
        win._agent_busy_done()
        win._set_mode("direct")            # 后面的搜索用例要在直接模式下跑
        return _expect(bool(ok), detail)

    check("Agent 模式滚到底会要更多（上限 100 条）", _agent_more)

    def _agent_stream():
        from app.models import SearchResult
        srv = win._agent_server
        if not win.agent_mode:
            win._set_mode("agent")
        srv.touch("测试 Agent")
        srv.clear_tasks()
        win._agent_busy_done()
        win._handoff_to_agent("流式测试")
        task = srv.current_task or {}
        tid = str(task.get("id") or "")
        before = len(win._cards)
        win._on_agent_results({
            "results": [SearchResult(title="流式之一", url="https://v.example.com/st-1",
                                     source="Agent", source_id="agent")],
            "query": "流式测试", "meta": {"task_id": tid, "done": False}})
        mid_busy = win._agent_busy and win.side_busy.isVisible()
        mid_sub = win.side_busy_sub.text()
        grew = len(win._cards) > before
        win._on_agent_results({
            "results": [SearchResult(title="流式之二", url="https://v.example.com/st-2",
                                     source="Agent", source_id="agent")],
            "query": "流式测试", "meta": {"task_id": tid, "done": True}})
        finished = not win._agent_busy and not win.side_busy.isVisible()
        srv.clear_tasks()
        win._agent_busy_done()
        win._set_mode("direct")
        ok = mid_busy and ("还在继续搜" in mid_sub) and grew and finished
        return _expect(ok, "过程忙碌=%s 文案=%s 新卡片=%s 收尾=%s"
                       % (mid_busy, mid_sub[:24], grew, finished))

    check("Agent 边搜边推：done:false 保持忙碌，done:true 收尾", _agent_stream)

    def _agent_pause():
        srv = win._agent_server
        if not win.agent_mode:
            win._set_mode("agent")
        who = srv.owner or '暂停测试 Agent'
        srv.touch(who)
        _expect(wait_until(lambda: not any(srv._waiting_polls.values()), timeout=3),
                '前一个用例的后台轮询未结束')
        win._query = ""                     # 避免「继续」时自动派 more
        win._agent_busy_done()
        win._toggle_agent_pause()
        paused = srv.paused and win.agent_pause_btn.text() == "继续"
        no_task = srv.pop_task(0.05, who) is None
        win._toggle_agent_pause()
        waiting = not win.search_edit.isEnabled() and srv._resume_pending
        srv.pop_task(0, who)
        win._tick_agent_bar()
        resumed = (not srv.paused) and win.agent_pause_btn.text() == "暂停" and win.search_edit.isEnabled()
        win._set_mode("direct")
        return _expect(paused and no_task and waiting and resumed,
                       "暂停=%s 停发任务=%s 等待新轮询=%s 继续=%s" % (paused, no_task, waiting, resumed))

    check("暂停 / 继续 Agent 搜索", _agent_pause)

    def _agent_interrupt():
        srv = win._agent_server
        if not win.agent_mode:
            win._set_mode("agent")
        srv.touch("测试 Agent")
        srv.clear_tasks()
        win._agent_busy_done()
        win._query = "旧关键词"
        win._handoff_to_agent("旧关键词")
        old_id = str((srv.current_task or {}).get("id") or "")
        win.search_edit.setText("新关键词")
        win._start_search()                  # 正在搜时换词，应当立刻改派
        task = srv.current_task or {}
        ok = (task.get("query") == "新关键词" and str(task.get("id")) != old_id
              and win._agent_busy)
        detail = "新任务=%s 旧任务=%s 忙碌=%s" % (task.get("query"), old_id, win._agent_busy)
        srv.clear_tasks()
        win._agent_busy_done()
        win.search_edit.clear()
        win._set_mode("direct")
        return _expect(ok, detail)

    check("Agent 搜索中换词会立刻改派新任务", _agent_interrupt)

    def _card_date():
        from app.models import SearchResult, short_date
        from app.ui.widgets import ResultCard
        now = time.time()
        r = SearchResult(title="日期角标测试", url="https://v.example.com/date-1",
                         source="B站", source_id="bili", pub_ts=now - 3 * 86400)
        card = ResultCard(r)
        shown = bool(card.date_badge.text()) and not card.date_badge.isHidden()
        label = card.date_badge.text()
        ok = shown and label == "3 天前" and short_date(now - 3 * 86400, now) == "3 天前"
        card.deleteLater()
        return _expect(ok, "角标=%r shown=%s" % (label, shown))

    check("封面左上角显示发布日期", _card_date)

    def _takeover():
        srv = win._agent_server
        srv.clear_tasks()
        srv._owner = ""
        srv._poll_times.clear()
        srv._seen_agents.clear()
        first = srv.touch("Agent 甲")
        second = srv.touch("Agent 乙")
        old_back = srv.touch("Agent 甲")          # 被顶掉的旧 Agent 不能再抢回去
        ok = (first and second and not old_back and srv.owner == "Agent 乙"
              and srv.epoch >= 1 and not srv.is_owner("Agent 甲"))
        detail = "甲=%s 乙=%s 甲回抢=%s 当前=%s epoch=%s" % (
            first, second, old_back, srv.owner, srv.epoch)
        srv._owner = ""
        srv._poll_times.clear()
        srv._seen_agents.clear()
        return _expect(ok, detail)

    check("新 Agent 接管后旧 Agent 退出", _takeover)

    def _new_query_clears():
        from app.models import SearchResult
        win._set_mode("agent")
        srv = win._agent_server
        srv.touch("测试 Agent")
        win._handoff_to_agent("旧关键词")
        win._on_agent_results({"results": [
            SearchResult(title="旧结果", url="https://v.example.com/old-1",
                         source="Agent", source_id="agent")],
            "query": "旧关键词", "meta": {"task_id": srv.current_task["id"], "done": True}})
        before = len(win._cards)
        win._agent_busy_done()
        win._handoff_to_agent("新关键词")
        after = len(win._cards)
        task = srv.current_task or {}
        ok = before >= 1 and after == 0 and task.get("query") == "新关键词"
        detail = "旧卡片=%d 换词后=%d 任务词=%s" % (before, after, task.get("query"))
        srv.clear_tasks()
        win._agent_busy_done()
        win._set_mode("direct")
        return _expect(ok, detail)

    check("换搜索词会清空旧结果", _new_query_clears)

    def _agent_error():
        captured = {}
        original = win._warn
        win._warn = lambda title, text: captured.update(title=title, text=text)
        try:
            win._set_mode("agent")
            win._handoff_to_agent("错误测试")
            win._on_agent_error({"task_id": win._agent_task_id, "agent": "WorkBuddy",
                                 "error": "审核不通过：不支持的搜索内容"})
        finally:
            win._warn = original
        ok = (bool(captured.get("title")) and "审核不通过" in str(captured.get("text"))
              and not win._agent_busy)
        return _expect(ok, "弹窗=%s / %s" % (captured.get("title"),
                                             str(captured.get("text"))[:40]))

    check("Agent 报错会弹提示并结束忙碌", _agent_error)

    def _startup_probe():
        import app.ui.mainwindow as mw_mod

        seen = {}

        def fake_probe(settings, timeout=4.0):
            seen["called"] = True
            return {"proxy": "", "proxy_ok": False, "domestic": True,
                    "abroad": False, "proxy_source": "none",
                    "system_proxy": "off", "route": "", "abroad_ms": 0.0,
                    "note": "启动自检（测试桩）"}

        original = mw_mod.probe_network
        mw_mod.probe_network = fake_probe
        try:
            startup_check(win)
            ok = wait_until(lambda: "国内" in win.net_badge.text(), timeout=5)
        finally:
            mw_mod.probe_network = original
        badge = win.net_badge.text().replace("\n", " / ")
        return _expect(bool(seen.get("called")) and ok,
                       "自动检测=%s 徽标=%s" % (bool(seen.get("called")), badge[:60]))

    check("启动时自动检测网络", _startup_probe)

    def _refresh_btn():
        count = {"n": 0}
        original = win._start_search

        def fake_start():
            count["n"] += 1

        win._start_search = fake_start
        try:
            win.search_edit.setText("刷新按钮测试")
            win.refresh_btn.click()
            win.search_edit.setText("")
            win.refresh_btn.click()          # 空关键词不应触发搜索
        finally:
            win._start_search = original
        return _expect(count["n"] == 1 and win.refresh_btn.isEnabled()
                       and win.refresh_btn.toolTip() != "",
                       "触发次数=%d 可用=%s" % (count["n"], win.refresh_btn.isEnabled()))

    check("搜索按钮旁的刷新按钮", _refresh_btn)

    if args.offline:
        print("\n[离线模式] 跳过联网用例")
        win.close()
        return _summary()

    print("\n[联网搜索：%s]" % args.query)
    win._set_mode("direct")

    def _search():
        win.search_edit.setText(args.query)
        win.progress.setValue(0)
        win._start_search()
        _expect(win._searching, "直接搜索未启动")
        ok = wait_until(lambda: not win._searching, timeout=120)
        _expect(ok, "等待搜索结束超时")
        return "%d 张卡片 · %s" % (len(win._cards), win.status_label.text())

    check("搜索并渲染卡片", _search)
    check("卡片数量 > 10", lambda: _expect(len(win._cards) > 10,
                                          "实际 %d 张" % len(win._cards)))
    check("状态栏站点计数正确",
          lambda: _expect("/%d 个站点" % win._total_sites in win.status_label.text(),
                          win.status_label.text()))
    check("侧栏出现 +N 标记",
          lambda: _expect("+" in win.site_list.item(0).text(),
                          win.site_list.item(0).text()))
    def _covers():
        # 封面是懒加载的：滚一下、再等几张下载回来
        win.scroll.verticalScrollBar().setValue(0)
        win._pump_thumbs()
        wait_until(lambda: sum(1 for c in win._cards if c.has_cover) > 3, timeout=25)
        n = sum(1 for c in win._cards if c.has_cover)
        # 屏幕外的卡片本来就没请求，不参与判断
        return _expect(n > 3, "有封面 %d 张" % n)

    check("封面懒加载后能出图", _covers)


    def _click_opens():
        card = win._cards[0]
        opened = []
        original = win._open_link
        win._open_link = lambda url: opened.append(url)
        try:
            QTest.mouseClick(card, Qt.MouseButton.LeftButton)
            QTest.qWait(200)
        finally:
            win._open_link = original
        return _expect(opened == [card.result.url], "打开=%s" % (opened[:1],))

    check("单击卡片直接打开网址", _click_opens)

    def _menu_items():
        card = win._cards[0]
        menu = card._menu()
        acts = [a.text() for a in menu.actions() if a.text()]
        want = ["在浏览器中打开", "复制网址", "复制标题", "复制封面图"]
        ok = all(w in acts for w in want)
        return _expect(ok, "菜单=%s" % acts)

    check("右键菜单有打开/复制网址/标题/封面图", _menu_items)

    def _copy_real():
        from types import SimpleNamespace
        from unittest.mock import patch
        target = None
        for card in win._cards[:40]:
            if "baidu.com/link" in card.result.url or "bing.com/ck" in card.result.url:
                target = card
                break
        if target is None:
            return "本次结果里没有跳转链接，跳过"
        # Other background work can replace the status label before resolution
        # finishes. Wait for this URL's actual result signal instead.
        completed = []
        def resolved(raw, final):
            if raw == target.result.url:
                completed.append(final)
        win.bridge.resolved.connect(resolved)
        # Keep the real asynchronous HTTP resolution, but isolate its clipboard
        # from other desktop apps while waiting. Native clipboard writes are
        # checked separately by the copy-toast case above.
        contents = ['']
        clipboard = SimpleNamespace(text=lambda: contents[0],
            setText=lambda text: contents.__setitem__(0, text))
        try:
            with patch('app.ui.mainwindow.QGuiApplication.clipboard', return_value=clipboard):
                win._copy_link(target.result.url)
                ok = wait_until(lambda: bool(completed), timeout=20)
                text = clipboard.text()
        finally:
            win.bridge.resolved.disconnect(resolved)
        return _expect(ok and text.startswith("http") and "baidu.com/link" not in text
                       and "bing.com/ck" not in text, "落地地址=%s" % text[:90])

    check("跳转链接自动解析", _copy_real)

    def _filter():
        win.config.add_blackword("4K")
        win.search_edit.setText(args.query)
        win._start_search()
        wait_until(lambda: not win._searching, timeout=120)
        bad = [c for c in win._cards if "4k" in c.result.title.lower()]
        win.config.remove_blackword("4K")
        return _expect(not bad, "含 4K 的结果 %d 条" % len(bad))

    check("黑名单过滤结果", _filter)

    def _stop():
        win.search_edit.setText("大海")
        win._start_search()
        QTest.qWait(400)
        win._stop_search()
        ok = wait_until(lambda: not win._searching, timeout=60)
        return _expect(ok, "状态=%s" % win.status_label.text())

    check("停止搜索可中断", _stop)

    print("\n[界面状态]")

    def _sort():
        win.sort_combo.setCurrentIndex(1)   # 按来源分组
        QTest.qWait(200)
        win.sort_combo.setCurrentIndex(0)
        return "切换排序正常"

    check("排序切换", _sort)

    def _paging():
        """滚到底会自动加载下一页；重复 url 必须为 0。"""
        if not win._cards:
            win.search_edit.setText("风景")
            win._start_search()
            wait_until(lambda: not win._searching, timeout=120)
        before = len(win._cards)
        bar = win.scroll.verticalScrollBar()
        win._no_more = False
        bar.setValue(bar.maximum())
        QApplication.processEvents()
        time.sleep(0.3)
        QApplication.processEvents()
        win._maybe_load_more()             # 不依赖滚动信号，直接触发一次
        QApplication.processEvents()
        ok = wait_until(lambda: not win._paging, timeout=120)
        added = len(win._cards) - before
        dup = len(win._all_results) - len({r.url for r in win._all_results})
        detail = ("page=%d +%d 张 重复=%d 滚动=%d/%d 视口=%d 忙=%s 模式=%s"
                  % (win._page, added, dup, bar.value(), bar.maximum(),
                     win.scroll.viewport().height(), win._agent_busy,
                     "agent" if win.agent_mode else "direct"))
        return _expect(ok and win._page >= 2 and dup == 0, detail)

    check("滚到底自动加载下一页", _paging)

    def _light():
        win.settings.theme = "light"
        win._apply_theme()
        win.repaint()
        QTest.qWait(500)
        p = shot(win, "10-light.png")
        win.settings.theme = "dark"
        win._apply_theme()
        win.repaint()
        QTest.qWait(300)
        return p

    check("浅色主题渲染", _light)

    def _small():
        win.resize(1000, 660)
        QTest.qWait(300)
        p = shot(win, "11-small.png")
        win.resize(1440, 900)
        QTest.qWait(200)
        return p

    check("最小尺寸布局", _small)

    def _settings_dialog():
        from app.ui.dialogs import SettingsDialog

        dlg = SettingsDialog(win.settings, win.http, win.thumbs, win)
        dlg.apply_to(win.settings)
        dlg.deleteLater()
        return "构造 / 应用成功"

    check("设置对话框", _settings_dialog)

    def _site_dialog():
        from app.ui.dialogs import SiteDialog

        dlg = SiteDialog(None, win.http, args.query, win)
        dlg.deleteLater()
        return "构造成功"

    check("新增站点对话框", _site_dialog)

    def _add_site():
        from app.config import SiteConfig

        n0 = win.site_list.count()
        site = SiteConfig(id="custom-test", name="测试站点", kind="generic",
                          search_url="https://www.bing.com/search?q={q}", max_results=5)
        win.config.add_site(site)
        win._reload_sites()
        ok = win.site_list.count() == n0 + 1
        win.config.remove_site("custom-test")
        win._reload_sites()
        return _expect(ok and win.site_list.count() == n0,
                       "%d → %d → %d" % (n0, n0 + 1, win.site_list.count()))

    check("新增 / 删除站点", _add_site)

    def _history():
        menu = win._history_menu()
        acts = [a.text() for a in menu.actions() if a.text()]
        ok = len(acts) >= 3 and any("风景" in a for a in acts)
        return _expect(ok, "菜单项=%s" % acts[:6])

    check("历史菜单", _history)

    def _save_load():
        win._save_config()
        again = app_config.load_config()
        return _expect(again.sites and again.history == win.config.history,
                       "%d 站点 / %d 条历史" % (len(again.sites), len(again.history)))

    check("配置保存与重载", _save_load)

    shot(win, "12-final.png")
    win.close()
    return _summary()


def _expect(cond, detail):
    if not cond:
        raise AssertionError(detail)
    return detail


def _summary() -> int:
    ok = sum(1 for r in RESULTS if r[0])
    bad = len(RESULTS) - ok
    print("\n" + "=" * 62)
    print("通过 %d 项，失败 %d 项" % (ok, bad))
    print("=" * 62)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
