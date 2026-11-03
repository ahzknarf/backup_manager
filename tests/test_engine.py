import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import backup as bm

BASE = Path(__file__).resolve().parents[1]


class EngineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / 'data').mkdir()
        self.source = self.root / 'data/live.db'
        self.snapshot = self.root / 'data/snapshot.db'
        self.config = {
            'version': 1,
            'databases': [{'type': 'sqlite', 'source': 'data/live.db', 'snapshot': 'data/snapshot.db'}],
            'git': {'enabled': False}, 'deploy': {'enabled': False},
        }
        with sqlite3.connect(self.source) as connection:
            connection.execute('create table items (value text)')
            connection.execute("insert into items values ('original')")
        connection.close()
        self.save()

    def save(self):
        (self.root / '.backup.yaml').write_text(json.dumps(self.config))

    def tool(self, *args, expected=0):
        result = subprocess.run([sys.executable, str(BASE / 'backup.py'), '--repo', str(self.root), *args],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def test_cross_database_overwrite_rejected_in_both_orders(self):
        other = self.root / 'data/other.db'
        other.write_bytes(self.source.read_bytes())
        original = self.source.read_bytes()
        specs = [self.config['databases'][0],
                 {'type': 'sqlite', 'source': 'data/other.db', 'snapshot': 'data/live.db'}]
        for order in (specs, list(reversed(specs))):
            with self.subTest(order=order):
                self.config['databases'] = order
                self.save()
                self.assertIn('overlapping', self.tool('backup', expected=1))
                self.assertEqual(self.source.read_bytes(), original)
                self.assertFalse(self.snapshot.exists())

    def test_internal_symlink_alias_and_sidecar_conflicts_rejected(self):
        (self.root / 'alias').symlink_to(self.root / 'data', target_is_directory=True)
        self.config['databases'][0]['snapshot'] = 'alias/live.db'
        self.save()
        self.assertIn('overlapping', self.tool('backup', expected=1))
        for source, snapshot in [('data/live.db', 'data/live.db-wal'), ('data/live.db-wal', 'data/live.db')]:
            with self.subTest(source=source):
                self.config['databases'][0].update(source=source, snapshot=snapshot)
                self.save()
                self.assertIn('overlapping', self.tool('check', expected=1))

    def test_reserved_destinations_rejected(self):
        for destination in ('.backup.yaml', 'docker-compose.yaml', '.backup-manager.lock', '.git/config'):
            with self.subTest(destination=destination):
                self.config['databases'][0]['snapshot'] = destination
                self.save()
                self.tool('backup', expected=1)

    def test_symlink_to_git_metadata_cannot_be_a_snapshot(self):
        metadata = self.root / '.git'
        metadata.mkdir()
        (metadata / 'config').write_text('preserve-metadata')
        (self.root / 'metadata-alias').symlink_to(metadata, target_is_directory=True)
        self.config['databases'][0]['snapshot'] = 'metadata-alias/config'
        self.save()
        self.assertIn('Git metadata', self.tool('backup', expected=1))
        self.assertEqual((metadata / 'config').read_text(), 'preserve-metadata')

    def test_configuration_types_and_unknown_keys_rejected(self):
        invalid = [
            {'version': True}, {'git': []}, {'deploy': None}, {'restore': False},
            {'git': {'enabled': 'false'}}, {'deploy': {'enabled': 0}},
            {'databases': {}}, {'databsaes': []},
        ]
        original = dict(self.config)
        for change in invalid:
            with self.subTest(change=change):
                self.config = {**original, **change}
                self.save()
                self.tool('check', expected=1)
        for timeout in (0, -1, True, '10', float('inf')):
            self.config = {**original, 'databases': [{**original['databases'][0], 'timeout_seconds': timeout}]}
            self.save()
            self.tool('check', expected=1)

    def test_duplicate_yaml_keys_rejected(self):
        (self.root / '.backup.yaml').write_text('version: 1\ngit: {enabled: false}\ngit: {enabled: true}\n')
        self.assertIn('Duplicate YAML key', self.tool('check', expected=1))

    def test_wal_backups_include_uncheckpointed_commits_for_both_methods(self):
        connection = sqlite3.connect(self.source)
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute("update items set value='committed-in-wal'")
        connection.commit()
        self.assertTrue(Path(str(self.source) + '-wal').is_file())
        for method in ('vacuum', 'backup_api'):
            with self.subTest(method=method):
                self.config['databases'][0]['method'] = method
                self.save()
                self.tool('backup')
                with sqlite3.connect(self.snapshot) as check:
                    self.assertEqual(check.execute('select value from items').fetchone()[0], 'committed-in-wal')
                check.close()
                bm.sqlite_integrity(self.snapshot, standalone=True)

    def test_busy_database_times_out_and_preserves_snapshot(self):
        self.tool('backup')
        previous = self.snapshot.read_bytes()
        blocker = sqlite3.connect(self.source)
        self.addCleanup(blocker.close)
        blocker.execute('BEGIN EXCLUSIVE')
        for method in ('vacuum', 'backup_api'):
            with self.subTest(method=method):
                self.config['databases'][0].update(method=method, timeout_seconds=0.1)
                self.save()
                start = time.monotonic()
                output = self.tool('backup', expected=1)
                self.assertIn('timed out', output)
                self.assertLess(time.monotonic() - start, 3)
                self.assertEqual(self.snapshot.read_bytes(), previous)

    def test_temporary_snapshot_remains_private_even_with_permissive_umask(self):
        integrity = bm.sqlite_integrity

        def verify(path, **kwargs):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            return integrity(path, **kwargs)

        previous_umask = os.umask(0)
        try:
            with mock.patch.object(bm, 'sqlite_integrity', side_effect=verify):
                for method in ('vacuum', 'backup_api'):
                    bm.backup_db(self.root, {**self.config['databases'][0], 'method': method})
        finally:
            os.umask(previous_umask)

    def test_repository_lock_blocks_every_command_before_mutation(self):
        original = self.source.read_bytes()
        with bm.repository_lock(self.root):
            for command in ('backup', 'restore', 'up', 'down', 'check'):
                with self.subTest(command=command):
                    self.assertIn('locked', self.tool(command, expected=1))
            self.assertIn('locked', self.tool('--lock-timeout', '0.05', 'backup', expected=1))
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(self.snapshot.exists())
        self.tool('backup')

    def test_strict_check_requires_snapshot_but_accepts_restore_ready_state(self):
        self.tool('check')
        self.tool('check', '--strict', expected=1)
        self.tool('backup')
        self.source.unlink()
        self.tool('check', '--strict')
        self.snapshot.write_bytes(b'')
        self.assertIn('nonempty SQLite', self.tool('check', '--strict', expected=1))

    def test_orphan_sidecars_and_snapshot_sidecars_block_restore(self):
        self.tool('backup')
        self.source.unlink()
        for suffix in bm.SIDECARS:
            sidecar = Path(str(self.source) + suffix)
            sidecar.symlink_to(self.root / 'nonexistent')
            self.tool('restore', expected=1)
            sidecar.unlink()
        sidecar = Path(str(self.snapshot) + '-wal')
        sidecar.write_bytes(b'')
        self.assertIn('standalone', self.tool('restore', expected=1))
        self.assertFalse(self.source.exists())

    def test_active_snapshot_sidecar_prevents_backup_replacement(self):
        self.tool('backup')
        previous = self.snapshot.read_bytes()
        Path(str(self.snapshot) + '-wal').write_bytes(b'')
        self.assertIn('active sidecars', self.tool('backup', expected=1))
        self.assertEqual(self.snapshot.read_bytes(), previous)

    def test_strict_check_rejects_orphan_source_sidecars(self):
        self.tool('backup')
        self.source.unlink()
        Path(str(self.source) + '-journal').write_bytes(b'')
        self.assertIn('Orphan', self.tool('check', '--strict', expected=1))

    def test_restore_does_not_replace_destination_created_during_copy(self):
        self.tool('backup')
        self.source.unlink()
        real_link = os.link

        def raced_link(source, destination):
            Path(destination).write_bytes(b'created-by-another-process')
            real_link(source, destination)

        with mock.patch.object(bm.os, 'link', side_effect=raced_link):
            with self.assertRaisesRegex(bm.BackupError, 'appeared'):
                bm.restore_all(self.root, self.config)
        self.assertEqual(self.source.read_bytes(), b'created-by-another-process')

    def test_disabled_up_has_no_restore_side_effect_and_down_never_runs_docker(self):
        self.tool('backup')
        self.source.unlink()
        for command in ('up', 'down'):
            with self.subTest(command=command), mock.patch.object(bm, 'run') as run:
                with mock.patch.object(sys, 'argv', ['backup.py', '--repo', str(self.root), command]):
                    with self.assertRaisesRegex(bm.BackupError, 'deploy.enabled=false'):
                        bm.main()
                run.assert_not_called()
                self.assertFalse(self.source.exists())

    def test_compose_states_fail_closed_for_live_or_unknown_containers(self):
        self.tool('backup')
        self.source.unlink()
        self.config['deploy']['enabled'] = True
        (self.root / 'docker-compose.yaml').write_text('services: {}\n')
        for state in ('running', 'paused', 'restarting', 'created', 'removing', 'unknown'):
            for output in (json.dumps([{'State': state}]), json.dumps({'State': state})):
                with self.subTest(state=state, output=output):
                    result = subprocess.CompletedProcess([], 0, output, '')
                    with mock.patch.object(bm, 'run', return_value=result):
                        with self.assertRaises(bm.BackupError):
                            bm.restore_all(self.root, self.config)
                    self.assertFalse(self.source.exists())
        for output in ('[]', '{"State":"exited"}\n{"State":"dead"}\n'):
            result = subprocess.CompletedProcess([], 0, output, '')
            with mock.patch.object(bm, 'run', return_value=result):
                self.assertFalse(bm.compose_running(self.root, self.config))
        with mock.patch.object(bm, 'run', return_value=subprocess.CompletedProcess([], 0, '{"bad":true}', '')):
            with self.assertRaisesRegex(bm.BackupError, 'Cannot parse'):
                bm.compose_running(self.root, self.config)


if __name__ == '__main__':
    unittest.main()
