import testenv
import sys,json,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import Config,Settings,SiteConfig
from app.adapters import adapter_for
from app.adapters.base import SearchContext
from app.adapters.resources import ResourceAdapter
from app.net import NetworkError
from app.models import SearchResult

def document(page,items,last=3):
    body=''.join('<div class="unit-container-restype-sound"><span class="title-name">雨<span>声</span>'+str(i)+'</span><span class="author" title="作者"></span><div class="item-view-url"><a href="/item/rain_'+str(i)+'.html">音效</a></div></div>' for i in items)
    return '<b class="pageInfo_currentPage">'+str(page)+'</b><i class="pageInfo_totalPage">'+str(last)+'</i>'+body

class SoundTests(unittest.TestCase):
    def test_official_metadata_enriches_verified_links_only(self):
        from app.adapters.sound import enrich
        rows=[SearchResult(title='猫',url='https://www.aigei.com/item/cat.html',resource_type='image')]
        original=rows[0].url
        data={'code':'10001','data':{'itemList':[{'itemTitle':'猫','work':{'workAuthor':'作者','workUploadDate':'2026-01-01'},'prevImageList':[{'prevMiniUrl':'https://s2.5open.com/pvimg/img/cat.jpg'}]}]}}
        called=[];http=SimpleNamespace(post_json=lambda url,**kw:called.append(url) or data)
        self.assertEqual(enrich(SearchContext('猫',http),rows,1,'image'),'')
        self.assertEqual(rows[0].url,original);self.assertEqual(rows[0].cover,'https://s2.5open.com/pvimg/img/cat.jpg');self.assertFalse(rows[0].author)
        self.assertTrue(rows[0].pub_ts);self.assertTrue(all(url.endswith('/search') for url in called))
        rows[0].cover='';data['data']['itemList']*=2;enrich(SearchContext('猫',http),rows,1,'image');self.assertFalse(rows[0].cover)
    def test_aigei_video_image_pages_covers_and_classification(self):
        import base64
        original='https://s1.aigei.com/src/img/jpg/test.jpg?e=1&token=test'
        encoded='aigei-image-encode-'+base64.b64encode(original.encode()).decode()
        for kind,css,inner in [('video','unit-container-restype-videoVideo',''),('image','unit-container-restype-icon','<div class="unit-content-box image-box"></div>')]:
            html='<b class="pageInfo_currentPage">1</b><i class="pageInfo_totalPage">1</i><div class="unit-container '+css+'">'+inner+'<b class="trans-title">猫图</b><img data-original="'+encoded+'" src="/grey.gif"><div class="item-view-url"><a href="/item/cat.html">猫</a></div></div>'
            site=Config().get_site('preset-aigei');urls=[]
            ctx=SearchContext('猫',SimpleNamespace(get_text=lambda url,*_:urls.append(url) or html),resource_type=kind)
            result=adapter_for(site,kind).search(ctx,site)
            self.assertEqual(len(result),1);self.assertTrue(result.exhausted);self.assertEqual(result[0].resource_type,kind)
            self.assertEqual(result[0].cover,original);self.assertEqual(result[0].extra['image_page'],kind=='image')
            self.assertIn('type='+('pic' if kind=='image' else 'video'),urls[0])
    def test_native_pagination_buffer_and_real_links(self):
        site=Config().get_site('preset-aigei');urls=[]
        def get(url,site):
            from urllib.parse import parse_qs,urlsplit
            page=int(parse_qs(urlsplit(url).query)['page'][0]);urls.append(url)
            return document(page,range((page-1)*3,page*3))
        http=SimpleNamespace(get_text=get)
        rows=[]
        for page in range(1,7):
            result=adapter_for(site,'sound').search(SearchContext('雨 & 雷',http,page=page,max_results=2,resource_type='sound'),site)
            rows.extend(result)
        self.assertEqual(len(rows),9);self.assertEqual(len({r.url for r in rows}),9);self.assertTrue(result.exhausted)
        self.assertEqual(len(urls),3);self.assertTrue(all(r.resource_type=='sound' for r in rows))
        self.assertEqual(rows[0].title,'雨声0');self.assertEqual(rows[0].author,'作者');self.assertIn('%26',urls[0])
    def test_wrong_page_or_restricted_response_does_not_exhaust(self):
        site=Config().get_site('preset-aigei')
        for html in ['<h1>请完成人机验证</h1>',document(2,[1]),'<div class="unit-container-restype-music"><a href="/item/music.html">推荐配乐</a></div>']:
            with self.assertRaises(NetworkError):
                adapter_for(site,'sound').search(SearchContext('雨声',SimpleNamespace(get_text=lambda *a:html),resource_type='sound'),site)
        result=adapter_for(site,'sound').search(SearchContext('none',SimpleNamespace(get_text=lambda *a:document(1,[],0)),resource_type='sound'),site)
        self.assertTrue(result.exhausted);self.assertFalse(result)
    def test_other_modes_and_known_non_sound_sources_are_not_mislabeled(self):
        cfg=Config()
        for identity,kind in [('preset-aigei','article'),('preset-pixiv','sound'),('bili','sound'),('ytb','sound')]:
            http=SimpleNamespace(get_text=lambda *a:(_ for _ in ()).throw(AssertionError('unexpected request')))
            result=adapter_for(cfg.get_site(identity),kind).search(SearchContext('雨声',http,resource_type=kind),cfg.get_site(identity))
            self.assertFalse(result.supported);self.assertFalse(result)
    def test_catalog_upgrade_does_not_restore_deleted_old_presets(self):
        cfg=Config();cfg.remove_site('preset-unsplash');cfg.remove_site('preset-aigei');data=cfg.to_dict();data['site_catalog_revision']=1
        upgraded=Config.from_dict(data)
        self.assertIsNone(upgraded.get_site('preset-unsplash'));self.assertIsNotNone(upgraded.get_site('preset-aigei'))
        self.assertFalse(upgraded.get_site('preset-aigei').enabled)
        upgraded.remove_site('preset-aigei');self.assertIsNone(Config.from_dict(upgraded.to_dict()).get_site('preset-aigei'))
        self.assertEqual(Settings.from_dict({'resource_type':'sound'}).resource_type,'sound')
    def test_sound_index_filters_out_articles_and_search_pages(self):
        html=''.join('<li class="b_algo"><h2><a href="'+url+'">'+title+'</a></h2><p>音效</p></li>' for url,title in [
            ('https://example.org/item/rain.html','下雨音效'),('https://example.org/blog/rain','音效介绍'),
            ('https://example.org/search?q=rain','音效搜索'),('https://example.org/sample.wav','雨声')])
        cfg=Config();ctx=SearchContext('rain',SimpleNamespace(get_text=lambda *a:html),resource_type='sound')
        result=ResourceAdapter('sound').search(ctx,cfg.get_site('bing'))
        self.assertEqual(len(result),2);self.assertTrue(all(r.resource_type=='sound' for r in result))

