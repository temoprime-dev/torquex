#!/usr/bin/env python3
"""Align the MS41 air-mass limits with a replacement MAF curve.

A new sensor with a higher ceiling only helps if the load limits move with it.
``c_maf_max`` ("Maximum air mass") and ``c_maf_max_diag`` ("Maximum air mass for
MAF sensor diagnosis: short circuit to battery") are load thresholds: if the
curve reads higher but the thresholds stay put, the extra range is unreachable
and the diagnosis trips earlier than before.

The rule here is deliberately scale-free: the limits are raised by exactly the
ratio by which the new curve's ceiling exceeds the stock one. The XDF does not
document how the sensor span in kg/h maps to these load thresholds in
mg/stroke, so no conversion is invented - keeping the *ratio* preserves the
stock margins, and the result is validated from logs afterwards.

Two gates before anything is planned:

* the log evidence must show the stock sensor was actually saturated
  (``maf.saturated = true`` with a credible ``voltage_max_v``), otherwise a
  higher ceiling is not proven to be needed;
* the ratio must stay inside the policy's delta bound for these symbols.

Everything else in the air-mass family is deliberately left alone and listed in
the report with the reason - catalyst-heating limits are thermal, adaptation
thresholds are calibration decisions, and idle-actuator limits have nothing to
do with the full-load span.
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
    from MS41.tools.maf_curve import (
        MAF_TITLE,
        Curve,
        CurveError,
        load_curve,
        resample,
    )
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
    from maf_curve import MAF_TITLE, Curve, CurveError, load_curve, resample
    from xdf import XdfError, get_object, load_definition, read_engineering

#: Limits that must follow the sensor's span, in the order they are planned.
LIMIT_TITLES = ("c_maf_max", "c_maf_max_diag")

#: Air-mass constants that are reviewed on every curve change and left stock.
REVIEWED: dict[str, str] = {
    "c_maf_max_ch_3": (
        "предел воздуха для подогрева катализатора — термический предел выхлопа, "
        "а не диапазон датчика"
    ),
    "c_maf_min_diag": (
        "нижний порог диагностики; верхняя часть диапазона датчика на него не влияет"
    ),
    "c_maf_lam_tol_tra": (
        "порог взвешивания коррекции впрыска — калибровочное решение, требует "
        "отдельного лога, а не пропорции"
    ),
    "c_maf_ti_ad_max": (
        "предел амбиентной лямбда-адаптации — задаёт, где адаптация выключается, "
        "менять вместе с датчиком нельзя"
    ),
    "c_maf_max_isa": "предел адаптации регулятора холостого хода",
    "c_maf_ad_isa": "воздух для адаптации холостого хода",
    "c_abc_max_maf": "лимит счётчика неисправностей датчика",
    "c_maf_dyw": "динамическое окно воздуха для лямбда-адаптации",
    "c_maf_hys_cop": "гистерезис защиты от перегрева катализатора",
}

#: A sensor claim of saturation below this voltage is not credible: the stock
#: grid runs to 5.018 V and the Stage 2 profile rejects anything from 4.7 V up.
SATURATION_MIN_VOLTS = 4.5

STATUS_READY = "READY"
STATUS_REQUIRES_DATA = "REQUIRES_DATA"
STATUS_EMPTY = "EMPTY"
STATUS_REJECTED = "REJECTED"


class LimitsError(ValueError):
    """Raised when the curve cannot be aligned with the limits at all."""


@dataclass
class LimitsReport:
    status: str
    ratio: float | None = None
    stock_ceiling_kgh: float | None = None
    new_ceiling_kgh: float | None = None
    source_sha256: str = ""
    plan_status: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    findings: list[dict[str, str]] = field(default_factory=list)
    patches: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "status": self.status,
            "source_sha256": self.source_sha256,
            "plan_status": self.plan_status,
            "ratio": round(self.ratio, 6) if self.ratio is not None else None,
            "stock_ceiling_kgh": (round(self.stock_ceiling_kgh, 4)
                                  if self.stock_ceiling_kgh is not None else None),
            "new_ceiling_kgh": (round(self.new_ceiling_kgh, 4)
                                if self.new_ceiling_kgh is not None else None),
            "details": self.details,
            "findings": self.findings,
            "patches": self.patches,
        }


def _finding(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def _extend_findings(target: list[dict[str, str]],
                     findings: list[dict[str, str]]) -> None:
    """Keep one copy of each distinct code+message (the planner repeats itself)."""
    seen = {(item["code"], item["message"]) for item in target}
    for finding in findings:
        key = (finding["code"], finding["message"])
        if key in seen:
            continue
        seen.add(key)
        target.append(dict(finding))


def ceiling_ratio(stock_path: Path, curve: Curve,
                  symbol: str = MAF_TITLE) -> tuple[float, list[float], list[float]]:
    """Resample the curve and return (ratio, stock values, requested values)."""
    objects, _ = load_definition()
    image, offset, _ = load_calibration(stock_path)
    obj = get_object(objects, symbol)
    grid = obj.axis_labels("y")
    if len(grid) != obj.element_count:
        raise LimitsError(
            f"{symbol}: the XDF declares {len(grid)} axis labels for "
            f"{obj.element_count} columns; the grid is incomplete"
        )
    stock = read_engineering(bytes(image[offset:offset + 0x6000]), obj)
    requested, _held = resample(curve, grid)
    stock_ceiling = max(stock)
    if stock_ceiling <= 0:
        raise LimitsError(f"{symbol}: the stock ceiling is {stock_ceiling} kg/h")
    return max(requested) / stock_ceiling, stock, requested


def saturation_proof(evidence: Evidence | None) -> tuple[dict[str, Any] | None, str]:
    """Return the maf section that proves saturation, or the reason it does not."""
    if evidence is None:
        return None, ("лога нет: без него не доказано, что стоковый датчик упёрся в "
                      "свой потолок и что новый диапазон вообще нужен")
    if evidence.is_stub:
        return None, (f"файл логов — заглушка ({evidence.source}), секция maf пуста: "
                      "насыщение стокового датчика не подтверждено")
    values = evidence.require("maf", "voltage_max_v", "saturated")
    if values is None:
        return None, ("в логе нет полной секции maf (нужны voltage_max_v и "
                      "saturated): насыщение стокового датчика не подтверждено")
    if values["saturated"] is not True:
        return None, ("лог говорит, что стоковый датчик не насыщался "
                      f"(saturated={values['saturated']!r}); рост потолка не обоснован")
    voltage = float(values["voltage_max_v"])
    if voltage < SATURATION_MIN_VOLTS:
        return None, (f"заявлено насыщение, но максимальное напряжение всего "
                      f"{voltage:.2f} В (сетка доходит до 5.018 В, порог проверки "
                      f"{SATURATION_MIN_VOLTS:.1f} В) — заявка неправдоподобна")
    return values, ""


def build_report(stock_path: Path, curve: Curve, evidence: Evidence | None,
                 policy: Policy, symbol: str = MAF_TITLE) -> LimitsReport:
    ratio, stock_values, requested = ceiling_ratio(stock_path, curve, symbol)
    objects, problems = load_definition()
    image, offset, image_type = load_calibration(stock_path)

    report = LimitsReport(
        status=STATUS_READY,
        ratio=ratio,
        stock_ceiling_kgh=max(stock_values),
        new_ceiling_kgh=max(requested),
        source_sha256=hashlib.sha256(bytes(image)).hexdigest(),
    )
    report.details = {
        "curve_sensor": curve.sensor,
        "curve_provenance": curve.provenance,
        "reviewed_not_changed": dict(REVIEWED),
    }

    if ratio <= 1.0 + 1e-9:
        report.status = STATUS_EMPTY
        report.findings.append(_finding(
            "CEILING_NOT_RAISED", "info",
            f"the new curve does not raise the ceiling ({max(stock_values):.1f} -> "
            f"{max(requested):.1f} kg/h); no limit has to follow it. Lowering a limit "
            "is a deliberate restriction, not a sensor alignment",
        ))
        return report

    for title in LIMIT_TITLES:
        entry = policy.entry(title)
        if entry is None:
            report.status = STATUS_REJECTED
            report.findings.append(_finding(
                "LIMIT_NOT_ALLOWED", "reject",
                f"{title} is not in the patch allowlist; the curve can be written but "
                "the limit cannot follow it",
            ))
            return report
        obj = get_object(objects, title)
        stock = read_engineering(bytes(image[offset:offset + 0x6000]), obj)[0]
        aligned = stock * ratio
        raw = int(round(obj.to_raw(aligned)))   # the planner rounds to nearest
        limit = (1 << obj.element_size_bits) - 1
        if raw > limit or raw < 0:
            report.status = STATUS_REJECTED
            report.findings.append(_finding(
                "NO_RAW_HEADROOM", "reject",
                f"{title}: {aligned:.2f} {obj.units} does not fit the cell "
                f"({obj.element_size_bits} bits, stock {stock:.2f}); the ceiling ratio "
                f"{ratio:.4f} is too large for this constant",
            ))
            return report
        achieved = obj.to_engineering(raw)
        report.details.setdefault("limits", {})[title] = {
            "description": obj.description,
            "units": obj.units,
            "address": f"0x{obj.address:04X}",
            "stock": round(stock, 4),
            "aligned": round(aligned, 4),
            "aligned_raw": raw,
            "aligned_representable": round(achieved, 4),
            "quantization_error_percent": round(
                abs(achieved - aligned) / max(abs(aligned), 1e-9) * 100.0, 5),
        }

    bound = policy.entry("c_maf_max").max_delta_percent
    report.details["ratio_percent"] = round((ratio - 1.0) * 100.0, 3)
    report.details["policy_delta_bound_percent"] = bound
    if bound is not None and (ratio - 1.0) * 100.0 > bound:
        report.status = STATUS_REJECTED
        report.findings.append(_finding(
            "CEILING_RISE_OUT_OF_SCOPE", "reject",
            f"the curve raises the ceiling by {(ratio - 1.0) * 100.0:.2f}%, above the "
            f"{bound:.0f}% the policy allows for the air-mass limits. A jump that "
            "large is a different project (different MAF housing or a forced-induction "
            "build) and needs its own review",
        ))
        return report

    proof, reason = saturation_proof(evidence)
    if proof is None:
        report.status = STATUS_REQUIRES_DATA
        report.findings.append(_finding("SATURATION_NOT_PROVEN", "info", reason))
        return report

    report.details["log_source"] = evidence.source if evidence else None
    report.details["logged_maf"] = {
        "voltage_max_v": float(proof["voltage_max_v"]),
        "saturated": True,
    }
    evidence_text = (f"curve_ceiling_ratio:{ratio:.4f}"
                     f"+log_saturation:{evidence.source}")

    requests = []
    for title, entry in report.details["limits"].items():
        requests.append({"symbol": title, "value": entry["aligned"]})
    plan = plan_patches(image, offset, image_type, objects, problems, requests, policy)
    report.plan_status = plan.status
    _extend_findings(report.findings, [
        _finding(finding.code, finding.severity, finding.message)
        for finding in plan.findings
    ])
    if plan.status != STATUS_READY:
        report.status = STATUS_REJECTED
        return report

    for change in plan.changes:
        title = change.symbol
        report.patches.append({
            "symbol": title,
            "value": change.new_engineering,
            "reason": (
                f"выравнивание предела по воздуху под новую кривую MAF "
                f"({curve.sensor}): потолок "
                f"{max(stock_values):.2f} -> {max(requested):.2f} кг/ч, "
                f"коэффициент {ratio:.4f} — тот же, что у кривой"
            ),
            "evidence": evidence_text,
            "requires_validation": True,
            "validation_hint": (
                "WOT-лог после прошивки: нагрузка не должна упираться в предел, "
                "а диагностика датчика (short circuit to battery) не должна "
                "появляться раньше, чем раньше"
            ),
        })

    report.findings.append(_finding(
        "LIMITS_ALIGNED", "info",
        f"{len(report.patches)} предел(а) по воздуху сдвинуты на {(ratio - 1) * 100:.2f}% "
        "вместе с кривой; пропорция сохраняет стоковые запасы, потому что "
        "документированной формулы «кг/ч -> мг/такт» в XDF нет",
    ))
    report.findings.append(_finding(
        "DIAG_LIMIT_FOLLOWS_SENSOR", "warning",
        f"{LIMIT_TITLES[1]} сдвинут вместе с датчиком, а не для того, чтобы скрыть "
        "неисправность: он остаётся пропорционален диапазону датчика, и после "
        "прошивки проверяется по логу",
    ))
    return report


def run(args: argparse.Namespace) -> int:
    policy = Policy.load(args.policy)
    evidence = None
    if args.evidence:
        try:
            evidence = load_evidence(args.evidence)
        except (EvidenceError, json.JSONDecodeError) as exc:
            raise SystemExit(f"error: {exc}")
    try:
        curve = load_curve(args.curve)
        report = build_report(args.bin, curve, evidence, policy)
    except (LimitsError, CurveError, PatchError, XdfError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    payload = {
        "version": 1,
        "status": report.status,
        "generator": "MS41/tools/maf_limits.py",
        "generated_from_sha256": report.source_sha256,
        "patches": report.patches,
        "unfulfilled": [] if report.status == STATUS_READY else [
            {"id": "maf_limits", "status": report.status,
             "detail": "; ".join(f["message"] for f in report.findings
                                 if f["severity"] != "info") or None}
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

    print(f"{report.status} {args.bin}")
    print(f"  ceiling {report.stock_ceiling_kgh:.2f} -> {report.new_ceiling_kgh:.2f} "
          f"kg/h (x{report.ratio:.4f}, +{(report.ratio - 1) * 100:.2f}%)")
    for title, entry in (report.details.get("limits") or {}).items():
        print(f"  {title}: {entry['stock']:.2f} -> {entry['aligned']:.2f} "
              f"{entry['units']} @ {entry['address']} "
              f"(raw {entry['aligned_raw']}, "
              f"sigma={entry['quantization_error_percent']}%)")
    if report.patches:
        print(f"  {len(report.patches)} limit(s) planned; patch file: "
              f"{args.out or '(not written, use --out)'}")
    for finding in report.findings:
        print(f"  [{finding['severity'].upper()}] {finding['code']}: {finding['message']}")
    return 1 if report.status == STATUS_REJECTED else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Align MS41 air-mass limits with a replacement MAF curve.")
    parser.add_argument("--bin", type=Path, required=True,
                        help="stock binary the calibration is built on")
    parser.add_argument("--curve", type=Path, required=True,
                        help="the documented curve file used for the MAF swap")
    parser.add_argument("--evidence", type=Path, default=None,
                        help="log evidence file (a stub until real logs exist)")
    parser.add_argument("--out", type=Path, default=None,
                        help="write the patch file here (only when READY)")
    parser.add_argument("--report", type=Path, default=None,
                        help="write the full report here")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--json", action="store_true", help="print JSON to stdout")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
