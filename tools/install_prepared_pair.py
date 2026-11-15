#!/usr/bin/env python3
"""Preflight or send one already-verified, exact-build carrier IPCC.

Read-only preflight is separate from the mutating commands.  Installation
requires an explicit ``--execute`` flag and leaves a local one-shot marker so
an uncertain result cannot be retried blindly.  ``switch`` sends once, restarts
once, and verifies; it never retries either operation.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
import plistlib
import re
import stat
import sys
import zipfile
from pathlib import Path

from pymobiledevice3.exceptions import (
    AppInstallError, DeviceNotFoundError, PyMobileDevice3Exception,
)

if __package__:
    from tools.build_selector_ipcc import BuildError, make_private_dir, normalized_selector
    from tools.read_phone_carrier_state import read_state
else:
    from build_selector_ipcc import BuildError, make_private_dir, normalized_selector
    from read_phone_carrier_state import read_state


DIRECTIONS = ("forward", "revert")

# Reconnection limits (seconds); module-level so offline tests can shorten them.
CONNECT_TIMEOUT = 75
RESTART_CONNECT_TIMEOUT = 90
CONNECT_RETRY_DELAY = 2


class PreTransferConnectionError(BuildError):
    """The phone could not be reached before any IPCC transfer began."""


class InstallResultUnknownError(BuildError):
    """The connection failed after the IPCC transfer call had begun."""


class RestartResultUnknownError(BuildError):
    """The diagnostics connection failed after the restart call had begun."""


class InstallRejectedError(BuildError):
    """installation_proxy explicitly answered with an error: nothing was applied."""


class RestartRefusedError(BuildError):
    """The diagnostics service answered the Restart request with a failure."""


INSTALL_ERROR_HINTS = {
    "InstallProhibited": (
        "установка запрещена ограничениями iPhone. Проверьте: Настройки → Экранное время → "
        "Контент и конфиденциальность → Покупки в iTunes и App Store → Установка приложений → "
        "«Разрешить». Если iPhone управляется организацией (MDM), снять запрет может только она."
    ),
}


def _explicit_install_error(exc: BaseException) -> BaseException | None:
    """Find an explicit installd answer anywhere in the exception chain.

    pymobiledevice3 deletes the staging folder in a ``finally`` block, so a
    clear refusal can be wrapped by a later cleanup/connection error.
    An AppInstallError without text means the socket closed: ambiguous.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, AppInstallError) and str(current).strip():
            return current
        current = current.__cause__ or current.__context__
    return None


