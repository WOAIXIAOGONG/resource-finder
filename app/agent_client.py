"""Auditable standard-library helper delivered to an Agent, never auto-executed by the UI."""

_SERVICE_SUPPORT = r'''# Shared fail-closed local control transport (not a model heartbeat).
class ServiceUnavailable(RuntimeError):
    pass

def service_stop(error):
    import json
    print(json.dumps(dict(ok=False, stop=True, session_ended=True, cancelled=True,
        keep_polling=False, next='stop', retryable=False, reason='service_unavailable',
        error=str(error)[:500], instruction='Resource Finder is unavailable. Stop this session and all owned searches. Do not restart or reconnect automatically.')), flush=True)

def read_local(opener, request, timeout):
    import json, time, urllib.error
    # Only retry reads; never replay a potentially accepted POST.
    attempts = 3 if request.get_method() == 'GET' else 1
    for attempt in range(attempts):
        try:
            with opener.open(request, timeout=timeout) as response:
                reply = json.load(response)
                if not isinstance(reply, dict):
                    raise ValueError('Invalid local service response')
                return reply
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                raise  # Preserve explicit stop/stale/paused protocol responses.
            error = exc
        except (OSError, ValueError) as exc:
            error = exc
        if attempt + 1 < attempts:
            time.sleep(1)
    raise ServiceUnavailable(type(error).__name__ + ': ' + str(error))

'''

HELPER_SCRIPT = _SERVICE_SUPPORT + r'''# Resource Finder network helper. Python 3; no external dependencies.
import argparse
import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.parse


def main():
    parser = argparse.ArgumentParser()
    for key in ("base", "token", "agent", "task-id"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args()
    base = args.base.rstrip("/")
    if urllib.parse.urlsplit(base).hostname != "127.0.0.1":
        raise ValueError("base must be the local Resource Finder address")
    cancelled = threading.Event()
    stopped = threading.Event()
    class Cancelled(Exception):
        pass
    request_timeout = 210
    identity = {"agent": args.agent, "task_id": args.task_id}
    def api(path, data=None):
        if cancelled.is_set():
            raise Cancelled()
        url = base + "/api/" + path + "?" + urllib.parse.urlencode({"token": args.token, **identity})
        payload = None if data is None else json.dumps({**identity, **data}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            reply = read_local(local, req, request_timeout if path == 'network/probe' else 4)
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 409):
                raise
            try:
                reply = json.load(exc)
            except (ValueError, TypeError):
                reply = {'cancelled': True, 'stop': exc.code == 403}
            if exc.code == 403:
                reply['stop'] = True
        if reply.get('stop') or reply.get('session_ended') or reply.get('next') == 'stop':
            stopped.set()
        if stopped.is_set() or reply.get('cancelled') or reply.get('paused') or reply.get('stale'):
            cancelled.set()
            raise Cancelled()
        return reply
    def progress(message):
        reply = api("progress", {"stage": "network_check", "message": message})
        if reply.get("paused"):
            raise RuntimeError("用户已暂停；停止检测并回到轮询")
    try:
        config = api("network/config")
        request_timeout = float(config.get("client_timeout_seconds", 210))
        plan = config["network"]
        if not plan.get("required"):
            print(json.dumps({"ok": True, "network": plan}, ensure_ascii=False))
            return 0
        jobs, results = queue.Queue(), queue.Queue()
        for target in plan['targets']:
            jobs.put(target)
        def probe(target):
            attempts = []
            routes = ('proxy', 'direct', 'relay') if config.get('proxy_url') else ('direct', 'relay')
            for route in routes:
                progress("正在检测 " + target + " · " + route)
                record = {"url": target, "route": route, "ok": False}
                try:
                    if route == "relay":
                        reply = api("network/probe", {"url": target})
                        if not reply.get("ok"):
                            raise RuntimeError(reply.get("error", "工具中转失败"))
                        record.update(ok=True, status=reply["status"], receipt=reply["receipt"])
                    else:
                        proxy = config.get("proxy_url", "") if route == "proxy" else ""
                        if route == "proxy" and not proxy:
                            raise RuntimeError("工具当前未配置可交付的 HTTP/HTTPS 代理")
                        proxies = {"http": proxy, "https": proxy} if proxy else {}
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
                        request = urllib.request.Request(target, headers={"User-Agent": "Mozilla/5.0"})
                        with opener.open(request, timeout=float(config.get("request_timeout_seconds", 180))) as response:
                            response.read(1024)
                            record.update(ok=200 <= response.status < 300, status=response.status)
                except Cancelled:
                    raise
                except Exception as exc:
                    record["error"] = type(exc).__name__ + ": " + str(exc)[:800]
                attempts.append(record)
                if record['ok']:
                    break
            return attempts
        def worker():
            while not cancelled.is_set():
                try:
                    target = jobs.get_nowait()
                except queue.Empty:
                    return
                try:
                    results.put((target, probe(target)))
                except Exception as exc:
                    results.put((target, exc))
                    return
        for _ in range(min(3, len(plan['targets']))):
            threading.Thread(target=worker, daemon=True).start()
        collected, next_check = {}, 0
        while len(collected) < len(plan['targets']):
            if time.monotonic() >= next_check:
                api('control')
                next_check = time.monotonic() + 1
            try:
                target, rows = results.get(timeout=0.2)
            except queue.Empty:
                continue
            if isinstance(rows, Exception):
                raise rows
            collected[target] = rows
        attempts = [row for target in plan['targets'] for row in collected[target]]
        reply = api("network/report", {"check_id": plan["check_id"], "attempts": attempts})
        print(json.dumps(reply, ensure_ascii=False, indent=2))
        return 0 if reply.get("usable") else 2
    except ServiceUnavailable as exc:
        cancelled.set()
        service_stop(exc)
        return 0
    except Cancelled:
        cancelled.set()
        terminal = stopped.is_set()
        print(json.dumps({'ok': False, 'cancelled': True, 'stop': terminal,
                          'session_ended': terminal, 'retryable': not terminal,
                          'next': 'stop' if terminal else 'poll'}, ensure_ascii=False), flush=True)
        return 0 if terminal else 75
    except Exception as exc:
        try:
            api("error", {"error": "网络助手未完成：" + type(exc).__name__ + ": " + str(exc)[:1500]})
        except Exception:
            pass
        print("网络助手未完成：" + str(exc), file=sys.stderr)
        cancelled.set()
        return 3


if __name__ == "__main__":
    sys.exit(main())
'''


