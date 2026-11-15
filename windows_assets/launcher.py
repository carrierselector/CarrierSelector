#!/usr/bin/env python3
"""Windows bootstrap with UTF-8 output and readable startup diagnostics."""

import os
import subprocess
import sys


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def apple_service_available() -> bool:
    if os.name != "nt":
        return True
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["sc.exe", "query", "Apple Mobile Device Service"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, creationflags=flags,
        )
    except OSError:
        return False
    return result.returncode == 0


def main() -> int:
    configure_console()
    if not apple_service_available():
        print("ПРЕДУПРЕЖДЕНИЕ: для подключения iPhone не найден компонент Apple.")
        print("Установите официальное приложение Apple Devices или iTunes,")
        print("затем перезагрузите Windows. Справка APN доступна и без драйвера.\n")
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
    except Exception as exc:
        print(f"Не удалось запустить CarrierSelector: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
