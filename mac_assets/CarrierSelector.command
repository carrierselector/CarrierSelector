#!/bin/zsh
set -u

SCRIPT_DIR=${0:A:h}
APP="$SCRIPT_DIR/CarrierSelector"

if [[ ! -x "$APP" ]]; then
  print -u2 "Не найден файл программы: $APP"
  read "?Нажмите Enter для выхода..."
  exit 2
fi

# Archives downloaded by a browser inherit macOS quarantine.  The executable
# is ad-hoc signed and verified during release assembly, but Gatekeeper can
# otherwise report it as "damaged" before it gets a chance to run.
xattr -d com.apple.quarantine "$APP" 2>/dev/null || true

"$APP"
RESULT=$?
print ""
print "CarrierSelector завершён, код: $RESULT"
read "?Нажмите Enter, чтобы закрыть окно..."
exit $RESULT
