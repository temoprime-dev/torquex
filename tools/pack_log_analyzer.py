# -*- coding: utf-8 -*-
"""Анализатор клиентских логов для пакетов «зажигание / топливо / ВАНос».

Задание 1 ревью ``docs/REVIEW_PACKS_CLAUDE_RU.md``: ворота пакетов с
повышенными границами принимают доказательство заезда **только** если оно
выдано этим анализатором. Ручной ``pack_log``-JSON больше доказательством
не является (см. ``verify_evidence``).

Как это устроено
----------------
1. Анализатор читает лог в любом из представлений Test0/TestO — CSV-экспорт
   (разделитель ``;`` или ``,``, кодировка ``utf-8``/``cp1251``, десятичный
   разделитель — точка или запятая) или родную базу
   ``YYYY_MM_DD.db`` (SQLite; каналы разных job-ов склеиваются по времени) —
   распознаёт каналы по каноническим именам, алиасам из
   ``docs/pack_log_requirements.json`` и полям PRG (``STAT_*_WERT``) из
   ``docs/test0_channels.json``, после чего считает структурные метрики
   заездов по правилам ``docs/PACKS_LOG_REQUIREMENTS_RU.md``.
2. Доказательство — это сам результат анализа: внутри лежат
   ``analyzer_version``, ``source_sha256`` (sha256 исходного CSV) и
   посчитанные ``results``. Самоутверждение клиента («всё хорошо») без
   файла анализ не заменяет.
3. Ворота (``verify_evidence``) находят исходный CSV, сверяют его sha256 и
   **пересчитывают** метрики анализатором: любое расхождение — отказ.
   Ручной JSON (нет метки генератора) отклоняется отдельным кодом
   ``EVIDENCE_MANUAL_JSON``.

Определение «детонации нет» и происхождение порогов
---------------------------------------------------
* «коррекция ≤ 0.75°» — два кванта сетки зажигания 0.375° (см. веб-анализ,
  ``docs/PACKS_WEB_ANALYSIS_RU.md`` §1: управление детонацией у Siemens MS4x
  дискретно с шагом 0.375°); ниже двух квантов считается штатным шумом
  регулирования, а не реакцией на детонацию;
* «удержание ≥ 1.5° дольше 0.5 с» — признак устойчивой детонации, при
  которой ЭБУ начинает выцветание в карту RON91 (веб-анализ §1.1);
* «пик сырого сигнала выше 3× медианы фрагмента» — грубый воспроизводимый
  фильтр одиночных выбросов по напряжению датчиков детонации;
* «воздух на впуске ≤ 45 °C» — жаркий впуск сам провоцирует детонацию, и
  такой лог не является доказательством против карты (веб-анализ §1.2).
Все пороги продублированы машиночитаемой спецификацией
``docs/pack_log_requirements.json`` — тесты сверяют документ и JSON.

Запуск::

    python3 tools/pack_log_analyzer.py --pack ignition --csv заезд.csv \
        --ron 98 --out MS43/examples/logs/knock_free_wot.evidence.json

Логи клиентов не коммитятся (правило приватности, ``docs/PACKS_LOG_REQUIREMENTS_RU.md`` §6);
в репозитории лежат только синтетические фикстуры тестов и примеры.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from pathlib import Path

# Скрипт запускают и как ``python3 tools/pack_log_analyzer.py``: тогда корень
# репозитория не лежит в sys.path, а пакет ``tools`` (namespace package) нужен
# для чтения логов Test0.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import pack_recommendation  # noqa: E402
from tools import test0_log  # noqa: E402

ANALYZER_VERSION = "1.0"
GENERATOR_ID = "torquex_pack_log_analyzer"
SCHEMA_VERSION = 1

#: Версия нормализации для ``source_sha256`` (задание 1 ревью №2): клиентские
#: CSV из Windows-логгеров приходят с CRLF, а репозиторий на Windows клонируется
#: с ``core.autocrlf=true`` — sha256 считается по содержимому с заменой
#: ``\r\n`` → ``\n``, поэтому один и тот же лог даёт одно доказательство на
#: любой платформе. Старые доказательства без этого поля проверяются и по
#: «сырому» хэшу (обратная совместимость).
SHA256_NORMALIZATION = "crlf-to-lf/1"

# Коды отказов ворот (стабильные, на них ссылаются тесты и документация).
EVIDENCE_MANUAL_JSON = "EVIDENCE_MANUAL_JSON"
EVIDENCE_SOURCE_NOT_FOUND = "EVIDENCE_SOURCE_NOT_FOUND"
EVIDENCE_SHA256_MISMATCH = "EVIDENCE_SHA256_MISMATCH"
EVIDENCE_TAMPERED = "EVIDENCE_TAMPERED"
EVIDENCE_INVALID = "EVIDENCE_INVALID"


def load_requirements() -> dict:
    path = REPO_ROOT / "docs" / "pack_log_requirements.json"
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def normalize_log_bytes(raw: bytes) -> bytes:
    """Нормализованное содержимое лога для хэширования (``\\r\\n`` → ``\\n``)."""
    return raw.replace(b"\r\n", b"\n")


def log_sha256(path: str | Path) -> str:
    """sha256 нормализованного содержимого лога (см. ``SHA256_NORMALIZATION``)."""
    return hashlib.sha256(normalize_log_bytes(Path(path).read_bytes())).hexdigest()


# ---------------------------------------------------------------------------
# Чтение CSV
# ---------------------------------------------------------------------------

def _score_candidate(text: str, delimiter: str) -> tuple[int, int]:
    """Число строк с постоянным числом колонок для кандидата-разделителя."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return (0, 0)
    counts = [line.count(delimiter) for line in lines]
    try:
        common = statistics.mode(counts)
    except statistics.StatisticsError:
        common = counts[0]
    consistent = sum(1 for count in counts if count == common)
    return (common, consistent)


