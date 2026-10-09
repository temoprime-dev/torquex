# MS41 Stage 1: веб-анализ перед разработкой генератора

Дата: 2026-10-05.

## 1. Scope первого генератора

MS41 нельзя рассматривать как одну универсальную прошивку. В семействе встречаются MS41.0, MS41.1 и MS41.2, разные software ID и разные адреса калибровок. Для первого релиза предлагается:

- ECU: **Siemens MS41.0**;
- hardware/software family: **1429861**;
- двигатель: атмосферный BMW M52;
- размер calibration: **24 KiB**;
- полный образ: **256 KiB**;
- топливо: отдельно RON95 и RON98;
- только штатный код, без MS41.3 и forced induction.

В репозитории уже есть XDF для 1429861, но пока нет проверенного stock BIN. До появления эталонного дампа генератор должен оставаться анализатором definitions, а не выпускать прошивки.

## 2. Особенности MS41 по сравнению с MS42/MS43

- MS41 управляет M52 и имеет один впускной VANOS с логикой **on/off**, а не непрерывные intake/exhaust targets как MS42/MS43.
- Основная калибровка занимает 24 KiB; полный flash — 256 KiB.
- Base fuel map задаёт injector pulse width для стехиометрической смеси. Full-load смесь формируется дополнительным Open Loop/WOT enrichment.
- Open-loop detection в штатном коде в основном опирается на throttle position и RPM. В MS41.3 доступна другая логика по load, но это отдельная платформа.
- У разных MS41.x совпадающие названия таблиц не гарантируют одинаковые адреса.
- MS41.2 ID12 исторически наиболее полно исследован в OpenMS41/RomRaider; для MS41.0 ID41 и MS41.1 ID60 поддержка отличается.