def describe_install_error(exc: BaseException) -> str:
    text = str(exc).strip()
    for code, hint in INSTALL_ERROR_HINTS.items():
        if code in text:
            return f"{hint} (код iOS: {code})"
    return text


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_manifest(pair: Path) -> dict[str, object]:
    try:
        manifest = json.loads((pair / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError("prepared pair manifest is unavailable") from exc
    if manifest.get("method") not in ("paired-ipcc-outer-selector", "selector-ipcc"):
        raise BuildError("unexpected prepared-package method")
    if manifest.get("selector_fields") != ["IMSI", "ICCID"]:
        raise BuildError("prepared pair is not the reviewed no-GID selector")
    return manifest


def inspect_package(pair: Path, direction: str, manifest: dict[str, object]) -> bytes:
    package_entry = package_entry_for(manifest, direction)
    filename = str(package_entry["file"])
    expected_identifier = str(package_entry["bundle_identifier"])
    path = pair / filename
    payload = path.read_bytes()
    if sha256(payload) != package_entry.get("sha256"):
        raise BuildError(f"prepared package digest changed: {filename}")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if not names or not names[0].startswith("Payload/") or not names[0].endswith(".bundle/"):
            raise BuildError("prepared package has an invalid outer directory")
        root = names[0]
        if any(not name.startswith(root) for name in names):
            raise BuildError("prepared package contains data outside its selector directory")
        selector = root.removeprefix("Payload/").removesuffix("/")
        if sha256(selector.encode("utf-8")) != manifest.get("selector_sha256"):
            raise BuildError("prepared package selector does not match its manifest")
        info = plistlib.loads(archive.read(root + "Info.plist"))
    if (info.get("CFBundleIdentifier") != expected_identifier
            or info.get("CFBundleVersion") != package_entry.get("bundle_version")):
        raise BuildError("prepared package has an unexpected carrier identity/version")
    return payload


def check_state(state: dict[str, object], manifest: dict[str, object], direction: str) -> None:
    selector = normalized_selector(str(state.get("imsi", "")), str(state.get("iccid", "")))
    if sha256(selector.encode("utf-8")) != manifest.get("selector_sha256"):
        raise BuildError("connected SIM does not match the prepared private selector")
    if not str(state.get("bundle_identifier", "")).startswith("com.apple."):
        raise BuildError("phone reports an unexpected active carrier bundle")


def version_tuple(value: object) -> tuple[int, ...]:
    """Compare carrier versions such as 73.0, 73.0.1 or 47.5a without crashing.

    Each dot-separated part contributes its leading digits.  A version without
    any leading digit is still rejected, because it cannot be ordered safely.
    """
    parts = []
    for part in str(value if value is not None else "").strip().split("."):
        match = re.match(r"\d+", part)
        if not match:
            break
        parts.append(int(match.group()))
    if not parts:
        raise BuildError(f"версия операторского пакета не числовая: {value!r}")
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def package_entry_for(manifest: dict[str, object], direction: str) -> dict[str, object]:
    entry = next((p for p in manifest.get("packages", []) if p.get("role") == direction), None)
    if entry is None:
        legacy_file = "vodafone-ro-forward.ipcc" if direction == "forward" else "mts-ru-revert.ipcc"
        entry = next((p for p in manifest.get("packages", []) if p.get("file") == legacy_file), None)
    if not isinstance(entry, dict):
        raise BuildError(f"manifest does not contain a {direction} package")
    return entry


def safe_state(state: dict[str, object]) -> dict[str, object]:
    return {key: state.get(key) for key in (
        "product", "hardware", "version", "build", "bundle_identifier", "bundle_version"
    )}


async def send_once(payload: bytes, expected_serial: str | None = None) -> None:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.installation_proxy import InstallationProxyService
    from pymobiledevice3.usbmux import list_devices

    # Applying the first carrier package can briefly remove the phone from
    # usbmux after it has already passed the caller's read-only state check.
    # Retry only this pre-transfer connection phase.  install_from_bytes below
    # is still called exactly once, so an ambiguous completed transfer is never
    # sent again.
    deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT
    last_error: BaseException | None = None
    device = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            devices = await asyncio.wait_for(list_devices(), 12)
            usb = [candidate for candidate in devices if candidate.connection_type == "USB"]
            if expected_serial:
                usb = [candidate for candidate in usb if candidate.serial == expected_serial]
            elif len(usb) > 1:
                raise BuildError("подключено несколько iPhone по USB; оставьте один")
            if not usb:
                raise DeviceNotFoundError("iPhone temporarily absent from usbmux")
            device = await asyncio.wait_for(
                create_using_usbmux(
                    serial=usb[0].serial,
                    connection_type="USB",
                    autopair=True,
                    pair_timeout=25,
                ),
                35,
            )
            break
        except BuildError:
            raise
        except (PyMobileDevice3Exception, OSError, asyncio.TimeoutError) as exc:
            last_error = exc
            await asyncio.sleep(CONNECT_RETRY_DELAY)
    if device is None:
        detail = f": {type(last_error).__name__}: {last_error}" if last_error else ""
        raise PreTransferConnectionError(
            "iPhone не вернулся в usbmux до начала отправки; пакет не отправлялся" + detail
        )
    transfer_started = False
    transfer_completed = False
    try:
        async with InstallationProxyService(device) as service:
            transfer_started = True
            await asyncio.wait_for(service.install_from_bytes(payload), 75)
            transfer_completed = True
    except Exception as exc:
        if transfer_completed:
            # install_from_bytes explicitly reported completion; a later
            # service-close error does not make a second transfer necessary.
            return
        explicit = _explicit_install_error(exc)
        if explicit is not None:
            # installd answered with an explicit Error/ErrorDescription.  The
            # package was refused, so there is nothing to reboot or verify.
            raise InstallRejectedError(
                f"iPhone отклонил пакет: {describe_install_error(explicit)}"
            ) from exc
        if transfer_started:
            raise InstallResultUnknownError(
                f"соединение прервалось после начала отправки: {type(exc).__name__}: {exc}"
            ) from exc
        raise PreTransferConnectionError(
            f"служба установки недоступна до начала отправки; пакет не отправлялся: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    finally:
        with contextlib.suppress(Exception):
            await device.close()


async def restart_once(expected_serial: str | None = None) -> None:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.diagnostics import DiagnosticsService
    from pymobiledevice3.usbmux import list_devices

    deadline = asyncio.get_running_loop().time() + RESTART_CONNECT_TIMEOUT
    last_error: BaseException | None = None
    device = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            devices = await asyncio.wait_for(list_devices(), 12)
            usb = [candidate for candidate in devices if candidate.connection_type == "USB"]
            if expected_serial:
                usb = [candidate for candidate in usb if candidate.serial == expected_serial]
            elif len(usb) > 1:
                raise BuildError("подключено несколько iPhone по USB; оставьте один")
            if not usb:
                raise DeviceNotFoundError("iPhone temporarily absent from usbmux")
            device = await asyncio.wait_for(
                create_using_usbmux(
                    serial=usb[0].serial, connection_type="USB", autopair=False, pair_timeout=15
                ),
                25,
            )
            break
        except BuildError:
            raise
        except (PyMobileDevice3Exception, OSError, asyncio.TimeoutError) as exc:
            last_error = exc
            await asyncio.sleep(CONNECT_RETRY_DELAY)
    if device is None:
        detail = f": {type(last_error).__name__}: {last_error}" if last_error else ""
        raise PreTransferConnectionError(
            "iPhone не вернулся в usbmux до команды перезагрузки; команда не отправлялась" + detail
        )
    restart_started = False
    restart_completed = False
    try:
        async with DiagnosticsService(device) as service:
            restart_started = True
            await asyncio.wait_for(service.restart(), 15)
            restart_completed = True
    except Exception as exc:
        if restart_completed:
            return
        if (isinstance(exc, PyMobileDevice3Exception)
                and "failed the restart" in str(exc).casefold()):
            # The phone answered "not Success": it will not reboot by itself.
            raise RestartRefusedError(
                f"iPhone отказался перезагружаться по команде: {exc}"
            ) from exc
        if restart_started:
            raise RestartResultUnknownError(
                f"соединение закрылось после команды перезагрузки: {type(exc).__name__}: {exc}"
            ) from exc
        raise PreTransferConnectionError(
            f"служба перезагрузки недоступна до отправки команды: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        with contextlib.suppress(Exception):
            await device.close()


async def wait_for_verified_state(
    args: argparse.Namespace,
    manifest: dict[str, object],
    desired_identifier: str,
    desired_version: str,
) -> dict[str, object] | None:
    deadline = asyncio.get_running_loop().time() + args.verify_timeout
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(3)
        try:
            state = await asyncio.wait_for(read_state(args.slot), 20)
            check_state(state, manifest, args.direction)
            if (state.get("bundle_identifier") == desired_identifier
                    and version_tuple(state.get("bundle_version")) >= version_tuple(desired_version)):
                return state
        except (BuildError, OSError, asyncio.TimeoutError, RuntimeError, PyMobileDevice3Exception):
            continue
    return None


def write_receipt(path: Path, receipt: dict[str, object]) -> None:
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


async def run(args: argparse.Namespace) -> int:
    pair = args.pair.resolve()
    state_dir = (args.state_dir or pair).resolve()
    make_private_dir(state_dir)
    os.chmod(state_dir, stat.S_IRWXU)
    manifest = load_manifest(pair)
    # Validate every package present before considering an install.
    inspect_package(pair, "forward", manifest)
    if any(package.get("role") == "revert" for package in manifest.get("packages", [])):
        inspect_package(pair, "revert", manifest)
    payload = inspect_package(pair, args.direction, manifest)
    state = await read_state(args.slot)
    check_state(state, manifest, args.direction)
    entry = package_entry_for(manifest, args.direction)
    desired_identifier = str(entry["bundle_identifier"])
    desired_version = str(entry["bundle_version"])

    print(
        "Preflight passed: matching private SIM selector and verified Apple-signed package."
    )
    if args.command == "preflight":
        print("Read-only preflight complete; no install or reboot attempted.")
        return 0
    marker_suffix = "switch-sent" if args.command == "switch" else "sent"
    marker_material = {
        "selector": manifest.get("selector_sha256"),
        "target": manifest.get("target"),
        "packages": [
            {key: package.get(key) for key in ("role", "bundle_identifier", "bundle_version", "sha256")}
            for package in manifest.get("packages", [])
        ],
    }
    pair_key = sha256(json.dumps(marker_material, sort_keys=True).encode())[:16]
    marker = state_dir / f".{pair_key}-{args.direction}-{marker_suffix}.once"
    if (state.get("bundle_identifier") == desired_identifier
            and version_tuple(state.get("bundle_version")) >= version_tuple(desired_version)):
        if args.command == "switch":
            marker.unlink(missing_ok=True)
        print(f"Already active: {desired_identifier} {state.get('bundle_version')}; no install or reboot attempted.")
        return 0
    if not args.execute:
        raise BuildError("installation requires the explicit --execute flag")

    if marker.exists():
        raise BuildError("one-shot marker already exists; inspect phone state before any retry")
    marker.write_text("One IPCC send attempted; do not retry blindly.\n", encoding="utf-8")
    os.chmod(marker, stat.S_IRUSR | stat.S_IWUSR)
    await send_once(payload, expected_serial=str(state.get("udid", "")) or None)
    if args.command == "install":
        print("Installation proxy reported completion. No reboot was requested; reboot the phone manually once.")
        return 0

    receipt_path = state_dir / f"{pair_key}-{args.direction}-switch-result.json"
    receipt = {
        "operation": "paired-ipcc-switch",
        "direction": args.direction,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "before": safe_state(state),
        "desired": {"bundle_identifier": desired_identifier, "bundle_version": desired_version},
        "phase": "install_complete",
        "identifiers_recorded": False,
    }
    write_receipt(receipt_path, receipt)
    print("Installation proxy reported completion. Requesting one normal restart...")
    await restart_once(expected_serial=str(state.get("udid", "")) or None)
    receipt["phase"] = "restart_requested"
    write_receipt(receipt_path, receipt)
    print("Waiting for the same phone and carrier state after reboot...")
    after = await wait_for_verified_state(args, manifest, desired_identifier, desired_version)
    if after is None:
        receipt["phase"] = "unconfirmed_after_restart"
        receipt["finished_utc"] = datetime.now(timezone.utc).isoformat()
        write_receipt(receipt_path, receipt)
        print("UNCONFIRMED: no second install or restart was sent. Unlock the phone and run preflight.")
        return 3
    receipt["phase"] = "verified"
    receipt["after"] = safe_state(after)
    receipt["finished_utc"] = datetime.now(timezone.utc).isoformat()
    write_receipt(receipt_path, receipt)
    marker.unlink(missing_ok=True)
    print(f"Verified after reboot: {desired_identifier} {after.get('bundle_version')}.")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--pair", type=Path, default=Path(".private/24B119-no-gid-pair"),
        help="prepared owner-only forward/revert directory",
    )
    result.add_argument("--slot", help="Slot value when more than one line is active")
    result.add_argument("--state-dir", type=Path, help="persistent owner-only markers and receipts")
    sub = result.add_subparsers(dest="command", required=True)
    preflight = sub.add_parser("preflight", help="read-only package and phone checks")
    preflight.add_argument("direction", choices=DIRECTIONS)
    install = sub.add_parser("install", help="send exactly one package; never reboots")
    install.add_argument("direction", choices=DIRECTIONS)
    install.add_argument("--execute", action="store_true")
    switch = sub.add_parser("switch", help="send once, restart once, and verify without retry")
    switch.add_argument("direction", choices=DIRECTIONS)
    switch.add_argument("--execute", action="store_true")
    switch.add_argument("--verify-timeout", type=int, default=180, choices=range(60, 301))
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except (BuildError, OSError, zipfile.BadZipFile, asyncio.TimeoutError, PyMobileDevice3Exception) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
