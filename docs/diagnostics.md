# Diagnostics and evidence

Titan runs local, read-only rules. It does not use a cloud service, an AI diagnosis, or a universal computer health score. Every finding has a severity, measured evidence, explanation, possible causes, next step, confidence, and an elevated-access flag. Missing measurements never become zero.

`titan diagnose` observes for 10 seconds by default at one-second intervals. `--duration` and `--interval` change that window. The first CPU/process/rate observation can be unavailable while counters warm up. Consequently, a fresh default 10-second command may have fewer than 10 seconds of usable CPU evidence and correctly produce no sustained finding. Use `titan diagnose --duration 15 --json` when a complete default sustained window is needed. In the interface, diagnostics use the current bounded live sample buffer.

No triggered rule means only that the collected observations did not satisfy a configured rule. It does not establish that all hardware is healthy, connectivity works, or a drive will not fail.

## Implemented rules

| Rule | Evidence and thresholds | Interpretation |
| --- | --- | --- |
| Sustained CPU activity | CPU at or above `cpu_threshold` (default 90%) across at least two contiguous usable observations spanning `sustained_seconds` (default 10 seconds) | Warning about demand; no inference of thermal throttling |
| Sustained memory pressure | RAM at or above `memory_threshold` (default 90%), plus available RAM at or below 10% of total or swap usage at least 50%, for the same sustained window | Warning about pressure; reclaimable cache alone is insufficient |
| Low filesystem space | Mounted volume at least 90% used, with readable capacity and free-space measurements | Attention at 90%, warning at 95%, critical at 99%; this is filesystem space, not physical drive health |
| Persistent CPU-active process | Same PID and creation time, at least 80% of one logical CPU, over the sustained window | Attention; process CPU can exceed 100% across several CPUs and may be expected |
| Process memory growth | Same process identity in at least four contiguous observations spanning at least 60 seconds; growth at least 32 MiB and 25%; at least 80% of successive comparisons do not decrease | Low-confidence attention; a leak is suspected, not confirmed; workload and cache growth can explain it |
| Battery capacity reduction | Readable design and full-charge capacities, design greater than zero, full capacity no greater than design, wear at least 20%, reported wear consistent within two percentage points with capacities | Attention at 20%, warning at 40%; firmware estimates are not guaranteed runtime |
| Collection failure | An explicit collector error | Informational support/permission state, not evidence of a hardware fault |

A usable sustained run resets on a missing/failed measurement, a value below the threshold, a process identity change, or a gap greater than `max(3 × refresh_seconds, 2 seconds)`. Duration is calculated from observation timestamps, not by counting samples. CLI diagnostics configure gap detection to the actual requested interval. Findings are sorted by severity and deduplicated by rule identity.

PID 0 is excluded from process activity and memory-growth findings. Windows uses
this entry for idle-time accounting, so a high value is not application demand.
See [Microsoft's explanation of System Idle Process](https://download.microsoft.com/download/7/3/8/7381E0E8-CE72-4366-9849-13B2BAFBBA3C/Microsoft_Press_ebook_Windows_10_Tools_Mobile-Only.pdf).

## Storage rules

`titan storage diagnose` distinguishes OS-enumerated whole drives from partitions and mounted volumes. It queries optional `smartctl` once, with a bounded total budget, using structured JSON and `-q noserial`. `titan diagnose` also requests one bounded drive-health collection. Ordinary monitoring does not repeatedly query SMART.

Rules explain offline or read-only drives, unknown/unmounted partitions, absent Windows drive letters, RAW filesystems, and Linux USB mass-storage interfaces without corresponding block devices. EFI, recovery, reserved, swap, encrypted-container and other recognized system/container partitions are excluded from ordinary missing-mount warnings where identifiable. Unmounted partitions can be intentional. A USB interface without a block device can be an empty card reader; it does not prove adapter or drive failure.

SMART interpretation distinguishes ATA named sector counters and NVMe fields. Current reported failure, smartctl current-failure bits, and NVMe critical warnings generate critical findings. Positive pending/reallocated/uncorrectable/media-error counters, device-defined spare threshold violations, current attribute threshold violations, or NVMe percentage-used at least 100 generate warnings. Historical self-test/error records generate attention rather than an unsupported current-failure claim. Counters can be cumulative; compare trends and vendor guidance. Temperature and power-on hours are observations rather than universal fault thresholds.

Titan never initializes, partitions, formats, rewrites, or repairs a drive. All recommendations prioritize backups and preservation of unrecovered data. SMART is neither a prediction of exact failure time nor a guarantee of future reliability. Access-denied, absent smartmontools, unsupported USB bridges, or incomplete inventory are shown as unavailable evidence.

## Network tests

Monitoring reads local OS configuration and counters; it does not send probes. `titan network test HOST --port 443 --count 4` explicitly resolves and connects to only the selected endpoint. DNS runs in a bounded daemon worker, TCP has a timeout, and optional ping performs at most five bounded echo attempts. No port scan occurs. Gateway reachability can be tested by explicitly selecting the displayed gateway address.

The result separates DNS success, TCP latency, and ICMP loss/stability. Ping may be blocked while TCP works. The few ICMP attempts describe a short test window, not long-term connection stability. An unsuccessful DNS or TCP test exits 1. The diagnostics evaluator can consume an explicitly supplied `network.data.diagnostic_test` failure; background snapshots do not infer an internet outage from traffic volume or interface state.

## Scope and remaining limitations

There is no sustained disk-busy or storage-latency diagnosis, device reconnect-event correlation, supported thermal-throttling detector, automatic network-outage detection, or long-term connectivity-stability rule in this release. These need additional platform-specific evidence. Battery power draw and desktop wall-power consumption are not estimated. Unsupported GPU sensors remain unavailable. `requires_admin` communicates requirements of a finding; Titan does not silently elevate or perform an administrative repair.

## CLI exit codes

- `0`: the requested operation completed; optional missing tools and ordinary diagnostic findings are reported as data.
- `1`: collection, configuration, export, history or explicit connectivity operation failed.
- `2`: command usage or parameter validation failed.
- `3`: `diagnose --fail-on-warning` found a warning or critical finding.

Machine-readable metric commands accept `--json`. Reports redact usernames, executable paths, network identifiers, names, serials and other sensitive fields by default. A detailed CLI report requires `--include-sensitive --review`, followed by confirmation or the explicit scripted confirmation `--yes`. No report is uploaded.
