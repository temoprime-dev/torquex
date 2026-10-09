# Community patchlist MS4X для MS430069 — разбор и правила работы

Документ закрывает пункт 4 задания (`MS43/TASK_RU.md`) в части «добыть фичи из
патчлиста»: что именно есть в community patchlist, как это проверено на реальных
образах и что из этого мы предлагаем клиенту. Код — `MS43/tools/patches.py`,
каталог — `MS43/patches/ms430069_community_patchlist_v2.9.2.json`.

**Патчлист — только 430069.** Его `basedata` снята с 18 стоков 430069, и другой
версии в патчлисте нет (сообщество прямо пишет, что по 430056 разработки не
велось). Поэтому `patches.py` знает `PATCHLIST_SOFTWARE = "430069"` и на любой
другой версии отказывает кодом `PATCHLIST_FEATURE_UNSUPPORTED` до всякой записи
(`patchlist_guard`); генератор отклоняет запрос любой фичи патчлиста вне 430069
тем же кодом. Байтовые маски на 430037/430055/430056/430066 **не проверялись** —
переносить их туда нельзя, пока не будет отдельного доказательства (дизассемблер
+ сверка с 430069). Что при этом всё-таки пишется на версиях 430037/430055/430056/
430066 — калибровка по их собственным политикам (`VERSION_COVERAGE_RU.md`).

## 1. Источник

| Параметр | Значение |
|---|---|
| файл | `Siemens_MS43_MS430069_Community_Patchlist_v2.9.2.xdf` |
| страница | <https://www.ms4x.net/index.php?title=TunerPro_MS43_Community_Patchlist> |
| sha256 | `ac7349125756548708c03327dc11ce920b93905a252374f25b78ebd054e6eadd` |
| размер | 98 048 → 112 898 байт (v2.9.2) |
| применимо к | полный образ MS430069, 512 КиБ (для MS42 есть отдельный патчлист) |
| извлечено | 27 патчей, 67 записей, 46 символов, 17 групп |

Это **не** файл прошивки: XDF описывает байтовые правки (`XDFPATCHENTRY` с
`basedata`/`patchdata`), которые TunerPro применяет к стоковому 512K-образу.
Версия v2.9.2 — последняя в клоне `/tmp/ecudefs/BMW/MS43_M54`; она же лежит в
репозитории MS4X. Файл в репозиторий проекта не кладём (это файл сообщества),
в Git идёт только извлечённый каталог с провенансом.

Почему 430069, а не 430056: 430069 — последняя заводская версия BMW
(2005), на неё портированы калибровки E39, она нужна для 6-ступенчатой коробки и
круиза на 6-й передаче; 430056 оставлены «для истории» с рекомендацией переходить
на 430069. Подробности обновления — `MS43/docs/UPGRADE_430069_RU.md`.

## 2. Что проверено на 18 стоковых образах

Проверка — сравнение `basedata` каждой записи с байтами 18 стоков 430069 из
`MS43/firmware` (`patches.py --extract` делает это автоматически):

* **26 патчей из 27** — basedata совпала со всеми 18 образами. Это значит, что
  патч описывает именно наш сток и применим к любому из них.
* `[PATCH] Remove Deprecated Launch Control` — 0/18. Его `basedata` описывает
  состояние **после** старого LC-патча v2.5, а `patchdata` совпадает со стоком:
  на чистом стоке это no-op, удалять нечего. Поэтому патч не предлагаем (см. §5).
* `[PATCH] Clear Immobilizer Data` — на стоках уже «applied»: область ISN
  (0x3ED4, 76 байт) в заводских файлах пуста (0xFF). Патч имеет смысл только для
  ЭБУ, который уже был привязан к машине/EWS.
* Вне записей патча и 6 байт CRC после применения не меняется **ничего** —
  это отдельная пост-проверка движка (см. §7).

## 3. Группы патчей

