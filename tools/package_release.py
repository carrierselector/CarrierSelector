#!/usr/bin/env python3
"""Lay out one release folder exactly like the hand-made 6.x releases and zip it.

  macOS:   CarrierSelector v.X - macOS <arch>/  CarrierSelector, CarrierSelector.command,
           README.txt, INSTRUCTION-RU.txt, SHA256.txt
  Linux:   CarrierSelector v.X - Linux x64/     CarrierSelector, CarrierSelector.sh, ...
  Windows: CarrierSelector v.X - Windows x64/   CarrierSelector.cmd, README.txt,
           INSTRUCTION-RU.txt, SHA256.txt, app/ (code + catalog), runtime/ (Python)

Usage:
  package_release.py mac     --label "macOS Apple Silicon" --readme mac_assets/README-Apple-Silicon.txt --binary dist/CarrierSelector --out out
  package_release.py linux   --binary dist/CarrierSelector --out out
  package_release.py windows --runtime build/runtime --out out
Prints the path of the zip it wrote.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_TOOLS = ("__init__.py", "build_selector_ipcc.py", "install_prepared_pair.py",
             "read_phone_carrier_state.py")
JUNK = {"__pycache__", ".DS_Store", "Thumbs.db", "desktop.ini"}


def app_version() -> str:
    text = (ROOT / "carrier_app.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION = "([0-9.]+)"$', text, re.MULTILINE)
    if not match:
        raise SystemExit("APP_VERSION not found in carrier_app.py")
    return match.group(1)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy(src: Path, dst: Path, executable: bool = False) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o755 if executable else 0o644)


def copytree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=lambda _d, names: [n for n in names if n in JUNK or n.startswith("._")])


def write_sums(folder: Path, names: list[str]) -> None:
    lines = [f"{sha256(folder / name)}  {name}" for name in names]
    (folder / "SHA256.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def zip_folder(folder: Path, archive: Path) -> None:
    base = folder.parent
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
        for path in sorted([folder, *folder.rglob("*")]):
            if any(part in JUNK or part.startswith("._") for part in path.relative_to(base).parts):
                continue
            name = path.relative_to(base).as_posix() + ("/" if path.is_dir() else "")
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            mode = 0o755 if path.is_dir() or os.access(path, os.X_OK) or path.suffix in {".sh", ".command"} \
                or path.name == "CarrierSelector" else 0o644
            info.external_attr = ((stat.S_IFDIR if path.is_dir() else stat.S_IFREG) | mode) << 16
            if path.is_dir():
                info.external_attr |= 0x10
                zipped.writestr(info, b"")
            else:
                info.compress_type = zipfile.ZIP_DEFLATED
                with path.open("rb") as handle:
                    zipped.writestr(info, handle.read())
    with zipfile.ZipFile(archive) as zipped:
        bad = zipped.testzip()
        if bad:
            raise SystemExit(f"broken entry in {archive.name}: {bad}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["mac", "linux", "windows"])
    parser.add_argument("--label")
    parser.add_argument("--readme", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    version = app_version()
    label = args.label or {"linux": "Linux x64", "windows": "Windows x64"}.get(args.kind)
    if not label:
        raise SystemExit("--label is required for mac")
    folder = args.out / f"CarrierSelector v.{version} - {label}"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    copy(ROOT / "INSTRUCTION-RU.txt", folder / "INSTRUCTION-RU.txt")

    if args.kind == "mac":
        copy(args.binary, folder / "CarrierSelector", executable=True)
        copy(ROOT / "mac_assets" / "CarrierSelector.command", folder / "CarrierSelector.command", executable=True)
        copy(args.readme, folder / "README.txt")
        write_sums(folder, ["CarrierSelector", "CarrierSelector.command"])
    elif args.kind == "linux":
        copy(args.binary, folder / "CarrierSelector", executable=True)
        copy(ROOT / "linux_assets" / "CarrierSelector.sh", folder / "CarrierSelector.sh", executable=True)
        copy(ROOT / "linux_assets" / "README.txt", folder / "README.txt")
        write_sums(folder, ["CarrierSelector", "CarrierSelector.sh"])
    else:
        copy(ROOT / "windows_assets" / "CarrierSelector.cmd", folder / "CarrierSelector.cmd")
        copy(ROOT / "windows_assets" / "README.txt", folder / "README.txt")
        app = folder / "app"
        copy(ROOT / "carrier_app.py", app / "carrier_app.py")
        copy(ROOT / "windows_assets" / "launcher.py", app / "launcher.py")
        for name in APP_TOOLS:
            copy(ROOT / "tools" / name, app / "tools" / name)
        copytree(ROOT / "embedded_catalog", app / "embedded_catalog")
        copytree(args.runtime, folder / "runtime")
        write_sums(folder, ["CarrierSelector.cmd", "app/carrier_app.py", "app/launcher.py",
                            "app/tools/build_selector_ipcc.py", "app/tools/install_prepared_pair.py",
                            "app/tools/read_phone_carrier_state.py"])

    slug = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-")
    archive = args.out / f"CarrierSelector-v{version}-{slug}.zip"
    zip_folder(folder, archive)
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
