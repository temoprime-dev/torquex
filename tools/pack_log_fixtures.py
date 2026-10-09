# -*- coding: utf-8 -*-
"""Синтетические фикстуры клиентских логов для тестов и примеров.

Реальные логи придут от клиентов; пока их нет, тесты и примеры в репозитории
используют синтетику, которая удовлетворяет требованиям
``docs/PACKS_LOG_REQUIREMENTS_RU.md`` (или нарушает их — для негативных
кейсов). Генераторы детерминированы (фиксированные сиды), поэтому байты
файлов примеров воспроизводимы.

Каналы и формат — по ``docs/pack_log_requirements.json``:
время с; обороты об/мин; нагрузка мг/такт; расход кг/ч; углы °КВ.
Частота 10 Гц (порог спецификации).

Использование из тестов::

    from tools import pack_log_fixtures as fx
    csv_path = fx.write_ignition_csv(tmpdir / "wot.csv")          # годный
    bad = fx.write_ignition_csv(tmpdir / "knock.csv", knock=True) # детонация
    evidence = fx.evidence_from_csv(csv_path, "ignition", ron=98)

Восстановление логов примеров::

    python3 tools/pack_log_fixtures.py --write-examples MS43 MS45
"""

from __future__ import annotations

import argparse
import json
import random
from contextlib import closing
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import pack_log_analyzer as analyzer  # noqa: E402

IGN_HEADERS = [
    "time", "rpm", "load", "maf", "ignition angle", "knock retard",
    "knock voltage cyl 1", "knock voltage cyl 2", "knock voltage cyl 3",
    "knock voltage cyl 4", "knock voltage cyl 5", "knock voltage cyl 6",
    "throttle", "coolant", "iat",
]
FUEL_HEADERS = [
    "time", "rpm", "load", "maf", "ipw", "wideband", "stft",
    "throttle", "coolant", "iat",
]
VANOS_HEADERS = [
    "time", "rpm", "load", "cam_in", "cam_ex",
    "vanos intake target", "vanos exhaust target",
    "coolant", "throttle", "ignition angle", "knock retard",
]


def _fmt(value: float) -> str:
    return f"{value:.2f}"


# ---------------------------------------------------------------------------
# Зажигание: три полных разгона 2000 → отсечка
# ---------------------------------------------------------------------------

def write_ignition_csv(path, knock: bool = False, hot_iat: bool = False) -> Path:
    """Годный лог зажигания; ``knock`` добавляет устойчивый откат угла,
    ``hot_iat`` — перегретый впуск (оба случая обязаны отклоняться)."""
    rng = random.Random(7)
    rows: list[str] = []
    time = 0.0

    def pause() -> None:
        nonlocal time
        for _ in range(30):
            rows.append(";".join(_fmt(v) for v in [
                time, 900 + rng.uniform(-6, 6), 80 + rng.uniform(-4, 4),
                15.0, 10.0, 0.0,
                *[0.30 + rng.uniform(-0.03, 0.03) for _ in range(6)],
                5.0, 88.0 + rng.uniform(-1, 1), 30.0 + rng.uniform(-2, 2),
            ]))
            time += 0.1

    def pull(sustained_knock: bool) -> None:
        nonlocal time
        rpm = 2000.0
        while rpm < 6400:
            progress = (rpm - 2000.0) / 4400.0
            load = 380.0 + progress * 320.0 + rng.uniform(-10, 10)
            retard = rng.uniform(0.0, 0.4)
            rows.append(";".join(_fmt(v) for v in [
                time, rpm, load, load * 2.2,
                28.0 - progress * 10.0 + rng.uniform(-0.3, 0.3), retard,
                *[0.32 + rng.uniform(-0.06, 0.06) for _ in range(6)],
                100.0, 90.0 + rng.uniform(-2, 2),
                30.0 + rng.uniform(-3, 3),
            ]))
            time += 0.1
            rpm += 45.0
        if sustained_knock:
            # 0.8 с устойчивого отката ≥ 1.5° — признак детонации
            for _ in range(8):
                rows.append(";".join(_fmt(v) for v in [
                    time, 6400.0, 660.0, 1400.0, 17.5, 2.25,
                    *[0.32 + rng.uniform(-0.05, 0.05) for _ in range(6)],
                    100.0, 94.0, 33.0,
                ]))
                time += 0.1

    for index in range(3):
        pause()
        pull(sustained_knock=knock and index == 1)
    if hot_iat:
        # хвост с перегретым впуском: датчики показывают 50..55 °C
        for _ in range(40):
            rows.append(";".join(_fmt(v) for v in [
                time, 3000.0, 430.0, 900.0, 22.0, 0.2,
                *[0.32 + rng.uniform(-0.05, 0.05) for _ in range(6)],
                100.0, 92.0, 52.0 + rng.uniform(-2, 3),
            ]))
            time += 0.1
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([";".join(IGN_HEADERS)] + rows) + "\n",
                    encoding="utf-8", newline="\n")
    return path


