#!/usr/bin/env python3
"""Download the embedded catalog of Apple carrier bundles into ./embedded_catalog.

The catalog (Apple-signed carrier bundles from the iOS 27.2.1 firmware of every
supported iPhone model) is not stored in git: it is attached to the GitHub
release CATALOG_TAG.  The archive is verified by SHA-256 before extraction and
every entry must stay inside embedded_catalog/.

Usage:  python tools/fetch_catalog.py            (skips if already present)
        python tools/fetch_catalog.py --force    (re-download and replace)
"""

from __future__ import annotations

import hashlib
import shutil
import ssl
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

CATALOG_TAG = "catalog-6.5"
CATALOG_FILE = "embedded_catalog-6.5.zip"
CATALOG_SHA256 = "ddf93c9c65d792265a990add1f2ab2a3bdb4787082599763fff7a72902cdfcf7"
CATALOG_URL = (
    "https://github.com/carrierselector/CarrierSelector/releases/download/"
    f"{CATALOG_TAG}/{CATALOG_FILE}"
)

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "embedded_catalog"


def tls_context() -> ssl.SSLContext:
    # python.org builds for macOS ship without root certificates; certifi comes
    # with the dependencies (requests/pymobiledevice3), so prefer it.
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def download(url: str, destination: Path) -> str:
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=120, context=tls_context()) as response, \
            destination.open("wb") as handle:
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            handle.write(chunk)
    return digest.hexdigest()


def safe_extract(archive: Path, root: Path) -> None:
    with zipfile.ZipFile(archive) as zipped:
        for info in zipped.infolist():
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "embedded_catalog":
                raise SystemExit(f"unexpected entry in the catalog archive: {info.filename}")
        zipped.extractall(root)


def main(argv: list[str]) -> int:
    force = "--force" in argv
    if (TARGET / "catalog.json").is_file() and not force:
        print(f"Каталог уже на месте: {TARGET}")
        return 0
    with tempfile.TemporaryDirectory() as temp:
        archive = Path(temp) / CATALOG_FILE
        print(f"Скачиваю {CATALOG_URL}")
        actual = download(CATALOG_URL, archive)
        if actual != CATALOG_SHA256:
            raise SystemExit(f"SHA-256 не совпадает: {actual}, ожидалось {CATALOG_SHA256}")
        staging = Path(temp) / "extract"
        safe_extract(archive, staging)
        if TARGET.exists():
            shutil.rmtree(TARGET)
        shutil.move(str(staging / "embedded_catalog"), TARGET)
    print(f"Каталог распакован: {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
