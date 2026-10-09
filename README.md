# TorqueX Generator

Открытый бесплатный проект: сайт https://gen.torquex.live и код генератора.
Лицензия кода — MIT (см. `LICENSE`).

> **Что в этом репозитории, а чего нет.** Здесь только код, тесты и документация.
> Прошивки, карты (XDF, A2L, DAMOS) и заводские калибровки (Siemens, BMW, ZF, Alpina)
> в репозиторий не входят: это чужие данные с собственными правами. Чтобы запускать
> сборку и все тесты, положите свои файлы в `MS41/firmware`, `MS42/firmware`,
> `MS43/firmware`, `MS45/firmware` (и описания в `MS4x/definitions`), а для коробок
> GS8 соберите базу командой `python tools/build_gs8_factory.py --spdaten <папка> --alpina <папка>`.
> Без данных `python tests/run_tests.py` пропускает тесты, которым они нужны.

Благодарность: сайту [MS4X](https://www.ms4x.net) и комьюнити BMW за открытые знания.

Генератор калибровок BMW: **Siemens MS41, MS42, MS43, MS45**.

## Уровень доверия (читать первым)

* **`bytes+crc verified`** — выпущенный файл сверен байт-в-байт: изменения только
  по разрешённым ячейкам политики, контрольные суммы (CRC/MO3) пересчитаны и
  проверены, вне калибровочного региона ничего не тронуто. Это проверяют тесты и
  команда `--verify-manifest`.
* **`field verified` — НЕТ.** Пока ни один файл не проверен на живой машине.
  Первый выезд делается по `docs/FIELD_TEST_PROTOCOL_RU.md` (самый осторожный
  вариант, без хлопков и без патчей программы). Мощность мы не обещаем.

Уровень доверия печатается в манифесте (`trust_level`) и в наряде каждого файла.

## Точка входа для новичка

```bash
python3 tools/torquex.py detect <сток.bin>          # что за ЭБУ и версия
python3 tools/torquex.py variants                   # что даёт вариант 1/2/3
python3 tools/torquex.py build --file <сток.bin> --variant v1 \
    --out <файл.bin> --note <наряд.md> --manifest <манифест.json>
python3 tools/torquex.py rollback --manifest <манифест.json> --stock <сток.bin> \
    --out <сток_для_записи.bin>
python3 tools/torquex.py explain <КОД>              # понятный текст отказа (RU/EN)
```

Гайд клиента: [`docs/CLIENT_GUIDE_RU.md`](docs/CLIENT_GUIDE_RU.md) ·
[`docs/CLIENT_GUIDE_EN.md`](docs/CLIENT_GUIDE_EN.md).

## С чего начать

| Хочу | Куда смотреть |
| --- | --- |
| Техническое задание генератора | [`MS41/docs/GENERATOR_REQUIREMENTS_RU.md`](MS41/docs/GENERATOR_REQUIREMENTS_RU.md) |
| Работа с инструментами MS41 | [`MS41/README.md`](MS41/README.md) |
| Логирование через Test0 | [`MS41/docs/TEST0_LOGGING_RU.md`](MS41/docs/TEST0_LOGGING_RU.md) |
| Инструкция клиенту: лог Test0 (RU/EN) | [`docs/TEST0_CLIENT_GUIDE_RU.md`](docs/TEST0_CLIENT_GUIDE_RU.md) · [`docs/TEST0_CLIENT_GUIDE_EN.md`](docs/TEST0_CLIENT_GUIDE_EN.md) |
| Формат логов Test0 (веб-анализ) | [`docs/TEST0_WEB_ANALYSIS_RU.md`](docs/TEST0_WEB_ANALYSIS_RU.md) |
| Матрица определений по всем ЭБУ | [`docs/DEFINITION_MATRIX.md`](docs/DEFINITION_MATRIX.md) |
| Прочитать/записать ЭБУ, снять лог, откатиться | [`docs/CLIENT_GUIDE_RU.md`](docs/CLIENT_GUIDE_RU.md) · [`docs/CLIENT_GUIDE_EN.md`](docs/CLIENT_GUIDE_EN.md) |
| Что нужно до публикации | [`docs/RELEASE_CHECKLIST_RU.md`](docs/RELEASE_CHECKLIST_RU.md) |
| Протокол первой живой проверки | [`docs/FIELD_TEST_PROTOCOL_RU.md`](docs/FIELD_TEST_PROTOCOL_RU.md) |
| Веб-API (код и развёртывание) | [`server/README_RU.md`](server/README_RU.md) · [`server/DEPLOY_RU.md`](server/DEPLOY_RU.md) |

## Проверка

```bash
python3 -m unittest discover -v
```

## Структура

- `MS41/`, `MS42/`, `MS43/`, `MS45/` — определения, прошивки, документация
  и инструменты по семействам ЭБУ.
- `docs/` — общий research, матрица определений, гайды и релизные ворота.
- `tools/` — единая точка входа (`torquex.py`), тексты отказов
  (`refusals.py`), анализатор логов Test0 и вспомогательные скрипты.
- `server/` — код и документы веб-обёртки (прод-сервер ведёт владелец).

Работа ведётся в ветке `arena/0a6c03f6-torquex-generator`.
