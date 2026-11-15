#!/bin/sh
# Linux: ./CarrierSelector.sh  — ищет Python 3.10+ и запускает start.py.
set -u
cd "$(dirname -- "$0")" || exit 2
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    exec "$c" start.py
  fi
done
echo "Не найден Python 3.10 или новее. Установите его, например: sudo apt install python3 python3-venv"
exit 2
