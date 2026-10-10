# Monitoring, snapshots and stability

All collectors remain local. Missing measurements are shown as unavailable.
Storage queries are read-only. Network probes and TCP tracing start only after
an explicit user action. No tools are installed and no elevation is requested.

## Drive connection timeline

Monitoring shows initial presence separately from connection, disconnection and
reconnection events. The timeline keeps 512 events in memory for this session.
Physical inventory uses a ten-second cache after each completed query; slow
queries or a longer refresh interval increase the observation gap. Events between
polls can be missed. Failed or incomplete inventory never generates a disconnect.
This is a polling timeline, not a replay of the operating system's event log.

Windows hashes available Get-Disk/CIM hardware identities. Linux hashes available
sysfs serial/WWID identities. Raw serial values are discarded during normalization.
Fallback identity uses location/model/size and may not distinguish an identical
replacement; quality is displayed. An OS identity can also change when a device
is moved between transport drivers. A reconnect is an observation, not a diagnosis
of a failing drive or USB cable.

## Disk response and queue depth

Read/write latency equals the change in accumulated I/O time divided by the
change in completed operations. Throughput and IOPS use measured monotonic
intervals. Idle intervals have no latency. Counter decreases, clock reversal,
device disappearance or a changed known hardware identity reset the baseline.

Windows reads `DISK_PERFORMANCE` using `IOCTL_DISK_PERFORMANCE`, with no requested
write access. Times convert from 100ns units to milliseconds before calculating
latency; this avoids the coarse time conversion in psutil 7.2.2's Windows API.
Buffers are allocated at their actual capacity, bounded to 64 KiB, and handles
are always closed. Linux uses psutil disk times and sysfs `inflight` queue depth.
Queue depth is instantaneous, not an interval average. Device/partition counters
may overlap. Linux busy time does not establish NVMe saturation. No benchmark,
test writes, performance-counter setup or disk configuration changes are made.

API references: [Windows disk performance](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ns-winioctl-disk_performance),
[psutil disk counters](https://psutil.readthedocs.io/stable/index.html#psutil.disk_io_counters).

## SMART trends

**Read drive health now** invokes existing smartctl with bounded read-only queries.
Manual checks have a 60-second cooldown. During this session, successful readings
can be compared without writing to disk. **Track SMART every 5 minutes** loads
and writes `health.sqlite3` in Titan's local data directory and starts periodic
reads for this session. Stop the switch to pause reads and writes; previous data
remains. The switch starts off on each launch.

The store contains timestamp, hashed identity and named numerical counters only.
Writes prune points older than 365 days and cap the store at 4096 points.
Increasing reallocated/pending/uncorrectable sectors, NVMe media errors, grown
defects and endurance consumption, or decreasing spare percentage, produce a
change alert. Endurance consumption can be expected with use. Temperature swings
and vendor error-log entry counts are not treated as deterioration. Missing or
failed SMART reads never establish a healthy state or create a baseline. Counter
resets do not produce growth alerts. Identity quality must be considered before
attributing a change to the same drive. Corrupt stores are preserved and reported.

## Before/after snapshots

Snapshots offers **Capture before**, **Capture after + compare**, and **Load as
baseline**. Files are saved under Titan's local `snapshots` folder, including a
comparison JSON after an after capture. CLI output paths are explicit:

```console
titan snapshot save before.json
titan snapshot save after.json
titan snapshot compare before.json after.json --json
titan snapshot list --json
```

Snapshots redact identifiers using the report policy. Hashes used for inventory
matching remain persistent pseudonymous identifiers; redacted does not mean
anonymous. JSON is size/schema checked and snapshot writes are atomic. Comparison
shows CPU/memory/disk/network/battery deltas, accessible inventory counts, added
and removed inventory fingerprints, and available CPU/OS metadata changes.
Unavailable data cannot produce a numeric delta. Platform changes are identified.
Use snapshots of the same computer under comparable workloads: point samples
are not benchmarks and do not establish that a change caused an improvement.

## Process I/O and TCP attribution

Process I/O uses PID plus creation time. Linux bytes are storage-accounted OS
counters; Windows bytes include file, network and device I/O. They do not equal
physical disk bandwidth. Access-denied or reset counters stay unavailable.

The TCP tracing switch is off by default. Windows uses native IPv4 TCP EStats
for connections observed in two samples; enabling its measurement counters needs
existing administrator rights. It does not elevate. Short-lived connections,
IPv6, UDP and TCP headers are excluded. It requests measurement counters only
after tracing starts and stops reading when tracing stops. Following Microsoft's
guidance, counters are left on until connection close to avoid disrupting other
monitors. Denied connections remain visible as missing coverage.

Linux uses an already installed BCC `tcptop` tool with existing BPF permissions.
Each bounded invocation samples one second of IPv4/IPv6 TCP, separated by setup
gaps. Values have the tool's 1 KiB rounding resolution. UDP and unobserved flows
are excluded; absent rows are unknown, not zero. Identity is revalidated after
tracing to prevent PID reuse from inheriting traffic. Unsupported kernels, tools,
permissions or tracing deadlines produce an unavailable status.

References: [Microsoft TCP EStats](https://learn.microsoft.com/en-us/windows/win32/api/iphlpapi/nf-iphlpapi-setpertcpconnectionestats),
[BCC tcptop](https://github.com/iovisor/bcc/blob/master/tools/tcptop_example.txt).

## TCP stability tests

Network uses your entered hostname/IP, one chosen port and a bounded duration
(1–3600 seconds). **Stop test** cancels further probes; an in-flight operation
finishes within its bounded timeout. CLI Ctrl+C prints partial results.
Successful handshake latency, median, p95, successive-success jitter, TCP
failures and DNS failures are separate. DNS is resolved each probe and up to
eight returned addresses share one timeout budget. The tool sends no application
payload; connections can appear in endpoint logs. Failure percentage is not
packet loss, bandwidth, or proof that the whole internet is unavailable. A refused
connection may be a closed service. Results persist in reports for this session.

## Thermal evidence and ASCII hardware map

Linux exposes named CPU sensors with reported high/critical thresholds where
available. Read-only sysfs thermal throttle counters prove events between reads;
package counters mirror across CPUs and are not summed. They do not establish
continuous throttling. Counter resets establish a new baseline. Windows can read
CPU temperature from an already running LibreHardwareMonitor/OpenHardwareMonitor
WMI provider; it does not install or start a privileged sensor driver. Direct
Windows thermal-throttle counters remain unavailable. Frequency alone never
produces a throttling diagnosis.

The hardware map uses terminal-safe ASCII and bounded wrapping. It includes
compute, RAM, detected drives and partitions, GPUs and network interfaces.
Branches show inventory membership, not inferred PCI or USB wiring.

Reference: [Linux thermal throttle counters](https://www.kernel.org/doc/html/latest/admin-guide/thermal/intel_thermal_throttle.html).
