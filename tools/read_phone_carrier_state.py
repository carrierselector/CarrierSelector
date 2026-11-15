#!/usr/bin/env python3
"""Read paired iPhone carrier metadata without changing the phone.

Full SIM identifiers are written only to an owner-readable local JSON file.
The terminal output contains no IMSI, ICCID, UDID, or selector value.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import stat
import sys
from pathlib import Path


IMSI_KEY = "InternationalMobileSubscriberIdentity"
ICCID_KEY = "IntegratedCircuitCardIdentity"


def normalize_iccid(value: object) -> str:
    """Normalize the optional SIM-file padding nibble without inventing digits."""
    text = str(value or "").strip()
    if text[-1:].casefold() == "f" and text[:-1].isdigit():
        text = text[:-1]
    return text


def normalize_gid1(value: object) -> str:
    """Keep a complete hexadecimal GID1 only for diagnostics.

    The interactive selector deliberately uses IMSI + ICCID and does not use
    GID1.  Some iOS/modem combinations expose a truncated nibble, an integer,
    or another private representation; that optional field must not block SIM
    discovery or installation.
    """
    if isinstance(value, bytes):
        value = value.hex()
    text = str(value or "").strip().removeprefix("0x").removeprefix("0X").upper()
    if text and not re.fullmatch(r"(?:[0-9A-F]{2}){1,8}", text):
        return ""
    return text


def imsi_matches_plmn(imsi: object, mcc: object, mnc: object) -> bool:
    """True when the IMSI starts with MCC+MNC (MNC may be reported without zero padding)."""
    text = str(imsi or "").strip()
    mcc_text, mnc_text = str(mcc or "").strip(), str(mnc or "").strip()
    if not (text.isdigit() and mcc_text.isdigit() and mnc_text.isdigit()):
        return False
    return any(text.startswith(mcc_text + variant)
               for variant in {mnc_text, mnc_text.zfill(2), mnc_text.zfill(3)})


async def read_states() -> dict[str, object]:
    """Return device metadata and every cellular line reported over lockdown."""
    from pymobiledevice3.exceptions import MissingValueError, PyMobileDevice3Exception
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.usbmux import list_devices

    devices = await asyncio.wait_for(list_devices(), 12)
    usb = [device for device in devices if device.connection_type == "USB"]
    if not usb:
        raise RuntimeError(
            "iPhone не найден по USB: подключите кабель, разблокируйте iPhone "
            "и подтвердите «Доверять этому компьютеру»"
        )
    if len(usb) > 1:
        raise RuntimeError("подключено несколько iPhone по USB; оставьте один")
    device = await asyncio.wait_for(
        create_using_usbmux(
            serial=usb[0].serial,
            connection_type="USB",
            autopair=True,
            pair_timeout=25,
        ),
        35,
    )
    try:
        values = await asyncio.wait_for(device.get_value(), 10)
        try:
            rows = await asyncio.wait_for(device.get_value(key="CarrierBundleInfoArray"), 10)
        except MissingValueError:
            rows = []
        rows = rows or []
        if not isinstance(values, dict) or not isinstance(rows, list):
            raise RuntimeError("unsupported paired device response")
        if not all(isinstance(row, dict) for row in rows):
            raise RuntimeError("unsupported carrier row response")

        common = {
            "product": str(values.get("ProductType", "")),
            "hardware": str(values.get("HardwareModel", "")),
            "version": str(values.get("ProductVersion", "")),
            "build": str(values.get("BuildVersion", "")),
            # Kept only in memory so a reconnect cannot silently switch to a
            # different attached phone.  safe_state() never writes it to a
            # receipt and the command-line output never prints it.
            "udid": str(usb[0].serial),
        }

        async def optional_value(keys: list[str]) -> str:
            for key in keys:
                current = values.get(key)
                if current:
                    return str(current)
                try:
                    current = await asyncio.wait_for(device.get_value(key=key), 5)
                except (MissingValueError, PyMobileDevice3Exception, asyncio.TimeoutError):
                    continue
                if current:
                    return str(current)
            return ""

        async def identity_source(suffix: str) -> tuple[str, str]:
            imsi_value = await optional_value([f"{IMSI_KEY}{suffix}"])
            iccid_value = normalize_iccid(await optional_value([
                f"{ICCID_KEY}{suffix}", f"IntegratedCircuitCardIdentifier{suffix}",
            ]))
            return imsi_value, iccid_value

        sources: dict[str, tuple[str, str]] = {}

        async def source(name: str) -> tuple[str, str]:
            if name not in sources:
                sources[name] = await identity_source("" if name == "root" else "2")
            return sources[name]

        lines = []
        for index, row in enumerate(rows):
            slot = str(row.get("Slot", ""))
            # Dual-SIM lockdown responses sometimes omit identifiers from the
            # CarrierBundleInfoArray row but still expose per-slot root keys:
            # unsuffixed ("root") and "2"-suffixed.  For old responses without
            # Slot, row order is the only mapping available.
            second = slot == "kTwo" or (not slot and index == 1)
            mcc = str(row.get("MCC", "") or "").strip()
            mnc = str(row.get("MNC", "") or "").strip()
            fallback = values if len(rows) == 1 and not second else {}
            imsi = str(row.get(IMSI_KEY) or "")
            iccid = normalize_iccid(row.get(ICCID_KEY) or row.get("IntegratedCircuitCardIdentifier"))
            identity_from = "row"
            if not (imsi and iccid):
                if len(rows) == 1 and second:
                    # One active line reported as kTwo (typical for eSIM-only
                    # US iPhones).  Lockdown can expose two identifier sets
                    # here: on some phones the root keys belong to this line
                    # (v5.4 behaviour, confirmed on iPhone 18 Pro Max US), on
                    # others the "2" keys do (v5.5 behaviour).  Pick the set
                    # whose IMSI belongs to this row's MCC/MNC; if both or
                    # neither match, keep the v5.4 order that worked.
                    order = ["root", "second"]
                elif second:
                    order = ["second"]
                else:
                    order = ["root"]
                candidates = []
                for name in order:
                    cand_imsi, cand_iccid = await source(name)
                    if imsi and cand_imsi and cand_imsi != imsi:
                        continue  # the row's own IMSI decides which set is ours
                    if cand_imsi or cand_iccid:
                        candidates.append((name, cand_imsi, cand_iccid))
                matching = [item for item in candidates if imsi_matches_plmn(item[1], mcc, mnc)]
                chosen = (matching or candidates or [None])[0]
                if chosen is not None:
                    identity_from = chosen[0]
                    imsi = imsi or chosen[1]
                    iccid = iccid or chosen[2]
            gid1 = row.get("GID1") or fallback.get("GID1")
            if not gid1 and len(rows) == 1:
                try:
                    gid1 = await asyncio.wait_for(device.get_value(key="GID1"), 5)
                except (PyMobileDevice3Exception, asyncio.TimeoutError):
                    gid1 = ""
            lines.append({
                **common,
                "slot": slot,
                "bundle_identifier": str(row.get("CFBundleIdentifier", "")),
                "bundle_version": str(row.get("CFBundleVersion", "")),
                "mcc": mcc,
                "mnc": mnc,
                "imsi": imsi,
                "iccid": iccid,
                "gid1": normalize_gid1(gid1),
                "identity_source": identity_from,
            })
        # Two lines must never share one selector: that would mean an
        # identifier was attributed to the wrong slot.  Hide such data rather
        # than prepare a package for the wrong SIM.
        seen: dict[tuple[str, str], int] = {}
        for line in lines:
            key = (str(line["imsi"]), str(line["iccid"]))
            if all(key):
                seen[key] = seen.get(key, 0) + 1
        for line in lines:
            if seen.get((str(line["imsi"]), str(line["iccid"])), 0) > 1:
                line["imsi"], line["iccid"] = "", ""
                line["conflict"] = True
        return {**common, "lines": lines}
    finally:
        with contextlib.suppress(Exception):
            await device.close()


async def read_state(slot: str | None) -> dict[str, object]:
    """Return one selected line; legacy callers may omit slot on one-line phones."""
    overview = await read_states()
    lines = overview["lines"]
    chosen = [row for row in lines if row.get("slot") == slot] if slot else lines
    if len(chosen) != 1:
        available = [str(row.get("slot", "")) for row in lines]
        raise RuntimeError(
            "select exactly one carrier line with --slot; available slots: "
            + ", ".join(available)
        )
    return chosen[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", help="Slot value shown by the paired phone")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        print(f"error: refusing to overwrite {args.output}", file=sys.stderr)
        return 2
    try:
        state = asyncio.run(read_state(args.slot))
        # Windows: no mode=0o700 (Python 3.13+ turns it into an owner-only ACL).
        if os.name == "nt":
            args.output.parent.mkdir(parents=True, exist_ok=True)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        args.output.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.chmod(args.output, stat.S_IRUSR | stat.S_IWUSR)
    except Exception as exc:
        message = re.sub(r"[0-9]{12,}", "<private identifier>", str(exc))
        print(f"error: {type(exc).__name__}: {message}", file=sys.stderr)
        return 2
    print(
        "Read-only carrier state saved privately: "
        f"{state['product']} {state['version']} ({state['build']}), "
        f"{state['bundle_identifier']} {state['bundle_version']}; "
        f"IMSI={'yes' if state['imsi'] else 'no'}, "
        f"ICCID={'yes' if state['iccid'] else 'no'}, "
        f"GID1={'yes' if state['gid1'] else 'no'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