CONTROL_SCRIPT = _SERVICE_SUPPORT + r'''# Resource Finder cooperative task runner. Python 3, standard library only.
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request


def serve(args):
    """Own a real reusable worker, not a heartbeat-only detached listener."""
    if not args.run:
        print('Agent mode requires interpretation and candidate review. Keep the Agent foreground loop, '
              'or supply --run with a real model-capable worker. A collector cannot substitute for the Agent.',
              file=sys.stderr, flush=True)
        return 2  # Fail before claiming a task or presenting a ready connection.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = args.base.rstrip('/')
    def api(path, data=None, **params):
        query = urllib.parse.urlencode(dict(token=args.token, agent=args.agent, **params))
        body = None if data is None else json.dumps(dict(agent=args.agent, **data)).encode('utf-8')
        request = urllib.request.Request(base+'/api/'+path+'?'+query, data=body,
            headers={'Content-Type':'application/json'})
        try:
            return read_local(opener, request, timeout=6)
        except urllib.error.HTTPError as exc:
            if exc.code not in (403,409):raise
            reply = json.load(exc)
            if exc.code == 403:reply.update(stop=True,session_ended=True,next='stop')
            return reply
    child = None
    initial = args.task_id
    stalled = {}
    last_task = None
    try:
        with tempfile.TemporaryDirectory(prefix='rf-worker-') as folder:
            task_file = os.path.join(folder, 'task.json')
            worker_command = args.run
            print(json.dumps(dict(event='dispatcher_started', worker='custom' if args.run else 'supplied')),flush=True)
            while True:
                if initial:
                    reply = api('task/context', task_id=initial)
                    initial = None
                else:
                    reply = api('task', wait=2)
                if reply.get('stop') or reply.get('session_ended') or reply.get('next')=='stop':
                    print(json.dumps(dict(stop=True,session_ended=True,next='stop')),flush=True)
                    return 0
                if reply.get('paused'):
                    time.sleep(.2);continue
                task = reply.get('task')
                if not task or task.get('state') in ('completed','failed'):
                    continue
                if task.get('kind')=='connect':
                    api('connect',dict(challenge=task['challenge'],ok=True))
                    print(json.dumps(dict(event='connected')),flush=True)
                    continue
                if task['id'] != last_task:
                    stalled.clear();last_task = task['id']
                with open(task_file,'w',encoding='utf-8') as f:json.dump(task,f,ensure_ascii=False)
                identity = dict(task_id=task['id'])
                ack = api('ack',identity)
                if not ack.get('ok') or ack.get('paused'):continue
                env = dict(os.environ, RF_TASK_FILE=task_file, RF_BASE=base,
                    RF_TOKEN=args.token, RF_AGENT=args.agent, RF_TASK_ID=task['id'],
                    RF_WORKER_STATE=os.path.join(folder,'cursors.json'))
                command = [sys.executable,os.path.abspath(__file__),'--base',base,'--token',args.token,
                    '--agent',args.agent,'--task-id',task['id'],'--state-file',task_file,'--run',*worker_command]
                print(json.dumps(dict(event='task_started',task_id=task['id'],want=task.get('want'))),flush=True)
                options = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
                child = subprocess.Popen(command,env=env,stdout=sys.stdout,stderr=sys.stderr,**options)
                code = child.wait();child = None
                state = api('control',task_id=task['id'],wait=0)
                print(json.dumps(dict(event='task_returned',task_id=task['id'],code=code,
                    received=state.get('received'),next=state.get('next'))),flush=True)
                if state.get('stop') or state.get('session_ended'):return 0
                if state.get('cancelled') or state.get('paused') or state.get('stale'):continue
                completion = state.get('completion') or {}
                marker = (task['id'],state.get('received'),completion.get('pages_checked'))
                stalled[marker] = stalled.get(marker,0)+1
                if completion.get('pending') and stalled[marker] < 3 and code in (0,75):
                    continue  # /task redelivers the same incomplete task.
                api('error',dict(task_id=task['id'],error='常驻搜索子程序退出但任务尚未收尾（退出码%d）；请修正通用工作脚本。接管器仍等待下一任务。'%code))
    except ServiceUnavailable as exc:
        service_stop(exc)
        return 0
    finally:
        if child is not None and child.poll() is None:
            if os.name=='nt':
                subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW,timeout=5)
            else:os.killpg(child.pid,signal.SIGTERM)
            try:child.wait(timeout=3)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=3)


def main():
    parser = argparse.ArgumentParser()
    for key in ('base', 'token', 'agent'):
        parser.add_argument('--' + key, required=True)
    parser.add_argument('--task-id')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--state-file')
    parser.add_argument('--run', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if urllib.parse.urlsplit(args.base).hostname != '127.0.0.1':
        raise ValueError('base must be the local Resource Finder address')
    if args.serve:return serve(args)
    if not args.task_id:parser.error('--task-id is required without --serve')
    query = urllib.parse.urlencode({'token': args.token, 'agent': args.agent, 'task_id': args.task_id, 'observe': 1})
    url = args.base.rstrip('/') + '/api/control?' + query
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    child = None
    runner_id = ''
    revision = None
    def update_state(reply):
        nonlocal revision
        if not args.state_file or reply.get('current_task_id') != args.task_id:return
        budget = reply.get('budget') or {}
        if budget.get('revision')==revision:return
        with open(args.state_file,encoding='utf-8') as f:task=json.load(f)
        task.update({k:reply[k] for k in ('budget','want','received','remaining') if k in reply})
        temporary=args.state_file+'.tmp'
        with open(temporary,'w',encoding='utf-8') as f:json.dump(task,f,ensure_ascii=False)
        os.replace(temporary,args.state_file)
        revision=budget.get('revision')
    def created(pid):
        if os.name!='nt':return None
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return None
        try:
            values=[wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle,*(ctypes.byref(v) for v in values)):return None
            return str(values[0].dwHighDateTime<<32|values[0].dwLowDateTime)
        finally:kernel.CloseHandle(handle)
    def runner_report(data):
        payload = json.dumps(dict(agent=args.agent, task_id=args.task_id, **data)).encode('utf-8')
        request = urllib.request.Request(args.base.rstrip('/')+'/api/activity/runner?'+query,
                    data=payload, headers={'Content-Type':'application/json'})
        try:
            with opener.open(request,timeout=4) as response:return json.load(response)
        except urllib.error.HTTPError as exc:
            try:return json.load(exc)
            except (ValueError,TypeError):return {}
        except (OSError,TimeoutError):return {}
    def check(wait=0):
        try:
            return read_local(opener, urllib.request.Request(url + '&wait=' + str(wait)), timeout=wait + 4)
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 409):
                raise
            try:
                reply = json.load(exc)
            except (ValueError, TypeError):
                reply = {'cancelled': True, 'stop': exc.code == 403}
            if exc.code == 403:
                reply['stop'] = True
            return reply
    def terminate():
        # Only terminate the process tree created by THIS invocation, never the Agent app.
        if child is None or child.poll() is not None:
            return
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name != 'nt':
                os.killpg(child.pid, signal.SIGKILL)
            else:
                child.kill()
            child.wait(timeout=2)
    try:
        reply = check()
        update_state(reply)
        if not any(reply.get(k) for k in ('cancelled', 'stop', 'session_ended')) and reply.get('next') != 'stop' and args.run:
            kwargs = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
            child = subprocess.Popen(args.run, stdout=sys.stdout, stderr=sys.stderr, **kwargs)
            if child.poll() is None:
                registered=runner_report(dict(runner_pid=os.getpid(),child_pid=child.pid,platform=os.name,
                    runner_created=created(os.getpid()),child_created=created(child.pid)))
                runner_id=str(registered.get('runner_id') or '')
                if not runner_id:url=url.replace('observe=1','observe=0')
                if registered.get('stop') or registered.get('cancelled'):reply=registered
        while True:
            update_state(reply)
            if reply.get('stop') or reply.get('session_ended') or reply.get('next') == 'stop':
                terminate()
                reply.update(stop=True, session_ended=True, cancelled=True, next='stop', retryable=False)
                print(json.dumps(reply, ensure_ascii=False), flush=True)
                return 0
            if reply.get('cancelled'):
                terminate()
                print(json.dumps(reply, ensure_ascii=False), flush=True)
                return 75
            if child is not None and child.poll() is not None:
                return child.returncode
            reply = check(wait=1)
    except ServiceUnavailable as exc:
        terminate()
        service_stop(exc)
        return 0
    except Exception as exc:
        terminate()
        print('Task runner stopped: ' + str(exc), file=sys.stderr)
        return 3
    finally:
        terminate()
        if runner_id:
            try:runner_report(dict(runner_id=runner_id))
            except Exception:pass


if __name__ == '__main__':
    sys.exit(main())
'''