import access_test
class SoundUITests(unittest.TestCase):
    setUpClass=classmethod(access_test.AccessUITests.setUpClass.__func__)
    tearDownClass=classmethod(access_test.AccessUITests.tearDownClass.__func__)
    setUp=access_test.AccessUITests.setUp
    tearDown=access_test.AccessUITests.tearDown
    def test_sound_switch_agent_task_and_cover_placeholder(self):
        from app.ui.widgets import ResultCard
        from app.agent import _to_result
        self.win.resource_combo.setCurrentIndex(self.win.resource_combo.findData('sound'))
        self.assertEqual(self.win.settings.resource_type,'sound');self.assertFalse(self.win.views_combo.isEnabled())
        access_test.connect(self.server,'sound-test');self.win._set_mode('agent');self.win._handoff_to_agent('脚步')
        task=self.server.current_task;self.assertEqual(task['filters']['resource_type'],'sound')
        raw={'title':'脚步','url':'https://www.aigei.com/item/foot.html','resource_type':'sound','source_id':'preset-aigei','source':'爱给网'}
        row=_to_result(raw);self.assertEqual(row.resource_type,'sound')
        card=ResultCard(row,self.win);self.assertFalse(card.cover.pixmap().isNull());self.assertFalse(card.download_hint.isHidden())
        from app.ui import theme
        for mode in ['dark','light']:self.assertFalse(theme.sound_cover(208,117,mode).isNull())
        self.win._clear_results();card.deleteLater()

if __name__=='__main__':unittest.main(verbosity=2)