Категории внутри патчлиста проставлены **позиционно** (`CATEGORYMEM index` —
ссылка на индекс, а не на имя) и перемешаны: таблицы CAN подписаны «Immobilizer
Bypass», `id_maf_tab__v_maf` — «Boost Control», а `c_t_conf_*` MIL-индикатора —
«Rolling Anti Lag Parameters». Поэтому рабочие группы заданы нами
(`PATCH_GROUPS` в `patches.py`), а источник сохранён в поле `category` только для
провенанса.

| Группа | Патчи | Фича |
|---|---|---|
| `patch_checksum_bypass` | Checksum Bypass | служебное (не предлагаем) |
| `patch_immobilizer` | Immobilizer Bypass, + DTC Fix, Clear Immobilizer Data | EWS |
| `patch_lc` | Launch Control & Rolling Anti Lag, [DATA] LC & RAL Default Values | launch control + RAL |
| `patch_ign_cut` | Ignition Cut Limiter | жёсткая отсечка по зажиганию |
| `patch_alpha_n` | Alpha/N | пересчёт нагрузки для ITB/больших форсунок |
| `patch_map_reduction` | Map Reduction | работать только с таблицами `tco_1` |
| `patch_maf_fl` | MAF Based Full Load Enrichment, [DATA] … - M54B30 | полная нагрузка по ДМРВ |
| `patch_maf_hack` | MAF Sensor Hack 2048 / 4096 кг/ч | расширение диапазона ДМРВ |
| `patch_mcluster` | M Cluster LED Support, [DATA] M3 Cluster Default Values | LED-табло M3 |
| `patch_ds2` | DS2 Logging Feature Enhancement, DS2 Speed Increase, DS2 Baudrate… | логирование |
| `patch_mil_knk` | MIL Knock Indicator, [DATA] MIL Knock Indicator | индикатор детонации |
| `patch_boost` | Boost Control | управление наддувом |
| `patch_can` | Custom CAN - OLM Boost Gauge | индикатор наддува в шине |
| `patch_vss` | E3x Vehicle Speed Sensor Accuracy Improvement | точность датчика скорости |
| `patch_fuel_cut_sw` | Fuel Cut Table Switch Over Cruise Control | выбор ПХХ-карт круизом |
| `patch_legacy_cleanup` | Remove Deprecated Launch Control (+ DATA) | совместимость с v2.5 |
| `patch_migration_fix` | Daniel_F. 430056 Update Fix | лечение незапуска после миграции |

Патчи `patch_legacy_cleanup` и `patch_migration_fix`, а также Checksum Bypass и
иммобилайзер — **служебные**: они не добавляют мощность и не входят в клиентский
пресет. Символы патчлиста в политике (`stage_policy.json`) есть только у 8 групп:
`patch_lc`, `patch_mil_knk`, `patch_mcluster`, `patch_ds2`, `patch_maf_fl`,
`patch_maf_hack`, `patch_map_reduction`, `patch_can`.

## 4. Все патчи: адреса, области, ворота

Адрес — абсолютное смещение в 512K-образе (совпадает с `mmedaddress` + 0x70000).
«Сверка» — сколько из 18 стоков совпало по `basedata`.

