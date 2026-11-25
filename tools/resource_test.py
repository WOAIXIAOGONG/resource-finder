import testenv
import sys, unittest, json, threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.adapters.resources import ResourceAdapter
from app.adapters.base import SearchContext
from app.config import Config, Settings, SiteConfig
from app.models import SearchResult
from app.agent import _to_result
from app.sources import result_groups
import access_test

class ResourceTests(unittest.TestCase):
    def search(self, kind, html, site=None, page=1):
        self.urls=[]
        def get(url, site):
            self.urls.append(url)
            return html
        ctx=SearchContext('风景', SimpleNamespace(get_text=get), max_results=20, page=page)
        return ResourceAdapter(kind).search(ctx, site or Config().get_site('bing'))

    def test_images_real_urls_cover_and_domain_scope(self):
        meta=json.dumps({'t':'图一','murl':'https://cdn.example.org/a.jpg','purl':'https://news.example.com/post/1'})
        site=SiteConfig(name='example', search_url='https://example.com/?q={q}',scope_domain='example.com')
        rows=self.search('image', "<a class='iusc' m='"+meta+"'></a>",site)
        self.assertEqual(rows[0].url,rows[0].cover)
        self.assertEqual(rows[0].resource_type,'image')
        self.assertIn('first=1',self.urls[0])
        self.assertIn('site%3Aexample.com',self.urls[0])
        self.assertIn('site:'+site.id,result_groups(rows[0],[site]))

    def test_articles_and_legacy_novels_share_article_search(self):
        html='<li class="b_algo"><h2><a href="https://example.com/read/1">示例小说正文</a></h2><p>简介内容</p><img src="https://example.com/cover.jpg"></li>'
        for kind in ('article','novel'):
            rows=self.search(kind,html)
            self.assertEqual(rows[0].resource_type,'article')
            self.assertEqual(rows[0].desc,'简介内容')
            self.assertTrue(rows[0].cover)
            self.assertNotIn('/videos/',self.urls[0])

    def test_empty_unknown_is_not_exhaustion(self):
        from app.net import NetworkError
        with self.assertRaises(NetworkError):self.search('image','captcha')
        self.assertTrue(self.search('article','No results found').exhausted)

    def test_agent_type_roundtrip_and_legacy(self):
        for kind in ('video','image','article','novel','sound'):
            row=_to_result({'title':'x','url':'https://example.com/a','resource_type':kind})
            self.assertEqual(row.to_dict()['resource_type'],'article' if kind=='novel' else kind)
        self.assertEqual(_to_result({'title':'x','url':'https://example.com/a'}).resource_type,'video')
        self.assertIsNone(_to_result({'title':'x','url':'https://example.com/a','resource_type':'bad'}))
        self.assertEqual(Settings.from_dict({'resource_type':'bad'}).resource_type,'video')
        self.assertEqual(Settings.from_dict({'resource_type':'novel'}).resource_type,'article')
        self.assertEqual(_to_result({'title':'x','url':'https://example.com/a.jpg','resource_type':'image','extra':[]}).cover,'https://example.com/a.jpg')

    def test_paging_buffers_native_rows_and_bounds_duplicates(self):
        def html(start, count):
            return ''.join("<a class='iusc' m='"+json.dumps({'t':str(i),'murl':f'https://example.com/{i}.jpg'})+"'></a>" for i in range(start,start+count))
        pages=iter([html(0,5),html(3,5),html(3,5),html(3,5),html(3,5)])
        calls=[]
        def fetch(url,site):
            calls.append(url)
            return next(pages)
        ctx=SearchContext('x',SimpleNamespace(get_text=fetch),max_results=3)
        site=Config().get_site('bing');adapter=ResourceAdapter('image')
        found=[]
        for page in range(1,4):
            ctx.page=page
            rows=adapter.search(ctx,site)
            found.extend(r.url for r in rows)
            self.assertIs(adapter.search(ctx,site),rows)
        self.assertEqual(len(found),8)
        self.assertEqual(len(found),len(set(found)))
        ctx.page=4
        rows=adapter.search(ctx,site)
        self.assertEqual(len(calls),5)
        self.assertFalse(rows.exhausted)
        self.assertIn('尚未确认末页',rows.message)

    def test_type_cursors_are_independent_and_legacy_novel_shares_article_cursor(self):
        html='<li class="b_algo"><h2><a href="https://example.com/read/1">小说正文</a></h2></li>'
        html += "<a class='iusc' m='"+json.dumps({'t':'图','murl':'https://example.com/a.jpg'})+"'></a>"
        ctx=SearchContext('x',SimpleNamespace(get_text=lambda *a:html))
        site=Config().get_site('bing')
        self.assertEqual(len(ResourceAdapter('article').search(ctx,site)),1)
        self.assertEqual(ResourceAdapter('novel').search(ctx,site)[0].resource_type,'article')
        self.assertEqual(ResourceAdapter('image').search(ctx,site)[0].resource_type,'image')
        self.assertEqual(len(ctx.http._pagination_cache),2)

    def test_catalog_defaults_and_one_time_migration_preserve_user_choices(self):
        from app.site_catalog import catalog_sites
        cfg=Config()
        self.assertEqual(len(catalog_sites()),13)
        self.assertEqual(len(cfg.enabled_sites()),4)
        self.assertTrue(all(s.is_valid() and not s.enabled for s in catalog_sites()))
        custom=SiteConfig(id='mine',name='我的Pixiv',search_url='https://www.pixiv.net/tags/{q}',enabled=True)
        legacy={'sites':[custom.to_dict()],'blacklist':['保留'],'history':['旧查询'],'settings':{'resource_type':'novel'}}
        migrated=Config.from_dict(legacy)
        self.assertEqual(migrated.sites[0].to_dict(),custom.to_dict())
        self.assertFalse(migrated.get_site('preset-pixiv'))
        self.assertEqual(len(migrated.sites),13)
        self.assertEqual(migrated.blacklist,['保留'])
        self.assertEqual(migrated.history,['旧查询'])
        self.assertEqual(migrated.settings.resource_type,'article')
        migrated.remove_site('preset-unsplash')
        reloaded=Config.from_dict(migrated.to_dict())
        self.assertFalse(reloaded.get_site('preset-unsplash'))
        self.assertEqual(len(reloaded.sites),12)
        self.assertEqual(Config.from_dict({'sites':[]}).sites,[])

    def test_cancel_does_not_commit_page(self):
        cancel=threading.Event()
        def fetch(*args):
            cancel.set()
            return 'No results found'
        http=SimpleNamespace(get_text=fetch)
        ctx=SearchContext('x',http,cancel=cancel)
        from app.net import NetworkError
        with self.assertRaises(NetworkError):ResourceAdapter('image').search(ctx,Config().get_site('bing'))
        self.assertFalse(next(iter(http._pagination_cache.values()))['pages'])

    def test_native_pixiv_buffers_real_pages_and_preserves_image_page(self):
        calls=[]
        def fetch(url,*a,**kw):
            from urllib.parse import parse_qs,urlsplit
            page=int(parse_qs(urlsplit(url).query)['p'][0]);calls.append(page)
            data=[{'id':str(page*100+i),'title':'宵宫','url':'https://i.pximg.net/'+str(i)+'.jpg','xRestrict':0} for i in range(5)]
            data.append({'id':'999','title':'hidden','xRestrict':1})
            return {'error':False,'body':{'illustManga':{'data':data,'lastPage':2,'total':10}}}
        ctx=SearchContext('宵宫',SimpleNamespace(get_json=fetch),max_results=3)
        adapter=ResourceAdapter('image');site=Config().get_site('preset-pixiv');found=[]
        for page in range(1,5):
            ctx.page=page;rows=adapter.search(ctx,site);found+=list(rows)
        self.assertEqual(calls,[1,2])
        self.assertTrue(rows.exhausted)
        self.assertEqual(len(found),10)
        self.assertEqual(len({r.url for r in found}),10)
        parsed=_to_result(found[0].to_dict())
        self.assertTrue(parsed.extra['image_page'])
        self.assertTrue(parsed.cover.startswith('https://i.pximg.net/'))

    def test_bili_articles_and_illustrations_are_real_sources(self):
        data={'code':0,'data':{'numPages':1,'result':[{'id':123,'title':'<em>宵宫</em>攻略','type':'article',
            'image_urls':['//i0.hdslb.com/1.jpg','//i0.hdslb.com/2.jpg'],'pubdate':1700000000}]}}
        for kind,count in [('article',1),('image',2)]:
            http=SimpleNamespace(get_json=lambda *a,**kw:data)
            with patch('app.adapters.bilibili.BilibiliAdapter._warm'):
                rows=ResourceAdapter(kind).search(SearchContext('宵宫',http),Config().get_site('bili'))
            self.assertEqual(len(rows),count)
            self.assertTrue(rows.exhausted)
            self.assertTrue(all(r.title=='宵宫攻略' for r in rows))
            self.assertTrue(all(r.extra['page_url']=='https://www.bilibili.com/read/cv123' for r in rows))

    def test_native_errors_are_not_misreported_as_no_results(self):
        from app.net import NetworkError
        http=SimpleNamespace(get_json=lambda *a,**kw:{'error':True,'message':'需要登录'})
        with self.assertRaisesRegex(NetworkError,'需要登录'):
            ResourceAdapter('image').search(SearchContext('宵宫',http),Config().get_site('preset-pixiv'))
        rows=ResourceAdapter('article').search(SearchContext('宵宫',SimpleNamespace()),Config().get_site('ytb'))
        self.assertFalse(rows.supported)
        self.assertFalse(rows.exhausted)

    def test_wikimedia_uses_continuation_buffers_and_keeps_origin(self):
        from urllib.parse import parse_qs,urlsplit
        calls=[]
        def fetch(url,*args,**kwargs):
            offset=int(parse_qs(urlsplit(url).query).get('gsroffset',['0'])[0]);calls.append(offset)
            entries=[{'title':'File:cat%d.jpg'%i,'index':i,'imageinfo':[{'mime':'image/jpeg','url':'https://upload.wikimedia.org/%d.jpg'%i,
                      'thumburl':'https://thumb.wikimedia.org/%d.jpg'%i,'descriptionurl':'https://commons.wikimedia.org/wiki/File:%d.jpg'%i}]} for i in range(offset,offset+3)]
            entries.append({'title':'File:video.webm','imageinfo':[{'mime':'video/webm','url':'https://upload.wikimedia.org/video.webm'}]})
            body={'batchcomplete':True,'query':{'pages':entries}}
            if offset==0:body['continue']={'gsroffset':3,'continue':'gsroffset||'}
            return body
        ctx=SearchContext('cat',SimpleNamespace(get_json=fetch),max_results=2)
        site=Config().get_site('preset-commons');adapter=ResourceAdapter('image');all_rows=[]
        for page in range(1,5):
            ctx.page=page;rows=adapter.search(ctx,site);all_rows.extend(rows)
            if page<4:self.assertFalse(rows.exhausted)
        self.assertEqual(calls,[0,3]);self.assertEqual(len(all_rows),6)
        self.assertEqual(len({r.url for r in all_rows}),6)
        self.assertTrue(rows.exhausted)
        self.assertTrue(all('site:preset-commons' in result_groups(r,[site]) for r in all_rows))

    def test_wikisource_empty_error_and_article_cover(self):
        from app.net import NetworkError
        from app.adapters.native_resources import mediawiki
        site=Config().get_site('preset-wikisource')
        for payload,raises in [({'batchcomplete':True},False),({},True),({'error':{'info':'blocked'}},True),({'batchcomplete':True,'continue':{'unexpected':1}},True)]:
            ctx=SearchContext('x',SimpleNamespace(get_json=lambda *a,**kw:payload))
            if raises:
                with self.assertRaises(NetworkError):mediawiki(ctx,site,'article',1)
            else:
                result=mediawiki(ctx,site,'article',1);self.assertEqual(len(result),0);self.assertTrue(result.exhausted)
        payload={'batchcomplete':True,'query':{'pages':[{'title':'紅樓夢','index':1,'fullurl':'https://zh.wikisource.org/wiki/紅樓夢'}]}}
        ctx=SearchContext('紅樓夢',SimpleNamespace(get_json=lambda *a,**kw:payload))
        result=mediawiki(ctx,site,'article',1)
        self.assertEqual(result[0].resource_type,'article');self.assertEqual(result[0].cover,'')
        self.assertEqual(result[0].pub_ts,0)
        with self.assertRaises(NetworkError):mediawiki(ctx,site,'article',2,'https://example.com/w/api.php')

