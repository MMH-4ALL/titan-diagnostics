"""Build a native console distribution on the current OS."""

import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
build_work = Path(os.environ.get("TITAN_BUILD_WORK", str(root / "build"))).resolve()
os.environ.setdefault("PYINSTALLER_CONFIG_DIR", str(build_work / "cache"))
subprocess.run(
    [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        "titan",
        "--paths",
        str(root / "src"),
        "--collect-all",
        "textual",
        "--collect-all",
        "rich",
        "--collect-all",
        "typer",
        "--copy-metadata",
        "titan-diagnostics",
        "--distpath",
        str(root / "dist" / "native"),
        "--workpath",
        str(build_work / "native"),
        "--specpath",
        str(build_work),
        str(root / "scripts" / "native_entry.py"),
    ],
    check=True,
    cwd=root,
)
