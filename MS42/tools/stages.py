#!/usr/bin/env python3
"""Build Stage 1 and Stage 2 calibration files for MS42 - honestly.

A stage file is not "a tune with more power". It is a bundle of packages, where
each package is either written with values that have a documented source, or is
reported as waiting for data. The generator never fills that gap with guesses:

Stage 1 (stock hardware)
  * ``limits``            - speed limiter, engine speed limiter, overrun cut
                            hysteresis, from the customer request (written);
  * ``octane``            - fuel profile RON91/95/98 (``"octane": "ron98"`` or an
                            ``octane`` intent). RON98 allows an ignition addition
                            (through the ignition package and its knock log);
                            RON95/RON91 keep the stock maps and only report it;
  * ``overrun_pops``      - written from a customer popcorn profile, but only with a
                            written "no catalyst" statement, a thermal acknowledgement and
                            a road validation plan (`--popcorn`);
  * ``idle``              - needs an idle log and the idle PWM tables (waiting);
  * ``ignition``/``fueling`` - needs a dyno and a knock/wideband log (waiting).

Stage 2 (hardware combination is required, and is recorded in the manifest)
  * ``injectors``         - written from a documented injector profile: the
                            pulse-width maps are multiplied by the flow ratio,
                            the dead-time table takes the new latency curve and
                            the fuel-consumption factor is divided by the same
                            ratio (MS4X "Aftermarket Injector Scaling");
  * ``maf``               - written from a documented sensor curve file;
  * ``intake``            - written from a documented MS43 donor calibration: the
                            DISA maps and the MAF diagnosis threshold are transferred
                            through `tools/donor.py`, which validates the donor base
                            offset, the shapes and the scales before writing;
  * ``cams``/``exhaust``  - waiting for a measured cam comparison and an off-road
                            declaration.

The hardware dictionary in the request is part of the manifest: a Stage 2 file
is keyed to one specific combination (ECU dataset id + engine + the actual
parts), not to a name. Without it, Stage 2 is refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    from MS42.tools import calibration as calibration_module
    from MS42.tools import generate_patches as generator_module
    from MS42.tools.analyze_firmware import analyze
    from MS42.tools.calibration import (
        DEFAULT_POLICY,
        DEFAULT_XDF,
        PatchError,
        Policy,
        load_calibration,
    )
    from MS42.tools import donor as donor_module
    from MS42.tools import popcorn as popcorn_module
    from MS42.tools.delivery_note import build_note
    from MS42.tools.generate_patches import EVIDENCE_REQUIREMENTS, generate
    from MS42.tools.xdf import XdfError, get_object, load_definition, read_engineering
except ModuleNotFoundError:  # Direct execution from MS42/tools.
    import calibration as calibration_module  # type: ignore[no-redef]
    import generate_patches as generator_module  # type: ignore[no-redef]
    from analyze_firmware import analyze  # type: ignore[no-redef]
    from calibration import (  # type: ignore[no-redef]
        DEFAULT_POLICY,
        DEFAULT_XDF,
        PatchError,
        Policy,
        load_calibration,
    )
    import donor as donor_module  # type: ignore[no-redef]
    import popcorn as popcorn_module  # type: ignore[no-redef]
    from delivery_note import build_note  # type: ignore[no-redef]
    from generate_patches import EVIDENCE_REQUIREMENTS, generate  # type: ignore[no-redef]
    from xdf import (  # type: ignore[no-redef]
        XdfError,
        get_object,
        load_definition,
        read_engineering,
    )

ROOT = Path(__file__).parents[1]
REPO_ROOT_DIR = Path(__file__).resolve().parents[2]
if str(REPO_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_DIR))
# Общие ворота варианта 1 (docs/TASK_VARIANT1_RU.md): профиль топлива RON91/95/98,
# правовая справка и опции, которые честно ждут данных (docs/VARIANT1_WEB_ANALYSIS_RU.md
# §5/§6/§7/§8). Модуль ничего не знает про образы: здесь только выбор клиента и ворота.
from tools import variant1  # noqa: E402
from tools import ignition_layout  # noqa: E402
from tools import ignition_caps  # noqa: E402
from MS4x.tools import stages as common_stages  # noqa: E402

STATUS_WRITTEN = "WRITTEN"
STATUS_READY = "READY"
STATUS_EMPTY = "EMPTY"
STATUS_REQUIRES_DATA = "REQUIRES_DATA"
STATUS_BLOCKED = "BLOCKED"

#: MS4X "Aftermarket Injector Scaling": the pulse-width maps, never the
#: enrichment factors.
PULSE_WIDTH_MAPS: tuple[str, ...] = (
    "ip_tipr_cst__tco",
    "ip_ti_cst__n__tco",
    "ip_ti_tco_1_is_ivvt__n__maf",
    "ip_tib__n__maf",
    "ip_ti_tco_2_is_ivvt__n__maf",
    "ip_ti_tco_2_pl_ivvt_1__n__maf",
    "ip_ti_tco_2_pl_ivvt_2__n__maf",
    "ip_ti_slow_wf_thd_min__tco",
    "ip_ti_fast_wf_thd_min__tco",
)
DEAD_TIME_SYMBOL = "ip_ti_add_dly__vb"
CONSUMPTION_SYMBOL = "ip_fco_map_cor__pq_ main_col"
MAF_SYMBOL = "id_maf_tab"
MAF_SYMBOL_16X16 = "id_maf_tab__v_maf_1__v_maf_2"

#: The documented injector ratio is allowed to move within this band. Anything
#: further means the change is not an injector swap any more.
INJECTOR_FACTOR_MIN = 0.5
INJECTOR_FACTOR_MAX = 2.0


@dataclass(frozen=True)
class Package:
    id: str
    stage: int
    title: str
    kind: str          # "limits" | "hardware" | "evidence"
    summary: str
    needs: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()


PACKAGES: tuple[Package, ...] = (
    Package("limits", 1, "Ограничители", "limits",
            "Скорость, обороты и гистерезис отсечки из заявки клиента.",
            sources=("ms4x-ms42",),
            symbols=("c_vs_max_1", "c_vs_max_add", "c_n_min_vs_max",
                     "id_n_max_mt__nvs", "id_n_max_at__nvs",
                     "c_n_hys_max_puc", "c_n_hys_min_puc")),
    Package("overrun_pops", 1, "Хлопки на сбросе газа", "popcorn",
            "Порог отсечки, углы и задержка на сбросе под профиль клиента "
            "(light/medium). Пишутся только с письменным подтверждением, что "
            "катализатора нет, и планом дорожной проверки; агрессивный пресет "
            "не предлагается.",
            needs=("popcorn_profile", "road_validation_plan"),
            sources=("ms4x-ms42",),
            symbols=("ip_n_min_puc__tco", "ip_n_min_accin_puc__tco",
                     "ip_iga_puc__n__tco", "ip_iga_accin_puc__n__tco",
                     "ip_t_puc_deacc__n__maf_mmv")),
    Package("idle", 1, "Обороты холостого хода", "evidence",
            "Уставка холостого хода поднимается вместе со скважностью РХХ, "
            "иначе регулятор упирается в предел. Нужен лог холостого хода.",
            needs=("idle_log", "rpm_target"),
            sources=("ms4x-ms42",),
            symbols=("ip_n_sp_is__tco", "ip_isapwm__tco", "c_n_sp_lgrd_is")),
    Package("ignition", 1, "Зажигание (RON91/RON98)", "evidence",
            EVIDENCE_REQUIREMENTS["ignition"]["summary"],
            needs=("dyno_pull", "knock_log", "wideband_log"),
            sources=("ms4x-ms42",),
            symbols=("ip_iga_ron_91_pl_ivvt__n__maf", "ip_iga_ron_98_pl_ivvt__n__maf",
                     "ip_iga_tco_2_is_ivvt__n__maf")),
    Package("fueling", 1, "Топливоподача", "evidence",
            EVIDENCE_REQUIREMENTS["fueling"]["summary"],
            needs=("wideband_log", "fuel_trims", "injector_duty"),
            sources=("ms4x-ms42",),
            symbols=("ip_tib__n__maf", "ip_ti_tco_2_pl_ivvt_1__n__maf")),
    Package("injectors", 2, "Форсунки (масштабирование)", "hardware",
            "Карты длительности впрыска умножаются на отношение потоков, "
            "мёртвое время берётся из паспорта новых форсунок, расходомер "
            "приборки делится на тот же множитель.",
            needs=("injector_profile",),
            sources=("ms4x-ms42",),
            symbols=PULSE_WIDTH_MAPS + (DEAD_TIME_SYMBOL, CONSUMPTION_SYMBOL)),
    Package("maf", 2, "Кривая ДМРВ", "hardware",
            "Замена передаточной функции под документированный датчик; "
            "скейл MAF смещает нагрузку примерно в 30 таблицах.",
            needs=("documented_maf_curve",),
            sources=("ms4x-ms42", "hpacademy-maf"),
            symbols=(MAF_SYMBOL,)),
    Package("intake", 2, "Впуск / DISA / коллектор M54", "donor",
            "Карты DISA и порог диагностики ДМРВ переносятся из донорской "
            "калибровки MS43 (MS4X: карты M54 копируются 1:1). Инструмент "
            "проверяет базовый адрес донора, форму, шкалу и границы политики, "
            "а каждая ячейка несет донорскую провенанс-строку.",
            needs=("donor_calibration", "hardware_intake"),
            sources=("ms4x-b25-tuning", "ms4x-ms42"),
            symbols=("id_vim_fl__n_vim", "id_vim_pl__n_vim__maf",
                     "id_pvs_fl_vim__n_vim", "c_n_hys_vim", "c_maf_hys_vim",
                     "c_maf_max_diag")),
    Package("cams", 2, "Распредвалы / VANOS", "evidence",
            "Калибровка валов (длительность, диапазон, опорный угол) и уставки "
            "VANOS меняются только по измеренному сравнению фаз.",
            needs=("cam_specification", "measured_cam_comparison"),
            sources=("ms4x-ms42",),
            symbols=("c_cam_op_in", "c_cam_adj_rng_max_in", "c_cam_ini_in",
                     "ip_cam_sp_tco_2_in_pl__n__maf_ivvt")),
    # Опции варианта 1 (docs/TASK_VARIANT1_RU.md §1, веб-анализ §5–§8): профиль
    # топлива задаётся клиентом, остальное — честные REQUIRES_DATA с причинами.
    Package("octane", 1, "Октан-профиль (RON91/RON95/RON98)", "octane",
            "Профиль топлива выбирает клиент. По спецификации Stage 1 (раунд 4) "
            "право на добавку угла без логов: RON98 — до +3.0°, RON95 — до "
            "+1.5°, RON91 — без добавки; с логом детонации RON98/RON95 — до "
            "+5.0°. В MS42 карта зажигания по-прежнему пишется только пакетом "
            "ignition и только по логу (веб-анализ §7): профиль задаёт право, "
            "не байты.",
            needs=("octane_profile",),
            sources=("ms4x-ms42",),
            symbols=("ip_iga_ron_91_pl_ivvt__n__maf", "ip_iga_ron_98_pl_ivvt__n__maf",
                     "ip_iga_aj_ron91__n__maf", "ip_iga_aj_ron98__n__maf")),
    Package("disa_off", 1, "DISA off (фиксация заслонок + диагностика)", "option",
            variant1.option_why("disa_off"),
            needs=variant1.option_needs("disa_off"),
            sources=("ms4x-ms42", "e46fanatics-disa"),
            symbols=("id_vim_fl__n_vim", "id_vim_pl__n_vim__maf", "c_n_hys_vim",
                     "c_maf_hys_vim", "c_tco_min_disa", "c_abc_inc_vim",
                     "c_abc_max_vim")),
    Package("isa_off", 1, "ISA off / обход клапана холостого хода", "option",
            variant1.option_why("isa_off"),
            needs=variant1.option_needs("isa_off"),
            sources=("renovelo-icv", "e46fanatics-icv"),
            symbols=("ip_isapwm__tco", "ip_isapwm_n_sp_add_spi__n_dif_sif",
                     "ip_isapwm_tia_add__tia", "c_isapwm_isa_diag",
                     "c_dtc_isa_1", "c_dtc_isa_2", "c_dtc_isa_mec",
                     "c_crlc_isapwm_diagcps")),
    Package("fan_thermostat", 1, "Вентилятор и термостат (трек-режим)", "option",
            variant1.option_why("fan_thermostat"),
            needs=variant1.option_needs("fan_thermostat"),
            sources=("ms4x-ms42",),
            symbols=("ip_isapwm_ecf_add__ecfpwm", "ip_n_ecf__tco_ex",
                     "ip_n_ecf_temp_cat__temp_cat", "ip_n_ecf_vs_cor__vs",
                     "id_n_ecf_min_accin__n_ecf", "c_isapwm_lgrd_ecf",
                     "id_tco_sp_ect__n__maf_sub", "id_ectpwm_add__n__tco_sp",
                     "ip_ectpwm_i__tco_dif", "ip_ectpwm_p__tco_dif")),
    Package("exhaust", 2, "Выпуск / headers", "evidence",
            "Headers без катализатора требуют решения по задним зондам и "
            "вторичному воздуху. Подавление DTC мы не продаём, поэтому пакет "
            "остаётся в ожидании юридического решения клиента.",
            needs=("offroad_declaration", "dtc_policy_decision"),
            sources=("chaos-calibrations", "ms4x-ms42")),
)


def packages_for(stage: int) -> list[Package]:
    return [package for package in PACKAGES if package.stage <= stage]


# Opt-in packages are dispatched below because their writers need the shared
# MS4x parameter models rather than the legacy evidence-only catalogue.  Keep
# an explicit stage registry nevertheless: the API/site can enumerate them
# without accidentally enabling them in old request schemas.
STAGE_PACKAGES: dict[str, tuple[str, ...]] = {
    "stage1": tuple(package.id for package in PACKAGES)
    + ("ignition_stage1", "pedal_response", "vanos_stage1"),
    "stage2": tuple(package.id for package in PACKAGES)
    + ("ignition_stage1", "pedal_response", "vanos_stage1"),
}


# ---------------------------------------------------------------------------
# Documented hardware inputs


@dataclass
class InjectorProfile:
    stock_flow: float
    new_flow: float
    source: str
    dead_time_ms: list[float] | None = None
    notes: str = ""

    @property
    def factor(self) -> float:
        """Pulse-width multiplier: more flow needs a shorter pulse."""
        return self.stock_flow / self.new_flow

    def to_json(self) -> dict[str, Any]:
        return {
            "stock_flow": self.stock_flow,
            "new_flow": self.new_flow,
            "factor": round(self.factor, 6),
            "source": self.source,
            "dead_time_ms": self.dead_time_ms,
            "notes": self.notes,
        }


def load_injector_profile(path: Path) -> InjectorProfile:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if str(raw.get("kind", "")) != "injectors":
        raise PatchError(f"{path}: kind must be 'injectors'")
    stock = raw.get("stock_flow")
    new = raw.get("new_flow")
    for name, value in (("stock_flow", stock), ("new_flow", new)):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise PatchError(f"{path}: {name} must be a positive number")
    source = str(raw.get("source") or "").strip()
    if not source:
        raise PatchError(f"{path}: 'source' is required - a documented injector spec, "
                         "not a forum post")
    factor = float(stock) / float(new)
    if not INJECTOR_FACTOR_MIN <= factor <= INJECTOR_FACTOR_MAX:
        raise PatchError(
            f"{path}: flow ratio {factor:.3f} is outside "
            f"{INJECTOR_FACTOR_MIN}..{INJECTOR_FACTOR_MAX}; that is not an injector swap"
        )
    dead_time = raw.get("dead_time_ms")
    if dead_time is not None:
        if not isinstance(dead_time, list) or len(dead_time) != 8:
            raise PatchError(f"{path}: dead_time_ms must be a list of 8 values "
                             "(the stock KF_TI_ADD_TOTZ voltage axis)")
        for index, value in enumerate(dead_time):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise PatchError(f"{path}: dead_time_ms[{index}] is not numeric")
            if not 0 <= float(value) <= 8.0:
                raise PatchError(f"{path}: dead_time_ms[{index}] = {value} is outside 0..8 ms")
    return InjectorProfile(
        stock_flow=float(stock), new_flow=float(new),
        source=source,
        dead_time_ms=[float(value) for value in dead_time] if dead_time else None,
        notes=str(raw.get("notes", "")),
    )


@dataclass
class MafCurve:
    values: list[float]
    provenance: str
    notes: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "points": len(self.values),
            "first": self.values[0] if self.values else None,
            "last": self.values[-1] if self.values else None,
            "provenance": self.provenance,
            "notes": self.notes,
        }


def load_maf_curve(path: Path, expected_points: int = 256) -> MafCurve:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if str(raw.get("kind", "")) != "maf_curve":
        raise PatchError(f"{path}: kind must be 'maf_curve'")
    values = raw.get("values_kg_h")
    if not isinstance(values, list) or len(values) != expected_points:
        raise PatchError(f"{path}: values_kg_h must be a list of {expected_points} values")
    numbers: list[float] = []
    for index, value in enumerate(values):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise PatchError(f"{path}: values_kg_h[{index}] is not numeric")
        if not 0 <= float(value) <= 1023.98:
            raise PatchError(f"{path}: values_kg_h[{index}] = {value} is outside 0..1023.98 kg/h")
        numbers.append(float(value))
    for index, (left, right) in enumerate(zip(numbers, numbers[1:])):
        if right < left:
            raise PatchError(f"{path}: the curve must be non-decreasing; "
                             f"values_kg_h[{index}] > values_kg_h[{index + 1}]")
    provenance = str(raw.get("provenance") or "").strip()
    if not provenance.startswith("documented_curve:"):
        raise PatchError(f"{path}: provenance must start with 'documented_curve:' and name "
                         "the datasheet or bench measurement")
    return MafCurve(values=numbers, provenance=provenance, notes=str(raw.get("notes", "")))


# ---------------------------------------------------------------------------
# Change construction


def _table_changes(symbol: str, factor: float, image: bytes, offset: int,
                   objects: dict[str, list], evidence: str, reason: str,
                   hint: str) -> list[dict[str, Any]]:
    obj = get_object(objects, symbol)
    values = read_engineering(bytes(image[offset:offset + 0x8000]), obj)
    changes = []
    for index, value in enumerate(values):
        changes.append({
            "symbol": symbol,
            "index": index,
            "value": round(value * factor, 6),
            "evidence": evidence,
            "reason": reason,
            "requires_validation": True,
            "validation_hint": hint,
            "units": obj.units,
        })
    return changes


def injector_changes(profile: InjectorProfile, image: bytes, offset: int,
                     objects: dict[str, list]) -> list[dict[str, Any]]:
    factor = profile.factor
    evidence = f"documented_injector:{profile.source}"
    reason = (f"масштабирование форсунок: поток {profile.stock_flow:g} -> "
              f"{profile.new_flow:g}, множитель {factor:.4f}")
    hint = ("лог лямбда-интеграторов (цель ±5 %) и скважности форсунок "
            "(цель не выше 90 %) после прошивки")
    changes: list[dict[str, Any]] = []
    for symbol in PULSE_WIDTH_MAPS:
        changes.extend(_table_changes(symbol, factor, image, offset, objects,
                                      evidence, reason, hint))
    if profile.dead_time_ms is not None:
        for index, value in enumerate(profile.dead_time_ms):
            changes.append({
                "symbol": DEAD_TIME_SYMBOL, "index": index, "value": value,
                "evidence": evidence,
                "reason": "мёртвое время новых форсунок по паспорту",
                "requires_validation": True,
                "validation_hint": "проверить пуск и холостой ход; при необходимости "
                                   "поправить кривую по логам",
                "units": "ms",
            })
    changes.extend(_table_changes(CONSUMPTION_SYMBOL, 1.0 / factor, image, offset, objects,
                                  evidence,
                                  f"расходомер приборки: деление на множитель {factor:.4f}",
                                  "сверить показания расхода приборки"))
    return changes


def maf_curve_changes(curve: MafCurve, objects: dict[str, list]) -> list[dict[str, Any]]:
    evidence = f"documented_curve:{curve.provenance}"
    return [
        {
            "symbol": MAF_SYMBOL, "index": index, "value": value,
            "evidence": evidence,
            "reason": "кривая ДМРВ под документированный датчик",
            "requires_validation": True,
            "validation_hint": "логи нагрузки/топливных коррекций: смещение скейла "
                               "влияет на все нагрузочные таблицы",
            "units": "kg/h",
        }
        for index, value in enumerate(curve.values)
    ]


#: Намерения, которых у MS42 нет, и причина отказа. Молча игнорировать запрос
#: нельзя: клиент должен увидеть, почему опции нет именно на этой версии
#: (веб-анализ docs/VARIANT1_WEB_ANALYSIS_RU.md §8: патча launch control для
#: MS41/MS42 не существует; подавление DTC проект не продаёт).
UNAVAILABLE_INTENTS: dict[str, tuple[str, str]] = {
    "launch": (
        "launch control",
        "MS42: двухшагового launch control нет ни в калибровке, ни патчем "
        "(веб-анализ §8); таблицы лимиттера по передачам — это отдельное "
        "намерение launch_limiter, и оно требует лога",
    ),
    "launch_control": (
        "launch control",
        "MS42: двухшагового launch control нет ни в калибровке, ни патчем "
        "(веб-анализ §8); таблицы лимиттера по передачам — это отдельное "
        "намерение launch_limiter, и оно требует лога",
    ),
    "dtc": (
        "подавление DTC",
        "MS42: подавление кодов ошибок проект не выпускает (см. пакет exhaust: "
        "решение по задним зондам и СВП принимает клиент вне продукта)",
    ),
    "cat_delete": (
        "удаление катализатора",
        "MS42: удаление катализатора проект не выпускает; хлопки — отдельное "
        "намерение popcorn и только с подтверждением, что катализатора нет",
    ),
    "sap_delete": (
        "удаление СВП",
        "MS42: удаление системы вторичного воздуха проект не выпускает",
    ),
}


def _refuse_unavailable_intents(intents: list[Any], findings: list[dict[str, str]]) -> None:
    for intent in intents:
        if not isinstance(intent, dict):
            continue
        kind = str(intent.get("kind", ""))
        spec = UNAVAILABLE_INTENTS.get(kind)
        if spec is None:
            continue
        title, reason = spec
        findings.append(_finding(
            "INTENT_UNAVAILABLE", "reject",
            f"{intent.get('id') or kind}: {title} — {reason}",
        ))


# ---------------------------------------------------------------------------
# Stage assembly


@dataclass
class PackageResult:
    id: str
    stage: int
    title: str
    status: str
    summary: str
    changes: list[dict[str, Any]] = field(default_factory=list)
    requirements: dict[str, Any] = field(default_factory=dict)
    findings: list[dict[str, str]] = field(default_factory=list)
    sources: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()


@dataclass
class StageResult:
    stage: str
    stage_number: int
    status: str
    packages: list[PackageResult]
    patches: list[dict[str, Any]]
    hardware: dict[str, Any]
    source: str
    source_sha256: str
    output_sha256: str | None = None
    output: str | None = None
    dataset_id: str | None = None
    osid: str | None = None
    image_type: str | None = None
    policy_version: int | None = None
    core_plan_status: str | None = None
    value_bytes_changed: int | None = None
    crc_bytes_changed: int | None = None
    crc_records_recomputed: int | None = None
    crc_valid_after: bool | None = None
    unplanned_byte_changes: list[str] = field(default_factory=list)
    donor: dict[str, Any] | None = None
    #: Профиль топлива варианта 1 (RON91/95/98) или None, если не заявлен.
    octane: dict[str, Any] | None = None
    findings: list[dict[str, str]] = field(default_factory=list)

    def package(self, package_id: str) -> PackageResult | None:
        return next((item for item in self.packages if item.id == package_id), None)


def _finding(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def _requirements_for(kind: str, package: Package) -> dict[str, Any]:
    base = dict(EVIDENCE_REQUIREMENTS.get(kind, {}))
    if base:
        base.setdefault("package", package.id)
        return base
    return {
        "package": package.id,
        "summary": package.summary,
        "needs": list(package.needs),
        "symbols": list(package.symbols),
    }


def build(stage: str, bin_path: Path, request: dict[str, Any], policy: Policy, *,
          injectors: InjectorProfile | None = None, maf_curve: MafCurve | None = None,
          popcorn: popcorn_module.PopcornProfile | None = None,
          donor: donor_module.Donor | None = None,
          donor_map: Path = donor_module.DEFAULT_MAP,
          out: Path | None = None, customer: str | None = None) -> StageResult:
    stage = stage.lower().replace("stage", "stage")
    if stage not in ("stage1", "stage2"):
        raise PatchError(f"unknown stage {stage!r}; expected 'stage1' or 'stage2'")
    stage_number = int(stage[-1])

    image, offset, image_type, _address_base = load_calibration(bin_path)
    objects, xdf_problems = load_definition(DEFAULT_XDF)
    analysis = analyze(bin_path)

    hardware = request.get("hardware")
    if not isinstance(hardware, dict):
        hardware = {}
    findings: list[dict[str, str]] = []
    if stage_number == 2 and not hardware:
        findings.append(_finding(
            "HARDWARE_REQUIRED", "reject",
            "Stage 2 привязан к конкретной комбинации железа: заполните 'hardware' "
            "(впуск, форсунки, ДМРВ, выпуск, валы) — без него файл не собирается.",
        ))
        return StageResult(
            stage=stage, stage_number=stage_number, status=STATUS_BLOCKED,
            packages=[], patches=[], hardware=hardware, source=str(bin_path),
            source_sha256=analysis.sha256, dataset_id=analysis.dataset_id,
            osid=analysis.osid, image_type=image_type, policy_version=policy.version,
            findings=findings,
        )

    intents = request.get("intents") or []
    if not isinstance(intents, list):
        raise PatchError("'intents' must be a list")
    _refuse_unavailable_intents(intents, findings)
    octane_profile = variant1.octane_choice(request, intents, findings)
    variant1.enforce_octane(octane_profile, intents, findings)
    if any(item.get("severity") == "reject" for item in findings):
        # Профиль топлива или недоступное намерение — отказ до планирования:
        # ни один пакет не собирается и файл не выпускается (как HARDWARE_REQUIRED).
        return StageResult(
            stage=stage, stage_number=stage_number, status=STATUS_BLOCKED,
            packages=[], patches=[], hardware=hardware, source=str(bin_path),
            source_sha256=analysis.sha256, dataset_id=analysis.dataset_id,
            osid=analysis.osid, image_type=image_type, policy_version=policy.version,
            octane=variant1.octane_report(octane_profile), findings=findings,
        )
    writable_kinds = ("vmax", "rpm_limit", "hardcut")
    limit_intents = [intent for intent in intents
                     if isinstance(intent, dict) and intent.get("kind") in writable_kinds]
    other_intents = [intent for intent in intents
                     if isinstance(intent, dict) and intent.get("kind") not in writable_kinds]

    packages: list[PackageResult] = []
    patches: list[dict[str, Any]] = []
    donor_payload: dict[str, Any] | None = None

    for package in packages_for(stage_number):
        if package.kind == "limits":
            result = _limits_package(package, limit_intents, bin_path, policy)
            packages.append(result)
            patches.extend(result.changes)
            continue
        if package.id == "ignition" and any(
                item.get("kind") == "ignition_stage1"
                or (item.get("kind") == "ignition"
                    and str(item.get("profile", "")).lower() in {"stage1", "cautious"})
                for item in other_intents):
            result_package = _ms42_ignition_stage1_package(
                image, offset, objects, policy, other_intents, findings, octane_profile,
                allow_linked=bool(request.get("_allow_linked_ignition", True)),
            )
        elif package.kind == "popcorn":
            result_package = _popcorn_package(package, other_intents, popcorn, image, offset,
                                              objects)
        elif package.kind == "donor":
            result_package, donor_payload = _donor_package(
                package, donor, donor_map, image, offset, objects, policy, hardware)
        elif package.id == "injectors":
            result_package = _injectors_package(package, injectors, image, offset, objects)
        elif package.id == "maf":
            result_package = _maf_package(package, maf_curve, objects)
        elif package.kind == "octane":
            result_package = _octane_package(package, octane_profile, other_intents)
        elif package.kind == "option":
            result_package = _option_package(package, other_intents)
        else:
            result_package = _evidence_package(package, other_intents)
        packages.append(result_package)
        patches.extend(result_package.changes)

    # The new MS42-only packages are opt-in and intentionally absent from the
    # legacy package catalogue so old delivery-note schemas remain stable.
    if any(item.get("kind") == "pedal_response" for item in other_intents):
        result_package = _ms42_pedal_response_package(
            image, offset, objects, policy, other_intents, findings)
        packages.append(result_package)
        patches.extend(result_package.changes)
    if any(item.get("kind") == "vanos_stage1" for item in other_intents):
        result_package = _ms42_vanos_stage1_package(
            image, offset, objects, policy, other_intents, findings)
        packages.append(result_package)
        patches.extend(result_package.changes)

    if not patches and not any(item.status == STATUS_REQUIRES_DATA for item in packages):
        findings.append(_finding("NO_CHANGES", "info",
                                 "ни один пакет не дал изменений"))

    # Everything that writes goes through the real patch core.
    result = StageResult(
        stage=stage, stage_number=stage_number, status=STATUS_READY,
        packages=packages, patches=patches, hardware=hardware,
        source=str(bin_path), source_sha256=analysis.sha256,
        dataset_id=analysis.dataset_id, osid=analysis.osid, image_type=image_type,
        policy_version=policy.version, donor=donor_payload,
        octane=variant1.octane_report(octane_profile), findings=findings,
    )

    payload = [
        {
            "symbol": change["symbol"],
            "index": change["index"],
            "value": change["value"],
            "units": change.get("units"),
            "write_context": change.get("write_context"),
            "linked_to": change.get("linked_to"),
        }
        for change in patches
    ]
    if payload:
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            patch_file = directory_path / "patches.json"
            patch_file.write_text(json.dumps({
                "status": "READY", "stage": stage,
                "source": str(bin_path), "patches": payload,
            }, indent=2, ensure_ascii=False), encoding="utf-8")
            report_file = directory_path / "patch_report.json"
            output_file = (out or (directory_path / "stage.bin")) if out else None
            namespace = argparse.Namespace(
                bin=bin_path, patches=patch_file,
                out=output_file if out else None,
                report=report_file, policy=DEFAULT_POLICY, xdf=DEFAULT_XDF, json=False,
            )
            code = calibration_module.run(namespace)
            if report_file.exists():
                patch_report = json.loads(report_file.read_text(encoding="utf-8"))
                _apply_patch_report(result, patch_report)
            if code != 0 or result.core_plan_status == "REJECTED":
                result.status = STATUS_BLOCKED
                for package in result.packages:
                    if package.status in (STATUS_READY, STATUS_EMPTY):
                        package.status = STATUS_BLOCKED
                        package.findings.append(_finding(
                            "CORE_PLAN_REJECTED", "reject",
                            "ядро патчинга отклонило план; см. findings StageResult",
                        ))
                if out and out.exists():
                    out.unlink()
    else:
        result.core_plan_status = "EMPTY"
        for package in result.packages:
            if package.status == STATUS_READY:
                package.status = STATUS_EMPTY

    if any(package.status == STATUS_BLOCKED for package in result.packages):
        result.status = STATUS_BLOCKED
    elif result.core_plan_status == "EMPTY" and result.status == STATUS_READY:
        result.status = STATUS_EMPTY
    return result


def _ms42_values(image: bytes | bytearray, calibration_offset: int, obj) -> list[float]:
    size = max(1, obj.element_size_bits // 8)
    values: list[float] = []
    start = calibration_offset + obj.address
    for index in range(obj.rows * obj.columns):
        raw = int.from_bytes(
            image[start + index * size:start + (index + 1) * size],
            "little" if obj.lsb_first else "big",
        )
        values.append(obj.scaler.a * raw + obj.scaler.b)
    return values


def _ms42_stage_entry(policy: Policy, objects: dict[str, list], symbol: str,
                      findings: list[dict[str, str]], context: str):
    entry = policy.entry(symbol)
    if entry is None or symbol not in objects:
        findings.append(_finding(
            "SYMBOL_NOT_IN_POLICY", "reject",
            f"{context}: {symbol} не доказан в политике MS42 0110C6",
        ))
        return None, None
    try:
        obj = (get_object(objects, symbol) if entry.xdf_index is None
           else get_object(objects, symbol, entry.xdf_index))
    except (KeyError, XdfError, IndexError) as error:
        findings.append(_finding("SYMBOL_NOT_IN_XDF", "reject",
                                 f"{context}: {symbol}: {error}"))
        return None, None
    return entry, obj


def _ms42_ignition_stage1_package(
    image: bytes | bytearray, calibration_offset: int, objects: dict[str, list],
    policy: Policy, intents: list[dict[str, Any]], findings: list[dict[str, str]],
    octane_profile: str | None,
    allow_linked: bool = True,
) -> PackageResult:
    title = ("Зажигание Stage 1 (RON98 + связанная MBT)" if allow_linked
             else "Зажигание Stage 1 (RON98, мягкий публичный профиль без MBT)")
    asked = [item for item in intents
             if item.get("kind") == "ignition_stage1"
             or (item.get("kind") == "ignition"
                 and str(item.get("profile", "")).lower() in {"stage1", "cautious"})]
    if not asked:
        return PackageResult("ignition_stage1", 1, title, STATUS_REQUIRES_DATA,
                             "заявка не содержит явного ignition_stage1-профиля")
    intent = asked[0]
    try:
        delta = float(intent.get("delta_deg"))
    except (TypeError, ValueError):
        findings.append(_finding("REQUEST_INVALID", "reject",
                                 "ignition_stage1: delta_deg должен быть числом"))
        return PackageResult("ignition_stage1", 1, title, STATUS_BLOCKED,
                             "добавка не число")
    profile = str(octane_profile or intent.get("octane") or "ron98").lower()
    no_log_cap = ignition_caps.cap_no_log_deg(profile)
    log_cap = ignition_caps.cap_with_log_deg(profile) if allow_linked else no_log_cap
    if delta <= 0 or delta > log_cap + 1e-9:
        findings.append(_finding("REQUEST_INVALID", "reject",
                                 f"ignition_stage1: добавка должна быть 0..{log_cap:g}°"))
        return PackageResult("ignition_stage1", 1, title, STATUS_BLOCKED,
                             "добавка вне общего потолка")
    if delta > no_log_cap + 1e-9:
        return PackageResult(
            "ignition_stage1", 1, title, STATUS_REQUIRES_DATA,
            f"{delta:g}° выше Stage 1-потолка {no_log_cap:g}°: нужен проверенный "
            "лог детонации для расширенного профиля v2",
            requirements={"needs": ["server_analyzed_ignition_log"]},
        )
    ron_entry, ron_obj = _ms42_stage_entry(
        policy, objects, "ip_iga_ron_98_pl_ivvt__n__maf", findings, "ignition_stage1")
    axis_entry, axis_obj = _ms42_stage_entry(
        policy, objects, "ldpm_maf__iga_ron_91_pl_ivvt", findings, "ignition_stage1")
    rpm_entry, rpm_obj = _ms42_stage_entry(
        policy, objects, "ldpm_n__iga_ron_91_pl_ivvt", findings, "ignition_stage1")
    mbt_entry = mbt_obj = mbt_rpm_obj = mbt_load_obj = None
    if allow_linked:
        mbt_entry, mbt_obj = _ms42_stage_entry(
            policy, objects, "ip_iga_optm_tco_2__n__maf", findings, "ignition_stage1")
        try:
            # These are the real linked XDF axis objects for KF_ZW_BEST_TKW2;
            # they are deliberately read-only metadata, never patch targets.
            mbt_rpm_obj = get_object(objects, "ldpm_n_16_2")
            mbt_load_obj = get_object(objects, "ldpm_maf_12_1")
        except (XdfError, KeyError) as error:
            findings.append(_finding("SYMBOL_NOT_IN_XDF", "reject",
                                     f"ignition_stage1: MBT axes: {error}"))
            return PackageResult("ignition_stage1", 1, title, STATUS_BLOCKED,
                                 "MBT axes не доказаны")
    required = (ron_entry, ron_obj, axis_entry, axis_obj, rpm_entry, rpm_obj)
    if allow_linked:
        required += (mbt_entry, mbt_obj, mbt_rpm_obj, mbt_load_obj)
    if any(item is None for item in required):
        return PackageResult("ignition_stage1", 1, title, STATUS_BLOCKED,
                             "карта/оси не доказаны")

    stock_ron = _ms42_values(image, calibration_offset, ron_obj)
    load_axis = _ms42_values(image, calibration_offset, axis_obj)
    rpm_axis = _ms42_values(image, calibration_offset, rpm_obj)
    limit = min(delta, no_log_cap)
    ceiling_by_cell = None
    if not allow_linked:
        ceiling_by_cell = common_stages.public_ignition_caps(
            limit, rpm_axis, ron_obj.columns
        )
    additions, layout_note = ignition_layout.plan_additions(
        stock_ron, None, limit, load_axis,
        ron_obj.columns, ron_obj.rows, stage1=True,
        ceiling_by_cell=ceiling_by_cell,
    )
    ron_values = [ignition_layout.quantize_target_down(
        current + addition, current, abs(ron_obj.scaler.a))
        for current, addition in zip(stock_ron, additions)]

    stock_mbt: list[float] = []
    mbt_values: list[float] = []
    if allow_linked:
        stock_mbt = _ms42_values(image, calibration_offset, mbt_obj)
        mbt_rpm_axis = _ms42_values(image, calibration_offset, mbt_rpm_obj)
        mbt_load_axis = _ms42_values(image, calibration_offset, mbt_load_obj)
        # Interpolate the addition, rather than copying absolute angles.  The
        # two maps currently share axes, but this remains explicit for future XDFs.
        linked_additions = ignition_layout.interpolate_grid(
            additions, ron_obj.rows, ron_obj.columns,
            rpm_axis, load_axis, mbt_rpm_axis, mbt_load_axis,
        )
        for current, addition in zip(stock_mbt, linked_additions):
            target = current + max(0.0, addition)
            target = min(target, mbt_entry.maximum if mbt_entry.maximum is not None else target)
            quantum = abs(mbt_obj.scaler.a)
            target = ignition_layout.quantize_target_down(target, current, quantum)
            mbt_values.append(target)

    changes: list[dict[str, Any]] = []
    for index, value in enumerate(ron_values):
        changes.append({
            "symbol": "ip_iga_ron_98_pl_ivvt__n__maf", "index": index, "value": value,
            "write_context": None, "linked_to": None, "units": "degCRK",
            "reason": ("мягкий публичный профиль: общая shape/load-раскладка; "
                       "выше 3000 об/мин малый потолок" if not allow_linked
                       else "одна shape/load-раскладка; MBT связан с RON98"),
        })
    if allow_linked:
        for index, value in enumerate(mbt_values):
            changes.append({
                "symbol": "ip_iga_optm_tco_2__n__maf", "index": index, "value": value,
                "write_context": "ignition_optm_link",
                "linked_to": "ip_iga_ron_98_pl_ivvt__n__maf", "units": "degCRK",
                "reason": "одна shape/load-раскладка; MBT связан с RON98",
            })
    changed_ron = sum(a != b for a, b in zip(stock_ron, ron_values))
    changed_mbt = sum(a != b for a, b in zip(stock_mbt, mbt_values)) if allow_linked else 0
    linked_note = (f"MBT: {changed_mbt} связанных ячеек" if allow_linked
                   else "MBT не изменяется до снятия bench gate")
    return PackageResult(
        "ignition_stage1", 1, title, STATUS_READY,
        f"RON98: {changed_ron} ячеек; {linked_note}; "
        f"потолок +{min(delta, no_log_cap):g}°; {layout_note}; RON91 не трогается",
        changes=changes,
        requirements={"profile": profile,
                       "linked_to": "ip_iga_ron_98_pl_ivvt__n__maf" if allow_linked else None,
                       "rpm_axis_points": len(rpm_axis), "load_axis_points": len(load_axis)},
        symbols=(("ip_iga_ron_98_pl_ivvt__n__maf", "ip_iga_optm_tco_2__n__maf")
                 if allow_linked else ("ip_iga_ron_98_pl_ivvt__n__maf",)),
    )


def _ms42_pedal_response_package(
    image: bytes | bytearray, calibration_offset: int, objects: dict[str, list],
    policy: Policy, intents: list[dict[str, Any]], findings: list[dict[str, str]],
) -> PackageResult:
    title = "Ранний режим полной нагрузки (MS42, опция)"
    asked = [item for item in intents if item.get("kind") == "pedal_response"]
    if not asked:
        return PackageResult("pedal_response", 1, title, STATUS_EMPTY,
                             "по умолчанию пакет не включён")
    intent = asked[0]
    if not (intent.get("acknowledge_risk") or intent.get("acknowledge_pedal_risk")):
        findings.append(_finding("ACKNOWLEDGE_RISK_REQUIRED", "reject",
                                 "Отклик педали: нужно acknowledge_risk=true"))
        return PackageResult("pedal_response", 1, title, STATUS_BLOCKED,
                             "нет подтверждения риска")
    try:
        level = int(intent.get("level", 1))
        transmission = str(intent.get("transmission") or "automatic").lower()
        if level not in common_stages.SUPPORTED_PEDAL_LEVELS:
            raise ValueError("level должен быть 1, 2 или 3")
        if transmission not in {"manual", "mt", "automatic", "auto", "at"}:
            raise ValueError("transmission должен быть MT или AT")
    except (TypeError, ValueError) as error:
        findings.append(_finding("REQUEST_INVALID", "reject", f"Отклик педали: {error}"))
        return PackageResult("pedal_response", 1, title, STATUS_BLOCKED,
                             "параметры невалидны")
    acknowledged, missing_code = common_stages.pedal_risk_acknowledged(intent, level)
    if not acknowledged:
        message = ("для уровня 2/3 нужно отдельное acknowledge_level_risk=true"
                   if missing_code == "ACKNOWLEDGE_LEVEL_RISK_REQUIRED"
                   else "нужно acknowledge_risk=true")
        findings.append(_finding(missing_code or "ACKNOWLEDGE_RISK_REQUIRED", "reject",
                                 "Отклик педали: " + message))
        return PackageResult("pedal_response", 1, title, STATUS_BLOCKED,
                             "нет требуемого подтверждения риска")
    changes: list[dict[str, Any]] = []
    details: list[str] = []
    for symbol in ("id_pvs_fl__n", "id_pvs_fl_ivvt__n", "id_pvs_fl_vim__n_vim"):
        entry, obj = _ms42_stage_entry(policy, objects, symbol, findings, "pedal_response")
        if entry is None:
            continue
        stock = _ms42_values(image, calibration_offset, obj)
        raw_values = common_stages.pedal_threshold_model(
            stock, level, transmission, minimum=entry.minimum, maximum=entry.maximum
        )
        values = []
        for value in raw_values:
            bounded = max(float(entry.minimum if entry.minimum is not None else value), value)
            if entry.maximum is not None:
                bounded = min(float(entry.maximum), bounded)
            bounded = obj.scaler.a * round((bounded - obj.scaler.b) / obj.scaler.a) + obj.scaler.b
            if entry.minimum is not None:
                bounded = max(float(entry.minimum), bounded)
            if entry.maximum is not None:
                bounded = min(float(entry.maximum), bounded)
            values.append(bounded)
        for index, value in enumerate(values):
            if abs(value - stock[index]) > 1e-12:
                changes.append({"symbol": symbol, "index": index, "value": value,
                                "write_context": "pedal_response"})
        details.append(f"{symbol}: {len(values)} порогов, −{level * 10}% к стоку")
    if not changes:
        return PackageResult("pedal_response", 1, title, STATUS_EMPTY,
                             "уровень уже совпадает с картами")
    gearbox = "MT" if transmission in {"manual", "mt"} else "AT (мягко)"
    return PackageResult("pedal_response", 1, title, STATUS_READY,
                         f"уровень {level}/3, {gearbox}; модель момента (ip_tqi_*) не затронута; "
                         + ", ".join(details), changes=changes,
                         symbols=tuple(details))


def _ms42_vanos_stage1_package(
    image: bytes | bytearray, calibration_offset: int, objects: dict[str, list],
    policy: Policy, intents: list[dict[str, Any]], findings: list[dict[str, str]],
) -> PackageResult:
    title = "VANOS Stage 1 (MS42, прогретые карты)"
    asked = [item for item in intents if item.get("kind") == "vanos_stage1"]
    if not asked:
        return PackageResult("vanos_stage1", 1, title, STATUS_EMPTY,
                             "пакет не выбран")
    intent = asked[0]
    if not (intent.get("acknowledge_risk") or intent.get("acknowledge_vanos_risk")):
        findings.append(_finding("ACKNOWLEDGE_RISK_REQUIRED", "reject",
                                 "VANOS Stage 1: нужно acknowledge_risk=true"))
        return PackageResult("vanos_stage1", 1, title, STATUS_BLOCKED,
                             "нет подтверждения риска")
    try:
        shift_in, shift_ex = common_stages.cautious_vanos_shift(intent)
    except ValueError as error:
        findings.append(_finding("REQUEST_INVALID", "reject", f"VANOS Stage 1: {error}"))
        return PackageResult("vanos_stage1", 1, title, STATUS_BLOCKED,
                             "сдвиг невалиден")
    changes: list[dict[str, Any]] = []
    for symbol, shift in (("ip_cam_sp_tco_2_in_pl__n__maf_ivvt", shift_in),
                          ("ip_cam_sp_tco_2_ex_pl__n__maf_ivvt", shift_ex)):
        entry, obj = _ms42_stage_entry(policy, objects, symbol, findings, "vanos_stage1")
        if entry is None:
            continue
        stock = _ms42_values(image, calibration_offset, obj)
        for index, current in enumerate(stock):
            value = obj.scaler.a * round((current + shift - obj.scaler.b) / obj.scaler.a) + obj.scaler.b
            if abs(value - current) > 5.0 + 1e-9:
                findings.append(_finding("REQUEST_INVALID", "reject",
                                         f"{symbol}: квантованный сдвиг вышел за ±5°"))
                continue
            if value != current:
                changes.append({"symbol": symbol, "index": index, "value": value,
                                "write_context": "vanos_stage1"})
    if not changes:
        return PackageResult("vanos_stage1", 1, title, STATUS_EMPTY,
                             "сдвиг не лёг на сетку или карты уже изменены")
    return PackageResult("vanos_stage1", 1, title, STATUS_READY,
                         f"прогретые partial-load карты: впуск {shift_in:+g}°, "
                         f"выпуск {shift_ex:+g}°, stock ±5°; логовый профиль v2 отдельно",
                         changes=changes,
                         symbols=("ip_cam_sp_tco_2_in_pl__n__maf_ivvt",
                                  "ip_cam_sp_tco_2_ex_pl__n__maf_ivvt"))


def _limits_package(package: Package, intents: list[dict[str, Any]], bin_path: Path,
                    policy: Policy) -> PackageResult:
    if not intents:
        return PackageResult(
            id=package.id, stage=package.stage, title=package.title,
            status=STATUS_REQUIRES_DATA,
            summary="в заявке нет ни одного намерения по ограничителям",
            requirements={"package": package.id, "needs": ["vmax", "rpm_limit", "hardcut"],
                          "symbols": list(package.symbols)},
            sources=package.sources, symbols=package.symbols,
        )
    generation = generate({"intents": intents}, bin_path, policy)
    changes = list(generation.patches)
    findings = []
    for outcome in generation.outcomes:
        findings.extend(outcome.findings)
    summaries = [outcome.summary for outcome in generation.outcomes if outcome.summary]
    pending = [outcome for outcome in generation.outcomes
               if outcome.status == STATUS_REQUIRES_DATA]
    requirements: dict[str, Any] = {}
    if changes:
        status = STATUS_READY
        # A package that writes something must not hide the intents that still
        # wait for data: they are reported next to the written ones.
        for outcome in pending:
            findings.append(_finding(
                "LIMIT_INTENT_PENDING", "warning",
                f"{outcome.summary} Запрос не записан: {_requirement_text(outcome)}"))
    elif pending:
        # Nothing was written because the customer has to hand something over
        # (an acknowledgement, a plan): that is REQUIRES_DATA, not "nothing to do".
        status = STATUS_REQUIRES_DATA
        requirements = {
            "package": package.id,
            "pending": [
                {"intent": outcome.intent_id, "summary": outcome.summary,
                 "requirements": outcome.requirements}
                for outcome in pending
            ],
        }
    else:
        status = {
            "READY": STATUS_READY, "EMPTY": STATUS_EMPTY,
            "BLOCKED": STATUS_BLOCKED,
        }.get(generation.status, STATUS_BLOCKED)
    if not summaries:
        if pending:
            summary = "не записано, запросы ждут данных: " + ", ".join(
                outcome.intent_id for outcome in pending)
        else:
            summary = "изменений нет"
    else:
        summary = "; ".join(summaries)
    return PackageResult(
        id=package.id, stage=package.stage, title=package.title, status=status,
        summary=summary,
        changes=changes, findings=findings, requirements=requirements,
        sources=package.sources, symbols=package.symbols,
    )


def _requirement_text(outcome: Any) -> str:
    """One line naming what a pending intent still needs."""
    requirements = outcome.requirements or {}
    parts = [str(value) for key, value in requirements.items()
             if key in ("field", "note")]
    needs = requirements.get("needs") or []
    parts.extend(str(item) for item in needs)
    return "; ".join(parts) or "см. requirements"


def _injectors_package(package: Package, profile: InjectorProfile | None, image: bytes,
                       offset: int, objects: dict[str, list]) -> PackageResult:
    if profile is None:
        return PackageResult(
            id=package.id, stage=package.stage, title=package.title,
            status=STATUS_REQUIRES_DATA,
            summary="профиль форсунок не передан: масштабирование не выполняется",
            requirements={
                "package": package.id,
                "needs": list(package.needs),
                "fields": ["kind='injectors'", "stock_flow", "new_flow", "source",
                           "dead_time_ms (8 значений, необязательно)"],
                "rule": "карты длительности впрыска умножаются на отношение потоков; "
                        "обогатительные множители не трогаются",
                "after_flash": ["лямбда-интеграторы в пределах ±5 %",
                                "скважность форсунок не выше ~90 %",
                                "показания расхода топлива на приборке"],
                "symbols": list(package.symbols),
            },
            sources=package.sources, symbols=package.symbols,
        )
    changes = injector_changes(profile, image, offset, objects)
    return PackageResult(
        id=package.id, stage=package.stage, title=package.title, status=STATUS_READY,
        summary=(f"форсунки {profile.stock_flow:g} -> {profile.new_flow:g} "
                 f"(множитель {profile.factor:.4f}), {len(changes)} ячеек"),
        changes=changes, sources=package.sources, symbols=package.symbols,
        findings=[_finding("AFTER_FLASH_REQUIRED", "warning",
                           "после прошивки обязательны логи лямбда-интеграторов и скважности: "
                           "без них результат не подтверждён")],
    )


def _maf_package(package: Package, curve: MafCurve | None,
                 objects: dict[str, list]) -> PackageResult:
    if curve is None:
        return PackageResult(
            id=package.id, stage=package.stage, title=package.title,
            status=STATUS_REQUIRES_DATA,
            summary="документированная кривая ДМРВ не передана",
            requirements={
                "package": package.id,
                "needs": list(package.needs),
                "fields": ["kind='maf_curve'", "values_kg_h (256 значений)",
                           "provenance='documented_curve:<даташит или стенд>'"],
                "rule": "кривая неубывающая, 0..1023.98 кг/ч; правка скейла смещает "
                        "нагрузку примерно в 30 таблицах",
                "symbols": list(package.symbols),
            },
            sources=package.sources, symbols=package.symbols,
        )
    changes = maf_curve_changes(curve, objects)
    return PackageResult(
        id=package.id, stage=package.stage, title=package.title, status=STATUS_READY,
        summary=f"кривая ДМРВ: {len(changes)} точек, источник {curve.provenance}",
        changes=changes, sources=package.sources, symbols=package.symbols,
    )


def _intake_declared(hardware: dict[str, Any]) -> str | None:
    """Return the declared intake part, or None when the request does not say."""
    for key, value in hardware.items():
        if not isinstance(value, str):
            continue
        key_lower = key.lower()
        if ("intake" in key_lower or "впус" in key_lower or "disa" in key_lower) and value.strip():
            return value.strip()
    return None


def _popcorn_package(package: Package, intents: list[dict[str, Any]],
                     profile: popcorn_module.PopcornProfile | None, image: bytes,
                     offset: int, objects: dict[str, list]) -> PackageResult:
    requested = any(str(intent.get("kind")) == "popcorn" for intent in intents)
    if profile is None:
        requirements = popcorn_module.requirements("--popcorn")
        requirements["package"] = package.id
        requirements["needs"] = list(package.needs)
        return PackageResult(
            id=package.id, stage=package.stage, title=package.title,
            status=STATUS_REQUIRES_DATA,
            summary=("профиль хлопков не передан: без письменного подтверждения, что "
                     "катализатора нет, и плана дорожной проверки ничего не пишется"
                     if requested else package.summary),
            requirements=requirements,
            findings=[_finding("EVIDENCE_REQUIRED", "reject",
                               requirements["summary"])],
            sources=package.sources, symbols=package.symbols,
        )
    changes = popcorn_module.popcorn_changes(profile, image, offset, objects)
    achieved = popcorn_module.achieved_raises(profile, image, offset, objects)
    if not achieved:
        raise_text = ""
    elif achieved["min_rpm"] == achieved["max_rpm"]:
        raise_text = f"фактически +{achieved['max_rpm']:g} об/мин по сетке"
    else:
        raise_text = f"фактически +{achieved['min_rpm']:g}..+{achieved['max_rpm']:g} об/мин по сетке"
    return PackageResult(
        id=package.id, stage=package.stage, title=package.title, status=STATUS_READY,
        summary=(f"хлопки: интенсивность {profile.intensity}, порог "
                 f"+{profile.threshold_add_rpm:g} об/мин, угол {profile.retard_deg:g}°, "
                 f"задержка {profile.cut_delay_s:g} с, {len(changes)} ячеек"
                 + (f", {raise_text}" if raise_text else "")),
        changes=changes, sources=package.sources, symbols=package.symbols,
        requirements={
            "no_catalyst": profile.no_catalyst,
            "road_validation_plan": profile.road_validation_plan,
            "intensity": profile.intensity,
            "bands": {name: list(band) for name, band in popcorn_module.INTENSITY_BANDS.items()},
            "refused": list(popcorn_module.REFUSED_INTENSITY),
        },
        findings=[_finding(
            "NO_CATALYST_AND_ROAD_PLAN_RECORDED", "warning",
            f"в манифест записаны подтверждение «{profile.no_catalyst}» и план проверки; "
            f"первые заезды короткие, следить за температурой выпуска и охлаждающей жидкости")],
    )


def _donor_package(package: Package, donor: donor_module.Donor | None, map_path: Path,
                   image: bytes, offset: int, objects: dict[str, list], policy: Policy,
                   hardware: dict[str, Any]) -> tuple[PackageResult, dict[str, Any] | None]:
    declared = _intake_declared(hardware)
    if donor is None:
        requirements = {
            "package": package.id,
            "summary": "Донорская калибровка MS43 не подключена: перенос карт не выполняется.",
            "needs": list(package.needs),
            "how_to": ("передайте --donor-bin MS43/firmware/Siemens_MS43_MS430069_E46_M54B30_"
                       "EU4_LHD.bin (--donor-xdf подставится сам) и опишите впуск в "
                       "hardware, например hardware.intake = 'M54B30 впускной коллектор'"),
            "mapping": str(donor_module.DEFAULT_MAP),
            "symbols": list(package.symbols),
        }
        return PackageResult(
            id=package.id, stage=package.stage, title=package.title,
            status=STATUS_REQUIRES_DATA,
            summary="донорская калибровка MS43 не подключена", requirements=requirements,
            findings=[_finding("DONOR_REQUIRED", "reject", requirements["summary"])],
            sources=package.sources, symbols=package.symbols,
        ), None
    if declared is None:
        requirements = {
            "package": package.id,
            "summary": "Впуск не описан: перенос карт DISA привязан к конкретному коллектору.",
            "needs": ["hardware.intake или hardware.disa с названием коллектора"],
            "donor": donor_module.donor_payload(donor, [])["donor"],
            "symbols": list(package.symbols),
        }
        return PackageResult(
            id=package.id, stage=package.stage, title=package.title,
            status=STATUS_REQUIRES_DATA,
            summary="впуск не описан в hardware: неясно, под какой коллектор переносить",
            requirements=requirements,
            findings=[_finding("HARDWARE_INTAKE_REQUIRED", "reject", requirements["summary"])],
            sources=package.sources, symbols=package.symbols,
        ), donor_module.donor_payload(donor, [])

    include_references = bool(hardware.get("use_donor_references"))
    changes, reports = donor_module.plan_transfer(
        donor, objects, image, offset, policy, map_path=map_path,
        include_references=include_references,
    )
    payload = donor_module.donor_payload(donor, reports)
    payload["intake"] = declared
    payload["use_donor_references"] = include_references
    applied = [report["ms42_symbol"] for report in reports if report.get("applied")]
    refused = [report for report in reports if not report.get("applied")]
    findings: list[dict[str, str]] = []
    for report in refused:
        for problem in report["problems"]:
            findings.append(_finding("DONOR_TRANSFER_REFUSED", "warning",
                                     f"{report['ms42_symbol']}: {problem}"))
    if not changes:
        requirements = {
            "package": package.id,
            "summary": "ни один перенос не применился",
            "donor": payload["donor"],
            "transfers": reports,
        }
        return PackageResult(
            id=package.id, stage=package.stage, title=package.title,
            status=STATUS_REQUIRES_DATA,
            summary="донор подключен, но ни один перенос не прошёл проверки",
            requirements=requirements, findings=findings, sources=package.sources,
            symbols=package.symbols,
        ), payload
    findings.append(_finding(
        "DONOR_TRANSFER_APPLIED", "info",
        f"перенос с MS43 {payload['donor']['dataset_id']} ({payload['donor']['osid']}) для "
        f"впуска «{declared}»: " + ", ".join(applied)))
    return PackageResult(
        id=package.id, stage=package.stage, title=package.title, status=STATUS_READY,
        summary=(f"перенос с донора MS43 {payload['donor']['dataset_id']} "
                 f"({payload['donor']['osid']}) под впуск «{declared}»: "
                 f"{len(applied)} карт, {len(changes)} ячеек"),
        changes=changes, requirements={"donor": payload["donor"], "transfers": reports,
                                       "intake": declared},
        findings=findings, sources=package.sources, symbols=package.symbols,
    ), payload


def _octane_package(package: Package, profile: str | None,
                    intents: list[dict[str, Any]]) -> PackageResult:
    """Профиль топлива варианта 1: выбор клиента, а не пакет байтов.

    MS42 считает базу по двум картам (RON91/RON98) с интерполяцией по выученному
    октану (веб-анализ §7). Поэтому профиль ничего не пишет сам: RON98 разрешает
    добавку угла (её выдаёт пакет ignition и только с логом детонации), RON95 и
    RON91 оставляют карты стоковыми. Без выбранного профиля пакет честно ждёт
    решения клиента.
    """
    report = variant1.octane_report(profile)
    requested = [intent for intent in intents if str(intent.get("kind", "")) == "octane"]
    if profile is None:
        return PackageResult(
            id=package.id, stage=package.stage, title=package.title,
            status=STATUS_REQUIRES_DATA,
            summary=("профиль топлива не выбран: укажите RON91, RON95 или RON98 "
                     "(поле 'octane' или намерение с kind 'octane')"),
            requirements={
                "package": package.id,
                "needs": ["octane_profile"],
                "profiles": report["profiles"],
                "note": report["note_ru"],
            },
            findings=[_finding("OCTANE_PROFILE_REQUIRED", "info", report["note_ru"])],
            sources=package.sources, symbols=package.symbols,
        )
    finding_code = ("OCTANE_ADDITION_GATED" if report["add_allowed"]
                    else "OCTANE_STOCK_MAPS")
    summary = f"профиль {report['fuel']}: {report['note_ru']}"
    if requested:
        summary = f"запрошен клиентом ({requested[0].get('id')}): " + summary
    return PackageResult(
        id=package.id, stage=package.stage, title=package.title,
        status=STATUS_READY, summary=summary,
        requirements={
            "package": package.id,
            "profile": report["profile"],
            "fuel": report["fuel"],
            "add_allowed": report["add_allowed"],
            "profiles": report["profiles"],
        },
        findings=[_finding(finding_code, "info", report["note_ru"])],
        sources=package.sources, symbols=package.symbols,
    )


def _option_package(package: Package,
                    intents: list[dict[str, Any]]) -> PackageResult:
    """Опция варианта 1, которая пока честно ждёт данных.

    Формулировки (почему опции нет и что нужно) берутся из общего
    ``tools/variant1`` — они выписаны из веб-анализа §5/§6/§8, а не придуманы в
    генераторе. Опция без запроса остаётся в списке, но с пометкой «не
    запрошена»: клиент видит её состояние, а не «пакет исчез».
    """
    related = [intent for intent in intents
               if str(intent.get("kind", "")) == package.id]
    why = variant1.option_why(package.id) or package.summary
    needs = variant1.option_needs(package.id) or package.needs
    if related:
        ids = ", ".join(str(intent.get("id") or package.id) for intent in related)
        summary = f"запрошено клиентом ({ids}), но без данных не выпускается: {why}"
        findings = [_finding("OPTION_REQUIRES_DATA", "info",
                             f"{package.id}: " + "; ".join(needs))]
    else:
        summary = f"не запрошена; если понадобится — {why}"
        findings = []
    return PackageResult(
        id=package.id, stage=package.stage, title=package.title,
        status=STATUS_REQUIRES_DATA, summary=summary,
        requirements={"package": package.id, "needs": list(needs), "why": why,
                      "requested": bool(related)},
        findings=findings, sources=package.sources, symbols=package.symbols,
    )


def _evidence_package(package: Package, intents: list[dict[str, Any]]) -> PackageResult:
    related = [intent for intent in intents
               if str(intent.get("kind", "")) == package.id
               or (package.id == "overrun_pops" and intent.get("kind") == "popcorn")]
    requirements = _requirements_for(package.id
                                     if package.id != "overrun_pops" else "popcorn",
                                     package)
    if package.id == "overrun_pops":
        requirements["needs"] = list(package.needs)
    status = STATUS_REQUIRES_DATA
    summary = package.summary
    findings = [_finding("EVIDENCE_REQUIRED", "reject", requirements.get("summary", summary))]
    if related:
        summary = f"запрошено клиентом: {summary}"
    return PackageResult(
        id=package.id, stage=package.stage, title=package.title, status=status,
        summary=summary, requirements=requirements, findings=findings,
        sources=package.sources, symbols=package.symbols,
    )


def _apply_patch_report(result: StageResult, report: dict[str, Any]) -> None:
    result.core_plan_status = report.get("status")
    result.output = report.get("output")
    result.output_sha256 = report.get("output_sha256")
    result.value_bytes_changed = report.get("value_bytes_changed")
    result.crc_bytes_changed = report.get("crc_bytes_changed")
    result.crc_records_recomputed = report.get("crc_records_recomputed")
    result.crc_valid_after = report.get("crc_valid_after")
    result.unplanned_byte_changes = list(report.get("unplanned_byte_changes") or [])
    for finding in report.get("findings", []):
        result.findings.append({
            "code": str(finding.get("code", "CORE")),
            "severity": str(finding.get("severity", "info")),
            "message": str(finding.get("message", "")),
        })


# ---------------------------------------------------------------------------
# Reporting


def build_payload(result: StageResult) -> dict[str, Any]:
    return {
        "trust_level": variant1.TRUST_LEVEL_STATEMENT,
        "stage": result.stage,
        "stage_number": result.stage_number,
        "status": result.status,
        "source": result.source,
        "source_sha256": result.source_sha256,
        "output": result.output,
        "output_sha256": result.output_sha256,
        "dataset_id": result.dataset_id,
        "osid": result.osid,
        "image_type": result.image_type,
        "policy_version": result.policy_version,
        "hardware": result.hardware,
        "donor": result.donor,
        "octane": result.octane,
        "core_plan_status": result.core_plan_status,
        "value_bytes_changed": result.value_bytes_changed,
        "crc_bytes_changed": result.crc_bytes_changed,
        "total_bytes_changed": ((result.value_bytes_changed or 0)
                                + (result.crc_bytes_changed or 0)),
        "crc_records_recomputed": result.crc_records_recomputed,
        "crc_valid_after": result.crc_valid_after,
        "unplanned_byte_changes": result.unplanned_byte_changes,
        "findings": result.findings,
        "packages": [
            {
                "id": package.id,
                "stage": package.stage,
                "title": package.title,
                "status": package.status,
                "summary": package.summary,
                "requirements": package.requirements,
                "findings": package.findings,
                "sources": list(package.sources),
                "symbols": list(package.symbols),
                "changes": package.changes,
            }
            for package in result.packages
        ],
        "patches": result.patches,
    }


def generation_payload(result: StageResult) -> dict[str, Any]:
    """Shape the stage result like a generator report for the delivery note."""
    intents = []
    for package in result.packages:
        intents.append({
            "id": package.id,
            "kind": package.id,
            "status": package.status,
            "summary": package.summary,
            "findings": package.findings,
            "requirements": package.requirements,
            "changes": [
                {
                    "symbol": change["symbol"],
                    "index": change.get("index", 0),
                    "value": change["value"],
                    "evidence": change.get("evidence", ""),
                    "intent_kind": package.id,
                    "requires_validation": change.get("requires_validation", True),
                    "validation_hint": change.get("validation_hint", ""),
                }
                for change in package.changes
            ],
        })
    return {"status": result.status, "intents": intents}


def patch_payload(result: StageResult) -> dict[str, Any] | None:
    """Shape the stage result like a calibration report for the delivery note."""
    if not result.output or result.core_plan_status is None:
        return None
    return {
        "status": result.core_plan_status,
        "source": result.source,
        "output": result.output,
        "source_sha256": result.source_sha256,
        "output_sha256": result.output_sha256,
        "image_type": result.image_type,
        "value_bytes_changed": result.value_bytes_changed,
        "crc_bytes_changed": result.crc_bytes_changed,
        "total_bytes_changed": ((result.value_bytes_changed or 0)
                                + (result.crc_bytes_changed or 0)),
        "crc_records_recomputed": result.crc_records_recomputed,
        "crc_valid_after": result.crc_valid_after,
        "unplanned_byte_changes": result.unplanned_byte_changes,
        "changes": result.patches,
    }


def run(args: argparse.Namespace) -> int:
    policy = Policy.load(args.policy)
    request = json.loads(args.request.read_text(encoding="utf-8"))
    profile = load_injector_profile(args.injectors) if args.injectors else None
    curve = load_maf_curve(args.maf_curve) if args.maf_curve else None
    pop = popcorn_module.load_profile(args.popcorn) if args.popcorn else None
    if pop is None and isinstance(request.get("popcorn"), dict):
        pop = popcorn_module.parse_profile(request["popcorn"], "request.popcorn")
    donor = donor_module.load_donor(args.donor_bin, args.donor_xdf) if args.donor_bin else None
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
    result = build(args.stage, args.bin, request, policy, injectors=profile,
                   maf_curve=curve, popcorn=pop, donor=donor,
                   donor_map=args.donor_map, out=args.out, customer=args.customer)
    payload = build_payload(result)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                               encoding="utf-8")
    if args.note:
        note = build_note(generation_payload(result), patch_payload(result), args.customer)
        header = (f"<!-- {result.stage}: {result.status}, "
                  f"файл: {result.output or 'не собран'}, "
                  f"dataset: {result.dataset_id or '?'} -->\n\n")
        args.note.parent.mkdir(parents=True, exist_ok=True)
        args.note.write_text(header + note, encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"{payload['status']} {payload['stage']}: dataset={payload['dataset_id']} "
              f"osid={payload['osid']} image={payload['image_type']} "
              f"policy=v{payload['policy_version']}")
        octane = payload.get("octane") or {}
        if octane.get("fuel"):
            print(f"  октан-профиль: {octane['fuel']} — {octane.get('note_ru', '')}")
        for package in payload["packages"]:
            print(f"  [{package['status']:14s}] stage{package['stage']} {package['id']}: "
                  f"{package['summary']}")
        if payload["output"]:
            print(f"  file: {payload['output']}  "
                  f"value bytes={payload['value_bytes_changed']} "
                  f"crc bytes={payload['crc_bytes_changed']} "
                  f"crc valid after={payload['crc_valid_after']}")
        for finding in payload["findings"]:
            if finding["severity"] != "info":
                print(f"  [{finding['severity']}] {finding['code']}: {finding['message']}")
    if payload["status"] == STATUS_BLOCKED:
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("stage1", "stage2"))
    parser.add_argument("--bin", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True,
                        help="JSON with 'intents', optional 'hardware'")
    parser.add_argument("--injectors", type=Path, help="documented injector profile")
    parser.add_argument("--maf-curve", type=Path, dest="maf_curve",
                        help="documented MAF curve")
    parser.add_argument("--popcorn", type=Path,
                        help="customer popcorn profile (no-catalyst statement, bands, road plan)")
    parser.add_argument("--donor-bin", type=Path, dest="donor_bin",
                        default=None, help="MS43 donor calibration for the intake package")
    parser.add_argument("--donor-xdf", type=Path, dest="donor_xdf",
                        default=donor_module.DEFAULT_DONOR_XDF,
                        help="MS43 donor definition (defaults to the 512K Extended Load one)")
    parser.add_argument("--donor-map", type=Path, dest="donor_map",
                        default=donor_module.DEFAULT_MAP,
                        help="explicit MS42<-MS43 mapping catalogue")
    parser.add_argument("--out", type=Path, help="write the stage BIN here")
    parser.add_argument("--report", type=Path, help="write the manifest JSON here")
    parser.add_argument("--note", type=Path, help="write the delivery note here")
    parser.add_argument("--customer", default=None)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--json", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
