#!/usr/bin/env python3
"""CarrierSelector на чистом Python: один запуск для macOS, Windows и Linux.

Нужен только Python 3.10 или новее.  Библиотеки (pymobiledevice3 и
cryptography) при первом запуске ставятся в папку .venv рядом с программой —
системный Python и другие программы не затрагиваются.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

MIN_PYTHON = (3, 10)
MIN_PMD3 = (11, 49)          # проверенные версии pymobiledevice3: 11.49 и 11.55
HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
REQUIREMENTS = HERE / "requirements.txt"
GUARD = "CARRIERSELECTOR_BOOTSTRAPPED"


def configure_console() -> None:
    if os.name == "nt":
        try:  # русский текст в консоли Windows и при вводе
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _version(text: str) -> tuple[int, ...]:
    parts = []
    for part in text.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def dependencies_ready() -> bool:
    from importlib import metadata
    try:
        pmd3 = metadata.version("pymobiledevice3")
        metadata.version("cryptography")
    except metadata.PackageNotFoundError:
        return False
    return _version(pmd3) >= MIN_PMD3


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def running_in_own_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV.resolve()
    except OSError:
        return False


def install_dependencies() -> bool:
    print("Для работы нужны библиотеки pymobiledevice3 и cryptography (около 150 МБ).")
    print("Они скачиваются один раз из PyPI (нужен интернет) в папку .venv рядом")
    print("с программой. Системный Python и другие программы не изменяются.")
    try:
        answer = input("Установить сейчас? Введите Y (или N для выхода): ").strip().casefold()
    except EOFError:
        return False
    if answer not in {"y", "yes", "д", "да"}:
        return False
    python = Path(sys.executable)
    if not running_in_own_venv():
        print("\nСоздаю папку .venv...", flush=True)
        try:
            subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
        except (OSError, subprocess.CalledProcessError):
            print("Не удалось создать папку .venv.")
            if sys.platform.startswith("linux"):
                print("Установите модуль venv, например: sudo apt install python3-venv python3-pip")
            print("Либо установите библиотеки сами: python3 -m pip install -r requirements.txt")
            return False
        python = venv_python()
    print("Устанавливаю библиотеки (это займёт 1–3 минуты)...", flush=True)
    try:
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                        "-r", str(REQUIREMENTS)], check=True)
    except (OSError, subprocess.CalledProcessError):
        print("\nУстановка библиотек не удалась. Проверьте интернет и повторите запуск.")
        return False
    print("Библиотеки установлены.\n")
    return True


def platform_warnings() -> None:
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            found = subprocess.run(["sc.exe", "query", "Apple Mobile Device Service"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   check=False, creationflags=flags).returncode == 0
        except OSError:
            found = False
        if not found:
            print("ПРЕДУПРЕЖДЕНИЕ: для подключения iPhone не найден компонент Apple.")
            print("Установите официальное приложение Apple Devices или iTunes,")
            print("затем перезагрузите Windows. Справка APN доступна и без драйвера.\n")
    elif sys.platform.startswith("linux"):
        if not (Path("/var/run/usbmuxd").exists() or Path("/run/usbmuxd").exists()):
            print("ПРЕДУПРЕЖДЕНИЕ: служба usbmuxd не обнаружена.")
            print("Для подключения iPhone установите и запустите usbmuxd.\n")


def run_app() -> int:
    sys.path.insert(0, str(HERE))
    platform_warnings()
    try:
        import carrier_app
    except Exception as exc:
        print(f"Не удалось загрузить CarrierSelector: {type(exc).__name__}: {exc}")
        return 2
    try:
        return carrier_app.run_menu()
    except KeyboardInterrupt:
        print("\nОперация прервана пользователем.")
        return 130


def main() -> int:
    if sys.version_info < MIN_PYTHON:
        print(f"Нужен Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} или новее, сейчас "
              f"{sys.version.split()[0]}. Установите свежий Python с python.org.")
        return 2
    configure_console()
    if dependencies_ready():
        return run_app()
    # Библиотеки уже стоят в своей .venv: запускаемся через неё (один раз).
    if venv_python().exists() and not running_in_own_venv() and not os.environ.get(GUARD):
        env = dict(os.environ, **{GUARD: "1"})
        sys.stdout.flush()
        return subprocess.run([str(venv_python()), str(Path(__file__).resolve()), *sys.argv[1:]],
                              env=env, check=False).returncode
    if not install_dependencies():
        print("Отменено.")
        return 1
    if running_in_own_venv():
        importlib.invalidate_caches()
        return run_app()
    env = dict(os.environ, **{GUARD: "1"})
    sys.stdout.flush()
    return subprocess.run([str(venv_python()), str(Path(__file__).resolve()), *sys.argv[1:]],
                          env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
