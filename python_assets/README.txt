CarrierSelector v.6.1 — версия на чистом Python (macOS, Windows, Linux)
=====================================================================

Это та же программа, что и в сборках для macOS, Windows и Linux, но без
встроенного Python: только исходный код, полный каталог профилей и один
универсальный запуск start.py. Работает на любой системе, где есть
Python 3.10 или новее, — в том числе на Mac с любым процессором, на Windows
ARM и на Linux ARM (например, Raspberry Pi).

ЧТО НУЖНО

  • Python 3.10 или новее (рекомендуется 3.12–3.14):
      macOS   — установщик с https://www.python.org/downloads/
                (встроенный в macOS python3 версии 3.9 не подходит);
      Windows — установщик с https://www.python.org/downloads/,
                при установке отметьте «Add python.exe to PATH»;
      Linux   — sudo apt install python3 python3-venv python3-pip
                (Fedora: sudo dnf install python3; Arch: sudo pacman -S python).
  • Связь с iPhone по USB:
      macOS   — ничего дополнительно не нужно;
      Windows — официальное приложение Apple Devices или iTunes;
      Linux   — sudo apt install usbmuxd libimobiledevice-utils
                (Fedora: usbmuxd libimobiledevice; Arch: usbmuxd libimobiledevice).
  • Интернет — только при первом запуске, чтобы скачать библиотеки.

ЗАПУСК

  1. Полностью распакуйте архив.
  2. Подключите к компьютеру только один iPhone по USB, разблокируйте его и
     подтвердите «Доверять этому компьютеру».
  3. Запустите программу:
       macOS   — двойной щелчок по CarrierSelector.command
                 (или в Терминале: python3 start.py);
       Windows — двойной щелчок по CarrierSelector.cmd
                 (или в командной строке: py start.py);
       Linux   — в терминале в папке программы: ./CarrierSelector.sh
                 (или: python3 start.py).
  4. При первом запуске программа предложит установить библиотеки
     pymobiledevice3 и cryptography — введите Y. Они ставятся в папку .venv
     внутри папки программы (около 150 МБ), системный Python не меняется.
     Следующие запуски — сразу, без интернета.
  5. Дальше всё как в обычной версии: выберите SIM и профиль, проверьте план,
     введите INSTALL и не отключайте кабель до результата.

Чтобы удалить программу, просто удалите её папку (вместе с .venv).

ЕСЛИ macOS НЕ ДАЁТ ОТКРЫТЬ CarrierSelector.command

Если архив скачан из интернета, macOS может не открыть .command двойным
щелчком. Проще всего запустить через Терминал: откройте Терминал, наберите
cd и пробел, перетащите в окно папку программы, нажмите Enter, затем
наберите python3 start.py и нажмите Enter. Либо: Системные настройки →
Конфиденциальность и безопасность → «Всё равно открыть».

ГДЕ ПРОГРАММА ХРАНИТ ДАННЫЕ

  macOS   — ~/Library/Application Support/CarrierSelector
  Windows — %LOCALAPPDATA%\CarrierSelector
  Linux   — $XDG_STATE_HOME/CarrierSelector или ~/.local/state/CarrierSelector

Это та же папка, что и у обычных сборок, поэтому сохранённые исходные
профили SIM общие: можно ставить пакет одной версией, а возвращать другой.

Журнал (без номеров SIM) пишется в папку logs внутри папки программы.

ПОДДЕРЖИВАЕМЫЕ iPhone

Поддерживаются iPhone с 5G — от iPhone 12 и SE 3 до iPhone 18 Pro Max US,
включая mini, Plus, Pro и Pro Max. Для каждой модели встроен полный набор
профилей из её собственной прошивки iOS 27.2.1 (24B156). iPhone 11 и более
ранние модели в поддержку не входят.

Подробная инструкция по пунктам меню и список изменений — в INSTRUCTION-RU.txt.
