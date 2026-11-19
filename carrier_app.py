#!/usr/bin/env python3
"""Interactive per-line iPhone carrier profile installer."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import sys
import traceback
import unicodedata
import zipfile

from pymobiledevice3 import exceptions as pmd3_exceptions
from pymobiledevice3.exceptions import ConnectionTerminatedError, PyMobileDevice3Exception

from tools.build_selector_ipcc import (
    BuildError, build_reset_payload, build_selection, make_private_dir, normalized_selector,
    read_bundle, verify_apple_signatures,
)
from tools.install_prepared_pair import (
    InstallRejectedError, InstallResultUnknownError, PreTransferConnectionError,
    RestartRefusedError, RestartResultUnknownError, check_state, inspect_package, load_manifest, package_entry_for, restart_once,
    safe_state, send_once, version_tuple, write_receipt,
)
from tools.read_phone_carrier_state import read_states


PROFILE_PAGE = 15
APP_VERSION = "6.5.1"

# Waiting limits (seconds).  Module-level so the offline test-suite can shorten them.
POLL_INTERVAL = 3
RECONNECT_TIMEOUT = 60
AFTER_SEND_TIMEOUT = 90
VERIFY_TIMEOUT = 240
REBOOT_DOWN_TIMEOUT = 60   # the phone must leave USB this soon after the restart command

PHASE_RU = {
    "checked": "проверка пройдена, отправка начата",
    "installed": "пакеты отправлены, перезагрузка не выполнена",
    "partially_installed": "отправлена только часть пакетов",
    "sent_unconfirmed": "пакет отправлен, ответ iPhone не получен",
    "send_failed": "ошибка во время отправки",
    "awaiting_manual_restart": "ожидалась ручная перезагрузка",
    "restart_requested": "перезагрузка запрошена",
    "unconfirmed_after_restart": "после перезагрузки профиль не подтвердился",
    "partially_verified": "после перезагрузки подтверждена только часть профилей",
    "restart_not_observed": "перезагрузка не обнаружена",
}

APN_HELP_RU = """Справка: APN и MMS операторов России и Беларуси
=============================================

Путь на iPhone:
  Настройки → Сотовая связь → нужная линия → Сотовая сеть передачи данных.

Раздел виден только тогда, когда активный операторский пакет разрешает ручную
правку APN. Если раздела нет, iOS не применит ручные значения: выберите пакет,
разрешающий правку, либо используйте автоматические настройки оператора.
Поля IMS/VoLTE/Wi-Fi Calling вручную не заполняются — их задаёт пакет.

МТС
  Сотовые данные / LTE / Режим модема: internet.mts.ru · пользователь mts · пароль mts
  MMS: APN mms.mts.ru · пользователь mts · пароль mts
       MMSC http://mmsc · прокси 192.168.192.192:8080 · размер 512000

МегаФон
  Сотовые данные / LTE / Режим модема: internet · имя и пароль пустые
  MMS: APN mms · пользователь mms · пароль mms
       MMSC http://mmsc:8002 · прокси 10.10.10.10:8080 · размер 307200

Yota
  Сотовые данные / LTE / Режим модема: internet.yota · имя и пароль пустые
  MMS: APN mms.yota · имя и пароль пустые
       MMSC http://mmsc:8002 · прокси 10.10.10.10:8080 · размер 307200

Билайн
  Сотовые данные / LTE / Режим модема: internet.beeline.ru
  Пользователь: beeline · пароль: beeline
  MMS: APN mms.beeline.ru · пользователь beeline · пароль beeline
       MMSC http://mms/ · прокси 192.168.94.23:8080 · размер 512000

T2 / Tele2
  Сотовые данные / LTE / Режим модема: internet.tele2.ru · имя и пароль пустые
  MMS: APN mms.tele2.ru · имя и пароль пустые
       MMSC http://mmsc.tele2.ru · прокси 193.12.40.65:8080 · размер 1048576

СберМобайл
  Сотовые данные / LTE / Режим модема: sberbank · имя и пароль пустые
  MMS: оставить автоматические значения оператора.

Ростелеком Мобайл
  Сотовые данные / LTE / Режим модема: internet.rtk.ru · имя и пароль пустые
  MMS: оставить автоматические значения оператора.

Т-Банк Мобайл
  Сотовые данные / LTE / Режим модема: tt · имя и пароль пустые
  MMS: оставить автоматические значения оператора.

ВТБ Мобайл
  Сотовые данные / LTE / Режим модема: vtb · имя и пароль пустые
  MMS: оставить автоматические значения оператора.

Газпромбанк Мобайл
  Сотовые данные / LTE / Режим модема: gpb · имя и пароль пустые
  MMS: оставить автоматические значения оператора.

Беларусь — МТС BY
  Сотовые данные / LTE / Режим модема: mts · пользователь mts · пароль mts
  MMS: APN mts · пользователь mts · пароль mts
       MMSC http://mmsc · прокси 192.168.192.168:8080 · размер 614400

Беларусь — A1
  Сотовые данные / LTE / Режим модема: internet · имя и пароль пустые
  MMS: APN mms · имя и пароль пустые
       MMSC http://mmsc · прокси 10.200.15.15:8080 · размер 307200

Беларусь — life:)
  Сотовые данные / LTE / Режим модема: internet.life.com.by · имя и пароль пустые
  MMS: APN mms.life.com.by · имя и пароль пустые
       MMSC http://mms.life.com.by/mmsc · прокси 10.10.10.20:8080 · размер 307200

Важно:
  • вводите значения латиницей, без пробелов до и после;
  • после изменения перезагрузите iPhone или включите и выключите авиарежим;
  • ручные APN хранятся отдельно для SIM и могут сохраниться после смены пакета;
  • «Сбросить настройки» внизу страницы возвращает значения активного пакета;
  • для региональных операторов и других MVNO используйте значения их поддержки:
    единых настроек для всех виртуальных операторов не существует.

Основные значения МТС, МегаФона, Yota, Билайна, T2, СберМобайла и операторов
Беларуси (МТС BY, A1, life:)) прочитаны из встроенных Apple carrier bundles. Остальные APN сверены с официальными
справочными страницами соответствующих операторов.
"""


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def state_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    root = base / "CarrierSelector"
    make_private_dir(root)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root


_PRIVATE_DIGITS = re.compile(r"\d{10,}")
_PRIVATE_HEX = re.compile(r"\b[0-9A-Fa-f]{8}-?[0-9A-Fa-f]{16}\b|\b[0-9a-fA-F]{40}\b")


def scrub(text: object) -> str:
    """Remove IMSI/ICCID/UDID-like values before anything is written to the log."""
    value = _PRIVATE_DIGITS.sub("<скрыто>", str(text))
    return _PRIVATE_HEX.sub("<скрыто>", value)


def program_root() -> Path:
    """The folder the user unpacked: next to the CarrierSelector binary
    (macOS/Linux), next to CarrierSelector.cmd (Windows) or start.py (Python)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    here = Path(__file__).resolve().parent
    if here.name == "app" and (here.parent / "CarrierSelector.cmd").is_file():
        return here.parent
    return here


_LOG_DIRS: dict[tuple[str, str], Path] = {}


def _writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".probe-{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def log_dir() -> Path:
    """logs inside the program folder; the private data folder only when the
    program folder is read-only (a mounted disk image, a protected folder)."""
    preferred, fallback = program_root() / "logs", state_root() / "logs"
    key = (str(preferred), str(fallback))
    if key not in _LOG_DIRS:
        _LOG_DIRS[key] = preferred if _writable_dir(preferred) else fallback
    return _LOG_DIRS[key]


def log_path() -> Path:
    return log_dir() / f"carrierselector-{datetime.now():%Y-%m-%d}.log"


def log(event: str, **fields: object) -> None:
    """Append one sanitized diagnostic line; logging must never break the app."""
    try:
        path = log_path()
        make_private_dir(path.parent)
        record = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "version": APP_VERSION, "event": event}
        record.update({key: scrub(value) if isinstance(value, str) else value
                       for key, value in fields.items()})
        with path.open("a", encoding="utf-8") as handle:
            handle.write(scrub(json.dumps(record, ensure_ascii=False, default=str)) + "\n")
    except Exception:
        pass


def log_overview(overview: dict[str, object]) -> None:
    log("phone", product=overview.get("product"), hardware=overview.get("hardware"),
        ios=overview.get("version"), build=overview.get("build"),
        lines=[{
            "slot": line.get("slot"), "mcc": line.get("mcc"), "mnc": line.get("mnc"),
            "bundle": line.get("bundle_identifier"), "bundle_version": line.get("bundle_version"),
            "has_imsi": bool(line.get("imsi")), "has_iccid": bool(line.get("iccid")),
            "conflict": bool(line.get("conflict")),
        } for line in overview.get("lines", []) if isinstance(line, dict)])


def _exception_is(exc: BaseException, *names: str) -> bool:
    classes = tuple(cls for cls in (getattr(pmd3_exceptions, name, None) for name in names) if cls)
    return bool(classes) and isinstance(exc, classes)


