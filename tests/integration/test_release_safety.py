import csv
import io
import socket
import subprocess
import sys
import threading
import time

import psutil

from titan.core.models import Section, Snapshot
from titan.core.processes import terminate_process
from titan.hardware.network import network_test
from titan.reports.export import export_processes


def test_explicit_local_endpoint_dns_and_tcp(monkeypatch):
    """Contact only a test server we create on loopback; never the internet."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(3)
    port = listener.getsockname()[1]
    accepted = threading.Event()

    def server():
        try:
            connection, _ = listener.accept()
            with connection:
                accepted.set()
        finally:
            listener.close()

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    monkeypatch.setattr("titan.hardware.network.shutil.which", lambda _: None)
    result = network_test("127.0.0.1", port=port, timeout=1, count=1)
    thread.join(4)
    assert accepted.is_set()
    assert result.data["dns"]["success"] is True
    assert result.data["tcp"]["success"] is True
    assert result.data["tcp"]["latency_ms"] >= 0


def test_confirmed_termination_only_of_owned_test_child(monkeypatch):
    """Exercise the actual OS action only on the child this test creates."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        # CI systems may assign all jobs to an outer service cgroup. Ownership
        # parsing is tested separately; this integration tests only our child.
        monkeypatch.setattr("titan.core.processes._services", lambda *_: [])
        identity = psutil.Process(child.pid).create_time()
        result = terminate_process(child.pid, identity, confirmed=True)
        child.wait(timeout=5)
        assert result["termination_requested"]
        assert result["pid"] == child.pid
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


def test_process_export_is_tabular_redacted_and_formula_safe():
    snapshot = Snapshot(
        time.time(),
        "test",
        {
            "processes": Section(
                {
                    "items": [
                        {
                            "pid": 123,
                            "name": "=bad-formula",
                            "exe": "C:/Users/Test/private.exe",
                            "username": "secret-user",
                            "memory_bytes": 100,
                        }
                    ]
                }
            )
        },
    )
    text = export_processes(snapshot)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 1 and rows[0]["pid"] == "123"
    assert "secret-user" not in text and "private.exe" not in text
    detailed = export_processes(snapshot, include_sensitive=True)
    assert "'=bad-formula" in detailed
