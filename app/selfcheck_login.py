"""Frozen-program login regression using fixture pages and real localhost HTTP."""
import json
import time

import requests

from .net import HttpClient
from .config import Settings


def run(window, app, log, screenshot=''):
    from .selfcheck_browser import run as check_browser
    check_browser(log)
    from PySide6.QtWidgets import QPushButton
    win = window
    manager = win.logins
    browser = manager.browser
    old_open, old_page, old_active = manager.open, browser.search_page, browser.active
    old_continuation = browser.continuation
    old_get = HttpClient.get_text
    old_sites = [(s, s.enabled) for s in win.config.sites]
    def forbidden_open(*args):
        raise AssertionError('Login dialog must not launch a browser')
    manager.open = forbidden_open
    yt = next(s for s in win.config.sites if s.builtin == 'youtube')
    yt.enabled = True
    section = {'itemSectionRenderer': {'contents': [{'backgroundPromoRenderer': {
        'title': {'simpleText': 'Confirm your age'}, 'bodyText': {'simpleText': 'Sign in'}}}]}}
    age = {'contents': {'twoColumnSearchResultsRenderer': {'primaryContents': {
        'sectionListRenderer': {'contents': [section]}}}}}
    normal = {'contents': {'videoRenderer': {'videoId': 'selflogin01', 'title': {'simpleText': 'Login verification fixture'}}}}
    HttpClient.get_text = lambda *a, **k: '<script>var ytInitialData = ' + json.dumps(age) + ';</script>'
    local = requests.Session(); local.trust_env = False
    try:
        win._set_mode('agent')
        win.agent_budget_combo.setCurrentIndex(0)
        win._source_selection = None
        win.settings.show_thumbnails = False
        server = win._agent_server
        server.disconnect()
        who = 'LoginSelfTest'
        handshake = server.pop_task(0, who)
        assert handshake['kind'] == 'connect'
        server.access.confirm({'challenge': handshake['challenge'], 'ok': True})
        server.pop_task(0, who)
        win._handoff_to_agent('nature scenery')
        task = server.pop_task(0, who)
        server.acknowledge(task['id'])
        server.access.report({'check_id': task['network']['check_id'], 'attempts': [
            {'url': u, 'route': 'native', 'status': 200, 'ok': True} for u in task['network']['targets']]})
        def post(path, **body):
            response = local.post(server.url + '/api/' + path, params={'token': server.token},
                json=dict(agent=who, task_id=task['id'], **body), timeout=5)
            assert response.status_code == 200, response.text
            return response.json()
        first = post('network/search', source_id=yt.id, query='test', page=1)
        assert first['login_required'] and first['code'] == 'age_verification_required'
        for _ in range(5):
            app.processEvents(); time.sleep(.02)
        dialog = win._login_dialogs[(task['id'], yt.id)]
        assert not dialog.isModal()
        assert sorted(b.text() for b in dialog.findChildren(QPushButton)) == sorted(['已登录，继续搜索', '本轮跳过该平台', '打开专用登录窗口'])
        if screenshot:
            dialog.grab().save(screenshot)
        # A result from a different platform still arrives while login is pending.
        other = post('results', results=[{'title': 'Other platform fixture',
            'url': 'https://www.bilibili.com/video/BVselflogin', 'source': '哔哩哔哩'}], done=True)
        assert other['accepted'] == 1 and not other['done']
        app.processEvents()
        browser.active = True
        browser.search_page = lambda *a: (age, {})
        manager.verify(task['id'], yt.id)
        app.processEvents()
        assert manager.snapshot(task['id'])[yt.id]['state'] == 'waiting'
        browser.search_page = lambda *a: (normal, {})
        manager.verify(task['id'], yt.id)
        app.processEvents()
        assert manager.snapshot(task['id'])[yt.id]['state'] == 'ready'
        rows = post('network/search', source_id=yt.id, query='test', page=1)
        assert rows['ok'] and len(rows['results']) == 1
        assert len(win._all_results) == 1  # The other platform's result was retained.
        log('login ok: structured age notice, modeless verify/skip with owned-login launcher, other platform continues, actual page checked before ready, session reused')
        from .net import SearchSessionExpired
        def item(vid):
            return {'videoRenderer':{'videoId':vid,'title':{'simpleText':'Recovery '+vid}}}
        def cursor(value):
            return {'continuationItemRenderer':{'continuationEndpoint':{'continuationCommand':{'token':value}}}}
        first_doc={'contents':{'sectionListRenderer':{'contents':[item('recovery001'),cursor('next')]}}}
        config={'INNERTUBE_CONTEXT':{'client':{'clientVersion':'fixture'}},'_rf_browser_session':['background','fixture']}
        browser.search_page=lambda *args:(first_doc,config)
        calls=[]
        def continuation(*args,**kwargs):
            calls.append(1)
            if len(calls)==1:raise SearchSessionExpired('Closed fixture search tab')
            return {'onResponseReceivedCommands':[{'appendContinuationItemsAction':{'continuationItems':[item('recovery002')]}}]}
        browser.continuation=continuation
        one=post('network/search',source_id=yt.id,query='session recovery fixture',page=1)
        two=post('network/search',source_id=yt.id,query='session recovery fixture',page=2)
        assert one['ok'] and two['ok'] and len(two['results'])==1
        assert one['results'][0]['url']!=two['results'][0]['url']
        assert len(calls)==2 and manager.snapshot(task['id'])[yt.id]['state']=='ready'
        log('login recovery ok: lost search page reopened through real Agent HTTP, saved login reused, duplicate skipped, no new login dialog; fixture resource data')
        browser.continuation=old_continuation
        # A model's prose cannot turn a known open cursor into a terminal page.
        browser.search_page=lambda *args:(first_doc,config)
        pending_page=post('network/search',source_id=yt.id,query='completion fixture',page=1)
        assert pending_page['pagination']['exhausted'] is False
        rejected=post('results',results=[],done=True,exhausted=True,message='Only irrelevant results after first page')
        app.processEvents()
        assert rejected['completion_pending'] and not rejected['done'] and not rejected['exhausted']
        assert not server.cancellation(task['id']).is_set() and not win._no_more
        assert '尚未完成' in win.pagination_notice.text()
        resumed=server.pop_task(0,who)
        assert resumed['id']==task['id'] and resumed['resume_required'] and resumed['remaining']==99
        assert resumed['completion']['next_actions']
        server.acknowledge(task['id'])
        log('completion guard ok: real Agent HTTP rejects unverified exhaustion, retains task/cancel token/results, returns same task and next pages, UI says unfinished; fixture resource data')
        # A new notice can be skipped without cancelling the whole task.
        manager.require(task['id'], yt, 'other query', 1, 'Sign in', server.cancellation(task['id']))
        app.processEvents()
        dialog = win._login_dialogs[(task['id'], yt.id)]
        dialog.close(); app.processEvents()
        assert manager.snapshot(task['id'])[yt.id]['state'] == 'skipped'
        assert not server.cancellation(task['id']).is_set()
        assert post('network/search', source_id=yt.id, query='other query')['skipped']
        assert Settings.from_dict({'timeout': 12}).timeout == 180
        assert server.access.config_payload()['client_timeout_seconds'] >= 210
        log('login skip/timeout ok: closing dialog skips only YouTube; 12-second legacy default migrates to 180 seconds')
        server.clear_tasks(); win._agent_busy_done()
    finally:
        HttpClient.get_text = old_get
        manager.open, browser.search_page, browser.active = old_open, old_page, old_active
        browser.continuation = old_continuation
        for site, enabled in old_sites:
            site.enabled = enabled
        local.close()
