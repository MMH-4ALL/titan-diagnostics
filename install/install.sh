#!/bin/sh
set -eu

version=0.2.1
release_url=${TITAN_RELEASE_URL:-https://github.com/MMH-4ALL/titan-diagnostics/releases/latest/download}
prefix=${TITAN_INSTALL_DIR:-${XDG_DATA_HOME:-"$HOME/.local/share"}/titan/install}
bin_dir="$HOME/.local/bin"
python=${TITAN_PYTHON:-python3}
while [ "$#" -gt 0 ]; do
    case "$1" in
        --release-url|--prefix|--bin-dir|--python)
            [ "$#" -ge 2 ] || { echo "Missing value for $1" >&2; exit 1; }
            case "$1" in
                --release-url) release_url=$2 ;;
                --prefix) prefix=$2 ;;
                --bin-dir) bin_dir=$2 ;;
                --python) python=$2 ;;
            esac
            shift 2 ;;
        --help)
            echo 'Install Titan: install.sh [--release-url HTTPS_URL] [--prefix DIR] [--bin-dir DIR] [--python EXECUTABLE]'
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done
[ "$(uname -s)" = Linux ] || { echo 'Use install.ps1 on Windows. This installer requires Linux.' >&2; exit 1; }
case "$release_url" in https://*) ;; *) echo 'Release URL must use HTTPS.' >&2; exit 1 ;; esac
command -v curl >/dev/null || { echo 'Install curl first.' >&2; exit 1; }
"$python" -c 'import sys; assert sys.version_info >= (3,11)' || { echo 'Install Python 3.11 or newer with venv support (python3-venv on Debian/Ubuntu).' >&2; exit 1; }
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT HUP INT TERM
wheel="titan_diagnostics-$version-py3-none-any.whl"
for name in SHA256SUMS.txt "$wheel" bootstrap.py; do
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 300 "${release_url%/}/$name" -o "$stage/$name"
done
"$python" - "$stage" "$wheel" <<'PY'
import hashlib, pathlib, re, sys
root = pathlib.Path(sys.argv[1])
entries = {}
for line in (root / 'SHA256SUMS.txt').read_text().splitlines():
    match = re.fullmatch(r'([a-fA-F0-9]{64})  (.+)', line)
    if match:
        if match[2] in entries:
            raise SystemExit('Duplicate checksum entry')
        entries[match[2]] = match[1].lower()
for name in [sys.argv[2], 'bootstrap.py']:
    if hashlib.sha256((root / name).read_bytes()).hexdigest() != entries.get(name):
        raise SystemExit('Checksum verification failed: ' + name)
PY
"$python" "$stage/bootstrap.py" --wheel "$stage/$wheel" --prefix "$prefix" --bin-dir "$bin_dir" >/dev/null
case ":$PATH:" in
    *":$bin_dir:"*) echo 'Installed. Run: titan' ;;
    *) printf 'Installed. Run: %s/titan\nAdd this directory to PATH: %s\n' "$bin_dir" "$bin_dir" ;;
esac
