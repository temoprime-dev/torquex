# Техническое задание: генератор калибровок Siemens MS41.0 / 1429861

Статус: web-analysis и требования к MVP. Дата: 2026-10-05.

## 1. Цель

Создать детерминированный генератор калибровок для подтверждённых Siemens MS41.0 `1429861`:

- базовые профили `Stage 1` и `Stage 2`;
- опции `Hardcut` и `Popcorn/Burble`;
- выбор топлива `RON 91`, `RON 95`, `RON 98`;
- входной BIN пользователя 24 KiB или 256 KiB;
- выходной BIN того же размера, checksum, подробный diff и предупреждения.

Генератор не должен подменять стендовую настройку. Он создаёт безопасную стартовую калибровку только для известного двигателя и железа.

## 2. Поддерживаемая платформа MVP

| Поле | Требование |
|---|---|
| Производитель | Siemens |
| ECU | MS41.0 |
| Hardware/software family | 1429861 / internal family `064101` |
| Partial | 24 KiB, XDF base `0x0000` |
| Full | 256 KiB, calibration base `0x14000` |
| Двигатели | M52B20, M52B25, M52B28 |
| Наддув | Нет |
| Код | Stock MS41; MS41.3 вне MVP |
| Коробка | MT/AT должна указываться пользователем |

Файл допускается только после `analyze_firmware.py`: Siemens/MS41 signature, размер и все 15 calibration CRC должны быть валидны.

## 3. Источники исследования

Основные:

