#!/bin/sh
set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2
APP="$SCRIPT_DIR/CarrierSelector"

if [ ! -x "$APP" ]; then
    printf '%s\n' "ERROR: CarrierSelector executable was not found." >&2
    printf '%s\n' "Extract the entire archive and run CarrierSelector.sh again." >&2
    exit 11
fi

if [ ! -S /var/run/usbmuxd ] && [ ! -S /run/usbmuxd ]; then
    printf '%s\n' "ПРЕДУПРЕЖДЕНИЕ: служба usbmuxd не обнаружена."
    printf '%s\n' "Для подключения iPhone установите и запустите usbmuxd."
    printf '\n'
fi

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
exec "$APP"
