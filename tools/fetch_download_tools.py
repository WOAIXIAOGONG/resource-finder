"""Developer-only: bundle official portable download tools with provenance."""
import sys,hashlib,json,zipfile,io,shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.net import HttpClient
from app.config import Settings
ROOT=Path(__file__).resolve().parents[1];DEST=ROOT/'vendor/download-tools'
DEST.mkdir(parents=True,exist_ok=True)
def fetch(url):
 h=HttpClient(Settings(timeout=240))
 try:return h.get(url,timeout=240).content
 finally:h.close_all()
def github(repo,asset):
 release=json.loads(fetch('https://api.github.com/repos/'+repo+'/releases/latest'))
 a=next(a for a in release['assets'] if a['name']==asset)
 data=fetch(a['browser_download_url']);digest=hashlib.sha256(data).hexdigest()
 expected=a.get('digest','').removeprefix('sha256:')
 if expected:assert digest==expected,(repo,'checksum mismatch')
 return data,{'project':repo,'version':release['tag_name'],'url':a['browser_download_url'],'sha256':digest,'verified_release_digest':bool(expected)}
def job(kind):
 if kind=='yt-dlp':
  data,record=github('yt-dlp/yt-dlp','yt-dlp.exe');(DEST/'yt-dlp.exe').write_bytes(data)
  (DEST/'LICENSE-yt-dlp.txt').write_bytes(fetch('https://raw.githubusercontent.com/yt-dlp/yt-dlp/master/LICENSE'))
 elif kind=='deno':
  data,record=github('denoland/deno','deno-x86_64-pc-windows-msvc.zip')
  with zipfile.ZipFile(io.BytesIO(data)) as z:(DEST/'deno.exe').write_bytes(z.read('deno.exe'))
  (DEST/'LICENSE-deno.txt').write_bytes(fetch('https://raw.githubusercontent.com/denoland/deno/main/LICENSE.md'))
 else:
  url='https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
  data=fetch(url);digest=hashlib.sha256(data).hexdigest()
  expected=fetch(url+'.sha256').decode().split()[0];assert expected.lower()==digest
  with zipfile.ZipFile(io.BytesIO(data)) as z:
   for name in ['ffmpeg.exe','ffprobe.exe','LICENSE']:
    member=next(n for n in z.namelist() if n.endswith('/bin/'+name) or n.endswith('/'+name))
    (DEST/(name if name.endswith('.exe') else 'LICENSE-ffmpeg.txt')).write_bytes(z.read(member))
   version=z.namelist()[0].split('/')[0]
  record={'project':'FFmpeg / gyan.dev essentials','version':version,'url':url,'sha256':digest,'verified_release_digest':True,'source':'https://www.gyan.dev/ffmpeg/builds/#release-builds'}
 print(kind,record['version'],'ready',flush=True);return record
with ThreadPoolExecutor(max_workers=3) as p:records=list(p.map(job,['yt-dlp','deno','ffmpeg']))
manifest={'components':records,'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in DEST.iterdir() if p.is_file() and p.name!='manifest.json'}}
(DEST/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
