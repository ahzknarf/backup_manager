#!/usr/bin/env python3
"""Generate/check the development manifest or build a commit-pinned Pages release."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'ahzknarf/backup_manager'


def manifest(root, url):
    engine = (root / 'backup.py').read_bytes()
    for node in ast.parse(engine).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in node.targets):
            version = ast.literal_eval(node.value)
            break
    else:
        raise ValueError('backup.py has no VERSION')
    return {'version': 'v' + version, 'url': url, 'sha256': hashlib.sha256(engine).hexdigest()}


def json_bytes(value):
    return (json.dumps(value, indent=2) + '\n').encode()


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True).stdout


def build_pages(root, output, base_url, repository=REPOSITORY, revision=None):
    parsed = urlsplit(base_url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or any(c.isspace() for c in base_url)):
        raise ValueError('--base-url must be a public HTTPS site URL without query/fragment/credentials')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid repository (expected owner/name)')
    revision = revision or git(root, 'rev-parse', 'HEAD').decode().strip()
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', revision):
        raise ValueError('--revision must be a full Git commit hash')
    for filename in ('backup.py', 'service.sh'):
        if git(root, 'show', f'{revision}:{filename}') != (root / filename).read_bytes():
            raise ValueError(f'{filename} differs from {revision}; commit changes before publishing')
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output directory must be empty; use a new directory for each release')
    release = manifest(root, f'https://raw.githubusercontent.com/{repository}/{revision}/backup.py')
    launcher, count = re.subn(r'^DEFAULT_MANIFEST_URL = .+$',
                             lambda _: 'DEFAULT_MANIFEST_URL = ' + repr(base_url.rstrip('/') + '/stable.json'),
                             (root / 'service.sh').read_text(), flags=re.MULTILINE)
    if count != 1:
        raise ValueError('Expected exactly one DEFAULT_MANIFEST_URL in service.sh')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'run').write_text(launcher)
    (output / 'service.sh').write_text(launcher)
    (output / 'stable.json').write_bytes(json_bytes(release))
    (output / '.nojekyll').touch()
    return release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('manifest', help='Refresh the development stable.json')
    command.add_argument('--check', action='store_true')
    command.add_argument('--url', default=f'https://raw.githubusercontent.com/{REPOSITORY}/master/backup.py')
    command = commands.add_parser('pages', help='Build the short launcher and commit-pinned manifest')
    command.add_argument('--base-url', default='https://ahzknarf.github.io/backup_manager')
    command.add_argument('--repository', default=REPOSITORY)
    command.add_argument('--revision')
    command.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    if args.command == 'manifest':
        expected = manifest(ROOT, args.url)
        if args.check:
            if json.loads((ROOT / 'stable.json').read_text()) != expected:
                raise ValueError('stable.json is stale; run: python3 scripts/release.py manifest')
            print('Manifest version, URL and SHA-256 OK')
        else:
            (ROOT / 'stable.json').write_bytes(json_bytes(expected))
            print('Updated stable.json')
    else:
        release = build_pages(ROOT, args.output, args.base_url, args.repository, args.revision)
        print(f'Built {release["version"]} in {args.output}; entry: {args.base_url.rstrip("/")}/run')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(f'Release error: {error}')
