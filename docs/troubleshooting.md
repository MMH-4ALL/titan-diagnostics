# Troubleshooting

**No drive in File Explorer/file manager:** open Storage and choose the external
drive check. Inventory separates physical drives from mounted volumes. A detected
disk without a visible mount is direct evidence; it does not prove corruption or
that the disk is blank. Unrecognized filesystems and read-only/offline states are
reported separately. Keep important unrecovered data intact. Do not initialize or
format a drive simply because Windows offers to do so.

**No physical inventory:** some Windows CIM/Storage cmdlets, Linux sysfs files or
lsblk may be inaccessible. Mounted volumes can still be available through psutil.
Titan displays the specific unavailable section. It does not bypass permissions.

**SMART unavailable:** check `titan doctor`; smartctl may be missing, permissions
insufficient or a USB bridge unsupported. SMART commands can return nonzero health
status while still producing valid JSON. Titan interprets those flags. A passed
SMART report does not guarantee the device will not fail.

**CPU or network briefly unavailable:** a new collector first primes the counters.
An interval percentage or transfer rate requires a second sample. Device counter
resets also produce an unavailable rate rather than an artificial spike.

**Pagefile/network connections unavailable on Windows:** some performance counters
or connection APIs can fail even when RAM and interface counters work. Titan keeps
submetrics separate so usable information is retained.

**Temperature or GPU metrics missing:** Windows has no universally supported CPU
temperature source. GPU inventory and vendor telemetry are separate; driver/tools
and permissions determine which metrics are readable. Missing sensors are not a
hardware fault finding.

**Connectivity test failure:** select your own known endpoint. DNS failures, TCP
refusals and unanswered ICMP probes are distinct. Firewalls may block ICMP despite
working applications, and an endpoint may not offer the chosen TCP port. One test
cannot establish long-term connection stability. Titan does not test automatically.

**Cannot terminate a process:** it may have exited, its PID may have been reused,
permissions may be insufficient, or it may be a protected/service process. Use the
operating system's service controls for services. No automatic elevation or forced
kill follows a refusal.

**History cannot open:** Titan preserves corrupt/incompatible databases. Back up
the file, choose a new history location, and re-enable recording after checking the
message. The application does not erase or repair the old file automatically.

**Invalid settings:** CLI exits with an error. The interactive app starts with safe
defaults and shows a warning. Review `titan config --help` and restore valid TOML.

**Small terminal:** below 80 columns the sidebar hides and panels stack. Use number
shortcuts or Ctrl+P, scroll, or enlarge the terminal. High Contrast and Monochrome
themes help limited-color terminals; actual colors remain terminal-dependent.
