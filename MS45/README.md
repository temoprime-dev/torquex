# Siemens MS45 stock calibration collection

The MS45.0 and MS45.1 partial calibration files are mirrored from the public
stock-ROM collection maintained with the RomRaider definitions:
https://github.com/pazi88/Siemens_ms45_RR-definitions/tree/master/Stock%20ROMs

Included sets:
- `MS45.0_456BO00S_E83/`: 456BO00S E83 partials, 118,784 bytes each.
- `MS45.1_457LO02S/`: 457LO02S/4570LO00 partials listed by ZUSB, 118,784 bytes each.

MS4X's firmware page independently lists the 457LO02S ZUSB variants:
https://www.ms4x.net/index.php?title=Firmware_Files#Siemens_MS45.1

These are partial calibrations, not complete 1 MiB flash images. Always match
ZUSB, hardware, chassis, engine, transmission, region, and steering variant.

Definitions are in `definitions/`: 116K and 1024K TunerPro XDF files plus the
multi-revision RomRaider XML. Use 116K XDFs with the partials in this repository;
1024K XDFs are only for full reads. The MS45.0 stock set is named 4560BO00S while
the available XDF identifies itself as 456BO02S, so its software ID, addresses,
and conversions require validation before any write. Development A2L files are
kept in `a2l/` as reverse-engineering references, not as proven drop-in matches.

## Дополнения 2026-10-06

* `firmware_spdaten/` — стоки из SP-Daten BMW (175 калибровок, 10 программ), собраны `tools/spdaten_extract.py`.
* `firmware_dumps/` — реальные чтения 456BN00S (1 MiB внешняя флэш + 448K MPC).
* `damos/` — DAMOS 4520D30S, `Ms45damos.bin`.
* `TASK_RU.md` — задание: генератор Stage-файлов, **только калибровочный регион** (иммо/VIN не трогаем).

## Инструменты и документы MS45

Код (единственный путь к записи — ядро):

| Файл | Что делает |
| --- | --- |
| `tools/checksums.py` | обе суммы: CRC-32/MPEG-2 @ `0x100` и MO3; проверка, отчёт, пересчёт |
| `tools/xdf.py` | определения XDF: объекты, формулы, размерности, область калибровки |
| `tools/xml_defs.py` | XML-определения сообщества: несколько заводских ROM в одном файле (источник адресов для 4560BN00S) |
| `tools/a2l_probe.py` | разбор пре-серийного A2L (исследование: почему он не подходит для заводских версий) |
| `tools/policy.py` + `policies/stage_policy.json` | allowlist: сборка из данных и проверка (3 семейства) |
| `tools/calibration.py` | ядро: план → ячейки → суммы → пост-проверка; коды отказа — в `docs/CORE_RU.md` |
| `tools/stages.py` | генератор Stage 1/2: пакеты, ворота, файл, наряд, манифест, проверка по манифесту |
| `tools/spdaten_extract.py` | сборка образов из SP-Daten (`.0PA`/`.0DA`) |
| `tools/pathfmt.py` | пути в артефактах всегда POSIX: политика и манифест не зависят от ОС |

Документы (`docs/`):

| Файл | О чём |
| --- | --- |
| `ANALYSIS_WEB_RU.md` | веб-анализ: источники, даты, вердикт по каждому пункту, `REQUIRES_DATA` |
| `CHECKSUMS_MS45_RU.md` | схема обеих сумм, структура заголовка, порядок пересчёта |
| `CALIBRATION_MAP_RU.md` | карта: адреса, стоковые границы, что и почему запрещено |
| `ALLOWLIST_RU.md` | allowlist: что разрешено писать и почему; адреса всех десяти семейств |
| `CORE_RU.md` | ядро: проверки, коды отказов (включая `SOURCE_NOT_STOCK`), границы, квантование |
| `STAGES_RU.md` | Stage 1/2, пакеты, ворота продукта, статусы, наряд, каталог «что требует данных», проверка по манифесту |
| `WORKFLOW_RU.md` | маршрут: заявка → файл → наряд → прошивка → проверка |
| `VERSION_COVERAGE_RU.md` | покрытие версий: 239 файлов, 10 семейств, что поддержано |
| `BN00S_DEFINITIONS_RU.md` | семья E60: как получены адреса и почему им можно верить |
| `FAMILY_CALIBRATION_EVIDENCE_RU.md` | доказательства и запреты по семи семействам без XDF: отпечатки, таблицы адресов, что осталось закрытым |

