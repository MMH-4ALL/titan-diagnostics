"""Stateful, failure-isolated system measurements independent of rendering."""

from __future__ import annotations

import copy
import logging
import platform
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

import psutil

from titan.core.models import Section, Snapshot
from titan.core.monitoring import DiskSampler, DriveTimeline, drive_identity
from titan.core.processes import ProcessSampler
from titan.hardware.network import collect_network_metadata
from titan.hardware.thermal import ThermalSampler
from titan.hardware.traffic import ProcessTraffic

LOG = logging.getLogger(__name__)


def counter_rate(current: int, previous: int, elapsed: float) -> float | None:
    """No artificial bursts after counter reset, device replacement, or clock reversal."""
    if elapsed <= 0 or current < previous:
        return None
    return (current - previous) / elapsed


class Collector:
    """Reuse one collector for interval percentages and accurate monotonic rates.

    Expensive OS inventory is cached for 30 seconds, GPU for five seconds.
    Collection should run in a worker; this class deliberately has no UI code.
    """

    def __init__(
        self,
        backend: Any = psutil,
        *,
        provider: Any = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        max_processes: int = 2048,
    ) -> None:
        self.backend = backend
        self._provider = provider
        self.monotonic = monotonic
        self.wall_time = wall_time
        self._cpu_primed = False
        self._cpu_previous: tuple[Any, list[Any]] | None = None
        self._cache: dict[str, tuple[float, Any]] = {}
        self._counters: dict[str, tuple[float, dict[str, tuple[int, int]]]] = {}
        self._processes = ProcessSampler(backend, max_processes)
        self._lock = threading.Lock()
        self.timeline = DriveTimeline()
        self.disks = DiskSampler()
        self.thermal = ThermalSampler()
        self.traffic = ProcessTraffic()

    def _get_provider(self) -> Any:
        if self._provider is None:
            from titan.platforms.base import get_provider

            self._provider = get_provider()
        return self._provider

    def _guard(self, name: str, function: Callable[[], Section]) -> Section:
        try:
            return function()
        except Exception as exc:
            # Log the exception type only: OS messages may contain personal paths.
            LOG.warning("%s collection failed (%s)", name, type(exc).__name__)
            permission = isinstance(exc, (PermissionError, self.backend.AccessDenied))
            message = (
                "Permission denied; this information is unavailable to the current user."
                if permission
                else f"Collection failed ({type(exc).__name__}); refresh to retry."
            )
            return Section(error=message, unavailable=[f"{name} measurements"])

    def _cached(self, name: str, seconds: float, function: Callable[[], Any]) -> Any:
        now = self.monotonic()
        existing = self._cache.get(name)
        if existing and 0 <= now - existing[0] < seconds:
            return copy.deepcopy(existing[1])
        result = function()
        self._cache[name] = (self.monotonic(), copy.deepcopy(result))
        return result

    def collect(self) -> Snapshot:
        with self._lock:
            # CPU metadata comes from the same cached system inventory shown to users.
            system = self._guard("system", self._system)
            sections = {
                "cpu": self._guard("cpu", lambda: self._cpu(system)),
                "memory": self._guard("memory", self._memory),
                "processes": self._guard("processes", self._process_section),
                "storage": self._guard("storage", self._storage),
                "network": self._guard("network", self._network),
                "battery": self._guard("battery", self._battery),
                "gpu": self._guard("gpu", self._gpu),
                "system": system,
            }
            stamp = self.wall_time()
            self.timeline.observe(sections["storage"], stamp)
            sections["storage"].data["events"] = list(self.timeline.events)
            thermal = self._guard(
                "thermal",
                lambda: self._cached(
                    "thermal",
                    30 if platform.system() == "Windows" else 5,
                    lambda: self.thermal.collect(self.backend, self._get_provider()),
                ),
            )
            sections["thermal"] = thermal
            temperatures = [
                sensor["temperature_c"]
                for sensor in thermal.data.get("sensors", [])
                if sensor.get("cpu_sensor") and sensor.get("temperature_c") is not None
            ]
            if temperatures:
                sections["cpu"].data["temperature_c"] = max(temperatures)
                sections["cpu"].unavailable = [
                    reason
                    for reason in sections["cpu"].unavailable
                    if "temperature" not in reason.lower()
                ]
            traffic = self._guard(
                "process traffic",
                lambda: self.traffic.collect(sections["processes"].data.get("items", [])),
            )
            sections["process_traffic"] = traffic
            rates = {
                (item["pid"], item["create_time"]): item for item in traffic.data.get("items", [])
            }
            for item in sections["processes"].data.get("items", []):
                activity = rates.get((item.get("pid"), item.get("create_time")), {})
                item["download_bps"] = activity.get("download_bps")
                item["upload_bps"] = activity.get("upload_bps")
            return Snapshot(timestamp=stamp, platform=platform.system(), sections=sections)

    def _system(self) -> Section:
        return self._cached("system", 30, lambda: self._get_provider().collect_system())

    def _cpu(self, system: Section) -> Section:
        total = self.backend.cpu_percent(interval=None)
        per_core = self.backend.cpu_percent(interval=None, percpu=True)
        if hasattr(self.backend, "cpu_times"):
            # psutil.cpu_percent keeps thread-local baselines. Worker pools may
            # move between threads, so preserve our own CPU-time baselines too.
            times = self.backend.cpu_times()
            core_times = self.backend.cpu_times(percpu=True)
            if self._cpu_previous is not None:
                total = self._cpu_usage(times, self._cpu_previous[0])
                previous_cores = self._cpu_previous[1]
                per_core = [
                    self._cpu_usage(current, previous_cores[index])
                    if index < len(previous_cores)
                    else None
                    for index, current in enumerate(core_times)
                ]
            self._cpu_previous = (times, core_times)
        primed = self._cpu_primed
        self._cpu_primed = True
        unavailable = []
        if not primed:
            total = None
            per_core = [None] * len(per_core)
            unavailable.append("CPU usage is warming up; one interval is required.")
        frequency = None
        try:
            frequency = self.backend.cpu_freq()
        except (AttributeError, NotImplementedError, OSError, RuntimeError):
            pass
        temperature = None
        if hasattr(self.backend, "sensors_temperatures"):
            try:
                sensors = self.backend.sensors_temperatures()
                values = [
                    sensor.current
                    for name, entries in sensors.items()
                    if any(
                        token in name.lower()
                        for token in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu")
                    )
                    for sensor in entries
                    if sensor.current is not None
                ]
                if values:
                    temperature = max(values)
            except (OSError, NotImplementedError, self.backend.AccessDenied):
                pass
        if temperature is None:
            unavailable.append("CPU temperature is not exposed by the operating system or driver.")
        if frequency is None:
            unavailable.append("CPU frequency is unavailable.")
        load = None
        if platform.system() != "Windows":
            try:
                load = list(self.backend.getloadavg())
            except (AttributeError, OSError, NotImplementedError):
                pass

        def frequency_value(field: str) -> float | None:
            value = getattr(frequency, field, None)
            return float(value) if value is not None and value > 0 else None

        model = system.data.get("cpu_model") or platform.processor() or None
        return Section(
            data={
                "model": model,
                "manufacturer": system.data.get("cpu_manufacturer"),
                "physical_cores": self.backend.cpu_count(logical=False),
                "logical_cores": self.backend.cpu_count(logical=True),
                "percent": total,
                "per_core": per_core,
                "freq_mhz": frequency_value("current"),
                "min_mhz": frequency_value("min"),
                "max_mhz": frequency_value("max"),
                "temperature_c": temperature,
                "load_avg": load,
            },
            unavailable=unavailable,
        )

    @staticmethod
    def _cpu_usage(current: Any, previous: Any) -> float | None:
        def totals(sample: Any) -> tuple[float, float]:
            fields = sample._asdict()
            # Guest times are included in user/nice on Linux. I/O wait is idle.
            total = sum(fields.values()) - fields.get("guest", 0) - fields.get("guest_nice", 0)
            idle = fields.get("idle", 0) + fields.get("iowait", 0)
            return total, idle

        current_total, current_idle = totals(current)
        previous_total, previous_idle = totals(previous)
        elapsed = current_total - previous_total
        idle_delta = current_idle - previous_idle
        if elapsed <= 0 or idle_delta < 0:
            return None
        return round(min(100.0, max(0.0, 100 * (elapsed - idle_delta) / elapsed)), 1)

    def _memory(self) -> Section:
        memory = self.backend.virtual_memory()
        unavailable = []
        try:
            swap = self.backend.swap_memory()
        except (OSError, RuntimeError, NotImplementedError, self.backend.AccessDenied):
            swap = None
            unavailable.append(
                "Swap/pagefile counters are inaccessible; RAM measurements remain available."
            )
        return Section(
            data={
                "total": memory.total,
                "available": memory.available,
                "used": memory.used,
                "percent": memory.percent,
                "swap_total": None if swap is None else swap.total,
                "swap_used": None if swap is None else swap.used,
                "swap_percent": None if swap is None else swap.percent,
                "cached": getattr(memory, "cached", None),
                "explanation": "Available RAM includes reclaimable caches. High used RAM alone does not establish memory pressure.",
            },
            unavailable=unavailable,
        )

    def _process_section(self) -> Section:
        data = self._processes.collect(self.monotonic())
        unavailable = []
        if data["inaccessible"]:
            unavailable.append(
                f"{data['inaccessible']} process identities could not be read or exited during collection."
            )
        if data["truncated"]:
            unavailable.append(
                f"The process list is limited to {self._processes.max_processes} entries to bound memory use."
            )
        return Section(data=data, unavailable=unavailable)

    def _rates(
        self, group: str, now: float, values: dict[str, tuple[int, int]]
    ) -> dict[str, tuple[float | None, float | None]]:
        previous = self._counters.get(group)
        result: dict[str, tuple[float | None, float | None]] = {}
        for name, current in values.items():
            earlier = None if previous is None else previous[1].get(name)
            if earlier is None:
                result[name] = (None, None)
            else:
                elapsed = now - (previous[0] if previous is not None else now)
                result[name] = (
                    counter_rate(current[0], earlier[0], elapsed),
                    counter_rate(current[1], earlier[1], elapsed),
                )
        self._counters[group] = (now, values)
        return result

    def _storage(self) -> Section:
        inventory = self._guard(
            "storage inventory",
            lambda: self._cached("storage", 10, lambda: self._get_provider().collect_storage()),
        )
        # Provider failures must not conceal readable mounted volume measurements.
        data, unavailable = copy.deepcopy(inventory.data), list(inventory.unavailable)
        data["inventory_observed_at"] = self._cache.get("storage", (self.monotonic(), None))[0]
        unavailable = [reason for reason in unavailable if reason != "Device reconnect history"]
        volumes = []
        for partition in self.backend.disk_partitions(all=False):
            try:
                usage = self.backend.disk_usage(partition.mountpoint)
                volumes.append(
                    {
                        "device": partition.device,
                        "mountpoint": partition.mountpoint,
                        "filesystem": partition.fstype,
                        "total": usage.total,
                        "used": usage.used,
                        "free": usage.free,
                        "percent": usage.percent,
                        "options": partition.opts,
                    }
                )
            except (OSError, self.backend.AccessDenied):
                unavailable.append(
                    "A mounted volume was inaccessible or removed during collection."
                )
        data["volumes"] = volumes
        try:
            counters = self.backend.disk_io_counters(nowrap=False)
            now = self.monotonic()
            values = (
                {} if counters is None else {"total": (counters.read_bytes, counters.write_bytes)}
            )
            rates = self._rates("disk", now, values).get("total", (None, None))
            data.update(
                {
                    "read_bps": rates[0],
                    "write_bps": rates[1],
                    "read_bytes": None if counters is None else counters.read_bytes,
                    "write_bytes": None if counters is None else counters.write_bytes,
                }
            )
        except (OSError, RuntimeError, NotImplementedError, self.backend.AccessDenied):
            data.update({"read_bps": None, "write_bps": None})
            unavailable.append("Disk activity counters are inaccessible.")
        try:
            per_disk = self.backend.disk_io_counters(perdisk=True, nowrap=False) or {}
            provider = self._get_provider()
            native = getattr(provider, "collect_disk_performance", None)
            if native:
                precise = native(list(per_disk))
                queues = {name: value.queue_depth for name, value in precise.items()}
                per_disk = {
                    name: precise.get(name) or value._replace(read_time=None, write_time=None)
                    for name, value in per_disk.items()
                }
            else:
                queues = self._cached(
                    "disk-queues", 10, getattr(provider, "collect_disk_queues", lambda: {})
                )
            data["disk_activity"] = self.disks.sample(
                per_disk,
                self.monotonic(),
                queues=queues,
                sysfs=getattr(provider, "sysfs", None),
                identities={
                    str(drive.get("id"))
                    if data.get("platform") == "Windows"
                    else str(drive.get("device", "")).rsplit("/", 1)[-1]: drive_identity(drive)
                    for drive in data.get("drives", [])
                }
                if data.get("inventory_complete")
                else None,
            )
            data["disk_activity_note"] = (
                "Latency is interval I/O time divided by completed operations; idle intervals have no latency. Windows uses native 100ns time counters; Linux uses OS millisecond counters. Queue depth is instantaneous. Devices and partitions can overlap; do not sum them. Busy time is not a universal NVMe saturation measure."
            )
        except (
            OSError,
            RuntimeError,
            TypeError,
            AttributeError,
            NotImplementedError,
            self.backend.AccessDenied,
        ):
            data["disk_activity"] = []
            unavailable.append("Per-device latency/queue counters")
        return Section(data=data, unavailable=unavailable, error=inventory.error)

    def _connections(self) -> dict[str, Any]:
        try:
            connections = []
            for connection in self.backend.net_connections(kind="inet")[:4096]:
                local = connection.laddr
                remote = connection.raddr
                connections.append(
                    {
                        "pid": connection.pid,
                        "status": connection.status,
                        "local_address": local.ip if local else None,
                        "local_port": local.port if local else None,
                        "remote_address": remote.ip if remote else None,
                        "remote_port": remote.port if remote else None,
                        "family": "IPv6" if connection.family == socket.AF_INET6 else "IPv4",
                    }
                )
            return {"connections": connections, "unavailable": []}
        except (OSError, RuntimeError, self.backend.AccessDenied, NotImplementedError):
            return {
                "connections": [],
                "unavailable": [
                    "Active connection details require additional permissions or are unsupported."
                ],
            }

    def _network(self) -> Section:
        counters = self.backend.net_io_counters(pernic=True, nowrap=False) or {}
        now = self.monotonic()
        values = {
            name: (counter.bytes_sent, counter.bytes_recv) for name, counter in counters.items()
        }
        previous_interfaces = self._counters.get("network")
        topology_changed = previous_interfaces is not None and set(previous_interfaces[1]) != set(
            values
        )
        rates = self._rates("network", now, values)
        aggregate = (
            sum(item[0] for item in values.values()),
            sum(item[1] for item in values.values()),
        )
        total_rates = self._rates("network-total", now, {"total": aggregate})["total"]
        if topology_changed:
            total_rates = (None, None)
        addresses = self.backend.net_if_addrs()
        states = self.backend.net_if_stats()
        interfaces = []
        for name in sorted(set(addresses) | set(states) | set(counters)):
            state = states.get(name)
            counter = counters.get(name)
            speed = getattr(state, "speed", None)
            interface_rates = rates.get(name, (None, None))
            interfaces.append(
                {
                    "name": name,
                    "is_up": None if state is None else state.isup,
                    "addresses": [
                        address.address
                        for address in addresses.get(name, [])
                        if address.family in (socket.AF_INET, socket.AF_INET6)
                    ],
                    "speed_mbps": speed if speed and speed > 0 else None,
                    "bytes_sent": None if counter is None else counter.bytes_sent,
                    "bytes_recv": None if counter is None else counter.bytes_recv,
                    "upload_bps": interface_rates[0],
                    "download_bps": interface_rates[1],
                }
            )
        metadata = self._cached("network-metadata", 30, collect_network_metadata)
        connections = self._cached("connections", 5, self._connections)
        unavailable = list(metadata.get("unavailable", [])) + list(
            connections.get("unavailable", [])
        )
        if not counters:
            unavailable.append("Network byte counters are unavailable.")
        return Section(
            data={
                "interfaces": interfaces,
                "upload_bps": total_rates[0] if counters else None,
                "download_bps": total_rates[1] if counters else None,
                "bytes_sent": aggregate[0] if counters else None,
                "bytes_recv": aggregate[1] if counters else None,
                "connections": connections["connections"],
                "dns": metadata.get("dns", []),
                "gateway": metadata.get("gateway"),
                "transfer_note": "Totals combine OS interface counters; virtual adapters can count the same traffic twice.",
            },
            unavailable=unavailable,
        )

    def _battery(self) -> Section:
        battery = self.backend.sensors_battery()
        if battery is None:
            return Section(
                data={
                    "present": False,
                    "percent": None,
                    "plugged": None,
                    "seconds_left": None,
                    "message": "No battery is reported. Desktop power consumption is not estimated.",
                }
            )
        try:
            metadata = self._cached(
                "battery", 30, lambda: self._get_provider().collect_battery_metadata()
            )
        except Exception as exc:
            metadata = {
                "unavailable": [f"Battery capacity metadata is unavailable ({type(exc).__name__})."]
            }
        unavailable = list(metadata.pop("unavailable", []))
        remaining = battery.secsleft
        data = dict(metadata)
        data.update(
            {
                "present": True,
                "percent": battery.percent,
                "plugged": battery.power_plugged,
                "seconds_left": remaining if remaining is not None and remaining >= 0 else None,
                "runtime_note": "Remaining runtime is an operating-system estimate, not a guaranteed lifetime.",
            }
        )
        batteries = data.get("batteries", [])
        if len(batteries) == 1:
            for key in (
                "design_capacity_mwh",
                "full_capacity_mwh",
                "wear_percent",
                "cycle_count",
                "temperature_c",
            ):
                data[key] = batteries[0].get(key)
        return Section(data=data, unavailable=unavailable)

    def _gpu(self) -> Section:
        return self._cached("gpu", 5, lambda: self._get_provider().collect_gpu())
