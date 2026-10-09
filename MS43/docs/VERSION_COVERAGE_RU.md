# Покрытие версий калибровки MS43 (задание 2, блок A) и хлопки (блок B)

Документ отвечает на три вопроса: **почему** адреса одной версии нельзя переносить
на другую, **чем доказаны** адреса и границы каждой покрытой версии и **что**
именно ядро и генератор делают на версии. Всё, что не доказано, — явно помечено
`REQUIRES_DATA` или «не предлагаем».

Инструменты: `MS43/tools/policy.py` (реестр и сборка политик),
`MS43/tools/calibration.py` (выбор политики по версии), `MS43/tools/stages.py`
(генератор), `MS43/tools/fetch_definitions.py` (определения и их суммы).
Тесты: `MS43/tests/test_version_coverage.py`.

## 1. Веб-анализ: почему политика — на версию, а не «на MS43»

| источник | что из него следует | дата |
| --- | --- | --- |
| MS4X wiki, [First steps and requirements](https://www.ms4x.net/index.php?title=First_steps_and_requirements) | «each firmware version has minor or major improvements … every change in the code also means that the maps in the tune need a new layout or position»; «a definition file (XDF/Damos/A2L) is written for a very specific software version»; «Even loading a MS430055 tune in TunerPro with a definition file for MS430056 will result in a mess»; «you should not flash a calibration file from one firmware version onto an ECU that has another firmware version installed as it will result in a non starting engine»; раскладка flash: 0x00000–0x6FFFF — программа (там же фичи вроде LC), 0x70000–0x7FFFF — параметрия, «if you want to add new features such as launch control to the ECU then a full write is necessary» | 2026-10-06 |
| MS4X wiki, [Definition Files](https://www.ms4x.net/index.php?title=Definition_Files) | для MS43 официально публикуются **шесть** версий: 430037, 430055, 430056, 430064, 430066, 430069 — каждая в двух размерах (Partial 64 КиБ и Full 512 КиБ) | 2026-10-06 |
| upstream `github.com/ms4x-net/ms43`, каталог `definitions` | те же 12 определений лежат в git; файлы 430069 названы `Siemens_MS43_430069_…`, остальные — `MS4300xx_…` | 2026-10-06 |
| e46fanatics, [[MS4X] Siemens MS43 Community Patchlist](https://www.e46fanatics.com/threads/ms4x-siemens-ms43-community-patchlist-for-tuner-pro.1255981/) | патчлист сообщества собирается под **MS430069**; по 430056: «There will be no further developement for 430056»; «also, there are some chassis dependencies … they were present on 430056 as well, but only one binary was published» | 2026-10-06 |
| mhhauto, «Siemens MS43 TunerPro xdf file BMW M54B**» | XDF под 430066 опубликован отдельно с прямым предупреждением: «Do not try to work with it with different Softwareversions (430037, 430055, 430056 and 430069) as it will just be a big mess» | 2026-10-06 |
| MS4X wiki, [Siemens MS43](https://www.ms4x.net/index.php?title=Siemens_MS43) → «Exhaust Pop Modifications» | два подхода к хлопкам: **toggleable** (`ip_n_min_puc__tco`, `ip_n_min_accin_puc__tco`, `ip_iga_pu__n__tco`, `ip_iga_puc__n__tco`, `ip_iga_accin_puc__n__tco`) и **timered** — «delays the activation of the trailing throttle fuel cut by a set amount of time» (`c_t_puc_deacc_vs`, `ip_t_puc_deacc_1..5__n__maf_mmv`); обход для хлопков «на месте» — `id_n_hys_min_puc__vs__gear` / `_dri_` | 2026-10-06 |
| MS4X wiki, [TunerPro MS43 Community Patchlist](https://www.ms4x.net/index.php?title=TunerPro_MS43_Community_Patchlist) | страница патчлиста описывает фичи как патчи к прошивке («Implements code for the launch control…», «Ignition Cut Limiter» + предупреждение о катушках) — то есть это правки **программной** области, а не параметрии; список не помечен как применимый к другим версиям | 2026-10-06 |
| MS4X wiki, [MS43X Custom Firmware](https://www.ms4x.net/index.php?title=MS43X_Custom_Firmware) | «Added ignition cut option to the engine speed limiter» — отсечка зажиганием является доработкой прошивки, поэтому как продукт возможна только там, где доказана байтовая совместимость патча | 2026-10-06 |
| наши измерения (см. §2) | таблицы версий лежат по **разным** адресам: `ip_iga_puc__n__tco` — 0x7717D в 430069, 0x76FED в 430056, 0x76FDF в 430055, 0x77180 в 430037, 0x77179 в 430066; в 430056 на месте 430069-адреса лежат другие данные (`8E 8B 87…` вместо `2D 28 23…`) | 2026-10-06 |

Вывод: покрытие — **только отдельной политикой на версию**; перенос адресов 430069
(или любой другой версии) запрещён и проверяется тестами.

## 2. Чем доказаны адреса и границы каждой версии

Порядок один и тот же для каждой версии, все шаги воспроизводимы:

1. **Адрес — из официального определения этой версии.**
   `MS43/definitions/MS4300xx_Extended_Load_512K.xdf` (зеркало upstream, суммы —
   `checksums/DEFINITIONS_SHA256SUMS.txt`). В политике адрес — абсолютное смещение
   в образе 512 КиБ: `BASEOFFSET (0x70000) + адрес объекта в XDF`.
2. **Вторая, независимая проверка того же адреса** — определение 64 КиБ той же
   версии: для всех XDF-символов всех версий выполнено
   `адрес_512K − 0x70000 == адрес_64K` — **274 сверки, 0 расхождений**
   (`test_addresses_agree_with_the_64k_definition_of_the_same_version`).
3. **Проверка по реальным образам (не по определению):** «модельно-стабильные»
   таблицы (`id_pvs_fl__n`, `ip_cam_sp_tco_2_in_fl__n`, `id_n_max_mt__gear`,
   `ip_iga_puc__n__tco` — физические константы модели, при смене версии ПО их
   байты не меняются, меняется только место) обязаны лежать в стоке версии
   **ровно по адресу её политики**, побайтово совпадая с эталоном 430069
   (E46 330i M54B30 EU4). Совпадают: 430037 — 5 стоков из 11, 430055 — 2 из 3,
   430056 — 5 из 10, 430066 — 4 из 5, 430069 — 10 из 19. Не совпадают — только
   образы **других моторов/шасси** (тот же эталон внутри 430069 не совпадает с
   M54B25, M52TUB28, M54B22, E36 Z3 и E39 530i): то есть различие зависит от
   модели, а не от версии, и на чужих адресах этих байтов нет. Тест —
   `test_stable_tables_match_the_reference_by_content`.
   Отдельный случай — `E39_530__c6668528` (430066): это вариант калибровки
   E39 530i, у которого свои значения (отсечки по передачам 6496..6624 об/мин,
   длинный набор −15° в ПХХ против −6.75° у E46); его значения проходят и
   границы XDF, и монотонность ДМРВ, поэтому он используется как сток, но в
   доказательство «побайтового совпадения» не входит.
4. **Согласие чтений внутри версии:** полный образ `E53 c356750f` (512 КиБ) и
   64 КиБ чтение `E53 c356750c` версии 430056 — один датасет: значения
   **55 из 55** XDF-объектов совпали. Это прямая проверка, что относительный
   адрес в определении 512 КиБ и нулевая база 64 КиБ чтения — одно и то же место.
5. **Стоки в границах:** для каждой версии все XDF-символы читаются из всех её
   стоков (полных и 64 КиБ) и обязаны лежать в границах политики, а таблица ДМРВ
   — быть монотонной. Нарушений нет (`test_every_stock_of_every_covered_version_…`).
6. **Байтовая сверка с XDF:** каждый адрес политики читается из стока версии тем
   же определением, которым собран, — сырые байты совпадают
   (`test_policy_addresses_match_the_version_definition_bytes`).
7. **Перечисление стоков без повторов** (ревью 2): файлы собираются через
   `patches.bin_files()` (фильтр `suffix.lower()` + дедупликация нормализованного
   пути), потому что на Windows `glob("*.bin")` и `glob("*.Bin")` возвращают одни
   и те же файлы и счётчики удваивались (430056 — 20 образов вместо 10, ломался
   `test_each_non_default_policy_rebuilds_from_its_own_definition`). Тесты —
   `StockEnumerationTests`: число стоков равно числу уникальных файлов, старый
   вариант с двумя глобами даёт тот же результат, счётчики политик совпадают с
   числом образов (у 430069 — 18 полных 512 КиБ стоков из `MS43/firmware`).

Сколько образов на версию (стоки, по которым измерены значения):

| версия | полных 512 КиБ | чтений 64 КиБ | всего | примечание |
| --- | --- | --- | --- | --- |
| 430037 | 5 | 6 | 11 | один 512 КиБ образ — побайтный дубликат 2.5L (см. `CHECKSUM_RU.md` §5) |
| 430055 | 1 | 2 | 3 | стоков мало: границы — объявленные XDF, а не «подтянутые» по стокам |
| 430056 | 2 | 8 | 10 | один 64 КиБ/512 КиБ пара — один датасет (55/55) |
| 430066 | 1 | 4 | 5 | модельно-стабильные таблицы на 64 КиБ чтениях совпадают с 430069 (§2, п. 3) |
| 430069 | 18 | 1 | 19 | политика по умолчанию, не менялась: 18 полных стоков |

## 3. Таблица «версия → что пишется»

| версия | политика | что можно писать | что **не** пишется |
| --- | --- | --- | --- |
| 430069 | `policies/stage_policy.json` (97 символов: 63 калибровочных + 34 патчлиста) | калибровка по allowlist **и** фичи патчлиста (LC/RAL, Ignition Cut Limiter, MIL, M3-кластер, DS2, VSS) | всё, что не доказано: пакеты `full_load`/`vanos`/`ignition`/`fuel`/`maf`/`intake`/`injectors`/`exhaust`/`torque` ждут данных |
| 430056 | `policies/stage_policy_430056.json` (63 символа, все XDF) | ограничители (скорость, обороты по передачам), хлопки (включая A/C on и задержку), пороги/карты для Stage-пакетов по allowlist | фичи патчлиста: его basedata снята с 430069, байтовые маски на версии не доказаны |
| 430037 | `policies/stage_policy_430037.json` (62 символа: нет `ip_iga_tqr_amt__n__maf` — его нет в определении версии) | то же | то же |
| 430055 | `policies/stage_policy_430055.json` (63 символа) | то же (границы шире, чем «сток±», стоков всего 3) | то же |
| 430066 | `policies/stage_policy_430066.json` (63 символа) | то же | то же |
| 430064 | нет | ничего: полного образа этой версии нет, адреса и границы доказать не по чему (в архиве только 64 КиБ чтения) | всё; отказ `CAL_VERSION_UNSUPPORTED` |

Механика отказа: ядро читает код версии из строки идентификации
(`0x70008` в образе 512 КиБ, `0x8` в 64 КиБ чтении), берёт политику этой версии из
реестра `VERSION_POLICY_FILES` и отказывает, если версии в реестре нет
(`CAL_VERSION_UNSUPPORTED`). 64 КиБ чтение отклоняется всегда — `IMAGE_SIZE`
(в нём нет boot/prog и полного набора CRC, а запись калибровки в MS43 без них не
доказуема). Явно переданная политика (`--policy`) используется как есть, но
образ чужой версии она всё равно не примет.

## 4. Хлопки (блок B): что проверено и что добавлено

* **Полосы `light`/`medium`** работают на каждой покрытой версии: пакет пишет
  `c_n_hys_max_puc` (гистерезис ПХХ) и карты угла ПХХ, значения прижимаются к
  сетке объекта, ворота (`no_catalyst`, `thermal_watch`, `road_validation_plan`,
  запрет агрессивных полос) не менялись. Тесты —
  `test_popcorn_writes_on_every_covered_version`.
* **Задержка хлопка (timered pops)** — из MS4X wiki «Siemens_MS43» →
  «Exhaust Pop Modifications»: «This approach uses a built in timer function in
  the MS43 which delays the activation of the trailing throttle fuel cut by a set
  amount of time. With this approach it's possible to achieve a set amount of pops
  before the trailing throttle fuel cut is activated». Объекты:
  `c_t_puc_deacc_vs` — «Delay before activating trailing throttle fuel cut when
  car is stationary»; `ip_t_puc_deacc_1..5__n__maf_mmv` — «Delay before activating
  trailing throttle fuel cut. First…Fifth gear». Они есть в определениях всех
  пяти версий, стоки согласованы (0..0.325 с на месте и 0.13..2.45 с по
  передачам), поэтому добавлены в политику (группа `puc`) и в профиль хлопков
  (`delay_seconds`). Пределы полос: `light` — до 1.0 с, `medium` — до 2.5 с
  (верх привязан к измеренному стоку 2.45 с; выше не предлагаем). Грабля: в XDF
  у этих таблиц в поле units стоит `mg/stk` — это единицы **осей** `n`/`maf_mmv`,
  а значения дают секунды (шкала `0.00017066667*X`), что согласуется с описанием
  вики. Смысл правки: ПХХ активируется позже, смесь на сбросе газа остаётся —
  при задержке 0 (сток 430069) это «по стоку».
* **Семейство «A/C on»** (`ip_iga_accin_puc__n__tco`, `ip_n_min_accin_puc__tco`)
  пишется вместе с основным: иначе после включения кондиционера поведение
  хлопков менялось бы (вики делит карты именно по состоянию A/C).
* **Индикатор хлопка по педали** (`id_n_hys_min_puc__vs__gear`,
  `id_n_hys_min_puc_dri__vs__gear`) в вики упоминается как способ «хлопков на
  месте», но это уже вмешательство в момент и температуру; в политику не
  добавлен — `REQUIRES_DATA` (нужны логи EGT/лямбды и стенд).

## 5. Rev limit, launch control, RAL, vmax (блок B)

* **Обороты по передачам**: мягкий лимит `id_n_max_mt/at__gear`, жёсткий
  `id_n_max_max_mt/at__gear`, лимит при ограничении скорости
  `id_n_max_vs_max_mt/at__gear` — есть в политиках всех покрытых версий; ворота
  прежние: выше стока — `acknowledge_risk`, выше 6750 — `acknowledge_oil_pump`,
  жёсткий ниже мягкого — отказ (`HARD_BELOW_SOFT`).
* **Отсечка — fuel cut**: базовый ограничитель MS43 отключает подачу (это и
  используют хлопки через ПХХ — активация ПХХ отложена задержкой, см. §4), а
  обнуление зажигания — отдельный патч `[PATCH] Ignition Cut Limiter`, который
  предупреждает о ресурсе катушек (`PATCHLIST_RU.md`). Отсечка зажиганием — не
  сток, а изменение: в кастомной прошивке MS43X она тоже перечислена как
  доработка («Added ignition cut option to the engine speed limiter», MS4X wiki
  «MS43X Custom Firmware»). Поэтому патч остаётся **только для 430069**
  (basedata патчлиста), а на других версиях запрос `ignition_cut` отклоняется
  кодом `PATCHLIST_FEATURE_UNSUPPORTED`.
* **Launch control / RAL / MIL / кластер / DS2 / VSS**: на 430069 работают как
  раньше; на 430037/430055/430056/430066 — отказ с `PATCHLIST_FEATURE_UNSUPPORTED`
  (байтовые маски патчлиста на этих версиях не доказаны, переносить нельзя).
  Тесты: `test_patchlist_features_are_refused_on_other_versions`,
  `test_patchlist_module_refuses_other_versions`,
  `test_every_patchlist_intent_is_refused_on_other_versions`.
* **Полный список «фичевых» намерений** (`stages.PATCHLIST_INTENTS`) отклоняется
  на не-069 версиях — включая те, что генератор не собирает: `maf_hack`,
  `maf_full_load`, `map_reduction`, `alpha_n`, `fuel_cut_cruise`, `boost`,
  `immo_bypass`, `immo_virginize`. Раньше такие запросы давали лишь
  предупреждение «намерение неизвестно», то есть могли быть молча не выполнены.
  Теперь на 430069 они отклоняются кодом `PATCHLIST_FEATURE_NOT_IMPLEMENTED`
  (фича есть в патчлисте, но правка — только вручную патчем, см.
  `PATCHLIST_RU.md` §6), а на других версиях — `PATCHLIST_FEATURE_UNSUPPORTED`.
  Тест: `test_unimplemented_patchlist_intents_are_refused_on_430069`.
* **Патчи на уровне ядра**: `patches.py --check` на чужой версии больше не
  разбирает basedata (это давало мусорные «конфликты»), а честно отказывает и
  возвращает код 2; `calibration.plan_changes([...], patches=…)` на версии без
  патчлиста отклоняет план кодом `PATCHLIST_FEATURE_UNSUPPORTED` ещё до записи
  (раньше `--dry-run` показывал «PLANNED»). Тесты —
  `test_check_cli_refuses_a_foreign_version_with_code_2`,
  `test_calibration_plan_refuses_patches_on_other_versions`.
* **В политиках 430037/430055/430056/430066 нет ни одного символа патчлиста**
  (в каждой только `origin: xdf`, `requires_patch` не выставлен) — тест
  `test_non_default_policies_have_no_patchlist_symbols` сверяет множества имён с
  каталогом патчлиста.
* **`vmax`**: пишется калибровкой `c_vs_max_mt_1`/`c_vs_max_at_1` (+`c_vs_max_add`)
  на каждой покрытой версии, с прежними воротами `acknowledge_risk`.

## 6. `REQUIRES_DATA` (точный список замеров)

Ничего из этого не выдумывается и не «переносится между версиями».
Пакеты «зажигание / топливо / ВАНос» реализованы и выпускаются с воротами из
`docs/PACKS_LOG_REQUIREMENTS_RU.md` (машиночитаемый формат и пороги — в
`docs/pack_log_requirements.json`, проверяется кодом): зажигание — только
карта RON98, ≤ +1.5° без лога / ≤ +3.0° с логом детонации и не выше потолка
`ip_iga_optm_tco_2`; топливо — малая добавка `ip_tib` без лога, обогащение
полной нагрузки только с логом широкой лямбды (λ не ниже 0.82); ВАНос —
только тёплые карты частичной нагрузки и только по логу фаз. Карта RON91,
цепочка детонации, пороги полной нагрузки и модель момента не записываются
никогда. Без лога пакет отвечает `REQUIRES_DATA` со списком замеров:

Три варианта поставки собираются полем `variant` в заявке (`v1`/`v2`/`v3`,
уточнённые определения — `docs/PACKS_WEB_ANALYSIS_RU.md` §6):

* `v1` — базовый: зажигание ≤ +1.5° и малая добавка `ip_tib`; без ВАНоса,
  оборотов и обогащения;
* `v2` — расширенный: вариант 1 + ВАНос по логу фаз, обороты
  (`acknowledge_risk`/`acknowledge_oil_pump`), топливо и зажигание по логам;
* `v3` — машина без катализатора: вариант 2 + слой диагностики
  (`kind: "dtc"`, группы `cat`/`dmtl`/`lambda`/`sap` — приращения счётчиков
  `c_abc_inc_*` в 0; символы проверены по всем пяти версиям и внесены в
  политики) + хлопки по существующим воротам; обязательное `no_catalyst: true`,
  предупреждение о законности и план проверки (коды `V3_LEGALITY_WARNING`,
  `DTC_LEGALITY_WARNING` в наряде). Передний зонд и коды детонации не
  подавляются ни в одном варианте.

Намерение вне состава варианта — отказ `VARIANT_SCOPE_VIOLATION`; примеры —
`MS43/examples/stage1_variant_v1/v2/v3.example.json`.

Остальные пакеты, ждущие данные:

* **ДМРВ**: кривая (256 точек, кг/ч) из даташита или со стенда + логи расхода до/после.
* **Форсунки**: паспорт (производительность, dead time, давление) + лог лямбды и скважности.
* **Впуск/DISA**: описание железа в блоке `hardware` + лог DISA/VANOS и наполнения.
* **Выпуск**: состав (коллектор, катализатор, зонды), off-road декларация, решение по задним зондам.
* **Зажигание/топливо**: логи детонации по цилиндрам, RON топлива, широкая лямбда под нагрузкой.
* **Момент**: лог среза момента и данные коробки, стенд до/после.
* **Хлопки «на месте»** (`id_n_hys_min_puc_*`): EGT и лямбда-логи, стенд; в вики
  («Siemens_MS43», Exhaust Pop Modifications) описан обход — «In order to have
  pops when stopped, set the entire column in the first row to 7008 for
  `id_n_hys_min_puc__vs__gear` and `id_n_hys_min_puc_dri__vs__gear`», но это
  чужое значение без наших замеров, поэтому в политику не входит.
* **«+X л.с.»** не обещаем нигде: критерий приёмки — повторяемая разница момента на одном стенде.

## 7. Как расширять покрытие дальше

1. Положить определения версии в `MS43/definitions/` (или
   `python3 MS43/tools/fetch_definitions.py --version 4300XX`) и записать суммы.
2. Добавить версию в `VERSION_POLICY_FILES` и `VERSION_DEFINITIONS`
   (`MS43/tools/policy.py`) — для 430064 понадобится **полный** сток: без него
   сборка политики намеренно падает («есть только чтения 64 КиБ»).
3. `python3 MS43/tools/policy.py --build-all` — политика соберётся из определения
   версии, границы — из XDF, стоки — из образов версии; патчлист в неё не войдёт.
4. Прогнать `python3 -m unittest discover -s MS43/tests -t .`.
5. Для фич патчлиста на новой версии — отдельное доказательство байтовых масок
   (дизассемблер + сравнение с 430069), до него фичи не предлагаются.

## 8. Воспроизведение

```bash
python3 MS43/tools/fetch_definitions.py --check          # суммы определений
python3 MS43/tools/policy.py --build-all                 # все политики версий
python3 MS43/tools/policy.py --build-version 430056      # одна версия
python3 -m unittest MS43.tests.test_version_coverage -v  # доказательства блока A
python3 -m unittest discover -s MS43/tests -t .          # весь MS43
```

Сборка Stage 1 на стоке версии (пример — 430056):

```bash
python3 MS43/tools/stages.py --stage stage1 \
    --bin MS43/firmware_archive/E46_330i__c256954g_Ca430056_Stok.bin \
    --request MS43/examples/stage1_request.example.json \
    --out /tmp/stage1_430056.bin --report /tmp/stage1_430056.json --note /tmp/stage1_430056.md
```

Наряд печатает версию (`430056`) и полный список пакетов; LC/RAL из примера
отклоняются кодом `PATCHLIST_FEATURE_UNSUPPORTED` — это ожидаемое поведение на
версии, где патчлист не доказан (см. §5), при этом пакет `limits` пишется.
