# Verified behavior and release boundaries

This is a working 0.2.0 release candidate, not a claim that every feature in the
original brief or every Windows/Linux device has been certified. No GitHub
repository was created and the supplied CI workflow has not been run remotely.

## Executed locally

Validation dates: 2026-10-09 and monitoring/ASCII revision 2026-10-10.
Host: Windows 11, Python 3.12.14, Textual 8.2.8,
psutil 7.2.2. All 160 automated tests pass. Ruff checking, Ruff formatting and
mypy pass. The source archive and universal Python wheel build; installing the
wheel produces a working `titan` console command. PyInstaller produces a native
Windows x64 console distribution; native version/help/doctor checks were executed.

Tests cover normalization, counter warmup/reset, collector and submetric failure
isolation, missing tools, subprocess timeout, device disappearance, permission
errors, process PID reuse and protected-process refusals, diagnostic windows and
confidence, config validation, history retention/aggregation/corruption, HTML
escaping, CSV formula protection, privacy redaction, CLI outputs and exit codes.

Textual tests exercise real widgets at 160×50, 100×34, 60×26 and 40×16, navigation,
command palette, search, typing without accidental shortcuts, theme persistence,
sorting, selection preservation, process confirmation/cancel, live updates, slow
collectors, history opt-in, process CSV export, explicit drive-health actions,
aggregate/per-core switching, requested network-test findings and shutdown.
These tests inject labeled fixtures; the dashboard screenshots separately use
the actual collector and real hardware observations.

The ASCII revision was visually inspected at 128×42, 100×34 and 60×26 using
real measurements. It uses theme-aware component drawings, ASCII panel borders
and a compact header below 80 columns or 28 rows. Keyboard and resize checks also
cover 40×16 and 80×24. The source, wheel and Windows executable were rebuilt after
the revision. A regression test prevents idle-time accounting from generating
process-activity findings while preserving warnings for real busy applications.

A local integration test contacts only a loopback server created by the test.
An OS termination integration test creates and terminates only its own child;
service detection is mocked in that narrowly scoped action test and separately
tested with ownership fixtures. No existing user process was terminated.

## Real hardware observations

Outside the restricted execution sandbox, the Windows provider returned CPU
identity/core counts/frequency and interval utilization, RAM and pagefile data,
running processes, one physical NVMe drive and its partitions, mounted-volume
space and disk transfer activity, network interfaces, configured DNS/default
gateway and accessible connections, battery charge/full capacity/cycle count,
and three OS/vendor-enumerated graphics adapter records. NVIDIA utilization,
VRAM, temperature, clock and power were readable. Unsupported adapter sensors
remain unavailable. CPU temperature and this battery's design capacity/wear,
temperature and power profile were unavailable.

The restricted sandbox denied several OS APIs; tests and live checks confirmed
that Titan retained accessible RAM, mounted-volume and network measurements while
showing missing submetrics. The host restrictions also blocked Python's local
event-loop socket pair, so UI tests and captures ran with ordinary host access.
Neither Titan nor these checks changed Windows security settings.

## Measured performance

The following performance measurements are from the 0.1.0 release and are
historical, not a benchmark of the expanded 0.2.0 collectors. A 0.2.0 five-sample
check before removing the slower Windows queue query measured 7.594 seconds
cold and 0.468–0.594 seconds warm. The final queue provider uses the same native
disk performance read as latency, avoiding that additional PowerShell query.

On this host with approximately 220 processes, cold inventory collection took
7.37 seconds; twelve cached collections had a 0.173-second median and
0.235-second maximum. Startup remains navigable while collection runs off-thread.
A fifteen-second Textual headless dashboard observation measured 17.8% of one
logical CPU and RSS increasing from 60.8 to 64.6 MiB. This short test is not a
long-term memory-stability guarantee, native-terminal benchmark or low-end
hardware certification. A longer refresh interval reduces work on modest systems.

Graphs retain 120 readings, aggregate snapshots at most 900. Process evidence
tracks up to 64 largest-memory plus 32 highest-CPU identities per interval,
retaining a default 75-second process window; the current process table still
shows up to 2,048 processes. Findings can therefore miss growth in smaller or
intermittently tracked processes. SQLite history retains aggregate metrics only,
and writes at most at the configured recording interval (default 10 seconds).

