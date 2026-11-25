"""Publish an already tested ZIP, retaining a draft until its digest is verified."""
import argparse,hashlib,json,re,subprocess,sys,tempfile,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.config import APP_VERSION
from app.update_channel import REPOSITORY

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--zip',required=True);parser.add_argument('--gh',default='gh')
    parser.add_argument('--publish',action='store_true',help='Publish after GitHub digest verification; otherwise retain draft')
    args=parser.parse_args();archive=Path(args.zip).resolve();tag='v'+APP_VERSION
    def gh(*command):
        proc=subprocess.run([args.gh,*command],cwd=ROOT,capture_output=True,text=True,encoding='utf-8')
        if proc.returncode:raise RuntimeError(proc.stderr.strip())
        return proc.stdout
    def git(*command):return subprocess.check_output(['git',*command],cwd=ROOT,text=True,encoding='utf-8').strip()
    if git('status','--porcelain'):raise SystemExit('源码还有未提交修改，拒绝发布')
    if archive.name!='资源搜寻器-%s-win64.zip'%tag:raise SystemExit('ZIP文件名与当前版本不符')
    with zipfile.ZipFile(archive) as z:
        names=z.namelist()
        if any('/profiles/' in '/'+n or '/.git/' in '/'+n or '/.validation/' in '/'+n for n in names):raise SystemExit('ZIP包含私人/开发目录')
        manifest=json.loads(z.read('资源搜寻器/update-manifest.json'))
        if manifest.get('version')!=APP_VERSION:raise SystemExit('ZIP清单版本不符')
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    changes=(ROOT/'CHANGELOG.md').read_text(encoding='utf-8')
    notes=changes.split('## ['+APP_VERSION+']',1)[1].split('\n## [',1)[0]
    with tempfile.TemporaryDirectory(prefix='rf-publish-') as folder:
        import shutil
        remote_archive=Path(folder)/('resource-finder-%s-win64.zip'%tag)
        shutil.copyfile(archive,remote_archive)
        note=Path(folder)/'notes.md';note.write_text('资源搜寻器 '+tag+'\n\n'+notes+'\n\nSHA256: `'+digest+'`\n',encoding='utf-8')
        # No --clobber: published assets and prior releases are never overwritten.
        gh('release','create',tag,'--repo',REPOSITORY,'--draft','--target',git('rev-parse','HEAD'),'--title','资源搜寻器 '+tag,'--notes-file',str(note))
        gh('release','upload',tag,str(remote_archive),'--repo',REPOSITORY)
        releases=json.loads(gh('api','repos/'+REPOSITORY+'/releases'))
        release=next(r for r in releases if r['tag_name']==tag)
        asset=next(a for a in release['assets'] if a['name']==remote_archive.name)
        if asset.get('digest')!='sha256:'+digest:raise SystemExit('GitHub文件摘要不匹配，保留草稿，不向用户发布')
        feed=Path(folder)/'update-info.json'
        feed.write_text(json.dumps(dict(schema=1,repository=REPOSITORY,version=APP_VERSION,
            name=remote_archive.name,size=archive.stat().st_size,sha256=digest,
            url='https://github.com/%s/releases/download/%s/%s'%(REPOSITORY,tag,remote_archive.name),
            notes=notes.strip()),ensure_ascii=False,indent=2),encoding='utf-8')
        gh('release','upload',tag,str(feed),'--repo',REPOSITORY)
        assets=json.loads(gh('api','repos/'+REPOSITORY+'/releases/'+str(release['id'])+'/assets'))
        feed_asset=next(a for a in assets if a['name']=='update-info.json')
        if feed_asset.get('digest')!='sha256:'+hashlib.sha256(feed.read_bytes()).hexdigest():raise SystemExit('公开更新清单摘要不匹配，保留草稿')
        if args.publish:gh('release','edit',tag,'--repo',REPOSITORY,'--draft=false','--latest')
        print(json.dumps(dict(url=release['html_url'],published=args.publish,sha256=digest),ensure_ascii=False))
    return 0
if __name__=='__main__':raise SystemExit(main())
