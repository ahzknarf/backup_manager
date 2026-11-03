#!/usr/bin/env python3
"""Per-repository SQLite backup, restore and Docker Compose management."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote

VERSION = '0.2.0'
LOCK_FILE = '.backup-manager.lock'
SIDECARS = ('-wal', '-shm', '-journal')

class BackupError(Exception):
    pass

def log(msg):
    print(f'[{dt.datetime.now().astimezone().isoformat(timespec="seconds")}] {msg}', flush=True)

def run(argv, cwd, *, capture=True, timeout=300, check=True, env=None):
    try:
        p = subprocess.run(argv, cwd=str(cwd), text=True, stdout=subprocess.PIPE if capture else None,
                           stderr=subprocess.PIPE if capture else None, timeout=timeout, check=False,
                           env={**os.environ, **env} if env is not None else None)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BackupError(f'Command failed: {argv[0]} ({e})') from e
    if check and p.returncode:
        raise BackupError(f'Command {argv[0]} returned {p.returncode}: {(p.stderr or p.stdout or "").strip()}')
    return p

def relative_path(root, value):
    if (not isinstance(value, str) or not value or value.startswith('-')
            or any(c in value for c in '\x00\n\r\\')):
        raise BackupError(f'Invalid relative path: {value!r}')
    p = Path(value)
    if p.is_absolute() or '..' in p.parts or '.git' in p.parts or p == Path('.'):
        raise BackupError(f'Path must be inside repository: {value}')
    result = root / p
    try:
        resolved = result.resolve(strict=False)
    except RuntimeError as e:
        raise BackupError(f'Invalid symlink path: {value}') from e
    if not resolved.is_relative_to(root.resolve()):
        raise BackupError(f'Path escapes repository through symlink: {value}')
    if resolved.is_relative_to((root / '.git').resolve()):
        raise BackupError(f'Path points into Git metadata: {value}')
    return result

def present(path):
    return path.exists() or path.is_symlink()

def database_paths(root, cfg):
    for spec in cfg.get('databases', []):
        source = relative_path(root, spec['source'])
        for path in dict.fromkeys((source, source.resolve())):
            yield path
            for suffix in SIDECARS:
                yield path.with_name(path.name + suffix)

def section(data, name, keys):
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise BackupError(f'{name} must be a mapping')
    unknown = value.keys() - keys
    if unknown:
        raise BackupError(f'Unknown {name} setting(s): {", ".join(map(str, unknown))}')
    if 'enabled' in value and type(value['enabled']) is not bool:
        raise BackupError(f'{name}.enabled must be true or false')
    data[name] = value
    return value

def positive_seconds(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise BackupError(f'{label} must be a positive finite number')
    return value

def git_paths(root, cfg):
    # Snapshot/config paths are always included. git.paths only adds explicit files.
    values = [s['snapshot'] for s in cfg.get('databases', [])] + ['.backup.yaml']
    values += cfg.get('git', {}).get('paths', [])
    paths = []
    runtime = {p.resolve() for p in database_paths(root, cfg)}
    runtime.add(root / LOCK_FILE)
    for value in values:
        path = relative_path(root, value)
        if any(c in value for c in ':*?[]'):
            raise BackupError(f'git.paths accepts literal files only: {value}')
        if path.is_dir():
            raise BackupError(f'git.paths cannot include a directory: {value}')
        if path.resolve() in runtime:
            raise BackupError(f'git.paths cannot include a live database or runtime file: {value}')
        normalized = path.relative_to(root).as_posix()
        if normalized not in paths:
            paths.append(normalized)
    return paths

def load_config(root):
    cfg_file = root / '.backup.yaml'
    if not cfg_file.is_file():
        raise BackupError(f'Missing {cfg_file}')
    try:
        import yaml
    except ImportError as e:
        raise BackupError('PyYAML missing. Install with: python3 -m pip install "PyYAML>=6,<7"') from e
    try:
        class UniqueLoader(yaml.SafeLoader):
            pass

        def unique_mapping(loader, node):
            loader.flatten_mapping(node)
            result = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node)
                if key in result:
                    raise ValueError(f'Duplicate YAML key: {key}')
                result[key] = loader.construct_object(value_node)
            return result

        UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
        data = yaml.load(cfg_file.read_text(encoding='utf-8'), Loader=UniqueLoader)
    except Exception as e:
        raise BackupError(f'Invalid YAML: {e}') from e
    if not isinstance(data, dict) or type(data.get('version')) is not int or data.get('version') != 1:
        raise BackupError('Expected .backup.yaml with version: 1')
    databases = data.get('databases', [])
    if not isinstance(databases, list):
        raise BackupError('databases must be a list')
    unknown = data.keys() - {'version', 'databases', 'restore', 'git', 'deploy'}
    if unknown:
        raise BackupError(f'Unknown configuration setting(s): {", ".join(map(str, unknown))}')
    restore = section(data, 'restore', {'policy'})
    settings = section(data, 'git', {'enabled', 'branch', 'remote', 'message', 'paths'})
    deploy = section(data, 'deploy', {'enabled', 'compose_file'})
    compose_file = relative_path(root, deploy.get('compose_file', 'docker-compose.yaml'))
    targets = [(root / name).resolve() for name in ('.backup.yaml', LOCK_FILE)]
    targets.append(compose_file.resolve())
    for d in databases:
        if not isinstance(d, dict) or d.get('type') != 'sqlite':
            raise BackupError('MVP supports type: sqlite only')
        if d.keys() - {'type', 'source', 'snapshot', 'method', 'restore_mode',
                       'restore_uid', 'restore_gid', 'timeout_seconds'}:
            raise BackupError('Unknown SQLite setting')
        if d.get('method', 'vacuum') not in ('vacuum', 'backup_api'):
            raise BackupError('Supported methods: vacuum, backup_api')
        src = relative_path(root, d.get('source'))
        dst = relative_path(root, d.get('snapshot'))
        for path in (src, dst):
            canonical = path.resolve()
            family = [canonical] + [canonical.with_name(canonical.name + suffix) for suffix in SIDECARS]
            for member in family:
                for previous in targets:
                    if (member.is_relative_to(previous) or previous.is_relative_to(member)
                            or (member.exists() and previous.exists() and member.samefile(previous))):
                        raise BackupError(f'Duplicate or overlapping SQLite paths: {path}')
            targets.extend(family)
            if path.is_dir():
                raise BackupError(f'SQLite path must be a file: {path}')
        positive_seconds(d.get('timeout_seconds', 300), 'SQLite timeout_seconds')
        mode = d.get('restore_mode')
        if mode is not None and (not isinstance(mode, str) or not re.fullmatch('[0-7]{3,4}', mode)):
            raise BackupError('restore_mode must be octal string such as "0600"')
        for key in ('restore_uid', 'restore_gid'):
            if key in d and (type(d[key]) is not int or d[key] < 0):
                raise BackupError(f'{key} must be nonnegative integer')
    if restore.get('policy', 'if_missing') != 'if_missing':
        raise BackupError('MVP supports only restore.policy: if_missing (never overwrite)')
    if settings.get('enabled', True):
        branch = settings.get('branch', 'master')
        if not isinstance(branch, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', branch) or '..' in branch:
            raise BackupError('Invalid Git branch')
        remote = settings.get('remote', 'origin')
        if not isinstance(remote, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', remote):
            raise BackupError('Invalid Git remote name')
        message = settings.get('message', 'backup: snapshot')
        if not isinstance(message, str) or not message.strip() or '\x00' in message:
            raise BackupError('git.message must be a nonempty string')
        paths = settings.get('paths', [])
        if not isinstance(paths, list):
            raise BackupError('git.paths must be a list')
        git_paths(root, data)
    return data

@contextmanager
def repository_lock(root, *, timeout=0):
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(root / LOCK_FILE, flags, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BackupError(f'Another task is running for {root}; repository is locked')
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        yield
    finally:
        # Keep the lock inode in place: unlinking it permits two independent locks.
        os.close(fd)

class Deadline:
    def __init__(self, seconds):
        self.end = time.monotonic() + seconds

    def check(self):
        if time.monotonic() >= self.end:
            raise BackupError('SQLite operation timed out')

    def progress(self):
        return int(time.monotonic() >= self.end)

    def connect(self, path):
        self.check()
        connection = sqlite3.connect(f'file:{quote(str(path))}?mode=ro', uri=True,
                                     timeout=min(1, max(0, self.end - time.monotonic())))
        connection.set_progress_handler(self.progress, 1000)
        return connection

def sqlite_header(db):
    with open(db, 'rb') as handle:
        if handle.read(16) != b'SQLite format 3\x00':
            raise BackupError(f'Not a nonempty SQLite database: {db}')

def sqlite_integrity(db, *, deadline=None, standalone=False):
    sqlite_header(db)
    if standalone and any(present(db.with_name(db.name + suffix)) for suffix in SIDECARS):
        raise BackupError(f'Snapshot has WAL/SHM/journal sidecars; expected a standalone database: {db}')
    deadline = deadline or Deadline(30)
    conn = deadline.connect(db)
    try:
        result = conn.execute('PRAGMA quick_check').fetchall()
        deadline.check()
        if result != [('ok',)]:
            raise BackupError(f'SQLite check failed for {db}: {result}')
    finally:
        conn.close()

def temporary_in_parent(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    os.close(fd)
    # VACUUM INTO accepts an empty existing file. Keep mkstemp's 0600 mode
    # and reserved name while SQLite writes potentially sensitive contents.
    return Path(name)

def durable_replace(source, target):
    os.replace(source, target)
    fd = os.open(str(target.parent), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def backup_db(root, spec):
    source = relative_path(root, spec['source'])
    target = relative_path(root, spec['snapshot'])
    if not source.is_file():
        raise BackupError(f'Database missing: {source}')
    if any(present(target.with_name(target.name + suffix)) for suffix in SIDECARS):
        raise BackupError(f'Snapshot has active sidecars; refusing replacement: {target}')
    sqlite_header(source)
    deadline = Deadline(spec.get('timeout_seconds', 300))
    temp = temporary_in_parent(target)
    method = spec.get('method', 'vacuum')
    try:
        # Source connection is read-only and receives a consistent SQLite snapshot.
        conn = deadline.connect(source)
        try:
            if method == 'vacuum':
                conn.execute('VACUUM INTO ?', (str(temp),))
            else:
                dst = sqlite3.connect(str(temp))
                try:
                    conn.backup(dst, pages=100, sleep=0.05,
                                progress=lambda *_: deadline.check())
                finally:
                    dst.close()
        finally:
            conn.close()
        sqlite_integrity(temp, deadline=deadline, standalone=True)
        if target.is_file():
            os.chmod(temp, target.stat().st_mode & 0o777)
        else:
            os.chmod(temp, 0o600)
        with open(temp, 'rb') as f:
            os.fsync(f.fileno())
        deadline.check()
        if any(present(target.with_name(target.name + suffix)) for suffix in SIDECARS):
            raise BackupError(f'Snapshot sidecar appeared during backup: {target}')
        durable_replace(temp, target)
        log(f'BACKUP OK: {spec["source"]} -> {spec["snapshot"]}')
    except sqlite3.Error:
        deadline.check()
        raise
    finally:
        if temp.exists():
            temp.unlink()

def backup_all(root, cfg):
    failures = []
    for spec in cfg.get('databases', []):
        try:
            backup_db(root, spec)
        except Exception as e:
            log(f'BACKUP FAILED: {spec.get("source")}: {e}')
            failures.append(str(e))
    if failures:
        raise BackupError(f'{len(failures)} database backup(s) failed; skipping Git commit')

def compose_command(root, cfg):
    deploy = cfg.get('deploy', {}) or {}
    if not deploy.get('enabled', True):
        raise BackupError('deploy.enabled=false; deployment commands are disabled')
    path = relative_path(root, deploy.get('compose_file', 'docker-compose.yaml'))
    if not path.is_file():
        raise BackupError(f'Compose file missing: {path}')
    return ['docker', 'compose', '-f', str(path)]

def compose_running(root, cfg):
    deploy = cfg.get('deploy', {}) or {}
    if not deploy.get('enabled', True):
        return False
    command = compose_command(root, cfg)
    try:
        p = run(command + ['ps', '--all', '--format', 'json'], root, timeout=30, check=False)
    except BackupError as e:
        raise BackupError(f'Cannot determine Docker service state; refusing restore: {e}') from e
    if p.returncode:
        raise BackupError(f'Cannot determine Docker service state; refusing restore: {(p.stderr or "").strip()}')
    try:
        text = p.stdout.strip()
        if not text:
            return False
        if text.startswith('['):
            containers = json.loads(text)
        else:
            containers = [json.loads(line) for line in text.splitlines()]
        if not isinstance(containers, list) or any(not isinstance(c, dict) or not c.get('State') for c in containers):
            raise ValueError('Missing container State')
        return any(c['State'] not in ('exited', 'dead') for c in containers)
    except (ValueError, TypeError) as e:
        raise BackupError(f'Cannot parse Docker service state; refusing restore: {e}') from e

def restore_all(root, cfg, *, dry_run=False):
    specs = cfg.get('databases', [])
    missing = []
    for spec in specs:
        target = relative_path(root, spec['source'])
        snapshot = relative_path(root, spec['snapshot'])
        if target.exists():
            if not target.is_file():
                raise BackupError(f'Target exists but is not a file: {target}')
            log(f'SKIP: database exists: {spec["source"]}')
            continue
        if target.is_symlink() or any(present(target.with_name(target.name + suffix)) for suffix in SIDECARS):
            raise BackupError(f'Orphan WAL/SHM file or symlink at target: {target}')
        if not snapshot.is_file():
            raise BackupError(f'Snapshot missing: {snapshot}; will not start with empty DB')
        sqlite_integrity(snapshot, standalone=True)
        missing.append((spec, target, snapshot))
    if not missing:
        log('RESTORE: nothing missing')
        return
    if compose_running(root, cfg):
        raise BackupError('Docker Compose service is running; refusing to restore missing database')
    for spec, target, snapshot in missing:
        if dry_run:
            log(f'DRY RUN: restore {snapshot} -> {target}')
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmpname = tempfile.mkstemp(prefix=f'.{target.name}.', suffix='.restore.tmp', dir=target.parent)
        tmp = Path(tmpname)
        try:
            with os.fdopen(fd, 'wb') as out, open(snapshot, 'rb') as src:
                shutil.copyfileobj(src, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            sqlite_integrity(tmp, standalone=True)
            os.chmod(tmp, int(spec.get('restore_mode', '0600'), 8))
            if 'restore_uid' in spec or 'restore_gid' in spec:
                os.chown(tmp, spec.get('restore_uid', -1), spec.get('restore_gid', -1))
            if any(present(target.with_name(target.name + suffix)) for suffix in SIDECARS):
                raise BackupError(f'Database sidecar appeared during restore: {target}')
            # Never replace a target created in the meantime. Hard-link is atomic no-clobber.
            os.link(tmp, target)
            dirfd = os.open(str(target.parent), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
            log(f'RESTORE OK: {spec["snapshot"]} -> {spec["source"]}')
        except FileExistsError as e:
            raise BackupError(f'Restore destination appeared during restore: {target}') from e
        finally:
            tmp.unlink(missing_ok=True)

def git_run(root, *args, **kwargs):
    env = {'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0', **kwargs.pop('env', {})}
    return run(['git', '--literal-pathspecs', *args], root, env=env, **kwargs)

def git_output(root, *args, **kwargs):
    return git_run(root, *args, **kwargs).stdout.strip()

def git_internal_path(root, name):
    return (root / git_output(root, 'rev-parse', '--git-path', name)).resolve()

def ensure_git_excludes(root, cfg):
    # Local generated exclusions keep the service's backup settings in one file.
    path = git_internal_path(root, 'info/exclude')
    old = path.read_text(encoding='utf-8') if path.exists() else ''
    start, end = '# BEGIN backup-manager (generated)', '# END backup-manager'
    if old.count(start) != old.count(end) or old.count(start) > 1:
        raise BackupError(f'Malformed backup-manager exclusion block: {path}')
    if start in old:
        before, rest = old.split(start, 1)
        _, after = rest.split(end, 1)
        old = before.rstrip('\n') + after

    def escape(value):
        return ''.join('\\' + c if c in '\\*?[]!# ' else c for c in value)

    patterns = ['/' + escape(p.relative_to(root).as_posix()) for p in database_paths(root, cfg)]
    patterns.append('/' + LOCK_FILE)
    for spec in cfg.get('databases', []):
        for key, suffix in (('snapshot', '*.tmp'), ('source', '*.restore.tmp')):
            p = relative_path(root, spec[key])
            parent = p.parent.relative_to(root).as_posix()
            prefix = '' if parent == '.' else escape(parent) + '/'
            patterns.append('/' + prefix + '.' + escape(p.name) + '.' + suffix)
    text = old.rstrip('\n') + '\n' + start + '\n' + '\n'.join(dict.fromkeys(patterns)) + '\n' + end + '\n'
    if path.exists() and text == path.read_text(encoding='utf-8'):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.exclude.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        durable_replace(Path(name), path)
    finally:
        Path(name).unlink(missing_ok=True)

def git_preflight(root, cfg):
    settings = cfg.get('git', {})
    if not settings.get('enabled', True):
        return None
    if Path(git_output(root, 'rev-parse', '--show-toplevel')).resolve() != root:
        raise BackupError('Repository root mismatch; run from Git repository root')
    branch = settings.get('branch', 'master')
    git_run(root, 'check-ref-format', '--branch', branch)
    actual = git_output(root, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False)
    if actual != branch:
        raise BackupError(f'Expected branch {branch}, found {actual or "detached HEAD"}')
    if present(git_internal_path(root, 'index.lock')):
        raise BackupError('Git index is locked by another operation')
    for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply'):
        if git_internal_path(root, name).exists():
            raise BackupError('Git operation in progress; finish it before backing up')
    if git_run(root, 'diff', '--cached', '--quiet', check=False).returncode != 0:
        raise BackupError('Existing staged changes; refusing automatic Git commit')
    git_output(root, 'var', 'GIT_AUTHOR_IDENT')
    git_output(root, 'var', 'GIT_COMMITTER_IDENT')
    runtime = [p.relative_to(root).as_posix() for p in database_paths(root, cfg)] + [LOCK_FILE]
    tracked = git_output(root, 'ls-files', '-z', '--', *runtime)
    if tracked:
        raise BackupError('Runtime files are already tracked by Git; remove them from the index manually: '
                          + tracked.replace('\x00', ', '))
    git_paths(root, cfg)
    ensure_git_excludes(root, cfg)
    git_run(root, 'fetch', '--no-tags', settings.get('remote', 'origin'),
            f'refs/heads/{branch}', timeout=120)
    remote_sha = git_output(root, 'rev-parse', 'FETCH_HEAD')
    local_sha = git_output(root, 'rev-parse', 'HEAD')
    base = git_output(root, 'merge-base', local_sha, remote_sha)
    if base != remote_sha:
        raise BackupError('Remote is ahead/diverged. Manual fast-forward or merge required; will not pull automatically')
    return local_sha

@contextmanager
def backup_index(root):
    # Reserve Git's normal index lock. Git writes to an alternate index, leaving
    # the user's index byte-for-byte intact on staging or commit failure.
    index = git_internal_path(root, 'index')
    lock = index.with_name(index.name + '.lock')
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as e:
        raise BackupError('Git index is locked by another operation') from e
    os.close(fd)
    try:
        if git_run(root, 'diff', '--cached', '--quiet', check=False).returncode != 0:
            raise BackupError('Existing staged changes; refusing automatic Git commit')
        env = {'GIT_INDEX_FILE': str(lock)}
        if index.exists():
            shutil.copyfile(index, lock)
        else:
            git_run(root, 'read-tree', 'HEAD', env=env)
        yield env
    finally:
        lock.unlink(missing_ok=True)

def git_commit_push(root, cfg, *, expected_head=None):
    settings = cfg.get('git', {})
    if not settings.get('enabled', True):
        log('GIT: disabled')
        return
    if expected_head is None:
        expected_head = git_preflight(root, cfg)
    if (git_output(root, 'rev-parse', 'HEAD') != expected_head
            or git_output(root, 'symbolic-ref', '--short', 'HEAD') != settings.get('branch', 'master')):
        raise BackupError('Git HEAD changed during backup; refusing automatic commit')
    with backup_index(root) as env:
        git_run(root, 'add', '-A', '--', *git_paths(root, cfg), env=env)
        status = git_run(root, 'diff', '--cached', '--quiet', check=False, env=env)
        if status.returncode == 1:
            git_run(root, 'commit', '-m', settings.get('message', 'backup: snapshot'), env=env)
            durable_replace(Path(env['GIT_INDEX_FILE']), git_internal_path(root, 'index'))
            log('GIT: created commit')
        elif status.returncode != 0:
            raise BackupError('Failed to check Git staging status')
        else:
            log('GIT: no new tracked changes')
    # Retry earlier committed backups even when the snapshot is unchanged.
    branch = settings.get('branch', 'master')
    git_run(root, 'push', settings.get('remote', 'origin'), f'HEAD:refs/heads/{branch}', timeout=180)
    log(f'GIT: pushed {git_output(root, "rev-parse", "HEAD")}')

def check_all(root, cfg, *, strict=False):
    if strict and not cfg.get('databases'):
        raise BackupError('Strict check requires at least one database')
    for spec in cfg.get('databases', []):
        source = relative_path(root, spec['source'])
        snapshot = relative_path(root, spec['snapshot'])
        if snapshot.is_file():
            sqlite_integrity(snapshot, standalone=True)
            log(f'CHECK: snapshot OK {spec["snapshot"]}')
        elif strict or present(snapshot):
            raise BackupError(f'Snapshot missing or not a file: {snapshot}')
        else:
            log(f'CHECK: no snapshot yet {spec["snapshot"]}')
        if source.is_file():
            sqlite_integrity(source)
        elif present(source):
            raise BackupError(f'Source is not a file: {source}')
        elif any(present(source.with_name(source.name + suffix)) for suffix in SIDECARS):
            raise BackupError(f'Orphan database sidecar: {source}')
        log(f'CHECK: source {"exists" if source.exists() else "absent"}: {spec["source"]}')
    if strict and cfg.get('deploy', {}).get('enabled', True):
        compose_command(root, cfg)
    log('CHECK OK')

def main():
    parser = argparse.ArgumentParser(description='Per-repository SQLite backup and restore tool')
    parser.add_argument('--repo', default='.', help='Git/service repository root')
    parser.add_argument('--lock-timeout', type=float, default=0, help='Seconds to wait for another task (default: fail immediately)')
    parser.add_argument('--version', action='version', version=VERSION)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('backup', help='SQLite snapshot + optional Git commit and push')
    p = sub.add_parser('restore', help='Restore only missing databases, no overwrites')
    p.add_argument('--dry-run', action='store_true')
    sub.add_parser('up', help='Restore missing DBs, then docker compose up -d')
    sub.add_parser('down', help='docker compose down (volumes kept)')
    p = sub.add_parser('check', help='Validate paths and snapshots')
    p.add_argument('--strict', action='store_true', help='Require verified snapshots for every database')
    args = parser.parse_args()
    root = Path(args.repo).expanduser().resolve()
    if not root.is_dir():
        raise BackupError(f'Repository does not exist: {root}')
    if not math.isfinite(args.lock_timeout) or args.lock_timeout < 0:
        raise BackupError('--lock-timeout must be a nonnegative finite number')
    with repository_lock(root, timeout=args.lock_timeout):
        cfg = load_config(root)
        log(f'{args.command.upper()} repo={root}')
        if args.command == 'backup':
            head = git_preflight(root, cfg)
            backup_all(root, cfg)
            git_commit_push(root, cfg, expected_head=head)
        elif args.command in ('restore', 'up'):
            command = compose_command(root, cfg) if args.command == 'up' else None
            restore_all(root, cfg, dry_run=getattr(args, 'dry_run', False))
            if command:
                run(command + ['up', '-d'], root, capture=False, timeout=300)
        elif args.command == 'down':
            run(compose_command(root, cfg) + ['down'], root, capture=False)
        elif args.command == 'check':
            check_all(root, cfg, strict=args.strict)

if __name__ == '__main__':
    try:
        main()
    except (BackupError, OSError, sqlite3.Error) as e:
        log(f'ERROR: {e}')
        sys.exit(1)
