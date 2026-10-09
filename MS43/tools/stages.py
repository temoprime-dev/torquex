#!/usr/bin/env python3
"""Генератор MS43: Stage 1 / Stage 2 из заявки клиента.

Устройство повторяет MS42 (`MS42/tools/stages.py`): файл собирается как набор
пакетов. Каждый пакет либо пишет значения через ядро (`calibration.py`), либо
честно отвечает `REQUIRES_DATA` со списком нужных замеров. Догадок в файле не
бывает: нет логов/стенда — нет чисел, только список требований.

Ворота продукта (реализованы здесь, а не в ядре):

* выше стокового значения (отсечка, ограничитель скорости) → `acknowledge_risk`;
* выше `OIL_PUMP_ACK_RPM` (6750) → отдельное `acknowledge_oil_pump`;
* хлопки — только при письменном подтверждении «катализатора нет», согласии
  следить за температурой и плане дорожной проверки; полосы только `light`
  и `medium`;
* NLS — отказ: в патчлисте он не реализован;
* Stage 2 без блока `hardware` отклоняется целиком (`HARDWARE_REQUIRED`);
* «+X л.с.» не обещаем: в наряде нет ни одной цифры прироста без стенда.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

try:
    from MS43.tools import calibration as cal
    from MS43.tools import checksum as ck
    from MS43.tools import patches as pt
    from MS43.tools.console import prepare_stdout
    from MS43.tools.policy import (
        ARCHIVE_DIR,
        OIL_PUMP_ACK_RPM,
        Policy,
        covered_software,
        load_policy,
        resolve_policy,
    )
except ModuleNotFoundError:  # Прямой запуск из MS43/tools.
    import calibration as cal  # type: ignore[no-redef]
    import checksum as ck  # type: ignore[no-redef]
    import patches as pt  # type: ignore[no-redef]
    from console import prepare_stdout  # type: ignore[no-redef]
    from policy import (  # type: ignore[no-redef]
        ARCHIVE_DIR,
        OIL_PUMP_ACK_RPM,
        Policy,
        covered_software,
        load_policy,
        resolve_policy,
    )

FIRMWARE_DIR = Path(__file__).resolve().parents[1] / "firmware"
REPO_ROOT_DIR = Path(__file__).resolve().parents[2]
if str(REPO_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_DIR))
# Общий анализатор логов (задание 1 ревью): доказательства пакетов принимает
# только ворота, сверяющие вывод анализатора с исходным CSV.
from tools import pack_log_analyzer  # noqa: E402
# Общие ворота варианта 1 (задание docs/TASK_VARIANT1_RU.md): профиль топлива,
# правовая справка, откат на сток, опции, ждущие данных.
from tools import variant1  # noqa: E402
# Рекомендация анализатора по зонам (docs/PACKS_LOG_REQUIREMENTS_RU.md §7.2):
# добавка угла раскладывается по зонам лога, а не по всей карте.
from tools import pack_recommendation  # noqa: E402
# Единый источник потолков добавки зажигания (задание docs/TASK_STAGE1_RU.md
# §2.1: Stage 1 ≤ +3.0° без лога для RON98, ≤ +1.5° для RON95, Stage 2 ≤ +5.0°
# с логом) и раскладка по запасу до MBT (§2а) — числа не дублируются здесь.
from tools import ignition_caps  # noqa: E402
from tools import ignition_layout  # noqa: E402
from MS4x.tools import stages as common_stages  # noqa: E402

LC_TITLE = "[PATCH] Launch Control & Rolling Anti Lag"
MIL_TITLE = "[PATCH] MIL Knock Indicator"
MCLUSTER_TITLE = "[PATCH] M Cluster LED Support"
DS2_TITLE = "[PATCH] DS2 Logging Feature Enhancement"
VSS_TITLE = "[PATCH] E3x Vehicle Speed Sensor Accuracy Improvement"
IGN_CUT_TITLE = "[PATCH] Ignition Cut Limiter"

#: Полосы хлопков: (об/мин сверх стока, угол зажигания при ПХХ).
#: ``delay_max`` — предел задержки активации ПХХ (timered pops, MS4X wiki
#: «Exhaust Pop Modifications»): light — 1.0 с, medium — 2.5 с. Верх привязан к
#: измерениям стока (максимум по всем версиям 2.45 с на 1-й передаче): выше
#: стока задержку не предлагаем, а выше 2.5 с она уже бессмысленна для хлопков.
POPCORN_BANDS: dict[str, dict[str, float]] = {
    "light": {"rpm_add_max": 600, "angle_min": -6.0, "angle_max": 0.0, "delay_max": 1.0},
    "medium": {"rpm_add_max": 2000, "angle_min": -12.0, "angle_max": 0.0, "delay_max": 2.5},
}
#: Символы задержки ПХХ: один скаляр на месте и по таблице на каждую передачу.
POPCORN_DELAY_SYMBOLS = (
    "c_t_puc_deacc_vs",
    "ip_t_puc_deacc_1__n__maf_mmv",
    "ip_t_puc_deacc_2__n__maf_mmv",
    "ip_t_puc_deacc_3__n__maf_mmv",
    "ip_t_puc_deacc_4__n__maf_mmv",
    "ip_t_puc_deacc_5__n__maf_mmv",
)
#: Имена, которые отклоняются по названию (агрессивного пресета нет).
POPCORN_FORBIDDEN = ("aggressive", "extreme", "flames", "gunshots", "bang", "hard")

#: Намерения, за которыми стоит community patchlist. Это правки **программной**
#: области (`0x00000..0x6FFFF`), а basedata патчлиста v2.9.2 снята с 18 стоков
#: MS430069, поэтому:
#:
#: * на 430037/430055/430056/430066 любой такой интент отклоняется кодом
#:   ``PATCHLIST_FEATURE_UNSUPPORTED`` — байтовые маски на этих версиях не
#:   доказаны (см. ``MS43/docs/VERSION_COVERAGE_RU.md`` §5);
#: * на 430069 работают только реализованные генератором (``PATCHLIST_IMPLEMENTED``);
#:   остальные отклоняются кодом ``PATCHLIST_FEATURE_NOT_IMPLEMENTED``, чтобы
#:   заявка не выглядела выполненной.
PATCHLIST_INTENTS: dict[str, str] = {
    # реализованы генератором (патч + калибровка)
    "launch_control": "launch control",
    "ral": "rolling anti lag",
    "ignition_cut": "отсечка по зажиганию (Ignition Cut Limiter)",
    "mil_knock": "индикация детонации на MIL",
    "m_cluster": "LED-табло M3",
    "ds2_logging": "DS2-логирование",
    "vss_fix": "точность датчика скорости (VSS)",
    # есть в патчлисте, но генератором не собираются — только ручной патч
    "maf_hack": "MAF Sensor Hack (2048/4096 кг/ч)",
    "maf_full_load": "обогащение full load по ДМРВ",
    "map_reduction": "Map Reduction (работа на таблицах tco_1)",
    "alpha_n": "Alpha/N (нагрузка по педали вместо ДМРВ)",
    "fuel_cut_cruise": "переключение таблиц fuel cut по круизу",
    "boost": "Boost Control (наддув)",
    "immo_bypass": "иммобилайзер: bypass (+ DTC Fix)",
    "immo_virginize": "иммобилайзер: очистка ISN (только bootmode)",
}

#: Подмножество ``PATCHLIST_INTENTS``, которое генератор умеет собирать сам.
PATCHLIST_IMPLEMENTED = frozenset(
    {
        "launch_control",
        "ral",
        "ignition_cut",
        "mil_knock",
        "m_cluster",
        "ds2_logging",
        "vss_fix",
    }
)

STAGE_PACKAGES: dict[str, tuple[str, ...]] = {
    "stage1": ("limits", "features", "popcorn", "full_load", "vanos_stage1", "vanos",
               "ignition", "pedal_response", "dtc", "fuel", "maf", "disa_off", "isa_off",
               "idle", "fan_thermostat"),
    "stage2": ("limits", "features", "intake", "maf", "injectors", "vanos", "dtc",
               "exhaust", "torque"),
}

PACKAGE_TITLES = {
    "limits": "Ограничители (скорость и обороты)",
    "features": "Фичи из патчлиста (LC/RAL, отсечка, MIL, кластер, DS2, VSS) — только 430069",
    "popcorn": "Хлопки на сбросе газа",
    "full_load": "Пороги full load",
    "vanos": "VANOS (фазы распредвалов, расширенный логовый профиль v2)",
    "vanos_stage1": "VANOS Stage 1 (осторожный прогретый профиль)",
    "pedal_response": "Ранний режим полной нагрузки (опция)",
    "dtc": "Слой диагностики «без катализатора» (подавление кодов)",
    "ignition": "Зажигание (RON91/RON98)",
    "fuel": "Топливо (обогащение на full load)",
    "maf": "ДМРВ (кривая и диагностика)",
    "intake": "Впуск/DISA под железо",
    "injectors": "Форсунки",
    "exhaust": "Выпуск и задние зонды",
    "torque": "Момент и его передача",
    # Опции варианта 1 (задание docs/TASK_VARIANT1_RU.md): запрашиваются
    # отдельными намерениями и сегодня честно ждут данных (нужны механика
    # клиента и логи), см. tools/variant1.py.
    "disa_off": "DISA off (фиксация заслонок + диагностика)",
    "isa_off": "ISA off / обход клапана холостого хода",
    "idle": "Холостой ход (обороты, прогрев)",
    "fan_thermostat": "Вентилятор и термостат (трек-режим)",
}


@dataclass
class PackageResult:
    id: str
    title: str
    status: str  # written | requires_data | not_requested | rejected
    detail: str = ""
    needs: tuple[str, ...] = ()
    requests: tuple[dict, ...] = ()
    patches: tuple[str, ...] = ()


@dataclass
class StageResult:
    stage: str
    status: str  # BUILT | REQUIRES_DATA | REJECTED
    source_sha256: str
    policy_version: int
    customer: str
    notes: str
    packages: tuple[PackageResult, ...]
    #: Версия калибровки из образа (``4300xx``); None -- не прочитана.
    cal_version: str | None = None
    #: Профиль топлива варианта 1 (``ron91``/``ron95``/``ron98``); None — не заявлен.
    octane_profile: str | None = None
    #: Версии, которые покрывает политика образа (только они и пишутся).
    software: tuple[str, ...] = ()
    #: Пакеты, доступные на версии образа (фичи патчлиста — только 430069).
    available_packages: tuple[str, ...] = ()
    plan: cal.PatchPlan | None = None
    result: cal.PatchResult | None = None
    findings: list[dict] = field(default_factory=list)
    manifest: dict = field(default_factory=dict)

    @property
    def written_packages(self) -> tuple[PackageResult, ...]:
        return tuple(item for item in self.packages if item.status == "written")

    @property
    def pending(self) -> tuple[PackageResult, ...]:
        return tuple(item for item in self.packages if item.status == "requires_data")


def _finding(items: list[dict], code: str, severity: str, message: str) -> None:
    items.append({"code": code, "severity": severity, "message": message})


def _current(image: bytes, entry, index: int = 0) -> float:
    size = max(1, entry.element_size_bits // 8)
    raw = int.from_bytes(
        image[entry.address + index * size : entry.address + (index + 1) * size], "little"
    )
    return entry.scale_a * raw + entry.scale_b


def _fits_steps(entry, value: float) -> float:
    """Прижать значение к сетке объекта (шаг = scale_a).

    Это обязательный шаг генератора: у части таблиц шаг крупный (0.375 °CRK
    у зажигания, 32 об/мин у отсечек), и «сырое» значение из заявки почти
    никогда не ложится точно. Генератор сам прижимает его к сетке, поэтому в
    наряде видно достижимое число, а не ошибку квантования.
    """
    if entry.scale_a == 0:
        return value
    return round((value - entry.scale_b) / entry.scale_a) * entry.scale_a + entry.scale_b


def _snap(policy: Policy, symbol: str, value: float) -> float:
    """Значение, которое реально ляжет в объект, — его и показываем клиенту."""
    return _fits_steps(policy.objects[symbol], float(value))


def _entry(policy: Policy, symbol: str, findings: list[dict], context: str):
    """Объект политики версии; нет объекта — честный отказ, а не падение.

    Политики версий собираются только из символов, доказанных на стоках этой
    версии, поэтому «нет символа» — это не ошибка кода, а отсутствие покрытия
    (например, ``ip_iga_tqr_amt__n__maf`` нет в определении 430037).
    """
    entry = policy.objects.get(symbol)
    if entry is None:
        _finding(
            findings,
            "SYMBOL_NOT_IN_POLICY",
            "reject",
            f"{context}: символа {symbol} нет в политике версии "
            f"{', '.join(policy.software) or '—'} — правка не выполняется",
        )
    return entry


def patchlist_available(policy: Policy) -> bool:
    """Есть ли в политике версии фичи патчлиста (base data проверена только 430069)."""
    return any(entry.origin == "patchlist" for entry in policy.objects.values())


def limits_package(
    image: bytes, intents: list[dict], policy: Policy, acknowledgements: set[str],
    findings: list[dict],
) -> PackageResult:
    requests: list[dict] = []
    needs: list[str] = []
    for intent in intents:
        kind = intent.get("kind")
        if kind == "vmax":
            transmission = intent.get("transmission", "manual")
            symbol = "c_vs_max_mt_1" if transmission == "manual" else "c_vs_max_at_1"
            entry = _entry(policy, symbol, findings, str(intent.get("id")))
            if entry is None:
                continue
            value = float(intent["value_kmh"])
            if value > _current(image, entry):
                if "acknowledge_risk" not in acknowledgements:
                    _finding(findings, "ACKNOWLEDGE_RISK_REQUIRED", "reject",
                             f"{intent.get('id')}: ограничитель скорости {value:g} км/ч выше "
                             f"стока {_current(image, entry):g} — нужно acknowledge_risk")
                    continue
            requests.append({"symbol": symbol, "values": [_snap(policy, symbol, value)]})
            add = float(intent.get("hard_add_kmh", 0.0))
            if add:
                add_entry = _entry(policy, "c_vs_max_add", findings, str(intent.get("id")))
                if add_entry is not None:
                    requests.append(
                        {"symbol": "c_vs_max_add", "values": [_fits_steps(add_entry, add)]}
                    )
        elif kind == "rpm_limit":
            transmission = intent.get("transmission", "manual")
            soft_symbol = "id_n_max_mt__gear" if transmission == "manual" else "id_n_max_at__gear"
            hard_symbol = (
                "id_n_max_max_mt__gear" if transmission == "manual" else "id_n_max_max_at__gear"
            )
            soft_entry = _entry(policy, soft_symbol, findings, str(intent.get("id")))
            hard_entry = _entry(policy, hard_symbol, findings, str(intent.get("id")))
            if soft_entry is None or hard_entry is None:
                continue
            soft = intent.get("soft_rpm")
            hard = intent.get("hard_rpm")
            if soft is None and hard is None:
                _finding(findings, "REQUEST_INVALID", "reject",
                         f"{intent.get('id')}: в rpm_limit нет ни soft_rpm, ни hard_rpm")
                continue
            if soft is not None:
                soft = float(soft)
                above = any(
                    soft > _current(image, soft_entry, index)
                    for index in range(soft_entry.rows * soft_entry.columns)
                )
                if above and "acknowledge_risk" not in acknowledgements:
                    _finding(findings, "ACKNOWLEDGE_RISK_REQUIRED", "reject",
                             f"{intent.get('id')}: отсечка {soft:g} об/мин выше стока — "
                             "нужно acknowledge_risk")
                elif soft > OIL_PUMP_ACK_RPM and "acknowledge_oil_pump" not in acknowledgements:
                    _finding(findings, "ACKNOWLEDGE_OIL_PUMP_REQUIRED", "reject",
                             f"{intent.get('id')}: отсечка {soft:g} об/мин выше порога "
                             f"масляного насоса {OIL_PUMP_ACK_RPM} — нужно acknowledge_oil_pump")
                else:
                    requests.append(
                        {"symbol": soft_symbol, "values": [_snap(policy, soft_symbol, soft)] * 6}
                    )
            if hard is not None:
                hard = float(hard)
                if "acknowledge_risk" not in acknowledgements:
                    _finding(findings, "ACKNOWLEDGE_RISK_REQUIRED", "reject",
                             f"{intent.get('id')}: жёсткая отсечка {hard:g} об/мин — "
                             "нужно acknowledge_risk")
                elif hard > OIL_PUMP_ACK_RPM and "acknowledge_oil_pump" not in acknowledgements:
                    _finding(findings, "ACKNOWLEDGE_OIL_PUMP_REQUIRED", "reject",
                             f"{intent.get('id')}: жёсткая отсечка {hard:g} об/мин выше "
                             f"{OIL_PUMP_ACK_RPM} — нужно acknowledge_oil_pump")
                else:
                    requests.append(
                        {"symbol": hard_symbol, "values": [_snap(policy, hard_symbol, hard)] * 6}
                    )
            if soft and hard and hard < soft:
                _finding(findings, "HARD_BELOW_SOFT", "reject",
                         f"{intent.get('id')}: жёсткая отсечка {hard:g} ниже мягкой {soft:g}")
        elif kind == "hardcut":
            # жёсткая отсечка по зажиганию = патч; параметры — в rpm_limit
            needs.append("hardcut задаётся intent-ом rpm_limit (hard_rpm) + патч отсечки")
        elif kind in ("popcorn", "puc"):
            continue  # отдельный пакет
    if not requests:
        return PackageResult("limits", PACKAGE_TITLES["limits"], "not_requested",
                             "в заявке нет намерений по ограничителям", tuple(needs))
    return PackageResult("limits", PACKAGE_TITLES["limits"], "written",
                         "ограничители из заявки", tuple(needs), tuple(requests))


def popcorn_package(
    image: bytes, intents: list[dict], policy: Policy, profile: dict | None,
    findings: list[dict],
) -> PackageResult:
    intent = next((item for item in intents if item.get("kind") in ("popcorn", "puc")), None)
    if intent is None and not profile:
        return PackageResult("popcorn", PACKAGE_TITLES["popcorn"], "not_requested",
                             "профиль хлопков не передан")
    profile = profile or {}
    if intent:
        # Профиль можно передать прямо в намерении (сборка варианта из одной
        # заявки, задание 3); поле из профиля, переданного отдельно, важнее.
        profile = {**profile}
        if intent.get("band"):
            profile.setdefault("band", intent["band"])
        for field in ("no_catalyst", "thermal_watch", "road_validation_plan"):
            if intent.get(field) is not None:
                profile.setdefault(field, intent[field])
    band = str(profile.get("band", "")).lower()
    if band in POPCORN_FORBIDDEN:
        _finding(findings, "POPCORN_AGGRESSIVE_REFUSED", "reject",
                 f"полоса {band!r}: агрессивного пресета нет, только light/medium")
        return PackageResult("popcorn", PACKAGE_TITLES["popcorn"], "rejected",
                             f"полоса {band!r} отклонена")
    if band not in POPCORN_BANDS:
        _finding(findings, "POPCORN_BAND_UNKNOWN", "reject",
                 f"полоса {band!r} неизвестна; допустимо {', '.join(POPCORN_BANDS)}")
        return PackageResult("popcorn", PACKAGE_TITLES["popcorn"], "rejected",
                             "полоса не указана или неизвестна")
    rules = POPCORN_BANDS[band]
    declaration = str(profile.get("no_catalyst", "")).strip()
    if len(declaration) < 10:
        _finding(findings, "POPCORN_NO_CATALYST_REQUIRED", "reject",
                 "нужно письменное подтверждение «катализатора нет» (не короче 10 символов)")
        return PackageResult("popcorn", PACKAGE_TITLES["popcorn"], "rejected",
                             "нет подтверждения об отсутствии катализатора")
    if not profile.get("thermal_watch"):
        _finding(findings, "POPCORN_THERMAL_WATCH_REQUIRED", "reject",
                 "нужно согласие следить за температурой выпуска и ОЖ")
        return PackageResult("popcorn", PACKAGE_TITLES["popcorn"], "rejected",
                             "нет согласия по температуре")
    plan_text = str(profile.get("road_validation_plan", "")).strip()
    if len(plan_text) < 10:
        _finding(findings, "POPCORN_ROAD_PLAN_REQUIRED", "reject",
                 "нужен план дорожной проверки (первые заезды, кто следит за температурой)")
        return PackageResult("popcorn", PACKAGE_TITLES["popcorn"], "rejected",
                             "нет плана дорожной проверки")
    rpm_add = float(profile.get("rpm_add", 400))
    if not 0 <= rpm_add <= rules["rpm_add_max"]:
        _finding(findings, "POPCORN_BAND_VIOLATION", "reject",
                 f"добавка {rpm_add:g} об/мин вне полосы {band} "
                 f"(0..{rules['rpm_add_max']:g})")
        return PackageResult("popcorn", PACKAGE_TITLES["popcorn"], "rejected",
                             "значения вне полосы")
    angle = float(profile.get("angle", rules["angle_max"]))
    if not rules["angle_min"] <= angle <= rules["angle_max"]:
        _finding(findings, "POPCORN_BAND_VIOLATION", "reject",
                 f"угол {angle:g}° вне полосы {band} "
                 f"({rules['angle_min']:g}..{rules['angle_max']:g})")
        return PackageResult("popcorn", PACKAGE_TITLES["popcorn"], "rejected",
                             "значения вне полосы")
    requests: list[dict] = []
    hys = _entry(policy, "c_n_hys_max_puc", findings, "popcorn")
    iga = _entry(policy, "ip_iga_puc__n__tco", findings, "popcorn")
    if hys is None or iga is None:
        return PackageResult("popcorn", PACKAGE_TITLES["popcorn"], "rejected",
                             "в политике версии нет объектов ПХХ")
    base = max(_current(image, hys), 0.0)
    target = _fits_steps(hys, base + rpm_add)
    if target > hys.maximum:
        _finding(findings, "POPCORN_BAND_VIOLATION", "reject",
                 f"гистерезис ПХХ {target:g} об/мин выше предела {hys.maximum:g}")
        return PackageResult("popcorn", PACKAGE_TITLES["popcorn"], "rejected",
                             "гистерезис выше аппаратного предела")
    # «Только с кондиционером» (MS4X wiki, «Exhaust Pop Modifications», toggleable
    # pops): порог отсечки при A/C on уходит выше достижимых оборотов
    # на любой температуре, карты A/C off, гистерезис и угол A/C off остаются заводскими.
    ac_only = bool((intent or {}).get("ac_only") or profile.get("ac_only"))
    if not ac_only:
        requests.append({"symbol": "c_n_hys_max_puc", "values": [target]})
    angle = _snap(policy, "ip_iga_puc__n__tco", angle)
    if not ac_only:
        requests.append(
            {"symbol": "ip_iga_puc__n__tco", "values": [angle] * (iga.rows * iga.columns)}
        )
    # Парная карта «A/C on»: хлопок должен быть одинаковым при любом состоянии
    # кондиционера (MS4X wiki, «Exhaust Pop Modifications»: семейство accin).
    iga_accin = _entry(policy, "ip_iga_accin_puc__n__tco", findings, "popcorn")
    if iga_accin is None:
        return PackageResult("popcorn", PACKAGE_TITLES["popcorn"], "rejected",
                             "в политике версии нет карты ПХХ для режима A/C on")
    requests.append(
        {
            "symbol": "ip_iga_accin_puc__n__tco",
            "values": [angle] * (iga_accin.rows * iga_accin.columns),
        }
    )
    if ac_only:
        thr = _entry(policy, "ip_n_min_accin_puc__tco", findings, "popcorn")
        if thr is None:
            return PackageResult("popcorn", PACKAGE_TITLES["popcorn"], "rejected",
                                 "в политике версии нет порога отсечки для A/C on")
        stock_thr = _read_all(image, thr)
        warm_rows = len(stock_thr)  # все строки оси ldpm_tco_4 (−30…90 °C)
        top = float(thr.maximum if thr.maximum is not None else max(stock_thr))
        values = list(stock_thr)
        for index in range(len(values) - warm_rows, len(values)):
            values[index] = _fits_steps(thr, top)
        requests.append({"symbol": "ip_n_min_accin_puc__tco", "values": values})
    before = {} if ac_only else profile.get("minimum_rpm_guard", {})
    if isinstance(before, dict) and before.get("requested"):
        puc_rpm = _entry(policy, "ip_n_min_puc__tco", findings, "popcorn")
        values = list(before["requested"])
        if puc_rpm is None:
            pass
        elif len(values) != puc_rpm.rows * puc_rpm.columns:
            _finding(findings, "REQUEST_INVALID", "reject",
                     "minimum_rpm_guard.requested должен содержать 6 значений")
        else:
            if not ac_only:
                requests.append({"symbol": "ip_n_min_puc__tco", "values": values})
            # тот же порог для режима A/C on: парный объект
            if _entry(policy, "ip_n_min_accin_puc__tco", findings, "popcorn") is not None:
                requests.append({"symbol": "ip_n_min_accin_puc__tco", "values": values})
    # Таймер хлопков (timered pops): задержка активации ПХХ на месте и по передачам.
    delay = profile.get("delay_seconds")
    delay_text = "по стоку"
    if delay is not None:
        delay = float(delay)
        if not 0 <= delay <= rules["delay_max"]:
            _finding(findings, "POPCORN_BAND_VIOLATION", "reject",
                     f"задержка ПХХ {delay:g} с вне полосы {band} "
                     f"(0..{rules['delay_max']:g} с)")
            return PackageResult("popcorn", PACKAGE_TITLES["popcorn"], "rejected",
                                 "задержка вне полосы")
        for symbol in POPCORN_DELAY_SYMBOLS:
            entry = _entry(policy, symbol, findings, "popcorn (задержка)")
            if entry is None:
                return PackageResult("popcorn", PACKAGE_TITLES["popcorn"], "rejected",
                                     "в политике версии нет таблиц задержки ПХХ")
            cells = entry.rows * entry.columns
            requests.append({"symbol": symbol, "values": [_fits_steps(entry, delay)] * cells})
        delay_text = f"{delay:g} с (на месте и 1–5 передачи)"
    note = (
        f"полоса {band}: +{rpm_add:g} об/мин, угол ПХХ {angle:g}° (сетка объекта), "
        + ("только при включённом кондиционере: порог отсечки A/C on выше оборотов "
           "на любой температуре, A/C off по стоку" if ac_only
           else "A/C on — та же карта")
        + f", задержка ПХХ {delay_text}, "
        f"подтверждение: {declaration[:60]}"
    )
    return PackageResult("popcorn", PACKAGE_TITLES["popcorn"], "written", note,
                         ("thermal_watch: true", f"road_validation_plan: {plan_text[:60]}"),
                         tuple(requests))


def features_package(
    image: bytes, intents: list[dict], policy: Policy, patches_offered: list[str],
    findings: list[dict], declarations: set[str],
) -> PackageResult:
    requests: list[dict] = []
    patches: list[str] = []
    details: list[str] = []
    patchlist_ok = patchlist_available(policy)
    version_text = ", ".join(policy.software) or "—"

    def patchlist_guard(kind: str, feature: str | None = None) -> bool:
        """Фичи патчлиста — только там, где его basedata проверена (430069)."""
        if patchlist_ok:
            return True
        label = f"{kind} ({feature})" if feature else kind
        _finding(
            findings,
            "PATCHLIST_FEATURE_UNSUPPORTED",
            "reject",
            f"{label}: фичи патчлиста сообщества проверены только на 430069; на версии "
            f"{version_text} их байтовые маски не доказаны — не предлагаем "
            "(см. MS43/docs/VERSION_COVERAGE_RU.md §5)",
        )
        return False

    for intent in intents:
        kind = intent.get("kind")
        # Фичи патчлиста, которых нет в этом генераторе: на 430069 честно
        # отказываем (руками — патчами), на остальных версиях отказываем как
        # «патчлист только для 430069». Молча пропустить чужой запрос нельзя.
        if kind in PATCHLIST_INTENTS and kind not in PATCHLIST_IMPLEMENTED:
            feature = PATCHLIST_INTENTS[kind]
            if not patchlist_guard(kind, feature):
                continue
            _finding(
                findings,
                "PATCHLIST_FEATURE_NOT_IMPLEMENTED",
                "reject",
                f"{kind} ({feature}): есть в патчлисте, но генератор эту фичу не "
                "собирает (только патч вручную, см. MS43/docs/PATCHLIST_RU.md §6) — "
                "в этом файле её не будет",
            )
            continue
        if kind == "launch_control":
            if not patchlist_guard("launch_control", PATCHLIST_INTENTS["launch_control"]):
                continue
            patches.append(LC_TITLE)
            conf = int(intent.get("mode", 1))
            if conf not in (1, 2):
                _finding(findings, "REQUEST_INVALID", "reject",
                         "launch_control.mode: 1 — по сцеплению, 2 — по сцеплению и круизу")
                continue
            cell = policy.objects["c_conf_lc"]
            requests.append({"symbol": "c_conf_lc", "values": [conf]})
            if intent.get("rpm") is not None:
                requests.append(
                    {"symbol": "c_n_max_lc",
                     "values": [_snap(policy, "c_n_max_lc", float(intent["rpm"]))]}
                )
            if intent.get("vmax_kmh") is not None:
                requests.append(
                    {"symbol": "c_vs_max_lc",
                     "values": [_snap(policy, "c_vs_max_lc", float(intent["vmax_kmh"]))]}
                )
            details.append(f"launch control: режим {conf}")
        elif kind == "ral":
            if not patchlist_guard("ral", PATCHLIST_INTENTS["ral"]):
                continue
            patches.append(LC_TITLE)
            requests.append({"symbol": "c_conf_ral", "values": [1]})
            if intent.get("pvs_min") is not None:
                requests.append(
                    {"symbol": "c_pvs_min_ral",
                     "values": [_snap(policy, "c_pvs_min_ral", float(intent["pvs_min"]))]}
                )
            details.append("rolling anti lag включён")
        elif kind == "nls":
            _finding(findings, "NLS_NOT_IMPLEMENTED", "reject",
                     "NLS (no-lift shift) в патчлисте не реализован — обещать нельзя")
            continue
        elif kind == "ignition_cut":
            if not patchlist_guard("ignition_cut", PATCHLIST_INTENTS["ignition_cut"]):
                continue
            patches.append(IGN_CUT_TITLE)
            details.append("жёсткая отсечка по зажиганию (ресурс катушек — под ответственность клиента)")
        elif kind == "mil_knock":
            if not patchlist_guard("mil_knock", PATCHLIST_INTENTS["mil_knock"]):
                continue
            # флаги индикации — один байт 0x707D0 на два уровня: их выставляет
            # [DATA] MIL Knock Indicator (0x03), калибровка пишет только время
            patches.extend([MIL_TITLE, "[DATA] MIL Knock Indicator"])
            if intent.get("seconds") is not None:
                requests.append(
                    {"symbol": "c_t_max_knk_mil",
                     "values": [_snap(policy, "c_t_max_knk_mil", float(intent["seconds"]))]}
                )
            details.append("индикатор детонации на MIL")
        elif kind == "m_cluster":
            if not patchlist_guard("m_cluster", PATCHLIST_INTENTS["m_cluster"]):
                continue
            patches.extend([MCLUSTER_TITLE, "[DATA] M3 Cluster Default Values"])
            details.append("LED-табло M3 (нужен кластер M3)")
        elif kind == "ds2_logging":
            if not patchlist_guard("ds2_logging", PATCHLIST_INTENTS["ds2_logging"]):
                continue
            patches.extend([DS2_TITLE, "[PATCH] DS2 Speed Increase"])
            details.append("DS2-логирование")
        elif kind == "vss_fix":
            if not patchlist_guard("vss_fix", PATCHLIST_INTENTS["vss_fix"]):
                continue
            patches.append(VSS_TITLE)
            details.append("точность датчика скорости (свапы E30/E34)")
        elif kind in ("torque", "full_load", "vanos", "vanos_stage1", "ignition",
                      "ignition_stage1", "fuel", "maf", "pedal_response", "intake",
                      "injectors", "exhaust", "dtc"):
            continue  # отдельные пакеты
        elif kind in ("vmax", "rpm_limit", "hardcut", "popcorn", "puc"):
            continue  # обрабатываются пакетом limits/popcorn
        elif kind == "octane" or kind in variant1.OPTION_TITLES:
            continue  # профиль топлива и опции варианта 1 — ворота/свой пакет
        else:
            _finding(findings, "INTENT_UNKNOWN", "warning",
                     f"намерение {kind!r} неизвестно и не обработано")
    if not requests and not patches:
        refused = any(
            item["code"] in ("PATCHLIST_FEATURE_UNSUPPORTED", "PATCHLIST_FEATURE_NOT_IMPLEMENTED",
                             "NLS_NOT_IMPLEMENTED")
            for item in findings
        )
        if refused:
            return PackageResult("features", PACKAGE_TITLES["features"], "rejected",
                                 "фичи патчлиста запрошены, но не выполняются на этой версии")
        return PackageResult("features", PACKAGE_TITLES["features"], "not_requested",
                             "фичи не запрошены")
    return PackageResult("features", PACKAGE_TITLES["features"], "written",
                         "; ".join(details) or "фичи из патчлиста", (), tuple(requests),
                         tuple(dict.fromkeys(patches)))


def requires_data_package(package: str, intents: list[dict]) -> PackageResult:
    """Пакеты, которые без логов/документов не пишутся — с конкретным списком."""
    needs = {
        "full_load": (
            "лог полной нагрузки (педаль, ДМРВ, обороты, зажигание, детонация)",
            "широкополосная лямбда под нагрузкой",
        ),
        "vanos": (
            "целевые и фактические фазы валов (лог VANOS)",
            "замер момента/наполнения на двух крайних положениях валов",
        ),
        "ignition": (
            "лог детонации по цилиндрам на выбранном топливе",
            "паспорт топлива (RON) и температура воздуха",
        ),
        "fuel": (
            "широкополосная лямбда и топливные коррекции",
            "скважность форсунок (ресурс)",
        ),
        "maf": (
            "кривая ДМРВ с даташитом или стендом (256 точек, кг/ч)",
            "лог расхода и нагрузок до/после правки",
        ),
        "isa_off": (
            "карты холостого хода и регулятор (ip_n_sp_is__tco, ip_isapwm__tco) "
            "правятся только вместе с проверкой устойчивости ХХ — нужен лог "
            "холостого хода до/после (пресет isa_off), а не только подтверждение "
            "снятия клапана",
            "подтверждение механики: клапан снят/заглушен и канал ХХ заглушен, "
            "ХХ держится дросселем (подтверждение клиента), риск неустойчивого ХХ "
            "принят",
            "подавление кода ошибки клапана — отдельный слой диагностики "
            "(намерение dtc, группа isa): он не заменяет правку карт ХХ, а только "
            "убирает код, поэтому выпускается отдельно и с правовой справкой",
        ),
        "intake": (
            "описание впуска (коллектор, ДЗ, фильтр) в блоке hardware",
            "лог DISA/VANOS и наполнения на стенде",
        ),
        "injectors": (
            "паспорт форсунок (производительность, dead time, давление)",
            "лог лямбды и скважности",
        ),
        "exhaust": (
            "состав выпуска (коллектор, катализатор/его отсутствие, зонды)",
            "решение по задним зондам и off-road декларация",
        ),
        "torque": (
            "лог вмешательства по моменту (срез) и данные коробки",
            "замер момента на стенде до/после настройки карт",
        ),
    }.get(package) or variant1.option_needs(package) or ("данные",)
    asked = [item for item in intents if item.get("kind") == package]
    if not asked:
        return PackageResult(
            package, PACKAGE_TITLES[package], "not_requested",
            "в заявке нет намерений по этому пакету",
        )
    return PackageResult(
        package,
        PACKAGE_TITLES[package],
        "requires_data",
        "запрошено, но без данных не пишем: " + ", ".join(str(item.get("id")) for item in asked),
        tuple(needs),
    )


#: Что нужно от клиента для «DISA off» MS43: код положения заслонки продукт не
#: угадывает (в стоке коды 0/1, но какой из них какому положению механики
#: соответствует — зависит от коллектора), механику клиент подтверждает сам.
DISA_OFF_NEEDS = (
    "подтверждение механики: что сделано с заслонкой (снята/зафиксирована) и в каком положении она остаётся",
    "код положения из стока MS43 (hold: 0 или 1) — соответствие кода и положения подтверждает клиент",
    "лог Test0 до и после (холостой ход + полная нагрузка): смесь, момент, новые коды привода DISA",
)


def disa_off_package(
    image: bytes, intents: list[dict], policy: Policy, findings: list[dict],
) -> PackageResult:
    """DISA off: заслонка удерживается в одном положении (MS43).

    Отключать функцию в калибровке нечем: карты ``id_vim_*`` задают **положение**
    заслонки, а не её наличие. Поэтому «DISA off» здесь — это удержание заслонки
    в выбранном клиентом положении по всей рабочей зоне (механику клиент
    подтверждает сам: заслонка снята и зафиксирована или привод обездвижен).
    Код ошибки привода не подавляется этим пакетом: это отдельный слой
    («dtc», группы vim/isa) со своей правовой справкой.

    Кодирование положения — то же, что в стоке (0/1; в стоке MS43 таблица
    полной нагрузки переключается 1 → 0 на высоких оборотах). Какой код
    соответствует «открыто/закрыто» механически, зависит от конкретного
    коллектора, поэтому продукт требует, чтобы клиент указал код и подтвердил
    механику, а не назначает его сам.
    """
    package = "disa_off"
    title = PACKAGE_TITLES[package]
    mine = [i for i in intents if i.get("kind") == package]
    if not mine:
        return PackageResult(package, title, "not_requested",
                             "в заявке нет намерения DISA off")
    intent = mine[0]
    hold = intent.get("hold")
    if hold is None:
        _finding(
            findings, "DISA_OFF_REQUIRES_DATA", "info",
            "DISA off: положение заслонки задаётся кодом из стока (hold: 0 или 1) — "
            "в стоке MS43 таблица полной нагрузки переключается 1 → 0 на высоких "
            "оборотах; какой код соответствует нужному положению механики, "
            "подтверждает клиент, сам продукт его не назначает",
        )
        return PackageResult(package, title, "requires_data",
                             "нет кода положения заслонки", DISA_OFF_NEEDS)
    try:
        hold_value = float(hold)
    except (TypeError, ValueError):
        _finding(findings, "REQUEST_INVALID", "reject",
                 "DISA off: hold должен быть числом 0 или 1")
        return PackageResult(package, title, "rejected", "положение не число")
    if hold_value not in (0.0, 1.0):
        _finding(findings, "REQUEST_INVALID", "reject",
                 "DISA off: hold принимает только 0 или 1 (кодирование стока)")
        return PackageResult(package, title, "rejected", "положение вне 0/1")
    if not intent.get("acknowledge_mechanics"):
        _finding(
            findings, "DISA_OFF_REQUIRES_DATA", "info",
            "DISA off: нужно подтверждение механики (acknowledge_mechanics: true) — "
            "что именно сделано с заслонкой и в каком положении она зафиксирована",
        )
        return PackageResult(package, title, "requires_data",
                             "нет подтверждения механики", DISA_OFF_NEEDS)
    requests: list[dict] = []
    details: list[str] = []
    for symbol in ("id_vim_fl__n_vim", "id_vim_pl__n_vim__maf"):
        entry = policy.objects.get(symbol)
        if entry is None or entry.read_only:
            _finding(findings, "SYMBOL_NOT_ALLOWED", "reject",
                     f"DISA off: {symbol} не разрешён политикой этой версии")
            return PackageResult(package, title, "rejected", f"{symbol} вне политики")
        stock = _read_all(image, entry)
        changed = sum(1 for value in stock if value != hold_value)
        if changed:
            requests.append({"symbol": symbol, "values": [hold_value] * len(stock)})
        details.append(
            f"{symbol}: {changed} из {len(stock)} точек переведены в "
            f"{hold_value:g} (в стоке {sorted(set(stock))})"
        )
    if not requests:
        return PackageResult(package, title, "written",
                             "изменений нет: заслонка уже удерживается в этом "
                             "положении; " + "; ".join(details))
    _finding(
        findings,
        "DISA_OFF_NOTICE",
        "info",
        "DISA off: положение заслонки зафиксировано по всей рабочей зоне; "
        "сообщества описывают потерю момента внизу (порядка 10 Н·м) и возможные "
        "ошибки смеси при неаккуратном удалении — код ошибки привода этим пакетом "
        "не подавляется, для этого отдельный слой диагностики «dtc» (группа vim)",
    )
    return PackageResult(package, title, "written", "; ".join(details),
                         requests=tuple(requests))


# ---------------------------------------------------------------------------
# Пакеты «зажигание / топливо / VANOS» (задание docs/TASK_PACKS_RU.md)
#
# Правила из веб-анализа (docs/PACKS_WEB_ANALYSIS_RU.md) и требования к логам
# (docs/PACKS_LOG_REQUIREMENTS_RU.md). Пороги читает из машиночитаемой
# спецификации `docs/pack_log_requirements.json`, чтобы код не расходился с
# документом. Чужие значения не копируются: все добавки относительны стока
# патчимого образа.
# ---------------------------------------------------------------------------

PACK_REQUIREMENTS_PATH = Path(__file__).resolve().parents[2] / "docs" / "pack_log_requirements.json"
_PACK_REQUIREMENTS: dict | None = None


def load_pack_requirements() -> dict:
    """Машиночитаемые требования к логам (единый источник порогов)."""
    global _PACK_REQUIREMENTS
    if _PACK_REQUIREMENTS is None:
        _PACK_REQUIREMENTS = json.loads(PACK_REQUIREMENTS_PATH.read_text(encoding="utf-8"))
    return _PACK_REQUIREMENTS


def _load_evidence(intent: dict, findings: list[dict]) -> dict | None:
    """Доказательство из заявки: словарь или путь к JSON-файлу.

    Ошибка чтения — честный отказ (код ``EVIDENCE_UNREADABLE``), а не падение:
    файл мог не доехать вместе с заявкой.
    """
    raw = intent.get("evidence")
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(Path(str(raw)).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _finding(
            findings,
            "EVIDENCE_UNREADABLE",
            "reject",
            f"{intent.get('id')}: файл доказательства {raw!r} не читается: {exc}",
        )
        return None


def evidence_gate(evidence: dict | None, package: str) -> tuple[bool, tuple[str, ...]]:
    """Задание 1 ревью: доказательство принимается, только если оно выдано
    анализатором ``tools/pack_log_analyzer.py``.

    Ручной ``pack_log``-JSON отклоняется отдельным кодом
    ``EVIDENCE_MANUAL_JSON``; результат анализатора сверяется с исходным
    логом (поиск файла, sha256, пересчёт метрик). Пороговые проверки —
    отдельно в ``validate_pack_evidence``.
    """
    return pack_log_analyzer.verify_evidence(
        evidence, package, search_dirs=(str(REPO_ROOT_DIR),)
    )


def validate_pack_evidence(
    evidence: dict | None, package: str, spec: dict | None = None
) -> tuple[bool, tuple[str, ...]]:
    """Проверка лога-доказательства по порогам из спецификации.

    Возвращает ``(прошёл, список проблем)``. Доказательство — это структурные
    измерения из клиентского лога (не булевы «всё хорошо»): каждое число
    сверяется с порогом, поэтому подделать прохождение можно только подделкой
    измерений, за которые отвечает клиент (см. наряд).
    """
    spec = spec or load_pack_requirements()
    problems: list[str] = []
    if evidence is None:
        return False, ("доказательство не приложено",)
    if evidence.get("kind") != "pack_log":
        return False, (f"kind должен быть 'pack_log', получено {evidence.get('kind')!r}",)
    if int(evidence.get("schema_version") or 0) != 1:
        problems.append(f"schema_version должна быть 1, получено {evidence.get('schema_version')!r}")
    if evidence.get("package") != package:
        problems.append(
            f"доказательство для пакета {evidence.get('package')!r}, нужен {package!r}"
        )
    if not str(evidence.get("source") or "").strip():
        problems.append("не указан источник лога (поле source)")
    results = evidence.get("results") or {}
    conditions = spec.get("conditions", {})
    if package == "ignition":
        no_knock = spec["packages"]["ignition"]["no_knock"]
        ron = evidence.get("fuel_ron")
        if ron is None or float(ron) < float(conditions["min_ron_for_ignition_evidence"]):
            problems.append(
                f"топливо должно быть не ниже RON {conditions['min_ron_for_ignition_evidence']:,}"
                .replace(",", "")
            )
        fraction = results.get("no_knock_fraction")
        if fraction is None or float(fraction) < float(no_knock["min_fraction_above_load"]):
            problems.append(
                f"доля выбросов без детонации (коррекция ≤ {no_knock['max_retard_deg']}°) "
                f"должна быть ≥ {no_knock['min_fraction_above_load']}"
            )
        sustained = results.get("max_sustained_retard_seconds")
        if sustained is None or float(sustained) >= float(no_knock["max_sustained_seconds"]):
            problems.append(
                f"длительность удержания коррекции ≥ {no_knock['max_sustained_retard_deg']}° "
                f"должна быть < {no_knock['max_sustained_seconds']} с"
            )
        spikes = results.get("knock_spike_events")
        if spikes is None or int(spikes) != 0:
            problems.append(
                f"одиночных пиков напряжения детонации выше {no_knock['knock_spike_ratio_vs_median']}× "
                "медианы быть не должно"
            )
        iat = results.get("max_iat_c")
        if iat is None or float(iat) > float(conditions["intake_temp_max_c"]):
            problems.append(
                f"температура воздуха на впуске в заезде ≤ {conditions['intake_temp_max_c']} °C"
            )
    elif package == "fuel":
        crit = spec["packages"]["fuel"]["criteria"]
        stdev = results.get("lambda_stdev_wot")
        if stdev is None or float(stdev) > float(crit["lambda_stdev_max_wot"]):
            problems.append(
                f"стандартное отклонение лямбды на полном ходу ≤ {crit['lambda_stdev_max_wot']}"
            )
        trims = results.get("partial_load_trim_max_percent")
        if trims is None or float(trims) > float(crit["partial_load_trim_max_percent"]):
            problems.append(
                "тримы частичной нагрузки в установившемся фрагменте "
                f"≤ ±{crit['partial_load_trim_max_percent']} %"
            )
        floor = results.get("lambda_floor")
        if floor is None or float(floor) < float(crit["lambda_target_min"]):
            problems.append(
                f"измеренный минимум лямбды ≥ {crit['lambda_target_min']} (ниже — переобогащение)"
            )
    elif package == "vanos":
        crit = spec["packages"]["vanos"]["criteria"]
        track = results.get("max_track_error_deg")
        if track is None or float(track) > float(crit["max_track_error_deg"]):
            problems.append(
                f"максимальное рассогласование факт/цель по фазам ≤ {crit['max_track_error_deg']}° "
                "(иначе механика не держит цели и править их бессмысленно)"
            )
        hold = spec["packages"]["vanos"]["hold_points"]
        hold_seconds = results.get("min_hold_seconds")
        if hold_seconds is None or float(hold_seconds) < float(hold["min_hold_seconds"]):
            problems.append(
                f"удержание каждой точки установившегося режима ≥ {hold['min_hold_seconds']} с"
            )
    else:
        problems.append(f"неизвестный пакет {package!r}")
    return not problems, tuple(problems)


def _read_all(image: bytes, entry) -> list[float]:
    return [
        _current(image, entry, index) for index in range(entry.rows * entry.columns)
    ]


def _ignition_axes(image: bytes, policy: Policy) -> tuple[list[float], list[float]] | None:
    """Оси карты RON98: (обороты 20, нагрузка 16) из политики версии.

    ``None`` — осей в политике нет или они короче двух точек: раскладку по
    зонам тогда применить нечем (плоская добавка).
    """
    try:
        n_axis = policy.objects["ldpm_n_14"]
        maf_axis = policy.objects["ldpm_maf_iga_2"]
    except KeyError:
        return None
    rpm = _read_all(image, n_axis)
    load = _read_all(image, maf_axis)
    if len(rpm) < 2 or len(load) < 2:
        return None
    return rpm, load


def _ignition_zone_caps(
    evidence: dict | None, policy: Policy, image: bytes, entry, limit: float,
    fallback_deg: float, findings: list[dict],
) -> tuple[list[float] | None, str | None]:
    """Пределы добавки по ячейкам карты RON98 из рекомендации анализатора (по зонам).

    Заявка задаёт потолок (``limit``), рекомендация — форму по зонам лога: зона
    с детонацией/перегревом/бедной смесью получает 0, зона без данных —
    консервативный потолок ``fallback_deg`` (права Stage 1 без логов).
    Возвращает ``(список пределов по ячейкам | None, строка в наряд)``;
    ``None`` — потолки плоские (вся карта по ``limit``), причина уходит
    находкой ``ZONES_UNAVAILABLE``. Запас до MBT, форма по нагрузке и
    сглаживание применяются поверх этих пределов (``tools/ignition_layout``).
    """
    recommendation, why = pack_recommendation.trusted_recommendation(
        evidence, "ignition", pack_log_analyzer.ANALYZER_VERSION
    )
    if recommendation is None:
        _finding(
            findings,
            "ZONES_UNAVAILABLE",
            "info",
            f"ignition: раскладка по зонам не применена — {why}; "
            f"потолок плоский ≤ {limit:g}°",
        )
        return None, None
    axes = _ignition_axes(image, policy)
    if axes is None:
        _finding(
            findings,
            "ZONES_UNAVAILABLE",
            "info",
            "ignition: осей карты RON98 нет в политике версии — раскладка "
            "по зонам не применена, потолок плоский",
        )
        return None, None
    rpm_axis, load_axis = axes
    if len(rpm_axis) != entry.rows or len(load_axis) != entry.columns:
        _finding(
            findings,
            "ZONES_UNAVAILABLE",
            "info",
            f"ignition: оси ({len(rpm_axis)}×{len(load_axis)}) не совпали с картой "
            f"({entry.rows}×{entry.columns}) — раскладка по зонам не применена",
        )
        return None, None
    floor_delta = min(float(fallback_deg), limit)
    per_cell = [
        min(value, limit)
        for value in pack_recommendation.deltas_for_map(
            recommendation, rpm_axis, load_axis, fallback_deg=floor_delta
        )
    ]
    no_data = 0
    for rpm in rpm_axis:
        for load in load_axis:
            if pack_recommendation.zone_delta(recommendation, float(rpm),
                                              float(load), None) is None:
                no_data += 1
    full = sum(1 for value in per_cell if value >= limit - 1e-9)
    partial = sum(1 for value in per_cell if 0 < value < limit - 1e-9)
    blocked = len(per_cell) - full - partial
    digest = str(recommendation.get("source_sha256") or "")
    note = (
        f"пределы по зонам лога (confidence {recommendation.get('confidence')}, "
        f"sha256 {digest[:12]}…): {full} ячеек с полным потолком {limit:g}°, "
        f"{partial} с меньшим пределом (в т. ч. {no_data} в зонах без данных — "
        f"консервативно {floor_delta:g}°), {blocked} без добавки "
        "(детонация/перегрев/бедная смесь в зоне)"
    )
    return per_cell, note


def _policy_axis(image: bytes, policy: Policy, symbol: str) -> list[float] | None:
    """Значения оси из политики версии (только чтение) или ``None``.

    Оси карт топлива и фаз лежат в политике отдельными символами (группа
    ``axes``); их адреса сняты с определения версии и сверены на стоках.
    """
    entry = policy.objects.get(symbol)
    if entry is None or entry.rows * entry.columns < 2:
        return None
    return _read_all(image, entry)


def _zones_unavailable(findings: list[dict], package: str, why: str, limit_text: str) -> None:
    _finding(
        findings,
        "ZONES_UNAVAILABLE",
        "info",
        f"{package}: раскладка по зонам не применена — {why}; {limit_text}",
    )


def _fuel_zone_limits(
    evidence: dict | None, policy: Policy, image: bytes, entry, requested: float,
    floor: float, findings: list[dict],
) -> tuple[list[float] | None, str | None]:
    """Допустимое обогащение λ по ячейкам ``ip_ti_fl__n`` (карта по оборотам).

    Обогащение полной нагрузки выпускается только по логу широкой лямбды и
    только там, где измеренная λ в зоне оставляет запас до пола λ 0.82: запас
    зоны — это и есть разрешённая правка. Обороты без данных не обогащаются.
    """
    recommendation, why = pack_recommendation.trusted_recommendation(
        evidence, "fuel", pack_log_analyzer.ANALYZER_VERSION
    )
    if recommendation is None:
        _zones_unavailable(findings, "fuel", why,
                           f"обогащение плоское ≤ −{requested:g} λ")
        return None, None
    axis = _policy_axis(image, policy, "ldpm_n_11")
    if axis is None:
        _zones_unavailable(findings, "fuel", "оси оборотов карты топлива нет в политике",
                           f"обогащение плоское ≤ −{requested:g} λ")
        return None, None
    if len(axis) != entry.rows * entry.columns:
        _zones_unavailable(
            findings, "fuel",
            f"ось ({len(axis)} точек) не совпала с картой ({entry.rows}×{entry.columns})",
            f"обогащение плоское ≤ −{requested:g} λ")
        return None, None
    limits = pack_recommendation.full_load_delta_by_rpm(
        recommendation, axis, floor, requested
    )
    untouched = sum(1 for value in limits if value <= 0)
    digest = str(recommendation.get("source_sha256") or "")
    note = (
        f"раскладка обогащения по оборотам (confidence {recommendation.get('confidence')}, "
        f"sha256 {digest[:12]}…): запас до пола λ {floor:g} по зонам лога; "
        f"{untouched} ячеек без данных не тронуты, максимум −{max(limits):g} λ"
    )
    return limits, note


def _vanos_zone_limits(
    evidence: dict | None, policy: Policy, image: bytes, entry, requested: float,
    findings: list[dict],
) -> tuple[list[float] | None, str | None]:
    """Допустимый сдвиг фаз по ячейкам карты VANOS (обороты × нагрузка).

    Сдвиг разрешён только в зонах, где лог подтвердил ту же сторону сдвига
    (там механика держит цель). Зона без данных, зона с рассогласованием выше
    предела и противоположное направление — без сдвига.
    """
    recommendation, why = pack_recommendation.trusted_recommendation(
        evidence, "vanos", pack_log_analyzer.ANALYZER_VERSION
    )
    if recommendation is None:
        _zones_unavailable(findings, "vanos", why,
                           f"сдвиг плоский {requested:+g}°")
        return None, None
    rpm_axis = _policy_axis(image, policy, "ldpm_n_5")
    load_axis = _policy_axis(image, policy, "ldpm_maf_ivvt_2")
    if rpm_axis is None or load_axis is None:
        _zones_unavailable(findings, "vanos", "осей карт фаз нет в политике версии",
                           f"сдвиг плоский {requested:+g}°")
        return None, None
    if len(rpm_axis) != entry.rows or len(load_axis) != entry.columns:
        _zones_unavailable(
            findings, "vanos",
            f"оси ({len(rpm_axis)}×{len(load_axis)}) не совпали с картой "
            f"({entry.rows}×{entry.columns})",
            f"сдвиг плоский {requested:+g}°")
        return None, None
    limits = pack_recommendation.shift_limits_for_map(
        recommendation, rpm_axis, load_axis, requested
    )
    allowed = sum(1 for value in limits if value > 0)
    otherside = sum(
        1 for value in limits if value == 0
    )
    digest = str(recommendation.get("source_sha256") or "")
    note = (
        f"раскладка сдвига по зонам лога (confidence {recommendation.get('confidence')}, "
        f"sha256 {digest[:12]}…): сдвиг разрешён в {allowed} ячейках из {len(limits)} "
        f"(остальные — без данных, с рассогласованием или в другую сторону), "
        f"максимум {max(limits, default=0.0):.3g}°"
    )
    return limits, note


def _optm_caps(image: bytes, policy: Policy) -> list[float] | None:
    """Потолок зажигания: карта MBT (``ip_iga_optm_tco_2``), интерполированная
    на сетку карты RON98 (документ: веб-анализ §1.2).

    Возвращает список по ячейкам ``ip_iga_ron_98_pl_ivvt__n__maf`` (строки —
    обороты, столбцы — нагрузка) или ``None``, если оси/карты нет в политике
    версии — тогда потолок не применяется (записывается в наряд).
    """
    try:
        ron98 = policy.objects["ip_iga_ron_98_pl_ivvt__n__maf"]
        optm = policy.objects["ip_iga_optm_tco_2__n__maf"]
        n_ron = policy.objects["ldpm_n_14"]
        maf_ron = policy.objects["ldpm_maf_iga_2"]
        n_opt = policy.objects["ldpm_n_4"]
        maf_opt = policy.objects["ldpm_maf_iga_1"]
    except KeyError:
        return None
    if optm.read_only is not True:
        return None
    n_a = _read_all(image, n_ron)      # обороты сетки RON98 (20)
    m_a = _read_all(image, maf_ron)    # нагрузка сетки RON98 (16)
    n_b = _read_all(image, n_opt)      # обороты сетки optm (16)
    m_b = _read_all(image, maf_opt)    # нагрузка сетки optm (12)
    opt = _read_all(image, optm)       # 16 строк × 12 столбцов

    def axis_pos(grid: list[float], value: float) -> tuple[int, int, float]:
        if value <= grid[0]:
            return 0, 0, 0.0
        if value >= grid[-1]:
            last = len(grid) - 1
            return last, last, 0.0
        for i in range(len(grid) - 1):
            if grid[i] <= value <= grid[i + 1]:
                span = grid[i + 1] - grid[i]
                frac = 0.0 if span == 0 else (value - grid[i]) / span
                return i, i + 1, frac
        last = len(grid) - 1
        return last, last, 0.0

    cols = ron98.columns
    opt_cols = optm.columns
    caps: list[float] = []
    for i in range(ron98.rows):
        n0, n1, nf = axis_pos(n_b, n_a[i])
        for j in range(cols):
            m0, m1, mf = axis_pos(m_b, m_a[j])
            v00 = opt[n0 * opt_cols + m0]
            v01 = opt[n0 * opt_cols + m1]
            v10 = opt[n1 * opt_cols + m0]
            v11 = opt[n1 * opt_cols + m1]
            caps.append(
                (v00 * (1 - mf) + v01 * mf) * (1 - nf)
                + (v10 * (1 - mf) + v11 * mf) * nf
            )
    return caps


def _linked_optm_values(image: bytes, policy: Policy, ron_additions: list[float]) -> tuple[list[float], str] | None:
    """Make the warm-MBT target carry the same interpolated RON98 addition.

    ``ip_iga_optm_tco_2`` remains read-only in the catalog.  The returned
    request is accepted only by the calibration core when it carries
    ``write_context=ignition_optm_link`` and ``linked_to`` pointing at the
    RON98 symbol.  Quantization is downward relative to the original MBT cell,
    so the linked map can never realise a larger addition than the plan.
    """
    try:
        ron = policy.objects["ip_iga_ron_98_pl_ivvt__n__maf"]
        optm = policy.objects["ip_iga_optm_tco_2__n__maf"]
        ron_rpm = policy.objects["ldpm_n_14"]
        ron_load = policy.objects["ldpm_maf_iga_2"]
        opt_rpm = policy.objects["ldpm_n_4"]
        opt_load = policy.objects["ldpm_maf_iga_1"]
    except KeyError:
        return None
    if len(ron_additions) != ron.rows * ron.columns:
        return None
    additions = ignition_layout.interpolate_grid(
        ron_additions, ron.rows, ron.columns,
        _read_all(image, ron_rpm), _read_all(image, ron_load),
        _read_all(image, opt_rpm), _read_all(image, opt_load),
    )
    stock = _read_all(image, optm)
    quantum = abs(float(optm.scale_a))
    values: list[float] = []
    for current, addition in zip(stock, additions):
        target = current + max(0.0, float(addition))
        if optm.maximum is not None:
            target = min(target, float(optm.maximum))
        if quantum:
            target = ignition_layout.quantize_target_down(target, current, quantum)
        values.append(target)
    return values, (
        "ip_iga_optm_tco_2: та же добавка интерполирована с сетки RON98 "
        f"({len(values)} ячеек), прижата вниз к кванту {quantum:g}°"
    )


#: Что нужно измерить, когда пакет ждёт данных (соответствует
#: `docs/PACKS_LOG_REQUIREMENTS_RU.md`, разделы 3–5).
PACK_EVIDENCE_NEEDS = {
    "ignition": (
        "лог детонации по цилиндрам: ≥3 полных разгона 2000→ограничитель, ≥10 Гц",
        "коррекция по детонации ≤0.75° на ≥99% выбросов, без удержаний ≥1.5° дольше 0.5 с",
        "топливо не ниже RON 98, температура воздуха на впуске ≤45 °C",
    ),
    "fuel": (
        "широкополосная лямбда: ≥2 полных разгона + установившаяся частичная нагрузка ≥10 с",
        "стандартное отклонение лямбды на полном ходу ≤0.03, тримы частичной нагрузки ≤±8%",
    ),
    "vanos": (
        "лог фактических и целевых фаз валов (впуск/выпуск), ≥10 Гц",
        "сетка установившихся точек 2000..6000 об/мин × 150..600 мг/такт, удержание ≥3 с",
        "рассогласование факт/цель ≤3° (иначе механика не держит цели)",
    ),
}


def ignition_package(
    image: bytes, intents: list[dict], policy: Policy, findings: list[dict],
    octane_profile: str | None = None,
    allow_linked: bool = True,
) -> PackageResult:
    """Зажигание: RON98 Stage 1, optionally with the gated linked MBT map.

    Потолки — из единой спецификации (``tools/ignition_caps.py``: Stage 1 без
    лога ≤ +3,0° для профиля RON98 и ≤ +1,5° для RON95, Stage 2 с логом
    детонации ≤ +5,0°; RON91 — добавки нет). Добавка не плоская
    (задание docs/TASK_STAGE1_RU.md §2а): форма — ноль ниже 200 мг/такт и
    линейный рост до 600 (docs/PACKS_WEB_ANALYSIS_RU.md §7), соседние ячейки
    различаются не больше шага сглаживания из спецификации. Карта
    При разрешённом linked-пути ``ip_iga_optm_tco_2`` получает ту же добавку
    через bilinear interpolation (никогда не копируется абсолютный угол). При
    публичном bench-gated пути MBT не пишется: выше 3000 об/мин потолок малый,
    а полный linked-профиль ждёт стенд. RON91 не трогается.
    """
    title = (PACKAGE_TITLES["ignition"] if allow_linked
             else "Зажигание Stage 1 (RON98, мягкий публичный профиль без MBT)")
    asked = [item for item in intents
             if item.get("kind") in ("ignition", "ignition_stage1")]
    if not asked:
        return PackageResult("ignition", title, "not_requested",
                             "в заявке нет намерений по зажиганию")
    delta_no_log = ignition_caps.cap_no_log_deg(octane_profile)
    # The API's public path is deliberately limited to the existing no-log
    # Stage 1 ceiling.  A registry-cleared request may use the linked/log path.
    delta_with_log = (ignition_caps.cap_with_log_deg(octane_profile)
                      if allow_linked else delta_no_log)
    symbol = "ip_iga_ron_98_pl_ivvt__n__maf"
    entry = _entry(policy, symbol, findings, "ignition")
    if entry is None:
        return PackageResult("ignition", title, "rejected",
                             "карты зажигания нет в политике версии")
    requests: list[dict] = []
    notes: list[str] = []
    capped_cells = 0
    at_mbt_cells = 0
    layout_notes: set[str] = set()
    for intent in asked:
        try:
            delta = float(intent.get("delta_deg"))
        except (TypeError, ValueError):
            _finding(findings, "REQUEST_INVALID", "reject",
                     f"{intent.get('id')}: нужна добавка угла в градусах "
                     f"(0 < delta_deg ≤ {delta_with_log:g})")
            continue
        if delta <= 0:
            _finding(findings, "REQUEST_INVALID", "reject",
                     f"{intent.get('id')}: добавка угла должна быть положительной "
                     "(откатывать угол продуктом не имеет смысла)")
            continue
        if delta > delta_with_log + 1e-9:
            _finding(findings, "REQUEST_INVALID", "reject",
                     f"{intent.get('id')}: добавка {delta:g}° выше предела "
                     f"{delta_with_log:g}° — дальше только индивидуальная калибровка на стенде")
            continue
        spec = load_pack_requirements()
        evidence = _load_evidence(intent, findings)
        zone_caps: list[float] | None = None
        zone_note: str | None = None
        log_verified = False
        if delta > delta_no_log + 1e-9:
            ok_gate, gate_problems = evidence_gate(evidence, "ignition")
            ok, problems = validate_pack_evidence(evidence, "ignition", spec)
            problems = gate_problems + problems
            ok = ok and ok_gate
            if not ok:
                return PackageResult(
                    "ignition", title, "requires_data",
                    f"добавка {delta:g}° требует лога детонации: " + "; ".join(problems),
                    PACK_EVIDENCE_NEEDS["ignition"],
                )
            log_verified = True
            limit = delta_with_log
            # Лог есть и проверен: пределы по зонам рекомендации, поверх —
            # запас до MBT, форма и сглаживание.
            zone_caps, zone_note = _ignition_zone_caps(
                evidence, policy, image, entry, delta, delta_no_log, findings
            )
        else:
            limit = min(delta, delta_no_log)
        # The linked MBT map is raised together with RON98.  Therefore the
        # old MBT value is not a ceiling for the RON98 request: using it here
        # would make the new linked map pointless exactly in the cells where
        # the stock RON98 map was already above the old model reference.  The
        # policy maximum remains the hard ceiling; the linked writer applies
        # the same interpolated addition to the warm-MBT cells.
        axes = _ignition_axes(image, policy)
        rpm_axis = axes[0] if axes is not None and len(axes[0]) == entry.rows else None
        load_axis = axes[1] if axes is not None and len(axes[1]) == entry.columns else None
        stock = _read_all(image, entry)
        ceiling_by_cell = zone_caps
        if not allow_linked:
            if rpm_axis is None:
                _finding(findings, "RPM_AXIS_UNAVAILABLE", "info",
                         "публичный ignition: ось оборотов не найдена, оставлен общий "
                         "консервативный потолок")
                rpm_axis = [0.0] * entry.rows
            ceiling_by_cell = common_stages.public_ignition_caps(
                limit, rpm_axis, entry.columns
            )
            layout_notes.add(
                "мягкий публичный профиль: выше 3000 об/мин потолок уменьшен, "
                "linked MBT ждёт bench gate"
            )
        # Публичный путь не поднимает карту модели момента, поэтому угол не должен
        # уходить выше неё: MBT остаётся потолком (иначе разность «угол − опора» растёт
        # без согласованной правки optm_tco_2; проверено на реальной 430069: 33 → 202 ячейки).
        public_mbt = None if allow_linked else _optm_caps(image, policy)
        adds, layout_note = ignition_layout.plan_additions(
            stock, public_mbt, limit, load_axis, entry.columns, entry.rows,
            stage1=not log_verified, ceiling_by_cell=ceiling_by_cell,
        )
        if allow_linked:
            layout_notes.add("RON98 добавка не обнуляется старым MBT: MBT поднимается связно")
        if layout_note:
            layout_notes.add(layout_note)
        values: list[float] = []
        changed = 0
        for index, cur in enumerate(stock):
            add = adds[index]
            if add <= 0:
                values.append(cur)  # зона/запас/форма дали 0 — угол не трогаем
                continue
            target = cur + add
            if entry.maximum is not None and target > float(entry.maximum):
                # The linked MBT has its own policy ceiling; RON98 retains its
                # policy maximum even when the old MBT value was lower.
                target = float(entry.maximum)
                capped_cells += 1
            value = _fits_steps(entry, target)
            if value > cur + add + 1e-9:  # округление к сетке не поднимает над планом
                value = cur + add
            if value <= cur:
                value = cur
            if value != cur:
                changed += 1
            values.append(value)
        if not changed:
            if zone_caps is not None and not any(value > 0 for value in zone_caps):
                return PackageResult(
                    "ignition", title, "requires_data",
                    "добавить нечего: рекомендация лога запрещает добавку во всех "
                    "зонах карты (детонация/перегрев/бедная смесь или нет данных)",
                    ("переснять лог в целевых зонах (полная нагрузка, RON98+)",),
                )
            return PackageResult(
                "ignition", title, "requires_data",
                "добавить нечего: все ячейки уже на потолке MBT (карта не ниже "
                "цели не сдвигается), либо форма по нагрузке обнулила карту",
                ("проверить образ: возможно, это уже тюн, а не сток",),
            )
        requests.append(
            {
                "symbol": symbol,
                "values": values,
                "evidence": (
                    f"pack_log:{evidence.get('source')}" if log_verified else
                    (f"публичный мягкий Stage 1 без логов: RON98 до +{delta_no_log:g}°, "
                     "без linked MBT; выше 3000 об/мин малый потолок"
                     if not allow_linked else
                     f"Stage 1 без логов: неплоская раскладка по нагрузке, потолок "
                     f"+{delta_no_log:g}°; MBT поднимается связно")
                ),
            }
        )
        linked = _linked_optm_values(image, policy, adds) if allow_linked else None
        if linked is not None:
            linked_values, linked_note = linked
            if any(abs(after - before) > 1e-12
                   for before, after in zip(_read_all(image, policy.objects["ip_iga_optm_tco_2__n__maf"]), linked_values)):
                requests.append(
                    {
                        "symbol": "ip_iga_optm_tco_2__n__maf",
                        "values": linked_values,
                        "linked_to": symbol,
                        "write_context": "ignition_optm_link",
                        "evidence": "same ignition layout as " + symbol,
                    }
                )
                notes.append(linked_note)
        written_max = round(max(adds) / entry.scale_a) * entry.scale_a
        stats = ignition_layout.additions_stats(adds, limit, zero_by_margin=at_mbt_cells)
        notes.append(
            f"запрошено до +{min(delta, limit):g}°, записано до +{written_max:g}° "
            f"(квант сетки {entry.scale_a:g}°) в {changed} ячейках из {len(stock)}; "
            f"средняя добавка по записанным +{stats['mean_add']:g}°"
        )
        if zone_note:
            notes.append(zone_note)
        elif log_verified:
            notes.append("пределы зон не применены — добавка плоская по потолку логов")
        else:
            notes.append(
                f"неплоская раскладка по нагрузке: {stats['written_cells']} ячеек "
                f"с добавкой, {stats['at_ceiling']} ячеек на общем потолке "
                f"без лога {delta_no_log:g}°; старый MBT не обнуляет план"
            )
    if not requests:
        return PackageResult("ignition", title, "rejected",
                             "ни одно намерение по зажиганию не прошло проверки")
    detail = "; ".join(notes)
    for note in sorted(layout_notes):
        detail += f"; {note}"
    if capped_cells:
        detail += f"; {capped_cells} ячеек прижаты к policy maximum RON98"
    detail += "; карта RON91 (защитный пол) не изменялась"
    return PackageResult("ignition", title, "written", detail, (), tuple(requests))


def fuel_package(
    image: bytes, intents: list[dict], policy: Policy, findings: list[dict],
) -> PackageResult:
    """Топливо: вариант 1 — малая добавка ``ip_tib`` (без лога);
    обогащение полной нагрузки ``ip_ti_fl__n`` — только с логом широкой лямбды.

    Границы (веб-анализ §2.3): добавка ≤ +0.25 мс и ≤ +4 % на ячейку;
    обогащение λ вниз не более чем на 0.05 и не ниже λ 0.82. Карты частичной
    нагрузки без лога не правятся (их ошибка съедается адаптациями).
    """
    title = PACKAGE_TITLES["fuel"]
    asked = [item for item in intents if item.get("kind") == "fuel"]
    if not asked:
        return PackageResult("fuel", title, "not_requested",
                             "в заявке нет намерений по топливу")
    spec = load_pack_requirements()
    crit = spec["packages"]["fuel"]["criteria"]
    tib_ms_max = float(crit["ms43_tib_add_ms_max"])
    tib_pct_max = float(crit["ms43_tib_add_percent_max"])
    fl_delta_max = float(crit["ms43_delta_ti_fl_lambda_max"])
    lambda_min = float(crit["lambda_target_min"])
    requests: list[dict] = []
    notes: list[str] = []
    for intent in asked:
        add_ms = intent.get("tib_add_ms")
        add_pct = intent.get("tib_add_percent")
        enrich = intent.get("ti_fl_enrich_lambda")
        if add_ms is None and add_pct is None and enrich is None:
            return PackageResult(
                "fuel", title, "requires_data",
                "укажите добавку: вариант 1 — только малая добавка "
                f"(tib_add_ms ≤ {tib_ms_max:g} и/или tib_add_percent ≤ {tib_pct_max:g}); "
                f"обогащение полной нагрузки (ti_fl_enrich_lambda ≤ {fl_delta_max:g}) — "
                "только с логом широкой лямбды",
                PACK_EVIDENCE_NEEDS["fuel"],
            )
        if add_ms is not None or add_pct is not None:
            entry = _entry(policy, "ip_tib__n__maf", findings, str(intent.get("id")))
            if entry is None:
                continue
            try:
                ms_cap = tib_ms_max if add_ms is None else min(float(add_ms), tib_ms_max)
                pct_cap = tib_pct_max if add_pct is None else min(float(add_pct), tib_pct_max)
            except (TypeError, ValueError):
                _finding(findings, "REQUEST_INVALID", "reject",
                         f"{intent.get('id')}: tib_add_ms/tib_add_percent должны быть числами")
                continue
            if ms_cap <= 0 or pct_cap <= 0:
                _finding(findings, "REQUEST_INVALID", "reject",
                         f"{intent.get('id')}: добавка топлива должна быть положительной")
                continue
            stock = _read_all(image, entry)
            values: list[float] = []
            changed = 0
            for cur in stock:
                add = min(ms_cap, cur * pct_cap / 100.0)
                value = _fits_steps(entry, cur + add)
                if value <= cur:
                    value = cur
                if value != cur:
                    changed += 1
                values.append(value)
            if changed:
                requests.append(
                    {
                        "symbol": "ip_tib__n__maf",
                        "values": values,
                        "evidence": f"консервативная добавка ≤ {ms_cap:g} мс и ≤ {pct_cap:g}%",
                    }
                )
                notes.append(f"ip_tib: +добавка к {changed} ячейкам из {len(stock)}")
        if enrich is not None:
            entry = _entry(policy, "ip_ti_fl__n", findings, str(intent.get("id")))
            if entry is None:
                continue
            try:
                enrich = float(enrich)
            except (TypeError, ValueError):
                _finding(findings, "REQUEST_INVALID", "reject",
                         f"{intent.get('id')}: ti_fl_enrich_lambda должно быть числом")
                continue
            if enrich <= 0:
                _finding(findings, "REQUEST_INVALID", "reject",
                         f"{intent.get('id')}: обогащение задаётся положительным сдвигом λ вниз")
                continue
            evidence = _load_evidence(intent, findings)
            ok_gate, gate_problems = evidence_gate(evidence, "fuel")
            ok, problems = validate_pack_evidence(evidence, "fuel", spec)
            problems = gate_problems + problems
            ok = ok and ok_gate
            if not ok:
                return PackageResult(
                    "fuel", title, "requires_data",
                    "обогащение полной нагрузки требует лога широкой лямбды: "
                    + "; ".join(problems),
                    PACK_EVIDENCE_NEEDS["fuel"],
                )
            enrich = min(enrich, fl_delta_max)
            # Лог проверен воротами: запас до пола λ берём по зонам лога.
            zone_limits, zone_note = _fuel_zone_limits(
                evidence, policy, image, entry, enrich, lambda_min, findings
            )
            stock = _read_all(image, entry)
            values = []
            changed = 0
            for index, cur in enumerate(stock):
                # значение таблицы = 1 − λ; обогащение = рост значения
                add = enrich if zone_limits is None else zone_limits[index]
                if add <= 0:
                    values.append(cur)  # в этой зоне лог запаса не даёт
                    continue
                new_lambda = max((1.0 - cur) - add, lambda_min)
                value = _fits_steps(entry, 1.0 - new_lambda)
                if value <= cur:
                    value = cur
                if value != cur:
                    changed += 1
                values.append(value)
            if changed:
                requests.append(
                    {
                        "symbol": "ip_ti_fl__n",
                        "values": values,
                        "evidence": (
                            f"pack_log:{evidence.get('source')}" if evidence else "лог (встроенный)"
                        ),
                    }
                )
                notes.append(
                    f"ip_ti_fl: обогащение до −{enrich:g} λ, изменено {changed} "
                    f"из {len(stock)} ячеек"
                )
                if zone_note:
                    notes.append(zone_note)
    if not requests:
        if any(item.get("kind") == "fuel" for item in intents):
            return PackageResult("fuel", title, "requires_data",
                                 "нечего записать: добавки нулевые или полностью "
                                 "прижаты ограничениями",
                                 PACK_EVIDENCE_NEEDS["fuel"])
        return PackageResult("fuel", title, "requires_data", "нет данных")
    return PackageResult("fuel", title, "written", "; ".join(notes), (), tuple(requests))


def pedal_response_package(
    image: bytes, intents: list[dict], policy: Policy, findings: list[dict],
) -> PackageResult:
    """Optional three-level MT/AT pedal-response model for MS43.

    It is intentionally separate from MS45 and never touches the MS45 torque
    or ``ip_fac_tq_req_driv_mon`` symbols.  The AT default is the soft model;
    a customer must opt in and acknowledge the risk before the request reaches
    the calibration core.
    """
    title = PACKAGE_TITLES["pedal_response"]
    asked = [item for item in intents if item.get("kind") == "pedal_response"]
    if not asked:
        return PackageResult("pedal_response", title, "not_requested",
                             "пакет не выбран: по умолчанию не включается")
    if len(asked) > 1:
        _finding(findings, "DUPLICATE_INTENT", "reject",
                 "Отклик педали: в заявке допускается один параметрический уровень")
        return PackageResult("pedal_response", title, "rejected", "двойной запрос")
    intent = asked[0]
    if not (intent.get("acknowledge_risk") or intent.get("acknowledge_pedal_risk")):
        _finding(findings, "ACKNOWLEDGE_RISK_REQUIRED", "reject",
                 "Отклик педали изменяет запрос момента: нужно acknowledge_risk=true")
        return PackageResult("pedal_response", title, "rejected", "нет подтверждения риска")
    try:
        level = int(intent.get("level", 1))
        transmission = str(intent.get("transmission") or "automatic").lower()
        if level not in common_stages.SUPPORTED_PEDAL_LEVELS:
            raise ValueError("level должен быть 1, 2 или 3")
        # Validate the gearbox before creating any values.
        if transmission not in {"manual", "mt", "automatic", "auto", "at"}:
            raise ValueError("transmission должен быть MT или AT")
    except (TypeError, ValueError) as error:
        _finding(findings, "REQUEST_INVALID", "reject", f"Отклик педали: {error}")
        return PackageResult("pedal_response", title, "rejected", "параметры невалидны")
    acknowledged, missing_code = common_stages.pedal_risk_acknowledged(intent, level)
    if not acknowledged:
        message = ("для уровня 2/3 нужно отдельное acknowledge_level_risk=true"
                   if missing_code == "ACKNOWLEDGE_LEVEL_RISK_REQUIRED"
                   else "нужно acknowledge_risk=true")
        _finding(findings, missing_code or "ACKNOWLEDGE_RISK_REQUIRED", "reject",
                 "Отклик педали: " + message)
        return PackageResult("pedal_response", title, "rejected", "нет требуемого подтверждения риска")

    requests: list[dict] = []
    details: list[str] = []
    # All full-load pedal thresholds are derived from their own stock values;
    # no threshold is copied between the injection, VANOS and DISA branches.
    symbols = ("id_pvs_fl__n", "id_pvs_fl_ivvt__n", "id_pvs_fl_vim__n_vim")
    for symbol in symbols:
        entry = _entry(policy, symbol, findings, "pedal_response")
        if entry is None:
            continue
        stock = _read_all(image, entry)
        raw_values = common_stages.pedal_threshold_model(
            stock, level, transmission, minimum=entry.minimum, maximum=entry.maximum
        )
        values = []
        for value in raw_values:
            bounded = max(float(entry.minimum if entry.minimum is not None else value), value)
            if entry.maximum is not None:
                bounded = min(float(entry.maximum), bounded)
            bounded = _fits_steps(entry, bounded)
            if entry.minimum is not None:
                bounded = max(float(entry.minimum), bounded)
            if entry.maximum is not None:
                bounded = min(float(entry.maximum), bounded)
            values.append(bounded)
        if any(abs(a - b) > 1e-12 for a, b in zip(stock, values)):
            requests.append({"symbol": symbol, "values": values,
                             "write_context": "pedal_response"})
            details.append(f"{symbol}: {len(values)} порогов, −{level * 10}% к стоку")

    if not requests:
        return PackageResult("pedal_response", title, "requires_data",
                             "уровень уже совпадает с текущими картами — писать нечего")
    gearbox = "MT" if transmission in {"manual", "mt"} else "AT (мягко по умолчанию)"
    return PackageResult(
        "pedal_response", title, "written",
        f"уровень {level}/3, {gearbox}; " + "; ".join(details)
        + "; модель момента (ip_tqi_*) и MS45 не затронуты",
        requests=tuple(requests),
    )


def vanos_stage1_package(
    image: bytes, intents: list[dict], policy: Policy, findings: list[dict],
) -> PackageResult:
    """Cautious warm-map Stage 1 VANOS profile (no log, bench-gated by API)."""
    title = PACKAGE_TITLES["vanos_stage1"]
    asked = [item for item in intents if item.get("kind") == "vanos_stage1"]
    if not asked:
        return PackageResult("vanos_stage1", title, "not_requested",
                             "пакет не выбран")
    intent = asked[0]
    if not (intent.get("acknowledge_risk") or intent.get("acknowledge_vanos_risk")):
        _finding(findings, "ACKNOWLEDGE_RISK_REQUIRED", "reject",
                 "VANOS Stage 1: нужно acknowledge_risk=true")
        return PackageResult("vanos_stage1", title, "rejected", "нет подтверждения риска")
    try:
        shift_in, shift_ex = common_stages.cautious_vanos_shift(intent)
    except ValueError as error:
        _finding(findings, "REQUEST_INVALID", "reject", f"VANOS Stage 1: {error}")
        return PackageResult("vanos_stage1", title, "rejected", "сдвиг невалиден")
    requests: list[dict] = []
    details: list[str] = []
    for symbol, shift in (("ip_cam_sp_tco_2_in_pl__n__maf_iv", shift_in),
                          ("ip_cam_sp_tco_2_ex_pl__n__maf_iv", shift_ex)):
        entry = _entry(policy, symbol, findings, "vanos_stage1")
        if entry is None:
            continue
        stock = _read_all(image, entry)
        values = [_fits_steps(entry, value + shift) for value in stock]
        # ±5° is checked above; this second check protects against an XDF
        # range/quantum surprise and refuses instead of clipping silently.
        if any(abs(value - current) > 5.0 + 1e-9
               for current, value in zip(stock, values)):
            _finding(findings, "REQUEST_INVALID", "reject",
                     f"{symbol}: квантованный сдвиг вышел за ±5°")
            continue
        if any(abs(a - b) > 1e-12 for a, b in zip(stock, values)):
            requests.append({"symbol": symbol, "values": values,
                             "write_context": "vanos_stage1"})
            details.append(f"{symbol}: {shift:+g}°")
    if not requests:
        return PackageResult("vanos_stage1", title, "requires_data",
                             "сдвиг не лёг на сетку или карты уже изменены")
    return PackageResult(
        "vanos_stage1", title, "written",
        "только прогретые карты частичной нагрузки; впуск "
        f"{shift_in:+g}°, выпуск {shift_ex:+g}°; предел stock ±5°; "
        + "; ".join(details) + "; расширенный логовый профиль оставлен для v2",
        requests=tuple(requests),
    )


def vanos_package(
    image: bytes, intents: list[dict], policy: Policy, findings: list[dict],
) -> PackageResult:
    """VANOS: только тёплые карты частичной нагрузки и только по логу фаз.

    Карты полной нагрузки не правятся (сток на верхних оборотах уже на
    механическом потолке — веб-анализ §3.1), холостой ход не правится
    (стабильность ХХ без стенда не проверить). Сдвиг ≤ ±5° от стока ячейки.
    """
    title = PACKAGE_TITLES["vanos"]
    asked = [item for item in intents if item.get("kind") == "vanos"]
    if not asked:
        return PackageResult("vanos", title, "not_requested",
                             "в заявке нет намерений по VANOS")
    spec = load_pack_requirements()
    max_shift = float(spec["packages"]["vanos"]["criteria"]["max_shift_from_stock_deg"])
    symbols = {
        "shift_in_deg": "ip_cam_sp_tco_2_in_pl__n__maf_iv",
        "shift_ex_deg": "ip_cam_sp_tco_2_ex_pl__n__maf_iv",
    }
    for intent in asked:
        shifts = {
            key: intent.get(key) for key in symbols if intent.get(key) is not None
        }
        if not shifts:
            _finding(findings, "REQUEST_INVALID", "reject",
                     f"{intent.get('id')}: укажите сдвиг целей (shift_in_deg и/или "
                     f"shift_ex_deg, ±{max_shift:g}° от стока)")
            continue
        for key, value in shifts.items():
            try:
                shift = float(value)
            except (TypeError, ValueError):
                _finding(findings, "REQUEST_INVALID", "reject",
                         f"{intent.get('id')}: {key} должно быть числом")
                return PackageResult("vanos", title, "rejected", "сдвиг не число")
            if abs(shift) > max_shift:
                _finding(findings, "REQUEST_INVALID", "reject",
                         f"{intent.get('id')}: сдвиг {shift:g}° больше предела "
                         f"±{max_shift:g}° от стока")
                return PackageResult("vanos", title, "rejected", "сдвиг выше предела")
        evidence = _load_evidence(intent, findings)
        ok_gate, gate_problems = evidence_gate(evidence, "vanos")
        ok, problems = validate_pack_evidence(evidence, "vanos", spec)
        problems = gate_problems + problems
        ok = ok and ok_gate
        if not ok:
            return PackageResult(
                "vanos", title, "requires_data",
                "VANOS без измеренного лога фаз не выпускается: " + "; ".join(problems),
                PACK_EVIDENCE_NEEDS["vanos"],
            )
        requests: list[dict] = []
        notes: list[str] = []
        for key, symbol in symbols.items():
            shift = shifts.get(key)
            if shift is None:
                continue
            entry = _entry(policy, symbol, findings, str(intent.get("id")))
            if entry is None:
                continue
            # фактический сдвиг прижат к сетке карты (квант 0.375°)
            snapped_shift = round(float(shift) / entry.scale_a) * entry.scale_a
            # Сдвиг раскладывается по зонам лога: разрешена только та сторона,
            # которую подтвердил лог фаз (иначе ячейка не двигается).
            zone_limits, zone_note = _vanos_zone_limits(
                evidence, policy, image, entry, float(shift), findings
            )
            sign = 1.0 if float(shift) >= 0 else -1.0
            stock = _read_all(image, entry)
            values = []
            changed = 0
            blocked = 0
            for index, cur in enumerate(stock):
                allowed = abs(float(shift)) if zone_limits is None else zone_limits[index]
                if allowed <= 0:
                    values.append(cur)
                    blocked += 1
                    continue
                value = _fits_steps(entry, cur + sign * allowed)
                if value == cur:
                    pass
                else:
                    changed += 1
                values.append(value)
            if changed:
                requests.append(
                    {
                        "symbol": symbol,
                        "values": values,
                        "write_context": "vanos_stage1",
                        "evidence": f"pack_log:{evidence.get('source')}",
                    }
                )
                notes.append(
                    f"{symbol}: запрошен сдвиг {float(shift):+g}°, записан "
                    f"{snapped_shift:+g}° (квант сетки 0.375°) в {changed} ячейках"
                    + (f"; {blocked} ячеек без сдвига" if blocked else "")
                )
                if zone_note:
                    notes.append(zone_note)
        if not requests:
            return PackageResult("vanos", title, "requires_data",
                                 "сдвиг нулевой или не лёг в сетку — писать нечего",
                                 PACK_EVIDENCE_NEEDS["vanos"])
        return PackageResult("vanos", title, "written", "; ".join(notes), (), tuple(requests))
    return PackageResult("vanos", title, "rejected", "ни одно намерение не прошло проверку")


# ---------------------------------------------------------------------------
# Слой диагностики «без катализатора» (вариант 3, задание 3).
#
# Механизм — приращения счётчиков неисправностей ``c_abc_inc_*``: значение 0
# означает, что счётчик не растёт и код не выставляется (мс4х-вики,
# «Suppressing Diagnostic Trouble Codes»); в части комплектаций завод сам
# держит 0 (например, DMTL на 430037) — механизм штатный. Символы проверены
# по всем пяти версиям и внесены в политики с границами из стоков.
# ---------------------------------------------------------------------------

#: Группы подавляемых кодов (задание: катализатор, DMTL, лямбда-задержки
#: заднего зонда, СВП). Передний зонд остаётся — его коды не трогаются.
DTC_GROUPS: dict[str, tuple[str, ...]] = {
    "cat": ("c_abc_inc_conv_mon",),
    "dmtl": ("c_abc_inc_dmtl", "c_abc_inc_vdmtl"),
    "lambda": ("c_abc_inc_lsh_down", "c_abc_inc_vls_down", "c_abc_inc_vls_down_t"),
    "sap": ("c_abc_inc_sap",),
    # Приводные группы (шаг 2 задания, DISA/VIM и клапан ХХ/ISA): это не
    # «катализаторные» коды, поэтому ворота у них другие — правовая справка и
    # подтверждение механики, а не подтверждение отсутствия катализатора.
    "vim": ("c_abc_inc_vim",),
    "isa": ("c_abc_inc_isa_1", "c_abc_inc_isa_2"),
}

#: Группы, подавление которых требует подтверждения «катализатора нет».
CATALYST_DTC_GROUPS = frozenset({"cat", "dmtl", "lambda", "sap"})
#: Группы привода: подавление кода при живом приводе скрывает неисправность,
#: поэтому нужны правовая справка и подтверждение механики.
DRIVER_DTC_GROUPS = frozenset({"vim", "isa"})


def dtc_package(
    image: bytes, intents: list[dict], policy: Policy, request: dict,
    findings: list[dict],
) -> PackageResult:
    """Подавление диагностических кодов для машины без катализатора.

    Выпускается только при подтверждении ``no_catalyst: true`` и только в
    значении 0 (подавить); поднять приращения обратно в этом продукте нельзя.
    """
    mine = [item for item in intents if item.get("kind") == "dtc"]
    if not mine:
        return PackageResult("dtc", PACKAGE_TITLES["dtc"], "not_requested")
    groups: list[str] = []
    for item in mine:
        for group in item.get("groups") or ():
            groups.append(str(group).lower())
    if not groups:
        _finding(
            findings,
            "REQUEST_INVALID",
            "reject",
            "слой диагностики: укажите группы (например, "
            "{\"kind\": \"dtc\", \"groups\": [\"cat\", \"dmtl\", \"lambda\", \"sap\"]})",
        )
        return PackageResult("dtc", PACKAGE_TITLES["dtc"], "rejected", "группы не указаны")
    if set(groups) & CATALYST_DTC_GROUPS and not request.get("no_catalyst"):
        _finding(
            findings,
            "DTC_NO_CATALYST_REQUIRED",
            "reject",
            "слой диагностики: группы " + ", ".join(sorted(set(groups) & CATALYST_DTC_GROUPS))
            + " подавляются только при подтверждении no_catalyst: true "
            "(коды машины без катализатора)",
        )
        return PackageResult("dtc", PACKAGE_TITLES["dtc"], "rejected", "нет подтверждения")
    if set(groups) & DRIVER_DTC_GROUPS:
        if not request.get("acknowledge_legality"):
            _finding(
                findings,
                "ACKNOWLEDGE_LEGALITY_REQUIRED",
                "reject",
                "слой диагностики: подавление кодов привода (VIM/ISA) выпускается "
                "только с подтверждённой правовой справкой "
                "(acknowledge_legality: true)",
            )
            return PackageResult("dtc", PACKAGE_TITLES["dtc"], "rejected",
                                 "нет правовой справки")
        if not all(item.get("acknowledge_mechanics") for item in mine):
            _finding(
                findings,
                "REQUEST_INVALID",
                "reject",
                "слой диагностики: для групп привода нужно подтверждение механики "
                "(acknowledge_mechanics: true) — что сделано с заслонкой/клапаном",
            )
            return PackageResult("dtc", PACKAGE_TITLES["dtc"], "rejected",
                                 "нет подтверждения механики")
    unknown = sorted(set(groups) - set(DTC_GROUPS))
    if unknown:
        _finding(
            findings,
            "DTC_GROUP_UNKNOWN",
            "reject",
            f"слой диагностики: неизвестные группы {', '.join(unknown)}; допустимо "
            + ", ".join(sorted(DTC_GROUPS)),
        )
        return PackageResult("dtc", PACKAGE_TITLES["dtc"], "rejected", "группы неизвестны")
    requests: list[dict] = []
    details: list[str] = []
    already = 0
    for group in dict.fromkeys(groups):
        for symbol in DTC_GROUPS[group]:
            entry = policy.objects.get(symbol)
            if entry is None or entry.read_only:
                _finding(
                    findings,
                    "SYMBOL_NOT_ALLOWED",
                    "reject",
                    f"слой диагностики: {symbol} не разрешён политикой этой версии",
                )
                return PackageResult("dtc", PACKAGE_TITLES["dtc"], "rejected",
                                     f"{symbol} вне политики")
            current = _current(image, entry)
            if current == 0:
                already += 1
                continue
            requests.append({"symbol": symbol, "values": [0.0]})
            details.append(f"{symbol}: {current:g} -> 0")
    if not requests:
        return PackageResult(
            "dtc", PACKAGE_TITLES["dtc"], "written",
            f"изменений нет: все запрошенные коды уже подавлены в стоке ({already} шт.)",
        )
    if set(groups) & DRIVER_DTC_GROUPS:
        _finding(
            findings,
            "DTC_DRIVER_WARNING",
            "info",
            "слой диагностики: коды по приводу "
            + ", ".join(sorted(set(groups) & DRIVER_DTC_GROUPS))
            + " выставляться не будут; сама функция сохраняется, но состояние "
            "привода больше не контролируется — после записи обязательны скан "
            "диагностикой и проверка холостого хода",
        )
    _finding(
        findings,
        "DTC_LEGALITY_WARNING",
        "info",
        "слой диагностики: подавление кодов для машины без катализатора — "
        "эксплуатация на дорогах общего пользования может нарушать технические и "
        "экологические требования; после записи — скан диагностикой и контроль "
        "смеси/температур",
    )
    detail = "; ".join(details)
    if already:
        detail += f"; уже подавлено в стоке: {already}"
    return PackageResult("dtc", PACKAGE_TITLES["dtc"], "written", detail,
                         requests=tuple(requests))


# ---------------------------------------------------------------------------
# Три варианта поставки (задание 3, уточнения — в веб-анализе §6).
# Вариант задаётся полем заявки ``variant``: «v1», «v2» или «v3». Без поля —
# свободная сборка (совместимо со старыми заявками). Генератор обязан собрать
# любой вариант из одной заявки; намерения вне состава варианта — отказ.
# ---------------------------------------------------------------------------

VARIANTS: dict[str, dict] = {
    # Вариант 1 («Stage 1 базовый», задание docs/TASK_VARIANT1_RU.md §1):
    # базовое зажигание/топливо + одобренные владельцем функции, каждая —
    # отдельным намерением клиента (октан, хлопки, лимитеры, vmax, launch
    # control, DISA off, ISA off, холостой ход, вентилятор/термостат, слой
    # диагностики и патчи 430069). Ворота самих функций не меняются: хлопки и
    # диагностика по-прежнему требуют «катализатора нет» и правовой справки,
    # фичи патчлиста — версии 430069, добавка по логам — доказательства.
    # Варианты v2/v3 не пересобирались (решение владельца: пока расширения).
    "v1": {
        "title": "базовый (вариант 1): зажигание до +3.0° (профиль RON98; RON95 "
                 "— до +1.5°) без логов + одобренные функции "
                 "отдельными намерениями (топливо не правится без лямбды)",
        "kinds": {
            "ignition", "ignition_stage1", "fuel", "octane", "pedal_response", "vanos_stage1",
            "popcorn", "rpm_limit", "vmax", "dtc",
            "launch_control", "ral", "ignition_cut", "mil_knock", "m_cluster",
            "ds2_logging", "vss_fix",
            "disa_off", "isa_off", "idle", "fan_thermostat",
        },
        # Потолки — из единой спецификации (docs/pack_log_requirements.json
        # через tools/ignition_caps.py), «на глаз» здесь чисел нет.
        "ignition_without_log_deg": ignition_caps.delta_without_log_deg(),
        "ignition_with_log_deg": ignition_caps.delta_with_log_deg(),
        "fuel_mode": "tib_only",
    },
    "v2": {
        "title": "расширенный: вариант 1 + ВАНос и обороты, топливо/зажигание по логам",
        "kinds": {"ignition", "ignition_stage1", "fuel", "vanos", "vmax", "rpm_limit"},
        "ignition_without_log_deg": ignition_caps.delta_without_log_deg(),
        "ignition_with_log_deg": ignition_caps.delta_with_log_deg(),
        "fuel_mode": "full",
    },
    "v3": {
        "title": "без катализатора: вариант 2 + хлопки и слой диагностики по существующим воротам",
        "kinds": {"ignition", "ignition_stage1", "fuel", "vanos", "vmax", "rpm_limit", "popcorn", "puc", "dtc"},
        "ignition_without_log_deg": ignition_caps.delta_without_log_deg(),
        "ignition_with_log_deg": ignition_caps.delta_with_log_deg(),
        "fuel_mode": "full",
        "requires_no_catalyst": True,
    },
}


def apply_variant_rules(
    variant: str | None, request: dict, intents: list[dict], findings: list[dict],
    octane_profile: str | None = None,
) -> None:
    """Ворота варианта поставки: состав, пределы и подтверждения."""
    if not variant:
        return
    variant = str(variant).lower()
    spec = VARIANTS.get(variant)
    if spec is None:
        _finding(
            findings,
            "VARIANT_UNKNOWN",
            "reject",
            f"вариант {variant!r} неизвестен: поддерживаются "
            + ", ".join(sorted(VARIANTS)),
        )
        return
    for intent in intents:
        kind = intent.get("kind")
        if kind not in spec["kinds"]:
            _finding(
                findings,
                "VARIANT_SCOPE_VIOLATION",
                "reject",
                f"вариант {variant} ({spec['title']}): намерение {kind!r} вне состава "
                "варианта — соберите заявку другого варианта",
            )
    if variant == "v1":
        # Stage 1 («без логов») — потолок профиля клиента из единой
        # спецификации: RON98 → 3.0°, RON95 → 1.5°, не выбран → 1.5°.
        v1_cap = ignition_caps.cap_no_log_deg(octane_profile)
        for intent in intents:
            kind = intent.get("kind")
            if kind in ("ignition", "ignition_stage1"):
                delta = intent.get("delta_deg")
                if delta is not None and float(delta) > v1_cap + 1e-9:
                    _finding(
                        findings,
                        "VARIANT_SCOPE_VIOLATION",
                        "reject",
                        f"вариант v1: добавка зажигания {float(delta):g}° больше "
                        f"потолка Stage 1 для профиля "
                        f"{(octane_profile or 'не выбран').upper()} "
                        f"(+{v1_cap:g}°) — добавки по логам разрешены с варианта v2",
                    )
            if kind == "fuel" and intent.get("ti_fl_enrich_lambda"):
                _finding(
                    findings,
                    "VARIANT_SCOPE_VIOLATION",
                    "reject",
                    "вариант v1: обогащение полной нагрузки не входит в базовый "
                    "вариант — только малая добавка к ip_tib (обогащение с варианта v2)",
                )
    if spec.get("requires_no_catalyst") and not request.get("no_catalyst"):
        _finding(
            findings,
            "NO_CATALYST_REQUIRED",
            "reject",
            f"вариант {variant}: нужно подтверждение no_catalyst: true в заявке "
            "(вариант для машины без катализатора)",
        )
    if spec.get("requires_no_catalyst"):
        _finding(
            findings,
            "V3_LEGALITY_WARNING",
            "info",
            "вариант v3: машина без катализатора — эксплуатация на дорогах общего "
            "пользования может нарушать технические и экологические требования; "
            "план проверки: герметичность выпуска, лямбда-логи, температуры, "
            "диагностический скан до и после записи",
        )


def build(
    stage: str,
    image: bytes,
    request: dict,
    *,
    policy: Policy | None = None,
    patch_catalog: pt.PatchCatalog | None = None,
    popcorn_profile: dict | None = None,
    source_name: str = "",
) -> StageResult:
    stage = str(stage).lower()
    if stage not in STAGE_PACKAGES:
        raise cal.PatchError(f"стадия {stage!r} неизвестна; есть {', '.join(STAGE_PACKAGES)}")
    # Политика — по версии калибровки из образа: каждая версия пишется своей
    # политикой (адреса и границы сняты со стоков именно этой версии).
    if policy is None:
        policy = resolve_policy(image)
    patch_catalog = patch_catalog or pt.load_catalog()
    intents = list(request.get("intents") or [])
    findings: list[dict] = []
    acknowledgements = {
        key for key in ("acknowledge_risk", "acknowledge_oil_pump")
        if any(item.get(key) for item in intents) or request.get(key)
    }
    declarations: set[str] = set(request.get("declarations") or ())
    # Вариант 1 (задание docs/TASK_VARIANT1_RU.md): профиль топлива читаем
    # раньше ворот варианта — потолок Stage 1 зависит от профиля (раунд 4),
    # чтобы «95-й бензин + добавка угла выше права» и «хлопки без
    # подтверждения» не дошли до записи.
    octane_profile = variant1.octane_choice(request, intents, findings)
    # Вариант поставки (задание 3): состав и ворота проверяются до пакетов.
    apply_variant_rules(request.get("variant"), request, intents, findings,
                        octane_profile)
    variant1.enforce_octane(octane_profile, intents, findings)
    variant1.legality_gate(request, intents, findings, ("popcorn", "dtc"))
    result = StageResult(
        stage=stage,
        status="REJECTED",
        source_sha256=hashlib.sha256(image).hexdigest(),
        policy_version=policy.version if policy is not None else 0,
        customer=str(request.get("customer", "")),
        notes=str(request.get("notes", "")),
        packages=(),
        octane_profile=octane_profile,
    )
    # Защита по версии калибровки: раньше всех, до расчёта пакетов, чтобы файл
    # для неподдерживаемой версии не собирался вообще (REVIEW_CLAUDE_RU.md, задание 1).
    result.cal_version = cal.read_cal_version(image)
    result.software = tuple(policy.software) if policy is not None else covered_software()
    if policy is None:
        guard = cal.check_version_supported(image)
        _finding(
            findings,
            guard.code if guard else "CAL_VERSION_UNSUPPORTED",
            guard.severity if guard else "reject",
            guard.message if guard else "для этой версии калибровки нет политики",
        )
        result.findings = findings
        result.manifest = _manifest(result, None, [], [], request)
        return result
    result.available_packages = tuple(
        package
        for package in STAGE_PACKAGES[stage]
        if package != "features" or patchlist_available(policy)
    )
    guard = cal.check_cal_version(image, policy)
    if guard is not None:
        _finding(findings, guard.code, guard.severity, guard.message)
        result.findings = findings
        result.manifest = _manifest(result, None, [], [], request)
        return result
    if stage == "stage2" and not request.get("hardware"):
        _finding(findings, "HARDWARE_REQUIRED", "reject",
                 "Stage 2 привязан к комбинации железа: нужен блок hardware")
    packages: list[PackageResult] = []
    requests: list[dict] = []
    patches: list[str] = []
    for package in STAGE_PACKAGES[stage]:
        if package == "limits":
            item = limits_package(image, intents, policy, acknowledgements, findings)
        elif package == "popcorn":
            item = popcorn_package(image, intents, policy, popcorn_profile, findings)
        elif package == "features":
            item = features_package(image, intents, policy, patches, findings, declarations)
        elif package == "ignition":
            item = ignition_package(
                image, intents, policy, findings, octane_profile=octane_profile,
                allow_linked=bool(request.get("_allow_linked_ignition", True)),
            )
        elif package == "fuel":
            item = fuel_package(image, intents, policy, findings)
        elif package == "vanos_stage1":
            item = vanos_stage1_package(image, intents, policy, findings)
        elif package == "vanos":
            item = vanos_package(image, intents, policy, findings)
        elif package == "pedal_response":
            item = pedal_response_package(image, intents, policy, findings)
        elif package == "dtc":
            item = dtc_package(image, intents, policy, request, findings)
        elif package == "disa_off":
            item = disa_off_package(image, intents, policy, findings)
        else:
            item = requires_data_package(package, intents)
        packages.append(item)
        requests.extend(item.requests)
        patches.extend(item.patches)
    result.packages = tuple(packages)
    result.findings = findings
    if any(item.get("severity") == "reject" for item in findings):
        result.status = "REJECTED"
        result.manifest = _manifest(result, None, requests, patches, {})
        return result

    # VIN не проверяем: community-порты (E39/Z3/M52TUB28) — совместимость
    # проверяет человек перед записью (TASK_RU.md).
    plan = cal.plan_changes(
        image,
        requests,
        policy=policy,
        patch_catalog=patch_catalog,
        patches=tuple(dict.fromkeys(patches)),
        declarations=declarations,
        acknowledgements=acknowledgements,
    )
    result.plan = plan
    result.manifest = _manifest(result, plan, requests, patches, request)
    if plan.status != "PLANNED":
        for finding in plan.problems():
            _finding(findings, finding.code, finding.severity, finding.message)
        # «нечего делать» — это не отказ заявки, а состояние «ждём данные/намерения»
        codes = {finding.code for finding in plan.problems()}
        result.status = "REQUIRES_DATA" if codes <= {"NOTHING_TO_DO"} else "REJECTED"
        return result
    built = cal.apply_plan(image, plan, patch_catalog=patch_catalog)
    result.result = built
    result.status = "BUILT" if built.changed_cells or built.patches else "REQUIRES_DATA"
    # манифест собираем после финального статуса, иначе в него попадёт черновой
    result.manifest = _manifest(result, plan, requests, patches, request)
    result.manifest.update(
        {
            "result_sha256": built.sha256,
            "changed_cells": built.changed_cells,
            "changed_bytes": built.changed_bytes,
            "crc_words": [
                {"record": name, "offset": f"0x{offset:05X}",
                 "before": f"0x{before:04X}", "after": f"0x{after:04X}"}
                for name, offset, before, after in built.crc_words
            ],
        }
    )
    return result


#: Версия схемы манифеста. 1 — манифест несёт блок ``result`` (sha256, размер,
#: число изменённых байт, слова CRC, диапазоны патчей), по которому работает
#: ``--verify-manifest``.
MANIFEST_VERSION = 1


def _patch_ranges(patches: list[str]) -> list[dict]:
    """Диапазоны байтов, которые меняют патчи кода (для проверки «ничего лишнего»)."""
    if not patches:
        return []
    try:
        catalog = pt.load_catalog()
    except Exception:  # pragma: no cover - каталог обязателен, но не роняем манифест
        return []
    wanted = set(patches)
    ranges: list[dict] = []
    for patch in catalog.patches:
        if patch.title not in wanted:
            continue
        for entry in patch.entries:
            ranges.append(
                {"start": f"0x{entry.address:05X}", "size": entry.end - entry.address}
            )
    return ranges


def _manifest(
    result: StageResult, plan: cal.PatchPlan | None, requests: list[dict],
    patches: list[str], request: dict,
) -> dict:
    # Хлопки и диагностический слой: правовая справка прикладывается к наряду и
    # манифесту, а не «по запросу» (задание docs/TASK_VARIANT1_RU.md §2).
    legal = None
    if request.get("acknowledge_legality") is True or any(
        item.get("kind") in variant1.LEGAL_GATE_KINDS
        for item in (request.get("intents") or ())
        if isinstance(item, dict)
    ):
        legal = variant1.legal_notice_payload()
    return {
        "manifest_version": MANIFEST_VERSION,
        "trust_level": variant1.TRUST_LEVEL_STATEMENT,
        "stage": result.stage,
        # Диапазоны патчей нужны проверке по манифесту: изменённые байты
        # обязаны укладываться в план (ячейки, слова CRC, патчи).
        "patch_ranges": _patch_ranges(sorted(set(patches))),
        "octane": variant1.octane_report(result.octane_profile),
        "legal_notice": legal,
        "rollback": variant1.rollback_block(
            result.source_sha256,
            image_kind="образ 512 КиБ",
        ),
        "status": result.status,
        "customer": result.customer,
        "date": date.today().isoformat(),
        "source_sha256": result.source_sha256,
        "cal_version": result.cal_version,
        "supported_software": list(result.software),
        "covered_software": list(covered_software()),
        "available_packages": list(result.available_packages),
        "policy_version": result.policy_version,
        "requests": requests,
        "patches": sorted(set(patches)),
        "packages": [
            {"id": item.id, "status": item.status, "detail": item.detail,
             "needs": list(item.needs)}
            for item in result.packages
        ],
        "hardware": request.get("hardware"),
        "findings": result.findings,
        "plan": plan.as_dict() if plan else None,
    }


def build_note(result: StageResult, source_name: str = "") -> str:
    """Наряд по-русски: что записано, чем подтверждено и что осталось ждать."""
    lines = [
        f"# Наряд MS43 — {result.stage.upper()}",
        "",
        f"* клиент: {result.customer or '—'}",
        f"* дата: {date.today().isoformat()}",
        f"* статус: {result.status}",
        f"* исходный образ: {source_name or '—'} (sha256 {result.source_sha256[:16]}…)",
        f"* версия калибровки образа: {result.cal_version or 'не прочитана'}",
    ]
    lines.append(f"* {variant1.trust_note_ru()}")
    octane = result.manifest.get("octane") or variant1.octane_report(result.octane_profile)
    lines.append(
        f"* топливо: {octane.get('fuel') or 'профиль не выбран'} — {octane['note_ru']}"
    )
    supported = list(result.software)
    if result.cal_version not in supported:
        lines.append(
            f"* поддерживаемые версии: {', '.join(supported) or 'нет'}; эта версия "
            "не поддерживается — ядро не записывает в такой образ ничего"
        )
    else:
        lines.append(
            f"* политика версии: {', '.join(supported)} (адреса и границы сняты со "
            "стоков этой версии)"
        )
    covered = list(result.manifest.get("covered_software") or covered_software())
    lines.append(f"* покрытые версии калибровки: {', '.join(covered)}")
    if result.available_packages:
        lines.append(
            "* доступные пакеты на этой версии: "
            + ", ".join(
                f"{package} ({PACKAGE_TITLES.get(package, package)})"
                for package in result.available_packages
            )
        )
    if "features" not in result.available_packages:
        lines.append(
            "* фичи патчлиста (LC/RAL, отсечка по зажиганию, MIL, кластер M3, DS2, VSS) "
            "на этой версии не предлагаются: их basedata проверена только на 430069"
        )
    if result.notes:
        lines.append(f"* примечание заявки: {result.notes}")
    if result.manifest.get("hardware"):
        hardware = result.manifest["hardware"]
        lines.append("* железо: " + ", ".join(f"{key}={value}" for key, value in hardware.items()))
    if result.result:
        lines.append(f"* результат: sha256 {result.result.sha256[:16]}…")
    lines.append("")

    lines.append("## Что записано")
    lines.append("")
    if not result.result or (not result.result.changed_cells and not result.result.patches):
        lines.append("Ничего не записано: все пакеты либо не запрошены, либо ждут данных.")
    else:
        if result.result.patches:
            lines.append("Патчи (I/O фич):")
            for title in result.result.patches:
                rules = pt.gate(pt.load_catalog().patch(title))
                lines.append(f"* `{title}`" + (f" — подтверждение: {rules['declaration']}"
                                               if rules.get("declaration") else ""))
            lines.append("")
        if result.plan and result.plan.symbols:
            lines.append("| символ | группа | ячеек | было → стало | ошибка квантования |")
            lines.append("| --- | --- | --- | --- | --- |")
            for item in result.plan.symbols:
                if not item.cells:
                    continue
                if len(item.cells) == 1:
                    cell = item.cells[0]
                    change = f"{cell.before:g} → {cell.after:g}"
                    error = f"{cell.quant_error_percent:.3f}%"
                else:
                    change = (
                        f"{min(c.before for c in item.cells):g}…"
                        f"{max(c.before for c in item.cells):g} → "
                        f"{min(c.after for c in item.cells):g}…"
                        f"{max(c.after for c in item.cells):g}"
                    )
                    error = f"до {max(c.quant_error_percent for c in item.cells):.3f}%"
                lines.append(
                    f"| `{item.symbol}` | {item.group} | {len(item.cells)} | {change} | {error} |"
                )
            lines.append("")
            lines.append("Источники правок:")
            for item in result.plan.symbols:
                if item.cells:
                    lines.append(f"* `{item.symbol}` — {item.source}")
            lines.append("")
        if result.result.crc_words:
            lines.append("Контрольные суммы (пересчитаны нашим ядром и проверены):")
            for name, offset, before, after in result.result.crc_words:
                lines.append(f"* {name} @0x{offset:05X}: 0x{before:04X} → 0x{after:04X}")
            lines.append("")

    lines.append("## Что не сделано и почему")
    lines.append("")
    pending = [item for item in result.packages if item.status == "requires_data"]
    if not pending:
        lines.append("Пакетов, ожидающих данные, нет.")
    for item in pending:
        lines.append(f"* **{item.title}** — {item.detail}")
        for need in item.needs:
            lines.append(f"  * нужно: {need}")
    lines.append("")

    if result.findings:
        lines.append("## Ворота и замечания")
        lines.append("")
        for finding in result.findings:
            lines.append(f"* `{finding['code']}` ({finding['severity']}): {finding['message']}")
        lines.append("")

    if result.manifest.get("legal_notice"):
        lines.append(variant1.legal_notice_markdown())
    rollback = result.manifest.get("rollback") or variant1.rollback_block(
        result.source_sha256, image_kind="образ 512 КиБ"
    )
    lines.append(variant1.rollback_markdown(rollback))

    lines.extend(
        [
            "## Как заливать",
            "",
            f"* файл — полный образ 512 КиБ, поддерживаемые версии калибровки: "
            f"{', '.join(supported) or 'нет'}; только полная запись "
            "(MS4X Dev Group Flasher / JMGarage / WinKFP);",
            "* bootmode не нужен; он требуется только для виргинизации/перепривязки EWS "
            "(см. `MS43/docs/UPGRADE_430069_RU.md`);",
            "* перед записью сделать полный бэкап ЭБУ; после записи — ключ off 10–15 с, "
            "синхронизация EWS при необходимости, сброс ошибок и адаптаций;",
            "* хлопки — только на машине без катализатора; следить за температурой "
            "выпуска и ОЖ;",
            "* прирост мощности в л.с. не обещается: без стенда это маркетинг. "
            "Критерий приёмки — повторяемая разница момента на одном стенде.",
            "",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Проверка выпущенного файла по манифесту
# ---------------------------------------------------------------------------

#: Где искать стоковый исходник, чтобы доказать «изменено только запланированное».
STOCK_SEARCH_DIRS: tuple[Path, ...] = (FIRMWARE_DIR, ARCHIVE_DIR)


@dataclass
class ManifestCheck:
    """Одна проверка файла по манифесту."""

    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class ManifestVerification:
    """Итог проверки: что сошлось, что нет."""

    manifest_path: str
    image_path: str | None
    status: str  # VERIFIED | FAILED | NOT_BUILT
    checks: list[ManifestCheck] = field(default_factory=list)
    source_path: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "VERIFIED"

    def as_dict(self) -> dict:
        return {
            "manifest": self.manifest_path.replace("\\", "/"),
            "image": None if self.image_path is None else self.image_path.replace("\\", "/"),
            "status": self.status,
            "ok": self.ok,
            "source_found": (
                None if self.source_path is None else self.source_path.replace("\\", "/")
            ),
            "checks": [item.as_dict() for item in self.checks],
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_stock_source(sha256: str, source_dir: str | Path | None = None) -> Path | None:
    """Найти в репозитории стоковый файл с таким sha256 (для сверки «до/после»)."""
    dirs = ([Path(source_dir)] if source_dir else []) + list(STOCK_SEARCH_DIRS)
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.bin")):
            try:
                if _sha256_file(path) == sha256:
                    return path
            except OSError:  # pragma: no cover - файл исчез во время обхода
                continue
    return None


def verify_manifest(
    manifest_path: str | Path,
    image_path: str | Path | None = None,
    *,
    source_dir: str | Path | None = None,
) -> ManifestVerification:
    """Проверить выпущенный файл по манифесту: суммы, ячейки, «ничего лишнего».

    Манифест — то, что генератор отдал клиенту. Проверка отвечает на вопрос
    «тот ли это файл и записано ли в нём ровно то, что в наряде», не требуя
    доверия к тому, кто файл пересылал: сверяются sha256, CRC-16 по записям,
    каждое запланированное слово CRC и ячейка, а разница со стоком обязана
    укладываться в план (ячейки, слова CRC и диапазоны патчей).
    """
    manifest_file = Path(manifest_path)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    verification = ManifestVerification(str(manifest_file), None, "FAILED")
    checks = verification.checks

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append(ManifestCheck(name, bool(ok), detail))

    version = manifest.get("manifest_version")
    if version is None:
        # Манифесты, собранные до введения поля, описывают ту же схему.
        check("manifest_version", True, "поле не писалось (схема 1 до введения поля)")
    else:
        check("manifest_version", isinstance(version, int) and 1 <= version <= MANIFEST_VERSION,
              f"версия схемы {version}, поддерживается до {MANIFEST_VERSION}")

    status = manifest.get("status")
    result_sha = manifest.get("result_sha256")
    if status != "BUILT" or not result_sha:
        check("status", status == "BUILT",
              f"статус {status}: проверять нечего, файл не выпущен")
        verification.status = "NOT_BUILT"
        return verification
    check("status", True, "BUILT")

    if image_path is None:
        result_name = manifest.get("result_name")
        if result_name:
            candidate = manifest_file.parent / str(result_name)
            if candidate.exists():
                image_path = candidate
    if image_path is None:
        check("result_name", False, "файл не указан и в манифесте нет result_name")
        verification.status = "FAILED"
        return verification
    image_file = Path(image_path)
    if not image_file.exists():
        check("image", False, f"нет файла {image_file}")
        verification.status = "FAILED"
        return verification
    verification.image_path = str(image_file)
    image = image_file.read_bytes()

    plan = manifest.get("plan") or {}
    expected_size = plan.get("image_size")
    check("image_size", len(image) == expected_size,
          f"{len(image)} байт, в манифесте {expected_size}")
    actual_sha = hashlib.sha256(image).hexdigest()
    check("sha256", actual_sha == result_sha,
          f"sha256 {actual_sha[:16]}…"
          + ("" if actual_sha == result_sha else " не совпал"))

    # CRC-16 по записям: у MS43 это главный признак «образ собран нашим ядром».
    try:
        report = ck.verify_image(image_file)
    except ck.MS43Error as error:
        check("checksums", False, f"{error}")
        verification.status = "FAILED"
        return verification
    details = ", ".join(
        f"{item.name} {'OK' if item.ok else 'FAIL'}" for item in report.records
    )
    check("checksums", report.ok and bool(report.records), details or "записей не найдено")
    check("image_kind", report.kind == "full512",
          f"тип образа {report.kind} (ожидается полный 512 КиБ)")
    cal_version = ck.read_cal_version(image)
    check("cal_version", cal_version == manifest.get("cal_version"),
          f"в файле {cal_version or 'не прочитана'}, в манифесте {manifest.get('cal_version')}")

    # Слова CRC: смещения в манифесте — от начала файла, порядок байтов младший вперёд.
    words = manifest.get("crc_words") or []
    word_problems: list[str] = []
    for word in words:
        offset = int(str(word["offset"]), 16)
        stored = int.from_bytes(image[offset : offset + 2], "little")
        if offset + 2 > len(image) or stored != int(str(word["after"]), 16):
            word_problems.append(f"{word['record']}@0x{offset:05X}")
    check("crc_words", bool(words) and not word_problems,
          f"{len(words)} слов CRC совпадают с манифестом"
          + (f", расхождения: {', '.join(word_problems)}" if word_problems else ""))

    # Ячейки: в файле должно лежать ровно то, что записано в плане.
    cells = [cell for item in plan.get("symbols", []) for cell in item.get("cells", [])]
    mismatched: list[str] = []
    for cell in cells:
        offset = int(str(cell["address"]), 16)
        size = int(cell.get("size_bytes") or 0)
        raw = image[offset : offset + size]
        if size < 1 or len(raw) != size or int.from_bytes(raw, "little") != int(cell["after_raw"]):
            mismatched.append(f"[{cell['index']}]@0x{offset:05X}")
    check("planned_cells", bool(cells) and not mismatched,
          f"{len(cells) - len(mismatched)}/{len(cells)} ячеек совпадают с планом"
          + (f", расхождения: {', '.join(mismatched[:5])}" if mismatched else ""))

    # «Ничего лишнего»: ищем стоковый исходник и сверяем байтовую разницу.
    source_sha = manifest.get("source_sha256")
    source_path = find_stock_source(str(source_sha), source_dir) if source_sha else None
    if source_path is None:
        check("outside_plan_changes", True,
              "стоковый исходник не найден локально — проверка «ничего лишнего» "
              "пропущена, суммы и ячейки проверены")
    else:
        verification.source_path = str(source_path)
        source = source_path.read_bytes()
        if len(source) != len(image):
            check("outside_plan_changes", False,
                  f"размер исходника {len(source)} ≠ размера файла {len(image)}")
        else:
            allowed: set[int] = set()
            for cell in cells:
                start = int(str(cell["address"]), 16)
                allowed.update(range(start, start + int(cell.get("size_bytes") or 0)))
            for word in words:
                offset = int(str(word["offset"]), 16)
                allowed.update(range(offset, offset + 2))
            for ran in manifest.get("patch_ranges") or []:
                start = int(str(ran["start"]), 16)
                allowed.update(range(start, start + int(ran["size"])))
            changed = [i for i, (a, b) in enumerate(zip(source, image)) if a != b]
            outside = [i for i in changed if i not in allowed]
            expected_bytes = manifest.get("changed_bytes")
            check("changed_bytes", expected_bytes is None or len(changed) >= int(expected_bytes),
                  f"изменено {len(changed)} байт (в манифесте {expected_bytes})")
            check("outside_plan_changes", not outside,
                  "изменённых байт вне плана нет" if not outside
                  else "байты вне плана: " + ", ".join(f"0x{i:05X}" for i in outside[:8]))

    verification.status = "VERIFIED" if all(item.ok for item in checks) else "FAILED"
    return verification


def verification_as_markdown(verification: ManifestVerification) -> str:
    """Отчёт проверки по-русски: для акта сдачи клиенту."""
    lines = [
        "# Проверка файла по манифесту (MS43)",
        "",
        f"* манифест: {verification.manifest_path}",
        f"* файл: {verification.image_path or '—'}",
        "* стоковый исходник: "
        + (verification.source_path or "не найден локально"),
        f"* итог: {verification.status}",
        "",
        "| проверка | итог | подробности |",
        "| --- | --- | --- |",
    ]
    for item in verification.checks:
        lines.append(f"| {item.name} | {'OK' if item.ok else 'НЕТ'} | {item.detail} |")
    lines.append("")
    if verification.status == "VERIFIED":
        lines.append("Файл соответствует манифесту: суммы сходятся, записано ровно "
                     "запланированное, изменений вне плана нет.")
    elif verification.status == "NOT_BUILT":
        lines.append("Манифест описывает стадию без выпущенного файла — проверять нечего.")
    else:
        lines.append("Файл НЕ соответствует манифесту. Не прошивать: сначала выяснить расхождение.")
    lines.append("")
    return "\n".join(lines)


def _run_verify(args: argparse.Namespace) -> int:
    """Режим ``--verify-manifest``: проверить выпущенный файл по манифесту."""
    try:
        verification = verify_manifest(
            args.verify_manifest, args.image, source_dir=args.source_dir
        )
    except (OSError, ValueError, KeyError) as error:
        print(f"ошибка чтения манифеста: {error}", file=sys.stderr)
        return 2
    print(f"проверка по манифесту: {verification.status}")
    for item in verification.checks:
        print(f"  [{'OK' if item.ok else 'НЕТ'}] {item.name}: {item.detail}")
    if verification.status == "VERIFIED":
        print(f"файл {verification.image_path} соответствует манифесту")
        code = 0
    elif verification.status == "NOT_BUILT":
        print("стадия не выпускала файл — проверять нечего")
        code = 0
    else:
        print("файл НЕ соответствует манифесту: прошивать нельзя", file=sys.stderr)
        code = 2
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(
            json.dumps(verification.as_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n",
        )
    if args.note_out:
        Path(args.note_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.note_out).write_text(
            verification_as_markdown(verification), encoding="utf-8", newline="\n"
        )
        print(f"отчёт проверки: {args.note_out}")
    return code


def run(args: argparse.Namespace) -> int:
    if getattr(args, "verify_manifest", None):
        return _run_verify(args)

    if not args.bin:
        print("нужен --bin (или --verify-manifest для проверки выпущенного файла)",
              file=sys.stderr)
        return 2
    image = Path(args.bin).read_bytes()
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    popcorn = (
        json.loads(Path(args.popcorn).read_text(encoding="utf-8")) if args.popcorn else None
    )
    result = build(
        args.stage or request.get("stage", "stage1"),
        image,
        request,
        popcorn_profile=popcorn,
        source_name=Path(args.bin).name,
    )
    if args.out:
        # Имя выпущенного файла — в манифест: проверка найдёт файл рядом с ним.
        result.manifest["result_name"] = Path(args.out).name
    manifest = json.dumps(result.manifest, ensure_ascii=False, indent=2)
    note = build_note(result, Path(args.bin).name)
    print(
        f"{result.stage}: {result.status} | калибровка "
        f"{result.cal_version or 'не прочитана'}"
    )
    for item in result.packages:
        print(f"  {item.id:12s} {item.status:14s} {item.detail[:70]}")
    for finding in result.findings:
        print(f"  {finding['severity'].upper():7s} {finding['code']}: {finding['message']}")
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(manifest + "\n", encoding="utf-8")
        print(f"манифест: {args.report}")
    if args.note:
        Path(args.note).parent.mkdir(parents=True, exist_ok=True)
        Path(args.note).write_text(note, encoding="utf-8")
        print(f"наряд: {args.note}")
    if result.status == "REJECTED":
        return 2
    if args.out and result.result:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_bytes(result.result.image)
        print(f"файл: {args.out} (sha256 {result.result.sha256[:16]}…)")
    return 0


def main(argv: list[str] | None = None) -> int:
    prepare_stdout()  # Windows: не падать на символах вне cp1251
    parser = argparse.ArgumentParser(description="Генератор Stage 1/2 для MS43.")
    parser.add_argument("--stage", choices=sorted(STAGE_PACKAGES))
    parser.add_argument("--bin", help="стоковый образ 512 КиБ")
    parser.add_argument("--request", help="JSON заявки клиента")
    parser.add_argument("--popcorn", help="JSON профиля хлопков")
    parser.add_argument("--out", help="куда записать собранный файл")
    parser.add_argument("--report", help="куда записать манифест (JSON)")
    parser.add_argument("--note", help="куда записать наряд (Markdown)")
    parser.add_argument("--verify-manifest",
                        help="проверить выпущенный файл по манифесту (JSON)")
    parser.add_argument("--image", help="файл для проверки по манифесту "
                                       "(иначе ищется рядом с манифестом)")
    parser.add_argument("--source-dir", help="где искать стоковый исходник "
                                             "для сверки «ничего лишнего»")
    parser.add_argument("--note-out", help="куда записать отчёт проверки (Markdown)")
    args = parser.parse_args(argv)
    if not args.verify_manifest and not (args.bin and args.request):
        parser.error("нужны --bin и --request (или --verify-manifest)")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
