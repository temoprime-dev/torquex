# -*- coding: utf-8 -*-
"""Чтение логов Test0/TestO: CSV-экспорт и родной SQLite ``.db``.

Задание `docs/TASK_LOGS_RU.md` (шаг 1): анализатор обязан принимать **оба**
представления Test0 — CSV и файл базы ``YYYY_MM_DD.db`` — и склеивать каналы
одного времени из разных job-ов в одну временную сетку.

Имена каналов приходят не «как в требованиях», а как поля DME:
``STAT_MOTORDREHZAHL_WERT`` / ``STATUS_MOTORDREHZAHL_WERT`` (EDIABAS), с
префиксом записи ``SET_1.`` у legacy TestO. Таблица «имя поля PRG → канал»
живёт в ``docs/test0_channels.json`` (имена сверены с выгрузками PRG
``docs/sgbd/``; тест ``tests/test_test0_log.py`` проверяет каждое имя).

Единицы: Test0 v2 пишет их в ``*_EINH`` (в ``.db``) или в квадратных скобках
в заголовке CSV; анализатор пакетов проверяет согласованность
(``docs/PACKS_LOG_REQUIREMENTS_RU.md`` §1.1). Единственное преобразование,
которое здесь делается молча в числах, — AFR → λ по стехиометрии бензина
(``lambda2afr = 14.7`` из конфигурации внешнего логгера 14Point7); оно
фиксируется в поле ``conversions`` и в доказательстве.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANNELS_FILE = REPO_ROOT / "docs" / "test0_channels.json"

#: Стехиометрическое число бензина для пересчёта AFR → λ (``lambda2afr``
#: внешнего логгера 14Point7; стандарт для бензина в Innovate/AEM/PLX).
AFR_STOICH = 14.7

#: Служебные результаты EDIABAS, не несущие измерений.
_TEL = re.compile(r"^_tel_", re.I)
_LOGGER_PREFIX = re.compile(r"^\s*set_\d+\.", re.I)
_EINH = re.compile(r"_EINH$", re.I)
_WERT = re.compile(r"_WERT$", re.I)
_UNIT_BRACKETS = re.compile(r"\[([^\]]*)\]\s*$")
#: Поля-подписи, которые Test0 пишет рядом со значением (текст/единица/счётчик).
_NON_VALUE = ("_TEXT", "_EINH")


def is_test0_db(path: str | Path) -> bool:
    """Файл похож на SQLite (родная база Test0), а не на CSV/текст."""
    try:
        with open(path, "rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def load_catalog() -> dict:
    with open(CHANNELS_FILE, encoding="utf-8") as handle:
        return json.load(handle)


def strip_logger_prefix(header: str) -> str:
    """Убирает префикс записи legacy TestO (``SET_1.``, ``SET_12.``)."""
    return _LOGGER_PREFIX.sub("", header.strip())


def has_logger_prefix(header: str) -> bool:
    """Заголовок начинается с префикса записи legacy TestO (``SET_1.``)."""
    return bool(_LOGGER_PREFIX.match(header.strip()))


def split_unit(header: str) -> tuple[str, str | None]:
    """Отделяет единицу в квадратных скобках: ``NAME [V]`` → ``(NAME, V)``."""
    match = _UNIT_BRACKETS.search(header.strip())
    if not match:
        return header.strip(), None
    return header[: match.start()].strip(), match.group(1).strip()


def ecus() -> tuple[str, ...]:
    return tuple(load_catalog()["ecus"])


def channel_names(only_extra: bool = False) -> tuple[str, ...]:
    """Имена каналов каталога (либо только те, которых нет в требованиях).

    ``only_extra=True`` отдаёт сервисные каналы (``disa_status``,
    ``idle_actuator`` и т. п.), которых нет в ``docs/pack_log_requirements.json``:
    анализатор обязан их читать и показывать, даже если конкретный пакет их
    пока не использует.
    """
    names = tuple(load_catalog()["channels"])
    if not only_extra:
        return names
    with open(REPO_ROOT / "docs" / "pack_log_requirements.json",
              encoding="utf-8") as handle:
        spec = json.load(handle)["channels"]
    return tuple(name for name in names if name not in spec)


def channel_map(ecu: str | None = None) -> tuple[dict[str, str], dict[tuple[str, str], str], set[str]]:
    """Три карты: по имени результата, по (job, результат) и спорные имена.

    ``ambiguous`` — имена, встречающиеся у разных job-ов (в MS45 это
    ``STAT_AUSGANG`` у ``STATUS_E_LUEFTER``, ``STATUS_LL_STELLER``,
    ``STATUS_VIMDISA`` и др.). По имени без job-а такой результат не
    различается, поэтому в плоскую карту он не попадает: адресовать его
    можно только парой «job + результат».
    """
    catalog = load_catalog()
    by_name: dict[str, str] = {}
    by_job: dict[tuple[str, str], str] = {}
    ambiguous: set[str] = set()
    for channel, spec in catalog["channels"].items():
        if spec.get("external"):
            continue
        for family, sources in spec.get("ecus", {}).items():
            if ecu is not None and family != ecu:
                continue
            for source in sources:
                for result in source["results"]:
                    key = result.upper()
                    by_job[(source["job"].upper(), key)] = channel
                    if source.get("ambiguous"):
                        ambiguous.add(key)
                        continue
                    existing = by_name.get(key)
                    if existing is not None and existing != channel:
                        ambiguous.add(key)
                    by_name[key] = channel
    for key in ambiguous:
        by_name.pop(key, None)
    return by_name, by_job, ambiguous


def channel_for_result(result_name: str, job: str | None = None,
                       ecu: str | None = None) -> str | None:
    """Канал для результата PRG; при известном job-е — точное совпадение."""
    by_name, by_job, _ = channel_map(ecu)
    key = strip_logger_prefix(result_name).upper()
    if job:
        channel = by_job.get((strip_logger_prefix(job).upper(), key))
        if channel:
            return channel
    return by_name.get(key)


# ---------------------------------------------------------------------------
# Родной формат Test0: SQLite .db
# ---------------------------------------------------------------------------

def _connect_readonly(path: str | Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def _require_tables(connection: sqlite3.Connection) -> None:
    missing = {"jobs", "result_definitions", "job_executions"} - _table_names(connection)
    if missing:
        raise ValueError(
            "это не родная база Test0: нет таблиц " + ", ".join(sorted(missing))
        )


def _as_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".").replace("\u00a0", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _job_name(row: dict) -> str:
    for key in ("name", "displayname", "displayName"):
        if row.get(key):
            return str(row[key])
    return ""


def _read_jobs(connection: sqlite3.Connection) -> dict[int, dict]:
    columns = [row[1] for row in connection.execute("PRAGMA table_info(jobs)")]
    select = ["id", "prgName"] + [c for c in ("name", "displayname", "displayName")
                                  if c in columns]
    jobs: dict[int, dict] = {}
    for row in connection.execute(f"SELECT {', '.join(select)} FROM jobs"):
        record = dict(zip(select, row))
        jobs[int(record["id"])] = {
            "prg": record.get("prgName") or "",
            "name": _job_name(record),
        }
    return jobs


def _read_definitions(connection: sqlite3.Connection) -> dict[int, list[dict]]:
    definitions: dict[int, list[dict]] = {}
    for job_id, idx, name, unit in connection.execute(
            "SELECT jobId, idx, name, unit FROM result_definitions"):
        definitions.setdefault(int(job_id), []).append(
            {"idx": int(idx), "name": str(name or ""), "unit": unit}
        )
    return definitions


def _median_interval(series: dict[float, float]) -> float:
    """Медианный шаг опроса одного канала (с), 0.0 — если точек мало."""
    moments = sorted(series)
    steps = [b - a for a, b in zip(moments, moments[1:]) if b > a]
    return sorted(steps)[len(steps) // 2] if steps else 0.0


def _merge_series(series: dict[float, float], master: list[float],
                  tolerance: float) -> list[float | None]:
    """Значения канала на общей временной сетке (ближайшая точка в допуске)."""
    if not series:
        return [None] * len(master)
    points = sorted(series.items())
    values: list[float | None] = []
    cursor = 0
    for moment in master:
        while cursor + 1 < len(points) and (
                abs(points[cursor + 1][0] - moment)
                <= abs(points[cursor][0] - moment) + 1e-9):
            cursor += 1
        best = points[cursor]
        values.append(
            best[1] if abs(best[0] - moment) <= tolerance + 1e-9 else None)
    return values


def read_test0_db(path: str | Path, ecu: str | None = None) -> dict:
    """Читает родной ``.db`` Test0 в таблицу каналов анализатора.

    ``ts`` в ``job_executions`` — миллисекунды Unix; ``vals`` — JSON-массив,
    выровненный по ``idx`` (первые два индекса — служебные
    ``_TEL_AUFTRAG``/``_TEL_ANTWORT``). Разные job-ы опрашиваются в разное
    время, поэтому каналы склеиваются по ближайшей отметке времени с допуском
    ``tolerance`` (половина медианного шага, но не больше секунды).
    """
    if not is_test0_db(path):
        raise ValueError(f"{path}: не похоже на базу SQLite (родной формат Test0)")

    with closing(_connect_readonly(path)) as connection:
        _require_tables(connection)
        jobs = _read_jobs(connection)
        definitions = _read_definitions(connection)
        indexes: dict[int, dict[str, dict]] = {}
        for job_id, items in definitions.items():
            by_idx = {item["idx"]: item for item in items}
            indexes[job_id] = by_idx

        issues: list[str] = []
        conversions: list[str] = []
        per_channel: dict[str, dict[float, float]] = {}
        units: dict[str, str] = {}

        for job_id, _job_num, ts, vals in connection.execute(
                "SELECT jobId, jobNum, ts, vals FROM job_executions ORDER BY ts, id"):
            info = jobs.get(int(job_id))
            if not info:
                continue
            by_idx = indexes.get(int(job_id), {})
            if not by_idx:
                continue
            try:
                values = json.loads(vals) if vals else []
            except (TypeError, ValueError):
                issues.append(
                    f"job {info['name']}: значения не разобраны как JSON — пропуск"
                )
                continue
            if not isinstance(values, list):
                continue
            moment = float(ts) / 1000.0
            # Числовые значения по индексам (единицу берём из *_EINH).
            for idx, item in by_idx.items():
                name = item["name"]
                if _TEL.search(name) or idx >= len(values):
                    continue
                if _EINH.search(name) or name.upper().endswith(_NON_VALUE):
                    continue
                channel = channel_for_result(name, job=info["name"], ecu=ecu)
                if channel is None:
                    continue
                value = _as_float(values[idx])
                if value is None:
                    continue
                unit = item.get("unit")
                if not unit:
                    # Единица живёт в соседнем результате STAT_<X>_EINH.
                    for other in by_idx.values():
                        if (other["name"].upper() == (name.upper() + "_EINH")
                                and other["idx"] < len(values)):
                            unit = str(values[other["idx"]] or "")
                            break
                if unit:
                    units.setdefault(channel, unit)
                if (channel == "lambda_wideband" and unit
                        and "afr" in unit.lower()):
                    marker = f"{name}: AFR→λ (/{AFR_STOICH})"
                    if marker not in conversions:
                        conversions.append(marker)
                    value = value / AFR_STOICH
                # Один канал — один job: служебные job-ы не подменяют источник.
                per_channel.setdefault(channel, {}).setdefault(moment, value)

    if not per_channel:
        raise ValueError(
            "в базе не найдено ни одного знакомого канала Test0 "
            "(ожидались поля PRG вида STAT_*_WERT; см. docs/test0_channels.json)"
        )

    master = sorted({moment for series in per_channel.values() for moment in series})
    steps = [b - a for a, b in zip(master, master[1:]) if b > a]
    median_step = sorted(steps)[len(steps) // 2] if steps else 0.0
    default_tolerance = min(max(median_step / 2.0, 0.02), 1.0)

    # Каждый канал опрашивается своим job-ом с собственным шагом: значение
    # канала считается действительным до следующего опроса (половина его
    # медианного шага, но не больше секунды).
    channels: dict[str, list] = {"time": master}
    tolerances: dict[str, float] = {}
    for channel, series in per_channel.items():
        interval = _median_interval(series)
        # Небольшой запас на дрожание расписания опроса Test0 (целые мс).
        tolerance = (min(max(interval * 0.55, 0.02), 1.0) if interval
                     else default_tolerance)
        tolerances[channel] = round(tolerance, 4)
        channels[channel] = _merge_series(series, master, tolerance)

    raw_rows = []
    for position in range(len(master)):
        cells = [f"{master[position]:.6f}".rstrip("0").rstrip(".")]
        for channel in sorted(per_channel):
            value = channels[channel][position]
            cells.append("" if value is None else f"{value:.6g}")
        raw_rows.append(";".join(cells))

    found = sorted(channels)
    return {
        "channels": channels,
        "headers": found,
        "raw_rows": raw_rows,
        "encoding": "sqlite",
        "delimiter": ";",
        "decimal_separator": ".",
        "rows": len(master),
        "issues": issues,
        "conversions": conversions,
        "format": "test0_db",
        "test0_db": {
            "path": str(path),
            "jobs": [
                {"id": job_id, "name": info["name"], "prg": info["prg"]}
                for job_id, info in jobs.items()
            ],
            "ambiguous_names": sorted(channel_map(ecu)[2]),
            "units": units,
            "merge_tolerance_s": tolerances,
            "channels_found": found,
        },
    }


def annotate_presets_for_channels(missing: list[str], ecu: str | None = None) -> str:
    """Подсказка клиенту, какими job-ами включить каналы в Test0.

    Возвращает одну строку вида
    ``rpm → MS43: job STATUS_MOTORDREHZAHL (STAT_MOTORDREHZAHL_WERT)``.
    """
    catalog = load_catalog()
    parts = []
    unavailable = catalog.get("unavailable_ru") or {}
    for name in missing:
        channel = catalog["channels"].get(name)
        if channel is None:
            # Канала нет в каталоге (например, knock_retard): если для него
            # записана причина «в PRG этого семейства нет» — объясняем её,
            # первую фразу, чтобы не растягивать сообщение об отказе.
            reason = unavailable.get(name)
            if reason is None and ecu:
                reason = unavailable.get(f"{name}_{ecu.lower()}")
            if reason:
                reason = reason.split(". ")[0].rstrip(".")
            parts.append(f"{name} — {reason}" if reason else name)
            continue
        options = []
        for family, sources in channel.get("ecus", {}).items():
            if ecu is not None and family != ecu:
                continue
            for source in sources:
                options.append(
                    f"{family}: job {source['job']} "
                    f"({', '.join(source['results'])})"
                )
        if channel.get("external"):
            options.append(
                "внешний источник Test0: Innovate/AEM/PLX/14Point7/Techedge "
                "(testo.ini [external_source])"
            )
        parts.append(f"{name} → " + ("; ".join(options) if options
                                     else channel.get("note_ru", "недоступен в PRG")))
    return ", ".join(parts)
