import socket
import subprocess
import threading
import time

import pytest

from titan.hardware import network


@pytest.mark.parametrize(
    "target", ["--help", "host;echo", "https://example.com/path", " host", "a\nb", "", "a" * 254]
)
def test_network_targets_reject_urls_options_and_shell_characters(target):
    with pytest.raises(ValueError):
        network.network_test(target)


def test_dns_timeout_prevents_other_probes(monkeypatch):
    def timeout(*args):
        raise TimeoutError("timed out")

    monkeypatch.setattr(network, "_resolve", timeout)
    monkeypatch.setattr(
        network.socket, "socket", lambda *args: pytest.fail("TCP should not be attempted")
    )
    result = network.network_test("example.com", timeout=0.2)
    assert result.error
    assert result.data["dns"]["success"] is False
    assert result.data["tcp"]["success"] is None


def test_resolver_timeout_has_a_single_bounded_worker(monkeypatch):
    release = threading.Event()
    calls = []

    def blocked(*args, **kwargs):
        calls.append(1)
        release.wait(1)
        return []

    monkeypatch.setattr(network, "_RESOLVER_SLOT", threading.BoundedSemaphore(1))
    monkeypatch.setattr(network.socket, "getaddrinfo", blocked)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="timed out"):
            network._resolve("example.com", 443, 0.02)
        with pytest.raises(TimeoutError, match="previous DNS"):
            network._resolve("example.com", 443, 0.02)
        assert len(calls) == 1
        assert time.monotonic() - started < 0.2
    finally:
        release.set()


def test_tcp_timeout_and_missing_ping_are_explicit(monkeypatch):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            assert value == 0.2

        def connect(self, address):
            raise TimeoutError()

    monkeypatch.setattr(
        network,
        "_resolve",
        lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))],
    )
    monkeypatch.setattr(network.socket, "socket", lambda *args: Connection())
    monkeypatch.setattr(network.shutil, "which", lambda command: None)
    result = network.network_test("example.com", timeout=0.2)
    assert result.data["dns"]["success"]
    assert result.data["tcp"]["success"] is False
    assert result.data["ping"]["packet_loss_percent"] is None
    assert "ping utility is unavailable" in result.unavailable[0]


@pytest.mark.parametrize(
    "output,expected_replies",
    [
        ("Reply from ::1: time<1ms", 1),
        ("Reply from ::1: Destination host unreachable.", 0),
        ("Respuesta desde ::1: tiempo<1ms", 0),
    ],
)
def test_windows_ipv6_reply_without_ttl_is_recognized(monkeypatch, output, expected_replies):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            pass

        def connect(self, address):
            pass

    monkeypatch.setattr(network.sys, "platform", "win32")
    monkeypatch.setattr(
        network,
        "_resolve",
        lambda *args: [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))],
    )
    monkeypatch.setattr(network.socket, "socket", lambda *args: Connection())
    monkeypatch.setattr(network.shutil, "which", lambda command: "ping")
    monkeypatch.setattr(
        network.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=output),
    )
    result = network.network_test("::1", count=1, timeout=0.2)
    assert result.data["ping"]["replies"] == expected_replies
    if "Respuesta" in output:
        assert result.data["ping"]["packet_loss_percent"] is None
        assert not result.data["ping"]["available"]
