# Шаг 1: веб-анализ калибровки Stage 1 для BMW M54 / MS43

Дата: 2026-10-05. Целевая первая платформа генератора: **MS43, MS430069, штатный атмосферный M54, бензин RON 98**.

> Генератор не должен обещать «максимальную мощность» без логов и стенда. Его задача — сформировать проверяемую стартовую калибровку и не выйти за защитные границы.

## 1. Итог исследования

Для исправного атмосферного M54 хороший результат получается не от одной «богатой смеси» или глобального `+3°`, а от согласованной работы:

1. корректного определения full load;
2. смеси под полной нагрузкой;
3. угла зажигания с сохранением октановой адаптации и knock control;
4. VANOS под конкретный впуск/выпуск;
5. переключения DISA;
6. отсутствия ложного torque intervention;
7. профиля педали — только для отклика, не для измеряемой мощности.

На полностью штатном моторе топливная модель уже откалибрована BMW. **Базовые injection maps и MAF не следует автоматически масштабировать ради Stage 1.** Их изменение оправдано только логами, другими форсунками, корпусом MAF или изменившимся наполнением. Основной потенциальный прирост Stage 1 находится в VANOS, осторожной оптимизации RON98 ignition и корректной активации full-load/DISA.

Коммерческие страницы заявляют примерно 10–15 hp для программной настройки штатного M54, однако это маркетинговые данные без единого протокола стенда. Их нельзя использовать как acceptance criterion генератора. Критерий — повторяемая разница момента на одном стенде при одинаковых условиях и отсутствие ухудшения защит/детонации.

## 2. Надёжность источников

### Основные источники

