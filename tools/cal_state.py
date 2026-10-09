# -*- coding: utf-8 -*-
"""Анализатор калибровки: заводской файл («сток») или уже изменённый («тюн»).

Генератор должен ставить Stage 1 только на сток: надбавка поверх чужой
прошивки удваивает правку и обходит потолки (+3° считаются от заводского угла).
Модуль сверяет ключевые карты загруженного файла с базой заводских вариантов
(``config/stock_fingerprints.json``, строит ``tools/build_stock_fingerprints.py``)
а также порог полной нагрузки и ограничитель оборотов.

Результат ``analyze`` — словарь:

* ``state``  — ``stock`` (карты совпали с известными заводскими вариантами),
  ``tuned`` (есть явные признаки правок), ``unknown`` (ни совпадения, ни
  признаков правки: файла нет в базе);
* ``reasons`` — короткие коды причин (для «Для специалистов»);
* ``checked`` — сколько карт сверено.

Модуль ничего не пишет и не читает посторонних файлов, кроме базы отпечатков.
Значения чужих тюнерских файлов в базу не попадают: для них хранится только
хеш заводского варианта.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FINGERPRINTS = ROOT / "config" / "stock_fingerprints.json"

STATE_STOCK = "stock"
STATE_TUNED = "tuned"
STATE_UNKNOWN = "unknown"

#: Карты, по которым судим о правках (имя в базе → символ политики MS43).
MS43_MAPS = {
    "ron98": "ip_iga_ron_98_pl_ivvt__n__maf",
    "ron91": "ip_iga_ron_91_pl_ivvt__n__maf",
    "optm": "ip_iga_optm_tco_2__n__maf",
    "fl": "id_pvs_fl__n",
    "nmax": "id_n_max_mt__gear",
    "camin": "ip_cam_sp_tco_2_in_pl__n__maf_iv",
    "camex": "ip_cam_sp_tco_2_ex_pl__n__maf_iv",
    "pop_thr": "ip_n_min_accin_puc__tco",
    "pop_iga": "ip_iga_accin_puc__n__tco",
    "pop_hys": "c_n_hys_max_puc",
}

#: Правило «явно изменено относительно ближайшего заводского варианта»:
#: (направление, минимальная средняя правка по изменённым ячейкам, подпись причины).
MAP_RULES = {
    "ron98": (+1, 0.5, "ignition_raised"),
    "ron91": (+1, 0.5, "ignition_raised"),
    "optm": (+1, 0.5, "ignition_raised"),
    "fl": (-1, 2.0, "full_load_threshold_lowered"),
    "nmax": (+1, 32.0, "rev_limit_raised"),
    "camin": (0, 1.0, "vanos_shifted"),
    "camex": (0, 1.0, "vanos_shifted"),
    "pop_thr": (+1, 300.0, "pops_enabled"),
    "pop_iga": (0, 2.0, "pops_enabled"),
    "pop_hys": (+1, 100.0, "pops_enabled"),
    "igab": (+1, 0.5, "ignition_raised"),
    "vmax": (+1, 5.0, "speed_limit_raised"),
}
#: Без одной из этих карт (зажигание) о стоке судить нельзя.
COVERAGE_KEYS = ("ron98", "igab")

#: Доля изменённых ячеек и доля правок в одну сторону для признака «изменено».
RAISED_MIN_FRACTION = 0.08
RAISED_MIN_SAME_SIGN = 0.90


def rule_for(name: str) -> tuple[int, float, str]:
    """Правило по имени карты: точное имя или префикс (``nmax_mt``, ``vanos:0``)."""
    if name in MAP_RULES:
        return MAP_RULES[name]
    if name.startswith("nmax"):
        return MAP_RULES["nmax"]
    for prefix in ("vanos",):
        if name.startswith(prefix):
            return MAP_RULES["nmax" if prefix == "nmax" else "camin"]
    return (+1, 1.0, "changed")


def fingerprint(values: list[float]) -> str:
    """Короткий хеш карты по физическим значениям (устойчив к порядку чтения)."""
    text = ",".join(f"{float(v):.4f}" for v in values)
    return hashlib.sha256(text.encode("ascii")).hexdigest()[:16]


@lru_cache(maxsize=1)
def load_db(path: str | None = None) -> dict[str, Any]:
    target = Path(path) if path else FINGERPRINTS
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _nearest(values: list[float], variants: list[list[float]]) -> tuple[list[float], int]:
    best = None
    best_diff = None
    for variant in variants:
        if len(variant) != len(values):
            continue
        diff = sum(abs(a - b) for a, b in zip(values, variant))
        if best_diff is None or diff < best_diff:
            best, best_diff = variant, diff
    return (best or []), (best_diff if best_diff is not None else len(values))


def changed_over_nearest(
    values: list[float], variants: list[list[float]], direction: int, min_mean: float,
) -> dict[str, float] | None:
    """Карта явно сдвинута в нужную сторону относительно ближайшего заводского варианта."""
    nearest, _ = _nearest(values, variants)
    if not nearest:
        return None
    raw = [a - b for a, b in zip(values, nearest) if abs(a - b) > 1e-6]
    if not raw:
        return None
    if direction == 0:  # сдвиг в любую сторону: смотрим на преобладающий знак
        direction = 1 if sum(d > 0 for d in raw) >= len(raw) / 2 else -1
    deltas = [d * direction for d in raw]
    fraction = len(deltas) / len(values)
    same_sign = sum(d > 0 for d in deltas) / len(deltas)
    mean = sum(deltas) / len(deltas)
    if fraction >= RAISED_MIN_FRACTION and same_sign >= RAISED_MIN_SAME_SIGN and mean >= min_mean:
        return {"cells": len(deltas), "mean": round(mean, 2), "fraction": round(fraction, 2)}
    return None


def evaluate(maps: dict[str, list[float]], db: dict[str, Any]) -> dict[str, Any]:
    """Чистая логика вердикта (без чтения файлов): по значениям карт."""
    reasons: list[str] = []
    matched = 0
    unmatched: list[str] = []
    for name, values in maps.items():
        entry = (db.get("maps") or {}).get(name) or {}
        if fingerprint(values) in set(entry.get("hashes") or []):
            matched += 1
            continue
        unmatched.append(name)
        direction, min_mean, label = rule_for(name)
        if changed_over_nearest(values, entry.get("variants") or [], direction, min_mean):
            reason = f"{label}:{name}"
            if reason not in reasons:
                reasons.append(reason)
    if reasons:
        state = STATE_TUNED
    elif not any(key in maps for key in COVERAGE_KEYS):
        # без карты зажигания судить о стоке нельзя: честно «не знаем»
        state = STATE_UNKNOWN
        reasons = ["low_coverage"]
    elif unmatched:
        state = STATE_UNKNOWN
        reasons = [f"not_in_db:{name}" for name in unmatched]
    else:
        state = STATE_STOCK
    return {"state": state, "reasons": reasons, "checked": len(maps), "matched": matched}


def _ms43_inputs(data: bytes) -> dict[str, list[float]] | None:
    from MS43.tools import calibration as cal
    from MS43.tools import policy as ms43_policy
    from MS43.tools import stages as ms43_stages

    try:
        version = cal.read_cal_version(data)
        policy = ms43_policy.load_policy_for_version(version)
    except Exception:  # чужой формат — не наша забота
        return None
    if policy is None or len(data) != 524288:
        return None
    maps: dict[str, list[float]] = {}
    for name, symbol in MS43_MAPS.items():
        entry = policy.objects.get(symbol)
        if entry is None:
            return None
        maps[name] = [float(v) for v in ms43_stages._read_all(data, entry)]
    return maps


def _ms45_inputs(data: bytes) -> dict[str, list[float]] | None:
    from MS45.tools import checksums as ck
    from MS45.tools import policy as ms45_policy

    try:
        _cal, cal_offset = ck.extract_cal(data)
        family = ck.read_family(_cal)
        fam = ms45_policy.load_policy().for_family(family)
    except Exception:  # не MS45 или семейство вне политики
        return None
    maps: dict[str, list[float]] = {}

    def read(name: str, symbol: str) -> None:
        entry = fam.entry(symbol)
        if entry is None:
            return
        count = entry.rows * entry.columns
        size = entry.element_size_bytes
        values = []
        for index in range(count):
            start = cal_offset + entry.address + index * size
            raw = int.from_bytes(data[start:start + size], "big")
            values.append(float(entry.to_engineering(raw)))
        maps[name] = values

    roles = fam.roles
    if roles.get("ignition"):
        read("ron98", roles["ignition"][0])
    for role, name in (("rev_limit_mt", "nmax_mt"), ("rev_limit_at", "nmax_at")):
        if roles.get(role):
            read(name, roles[role][0])
    for number, symbol in enumerate(roles.get("vanos") or []):
        read(f"vanos:{number}", symbol)
    return maps or None


@lru_cache(maxsize=1)
def _ms42_objects():
    from MS42.tools import stages as ms42_stages
    from MS42.tools.xdf import load_definition

    objects, problems = load_definition(ms42_stages.DEFAULT_XDF)
    return None if problems else objects


MS42_MAPS = {
    "ron98": "ip_iga_ron_98_pl_ivvt__n__maf",
    "ron91": "ip_iga_ron_91_pl_ivvt__n__maf",
    "optm": "ip_iga_optm_tco_2__n__maf",
    "fl": "id_pvs_fl__n",
    "nmax_mt": "id_n_max_mt__nvs",
    "nmax_at": "id_n_max_at__nvs",
    "camin": "ip_cam_sp_tco_2_in_pl__n__maf_ivvt",
    "camex": "ip_cam_sp_tco_2_ex_pl__n__maf_ivvt",
    "pop_thr": "ip_n_min_accin_puc__tco",
    "pop_iga": "ip_iga_accin_puc__n__tco",
    "pop_hys": "c_n_hys_max_puc",
}


def _ms42_inputs(data: bytes) -> dict[str, list[float]] | None:
    try:
        from MS42.tools import stages as ms42_stages
        from MS42.tools.analyze_firmware import identify_layout
        from MS42.tools.xdf import get_object

        image_type, offset = identify_layout(len(data))
        objects = _ms42_objects()
        if image_type is None or offset is None or objects is None:
            return None
        maps: dict[str, list[float]] = {}
        for name, symbol in MS42_MAPS.items():
            if symbol not in objects:
                continue
            maps[name] = [round(float(v), 4) for v in
                          ms42_stages._ms42_values(data, offset, get_object(objects, symbol))]
    except Exception:  # чужой формат
        return None
    return maps if "ron98" in maps else None


MS41_MAPS = {
    "igab": "ip_igab__n__maf",
    "nmax": "c_n_max",
    "vmax": "c_vs_max",
}


def _ms41_inputs(data: bytes) -> dict[str, list[float]] | None:
    import os
    import tempfile

    try:
        from MS41.tools import analyze_firmware as af
        from MS41.tools import xdf as x41

        handle, name = tempfile.mkstemp(suffix=".bin")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
            analysis = af.analyze(Path(name))
        finally:
            os.unlink(name)
        if not analysis.supported or analysis.calibration_offset is None:
            return None
        objects = _ms41_objects()
        if objects is None:
            return None
        maps: dict[str, list[float]] = {}
        for key, symbol in MS41_MAPS.items():
            if symbol not in objects:
                continue
            obj = objects[symbol][0]
            window = data[analysis.calibration_offset:]
            maps[key] = [round(float(v), 4) for v in x41.read_engineering(window, obj)]
    except Exception:  # чужой формат
        return None
    return maps if "igab" in maps else None


@lru_cache(maxsize=1)
def _ms41_objects():
    from MS41.tools import xdf as x41

    objects, problems = x41.load_definition()
    return None if problems else objects


ADAPTERS = {"MS43": _ms43_inputs, "MS45": _ms45_inputs, "MS42": _ms42_inputs, "MS41": _ms41_inputs}


def read_inputs(platform: str, data: bytes) -> dict[str, list[float]] | None:
    adapter = ADAPTERS.get(str(platform).upper())
    return adapter(data) if adapter else None


def analyze(platform: str, data: bytes, db: dict[str, Any] | None = None) -> dict[str, Any]:
    """Вердикт для загруженного файла; платформа без адаптера — ``unknown``."""
    platform = str(platform).upper()
    inputs = read_inputs(platform, data)
    if inputs is None:
        return {"state": STATE_UNKNOWN, "reasons": ["no_adapter"], "checked": 0, "matched": 0}
    database = (db if db is not None else load_db()).get(platform) or {}
    if not database:
        return {"state": STATE_UNKNOWN, "reasons": ["no_db"], "checked": 0, "matched": 0}
    return evaluate(inputs, database)
