#!/usr/bin/env python3
"""Merge per-firmware Apple carrier bundle sets into one multi-board set.

Build-time tool (not used by the app at runtime).  Apple ships each carrier
bundle per firmware with a signed common part plus signed per-board override
groups.  When the common part of the same bundle/version is byte-identical in
several firmware sets, the override groups can live side by side in one bundle
directory -- exactly the layout of Apple's own multi-board OTA IPCC files.

Nothing is edited: files are copied byte-for-byte.  A bundle is merged only if
every file present in more than one source is identical; otherwise the variants
are kept separate (written to per-source overflow roots).  Every resulting
bundle is re-verified with Apple's production P-384 key.

Usage: merge_carrier_sets.py OUTPUT_DIR LABEL=SET_DIR [LABEL=SET_DIR ...]
"""

from __future__ import annotations

import json
import plistlib
import shutil
import sys
from collections import defaultdict
from pathlib import Path

if __package__:
    from tools.build_selector_ipcc import is_os_junk, read_bundle, verify_apple_signatures
else:  # run as a script from the project root
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools.build_selector_ipcc import is_os_junk, read_bundle, verify_apple_signatures

SKIP = {"Default", "DefaultBCAM", "OtherKnown"}


def bundle_files(directory: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        if is_os_junk(relative) or path.is_symlink() or not path.is_file():
            continue
        files[relative] = path.read_bytes()
    return files


def main(argv: list[str]) -> int:
    output = Path(argv[1])
    sources = [(item.split("=", 1)[0], Path(item.split("=", 1)[1])) for item in argv[2:]]
    if output.exists():
        raise SystemExit(f"refusing to overwrite {output}")
    groups: dict[tuple[str, str], list[tuple[str, Path]]] = defaultdict(list)
    for label, root in sources:
        for bundle in sorted(root.glob("*.bundle")):
            if bundle.stem in SKIP or bundle.is_symlink() or not bundle.is_dir():
                continue
            version = str(plistlib.loads((bundle / "Info.plist").read_bytes()).get("CFBundleVersion"))
            groups[(bundle.name, version)].append((label, bundle))

    merged_root = output / "merged"
    shared_root = output / "shared"
    report = {"merged": 0, "single": 0, "shared_split": 0, "conflicts": [], "failed": []}
    # A firmware that ships a bundle WITHOUT any board overrides (a shared
    # profile) must keep getting exactly that layout.  Adding other boards'
    # override groups to it would change how iOS treats it on that model, so
    # shared and board-specific variants are never mixed.
    split: list[tuple[tuple[str, str], list[tuple[str, Path]], Path]] = []
    for key, members in sorted(groups.items()):
        boarded = [(l, b) for l, b in members if list(b.glob("overrides_*.plist"))]
        shared = [(l, b) for l, b in members if not list(b.glob("overrides_*.plist"))]
        if boarded and shared:
            report["shared_split"] += 1
            split.append((key, boarded, merged_root))
            split.append((key, shared, shared_root))
        else:
            split.append((key, members, merged_root))
    for (name, version), members, destination in split:
        combined: dict[str, bytes] = {}
        conflict = None
        for label, bundle in members:
            for relative, data in bundle_files(bundle).items():
                if relative in combined and combined[relative] != data:
                    conflict = f"{relative} differs ({label})"
                    break
                combined.setdefault(relative, data)
            if conflict:
                break
        if conflict:
            # Keep every variant untouched in its own overflow root.
            report["conflicts"].append(f"{name} {version}: {conflict}")
            for label, bundle in members:
                shutil.copytree(bundle, output / f"only-{label}" / name)
            continue
        target = destination / name
        if target.exists():  # same directory name with another version: keep apart
            target = output / f"v{version}" / name
        for relative, data in combined.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        try:
            files, _ = read_bundle(target)
            verify_apple_signatures(files)
        except Exception as exc:  # never ship a bundle that fails Apple's signature
            report["failed"].append(f"{name} {version}: {exc}")
            shutil.rmtree(target)
            for label, bundle in members:
                shutil.copytree(bundle, output / f"only-{label}" / name, dirs_exist_ok=True)
            continue
        report["merged" if len(members) > 1 else "single"] += 1
    (output / "merge-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"разделено на вариант с блоками и общий: {report['shared_split']}")
    print(f"объединено из нескольких прошивок: {report['merged']}, только в одной: {report['single']}, "
          f"расхождений общей части: {len(report['conflicts'])}, не прошли подпись: {len(report['failed'])}")
    for line in (report["conflicts"] + report["failed"])[:10]:
        print("   ", line)
    return 0 if not report["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