Источники: [MS4X Siemens MS41](https://www.ms4x.net/index.php?title=Siemens_MS41), [OpenMS41](https://sites.google.com/site/openms41/), [RomRaider MS41 definitions](https://www.romraider.com/forum/viewtopic.php?f=58&t=10695).

## 3. Что реально даёт прирост на атмосферном M52

При штатном впуске, выпуске и распредвалах заводская калибровка уже близка к оптимальной. Основные доступные направления:

1. найти правильную точку отключения VANOS;
2. привести fuel trims к нулю и настроить WOT lambda;
3. оптимизировать high-load ignition до MBT или knock limit;
4. убедиться, что open-loop enrichment включается в нужной зоне;
5. откалибровать MAF/форсунки только при изменённом железе.

Форумные и коммерческие заявления обычно находятся в диапазоне примерно 5–10 hp для stock Stage 1 и 10–15 hp с впуском/выпуском, но это не единый проверяемый стандарт. Генератор не должен обещать такую прибавку. Итог принимается только по повторяемым A/B/A замерам момента.

Большой атмосферный прирост чаще требует железа: M50 manifold, распредвалы, выпуск. Тогда универсальная Stage 1 карта уже не подходит.

## 4. Stage 0 — обязательный допуск

Performance BIN не создаётся, если не подтверждено:

- точное семейство MS41.x и software ID;
- совпадение BIN, размера и definition;
- отсутствие DTC/misfire;
- исправность MAF, lambda, TPS, coolant/IAT;
- одинаково разумные trims банков;
- давление топлива и производительность форсунок;
- отсутствие vacuum leaks;
- механическая исправность VANOS;
- компрессия и исправное зажигание;
- известный минимальный октан;
- оригинальный full read и SHA-256.

## 5. Приоритет P0 — корректная идентификация

Генератор должен определять:

- 24 KiB partial или 256 KiB full;
- MS41.0/41.1/41.2;
- internal software/calibration ID;
- двигатель, коробку, регион и emissions variant;
- применимый XDF/XML.

Нельзя выбирать definition только по размеру. OpenMS41 указывает как минимум ID41 для MS41.0, ID60 для MS41.1 и ID12 для MS41.2. Неправильные адреса могут дать правдоподобные, но ложные таблицы.

Для нашего XDF первая поддерживаемая пара должна быть строго `1429861 + matching 24K/256K XDF`.

## 6. Приоритет P1 — топливо

Руководство [OpenMS41 Fueling](https://sites.google.com/site/openms41/romraider/fueling) требует wideband и рекомендует менять либо injector calibration, либо MAF calibration за один цикл, чтобы не смешивать ошибки.

### Главные функции

В терминологии OpenMS41/RomRaider:

- `Fuel Injection - Base` — базовый pulse width;
- `Fuel Injection - Open Loop Enrichment` / `WOT Enrichment`;
- `Open Loop Enrichment - Detection` / WOT TPS threshold;
- `Fuel Injection - Base - VANOS Correction`;
- `Fuel Injector Dead Time`;
- cranking initial/cranking maps;
- warm-up и transient enrichment.

В Siemens XDF 1429861 соответствующие области представлены символами:

- `ip_tib__n__maf` — base injection;
- `ip_ti_ivvt__n__maf` — VANOS-related injection correction;
- `ip_ti_wup__n__maf` — warm-up;
- `ip_ti_fac_ae__n__maf` и transient corrections;
- `id_maf_tab__v_maf_1__v_maf_2` — MAF transfer.

Точное соответствие OpenMS41 friendly-name ↔ Siemens symbol надо подтвердить по адресам и формулам до реализации writer.

### Правила Stage 1

- На штатных форсунках/MAF не масштабировать base fuel map автоматически.
- Сначала добиться корректных trims в closed loop.
- WOT настраивать только по синхронизированной wideband lambda.
- Open-loop threshold должен включать enrichment до высокой нагрузки, но не при обычном cruise.
- После изменения VANOS повторно проверять WOT lambda: наполнение изменится.
- Не использовать MAF scaling для маскировки vacuum leak или неправильного давления топлива.
- Не смешивать в одной итерации MAF scaling и injector/dead-time calibration.

В обсуждении [RomRaider Fuel map tuning methods](https://www.romraider.com/forum/viewtopic.php?start=75&t=13969) последовательность также сводится к настройке stoichiometric base map, затем WOT enrichment. Форумные процедуры отключения отдельных corrections полезны только для ручного контролируемого эксперимента и не должны автоматически попадать в пользовательский итоговый BIN.

## 7. Приоритет P1 — VANOS

Для M52 это наиболее понятная и измеримая оптимизация. Метод OpenMS41:

1. WOT pull с VANOS постоянно выключенным;
2. WOT pull с VANOS постоянно включённым;
3. построить load vs RPM;
4. найти пересечение кривых;
5. поставить switch-off около пересечения.

Источник: [OpenMS41 VANOS tuning](https://sites.google.com/site/openms41/romraider/vanos-tuning).

Практическое уточнение сообщества: из-за скорости механизма команду иногда приходится давать примерно на 200–300 rpm раньше измеренной точки пересечения. Это не универсальная константа и должно проверяться actual response/log.

Для других распредвалов точка значительно меняется. В обсуждениях M52 с S52 cams перенос switch-off выше устранял провал момента, но готовое число с чужого мотора копировать нельзя.

Генератор должен поддерживать:

- `stock` — исходная точка;
- `measured` — вычисление по двум логам;
- hardware preset — только для подтверждённой комбинации engine/cams/manifold;
- проверку температурных и light/heavy throttle VANOS tables.

Нельзя просто держать VANOS включённым до limiter.

## 8. Приоритет P1 — зажигание

Основные Siemens XDF symbols:

- `ip_igab__n__maf` — base ignition;
- `ip_iga_ivvt__n__maf` — VANOS-related ignition correction;
- `ip_iga_max_knk__n__maf` — knock-related ignition boundary;
- `ip_iga_tra_knk__n__maf` — knock transition/correction;
- cylinder-specific knock adaptation tables `id_fac_knk_*`, `id_knkwb_*`, `id_knkwe_*`.

Стратегия:

1. сначала исправить lambda и VANOS;
2. стабилизировать IAT/coolant;
3. менять только high-load cells;
4. использовать маленькие локальные шаги;
5. сравнивать момент и knock adaptation;
6. остановиться у MBT, даже если knock отсутствует;
7. откатить угол при повторяемой коррекции.

Отсутствие зарегистрированной детонации не означает, что дальнейший advance даёт мощность. В опубликованном опыте MS41 чрезмерный high-rpm advance ухудшал мощность без явного knock. Поэтому глобальное `+2…5°` из форумных сообщений нельзя превращать в правило генератора.

Запрещено автоматически менять knock sensitivity/thresholds или отключать knock detection.

## 9. MAF и форсунки

Изменять только при смене компонентов.

### Форсунки

OpenMS41 рекомендует:

- начать с процентной коррекции base injection по отношению расхода старых и новых форсунок;
- отдельно изменить cranking maps;
- dead-time взять из данных производителя при фактическом давлении;
- затем настроить по trims и wideband.

Процентное масштабирование — только стартовая оценка: dead-time особенно влияет на idle/low load.

### MAF

Нужна transfer curve напряжение → airflow и проверка по логам. Изменение MAF и форсунок одновременно делает ошибку неидентифицируемой. Для штатного NA Stage 1 MAF остаётся без изменений.

## 10. Load/RPM axes и ограничения

На атмосферном штатном моторе оси обычно менять не нужно. Расширение load axis актуально для forced induction или существенно другого airflow, но уменьшает разрешение таблицы и требует согласованного изменения fuel, ignition, knock и diagnostics maps.

MS41.3 предлагает расширенные load/airflow limits, AFR target, MAP/Alpha-N и другие функции, но это отдельный firmware target. Нельзя применять правила MS41.3 к stock MS41.0 BIN.

Rev limiter не создаёт мощность. Поднимать его можно только при наличии железа, которое продолжает создавать мощность и безопасно работает на этих оборотах.

## 11. Throttle response

MS41 на M52 не имеет MS43-style electronic Drivers Wish. Ощущение отклика определяется TPS/open-loop detection, transient enrichment, VANOS, зажиганием, тросовой заслонкой и механикой.

Нельзя выдавать раннее enrichment или агрессивный transient fuel за прирост мощности. Transient maps настраиваются отдельно после steady-state fuel/ignition.

## 12. Что не входит в безопасный Stage 1

- EWS delete;
- DTC suppression;
- SAP/rear O2 delete;
- catalyst heating delete;
- pops/bangs;
- hard cut;
- checksum disable;
- knock disable;
- ASC safety removal;
- повышенный limiter без механического обоснования.

Это отдельные compatibility/off-road options и не является power tuning.

## 13. Минимальные логи

- RPM, TPS, load;
- MAF voltage и airflow;
- injector pulse width;
- external wideband lambda;
- STFT/LTFT обеих банков;
- ignition advance;
- cylinder knock adaptation/retard;
- VANOS commanded/state;
- coolant, IAT;
- vehicle speed/gear;
- battery voltage.

Для VANOS нужны парные pulls ON/OFF. Для ignition нужны повторяемые стендовые pulls. Один дорожный pull недостаточен для финального preset.

## 14. Алгоритм MS41 Stage 1 v0

1. Определить exact variant/software ID и размер.
2. Проверить definition compatibility.
3. Проверить Stage 0 report.
4. Извлечь baseline fuel/ignition/VANOS/MAF/limiter maps.
5. На stock hardware оставить MAF/injector/base fuel без автоматической дельты.
6. Рассчитать VANOS switch из ON/OFF logs либо оставить stock.
7. Настроить open-loop threshold без раннего enrichment на cruise.
8. Настроить WOT lambda по wideband error surface.
9. После lambda/VANOS оптимизировать high-load ignition до MBT/knock limit.
10. Проверить transient, hot/cold operation и fuel trims.
11. Выполнить range, smoothness, axis и checksum validation.
12. Выдать BIN, полный diff, confidence и список непроверенных зон.

## 15. Первый программный milestone

До генерации значений реализовать **MS41 analyzer**:

- чтение 24K и 256K;
- распознавание 1429861 и software/calibration ID;
- отказ при несовпадающем XDF;
- извлечение ключевых symbols;
- адреса, размеры, axes, units и conversion formulas;
- baseline JSON/Markdown report;
- round-trip encode/decode без единого изменённого байта;
- checksum detection/validation;
- запрет записи, пока нет проверенного stock BIN fixture.

## 16. Вывод

Для штатного M52 лучший кандидат на измеримый Stage 1 эффект — правильно найденная точка VANOS, затем WOT lambda и осторожная high-load ignition optimization. Универсальные процентные fuel/ignition изменения небезопасны и часто не дают мощности. Разработку следует начать с точной идентификации MS41 и анализатора XDF/BIN, а presets создавать только из подтверждённых логами и стендом комбинаций.
