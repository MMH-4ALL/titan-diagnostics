# Titan

Offline system diagnostics and monitoring in your terminal, for Windows and Linux.

Install this release tarball with `npm install -g ./titan-diagnostics-0.2.0.tgz`.
Then run `titan`, `titan --help`, or `titan overview --json`.
For a one-off run: `npx --package ./titan-diagnostics-0.2.0.tgz titan --version`.
Node.js 20 or newer is required. No npm install scripts run.

If the package contains a native executable for your OS/architecture, Titan runs
it directly without Python or a first-run download. Otherwise Python 3.11 or newer
with venv support is required. Titan installs its bundled Python wheel and Python
dependencies in a private user folder on the first launch. This first launch
needs internet for dependencies; subsequent launches work offline. On Debian or
Ubuntu install `python3-venv` if your Python lacks venv support.

Set `TITAN_PYTHON` to a Python executable to select it. `TITAN_INSTALL_DIR` changes
the private Python runtime location. `TITAN_DATA_DIR` independently changes where
Titan saves its local diagnostics data. No elevation, telemetry, or cloud uploads.

This package is ready for hosting as a GitHub release asset. The bare command
`npm install -g titan-diagnostics` requires publication to the npm registry;
it is not a claim that the registry name is reserved or published.

Updates: install a newer tarball with the same npm command. Uninstall:
`npm uninstall -g titan-diagnostics`. This removes the launcher and bundled native
files. Private Python runtimes and diagnostics data are retained.
