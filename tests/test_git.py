import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import backup as bm

BASE = Path(__file__).resolve().parents[1]


class GitSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'repo'
        self.remote = self.base / 'remote.git'
        self.env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
        self.command('git', 'init', '--bare', str(self.remote))
        self.command('git', 'init', '-b', 'master', str(self.root))
        self.git('config', 'user.email', 'backup@example.test')
        self.git('config', 'user.name', 'Backup Test')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('config', 'core.hooksPath', str(self.root / '.git/hooks'))
        (self.root / 'data').mkdir()
        self.source = self.root / 'data/live.db'
        self.snapshot = self.root / 'data/snapshot.db'
        with sqlite3.connect(self.source) as connection:
            connection.execute('create table items(value text)')
            connection.execute("insert into items values('original')")
        connection.close()
        self.config = {'version': 1, 'databases': [
            {'type': 'sqlite', 'source': 'data/live.db', 'snapshot': 'data/snapshot.db'}],
            'git': {'branch': 'master'}, 'deploy': {'enabled': False}}
        self.save()
        (self.root / 'notes.txt').write_text('original\n')
        self.git('add', '.backup.yaml', 'notes.txt')
        self.git('commit', '-m', 'initial')
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', '-u', 'origin', 'master')

    def command(self, *args, cwd=None):
        return subprocess.run(args, cwd=cwd, env=self.env, check=True,
                              text=True, capture_output=True, timeout=15).stdout.strip()

    def git(self, *args):
        return self.command('git', *args, cwd=self.root)

    def save(self):
        (self.root / '.backup.yaml').write_text(json.dumps(self.config))

    def tool(self, expected=0):
        result = subprocess.run([sys.executable, str(BASE / 'backup.py'), '--repo', str(self.root), 'backup'],
                                env=self.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def hook(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\n' + content + '\n')
        path.chmod(0o700)

    def test_single_config_derives_exclusions_and_keeps_unrelated_changes(self):
        (self.root / '.env').write_text('SECRET=test-only\n')
        (self.root / 'notes.txt').write_text('uncommitted\n')
        self.tool()
        self.assertFalse((self.root / '.gitignore').exists())
        for path in ('data/live.db', 'data/live.db-wal', 'data/live.db-shm', 'data/live.db-journal', '.backup-manager.lock'):
            self.assertEqual(self.git('check-ignore', path), path)
        self.assertEqual(self.git('show', 'HEAD:notes.txt'), 'original')
        self.assertNotIn('.env', self.git('ls-files').splitlines())
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')
        self.assertIn('notes.txt', self.git('diff', '--name-only'))
        self.tool()
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '2')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.command('git', '--git-dir', str(self.remote), 'rev-parse', 'master'))

    def test_commit_hook_failure_keeps_real_index_and_retry_works(self):
        index = self.root / '.git/index'
        original = index.read_bytes()
        (self.root / 'secret.txt').write_text('test-only\n')
        hook = self.root / '.git/hooks/pre-commit'
        self.hook(hook, 'git add -- secret.txt\nexit 1')
        self.tool(expected=1)
        self.assertEqual(index.read_bytes(), original)
        self.assertFalse((self.root / '.git/index.lock').exists())
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')
        hook.unlink()
        self.tool()
        self.assertNotIn('secret.txt', self.git('ls-files').splitlines())
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')

    def test_partial_git_add_failure_leaves_no_staging_and_retry_works(self):
        self.config['git']['paths'] = ['missing.txt']
        self.save()
        original = (self.root / '.git/index').read_bytes()
        self.tool(expected=1)
        self.assertEqual((self.root / '.git/index').read_bytes(), original)
        del self.config['git']['paths']
        self.save()
        self.tool()

    def test_existing_staging_is_preserved_and_snapshot_is_untouched(self):
        (self.root / 'notes.txt').write_text('staged-by-user\n')
        self.git('add', 'notes.txt')
        original = (self.root / '.git/index').read_bytes()
        self.assertIn('Existing staged changes', self.tool(expected=1))
        self.assertEqual((self.root / '.git/index').read_bytes(), original)
        self.assertFalse(self.snapshot.exists())

    def test_push_failure_retries_existing_commit_without_an_extra_commit(self):
        hook = self.remote / 'hooks/pre-receive'
        self.hook(hook, 'exit 1')
        self.tool(expected=1)
        committed = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')
        hook.unlink()
        self.tool()
        self.assertEqual(self.git('rev-parse', 'HEAD'), committed)
        self.assertEqual(self.command('git', '--git-dir', str(self.remote), 'rev-parse', 'master'), committed)

    def test_remote_ahead_fails_before_snapshot_replacement(self):
        other = self.base / 'other'
        self.command('git', 'clone', str(self.remote), str(other))
        self.command('git', '-C', str(other), 'config', 'user.name', 'Other Test')
        self.command('git', '-C', str(other), 'config', 'user.email', 'other@example.test')
        self.command('git', '-C', str(other), 'config', 'commit.gpgsign', 'false')
        (other / 'remote.txt').write_text('ahead\n')
        self.command('git', '-C', str(other), 'add', 'remote.txt')
        self.command('git', '-C', str(other), 'commit', '-m', 'remote ahead')
        self.command('git', '-C', str(other), 'push')
        self.assertIn('ahead/diverged', self.tool(expected=1))
        self.assertFalse(self.snapshot.exists())

    def test_runtime_files_already_tracked_are_not_silently_removed(self):
        self.git('add', 'data/live.db')
        self.git('commit', '-m', 'track runtime database')
        self.git('push')
        self.assertIn('already tracked', self.tool(expected=1))
        self.assertIn('data/live.db', self.git('ls-files').splitlines())
        self.assertFalse(self.snapshot.exists())

    def test_source_alias_cannot_hide_an_already_tracked_database(self):
        self.git('add', 'data/live.db')
        self.git('commit', '-m', 'track runtime database')
        self.git('push')
        (self.root / 'alias').symlink_to(self.root / 'data', target_is_directory=True)
        self.config['databases'][0]['source'] = 'alias/live.db'
        self.save()
        self.assertIn('already tracked', self.tool(expected=1))
        self.assertFalse(self.snapshot.exists())

    def test_broad_paths_and_explicit_runtime_paths_rejected(self):
        for path in ('*', ':(top)*', 'data', '.', 'data/live.db', 'data/live.db-wal'):
            with self.subTest(path=path):
                self.config['git']['paths'] = [path]
                self.save()
                self.tool(expected=1)
                self.assertFalse(self.snapshot.exists())
                self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '1')

    def test_existing_git_lock_prevents_snapshot_work(self):
        lock = self.root / '.git/index.lock'
        lock.write_bytes(b'another-operation')
        self.assertIn('index is locked', self.tool(expected=1))
        self.assertEqual(lock.read_bytes(), b'another-operation')
        self.assertFalse(self.snapshot.exists())


if __name__ == '__main__':
    unittest.main()
