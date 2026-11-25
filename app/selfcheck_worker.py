"""Real website + Qt + supplied worker acceptance, opt-in with isolated selftest."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import requests


def run(app, win, cfg, log, python):
    if not python or not Path(python).is_file():
        raise ValueError('--selfworkerpython must point to the test Python interpreter')
    for site in cfg.sites:site.enabled=site.id=='ytb'
    win.settings.show_thumbnails=False
    win._set_mode('agent')
    win.agent_budget_combo.setCurrentIndex(0)
    server=win._agent_server
    # Explicit transport diagnostic. Never claim this synthetic opt-out verifies
    # Agent understanding; production tasks always require model review.
    original_push=server.push_task
    def transport_task(*args, **kwargs):
        with server._task_lock:
            task=original_push(*args, **kwargs)
            task['execution']['requires_agent_reasoning']=False
            return task
    server.push_task=transport_task
    def wait(predicate,seconds=150):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            time.sleep(.05);app.processEvents()
            if predicate():return
        raise RuntimeError('Supplied worker UI timeout: '+str(server.task_status()))
    with tempfile.TemporaryDirectory(prefix='rf-live-ui-') as directory, requests.Session() as client:
        client.trust_env=False
        helper=Path(directory)/'runner.py';output_path=Path(directory)/'worker.log'
        reply=client.get(server.url+'/api/control/helper',params={'token':server.token},timeout=5).json()
        helper.write_text(reply['script'],encoding='utf-8')
        worker=Path(directory)/'worker.py'
        worker.write_text(client.get(server.url+'/api/worker/helper',params={'token':server.token},timeout=5).json()['script'],encoding='utf-8')
        with output_path.open('w',encoding='utf-8') as output:
            process=subprocess.Popen([python,str(helper),'--base',server.url,'--token',server.token,
                '--agent','Live-UI-Worker','--serve','--run',python,str(worker)],stdout=output,stderr=output,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            try:
                wait(server.session_ready,10)
                win.search_edit.setText('nature documentary')
                win._start_search()
                wait(lambda:win._agent_budget_used==100 and not win._agent_busy)
                first={r.url for r in win._all_results}
                assert len(first)==100
                wait(lambda:server.task_status()['waiting_polls']>0,10)
                assert process.poll() is None
                win._load_agent_batch()
                wait(lambda:win._agent_budget_used==200 and not win._agent_busy)
                assert len({r.url for r in win._all_results})==200
                win.agent_budget_combo.setCurrentIndex(1)
                wait(lambda:win._agent_budget_used>200)
                win.agent_budget_combo.setCurrentIndex(0)
                app.processEvents()
                assert not win._agent_busy and win._agent_budget_used>=200
                wait(lambda:server.task_status()['waiting_polls']>0,10)
                assert process.poll() is None and server.session_ready()
                log('collector transport diagnostic (model NOT tested): YouTube 100 -> next batch 200 -> unlimited '+str(win._agent_budget_used)+
                    ' -> limited ends round, results preserved, dispatcher still polling; actual website pages')
            finally:
                server.push_task=original_push
                server.disconnect();process.wait(timeout=10)
                log('supplied worker log: '+output_path.read_text(encoding='utf-8')[-5000:])
