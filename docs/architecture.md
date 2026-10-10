# Architecture

Titan separates OS collection, normalized transport data, diagnosis, persistence, exports and terminal rendering. All monitoring operates locally. Python 3.11 or newer is required.

## Data flow

```text
psutil + platform providers + optional vendor tools
                        |
               core.collectors.Collector
                        |
                    Snapshot
          /             |              \
   Textual rendering   evaluate()     opt-in HistoryStore
          |             |              |
     bounded graphs   Finding[]    aggregate SQLite metrics
          \             /
          reports.export_report()
             offline/redacted files
```

`Section(data: dict[str, Any], unavailable: list[str], error: str | None)` explicitly separates accessible observations, unsupported measurements and collection errors. `Snapshot(timestamp: float, platform: str, sections: dict[str, Section])` uses an epoch collection timestamp. `Finding` contains the rule identity, title, severity, evidence, explanation, possible causes, next step, confidence and elevated-access requirement. They are dataclasses and support `to_dict()`.

## Collection and lifecycle

`Collector.collect() -> Snapshot` is stateful. Reuse one instance for CPU/process baselines and monotonic byte-counter differences. First-use interval metrics are unavailable until a later observation. Counter resets, device replacements and nonpositive durations produce unavailable rates instead of artificial bursts. A failed section is isolated and cannot prevent other sections from returning.

Collection runs outside the UI event loop. OS identity/storage/network metadata is cached for 30 seconds, GPU data for five seconds, and active connections for five seconds. Textual maintains bounded live graph/sample buffers and avoids overlapping refresh workers. Vendor subprocesses use argument lists, timeouts, bounded outputs and no shell interpolation. Shutdown cancels workers and closes persistence resources.

`platforms.base.get_provider()` selects Windows or Linux support. Provider methods normalize CPU/system identity, physical storage, GPU and battery-capacity information. Ordinary activity, memory, interfaces, mounted-volume usage and processes use psutil. Windows providers use fixed read-only PowerShell/CIM programs; Linux uses lsblk, procfs and sysfs. APIs report inaccessible optional integrations explicitly.

`hardware.storage.add_smart_health(section)` adds structured SMART observations to an already enumerated drive section on an explicit diagnosis request. It validates enumerated device paths and supported bridge types and never changes the drive. `storage_findings(section) -> list[Finding]` interprets this evidence. `hardware.network.network_test(target, port=443, count=4, timeout=3) -> Section` runs only on an explicitly selected target and isolates DNS, TCP and ICMP results. `core.processes.terminate_process(pid, create_time, confirmed=False)` requires confirmation, revalidates identity and protects critical processes; it does not silently kill services.

## Configuration

`Config` is immutable and typed. `load_config(path=None) -> Config`, `validate_config(values) -> Config`, and `save_config(config, path=None)` reject unknown keys, invalid types, nonfinite numbers and out-of-range settings. Writes use a temporary file and atomic replacement. Missing files use safe defaults; malformed files produce actionable errors and can be reset explicitly.

Default files use `%LOCALAPPDATA%/Titan` on Windows. Linux configuration uses `$XDG_CONFIG_HOME/titan` (default `~/.config/titan`) and data uses `$XDG_DATA_HOME/titan` (default `~/.local/share/titan`). `default_data_dir()`, `default_config_path()` and `default_history_path()` are shared by CLI and interface. No history is recorded by default.

## Diagnostics

`core.diagnostics.evaluate(samples: Sequence[Snapshot], config: Config | None = None) -> list[Finding]` has no I/O except importing the storage-rule function. It deduplicates timestamps, orders evidence, checks contiguous elapsed windows and uses same-PID/same-creation-time process identities. Process lookup indexes make evaluation linear in the number of observed process records rather than repeatedly scanning entire process lists. A missing measurement breaks sustained evidence. There is no universal score or thermal-throttling claim. Exact assumptions and unimplemented rules are listed in [diagnostics.md](diagnostics.md).

## History

`HistoryStore(path, retention_days=7)` performs no filesystem operation at construction. `record(snapshot)`, `query(start=None, end=None, bucket_seconds=0)`, `cleanup()` and `close()` manage aggregate-only SQLite records. Reading a nonexistent database returns an empty list without creating it. The interface uses a dedicated single-thread executor for all operations on a store, including close, because SQLite connections retain thread affinity.

The database stores only epoch time, CPU/RAM percentages, disk and network rates, mean available GPU utilization, CPU temperature and battery charge. It stores no process names, executable paths, usernames, network addresses or device serials. Writes occur at the configurable history interval, default 10 seconds. Retention cleanup runs at most hourly during recording and deletes records older than the configured policy. Queries are bounded to 10,000 rows; larger ranges are averaged into automatically sized time buckets. Null metrics remain null when no available observation exists. `samples` tells how many observations contributed to each row.

SQLite integrity/schema checks and readable `HistoryError` messages explain damaged or incompatible files. Titan preserves the existing history file rather than replacing it automatically. A new path can be selected after backing up the original file. `close()` is idempotent; the class also supports a context manager.

## Export and command entry

`export_report(snapshot, findings, format="json", include_sensitive=False) -> str` returns JSON, flattened metric/value CSV, human-readable text, or offline HTML. `export_processes(snapshot, include_sensitive=False) -> str` returns one process per CSV row. Redaction traverses sensitive fields and identifiers and also scrubs them from findings/errors. HTML escapes all observations and has no scripts/external resources; CSV strings are protected against spreadsheet formula execution. Reports contain collection time, tool versions, explicit missing/error states and privacy status.

`titan.cli.app` is the Typer application. `titan.cli.main()` is both the installed `titan` console entry and the `python -m titan` entry. Running without a command imports and launches `TitanApp` lazily. Machine-readable CLI commands therefore do not construct the full-screen interface. Shared Config/HistoryStore/diagnostics/export APIs keep CLI behavior consistent with the interface. Script and hardware-dependent failures have meaningful exit codes documented in [diagnostics.md](diagnostics.md).

## Extended monitoring

`core.monitoring` maintains bounded drive presence observations and per-disk
interval deltas independently of Textual. Inventory completion and cache age
prevent stale/failed reads from becoming disconnects. `platforms.windows_disk`
reads native disk timings with validated names, bounded allocated buffers and
closed handles. `hardware.thermal` reports sensor-specific thresholds and direct
Linux thermal-counter increments. `hardware.traffic` attributes only measured TCP
bytes and validates process identities; optional tracing starts off.

`core.health_trends` retains numerical SMART points and hashed identities, with
optional bounded SQLite persistence. `core.snapshots` creates redacted atomic
JSON artifacts and compares available values and inventory fingerprints. No
pickle is read. `hardware.stability` runs a bounded, cancellable probe sequence
against the selected endpoint; samples are bounded to 3600. `hardware.map`
renders sanitized ASCII from the snapshot without collecting more hardware data.
The monitoring guide documents measurement scope and platform dependencies.

## Verification boundaries

Tests use injected collectors, fake OS/vendor responses, temporary databases, Typer's runner and Textual pilots. Real collector smoke tests exercise only the current host. Mocked Windows/Linux parsing tests establish normalization and failure behavior, not universal hardware compatibility. Real NVIDIA/AMD/Intel, USB bridge, SMART and multi-battery behavior must be checked on the relevant physical hardware. CI runs unit/integration/UI checks separately on Windows and Linux and builds platform-specific release artifacts.
