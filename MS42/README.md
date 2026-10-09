# Siemens MS42 (0110C6) — генератор калибровок

Второй продукт TorqueX после MS41. Тот же принцип: **ничего не пишется без
источника значения, всё проверяется контрольными суммами, ничего не угадывается.**

```bash
python3 -m unittest discover -s MS42/tests -t .   # 239 тестов, все без машины
```

## Что уже работает

* **Детектор** `tools/analyze_firmware.py` — определяет полный образ 512 КиБ,
  частичное чтение 64 КиБ и калибровку 32 КиБ, находит `OSID 0110C6` и
  идентификатор набора (например `91c6322g`), проверяет все присутствующие
  записи CRC.
* **Контрольные суммы** `tools/checksum.py` — CRC-16/ARC с тремя
  самоописывающими записями (`boot` `0x3C24`, `cal` `0x4FEE0`, `prog`
  `0x50306`). Проверено на всех 16 стоковых образах; починка признаётся только
  после независимой повторной проверки. Алгоритм — `docs/CHECKSUM_RU.md`.
* **XDF-слой** `tools/xdf.py` — 2 308 объектов из официального определения
  MS4X; адрес XDF + `0x48000` = адрес в файле.
* **Раскладка таблиц** `tools/table_layout.py` — оси, диапазоны, ориентация и
  проверка, что данные лежат внутри защищённого блока `0x48000..0x4F843`.
* **Ядро патчинга** `tools/calibration.py` — allowlist
  `policies/stage_policy.json` (**v4, 37 объектов**), квантование, монотонность,
  пересечения, пересчёт CRC и отказ писать файл, если после правки что-то не
  сходится.
* **Генератор нарядов** `tools/generate_patches.py` — 9 видов намерений:
  `vmax`, `rpm_limit`, `hardcut` пишут значения; `ignition`, `fueling`,
  `vanos`, `maf_curve`, `launch_limiter` возвращают `REQUIRES_DATA` с
  требованиями к логам и стенду. Ограничитель оборотов: выше стока —
  `acknowledge_risk`, выше **6750 об/мин** — отдельное `acknowledge_oil_pump`
  (штатный масляный насос — известное слабое место, измерений у нас нет).
* **Наряд клиенту** `tools/delivery_note.py` — собирается из отчётов, включает
  SHA-256, «что не выполнено» и статус проверки «НЕ ВЫПОЛНЕНА».
* **Stage 1 / Stage 2** `tools/stages.py` — файлы уровня как набор пакетов:
  `limits` пишется из заявки, `overrun_pops` — по профилю клиента с
  подтверждением «катализатора нет» и планом дорожной проверки, `injectors` и
  `maf` — по документам клиента (профиль форсунок, кривая датчика), `intake` —
  переносом с донорской калибровки MS43, остальное честно ждёт стенда. Stage 2
  привязан к комбинации железа и без неё не собирается.
* **Хлопки** `tools/popcorn.py` — полосы `light`/`medium` (агрессивный пресет
  отклоняется по имени), обязательные `no_catalyst`, `thermal_watch` и
  `road_validation_plan`, прижатие к сетке объекта, обе семьи таблиц (с
  кондиционером и без).
* **Донор MS43** `tools/donor.py` + `donor/ms43_to_ms42.json` — перенос карт DISA
  и диагностики ДМРВ с доказательством адресации донора (`BASEOFFSET` + проверка
  физической правдоподобности двух объектов), проверками формы, шкалы, порядка
  байт и политики; каждая ячейка помечена `donor_ms43:<dataset>:<символ>`.
  Подробности — `docs/DONOR_MS43_RU.md`.
* **База знаний MS4X** `tools/knowledge_base.py` + `knowledge/ms42_knowledge.json`
  — 30 записей с вердиктами (реализовано / по документу / нужны данные /
  исследование / отказано) и источниками; каждый символ проверяется по XDF.
  Сайт по ней — автономный `site/index.html`.

## Команды

