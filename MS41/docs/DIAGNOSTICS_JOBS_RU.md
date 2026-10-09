# Диагностические джобы MS41 (.prg) и их разбор

Статус: выполнено. Дата: 2026-10-05.

## Зачем это нужно

Для настройки генератора не хватало точных имён каналов логирования. Раньше
мы знали только «примерные» названия вида `STATUS_LMM_WERT`, а анализатор
логов сопоставлял колонки нечётким поиском. Источник истины — файлы описания
ЭБУ (`*.prg`, SGBD), которые использует EDIABAS/INPA/Tool32 и, соответственно,
логгер Test0.

Теперь этот пробел закрыт: имена джобов, результатов, единиц измерения и
масштабов взяты из самих SGBD, а не угаданы.

**Важно:** это не заменяет логи. Метаданные говорят, *какие* каналы бывают и
как называются; *значения* по-прежнему берутся только с автомобиля.

## Что найдено

### Файлы SGBD для MS41

| Файл | ЭБУ | Ревизия | Джобов | из них STATUS |
| --- | --- | --- | --- | --- |
| `MS410DS0` | MS 41.0 fuer M52 | 1.905 | 83 | 51 |
| `MS410DS1` | MS 41.0 fuer M52 | 1.10 | 86 | 52 |
| `MS410DS2` | MS 41.0 fuer M52 Flash | 1.10 | 89 | 54 |
| `MS410DS3` | MS 41.0 fuer M52 Flash | 1.08 | 91 | 55 |
| `MS411DS1` | MS 41.1 fuer M52 OBD II | 1.13 | 118 | 66 |
| `MS411DS2` | MS 41.1 fuer M52 OBD II LDP | 1.03 | 120 | 68 |

Для наших прошивок (`1429861`, семейство `064101`, MS41.1 OBD II) практически
значимы `MS411DS1` и `MS411DS2`.

Таблицы, разобранные в каждом варианте: `BETRIEBSWMATRIX`, `BITS`,
`FORTTEXTE`, `FARTMATRIX`, `FARTTEXTE`, `FUMWELTTEXTE`, `NULLEINSTEXTE`,
`JOBRESULT`.

### Найденные парсеры PRG

| Инструмент | Язык | Что даёт |
| --- | --- | --- |
| `radelbro/BimmerDis` | C++ | расшифровка `.prg` (XOR 0xF7) в читаемый `.b1v`/`.b1g` |
| `emdzej/ediabasx` | TypeScript | разбор `.prg`/`.grp`, CLI `ediabasx jobs`, дизассемблер BEST/2 |
| `zer02dev/BimmerDaten` | Python | просмотр джобов, аргументов, результатов, таблиц живым данных |
| `uholeschak/ediabaslib` | C# | интерпретатор EDIABAS, чтение `.grp`/`.prg` напрямую |
| `lpcvoid/bmw-best2-vm` | C | виртуальная машина BEST2 |

В репозитории используется **свой зависимость-независимый парсер**
(`MS41/tools/parse_sgbd.py`) для уже расшифрованного формата — чтобы не
тянуть Node.js/C++/C# в проект и не распространять бинарники BMW.

## Что из этого следует практически

### 1. Точные каналы для Test0

Сформированы готовые наборы логирования: `ms41_logging_presets.json`.
Для `MS411DS1` все четыре профиля (`stage0`, `wot`, `ignition`, `fueling`)
покрыты полностью, без «недостающих» сигналов.

Пример соответствия профиля и джобов:

| Сигнал | Джоб | Результат | Единица |
| --- | --- | --- | --- |
| `rpm` | `STATUS_MOTORDREHZAHL` | `STATUS_MOTORDREHZAHL_WERT` | 1/min |
| `load` | `STATUS_LAST` | `STATUS_LAST_WERT` | mg/Hub |
| `maf_airflow` | `STATUS_LMM` | `STATUS_LMM_WERT` | kg/h |
| `maf_voltage` | `STATUS_LMM_VOLT` | `STATUS_LMM_VOLT_WERT` | V |
| `ignition` | `STATUS_ZUENDWINKEL` | `STATUS_ZUENDWINKEL_WERT` | Grad KW |
| `injector_pulse_width` | `STATUS_EINSPRITZZEIT` | `STATUS_EINSPRITZZEIT_WERT` | ms |
| `lambda_bank1` | `STATUS_L_SONDE` | `STATUS_L_SONDE_WERT` | Volt |
| `knock_bank1` | `STATUS_KLOPF_ADC1` | `STATUS_KLOPF_ADC1_WERT` | V |
| `vehicle_speed` | `STATUS_GESCHWINDIGKEIT` | `STATUS_GESCHWINDIGKEIT_WERT` | km/h |

Это ровно те имена, которые Test0 показывает в списке результатов.

### 2. Быстрое чтение RAM (fast serial)

Таблица `BETRIEBSWMATRIX` описывает ячейки RAM, их порядок байт и
масштабирование — это тот самый ускоренный режим (MS41 Serial/DS2), который в
Test0/TestO даёт высокую частоту обновления. Для `MS411DS1`:

| Сигнал | Ячейка | Порядок | Масштаб A | Смещение B | Единица |
| --- | --- | --- | --- | --- | --- |
| Motordrehzahl | `0xDA2A` | HL | 1 | 0 | 1/min |
| Last | `0xE78E` | HL | 0.02119 | 0 | mg/Hub |
| Zuendwinkel | `0xE818` | HL | 0.01458 | −23.6 | Grad KW |
| Einspritzzeit | `0xDA1E` | HL | 0.005339 | 0 | ms |
| Luftmasse | `0xDA34` | LH | 0.25 | 0 | kg/h |
| Motortemperatur | `0xDA5A` | — | 0.747 | −48 | Grad C |
| Ansauglufttemp | `0xDA50` | — | 0.747 | −48 | Grad C |
| Tast_ll_Steller | `0xDA38` | HL | 655.35 | 0 | % |
| Ubatt | `0xDA5B` | — | 0.102 | 0 | Volt |

Джоб `RAM_LESEN` (`RAM_LESEN_ADRESSE`, `RAM_LESEN_ANZAHL_BYTE` → binary)
позволяет читать произвольные ячейки, включая перечисленные.

Практический вывод: при логировании через быстрый режим значения нужно
применять с этими масштабами; «сырые» два байта без масштаба ничего не значат.

### 3. Булевы состояния — контроль hardcut и popcorn

Джоб `STATUS_DIGITAL` отдаёт битовые признаки, критичные для проверки наших
доработок:

| Результат | Смысл |
| --- | --- |
| `STATUS_SCHUB_AB_EIN` | **Schubabschaltung** — отсечка топлива при сбросе газа активна |
| `STATUS_VL_EIN` | полная нагрузка (WOT) |
| `STATUS_LL_EIN` | холостой ход |
| `STATUS_LAMBDAREGELUNG_1_EIN` / `_2_EIN` | замкнутый контур лямбды по банкам |
| `STATUS_VANOS_EIN` | активация VANOS |
| `STATUS_KO_EIN` | компрессор кондиционера |
| `STATUS_MSR_EIN` | вмешательство MSR/ASC |

`STATUS_SCHUB_AB_EIN` — прямой способ проверить, что стоковая отсечка при
сбросе газа действительно срабатывает и что изменения `c_n_*_puc` не сломали её.

### 4. Таблица неисправностей

`FORTTEXTE` даёт коды `ORT` и немецкие тексты. Их можно использовать для
расшифровки кодов из FS_LESEN в отчёте Stage 0 вместо внешних таблиц.
Примеры: `0x01` «Zuendung Zylinder 2», `0x08` «Luftmassenmesser»,
`0x0A` «Temperaturfuehler Motortemperatur», `0x0E` «Temperaturfuehler
Ansaugluft». Полный список — `ms41_faults.json`: 96 содержательных записей
для `MS411DS1`, 103 для `MS411DS2` (пустые строки с текстом `--` отброшены).

## Команды

```bash
# 1. Получить расшифрованные SGBD (в git-игнорируемый кэш)
python3 MS41/tools/fetch_ms41_sgbd.py

# 2. Из своей установки EDIABAS (рекомендуемый путь для пользователя)
python3 MS41/tools/fetch_ms41_sgbd.py --from-org-dir "C:/EDIABAS/Ecu"

# 3. Разобрать и обновить bundle
python3 MS41/tools/parse_sgbd.py MS41/.cache/sgbd/*.md --outdir MS41/diagnostics

# 4. Разовые отчёты
python3 MS41/tools/parse_sgbd.py MS41/.cache/sgbd/MS411DS1.md --emit summary --pretty
python3 MS41/tools/parse_sgbd.py MS41/.cache/sgbd/MS411DS1.md --emit channels --pretty
python3 MS41/tools/parse_sgbd.py MS41/.cache/sgbd/MS411DS1.md --emit faults
```

Анализатор логов теперь использует `ms41_aliases.json` автоматически: колонки
с точными именами EDIABAS распознаются без нечёткого поиска.

## Юридическая сторона

BMW рассылала DMCA-требования против репозиториев с декомпилированными SGBD
(например, `piersholt/ediabas-sgbd` заблокирован с ноября 2022). Поэтому:

- в репозитории **нет** ни одного `.prg`/`.grp`;
- кэш расшифрованных исходников игнорируется git-ом;
- в `MS41/diagnostics/` лежит только переработанная функциональная выжимка
  (имена, единицы, масштабы, коды), а не тексты файлов BMW;
- пользователю рекомендуется строить bundle из собственной лицензионной
  установки EDIABAS через `--from-org-dir`.

## Ограничения

- Расшифрованные документы описывают вариант ЭБУ, а не конкретную версию ПО.
- Если джоб отсутствует в варианте, он попадает в `unsupported_signals`, а не
  подставляется молча.
- `FUMWELTTEXTE` (freeze-frame) разобрана, но пока не сведена в отчёт по
  неисправностям.
- Значения всё равно нужно снимать с автомобиля: метаданные не заменяют логи.
