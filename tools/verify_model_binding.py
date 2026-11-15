#!/usr/bin/env python3
"""Check, for every iPhone model in the catalog, which profile the app offers.

For each model (product type + board) and every operator in that model's own
firmware set, the offered profile must be either
  * the bundle from the model's own firmware set (same directory, same bytes), or
  * a curated, tested variant (Telia 68, MTN 72, ...) that also carries exact
    settings for this board.
Anything else is reported as a problem.  Also verifies that no offered profile
lacks settings for the board and that every offered profile passes Apple's
signature check.  Exit code 1 if any problem is found.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import carrier_app as c  # noqa: E402
from tools.build_selector_ipcc import read_bundle, verify_apple_signatures  # noqa: E402

SKIP = {"Default", "DefaultBCAM", "OtherKnown"}


def board_has_settings(directory: Path, short: str) -> bool:
    return any(short.casefold() in {b.casefold() for b in p.stem.removeprefix("overrides_").split("_")}
               for p in directory.glob("overrides_*.plist"))


def main() -> int:
    catalog = c.load_catalog()
    base = c.resource_root() / "embedded_catalog"
    problems: list[str] = []
    signature_checked: set[str] = set()
    print(f"{'Модель':12} {'Плата':8} {'Свой набор':14} {'опер.':>5} {'своя прошивка':>13} {'проверенный':>11} "
          f"{'прочие':>6} {'не под плату в прошивке':>23}")
    for family in catalog["families"]:
        own_root = family["catalog_root"]
        own_dir = base / own_root
        for product, hardware in zip(family["products"], family["hardwares"]):
            overview = {"product": product, "hardware": hardware}
            matched = c.matching_family(catalog, overview)
            if matched is not family:
                problems.append(f"{product}/{hardware}: определяется не как «{family['name']}»")
                continue
            short = c.board_short(hardware)
            offered = {row["identifier"]: row for row in c.offered_profiles(c.profiles_for(catalog, family, overview))}
            stats = Counter()
            for bundle in sorted(own_dir.glob("*.bundle")):
                if bundle.stem in SKIP:
                    continue
                score, _ = c.profile_compatibility(bundle, overview)
                identifier = __import__("plistlib").loads((bundle / "Info.plist").read_bytes())["CFBundleIdentifier"]
                if score == c.UNSUITABLE_SCORE:
                    stats["unsuitable_in_firmware"] += 1  # Apple shipped no settings for this board here
                    continue
                row = offered.get(identifier)
                if row is None:
                    problems.append(f"{product}: {bundle.name} есть в прошивке, но не предлагается")
                    continue
                chosen = c.bundle_path(str(row["path"]))
                if chosen == bundle.resolve():
                    stats["own"] += 1
                elif not str(row.get("key", "")).startswith("catalog-"):
                    if row["compatibility_score"] == 400 and not board_has_settings(chosen, short):
                        problems.append(f"{product}: проверенный {row['label']} без настроек платы {short}")
                    stats["curated"] += 1
                else:
                    stats["other"] += 1
                    problems.append(f"{product}: {identifier} взят не из своей прошивки, а из {row['path']}")
            for row in offered.values():
                if not c.is_suitable(row):
                    problems.append(f"{product}: предлагается неподходящий {row['label']}")
                if row["compatibility_score"] == 400 and not board_has_settings(c.bundle_path(str(row["path"])), short) \
                        and not c._supported_devices(c.bundle_path(str(row["path"]))):
                    problems.append(f"{product}: {row['label']} помечен «точные настройки», но блока {short} нет")
                if row["path"] not in signature_checked:
                    try:
                        files, _ = read_bundle(c.bundle_path(str(row["path"])))
                        verify_apple_signatures(files)
                    except Exception as exc:
                        problems.append(f"{product}: {row['label']}: подпись Apple не прошла: {exc}")
                    signature_checked.add(row["path"])
            print(f"{product:12} {short:8} {own_root:14} {len(offered):5} {stats['own']:13} {stats['curated']:11} "
                  f"{stats['other']:6} {stats['unsuitable_in_firmware']:23}")
    print(f"\nПроверено подписей Apple у предлагаемых профилей: {len(signature_checked)}")
    print(f"ПРОБЛЕМ: {len(problems)}")
    for line in problems[:40]:
        print("   ", line)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
