# Наряд на калибровку MS41 — example-client

## Что сделано

Собран файл `/tmp/maf_req_patched.bin` из стокового `/tmp/stock.bin`.

- Выполнение: **WRITTEN** — файл записан на диск (в ЭБУ не записан).
- Изменено байтов значений: 308, контрольных слов: 4, всего: 312.
- Пересчитано блоков CRC: 15; цепочка после записи валидна: True. Контрольная цепочка покрывает блок до 0x2D4A.
- Байт вне плана изменено: 0.
- SHA-256 исходника: `31110cb55f0f55be8687ad15c00e3f6a606a1863b22658b9259c8e3e1f000acc`
- SHA-256 результата: `5edf6c7a1d72550b69a5389c0d448331148f0aad01056e9faff35a4e781ca6f4`

## Изменения

| Объект | Адрес | Было | Стало | Сырые значения | Δ от стока |
| --- | --- | --- | --- | --- | --- |
| id_maf_tab__v_maf_1__v_maf_2 (1x256) (сводно) | 0x2C16…0x2D48 | 67.69 … 725.1 kg/h | 71.75 … 768.6 kg/h | 154 столбцов | макс. +43.5 |
| c_vs_max | 0x0272 | 255 km/h | 250 km/h | 255 → 250 | -1.96 % |

_Для таблиц, изменённых целиком, приведена сводка; полный постолбцовый список — в JSON-отчётах._

### Почему именно эти значения

| Объект | Было | Стало | Источник значения | Нужна проверка | Обоснование |
| --- | --- | --- | --- | --- | --- |
| id_maf_tab__v_maf_1__v_maf_2 (1x256) (сводно) | 67.6875 | 768.5938 | documented_curve:derived | да | 154 столбцов: кривая датчика: пример (не даташит): стоковая кривая B28 с +6 % выше 2 В (derived, источник: MS41/examples/maf_curve_stock_b28.example.json, documented transformation) |
| c_vs_max | 255 | 250 | customer_intent | да | просьба клиента: ограничитель 250 км/ч |

_Для таблиц значения приведены диапазоном; обоснование одинаково для всех столбцов._

## Что не выполнено

_Все пункты запроса выполнены._

## Что нужно проверить после прошивки

- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- log MAF voltage, load and lambda at several points and compare with the supplier curve; a wrong curve shows up first in the trims
- road test: confirm the limiter releases at the requested speed and that the ECU does not re-limit during the pull

Статус проверки на момент выпуска наряда: **НЕ ВЫПОЛНЕНА** — наряд построен по расчёту, а не по результатам заездов.

## Границы

- Зажигание и топливные карты в наряд не входят: для них нужны логи, широкополосная лямбда и стенд (см. `docs/WORKFLOW_RU.md`).
- Кривая MAF переписывается только по документированной кривой датчика; источник `documented_curve:*` — это файл кривой, а не измерения на машине, поэтому проверка по логам обязательна.
- Файл подготовлен для записи внешним флешером; этот наряд прошивку не выполняет и не заменяет проверку на машине.
- Значения из пунктов с источником `customer_intent` — это пожелание клиента, а не измеренный оптимум.