## Not yet verified on real hardware

- Linux live collection and native executable; Linux provider parsing is tested
  with fixtures and the workflow is configured to run on Linux independently.
- Real ATA/SATA/USB/NVMe SMART passthrough: smartctl was absent on this host.
  Structured parsing, status-bit interpretation and unavailable fallback are tested.
- External drive failure, RAW/encrypted volumes, USB bridge-only states and
  disconnect/reconnect behavior on physical devices. Normal physical-drive
  inventory was tested; troubleshooting edge cases use fixtures.
- AMD/Intel live telemetry, multiple identical GPUs, multi-battery laptops,
  battery design-capacity/wear accuracy and permission variants on other machines.
- Long-running native terminal sessions, non-UTF-8 terminals, uncommon terminals,
  low-end dual-core hardware and binaries on other Windows installations.

## Features deferred from the full brief

The eight requested additions are implemented in 0.2.0. New tests cover physical
identity changes, partial/failed/cached inventory, native Windows time units and
buffer/handle safeguards, SMART change direction and corrupt-store preservation,
snapshot privacy/schema checks, thermal thresholds/counter resets, TCP EStats
PID reuse, BCC output, stability cancellation and actual loopback TCP success and
refusal. UI tests exercise new pages, baseline save/load/comparison, resizing,
stability controls and report retention.

Actual Windows checks read one physical disk's native time counters, observed
about 0.166 ms/read and 0.054 ms/write during a one-second sample, and read queue
depth zero. A live collection found 225 accessible process I/O readings, one
initial drive-presence event and complete physical inventory. These are point
observations, not performance guarantees. The self-created loopback TCP EStats
test received access-denied on both connections; no elevated run was attempted.
CPU temperature WMI and direct thermal counters were unavailable on this host.
Linux is not installed on this computer, so Linux thermal/BCC paths remain
fixture-tested. Real SMART trend history requires successful reads from actual
supported drives and still needs hardware validation.

No automatic disk saturation diagnosis, physical USB bus disconnect cause
correlation, memory PSI, Windows AMD/Intel utilization, battery power draw or
wall-power estimate is implemented. Windows USB-bridge-to-disk correlation and
direct Windows thermal-throttle counters remain unavailable. TCP attribution is
conditional and scoped as described in [monitoring](monitoring.md), not a complete
per-process network bandwidth meter. CPU/GPU graphs reflect
observed ranges; they are not fixed zero-to-100 axes. No storage repair is offered.
Custom history ranges are available in the CLI; the UI offers preset ranges.
Logs are bounded operational events; detailed system snapshots are never logged.

## Command installation verification

Windows checks for the installable-command revision pass: 165 Python tests,
four Node launcher tests, Ruff checks/format and mypy. A fresh isolated npm
global install runs the bundled native executable without Python bootstrap.
A separate wheel-only npx package creates a private Python environment and
reuses it on the next run. The PowerShell installer preserves PATH when requested,
verifies its download, and launches from a path containing spaces and Unicode.
Damaged checksums cannot change an existing installation; valid updates switch
the managed command and preserve CLI error exit codes.
Shell syntax was checked with Git's sh; that does not establish a Linux install.
Remote release and Linux installer validation are recorded separately after
execution; the workflow itself is not evidence that it ran successfully.

## Hardware verification procedure

1. Install the wheel/native distribution on a clean target system as a regular user.
2. Compare a sustained workload against the OS task monitor, allowing for sampling
   intervals and process CPU's one-logical-core basis.
3. Compare physical inventory with Disk Management or `lsblk`; verify ordinary
   volumes separately from reserved/recovery/container partitions.
4. If smartctl is already available, explicitly run storage diagnosis and compare
   its JSON readout with the vendor's interpretation. Use noncritical test drives
   for USB removal, missing-letter and inaccessible-device cases; never alter a
   drive containing unrecovered data to construct a test.
5. Compare vendor GPU readings and platform battery reports with Titan; record
   missing support without substituting invented values.
6. Test only endpoints you explicitly select; distinguish DNS/TCP/ICMP behavior.
7. Inspect redacted exports offline, confirm history is paused by default, exercise
   resizing/themes/search, and run a prolonged session to assess resource use.
