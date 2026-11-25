"""Versioned, standard-library adapter worker delivered with the dispatcher."""

WORKER_SCRIPT = r'''# Resource Finder adapter worker; supplied by the local tool.
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class TaskEnded(Exception):
    pass


class Worker:
    def __init__(self):
        self.base = os.environ['RF_BASE'].rstrip('/')
        if urllib.parse.urlsplit(self.base).hostname != '127.0.0.1':
            raise ValueError('Only the local Resource Finder endpoint is supported')
        self.token = os.environ['RF_TOKEN']
        self.agent = os.environ['RF_AGENT']
        self.tid = os.environ['RF_TASK_ID']
        self.task = self.read_task()
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.timeout = 210
        self.cursor_file = os.environ.get('RF_WORKER_STATE', '')
        self.cursors = {}
        self.signature = json.dumps([self.task['query'], self.task.get('filters', {})], sort_keys=True)
        try:
            with open(self.cursor_file, encoding='utf-8') as f: saved = json.load(f)
            if saved.get('signature') == self.signature and (self.task.get('kind') == 'more' or saved.get('task_id') == self.tid):
                self.cursors = saved.get('cursors', {})
        except (OSError, ValueError):
            pass

    def read_task(self):
        with open(os.environ['RF_TASK_FILE'], encoding='utf-8') as f:
            return json.load(f)

    def api(self, path, data=None, **params):
        identity = dict(agent=self.agent, task_id=self.tid)
        query = urllib.parse.urlencode(dict(token=self.token, **identity, **params))
        body = None if data is None else json.dumps(dict(**identity, **data)).encode('utf-8')
        req = urllib.request.Request(self.base+'/api/'+path+'?'+query, data=body,
                                     headers={'Content-Type': 'application/json'})
        try:
            with self.http.open(req, timeout=self.timeout) as response: reply = json.load(response)
        except urllib.error.HTTPError as exc:
            reply = json.load(exc)
            if exc.code == 403: reply['stop'] = True
        if any(reply.get(k) for k in ('stop', 'session_ended', 'stale', 'cancelled', 'paused')):
            raise TaskEnded()
        if not reply.get('ok') and not (reply.get('login_required') or reply.get('skipped')):
            raise RuntimeError(path+': '+str(reply.get('error') or reply)[:1200])
        return reply

    def control(self):
        self.task = self.read_task()
        return self.api('control', wait=0)

    def save_page(self, sid, query, page):
        self.cursors[sid+'\n'+query] = page
        if self.cursor_file:
            with open(self.cursor_file+'.tmp', 'w', encoding='utf-8') as f:
                json.dump(dict(signature=self.signature, task_id=self.tid, cursors=self.cursors), f)
            os.replace(self.cursor_file+'.tmp', self.cursor_file)

    def run(self):
        if (self.task.get('execution') or {}).get('requires_agent_reasoning'):
            raise RuntimeError('本任务要求Agent理解需求并审核相关性。此采集脚本没有模型能力，已停止；请由Agent读取任务后自行理解、检索和筛选，不要将全部候选直接回推。')
        cfg = self.api('network/config')
        # sources is a TOP-LEVEL field. network describes connectivity only.
        sources = cfg.get('sources')
        if not isinstance(sources, list) or not sources:
            raise RuntimeError('没有可搜索来源；请检查当前启用站点及来源筛选。配置字段：'+', '.join(sorted(cfg)))
        self.timeout = float(cfg.get('client_timeout_seconds', 210))
        print(json.dumps(dict(event='worker_ready', task_id=self.tid,
            sources=[s['id'] for s in sources]), ensure_ascii=False), flush=True)
        plan = cfg.get('network') or {}
        if plan.get('required'):
            attempts = []
            for url in plan.get('targets', []):
                self.control()
                try:
                    probe = self.api('network/probe', dict(url=url))
                    attempts.append(dict(url=url, route='relay', ok=True,
                        status=probe['status'], receipt=probe['receipt']))
                except RuntimeError as exc:
                    attempts.append(dict(url=url, route='relay', ok=False, error=str(exc)))
            report = self.api('network/report', dict(check_id=plan['check_id'], attempts=attempts))
            if not report.get('usable'):
                return 3  # The server already reported the actual probe failure.
        allowed = {s['id'] for s in sources}
        pending = list((self.task.get('completion') or {}).get('next_actions') or [])
        if not pending:
            pending = [dict(source_id=s['id'], query=self.task['query'],
                page=self.cursors.get(s['id']+'\n'+self.task['query'], 1)) for s in sources]
        blocked, errors = {}, {}
        excluded = set(self.task.get('exclude') or [])
        while True:
            self.control()  # Latest server quota, never cached initial want.
            cfg = self.api('network/config')
            login = cfg.get('login') or {}
            for sid, action in list(blocked.items()):
                if login.get(sid, {}).get('state') == 'ready':
                    pending.append(action); blocked.pop(sid)
                elif login.get(sid, {}).get('state') == 'skipped':
                    blocked.pop(sid)
            while pending:
                action = pending.pop(0)
                sid, query, page = action['source_id'], action['query'], int(action.get('page') or 1)
                if sid not in allowed or sid in blocked or sid in errors:
                    continue
                self.control()
                self.save_page(sid, query, page)
                try:
                    result = self.api('network/search', dict(source_id=sid, query=query, page=page, compact=True))
                except RuntimeError as exc:
                    errors[sid] = str(exc)
                    continue  # Other selected sources still get their turn.
                if result.get('login_required'):
                    blocked[sid] = action
                    continue  # Login request already exists; do not recreate it.
                if result.get('skipped'):
                    continue
                ids = [r['candidate_id'] for r in result.get('results', [])
                       if r.get('candidate_id') and r.get('url') not in excluded]
                self.control()
                pushed = self.api('results', dict(candidate_ids=ids, done=False))
                print(json.dumps(dict(event='page', task_id=self.tid, source_id=sid, page=page,
                    candidates=len(ids), accepted=pushed.get('accepted'), received=pushed.get('received'),
                    remaining=pushed.get('remaining')), ensure_ascii=False), flush=True)
                if pushed.get('done'):
                    return 0  # Keep this page so next batch can consume its remainder.
                pagination = result.get('pagination') or {}
                if not (pagination.get('exhausted') or pagination.get('limited') or pagination.get('supported') is False):
                    self.save_page(sid, query, page+1)
            self.control()
            finished = self.api('results', dict(done=True, results=[],
                message='工具工作脚本已处理当前选定来源；按服务器分页证据判断收尾。'))
            if finished.get('done'):
                print(json.dumps(dict(event='round_complete', task_id=self.tid,
                    received=finished.get('received'), keep_polling=True)), flush=True)
                return 0
            pending = [a for a in (finished.get('completion') or {}).get('next_actions', [])
                       if a['source_id'] not in blocked and a['source_id'] not in errors]
            if pending:
                continue
            if blocked:
                self.api('control', wait=2)
                continue
            raise RuntimeError('；'.join(errors.values()) or '服务端未接受收尾且未提供后续动作，任务未冒充完成')


def main():
    worker = Worker()
    try:
        return worker.run()
    except TaskEnded:
        return 75
    except Exception as exc:
        print('worker_error: '+str(exc), file=sys.stderr, flush=True)
        try:
            worker.api('error', dict(error=str(exc)))
        except TaskEnded:
            return 75
        return 3


if __name__ == '__main__':
    sys.exit(main())
'''
