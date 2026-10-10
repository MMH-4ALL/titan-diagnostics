"""Build a self-contained npm tarball from the wheel and optional native folders."""

import argparse
import json
import platform
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", action="append", default=[], metavar="PLATFORM-ARCH=DIR")
    parser.add_argument("--out", type=Path, default=root / "dist")
    args = parser.parse_args()
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    wheel = root / "dist" / f"titan_diagnostics-{version}-py3-none-any.whl"
    if not wheel.is_file():
        raise SystemExit("Build the Python wheel first: python -m build")
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="titan-npm-") as temporary:
        stage = Path(temporary)
        shutil.copytree(root / "npm" / "bin", stage / "bin")
        shutil.copy2(root / "install" / "bootstrap.py", stage / "bootstrap.py")
        shutil.copy2(root / "npm" / "README.md", stage / "README.md")
        for name in ["LICENSE", "THIRD_PARTY_NOTICES.md"]:
            shutil.copy2(root / name, stage / name)
        (stage / "wheel").mkdir()
        shutil.copy2(wheel, stage / "wheel" / wheel.name)
        package = json.loads((root / "npm" / "package.json").read_text())
        package["version"] = version
        package.pop("scripts", None)
        (stage / "package.json").write_text(json.dumps(package, indent=2) + "\n")
        for specification in args.native:
            key, separator, directory = specification.partition("=")
            if not separator or key not in {"win32-x64", "linux-x64", "linux-arm64", "win32-arm64"}:
                raise SystemExit(
                    "Use --native PLATFORM-ARCH=DIR with a supported Windows/Linux target"
                )
            source = Path(directory).resolve()
            if not (source / ("titan.exe" if key.startswith("win32") else "titan")).is_file():
                raise SystemExit(f"Native executable missing from {source}")
            shutil.copytree(source, stage / "native" / key)
        npm = shutil.which("npm.cmd" if platform.system() == "Windows" else "npm")
        if not npm:
            raise SystemExit("Install Node.js/npm to build the npm package")
        packed = subprocess.run(
            [
                npm,
                "pack",
                str(stage),
                "--pack-destination",
                str(args.out.resolve()),
                "--ignore-scripts",
                "--json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(packed.stdout)[0]
        print(
            json.dumps(
                {key: result[key] for key in ["filename", "size", "unpackedSize", "entryCount"]}
            )
        )


if __name__ == "__main__":
    main()