Поддержаны **все десять** семейств MS45 (лимиты `vmax`/`rev_limit_*` и
задержки ПХХ — у всех); семейства вне списка отвергаются
`CAL_VERSION_UNSUPPORTED`. Функции по семействам (2026-10-08):

| Семейство | Лимиты + ПХХ | Зажигание Stage 1 (карта RON98) | Топливо | ВАНос | Диагностика/катализатор |
| --- | --- | --- | --- | --- | --- |
| `4560BO00S` (MS45.0, E83) | да | да (XDF) | `REQUIRES_DATA` | по логу | да |
| `4570LO00S` (MS45.1, E46 ZHP) | да | да (XDF) | по логу | по логу | да |
| `4560BN00S` (MS45.0, E60, есть реальное чтение) | да | `REQUIRES_DATA` | `REQUIRES_DATA` | `REQUIRES_DATA` | `REQUIRES_DATA` |
| `4560BL00S`, `4560BM00S`, `4570LL30S`, `4570LM00S`, `4570LN00S` | да | да (адреса доказаны отпечатками по всем стокам) | `REQUIRES_DATA` | `REQUIRES_DATA` | `REQUIRES_DATA` |
| `4560BL30S`, `4570LL00S` (по одному стоку) | да | `REQUIRES_DATA` — нужен второй образ | `REQUIRES_DATA` | `REQUIRES_DATA` | `REQUIRES_DATA` |

Зажигание везде по одним правилам: только карта RON98; без лога RON98 ≤
+3,0°, RON95 ≤ +1,5°, RON91 — запрет; с логом детонации ≤ +5,0°; карта RON91,
fallback-карта и защиты не пишутся. Педаль/момент на MS45 не выпускаются.
Подробности: `docs/VERSION_COVERAGE_RU.md` (§3а) и
`docs/FAMILY_CALIBRATION_EVIDENCE_RU.md` (§2–4).

Примеры: `examples/*.json` (заявки клиента, включая `stage1_request_bn00s.example.json`)
и `examples/stage1_naryad.example.md` (наряд, собранный генератором).

Быстрая проверка всего MS45:

```bash
python3 -m unittest discover -s MS45/tests -t .          # тесты
python3 -m MS45.tools.checksums MS45/firmware MS45/firmware_spdaten/cal MS45/firmware_dumps
python3 -m MS45.tools.policy --build --validate

# выпуск Stage 1 и проверка выпущенного файла по манифесту
python3 -m MS45.tools.stages --image MS45/firmware/.../сток.bin --stage stage1 \
    --request MS45/examples/stage1_request.example.json \
    --out stage1.bin --note-out naryad.md --manifest-out manifest.json
python3 -m MS45.tools.stages --verify-manifest manifest.json --note-out proverka.md
```

### Windows и Linux

* инструменты запускаются и модулем, и скриптом (см. ниже);
* пути в артефактах (политика, манифест, наряд, отчёты) — всегда с прямыми
  слэшами (`tools/pathfmt.py`), поэтому JSON политики совпадает побайтово на
  обеих ОС;
* артефакты пишутся с переводами строк LF (`newline="\n"`), а `.gitattributes`
  помечает образы прошивок двоичными — Windows с `core.autocrlf=true` их не
  испортит;
* после первого обновления дерева, где `.gitattributes` ещё не было, файлы могут
  остаться в старой CRLF-разметке — это видно как «изменено» в `git status` до
  первого коммита; лечится `git checkout -- .` (несохранённые правки при этом
  теряются, поэтому лучше сделать это сразу после `git pull`).

Все инструменты запускаются **двумя способами** — как модуль и как скрипт (как в
MS42/MS43); это проверяется тестами (`test_cli_console.py::ScriptRunTests`):

```bash
python3 -m MS45.tools.stages --help
python3 MS45/tools/stages.py --help
```