# ---------------------------------------------------------------------------
# Топливо: два полных разгона + установившаяся частичная нагрузка
# ---------------------------------------------------------------------------

def write_fuel_csv(path, unstable: bool = False, lean: bool = False,
                   narrowband: bool = False, big_trim: bool = False) -> Path:
    """Годный лог широкой лямбды.

    ``unstable``  — разброс λ на полном ходу больше 0.03;
    ``lean``      — фактическая λ уходит ниже 0.82;
    ``narrowband``— зонд «залипает» (дисперсия ≈ 0);
    ``big_trim``  — тримы частичной нагрузки больше ±8 %.
    """
    rng = random.Random(11)
    rows: list[str] = []
    time = 0.0

    def pause() -> None:
        nonlocal time
        for _ in range(20):
            rows.append(",".join(_fmt(v) for v in [
                time, 900 + rng.uniform(-5, 5), 80 + rng.uniform(-3, 3),
                15.0, 2.0, 0.99, rng.uniform(-2, 2),
                5.0, 88.0 + rng.uniform(-1, 1), 30.0 + rng.uniform(-1, 1),
            ]))
            time += 0.1

    def pull() -> None:
        nonlocal time
        rpm = 2000.0
        while rpm < 6400:
            progress = (rpm - 2000.0) / 4400.0
            load = 380.0 + progress * 320.0
            if narrowband:
                lamb = 0.85
            elif lean:
                lamb = 0.87 + rng.uniform(-0.09, 0.07)
            elif unstable:
                lamb = 0.87 + rng.uniform(-0.06, 0.06)
            else:
                lamb = 0.87 + rng.uniform(-0.025, 0.025)
            rows.append(",".join(_fmt(v) for v in [
                time, rpm, load, load * 2.2,
                8.0 + progress * 8.0, lamb, rng.uniform(-3, 3),
                100.0, 90.0 + rng.uniform(-2, 2), 30.0 + rng.uniform(-2, 2),
            ]))
            time += 0.1
            rpm += 45.0

    for _ in range(2):
        pause()
        pull()
    # установившаяся частичная нагрузка: ~2500 об/мин, ~13 с
    for _ in range(130):
        trim = rng.uniform(-12, 12) if big_trim else rng.uniform(-3, 3)
        rows.append(",".join(_fmt(v) for v in [
            time, 2500 + rng.uniform(-30, 30), 250.0, 550.0,
            6.0, 0.97, trim, 35.0, 90.0 + rng.uniform(-1, 1), 31.0,
        ]))
        time += 0.1
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([",".join(FUEL_HEADERS)] + rows) + "\n",
                    encoding="utf-8", newline="\n")
    return path


# ---------------------------------------------------------------------------
# ВАНос: сетка 9 оборотов × 4 нагрузки + контрольный разгон
# ---------------------------------------------------------------------------

