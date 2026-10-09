# Test0/TestO для логирования Siemens MS41

Статус: рекомендуемый логгер проекта TorqueX. Дата исследования: 2026-10-05.

> Название программы пишется `Test0` — последний символ является цифрой ноль. В старых публикациях встречаются `TestO`, `testo` и «Test-O».

## Почему рекомендуем Test0

Для MS41 нужен доступ к BMW-specific данным, которых недостаточно в generic OBD-II. Test0 работает через EDIABAS, умеет выполнять ECU jobs, записывать CSV и подключать внешние wideband-контроллеры. Для MS41 также исторически поддерживался ускоренный direct serial/DS2 logging.

Актуальный Test0 v2 является современной переработкой старой версии 2017 года:

- Windows 10/11 x64;
- встроенный EdiabasLib или существующая установка INPA/Tool32/ISTA;
- автоматическое определение ECU;
- logging presets;
- CSV;
- внешние Innovate, AEM, PLX, 14Point7 и Techedge wideband;
- custom expressions;
- WebSocket и MCP интерфейсы.

Официальные страницы:

- https://phenoboy.kapsi.fi/Test0-2.0/
- https://phenoboy.kapsi.fi/Test0-2.0/docs.html
- https://phenoboy.kapsi.fi/Test0-2.0/downloads.html

Источники по MS41 и legacy TestO:

- https://www.romraider.com/forum/viewtopic.php?f=42&t=11083
- https://www.romraider.com/forum/viewtopic.php?t=11086
- https://sites.google.com/site/openms41/romraider/datalogging

Проект не включает установщик Test0, BMW PRG-файлы или проприетарные компоненты. Пользователь скачивает их из официального источника и принимает соответствующие лицензии.

## Рекомендуемая установка

1. Установить актуальный Test0 v2 с официальной страницы.
2. Использовать встроенный EdiabasLib либо корректно установленный BMW EDIABAS.
3. При отсутствии ECU descriptions установить официальный Test0 ECU bundle.
4. Подключить качественный K-line/K+DCAN USB-интерфейс.
5. Выполнить `File → Detect ECU`.
6. Проверить, что выбран именно ECU автомобиля, а не похожее семейство.
7. Сохранить отдельный logging preset `TorqueX_MS41`.
8. Указать CSV output и записать короткий stationary test до дорожного лога.

Для legacy TestO следует выбирать MS41 Serial/DS2 profile, если он корректно соединяется с конкретным ECU. В противном случае используется EDIABAS job logging. Нельзя подменять тип ECU только ради соединения.

## Важная особенность синхронизации

Legacy EDIABAS jobs могут опрашивать параметры последовательно. Поэтому RPM, TPS, MAF и ignition могут относиться к немного разным моментам времени. Чем больше отдельных jobs/channels, тем ниже фактическая частота каждого сигнала.

Правила:

- использовать единый File/Data Logger CSV;
- в legacy версии выбрать actual time;
- синхронизировать/reset graphs до заезда;
- не включать десятки ненужных параметров;
- использовать ускоренный MS41 Serial/DS2 preset, если он подтверждён;
- всегда сохранять timestamp;
- analyser должен показывать sample rate, пропуски и несинхронность;
- при внешнем wideband предпочтительно писать его в Test0 на той же timeline.

Средняя частота строк не равна гарантированной частоте каждого канала. Для принятия tuning-решений требуется проверить изменения и timestamps конкретных каналов.

## Наборы параметров

### `TorqueX_MS41_Stage0`

Минимум:

- timestamp;
- RPM;
- ECT;
- IAT;
- MAF kg/h;
- MAF voltage;
- load mg/stroke;
- TPS;
- ignition angle;
- battery voltage;
- lambda/fuel adaptations обоих банков;
- VANOS state;
- vehicle speed;
- active/pending DTC отдельным диагностическим чтением.

### `TorqueX_MS41_WOT`

Обязательно:

- timestamp;
- RPM;
- TPS;
- load;
- MAF kg/h;
- MAF voltage;
- injector pulse width;
- actual ignition angle;
- IAT;
- ECT;
- vehicle speed;
- knock correction/adaptation по цилиндрам, если доступно;
- external wideband lambda.

Желательно:

- VANOS state;
- open-loop/full-load state;
- battery voltage;
- marker начала заезда.

Штатные narrowband sensors не заменяют external wideband для WOT fueling.

### `TorqueX_MS41_Fueling`

- RPM;
- load;
- MAF kg/h и voltage;
- IPW;
- STFT/LTFT или BMW lambda adaptations обоих банков;
- external wideband lambda;
- IAT/ECT;
- TPS;
- open/closed-loop state.

### `TorqueX_MS41_Ignition`

- RPM;
- load;
- actual ignition;
- knock current/adaptation по доступным цилиндрам;
- IAT/ECT;
- external lambda;
- MAF;
- TPS;
- vehicle speed.

## Протокол безопасного заезда

- Логгер настраивает пассажир; водитель не работает с ноутбуком в движении.
- Закрытая площадка или динамометрический стенд.
- До WOT полностью прогреть двигатель, но не перегреть intake.
- Проверить отсутствие DTC, misfire и утечек.
- Обычно используется одна передача и плавный переход к WOT; точную передачу определяют автомобиль, стенд и безопасность.
- Немедленно прекратить заезд при lean lambda, knock, падении fuel pressure, перегреве, misfire или ненормальном звуке.
- Выполнить не менее трёх воспроизводимых заездов с cooldown.
- Сохранять metadata: автомобиль, двигатель, BIN SHA-256, прошивка, RON, АЗС, температура, передача и конфигурация железа.

## Чтение CSV в TorqueX

Добавлен dependency-free analyser:

```bash
python3 MS41/tools/analyze_testo_log.py drive.csv --profile stage0
python3 MS41/tools/analyze_testo_log.py drive.csv --profile wot --json
python3 MS41/tools/analyze_testo_log.py drive.csv --profile fueling
python3 MS41/tools/analyze_testo_log.py drive.csv --profile ignition
```

Поддерживается:

- legacy TestO и Test0 v2 CSV;
- UTF-8/BOM и Windows cp1252;
- comma, semicolon, TAB и pipe delimiters;
- decimal point и decimal comma;
- numeric, clock и date/time timestamps;
- немецкие EDIABAS и английские названия каналов;
- сохранение неизвестных колонок в отчёте;
- duration, average row rate, missing values, min/max/mean и число изменений.

Статусы:

- `READY_FOR_ANALYSIS` — обязательные каналы выбранного профиля найдены;
- `INCOMPLETE` — CSV прочитан, но обязательных каналов не хватает;
- `INVALID`/ошибка — файл невозможно надёжно прочитать.

`READY_FOR_ANALYSIS` означает только достаточную структуру лога. Это не означает, что двигатель или калибровка безопасны. Следующий этап должен анализировать участки режима, lambda error, knock, MAF clipping и injector duty cycle с учётом единиц.

## Ограничения первой реализации

Реальные Test0 v2 и legacy TestO могут экспортировать пользовательские названия колонок. Поэтому:

- исходное имя каждой колонки всегда сохраняется;
- единицы не угадываются и не преобразуются молча;
- несколько колонок, сопоставленных одному типу сигнала, вызывают warning;
- неизвестный signal остаётся `unmapped`;
- пользовательские aliases будут добавляться по реальным загруженным логам.

До получения набора реальных MS41 CSV analyser является совместимым parser/quality gate, а не окончательным автоматическим тюнером.
