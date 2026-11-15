#!/bin/zsh
# macOS: двойной щелчок. Ищет Python 3.10+ и запускает start.py.
set -u
cd "${0:A:h}" || exit 2
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [[ -z "$PY" ]]; then
  print "Не найден Python 3.10 или новее. Установите его с https://www.python.org/downloads/"
  read "?Нажмите Enter для выхода..."
  exit 2
fi
"$PY" start.py
RESULT=$?
print ""
print "CarrierSelector завершён, код: $RESULT"
read "?Нажмите Enter, чтобы закрыть окно..."
exit $RESULT
