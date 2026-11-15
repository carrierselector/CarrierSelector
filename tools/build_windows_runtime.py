#!/usr/bin/env python3
"""Build the Windows runtime folder: python.org embeddable Python + dependencies.

Run on Windows x64 with the same Python minor version that the runtime will use
(the embeddable package is taken for exactly the running interpreter's version,
so compiled wheels match).  Result: OUTPUT/python.exe, python3XY._pth,
Lib/site-packages/... — the layout of the hand-made 6.x Windows releases.

Usage: python tools/build_windows_runtime.py OUTPUT_DIR
"""

from __future__ import annotations

import io
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit(__doc__)
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise SystemExit("run this on Windows x64")
    out = Path(argv[0]).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    version = platform.python_version()
    tag = f"python{sys.version_info.major}{sys.version_info.minor}"
    url = f"https://www.python.org/ftp/python/{version}/python-{version}-embed-amd64.zip"
    print(f"Скачиваю {url}")
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        zipped.extractall(out)

    # Same search path as the 6.x releases: the program lives in ..\app.
    (out / f"{tag}._pth").write_text(
        "\n".join([
            f"{tag}.zip", ".", r"..\app", r"Lib\site-packages",
            r"Lib\site-packages\win32", r"Lib\site-packages\win32\lib",
            r"Lib\site-packages\pythonwin", r"Lib\site-packages\pywin32_system32",
            "import site", "",
        ]),
        encoding="utf-8",
    )
    site_packages = out / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-compile", "--disable-pip-version-check",
         "--target", str(site_packages), "-r", str(ROOT / "python_assets" / "requirements.txt")],
        check=True,
    )
    for cache in site_packages.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    print(f"Готово: {out} (Python {version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
