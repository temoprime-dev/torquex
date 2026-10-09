#!/usr/bin/env python3
"""Rescale the MS41 base injection map for documented replacement injectors.

Bigger injectors deliver more fuel per millisecond of opening time, so the ECU
must open them for less time for the same air mass. The base injection map
``ip_tib__n_maf`` (16x12 = 192 cells, milliseconds of injection time) is scaled
by one factor:

    factor = stock_flow / new_flow

Two independent estimates must agree before anything is planned:

* the factor from the documented flows (the hardware profile), and
* the factor from the logged long-term fuel trim (``1 + LTFT/100``), which is
  what the ECU itself had to correct with the new injectors fitted.

If they disagree by more than 10%, either the injector data or the log is wrong
and the tool refuses. With no logs at all it reports the factor, the number of
cells and the checks it would run, and stays REQUIRES_DATA - it never writes a
fuel map from a datasheet alone.

The minimum injection time (``c_ti_min_iv``), the cranking maps and the lambda
adaptation limits are deliberately *not* rescaled here: they need their own
evidence, and two of them are outside allowlist v1. The report names them as
outstanding work instead of quietly changing them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from MS41.tools.calibration import (
        DEFAULT_POLICY,
        PatchError,
        Policy,
        load_calibration,
        plan_patches,
    )
    from MS41.tools.log_evidence import Evidence, EvidenceError, load_evidence
    from MS41.tools.xdf import XdfError, get_object, load_definition, read_engineering
except ModuleNotFoundError:  # Direct execution from MS41/tools.
    from calibration import (
        DEFAULT_POLICY,
        PatchError,
        Policy,
        load_calibration,
        plan_patches,
    )
    from log_evidence import Evidence, EvidenceError, load_evidence
    from xdf import XdfError, get_object, load_definition, read_engineering

INJECTION_TITLE = "ip_tib__n__maf"
MIN_INJECTION_TITLE = "c_ti_min_iv"
CRANKING_TITLES = ("ip_ti_cst__n__tco", "ip_tipr_cst__tco")
ADAPTATION_TITLES = ("c_ti_ad_fac_max", "c_ti_ad_fac_min", "c_ti_ad_add_max")

#: How far the two estimates of the rescale factor may differ.
FACTOR_TOLERANCE = 0.10
#: A factor this close to 1.0 means there is nothing to rescale.
FACTOR_NEUTRAL = 0.005
#: Duty cycle limits, shared with the Stage 2 hardware profile.
DUTY_REJECT_PERCENT = 92.0
DUTY_WARN_PERCENT = 80.0
#: A short-term trim this large means the log was not taken at steady state.
STFT_WARN_PERCENT = 10.0

STATUS_READY = "READY"
STATUS_REQUIRES_DATA = "REQUIRES_DATA"
STATUS_EMPTY = "EMPTY"
STATUS_REJECTED = "REJECTED"


class ScalingError(ValueError):
    """Raised when the inputs cannot describe an injector change."""


@dataclass
class ScalingReport:
    status: str
    source_sha256: str = ""
    factor_from_flows: float | None = None
    factor_from_trims: float | None = None
    findings: list[dict[str, str]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    patches: list[dict[str, Any]] = field(default_factory=list)
    plan_status: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "status": self.status,
            "source_sha256": self.source_sha256,
            "factor_from_flows": (round(self.factor_from_flows, 6)
                                  if self.factor_from_flows is not None else None),
            "factor_from_trims": (round(self.factor_from_trims, 6)
                                  if self.factor_from_trims is not None else None),
            "plan_status": self.plan_status,
            "details": self.details,
            "findings": self.findings,
            "patches": self.patches,
        }


def _finding(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def _extend_findings(target: list[dict[str, str]],
                      findings: list[dict[str, str]]) -> None:
    """Copy findings, keeping one copy of each distinct code+message.

    The planner reports the same rejection once per requested cell, which is
    correct for a patch plan and unreadable in a report: a 192-cell table would
    otherwise carry 192 identical lines.
    """
    seen = {(item["code"], item["message"]) for item in target}
    for finding in findings:
        key = (finding["code"], finding["message"])
        if key in seen:
            continue
        seen.add(key)
        target.append(dict(finding))


def flows_from_profile(profile: dict[str, Any]) -> tuple[float, float]:
    """Return the (stock, replacement) injector flows in cc/min."""
    if not isinstance(profile, dict):
        raise ScalingError("the hardware profile must be a JSON object")
    kind = profile.get("injectors")
    if kind in (None, "stock"):
        raise ScalingError(
            "the hardware profile declares stock injectors, so there is no flow "
            "change to rescale for"
        )
    stock = profile.get("injector_stock_flow_cc_min")
    new = profile.get("injector_flow_cc_min")
    if not isinstance(stock, (int, float)) or not isinstance(new, (int, float)):
        raise ScalingError(
            "replacement injectors need both injector_flow_cc_min and "
            "injector_stock_flow_cc_min; a flow ratio cannot be guessed"
        )
    if stock <= 0 or new <= 0:
        raise ScalingError(f"injector flows must be positive, got {stock} and {new}")
    return float(stock), float(new)


def flow_factor(stock_flow: float, new_flow: float) -> float:
    """Injection-time factor for a flow change (more flow -> less time)."""
    return stock_flow / new_flow


def trims_factor(evidence: Evidence | None) -> float | None:
    """Rescale factor implied by the logged long-term fuel trim."""
    if evidence is None or evidence.is_stub:
        return None
    values = evidence.require("trims", "stft_percent", "ltft_percent")
    if values is None:
        return None
    return 1.0 + float(values["ltft_percent"]) / 100.0


def build_report(stock_path: Path, profile: dict[str, Any],
                 evidence: Evidence | None, policy: Policy,
                 symbol: str = INJECTION_TITLE) -> ScalingReport:
    stock_flow, new_flow = flows_from_profile(profile)
    factor = flow_factor(stock_flow, new_flow)

    objects, problems = load_definition()
    image, offset, image_type = load_calibration(stock_path)
    calibration = bytes(image[offset:offset + 0x6000])
    obj = get_object(objects, symbol)
    stock = read_engineering(calibration, obj)

    report = ScalingReport(
        status=STATUS_READY,
        source_sha256=hashlib.sha256(bytes(image)).hexdigest(),
        factor_from_flows=factor,
    )
    report.details = {
        "symbol": symbol,
        "address": f"0x{obj.address:04X}",
        "shape": f"{obj.rows}x{obj.columns}" if obj.rows and obj.columns else None,
        "cells": obj.element_count,
        "units": obj.units,
        "stock_flow_cc_min": stock_flow,
        "new_flow_cc_min": new_flow,
        "stock_time_ms": {"min": round(min(stock), 4), "max": round(max(stock), 4)},
    }
    for title in (MIN_INJECTION_TITLE, *CRANKING_TITLES, *ADAPTATION_TITLES):
        try:
            other = get_object(objects, title)
        except (PatchError, XdfError):
            continue
        values = read_engineering(calibration, other)
        if not values:
            continue
        entry: dict[str, Any] = {"units": other.units}
        if other.element_count == 1:
            entry["stock"] = round(values[0], 4)
        else:
            # a table is not summarised by its first cell
            entry.update({"cells": other.element_count,
                          "min": round(min(values), 4),
                          "max": round(max(values), 4)})
        report.details.setdefault("untouched", {})[title] = entry

    if abs(factor - 1.0) < FACTOR_NEUTRAL:
        report.status = STATUS_EMPTY
        report.findings.append(_finding(
            "FACTOR_NEUTRAL", "info",
            f"the declared flows differ by {abs(1 - factor) * 100:.2f}%, which is "
            "within 0.5% of stock; nothing is rescaled",
        ))
        return report

    report.details["new_time_ms"] = {"min": round(min(stock) * factor, 4),
                                     "max": round(max(stock) * factor, 4)}

    if evidence is None:
        report.status = STATUS_REQUIRES_DATA
        report.findings.append(_finding(
            "LOG_DATA_REQUIRED", "info",
            "no log evidence supplied: a fuel map is not written from a datasheet "
            "alone. Log steady-state idle and cruise long-term trims with the new "
            "injectors fitted (idle, cruise, one part-load pull) and one wideband "
            "pull, then supply --evidence",
        ))
        return report
    if evidence.is_stub:
        report.status = STATUS_REQUIRES_DATA
        report.findings.append(_finding(
            "LOG_DATA_REQUIRED", "info",
            f"the log evidence file is a stub ({evidence.source}), so the trims "
            "section carries no values yet; the factor stays unconfirmed",
        ))
        return report

    logged = evidence.require("trims", "stft_percent", "ltft_percent")
    if logged is None:
        report.status = STATUS_REQUIRES_DATA
        report.findings.append(_finding(
            "LOG_DATA_REQUIRED", "info",
            "the log evidence has no complete trims section (needs stft_percent and "
            "ltft_percent at steady state), so the flow-based factor is unconfirmed",
        ))
        return report

    factor_log = trims_factor(evidence)
    report.factor_from_trims = factor_log
    report.details["log_source"] = evidence.source
    report.details["logged_trims_percent"] = {
        "short_term": float(logged["stft_percent"]),
        "long_term": float(logged["ltft_percent"]),
    }

    if abs(float(logged["stft_percent"])) > STFT_WARN_PERCENT:
        report.findings.append(_finding(
            "SHORT_TERM_TRIM_HIGH", "warning",
            f"short-term trim is {float(logged['stft_percent']):+.1f}%; the log is not "
            "at steady state, so the long-term trim — and with it the whole check — "
            "is not trustworthy. Re-log a steady idle/cruise segment",
        ))

    divergence = abs(factor - factor_log) / abs(factor)
    report.details["factor_divergence_percent"] = round(divergence * 100, 3)
    if divergence > FACTOR_TOLERANCE:
        report.status = STATUS_REJECTED
        report.findings.append(_finding(
            "FACTOR_MISMATCH", "reject",
            f"the flows imply a factor of {factor:.4f} while the logged trims imply "
            f"{factor_log:.4f} — a {divergence * 100:.1f}% disagreement, above the "
            f"{FACTOR_TOLERANCE * 100:.0f}% limit. Either the injector data or the log "
            "is wrong; fix that before rescaling the fuel map",
        ))
        return report

    duty = evidence.require("injectors", "duty_percent_max", "maf_ceiling_kgh")
    if duty is None:
        report.findings.append(_finding(
            "DUTY_HEADROOM_UNKNOWN", "warning",
            "the log has no complete injectors section (duty_percent_max, "
            "maf_ceiling_kgh), so the duty-cycle headroom is checked by the Stage 2 "
            "profile only",
        ))
    else:
        percent = float(duty["duty_percent_max"])
        report.details["duty_percent_max"] = percent
        report.details["maf_ceiling_kgh"] = float(duty["maf_ceiling_kgh"])
        if percent >= DUTY_REJECT_PERCENT:
            report.status = STATUS_REJECTED
            report.findings.append(_finding(
                "DUTY_TOO_HIGH", "reject",
                f"logged injector duty is {percent:.1f}%; rescaling the fuel map does "
                f"not fix a duty cycle above {DUTY_REJECT_PERCENT:.0f}%, it needs a "
                "different injector or a lower airflow target",
            ))
            return report
        report.findings.append(_finding(
            "DUTY_HEADROOM", "info",
            f"logged duty {percent:.1f}% against the {DUTY_WARN_PERCENT:.0f}% "
            "planning limit",
        ))
        if percent >= DUTY_WARN_PERCENT:
            report.findings.append(_finding(
                "DUTY_HIGH", "warning",
                f"duty {percent:.1f}% leaves little margin at peak power; the rescale "
                "is valid but the injector is close to its limit",
            ))

    requests = [{"symbol": symbol, "index": index, "value": value * factor}
                for index, value in enumerate(stock)]
    plan = plan_patches(image, offset, image_type, objects, problems, requests, policy)
    report.plan_status = plan.status
    _extend_findings(report.findings, [
        _finding(finding.code, finding.severity, finding.message)
        for finding in plan.findings
    ])
    if plan.status != STATUS_READY:
        report.status = STATUS_REJECTED
        return report

    errors = [change.quantization_error_percent for change in plan.changes]
    deltas = [change.delta_percent for change in plan.changes
              if change.delta_percent is not None]
    report.details["planned_cells"] = len(plan.changes)
    report.details["max_quantization_error_percent"] = round(max(errors), 4) if errors else 0.0
    report.details["max_delta_percent"] = round(max(deltas, key=abs), 4) if deltas else None
    report.patches = [
        {
            "symbol": change.symbol,
            "index": change.index,
            "value": change.new_engineering,
            "reason": (
                f"документированные форсунки {profile.get('injector_part_number') or '?'}: "
                f"{stock_flow:g} -> {new_flow:g} см³/мин, коэффициент {factor:.4f} "
                "(подтверждён логом долгосрочных коррекций)"
            ),
            "evidence": (
                f"documented_injectors:{profile.get('injector_part_number') or 'flow ratio'}"
                f"+log_trims:{evidence.source}"
            ),
            "requires_validation": True,
            "validation_hint": (
                "log fuel trims and wideband lambda at idle, cruise and full load; "
                "long-term trims should sit near zero across the range, not just at "
                "idle"
            ),
        }
        for change in plan.changes
    ]

    report.findings.append(_finding(
        "MIN_INJECTION_TIME_REVIEW", "warning",
        f"{MIN_INJECTION_TITLE} is left at stock "
        f"({report.details.get('untouched', {}).get(MIN_INJECTION_TITLE, {}).get('stock')} ms). "
        "Injectors with a longer dead time need a different minimum time and, at "
        "small pulse widths, a different offset — check the small-load area after "
        "flashing",
    ))
    report.findings.append(_finding(
        "CRANKING_MAP_REVIEW", "warning",
        "the cranking maps (" + ", ".join(CRANKING_TITLES) + ") are not rescaled: "
        "cold start must be re-logged and rescaled separately by the same factor",
    ))
    report.findings.append(_finding(
        "ADAPTATION_LIMITS_KEPT", "info",
        "lambda adaptation limits (" + ", ".join(ADAPTATION_TITLES) + ") stay stock so "
        "the adaptation keeps its full authority while the base map is validated",
    ))
    return report


def run(args: argparse.Namespace) -> int:
    policy = Policy.load(args.policy)
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    evidence = None
    if args.evidence:
        try:
            evidence = load_evidence(args.evidence)
        except (EvidenceError, json.JSONDecodeError) as exc:
            raise SystemExit(f"error: {exc}")
    try:
        report = build_report(args.bin, profile, evidence, policy)
    except (ScalingError, PatchError, XdfError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    payload = {
        "version": 1,
        "status": report.status,
        "generator": "MS41/tools/injector_scaling.py",
        "generated_from_sha256": report.source_sha256,
        "patches": report.patches,
        "unfulfilled": [] if report.status == STATUS_READY else [
            {"id": "injectors", "status": report.status,
             "detail": "; ".join(f["message"] for f in report.findings) or None}
        ],
    }
    if args.out and report.status == STATUS_READY and report.patches:
        args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
    if args.report:
        args.report.write_text(json.dumps(report.to_json(), indent=2, ensure_ascii=False)
                               + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps({"report": report.to_json(), "patch_file": payload},
                         indent=2, ensure_ascii=False))
        return 1 if report.status == STATUS_REJECTED else 0

    details = report.details
    print(f"{report.status} {args.bin}")
    print(f"  factor from flows: {details.get('stock_flow_cc_min')} -> "
          f"{details.get('new_flow_cc_min')} cc/min = {report.factor_from_flows:.4f}")
    if report.factor_from_trims is not None:
        print(f"  factor from logged trims: {report.factor_from_trims:.4f} "
              f"(divergence {details.get('factor_divergence_percent')}%)")
    print(f"  {details.get('symbol')} @ {details.get('address')}: "
          f"{details.get('cells')} cells, {details.get('units')}, "
          f"{details.get('stock_time_ms')} ms -> {details.get('new_time_ms')} ms")
    if report.patches:
        print(f"  {len(report.patches)} cells planned; patch file: "
              f"{args.out or '(not written, use --out)'}")
    for finding in report.findings:
        print(f"  [{finding['severity'].upper()}] {finding['code']}: {finding['message']}")
    return 1 if report.status == STATUS_REJECTED else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rescale the MS41 base injection map for documented injectors.")
    parser.add_argument("--bin", type=Path, required=True,
                        help="stock binary the calibration is built on")
    parser.add_argument("--profile", type=Path, required=True,
                        help="Stage 2 hardware profile with documented injector flows")
    parser.add_argument("--evidence", type=Path, default=None,
                        help="log evidence file (a stub until real logs exist)")
    parser.add_argument("--out", type=Path, default=None,
                        help="write the patch file here (only when READY)")
    parser.add_argument("--report", type=Path, default=None,
                        help="write the full scaling report here")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--json", action="store_true", help="print JSON to stdout")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
