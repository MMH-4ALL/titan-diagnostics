"""Safety and reuse checks for the private-runtime installer."""

import importlib.util
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("bootstrap", ROOT / "install/bootstrap.py")
assert spec and spec.loader
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def make_wheel(path: Path, name: str = "titan-diagnostics", version: str = "0.2.0") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "titan_diagnostics.dist-info/METADATA", f"Name: {name}\nVersion: {version}\n"
        )
    return path


def test_rejects_non_titan_wheel_and_unsafe_version(tmp_path):
    with pytest.raises(ValueError, match="not a Titan"):
        bootstrap.wheel_version(make_wheel(tmp_path / "other.whl", name="other"))
    with pytest.raises(ValueError, match="not a Titan"):
        bootstrap.wheel_version(make_wheel(tmp_path / "unsafe.whl", version="../../bad"))


def test_cached_linux_python_symlink_is_reused(tmp_path):
    runtime = tmp_path / "versions/one/bin/python"
    runtime.parent.mkdir(parents=True)
    actual = tmp_path / "system-python"
    actual.write_text("fixture")
    try:
        runtime.symlink_to(actual)
    except OSError:
        runtime.write_text("fixture")
    marker = tmp_path / "ready.json"
    marker.write_text(json.dumps({"python": str(runtime)}))
    assert bootstrap.cached_python(marker, tmp_path) == runtime
    marker.write_text(json.dumps({"python": str(actual)}))
    assert bootstrap.cached_python(marker, tmp_path) is None


def test_failed_install_removes_only_its_new_environment(tmp_path, monkeypatch):
    wheel = make_wheel(tmp_path / "titan.whl")
    prefix = tmp_path / "install"
    old = prefix / "versions/old"
    old.mkdir(parents=True)
    (old / "keep.txt").write_text("keep")

    def fail(command, **kwargs):
        Path(command[-1]).mkdir(parents=True)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(bootstrap.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        bootstrap.prepare(wheel, prefix)
    assert list((prefix / "versions").iterdir()) == [old]
    assert (old / "keep.txt").read_text() == "keep"
    assert not list(prefix.glob("*.lock"))


def test_linux_shim_quotes_paths_and_preserves_unrelated_command(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.sys, "platform", "linux")
    runtime = tmp_path / "a space's/python"
    target = bootstrap.install_shim(runtime, tmp_path / "bin")
    assert '"$@"' in target.read_text()
    assert "'\"'\"'" in target.read_text()
    if os.name == "posix":
        assert os.stat(target).st_mode & 0o111
    target.write_text("existing user command")
    with pytest.raises(ValueError, match="already exists"):
        bootstrap.install_shim(runtime, tmp_path / "bin")
    assert target.read_text() == "existing user command"


def test_prepare_rejects_filesystem_root(tmp_path):
    with pytest.raises(ValueError, match="filesystem root"):
        bootstrap.prepare(make_wheel(tmp_path / "titan.whl"), Path(tmp_path.anchor))


def test_portable_scripts_have_unix_line_endings():
    for path in [ROOT / "install/install.sh", ROOT / "npm/bin/titan.cjs"]:
        assert b"\r" not in path.read_bytes(), f"{path.name} must use LF on every build OS"
