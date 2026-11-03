#!/usr/bin/env bash
set -euo pipefail
# Central launcher: curl .../run | bash -s -- [--repo /path/to/service] backup
exec python3 - "$@" <<'PY'
import argparse
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

DEFAULT_MANIFEST_URL = 'https://raw.githubusercontent.com/ahzknarf/backup_manager/master/stable.json'
YAML_REQUIREMENT = 'PyYAML==6.0.3'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def https_url(url):
    if not isinstance(url, str):
        raise ValueError('URL must be an HTTPS string')
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or any(c.isspace() for c in url)):
        raise ValueError('URL must use HTTPS without credentials or fragments')
    return url


class HTTPSRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, limit=2_000_000):
    request = urllib.request.Request(https_url(url), headers={'User-Agent': 'backup-manager-launcher/0.2'})
    opener = urllib.request.build_opener(HTTPSRedirects())
    with opener.open(request, timeout=15) as response:
        https_url(response.geturl())
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError('Download too large')
    return body


def parse_manifest(content):
    manifest = json.loads(content)
    if not isinstance(manifest, dict):
        raise ValueError('Manifest must be a JSON object')
    version = manifest.get('version')
    sha = manifest.get('sha256')
    if (not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,127}', version)
            or '..' in version):
        raise ValueError('Invalid manifest version')
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
        raise ValueError('Invalid manifest sha256')
    https_url(manifest.get('url'))
    return manifest


def atomic_write(path, content):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def tool_path(cache, manifest):
    # Immutable content filenames also protect a tool already being executed.
    return cache / ('backup-' + manifest['sha256'] + '.py')


def verified_tool(cache, manifest):
    path = tool_path(cache, manifest)
    if not path.is_file() or digest(path.read_bytes()) != manifest['sha256']:
        raise ValueError('Cached tool missing or SHA-256 mismatch')
    compile(path.read_bytes(), str(path), 'exec')
    return path


def cached_release(cache):
    errors = []
    for name in ('stable.json', 'previous.json'):
        try:
            manifest = parse_manifest((cache / name).read_bytes())
            return manifest, verified_tool(cache, manifest)
        except (OSError, ValueError, SyntaxError) as error:
            errors.append(f'{name}: {error}')
    raise RuntimeError('No verified cached release: ' + '; '.join(errors))


def select_release(cache, manifest_url, *, offline=False):
    if offline:
        return cached_release(cache)
    try:
        manifest = parse_manifest(download(manifest_url, 64_000))
        path = tool_path(cache, manifest)
        try:
            verified_tool(cache, manifest)
        except (OSError, ValueError, SyntaxError):
            content = download(manifest['url'])
            if digest(content) != manifest['sha256']:
                raise ValueError('Tool SHA-256 mismatch; refusing execution')
            compile(content, str(path), 'exec')
            atomic_write(path, content)
        # Only promote a complete, verified release. Preserve a usable predecessor.
        try:
            current = parse_manifest((cache / 'stable.json').read_bytes())
            verified_tool(cache, current)
        except (OSError, ValueError, SyntaxError):
            current = None
        if current is not None and current != manifest:
            atomic_write(cache / 'previous.json', json.dumps(current).encode())
        atomic_write(cache / 'stable.json', json.dumps(manifest).encode())
        return manifest, path
    except (OSError, ValueError, SyntaxError, RuntimeError, http.client.HTTPException) as error:
        try:
            result = cached_release(cache)
        except RuntimeError as cached_error:
            raise RuntimeError(f'Cannot obtain a verified tool ({error}); {cached_error}') from error
        print(f'Warning: using verified cached release ({error})', file=sys.stderr)
        return result


def python_has_module(python, module, *, virtual=False):
    if not Path(python).is_file():
        return False
    code = 'import sys\n'
    if virtual:
        code += 'if sys.prefix == sys.base_prefix:\n    sys.exit(1)\n'
    code += 'import ' + module
    try:
        result = subprocess.run([str(python), '-c', code], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=30)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def dependency_help():
    package = f'python{sys.version_info.major}.{sys.version_info.minor}-venv'
    return ('Install PyYAML for the invoking Python, or install its venv/ensurepip support.\n'
            'On Debian/Ubuntu using /usr/bin/python3, the simplest fix is:\n'
            '  apt-get update && apt-get install -y python3-yaml\n'
            'Alternatively, enable venv for this Python version:\n'
            f'  apt-get update && apt-get install -y {package}\n'
            'Then rerun the launcher; incomplete cached environments are repaired automatically.')


def python_with_yaml(base_cache):
    if python_has_module(sys.executable, 'yaml'):
        return sys.executable
    environment = base_cache / 'venv'
    python = environment / 'bin' / 'python3'
    if python_has_module(python, 'yaml', virtual=True):
        return str(python)
    if os.environ.get('BACKUP_MANAGER_OFFLINE') == '1':
        raise RuntimeError('PyYAML is unavailable in the system or cached venv; cannot install offline')
    # A failed ensurepip step can leave bin/python3 behind without pip. Probe
    # the environment instead of treating the presence of Python as success.
    if not python_has_module(python, 'pip', virtual=True):
        try:
            subprocess.run([sys.executable, '-m', 'venv', str(environment)], check=True,
                           capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as error:
            detail = (getattr(error, 'stderr', '') or getattr(error, 'stdout', '') or str(error)).strip()
            raise RuntimeError(f'Cannot create or repair cached venv: {detail[-2000:]}\n'
                               + dependency_help()) from error
        if not python_has_module(python, 'pip', virtual=True):
            raise RuntimeError('Cached venv still has no working pip after recreation.\n' + dependency_help())
    subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                    YAML_REQUIREMENT], check=True, timeout=180)
    if not python_has_module(python, 'yaml', virtual=True):
        raise RuntimeError(f'PyYAML installation finished but import failed: {python}')
    return str(python)


def main():
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--repo', default='.')
    args, forwarded = parser.parse_known_args()
    repo = Path(args.repo).expanduser().resolve()
    if not (repo / '.backup.yaml').is_file():
        raise RuntimeError(f'.backup.yaml not found: {repo}; use --repo or run in the service directory')
    local = os.environ.get('BACKUP_MANAGER_LOCAL')
    if local:
        path = Path(local).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f'Local tool missing: {path}')
        os.execv(sys.executable, [sys.executable, str(path), '--repo', str(repo), *forwarded])
    manifest_url = https_url(os.environ.get('BACKUP_MANAGER_MANIFEST_URL', DEFAULT_MANIFEST_URL))
    base_cache = Path(os.environ.get('BACKUP_MANAGER_CACHE_DIR', Path.home() / '.cache' / 'backup-manager')).expanduser().resolve()
    base_cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Different distribution endpoints cannot replace each other's trusted cache.
    cache = base_cache / digest(manifest_url.encode())[:16]
    cache.mkdir(mode=0o700, exist_ok=True)
    with open(base_cache / '.download.lock', 'a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        python = python_with_yaml(base_cache)
        _, executable = select_release(cache, manifest_url,
                                       offline=os.environ.get('BACKUP_MANAGER_OFFLINE') == '1')
    os.execv(python, [python, str(executable), '--repo', str(repo), *forwarded])


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, http.client.HTTPException) as error:
        sys.exit(f'Launcher error: {error}')
PY
