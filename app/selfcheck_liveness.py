"""Frozen-process smoke test: real registered workers, quiet UI and late progress."""
import os
from pathlib import Path
import subprocess
import time
import requests


def run(app, win, log):
    workers=[];old_warn=win._warn;warnings=[];client=requests.Session();client.trust_env=False
    win._warn=lambda *args:warnings.append(args)
    try:
        win._set_mode('agent');win._clear_results()
        server=win._agent_server;who='FrozenActivity'
        handshake=server.pop_task(0,who)
        if handshake and handshake.get('kind')=='connect':
            server.access.confirm(dict(challenge=handshake['challenge'],ok=True));server.pop_task(0,who)
        win._handoff_to_agent('只搜国内视频');task=server.pop_task(0,who);server.acknowledge(task['id'])
        executable=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        for _ in range(2):
            workers.append(subprocess.Popen([str(executable),'-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 20'],
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)))
        response=client.post(server.url+'/api/activity/runner',params={'token':server.token},json=dict(
            agent=who,task_id=task['id'],runner_pid=workers[0].pid,child_pid=workers[1].pid),timeout=5)
        assert response.status_code==200,response.text
        server._poll_times[who]=time.time()-7200
        win._agent_busy_timeout();win._tick_agent_bar();app.processEvents()
        assert server.connected() and server.liveness()['registered_searches']==1
        assert '搜索子程序运行中' in win.agent_state.text()
        assert '工具外等待/执行' in win.agent_timing_state.text()
        assert server.current_task['id']==task['id'] and not server.cancellation(task['id']).is_set()
        for worker in workers:worker.terminate();worker.wait(timeout=5)
        assert server.liveness()['registered_searches']==0
        for age in (211,301,7200):
            server._poll_times[who]=time.time()-age
            win._agent_busy_timeout();win._tick_agent_bar();app.processEvents()
            assert server.current_task['id']==task['id'] and win._agent_busy
            assert not server.cancellation(task['id']).is_set()
            assert '接管保持' in win.agent_state.text()
        response=client.post(server.url+'/api/progress',params={'token':server.token},json=dict(
            agent=who,task_id=task['id'],stage='searching',message='迟到进度已接收'),timeout=5)
        assert response.status_code==200
        app.processEvents();assert not warnings,warnings
        log('Agent liveness ok: real registered process identities, no PID scan; quiet 211/301/7200-second observations retain same task; late progress accepted; explicit disconnect retained')
    finally:
        for worker in workers:
            if worker.poll() is None:worker.terminate();worker.wait(timeout=5)
        client.close();win._warn=old_warn
        if win._agent_server:win._agent_server.disconnect()
        win._agent_busy_done();win._set_mode('direct');win._clear_results()