def write_vanos_csv(path, track_error_deg: float = 0.8,
                    drop_points: int = 0) -> Path:
    """Годный лог фаз; ``track_error_deg`` — амплитуда рассогласования
    факт/цель (порог 3°), ``drop_points`` — сколько точек сетки пропущено."""
    rng = random.Random(3)
    rows: list[str] = []
    time = 0.0
    rpms = [2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000]
    loads = [150, 300, 450, 600]
    cells = [(rpm, load) for rpm in rpms for load in loads]
    if drop_points:
        cells = cells[:-drop_points]
    for rpm, load in cells:
        target_in = 100.0 + rpm / 6000.0 * 20.0
        target_ex = 100.0 - rpm / 6000.0 * 15.0
        for _ in range(35):  # 3.5 с удержания при пороге 3 с
            rows.append(";".join(_fmt(v) for v in [
                time, rpm + rng.uniform(-20, 20), load + rng.uniform(-10, 10),
                target_in + rng.uniform(-track_error_deg, track_error_deg),
                target_ex + rng.uniform(-track_error_deg, track_error_deg),
                target_in, target_ex,
                88.0 + rng.uniform(-1, 1), 30.0,
                20.0 + rng.uniform(-1, 1), 0.0,
            ]))
            time += 0.1
    rpm = 2000.0
    while rpm < 6400:  # контрольный разгон для переходных целей
        rows.append(";".join(_fmt(v) for v in [
            time, rpm, 500.0, 110.0, 95.0, 110.0, 95.0,
            90.0, 100.0, 25.0, 0.0,
        ]))
        time += 0.1
        rpm += 60.0
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([";".join(VANOS_HEADERS)] + rows) + "\n",
                    encoding="utf-8", newline="\n")
    return path


# ---------------------------------------------------------------------------
# Формат реального логгера (задание 2 ревью №2): заголовки с единицами в
# квадратных скобках, номера цилиндров в круглых, без ОЖ/педали/коррекции.
# ---------------------------------------------------------------------------

def write_e46_logger_csv(path, full: bool = False,
                         knock_voltage: bool = True) -> Path:
    """Лог в формате реального инструмента клиента (задание 2 ревью №2).

    ``full`` — добавить каналы, которых не хватало в реальном логе
    (температура ОЖ и коррекция детонации): такой лог обязан проходить
    анализатор, причём «газ в пол» находится по нагрузке (педали нет).
    ``knock_voltage=False`` — убрать ещё и напряжения датчиков детонации
    (канал вспомогательный: лог остаётся годным, появляется примечание).
    """
    rng = random.Random(21)
    headers = ["TIME [s]", "Engine Speed [RPM]", "Engine Load [mg/stroke]",
               "Ignition Advance [deg]"]
    if knock_voltage:
        headers += [f"Knock Voltage (Cylinder {index}) [V]"
                    for index in range(1, 7)]
    headers += ["Lambda Actual Value (Bank 1)", "Mass Air Flow [kg/h]",
                "Intake Air Temp [deg C]", "Vehicle Speed [km/h]",
                "Intake VANOS: Actual Value [deg]",
                "Exhaust VANOS: Actual Value [deg]", "Injection Time [ms]"]
    if full:
        headers += ["Coolant Temp [deg C]", "Knock Retard [deg]"]
    rows = [",".join(headers)]
    time = 0.0

    def emit(values: list[float]) -> None:
        rows.append(",".join(f"{value:.2f}" for value in values))

    def tail(base: list[float]) -> list[float]:
        if full:
            return base + [90.0 + rng.uniform(-2, 2), rng.uniform(0, 0.4)]
        return base

    def pause() -> None:
        nonlocal time
        for _ in range(30):
            base = [time, 900 + rng.uniform(-5, 5), 80 + rng.uniform(-4, 4),
                    10.0]
            if knock_voltage:
                base += [0.30 + rng.uniform(-0.03, 0.03) for _ in range(6)]
            base += [0.98, 15.0, 30.0 + rng.uniform(-1, 1), 0.0, 60.0,
                     100.0, 2.0]
            emit(tail(base))
            time += 0.1

    def pull() -> None:
        nonlocal time
        rpm = 2000.0
        while rpm < 6400:
            progress = (rpm - 2000.0) / 4400.0
            load = 380.0 + progress * 320.0 + rng.uniform(-10, 10)
            base = [time, rpm, load,
                    28.0 - progress * 10.0 + rng.uniform(-0.3, 0.3)]
            if knock_voltage:
                base += [0.32 + rng.uniform(-0.06, 0.06) for _ in range(6)]
            base += [0.87 + rng.uniform(-0.02, 0.02), load * 2.2,
                     30.0 + rng.uniform(-3, 3),
                     40.0 + progress * 120.0, 105.0, 95.0,
                     8.0 + progress * 8.0]
            emit(tail(base))
            time += 0.1
            rpm += 45.0

    for _ in range(3):
        pause()
        pull()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    return path