def _to_float(cell: str, decimal_separator: str) -> float | None:
    cell = cell.strip().replace("'", "").replace("\u00a0", " ")
    if not cell:
        return None
    if decimal_separator == ",":
        cell = cell.replace(" ", "").replace(",", ".")
    else:
        cell = cell.replace(" ", "")
    try:
        return float(cell)
    except ValueError:
        return None


_UNIT_BRACKETS = re.compile(r"\[([^\]]*)\]\s*$")
_CYL_PARENS = re.compile(
    r"^\s*(.*?)\s*\(\s*(?:cylinder|zylinder|цил(?:индр)?\.?)\s*(\d+)\s*\)\s*$", re.I)
_BANK_PARENS = re.compile(r"^\s*(.*?)\s*\(\s*(?:bank|банк)\s*(\d+)\s*\)\s*$", re.I)
_TRAIL_PARENS = re.compile(r"^\s*(.*?)\s*\([^)]*\)\s*$")
_TRAIL_DIGIT = re.compile(r"^(.*?)[\s_\-]+(\d)$")
_PRG_ECU = re.compile(r"^(MS\d\d)", re.I)


def detect_ecu(table: dict) -> str | None:
    """Семейство ЭБУ по метаданным Test0 (``prgName``) или по заголовкам.

    В родном ``.db`` Test0 хранится ``prgName`` (``MS430DS0`` и т.п.). В CSV
    этой информации нет — тогда возвращается ``None``, и подсказки каналов
    печатаются без привязки к семейству.
    """
    prg = ""
    for job in (table.get("test0_db") or {}).get("jobs", []):
        prg = job.get("prg") or ""
        if prg:
            break
    if not prg:
        for header in table.get("headers", []):
            match = _PRG_ECU.match(header.strip())
            if match:
                prg = match.group(1)
                break
    if not prg:
        return None
    family = prg[:4].upper() if len(prg) >= 4 else prg.upper()
    if family.startswith("MS4") and family[3].isdigit():
        return family
    return None


_TEST0_INDEX: dict[str, str] | None = None


def _test0_result_index() -> dict[str, str]:
    """Ленивая карта «поле PRG (STAT_*_WERT) → канал» из docs/test0_channels.json."""
    global _TEST0_INDEX
    if _TEST0_INDEX is None:
        by_name, _by_job, _ambiguous = test0_log.channel_map()
        _TEST0_INDEX = by_name
    return _TEST0_INDEX


def _match_channel(header: str, spec_channels: dict) -> tuple[str | None, int | None, str | None]:
    """Каноническое имя канала для заголовка записывающего инструмента.

    Заголовки реальных логгеров (задание 2 ревью №2) выглядят как
    ``Engine Speed [RPM]`` или ``Knock Voltage (Cylinder 3) [V]``: единицы в
    квадратных скобках, номера цилиндров и банков в круглых, произвольный
    регистр. Возвращает ``(имя канала, номер цилиндра, единица из скобок)``.
    """
    text = header.strip()
    unit = None
    m = _UNIT_BRACKETS.search(text)
    if m:
        unit = " ".join(m.group(1).strip().lower().split())
        text = text[: m.start()].strip()
    cylinder = None
    m = _CYL_PARENS.match(text)
    if m:
        cylinder = int(m.group(2))
        text = m.group(1)
    else:
        m = _BANK_PARENS.match(text)
        if m:
            bank = int(m.group(2))
            text = m.group(1)
            if bank != 1:
                # лямбда-регулирование побанковое, но доказательство строится
                # по банку 1; остальные банки не используются
                return None, None, unit
        else:
            m = _TRAIL_PARENS.match(text)
            if m:
                text = m.group(1)
    # Логи Test0/TestO: заголовок — поле PRG (``STAT_MOTORDREHZAHL_WERT``),
    # возможно с префиксом записи ``SET_1.``. Имена сверены с выгрузками PRG
    # (docs/test0_channels.json), угадывание по подстроке запрещено.
    prg = test0_log.strip_logger_prefix(text).upper()
    channel = _test0_result_index().get(prg)
    if channel is None and prg.endswith("_WERT"):
        # Некоторые выгрузки/пресеты пишут базовое имя без ``_WERT``.
        channel = _test0_result_index().get(prg[: -len("_WERT")])
    if channel is not None:
        return channel, cylinder, unit

    normalized = " ".join(text.lower().split())
    for name, channel in spec_channels.items():
        for alias in channel["aliases"]:
            if normalized == alias.lower():
                return name, cylinder, unit
    # Цифровой хвост у каналов детонации: «knock voltage cyl 3», «knks_3».
    m = _TRAIL_DIGIT.match(normalized)
    if m and 1 <= int(m.group(2)) <= 6:
        base = m.group(1).rstrip(" _-")
        for alias in spec_channels["knock_cylinders"]["aliases"]:
            if base == alias.lower():
                return "knock_cylinders", int(m.group(2)), unit
    return None, None, unit


def _unit_ok(channel: dict, unit: str | None) -> bool:
    """Согласованность единиц: единица из ``[...]`` обязана совпасть с
    ожидаемой у канала (список ``unit_aliases`` спецификации)."""
    if unit is None:
        return True
    aliases = channel.get("unit_aliases")
    if not aliases:
        return True
    return unit in {expected.lower() for expected in aliases}


