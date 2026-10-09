#!/usr/bin/env python3
"""Turn a customer request into a patch plan for the MS42 calibration core.

Same contract as the MS41 generator: a value only enters the plan when the
customer stated it or when it comes from a file this repository holds. The
planner is dry-run through ``calibration.py`` before a patch file is written, so
a generated plan cannot be rejected later by the core.

Intent kinds:

* ``vmax``        - vehicle speed limiter (soft ``c_vs_max_1`` / hard
                    ``c_vs_max_1 + c_vs_max_add``), optionally disabled outright;
* ``rpm_limit``   - engine speed limiter, soft and hard, manual or automatic
                    transmission tables, per detected gear;
* ``hardcut``     - trailing-throttle fuel cut hysteresis (the two overrun
                    hysteresis constants);
* ``ignition`` / ``fueling`` / ``popcorn`` / ``vanos`` / ``maf_curve`` /
  ``launch_limiter`` - no local data exists for these, so they return
  ``REQUIRES_DATA`` with a concrete requirements plan instead of guesses.

Nothing here is "just a number": raising the rpm limiter above stock, removing
the speed limiter and hardening the overrun cutoff all carry an
``acknowledge_risk`` gate and per-change ``requires_validation`` hints.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    from MS42.tools.calibration import (
        DEFAULT_POLICY,
        PatchError,
        Policy,
        load_calibration,
        plan_patches,
    )
    from MS42.tools.xdf import XdfError, get_object, load_definition
except ModuleNotFoundError:  # Direct execution from MS42/tools.
    from calibration import (  # type: ignore[no-redef]
        DEFAULT_POLICY,
        PatchError,
        Policy,
        load_calibration,
        plan_patches,
    )
    from xdf import XdfError, get_object, load_definition  # type: ignore[no-redef]

ROOT = Path(__file__).parents[1]
DEFAULT_XDF = ROOT / "definitions" / "Siemens_MS42_0110C6_ENG_512K_v1.3.xdf"

STATUS_READY = "READY"
STATUS_EMPTY = "EMPTY"
STATUS_REQUIRES_DATA = "REQUIRES_DATA"
STATUS_BLOCKED = "BLOCKED"

EVIDENCE_INTENT = "customer_intent"

#: Engine-speed limiter tables, per transmission. 1x3 tables indexed by the
#: gear-detection axis ldpm_nvs_1_3 (AT) / ldpm_nvs_1_3_2 (MT).
RPM_TABLES = {
    "manual": {"soft": "id_n_max_mt__nvs", "hard": "id_n_max_max_mt__nvs"},
    "auto": {"soft": "id_n_max_at__nvs", "hard": "id_n_max_max_at__nvs"},
}

#: Above this engine speed the stock oil pump is the known limiting part: the
#: aftermarket guidance (SLGECU: "do not raise rev limit above 6750 without an
#: oil-pump solution") is that the pump, not the valvetrain software, decides
#: whether a raise survives. The tables themselves are representable up to
#: 8160 rpm, so this is a data gate, not a raw-value limit.
OIL_PUMP_CEILING_RPM = 6750.0
OIL_PUMP_ACK = "acknowledge_oil_pump"


@dataclass(frozen=True)
class Rule:
    kind: str
    expand: "Callable[[dict[str, Any], Rule, dict[str, Any]], Outcome]"
    note: str = ""


@dataclass
class Outcome:
    intent_id: str
    kind: str
    status: str
    summary: str
    changes: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, str]] = field(default_factory=list)
    requirements: dict[str, Any] = field(default_factory=dict)


@dataclass
class GenerationResult:
    status: str
    source: str
    source_sha256: str | None
    image_type: str | None
    calibration_offset: int | None
    outcomes: list[Outcome]
    patches: list[dict[str, Any]]
    findings: list[dict[str, str]]
    policy_version: int
    core_plan_status: str | None = None


def _finding(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _change(symbol: str, value: float, intent: dict[str, Any], rule: Rule,
            index: int = 0, requires_validation: bool = True,
            validation_hint: str = "", evidence: str | None = None,
            units: str | None = None) -> dict[str, Any]:
    change: dict[str, Any] = {
        "symbol": symbol,
        "index": index,
        "value": value,
        "intent_id": str(intent.get("id") or rule.kind),
        "intent_kind": rule.kind,
        "evidence": evidence or EVIDENCE_INTENT,
        "requires_validation": requires_validation,
        "validation_hint": validation_hint,
    }
    if units:
        change["units"] = units
    return change


def _stock_engineering(bin_path: Path, symbol: str, index: int = 0) -> float:
    """Read the stock value of a symbol from the image being patched."""
    image, offset, _image_type, _base = load_calibration(bin_path)
    objects, _problems = load_definition(DEFAULT_XDF)
    obj = get_object(objects, symbol)
    step = obj.element_size_bytes
    raw = int.from_bytes(
        image[offset + obj.address + index * step:
              offset + obj.address + (index + 1) * step],
        "little" if obj.lsb_first else "big",
    )
    return obj.to_engineering(raw)


def _as_list(value: Any, length: int) -> list[Any] | None:
    if isinstance(value, list):
        if len(value) > length:
            return None
        return list(value) + [value[-1]] * (length - len(value))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [value] * length
    return None


def _expand_vmax(intent: dict[str, Any], rule: Rule, context: dict[str, Any]) -> Outcome:
    outcome = Outcome(str(intent.get("id") or "vmax"), rule.kind, STATUS_EMPTY, "")
    if intent.get("disable"):
        if not intent.get("acknowledge_risk"):
            outcome.status = STATUS_REQUIRES_DATA
            outcome.findings.append(_finding(
                "ACKNOWLEDGE_RISK_REQUIRED", "reject",
                "Disabling the vehicle speed limiter (c_n_min_vs_max = 8160 rpm) removes a "
                "factory protection. Set acknowledge_risk to accept it.",
            ))
            outcome.requirements = {
                "kind": "vmax_disable",
                "symbol": "c_n_min_vs_max",
                "stock_rpm": 800,
                "disable_value_rpm": 8160,
                "note": "8160 rpm (raw 255) is above any reachable engine speed, so the "
                        "limiter can never activate; this is the documented MS4X way to "
                        "turn the limiter off.",
            }
            return outcome
        outcome.changes.append(_change(
            "c_n_min_vs_max", 8160.0, intent, rule,
            requires_validation=True,
            validation_hint="confirm the limiter no longer engages and that no DTC was set "
                            "for implausible vehicle speed",
        ))
        outcome.status = STATUS_READY
        outcome.summary = "Vehicle speed limiter disabled (activation threshold raised out of reach)."
        return outcome

    soft = _number(intent.get("value_kmh"))
    if soft is None:
        outcome.status = STATUS_REQUIRES_DATA
        outcome.findings.append(_finding(
            "VALUE_REQUIRED", "reject",
            "vmax needs value_kmh (the soft limiter threshold) or disable=true.",
        ))
        outcome.requirements = {
            "kind": "vmax",
            "soft_symbol": "c_vs_max_1",
            "hard_symbol": "c_vs_max_add",
            "hard_threshold": "c_vs_max_1 + c_vs_max_add",
            "stock": "216 km/h soft on M52TUB20 images, 255 km/h elsewhere; +5.0 km/h hard "
                     "offset on every stock image",
            "note": "255 km/h is the cell maximum, so the factory value on most variants is "
                    "already the top of the scale.",
        }
        return outcome

    stock_soft = _stock_engineering(Path(context["bin"]), "c_vs_max_1")
    outcome.changes.append(_change(
        "c_vs_max_1", soft, intent, rule,
        validation_hint="log vehicle speed and check the cylinder-shutoff stage engages where "
                        "expected",
    ))
    hard_add = _number(intent.get("hard_add_kmh"))
    if hard_add is not None:
        outcome.changes.append(_change(
            "c_vs_max_add", hard_add, intent, rule,
            validation_hint="the hard stage cuts the fuel pump at c_vs_max_1 + c_vs_max_add; "
                            "verify the value is above the soft stage",
        ))
    if soft < stock_soft:
        outcome.findings.append(_finding(
            "LIMIT_LOWERED", "warning",
            f"the soft limit is being LOWERED from stock {stock_soft:.0f} km/h to "
            f"{soft:.0f} km/h; make sure the customer asked for a restriction, not for a "
            f"raise.",
        ))
    outcome.status = STATUS_READY
    outcome.summary = (
        f"Soft vehicle speed limit {stock_soft:.0f} -> {soft:.0f} km/h"
        + (f", hard stage offset {hard_add:g} km/h" if hard_add is not None else
           " (hard stage offset left stock)")
    )
    return outcome


def _expand_rpm_limit(intent: dict[str, Any], rule: Rule, context: dict[str, Any]) -> Outcome:
    outcome = Outcome(str(intent.get("id") or "rpm_limit"), rule.kind, STATUS_EMPTY, "")
    transmission = str(intent.get("transmission") or "").lower()
    if transmission not in RPM_TABLES:
        outcome.status = STATUS_BLOCKED
        outcome.findings.append(_finding(
            "TRANSMISSION_REQUIRED", "reject",
            "rpm_limit must state transmission: 'manual' or 'auto' - MS42 keeps separate "
            "soft/hard limiter tables for each gearbox and they are not interchangeable.",
        ))
        return outcome
    soft_values = _as_list(intent.get("soft_rpm"), 3)
    if not soft_values or _number(soft_values[0]) is None:
        outcome.status = STATUS_REQUIRES_DATA
        outcome.findings.append(_finding(
            "VALUE_REQUIRED", "reject",
            "rpm_limit needs soft_rpm (a number, or up to three values for gears 1..3).",
        ))
        return outcome
    hard_values = _as_list(intent.get("hard_rpm"), 3)
    if intent.get("hard_rpm") is not None and hard_values is None:
        outcome.status = STATUS_BLOCKED
        outcome.findings.append(_finding(
            "HARD_LIMIT_INVALID", "reject", "hard_rpm must be a number or a list of up to 3.",
        ))
        return outcome
    if hard_values is not None and any(_number(value) is None for value in hard_values):
        outcome.status = STATUS_BLOCKED
        outcome.findings.append(_finding(
            "HARD_LIMIT_INVALID", "reject", f"hard_rpm holds a non-numeric value: "
                                            f"{hard_values!r}",
        ))
        return outcome

    table = RPM_TABLES[transmission]
    stock_soft = [_stock_engineering(Path(context["bin"]), table["soft"], index)
                  for index in range(3)]
    stock_hard = [_stock_engineering(Path(context["bin"]), table["hard"], index)
                  for index in range(3)]
    raising = any(
        _number(value) > stock for value, stock in zip(soft_values, stock_soft)
    ) or (
        hard_values is not None
        and any(_number(value) > stock for value, stock in zip(hard_values, stock_hard))
    )
    if raising and not intent.get("acknowledge_risk"):
        outcome.status = STATUS_REQUIRES_DATA
        outcome.findings.append(_finding(
            "ACKNOWLEDGE_RISK_REQUIRED", "reject",
            f"Raising the engine speed limiter above stock (soft "
            f"{'/'.join(f'{v:.0f}' for v in stock_soft)} rpm, hard "
            f"{'/'.join(f'{v:.0f}' for v in stock_hard)} rpm for {transmission}) is a "
            f"deliberate decision: the valvetrain, oil pump and stock protection assume the "
            f"factory limit. Set acknowledge_risk to accept it.",
        ))
        outcome.requirements = {
            "kind": "rpm_limit",
            "transmission": transmission,
            "soft_symbol": table["soft"],
            "hard_symbol": table["hard"],
            "stock_rpm": {"soft": stock_soft, "hard": stock_hard},
            "axes": (
                "ldpm_nvs_1_3_2 (manual, 24/30/40 rpm per km/h) for gears 1..3; "
                "ldpm_nvs_1_3 (auto, 22/28/36)"
            ),
            "note": "soft limiting shuts cylinders down in the fuelcut pattern; the hard "
                    "limiter cuts all cylinders. Keep soft <= hard.",
        }
        return outcome

    # Validate every value before anything is written: a request that is wrong or
    # inconsistent must be reported as wrong, not as "needs another acknowledgement".
    for index, value in enumerate(soft_values):
        numeric = _number(value)
        if numeric is None:
            outcome.status = STATUS_BLOCKED
            outcome.findings.append(_finding(
                "VALUE_INVALID", "reject", f"soft_rpm[{index}] is not numeric: {value!r}",
            ))
            return outcome
        if hard_values is not None and numeric > (_number(hard_values[index]) or 0.0):
            outcome.status = STATUS_BLOCKED
            outcome.findings.append(_finding(
                "SOFT_ABOVE_HARD", "reject",
                f"gear {index + 1}: soft {numeric:.0f} rpm is above hard "
                f"{_number(hard_values[index]):.0f} rpm; the soft limiter would never be "
                f"reached.",
            ))
            return outcome

    above_ceiling = [
        (value, source)
        for source, values in (("soft_rpm", soft_values),
                               ("hard_rpm", hard_values or []))
        for value in values
        if _number(value) is not None and _number(value) > OIL_PUMP_CEILING_RPM
    ]
    if above_ceiling and not intent.get(OIL_PUMP_ACK):
        outcome.status = STATUS_REQUIRES_DATA
        unique = sorted({(source, _number(value)) for value, source in above_ceiling},
                        key=lambda item: (item[0], item[1]))
        requested = ", ".join(f"{source} {value:.0f}" for source, value in unique)
        outcome.findings.append(_finding(
            "HIGH_RPM_OIL_PUMP_ACK_REQUIRED", "reject",
            f"{requested} rpm is above {OIL_PUMP_CEILING_RPM:.0f} rpm. The stock oil pump is "
            f"the part that decides whether that survives; there is no measurement in this "
            f"repository that proves it does. The request must add "
            f"\"{OIL_PUMP_ACK}\": true together with a documented oil-pump or baffle solution.",
        ))
        outcome.requirements = {
            "kind": "rpm_limit",
            "transmission": transmission,
            "soft_symbol": table["soft"],
            "hard_symbol": table["hard"],
            "oil_pump_ceiling_rpm": OIL_PUMP_CEILING_RPM,
            "field": OIL_PUMP_ACK,
            "note": "keep the request at or below the ceiling unless the oil pump is dealt "
                    "with; keep soft <= hard",
            "sources": ["slgecu-ecu"],
        }
        return outcome

    for index, value in enumerate(soft_values):
        numeric = _number(value)
        outcome.changes.append(_change(
            table["soft"], numeric, intent, rule, index=index,
            validation_hint="log the limiter event: soft limiting must cut cylinders before "
                            "the hard limiter cuts all of them",
        ))
        if hard_values is not None:
            outcome.changes.append(_change(
                table["hard"], _number(hard_values[index]), intent, rule, index=index,
                validation_hint="hard cut engages here; verify no lean spike or misfire DTC",
            ))

    hysteresis = _number(intent.get("hysteresis_rpm"))
    if hysteresis is not None:
        outcome.changes.append(_change(
            "c_n_max_hys", hysteresis, intent, rule,
            validation_hint="hysteresis before injection is re-enabled; too small makes the "
                            "limiter chatter",
        ))
    hysteresis_max = _number(intent.get("hysteresis_max_rpm"))
    if hysteresis_max is not None:
        outcome.changes.append(_change(
            "c_n_max_hys_max", hysteresis_max, intent, rule,
            validation_hint="hysteresis before all injectors return; must be >= hysteresis_rpm",
        ))

    other = "auto" if transmission == "manual" else "manual"
    outcome.findings.append(_finding(
        "OTHER_GEARBOX_TABLES_UNTOUCHED", "info",
        f"{RPM_TABLES[other]['soft']} / {RPM_TABLES[other]['hard']} are left stock: this "
        f"request is for a {transmission} gearbox, and MS42 selects the pair by the coded "
        f"variant, not by the highest value.",
    ))
    if not raising:
        outcome.findings.append(_finding(
            "LIMIT_LOWERED_OR_KEPT", "warning",
            "the requested limiter is not above stock; this is a restriction, not a raise.",
        ))
    outcome.status = STATUS_READY
    outcome.summary = (
        f"{transmission} engine speed limiter: soft "
        f"{'/'.join(f'{_number(v):.0f}' for v in soft_values)} rpm, hard "
        + ("kept stock" if hard_values is None
           else '/'.join(f'{_number(v):.0f}' for v in hard_values) + " rpm")
    )
    return outcome


def _expand_hardcut(intent: dict[str, Any], rule: Rule, context: dict[str, Any]) -> Outcome:
    outcome = Outcome(str(intent.get("id") or "hardcut"), rule.kind, STATUS_EMPTY, "")
    upper = _number(intent.get("upper_rpm"))
    lower = _number(intent.get("lower_rpm"))
    if upper is None and lower is None:
        outcome.status = STATUS_REQUIRES_DATA
        outcome.findings.append(_finding(
            "VALUE_REQUIRED", "reject",
            "hardcut needs upper_rpm (c_n_hys_max_puc) and/or lower_rpm (c_n_hys_min_puc).",
        ))
        outcome.requirements = {
            "kind": "hardcut",
            "upper_symbol": "c_n_hys_max_puc",
            "lower_symbol": "c_n_hys_min_puc",
            "stock_rpm": {"upper": [640, 480], "lower": [128, 448]},
            "note": "Trailing-throttle (overrun) fuel cut hysteresis. Raising 'upper' makes "
                    "the cut re-engage sooner after a lift; lowering 'lower' makes it stay "
                    "engaged longer. Both interact with the popcorn tables.",
        }
        return outcome
    if upper is not None and lower is not None and lower > upper:
        outcome.status = STATUS_BLOCKED
        outcome.findings.append(_finding(
            "HYSTERESIS_INVERTED", "reject",
            f"lower_rpm {lower:.0f} is above upper_rpm {upper:.0f}; the two hysteresis "
            f"bands must not cross.",
        ))
        return outcome
    if upper is not None:
        outcome.changes.append(_change(
            "c_n_hys_max_puc", upper, intent, rule,
            validation_hint="log STATUS_SCHUB_AB / overrun state and confirm the cut "
                            "re-engages where expected",
        ))
    if lower is not None:
        outcome.changes.append(_change(
            "c_n_hys_min_puc", lower, intent, rule,
            validation_hint="log the overrun cut and confirm the engine picks up cleanly "
                            "when the throttle returns",
        ))
    outcome.status = STATUS_READY
    outcome.summary = "Trailing-throttle fuel cut hysteresis updated (no popcorn delay or " \
                      "ignition change is implied)."
    return outcome


def _expand_evidence_request(kind: str, intent: dict[str, Any], rule: Rule,
                             context: dict[str, Any]) -> Outcome:
    outcome = Outcome(str(intent.get("id") or kind), rule.kind, STATUS_REQUIRES_DATA, "")
    requirements = EVIDENCE_REQUIREMENTS[kind]
    outcome.requirements = dict(requirements)
    outcome.findings.append(_finding(
        "EVIDENCE_REQUIRED", "reject",
        requirements["summary"],
    ))
    outcome.summary = requirements["summary"]
    return outcome


#: Requirements plans for the intents whose values cannot come from this
#: repository. Every symbol below exists in the MS42 XDF (checked by a test).
EVIDENCE_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "ignition": {
        "kind": "ignition",
        "summary": "Ignition timing needs a dyno and a knock log; no values are written.",
        "tables": [
            {"symbol": "ip_iga_tco_1_is_ivvt__n__maf", "role": "cold idle target angle"},
            {"symbol": "ip_iga_tco_1_pl_ivvt__n__maf", "role": "cold part/full load"},
            {"symbol": "ip_iga_tco_2_is_ivvt__n__maf", "role": "warm idle target angle"},
            {"symbol": "ip_iga_ron_91_pl_ivvt__n__maf", "role": "RON91 base map"},
            {"symbol": "ip_iga_ron_98_pl_ivvt__n__maf", "role": "RON98 base map"},
            {"symbol": "id_iga_dec_knk_1__n", "role": "knock retard stage 1 (keep stock)"},
            {"symbol": "id_iga_dec_knk_2__n", "role": "knock retard stage 2 (keep stock)"},
        ],
        "log_evidence": ["knock retard per cylinder", "lambda integrators", "ignition angle"],
        "bench_steps": [
            "baseline dyno pull on the current calibration",
            "one change at a time, 2-3 dyno pulls apart",
            "knock retard must stay below the stock envelope on RON91 and RON98",
        ],
        "gates": ["EVIDENCE_REQUIRED", "TABLES_RESOLVED", "KNOCK_HEADROOM", "NOTHING_WRITTEN"],
        "not_supported": ["claimed power figures without a dyno sheet"],
    },
    "fueling": {
        "kind": "fueling",
        "summary": "Fuel maps need a wideband log; the stock lambda control stays authoritative.",
        "tables": [
            {"symbol": "ip_tib__n__maf", "role": "base injection time (VANOS fault + cold)"},
            {"symbol": "ip_ti_tco_2_pl_ivvt_1__n__maf", "role": "warm part load, bank 1"},
            {"symbol": "ip_ti_tco_2_pl_ivvt_2__n__maf", "role": "warm part load, bank 2"},
            {"symbol": "ip_ti_tco_1_is_ivvt__n__maf", "role": "cold idle"},
            {"symbol": "ip_ti_tco_2_is_ivvt__n__maf", "role": "warm idle"},
        ],
        "log_evidence": ["wideband lambda error", "long/short term trims", "injector duty"],
        "bench_steps": [
            "log trims on the stock calibration first",
            "scale the base injection map, never the enrichment factors",
            "verify duty cycle stays at or below 90% at the limiter",
        ],
        "gates": ["EVIDENCE_REQUIRED", "TRIMS_WITHIN_BAND", "DUTY_HEADROOM", "NOTHING_WRITTEN"],
        "not_supported": ["aftermarket injectors without documented flow and dead time"],
    },
    "popcorn": {
        "kind": "popcorn",
        "summary": "Exhaust pops need a written no-catalyst confirmation and a road validation "
                   "plan; a catalyst makes them unsafe.",
        "tables": [
            {"symbol": "ip_n_min_puc__tco", "role": "overrun cut threshold, AC off (6 columns)"},
            {"symbol": "ip_n_min_accin_puc__tco", "role": "overrun cut threshold, AC on"},
            {"symbol": "ip_iga_puc__n__tco", "role": "ignition during the cut"},
            {"symbol": "ip_iga_accin_puc__n__tco", "role": "ignition during the cut, AC on"},
            {"symbol": "ip_t_puc_deacc__n__maf_mmv", "role": "delay before the cut (pop length)"},
        ],
        "log_evidence": ["STATUS_SCHUB_AB_EIN during a lift off", "catalyst temperature if known"],
        "bench_steps": [
            "no catalyst: confirm in writing before any change",
            "shorten the pop (few ms) before lengthening it",
            "watch coolant and catalyst temperature for the first 20 minutes",
        ],
        "gates": ["NO_CATALYST_PROVEN", "THERMAL_LIMITS", "ROAD_PLAN", "BAND_LIMITS",
                  "NOTHING_WRITTEN_HERE"],
        "not_supported": ["popcorn on a car that still has a catalyst", "an aggressive public preset"],
        "how_to_write": "pops are written by stages.py: pass the profile with --popcorn; the "
                        "profile carries the written no-catalyst statement, the thermal "
                        "acknowledgement and the road validation plan",
    },
    "vanos": {
        "kind": "vanos",
        "summary": "VANOS setpoint changes need a measured cam comparison; none exists locally.",
        "tables": [
            {"symbol": "ip_cam_sp_tco_2_in_pl__n__maf_ivvt", "role": "intake, warm part load (16x12)"},
            {"symbol": "ip_cam_sp_tco_2_ex_pl__n__maf_ivvt", "role": "exhaust, warm part load (16x12)"},
            {"symbol": "ip_cam_sp_tco_2_in_is__n__maf_ivvt", "role": "intake, warm idle (6x6)"},
            {"symbol": "ip_cam_sp_tco_2_ex_is__n__maf_ivvt", "role": "exhaust, warm idle (6x6)"},
            {"symbol": "ip_cam_sp_tco_2_in_fl__n", "role": "intake, warm full load (16x1)"},
            {"symbol": "ip_cam_sp_tco_2_ex_fl__n", "role": "exhaust, warm full load (16x1)"},
        ],
        "log_evidence": ["commanded vs actual cam angle", "torque or airflow delta per setpoint"],
        "bench_steps": [
            "measure both cam positions at the target operating point",
            "change one cell family at a time",
            "verify no VANOS fault is latched",
        ],
        "gates": ["EVIDENCE_REQUIRED", "MEASURED_COMPARISON", "NOTHING_WRITTEN"],
        "not_supported": ["ghost cam / lumpy idle presets published without measurements"],
    },
    "maf_curve": {
        "kind": "maf_curve",
        "summary": "A MAF curve replacement needs a documented sensor curve; the stock curve "
                   "is read from the file and can be exported.",
        "tables": [
            {"symbol": "id_maf_tab", "role": "256-column voltage -> kg/h transfer function"},
        ],
        "log_evidence": ["sensor datasheet, or a measured bench curve with its provenance"],
        "bench_steps": [
            "record the exact sensor part number and its curve",
            "keep the curve non-decreasing and inside 0..1023.98 kg/h",
            "do not write through the '(MAF Hack 2048kg/h)' view without the program patch",
        ],
        "gates": ["EVIDENCE_REQUIRED", "CURVE_MONOTONIC", "NOTHING_WRITTEN"],
        "not_supported": ["a curve extrapolated from forum threads", "the MAF hack without the "
                          "matching program patch"],
    },
    "launch_limiter": {
        "kind": "launch_limiter",
        "summary": "A launch limiter is per-gear engine speed limitation; MS42 already has "
                   "per-gear limiter tables, but using them as a launch limiter changes how "
                   "the car behaves on the road.",
        "tables": [
            {"symbol": "id_n_max_mt__nvs", "role": "soft limiter, manual (gears 1..3)"},
            {"symbol": "id_n_max_max_mt__nvs", "role": "hard limiter, manual"},
            {"symbol": "id_n_max_cycnr__vs", "role": "cylinder shutoff escalation per speed"},
        ],
        "log_evidence": ["vehicle speed at launch", "which gear is detected"],
        "bench_steps": [
            "confirm the gearbox variant coded in the ECU",
            "change the soft limiter first and log the cylinder-shutoff behaviour",
            "never rely on the soft stage alone at full throttle",
        ],
        "gates": ["EVIDENCE_REQUIRED", "TRANSMISSION_MATCH", "NOTHING_WRITTEN"],
        "not_supported": ["a two-step launch limiter or ignition-cut limiter: stock MS42 has "
                          "neither"],
    },
}

RULES: dict[str, Rule] = {
    "vmax": Rule("vmax", _expand_vmax),
    "rpm_limit": Rule("rpm_limit", _expand_rpm_limit),
    "hardcut": Rule("hardcut", _expand_hardcut),
    "ignition": Rule("ignition",
                     lambda intent, rule, context: _expand_evidence_request("ignition", intent, rule, context)),
    "fueling": Rule("fueling",
                    lambda intent, rule, context: _expand_evidence_request("fueling", intent, rule, context)),
    "popcorn": Rule("popcorn",
                    lambda intent, rule, context: _expand_evidence_request("popcorn", intent, rule, context)),
    "vanos": Rule("vanos",
                  lambda intent, rule, context: _expand_evidence_request("vanos", intent, rule, context)),
    "maf_curve": Rule("maf_curve",
                      lambda intent, rule, context: _expand_evidence_request("maf_curve", intent, rule, context)),
    "launch_limiter": Rule(
        "launch_limiter",
        lambda intent, rule, context: _expand_evidence_request("launch_limiter", intent, rule, context),
    ),
}

IMPLEMENTED_KINDS = ("vmax", "rpm_limit", "hardcut")


def generate(request: dict[str, Any], bin_path: Path, policy: Policy) -> GenerationResult:
    intents = request.get("intents")
    if not isinstance(intents, list) or not intents:
        raise PatchError("request must contain a non-empty 'intents' list")

    image, offset, image_type, address_base = load_calibration(bin_path)
    objects, problems = load_definition(DEFAULT_XDF)
    context = {"bin": str(bin_path), "objects": objects}
    outcomes: list[Outcome] = []
    patches: list[dict[str, Any]] = []
    for intent in intents:
        if not isinstance(intent, dict):
            outcomes.append(Outcome("?", "?", STATUS_BLOCKED, "intent is not an object",
                                    findings=[_finding("REQUEST_INVALID", "reject",
                                                       f"intent is not an object: {intent!r}")]))
            continue
        kind = str(intent.get("kind") or "")
        rule = RULES.get(kind)
        if rule is None:
            outcomes.append(Outcome(
                str(intent.get("id") or kind), kind, STATUS_BLOCKED,
                f"unknown intent kind {kind!r}",
                findings=[_finding("INTENT_UNKNOWN", "reject",
                                   f"unknown intent kind {kind!r}; known kinds: "
                                   f"{sorted(RULES)}")],
            ))
            continue
        outcome = rule.expand(intent, rule, context)
        outcomes.append(outcome)
        patches.extend(outcome.changes)

    # Dry-run the collected changes through the real planner: a generated patch
    # file must never be rejected by the core it is written for.
    core_status: str | None = None
    findings: list[dict[str, str]] = []
    payload = [
        {"symbol": change["symbol"], "index": change["index"], "value": change["value"]}
        for change in patches
    ]
    if payload:
        plan = plan_patches(
            image, offset, address_base, image_type, objects, problems, payload, policy
        )
        core_status = plan.status
        findings.extend(asdict(finding) for finding in plan.findings)
        if plan.status == "REJECTED":
            for outcome in outcomes:
                if outcome.status == STATUS_READY:
                    outcome.status = STATUS_BLOCKED
                    outcome.findings.append(_finding(
                        "CORE_PLAN_REJECTED", "reject",
                        "the calibration core rejected this intent; see the generation findings",
                    ))
    else:
        core_status = "EMPTY"

    blocked = any(outcome.status == STATUS_BLOCKED for outcome in outcomes)
    ready = any(outcome.status == STATUS_READY for outcome in outcomes)
    if blocked:
        status = STATUS_BLOCKED
    elif ready:
        status = STATUS_READY
    else:
        status = STATUS_EMPTY

    return GenerationResult(
        status=status,
        source=str(bin_path),
        source_sha256=hashlib.sha256(bytes(image)).hexdigest(),
        image_type=image_type,
        calibration_offset=offset,
        outcomes=outcomes,
        patches=patches,
        findings=findings,
        policy_version=policy.version,
        core_plan_status=core_status,
    )


def build_payload(result: GenerationResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "source": result.source,
        "image_type": result.image_type,
        "calibration_offset": result.calibration_offset,
        "policy_version": result.policy_version,
        "core_plan_status": result.core_plan_status,
        "intents": [
            {
                "id": outcome.intent_id,
                "kind": outcome.kind,
                "status": outcome.status,
                "summary": outcome.summary,
                "findings": outcome.findings,
                "requirements": outcome.requirements,
                "changes": outcome.changes,
            }
            for outcome in result.outcomes
        ],
        "findings": result.findings,
        "patches": result.patches,
    }


def run(args: argparse.Namespace) -> int:
    policy = Policy.load(args.policy)
    request = json.loads(args.request.read_text(encoding="utf-8"))
    try:
        result = generate(request, args.bin, policy)
    except (PatchError, XdfError) as exc:
        raise SystemExit(f"error: {exc}")
    payload = build_payload(result)
    if args.out and payload["status"] == STATUS_READY and payload["patches"]:
        args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if args.report:
        args.report.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if args.json or not args.report:
        print(json.dumps(payload, indent=2))
    else:
        print(f"{payload['status']}: {len(payload['intents'])} intent(s), "
              f"{len(payload['patches'])} change(s), core plan "
              f"{payload['core_plan_status']}")
        for intent in payload["intents"]:
            print(f"  [{intent['status']}] {intent['kind']}: {intent['summary']}")
        for finding in payload["findings"]:
            if finding["severity"] != "info":
                print(f"  [{finding['severity']}] {finding['code']}: {finding['message']}")
    return 0 if payload["status"] != STATUS_BLOCKED else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--out", type=Path, help="write the patch file here when READY")
    parser.add_argument("--report", type=Path, help="write the full JSON report here")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
