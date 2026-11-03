import contextlib
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
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
        self.assertRegex(self.launcher.parse_manifest((BASE / 'stable.json').read_bytes())['version'],
                         r'^v[0-9]+\.[0-9]+\.[0-9]+$')

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

    def test_system_yaml_bypasses_even_a_broken_cached_venv(self):
        with mock.patch.object(self.launcher, 'python_has_module', return_value=True), \
                mock.patch.object(self.launcher.subprocess, 'run') as run:
            self.assertEqual(self.launcher.python_with_yaml(self.cache), sys.executable)
        run.assert_not_called()

    def test_cached_yaml_is_usable_offline_without_pip(self):
        with mock.patch.object(self.launcher, 'python_has_module', side_effect=[False, True]), \
                mock.patch.object(self.launcher.subprocess, 'run') as run, \
                mock.patch.dict(os.environ, {'BACKUP_MANAGER_OFFLINE': '1'}):
            self.assertEqual(self.launcher.python_with_yaml(self.cache), str(self.cache / 'venv/bin/python3'))
        run.assert_not_called()

    def test_missing_pip_repairs_environment_before_installing_yaml(self):
        python = str(self.cache / 'venv/bin/python3')
        with mock.patch.object(self.launcher, 'python_has_module', side_effect=[False, False, False, True, True]), \
                mock.patch.object(self.launcher.subprocess, 'run') as run, \
                mock.patch.dict(os.environ, {'BACKUP_MANAGER_OFFLINE': '0'}):
            self.assertEqual(self.launcher.python_with_yaml(self.cache), python)
        self.assertEqual(run.call_args_list[0].args[0], [sys.executable, '-m', 'venv', str(self.cache / 'venv')])
        self.assertEqual(run.call_args_list[1].args[0][:4], [python, '-m', 'pip', 'install'])

    def test_failed_ensurepip_reports_system_package_and_never_attempts_pip(self):
        failure = subprocess.CalledProcessError(1, ['python3', '-m', 'venv'], output='ensurepip is not available')
        with mock.patch.object(self.launcher, 'python_has_module', return_value=False), \
                mock.patch.object(self.launcher.subprocess, 'run', side_effect=failure) as run, \
                mock.patch.dict(os.environ, {'BACKUP_MANAGER_OFFLINE': '0'}):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'apt-get install -y python3-yaml') as raised:
                    self.launcher.python_with_yaml(self.cache)
                self.assertIn('ensurepip is not available', str(raised.exception))
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(call.args[0][1:3] == ['-m', 'venv'] for call in run.call_args_list))

    def test_recreation_without_working_pip_stops_before_install(self):
        with mock.patch.object(self.launcher, 'python_has_module', return_value=False), \
                mock.patch.object(self.launcher.subprocess, 'run') as run, \
                mock.patch.dict(os.environ, {'BACKUP_MANAGER_OFFLINE': '0'}):
            with self.assertRaisesRegex(RuntimeError, 'no working pip after recreation'):
                self.launcher.python_with_yaml(self.cache)
        self.assertEqual(run.call_count, 1)

    def test_offline_missing_yaml_neither_creates_venv_nor_installs(self):
        with mock.patch.object(self.launcher, 'python_has_module', return_value=False), \
                mock.patch.object(self.launcher.subprocess, 'run') as run, \
                mock.patch.dict(os.environ, {'BACKUP_MANAGER_OFFLINE': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'cannot install offline'):
                self.launcher.python_with_yaml(self.cache)
        run.assert_not_called()
        self.assertFalse((self.cache / 'venv').exists())

    def test_existing_working_pip_is_reused_and_install_is_verified(self):
        with mock.patch.object(self.launcher, 'python_has_module', side_effect=[False, False, True, True]), \
                mock.patch.object(self.launcher.subprocess, 'run') as run, \
                mock.patch.dict(os.environ, {'BACKUP_MANAGER_OFFLINE': '0'}):
            self.launcher.python_with_yaml(self.cache)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1:4], ['-m', 'pip', 'install'])

    def test_successful_pip_exit_without_importable_yaml_is_rejected(self):
        with mock.patch.object(self.launcher, 'python_has_module', side_effect=[False, False, True, False]), \
                mock.patch.object(self.launcher.subprocess, 'run'), \
                mock.patch.dict(os.environ, {'BACKUP_MANAGER_OFFLINE': '0'}):
            with self.assertRaisesRegex(RuntimeError, 'import failed'):
                self.launcher.python_with_yaml(self.cache)

    def test_actual_venv_without_pip_is_repaired_without_network(self):
        import yaml
        environment = self.cache / 'venv'
        real_run = subprocess.run
        real_probe = self.launcher.python_has_module
        real_run([sys.executable, '-m', 'venv', '--without-pip', str(environment)], check=True,
                 capture_output=True, timeout=30)
        python = str(environment / 'bin/python3')
        self.assertFalse(real_probe(python, 'pip', virtual=True))

        def probe(interpreter, module, **kwargs):
            if str(interpreter) == sys.executable and module == 'yaml':
                return False
            return real_probe(interpreter, module, **kwargs)

        def run(argv, **kwargs):
            if argv[:4] == [python, '-m', 'pip', 'install']:
                # Supply the already installed test dependency locally; no PyPI access.
                result = real_run([python, '-c', "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                  capture_output=True, text=True, check=True)
                shutil.copytree(Path(yaml.__file__).parent, Path(result.stdout.strip()) / 'yaml')
                return subprocess.CompletedProcess(argv, 0)
            return real_run(argv, **kwargs)

        with mock.patch.object(self.launcher, 'python_has_module', side_effect=probe), \
                mock.patch.object(self.launcher.subprocess, 'run', side_effect=run), \
                mock.patch.dict(os.environ, {'BACKUP_MANAGER_OFFLINE': '0'}):
            self.assertEqual(self.launcher.python_with_yaml(self.cache), python)
        self.assertTrue(real_probe(python, 'pip', virtual=True))
        self.assertTrue(real_probe(python, 'yaml', virtual=True))

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
