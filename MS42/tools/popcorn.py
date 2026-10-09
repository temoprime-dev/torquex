#!/usr/bin/env python3
"""Trailing-throttle pops for MS42: a profile with gates, not a preset.

MS42 gets its pops from three places (all of them already in the allowlist):

* ``ip_n_min_puc__tco`` / ``ip_n_min_accin_puc__tco`` - the engine speed below
  which the trailing-throttle fuel cut is active. Raising the threshold keeps the
  cut engaged longer, which is what fills the exhaust with unburnt mixture;
* ``ip_iga_puc__n__tco`` / ``ip_iga_accin_puc__n__tco`` - the ignition angle
  while the cut is active. Retarding it further delays combustion until the
  exhaust, which is what makes the bang;
* ``ip_t_puc_deacc__n__maf_mmv`` - the delay before the cut engages, in seconds.
  This is the "pop length" knob.

Every number comes from the customer's profile file (``--popcorn``); this module
never invents one. What it does enforce:

* a written statement that the car has no catalyst, and an explicit
  acknowledgement that the exhaust temperature will be watched;
* every target is snapped half-up to the object's raw grid before it is written,
  so a "+400 rpm" threshold lands on the 32 rpm step the ECU can actually store
  (a coarse grid is the reason a naive value would fail the patch core's
  quantization check);
* intensity bands - ``light`` and ``medium`` only. An ``aggressive`` public
  preset is refused by design, and the numbers must fit the band they claim, so
  a profile cannot smuggle an aggressive tune behind a modest name;
* bounds on top of the patch policy: the threshold may not be raised by more
  than 2000 rpm, the retard may not exceed -12 degCRK, the delay may not exceed
  3 s. All three keep the result inside something that can still be driven and
  validated on the road;
* the usual gates of the patch core: policy bounds, cell quantum, CRC and the
  post-application verification.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from MS42.tools.calibration import PatchError
    from MS42.tools.xdf import get_object, read_engineering
except ModuleNotFoundError:  # Direct execution from MS42/tools.
    from calibration import PatchError  # type: ignore[no-redef]
    from xdf import get_object, read_engineering  # type: ignore[no-redef]

#: Intensity bands: (max threshold raise in rpm, max retard in degCRK, max delay in s).
INTENSITY_BANDS: dict[str, tuple[float, float, float]] = {
    "light": (600.0, 6.0, 1.2),
    "medium": (2000.0, 12.0, 3.0),
}
#: Deliberately not offered: a public "aggressive" preset.
REFUSED_INTENSITY = ("aggressive", "extreme", "flames", "gunshots")

THRESHOLD_SYMBOLS = ("ip_n_min_puc__tco", "ip_n_min_accin_puc__tco")
IGNITION_SYMBOLS = ("ip_iga_puc__n__tco", "ip_iga_accin_puc__n__tco")
DELAY_SYMBOL = "ip_t_puc_deacc__n__maf_mmv"

MIN_NO_CATALYST_STATEMENT = 10
#: A road validation plan has to be a sentence, not a word.
MIN_ROAD_PLAN_CHARS = 20

#: The MS42 XDF mislabels the units of these two families: the overrun cut
#: thresholds are engine speeds (32 rpm per count) and the deacceleration table
#: is the pop delay documented in seconds by MS4X. The raw values and the scales
#: are used as the definition says; only the unit string in the report is fixed.
UNIT_OVERRIDES = {
    "ip_n_min_puc__tco": "[1/min]",
    "ip_n_min_accin_puc__tco": "[1/min]",
    "ip_t_puc_deacc__n__maf_mmv": "s",
}


@dataclass(frozen=True)
class PopcornProfile:
    no_catalyst: str
    thermal_watch: bool
    road_validation_plan: str
    intensity: str
    threshold_add_rpm: float
    retard_deg: float
    cut_delay_s: float
    notes: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "intensity": self.intensity,
            "threshold_add_rpm": self.threshold_add_rpm,
            "retard_deg": self.retard_deg,
            "cut_delay_s": self.cut_delay_s,
            "no_catalyst": self.no_catalyst,
            "thermal_watch": self.thermal_watch,
            "road_validation_plan": self.road_validation_plan,
            "notes": self.notes,
        }


def parse_profile(raw: dict[str, Any], where: str = "profile") -> PopcornProfile:
    """Validate an already-parsed popcorn profile (file or request intent)."""
    if str(raw.get("kind", "popcorn")) != "popcorn":
        raise PatchError(f"{where}: kind must be 'popcorn'")

    statement = str(raw.get("no_catalyst") or "").strip()
    if len(statement) < MIN_NO_CATALYST_STATEMENT:
        raise PatchError(
            f"{where}: 'no_catalyst' must be a written statement (at least "
            f"{MIN_NO_CATALYST_STATEMENT} characters) confirming the car has no catalyst - "
            f"pops destroy a catalyst and are off-road use only"
        )
    if raw.get("thermal_watch") is not True:
        raise PatchError(f"{where}: set \"thermal_watch\": true to accept watching exhaust and "
                         f"coolant temperature on the first runs")

    road_plan = str(raw.get("road_validation_plan") or "").strip()
    if len(road_plan) < MIN_ROAD_PLAN_CHARS:
        raise PatchError(
            f"{where}: 'road_validation_plan' must describe the first drive "
            f"(at least {MIN_ROAD_PLAN_CHARS} characters): which gears, how long, who watches "
            f"the temperatures and what the file is reverted to"
        )

    intensity = str(raw.get("intensity") or "").lower()
    if intensity in REFUSED_INTENSITY:
        raise PatchError(
            f"{where}: intensity {intensity!r} is refused: a public aggressive/flame preset is "
            f"not offered. Use 'light' or 'medium'."
        )
    if intensity not in INTENSITY_BANDS:
        raise PatchError(f"{where}: intensity must be one of {sorted(INTENSITY_BANDS)}, "
                         f"got {intensity!r}")

    numbers = {}
    for field_name in ("threshold_add_rpm", "retard_deg", "cut_delay_s"):
        value = raw.get(field_name)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise PatchError(f"{where}: {field_name} must be a number")
        numbers[field_name] = float(value)

    max_raise, max_retard, max_delay = INTENSITY_BANDS[intensity]
    if not 0 <= numbers["threshold_add_rpm"] <= max_raise:
        raise PatchError(
            f"{where}: threshold_add_rpm={numbers['threshold_add_rpm']:g} is outside the "
            f"{intensity!r} band (0..{max_raise:g}); raising the cut threshold by more than the "
            f"band means the cut engages above any usable overrun speed"
        )
    if not -max_retard <= numbers["retard_deg"] <= 0:
        raise PatchError(
            f"{where}: retard_deg={numbers['retard_deg']:g} is outside the {intensity!r} band "
            f"(-{max_retard:g}..0)"
        )
    if not 0 <= numbers["cut_delay_s"] <= max_delay:
        raise PatchError(
            f"{where}: cut_delay_s={numbers['cut_delay_s']:g} is outside the {intensity!r} band "
            f"(0..{max_delay:g} s)"
        )
    return PopcornProfile(
        no_catalyst=statement,
        thermal_watch=True,
        road_validation_plan=road_plan,
        intensity=intensity,
        threshold_add_rpm=numbers["threshold_add_rpm"],
        retard_deg=numbers["retard_deg"],
        cut_delay_s=numbers["cut_delay_s"],
        notes=str(raw.get("notes", "")),
    )


def load_profile(path: Path) -> PopcornProfile:
    return parse_profile(json.loads(path.read_text(encoding="utf-8")), str(path))


def snap_to_grid(value: float, step: float) -> float:
    """Round half-up to the raw grid the definition stores, so the write is exact.

    Half-up rather than Python's banker's rounding: a "+400 rpm" request must land
    on the same side of the grid for every cell, otherwise the same profile gives
    +384 here and +416 there for no reason a customer could see.
    """
    if not step or step <= 0:
        return value
    return round(math.floor(value / step + 0.5) * step, 9)


def popcorn_changes(profile: PopcornProfile, image: bytes, offset: int,
                    objects: dict[str, list]) -> list[dict[str, Any]]:
    """Turn a validated profile into patch changes, quoting the customer's statement."""
    calibration = bytes(image[offset:offset + 0x8000])
    evidence = f"customer_intent:no_catalyst={profile.no_catalyst}"
    reason = (f"хлопки ({profile.intensity}): порог отсечки +{profile.threshold_add_rpm:g} об/мин, "
              f"угол {profile.retard_deg:g}°, задержка {profile.cut_delay_s:g} с")
    changes: list[dict[str, Any]] = []

    for symbol in THRESHOLD_SYMBOLS:
        obj = get_object(objects, symbol)
        for index, value in enumerate(read_engineering(calibration, obj)):
            changes.append({
                "symbol": symbol, "index": index,
                "value": snap_to_grid(value + profile.threshold_add_rpm, obj.scaler.a),
                "evidence": evidence, "reason": reason,
                "requires_validation": True,
                "validation_hint": "лог STATUS_SCHUB_AB на сбросе газа: отсечка должна держаться, "
                                   "а не включаться/выключаться рывками",
                "units": UNIT_OVERRIDES.get(symbol, obj.units),
            })

    for symbol in IGNITION_SYMBOLS:
        obj = get_object(objects, symbol)
        for index, value in enumerate(read_engineering(calibration, obj)):
            changes.append({
                "symbol": symbol, "index": index,
                "value": snap_to_grid(value + profile.retard_deg, obj.scaler.a),
                "evidence": evidence, "reason": reason,
                "requires_validation": True,
                "validation_hint": "первые заезды короткие: следить за температурой выпуска и "
                                   "охлаждающей жидкости",
                "units": obj.units,
            })

    obj = get_object(objects, DELAY_SYMBOL)
    for index in range(obj.element_count):
        changes.append({
            "symbol": DELAY_SYMBOL, "index": index,
            "value": snap_to_grid(profile.cut_delay_s, obj.scaler.a),
            "evidence": evidence, "reason": reason,
            "requires_validation": True,
            "validation_hint": "длина хлопка: сначала короче, потом длиннее; следить за "
                               "температурой",
            "units": UNIT_OVERRIDES.get(DELAY_SYMBOL, obj.units),
        })
    return changes


