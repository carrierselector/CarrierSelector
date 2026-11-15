"""Offline iPhone simulator for end-to-end CarrierSelector tests.

It replaces only the pymobiledevice3 transport (usbmux list, lockdown values,
installation_proxy and diagnostics restart).  Everything else — SIM reading,
profile choice, IPCC build with Apple signature checks, one-shot markers,
receipts and verification — runs the real application code.

A package is applied only to the SIM whose IMSI+ICCID equals the selector
inside the IPCC, exactly like CommCenter does, so a package prepared for the
wrong SIM shows up as "orphan" and never becomes active.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import plistlib
import zipfile
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest import mock

from pymobiledevice3.exceptions import (
    AppInstallError, ConnectionTerminatedError, PyMobileDevice3Exception,
)

from tools.build_selector_ipcc import normalized_selector

IMSI = "InternationalMobileSubscriberIdentity"


def _vt(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in str(value).split(".") if part.isdigit())
ICCID = "IntegratedCircuitCardIdentity"


@dataclass
class Sim:
    slot: str
    imsi: str
    iccid: str
    mcc: str
    mnc: str
    bundle: str
    version: str
    # The SIM's built-in carrier profile and whether a user (selector) package is active.
    system: tuple[str, str] | None = None
    user_active: bool = False

    def __post_init__(self):
        if self.system is None:
            self.system = (self.bundle, self.version)

    @property
    def selector(self) -> str:
        return normalized_selector(self.imsi, self.iccid)


@dataclass
class FakePhone:
    sims: list[Sim]
    product: str = "iPhone18,2"   # iPhone 17 Pro Max: exact Telia/MTN/Vodafone 72 variants
    hardware: str = "V54AP"
    ios: str = "27.2.1"
    build: str = "24B156"
    udid: str = "FAKE-UDID-0001"
    # Where lockdown publishes identifiers: "row" (inside CarrierBundleInfoArray)
    # or "keys" (root keys given explicitly in root_keys).
    identifiers_in: str = "row"
    root_keys: dict[str, str] = field(default_factory=dict)
    # Per-send behaviour, consumed in order; default "ok".
    #   ok      – installd reports completion, CommCenter applies on restart
    #   reject  – installd answers with an explicit Error
    #   drop    – socket closes after upload (ambiguous), still applied on restart
    #   ignore  – accepted, but iOS keeps the previous profile after restart
    #   reject_masked – explicit Error, then the staging cleanup fails too
    #   early_revert  – applied at once (before reboot), rolled back by the reboot
    behaviours: list[str] = field(default_factory=list)
    # Restart behaviour: "ok" (reboots), "refuse" (answers not Success),
    # "silent" (acknowledges but never reboots).
    restart_mode: str = "ok"
    # A real iPhone stays on USB for a few seconds after the restart command;
    # this many usbmux polls still see the pre-reboot state.
    restart_delay: int = 0
    reboot_countdown: int = -1
    reverts: dict[str, tuple[str, str]] = field(default_factory=dict)
    absent_after_restart: int = 2
    absent_now: int = 0
    sent_selectors: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    restarts: int = 0
    pending: dict[str, tuple[str, str]] = field(default_factory=dict)
    # CommCenter live log (os_trace relay): "ok", "broken" (service fails), "silent".
    log_mode: str = "ok"
    log_lines: list[str] = field(default_factory=list)
    # Reset packages (empty SupportedSIMs) received, and how iOS answers them:
    # "reject" (real iOS 27.2.1) or "accept" (a hypothetical future iOS).
    reset_answer: str = "reject"
    resets: int = 0

    # ---- lockdown view -------------------------------------------------
    def values(self) -> dict[str, object]:
        base = {
            "ProductType": self.product, "HardwareModel": self.hardware,
            "ProductVersion": self.ios, "BuildVersion": self.build,
        }
        if self.identifiers_in == "keys":
            base.update(self.root_keys)
        return base

    def rows(self) -> list[dict[str, object]]:
        rows = []
        for sim in self.sims:
            row = {"Slot": sim.slot, "CFBundleIdentifier": sim.bundle,
                   "CFBundleVersion": sim.version, "MCC": sim.mcc, "MNC": sim.mnc}
            if self.identifiers_in == "row":
                row[IMSI] = sim.imsi
                row[ICCID] = sim.iccid
            rows.append(row)
        return rows

    # ---- transport fakes ------------------------------------------------
    async def list_devices(self, *args, **kwargs):
        if self.reboot_countdown > 0:
            self.reboot_countdown -= 1
        elif self.reboot_countdown == 0:
            self.reboot_countdown = -1
            self._go_down()
        if self.absent_now > 0:
            self.absent_now -= 1
            return []
        return [SimpleNamespace(connection_type="USB", serial=self.udid)]

    async def create_using_usbmux(self, serial=None, **kwargs):
        phone = self

        class Lockdown:
            async def get_value(self, domain=None, key=None):
                if key is None:
                    return phone.values()
                if key == "CarrierBundleInfoArray":
                    return phone.rows()
                return phone.values().get(key)

            async def close(self):
                return None

        return Lockdown()

    def installation_proxy(self, lockdown):
        phone = self

        class Service:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def install_from_bytes(self, payload: bytes, *args, **kwargs):
                phone._receive(payload)

        return Service()

    def diagnostics(self, lockdown):
        phone = self

        class Service:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def restart(self):
                if phone.restart_mode == "refuse":
                    raise PyMobileDevice3Exception(
                        "The device failed the Restart diagnostics request: {'Status': 'Failure'}")
                if phone.restart_mode == "silent":
                    return None
                phone._restart()
                raise ConnectionTerminatedError("restart closes the connection")

        return Service()

    # ---- behaviour ------------------------------------------------------
    def _receive(self, payload: bytes) -> None:
        behaviour = self.behaviours.pop(0) if self.behaviours else "ok"
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            root = archive.namelist()[0]
            info = plistlib.loads(archive.read(root + "Info.plist"))
            carrier = plistlib.loads(archive.read(root + "carrier.plist"))
        selector = root.removeprefix("Payload/").rstrip("/")
        if carrier.get("SupportedSIMs") == []:
            self._receive_reset(selector, info)
            return
        if behaviour == "reject":
            raise AppInstallError("InstallProhibited: carrier bundle rejected by installd")
        if behaviour == "reject_masked":
            try:
                raise AppInstallError("InstallProhibited: installation is restricted")
            except AppInstallError:
                # pymobiledevice3's finally-cleanup failing on top of the refusal
                raise ConnectionTerminatedError("AFC closed while removing staging")
        self.sent_selectors.append(selector)
        target = next((sim for sim in self.sims if sim.selector == selector), None)
        if target is None:
            self.orphans.append(selector)
        elif target.user_active and _vt(info["CFBundleVersion"]) < _vt(target.version):
            # Real CommCenter: same SIM slot, lower version than the active user
            # package -> "kNoChange", the new bundle is deleted, built-in profile used.
            self.pending[selector] = target.system
        elif behaviour == "early_revert":
            self.reverts[selector] = (target.bundle, target.version)
            target.bundle, target.version = info["CFBundleIdentifier"], info["CFBundleVersion"]
        elif behaviour != "ignore":
            self.pending[selector] = (info["CFBundleIdentifier"], info["CFBundleVersion"])
        if behaviour == "drop":
            raise ConnectionTerminatedError("installation_proxy closed the socket")

    def _receive_reset(self, selector: str, info: dict) -> None:
        """CommCenter on iOS 27.2.1 (iPhone 15 Pro log): the user package bound to
        the selector is removed, the new one has no symlinks and is rejected."""
        self.resets += 1
        self.sent_selectors.append(selector)
        target = next((sim for sim in self.sims if sim.selector == selector), None)
        path = f"/private/var/mobile/Library/Carrier Bundles/iPhone/{selector}"
        self._log("[cb.man] Install bundle: /private/var/mobile/Media//PublicStaging/x.ipcc")
        if target is None:
            self.orphans.append(selector)
        if self.reset_answer == "accept":
            if target is not None:
                self.pending[selector] = (info["CFBundleIdentifier"], info["CFBundleVersion"])
            self._log('[cb.server] Reply dictionary after carrier bundle installation: {\n\t"kInstallResult": true\n}')
            return
        if target is not None and (target.user_active or selector in self.pending):
            self._log(f"[cb.bun] Attempting to remove the bundle {path} from the system")
            self._log("[cb.bun] Deleting Symlink <private>")
            self._log(f"[cb.bun] Deleting Bundle {path}")
        self._log("[cb.install] No symlinks available. Nothing to do.")
        self._log(f"[cb.bun] Deleting Bundle {path}")
        self._log('[cb.server] Reply dictionary after carrier bundle installation: {\n\t"kInstallResult": false\n}')
        if target is not None:
            self.pending[selector] = target.system  # built-in profile after the reboot

    def _log(self, message: str) -> None:
        if self.log_mode == "ok":
            self.log_lines.append(message)

    def os_trace(self, lockdown):
        phone = self

        class Service:
            async def __aenter__(self):
                if phone.log_mode == "broken":
                    raise ConnectionTerminatedError("os_trace relay unavailable")
                return self

            async def __aexit__(self, *exc):
                return False

            async def get_pid_list(self):
                return {"Status": "RequestSuccessful", "Payload": {"104": {"ProcessName": "CommCenter"}}}

            async def syslog(self, pid=-1, **kwargs):
                position = len(phone.log_lines)
                while True:
                    while position < len(phone.log_lines):
                        position += 1
                        yield SimpleNamespace(message=phone.log_lines[position - 1])
                    await asyncio.sleep(0.002)

        return Service()

    def _restart(self) -> None:
        self.restarts += 1
        if self.restart_delay:
            self.reboot_countdown = self.restart_delay
        else:
            self._go_down()

    def _go_down(self) -> None:
        for sim in self.sims:
            if sim.selector in self.pending:
                sim.bundle, sim.version = self.pending.pop(sim.selector)
                sim.user_active = (sim.bundle, sim.version) != sim.system
            if sim.selector in self.reverts:
                sim.bundle, sim.version = self.reverts.pop(sim.selector)
        self.absent_now = self.absent_after_restart

    @contextlib.contextmanager
    def attached(self):
        with mock.patch("pymobiledevice3.usbmux.list_devices", self.list_devices), \
                mock.patch("pymobiledevice3.lockdown.create_using_usbmux", self.create_using_usbmux), \
                mock.patch("pymobiledevice3.services.installation_proxy.InstallationProxyService",
                           self.installation_proxy), \
                mock.patch("pymobiledevice3.services.diagnostics.DiagnosticsService", self.diagnostics), \
                mock.patch("pymobiledevice3.services.os_trace.OsTraceService", self.os_trace), \
                mock.patch("tools.install_prepared_pair.CONNECT_RETRY_DELAY", 0.01):
            yield self