```bash
# что это за образ и сходятся ли суммы
python3 MS42/tools/analyze_firmware.py MS42/firmware/<файл>.bin
python3 MS42/tools/checksum.py MS42/firmware/<файл>.bin

# заявка → план → файл → наряд
python3 MS42/tools/generate_patches.py --bin MS42/firmware/<файл>.bin \
  --request MS42/examples/stage1_request.example.json \
  --out patches.json --report generation.json
python3 MS42/tools/calibration.py --bin MS42/firmware/<файл>.bin \
  --patches patches.json --out build/patched.bin --report patch.json
python3 MS42/tools/delivery_note.py --generation generation.json \
  --patch patch.json --customer "<имя>" --out delivery_note.md

# файл уровня (Stage 1 или Stage 2)
python3 MS42/tools/stages.py --stage stage2 --bin MS42/firmware/<файл>.bin \
  --request MS42/examples/stage2_request.example.json \
  --injectors MS42/examples/injectors.example.json \
  --out build/stage2.bin --report build/stage2.manifest.json --note build/stage2.md

# хлопки: тот же генератор, но профиль клиента обязателен
python3 MS42/tools/stages.py --stage stage1 --bin MS42/firmware/<файл>.bin \
  --request MS42/examples/stage1_request_popcorn.example.json \
  --popcorn MS42/examples/popcorn.example.json \
  --out build/stage1_popcorn.bin --report build/stage1_popcorn.json

# впуск M54: перенос карт с донорской калибровки MS43
python3 MS42/tools/stages.py --stage stage2 --bin MS42/firmware/<файл>.bin \
  --request MS42/examples/stage2_request_intake.example.json \
  --donor-bin MS43/firmware/Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin \
  --out build/stage2_intake.bin --report build/stage2_intake.json
python3 MS42/tools/donor.py --bin MS42/firmware/<файл>.bin   # только отчёт, без записи

# база знаний и сайт
python3 MS42/tools/knowledge_base.py --check
python3 MS42/tools/knowledge_base.py --html MS42/site/index.html
```

Полный маршрут — `docs/WORKFLOW_RU.md`; примеры запросов, манифестов и нарядов —
в `MS42/examples/` (включая `popcorn.example.json`,
`stage1_popcorn_manifest.example.json`, `delivery_note_stage1_popcorn.example.md`,
`stage2_request_intake.example.json`).

## Документация

| Файл | О чём |
| --- | --- |
| `docs/CHECKSUM_RU.md` | алгоритм CRC16, три записи, смещения, проверка и починка |
| `docs/CALIBRATION_RU.md` | политика, адреса объектов, что запрещено, статусы |
| `docs/GENERATOR_RU.md` | виды намерений, параметры, гейты, статусы |
| `docs/EVIDENCE_REQUIREMENTS_RU.md` | что нужно, чтобы писать зажигание/топливо/popcorn/VANOS/MAF/launch |
| `docs/WORKFLOW_RU.md` | маршрут от стока до файла для флешера |
| `docs/STAGES_RU.md` | Stage 1 и Stage 2: пакеты, профиль форсунок, кривая ДМРВ, допуски квантования, хлопки, потолок оборотов |
| `docs/DONOR_MS43_RU.md` | перенос карт с донорской калибровки MS43: доказательство адресации, каталог пар, отказы |
| `docs/KNOWLEDGE_BASE_RU.md` | база знаний MS4X: вердикты, источники, сайт |

## Данные в репозитории

* `firmware/` — 16 полных образов 0110C6 (512 КиБ) из официального репозитория
  MS4X, 15 уникальных: пара `E46 M52TUB24 THAI RHD` и `E46 M52TUB25 EU3 LHD`
  совпадает байт-в-байт. Контрольные суммы файлов — `checksums/`.
* `definitions/` — официальные XDF MS4X: 512K (проверен на образах) и 32K
  (только для чтения калибровки; локального 32 КиБ образа нет).
* `donor/ms43_to_ms42.json` — каталог переносов MS43 → MS42 (сами донорские
  образы лежат в `MS43/`, они не копируются сюда).

Варианты 0110AD/0110CA не подтверждены: XDF обязан совпадать с прошивкой по
идентификатору, а таких образов в наборе нет.

## Границы

Инструменты не прошивают ЭБУ и не заменяют проверку на машине. Зажигание,
топливо, VANOS, замена кривой MAF и launch limiter не пишутся без измерений —
это зафиксировано в тестах (`REQUIRES_DATA`), а не только в тексте. Хлопки
пишутся только по профилю клиента с письменным подтверждением, что катализатора
нет, и планом дорожной проверки; агрессивный публичный пресет не предлагается.
