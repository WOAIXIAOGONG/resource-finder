"""Reproducible public search audit. Uses isolated config, never user saves."""
import sys,os,json,time,tempfile,argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

CASES={
 'bili': [('video',['猫','咖啡','Python']),('image',['宵宫','猫','风景']),('article',['宵宫','摄影','Python'])],
 'ytb':[('video',['cat','coffee','Python tutorial'])],
 'bing':[('image',['宵宫','cat','mountain']),('article',['宵宫','摄影','Python'])],
 'baidu':[('video',['猫','咖啡','Python'])],
 'preset-pixiv':[('image',['宵宫','猫','初音ミク']),('article',['原神','猫','恋愛'])],
 'preset-unsplash':[('image',['cat','mountain','coffee'])],
 'preset-pexels':[('image',['cat','mountain','coffee'])],
 'preset-pixabay':[('image',['cat','mountain','coffee'])],
 'preset-commons':[('image',['cat','mountain','coffee'])],
 'preset-zhihu':[('article',['Python','摄影','学习'])],
 'preset-jianshu':[('article',['Python','摄影','学习'])],
 'preset-qidian':[('article',['斗破苍穹','诡秘之主','凡人修仙传'])],
 'preset-zongheng':[('article',['剑来','雪中悍刀行','逆天邪神'])],
 'preset-jinjiang':[('article',['天官赐福','魔道祖师','撒野'])],
 'preset-gutenberg':[('article',['Pride and Prejudice','Alice','Shakespeare'])],
 'preset-wikisource':[('article',['李白','紅樓夢','唐詩'])],
}

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--out',required=True);ap.add_argument('--sites',nargs='*');ap.add_argument('--timeout',type=float,default=12);args=ap.parse_args()
 out=Path(args.out).resolve();out.mkdir(parents=True,exist_ok=True)
 with tempfile.TemporaryDirectory(prefix='rf-matrix-') as home:
  os.environ['RESOURCE_FINDER_HOME']=home
  from app.config import Config,Settings
  from app.net import HttpClient
  from app.adapters import adapter_for
  from app.adapters.base import SearchContext
  from bs4 import BeautifulSoup
  from urllib.parse import urlsplit
  from app.adapters.bing import real_url
  def run_site(sid):
   records=[];site=Config().get_site(sid)
   for kind,queries in CASES[sid]:
    for n,query in enumerate(queries):
     http=HttpClient(Settings(timeout=args.timeout));requests=[];get_text=http.get_text
     def capture(url,*a,**kw):
      body=get_text(url,*a,**kw);soup=BeautifulSoup(body,'html.parser')
      links=[]
      for node in soup.select('li.b_algo h2 a[href]'):links.append(real_url(node['href']))
      for node in soup.select('a.iusc[m]'):
       try:links.append(json.loads(node['m']).get('purl',''))
       except (ValueError,TypeError):pass
      requests.append({'url':url,'bytes':len(body),'raw_results':len(links),'domains':sorted({urlsplit(u).hostname or '' for u in links})})
      if len(requests)==1:(out/(sid+'-'+kind+'-'+str(n)+'.html')).write_text(body,encoding='utf-8')
      return body
     http.get_text=capture;start=time.monotonic()
     row={'site':sid,'name':site.display,'type':kind,'query':query}
     try:
      results=adapter_for(site,kind).search(SearchContext(query,http,max_results=24,resource_type=kind),site)
      row.update(count=len(results),covers=sum(bool(r.cover) for r in results),exhausted=getattr(results,'exhausted',None),supported=getattr(results,'supported',True),message=getattr(results,'message',''),samples=[{'title':r.title,'url':r.url} for r in results[:3]])
     except Exception as exc:row.update(count=0,error=type(exc).__name__+': '+str(exc))
     finally:http.close_all()
     row.update(seconds=round(time.monotonic()-start,2),requests=requests);records.append(row)
     print(json.dumps({k:v for k,v in row.items() if k not in ('requests','samples')},ensure_ascii=False),flush=True)
   (out/(sid+'.json')).write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
   return records
  rows=[]
  with ThreadPoolExecutor(max_workers=4) as pool:
   for f in as_completed([pool.submit(run_site,sid) for sid in (args.sites or CASES)]):rows+=f.result()
  (out/'matrix.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
  print('COMPLETED',len(rows),'cases; successful queries:',sum(r['count']>0 for r in rows),flush=True)

if __name__=='__main__':main()
