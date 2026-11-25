"""Exercise release entrypoints against source aliases and portable user data."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from release_safety import release_target


class ReleaseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rf-release-safety-')
        self.root = Path(self.temp.name) / 'source'
        self.root.mkdir()
        (self.root / 'app').mkdir()
        (self.root / 'app' / 'keep.py').write_text('user source', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_independent_stage_is_allowed(self):
        out = self.root / '.validation' / 'stage'
        self.assertEqual(release_target(self.root, out), out / '资源搜寻器')

    def test_source_itself_parent_and_source_children_are_rejected(self):
        targets = [self.root, self.root.parent, self.root / 'app', self.root / 'app' / 'nested']
        for target in targets:
            with self.subTest(target=target), self.assertRaises(ValueError):
                release_target(self.root, target.parent, target.name)
        self.assertEqual((self.root / 'app' / 'keep.py').read_text(), 'user source')

    def test_program_name_cannot_escape_the_stage(self):
        for name in ('..', '../app', '..\\app', 'C:\\app', '/app'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release_target(self.root, self.root / 'dist', name)

    def test_build_and_package_refuse_stores_before_any_mutation(self):
        for filename in ('build.py', 'package.py'):
            path = Path(__file__).with_name(filename)
            spec = importlib.util.spec_from_file_location('release_entry_' + path.stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for store in ('profiles', 'data', 'downloads'):
                output = self.root / (path.stem + '-' + store)
                saved = output / '资源搜寻器' / store / 'keep.txt'
                saved.parent.mkdir(parents=True)
                saved.write_bytes(b'portable settings or download')
                option = '--output-dir' if filename == 'build.py' else '--dist-dir'
                with self.subTest(tool=filename, store=store), patch.object(module, 'HERE', str(self.root)), \
                        patch('sys.argv', [filename, option, str(output)]), \
                        patch.object(module.subprocess, 'run') as launch:
                    with self.assertRaises(SystemExit):
                        module.main()
                    launch.assert_not_called()
                self.assertEqual(saved.read_bytes(), b'portable settings or download')

    def test_release_cannot_be_packaged_over_source(self):
        for filename, option in (('build.py', '--output-dir'), ('package.py', '--dist-dir')):
            spec = importlib.util.spec_from_file_location('release_alias_' + filename[:-3], Path(__file__).with_name(filename))
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            source = self.root / '资源搜寻器'; source.mkdir(exist_ok=True)
            with self.subTest(tool=filename), patch.object(module, 'HERE', str(source)), \
                    patch('sys.argv', [filename, option, str(self.root)]), \
                    patch.object(module.subprocess, 'run') as launch:
                with self.assertRaises(SystemExit): module.main()
                launch.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
