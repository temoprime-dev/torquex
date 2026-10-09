# Siemens MS43 stock firmware

Full 512 KiB firmware images for software MS430069, mirrored from the official
MS4X repository:
https://github.com/ms4x-net/ms43/tree/main/firmware

Some combinations are community ports rather than factory-released MS430069
calibrations (notably E39, Z3, and M52TUB28). See the upstream README before use.
The files do not contain a valid VIN.

Matching MS430069 Extended Load TunerPro definitions are in `definitions/`.
Use the 512K XDF with these full images; the 64K XDF is only for a calibration
read. Definitions for the other supported versions (430037/430055/430056/430066,
512K and 64K each) are in the same directory.

`firmware_archive/` — 33 stock images of the other versions used for the
per-version policies and tests: 11 files for 430037 (5 × 512 KiB + 6 calibration
reads of 64 KiB), 3 for 430055, 10 for 430056, 5 for 430066 and 4 calibration
reads for 430064 (no full image — 430064 is not writable, see below).

Extended Load and standard firmware/calibration compatibility must be
checked before flashing.

## Поддерживаемые версии калибровки

| версия | политика | что пишется |
|---|---|---|
| **430069** | `policies/stage_policy.json`, 97 символов (63 калибровки + 34 патчлиста) | калибровка по allowlist и фичи community patchlist |
| **430056** | `policies/stage_policy_430056.json`, 63 символа | только калибровка: лимитеры, хлопки (полоса + задержка), пакеты Stage 1/2 (хлопки отдельного патча не требуют) |
| **430037** | `policies/stage_policy_430037.json`, 62 символа | то же (в определении версии нет `ip_iga_tqr_amt__n__maf`) |
| **430055** | `policies/stage_policy_430055.json`, 63 символа | то же |
| **430066** | `policies/stage_policy_430066.json`, 63 символа | то же |
| 430064 | нет | отказ: в архиве только 64 КиБ чтения, полного стока и адресов нет |

Код версии читается из строки идентификации (`0x70008` в полном образе 512 КиБ,
`0x8` в 64 КиБ чтении калибровки); ядро берёт политику этой версии из реестра
`VERSION_POLICY_FILES` и отказывает (`CAL_VERSION_UNSUPPORTED`) до проверки CRC
и планирования правок, если версии в реестре нет. 64 КиБ чтения отклоняются
кодом `IMAGE_SIZE` на любой версии: в них нет boot/prog и полного набора
записей CRC. Фичи патчлиста доступны **только на 430069** — его basedata снята с
18 стоков именно этой версии, а байтовые маски на других версиях не доказаны
(`PATCHLIST_FEATURE_UNSUPPORTED`).

Адреса версий берутся из **их собственных** официальных определений, границы —
из их стоков, и это проверено независимо: 512 КиБ и 64 КиБ определения версии
совпадают по адресам (274 сверки), «модельно-стабильные» таблицы побайтово
совпадают с эталоном 430069 ровно по адресам политики, стоки всех версий лежат
в границах. Разбор, источники и доказательства — `docs/VERSION_COVERAGE_RU.md`,
почему нельзя переносить адреса между версиями — `docs/CALIBRATION_RU.md` §5a.

## Консоль Windows

CLI MS43 печатают только cp1251-совместимые символы, а `console.prepare_stdout()`
страхует от падения на древней консоли; это проверяет
`tests/test_cli_console.py` (в том числе запуском с `PYTHONIOENCODING=cp1251`).
Наряды и манифесты пишутся файлами в UTF-8.

## Контрольные суммы

`MS43/tools/checksum.py` — проверка и пересчёт CRC-16/ARC (boot 0x3C24,
program 0x6FDE0, calibration 0x73FE0; в 64K-чтении только calibration @ 0x3FE0).
Разбор схемы и грабли — `docs/CHECKSUM_RU.md`, результат прогона по всему
корпусу — `checksums/CRC_VERIFY_RU.md`, тесты — `tests/test_checksum.py`.

```bash
python3 MS43/tools/checksum.py MS43/firmware/*.bin MS43/firmware_archive/*.bin
python3 -m unittest discover -s MS43/tests -t .   # 189 тестов MS43
python3 -m unittest discover                      # весь репозиторий (791 тест)
```

Суммы `_mon` (0x6FDAE, 0x72FFC) прошивка сверяет во время работы: статически
они не проверяются, инструмент показывает их со статусом `unverified`.
Образы-исключения (3 файла) перечислены в `docs/CHECKSUM_RU.md` §5 —
не использовать их как эталон границ и не заливать без пересчёта сумм.

## Определения XDF

`MS43/tools/xdf.py` — чтение определений TunerPro: `BASEOFFSET 0x70000`
(512K) и `0` (64K), 16-битные элементы младшим байтом вперёд, статические оси
и оси-ссылки. Двадцать констант `c_t_*` в определениях MS4X записаны с
дефектным именем переменной (`0.1*X00` вместо `0.1*X`); загрузчик читает их
как `0.1*X` и печатает список подстановок. Разбор — `docs/XDF_RU.md`.