| Патч | Группа | Зап. | Области | Адреса записей | Сверка | Ворота |
|---|---|---|---|---|---|---|
| Immobilizer Bypass | patch_immobilizer | 3 | cal/program | 0x600D8, 0x53BF8, 0x70A6E | 18/18 | подтверждение `immo_change` |
| Immobilizer Bypass DTC Fix | patch_immobilizer | 1 | program | 0x2E2F0 | 18/18 | подтверждение `immo_change` |
| Clear Immobilizer Data | patch_immobilizer | 1 | boot | 0x03ED4 | 18/18 | подтверждение `immo_virginize` |
| Checksum Bypass | patch_checksum_bypass | 4 | cal/program | 0x70E07, 0x6FFB0, 0x63595, 0x73FFE | 18/18 | не предлагаем |
| Remove Deprecated Launch Control | patch_legacy_cleanup | 2 | program | 0x58C14, 0x4AACA | 0/18 | не предлагаем |
| [DATA] Remove Deprecated Launch Control | patch_legacy_cleanup | 1 | cal | 0x71316 | 18/18 | не предлагаем |
| Launch Control & Rolling Anti Lag | patch_lc | 4 | program | 0x58C14, 0x2C75A, 0x4ABD0, 0x4AB60 | 18/18 | предлагаем |
| [DATA] LC & RAL Default Values | patch_lc | 1 | cal | 0x71316 | 18/18 | предлагаем |
| Ignition Cut Limiter | patch_ign_cut | 1 | program | 0x58524 | 18/18 | предлагаем + риск |
| Alpha/N | patch_alpha_n | 3 | program | 0x3B5E2, 0x3B604, 0x580B0 | 18/18 | предлагаем |
| Map Reduction | patch_map_reduction | 6 | cal | 0x76866, 0x76A98, 0x766F3, 0x76673, 0x76733, 0x766B3 | 18/18 | предлагаем |
| MAF Based Full Load Enrichment | patch_maf_fl | 8 | program | 0x2FF6C, 0x3D3D6, 0x3D3E2, 0x3D402, 0x3D588, 0x4473C, 0x44B56, 0x6EA7A | 18/18 | предлагаем |
| [DATA] MAF Based FL Default Values - M54B30 | patch_maf_fl | 3 | cal | 0x75D6F, 0x70C3E, 0x7BEE4 | 18/18 | предлагаем |
| MAF Sensor Hack 2048kg/h | patch_maf_hack | 2 | program | 0x3B4C3, 0x3B4E7 | 18/18 | альтернатива 4096 |
| MAF Sensor Hack 4096kg/h | patch_maf_hack | 2 | program | 0x3B4C3, 0x3B4E7 | 18/18 | альтернатива 2048 |
| M Cluster LED Support | patch_mcluster | 2 | program | 0x55924, 0x3FCBA | 18/18 | предлагаем |
| [DATA] M3 Cluster Default Values | patch_mcluster | 4 | cal | 0x78520, 0x78519, 0x74857, 0x7484F | 18/18 | предлагаем |
| DS2 Logging Feature Enhancement | patch_ds2 | 5 | program | 0x25CC0, 0x26A10, 0x4A8D8, 0x4A9D8, 0x4AAD8 | 18/18 | предлагаем |
| DS2 Speed Increase | patch_ds2 | 1 | program | 0x30016 | 18/18 | предлагаем |
| DS2 Baudrate Switch Engine Stopped Bypass | patch_ds2 | 1 | program | 0x25B84 | 18/18 | предлагаем (экспериментальный) |
| MIL Knock Indicator | patch_mil_knk | 3 | program | 0x360EA, 0x3FD20, 0x55830 | 18/18 | предлагаем |
| [DATA] MIL Knock Indicator | patch_mil_knk | 1 | cal | 0x707D0 | 18/18 | предлагаем |
| Boost Control | patch_boost | 2 | cal/program | 0x20320, 0x795A2 | 18/18 | предлагаем (только с железом) |
| Custom CAN - OLM Boost Gauge | patch_can | 2 | program | 0x5594A, 0x4E000 | 18/18 | предлагаем |
| E3x VSS Accuracy Improvement | patch_vss | 1 | program | 0x607F8 | 18/18 | предлагаем |
| Fuel Cut Table Switch Over Cruise | patch_fuel_cut_sw | 2 | program | 0x2A3F2, 0x2C968 | 18/18 | предлагаем |
| Daniel_F. 430056 Update Fix | patch_migration_fix | 1 | program | 0x6361E | 18/18 | предлагаем только при незапуске |

### Взаимные пересечения

Единственное пересечение — альтернативы MAF-hack: `2048 кг/ч` и `4096 кг/ч`
пишут разные байты в 0x3B4C3 и 0x3B4E7. Движок отказывается применять их вместе
(`patches.conflicts`). Остальные 20+ патчей применяются совместно: 22 предлагаемых
патча вместе меняют 1499 байт в boot/cal/program, все три CRC сходятся.