- [OpenMS41 — Fueling](https://sites.google.com/site/openms41/romraider/fueling)
- [OpenMS41 — VANOS tuning](https://sites.google.com/site/openms41/romraider/vanos-tuning)
- [OpenMS41 — Datalogging](https://sites.google.com/site/openms41/romraider/datalogging)
- [OpenMS41 — MS41.3](https://sites.google.com/site/openms41/custom-code---ms41-3)
- [RomRaider — MS41 ECU/Logger Definitions](https://www.romraider.com/forum/viewtopic.php?f=58&t=10695)
- [RomRaider — Deceleration](https://www.romraider.com/forum/viewtopic.php?f=42&t=11393)
- [RomRaider — Hard limiter](https://www.romraider.com/forum/viewtopic.php?f=42&t=12830)
- [RomRaider — Fuel map tuning methods](https://www.romraider.com/forum/viewtopic.php?t=13969)

Форумные численные значения считаются экспериментальными, а не готовыми defaults. В генератор попадают только значения, подтверждённые для конкретной комбинации двигатель/железо/топливо.

## 4. Входные данные

Обязательные:

```yaml
ecu: MS41.0
engine: M52B28
transmission: MT
fuel_ron: 98
stage: 1
hardware:
  injectors: stock
  maf: stock
  intake_manifold: stock
  camshafts: stock
  exhaust_manifold: stock
  catalytic_converters: present
options:
  hardcut: false
  popcorn: off
```

Также требуется Stage 0 checklist:

- нет active/pending DTC и misfire;
- исправны MAF, TPS, lambda, IAT/ECT, VANOS и knock sensors;
- trims обоих банков в допустимом диапазоне;
- нет vacuum leak;
- подтверждено давление топлива;
- известен минимальный используемый RON;
- сохранён оригинальный full read.

При отсутствии Stage 0 подтверждения генератор может сделать анализ, но не performance output.

## 5. Общая последовательность калибровки

Порядок обязателен:

1. идентификация BIN/XDF;
2. baseline report;
3. MAF/injector calibration — только если железо заменено;
4. closed-loop fuel trims;
5. VANOS switch point;
6. full-load detection и WOT lambda;
7. high-load ignition под выбранный RON;
8. transient validation;
9. только затем Hardcut/Popcorn;
10. CRC, diff и повторное чтение результата.

После изменения VANOS смесь и зажигание проверяются повторно, потому что изменяется наполнение цилиндров.

---

# 6. Stage 1 — базовый профиль

## 6.1 Назначение

Для исправного M52 со штатными:

- форсунками;
- MAF;
- впускным коллектором;
- распредвалами;
- выпуском или только cat-back.

Ожидается улучшение формы момента и отклика, а не большая прибавка peak power. Маркетинговые заявления `+5–10 hp` не являются критерием приёмки.

## 6.2 Что разрешено изменять

### VANOS

Ключевые symbols:

- `id_ivvt_n_fl__n` — состояние VANOS на высокой нагрузке;
- `id_pvs_ivvt__n` — throttle threshold VANOS high-load;
- `ip_maf_min_ivvt__n__tco` — load threshold, MT;
- `ip_dri_maf_min_ivvt__n__tco` — load threshold, AT;
- `id_maf_hys_ivvt__n`, `c_n_hys_ivvt` — hysteresis;
- `ip_iga_ivvt__n__maf` — ignition correction с активным VANOS;
- `ip_ti_ivvt__n__maf` — fuel correction с активным VANOS.

Метод определения switch-off:

1. лог WOT с VANOS forced off;
2. лог WOT с VANOS forced on;
3. сравнить load/airflow относительно RPM;
4. найти пересечение;
5. учесть фактическое время реакции VANOS;
6. подтвердить A/B/A pulls.

В генераторе допустим stock preset или измеренный switch point. Чужое фиксированное RPM не является универсальным preset.

### Full-load detection

- `id_pvs_fl__n` — TPS threshold full-load;
- `c_n_min_fl`, `c_tco_min_fl`, `c_vs_min_fl` — условия разрешения.

Нельзя чрезмерно понижать threshold: это преждевременно включает open-loop enrichment в обычной езде.

### WOT fueling

- `ip_ti_fl__n` / представление `ip_ti_fl__n (Lambda)`;
- `ip_tib__n__maf` — base/fallback injection;
- `ip_ti_ivvt__n__maf` — VANOS correction.

Правила:

- штатные base fuel и MAF не изменяются без логов;
- WOT target задаётся в lambda;
- обязательна внешняя wideband;
- контролируются обе банки, IPW и fuel pressure;
- correction строится как smooth RPM curve, без ступеней;
- точное target lambda не задаётся универсально из веб-таблицы.

### Ignition

- `ip_igab__n__maf` — target ignition/fallback;
- основная ignition surface, определённая XDF для normal operation;
- `ip_iga_ivvt__n__maf` — correction при VANOS;
- knock adaptation/retard maps используются для мониторинга, но не ослабляются.

Оптимизация только high-load cells: небольшие локальные шаги до MBT или knock limit. Если момент перестал расти, advance прекращается даже при отсутствии knock.

## 6.3 Что Stage 1 не меняет

- injector data и MAF на штатном железе;
- knock thresholds/windows;
- catalyst protection;
- DTC masks;
- cold start ради мощности;
- rev limiter автоматически;
- base fuel глобальным процентом.

---

# 7. Stage 2 — hardware-dependent профиль

## 7.1 Допустимое железо

Stage 2 разрешён только при точном выборе компонентов:

- M50 intake manifold или другой известный manifold;
- headers / high-flow exhaust;
- performance intake с сохранённым штатным MAF housing;
- известные camshafts;
- другие injectors/MAF — только с полными характеристиками.

`Stage 2` не является одной картой. Preset key:

```text
engine + manifold + cams + headers + MAF + injectors + fuel RON + transmission
```

Если комбинации нет в базе validated presets, генератор выпускает baseline и требует логирование, но не заявляет финальную Stage 2 настройку.

## 7.2 Дополнительные изменения

### M50 manifold / cams

- повторный VANOS ON/OFF sweep;
- новая точка switch-off;
- повторная WOT lambda calibration;
- новая ignition optimization;
- idle/transient проверка.

С форума встречается перенос VANOS switch выше для S52 cams, но конкретное RPM не должно копироваться без load-curve intersection.

### Headers / freer exhaust

- wideband проверка WOT;
- ignition sweep заново;
- контроль ложного knock из-за изменившейся механической/выхлопной акустики;
- catalyst configuration не отключается автоматически.

### Другие форсунки

- `ip_tib__n__maf` как стартовое масштабирование по отношению flow;
- cranking maps `ip_ti_cst__n__tco`, `ip_tipr_cst__tco`;
- dead-time/voltage correction из datasheet;
- затем trims и wideband.

Нельзя одновременно калибровать неизвестные injectors и неизвестный MAF.

### Другой MAF/housing

- `id_maf_tab__v_maf_1__v_maf_2 (1x256)` / `id_maf_tab__v_maf_1__v_maf_2 (16x16)`;
- диагностика и limits;
- проверка smooth/monotonic transfer curve;
- изменение fuel/ignition load axes только при реальной необходимости.

---

# 8. Выбор топлива RON 91 / 95 / 98

## 8.1 Архитектура опции

Stock MS41 не предоставляет современный независимый переключатель трёх готовых ignition maps. Выбор RON в генераторе означает создание отдельного BIN/preset:

```text
output_M52B28_stage1_RON91.bin
output_M52B28_stage1_RON95.bin
output_M52B28_stage1_RON98.bin
```

Пользователь обязан постоянно использовать топливо не ниже выбранного RON.

## 8.2 RON 91

- наиболее консервативный high-load ignition;
- запрещён перенос advance из RON98 preset;
- увеличенный запас для IAT, нагара и разброса топлива;
- knock adaptation должна оставаться активной;
- Stage 2 с высокой степенью сжатия/агрессивными cams может быть отклонён.

## 8.3 RON 95

- средний профиль;
- начинается от validated stock/91 baseline;
- advance добавляется только там, где стенд показывает рост момента без устойчивой knock correction.

## 8.4 RON 98

- разрешает отдельную оптимизацию high-load ignition;
- не означает глобальный `+2…5°`;
- итог ограничен MBT и knock, а не номером топлива;
- после смены АЗС/сезона нужны контрольные логи.

## 8.5 Общие требования

Нельзя изменять в зависимости от RON:

- knock sensor enable;
- knock windows/sensitivity без отдельной инженерной валидации;
- thermal protections;
- misfire detection.

Логи: current ignition, все cylinder knock adaptations, IAT, ECT, load, RPM и lambda. Высокие knock adaptations сначала рассматриваются как неисправность/MAF/смесь/механический шум, а не как повод ослабить knock control.

---

# 9. Hardcut

## 9.1 Что реально умеет stock MS41

Штатный limiter MS41 является **fuel-cut limiter**. Это не spark-cut. Настоящий ignition-cut/retard limiter относится к custom code MS41.3 и не должен имитироваться случайной правкой stock maps.

Основные symbols MS41.0 `1429861`:

| Symbol | Partial address | Назначение |
|---|---:|---|
| `c_n_max` | `0x01CA` | основной RPM limit |
| `c_n_max_max` | `0x01D3` | maximum/hard RPM limit |
| `c_n_max_hys` | `0x01D1` | hysteresis возврата injection |
| `c_n_max_hys_max` | `0x01D2` | hysteresis включения всех injectors |
| `id_n_max_cycnr__vs` | table | скорость нарастания cylinder shut-off по vehicle speed |

Для full 256K эффективный адрес равен partial address `+0x14000`.

## 9.2 Профили

- `off/stock` — без изменений;
- `firm` — штатный fuel cut с уменьшенной, но не минимальной hysteresis;
- `hard` — более узкая hysteresis, отдельное экспертное подтверждение;
- `spark_cut` — недоступно на stock MS41, требует MS41.3.

## 9.3 Правила безопасности

- `c_n_max_max` должен оставаться выше основного limit;
- значения должны быть согласованы с MT/AT и mechanical limit;
- generator не повышает RPM limit только ради звука;
- для stock valvetrain применяется conservative ceiling;
- запрет при неизвестном состоянии valve springs/oil system;
- не объединять aggressive limiter с Popcorn по умолчанию.

Форумные советы сводятся к уменьшению hysteresis, но минимальное значение создаёт наиболее частый cut/re-enable цикл и повышенную нагрузку. Поэтому exact default должен быть подтверждён на автомобиле, а не взят из одного сообщения.

---

# 10. Popcorn / Burble

## 10.1 Принцип

Для crackles нужны одновременно:

1. fuel не должен быть немедленно отключён на deceleration;
2. ignition должен быть задержан;
3. должен оставаться контролируемый airflow.

MS41 deceleration logic сначала может плавно retard ignition, затем отключить injectors. Правильная ограниченная реализация работает через deceleration maps, а не через разрушительную правку всей low-load base ignition surface.

## 10.2 Связанные symbols

Определённые в нашем XDF:

- `c_pvs_grd_pu` — throttle gradient fast-deceleration detection;
- `c_n_min_lgrd_pu`, `c_tco_min_pu` — fast-deceleration conditions;
- `ip_n_min_puc__tco` — minimum RPM для trailing-throttle fuel cut, AC off;
- `ip_n_min_accin_puc__tco` — то же, AC on;
- `c_n_hys_min_puc`, `c_n_hys_max_puc` — overrun fuel-cut hysteresis;
- `ip_iga_puc__n` — ignition correction during fuel cut;
- `ip_iga_puc_accin__n` — correction с AC;
- `ip_iga_max_puc__n` — maximum ignition advance during overrun;
- `ip_ti_cycnr_pur__n__tco` — duration deceleration fuel reduction;
- `ip_ti_fac_pur__n__maf` и transient deceleration corrections;
- fuel reactivation maps `id_ti_add_*`, `c_ti_inc_reac`, `c_ti_dec_reac`.

Friendly names из OpenMS41/RomRaider должны быть сопоставлены с Siemens symbols по адресу перед записью rule set.

## 10.3 Пользовательские режимы

```text
popcorn: off
popcorn: mild
popcorn: sport
```

`aggressive/flame` не включается в публичный безопасный preset.

### Mild

- только прогретый двигатель;
- ограниченное окно RPM;
- короткая duration;
- умеренный retard;
- штатный fuel cut обязательно возвращается;
- не работает около idle и cold start.

### Sport

- только catless/off-road конфигурация;
- более широкое RPM window;
- отдельное подтверждение риска;
- EGT и exhaust hardware validation обязательны.

## 10.4 Жёсткие ограничения

Popcorn запрещён, если:

- catalytic converters установлены;
- неизвестна конфигурация выпуска;
- есть misfire/lambda/MAF/DTC;
- двигатель холодный;
- AT/DSC behavior не проверено;
- пользователь выбрал RON ниже validated preset;
- нет stock fallback BIN.

Риски: перегрев/оплавление катализатора, повреждение выпускных клапанов, коллектора, turbo (если есть), глушителя и lambda sensors; увеличение выбросов и возможная незаконность на дорогах. OpenMS41 описывает catalyst heating/overheat protection как отдельные защитные функции — генератор не отключает их ради звука.

## 10.5 Чего нельзя делать

- ставить extreme retard по всей base ignition map;
- полностью отключать overrun fuel cut во всех температурах/RPM;
- отключать catalyst overheat protection;
- добавлять топливо глобально;
- включать Popcorn одновременно с cold catalyst heating;
- копировать форумные `-19/-20°` как универсальный default.

---

# 11. Совместимость опций

| Stage | RON | Hardcut | Popcorn | Результат |
|---|---|---|---|---|
| 1 | 91/95/98 | stock/firm | off | Основной поддерживаемый режим |
| 1 | 91/95/98 | hard | off | Expert, mechanical confirmation |
| 1 | 95/98 | stock/firm | mild | Только без катализаторов после validation |
| 2 | 91 | любой | любой | Может быть отклонён по hardware/compression |
| 2 | 95/98 | stock/firm | off | Hardware-specific preset |
| 2 | 98 | hard | sport | Только expert/off-road, не default |

Генератор должен разрешать конфликтующие опции явно, а не молча комбинировать их.

# 12. Выходные артефакты

Для каждой генерации:

```text
output.bin
output.diff.json
output.report.md
output.sha256
ruleset.json
```

Diff содержит:

- symbol;
- partial и effective full address;
- old raw/new raw;
- old physical/new physical;
- единицу;
- основание изменения;
- preset source/confidence;
- warning/risk category.

# 13. Валидация результата

Обязательные проверки:

- размер входа = размеру выхода;
- изменения только по allowlist addresses;
- XDF bounds;
- axes не повреждены;
- smooth/monotonic checks;
- limiter ordering;
- RON option не изменяет knock protection;
- Popcorn temperature/RPM gating;
- все 15 calibration CRC корректны;
- повторный parse output;
- оригинал не перезаписан.

# 14. Этапы реализации

## Phase 1 — безопасное ядро

- XDF parser;
- symbol inventory и address resolver;
- physical/raw conversion;
- read-only report;
- round-trip encode test;
- CRC corrector.

## Phase 2 — Stage 1

- engine-specific baseline presets;
- VANOS measured switch-point input;
- full-load threshold;
- WOT lambda correction import;
- RON-specific ignition delta surface;
- без Popcorn по умолчанию.

## Phase 3 — Stage 2

- hardware profiles;
- M50 manifold/cams/headers;
- MAF/injector workflows;
- обязательный log-feedback cycle.

## Phase 4 — дополнительные опции

- fuel-cut Hardcut profiles;
- ограниченный Popcorn mild/sport;
- conflict/risk engine;
- отдельный expert mode.

# 15. Критерий готовности preset

Preset становится `validated`, только если есть:

- точный stock BIN SHA-256;
- engine/hardware/fuel metadata;
- минимум три повторяемых baseline и tuned pulls;
- wideband logs;
- knock adaptations всех цилиндров;
- IAT/ECT диапазон;
- A/B/A dyno или эквивалентная воспроизводимая проверка;
- отсутствие DTC и ухудшения drivability;
- reviewer и версия ruleset.

До этого preset имеет статус `experimental` и не может быть default.
