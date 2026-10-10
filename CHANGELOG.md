# Changelog

## 0.2.0 — 2026-10-10 (release candidate)

- Added an npm/npx launcher, checked curl installers for Windows/Linux, private Python fallback runtimes, and GitHub release packaging.
- Added bounded drive connection timelines with hardware identity and incomplete-inventory safeguards.
- Added per-device latency, throughput, IOPS and instantaneous queue depth. Windows uses precise native disk time counters.
- Added manual/session SMART trends and opt-in local periodic health tracking with change alerts.
- Added redacted, atomic before/after snapshots, saved baseline loading, inventory and metric comparisons.
- Added process I/O rates and explicit Windows EStats/Linux BCC TCP tracing with coverage and access status.
- Added cancellable endpoint-selected TCP stability sampling, latency summaries and report integration.
- Added Linux thermal sensor thresholds and direct throttle-event evidence; optional existing Windows sensor WMI support.
- Added a responsive ASCII hardware map and a new slanted ASCII TITAN wordmark for Windows/Linux.
- Fixed inventory caching after slow queries and reset known disk baselines when hardware identities change.
- Extended CLI, tests, documentation, actual application captures and rebuilt release packages.

## 0.1.0 visual revision — 2026-10-10

- Added an original ASCII wordmark, CPU/memory/storage/network drawings,
  square terminal frames, numbered workspaces and an amber Titan Dark palette.
- Added a compact small-terminal header and denser medium-terminal navigation;
  graphs and artwork follow the selected theme.
- Compact headers also accommodate short terminals. Fixed a false activity
  finding caused by Windows' PID 0 idle-time accounting.
- Refreshed real dashboard captures and rebuilt release distributions.

## 0.1.0 — 2026-10-09 (release candidate)

- Implemented Textual monitoring workspaces, responsive layouts, keyboard search,
  command palette, themes, sortable process tables and process confirmation.
- Added stateful psutil measurements and read-only Windows/Linux providers,
  modular GPU telemetry, optional SMART analysis and external-drive findings.
- Added offline elapsed-time diagnostic rules, validated TOML settings, opt-in
  SQLite metric history, privacy-aware offline reports and scriptable CLI commands.
- Added automated unit, integration and UI tests, package builds, native build
  workflow and hardware verification documentation.

Hardware and platform validation remains limited to the environments explicitly
listed in `docs/verification.md`; this is not a claim of universal compatibility.