### Патчи без basedata — снять нельзя

У 5 патчей (12 записей) в патчлисте нет `basedata`: `[PATCH] Clear Immobilizer
Data`, `[DATA] Remove Deprecated Launch Control`, `[PATCH] Map Reduction`,
`[DATA] LC & RAL Default Values`, `[DATA] MAF Based FL Default Values - M54B30`.
Движок их применяет, но **отказывает в revert** с явным сообщением: восстановить
исходные байты нечем, значит нужен стоковый бэкап. Это осознанное ограничение, а
не баг: DATA-патчи пишут калибровочные ячейки «по умолчанию», которые на стоке
пусты (0xFF/0x00).

## 5. Ворота: что не предлагаем и почему

* **Checksum Bypass** — не предлагаем. Мы всегда пересчитываем суммы своим ядром
  (`MS43/tools/checksum.py`, см. `CHECKSUM_RU.md`), а bypass нужен только старым
  инструментам. Внешнее подтверждение вреда есть в теме патчлиста: на 430069 он
  даёт ошибку **115 TPS-Adaption**, которая исчезает после снятия патча; у
  флешера MSS5X есть баг с порчей сумм, из-за которого bypass иногда советуют как
  «костыль».
* **Remove Deprecated Launch Control** — не предлагаем: на чистом стоке 430069
  патч ничего не делает (см. §2). Он нужен только тем, у кого стоит старый LC из
  v2.5 и ниже, то есть приходит с готовым файлом, а не со стока.
* **Иммобилайзер** — два уровня подтверждения:
  * `[PATCH] Immobilizer Bypass` (+ DTC Fix) требует `immo_change`: отключает
    проверки EWS, машина перестаёт проверять ключ (и её проще угнать).
  * `[PATCH] Clear Immobilizer Data` требует `immo_virginize`: сбрасывает ISN,
    **записывается только в bootmode** и требует последующей синхронизации с EWS
    (INPA). По опыту сообщества (e46fanatics, тема патчлиста) «слепой» EWS-delete
    на 430069 — «hit and miss»: типовые ошибки 100/115/128, а рабочий рецепт —
    сначала 430056 EWS-delete в bootmode, затем 430069 полной записью в обычном
    режиме, затем синхронизация EWS через INPA.
* Служебные патчи (`Daniel_F. 430056 Update Fix`, DS2 Baudrate…) включаются
  только по конкретному симптому, а не «на всякий случай»: Fix лечит незапуск
  после миграции с файла Daniel_F и в остальных случаях бесполезен.

## 6. Разобранные значения по фичам

Все числа ниже сняты из `patchdata` патчлиста и перепроверены на 18 стоках.

### Launch Control и RAL (группа `patch_lc`)

`[DATA] LC & RAL Default Values` пишет 14 байт по 0x71316:

| Адрес | Символ | Байт | Значение по умолчанию |
|---|---|---|---|
| 0x71316 | `c_tco_max_lc_nls_ral_iga_rtd` | 0xD8 | 114 °C — предел ОЖ для позднего зажигания |
| 0x71317 | `c_iga_rtd_lc_nls_ral` | 0x48 | −21 °CRK — дополнительный поздний угол |
| 0x71318 | `c_pvs_min_lc_nls` | 0x90 | 67.5 °PVS — порог педали для LC/NLS |
| 0x71319 | `c_conf_lc` | 0x01 | 1 = LC включён по выжатому сцеплению |
| 0x7131A | `c_conf_lc_iga_rtd` | 0x00 | 0 = доп. поздний угол выключен |
| 0x7131B | `c_n_max_lc` | 0x6E | 3520 об/мин — предел оборотов при LC |
| 0x7131C | `c_vs_max_lc` | 0x14 | 20 км/ч — выше LC отключается |
| 0x7131D..1F | — | 0x00 | резерв |
| 0x71320 | `c_conf_n_max_max_td` | 0x00 | 0 = жёсткая отсечка зажиганием выключена |
| 0x71321 | — | 0x00 | резерв |
| 0x71322 | `c_conf_ral` | 0x00 | 0 = RAL выключен |
| 0x71323 | `c_pvs_min_ral` | 0x90 | 67.5 °PVS — порог педали для RAL |

