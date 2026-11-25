"""Update transport, archive boundaries, rollback and user confirmation."""
import testenv
from access_test import AccessUITests
import io,json,os,stat,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch,Mock
import requests
from app.config import Settings
from app.updates import (ReleaseClient,UpdateError,UpdateCancelled,UpdateSettings,version,sha,
    write_manifest,verify_stage,stage_archive,apply_files,EXE)

def fixture(root,ver='3.38.0',payload='new'):
    root.mkdir();(root/EXE).write_text(payload);(root/'_internal').mkdir();(root/'_internal'/'runtime').write_text(payload)
    (root/'app').mkdir();(root/'app'/'entry.py').write_text(payload);write_manifest(root,ver);return root

def response(body=b'',status=200,headers=None):
    r=requests.Response();r.status_code=status;r.headers.update(headers or {});r.raw=io.BytesIO(body);return r

class UpdateTests(unittest.TestCase):
    def test_corrupt_local_settings_do_not_break_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            store=UpdateSettings(folder);store.path.parent.mkdir(parents=True,exist_ok=True)
            for value in ('null','[]','broken'):
                store.path.write_text(value)
                self.assertTrue(UpdateSettings(folder).automatic)
                self.assertEqual(UpdateSettings(folder).token(),'')
    def test_versions_and_latest_digest_required(self):
        self.assertGreater(version('v3.10.0'),version('3.9.9'))
        for value in ['latest','3.1','v3.1.0-rc1','../3.2.0']:
            with self.assertRaises(UpdateError):version(value)
        client=ReleaseClient(Settings(),'owner/repo')
        data={'tag_name':'v3.38.0','assets':[{'name':'资源搜寻器-v3.38.0-win64.zip','state':'uploaded','id':3,'size':10,'digest':'sha256:'+'a'*64}]}
        with patch.object(client,'request',side_effect=lambda *_:response(json.dumps(data).encode())):
            self.assertEqual(client.latest('3.37.4')['version'],'3.38.0')
            self.assertIsNone(client.latest('3.38.0'))
            data['assets'][0]['digest']=''
            with self.assertRaisesRegex(UpdateError,'SHA256'):client.latest('3.37.4')
        client.close()

    def test_redirect_drops_authorization_and_rejects_non_github(self):
        client=ReleaseClient(Settings(),'owner/repo','fixture-secret');calls=[]
        def get(url,**kwargs):
            calls.append((url,kwargs))
            return response(status=302,headers={'Location':'https://release-assets.githubusercontent.com/file'}) if len(calls)==1 else response(b'zip')
        with patch.object(client.session,'get',side_effect=get):
            client.request('https://api.github.com/repos/owner/repo/releases/assets/1',True).close()
        self.assertIn('Authorization',calls[0][1]['headers']);self.assertNotIn('Authorization',calls[1][1]['headers'])
        with patch.object(client.session,'get',return_value=response(status=302,headers={'Location':'https://evil.test/file'})):
            with self.assertRaisesRegex(UpdateError,'官方'):client.request('https://api.github.com/test')
        client.close()

    def test_bad_hash_and_cancel_leave_no_partial_package(self):
        client=ReleaseClient(Settings(),'owner/repo')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'update.zip'
            with patch.object(client,'request',return_value=response(b'abc')):
                with self.assertRaisesRegex(UpdateError,'SHA256'):client.download(dict(asset_id=1,size=3,sha256='0'*64),path)
            self.assertFalse(path.exists());client.cancel.set()
            with self.assertRaises(UpdateCancelled):client.download(dict(asset_id=1,size=3,sha256='0'*64),path)
            self.assertFalse(path.exists())
        client.close()

    def test_archive_rejects_traversal_user_data_symlinks_and_case_collisions(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            groups=[['资源搜寻器/../escape'],['资源搜寻器/profiles/key'],['资源搜寻器/C:evil'],['资源搜寻器/app/CON.txt'],
                    ['资源搜寻器/app/X.py','资源搜寻器/app/x.py'],['资源搜寻器/app/link']]
            for i,entries in enumerate(groups):
                archive=root/('%s.zip'%i)
                with zipfile.ZipFile(archive,'w') as z:
                    for name in entries:
                        info=zipfile.ZipInfo(name)
                        if name.endswith('/link'):info.external_attr=(stat.S_IFLNK|0o777)<<16
                        z.writestr(info,'bad')
                with self.assertRaises(UpdateError):stage_archive(archive,root/str(i),'3.38.0')
            self.assertFalse((root/'escape').exists())

    def test_upgrade_preserves_user_data_and_unknown_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target=fixture(root/'target','3.37.4','old');stage=fixture(root/'stage')
            for name in ('profiles','downloads','data'):
                (target/name).mkdir();(target/name/'keep').write_bytes(b'private')
            (target/'user-notes.txt').write_text('keep')
            apply_files(target,stage,root/'backup','3.38.0',lambda p:verify_stage(p,'3.38.0',True))
            self.assertEqual((target/EXE).read_text(),'new');self.assertEqual((root/'backup'/EXE).read_text(),'old')
            for name in ('profiles','downloads','data'):self.assertEqual((target/name/'keep').read_bytes(),b'private')
            self.assertEqual((target/'user-notes.txt').read_text(),'keep')

    def test_failed_healthcheck_rolls_back_exact_program_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target=fixture(root/'target','3.37.4','old');stage=fixture(root/'stage')
            before={str(p.relative_to(target)):sha(p) for p in target.rglob('*') if p.is_file()}
            with self.assertRaisesRegex(RuntimeError,'startup failure'):
                apply_files(target,stage,root/'backup','3.38.0',Mock(side_effect=RuntimeError('startup failure')))
            self.assertEqual(before,{str(p.relative_to(target)):sha(p) for p in target.rglob('*') if p.is_file()})

    def test_manifest_mismatch_refuses_before_touching_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target=fixture(root/'target','3.37.4','old');stage=fixture(root/'stage')
            (stage/EXE).write_text('tampered')
            with self.assertRaises(UpdateError):apply_files(target,stage,root/'backup','3.38.0',lambda p:None)
            self.assertEqual((target/EXE).read_text(),'old');self.assertFalse((root/'backup').exists())

    @unittest.skipUnless(os.name=='nt','Windows secure storage')
    def test_credentials_are_dpapi_encrypted_and_clearable(self):
        with tempfile.TemporaryDirectory() as folder:
            store=UpdateSettings(folder);store.save(token='fixture-not-a-real-token',automatic=False)
            self.assertNotIn('fixture-not-a-real-token',store.path.read_text())
            self.assertEqual(UpdateSettings(folder).token(),'fixture-not-a-real-token')
            self.assertFalse(store.automatic);store.save(token='');self.assertEqual(store.token(),'')

class UpdateUITests(unittest.TestCase):
    setUpClass=classmethod(AccessUITests.setUpClass.__func__)
    tearDownClass=classmethod(AccessUITests.tearDownClass.__func__)
    setUp=AccessUITests.setUp
    tearDown=AccessUITests.tearDown
    def test_manual_button_and_skip_never_install(self):
        from app.ui.updates import UpdateDialog
        with patch.object(UpdateDialog,'check') as check:
            self.win.update_btn.click();dialog=self.win._update_dialog
            self.assertTrue(dialog.isVisible());check.assert_called_once()
        dialog.finished_work('checked',dict(version='9.0.0',notes='fixture'))
        with patch('app.ui.updates.prepare_update') as prepare:
            dialog.close_button.click();prepare.assert_not_called()
        self.assertTrue(dialog.cancel.is_set())
    def test_silent_failure_does_not_open_modal(self):
        from app.ui.updates import UpdateDialog
        dialog=UpdateDialog(self.win,True);dialog.finished_work('error','fixture offline')
        self.assertFalse(dialog.isVisible());self.assertIn('fixture offline',self.win.update_btn.toolTip())

del AccessUITests
if __name__=='__main__':unittest.main(verbosity=2)