# ---------------------------------------------------------------------------
# Формат Test0 (задание docs/TASK_LOGS_RU.md): CSV-экспорт и родная база
# ---------------------------------------------------------------------------

#: Job-ы и результаты MS43 из docs/test0_channels.json — «расписание» опроса
#: синтетической базы Test0: (job, результат, единица).
TEST0_DB_JOBS = [
    ("STATUS_MOTORDREHZAHL", "STAT_MOTORDREHZAHL_WERT", "1/min"),
    ("STATUS_LAST", "STAT_LAST_WERT", "mg/stroke"),
    ("STATUS_ZUENDWINKEL", "STAT_ZUENDWINKEL_WERT", "deg"),
    ("STATUS_LMM", "STAT_LMM_WERT", "kg/h"),
    ("STATUS_MOTORTEMPERATUR_MWB", "STAT_MOTORTEMPERATUR_MWB_WERT", "deg C"),
    ("STATUS_AN_LUFTTEMPERATUR", "STAT_AN_LUFTTEMPERATUR_WERT", "deg C"),
]


def _engine_model(moment: float, rng: random.Random) -> dict[str, float]:
    """Синтетический «двигатель»: 3 разгона полного газа 2000→6500 об/мин."""
    cycle = 7.4  # 3.0 с пауза + 4.4 с разгон
    phase = moment % cycle
    if phase < 3.0:  # пауза
        return {"rpm": 900 + rng.uniform(-6, 6),
                "load": 80 + rng.uniform(-4, 4),
                "ignition": 10 + rng.uniform(-0.2, 0.2),
                "maf_airflow": 15 + rng.uniform(-0.5, 0.5),
                "coolant_temp": 90 + rng.uniform(-1, 1),
                "intake_temp": 30 + rng.uniform(-1, 1)}
    progress = (phase - 3.0) / 4.4
    rpm = 2000 + progress * 4500
    load = 400 + progress * 300 + rng.uniform(-10, 10)
    return {"rpm": rpm,
            "load": load,
            "ignition": 28 - progress * 10 + rng.uniform(-0.3, 0.3),
            "maf_airflow": load * 2.2 + rng.uniform(-5, 5),
            "coolant_temp": 90 + rng.uniform(-1, 1),
            "intake_temp": 30 + rng.uniform(-2, 2)}


#: Канал → имя поля PRG в расписании TEST0_DB_JOBS.
TEST0_CHANNEL_FOR_RESULT = {
    "STAT_MOTORDREHZAHL_WERT": "rpm",
    "STAT_LAST_WERT": "load",
    "STAT_ZUENDWINKEL_WERT": "ignition",
    "STAT_LMM_WERT": "maf_airflow",
    "STAT_MOTORTEMPERATUR_MWB_WERT": "coolant_temp",
    "STAT_AN_LUFTTEMPERATUR_WERT": "intake_temp",
}


