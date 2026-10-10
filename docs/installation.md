# Installation and builds

## npm and npx

Node.js 20 or newer. Release 0.2.2 npm tarballs include Windows x64 and Linux
x64 native executables:

```console
npm install -g https://github.com/MMH-4ALL/titan-diagnostics/releases/latest/download/titan-diagnostics-0.2.2.tgz
titan
titan --version
titan --help
```

You can also install a downloaded file: `npm install -g ./titan-diagnostics-0.2.2.tgz`.
For a one-off run without a global install:

```console
npx --yes --package https://github.com/MMH-4ALL/titan-diagnostics/releases/latest/download/titan-diagnostics-0.2.2.tgz titan --version
```

Native runtimes bundled for your platform run directly with no Python requirement.
If no matching runtime is bundled, the launcher requires Python 3.11+ with venv
support, and installs its bundled wheel and Python dependencies in a private
user directory on first run. That first preparation downloads dependencies;
later runs are offline. Install `python3-venv` on Debian/Ubuntu when necessary.
Set `TITAN_PYTHON` to an executable to choose Python. `TITAN_INSTALL_DIR` changes
the private-runtime location, separate from `TITAN_DATA_DIR` for diagnostic data.
There are no npm lifecycle install scripts and no administrator prompts.

Update with a newer release tarball. `npm uninstall -g titan-diagnostics` removes
the npm command and bundled files; private Python runtimes and user data remain.
`npm install -g titan-diagnostics` becomes available only after an npm registry
publication; this project has not reserved or published that registry name.

## curl on Linux

Python 3.11+ with venv support and curl are required:

```sh
curl -fsSL https://github.com/MMH-4ALL/titan-diagnostics/releases/latest/download/install.sh | sh
titan
```

The installer verifies SHA-256 checksums, prepares a private Python runtime, and
writes `~/.local/bin/titan`. If that directory is outside PATH it prints its full
command path; add `~/.local/bin` to your shell's PATH. Your shell files are not edited.
For custom locations, save the script and use `sh install.sh --prefix DIR --bin-dir DIR`.
`--python EXECUTABLE` chooses Python, and `--release-url HTTPS_URL` selects a hosted
release directory containing the wheel, bootstrap.py and SHA256SUMS.txt.
Re-run the newer release's installer to update. Remove the managed `titan` shell
script to uninstall the command. Runtime files are retained under
`${XDG_DATA_HOME:-~/.local/share}/titan/install`; diagnostic data stays separate.

## curl on Windows

In PowerShell on 64-bit Windows:

```powershell
curl.exe -fsSL https://github.com/MMH-4ALL/titan-diagnostics/releases/latest/download/install.ps1 -o "$env:TEMP\install-titan.ps1"
& "$env:TEMP\install-titan.ps1"
titan
```

This installs the complete standalone Windows x64 release, verifies its checksum
and version, and adds `%LOCALAPPDATA%\Titan\cli\bin` to your **user** PATH.
Open a fresh terminal for other windows to see the new command. No Python or Node
is required for this route. Existing machine execution-policy restrictions still
apply; the installer does not change policy. If they prevent running a script,
use the npm route or extract the standalone ZIP.

For a local archive: `./install.ps1 -ArchivePath ./Titan-0.2.2-windows-x64.zip -ChecksumPath ./SHA256SUMS.txt`.
`-InstallDir DIR` changes its folder; `-NoPathUpdate` leaves PATH untouched and
prints the launcher path. Updates keep old version directories and user data.
To uninstall this method, remove its dedicated installation folder and its
`bin` entry from your user PATH. Diagnostic data is outside that folder.

## Python application

Requires Python 3.11 or newer on Windows 11 or a modern Linux distribution:

```console
python -m pip install titan_diagnostics-0.2.2-py3-none-any.whl
titan --version
titan doctor
titan
```

Alternatively, from this source directory use `python -m pip install .`.
The distribution name is `titan-diagnostics`; the command is `titan`.
Runtime dependencies are Textual, Rich, Typer and psutil. Installation downloads
dependencies unless they are already available. Running Titan requires no internet.
Run in Windows Terminal or a UTF-8 Linux terminal. No administrator access is
needed for ordinary monitoring; hardware queries may remain unavailable.

## Standalone executable

The Windows release archive contains `Titan/titan.exe` and its support directory.
Extract the entire archive; keep `_internal` beside the executable. Open a terminal
in that folder and run `titan.exe`. It is a console application, so launching in an
existing terminal is recommended. The binary is unsigned; no signing identity
was supplied for this project. Linux binaries are built independently on Linux.

## Optional integrations

Install smartmontools from its official distribution or your Linux package
manager and put `smartctl` on PATH. Titan recognizes the tool and uses structured
JSON read-only attributes. USB bridge passthrough and permissions vary. Do not
change disk state merely to obtain a reading.

For NVIDIA, use the vendor driver that supplies `nvidia-smi`. Windows AMD/Intel
adapters can be detected through CIM, but no unsupported utilization values are
invented. Linux AMD/Intel telemetry depends on the driver's sysfs interfaces.
Titan continues without either optional tool.

## Configuration and data

Windows defaults: `%LOCALAPPDATA%/Titan/config.toml`, `history.sqlite3`, `reports/`.
Linux defaults: `$XDG_CONFIG_HOME/titan/config.toml` and `$XDG_DATA_HOME/titan/`;
fallbacks are `~/.config` and `~/.local/share`. History starts paused. CLI path
options support isolated installations and custom history locations; see help.
Set `TITAN_DATA_DIR` to select a portable data folder, including reports and logs.
Operational logs rotate at 256 KB with two backups and do not contain snapshots,
process names, user paths or hardware identifiers.

## Rebuild

```console
python -m pip install -e ".[dev,bundle]"
python -m build
python scripts/build_native.py
python scripts/package_release.py
python scripts/build_npm.py --native win32-x64=dist/native/titan
```

The native build uses PyInstaller on the current platform. Build Windows on Windows
and Linux on Linux; this is not cross-compilation. Theme definitions are embedded
in Python and Textual resources are included by PyInstaller collection hooks.
CI performs the same builds. `requirements-lock.txt` records the exact Python 3.12
Windows build environment; Linux resolves the supported dependency ranges because
its binary wheels differ. Rebuilds are repeatable procedures, not guaranteed
bit-for-bit identical binaries.

On Linux substitute `--native linux-x64=dist/native/titan`. Omit `--native` for
a wheel-only npm package that always uses the private Python fallback. The
`Build installable release` workflow verifies both OS builds, bundles both native
runtimes in one npm package, and publishes checksummed GitHub assets. A manual
run or matching `vVERSION` tag releases the version in pyproject.toml. It does not
publish to npm or PyPI. Existing release tags are not overwritten automatically.
