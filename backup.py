#!/usr/bin/env python3
"""Standalone per-repository backup/restore/deploy engine (v0.1.0).
Requires PyYAML (pip install 'PyYAML>=6,<7'). Remote install is handled by service.sh only for tool delivery, not dependencies.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
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

VERSION = '0.1.0'

class BackupError(Exception):
    pass

def log(msg):
    print(f'[{dt.datetime.now().astimezone().isoformat(timespec="seconds")}] {msg}', flush=True)

def run(argv, cwd, *, capture=True, timeout=300, check=True):
    try:
        p = subprocess.run(argv, cwd=str(cwd), text=True, stdout=subprocess.PIPE if capture else None,
                           stderr=subprocess.PIPE if capture else None, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BackupError(f'Command failed: {argv[0]} ({e})') from e
    if check and p.returncode:
        raise BackupError(f'Command {argv[0]} returned {p.returncode}: {(p.stderr or p.stdout or "").strip()}')
    return p

def relative_path(root, value):
    if not isinstance(value, str) or not value or value.startswith('-'):
        raise BackupError(f'Invalid relative path: {value!r}')
    p = Path(value)
    if p.is_absolute() or '..' in p.parts:
        raise BackupError(f'Path must be inside repository: {value}')
    result = root / p
    if not result.resolve(strict=False).is_relative_to(root.resolve()):
        raise BackupError(f'Path escapes repository through symlink: {value}')
    return result

def load_config(root):
    cfg_file = root / '.backup.yaml'
    if not cfg_file.is_file():
        raise BackupError(f'Missing {cfg_file}')
    try:
        import yaml
    except ImportError as e:
        raise BackupError('PyYAML missing. Install with: python3 -m pip install "PyYAML>=6,<7"') from e
    try:
        data = yaml.safe_load(cfg_file.read_text(encoding='utf-8'))
    except Exception as e:
        raise BackupError(f'Invalid YAML: {e}') from e
    if not isinstance(data, dict) or data.get('version') != 1:
        raise BackupError('Expected .backup.yaml with version: 1')
    databases = data.get('databases', [])
    if not isinstance(databases, list):
        raise BackupError('databases must be a list')
    targets = set()
    for d in databases:
        if not isinstance(d, dict) or d.get('type') != 'sqlite':
            raise BackupError('MVP supports type: sqlite only')
        if d.get('method', 'vacuum') not in ('vacuum', 'backup_api'):
            raise BackupError('Supported methods: vacuum, backup_api')
        src = relative_path(root, d.get('source'))
        dst = relative_path(root, d.get('snapshot'))
        if src == dst or dst in targets or src in targets:
            raise BackupError('Duplicate or overlapping SQLite paths')
        targets.add(dst)
        mode = d.get('restore_mode')
        if mode is not None and (not isinstance(mode, str) or not re.fullmatch('[0-7]{3,4}', mode)):
            raise BackupError('restore_mode must be octal string such as "0600"')
        for key in ('restore_uid', 'restore_gid'):
            if key in d and (not isinstance(d[key], int) or d[key] < 0):
                raise BackupError(f'{key} must be nonnegative integer')
    restore = data.get('restore', {}) or {}
    if restore.get('policy', 'if_missing') != 'if_missing':
        raise BackupError('MVP supports only restore.policy: if_missing (never overwrite)')
    git = data.get('git', {}) or {}
    if git.get('enabled', True):
        branch = git.get('branch', 'master')
        if not isinstance(branch, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', branch) or '..' in branch:
            raise BackupError('Invalid Git branch')
        paths = git.get('paths', [d['snapshot'] for d in databases] + ['.backup.yaml'])
        if not isinstance(paths, list):
            raise BackupError('git.paths must be a list')
        for path in paths:
            relative_path(root, path)
    compose = data.get('deploy', {}) or {}
    if compose.get('enabled', True):
        relative_path(root, compose.get('compose_file', 'docker-compose.yaml'))
    return data

def sqlite_integrity(db):
    uri = f'file:{quote(str(db))}?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        result = conn.execute('PRAGMA quick_check').fetchone()
        if not result or result[0] != 'ok':
            raise BackupError(f'SQLite check failed for {db}: {result}')
    finally:
        conn.close()

def temporary_in_parent(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    os.close(fd)
    os.unlink(name)  # VACUUM INTO requires target not to exist
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
    temp = temporary_in_parent(target)
    method = spec.get('method', 'vacuum')
    try:
        # Source connection is read-only and receives a consistent SQLite snapshot.
        conn = sqlite3.connect(f'file:{quote(str(source))}?mode=ro', uri=True, timeout=30)
        try:
            conn.execute('PRAGMA busy_timeout=30000')
            if method == 'vacuum':
                conn.execute('VACUUM INTO ?', (str(temp),))
            else:
                dst = sqlite3.connect(str(temp))
                try:
                    conn.backup(dst, pages=100, sleep=0.25)
                finally:
                    dst.close()
        finally:
            conn.close()
        sqlite_integrity(temp)
        with open(temp, 'rb') as f:
            os.fsync(f.fileno())
        if target.is_file():
            os.chmod(temp, target.stat().st_mode & 0o777)
        else:
            os.chmod(temp, 0o600)
        durable_replace(temp, target)
        log(f'BACKUP OK: {spec["source"]} -> {spec["snapshot"]}')
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
    return ['docker', 'compose', '-f', str(relative_path(root, deploy.get('compose_file', 'docker-compose.yaml')))]

def compose_running(root, cfg):
    deploy = cfg.get('deploy', {}) or {}
    if not deploy.get('enabled', True):
        return False
    command = compose_command(root, cfg)
    try:
        p = run(command + ['ps', '--status', 'running', '--services'], root, timeout=30, check=False)
    except BackupError as e:
        raise BackupError(f'Cannot determine Docker service state; refusing restore: {e}') from e
    if p.returncode:
        raise BackupError(f'Cannot determine Docker service state; refusing restore: {(p.stderr or "").strip()}')
    return bool(p.stdout.strip())

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
        if target.is_symlink() or target.with_name(target.name + '-wal').exists() or target.with_name(target.name + '-shm').exists():
            raise BackupError(f'Orphan WAL/SHM file or symlink at target: {target}')
        if not snapshot.is_file():
            raise BackupError(f'Snapshot missing: {snapshot}; will not start with empty DB')
        sqlite_integrity(snapshot)
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
            sqlite_integrity(tmp)
            os.chmod(tmp, int(spec.get('restore_mode', '0600'), 8))
            if 'restore_uid' in spec or 'restore_gid' in spec:
                os.chown(tmp, spec.get('restore_uid', -1), spec.get('restore_gid', -1))
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

def git_output(root, *args, check=True):
    return run(['git', *args], root, check=check).stdout.strip()

def git_commit_push(root, cfg):
    settings = cfg.get('git', {}) or {}
    if not settings.get('enabled', True):
        log('GIT: disabled')
        return
    branch = settings.get('branch', 'master')
    remote = settings.get('remote', 'origin')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', remote):
        raise BackupError('Invalid Git remote name')
    if git_output(root, 'rev-parse', '--show-toplevel') != str(root.resolve()):
        raise BackupError('Repository root mismatch; run from Git repository root')
    head_branch = git_output(root, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False)
    if head_branch != branch:
        raise BackupError(f'Expected branch {branch}, found {head_branch or "detached HEAD"}')
    # Disallow preexisting staged changes that could accidentally be included in backup commit.
    if run(['git', 'diff', '--cached', '--quiet'], root, check=False).returncode != 0:
        raise BackupError('Existing staged changes; refusing automatic Git commit')
    run(['git', 'fetch', remote, branch], root, timeout=120)
    remote_sha = git_output(root, 'rev-parse', 'FETCH_HEAD')
    local_sha = git_output(root, 'rev-parse', 'HEAD')
    base = git_output(root, 'merge-base', local_sha, remote_sha)
    if base != remote_sha:
        raise BackupError('Remote is ahead/diverged. Manual fast-forward or merge required; will not pull automatically')
    paths = settings.get('paths', [s['snapshot'] for s in cfg.get('databases', [])] + ['.backup.yaml'])
    if paths:
        run(['git', 'add', '-A', '--', *paths], root)
    status = run(['git', 'diff', '--cached', '--quiet'], root, check=False)
    if status.returncode == 1:
        run(['git', 'commit', '-m', settings.get('message', 'backup: snapshot')], root)
        log('GIT: created commit')
    elif status.returncode != 0:
        raise BackupError('Failed to check Git staging status')
    else:
        log('GIT: no new tracked changes')
    # Push even if there is no new commit, allowing retry after prior network failure.
    run(['git', 'push', remote, f'HEAD:refs/heads/{branch}'], root, timeout=180)
    log(f'GIT: pushed {git_output(root, "rev-parse", "HEAD")}')

def main():
    parser = argparse.ArgumentParser(description='Per-repository SQLite backup and restore tool')
    parser.add_argument('--repo', default='.', help='Git/service repository root')
    parser.add_argument('--version', action='version', version=VERSION)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('backup', help='SQLite snapshot + optional Git commit and push')
    p = sub.add_parser('restore', help='Restore only missing databases, no overwrites')
    p.add_argument('--dry-run', action='store_true')
    sub.add_parser('up', help='Restore missing DBs, then docker compose up -d')
    sub.add_parser('down', help='docker compose down (volumes kept)')
    sub.add_parser('check', help='Validate paths and snapshots')
    args = parser.parse_args()
    root = Path(args.repo).expanduser().resolve()
    if not root.is_dir():
        raise BackupError(f'Repository does not exist: {root}')
    cfg = load_config(root)
    log(f'{args.command.upper()} repo={root}')
    if args.command == 'backup':
        backup_all(root, cfg)
        git_commit_push(root, cfg)
    elif args.command in ('restore', 'up'):
        restore_all(root, cfg, dry_run=getattr(args, 'dry_run', False))
        if args.command == 'up':
            if not (cfg.get('deploy', {}) or {}).get('enabled', True):
                raise BackupError('deploy.enabled=false; cannot up')
            run(compose_command(root, cfg) + ['up', '-d'], root, capture=False, timeout=300)
    elif args.command == 'down':
        run(compose_command(root, cfg) + ['down'], root, capture=False)
    elif args.command == 'check':
        for d in cfg.get('databases', []):
            src = relative_path(root, d['source'])
            snap = relative_path(root, d['snapshot'])
            if snap.is_file():
                sqlite_integrity(snap)
                log(f'CHECK: snapshot OK {d["snapshot"]}')
            else:
                log(f'CHECK: no snapshot yet {d["snapshot"]}')
            log(f'CHECK: source {"exists" if src.exists() else "absent"}: {d["source"]}')
        log('CHECK OK')

if __name__ == '__main__':
    try:
        main()
    except (BackupError, OSError, sqlite3.Error) as e:
        log(f'ERROR: {e}')
        sys.exit(1)
