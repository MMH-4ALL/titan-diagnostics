"""Prepare a private wheel installation; stdout contains only the Python path."""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from email.parser import Parser
from pathlib import Path


def wheel_version(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        metadata = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise ValueError("Expected one distribution in the Titan wheel")
        info = Parser().parsestr(archive.read(metadata[0]).decode("utf-8"))
    version = str(info.get("Version", ""))
    if (
        info.get("Name") != "titan-diagnostics"
        or not version
        or any(c not in "0123456789abcdefghijklmnopqrstuvwxyz.-+" for c in version)
    ):
        raise ValueError("This is not a Titan distribution")
    return version


def default_prefix() -> Path:
    override = os.environ.get("TITAN_INSTALL_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
            / "Titan/cli-python"
        )
    return (
        Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "titan/install"
    )


def atomic_text(path: Path, text: str, mode: int = 0o600) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".titan-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def cached_python(marker: Path, prefix: Path) -> Path | None:
    try:
        runtime = Path(json.loads(marker.read_text(encoding="utf-8"))["python"]).absolute()
        if runtime.parent.resolve().is_relative_to(prefix / "versions") and runtime.is_file():
            return runtime
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def prepare(wheel: Path, prefix: Path) -> Path:
    if sys.version_info < (3, 11):  # noqa: UP036 - this also bootstraps outside the package
        raise ValueError("Python 3.11 or newer is required")
    prefix = prefix.expanduser().resolve()
    if prefix == Path(prefix.anchor):
        raise ValueError("Choose a dedicated Titan installation folder, not a filesystem root")
    version = wheel_version(wheel)
    key = f"{version}-{hashlib.sha256(wheel.read_bytes()).hexdigest()[:16]}"
    prefix.mkdir(parents=True, exist_ok=True)
    marker = prefix / f"{key}.json"
    runtime = cached_python(marker, prefix)
    if runtime:
        return runtime
    lock = prefix / f".{key}.lock"
    deadline = time.monotonic() + 30
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            runtime = cached_python(marker, prefix)
            if runtime:
                return runtime
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Another install holds {lock}. Wait for it to finish. If it crashed, remove that empty lock folder and retry."
                ) from None
            time.sleep(0.25)
    environment = prefix / "versions" / f"{key}-{uuid.uuid4().hex[:8]}"
    try:
        runtime = cached_python(marker, prefix)
        if runtime:
            return runtime
        print(
            "Titan: preparing a private Python environment. First installation downloads Python dependencies; later launches work offline.",
            file=sys.stderr,
        )
        subprocess.run(
            [sys.executable, "-m", "venv", str(environment)], check=True, stdout=sys.stderr
        )
        runtime = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        subprocess.run(
            [
                str(runtime),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                str(wheel),
            ],
            check=True,
            stdout=sys.stderr,
        )
        subprocess.run(
            [
                str(runtime),
                "-c",
                "from titan import __version__; import textual, psutil; assert __version__ == "
                + repr(version),
            ],
            check=True,
            stdout=sys.stderr,
        )
        atomic_text(marker, json.dumps({"version": version, "python": str(runtime)}) + "\n")
        return runtime
    except BaseException:
        # Delete only the unique environment created by this attempt.
        if environment.resolve().is_relative_to(prefix / "versions") and environment.is_dir():
            shutil.rmtree(environment)
        raise
    finally:
        lock.rmdir()


def install_shim(runtime: Path, bin_dir: Path) -> Path:
    if sys.platform != "linux":
        raise ValueError("The shell installer supports Linux; use install.ps1 on Windows")
    bin_dir = bin_dir.expanduser().resolve()
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "titan"
    signature = "# Managed by Titan installer"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or signature not in target.read_text(encoding="utf-8"):
            raise ValueError(
                f"{target} already exists and is not managed by Titan. Choose --bin-dir."
            )
    atomic_text(
        target, f'#!/bin/sh\n{signature}\nexec {shlex.quote(str(runtime))} -m titan "$@"\n', 0o755
    )
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, default=default_prefix())
    parser.add_argument("--bin-dir", type=Path)
    args = parser.parse_args()
    try:
        runtime = prepare(args.wheel.resolve(), args.prefix)
        if args.bin_dir:
            target = install_shim(runtime, args.bin_dir)
            print(f"Titan installed: {target}", file=sys.stderr)
        print(runtime)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Titan installation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
