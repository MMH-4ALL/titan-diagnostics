import json
import socket
import threading

from typer.testing import CliRunner

from titan import cli
from titan.core.models import Section, Snapshot
from titan.hardware.stability import StabilityTest


def test_cli_snapshot_roundtrip_and_rejected_file(monkeypatch, tmp_path):
    snapshot = Snapshot(
        100, "Test", {"cpu": Section({"percent": 10}), "thermal": Section({"sensors": []})}
    )
    monkeypatch.setattr(cli, "_collect", lambda: snapshot)
    runner = CliRunner()
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    assert runner.invoke(cli.app, ["snapshot", "save", str(before)]).exit_code == 0
    snapshot.timestamp = 120
    snapshot.sections["cpu"].data["percent"] = 30
    assert runner.invoke(cli.app, ["snapshot", "save", str(after)]).exit_code == 0
    result = runner.invoke(cli.app, ["snapshot", "compare", str(before), str(after), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["measurements"][0]["change"] == 20
    after.write_text("not json")
    assert runner.invoke(cli.app, ["snapshot", "compare", str(before), str(after)]).exit_code == 1
    assert runner.invoke(cli.app, ["thermal", "--json"]).exit_code == 0
    assert runner.invoke(cli.app, ["hardware-map"]).exit_code == 0


def test_actual_loopback_stability_and_refused_service():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(10)
    server.settimeout(0.1)
    port = server.getsockname()[1]
    stop = threading.Event()

    def accept():
        while not stop.is_set():
            try:
                connection, _ = server.accept()
                connection.close()
            except TimeoutError:
                pass

    worker = threading.Thread(target=accept)
    worker.start()
    try:
        result = StabilityTest("127.0.0.1", port, duration=1).run()
        assert result["attempts"] == 1 and result["failures"] == 0
        assert result["median_ms"] >= 0
    finally:
        stop.set()
        worker.join(2)
        server.close()
    result = StabilityTest("127.0.0.1", port, duration=1, timeout=0.2).run()
    assert result["failures"] == 1 and result["dns_failures"] == 0