Семантика переключателей — из описаний XDF: `c_conf_lc`: 0 = выключено, 1 =
включено по сцеплению, 2 = по сцеплению и «взведённому» круизу;
`c_conf_lc_iga_rtd`, `c_conf_ral`, `c_conf_n_max_max_td`: 0/1 = выкл/вкл.
То есть установка патча сразу даёт **включённый** LC на 3520 об/мин — для
клиента это отдельный пункт запроса, а не «молчаливый дефолт»: в генераторе
значения пишутся явно и отражаются в наряде.

Важно: NLS (no-lift shift) в патчлисте **не реализован** — категория
«No Lift Shift Parameters - Not Implemented Yet» и описание `[PATCH] Alpha/N`
говорят прямо. Обещать NLS клиенту нельзя (это `нельзя` до появления кода).

### Ограничители (не патчи, а калибровка)

Хлопки/лимитеры из задания — это в основном не патчи, а калибровка штатных
таблиц: `id_n_max_mt__gear` (мягкий), `id_n_max_max_mt__gear` (жёсткий),
`id_n_max_vs_max_*`, `c_vs_max_*`, ПХХ (`puc`) — они уже в политике; причём это
единственная часть, которая пишется **на всех покрытых версиях** (430069, 430037,
430055, 430056, 430066), потому что калибровка защищена границами из стоков
версии, а не патчлистом. Сюда же хлопки: карты ПХХ, пара «A/C on» и задержка
хлопка (`c_t_puc_deacc_vs`, `ip_t_puc_deacc_1..5__n__maf_mmv`) — все адреса из
определения версии (`VERSION_COVERAGE_RU.md` §4).

Патч нужен только для двух вещей: «жёсткой» отсечки по зажиганию
(`[PATCH] Ignition Cut Limiter`) и LC/RAL — и обе доступны **только на 430069**.
Штатная отсечка MS43 — по топливу (fuel cut): её поведение задаётся калибровкой
и работает на всех покрытых версиях; обнуление зажигания вместо топлива — это уже
патч, поэтому на других версиях отказ `PATCHLIST_FEATURE_UNSUPPORTED`.

### Ignition Cut Limiter

Описание патчлиста предупреждает прямо: обнуление dwell при жёстком ограничителе
«может повредить катушки, центральную звёздочку, двигатель и выпуск». Значит:
предлагается только по явному запросу с `acknowledge_risk`, вместе с моторным
лимитом (обороты задаёт калибровка `id_n_max_max_*`), и в наряде печатается
предупреждение.

### MIL Knock Indicator

`[DATA]` пишет 0x707D0 = `03 96`: биты 0 и 1 (`lv_mil_knk_lv_1` — лёгкая
детонация, `lv_mil_knk_lv_2` — тяжёлая) включены, `c_t_max_knk_mil` (0x707D1) =
0x96 = 1.5 с. На стоке 0x707D0..0x707D1 = `00 00`, то есть индикация выключена.
Оба флага живут в одном байте, поэтому в политике они **только чтение**: байт
выставляет DATA-патч значением `0x03`, а калибровка пишет лишь время
`c_t_max_knk_mil` — иначе поштучная запись потеряла бы второй флаг.
Это **индикатор**, а не замена штатной защите по детонации: защита сохраняется
(`TASK_RU.md`), индикатор лишь показывает события водителю.

### M Cluster LED Support

`[DATA] M3 Cluster Default Values` пишет опорные точки LED: 0x78520 (`id_icl_n_led__n`)
= `00 30 40 50 60 70`, 0x78519 (`id_icl_toil_led__n`) = `70 60 50 40 30 20 70`,
0x74857 = `06 C0 C2 C3 C5 C6 C8 00`, 0x7484F = `07 88 8E 94 9B A1 A7 D3`. На
стоке это нули (LED выключены). Требуется кластер M3 — иначе фича не видна.