class ResourceProtocolTests(unittest.TestCase):
    setUp=access_test.AccessTests.setUp
    tearDown=access_test.AccessTests.tearDown
    post=access_test.AccessTests.post
    get=access_test.AccessTests.get
    ready=access_test.AccessTests.ready

    def test_all_new_types_use_task_adapter_and_compact_candidate_roundtrip(self):
        from unittest.mock import Mock
        self.ready()
        for kind in ('image','article','novel'):
            with self.subTest(kind=kind):
                expected='article' if kind=='novel' else kind
                task=self.server.push_task('国内资源',{'resource_type':kind,'source_keys':['platform:bilibili']},want=1)
                row=SearchResult(title='资源',url='https://cdn.example.org/a.jpg' if kind=='image' else 'https://www.bilibili.com/read/a',
                    source_id='bili',resource_type=kind,cover='https://example.com/thumb.jpg',extra={'page_url':'https://www.bilibili.com/opus/a'})
                with patch('app.adapters.adapter_for',return_value=Mock(search=Mock(return_value=[row]))) as adapter:
                    reply=self.post('network/search',task_id=task['id'],source_id='bili',query='国内资源',compact=True)
                    self.assertEqual(reply.status_code,200,reply.text)
                    self.assertEqual(adapter.call_args.args[1],expected)
                candidate=reply.json()['results'][0]
                self.assertEqual(candidate['resource_type'],expected)
                self.assertEqual(candidate['extra']['page_url'],row.extra['page_url'])
                submitted=self.post('results',task_id=task['id'],candidate_ids=[candidate['candidate_id']]*2)
                self.assertEqual(submitted.json()['accepted'],1,submitted.text)
                self.assertTrue(submitted.json()['limit_reached'])
                self.assertEqual(self.rows[-1][0][0].resource_type,expected)
                self.assertEqual(self.rows[-1][0][0].cover,row.cover)