def achieved_raises(profile: PopcornProfile, image: bytes, offset: int,
                    objects: dict[str, list]) -> dict[str, Any]:
    """What the snapped thresholds give per cell, so the report states the truth."""
    calibration = bytes(image[offset:offset + 0x8000])
    raises: list[float] = []
    for symbol in THRESHOLD_SYMBOLS:
        obj = get_object(objects, symbol)
        for value in read_engineering(calibration, obj):
            target = snap_to_grid(value + profile.threshold_add_rpm, obj.scaler.a)
            raises.append(round(target - value, 6))
    return {"min_rpm": min(raises), "max_rpm": max(raises)} if raises else {}


def requirements(where: str = "--popcorn") -> dict[str, Any]:
    """What a customer has to hand over before pops are written."""
    return {
        "kind": "popcorn",
        "summary": "Хлопки пишутся только по профилю клиента: письменное подтверждение, "
                   "что катализатора нет, и явное согласие следить за температурой.",
        "fields": [
            "kind = 'popcorn'",
            "no_catalyst = '<письменное подтверждение, катализатора нет>'",
            "thermal_watch = true",
            "road_validation_plan = '<план первых заездов>'",
            "intensity = 'light' | 'medium'  (aggressive не предлагается)",
            "threshold_add_rpm = 0..600 (light) / 0..2000 (medium)",
            "retard_deg = -6..0 (light) / -12..0 (medium)",
            "cut_delay_s = 0..1.2 (light) / 0..3.0 (medium)",
        ],
        "example": "MS42/examples/popcorn.example.json",
        "load_with": where,
        "after_flash": ["лог STATUS_SCHUB_AB на сбросе газа",
                        "температура выпуска и охлаждающей жидкости на первых заездах"],
        "refused": ["агрессивный публичный пресет", "хлопки на машине с катализатором"],
    }
