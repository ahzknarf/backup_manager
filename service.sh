#!/usr/bin/env bash
set -euo pipefail
# Designed for `curl .../service.sh | bash -s -- backup`.
# No service.sh needs to exist inside a service repository.
# The invoking working directory must be the service repository root.
REPO_DIR="$(pwd -P)"
if [[ ! -f "$REPO_DIR/.backup.yaml" ]]; then
  echo "Error: .backup.yaml not found in current directory: $REPO_DIR" >&2
  echo "Run this command from the service repository root." >&2
  exit 2
fi
export BACKUP_MANAGER_REPO_DIR="$REPO_DIR"
exec python3 - "$@" <<'PY'
import fcntl, hashlib, json, os, pathlib, subprocess, sys, urllib.request
repo = pathlib.Path(os.environ['BACKUP_MANAGER_REPO_DIR'])
local = os.environ.get('BACKUP_MANAGER_LOCAL')
if local:
    os.execv(sys.executable, [sys.executable, local, '--repo', str(repo), *sys.argv[1:]])
manifest_url = os.environ.get('BACKUP_MANAGER_MANIFEST_URL',
    'https://raw.githubusercontent.com/OWNER/backup-manager/main/stable.json')
if not manifest_url.startswith('https://'):
    sys.exit('Manifest URL must be HTTPS')
cache = pathlib.Path(os.environ.get('BACKUP_MANAGER_CACHE_DIR', pathlib.Path.home() / '.cache' / 'backup-manager'))
cache.mkdir(parents=True, exist_ok=True)

def download(url):
    if not url.startswith('https://'):
        raise RuntimeError('Tool URL must use HTTPS')
    req = urllib.request.Request(url, headers={'User-Agent':'backup-manager-launcher/0.1'})
    with urllib.request.urlopen(req, timeout=15) as res:
        body = res.read(2_000_001)
    if len(body) > 2_000_000:
        raise RuntimeError('Download too large')
    return body

def digest(data):
    return hashlib.sha256(data).hexdigest()

with open(cache / '.lock', 'a+b') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    latest = cache / 'stable.json'
    try:
        content = download(manifest_url)
        manifest = json.loads(content)
        if not isinstance(manifest, dict):
            raise ValueError('Manifest must be JSON object')
        # Save only valid manifest after parsing.
        latest.write_bytes(content)
    except Exception as e:
        if not latest.exists():
            sys.exit(f'Cannot download tool manifest and no cache exists: {e}')
        print(f'Warning: using offline cached tool manifest ({e})', file=sys.stderr)
        manifest = json.loads(latest.read_text())
    version = manifest.get('version', '')
    sha = manifest.get('sha256', '')
    url = manifest.get('url', '')
    if not isinstance(version, str) or not version or any(x in version for x in '/\\.') or version.startswith('-'):
        sys.exit('Invalid manifest version')
    if not isinstance(sha, str) or len(sha) != 64 or any(x not in '0123456789abcdef' for x in sha):
        sys.exit('Invalid manifest sha256')
    if not isinstance(url, str) or not url.startswith('https://'):
        sys.exit('Invalid tool URL')
    executable = cache / ('backup-' + version + '.py')
    if not executable.is_file() or digest(executable.read_bytes()) != sha:
        raw = download(url)
        if digest(raw) != sha:
            sys.exit('Tool SHA-256 mismatch; refusing execution')
        tmp = cache / (executable.name + '.tmp')
        tmp.write_bytes(raw)
        os.replace(tmp, executable)
    # Use system PyYAML where available, otherwise install into a single shared venv.
    try:
        import yaml
        python = sys.executable
    except ImportError:
        env = cache / 'venv'
        python = str(env / 'bin' / 'python3')
        if not pathlib.Path(python).exists():
            subprocess.run([sys.executable, '-m', 'venv', str(env)], check=True)
        test = subprocess.run([python, '-c', 'import yaml'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if test.returncode:
            subprocess.run([python, '-m', 'pip', 'install', 'PyYAML>=6,<7'], check=True)
os.execv(python, [python, str(executable), '--repo', str(repo), *sys.argv[1:]])
PY
