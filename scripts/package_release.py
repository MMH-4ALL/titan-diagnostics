"""Prepare checksum-ready release assets for this platform's native build."""

import hashlib
import platform
import shutil
import sys
import tomllib
import zipfile
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    native = root / "dist/native/titan"
    release = root / "dist/release"
    release.mkdir(parents=True, exist_ok=True)
    system = {"Windows": "windows", "Linux": "linux"}.get(platform.system())
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine().lower()
    )
    if not system or not arch:
        raise SystemExit("Unsupported native release platform")
    for name in ["LICENSE", "THIRD_PARTY_NOTICES.md", "RELEASE_SUMMARY.md"]:
        shutil.copy2(root / name, native / name)
    shutil.copytree(
        root / "third_party_licenses", native / "third_party_licenses", dirs_exist_ok=True
    )
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, native / "third_party_licenses/Python-LICENSE.txt")
    with zipfile.ZipFile(
        release / f"Titan-{version}-{system}-{arch}.zip", "w", zipfile.ZIP_DEFLATED
    ) as archive:
        for path in sorted(native.rglob("*")):
            if path.is_file():
                archive.write(path, Path("Titan") / path.relative_to(native))
    for pattern in ["*.whl", "*.tar.gz"]:
        for path in (root / "dist").glob(pattern):
            shutil.copy2(path, release / path.name)
    for path in (root / "install").iterdir():
        if path.is_file():
            if path.suffix == ".sh":
                (release / path.name).write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
            else:
                shutil.copy2(path, release / path.name)
    checksums = "\n".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in sorted(release.iterdir())
        if path.is_file() and path.name != "SHA256SUMS.txt"
    )
    (release / "SHA256SUMS.txt").write_text(checksums + "\n")


if __name__ == "__main__":
    main()