### MAF Based Full Load Enrichment

`[DATA] … - M54B30` пишет: `id_maf_fl_ivvt__n` 0x75D6F (40 байт → 0x37),
`c_maf_fl_accin` 0x70C3E = 0x37 (≈300 мг/стк), таблицу `ip_ti_fl__n__maf`
0x7BEE4 (192 байта, 16x12, база 0x80). На стоке 0x7BEE4 — 0xFF. Это **только
M54B30**: для B22/B25 таблицу надо строить заново по логам (в патчлисте её нет).

### Map Reduction

Патч заставляет ЭБУ работать на таблицах `tco_1` (injection/idle/part load,
ignition, VANOS все режимы). Внимание из описания: при **активной ошибке**
используются `ip_tib__n__maf`, `ip_igab_is__n__maf`, `ip_igab__n__maf`, то есть
аварийные карты тоже придётся держать корректными. Уменьшает число карт для
тюнинга — полезно на стенде, рискованно как «первый патч».

### DS2, CAN, VSS, Fuel Cut, Boost

* DS2-логгирование добавляет команду `0B B0` (момент по всем цилиндрам, банк 2
  форсунок, расчётное давление во впуске, активная таблица VANOS, нагрузка,
  передача, задняя лямбда, DMTL, крышка бака, адаптация по RON, индицируемый
  момент) и ускоряет обработку DS2 — можно логировать без CAN-платы.
* `[PATCH] Custom CAN - OLM Boost Gauge` добавляет расчётное давление во впуске в
  CAN для кластеров с указателем уровня масла; в кластере нужно закодировать
  `OELNIVEAU_M3 = aktiv`. Таблицы CAN (0x6C494/0x6C496) в политике **только
  чтение**.
* VSS-патч убирает деление сигнала датчика скорости — актуально для свапов в
  E30/E34 с датчиком в редукторе (`c_vs_fac` ≈ 1096 имп/км).
* Fuel Cut Switch переводит выбор карт ПХХ с реле кондиционера на главный
  выключатель круиза.
* Boost Control — управление наддувом через выход клапана адсорбера; без
  переделанного железа включать нельзя.

## 7. Движок: что он гарантирует

`MS43/tools/patches.py` (23 теста, `MS43/tests/test_patches.py`):

1. **Каталог проверяется на целостность**: 27 патчей / 67 записей, sha256
   источника, при извлечении — все патчи обязаны быть в `PATCH_GROUPS`, а группы
   и оговорки в JSON обязаны совпадать с кодом (иначе `PatchError`).
2. **Проверка образа** (`check_image`): по каждой записи — `applicable`
   (совпала basedata), `applied` (уже patchdata), `conflict`, `partial`.
3. **Применение** (`apply_patches`): отказ, если байты не совпали с basedata
   (значит образ уже правлен), идемпотентность для уже применённых записей,
   отказ при конфликте альтернатив, ворота и подтверждения (`allow_gated`,
   `declarations`).
4. **Пересчёт и пост-проверка**: CRC считает наше ядро
   (`checksum.repair_image`), после чего движок проверяет, что изменились только
   байты записей патча и 6 позиций CRC. Любой лишний изменённый байт — отказ.
5. **Revert** — байт-в-байт к исходнику (проверено тестом), и отказ там, где в
   патчлисте нет basedata.
6. **Пакетный прогон**: все 18 стоков принимают предлагаемый набор патчей, все
   три CRC сходятся.

CLI: `python3 MS43/tools/patches.py --list | --check ФАЙЛ | --apply ФАЙЛ --patch "…"
--out ФАЙЛ | --revert … | --extract ИСТОЧНИК.xdf [--allow-gated --declaration X]`.

## 8. Символы патчлиста в политике

