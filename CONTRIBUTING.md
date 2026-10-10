# Contributing

Use Python 3.11+, an isolated environment, and `pip install -e ".[dev,bundle]"`.
Before proposing a change run pytest, Ruff checking and formatting, mypy, and
`python -m build`. UI tests must use Textual's Pilot and cover keyboard input,
resizing, unavailable measurements, and shutdown.

Keep operating system queries in providers, normalized measurement collection
outside rendering, and read-only behavior as the default. New subprocesses must
use argument arrays, a fixed executable, finite timeouts and `shell=False`.
Do not introduce telemetry, silent elevation, disk writes, automatic network
probes or unconfirmed process termination. A missing metric is `None` with a
reason, never a guessed value. Protect privacy in report tests.

Include a fixture for inaccessible APIs and real hardware verification steps
for every new hardware integration. Record exactly which operating systems,
drivers and devices were tested. Never treat a parser fixture as a successful
hardware test. Update documentation when changing rules, thresholds or support.

Submit a focused pull request explaining the user-visible change and validation.
Do not attach personal reports or raw hardware serial numbers.
