#!/usr/bin/env python3
"""Build reversible SIM-specific IPCC packages without editing signed files.

This tool does not connect to a phone and does not install anything.  It only
repackages two already-extracted, Apple-signed carrier bundle directories under
the same SIM-specific outer directory name used by CommCenter's selector.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import plistlib
import re
import stat
import struct
import sys
import zipfile
from pathlib import Path, PurePosixPath


IMSI_RE = re.compile(r"[0-9]{14,16}")
ICCID_RE = re.compile(r"[0-9]{18,22}")
GID1_RE = re.compile(r"(?:[0-9A-F]{2}){1,8}")
APPLE_PRODUCTION_P384 = bytes.fromhex(
    "04de7893741a21ef21e5fe21fc5838034fed5ba8666e83bc29a29501da497727f32"
    "ed7796dd138d3b8550e4eef9cf53490059e615cd260ed0d53f37c781ebd92efdd"
    "7c6779afa27cf617269d603a2eb544a61c45dfcd337643ebb4f28b548eaa97"
)


class BuildError(ValueError):
    pass



def make_private_dir(path: Path, *, exist_ok: bool = True) -> None:
    """Create a directory that only this user can read.

    POSIX: mode 0o700.  Windows: plain mkdir, the folder inherits the per-user
    ACL of %LOCALAPPDATA%.  Since Python 3.13, mode=0o700 on Windows writes a
    protected ACL that grants access to SYSTEM, Administrators and the folder's
    *owner*; a folder created once from an elevated window is owned by the
    Administrators group and then refuses the same user in a normal window
    (WinError 5).
    """
    if os.name == "nt":
        path.mkdir(parents=True, exist_ok=exist_ok)
    else:
        path.mkdir(parents=True, exist_ok=exist_ok, mode=0o700)

def normalized_selector(imsi: str, iccid: str, gid1: str | None = None) -> str:
    imsi = imsi.strip()
    iccid = iccid.strip()
    if not IMSI_RE.fullmatch(imsi):
        raise BuildError("IMSI must contain 14-16 decimal digits")
    if not ICCID_RE.fullmatch(iccid):
        raise BuildError("ICCID must contain 18-22 decimal digits")
    if gid1 is None or not gid1.strip():
        # CommCenter's observed SupportedSIMs grammar includes MCCMNC_ID-ICCID
        # selectors without either GID field.  Keeping full IMSI and ICCID here
        # makes the outer selector SIM-specific without inventing a missing GID.
        return f"{imsi}_ID-{iccid}_ID-.bundle"
    gid1 = gid1.strip().removeprefix("0x").removeprefix("0X").upper()
    if not GID1_RE.fullmatch(gid1):
        raise BuildError("GID1 must contain 1-8 complete hexadecimal bytes")
    return f"{imsi}_GID1-{gid1}_ID-{iccid}_ID-.bundle"


def is_os_junk(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    return any(
        part.startswith("._") or part in {".DS_Store", "__MACOSX", "Thumbs.db", "desktop.ini"}
        for part in parts
    )


def read_bundle(directory: Path) -> tuple[dict[str, bytes], dict[str, object]]:
    if not directory.is_dir():
        raise BuildError(f"carrier bundle directory does not exist: {directory}")
    files: dict[str, bytes] = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        if is_os_junk(relative):
            # AppleDouble "._*" files and .DS_Store appear when the folder is
            # copied through macOS or a FAT/exFAT drive.  They are not part of
            # the Apple-signed bundle and must not reach the IPCC.
            continue
        if path.is_symlink():
            raise BuildError(f"symbolic links are not accepted: {path}")
        if not path.is_file():
            continue
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts:
            raise BuildError(f"unsafe bundle path: {relative}")
        files[relative] = path.read_bytes()

    required = {"Info.plist", "carrier.plist", "signatures/common.plist"}
    missing = required - files.keys()
    if missing:
        raise BuildError(f"bundle is missing required signed files: {sorted(missing)}")
    try:
        info = plistlib.loads(files["Info.plist"])
    except Exception as exc:
        raise BuildError("Info.plist is not a valid plist") from exc
    if not isinstance(info, dict):
        raise BuildError("Info.plist root must be a dictionary")
    identifier = info.get("CFBundleIdentifier")
    version = info.get("CFBundleVersion")
    if not isinstance(identifier, str) or not identifier.startswith("com.apple."):
        raise BuildError("unexpected carrier CFBundleIdentifier")
    if not isinstance(version, str) or not version:
        raise BuildError("carrier CFBundleVersion is missing")

    for relative in files:
        if not relative.startswith("overrides_") or not relative.endswith(".plist"):
            continue
        signature = "signatures/" + relative
        if signature not in files:
            raise BuildError(f"missing Apple signature paired with {relative}")
    return files, info


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def signature_group(name: str) -> str | None:
    path = PurePosixPath(name)
    if path.parts[0] == "signatures" or path.name == "version.plist":
        return None
    if (len(path.parts) == 2 and path.parts[0].endswith(".lproj")
            and path.name in {"CBMessage.strings", "carrier.strings", "locversion.plist"}):
        # Genuine Apple OTA carrier bundles can include localized display and
        # consent resources outside CBSignature3.  Preserve them byte-for-byte,
        # but do not add them to a signed digest group that Apple did not sign.
        return None
    if len(path.parts) != 1:
        raise BuildError(f"unmodeled nested carrier file: {name}")
    if path.name.startswith("overrides_"):
        match = re.fullmatch(
            r"overrides_([A-Za-z0-9]+(?:_[A-Za-z0-9]+)*)\.(plist|der\.(?:pri|tri)|pri)",
            path.name,
        )
        if not match:
            raise BuildError(f"unmodeled override file: {name}")
        return match.group(1)
    signed_common = {
        "CarrierCA.crt", "Info.plist", "carrier.plist", "carrier.pri",
        "device+carrier.plist", "supported_devices.plist",
        "profile.mobileconfig", "ERI.plist", "carrier.ims", "carrier.dmu",
        "hexvalues.plist", "tree.xml", "carrier.prl", "bundle.metadata",
    }
    if path.name in signed_common:
        return "common"
    # These localized display resources are present in genuine Apple bundles
    # but are not part of CBSignature3's common digest.
    if path.name == "carrier.loctable":
        return None
    raise BuildError(f"unmodeled carrier file: {name}")


def canonical_signed_file(name: str, data: bytes) -> bytes:
    if name.endswith(".plist"):
        try:
            return plistlib.dumps(
                plistlib.loads(data), fmt=plistlib.FMT_XML, sort_keys=True
            )
        except Exception as exc:
            raise BuildError(f"signed plist is invalid: {name}") from exc
    return data


def verify_apple_signatures(files: dict[str, bytes]) -> dict[str, str]:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils
    except ImportError as exc:
        raise BuildError(
            "cryptography is required for Apple P-384 signature verification"
        ) from exc

    actual = {name for name in files if name.startswith("signatures/")}
    signed_override_groups = {
        match.group(1)
        for name in actual
        if (match := re.fullmatch(r"signatures/overrides_(.+)\.plist", name))
    }
    grouped: dict[str, list[str]] = {}
    for name in sorted(files):
        group = signature_group(name)
        if group not in (None, "common") and group not in signed_override_groups:
            suffix_matches = [
                signed for signed in signed_override_groups
                if group.endswith("_" + signed)
            ]
            if len(suffix_matches) == 1:
                group = suffix_matches[0]
        # Some Apple bundles contain an auxiliary override binary without a
        # matching override plist/signature (for example T-Mobile MVNO data).
        # A signed override plist is still mandatory by read_bundle().
        if group not in (None, "common") and group not in signed_override_groups:
            if name.endswith(".plist"):
                raise BuildError(f"missing Apple signature paired with {name}")
            continue
        if group is not None:
            grouped.setdefault(group, []).append(name)
    if "common" not in grouped:
        raise BuildError("carrier bundle has no common signature group")

    expected = {
        "signatures/"
        + ("common" if group == "common" else "overrides_" + group)
        + ".plist"
        for group in grouped
    }
    if expected != actual:
        raise BuildError("signature files do not correspond exactly to payload groups")

    key = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP384R1(), APPLE_PRODUCTION_P384
    )
    digests: dict[str, bytes] = {}
    result: dict[str, str] = {}
    for group in ["common"] + sorted(set(grouped) - {"common"}):
        digest = hashlib.sha384()
        if group != "common":
            digest.update(digests["common"])
        for name in grouped[group]:
            digest.update(canonical_signed_file(name, files[name]))
        for name in grouped[group]:
            basename = PurePosixPath(name).name.encode("utf-8")
            digest.update(struct.pack(">I", len(basename)))
            digest.update(basename)
        digests[group] = digest.digest()
        signature_name = (
            "signatures/"
            + ("common" if group == "common" else "overrides_" + group)
            + ".plist"
        )
        try:
            document = plistlib.loads(files[signature_name])
            signature = document["CBSignature3"]
            if not isinstance(signature, bytes):
                raise TypeError
            key.verify(
                signature,
                digests[group],
                ec.ECDSA(utils.Prehashed(hashes.SHA384())),
            )
        except InvalidSignature as exc:
            raise BuildError(f"invalid Apple signature group: {group}") from exc
        except (KeyError, TypeError, plistlib.InvalidFileException) as exc:
            raise BuildError(f"unsupported Apple signature document: {signature_name}") from exc
        result[group] = digest.hexdigest()
    return result


def build_one(
    directory: Path,
    output: Path,
    selector: str,
    expected_identifier: str,
) -> dict[str, object]:
    files, info = read_bundle(directory)
    if info["CFBundleIdentifier"] != expected_identifier:
        raise BuildError(
            f"expected {expected_identifier}, got {info['CFBundleIdentifier']}"
        )
    signature_groups = verify_apple_signatures(files)

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise BuildError(f"refusing to overwrite existing output: {output}")
    root = f"Payload/{selector}/"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(root, b"")
        archive.writestr(root + "signatures/", b"")
        for relative, data in sorted(files.items()):
            archive.writestr(root + relative, data)
    os.chmod(output, stat.S_IRUSR | stat.S_IWUSR)

    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        if not names or names[0] != root:
            raise BuildError("IPCC outer directory is not the first ZIP entry")
        recovered = {
            name[len(root) :]: archive.read(name)
            for name in names
            if name.startswith(root) and not name.endswith("/")
        }
    if recovered != files:
        output.unlink(missing_ok=True)
        raise BuildError("IPCC packaging changed a signed file")
    if verify_apple_signatures(recovered) != signature_groups:
        output.unlink(missing_ok=True)
        raise BuildError("Apple signature result changed after IPCC packaging")

    payload = output.read_bytes()
    return {
        "file": output.name,
        "sha256": sha256(payload),
        "size": len(payload),
        "bundle_identifier": info["CFBundleIdentifier"],
        "bundle_version": info["CFBundleVersion"],
        "signed_file_count": len(files),
        "verified_signature_groups": signature_groups,
    }


RESET_SELECTOR_RE = re.compile(r"[0-9]{14,16}_(?:GID1-[0-9A-F]{2,16}_)?ID-[0-9]{18,22}_ID-\.bundle")


def build_reset_payload(directory: Path, selector: str, expected_identifier: str) -> bytes:
    """IPCC that makes CommCenter drop this SIM's user carrier package.

    Verified on iPhone 15 Pro / iOS 27.2.1: for a package whose carrier.plist
    lists no SupportedSIMs, CommCenter first removes the bundle already bound
    to the selector, then finds "No symlinks available" for the new one,
    deletes it as well and answers kInstallResult=false.  After one reboot the
    SIM uses the carrier profile built into iOS (/System/Library/Carrier
    Bundles), exactly as before any package was installed.  Sending it to a
    SIM without a user package changes nothing.

    The donor must be a genuine Apple bundle (its signature is verified first);
    only SupportedSIMs is emptied, which necessarily invalidates the signature.
    """
    if not RESET_SELECTOR_RE.fullmatch(selector):
        raise BuildError("invalid SIM selector for the reset package")
    files, info = read_bundle(directory)
    if info["CFBundleIdentifier"] != expected_identifier:
        raise BuildError(f"expected {expected_identifier}, got {info['CFBundleIdentifier']}")
    verify_apple_signatures(files)
    original = files["carrier.plist"]
    try:
        carrier = plistlib.loads(original)
    except Exception as exc:
        raise BuildError("carrier.plist is not a valid plist") from exc
    if not isinstance(carrier, dict):
        raise BuildError("carrier.plist root must be a dictionary")
    carrier["SupportedSIMs"] = []
    fmt = plistlib.FMT_BINARY if original.startswith(b"bplist") else plistlib.FMT_XML
    changed = dict(files)
    changed["carrier.plist"] = plistlib.dumps(carrier, fmt=fmt)

    root = f"Payload/{selector}/"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(root, b"")
        archive.writestr(root + "signatures/", b"")
        for relative, data in sorted(changed.items()):
            archive.writestr(root + relative, data)
    payload = buffer.getvalue()

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        if not names or names[0] != root:
            raise BuildError("reset IPCC outer directory is not the first ZIP entry")
        recovered = {
            name[len(root):]: archive.read(name)
            for name in names if name.startswith(root) and not name.endswith("/")
        }
    if recovered != changed:
        raise BuildError("reset IPCC packaging changed a file")
    if plistlib.loads(recovered["carrier.plist"]).get("SupportedSIMs") != []:
        raise BuildError("reset IPCC still lists SupportedSIMs")
    if any(recovered[name] != files[name] for name in files if name != "carrier.plist"):
        raise BuildError("reset IPCC changed a file other than carrier.plist")
    return payload


def build_pair(args: argparse.Namespace) -> int:
    if args.state_json:
        try:
            state = json.loads(args.state_json.read_text(encoding="utf-8"))
            imsi = state["imsi"]
            iccid = state["iccid"]
            reported_gid1 = state.get("gid1") or None
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise BuildError("private state JSON is missing valid SIM identifiers") from exc
        if not isinstance(imsi, str) or not isinstance(iccid, str):
            raise BuildError("private state JSON contains unsupported SIM identifiers")
        gid1 = args.gid1 if args.gid1 is not None else reported_gid1
    else:
        if args.imsi is None or args.iccid is None:
            raise BuildError("provide --state-json or both --imsi and --iccid")
        imsi, iccid, gid1 = args.imsi, args.iccid, args.gid1
    selector = normalized_selector(imsi, iccid, gid1)
    output = args.output.resolve()
    if output.exists():
        raise BuildError(f"refusing to reuse existing output directory: {output}")
    make_private_dir(output, exist_ok=False)

    try:
        donor = args.donor or args.vodafone
        if donor is None:
            raise BuildError("provide --donor (or the legacy --vodafone option)")
        donor_identifier = args.donor_identifier
        donor_slug = args.donor_slug or donor_identifier.removeprefix("com.apple.").lower().replace("_", "-")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", donor_slug):
            raise BuildError("donor slug must contain lowercase letters, digits, and single hyphens")
        forward = build_one(
            donor.resolve(),
            output / f"{donor_slug}-forward.ipcc",
            selector,
            donor_identifier,
        )
        forward["role"] = "forward"
        forward["legacy"] = bool(args.legacy_donor)
        revert = build_one(
            args.mts.resolve(),
            output / "mts-ru-revert.ipcc",
            selector,
            "com.apple.MTS_ru",
        )
        revert["role"] = "revert"
        revert["legacy"] = False
        exact_values = (args.target_product, args.target_hardware)
        family_values = (args.target_products, args.target_hardwares)
        if any(exact_values) and any(family_values):
            raise BuildError("use either exact target fields or family target lists")
        if any(exact_values) and not all(exact_values):
            raise BuildError("exact target product and hardware must be supplied together")
        if any(family_values) and not all(family_values):
            raise BuildError("target product and hardware lists must be supplied together")
        if (any(exact_values) or any(family_values)) and not (args.target_version and args.target_build):
            raise BuildError("target version and build are required with a target model")
        manifest = {
            "format": 1,
            "method": "paired-ipcc-outer-selector",
            "selector_fields": ["IMSI", *( ["GID1"] if gid1 else []), "ICCID"],
            "selector_sha256": sha256(selector.encode("utf-8")),
            "private_identifiers_recorded": False,
            "packages": [forward, revert],
        }
        if all(exact_values):
            manifest["target"] = {"product": args.target_product, "hardware": args.target_hardware,
                                  "version": args.target_version, "build": args.target_build}
        elif all(family_values):
            products = [value.strip() for value in args.target_products.split(",") if value.strip()]
            hardwares = [value.strip() for value in args.target_hardwares.split(",") if value.strip()]
            if not products or not hardwares:
                raise BuildError("target family lists cannot be empty")
            manifest["target"] = {"products": products, "hardwares": hardwares,
                                  "version": args.target_version, "build": args.target_build}
        manifest_path = output / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(manifest_path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        for child in output.iterdir():
            if child.is_file():
                child.unlink()
        output.rmdir()
        raise

    print(f"Built verified forward/revert pair in {output}")
    print("The manifest contains only a hash of the private selector.")
    print("No phone connection or installation was attempted.")
    return 0


def build_selection(args: argparse.Namespace) -> int:
    """Build one SIM-specific carrier package for the interactive selector."""
    try:
        state = getattr(args, "state", None)
        if state is None:
            state = json.loads(args.state_json.read_text(encoding="utf-8"))
        imsi = state["imsi"]
        iccid = state["iccid"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise BuildError("private state JSON is missing valid SIM identifiers") from exc
    if not isinstance(imsi, str) or not isinstance(iccid, str):
        raise BuildError("private state JSON contains unsupported SIM identifiers")
    # Do not depend on GID: full IMSI + full ICCID makes the package line-specific
    # and survives carriers that omit GID from lockdown metadata.
    selector = normalized_selector(imsi, iccid)
    output = args.output.resolve()
    if output.exists():
        raise BuildError(f"refusing to reuse existing output directory: {output}")
    make_private_dir(output, exist_ok=False)
    try:
        package = build_one(
            args.donor.resolve(),
            output / f"{args.donor_slug}.ipcc",
            selector,
            args.donor_identifier,
        )
        package["role"] = "forward"
        package["legacy"] = bool(args.legacy_donor)
        manifest = {
            "format": 2,
            "method": "selector-ipcc",
            "selector_fields": ["IMSI", "ICCID"],
            "selector_sha256": sha256(selector.encode("utf-8")),
            "private_identifiers_recorded": False,
            "packages": [package],
            # Informational only. Model and iOS are deliberately not install gates.
            "prepared_from": {
                key: str(state.get(key, ""))
                for key in ("product", "hardware", "version", "build", "slot")
            },
        }
        manifest_path = output / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(manifest_path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        for child in output.iterdir():
            if child.is_file():
                child.unlink()
        output.rmdir()
        raise
    print("Профиль подготовлен, подпись Apple проверена.")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    pair = sub.add_parser("build-pair", help="build Vodafone forward and MTS revert IPCCs")
    donor_group = pair.add_mutually_exclusive_group(required=True)
    donor_group.add_argument("--donor", type=Path, help="Apple-signed donor bundle directory")
    donor_group.add_argument("--vodafone", type=Path, help="legacy alias for --donor")
    pair.add_argument("--donor-identifier", default="com.apple.Vodafone_ro")
    pair.add_argument("--donor-slug")
    pair.add_argument("--legacy-donor", action="store_true")
    pair.add_argument("--mts", type=Path, required=True, help="MTS_ru.bundle directory")
    pair.add_argument("--state-json", type=Path, help="owner-readable JSON from read_phone_carrier_state.py")
    pair.add_argument("--imsi", help="full IMSI (prefer --state-json to keep it out of process arguments)")
    pair.add_argument("--iccid", help="full ICCID (prefer --state-json to keep it out of process arguments)")
    pair.add_argument("--gid1", help="confirmed full GID1; omit when the SIM exposes none")
    pair.add_argument("--target-product")
    pair.add_argument("--target-hardware")
    pair.add_argument("--target-products", help="comma-separated ProductType family")
    pair.add_argument("--target-hardwares", help="comma-separated HardwareModel family")
    pair.add_argument("--target-version")
    pair.add_argument("--target-build")
    pair.add_argument("--output", type=Path, required=True)
    pair.set_defaults(handler=build_pair)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.handler(args)
    except (BuildError, OSError, zipfile.BadZipFile) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