#: Подсказки клиенту: как включить недостающий канал в логгере (задание 2
#: ревью №2 — сообщение анализатора обязано быть понятным).
CHANNEL_HINTS = {
    "time": "база времени записи (колонка времени, «TIME [s]»)",
    "rpm": "обороты двигателя («Engine Speed [RPM]»/«обороты»)",
    "load": "нагрузка («Engine Load [mg/stroke]»/«нагрузка»)",
    "maf_airflow": "расход воздуха («Mass Air Flow [kg/h]»/«дмрв_расход»)",
    "ignition": "фактический угол зажигания («Ignition Advance [deg]»/"
                "«угол_зажигания»)",
    "knock_retard": "коррекция по детонации («Knock Retard»/«коррекция "
                    "детонации») — вердикт ЭБУ; сырые напряжения датчиков "
                    "заменой не являются (фильтрация по окнам и порогам "
                    "выполняется внутри DME)",
    "injector_pulse_width": "время впрыска («Injection Time [ms]»/"
                            "«время_впрыска»)",
    "lambda_wideband": "широкополосная лямбда, банк 1 («Lambda Actual Value»/"
                       "«лямбда_широкополосная»)",
    "lambda_integrator": "интегратор лямбды («Lambda Integrator»/«лямбда_"
                         "интегратор»)",
    "coolant_temp": "температура ОЖ («Coolant»/«охлаждающая_жидкость») — "
                    "обязательна: без неё нельзя проверить прогретость мотора",
    "intake_temp": "температура воздуха на впуске («Intake Air Temp»/"
                   "«воздух_на_впуске»)",
    "pedal": "педаль/дроссель («Throttle»/«педаль»); если канала нет, «газ в "
             "пол» определяется по нагрузке — включите хотя бы нагрузку",
    "vanos_intake_actual": "фактические фазы впуска («Intake VANOS: Actual "
                           "Value»)",
    "vanos_exhaust_actual": "фактические фазы выпуска («Exhaust VANOS: Actual "
                            "Value»)",
    "vanos_intake_target": "целевые фазы впуска («Intake VANOS: Target "
                           "Value»)",
    "vanos_exhaust_target": "целевые фазы выпуска («Exhaust VANOS: Target "
                            "Value»)",
}


def _required_channels(pack: str, spec: dict) -> list[str]:
    """Обязательные каналы пакета по спецификации (``hard_required``).

    Педаль не входит: без неё «газ в пол» определяется по нагрузке
    (веб-анализ §9). Напряжения детонации по цилиндрам — вспомогательный
    фильтр, тоже не обязателен.
    """
    channels = spec["channels"]
    return sorted(
        name
        for name, channel in channels.items()
        if pack in channel.get("required_by", [])
        and channel.get("hard_required", True)
        and name != "knock_cylinders"
    )


def _missing_report(table: dict, pack: str, spec: dict) -> list[str]:
    """Точный список недостающих обязательных каналов с подсказками."""
    missing = [
        name
        for name in _required_channels(pack, spec)
        if not _compact(_series(table, name))
    ]
    if not missing:
        return []
    if str(table.get("format", "")).startswith("test0"):
        # Log Test0: подсказываем конкретные job-ы PRG и готовый пресет.
        hints = test0_log.annotate_presets_for_channels(missing, detect_ecu(table))
        return [
            "нет обязательных каналов (Test0): " + hints
            + "; готовые наборы job-ов — docs/test0_presets/ "
            "(соберите пресет в Test0 и запишите лог заново)"
        ]
    details = "; ".join(
        f"{name} — включите в логгере канал: {CHANNEL_HINTS.get(name, name)}"
        for name in missing
    )
    return ["нет обязательных каналов: " + details]


