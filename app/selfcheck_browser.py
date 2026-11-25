"""Frozen runtime smoke test for the real extension bridge, with fixture replies."""
import json
import threading
from websockets.sync.client import connect
from .config import Settings
from .login_browser import LoginBrowser


def run(log):
    browser = LoginBrowser(Settings())
    ws = None
    try:
        address = browser.connection_info()
        ws = connect(address, origin='chrome-extension://' + 'a' * 32, proxy=None)
        assert json.loads(ws.recv())['type'] == 'connected'
        data = {'contents': {'videoRenderer': {'videoId': 'selftest001', 'title': {'simpleText': 'Fixture'}}}}
        def reply():
            try:
                for raw in ws:
                    msg = json.loads(raw)
                    if msg.get('type') != 'job':
                        continue
                    result = ([{'id': 1, 'signedIn': True}] if msg['op'] == 'discover' else
                              {'html': '<script>var ytInitialData = ' + json.dumps(data) + ';</script>'})
                    ws.send(json.dumps(dict(type='result', id=msg['id'], ok=True, data=result)))
            except Exception:
                pass
        threading.Thread(target=reply, daemon=True).start()
        result, config = browser.search_page('fixture')
        assert result == data and config['_rf_browser_session'][1] == 1
        log('browser bridge ok: real loopback WebSocket, extension-origin pairing, fixture search and pinned browser session; no browser launched')
    finally:
        if ws:
            ws.close()
        browser.close()