def write_test0_db(path, runs: float = 0.0, corrupt_json: bool = False,
                   foreign_unit: str | None = None) -> Path:
    """Синтетическая родная база Test0 ``YYYY_MM_DD.db`` (семейство MS43).

    Схема и порядок опроса повторяют настоящий Test0: ``jobs``,
    ``result_definitions`` (idx 0/1 — ``_TEL_AUFTRAG``/``_TEL_ANTWORT``, далее
    пара «значение + ``_EINH``») и ``job_executions`` (``ts`` — мс Unix,
    ``vals`` — JSON). Job-ы опрашиваются по очереди с шагом 0.1 с, поэтому
    каналы приходятся на разные отметки времени — при чтении они
    склеиваются. ``corrupt_json=True`` портит одну запись (канал
    отбрасывается, а не выдумывается); ``foreign_unit`` задаёт имя канала
    (например, ``"coolant_temp"``), у которого ``*_EINH`` подменяется на
    чужую единицу ``F`` — такой канал обязан пропускаться с сообщением.
    """
    rng = random.Random(5)
    path = Path(path)
    if path.exists():
        path.unlink()
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY, prgName TEXT, name TEXT,
                displayname TEXT, startTime TEXT, endTime TEXT,
                runningTime INTEGER, virtualName TEXT
            );
            CREATE TABLE result_definitions (
                id INTEGER PRIMARY KEY, jobId INTEGER, idx INTEGER,
                name TEXT, unit TEXT
            );
            CREATE TABLE job_executions (
                id INTEGER PRIMARY KEY, jobId INTEGER, jobNum INTEGER,
                ts INTEGER, vals TEXT
            );
            """
        )
        for job_index, (job, result, unit) in enumerate(TEST0_DB_JOBS, start=1):
            connection.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)",
                (job_index, "MS430DS0", job, job, "2026-10-07 10:00:00",
                 "2026-10-07 10:01:00", 60, ""))
            definition_unit = unit
            if foreign_unit and TEST0_CHANNEL_FOR_RESULT[result] == foreign_unit:
                definition_unit = "F"
            definitions = [("_TEL_AUFTRAG", ""), ("_TEL_ANTWORT", ""),
                           (result, definition_unit), (result + "_EINH", "")]
            for idx, (name, unit_text) in enumerate(definitions):
                connection.execute(
                    "INSERT INTO result_definitions(jobId, idx, name, unit) "
                    "VALUES (?,?,?,?)",
                    (job_index, idx, name, unit_text))

        duration = max(runs, 22.6)
        ticks = int(round(duration / 0.1))
        for tick in range(ticks):
            job_index = (tick % len(TEST0_DB_JOBS)) + 1
            _, result, unit = TEST0_DB_JOBS[job_index - 1]
            moment = tick * 0.1
            value = _engine_model(moment, rng)[TEST0_CHANNEL_FOR_RESULT[result]]
            unit_text = unit
            if foreign_unit and TEST0_CHANNEL_FOR_RESULT[result] == foreign_unit:
                unit_text = "F"
            payload = json.dumps(
                ["8312F1224000", "AFF1", f"{value:.4f}", unit_text],
                ensure_ascii=False)
            if corrupt_json and tick == len(TEST0_DB_JOBS) * 3:
                payload = "[не JSON"  # битая запись: канал отбрасывается
            connection.execute(
                "INSERT INTO job_executions(jobId, jobNum, ts, vals) "
                "VALUES (?,?,?,?)",
                (job_index, tick, int(round(moment * 1000)), payload))
        connection.commit()
    return path


def write_test0_csv(path, full: bool = False) -> Path:
    """Синтетический CSV-экспорт Test0 (имена полей PRG из docs/sgbd).

    Точный вид экспорта Test0 v2 официально не документирован (см.
    ``docs/TEST0_WEB_ANALYSIS_RU.md``): колонки — поля PRG вида
    ``STAT_*_WERT``; в legacy TestO к ним добавлялся префикс записи
    ``SET_1.``. Обе формы поддерживаются анализатором и проверяются тестом.

    ``full=True`` добавляет канал «Knock Retard [deg]» в формате реального
    логгера: база MS4x такого канала не отдаёт, но он нужен воротам
    зажигания, поэтому синтетика проверяет обе половины.
    """
    rng = random.Random(13)
    headers = ["TIME [s]", "SET_1.STAT_MOTORDREHZAHL_WERT [1/min]",
               "STAT_LAST_WERT [mg/stroke]", "STAT_ZUENDWINKEL_WERT [deg]",
               "STAT_LMM_WERT [kg/h]", "STAT_MOTORTEMPERATUR_MWB_WERT [deg C]",
               "STAT_AN_LUFTTEMPERATUR_WERT [deg C]"]
    if full:
        headers += ["Knock Retard [deg]"]
    rows = [";".join(headers)]
    duration = 22.6
    ticks = int(round(duration / 0.1))
    for tick in range(ticks):
        moment = tick * 0.1
        model = _engine_model(moment, rng)
        values = [f"{moment:.1f}".replace(".", ","),
                  f"{model['rpm']:.0f}",
                  f"{model['load']:.0f}".replace(".", ","),
                  f"{model['ignition']:.2f}".replace(".", ","),
                  f"{model['maf_airflow']:.1f}".replace(".", ","),
                  f"{model['coolant_temp']:.1f}".replace(".", ","),
                  f"{model['intake_temp']:.1f}".replace(".", ",")]
        if full:
            values.append(f"{rng.uniform(0.0, 0.4):.2f}".replace(".", ","))
        rows.append(";".join(values))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    return path


# ---------------------------------------------------------------------------
# Доказательства
# ---------------------------------------------------------------------------

def evidence_from_csv(csv_path, pack: str, ron: float | None = 98,
                      source_as: str | None = None) -> dict:
    """Прогоняет лог через анализатор и возвращает доказательство (как есть,
    вместе с ``results``, ``source_sha256`` и версией анализатора)."""
    outcome = analyzer.analyze_pack_log(csv_path, pack, ron)
    if source_as:
        outcome["source"] = source_as
    return outcome


def write_evidence_for_examples(family_dir: Path) -> None:
    """Пересоздаёт логи и доказательства примеров семейства (детерминировано)."""
    logs = family_dir / "examples" / "logs"
    write_ignition_csv(logs / "wot_knock_free.csv")
    write_fuel_csv(logs / "wot_wideband.csv")
    write_vanos_csv(logs / "vanos_grid.csv")
    rel = logs.relative_to(REPO_ROOT).as_posix()
    for pack, csv_name, json_name in (
        ("ignition", "wot_knock_free.csv", "evidence_ignition.json"),
        ("fuel", "wot_wideband.csv", "evidence_fuel.json"),
        ("vanos", "vanos_grid.csv", "evidence_vanos.json"),
    ):
        outcome = analyzer.analyze_pack_log(logs / csv_name, pack, 98)
        outcome["source"] = f"{rel}/{csv_name}"
        analyzer.write_evidence(outcome, logs / json_name,
                                source_as=outcome["source"])
        assert outcome["source_sha256"] == analyzer.log_sha256(logs / csv_name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Пересоздание синтетических логов и доказательств примеров")
    parser.add_argument("--write-examples", nargs="+", default=["MS43", "MS45"],
                        help="семейства, для которых обновить examples/logs")
    args = parser.parse_args(argv)
    for family in args.write_examples:
        write_evidence_for_examples(REPO_ROOT / family)
        print(f"{family}: логи и доказательства примеров обновлены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