def read_pack_csv(path: str | Path) -> dict:
    """Читает CSV и раскладывает колонки по каноническим каналам.

    Возвращает словарь: ``channels`` (канал → список значений),
    ``headers``, ``encoding``, ``delimiter``, ``decimal_separator``,
    ``rows``; проблемы формата — в ``issues``.
    """
    raw = Path(path).read_bytes()
    text = None
    for encoding in ("utf-8", "cp1251"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError(f"не удалось декодировать {path} ни в utf-8, ни в cp1251")

    spec = load_requirements()["channels"]
    best = None
    for delimiter in (";", ","):
        columns, consistent = _score_candidate(text, delimiter)
        score = (columns, consistent)
        if best is None or score > best[0]:
            best = (score, delimiter)
    delimiter = best[1]

    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError(f"{path}: в логе меньше двух строк")
    headers = [h.strip() for h in lines[0].split(delimiter)]

    # Десятичный разделитель выбираем по доле парсимых ячеек (кроме времени:
    # время может быть целым секундным счётом).
    body = [line.split(delimiter) for line in lines[1:]]
    scores = {}
    for separator in (".", ","):
        parsed = 0
        total = 0
        for row in body[:50]:
            for index, cell in enumerate(row):
                if index == 0:
                    continue
                total += 1
                if _to_float(cell, separator) is not None:
                    parsed += 1
        scores[separator] = parsed / total if total else 0.0
    decimal_separator = max(scores, key=scores.get) if max(scores.values()) > 0 else "."

    # Каналы требований + сервисные каналы Test0 (docs/test0_channels.json),
    # которых в требованиях пакетов ещё нет: их тоже нужно читать и проверять.
    channel_names = list(spec) + [name for name in test0_log.channel_names(only_extra=True)]
    mapping: list[tuple[int, str, int | None]] = []
    issues: list[str] = []
    conversions: list[str] = []
    test0_columns = 0
    for index, header in enumerate(headers):
        name, cylinder, unit = _match_channel(header, spec)
        if name is None:
            continue
        if not _unit_ok(spec[name], unit):
            issues.append(
                f"канал {name!r} (заголовок {header!r}) пропущен: единица "
                f"[{unit}] не совпадает с ожидаемой для этого канала "
                "(сверьте настройки инструмента)"
            )
            continue
        prg = test0_log.strip_logger_prefix(header).upper()
        if (_test0_result_index().get(prg) is not None
                or test0_log.has_logger_prefix(header)):
            test0_columns += 1
        if name == "knock_cylinders":
            if cylinder is None:
                continue
            mapping.append((index, f"knock_cyl_{cylinder}", cylinder))
        else:
            mapping.append((index, name, None))

    # Все каналы спецификации присутствуют (при отсутствии — список None той же
    # длины), чтобы анализы не падали на индексах при неполных логах.
    channel_values: dict[str, list] = {
        name: [None] * len(body) for name in channel_names
    }
    for _, key, _ in mapping:
        channel_values[key] = []
    for row in body:
        for index, key, _ in mapping:
            cell = row[index] if index < len(row) else ""
            channel_values[key].append(_to_float(cell, decimal_separator))

    # Внешний широкополосный зонд может писать AFR: переводим в λ по
    # стехиометрии бензина (14Point7 ``lambda2afr``). Пересчёт фиксируется.
    for index, key, _ in mapping:
        if key != "lambda_wideband":
            continue
        unit = _UNIT_BRACKETS.search(headers[index].strip())
        if unit and "afr" in unit.group(1).strip().lower():
            channel_values[key] = [
                None if value is None else value / test0_log.AFR_STOICH
                for value in channel_values[key]
            ]
            conversions.append(
                f"lambda_wideband <{headers[index]}>: AFR→λ (/{test0_log.AFR_STOICH})")

    format_name = "test0_csv" if test0_columns else "csv"
    return {
        "channels": channel_values,
        "headers": headers,
        "raw_rows": [delimiter.join(row) for row in body],
        "encoding": encoding,
        "delimiter": delimiter,
        "decimal_separator": decimal_separator,
        "rows": len(body),
        "issues": issues,
        "conversions": conversions,
        "format": format_name,
    }


def read_log_table(path: str | Path) -> dict:
    """Читает лог в любом поддерживаемом представлении Test0.

    Родная база ``YYYY_MM_DD.db`` читается напрямую из SQLite, CSV-экспорт
    Test0/TestO и CSV сторонних логгеров — разбором колонок.
    """
    path = Path(path)
    if test0_log.is_test0_db(path):
        table = test0_log.read_test0_db(path)
        spec = load_requirements()["channels"]
        # Единицы из *_EINH проверяются так же строго, как в CSV: канал с
        # чужой единицей пропускается с понятным сообщением (док. §1.1).
        units = (table.get("test0_db") or {}).get("units", {})
        for name, unit in units.items():
            channel = spec.get(name)
            if channel is None or not unit:
                continue
            if not _unit_ok(channel, unit.strip().lower()):
                table["issues"].append(
                    f"канал {name!r} пропущен: единица [{unit}] не совпадает с "
                    "ожидаемой для этого канала (сверьте настройки Test0)"
                )
                table["channels"][name] = [None] * table["rows"]
        for name in list(spec) + list(test0_log.channel_names(only_extra=True)):
            table["channels"].setdefault(name, [None] * table["rows"])
        return table
    return read_pack_csv(path)


# ---------------------------------------------------------------------------
# Метрики заездов
# ---------------------------------------------------------------------------

def _series(table: dict, name: str) -> list[float | None]:
    return table["channels"].get(name, [])


def _compact(series: list[float | None]) -> list[float]:
    return [value for value in series if value is not None]


def _stdev(values: list[float]) -> float:
    return statistics.pstdev(values) if len(values) > 1 else 0.0


def _variance(values: list[float]) -> float:
    return statistics.pvariance(values) if len(values) > 1 else 0.0


def _physical_impossible(table: dict) -> list[str]:
    """Признаки подделки/ошибки снятия (документ §6): лог отклоняется целиком."""
    issues = []
    rpm = _compact(_series(table, "rpm"))
    if any(value > 8160 for value in rpm):
        issues.append("обороты выше 8160 — физически невозможно для этих моторов")
    lamb = _compact(_series(table, "lambda_wideband"))
    if any(value > 4 for value in lamb):
        issues.append("лямбда выше 4 — физически невозможно")
    time = _compact(_series(table, "time"))
    if any(b <= a for a, b in zip(time, time[1:])):
        issues.append("время не монотонно — лог отклоняется целиком")
    # «дубликаты строк» — подряд идущие полностью одинаковые строки: живые
    # данные всегда шумят, копипаста фрагмента — нет.
    raw_rows = table.get("raw_rows", [])
    if raw_rows:
        duplicates = sum(1 for a, b in zip(raw_rows, raw_rows[1:]) if a == b)
        if duplicates / len(raw_rows) > 0.01:
            issues.append("больше 1% подряд идущих одинаковых строк — признак подделки")
    return issues


def _sample_rate_hz(table: dict) -> float:
    time = _compact(_series(table, "time"))
    if len(time) < 3:
        return 0.0
    diffs = [b - a for a, b in zip(time, time[1:]) if b > a]
    if not diffs:
        return 0.0
    median = statistics.median(diffs)
    return 1.0 / median if median > 0 else 0.0


def _time_step_uniform(table: dict, tolerance_percent: float) -> bool:
    time = _compact(_series(table, "time"))
    diffs = [b - a for a, b in zip(time, time[1:])]
    if len(diffs) < 2:
        return True
    median = statistics.median(diffs)
    if median <= 0:
        return False
    limit = median * tolerance_percent / 100.0
    return all(abs(d - median) <= limit for d in diffs)


def _pedal_percent(series: list[float | None]) -> list[float | None]:
    """Педаль в процентах; если канал снят в долях (макс ≤ 1.5) — масштабируем."""
    values = _compact(series)
    if values and max(values) <= 1.5:
        return [None if v is None else v * 100.0 for v in series]
    return series


def _wot_runs(table: dict, spec: dict, pack: str) -> list[tuple[int, int]]:
    """Индексы фрагментов «газ в пол».

    Если канал педали есть — полный ход определяется по педали (≥ 90 %).
    Если педали в логе нет (частый случай реальных логгеров, задание 2
    ревью №2) — по нагрузке: окно «газ в пол» спецификации начинается с
    400 мг/такт, и на полном ходу нагрузка выше него на всех передачах
    (веб-анализ §9).
    """
    pedal = _pedal_percent(_series(table, "pedal"))
    has_pedal = bool(_compact(pedal))
    load = _series(table, "load")
    load_wot = float(
        spec["packages"]["ignition"]["wot_run"]["load_min_mg_stroke"])
    rpm = _series(table, "rpm")
    tco = _series(table, "coolant_temp")
    conditions = spec["conditions"]
    wot_cfg = spec["packages"][pack].get("wot_run", {"rpm_start": 2000})
    rpm_start = float(wot_cfg["rpm_start"])
    runs: list[tuple[int, int]] = []
    start = None
    for index in range(len(rpm)):
        if has_pedal:
            demanded = pedal[index] is not None and pedal[index] >= 90.0
        else:
            demanded = load[index] is not None and load[index] >= load_wot
        ok = (
            demanded
            and rpm[index] is not None and rpm[index] >= rpm_start
            and tco[index] is not None
            and conditions["coolant_min_c"] <= tco[index] <= conditions["coolant_max_c"]
        )
        if ok and start is None:
            start = index
        if (not ok or index == len(rpm) - 1) and start is not None:
            end = index if not ok else index + 1
            rpm_lo = min(v for v in rpm[start:end] if v is not None)
            rpm_hi = max(v for v in rpm[start:end] if v is not None)
            if rpm_hi - rpm_lo >= 1500 and end - start >= 5:
                runs.append((start, end))
            start = None
    return runs


def _analyze_ignition(table: dict, spec: dict, fuel_ron: float | None) -> dict:
    pack_spec = spec["packages"]["ignition"]
    no_knock = pack_spec["no_knock"]
    issues: list[str] = []
    results: dict = {}

    notes: list[str] = []
    issues.extend(_missing_report(table, "ignition", spec))
    cylinders_present = any(
        _compact(_series(table, f"knock_cyl_{index}")) for index in range(1, 7)
    )
    if not cylinders_present:
        notes.append(
            "нет напряжений датчиков детонации по цилиндрам — канал "
            "вспомогательный: фильтр одиночных пиков не применяется, основное "
            "доказательство — коррекция по детонации"
        )

    runs = _wot_runs(table, spec, "ignition")
    results["wot_runs"] = len(runs)
    if len(runs) < pack_spec["min_wot_runs"]:
        issues.append(
            f"полных разгонов найдено {len(runs)}, нужно не меньше {pack_spec['min_wot_runs']}"
        )

    load_min = float(pack_spec["wot_run"]["load_min_mg_stroke"])
    fractions = []
    sustained_worst = 0.0
    spike_events = 0
    iat_max = None
    time = _series(table, "time")

    medians = []
    for cylinder in range(1, 7):
        values = _compact(_series(table, f"knock_cyl_{cylinder}"))
        if values:
            medians.append(statistics.median(values))
    knock_median = max(medians) if medians else 0.0
    spike_threshold = max(
        knock_median * float(no_knock["knock_spike_ratio_vs_median"]), 0.05
    )
    if not cylinders_present:
        spike_threshold = None  # фильтр пиков не применяется без канала

    for start, end in runs:
        total = 0
        calm = 0
        for index in range(start, end):
            load = table["channels"]["load"][index]
            retard = table["channels"]["knock_retard"][index]
            if load is None or retard is None or load < load_min:
                continue
            total += 1
            if retard <= float(no_knock["max_retard_deg"]):
                calm += 1
        fractions.append(calm / total if total else 0.0)

        held_seconds = 0.0
        for index in range(start + 1, end):
            retard = table["channels"]["knock_retard"][index]
            if retard is not None and retard >= float(no_knock["max_sustained_retard_deg"]):
                dt = (time[index] or 0.0) - (time[index - 1] or 0.0)
                held_seconds += max(dt, 0.0)
            else:
                held_seconds = 0.0
            sustained_worst = max(sustained_worst, held_seconds)

        for cylinder in range(1, 7):
            series = _series(table, f"knock_cyl_{cylinder}")
            if not _compact(series):
                continue
            for index in range(start, end):
                value = series[index]
                if value is not None and value > spike_threshold:
                    spike_events += 1

        iat_values = [v for v in table["channels"]["intake_temp"][start:end]
                      if v is not None]
        if iat_values:
            run_iat = max(iat_values)
            iat_max = run_iat if iat_max is None else max(iat_max, run_iat)

    results["no_knock_fraction"] = round(min(fractions), 4) if fractions else 0.0
    results["max_sustained_retard_seconds"] = round(sustained_worst, 3)
    results["knock_spike_events"] = spike_events
    results["max_iat_c"] = round(iat_max, 2) if iat_max is not None else None

    if results["no_knock_fraction"] < float(no_knock["min_fraction_above_load"]):
        issues.append(
            f"доля выборок без коррекции детонации {results['no_knock_fraction']} "
            f"ниже порога {no_knock['min_fraction_above_load']}"
        )
    if sustained_worst > float(no_knock["max_sustained_seconds"]):
        issues.append(
            f"коррекция от {no_knock['max_sustained_retard_deg']}° держалась "
            f"{round(sustained_worst, 2)} с (порог {no_knock['max_sustained_seconds']} с)"
        )
    if spike_events:
        issues.append(f"одиночные пики датчиков детонации: {spike_events}")
    if iat_max is not None and iat_max > float(spec["conditions"]["intake_temp_max_c"]):
        issues.append(
            f"воздух на впуске до {round(iat_max, 1)}°C — выше порога "
            f"{spec['conditions']['intake_temp_max_c']}°C, лог не доказывает стойкость карты"
        )
    min_ron = spec["conditions"]["min_ron_for_ignition_evidence"]
    if fuel_ron is None or float(fuel_ron) < float(min_ron):
        issues.append(f"для доказательства по зажиганию нужно топливо не ниже RON {min_ron}")

    return {"results": results, "issues": issues, "notes": notes}


def _analyze_fuel(table: dict, spec: dict) -> dict:
    pack_spec = spec["packages"]["fuel"]
    criteria = pack_spec["criteria"]
    issues: list[str] = []
    results: dict = {}

    issues.extend(_missing_report(table, "fuel", spec))

    runs = _wot_runs(table, spec, "fuel")
    results["wot_runs"] = len(runs)
    if len(runs) < pack_spec["min_wot_runs"]:
        issues.append(
            f"полных разгонов найдено {len(runs)}, нужно не меньше {pack_spec['min_wot_runs']}"
        )

    run_lambdas = []
    stdevs = []
    lambda_floor = None
    for start, end in runs:
        window = []
        rpm_values = [v for v in table["channels"]["rpm"][start:end] if v is not None]
        rpm_top = max(rpm_values) if rpm_values else 0.0
        for index in range(start, end):
            rpm = table["channels"]["rpm"][index]
            lamb = table["channels"]["lambda_wideband"][index]
            if rpm is None or lamb is None or rpm < 0.7 * rpm_top:
                continue
            window.append(lamb)
            run_lambdas.append(lamb)
            lambda_floor = lamb if lambda_floor is None else min(lambda_floor, lamb)
        if window:
            stdevs.append(_stdev(window))

    variance = _variance(run_lambdas)
    results["lambda_variance"] = round(variance, 6)
    results["lambda_stdev_wot"] = round(max(stdevs), 4) if stdevs else None
    results["lambda_floor"] = round(lambda_floor, 4) if lambda_floor is not None else None

    if variance < float(criteria["wideband_variance_min"]):
        issues.append(
            "лямбда-канал «залипает» (дисперсия ≈ 0) — похоже на узкополосный зонд"
        )
    if stdevs and max(stdevs) > float(criteria["lambda_stdev_max_wot"]):
        issues.append(
            f"разброс лямбды на полном ходу {round(max(stdevs), 3)} больше порога "
            f"{criteria['lambda_stdev_max_wot']}"
        )
    if lambda_floor is not None and lambda_floor < float(criteria["lambda_target_min"]):
        issues.append(
            f"фактическая лямбда уходит ниже {criteria['lambda_target_min']} — "
            "запас по обогащению исчерпан"
        )

    # Установившийся фрагмент частичной нагрузки: обороты 1500..3500, педаль
    # ≤ 40 % (без педали — нагрузка ≤ 300 мг/такт, см. веб-анализ §9),
    # разброс оборотов в окне ≤ 100, длительность ≥ 10 с.
    rpm = _series(table, "rpm")
    pedal = _pedal_percent(_series(table, "pedal"))
    has_pedal = bool(_compact(pedal))
    load_series = _series(table, "load")
    integrator = _series(table, "lambda_integrator")
    hold_seconds = float(pack_spec["partial_load_hold_seconds"])
    trims: list[float] = []
    longest = 0.0
    start = None
    for index in range(len(rpm)):
        if has_pedal:
            part_load = pedal[index] is not None and pedal[index] <= 40.0
        else:
            part_load = (load_series[index] is not None
                         and load_series[index] <= 300.0)
        ok = (
            rpm[index] is not None and 1500 <= rpm[index] <= 3500
            and part_load
        )
        if ok and start is None:
            start = index
        if not ok or index == len(rpm) - 1:
            if start is not None:
                end = index if not ok else index + 1
                segment_rpm = [v for v in rpm[start:end] if v is not None]
                duration = len(segment_rpm) / max(_sample_rate_hz(table), 1.0)
                if segment_rpm and max(segment_rpm) - min(segment_rpm) <= 100 \
                        and duration >= hold_seconds:
                    longest = max(longest, duration)
                    trims.extend(v for v in integrator[start:end] if v is not None)
                start = None
    results["partial_load_hold_seconds"] = round(longest, 2)
    trim_max = max(abs(v) for v in trims) if trims else None
    results["partial_load_trim_max_percent"] = round(trim_max, 3) if trim_max is not None else None
    if trim_max is None:
        issues.append(
            f"не найден установившийся фрагмент частичной нагрузки длительностью {hold_seconds} с"
        )
    elif trim_max > float(criteria["partial_load_trim_max_percent"]):
        issues.append(
            f"тримы частичной нагрузки до {round(trim_max, 2)}% при пороге "
            f"{criteria['partial_load_trim_max_percent']}%"
        )

    return {"results": results, "issues": issues}


def _analyze_vanos(table: dict, spec: dict) -> dict:
    pack_spec = spec["packages"]["vanos"]
    criteria = pack_spec["criteria"]
    issues: list[str] = []
    results: dict = {}

    issues.extend(_missing_report(table, "vanos", spec))

    rpm_grid = [float(v) for v in pack_spec["hold_points"]["rpm"]]
    load_grid = [float(v) for v in pack_spec["hold_points"]["load_mg_stroke"]]
    min_hold = float(pack_spec["hold_points"]["min_hold_seconds"])
    settle = float(criteria["settle_seconds"])

    rpm = _series(table, "rpm")
    load = _series(table, "load")
    rate = max(_sample_rate_hz(table), 1.0)
    covered: set[tuple[float, float]] = set()
    worst_error = 0.0
    shortest_hold = None

    def nearest_cell(index: int) -> tuple[float, float] | None:
        near_rpm = next((g for g in rpm_grid
                         if rpm[index] is not None and abs(rpm[index] - g) <= 150), None)
        near_load = next((g for g in load_grid
                          if load[index] is not None and abs(load[index] - g) <= 75), None)
        if near_rpm is None or near_load is None:
            return None
        return (near_rpm, near_load)

    # Сегмент — непрерывный кусок лога, в котором ближайшая точка сетки
    # постоянна; при смене точки сегмент закрывается и открывается новый.
    def flush(start: int, end: int, cell: tuple[float, float]) -> None:
        nonlocal worst_error, shortest_hold
        duration = (end - start) / rate
        tco_ok = all(
            v is not None and spec["conditions"]["coolant_min_c"] <= v
            for v in table["channels"]["coolant_temp"][start:end]
        )
        if duration < min_hold or not tco_ok:
            return
        covered.add(cell)
        shortest_hold = duration if shortest_hold is None \
            else min(shortest_hold, duration)
        offset = max(int(settle * rate), 1)
        for index2 in range(start + offset, end):
            for actual, target in (
                ("vanos_intake_actual", "vanos_intake_target"),
                ("vanos_exhaust_actual", "vanos_exhaust_target"),
            ):
                a = table["channels"][actual][index2]
                t = table["channels"][target][index2]
                if a is not None and t is not None:
                    worst_error = max(worst_error, abs(a - t))

    current_cell = None
    segment_start = 0
    for index in range(len(rpm)):
        cell = nearest_cell(index)
        if cell != current_cell:
            if current_cell is not None:
                flush(segment_start, index, current_cell)
            current_cell = cell
            segment_start = index
    if current_cell is not None:
        flush(segment_start, len(rpm), current_cell)

    results["hold_points"] = len(covered)
    results["hold_points_required"] = len(rpm_grid) * len(load_grid)
    results["max_track_error_deg"] = round(worst_error, 3)
    results["min_hold_seconds"] = round(shortest_hold, 2) if shortest_hold else None

    if len(covered) < len(rpm_grid) * len(load_grid):
        issues.append(
            f"удержанных точек сетки {len(covered)} из "
            f"{len(rpm_grid) * len(load_grid)} — серии недостаточно"
        )
    if worst_error > float(criteria["max_track_error_deg"]):
        issues.append(
            f"рассогласование факт/цель до {round(worst_error, 2)}° при пороге "
            f"{criteria['max_track_error_deg']}° — механика не держит цель"
        )
    runs = _wot_runs(table, spec, "vanos")
    results["wot_runs"] = len(runs)
    if not runs:
        issues.append("нет контрольного полного разгона для переходных целей")

    return {"results": results, "issues": issues}


# ---------------------------------------------------------------------------
# Полный анализ и доказательство
# ---------------------------------------------------------------------------

def analyze_pack_log(path: str | Path, pack: str,
                     fuel_ron: float | None = None) -> dict:
    """Считает метрики пакета из лога Test0 (CSV/``.db``) или CSV логгера."""
    spec = load_requirements()
    if pack not in spec["packages"]:
        raise ValueError(f"неизвестный пакет: {pack!r}")
    table = read_log_table(path)
    issues = list(table["issues"])
    issues.extend(_physical_impossible(table))

    rate = _sample_rate_hz(table)
    min_rate = float(spec["packages"][pack]["min_rate_hz"])
    if rate + 1e-6 < min_rate:
        issues.append(f"частота записи {round(rate, 1)} Гц ниже требуемых {min_rate} Гц")
    if not _time_step_uniform(table,
                              spec["file_format"]["time_step_tolerance_percent"]):
        issues.append("шаг времени неравномерен (за пределами ±20%)")

    analyzers = {"ignition": lambda: _analyze_ignition(table, spec, fuel_ron),
                 "fuel": lambda: _analyze_fuel(table, spec),
                 "vanos": lambda: _analyze_vanos(table, spec)}
    outcome = analyzers[pack]()
    issues.extend(outcome["issues"])
    notes = outcome.get("notes", [])
    outcome["results"]["wot_detection"] = (
        "pedal" if _compact(_series(table, "pedal")) else "load"
    )

    duration = 0.0
    time = _compact(_series(table, "time"))
    if len(time) >= 2:
        duration = time[-1] - time[0]

    outcome_payload = {
        "kind": "pack_log",
        "schema_version": SCHEMA_VERSION,
        "package": pack,
        "generator": GENERATOR_ID,
        "analyzer_version": ANALYZER_VERSION,
        "sha256_normalization": SHA256_NORMALIZATION,
        "source": str(path),
        "source_sha256": log_sha256(path),
        "source_format": table.get("format", "csv"),
        "source_encoding": table.get("encoding"),
        "rows": table["rows"],
        "duration_s": round(duration, 3),
        "sample_rate_hz": round(rate, 2),
        "fuel_ron": fuel_ron,
        "results": outcome["results"],
        "issues": issues,
        "notes": notes,
        "valid": not issues,
    }
    # Рекомендация генератору (задание `docs/TASK_VARIANT1_RU.md` §3): числа
    # выводятся из этого же лога; генератор примет её только вместе с sha256
    # лога и версией анализатора и всё равно ограничит своими воротами.
    try:
        recommendation = pack_recommendation.build_recommendation(
            table, pack, spec, fuel_ron)
    except (KeyError, TypeError, ValueError) as exc:  # pragma: no cover
        issues.append(f"рекомендация не построена: {exc}")
    else:
        recommendation["source_sha256"] = outcome_payload["source_sha256"]
        outcome_payload["pack_recommendation"] = recommendation
    if table.get("conversions"):
        outcome_payload["conversions"] = list(table["conversions"])
    if table.get("test0_db"):
        outcome_payload["test0_db"] = {
            "jobs": [job["name"] for job in table["test0_db"]["jobs"]],
            "merge_tolerance_s": table["test0_db"]["merge_tolerance_s"],
        }
    return outcome_payload


def write_evidence(outcome: dict, out_path: str | Path,
                   source_as: str | None = None) -> Path:
    """Пишет доказательство; ``source_as`` задаёт канонический путь лога."""
    payload = dict(outcome)
    if source_as:
        payload["source"] = source_as
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return out_path


# ---------------------------------------------------------------------------
# Ворота: принимается только доказательство анализатора
# ---------------------------------------------------------------------------

def _resolve_source(source: str, search_dirs=()) -> Path | None:
    candidates = [Path(source)]
    candidates.extend(Path(directory) / source for directory in search_dirs)
    candidates.append(REPO_ROOT / source)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _results_equal(a: dict, b: dict) -> bool:
    if set(a) != set(b):
        return False
    for key in a:
        left, right = a[key], b[key]
        if isinstance(left, float) or isinstance(right, float):
            try:
                if abs(float(left) - float(right)) > 1e-6:
                    return False
                continue
            except (TypeError, ValueError):
                return False
        if left != right:
            return False
    return True


def verify_evidence(evidence: dict | None, pack: str,
                    search_dirs=()) -> tuple[bool, tuple[str, ...]]:
    """Ворота задания 1: доказательство обязано быть выводом анализатора.

    Порядок проверок: метка генератора → поиск исходного CSV → sha256 →
    пересчёт метрик и побайтовое сравнение. Ручной JSON не проходит уже на
    первой проверке и получает отдельный код ``EVIDENCE_MANUAL_JSON``.
    """
    if evidence is None:
        return False, ("доказательство не приложено; прогоните лог через "
                       "tools/pack_log_analyzer.py",)
    if evidence.get("generator") != GENERATOR_ID:
        return False, (
            f"{EVIDENCE_MANUAL_JSON}: ручной pack_log-JSON доказательством не "
            "является — доказательство должен выдать анализатор "
            "(python3 tools/pack_log_analyzer.py ...)",
        )
    if evidence.get("analyzer_version") != ANALYZER_VERSION:
        return False, (
            f"{EVIDENCE_TAMPERED}: версия анализатора "
            f"{evidence.get('analyzer_version')!r} не совпадает с текущей "
            f"{ANALYZER_VERSION}",
        )
    if evidence.get("package") != pack:
        return False, (
            f"доказательство для пакета {evidence.get('package')!r}, нужен {pack!r}",
        )
    source = str(evidence.get("source") or "")
    if not source:
        return False, (f"{EVIDENCE_SOURCE_NOT_FOUND}: не указан путь к логу",)
    csv_path = _resolve_source(source, search_dirs)
    if csv_path is None:
        return False, (
            f"{EVIDENCE_SOURCE_NOT_FOUND}: исходный лог {source!r} не найден — "
            "доказательство принимается только вместе с файлом, по которому оно "
            "выдано",
        )
    digest = hashlib.sha256(normalize_log_bytes(csv_path.read_bytes())).hexdigest()
    raw_digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    declared = evidence.get("source_sha256")
    if declared == digest:
        pass  # нормализованный хэш (текущая версия)
    elif declared == raw_digest and not evidence.get("sha256_normalization"):
        pass  # старое доказательство без поля нормализации — принято по сырому хэшу
    else:
        return False, (
            f"{EVIDENCE_SHA256_MISMATCH}: sha256 лога {digest} не совпадает с "
            f"записанным в доказательстве {declared}",
        )
    recomputed = analyze_pack_log(csv_path, pack, evidence.get("fuel_ron"))
    if not _results_equal(recomputed["results"], evidence.get("results") or {}):
        return False, (
            f"{EVIDENCE_TAMPERED}: результаты в доказательстве не совпадают с "
            "пересчётом из исходного лога",
        )
    if recomputed["source_sha256"] != evidence.get("source_sha256"):
        return False, (f"{EVIDENCE_TAMPERED}: sha256 пересчёта не сошёлся",)
    # Рекомендация обязана быть выводом анализатора из того же лога: она
    # пересчитывается и сравнивается целиком, поэтому подправить числа руками
    # (например, поднять добавку по зоне) нельзя — ворота это увидят.
    declared_rec = evidence.get("pack_recommendation")
    if declared_rec is not None:
        if not isinstance(declared_rec, dict):
            return False, (f"{EVIDENCE_TAMPERED}: pack_recommendation не словарь",)
        if declared_rec.get("source_sha256") != evidence.get("source_sha256"):
            return False, (
                f"{EVIDENCE_TAMPERED}: рекомендация не привязана к sha256 лога",)
        fresh_rec = pack_recommendation.build_recommendation(
            read_log_table(csv_path), pack, load_requirements(),
            evidence.get("fuel_ron"))
        fresh_rec["source_sha256"] = recomputed["source_sha256"]
        if json.dumps(fresh_rec, ensure_ascii=False, sort_keys=True) != \
                json.dumps(declared_rec, ensure_ascii=False, sort_keys=True):
            return False, (
                f"{EVIDENCE_TAMPERED}: рекомендация в доказательстве не совпадает "
                "с пересчётом из исходного лога",
            )
    return True, ()


def main(argv=None) -> int:
    # Русская консоль Windows пишет в cp1251: разрешаем замену вместо падения.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    parser = argparse.ArgumentParser(
        description="Анализатор клиентского лога для пакетов зажигание/топливо/ВАНос")
    parser.add_argument("--pack", required=True, choices=("ignition", "fuel", "vanos"))
    parser.add_argument("--csv", required=True,
                        help="путь к логу: CSV-экспорт Test0/TestO или родная "
                             "база Test0 (YYYY_MM_DD.db)")
    parser.add_argument("--ron", type=float, default=None,
                        help="RON топлива из карточки лога (для зажигания не ниже 98)")
    parser.add_argument("--source-as", default=None,
                        help="канонический путь лога внутри доказательства")
    parser.add_argument("--out", default=None, help="куда писать доказательство")
    parser.add_argument("--recommend", action="store_true",
                        help="показать только рекомендацию генератору "
                             "(pack_recommendation) без метрик заезда")
    args = parser.parse_args(argv)

    outcome = analyze_pack_log(args.csv, args.pack, args.ron)
    if args.recommend:
        print(json.dumps(outcome.get("pack_recommendation") or {
            "kind": "pack_recommendation",
            "confidence": "insufficient",
            "warnings": outcome["issues"],
            "zones": [],
        }, ensure_ascii=False, indent=2))
        return 0 if outcome.get("pack_recommendation") else 1
    if args.out:
        write_evidence(outcome, args.out, args.source_as)
        print(f"доказательство записано: {args.out}")
    else:
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
    print(f"итог: {'ПРИНЯТ' if outcome['valid'] else 'ОТКЛОНЁН'} "
          f"(замечаний: {len(outcome['issues'])})", file=sys.stderr)
    return 0 if outcome["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