В `definitions/` лежат 10 определений: 512K и 64K для 430037, 430055, 430056 и
430066 плюс оба определения 430069 (64 КиБ — только чтение).
Это те же файлы, что публикует MS4X; их sha256 — в
`checksums/DEFINITIONS_SHA256SUMS.txt`, скачать/проверить — `fetch_definitions.py`
(`--check`, `--all`, `--version 430064`).

```bash
python3 MS43/tools/xdf.py                        # отчёт по определениям
python3 MS43/tools/fetch_definitions.py --check  # суммы 10 определений
```

## Политика записи (allowlist)

`MS43/policies/stage_policy.json` — 97 символов MS430069 с адресами
(абсолютные смещения в образе 512 КиБ), границами и измеренными стоковыми
значениями: 63 калибровочных (включая группу ПХХ/хлопков) и 34 символа фич из
community patchlist (`requires_patch`); часть символов — `read_only`
(справочные). На остальные покрытые версии собираются свои политики:
430056/430055/430066 — 63 символа, 430037 — 62. Каталог — `MS43/tools/policy.py`.
Разбор и правила — `docs/ALLOWLIST_RU.md`, `docs/TORQUE_RU.md`,
`docs/PATCHLIST_RU.md`, `docs/VERSION_COVERAGE_RU.md`.

```bash
python3 MS43/tools/policy.py --build-all      # все политики версий
python3 MS43/tools/policy.py --build-version 430056   # одна версия
```

## Патчи сообщества (MS4X patchlist)

`MS43/patches/ms430069_community_patchlist_v2.9.2.json` — извлечённый каталог
community patchlist v2.9.2 (27 патчей / 67 записей / 46 символов, sha256
источника `ac734912…6eadd`), `MS43/tools/patches.py` — применение и снятие
патчей с пересчётом сумм и пост-проверкой «изменились только записи патча и
6 байт CRC». Категории патчлиста позиционные — рабочие группы заданы нами
(`PATCH_GROUPS`). Разбор, ворота и значения фич — `docs/PATCHLIST_RU.md`,
рекомендации по обновлению до 430069 — `docs/UPGRADE_430069_RU.md`.

```bash
python3 MS43/tools/patches.py --list
python3 MS43/tools/patches.py --check MS43/firmware/Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin
python3 MS43/tools/patches.py --check <сток другой версии>   # отказ, код выхода 2
```

Патчлист подключён **только к 430069**: `PATCHLIST_SOFTWARE = ("430069",)`,
ядро (`apply_patches`/`revert_patches`), `--check` и `calibration.plan_changes`
отказывают на любой другой версии (`PATCHLIST_FEATURE_UNSUPPORTED`), в политиках
430037/430055/430056/430066 нет ни одного символа патчлиста, а генератор
отклоняет любое фичевое намерение вне 430069.

## Генератор Stage 1/2

`MS43/tools/stages.py` — заявка клиента → набор пакетов (ограничители, фичи
патчлиста, хлопки, full load, VANOS, зажигание, топливо, ДМРВ, впуск, форсунки,
выпуск, момент) → файл через ядро + манифест + русский наряд. Пакеты без
логов/документов честно отвечают `REQUIRES_DATA`. Ворота: `acknowledge_risk`,
`acknowledge_oil_pump` (6750), хлопки только без катализатора (полосы
`light`/`medium`, задержка хлопка ≤1.0/≤2.5 с), NLS отклоняется, Stage 2 без
блока `hardware` — отказ. Лимитеры и хлопки пишутся на всех покрытых версиях,
фичи патчлиста — только на 430069 (на других версиях
`PATCHLIST_FEATURE_UNSUPPORTED`). Разбор — `docs/STAGES_RU.md`,
`docs/VERSION_COVERAGE_RU.md`, примеры — `MS43/examples/`.

```bash
python3 MS43/tools/stages.py --stage stage1 \
    --bin MS43/firmware/Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin \
    --request MS43/examples/stage1_request.example.json \
    --out build/stage1.bin --report build/stage1.manifest.json --note build/stage1.md
```

## Ядро патчинга калибровки

`MS43/tools/calibration.py` — запись калибровки строго по allowlist: границы,
точность квантования, Δ на правку, лимит ячеек, монотонность, `requires_patch`,
пересчёт CRC нашим ядром и доказательство, что не изменено ничего лишнего.
Порядок: патчи → калибровка → CRC → пост-проверка. Разбор — `docs/CALIBRATION_RU.md`.

```bash
python3 MS43/tools/calibration.py --image <сток>.bin \
    --patch "[PATCH] Launch Control & Rolling Anti Lag" --set c_n_max_lc=4000 --dry-run
```