class ResourceUITests(unittest.TestCase):
    setUpClass=classmethod(access_test.AccessUITests.setUpClass.__func__)
    tearDownClass=classmethod(access_test.AccessUITests.tearDownClass.__func__)
    setUp=access_test.AccessUITests.setUp
    tearDown=access_test.AccessUITests.tearDown

    def test_long_notices_keep_details_without_taking_over_screen(self):
        message='；'.join('站点%d：索引暂未返回匹配资源，未知末页'%i for i in range(16))
        self.win._page_notice(message)
        self.assertLess(len(self.win.pagination_notice.text()),80)
        self.assertFalse(self.win.notice_details.isHidden())
        self.assertEqual(self.win._search_details_text,message)
        self.win._page_notice('')
        self.assertTrue(self.win.notice_details.isHidden())

    def test_global_search_requires_explicit_action_and_preserves_other_sites(self):
        self.win._set_mode('direct')
        for site in self.win.config.sites:site.enabled=site.id=='bili'
        self.win._query='宵宫';self.win.search_edit.setText('宵宫')
        self.win._page_notice('当前所选来源暂无匹配')
        self.assertFalse(self.win.global_search_btn.isHidden())
        self.assertFalse(self.win.config.get_site('bing').enabled)
        with patch.object(self.win,'_start_search') as start:
            self.win.global_search_btn.click()
            start.assert_called_once()
        self.assertTrue(self.win.config.get_site('bing').enabled)
        self.assertTrue(self.win.config.get_site('bili').enabled)

    def test_single_type_selector_and_video_views_do_not_hide_other_resources(self):
        self.win.settings.show_thumbnails=False
        self.assertEqual(self.win.resource_combo.count(),4)
        self.assertFalse(hasattr(self.win,'type_filter'))
        self.assertEqual(self.win.resource_combo.findData('novel'),-1)
        self.win._append_results([SearchResult(title=k,url='https://example.com/'+k,resource_type=k) for k in ('video','image','article','sound')])
        self.assertEqual(len(self.win._filtered_results()),4)
        self.win.views_combo.setCurrentIndex(1)
        self.assertEqual(len(self.win._filtered_results()),3)

    def test_image_thumbnail_failure_tries_original_without_video_lookup(self):
        from app.ui.widgets import ResultCard
        row=SearchResult(title='image',url='https://example.com/full.jpg',cover='https://example.com/thumb.jpg',resource_type='image')
        card=ResultCard(row,self.win)
        calls=[]
        def request(url,referer='',done=None):
            calls.append(url)
            if done:done(url,'')
        with patch.object(self.win.thumbs,'cached',return_value=None), patch.object(self.win.thumbs,'request',side_effect=request), patch.object(self.win.thumbs,'request_video') as video:
            self.win._request_thumb(card,'https://example.com/')
            self.assertEqual(calls,[row.cover,row.url])
            video.assert_not_called()
        card.deleteLater()

    def test_artwork_page_is_not_downloaded_as_thumbnail(self):
        from app.ui.widgets import ResultCard
        row=SearchResult(title='作品',url='https://www.pixiv.net/artworks/123',cover='https://i.pximg.net/thumb.jpg',
                         resource_type='image',extra={'image_page':True})
        card=ResultCard(row,self.win)
        calls=[]
        def request(url,referer='',done=None):
            calls.append(url)
            if done:done(url,'')
        with patch.object(self.win.thumbs,'cached',return_value=None), patch.object(self.win.thumbs,'request',side_effect=request), patch.object(self.win.thumbs,'request_video') as video:
            self.win._request_thumb(card,'https://www.pixiv.net/')
            self.assertEqual(calls,[row.cover])
            video.assert_not_called()
        card.deleteLater()

    def test_article_cover_uses_work_page_and_respects_explicit_referer(self):
        row=SearchResult(title='小说',url='https://www.pixiv.net/novel/show.php?id=123',
                         cover='https://i.pximg.net/thumb.jpg',resource_type='article',source_id='preset-pixiv')
        self.win._append_results([row])
        self.assertEqual(self.win._cards[0]._referer,row.url)
        self.win.config.get_site('preset-pixiv').referer='https://www.pixiv.net/'
        self.win._clear_results()
        self.win._append_results([row])
        self.assertEqual(self.win._cards[0]._referer,'https://www.pixiv.net/')

    def test_mode_change_cancels_old_task_and_new_task_includes_type(self):
        access_test.connect(self.server,'types')
        self.win._handoff_to_agent('old')
        old=self.server.current_task['id']
        self.win.resource_combo.setCurrentIndex(self.win.resource_combo.findData('image'))
        self.assertFalse(self.server.accepts(old))
        self.assertFalse(self.win.views_combo.isEnabled())
        self.win._handoff_to_agent('new')
        self.assertEqual(self.server.current_task['filters']['resource_type'],'image')
        self.assertEqual(self.win.settings.resource_type,'image')

    def test_server_rejects_wrong_type_does_not_consume_budget(self):
        import requests
        access_test.connect(self.server,'types')
        self.win.resource_combo.setCurrentIndex(1)
        self.win._handoff_to_agent('只搜国内图片')
        task=self.server.current_task
        with requests.Session() as session:
            session.trust_env=False
            response=session.post(self.server.endpoint(),json={'task_id':task['id'],'agent':'types','done':False,
                'results':[{'title':'bad','url':'https://example.com/video','resource_type':'video'},
                           {'title':'good','url':'https://example.com/a.jpg','resource_type':'image'}]},timeout=3)
        self.assertEqual(response.json().get('accepted'),1,response.json())

if __name__=='__main__':unittest.main(verbosity=2)
