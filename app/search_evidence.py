"""Task-scoped pagination evidence, recorded only after server stale checks.

Zero accepted matches, duplicates, irrelevant candidates and an Agent's prose
are not evidence that a site's continuation has ended.
"""
from collections import OrderedDict
from .models import normalize_url

MAX_SEARCH_PAGE = 50
MAX_TASK_PAGES = 200
MAX_IDLE_PAGES = 3


class SearchEvidence:
    def __init__(self):
        self.reset('')

    def reset(self, task_id):
        if getattr(self, 'task_id', None) != task_id:
            self.task_id = task_id
            self.rows = OrderedDict()
            self.overflow = False
            self.page_count = 0

    def limit(self, data):
        row = self.rows.get((str(data.get('source_id') or ''), str(data.get('query') or '').strip()[:300]))
        if self.page_count >= MAX_TASK_PAGES or self.overflow:
            return 'task_search_budget'
        return row.get('limited', '') if row else ''

    def record(self, task_id, data, result):
        if task_id != self.task_id:
            return
        pagination = result.get('pagination') or {}
        query = str(data.get('query') or '').strip()[:300]
        key = (str(data.get('source_id') or ''), query)
        page = max(1, int(pagination.get('page') or data.get('page') or 1))
        old = self.rows.get(key)
        if old and page <= old['page']:
            return  # A cached earlier page cannot reopen an observed terminal.
        if key not in self.rows and len(self.rows) >= 256:
            self.overflow = True
            return  # Bounded memory; never forget an open cursor to accept done.
        seen = set(old['seen']) if old else set()
        urls = {normalize_url(r.get('url', '')) or r.get('url', '') for r in result.get('results') or []}
        urls.discard('')
        idle = 0 if urls - seen else (old['idle'] if old and page == old['page']+1 else 0) + 1
        seen.update(urls)
        self.page_count += 1
        limited = ('no_candidate_progress' if idle >= MAX_IDLE_PAGES else
                   'query_page_budget' if page >= MAX_SEARCH_PAGE else '')
        if pagination.get('exhausted') is True or pagination.get('supported') is False:
            limited = ''
        self.rows[key] = dict(source_id=key[0], query=query, page=page, seen=seen, idle=idle, limited=limited,
                              exhausted=pagination.get('exhausted'),
                              supported=pagination.get('supported', True),
                              candidates=len(result.get('results') or []))

    def review(self, task, sites, login):
        actions, limits = [], []
        observed = bool(self.rows)
        for site in sites:
            if login.get(site.id, {}).get('state') == 'skipped':
                continue
            rows = [r for (source, _), r in self.rows.items() if source == site.id]
            if not rows:
                actions.append(dict(source_id=site.id, query=task['query'], page=1,
                                    reason='source_not_checked', action='choose_query_and_search'))
            for row in rows:
                if row['supported'] is not False and row['exhausted'] is not True:
                    if row['limited']:
                        limits.append(dict(source_id=site.id, query=row['query'], page=row['page'], reason=row['limited']))
                    else:
                        actions.append(dict(source_id=site.id, query=row['query'], page=row['page']+1,
                                        reason='has_next_page' if row['exhausted'] is False else 'end_not_confirmed',
                                        action='continue_search'))
        skipped_all = bool(sites) and all(login.get(site.id, {}).get('state') == 'skipped' for site in sites)
        if self.page_count >= MAX_TASK_PAGES or self.overflow:
            limits.append(dict(reason='task_search_budget'))
            actions = []
        allowed = (observed or skipped_all) and not actions
        valid = allowed and not limits
        # Drain one query per source before switching its browser page. Sending
        # eight interleaved queries repeatedly evicts the four private pages and
        # replays the same prefix on every recovery.
        scheduled, sources = [], set()
        for action in actions:
            if action['source_id'] not in sources:
                scheduled.append(action)
                sources.add(action['source_id'])
        return dict(verified=valid, observed=observed, outstanding=len(actions),
                    next_actions=scheduled[:3], overflow=self.overflow, can_finish=allowed,
                    limited=bool(limits), limits=limits[:32], pages_checked=self.page_count)
