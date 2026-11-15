# CarrierSelector

[![Tests](https://github.com/carrierselector/CarrierSelector/actions/workflows/tests.yml/badge.svg)](https://github.com/carrierselector/CarrierSelector/actions/workflows/tests.yml)

Профиль оператора от Apple — на нужную SIM iPhone, за одну перезагрузку.
По USB, без джейлбрейка, профили Apple ставятся без изменений.
Профиль, встроенный в iOS, всегда можно вернуть.

> ⚠️ Экспериментальный инструмент. Все действия с iPhone вы выполняете на свой
> страх и риск. Работа на будущих версиях iOS не гарантируется.

Обсуждение и рекомендации по профилям для операторов России и Беларуси —
[тема на 4PDA](https://4pda.to/forum/index.php?showtopic=1055886&view=findpost&p=145976834).

## Что умеет

| Пункт меню | Что делает |
|---|---|
| 1 | Привязать профиль оператора к одной SIM |
| 2 | Обе SIM сразу, отдельный профиль для каждой |
| 3 | Вернуть профиль, встроенный в iOS (удалить привязку пакета) |
| 4 | Текущие профили всех SIM (без записи) |
| 5 | Проверить подписи Apple во встроенном каталоге (без записи) |
| 6 | Справка APN и MMS операторов России и Беларуси |
| 7 | Старый способ возврата: поставить сохранённый исходный пакет |

Поддерживаются iPhone с 5G: от iPhone 12 и SE 3 до iPhone 18 Pro Max US (31 модель).
Каждая модель получает профили из своей прошивки iOS 27.2.1 с подписью Apple.

## Как это работает

- Профиль (Apple carrier bundle) упаковывается в IPCC, внешняя папка которого
  названа по IMSI и ICCID выбранной SIM. iOS привязывает такой пакет только к этой
  SIM. Файлы Apple внутри не меняются, подпись Apple проверяется до отправки.
- Пакет отправляется через `installation_proxy` (pymobiledevice3), затем iPhone
  перезагружается один раз, и программа проверяет, какой профиль реально активен.
- Перед отправкой программа сверяет пакет с моделью и платой iPhone: профили без
  настроек для этой платы не предлагаются.
- **Пункт 3** отправляет на SIM профиль её оператора с пустым `SupportedSIMs`.
  CommCenter удаляет пакет, ранее привязанный к этой SIM, и отклоняет новый
  (`No symlinks available`, `kInstallResult: false`). После перезагрузки SIM
  работает на профиле из `/System/Library/Carrier Bundles` — как до любых
  установок. Ответ iOS программа читает из журнала iPhone. Проверено на
  iPhone 15 Pro и 17 Pro Max с двумя SIM, iOS 27.2.1.
- Привязку CarrierSIM (ссылка с именем из одного IMSI, сделанная через AirLift)
  пункт 3 не снимает: iOS принимает только пакеты с папкой `….bundle`.

Подробно — в [INSTRUCTION-RU.txt](INSTRUCTION-RU.txt).

## Скачать

Готовые сборки для macOS (Apple Silicon и Intel), Windows x64 и Linux x64 —
в [Releases](https://github.com/carrierselector/CarrierSelector/releases) и по
ссылкам в теме на 4PDA. Python в сборках уже есть.

- Windows: нужен Apple Devices или iTunes.
- Linux: нужен `usbmuxd`.
- macOS: сборки без подписи Apple Developer ID — при первом запуске разрешите
  программу в «Конфиденциальность и безопасность» или снимите карантин:
  `xattr -dr com.apple.quarantine <папка программы>`.

## Запуск из исходников

Нужен Python 3.12 или новее.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r python_assets/requirements.txt
python tools/fetch_catalog.py      # каталог профилей Apple, ~170 МБ
python carrier_app.py
```

`tools/fetch_catalog.py` скачивает архив каталога из релиза
`catalog-ios27.2.1` и проверяет его SHA-256. В git каталог не хранится.

## Тесты

```bash
PYTHONPATH=. python -m unittest discover -s tests
```

Телефон не нужен: тесты используют эмулятор iPhone (`tests/fake_phone.py`),
который ведёт себя как CommCenter, включая сброс пунктом 3. Тесты запускаются
в GitHub Actions на macOS, Windows и Linux.

## Лицензии

- Код CarrierSelector — [MIT](LICENSE).
- Файлы Apple (профили операторов каталога) под эту лицензию не попадают,
  права на них принадлежат Apple Inc.
- Сторонние библиотеки сохраняют свои лицензии: pymobiledevice3 —
  GPL-3.0-or-later, cryptography — Apache-2.0 или BSD-3-Clause. Сборки
  распространяются вместе с полным исходным кодом из этого репозитория.