def explain_error(exc: BaseException) -> str:
    """Turn library exceptions into an actionable Russian message."""
    if _exception_is(exc, "PasswordRequiredError", "PasscodeRequiredError"):
        return "iPhone заблокирован. Разблокируйте его, не отключая кабель, и повторите."
    if _exception_is(exc, "PairingDialogResponsePendingError"):
        return ("на iPhone ждёт ответа вопрос «Доверять этому компьютеру?». "
                "Нажмите «Доверять», введите код-пароль и повторите.")
    if _exception_is(exc, "UserDeniedPairingError"):
        return ("на iPhone выбрано «Не доверять». Отключите и снова подключите кабель, "
                "затем нажмите «Доверять».")
    if _exception_is(exc, "NotPairedError", "InvalidHostIDError", "FatalPairingError", "PairingError"):
        return ("компьютер не сопряжён с iPhone. Переподключите кабель, разблокируйте iPhone "
                "и подтвердите «Доверять этому компьютеру».")
    if _exception_is(exc, "ConnectionFailedToUsbmuxdError"):
        if os.name == "nt":
            return ("не запущена служба Apple Mobile Device Service. Установите приложение "
                    "Apple Devices или iTunes и перезагрузите Windows.")
        if sys.platform == "darwin":
            return "служба usbmuxd macOS недоступна. Переподключите iPhone или перезагрузите Mac."
        return ("служба usbmuxd не запущена. Установите и запустите её: "
                "sudo systemctl start usbmuxd")
    if isinstance(exc, (asyncio.TimeoutError, ConnectionTerminatedError)) or _exception_is(
            exc, "DeviceNotFoundError", "ConnectionFailedError", "MuxException"):
        return ("iPhone не ответил вовремя. Проверьте кабель, разблокируйте iPhone "
                "и повторите действие.")
    if isinstance(exc, (BuildError, RuntimeError)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def load_catalog() -> dict[str, object]:
    path = resource_root() / "embedded_catalog" / "catalog.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if catalog.get("format") != 1:
        raise BuildError("повреждён встроенный каталог")
    return catalog


def _folded(values: object) -> set[str]:
    return {str(value).casefold() for value in values} if isinstance(values, (list, tuple, set)) else set()


def matching_family(catalog: dict[str, object], overview: dict[str, object]) -> dict[str, object] | None:
    # Board names are compared without case: lockdown may report "D53GAP" for "D53gAP".
    product = str(overview.get("product", "")).casefold()
    hardware = str(overview.get("hardware", "")).casefold()
    exact = [family for family in catalog["families"]
             if product in _folded(family["products"]) and hardware in _folded(family["hardwares"])]
    if len(exact) == 1:
        return exact[0]
    by_board = [family for family in catalog["families"] if hardware in _folded(family["hardwares"])]
    return by_board[0] if len(by_board) == 1 else None


def catalog_profiles(family: dict[str, object] | None) -> list[dict[str, object]]:
    if not family or not family.get("catalog_root"):
        return []
    return [dict(row) for row in _catalog_root_profiles(str(family["catalog_root"]), str(family.get("name", "")))]


@functools.lru_cache(maxsize=None)
def _catalog_root_profiles(root_name: str, family_name: str) -> tuple[dict[str, object], ...]:
    family = {"name": family_name or root_name, "catalog_root": root_name}
    root = bundle_path(root_name)
    if not root.is_dir():
        raise BuildError(f"отсутствует полный каталог {family['name']}")
    rows = []
    for directory in sorted(root.glob("*.bundle"), key=lambda path: path.name.casefold()):
        # System fallback/metadata bundles are not selectable operator profiles.
        # "._Name.bundle" AppleDouble files appear after copying through macOS.
        if (directory.stem in {"Default", "DefaultBCAM", "OtherKnown"}
                or directory.name.startswith("._") or not directory.is_dir()):
            continue
        try:
            info = plistlib.loads((directory / "Info.plist").read_bytes())
            carrier = plistlib.loads((directory / "carrier.plist").read_bytes())
        except (OSError, plistlib.InvalidFileException) as exc:
            raise BuildError(f"повреждён профиль {directory.name}") from exc
        identifier = info.get("CFBundleIdentifier")
        version = info.get("CFBundleVersion")
        if (not (directory / "carrier.plist").is_file()
                or not isinstance(identifier, str) or not identifier.startswith("com.apple.")
                or not isinstance(version, str)):
            raise BuildError(f"некорректный профиль {directory.name}")
        name = str(info.get("CFBundleName") or directory.stem)
        public_name = carrier.get("CarrierName") if isinstance(carrier, dict) else None
        suffix = (
            f" · {public_name}" if isinstance(public_name, str)
            and public_name.casefold() not in name.casefold() else ""
        )
        relative = f"{root_name}/{directory.name}"
        key_hash = hashlib.sha256(relative.encode()).hexdigest()[:12]
        rows.append({
            "key": f"catalog-{key_hash}", "path": relative,
            "identifier": identifier, "version": version,
            "label": f"{name} {version}{suffix}", "legacy": False,
        })
    return tuple(rows)


def board_short(hardware: object) -> str:
    return re.sub(r"AP$", "", str(hardware or ""), flags=re.IGNORECASE)


@functools.lru_cache(maxsize=None)
def _bundle_boards(directory: Path) -> tuple[frozenset[str], tuple[tuple[str, ...], ...]]:
    """(advisory supported-device list, board groups of every override file) of a bundle."""
    groups = tuple(tuple(path.stem.removeprefix("overrides_").split("_"))
                   for path in sorted(directory.glob("overrides_*.plist")))
    return frozenset(_supported_devices(directory)), groups


def _supported_devices(directory: Path) -> set[str]:
    path = directory / "supported_devices.plist"
    if not path.is_file():
        return set()
    try:
        document = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise BuildError(f"повреждён supported_devices.plist в {directory.name}") from exc
    if not isinstance(document, dict):
        return set()
    values: set[str] = set()
    for key, value in document.items():
        if "supported" not in str(key).casefold():
            continue
        if isinstance(value, str):
            values.add(value)
        elif isinstance(value, list):
            values.update(str(item) for item in value if isinstance(item, (str, int)))
    return values


def profile_compatibility(directory: Path, overview: dict[str, object] | None) -> tuple[int, str]:
    """Rank a signed bundle without treating a missing board override as rejection."""
    if not overview:
        return 100, "плата iPhone не определена"
    hardware = str(overview.get("hardware", ""))
    short = board_short(hardware)
    product = str(overview.get("product", ""))
    supported, groups = _bundle_boards(directory)
    accepted = {hardware.casefold(), short.casefold(), product.casefold()}
    supported_match = bool({value.casefold() for value in supported} & accepted)
    overrides = []
    matching_override = False
    for boards in groups:
        overrides.extend(boards)
        if short.casefold() in {board.casefold() for board in boards}:
            matching_override = True
    if supported_match or matching_override:
        return 400, f"точные настройки {short or hardware}"
    if overrides:
        # Board settings exist only for OTHER models.  iOS evaluates the
        # override groups against the device ("Device overlay decision"), so
        # such a variant is not what Apple ships for this board.
        return UNSUITABLE_SCORE, "общие настройки; отдельного override платы нет"
    if supported:
        # This is not an install gate.  Real devices accept the signed common
        # profile even when this advisory list predates the board (for example
        # MTS_ru 73.0 on D23).  Rank it below an unrestricted common bundle.
        return 150, "общий профиль; плата не перечислена в справочном списке Apple"
    return 300, "универсальные общие настройки"


def catalog_roots(catalog: dict[str, object]) -> list[str]:
    """Every full Apple set in the catalog: each family's main root plus older extra roots."""
    roots: list[str] = []
    for item in catalog["families"]:
        for root in [item.get("catalog_root", "")] + list(item.get("extra_roots", [])):
            if root and str(root) not in roots:
                roots.append(str(root))
    return roots


def _all_profile_sources(
    catalog: dict[str, object], family: dict[str, object] | None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    preferred_root = str(family.get("catalog_root", "")) if family else ""
    if family:
        rows.extend(dict(item, source_priority=30) for item in family.get("donors", []))
    for item in catalog["families"]:
        priority = 20 if item is family else 10
        rows.extend(dict(donor, source_priority=priority) for donor in item.get("donors", []))
    seen_roots: set[str] = set()
    own_extra = [str(root) for root in family.get("extra_roots", [])] if family else []
    roots = ([preferred_root] if preferred_root else []) + own_extra
    roots.extend(catalog_roots(catalog))
    for root in roots:
        if not root or root in seen_roots:
            continue
        seen_roots.add(root)
        synthetic = {"name": root, "catalog_root": root}
        priority = 20 if root == preferred_root else 15 if root in own_extra else 10
        rows.extend(dict(row, source_priority=priority) for row in catalog_profiles(synthetic))
    original = dict(catalog["revert"])
    original.update(key="mts73-common", legacy=False, source_priority=5)
    rows.append(original)
    return rows


def profile_source_ios(catalog: dict[str, object], row: dict[str, object]) -> str:
    if row.get("ios"):
        return str(row["ios"])
    path = str(row.get("path", ""))
    root_name = path.split("/")[0] if "/" in path else ""
    for prefix, release in catalog.get("root_ios", {}).items():
        if root_name.startswith(prefix):
            return release
    if "modern" in path or "73" in str(row.get("key", "")):
        return "27.2"
    return ""


def profiles_for(
    catalog: dict[str, object], family: dict[str, object] | None,
    overview: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """Return the global signed catalog, preferring the best variant for this board."""
    phone_ios = _ios_tuple(overview.get("version")) if overview else ()
    unique: dict[tuple[str, str], dict[str, object]] = {}
    for source in _all_profile_sources(catalog, family):
        row = dict(source)
        source_ios = profile_source_ios(catalog, row)
        row["ios"] = source_ios
        if (not source.get("ios") and _ios_tuple(source_ios) and _ios_tuple(source_ios) < (27, 0)
                and not (phone_ios and phone_ios < (27, 0))):
            # Full iOS 26 sets exist only for phones on iOS 26; on iOS 27 their
            # board-specific 70.0 files must not outrank the current 72.x/73.x.
            continue
        if phone_ios and phone_ios < (27, 0):
            p_ios = _ios_tuple(source_ios)
            if not p_ios or p_ios >= (27, 0):
                continue
            if phone_ios[0] == 26 and p_ios[0] == 26:
                pass
            elif p_ios > phone_ios:
                continue
        directory = bundle_path(str(row["path"]))
        if not row.get("version"):
            try:
                info = plistlib.loads((directory / "Info.plist").read_bytes())
                row["version"] = str(info["CFBundleVersion"])
            except (OSError, KeyError, plistlib.InvalidFileException) as exc:
                raise BuildError(f"повреждён профиль {row['label']}") from exc
        score, compatibility = profile_compatibility(directory, overview)
        row["compatibility_score"] = score
        row["compatibility"] = compatibility
        identity = (str(row["identifier"]), str(row["version"]))
        rank = (score, int(row.get("source_priority", 0)))
        current = unique.get(identity)
        current_rank = (
            int(current.get("compatibility_score", 0)),
            int(current.get("source_priority", 0)),
        ) if current else (-1, -1)
        if rank > current_rank:
            unique[identity] = row
    return sorted(unique.values(), key=lambda row: str(row["label"]).casefold())


# Compatibility scores from profile_compatibility():
#   400 exact settings for this board, 300 universal (no board overrides),
#   150 common profile whose advisory device list omits the board (MTS/Beeline/
#       MegaFon; physically confirmed to apply), 50 settings only for OTHER
#   boards (e.g. Vodafone_ro 73.0 built for iPhone 15 offered to iPhone 17).
#   The unsuitable score is the LOWEST on purpose: when two variants of the same
#   bundle/version exist, a suitable one must always win the de-duplication.
UNSUITABLE_SCORE = 50


def is_suitable(profile: dict[str, object]) -> bool:
    return int(profile.get("compatibility_score", 0)) != UNSUITABLE_SCORE


def _offer_rank(profile: dict[str, object]) -> tuple[int, tuple[int, ...], int]:
    try:
        version = version_tuple(profile.get("version"))
    except BuildError:
        version = ()
    curated = 0 if str(profile.get("key", "")).startswith("catalog-") else 1
    return int(profile.get("compatibility_score", 0)), version, curated


def offered_profiles(profiles: list[dict[str, object]]) -> list[dict[str, object]]:
    """Per operator: the newest suitable version (the one the current iOS
    firmware ships, e.g. 73.x for iOS 27.2.1), plus an older curated (tested)
    variant when there is one.  Unsuitable variants are hidden."""
    best: dict[str, dict[str, object]] = {}
    tested: dict[tuple[str, str], dict[str, object]] = {}
    for profile in profiles:
        if not is_suitable(profile):
            continue
        identifier = str(profile.get("identifier", ""))
        if identifier not in best or _offer_rank(profile) > _offer_rank(best[identifier]):
            best[identifier] = profile
        if not str(profile.get("key", "")).startswith("catalog-"):
            tested.setdefault((identifier, str(profile.get("version", ""))), profile)
    rows = list(best.values())
    for (identifier, version), profile in tested.items():
        if str(best[identifier].get("version", "")) != version:
            rows.append(profile)
    return sorted(rows, key=_offer_sort_key)


def _offer_sort_key(row: dict[str, object]) -> tuple[str, tuple[int, ...], str]:
    """Operators by name; within one operator the newest version first
    (Telia_dk 73.0, 68.0, 62.0), so the list reads top-down from new to old."""
    name = str(row.get("identifier", "")).removeprefix("com.apple.").casefold()
    try:
        version = tuple(-part for part in version_tuple(row.get("version")))
    except BuildError:
        version = ()
    return name, version, str(row.get("key", ""))


def refuse_downgrade_over_own_package(state: dict[str, object], identifier: object,
                                      version: object, label: object) -> None:
    """iOS keeps one user carrier bundle per SIM.  If one of OUR packages is active
    and a LOWER version arrives for the same SIM, CommCenter deletes the new
    package and falls back to the built-in profile (iOS log on iPhone 15 Pro /
    27.0.1: "Versions in the plist files (73.0, 68.0) ... kNoChange" followed by
    "Deleting Bundle").  Refuse before sending and explain the working order."""
    original = load_baseline(state)
    current_id = str(state.get("bundle_identifier", ""))
    if original is None or current_id == str(original.get("bundle_identifier")):
        return  # the SIM's own (built-in) carrier is active: any version is accepted
    try:
        lower = version_tuple(version) < version_tuple(state.get("bundle_version"))
    except BuildError:
        return
    if lower:
        slot = slot_label(state.get("slot"))
        raise BuildError(
            f"на {slot} сейчас активен ранее установленный пакет {carrier_name(state)} "
            f"{state.get('bundle_version')}, а {label} — более старая версия. Поверх нашего "
            "пакета iOS может такую версию не принять: удалить её и включить встроенный "
            "профиль (так было на iPhone 15 Pro с Telia_dk 68.0 поверх Vodafone_ro 73.0). "
            "Надёжный порядок: сначала верните исходный профиль (пункт 3), затем "
            f"установите {label}. Телефон не изменён"
        )


def require_model(overview: dict[str, object]) -> None:
    if not str(overview.get("hardware", "")).strip() or not str(overview.get("product", "")).strip():
        raise BuildError("iPhone не сообщил модель и плату; без этого установка запрещена")


def require_suitable(profile: dict[str, object], phone: dict[str, object]) -> None:
    """Mandatory gate: never send a package without settings for this iPhone's board."""
    require_model(phone)
    if not profile.get("path"):
        return  # a rollback package prepared earlier for this same SIM
    score, _ = profile_compatibility(bundle_path(str(profile["path"])), phone)
    if score == UNSUITABLE_SCORE:
        raise BuildError(
            f"{profile.get('label')} не содержит настроек для платы {board_short(phone.get('hardware'))} "
            f"этого iPhone ({phone.get('product')}); установка запрещена, телефон не изменён"
        )


def normalized_search(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value)).casefold()
    text = "".join(character if character.isalnum() else " " for character in text)
    return " ".join(text.split())


def profile_matches_query(profile: dict[str, object], query: str) -> bool:
    wanted = normalized_search(query).split()
    haystack = normalized_search(
        f"{profile.get('label', '')} {profile.get('identifier', '')}"
    )
    return all(token in haystack for token in wanted)


def bundle_path(relative: str) -> Path:
    path = (resource_root() / "embedded_catalog" / relative).resolve()
    root = (resource_root() / "embedded_catalog").resolve()
    if root not in path.parents:
        raise BuildError("недопустимый путь встроенного пакета")
    return path


def audit_entry(entry: dict[str, object]) -> str:
    files, info = read_bundle(bundle_path(str(entry["path"])))
    if info.get("CFBundleIdentifier") != entry["identifier"]:
        raise BuildError("имя встроенного профиля не совпадает")
    verify_apple_signatures(files)
    return str(info["CFBundleVersion"])


def selector_digest(state: dict[str, object]) -> str:
    value = normalized_selector(str(state.get("imsi", "")), str(state.get("iccid", "")))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def baseline_path(state: dict[str, object]) -> Path:
    return state_root() / "baselines" / f"{selector_digest(state)}.json"


def valid_original_identity(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    identifier = value.get("bundle_identifier")
    version = value.get("bundle_version")
    if not isinstance(identifier, str) or not identifier.startswith("com.apple."):
        return False
    if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+)*", version):
        return False
    return True


def recover_legacy_baseline(state: dict[str, object]) -> dict[str, object] | None:
    """Recover the original profile only from a receipt for this exact SIM."""
    digest = selector_digest(state)
    evidence: dict[tuple[str, str], list[tuple[float, dict[str, object]]]] = {}
    for manifest_path in state_root().rglob("manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("selector_sha256") != digest:
            continue
        prepared = manifest.get("prepared_from")
        prepared = prepared if isinstance(prepared, dict) else {}
        for package in manifest.get("packages", []):
            if not isinstance(package, dict) or package.get("role") != "forward":
                continue
            key = (str(package.get("bundle_identifier", "")), str(package.get("bundle_version", "")))
            evidence.setdefault(key, []).append((manifest_path.stat().st_mtime, prepared))

    found: list[dict[str, object]] = []
    receipt_paths = list(state_root().glob("*-install-result.json"))
    receipt_paths.extend(state_root().glob("*-switch-result.json"))
    loaded = []
    for path in set(receipt_paths):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(receipt, dict):
            loaded.append((_receipt_started(path, receipt), path, receipt))
    # Oldest attempt first: its "before" is the SIM's original profile.  Sorting
    # by file mtime was wrong because a receipt is rewritten during its attempt.
    for _, path, receipt in sorted(loaded, key=lambda item: (item[0], item[1].name)):
        if "restore" in str(receipt.get("operation", "")).casefold():
            # A restore receipt can itself have been produced from an already
            # corrupted baseline, so it must never redefine the original.
            continue
        before_rows = receipt.get("before", [])
        desired_rows = receipt.get("desired", [])
        selector_hashes = receipt.get("selector_hashes", [])
        if isinstance(before_rows, dict):
            before_rows = [before_rows]
        if isinstance(desired_rows, dict):
            desired_rows = [desired_rows]
        if not isinstance(before_rows, list) or not isinstance(desired_rows, list):
            continue
        if not isinstance(selector_hashes, list):
            selector_hashes = []
        try:
            started = datetime.fromisoformat(str(receipt.get("started_utc", ""))).timestamp()
        except ValueError:
            started = path.stat().st_mtime
        for index, (before, desired) in enumerate(zip(before_rows, desired_rows)):
            if not isinstance(before, dict) or not isinstance(desired, dict):
                continue
            recorded_digest = selector_hashes[index] if index < len(selector_hashes) else None
            exact = recorded_digest == digest
            if not exact:
                target = (str(desired.get("bundle_identifier", "")), str(desired.get("bundle_version", "")))
                matches = evidence.get(target, [])
                exact = any(
                    stamp <= started + 120 and started - stamp <= 86400
                    and str(prepared.get("product", before.get("product", ""))) == str(before.get("product", ""))
                    and str(prepared.get("hardware", before.get("hardware", ""))) == str(before.get("hardware", ""))
                    and (not desired.get("slot") or str(desired.get("slot")) == str(state.get("slot", "")))
                    and (not prepared.get("slot") or str(prepared.get("slot")) == str(state.get("slot", "")))
                    and str(before.get("product", "")) == str(state.get("product", ""))
                    and str(before.get("hardware", "")) == str(state.get("hardware", ""))
                    for stamp, prepared in matches
                )
            if not exact:
                continue
            identifier = before.get("bundle_identifier")
            version = before.get("bundle_version")
            candidate = {"bundle_identifier": identifier, "bundle_version": version}
            if valid_original_identity(candidate):
                found.append(candidate)
    if not found:
        return None
    # The oldest receipt names the SIM's own carrier.  Its built-in profile only
    # ever gets NEWER (iOS / carrier updates), while a "before" can be older
    # after a return to an older version -- so take the newest version seen.
    first = found[0]
    same = [item for item in found if item["bundle_identifier"] == first["bundle_identifier"]]
    return max(same, key=lambda item: _version_key(item["bundle_version"]))


def _version_key(value: object) -> tuple[int, ...]:
    try:
        return version_tuple(value)
    except BuildError:
        return ()


def _ios_tuple(value: object) -> tuple[int, ...]:
    parts = []
    for part in str(value or "").split(".")[:2]:
        if not part.isdigit():
            break
        parts.append(int(part))
    return tuple(parts)


def native_firmware_version(state: dict[str, object], identifier: str) -> str | None:
    """Version of the carrier's built-in profile for the iOS version this phone runs.

    Each full Apple set in the catalog is tagged with the iOS release it comes
    from (catalog["root_ios"]).  The phone's own model sets are checked and the
    newest set not newer than the phone's iOS wins: e.g. iOS 27.0.1 -> 72.x,
    iOS 27.2 / 27.2.1 -> 73.x, a later iOS -> at least the newest known.
    """
    try:
        catalog = load_catalog()
    except (OSError, ValueError, BuildError):
        return None
    phone_ios = _ios_tuple(state.get("version"))
    family = matching_family(catalog, state)
    if not phone_ios or not family:
        return None
    tags = catalog.get("root_ios", {})
    best: tuple[tuple[int, ...], str] | None = None
    for root in [family.get("catalog_root", "")] + list(family.get("extra_roots", [])):
        ios = next((_ios_tuple(release) for prefix, release in tags.items() if str(root).startswith(prefix)), ())
        if not ios or ios > phone_ios:
            continue
        try:
            rows = _catalog_root_profiles(str(root), str(family.get("name", "")))
        except BuildError:
            continue
        version = next((str(row["version"]) for row in rows if row["identifier"] == identifier), None)
        if version and (best is None or ios > best[0]):
            best = (ios, version)
    return best[1] if best else None


def recover_private_state_baseline(state: dict[str, object]) -> dict[str, object] | None:
    """Recover pre-install state saved while preparing this exact SIM package."""
    digest = selector_digest(state)
    path = state_root() / f"phone-state-{digest[:16]}.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if selector_digest(saved) != digest:
            return None
    except (OSError, json.JSONDecodeError, BuildError):
        return None
    identifier = saved.get("bundle_identifier")
    version = saved.get("bundle_version")
    candidate = {"bundle_identifier": identifier, "bundle_version": version}
    if valid_original_identity(candidate):
        return candidate
    return None


def load_baseline(state: dict[str, object]) -> dict[str, object] | None:
    path = baseline_path(state)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("selector_sha256") != selector_digest(state):
        return None
    original = data.get("original")
    return original if valid_original_identity(original) else None


def _identity(value: dict[str, object]) -> dict[str, object]:
    return {"bundle_identifier": str(value["bundle_identifier"]), "bundle_version": str(value["bundle_version"])}


def remember_baseline(
    state: dict[str, object], *, allow_current: bool = True,
) -> dict[str, object] | None:
    existing = load_baseline(state)
    exact_receipt = recover_legacy_baseline(state)
    # Exact-selector receipts repair baselines written by builds that matched
    # receipts only by kOne/kTwo and could therefore cross two different phones.
    original = exact_receipt or existing or recover_private_state_baseline(state)
    if original is None and allow_current:
        candidate = safe_state(state)
        original = candidate if valid_original_identity(candidate) else None
    if original is None:
        return None
    # Only the carrier identity matters.  Older builds stored a whole state
    # snapshot here; comparing snapshots made every run rewrite the record (and
    # drop its prepared rollback package) although the original was unchanged.
    original = _identity(original)
    # The SIM's own carrier profile only ever gets newer.  A stale record (taken
    # before an iOS update, or after a return to an older version) must not win:
    # use the newest version among the stored record, the profile active right
    # now on this SIM, and the built-in profile of the iOS build the phone runs.
    identifier = str(original["bundle_identifier"])
    current = {key: state.get(key) for key in ("bundle_identifier", "bundle_version")}
    evidence = [existing, current]
    native = native_firmware_version(state, identifier)
    if native:
        evidence.append({"bundle_identifier": identifier, "bundle_version": native})
    for candidate in evidence:
        if (valid_original_identity(candidate) and candidate["bundle_identifier"] == identifier
                and _version_key(candidate["bundle_version"]) > _version_key(original["bundle_version"])):
            original = {"bundle_identifier": identifier, "bundle_version": str(candidate["bundle_version"])}
    if existing and _identity(existing) == original:
        return existing  # unchanged: keep the stored record and its rollback package
    if existing:
        log("baseline_updated", old=f"{existing.get('bundle_identifier')} {existing.get('bundle_version')}",
            new=f"{original['bundle_identifier']} {original['bundle_version']}")
    path = baseline_path(state)
    make_private_dir(path.parent)
    write_receipt(path, {
        "format": 1, "selector_sha256": selector_digest(state),
        "recorded_utc": datetime.now(timezone.utc).isoformat(),
        "original": original, "identifiers_recorded": False,
    })
    return original


def save_rollback_package(
    state: dict[str, object], original: dict[str, object],
    profile: dict[str, object], package_dir: Path,
) -> None:
    """Attach an immutable prepared rollback package to this SIM baseline."""
    path = baseline_path(state)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError("не удалось прочитать сохранённый исходный профиль") from exc
    if data.get("selector_sha256") != selector_digest(state) or data.get("original") != original:
        raise BuildError("исходный профиль SIM изменился во время подготовки")
    root = state_root().resolve()
    prepared = package_dir.resolve()
    if root not in prepared.parents:
        raise BuildError("обратный пакет находится вне защищённого каталога")
    data["rollback"] = {
        "package": prepared.relative_to(root).as_posix(),
        "bundle_identifier": str(profile["identifier"]),
        "bundle_version": str(profile["version"]),
        "label": str(profile["label"]),
    }
    write_receipt(path, data)


def cached_rollback_package(
    state: dict[str, object], original: dict[str, object],
) -> tuple[dict[str, object], Path] | None:
    path = baseline_path(state)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("selector_sha256") != selector_digest(state) or data.get("original") != original:
        return None
    rollback = data.get("rollback")
    if not isinstance(rollback, dict) or not isinstance(rollback.get("package"), str):
        return None
    root = state_root().resolve()
    package_dir = (root / str(rollback["package"])).resolve()
    if root not in package_dir.parents or not package_dir.is_dir():
        return None
    try:
        manifest = load_manifest(package_dir)
        check_state(state, manifest, "forward")
        inspect_package(package_dir, "forward", manifest)
        entry = package_entry_for(manifest, "forward")
        if (str(entry.get("bundle_identifier")) != str(rollback.get("bundle_identifier"))
                or str(entry.get("bundle_version")) != str(rollback.get("bundle_version"))):
            return None
    except (BuildError, OSError, ValueError):
        return None
    profile = {
        "key": "cached-rollback",
        "path": "",
        "identifier": str(rollback.get("bundle_identifier", "")),
        "version": str(rollback.get("bundle_version", "")),
        "label": str(rollback.get("label", "сохранённый обратный пакет")),
        "legacy": False,
        "compatibility": "заранее подготовленный обратный пакет",
    }
    return profile, package_dir


def exact_profile(profiles: list[dict[str, object]], original: dict[str, object]) -> dict[str, object] | None:
    identifier = str(original.get("bundle_identifier", ""))
    version = str(original.get("bundle_version", ""))
    matches = [
        profile for profile in profiles
        if str(profile.get("identifier")) == identifier
        and str(profile.get("version") or audit_entry(profile)) == version
    ]
    # Returning to the SIM's own original carrier must stay possible, so an
    # other-board variant is only a last resort, never a reason to refuse.
    return next((p for p in matches if is_suitable(p)), matches[0] if matches else None)


def compatible_original_profile(
    profiles: list[dict[str, object]], original: dict[str, object],
) -> dict[str, object] | None:
    """Choose the newest signed same-carrier bundle not newer than baseline."""
    identifier = str(original.get("bundle_identifier", ""))
    target = version_tuple(original.get("bundle_version"))
    candidates = []
    for profile in profiles:
        if str(profile.get("identifier")) != identifier:
            continue
        actual = version_tuple(profile.get("version") or audit_entry(profile))
        if actual <= target:
            candidates.append((is_suitable(profile), actual, profile))
    # Suitable board variants first, then the newest version not above the original.
    return max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


CACHE_NAMES = ("packages",) + tuple(f"packages-{n}" for n in range(2, 10))


def _cache_writable(root: Path) -> bool:
    try:
        make_private_dir(root)
        probe = root / f".probe-{os.getpid()}"
        probe.mkdir()
        probe.rmdir()
        return True
    except OSError as exc:
        log("package_cache_unwritable", folder=root.name, error=f"{type(exc).__name__}: {exc}")
        return False


def package_cache_roots():
    """Folders for prepared packages, in order of preference.

    A cache folder this user can no longer write to (for example created
    earlier from an elevated window on Windows) must not block installs:
    the next name inside the same private folder is used instead.
    """
    base = state_root()
    for name in CACHE_NAMES:
        root = base / name
        if _cache_writable(root):
            yield root


def _remove_prepared_dir(path: Path) -> None:
    base = state_root().resolve()
    target = path.resolve()
    try:
        relative = target.relative_to(base)
    except ValueError:
        relative = None
    if relative is None or len(relative.parts) < 3 or relative.parts[0] not in CACHE_NAMES:
        raise BuildError("отказ удалить каталог вне кэша пакетов")
    shutil.rmtree(target)


def prepare_package(entry: dict[str, object], state: dict[str, object]) -> Path:
    digest = selector_digest(state)
    key = re.sub(r"[^a-z0-9-]", "-", str(entry["key"]).lower())
    profile_material = "\0".join(str(entry.get(name, "")) for name in (
        "path", "identifier", "version",
    ))
    profile_hash = hashlib.sha256(profile_material.encode()).hexdigest()[:12]
    denied: PermissionError | None = None
    for root in package_cache_roots():
        output = root / f"{key}-{profile_hash}" / digest[:16]
        try:
            prepared = _prepare_package_at(output, entry, state, key, digest)
        except PermissionError as exc:
            # A folder left by an elevated run (Windows) or by another user:
            # try the next cache folder instead of failing the install.
            log("package_cache_denied", folder=root.name, error=f"{type(exc).__name__}: {exc}")
            denied = exc
            continue
        if root.name != CACHE_NAMES[0]:
            log("package_cache_fallback", folder=root.name)
        return prepared
    if denied is not None:
        raise denied
    raise BuildError(f"нет доступа на запись к папке {state_root()}. Запустите программу от "
                     "имени того же пользователя (и так же с правами или без прав администратора), "
                     "что и в прошлый раз, либо удалите эту папку.")


def _prepare_package_at(output: Path, entry: dict[str, object], state: dict[str, object],
                        key: str, digest: str) -> Path:
    if output.exists():
        try:
            manifest = load_manifest(output)
            inspect_package(output, "forward", manifest)
            if sha_selector(manifest) != digest:
                raise BuildError("кэшированный пакет подготовлен для другой SIM")
            prepared = package_entry_for(manifest, "forward")
            if (str(prepared.get("bundle_identifier")) != str(entry.get("identifier"))
                    or str(prepared.get("bundle_version")) != str(entry.get("version"))):
                raise BuildError("кэшированный пакет не совпадает с выбранным профилем")
            return output
        except PermissionError:
            raise
        except (BuildError, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            # A half-written or damaged cache must not block this SIM forever.
            log("cache_rebuild", reason=str(exc))
            print("Сохранённая копия пакета повреждена — готовлю её заново.")
            _remove_prepared_dir(output)
    # SIM identifiers stay in memory: v5.6 no longer writes phone-state files.
    args = argparse.Namespace(
        donor=bundle_path(str(entry["path"])), donor_identifier=str(entry["identifier"]),
        donor_slug=key, legacy_donor=bool(entry.get("legacy")),
        state=dict(state), state_json=None, output=output,
    )
    if build_selection(args) != 0:
        raise BuildError("не удалось подготовить профиль")
    return output


def sha_selector(manifest: dict[str, object]) -> str:
    return str(manifest.get("selector_sha256", ""))


def slot_label(slot: object, index: int | None = None) -> str:
    text = str(slot or "")
    return {"kOne": "SIM 1", "kTwo": "SIM 2"}.get(text, text or (f"SIM {index}" if index else "SIM"))


def carrier_name(line: dict[str, object]) -> str:
    return str(line.get("bundle_identifier", "?")).removeprefix("com.apple.")


def line_label(line: dict[str, object], index: int | None = None) -> str:
    plmn = str(line.get("mcc", "")) + str(line.get("mnc", ""))
    current = f"{carrier_name(line)} {line.get('bundle_version', '')}".strip()
    return f"{slot_label(line.get('slot'), index)} · {plmn or 'оператор не определён'} · сейчас {current}"


def valid_line(line: dict[str, object]) -> bool:
    try:
        normalized_selector(str(line.get("imsi", "")), str(line.get("iccid", "")))
        return True
    except BuildError:
        return False


def line_status(line: dict[str, object]) -> str:
    if line.get("conflict"):
        return "недоступно: iPhone сообщил одинаковые данные для двух SIM"
    missing = []
    if not str(line.get("imsi", "")).strip():
        missing.append("IMSI")
    if not str(line.get("iccid", "")).strip():
        missing.append("ICCID")
    if missing:
        return "недоступно: iPhone не сообщил " + " и ".join(missing)
    try:
        normalized_selector(str(line.get("imsi", "")), str(line.get("iccid", "")))
    except BuildError:
        return "недоступно: iPhone сообщил неполные данные SIM"
    return "готова"


def show_phone(overview: dict[str, object], catalog: dict[str, object]) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    family = matching_family(catalog, overview)
    print(f"iPhone: {overview.get('product')} · iOS {overview.get('version')} ({overview.get('build')})")
    if not family:
        print("ВНИМАНИЕ: для этой модели нет точного встроенного варианта; выбранный профиль может не примениться.")
    lines = list(overview.get("lines", []))
    if not lines:
        print("iPhone не сообщил активных SIM-линий.")
    else:
        print("SIM-линии:")
        for index, line in enumerate(lines, 1):
            print(f"  {index}. {line_label(line, index)} · {line_status(line)}")
    return family, lines


def choose_lines(lines: list[dict[str, object]], *, reset: bool = False) -> list[dict[str, object]] | None:
    usable = [line for line in lines if valid_line(line)]
    if not usable:
        raise BuildError("данные SIM пока недоступны; телефон не изменён")
    if len(usable) == 1:
        print(f"\nВыбрана единственная доступная линия: {line_label(usable[0])}")
        return usable
    print("\nДля каких SIM вернуть профиль, встроенный в iOS?" if reset
          else "\nНа какие SIM установить профили?")
    options: list[list[dict[str, object]]] = []
    if len(usable) > 1:
        options.append(usable)
        print("  1. Все доступные SIM" if reset
              else "  1. Все доступные SIM — профиль выбирается отдельно для каждой")
    for line in usable:
        options.append([line])
        print(f"  {len(options)}. Только {line_label(line)} · другую SIM не трогать")
    raw = input("Номер или Q для отмены: ").strip()
    if raw.casefold() == "q" or not raw:
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1]
    raise BuildError("неизвестный выбор SIM; телефон не изменён")


def is_active_row(row: dict[str, object], line: dict[str, object]) -> bool:
    """«активен» only for the exact version on the SIM (v6.2: before, both
    Telia_dk 68.0 and 73.0 were marked active when 68.0 was installed)."""
    if str(row.get("identifier")) != str(line.get("bundle_identifier")):
        return False
    active_version = str(line.get("bundle_version") or "")
    return not active_version or str(row.get("version")) == active_version


def is_mts_russia(line: dict[str, object]) -> bool:
    return operator_name(line) == "МТС"


def mts_telia_hint(offered: list[dict[str, object]], line: dict[str, object], query: str) -> str:
    """MTS (250-01) accepts Wi-Fi calling only with AES-128/SHA2-256/DH14 or
    AES-256/SHA1/DH14.  Telia_dk has that only up to 68.0 (iPhone 12-17, 16e,
    17e, Air; not iPhone 18 / Duo).  Searching for Telia on another model must
    not end with a profile that cannot give MTS Wi-Fi calling."""
    if "telia" not in query.casefold() or not is_mts_russia(line):
        return ""
    if any(str(row.get("identifier")) == "com.apple.Telia_dk"
           and str(row.get("version")) in {"68.0", "62.0"} for row in offered):
        return ""
    text = ("  Telia_dk 68.0 для этой модели нет. В Telia_dk 69–73 другие шифры звонков по Wi-Fi —"
            " шлюз МТС их не принимает.")
    alternatives = []
    for name in ("Vodafone_tr", "Vodafone_ro"):
        rows = sorted((row for row in offered if str(row.get("identifier")) == f"com.apple.{name}"),
                      key=_offer_sort_key)
        if rows:
            alternatives.append(f"{name} {rows[0].get('version')}")
    if alternatives:
        text += ("\n  Для звонков по Wi-Fi на МТС подойдёт " + " или ".join(alternatives)
                 + " (имя от сети, но конференции в них идут через сервер Турции или Румынии).")
    return text


# Operators by MCC/MNC of the SIM (the same codes the phone reports for a line).
OPERATORS = {
    ("250", "01"): "МТС", ("250", "99"): "Билайн", ("250", "02"): "МегаФон",
    ("250", "20"): "T2", ("250", "11"): "Yota",
    ("257", "02"): "МТС BY", ("257", "01"): "A1", ("257", "04"): "life:)",
}

# Profiles that suit each operator (results of device tests, firmware analysis
# and forum reports, November 2026).  version None = the newest offered version.
# They head the profile list with a short note; there is no "main"/"backup"
# label on purpose — the user picks by the notes.
RECOMMENDED: dict[str, tuple[tuple[str, str | None, str], ...]] = {
    "МТС": (
        ("Vodafone_tr", None,
         "имя от сети, SMS по Wi-Fi, USSD через IMS, Wi-Fi-звонки и в роуминге; конференции через Турцию"),
        ("Telia_dk", "68.0",
         "конференции через МТС; надпись «Norlys», SMS в авиарежиме не работают, нет роуминга"),
        ("Telia_dk", "62.0", "как 68.0, но надпись «TELIA»"),
        ("Vodafone_ro", None,
         "имя от сети, SMS по Wi-Fi; конференции через Румынию, нет роуминга и USSD через IMS"),
    ),
    # Ufone_pk is gone on purpose: Preconditions=None without
    # RequirePreconditionsWhenMandatory breaks incoming VoLTE calls on Beeline
    # (SIP logs, 2026-11-18).  Every Beeline entry below rang on a real call.
    "Билайн": (
        ("Ncell_np", None, "HD-голос EVS, SMS и USSD через IMS, конференции через Билайн; APN «web» не правится"),
        ("MobiCom_mn", None, "как Ncell_np: HD-голос EVS, SMS и USSD через IMS; APN «internet» не правится"),
        ("MTN_cy", None, "HD-голос EVS, правка APN; SMS и USSD не через IMS, нет переключателя VoLTE"),
        ("Vodafone_hu", None, "HD-голос EVS, правка APN; конференции через Венгрию"),
    ),
    "МегаФон": (
        ("MTN_cy", None, "HD-голос EVS, правка APN; много отзывов"),
        ("Ncell_np", None, "HD-голос EVS, USSD через IMS; APN «web» не правится"),
    ),
    "T2": (("MTN_cy", None, "5G и VoLTE, правка APN"),),
    "Yota": (("MTN_cy", None, "сеть МегаФона: HD-голос EVS, правка APN"),),
    "МТС BY": (("Telia_dk", None, "5G, VoLTE и HD-голос EVS, конференции через МТС BY"),),
    "life:)": (("MTS_ua", None, "5G; VoLTE у life официально не запущен"),),
}


def operator_name(line: dict[str, object]) -> str | None:
    mcc = str(line.get("mcc", "")).strip()
    mnc = str(line.get("mnc", "")).strip()
    if mnc.isdigit():
        mnc = f"{int(mnc):02d}"  # "1", "01" and "001" are the same network
    return OPERATORS.get((mcc, mnc))


def recommended_rows(offered: list[dict[str, object]],
                     line: dict[str, object]) -> list[tuple[dict[str, object], str]]:
    """(row, note) for the SIM's operator; only profiles that are offered for
    this iPhone (with settings for its board)."""
    result: list[tuple[dict[str, object], str]] = []
    for name, version, note in RECOMMENDED.get(operator_name(line) or "", ()):
        identifier = f"com.apple.{name}"
        # Only variants with this board's modem file (.der.pri): those are the
        # ones checked with 5G on real phones.  E.g. Ufone_pk on iPhone 16e/17e/
        # Air/18 has a board override without the modem file — still offered,
        # but not recommended until 5G is confirmed there.
        rows = [row for row in offered if str(row.get("identifier")) == identifier
                and (version is None or str(row.get("version")) == version)
                and int(row.get("compatibility_score", 0)) >= 400
                and (name in WITHOUT_MODEM_FILE or has_modem_file(row, line))]
        if not rows:
            continue
        rows.sort(key=_offer_sort_key)
        chosen = rows[0]
        if all(chosen is not item for item, _ in result):
            result.append((chosen, note))
    return result


# Recommended although no board has a modem file: MTS_ua gives 5G on life:)
# and MegaFon by user reports.
WITHOUT_MODEM_FILE = {"MTS_ua"}


# IKE proposals the MTS ePDG (213.87.142.77) accepts: probed IKE_SA_INIT on
# 2026-11-17 — everything else (iOS defaults, AES-256/SHA2, GCM, other DH
# groups or PRF) gets NO_PROPOSAL_CHOSEN.  (encryption, integrity, PRF, DH)
MTS_IKE_PROPOSALS = {("AES-128", "SHA2-256", "SHA2-256", 14), ("AES-256", "SHA1-96", "SHA1-160", 14)}


def _merge(base: object, update: object) -> object:
    if isinstance(base, dict) and isinstance(update, dict):
        merged = dict(base)
        for key, value in update.items():
            merged[key] = _merge(base.get(key), value) if key in base else value
        return merged
    return update


@functools.lru_cache(maxsize=None)
def bundle_settings(path: str, board: str) -> dict[str, object]:
    """carrier.plist merged with the override for this board (as iOS applies it)."""
    directory = bundle_path(path)
    try:
        settings = plistlib.loads((directory / "carrier.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return {}
    if not isinstance(settings, dict):
        return {}
    for override in sorted(directory.glob("overrides_*.plist")):
        boards = {item.casefold() for item in override.stem.removeprefix("overrides_").split("_")}
        if board.casefold() in boards:
            try:
                settings = _merge(settings, plistlib.loads(override.read_bytes()))
            except (OSError, plistlib.InvalidFileException):
                pass
            break
    return settings if isinstance(settings, dict) else {}


def _get(document: object, *path: str) -> object:
    for key in path:
        if not isinstance(document, dict):
            return None
        document = document.get(key)
    return document


def mts_wifi_calling(profile: dict[str, object], line: dict[str, object]) -> bool:
    """True when the profile can give Wi-Fi calling on MTS: one of the two IKE
    proposals the MTS gateway accepts, the gateway address built from the SIM
    (or not set) and Wi-Fi calling not switched off without MTS permission."""
    if not profile.get("path"):
        return False
    settings = bundle_settings(str(profile["path"]), board_short(line.get("hardware")))
    ike = _get(settings, "TechSettings", "IKE")
    if not isinstance(ike, dict):
        return False

    def values(value: object) -> list[object]:
        return list(value) if isinstance(value, (list, tuple)) else [value]

    # A proposal may list several algorithms per field; any combination counts.
    matches = any(
        (encryption, integrity, prf, group) in MTS_IKE_PROPOSALS
        for item in (ike.get("Proposals") or []) if isinstance(item, dict)
        for encryption in values(item.get("EncryptionAlgorithm"))
        for integrity in values(item.get("IntegrityAlgorithm"))
        for prf in values(item.get("PRFAlgorithm"))
        for group in values(item.get("DHGroup"))
    )
    address = str(ike.get("RemoteAddress") or "")
    allowed = _get(settings, "IMSConfig", "EnableWiFiCallingWithoutEntitlement") is not False
    return matches and (not address or "$" in address) and allowed


def has_modem_file(profile: dict[str, object], line: dict[str, object]) -> bool:
    """The board override names a modem settings file (DerPriFileName)."""
    if not profile.get("path"):
        return False
    return bool(bundle_settings(str(profile["path"]), board_short(line.get("hardware"))).get("DerPriFileName"))


def apn_editable(profile: dict[str, object], line: dict[str, object]) -> tuple[bool, str]:
    """(can the user change the APN in Settings, internet APN of the profile)."""
    if not profile.get("path"):
        return True, ""
    settings = bundle_settings(str(profile["path"]), board_short(line.get("hardware")))
    internet = ""
    for item in settings.get("apns") or []:
        if isinstance(item, dict) and int(item.get("type-mask") or 0) & 1:
            internet = str(item.get("apn") or "")
            break
    return settings.get("AllowEDGEEditing") is True, internet


def breaks_beeline_incoming(profile: dict[str, object], line: dict[str, object]) -> bool:
    """True when the profile sets IMS Signaling Preconditions to 'None' without
    defining RequirePreconditionsWhenMandatory. On Beeline (250-99), incoming
    calls fail with BYE cause 127 because iPhone answers 183 Require: precondition
    without preconditions in SDP (tested on real iPhone 17 Pro Max, 2026-11-18)."""
    if not profile.get("path"):
        return False
    settings = bundle_settings(str(profile["path"]), board_short(line.get("hardware")))
    sig = _get(settings, "IMSConfig", "Signaling")
    if not isinstance(sig, dict):
        return False
    return sig.get("Preconditions") == "None" and "RequirePreconditionsWhenMandatory" not in sig


def choice_warnings(row: dict[str, object], line: dict[str, object]) -> None:
    """Warnings printed right after a profile is picked.  The plan that follows
    is confirmed with INSTALL, so the user can still cancel there."""
    warnings = []
    if (is_mts_russia(line) and str(row.get("identifier")) != "com.apple.MTS_ru"
            and not mts_wifi_calling(row, line)):
        warnings.append(
            f"С {row['label']} звонков по Wi-Fi на МТС не будет: шлюз МТС принимает только шифры "
            "AES-128/SHA2-256/DH14 или AES-256/SHA1/DH14, а в этом профиле их нет. "
            "5G и VoLTE это не касается. Для звонков по Wi-Fi на МТС подходят Vodafone_tr, "
            "Vodafone_ro и Telia_dk 62.0/68.0."
        )
    if operator_name(line) == "Билайн" and breaks_beeline_incoming(row, line):
        warnings.append(
            "С этим профилем входящие звонки на Билайне не проходят: звонящий слышит "
            "„абонент недоступен“. Выберите профиль с пометкой ★."
        )
    editable, internet = apn_editable(row, line)
    if not editable and internet:
        warnings.append(
            f"В {row['label']} правка APN недоступна: останется APN профиля «{internet}», "
            "свой APN оператора прописать не получится. На Билайне интернет с чужим APN работает; "
            "если у вас не заработает — выберите профиль с правкой APN."
        )
    for text in warnings:
        print("\n  ⚠ " + text)
    if warnings:
        print("  Отменить можно на следующем шаге: не вводите INSTALL.")


def choose_profile(profiles: list[dict[str, object]], line: dict[str, object]) -> dict[str, object] | None:
    offered = offered_profiles(profiles)
    board = board_short(line.get("hardware")) or "?"
    operator = operator_name(line)
    recommended = recommended_rows(offered, line)
    notes_for = {id(row): note for row, note in recommended}
    ordered = [row for row, _ in recommended] + [row for row in offered if id(row) not in notes_for]
    mts = is_mts_russia(line)
    query, page = "", 0
    while True:
        rows = [row for row in ordered if profile_matches_query(row, query)]
        pages = max(1, (len(rows) + PROFILE_PAGE - 1) // PROFILE_PAGE)
        page = min(page, pages - 1)
        shown = rows[page * PROFILE_PAGE:(page + 1) * PROFILE_PAGE]
        print(f"\nДоступные пакеты для {line_label(line)}")
        print(f"Только версии с настройками для этого iPhone (плата {board}): "
              f"новейшая версия каждого оператора (и проверенная старая, если есть), "
              f"{len(offered)} из {len(profiles)}.")
        if recommended:
            print(f"★ — рекомендуемые для {operator}: они в начале списка, выбирайте по пояснению "
                  "(подробности — в теме на 4PDA).")
        if mts:
            print("Wi-Fi-звонки на МТС дают только профили с пометкой «Wi-Fi-звонки на МТС».")
        print(f"Поиск: {query or 'все'} · найдено {len(rows)} · страница {page + 1}/{pages}")
        for index, row in enumerate(shown, 1):
            notes = []
            if id(row) in notes_for:
                notes.append(f"★ {notes_for[id(row)]}")
            if is_active_row(row, line):
                notes.append("активен")
            if any(str(other["identifier"]) == str(row["identifier"])
                   and _offer_rank(other) > _offer_rank(row) for other in offered):
                notes.append("проверенная старая версия" if row.get("tested", True) else "старая версия")
            elif row.get("legacy"):
                notes.append("старая версия")
            if mts and mts_wifi_calling(row, line):
                notes.append("Wi-Fi-звонки на МТС")
            if row.get("compatibility"):
                notes.append(str(row["compatibility"]))
            suffix = " · " + ", ".join(notes) if notes else ""
            print(f"  {index:2}. {row['label']}{suffix}")
        if not rows and query:
            hidden = [row for row in profiles if profile_matches_query(row, query)]
            if hidden:
                print(f"  Для этого iPhone подходящих версий нет. Есть {len(hidden)} для других моделей "
                      f"iPhone — на плату {board} они не рассчитаны, и программа их не устанавливает.")
        hint = mts_telia_hint(offered, line, query)
        if hint:
            print(hint)
        print("Команды: номер · N/P страницы · текст или /текст — поиск · C очистить · Q отмена")
        raw = input("Пакет или поиск: ").strip()
        if raw.casefold() == "q":
            return None
        if raw.casefold() == "n":
            page = min(page + 1, pages - 1)
        elif raw.casefold() == "p":
            page = max(page - 1, 0)
        elif raw.casefold() == "c":
            query, page = "", 0
        elif raw.startswith("/"):
            query, page = raw[1:].strip(), 0
        elif raw.isdigit() and 1 <= int(raw) <= len(shown):
            choice_warnings(shown[int(raw) - 1], line)
            return shown[int(raw) - 1]
        elif raw.isdigit() and shown:
            print(f"Нет пакета с номером {raw} на этой странице (1–{len(shown)}). "
                  "Чтобы искать цифры, введите /" + raw)
        elif raw:
            query, page = raw, 0
        else:
            print("Неверный выбор.")


def _expected_serials(items: list[dict[str, object]]) -> set[str]:
    return {str(item["state"].get("udid")) for item in items if item["state"].get("udid")}


def _line_for(overview: dict[str, object], manifest: dict[str, object]) -> dict[str, object] | None:
    for line in overview.get("lines", []):
        if isinstance(line, dict) and _state_matches_manifest(line, manifest):
            return line
    return None


def _is_active(line: dict[str, object] | None, desired: dict[str, object]) -> bool:
    if not line or line.get("bundle_identifier") != desired["bundle_identifier"]:
        return False
    try:
        return version_tuple(line.get("bundle_version")) >= version_tuple(desired["bundle_version"])
    except BuildError:
        return False


async def wait_for_plan(
    items: list[dict[str, object]], timeout: float | None = None,
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    """Poll after the restart.  Returns (verified, last_seen) keyed by selector hash."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (VERIFY_TIMEOUT if timeout is None else timeout)
    serials = _expected_serials(items)
    verified: dict[str, dict[str, object]] = {}
    last_seen: dict[str, dict[str, object]] = {}
    while loop.time() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            overview = await asyncio.wait_for(read_states(), 25)
        except (BuildError, OSError, RuntimeError, asyncio.TimeoutError, PyMobileDevice3Exception):
            continue
        if serials and str(overview.get("udid", "")) not in serials:
            continue  # another phone is attached; keep waiting for ours
        for item in items:
            key = str(item["manifest"]["selector_sha256"])
            line = _line_for(overview, item["manifest"])
            if line is not None:
                last_seen[key] = line
            if _is_active(line, item["entry"]):
                verified[key] = line  # type: ignore[assignment]
        if len(verified) == len(items):
            break
    return verified, last_seen


async def wait_for_reboot(serials: set[str], timeout: float | None = None) -> bool:
    """True once the phone has left usbmux, i.e. the restart really started.

    Verifying before that could read the pre-reboot state and report success
    that iOS later discards during the reboot.
    """
    from pymobiledevice3.usbmux import list_devices

    loop = asyncio.get_running_loop()
    deadline = loop.time() + (REBOOT_DOWN_TIMEOUT if timeout is None else timeout)
    while loop.time() < deadline:
        try:
            devices = await asyncio.wait_for(list_devices(), 10)
        except (OSError, asyncio.TimeoutError, PyMobileDevice3Exception):
            return True  # usbmux itself lost the phone
        present = [
            device for device in devices if getattr(device, "connection_type", "") == "USB"
            and (not serials or str(getattr(device, "serial", "")) in serials)
        ]
        if not present:
            return True
        await asyncio.sleep(min(1.0, POLL_INTERVAL))
    return False


async def wait_for_selected_lines(items: list[dict[str, object]], timeout: float | None = None) -> bool:
    """Wait for reconnection between two distinct one-shot sends; never resend."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (RECONNECT_TIMEOUT if timeout is None else timeout)
    serials = _expected_serials(items)
    while loop.time() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            overview = await asyncio.wait_for(read_states(), 25)
        except (BuildError, OSError, RuntimeError, asyncio.TimeoutError, PyMobileDevice3Exception):
            continue
        if serials and str(overview.get("udid", "")) not in serials:
            continue
        if all(_line_for(overview, item["manifest"]) is not None for item in items):
            return True
    return False


def _state_matches_manifest(state: dict[str, object], manifest: dict[str, object]) -> bool:
    try:
        check_state(state, manifest, "forward")
        return True
    except BuildError:
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_profile(line: dict[str, object] | None) -> str:
    if not line:
        return "линия не найдена"
    return f"{carrier_name(line)} {line.get('bundle_version', '')}".strip()


def _receipt_started(path: Path, receipt: dict[str, object]) -> float:
    try:
        return datetime.fromisoformat(str(receipt.get("started_utc", ""))).timestamp()
    except (TypeError, ValueError):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0


def latest_receipt(batch_key: str) -> dict[str, object]:
    newest: tuple[float, dict[str, object]] = (-1.0, {})
    for path in state_root().glob(f"{batch_key}-*install-result.json"):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(receipt, dict):
            newest = max(newest, (_receipt_started(path, receipt), receipt), key=lambda item: item[0])
    return newest[1]


def confirm_retry(previous: dict[str, object], items: list[dict[str, object]]) -> bool:
    """A previous identical attempt was never confirmed: explain and ask explicitly."""
    phase = str(previous.get("phase", ""))
    when = str(previous.get("started_utc", ""))[:16].replace("T", " ")
    print("\nВНИМАНИЕ: такой же план уже запускался" + (f" ({when} UTC)" if when else "")
          + " и не был подтверждён.")
    if phase:
        print(f"  Последний этап: {PHASE_RU.get(phase, phase)}.")
    print("  Сейчас на iPhone:")
    for item in items:
        print(f"    {slot_label(item['state'].get('slot'))}: {_short_profile(item['state'])}"
              f" (нужен {item['profile']['label']})")
    print("Если после той попытки iPhone уже перезагружался, а нужный профиль так и не")
    print("стал активным, пакет можно отправить ещё раз. Если iPhone не перезагружался —")
    print("ответьте Q, перезагрузите его один раз и проверьте профили пунктом 4.")
    answer = input("Для повторной отправки введите RETRY (или Q): ").strip()
    log("retry_prompt", phase=phase, accepted=answer == "RETRY")
    return answer == "RETRY"


async def execute_plan(
    plan: list[tuple[dict[str, object], dict[str, object], Path]],
    *, remember_original: bool = True, operation: str = "multi-line-carrier-install",
) -> int:
    fresh = await read_states()
    log_overview(fresh)
    items = []
    for original, profile, package_dir in plan:
        if (original.get("udid") and fresh.get("udid")
                and str(original.get("udid")) != str(fresh.get("udid"))):
            raise BuildError("после подтверждения подключён другой iPhone; установка не выполнялась")
        manifest = load_manifest(package_dir)
        matches = [
            line for line in fresh.get("lines", [])
            if _state_matches_manifest(line, manifest)
        ]
        if len(matches) != 1:
            raise BuildError(
                f"{slot_label(original.get('slot'))} не найдена по точному селектору SIM; "
                "установка не выполнялась"
            )
        state = matches[0]
        if remember_original:
            # Installation (not a return to the original): re-check the package
            # against the iPhone that is connected right now.
            require_suitable(profile, state)
        payload = inspect_package(package_dir, "forward", manifest)
        entry = package_entry_for(manifest, "forward")
        if _is_active(state, entry):
            current_version = str(state.get("bundle_version", ""))
            if current_version and current_version != str(entry["bundle_version"]):
                print(f"{slot_label(state.get('slot'))}: уже активна более новая версия "
                      f"{carrier_name(state)} {current_version}; установка {profile['label']} не нужна.")
            else:
                print(f"{slot_label(state.get('slot'))}: {profile['label']} уже активен; установка не нужна.")
            continue
        if remember_original:
            refuse_downgrade_over_own_package(state, entry.get("bundle_identifier"),
                                              entry.get("bundle_version"), profile.get("label"))
        items.append({"state": state, "profile": profile, "manifest": manifest,
                      "payload": payload, "entry": entry})
    if not items:
        print("Все выбранные профили уже активны; телефон не изменён.")
        return 0

    if remember_original:
        for item in items:
            remember_baseline(item["state"])

    material = [(item["manifest"]["selector_sha256"], item["entry"]["sha256"]) for item in items]
    batch_key = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]
    marker = state_root() / f".{batch_key}-install.once"
    receipt_path = state_root() / f"{batch_key}-install-result.json"
    if marker.exists():
        # v5.5 refused here forever, with no way to clear the marker from the
        # menu.  Now the user sees the previous attempt and decides.
        if not confirm_retry(latest_receipt(batch_key), items):
            print("Отменено; телефон не изменён.")
            return 0
        marker.unlink(missing_ok=True)
    if receipt_path.exists():
        # Keep earlier receipts: they are evidence of the SIM's original profile.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        receipt_path = state_root() / f"{batch_key}-{stamp}-install-result.json"
    receipt: dict[str, object] = {
        "operation": operation, "started_utc": _now(),
        "phase": "checked", "before": [safe_state(item["state"]) for item in items],
        "desired": [{"slot": item["state"].get("slot"),
                     "bundle_identifier": item["entry"]["bundle_identifier"],
                     "bundle_version": item["entry"]["bundle_version"]} for item in items],
        "selector_hashes": [item["manifest"]["selector_sha256"] for item in items],
        "identifiers_recorded": False, "app_version": APP_VERSION,
    }
    marker.write_text("Confirmed carrier installation sent; verify state before retry.\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(marker, 0o600)
    write_receipt(receipt_path, receipt)
    log("plan", operation=operation, desired=receipt["desired"])

    sent: list[dict[str, object]] = []        # transfer finished or ambiguous: never resend
    uncertain: list[dict[str, object]] = []
    rejected: list[tuple[dict[str, object], str]] = []
    not_sent: list[tuple[dict[str, object], str]] = []

    def save(phase: str, **extra: object) -> None:
        receipt["phase"] = phase
        receipt["sent_selector_hashes"] = [i["manifest"]["selector_sha256"] for i in sent]
        if uncertain:
            receipt["uncertain_selector_hashes"] = [i["manifest"]["selector_sha256"] for i in uncertain]
        if rejected:
            receipt["rejected"] = [{"slot": i["state"].get("slot"), "reason": scrub(r)} for i, r in rejected]
        if not_sent:
            receipt["not_sent"] = [{"slot": i["state"].get("slot"), "reason": scrub(r)} for i, r in not_sent]
        receipt.update(extra)
        write_receipt(receipt_path, receipt)

    try:
        for index, item in enumerate(items):
            label = slot_label(item["state"].get("slot"))
            if sent:
                print("Проверяю соединение перед следующей SIM...")
                if not await wait_for_selected_lines(items):
                    reason = "соединение с iPhone не восстановилось после предыдущей SIM"
                    for rest in items[index:]:
                        not_sent.append((rest, reason))
                    print(f"{label}: пакет не отправлен — {reason}.")
                    break
            print(f"Установка: {label} → {item['profile']['label']}...")
            try:
                await send_once(
                    item["payload"],
                    expected_serial=str(item["state"].get("udid", "")) or None,
                )
                sent.append(item)
                log("send", slot=item["state"].get("slot"), result="completed")
            except InstallRejectedError as exc:
                rejected.append((item, str(exc)))
                log("send", slot=item["state"].get("slot"), result="rejected", reason=str(exc))
                print(f"{label}: {exc}")
            except (InstallResultUnknownError, ConnectionTerminatedError) as exc:
                # installation_proxy may close its socket while CommCenter is
                # applying an already-transferred IPCC.  The result is
                # ambiguous, so never resend: reboot once and verify instead.
                sent.append(item)
                uncertain.append(item)
                log("send", slot=item["state"].get("slot"), result="uncertain", reason=str(exc))
                print("Соединение службы закрылось после отправки. Повторной отправки не будет.")
            except BuildError as exc:
                # PreTransferConnectionError and device-selection errors happen
                # before any byte of the package reached the phone.
                reason = explain_error(exc)
                for rest in items[index:]:
                    not_sent.append((rest, reason))
                log("send", slot=item["state"].get("slot"), result="not_sent", reason=str(exc))
                print(f"{label}: пакет не отправлен — {reason}")
                break
            save("partially_installed" if len(sent) < len(items) else "installed")
    except BaseException as exc:
        save("send_failed" if not sent else "partially_installed", finished_utc=_now(),
             error=scrub(f"{type(exc).__name__}: {exc}"))
        raise

    if not sent:
        # Nothing reached CommCenter: a later attempt is safe without RETRY.
        save("rejected" if rejected and not not_sent else "not_sent_connection_failed",
             finished_utc=_now())
        marker.unlink(missing_ok=True)
        print("\nНичего не установлено; перезагрузка не нужна.")
        for item, reason in rejected + not_sent:
            print(f"  {slot_label(item['state'].get('slot'))}: {reason}")
        return 2

    save("sent_unconfirmed" if uncertain else "installed")
    if uncertain:
        print("Жду восстановления USB-службы перед единственной перезагрузкой...")
        if not await wait_for_selected_lines(sent, timeout=AFTER_SEND_TIMEOUT):
            save("awaiting_manual_restart", finished_utc=_now())
            print("USB-служба пока недоступна. Перезагрузите iPhone вручную один раз, разблокируйте его")
            print("и выберите пункт 4 для проверки. Установку повторять не нужно.")
            return 3
    print("Пакеты отправлены. Перезагружаю iPhone один раз...")
    try:
        await restart_once(expected_serial=str(sent[0]["state"].get("udid", "")) or None)
    except (RestartResultUnknownError, ConnectionTerminatedError):
        # Diagnostics connection normally disappears as the restart begins.
        print("Команда перезагрузки принята; соединение с iPhone закрылось.")
    except (PreTransferConnectionError, RestartRefusedError) as exc:
        save("awaiting_manual_restart", finished_utc=_now(), error=scrub(str(exc)))
        log("restart", result="not_done", reason=str(exc))
        if isinstance(exc, RestartRefusedError):
            print("iPhone не принял команду перезагрузки.")
        else:
            print("iPhone не появился для команды перезагрузки.")
        print("Перезагрузите его вручную один раз, разблокируйте и выберите пункт 4.")
        print("Пакеты повторно отправлять не нужно.")
        return 3
    save("restart_requested")
    rebooted = await wait_for_reboot(_expected_serials(sent))
    log("restart", result="observed" if rebooted else "not_observed")
    if rebooted:
        print("iPhone перезагружается. Разблокируйте его, когда он включится...")
    else:
        print("iPhone не отключился после команды перезагрузки; проверяю текущее состояние...")
    verified, last_seen = await wait_for_plan(sent, timeout=None if rebooted else POLL_INTERVAL * 3)
    receipt["after"] = [safe_state(state) for state in verified.values()]
    missing = [item for item in sent if str(item["manifest"]["selector_sha256"]) not in verified]
    log("verify", verified=len(verified), expected=len(sent),
        seen=[_short_profile(line) for line in last_seen.values()])
    if not rebooted:
        save("restart_not_observed", finished_utc=_now())
        if not missing:
            marker.unlink(missing_ok=True)
        print("ВНИМАНИЕ: перезагрузка не обнаружена, поэтому результат не окончательный.")
        for item in sent:
            seen = last_seen.get(str(item["manifest"]["selector_sha256"]))
            print(f"  {slot_label(item['state'].get('slot'))}: сейчас "
                  f"{_short_profile(seen) if seen else 'iPhone не ответил'}")
        print("Перезагрузите iPhone вручную один раз, разблокируйте и проверьте пунктом 4.")
        print("Пакеты повторно отправлять не нужно.")
        return 3
    if verified:
        print("После перезагрузки подтверждены:")
        for state in verified.values():
            print(f"  {slot_label(state.get('slot'))}: {_short_profile(state)}")
    if missing:
        save("unconfirmed_after_restart" if not verified else "partially_verified", finished_utc=_now())
        print("НЕ ПОДТВЕРЖДЕНО (повторной отправки не было):")
        for item in missing:
            seen = last_seen.get(str(item["manifest"]["selector_sha256"]))
            print(f"  {slot_label(item['state'].get('slot'))}: нужен {item['profile']['label']}, "
                  f"сейчас {_short_profile(seen) if seen else 'iPhone не ответил'}")
        print("Если iPhone был заблокирован, разблокируйте его и проверьте пунктом 4.")
        print("Если iOS оставила прежний профиль, повторный запуск этого же плана")
        print("предложит отправить пакет ещё раз (подтверждение RETRY).")
    else:
        save("verified" if not (rejected or not_sent) else "partially_verified", finished_utc=_now())
        marker.unlink(missing_ok=True)
    for item, reason in rejected + not_sent:
        print(f"  {slot_label(item['state'].get('slot'))}: не установлено — {reason}")
    if not missing and not rejected and not not_sent:
        print("Готово.")
        return 0
    return 3


def current_profiles(catalog: dict[str, object]) -> None:
    overview = asyncio.run(read_states())
    log_overview(overview)
    show_phone(overview, catalog)
    print("Проверка завершена; запись не выполнялась.")


def show_apn_help() -> None:
    print("\n" + APN_HELP_RU)


def audit_catalog(catalog: dict[str, object]) -> None:
    entries = [catalog["revert"]]
    for family in catalog["families"]:
        entries.extend(family.get("donors", []))
    for root in catalog_roots(catalog):
        entries.extend(catalog_profiles({"name": root, "catalog_root": root}))
    seen: set[str] = set()
    failures: list[str] = []
    for entry in entries:
        if entry["path"] in seen:
            continue
        seen.add(entry["path"])
        try:
            audit_entry(entry)
        except Exception as exc:
            failures.append(f"{entry.get('label', entry['path'])}: {exc}")
            print(f"  ОШИБКА: {failures[-1]}")
        if len(seen) % 100 == 0:
            print(f"  Проверено: {len(seen)}")
    log("audit", checked=len(seen), failed=len(failures))
    if failures:
        raise BuildError(f"проверено {len(seen)}, не прошли проверку: {len(failures)}; телефон не изменён")
    print(f"Проверено профилей: {len(seen)}, все подписи Apple в порядке. Телефон не изменён.")


def already_active(line: dict[str, object], profile: dict[str, object], *, restore: bool = False) -> bool:
    """Say so at once (before any confirmation) when nothing has to be sent."""
    desired = {"bundle_identifier": profile.get("identifier"), "bundle_version": profile.get("version")}
    if not _is_active(line, desired):
        return False
    slot = slot_label(line.get("slot"))
    if str(line.get("bundle_version", "")) != str(profile.get("version", "")):
        if restore:
            print(f"{slot}: уже активна более новая версия {carrier_name(line)} "
                  f"{line.get('bundle_version', '')}; возврат не нужен.")
        else:
            print(f"{slot}: уже активна более новая версия {carrier_name(line)} "
                  f"{line.get('bundle_version', '')}, {profile['label']} не ставится. Чтобы перейти "
                  "на более старую версию того же оператора, сначала верните исходный профиль "
                  "(пункт 3), затем установите её.")
    else:
        print(f"{slot}: {profile['label']} уже активен; установка не нужна.")
    return True


def build_plan(
    profiles: list[dict[str, object]], chosen_lines: list[dict[str, object]],
) -> list[tuple[dict[str, object], dict[str, object], Path]] | None:
    plan = []
    for line in chosen_lines:
        profile = choose_profile(profiles, line)
        if profile is None:
            return None
        if already_active(line, profile):
            continue
        require_suitable(profile, line)
        refuse_downgrade_over_own_package(line, profile.get("identifier"), profile.get("version"),
                                          profile.get("label"))
        audit_entry(profile)
        forward = prepare_package(profile, line)
        original = remember_baseline(line, allow_current=True)
        if original is None:
            raise BuildError(f"для {slot_label(line.get('slot'))} не удалось сохранить исходный профиль")
        rollback_profile = exact_profile(profiles, original)
        if rollback_profile is None:
            rollback_profile = compatible_original_profile(profiles, original)
        if rollback_profile is None:
            name = str(original.get("bundle_identifier", "?")).removeprefix("com.apple.")
            raise BuildError(
                f"для {slot_label(line.get('slot'))} нельзя заранее подготовить откат "
                f"{name} {original.get('bundle_version', '')}; установка не начата"
            )
        audit_entry(rollback_profile)
        rollback_dir = prepare_package(rollback_profile, line)
        save_rollback_package(line, original, rollback_profile, rollback_dir)
        if (str(rollback_profile.get("identifier")) != str(original.get("bundle_identifier"))
                or str(rollback_profile.get("version")) != str(original.get("bundle_version"))):
            print(
                f"{slot_label(line.get('slot'))}: точного исходного пакета "
                f"{str(original.get('bundle_identifier', '?')).removeprefix('com.apple.')} "
                f"{original.get('bundle_version', '')} нет; для безопасного отката подготовлен "
                f"{rollback_profile['label']}."
            )
        else:
            print(f"{slot_label(line.get('slot'))}: обратный пакет {rollback_profile['label']} подготовлен.")
        plan.append((line, profile, forward))
    return plan


def confirm_plan(
    plan: list[tuple[dict[str, object], dict[str, object], Path]], word: str,
    *, remember_original: bool, operation: str,
) -> int:
    print("\nПлан:")
    for line, profile, _ in plan:
        warning = " · старая версия" if profile.get("legacy") else ""
        print(f"  {slot_label(line.get('slot'))} → {profile['label']}{warning}")
    foreign = [line for line, _, _ in plan if str(line.get("mcc", "")) not in {"250", "257"}]
    if foreign:
        print("\nВНИМАНИЕ: выбрана SIM не России/Беларуси:")
        for line in foreign:
            print(f"  {line_label(line)}")
        if input("Чтобы подтвердить выбор иностранной SIM, введите FOREIGN: ").strip() != "FOREIGN":
            print("Отменено; телефон не изменён.")
            return 0
    if input(f"Для одной установки на линию и одной перезагрузки введите {word}: ").strip() != word:
        print("Отменено; телефон не изменён.")
        return 0
    return asyncio.run(execute_plan(
        plan, remember_original=remember_original, operation=operation,
    ))


MIN_SUPPORTED_IOS = (26, 6)


def require_supported_ios(overview: dict[str, object]) -> None:
    """Profiles and rollback packages exist only for iOS 26.6 and later."""
    phone_ios = _ios_tuple(overview.get("version"))
    if phone_ios and phone_ios < MIN_SUPPORTED_IOS:
        raise BuildError(
            f"на этом iPhone iOS {overview.get('version')}, а программа работает с iOS 26.6 и новее. "
            "Для более старой iOS в программе нет профилей и исходного пакета для отката. "
            "Обновите iOS до 26.6 или новее и запустите установку снова; телефон не изменён"
        )


def install_interactive(catalog: dict[str, object], *, both: bool = False) -> int:
    overview = asyncio.run(read_states())
    log_overview(overview)
    family, lines = show_phone(overview, catalog)
    require_model(overview)
    require_supported_ios(overview)
    print("Подбираю профили для этой модели…", flush=True)
    profiles = profiles_for(catalog, family, overview)
    usable = [line for line in lines if valid_line(line)]
    if both:
        if len(usable) != 2:
            raise BuildError("для одновременной привязки нужны две доступные SIM-линии")
        chosen_lines = usable
        print("\nВыбраны обе SIM. Пакет задаётся отдельно для каждой линии.")
    else:
        chosen_lines = choose_lines(lines)
    if not chosen_lines:
        print("Отменено; телефон не изменён.")
        return 0
    plan = build_plan(profiles, chosen_lines)
    if plan is None:
        print("Отменено; телефон не изменён.")
        return 0
    if not plan:
        print("Телефон не изменён.")
        return 0
    return confirm_plan(
        plan, "INSTALL", remember_original=True,
        operation="dual-line-carrier-install" if both else "carrier-install",
    )


def restore_interactive(catalog: dict[str, object]) -> int:
    overview = asyncio.run(read_states())
    log_overview(overview)
    family, lines = show_phone(overview, catalog)
    print("Подбираю профили для этой модели…", flush=True)
    profiles = profiles_for(catalog, family, overview)
    chosen_lines = choose_lines(lines)
    if not chosen_lines:
        print("Отменено; телефон не изменён.")
        return 0
    plan = []
    for line in chosen_lines:
        original = remember_baseline(line, allow_current=False)
        if original is None:
            raise BuildError(
                f"для {slot_label(line.get('slot'))} на этом компьютере не сохранён исходный пакет; "
                "без него безопасный возврат невозможен. Возможные причины: установка "
                "делалась на другом компьютере, либо SIM/eSIM перевыпускалась (у неё новый "
                "IMSI/ICCID, и старый профиль к ней не относится)"
            )
        profile = exact_profile(profiles, original)
        exact = profile is not None
        if profile is None:
            profile = compatible_original_profile(profiles, original)
        cached = cached_rollback_package(line, original)
        if cached is not None and (profile is None or _version_key(cached[0]["version"])
                                   >= _version_key(profile["version"])):
            # A package prepared earlier is used only while the catalog has
            # nothing closer to the original (v5.7 kept VimpelCom 72 for a SIM
            # whose own profile is 73; v6 ships the exact 73).
            if not already_active(line, cached[0], restore=True):
                plan.append((line, cached[0], cached[1]))
            continue
        if cached is not None:
            log("rollback_cache_superseded", cached=cached[0]["version"], catalog=profile["version"])
        if profile is None:
            name = str(original.get("bundle_identifier", "?")).removeprefix("com.apple.")
            raise BuildError(
                f"для {slot_label(line.get('slot'))} нет совместимого подписанного исходного "
                f"пакета {name} {original.get('bundle_version', '')}; телефон не изменён"
            )
        if already_active(line, profile, restore=True):
            continue
        if not exact:
            print(
                f"{slot_label(line.get('slot'))}: точной версии "
                f"{original.get('bundle_version', '')} нет во встроенном каталоге; "
                f"будет отправлен Apple-подписанный {profile['label']}. "
                "Штатная версия той же линии может восстановиться автоматически."
            )
        audit_entry(profile)
        package_dir = prepare_package(profile, line)
        save_rollback_package(line, original, profile, package_dir)
        plan.append((line, profile, package_dir))
    if not plan:
        print("Исходные профили уже активны; телефон не изменён.")
        return 0
    print("\nБудет возвращён исходный пакет каждой выбранной SIM.")
    print("Системная служебная запись может остаться, но выбранный сторонний пакет будет заменён исходным.")
    return confirm_plan(
        plan, "RESTORE", remember_original=False, operation="carrier-baseline-restore",
    )


# ---- v6.2: item 3 — back to the carrier profile built into iOS -------------
#
# Verified on iPhone 15 Pro / iOS 27.2.1 with the CommCenter log:
#   * a package whose carrier.plist has an EMPTY SupportedSIMs makes CommCenter
#     remove the user package bound to this SIM ("Deleting Bundle .../Carrier
#     Bundles/iPhone/<selector>"), then reject the new one ("No symlinks
#     available", kInstallResult=false).  After one reboot the SIM runs on
#     /System/Library/Carrier Bundles — the factory state, updated with iOS;
#   * sent to a SIM without a user package it changes nothing;
#   * only a broken signature (SupportedSIMs kept) is NOT enough: iOS stores
#     the package in the binding and merely ignores it ("Verification is
#     failing"), so the SIM stays bound to a dead package.
# installation_proxy reports success either way; the real answer is read from
# the iPhone's live CommCenter log while the package is sent.

RESET_LOG_WAIT = 15.0      # seconds to wait for CommCenter's answer after the upload
RESET_LOG_SETTLE = 1.5     # let the log stream start before the upload


@functools.lru_cache(maxsize=None)
def _native_index(root_name: str) -> dict[str, tuple[tuple[str, str, str], ...]]:
    """MCC+MNC -> ((identifier, version, path), ...) for one full Apple firmware set."""
    found: dict[str, list[tuple[str, str, str]]] = {}
    root = bundle_path(root_name)
    if not root.is_dir():
        return {}
    for directory in sorted(root.glob("*.bundle"), key=lambda path: path.name.casefold()):
        if (directory.stem in {"Default", "DefaultBCAM", "OtherKnown"}
                or directory.name.startswith("._") or not directory.is_dir()):
            continue
        try:
            info = plistlib.loads((directory / "Info.plist").read_bytes())
            carrier = plistlib.loads((directory / "carrier.plist").read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        identifier, version = info.get("CFBundleIdentifier"), info.get("CFBundleVersion")
        sims = carrier.get("SupportedSIMs") if isinstance(carrier, dict) else None
        if not isinstance(identifier, str) or not isinstance(version, str) or not isinstance(sims, list):
            continue
        for code in sims:
            if re.fullmatch(r"[0-9]{5,6}", str(code)):
                found.setdefault(str(code), []).append((identifier, version, f"{root_name}/{directory.name}"))
    return {code: tuple(rows) for code, rows in found.items()}


def native_profile(catalog: dict[str, object], line: dict[str, object]) -> dict[str, object] | None:
    """The carrier profile iOS itself picks for this SIM's network (by MCC+MNC)."""
    family = matching_family(catalog, line)
    mcc, mnc = str(line.get("mcc", "")), str(line.get("mnc", ""))
    if not family or not mcc.isdigit() or not mnc.isdigit():
        return None
    codes = list(dict.fromkeys([mcc + mnc, mcc + mnc.zfill(2), mcc + mnc.zfill(3)]))
    phone_ios = _ios_tuple(line.get("version"))
    tags = catalog.get("root_ios", {})
    roots = [family.get("catalog_root", "")] + list(family.get("extra_roots", []))
    if phone_ios:
        def root_sort(r: object) -> tuple[bool, tuple[int, ...]]:
            ios = next((_ios_tuple(release) for prefix, release in tags.items() if str(r).startswith(prefix)), (27, 2))
            return (ios <= phone_ios, ios)
        roots = sorted(roots, key=root_sort, reverse=True)
    for root in roots:
        if not root:
            continue
        index = _native_index(str(root))
        for code in codes:
            rows = index.get(code, ())
            if len(rows) == 1:
                identifier, version, path = rows[0]
                return {
                    "key": "native-" + hashlib.sha256(path.encode()).hexdigest()[:12],
                    "path": path, "identifier": identifier, "version": version,
                    "label": f"{identifier.removeprefix('com.apple.')} {version}", "legacy": False,
                }
    return None


def reset_donor(catalog: dict[str, object], line: dict[str, object],
                native: dict[str, object] | None) -> dict[str, object]:
    """Signed bundle whose copy (with SupportedSIMs emptied) is sent.  CommCenter
    rejects it whatever it is; the SIM's own carrier is used so that even an
    iOS that accepted it would end up on the SIM's own settings."""
    if native is not None:
        return native
    family = matching_family(catalog, line)
    profiles = profiles_for(catalog, family, line)
    wanted = []
    original = load_baseline(line)
    if original:
        wanted.append(str(original.get("bundle_identifier")))
    wanted.append(str(line.get("bundle_identifier", "")))
    for identifier in wanted:
        rows = [row for row in profiles if str(row.get("identifier")) == identifier]
        if rows:
            return max(rows, key=lambda row: (is_suitable(row), _version_key(row.get("version"))))
    return dict(catalog["revert"], key="mts73-common", legacy=False)


def _note_reset_message(message: str, selector: str, evidence: dict[str, object]) -> None:
    if "Install bundle:" in message:
        evidence["install_seen"] = True
    if "Deleting Bundle" in message and f"Carrier Bundles/iPhone/{selector}" in message:
        evidence["removed"] = True
    if "No symlinks available" in message:
        evidence["no_symlinks"] = True
    if "Verification is failing" in message and selector in message:
        evidence["kept_invalid"] = True
    match = re.search(r'"?kInstallResult"?\s*[:=]\s*"?(true|false|1|0)', message)
    if match and "result" not in evidence:
        evidence["result"] = match.group(1) in {"true", "1"}


async def watch_reset_answer(serial: str | None, selector: str, started: asyncio.Event,
                             answered: asyncio.Event, evidence: dict[str, object]) -> None:
    """Read CommCenter's live log while the reset package is sent.  Never raises:
    without the log the result is still checked by the profile after reboot."""
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.os_trace import OsTraceService

        lockdown = await asyncio.wait_for(
            create_using_usbmux(serial=serial, connection_type="USB", autopair=False), 20)
        try:
            async with OsTraceService(lockdown) as service:
                pids = await asyncio.wait_for(service.get_pid_list(), 15)
            payload = (pids or {}).get("Payload") or {} if isinstance(pids, dict) else {}
            pid = next((int(p) for p, v in payload.items()
                        if isinstance(v, dict) and v.get("ProcessName") == "CommCenter"), -1)
            async with OsTraceService(lockdown) as service:
                evidence["log"] = True
                started.set()
                async for entry in service.syslog(pid=pid):
                    _note_reset_message(str(getattr(entry, "message", "")), selector, evidence)
                    if "result" in evidence:
                        answered.set()
                        break
        finally:
            with contextlib.suppress(Exception):
                await lockdown.close()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # the log is evidence, not a requirement
        evidence["log_error"] = type(exc).__name__
    finally:
        started.set()


def _reset_line(overview: dict[str, object], digest: str) -> dict[str, object] | None:
    for line in overview.get("lines", []):
        try:
            if isinstance(line, dict) and selector_digest(line) == digest:
                return line
        except BuildError:
            continue
    return None


def _reset_done(line: dict[str, object] | None, item: dict[str, object]) -> bool:
    if line is None:
        return False
    expected = item.get("expected")
    if not expected:
        return True  # no profile of its own in iOS: judged by the log only
    return _is_active(line, expected)


async def wait_for_reset(items: list[dict[str, object]], timeout: float | None = None,
                         ) -> tuple[set[str], dict[str, dict[str, object]]]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (VERIFY_TIMEOUT if timeout is None else timeout)
    serials = _expected_serials(items)
    done: set[str] = set()
    seen: dict[str, dict[str, object]] = {}
    while loop.time() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            overview = await asyncio.wait_for(read_states(), 25)
        except (BuildError, OSError, RuntimeError, asyncio.TimeoutError, PyMobileDevice3Exception):
            continue
        if serials and str(overview.get("udid", "")) not in serials:
            continue
        for item in items:
            line = _reset_line(overview, item["digest"])
            if line is not None:
                seen[item["digest"]] = line
            if _reset_done(line, item):
                done.add(item["digest"])
        if len(done) == len(items):
            break
    return done, seen


async def execute_reset(items: list[dict[str, object]]) -> int:
    fresh = await read_states()
    log_overview(fresh)
    for item in items:
        if (item["state"].get("udid") and fresh.get("udid")
                and str(item["state"].get("udid")) != str(fresh.get("udid"))):
            raise BuildError("после подтверждения подключён другой iPhone; телефон не изменён")
        if _reset_line(fresh, item["digest"]) is None:
            raise BuildError(f"{slot_label(item['state'].get('slot'))} не найдена по точному "
                             "селектору SIM; телефон не изменён")
    sent: list[dict[str, object]] = []
    not_sent: list[tuple[dict[str, object], str]] = []
    for index, item in enumerate(items):
        label = slot_label(item["state"].get("slot"))
        if sent:
            print("Проверяю соединение перед следующей SIM...")
            if not await wait_for_selected_lines_by_digest(items):
                reason = "соединение с iPhone не восстановилось после предыдущей SIM"
                for rest in items[index:]:
                    not_sent.append((rest, reason))
                print(f"{label}: пакет не отправлен — {reason}.")
                break
        serial = str(item["state"].get("udid", "")) or None
        evidence: dict[str, object] = {}
        item["evidence"] = evidence
        started, answered = asyncio.Event(), asyncio.Event()
        watcher = asyncio.create_task(
            watch_reset_answer(serial, item["selector"], started, answered, evidence))
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(started.wait(), 25)
        await asyncio.sleep(RESET_LOG_SETTLE)
        print(f"Сброс: {label} → отправляю пакет сброса...")
        try:
            await send_once(item["payload"], expected_serial=serial)
            sent.append(item)
        except InstallRejectedError as exc:
            # installd refused before CommCenter: nothing was changed.
            not_sent.append((item, str(exc)))
            print(f"{label}: {exc}")
        except (InstallResultUnknownError, ConnectionTerminatedError):
            sent.append(item)  # harmless to repeat, but never resent blindly here
            print("Соединение службы закрылось после отправки. Повторной отправки не будет.")
        except BuildError as exc:
            reason = explain_error(exc)
            for rest in items[index:]:
                not_sent.append((rest, reason))
            print(f"{label}: пакет не отправлен — {reason}")
            watcher.cancel()
            with contextlib.suppress(BaseException):
                await watcher
            break
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(answered.wait(), RESET_LOG_WAIT)
        watcher.cancel()
        with contextlib.suppress(BaseException):
            await watcher
        log("reset_send", slot=item["state"].get("slot"),
            result={True: "accepted", False: "rejected"}.get(evidence.get("result"), "unknown"),
            removed=bool(evidence.get("removed")), no_symlinks=bool(evidence.get("no_symlinks")),
            log_error=evidence.get("log_error", ""))
        if evidence.get("result") is True:
            print(f"ВНИМАНИЕ: {label}: iOS приняла пакет сброса. Так быть не должно — возможно, "
                  "изменилась iOS. Проверьте результат после перезагрузки.")
    if not sent:
        print("\nНичего не отправлено; перезагрузка не нужна.")
        for item, reason in not_sent:
            print(f"  {slot_label(item['state'].get('slot'))}: {reason}")
        return 2

    print("Пакеты сброса отправлены. Перезагружаю iPhone один раз...")
    try:
        await restart_once(expected_serial=str(sent[0]["state"].get("udid", "")) or None)
    except (RestartResultUnknownError, ConnectionTerminatedError):
        print("Команда перезагрузки принята; соединение с iPhone закрылось.")
    except (PreTransferConnectionError, RestartRefusedError) as exc:
        log("restart", result="not_done", reason=str(exc))
        print("iPhone не принял команду перезагрузки." if isinstance(exc, RestartRefusedError)
              else "iPhone не появился для команды перезагрузки.")
        print("Перезагрузите его вручную один раз, разблокируйте и проверьте пунктом 4.")
        print("Повторно отправлять пакет сброса не нужно.")
        return 3
    rebooted = await wait_for_reboot(_expected_serials(sent))
    log("restart", result="observed" if rebooted else "not_observed")
    if rebooted:
        print("iPhone перезагружается. Разблокируйте его, когда он включится...")
    else:
        print("iPhone не отключился после команды перезагрузки; проверяю текущее состояние...")
    done, seen = await wait_for_reset(sent, timeout=None if rebooted else POLL_INTERVAL * 3)
    problems = 0
    print("\nРезультат:")
    for item in sent:
        label = slot_label(item["state"].get("slot"))
        line = seen.get(item["digest"])
        now = _short_profile(line) if line else "iPhone не ответил"
        evidence = item["evidence"]
        log("reset_verify", slot=item["state"].get("slot"), done=item["digest"] in done, after=now,
            rebooted=rebooted)
        removed = evidence.get("result") is False and (evidence.get("removed") or evidence.get("no_symlinks"))
        if evidence.get("result") is True:
            problems += 1
            print(f"  {label}: сейчас {now}. iOS приняла пакет сброса, привязка может остаться.")
            print("    Верните исходный профиль старым способом: пункт 7.")
        elif not rebooted:
            problems += 1
            print(f"  {label}: сейчас {now}. Перезагрузка не обнаружена — перезагрузите iPhone "
                  "вручную и проверьте пунктом 4.")
        elif item["digest"] not in done:
            problems += 1
            print(f"  {label}: НЕ ПОДТВЕРЖДЕНО — нужен встроенный {item['expected_label']}, сейчас {now}.")
            print("    Если iPhone был заблокирован, разблокируйте его и проверьте пунктом 4.")
            print("    Повтор пункта 3 безопасен; запасной путь — пункт 7.")
        elif not item.get("expected"):
            if removed:
                print(f"  {label}: привязка удалена; своего профиля у этой сети в iOS нет, "
                      f"iPhone использует общие настройки iOS (сейчас {now}).")
            else:
                problems += 1
                print(f"  {label}: сейчас {now}. Журнал iPhone не подтвердил удаление привязки.")
        elif removed:
            print(f"  {label}: привязка удалена, работает встроенный профиль iOS {now}.")
        elif evidence.get("log"):
            print(f"  {label}: работает встроенный {now}. Ответа iOS в журнале не видно, но "
                  "профиль правильный.")
        else:
            print(f"  {label}: работает {now} — профиль оператора из iOS. Журнал iPhone прочитать "
                  "не удалось, поэтому удаление привязки подтверждено только по профилю.")
    for item, reason in not_sent:
        problems += 1
        print(f"  {slot_label(item['state'].get('slot'))}: не отправлено — {reason}")
    if problems:
        return 3
    print("Готово. Дальше профиль оператора обновляется вместе с iOS.")
    return 0


async def wait_for_selected_lines_by_digest(items: list[dict[str, object]],
                                            timeout: float | None = None) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (RECONNECT_TIMEOUT if timeout is None else timeout)
    serials = _expected_serials(items)
    while loop.time() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            overview = await asyncio.wait_for(read_states(), 25)
        except (BuildError, OSError, RuntimeError, asyncio.TimeoutError, PyMobileDevice3Exception):
            continue
        if serials and str(overview.get("udid", "")) not in serials:
            continue
        if all(_reset_line(overview, item["digest"]) is not None for item in items):
            return True
    return False


def reset_interactive(catalog: dict[str, object]) -> int:
    overview = asyncio.run(read_states())
    log_overview(overview)
    _family, lines = show_phone(overview, catalog)
    chosen_lines = choose_lines(lines, reset=True)
    if not chosen_lines:
        print("Отменено; телефон не изменён.")
        return 0
    items = []
    for line in chosen_lines:
        native = native_profile(catalog, line)
        donor = reset_donor(catalog, line, native)
        selector = normalized_selector(str(line.get("imsi", "")), str(line.get("iccid", "")))
        payload = build_reset_payload(bundle_path(str(donor["path"])), selector, str(donor["identifier"]))
        expected = None
        expected_label = "общий профиль iOS"
        if native is not None:
            identifier = str(native["identifier"])
            version = native_firmware_version(line, identifier) or ""
            expected = {"bundle_identifier": identifier, "bundle_version": version or "0"}
            expected_label = (f"{identifier.removeprefix('com.apple.')} "
                              f"{version + ' или новее' if version else ''}").strip()
        items.append({"state": line, "selector": selector, "digest": selector_digest(line),
                      "payload": payload, "expected": expected, "expected_label": expected_label})
    print("\nВозврат к профилю, встроенному в iOS (удаление привязки пакета):")
    for item in items:
        line = item["state"]
        note = ""
        if item["expected"] and _is_active(line, item["expected"]):
            note = " · сейчас уже этот оператор; если привязки нет, ничего не изменится"
        target = (f"встроенный профиль iOS {item['expected_label']}" if item["expected"]
                  else "общие настройки iOS (своего профиля у этой сети в iOS нет)")
        print(f"  {line_label(line)} → {target}{note}")
    print("\nКак это работает: на SIM отправляется пакет её оператора с пустым списком SIM.")
    print("iOS удаляет пакет, ранее привязанный к этой SIM, а сам новый пакет не принимает.")
    print("После перезагрузки iPhone работает на профиле, встроенном в iOS, — как до любых")
    print("установок, и дальше этот профиль обновляется вместе с iOS. Другая SIM не затрагивается.")
    if input("Для одной отправки на линию и одной перезагрузки введите RESET: ").strip() != "RESET":
        print("Отменено; телефон не изменён.")
        return 0
    return asyncio.run(execute_reset(items))


def run_menu() -> int:
    catalog = load_catalog()
    log("start", platform=f"{platform.system()} {platform.machine()}", python=platform.python_version())
    last_result = 0
    while True:
        print(f"\nCarrierSelector v.{APP_VERSION}")
        print("1. Привязать пакет: выбрать одну SIM или обе")
        print("2. Обе SIM одновременно: отдельный пакет для каждой")
        print("3. Вернуть профиль, встроенный в iOS (удалить привязку пакета)")
        print("4. Текущие профили всех SIM (без записи)")
        print("5. Проверить встроенные профили (без записи)")
        print("6. Справка: APN и MMS операторов России и Беларуси")
        print("7. Старый способ возврата: поставить сохранённый исходный пакет")
        print("0. Выход")
        try:
            choice = input("Выберите действие: ").strip()
        except EOFError:
            return last_result
        if choice == "0":
            return last_result
        try:
            if choice == "1":
                last_result = install_interactive(catalog)
            elif choice == "2":
                last_result = install_interactive(catalog, both=True)
            elif choice == "3":
                last_result = reset_interactive(catalog)
            elif choice == "4":
                current_profiles(catalog)
            elif choice == "5":
                audit_catalog(catalog)
            elif choice == "6":
                show_apn_help()
            elif choice == "7":
                last_result = restore_interactive(catalog)
            else:
                print("Неизвестный пункт.")
        except EOFError:
            return last_result
        except KeyboardInterrupt:
            last_result = 130
            log("interrupted", action=choice)
            print("\nДействие прервано (Ctrl+C).")
            print("Если отправка пакета уже началась, не повторяйте её вслепую:")
            print("перезагрузите iPhone один раз и проверьте профили пунктом 4.")
        except Exception as exc:
            last_result = 2
            log("error", action=choice, error=f"{type(exc).__name__}: {exc}",
                trace="".join(traceback.format_exception(exc))[-4000:])
            print(f"ОШИБКА: {explain_error(exc)}")
            print("Повторная установка автоматически не выполнялась.")
            print(f"Журнал для разбора (без номеров SIM): {log_path()}")
        try:
            input("\nНажмите Enter, чтобы вернуться в меню...")
        except EOFError:
            return last_result


if __name__ == "__main__":
    try:
        raise SystemExit(run_menu())
    except KeyboardInterrupt:
        print("\nОперация прервана пользователем.", file=sys.stderr)
        raise SystemExit(130)
