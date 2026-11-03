import os
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
TOOL = BASE / 'backup.py'

class BackupTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.repo = Path(self.t.name) / 'service'
        self.repo.mkdir()
        (self.repo / 'data').mkdir()
        (self.repo / '.backup.yaml').write_text('''version: 1
databases:
  - type: sqlite
    source: data/app.db
    snapshot: data/backup_app.db
    method: vacuum
restore:
  policy: if_missing
git:
  enabled: false
deploy:
  enabled: false
''')
        self.source = self.repo / 'data/app.db'
        self.snapshot = self.repo / 'data/backup_app.db'
        con = sqlite3.connect(self.source)
        con.execute('create table items (id integer primary key, value text)')
        con.execute("insert into items (value) values ('original')")
        con.commit()
        con.close()

    def tool(self, command, expected=0):
        p = subprocess.run([sys.executable, str(TOOL), '--repo', str(self.repo), command], capture_output=True, text=True)
        self.assertEqual(p.returncode, expected, p.stdout + p.stderr)
        return p.stdout

    def test_backup_restore_no_overwrite(self):
        self.tool('backup')
        self.assertTrue(self.snapshot.exists())
        con = sqlite3.connect(self.source)
        con.execute("update items set value='live'")
        con.commit()
        con.close()
        self.tool('restore')
        with closing(sqlite3.connect(self.source)) as connection:
            self.assertEqual(connection.execute('select value from items').fetchone()[0], 'live')
        self.source.unlink()
        self.tool('restore')
        with closing(sqlite3.connect(self.source)) as connection:
            self.assertEqual(connection.execute('select value from items').fetchone()[0], 'original')

    def test_missing_snapshot_is_error(self):
        self.source.unlink()
        self.tool('restore', expected=1)
        self.assertFalse(self.source.exists())

    def test_failed_backup_keeps_previous(self):
        self.tool('backup')
        old = self.snapshot.read_bytes()
        self.source.unlink()
        self.tool('backup', expected=1)
        self.assertEqual(self.snapshot.read_bytes(), old)

    def test_path_traversal_rejected(self):
        txt = (self.repo / '.backup.yaml').read_text().replace('data/app.db', '../outside.db')
        (self.repo / '.backup.yaml').write_text(txt)
        self.tool('backup', expected=1)

class GitTests(unittest.TestCase):
    def test_commit_push_and_repeat(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bare = root / 'remote.git'
            repo = root / 'repo'
            subprocess.run(['git', 'init', '--bare', str(bare)], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(['git', 'init', '-b', 'master', str(repo)], check=True, stdout=subprocess.DEVNULL)
            def git(*args):
                return subprocess.run(['git', *args], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()
            git('config', 'user.email', 'backup@example.test')
            git('config', 'user.name', 'Backup Test')
            git('config', 'commit.gpgsign', 'false')
            git('config', 'core.hooksPath', str(repo / '.git/hooks'))
            (repo / 'data').mkdir()
            db = repo / 'data/db.sqlite3'
            con = sqlite3.connect(db)
            con.execute('create table v (x integer)')
            con.execute('insert into v values (1)')
            con.commit()
            con.close()
            (repo / '.gitignore').write_text('/data/db.sqlite3\n')
            (repo / '.backup.yaml').write_text('''version: 1
databases:
  - type: sqlite
    source: data/db.sqlite3
    snapshot: data/backup.sqlite3
git:
  branch: master
  paths:
    - data/backup.sqlite3
    - .backup.yaml
deploy:
  enabled: false
''')
            git('add', '.backup.yaml', '.gitignore')
            git('commit', '-m', 'initial')
            git('remote', 'add', 'origin', str(bare))
            git('push', '-u', 'origin', 'master')
            for _ in range(2):
                p = subprocess.run([sys.executable, str(TOOL), '--repo', str(repo), 'backup'], capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertTrue((repo / 'data/backup.sqlite3').exists())
            self.assertEqual(git('rev-list', '--count', 'HEAD'), '2')

if __name__ == '__main__':
    unittest.main()
