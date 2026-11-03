import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('release', BASE / 'scripts/release.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for filename in ('backup.py', 'service.sh'):
            shutil.copyfile(BASE / filename, self.root / filename)
        def git(*args):
            return subprocess.run(['git', *args], cwd=self.root, check=True,
                                  text=True, capture_output=True).stdout.strip()
        git('init', '-b', 'master')
        git('config', 'user.name', 'Release Test')
        git('config', 'user.email', 'release@example.test')
        git('config', 'core.hooksPath', str(self.root / 'no-hooks'))
        git('config', 'commit.gpgsign', 'false')
        git('add', 'backup.py', 'service.sh')
        git('commit', '-m', 'release fixture')
        self.revision = git('rev-parse', 'HEAD')
        self.output = self.root / 'dist'

    def test_pages_release_is_commit_pinned_and_short_launcher_uses_same_site(self):
        result = release.build_pages(self.root, self.output, 'https://bk.example.com', revision=self.revision)
        self.assertEqual(result['sha256'], hashlib.sha256((self.root / 'backup.py').read_bytes()).hexdigest())
        self.assertEqual(result['url'], f'https://raw.githubusercontent.com/ahzknarf/backup_manager/{self.revision}/backup.py')
        self.assertEqual(json.loads((self.output / 'stable.json').read_text()), result)
        launcher = (self.output / 'run').read_text()
        embedded = launcher.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        compile(embedded, 'published-run', 'exec')
        self.assertIn("DEFAULT_MANIFEST_URL = 'https://bk.example.com/stable.json'", launcher)
        subprocess.run(['bash', '-n', str(self.output / 'run')], check=True)
        self.assertEqual((self.output / 'service.sh').read_text(), launcher)

    def test_dirty_engine_or_launcher_cannot_be_published_with_old_revision(self):
        for filename in ('backup.py', 'service.sh'):
            path = self.root / filename
            original = path.read_bytes()
            path.write_bytes(original + b'\n# uncommitted change\n')
            with self.assertRaisesRegex(ValueError, 'commit changes'):
                release.build_pages(self.root, self.output, 'https://example.test', revision=self.revision)
            self.assertFalse(self.output.exists())
            path.write_bytes(original)

    def test_unsafe_public_urls_are_rejected(self):
        for url in ('http://example.test', 'https://user:secret@example.test', 'https://example.test?token=secret'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                release.build_pages(self.root, self.output, url)


if __name__ == '__main__':
    unittest.main()
