import contextlib
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
SCRIPT = (BASE / 'service.sh').read_text()
EMBEDDED = SCRIPT.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]


def launcher_module():
    module = types.ModuleType('backup_launcher')
    exec(compile(EMBEDDED, 'service.sh:python', 'exec'), module.__dict__)
    return module


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache = Path(temporary.name).resolve()
        self.launcher = launcher_module()
        self.old_code = b"print('old release')\n"
        self.old = self.manifest('v0.1.0', self.old_code)
        self.seed(self.old, self.old_code)
        self.url = 'https://example.test/stable.json'

    def manifest(self, version, code):
        return {'version': version, 'url': f'https://example.test/{version}/backup.py',
                'sha256': hashlib.sha256(code).hexdigest()}

    def seed(self, manifest, code, *, name='stable.json', cache=None):
        cache = cache or self.cache
        cache.mkdir(parents=True, exist_ok=True)
        (cache / name).write_text(json.dumps(manifest))
        self.launcher.tool_path(cache, manifest).write_bytes(code)

    def select(self):
        with contextlib.redirect_stderr(io.StringIO()):
            return self.launcher.select_release(self.cache, self.url)

    def test_dotted_release_version_is_accepted(self):
        self.assertEqual(self.launcher.parse_manifest(json.dumps(self.old)), self.old)
        self.assertEqual(self.launcher.parse_manifest((BASE / 'stable.json').read_bytes())['version'], 'v0.2.0')

    def test_invalid_manifest_does_not_poison_good_cache(self):
        original = (self.cache / 'stable.json').read_bytes()
        for content in (b'{}', b'[]', b'not-json', json.dumps({**self.old, 'version': '../bad'}).encode(),
                        json.dumps({**self.old, 'url': 'http://example.test/code'}).encode()):
            with self.subTest(content=content), mock.patch.object(self.launcher, 'download', return_value=content):
                manifest, tool = self.select()
                self.assertEqual(manifest, self.old)
                self.assertEqual(tool.read_bytes(), self.old_code)
                self.assertEqual((self.cache / 'stable.json').read_bytes(), original)

    def test_bad_hash_and_interrupted_tool_download_keep_previous_release(self):
        new = self.manifest('v0.2.0', b"print('new')\n")
        for failure in (b'wrong bytes', OSError('simulated interrupted download'), http.client.IncompleteRead(b'partial')):
            with self.subTest(failure=failure):
                def download(url, limit=2_000_000):
                    if url == self.url:
                        return json.dumps(new).encode()
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                with mock.patch.object(self.launcher, 'download', side_effect=download):
                    manifest, _ = self.select()
                self.assertEqual(manifest, self.old)
                self.assertEqual(json.loads((self.cache / 'stable.json').read_text()), self.old)
                self.assertFalse(self.launcher.tool_path(self.cache, new).exists())

    def test_successful_update_preserves_previous_and_uses_content_address(self):
        code = b"print('new release')\n"
        # Even accidental reuse of a version cannot overwrite a running old tool.
        new = self.manifest(self.old['version'], code)
        with mock.patch.object(self.launcher, 'download', side_effect=[json.dumps(new).encode(), code]):
            manifest, executable = self.select()
        self.assertEqual(manifest, new)
        self.assertEqual(executable.read_bytes(), code)
        self.assertEqual(self.launcher.tool_path(self.cache, self.old).read_bytes(), self.old_code)
        self.assertEqual(json.loads((self.cache / 'previous.json').read_text()), self.old)

    def test_corrupt_current_artifact_falls_back_to_verified_previous(self):
        new_code = b"print('new')\n"
        new = self.manifest('v0.2.0', new_code)
        self.seed(self.old, self.old_code, name='previous.json')
        self.seed(new, new_code)
        self.launcher.tool_path(self.cache, new).write_bytes(b'tampered')
        with mock.patch.object(self.launcher, 'download', side_effect=AssertionError('offline must not download')):
            manifest, _ = self.launcher.select_release(self.cache, self.url, offline=True)
        self.assertEqual(manifest, self.old)

    def test_corrupt_cache_without_verified_previous_is_never_executed(self):
        self.launcher.tool_path(self.cache, self.old).write_bytes(b'tampered')
        with self.assertRaisesRegex(RuntimeError, 'No verified cached release'):
            self.launcher.select_release(self.cache, self.url, offline=True)

    def test_syntax_error_in_new_engine_is_not_promoted(self):
        code = b'not valid python!!'
        new = self.manifest('v0.2.0', code)
        with mock.patch.object(self.launcher, 'download', side_effect=[json.dumps(new).encode(), code]):
            manifest, _ = self.select()
        self.assertEqual(manifest, self.old)

    def test_no_cache_and_bad_download_fails_clearly(self):
        (self.cache / 'stable.json').unlink()
        with mock.patch.object(self.launcher, 'download', side_effect=OSError('offline')):
            with self.assertRaisesRegex(RuntimeError, 'Cannot obtain a verified tool'):
                self.launcher.select_release(self.cache, self.url)

    def test_redirect_to_http_is_rejected_before_following(self):
        handler = self.launcher.HTTPSRedirects()
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            handler.redirect_request(None, None, 302, '', {}, 'http://example.test/insecure')

    def test_different_manifest_endpoints_use_separate_caches(self):
        repo = self.cache / 'service'
        repo.mkdir()
        (repo / '.backup.yaml').write_text('version: 1\n')
        base = self.cache / 'shared'
        selected = []
        env = {'BACKUP_MANAGER_CACHE_DIR': str(base), 'BACKUP_MANAGER_LOCAL': ''}
        with mock.patch.object(sys, 'argv', ['service.sh', '--repo', str(repo), 'check']), \
                mock.patch.object(self.launcher, 'python_with_yaml', return_value=sys.executable), \
                mock.patch.object(self.launcher.os, 'execv') as execute, \
                mock.patch.dict(os.environ, env):
            for url in ('https://one.test/stable.json', 'https://two.test/stable.json'):
                with mock.patch.dict(os.environ, {'BACKUP_MANAGER_MANIFEST_URL': url}), \
                        mock.patch.object(self.launcher, 'select_release',
                                          side_effect=lambda cache, *a, **kw: (selected.append(cache) or (self.old, cache / 'engine.py'))):
                    self.launcher.main()
            self.assertEqual(execute.call_count, 2)
        self.assertNotEqual(selected[0], selected[1])

    def test_shell_launcher_works_via_stdin_and_explicit_repo(self):
        repo = self.cache / 'service with spaces'
        repo.mkdir()
        (repo / '.backup.yaml').write_text('version: 1\ndatabases: []\ngit: {enabled: false}\ndeploy: {enabled: false}\n')
        env = {**os.environ, 'BACKUP_MANAGER_LOCAL': str(BASE / 'backup.py'),
               'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH']}
        result = subprocess.run(['bash', '-s', '--', '--repo', str(repo), 'check'], input=SCRIPT,
                                env=env, cwd=self.cache, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('CHECK OK', result.stdout)

    def test_shell_launcher_executes_verified_warm_cache_offline(self):
        repo = self.cache / 'service'
        repo.mkdir()
        (repo / '.backup.yaml').write_text('version: 1\ngit: {enabled: false}\ndeploy: {enabled: false}\n')
        manifest_url = self.launcher.DEFAULT_MANIFEST_URL
        cache = self.cache / hashlib.sha256(manifest_url.encode()).hexdigest()[:16]
        engine = (BASE / 'backup.py').read_bytes()
        self.seed(self.manifest('v0.2.0', engine), engine, cache=cache)
        env = {**os.environ, 'BACKUP_MANAGER_LOCAL': '', 'BACKUP_MANAGER_OFFLINE': '1',
               'BACKUP_MANAGER_CACHE_DIR': str(self.cache), 'BACKUP_MANAGER_MANIFEST_URL': manifest_url,
               'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH']}
        result = subprocess.run(['bash', str(BASE / 'service.sh'), '--repo', str(repo), 'check'],
                                env=env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('CHECK OK', result.stdout)


if __name__ == '__main__':
    unittest.main()