46 символов каталога → 34 в политике (`origin: patchlist`, поле `requires_patch`).
Исключены 12:

* 8 объектов зоны загрузчика (`UIF *`, `User Information Fields`, `ISN Value`,
  `ECU Number`) — это идентификация ЭБУ/кузова, а не калибровка; их правка —
  отдельная операция привязки к машине и в нашей политике запрещена;
* `Softwareversion` (0x70042) — идентификационная строка, к фичам не привязана;
* `ldp_n_vim__pvs_fl_vim`, `ldpm_maf_8`, `ldpm_n_11` — оси таблиц без
  подтверждённого владельца-фичи: в патчлисте у них нет ни категории, ни патча,
  привязка по имени не доказана. Пока не подтвердим — в политику не попадут
  (правило «нет источника в базе знаний — нет символа»).

Поле `requires_patch` в самом патчлисте ненадёжно (позиционные категории),
поэтому рабочая привязка символа к патчу живёт в `stage_policy.json`
(`SYMBOL_OVERRIDES` в `policy.py`) и проверяется тестами.

## 8a. Как патчлист отделён в коде (three lines of defence)

1. **Каталог / ядро**: `PATCHLIST_SOFTWARE = ("430069",)`,
   `patches.refuse_unsupported_version()` вызывается в `apply_patches` и
   `revert_patches`; `patches.py --check` на чужой версии не разбирает basedata,
   а отказывает (код выхода 2). `calibration.plan_changes(..., patches=...)` на
   версии без патчлиста отклоняет план кодом `PATCHLIST_FEATURE_UNSUPPORTED` —
   то есть ни `--dry-run`, ни `--out` не дадут ложного «PLANNED».
2. **Политики**: в файлах 430037/430055/430056/430066 нет ни одного символа
   патчлиста (`origin: xdf`, `requires_patch = None`); патчлист собирается только
   в `stage_policy.json` (430069) через `build_version_policy(include_patchlist=False)`.
3. **Генератор**: любое намерение из `stages.PATCHLIST_INTENTS` на версии, кроме
   430069, отклоняется кодом `PATCHLIST_FEATURE_UNSUPPORTED`; на 430069
   нереализованные фичи (`maf_hack`, `maf_full_load`, `map_reduction`, `alpha_n`,
   `fuel_cut_cruise`, `boost`, `immo_bypass`, `immo_virginize`) — кодом
   `PATCHLIST_FEATURE_NOT_IMPLEMENTED`; реализованные (LC/RAL, ignition cut, MIL,
   кластер M3, DS2, VSS) пишутся.

Тесты этого правила: `MS43/tests/test_patches.py` (`VersionGuardTests`),
`MS43/tests/test_version_coverage.py` (`PatchlistScopeTests`).

## 9. Что дальше

Патчлист закрывает «I/O» для фич: LC/RAL, ignition cut, alpha/N, map reduction,
MAF-FL, MAF-hack, кластер M3, DS2-логирование, MIL-детонацию, CAN-индикатор,
VSS, fuel-cut switch, boost — все **на 430069**. Дальше по `TASK2_RU.md`: фичи
патчлиста остаются эксклюзивом 430069, а расширять покрытие можно только
калибровкой по определениям версий. Если появится задача перенести фичу на
другую версию — сначала отдельное доказательство совместимости байтовых масок
(дизассемблер, сверка basedata с образами версии), и только потом код. NLS
остаётся `нельзя` до появления кода (в патчлисте он не реализован).

## 10. Воспроизведение

```bash
python3 MS43/tools/patches.py --extract /путь/к/Siemens_MS43_MS430069_Community_Patchlist_v2.9.2.xdf
python3 MS43/tools/patches.py --list
python3 MS43/tools/patches.py --check MS43/firmware/Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin
python3 MS43/tools/patches.py --apply MS43/firmware/Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin \
    --patch "[PATCH] Launch Control & Rolling Anti Lag" --out /tmp/lc.bin
python3 -m unittest discover -s MS43/tests -t .
```
