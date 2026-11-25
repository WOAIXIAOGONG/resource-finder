"""Live native pagination verification, read-only; never saves user settings."""
import sys
import time
import json
import concurrent.futures
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Config
from app.net import HttpClient
from app.adapters import adapter_for
from app.adapters.base import SearchContext
from app.models import normalize_url


def probe(site, query, pages=4):
    http = HttpClient(Config().settings)
    seen, checks = set(), []
    try:
        for page in range(1, pages + 1):
            start = time.monotonic()
            rows = adapter_for(site).search(SearchContext(query, http, max_results=site.max_results, page=page), site)
            urls = {normalize_url(r.url) for r in rows}
            fresh = urls - seen
            entry = dict(site=site.id, query=query, page=page, count=len(rows), new=len(fresh),
                         covers=sum(bool(r.cover) for r in rows), ended=getattr(rows, 'exhausted', None),
                         seconds=round(time.monotonic() - start, 2))
            print(json.dumps(entry, ensure_ascii=False), flush=True)
            checks.append(entry)
            seen.update(urls)
            if getattr(rows, 'exhausted', None):
                break
    except Exception as exc:
        print(site.id, 'ERROR', repr(exc), flush=True)
        return False
    finally:
        http.close()
    return len(checks) == pages and all(c['new'] > 0 for c in checks)


if __name__ == '__main__':
    query = sys.argv[1] if len(sys.argv) > 1 else '猫'
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(probe, s, 'cats' if s.id == 'ytb' else query) for s in Config().sites]
        passed = all([f.result() for f in futures])
    print('RESULT:', 'PASS' if passed else 'FAIL')
    sys.exit(0 if passed else 1)
