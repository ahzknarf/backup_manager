import os
import sqlite3
import subprocess
import unittest

from test_git import GitTestCase


class GitAllTests(GitTestCase):
    def setUp(self):
        super().setUp()
        self.config['git']['scope'] = 'all'
        self.save()

    def head_files(self):
        return set(self.git('ls-tree', '-r', '-z', '--name-only', 'HEAD').split('\x00')) - {''}

    def test_all_commits_additions_modifications_deletions_and_literal_names(self):
        (self.root / 'obsolete.txt').write_text('delete this\n')
        self.git('add', 'obsolete.txt')
        self.git('commit', '-m', 'deletion fixture')
        self.git('push')
        (self.root / 'obsolete.txt').unlink()
        (self.root / 'notes.txt').write_text('updated\n')
        names = ['new.txt', 'nested/[settings] 中文.yaml', ':metadata.txt', '-dash.txt', 'line\nbreak.txt']
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('new contents\n')
        (self.root / '.gitignore').write_text('/.env\n/ignored-directory/\n')
        (self.root / '.env').write_text('SECRET=ignored\n')
        (self.root / 'ignored-directory').mkdir()
        (self.root / 'ignored-directory/secret.txt').write_text('ignored\n')
        self.assertIn('scope=all', self.tool())
        files = self.head_files()
        self.assertTrue(set(names).issubset(files))
        self.assertNotIn('obsolete.txt', files)
        self.assertNotIn('.env', files)
        self.assertNotIn('ignored-directory/secret.txt', files)
        self.assertNotIn('data/live.db', files)
        self.assertEqual(self.git('show', 'HEAD:notes.txt'), 'updated')
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.command('git', '--git-dir', str(self.remote), 'rev-parse', 'master'))

    def test_all_includes_staged_work_but_preserves_ignored_staged_content(self):
        (self.root / 'notes.txt').write_text('partially staged\n')
        self.git('add', 'notes.txt')
        (self.root / 'notes.txt').write_text('complete working tree\n')
        (self.root / 'staged.yaml').write_text('before: staging\n')
        self.git('add', 'staged.yaml')
        (self.root / 'staged.yaml').write_text('after: staging\n')
        (self.root / '.env').write_text('SECRET=staged\n')
        self.git('add', '.env')
        (self.root / '.env').write_text('SECRET=working\n')
        (self.root / '.gitignore').write_text('/.env\n')
        self.tool()
        self.assertEqual(self.git('show', 'HEAD:notes.txt'), 'complete working tree')
        self.assertEqual(self.git('show', 'HEAD:staged.yaml'), 'after: staging')
        self.assertNotIn('.env', self.head_files())
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '.env')
        self.assertEqual(self.git('show', ':0:.env'), 'SECRET=staged')
        self.assertEqual((self.root / '.env').read_text(), 'SECRET=working\n')

    def test_failure_preserves_original_staging_byte_for_byte_and_retry_works(self):
        (self.root / 'notes.txt').write_text('staged work\n')
        self.git('add', 'notes.txt')
        (self.root / '.env').write_text('SECRET=staged\n')
        self.git('add', '.env')
        (self.root / '.gitignore').write_text('/.env\n')
        original = (self.root / '.git/index').read_bytes()
        original_head = self.git('rev-parse', 'HEAD')
        hook = self.root / '.git/hooks/pre-commit'
        self.hook(hook, 'exit 1')
        self.tool(expected=1)
        self.assertEqual((self.root / '.git/index').read_bytes(), original)
        self.assertEqual(self.git('rev-parse', 'HEAD'), original_head)
        self.assertFalse((self.root / '.git/index.lock').exists())
        hook.unlink()
        self.tool()
        self.assertEqual(self.git('show', 'HEAD:notes.txt'), 'staged work')
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '.env')

    def test_runtime_files_and_aliases_are_excluded_even_if_explicitly_unignored(self):
        (self.root / '.gitignore').write_text('!/data/live.db*\n!/.backup-manager.lock\n!/data/.*.tmp\n')
        connection = sqlite3.connect(self.source)
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute("update items set value='committed in WAL'")
        connection.commit()
        os.link(self.source, self.root / 'database-hardlink.db')
        temporary = ['data/.live.db.example.restore.tmp', 'data/.snapshot.db.example.tmp']
        for name in temporary:
            (self.root / name).write_text('temporary\n')
        self.assertIn('SKIP runtime path:', self.tool())
        files = self.head_files()
        for name in ['data/live.db', 'data/live.db-wal', 'data/live.db-shm', '.backup-manager.lock',
                     'database-hardlink.db', *temporary]:
            self.assertNotIn(name, files)
        self.assertIn('data/snapshot.db', files)

    def test_staged_deletion_and_rename_are_committed(self):
        (self.root / 'delete.txt').write_text('delete\n')
        self.git('add', 'delete.txt')
        self.git('commit', '-m', 'staged deletion fixture')
        self.git('push')
        self.git('rm', 'delete.txt')
        self.git('mv', 'notes.txt', 'renamed.txt')
        self.tool()
        files = self.head_files()
        self.assertNotIn('delete.txt', files)
        self.assertNotIn('notes.txt', files)
        self.assertIn('renamed.txt', files)
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')

    def test_ignored_tracked_staging_is_not_committed_or_discarded(self):
        (self.root / 'notes.txt').write_text('private staged change\n')
        self.git('add', 'notes.txt')
        (self.root / '.gitignore').write_text('/notes.txt\n')
        self.tool()
        self.assertEqual(self.git('show', 'HEAD:notes.txt'), 'original')
        self.assertEqual(self.git('show', ':0:notes.txt'), 'private staged change')
        self.assertEqual(self.git('diff', '--cached', '--name-only'), 'notes.txt')

    def test_push_retry_with_ignored_staging_does_not_create_duplicate_commit(self):
        (self.root / '.env').write_text('SECRET=staged\n')
        self.git('add', '.env')
        (self.root / '.gitignore').write_text('/.env\n')
        hook = self.remote / 'hooks/pre-receive'
        self.hook(hook, 'exit 1')
        self.tool(expected=1)
        committed = self.git('rev-parse', 'HEAD')
        hook.unlink()
        self.tool()
        self.assertEqual(self.git('rev-parse', 'HEAD'), committed)
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '.env')
        self.assertEqual(self.command('git', '--git-dir', str(self.remote), 'rev-parse', 'master'), committed)

    def test_all_does_not_depend_on_the_paths_allowlist(self):
        self.config['git']['paths'] = ['missing.txt', 'data']
        self.save()
        (self.root / 'other.txt').write_text('included by all mode\n')
        self.tool()
        self.assertIn('other.txt', self.head_files())

    def test_invalid_scope_is_rejected_before_backup(self):
        for value in ('everything', True, None, []):
            with self.subTest(value=value):
                self.config['git']['scope'] = value
                self.save()
                self.assertIn('git.scope', self.tool(expected=1))
                self.assertFalse(self.snapshot.exists())

    def test_all_does_not_resolve_unmerged_index_left_by_stash_apply(self):
        (self.root / 'notes.txt').write_text('stashed change\n')
        self.git('add', 'notes.txt')
        self.git('stash', 'push', '-m', 'conflict fixture', '--', 'notes.txt')
        (self.root / 'notes.txt').write_text('different committed change\n')
        self.git('add', 'notes.txt')
        self.git('commit', '-m', 'conflicting change')
        self.git('push')
        with self.assertRaises(subprocess.CalledProcessError):
            self.git('stash', 'apply')
        self.assertTrue(self.git('ls-files', '--unmerged'))
        self.assertFalse((self.root / '.git/MERGE_HEAD').exists())
        original = (self.root / '.git/index').read_bytes()
        self.assertIn('Unmerged Git entries', self.tool(expected=1))
        self.assertEqual((self.root / '.git/index').read_bytes(), original)
        self.assertFalse(self.snapshot.exists())


if __name__ == '__main__':
    unittest.main()
