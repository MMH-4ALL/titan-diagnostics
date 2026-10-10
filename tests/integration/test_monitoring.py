"""Live tests touch only a loopback listener and a test-created child process."""

import socket
import subprocess
import sys

import psutil
import pytest

from titan.core import processes
from titan.hardware import network


def test_explicit_loopback_target_connects_without_external_traffic(monkeypatch):
    monkeypatch.setattr(network.shutil, "which", lambda name: None)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        result = network.network_test("127.0.0.1", port=port, count=1, timeout=1)
    assert result.data["dns"]["addresses"] == ["127.0.0.1"]
    if result.data["tcp"].get("error") == "PermissionError":
        pytest.skip("This restricted host blocks even loopback TCP connections.")
    assert result.data["tcp"]["success"] is True
    assert result.data["tcp"]["latency_ms"] is not None


def test_confirmed_termination_of_a_test_created_child(monkeypatch):
    # Ownership/service detection is exhaustively mocked in unit tests. This
    # test validates the real OS termination operation on only our own child.
    monkeypatch.setattr(processes, "_services", lambda pid, backend: [])
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        identity = psutil.Process(child.pid).create_time()
        result = processes.terminate_process(child.pid, identity, confirmed=True)
        assert result["termination_requested"] is True
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
