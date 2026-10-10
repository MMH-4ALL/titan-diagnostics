# Titan 0.2.1 release candidate

The source delivery includes a working Textual application, real local monitoring,
offline diagnostic rules, read-only storage troubleshooting and optional SMART,
safe process management, explicit endpoint tests, opt-in SQLite history, validated
TOML settings and redacted JSON/CSV/text/standalone HTML reports. Five themes,
keyboard navigation, search, command palette, sortable tables and responsive
layouts are implemented. Samples are clearly marked synthetic.

All eight requested additions are implemented: drive connection timeline,
disk latency/IOPS/queue monitoring, SMART trends and alerts, before/after
snapshots, process I/O and optional TCP attribution, cancellable TCP stability
tests, expanded thermal evidence, and an ASCII hardware map. The slanted ASCII
TITAN wordmark supports Windows and Linux with a compact narrow layout.
Monitoring, Hardware map and Snapshots are accessible through the sidebar,
search and command palette. CLI commands expose the new features too.

Installable npm tarballs provide the `titan` command and one-off npx launches.
Native executables run directly when bundled for the target platform; otherwise
Python 3.11+ prepares a private environment on first launch. Checked curl
installers support Windows and Linux. Installation can download dependencies;
subsequent app runs are offline. The npm registry name is not published; use the
GitHub release tarball. The release workflow builds Windows/Linux independently.

Automated tests, Ruff checks/formatting and mypy pass on Windows/Python 3.12. The Python
wheel and source archive build and install. A Windows x64 standalone console
distribution is included; extract the entire folder and run `titan.exe`. Linux
source/providers and independent CI/build workflows are included. A Linux binary
is distributed only when that independent build succeeds; Linux hardware
certification is not claimed.

The native application is unsigned. Ordinary monitoring does not need elevation;
optional sensor access depends on the OS, driver and hardware. No telemetry,
uploads, paid APIs, disk modification or silent process termination is present.

Windows live checks verify precise native disk timings, queue depth, process I/O
and physical inventory. Loopback-only tests verify TCP success and refusal.
The Windows EStats check reported missing administrator rights; no elevation was
attempted. CPU sensor WMI and smartctl are absent on this host, so those readings
remain unavailable. Linux providers and BCC output have fixture coverage;
Linux live hardware, SMART/USB failures, and privileged tracing need further
hardware validation. Windows process I/O includes network/device operations;
Windows tracing covers IPv4 TCP and Linux BCC samples have setup gaps and 1 KiB
resolution. Tracking and tracing start off, and snapshots are redacted.
See `docs/monitoring.md` and `docs/verification.md` for scope and validation.