- [MS4X Wiki — Siemens MS43](https://www.ms4x.net/index.php?title=Siemens_MS43): алгоритмы нагрузки, топлива, зажигания, VANOS, torque и full-load.
- [MS4X Wiki — First steps](https://www.ms4x.net/index.php?title=First_steps_and_requirements): совместимость версии ПО/XDF, размер flash и checksum.
- [MS4X GitHub — MS43](https://github.com/ms4x-net/ms43): актуальные XDF и эталонные MS430069 BIN.
- [MS4X Community Patchlist](https://www.ms4x.net/index.php?title=TunerPro_MS43_Community_Patchlist): расширенное логирование и MAF-based full-load.

### Вторичные источники

Форумы и страницы тюнеров использовались только для поиска направлений и оценки распространённых практик. Их численные прибавки и готовые дельты не считаются доказанными правилами. Пример форумного обсуждения показывает, что одна и та же VANOS-правка может не дать прироста и даже увеличить расход: [E46Fanatics MS43 tuning](https://www.e46fanatics.com/threads/ms43-tuning.1082287/page-171).

## 3. Обязательный Stage 0

До генерации силовых изменений должны быть подтверждены:

- отсутствуют active/pending DTC и misfire counters;
- обе банки имеют близкие STFT/LTFT, без явного vacuum leak;
- MAF, pre-cat lambda, coolant/IAT plausibility исправны;
- давление топлива соответствует штатному;
- DISA исправна механически и переключается;
- VANOS достигает target, нет медленной реакции и больших tracking errors;
- нет перегрева, расхода масла, проблем CCV и забитого выпуска;
- известны двигатель, коробка, регион прошивки и минимальный октан;
- сохранён оригинальный full read и его SHA-256.

При провале Stage 0 генератор выдаёт отчёт, но не performance BIN.

## 4. Приоритеты изменений

### P0 — full-load state и доступ к нужным картам

Стоковая логика использует разные пороги педали для injection, VANOS и DISA:

- `id_pvs_fl__n` — full load для injection;
- `id_pvs_fl_ivvt__n` — full load для VANOS;
- `id_pvs_fl_vim__n_vim` — full load для DISA;
- `id_vim_fl__n_vim` / `id_vim_pl__n_vim__maf` — состояние впускного коллектора.

Генератор должен не просто понизить пороги, а проверить в логе, что три подсистемы согласованно переходят в ожидаемое состояние. Слишком низкий порог ухудшит экономичность и создаст неожиданное обогащение при частичной педали.

Предпочтительный будущий вариант — MAF-based full-load patch, потому что нагрузка лучше отражает запрос двигателя, чем только положение педали. Но code patch не входит в первый 64K-only MVP.

### P1 — смесь под полной нагрузкой

Ключевая enrichment-карта:

- `ip_ti_fl__n` — full-load enrichment по оборотам.

MS4X отмечает: отображаемый target lambda достигается только тогда, когда базовые injection maps уже дают lambda 1.00 до enrichment. Поэтому алгоритм должен быть двухступенчатым:

1. проверить closed-loop baseline и fuel trims;
2. только затем корректировать full-load enrichment по wideband.

Рабочие и fallback maps:

- warm/cold part-load injection maps из XDF;
- `ip_tib__n__maf` — fallback при VANOS fault;
- injector/dead-time и MAF maps — только при замене железа.

Правила MVP:

- не записывать фиксированный AFR «для всех M54»;
- хранить цели в lambda, а не gasoline AFR — это исключает путаницу для другого топлива;
- не генерировать performance fuel changes без wideband log;
- контролировать bank split, trims, injector duty и fuel pressure;
- не лечить смесь правкой MAF, если причина — vacuum leak или давление топлива.

### P1 — зажигание

Основные карты:

- `ip_iga_ron_98_pl_ivvt__n__maf`;
- `ip_iga_ron_91_pl_ivvt__n__maf`;
- cold/warm карты `ip_iga_tco_*`;
- fallback `ip_igab__n__maf` и `ip_igab_is__n__maf`;
- reference/optimal maps `ip_iga_optm_tco_1__n__maf`, `ip_iga_optm_tco_2__n__maf` используются также моделью момента/защитами.

MS43 интерполирует RON91/RON98 по октановой адаптации. Следовательно:

- RON91 нельзя превращать в копию агрессивной RON98;
- нельзя глобально прибавлять одинаковый угол;
- Stage 1 изменяет только warm, high-load области RON98;
- переходы по load/rpm должны быть гладкими;
- RON91 сохраняет безопасный запас для плохого топлива;
- при повторяемом knock retard угол уменьшается, а не маскируется отключением knock;
- рост угла разрешён только до MBT или knock limit — что наступит раньше.

Без измерения момента генератор может выдать только консервативную стартовую дельту и обязательный статус `requires_validation`, но не финальную карту.

### P1 — VANOS

Основные full-load setpoints:

- `ip_cam_sp_tco_1_in_fl__n`, `ip_cam_sp_tco_1_ex_fl__n`;
- `ip_cam_sp_tco_2_in_fl__n`, `ip_cam_sp_tco_2_ex_fl__n`.

Part-load и переходы также надо сохранять согласованными:

- `ip_cam_sp_tco_[1|2]_[in|ex]_pl__n__maf_iv`;
- карты cold-to-warm blending;
- `ip_fac_maf_sub_ivvt__maf_sub_diag` и MAF substitute model при существенно изменившемся airflow.

Для штатной геометрии M54B30 MS4X приводит приблизительный физический диапазон intake `86–126° CRK`; exhaust в примере находится примерно в диапазоне `-105…-80°`. Это **границы механизма, не готовые оптимальные targets**.

Общее направление из MS4X:

- продвижение валов чаще усиливает низ/середину ценой верха;
- запаздывание чаще переносит наполнение вверх;
- overlap около средней зоны может помочь с более свободным выпуском;
- со штатным выпускным коллектором MS4X советует осторожнее менять exhaust side.

Правильная оптимизация — sweep отдельных зон на стенде: сначала intake, затем exhaust, затем взаимодействие. Сравниваются torque, airflow/load, lambda и knock при одинаковой температуре.

### P1 — DISA

DISA меняет эффективную длину впуска. Оптимальная точка — место пересечения измеренных кривых момента в двух положениях заслонки. Нельзя переносить одну точку между B22/B25/B30, M50 manifold и штатным M54 manifold.

Генератор Stage 1:

- сохраняет штатную точку по умолчанию;
- предлагает изменение только для известной конфигурации коллектора;
- требует dyno A/B для финального выбора;
- проверяет согласованность DISA full-load activation с injection/VANOS.

### P2 — torque model и ограничения

Карты/константы:

- `ip_tq_max__n__pvs_cor_rpl`;
- `c_conf_tq_lim_gear`, `c_tq_max_gear`;
- `c_tq_stnd`;
- torque model `ip_tqi_pvs__n__pvs`, `ip_tqi_maf__n__maf`, `ip_tqfr__n__maf`;
- optimal ignition maps, участвующие в оценке torque reduction.

MS4X допускает заполнение torque limit очень большим значением для отключения вмешательства, но для генератора это плохой default. Torque model используется другими блоками и коробкой. Стратегия:

1. обнаружить, действительно ли вмешательство ограничивает WOT;
2. поднять лимит только с запасом над ожидаемым моментом;
3. по возможности скорректировать torque model;
4. сохранять достоверный CAN torque для AT/SMG и свапов коробок;
5. first-gear limiter менять отдельной опцией.

### P3 — электронная педаль

Drivers Wish и throttle correction меняют скорость/форму запроса, но не максимальный airflow при уже полностью открытом throttle. Это отдельные UX-профили:

- `stock`;
- `linear`;
- `sport`.

Профиль должен оставаться монотонным, без скачка момента на малой педали и без маскировки реального прироста «острой» педалью.

### Не считать силовым тюнингом

- Vmax removal;
- повышенный rev limiter;
- hard cut / pops / burbles;
- повышенный idle;
- отключение DTC, catalyst heating или protections.

Они не создают мощность. Pops/burbles и отключение защит не должны входить в безопасный Stage 1.

## 5. Что генератор не должен менять автоматически

- MAF scaling и injector data на штатном железе;
- RON91 вслед за RON98;
- knock thresholds/sensitivity;
- misfire diagnostics;
- catalyst/EGT/temperature protections;
- rev limit без подтверждения механического состояния;
- checksum-disable patches;
- DTC masks для сокрытия неисправностей;
- все fuel/ignition/VANOS карты одним процентом;
- torque limit в `65535` без анализа трансмиссии.

## 6. Минимальный набор логов

До/после на одинаковой передаче и участке или стенде:

- RPM, pedal, throttle angle, gear, vehicle speed;
- `maf_mes`, `maf_ti`, `maf_iga`, `maf_ivvt`;
- wideband lambda (внешняя, синхронизированная);
- STFT/LTFT обеих банков;
- commanded/actual ignition по цилиндрам и knock retard;
- RON adaptation;
- VANOS target/actual intake/exhaust;
- DISA state;
- coolant, IAT, oil temperature;
- injection time/duty;
- calculated torque и torque intervention state.

Сравнение WOT без контроля IAT/coolant и без нескольких повторов недостоверно.

## 7. Предлагаемый алгоритм Stage 1 v0

1. Идентифицировать exact MS430069 и размер BIN.
2. Проверить SHA-256 исходника и совместимость XDF.
3. Определить M54B22/B25/B30 и stock hardware profile.
4. Stage 0 gate по диагностическому отчёту.
5. Создать baseline report всех P0/P1 symbols.
6. Full-load activation: только согласование состояний, без чрезмерно раннего WOT.
7. Fuel: оставить base maps; enrichment менять только при наличии wideband target/error.
8. Ignition: консервативная high-load RON98 surface delta, RON91 не агрессивнее stock.
9. VANOS: применять только engine-specific validated preset; иначе stock + план dyno sweep.
10. DISA: stock, если нет manifold-specific validated preset.
11. Torque: изменять только при доказанном intervention.
12. DBW: опциональный отдельный профиль.
13. Проверить ranges, smoothness, axes, fallback coherence и checksum.
14. Выдать BIN + machine-readable diff + предупреждения + список необходимых логов.

## 8. Как получить доказанный preset

Для каждого двигателя/железа нужен calibration experiment:

1. зафиксировать исправный stock baseline, минимум три повторяемых замера;
2. стабилизировать IAT/coolant;
3. откалибровать lambda;
4. выполнить VANOS sweep по зонам rpm, меняя один фактор;
5. оптимизировать DISA switch;
6. выполнить ignition sweep до MBT/knock limit;
7. повторно проверить lambda после VANOS/ignition;
8. проверить transient, hot restart, cold start, part-load и аварийные режимы;
9. сохранить preset только после повторного A/B/A подтверждения.

Preset должен иметь ключ:

```text
ECU + software + engine + intake + manifold + exhaust + cams + fuel + transmission
```

и метаданные: источник, стенд, дата, число повторов, диапазон температур, confidence и SHA-256 базового BIN.

## 9. Решение для разработки

Первый кодовый milestone следует ограничить **анализатором**, а не генератором агрессивных значений:

- распознать MS430069 64K/512K;
- извлечь перечисленные symbols из XDF;
- построить stock baseline report;
- валидировать диапазоны и гладкость;
- подготовить schema rules/presets;
- только после этого реализовать запись и checksum.

Так мы не зашьём в продукт форумные «магические числа» и получим основу для верифицированных Stage 1 presets.
